import pytest

from hermes_time import now

from agent_runtime.decision_schema import DecisionPayloadInvalid
from agent_runtime.mission_plan import (
    attach_proofs_to_plan_stage,
    blocking_stages_ready_for_qa,
    ensure_mission_plan,
    mission_plan_summary,
    validate_mission_plan,
)
from agent_runtime.models import MissionIntent, MissionPlan, MissionPlanStage, Proof, Task
from agent_runtime.proof_rules import ProofType
from agent_runtime.states import StageStatus, TaskState


def make_task(**overrides):
    ts = now()
    values = {
        "id": "task_51",
        "title": "Fix Mission Control all role live terminals",
        "description": "Backend stream seed first, then Launcher UI repair, then QA visual verification.",
        "state": TaskState.CREATED,
        "created_at": ts,
        "updated_at": ts,
        "requested_by": "tony",
        "acceptance_criteria": ["Neko, Backend Dev, Launcher Dev, and QA streams are visible."],
    }
    values.update(overrides)
    return Task(**values)


def test_neko_backend_only_handoff_preserves_launcher_stage_for_parent_goal():
    task = make_task()

    plan = ensure_mission_plan(
        task,
        {
            "objective": "Run backend contract smoke.",
            "acceptance_criteria": ["Backend proof passes."],
            "affected_repos": ["EterniaBackend"],
            "handoff_packet": {
                "target_owner": "backend_dev",
                "target_repo": "EterniaBackend",
                "proof_gate": {"required": True, "proof_recipe_id": "backend_contract_smoke"},
            },
        },
        actor="neko_supervisor",
    )

    assert [stage.id for stage in plan.stages] == [
        "backend_contract_smoke",
        "launcher_implementation",
        "qa_release",
    ]
    assert plan.mission_intent.objective == task.description
    assert plan.stages[0].kind == "proof_only"
    assert plan.stages[1].owner == "dev"
    assert plan.stages[1].depends_on == ["backend_contract_smoke"]
    assert plan.stages[2].depends_on == ["backend_contract_smoke", "launcher_implementation"]
    assert task.current_stage_id == "backend_contract_smoke"
    launcher_stage = next(stage for stage in task.stages if stage.id == "launcher_implementation")
    assert launcher_stage.affected_paths == ["lib/features/mission_control/", "test/features/mission_control/"]
    assert launcher_stage.test_plan == [
        "flutter analyze lib/features/mission_control test/features/mission_control",
        "flutter test test/features/mission_control",
    ]


def test_backend_no_product_edit_investigation_routes_directly_to_qa():
    task = make_task(
        title="Investigate NSFW filter leakage hardening plan",
        description=(
            "Produce an investigation plan for why porn is slipping through NSFW filters. "
            "Inspect backend moderation paths, tag/rating ingestion, thresholds, queueing, "
            "failed-job handling, and UI/backend contract gaps. This is not a product "
            "implementation goal. Do not edit product code."
        ),
        affected_repos=[],
    )

    plan = ensure_mission_plan(
        task,
        {
            "objective": "Produce a no-product-edit backend investigation report and staged hardening plan.",
            "acceptance_criteria": ["Investigation report lists files inspected and implementation stages."],
            "affected_repos": ["EterniaBackend"],
            "handoff_packet": {
                "target_owner": "backend_dev",
                "target_repo": "EterniaBackend",
                "proof_gate": {"required": True, "proof_recipe_id": "backend_contract_smoke"},
            },
        },
        actor="neko_supervisor",
    )

    assert [stage.id for stage in plan.stages] == ["backend_investigation", "qa_release"]
    assert plan.stages[0].owner == "backend_dev"
    assert plan.stages[0].repo == "EterniaBackend"
    assert plan.stages[0].kind == "context"
    assert plan.stages[0].requires_product_edit is False
    assert plan.stages[1].owner == "qa"
    assert plan.stages[1].repo == "EterniaBackend"
    assert plan.stages[1].depends_on == ["backend_investigation"]


def test_backend_product_hardening_admin_ui_nongoal_does_not_synthesize_launcher_stage():
    task = make_task(
        title="NSFW hardening slice: fail-closed media safety verdicts",
        description=(
            "Implement a narrow backend hardening slice in EterniaBackend. "
            "Add media safety verdict primitives, fail closed for public media exposure, "
            "harden dedupe and thumbnail inheritance, and add focused backend tests. "
            "Do not implement admin UI in this goal."
        ),
        affected_repos=[],
        acceptance_criteria=[],
    )

    plan = ensure_mission_plan(
        task,
        {
            "objective": (
                "In EterniaBackend, implement a focused fail-closed media safety verdict slice "
                "and prove unscanned or blocked media cannot leak through public surfaces."
            ),
            "acceptance_criteria": ["Backend tests prove fail-closed media behavior."],
            "affected_repos": ["EterniaBackend"],
            "handoff_packet": {
                "target_owner": "backend_dev",
                "target_repo": "EterniaBackend",
            },
        },
        actor="neko_supervisor",
    )

    assert [stage.repo for stage in plan.stages] == ["EterniaBackend", "EterniaBackend"]
    assert [stage.owner for stage in plan.stages] == ["backend_dev", "qa"]
    assert plan.stages[0].id == "backend_implementation"
    assert plan.stages[0].kind == "implementation"
    assert plan.stages[0].proof_recipe_id is None
    assert plan.stages[0].requires_product_edit is True
    assert not any(stage.id == "launcher_implementation" for stage in plan.stages)
    assert plan.stages[1].depends_on == [plan.stages[0].id]


def test_backend_only_no_launcher_frontend_goal_strips_synthesized_launcher_stage():
    task = make_task(
        title="NSFW hardening slice retry: fail-closed media safety verdicts",
        description=(
            "Implement the first concrete EterniaBackend hardening slice. "
            "Patch only EterniaBackend. Non-goals: no Launcher/frontend changes, "
            "no admin UI, no broad federated/video claim."
        ),
        affected_repos=[],
        acceptance_criteria=[],
    )

    plan = ensure_mission_plan(
        task,
        {
            "objective": "Patch only EterniaBackend media safety verdict primitives and fail-closed public exposure behavior.",
            "acceptance_criteria": ["Backend tests prove unscanned or blocked media cannot leak."],
            "affected_repos": ["EterniaBackend"],
            "handoff_packet": {
                "target_owner": "backend_dev",
                "target_repo": "EterniaBackend",
            },
        },
        actor="neko_supervisor",
    )

    assert [stage.repo for stage in plan.stages] == ["EterniaBackend", "EterniaBackend"]
    assert [stage.owner for stage in plan.stages] == ["backend_dev", "qa"]
    assert task.requires_visual_proof is False
    assert not any(stage.repo == "EterniaLauncher" for stage in plan.stages)


def test_neko_persona_diagnostic_self_observation_does_not_synthesize_qa_stage():
    task = make_task(
        id="task_neko_diag",
        title="Stage 57/58 Neko-only contract burn",
        description="Run one Neko diagnostic turn and stop.",
        affected_repos=["hermes-agent"],
        risk_flags=["persona_operation", "diagnostic_persona:neko_supervisor"],
    )

    plan = ensure_mission_plan(
        task,
        {
            "objective": "Validate one Neko diagnostic response without Dev or QA.",
            "acceptance_criteria": ["Exactly one Neko turn runs."],
            "affected_repos": ["hermes-agent"],
            "handoff_packet": {
                "packet_kind": "fresh_scope",
                "mission_phase": "neko_only_contract_diagnostic",
                "handoff_mode": "single_specialist",
                "target_owner": "neko_supervisor",
                "target_repo": "hermes-agent",
                "proof_gate": {
                    "required": False,
                    "required_proof_types": ["harness_observation"],
                    "minimum_status": "passed",
                    "visual_required": False,
                },
                "join_gate": {
                    "release_condition": "Harness observes one valid Neko diagnostic decision and stops.",
                },
            },
        },
        actor="neko_supervisor",
    )

    assert [stage.id for stage in plan.stages] == ["neko_diagnostic"]
    assert plan.current_stage_id == "neko_diagnostic"
    assert plan.stages[0].owner == "neko_supervisor"
    assert plan.stages[0].kind == "planning"
    assert plan.stages[0].blocks_qa_until is False


def test_mission_control_launcher_plan_projects_focused_worker_hud_defaults():
    task = make_task(
        title="Mission Control terminal thinking visibility",
        description=(
            "Upgrade Mission Control Live Agent Terminal thinking visibility so Neko, Launcher Dev, "
            "Backend Dev if involved, and QA each show a visible safe thinking summary row."
        ),
        affected_repos=["EterniaLauncher"],
    )

    plan = ensure_mission_plan(
        task,
        {
            "objective": "Implement the narrow Mission Control terminal thinking visibility UI fix.",
            "acceptance_criteria": ["Mission Control displays redaction-safe thinking summary rows."],
            "affected_repos": ["EterniaLauncher"],
            "handoff_packet": {"target_owner": "dev", "target_repo": "EterniaLauncher"},
        },
        actor="neko_supervisor",
    )

    assert plan.current_stage_id == "launcher_implementation"
    stage = task.stages[0]
    assert stage.id == "launcher_implementation"
    assert stage.affected_paths == ["lib/features/mission_control/", "test/features/mission_control/"]
    assert stage.test_plan == [
        "flutter analyze lib/features/mission_control test/features/mission_control",
        "flutter test test/features/mission_control",
    ]


def test_launcher_post_media_plan_projects_focused_worker_hud_defaults():
    task = make_task(
        title="Launcher post media thumbnails and portrait videos 3x",
        description=(
            "Make each post thumbnail also 3x bigger like the post images. "
            "Make portrait videos 3x bigger too while preserving aspect ratio."
        ),
        affected_repos=["EterniaLauncher"],
        requires_visual_proof=True,
    )

    plan = ensure_mission_plan(
        task,
        {
            "objective": "Implement the scoped EterniaLauncher feed/post media presentation change.",
            "acceptance_criteria": ["Post thumbnails and portrait videos render 3x larger without overlap."],
            "affected_repos": ["EterniaLauncher"],
            "handoff_packet": {"target_owner": "dev", "target_repo": "EterniaLauncher"},
            "requires_visual_proof": True,
        },
        actor="neko_supervisor",
    )

    assert plan.current_stage_id == "launcher_implementation"
    stage = task.stages[0]
    assert stage.id == "launcher_implementation"
    assert stage.requires_visual_proof is True
    assert stage.affected_paths == ["lib/features/posts/", "test/features/posts/"]
    assert stage.test_plan == [
        "flutter analyze lib/features/posts test/features/posts",
        "flutter test test/features/posts",
    ]


def test_launcher_post_media_no_product_edit_certification_is_proof_only():
    task = make_task(
        title="Certify committed Launcher post media 3x sizing",
        description=(
            "No-product-edit certification of the committed Launcher post media sizing change. "
            "Verify that post thumbnails and portrait videos use the larger 3x inline media sizing "
            "through focused Launcher post media tests and analyze. Do not edit product code."
        ),
        affected_repos=["EterniaLauncher"],
        non_goals=["Do not change product code or backend APIs during this certification goal."],
    )

    plan = ensure_mission_plan(
        task,
        {
            "objective": "Certify the committed Launcher post media 3x inline sizing change without editing product code by collecting focused command proof.",
            "acceptance_criteria": ["Focused post media tests and analyze pass."],
            "affected_repos": ["EterniaLauncher"],
            "handoff_packet": {"target_owner": "dev", "target_repo": "EterniaLauncher"},
        },
        actor="neko_supervisor",
    )

    stage = plan.stages[0]
    assert stage.id == "launcher_implementation"
    assert stage.kind == "proof_only"
    assert stage.requires_product_edit is False
    assert task.stages[0].test_plan == [
        "flutter analyze lib/features/posts test/features/posts",
        "flutter test test/features/posts",
    ]


def test_planner_rejects_unknown_owner_repo_kind_and_cycles():
    plan = MissionPlan(
        mission_intent=MissionIntent(title="x", objective="x"),
        current_stage_id="a",
        stages=[
            MissionPlanStage(id="a", title="A", objective="A", owner="wizard", repo="Mars", kind="magic", depends_on=["b"]),
            MissionPlanStage(id="b", title="B", objective="B", owner="dev", repo="EterniaLauncher", kind="implementation", depends_on=["a"]),
        ],
    )

    errors = validate_mission_plan(plan)

    assert not any("owner" in error for error in errors)
    assert any("repo" in error for error in errors)
    assert any("kind" in error for error in errors)
    assert any("dependency cycle" in error for error in errors)


def test_mission_plan_payload_drops_unknown_stage_keys():
    task = make_task()
    payload = {
        "mission_plan": {
            "mission_intent": {"title": "x", "objective": "x"},
            "unknown_plan_key": True,
            "stages": [{"id": "s", "title": "S", "objective": "S", "owner": "dev", "repo": "EterniaLauncher", "kind": "implementation", "made_up": True}],
        }
    }

    plan = ensure_mission_plan(
        task,
        payload,
        actor="neko_supervisor",
    )

    assert plan.stages[0].id == "s"
    assert payload["mission_plan"]["_normalization"]["dropped_fields"] == ["mission_plan.unknown_plan_key"]
    assert payload["mission_plan"]["stages"][0]["_normalization"]["dropped_fields"] == ["mission_plan.stages[0].made_up"]


def test_blocking_stage_gate_reports_typed_stage_ids():
    task = make_task(
        mission_plan=MissionPlan(
            mission_intent=MissionIntent(title="Fix", objective="Fix"),
            current_stage_id="launcher",
            stages=[
                MissionPlanStage(
                    id="launcher",
                    title="Launcher",
                    objective="Patch Launcher",
                    owner="dev",
                    repo="EterniaLauncher",
                    kind="implementation",
                    status=StageStatus.IMPLEMENTING,
                    requires_product_edit=True,
                    blocks_qa_until=True,
                ),
                MissionPlanStage(
                    id="qa_release",
                    title="QA",
                    objective="QA",
                    owner="qa",
                    repo="EterniaLauncher",
                    kind="qa_verdict",
                    depends_on=["launcher"],
                    blocks_qa_until=False,
                ),
            ],
        )
    )

    ready, missing = blocking_stages_ready_for_qa(task)

    assert ready is False
    assert missing == ["typed stage launcher is implementing, not ready_for_qa"]


def test_implementation_stage_recovers_to_ready_after_later_passed_command_proof():
    task = make_task(
        mission_plan=MissionPlan(
            mission_intent=MissionIntent(title="Fix", objective="Fix"),
            current_stage_id="launcher",
            stages=[
                MissionPlanStage(
                    id="launcher",
                    title="Launcher",
                    objective="Patch Launcher",
                    owner="dev",
                    repo="EterniaLauncher",
                    kind="implementation",
                    status=StageStatus.IMPLEMENTING,
                    requires_product_edit=True,
                    blocks_qa_until=True,
                ),
                MissionPlanStage(
                    id="qa_release",
                    title="QA",
                    objective="QA",
                    owner="qa",
                    repo="EterniaLauncher",
                    kind="qa_verdict",
                    depends_on=["launcher"],
                    blocks_qa_until=False,
                ),
            ],
        )
    )
    failed = Proof(
        id="proof_failed",
        task_id=task.id,
        stage_id="launcher",
        type=ProofType.TEST_RUN,
        title="failed",
        path_or_value="failed",
        created_by="dev",
        created_at=now(),
        metadata={"status": "failed", "exit_code": 1},
        redaction_status="safe",
    )
    passed = Proof(
        id="proof_passed",
        task_id=task.id,
        stage_id="launcher",
        type=ProofType.TEST_RUN,
        title="passed",
        path_or_value="passed",
        created_by="dev",
        created_at=now(),
        metadata={"status": "passed", "exit_code": 0},
        redaction_status="safe",
    )

    class Proofs:
        def get(self, proof_id):
            return {"proof_failed": failed, "proof_passed": passed}[proof_id]

    attach_proofs_to_plan_stage(task, "launcher", ["proof_failed"], proof_store=Proofs())
    assert task.mission_plan.stages[0].status == StageStatus.NEEDS_FIXES

    attach_proofs_to_plan_stage(task, "launcher", ["proof_passed"], proof_store=Proofs())

    assert task.mission_plan.stages[0].status == StageStatus.READY_FOR_QA
    ready, missing = blocking_stages_ready_for_qa(task, proof_store=Proofs())
    assert ready is True
    assert missing == []


def test_mission_plan_summary_is_redaction_safe_shape():
    task = make_task()
    ensure_mission_plan(task, {"objective": "Patch Launcher", "acceptance_criteria": ["proof"], "affected_repos": ["EterniaLauncher"]})

    summary = mission_plan_summary(task)

    assert summary["enabled"] is True
    assert summary["current_stage_id"] == "launcher_implementation"
    assert summary["stages"][0]["owner"] == "dev"
