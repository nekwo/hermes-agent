import pytest

from agent_runtime.default_scope import (
    DEFAULT_REALM_ID,
    DEFAULT_WORKSPACE_ID,
    ensure_default_scope,
)
from agent_runtime.errors import StoreCorrupt
from agent_runtime.store import RealmStore, WorkspaceStore


def test_default_scope_is_durable_and_relationship_complete():
    created = ensure_default_scope(agent_ids=["neko_supervisor", "dev"])

    assert created.created_realm is True
    assert created.created_workspace is True
    assert created.realm.id == DEFAULT_REALM_ID
    assert created.realm.server_id is None
    assert created.realm.default_workspace_id == DEFAULT_WORKSPACE_ID
    assert created.realm.workspace_ids == [DEFAULT_WORKSPACE_ID]
    assert created.workspace.id == DEFAULT_WORKSPACE_ID
    assert created.workspace.realm_id == DEFAULT_REALM_ID
    assert created.workspace.agent_ids == ["neko_supervisor", "dev"]
    assert RealmStore().active_id() == DEFAULT_REALM_ID
    assert WorkspaceStore().active_id() == DEFAULT_WORKSPACE_ID


def test_reinitializing_after_join_preserves_both_realms_and_active_scope():
    ensure_default_scope(agent_ids=["neko_supervisor", "dev"])
    joined = RealmStore().create(name="Joined", server_id="server_1")
    joined_workspace = WorkspaceStore().create(
        name="Shared Office",
        realm_id=joined.id,
    )
    joined.workspace_ids.append(joined_workspace.id)
    RealmStore().save(joined)
    RealmStore().set_active(joined.id)
    WorkspaceStore().set_active(joined_workspace.id)

    existing = ensure_default_scope(agent_ids=["qa"])

    assert existing.created_realm is False
    assert existing.created_workspace is False
    assert {realm.id for realm in RealmStore().list_all()} == {
        DEFAULT_REALM_ID,
        joined.id,
    }
    assert RealmStore().active_id() == joined.id
    assert WorkspaceStore().active_id() == joined_workspace.id
    # Idempotent init does not treat the requested seed roster as an update.
    assert existing.workspace.agent_ids == ["neko_supervisor", "dev"]


def test_legacy_unscoped_default_workspace_is_adopted_without_data_loss():
    legacy = WorkspaceStore().create(
        name="My Local Office",
        workspace_id=DEFAULT_WORKSPACE_ID,
        agent_ids=["custom_agent"],
    )

    result = ensure_default_scope(agent_ids=["neko_supervisor"])

    assert result.created_workspace is False
    assert result.workspace.realm_id == DEFAULT_REALM_ID
    assert result.workspace.name == legacy.name
    assert result.workspace.agent_ids == ["custom_agent"]
    assert result.realm.default_workspace_name == legacy.name


def test_reserved_default_workspace_collision_fails_before_partial_write():
    other = RealmStore().create(name="Other", server_id="server_1")
    WorkspaceStore().create(
        name="Collision",
        workspace_id=DEFAULT_WORKSPACE_ID,
        realm_id=other.id,
    )

    with pytest.raises(StoreCorrupt, match="reserved"):
        ensure_default_scope()

    assert {realm.id for realm in RealmStore().list_all()} == {other.id}
