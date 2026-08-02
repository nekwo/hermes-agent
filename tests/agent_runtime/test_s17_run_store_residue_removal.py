"""S17 cuts the store.py Tier-2 residue the mission-lane removal deferred.

Two clusters of module-level helpers in ``store.py`` outlived their callers when
S6/S8/S9 took the proof store, the ``Task`` record, and the archive verb:

* the proof-attribution/redaction helpers (``_enrich_proof_lane_attribution``,
  ``_safe_proof_actor``, ``_safe_run_id``, ``_safe_proof_status``) — the last of
  ``ProofStore``'s private surface, and
* the whole task-archive evidence-mover family (``_archive_*``,
  ``_move_if_exists``, ``_safe_archive_*``, ``_active_worker_sessions_for_task``).

Every one referenced only the others; nothing outside the two blocks called in.

``RunStore`` is the same story on the write side. afd6c0a83 de-registered 67
unemittable event contracts but deliberately held ``run.opened`` /
``run.heartbeat`` / ``run.approved`` / ``run.closed`` because their writer still
existed and de-registering first would have turned dormant methods into
``ValueError`` crashes. With the mission lane gone, six ``RunStore`` methods have
zero production callers (tests were their only remaining users), so the writer
goes first and the contract second — the order that commit asked for.

THE TRAP THIS TEST EXISTS TO HOLD: ``_safe_event_token`` sits physically BETWEEN
the two dead blocks and is LIVE — ``IncidentStore.open`` runs every metadata
value through it before copying it onto the ``incident.opened`` payload. A
line-range cut that swallows it silently removes the redaction gate on a live
event. It is pinned below by behavior, not by ``hasattr``.

ROUND 2 RE-DERIVATION: ``RunStore.cancel`` / ``close_run`` and the
``run.closed`` writer now have zero production callers after the obsolete
worker-chat replacement lane was removed. They remain compatibility-held in
this pass because removing an event contract is an operator contract decision,
outside a dead-code campaign's authority. The behavior pin below records that
held contract; it is no longer evidence of production reachability.
"""

from __future__ import annotations

import inspect


import agent_runtime.store as store_module
from agent_runtime.events import ALLOWED_EVENT_TYPES, EventLog
from agent_runtime.models import AgentRun, Incident
from agent_runtime.states import RunState
from agent_runtime.store import IncidentStore, RunStore
from hermes_time import now


DEAD_STORE_HELPERS = (
    # ProofStore's private surface, orphaned when S6 took the proof store.
    "_enrich_proof_lane_attribution",
    "_safe_proof_actor",
    "_safe_run_id",
    "_safe_proof_status",
    # The task-archive evidence movers, orphaned when S8 took the archive verb.
    "_safe_archive_actor",
    "_safe_archive_persona",
    "_safe_archive_reason",
    "_archive_result",
    "_archive_batch_name",
    "_move_if_exists",
    "_active_worker_sessions_for_task",
    "_archive_worker_evidence",
    "_archive_persona_assignment_evidence",
    "_archive_persona_instance_evidence",
    "_archive_repo_bundle_evidence",
    "_archive_runtime_instance_evidence",
    "_archive_packet_artifacts",
    "_archive_role_envelope_evidence",
    "_archive_self_test_evidence",
    # Session-reuse predicates read only by RunStore.latest_session_id.
    "_latest_run_is_invalid",
    "_run_session_is_reusable",
    "_run_has_approved_continuation",
    # The stage sentinel read only by latest_session_id / find_active.
    "_ANY_STAGE",
    # Task-state frozensets with zero readers repo-wide since the Task record
    # went at S8 — store.py was their only home and nothing imported them.
    "TERMINAL_TASK_STATES",
    "RELEASE_ASSIGNMENT_TASK_STATES",
    "ARCHIVABLE_TASK_STATES",
    # Imports the two dead blocks were the last users of. They were reachable as
    # ``store_module.<name>`` re-exports, so leaving them is not free: it keeps a
    # removed subsystem importable from a surviving module.
    "timedelta",
    "TaskState",
    "Proof",
    "GoalRuntimeInstance",
    "PersonaAssignmentStore",
    "PersonaInstanceStore",
    "archive_task_events",
    "archive_lock",
    "task_lock",
    "emit_task_refresh",
)

WRITE_DEAD_RUN_STORE_METHODS = (
    "open_run",
    "heartbeat",
    "approve_continuation",
    "latest_session_id",
    "find_stale",
    "find_active",
)

LIVE_RUN_STORE_METHODS = ("get", "update", "close_run", "cancel", "list_for_task", "list_all")

DE_REGISTERED_RUN_EVENT_TYPES = frozenset({"run.heartbeat", "run.approved"})

# The ABSOLUTE registered-contract count has a single owner:
# tests/agent_runtime/test_s15_event_contract_pruning.SURVIVING_EVENT_COUNT.
# This file asserts only its own delta — two duplicated totals would just mean
# two places to edit on every registry change, and one of them going stale.


def _seed_run(*, run_id: str = "run_s17", persona_id: str = "dev", task_id: str = "task_s17") -> AgentRun:
    """Create a run row without the removed writer.

    ``RunStore.update`` is the surviving write path: it tolerates a missing
    previous row (``NotFound`` -> ``previous is None``) and persists.
    """

    ts = now()
    run = AgentRun(
        id=run_id,
        persona_id=persona_id,
        task_id=task_id,
        stage_id=None,
        state=RunState.RUNNING,
        started_at=ts,
        last_heartbeat_at=ts,
    )
    assert RunStore().update(run) is True
    return run




def test_the_names_other_modules_import_from_store_survive():
    """Negative gate: the cut trimmed store.py's imports, and six modules read
    ``ACTIVE_RUN_STATES`` / ``_safe_session_id`` straight off this module."""

    for name in ("ACTIVE_RUN_STATES", "TERMINAL_RUN_STATES", "_safe_session_id", "TaskStore"):
        assert hasattr(store_module, name), name


def test_safe_event_token_survived_the_cut_and_still_gates_incident_payloads():
    """The live helper wedged between the two dead blocks.

    ``IncidentStore.open`` copies metadata onto the ``incident.opened`` payload
    only when ``_safe_event_token`` accepts the value. Pinned by behavior so a
    future cut cannot delete the gate and keep the test green.
    """

    assert callable(getattr(store_module, "_safe_event_token", None))

    IncidentStore().open(
        Incident(
            id="inc_s17",
            task_id="task_s17",
            run_id=None,
            kind="tool_failure",
            summary="s17",
            detail_path="incidents/inc_s17.txt",
            opened_at=now(),
            metadata={
                "stage_id": "stage_1",
                # Rejected: spaces and a path separator are not token-safe.
                "lane_id": "C:/Users/example/secret lane",
            },
        )
    )

    event = EventLog().tail(1)[0]
    assert event.type == "incident.opened"
    assert event.payload["stage_id"] == "stage_1"
    assert "lane_id" not in event.payload




def test_the_surviving_run_store_surface_is_exactly_the_read_and_close_path():
    public = {
        name
        for name, _ in inspect.getmembers(RunStore, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert public == set(LIVE_RUN_STORE_METHODS)




def test_run_closed_compatibility_hold_still_emits_registered_event():
    """Contract hold: removal needs a separate operator event ruling."""

    assert "run.closed" in ALLOWED_EVENT_TYPES

    run = _seed_run()
    cancelled = RunStore().cancel(run.id, reason="operator stopped runaway smoke")

    assert cancelled.state == RunState.CANCELLED
    event = EventLog().tail(1)[0]
    assert event.type == "run.closed"
    assert event.payload["state"] == "cancelled"


def test_the_surviving_read_path_still_answers():
    run = _seed_run()
    store = RunStore()

    assert store.get(run.id).id == run.id
    assert [item.id for item in store.list_for_task("task_s17")] == [run.id]
    assert [item.id for item in store.list_all()] == [run.id]
