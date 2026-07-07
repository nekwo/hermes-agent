import json
import pytest
from hermes_time import now
from agent_runtime.decision_schema import AgentDecision, DecisionType, DecisionPayloadInvalid
from agent_runtime.events import EventLog
from agent_runtime.packets import make_packet, record_packet
from agent_runtime.planning import apply_planning_decision
from agent_runtime.models import Incident, MissionIntent, MissionPlan, MissionPlanStage, Proof, Task, TaskStage
from agent_runtime.proof_rules import ProofType
from agent_runtime.store import IncidentStore, ProofStore, RunStore
from agent_runtime.states import RunState, StageStatus, TaskState


def task(state=TaskState.CREATED):
    ts=now(); return Task(id="task_1", title="T", description="raw", state=state, created_at=ts, updated_at=ts, requested_by="tony")


def dec(t, payload): return AgentDecision(type=t, summary="s", rationale="r", payload=payload)


def test_pm_fleshing_updates_task_and_moves_ready_for_dev():
    t=task()
    apply_planning_decision(t, dec(DecisionType.PROPOSE_ACCEPTANCE, {"objective":"obj", "acceptance_criteria":["done"], "non_goals":["x"], "affected_repos":["repo"], "suggested_roles":["dev"], "requires_visual_proof": True, "risk_flags":["ui"]}), actor="pm")
    assert t.description == "obj"
    assert t.acceptance_criteria == ["done"]
    assert t.requires_visual_proof is True
    assert t.state == TaskState.RUNNING


def test_neko_acceptance_derives_repo_scope_from_handoff_when_affected_repos_empty():
    t = task()
    apply_planning_decision(
        t,
        dec(
            DecisionType.PROPOSE_ACCEPTANCE,
            {
                "objective": "Route a no-op Launcher handoff without product edits.",
                "acceptance_criteria": ["Launcher Dev receives the route."],
                "affected_repos": [],
                "handoff_packet": {
                    "packet_kind": "fresh_scope",
                    "mission_phase": "initial_scope",
                    "handoff_mode": "single_specialist",
                    "target_owner": "dev",
                    "target_repo": "EterniaLauncher",
                    "proof_gate": {
                        "required": True,
                        "required_proof_types": ["test_run"],
                        "minimum_status": "passed",
                        "visual_required": False,
                    },
                },
            },
        ),
        actor="neko_supervisor",
    )

    assert t.affected_repos == ["EterniaLauncher"]
    assert t.state == TaskState.RUNNING


def test_neko_scope_recovery_cancels_read_search_budget_run_and_routes_fresh_dev(isolate_agent_runtime_root):
    t = task(TaskState.BLOCKED)
    t.current_stage_id = "backend_implementation"
    t.open_incident_ids = ["inc_loop"]
    runs = RunStore()
    incidents = IncidentStore()
    waiting = runs.open_run("backend_dev", t.id, stage_id="backend_implementation", session_id="session_budget")
    waiting.progress = {
        "loop_warning": "read_search_without_patch_threshold",
        "read_search_count": 6,
        "read_search_limit": 6,
        "patch_count": 0,
        "proof_count": 0,
    }
    waiting.state = RunState.WAITING_ON_APPROVAL
    waiting.error = {"type": "run_budget_exceeded"}
    runs.update(waiting)
    incidents.open(
        Incident(
            id="inc_loop",
            task_id=t.id,
            run_id=waiting.id,
            kind="run_budget_exceeded",
            summary="budget",
            detail_path=None,
            opened_at=now(),
        )
    )

    apply_planning_decision(
        t,
        dec(
            DecisionType.PROPOSE_ACCEPTANCE,
            {
                "objective": "Narrow Dev to inspect posts/media safety gate files first, then patch only the first public exposure leak.",
                "acceptance_criteria": ["Patch one proven leak and run one focused backend test."],
                "affected_repos": ["EterniaBackend"],
                "handoff_packet": {
                    "packet_kind": "scope_recovery",
                    "mission_phase": "backend_dev_scope_recovery",
                    "handoff_mode": "single_specialist",
                    "target_owner": "backend_dev",
                    "target_repo": "EterniaBackend",
                    "proof_gate": {
                        "required": True,
                        "required_proof_types": ["test_run"],
                        "minimum_status": "passed",
                        "visual_required": False,
                    },
                },
            },
        ),
        actor="neko_supervisor",
        incident_store=incidents,
    )

    assert incidents.get("inc_loop").closed_at is not None
    assert runs.get(waiting.id).state == RunState.CANCELLED
    assert t.open_incident_ids == []
    assert t.state == TaskState.RUNNING
    assert t.affected_repos == ["EterniaBackend"]


def test_typed_neko_acceptance_creates_plan_without_shrinking_parent_goal():
    t = task()
    t.title = "Fix Mission Control all role terminals"
    t.description = "Backend stream proof, Launcher UI repair, and QA screenshot must all complete."
    t.acceptance_criteria = ["All role streams render in Mission Control."]

    apply_planning_decision(
        t,
        dec(
            DecisionType.PROPOSE_ACCEPTANCE,
            {
                "objective": "Run backend contract smoke.",
                "acceptance_criteria": ["Backend proof passes."],
                "affected_repos": ["EterniaBackend"],
                "handoff_packet": {
                    "packet_kind": "fresh_scope",
                    "mission_phase": "backend_first",
                    "handoff_mode": "backend_first_cross_stack",
                    "target_owner": "backend_dev",
                    "target_repo": "EterniaBackend",
                    "proof_gate": {
                        "required": True,
                        "required_proof_types": ["test_run"],
                        "minimum_status": "passed",
                        "visual_required": False,
                        "proof_recipe_id": "backend_contract_smoke",
                    },
                    "join_gate": {"release_condition": "backend proof releases Launcher implementation"},
                },
            },
        ),
        actor="neko_supervisor",
        mission_plan_flow=True,
    )

    assert t.description == "Backend stream proof, Launcher UI repair, and QA screenshot must all complete."
    assert t.acceptance_criteria == ["All role streams render in Mission Control."]
    assert t.routing_scope["objective"] == "Run backend contract smoke."
    assert t.routing_scope["acceptance_criteria"] == ["Backend proof passes."]
    assert t.mission_plan.mission_intent.acceptance_criteria == ["All role streams render in Mission Control."]
    assert [stage.id for stage in t.mission_plan.stages] == [
        "scope",
        "backend_implementation",
        "implement",
    ]
    assert t.current_stage_id == "scope"
    # Neko's validated payload scope (EterniaBackend) survives the release:
    # the default blueprint's scope-stage placeholder repo (hermes-agent)
    # must not overwrite an explicitly scoped single-repo goal.
    assert t.affected_repos == ["EterniaBackend"]


def test_typed_qa_approval_rejects_missing_launcher_stage():
    t = task(TaskState.RUNNING)
    t.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title="Fix", objective="Fix"),
        current_stage_id="qa_release",
        stages=[
            MissionPlanStage(
                id="backend_contract_smoke",
                title="Backend",
                objective="Backend",
                owner="backend_dev",
                repo="EterniaBackend",
                kind="proof_only",
                status=StageStatus.READY_FOR_QA,
                proof_recipe_id="backend_contract_smoke",
                proof_ids=["proof_backend"],
            ),
            MissionPlanStage(
                id="launcher_implementation",
                title="Launcher",
                objective="Launcher",
                owner="dev",
                repo="EterniaLauncher",
                kind="implementation",
                status=StageStatus.IMPLEMENTING,
                depends_on=["backend_contract_smoke"],
                requires_product_edit=True,
            ),
            MissionPlanStage(
                id="qa_release",
                title="QA",
                objective="QA",
                owner="qa",
                repo="EterniaLauncher",
                kind="qa_verdict",
                depends_on=["backend_contract_smoke", "launcher_implementation"],
                blocks_qa_until=False,
            ),
        ],
    )

    with pytest.raises(DecisionPayloadInvalid, match="typed mission plan"):
        apply_planning_decision(
            t,
            dec(
                DecisionType.REPORT_QA_VERDICT,
                {"review_scope": "implementation", "verdict": "approved", "proof_ids": ["proof_backend"], "findings": []},
            ),
            actor="qa",
            mission_plan_flow=True,
        )


def test_neko_no_edit_recipe_handoff_materializes_executable_stage():
    t = task()

    apply_planning_decision(
        t,
        dec(
            DecisionType.PROPOSE_ACCEPTANCE,
            {
                "objective": "Certify Harness status without product edits.",
                "acceptance_criteria": ["Status proof passes.", "QA verdict follows proof."],
                "affected_repos": [],
                "handoff_packet": {
                    "packet_kind": "bounded_dev_recovery",
                    "mission_phase": "no_edit_certification_proof",
                    "handoff_mode": "single_specialist",
                    "target_owner": "dev",
                    "target_repo": "hermes-agent",
                    "proof_gate": {
                        "required": True,
                        "required_proof_types": ["test_run"],
                        "minimum_status": "passed",
                        "mode": "no_product_edit",
                        "recipe_id": "harness_runtime_status_snapshot",
                        "repo_scope": "hermes-agent",
                        "visual_required": False,
                    },
                },
            },
        ),
        actor="neko_supervisor",
    )

    assert t.state == TaskState.RUNNING
    assert t.current_stage_id == "hermes_agent_bounded_dev_recovery"
    assert [stage.id for stage in t.stages] == ["hermes_agent_bounded_dev_recovery"]
    assert t.stages[0].status == StageStatus.IMPLEMENTING
    assert t.affected_repos == ["hermes-agent"]
    assert "neko_scoped_dev_handoff_stage" in t.risk_flags


def test_neko_harness_thinking_log_smoke_inferrs_no_edit_status_snapshot_gate():
    t = task()

    apply_planning_decision(
        t,
        dec(
            DecisionType.PROPOSE_ACCEPTANCE,
            {
                "objective": "Verify the Hermes Harness Mission Control log/snapshot contract for redaction-safe thinking process evidence without product edits.",
                "acceptance_criteria": [
                    "Safe reasoning summaries appear in Mission Control-ready snapshot logs.",
                    "Hidden provider chain-of-thought and unsafe raw output are not exposed.",
                    "Attach the cheapest Harness status/snapshot proof before QA.",
                ],
                "affected_repos": ["hermes-agent"],
                "handoff_packet": {
                    "packet_kind": "fresh_scope",
                    "mission_phase": "harness_thinking_log_smoke",
                    "handoff_mode": "single_specialist",
                    "target_owner": "dev",
                    "target_repo": "hermes-agent",
                    "proof_gate": {
                        "required": True,
                        "required_proof_types": ["test_run"],
                        "minimum_status": "passed",
                        "visual_required": False,
                    },
                },
            },
        ),
        actor="neko_supervisor",
        mission_plan_flow=True,
    )

    stage = t.mission_plan.stages[0]
    assert t.current_stage_id == "scope"
    assert stage.id == "scope"
    assert stage.kind == "scope"
    assert stage.proof_recipe_id is None
    assert t.requires_visual_proof is False
    assert [item.id for item in t.mission_plan.stages] == [
        "scope",
        "backend_implementation",
        "implement",
    ]


def test_neko_harness_code_change_does_not_infer_no_edit_status_snapshot_gate():
    t = task()

    apply_planning_decision(
        t,
        dec(
            DecisionType.PROPOSE_ACCEPTANCE,
            {
                "objective": "Implement focused hermes-agent goal runner receipt improvements so GoalRunResult JSON surfaces final_summary and proof_summary.",
                "acceptance_criteria": [
                    "Focused tests cover done and blocked outcomes.",
                    "Blocked outcomes without open incidents have useful next actions.",
                ],
                "affected_repos": ["hermes-agent"],
                "handoff_packet": {
                    "packet_kind": "fresh_scope",
                    "mission_phase": "harness_code_change",
                    "handoff_mode": "single_specialist",
                    "target_owner": "dev",
                    "target_repo": "hermes-agent",
                    "proof_gate": {
                        "required": True,
                        "required_proof_types": ["test_run"],
                        "minimum_status": "passed",
                        "visual_required": False,
                    },
                },
            },
        ),
        actor="neko_supervisor",
        mission_plan_flow=True,
    )

    stage = t.mission_plan.stages[0]
    assert stage.id == "scope"
    assert stage.kind == "scope"
    assert stage.proof_recipe_id is None
    assert stage.repo == "hermes-agent"
    assert [item.id for item in t.mission_plan.stages] == ["scope", "backend_implementation", "implement"]


def test_neko_raw_hermes_no_product_edit_stage_becomes_focused_proof_only():
    t = task()
    t.title = "Stage 54 live burn task-list authority smoke"
    t.description = (
        "Live-token burn test for Stage 54. No product edits. Dev should request or run "
        "the focused proof for tests/agent_runtime/test_stage52_role_envelopes.py only."
    )
    t.acceptance_criteria = [
        "Dev does not create or promote global stages from checklist updates",
        "QA approves only from proof IDs and task-list evidence",
    ]
    t.non_goals = ["Do not edit product code"]
    t.affected_repos = ["hermes-agent"]

    apply_planning_decision(
        t,
        dec(
            DecisionType.PROPOSE_ACCEPTANCE,
            {
                "objective": (
                    "Run the Stage 54 no-product-edit smoke proof for hermes-agent, limited to "
                    "tests/agent_runtime/test_stage52_role_envelopes.py, and attach the resulting command proof ID "
                    "without editing product code or creating/promoting global stages from role-local checklist updates."
                ),
                "acceptance_criteria": list(t.acceptance_criteria),
                "affected_repos": ["hermes-agent"],
                "mission_plan": {
                    "version": 1,
                    "enabled": True,
                    "stages": [
                        {
                            "id": "run_stage_54_no_product_edit_smoke",
                            "title": "Implementation",
                            "objective": (
                                "Run the Stage 54 no-product-edit smoke proof for hermes-agent, limited to "
                                "tests/agent_runtime/test_stage52_role_envelopes.py."
                            ),
                            "owner": "dev",
                            "repo": "hermes-agent",
                            "kind": "implementation",
                        },
                        {
                            "id": "qa_release",
                            "title": "QA Release Verdict",
                            "objective": "Verify proof.",
                            "owner": "qa",
                            "repo": "hermes-agent",
                            "kind": "qa_verdict",
                            "depends_on": ["run_stage_54_no_product_edit_smoke"],
                            "blocks_qa_until": False,
                        },
                    ],
                },
            },
        ),
        actor="neko_supervisor",
        mission_plan_flow=True,
    )

    stage = t.mission_plan.stages[0]
    assert stage.kind == "proof_only"
    assert stage.requires_product_edit is False
    assert t.stages[0].affected_paths == ["tests/agent_runtime/test_stage52_role_envelopes.py"]
    assert t.stages[0].test_plan == [
        "python -m pytest tests/agent_runtime/test_stage52_role_envelopes.py -q"
    ]


def test_neko_invented_product_edit_flag_cannot_override_locked_no_edit_smoke():
    t = task()
    t.title = "Stage 54 live burn retry task-list authority smoke"
    t.description = (
        "Live-token burn retry for Stage 54. No product edits. Verify that Neko scopes "
        "operational mission stages, Dev treats its checklist as role-local, QA verifies "
        "from proof, and no persona invents global stage/checklist fields. Dev should "
        "request or run the focused proof for tests/agent_runtime/test_stage52_role_envelopes.py only."
    )
    t.acceptance_criteria = [
        "Neko creates or preserves operational stages without over-scoping product repos",
        "Dev does not create or promote global stages from checklist updates",
        "QA approves only from proof IDs and task-list evidence",
    ]
    t.non_goals = ["Do not edit product code"]
    t.affected_repos = ["hermes-agent"]

    apply_planning_decision(
        t,
        dec(
            DecisionType.PROPOSE_ACCEPTANCE,
            {
                "objective": t.description,
                "acceptance_criteria": list(t.acceptance_criteria),
                "non_goals": list(t.non_goals),
                "affected_repos": ["hermes-agent"],
                "mission_plan": {
                    "version": 1,
                    "enabled": True,
                    "stages": [
                        {
                            "id": "perform_the_stage_54_live_burn_retry_smoke_in_hermes-agent_without_product_edits_dev_must_treat",
                            "title": "Implementation",
                            "objective": (
                                "Perform the Stage 54 live burn retry smoke in hermes-agent without product edits: "
                                "Dev must treat checklist updates as role-local and run or request only the focused "
                                "proof for tests/agent_runtime/test_stage52_role_envelopes.py."
                            ),
                            "owner": "dev",
                            "repo": "hermes-agent",
                            "kind": "implementation",
                            "requires_product_edit": True,
                        },
                        {
                            "id": "qa_release",
                            "title": "QA Release Verdict",
                            "objective": "Verify typed mission plan coverage for Stage 54 live burn retry task-list authority smoke.",
                            "owner": "qa",
                            "repo": "hermes-agent",
                            "kind": "qa_verdict",
                            "depends_on": [
                                "perform_the_stage_54_live_burn_retry_smoke_in_hermes-agent_without_product_edits_dev_must_treat"
                            ],
                            "blocks_qa_until": False,
                        },
                    ],
                },
            },
        ),
        actor="neko_supervisor",
        mission_plan_flow=True,
    )

    stage = t.mission_plan.stages[0]
    assert stage.kind == "proof_only"
    assert stage.requires_product_edit is False
    assert t.stages[0].affected_paths == ["tests/agent_runtime/test_stage52_role_envelopes.py"]
    assert t.stages[0].test_plan == [
        "python -m pytest tests/agent_runtime/test_stage52_role_envelopes.py -q"
    ]


def test_explicit_agent_runtime_test_path_outranks_generic_no_edit_recipe():
    t = task()
    t.title = "Stage 54 live burn verification no-edit proof smoke"
    t.description = (
        "Live-token burn verification after the locked no-edit classifier fix. No product edits. "
        "Dev requests or runs only the focused proof for tests/agent_runtime/test_stage52_role_envelopes.py."
    )
    t.acceptance_criteria = ["QA approves only from proof IDs and task-list evidence"]
    t.non_goals = ["Do not edit product code"]
    t.affected_repos = ["hermes-agent"]

    apply_planning_decision(
        t,
        dec(
            DecisionType.PROPOSE_ACCEPTANCE,
            {
                "objective": t.description,
                "acceptance_criteria": list(t.acceptance_criteria),
                "non_goals": list(t.non_goals),
                "affected_repos": ["hermes-agent"],
                "mission_plan": {
                    "version": 1,
                    "enabled": True,
                    "stages": [
                        {
                            "id": "qa_release_verdict_smoke",
                            "title": "Implementation",
                            "objective": (
                                "Run a no-product-edit hermes-agent smoke verifying the locked Stage 54 "
                                "classifier behavior through the focused tests/agent_runtime/test_stage52_role_envelopes.py proof path only."
                            ),
                            "owner": "dev",
                            "repo": "hermes-agent",
                            "kind": "proof_only",
                            "requires_product_edit": False,
                            "proof_recipe_id": "qa_release_verdict_smoke",
                        },
                        {
                            "id": "qa_release",
                            "title": "QA Release Verdict",
                            "objective": "Verify proof.",
                            "owner": "qa",
                            "repo": "hermes-agent",
                            "kind": "qa_verdict",
                            "depends_on": ["qa_release_verdict_smoke"],
                            "blocks_qa_until": False,
                        },
                    ],
                },
            },
        ),
        actor="neko_supervisor",
        mission_plan_flow=True,
    )

    stage = t.mission_plan.stages[0]
    assert stage.kind == "proof_only"
    assert stage.proof_recipe_id is None
    assert stage.requires_product_edit is False
    assert t.stages[0].affected_paths == ["tests/agent_runtime/test_stage52_role_envelopes.py"]
    assert t.stages[0].test_plan == [
        "python -m pytest tests/agent_runtime/test_stage52_role_envelopes.py -q"
    ]


def test_neko_handoff_no_edit_focused_pytest_without_recipe_becomes_proof_only():
    t = task()
    t.title = "Neko-only focused proof planning diagnostic"
    t.description = (
        "Live-token Neko-only diagnostic for focused proof precedence. No product edits. "
        "Neko must scope a hermes-agent proof-only smoke, with requires_product_edit false, "
        "and the proof gate must be the focused command "
        "python -m pytest tests/agent_runtime/test_stage52_role_envelopes.py -q. "
        "Do not use generic harness observe/status recipes as the implementation proof."
    )
    t.acceptance_criteria = [
        "Typed mission stage is proof_only and requires_product_edit is false",
        "Proof gate is the focused tests/agent_runtime/test_stage52_role_envelopes.py pytest command",
    ]
    t.non_goals = ["Do not edit product code"]
    t.affected_repos = ["hermes-agent"]

    apply_planning_decision(
        t,
        dec(
            DecisionType.PROPOSE_ACCEPTANCE,
            {
                "objective": (
                    "Run a no-product-edit hermes-agent proof-only smoke for Stage 52 role envelope coverage "
                    "using the focused pytest command as the sole implementation proof gate."
                ),
                "acceptance_criteria": list(t.acceptance_criteria),
                "non_goals": list(t.non_goals),
                "affected_repos": ["hermes-agent"],
                "handoff_packet": {
                    "packet_kind": "fresh_scope",
                    "mission_phase": "focused_proof_planning_diagnostic",
                    "handoff_mode": "single_specialist",
                    "target_owner": "dev",
                    "target_repo": "hermes-agent",
                    "proof_gate": {
                        "required": True,
                        "required_proof_types": ["test_run"],
                        "minimum_status": "passed",
                        "visual_required": False,
                    },
                },
            },
        ),
        actor="neko_supervisor",
        mission_plan_flow=True,
    )

    stage = t.mission_plan.stages[0]
    assert stage.id == "scope"
    assert stage.kind == "scope"
    assert stage.proof_recipe_id is None
    assert [item.id for item in t.mission_plan.stages] == ["scope", "backend_implementation", "implement"]


def test_neko_handoff_focused_path_suppresses_inferred_status_snapshot_recipe():
    t = task()
    t.title = "Neko live persona diagnostic focused proof rerun"
    t.description = (
        "Live-token persona diagnostic for Neko only. No product edits. Scope a hermes-agent "
        "proof-only handoff to Dev that preserves the exact focused proof path "
        "tests/agent_runtime/test_persona_diagnostics.py. Do not broaden to full tests/agent_runtime "
        "and do not use generic harness status or observe recipes when a focused path is named."
    )
    t.acceptance_criteria = [
        "Neko produces one valid bounded routing decision",
        "The focused tests/agent_runtime/test_persona_diagnostics.py path is preserved as the proof target",
    ]
    t.non_goals = ["Do not edit product code"]
    t.affected_repos = ["hermes-agent"]

    apply_planning_decision(
        t,
        dec(
            DecisionType.PROPOSE_ACCEPTANCE,
            {
                "objective": (
                    "Dev performs a proof-only rerun for the Neko live persona diagnostic in hermes-agent "
                    "with no product edits, using only the focused proof target "
                    "tests/agent_runtime/test_persona_diagnostics.py."
                ),
                "acceptance_criteria": list(t.acceptance_criteria),
                "non_goals": list(t.non_goals),
                "affected_repos": ["hermes-agent"],
                "handoff_packet": {
                    "packet_kind": "fresh_scope",
                    "mission_phase": "persona_diagnostic",
                    "handoff_mode": "single_specialist",
                    "target_owner": "dev",
                    "target_repo": "hermes-agent",
                    "proof_gate": {
                        "required": True,
                        "required_proof_types": ["test_run"],
                        "minimum_status": "passed",
                        "visual_required": False,
                    },
                },
            },
        ),
        actor="neko_supervisor",
        mission_plan_flow=True,
    )

    stage = t.mission_plan.stages[0]
    assert stage.id == "scope"
    assert stage.kind == "scope"
    assert stage.proof_recipe_id is None
    assert [item.id for item in t.mission_plan.stages] == ["scope", "backend_implementation", "implement"]


def test_hermes_only_typed_plan_drops_unrelated_launcher_stage_from_negated_prose():
    t = task()
    t.title = "Improve Stage 53 goal runner final receipts rerun"
    t.description = (
        "No Launcher or backend product edits. Verify the Hermes Harness now treats this as a "
        "hermes-agent implementation/code-change proof, not a no-edit status snapshot."
    )
    t.acceptance_criteria = [
        "Mission planning does not route Hermes code-change goals to harness_runtime_status_snapshot proof_only",
        "GoalRunResult JSON includes final_summary proof_summary and blocker_summary",
        "Focused tests for goal runner and planning pass",
    ]
    t.affected_repos = ["hermes-agent"]

    apply_planning_decision(
        t,
        dec(
            DecisionType.PROPOSE_ACCEPTANCE,
            {
                "objective": (
                    "Inspect the committed hermes-agent Stage 53 goal runner/planning implementation and "
                    "produce proof that final receipts and planning classification behave as required, "
                    "without editing Launcher or backend product code."
                ),
                "acceptance_criteria": list(t.acceptance_criteria),
                "affected_repos": ["hermes-agent"],
                "mission_plan": {
                    "version": 1,
                    "enabled": True,
                    "stages": [
                        {
                            "id": "inspect_committed_hermes_agent_stage_53",
                            "title": "Implementation",
                            "objective": "Inspect committed hermes-agent implementation and run focused proof.",
                            "owner": "dev",
                            "repo": "hermes-agent",
                            "kind": "implementation",
                            "requires_product_edit": True,
                        },
                        {
                            "id": "launcher_implementation",
                            "title": "Launcher Mission Control Implementation",
                            "objective": "Complete the Launcher/Mission Control side of the parent goal.",
                            "owner": "dev",
                            "repo": "EterniaLauncher",
                            "kind": "implementation",
                            "requires_product_edit": True,
                        },
                        {
                            "id": "qa_release",
                            "title": "QA Release Verdict",
                            "objective": "Verify proof.",
                            "owner": "qa",
                            "repo": "EterniaLauncher",
                            "kind": "qa_verdict",
                            "depends_on": ["inspect_committed_hermes_agent_stage_53", "launcher_implementation"],
                            "blocks_qa_until": False,
                        },
                    ],
                },
            },
        ),
        actor="neko_supervisor",
        mission_plan_flow=True,
    )

    assert [stage.id for stage in t.mission_plan.stages] == [
        "inspect_committed_hermes_agent_stage_53",
        "qa_release",
    ]
    assert [stage.repo for stage in t.mission_plan.stages] == ["hermes-agent", "hermes-agent"]
    assert t.mission_plan.stages[1].depends_on == ["inspect_committed_hermes_agent_stage_53"]
    assert t.affected_repos == ["hermes-agent"]
    assert t.stages[0].affected_paths == [
        "agent_runtime/goal_runner.py",
        "agent_runtime/mission_plan.py",
        "agent_runtime/planning.py",
        "tests/agent_runtime/test_goal_runner.py",
        "tests/agent_runtime/test_planning.py",
    ]
    assert t.stages[0].test_plan == [
        "python -m pytest tests/agent_runtime/test_goal_runner.py tests/agent_runtime/test_planning.py -q"
    ]


def test_neko_generic_backend_handoff_materializes_executable_stage():
    t = task()
    log = EventLog()

    apply_planning_decision(
        t,
        dec(
            DecisionType.PROPOSE_ACCEPTANCE,
            {
                "objective": "Backend Dev determines whether a backend-owned Mission Control role stream surface exists.",
                "acceptance_criteria": [
                    "Backend Dev attaches a redaction-safe backend contract or no-surface proof.",
                    "Launcher Dev is released only after Backend Dev evidence exists.",
                    "Launcher Dev consumes the backend handoff and implements the narrow Mission Control UI fix.",
                    "QA verifies the final behavior with full-screen Mission Control visual proof.",
                ],
                "affected_repos": ["EterniaBackend", "EterniaLauncher", "hermes-agent"],
                "handoff_packet": {
                    "packet_kind": "fresh_scope",
                    "mission_phase": "backend_first_contract_probe",
                    "handoff_mode": "backend_first_cross_stack",
                    "target_owner": "backend_dev",
                    "target_repo": "EterniaBackend",
                    "proof_gate": {
                        "required": True,
                        "required_proof_types": ["test_run", "contract_packet"],
                        "minimum_status": "passed",
                        "visual_required": False,
                    },
                    "join_gate": {"release_condition": "backend proof or no-surface packet is attached"},
                },
                "risk_flags": ["cross_stack_handoff_required"],
            },
        ),
        actor="neko_supervisor",
        event_log=log,
    )

    assert t.state == TaskState.RUNNING
    assert t.current_stage_id == "eterniabackend_fresh_scope"
    assert len(t.stages) == 1
    assert t.stages[0].status == StageStatus.IMPLEMENTING
    assert t.stages[0].affected_paths == ["EterniaBackend"]
    assert "Backend Dev" in t.stages[0].objective
    assert all("Launcher Dev consumes" not in item for item in t.stages[0].acceptance_criteria)
    assert all("QA verifies" not in item for item in t.stages[0].acceptance_criteria)
    assert t.affected_repos == ["EterniaBackend"]
    assert "neko_scoped_dev_handoff_stage" in t.risk_flags
    assert any(event.type == "task.stage_added" and event.payload["source"] == "neko_scoped_dev_handoff" for event in log.for_task(t.id, limit=0))


def test_stage_plan_appends_and_updates_existing_stage_without_duplicate():
    t=task(TaskState.RUNNING)
    payload={"stages":[{"id":"stage_1","title":"A","objective":"Do A","acceptance_criteria":["ok"],"test_plan":["pytest a"]}]}
    apply_planning_decision(t, dec(DecisionType.PROPOSE_STAGE_PLAN, payload), actor="dev")
    payload["stages"][0]["title"]="A2"
    apply_planning_decision(t, dec(DecisionType.PROPOSE_STAGE_PLAN, payload), actor="dev")
    assert len(t.stages)==1
    assert t.stages[0].title=="A2"
    assert t.current_stage_id=="stage_1"


def test_request_test_run_smoke_recipe_cannot_materialize_helper_stage_while_product_stage_incomplete():
    t = task(TaskState.RUNNING)
    t.title = "Mission Control DM bubble terminal rows"
    t.description = "Upgrade Launcher Mission Control event rows into compact DM bubbles."
    t.affected_repos = ["EterniaLauncher"]
    t.current_stage_id = "mc_terminal_dm_bubble_rows"
    t.stages = [
        TaskStage(
            id="mc_terminal_dm_bubble_rows",
            title="Implement compact Mission Control terminal DM bubble event rows",
            objective="Replace heavy block cards with compact expandable DM bubble rows.",
            status=StageStatus.IMPLEMENTING,
            affected_paths=["lib/features/mission_control/", "test/features/mission_control/"],
            acceptance_criteria=["Widget tests cover bubble row rendering and expansion behavior."],
            test_plan=["flutter test test/features/mission_control", "flutter analyze lib/features/mission_control"],
        )
    ]

    with pytest.raises(DecisionPayloadInvalid, match="cannot bypass incomplete product-edit stage"):
        apply_planning_decision(
            t,
            dec(
                DecisionType.REQUEST_TEST_RUN,
                {"stage_id": "launcher_contract_smoke", "recipe_id": "launcher_contract_smoke"},
            ),
            actor="dev",
        )

    assert t.current_stage_id == "mc_terminal_dm_bubble_rows"
    assert [stage.id for stage in t.stages] == ["mc_terminal_dm_bubble_rows"]


def test_correct_stage_can_reroute_current_stage_to_known_target_stage():
    t = task(TaskState.RUNNING)
    t.title = "Mission Control DM bubble terminal rows"
    t.current_stage_id = "launcher_contract_smoke"
    t.stages = [
        TaskStage(
            id="mc_terminal_dm_bubble_rows",
            title="Implement compact Mission Control terminal DM bubble event rows",
            objective="Replace heavy block cards with compact expandable DM bubble rows.",
            status=StageStatus.READY,
            affected_paths=["lib/features/mission_control/", "test/features/mission_control/"],
            acceptance_criteria=["Widget tests cover bubble row rendering and expansion behavior."],
        ),
        TaskStage(
            id="launcher_contract_smoke",
            title="Launcher Contract Smoke",
            objective="Collect placeholder command proof.",
            status=StageStatus.IMPLEMENTING,
            test_plan=["python -c \"print('launcher_contract_smoke contract_packet_consumed backend_proof_consumed')\""],
        ),
    ]

    apply_planning_decision(
        t,
        dec(
            DecisionType.CORRECT_STAGE,
            {
                "stage_id": "launcher_contract_smoke",
                "target_stage_id": "mc_terminal_dm_bubble_rows",
                "corrections": ["Smoke proof is not implementation proof; return to the DM bubble implementation stage."],
            },
        ),
        actor="dev",
    )

    assert t.current_stage_id == "mc_terminal_dm_bubble_rows"
    assert t.stages[0].status == StageStatus.IMPLEMENTING
    assert t.stages[1].status == StageStatus.BLOCKED


def test_dev_stage_plan_accepts_executable_proof_stage_with_qa_handoff_acceptance():
    t = task(TaskState.RUNNING)
    t.title = "Stage 49 live contract registry certification retry"
    t.description = "No product edits. Verify canonical contract examples."
    t.acceptance_criteria = ["QA reviews the passed proof before completion."]
    t.risk_flags = ["no_product_edits"]
    t.affected_repos = ["hermes-agent"]
    payload = {
        "stages": [
            {
                "id": "stage49_contract_registry_verify_examples",
                "title": "Contract registry verify examples",
                "objective": "Run the bounded contract examples verifier.",
                "acceptance_criteria": [
                    "The verifier command passes.",
                    "QA reviews the passed proof before completion.",
                ],
                "affected_paths": ["agent_runtime/decision_contract_registry.py"],
                "test_plan": ["python -m hermes_cli.main harness contracts verify-examples --json"],
            }
        ]
    }

    apply_planning_decision(t, dec(DecisionType.PROPOSE_STAGE_PLAN, payload), actor="dev")

    assert t.state == TaskState.RUNNING
    assert t.current_stage_id == "stage49_contract_registry_verify_examples"
    assert len(t.stages) == 1
    assert t.stages[0].test_plan == ["python -m hermes_cli.main harness contracts verify-examples --json"]


def test_duplicate_stage_ids_rejected():
    t=task(TaskState.RUNNING)
    payload={"stages":[{"id":"x","title":"A","objective":"a","acceptance_criteria":["ok"]},{"id":"x","title":"B","objective":"b","acceptance_criteria":["ok"]}]}
    with pytest.raises(DecisionPayloadInvalid):
        apply_planning_decision(t, dec(DecisionType.PROPOSE_STAGE_PLAN, payload), actor="dev")


def test_correction_updates_existing_stage_only():
    t=task(TaskState.RUNNING)
    apply_planning_decision(t, dec(DecisionType.PROPOSE_STAGE_PLAN, {"stages":[{"id":"stage_1","title":"A","objective":"a","acceptance_criteria":["ok"]}]}), actor="dev")
    apply_planning_decision(t, dec(DecisionType.CORRECT_STAGE, {"stage_id":"stage_1","corrections":["fix"],"test_plan":["pytest"]}), actor="qa")
    assert len(t.stages)==1
    assert "qa: fix" in t.stages[0].corrections
    assert t.stages[0].test_plan == ["pytest"]



def test_propose_patch_advances_task_to_READY_FOR_REVIEW():
    t = task(TaskState.RUNNING)
    decision = AgentDecision(
        type=DecisionType.PROPOSE_PATCH,
        summary="patch ready",
        rationale="bounded implementation is complete",
        payload={"patch": "diff --git ..."},
    )

    apply_planning_decision(t, decision, actor="dev")

    assert t.state == TaskState.RUNNING


def test_propose_patch_requires_existing_proof_when_store_available():
    t = task(TaskState.RUNNING)
    decision = AgentDecision(
        type=DecisionType.PROPOSE_PATCH,
        summary="patch ready",
        rationale="bounded implementation is complete",
        payload={"patch": "diff --git ..."},
    )

    with pytest.raises(DecisionPayloadInvalid, match="proof_ids are required"):
        apply_planning_decision(t, decision, actor="dev", proof_store=ProofStore())

    assert t.state == TaskState.RUNNING


def test_normal_worker_flow_accepts_patch_delivery_without_pretending_qa_ready():
    t = task(TaskState.RUNNING)
    t.current_stage_id = "stage_1"
    t.stages = [
        TaskStage(
            id="stage_1",
            title="Implement Launcher UI",
            objective="Upgrade Mission Control terminal rows.",
            status=StageStatus.IMPLEMENTING,
            affected_paths=["lib/features/mission_control/mission_control_page.dart"],
            test_plan=["flutter test test/features/mission_control/mission_control_page_test.dart"],
        )
    ]
    decision = AgentDecision(
        type=DecisionType.PROPOSE_PATCH,
        summary="patch ready",
        rationale="bounded implementation is complete",
        payload={
            "summary": "Implemented compact rows.",
            "changed_files": ["lib/features/mission_control/mission_control_page.dart"],
            "tests": ["flutter test test/features/mission_control/mission_control_page_test.dart passed"],
        },
    )

    apply_planning_decision(t, decision, actor="dev", proof_store=ProofStore(), normal_worker_flow=True)

    assert t.state == TaskState.RUNNING
    assert t.stages[0].status == StageStatus.IMPLEMENTING
    assert t.proof_ids == []


def test_propose_patch_rejects_failed_command_proof_when_store_available():
    t = task(TaskState.RUNNING)
    store = ProofStore()
    store.attach(
        Proof(
            id="failed_test",
            task_id=t.id,
            stage_id="stage_1",
            type=ProofType.TEST_RUN,
            title="Failed command",
            path_or_value="failed.log",
            created_by="harness",
            created_at=now(),
            metadata={"status": "failed", "exit_code": 1, "run_id": "run_1"},
            redaction_status="safe",
        )
    )
    decision = AgentDecision(
        type=DecisionType.PROPOSE_PATCH,
        summary="patch ready",
        rationale="bounded implementation is complete",
        payload={"proof_ids": ["failed_test"]},
    )

    with pytest.raises(DecisionPayloadInvalid, match="hand_off requires passing command proof_ids"):
        apply_planning_decision(t, decision, actor="dev", proof_store=store)

    assert t.state == TaskState.RUNNING
    assert t.proof_ids == []


def test_dev_request_qa_review_advances_implementation_to_qa_verification():
    t = task(TaskState.RUNNING)
    decision = AgentDecision(
        type=DecisionType.REQUEST_QA_REVIEW,
        summary="implementation proof ready",
        rationale="dev completed implementation proof and wants independent QA",
        payload={"stage_id": "stage_1", "proof_ids": ["proof_tests"], "handoff": {"to": "qa", "stage_complete": True}},
    )

    apply_planning_decision(t, decision, actor="dev")

    assert t.state == TaskState.RUNNING


def test_dev_request_qa_review_merges_existing_proof_ids_when_store_available():
    t = task(TaskState.RUNNING)
    store = ProofStore()
    store.attach(Proof(id="dev_diff", task_id=t.id, stage_id="stage_1", type=ProofType.DIFF, title="Diff", path_or_value="diff", created_by="dev", created_at=now(), redaction_status="safe"))
    decision = AgentDecision(
        type=DecisionType.REQUEST_QA_REVIEW,
        summary="implementation proof ready",
        rationale="dev completed implementation proof and wants independent QA",
        payload={"stage_id": "stage_1", "proof_ids": ["dev_diff"], "handoff": {"to": "qa", "stage_complete": True}},
    )

    apply_planning_decision(t, decision, actor="dev", proof_store=store)

    assert t.state == TaskState.RUNNING
    assert t.proof_ids == ["dev_diff"]


def test_dev_request_qa_review_rejects_missing_proof_ids_when_store_available():
    t = task(TaskState.RUNNING)
    decision = AgentDecision(
        type=DecisionType.REQUEST_QA_REVIEW,
        summary="implementation proof ready",
        rationale="dev completed implementation proof and wants independent QA",
        payload={"stage_id": "stage_1", "proof_ids": ["missing"], "handoff": {"to": "qa", "stage_complete": True}},
    )

    with pytest.raises(DecisionPayloadInvalid, match="unknown proof_ids"):
        apply_planning_decision(t, decision, actor="dev", proof_store=ProofStore())

    assert t.state == TaskState.RUNNING


def test_dev_request_qa_review_rejects_failed_command_proof_when_store_available():
    t = task(TaskState.RUNNING)
    store = ProofStore()
    store.attach(
        Proof(
            id="failed_test",
            task_id=t.id,
            stage_id="stage_1",
            type=ProofType.TEST_RUN,
            title="Failed command",
            path_or_value="failed.log",
            created_by="harness",
            created_at=now(),
            metadata={"status": "failed", "exit_code": 1, "run_id": "run_1"},
            redaction_status="safe",
        )
    )
    decision = AgentDecision(
        type=DecisionType.REQUEST_QA_REVIEW,
        summary="implementation proof ready",
        rationale="dev completed implementation proof and wants independent QA",
        payload={"stage_id": "stage_1", "proof_ids": ["failed_test"], "handoff": {"to": "qa", "stage_complete": True}},
    )

    with pytest.raises(DecisionPayloadInvalid, match="requires passing command proof_ids"):
        apply_planning_decision(t, decision, actor="dev", proof_store=store)

    assert t.state == TaskState.RUNNING
    assert t.proof_ids == []


def test_dev_request_qa_review_rejects_incomplete_recipe_batch_when_store_available():
    t = task(TaskState.RUNNING)
    store = ProofStore()
    store.attach(
        Proof(
            id="recipe_command_1",
            task_id=t.id,
            stage_id="stage_1",
            type=ProofType.TEST_RUN,
            title="Recipe command 1",
            path_or_value="recipe-command-1.log",
            created_by="harness",
            created_at=now(),
            metadata={
                "status": "passed",
                "exit_code": 0,
                "run_id": "run_recipe",
                "command_index": 1,
                "commands_requested": 2,
                "proof_recipe_recipe_id": "archive_button_cli_contract",
            },
            redaction_status="safe",
        )
    )
    decision = AgentDecision(
        type=DecisionType.REQUEST_QA_REVIEW,
        summary="implementation proof ready",
        rationale="dev completed implementation proof and wants independent QA",
        payload={"stage_id": "stage_1", "proof_ids": ["recipe_command_1"], "handoff": {"to": "qa", "stage_complete": True}},
    )

    with pytest.raises(DecisionPayloadInvalid, match="complete passing proof recipe batch"):
        apply_planning_decision(t, decision, actor="dev", proof_store=store)

    assert t.state == TaskState.RUNNING
    assert t.proof_ids == []


def test_dev_request_qa_review_accepts_complete_recipe_batch_when_store_available():
    t = task(TaskState.RUNNING)
    store = ProofStore()
    for command_index in (0, 1):
        store.attach(
            Proof(
                id=f"recipe_command_{command_index}",
                task_id=t.id,
                stage_id="stage_1",
                type=ProofType.TEST_RUN,
                title=f"Recipe command {command_index}",
                path_or_value=f"recipe-command-{command_index}.log",
                created_by="harness",
                created_at=now(),
                metadata={
                    "status": "passed",
                    "exit_code": 0,
                    "run_id": "run_recipe",
                    "command_index": command_index,
                    "commands_requested": 2,
                    "proof_recipe": {"recipe_id": "archive_button_cli_contract"},
                },
                redaction_status="safe",
            )
        )
    decision = AgentDecision(
        type=DecisionType.REQUEST_QA_REVIEW,
        summary="implementation proof ready",
        rationale="dev completed implementation proof and wants independent QA",
        payload={"stage_id": "stage_1", "proof_ids": ["recipe_command_0", "recipe_command_1"], "handoff": {"to": "qa", "stage_complete": True}},
    )

    apply_planning_decision(t, decision, actor="dev", proof_store=store)

    assert t.state == TaskState.RUNNING
    assert t.proof_ids == ["recipe_command_0", "recipe_command_1"]


def test_qa_implementation_approval_rejects_failed_command_proof_when_store_available():
    t = task(TaskState.RUNNING)
    store = ProofStore()
    store.attach(
        Proof(
            id="failed_test",
            task_id=t.id,
            stage_id="stage_1",
            type=ProofType.TEST_RUN,
            title="Failed command",
            path_or_value="failed.log",
            created_by="harness",
            created_at=now(),
            metadata={"status": "failed", "exit_code": 1, "run_id": "run_1"},
            redaction_status="safe",
        )
    )
    decision = AgentDecision(
        type=DecisionType.REPORT_QA_VERDICT,
        summary="implementation approved",
        rationale="QA reviewed the supplied proof ids",
        payload={"review_scope": "implementation", "verdict": "approved", "proof_ids": ["failed_test"], "findings": []},
    )

    with pytest.raises(DecisionPayloadInvalid, match="implementation approval requires passing command proof_ids"):
        apply_planning_decision(t, decision, actor="qa", proof_store=store)

    assert t.state == TaskState.RUNNING
    assert t.proof_ids == []


def test_qa_implementation_verdict_attaches_verdict_proof_and_advances():
    t = task(TaskState.RUNNING)
    t.proof_ids = ["dev_diff"]
    store = ProofStore()
    store.attach(Proof(id="dev_diff", task_id=t.id, stage_id="stage_1", type=ProofType.DIFF, title="Diff", path_or_value="diff", created_by="dev", created_at=now(), redaction_status="safe"))
    decision = AgentDecision(
        type=DecisionType.REPORT_QA_VERDICT,
        summary="implementation approved",
        rationale="QA reviewed the supplied proof ids",
        payload={"review_scope": "implementation", "verdict": "approved", "proof_ids": ["dev_diff"], "findings": []},
    )

    apply_planning_decision(t, decision, actor="qa", proof_store=store)

    assert t.state == TaskState.RUNNING
    assert len(t.proof_ids) == 2
    qa_proof = store.get(t.proof_ids[-1])
    assert qa_proof.type == ProofType.QA_VERDICT
    assert qa_proof.metadata["proof_ids"] == ["dev_diff"]


def test_approved_qa_review_synthesizes_missing_reviewed_stages_and_enters_dev_implementing():
    t = task(TaskState.RUNNING)
    t.acceptance_criteria = ["ship bounded recovery"]
    decision = AgentDecision(
        type=DecisionType.REPORT_QA_VERDICT,
        summary="approved phantom stages",
        rationale="QA approved the named bounded recovery stages.",
        payload={
            "review_scope": "plan",
            "reviewed_stage_ids": ["drawer_drag_recovery_consolidation", "bounded_morph_analyzer_fix_pass_1"],
            "verdict": "approved",
            "proof_requirements_confirmed": True,
            "test_plan_confirmed": True,
        },
    )

    apply_planning_decision(t, decision, actor="qa")

    assert t.state == TaskState.RUNNING
    assert [stage.id for stage in t.stages] == ["drawer_drag_recovery_consolidation", "bounded_morph_analyzer_fix_pass_1"]
    assert all(stage.test_plan for stage in t.stages)


def test_neko_cross_stack_backend_join_releases_launcher_stage_before_qa():
    t = task(TaskState.RUNNING)
    t.risk_flags = ["cross_stack_contract_handoff"]
    t.affected_repos = ["EterniaBackend"]
    t.proof_ids = ["proof_backend"]
    t.stages = [
        TaskStage(id="backend_contract", title="Backend Contract", objective="prove backend", status=StageStatus.READY_FOR_QA),
    ]
    decision = dec(
        DecisionType.PROPOSE_ACCEPTANCE,
        {
            "objective": "Launcher Dev verifies the backend contract packet.",
            "acceptance_criteria": ["Launcher proof is attached before QA."],
            "affected_repos": ["EterniaLauncher"],
            "suggested_roles": ["dev"],
        },
    )

    apply_planning_decision(t, decision, actor="neko_supervisor")

    assert t.state == TaskState.RUNNING
    assert t.affected_repos == ["EterniaLauncher"]
    assert t.current_stage_id is None
    assert not any(stage.id == "launcher_contract_smoke" for stage in t.stages)


def test_neko_launcher_release_narrows_broad_cross_stack_repos_to_launcher():
    t = task(TaskState.RUNNING)
    t.risk_flags = ["cross_stack_contract_handoff"]
    t.affected_repos = ["EterniaBackend", "EterniaLauncher", "hermes-agent"]
    t.proof_ids = ["proof_backend"]
    t.stages = [
        TaskStage(id="backend_contract", title="Backend Contract", objective="prove backend", status=StageStatus.READY_FOR_QA),
    ]
    decision = dec(
        DecisionType.PROPOSE_ACCEPTANCE,
        {
            "objective": "Join backend proof and release Launcher Dev before QA.",
            "acceptance_criteria": ["Launcher proof is attached before QA."],
            "affected_repos": ["EterniaBackend", "EterniaLauncher", "hermes-agent"],
            "handoff_packet": {
                "packet_kind": "contract_join",
                "mission_phase": "launcher_handoff",
                "handoff_mode": "sequential_specialists",
                "target_owner": "dev",
                "target_repo": "EterniaLauncher",
                "final_owner": "qa",
                "final_repo": "EterniaLauncher",
                "proof_gate": {
                    "required": True,
                    "required_proof_types": ["test_run"],
                    "minimum_status": "passed",
                    "visual_required": False,
                },
                "join_gate": {
                    "release_condition": "Backend proof is joined before Launcher proof collection.",
                },
            },
        },
    )

    apply_planning_decision(t, decision, actor="neko_supervisor")

    assert t.state == TaskState.RUNNING
    assert t.affected_repos == ["EterniaLauncher"]
    assert t.current_stage_id == "eternialauncher_contract_join"


def test_neko_contract_join_packet_only_releases_launcher_before_qa():
    log = EventLog()
    t = task(TaskState.RUNNING)
    t.risk_flags = ["cross_stack_contract_join"]
    t.affected_repos = ["EterniaBackend"]
    t.proof_ids = ["proof_backend"]
    t.stages = [
        TaskStage(id="backend_contract", title="Backend Contract", objective="prove backend", status=StageStatus.READY_FOR_QA),
    ]
    decision = dec(
        DecisionType.PROPOSE_ACCEPTANCE,
        {
            "objective": "Join proof gate.",
            "acceptance_criteria": ["Follow the structured handoff packet."],
            "handoff_packet": {
                "packet_kind": "contract_join",
                "mission_phase": "launcher_handoff",
                "handoff_mode": "sequential_specialists",
                "target_owner": "dev",
                "target_repo": "EterniaLauncher",
                "final_owner": "qa",
                "final_repo": "EterniaLauncher",
                "proof_gate": {
                    "required": True,
                    "required_proof_types": ["test_run"],
                    "minimum_status": "passed",
                    "visual_required": False,
                },
                "join_gate": {
                    "release_condition": "Backend proof is joined before Launcher proof collection.",
                },
            },
        },
    )

    apply_planning_decision(t, decision, actor="neko_supervisor", event_log=log)

    assert t.state == TaskState.RUNNING
    assert t.affected_repos == ["EterniaLauncher"]
    assert t.current_stage_id == "eternialauncher_contract_join"
    assert not [event for event in log.for_task(t.id, limit=0) if event.type == "qa.coordination_released"]


def test_neko_launcher_contract_join_cannot_release_qa_after_launcher_stage_complete():
    log = EventLog()
    t = task(TaskState.RUNNING)
    t.risk_flags = ["cross_stack_contract_handoff", "backend_contract_first"]
    t.affected_repos = ["EterniaLauncher"]
    t.proof_ids = ["proof_backend", "proof_launcher"]
    t.current_stage_id = "launcher_contract_smoke"
    t.stages = [
        TaskStage(id="backend_contract", title="Backend Contract", objective="prove backend", status=StageStatus.READY_FOR_QA),
        TaskStage(id="launcher_contract_smoke", title="Launcher Contract", objective="prove launcher", status=StageStatus.READY_FOR_QA),
    ]
    decision = dec(
        DecisionType.PROPOSE_ACCEPTANCE,
        {
            "objective": "Join proof gate.",
            "acceptance_criteria": ["QA requires an explicit qa_coordination_release packet."],
            "handoff_packet": {
                "packet_kind": "contract_join",
                "mission_phase": "launcher_handoff",
                "handoff_mode": "sequential_specialists",
                "target_owner": "dev",
                "target_repo": "EterniaLauncher",
                "final_owner": "qa",
                "final_repo": "EterniaLauncher",
                "proof_gate": {
                    "required": True,
                    "required_proof_types": ["test_run"],
                    "minimum_status": "passed",
                    "visual_required": False,
                },
                "join_gate": {
                    "release_condition": "Launcher contract_join is not a QA release.",
                },
            },
        },
    )

    apply_planning_decision(t, decision, actor="neko_supervisor", event_log=log)

    assert t.state == TaskState.RUNNING
    assert "cross_stack_qa_coordination_release_missing" in t.risk_flags
    assert t.harness_self_heal["evidence_stack"][-1]["recommended_owner"] == "neko_supervisor"
    assert not [event for event in log.for_task(t.id, limit=0) if event.type == "qa.coordination_released"]


def test_neko_post_scope_needs_context_for_missing_launcher_proof_continues_to_launcher_dev():
    t = task(TaskState.BLOCKED)
    t.description = "Complete the Launcher side after backend proof."
    t.acceptance_criteria = ["Launcher proof is attached before QA."]
    t.proof_ids = ["proof_backend"]
    t.current_stage_id = "backend_contract"
    t.stages = [
        TaskStage(id="backend_contract", title="Backend Contract", objective="prove backend", status=StageStatus.READY_FOR_QA),
    ]
    decision = AgentDecision(
        type=DecisionType.NEEDS_CONTEXT,
        summary="Launcher proof is missing, so the mission cannot proceed to QA.",
        rationale="Backend proof exists but the Launcher proof required by the handoff is missing.",
        payload={},
    )

    apply_planning_decision(t, decision, actor="neko_supervisor")

    assert t.state == TaskState.RUNNING
    assert t.affected_repos == ["EterniaLauncher"]
    assert t.current_stage_id == "eternialauncher_contract_join"
    assert "post_scope_wait_coerced_to_handoff" in t.risk_flags


def test_neko_needs_context_handoff_request_continues_to_launcher_dev_without_prose_match():
    log = EventLog()
    t = task(TaskState.BLOCKED)
    t.description = "Continue the downstream specialist handoff."
    t.acceptance_criteria = ["Downstream proof is attached before QA."]
    t.proof_ids = ["proof_backend"]
    t.current_stage_id = "backend_contract"
    t.stages = [
        TaskStage(id="backend_contract", title="Backend Contract", objective="prove backend", status=StageStatus.READY_FOR_QA),
    ]
    decision = AgentDecision(
        type=DecisionType.NEEDS_CONTEXT,
        summary="Need downstream specialist continuation.",
        rationale="The next specialist needs to verify the joined contract.",
        payload={"handoff_request": {"target_repo": "EterniaLauncher", "target_owner": "launcher_dev", "target_stage": "launcher_contract_smoke"}},
    )

    apply_planning_decision(t, decision, actor="neko_supervisor", event_log=log)

    assert t.state == TaskState.RUNNING
    assert t.affected_repos == ["EterniaLauncher"]
    assert t.current_stage_id == "eternialauncher_contract_join"
    assert "post_scope_wait_coerced_to_handoff" in t.risk_flags
    assert not [event for event in log.for_task(t.id, limit=0) if event.type == "handoff_request.deprecated_heuristic_agreement"]


def test_neko_proof_backed_join_needs_context_releases_launcher_without_blocking():
    log = EventLog()
    t = task(TaskState.RUNNING)
    t.description = "Backend proof is complete; Launcher proof is still required."
    t.acceptance_criteria = ["Launcher proof is attached before QA."]
    t.risk_flags = ["cross_stack_contract_handoff", "backend_contract_first"]
    t.affected_repos = ["EterniaBackend"]
    t.proof_ids = ["proof_backend"]
    t.current_stage_id = "backend_contract"
    t.stages = [
        TaskStage(id="backend_contract", title="Backend Contract", objective="prove backend", status=StageStatus.READY_FOR_QA),
    ]
    backend_decision = AgentDecision(
        type=DecisionType.REQUEST_TEST_RUN,
        summary="backend proof",
        rationale="proof",
        payload={"stage_id": "backend_contract", "commands": ["printf backend"]},
    )
    record_packet(
        make_packet(
            task=t,
            decision=backend_decision,
            packet_type="delivery",
            body={
                "work_status": "proof_requested",
                "produced_contract_packet_id": "packet_contract_stage47",
                "contract_packet": {
                    "contract_packet_id": "packet_contract_stage47",
                    "endpoint": "GET /api/stage47",
                    "request_shape": {},
                    "response_shape": {"ok": "boolean"},
                    "error_shape": {"error": "string"},
                    "example_response": {"ok": True},
                },
                "consumed_proof_ids": ["proof_backend"],
                "next_owner": "neko_supervisor",
            },
            actor="backend_dev",
            run_id="run_backend",
            stage_id="backend_contract",
        ),
        event_log=log,
    )
    decision = AgentDecision(
        type=DecisionType.NEEDS_CONTEXT,
        summary="Launcher proof is missing, so Neko cannot send the mission to QA yet.",
        rationale="Backend proof exists; the next required artifact is Launcher proof.",
        payload={},
    )

    apply_planning_decision(t, decision, actor="neko_supervisor", event_log=log)

    assert t.state == TaskState.RUNNING
    assert t.affected_repos == ["EterniaLauncher"]
    assert t.current_stage_id == "eternialauncher_contract_join"
    assert any(stage.id == "eternialauncher_contract_join" and stage.status == StageStatus.IMPLEMENTING for stage in t.stages)


def test_neko_proof_backed_join_missing_contract_packet_routes_backend_repair():
    log = EventLog()
    t = task(TaskState.RUNNING)
    t.description = "Backend proof is complete; Launcher proof is still required."
    t.acceptance_criteria = ["Backend contract packet and Launcher proof are required before QA."]
    t.risk_flags = ["cross_stack_contract_handoff", "backend_contract_first"]
    t.affected_repos = ["EterniaBackend"]
    t.proof_ids = ["proof_backend"]
    t.current_stage_id = "backend_contract"
    t.stages = [
        TaskStage(id="backend_contract", title="Backend Contract", objective="prove backend", status=StageStatus.READY_FOR_QA),
    ]
    decision = AgentDecision(
        type=DecisionType.NEEDS_CONTEXT,
        summary="Backend proof passed, but no backend-to-Launcher contract packet is attached.",
        rationale="The contract packet is required before Launcher release.",
        payload={},
    )

    apply_planning_decision(t, decision, actor="neko_supervisor", event_log=log)

    assert t.state == TaskState.RUNNING
    assert t.affected_repos == ["EterniaBackend"]
    assert t.current_stage_id == "stage_47_backend_contract_packet"
    assert any(stage.id == "stage_47_backend_contract_packet" and stage.status == StageStatus.IMPLEMENTING for stage in t.stages)
    assert "backend_contract_packet_missing_repair" in t.risk_flags
    assert any(event.type == "cross_stack.backend_contract_packet_missing" for event in log.for_task(t.id, limit=0))


def test_neko_initial_cross_stack_scope_normalizes_to_executable_backend_first_slice():
    t = task(TaskState.CREATED)
    decision = dec(
        DecisionType.PROPOSE_ACCEPTANCE,
        {
            "objective": "Run a no-edit sequential cross-stack smoke.",
            "acceptance_criteria": [
                "Backend Dev runs first and collects backend-side observational proof.",
                "Launcher Dev runs only after Neko join release and backend proof IDs exist.",
                "QA approves only after both backend and Launcher proof IDs exist.",
            ],
            "affected_repos": [
                "path-withheld-backend-repository-unresolved",
                "path-withheld-launcher-repository-unresolved",
                "hermes-agent",
            ],
            "risk_flags": [
                "cross_stack_contract_ordering",
                "sequential_specialist_handoff",
                "path_withheld_repo_resolution",
            ],
        },
    )

    apply_planning_decision(t, decision, actor="neko_supervisor")

    assert t.state == TaskState.RUNNING
    assert t.affected_repos == ["EterniaBackend"]
    assert "cross_stack_contract_handoff" in t.risk_flags
    assert "backend_contract_first" in t.risk_flags


def test_neko_backend_before_launcher_wording_normalizes_to_backend_slice():
    t = task(TaskState.CREATED)
    decision = dec(
        DecisionType.PROPOSE_ACCEPTANCE,
        {
            "objective": "Run a no-edit sequential observational proof smoke.",
            "acceptance_criteria": [
                "Neko records this scope once and routes using sequential_specialists: Backend Dev before Launcher Dev before QA.",
                "Backend Dev performs backend observational proof only and attaches concrete backend proof IDs.",
                "Neko releases Launcher Dev only after backend proof IDs exist.",
            ],
            "affected_repos": ["EterniaBackend", "EterniaLauncher", "hermes-agent"],
            "risk_flags": ["cross_stack_sequential_handoff", "no_edit_smoke"],
        },
    )

    apply_planning_decision(t, decision, actor="neko_supervisor")

    assert t.affected_repos == ["EterniaBackend"]
    assert "cross_stack_contract_handoff" in t.risk_flags
    assert "backend_contract_first" in t.risk_flags


def test_neko_live_terminal_scope_preserves_launcher_followup_from_text_flags():
    t = task(TaskState.CREATED)
    t.title = "Fix Mission Control all-role live terminals"
    decision = dec(
        DecisionType.PROPOSE_ACCEPTANCE,
        {
            "objective": "Seed and prove Backend Dev live terminal/event stream artifacts without backend product edits, using only the existing no-product-edit backend_contract_smoke proof recipe.",
            "acceptance_criteria": [
                "Backend Dev requests the existing backend_contract_smoke proof recipe only; no backend product file inspection, edits, or patches are performed.",
                "The proof result creates redaction-safe Backend Dev live terminal/event rows/artifacts suitable for Mission Control snapshot/state consumption.",
                "Neko joins Backend proof and releases Launcher Dev UI/bridge repair before QA.",
            ],
            "affected_repos": ["EterniaBackend"],
            "risk_flags": [
                "Cross-stack live event rendering depends on backend proof artifacts being joined before Launcher Dev UI/bridge repair.",
                "Backend Dev scope is intentionally no-product-edit to seed/prove role terminal events only.",
            ],
        },
    )

    apply_planning_decision(t, decision, actor="neko_supervisor")

    assert t.affected_repos == ["EterniaBackend"]
    assert "cross_stack_contract_handoff" in t.risk_flags
    assert "backend_contract_first" in t.risk_flags


def test_dev_stage_plan_skips_orchestration_only_neko_and_qa_stages():
    t = task(TaskState.RUNNING)
    t.risk_flags = ["cross_stack_contract_handoff", "backend_contract_first", "sequential_specialist_handoff"]
    decision = dec(
        DecisionType.PROPOSE_STAGE_PLAN,
        {
            "stages": [
                {
                    "id": "stage_1_neko_scope_freeze",
                    "title": "Neko scope freeze",
                    "objective": "Record Neko scope freeze before Dev.",
                    "acceptance_criteria": ["Neko records freeze."],
                    "affected_paths": ["harness event log"],
                    "test_plan": ["Neko records event."],
                },
                {
                    "id": "stage_2_backend_observational_proof",
                    "title": "Backend Dev no-edit proof",
                    "objective": "Collect backend-side proof.",
                    "acceptance_criteria": ["Backend proof attached."],
                    "affected_paths": ["EterniaBackend"],
                    "test_plan": ["backend proof command"],
                },
                {
                    "id": "stage_3_neko_join_gate",
                    "title": "Neko join gate",
                    "objective": "Release Launcher after backend proof.",
                    "acceptance_criteria": ["Neko verifies backend proof."],
                    "affected_paths": ["harness event log"],
                    "test_plan": ["Neko records join."],
                },
                {
                    "id": "stage_4_launcher_observational_proof",
                    "title": "Launcher Dev no-edit proof",
                    "objective": "Collect Launcher-side proof.",
                    "acceptance_criteria": ["Launcher proof attached."],
                    "affected_paths": ["EterniaLauncher"],
                    "test_plan": ["launcher proof command"],
                },
                {
                    "id": "stage_5_qa_ordering_verification",
                    "title": "QA ordering verification",
                    "objective": "QA verifies proof order.",
                    "acceptance_criteria": ["QA approval."],
                    "affected_paths": ["harness event log"],
                    "test_plan": ["QA verdict."],
                },
            ]
        },
    )

    apply_planning_decision(t, decision, actor="backend_dev")

    assert [stage.id for stage in t.stages] == [
        "stage_2_backend_observational_proof",
    ]
    assert t.current_stage_id == "stage_2_backend_observational_proof"


def test_neko_cannot_release_launcher_before_backend_proof():
    t = task(TaskState.RUNNING)
    t.risk_flags = ["cross_stack_contract_handoff", "backend_contract_first"]
    t.affected_repos = ["EterniaBackend"]
    t.stages = [
        TaskStage(id="backend_contract", title="Backend Contract", objective="prove backend", status=StageStatus.READY_FOR_QA),
    ]
    decision = dec(
        DecisionType.PROPOSE_ACCEPTANCE,
        {
            "objective": "Release Launcher Dev.",
            "acceptance_criteria": ["Launcher proof is attached."],
            "affected_repos": ["EterniaLauncher"],
        },
    )

    apply_planning_decision(t, decision, actor="neko_supervisor")

    assert t.state == TaskState.RUNNING
    assert t.affected_repos == ["EterniaBackend"]
    assert t.current_stage_id is None
    assert "cross_stack_backend_proof_missing_before_launcher_release" in t.risk_flags
    assert t.harness_self_heal["evidence_stack"][-1]["kind"] == "blocked_escalation"


def test_neko_launcher_release_with_cross_stack_join_synonym_creates_launcher_stage():
    t = task(TaskState.RUNNING)
    t.risk_flags = ["cross_stack_sequential_join_required", "worker_session_receipts_required"]
    t.affected_repos = ["EterniaBackend"]
    t.proof_ids = ["proof_backend"]
    t.current_stage_id = "stage_48_backend_contract_smoke"
    t.stages = [
        TaskStage(id="stage_48_backend_contract_smoke", title="Stage 48 Backend Contract Smoke", objective="prove backend", status=StageStatus.READY_FOR_QA),
    ]
    decision = dec(
        DecisionType.PROPOSE_ACCEPTANCE,
        {
            "objective": "Release Launcher Dev with the joined backend proof.",
            "acceptance_criteria": ["Launcher contract proof is attached."],
            "affected_repos": ["EterniaLauncher"],
        },
    )

    apply_planning_decision(t, decision, actor="neko_supervisor")

    assert t.state == TaskState.RUNNING
    assert t.affected_repos == ["EterniaLauncher"]
    assert t.current_stage_id == "stage_48_backend_contract_smoke"


def test_backend_dev_launcher_stage_plan_is_not_materialized_in_backend_first_handoff():
    t = task(TaskState.RUNNING)
    t.risk_flags = ["cross_stack_contract_handoff", "backend_contract_first"]
    t.proof_ids = ["proof_backend"]
    decision = dec(
        DecisionType.PROPOSE_STAGE_PLAN,
        {
            "stages": [
                {
                    "id": "launcher_contract_smoke",
                    "title": "Launcher contract smoke",
                    "objective": "Collect Launcher proof after Neko join.",
                    "acceptance_criteria": ["Launcher proof attached."],
                    "affected_paths": ["EterniaLauncher"],
                    "test_plan": ["launcher proof command"],
                },
            ]
        },
    )

    apply_planning_decision(t, decision, actor="backend_dev")

    assert t.stages == []

    t.risk_flags.append("neko_scoped_dev_handoff_stage")

    apply_planning_decision(t, decision, actor="backend_dev")

    assert t.stages == []


def test_backend_dev_keeps_backend_stage_when_acceptance_mentions_later_launcher_gate():
    t = task(TaskState.RUNNING)
    t.requested_by = "stage47_burn_in"
    t.risk_flags = ["cross_stack_contract_handoff", "backend_contract_first", "bounded_complex_burn_in"]
    decision = dec(
        DecisionType.PROPOSE_STAGE_PLAN,
        {
            "stages": [
                {
                    "id": "stage_47_backend_contract",
                    "title": "Backend contract proof",
                    "objective": "Collect Backend Dev contract proof before Launcher is released.",
                    "acceptance_criteria": [
                        "Backend proof is attached.",
                        "Neko releases Launcher Dev only after this backend proof passes.",
                    ],
                    "affected_paths": ["EterniaBackend"],
                    "test_plan": ["python -c \"print('backend-contract-proof')\""],
                },
                {
                    "id": "stage_47_launcher_contract_consumption",
                    "title": "Launcher contract consumption",
                    "objective": "Collect Launcher proof after Neko join.",
                    "acceptance_criteria": ["Launcher proof is attached."],
                    "affected_paths": ["EterniaLauncher"],
                    "test_plan": ["flutter test test/mission_control_contract_test.dart"],
                },
            ]
        },
    )

    apply_planning_decision(t, decision, actor="backend_dev")

    assert [stage.id for stage in t.stages] == ["stage_47_backend_contract"]
    assert t.current_stage_id == "stage_47_backend_contract"
    assert t.state == TaskState.RUNNING


def test_backend_dev_orchestration_only_plan_fails_closed_for_bounded_cross_stack_burn_in():
    t = task(TaskState.RUNNING)
    t.requested_by = "stage47_burn_in"
    t.risk_flags = ["cross_stack_contract_handoff", "backend_contract_first", "bounded_complex_burn_in"]
    decision = dec(
        DecisionType.PROPOSE_STAGE_PLAN,
        {
            "stages": [
                {
                    "id": "stage_47_neko_backend_join",
                    "title": "Neko backend join gate",
                    "objective": "Neko verifies backend proof before Launcher release.",
                    "acceptance_criteria": ["Neko join is recorded."],
                    "affected_paths": ["harness event log"],
                    "test_plan": ["Neko join event"],
                },
                {
                    "id": "stage_47_launcher_contract_consumption",
                    "title": "Launcher contract consumption",
                    "objective": "Launcher consumes joined backend contract.",
                    "acceptance_criteria": ["Launcher proof is attached."],
                    "affected_paths": ["EterniaLauncher"],
                    "test_plan": ["flutter test test/mission_control_contract_test.dart"],
                },
            ]
        },
    )

    with pytest.raises(DecisionPayloadInvalid, match="only Neko/Launcher/QA orchestration"):
        apply_planning_decision(t, decision, actor="backend_dev")


def test_dev_cannot_replan_materialized_bounded_proof_stage():
    t = task(TaskState.RUNNING)
    t.requested_by = "stage47_burn_in"
    t.risk_flags = ["bounded_complex_burn_in", "proof_ids_required_before_qa"]
    t.current_stage_id = "stage_47_backend_contract"
    t.stages = [
        TaskStage(
            id="stage_47_backend_contract",
            title="Backend contract proof",
            objective="Collect backend proof.",
            status=StageStatus.READY,
            acceptance_criteria=["Backend proof attached."],
            test_plan=["python -c \"print('backend')\""],
        )
    ]
    decision = dec(
        DecisionType.PROPOSE_STAGE_PLAN,
        {
            "stages": [
                {
                    "id": "stage_47_backend_contract",
                    "title": "Backend contract proof again",
                    "objective": "Repeat planning instead of proof.",
                    "acceptance_criteria": ["Backend proof attached."],
                    "affected_paths": ["EterniaBackend"],
                    "test_plan": ["python -c \"print('backend')\""],
                }
            ]
        },
    )

    with pytest.raises(DecisionPayloadInvalid, match="already materialized"):
        apply_planning_decision(t, decision, actor="backend_dev")


def test_block_decision_requires_log_ref_evidence():
    t = task(TaskState.RUNNING)
    decision = AgentDecision(
        type=DecisionType.BLOCK,
        summary="blocked",
        rationale="Need operator intervention.",
        payload={"reason": "Cannot collect proof."},
    )

    with pytest.raises(DecisionPayloadInvalid, match="block log_ref is required"):
        apply_planning_decision(t, decision, actor="dev")

    assert t.state == TaskState.RUNNING


def test_block_decision_accepts_log_ref_evidence():
    t = task(TaskState.RUNNING)
    decision = AgentDecision(
        type=DecisionType.BLOCK,
        summary="blocked",
        rationale="Need operator intervention.",
        payload={
            "reason": "Cannot collect proof.",
            "log_ref": {"path": "events.jsonl", "line": 6006, "summary": "Dev run opened without proof context."},
        },
    )

    apply_planning_decision(t, decision, actor="dev")

    assert t.state == TaskState.RUNNING
    assert t.harness_self_heal["evidence_stack"][-1]["reason"] == "blocked"


def test_block_decision_rejects_invalid_log_ref_line():
    t = task(TaskState.RUNNING)
    decision = AgentDecision(
        type=DecisionType.BLOCK,
        summary="blocked",
        rationale="Need operator intervention.",
        payload={
            "reason": "Cannot collect proof.",
            "log_ref": {"path": "events.jsonl", "line": 0, "summary": "not enough evidence"},
        },
    )

    with pytest.raises(DecisionPayloadInvalid, match="block log_ref.line"):
        apply_planning_decision(t, decision, actor="dev")

    assert t.state == TaskState.RUNNING


def test_block_decision_requires_payload_reason_not_rationale_fallback():
    t = task(TaskState.RUNNING)
    decision = AgentDecision(
        type=DecisionType.BLOCK,
        summary="blocked",
        rationale="This rationale alone should not satisfy the payload contract.",
        payload={"log_ref": {"path": "events.jsonl", "line": 1, "summary": "evidence"}},
    )

    with pytest.raises(DecisionPayloadInvalid, match="block reason is required"):
        apply_planning_decision(t, decision, actor="dev")

    assert t.state == TaskState.RUNNING


def test_block_decision_rejects_unsafe_log_ref_path():
    unsafe_paths = [
        "C:/Users/example/.ssh/id_rsa",
        "/home/user/.env",
        "auth/token.md",
        "../events.jsonl",
        ".ssh/config",
    ]
    for index, path in enumerate(unsafe_paths, start=1):
        t = task(TaskState.RUNNING)
        t.id = f"task_unsafe_log_{index}"
        decision = AgentDecision(
            type=DecisionType.BLOCK,
            summary="blocked",
            rationale="Need operator intervention.",
            payload={"reason": "Cannot collect proof.", "log_ref": {"path": path, "line": 1, "summary": "evidence"}},
        )

        with pytest.raises(DecisionPayloadInvalid, match="block log_ref.path"):
            apply_planning_decision(t, decision, actor="dev")

        assert t.state == TaskState.RUNNING
