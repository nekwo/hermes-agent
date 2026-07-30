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

    def list_open_with_closed_count(self):
        """The incident-store contract ``build_snapshot`` reads since cc9db651f.

        Closed incidents are history-only in the steady-state frame, so the real
        store returns the open rows plus a COUNT of the closed tail rather than
        coercing thousands of closed files per snapshot. This double answers the
        same shape off whatever it was handed."""

        items = list(self.items)
        open_items = [item for item in items if getattr(item, "closed_at", None) is None]
        return open_items, len(items) - len(open_items)


class EmptyProofStore:
    def list_for_task(self, task_id):
        return []


class EmptyAgentStore:
    def list_all(self):
        return []


class EventLogStub:
    def __init__(self, events):
        self.events = events

    def tail(self, n):
        return list(self.events)[-n:]


def test_observability_flags_stale_daemon_stalled_run_and_repeated_context_requests():
    ts = now()
    task = Task(
        id="task_obs",
        title="Mission",
        description="d",
        state=TaskState.RUNNING,
        created_at=ts - timedelta(hours=2),
        updated_at=ts - timedelta(minutes=30),
        requested_by="human",
        context_requests=[
            {"actor": "dev", "paths": ["sensitive/path/one.dart"], "reason": "need private marker"},
            {"actor": "dev", "paths": ["sensitive/path/two.dart"], "reason": "need private marker"},
        ],
    )
    run = AgentRun(
        id="run_obs",
        persona_id="dev",
        task_id=task.id,
        stage_id=None,
        state=RunState.RUNNING,
        started_at=ts - timedelta(minutes=25),
        last_heartbeat_at=ts - timedelta(minutes=24),
    )
    incident = Incident(
        id="inc_obs",
        task_id=task.id,
        run_id=run.id,
        kind="model_invalid_output",
        summary="private marker should never leak",
        detail_path="C:/private/path.txt",
        opened_at=ts - timedelta(minutes=20),
    )

    obs = build_observability(
        tasks=[task],
        runs=[run],
        incidents=[incident],
        proofs=[],
        daemon_status={"state": "running", "pid": 123, "heartbeat_at": ts - timedelta(minutes=10)},
        events=[Event(ts=ts, type="run.opened", task_id=task.id, run_id=run.id, persona_id="dev")],
        reference_time=ts,
    )

    assert obs["schema_version"] == 1
    assert obs["health"]["status"] == "critical"
    assert obs["signals"]["stale_daemon"] is True
    assert obs["signals"]["stalled_running_runs"] == 1
    assert obs["active_runs"][0]["progress"] is None
    assert obs["signals"]["repeated_context_request_tasks"] == 1
    assert {item["kind"] for item in obs["interventions"]} >= {"daemon_stale", "run_stalled", "open_incident", "context_request_loop"}
    incident_intervention = next(item for item in obs["interventions"] if item["kind"] == "open_incident")
    assert incident_intervention["ask"] == "Open model_invalid_output incident requires review"
    assert incident_intervention["risk_if_ignored"]
    assert incident_intervention["allowed_actions"] == ["answer_intervention", "retry_stage"]
    assert incident_intervention["expires_at"] is None
    assert incident_intervention["safe_refs"] == {
        "task_id": task.id,
        "run_id": run.id,
        "incident_id": incident.id,
    }
    assert obs["recent_events"] == [
        {
            "ts": ts,
            "type": "run.opened",
            "task_id": task.id,
            "run_id": run.id,
            "persona_id": "dev",
            "display_kind": "event",
            "display_title": "run.opened",
        }
    ]
    encoded = json.dumps(obs, default=str)
    assert "private marker" not in encoded
    assert "C:/private" not in encoded
    assert "sensitive/path" not in encoded


def test_delivery_evidence_incidents_project_structured_operator_actions():
    ts = now()
    task = Task(
        id="task_stage_no_progress",
        title="Mission",
        description="d",
        state=TaskState.BLOCKED,
        created_at=ts,
        updated_at=ts,
        requested_by="human",
        open_incident_ids=["inc_stage_no_progress"],
    )
    incident = Incident(
        id="inc_stage_no_progress",
        task_id=task.id,
        run_id="run_empty",
        kind="stage_no_progress",
        summary="Stage repeated an empty delivery with no new proof evidence.",
        detail_path=None,
        opened_at=ts,
    )

    obs = build_observability(
        tasks=[task],
        runs=[],
        incidents=[incident],
        proofs=[],
        daemon_status={"state": "idle"},
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
        tasks=[],
        runs=[],
        incidents=[],
        proofs=[],
        daemon_status={"state": "offline"},
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
        tasks=[],
        runs=[],
        incidents=[],
        proofs=[],
        daemon_status={"state": "offline"},
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
        tasks=[],
        runs=[],
        incidents=[],
        proofs=[],
        daemon_status={"state": "offline"},
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
        tasks=[],
        runs=[],
        incidents=[],
        proofs=[],
        daemon_status={"state": "offline"},
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
        tasks=[],
        runs=[],
        incidents=[],
        proofs=[],
        daemon_status={"state": "offline"},
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

    obs = build_observability(tasks=[task], runs=runs, incidents=[], proofs=[], daemon_status={"state": "running", "pid": 1, "heartbeat_at": ts}, reference_time=ts)

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

    obs = build_observability(tasks=[task], runs=[run], incidents=[], proofs=[], daemon_status={"state": "offline"}, reference_time=ts)

    active = obs["active_runs"][0]
    assert active["progress"]["command_label"] == "flutter analyze lib/features/posts test/features/posts"
    assert active["active_tool"] == {
        "tool_name": "terminal",
        "status": "started",
        "summary": "Started tool terminal: flutter analyze lib/features/posts test/features/posts",
        "command_label": "flutter analyze lib/features/posts test/features/posts",
    }


def test_non_offline_daemon_without_heartbeat_is_critical():
    obs = build_observability(tasks=[], runs=[], incidents=[], proofs=[], daemon_status={"state": "running", "pid": 123}, execution_mode="daemon")

    assert obs["signals"]["stale_daemon"] is True
    assert any(item["kind"] == "daemon_stale" for item in obs["interventions"])
    assert obs["health"]["status"] == "critical"


def test_manual_mode_does_not_treat_offline_daemon_as_critical():
    obs = build_observability(tasks=[], runs=[], incidents=[], proofs=[], daemon_status={"state": "offline"}, execution_mode="manual")

    assert obs["execution_mode"] == "manual"
    assert not any(item["kind"] == "daemon_offline" for item in obs["interventions"])
    assert obs["health"]["status"] == "healthy"


def test_manual_mode_does_not_page_stale_idle_daemon_status():
    ts = now()
    obs = build_observability(
        tasks=[],
        runs=[],
        incidents=[],
        proofs=[],
        daemon_status={"state": "idle", "pid": 123, "heartbeat_at": ts - timedelta(minutes=10)},
        execution_mode="manual",
        reference_time=ts,
    )

    assert obs["signals"]["stale_daemon"] is False
    assert not any(item["kind"] == "daemon_stale" for item in obs["interventions"])
    assert obs["health"]["status"] == "healthy"


def test_daemon_mode_treats_offline_daemon_as_critical():
    obs = build_observability(tasks=[], runs=[], incidents=[], proofs=[], daemon_status={"state": "offline"}, execution_mode="daemon")

    assert obs["execution_mode"] == "daemon"
    assert any(item["kind"] == "daemon_offline" for item in obs["interventions"])
    assert obs["health"]["status"] == "critical"


def test_snapshot_embeds_observability_envelope():
    ts = now()
    task = Task(id="task_obs", title="Mission", description="d", state=TaskState.CREATED, created_at=ts, updated_at=ts, requested_by="human")

    snapshot = build_snapshot(
        task_store=Store([task]),
        run_store=Store([]),
        agent_store=EmptyAgentStore(),
        proof_store=EmptyProofStore(),
        incident_store=Store([]),
    )

    assert snapshot["observability"]["schema_version"] == 1
    assert snapshot["observability"]["health"]["status"] in {"healthy", "degraded", "critical"}
    assert "signals" in snapshot["observability"]
    assert "interventions" in snapshot["observability"]
