from __future__ import annotations

from datetime import timedelta

from hermes_time import now

from agent_runtime.events import EventLog
from agent_runtime.harness_doctor import run_harness_doctor
from agent_runtime.models import AgentPersona, Event, Incident
from types import SimpleNamespace

Task = SimpleNamespace
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


def _write_config(monkeypatch, tmp_path, body: str):
    p = tmp_path / "config.yaml"
    p.write_text(body, encoding="utf-8")
    monkeypatch.setattr("agent_runtime.config.get_config_path", lambda: p)
    return p


def test_harness_doctor_flags_shadowing_model_authority(isolate_agent_runtime_root, tmp_path, monkeypatch):
    _write_config(
        monkeypatch,
        tmp_path,
        "model:\n"
        "  default: gpt-5.6-luna\n"
        "agent_runtime:\n"
        "  default_model: gpt-5.5\n"
        "  personas:\n"
        "    neko_supervisor:\n"
        "      model: gpt-5.5\n"
        "    pm:\n"
        "      model: gpt-5.3-codex-spark\n",
    )

    report = run_harness_doctor(include_worktrees=False, snapshot_builder=lambda: {"runs": [], "tasks": []})

    authority = report["model_authority"]
    assert authority["available"] is True
    assert authority["divergent"] is True
    assert authority["harness_override"]["model_state"] == "shadowing"
    assert any("shadows the runtime default" in notice for notice in authority["notices"])
    # Informational only — a stale pin never turns the doctor into a fix job.
    assert report["summary"]["needs_fix"] is False


def test_harness_doctor_model_authority_clean_when_only_top_level(isolate_agent_runtime_root, tmp_path, monkeypatch):
    _write_config(monkeypatch, tmp_path, "model:\n  default: gpt-5.6-luna\n")

    report = run_harness_doctor(include_worktrees=False, snapshot_builder=lambda: {"runs": [], "tasks": []})

    authority = report["model_authority"]
    assert authority["divergent"] is False
    assert authority["harness_override"]["model_state"] == "absent"
    assert authority["notices"] == []
    assert authority["resolved"]["model"] == "gpt-5.6-luna"


def test_harness_doctor_reports_stale_runtime_and_snapshot_null_ids(isolate_agent_runtime_root):
    report = run_harness_doctor(
        stale_run_hours=1,
        stale_worker_hours=1,
        stale_task_days=1,
        include_worktrees=False,
        snapshot_builder=lambda: {"persona_instances": [{"persona_instance_id": None}]},
    )

    counts = report["summary"]["finding_counts"]
    assert counts["snapshot_null_id_rows"] == 1
    assert set(counts) == {"orphan_worktrees", "snapshot_null_id_rows"}
    assert report["findings"]["snapshot_null_id_rows"] == [
        {"collection": "persona_instances", "index": 0, "id_key": "persona_instance_id"}
    ]
    assert report["mode"] == {"fix": False, "dry_run": False}


def test_harness_doctor_fix_closes_stale_rows_and_is_idempotent(isolate_agent_runtime_root):
    dry = run_harness_doctor(
        fix=True,
        dry_run=True,
        stale_run_hours=1,
        stale_worker_hours=1,
        stale_task_days=1,
        include_worktrees=False,
        snapshot_builder=lambda: {},
    )
    assert dry["repairs"] == {"worktrees_reaped": [], "dry_run": True}

    fixed = run_harness_doctor(
        fix=True,
        stale_run_hours=1,
        stale_worker_hours=1,
        stale_task_days=1,
        include_worktrees=False,
        snapshot_builder=lambda: {},
    )

    assert fixed["repairs"] == {"worktrees_reaped": [], "dry_run": False}

    again = run_harness_doctor(
        fix=True,
        stale_run_hours=1,
        stale_worker_hours=1,
        stale_task_days=1,
        include_worktrees=False,
        snapshot_builder=lambda: {},
    )
    assert again["summary"]["finding_counts"] == {
        "orphan_worktrees": 0,
        "snapshot_null_id_rows": 0,
    }


def test_harness_doctor_sweeps_stale_duplicate_budget_incidents(isolate_agent_runtime_root):
    dry = run_harness_doctor(
        fix=True,
        dry_run=True,
        stale_incident_hours=1,
        include_worktrees=False,
        snapshot_builder=lambda: {},
    )
    assert "stale_incidents" not in dry["summary"]["finding_counts"]

    fixed = run_harness_doctor(
        fix=True,
        stale_incident_hours=1,
        include_worktrees=False,
        snapshot_builder=lambda: {},
    )
    assert "closed_incident_ids" not in fixed["repairs"]


def test_harness_doctor_accepts_stale_incident_days(isolate_agent_runtime_root):
    dry = run_harness_doctor(
        fix=True,
        dry_run=True,
        stale_incident_days=7,
        include_worktrees=False,
        snapshot_builder=lambda: {},
    )

    assert dry["thresholds"]["stale_incident_hours"] == 168
    assert dry["thresholds"]["stale_incident_days"] == 7
    assert "stale_incidents" not in dry["summary"]["finding_counts"]


def test_harness_doctor_reports_and_compacts_archived_event_rows(isolate_agent_runtime_root):
    dry = run_harness_doctor(
        fix=True,
        dry_run=True,
        compact_events=True,
        include_worktrees=False,
        snapshot_builder=lambda: {},
    )

    assert dry["findings"]["event_log_compaction"]["skipped"] == "mission_event_compaction_removed"

    fixed = run_harness_doctor(
        fix=True,
        compact_events=True,
        include_worktrees=False,
        snapshot_builder=lambda: {},
    )

    assert fixed["findings"]["event_log_compaction"]["removed_event_count"] == 0
