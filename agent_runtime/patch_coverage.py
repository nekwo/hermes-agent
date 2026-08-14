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
  events, board/flow writes, every office write EXCEPT the actor-only upsert
  (see ``LIVE_COVERED_DOMAIN_EVENT_TYPES``), and — the known uncovered case the
  plan names — ``planning.py`` chokepoint-less mutations (no ``state.patched``
  at all).

Conservative-by-construction: a single uncovered event in a batch demotes the
whole batch, so the launcher never sees a patch frame it cannot fold.

Entity coverage is NEGOTIATED (2026-08-13)
------------------------------------------

Every rule above is about the OP. None of them is about the ENTITY — and the
consumer half has always been the narrower of the two. The launcher's fold maps
an entity class to a keyed core section through a hardcoded table of exactly two
entries (``mission_read_model.dart``)::

    static const Map<String, String> _entitySection = <String, String>{
      'persona_instance': 'persona_instances',
      'incident': 'incidents',
    };

    final section = _entitySection[entity];
    if (section == null) return 'patch_unknown_entity:$entity';

and a ``patch_unknown_entity:`` fold outcome is ``needsResync`` — the caller MUST
re-hydrate from a fresh checkpoint. So the moment hermes emits a patch for a
THIRD entity (``office_actor``, ``running_work``, …), every such frame costs a
connected launcher a full re-hydrate: strictly WORSE than the full-core delta it
replaced, because the client now pays the patch AND a fresh core.

The producer cannot know unilaterally which entities are promotable — only the
client knows what its fold table holds. So the CLIENT DECLARES the entity classes
it can fold (``harness stream --fold-entities``, or ``fold_entities`` on the
socket lane's ``{"op":"subscribe","lane":"stream"}``) and the producer promotes
a batch only when every ``state.patched`` in it names a declared entity. A single
undeclared entity demotes the WHOLE batch onto the same honest-fallback full core
an uncovered op already takes — no new lane, no new frame kind, and the demotion
is the outcome the client would have got anyway if it had never folded at all.

This makes a third entity POSSIBLE. It does not enable one: nothing in this
change widens what the producer emits.
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
#: The HISTORICAL half of the set below: fold-classifier entries whose domain
#: event has been DE-REGISTERED and can no longer be emitted, kept deliberately
#: so a cross-stack fixture replaying an old batch still classifies the way the
#: launcher folded it when the event was live.
#:
#: This split exists because the flat set had become dishonest. S65 retired
#: ``persona_instance.reaped`` and ``incident.closed`` from the event catalog
#: with their last writers, and the surviving vocabulary was indistinguishable
#: from the live entries — a classifier naming a producer nobody has. Naming
#: the two halves separately makes the outliving VISIBLE, and
#: ``test_patch_coverage`` gates the partition: every LIVE entry must be in
#: ``event_catalog()``, every HISTORICAL entry must be OUT of it. A new entry
#: therefore cannot be added to the live half without a registered contract
#: behind it, and a contract cannot be de-registered out from under a live
#: entry without this going red.
HISTORICAL_COVERED_DOMAIN_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "persona_instance.reaped",
        "incident.closed",
    }
)

#: Domain events with a LIVE producer that ride alongside their ``state.patched``
#: in the same coalesced batch.
LIVE_COVERED_DOMAIN_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "persona_instance.steered",
        "persona_instance.profile_updated",
        # Pairs with the ``office_actor`` patch ``OfficeStore.upsert_actor``
        # emits from inside the same ``office_lock`` — same chokepoint, same
        # batch, no fold state of its own (its payload is an item count and a
        # revision the patch's own row already carries).
        #
        # Its SIBLINGS are deliberately absent and must stay absent.
        # ``office.actor.removed`` / ``.restored`` / ``.conflict_resolved`` and
        # ``office.surface.*`` each rewrite the SURFACE row —
        # ``archived_actor_keys``, ``folders``, the surface's own ``revision``
        # and ``updated_at`` — which an actor-row patch cannot express. Leaving
        # them uncovered is what routes their batches down the full-core lane
        # with no new code and no new failure mode; covering one would ship a
        # patch frame that folded an actor and silently dropped the surface
        # change riding beside it.
        "office.actor.upserted",
    }
)

COVERED_DOMAIN_EVENT_TYPES: frozenset[str] = (
    LIVE_COVERED_DOMAIN_EVENT_TYPES | HISTORICAL_COVERED_DOMAIN_EVENT_TYPES
)


#: What a client that declares NOTHING is taken to fold.
#:
#: **Not the empty set, and that is load-bearing.** Absence is what every client
#: alive today sends — no launcher build in the field knows this negotiation
#: exists — and these two entities are EXACTLY the launcher's ``_entitySection``
#: table (quoted in the module docstring), i.e. exactly today's wire. Defaulting
#: to the empty set would be the safe-looking choice that silently demoted every
#: currently-connected client to full cores forever: the S7-A patch lane, whose
#: whole measured value is 486 bytes where the full core is 822,671, would go
#: dark again for everyone the day this landed — the same "dark for its whole
#: life" failure the root-only-config incident already cost this lane once
#: (see ``config.ROOT_ONLY_CONFIG_KEYS``).
#:
#: So absence resolves HERE, and an un-updated client keeps its current
#: behaviour byte-for-byte. Widening this set is a CROSS-STACK change: it may
#: only name entities every fielded client already folds.
HISTORICAL_FOLD_ENTITIES: frozenset[str] = frozenset(
    {
        "persona_instance",
        "incident",
    }
)


def normalize_fold_entities(declared: Iterable[str] | None) -> frozenset[str]:
    """The entity set to promote against, from a client's declaration.

    ``None`` — the client said nothing — resolves to
    :data:`HISTORICAL_FOLD_ENTITIES`, never to the empty set (see the constant).
    An EXPLICIT empty declaration is honoured as empty: "I fold nothing, send me
    full cores" is a thing a client is allowed to say, and it must be
    distinguishable from saying nothing at all.
    """

    if declared is None:
        return HISTORICAL_FOLD_ENTITIES
    if isinstance(declared, frozenset):
        return declared
    return frozenset(str(name).strip() for name in declared if str(name).strip())


def accepted_fold_entities(declarations: Iterable[Iterable[str] | None]) -> frozenset[str]:
    """The set a SHARED producer may promote for a room of clients: the
    INTERSECTION of every attached client's declaration.

    The socket lane runs ONE producer for N subscribers (see
    :mod:`agent_runtime.serve_stream_hub` — a per-client generator would
    multiply a full snapshot rebuild by the subscriber count, which is the
    number a durable multi-client service exists to make large). Every frame is
    fanned out to everyone, so a batch may only be promoted when EVERY attached
    subscriber can fold it: one client that cannot would take a re-hydrate on a
    frame produced for somebody else. Intersection, not union — the union is the
    exact bug this negotiation exists to prevent, aimed at the wrong client.

    An empty room yields :data:`HISTORICAL_FOLD_ENTITIES` (nobody has narrowed
    anything), so a producer built before its first subscriber behaves as today.
    """

    accepted: frozenset[str] | None = None
    for declared in declarations:
        normalized = normalize_fold_entities(declared)
        accepted = normalized if accepted is None else (accepted & normalized)
    return HISTORICAL_FOLD_ENTITIES if accepted is None else accepted


def parse_fold_entities_option(raw: str | None) -> frozenset[str] | None:
    """Parse a comma-separated ``--fold-entities`` value.

    ``None`` (flag absent) stays ``None`` — the caller must keep "said nothing"
    distinguishable from "said empty" all the way down to
    :func:`normalize_fold_entities`, or the historical default is lost. An empty
    or whitespace-only STRING is an explicit empty declaration.
    """

    if raw is None:
        return None
    return frozenset(part.strip() for part in str(raw).split(",") if part.strip())


def state_patch_op(payload: Any) -> str | None:
    """The op of a ``state.patched`` payload, or None if malformed."""

    if not isinstance(payload, dict):
        return None
    op = payload.get("op")
    return op if isinstance(op, str) and op else None


def state_patch_entity(payload: Any) -> str | None:
    """The entity class of a ``state.patched`` payload, or None if malformed.

    None is never a member of a declared set, so a malformed payload demotes its
    batch — the same direction every other ambiguity in this module takes.
    """

    if not isinstance(payload, dict):
        return None
    entity = payload.get("entity")
    return entity if isinstance(entity, str) and entity else None


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


def event_is_patch_coverable(
    event: Any, *, fold_entities: Iterable[str] | None = None
) -> bool:
    """Whether one drained EventLog entry is safe to ship on the patch lane.

    A ``state.patched`` is coverable iff foldable (upsert/remove, not refresh)
    AND its entity is one the client declared it can fold — the op rule runs
    FIRST and is unchanged; the entity rule is an additional requirement layered
    on top, never a replacement. ``fold_entities=None`` means the client
    declared nothing → :data:`HISTORICAL_FOLD_ENTITIES` (see the constant: NOT
    the empty set).

    A covered domain event is coverable because its fold state rides in the
    paired patch — it is not entity-gated, because it carries no state to fold
    and the launcher ignores it; the gate that matters is on the paired
    ``state.patched``, which rides the same batch. Anything else (task/assignment
    domain events, run traces, ``state.reconciled`` watchdog, board/flow writes,
    the office SURFACE/remove/restore writes, planning.py chokepoint-less
    mutations) is uncovered → the whole batch falls back to a full core."""

    event_type = getattr(event, "type", None)
    if event_type == STATE_PATCHED_EVENT_TYPE:
        payload = getattr(event, "payload", None)
        if not state_patch_is_foldable(payload):
            return False
        return state_patch_entity(payload) in normalize_fold_entities(fold_entities)
    return event_type in COVERED_DOMAIN_EVENT_TYPES


def batch_is_patch_coverable(
    events: Iterable[Any], *, fold_entities: Iterable[str] | None = None
) -> bool:
    """Whether an entire coalesced batch ships as a v2 patch frame.

    Coverable unless it contains a truly uncovered event (chokepoint-less
    planning writes, an assignment/task domain event, a ``refresh`` op, …) or one
    naming an entity the client did not declare it can fold. An empty batch is
    not coverable (nothing to ship)."""

    materialized = list(events)
    if not materialized:
        return False
    # Normalized ONCE for the batch: the per-event call would otherwise re-derive
    # the same frozenset for every entry of a 256-event batch.
    declared = normalize_fold_entities(fold_entities)
    return all(
        event_is_patch_coverable(event, fold_entities=declared) for event in materialized
    )
