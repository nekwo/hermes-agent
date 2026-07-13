from hermes_time import now

from agent_runtime.events import EventLog
from agent_runtime.models import Proof, Task
from agent_runtime.proof_rules import ProofType
from agent_runtime.runtime_instances import GoalRuntimeInstanceStore
from agent_runtime.states import TaskState
from agent_runtime.store import ProofStore, TaskStore


def make_task(task_id="task_lane"):
    ts = now()
    task = Task(
        id=task_id,
        title="Lane task",
        description="d",
        state=TaskState.CREATED,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
    )
    TaskStore().create(task)
    return task


def test_burn_in_driver_emits_daemon_lifecycle_events(isolate_agent_runtime_root):
    from agent_runtime.burn_in import _run_burn_in_until_boundary
    from agent_runtime.store import IncidentStore, RunStore

    class SettledEngine:
        config = None

        def run_until_settled(self, *, task_id=None, max_actions=None):
            from datetime import datetime, timezone

            from agent_runtime.ticker import RunUntilSettledResult

            return RunUntilSettledResult(
                settle_id="settle_1",
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
                ticks=1,
                actions_taken=[],
                stop_reason="task_terminal",
                task_id=task_id,
            )

    make_task("task_burnlife")
    _run_burn_in_until_boundary(
        SettledEngine(),
        task_id="task_burnlife",
        max_actions=2,
        run_store=RunStore(),
        incident_store=IncidentStore(),
    )

    types = [event.type for event in EventLog().for_task("task_burnlife", limit=0)]
    assert "daemon.started" in types
    assert "daemon.stopped" in types


def test_mission_daemon_emits_global_lane_lifecycle_events(isolate_agent_runtime_root):
    from datetime import datetime, timezone

    from agent_runtime.daemon import MissionDaemon
    from agent_runtime.ticker import RunUntilSettledResult

    class SettledEngine:
        def run_until_settled(self, *, task_id=None, max_actions=None):
            return RunUntilSettledResult(
                settle_id="settle_1",
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
                ticks=1,
                actions_taken=[],
                stop_reason="task_terminal",
                task_id=task_id,
            )

    make_task("task_daemonlife")
    MissionDaemon(engine_factory=SettledEngine, target_task_id="task_daemonlife", interval_seconds=0, idle_interval_seconds=0).run_foreground(max_loops=2)

    types = [event.type for event in EventLog().tail(20)]
    assert "daemon.started" in types
    assert "daemon.stopped" in types


def test_proof_attach_defaults_lane_attribution(isolate_agent_runtime_root):
    task = make_task("task_proofattr")
    instance = GoalRuntimeInstanceStore().create_lane(task_id=task.id, started_by="test", state="running")
    ts = now()
    proof = Proof(
        id="proof_lane_1",
        task_id=task.id,
        stage_id="stage_1",
        type=ProofType.TEST_RUN,
        title="t",
        path_or_value="proof.txt",
        created_by="dev",
        created_at=ts,
        metadata={"status": "passed"},
    )

    ProofStore().attach(proof)

    assert proof.metadata.get("lane_id") == instance.id


def test_proof_attach_keeps_caller_lane_attribution(isolate_agent_runtime_root):
    task = make_task("task_proofattr2")
    GoalRuntimeInstanceStore().create_lane(task_id=task.id, started_by="test", state="running")
    ts = now()
    proof = Proof(
        id="proof_lane_2",
        task_id=task.id,
        stage_id="stage_1",
        type=ProofType.TEST_RUN,
        title="t",
        path_or_value="proof.txt",
        created_by="dev",
        created_at=ts,
        metadata={"status": "passed", "lane_id": "lane_explicit"},
    )

    ProofStore().attach(proof)

    assert proof.metadata.get("lane_id") == "lane_explicit"


def test_burn_case_task_captures_repo_clean_baseline(isolate_agent_runtime_root):
    from agent_runtime.burn_in import STAGE47_CASES, _create_case_task

    hygiene = {
        "dirty_state_after_cleanup": {
            "repos": [
                {"label": "EterniaBackend", "dirty": True, "dirty_count": 9, "error": None, "status_excerpt": [" M media/tests.py"]},
            ]
        }
    }

    task = _create_case_task("noop-orchestration", STAGE47_CASES["noop-orchestration"], hygiene=hygiene)

    baseline = task.harness_self_heal.get("repo_clean_baseline")
    assert baseline and baseline["repos"][0]["label"] == "EterniaBackend"
    assert baseline["repos"][0]["dirty_count"] == 9


def test_progress_hash_ignores_fresh_proof_ids_and_checklist_revision(isolate_agent_runtime_root):
    from agent_runtime.role_envelopes import _progress_hash

    payload = {"stage_id": "s1", "commands": ["pytest -q tests/foo.py"]}
    first = _progress_hash(decision_type="request_test_run", proof_ids=["proof_a"], checklist_revision=1, payload=payload)
    second = _progress_hash(decision_type="request_test_run", proof_ids=["proof_b"], checklist_revision=2, payload=payload)
    different = _progress_hash(decision_type="request_test_run", proof_ids=["proof_b"], checklist_revision=2, payload={"stage_id": "s1", "commands": ["pytest -q tests/bar.py"]})

    assert first == second
    assert first != different


def test_handoff_payload_does_not_synthesize_no_edit_cross_stack_launcher_leg(isolate_agent_runtime_root):
    from agent_runtime.burn_in import STAGE47_CASES, _create_case_task
    from agent_runtime.mission_plan import ensure_mission_plan

    task = _create_case_task("noop-orchestration", STAGE47_CASES["noop-orchestration"], hygiene=None)
    payload = {
        "objective": task.description,
        "handoff_packet": {
            "target_owner": "backend_dev",
            "target_repo": "EterniaBackend",
            "handoff_mode": "no_product_edit_proof",
            "proof_gate": {"required": True, "required_proof_types": ["test_run"], "minimum_status": "passed"},
        },
    }

    plan = ensure_mission_plan(task, payload)

    assert plan.blueprint_id == "neko_two_dev_default"
    assert [stage.id for stage in plan.stages] == ["scope", "backend_implementation", "implement"]
    assert not [stage for stage in plan.stages if stage.id == "launcher_implementation"]


def test_automated_unblock_is_not_manual_intervention(isolate_agent_runtime_root):
    from types import SimpleNamespace

    from agent_runtime.burn_in import _manual_intervention_counts

    events = [
        SimpleNamespace(type="task.unblocked", payload={"actor": "neko_supervisor", "reason": "recovery"}),
        SimpleNamespace(type="task.unblocked", payload={"actor": "harness", "reason": "re-arm"}),
        SimpleNamespace(type="task.unblocked", payload={"actor": "cli", "reason": "operator unblock"}),
    ]

    counts = _manual_intervention_counts(events)

    assert counts["task_unblocks"] == 1


def test_queued_bundle_with_delivered_dependency_wakes_at_schedule_gate(isolate_agent_runtime_root):
    from agent_runtime.repo_bundles import RepoBundleStore
    from agent_runtime.models import RepoBundle

    task = make_task("task_wake")
    store = RepoBundleStore()
    ts = now()
    backend = RepoBundle(
        id="bundle_backend",
        task_id=task.id,
        repo="EterniaBackend",
        title="Backend",
        objective="o",
        owner_persona_id="backend_dev",
        state="delivered_waiting_for_qa",
        created_at=ts,
        updated_at=ts,
    )
    launcher = RepoBundle(
        id="bundle_launcher",
        task_id=task.id,
        repo="EterniaLauncher",
        title="Launcher",
        objective="o",
        owner_persona_id="dev",
        state="queued_waiting_dependency",
        dependency_bundle_ids=["bundle_backend"],
        created_at=ts,
        updated_at=ts,
    )
    store._write(backend)
    store._write(launcher)

    woke = store.wake_ready_dependencies(task.id)

    assert [bundle.id for bundle in woke] == ["bundle_launcher"]
    assert store.get(task.id, "bundle_launcher").state == "planned"
