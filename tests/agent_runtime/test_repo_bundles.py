from __future__ import annotations

from datetime import timedelta

from hermes_time import now

from agent_runtime.actions import HarnessAction, HarnessActionType
from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.models import MissionIntent, MissionPlan, MissionPlanStage, Proof, Task
from agent_runtime.persona_assignments import PersonaAssignmentSpec, PersonaAssignmentStore
from agent_runtime.repo_bundles import RepoBundleStore, acquire_repo_bundle_locks, desired_bundles_for_task, qa_waiting_on, release_repo_bundle_locks, repo_lock_summary
from agent_runtime.runtime_config import EnterpriseWorkerSessionsConfig, RepoBundleRoutingConfig, SimplifiedAgentContractConfig
from agent_runtime.snapshot import build_snapshot
from agent_runtime.proof_rules import ProofType
from agent_runtime.states import StageStatus, TaskState
from agent_runtime.store import ProofStore, TaskStore
from agent_runtime.ticker import TickEngine


def _task_with_plan(task_id: str = "task_bundle") -> Task:
    ts = now()
    return Task(
        id=task_id,
        title="Cross repo mission",
        description="Patch backend and launcher.",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        affected_repos=["EterniaBackend", "EterniaLauncher"],
        current_stage_id="launcher_impl",
        mission_plan=MissionPlan(
            mission_intent=MissionIntent(title="Cross repo mission", objective="Patch backend and launcher."),
            current_stage_id="launcher_impl",
            stages=[
                MissionPlanStage(
                    id="backend_contract",
                    title="Backend contract",
                    objective="Update backend API contract.",
                    owner="backend_dev",
                    repo="EterniaBackend",
                    kind="implementation",
                    status=StageStatus.READY,
                    proof_recipe_id="backend_contract_smoke",
                ),
                MissionPlanStage(
                    id="launcher_impl",
                    title="Launcher implementation",
                    objective="Consume the backend contract.",
                    owner="dev",
                    repo="EterniaLauncher",
                    kind="implementation",
                    status=StageStatus.READY,
                    depends_on=["backend_contract"],
                    requires_visual_proof=True,
                ),
            ],
        ),
    )


def test_repo_bundle_write_lock_conflict_parks_second_lane(isolate_agent_runtime_root):
    first = acquire_repo_bundle_locks(lane_id="lane_1", task_id="task_1", bundle_ids=["bundle_backend"], mode="write")
    second = acquire_repo_bundle_locks(lane_id="lane_2", task_id="task_2", bundle_ids=["bundle_backend"], mode="write")

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["park_state"] == "parked_by_repo_lock"
    assert second["conflicts"][0]["owner_lane_id"] == "lane_1"
    assert repo_lock_summary()["lock_count"] == 1

    released = release_repo_bundle_locks(lane_id="lane_1")
    assert released["released_count"] == 1


def test_playground_lane_cannot_acquire_write_lock_by_default(isolate_agent_runtime_root):
    result = acquire_repo_bundle_locks(lane_id="lane_play", task_id="task_play", bundle_ids=["bundle_backend"], mode="write", lane_kind="playground")

    assert result["ok"] is False
    assert result["reason"] == "playground lanes are read-locked by default"


def _bundle_config() -> AgentRuntimeConfig:
    return AgentRuntimeConfig(
        repo_bundle_routing=RepoBundleRoutingConfig(enabled=True),
        simplified_agent_contract=SimplifiedAgentContractConfig(enabled=True),
        enterprise_worker_sessions=EnterpriseWorkerSessionsConfig(
            enabled=True,
            persona_instance_runtime=True,
            persona_assignment_store=True,
        ),
    )


class CompleteDevRuntime:
    def run_tick(self, persona, ctx, *, run):
        return AgentDecision(
            type=DecisionType.REQUEST_QA_REVIEW,
            summary="Delivered repo bundle.",
            rationale="The fake runtime completed the assigned repo bundle.",
            payload={
                "stage_id": "backend_contract",
                "proof_ids": ["proof_backend"],
                "handoff": {"to": "qa", "stage_complete": True, "summary": "Backend bundle ready."},
            },
        )


class RequestTestRunRuntime:
    def run_tick(self, persona, ctx, *, run):
        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="Request focused proof.",
            rationale="Use Harness proof for the current proof-only bundle.",
            payload={"stage_id": "backend_contract", "commands": ["python -c \"print('ok')\""]},
        )


class ApproveQaRuntime:
    def run_tick(self, persona, ctx, *, run):
        return AgentDecision(
            type=DecisionType.REPORT_QA_VERDICT,
            summary="Approve focused proof.",
            rationale="The proof ID covers the requested bundle.",
            payload={
                "review_scope": "implementation",
                "verdict": "approved",
                "proof_ids": ["proof_requested_ok"],
                "findings": [],
            },
        )


class PassingProofRunner:
    def __init__(self, proof_store: ProofStore):
        self.proof_store = proof_store

    def run_commands(self, task, *, stage_id, run_id, actor, commands, **_kwargs):
        proof = Proof(
            id="proof_requested_ok",
            task_id=task.id,
            stage_id=stage_id,
            type=ProofType.TEST_RUN,
            title="Requested proof",
            path_or_value="proof.log",
            created_by="harness",
            created_at=now(),
            metadata={"status": "passed", "run_id": run_id, "command": ".EterniaBackendVirtualEnv/Scripts/python.exe manage.py check"},
            redaction_status="safe",
        )
        return [self.proof_store.attach(proof)]


def _attach_product_promotion_proofs(task: Task, proof_store: ProofStore, *, after):
    proofs = [
        Proof(
            id="proof_backend_docker_postgres",
            task_id=task.id,
            stage_id="qa_release",
            type=ProofType.TEST_RUN,
            title="Backend Docker/PostgreSQL validation",
            path_or_value="docker-postgres.log",
            created_by="harness",
            created_at=after + timedelta(seconds=30),
            metadata={"status": "passed", "command": "scripts/test.sh # local Docker Compose PostgreSQL tier"},
            redaction_status="safe",
        ),
        Proof(
            id="proof_staging_k8",
            task_id=task.id,
            stage_id="qa_release",
            type=ProofType.TEST_RUN,
            title="Staging k8 validation",
            path_or_value="staging.log",
            created_by="harness",
            created_at=after + timedelta(minutes=1),
            metadata={"status": "passed", "command": "kubectl -n staging rollout status deploy/eternia-backend"},
            redaction_status="safe",
        ),
        Proof(
            id="proof_prod_rollout",
            task_id=task.id,
            stage_id="qa_release",
            type=ProofType.TEST_RUN,
            title="Production pod rollout",
            path_or_value="prod.log",
            created_by="harness",
            created_at=after + timedelta(minutes=2),
            metadata={"status": "passed", "command": "kubectl -n prod rollout status deploy/eternia-backend", "proof_intent": "prod_rollout"},
            redaction_status="safe",
        ),
    ]
    for proof in proofs:
        proof_store.attach(proof)
        task.proof_ids.append(proof.id)


class ShouldNotRunRuntime:
    def run_tick(self, persona, ctx, *, run):
        raise AssertionError("queued assignment should not launch persona runtime")


def test_desired_bundles_group_by_repo_and_queue_dependencies(isolate_agent_runtime_root):
    task = _task_with_plan()

    bundles = desired_bundles_for_task(task)
    by_repo = {bundle.repo: bundle for bundle in bundles}

    assert set(by_repo) == {"EterniaBackend", "EterniaLauncher"}
    assert by_repo["EterniaBackend"].owner_persona_id == "backend_dev"
    assert by_repo["EterniaLauncher"].owner_persona_id == "dev"
    assert by_repo["EterniaLauncher"].state == "queued_waiting_dependency"
    assert by_repo["EterniaLauncher"].dependency_bundle_ids == [by_repo["EterniaBackend"].id]
    assert by_repo["EterniaLauncher"].visual_requirements == ["visual_proof"]
    assert all("qa_release" not in bundle.stage_ids for bundle in bundles)


def test_repo_bundle_store_is_idempotent_and_wakes_dependencies(isolate_agent_runtime_root):
    task = _task_with_plan()
    store = RepoBundleStore()

    first = store.create_or_update_from_task(task)
    second = store.create_or_update_from_task(task)
    assert [bundle.id for bundle in second] == [bundle.id for bundle in first]

    backend = next(bundle for bundle in first if bundle.repo == "EterniaBackend")
    launcher = next(bundle for bundle in first if bundle.repo == "EterniaLauncher")
    store.mark_delivered(backend, proof_ids=["proof_backend"])
    woke = store.wake_ready_dependencies(task.id)

    assert [bundle.id for bundle in woke] == [launcher.id]
    assert store.get(task.id, launcher.id).state == "planned"


def test_repo_bundle_store_cancels_preplan_placeholder_after_mission_plan(isolate_agent_runtime_root):
    ts = now()
    task = Task(
        id="task_supersede",
        title="Before plan",
        description="No plan yet.",
        state=TaskState.CREATED,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        affected_repos=["hermes-agent"],
    )
    store = RepoBundleStore()
    placeholder = store.create_or_update_from_task(task)[0]
    task.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title=task.title, objective=task.description),
        current_stage_id="stage_dev",
        stages=[
            MissionPlanStage(
                id="stage_dev",
                title="Dev proof",
                objective="Run focused proof.",
                owner="dev",
                repo="hermes-agent",
                kind="proof_only",
            ),
            MissionPlanStage(
                id="qa_release",
                title="QA",
                objective="Review proof.",
                owner="qa",
                repo="hermes-agent",
                kind="qa_verdict",
                depends_on=["stage_dev"],
            ),
        ],
    )

    bundles = store.create_or_update_from_task(task)

    assert len([bundle for bundle in bundles if bundle.state != "cancelled"]) == 1
    assert store.get(task.id, placeholder.id).state == "cancelled"


def test_assignment_signal_hash_includes_repo_bundle_id(isolate_agent_runtime_root):
    store = PersonaAssignmentStore()
    base = dict(
        persona_id="dev",
        kind="repo_bundle",
        title="Launcher",
        message="Patch launcher.",
        task_id="task_bundle",
        stage_id="launcher_impl",
        repo="EterniaLauncher",
    )

    first = store.create_or_resume(PersonaAssignmentSpec(**base, repo_bundle_id="bundle_a"))
    second = store.create_or_resume(PersonaAssignmentSpec(**base, repo_bundle_id="bundle_b"))

    assert first.id != second.id
    assert first.repo_bundle_id == "bundle_a"
    assert second.repo_bundle_id == "bundle_b"


def test_snapshot_projects_repo_bundles_and_qa_waiting_on(isolate_agent_runtime_root):
    task_store = TaskStore()
    task = task_store.create(_task_with_plan())
    bundles = RepoBundleStore().create_or_update_from_task(task)

    snapshot = build_snapshot(task_store=task_store)
    task_summary = snapshot["tasks"][0]

    assert task_summary["simplified_phase"] == "working"
    assert sorted(task_summary["repo_bundle_ids"]) == sorted(bundle.id for bundle in bundles)
    assert task_summary["bundle_queue"][0]["state"] == "queued_waiting_dependency"
    assert task_summary["qa_waiting_on"]
    assert snapshot["repo_bundles"]


def test_archive_preserves_repo_bundle_evidence(isolate_agent_runtime_root):
    task_store = TaskStore()
    task = _task_with_plan("task_archive_bundle")
    task.state = TaskState.DONE
    task_store.create(task)
    bundles = RepoBundleStore().create_or_update_from_task(task)

    result = task_store.archive(task.id, actor="cli", reason="bundle archive proof")
    archived = result["archived_tasks"][0]
    archive_dir = result["archive_dir"]

    assert archived["repo_bundles_archived"] is True
    assert sorted(archived["repo_bundle_ids"]) == sorted(bundle.id for bundle in bundles)
    assert archive_dir


def test_ticker_links_repo_bundle_to_assignment_and_marks_delivery(isolate_agent_runtime_root):
    task_store = TaskStore()
    task_data = _task_with_plan()
    task_data.current_stage_id = "backend_contract"
    task_data.mission_plan.current_stage_id = "backend_contract"
    task = task_store.create(task_data)
    ProofStore().attach(
        Proof(
            id="proof_backend",
            task_id=task.id,
            stage_id="backend_contract",
            type=ProofType.TEST_RUN,
            title="Backend proof",
            path_or_value="proof.log",
            created_by="harness",
            created_at=now(),
            metadata={"status": "passed"},
            redaction_status="safe",
        )
    )
    config = _bundle_config()

    result = TickEngine(task_store=task_store, persona_runtime=CompleteDevRuntime(), config=config)._execute_action(
        HarnessAction(HarnessActionType.RUN_SLOT, task_id=task.id, reason="run launcher bundle", slot_id="dev"),
        task,
    )

    assert result.ok is True
    assignments = PersonaAssignmentStore().list_for_task(task.id)
    assert len(assignments) == 1
    assert assignments[0].repo_bundle_id
    bundle = RepoBundleStore().get(task.id, assignments[0].repo_bundle_id)
    assert bundle.assignment_id == assignments[0].id
    assert bundle.state == "delivered_waiting_for_qa"
    assert qa_waiting_on(RepoBundleStore().list_for_task(task.id))


def test_ticker_does_not_launch_dev_run_for_queued_dependency_bundle(isolate_agent_runtime_root):
    task_store = TaskStore()
    task_data = _task_with_plan("task_queued_dev")
    task_data.current_stage_id = "launcher_impl"
    task = task_store.create(task_data)
    config = _bundle_config()

    result = TickEngine(task_store=task_store, persona_runtime=ShouldNotRunRuntime(), config=config)._execute_action(
        HarnessAction(HarnessActionType.RUN_SLOT, task_id=task.id, reason="run queued launcher bundle", slot_id="dev"),
        task,
    )

    assert result.ok is True
    assert "queued waiting for dependency" in result.summary
    assert result.payload["repo_bundle_id"]
    assert PersonaAssignmentStore().list_for_task(task.id)


def test_ticker_does_not_launch_qa_run_while_repo_bundle_waiting(isolate_agent_runtime_root):
    task_store = TaskStore()
    task_data = _task_with_plan("task_queued_qa")
    task_data.current_stage_id = "qa_release"
    task = task_store.create(task_data)
    RepoBundleStore().create_or_update_from_task(task)
    config = _bundle_config()

    result = TickEngine(task_store=task_store, persona_runtime=ShouldNotRunRuntime(), config=config)._execute_action(
        HarnessAction(HarnessActionType.RUN_SLOT, task_id=task.id, reason="qa waits on bundles", slot_id="qa"),
        task,
    )

    assert result.ok is True
    assert "QA queued waiting" in result.summary
    assert result.payload["qa_waiting_on"]


def test_ticker_marks_request_test_run_bundle_delivered_after_passing_proof(isolate_agent_runtime_root):
    task_store = TaskStore()
    task_data = _task_with_plan("task_request_test_bundle")
    task_data.current_stage_id = "backend_contract"
    task_data.mission_plan.current_stage_id = "backend_contract"
    task = task_store.create(task_data)
    proof_store = ProofStore()

    result = TickEngine(
        task_store=task_store,
        proof_store=proof_store,
        persona_runtime=RequestTestRunRuntime(),
        proof_runner=PassingProofRunner(proof_store),
        config=_bundle_config(),
    )._execute_action(
        HarnessAction(HarnessActionType.RUN_SLOT, task_id=task.id, reason="run backend bundle", slot_id="backend_dev"),
        task,
    )

    assert result.ok is True
    assignment = PersonaAssignmentStore().list_for_task(task.id)[0]
    bundle = RepoBundleStore().get(task.id, assignment.repo_bundle_id)
    assert bundle.state == "delivered_waiting_for_qa"
    assert bundle.proof_ids == ["proof_requested_ok"]


def test_qa_review_does_not_regress_delivered_bundle_to_running(isolate_agent_runtime_root):
    task_store = TaskStore()
    task_data = _task_with_plan("task_qa_bundle")
    task_data.current_stage_id = "backend_contract"
    task_data.mission_plan.current_stage_id = "backend_contract"
    task = task_store.create(task_data)
    proof_store = ProofStore()
    dev_engine = TickEngine(
        task_store=task_store,
        proof_store=proof_store,
        persona_runtime=RequestTestRunRuntime(),
        proof_runner=PassingProofRunner(proof_store),
        config=_bundle_config(),
    )
    dev_result = dev_engine._execute_action(
        HarnessAction(HarnessActionType.RUN_SLOT, task_id=task.id, reason="run backend bundle", slot_id="backend_dev"),
        task,
    )
    assert dev_result.ok is True
    bundle_store = RepoBundleStore()
    for bundle in bundle_store.list_for_task(task.id):
        if bundle.repo != "EterniaBackend":
            bundle_store.mark_delivered(bundle, proof_ids=["proof_launcher"])
    task = task_store.get(task.id)
    local_proof = proof_store.get("proof_requested_ok")
    _attach_product_promotion_proofs(task, proof_store, after=local_proof.created_at)
    task.current_stage_id = "qa_release"
    task.mission_plan.current_stage_id = "qa_release"
    task_store.update(task, actor="test", reason="advance to QA")

    qa_result = TickEngine(
        task_store=task_store,
        proof_store=proof_store,
        persona_runtime=ApproveQaRuntime(),
        config=_bundle_config(),
    )._execute_action(
        HarnessAction(HarnessActionType.RUN_SLOT, task_id=task.id, reason="qa verify bundle", slot_id="qa"),
        task_store.get(task.id),
    )

    assert qa_result.ok is True
    bundles = RepoBundleStore().list_for_task(task.id)
    backend_bundle = next(bundle for bundle in bundles if bundle.repo == "EterniaBackend")
    assert backend_bundle.state == "verified"
    assert backend_bundle.active_run_id is None
