"""S7-A wire coverage: which coalesced batches ship as op-based patch frames.

Read-model workstream Stage S7-A, wire half. The producer
(:mod:`agent_runtime.state_patches`) LOGS an op-based ``state.patched`` entry
(``upsert`` / ``remove`` / ``refresh``) for every chokepointed keyed-entity
mutation (steer, profile/model, persona-instance close/reap, incident
open/close, task transition). This module decides which coalesced batches the
STREAM promotes to a v2 ``patch`` frame vs. which fall back to a full-core delta
frame — the **honest fallback** the plan mandates for any batch the launcher
cannot fold verbatim.

The S7-A rule (plan §S7-A): **a batch is coverable unless it contains a truly
chokepoint-less event.** Because the producer now projects the changed entity's
WIRE row (op ``upsert``) or a removal (op ``remove``), fold fidelity is trivial
for every covered op — the launcher merges/deletes the keyed row verbatim, no
per-field allowlist, no re-derivation. So coverage is defined at the OP level:

* a ``state.patched`` is *foldable* iff its op is ``upsert`` or ``remove``
  (:data:`state_patches.FOLDABLE_PATCH_OPS`). An op ``refresh`` is NOT foldable —
  it is the accounted "this actor is too big to fold, re-fetch it", so a batch
  carrying one falls back to a full core (which IS that refetch). Task
  transitions ride ``refresh`` (a ~80 KB goal row can't fold), so their batch —
  which also fans out persona-instance removes + assignment closes — is a full
  core that reflects the whole fan-out at once.
* a *covered domain event* (:data:`COVERED_DOMAIN_EVENT_TYPES`) rides in the SAME
  coalesced batch as its ``state.patched`` (same chokepoint, same lock scope) and
  carries no independent fold state — the launcher ignores it and folds the
  paired patch. These are coverable alongside a patch.
* **everything else is uncovered** → the whole batch falls back to a full core:
  run traces, the ``state.reconciled`` watchdog, ``persona_assignment.*`` (the
  ``persona_assignments`` frame section is a ``{active, recent}`` projection, not
  an id-keyed table the launcher can fold a remove into), ``task.*`` domain
  events, board/flow/office writes, and — the known uncovered case the plan names —
  ``planning.py`` chokepoint-less mutations (no ``state.patched`` at all).

Conservative-by-construction: a single uncovered event in a batch demotes the
whole batch, so the launcher never sees a patch frame it cannot fold.
"""

from __future__ import annotations

from typing import Any, Iterable

from .state_patches import FOLDABLE_PATCH_OPS, PATCH_OP_UPSERT, STATE_PATCHED_EVENT_TYPE

#: Domain events that ride alongside their ``state.patched`` in the same
#: coalesced batch (same chokepoint) and carry no fold state of their own — the
#: launcher ignores them and folds the paired op. Each has a paired op:
#: ``.steered`` / ``.profile_updated`` → persona_instance ``upsert``;
#: ``.reaped`` → persona_instance ``remove``; ``incident.closed`` → incident
#: ``remove``. (``incident.opened`` is deliberately NOT covered: an open ships a
#: full row that would be a create-on-absent, so opens ride the full core; only
#: the resolving ``remove`` folds — see the plan's coverage table.)
COVERED_DOMAIN_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "persona_instance.steered",
        "persona_instance.profile_updated",
        # Legacy fold-classifier entries retained for cross-stack fixture
        # compatibility; their domain events are no longer emittable.
        "persona_instance.reaped",
        "incident.closed",
    }
)


def state_patch_op(payload: Any) -> str | None:
    """The op of a ``state.patched`` payload, or None if malformed."""

    if not isinstance(payload, dict):
        return None
    op = payload.get("op")
    return op if isinstance(op, str) and op else None


def state_patch_is_foldable(payload: Any) -> bool:
    """Whether a ``state.patched`` payload is one the launcher folds verbatim.

    Foldable iff its op is ``upsert`` or ``remove`` (see
    :data:`state_patches.FOLDABLE_PATCH_OPS`). An ``upsert`` additionally must
    carry a non-empty ``changed`` (an empty upsert is malformed → not foldable);
    a ``remove`` carries none by contract. ``refresh`` is never foldable."""

    op = state_patch_op(payload)
    if op not in FOLDABLE_PATCH_OPS:
        return False
    if op == PATCH_OP_UPSERT:
        changed = payload.get("changed") if isinstance(payload, dict) else None
        return isinstance(changed, dict) and bool(changed)
    return True


def event_is_patch_coverable(event: Any) -> bool:
    """Whether one drained EventLog entry is safe to ship on the patch lane.

    A ``state.patched`` is coverable iff foldable (upsert/remove, not refresh); a
    covered domain event is coverable because its fold state rides in the paired
    patch. Anything else (task/assignment domain events, run traces,
    ``state.reconciled`` watchdog, board/flow/office writes, planning.py chokepoint-less
    mutations) is uncovered → the whole batch falls back to a full core."""

    event_type = getattr(event, "type", None)
    if event_type == STATE_PATCHED_EVENT_TYPE:
        return state_patch_is_foldable(getattr(event, "payload", None))
    return event_type in COVERED_DOMAIN_EVENT_TYPES


def batch_is_patch_coverable(events: Iterable[Any]) -> bool:
    """Whether an entire coalesced batch ships as a v2 patch frame.

    Coverable unless it contains a truly uncovered event (chokepoint-less
    planning writes, an assignment/task domain event, a ``refresh`` op, …). An
    empty batch is not coverable (nothing to ship)."""

    materialized = list(events)
    if not materialized:
        return False
    return all(event_is_patch_coverable(event) for event in materialized)
