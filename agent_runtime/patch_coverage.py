"""S6 wire coverage: which coalesced batches ship as field-patch frames.

Read-model workstream Stage S6, wire half. The S6 *producer*
(:mod:`agent_runtime.state_patches`) LOGS a ``state.patched`` entry for several
store chokepoints (steer, profile/model, task-transition, incident-close). This
module decides which of those the STREAM promotes to a v2 ``patch`` frame vs.
which fall back to a full-core delta frame — the **honest fallback** the plan
mandates for any batch the launcher cannot fold with projection fidelity.

Coverage is defined at the FIELD level, aligned exactly with the launcher's
foldable-field contract, so a covered batch never forces a launcher resync.
A ``state.patched`` is *foldable* iff it patches an ``{entity, field}`` pair the
launcher re-projects with fidelity. For S6 that is the **STEER case** — a
``persona_instance`` patch whose ``changed`` fields are a subset of
``{steered_by, spawned_by}`` — because the launcher derives its whole steering
topology from ``steered_by`` (2026-07-15 graph decoupling); folding those raw
fields and re-projecting reproduces the full rebuild exactly (proven by the
launcher's parity burn-in comparator).

The other producer patches stay UNCOVERED for S6 and ride the full-core lane,
because a raw field-merge would NOT reproduce the full rebuild:

* task ``state`` → the goal row derives actor/stage labels + blocked flags from
  the task state; a terminal transition also fans out ``persona_instance.closed``
  / ``persona_assignment.closed`` events (themselves uncovered).
* incident ``closed_at`` → incidents ship **open-only** (S2), so a close is a
  row *removal*, not a field write; ``is_open`` is derived.
* persona profile/model fields → ``effective_model`` / ``model_is_override`` /
  ``reasoning_supported`` / ``skills`` are derived, and ``display_name`` has an
  in-row mirror (``agent_profile_display_name``).

Those are the reported coverage gaps for the S7 follow-up (either the producer
emits the derived fields too, or the fold learns the entity-specific semantics).
Until then they are shipped as full cores — correct, just not yet shrunk.
"""

from __future__ import annotations

from typing import Any, Iterable

from .state_patches import STATE_PATCHED_EVENT_TYPE

#: The persona-instance fields a launcher fold reproduces with full projection
#: fidelity (the steer flagship). MUST stay in lockstep with the launcher's
#: ``MissionReadModel`` foldable-field allowlist — the launcher's parity
#: comparator is the cross-repo guard that keeps the two honest.
FOLDABLE_STEER_FIELDS: frozenset[str] = frozenset({"steered_by", "spawned_by"})

#: Domain events that ride in the SAME coalesced batch as their ``state.patched``
#: (same chokepoint, same lock scope) and carry no fold state of their own — the
#: launcher ignores them and folds the paired patch. Coverable alongside a patch.
COVERED_DOMAIN_EVENT_TYPES: frozenset[str] = frozenset({"persona_instance.steered"})


def state_patch_is_foldable(payload: Any) -> bool:
    """Whether a ``state.patched`` payload is one the launcher folds with
    projection fidelity (the S6 steer contract)."""

    if not isinstance(payload, dict):
        return False
    if payload.get("entity") != "persona_instance":
        return False
    changed = payload.get("changed")
    if not isinstance(changed, dict) or not changed:
        return False
    return set(changed.keys()) <= FOLDABLE_STEER_FIELDS


def event_is_patch_coverable(event: Any) -> bool:
    """Whether one drained EventLog entry is safe to ship on the patch lane.

    A ``state.patched`` is coverable iff foldable; a steer domain event is
    coverable because its fold state rides in the paired patch. Anything else
    (task/incident/profile events, run traces, ``state.reconciled`` watchdog,
    persona create/close, board/flow writes, planning.py chokepoint-less
    mutations) is uncovered → the whole batch falls back to a full core."""

    event_type = getattr(event, "type", None)
    if event_type == STATE_PATCHED_EVENT_TYPE:
        return state_patch_is_foldable(getattr(event, "payload", None))
    return event_type in COVERED_DOMAIN_EVENT_TYPES


def batch_is_patch_coverable(events: Iterable[Any]) -> bool:
    """Whether an entire coalesced batch ships as a v2 patch frame.

    Conservative-by-construction: a single uncovered event in the batch demotes
    the whole batch to the full-core lane, so the launcher never sees a patch it
    cannot fold. An empty batch is not coverable (nothing to ship)."""

    materialized = list(events)
    if not materialized:
        return False
    return all(event_is_patch_coverable(event) for event in materialized)
