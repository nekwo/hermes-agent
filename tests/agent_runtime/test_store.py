import json
from datetime import timedelta

import pytest

import agent_runtime.store as store_module
from hermes_time import now

from agent_runtime import paths
from agent_runtime.errors import AlreadyExists, NotFound
from agent_runtime.events import EventLog, compact_archived_task_events
from agent_runtime.models import AgentPersona, AgentRun, Event, Incident, Task
from agent_runtime.self_test_evidence import record_self_test_from_progress
from agent_runtime.states import RunState, TaskState
from agent_runtime.store import AgentStore, IncidentStore, RunStore, TaskStore
from agent_runtime.snapshot import build_snapshot
from agent_runtime.transitions import apply_transition


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


def test_task_store_create_update_round_trip_and_records_events():
    store = TaskStore()
    task = store.create(make_task())

    assert paths.goal_path("task_abc").exists()
    assert not paths.legacy_task_path("task_abc").exists()

    apply_transition(task, TaskState.RUNNING, actor="pm", reason="triage")
    store.update(task, actor="pm", reason="triage")

    assert store.get("task_abc").state == TaskState.RUNNING
    events = EventLog().tail(10)
    assert [evt.type for evt in events] == ["task.created", "task.transition"]
    assert events[-1].payload == {"from": "created", "to": "running", "actor": "pm", "reason": "triage"}


def test_task_store_dual_reads_legacy_tasks_directory(isolate_agent_runtime_root):
    legacy = make_task("task_legacy", TaskState.BLOCKED)
    store_module._write_model(paths.legacy_task_path(legacy.id), legacy)

    store = TaskStore()

    assert store.get("task_legacy").state == TaskState.BLOCKED
    assert [task.id for task in store.list_all()] == ["task_legacy"]

    legacy.state = TaskState.RUNNING
    store.update(legacy, actor="test", reason="legacy compatibility")

    assert paths.legacy_task_path("task_legacy").exists()
    assert not paths.goal_path("task_legacy").exists()
    assert store.get("task_legacy").state == TaskState.RUNNING


def test_task_store_invalid_get_raises_not_found():
    with pytest.raises(NotFound):
        TaskStore().get("missing")


def test_task_store_filters_open_and_by_state():
    store = TaskStore()
    store.create(make_task("task_open", TaskState.RUNNING))
    store.create(make_task("task_done", TaskState.DONE))
    store.create(make_task("task_blocked", TaskState.BLOCKED))

    assert [task.id for task in store.list_open()] == ["task_blocked", "task_open"]
    assert [task.id for task in store.list_by_state(TaskState.DONE)] == ["task_done"]


def test_incident_events_preserve_lane_attribution(isolate_agent_runtime_root):
    events = EventLog()
    task_store = TaskStore(event_log=events)
    incidents = IncidentStore(event_log=events)
    ts = now()
    task_store.create(make_task("task_lane"))
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
    store = TaskStore()
    store.create(make_task("task_stable", TaskState.DONE))
    store.create(make_task("task_archiving", TaskState.DONE))
    original_read_model = store_module._read_model

    def read_model_with_archive_race(cls, path):
        if path.name == "task_archiving.json":
            raise NotFound(str(path))
        return original_read_model(cls, path)

    monkeypatch.setattr(store_module, "_read_model", read_model_with_archive_race)

    assert [task.id for task in store.list_all()] == ["task_stable"]


def test_task_store_cancel_marks_task_cancelled_with_reason_event():
    store = TaskStore()
    store.create(make_task("task_cancel", TaskState.RUNNING))

    cancelled = store.cancel("task_cancel", reason="operator stopped runaway smoke", actor="alice")

    assert cancelled.state == TaskState.CANCELLED
    assert store.get("task_cancel").state == TaskState.CANCELLED
    event = EventLog().tail(1)[0]
    assert event.type == "task.cancelled"
    assert event.payload["actor"] == "alice"
    assert event.payload["reason"] == "operator requested cancellation"


def test_task_store_cancel_redacts_sensitive_reason_and_preserves_terminal_task():
    store = TaskStore()
    store.create(make_task("task_done", TaskState.DONE))

    cancelled = store.cancel("task_done", reason="Bearer token=abc123SECRET", actor="alice")

    assert cancelled.state == TaskState.DONE
    assert "SECRET" not in EventLog().tail(10)[-1].payload.get("reason", "")


def test_archive_active_refusal_creates_no_empty_batch_and_explains_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    store = TaskStore()
    store.create(make_task("task_active", TaskState.RUNNING))

    result = store.archive("task_active", actor="cli", reason="operator archive")

    assert result["archive_batch"] is None
    assert result["archive_dir"] is None
    assert result["manifest_path"] is None
    assert result["archived_task_ids"] == []
    assert result["skipped_tasks"][0]["reason"] == "not_terminal"
    assert "only done/cancelled tasks" in result["skipped_tasks"][0]["message"]
    assert not (tmp_path / "runtime" / "deleted_archive").exists()
    assert store.get("task_active").state == TaskState.RUNNING


def test_archive_writes_prepare_and_final_manifest_before_archived_event(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    store = TaskStore()
    store.create(make_task("task_done", TaskState.DONE))

    result = store.archive("task_done", actor="cli", reason="Bearer token should redact")

    archive_dir = tmp_path / "runtime" / "deleted_archive" / result["archive_batch"]
    prepare = archive_dir / "manifest.prepare.json"
    final = archive_dir / "manifest.json"
    assert prepare.exists()
    assert final.exists()
    final_data = __import__("json").loads(final.read_text(encoding="utf-8"))
    assert final_data["prepare_manifest_path"] == "manifest.prepare.json"
    assert final_data["reason"] == "operator archive command"
    event = EventLog().tail(1)[0]
    assert event.type == "task.archived"
    assert event.payload["manifest_path"] == "manifest.json"
    assert event.payload["reason"] == "operator archive command"
    assert final.stat().st_mtime <= paths.events_path().stat().st_mtime


def test_archive_preserves_incidents_and_removes_live_blocker(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    task_store = TaskStore()
    task = make_task("task_cancelled", TaskState.CANCELLED)
    task.open_incident_ids = ["inc_archive"]
    task_store.create(task)
    incident_store = IncidentStore()
    incident_store.open(
        Incident(
            id="inc_archive",
            task_id=task.id,
            run_id=None,
            kind="runtime_freeze",
            summary="freeze finding",
            detail_path="incidents/inc_archive.txt",
            opened_at=now(),
        )
    )
    detail = paths.incident_detail_path("inc_archive")
    detail.parent.mkdir(parents=True, exist_ok=True)
    detail.write_text("redaction-safe incident detail\n", encoding="utf-8")

    result = task_store.archive(task.id, actor="cli", reason="cleanup terminal task")

    archive_dir = tmp_path / "runtime" / "deleted_archive" / result["archive_batch"]
    assert result["archived_tasks"][0]["incident_ids"] == ["inc_archive"]
    archived_incident = archive_dir / "incidents" / "inc_archive.json"
    assert archived_incident.exists()
    archived_incident_data = json.loads(archived_incident.read_text(encoding="utf-8"))
    assert archived_incident_data["closed_at"]
    assert (archive_dir / "incident_details" / "inc_archive.txt").exists()
    assert not paths.incident_path("inc_archive").exists()
    assert incident_store.list_open() == []
    closed_event = [event for event in EventLog().tail(10) if event.type == "incident.closed"][-1]
    assert closed_event.payload["reason"] == "task_archived"
    event = EventLog().tail(1)[0]
    assert event.type == "task.archived"
    assert event.payload["incident_count"] == 1


def test_archive_writes_task_event_slice_and_compaction_preserves_unarchived_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    task_store = TaskStore()
    task = make_task("task_done_events", TaskState.DONE)
    task_store.create(task)
    other = make_task("task_live_events", TaskState.RUNNING)
    task_store.create(other)
    log = EventLog()
    log.append(Event(now(), "run.progress", task.id, "run_archived", "dev", {"phase": "proof", "step": "test", "status": "passed"}))
    log.append(Event(now(), "run.progress", other.id, "run_live", "dev", {"phase": "proof", "step": "test", "status": "running"}))

    result = task_store.archive(task.id, actor="cli", reason="cleanup terminal task")

    archive_dir = tmp_path / "runtime" / "deleted_archive" / result["archive_batch"]
    archived_task = result["archived_tasks"][0]
    assert archived_task["event_count"] >= 2
    archived_events = archive_dir / archived_task["events_path"]
    assert archived_events.exists()
    archived_lines = archived_events.read_text(encoding="utf-8").splitlines()
    assert any('"task_id":"task_done_events"' in line for line in archived_lines)

    dry = compact_archived_task_events(dry_run=True)
    assert dry["removed_event_count"] == archived_task["event_count"]
    assert paths.events_path().exists()
    before_size = paths.events_path().stat().st_size
    fixed = compact_archived_task_events(dry_run=False)
    assert fixed["removed_event_count"] == archived_task["event_count"]
    assert fixed["watermark_reset"] is True
    assert paths.events_path().stat().st_size < before_size
    remaining = paths.events_path().read_text(encoding="utf-8")
    assert '"task_id":"task_live_events"' in remaining
    assert '"task_id":"task_done_events"' in remaining  # archive tombstone stays live; archived rows were copied first.


def test_archive_preserves_self_test_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    task_store = TaskStore()
    task = make_task("task_done_selftest", TaskState.DONE)
    task_store.create(task)
    run = AgentRun(
        id="run_selftest",
        persona_id="dev",
        task_id=task.id,
        stage_id="stage_1",
        state=RunState.COMPLETED,
        started_at=now(),
        last_heartbeat_at=now(),
    )
    evidence = record_self_test_from_progress(
        run,
        "run.tool.finished",
        {
            "tool_name": "terminal",
            "command": "pytest tests/agent_runtime/test_store.py -q",
            "exit_code": 0,
            "stdout": "passed",
        },
    )
    assert evidence is not None
    assert paths.self_test_task_dir(task.id).exists()

    result = task_store.archive(task.id, actor="cli", reason="cleanup terminal task")

    archive_dir = tmp_path / "runtime" / "deleted_archive" / result["archive_batch"]
    archived_task = result["archived_tasks"][0]
    assert archived_task["self_test_evidence_ids"] == [evidence.evidence_id]
    assert archived_task["self_test_evidence_archived"] is True
    assert (archive_dir / "self_tests" / task.id / f"{evidence.evidence_id}.json").exists()
    assert not paths.self_test_task_dir(task.id).exists()
    event = EventLog().tail(1)[0]
    assert event.type == "task.archived"
    assert event.payload["self_test_evidence_count"] == 1


def test_incident_store_close_removes_task_open_incident_reference():
    task_store = TaskStore()
    task = make_task("task_with_incident", TaskState.BLOCKED)
    task.open_incident_ids = ["inc_stale"]
    task_store.create(task)
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
    saved = task_store.get(task.id)
    assert saved.open_incident_ids == []
    assert saved.state == TaskState.RUNNING
    assert saved.harness_self_heal["stages"]["_mission"]["incident_close_counter"] == 1
    assert saved.harness_self_heal["stages"]["_mission"]["last_closed_incident_id"] == "inc_stale"

    incident_store.close("inc_stale", reason="already resolved")
    saved_again = task_store.get(task.id)
    assert saved_again.harness_self_heal["stages"]["_mission"]["incident_close_counter"] == 1


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
