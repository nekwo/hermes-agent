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
    assert set(counts) == {
        "orphan_worktrees",
        "snapshot_null_id_rows",
        "misplaced_root_only_keys",
    }
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
        "misplaced_root_only_keys": 0,
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


def _diverged_binding() -> "object":
    from agent_runtime.persona_profile_binding import EffectiveBinding

    return EffectiveBinding(
        persona_id="dev",
        config_profile="alice",
        store_profile="bob",
        config_declared=True,
        store_row_present=True,
        effective_profile="bob",
        source="store_wins",
        diverged=True,
    )


def test_harness_doctor_verdict_spans_the_persona_binding_section(
    isolate_agent_runtime_root, monkeypatch
):
    """A diverged binding is a finding, so it must move the verdict.

    ``needs_fix`` was derived from two of the five sections, so a report whose
    ``persona_binding`` block carried divergences (and the remediation command
    for them) still announced ``ok: true, needs_fix: false`` — the triage tool
    telling an operator to stop looking.
    """

    monkeypatch.setattr(
        "agent_runtime.persona_profile_binding.binding_index",
        lambda *_a, **_k: {"dev": _diverged_binding()},
    )

    report = run_harness_doctor(include_worktrees=False, snapshot_builder=lambda: {})

    assert report["persona_binding"]["diverged_count"] == 1
    assert report["persona_binding"]["health"] == "defect"
    assert report["summary"]["section_health"]["persona_binding"] == "defect"
    assert report["summary"]["defective_sections"] == ["persona_binding"]
    assert report["summary"]["needs_fix"] is True
    assert report["ok"] is False


def test_harness_doctor_reports_an_unexamined_section_instead_of_an_all_clear(
    isolate_agent_runtime_root, monkeypatch
):
    """A section whose probe RAISED clears ``ok`` without inventing a defect.

    The event log is read through ``stat`` on the live slice and the rotation
    manifest, which on this runtime's platform can fail under AV/share-violation
    contention. That must read as "not examined", not as a clean run.
    """

    def _boom():
        raise OSError(13, "share violation")

    monkeypatch.setattr("agent_runtime.harness_doctor.event_log_health", _boom)

    report = run_harness_doctor(include_worktrees=False, snapshot_builder=lambda: {})

    assert report["findings"]["event_log"]["health"] == "unknown"
    assert "share violation" in report["findings"]["event_log"]["error"]
    assert report["summary"]["unexamined_sections"] == ["event_log"]
    # Unknown is not a defect: nothing to fix, but nothing to clear either.
    assert report["summary"]["needs_fix"] is False
    assert report["ok"] is False


def test_harness_doctor_model_authority_error_is_unknown_not_ok(
    isolate_agent_runtime_root, monkeypatch
):
    monkeypatch.setattr(
        "agent_runtime.config.describe_runtime_default_authority",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("config.yaml is not a mapping")),
    )

    report = run_harness_doctor(include_worktrees=False, snapshot_builder=lambda: {})

    assert report["model_authority"]["available"] is False
    assert report["model_authority"]["health"] == "unknown"
    assert "model_authority" in report["summary"]["unexamined_sections"]
    assert report["ok"] is False


def test_harness_doctor_snapshot_crash_is_not_counted_as_a_null_id_row(
    isolate_agent_runtime_root,
):
    """A build CRASH must not be reported as an observation of null-id rows.

    It used to be returned as one ``snapshot_null_id_rows`` defect, so the
    counter named a defect class nobody looked for and sent the investigator
    hunting null ids in a frame that never built.
    """

    def _builder():
        raise RuntimeError("snapshot build exploded")

    report = run_harness_doctor(include_worktrees=False, snapshot_builder=_builder)

    assert report["findings"]["snapshot_null_id_rows"] == []
    # ``None`` — the class was not observed. A ``0`` here would be the same lie
    # in the other direction.
    assert report["summary"]["finding_counts"]["snapshot_null_id_rows"] is None
    assert report["findings"]["snapshot_build"]["health"] == "unknown"
    assert report["findings"]["snapshot_build"]["observed"] is False
    assert "snapshot build exploded" in report["findings"]["snapshot_build"]["error"]
    assert report["summary"]["needs_fix"] is False
    assert report["ok"] is False


def test_harness_doctor_clean_runtime_still_reads_ok(isolate_agent_runtime_root):
    """The derived verdict must not become permanently pessimistic."""

    report = run_harness_doctor(
        include_worktrees=False,
        snapshot_builder=lambda: {"agents": [{"persona_id": "dev"}]},
    )

    assert report["summary"]["section_health"] == {
        "orphan_worktrees": "ok",
        "snapshot_null_id_rows": "ok",
        "event_log": "ok",
        "model_authority": "ok",
        "persona_binding": "ok",
        "root_config_misplacement": "ok",
    }
    assert report["summary"]["needs_fix"] is False
    assert report["ok"] is True


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
