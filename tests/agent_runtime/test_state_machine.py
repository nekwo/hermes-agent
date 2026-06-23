from types import SimpleNamespace

from hermes_time import now

from agent_runtime.actions import HarnessActionType
from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.events import EventLog
from agent_runtime.models import Event
from agent_runtime.models import MissionIntent, MissionPlan, MissionPlanStage, Task, TaskStage
from agent_runtime.proof_rules import ProofType
from agent_runtime.runtime_config import MissionPlanConfig, RuntimeConfig
from agent_runtime.recovery_flags import mark_block_recovery_attempt, mark_incident_closed_for_recovery
from agent_runtime.state_machine import MissionStateMachine
from agent_runtime.states import StageStatus, TaskState


def make_mission(state=TaskState.CREATED):
    ts = now()
    return Task(
        id="mission_1",
        title="Mission",
        description="d",
        state=state,
        created_at=ts,
        updated_at=ts,
        requested_by="human",
    )


def typed_config():
    return RuntimeConfig(mission_plan=MissionPlanConfig(enabled=True))


def make_typed_cross_stack_mission():
    mission = make_mission(TaskState.READY_FOR_VERIFICATION)
    mission.title = "Fix Mission Control live terminals"
    mission.description = "Backend stream seed first, then Launcher UI repair, then QA."
    mission.mission_plan = MissionPlan(
        mission_intent=MissionIntent(
            title=mission.title,
            objective=mission.description,
            acceptance_criteria=["All role streams render."],
            source_task_id=mission.id,
        ),
        current_stage_id="backend_contract_smoke",
        stages=[
            MissionPlanStage(
                id="backend_contract_smoke",
                title="Backend Contract Smoke",
                objective="Emit backend stream seed proof.",
                owner="backend_dev",
                repo="EterniaBackend",
                kind="proof_only",
                status=StageStatus.READY_FOR_QA,
                proof_recipe_id="backend_contract_smoke",
                proof_ids=["proof_backend"],
                blocks_qa_until=True,
            ),
            MissionPlanStage(
                id="launcher_implementation",
                title="Launcher Implementation",
                objective="Repair Launcher terminal streams.",
                owner="dev",
                repo="EterniaLauncher",
                kind="implementation",
                status=StageStatus.READY,
                depends_on=["backend_contract_smoke"],
                requires_product_edit=True,
                requires_visual_proof=True,
                blocks_qa_until=True,
            ),
            MissionPlanStage(
                id="qa_release",
                title="QA Release",
                objective="Verify full typed plan.",
                owner="qa",
                repo="EterniaLauncher",
                kind="qa_verdict",
                depends_on=["backend_contract_smoke", "launcher_implementation"],
                blocks_qa_until=False,
            ),
        ],
    )
    mission.current_stage_id = "backend_contract_smoke"
    return mission


def test_state_machine_selects_neko_lead_dev_qa_actions_with_mission_language():
    machine = MissionStateMachine()

    assert machine.next_action(make_mission(TaskState.CREATED)).type == HarnessActionType.RUN_SLOT
    assert machine.next_action(make_mission(TaskState.READY_FOR_IMPLEMENTATION)).type == HarnessActionType.RUN_SLOT
    assert machine.next_action(make_mission(TaskState.QA_REVIEW_PLAN)).type == HarnessActionType.RUN_SLOT
    assert machine.next_action(make_mission(TaskState.DEV_IMPLEMENTING)).type == HarnessActionType.RUN_SLOT
    dev_ready = make_mission(TaskState.READY_FOR_VERIFICATION)
    assert machine.next_action(dev_ready).type == HarnessActionType.RUN_SLOT
    dev_ready.risk_flags = ["neko_qa_coordination_released"]
    assert machine.next_action(dev_ready).type == HarnessActionType.RUN_SLOT
    assert machine.next_action(make_mission(TaskState.QA_TESTING)).type == HarnessActionType.RUN_SLOT
    assert machine.next_action(make_mission(TaskState.VERIFIED)).type == HarnessActionType.COMPLETE_TASK
    assert machine.next_action(make_mission(TaskState.PROOF_REVIEW)).type == HarnessActionType.COMPLETE_TASK


def test_open_incident_routes_neko_even_when_task_not_blocked():
    mission = make_mission(TaskState.DEV_IMPLEMENTING)
    mission.open_incident_ids = ["inc_loop"]

    action = MissionStateMachine(config=typed_config()).next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert "open incidents" in action.reason


def test_legacy_qa_stage_does_not_count_as_remaining_dev_work():
    mission = make_mission(TaskState.READY_FOR_VERIFICATION)
    mission.risk_flags = ["neko_qa_coordination_released"]
    mission.current_stage_id = "backend_implementation"
    mission.proof_ids = ["proof_backend"]
    mission.stages = [
        TaskStage(
            id="backend_implementation",
            title="Backend Implementation",
            objective="Patch backend media safety.",
            status=StageStatus.READY_FOR_QA,
            affected_paths=["media/models.py"],
            test_plan=["python manage.py test media.tests.FinalizeUploadTests"],
        ),
        TaskStage(
            id="qa_release",
            title="QA Release Verdict",
            objective="Verify typed plan.",
            status=StageStatus.IMPLEMENTING,
        ),
    ]

    action = MissionStateMachine(config=typed_config()).next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT


def test_blocked_open_incident_is_settled_not_repeated_neko_loop():
    mission = make_mission(TaskState.BLOCKED)
    mission.open_incident_ids = ["inc_loop"]
    mark_block_recovery_attempt(mission)

    action = MissionStateMachine(config=typed_config()).next_action(mission)

    assert action.type == HarnessActionType.NOOP
    assert "open incidents" in action.reason


def test_typed_NEEDS_FIXES_routes_back_to_dev_not_qa_loop():
    mission = make_mission(TaskState.NEEDS_FIXES)
    mission.current_stage_id = "launcher_implementation"
    mission.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title="Mission Control", objective="Patch UI"),
        current_stage_id="launcher_implementation",
        stages=[
            MissionPlanStage(
                id="launcher_implementation",
                title="Launcher Implementation",
                objective="Patch Mission Control",
                owner="dev",
                repo="EterniaLauncher",
                kind="implementation",
                status=StageStatus.READY_FOR_QA,
                requires_product_edit=True,
                blocks_qa_until=True,
                proof_ids=["proof_passed"],
            ),
            MissionPlanStage(
                id="qa_release",
                title="QA",
                objective="Verify",
                owner="qa",
                repo="EterniaLauncher",
                kind="qa_verdict",
                status=StageStatus.READY,
                depends_on=["launcher_implementation"],
                blocks_qa_until=False,
            ),
        ],
    )

    action = MissionStateMachine(config=typed_config()).next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert "QA requested fixes" in action.reason


def test_typed_plan_backend_ready_routes_neko_before_launcher_not_qa():
    mission = make_typed_cross_stack_mission()

    action = MissionStateMachine(config=typed_config()).next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert "release" in action.reason


def test_typed_plan_ready_proof_stage_releases_directly_to_qa():
    mission = make_mission(TaskState.DEV_IMPLEMENTING)
    mission.current_stage_id = "harness_runtime_status_snapshot"
    mission.proof_ids = ["proof_status_snapshot"]
    mission.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title="Harness thinking log smoke", objective="Verify Harness logs."),
        current_stage_id="harness_runtime_status_snapshot",
        stages=[
            MissionPlanStage(
                id="harness_runtime_status_snapshot",
                title="Harness Runtime Status Snapshot",
                objective="Collect Harness status/snapshot proof.",
                owner="dev",
                repo="hermes-agent",
                kind="proof_only",
                status=StageStatus.READY_FOR_QA,
                proof_recipe_id="harness_runtime_status_snapshot",
                proof_ids=["proof_status_snapshot"],
                blocks_qa_until=True,
            ),
            MissionPlanStage(
                id="qa_release",
                title="QA Release",
                objective="Review proof.",
                owner="qa",
                repo="hermes-agent",
                kind="qa_verdict",
                depends_on=["harness_runtime_status_snapshot"],
                blocks_qa_until=False,
            ),
        ],
    )

    proof_store = SimpleNamespace(
        get=lambda proof_id: SimpleNamespace(
            id=proof_id,
            task_id=mission.id,
            stage_id="harness_runtime_status_snapshot",
            type=ProofType.TEST_RUN,
            metadata={"status": "passed", "proof_recipe_id": "harness_runtime_status_snapshot", "exit_code": 0},
        )
    )

    action = MissionStateMachine(config=typed_config(), proof_store=proof_store).next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert mission.current_stage_id == "qa_release"
    assert mission.mission_plan.current_stage_id == "qa_release"


def test_typed_plan_never_completes_from_single_backend_substage():
    mission = make_typed_cross_stack_mission()
    mission.state = TaskState.VERIFIED

    action = MissionStateMachine(config=typed_config()).next_action(mission)

    assert action.type != HarnessActionType.COMPLETE_TASK
    assert action.type == HarnessActionType.RUN_SLOT


def test_typed_plan_released_launcher_routes_launcher_dev():
    mission = make_typed_cross_stack_mission()
    mission.mission_plan.current_stage_id = "launcher_implementation"
    mission.current_stage_id = "launcher_implementation"
    mission.state = TaskState.DEV_IMPLEMENTING

    action = MissionStateMachine(config=typed_config()).next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert "launcher_implementation" in action.reason


def test_typed_no_edit_investigation_with_repeated_fulfilled_context_routes_to_dev_delivery():
    mission = make_mission(TaskState.DEV_IMPLEMENTING)
    mission.current_stage_id = "backend_investigation"
    mission.context_requests = [
        {
            "id": "ctx_1",
            "actor": "backend_dev",
            "stage_id": "backend_investigation",
            "status": "fulfilled",
            "reason": "backend_investigation needs first code map",
        },
        {
            "id": "ctx_2",
            "actor": "backend_dev",
            "stage_id": "backend_investigation",
            "status": "fulfilled_partial",
            "reason": "backend_investigation needs filter details",
        },
    ]
    mission.mission_plan = MissionPlan(
        mission_intent=MissionIntent(
            title="Investigate NSFW leakage",
            objective="No-product-edit backend investigation.",
        ),
        current_stage_id="backend_investigation",
        stages=[
            MissionPlanStage(
                id="backend_investigation",
                title="Backend Investigation",
                objective="No-product-edit investigation of NSFW filter leakage.",
                owner="backend_dev",
                repo="EterniaBackend",
                kind="investigation",
                status=StageStatus.READY,
                blocks_qa_until=True,
            ),
            MissionPlanStage(
                id="qa_release",
                title="QA Release",
                objective="Review investigation proof.",
                owner="qa",
                repo="EterniaBackend",
                kind="qa_verdict",
                depends_on=["backend_investigation"],
                blocks_qa_until=False,
            ),
        ],
    )

    action = MissionStateMachine(config=typed_config()).next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert "must deliver findings or block" in action.reason


def test_typed_no_edit_investigation_repeated_legacy_context_routes_to_dev_delivery():
    mission = make_mission(TaskState.DEV_IMPLEMENTING)
    mission.current_stage_id = "backend_investigation"
    mission.context_requests = [
        {
            "id": "ctx_1",
            "actor": "backend_dev",
            "stage_id": "backend_investigation",
            "status": "fulfilled",
            "reason": "backend_investigation needs first code map",
        },
        {
            "id": "ctx_2",
            "actor": "backend_dev",
            "stage_id": "backend_investigation",
            "status": "fulfilled",
            "reason": "backend_investigation needs filter details",
        },
    ]
    mission.mission_plan = MissionPlan(
        mission_intent=MissionIntent(
            title="Investigate NSFW leakage",
            objective="No-product-edit backend investigation.",
        ),
        current_stage_id="backend_investigation",
        stages=[
            MissionPlanStage(
                id="backend_investigation",
                title="Backend Investigation",
                objective="No-product-edit investigation of NSFW filter leakage.",
                owner="backend_dev",
                repo="EterniaBackend",
                kind="investigation",
                status=StageStatus.READY,
                blocks_qa_until=True,
            )
        ],
    )
    action = MissionStateMachine(config=typed_config()).next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert "must deliver findings or block" in action.reason


def test_typed_no_edit_investigation_allows_one_context_bundle_before_neko_boundary():
    mission = make_mission(TaskState.DEV_IMPLEMENTING)
    mission.current_stage_id = "backend_investigation"
    mission.context_requests = [
        {
            "id": "ctx_1",
            "actor": "backend_dev",
            "stage_id": "backend_investigation",
            "status": "fulfilled",
            "reason": "backend_investigation needs first code map",
        }
    ]
    mission.mission_plan = MissionPlan(
        mission_intent=MissionIntent(
            title="Investigate NSFW leakage",
            objective="No-product-edit backend investigation.",
        ),
        current_stage_id="backend_investigation",
        stages=[
            MissionPlanStage(
                id="backend_investigation",
                title="Backend Investigation",
                objective="No-product-edit investigation of NSFW filter leakage.",
                owner="backend_dev",
                repo="EterniaBackend",
                kind="investigation",
                status=StageStatus.READY,
                blocks_qa_until=True,
            )
        ],
    )

    action = MissionStateMachine(config=typed_config()).next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert "backend_investigation" in action.reason


def test_typed_implementation_stage_not_blocked_by_repeated_context_bundles():
    mission = make_mission(TaskState.DEV_IMPLEMENTING)
    mission.current_stage_id = "launcher_implementation"
    mission.context_requests = [
        {
            "id": "ctx_1",
            "actor": "dev",
            "stage_id": "launcher_implementation",
            "status": "fulfilled",
            "reason": "launcher_implementation needs view code",
        },
        {
            "id": "ctx_2",
            "actor": "dev",
            "stage_id": "launcher_implementation",
            "status": "fulfilled",
            "reason": "launcher_implementation needs bridge code",
        },
    ]
    mission.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title="Patch Mission Control", objective="Product implementation."),
        current_stage_id="launcher_implementation",
        stages=[
            MissionPlanStage(
                id="launcher_implementation",
                title="Launcher Implementation",
                objective="Patch Mission Control agent terminal UI.",
                owner="dev",
                repo="EterniaLauncher",
                kind="implementation",
                status=StageStatus.READY,
                requires_product_edit=True,
                blocks_qa_until=True,
            )
        ],
    )

    action = MissionStateMachine(config=typed_config()).next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert "launcher_implementation" in action.reason


def test_state_machine_retries_qa_after_resolved_incident_only_blocker():
    mission = make_mission(TaskState.BLOCKED)
    mission.open_incident_ids = []
    mission.proof_ids = ["proof_qa_blocked"]
    proof_store = SimpleNamespace(
        get=lambda proof_id: SimpleNamespace(
            type="qa_verdict",
            metadata={
                "verdict": "blocked",
                "findings": [
                    {"kind": "open_incidents", "severity": "blocking", "summary": "stale incident ids"},
                    {"kind": "proof_review", "severity": "info", "summary": "proof looked good"},
                ],
            },
        )
    )

    action = MissionStateMachine(proof_store=proof_store).next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "dev"
    assert "QA blocked verdict" in action.reason


def test_state_machine_retries_qa_after_resolved_qa_output_incident_without_neko_loop():
    mission = make_mission(TaskState.BLOCKED)
    mission.open_incident_ids = []
    mission.risk_flags = ["neko_qa_coordination_released"]
    mission.proof_ids = ["proof_backend", "proof_launcher"]
    mission.current_stage_id = "launcher_contract_smoke"
    mission.stages = [
        TaskStage(id="backend_contract", title="Backend Contract", objective="prove backend", status=StageStatus.READY_FOR_QA),
        TaskStage(id="launcher_contract_smoke", title="Launcher Contract", objective="prove launcher", status=StageStatus.READY_FOR_QA),
    ]

    action = MissionStateMachine().next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "neko_supervisor"
    assert "blueprint intervention" in action.reason


def test_state_machine_routes_blocked_no_incident_to_one_neko_recovery_pass():
    mission = make_mission(TaskState.BLOCKED)

    action = MissionStateMachine().next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "neko_supervisor"
    assert "blueprint intervention" in action.reason

    mark_block_recovery_attempt(mission)
    after_attempt = MissionStateMachine().next_action(mission)
    assert after_attempt.type == HarnessActionType.NOOP

    mark_incident_closed_for_recovery(mission, incident_id="inc_new")
    after_incident_close = MissionStateMachine().next_action(mission)
    assert after_incident_close.type == HarnessActionType.RUN_SLOT


def test_blocked_task_with_pending_launcher_handoff_packet_resumes_dev_after_neko_wait():
    mission = make_mission(TaskState.BLOCKED)
    mission.current_stage_id = "backend_contract"
    mission.proof_ids = ["proof_backend"]
    mission.stages = [
        TaskStage(id="backend_contract", title="Backend Contract", objective="prove backend", status=StageStatus.READY_FOR_QA),
    ]
    mark_block_recovery_attempt(mission)
    EventLog().append(
        Event(
            now(),
            "packet.recorded",
            mission.id,
            "run_neko",
            "neko_supervisor",
            {
                "packet_type": "handoff_packet",
                "stage_id": "backend_contract",
                "body": {
                    "packet_kind": "contract_join",
                    "mission_phase": "launcher_handoff",
                    "handoff_mode": "sequential_specialists",
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
        )
    )

    action = MissionStateMachine().next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "neko_supervisor"
    assert "blueprint intervention" in action.reason
    assert mission.current_stage_id == "backend_contract"


def test_implementing_task_with_pending_launcher_handoff_realigns_stage_before_dev():
    mission = make_mission(TaskState.DEV_IMPLEMENTING)
    mission.risk_flags = ["cross_stack_contract_handoff", "backend_contract_first"]
    mission.current_stage_id = "backend_contract"
    mission.proof_ids = ["proof_backend"]
    mission.stages = [
        TaskStage(id="backend_contract", title="Backend Contract", objective="prove backend", status=StageStatus.READY_FOR_QA),
    ]
    EventLog().append(
        Event(
            now(),
            "packet.recorded",
            mission.id,
            "run_neko",
            "neko_supervisor",
            {
                "packet_type": "handoff_packet",
                "stage_id": "backend_contract",
                "body": {
                    "packet_kind": "contract_join",
                    "mission_phase": "launcher_handoff",
                    "handoff_mode": "sequential_specialists",
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
        )
    )

    action = MissionStateMachine().next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "backend_dev"
    assert "backend_contract" in action.reason
    assert mission.current_stage_id == "backend_contract"


def test_implementing_task_with_premature_launcher_handoff_routes_to_neko_without_backend_proof():
    mission = make_mission(TaskState.DEV_IMPLEMENTING)
    mission.risk_flags = ["cross_stack_contract_handoff", "backend_contract_first"]
    mission.current_stage_id = "backend_contract"
    mission.stages = [
        TaskStage(id="backend_contract", title="Backend Contract", objective="prove backend", status=StageStatus.IMPLEMENTING),
    ]
    EventLog().append(
        Event(
            now(),
            "packet.recorded",
            mission.id,
            "run_neko",
            "neko_supervisor",
            {
                "packet_type": "handoff_packet",
                "stage_id": "backend_contract",
                "body": {
                    "packet_kind": "contract_join",
                    "mission_phase": "launcher_handoff",
                    "handoff_mode": "sequential_specialists",
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
        )
    )

    action = MissionStateMachine().next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "backend_dev"
    assert "backend_contract" in action.reason
    assert mission.current_stage_id == "backend_contract"


def test_blocked_launcher_stage_without_backend_proof_routes_to_neko_not_dev():
    mission = make_mission(TaskState.BLOCKED)
    mission.risk_flags = ["cross_stack_contract_handoff", "backend_contract_first"]
    mission.current_stage_id = "launcher_contract_smoke"
    mission.stages = [
        TaskStage(id="launcher_contract_smoke", title="Launcher Contract Smoke", objective="prove launcher", status=StageStatus.BLOCKED),
    ]
    mark_block_recovery_attempt(mission)
    EventLog().append(
        Event(
            now(),
            "packet.recorded",
            mission.id,
            "run_neko",
            "neko_supervisor",
            {
                "packet_type": "handoff_packet",
                "stage_id": None,
                "body": {
                    "packet_kind": "fresh_scope",
                    "mission_phase": "initial_scope",
                    "handoff_mode": "backend_first_cross_stack",
                    "target_owner": "backend_dev",
                    "target_repo": "EterniaBackend",
                    "proof_gate": {"required": True, "minimum_status": "passed"},
                },
            },
        )
    )

    action = MissionStateMachine().next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "neko_supervisor"
    assert "blueprint intervention" in action.reason
    assert mission.current_stage_id == "launcher_contract_smoke"


def test_implementing_launcher_stage_without_neko_release_routes_to_neko():
    mission = make_mission(TaskState.DEV_IMPLEMENTING)
    mission.risk_flags = ["cross_stack_contract_handoff", "backend_contract_first"]
    mission.proof_ids = ["proof_backend"]
    mission.current_stage_id = "launcher_contract_smoke"
    mission.stages = [
        TaskStage(id="backend_contract", title="Backend Contract", objective="prove backend", status=StageStatus.READY_FOR_QA),
        TaskStage(id="launcher_contract_smoke", title="Launcher Contract Smoke", objective="prove launcher", status=StageStatus.IMPLEMENTING),
    ]

    action = MissionStateMachine().next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "dev"
    assert "launcher_contract_smoke" in action.reason


def test_implementing_launcher_stage_after_neko_release_routes_to_dev():
    mission = make_mission(TaskState.DEV_IMPLEMENTING)
    mission.risk_flags = ["cross_stack_contract_handoff", "backend_contract_first", "launcher_contract_released_by_neko"]
    mission.proof_ids = ["proof_backend"]
    mission.current_stage_id = "launcher_contract_smoke"
    mission.stages = [
        TaskStage(id="backend_contract", title="Backend Contract", objective="prove backend", status=StageStatus.READY_FOR_QA),
        TaskStage(id="launcher_contract_smoke", title="Launcher Contract Smoke", objective="prove launcher", status=StageStatus.IMPLEMENTING),
    ]

    action = MissionStateMachine().next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "dev"
    assert "launcher_contract_smoke" in action.reason


def test_dev_ready_cross_stack_sequential_join_synonym_routes_to_neko_before_qa():
    mission = make_mission(TaskState.READY_FOR_VERIFICATION)
    mission.risk_flags = ["cross_stack_sequential_join_required", "worker_session_receipts_required"]
    mission.proof_ids = ["proof_backend"]
    mission.current_stage_id = "stage_48_backend_contract_smoke"
    mission.stages = [
        TaskStage(
            id="stage_48_backend_contract_smoke",
            title="Stage 48 Backend Contract Smoke",
            objective="prove backend",
            status=StageStatus.READY_FOR_QA,
        ),
    ]

    action = MissionStateMachine().next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "backend_dev"
    assert "stage_48_backend_contract_smoke" in action.reason


def test_blocked_current_stage_failed_command_proof_routes_to_dev_retry():
    mission = make_mission(TaskState.BLOCKED)
    mission.current_stage_id = "backend_observational_proof"
    mission.proof_ids = ["proof_failed"]
    proof_store = SimpleNamespace(
        get=lambda proof_id: SimpleNamespace(
            type="test_run",
            stage_id="backend_observational_proof",
            metadata={"status": "failed", "exit_code": 1},
        )
    )

    action = MissionStateMachine(proof_store=proof_store).next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "neko_supervisor"
    assert "blueprint intervention" in action.reason


def test_state_machine_closes_VERIFIED_mission_with_existing_proof_without_pm_model_loop():
    mission = make_mission(TaskState.VERIFIED)
    mission.proof_ids = ["proof_test", "proof_qa"]

    action = MissionStateMachine().next_action(mission)

    assert action.type == HarnessActionType.COMPLETE_TASK
    assert "no remaining stages" in action.reason


def test_cross_stack_backend_only_qa_state_routes_to_neko_launcher_release():
    mission = make_mission(TaskState.VERIFIED)
    mission.risk_flags = ["cross_stack_contract_handoff"]
    mission.proof_ids = ["proof_backend", "proof_qa"]
    mission.stages = [
        TaskStage(id="backend_contract", title="Backend Contract", objective="prove backend", status=StageStatus.PASSED),
    ]

    action = MissionStateMachine().next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "backend_dev"
    assert "backend_contract" in action.reason


def test_cross_stack_backend_only_dev_ready_routes_to_neko_before_qa():
    mission = make_mission(TaskState.READY_FOR_VERIFICATION)
    mission.risk_flags = ["cross_stack_contract_handoff", "neko_qa_coordination_released"]
    mission.proof_ids = ["proof_backend"]
    mission.stages = [
        TaskStage(id="backend_contract", title="Backend Contract", objective="prove backend", status=StageStatus.READY_FOR_QA),
    ]

    action = MissionStateMachine().next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "backend_dev"
    assert "backend_contract" in action.reason


def test_cross_stack_contract_join_flag_routes_to_neko_before_qa():
    mission = make_mission(TaskState.READY_FOR_VERIFICATION)
    mission.risk_flags = ["cross_stack_contract_join", "neko_qa_coordination_released"]
    mission.proof_ids = ["proof_backend"]
    mission.stages = [
        TaskStage(id="backend_contract", title="Backend Contract", objective="prove backend", status=StageStatus.READY_FOR_QA),
    ]

    action = MissionStateMachine().next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "backend_dev"
    assert "backend_contract" in action.reason


def test_backend_stage_that_mentions_future_launcher_gate_still_requires_launcher_release():
    mission = make_mission(TaskState.READY_FOR_VERIFICATION)
    mission.risk_flags = ["cross_stack_contract_handoff", "sequential_specialist_handoff"]
    mission.proof_ids = ["proof_backend"]
    mission.stages = [
        TaskStage(
            id="backend_contract_smoke",
            title="Backend Contract Smoke",
            objective="Collect deterministic backend command proof.",
            status=StageStatus.READY_FOR_QA,
            acceptance_criteria=[
                "Launcher Dev is released only after backend proof IDs and backend contract/proof packet exist.",
                "QA is released only after both backend and Launcher proof sets exist.",
            ],
            test_plan=[
                "Confirm backend worktree is clean while allowing a literal launcher/ path prefix in the dirty-path filter.",
                "Confirm docker-compose paths are not required for the final QA route until Backend Dev asks for them.",
            ],
        ),
    ]

    action = MissionStateMachine().next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "qa"
    assert "backend_contract_smoke" in action.reason


def test_text_only_backend_first_live_terminal_goal_routes_to_neko_before_qa():
    mission = make_mission(TaskState.READY_FOR_VERIFICATION)
    mission.title = "Fix Mission Control all-role live terminals"
    mission.description = "Seed and prove Backend Dev live terminal/event stream artifacts without backend product edits, using only the existing no-product-edit backend_contract_smoke proof recipe."
    mission.acceptance_criteria = [
        "Backend Dev requests the existing backend_contract_smoke proof recipe only.",
        "Neko joins Backend proof and releases Launcher Dev UI/bridge repair before QA.",
    ]
    mission.risk_flags = [
        "Cross-stack live event rendering depends on backend proof artifacts being joined before Launcher Dev UI/bridge repair.",
    ]
    mission.proof_ids = ["test_task_backend_contract_smoke_proof"]
    mission.stages = [
        TaskStage(id="eterniabackend_fresh_scope", title="Backend Dev Fresh Scope", objective="Collect backend proof.", status=StageStatus.READY_FOR_QA),
    ]

    action = MissionStateMachine().next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "backend_dev"
    assert "eterniabackend_fresh_scope" in action.reason


def test_state_machine_requires_visual_proof_before_terminal_close_when_requested():
    mission = make_mission(TaskState.VERIFIED)
    mission.requires_visual_proof = True
    mission.proof_ids = ["proof_backend", "proof_qa"]

    action = MissionStateMachine().next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert "visual proof" in action.reason


def test_state_machine_applies_neko_mission_lead_decision_through_transition_authority():
    mission = make_mission(TaskState.CREATED)
    decision = AgentDecision(
        type=DecisionType.PROPOSE_ACCEPTANCE,
        summary="scoped",
        rationale="ready",
        payload={"objective": "obj", "acceptance_criteria": ["ok"]},
    )

    result = MissionStateMachine().apply_decision(mission, decision, actor="neko_supervisor")

    assert result.from_state == TaskState.CREATED
    assert result.to_state == TaskState.READY_FOR_IMPLEMENTATION
    assert mission.state == TaskState.READY_FOR_IMPLEMENTATION
    assert mission.acceptance_criteria == ["ok"]
    assert result.events
    assert result.events[0].payload["actor"] == "neko_supervisor"


def test_neko_scoped_launcher_fix_with_harness_support_scope_routes_to_dev():
    mission = make_mission(TaskState.READY_FOR_IMPLEMENTATION)
    mission.title = "Fix Mission Control live terminals for all agents"
    mission.description = (
        "Launcher Dev will diagnose and implement the narrow EterniaLauncher "
        "Mission Control live-terminal bridge/UI fix."
    )
    mission.affected_repos = ["EterniaLauncher", "hermes-agent"]
    mission.suggested_roles = ["dev"]
    mission.acceptance_criteria = [
        "Identify the current Mission Control selector-to-role mapping.",
        "Implement the narrow Launcher-side bridge/UI changes.",
        "For each role selector, distinguish loading, no events, and failure states.",
        "Run focused Windows Flutter/Launcher verification.",
    ]

    action = MissionStateMachine().next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert "dev" in action.reason.lower()
