from __future__ import annotations

from datetime import timedelta
import sys

from hermes_time import now

from agent_runtime.events import EventLog
from agent_runtime.liveness import LivenessProbe, run_liveness_bounded_command
from agent_runtime.migrations import validate_runtime_config
from agent_runtime.models import AgentPersona, Event, Task
from agent_runtime.runtime_config import RuntimeConfig
from agent_runtime.states import RunState, TaskState, WorkerSessionState
from agent_runtime.store import IncidentStore, RunStore, TaskStore
from agent_runtime.worker_sessions import WorkerSessionStore


def _task(task_id: str = "task_live") -> Task:
    ts = now()
    return Task(
        id=task_id,
        title="Live goal",
        description="Exercise liveness",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="test",
    )


def _persona() -> AgentPersona:
    return AgentPersona(
        id="dev",
        display_name="Dev",
        role="dev",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=["terminal"],
        system_prompt_path="personas/dev/system.md",
    )


def test_liveness_quiet_run_warns_and_counts_worker_watchdog(isolate_agent_runtime_root):
    tasks = TaskStore()
    runs = RunStore()
    workers = WorkerSessionStore()
    tasks.create(_task())
    run = runs.open_run("dev", "task_live")
    run.last_heartbeat_at = now() - timedelta(seconds=100)
    runs.update(run)
    worker = workers.open(task_id="task_live", persona=_persona(), stage_id=None)
    workers.assign_run(worker.id, run)

    probe = LivenessProbe(
        config=RuntimeConfig(liveness_poll_seconds=30, liveness_quiet_strikes=2, liveness_hung_seconds=300),
        run_store=runs,
        worker_session_store=workers,
        task_store=tasks,
    )
    probe.poll_once()
    result = probe.poll_once()

    assert result["warnings"] == 1
    assert workers.get(worker.id).watchdog_warning_count == 1
    events = list(EventLog().iter_all())
    assert "run.liveness.warning" in {event.type for event in events}


def test_liveness_hung_run_cancels_and_links_run_hung_incident(isolate_agent_runtime_root):
    tasks = TaskStore()
    runs = RunStore()
    incidents = IncidentStore()
    workers = WorkerSessionStore()
    tasks.create(_task())
    run = runs.open_run("dev", "task_live")
    run.last_heartbeat_at = now() - timedelta(seconds=100)
    runs.update(run)
    worker = workers.open(task_id="task_live", persona=_persona(), stage_id=None)
    workers.assign_run(worker.id, run)

    probe = LivenessProbe(
        config=RuntimeConfig(liveness_poll_seconds=30, liveness_quiet_strikes=1, liveness_hung_seconds=5),
        run_store=runs,
        incident_store=incidents,
        worker_session_store=workers,
        task_store=tasks,
    )
    result = probe.poll_once()

    assert result["hung_runs"] == 1
    assert runs.get(run.id).state == RunState.CANCELLED
    open_incidents = incidents.list_open()
    assert [(incident.kind, incident.run_id) for incident in open_incidents] == [("run_hung", run.id)]
    assert open_incidents[0].id in tasks.get("task_live").open_incident_ids
    assert workers.get(worker.id).state == WorkerSessionState.CLOSED


def test_liveness_event_progress_resets_quiet_strikes(isolate_agent_runtime_root):
    tasks = TaskStore()
    runs = RunStore()
    tasks.create(_task())
    run = runs.open_run("dev", "task_live")
    run.last_heartbeat_at = now() - timedelta(seconds=100)
    runs.update(run)
    probe = LivenessProbe(
        config=RuntimeConfig(liveness_poll_seconds=30, liveness_quiet_strikes=1, liveness_hung_seconds=300),
        run_store=runs,
        task_store=tasks,
    )

    probe.poll_once()
    assert probe._states[run.id].classification == "quiet"
    EventLog().append(
        Event(
            ts=now(),
            type="run.progress",
            task_id="task_live",
            run_id=run.id,
            persona_id="dev",
            payload={"phase": "tool", "step": "tool_finished", "status": "passed", "summary": "Tool completed"},
        )
    )
    probe.poll_once()

    assert probe._states[run.id].classification == "advancing"
    assert probe._states[run.id].quiet_strikes == 0


def test_liveness_config_validation_rejects_hung_threshold_past_ttl():
    cfg = RuntimeConfig(heartbeat_ttl_seconds=60, liveness_hung_seconds=60)

    result = validate_runtime_config(cfg)

    assert result["ok"] is False
    assert any(error["field"] == "liveness_hung_seconds" for error in result["errors"])


def test_liveness_bounded_command_times_out_and_opens_command_hung(isolate_agent_runtime_root):
    tasks = TaskStore()
    runs = RunStore()
    incidents = IncidentStore()
    tasks.create(_task())
    run = runs.open_run("dev", "task_live")

    completed = run_liveness_bounded_command(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        shell=False,
        cwd=__import__("pathlib").Path.cwd(),
        timeout_seconds=1,
        task_id="task_live",
        run_id=run.id,
        persona_id="dev",
        run_store=runs,
        incident_store=incidents,
        task_store=tasks,
    )

    assert completed.timed_out is True
    assert [(incident.kind, incident.run_id) for incident in incidents.list_open()] == [("command_hung", run.id)]
    assert tasks.get("task_live").open_incident_ids == [incidents.list_open()[0].id]
