from hermes_time import now

from agent_runtime.actions import HarnessActionType
from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.models import Task
from agent_runtime.planning import apply_planning_decision
from agent_runtime.reconciler import reconcile_task
from agent_runtime.state_machine import MissionStateMachine
from agent_runtime.states import TaskState


def _task(state=TaskState.RUNNING):
    ts = now()
    return Task(
        id="task_reconcile",
        title="Reconcile",
        description="d",
        state=state,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        acceptance_criteria=["done"],
    )


def _qa_approval(stage_ids):
    return AgentDecision(
        type=DecisionType.REPORT_QA_VERDICT,
        summary="qa approved",
        rationale="QA approved the named stages.",
        payload={
            "review_scope": "plan",
            "reviewed_stage_ids": stage_ids,
            "verdict": "approved",
            "proof_requirements_confirmed": True,
            "test_plan_confirmed": True,
        },
    )


def test_reconciler_flags_legacy_APPROVED_missing_stage_records():
    task = _task(TaskState.RUNNING)
    apply_planning_decision(task, _qa_approval(["stage_a"]), actor="qa")
    task.stages = []  # Legacy/runtime residue predating stage reconciliation.

    result = reconcile_task(task)

    assert result.needs_supervisor is True
    assert result.findings[0].kind == "qa_approved_missing_stage_records"
    assert "missing_stage:stage_a" in result.findings[0].evidence


def test_reconciler_routes_repeated_unsupported_context_requests_to_neko():
    task = _task(TaskState.RUNNING)
    task.context_requests = [
        {"id": "ctx_1", "actor": "qa", "status": "unsupported", "failure_reason": "path_not_found"},
        {"id": "ctx_2", "actor": "qa", "status": "unsupported", "failure_reason": "path_not_found"},
        {"id": "ctx_3", "actor": "qa", "status": "unsupported", "failure_reason": "path_not_found"},
    ]

    result = reconcile_task(task)
    action = MissionStateMachine().next_action(task)

    assert result.needs_supervisor is True
    assert result.findings[0].kind == "repeated_unsupported_context_requests"
    assert action.type == HarnessActionType.RUN_SLOT
    assert "transition reconciliation" in action.reason


def test_reconciler_does_not_route_clean_dev_ready_task_to_neko():
    task = _task(TaskState.RUNNING)

    result = reconcile_task(task)
    action = MissionStateMachine().next_action(task)

    assert result.findings == []
    assert action.type == HarnessActionType.RUN_SLOT
