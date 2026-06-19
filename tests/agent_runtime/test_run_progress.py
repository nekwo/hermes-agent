from hermes_time import now
from agent_runtime.events import EventLog
from agent_runtime.models import AgentRun
from agent_runtime.progress import RunProgressSink
from agent_runtime.states import RunState
from agent_runtime.store import RunStore


def test_run_progress_sink_updates_run_and_appends_safe_event(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    store = RunStore()
    run = store.open_run("dev", "task_1", None)
    sink = RunProgressSink(run_store=store, event_log=EventLog(), run_id=run.id)

    sink.emit(
        "run.model_call.started",
        {
            "state": "waiting_model",
            "prompt": "SECRET",
            "path": "C:/secret/file.txt",
            "detail": "/home/user/private.log",
            "summary": "bearer credential marker",
            "next_expected": "request_qa_review",
        },
    )

    updated = store.get(run.id)
    assert updated.progress["type"] == "run.model_call.started"
    assert updated.progress["state"] == "waiting_model"
    assert "prompt" not in updated.progress
    assert "path" not in updated.progress
    assert "detail" not in updated.progress
    assert "summary" not in updated.progress
    assert updated.progress["next_expected"] == "request_qa_review"
    events = EventLog().tail(1)
    assert events[0].type == "run.model_call.started"
    assert events[0].run_id == run.id
    assert updated.last_heartbeat_at >= run.last_heartbeat_at


def test_run_progress_sink_ignores_late_progress_after_terminal_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    store = RunStore()
    run = store.open_run("dev", "task_1", None)
    cancelled = store.close_run(run.id, state=RunState.CANCELLED, error={"type": "operator_cancelled"})
    events_before = EventLog().tail(10)
    sink = RunProgressSink(run_store=store, event_log=EventLog(), run_id=run.id)

    sink.emit("run.progress", {"state": "still_running", "summary": "late child process output"})

    updated = store.get(run.id)
    assert updated.state == RunState.CANCELLED
    assert updated.progress == cancelled.progress
    assert updated.last_heartbeat_at == cancelled.last_heartbeat_at
    assert EventLog().tail(10) == events_before
