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

This module is the missing predicate, and it lives OUTSIDE the store on
purpose: the store's contract ("an explicit upsert of an archived key is intent
to re-add") is correct for a caller that actually holds operator intent. What
was missing is that two callers do NOT hold it — a template copy and a CLI verb
fed a payload — and so must ask before writing. Guarding at the callers keeps
the store's chokepoint semantics intact and keeps ``restore_actor`` (the
sanctioned un-archive verb) working exactly as before.

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

from .errors import AgentRuntimeError

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

    Pure read. A workspace with no office surface — the create-from-template
    destination, the overwhelmingly common legitimate case — can never collide
    and short-circuits before any directory scan.
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
        for actor in store.list_actors(workspace_id):
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
