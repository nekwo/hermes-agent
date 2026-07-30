from hermes_time import now
from agent_runtime.events import EventLog
from agent_runtime.models import AgentRun
from agent_runtime.progress import RunProgressSink
from agent_runtime.states import RunState
from agent_runtime.store import RunStore


def _seed_run(store: RunStore, *, run_id: str = "run_progress", task_id: str = "task_1") -> AgentRun:
    """Persist a run row without ``RunStore.open_run``.

    S17 removed ``open_run`` as write-dead (no production caller survived the
    mission lane). ``update`` is the surviving write path and tolerates a
    missing previous row; these tests cover ``RunProgressSink``, not the writer.
    """

    ts = now()
    run = AgentRun(
        id=run_id,
        persona_id="dev",
        task_id=task_id,
        stage_id=None,
        state=RunState.RUNNING,
        started_at=ts,
        last_heartbeat_at=ts,
    )
    assert store.update(run) is True
    return run



def test_run_progress_sink_updates_run_and_appends_safe_event(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    store = RunStore()
    run = _seed_run(store)
    sink = RunProgressSink(run_store=store, event_log=EventLog(), run_id=run.id)

    sink.emit(
        "run.progress",
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
    assert updated.progress["type"] == "run.progress"
    assert updated.progress["state"] == "waiting_model"
    assert "prompt" not in updated.progress
    assert "path" not in updated.progress
    assert "detail" not in updated.progress
    assert "summary" not in updated.progress
    assert updated.progress["next_expected"] == "request_qa_review"
    events = EventLog().tail(1)
    assert events[0].type == "run.progress"
    assert events[0].run_id == run.id
    assert updated.last_heartbeat_at >= run.last_heartbeat_at


def test_run_progress_sink_prunes_timing_progress_from_durable_log(tmp_path, monkeypatch):
    """``phase: timing`` run.progress is telemetry, not a durable authority fact.

    The event must NOT be appended to the durable EventLog (no reader consumes
    it; its durations live in the observability timing aggregate), but the run's
    live progress snapshot and heartbeat MUST still update so liveness/real-time
    telemetry is unaffected. A non-timing progress event in the same session
    still persists — proving the prune is phase-scoped, not a blanket drop.
    """
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    store = RunStore()
    run = _seed_run(store)
    baseline = EventLog().tail(50)
    sink = RunProgressSink(run_store=store, event_log=EventLog(), run_id=run.id)

    before_heartbeat = store.get(run.id).last_heartbeat_at
    sink.emit(
        "run.progress",
        {
            "phase": "timing",
            "step": "provider_stream_consume",
            "status": "completed",
            "summary": "Provider Stream Consume completed in 18138ms.",
            "duration_ms": 18138,
            "timing_key": "provider_stream_consume_ms",
        },
    )

    # Live snapshot + heartbeat updated…
    after_timing = store.get(run.id)
    assert after_timing.progress["phase"] == "timing"
    assert after_timing.progress["duration_ms"] == 18138
    assert after_timing.last_heartbeat_at >= before_heartbeat
    # …but nothing was appended to the durable authority log.
    assert EventLog().tail(50) == baseline

    # A non-timing progress event in the same run still persists.
    sink.emit(
        "run.progress",
        {
            "phase": "inspect",
            "step": "repo_context_loaded",
            "status": "running",
            "summary": "Repo context loaded",
        },
    )
    tail = EventLog().tail(50)
    assert len(tail) == len(baseline) + 1
    assert tail[-1].type == "run.progress"
    assert tail[-1].payload.get("phase") == "inspect"


def test_run_progress_sink_ignores_late_progress_after_terminal_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    store = RunStore()
    run = _seed_run(store)
    cancelled = store.close_run(run.id, state=RunState.CANCELLED, error={"type": "operator_cancelled"})
    events_before = EventLog().tail(10)
    sink = RunProgressSink(run_store=store, event_log=EventLog(), run_id=run.id)

    sink.emit("run.progress", {"state": "still_running", "summary": "late child process output"})

    updated = store.get(run.id)
    assert updated.state == RunState.CANCELLED
    assert updated.progress == cancelled.progress
    assert updated.last_heartbeat_at == cancelled.last_heartbeat_at
    assert EventLog().tail(10) == events_before
