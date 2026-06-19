from hermes_time import now

from agent_runtime.decision_schema import AgentDecision, DecisionPayloadInvalid, DecisionType
from agent_runtime.models import MissionIntent, MissionPlan, MissionPlanStage, Proof, Task
from agent_runtime.planning import _validate_commit_deploy_gate
from agent_runtime.proof_rules import ProofType
from agent_runtime.states import TaskState
from agent_runtime.store import ProofStore, TaskStore

import pytest


def make_edit_task(task_id="task_gate", requires_product_edit=True):
    ts = now()
    task = Task(
        id=task_id,
        title="Edit slice",
        description="d",
        state=TaskState.DEV_IMPLEMENTING,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
    )
    task.mission_plan = MissionPlan(
        enabled=True,
        mission_intent=MissionIntent(title="t", objective="o", source_task_id=task_id),
        current_stage_id="backend_implementation",
        stages=[
            MissionPlanStage(
                id="backend_implementation",
                title="Backend",
                objective="o",
                owner="backend_dev",
                repo="EterniaBackend",
                kind="implementation",
                requires_product_edit=requires_product_edit,
            )
        ],
    )
    TaskStore().create(task)
    return task


def attach_proof(task_id, proof_id, command, status="passed"):
    ts = now()
    proof = Proof(
        id=proof_id,
        task_id=task_id,
        stage_id="backend_implementation",
        type=ProofType.TEST_RUN,
        title="t",
        path_or_value="proof.txt",
        created_by="harness",
        created_at=ts,
        metadata={"status": status, "command": command},
    )
    ProofStore().attach(proof)
    return proof


def decision(payload):
    return AgentDecision(type=DecisionType.REQUEST_QA_REVIEW, summary="s", rationale="r", payload=payload)


def test_gate_rejects_product_edit_handoff_without_commit_refs(isolate_agent_runtime_root):
    task = make_edit_task("task_gate1")
    attach_proof(task.id, "proof_dc1", "manage.py check")

    with pytest.raises(DecisionPayloadInvalid, match="commit_refs"):
        _validate_commit_deploy_gate(
            task,
            decision({"delivery": {"work_status": "ready_for_qa", "proof_ids": ["proof_dc1"]}}),
            proof_store=ProofStore(),
            stage_id="backend_implementation",
        )


def test_gate_rejects_missing_deploy_check_proof(isolate_agent_runtime_root):
    task = make_edit_task("task_gate2")
    attach_proof(task.id, "proof_tests_only", "scripts/test.sh media.tests")

    with pytest.raises(DecisionPayloadInvalid, match="deploy-check"):
        _validate_commit_deploy_gate(
            task,
            decision({
                "proof_ids": ["proof_tests_only"],
                "delivery": {"work_status": "ready_for_qa", "commit_refs": ["EterniaBackend@main:abc1234"]},
            }),
            proof_store=ProofStore(),
            stage_id="backend_implementation",
        )


def test_gate_passes_with_commit_and_deploy_proof(isolate_agent_runtime_root):
    task = make_edit_task("task_gate3")
    attach_proof(task.id, "proof_deploy", "source venv && manage.py check")

    _validate_commit_deploy_gate(
        task,
        decision({
            "delivery": {
                "work_status": "ready_for_qa",
                "commit_refs": ["EterniaBackend@main:abc1234"],
                "deploy_verification": {"status": "passed", "method": "manage.py check", "proof_id": "proof_deploy"},
            }
        }),
        proof_store=ProofStore(),
        stage_id="backend_implementation",
    )


def test_gate_skips_no_edit_stage(isolate_agent_runtime_root):
    task = make_edit_task("task_gate4", requires_product_edit=False)

    _validate_commit_deploy_gate(
        task,
        decision({"delivery": {"work_status": "ready_for_qa"}}),
        proof_store=ProofStore(),
        stage_id="backend_implementation",
    )


def test_gate_rejects_failed_deploy_proof(isolate_agent_runtime_root):
    task = make_edit_task("task_gate5")
    attach_proof(task.id, "proof_deploy_failed", "manage.py check", status="failed")

    with pytest.raises(DecisionPayloadInvalid, match="deploy-check"):
        _validate_commit_deploy_gate(
            task,
            decision({
                "delivery": {
                    "work_status": "ready_for_qa",
                    "commit_refs": ["EterniaBackend@main:abc1234"],
                    "deploy_verification": {"proof_id": "proof_deploy_failed"},
                }
            }),
            proof_store=ProofStore(),
            stage_id="backend_implementation",
        )
