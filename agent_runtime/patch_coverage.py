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
re-hydrate from a fresh checkpoint. So the moment hermes emits a patch for an
entity that table does not hold, every such frame costs a connected launcher a
full re-hydrate: strictly WORSE than the full-core delta it replaced, because
the client now pays the patch AND a fresh core.

The producer cannot know unilaterally which entities are promotable — only the
client knows what its fold table holds. So the CLIENT DECLARES the entity classes
it can fold (``harness stream --fold-entities``, or ``fold_entities`` on the
socket lane's ``{"op":"subscribe","lane":"stream"}``) and the producer promotes
a batch only when every ``state.patched`` in it names a declared entity. A single
undeclared entity demotes the WHOLE batch onto the same honest-fallback full core
an uncovered op already takes — no new lane, no new frame kind, and the demotion
is the outcome the client would have got anyway if it had never folded at all.

The third entity has since arrived (2026-08-14/15)
--------------------------------------------------
The quoted snippet above is still the launcher's GENERIC path, and it is still
two entries — but it is no longer the whole fold. ``office_actor`` is dispatched
ahead of that table into ``_applyOfficeActorPatch``, which folds an actor row
into ``offices[<workspace_id>].actors[]`` (nested, because the office core
section is workspace-keyed and has no top-level actor table for the generic
merge to reach). The launcher declares the wider set on its own subscribe —
``harness stream --fold-entities persona_instance,incident,office_actor`` — so
the negotiation is doing exactly the job it was built for.

What that entity is NOT is a widening of :data:`HISTORICAL_FOLD_ENTITIES`. The
declaration is per-subscriber, and a client that says nothing still gets the
historical two.
"""

from __future__ import annotations

from typing import Any, Iterable

from .state_patches import (
    FOLDABLE_PATCH_OPS,
    OFFICE_ACTOR_ENTITY,
    OFFICE_SURFACE_ENTITY,
    PATCH_OP_REMOVE,
    PATCH_OP_UPSERT,
    PERSONA_INSTANCE_ENTITY,
    STATE_PATCHED_EVENT_TYPE,
)

#: A CAPABILITY token, not an entity — and the declaration channel carries it
#: because that channel was never anything but a set of strings
#: (:func:`normalize_fold_entities` interprets none of them).
#:
#: Why a token was needed at all (office fold-promotion plan §V4, 2026-08-16).
#: The negotiation is per-ENTITY, and the 2026-08-16 change WIDENS THE OPS of an
#: entity that is already declared in the field: ``office_actor`` gained
#: insert-on-absent upserts (``created: true``) and ``remove``. The entity
#: vocabulary cannot express that. A runtime that simply started emitting the
#: widened rows would have them PROMOTED at every fielded launcher — which
#: declares ``office_actor`` today and whose fold answers a create with
#: ``patch_without_target`` and a remove with ``patch_unsupported_op``, each →
#: a full re-hydrate. That is strictly WORSE than the full core it replaced,
#: because the client pays the patch AND the core: the exact "declares what it
#: cannot fold" failure the negotiation exists to prevent, aimed at a client
#: that did nothing wrong.
#:
#: So the widened rows are coverable only for a client that names this token
#: beside its entities. Every mixed pair then degrades to exactly today's wire:
#: an old client never declares it and keeps getting full cores; an old runtime
#: sees it as an unknown string in a frozenset and ignores it.
#:
#: A plain MOVE upsert stays coverable under bare ``office_actor``, unchanged —
#: the token gates the two lifecycle ops, not the entity.
OFFICE_ACTOR_LIFECYCLE_CAPABILITY = "office_actor_lifecycle"

#: The SECOND capability token, and it exists for exactly the reason the first
#: one does — a widened OP on an already-declared entity, which the per-entity
#: vocabulary cannot express (D3, plan §10.3, 2026-08-16).
#:
#: ``persona_instance`` has been declared by every fielded launcher since S7-A,
#: and every ``upsert`` under it has been a SUBSET merge onto a row the client
#: already holds. D3 adds a complete-row ``upsert`` stamped ``created: true`` for
#: a row the client does NOT hold, which a fielded launcher answers with
#: ``patch_without_target`` → a full re-hydrate: the patch AND the core, strictly
#: worse than the full core it replaced. So the create is coverable only for a
#: client that names this token beside its entities.
#:
#: **The one asymmetry with the office token, and it is load-bearing.** For
#: ``office_actor`` the launcher's fold inserts-on-absent UNCONDITIONALLY and the
#: marker only gates coverage: every office upsert already carries the complete
#: row, because the store has no per-field office write. ``persona_instance``
#: upserts are subsets, so an unconditional insert-on-absent would build a roster
#: row out of whichever three fields a steer happened to move. The launcher's
#: generic fold therefore READS ``created`` — insert only when it is stamped,
#: ``patch_without_target`` otherwise — which makes the stamp part of the fold
#: contract here, not merely part of the negotiation.
PERSONA_INSTANCE_CREATE_CAPABILITY = "persona_instance_create"

#: The THIRD capability token, and the first one that gates a DOMAIN EVENT
#: rather than a widened op (WV-H3, office write-verbs plan §2.3, 2026-08-16).
#:
#: ``office_surface`` is a NEW entity name, so the per-entity vocabulary could
#: express the patch row on its own — a client that never declares it is never
#: sent one. What the vocabulary CANNOT express is the other half of this
#: change: ``office.surface.updated`` joins
#: :data:`LIVE_COVERED_DOMAIN_EVENT_TYPES`, and covered domain events are not
#: entity-gated at all (see :func:`event_is_patch_coverable` — they carry no
#: fold state, so the gate that matters is on the paired patch). Adding it
#: un-gated would make every batch carrying a folder write coverable for a
#: client that has never heard of ``office_surface``: it would receive a patch
#: frame whose ONLY row it answers with ``patch_unknown_entity`` → a full
#: re-hydrate, the patch AND the core. Strictly worse than the full core it
#: replaced, aimed at a client that did nothing wrong — the exact failure the
#: other two tokens exist to prevent.
#:
#: So both halves ride this token: the event is coverable only when it is
#: declared, and the patch row is gated on it too. Gating the row is
#: belt-and-braces (a client declaring the entity but not the token would
#: simply demote on the event), and it is kept because the two must move
#: together — a client that folds this row and does not accept the event's
#: coverage has said something incoherent, and one gate saying so is cheaper
#: than two that can disagree.
#:
#: Every mixed pair degrades to exactly today's wire: an old client never
#: declares it and keeps getting full cores; an old runtime sees it as an
#: unknown string in a frozenset and ignores it.
OFFICE_SURFACE_FOLD_CAPABILITY = "office_surface_fold"

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
        # The OPERATOR GESTURE half (office fold-promotion plan O-H3,
        # 2026-08-16). Adding or deleting an agent in the Mission Office cost
        # two full ~822 KB core builds each — one on the launcher's `harness
        # stream` child, one on the serve hub — because each gesture's batch
        # carried an event nobody had a pair for. These three now have one.
        #
        # ``persona_instance.retired``: pairs with the ``persona_instance``
        # ``remove`` emitted two lines later at the same chokepoint. Its fold
        # state IS the row's departure, which the patch carries.
        #
        # ``persona_instance.chat_opened``: pairs with ``open_chat``'s new
        # producer — a diffed ``upsert`` on re-open, an honest ``refresh`` on
        # create (a brand-new roster row cannot be assumed to fit the 4 KB cap;
        # deferred D3). Covering this BEFORE that producer existed would have
        # silently dropped ``mode``/``workspace_id``/``profile_id``/the session
        # trio from every connected client, which is why the two land together.
        #
        # ``office.actor.removed``: pairs with the ``office_actor`` ``remove``
        # ``_archive_actor_locked`` now emits inside the same lock. It was
        # deliberately absent until 2026-08-16 and the comment here said so —
        # an archive rewrites the surface's ``archived_actor_keys`` ledger and
        # ``updated_at``, which an actor-row patch could not express, so
        # covering it would have shipped a patch that folded the actor and
        # silently dropped the surface change beside it. That reasoning was
        # correct FOR THE WIRE AS IT STOOD. What retired it is the derivability
        # audit (§V1), not a change in the honesty rule: the ledger's delta
        # under the two lifecycle ops is exactly determined (archive appends the
        # key, re-add removes it) so the client mirrors it during the fold, the
        # counts are derived at projection and recomputed the same way, and the
        # surface ``updated_at`` has no launcher reader at all. Nothing is
        # dropped because nothing is left that only the surface row could say.
        #
        # Pairs with the ``office_actor`` patch ``OfficeStore.upsert_actor``
        # emits from inside the same ``office_lock`` — same chokepoint, same
        # batch, no fold state of its own (its payload is an item count and a
        # revision the patch's own row already carries).
        #
        # ``office.surface.updated``: pairs with the ``office_surface`` patch
        # ``update_surface`` now emits inside the same lock (WV-H3,
        # 2026-08-16). It is the one entry here that is TOKEN-GATED — see
        # :data:`OFFICE_SURFACE_FOLD_CAPABILITY` and
        # :data:`TOKEN_GATED_DOMAIN_EVENT_TYPES` — because a covered domain
        # event is otherwise not entity-gated at all, and un-gating this one
        # would promote a batch at a client with no ``office_surface`` fold.
        #
        # It was in the "must stay absent" list below until 2026-08-16, on the
        # reasoning that ``folders`` and the surface's own ``revision`` are
        # moved only by ``update_surface`` and a fold could not reproduce them.
        # That reasoning was correct FOR THE WIRE AS IT STOOD — there was no
        # row carrying them. What retires it is a producer, not a change in the
        # honesty rule: the §V1 derivability audit run over this write finds
        # exactly three moved fields and the new patch carries all three
        # verbatim, so nothing is left that only the demoted core could say.
        # What made it worth building is that "rare operator action" was wrong:
        # the write fires on every folder change AND on page open, so one
        # uncoverable event was demoting the whole boot batch.
        #
        # The REMAINING siblings are deliberately absent and must stay absent.
        # ``office.actor.restored`` / ``.conflict_resolved`` and
        # ``office.surface.created`` move surface state a fold genuinely cannot
        # reproduce: a create authors a surface the client has never held (and
        # is emitted by lazy ``ensure_surface`` from inside other writes, so
        # covering it would need a pairing audit of every one of them), a
        # restore un-archives from a copy the client never held, and a conflict
        # resolution adopts a peer's row through a path that bypasses the
        # upsert chokepoint. Leaving them uncovered routes their batches down
        # the full-core lane with no new code and no new failure mode.
        #
        # ``persona_instance.chat_binding_cleared`` is the PERSONA-side member
        # of that must-stay-absent list, and it is the one whose absence reads
        # like an oversight, so it is named here rather than left to inference.
        # It is the mirror of ``chat_opened`` and it looks pairable — the clear
        # moves ``mode`` and the session trio on ``persona_instance_summary``,
        # exactly the fields the bind's patch already carries. What it ALSO
        # moves is the instance's ``persona_chat_history`` row: that projection
        # keys chat rows by ``default_chat_session_id``, so dropping the pointer
        # takes the row out of the section entirely (measured — the section goes
        # from one row to none), and there is no ``persona_chat_history`` patch
        # entity for that departure to ride. Covering the event would promote
        # the batch to a frame whose only row is the persona-instance patch, and
        # the chat row would stay on every connected client for the rest of its
        # session. The demote to a full core is what carries it, which is why
        # ``clear_chat_session_binding`` deliberately emits no ``state.patched``
        # at all — a patch there is unreachable, and the coverage entry that
        # would reach it is unsafe. Both halves are pinned in
        # ``test_persona_assignments.py``.
        #
        # Every entry here is gated by ``test_stream_patch.py``'s both-ways
        # partition test: a live entry must have a registered contract, so none
        # of the four above could be added without a producer behind it.
        "persona_instance.retired",
        "persona_instance.chat_opened",
        "office.actor.removed",
        "office.actor.upserted",
        "office.surface.updated",
    }
)

#: Covered domain events whose coverage additionally requires a CAPABILITY
#: TOKEN in the client's declaration.
#:
#: Empty until 2026-08-16, and the reason it has to exist is structural: a
#: covered domain event is not entity-gated (it carries no fold state, so the
#: gate that matters is on the paired ``state.patched``). That is true for every
#: entry that pairs with a patch on an entity the client ALREADY declares — the
#: patch's own gate covers both. It stops being true for a patch on a NEW
#: entity: the paired event would be coverable at a client that cannot fold the
#: row beside it, and a batch of just the two would ship a frame answered with a
#: re-hydrate.
#:
#: So an event paired with a newly-introduced entity rides that entity's token
#: here. Read this table as "the event is only free-riding once the client has
#: said it can carry the paired row".
TOKEN_GATED_DOMAIN_EVENT_TYPES: dict[str, str] = {
    "office.surface.updated": OFFICE_SURFACE_FOLD_CAPABILITY,
}

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


def state_patch_is_office_lifecycle(payload: Any) -> bool:
    """Whether an ``office_actor`` patch uses one of the two WIDENED ops.

    The two are ``remove`` (the archive) and an ``upsert`` stamped
    ``created: true`` (a first placement or a resurrection re-add — a row the
    client does not hold, so its fold must INSERT rather than merge). Both also
    require the client to maintain the derived container state — the recomputed
    ``actor_count``/``actors_truncated`` and the mirrored ``archived_actor_keys``
    ledger — which is precisely the capability
    :data:`OFFICE_ACTOR_LIFECYCLE_CAPABILITY` names.

    A plain move upsert (no ``created`` key) is NOT lifecycle: it replaces a row
    the client already holds and moves no container state, which is what every
    fielded launcher has folded since 2026-08-14.

    Answered off the PAYLOAD rather than off the entity alone, so a producer that
    stops stamping ``created`` cannot silently re-open the promotion at an
    un-updated client through the un-gated arm — the paired producer test and the
    gate test in ``test_office_state_patches.py`` hold that from both sides.
    """

    if not isinstance(payload, dict):
        return False
    if state_patch_entity(payload) != OFFICE_ACTOR_ENTITY:
        return False
    return payload.get("op") == PATCH_OP_REMOVE or payload.get("created") is True


def state_patch_is_persona_instance_create(payload: Any) -> bool:
    """Whether a ``persona_instance`` patch is the WIDENED create-upsert (D3).

    One shape only: an ``upsert`` stamped ``created: true``, carrying the
    complete row for an instance the client does not hold. A subset upsert (no
    ``created`` key) is NOT a create and stays coverable under bare
    ``persona_instance``, exactly as every fielded launcher has folded it since
    S7-A. A ``remove`` is not gated either — deleting a row a client may not hold
    is idempotent and every fielded fold already treats a missing target as a
    clean no-op.

    Answered off the PAYLOAD rather than the entity, for the same reason its
    office sibling is: a producer that stopped stamping ``created`` would
    otherwise silently re-open promotion at an un-updated client through the
    un-gated arm — and there the launcher would insert nothing and resync, which
    is the regression this gate exists to prevent.
    """

    if not isinstance(payload, dict):
        return False
    if state_patch_entity(payload) != PERSONA_INSTANCE_ENTITY:
        return False
    return payload.get("op") == PATCH_OP_UPSERT and payload.get("created") is True


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

    An ``office_actor`` patch using one of the two WIDENED lifecycle ops
    (``remove``, or an ``upsert`` stamped ``created: true``) additionally
    requires the client to have declared
    :data:`OFFICE_ACTOR_LIFECYCLE_CAPABILITY` — see that constant for why a
    third gate exists at all rather than the entity rule being enough. A
    ``persona_instance`` complete-row create (``upsert`` stamped ``created:
    true``) is gated the same way on
    :data:`PERSONA_INSTANCE_CREATE_CAPABILITY` (D3).

    An ``office_surface`` patch — the folder-taxonomy subset-merge — requires
    :data:`OFFICE_SURFACE_FOLD_CAPABILITY`, whose constant explains why a NEW
    entity needs a token at all when the entity vocabulary could have expressed
    it: the token's real job is the paired DOMAIN EVENT, and the two must move
    together.

    A covered domain event is coverable because its fold state rides in the
    paired patch — it is not entity-gated, because it carries no state to fold
    and the launcher ignores it; the gate that matters is on the paired
    ``state.patched``, which rides the same batch. The exception is
    :data:`TOKEN_GATED_DOMAIN_EVENT_TYPES`, whose entries pair with a patch on
    an entity a fielded client may not fold at all — there the event carries the
    same token as its pair, or an undeclared client would be promoted a batch it
    answers with a re-hydrate. Anything else (task/assignment domain events, run
    traces, ``state.reconciled`` watchdog, board/flow writes, the office
    surface-CREATE/restore/conflict writes, planning.py chokepoint-less
    mutations) is uncovered → the whole batch falls back to a full core."""

    event_type = getattr(event, "type", None)
    if event_type == STATE_PATCHED_EVENT_TYPE:
        payload = getattr(event, "payload", None)
        if not state_patch_is_foldable(payload):
            return False
        declared = normalize_fold_entities(fold_entities)
        if state_patch_entity(payload) not in declared:
            return False
        if state_patch_is_office_lifecycle(payload):
            return OFFICE_ACTOR_LIFECYCLE_CAPABILITY in declared
        if state_patch_is_persona_instance_create(payload):
            return PERSONA_INSTANCE_CREATE_CAPABILITY in declared
        if state_patch_entity(payload) == OFFICE_SURFACE_ENTITY:
            return OFFICE_SURFACE_FOLD_CAPABILITY in declared
        return True
    if event_type in COVERED_DOMAIN_EVENT_TYPES:
        # Most covered events free-ride on the paired patch's gate. The ones in
        # this table cannot, because their pair names an entity a fielded
        # client may not fold — see the constant.
        token = TOKEN_GATED_DOMAIN_EVENT_TYPES.get(event_type)
        return token is None or token in normalize_fold_entities(fold_entities)
    return False


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
