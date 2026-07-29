import importlib.util

import pytest

from hermes_time import now

from agent_runtime.decision_schema import DecisionPayloadInvalid
from agent_runtime.default_plan import build_default_mission_plan, ensure_default_mission_plan
from agent_runtime.mission_plan import (
    attach_proofs_to_plan_stage,
    blocking_stages_ready_for_qa,
    ensure_mission_plan,
    is_mission_lead_actor,
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


def assert_default_blueprint_plan(plan, task):
    assert plan.blueprint_id == "neko_two_dev_default"
    # Graph-driven fork: scope, then two parallel dev branches. No Neko join stage —
    # the harness waits for both branches (task done only when all stages pass).
    assert [stage.id for stage in plan.stages] == ["scope", "backend_implementation", "implement"]
    by_id = {stage.id: stage for stage in plan.stages}
    assert by_id["backend_implementation"].depends_on == ["scope"]
    assert by_id["implement"].depends_on == ["scope"]
    assert plan.current_stage_id == "scope"
    assert task.current_stage_id == "scope"
    assert plan.bindings["neko_supervisor"] == "neko_supervisor"
    assert plan.bindings["backend_dev"] == "backend_dev"
    assert plan.bindings["dev"] == "dev"


def test_running_default_plan_dispatches_from_graph_root_without_prepassing():
    # De-hardwired: a RUNNING default goal is NOT pre-advanced by stage id. Dispatch
    # starts at the graph root (scope); nothing is auto-passed, and the parallel dev
    # branches are released by the dispatcher when scope passes.
    task = make_task(state=TaskState.RUNNING)

    plan = ensure_default_mission_plan(task)

    stages = {stage.id: stage for stage in plan.stages}
    assert plan.current_stage_id == "scope"
    assert stages["scope"].status != StageStatus.PASSED
    assert stages["backend_implementation"].status != StageStatus.PASSED
    assert stages["implement"].status != StageStatus.PASSED


def test_default_plan_skips_backend_branch_for_launcher_only_scope():
    task = make_task(
        title="Launcher-only trust probe",
        description="Write the Launcher-side proof note only.",
        affected_repos=["EterniaLauncher"],
    )

    plan = ensure_default_mission_plan(task)

    stages = {stage.id: stage for stage in plan.stages}
    assert stages["backend_implementation"].status == StageStatus.PASSED
    assert stages["backend_implementation"].requires_product_edit is False
    assert stages["backend_implementation"].proof_gate == {"required": False}
    assert "out of scope" in " ".join(stages["backend_implementation"].audit_notes)
    assert stages["implement"].status != StageStatus.PASSED


def test_launcher_only_default_scope_pass_releases_launcher_without_resurrecting_backend():
    from agent_runtime.blueprints.routing import apply_stage_outcome
    from agent_runtime.blueprints.schema import StageOutcome

    task = make_task(
        title="Launcher-only trust probe",
        description="Write the Launcher-side proof note only.",
        affected_repos=["EterniaLauncher"],
    )
    plan = ensure_default_mission_plan(task)

    target = apply_stage_outcome(task, "scope", StageOutcome.READY, reason="scope accepted")

    stages = {stage.id: stage for stage in plan.stages}
    assert target == "implement"
    assert task.current_stage_id == "implement"
    assert stages["backend_implementation"].status == StageStatus.PASSED
    assert stages["implement"].status == StageStatus.IMPLEMENTING


def test_launcher_only_default_plan_does_not_create_backend_repo_bundle():
    from agent_runtime.repo_bundles import desired_bundles_for_task

    task = make_task(
        title="Launcher-only trust probe",
        description="Write the Launcher-side proof note only.",
        affected_repos=["EterniaLauncher"],
    )
    ensure_default_mission_plan(task)

    bundles = desired_bundles_for_task(task)

    assert [bundle.repo for bundle in bundles] == ["EterniaLauncher"]


def test_default_plan_adds_qa_stage_when_goal_requires_qa_approval():
    task = make_task(
        title="Launcher trust probe with QA",
        description="Write the Launcher-side proof note only. QA must approve the final evidence.",
        acceptance_criteria=["QA must approve the exact command proof before closeout."],
        affected_repos=["EterniaLauncher"],
    )

    plan = ensure_default_mission_plan(task)

    stages = {stage.id: stage for stage in plan.stages}
    assert [stage.id for stage in plan.stages] == ["scope", "backend_implementation", "implement", "qa_release"]
    assert stages["backend_implementation"].status == StageStatus.PASSED
    assert stages["implement"].blocks_qa_until is True
    assert stages["qa_release"].owner == "qa"
    assert stages["qa_release"].repo == "EterniaLauncher"
    assert stages["qa_release"].depends_on == ["backend_implementation", "implement"]
    assert stages["qa_release"].proof_gate == {
        "required": True,
        "minimum_status": "approved",
        "required_proof_types": ["qa_verdict"],
    }
    assert plan.bindings["qa"] == "qa"


def test_explicit_basic_blueprint_visual_no_edit_task_uses_screenshot_gate():
    task = make_task(
        title="Stage 46 visual proof certification",
        description="Capture a fullscreen Mission Control screenshot proof through the launcher_qa MCP provider without product edits.",
        acceptance_criteria=["A screenshot proof with type=screenshot is attached to the visual stage."],
        non_goals=["Do not invoke an operator-side PowerShell proof script."],
        requires_visual_proof=True,
    )

    plan = build_default_mission_plan(
        task,
        blueprint_id="neko_dev_qa_basic",
        bindings={"lead": "persona:neko_supervisor", "builder": "persona:dev", "verifier": "persona:qa"},
    )

    stages = {stage.id: stage for stage in plan.stages}
    implement = stages["implement"]
    assert implement.kind == "proof_only"
    assert implement.requires_visual_proof is True
    assert implement.test_plan == []
    assert implement.proof_gate["required_proof_types"] == ["screenshot"]
    assert implement.proof_gate["visual_required"] is True


def test_mission_lead_actor_resolves_blueprint_slot_binding():
    task = make_task()
    task.mission_plan = MissionPlan(
        enabled=True,
        current_stage_id="scope",
        slots={"lead": {"role": "lead", "required": True}},
        bindings={"lead": "persona:captain"},
        stages=[
            MissionPlanStage(
                id="scope",
                title="Scope",
                objective="Scope the mission.",
                owner="lead",
                owner_slot="lead",
                repo="hermes-agent",
                kind="planning",
            )
        ],
    )

    assert is_mission_lead_actor(task, "captain")
    assert not is_mission_lead_actor(task, "neko_supervisor")


def test_handoff_payload_does_not_synthesize_per_task_plan():
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

    assert_default_blueprint_plan(plan, task)
    assert not any(stage.id == "backend_contract_smoke" for stage in plan.stages)


def test_handoff_payload_merges_observed_lane_requirement_into_existing_stage():
    task = make_task(
        title="Observed lane no-edit proof",
        description="Run a no-product-edit cross-stack observed-lane proof.",
        affected_repos=["EterniaBackend", "EterniaLauncher"],
    )

    plan = ensure_mission_plan(
        task,
        {
            "objective": "Run backend observed-lane proof.",
            "acceptance_criteria": [
                "Backend observed_proof_ids must come from agent_tool_trace.",
            ],
            "affected_repos": ["EterniaBackend"],
            "handoff_packet": {
                "target_owner": "backend_dev",
                "target_repo": "EterniaBackend",
                "proof_gate": {
                    "required": True,
                    "recipe_id": "backend_contract_smoke",
                    "observed_lane_required": True,
                    "observed_lane_requirement": "agent_tool_trace/run.tool.finished must populate observed_proof_ids",
                },
            },
        },
        actor="neko_supervisor",
    )

    backend = next(stage for stage in plan.stages if stage.id == "backend_implementation")
    # The explicit handoff packet's observed-lane requirement still merges onto the
    # existing stage (generic packet merge). The proof_recipe_id assertion was dropped
    # with the de-hardwiring: recipes are no longer injected by goal-text specialization.
    assert backend.proof_gate["observed_lane_required"] is True
    assert "agent_tool_trace" in backend.proof_gate["observed_lane_requirement"]


def test_backend_no_product_edit_investigation_uses_default_blueprint_without_implicit_route():
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

    assert_default_blueprint_plan(plan, task)
    assert not any(stage.id == "backend_investigation" for stage in plan.stages)


# Removed with the de-hardwiring: _specialize_default_no_edit_cross_stack_plan rewrote
# the backend/launcher stages by id (kind=proof_only, harness-owned recipes) for
# no-edit cross-stack goals. That stage-id specialization is gone — the graph carries
# both dev nodes and each assigned agent runs and self-reports; proof recipes are not
# injected by goal-text inference.


def test_backend_product_hardening_nongoal_keeps_blueprint_authority():
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

    assert_default_blueprint_plan(plan, task)
    assert plan.stages[1].repo == "EterniaBackend"


def test_backend_only_no_launcher_frontend_goal_does_not_rewrite_blueprint_from_handoff():
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

    assert_default_blueprint_plan(plan, task)
    assert task.requires_visual_proof is False


def test_neko_persona_diagnostic_self_observation_uses_default_blueprint_without_implicit_single_stage():
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

    assert_default_blueprint_plan(plan, task)
    assert not any(stage.id == "neko_diagnostic" for stage in plan.stages)


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
            "mission_plan": {
                "current_stage_id": "launcher_implementation",
                "stages": [
                    {
                        "id": "launcher_implementation",
                        "title": "Launcher Implementation",
                        "objective": "Implement the narrow Mission Control terminal thinking visibility UI fix.",
                        "owner": "dev",
                        "repo": "EterniaLauncher",
                        "kind": "implementation",
                        "requires_product_edit": True,
                    }
                ],
            }
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


def test_exact_proof_goal_projects_exact_stage_defaults_not_mission_control_flutter():
    task = make_task(
        title="Mission Control Harness exact proof trust probe",
        description=(
            "Write docs/scratch/e2e_trust_probe.md. "
            "The final Harness-owned command proof must run exactly: echo e2e-trust-probe."
        ),
        acceptance_criteria=["Preserve Mission Control/Harness snapshots and archive evidence."],
        non_goals=["Do not run Flutter analyze/tests."],
        affected_repos=["EterniaLauncher"],
    )

    ensure_mission_plan(
        task,
        {
            "mission_plan": {
                "current_stage_id": "launcher_implementation",
                "stages": [
                    {
                        "id": "launcher_implementation",
                        "title": "Launcher Implementation",
                        "objective": "Implement the narrow Mission Control trust probe file.",
                        "owner": "dev",
                        "repo": "EterniaLauncher",
                        "kind": "implementation",
                        "requires_product_edit": True,
                    }
                ],
            }
        },
        actor="neko_supervisor",
    )

    stage = task.stages[0]
    assert stage.affected_paths == ["docs/scratch/e2e_trust_probe.md"]
    assert stage.test_plan == ["echo e2e-trust-probe"]


def test_exact_proof_projection_reads_locked_mission_intent_after_description_rewrite():
    task = make_task(
        title="Mission Control Harness exact proof trust probe",
        description="Create the concise Launcher trust-probe artifact.",
        acceptance_criteria=["Launcher Dev attaches focused evidence and QA reviews it."],
        affected_repos=["EterniaLauncher"],
    )
    task.mission_plan = MissionPlan(
        enabled=True,
        mission_intent=MissionIntent(
            title="Mission Control Harness exact proof trust probe",
            objective=(
                "Write docs/scratch/e2e_trust_probe_qa.md. "
                "The final Harness-owned command proof must run exactly: echo e2e-trust-probe-qa. "
                "Do not run Flutter analyze/tests."
            ),
            acceptance_criteria=[],
            source_task_id=task.id,
        ),
        current_stage_id="implement",
        stages=[
            MissionPlanStage(
                id="implement",
                title="Launcher Implementation",
                objective="Implement the Launcher trust probe.",
                owner="dev",
                owner_slot="dev",
                repo="EterniaLauncher",
                kind="implementation",
                requires_product_edit=True,
            )
        ],
    )

    ensure_mission_plan(task, {}, actor="neko_supervisor")

    stage = task.stages[0]
    assert stage.affected_paths == ["docs/scratch/e2e_trust_probe_qa.md"]
    assert stage.test_plan == ["echo e2e-trust-probe-qa"]


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
            "mission_plan": {
                "current_stage_id": "launcher_implementation",
                "stages": [
                    {
                        "id": "launcher_implementation",
                        "title": "Launcher Implementation",
                        "objective": "Implement the scoped EterniaLauncher feed/post media presentation change.",
                        "owner": "dev",
                        "repo": "EterniaLauncher",
                        "kind": "implementation",
                        "requires_product_edit": True,
                        "requires_visual_proof": True,
                    }
                ],
            }
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
            "mission_plan": {
                "current_stage_id": "launcher_implementation",
                "stages": [
                    {
                        "id": "launcher_implementation",
                        "title": "Launcher Certification",
                        "objective": "Certify the committed Launcher post media 3x inline sizing change without editing product code by collecting focused command proof.",
                        "owner": "dev",
                        "repo": "EterniaLauncher",
                        "kind": "proof_only",
                        "requires_product_edit": False,
                    }
                ],
            }
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
    assert task.mission_plan.stages[0].status == StageStatus.REWORK

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
    assert summary["current_stage_id"] == "scope"
    assert summary["stages"][0]["owner"] == "neko_supervisor"
    assert summary["blueprint_id"] == "neko_two_dev_default"


# --- Stage 15.2: no task is born plan-less -----------------------------------
#
# One assertion per production task-creation entry point. The un-typed window
# between construction and the first tick is the ONLY shape that reached the
# retired legacy orchestrator, so closing it is a per-site invariant, not a
# module-level one — a new creation site that forgets a plan must fail here.


def _assert_graph_typed(task, *, site: str):
    assert task.mission_plan is not None, f"{site} created a plan-less task"
    assert task.mission_plan.stages, f"{site} created a stage-less plan"


def test_burn_in_case_tasks_are_graph_typed_at_creation():
    from agent_runtime.burn_in import STAGE47_CASES, _create_case_task

    for case_id, case in STAGE47_CASES.items():
        task = _create_case_task(case_id, case, hygiene=None)
        _assert_graph_typed(task, site=f"burn_in._create_case_task[{case_id}]")
        assert task.mission_plan.blueprint_id


def test_mission_goal_creation_entrypoint_is_removed_from_stage_graph():
    assert importlib.util.find_spec("agent_runtime.mission_goal") is None


def test_persona_diagnostic_tasks_are_typed_at_creation():
    from agent_runtime.persona_diagnostics import PersonaDiagnosticOptions, _attach_persona_stage
    from agent_runtime.states import TaskState as _TaskState

    for persona_id in ("dev", "backend_dev", "qa"):
        task = make_task(id=f"task_diag_{persona_id}", state=_TaskState.CREATED)
        task.mission_plan = None
        _attach_persona_stage(task, persona_id=persona_id)
        _assert_graph_typed(task, site=f"persona_diagnostics._attach_persona_stage[{persona_id}]")
    assert PersonaDiagnosticOptions is not None


def test_scope_control_child_missions_are_graph_typed_at_creation():
    from agent_runtime.default_plan import ensure_default_mission_plan as _ensure

    child = make_task(id="task_child", state=TaskState.CREATED)
    child.mission_plan = None
    _ensure(child)
    _assert_graph_typed(child, site="scope_control fork child")
    assert child.mission_plan.blueprint_id


def test_blueprint_run_cli_and_api_tasks_carry_the_instantiated_plan():
    """`harness blueprint run` / POST /api/blueprints/{id}/run and the smoke goal
    all construct the Task WITH `mission_plan=plan`; pin the shared precondition
    (an instantiated blueprint always yields a graph-typed plan) rather than
    booting a CLI/web process per site."""

    from agent_runtime.blueprints.instantiate import instantiate_blueprint
    from agent_runtime.blueprints.store import BlueprintStore
    from agent_runtime.default_plan import DEFAULT_TASK_BLUEPRINT_ID

    from agent_runtime.default_plan import DEFAULT_TASK_BLUEPRINT_BINDINGS

    bp = BlueprintStore().get(DEFAULT_TASK_BLUEPRINT_ID)
    plan = instantiate_blueprint(bp, goal="typed from birth", bindings=dict(DEFAULT_TASK_BLUEPRINT_BINDINGS))
    assert plan is not None and plan.stages and plan.blueprint_id == DEFAULT_TASK_BLUEPRINT_ID
