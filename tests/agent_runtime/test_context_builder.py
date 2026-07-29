from hermes_time import now

from agent_runtime.context_builder import AgentContext, build_context, render_context
from agent_runtime.default_plan import ensure_default_mission_plan
from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.events import EventLog
from agent_runtime.models import AgentRun, MissionIntent, MissionPlan, MissionPlanStage, Proof, Task, TaskStage
from agent_runtime.packets import make_packet, record_packet
from agent_runtime.proof_rules import ProofType
from agent_runtime.repo_bundles import RepoBundleStore
from agent_runtime.runtime_config import MissionPlanConfig, NormalWorkerFlowConfig, RoleEnvelopeConfig, RuntimeConfig, SimplifiedAgentContractConfig
from agent_runtime.states import RunState, StageStatus, TaskState
from agent_runtime.store import ProofStore
from agent_runtime.role_checklists import RoleChecklistStore


def make_task():
    ts = now()
    return Task(
        id="task_abc",
        title="Build harness",
        description="Make agent runtime reliable",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        acceptance_criteria=["tests pass"],
        stages=[TaskStage(id="stage_1", title="Plan", objective="Plan it", status=StageStatus.DRAFT)],
        current_stage_id="stage_1",
    )


def make_run():
    ts = now()
    return AgentRun(
        id="run_abc",
        persona_id="dev",
        task_id="task_abc",
        stage_id="stage_1",
        state=RunState.RUNNING,
        started_at=ts,
        last_heartbeat_at=ts,
    )


def test_build_context_selects_current_stage_and_recent_events():
    ctx = build_context(make_task(), make_run(), recent_events=[{"type": "task.created"}], proof_ids=["proof_1"])

    assert isinstance(ctx, AgentContext)
    assert ctx.current_stage.id == "stage_1"
    assert ctx.recent_events == [{"type": "task.created"}]
    assert ctx.proof_ids == ["proof_1"]


def test_context_includes_stage53_simplified_agent_hud():
    ctx = build_context(make_task(), make_run())

    agent_hud = ctx.mission_hud["agent_hud"]
    assert agent_hud["mode"] == "stage53_simplified"
    assert agent_hud["contract"]["allowed_actions"] == ["deliver", "report_blocker", "request_missing_input"]
    assert agent_hud["current_assignment"]["acceptance"] == ["tests pass"]
    assert agent_hud["options"]
    assert agent_hud["recommended_action"]["payload_skeleton"] is not None
    assert agent_hud["recommended_action"]["skill_ref"] == "harness-dev-delivery"
    assert "decision_menu" in ctx.mission_hud


def test_neko_closed_choice_hud_does_not_release_qa_for_default_graph():
    task = make_task()
    task.state = "dev_ready_for_qa"
    task.stages = []
    ensure_default_mission_plan(task)
    run = make_run()
    run.persona_id = "neko_supervisor"
    cfg = RuntimeConfig(normal_worker_flow=NormalWorkerFlowConfig(enabled=False))

    hud = build_context(task, run, config=cfg).mission_hud

    assert hud["agent_hud"]["recommended_action"]["shape_id"] == "neko.scope_route"
    assert hud["next_required_move"]["shape_id"] == "neko.scope_route"


def test_rendered_context_uses_stage_output_template_instead_of_raw_description():
    task = make_task()
    task.description = "Raw task description should be input, not the whole first message."
    task.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title=task.title, objective=task.description),
        current_stage_id="implement_socket",
        stages=[
            MissionPlanStage(
                id="implement_socket",
                title="Implement Socket",
                objective="Implement the output-socket driven objective.",
                owner="dev",
                owner_slot="builder",
                repo="hermes-agent",
                kind="implementation",
                output_type="code feature",
                proof_gate={"required": True, "minimum_status": "passed", "required_proof_types": ["test_run"]},
            )
        ],
        slots={"builder": {"role": "builder", "required": True}},
    )
    task.current_stage_id = "implement_socket"
    run = make_run()
    run.stage_id = "implement_socket"

    rendered = render_context(build_context(task, run))

    assert "## Objective" in rendered
    assert "Build this node." in rendered
    assert "Deliver: a working code change with focused proof." in rendered
    assert "Objective: Implement the output-socket driven objective." in rendered
    assert "## Description" not in rendered


def test_downstream_objective_template_uses_upstream_packet_as_input():
    log = EventLog()
    task = make_task()
    task.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title=task.title, objective=task.description),
        current_stage_id="verify_socket",
        stages=[
            MissionPlanStage(
                id="implement_socket",
                title="Implement Socket",
                objective="Implement the feature.",
                owner="dev",
                owner_slot="builder",
                repo="hermes-agent",
                kind="implementation",
                output_type="code feature",
            ),
            MissionPlanStage(
                id="verify_socket",
                title="Verify Socket",
                objective="Verify the upstream output.",
                owner="qa",
                owner_slot="verifier",
                repo="hermes-agent",
                kind="qa_verdict",
                output_type="qa verdict",
                proof_gate={"required": True, "minimum_status": "approved", "required_proof_types": ["qa_verdict"]},
                depends_on=["implement_socket"],
            ),
        ],
        slots={"builder": {"role": "builder", "required": True}, "verifier": {"role": "verifier", "required": True}},
    )
    task.current_stage_id = "verify_socket"
    run = make_run()
    run.persona_id = "qa"
    run.stage_id = "verify_socket"
    packet = make_packet(
        task=task,
        decision=AgentDecision(type=DecisionType.PROPOSE_ACCEPTANCE, summary="delivered", rationale="done", payload={}),
        packet_type="delivery",
        body={"work_status": "patch_proposed", "summary": "upstream build output is ready"},
        actor="dev",
        run_id="run_dev",
        stage_id="implement_socket",
    )
    record_packet(packet, event_log=log)

    rendered = render_context(build_context(task, run, event_log=log))

    assert "Verify this node." in rendered
    assert "Deliver: a proof-backed QA verdict." in rendered
    assert "upstream build output is ready" in rendered


def test_agent_hud_current_assignment_is_stage_shaped_from_proof_gate():
    task = make_task()
    task.description = "Task-level description should not shape current assignment."
    task.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title=task.title, objective=task.description),
        current_stage_id="design_socket",
        stages=[
            MissionPlanStage(
                id="design_socket",
                title="Design Socket",
                objective="Write the design artifact.",
                owner="dev",
                owner_slot="builder",
                repo="hermes-agent",
                kind="context",
                output_type="design document",
                proof_gate={"required": True, "minimum_status": "passed", "required_proof_types": ["artifact"]},
            )
        ],
        slots={"builder": {"role": "builder", "required": True}},
        edges=[{"source": "design_socket", "outcome": "passed", "target": "done"}],
    )
    task.current_stage_id = "design_socket"
    run = make_run()
    run.stage_id = "design_socket"

    cfg = RuntimeConfig(normal_worker_flow=NormalWorkerFlowConfig(enabled=True))
    hud = build_context(task, run, config=cfg).mission_hud
    assignment = hud["agent_hud"]["current_assignment"]

    assert assignment["stage_id"] == "design_socket"
    assert assignment["owner_slot"] == "builder"
    assert assignment["objective"] == "Write the design artifact."
    assert assignment["output_type"] == "design document"
    assert assignment["required_proof_types"] == ["artifact"]
    assert assignment["outgoing_edges"] == [{"outcome": "passed", "target": "done"}]


def test_typed_plan_hud_exposes_stage_task_list_not_role_task_list(isolate_agent_runtime_root):
    task = make_task()
    task.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title=task.title, objective=task.description),
        current_stage_id="build_socket",
        stages=[
            MissionPlanStage(
                id="build_socket",
                title="Build Socket",
                objective="Build the socket stage.",
                owner="dev",
                owner_slot="builder",
                repo="hermes-agent",
                kind="implementation",
                output_type="code feature",
                proof_gate={"required": True, "minimum_status": "passed", "required_proof_types": ["test_run"]},
            )
        ],
        slots={"builder": {"role": "builder", "required": True}},
    )
    task.current_stage_id = "build_socket"
    run = make_run()
    run.stage_id = "build_socket"
    cfg = RuntimeConfig(role_envelope=RoleEnvelopeConfig(enabled=True))
    RoleChecklistStore().open_or_create(task=task, role_id="dev", mission_stage_id="build_socket", run_id=run.id)

    hud = build_context(task, run, config=cfg).mission_hud
    rendered = render_context(build_context(task, run, config=cfg))

    assert "role_task_list" not in hud
    assert hud["stage_task_list"]["stage_id"] == "build_socket"
    assert hud["stage_task_list"]["owner_slot"] == "builder"
    assert hud["stage_task_list"]["output_type"] == "code feature"
    assert '"stage_task_list"' in rendered
    assert '"role_task_list"' not in rendered


def test_context_agent_hud_surfaces_advisory_evidence_stack():
    task = make_task()
    task.harness_self_heal = {
        "evidence_stack": [
            {
                "kind": "proof_gate",
                "severity": "warning",
                "stage_id": "stage_1",
                "summary": "Required proof is missing or stale; goal owner must adjudicate.",
                "missing": ["missing test_run proof"],
                "warnings": ["stale visual proof"],
                "recommended_owner": "neko_supervisor",
                "recorded_at": "2026-06-23T00:00:00",
            }
        ]
    }

    ctx = build_context(task, make_run())

    evidence = ctx.mission_hud["agent_hud"]["evidence_stack"]
    assert evidence[0]["kind"] == "proof_gate"
    assert evidence[0]["missing"] == ["missing test_run proof"]
    assert evidence[0]["recommended_owner"] == "neko_supervisor"


def test_rendered_context_hides_legacy_hud_action_surfaces():
    ctx = build_context(make_task(), make_run())

    rendered = render_context(ctx)

    assert '"agent_hud"' in rendered
    assert '"recommended_action"' in rendered
    assert '"decision_menu"' not in rendered
    assert '"next_required_move"' not in rendered
    assert '"decision_shape_index"' not in rendered


def test_neko_diagnostic_hud_recommends_valid_ack_packet():
    task = Task(
        id="task_neko_diag",
        title="Neko diagnostic",
        description="Run one Neko diagnostic turn.",
        state=TaskState.CREATED,
        created_at=now(),
        updated_at=now(),
        requested_by="test",
        acceptance_criteria=["one Neko turn"],
        non_goals=["no Dev or QA"],
        affected_repos=["hermes-agent"],
        risk_flags=["persona_operation", "diagnostic_persona:neko_supervisor"],
    )
    run = AgentRun(
        id="run_neko_diag",
        persona_id="neko_supervisor",
        task_id=task.id,
        stage_id=None,
        state=RunState.RUNNING,
        started_at=now(),
        last_heartbeat_at=now(),
    )

    cfg = RuntimeConfig(normal_worker_flow=NormalWorkerFlowConfig(enabled=True))
    hud = build_context(task, run, config=cfg).mission_hud
    next_move = hud["next_required_move"]
    payload = next_move["recommended_payload"]

    assert next_move["shape_id"] == "neko.scope_route"
    assert payload["target_owner"] == "neko_supervisor"
    assert payload["proof_gate"] == {
        "required": False,
        "required_proof_types": ["harness_observation"],
        "minimum_status": "passed",
        "visual_required": False,
    }
    assert hud["agent_hud"]["recommended_action"]["shape_id"] == "neko.scope_route"


def test_simplified_agent_hud_actor_contracts_are_closed_choice():
    from agent_runtime.context_builder import build_context
    from agent_runtime.models import AgentRun, Task
    from agent_runtime.states import RunState, TaskState
    from hermes_time import now

    task = Task(
        id="task_contract_roles",
        title="Role contracts",
        description="Verify simplified role contracts.",
        state=TaskState.RUNNING,
        created_at=now(),
        updated_at=now(),
        requested_by="test",
        acceptance_criteria=["closed choices"],
    )

    expected = {
        "neko_supervisor": ["assign", "report_blocker", "request_missing_input"],
        "dev": ["deliver", "report_blocker", "request_missing_input"],
        "backend_dev": ["deliver", "report_blocker", "request_missing_input"],
        "qa": ["approve", "reject", "request_missing_proof"],
    }
    for persona_id, allowed_actions in expected.items():
        run = AgentRun(
            id=f"run_{persona_id}",
            persona_id=persona_id,
            task_id=task.id,
            stage_id=None,
            state=RunState.RUNNING,
            started_at=now(),
            last_heartbeat_at=now(),
        )
        ctx = build_context(task, run)
        contract = ctx.mission_hud["agent_hud"]["contract"]
        assert contract["allowed_actions"] == allowed_actions
        assert "request_test_run" not in contract["allowed_actions"]
        assert "correct_stage" not in contract["allowed_actions"]


def test_context_renders_repo_bundle_hud_with_datetime_fields(isolate_agent_runtime_root):
    task = make_task()
    task.affected_repos = ["hermes-agent"]
    RepoBundleStore().create_or_update_from_task(task)
    ctx = build_context(task, make_run())

    rendered = render_context(ctx)

    assert "repo_bundles" in rendered
    assert "staged_bundle_not_applied" in rendered
    assert "checkout_applied" in rendered
    assert "checkout not modified" in rendered
    assert "created_at" in rendered
    assert "Object of type datetime" not in rendered


def test_context_projects_latest_packet_from_event_log_after_resume():
    log = EventLog()
    task = make_task()
    run = make_run()
    decision = AgentDecision(
        type=DecisionType.REQUEST_TEST_RUN,
        summary="proof",
        rationale="proof",
        payload={"stage_id": "stage_1", "commands": ["pytest"], "delivery": {"work_status": "proof_requested", "known_gaps": []}},
    )
    packet = make_packet(task=task, decision=decision, packet_type="delivery", body={"work_status": "proof_requested", "known_gaps": []}, actor="dev", run_id=run.id, stage_id=run.stage_id)
    record_packet(packet, event_log=log)

    ctx = build_context(task, run, event_log=log)
    rendered = render_context(ctx)

    assert ctx.latest_delivery["packet_id"] == packet.packet_id
    assert "## Latest Delivery Packet" in rendered
    assert "proof_requested" in rendered


def test_launcher_contract_join_context_carries_backend_delivery_and_events():
    log = EventLog()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "launcher_contract_smoke"
    task.proof_ids = ["proof_backend"]
    task.stages = [
        TaskStage(id="backend_contract", title="Backend Contract", objective="Prove backend", status=StageStatus.READY),
        TaskStage(id="launcher_contract_smoke", title="Launcher Contract Smoke", objective="Consume backend packet", status=StageStatus.IMPLEMENTING),
    ]
    run = make_run()
    run.stage_id = "launcher_contract_smoke"
    backend_decision = AgentDecision(
        type=DecisionType.REQUEST_TEST_RUN,
        summary="backend proof",
        rationale="proof",
        payload={"stage_id": "backend_contract", "commands": ["printf backend"], "delivery": {"work_status": "proof_requested"}},
    )
    backend_packet = make_packet(
        task=task,
        decision=backend_decision,
        packet_type="delivery",
        body={
            "work_status": "proof_requested",
            "produced_contract_packet_id": "backend_contract_stage47_v1",
            "contract_packet": {
                "contract_packet_id": "backend_contract_stage47_v1",
                "endpoint": "GET /api/stage47",
                "request_shape": {},
                "response_shape": {"ok": "boolean"},
                "error_shape": {"error": "string"},
                "example_response": {"ok": True},
            },
            "known_gaps": ["Launcher proof pending"],
            "next_owner": "neko_supervisor",
        },
        actor="backend_dev",
        run_id="run_backend",
        stage_id="backend_contract",
    )
    record_packet(backend_packet, event_log=log)
    handoff_decision = AgentDecision(
        type=DecisionType.PROPOSE_ACCEPTANCE,
        summary="release launcher",
        rationale="joined",
        payload={"objective": "consume", "acceptance_criteria": ["proof"], "handoff_packet": {}},
    )
    handoff_packet = make_packet(
        task=task,
        decision=handoff_decision,
        packet_type="handoff_packet",
        body={
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
                "required_proof_ids": ["proof_backend"],
            },
            "join_gate": {"release_condition": "backend proof and contract packet joined"},
            "joined_proof_ids": ["proof_backend"],
            "joined_contract_packet_ids": [backend_packet.packet_id],
        },
        actor="neko_supervisor",
        run_id="run_neko",
        stage_id="launcher_contract_smoke",
    )
    record_packet(handoff_packet, event_log=log)

    ctx = build_context(task, run, event_log=log)
    rendered = render_context(ctx)

    assert ctx.latest_handoff_packet["packet_id"] == handoff_packet.packet_id
    assert ctx.latest_delivery["packet_id"] == backend_packet.packet_id
    assert any(event["type"] == "packet.recorded" for event in ctx.recent_events)
    assert "backend_contract_stage47_v1" in rendered
    assert "GET /api/stage47" in rendered
    assert "joined_proof_ids" in rendered
    assert "proof_backend" in rendered


def test_qa_release_context_carries_latest_upstream_delivery_packet():
    log = EventLog()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "backend_investigation"
    task.stages = [
        TaskStage(id="backend_investigation", title="Backend Investigation", objective="Investigate", status=StageStatus.READY_FOR_QA),
        TaskStage(id="qa_release", title="QA Release", objective="Review", status=StageStatus.READY),
    ]
    run = make_run()
    run.persona_id = "qa"
    run.stage_id = "qa_release"
    decision = AgentDecision(
        type=DecisionType.PROPOSE_PATCH,
        summary="Delivered no-edit report.",
        rationale="No product edits.",
        payload={
            "summary": "report",
            "changed_files": [],
            "tests": ["no product edits"],
            "delivery": {
                "work_status": "patch_proposed",
                "summary": "NSFW filtering can leak before moderation completes.",
                "findings": ["Uploads can become visible before moderation proof exists."],
                "recommendations": ["Keep media pending until moderation passes."],
                "known_gaps": [],
            },
        },
    )
    packet = make_packet(
        task=task,
        decision=decision,
        packet_type="delivery",
        body=decision.payload["delivery"],
        actor="backend_dev",
        run_id="run_backend",
        stage_id="backend_investigation",
    )
    record_packet(packet, event_log=log)

    ctx = build_context(task, run, event_log=log)
    rendered = render_context(ctx)

    assert ctx.latest_delivery["packet_id"] == packet.packet_id
    assert "NSFW filtering can leak" in rendered
    assert "Uploads can become visible" in rendered


def test_qa_release_context_prefers_newest_delivery_over_stale_stage_local_packet():
    log = EventLog()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "qa_release"
    task.stages = [
        TaskStage(id="backend_investigation", title="Backend Investigation", objective="Investigate", status=StageStatus.READY_FOR_QA),
        TaskStage(id="qa_release", title="QA Release", objective="Review", status=StageStatus.READY),
    ]
    run = make_run()
    run.persona_id = "qa"
    run.stage_id = "qa_release"

    stale_decision = AgentDecision(
        type=DecisionType.PROPOSE_PATCH,
        summary="Stale.",
        rationale="Stale.",
        payload={"delivery": {"work_status": "patch_proposed", "summary": "stale packet"}},
    )
    stale_packet = make_packet(
        task=task,
        decision=stale_decision,
        packet_type="delivery",
        body=stale_decision.payload["delivery"],
        actor="backend_dev",
        run_id="run_stale",
        stage_id="qa_release",
    )
    record_packet(stale_packet, event_log=log)

    latest_decision = AgentDecision(
        type=DecisionType.PROPOSE_PATCH,
        summary="Latest.",
        rationale="Latest.",
        payload={
            "delivery": {
                "work_status": "patch_proposed",
                "summary": "latest packet",
                "findings": ["Qwen VL local model option is explicitly covered."],
                "recommendations": ["WD tagger is a weak signal, not the sole enforcement gate."],
            }
        },
    )
    latest_packet_obj = make_packet(
        task=task,
        decision=latest_decision,
        packet_type="delivery",
        body=latest_decision.payload["delivery"],
        actor="backend_dev",
        run_id="run_latest",
        stage_id="backend_investigation",
    )
    record_packet(latest_packet_obj, event_log=log)

    ctx = build_context(task, run, event_log=log)
    rendered = render_context(ctx)

    assert ctx.latest_delivery["packet_id"] == latest_packet_obj.packet_id
    assert ctx.latest_delivery["packet_id"] != stale_packet.packet_id
    assert "Qwen VL local model option" in rendered
    assert "WD tagger is a weak signal" in rendered


def test_dev_recovery_context_carries_latest_cross_stage_qa_review():
    log = EventLog()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "backend_investigation"
    task.stages = [
        TaskStage(id="backend_investigation", title="Backend Investigation", objective="Investigate", status=StageStatus.REWORK),
        TaskStage(id="qa_release", title="QA Release", objective="Review", status=StageStatus.READY),
    ]
    run = make_run()
    run.persona_id = "backend_dev"
    run.stage_id = "backend_investigation"
    decision = AgentDecision(
        type=DecisionType.REPORT_QA_VERDICT,
        summary="Needs fixes.",
        rationale="QA found missing sections.",
        payload={
            "approved": False,
            "delivery_packets_reviewed": ["packet_de_report"],
            "qa_review": {
                "qa_review_version": 1,
                "review_scope": "implementation",
                "proof_reviewed": [],
                "coverage": {
                    "backend_contract": "reviewed",
                    "launcher_integration": "not_required",
                    "visual_or_mcp": "not_required",
                    "cross_stack_join": "not_required",
                },
                "delivery_packets_reviewed": ["packet_de_report"],
                "decision_basis": "delivery_packet_review",
                "remaining_gaps": [
                    "Add visible Qwen VL local model options with latency and hardware caveats.",
                    "State whether WD tagger safety ratings are enough for enforcement.",
                ],
                "next_owner": "dev",
            },
        },
    )
    packet = make_packet(
        task=task,
        decision=decision,
        packet_type="qa_review",
        body=decision.payload["qa_review"],
        actor="qa",
        run_id="run_qa",
        stage_id="qa_release",
    )
    record_packet(packet, event_log=log)

    ctx = build_context(task, run, event_log=log)
    rendered = render_context(ctx)

    assert ctx.latest_qa_review["packet_id"] == packet.packet_id
    assert "Latest QA Review Packet" in rendered
    assert "Add visible Qwen VL local model options" in rendered
    assert "WD tagger safety ratings" in rendered


def test_render_context_is_user_message_and_contains_no_schema_dump():
    ctx = build_context(make_task(), make_run())

    rendered = render_context(ctx)

    assert "# Agent Runtime Tick Context" in rendered
    assert "Build harness" in rendered
    assert "stage_1" in rendered
    assert "decision_schema" not in rendered.lower()


def test_render_context_includes_task_scope_fields_for_persona_execution():
    task = make_task()
    task.affected_repos = ["X:/repo/product"]

    rendered = render_context(build_context(task, make_run()))

    assert "## Affected Repositories" in rendered
    assert "product (unresolved; path withheld)" in rendered
    assert "X:/repo/product" not in rendered


def test_render_context_redacts_invalid_absolute_affected_repo_from_snapshot(tmp_path):
    task = make_task()
    missing_repo = tmp_path / "missing-private-repo"
    task.affected_repos = [str(missing_repo)]

    rendered = render_context(build_context(task, make_run()))

    assert "missing-private-repo" in rendered
    assert str(tmp_path) not in rendered


def test_build_context_defaults_to_task_proof_ids_for_handoff_awareness():
    task = make_task()
    task.proof_ids = ["proof_diff", "proof_tests"]

    ctx = build_context(task, make_run())
    rendered = render_context(ctx)

    assert ctx.proof_ids == ["proof_diff", "proof_tests"]
    assert "proof_diff, proof_tests" in rendered


def test_render_context_caps_proof_id_list_but_preserves_context_ids():
    proof_ids = [f"proof_{idx}" for idx in range(20)]
    ctx = build_context(make_task(), make_run(), recent_events=[], proof_ids=proof_ids)

    rendered = render_context(ctx)

    assert ctx.proof_ids == proof_ids
    assert "proof_0" not in rendered
    assert "proof_8" in rendered
    assert "proof_19" in rendered
    assert "+8 earlier proof id(s) omitted" in rendered


def test_validation_repair_hud_teaches_visual_screenshot_packet_shape(monkeypatch):
    monkeypatch.setattr(
        "agent_runtime.context_builder.mcp_owner_profile_name",
        lambda mcp_server: "launcher-qa",
    )
    run = make_run()
    run.persona_id = "qa"
    ctx = build_context(
        make_task(),
        run,
        requires_repair=True,
        repair_error="request_screenshot mcp_server must be launcher_qa",
    )

    repair_hud = ctx.mission_hud["validation_repair"]

    assert repair_hud["invalid_field"] == "payload.mcp_server"
    assert repair_hud["allowed_values"] == ["launcher_qa"]
    assert repair_hud["recommended_value"] == "launcher_qa"
    assert repair_hud["recommended_values"]["target"] == "mission_control"
    assert repair_hud["recommended_values"]["required_launch_pins.hermes_profile"] == "launcher-qa"
    assert repair_hud["required_payload_keys"] == [
        "stage_id",
        "target",
        "proof_requirement",
        "mcp_server",
        "required_launch_pins",
    ]
    assert repair_hud["required_launch_pins_keys"] == ["hermes_profile", "runtime_root_id"]
    assert "request_screenshot/request_video" in repair_hud["shape_hint"]
    assert "launcher_qa" in repair_hud["shape_hint"]
    assert "absolute path" in repair_hud["shape_hint"]


def test_validation_repair_hud_maps_missing_visual_target_to_mission_control():
    run = make_run()
    run.persona_id = "qa"
    ctx = build_context(
        make_task(),
        run,
        requires_repair=True,
        repair_error="missing payload keys: ['target']",
    )

    repair_hud = ctx.mission_hud["validation_repair"]

    assert repair_hud["invalid_field"] == "payload.target"
    assert repair_hud["recommended_value"] == "mission_control"
    assert repair_hud["recommended_values"]["mcp_server"] == "launcher_qa"
    assert "required_launch_pins" in repair_hud["shape_hint"]


def test_validation_repair_hud_teaches_qa_review_coverage_shape():
    run = make_run()
    run.persona_id = "qa"
    ctx = build_context(
        make_task(),
        run,
        requires_repair=True,
        repair_error="qa_review.coverage missing ['backend_contract', 'cross_stack_join', 'launcher_integration', 'visual_or_mcp']",
    )

    repair_hud = ctx.mission_hud["validation_repair"]

    assert repair_hud["invalid_field"] == "payload.qa_review.coverage"
    assert repair_hud["required_coverage_keys"] == [
        "backend_contract",
        "launcher_integration",
        "visual_or_mcp",
        "cross_stack_join",
    ]
    assert "reviewed" in repair_hud["allowed_values"]
    assert repair_hud["example"]["coverage"]["visual_or_mcp"] == "reviewed"
    assert repair_hud["example"]["next_owner"] == "harness"


def test_validation_repair_hud_points_unknown_payload_keys_to_closed_menu():
    ctx = build_context(
        make_task(),
        make_run(),
        requires_repair=True,
        repair_error="request_test_run payload has unsupported keys: ['made_up']; allowed keys are ['stage_id']",
    )

    repair_hud = ctx.mission_hud["validation_repair"]

    assert repair_hud["repair_mode"] == "closed_payload_contract"
    assert repair_hud["invalid_field"] == "payload"
    assert "agent_hud.recommended_action" in repair_hud["shape_hint"]
    assert repair_hud["corrected_shape"]["shape_id"] == ctx.mission_hud["agent_hud"]["recommended_action"]["shape_id"]
    assert repair_hud["corrected_shape"]["payload_skeleton"] is not None
    assert repair_hud["retry_rule"].startswith("Retry once")


def test_dev_mission_hud_exposes_closed_request_test_run_choice_and_commands():
    task = make_task()
    task.state = TaskState.RUNNING
    task.stages[0].test_plan = ["pytest tests/agent_runtime/test_context_builder.py -q"]

    ctx = build_context(task, make_run())

    hud = ctx.mission_hud
    assert hud["decision_contract_mode"] == "closed_choice"
    assert hud["next_required_move"]["shape_id"] == "dev.request_test_run"
    assert hud["decision_menu"][0]["primary"] is True
    assert hud["decision_menu"][0]["shape_id"] == "dev.request_test_run"
    assert hud["decision_menu"][0]["allowed_payload_keys"][:9] == [
        "stage_id",
        "commands",
        "recipe_id",
        "proof_intent",
        "intent",
        "repo_scope",
        "delivery",
        "failed_proof_ids",
        "failed_proof_auto_attached",
    ]
    assert hud["decision_menu"][0]["forbid_unknown_payload_keys"] is True
    assert hud["decision_menu"][0]["recommended_payload"]["commands"] == [
        "pytest tests/agent_runtime/test_context_builder.py -q"
    ]
    assert "common.request_file_reads" in [item["shape_id"] for item in hud["context_expansion_menu"]]






























def test_dev_mission_hud_requires_stage_plan_when_no_executable_stage_exists():
    task = make_task()
    task.state = TaskState.RUNNING
    task.stages = []
    task.current_stage_id = None
    run = make_run()
    run.stage_id = None

    ctx = build_context(task, run)

    hud = ctx.mission_hud
    assert hud["next_required_move"]["shape_id"] == "dev.propose_stage_plan"
    assert hud["decision_menu"][0]["primary"] is True
    assert hud["decision_menu"][0]["shape_id"] == "dev.propose_stage_plan"
    assert "dev.request_test_run" in [item["shape_id"] for item in hud["decision_menu"]]


def test_neko_mission_hud_exposes_visual_recovery_choice_without_patch_options():
    task = make_task()
    task.state = TaskState.RUNNING
    task.title = "Mission Control fullscreen visual proof"
    task.harness_self_heal = {"stages": {"stage_1": {"last_failed_proof_ids": ["proof_failed_visual"]}}}
    task.stages[0].requires_visual_proof = True
    run = make_run()
    run.persona_id = "neko_supervisor"

    ctx = build_context(task, run)

    hud = ctx.mission_hud
    assert hud["next_required_move"]["shape_id"] == "neko.scope_route"
    assert hud["decision_menu"][0]["decision_type"] == "scope_route"
    assert "proof_gate" in hud["decision_shape_index"]["neko.scope_route"]["allowed_payload_keys"]
    assert "request_test_run" not in hud.get("forbidden_decisions", [])
    assert all(item["forbid_unknown_payload_keys"] is True for item in hud["decision_menu"])


def test_qa_mission_hud_exposes_screenshot_and_verdict_choices():
    task = make_task()
    task.state = TaskState.RUNNING
    task.requires_visual_proof = True
    task.stages[0].requires_visual_proof = True
    run = make_run()
    run.persona_id = "qa"

    ctx = build_context(task, run)

    hud = ctx.mission_hud
    assert hud["next_required_move"]["shape_id"] == "qa.request_screenshot"
    shape_ids = [item["shape_id"] for item in hud["decision_menu"]]
    assert "qa.request_screenshot" in shape_ids
    assert "qa.verdict" in shape_ids
    assert hud["decision_shape_index"]["qa.request_screenshot"]["allowed_payload_keys"][:6] == [
        "stage_id",
        "target",
        "proof_requirement",
        "mcp_server",
        "required_launch_pins",
        "qa_review",
    ]




def test_render_context_includes_all_stages_and_proof_metadata_for_qa_review():
    task = make_task()
    task.stages.append(TaskStage(id="stage_2", title="Smoke", objective="Run smoke", status=StageStatus.READY, acceptance_criteria=["smoke-ok"], test_plan=["printf smoke-ok"]))
    task.proof_ids = ["proof_smoke"]
    store = ProofStore()
    store.attach(
        Proof(
            id="proof_smoke",
            task_id=task.id,
            stage_id="stage_2",
            type=ProofType.TEST_RUN,
            title="Smoke command proof",
            path_or_value="proofs/task_abc/artifacts/proof_smoke.log",
            created_by="harness",
            created_at=now(),
            metadata={
                "command": "printf 'smoke-ok'",
                "exit_code": 0,
                "status": "passed",
                "shell": "bash",
                "workdir_label": "EterniaBackend",
                "artifact_exists": True,
                "artifact_bytes": 120,
                "artifact_relative_path": "proofs/task_abc/artifacts/proof_smoke.log",
                "stdout_excerpt": "smoke-ok",
                "stderr_excerpt": "",
                "unsafe_debug_dump": "TOKEN=should-not-render",
            },
            redaction_status="safe",
        )
    )

    rendered = render_context(build_context(task, make_run(), proof_store=store))

    assert "## All Stages" in rendered
    assert "stage_1" in rendered and "stage_2" in rendered
    assert "## Proof Records" in rendered
    assert "proof_smoke" in rendered
    assert "exit_code: 0" in rendered
    assert "shell: bash" in rendered
    assert "workdir_label: EterniaBackend" in rendered
    assert "printf 'smoke-ok'" in rendered
    assert "artifact_exists: True" in rendered
    assert "artifact_relative_path: proofs/task_abc/artifacts/proof_smoke.log" in rendered
    assert "stdout_excerpt: smoke-ok" in rendered
    assert "unsafe_debug_dump" not in rendered
    assert "should-not-render" not in rendered


def test_render_context_compacts_non_current_stages_to_avoid_dev_context_bloat():
    task = make_task()
    for idx in range(2, 8):
        task.stages.append(
            TaskStage(
                id=f"stage_{idx}",
                title=f"Stage {idx}",
                objective="very long objective " * 40,
                status=StageStatus.READY,
                acceptance_criteria=["criterion " * 30],
                test_plan=["pytest tests/huge.py " * 20],
            )
        )

    rendered = render_context(build_context(task, make_run()))

    assert "## All Stages" in rendered
    assert "stage_7" in rendered
    assert "very long objective" not in rendered
    assert "acceptance_criteria: 1 item(s)" in rendered
    assert "test_plan: 1 item(s)" in rendered


def test_render_context_includes_safe_qa_blocked_findings_for_dev_recovery():
    task = make_task()
    task.state = TaskState.BLOCKED
    task.proof_ids = ["proof_qa_blocked"]
    store = ProofStore()
    store.attach(
        Proof(
            id="proof_qa_blocked",
            task_id=task.id,
            stage_id="stage_1",
            type=ProofType.QA_VERDICT,
            title="QA verdict: blocked",
            path_or_value="blocked",
            created_by="qa",
            created_at=now(),
            metadata={
                "verdict": "blocked",
                "findings": [
                    {
                        "severity": "blocking",
                        "issue": "missing proof manifest",
                        "required_fix": "attach proof mapping",
                        "secret_debug_dump": "TOKEN=should-not-render",
                    }
                ],
            },
            redaction_status="safe",
        )
    )

    rendered = render_context(build_context(task, make_run(), proof_store=store))

    assert "findings:" in rendered
    assert "missing proof manifest" in rendered
    assert "attach proof mapping" in rendered
    assert "blocking" in rendered
    assert "secret_debug_dump" not in rendered
    assert "should-not-render" not in rendered
