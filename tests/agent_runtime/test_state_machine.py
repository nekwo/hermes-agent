from types import SimpleNamespace

from hermes_time import now

from agent_runtime.actions import HarnessActionType
from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.default_plan import ensure_default_mission_plan
from agent_runtime.events import EventLog
from agent_runtime.models import Event
from agent_runtime.models import MissionIntent, MissionPlan, MissionPlanStage, Task, TaskStage
from agent_runtime.proof_rules import ProofType
from agent_runtime.runtime_config import MissionPlanConfig, RuntimeConfig
from agent_runtime.recovery_flags import mark_block_recovery_attempt, mark_incident_closed_for_recovery
from agent_runtime.state_machine import MissionStateMachine
from agent_runtime.store import ProofStore
from agent_runtime.states import StageStatus, TaskState
from tests.agent_runtime.conftest import release_to_implementation


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


def mark_graph_complete(mission: Task) -> Task:
    mission.current_stage_id = None
    mission.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title=mission.title, objective=mission.description),
        current_stage_id=None,
        blueprint_id="neko_dev_qa_basic",
        stages=[
            MissionPlanStage(
                id="qa_release",
                title="QA Release",
                objective="Verify mission.",
                owner="qa",
                owner_slot="qa",
                repo="hermes-agent",
                kind="qa_verdict",
                status=StageStatus.PASSED,
            )
        ],
    )
    return mission


def typed_config():
    return RuntimeConfig(mission_plan=MissionPlanConfig(enabled=True))


def make_typed_cross_stack_mission():
    mission = make_mission(TaskState.RUNNING)
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
    assert machine.next_action(make_mission(TaskState.RUNNING)).type == HarnessActionType.RUN_SLOT
    assert machine.next_action(make_mission(TaskState.RUNNING)).type == HarnessActionType.RUN_SLOT
    assert machine.next_action(make_mission(TaskState.RUNNING)).type == HarnessActionType.RUN_SLOT
    dev_ready = make_mission(TaskState.RUNNING)
    assert machine.next_action(dev_ready).type == HarnessActionType.RUN_SLOT
    dev_ready.risk_flags = []
    assert machine.next_action(dev_ready).type == HarnessActionType.RUN_SLOT
    assert machine.next_action(make_mission(TaskState.RUNNING)).type == HarnessActionType.RUN_SLOT
    assert machine.next_action(mark_graph_complete(make_mission(TaskState.RUNNING))).type == HarnessActionType.COMPLETE_TASK


def test_open_incident_routes_neko_even_when_task_not_blocked():
    mission = make_mission(TaskState.RUNNING)
    mission.open_incident_ids = ["inc_loop"]

    action = MissionStateMachine(config=typed_config()).next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert "open incidents" in action.reason


def test_launcher_only_default_plan_dispatches_launcher_not_backend_after_scope():
    mission = make_mission(TaskState.RUNNING)
    mission.title = "Launcher-only trust probe"
    mission.description = "Write the Launcher-side proof note only."
    mission.affected_repos = ["EterniaLauncher"]
    plan = ensure_default_mission_plan(mission)
    stages = {stage.id: stage for stage in plan.stages}
    stages["scope"].status = StageStatus.PASSED
    mission.current_stage_id = None
    plan.current_stage_id = None

    actions = MissionStateMachine(config=typed_config()).next_actions(mission)

    run_slots = [(action.slot_id, action.stage_id) for action in actions if action.type == HarnessActionType.RUN_SLOT]
    assert run_slots == [("dev", "implement")]


def test_running_open_incident_adjudication_is_one_pass_per_signal(isolate_agent_runtime_root):
    """A RUNNING mission with open incidents gets ONE Neko adjudication pass
    per evidence signal (observed live: a supervisor answering adjudication
    with `block` was re-dispatched every ~30-60s forever). Closing an
    incident changes the signal and re-arms recovery."""

    import uuid

    from agent_runtime.models import Incident
    from agent_runtime.store import IncidentStore

    incidents = IncidentStore()
    incident = Incident(
        id=f"inc_{uuid.uuid4().hex[:8]}",
        task_id="mission_1",
        run_id=None,
        kind="run_hung",
        summary="hung",
        detail_path=None,
        opened_at=now(),
    )
    incidents.open(incident)

    mission = make_mission(TaskState.RUNNING)
    mission.open_incident_ids = [incident.id]
    machine = MissionStateMachine(config=typed_config())

    first = machine.next_action(mission)
    assert first.type == HarnessActionType.RUN_SLOT
    assert "open incidents" in first.reason

    # The harness marks the bounded pass when it dispatches Neko.
    mark_block_recovery_attempt(mission)
    second = machine.next_action(mission)
    assert second.type == HarnessActionType.NOOP
    assert "waiting on intervention" in second.reason

    # Real progress (incident close) changes the signal and re-arms; the
    # prune also unlinks the now-closed incident so routing moves on.
    incidents.close(incident.id, reason="adjudicated")
    third = machine.next_action(mission)
    assert mission.open_incident_ids == []
    assert "open incidents" not in (third.reason or "")


def test_closed_store_incident_link_is_pruned_not_neko_looped(isolate_agent_runtime_root):
    """A stale open_incident_ids link whose incident is CLOSED in the store
    must not route Neko adjudication forever (observed live: an in-flight
    engine turn persisted a stale task copy over the operator's incident-close
    unlink). Unknown ids with no store record stay linked, fail-safe."""

    import uuid

    from agent_runtime.models import Incident
    from agent_runtime.store import IncidentStore

    incidents = IncidentStore()
    incident = Incident(
        id=f"inc_{uuid.uuid4().hex[:8]}",
        task_id="mission_1",
        run_id=None,
        kind="run_budget_exceeded",
        summary="budget",
        detail_path=None,
        opened_at=now(),
    )
    incidents.open(incident)
    incidents.close(incident.id, reason="operator recovery")

    mission = make_mission(TaskState.RUNNING)
    mission.open_incident_ids = [incident.id]

    action = MissionStateMachine(config=typed_config()).next_action(mission)

    assert mission.open_incident_ids == []
    assert "open incidents" not in (action.reason or "")


def test_legacy_qa_stage_does_not_count_as_remaining_dev_work():
    mission = make_mission(TaskState.RUNNING)
    mission.risk_flags = []
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


def test_typed_REWORK_routes_back_to_dev_not_qa_loop():
    mission = make_mission(TaskState.RUNNING)
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
                status=StageStatus.REWORK,
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
    assert "blueprint stage launcher_implementation needs slot dev" in action.reason


def test_typed_plan_backend_ready_routes_neko_before_launcher_not_qa():
    mission = make_typed_cross_stack_mission()

    action = MissionStateMachine(config=typed_config()).next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert "blueprint stage launcher_implementation needs slot dev" in action.reason


def test_typed_plan_ready_proof_stage_releases_directly_to_qa():
    mission = make_mission(TaskState.RUNNING)
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
    mission.state = TaskState.RUNNING

    action = MissionStateMachine(config=typed_config()).next_action(mission)

    assert action.type != HarnessActionType.COMPLETE_TASK
    assert action.type == HarnessActionType.RUN_SLOT


def test_typed_plan_released_launcher_routes_launcher_dev():
    mission = make_typed_cross_stack_mission()
    mission.mission_plan.current_stage_id = "launcher_implementation"
    mission.current_stage_id = "launcher_implementation"
    mission.state = TaskState.RUNNING

    action = MissionStateMachine(config=typed_config()).next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert "launcher_implementation" in action.reason


def test_typed_no_edit_investigation_with_repeated_fulfilled_context_routes_to_dev_delivery():
    mission = make_mission(TaskState.RUNNING)
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
    assert "blueprint stage backend_investigation needs slot backend_dev" in action.reason


def test_typed_no_edit_investigation_repeated_legacy_context_routes_to_dev_delivery():
    mission = make_mission(TaskState.RUNNING)
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
    assert "blueprint stage backend_investigation needs slot backend_dev" in action.reason


def test_typed_no_edit_investigation_allows_one_context_bundle_before_neko_boundary():
    mission = make_mission(TaskState.RUNNING)
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
    mission = make_mission(TaskState.RUNNING)
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
    mission.risk_flags = []
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
    mission = make_mission(TaskState.RUNNING)
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
    assert action.slot_id == "qa"
    assert "needs slot qa" in action.reason
    assert mission.current_stage_id == "verify"


def test_implementing_task_with_premature_launcher_handoff_routes_to_neko_without_backend_proof():
    mission = make_mission(TaskState.RUNNING)
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
    mission = make_mission(TaskState.RUNNING)
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
    mission = make_mission(TaskState.RUNNING)
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


def test_dev_ready_cross_stack_sequential_join_synonym_routes_to_neko_before_qa():
    mission = make_mission(TaskState.RUNNING)
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
    assert action.slot_id == "qa"
    assert "needs slot qa" in action.reason


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


def test_state_machine_closes_APPROVED_mission_with_existing_proof_without_pm_model_loop():
    mission = mark_graph_complete(make_mission(TaskState.RUNNING))
    mission.proof_ids = ["proof_test", "proof_qa"]

    action = MissionStateMachine().next_action(mission)

    assert action.type == HarnessActionType.COMPLETE_TASK
    assert "no remaining stages" in action.reason


def test_blueprint_terminal_close_reopens_unfinished_stage_instead_of_completing():
    mission = mark_graph_complete(make_mission(TaskState.RUNNING))
    mission.mission_plan.stages.append(
        MissionPlanStage(
            id="launcher_contract",
            title="Launcher Contract",
            objective="Collect launcher proof.",
            owner="dev",
            owner_slot="dev",
            repo="EterniaLauncher",
            kind="proof_only",
            status=StageStatus.IMPLEMENTING,
            proof_gate={"required": True, "minimum_status": "passed", "required_proof_types": ["test_run"]},
        )
    )

    action = MissionStateMachine(proof_store=ProofStore()).next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "dev"
    assert mission.current_stage_id == "launcher_contract"
    assert "launcher_contract" in action.reason


def test_blueprint_terminal_close_blocks_passed_stage_with_missing_required_proof():
    mission = mark_graph_complete(make_mission(TaskState.RUNNING))
    mission.mission_plan.stages[0].proof_gate = {
        "required": True,
        "minimum_status": "passed",
        "required_proof_types": ["test_run"],
    }

    action = MissionStateMachine(proof_store=ProofStore()).next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "neko_supervisor"
    assert mission.current_stage_id == "qa_release"
    assert mission.mission_plan.stages[0].status == StageStatus.BLOCKED
    assert "proof gate unsatisfied" in action.reason


def test_cross_stack_backend_only_qa_state_routes_to_neko_launcher_release():
    mission = make_mission(TaskState.RUNNING)
    mission.risk_flags = ["cross_stack_contract_handoff"]
    mission.proof_ids = ["proof_backend", "proof_qa"]
    mission.stages = [
        TaskStage(id="backend_contract", title="Backend Contract", objective="prove backend", status=StageStatus.PASSED),
    ]

    action = MissionStateMachine().next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "qa"
    assert "needs slot qa" in action.reason


def test_cross_stack_backend_only_dev_ready_routes_to_neko_before_qa():
    mission = make_mission(TaskState.RUNNING)
    mission.risk_flags = ["cross_stack_contract_handoff"]
    mission.proof_ids = ["proof_backend"]
    mission.stages = [
        TaskStage(id="backend_contract", title="Backend Contract", objective="prove backend", status=StageStatus.READY_FOR_QA),
    ]

    action = MissionStateMachine().next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "qa"
    assert "needs slot qa" in action.reason


def test_cross_stack_contract_join_flag_routes_to_neko_before_qa():
    mission = make_mission(TaskState.RUNNING)
    mission.risk_flags = ["cross_stack_contract_join"]
    mission.proof_ids = ["proof_backend"]
    mission.stages = [
        TaskStage(id="backend_contract", title="Backend Contract", objective="prove backend", status=StageStatus.READY_FOR_QA),
    ]

    action = MissionStateMachine().next_action(mission)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "qa"
    assert "needs slot qa" in action.reason


def test_backend_stage_that_mentions_future_launcher_gate_still_requires_launcher_release():
    mission = make_mission(TaskState.RUNNING)
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
    assert "needs slot qa" in action.reason


def test_text_only_backend_first_live_terminal_goal_routes_to_neko_before_qa():
    mission = make_mission(TaskState.RUNNING)
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
    assert action.slot_id == "qa"
    assert "needs slot qa" in action.reason


def test_state_machine_requires_visual_proof_before_terminal_close_when_requested():
    mission = mark_graph_complete(make_mission(TaskState.RUNNING))
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
    assert result.to_state == TaskState.RUNNING
    assert mission.state == TaskState.RUNNING
    assert mission.acceptance_criteria == ["ok"]
    assert result.events
    assert result.events[0].payload["actor"] == "neko_supervisor"


def test_unscoped_launcher_fix_enters_at_graph_root_with_launcher_pinned_dev_lane():
    """Stage 15.3 retarget. An unscoped Launcher+harness mission enters at the
    graph ROOT (Neko scopes first) instead of being keyword-routed straight to a
    dev slot, and the instantiated graph carries a dev lane pinned to
    ``EterniaLauncher`` — which is the slot that actually runs once scope passes."""

    mission = make_mission(TaskState.RUNNING)
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

    # This test used to pin LADDER behavior: with routing conditional, an
    # unscoped mission skipped typing and the legacy orchestrator inferred
    # "route to dev" by keyword-sniffing the title/description. Routing is now
    # graph-derived, so the FIRST action is the scope stage — the de-hardwiring
    # 01-architecture.md requires.
    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "neko_supervisor"
    assert action.stage_id == "scope"
    dev_stages = [
        stage
        for stage in mission.mission_plan.stages
        if (stage.owner_slot or stage.owner) in {"dev", "backend_dev"}
    ]
    assert dev_stages, "graph must carry a dev lane for the Launcher fix"
    launcher_lanes = [stage for stage in dev_stages if stage.repo == "EterniaLauncher"]
    assert launcher_lanes

    # Entering at the root only DEFERS the dev dispatch — it must still happen.
    # Release the scope stage the way the dev-mechanics suites do and assert the
    # Launcher lane is what the graph dispatches next, so the eventual-dev-run
    # proof the pre-retarget assertion carried is not lost.
    release_to_implementation(mission)

    released = MissionStateMachine().next_action(mission)

    assert released.type == HarnessActionType.RUN_SLOT
    assert released.slot_id == "dev"
    assert released.stage_id in {stage.id for stage in launcher_lanes}


# --- Stage 15.4: exactly one orchestrator ------------------------------------
#
# These replace the Stage 15.1 reachability probe (typed `orchestrator.legacy_fallback`
# event + per-call-site counters). The probe existed to MEASURE whether the legacy
# ladder was reachable before deleting it — it measured zero across the deterministic
# smoke mission, all nine burn-in case shapes, and a goal_runner goal — and its subject
# no longer exists, so its tests are retargeted to the successor behavior rather than
# carried as dead scaffolding.


def test_routing_is_unconditional_even_with_mission_plan_config_disabled():
    """Stage 15.3. A plan-less mission under the DEFAULT config (`mission_plan.enabled`
    false) is the exact shape that used to skip typing and fall to the legacy ladder.
    It must now be graph-typed and dispatched from the graph."""

    mission = make_mission(TaskState.RUNNING)
    assert mission.mission_plan is None
    assert not mission.stages
    config = RuntimeConfig(mission_plan=MissionPlanConfig(enabled=False))

    action = MissionStateMachine(config=config).next_action(mission)

    assert mission.mission_plan is not None
    assert mission.mission_plan.blueprint_id
    assert action.type == HarnessActionType.RUN_SLOT
    assert action.reason.startswith("blueprint stage ")


def test_blueprint_router_returning_none_refuses_instead_of_routing():
    """Stage 15.4. `_blueprint_next_action` is the ONLY action source; its `None`
    case is a typed refusal, never a silent fall-through to a second orchestrator."""

    import pytest

    from agent_runtime.errors import LegacyOrchestratorRemoved

    machine = MissionStateMachine(config=typed_config())
    mission = make_mission(TaskState.RUNNING)
    machine._blueprint_next_action = lambda mission, *, state: None

    with pytest.raises(LegacyOrchestratorRemoved) as excinfo:
        machine.next_action(mission)

    error = excinfo.value
    assert error.code == "legacy_orchestrator_removed"
    assert error.safe_details["task_id"] == mission.id
    assert error.safe_details["state"] == "running"
    assert error.safe_details["mission_plan_absent"] is False
    assert error.safe_details["blueprint_id_absent"] is False
    assert error.safe_details["stage_count"] >= 1


def test_legacy_orchestrator_removed_envelope_is_read_surface_safe():
    """Stage 15.4 read-surface contract. Read surfaces (status/snapshot) report
    this refusal as typed data instead of dying on it, so the envelope must carry
    every routing fact an operator needs to diagnose the mission — and nothing
    from the mission's own content."""

    import json

    import pytest

    from agent_runtime.errors import LegacyOrchestratorRemoved

    machine = MissionStateMachine(config=typed_config())
    mission = make_mission(TaskState.RUNNING)
    mission.title = "SECRET MISSION TITLE"
    mission.description = "SECRET MISSION DESCRIPTION"
    machine._blueprint_next_action = lambda mission, *, state: None

    with pytest.raises(LegacyOrchestratorRemoved) as excinfo:
        machine.next_action(mission)

    envelope = excinfo.value.read_surface_envelope()
    assert envelope["code"] == "legacy_orchestrator_removed"
    assert envelope["message"] == str(excinfo.value)
    assert set(envelope) == {
        "code",
        "message",
        "task_id",
        "state",
        "mission_plan_absent",
        "blueprint_id_absent",
        "stage_count",
        "current_stage_id",
    }
    assert "SECRET" not in json.dumps(envelope)
    # Mutating the projection must not mutate the exception's own details.
    envelope["task_id"] = "tampered"
    assert excinfo.value.safe_details["task_id"] == mission.id


def test_no_second_orchestrator_survives_in_the_state_machine_module():
    """Regression pin for the condition 03-retirement-ledger.md calls the single
    largest risk. The ledger marked this retired on 2026-06-25 while
    `_legacy_next_action` was still live and reached; pin the symbols so the
    claim cannot silently become false a second time."""

    import inspect

    from agent_runtime import state_machine

    source = inspect.getsource(state_machine)
    for retired in (
        "_legacy_next_action",
        "_legacy_dev_slot_for_task",
        "_legacy_backend_first_burn_in",
        "_mission_plan_routing_enabled",
    ):
        assert not hasattr(state_machine, retired), f"{retired} is back"
        assert retired not in source.replace("`" + retired + "`", ""), f"{retired} is back in source"
    assert "LegacyOrchestratorRemoved" in source
