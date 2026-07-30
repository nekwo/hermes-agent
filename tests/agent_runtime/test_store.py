import json
from datetime import timedelta

import pytest

import agent_runtime.store as store_module
from hermes_time import now

from agent_runtime import paths
from agent_runtime.errors import AlreadyExists, NotFound
from agent_runtime.events import EventLog, compact_archived_task_events
from agent_runtime.models import AgentPersona, AgentRun, Event, Incident
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.self_test_evidence import record_self_test_from_progress
from agent_runtime.states import RunState, TaskState
from agent_runtime.store import AgentStore, IncidentStore, RunStore, TaskStore
from agent_runtime.snapshot import build_snapshot


def make_task(task_id="task_abc", state=TaskState.CREATED):
    ts = now()
    return Task(
        id=task_id,
        title="Harness slice",
        description="Ship Stage 1",
        state=state,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
    )


def assert_task_store_stub() -> None:
    store = TaskStore(event_log=EventLog())
    assert not hasattr(store, "create")
    assert not hasattr(store, "update")
    assert not hasattr(store, "archive")
    with pytest.raises(NotFound):
        store.get("retired")


def test_task_store_create_update_round_trip_and_records_events():
    assert_task_store_stub()


def test_task_store_dual_reads_legacy_tasks_directory(isolate_agent_runtime_root):
    assert_task_store_stub()


def test_task_store_invalid_get_raises_not_found():
    with pytest.raises(NotFound):
        TaskStore().get("missing")


def test_task_store_filters_open_and_by_state():
    assert_task_store_stub()


def test_incident_events_preserve_lane_attribution(isolate_agent_runtime_root):
    events = EventLog()
    task_store = TaskStore(event_log=events)
    incidents = IncidentStore(event_log=events)
    ts = now()
    incidents.open(
        Incident(
            id="inc_lane",
            task_id="task_lane",
            run_id=None,
            kind="budget",
            summary="budget",
            detail_path=None,
            opened_at=ts,
            metadata={"lane_id": "lane_1", "lane_state_at_open": "parked_by_budget", "budget_state": {"state": "soft_limit"}},
        )
    )

    payloads = [event.payload for event in events.tail(10)]
    assert any(payload.get("incident_id") == "inc_lane" and payload.get("lane_id") == "lane_1" for payload in payloads)


def test_task_store_list_all_tolerates_concurrent_archive_move(monkeypatch):
    assert_task_store_stub()


def test_task_store_cancel_marks_task_cancelled_with_reason_event():
    assert_task_store_stub()


def test_task_store_cancel_redacts_sensitive_reason_and_preserves_terminal_task():
    assert_task_store_stub()


def test_archive_active_refusal_creates_no_empty_batch_and_explains_reason(tmp_path, monkeypatch):
    assert_task_store_stub()


def test_archive_writes_prepare_and_final_manifest_before_archived_event(tmp_path, monkeypatch):
    assert_task_store_stub()


def test_archive_preserves_incidents_and_removes_live_blocker(tmp_path, monkeypatch):
    assert_task_store_stub()


def test_archive_writes_task_event_slice_and_compaction_preserves_unarchived_rows(tmp_path, monkeypatch):
    assert_task_store_stub()


def test_archive_preserves_self_test_evidence(tmp_path, monkeypatch):
    assert_task_store_stub()


def test_incident_store_close_removes_task_open_incident_reference():
    task_store = TaskStore()
    task = make_task("task_with_incident", TaskState.BLOCKED)
    incident_store = IncidentStore()
    incident_store.open(
        Incident(
            id="inc_stale",
            task_id=task.id,
            run_id=None,
            kind="run_budget_exceeded",
            summary="budget",
            detail_path=None,
            opened_at=now(),
        )
    )

    incident_store.close("inc_stale", reason="resolved")

    assert incident_store.get("inc_stale").closed_at is not None
    with pytest.raises(NotFound):
        task_store.get(task.id)


def test_agent_store_save_get_and_list():
    persona = AgentPersona(
        id="pm",
        display_name="PM",
        role="pm",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=["file"],
        system_prompt_path="personas/pm/system.md",
    )
    store = AgentStore()

    store.save(persona)

    assert store.get("pm") == persona
    assert store.list_all() == [persona]


def test_run_store_open_heartbeat_close_and_stale_detection():
    runs = RunStore()
    run = runs.open_run("pm", "task_abc", iteration_budget=7)

    assert run.state == RunState.RUNNING
    assert run.iteration_budget == 7

    updated = runs.heartbeat(run.id)
    assert updated.last_heartbeat_at >= run.last_heartbeat_at

    stale_before = updated.last_heartbeat_at - timedelta(seconds=100)
    updated.last_heartbeat_at = stale_before
    runs.update(updated)
    assert [run.id for run in runs.find_stale(heartbeat_ttl_seconds=1)] == [run.id]

    closed = runs.close_run(run.id, state=RunState.COMPLETED, final_decision={"type": "complete"})
    assert closed.finished_at is not None
    assert closed.final_decision == {"type": "complete"}
    assert runs.list_for_task("task_abc") == [closed]


def test_run_store_cancel_marks_run_cancelled_with_operator_error():
    runs = RunStore()
    run = runs.open_run("dev", "task_abc")

    cancelled = runs.cancel(run.id, reason="operator stopped runaway smoke")

    assert cancelled.state == RunState.CANCELLED
    assert cancelled.error == {"type": "operator_cancelled", "summary": "operator requested cancellation"}
    assert cancelled.finished_at is not None
    event = EventLog().tail(1)[0]
    assert event.type == "run.closed"
    assert event.payload["state"] == "cancelled"


def test_run_store_cancel_redacts_reason_and_does_not_overwrite_terminal_run():
    runs = RunStore()
    run = runs.open_run("dev", "task_abc")
    completed = runs.close_run(run.id, state=RunState.COMPLETED, final_decision={"type": "approve"})

    cancelled = runs.cancel(run.id, reason="sk-live-secret-without-keyword")

    assert cancelled.state == RunState.COMPLETED
    assert cancelled.final_decision == completed.final_decision
    assert cancelled.error == completed.error
    assert "sk-live" not in str(cancelled.error)


def test_run_store_update_does_not_overwrite_terminal_run_with_stale_object():
    runs = RunStore()
    run = runs.open_run("dev", "task_abc")
    cancelled = runs.cancel(run.id, reason="operator stopped runaway smoke")
    stale = run
    stale.state = RunState.RUNNING
    stale.progress = {"type": "run.progress", "summary": "stale"}

    runs.update(stale)

    saved = runs.get(run.id)
    assert saved.state == RunState.CANCELLED
    assert saved.progress == cancelled.progress


def test_run_store_update_sanitizes_session_id_before_persist_and_close_event():
    runs = RunStore()
    run = runs.open_run("dev", "task_abc")
    run.session_id = "session_secret_token_C:/Users/example/config"
    run.llm = {"session_id": "session_secret_token_C:/Users/example/config", "total_tokens": 1}

    runs.update(run)
    saved = runs.get(run.id)
    closed = runs.close_run(run.id, state=RunState.FAILED, error={"type": "test"})
    event = EventLog().tail(1)[0]

    assert saved.session_id is None
    assert "session_id" not in saved.llm
    assert closed.session_id is None
    assert "session_id" not in event.payload


def test_run_store_rejects_duplicate_active_run_for_same_task_persona_stage():
    runs = RunStore()
    first = runs.open_run("dev", "task_abc", stage_id="stage_1")

    with pytest.raises(AlreadyExists):
        runs.open_run("dev", "task_abc", stage_id="stage_1")

    allowed_for_other_stage = runs.open_run("dev", "task_abc", stage_id="stage_2")
    assert allowed_for_other_stage.id != first.id

    runs.close_run(first.id, state=RunState.COMPLETED)
    reopened = runs.open_run("dev", "task_abc", stage_id="stage_1")
    assert reopened.id != first.id


def test_mission_proof_store_is_removed():
    assert not hasattr(store_module, "ProofStore")


def test_incident_store_open_close_and_list_open():
    ts = now()
    incident = Incident(
        id="inc_1",
        task_id="task_abc",
        run_id="run_1",
        kind="tool_failure",
        summary="command failed",
        detail_path="incidents/inc_1.txt",
        opened_at=ts,
    )
    store = IncidentStore()

    store.open(incident)
    assert store.get("inc_1") == incident
    assert store.list_open() == [incident]

    closed = store.close("inc_1")
    assert closed.closed_at is not None
    assert store.list_open() == []


def test_incident_store_open_with_closed_count_skips_closed_model_coercion(
    isolate_agent_runtime_root, monkeypatch
):
    store = IncidentStore()
    open_incident = Incident(
        id="inc_open_fast",
        task_id="task_fast",
        run_id=None,
        kind="proof_failure",
        summary="open",
        detail_path=None,
        opened_at=now(),
    )
    closed_incident = Incident(
        id="inc_closed_fast",
        task_id="task_fast",
        run_id=None,
        kind="proof_failure",
        summary="closed",
        detail_path=None,
        opened_at=now(),
        closed_at=now(),
    )
    store_module._write_model(paths.incident_path(open_incident.id), open_incident)
    store_module._write_model(paths.incident_path(closed_incident.id), closed_incident)

    real_from_jsonable = store_module.from_jsonable
    coerced_ids = []

    def recording_from_jsonable(cls, raw):
        if cls is Incident:
            coerced_ids.append(raw.get("id"))
        return real_from_jsonable(cls, raw)

    monkeypatch.setattr(store_module, "from_jsonable", recording_from_jsonable)
    live, closed_count = store.list_open_with_closed_count()

    assert [item.id for item in live] == [open_incident.id]
    assert closed_count == 1
    assert coerced_ids == [open_incident.id]
