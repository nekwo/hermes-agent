from __future__ import annotations

import hashlib
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from hermes_time import now
from utils import atomic_json_write

from . import paths
from .errors import DefaultScopeReconciliationRequired, NotFound, StoreCorrupt
from .locks import archive_lock
from .models import Realm, Workspace
from .store import RealmStore, WorkspaceStore, lift_deleted_workspace


DEFAULT_REALM_ID = "realm_default"
DEFAULT_WORKSPACE_ID = "ws_default"
DEFAULT_SCOPE_NAME = "Default"


@dataclass(frozen=True, slots=True)
class DefaultScope:
    realm: Realm
    workspace: Workspace
    created_realm: bool
    created_workspace: bool
    adopted_legacy_realm: bool = False


def ensure_default_scope(*, agent_ids: list[str] | None = None) -> DefaultScope:
    """Materialize the launcher's durable local baseline scope.

    ``harness init`` is idempotent, so this coordinator must be too. New stores
    receive fixed ids, while one unambiguous pre-id local ``default`` realm is
    adopted in place so startup never creates a case-variant twin. Existing
    operator state is preserved: ids, roster edits, names, sibling workspaces,
    and a valid active server realm/workspace are never overwritten.
    """

    realm_store = RealmStore()
    workspace_store = WorkspaceStore()

    realm = _get_realm(realm_store, DEFAULT_REALM_ID)
    reserved_workspace = _get_workspace(workspace_store, DEFAULT_WORKSPACE_ID)
    legacy_realms = _legacy_default_realms(realm_store)

    if realm is not None and legacy_realms:
        _raise_reconciliation_required(
            "Canonical and legacy default realms coexist; startup will not merge identities.",
            realm_ids=[realm.id, *[item.id for item in legacy_realms]],
        )
    if realm is None and len(legacy_realms) > 1:
        _raise_reconciliation_required(
            "Multiple local default-like realms exist; startup cannot choose one safely.",
            realm_ids=[item.id for item in legacy_realms],
        )

    adopted_legacy_realm = realm is None and len(legacy_realms) == 1
    if adopted_legacy_realm:
        realm = legacy_realms[0]

    workspace = None
    if realm is not None:
        workspace, workspace_issue = _select_default_workspace(
            realm,
            workspace_store=workspace_store,
            reserved_workspace=reserved_workspace,
        )
        if workspace_issue:
            _raise_reconciliation_required(
                workspace_issue,
                realm_ids=[realm.id],
                workspace_ids=[
                    item.id
                    for item in workspace_store.list_all(include_archived=True)
                    if item.realm_id == realm.id
                ],
            )

    # Validate both reserved identities before writing either half, avoiding a
    # partially-created baseline when an older/custom store already claimed a
    # fixed id for incompatible authority.
    if realm is not None and realm.server_id:
        raise StoreCorrupt(
            f"{DEFAULT_REALM_ID} is reserved for the local default realm"
        )
    target_realm_id = realm.id if realm is not None else DEFAULT_REALM_ID
    if reserved_workspace is not None and reserved_workspace.realm_id not in {
        None,
        target_realm_id,
    }:
        raise StoreCorrupt(
            f"{DEFAULT_WORKSPACE_ID} is reserved for the local default realm"
        )

    created_realm = realm is None
    if realm is None:
        realm = realm_store.create(
            name=DEFAULT_SCOPE_NAME,
            realm_id=DEFAULT_REALM_ID,
            default_workspace_id=DEFAULT_WORKSPACE_ID,
            default_workspace_name=DEFAULT_SCOPE_NAME,
        )
        workspace = reserved_workspace

    created_workspace = workspace is None
    if workspace is None:
        workspace = workspace_store.create(
            name=DEFAULT_SCOPE_NAME,
            workspace_id=DEFAULT_WORKSPACE_ID,
            realm_id=realm.id,
            agent_ids=agent_ids,
        )
    elif workspace.realm_id is None:
        # A pre-realm workspace is the legacy form of this same local scope.
        # Adopt it without replacing any operator-authored fields or ids.
        workspace.realm_id = realm.id
        workspace = workspace_store.save(workspace)

    realm_changed = False
    if realm.default_workspace_id != workspace.id:
        realm.default_workspace_id = workspace.id
        realm_changed = True
    if realm.default_workspace_name != workspace.name:
        realm.default_workspace_name = workspace.name
        realm_changed = True
    if workspace.id not in realm.workspace_ids:
        realm.workspace_ids.append(workspace.id)
        realm_changed = True
    # A LIFT, not a bare local removal: the MARKER is what reaches the other
    # members (see ``store.lift_deleted_workspace``). Under RD-11's set-union
    # merge a removal is an absence, indistinguishable from "that peer never
    # heard about this delete", so the next pull from any member still carrying
    # the id put it straight back and the restore never left this machine.
    if lift_deleted_workspace(realm, workspace.id):
        realm_changed = True
    if realm_changed:
        realm = realm_store.save(realm)

    # Seed active pointers only when no valid choice exists. Re-running init
    # after joining a realm must not pull the operator back to Default.
    active_realm_id = realm_store.active_id()
    if active_realm_id is None or not _realm_exists(realm_store, active_realm_id):
        realm_store.set_active(realm.id)
        active_realm_id = realm.id

    active_workspace_id = workspace_store.active_id()
    if active_realm_id == realm.id and (
        active_workspace_id is None
        or not _workspace_belongs_to_realm(
            workspace_store,
            active_workspace_id,
            realm.id,
        )
    ):
        workspace_store.set_active(workspace.id)

    return DefaultScope(
        realm=realm,
        workspace=workspace,
        created_realm=created_realm,
        created_workspace=created_workspace,
        adopted_legacy_realm=adopted_legacy_realm,
    )


def preview_default_scope_migration() -> dict:
    """Read-only inventory for a possible default-scope reconciliation.

    The preview never writes, renames, merges, archives, or re-points data. It
    accounts for every default-like candidate's pointers, workspaces, goals,
    boards, office actors, persisted roster, and exact scoped persona-instance
    rows so an operator can approve a later identity-changing migration with
    the full blast radius visible.
    """

    from .board_store import BoardStore
    from .office_store import OfficeStore
    from .persona_assignments import PersonaInstanceStore
    from .workspace_scope import exact_scoped_instance_ids

    realm_store = RealmStore()
    workspace_store = WorkspaceStore()
    canonical = _get_realm(realm_store, DEFAULT_REALM_ID)
    reserved_workspace = _get_workspace(workspace_store, DEFAULT_WORKSPACE_ID)
    legacy = _legacy_default_realms(realm_store)
    archived_default_like = [
        item
        for item in realm_store.list_all(include_archived=True)
        if item.archived and _is_default_equivalent(item)
    ]

    selected = canonical or (legacy[0] if len(legacy) == 1 else None)
    selected_workspace = None
    workspace_issue = None
    if selected is not None:
        selected_workspace, workspace_issue = _select_default_workspace(
            selected,
            workspace_store=workspace_store,
            reserved_workspace=reserved_workspace,
        )
    elif reserved_workspace is not None and reserved_workspace.realm_id not in {
        None,
        DEFAULT_REALM_ID,
    }:
        workspace_issue = f"{DEFAULT_WORKSPACE_ID} belongs to another realm"

    if canonical is not None and legacy:
        status = "reconciliation_required"
        reason = "canonical_and_legacy_default_realms_coexist"
    elif canonical is None and len(legacy) > 1:
        status = "reconciliation_required"
        reason = "multiple_legacy_default_realms"
    elif workspace_issue:
        status = "reconciliation_required"
        reason = "default_workspace_ambiguous"
    elif canonical is not None:
        status = "canonical"
        reason = None
    elif len(legacy) == 1:
        status = "legacy_adoption_ready"
        reason = None
    else:
        status = "canonical_creation_ready"
        reason = None

    boards = BoardStore()
    offices = OfficeStore()
    instances = PersonaInstanceStore().list_all()
    all_workspaces = workspace_store.list_all(include_archived=True)
    all_realms = realm_store.list_all(include_archived=True)
    candidate_realms = list(
        {
            item.id: item
            for item in [canonical, *legacy, *archived_default_like]
            if item is not None
        }.values()
    )
    candidate_realm_ids = {item.id for item in candidate_realms}

    scopes: list[dict] = []
    for candidate in sorted(candidate_realms, key=lambda item: item.id):
        configured_ids = list(candidate.workspace_ids or [])
        related = [item for item in all_workspaces if item.realm_id == candidate.id]
        related_by_id = {item.id: item for item in related}
        if (
            selected is not None
            and candidate.id == selected.id
            and selected_workspace is not None
        ):
            related_by_id[selected_workspace.id] = selected_workspace
        for workspace_id in [candidate.default_workspace_id, *configured_ids]:
            if not workspace_id or workspace_id in related_by_id:
                continue
            item = _get_workspace(workspace_store, workspace_id)
            if item is not None:
                related_by_id[item.id] = item
        workspace_rows: list[dict] = []
        for workspace in sorted(related_by_id.values(), key=lambda item: item.id):
            board_ids = [
                board.board_id
                for board in boards.list_for_workspace(workspace.id, include_archived=True)
            ]
            # ``.actors``, shortfall dropped on purpose: this row DESCRIBES a
            # workspace for an operator choosing between duplicate scopes, and
            # a scope listing that refused because one actor file would not
            # decode would hide the very workspace the operator is trying to
            # reconcile. Spelled out rather than inherited (AX5).
            actors = offices.scan_actors(workspace.id, include_archived=True).actors
            workspace_rows.append(
                {
                    "id": workspace.id,
                    "name": workspace.name,
                    "realm_id": workspace.realm_id,
                    "archived": bool(workspace.archived),
                    "declared_by_realm": workspace.id in configured_ids,
                    "realm_default": workspace.id == candidate.default_workspace_id,
                    "active": workspace.id == workspace_store.active_id(),
                    "roster_agent_ids": list(workspace.agent_ids or []),
                    "board_ids": board_ids,
                    "office_surface": offices.surface_exists(workspace.id),
                    "office_actor_keys": [actor.actor_key for actor in actors],
                    "office_persona_instance_ids": [
                        actor.persona_instance_id
                        for actor in actors
                        if actor.persona_instance_id
                    ],
                    "exact_persona_instance_ids": exact_scoped_instance_ids(
                        instances, workspace_id=workspace.id
                    ),
                }
            )
        scopes.append(
            {
                "realm": {
                    "id": candidate.id,
                    "name": candidate.name,
                    "server_id": candidate.server_id,
                    "archived": bool(candidate.archived),
                    "default_workspace_id": candidate.default_workspace_id,
                    "workspace_ids": configured_ids,
                    "active": candidate.id == realm_store.active_id(),
                },
                "workspaces": workspace_rows,
            }
        )

    return {
        "kind": "default_scope_migration_preview",
        "dry_run": True,
        "mutated": False,
        "apply_supported": (
            status == "reconciliation_required"
            and reason == "canonical_and_legacy_default_realms_coexist"
            and len(legacy) == 1
        ),
        "status": status,
        "reason": reason,
        "active_pointers": {
            "realm_id": realm_store.active_id(),
            "workspace_id": workspace_store.active_id(),
        },
        "canonical_ids": {
            "realm_id": DEFAULT_REALM_ID,
            "workspace_id": DEFAULT_WORKSPACE_ID,
        },
        "proposed_default_scope": {
            "realm_id": (
                None
                if status == "reconciliation_required"
                else selected.id if selected is not None else DEFAULT_REALM_ID
            ),
            "workspace_id": (
                None
                if status == "reconciliation_required"
                else selected_workspace.id
                if selected_workspace is not None
                else DEFAULT_WORKSPACE_ID
            ),
            "would_adopt_legacy_realm": (
                status != "reconciliation_required"
                and selected is not None
                and selected.id != DEFAULT_REALM_ID
            ),
            "would_create_realm": selected is None and status == "canonical_creation_ready",
            "would_create_workspace": selected_workspace is None and status != "reconciliation_required",
        },
        "candidate_scopes": scopes,
        "untouched_realm_ids": sorted(
            item.id for item in all_realms if item.id not in candidate_realm_ids
        ),
        "archived_default_like_realm_ids": [item.id for item in archived_default_like],
        "requires_operator_approval": status == "reconciliation_required",
    }


def reconcile_default_scope_to_legacy(
    *,
    winner_realm_id: str,
    winner_workspace_id: str,
) -> dict:
    """Archive an empty fixed-id duplicate and retain a chosen legacy scope.

    This is deliberately narrower than a general merge.  It refuses to move,
    combine, delete, or retire goals, boards, offices, or persona instances.
    The fixed-id loser must contain none of those live identity-bearing rows;
    its roster metadata remains recoverable inside the archived workspace.
    Every touched store file is copied to a timestamped backup before writes.
    """

    from .board_store import BoardStore
    from .office_store import OfficeStore
    from .persona_assignments import PersonaInstanceStore
    from .workspace_scope import exact_scoped_instance_ids

    realm_store = RealmStore()
    workspace_store = WorkspaceStore()

    with archive_lock():
        winner_realm = _get_realm(realm_store, winner_realm_id)
        winner_workspace = _get_workspace(workspace_store, winner_workspace_id)
        loser_realm = _get_realm(realm_store, DEFAULT_REALM_ID)
        loser_workspace = _get_workspace(workspace_store, DEFAULT_WORKSPACE_ID)

        if winner_realm is None or winner_workspace is None:
            _raise_reconciliation_required(
                "Chosen lowercase default scope is missing or archived.",
                realm_ids=[winner_realm_id],
                workspace_ids=[winner_workspace_id],
            )
        if winner_realm.id == DEFAULT_REALM_ID or winner_workspace.id == DEFAULT_WORKSPACE_ID:
            _raise_reconciliation_required(
                "The reconciliation winner must be the existing non-reserved default scope.",
                realm_ids=[winner_realm.id],
                workspace_ids=[winner_workspace.id],
            )
        if winner_realm.server_id or not _is_default_equivalent(winner_realm):
            _raise_reconciliation_required(
                "The reconciliation winner must be one local default-equivalent realm.",
                realm_ids=[winner_realm.id],
                workspace_ids=[winner_workspace.id],
            )
        if winner_workspace.realm_id not in {None, winner_realm.id}:
            _raise_reconciliation_required(
                "Chosen workspace does not belong to the chosen lowercase realm.",
                realm_ids=[winner_realm.id],
                workspace_ids=[winner_workspace.id],
            )
        other_legacy = [
            item.id
            for item in _legacy_default_realms(realm_store)
            if item.id != winner_realm.id
        ]
        if other_legacy:
            _raise_reconciliation_required(
                "Additional live default-like realms exist; no automatic winner is safe.",
                realm_ids=[winner_realm.id, *other_legacy],
                workspace_ids=[winner_workspace.id],
            )
        if loser_realm is None or loser_workspace is None:
            # Idempotent success after a previous application: archived fixed
            # ids are deliberately invisible to the live lookup.
            archived_realm = _get_realm(
                realm_store, DEFAULT_REALM_ID, include_archived=True
            )
            archived_workspace = _get_workspace(
                workspace_store, DEFAULT_WORKSPACE_ID, include_archived=True
            )
            if (
                archived_realm is not None
                and archived_realm.archived
                and archived_workspace is not None
                and archived_workspace.archived
            ):
                return {
                    "kind": "default_scope_reconciliation",
                    "status": "already_applied",
                    "mutated": False,
                    "winner_realm_id": winner_realm.id,
                    "winner_workspace_id": winner_workspace.id,
                    "archived_realm_id": DEFAULT_REALM_ID,
                    "archived_workspace_id": DEFAULT_WORKSPACE_ID,
                    "backup_dir": None,
                }
            _raise_reconciliation_required(
                "The fixed-id duplicate pair is incomplete; refusing partial reconciliation.",
                realm_ids=[winner_realm.id, DEFAULT_REALM_ID],
                workspace_ids=[winner_workspace.id, DEFAULT_WORKSPACE_ID],
            )
        if loser_realm.server_id or not _is_default_equivalent(loser_realm):
            _raise_reconciliation_required(
                "The fixed-id realm is not a disposable local default duplicate.",
                realm_ids=[winner_realm.id, loser_realm.id],
                workspace_ids=[winner_workspace.id, loser_workspace.id],
            )
        if loser_workspace.realm_id != loser_realm.id:
            _raise_reconciliation_required(
                "The fixed-id workspace does not belong to the fixed-id realm.",
                realm_ids=[winner_realm.id, loser_realm.id],
                workspace_ids=[winner_workspace.id, loser_workspace.id],
            )
        loser_workspace_ids = {
            item.id
            for item in workspace_store.list_all()
            if item.realm_id == loser_realm.id
        }
        if loser_workspace_ids != {loser_workspace.id}:
            _raise_reconciliation_required(
                "The fixed-id realm owns additional live workspaces; refusing to archive it.",
                realm_ids=[winner_realm.id, loser_realm.id],
                workspace_ids=sorted(loser_workspace_ids),
            )
        if realm_store.active_id() != winner_realm.id or workspace_store.active_id() != winner_workspace.id:
            _raise_reconciliation_required(
                "The chosen lowercase scope must already be the active operator scope.",
                realm_ids=[winner_realm.id, loser_realm.id],
                workspace_ids=[winner_workspace.id, loser_workspace.id],
            )

        boards = BoardStore().list_for_workspace(
            loser_workspace.id, include_archived=True
        )
        offices = OfficeStore()
        # ``.actors``, and here the drop is SAFE IN THE RIGHT DIRECTION: the
        # list is spent below only as a truthiness test for "does the loser hold
        # live scoped data", and an undecodable actor file is still a file in
        # that directory. It can make the check answer "empty" only if the
        # directory holds nothing BUT undecodable files — in which case the
        # sibling ``surface_exists`` check in the same condition still refuses
        # the merge. Spelled out rather than inherited (AX5).
        actors = offices.scan_actors(loser_workspace.id, include_archived=True).actors
        instances = exact_scoped_instance_ids(
            PersonaInstanceStore().list_all(), workspace_id=loser_workspace.id
        )
        if boards or actors or offices.surface_exists(loser_workspace.id) or instances:
            _raise_reconciliation_required(
                "The fixed-id duplicate contains live scoped data; a reviewed merge is required.",
                realm_ids=[winner_realm.id, loser_realm.id],
                workspace_ids=[winner_workspace.id, loser_workspace.id],
            )

        touched = [
            paths.realm_path(winner_realm.id),
            paths.workspace_path(winner_workspace.id),
            paths.realm_path(loser_realm.id),
            paths.workspace_path(loser_workspace.id),
            paths.active_realm_path(),
            paths.active_workspace_path(),
        ]
        backup_dir = _backup_default_scope_files(touched)
        try:
            if winner_workspace.realm_id is None:
                winner_workspace.realm_id = winner_realm.id
                winner_workspace = workspace_store.save(winner_workspace)
            changed = False
            if winner_realm.default_workspace_id != winner_workspace.id:
                winner_realm.default_workspace_id = winner_workspace.id
                changed = True
            if winner_realm.default_workspace_name != winner_workspace.name:
                winner_realm.default_workspace_name = winner_workspace.name
                changed = True
            if winner_workspace.id not in winner_realm.workspace_ids:
                winner_realm.workspace_ids.append(winner_workspace.id)
                changed = True
            # The same propagating lift as ``ensure_default_scope`` — one
            # chokepoint, so the reconcile path cannot drift into the old
            # local-only removal.
            if lift_deleted_workspace(winner_realm, winner_workspace.id):
                changed = True
            if changed:
                winner_realm = realm_store.save(winner_realm)

            workspace_store.archive(loser_workspace.id)
            realm_store.archive(loser_realm.id)
            _finish_default_scope_backup(backup_dir, status="applied")
        except Exception:
            _restore_default_scope_files(backup_dir)
            _finish_default_scope_backup(backup_dir, status="rolled_back")
            raise

        return {
            "kind": "default_scope_reconciliation",
            "status": "applied",
            "mutated": True,
            "winner_realm_id": winner_realm.id,
            "winner_workspace_id": winner_workspace.id,
            "archived_realm_id": loser_realm.id,
            "archived_workspace_id": loser_workspace.id,
            "backup_dir": str(backup_dir),
            "preserved_roster_agent_ids": list(loser_workspace.agent_ids or []),
        }


def _backup_default_scope_files(files: list[Path]) -> Path:
    created_at = now()
    stamp = created_at.strftime("%Y%m%dT%H%M%S%fZ")
    backup_dir = (
        paths.store_root()
        / "migration_backups"
        / f"default_scope_{stamp}_{uuid.uuid4().hex[:8]}"
    )
    before = backup_dir / "before"
    before.mkdir(parents=True, exist_ok=False)
    records = []
    for index, source in enumerate(files):
        destination = before / f"{index:02d}_{source.name}"
        existed = source.exists()
        if existed:
            shutil.copy2(source, destination)
        records.append(
            {
                "source": str(source),
                "backup": str(destination),
                "existed": existed,
                "sha256": _sha256(source) if existed else None,
            }
        )
    atomic_json_write(
        backup_dir / "manifest.json",
        {
            "kind": "default_scope_reconciliation_backup",
            "status": "prepared",
            "created_at": created_at.isoformat(),
            "files": records,
        },
        indent=2,
        sort_keys=True,
    )
    return backup_dir


def _restore_default_scope_files(backup_dir: Path) -> None:
    import json

    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    for record in manifest["files"]:
        source = Path(record["source"])
        if record["existed"]:
            shutil.copy2(Path(record["backup"]), source)
        else:
            source.unlink(missing_ok=True)


def _finish_default_scope_backup(backup_dir: Path, *, status: str) -> None:
    import json

    path = backup_dir / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["status"] = status
    manifest["finished_at"] = now().isoformat()
    atomic_json_write(path, manifest, indent=2, sort_keys=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _get_realm(
    store: RealmStore,
    realm_id: str,
    *,
    include_archived: bool = False,
) -> Realm | None:
    try:
        item = store.get(realm_id)
    except NotFound:
        return None
    return item if include_archived or not item.archived else None


def _get_workspace(
    store: WorkspaceStore,
    workspace_id: str,
    *,
    include_archived: bool = False,
) -> Workspace | None:
    try:
        item = store.get(workspace_id)
    except NotFound:
        return None
    return item if include_archived or not item.archived else None


def _is_default_equivalent(realm: Realm) -> bool:
    return any(
        str(value or "").strip().casefold() == DEFAULT_SCOPE_NAME.casefold()
        for value in (realm.name, realm.slug)
    )


def _legacy_default_realms(store: RealmStore) -> list[Realm]:
    return [
        item
        for item in store.list_all()
        if item.id != DEFAULT_REALM_ID
        and item.server_id is None
        and _is_default_equivalent(item)
    ]


def _select_default_workspace(
    realm: Realm,
    *,
    workspace_store: WorkspaceStore,
    reserved_workspace: Workspace | None,
) -> tuple[Workspace | None, str | None]:
    if reserved_workspace is not None and reserved_workspace.realm_id not in {
        None,
        realm.id,
    }:
        return None, f"{DEFAULT_WORKSPACE_ID} belongs to another realm"
    if realm.id == DEFAULT_REALM_ID and reserved_workspace is not None:
        return reserved_workspace, None

    workspaces = [
        item
        for item in workspace_store.list_all()
        if item.realm_id == realm.id and not item.archived
    ]
    if realm.default_workspace_id:
        declared = _get_workspace(workspace_store, realm.default_workspace_id)
        if declared is not None and not declared.archived:
            if declared.realm_id in {None, realm.id}:
                return declared, None
            return None, (
                f"Realm {realm.id} declares default workspace {declared.id}, "
                f"but that workspace belongs to {declared.realm_id}"
            )

    default_named = [
        item
        for item in workspaces
        if any(
            str(value or "").strip().casefold() == DEFAULT_SCOPE_NAME.casefold()
            for value in (item.name, item.slug)
        )
    ]
    if len(default_named) == 1:
        return default_named[0], None
    if len(workspaces) == 1:
        return workspaces[0], None
    if (
        not workspaces
        and reserved_workspace is not None
        and reserved_workspace.realm_id is None
    ):
        return reserved_workspace, None
    if not workspaces:
        return None, None
    return None, (
        f"Legacy default realm {realm.id} has multiple workspaces and no unique "
        "declared/default-like workspace"
    )


def _raise_reconciliation_required(
    message: str,
    *,
    realm_ids: list[str],
    workspace_ids: list[str] | None = None,
) -> None:
    raise DefaultScopeReconciliationRequired(
        message,
        safe_details={
            "candidate_realm_ids": sorted(set(realm_ids)),
            "candidate_workspace_ids": sorted(set(workspace_ids or [])),
            "preview_command": "hermes harness realm default-scope --dry-run --json",
        },
    )


def _realm_exists(store: RealmStore, realm_id: str) -> bool:
    try:
        realm = store.get(realm_id)
    except NotFound:
        return False
    return not realm.archived


def _workspace_belongs_to_realm(
    store: WorkspaceStore,
    workspace_id: str,
    realm_id: str,
) -> bool:
    try:
        workspace = store.get(workspace_id)
    except NotFound:
        return False
    return workspace.realm_id == realm_id and not workspace.archived
