from __future__ import annotations

from datetime import timedelta

from hermes_time import now

from agent_runtime.harness_doctor import run_harness_doctor
from agent_runtime.models import AgentPersona, Incident, Task
from agent_runtime.errors import NotFound
from agent_runtime.states import RunState, TaskState, WorkerSessionState
from agent_runtime.store import IncidentStore, RunStore, TaskStore
from agent_runtime.worker_sessions import WorkerSessionStore


def _persona() -> AgentPersona:
    return AgentPersona(
        id="dev",
        display_name="Dev",
        role="dev",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=[],
        system_prompt_path="",
    )


def test_harness_doctor_reports_stale_runtime_and_snapshot_null_ids(isolate_agent_runtime_root):
    stamp = now() - timedelta(days=3)
    tasks = TaskStore()
    stale_task = tasks.create(
        Task(
            id="task_stale_open",
            title="Stale",
            description="d",
            state=TaskState.RUNNING,
            created_at=stamp,
            updated_at=stamp,
            requested_by="test",
        )
    )
    run = RunStore().open_run("dev", "task_active", stage_id="implement")
    run.last_heartbeat_at = now() - timedelta(hours=8)
    RunStore().update(run)
    worker = WorkerSessionStore().open(task_id="task_worker", persona=_persona(), stage_id="implement")
    worker.last_heartbeat_at = now() - timedelta(hours=8)
    WorkerSessionStore().update(worker)

    report = run_harness_doctor(
        stale_run_hours=1,
        stale_worker_hours=1,
        stale_task_days=1,
        include_worktrees=False,
        snapshot_builder=lambda: {"runs": [{"run_id": None}], "tasks": [{"task_id": stale_task.id}]},
    )

    counts = report["summary"]["finding_counts"]
    assert counts["stale_runs"] == 1
    assert counts["stale_workers"] == 1
    assert counts["stale_open_tasks"] == 1
    assert counts["snapshot_null_id_rows"] == 1
    assert report["findings"]["stale_runs"][0]["run_id"] == run.id
    assert report["findings"]["stale_workers"][0]["worker_session_id"] == worker.id
    assert report["findings"]["stale_open_tasks"][0]["task_id"] == stale_task.id
    assert report["findings"]["snapshot_null_id_rows"] == [{"collection": "runs", "index": 0, "id_key": "run_id"}]
    assert report["mode"] == {"fix": False, "dry_run": False}


def test_harness_doctor_fix_closes_stale_rows_and_is_idempotent(isolate_agent_runtime_root):
    stamp = now() - timedelta(days=3)
    tasks = TaskStore()
    stale_task = tasks.create(
        Task(
            id="task_fix_stale_open",
            title="Stale",
            description="d",
            state=TaskState.RUNNING,
            created_at=stamp,
            updated_at=stamp,
            requested_by="test",
        )
    )
    runs = RunStore()
    run = runs.open_run("dev", "task_fix_run", stage_id="implement")
    run.last_heartbeat_at = now() - timedelta(hours=8)
    runs.update(run)
    workers = WorkerSessionStore()
    worker = workers.open(task_id="task_fix_worker", persona=_persona(), stage_id="implement")
    worker.last_heartbeat_at = now() - timedelta(hours=8)
    workers.update(worker)

    dry = run_harness_doctor(
        fix=True,
        dry_run=True,
        stale_run_hours=1,
        stale_worker_hours=1,
        stale_task_days=1,
        include_worktrees=False,
        snapshot_builder=lambda: {},
    )
    assert dry["repairs"]["stale_run_ids"] == [run.id]
    assert dry["repairs"]["archived_task_ids"] == [stale_task.id]
    assert runs.get(run.id).state == RunState.RUNNING

    fixed = run_harness_doctor(
        fix=True,
        stale_run_hours=1,
        stale_worker_hours=1,
        stale_task_days=1,
        include_worktrees=False,
        snapshot_builder=lambda: {},
    )

    assert fixed["repairs"]["stale_run_ids"] == [run.id]
    assert fixed["repairs"]["closed_worker_session_ids"] == [worker.id]
    assert fixed["repairs"]["archived_task_ids"] == [stale_task.id]
    assert runs.get(run.id).state == RunState.STALE
    assert workers.get(worker.id).state == WorkerSessionState.CLOSED
    try:
        tasks.get(stale_task.id)
    except NotFound:
        pass
    else:
        raise AssertionError("stale task should be archived out of the live store")
    open_kinds = {incident.kind for incident in IncidentStore().list_open()}
    assert "stale_run" in open_kinds

    again = run_harness_doctor(
        fix=True,
        stale_run_hours=1,
        stale_worker_hours=1,
        stale_task_days=1,
        include_worktrees=False,
        snapshot_builder=lambda: {},
    )
    assert again["summary"]["finding_counts"]["stale_runs"] == 0
    assert again["summary"]["finding_counts"]["stale_workers"] == 0
    assert again["summary"]["finding_counts"]["stale_open_tasks"] == 0
    assert len([incident for incident in IncidentStore().list_open() if incident.kind == "stale_run"]) == 1


def test_harness_doctor_sweeps_stale_duplicate_budget_incidents(isolate_agent_runtime_root):
    stamp = now() - timedelta(days=2)
    task = TaskStore().create(
        Task(
            id="task_budget_flood",
            title="Budget flood",
            description="d",
            state=TaskState.BLOCKED,
            created_at=stamp,
            updated_at=stamp,
            requested_by="test",
        )
    )
    incidents = IncidentStore()
    ids = []
    for index in range(3):
        incident = incidents.open(
            Incident(
                id=f"inc_budget_{index}",
                task_id=task.id,
                run_id=None,
                kind="mission_budget_exceeded",
                summary="Mission token budget exceeded: total_tokens=2/1",
                detail_path=None,
                opened_at=stamp + timedelta(minutes=index),
                metadata={"source": "test"},
            )
        )
        ids.append(incident.id)
        task.open_incident_ids.append(incident.id)
    TaskStore().update(task, actor="test", reason="seed incident flood")

    dry = run_harness_doctor(
        fix=True,
        dry_run=True,
        stale_incident_hours=1,
        include_worktrees=False,
        snapshot_builder=lambda: {},
    )
    assert dry["summary"]["finding_counts"]["stale_incidents"] == 2
    assert dry["repairs"]["closed_incident_count_by_kind"] == {"mission_budget_exceeded": 2}

    fixed = run_harness_doctor(
        fix=True,
        stale_incident_hours=1,
        include_worktrees=False,
        snapshot_builder=lambda: {},
    )
    assert fixed["repairs"]["closed_incident_count_by_kind"] == {"mission_budget_exceeded": 2}
    open_budget_ids = [incident.id for incident in IncidentStore().list_open()]
    assert open_budget_ids == [ids[-1]]
    assert TaskStore().get(task.id).open_incident_ids == [ids[-1]]


def test_harness_doctor_accepts_stale_incident_days(isolate_agent_runtime_root):
    stamp = now() - timedelta(days=8)
    TaskStore().create(
        Task(
            id="task_terminal_incident",
            title="Terminal incident",
            description="d",
            state=TaskState.DONE,
            created_at=stamp,
            updated_at=stamp,
            requested_by="test",
        )
    )
    IncidentStore().open(
        Incident(
            id="inc_terminal_old",
            task_id="task_terminal_incident",
            run_id=None,
            kind="runtime_freeze",
            summary="old terminal incident",
            detail_path=None,
            opened_at=stamp,
            metadata={"source": "test"},
        )
    )

    dry = run_harness_doctor(
        fix=True,
        dry_run=True,
        stale_incident_days=7,
        include_worktrees=False,
        snapshot_builder=lambda: {},
    )

    assert dry["thresholds"]["stale_incident_hours"] == 168
    assert dry["thresholds"]["stale_incident_days"] == 7
    assert dry["summary"]["finding_counts"]["stale_incidents"] == 1
    assert dry["repairs"]["closed_incident_ids"] == ["inc_terminal_old"]
