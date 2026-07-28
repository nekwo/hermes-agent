from __future__ import annotations

from dataclasses import dataclass

from .errors import DefaultScopeReconciliationRequired, NotFound, StoreCorrupt
from .models import Realm, Workspace
from .store import RealmStore, WorkspaceStore


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
    if workspace.id in realm.deleted_workspace_ids:
        realm.deleted_workspace_ids = [
            workspace_id
            for workspace_id in realm.deleted_workspace_ids
            if workspace_id != workspace.id
        ]
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
    from .store import TaskStore
    from .workspace_scope import exact_scoped_instance_ids

    realm_store = RealmStore()
    workspace_store = WorkspaceStore()
    canonical = _get_realm(realm_store, DEFAULT_REALM_ID)
    reserved_workspace = _get_workspace(workspace_store, DEFAULT_WORKSPACE_ID)
    legacy = _legacy_default_realms(realm_store)
    archived_default_like = [
        item
        for item in realm_store.list_all(include_archived=True)
        if item.archived and item.id != DEFAULT_REALM_ID and _is_default_equivalent(item)
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

    tasks = TaskStore()
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
            goal_ids = [
                getattr(task, "goal_id", None) or task.id
                for task in tasks.list_for_workspace(workspace.id)
            ]
            board_ids = [
                board.board_id
                for board in boards.list_for_workspace(workspace.id, include_archived=True)
            ]
            actors = offices.list_actors(workspace.id, include_archived=True)
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
                    "goal_ids": goal_ids,
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
        "apply_supported": False,
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


def _get_realm(store: RealmStore, realm_id: str) -> Realm | None:
    try:
        return store.get(realm_id)
    except NotFound:
        return None


def _get_workspace(store: WorkspaceStore, workspace_id: str) -> Workspace | None:
    try:
        return store.get(workspace_id)
    except NotFound:
        return None


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
        store.get(realm_id)
    except NotFound:
        return False
    return True


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
