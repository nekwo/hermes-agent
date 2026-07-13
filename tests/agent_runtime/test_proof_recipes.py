from hermes_time import now

from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.models import MissionIntent, MissionPlan, MissionPlanStage, Task
from agent_runtime.proof_recipes import normalize_request_test_run_decision
from agent_runtime.states import StageStatus, TaskState


def test_normalize_request_test_run_accepts_typed_proof_only_legacy_implement_stage():
    ts = now()
    task = Task(
        id="task_recipe_legacy_implement",
        title="Cross-stack no-edit proof",
        description="No product edits. Certify backend-first then Launcher no-edit proof routing.",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="test",
        affected_repos=["EterniaBackend", "EterniaLauncher"],
        risk_flags=["no_product_edits"],
        current_stage_id="implement",
    )
    task.mission_plan = MissionPlan(
        enabled=True,
        mission_intent=MissionIntent(
            title=task.title,
            objective=task.description,
            acceptance_criteria=["Backend and Launcher no-product-edit proofs pass."],
        ),
        current_stage_id="implement",
        stages=[
            MissionPlanStage(
                id="implement",
                title="Launcher No-Edit Contract Smoke",
                objective="Collect the Harness-owned launcher_contract_smoke proof after backend proof is attached, without editing Launcher product files.",
                owner="dev",
                repo="EterniaLauncher",
                kind="proof_only",
                status=StageStatus.IMPLEMENTING,
                proof_recipe_id="launcher_contract_smoke",
                requires_product_edit=False,
                test_plan=["proof_recipe:launcher_contract_smoke"],
            )
        ],
    )
    decision = AgentDecision(
        type=DecisionType.REQUEST_TEST_RUN,
        summary="Run launcher contract smoke.",
        rationale="Stage is typed as no-product-edit proof_only.",
        payload={"stage_id": "implement", "recipe_id": "launcher_contract_smoke"},
    )

    recipe = normalize_request_test_run_decision(task, decision)

    assert recipe is not None
    assert recipe.id == "launcher_contract_smoke"
    assert decision.payload["commands"] == list(recipe.commands)
    assert decision.payload["repo_scope"] == "EterniaLauncher"
