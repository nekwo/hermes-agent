"""OfficeStore — the single write chokepoint for the Mission Office domain.

Mirrors ``BoardStore`` 1:1 (the plan's lean directive —
``docs/mission_control/OFFICE_LAYOUT_REALM_SYNC_PLAN_2026-07-17.md``, launcher
repo): file-per-actor JSON under the runtime root, the shared store-lock
discipline, atomic writes, and a typed ``EventLog`` event on EVERY mutation
(standing store rule — an event-less write is invisible to the watermark-gated
snapshot/serve pipeline).

Hard invariants this store upholds:

- **Actor keys are canonicalized at THIS boundary** (plan §4.3):
  ``canonical_persona_instance_id`` for instance-bound actors, else the
  normalized persona id. The launcher sends the identity triple and never
  computes a sync filename; ``office_models.actor_file_token`` is the only
  filename derivation.
- **Archive-never-delete.** Removed actors move to ``archive/`` and their keys
  are recorded in the surface's ``archived_actor_keys`` resurrection-guard
  ledger.
- **Display names are validated at WRITE time** against the realm-sync
  secret-assignment scanner, so one member's name can never hard-fail another
  member's realm publish (plan §4.2). The publish-time scan stays as
  defense-in-depth.
- **Upserts are naturally idempotent** (keyed by ``actor_key``; identical
  content re-writes converge on the same file), so no idempotency ledger is
  needed — the board's ledger exists because ``add_card`` mints a new id per
  call; office writes don't.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

from hermes_time import now
from utils import atomic_json_write

from . import office_models, paths
from .errors import (
    ActorArchived,
    ActorsUnreadable,
    AgentRuntimeError,
    AlreadyExists,
    ArchiveUnreadable,
    NotFound,
    StaleRevision,
    SyncConflict,
    WorkspaceUnresolved,
)
from .events import EventLog
from .locks import office_lock
from .models import Event, OfficeActor, OfficeItem, OfficeSurface
# Eager, not lazy: the rule now lives in stdlib-only ``agent_runtime.redaction``,
# so importing it costs nothing. The old lazy ``from .realm_sync import …`` was
# dodging ``realm_sync``'s weight — the tell that ``realm_sync`` was the wrong
# home for a constant four other modules needed.
from .redaction import SECRET_ASSIGNMENT_RE
from .serde import from_jsonable, safe_id, to_jsonable

ARCHIVED_LEDGER_CAP = 5000
MAX_ITEMS_PER_ACTOR = 32
MAX_FOLDERS = 64


def merge_archived_ledgers(peer_keys, local_keys) -> list[str]:
    """Union two ``archived_actor_keys`` ledgers — peer order first, local tail.

    The resurrection guard is a LEDGER, and a ledger that one side can overwrite
    guards nothing: :meth:`OfficeStore.adopt_remote_surface` wrote the peer's
    list verbatim, so a pull from a member that had never heard of a key this
    install archived silently erased that key's tombstone. The archived FILE
    survives, which is why the fence's OR-semantics papered the hole over; the
    ledger half — the half ``classify_three_way_pull(..., locally_archived=)``
    reads — did not.

    Two properties, both load-bearing, neither an accident of the expression:

    * **the peer's list leads, in the peer's order.** When the local ledger is a
      subset of the peer's (the converged case, and the common one) the result is
      the peer's list byte-for-byte, so ``office_content_hash`` still matches the
      remote and a pull that changed nothing stays a no-op. Local-first ordering
      would re-hash every converged surface and hand the next pull a permanent
      "unpublished" local edit over identical content.
    * **local-only keys land at the TAIL**, which is the end the
      ``[-ARCHIVED_LEDGER_CAP:]`` truncation keeps. Under cap pressure the keys
      that survive are the ones THIS install archived — the ones whose
      resurrection this store is the only witness to.

    Deduplicated on first occurrence: ``_archive_actor_locked`` cannot produce a
    repeat locally, so a duplicate can only have arrived from a peer, and
    carrying it forward would spend cap budget on a key already guarded.
    """

    merged: list[str] = []
    seen: set[str] = set()
    for key in (*(peer_keys or ()), *(local_keys or ())):
        text = str(key)
        if text in seen:
            continue
        seen.add(text)
        merged.append(text)
    return merged[-ARCHIVED_LEDGER_CAP:]

#: Stage-42 error code for the desk fence, and the wire's ``data.reason``.
#: One spelling per lane rather than two vocabularies for one refusal, and the
#: same word the launcher's render-time detector already prints
#: (``MissionOfficeRenderResolver._scanDeskInvariants``) — the fence and the
#: detector name the same fault, so an operator who has seen one recognizes the
#: other. Exit family 4 beside ``duplicate_conflict``: same operator move
#: (something is already placed; move or remove it), different WHICH.
DUPLICATE_DESK_REFUSAL_CODE = "duplicate_desk"


class DuplicateDeskRefused(AgentRuntimeError):
    """Raised when an actor write would give one persona a SECOND live desk.

    The one-desk-per-persona rule was a LAUNCHER rule only (plan
    ``agent-placement-verb`` F9): ``MissionOfficeLayout.hasAuthoredDeskForPersona``
    guards the authoring gesture and ``MissionOfficeRenderResolver`` counts desk
    render nodes afterwards, so the client refuses what the client authors and
    reports what it finds. Neither is a fence: the 2026-08-24 incident authored
    a second ``qa`` desk through ``harness office actor-upsert``, a door no
    launcher predicate can stand in front of, and the store took the write.

    So the rule moves to the write chokepoint as defence in depth (D6). The
    launcher guard stays — refusing at the gesture is a better experience than
    refusing at the ack — and the render warning stays, because it is the only
    thing that can see data that PREDATES this fence (a realm pull, per D6, is
    deliberately outside it: ``office_sync.apply_office_pull`` writes files
    directly and a pulled duplicate is a conflict-lane fact, not a local write).

    ``safe_details`` carries keys and ids only — never positions, display names
    or any other placement content.
    """

    code = DUPLICATE_DESK_REFUSAL_CODE

    def __init__(self, message: str, *, safe_details: dict | None = None):
        super().__init__(message)
        self.safe_details = dict(safe_details or {})


class ActorScan(NamedTuple):
    """What an actor-directory scan FOUND, beside what it could not read.

    The second field is the whole point. ``_read_actor_dir`` has always skipped
    a file it could not decode and returned the rest, so every reader downstream
    received a SHORTER list that described itself as complete — and the office
    projection then computed ``actors_truncated`` from the already-shortened
    list, arriving at 0. A launcher rendering that answer cannot tell a desk that
    was removed from a desk whose file the platform would not open.

    Two fields rather than a bare list because the two facts have to travel
    TOGETHER: any seam that carried only the actors would re-open the hole at
    that seam, which is exactly how the projection acquired it.
    """

    actors: list[OfficeActor]
    #: How many ``*.json`` files in the scanned directories existed and did not
    #: decode. NEVER folded into ``actors`` and never silently zero.
    unreadable: int


def _unreadable_scan_failure(workspace_id: str, scan: ActorScan) -> dict:
    """The archive lane's failure row for a shortfall that belongs to NO actor.

    ``actor_key`` is ``None`` rather than a guess — the same shape
    ``agent_retire`` already uses when the office projection itself will not
    construct, and for the same reason: the value of this list is that it names
    what it knows and nothing else. The unreadable file's own key is precisely
    what could not be decoded, so naming one would be inventing it.

    The COUNT is what the scan can honestly supply (``_read_actor_dir`` keeps a
    per-class tally, not a path list), and the count is enough to do this row's
    job: turn an empty ``failures`` list back into the positive claim
    ``agent_retire``'s docstring says it is.
    """

    return {
        "actor_key": None,
        "workspace_id": workspace_id,
        # Class + count, never a message or a path — the disclosure rule this
        # list's other rows follow.
        "error": f"ActorsUnreadable: {scan.unreadable}",
    }


def _safe_actor_ref(value: Any, *, fallback: str = "operator") -> str:
    return safe_id(value) or fallback


def _normalize_persona_id(value: Any) -> str | None:
    # Mirrors the launcher's OfficeAgentIdentity normalization: trim + lower.
    text = str(value or "").strip().lower()
    return safe_id(text)


def _safe_folder(value: Any) -> str:
    return " ".join(str(value or "").split())[:80]


def _safe_display_name(value: Any) -> str | None:
    text = " ".join(str(value or "").split())[:120]
    return text or None


def _assert_display_name_publishable(name: str) -> None:
    """Typed write-time rejection of secret-shaped display names (plan §4.2).

    The realm-sync publish scan (`_assert_no_secret_artifacts`) hard-fails the
    WHOLE realm publish on a content match; rejecting at the write chokepoint
    keeps that scan defense-in-depth instead of the primary gate.
    """

    if SECRET_ASSIGNMENT_RE.search(name):
        raise ValueError("invalid_request: display_name looks like a secret assignment")


def _canonical_actor_key(persona_id: str, persona_instance_id: str | None) -> str:
    if persona_instance_id:
        from .persona_assignments import canonical_persona_instance_id  # single derivation authority

        canonical = canonical_persona_instance_id(persona_instance_id, persona_id=persona_id)
        if canonical:
            return canonical
    return persona_id


def _normalize_item(raw: Any, *, persona_id: str) -> OfficeItem:
    if not isinstance(raw, dict):
        raise ValueError("invalid_request: item must be an object")
    item_id = safe_id(raw.get("item_id"))
    if not item_id:
        raise ValueError("invalid_request: item_id required")
    item_persona = _normalize_persona_id(raw.get("persona_id")) or persona_id
    position = raw.get("position")
    if not isinstance(position, (list, tuple)) or len(position) < 2:
        raise ValueError("invalid_request: item position must be [x, y]")
    try:
        x = float(position[0])
        y = float(position[1])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_request: item position must be numeric") from exc
    if x != x or y != y or abs(x) == float("inf") or abs(y) == float("inf"):
        raise ValueError("invalid_request: item position must be finite")
    display_name = _safe_display_name(raw.get("display_name"))
    if display_name:
        _assert_display_name_publishable(display_name)
    return OfficeItem(
        item_id=item_id,
        persona_id=item_persona,
        kind=office_models.normalize_item_kind(raw.get("kind")),
        position=[x, y],
        folder=_safe_folder(raw.get("folder")),
        display_name=display_name,
        pet_slug=safe_id(raw.get("pet_slug")),
        scale=office_models.normalize_scale(raw.get("scale", office_models.SCALE_DEFAULT)),
    )


def _normalize_folders(values: Any) -> list[str]:
    folders: list[str] = [*office_models.DEFAULT_FOLDERS]
    if isinstance(values, (list, tuple)):
        for value in values:
            folder = _safe_folder(value)
            if folder and folder not in folders:
                folders.append(folder)
            if len(folders) >= MAX_FOLDERS:
                break
    return folders


def _duplicate_desk_collision(
    store: "OfficeStore",
    workspace_id: str,
    *,
    actor_key: str,
    items: list[OfficeItem],
) -> dict | None:
    """Would this write leave one persona holding TWO live desks? ``None`` if not.

    THE predicate — one derivation authority, the shape
    ``office_class_key_guard.class_key_collision`` established. The fence that
    spends it is ``OfficeStore._guard_duplicate_desk``; both doors
    (``serve_rpc._runtime_office_upsert``, ``harness office actor-upsert``) keep
    only a TRANSLATION of the typed refusal into their transport's taxonomy,
    never a second copy of the decision.

    The question is asked about the POST-WRITE state, not about the payload
    alone, because ``upsert_actor`` REPLACES the target actor's items: after the
    write persona ``P`` holds exactly the desks in this payload plus the desks
    every OTHER live actor holds for ``P``. Stating it that way is what makes
    the three cases in D6 fall out of one predicate instead of three branches:

    * a second actor authoring a desk for a persona another actor already
      desks → refused;
    * the SAME actor re-writing (moving, re-folding, re-scaling) its own desk →
      accepted, because its own row is excluded from the scan it is replacing;
    * a desk whose only holder is ARCHIVED → accepted, because
      ``scan_actors`` reads the LIVE directory and an archive is not a holding.

    Desks are keyed on the ITEM's persona, not the actor's. ``_normalize_item``
    lets an item carry its own ``persona_id`` (defaulting to the actor's), the
    launcher's guard is persona-keyed (``hasAuthoredDeskForPersona``), and an
    actor-keyed test would wave through the one shape the launcher already
    refuses: two actors of one persona, each desking it.

    A desk's IDENTITY is its ``item_id``, and the count is of DISTINCT ids —
    which is the same narrowing ``office_class_key_guard`` records for its own
    predicate, and for the same reason. One desk owned by two actor files is a
    duplicate PLACEMENT (``duplicate_item_placement``), a different fault with a
    different cure, and it is a state the class→instance re-key migration
    deliberately passes through: ``scripts/office_actor_rekey_to_instance.py``
    mints the instance-keyed actor with the class-keyed actor's items COPIED
    VERBATIM and only then archives the old key, so both rows briefly claim the
    same desk. Counting rows instead of ids would refuse that migration — the
    one operator script whose whole job is to move a placement — while catching
    nothing this fence is for. What this fence is for is a SECOND desk: a
    different id, which is what the 2026-08-24 incident authored and what the
    launcher's render-time detector counts.

    Read-only against the store, and NOT total — it raises
    :class:`~.errors.ActorsUnreadable` when the answer is unknowable rather than
    answering "no holder" from a directory it could only partly read. Same
    reasoning as the class-key fence's (EG-6.6): a desk holder can only be
    proven ABSENT by reading every actor that might be one, and a fence that
    reports "no conflict" from half a directory is a fence that fails open on
    exactly the corrupt store where it matters most.

    Costs a directory scan only when the payload actually carries a desk. The
    placement verb authors none (D6), so no ``agent create`` and no canvas drop
    pays for this.
    """

    incoming = [(item.persona_id, item.item_id) for item in items if item.kind == "desk"]
    if not incoming:
        return None

    scan = store.scan_actors(workspace_id)
    if scan.unreadable:
        raise ActorsUnreadable(
            f"actors_unreadable:{workspace_id} ({scan.unreadable} of "
            f"{len(scan.actors) + scan.unreadable} actor files) — the desk fence "
            "cannot prove this persona does not already hold a desk. Repair or "
            "remove the unreadable actor file and retry the same write."
        )

    # persona -> {desk item_id: the actor key holding it}. ``scan_actors`` sorts
    # by actor key and ``setdefault`` keeps the first, so the refusal names the
    # same holder on every machine and every retry.
    held: dict[str, dict[str, str]] = {}
    for actor in scan.actors:
        if actor.actor_key == actor_key:
            continue  # this write replaces its own items
        for item in actor.items:
            if item.kind == "desk":
                held.setdefault(item.persona_id, {}).setdefault(item.item_id, actor.actor_key)

    # The payload's own desks count too. Two desks for one persona inside ONE
    # payload is the same invariant reached without any existing row, and
    # excluding it would leave the fence trivially walkable by the very writer
    # it was built for (a hand-assembled ``--actor-json``).
    staged: dict[str, dict[str, str]] = {}
    for persona_id, item_id in incoming:
        others = held.get(persona_id) or {}
        mine = staged.setdefault(persona_id, {})
        mine.setdefault(item_id, actor_key)
        # Every DISTINCT desk this persona would hold after the write, with the
        # actor that holds each. More than one is the refusal.
        after = {**others, **{k: v for k, v in mine.items() if k not in others}}
        if len(after) > 1:
            holder_id, holder_key = next(
                (k, v) for k, v in after.items() if k != item_id
            )
            return {
                "workspace_id": workspace_id,
                "actor_key": actor_key,
                "persona_id": persona_id,
                "item_id": item_id,
                "holding_actor_key": holder_key,
                "holding_item_id": holder_id,
            }
    return None


def _duplicate_desk_message(collision: dict) -> str:
    """One operator-readable line naming the holder and the way out.

    It names the holding actor and item because ``emit_harness_error`` merges
    ``safe_details`` for three exception types it lists explicitly and this is
    not one of them — a refusal that does not name what is already there is a
    refusal nobody can act on. Same reason ``office_class_key_guard
    .refusal_message`` carries its conflicting keys.
    """

    return (
        f"office write for persona {collision['persona_id']!r} into "
        f"{collision['workspace_id']!r} refused: desk item "
        f"{collision['item_id']!r} would be a SECOND live desk — "
        f"{collision['holding_actor_key']!r} already holds "
        f"{collision['holding_item_id']!r}. A persona has one desk on a level "
        "(desks are shared across that persona's instances). Move the existing "
        "desk instead of authoring another, or remove it with `harness office "
        "actor-remove` first."
    )


class OfficeStore:
    def __init__(self, event_log: EventLog | None = None) -> None:
        self.event_log = event_log or EventLog()

    # --- event emission (single chokepoint) ------------------------------

    def _emit(self, event_type: str, correlation_id: str | None = None, **payload: Any) -> None:
        """Append one office DOMAIN event.

        ``correlation_id`` (EG-2.3 / Plan D §V2) is the gesture token, threaded
        from the write boundary. It is a plain payload key, so the delta lane
        lifts it to ``entity.correlation_id`` for free and the office
        notification forwards it verbatim — no wire change on either lane.
        Positional-or-keyword rather than keyword-only because ``**payload``
        would otherwise swallow it; ``None`` is filtered out by the comprehension
        below exactly as every other absent field is, which is what keeps a
        gestureless event byte-identical to before this key existed.
        """

        try:
            # Function-local like every other ``state_patches`` reach in this
            # class, so the patch module's import weight stays off the store's
            # own import path.
            from .state_patches import CORRELATION_ID_KEY, normalize_correlation_id

            body = {key: value for key, value in payload.items() if value is not None}
            token = normalize_correlation_id(correlation_id)
            if token is not None:
                body[CORRELATION_ID_KEY] = token
            self.event_log.append(Event(now(), event_type, None, None, None, body))
        except Exception:
            import logging

            logging.getLogger(__name__).warning("office event append failed: %s", event_type, exc_info=True)

    def _emit_actor_patch(
        self, actor: OfficeActor, *, created: bool, correlation_id: str | None = None
    ) -> None:
        """Emit the S7-A ``office_actor`` patch for one actor write.

        Called from INSIDE ``office_lock`` and handed the in-memory actor that
        was just written. Both are load-bearing, and together they are the
        monotonicity guarantee the whole patch lane rests on:

        * **Inside the lock.** ``office_lock`` is a cross-process file lock, so
          holding it across (write file → append event) makes EventLog order
          agree with revision order for a given actor. Appending after release
          admits the inversion: writer A takes rev 2 and writer B takes rev 3,
          B's append wins the race, and the fold applies rev 3 at the lower
          offset then rev 2 at the higher — leaving the launcher's core at rev 2
          while disk says 3, with no gap for the ``base_offset`` check to catch.
          The stream watermark stays monotonic (it is the log offset); the
          ENTITY watermark would not be, and nothing downstream would notice.
        * **From the written object rather than a re-read.** Stated honestly
          after mutation testing showed it is NOT independently load-bearing:
          swapping in a ``get_actor`` re-read stayed green, because the re-read
          happens under the same lock and therefore cannot see another writer.
          It is defense-in-depth for the day someone moves this call out of the
          lock — the placement above is the actual guarantee, and the re-read
          would only turn a lock bug into a subtler one. Do not read the two
          bullets as two independent guards; there is one, and it is the lock.

        ``created`` says the row was ABSENT before this write — a first
        placement, or a re-add resurrecting an archived key. It rides the patch
        as an additive marker so ``patch_coverage`` can gate the widened op
        behind the ``office_actor_lifecycle`` capability token; the fold itself
        inserts-on-absent either way. It replaces the old
        ``replaced_existing``/``surface_rewritten`` pair, which existed to route
        creates and ledger-clearing re-adds onto ``refresh`` because a patch
        could not express the parent office row. The 2026-08-16 fold-promotion
        plan (§V1) retired that: ``actor_count``, ``actors_truncated`` and the
        ``archived_actor_keys`` delta under the two lifecycle ops are exactly
        derivable by a client from the rows it folds, so they no longer need a
        wire row and these writes no longer need to demote.

        The ONE case a patch still cannot express is TRUNCATION. Past
        ``MAX_OFFICE_ACTORS_PROJECTED`` the snapshot projects a CUT of the actor
        list, and which actors survive the cut is not client-decidable — neither
        the row's presence nor the derived counts can be folded — so the count is
        taken post-write, under the lock that is already held, and a workspace
        over the bound keeps the honest ``refresh``.

        Cost of that count, named rather than discovered: ``list_actors`` is a
        directory glob plus a JSON parse per actor, on a mutation path. It is
        bounded by the same 200-actor projection cap it is checking, and it runs
        under a lock that already serializes office writes — the alternative (a
        filename glob) would count unparseable files as members, which is the
        wrong direction for a guard whose job is to know when the projection is
        no longer complete.

        Best-effort like ``_emit`` beside it: a patch-lane fault must never take
        an office write down, and a missing patch is a missing PROMOTION — the
        batch then ships the full core it would have shipped before this lane
        existed.
        """

        try:
            from .snapshot import MAX_OFFICE_ACTORS_PROJECTED
            from .state_patches import emit_office_actor_patch, emit_office_actor_refresh

            if len(self.list_actors(actor.workspace_id)) > MAX_OFFICE_ACTORS_PROJECTED:
                emit_office_actor_refresh(
                    self.event_log,
                    actor.workspace_id,
                    actor.actor_key,
                    correlation_id=correlation_id,
                )
            else:
                emit_office_actor_patch(
                    self.event_log, actor, created=created, correlation_id=correlation_id
                )
        except Exception as exc:
            import logging

            # The CLASS in the message, not only in the traceback. This swallow is
            # one of the five paths that leave a covered domain event without its
            # paired patch, and the stream now DEMOTES that batch to a full core
            # (`snapshot_build reason=demote`) rather than shipping an empty patch
            # frame. A grep-joinable class name is what makes that demote
            # attributable instead of mysterious.
            logging.getLogger(__name__).warning(
                "office actor patch emit failed: %s error=%s",
                actor.actor_key,
                type(exc).__name__,
                exc_info=True,
            )

    def _emit_surface_patch(
        self, surface: OfficeSurface, *, correlation_id: str | None = None
    ) -> None:
        """Emit the ``office_surface`` patch for one folder-taxonomy write.

        Called from INSIDE ``office_lock`` and BEFORE the paired
        ``office.surface.updated``, for the two reasons ``_emit_actor_patch``
        gives at length: the cross-process lock is what makes EventLog order
        agree with revision order for this surface, and the pairing is what lets
        the coverage classifier treat the domain event as carrying no fold state
        of its own. A patch appended after the lock admits the inversion where a
        lower revision lands at a higher offset and the client holds the older
        folder list with nothing downstream to notice.

        No truncation guard, unlike ``_emit_actor_patch``. That guard exists
        because past ``MAX_OFFICE_ACTORS_PROJECTED`` the snapshot ships a CUT of
        the actor list and the derived counts stop being client-computable.
        This row touches neither the list nor the counts — ``folders`` and the
        surface ``revision`` are projected whole at any office size — so there
        is no size at which it stops being expressible.

        Best-effort like ``_emit`` beside it: a patch-lane fault must never take
        a folder write down, and a missing patch is a missing PROMOTION — the
        batch ships the full core it would have shipped before this lane
        existed.
        """

        try:
            from .state_patches import emit_office_surface_patch

            emit_office_surface_patch(
                self.event_log, surface, correlation_id=correlation_id
            )
        except Exception as exc:
            import logging

            # See ``_emit_actor_patch``: the exception class rides the message so
            # the demote this suppression now forces is attributable.
            logging.getLogger(__name__).warning(
                "office surface patch emit failed: %s error=%s",
                surface.workspace_id,
                type(exc).__name__,
                exc_info=True,
            )

    def _emit_actor_remove_patch(
        self, actor: OfficeActor, *, correlation_id: str | None = None
    ) -> None:
        """Emit the ``office_actor`` ``remove`` for one archive.

        No truncation guard, deliberately: a remove carries no ``changed`` and
        makes no claim about the projected list's membership — it says one key
        left, which is true under a cut as well as under a complete list. The
        client's own truncated-base guard is what refuses the fold when its held
        projection was already a cut.

        Best-effort, like every emitter in this class.
        """

        try:
            from .state_patches import emit_office_actor_remove

            emit_office_actor_remove(
                self.event_log,
                actor.workspace_id,
                actor.actor_key,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            import logging

            # See ``_emit_actor_patch``: the exception class rides the message so
            # the demote this suppression now forces is attributable.
            logging.getLogger(__name__).warning(
                "office actor remove patch emit failed: %s error=%s",
                actor.actor_key,
                type(exc).__name__,
                exc_info=True,
            )

    # --- surface reads ----------------------------------------------------

    def get_surface(self, workspace_id: str) -> OfficeSurface:
        path = paths.office_surface_path(workspace_id)
        if not path.exists():
            raise NotFound(f"office:{workspace_id}")
        return from_jsonable(OfficeSurface, _read_json(path))

    def surface_exists(self, workspace_id: str) -> bool:
        return paths.office_surface_path(workspace_id).exists()

    def list_workspaces(self) -> list[str]:
        root = paths.office_root()
        if not root.exists():
            return []
        return sorted(child.name for child in root.iterdir() if (child / "office.json").exists())

    # --- surface writes ---------------------------------------------------

    def ensure_surface(self, workspace_id: str, *, created_by: str = "operator") -> OfficeSurface:
        """Lazily create the deterministic default surface for a workspace.

        Idempotent: if the surface already exists it is returned unchanged (no
        event). Two machines calling this converge on identical semantic
        content (fixed folders + timestamp-excluded content hash).

        REFUSES typed (:class:`~.errors.WorkspaceUnresolved`) when no workspace
        record resolves the id. Until MC-8 this authored a surface for ANY id
        that passed ``safe_id``, which is how a leaked test context minted a
        LIVE office — 135 events and a ``revision 67`` actor file — for a
        workspace that never existed. The parity warning could describe that
        afterwards; this is the door.

        THE ORDER IS THE CONTRACT, and each step is placed against a specific
        failure:

        1. ``safe_id`` FIRST — an unusable id is a caller error, not a question
           about store state, and asking the store about it would be asking a
           question with no meaning.
        2. the ``surface_exists`` short-circuit SECOND, i.e. **the refusal guards
           CREATION and never reading**. An office whose workspace record has
           since disappeared must still be returned: the projection, the parity
           warning and the archive verb all read through here, so refusing an
           existing orphan would break every path the operator has for cleaning
           one up — including ``archive_orphaned_surface``, whose entire
           precondition is ``workspace_resolves() is False``. Putting the refusal
           above this line would make the live orphan unarchivable by the verb
           that exists to archive it.
        3. the refusal THIRD — before ``office_lock`` and before any write. The
           same discipline ``store.WorkspaceStore.delete``'s cascade follows:
           refused before the first irreversible step, so a refused call leaves
           the store exactly as it found it. Nothing is created, no lock is
           taken, no event is emitted — which is what makes "it refused" and "it
           refused before doing anything" one statement here instead of two.
        """

        wsid = safe_id(workspace_id)
        if not wsid:
            raise ValueError("invalid_request")
        if self.surface_exists(wsid):
            return self.get_surface(wsid)
        if not self.workspace_resolves(wsid):
            # ``workspace_resolves`` is the SHARED predicate — the one
            # ``archive_orphaned_surface`` refuses on, derived from the same
            # membership the ``orphaned_office`` parity warning uses — so the
            # door and the diagnostic cannot answer differently about one id.
            # Archived workspaces resolve, deliberately: an archived workspace is
            # a real record and its office is not an orphan.
            raise WorkspaceUnresolved(
                f"no workspace record resolves '{wsid}', so an office surface "
                "will not be authored for it; create the workspace first, or if "
                "the id is expected to arrive by realm sync, pull the realm "
                "before placing into it",
                safe_details={"workspace_id": wsid},
            )
        with office_lock(wsid):
            if self.surface_exists(wsid):
                return self.get_surface(wsid)
            surface = office_models.default_surface(wsid, created_at=now(), updated_by=_safe_actor_ref(created_by))
            _write_surface(surface)
            self._emit("office.surface.created", workspace_id=surface.workspace_id)
        return self.get_surface(wsid)

    def update_surface(
        self,
        workspace_id: str,
        *,
        folders: list[str] | None = None,
        updated_by: str = "operator",
        expect_revision: int | None = None,
        correlation_id: str | None = None,
        dry_run: bool = False,
    ) -> OfficeSurface:
        wsid = safe_id(workspace_id)
        if not wsid:
            raise ValueError("invalid_request")
        # Read BEFORE the ensure below, because the ensure is what makes the
        # answer stop being true. It decides whether this write gets an
        # ``office_surface`` patch at all: a folder write that AUTHORED the
        # office is a create as far as any reader is concerned, and the patch is
        # a three-field SUBSET — a client that has never held this workspace
        # would answer it ``patch_without_target`` and re-hydrate, paying the
        # patch AND the core. That is not hypothetical: ``workspace_template``
        # clones an office by calling ``ensure_surface`` and then this, and a
        # template clone is exactly the case where no client holds the row.
        # Creates stay full-core, which is the same ruling
        # ``office.surface.created`` already rides.
        surface_existed = self.surface_exists(wsid)
        if not dry_run:
            self.ensure_surface(wsid, created_by=updated_by)
        with office_lock(wsid):
            # A dry-run against an unauthored office validates + previews against
            # the WOULD-BE default surface without persisting it (no ensure_surface
            # write above), so the preview is honest and the store stays untouched.
            if self.surface_exists(wsid):
                surface = self.get_surface(wsid)
            else:
                surface = office_models.default_surface(
                    wsid, created_at=now(), updated_by=_safe_actor_ref(updated_by)
                )
            _check_revision(surface.revision, expect_revision)
            change = []
            if folders is not None:
                surface.folders = _normalize_folders(folders)
                change.append("folders")
            surface.revision += 1
            surface.updated_at = now()
            surface.updated_by = _safe_actor_ref(updated_by)
            if dry_run:
                # Full validation + revision check ran; return the would-be
                # surface in memory. Write nothing, emit no event.
                return surface
            _write_surface(surface)
            if surface_existed:
                self._emit_surface_patch(surface, correlation_id=correlation_id)
            self._emit(
                "office.surface.updated",
                correlation_id,
                workspace_id=surface.workspace_id,
                change=",".join(change) or "saved",
                revision=surface.revision,
            )
        return self.get_surface(wsid)

    # --- actor reads ------------------------------------------------------

    def get_actor(self, workspace_id: str, actor_key: str) -> OfficeActor:
        path = paths.office_actor_path(workspace_id, actor_key)
        if not path.exists():
            raise NotFound(f"office_actor:{actor_key}")
        return from_jsonable(OfficeActor, _read_json(path))

    def actor_exists(self, workspace_id: str, actor_key: str) -> bool:
        return paths.office_actor_path(workspace_id, actor_key).exists()

    def scan_actors(self, workspace_id: str, *, include_archived: bool = False) -> ActorScan:
        """Every actor this workspace HAS, plus how many files did not decode.

        THE chokepoint. ``list_actors`` is the thin list view over it, so the
        sixteen callers that only want rows keep their signature while the one
        caller that must not lie about completeness — the office projection both
        ``runtime.office.get`` and ``runtime.office.subscribe`` answer from — can
        ask the fuller question. Forking those two readers is the failure this
        shape forbids: a count that reached ``get`` and not the subscribe
        baseline would put the two back in the silent-disagreement state
        ``_office_projection`` was extracted to end.
        """

        scan = self._read_actor_dir(paths.office_actors_dir(workspace_id))
        actors = scan.actors
        unreadable = scan.unreadable
        if include_archived:
            archived = self._read_actor_dir(paths.office_archive_dir(workspace_id))
            actors = [*actors, *archived.actors]
            unreadable += archived.unreadable
        return ActorScan(sorted(actors, key=lambda a: a.actor_key), unreadable)

    def list_actors(self, workspace_id: str, *, include_archived: bool = False) -> list[OfficeActor]:
        return self.scan_actors(workspace_id, include_archived=include_archived).actors

    def conflict_actor_keys(self, workspace_id: str) -> list[str]:
        conflicts_dir = paths.office_conflicts_dir(workspace_id)
        if not conflicts_dir.exists():
            return []
        keys: list[str] = []
        for path in sorted(conflicts_dir.glob("*.json")):
            if path.name.endswith(".resolved.json"):
                continue
            try:
                payload = _read_json(path)
                key = str(payload.get("actor_key") or "").strip()
            except Exception:
                key = ""
            keys.append(key or path.stem)
        return keys

    # --- actor writes -----------------------------------------------------

    def upsert_actor(
        self,
        workspace_id: str,
        payload: dict[str, Any],
        *,
        updated_by: str = "operator",
        expect_revision: int | None = None,
        allow_class_key: bool = False,
        resurrect: bool = False,
        correlation_id: str | None = None,
        dry_run: bool = False,
    ) -> OfficeActor:
        """Write ONE actor placement, creating the surface if it does not exist.

        ``allow_class_key`` is the sanctioned override for the class→instance
        re-key fence below (``_guard_class_keyed_write``). It is a STORE
        parameter and not a caller-side fence omission on purpose (EG-6.6): an
        override has to be a value someone passed on the record, because the
        alternative — a caller that simply does not call the guard — is
        indistinguishable from a caller that forgot, which is how this fence
        came to exist at four call sites around one store. Only ``harness office
        actor-upsert --allow-class-key`` passes it; the wire lane deliberately
        has no equivalent (a parameter is not consent — see
        ``serve_rpc._runtime_office_upsert``).

        ``resurrect`` is the same kind of parameter for the tombstone fence
        (D1): without it, an upsert of a key that has an archive copy or a live
        resurrection-guard ledger entry raises :class:`ActorArchived` instead of
        re-adding it. It is ORTHOGONAL to ``allow_class_key`` and deliberately
        not implied by it — one answers "may this write use a class key", the
        other "may this write raise the dead", and an operator who consented to
        the first was never asked the second. Only ``harness office
        actor-upsert --resurrect`` passes it; the wire lane again has no
        equivalent, for the reason spelled out there.
        """

        wsid = safe_id(workspace_id)
        if not wsid:
            raise ValueError("invalid_request")
        if not isinstance(payload, dict):
            raise ValueError("invalid_request: actor payload must be an object")
        persona_id = _normalize_persona_id(payload.get("persona_id"))
        if not persona_id:
            raise ValueError("invalid_request: persona_id required")
        raw_instance = str(payload.get("persona_instance_id") or "").strip() or None
        actor_key = _canonical_actor_key(persona_id, raw_instance)
        raw_items = payload.get("items")
        if not isinstance(raw_items, (list, tuple)) or not raw_items:
            raise ValueError("invalid_request: items required")
        if len(raw_items) > MAX_ITEMS_PER_ACTOR:
            raise ValueError("invalid_request: too many items")
        items = [_normalize_item(item, persona_id=persona_id) for item in raw_items]

        if not dry_run:
            self.ensure_surface(wsid, created_by=updated_by)
        with office_lock(wsid):
            # THE class-key fence, first and inside the lock (EG-6.6). First
            # because that is the precedence the four caller-side copies had —
            # they all ran before any store call — and inside the lock because
            # the predicate reads the ledger and the live actor set, which a
            # concurrent writer can move between a caller's read and this write.
            self._guard_class_keyed_write(wsid, payload, allow_class_key=allow_class_key)
            self._guard_no_conflict(wsid, actor_key)
            # THE desk fence (D6), inside the same lock and before any write.
            # After the class-key fence and the conflict guard on purpose: those
            # two refuse writes that are illegitimate whatever they carry, and a
            # payload that is both class-keyed AND desk-duplicating should hear
            # the older, narrower refusal first — its remedy (send the binding)
            # is the one that also dissolves this one. Before the revision check
            # because a stale prediction is a retryable race and a second desk is
            # not: telling the operator to refetch and replay a write this fence
            # will refuse again is advice that cannot work.
            self._guard_duplicate_desk(wsid, actor_key=actor_key, items=items)
            existing: OfficeActor | None = None
            if self.actor_exists(wsid, actor_key):
                existing = self.get_actor(wsid, actor_key)
            archived_path = paths.office_archived_actor_path(wsid, actor_key)
            # THE tombstone fence (D1), before the archive is read rather than
            # after. Without consent this write is refused whatever the archive
            # decodes to, so decoding it first would only mean answering
            # ``archive_unreadable`` — "ask again once the file is readable" —
            # to a caller whose write can never be accepted no matter how
            # readable the file becomes. ``ArchiveUnreadable`` stays reachable on
            # the CONSENTED path below, which is the path that actually needs the
            # revision token the archive carries.
            #
            # ``existing is None`` is load-bearing: a LIVE row whose key also
            # sits in the ledger is a ledger the write should clean up, not a
            # resurrection, and that half of the arm still runs untouched.
            if existing is None:
                self._guard_archived_actor(
                    wsid,
                    actor_key=actor_key,
                    persona_instance_id=(
                        _canonical_actor_key(persona_id, raw_instance)
                        if raw_instance
                        else None
                    ),
                    archived_path=archived_path,
                    resurrect=resurrect,
                )
            archived: OfficeActor | None = None
            if existing is None and archived_path.exists():
                # REFUSED, not swallowed. This read is where the revision guard's
                # token lives between a remove and the re-add that follows it:
                # ``base_revision`` below takes the archived revision precisely so
                # a re-added key carries its history forward. ``archived = None``
                # made the base 0 and the new revision 1 — a token BELOW the one
                # every peer and every launcher read model already holds, so the
                # next guarded write on this key reads as a stale prediction
                # against a server that silently rewound. A fresh start is a
                # decision an operator makes (``actor-restore``, a deliberate
                # re-key), never one an unreadable file makes for them.
                try:
                    archived = from_jsonable(OfficeActor, _read_json(archived_path))
                except Exception as exc:
                    raise ArchiveUnreadable(
                        f"archive_unreadable:{actor_key} ({type(exc).__name__})"
                    ) from exc
            _check_revision(existing.revision if existing else None, expect_revision)
            ts = now()
            base_revision = existing.revision if existing else (archived.revision if archived else 0)
            actor = OfficeActor(
                actor_key=actor_key,
                workspace_id=wsid,
                persona_id=persona_id,
                persona_instance_id=_canonical_actor_key(persona_id, raw_instance) if raw_instance else None,
                backing_profile=_normalize_persona_id(payload.get("backing_profile")),
                items=items,
                state="active",
                revision=base_revision + 1,
                created_at=existing.created_at if existing else ts,
                updated_at=ts,
                updated_by=_safe_actor_ref(updated_by),
            )
            if dry_run:
                # Full validation (payload/items/secret-name), conflict guard, and
                # revision check ran above; return the would-be actor in memory.
                # Write nothing, touch no ledger, emit no event.
                return actor
            surface = self.get_surface(wsid)
            _write_actor(actor)
            # An explicit local upsert of an archived key is operator intent to
            # re-add: clear the resurrection-guard ledger entry + archive copy
            # so a later pull doesn't re-archive it. The client mirrors exactly
            # this delta during the fold (§V1's derivation table), which is why
            # it no longer forces the write onto the full-core lane.
            if actor_key in surface.archived_actor_keys:
                surface.archived_actor_keys = [k for k in surface.archived_actor_keys if k != actor_key]
                surface.updated_at = ts
                _write_surface(surface)
            archived_path.unlink(missing_ok=True)
            # INSIDE the lock, and from the actor object just written — see
            # _emit_actor_patch for why both halves of that are load-bearing.
            #
            # ``created`` is ABSENCE of the live row, not absence of the key: a
            # resurrection re-add is created=True because the row is missing from
            # the client's list, which is the only question the fold's
            # insert-on-absent asks.
            self._emit_actor_patch(
                actor, created=existing is None, correlation_id=correlation_id
            )
            self._emit(
                "office.actor.upserted",
                correlation_id,
                workspace_id=wsid,
                actor_key=actor_key,
                persona_id=persona_id,
                items=len(items),
                revision=actor.revision,
            )
        return self.get_actor(wsid, actor_key)

    def remove_actor(
        self,
        workspace_id: str,
        actor_key: str,
        *,
        reason: str = "operator",
        updated_by: str = "operator",
        expect_revision: int | None = None,
        correlation_id: str | None = None,
        dry_run: bool = False,
    ) -> OfficeActor:
        wsid = safe_id(workspace_id)
        if not wsid:
            raise ValueError("invalid_request")
        with office_lock(wsid):
            if not self.actor_exists(wsid, actor_key):
                # Idempotent: already archived → return the archived copy.
                archived_path = paths.office_archived_actor_path(wsid, actor_key)
                if archived_path.exists():
                    # Typed for the same reason the upsert's twin is: the ack
                    # this branch returns CARRIES the revision, so a decode
                    # failure here is the guard token going missing, not a
                    # generic handler crash. Raising the same class means one
                    # reason string covers the condition on both write verbs.
                    try:
                        return from_jsonable(OfficeActor, _read_json(archived_path))
                    except Exception as exc:
                        raise ArchiveUnreadable(
                            f"archive_unreadable:{actor_key} ({type(exc).__name__})"
                        ) from exc
                raise NotFound(f"office_actor:{actor_key}")
            actor = self.get_actor(wsid, actor_key)
            _check_revision(actor.revision, expect_revision)
            if dry_run:
                # Existence + revision check ran; return the would-be archived
                # actor in memory (mirrors _archive_actor_locked's mutation) and
                # persist nothing / emit nothing.
                actor.state = "archived"
                actor.revision += 1
                actor.updated_at = now()
                actor.updated_by = _safe_actor_ref(updated_by)
                return actor
            surface = self.ensure_surface(wsid, created_by=updated_by)
            self._archive_actor_locked(
                surface, actor, reason=reason, updated_by=updated_by, correlation_id=correlation_id
            )
        return from_jsonable(OfficeActor, _read_json(paths.office_archived_actor_path(wsid, actor_key)))

    def restore_actor(
        self,
        workspace_id: str,
        actor_key: str,
        *,
        updated_by: str = "operator",
        correlation_id: str | None = None,
        dry_run: bool = False,
    ) -> OfficeActor:
        """Un-archive one actor placement — ``harness office actor-restore``.

        The third and last production writer of a live actor file, and the one
        with NO class-key fence — which is a disposition, not an omission
        (EG-6.6's enumeration witness names it as such). Restoring an archived
        class key IS the deliberate resurrection the fence refuses on every other
        path: it takes no payload to be wrong about, writes back exactly the
        bytes the archive holds, and is the very exit
        ``office_class_key_guard.refusal_message`` tells the operator to take.
        Fencing the sanctioned override against itself would leave the refusal
        pointing at a dead end.
        """

        wsid = safe_id(workspace_id)
        if not wsid:
            raise ValueError("invalid_request")
        with office_lock(wsid):
            archive_path = paths.office_archived_actor_path(wsid, actor_key)
            if not archive_path.exists():
                raise NotFound(f"office_actor:{actor_key}")
            actor = from_jsonable(OfficeActor, _read_json(archive_path))
            actor.state = "active"
            actor.revision += 1
            actor.updated_at = now()
            actor.updated_by = _safe_actor_ref(updated_by)
            if dry_run:
                # Archived-copy existence checked; return the would-be restored
                # actor in memory without writing / unlinking / emitting.
                return actor
            _write_actor(actor)
            archive_path.unlink(missing_ok=True)
            surface = self.ensure_surface(wsid, created_by=updated_by)
            if actor_key in surface.archived_actor_keys:
                surface.archived_actor_keys = [k for k in surface.archived_actor_keys if k != actor_key]
                surface.updated_at = now()
                _write_surface(surface)
            self._emit(
                "office.actor.restored",
                correlation_id,
                workspace_id=wsid,
                actor_key=actor_key,
            )
        return self.get_actor(wsid, actor_key)

    # --- realm-sync adoption (the pull's write arms) -----------------------

    def adopt_remote_surface(
        self,
        surface: OfficeSurface,
        *,
        updated_by: str = "realm_sync",
        correlation_id: str | None = None,
    ) -> OfficeSurface:
        """Write a PEER's office surface verbatim — the realm pull's surface arm.

        ``office_sync.apply_office_pull`` wrote this row with a raw
        ``atomic_json_write`` until H1, so the ONE lane that rewrites a live
        office from outside this machine emitted nothing while the archive arm
        beside it (``remove_actor``) emitted a full pair. The asymmetry was the
        defect: a pull that DELETED a desk reached every live consumer and a pull
        that GAVE you one reached none.

        A verb of its own rather than ``update_surface``, because the two write
        different things. ``update_surface`` authors LOCAL intent — it lazily
        creates the surface (refusing an unresolved workspace), normalizes the
        folder list and BUMPS the revision. A pull adopts a record the peer
        already numbered: the revision is the REMOTE's or the next
        ``classify_three_way_pull`` re-classifies a row nobody edited, and the
        surface must be writable for a workspace whose local record has not
        arrived yet (the pull is exactly how such a workspace arrives).

        ``updated_by`` records the SYNC, matching the archive arm's
        ``updated_by="realm_sync"``. It is hash-neutral: ``office_content_hash``
        excludes ``updated_by`` (with ``revision`` and the timestamps), so the
        caller's ``baseline[key] = remote_hash`` stays keyed off the remote
        content.

        Verbatim in every field BUT ONE. ``archived_actor_keys`` is UNIONED with
        the local ledger (:func:`merge_archived_ledgers`), because that field is
        not the peer's opinion about this workspace — it is the resurrection
        guard, and the pull is the one lane that can reach it from outside this
        machine. Adopting it wholesale erased any tombstone the peer had not
        heard of, which is a deletion of the exact evidence
        ``classify_three_way_pull(..., locally_archived=…)`` reads to refuse a
        resurrection. Reachable, not theoretical: publish records the LOCAL hash
        as the baseline, so an install that archives a desk and publishes is
        ``unchanged`` on its next pull — and one peer edit away from
        ``take_remote`` over its own ledger.

        The merge is hash-neutral wherever nothing was actually lost (the peer's
        order leads, so a local subset re-hashes to the remote's exact list); it
        is deliberately hash-CHANGING when a local-only key survives, which
        leaves the surface classified as locally edited until the next publish
        carries the fuller ledger back to the realm. That is the honest state:
        this install now holds a ledger the realm has not seen.

        A CREATE emits only the domain ``office.surface.created`` and no patch —
        the same ruling ``update_surface`` rides for the same reason (a client
        that has never held this workspace answers a three-field subset with
        ``patch_without_target`` and pays for the patch AND the core).
        """

        wsid = safe_id(surface.workspace_id)
        if not wsid:
            raise ValueError("invalid_request")
        surface.workspace_id = wsid
        surface.updated_by = _safe_actor_ref(updated_by)
        with office_lock(wsid):
            existed = self.surface_exists(wsid)
            if existed:
                # Read INSIDE the lock that will hold for the write: a local
                # archive racing this pull must land on one side of the union or
                # the other, never between the read and the write.
                surface.archived_actor_keys = merge_archived_ledgers(
                    surface.archived_actor_keys,
                    self.get_surface(wsid).archived_actor_keys,
                )
            _write_surface(surface)
            if existed:
                # INSIDE the lock and BEFORE the domain event, like every other
                # emitter in this class — see ``_emit_surface_patch``.
                self._emit_surface_patch(surface, correlation_id=correlation_id)
                self._emit(
                    "office.surface.updated",
                    correlation_id,
                    workspace_id=wsid,
                    change="realm_sync",
                    revision=surface.revision,
                )
            else:
                self._emit("office.surface.created", workspace_id=wsid)
        return surface

    def adopt_remote_actor(
        self,
        actor: OfficeActor,
        *,
        updated_by: str = "realm_sync",
        correlation_id: str | None = None,
    ) -> OfficeActor:
        """Write a PEER's actor row verbatim — the realm pull's adopt/converge arm.

        The twin of :meth:`adopt_remote_surface`, and the half that actually
        puts a desk on somebody's canvas. Before H1 this was
        ``atomic_json_write(paths.office_actor_path(...))`` inside
        ``apply_office_pull``, so an adopted desk was invisible to the office
        subscribe lane, to the patch fold, and to anything tailing ``office.*``.

        Three properties the raw write had and this verb must not lose, each
        pinned by a test rather than by this sentence:

        (a) **the revision is the REMOTE's.** No ``base_revision + 1``. A pull
            that renumbered revisions would hand the next
            ``classify_three_way_pull`` a row that looks locally edited, turning
            every subsequent pull of an untouched desk into a conflict.
        (b) **``updated_by`` records the sync**, matching the archive arm
            (``remove_actor(..., updated_by="realm_sync")``) rather than
            defaulting to ``"operator"``. Hash-neutral, see the surface twin.
        (c) **nothing is re-derived from the write.** The caller keys its
            baseline off the REMOTE content hash; this verb returns the same
            object it wrote so no re-read can drift from it.

        NOT fenced — a RULING (operator, 2026-08-30, plan
        ``realm-actor-lifecycle-refactor`` D3), no longer an open carve-out.
        The class-key fence, the tombstone fence and the desk fence that
        ``upsert_actor`` spends all refuse LOCAL authoring intent, and a pull
        has no operator behind it to offer consent, so fencing it would mean
        refusing to hold a fact a peer already published with nobody present to
        take the override. A pulled duplicate desk (or a peer's un-migrated
        class key) is a conflict-lane fact about what a peer published, which is
        why the launcher's render-time ``duplicate_desk`` warning stays. The two
        REAL holes task #33 had bundled with this one were closed instead: the
        surface arm's tombstone-ledger overwrite (C1,
        :func:`merge_archived_ledgers`) and the pull archive arm's discarded
        outcome (C2, ``office_sync.OfficeArchiveOutcome``).
        ``tests/agent_runtime/test_office_class_key_one_fence.py`` carries the
        ruling and pins it at runtime.

        The neighbour one method down differs on purpose:
        :meth:`resolve_conflict` with ``take="remote"`` writes a peer's row too
        and IS class-key fenced, because an operator asked for it — see its
        docstring for the discriminator.

        The resurrection question is answered UPSTREAM and not here:
        ``classify_three_way_pull(..., locally_archived=True)`` never returns
        ``WRITE_REMOTE`` for a key this store archived, so this verb is never
        reached with a tombstoned key and does not need a second opinion about
        one.
        """

        wsid = safe_id(actor.workspace_id)
        if not wsid:
            raise ValueError("invalid_request")
        actor.workspace_id = wsid
        actor.updated_by = _safe_actor_ref(updated_by)
        with office_lock(wsid):
            # ABSENCE of the live row, asked under the lock that will hold for
            # the write — the same question ``upsert_actor`` asks, and the only
            # one the fold's insert-on-absent cares about.
            created = not self.actor_exists(wsid, actor.actor_key)
            _write_actor(actor)
            self._emit_actor_patch(actor, created=created, correlation_id=correlation_id)
            self._emit(
                "office.actor.upserted",
                correlation_id,
                workspace_id=wsid,
                actor_key=actor.actor_key,
                persona_id=actor.persona_id,
                items=len(actor.items),
                revision=actor.revision,
            )
        return actor

    def resolve_conflict(
        self,
        workspace_id: str,
        actor_key: str,
        *,
        take: str,
        updated_by: str = "operator",
        allow_class_key: bool = False,
        correlation_id: str | None = None,
        dry_run: bool = False,
    ) -> OfficeActor | None:
        """Resolve a realm-sync conflict sidecar for an actor. ``take=local``
        keeps the local actor; ``take=remote`` adopts the sidecar's remote copy
        (or archives the local actor for an edit-vs-remove tombstone). Always
        archives the sidecar and emits ``office.actor.conflict_resolved``.

        ``allow_class_key`` is the operator's on-the-record override for the
        class-key fence below (``harness office resolve-conflict
        --allow-class-key``); see ``_guard_class_keyed_adoption``.

        Why this arm IS fenced when :meth:`adopt_remote_actor` is not, given
        that both write a peer's row into the live directory of the same
        workspace: the discriminator every fence in this store keys on is LOCAL
        AUTHORING INTENT, not the row's provenance. A pull is automatic — no
        operator, no consent to offer, and nobody to take an override — so
        fencing it would only mean refusing to hold what a peer published (the
        D3 ruling at :meth:`adopt_remote_actor`). ``resolve-conflict --take
        remote`` is the opposite case in every respect: it is an operator
        gesture whose whole content is the decision to adopt the peer's copy as
        local truth, it has an override to hand them (``allow_class_key``), and
        the refusal it can raise points at a real next move. Same input class,
        opposite dispositions, one rule."""

        take = str(take or "").strip().lower()
        if take not in {"local", "remote"}:
            raise ValueError("invalid_request")
        wsid = safe_id(workspace_id)
        if not wsid:
            raise ValueError("invalid_request")
        with office_lock(wsid):
            sidecar_path = paths.office_conflict_path(wsid, actor_key)
            if not sidecar_path.exists():
                raise SyncConflict(f"no_conflict:{actor_key}")
            sidecar = _read_json(sidecar_path)
            result_actor: OfficeActor | None = None
            if take == "local":
                if self.actor_exists(wsid, actor_key):
                    result_actor = self.get_actor(wsid, actor_key)
            else:  # take == remote
                remote = sidecar.get("remote_actor")
                if isinstance(remote, dict):
                    actor = from_jsonable(OfficeActor, remote)
                    actor.workspace_id = wsid
                    actor.state = "active"
                    self._guard_class_keyed_adoption(wsid, actor, allow_class_key=allow_class_key)
                    actor.revision = max(int(actor.revision or 1), 1) + 1
                    actor.updated_at = now()
                    actor.updated_by = _safe_actor_ref(updated_by)
                    result_actor = actor
                    if not dry_run:
                        _write_actor(actor)
                elif self.actor_exists(wsid, actor_key):
                    # Remote removed the actor (edit-vs-remove) → archive local.
                    actor = self.get_actor(wsid, actor_key)
                    if not dry_run:
                        surface = self.ensure_surface(wsid, created_by=updated_by)
                        self._archive_actor_locked(
                            surface,
                            actor,
                            reason="remote_removed",
                            updated_by=updated_by,
                            emit=False,
                            correlation_id=correlation_id,
                        )
            if dry_run:
                # take value + sidecar existence validated; return the would-be
                # resolved actor in memory. Leave the sidecar in place and emit
                # nothing (matches the real return, incl. None for edit-vs-remove).
                return result_actor
            _archive_conflict_sidecar(wsid, actor_key)
            self._emit(
                "office.actor.conflict_resolved",
                correlation_id,
                workspace_id=wsid,
                actor_key=actor_key,
                take=take,
                revision=getattr(result_actor, "revision", None),
            )
        return result_actor

    # --- orphaned-surface exit (EG-0.1 / HC §3) ----------------------------

    def workspace_resolves(self, workspace_id: str) -> bool:
        """Does a workspace record exist for ``workspace_id``?

        Derived the way :func:`snapshot._offices_summary` derives ``orphaned`` —
        membership in ``WorkspaceStore().list_all(include_archived=True)``
        (``snapshot.py:450``) — so this predicate and the ``orphaned_office``
        parity warning cannot answer differently. In particular an ARCHIVED
        workspace still resolves: its office is not an orphan and archiving it
        would be data loss, not cleanup.
        """

        wsid = safe_id(workspace_id)
        if not wsid:
            return False
        from .store import WorkspaceStore

        return wsid in {
            getattr(w, "id", None)
            for w in WorkspaceStore().list_all(include_archived=True)
        }

    def archive_orphaned_surface(
        self,
        workspace_id: str,
        *,
        updated_by: str = "operator",
        dry_run: bool = False,
    ) -> dict:
        """Move a whole ORPHANED office surface out of the projection.

        The operator's exit from the ``orphaned_office`` parity warning. Before
        this existed, a surface whose workspace record had gone raised a HUD
        parity-warning chip forever and the only way to clear it was deleting
        files by hand in the live runtime root — so the honest instrument was a
        first-class verb (the same reasoning ``office resolve-conflict`` rides,
        and the board warning's "archive to repair" hint has had an equivalent
        since inception; the office side had none).

        REFUSES a surface whose workspace still resolves. That is the whole
        safety property: this moves an entire surface — folders, every active
        and archived placement, the conflict sidecars — so pointed at a LIVE
        workspace it is a mass delete wearing a cleanup verb's name. The check
        runs twice, once before the lock for a clean refusal and once inside it,
        because a workspace can be re-created between the two.

        Archive-never-delete, like every other removal in this store: the
        directory is MOVED under ``paths.office_surface_archive_root()``, never
        unlinked, so a mistaken archive is recoverable by moving it back.

        ``updated_by`` is on the domain event, not just the signature. It was
        accepted and discarded until 2026-08-19 — the ONE verb in this store
        that moves a whole surface was also the only one whose event could not
        say who moved it, which is exactly backwards: the 2026-08-15 mass
        archive is the precedent for wanting attribution most on the highest
        blast radius.
        """

        wsid = safe_id(workspace_id)
        if not wsid:
            raise ValueError("invalid_request")
        if not self.surface_exists(wsid):
            raise NotFound(f"office:{wsid}")
        self._guard_surface_is_orphaned(wsid)

        surface = self.get_surface(wsid)
        active = self.list_actors(wsid)
        archived = self.list_actors(wsid, include_archived=True)
        destination = _free_surface_archive_dir(wsid)
        result = {
            "workspace_id": surface.workspace_id,
            "revision": surface.revision,
            "folders": list(surface.folders),
            "actor_count": len(active),
            "archived_placement_count": max(0, len(archived) - len(active)),
            "archived_as": destination.name,
        }
        if dry_run:
            # Full validation + the refusal check ran; nothing moved, no event.
            return result

        with office_lock(wsid):
            # Re-checked under the lock: a workspace re-created (or a surface
            # already archived by a concurrent operator) between the checks
            # above and here must not be archived anyway.
            if not self.surface_exists(wsid):
                raise NotFound(f"office:{wsid}")
            self._guard_surface_is_orphaned(wsid)
            destination = _free_surface_archive_dir(wsid)
            destination.parent.mkdir(parents=True, exist_ok=True)
            paths.office_dir(wsid).rename(destination)
            result["archived_as"] = destination.name
            # The accounted degrade, emitted BEFORE the domain event and inside
            # the lock, exactly like the two emitters above. ``office_surface``
            # ``refresh`` says "this row is not expressible as a fold, re-fetch
            # it", which is the truth: the offices row and every office_actor row
            # under it left in one move and there is no remove-a-surface op on
            # this wire. It is what demotes the batch to a full core — WITHOUT it
            # the covered ``office.surface.updated`` below would be the only
            # entry in an otherwise-coverable batch, shipping a patch frame with
            # an EMPTY patches list: the client advances its watermark having
            # folded nothing and keeps the archived surface, and its
            # ``orphaned_office`` chip, forever. See
            # ``state_patches.emit_office_surface_refresh``.
            self._emit_surface_refresh_patch(surface.workspace_id)
            self._emit(
                "office.surface.updated",
                workspace_id=surface.workspace_id,
                change="archived",
                revision=surface.revision,
                updated_by=_safe_actor_ref(updated_by),
            )
        return result

    def _emit_surface_refresh_patch(self, workspace_id: str) -> None:
        """Best-effort like every emitter in this class — a patch-lane fault must
        never take the archive down, and a missing refresh is a missing DEMOTE
        that the batch's own uncovered content still forces on any client that
        did not declare ``office_surface``."""

        try:
            from .state_patches import emit_office_surface_refresh

            emit_office_surface_refresh(self.event_log, workspace_id)
        except Exception:
            import logging

            logging.getLogger(__name__).warning(
                "office surface refresh patch emit failed: %s", workspace_id, exc_info=True
            )

    def _guard_surface_is_orphaned(self, workspace_id: str) -> None:
        if self.workspace_resolves(workspace_id):
            raise ValueError(
                f"office surface '{workspace_id}' is NOT orphaned — its workspace "
                "still resolves, so archiving it would move a live surface (every "
                "actor placement included) out of the projection. Archive the "
                "workspace itself if that is what you meant."
            )

    # --- prune lane (plan §4.3) --------------------------------------------

    def _instance_bound_actor(self, actor, canonical: str) -> bool:
        """Is *actor* the placement of the instance whose canonical id is given?

        The ONE spelling of "bound to this instance", asked by both the prune
        below and the archived-side read beside it. Persona-id-keyed actors
        answer ``False`` by construction (they carry no ``persona_instance_id``)
        and survive instance churn by design.
        """

        from .persona_assignments import canonical_persona_instance_id

        bound = actor.persona_instance_id
        if not bound:
            return False
        return (
            canonical_persona_instance_id(bound, persona_id=actor.persona_id) or bound
        ) == canonical

    def archive_actors_for_instance(
        self,
        persona_instance_id: str,
        *,
        reason: str = "instance_reaped",
        correlation_id: str | None = None,
    ) -> dict:
        """Hermes prune-lane hook: archive every active placement bound to a
        reaped persona instance so no phantom desk file re-materializes the
        agent (NEVER a launcher-side filter — the orphan-tombstone precedent).
        Persona-id-keyed placements survive instance churn by design.

        Returns ``{"archived": N, "failed": M, "archived_actor_keys": [...],
        "failures": [{actor_key, workspace_id, error}]}``.

        The per-actor swallow KEEPS the loop — a prune must not die on one bad
        file, and the retirement it serves is authoritative with or without the
        office projection — but for a long time it swallowed the IDENTITIES too:
        a bare ``0`` meant two opposite things (nothing matched, and three
        matches all failed), and the counts that replaced it still could not
        answer *which* desk is still on the canvas after a retire said it was
        gone. Placement plan D7 makes that half VISIBLE at this ONE chokepoint,
        because a caller that re-derived the list would be a second,
        disagreeing answer to a question this loop already knows: the keys it
        archived, and the failures it survived, leave WITH the counts.

        The counts stay, and are the lists' lengths by construction; every
        existing caller keeps reading exactly the two keys it read before.

        ``correlation_id`` is the RETIRE GESTURE's token, and it rides down to
        each :meth:`remove_actor` so the ``office.actor.removed`` event and the
        ``state.patched`` remove row this loop produces carry the same id the
        operator's create half carried. It defaults to ``None``, which is what
        keeps the janitor/prune callers — who have no gesture behind them —
        byte-identical to before this parameter existed. A default of "mint one"
        would be worse than nothing: it would join a prune to a gesture that
        never happened.
        """

        target = str(persona_instance_id or "").strip()
        if not target:
            return {"archived": 0, "failed": 0, "archived_actor_keys": [], "failures": []}
        from .persona_assignments import canonical_persona_instance_id

        canonical = canonical_persona_instance_id(target) or target
        archived_keys: list[str] = []
        failures: list[dict] = []
        for wsid in self.list_workspaces():
            # ``scan_actors``, not ``list_actors``: the thin view answers "these
            # are the actors" for a directory it only partly read, and this loop
            # spends that answer on a COMPLETENESS claim — an empty ``failures``
            # is the retire ack's positive statement that every bound actor is
            # off the level (``agent_retire`` says so at its own docstring). A
            # bound desk whose file would not decode is not archived and is not
            # visible either way, so the shortfall becomes a failure row of its
            # own rather than a shorter loop nobody can see.
            scan = self.scan_actors(wsid)
            if scan.unreadable:
                failures.append(_unreadable_scan_failure(wsid, scan))
            for actor in scan.actors:
                if not self._instance_bound_actor(actor, canonical):
                    continue
                try:
                    self.remove_actor(
                        wsid,
                        actor.actor_key,
                        reason=reason,
                        updated_by="harness",
                        correlation_id=correlation_id,
                    )
                except Exception as exc:  # noqa: BLE001 — the loop survives one bad file
                    failures.append(
                        {
                            "actor_key": actor.actor_key,
                            "workspace_id": wsid,
                            # Class + message, never the traceback: this string
                            # rides an operator ack and a launcher decode.
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    continue
                archived_keys.append(actor.actor_key)
        return {
            "archived": len(archived_keys),
            "failed": len(failures),
            "archived_actor_keys": archived_keys,
            "failures": failures,
        }

    def archived_actor_keys_for_instance(self, persona_instance_id: str) -> list[str]:
        """Which ARCHIVED actors are bound to this instance — the replay's evidence.

        Read-only, and the reason it exists is idempotence: ``agent retire``
        answers a second call for an already-archived instance with the same ack
        rather than ``not_found`` (plan D11), and "the same ack" has to include
        every ARCHIVED actor bound to this instance — a SUPERSET of what the
        first call archived whenever another lane (``runtime.office.remove``)
        archived one earlier; a replay names the union, never the first call's list. The prune above cannot supply
        them a second time — it archived them, so they are no longer live for it
        to find — and a caller that reconstructed the list from a scene snapshot
        would be re-deriving a store fact from a render. This asks the archive.

        RAISES :class:`ActorsUnreadable` when either directory it walks would not
        fully decode, and that is the C4 correction rather than a new fragility.
        This list is EVIDENCE — the replay's answer to "which desks are off the
        level" — and it was built from ``list_actors``, which drops what it could
        not read and reports the remainder as complete. A short list here is not
        a smaller truth, it is a DIFFERENT claim: a bound desk whose archive copy
        will not decode reads as "not archived by this instance", which is the
        one thing an empty list is supposed to rule out. The caller
        (``agent_retire``) turns the refusal into an ``office_archive_failures``
        row, so the shortfall reaches the operator instead of a silently short
        list of keys.
        """

        target = str(persona_instance_id or "").strip()
        if not target:
            return []
        from .persona_assignments import canonical_persona_instance_id

        canonical = canonical_persona_instance_id(target) or target
        keys: list[str] = []
        for wsid in self.list_workspaces():
            # Both reads gated, and gated on the WIDER one: the archive-inclusive
            # scan covers the live directory and the archive together, so its
            # count is the shortfall of everything this answer depends on. The
            # live scan still supplies the re-added keys to skip, exactly as
            # before — the discrimination stays "which DIRECTORY holds it".
            live_scan = self.scan_actors(wsid)
            scan = self.scan_actors(wsid, include_archived=True)
            if scan.unreadable:
                raise ActorsUnreadable(
                    f"office actors unreadable in {wsid}: {scan.unreadable}"
                )
            live = {actor.actor_key for actor in live_scan.actors}
            for actor in scan.actors:
                if actor.actor_key in live:
                    continue
                if not self._instance_bound_actor(actor, canonical):
                    continue
                keys.append(actor.actor_key)
        return keys

    # --- internal helpers ---------------------------------------------------

    def _read_actor_dir(self, directory) -> ActorScan:
        """One directory of actor files, COUNTING the ones that would not open.

        The skip stays — a whole office must not vanish because one file is
        mid-write or held by an AV scanner — but ``continue`` alone made the skip
        invisible, and an invisible skip is a shortened projection that reports
        itself complete. The count leaves with the rows.

        Logged once per scan, aggregated by exception CLASS: an operator needs to
        know whether they are looking at a share violation or a half-written JSON
        file, and per-file lines would turn a directory of stale files into a log
        flood on every read of the office. Class only, never the message — the
        same disclosure rule the rest of this runtime's receipts follow.
        """

        actors: list[OfficeActor] = []
        if not directory.exists():
            return ActorScan(actors, 0)
        unreadable = 0
        classes: dict[str, int] = {}
        for path in sorted(directory.glob("*.json")):
            try:
                actors.append(from_jsonable(OfficeActor, _read_json(path)))
            except Exception as exc:
                unreadable += 1
                name = type(exc).__name__
                classes[name] = classes.get(name, 0) + 1
        if classes:
            import logging

            logging.getLogger(__name__).warning(
                "office actor files unreadable in %s: %d (%s)",
                directory.name,
                unreadable,
                ", ".join(f"{name} x{count}" for name, count in sorted(classes.items())),
            )
        return ActorScan(actors, unreadable)

    def _archive_actor_locked(
        self,
        surface: OfficeSurface,
        actor: OfficeActor,
        *,
        reason: str,
        updated_by: str,
        emit: bool = True,
        correlation_id: str | None = None,
    ) -> None:
        actor.state = "archived"
        actor.revision += 1
        actor.updated_at = now()
        actor.updated_by = _safe_actor_ref(updated_by)
        atomic_json_write(
            paths.office_archived_actor_path(actor.workspace_id, actor.actor_key),
            to_jsonable(actor),
            indent=2,
            sort_keys=True,
        )
        paths.office_actor_path(actor.workspace_id, actor.actor_key).unlink(missing_ok=True)
        if actor.actor_key not in surface.archived_actor_keys:
            surface.archived_actor_keys = [*surface.archived_actor_keys, actor.actor_key][-ARCHIVED_LEDGER_CAP:]
            surface.updated_at = now()
            _write_surface(surface)
        # The archive half of the lifecycle pair, INSIDE the lock and BEFORE the
        # domain event — the same ordering ``upsert_actor`` uses, for the same
        # monotonicity reason (see ``_emit_actor_patch``).
        #
        # It fires for ``emit=False`` too, and that is correct rather than an
        # oversight: ``resolve_conflict``'s edit-vs-remove branch suppresses the
        # DOMAIN event, but the row really did leave the office and a client that
        # never heard so would render a desk the store no longer has. That batch
        # demotes anyway on its own uncovered ``office.actor.conflict_resolved``,
        # so the patch costs nothing there and is honest everywhere.
        self._emit_actor_remove_patch(actor, correlation_id=correlation_id)
        if emit:
            self._emit(
                "office.actor.removed",
                correlation_id,
                workspace_id=actor.workspace_id,
                actor_key=actor.actor_key,
                reason=reason,
            )

    def _guard_no_conflict(self, workspace_id: str, actor_key: str) -> None:
        if paths.office_conflict_path(workspace_id, actor_key).exists():
            raise SyncConflict(f"actor_conflict:{actor_key}")

    def _guard_archived_actor(
        self,
        workspace_id: str,
        *,
        actor_key: str,
        persona_instance_id: str | None,
        archived_path: Path,
        resurrect: bool,
    ) -> None:
        """THE tombstone fence for ``upsert_actor`` (D1).

        Called only when there is NO live row, because a live row cannot be a
        resurrection whatever the ledger says about it.

        TWO pieces of evidence, either of which is enough. The archive COPY is
        the primary one; the ledger entry is kept beside it because the two can
        legitimately disagree — a realm-sync pull rewrites the surface without
        the archive file, and an archive file can be moved away by hand — and a
        fence that demanded both would be defeated by whichever half went
        missing first. That asymmetry is the whole live incident: the re-add
        cleared both, so by the time the retire replay looked, neither was left
        to prove the delete had ever happened.

        A method rather than an inline block for the reason the class-key fence
        is one: a fence with a NAME can be pinned by the source tests, reported
        on by the doctor, and — the case that forced it — neutralised on its own
        by a test isolating a DIFFERENT fence's claim. An inline block silently
        makes every such test measure two guards at once.
        """

        if resurrect:
            return
        archived_keys: list[str] = []
        try:
            archived_keys = list(self.get_surface(workspace_id).archived_actor_keys)
        except Exception:  # noqa: BLE001 - no surface is no ledger to consult
            archived_keys = []
        if not archived_path.exists() and actor_key not in archived_keys:
            return
        raise ActorArchived(
            f"actor_archived:{actor_key} was deleted on this server; drop the "
            "local row and place a new agent instead of re-adding this key "
            "(`harness office actor-restore`, or --resurrect, re-adds it "
            "deliberately)",
            safe_details={
                "actor_key": actor_key,
                "workspace_id": workspace_id,
                "persona_instance_id": persona_instance_id,
            },
        )

    def _guard_duplicate_desk(self, workspace_id: str, *, actor_key: str, items: list[OfficeItem]) -> None:
        """THE one-desk-per-persona fence for ``upsert_actor`` (D6).

        A fence at the store rather than at its callers, for the reason EG-6.6
        recorded when it hoisted the class-key one out of four writers: a
        caller-side fence is invisible in the store's contract, so the next
        writer ships unfenced with every reply-shape test green. There are
        already four writers reaching ``upsert_actor`` and the incident that
        motivated this one came through the CLI verb, not through the launcher
        the client-side guard protects.

        No ``allow_...`` override, and that asymmetry with
        ``_guard_class_keyed_write`` is deliberate. That fence guards a
        MIGRATION, and an operator can legitimately want the pre-migration shape
        back (``actor-restore``, ``--allow-class-key``). This one guards an
        INVARIANT the render layer depends on — the implicit desk is drawn under
        an agent only while its persona has no authored desk, so a second
        authored desk is not a placement an operator can mean, it is two desks
        one of which will never be reachable. The way past it is to move or
        remove the desk that is already there, which the message names.

        Fires on ``dry_run`` too, for the same reason the class-key fence does:
        a preview whose whole job is to show what the real run would do must
        show the refusal, or the operator learns about it from the write.

        Realm pull is deliberately NOT behind this fence.
        ``office_sync.apply_office_pull`` writes actor files directly and never
        reaches ``upsert_actor``; a workspace pulled from a peer can therefore
        still arrive holding two desks for one persona. That is the correct
        boundary — a pulled duplicate is a conflict-lane fact about what a peer
        published, not a local write this store may refuse — and it is why the
        launcher's render-time ``duplicate_desk`` warning stays: it is the only
        thing that can see data predating or bypassing this fence.
        """

        collision = _duplicate_desk_collision(
            self, workspace_id, actor_key=actor_key, items=items
        )
        if collision is None:
            return
        raise DuplicateDeskRefused(
            _duplicate_desk_message(collision), safe_details=collision
        )

    def _guard_class_keyed_write(self, workspace_id: str, payload: dict[str, Any], *, allow_class_key: bool) -> None:
        """THE class-key fence for ``upsert_actor`` — one fence, at the store.

        Hoisted here from its four callers (Plan EG-6.6). It used to be called
        at ``serve_rpc._runtime_office_upsert``, ``agent_create``'s placement
        leg, ``harness office actor-upsert`` and ``workspace_template``'s copy —
        four copies of one decision around one store, which is why
        ``scripts/office_actor_rekey_to_instance.py`` had to WARN that a new
        writer reaching ``upsert_actor`` was unfenced by default. It was: the
        fifth caller would have shipped with the hole and every reply-shape test
        green, because a caller-side fence is invisible in the store's own
        contract.

        What the callers keep is a TRANSLATION of the refusal below into their
        transport's taxonomy (a 4090 with ``data.reason``, a stage-42
        ``duplicate_conflict`` exit, a copy warning, a compensated create
        failure) — never a second copy of the decision. The predicate itself
        still lives in ``office_class_key_guard``, one derivation authority.

        The refusal MESSAGE is the shared ``refusal_message`` verbatim, because
        three of those translations assert on it and because the operator-facing
        exits it names (send the binding, ``actor-restore``,
        ``--allow-class-key``) are the same three from every lane that has them.

        Fires on ``dry_run`` too. A preview whose whole job is to show what the
        real run would do must show the refusal, or the operator learns about it
        only from the write.
        """

        if allow_class_key:
            return
        from .office_class_key_guard import (
            ClassKeyedPlacementRefused,
            class_key_collision,
            refusal_message,
        )

        collision = class_key_collision(self, workspace_id, payload)
        if collision is None:
            return
        raise ClassKeyedPlacementRefused(refusal_message(collision), safe_details=collision)

    def _guard_class_keyed_adoption(self, workspace_id: str, actor: OfficeActor, *, allow_class_key: bool) -> None:
        """The class-key fence for ``resolve_conflict(take="remote")``.

        That branch writes a PEER's actor with ``_write_actor`` DIRECTLY,
        bypassing ``upsert_actor`` and therefore its fence. So it can put an
        archived CLASS key back on disk as ACTIVE beside its instance-keyed
        sibling — the same double placement the re-key migration exists to
        remove, reached through the one door the fence did not cover.

        The SECOND of the store's two class-key fences, and it stays separate
        from ``_guard_class_keyed_write`` rather than merging into it: this one's
        input is a deserialized peer RECORD (whose ``actor_key`` is authoritative
        and whose spelling never met ``_normalize_persona_id``), not a caller's
        payload, and its refusal names a different exit (``--take local``). One
        predicate, two typed entrances — which is the shape that let this method
        be fenced at all, since its payload never passes ``upsert_actor``.

        Refuses, rather than silently re-keying the incoming actor onto the
        instance binding: that would rewrite what the peer published into a
        different identity (the next push sends back an actor the peer never
        had), and in the duplicate-item case it would land ON TOP of the
        migrated instance-keyed actor — trading a visible double placement for a
        silent clobber. ``allow_class_key`` is the operator's way through.

        Normalizes BOTH sides of the class-key test. This is the one path whose
        input never met ``_normalize_persona_id``, so a peer's ``Backend_Dev``
        arrives verbatim; a raw comparison would read it as instance-keyed and
        wave the write through.
        """

        if allow_class_key:
            return
        from .office_class_key_guard import (
            ClassKeyedPlacementRefused,
            class_key_collision,
            refusal_message,
        )

        persona_id = _normalize_persona_id(actor.persona_id)
        if not persona_id or _normalize_persona_id(actor.actor_key) != persona_id:
            # Instance-keyed adoption: it IS the migration's shape, never undoes it.
            return
        collision = class_key_collision(
            self,
            workspace_id,
            # Deliberately class-keyed and deliberately UN-normalized: the guard
            # owns normalization (one derivation authority), and the key that
            # would actually be written is the class key regardless of what
            # ``persona_instance_id`` the peer's record happens to carry.
            {
                "persona_id": actor.persona_id,
                "items": [{"item_id": item.item_id} for item in actor.items],
            },
        )
        if collision is None:
            return
        # The shared ``refusal_message`` is left untouched (the upsert fence
        # raises it verbatim and three lanes assert on it); the
        # resolve-specific exit gets appended, because "--take local" is
        # the answer an operator under conflict pressure actually needs and the
        # shared message cannot know to offer it.
        raise ClassKeyedPlacementRefused(
            refusal_message(collision) + " Resolve with --take local to keep the migrated state.",
            safe_details={**collision, "take": "remote"},
        )


# --- module-level file helpers ---------------------------------------------


def _read_json(path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def _write_surface(surface: OfficeSurface) -> None:
    atomic_json_write(paths.office_surface_path(surface.workspace_id), to_jsonable(surface), indent=2, sort_keys=True)


def _write_actor(actor: OfficeActor) -> None:
    atomic_json_write(paths.office_actor_path(actor.workspace_id, actor.actor_key), to_jsonable(actor), indent=2, sort_keys=True)


def _archive_conflict_sidecar(workspace_id: str, actor_key: str) -> None:
    sidecar_path = paths.office_conflict_path(workspace_id, actor_key)
    if not sidecar_path.exists():
        return
    try:
        payload = _read_json(sidecar_path)
    except Exception:
        payload = {"actor_key": actor_key}
    payload["resolved_at"] = to_jsonable(now())
    from .office_models import actor_file_token

    dest = paths.office_conflicts_dir(workspace_id) / f"{actor_file_token(actor_key)}.resolved.json"
    atomic_json_write(dest, payload, indent=2, sort_keys=True)
    sidecar_path.unlink(missing_ok=True)


def _free_surface_archive_dir(workspace_id: str):
    """First unused archive slot for ``workspace_id``.

    Deterministic rather than timestamped so a test can name the destination,
    and suffixed rather than refusing so an operator who archives a re-created
    orphan a second time is not stuck with a conflict they cannot resolve
    without hand-moving files — which is the thing this verb exists to avoid.
    """

    base = paths.office_archived_surface_dir(workspace_id)
    if not base.exists():
        return base
    for attempt in range(2, 1000):
        candidate = base.with_name(f"{base.name}-{attempt}")
        if not candidate.exists():
            return candidate
    raise AlreadyExists(f"office_archive:{workspace_id}")


def _check_revision(current: int | None, expected: int | None) -> None:
    if expected is None:
        return
    if current is None or int(current) != int(expected):
        raise StaleRevision(f"stale_revision: expected {expected}, have {current}")
