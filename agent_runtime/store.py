from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
from pathlib import Path
from typing import TypeVar

from hermes_time import now
from utils import atomic_json_write

from . import paths
from .errors import AlreadyExists, NotFound, WorkspaceDeleteBlocked
from .events import EventLog
from .locks import run_lock
from .models import AgentPersona, AgentRun, Event, Incident, Realm, Workspace
from .serde import from_jsonable, to_jsonable
from .state_patches import emit_incident_remove
from .simplified_contract import public_decision_type_value
from .states import RunState

T = TypeVar("T")

TERMINAL_RUN_STATES = frozenset({RunState.COMPLETED, RunState.FAILED, RunState.STALE, RunState.CANCELLED})
ACTIVE_RUN_STATES = frozenset({RunState.QUEUED, RunState.STARTING, RunState.RUNNING, RunState.WAITING_ON_TOOL, RunState.WAITING_ON_APPROVAL})
# Bound on Realm.deleted_workspace_ids — the workspace-delete resurrection
# guard. Oldest entries fall off first; by then every member has long since
# pulled the tombstone (the bounded-ledger idiom shared with the board/office
# archived ledgers).
DELETED_WORKSPACE_LEDGER_CAP = 500


def _safe_operator_reason(reason: str) -> str:
    return "operator requested cancellation"


def _safe_session_id(value) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        lowered = value.lower()
        if not any(marker in lowered for marker in ("secret", "token", "password", "credential", "cookie", "key")):
            return value
    return None


def _safe_model_id(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    cleaned = "".join(ch if ch.isalnum() or ch in "_.:-" else "_" for ch in text)
    return cleaned.strip("._:-")[:120] or None


def _safe_display_name(value) -> str:
    return " ".join(str(value or "").split())[:160]


def _slugify(value) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return slug[:80] or "item"


def _dedupe_ids(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        clean = _safe_model_id(value)
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
    current_value = _safe_model_id(current.get(key))
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
            id=_safe_model_id(workspace_id) or f"ws_{slug}_{uuid.uuid4().hex[:6]}",
            slug=slug,
            name=clean_name,
            agent_ids=_dedupe_ids(agent_ids or []),
            default_blueprint_id=_safe_model_id(default_blueprint_id),
            isolation=clean_isolation,
            max_concurrent_lanes=max_concurrent_lanes if max_concurrent_lanes is None else max(1, int(max_concurrent_lanes)),
            realm_id=_safe_model_id(realm_id),
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
        value = _safe_model_id(workspace_id)
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
        return _safe_model_id(raw.get("workspace_id"))

    def add_agent(self, workspace_id: str, persona_id: str) -> Workspace:
        item = self.get(workspace_id)
        persona = _safe_model_id(persona_id)
        if persona and persona not in item.agent_ids:
            item.agent_ids.append(persona)
        item = self.save(item, emit_event=False)
        _append_store_event(
            self.event_log, "workspace.updated", workspace_id=item.id, change="agent_added", persona_id=persona
        )
        return item

    def remove_agent(self, workspace_id: str, persona_id: str) -> Workspace:
        item = self.get(workspace_id)
        persona = _safe_model_id(persona_id)
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

        with office_lock(item.id):
            shutil.rmtree(paths.office_dir(item.id), ignore_errors=True)
        for board in BoardStore(event_log=self.event_log).list_all():
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
    dot, no path separator, and identical to their ``_safe_token`` form — the
    same tokenizer the realm publisher uses for skill directory names, so a
    valid slug round-trips to its published path. Every malformed slug is
    collected and reported in ONE ``ValueError`` (mapped to a typed
    ``invalid_request`` at the CLI seam) so a batch save names all offenders
    instead of failing on the first. Slugs unknown to the local catalog are
    NOT filtered here — that is realm truth another member may own.
    """
    # Function-local import breaks the module cycle (realm_sync imports store).
    from agent_runtime.realm_sync import _safe_token

    cleaned: set[str] = set()
    rejected: list[str] = []
    for raw in selection or []:
        slug = str(raw).strip()
        if (
            not slug
            or slug.startswith(".")
            or "/" in slug
            or "\\" in slug
            or slug != _safe_token(slug)
        ):
            rejected.append(slug or repr(raw))
            continue
        cleaned.add(slug)
    if rejected:
        raise ValueError(
            "malformed skill selection slug(s): " + ", ".join(repr(slug) for slug in sorted(set(rejected)))
        )
    return sorted(cleaned)


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
        normalized = _safe_model_id(value)
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
            id=_safe_model_id(realm_id) or f"realm_{slug}_{uuid.uuid4().hex[:6]}",
            slug=slug,
            name=clean_name,
            server_id=_safe_model_id(server_id),
            default_workspace_id=_safe_model_id(default_workspace_id),
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
        item.server_id = _safe_model_id(server_id)
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
        separators, must equal their ``_safe_token`` form). Slugs unknown to
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
        value = _safe_model_id(realm_id)
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
        return _safe_model_id(raw.get("realm_id"))


class RunStore:
    def __init__(self, event_log: EventLog | None = None):
        self.event_log = event_log or EventLog()

    def get(self, run_id: str) -> AgentRun:
        return _read_model(AgentRun, paths.run_path(run_id))

    def update(self, run: AgentRun) -> bool:
        with run_lock(run.id):
            run.session_id = _safe_session_id(run.session_id)
            try:
                previous = self.get(run.id)
            except NotFound:
                previous = None
            if previous is not None and previous.state in TERMINAL_RUN_STATES:
                return False
            _write_model(paths.run_path(run.id), run)
            return True

    def close_run(
        self,
        run_id: str,
        *,
        state: RunState,
        final_decision=None,
        error=None,
    ) -> AgentRun:
        with run_lock(run_id):
            run = self.get(run_id)
            run.session_id = _safe_session_id(run.session_id)
            if run.state in TERMINAL_RUN_STATES:
                return run
            run.state = state if isinstance(state, RunState) else RunState(state)
            run.finished_at = now()
            if final_decision and isinstance(final_decision, dict):
                public_type = public_decision_type_value(final_decision.get("type"))
                if public_type and public_type != final_decision.get("type"):
                    raw_decision_type = final_decision.get("type")
                    final_decision = {**final_decision, "type": public_type}
                    final_decision.setdefault("execution_type", raw_decision_type)
            run.final_decision = final_decision
            run.error = error
            _write_model(paths.run_path(run.id), run)
            payload = {"state": str(run.state)}
            if isinstance(final_decision, dict):
                payload["decision_type"] = final_decision.get("type")
                payload["validation_status"] = "valid"
            if run.session_id:
                payload["session_id"] = run.session_id
            self.event_log.append(
                Event(
                    ts=now(),
                    type="run.closed",
                    task_id=run.task_id,
                    run_id=run.id,
                    persona_id=run.persona_id,
                    payload=payload,
                )
            )
            return run

    def cancel(self, run_id: str, *, reason: str) -> AgentRun:
        return self.close_run(
            run_id,
            state=RunState.CANCELLED,
            error={"type": "operator_cancelled", "summary": _safe_operator_reason(reason)},
        )

    def list_for_task(self, task_id: str) -> list[AgentRun]:
        return [run for run in self.list_all() if run.task_id == task_id]

    def list_all(self) -> list[AgentRun]:
        return _list_models(AgentRun, paths.runs_dir())


def _safe_event_token(value, *, fallback: str | None) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        return value
    return fallback


class IncidentStore:
    def __init__(self, event_log: EventLog | None = None):
        self.event_log = event_log or EventLog()

    def open(self, incident: Incident) -> Incident:
        _write_model(paths.incident_path(incident.id), incident)
        # NOTE: intentionally does NOT auto-link the incident into
        # task.open_incident_ids. The settle boundary (ticker._settled_boundary)
        # treats a linked incident as "Neko owns recovery" vs an unlinked one as
        # a hard stop, so centralized linking here changes stop-vs-recover
        # semantics. Linking must be done deliberately at the call sites that
        # want recovery routing, paired with a recovery-semantics review.
        metadata = incident.metadata if isinstance(getattr(incident, "metadata", None), dict) else {}
        payload = {"incident_id": incident.id, "kind": incident.kind}
        for key in ("proof_id", "check_id", "environment_fingerprint", "blocking_event_id", "stage_id", "persona_target", "lane_id", "lane_state_at_open"):
            value = metadata.get(key)
            if isinstance(value, str) and _safe_event_token(value, fallback=None):
                payload[key] = value
        if isinstance(metadata.get("budget_state"), dict):
            payload["budget_state"] = metadata["budget_state"]
        if isinstance(metadata.get("repo_lock_state"), dict):
            payload["repo_lock_state"] = metadata["repo_lock_state"]
        self.event_log.append(
            Event(
                ts=now(),
                type="incident.opened",
                task_id=incident.task_id,
                run_id=incident.run_id,
                persona_id=None,
                payload=payload,
            )
        )
        return incident

    def get(self, incident_id: str) -> Incident:
        return _read_model(Incident, paths.incident_path(incident_id))

    def close(self, incident_id: str, *, reason: str | None = None) -> Incident:
        from .locks import incident_lock

        with incident_lock(incident_id):
            incident = self.get(incident_id)
            incident.closed_at = now()
            _write_model(paths.incident_path(incident_id), incident)
            payload = {"incident_id": incident.id}
            if reason:
                payload["reason"] = reason
            self.event_log.append(
                Event(
                    ts=now(),
                    type="incident.closed",
                    task_id=incident.task_id,
                    run_id=incident.run_id,
                    persona_id=None,
                    payload=payload,
                )
            )
            # S7-A producer: incidents ship open-only in the frame (settled), so a
            # close is a ``remove`` op — the launcher deletes the keyed row. Dark
            # by default (read_model.delta_patches off).
            emit_incident_remove(
                self.event_log,
                incident,
            )
            return incident

    def list_open_with_closed_count(self) -> tuple[list[Incident], int]:
        """Return live incidents without deserializing the closed-history tail.

        Mission Control keeps only open incidents in its steady-state frame;
        closed rows are represented by a count and fetched on demand.  Large
        long-lived runtimes can have thousands of closed incident files, and
        coercing every one into the recursive ``Incident`` dataclass graph on
        every snapshot made history cost as much as live state.

        We still JSON-decode every file so corrupt store rows fail exactly as
        ``list_all`` does.  Only closed rows skip the substantially more
        expensive ``from_jsonable`` pass.
        """

        directory = paths.incidents_dir()
        if not directory.exists():
            return [], 0
        open_incidents: list[Incident] = []
        closed_count = 0
        for path in directory.glob("*.json"):
            try:
                raw = _read_json(path)
            except NotFound:
                # Preserve the archive-race tolerance of ``_list_models``.
                continue
            if raw.get("closed_at") is not None:
                closed_count += 1
                continue
            open_incidents.append(from_jsonable(Incident, raw))
        return sorted(open_incidents, key=lambda item: item.id), closed_count

    def list_all(self) -> list[Incident]:
        return _list_models(Incident, paths.incidents_dir())
