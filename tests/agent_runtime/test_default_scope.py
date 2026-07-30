from pathlib import Path

import pytest

from agent_runtime.board_store import BoardStore
from agent_runtime.default_scope import (
    DEFAULT_REALM_ID,
    DEFAULT_WORKSPACE_ID,
    ensure_default_scope,
    preview_default_scope_migration,
    reconcile_default_scope_to_legacy,
)
from agent_runtime.errors import DefaultScopeReconciliationRequired, StoreCorrupt
from agent_runtime.models import PersonaInstance
from agent_runtime.office_store import OfficeStore
from agent_runtime.persona_assignments import PersonaInstanceStore
from agent_runtime.states import WorkerSessionState
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


def test_existing_lowercase_default_is_adopted_in_place_and_startup_is_idempotent():
    legacy_realm = RealmStore().create(name="default")
    legacy_workspace = WorkspaceStore().create(
        name="default",
        agent_ids=["neko_supervisor", "dev"],
    )
    legacy_realm.default_workspace_id = legacy_workspace.id
    legacy_realm.default_workspace_name = legacy_workspace.name
    legacy_realm.workspace_ids = [legacy_workspace.id]
    RealmStore().save(legacy_realm)
    RealmStore().set_active(legacy_realm.id)
    WorkspaceStore().set_active(legacy_workspace.id)

    first = ensure_default_scope(agent_ids=["qa"])
    second = ensure_default_scope(agent_ids=["qa"])

    assert first.adopted_legacy_realm is True
    assert second.adopted_legacy_realm is True
    assert first.realm.id == second.realm.id == legacy_realm.id
    assert first.workspace.id == second.workspace.id == legacy_workspace.id
    assert first.workspace.agent_ids == ["neko_supervisor", "dev"]
    assert {realm.id for realm in RealmStore().list_all()} == {legacy_realm.id}
    assert {workspace.id for workspace in WorkspaceStore().list_all()} == {
        legacy_workspace.id
    }
    assert RealmStore().active_id() == legacy_realm.id
    assert WorkspaceStore().active_id() == legacy_workspace.id


def test_legacy_default_with_multiple_workspaces_selects_declared_default_and_preserves_active():
    legacy_realm = RealmStore().create(name="default")
    declared = WorkspaceStore().create(name="Primary", realm_id=legacy_realm.id)
    active = WorkspaceStore().create(name="Research", realm_id=legacy_realm.id)
    legacy_realm.default_workspace_id = declared.id
    legacy_realm.default_workspace_name = declared.name
    legacy_realm.workspace_ids = [declared.id, active.id]
    RealmStore().save(legacy_realm)
    test_realm = RealmStore().create(name="test realm")
    RealmStore().set_active(legacy_realm.id)
    WorkspaceStore().set_active(active.id)

    result = ensure_default_scope()

    assert result.realm.id == legacy_realm.id
    assert result.workspace.id == declared.id
    assert RealmStore().active_id() == legacy_realm.id
    assert WorkspaceStore().active_id() == active.id
    assert {workspace.id for workspace in WorkspaceStore().list_all()} == {
        declared.id,
        active.id,
    }
    assert {realm.id for realm in RealmStore().list_all()} == {
        legacy_realm.id,
        test_realm.id,
    }


def test_canonical_and_legacy_default_coexistence_fails_closed_without_writes():
    canonical = ensure_default_scope()
    legacy_realm = RealmStore().create(name="default")
    legacy_workspace = WorkspaceStore().create(
        name="default",
        realm_id=legacy_realm.id,
    )
    before_realm_ids = [realm.id for realm in RealmStore().list_all()]
    before_workspace_ids = [workspace.id for workspace in WorkspaceStore().list_all()]

    with pytest.raises(DefaultScopeReconciliationRequired) as excinfo:
        ensure_default_scope()

    assert excinfo.value.code == "default_scope_reconciliation_required"
    assert excinfo.value.safe_details["candidate_realm_ids"] == sorted(
        [canonical.realm.id, legacy_realm.id]
    )
    assert [realm.id for realm in RealmStore().list_all()] == before_realm_ids
    assert [workspace.id for workspace in WorkspaceStore().list_all()] == before_workspace_ids
    assert RealmStore().active_id() == canonical.realm.id
    assert WorkspaceStore().active_id() == canonical.workspace.id
    assert legacy_workspace.realm_id == legacy_realm.id
    preview = preview_default_scope_migration()
    assert preview["status"] == "reconciliation_required"
    assert preview["proposed_default_scope"]["realm_id"] is None
    assert preview["proposed_default_scope"]["workspace_id"] is None


def test_default_scope_preview_inventories_candidates_without_mutation():
    legacy_realm = RealmStore().create(name="default")
    workspace = WorkspaceStore().create(
        name="default",
        realm_id=legacy_realm.id,
        agent_ids=["dev", "qa"],
    )
    legacy_realm.default_workspace_id = workspace.id
    legacy_realm.workspace_ids = [workspace.id]
    RealmStore().save(legacy_realm)
    board = BoardStore().ensure_default_board(workspace.id)
    instance = PersonaInstanceStore().update(
        PersonaInstance(
            id="personainst_preview_dev",
            persona_id="dev",
            role="dev",
            display_name="Dev",
            profile_id=None,
            runtime_root="runtime",
            state=WorkerSessionState.IDLE,
            realm_id=legacy_realm.id,
            workspace_id=workspace.id,
        )
    )
    actor = OfficeStore().upsert_actor(
        workspace.id,
        {
            "persona_id": "dev",
            "persona_instance_id": instance.id,
            "items": [
                {
                    "item_id": "dev",
                    "persona_id": "dev",
                    "kind": "agent",
                    "position": [1.0, 2.0],
                    "folder": "Agents",
                }
            ],
        },
    )

    before_realm = RealmStore().get(legacy_realm.id)
    before_workspace = WorkspaceStore().get(workspace.id)
    preview = preview_default_scope_migration()

    assert preview["dry_run"] is True
    assert preview["mutated"] is False
    assert preview["apply_supported"] is False
    assert preview["status"] == "legacy_adoption_ready"
    assert preview["proposed_default_scope"] == {
        "realm_id": legacy_realm.id,
        "workspace_id": workspace.id,
        "would_adopt_legacy_realm": True,
        "would_create_realm": False,
        "would_create_workspace": False,
    }
    scope = preview["candidate_scopes"][0]
    row = scope["workspaces"][0]
    assert row["roster_agent_ids"] == ["dev", "qa"]
    assert "goal_ids" not in row
    assert row["board_ids"] == [board.board_id]
    assert row["office_surface"] is True
    assert row["office_actor_keys"] == [actor.actor_key]
    assert row["office_persona_instance_ids"] == [instance.id]
    assert row["exact_persona_instance_ids"] == [instance.id]
    assert RealmStore().get(legacy_realm.id) == before_realm
    assert WorkspaceStore().get(workspace.id) == before_workspace
    assert DEFAULT_REALM_ID not in {item.id for item in RealmStore().list_all()}
    assert DEFAULT_WORKSPACE_ID not in {
        item.id for item in WorkspaceStore().list_all()
    }


def test_default_scope_reconciliation_keeps_legacy_and_archives_empty_fixed_duplicate(
    isolate_agent_runtime_root,
):
    legacy_realm = RealmStore().create(name="default")
    legacy_workspace = WorkspaceStore().create(
        name="default",
        realm_id=legacy_realm.id,
        agent_ids=["neko_supervisor", "dev"],
    )
    RealmStore().set_active(legacy_realm.id)
    WorkspaceStore().set_active(legacy_workspace.id)
    canonical = RealmStore().create(
        name="Default",
        realm_id=DEFAULT_REALM_ID,
        default_workspace_id=DEFAULT_WORKSPACE_ID,
    )
    canonical_workspace = WorkspaceStore().create(
        name="Default",
        workspace_id=DEFAULT_WORKSPACE_ID,
        realm_id=canonical.id,
        agent_ids=["backend_dev", "dev", "neko_supervisor", "qa"],
    )
    canonical.workspace_ids = [canonical_workspace.id]
    RealmStore().save(canonical)
    test_realm = RealmStore().create(name="test realm", server_id="server_test")

    result = reconcile_default_scope_to_legacy(
        winner_realm_id=legacy_realm.id,
        winner_workspace_id=legacy_workspace.id,
    )

    assert result["status"] == "applied"
    assert result["mutated"] is True
    assert Path(result["backup_dir"]).joinpath("manifest.json").exists()
    winner = RealmStore().get(legacy_realm.id)
    assert winner.archived is False
    assert winner.default_workspace_id == legacy_workspace.id
    assert winner.workspace_ids == [legacy_workspace.id]
    assert WorkspaceStore().get(legacy_workspace.id).agent_ids == [
        "neko_supervisor",
        "dev",
    ]
    assert RealmStore().get(DEFAULT_REALM_ID).archived is True
    assert WorkspaceStore().get(DEFAULT_WORKSPACE_ID).archived is True
    assert RealmStore().get(test_realm.id).archived is False
    assert RealmStore().active_id() == legacy_realm.id
    assert WorkspaceStore().active_id() == legacy_workspace.id

    adopted = ensure_default_scope()
    assert adopted.realm.id == legacy_realm.id
    assert adopted.workspace.id == legacy_workspace.id
    assert adopted.adopted_legacy_realm is True
    assert reconcile_default_scope_to_legacy(
        winner_realm_id=legacy_realm.id,
        winner_workspace_id=legacy_workspace.id,
    )["status"] == "already_applied"

    preview = preview_default_scope_migration()
    assert preview["status"] == "legacy_adoption_ready"
    assert preview["archived_default_like_realm_ids"] == [DEFAULT_REALM_ID]
    assert test_realm.id in preview["untouched_realm_ids"]


def test_default_scope_reconciliation_refuses_fixed_duplicate_with_scoped_data():
    legacy_realm = RealmStore().create(name="default")
    legacy_workspace = WorkspaceStore().create(
        name="default", realm_id=legacy_realm.id
    )
    RealmStore().set_active(legacy_realm.id)
    WorkspaceStore().set_active(legacy_workspace.id)
    canonical = RealmStore().create(
        name="Default",
        realm_id=DEFAULT_REALM_ID,
        default_workspace_id=DEFAULT_WORKSPACE_ID,
    )
    canonical_workspace = WorkspaceStore().create(
        name="Default",
        workspace_id=DEFAULT_WORKSPACE_ID,
        realm_id=canonical.id,
    )
    canonical.workspace_ids = [canonical_workspace.id]
    RealmStore().save(canonical)
    BoardStore().ensure_default_board(canonical_workspace.id)

    with pytest.raises(DefaultScopeReconciliationRequired):
        reconcile_default_scope_to_legacy(
            winner_realm_id=legacy_realm.id,
            winner_workspace_id=legacy_workspace.id,
        )

    assert RealmStore().get(DEFAULT_REALM_ID).archived is False
    assert WorkspaceStore().get(DEFAULT_WORKSPACE_ID).archived is False
