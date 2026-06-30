from __future__ import annotations

from hermes_time import now

from agent_runtime.actions import HarnessActionType
from agent_runtime.blueprints import BlueprintStore, instantiate_blueprint
from agent_runtime.blueprints.routing import apply_stage_outcome, derive_stage_outcome
from agent_runtime.blueprints.schema import StageOutcome, validate_blueprint
from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.models import Task
from agent_runtime.state_machine import MissionStateMachine
from agent_runtime.states import StageStatus, TaskState


def test_neko_dev_qa_basic_validates_and_starts_with_lead_slot():
    bp = BlueprintStore().get("neko_dev_qa_basic")

    assert validate_blueprint(bp) == []
    assert [slot.id for slot in bp.slots] == ["lead", "builder", "verifier"]

    task = _neko_task()
    action = MissionStateMachine().next_action(task)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "lead"


def test_scope_completion_emits_ready_and_routes_to_builder():
    task = _neko_task()
    scope = task.mission_plan.stages[0]
    decision = AgentDecision(
        type=DecisionType.COMPLETE,
        summary="Scope is ready",
        rationale="acceptance and proof lane are clear",
        payload={},
    )

    assert derive_stage_outcome(decision, scope) == StageOutcome.READY
    assert apply_stage_outcome(task, "scope", StageOutcome.READY, reason="scoped") == "implement"
    assert task.mission_plan.current_stage_id == "implement"
    assert scope.status == StageStatus.PASSED
    assert MissionStateMachine().next_action(task).slot_id == "builder"


def test_blocked_implementation_routes_back_to_lead_slot():
    task = _neko_task()
    apply_stage_outcome(task, "scope", StageOutcome.READY, reason="scoped")

    assert apply_stage_outcome(task, "implement", StageOutcome.BLOCKED, reason="blocked") == "scope"
    assert task.mission_plan.current_stage_id == "scope"
    assert MissionStateMachine().next_action(task).slot_id == "lead"


def test_failed_verification_routes_by_edge():
    task = _neko_task()
    apply_stage_outcome(task, "scope", StageOutcome.READY, reason="scoped")
    apply_stage_outcome(task, "implement", StageOutcome.PASSED, reason="implemented")

    assert apply_stage_outcome(task, "verify", StageOutcome.FAILED, reason="verification failed") == "scope"
    assert task.mission_plan.current_stage_id == "scope"
    assert MissionStateMachine().next_action(task).slot_id == "lead"


def _neko_task() -> Task:
    bp = BlueprintStore().get("neko_dev_qa_basic")
    plan = instantiate_blueprint(
        bp,
        goal="neko flow smoke",
        bindings={"lead": "persona:neko_supervisor", "builder": "persona:dev", "verifier": "persona:qa"},
    )
    return Task(
        id="task_neko_dev_qa_basic",
        title="Neko Dev QA Basic",
        description="neko flow smoke",
        state=TaskState.CREATED,
        created_at=now(),
        updated_at=now(),
        requested_by="test",
        mission_plan=plan,
        current_stage_id=plan.current_stage_id,
    )
