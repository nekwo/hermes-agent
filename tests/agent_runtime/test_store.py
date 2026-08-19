import json

import pytest

import agent_runtime.store as store_module
from hermes_time import now

from agent_runtime import paths
from agent_runtime.errors import NotFound
from agent_runtime.events import EventLog
from agent_runtime.models import AgentPersona, AgentRun, Event, Incident
from types import SimpleNamespace

Task = SimpleNamespace
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


def seed_run(
    *,
    run_id: str = "run_store_case",
    persona_id: str = "dev",
    task_id: str = "task_abc",
    state: RunState = RunState.RUNNING,
) -> AgentRun:
    """Persist a run row without ``RunStore.open_run``.

    The run store is historical/read-only. Seed representative persisted rows
    directly so the tests cover only its surviving reader surface.
    """

    ts = now()
    run = AgentRun(
        id=run_id,
        persona_id=persona_id,
        task_id=task_id,
        stage_id=None,
        state=state,
        started_at=ts,
        last_heartbeat_at=ts,
    )
    store_module._write_model(paths.run_path(run.id), run)
    return run


def test_run_store_is_historical_read_only():
    runs = RunStore()
    run = seed_run(state=RunState.COMPLETED)

    assert runs.get(run.id) == run
    assert not hasattr(runs, "update")
    assert not hasattr(runs, "list_for_task")


# The duplicate-active-run guard went with its writer: it lived inside
# RunStore.open_run, which S17 removed as write-dead (zero production callers
# after the mission lane). The removal itself is pinned in
# tests/agent_runtime/test_s17_run_store_residue_removal.py, not weakened here.


def test_mission_proof_store_is_removed():
    assert not hasattr(store_module, "ProofStore")


def test_incident_store_reads_open_and_closed_history():
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

    store_module._write_model(paths.incident_path(incident.id), incident)
    assert store.get("inc_1") == incident
    assert store.list_all() == [incident]

    incident.closed_at = now()
    store_module._write_model(paths.incident_path(incident.id), incident)
    # `list_all` is the whole read surface now: `list_open_with_closed_count`
    # was a snapshot-lane optimisation whose caller went with the incident
    # observability S9 removed, leaving these asserts as its only exercise.
    assert [item.closed_at for item in store.list_all()] == [incident.closed_at]
    assert not hasattr(store, "open")
    assert not hasattr(store, "close")
    assert not hasattr(store, "list_open_with_closed_count")

