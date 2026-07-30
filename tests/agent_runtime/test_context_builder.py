from __future__ import annotations

from hermes_time import now

from agent_runtime.context_builder import build_context, render_context
from agent_runtime.events import EventLog
from agent_runtime.models import AgentRun
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.states import RunState, TaskState


def _task_and_run() -> tuple[Task, AgentRun]:
    ts = now()
    task = Task(
        id="task_context",
        title="Chat-first runtime",
        description="Keep the operator context bounded.",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="test",
        affected_repos=["hermes-agent"],
        current_stage_id="retired-stage",
    )
    run = AgentRun(
        id="run_context",
        task_id=task.id,
        persona_id="dev",
        state=RunState.RUNNING,
        started_at=ts,
        last_heartbeat_at=ts,
        stage_id="retired-stage",
    )
    return task, run


def test_build_context_ignores_retired_stage_graph() -> None:
    task, run = _task_and_run()
    context = build_context(task, run, event_log=EventLog())

    assert context.current_stage is None
    assert context.mission_hud["task_id"] == task.id
    assert not any(key.startswith("typed_") for key in context.mission_hud)


def test_render_context_keeps_task_identity_without_stage_graph() -> None:
    task, run = _task_and_run()
    rendered = render_context(build_context(task, run, event_log=EventLog()))

    assert "Chat-first runtime" in rendered
    assert "typed_mission_plan" not in rendered


def test_validation_repair_survives_graph_removal() -> None:
    task, run = _task_and_run()
    context = build_context(
        task,
        run,
        event_log=EventLog(),
        requires_repair=True,
        repair_error="unknown payload key",
    )

    assert "validation_repair" in context.mission_hud
