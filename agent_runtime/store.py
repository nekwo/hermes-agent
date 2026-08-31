from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from hermes_time import now
from utils import atomic_json_write

from . import paths
from .errors import AlreadyExists, NotFound, SkillTombstoneRefused, WorkspaceDeleteBlocked
from .events import EventLog
from .models import AgentPersona, AgentRun, Event, Incident, Realm, SkillTombstone, Workspace
from .serde import from_jsonable, safe_id, to_jsonable
from .states import RunState

T = TypeVar("T")

ACTIVE_RUN_STATES = frozenset({RunState.QUEUED, RunState.STARTING, RunState.RUNNING, RunState.WAITING_ON_TOOL, RunState.WAITING_ON_APPROVAL})
# Bound on Realm.deleted_workspace_ids — the workspace-delete resurrection
# guard. Oldest entries fall off first; by then every member has long since
# pulled the tombstone (the bounded-ledger idiom shared with the board/office
# archived ledgers).
DELETED_WORKSPACE_LEDGER_CAP = 500
# Bound on Realm.skill_tombstones — the same guard for shared skills. Smaller
# than the workspace cap because the shared catalog is small; the eviction
# argument is identical (by the time an entry falls off, every member has long
# since pulled it).
SKILL_TOMBSTONE_LEDGER_CAP = 200


def _safe_display_name(value) -> str:
    return " ".join(str(value or "").split())[:160]


def _slugify(value) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return slug[:80] or "item"


def _dedupe_ids(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        clean = safe_id(value)
        if clean and clean not in result:
            result.append(clean)
    return result


def _read_json(path: Path) -> dict:
    if not path.exists():
        raise NotFound(str(path))
    return json.loads(path.read_text(encoding="utf-8"))


def _read_model(cls: type[T], path: Path) -> T:
    return from_jsonable(cls, _read_json(path))


def _write_model(path: Path, model) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, to_jsonable(model), indent=2, sort_keys=True)


def _append_store_event(event_log: EventLog, event_type: str, **payload) -> None:
    """Advance the EventLog watermark after a store mutation (Stage 12).

    The stream/read-model pipeline is watermark-gated: a store write with no
    event is invisible to every consumer (launcher snapshot, serve read model)
    until an unrelated event advances the offset. Emission lives HERE, at the
    store chokepoint, so programmatic callers are covered — not just CLI verbs.
    Payload values of None are dropped. Best effort: a broken event log must
    not fail the write, but the failure is logged, never silent.
    """
    try:
        body = {key: value for key, value in payload.items() if value is not None}
        event_log.append(Event(now(), event_type, None, None, None, body))
    except Exception:
        logging.getLogger(__name__).warning(
            "store event append failed: %s", event_type, exc_info=True
        )


def _parse_intent_basis(value):
    """Parse an ISO-8601 UTC intent basis into a datetime; None when absent or
    unparseable (fail-open — a malformed basis must never block a scope
    switch, it just loses supersede protection for that one write)."""
    if not value:
        return None
    try:
        from datetime import datetime

        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _resolve_activation_write(pointer_path: Path, key: str, value: str | None, issued_at: str | None) -> tuple[str, str | None, str]:
    """Compare-and-set decision for an active-pointer write.

    Mutation intents carry the wall-clock instant the operator issued them
    (``issued_at``). Transport can deliver an intent twice (serve timeout →
    CLI fallback re-runs the same argv) or late (a wedged serve child drains
    an abandoned request minutes later) — the intent's basis, not its arrival
    order, decides who wins. Returns ``(decision, current_value, basis)``:

    - ``apply``      — write the pointer and emit the activation event.
    - ``superseded`` — a strictly newer intent already owns the pointer; do
      not write, do not emit.
    - ``duplicate``  — the exact same intent (same basis, same target) was
      already applied; do not write, do not emit (exact-once event feed).

    A caller with no basis (human at a terminal, legacy callers) is stamped
    ``now()`` so the basis timeline always advances and manual actions win.
    """
    basis = issued_at or now()
    try:
        current = _read_json(pointer_path)
    except Exception:
        return "apply", None, basis
    current_value = safe_id(current.get(key))
    incoming = _parse_intent_basis(basis)
    stored = _parse_intent_basis(current.get("intent_issued_at"))
    if incoming is None or stored is None:
        return "apply", current_value, basis
    if incoming < stored:
        return "superseded", current_value, basis
    if incoming == stored and value == current_value:
        return "duplicate", current_value, basis
    return "apply", current_value, basis


def _list_models(cls: type[T], directory: Path) -> list[T]:
    if not directory.exists():
        return []
    items: list[T] = []
    for path in directory.glob("*.json"):
        try:
            items.append(_read_model(cls, path))
        except NotFound:
            # Archive moves are evidence-preserving but not invisible to UI polls:
            # a file can disappear after glob() and before read_text().
            continue
    return sorted(items, key=lambda item: item.id)


from .task_store_stub import TaskStoreStub as TaskStore


class AgentStore:
    def __init__(self, event_log: EventLog | None = None):
        self.event_log = event_log or EventLog()

    def save(self, persona: AgentPersona) -> AgentPersona:
        _write_model(paths.agent_path(persona.id), persona)
        _append_store_event(
            self.event_log,
            "persona.updated",
            persona_id=persona.id,
            display_name=persona.display_name,
        )
        return persona

    def get(self, persona_id: str) -> AgentPersona:
        return _read_model(AgentPersona, paths.agent_path(persona_id))

    def list_all(self) -> list[AgentPersona]:
        return _list_models(AgentPersona, paths.agents_dir())


class WorkspaceStore:
    def __init__(self, event_log: EventLog | None = None):
        self.event_log = event_log or EventLog()

    def create(
        self,
        *,
        name: str,
        agent_ids: list[str] | None = None,
        default_blueprint_id: str | None = None,
        isolation: str = "soft",
        max_concurrent_lanes: int | None = None,
        realm_id: str | None = None,
        workspace_id: str | None = None,
    ) -> Workspace:
        clean_name = _safe_display_name(name)
        if not clean_name:
            raise ValueError("workspace name is required")
        clean_isolation = str(isolation or "soft").strip().lower()
        if clean_isolation not in {"soft", "hard"}:
            raise ValueError("invalid_isolation")
        ts = now()
        slug = _slugify(clean_name)
        item = Workspace(
            id=safe_id(workspace_id) or f"ws_{slug}_{uuid.uuid4().hex[:6]}",
            slug=slug,
            name=clean_name,
            agent_ids=_dedupe_ids(agent_ids or []),
            default_blueprint_id=safe_id(default_blueprint_id),
            isolation=clean_isolation,
            max_concurrent_lanes=max_concurrent_lanes if max_concurrent_lanes is None else max(1, int(max_concurrent_lanes)),
            realm_id=safe_id(realm_id),
            created_at=ts,
            updated_at=ts,
        )
        path = paths.workspace_path(item.id)
        if path.exists():
            raise AlreadyExists(item.id)
        _write_model(path, item)
        _append_store_event(
            self.event_log,
            "workspace.created",
            workspace_id=item.id,
            name=item.name,
            realm_id=item.realm_id,
        )
        return self.get(item.id)

    def get(self, workspace_id: str) -> Workspace:
        return _read_model(Workspace, paths.workspace_path(workspace_id))

    def list_all(self, *, include_archived: bool = False) -> list[Workspace]:
        items = _list_models(Workspace, paths.workspaces_dir())
        if not include_archived:
            items = [item for item in items if not item.archived]
        return sorted(items, key=lambda item: (item.name.lower(), item.id))

    def save(self, item: Workspace, *, emit_event: bool = True) -> Workspace:
        """``emit_event=False`` is for named mutators (rename/archive/…) that
        append their own, more specific event — never for skipping emission."""
        item.updated_at = now()
        _write_model(paths.workspace_path(item.id), item)
        if emit_event:
            _append_store_event(
                self.event_log, "workspace.updated", workspace_id=item.id, change="saved", name=item.name
            )
        return self.get(item.id)

    def set_active(self, workspace_id: str | None, *, issued_at: str | None = None) -> dict:
        value = safe_id(workspace_id)
        name = self.get(value).name if value else None
        decision, current_value, basis = _resolve_activation_write(
            paths.active_workspace_path(), "workspace_id", value, issued_at
        )
        if decision != "apply":
            return {"workspace_id": current_value, "applied": False, "reason": decision, "requested_workspace_id": value}
        _write_model(
            paths.active_workspace_path(),
            {"workspace_id": value, "updated_at": now(), "intent_issued_at": basis},
        )
        if value:
            _append_store_event(self.event_log, "workspace.activated", workspace_id=value, name=name)
        else:
            _append_store_event(self.event_log, "workspace.activated", cleared=True)
        return {"workspace_id": value, "applied": True}

    def active_id(self) -> str | None:
        try:
            raw = _read_json(paths.active_workspace_path())
        except Exception:
            return None
        return safe_id(raw.get("workspace_id"))

    def add_agent(self, workspace_id: str, persona_id: str) -> Workspace:
        item = self.get(workspace_id)
        persona = safe_id(persona_id)
        if persona and persona not in item.agent_ids:
            item.agent_ids.append(persona)
        item = self.save(item, emit_event=False)
        _append_store_event(
            self.event_log, "workspace.updated", workspace_id=item.id, change="agent_added", persona_id=persona
        )
        return item

    def remove_agent(self, workspace_id: str, persona_id: str) -> Workspace:
        item = self.get(workspace_id)
        persona = safe_id(persona_id)
        item.agent_ids = [value for value in item.agent_ids if value != persona]
        item = self.save(item, emit_event=False)
        _append_store_event(
            self.event_log, "workspace.updated", workspace_id=item.id, change="agent_removed", persona_id=persona
        )
        return item

    def rename(self, workspace_id: str, name: str) -> Workspace:
        item = self.get(workspace_id)
        item.name = _safe_display_name(name)
        item.slug = _slugify(item.name)
        item = self.save(item, emit_event=False)
        _append_store_event(
            self.event_log, "workspace.updated", workspace_id=item.id, change="renamed", name=item.name
        )
        return item

    def archive(self, workspace_id: str) -> Workspace:
        item = self.get(workspace_id)
        item.archived = True
        item = self.save(item, emit_event=False)
        _append_store_event(self.event_log, "workspace.archived", workspace_id=item.id, name=item.name)
        return item

    def delete(self, workspace_id: str, *, reason: str = "operator_delete") -> dict:
        """Hard-delete a workspace and cascade its scoped content stores.

        The single write chokepoint for workspace deletion (archive stays the
        reversible path). Guards, in order:

        - ``realm_default_workspace`` — a SERVER-bound realm's default pointer
          is backend-adoption authority; promote another default first. A
          local realm's default pointer is local truth and is cleared here.

        Cascade: the workspace JSON, its Mission Office subtree, and every
        board owned by the workspace. A realm-bound delete also rewrites realm
        membership and records the id in the realm's ``deleted_workspace_ids``
        resurrection-guard ledger, so realm sync propagates the removal
        instead of letting another member's surviving copy republish it.
        Emits ``workspace.deleted`` (Stage 12: the mutation must ride its own
        event or stay invisible to the watermark-gated consumers).
        """
        item = self.get(workspace_id)
        realm: Realm | None = None
        if item.realm_id:
            try:
                realm = RealmStore(event_log=self.event_log).get(item.realm_id)
            except NotFound:
                realm = None
        if realm is not None and realm.server_id and realm.default_workspace_id == item.id:
            raise WorkspaceDeleteBlocked(
                "realm_default_workspace",
                "This workspace is the realm's default; promote another default workspace first.",
                safe_details={"realm_id": realm.id},
            )

        # Cascade content stores under their own write locks so a concurrent
        # office/board write cannot interleave with the removal.
        from .board_store import BoardStore
        from .locks import board_lock, office_lock

        # THE CASCADE'S ENUMERATION IS ITS DELETE LIST, so it is refused whole
        # rather than run short. ``list_all`` drops a board whose ``board.json``
        # will not decode, and the loop below deletes by MATCHING
        # ``board.workspace_id`` — a board it cannot decode is a board it cannot
        # attribute, so it silently survives a workspace that no longer exists.
        # That is worse than either outcome the operator chose between: an orphan
        # directory owned by a deleted workspace, which no workspace-scoped verb
        # will list again and no later delete can re-reach, because the row that
        # named it is gone.
        #
        # Refused BEFORE the office subtree is removed — before the cascade takes
        # its first irreversible step — so the store is left exactly as found. A
        # half-done cascade is worse than a refused one: the refusal is repairable
        # by fixing one file; the half-done state is not repairable at all once
        # the workspace row is unlinked.
        board_scan = BoardStore(event_log=self.event_log).scan_all()
        if board_scan.unreadable:
            raise WorkspaceDeleteBlocked(
                "workspace_boards_unreadable",
                (
                    f"{board_scan.unreadable} board definition(s) will not decode, so the "
                    "cascade cannot tell which boards this workspace owns; repair or "
                    "remove them before deleting the workspace."
                ),
                safe_details={"unreadable": board_scan.unreadable, "workspace_id": item.id},
            )

        with office_lock(item.id):
            shutil.rmtree(paths.office_dir(item.id), ignore_errors=True)
        for board in board_scan.boards:
            if board.workspace_id != item.id:
                continue
            with board_lock(board.board_id):
                shutil.rmtree(paths.board_dir(board.board_id), ignore_errors=True)

        if realm is not None:
            realm.workspace_ids = [wid for wid in (realm.workspace_ids or []) if wid != item.id]
            ledger = [wid for wid in (realm.deleted_workspace_ids or []) if wid != item.id]
            ledger.append(item.id)
            realm.deleted_workspace_ids = ledger[-DELETED_WORKSPACE_LEDGER_CAP:]
            if realm.default_workspace_id == item.id:
                # Only reachable for local realms — the server-bound case is
                # guarded above.
                realm.default_workspace_id = None
            RealmStore(event_log=self.event_log).save(realm, emit_event=False)
            _append_store_event(
                self.event_log, "realm.updated", realm_id=realm.id, change="workspace_deleted"
            )

        paths.workspace_path(item.id).unlink(missing_ok=True)
        if self.active_id() == item.id:
            # Clear the dangling pointer; verb-layer callers may re-reconcile
            # to the realm's default afterwards.
            self.set_active(None)
        _append_store_event(
            self.event_log,
            "workspace.deleted",
            workspace_id=item.id,
            name=item.name,
            realm_id=item.realm_id,
            reason=reason,
        )
        return {
            "id": item.id,
            "name": item.name,
            "realm_id": item.realm_id,
            "deleted": True,
        }


def _normalize_skill_selection(selection: list[str] | None) -> list[str]:
    """Validate (shape only), dedupe, and sort skill selection slugs.

    Shape rules (per REALM_SKILL_SELECTION_DESIGN §2): non-empty, no leading
    dot, no path separator, and identical to their ``safe_path_token`` form — the
    same tokenizer the realm publisher uses for skill directory names, so a
    valid slug round-trips to its published path. Every malformed slug is
    collected and reported in ONE ``ValueError`` (mapped to a typed
    ``invalid_request`` at the CLI seam) so a batch save names all offenders
    instead of failing on the first. Slugs unknown to the local catalog are
    NOT filtered here — that is realm truth another member may own.
    """
    from agent_runtime.paths import safe_path_token

    cleaned: set[str] = set()
    rejected: list[str] = []
    for raw in selection or []:
        slug = str(raw).strip()
        if (
            not slug
            or slug.startswith(".")
            or "/" in slug
            or "\\" in slug
            or slug != safe_path_token(slug)
        ):
            rejected.append(slug or repr(raw))
            continue
        cleaned.add(slug)
    if rejected:
        raise ValueError(
            "malformed skill selection slug(s): " + ", ".join(repr(slug) for slug in sorted(set(rejected)))
        )
    return sorted(cleaned)


def skill_tombstone_matches(entry_slug: str, slug: str) -> bool:
    """Does ledger entry ``entry_slug`` block the package published as ``slug``?

    Mirrors ``realm_sync._skill_slug_selected`` exactly: the slug itself, or —
    for a categorized ``<cat>/<child>`` slug — its bare child name. The
    selection and the tombstone MUST agree about what a name means, or a slug
    could be simultaneously "selected" and "not the thing that was deleted".

    Public because the operator delete verb (``hermes harness skills delete``)
    asks the rule while holding a CANDIDATE entry slug and no realm yet — "which
    canonical packages would a tombstone on this name cover, and which realms
    currently publish one of them" — a question :func:`skill_tombstoned` cannot
    be asked, since there is no ledger to read. Two entry points, ONE rule; a
    second spelling in the CLI is precisely what §2.3 forbids.

    Because the rule is deliberately identical to the SELECTION rule, the same
    function answers "is this package selected" as
    ``any(skill_tombstone_matches(entry, slug) for entry in selection)``.
    """

    if entry_slug == slug:
        return True
    if "/" in slug:
        return entry_slug == slug.split("/", 1)[1]
    return False


#: Historical private name, kept for this module's own call sites (the
#: ``validate_skill_slug`` / ``_validate_slug`` idiom in ``skill_promotion``).
_tombstone_blocks = skill_tombstone_matches


def active_skill_tombstones(realm: Realm) -> list[SkillTombstone]:
    """The ledger entries that currently BLOCK — the ONE spelling of "active".

    Since RD-11 the ledger is a per-slug state register: a restored entry stays
    on it (carrying ``restored_at``) so the union merge can see the restore, but
    it blocks nothing. Every reader that used to mean "is there an entry for
    this slug" — the match rule below, the receipt rows, the two CLI verbs'
    before-the-write questions — asks through here instead, so "active" cannot
    acquire a second, drifting definition the way an open-coded
    ``entry.slug == slug`` scan would.
    """

    return [
        entry
        for entry in (getattr(realm, "skill_tombstones", None) or [])
        if getattr(entry, "restored_at", None) is None
    ]


def prune_settled_ledger(items: list[T], *, cap: int, settled: Callable[[Any], bool]) -> list[T]:
    """Bound a per-key state register, dropping SETTLED history first.

    ``items`` is oldest-first (the append order every ledger chokepoint keeps).
    A plain ``items[-cap:]`` would let restored — i.e. inert — entries push
    LIVE blocks off the front, which is the one eviction this ledger must never
    make: an evicted block is a resurrected skill. So settled entries are
    dropped oldest-first until the list fits, and only then does the ordinary
    oldest-first bound apply.

    Shape-agnostic (``settled`` is supplied by the caller) because the same rule
    runs twice: over :class:`SkillTombstone` records at the store chokepoints,
    and over raw JSON rows in ``realm_sync``'s pull-time union merge, which must
    stay tolerant of a peer's unparseable stamps. One rule, two shapes.
    """

    if cap <= 0 or len(items) <= cap:
        return list(items)
    over = len(items) - cap
    dropped: set[int] = set()
    for index, item in enumerate(items):
        if len(dropped) >= over:
            break
        if settled(item):
            dropped.add(index)
    kept = [item for index, item in enumerate(items) if index not in dropped]
    return kept[-cap:]


def _prune_skill_tombstones(entries: list[SkillTombstone]) -> list[SkillTombstone]:
    """The record-shaped half of :func:`prune_settled_ledger`."""

    return prune_settled_ledger(
        entries,
        cap=SKILL_TOMBSTONE_LEDGER_CAP,
        settled=lambda entry: getattr(entry, "restored_at", None) is not None,
    )


def skill_tombstoned(realm: Realm, slug: str) -> SkillTombstone | None:
    """The ONE spelling of "is this skill deleted in this realm".

    Every enforcement point (the pull's inbox mirror and canonical archive, the
    publish artifact filter, the operator surfaces) asks through here, so a
    second, drifting copy of the match rule cannot exist. Returns the blocking
    entry (evidence for the refusal message) or ``None`` — a RESTORED entry is
    not a blocking one, so the scan runs over the active ledger.
    """

    clean = str(slug or "").strip()
    if not clean:
        return None
    for entry in active_skill_tombstones(realm):
        if _tombstone_blocks(entry.slug, clean):
            return entry
    return None


def _normalize_agent_selection(selection: list[str] | None) -> list[str]:
    """Validate, dedupe, and sort Realm persona-definition ids.

    Persona ids use the store's canonical model-id grammar (including ``:``
    for profile-backed personas). Unknown ids are deliberately preserved: a
    different Realm member may own the definition locally, so filtering an
    unrelated save through this machine's catalog would corrupt Realm truth.
    Every malformed id is reported together and no partial write occurs.
    """
    cleaned: set[str] = set()
    rejected: list[str] = []
    for raw in selection or []:
        value = str(raw).strip()
        normalized = safe_id(value)
        if not value or normalized is None or value != normalized:
            rejected.append(value or repr(raw))
            continue
        cleaned.add(value)
    if rejected:
        raise ValueError(
            "malformed agent selection id(s): "
            + ", ".join(repr(value) for value in sorted(set(rejected)))
        )
    return sorted(cleaned)


class RealmStore:
    def __init__(self, event_log: EventLog | None = None):
        self.event_log = event_log or EventLog()

    def create(
        self,
        *,
        name: str,
        server_id: str | None = None,
        realm_id: str | None = None,
        default_workspace_id: str | None = None,
        default_workspace_name: str = "Default",
        default_workspace_version: int = 0,
    ) -> Realm:
        clean_name = _safe_display_name(name)
        if not clean_name:
            raise ValueError("realm name is required")
        ts = now()
        slug = _slugify(clean_name)
        item = Realm(
            id=safe_id(realm_id) or f"realm_{slug}_{uuid.uuid4().hex[:6]}",
            slug=slug,
            name=clean_name,
            server_id=safe_id(server_id),
            default_workspace_id=safe_id(default_workspace_id),
            default_workspace_name=_safe_display_name(default_workspace_name) or "Default",
            default_workspace_version=max(0, int(default_workspace_version)),
            created_at=ts,
            updated_at=ts,
        )
        path = paths.realm_path(item.id)
        if path.exists():
            raise AlreadyExists(item.id)
        _write_model(path, item)
        _append_store_event(
            self.event_log, "realm.created", realm_id=item.id, name=item.name, server_id=item.server_id
        )
        return self.get(item.id)

    def get(self, realm_id: str) -> Realm:
        return _read_model(Realm, paths.realm_path(realm_id))

    def list_all(self, *, include_archived: bool = False) -> list[Realm]:
        items = _list_models(Realm, paths.realms_dir())
        if not include_archived:
            items = [item for item in items if not item.archived]
        return sorted(items, key=lambda item: (item.name.lower(), item.id))

    def save(self, item: Realm, *, emit_event: bool = True) -> Realm:
        """``emit_event=False`` is for callers that append their own, more
        specific event in the same mutation (bind_server, realm adopt)."""
        item.updated_at = now()
        _write_model(paths.realm_path(item.id), item)
        if emit_event:
            _append_store_event(self.event_log, "realm.updated", realm_id=item.id, change="saved")
        return self.get(item.id)

    def archive(self, realm_id: str) -> Realm:
        """Recoverably remove a Realm from live selectors and projections."""

        item = self.get(realm_id)
        if item.archived:
            return item
        item.archived = True
        item = self.save(item, emit_event=False)
        _append_store_event(
            self.event_log,
            "realm.archived",
            realm_id=item.id,
            name=item.name,
        )
        return item

    def bind_server(self, realm_id: str, server_id: str) -> Realm:
        item = self.get(realm_id)
        item.server_id = safe_id(server_id)
        item = self.save(item, emit_event=False)
        _append_store_event(
            self.event_log, "realm.updated", realm_id=item.id, change="server_bound", server_id=item.server_id
        )
        return item

    def set_skill_selection(
        self, realm_id: str, *, mode: str, selection: list[str], dry_run: bool = False
    ) -> Realm:
        """Single write chokepoint for a realm's shared-skill publish selection.

        ``mode == "all"`` publishes every shared skill and PRESERVES the stored
        ``skill_selection`` intact (switching back to "selected" restores it, so
        the passed ``selection`` is ignored in this mode). ``mode == "selected"``
        replaces the selection with the validated/deduped/sorted slugs (an empty
        list means "publish none").

        Slugs are validated for shape only (non-empty, no leading dot, no path
        separators, must equal their ``safe_path_token`` form). Slugs unknown to
        this machine's catalog are NOT filtered here — another member may hold
        the skill locally, and dropping it on an unrelated save would corrupt
        realm truth; unknown slugs are reported (``missing``) by the CLI, never
        stripped. Emits ``realm.updated``/``skill_selection`` so the read-model
        pipeline sees the mutation (Stage 12 watermark discipline).

        ``dry_run`` runs the full validation and returns the WOULD-BE realm
        (in-memory only) without saving and without emitting the store event.
        """
        if mode not in {"all", "selected"}:
            raise ValueError(f"invalid skill_publish_mode: {mode!r}")
        item = self.get(realm_id)
        if mode == "selected":
            item.skill_selection = _normalize_skill_selection(selection)
        item.skill_publish_mode = mode
        if dry_run:
            return item
        item = self.save(item, emit_event=False)
        _append_store_event(
            self.event_log,
            "realm.updated",
            realm_id=item.id,
            change="skill_selection",
            mode=mode,
            selection_count=len(item.skill_selection),
        )
        return item

    def tombstone_skill(
        self,
        realm_id: str,
        slug: str,
        *,
        deleted_hash: str | None = None,
        dry_run: bool = False,
    ) -> Realm:
        """Single write chokepoint for a realm's shared-skill delete ledger.

        Records realm-wide intent: "this skill is deleted, not merely
        unpublished here". Without the distinction, narrowing
        ``skill_selection`` would be indistinguishable from a delete and would
        destroy members' local copies that were never meant to die.

        Refusals are typed (:class:`~.errors.SkillTombstoneRefused`) and raised
        BEFORE any mutation:

        - ``skill_slug_invalid`` — shape refusal from the promotion door's own
          validator, so a delete and a promotion share one alphabet.
        - ``skill_installer_owned`` — ``CANONICAL_SHARED_SKILL_IDS`` are
          reinstalled from repo source on every pull; a ledger entry for one
          would lose that argument forever, so the door names the real delete
          lane instead of minting a tombstone that does nothing.

        Re-tombstoning an already-listed slug REPLACES the entry (one entry per
        slug), which refreshes ``deleted_at`` and clears any ``restored_at``
        from an earlier lift — the register records the CURRENT state, never a
        delete stamp sitting beside a stale restore stamp. The list is bounded
        through :func:`prune_settled_ledger`, so settled (restored) history is
        evicted before any live block. The slug is also pruned from ``skill_selection`` at the
        same write — a selection naming a tombstoned slug is a standing
        contradiction — using the ``skill_tombstoned`` match rule, so a
        categorized ``<cat>/<child>`` selection entry blocked by a bare-name
        tombstone goes too. ``restore_skill`` does NOT re-add it: selection is a
        separate, deliberate act (``realm skills set``).

        ``dry_run`` runs the full validation and returns the WOULD-BE realm
        (in-memory only) without saving and without emitting the store event.
        """
        from hermes_constants import CANONICAL_SHARED_SKILL_IDS

        from .skill_promotion import validate_skill_slug

        clean = str(slug or "").strip()
        reason = validate_skill_slug(clean)
        if reason is not None:
            raise SkillTombstoneRefused(
                "skill_slug_invalid",
                f"{clean!r} is not a valid skill slug: {reason}",
                safe_details={"slug": clean},
            )
        if clean in CANONICAL_SHARED_SKILL_IDS:
            raise SkillTombstoneRefused(
                "skill_installer_owned",
                (
                    f"{clean!r} is a hermes-installed harness skill: every realm pull "
                    "reinstalls it from repo source, so a realm tombstone can never "
                    "hold. Delete it from hermes_constants.CANONICAL_SHARED_SKILL_IDS "
                    "and docs/agent-runtime-harness/harness-skills/ instead."
                ),
                safe_details={"slug": clean},
            )

        item = self.get(realm_id)
        ledger = [
            entry for entry in (item.skill_tombstones or []) if entry.slug != clean
        ]
        ledger.append(SkillTombstone(slug=clean, deleted_at=now(), deleted_hash=deleted_hash))
        item.skill_tombstones = _prune_skill_tombstones(ledger)
        item.skill_selection = [
            value
            for value in (item.skill_selection or [])
            if not _tombstone_blocks(clean, value)
        ]
        if dry_run:
            return item
        item = self.save(item, emit_event=False)
        _append_store_event(
            self.event_log,
            "realm.updated",
            realm_id=item.id,
            change="skill_tombstoned",
            slug=clean,
        )
        return item

    def restore_skill(self, realm_id: str, slug: str, *, dry_run: bool = False) -> Realm:
        """Lift ONE ledger entry — the explicit door out of a skill tombstone.

        Names a LEDGER ENTRY, not a package: the entry whose ``slug`` matches
        exactly is lifted, which is what ``realm skills show`` lists. (A
        categorized package blocked by a bare-name tombstone is restored by
        naming that bare name.) Restoring content is the separate, existing
        lane — ``skills promote --from-path shared/skills/.archive/<ts>/<slug>``,
        or a fresh publish from a member who still holds it.

        The lift STAMPS ``restored_at`` rather than removing the entry (RD-11).
        A removal is an absence, and the pull-time union merge cannot tell an
        absence that means "restored here" from one that means "this member
        never heard about the delete" — so under a union every restore would be
        undone by the next pull from a member who still held the tombstone. The
        marker is a fact that can win the merge on its own timestamp. The entry
        stops blocking the moment it is stamped (:func:`active_skill_tombstones`),
        which is what every enforcement point and receipt reads.

        Idempotent: an absent entry — or one already lifted — is not an error
        and writes nothing (a no-op mutation must not emit an event either — the
        watermark advances only for real writes). Callers report ``restored`` by
        asking the ACTIVE ledger first.
        """
        clean = str(slug or "").strip()
        item = self.get(realm_id)
        lifted = next(
            (entry for entry in active_skill_tombstones(item) if entry.slug == clean),
            None,
        )
        if lifted is None:
            return item
        lifted.restored_at = now()
        item.skill_tombstones = _prune_skill_tombstones(item.skill_tombstones or [])
        if dry_run:
            return item
        item = self.save(item, emit_event=False)
        _append_store_event(
            self.event_log,
            "realm.updated",
            realm_id=item.id,
            change="skill_tombstone_restored",
            slug=clean,
        )
        return item

    def set_agent_selection(
        self, realm_id: str, *, mode: str, selection: list[str], dry_run: bool = False
    ) -> Realm:
        """Single write chokepoint for Realm persona-definition selection.

        ``workspace`` keeps the explicit list intact but publishes only the
        definitions required by synced workspace/Office references.
        ``selected`` publishes the explicit set plus those required references.
        Unknown persona ids are preserved and reported by the CLI envelope.
        """
        if mode not in {"workspace", "selected"}:
            raise ValueError(f"invalid agent_publish_mode: {mode!r}")
        item = self.get(realm_id)
        if mode == "selected":
            item.agent_selection = _normalize_agent_selection(selection)
        item.agent_publish_mode = mode
        if dry_run:
            return item
        item = self.save(item, emit_event=False)
        _append_store_event(
            self.event_log,
            "realm.updated",
            realm_id=item.id,
            change="agent_selection",
            mode=mode,
            selection_count=len(item.agent_selection),
        )
        return item

    def set_active(self, realm_id: str | None, *, issued_at: str | None = None) -> dict:
        value = safe_id(realm_id)
        name = self.get(value).name if value else None
        decision, current_value, basis = _resolve_activation_write(
            paths.active_realm_path(), "realm_id", value, issued_at
        )
        if decision != "apply":
            return {"realm_id": current_value, "applied": False, "reason": decision, "requested_realm_id": value}
        _write_model(
            paths.active_realm_path(),
            {"realm_id": value, "updated_at": now(), "intent_issued_at": basis},
        )
        if value:
            _append_store_event(self.event_log, "realm.activated", realm_id=value, name=name)
        else:
            _append_store_event(self.event_log, "realm.activated", cleared=True)
        return {"realm_id": value, "applied": True}

    def active_id(self) -> str | None:
        try:
            raw = _read_json(paths.active_realm_path())
        except Exception:
            return None
        return safe_id(raw.get("realm_id"))


class RunStore:
    def get(self, run_id: str) -> AgentRun:
        return _read_model(AgentRun, paths.run_path(run_id))

    def list_all(self) -> list[AgentRun]:
        return _list_models(AgentRun, paths.runs_dir())


class IncidentStore:
    def get(self, incident_id: str) -> Incident:
        return _read_model(Incident, paths.incident_path(incident_id))

    def list_all(self) -> list[Incident]:
        return _list_models(Incident, paths.incidents_dir())
