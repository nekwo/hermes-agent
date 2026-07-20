from __future__ import annotations

from dataclasses import dataclass

from .errors import NotFound, StoreCorrupt
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


def ensure_default_scope(*, agent_ids: list[str] | None = None) -> DefaultScope:
    """Materialize the launcher's durable local baseline scope.

    ``harness init`` is idempotent, so this coordinator must be too. The fixed
    local ids make the baseline distinguishable from server-granted realms;
    adopting a realm can then add store rows without replacing an implicit UI
    placeholder. Existing operator state is preserved: roster edits, names,
    and a valid active server realm/workspace are never overwritten.
    """

    realm_store = RealmStore()
    workspace_store = WorkspaceStore()

    try:
        realm = realm_store.get(DEFAULT_REALM_ID)
    except NotFound:
        realm = None
    try:
        workspace = workspace_store.get(DEFAULT_WORKSPACE_ID)
    except NotFound:
        workspace = None

    # Validate both reserved identities before writing either half, avoiding a
    # partially-created baseline when an older/custom store already claimed a
    # fixed id for incompatible authority.
    if realm is not None and realm.server_id:
        raise StoreCorrupt(
            f"{DEFAULT_REALM_ID} is reserved for the local default realm"
        )
    if workspace is not None and workspace.realm_id not in {
        None,
        DEFAULT_REALM_ID,
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

    created_workspace = workspace is None
    if workspace is None:
        workspace = workspace_store.create(
            name=DEFAULT_SCOPE_NAME,
            workspace_id=DEFAULT_WORKSPACE_ID,
            realm_id=DEFAULT_REALM_ID,
            agent_ids=agent_ids,
        )
    elif workspace.realm_id is None:
        # A pre-realm ws_default row is the legacy form of this same local
        # workspace. Adopt it without replacing any operator-authored fields.
        workspace.realm_id = DEFAULT_REALM_ID
        workspace = workspace_store.save(workspace)

    realm_changed = False
    if realm.default_workspace_id != DEFAULT_WORKSPACE_ID:
        realm.default_workspace_id = DEFAULT_WORKSPACE_ID
        realm_changed = True
    if realm.default_workspace_name != workspace.name:
        realm.default_workspace_name = workspace.name
        realm_changed = True
    if DEFAULT_WORKSPACE_ID not in realm.workspace_ids:
        realm.workspace_ids.append(DEFAULT_WORKSPACE_ID)
        realm_changed = True
    if DEFAULT_WORKSPACE_ID in realm.deleted_workspace_ids:
        realm.deleted_workspace_ids = [
            workspace_id
            for workspace_id in realm.deleted_workspace_ids
            if workspace_id != DEFAULT_WORKSPACE_ID
        ]
        realm_changed = True
    if realm_changed:
        realm = realm_store.save(realm)

    # Seed active pointers only when no valid choice exists. Re-running init
    # after joining a realm must not pull the operator back to Default.
    active_realm_id = realm_store.active_id()
    if active_realm_id is None or not _realm_exists(realm_store, active_realm_id):
        realm_store.set_active(DEFAULT_REALM_ID)
        active_realm_id = DEFAULT_REALM_ID

    active_workspace_id = workspace_store.active_id()
    if active_realm_id == DEFAULT_REALM_ID and (
        active_workspace_id is None
        or not _workspace_belongs_to_default(
            workspace_store,
            active_workspace_id,
        )
    ):
        workspace_store.set_active(DEFAULT_WORKSPACE_ID)

    return DefaultScope(
        realm=realm,
        workspace=workspace,
        created_realm=created_realm,
        created_workspace=created_workspace,
    )


def _realm_exists(store: RealmStore, realm_id: str) -> bool:
    try:
        store.get(realm_id)
    except NotFound:
        return False
    return True


def _workspace_belongs_to_default(
    store: WorkspaceStore,
    workspace_id: str,
) -> bool:
    try:
        workspace = store.get(workspace_id)
    except NotFound:
        return False
    return workspace.realm_id == DEFAULT_REALM_ID and not workspace.archived
