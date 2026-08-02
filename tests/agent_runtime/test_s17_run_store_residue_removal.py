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

Round 4 exercised the deferred operator ruling and removed the caller-less
``RunStore.cancel`` / ``close_run`` writer with the ``run.closed`` write
contract. Historical rows still render through ``events.operator_event_summary``.
"""

from __future__ import annotations

import inspect


import agent_runtime.store as store_module
from agent_runtime.events import EventLog
from agent_runtime.models import Incident
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

LIVE_RUN_STORE_METHODS = ("get", "list_all")

DE_REGISTERED_RUN_EVENT_TYPES = frozenset({"run.heartbeat", "run.approved"})

# The ABSOLUTE registered-contract count has a single owner:
# tests/agent_runtime/test_s15_event_contract_pruning.SURVIVING_EVENT_COUNT.
# This file asserts only its own delta — two duplicated totals would just mean
# two places to edit on every registry change, and one of them going stale.




def test_the_names_other_modules_import_from_store_survive():
    """Negative gate: the cut trimmed store.py's imports, and six modules read
    ``ACTIVE_RUN_STATES`` / ``_safe_session_id`` straight off this module."""

    for name in ("ACTIVE_RUN_STATES", "TaskStore"):
        assert hasattr(store_module, name), name


def test_the_surviving_run_store_surface_is_exactly_the_historical_read_path():
    public = {
        name
        for name, _ in inspect.getmembers(RunStore, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert public == set(LIVE_RUN_STORE_METHODS)
