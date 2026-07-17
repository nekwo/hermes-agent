import json
from types import SimpleNamespace

from hermes_time import now
from utils import atomic_json_write
from agent_runtime.blueprints.runs import BlueprintRunStore
from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.models import AgentPersona, Event, Incident, MissionIntent, MissionPlan, MissionPlanStage, Proof, Task, TaskStage
from agent_runtime.persona_assignments import PersonaAssignmentSpec, PersonaAssignmentStore, PersonaInstanceStore
from agent_runtime.proof_rules import ProofType
from agent_runtime.runtime_config import EnterpriseWorkerSessionsConfig
from agent_runtime.snapshot import AGENT_TOPOLOGY_NODE_ID_CAP, _agent_topology, _agent_topology_node, _parity_warnings, build_snapshot, write_snapshot
from agent_runtime.states import RunState, StageStatus, TaskState
from agent_runtime.steering import execute_steer_action
from agent_runtime.store import IncidentStore, ProofStore, RunStore, TaskStore
from agent_runtime.events import EventLog
from agent_runtime.serde import to_jsonable


def test_agent_topology_collapses_multi_turn_instances_to_one_node_per_persona(isolate_agent_runtime_root):
    # A persona that runs N turns creates N persona_instances; the topology must
    # render ONE node per agent, not one per turn, or the graph floods with
    # duplicate "Backend Dev Agent" / "Launcher Dev Agent" nodes.
    ts = now()
    task = Task(
        id="task_topo",
        title="t",
        description="t",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="t",
        affected_repos=["EterniaBackend", "EterniaLauncher"],
        current_stage_id="backend_implementation",
    )
    task.goal_id = "task_topo"
    instances = (
        [SimpleNamespace(id=f"pi_bd_{n}", persona_id="backend_dev", goal_id="task_topo", task_id="task_topo", spawned_by="", state="completed") for n in range(30)]
        + [SimpleNamespace(id=f"pi_dev_{n}", persona_id="dev", goal_id="task_topo", task_id="task_topo", spawned_by="", state="completed") for n in range(30)]
        + [SimpleNamespace(id="pi_neko", persona_id="neko_supervisor", goal_id="task_topo", task_id="task_topo", spawned_by="", state="completed")]
    )

    topo = _agent_topology(task, active_runs=[], active_workers=[], runtime_instances=[], persona_instances=instances, role_streams=[])

    personas = [str(node.get("persona_id") or "") for node in topo["nodes"]]
    assert personas.count("backend_dev") == 1
    assert personas.count("dev") == 1
    assert personas.count("neko_supervisor") == 1
    assert len(topo["nodes"]) == 3
    assert topo["completeness"]["collapsed_multi_turn_instances"] == 58


def test_snapshot_contains_task_summary_and_no_raw_logs(isolate_agent_runtime_root):
    ts=TaskStore(); n=now(); ts.create(Task(id="t", title="T", description="d", state=TaskState.CREATED, created_at=n, updated_at=n, requested_by="tony"))
    snap=build_snapshot(task_store=ts)
    assert snap["tasks"][0]["task_id"] == "t"
    assert "stdout" not in str(snap).lower()
    assert snap["summary"]["dirty"] is True
    assert snap["dirty_state"]["runtime"]["open_task_ids"] == ["t"]
    write_snapshot(snap)
    assert (isolate_agent_runtime_root / "snapshot.json").exists()


def test_snapshot_parity_warns_on_legacy_operator_event_without_summary(isolate_agent_runtime_root):
    raw_event = Event(now(), "run.opened", "task_legacy", "run_legacy", "dev", {"stage_id": "impl"})
    payload = to_jsonable(raw_event)
    payload["payload"].pop("summary", None)
    events_path = isolate_agent_runtime_root / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    snap = build_snapshot()

    assert any(
        warning.get("code") == "event_summary_missing" and warning.get("event_type") == "run.opened"
        for warning in snap["parity"]["warnings"]
    )


def test_snapshot_parity_warns_on_non_iso_persona_chat_history_timestamp():
    warnings = _parity_warnings(
        {
            "persona_instance_runtime": {"enabled": True},
            "persona_instances": [
                {"persona_id": "neko_supervisor", "persona_instance_id": "personainst_neko"}
            ],
            "persona_chat_history": [
                {
                    "session_id": "chat_bad",
                    "persona_id": "neko_supervisor",
                    "persona_instance_id": "personainst_neko",
                    "kind": "chat",
                    "created_at": "2026-07-07T09:00:40Z",
                    "updated_at": 1783423009.58381,
                }
            ],
            "persona_chat_trace": [],
            "operator_channels": [],
            "summary": {},
        }
    )

    assert any(
        warning["code"] == "persona_chat_history.non_iso_timestamp"
        and warning["entity_id"] == "chat_bad"
        for warning in warnings
    )


def test_snapshot_parity_warns_when_live_mission_row_shadows_chat():
    warnings = _parity_warnings(
        {
            "persona_instance_runtime": {"enabled": True},
            "persona_instances": [
                {"persona_id": "neko_supervisor", "persona_instance_id": "personainst_neko"}
            ],
            "persona_chat_history": [
                {
                    "session_id": "relay_chat",
                    "persona_id": "neko_supervisor",
                    "persona_instance_id": "personainst_neko",
                    "kind": "chat",
                    "created_at": "2026-07-07T09:00:00Z",
                    "updated_at": "2026-07-07T09:00:00Z",
                },
                {
                    "session_id": "mission_run",
                    "persona_id": "neko_supervisor",
                    "persona_instance_id": "personainst_neko",
                    "kind": "mission",
                    "created_at": "2026-07-07T09:10:00Z",
                    "updated_at": "2026-07-07T09:10:00Z",
                },
            ],
            "persona_chat_trace": [],
            "operator_channels": [],
            "summary": {},
        }
    )

    assert any(
        warning["code"] == "persona_chat_history.live_mission_shadow"
        and warning["entity_id"] == "mission_run"
        for warning in warnings
    )


def _shadow_parity_data(generated_at):
    return {
        "generated_at": generated_at,
        "persona_instance_runtime": {"enabled": True},
        "persona_instances": [
            {"persona_id": "neko_supervisor", "persona_instance_id": "personainst_neko"}
        ],
        "persona_chat_history": [
            {
                "session_id": "relay_chat",
                "persona_id": "neko_supervisor",
                "persona_instance_id": "personainst_neko",
                "kind": "chat",
                "created_at": "2026-07-07T09:00:00Z",
                "updated_at": "2026-07-07T09:00:00Z",
            },
            {
                "session_id": "mission_run",
                "persona_id": "neko_supervisor",
                "persona_instance_id": "personainst_neko",
                "kind": "mission",
                "created_at": "2026-07-07T09:10:00Z",
                "updated_at": "2026-07-07T09:10:00Z",
            },
        ],
        "persona_chat_trace": [],
        "operator_channels": [],
        "summary": {},
    }


def test_live_mission_shadow_fires_when_mission_row_tracks_build_time():
    # The restamping regression: the mission row's timestamp hugs the
    # snapshot's own generated_at on every build.
    warnings = _parity_warnings(_shadow_parity_data("2026-07-07T09:10:01Z"))

    assert any(
        warning["code"] == "persona_chat_history.live_mission_shadow"
        and warning["entity_id"] == "mission_run"
        for warning in warnings
    )


def test_live_mission_shadow_stays_quiet_for_honest_assignment_anchor():
    # An assignment-anchored mission row is legitimately newer than every chat
    # after a mission is assigned; only build-time proximity is drift.
    warnings = _parity_warnings(_shadow_parity_data("2026-07-07T12:00:00Z"))

    assert not any(
        warning["code"] == "persona_chat_history.live_mission_shadow"
        for warning in warnings
    )


def test_snapshot_coalesces_progress_events_by_event_id(isolate_agent_runtime_root):
    n = now()
    TaskStore().create(
        Task(
            id="task_progress_coalesce",
            title="Progress coalesce",
            description="collapse repeated proof progress",
            state=TaskState.RUNNING,
            created_at=n,
            updated_at=n,
            requested_by="tony",
        )
    )
    log = EventLog()
    for status in ("running", "still_running"):
        log.append(
            Event(
                n,
                "run.progress",
                "task_progress_coalesce",
                "run_progress",
                "dev",
                {
                    "event_id": "progress:run_progress:proof:proof_command_running:1",
                    "phase": "proof",
                    "step": "proof_command_running",
                    "status": status,
                    "summary": f"proof is {status}",
                },
            )
        )

    snap = build_snapshot()
    timeline = [item for item in snap["tasks"][0]["timeline"] if item["type"] == "run.progress"]

    assert len(timeline) == 1
    assert timeline[0]["display_summary"] == "proof is still_running"


def test_snapshot_projects_blueprint_run_records(isolate_agent_runtime_root):
    n = now()
    task = Task(
        id="task_blueprint_snapshot",
        title="Blueprint snapshot",
        description="record projection",
        state=TaskState.DONE,
        created_at=n,
        updated_at=n,
        requested_by="tony",
        mission_plan=MissionPlan(
            mission_intent=MissionIntent(title="Blueprint snapshot", objective="record projection"),
            blueprint_id="one_agent_smoke",
            blueprint_version=1,
            bindings={"builder": "dev"},
            binding_sources={"builder": "persona:dev"},
            stages=[
                MissionPlanStage(
                    id="build",
                    title="Build",
                    objective="Build",
                    owner="builder",
                    owner_slot="builder",
                    repo="hermes-agent",
                    kind="implementation",
                    status=StageStatus.PASSED,
                )
            ],
        ),
    )
    BlueprintRunStore().record_task_terminal(task, result="passed", ended_at=n)

    snap = build_snapshot()

    assert snap["blueprint_runs"][0]["task_id"] == "task_blueprint_snapshot"
    assert snap["blueprint_runs"][0]["blueprint_id"] == "one_agent_smoke"
    assert snap["blueprint_runs"][0]["per_stage_outcomes"] == {"build": "passed"}
    assert any(item["id"] == "one_agent_smoke" for item in snap["blueprints"])


def test_snapshot_exposes_stage38_goal_flow_read_models(isolate_agent_runtime_root, history_in_frame_config):
    n = now()
    task = Task(
        id="stage38",
        title="Stage 38 projection",
        description="Project Neko, Dev, QA, and proof gate.",
        state=TaskState.RUNNING,
        created_at=n,
        updated_at=n,
        requested_by="tony",
        mission_plan=MissionPlan(
            mission_intent=MissionIntent(title="Stage 38 projection", objective="Project flow."),
            current_stage_id="scope",
            blueprint_id="neko_dev_qa_basic",
            blueprint_version=1,
            slots={
                "lead": {"role": "neko", "required": True},
                "builder": {"role": "builder", "required": True},
                "verifier": {"role": "verifier", "required": True},
            },
            bindings={
                "lead": "neko_supervisor",
                "builder": "dev",
                "verifier": "qa",
            },
            agent_topology={
                "root": "lead",
                "edges": [
                    {"source": "lead", "target": "builder", "kind": "steers"},
                    {"source": "builder", "target": "verifier", "kind": "steers"},
                ],
            },
            stages=[
                MissionPlanStage(
                    id="scope",
                    title="Scope",
                    objective="Scope the mission.",
                    owner="neko_supervisor",
                    owner_slot="lead",
                    repo="hermes-agent",
                    kind="planning",
                    status=StageStatus.IMPLEMENTING,
                ),
                MissionPlanStage(
                    id="implement",
                    title="Implement",
                    objective="Implement.",
                    owner="dev",
                    owner_slot="builder",
                    repo="hermes-agent",
                    kind="implementation",
                    depends_on=["scope"],
                    proof_gate={"required": True, "minimum_status": "passed", "required_proof_types": ["test_run"]},
                ),
                MissionPlanStage(
                    id="verify",
                    title="Verify",
                    objective="Verify proof.",
                    owner="qa",
                    owner_slot="verifier",
                    repo="hermes-agent",
                    kind="qa_verdict",
                    depends_on=["implement"],
                    proof_gate={"required": True, "minimum_status": "approved", "required_proof_types": ["qa_verdict"]},
                    blocks_qa_until=False,
                ),
            ],
        ),
    )
    TaskStore().create(task)
    events = EventLog()
    events.append(
        Event(
            ts=n,
            type="run.progress",
            task_id=task.id,
            run_id="run_neko",
            persona_id="neko_supervisor",
            payload={"summary": "Neko scoped the mission.", "redaction_status": "safe"},
        )
    )

    snap = build_snapshot(event_log=events)
    row = next(item for item in snap["tasks"] if item["task_id"] == "stage38")

    assert snap["parity"]["contract_version"] == 39
    assert "mission_level_state" in snap["parity"]["capabilities"]
    assert "agent_topology" in snap["parity"]["capabilities"]
    assert row["mission_level_state"]["blueprint_id"] == "neko_dev_qa_basic"
    topology = row["mission_level_state"]["agent_topology"]
    assert topology["root_node_id"] == "slot_lead"
    assert [(node["node_id"], node["persona_id"]) for node in topology["nodes"]] == [
        ("slot_lead", "neko_supervisor"),
        ("slot_builder", "dev"),
        ("slot_verifier", "qa"),
    ]
    assert [(edge["source_node_id"], edge["target_node_id"], edge["kind"]) for edge in topology["edges"]] == [
        ("slot_lead", "slot_builder", "steers"),
        ("slot_builder", "slot_verifier", "steers"),
    ]
    assert topology["control_node_id"] == "slot_lead"
    route_action = next(action for action in topology["steer_actions"] if action["verb"] == "route" and action["target_node_id"] == "slot_builder")
    assert route_action["available_now"] is True
    assert route_action["capability_id"] == "goal.steer"
    assert route_action["capability_args"] == {
        "task_id": "stage38",
        "verb": "route",
        "source_node_id": "slot_lead",
        "target_node_id": "slot_builder",
    }
    assert any(action["recommended_steer"] for action in topology["steer_actions"])
    assert topology["completeness"]["stream_event_cap_per_node"] == 3
    assert len(topology["nodes"][0]["progress_peek"]) <= 3
    assert [(actor["persona_id"], actor["presence"]) for actor in row["mission_level_state"]["actors"]] == [
        ("neko_supervisor", "waiting"),
        ("dev", "queued"),
        ("qa", "queued"),
    ]
    assert row["mission_flow_timeline"]["items"][0]["stage_id"] == "scope"
    assert row["proof_gate_state"]["gate_state"] == "incomplete"
    assert row["proof_gate_state"]["missing_stage_ids"] == ["implement", "verify"]
    assert row["proof_gate_state"]["why_not_ready"]
    assert row["operator_capabilities"]["actions"]["waive_proof"]["enabled"] is True


def test_mission_level_actors_emit_typed_persona_instance_id(monkeypatch, isolate_agent_runtime_root):
    # Strict contract (launcher handoff, HERMES_HANDOFF_persona_instance_id_2026-07-10):
    # every mission-level actor carries `persona_instance_id` — the persona
    # instance it is running as when bound, None when unbound. The launcher's
    # Mission Office matches live actors to authored placements by this id
    # (tier-1 sameInstance); dropping the field would regress that matching to
    # guessing by role.
    import agent_runtime.snapshot as snapshot_mod

    cfg = AgentRuntimeConfig(
        enterprise_worker_sessions=EnterpriseWorkerSessionsConfig(
            enabled=True,
            worker_session_store=True,
            persona_instance_runtime=True,
            persona_assignment_store=True,
        )
    )
    monkeypatch.setattr(snapshot_mod, "load_agent_runtime_config", lambda: cfg)
    n = now()
    task = Task(
        id="task_actor_contract",
        title="Actor instance contract",
        description="Bound dev emits its instance id; unbound qa emits None.",
        state=TaskState.RUNNING,
        created_at=n,
        updated_at=n,
        requested_by="tony",
        mission_plan=MissionPlan(
            mission_intent=MissionIntent(title="Actor instance contract", objective="Pin the actor-to-instance link."),
            current_stage_id="implement",
            blueprint_id="neko_dev_qa_basic",
            blueprint_version=1,
            bindings={"builder": "dev", "verifier": "qa"},
            stages=[
                MissionPlanStage(
                    id="implement",
                    title="Implement",
                    objective="Implement.",
                    owner="dev",
                    owner_slot="builder",
                    repo="hermes-agent",
                    kind="implementation",
                    status=StageStatus.IMPLEMENTING,
                ),
                MissionPlanStage(
                    id="verify",
                    title="Verify",
                    objective="Verify.",
                    owner="qa",
                    owner_slot="verifier",
                    repo="hermes-agent",
                    kind="qa_verdict",
                    depends_on=["implement"],
                ),
            ],
        ),
    )
    TaskStore().create(task)
    PersonaAssignmentStore().create_or_resume(
        PersonaAssignmentSpec(
            persona_id="dev",
            kind="task_stage",
            title="Implement",
            message="Implement the slice.",
            persona_instance_id="personainst_goal_task_actor_contract_dev",
            task_id=task.id,
            stage_id="implement",
        )
    )

    snap = build_snapshot()
    row = next(item for item in snap["tasks"] if item["task_id"] == "task_actor_contract")
    actors = {actor["persona_id"]: actor for actor in row["mission_level_state"]["actors"]}

    # Indexing (not .get) so a dropped field fails the contract loudly.
    assert actors["dev"]["persona_instance_id"] == "personainst_goal_task_actor_contract_dev"
    assert actors["qa"]["persona_instance_id"] is None


def test_steer_route_executes_live_available_action(isolate_agent_runtime_root):
    n = now()
    TaskStore().create(_steering_task(n))

    result = execute_steer_action(
        "steer_task",
        action_id="steer:slot_lead:slot_builder:route",
        requested_by="operator",
        reason="hand active slice to builder",
    )

    assert result["ok"] is True
    assert result["result"] == "stage_routed"
    assert result["stage_id"] == "implement"
    task = TaskStore().get("steer_task")
    assert task.current_stage_id == "implement"
    assert task.mission_plan.current_stage_id == "implement"
    assert next(stage for stage in task.mission_plan.stages if stage.id == "implement").status == StageStatus.IMPLEMENTING
    assignments = PersonaAssignmentStore().find_active(task_id=task.id, stage_id="implement")
    assert assignments
    assert assignments[0].kind == "steer_route"
    events = EventLog().for_task(task.id, limit=20)
    assert [event.type for event in events if event.type.startswith("steer.")] == [
        "steer.requested",
        "steer.started",
        "steer.returned",
    ]


def test_steer_spawn_executes_and_records_lineage(isolate_agent_runtime_root):
    n = now()
    TaskStore().create(_steering_task(n))

    result = execute_steer_action(
        "steer_task",
        action_id="steer:slot_lead:slot_builder:spawn",
        requested_by="operator",
        reason="delegate focused helper",
    )

    assert result["ok"] is True
    assert result["result"] == "helper_spawned"
    child = PersonaInstanceStore().get(result["persona_instance_id"])
    assert child.persona_id == "dev"
    assert child.goal_id == "steer_task"
    assert child.spawned_by
    parent = PersonaInstanceStore().get(child.spawned_by)
    assert parent.persona_id == "neko_supervisor"
    assignment = PersonaAssignmentStore().get(result["assignment_id"])
    assert assignment.kind == "steer_spawn"
    assert assignment.persona_instance_id == child.id


def test_steer_rejects_dead_affordance_and_opens_incident(isolate_agent_runtime_root):
    n = now()
    TaskStore().create(_steering_task(n))

    result = execute_steer_action(
        "steer_task",
        action_id="steer:slot_builder:slot_lead:route",
        requested_by="operator",
        reason="invalid reverse edge",
    )

    assert result["ok"] is False
    assert result["error_kind"] == "action_unavailable"
    incident = IncidentStore().get(result["incident_id"])
    assert incident.kind == "steer_failed"
    assert incident.task_id == "steer_task"


def _steering_task(n):
    return Task(
        id="steer_task",
        title="Steering task",
        description="Exercise steering.",
        state=TaskState.RUNNING,
        created_at=n,
        updated_at=n,
        requested_by="tony",
        mission_plan=MissionPlan(
            mission_intent=MissionIntent(title="Steering task", objective="Exercise steering."),
            current_stage_id="scope",
            blueprint_id="steering_test",
            blueprint_version=1,
            slots={
                "lead": {"role": "neko", "required": True},
                "builder": {"role": "builder", "required": True},
            },
            bindings={
                "lead": "neko_supervisor",
                "builder": "dev",
            },
            agent_topology={
                "root": "lead",
                "edges": [
                    {"source": "lead", "target": "builder", "kind": "steers"},
                ],
            },
            stages=[
                MissionPlanStage(id="scope", title="Scope", objective="Scope", owner="neko_supervisor", owner_slot="lead", repo="hermes-agent", kind="planning", status=StageStatus.IMPLEMENTING),
                MissionPlanStage(id="implement", title="Implement", objective="Implement", owner="dev", owner_slot="builder", repo="hermes-agent", kind="implementation", status=StageStatus.READY),
            ],
        ),
    )


def test_agent_topology_node_caps_id_lists_and_reports_drops():
    stages = [
        SimpleNamespace(id=f"stage_{index}", status=StageStatus.READY)
        for index in range(AGENT_TOPOLOGY_NODE_ID_CAP + 2)
    ]
    runs = [
        SimpleNamespace(id=f"run_{index}", persona_id="dev", llm={"total_tokens": 1})
        for index in range(AGENT_TOPOLOGY_NODE_ID_CAP + 3)
    ]
    workers = [
        SimpleNamespace(id=f"worker_{index}", persona_id="dev")
        for index in range(AGENT_TOPOLOGY_NODE_ID_CAP + 4)
    ]

    node = _agent_topology_node(
        node_id="slot_builder",
        persona_id="dev",
        instance=SimpleNamespace(id="personainst_dev", display_name="Dev", role="dev"),
        owned_stages=stages,
        active_runs=runs,
        active_workers=workers,
        stream={},
        fallback_display="Dev",
        fallback_role="dev",
    )

    assert len(node["stage_ids"]) == AGENT_TOPOLOGY_NODE_ID_CAP
    assert len(node["active_run_ids"]) == AGENT_TOPOLOGY_NODE_ID_CAP
    assert len(node["active_worker_session_ids"]) == AGENT_TOPOLOGY_NODE_ID_CAP
    assert {drop["field"] for drop in node["_drops"]} == {
        "stage_ids",
        "active_run_ids",
        "active_worker_session_ids",
    }
    assert all(drop["reason"] == "topology_node_id_cap" for drop in node["_drops"])


def test_snapshot_projects_bounded_stage_verification_with_parity(isolate_agent_runtime_root):
    n = now()
    task = Task(
        id="stage_verify",
        title="Stage verification",
        description="Project observed and authoritative proof lanes.",
        state=TaskState.RUNNING,
        created_at=n,
        updated_at=n,
        requested_by="tony",
        risk_flags=["test_tampering_detected"],
        mission_plan=MissionPlan(
            mission_intent=MissionIntent(title="Stage verification", objective="Show proof lanes."),
            current_stage_id="stage_13",
            stages=[
                MissionPlanStage(
                    id=f"stage_{index}",
                    title=f"Stage {index}",
                    objective="Verify",
                    owner="dev",
                    repo="EterniaLauncher",
                    kind="implementation",
                )
                for index in range(14)
            ],
        ),
    )
    observations = {}
    for index in range(14):
        observations[f"stage_{index}"] = {
            "schema_version": 1,
            "captured_at": f"2026-07-01T20:{index:02d}:00Z",
            "source": "harness_observed_handoff",
            "actor": "dev",
            "run_id": f"run_observed_{index}",
            "stage_id": f"stage_{index}",
            "repo_diff": {
                "diff": "x" * 120,
                "diff_chars": 120 + index,
                "truncated": index == 13,
                "baseline_dirty_count": 8,
                "excluded_baseline_paths": [f"dirty_{n}.dart" for n in range(8)],
            },
            "observed_proof_ids": [f"proof_observed_{n}" for n in range(10)],
            "authoritative_gate_proof_ids": ["proof_auth_passed"],
            "authoritative_gate_status": "passed",
            "authoritative_gate_run_id": f"run_auth_{index}",
            "tamper_flag": index == 13,
        }
    task.harness_self_heal = {"stage_observations": observations}
    TaskStore().create(task)
    proof_store = ProofStore()
    for proof_index in range(8):
        proof_store.attach(
            Proof(
                id=f"proof_observed_{proof_index}",
                task_id=task.id,
                stage_id="stage_13",
                type=ProofType.TEST_RUN,
                title="Observed proof",
                path_or_value="observed.log",
                created_by="dev",
                created_at=n,
                metadata={"status": "passed"},
            )
        )
    proof_store.attach(
        Proof(
            id="proof_auth_passed",
            task_id=task.id,
            stage_id="stage_13",
            type=ProofType.TEST_RUN,
            title="Authoritative proof",
            path_or_value="auth.log",
            created_by="harness",
            created_at=n,
            metadata={"status": "passed"},
        )
    )

    snap = build_snapshot()
    row = next(item for item in snap["tasks"] if item["task_id"] == task.id)
    verification = row["stage_verification"]

    assert len(verification["stages"]) == 12
    assert verification["stages"][0]["stage_id"] == "stage_2"
    latest = verification["stages"][-1]
    assert latest["stage_id"] == "stage_13"
    assert latest["owner"] == "dev"
    assert latest["repo_diff"]["diff_chars"] == 133
    assert len(latest["repo_diff"]["excluded_baseline_paths"]) == 6
    assert len(latest["observed"]["proof_ids"]) == 8
    assert latest["observed"]["status"] == "passed"
    assert latest["authoritative"]["status"] == "passed"
    assert latest["authoritative"]["run_id"] == "run_auth_13"
    assert latest["tamper_flag"] is True
    assert verification["completeness"]["truncated"] is True
    assert "stage_verification" in snap["parity"]["capabilities"]
    parity = snap["parity"]["completeness"]["stage_verification"]
    assert parity["considered"] == 14
    assert parity["included"] == 12
    assert parity["truncated"] is True
    assert parity["reasons"]["stage_cap"] == 2
    assert parity["reasons"]["observed_proof_id_cap"] == 24
    assert parity["reasons"]["excluded_baseline_path_cap"] == 24
    assert parity["reasons"]["source_diff_truncated"] == 1


def test_snapshot_agent_topology_runtime_spawned_by_overrides_blueprint(isolate_agent_runtime_root):
    n = now()
    task = Task(
        id="topology_spawned_by",
        title="Topology spawned_by",
        description="Runtime steering should win.",
        state=TaskState.RUNNING,
        created_at=n,
        updated_at=n,
        requested_by="tony",
        mission_plan=MissionPlan(
            mission_intent=MissionIntent(title="Topology spawned_by", objective="Runtime steering should win."),
            current_stage_id="scope",
            blueprint_id="neko_two_dev_default",
            blueprint_version=1,
            slots={
                "lead": {"role": "neko", "required": True},
                "backend_builder": {"role": "backend_dev", "required": True},
                "builder": {"role": "builder", "required": True},
            },
            bindings={
                "lead": "neko_supervisor",
                "backend_builder": "backend_dev",
                "builder": "dev",
            },
            agent_topology={
                "root": "lead",
                "edges": [
                    {"source": "lead", "target": "builder", "kind": "steers"},
                    {"source": "builder", "target": "backend_builder", "kind": "steers"},
                ],
            },
            stages=[
                MissionPlanStage(id="scope", title="Scope", objective="Scope", owner="neko_supervisor", owner_slot="lead", repo="hermes-agent", kind="planning"),
                MissionPlanStage(id="backend", title="Backend", objective="Backend", owner="backend_dev", owner_slot="backend_builder", repo="EterniaBackend", kind="implementation"),
                MissionPlanStage(id="launcher", title="Launcher", objective="Launcher", owner="dev", owner_slot="builder", repo="EterniaLauncher", kind="implementation"),
            ],
        ),
    )
    TaskStore().create(task)
    store = PersonaInstanceStore()
    neko = store.ensure_for_goal(
        _topology_persona("neko_supervisor", "Neko Mission Lead", "neko_supervisor"),
        goal_id=task.id,
        spawned_by=None,
        placement_id="topology_spawned_by:neko_supervisor",
    )
    backend = store.ensure_for_goal(
        _topology_persona("backend_dev", "Backend Dev Agent", "backend_dev"),
        goal_id=task.id,
        spawned_by=neko.id,
        placement_id="topology_spawned_by:backend_dev",
    )
    store.ensure_for_goal(
        _topology_persona("dev", "Launcher Dev Agent", "dev"),
        goal_id=task.id,
        spawned_by=backend.id,
        placement_id="topology_spawned_by:dev",
    )

    snap = build_snapshot()
    row = next(item for item in snap["tasks"] if item["task_id"] == task.id)

    topology = row["mission_level_state"]["agent_topology"]
    assert topology["source"] == "runtime_spawned_by"
    assert [(edge["source_node_id"], edge["target_node_id"]) for edge in topology["edges"]] == [
        ("personainst_topology_spawned_by_neko_supervisor", "personainst_topology_spawned_by_backend_dev"),
        ("personainst_topology_spawned_by_backend_dev", "personainst_topology_spawned_by_dev"),
    ]
    backend_node = next(node for node in topology["nodes"] if node["persona_id"] == "backend_dev")
    assert backend_node["spawned_by"] == "personainst_topology_spawned_by_neko_supervisor"


def _topology_persona(persona_id: str, display_name: str, role: str) -> AgentPersona:
    return AgentPersona(
        id=persona_id,
        display_name=display_name,
        role=role,
        model=None,
        provider=None,
        api_mode=None,
        toolsets=[],
        system_prompt_path="",
    )


def test_snapshot_unscoped_task_keeps_all_canonical_role_streams_visible(isolate_agent_runtime_root):
    ts = TaskStore()
    n = now()
    ts.create(
        Task(
            id="unscoped",
            title="Fresh Mission Control goal",
            description="Created before Neko has written a mission plan.",
            state=TaskState.CREATED,
            created_at=n,
            updated_at=n,
            requested_by="tony",
        )
    )

    snap = build_snapshot(task_store=ts)
    roles = {stream["persona_id"]: stream for stream in snap["tasks"][0]["role_streams"]}

    assert {"neko_supervisor", "backend_dev", "dev", "qa"}.issubset(roles)
    assert all(stream["events"] for stream in roles.values())
    assert roles["dev"]["events"][0]["type"] == "role_stream.status"
    assert roles["qa"]["events"][0]["payload"]["display_title"] == "QA Agent ready"


def test_snapshot_role_stream_projects_decision_summary_thinking_fields(isolate_agent_runtime_root):
    ts = TaskStore()
    events = EventLog()
    n = now()
    ts.create(
        Task(
            id="thinking_task",
            title="Harness thinking log smoke",
            description="Show safe process summaries.",
            state=TaskState.DONE,
            created_at=n,
            updated_at=n,
            requested_by="tony",
        )
    )
    events.append(
        Event(
            ts=n,
            type="run.progress",
            task_id="thinking_task",
            run_id="run_slot",
            persona_id="dev",
            payload={
                "type": "run.progress",
                "phase": "thinking_process",
                "step": "decision_summary",
                "status": "completed",
                "summary": "Agent decision process summarized",
                "decision_type": "request_test_run",
                "reasoning_summary": "Request Harness proof before QA.",
            },
        )
    )

    snap = build_snapshot(task_store=ts, event_log=events)
    roles = {stream["persona_id"]: stream for stream in snap["tasks"][0]["role_streams"]}
    event = roles["dev"]["events"][0]

    assert event["payload"]["display_kind"] == "thinking_summary"
    assert event["payload"]["phase"] == "thinking_process"
    assert event["payload"]["step"] == "decision_summary"
    assert event["payload"]["decision_type"] == "request_test_run"
    assert event["payload"]["reasoning_summary"] == "Request Harness proof before QA."


def test_snapshot_exposes_terminal_and_active_run_execution_truth(isolate_agent_runtime_root):
    ts = TaskStore()
    runs = RunStore()
    n = now()
    ts.create(Task(id="done", title="Done", description="d", state=TaskState.DONE, created_at=n, updated_at=n, requested_by="tony"))
    ts.create(Task(id="active", title="Active", description="d", state=TaskState.RUNNING, created_at=n, updated_at=n, requested_by="tony"))
    runs.open_run("dev", "active", stage_id=None)

    snap = build_snapshot(task_store=ts, run_store=runs)
    by_id = {task["task_id"]: task for task in snap["tasks"]}

    assert by_id["done"]["execution_status"] == "complete"
    assert by_id["done"]["can_start_run"] is False
    assert by_id["done"]["run_blocked_reason"] == "mission is terminal"
    assert by_id["active"]["execution_status"] == "running"
    assert by_id["active"]["active_persona_ids"] == ["dev"]
    assert by_id["active"]["can_start_run"] is False
    assert snap["summary"]["active_runs"] == 1
    assert snap["summary"]["running_runs"] == 1


def test_snapshot_top_level_proofs_include_status_and_command_metadata(isolate_agent_runtime_root):
    ts = TaskStore()
    proofs = ProofStore()
    n = now()
    task = Task(id="proofed", title="Proofed", description="d", state=TaskState.DONE, created_at=n, updated_at=n, requested_by="tony")
    ts.create(task)
    proofs.attach(
        Proof(
            id="proof_test",
            task_id=task.id,
            stage_id="stage_1",
            type=ProofType.TEST_RUN,
            title="Command proof: pytest tests/example.py",
            path_or_value="proofs/proofed/artifacts/proof_test.log",
            created_by="harness",
            created_at=n,
            metadata={"status": "passed", "exit_code": 0, "duration_ms": 42},
            redaction_status="safe",
        )
    )

    snap = build_snapshot(task_store=ts, proof_store=proofs)

    assert snap["proofs"] == [
        {
            "proof_id": "proof_test",
            "task_id": "proofed",
            "stage_id": "stage_1",
            "type": "test_run",
            "title": "Command proof: pytest tests/example.py",
            "status": "passed",
            "exit_code": 0,
            "duration_ms": 42,
            "has_artifact": True,
            "redaction_status": "safe",
            "created_by": "harness",
            "created_at": n,
        }
    ]


def test_snapshot_exposes_typed_mission_role_and_stage_streams(isolate_agent_runtime_root):
    ts = TaskStore()
    events = EventLog()
    n = now()
    task = Task(
        id="typed",
        title="Fix Mission Control terminals",
        description="d",
        state=TaskState.RUNNING,
        created_at=n,
        updated_at=n,
        requested_by="tony",
        current_stage_id="launcher_implementation",
        mission_plan=MissionPlan(
            mission_intent=MissionIntent(title="Fix Mission Control terminals", objective="d"),
            current_stage_id="launcher_implementation",
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
        ),
    )
    ts.create(task)
    events.append(Event(n, "worker_session.opened", task.id, None, "backend_dev", {"stage_id": "backend_contract_smoke"}))
    events.append(Event(n, "run.tool.finished", task.id, "run_launcher", "dev", {"stage_id": "launcher_implementation", "tool_name": "terminal", "summary": "flutter test passed"}))

    snap = build_snapshot(task_store=ts, event_log=events)
    item = snap["tasks"][0]

    assert item["mission_plan"]["current_stage_id"] == "launcher_implementation"
    roles = {stream["persona_id"]: stream for stream in item["role_streams"]}
    assert {"neko_supervisor", "backend_dev", "dev", "qa"}.issubset(roles)
    assert roles["backend_dev"]["events"][0]["type"] == "worker_session.opened"
    stages = {stream["stage_id"]: stream for stream in item["stage_streams"]}
    assert stages["launcher_implementation"]["events"][0]["type"] == "run.tool.finished"


def test_snapshot_typed_plan_keeps_empty_backend_stream_selectable(isolate_agent_runtime_root):
    ts = TaskStore()
    n = now()
    task = Task(
        id="typed_missing_backend",
        title="Fix Mission Control role logs",
        description="Show Neko, Backend Dev, Launcher Dev, and QA logs even when one role has no events yet.",
        state=TaskState.RUNNING,
        created_at=n,
        updated_at=n,
        requested_by="tony",
        current_stage_id="launcher_implementation",
        mission_plan=MissionPlan(
            mission_intent=MissionIntent(title="Fix Mission Control role logs", objective="Show all role logs."),
            current_stage_id="launcher_implementation",
            stages=[
                MissionPlanStage(
                    id="launcher_implementation",
                    title="Launcher",
                    objective="Launcher",
                    owner="dev",
                    repo="EterniaLauncher",
                    kind="implementation",
                    status=StageStatus.IMPLEMENTING,
                ),
                MissionPlanStage(
                    id="qa_release",
                    title="QA",
                    objective="QA",
                    owner="qa",
                    repo="EterniaLauncher",
                    kind="qa_verdict",
                    depends_on=["launcher_implementation"],
                    blocks_qa_until=False,
                ),
            ],
        ),
    )
    ts.create(task)

    snap = build_snapshot(task_store=ts)
    roles = {stream["persona_id"]: stream for stream in snap["tasks"][0]["role_streams"]}

    assert {"neko_supervisor", "backend_dev", "dev", "qa"}.issubset(roles)
    assert roles["backend_dev"]["display_name"] == "Backend Dev Agent"
    assert roles["backend_dev"]["events"][0]["type"] == "role_stream.status"
    assert roles["backend_dev"]["events"][0]["persona_id"] == "backend_dev"
    assert roles["backend_dev"]["events"][0]["payload"]["redaction_status"] == "safe"
    assert "no redaction-safe events" in roles["backend_dev"]["events"][0]["payload"]["display_summary"]
    assert roles["backend_dev"]["active_run_ids"] == []
    assert all(stream["events"] for stream in roles.values())


def test_snapshot_role_streams_use_task_window_not_global_tail(isolate_agent_runtime_root):
    ts = TaskStore()
    events = EventLog()
    n = now()
    task = Task(
        id="typed_stream_tail",
        title="Fix Mission Control role logs",
        description="Keep every role visible even after one role emits many events.",
        state=TaskState.RUNNING,
        created_at=n,
        updated_at=n,
        requested_by="tony",
        current_stage_id="launcher_implementation",
        mission_plan=MissionPlan(
            mission_intent=MissionIntent(title="Fix Mission Control role logs", objective="Show all role logs."),
            current_stage_id="launcher_implementation",
            stages=[
                MissionPlanStage(
                    id="launcher_implementation",
                    title="Launcher",
                    objective="Launcher",
                    owner="dev",
                    repo="EterniaLauncher",
                    kind="implementation",
                    status=StageStatus.IMPLEMENTING,
                ),
                MissionPlanStage(
                    id="qa_release",
                    title="QA",
                    objective="QA",
                    owner="qa",
                    repo="EterniaLauncher",
                    kind="qa_verdict",
                    depends_on=["launcher_implementation"],
                    blocks_qa_until=False,
                ),
            ],
        ),
    )
    ts.create(task)
    events.append(Event(n, "mission_plan.updated", task.id, "run_neko", "neko_supervisor", {"summary": "Neko scoped the mission."}))
    for index in range(25):
        events.append(Event(n, "run.tool.finished", task.id, "run_slot", "dev", {"summary": f"Dev tool event {index}"}))

    snap = build_snapshot(task_store=ts, event_log=events)
    roles = {stream["persona_id"]: stream for stream in snap["tasks"][0]["role_streams"]}

    assert roles["neko_supervisor"]["events"][0]["type"] == "mission_plan.updated"
    assert roles["dev"]["events"]


def test_snapshot_next_action_owner_reports_backend_specialist_for_backend_stage(isolate_agent_runtime_root):
    ts = TaskStore()
    n = now()
    ts.create(
        Task(
            id="backend",
            title="Stage 47",
            description="d",
            state=TaskState.RUNNING,
            created_at=n,
            updated_at=n,
            requested_by="tony",
            affected_repos=["EterniaBackend", "EterniaLauncher"],
            current_stage_id="stage_47_backend_contract_proof",
            stages=[
                TaskStage(
                    id="stage_47_backend_contract_proof",
                    title="Backend Contract Proof",
                    objective="Attach backend proof before the Launcher release gate.",
                    status=StageStatus.IMPLEMENTING,
                    test_plan=["python manage.py test api.tests.SystemHealthContractTests"],
                )
            ],
        )
    )

    snap = build_snapshot(task_store=ts)

    assert snap["tasks"][0]["next_action"]["action"] == "run_slot"
    assert snap["tasks"][0]["next_action"]["stopped_progress"]["owner"] == "backend_dev"


def test_snapshot_routes_budget_approval_to_neko_before_cap(isolate_agent_runtime_root):
    ts = TaskStore()
    runs = RunStore()
    incidents = IncidentStore()
    n = now()
    ts.create(Task(id="budget", title="Budget", description="d", state=TaskState.RUNNING, created_at=n, updated_at=n, requested_by="tony"))
    waiting = runs.open_run("dev", "budget", stage_id="stage_1", session_id="session_budget")
    waiting.state = RunState.WAITING_ON_APPROVAL
    waiting.error = {"type": "run_budget_exceeded"}
    runs.update(waiting)
    incidents.open(Incident(id="inc_budget", task_id="budget", run_id=waiting.id, kind="run_budget_exceeded", summary="budget", detail_path=None, opened_at=n))

    snap = build_snapshot(task_store=ts, run_store=runs, incident_store=incidents)

    assert snap["tasks"][0]["next_action"]["action"] == "run_slot"
    assert snap["tasks"][0]["next_action"]["stopped_progress"]["owner"] == "neko_supervisor"


def test_snapshot_routes_read_search_budget_loop_to_neko_scope_recovery(isolate_agent_runtime_root):
    ts = TaskStore()
    runs = RunStore()
    incidents = IncidentStore()
    n = now()
    ts.create(Task(id="budget", title="Budget", description="d", state=TaskState.RUNNING, created_at=n, updated_at=n, requested_by="tony", open_incident_ids=["inc_loop"]))
    waiting = runs.open_run("backend_dev", "budget", stage_id="backend_implementation", session_id="session_budget")
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
    incidents.open(Incident(id="inc_loop", task_id="budget", run_id=waiting.id, kind="run_budget_exceeded", summary="budget", detail_path=None, opened_at=n))

    snap = build_snapshot(task_store=ts, run_store=runs, incident_store=incidents)

    assert snap["tasks"][0]["next_action"]["action"] == "run_slot"
    assert snap["tasks"][0]["next_action"]["reason"] == "Dev exhausted read/search without patch or proof; Neko must split or narrow the stage before retry"
    assert snap["tasks"][0]["next_action"]["stopped_progress"]["owner"] == "neko_supervisor"


def test_snapshot_exposes_specialist_agent_repo_scope_labels(isolate_agent_runtime_root):
    class AgentList:
        def list_all(self):
            return [
                AgentPersona(
                    id="backend_dev",
                    display_name="Backend Dev Agent",
                    role="dev",
                    model="gpt-5.5",
                    provider="openai-codex",
                    api_mode="codex_responses",
                    toolsets=["file", "search", "terminal"],
                    system_prompt_path="personas/dev/system.md",
                    hermes_profile="backend-dev",
                    repo_scope="X:/Unreal Engine/Engine/EterniaBackend",
                    repo_scope_label="EterniaBackend",
                )
            ]

    snap = build_snapshot(agent_store=AgentList())
    agent = snap["agents"][0]

    assert agent["persona_id"] == "backend_dev"
    assert agent["display_name"] == "Backend Dev Agent"
    assert agent["role"] == "dev"
    assert agent["hermes_profile"] == "backend-dev"
    assert agent["repo_scope_label"] == "EterniaBackend"
    assert "X:/Unreal" not in repr(agent)


def test_snapshot_exposes_available_profile_personas_without_changing_agents(tmp_path, monkeypatch, isolate_agent_runtime_root):
    from agent_runtime import snapshot as snapshot_mod

    hermes_home = tmp_path / "hermes_home"
    profiles_root = hermes_home / "profiles"
    alice = profiles_root / "alice"
    reviewer = profiles_root / "reviewer"
    alice.mkdir(parents=True)
    reviewer.mkdir(parents=True)
    (alice / "profile.yaml").write_text("description: Alice mission lead profile\n", encoding="utf-8")
    (reviewer / "profile.yaml").write_text("description: Reviews release proof\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    class AgentList:
        def list_all(self):
            return [
                AgentPersona(
                    id="neko_supervisor",
                    display_name="Neko Mission Lead",
                    role="alice_supervisor",
                    model="gpt-5.5",
                    provider="openai-codex",
                    api_mode="codex_responses",
                    toolsets=["file", "search", "terminal"],
                    system_prompt_path="personas/neko/system.md",
                    hermes_profile="alice",
                )
                ]

    original_available_profile_templates = snapshot_mod.available_profile_templates
    monkeypatch.setattr(snapshot_mod, "available_profile_templates", lambda: [])
    before = build_snapshot(agent_store=AgentList())
    monkeypatch.setattr(snapshot_mod, "available_profile_templates", original_available_profile_templates)
    snap = build_snapshot(agent_store=AgentList())

    assert snap["agents"] == before["agents"]
    assert snap["agents"][0]["persona_id"] == "neko_supervisor"
    assert snap["agents"][0]["display_name"] == "Neko Mission Lead"
    assert snap["agents"][0]["hermes_profile"] == "alice"

    by_id = {item["persona_id"]: item for item in snap["available_personas"]}
    assert set(by_id) == {"profile:alice", "profile:reviewer"}
    assert by_id["profile:alice"]["template_only"] is True
    assert by_id["profile:alice"]["source"] == "hermes_profile"
    assert by_id["profile:alice"]["role"] == "profile"
    assert by_id["profile:alice"]["hermes_profile"] == "alice"
    assert by_id["profile:alice"]["display_name"] == "Alice"
    assert by_id["profile:alice"]["profile_readiness"] == "available"
    assert by_id["profile:alice"]["backs_persona_id"] == "neko_supervisor"
    assert by_id["profile:alice"]["description"] == "Alice mission lead profile"
    assert "backs_persona_id" not in by_id["profile:reviewer"]
    assert "reviewer" not in by_id


def test_snapshot_exposes_canonical_persona_instance_ids(monkeypatch, isolate_agent_runtime_root):
    import agent_runtime.snapshot as snapshot_mod

    cfg = AgentRuntimeConfig(
        enterprise_worker_sessions=EnterpriseWorkerSessionsConfig(
            enabled=True,
            worker_session_store=True,
            persona_instance_runtime=True,
            persona_assignment_store=True,
        )
    )
    monkeypatch.setattr(snapshot_mod, "load_agent_runtime_config", lambda: cfg)
    created = PersonaInstanceStore().create_free_floating("profile:reviewer")

    snap = build_snapshot()
    by_id = {item["persona_instance_id"]: item for item in snap["persona_instances"]}

    assert created.id == "personainst_profile_reviewer"
    assert by_id[created.id]["agent_profile_id"] == "personainst_profile_reviewer"
    assert by_id[created.id]["persona_id"] == "profile:reviewer"
    assert by_id[created.id]["lifecycle_mode"] == "free_floating"


def test_snapshot_links_deleted_archive_tasks(isolate_agent_runtime_root, history_in_frame_config):
    archive = isolate_agent_runtime_root / "deleted_archive" / "20260601T010203Z_clear_ready"
    (archive / "tasks").mkdir(parents=True)
    atomic_json_write(
        archive / "manifest.json",
        {"reason": "Tony cleared ready missions", "created_at_utc": "2026-06-01T01:02:03Z"},
    )
    atomic_json_write(
        archive / "tasks" / "task_archived.json",
        {"id": "task_archived", "title": "Archived mission from disk", "state": "done"},
    )

    snap = build_snapshot()

    archived = snap["archived_tasks"][0]
    assert archived["task_id"] == "task_archived"
    assert archived["title"] == "Archived mission from disk"
    assert archived["state"] == "archived"
    assert archived["original_state"] == "done"
    assert archived["archive_batch"] == "20260601T010203Z_clear_ready"
    assert archived["archive_reason"] == "Tony cleared ready missions"
    assert archived["archived_at"] == "2026-06-01T01:02:03Z"


def test_snapshot_archived_tasks_include_run_proof_and_decision_transcript(isolate_agent_runtime_root, history_in_frame_config):
    archive = isolate_agent_runtime_root / "deleted_archive" / "20260601T010203Z_clear_ready"
    (archive / "tasks").mkdir(parents=True)
    (archive / "runs").mkdir(parents=True)
    (archive / "proofs" / "task_archived").mkdir(parents=True)
    atomic_json_write(
        archive / "manifest.json",
        {"reason": "Tony cleared ready missions", "created_at_utc": "2026-06-01T01:02:03Z"},
    )
    atomic_json_write(
        archive / "tasks" / "task_archived.json",
        {"id": "task_archived", "title": "Archived mission from disk", "state": "done"},
    )
    atomic_json_write(
        archive / "runs" / "run_dev.json",
        {
            "id": "run_slot",
            "persona_id": "dev",
            "task_id": "task_archived",
            "stage_id": "stage_impl",
            "state": "completed",
            "started_at": "2026-06-01T01:03:00Z",
            "finished_at": "2026-06-01T01:05:00Z",
            "session_id": "safe_session",
            "final_decision": {
                "type": "request_test_run",
                "summary": "Implemented archived transcript mapping.",
                "rationale": "Tests are required before QA handoff.",
            },
            "llm": {
                "provider": "openai-codex",
                "model": "gpt-5.5",
                "session_id": "safe_session",
                "api_calls": 2,
                "tool_turns": 1,
                "input_tokens": 100,
                "output_tokens": 25,
                "total_tokens": 125,
                "validation_status": "valid",
                "decision_type": "request_test_run",
                "timing": {
                    "persona_runtime_ms": 118000,
                    "provider_call_ms": 90000,
                    "profile_conversation_call_total_ms": 87000,
                    "profile_provider_stream_consume_ms": 84000,
                    "unsafe-key": 1,
                    "secret": "drop",
                },
            },
        },
    )
    atomic_json_write(
        archive / "proofs" / "task_archived" / "proof_dev.json",
        {
            "id": "proof_dev",
            "task_id": "task_archived",
            "stage_id": "stage_impl",
            "type": "command",
            "created_by": "dev",
            "path_or_value": "artifact.log",
            "metadata": {"status": "passed", "exit_code": 0, "duration_ms": 321},
        },
    )
    (archive / "events_task_archived.jsonl").write_text(
        "\n".join(
            json.dumps(to_jsonable(event), separators=(",", ":"))
            for event in [
                Event(
                    ts=now(),
                    type="run.tool.finished",
                    task_id="task_archived",
                    run_id="run_slot",
                    persona_id="dev",
                    payload={
                        "tool_name": "flutter test",
                        "status": "passed",
                        "summary": "Focused archive replay proof passed.",
                        "duration_ms": 321,
                    },
                ),
                Event(
                    ts=now(),
                    type="proof.gate_checked",
                    task_id="task_archived",
                    run_id=None,
                    persona_id="qa",
                    payload={
                        "status": "passed",
                        "summary": "Proof gate accepted archived command proof.",
                    },
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    snap = build_snapshot()

    archived = snap["archived_tasks"][0]
    assert archived["runs"][0]["run_id"] == "run_slot"
    assert archived["runs"][0]["decision_summary"] == "Implemented archived transcript mapping."
    assert archived["runs"][0]["decision_rationale"] == "Tests are required before QA handoff."
    assert archived["runs"][0]["reasoning_summary"] == "Tests are required before QA handoff."
    assert archived["runs"][0]["duration_ms"] == 120000
    assert archived["runs"][0]["llm"]["total_tokens"] == 125
    assert archived["runs"][0]["llm"]["timing"] == {
        "persona_runtime_ms": 118000,
        "provider_call_ms": 90000,
        "profile_conversation_call_total_ms": 87000,
        "profile_provider_stream_consume_ms": 84000,
    }
    assert archived["persona_timing_summaries"] == [
        {
            "persona_id": "dev",
            "run_count": 1,
            "run_ids": ["run_slot"],
            "duration_ms": 120000,
            "persona_runtime_ms": 118000,
            "provider_call_ms": 90000,
            "provider_call_count": 1,
            "conversation_call_ms": 87000,
            "conversation_call_count": 1,
            "stream_consume_ms": 84000,
            "api_calls": 2,
            "tool_turns": 1,
            "total_tokens": 125,
            "input_tokens": 100,
            "output_tokens": 25,
        }
    ]
    assert archived["proof_summaries"] == [
        {
            "proof_id": "proof_dev",
            "type": "command",
            "status": "passed",
            "exit_code": 0,
            "duration_ms": 321,
            "created_by": "dev",
            "has_artifact": True,
        }
    ]
    assert [event["type"] for event in archived["recent_events"]] == [
        "run.progress",
        "run.decision",
        "proof.attached",
        "run.tool.finished",
        "proof.gate_checked",
    ]
    assert archived["recent_events"][0]["summary"] == "Agent decision process summarized"
    assert archived["recent_events"][0]["phase"] == "thinking_process"
    assert archived["recent_events"][0]["step"] == "decision_summary"
    assert archived["recent_events"][0]["reasoning_summary"] == "Tests are required before QA handoff."
    assert archived["recent_events"][1]["summary"] == "Implemented archived transcript mapping."
    assert archived["recent_events"][1]["rationale"] == "Tests are required before QA handoff."
    assert archived["recent_events"][1]["reasoning_summary"] == "Tests are required before QA handoff."
    assert archived["recent_events"][3]["tool_name"] == "flutter test"


def test_snapshot_projects_archived_goal_as_operator_channel(monkeypatch, isolate_agent_runtime_root):
    import agent_runtime.snapshot as snapshot_mod

    cfg = AgentRuntimeConfig(
        enterprise_worker_sessions=EnterpriseWorkerSessionsConfig(
            enabled=True,
            worker_session_store=True,
            persona_instance_runtime=True,
            persona_assignment_store=True,
        )
    )
    monkeypatch.setattr(snapshot_mod, "load_agent_runtime_config", lambda: cfg)
    archive = isolate_agent_runtime_root / "deleted_archive" / "20260601T010203Z_clear_ready"
    (archive / "tasks").mkdir(parents=True)
    (archive / "runs").mkdir(parents=True)
    (archive / "persona_assignments").mkdir(parents=True)
    atomic_json_write(
        archive / "manifest.json",
        {"reason": "daemon archived terminal goal", "created_at_utc": "2026-06-01T01:10:00Z"},
    )
    atomic_json_write(
        archive / "tasks" / "task_archived.json",
        {
            "id": "task_archived",
            "goal_id": "goal_archived",
            "title": "Archived Neko default graph token flow",
            "description": "Verify the terminal Neko chat still recalls.",
            "acceptance_criteria": ["Agent Console shows this goal input."],
            "state": "done",
            "created_at": "2026-06-01T01:00:00Z",
            "updated_at": "2026-06-01T01:05:00Z",
        },
    )
    atomic_json_write(
        archive / "persona_assignments" / "assign_dev.json",
        {
            "id": "assign_dev",
            "persona_id": "dev",
            "persona_instance_id": "personainst_goal_archived_dev",
            "task_id": "task_archived",
            "goal_id": "goal_archived",
            "stage_id": "implement",
            "title": "Launcher Implementation",
            "message": "Implement the scoped work and attach proof.",
            "state": "completed",
            "created_at": "2026-06-01T01:01:00Z",
            "allowed_decisions": ["deliver", "report_blocker"],
        },
    )
    atomic_json_write(
        archive / "runs" / "run_neko.json",
        {
            "id": "run_neko",
            "persona_id": "neko_supervisor",
            "task_id": "task_archived",
            "stage_id": "scope",
            "state": "completed",
            "started_at": "2026-06-01T01:00:30Z",
            "finished_at": "2026-06-01T01:02:00Z",
            "final_decision": {
                "type": "scope_route",
                "summary": "Neko routed the archived goal to Launcher Dev.",
                "rationale": "The archived operator channel should remain recallable after daemon cleanup.",
            },
        },
    )

    snap = build_snapshot()

    channel = next(
        item
        for item in snap["operator_channels"]
        if item["task_id"] == "task_archived"
    )
    conversation = channel["conversation"]
    assert channel["archived"] is True
    assert channel["persona_id"] == "neko_supervisor"
    assert conversation["status"] == "complete"
    assert conversation["goal_id"] == "goal_archived"
    assert [message["kind"] for message in conversation["messages"]] == [
        "goal_input",
        "handoff",
        "final",
    ]
    transcript = "\n".join(message["display_text"] for message in conversation["messages"])
    assert "Goal: Archived Neko default graph token flow" in transcript
    assert "Prompted dev." in transcript
    assert "Implement the scoped work and attach proof." in transcript
    assert "archived operator channel should remain recallable" in transcript


def test_snapshot_archived_typed_task_keeps_all_canonical_role_streams_visible(isolate_agent_runtime_root, history_in_frame_config):
    archive = isolate_agent_runtime_root / "deleted_archive" / "20260601T010203Z_clear_ready"
    (archive / "tasks").mkdir(parents=True)
    (archive / "runs").mkdir(parents=True)
    atomic_json_write(
        archive / "manifest.json",
        {"reason": "Tony cleared ready missions", "created_at_utc": "2026-06-01T01:02:03Z"},
    )
    atomic_json_write(
        archive / "tasks" / "task_archived.json",
        {
            "id": "task_archived",
            "title": "Archived typed mission",
            "state": "cancelled",
            "created_at": "2026-06-01T01:02:00Z",
            "updated_at": "2026-06-01T01:05:00Z",
            "current_stage_id": "launcher_impl",
            "mission_plan": {
                "enabled": True,
                "current_stage_id": "launcher_impl",
                "stages": [
                    {"id": "backend_contract", "owner": "backend_dev", "status": "ready"},
                    {"id": "launcher_impl", "owner": "dev", "status": "implementing"},
                    {"id": "qa_release", "owner": "qa", "status": "ready"},
                ],
            },
        },
    )
    atomic_json_write(
        archive / "runs" / "run_neko.json",
        {
            "id": "run_neko",
            "persona_id": "neko_supervisor",
            "task_id": "task_archived",
            "state": "completed",
            "started_at": "2026-06-01T01:03:00Z",
            "finished_at": "2026-06-01T01:04:00Z",
            "final_decision": {
                "type": "propose_acceptance",
                "summary": "Scoped the typed mission.",
            },
        },
    )

    archived = build_snapshot()["archived_tasks"][0]
    roles = {stream["persona_id"]: stream for stream in archived["role_streams"]}

    assert {"neko_supervisor", "backend_dev", "dev", "qa"}.issubset(roles)
    assert roles["neko_supervisor"]["events"][0]["type"] == "run.progress"
    assert roles["neko_supervisor"]["events"][0]["payload"]["display_kind"] == "thinking_summary"
    assert roles["neko_supervisor"]["events"][1]["type"] == "run.decision"
    assert roles["backend_dev"]["events"][0]["type"] == "role_stream.status"
    assert roles["backend_dev"]["events"][0]["payload"]["display_title"] == "Backend Dev Agent archived"
    assert roles["dev"]["events"][0]["payload"]["redaction_status"] == "safe"
    assert roles["qa"]["events"][0]["payload"]["display_title"] == "QA Agent archived"


def test_snapshot_masks_secret_assignments_but_keeps_pathful_decision_text(isolate_agent_runtime_root, history_in_frame_config):
    """Decision text is operator-grade: paths survive verbatim; only
    secret-shaped assignments are masked, in place, without nulling the rest
    of the rationale (the old behavior dropped the whole text and starved the
    conversation projection of thinking/turn detail)."""

    archive = isolate_agent_runtime_root / "deleted_archive" / "20260601T010203Z_clear_ready"
    (archive / "tasks").mkdir(parents=True)
    (archive / "runs").mkdir(parents=True)
    atomic_json_write(archive / "manifest.json", {"reason": "Tony cleared ready missions"})
    atomic_json_write(archive / "tasks" / "task_archived.json", {"id": "task_archived", "title": "Archived", "state": "done"})
    atomic_json_write(
        archive / "runs" / "run_dev.json",
        {
            "id": "run_slot",
            "persona_id": "dev",
            "task_id": "task_archived",
            "state": "completed",
            "final_decision": {
                "type": "request_test_run",
                "summary": "Edited docs/scratch/goal_turn_probe.md and reran the proof.",
                "rationale": "Wrote docs/scratch/goal_turn_probe.md then exported api_key=sk-live-12345 for the check.",
                "reasoning_summary": "Compared lib/features/mission_control widgets before patching.",
            },
        },
    )

    archived = build_snapshot()["archived_tasks"][0]
    run = archived["runs"][0]

    # Paths are content on the operator surface — they must survive.
    assert run["decision_summary"] == "Edited docs/scratch/goal_turn_probe.md and reran the proof."
    assert run["reasoning_summary"] == "Compared lib/features/mission_control widgets before patching."
    # Secret assignments are masked in place; the surrounding rationale stays.
    assert "docs/scratch/goal_turn_probe.md" in run["decision_rationale"]
    assert "[redacted secret]" in run["decision_rationale"]
    assert "sk-live-12345" not in repr(archived)
