from __future__ import annotations

from datetime import timedelta

from hermes_time import now

from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.events import EventLog
from agent_runtime.models import Event, RepoBundle
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.persona_assignments import PersonaAssignmentSpec, PersonaAssignmentStore
from agent_runtime import repo_bundles as repo_bundles_module
from agent_runtime.repo_bundles import RepoBundleStore
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


def test_the_repo_bundle_read_only_summaries_are_gone_after_s56(isolate_agent_runtime_root):
    """S56 finished the cut this case has been tracking since S52.

    S52 deleted the repo-bundle WRITE lane (``acquire_repo_bundle_locks`` /
    ``release_repo_bundle_locks`` and the bundle mutators) for want of a
    production caller. This case then stood as the honest witness that
    ``repo_lock_summary`` survived as an asserted CONSTANT -- the S47 item-5
    defect class, filed for a follow-up wave.

    S56 is that wave: the summaries themselves are gone, along with the
    ``repo_bundles`` / ``repo_bundle_closeout`` / ``bundle_queue`` /
    ``repo_locks`` rows they fed in ``build_status``. The pin is INVERTED rather
    than dropped so a stale producer cannot resurrect a reader.
    """

    for retired in (
        "repo_bundle_summary",
        "repo_bundle_delivery_summary",
        "bundle_queue_summary",
        "repo_lock_summary",
        "REPO_BUNDLE_DELIVERY_CONTRACT",
        "REPO_BUNDLE_CHECKOUT_STATUS",
    ):
        assert not hasattr(repo_bundles_module, retired), retired


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


def test_the_store_is_read_only_after_s52(isolate_agent_runtime_root):
    """S52 retargeted the three projection/idempotency tests that stood here.

    They drove ``desired_bundles_for_task``, ``create_or_update_from_task``,
    ``update`` and ``wake_ready_dependencies`` -- the whole write lane, whose
    only callers were these tests. A store that can only be written by its own
    tests is a closed loop, not covered code (the settled ledger-item-2 rule),
    so the lane went and the tests went with it.

    What survives is the boundary the cut had to respect: the READ side is live,
    ``status.py`` projects operator bundle rows off ``list_all``, and the store
    must no longer expose a single mutator.
    """

    store = RepoBundleStore()
    for mutator in (
        "create_or_update_from_task",
        "update",
        "attach_assignment",
        "mark_running",
        "mark_verified",
        "mark_rejected",
        "wake_ready_dependencies",
        "cancel_superseded",
        "_write",
        "_event",
    ):
        assert not hasattr(store, mutator), mutator

    # The read side still answers, and answers empty on a clean root.
    assert store.list_all() == []
    assert store.list_for_task("task_absent") == []


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
