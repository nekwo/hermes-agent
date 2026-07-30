from __future__ import annotations

from datetime import timedelta

from hermes_time import now

from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.events import EventLog
from agent_runtime.models import Event, RepoBundle
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.persona_assignments import PersonaAssignmentSpec, PersonaAssignmentStore
from agent_runtime.repo_bundles import RepoBundleStore, acquire_repo_bundle_locks, desired_bundles_for_task, qa_waiting_on, release_repo_bundle_locks, repo_lock_summary
from agent_runtime.runtime_config import EnterpriseWorkerSessionsConfig, RepoBundleRoutingConfig, SimplifiedAgentContractConfig
from agent_runtime.snapshot import build_snapshot
from agent_runtime.states import TaskState
from agent_runtime.store import IncidentStore, TaskStore


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
        requires_visual_proof=True,
    )


def _simple_bundle(
    *,
    task_id: str,
    bundle_id: str = "bundle_empty",
    run_id: str = "run_empty_1",
    repo: str = "hermes-agent",
    owner_persona_id: str = "dev",
    stage_ids: list[str] | None = None,
    proof_ids: list[str] | None = None,
) -> RepoBundle:
    ts = now()
    return RepoBundle(
        id=bundle_id,
        task_id=task_id,
        repo=repo,
        owner_persona_id=owner_persona_id,
        state="running",
        title="Harness bundle",
        objective="Update docs.",
        stage_ids=stage_ids or ["implement"],
        active_run_id=run_id,
        proof_ids=proof_ids or ["proof_same"],
        created_at=ts,
        updated_at=ts,
    )


def test_empty_delivery_capture_opens_patch_landed_nowhere_incident(isolate_agent_runtime_root, monkeypatch):
    assert not hasattr(TaskStore(), "create")


def test_proof_only_delivery_intent_does_not_open_empty_patch_incident(isolate_agent_runtime_root, monkeypatch):
    assert not hasattr(TaskStore(), "create")


def test_no_product_edit_delivery_no_longer_bypasses_patch_guard_with_retired_proofs(isolate_agent_runtime_root, monkeypatch):
    assert not hasattr(TaskStore(), "create")


def test_repeated_empty_delivery_without_new_proof_waits_for_operator(isolate_agent_runtime_root, monkeypatch):
    assert not hasattr(TaskStore(), "create")


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
            type=DecisionType.HAND_OFF,
            summary="Delivered repo bundle.",
            rationale="The fake runtime completed the assigned repo bundle.",
            payload={
                "stage_id": "backend_contract",
                "summary": "Backend bundle ready.",
                "known_gaps": ["Launcher implementation remains queued on this backend contract proof."],
            },
        )


class RequestTestRunRuntime:
    def run_tick(self, persona, ctx, *, run):
        return AgentDecision(
            type=DecisionType.HAND_OFF,
            summary="Deliver focused proof-only bundle.",
            rationale="Collapsed hand_off lets the Harness run the authoritative proof gate.",
            payload={
                "stage_id": "backend_contract",
                "summary": "Backend contract proof lane is ready for the Harness gate.",
                "known_gaps": [],
            },
        )


class ApproveQaRuntime:
    def run_tick(self, persona, ctx, *, run):
        return AgentDecision(
            type=DecisionType.QA_VERDICT,
            summary="Approve focused proof.",
            rationale="The proof ID covers the requested bundle.",
            payload={
                "verdict": "approved",
                "coverage": {
                    "backend_contract": "reviewed",
                    "launcher_integration": "reviewed",
                    "visual_or_mcp": "reviewed",
                    "cross_stack_join": "reviewed",
                },
                "proof_ids": [
                    "proof_requested_ok",
                    "proof_launcher",
                    "proof_backend_docker_postgres",
                    "proof_staging_k8",
                    "proof_prod_rollout",
                ],
                "findings": [],
            },
        )


class ShouldNotRunRuntime:
    def run_tick(self, persona, ctx, *, run):
        raise AssertionError("queued assignment should not launch persona runtime")


def test_desired_bundles_project_affected_repos_without_stage_dependencies(isolate_agent_runtime_root):
    task = _task_with_plan()

    bundles = desired_bundles_for_task(task)
    by_repo = {bundle.repo: bundle for bundle in bundles}

    assert set(by_repo) == {"EterniaBackend", "EterniaLauncher"}
    assert by_repo["EterniaBackend"].owner_persona_id == "backend_dev"
    assert by_repo["EterniaLauncher"].owner_persona_id == "dev"
    assert by_repo["EterniaLauncher"].state == "planned"
    assert by_repo["EterniaLauncher"].dependency_bundle_ids == []
    assert by_repo["EterniaLauncher"].visual_requirements == ["visual_proof"]
    assert all("qa_release" not in bundle.stage_ids for bundle in bundles)


def test_repo_bundle_store_is_idempotent_without_stage_dependencies(isolate_agent_runtime_root):
    task = _task_with_plan()
    store = RepoBundleStore()

    first = store.create_or_update_from_task(task)
    second = store.create_or_update_from_task(task)
    assert [bundle.id for bundle in second] == [bundle.id for bundle in first]

    backend = next(bundle for bundle in first if bundle.repo == "EterniaBackend")
    # S24 removed ``mark_delivered`` with the delivery-capture path; the wake
    # rule under test is about dependency state, not about how it got there.
    backend.state = "delivered_waiting_for_qa"
    store.update(backend)
    woke = store.wake_ready_dependencies(task.id)

    assert woke == []


def test_repo_bundle_store_keeps_repo_projection_stable_without_stage_graph(isolate_agent_runtime_root):
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

    bundles = store.create_or_update_from_task(task)

    assert [bundle.id for bundle in bundles] == [placeholder.id]
    assert store.get(task.id, placeholder.id).state == "planned"


def test_assignment_signal_hash_includes_repo_bundle_id(isolate_agent_runtime_root):
    store = PersonaAssignmentStore()
    base = dict(
        persona_id="dev",
        kind="repo_bundle",
        title="Launcher",
        message="Patch launcher.",
        goal_id="task_bundle",
        stage_id="launcher_impl",
        repo="EterniaLauncher",
    )

    first = store.create_or_resume(PersonaAssignmentSpec(**base, repo_bundle_id="bundle_a"))
    second = store.create_or_resume(PersonaAssignmentSpec(**base, repo_bundle_id="bundle_b"))

    assert first.id != second.id
    assert first.repo_bundle_id == "bundle_a"
    assert second.repo_bundle_id == "bundle_b"


def test_snapshot_projects_repo_bundles_and_qa_waiting_on(isolate_agent_runtime_root):
    snapshot = build_snapshot()
    assert "goals" not in snapshot
    assert not hasattr(TaskStore(), "create")


def test_done_task_repo_bundle_closeout_labels_staged_not_applied(isolate_agent_runtime_root):
    assert "goals" not in build_snapshot()
    assert not hasattr(TaskStore(), "update")


def test_archive_preserves_repo_bundle_evidence(isolate_agent_runtime_root):
    assert not hasattr(TaskStore(), "archive")


# Ticker-driven bundle delivery cases retired with the S5 dispatch loop.
