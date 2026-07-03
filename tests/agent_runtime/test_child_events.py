from __future__ import annotations

from hermes_time import now

from agent_runtime.actions import HarnessActionType
from agent_runtime.child_events import emit_child_blocked
from agent_runtime.continuity import return_summary_to_parent_session
from agent_runtime.events import EventLog
from agent_runtime.models import AgentPersona, Task
from agent_runtime.progress import RunProgressSink
from agent_runtime.runtime_config import RuntimeConfig, SupervisionConfig
from agent_runtime.state_machine import MissionStateMachine
from agent_runtime.states import TaskState
from agent_runtime.store import RunStore, TaskStore
from agent_runtime.ticker import _commit_child_event_offset
from agent_runtime.persona_assignments import PersonaInstanceStore


def _persona(persona_id: str) -> AgentPersona:
    return AgentPersona(
        id=persona_id,
        display_name=persona_id.replace("_", " ").title(),
        role="dev" if persona_id != "neko_supervisor" else "alice_supervisor",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=["file"],
        system_prompt_path=f"personas/{persona_id}/system.md",
    )


def _task(task_id: str = "task_child_events") -> Task:
    ts = now()
    return Task(
        id=task_id,
        title="Child events",
        description="Exercise child event supervision",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="test",
    )


def _enabled_config() -> RuntimeConfig:
    return RuntimeConfig(supervision=SupervisionConfig(child_events_enabled=True), child_progress_min_interval_seconds=30)


def test_child_progress_is_throttled_and_does_not_wake_parent(isolate_agent_runtime_root):
    config = _enabled_config()
    tasks = TaskStore()
    runs = RunStore()
    instances = PersonaInstanceStore()
    task = tasks.create(_task())
    parent = instances.ensure_for_goal(_persona("neko_supervisor"), goal_id=task.id, spawned_by=None)
    child = instances.ensure_for_goal(_persona("dev"), goal_id=task.id, spawned_by=parent.id)
    run = runs.open_run("dev", task.id)

    sink = RunProgressSink(run_store=runs, event_log=EventLog(), run_id=run.id, config=config)
    sink.emit(
        "run.progress",
        {
            "phase": "tool",
            "step": "tool_started",
            "status": "running",
            "summary": "Started work",
            "persona_instance_id": child.id,
        },
    )
    sink.emit(
        "run.progress",
        {
            "phase": "tool",
            "step": "tool_running",
            "status": "running",
            "summary": "Still working",
            "persona_instance_id": child.id,
        },
    )

    child_progress = [event for event in EventLog().iter_all() if event.type == "child.progress"]
    assert len(child_progress) == 1
    action = MissionStateMachine(config=config).next_action(tasks.get(task.id))
    assert action.reason != "child status event requires parent supervision turn"


def test_child_returned_wakes_parent_and_advances_offset_only_after_commit(isolate_agent_runtime_root):
    config = _enabled_config()
    tasks = TaskStore()
    instances = PersonaInstanceStore()
    task = tasks.create(_task())
    parent = instances.ensure_for_goal(_persona("neko_supervisor"), goal_id=task.id, spawned_by=None)
    child = instances.ensure_for_goal(_persona("dev"), goal_id=task.id, spawned_by=parent.id)

    result = return_summary_to_parent_session(
        child.id,
        parent_session_id="parent_session_child_events",
        summary="Child completed the implementation.",
        proof_ids=["proof_1"],
        artifact_refs=["artifact_1"],
        task_id=task.id,
        stage_id="implement",
        child_events_enabled=True,
    )
    action = MissionStateMachine(config=config).next_action(tasks.get(task.id))

    assert result["ok"] is True
    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "neko_supervisor"
    assert action.parent_node_id == parent.id
    assert action.child_events_offset and action.child_events_offset > 0
    assert instances.get(parent.id).child_events_offset == 0
    assert _commit_child_event_offset(action, persona_store=instances) is True
    assert instances.get(parent.id).child_events_offset == action.child_events_offset
    event_types = [event.type for event in EventLog().iter_all()]
    assert "steer.returned" in event_types
    assert "child.returned" in event_types


def test_child_blocked_wakes_parent(isolate_agent_runtime_root):
    config = _enabled_config()
    tasks = TaskStore()
    instances = PersonaInstanceStore()
    task = tasks.create(_task())
    parent = instances.ensure_for_goal(_persona("neko_supervisor"), goal_id=task.id, spawned_by=None)
    child = instances.ensure_for_goal(_persona("dev"), goal_id=task.id, spawned_by=parent.id)

    assert emit_child_blocked(child_instance_id=child.id, reason="Needs missing input", task_id=task.id)

    action = MissionStateMachine(config=config).next_action(tasks.get(task.id))
    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "neko_supervisor"
