from __future__ import annotations

from hermes_time import now

from agent_runtime.actions import HarnessActionType
from agent_runtime.child_events import emit_child_returned
from agent_runtime.events import EventLog
from agent_runtime.models import AgentPersona, Proof, Task
from agent_runtime.proof_rules import ProofType
from agent_runtime.runtime_config import RuntimeConfig, SupervisionConfig
from agent_runtime.states import TaskState
from agent_runtime.store import ProofStore, TaskStore
from agent_runtime.persona_assignments import PersonaInstanceStore
from agent_runtime.state_machine import MissionStateMachine
from agent_runtime.supervision import direct_children, distilled_child_context, enforce_supervision_caps


def _persona(persona_id: str) -> AgentPersona:
    return AgentPersona(
        id=persona_id,
        display_name=persona_id,
        role="dev",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=[],
        system_prompt_path=f"personas/{persona_id}/system.md",
    )


def _task(task_id: str = "task_recursive") -> Task:
    ts = now()
    return Task(id=task_id, title="Recursive", description="d", state=TaskState.RUNNING, created_at=ts, updated_at=ts, requested_by="test")


def test_supervision_context_is_direct_children_only(isolate_agent_runtime_root):
    task = TaskStore().create(_task())
    store = PersonaInstanceStore()
    root = store.ensure_for_goal(_persona("neko_supervisor"), goal_id=task.id, spawned_by=None)
    child = store.ensure_for_goal(_persona("backend_dev"), goal_id=task.id, spawned_by=root.id)
    grandchild = store.ensure_for_goal(_persona("dev"), goal_id=task.id, spawned_by=child.id)

    emit_child_returned(child=child, summary="Backend done", proof_ids=[], artifact_refs=[], task_id=task.id, stage_id="backend")
    emit_child_returned(child=grandchild, summary="Grandchild detail", proof_ids=[], artifact_refs=[], task_id=task.id, stage_id="leaf")

    assert [item.id for item in direct_children(root.id, goal_id=task.id, store=store)] == [child.id]
    context = distilled_child_context(root.id, task_id=task.id, store=store)
    assert [item["child_node_id"] for item in context] == [child.id]
    assert "Grandchild detail" not in str(context)


def test_supervision_caps_log_when_fanout_or_depth_exceeded(isolate_agent_runtime_root):
    task = TaskStore().create(_task())
    store = PersonaInstanceStore()
    root = store.ensure_for_goal(_persona("neko_supervisor"), goal_id=task.id, spawned_by=None)
    parent = root
    for index in range(4):
        child = store.ensure_for_goal(_persona(f"dev_{index}"), goal_id=task.id, spawned_by=root.id, placement_id=f"{task.id}:dev_{index}")
        parent = child if index == 0 else parent
    leaf = parent
    for depth in range(3):
        leaf = store.ensure_for_goal(_persona(f"leaf_{depth}"), goal_id=task.id, spawned_by=leaf.id, placement_id=f"{task.id}:leaf_{depth}")

    result = enforce_supervision_caps(root.id, goal_id=task.id, store=store)

    assert result["ok"] is False
    assert {hit["cap"] for hit in result["hits"]} == {"max_children_per_steerer", "max_depth"}
    assert [event.type for event in EventLog().iter_all()].count("steer.cap_hit") == 2


def test_recursive_child_return_requires_passed_harness_proof(isolate_agent_runtime_root):
    task_store = TaskStore()
    proof_store = ProofStore()
    task = task_store.create(_task())
    store = PersonaInstanceStore()
    parent = store.ensure_for_goal(_persona("neko_supervisor"), goal_id=task.id, spawned_by=None)
    child = store.ensure_for_goal(_persona("dev"), goal_id=task.id, spawned_by=parent.id)
    proof_store.attach(
        Proof(
            id="proof_failed_child",
            task_id=task.id,
            stage_id="implement",
            type=ProofType.TEST_RUN,
            title="failed",
            path_or_value="pytest",
            created_by="test",
            created_at=now(),
            metadata={"status": "failed"},
            redaction_status="safe",
        )
    )
    emit_child_returned(child=child, summary="Done, trust me", proof_ids=["proof_failed_child"], artifact_refs=[], task_id=task.id, stage_id="implement")
    config = RuntimeConfig(supervision=SupervisionConfig(child_events_enabled=True, recursive_enabled=True))

    action = MissionStateMachine(config=config).next_action(task_store.get(task.id))

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "neko_supervisor"
    assert action.reason == "child return failed recursive gate; parent supervision turn required"
    blocked_events = [event for event in EventLog().iter_all() if event.type == "child.blocked"]
    assert blocked_events
    assert "recursive gate failed" in blocked_events[-1].payload["reason"]


def test_recursive_child_return_rejects_agent_observed_trace(isolate_agent_runtime_root):
    task_store = TaskStore()
    proof_store = ProofStore()
    task = task_store.create(_task("task_recursive_trace"))
    store = PersonaInstanceStore()
    parent = store.ensure_for_goal(_persona("neko_supervisor"), goal_id=task.id, spawned_by=None)
    child = store.ensure_for_goal(_persona("dev"), goal_id=task.id, spawned_by=parent.id)
    proof_store.attach(
        Proof(
            id="proof_observed_child",
            task_id=task.id,
            stage_id="implement",
            type=ProofType.TEST_RUN,
            title="observed",
            path_or_value="trace",
            created_by="dev",
            created_at=now(),
            metadata={"status": "passed", "source": "agent_tool_trace", "authoritative": False},
            redaction_status="safe",
        )
    )
    emit_child_returned(child=child, summary="Done", proof_ids=["proof_observed_child"], artifact_refs=[], task_id=task.id, stage_id="implement")
    config = RuntimeConfig(supervision=SupervisionConfig(child_events_enabled=True, recursive_enabled=True))

    action = MissionStateMachine(config=config).next_action(task_store.get(task.id))

    assert action.reason == "child return failed recursive gate; parent supervision turn required"
    blocked_events = [event for event in EventLog().iter_all() if event.type == "child.blocked"]
    assert blocked_events[-1].payload["reason"] == "recursive gate failed: child_proof_not_harness_owned"


def test_recursive_child_return_accepts_harness_owned_stage_proof(isolate_agent_runtime_root):
    task_store = TaskStore()
    proof_store = ProofStore()
    task = task_store.create(_task("task_recursive_harness"))
    store = PersonaInstanceStore()
    parent = store.ensure_for_goal(_persona("neko_supervisor"), goal_id=task.id, spawned_by=None)
    child = store.ensure_for_goal(_persona("dev"), goal_id=task.id, spawned_by=parent.id)
    proof_store.attach(
        Proof(
            id="proof_harness_child",
            task_id=task.id,
            stage_id="implement",
            type=ProofType.TEST_RUN,
            title="harness",
            path_or_value="artifact",
            created_by="harness",
            created_at=now(),
            metadata={"status": "passed", "proof_intent": "authoritative_gate_after_hand_off"},
            redaction_status="safe",
        )
    )
    emit_child_returned(child=child, summary="Done", proof_ids=["proof_harness_child"], artifact_refs=[], task_id=task.id, stage_id="implement")
    config = RuntimeConfig(supervision=SupervisionConfig(child_events_enabled=True, recursive_enabled=True))

    action = MissionStateMachine(config=config).next_action(task_store.get(task.id))

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.reason == "child status event requires parent supervision turn"
    assert not [event for event in EventLog().iter_all() if event.type == "child.blocked"]
