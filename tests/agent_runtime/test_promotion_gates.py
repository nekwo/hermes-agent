from datetime import timedelta

from hermes_time import now

import pytest

from agent_runtime.decision_schema import DecisionPayloadInvalid
from agent_runtime.models import MissionIntent, MissionPlan, MissionPlanStage, Proof, Task, TaskStage
from agent_runtime.promotion_gates import product_promotion_required, satisfied_promotion_lanes, validate_product_promotion_gate
from agent_runtime.proof_rules import ProofType
from agent_runtime.states import StageStatus, TaskState
from agent_runtime.store import ProofStore, TaskStore


def make_product_task(task_id="task_promo"):
    ts = now()
    task = Task(
        id=task_id,
        title="Product feature",
        description="Change backend feature.",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        affected_repos=["EterniaBackend"],
        proof_ids=[],
    )
    task.mission_plan = MissionPlan(
        enabled=True,
        mission_intent=MissionIntent(title="Product feature", objective="Change backend feature.", source_task_id=task_id),
        current_stage_id="backend_implementation",
        stages=[
            MissionPlanStage(
                id="backend_implementation",
                title="Backend Implementation",
                objective="Implement product feature.",
                owner="backend_dev",
                repo="EterniaBackend",
                kind="implementation",
                status=StageStatus.READY_FOR_QA,
                requires_product_edit=True,
            )
        ],
    )
    TaskStore().create(task)
    return task


def make_launcher_scratch_task(task_id="task_launcher_scratch"):
    ts = now()
    task = Task(
        id=task_id,
        title="Launcher scratch proof",
        description="Create docs/scratch/e2e_trust_probe.md in EterniaLauncher.",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        affected_repos=["EterniaLauncher"],
        proof_ids=[],
    )
    task.mission_plan = MissionPlan(
        enabled=True,
        mission_intent=MissionIntent(title="Launcher scratch proof", objective=task.description, source_task_id=task_id),
        current_stage_id="implement",
        stages=[
            MissionPlanStage(
                id="implement",
                title="Launcher Implementation",
                objective="Create the scratch proof file.",
                owner="dev",
                repo="EterniaLauncher",
                kind="implementation",
                status=StageStatus.READY_FOR_QA,
                requires_product_edit=True,
                affected_paths=["docs/scratch/e2e_trust_probe.md"],
            )
        ],
    )
    TaskStore().create(task)
    return task


def attach(task, proof_id, command, *, intent="", status="passed", created_at=None):
    proof = Proof(
        id=proof_id,
        task_id=task.id,
        stage_id="backend_implementation",
        type=ProofType.TEST_RUN,
        title=f"Command proof: {command}",
        path_or_value=f"proofs/{task.id}/artifacts/{proof_id}.log",
        created_by="harness",
        created_at=created_at or now(),
        metadata={"status": status, "command": command, "proof_intent": intent},
        redaction_status="safe",
    )
    ProofStore().attach(proof)
    task.proof_ids.append(proof.id)
    return proof


def test_docs_scratch_launcher_probe_does_not_require_product_promotion(isolate_agent_runtime_root):
    task = make_launcher_scratch_task()
    proof = attach(task, "proof_exact_echo", "echo e2e-trust-probe", intent="auto_final_gate_after_delivery")

    assert product_promotion_required(task) is False
    validate_product_promotion_gate(task, [proof.id], proof_store=ProofStore())


def test_explicit_no_edit_plan_stage_overrides_implementation_shaped_compat_projection(isolate_agent_runtime_root):
    task = make_product_task("task_explicit_no_edit")
    task.affected_repos = ["EterniaLauncher"]
    task.description = "No product edits; capture Stage-C Mission Control proof."
    task.mission_plan.stages = [
        MissionPlanStage(
            id="implement_ui",
            title="Implement UI",
            objective="Capture Stage-C visual proof without product edits.",
            owner="qa",
            repo="EterniaLauncher",
            kind="implementation",
            status=StageStatus.PASSED,
            requires_product_edit=False,
        )
    ]
    task.stages = [
        TaskStage(
            id="implement_ui",
            title="Implement UI",
            objective="Implement the UI change and attach visual proof.",
            status=StageStatus.PASSED,
        )
    ]

    assert product_promotion_required(task) is False


def test_product_promotion_gate_rejects_local_only(isolate_agent_runtime_root):
    task = make_product_task()
    attach(task, "proof_local", ".EterniaBackendVirtualEnv/Scripts/python.exe manage.py check", intent="auto_final_gate_after_delivery")

    with pytest.raises(DecisionPayloadInvalid, match="local Docker/PostgreSQL integration tests"):
        validate_product_promotion_gate(task, ["proof_local"], proof_store=ProofStore())


def test_backend_promotion_gate_rejects_sqlite_or_mocked_local_integration(isolate_agent_runtime_root):
    task = make_product_task()
    attach(task, "proof_local", ".EterniaBackendVirtualEnv/Scripts/python.exe manage.py check", intent="auto_final_gate_after_delivery")
    attach(task, "proof_sqlite", "scripts/test.sh --sqlite # mocked-only local escape hatch", intent="local")
    attach(task, "proof_staging", "kubectl -n staging rollout status deploy/eternia-backend && kubectl -n staging exec deploy/eternia-backend -- smoke-test")
    attach(task, "proof_prod", "kubectl -n prod rollout status deploy/eternia-backend", intent="prod_rollout")

    assert "local_docker_postgres" not in satisfied_promotion_lanes(task, [ProofStore().get(pid) for pid in task.proof_ids])
    with pytest.raises(DecisionPayloadInvalid, match="local Docker/PostgreSQL integration tests"):
        validate_product_promotion_gate(task, task.proof_ids, proof_store=ProofStore())


def test_backend_promotion_gate_rejects_mislabeled_sqlite_docker_intent(isolate_agent_runtime_root):
    task = make_product_task()
    attach(task, "proof_local", ".EterniaBackendVirtualEnv/Scripts/python.exe manage.py check", intent="auto_final_gate_after_delivery")
    attach(task, "proof_sqlite", "scripts/test.sh --sqlite # mocked-only local escape hatch", intent="local_docker_postgres")
    attach(task, "proof_staging", "kubectl -n staging rollout status deploy/eternia-backend && kubectl -n staging exec deploy/eternia-backend -- smoke-test")
    attach(task, "proof_prod", "kubectl -n prod rollout status deploy/eternia-backend", intent="prod_rollout")

    with pytest.raises(DecisionPayloadInvalid, match="local Docker/PostgreSQL integration tests"):
        validate_product_promotion_gate(task, task.proof_ids, proof_store=ProofStore())


def test_product_promotion_gate_accepts_local_docker_staging_and_prod(isolate_agent_runtime_root):
    task = make_product_task()
    local = attach(task, "proof_local", ".EterniaBackendVirtualEnv/Scripts/python.exe manage.py check", intent="auto_final_gate_after_delivery")
    docker = attach(task, "proof_docker", "python -m pytest tests/docker/ -v --tb=short # local docker compose PostgreSQL", intent="local_docker_postgres")
    staging = attach(task, "proof_staging", "kubectl -n staging rollout status deploy/eternia-backend && kubectl -n staging exec deploy/eternia-backend -- smoke-test")
    prod = attach(task, "proof_prod", "kubectl -n prod rollout status deploy/eternia-backend", intent="prod_rollout")

    assert satisfied_promotion_lanes(task, [local, docker, staging, prod]) == {"local", "local_docker_postgres", "staging_k8", "prod_rollout"}
    validate_product_promotion_gate(task, [local.id, docker.id, staging.id, prod.id], proof_store=ProofStore())


def test_product_promotion_gate_accepts_synced_push_triggered_prod_deploy(isolate_agent_runtime_root):
    task = make_product_task()
    local = attach(task, "proof_local", ".EterniaBackendVirtualEnv/Scripts/python.exe manage.py check", intent="auto_final_gate_after_delivery")
    docker = attach(task, "proof_docker", "docker compose up -d postgres && python -m pytest tests/docker/ -v --tb=short", intent="local_docker_postgres")
    staging = attach(task, "proof_staging", "kubectl -n staging rollout status deploy/eternia-backend && kubectl -n staging exec deploy/eternia-backend -- smoke-test")
    prod = attach(
        task,
        "proof_prod_push",
        "git fetch origin && git rebase origin/main && git push origin main # production deployment trigger",
        intent="prod_rollout",
    )

    assert satisfied_promotion_lanes(task, [local, docker, staging, prod]) == {"local", "local_docker_postgres", "staging_k8", "prod_rollout"}
    validate_product_promotion_gate(task, [local.id, docker.id, staging.id, prod.id], proof_store=ProofStore())


def test_product_promotion_gate_rejects_unsynced_push_triggered_prod_deploy(isolate_agent_runtime_root):
    task = make_product_task()
    attach(task, "proof_local", ".EterniaBackendVirtualEnv/Scripts/python.exe manage.py check", intent="auto_final_gate_after_delivery")
    attach(task, "proof_docker", "python -m pytest tests/docker/ -v --tb=short # PostgreSQL via docker compose", intent="local_docker_postgres")
    attach(task, "proof_staging", "kubectl -n staging rollout status deploy/eternia-backend && kubectl -n staging exec deploy/eternia-backend -- smoke-test")
    attach(task, "proof_prod_push", "git push origin main # production deployment trigger", intent="prod_rollout")

    with pytest.raises(DecisionPayloadInvalid, match="production pod rollout"):
        validate_product_promotion_gate(task, task.proof_ids, proof_store=ProofStore())


def test_product_promotion_gate_rejects_prod_before_staging(isolate_agent_runtime_root):
    task = make_product_task()
    ts = now()
    attach(
        task,
        "proof_local",
        ".EterniaBackendVirtualEnv/Scripts/python.exe manage.py check",
        intent="auto_final_gate_after_delivery",
        created_at=ts,
    )
    attach(
        task,
        "proof_docker",
        "python -m pytest tests/docker/ -v --tb=short # local docker compose PostgreSQL",
        intent="local_docker_postgres",
        created_at=ts + timedelta(seconds=30),
    )
    attach(
        task,
        "proof_prod",
        "kubectl -n prod rollout status deploy/eternia-backend",
        intent="prod_rollout",
        created_at=ts + timedelta(minutes=1),
    )
    attach(
        task,
        "proof_staging",
        "kubectl -n staging rollout status deploy/eternia-backend",
        created_at=ts + timedelta(minutes=2),
    )

    with pytest.raises(DecisionPayloadInvalid, match="out of order"):
        validate_product_promotion_gate(task, task.proof_ids, proof_store=ProofStore())
