"""The class→instance re-key migration's durability fence.

``scripts/office_actor_rekey_to_instance.py`` moves every live placement from
its persona CLASS key (``backend_dev``) to its canonical persona-INSTANCE key
(``personainst_backend_dev_agent_29fdd71a``), archiving the old key and
appending it to the surface's ``archived_actor_keys`` resurrection guard.

That migration is **not durable on its own**. ``OfficeStore.upsert_actor``
treats an explicit upsert of an archived key as operator intent to re-add and
CLEARS the ledger entry (``office_store.py:344-351``). So one surviving
class-keyed write re-creates the old actor as ACTIVE beside the instance-keyed
one — the same agent placed twice, duplicate ``item_id`` values on one canvas,
and not a single conflict warning, because the two actor keys are different
strings and every guard in the store keys on the actor key.

This module is the missing predicate. It is a PREDICATE and nothing else: the
fence that spends it lives at the store's write chokepoint —
``OfficeStore._guard_class_keyed_write`` (for ``upsert_actor``) and
``OfficeStore._guard_class_keyed_adoption`` (for ``resolve_conflict``) — and
every caller keeps only a transport-shaped translation of the store's typed
refusal.

That is a correction, not the original shape (Plan EG-6.6). The guard used to be
CALLED at four writers around one store, on the reasoning that the store's
contract ("an explicit upsert of an archived key is intent to re-add") is
correct for a caller that actually holds operator intent while the template copy
and the CLI verb do not. The reasoning was sound and the shape was still wrong:
a fence at N callers is N fences, the rekey script's own docstring had to WARN
that any new writer reaching ``_write_actor`` was unfenced by default, and the
fifth writer would have shipped unfenced with every reply-shape test green. So
the predicate stayed here and the decision moved into the store, where a new
caller inherits it instead of having to remember it.

The sanctioned overrides survive as EXPLICIT STORE PARAMETERS
(``upsert_actor(allow_class_key=...)``, ``resolve_conflict(allow_class_key=...)``)
rather than as a caller-side fence omission — the difference being that an
override is now a value someone passed on the record, never a guard someone
forgot to call. ``restore_actor`` (the sanctioned un-archive verb) keeps working
exactly as before: it IS the operator's deliberate resurrection, so it is the
one actor writer whose whole purpose is the thing the fence refuses.

Only CLASS-KEYED writes are guarded — a payload with no ``persona_instance_id``.
An instance-keyed write cannot undo the migration; it IS the migration's shape.

The two collision reasons, both narrow on purpose:

``resurrects_archived_class_key``
    The class key sits in ``archived_actor_keys``. Writing it clears the
    resurrection guard — the exact mechanism above. ``harness office
    actor-restore`` is the verb for deliberately bringing that placement back.

``duplicate_item_placement``
    An ACTIVE actor with the same ``persona_id`` under a DIFFERENT key already
    holds one of the incoming ``item_id`` values. This is the harm named in the
    migration docstring — the same canvas item owned by two actor files.

    Deliberately narrowed to item-id overlap rather than "same persona, two
    keys". Class-keyed placements are a supported shape, not a defect
    (``OfficeStore.archive_actors_for_instance``: "Persona-id-keyed placements
    survive instance churn by design"), and the launcher legitimately emits one
    payload per (persona, binding) group. Refusing every unbound placement that
    shares a persona with a bound one would outlaw a legal canvas; refusing a
    DUPLICATED ITEM refuses only the corruption.
"""

from __future__ import annotations

from typing import Any

from .errors import ActorsUnreadable, AgentRuntimeError

#: Stage-42 error code for the refusal. ``duplicate_conflict`` (exit 4) rather
#: than ``sync_conflict``: nothing is under realm-sync conflict, the write would
#: simply place one agent twice.
CLASS_KEY_REFUSAL_CODE = "duplicate_conflict"


class ClassKeyedPlacementRefused(AgentRuntimeError):
    """Raised when a class-keyed actor write would undo the re-key migration.

    ``safe_details`` carries keys and reasons only — never item positions,
    display names, or any other placement content.
    """

    code = CLASS_KEY_REFUSAL_CODE

    def __init__(self, message: str, *, safe_details: dict | None = None):
        super().__init__(message)
        self.safe_details = dict(safe_details or {})


def is_class_keyed_payload(payload: Any) -> bool:
    """True when the payload carries no instance binding, so the store would
    fall through to the bare persona id (``_canonical_actor_key``)."""

    if not isinstance(payload, dict):
        return False
    return not str(payload.get("persona_instance_id") or "").strip()


def class_key_collision(store: Any, workspace_id: str, payload: dict) -> dict | None:
    """Would this class-keyed payload undo the re-key migration? ``None`` if not.

    Read-only against the store, but NOT total: it raises
    :class:`~.errors.ActorsUnreadable` when the answer is unknowable rather than
    answering "no" from partial knowledge (see below). Callers are the store's
    own two fences; nothing outside the store spends this predicate.

    A workspace with no office surface — the create-from-template destination,
    the overwhelmingly common legitimate case — can never collide and
    short-circuits before any directory scan.
    """

    # The store owns id normalization; re-deriving it here is exactly the drift
    # the office plan's "one derivation authority" rule exists to prevent.
    from .office_store import _normalize_persona_id, _safe_id

    if not is_class_keyed_payload(payload):
        return None
    persona_id = _normalize_persona_id(payload.get("persona_id"))
    if not persona_id:
        # An unusable persona id is the store's ValueError to raise, not ours.
        return None
    if not store.surface_exists(workspace_id):
        return None

    reasons: list[str] = []
    conflicting: set[str] = set()

    surface = store.get_surface(workspace_id)
    if persona_id in (surface.archived_actor_keys or []):
        reasons.append("resurrects_archived_class_key")

    incoming_items = {
        item_id
        for item_id in (
            _safe_id(raw.get("item_id")) if isinstance(raw, dict) else None
            for raw in (payload.get("items") or [])
        )
        if item_id
    }
    if incoming_items:
        # ``scan_actors``, never ``list_actors`` (EG-1.5's chokepoint, EG-6.6's
        # close). The list view drops files that will not decode, so a single
        # unreadable instance-keyed sibling turned "I cannot tell" into "no
        # conflict" — for EVERY writer through this one predicate — and the
        # duplicate placement landed under the actor nobody could read. There is
        # no honest partial answer here: an item id can only be proven ABSENT by
        # reading every actor that might hold it.
        scan = store.scan_actors(workspace_id)
        if scan.unreadable:
            raise ActorsUnreadable(
                f"actors_unreadable:{workspace_id} ({scan.unreadable} of "
                f"{len(scan.actors) + scan.unreadable} actor files) — the class-key "
                "fence cannot prove this placement is not already held by another "
                "actor. Repair or remove the unreadable actor file, or supply "
                "persona_instance_id to place the instance."
            )
        for actor in scan.actors:
            if actor.actor_key == persona_id:
                continue  # the class-keyed actor's own idempotent re-save
            if _normalize_persona_id(actor.persona_id) != persona_id:
                continue
            if incoming_items.isdisjoint(item.item_id for item in actor.items):
                continue
            conflicting.add(actor.actor_key)
    if conflicting:
        reasons.append("duplicate_item_placement")

    if not reasons:
        return None
    return {
        "workspace_id": workspace_id,
        "persona_id": persona_id,
        "class_actor_key": persona_id,
        "reasons": sorted(reasons),
        "conflicting_actor_keys": sorted(conflicting),
    }


def refusal_message(collision: dict) -> str:
    """One operator-readable line naming the conflict and both ways out.

    The message carries the conflicting keys because ``emit_harness_error``
    only merges ``safe_details`` for three exception types it names explicitly;
    a refusal that does not name the other actor is a refusal the operator
    cannot act on.
    """

    reasons = ", ".join(collision.get("reasons") or []) or "class_keyed_write"
    others = collision.get("conflicting_actor_keys") or []
    against = f" (conflicts with {', '.join(others)})" if others else ""
    return (
        f"class-keyed office write for persona {collision.get('persona_id')!r} into "
        f"{collision.get('workspace_id')!r} refused: {reasons}{against}. "
        "The class→instance re-key migration archived this key; writing it back "
        "clears the resurrection guard and places the agent twice. Supply the "
        "persona_instance_id to place the instance, use `harness office "
        "actor-restore` to un-archive the class-keyed placement on purpose, or "
        "pass --allow-class-key to force this write."
    )
