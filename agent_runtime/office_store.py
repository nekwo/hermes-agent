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

from typing import Any

from hermes_time import now
from utils import atomic_json_write

from . import office_models, paths
from .errors import AlreadyExists, NotFound, StaleRevision, SyncConflict
from .events import EventLog
from .locks import office_lock
from .models import Event, OfficeActor, OfficeItem, OfficeSurface
# Eager, not lazy: the rule now lives in stdlib-only ``agent_runtime.redaction``,
# so importing it costs nothing. The old lazy ``from .realm_sync import …`` was
# dodging ``realm_sync``'s weight — the tell that ``realm_sync`` was the wrong
# home for a constant four other modules needed.
from .redaction import SECRET_ASSIGNMENT_RE
from .serde import from_jsonable, to_jsonable

ARCHIVED_LEDGER_CAP = 5000
MAX_ITEMS_PER_ACTOR = 32
MAX_FOLDERS = 64


def _safe_id(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    cleaned = "".join(ch if ch.isalnum() or ch in "_.:-" else "_" for ch in text)
    return cleaned.strip("._:-")[:120] or None


def _safe_actor_ref(value: Any, *, fallback: str = "operator") -> str:
    return _safe_id(value) or fallback


def _normalize_persona_id(value: Any) -> str | None:
    # Mirrors the launcher's OfficeAgentIdentity normalization: trim + lower.
    text = str(value or "").strip().lower()
    return _safe_id(text)


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
    item_id = _safe_id(raw.get("item_id"))
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
        pet_slug=_safe_id(raw.get("pet_slug")),
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


class OfficeStore:
    def __init__(self, event_log: EventLog | None = None) -> None:
        self.event_log = event_log or EventLog()

    # --- event emission (single chokepoint) ------------------------------

    def _emit(self, event_type: str, **payload: Any) -> None:
        try:
            body = {key: value for key, value in payload.items() if value is not None}
            self.event_log.append(Event(now(), event_type, None, None, None, body))
        except Exception:
            import logging

            logging.getLogger(__name__).warning("office event append failed: %s", event_type, exc_info=True)

    def _emit_actor_patch(self, actor: OfficeActor, *, created: bool) -> None:
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
                emit_office_actor_refresh(self.event_log, actor.workspace_id, actor.actor_key)
            else:
                emit_office_actor_patch(self.event_log, actor, created=created)
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

    def _emit_surface_patch(self, surface: OfficeSurface) -> None:
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

            emit_office_surface_patch(self.event_log, surface)
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

    def _emit_actor_remove_patch(self, actor: OfficeActor) -> None:
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

            emit_office_actor_remove(self.event_log, actor.workspace_id, actor.actor_key)
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
        content (fixed folders + timestamp-excluded content hash)."""

        wsid = _safe_id(workspace_id)
        if not wsid:
            raise ValueError("invalid_request")
        if self.surface_exists(wsid):
            return self.get_surface(wsid)
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
        dry_run: bool = False,
    ) -> OfficeSurface:
        wsid = _safe_id(workspace_id)
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
                self._emit_surface_patch(surface)
            self._emit(
                "office.surface.updated",
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

    def list_actors(self, workspace_id: str, *, include_archived: bool = False) -> list[OfficeActor]:
        actors = self._read_actor_dir(paths.office_actors_dir(workspace_id))
        if include_archived:
            actors = [*actors, *self._read_actor_dir(paths.office_archive_dir(workspace_id))]
        return sorted(actors, key=lambda a: a.actor_key)

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
        dry_run: bool = False,
    ) -> OfficeActor:
        wsid = _safe_id(workspace_id)
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
            self._guard_no_conflict(wsid, actor_key)
            existing: OfficeActor | None = None
            if self.actor_exists(wsid, actor_key):
                existing = self.get_actor(wsid, actor_key)
            archived_path = paths.office_archived_actor_path(wsid, actor_key)
            archived: OfficeActor | None = None
            if existing is None and archived_path.exists():
                try:
                    archived = from_jsonable(OfficeActor, _read_json(archived_path))
                except Exception:
                    archived = None
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
            self._emit_actor_patch(actor, created=existing is None)
            self._emit(
                "office.actor.upserted",
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
        dry_run: bool = False,
    ) -> OfficeActor:
        wsid = _safe_id(workspace_id)
        if not wsid:
            raise ValueError("invalid_request")
        with office_lock(wsid):
            if not self.actor_exists(wsid, actor_key):
                # Idempotent: already archived → return the archived copy.
                archived_path = paths.office_archived_actor_path(wsid, actor_key)
                if archived_path.exists():
                    return from_jsonable(OfficeActor, _read_json(archived_path))
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
            self._archive_actor_locked(surface, actor, reason=reason, updated_by=updated_by)
        return from_jsonable(OfficeActor, _read_json(paths.office_archived_actor_path(wsid, actor_key)))

    def restore_actor(
        self, workspace_id: str, actor_key: str, *, updated_by: str = "operator", dry_run: bool = False
    ) -> OfficeActor:
        wsid = _safe_id(workspace_id)
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
            self._emit("office.actor.restored", workspace_id=wsid, actor_key=actor_key)
        return self.get_actor(wsid, actor_key)

    def resolve_conflict(
        self,
        workspace_id: str,
        actor_key: str,
        *,
        take: str,
        updated_by: str = "operator",
        allow_class_key: bool = False,
        dry_run: bool = False,
    ) -> OfficeActor | None:
        """Resolve a realm-sync conflict sidecar for an actor. ``take=local``
        keeps the local actor; ``take=remote`` adopts the sidecar's remote copy
        (or archives the local actor for an edit-vs-remove tombstone). Always
        archives the sidecar and emits ``office.actor.conflict_resolved``.

        ``allow_class_key`` is the operator's on-the-record override for the
        class-key fence below (``harness office resolve-conflict
        --allow-class-key``); see ``_guard_class_keyed_adoption``."""

        take = str(take or "").strip().lower()
        if take not in {"local", "remote"}:
            raise ValueError("invalid_request")
        wsid = _safe_id(workspace_id)
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
                        self._archive_actor_locked(surface, actor, reason="remote_removed", updated_by=updated_by, emit=False)
            if dry_run:
                # take value + sidecar existence validated; return the would-be
                # resolved actor in memory. Leave the sidecar in place and emit
                # nothing (matches the real return, incl. None for edit-vs-remove).
                return result_actor
            _archive_conflict_sidecar(wsid, actor_key)
            self._emit(
                "office.actor.conflict_resolved",
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

        wsid = _safe_id(workspace_id)
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
        """

        wsid = _safe_id(workspace_id)
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

    def archive_actors_for_instance(self, persona_instance_id: str, *, reason: str = "instance_reaped") -> int:
        """Hermes prune-lane hook: archive every active placement bound to a
        reaped persona instance so no phantom desk file re-materializes the
        agent (NEVER a launcher-side filter — the orphan-tombstone precedent).
        Persona-id-keyed placements survive instance churn by design.
        """

        target = str(persona_instance_id or "").strip()
        if not target:
            return 0
        from .persona_assignments import canonical_persona_instance_id

        canonical = canonical_persona_instance_id(target) or target
        archived = 0
        for wsid in self.list_workspaces():
            for actor in self.list_actors(wsid):
                bound = actor.persona_instance_id
                if not bound:
                    continue
                if (canonical_persona_instance_id(bound, persona_id=actor.persona_id) or bound) != canonical:
                    continue
                try:
                    self.remove_actor(wsid, actor.actor_key, reason=reason, updated_by="harness")
                    archived += 1
                except Exception:
                    continue
        return archived

    # --- internal helpers ---------------------------------------------------

    def _read_actor_dir(self, directory) -> list[OfficeActor]:
        actors: list[OfficeActor] = []
        if not directory.exists():
            return actors
        for path in directory.glob("*.json"):
            try:
                actors.append(from_jsonable(OfficeActor, _read_json(path)))
            except Exception:
                continue
        return actors

    def _archive_actor_locked(self, surface: OfficeSurface, actor: OfficeActor, *, reason: str, updated_by: str, emit: bool = True) -> None:
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
        self._emit_actor_remove_patch(actor)
        if emit:
            self._emit(
                "office.actor.removed",
                workspace_id=actor.workspace_id,
                actor_key=actor.actor_key,
                reason=reason,
            )

    def _guard_no_conflict(self, workspace_id: str, actor_key: str) -> None:
        if paths.office_conflict_path(workspace_id, actor_key).exists():
            raise SyncConflict(f"actor_conflict:{actor_key}")

    def _guard_class_keyed_adoption(self, workspace_id: str, actor: OfficeActor, *, allow_class_key: bool) -> None:
        """The class-key fence for ``resolve_conflict(take="remote")``.

        That branch writes a PEER's actor with ``_write_actor`` DIRECTLY,
        bypassing ``upsert_actor`` and therefore every class-key guard the other
        writers call. So it can put an archived CLASS key back on disk as
        ACTIVE beside its instance-keyed sibling — the same double placement the
        re-key migration exists to remove, reached through the one door the fence
        did not cover.

        The fence lives in the store rather than at the CLI (where
        ``actor-upsert``'s does) because the hazard is intrinsic to THIS METHOD,
        not to any one caller: the payload is peer-authored and never passes
        ``upsert_actor``'s chokepoint, so a future second caller would reopen the
        hole by default. ``upsert_actor``'s guard is at its callers for the
        opposite reason — its payload IS the caller's intent.

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
        # ``refusal_message`` is left untouched (three other writers assert on
        # it); the resolve-specific exit gets appended, because "--take local" is
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
