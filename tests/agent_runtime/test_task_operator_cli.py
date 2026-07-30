import argparse
import json

from hermes_time import now

from agent_runtime.models import Incident
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.states import TaskState
from agent_runtime.store import IncidentStore, TaskStore
from hermes_cli import harness as harness_cli


def _task(task_id: str, state: TaskState = TaskState.CREATED) -> Task:
    ts = now()
    return Task(
        id=task_id,
        title="Operator CLI regression",
        description="d",
        state=state,
        created_at=ts,
        updated_at=ts,
        requested_by="test",
    )


def test_task_show_archived_task_returns_archive_metadata_without_traceback(capsys, isolate_agent_runtime_root):
    store = TaskStore()
    task = _task("task_archived_show", TaskState.DONE)
    store.create(task)
    archive = store.archive(task.id, actor="cli", reason="test archive")
    assert archive["archived_count"] == 1

    rc = harness_cli._cmd_task_show(
        argparse.Namespace(task_id=task.id, events=0, since=None, json=True)
    )

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["archived"] is True
    assert data["task_id"] == task.id
    assert data["archive_batch"] == archive["archive_batch"]
    assert data["manifest_path"].endswith("manifest.json")
    assert data["task"]["id"] == task.id


def test_task_unblock_rescope_closes_stale_incidents_and_clears_recovery_markers(capsys, isolate_agent_runtime_root):
    store = TaskStore()
    incident_store = IncidentStore()
    task = _task("task_blocked_unblock", TaskState.BLOCKED)
    task.open_incident_ids = ["inc_listed"]
    task.risk_flags = ["neko_block_recovery_attempted", "keep_me"]
    task.current_stage_id = "stage_old"
    task.affected_repos = ["Launcher"]
    task.assigned_persona_ids = {"stage_old": "dev"}
    task.harness_self_heal = {
        "stages": {
            "_mission": {
                "last_block_recovery_signal": "1:unknown:none:0",
                "last_closed_incident_id": "inc_stale",
                "incident_close_counter": 2,
            },
            "stage_old": {
                "last_budget_recovery_signal": "old",
                "kept_observation": "preserve",
            },
        },
        "other": {"preserve": True},
    }
    store.create(task)
    incident_store.open(
        Incident(
            id="inc_listed",
            task_id=task.id,
            run_id=None,
            kind="blocked",
            summary="listed incident",
            detail_path=None,
            opened_at=now(),
        )
    )
    incident_store.open(
        Incident(
            id="inc_unlisted",
            task_id=task.id,
            run_id=None,
            kind="blocked",
            summary="unlisted incident",
            detail_path=None,
            opened_at=now(),
        )
    )

    rc = harness_cli._cmd_task_unblock(
        argparse.Namespace(
            task_id=task.id,
            reason="operator re-arm",
            state="created",
            rescope=True,
            foreground=False,
            json=True,
        )
    )

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["from"] == "blocked"
    assert data["to"] == "created"
    assert data["rescope"] is True
    assert sorted(data["closed_incident_ids"]) == ["inc_listed", "inc_unlisted"]
    assert "stages._mission.last_block_recovery_signal" in data["cleared_recovery_keys"]

    saved = store.get(task.id)
    assert saved.state == TaskState.CREATED
    assert saved.open_incident_ids == []
    assert saved.risk_flags == ["keep_me"]
    assert not hasattr(saved, "mission_plan")
    assert not hasattr(saved, "stages")
    assert saved.current_stage_id is None
    assert saved.affected_repos == []
    assert saved.assigned_persona_ids == {}
    assert saved.harness_self_heal["other"] == {"preserve": True}
    assert "_mission" not in saved.harness_self_heal.get("stages", {})
    assert saved.harness_self_heal["stages"]["stage_old"] == {"kept_observation": "preserve"}
    assert incident_store.get("inc_listed").closed_at is not None
    assert incident_store.get("inc_unlisted").closed_at is not None


def test_task_unblock_refuses_archived_task(capsys, isolate_agent_runtime_root):
    store = TaskStore()
    task = _task("task_archived_unblock", TaskState.DONE)
    store.create(task)
    store.archive(task.id, actor="cli", reason="test archive")

    rc = harness_cli._cmd_task_unblock(
        argparse.Namespace(
            task_id=task.id,
            reason="operator re-arm",
            state="created",
            rescope=False,
            foreground=False,
            json=True,
        )
    )

    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert data["error"] == "task_archived"
