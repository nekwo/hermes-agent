from __future__ import annotations

import json
from datetime import timedelta

from hermes_time import now

from agent_runtime.models import AgentRun, Event, Incident
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.observability import build_observability
from agent_runtime.snapshot import build_snapshot
from agent_runtime.states import RunState, TaskState


class Store:
    def __init__(self, items):
        self.items = items

    def list_all(self):
        return list(self.items)


class EmptyAgentStore:
    def list_all(self):
        return []


class EventLogStub:
    def __init__(self, events):
        self.events = events

    def tail(self, n):
        return list(self.events)[-n:]


def test_observability_flags_stalled_run_and_open_incident_without_leaking_details():
    # S28 removed the `tasks` / `proofs` / `daemon_status` parameters (both
    # callers passed literals), so this case lost its stale-daemon and
    # repeated-context-request halves. Its surviving subject — a stalled run and
    # an open incident projected redaction-safely — is unchanged and is what the
    # verb still measures. The daemon-lane pins moved to
    # tests/agent_runtime/test_s28_status_observe_shrink.py.
    ts = now()
    run = AgentRun(
        id="run_obs",
        persona_id="dev",
        task_id="task_obs",
        stage_id=None,
        state=RunState.RUNNING,
        started_at=ts - timedelta(minutes=25),
        last_heartbeat_at=ts - timedelta(minutes=24),
    )
    incident = Incident(
        id="inc_obs",
        task_id="task_obs",
        run_id=run.id,
        kind="model_invalid_output",
        summary="private marker should never leak",
        detail_path="C:/private/path.txt",
        opened_at=ts - timedelta(minutes=20),
    )

    obs = build_observability(
        runs=[run],
        incidents=[incident],
        events=[Event(ts=ts, type="run.opened", task_id="task_obs", run_id=run.id, persona_id="dev")],
        reference_time=ts,
    )

    assert obs["schema_version"] == 1
    assert obs["health"]["status"] == "critical"
    assert obs["signals"]["stalled_running_runs"] == 1
    assert obs["active_runs"][0]["progress"] is None
    assert {item["kind"] for item in obs["interventions"]} >= {"run_stalled", "open_incident"}
    incident_intervention = next(item for item in obs["interventions"] if item["kind"] == "open_incident")
    assert incident_intervention["ask"] == "Open model_invalid_output incident requires review"
    assert incident_intervention["risk_if_ignored"]
    assert incident_intervention["allowed_actions"] == ["answer_intervention", "retry_stage"]
    assert incident_intervention["expires_at"] is None
    assert incident_intervention["safe_refs"] == {
        "task_id": "task_obs",
        "run_id": run.id,
        "incident_id": incident.id,
    }
    assert obs["recent_events"] == [
        {
            "ts": ts,
            "type": "run.opened",
            "task_id": "task_obs",
            "run_id": run.id,
            "persona_id": "dev",
            "display_kind": "event",
            "display_title": "run.opened",
        }
    ]
    encoded = json.dumps(obs, default=str)
    assert "private marker" not in encoded
    assert "C:/private" not in encoded


def test_delivery_evidence_incidents_project_structured_operator_actions():
    ts = now()
    incident = Incident(
        id="inc_stage_no_progress",
        task_id="task_stage_no_progress",
        run_id="run_empty",
        kind="stage_no_progress",
        summary="Stage repeated an empty delivery with no new proof evidence.",
        detail_path=None,
        opened_at=ts,
    )

    obs = build_observability(
        runs=[],
        incidents=[incident],
        events=[],
        reference_time=ts,
    )

    intervention = obs["interventions"][0]
    assert intervention["kind"] == "stage_no_progress"
    assert intervention["severity"] == "high"
    assert intervention["allowed_actions"] == ["answer_intervention", "cancel_run", "rescope"]
    assert intervention["safe_refs"]["incident_id"] == incident.id


def test_recent_events_include_redaction_safe_progress_summary():
    ts = now()
    event = Event(
        ts=ts,
        type="run.progress",
        task_id="task_log",
        run_id="run_log",
        persona_id="dev",
        payload={
            "summary": "Running targeted Mission Control widget tests",
            "status": "running",
            "tool_name": "flutter test",
            "state": "waiting_on_tool",
            "elapsed_seconds": 12,
            "path": "C:/Users/example/private.txt",
            "unsafe_extra": "private marker",
        },
    )

    obs = build_observability(
        runs=[],
        incidents=[],
        events=[event],
        reference_time=ts,
    )

    assert obs["recent_events"] == [
        {
            "ts": ts,
            "type": "run.progress",
            "task_id": "task_log",
            "run_id": "run_log",
            "persona_id": "dev",
            "display_kind": "event",
            "display_title": "run.progress",
            "display_summary": "Running targeted Mission Control widget tests",
            "summary": "Running targeted Mission Control widget tests",
            "status": "running",
            "tool_name": "flutter test",
            "state": "waiting_on_tool",
            "elapsed_seconds": 12,
        }
    ]
    encoded = json.dumps(obs, default=str)
    assert "private marker" not in encoded
    assert "C:/Users" not in encoded


def test_legacy_proof_event_uses_generic_display_but_keeps_safe_quality_fields():
    # ``proof.attached`` was de-registered in S15 (no producer survives). This
    # pins the read side: a HISTORICAL log row carrying a de-registered type
    # still renders safely — the allowlist is checked on append only, never on
    # read, so old events never become unreadable.
    ts = now()
    event = Event(
        ts=ts,
        type="proof.attached",
        task_id="task_log",
        run_id="run_log",
        persona_id="dev",
        payload={
            "phase": "proof",
            "severity": "info",
            "step": "command_proof",
            "summary": "Command proof passed: exit 0, 467ms, proof proof_1",
            "status": "passed",
            "exit_code": 0,
            "duration_ms": 467,
            "proof_id": "proof_1",
            "next_expected": "request_qa_review",
            "detail": "C:/Users/example/private.log",
        },
    )

    obs = build_observability(
        runs=[],
        incidents=[],
        events=[event],
        reference_time=ts,
    )

    assert obs["recent_events"] == [
        {
            "ts": ts,
            "type": "proof.attached",
            "task_id": "task_log",
            "run_id": "run_log",
            "persona_id": "dev",
            "display_kind": "event",
            "display_title": "proof.attached",
            "display_summary": "Command proof passed: exit 0, 467ms, proof proof_1",
            "artifact_refs": [{"kind": "proof", "id": "proof_1"}],
            "phase": "proof",
            "severity": "info",
            "step": "command_proof",
            "summary": "Command proof passed: exit 0, 467ms, proof proof_1",
            "status": "passed",
            "exit_code": 0,
            "duration_ms": 467,
            "proof_id": "proof_1",
            "next_expected": "request_qa_review",
        }
    ]
    encoded = json.dumps(obs, default=str)
    assert "C:/Users" not in encoded


def test_recent_events_project_self_test_display_metadata():
    ts = now()
    event = Event(
        ts=ts,
        type="self_test.recorded",
        task_id="task_log",
        run_id="run_log",
        persona_id="dev",
        payload={
            "evidence_id": "selftest_123",
            "status": "passed",
            "stage_id": "stage_1",
            "command_label": "pytest tests/agent_runtime/test_observability.py -q",
            "redaction_status": "safe",
        },
    )

    obs = build_observability(
        runs=[],
        incidents=[],
        events=[event],
        reference_time=ts,
    )

    summary = obs["recent_events"][0]
    assert summary["display_kind"] == "self_test"
    assert summary["display_title"] == "Self-test passed"
    assert summary["display_summary"] == "passed"
    assert summary["artifact_refs"] == [{"kind": "evidence", "id": "selftest_123"}]
    assert summary["redaction_status"] == "safe"
    assert summary["evidence_id"] == "selftest_123"


def test_recent_events_project_historical_run_closed_display_metadata():
    ts = now()
    event = Event(
        ts=ts,
        type="run.closed",
        task_id="task_log",
        run_id="run_log",
        persona_id="custom_reviewer",
        payload={"state": "completed", "reason": "handoff complete"},
    )

    obs = build_observability(runs=[], incidents=[], events=[event], reference_time=ts)

    summary = obs["recent_events"][0]
    assert summary["display_kind"] == "run_closed"
    assert summary["display_title"] == "Run closed as completed"
    assert summary["display_summary"] == "handoff complete"


def test_recent_events_drop_path_and_credential_like_summaries():
    ts = now()
    unsafe_values = [
        "/home/user/project/.env",
        "/c/Users/beast/AppData/Local/run.log",
        "/x/Unreal Engine/Engine/private.log",
        "api" + "_key=abc123",
        "auth" + "orization header present",
        "bear" + "er credential marker",
        "cred" + "ential=abc123",
        "coo" + "kie=sessionid",
    ]

    obs = build_observability(
        runs=[],
        incidents=[],
        events=[
            Event(
                ts=ts,
                type="run.progress",
                task_id="task_log",
                run_id="run_log",
                persona_id="dev",
                payload={"summary": value},
            )
            for value in unsafe_values
        ],
        reference_time=ts,
    )

    assert all("summary" not in event for event in obs["recent_events"])
    encoded = json.dumps(obs, default=str)
    for value in unsafe_values:
        assert value not in encoded


def test_recent_events_drop_unsafe_values_from_new_dev_work_fields():
    ts = now()
    obs = build_observability(
        runs=[],
        incidents=[],
        events=[
            Event(
                ts=ts,
                type="run.progress",
                task_id="task_log",
                run_id="run_log",
                persona_id="dev",
                payload={
                    "phase": "proof",
                    "step": "command_proof",
                    "detail": "/home/user/private.log",
                    "next_expected": "~/private/plan.md",
                    "error_class": "bearer credential marker",
                    "proof_id": "proof_safe",
                },
            )
        ],
        reference_time=ts,
    )

    event = obs["recent_events"][0]
    assert event["phase"] == "proof"
    assert event["step"] == "command_proof"
    assert event["proof_id"] == "proof_safe"
    assert "detail" not in event
    assert "next_expected" not in event
    assert "error_class" not in event
    encoded = json.dumps(obs, default=str)
    assert "/home/user" not in encoded
    assert "bearer" not in encoded



def test_active_runs_include_queued_starting_running_and_waiting_states():
    ts = now()
    task = Task(id="task_active", title="Mission", description="d", state=TaskState.RUNNING, created_at=ts, updated_at=ts, requested_by="human")
    runs = [
        AgentRun(id="run_queued", persona_id="dev", task_id=task.id, stage_id="stage_1", state=RunState.QUEUED, started_at=ts, last_heartbeat_at=ts),
        AgentRun(id="run_starting", persona_id="dev", task_id=task.id, stage_id="stage_2", state=RunState.STARTING, started_at=ts, last_heartbeat_at=ts),
        AgentRun(id="run_running", persona_id="dev", task_id=task.id, stage_id="stage_3", state=RunState.RUNNING, started_at=ts, last_heartbeat_at=ts),
        AgentRun(id="run_tool", persona_id="dev", task_id=task.id, stage_id="stage_4", state=RunState.WAITING_ON_TOOL, started_at=ts, last_heartbeat_at=ts),
        AgentRun(id="run_approval", persona_id="qa", task_id=task.id, stage_id="stage_5", state=RunState.WAITING_ON_APPROVAL, started_at=ts, last_heartbeat_at=ts),
        AgentRun(id="run_done", persona_id="pm", task_id=task.id, stage_id=None, state=RunState.COMPLETED, started_at=ts, last_heartbeat_at=ts, finished_at=ts),
    ]

    obs = build_observability(runs=runs, incidents=[], reference_time=ts)

    assert obs["signals"]["active_runs"] == 5
    assert obs["signals"]["queued_runs"] == 1
    assert obs["signals"]["running_runs"] == 1
    assert obs["signals"]["waiting_runs"] == 2
    assert [run["run_id"] for run in obs["active_runs"]] == ["run_queued", "run_starting", "run_running", "run_tool", "run_approval"]


def test_active_run_summary_includes_current_tool_command_label():
    ts = now()
    task = Task(id="task_active_tool", title="Mission", description="d", state=TaskState.RUNNING, created_at=ts, updated_at=ts, requested_by="human")
    run = AgentRun(
        id="run_active_tool",
        persona_id="dev",
        task_id=task.id,
        stage_id="stage_1",
        state=RunState.RUNNING,
        started_at=ts,
        last_heartbeat_at=ts,
        progress={
            "type": "run.tool.started",
            "phase": "tool",
            "step": "tool_started",
            "tool_name": "terminal",
            "status": "started",
            "summary": "Started tool terminal: flutter analyze lib/features/posts test/features/posts",
            "command_label": "flutter analyze lib/features/posts test/features/posts",
        },
    )

    obs = build_observability(runs=[run], incidents=[], reference_time=ts)

    active = obs["active_runs"][0]
    assert active["progress"]["command_label"] == "flutter analyze lib/features/posts test/features/posts"
    assert active["active_tool"] == {
        "tool_name": "terminal",
        "status": "started",
        "summary": "Started tool terminal: flutter analyze lib/features/posts test/features/posts",
        "command_label": "flutter analyze lib/features/posts test/features/posts",
    }


# S28 retargeted the three daemon-mode cases that stood here
# (`test_non_offline_daemon_without_heartbeat_is_critical`,
# `test_manual_mode_does_not_page_stale_idle_daemon_status`,
# `test_daemon_mode_treats_offline_daemon_as_critical`). Their subject was the
# `daemon_status` parameter, which both callers passed as `None` — the Mission
# Daemon was retired before this wave, so every daemon signal, freshness row,
# and intervention was derived from a `{"state": "offline"}` default. The
# removal contract lives at
# tests/agent_runtime/test_s28_status_observe_shrink.py::test_build_observability_no_longer_accepts_the_literal_fed_parameters
# (`:94`) and ::test_the_envelope_drops_every_row_those_parameters_fed (`:116`).
# This comment named ::test_the_daemon_and_task_intervention_families_are_gone,
# which has never existed -- repointed MCF-78 2026-08-20.
# What survives from them is the health-status derivation itself, which is
# parameter-independent and is pinned here.


def test_health_is_healthy_with_no_interventions_and_names_its_execution_mode():
    obs = build_observability(runs=[], incidents=[], execution_mode="manual")

    assert obs["execution_mode"] == "manual"
    assert obs["interventions"] == []
    assert obs["health"]["status"] == "healthy"
    assert obs["health"]["summary"] == "Mission runtime observability is healthy"


def test_snapshot_embeds_observability_envelope():
    # The task/run/incident stores this used to inject are gone from
    # ``build_snapshot``'s signature: they were pass-throughs that reached
    # ``_build_snapshot_in_runtime_scope`` and were never loaded there, so
    # passing them changed nothing about the frame — including the absence
    # this test asserts.
    snapshot = build_snapshot(agent_store=EmptyAgentStore())

    # S9 removed mission/run/incident observability from the snapshot wire.
    assert "observability" not in snapshot
