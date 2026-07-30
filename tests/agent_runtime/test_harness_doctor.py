from __future__ import annotations

import inspect

import pytest

from agent_runtime.harness_doctor import run_harness_doctor
from agent_runtime.models import AgentPersona


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


def test_harness_doctor_reports_snapshot_null_ids(isolate_agent_runtime_root):
    report = run_harness_doctor(
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


def test_harness_doctor_fix_is_idempotent(isolate_agent_runtime_root):
    dry = run_harness_doctor(
        fix=True,
        dry_run=True,
        include_worktrees=False,
        snapshot_builder=lambda: {},
    )
    assert dry["repairs"] == {"worktrees_reaped": [], "dry_run": True}

    fixed = run_harness_doctor(
        fix=True,
        include_worktrees=False,
        snapshot_builder=lambda: {},
    )

    assert fixed["repairs"] == {"worktrees_reaped": [], "dry_run": False}

    again = run_harness_doctor(
        fix=True,
        include_worktrees=False,
        snapshot_builder=lambda: {},
    )
    assert again["summary"]["finding_counts"] == {
        "orphan_worktrees": 0,
        "snapshot_null_id_rows": 0,
    }


def test_harness_doctor_rejects_the_removed_mission_era_parameters(isolate_agent_runtime_root):
    # The legacy threshold/store kwargs and the compaction switch left with the
    # mission lane (doc 16). The CLI stopped passing them in 126976088; the
    # library surface follows.
    for kwarg in (
        "stale_run_hours",
        "stale_worker_hours",
        "stale_task_days",
        "stale_incident_hours",
        "stale_incident_days",
        "compact_events",
        "task_store",
        "run_store",
        "worker_store",
        "incident_store",
    ):
        with pytest.raises(TypeError):
            run_harness_doctor(
                include_worktrees=False,
                snapshot_builder=lambda: {},
                **{kwarg: 1},
            )
    params = set(inspect.signature(run_harness_doctor).parameters)
    assert params == {
        "fix",
        "dry_run",
        "worktree_min_age_seconds",
        "include_worktrees",
        "event_log",
        "snapshot_builder",
    }


def test_harness_doctor_thresholds_and_findings_carry_no_mission_rows(isolate_agent_runtime_root):
    report = run_harness_doctor(
        fix=True,
        dry_run=True,
        include_worktrees=False,
        snapshot_builder=lambda: {},
    )

    assert set(report["thresholds"]) == {"worktree_min_age_seconds", "include_worktrees"}
    assert "event_log_compaction" not in report["findings"]
    assert "stale_incidents" not in report["summary"]["finding_counts"]
    assert "closed_incident_ids" not in report["repairs"]
