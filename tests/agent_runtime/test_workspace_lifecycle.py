"""Workspace lifecycle: hard delete (cascade + guards + tombstone ledger)
and create-from-template content copy."""

import json
import subprocess
import sys

import pytest
from hermes_time import now

from agent_runtime import board_models, paths
from agent_runtime.board_store import BoardStore
from agent_runtime.errors import WorkspaceDeleteBlocked
from agent_runtime.events import EventLog
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.office_store import OfficeStore
from agent_runtime.realm_sync import _apply_workspace_tombstones, resolve_realm_sync_artifacts
from agent_runtime.states import TaskState
from agent_runtime.store import RealmStore, TaskStore, WorkspaceStore
from agent_runtime.workspace_template import copy_workspace_content, normalize_copy_scopes


def _seed_office_actor(workspace_id: str, persona_id: str = "dev") -> None:
    OfficeStore().upsert_actor(
        workspace_id,
        {
            "persona_id": persona_id,
            "items": [
                {"item_id": persona_id, "persona_id": persona_id, "kind": "agent", "position": [1.5, -2.0], "folder": "Agents"},
                {"item_id": f"{persona_id}_desk", "persona_id": persona_id, "kind": "desk", "position": [1.5, -1.0], "folder": "Desks"},
            ],
        },
    )


def _seed_board_card(workspace_id: str, title: str = "Template card") -> None:
    BoardStore().add_card(workspace_id=workspace_id, title=title, description="seeded", priority="p1")


def _task_for(workspace_id: str, task_id: str, state: TaskState = TaskState.CREATED) -> Task:
    ts = now()
    return Task(
        id=task_id,
        goal_id=task_id,
        workspace_id=workspace_id,
        title="Guard goal",
        description="Guard goal",
        state=state,
        created_at=ts,
        updated_at=ts,
        requested_by="test",
    )


def _run_harness(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "harness", *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


# ── delete ────────────────────────────────────────────────────────────────


def test_delete_cascades_office_board_and_records_tombstone():
    realm = RealmStore().create(name="Delete Realm")
    workspace = WorkspaceStore().create(name="Doomed", realm_id=realm.id)
    realm.workspace_ids.append(workspace.id)
    RealmStore().save(realm)
    _seed_office_actor(workspace.id)
    _seed_board_card(workspace.id)
    board_id = board_models.default_board_id(workspace.id)
    assert paths.office_dir(workspace.id).exists()
    assert paths.board_dir(board_id).exists()

    row = WorkspaceStore().delete(workspace.id)

    assert row == {"id": workspace.id, "name": "Doomed", "realm_id": realm.id, "deleted": True}
    assert not paths.workspace_path(workspace.id).exists()
    assert not paths.office_dir(workspace.id).exists()
    assert not paths.board_dir(board_id).exists()
    refreshed = RealmStore().get(realm.id)
    assert workspace.id not in refreshed.workspace_ids
    assert workspace.id in refreshed.deleted_workspace_ids
    types = [event.type for event in EventLog().tail(10)]
    assert "workspace.deleted" in types


def test_delete_clears_active_pointer():
    realm = RealmStore().create(name="Active Realm")
    workspace = WorkspaceStore().create(name="Active WS", realm_id=realm.id)
    WorkspaceStore().set_active(workspace.id)
    WorkspaceStore().delete(workspace.id)
    assert WorkspaceStore().active_id() is None


def test_delete_refuses_workspace_with_goals():
    workspace = WorkspaceStore().create(name="Busy")
    TaskStore().create(_task_for(workspace.id, "task_guard_1"))
    with pytest.raises(WorkspaceDeleteBlocked) as excinfo:
        WorkspaceStore().delete(workspace.id)
    assert excinfo.value.code == "workspace_has_goals"
    assert paths.workspace_path(workspace.id).exists()


def test_delete_refuses_server_realm_default_workspace():
    realm = RealmStore().create(name="Server Realm", server_id="srv_1")
    workspace = WorkspaceStore().create(name="Realm Default", realm_id=realm.id)
    realm.workspace_ids.append(workspace.id)
    realm.default_workspace_id = workspace.id
    RealmStore().save(realm)
    with pytest.raises(WorkspaceDeleteBlocked) as excinfo:
        WorkspaceStore().delete(workspace.id)
    assert excinfo.value.code == "realm_default_workspace"


def test_delete_clears_local_realm_default_pointer():
    realm = RealmStore().create(name="Local Realm")
    workspace = WorkspaceStore().create(name="Local Default", realm_id=realm.id)
    realm.workspace_ids.append(workspace.id)
    realm.default_workspace_id = workspace.id
    RealmStore().save(realm)
    WorkspaceStore().delete(workspace.id)
    assert RealmStore().get(realm.id).default_workspace_id is None


def test_cli_delete_requires_yes_and_deletes_with_yes():
    workspace = WorkspaceStore().create(name="CLI Doomed")
    refused = _run_harness("workspace", "delete", workspace.id, "--json")
    assert refused.returncode == 8
    assert paths.workspace_path(workspace.id).exists()

    deleted = _run_harness("workspace", "delete", workspace.id, "--yes", "--json")
    assert deleted.returncode == 0, deleted.stderr
    payload = json.loads(deleted.stdout)
    assert payload["deleted"] is True
    assert not paths.workspace_path(workspace.id).exists()


# ── template copy ─────────────────────────────────────────────────────────


def test_normalize_copy_scopes_defaults_to_all():
    assert normalize_copy_scopes(None) == ("office", "board", "agents", "settings")
    assert normalize_copy_scopes(["board", "board", "office"]) == ("board", "office")
    assert normalize_copy_scopes(["nonsense"]) == ("office", "board", "agents", "settings")


def test_copy_workspace_content_copies_office_and_board():
    source = WorkspaceStore().create(name="Template Source")
    dest = WorkspaceStore().create(name="Fresh Copy")
    OfficeStore().update_surface(source.id, folders=["Agents", "Desks", "QA Pit"])
    _seed_office_actor(source.id, persona_id="dev")
    _seed_office_actor(source.id, persona_id="qa")
    _seed_board_card(source.id, title="Card A")
    _seed_board_card(source.id, title="Card B")

    outcome = copy_workspace_content(source.id, dest.id, scopes=("office", "board"))

    assert outcome["warnings"] == []
    assert outcome["copied"] == {"office_actors": 2, "office_folders": 3, "board_cards": 2}
    dest_actors = OfficeStore().list_actors(dest.id)
    assert {actor.actor_key for actor in dest_actors} == {"dev", "qa"}
    dev = next(actor for actor in dest_actors if actor.actor_key == "dev")
    assert [item.position for item in dev.items] == [[1.5, -2.0], [1.5, -1.0]]
    assert OfficeStore().get_surface(dest.id).folders == ["Agents", "Desks", "QA Pit"]
    dest_cards = BoardStore().list_cards(board_models.default_board_id(dest.id))
    assert sorted(card.title for card in dest_cards) == ["Card A", "Card B"]
    # Source untouched.
    assert len(BoardStore().list_cards(board_models.default_board_id(source.id))) == 2


def test_cli_create_from_workspace_copies_scoped_content_and_settings():
    realm = RealmStore().create(name="Template Realm")
    source = WorkspaceStore().create(
        name="Golden Template", realm_id=realm.id, isolation="hard", max_concurrent_lanes=3
    )
    realm.workspace_ids.append(source.id)
    RealmStore().save(realm)
    _seed_office_actor(source.id)
    _seed_board_card(source.id)

    created = _run_harness(
        "workspace", "create", "--name", "From Template", "--from-workspace", source.id, "--json"
    )
    assert created.returncode == 0, created.stderr
    row = json.loads(created.stdout)
    assert row["template_workspace_id"] == source.id
    assert set(row["copy_scopes"]) == {"office", "board", "agents", "settings"}
    assert row["copied"] == {"office_actors": 1, "office_folders": 2, "board_cards": 1}
    created_ws = WorkspaceStore().get(row["id"])
    assert created_ws.isolation == "hard"
    assert created_ws.max_concurrent_lanes == 3
    assert OfficeStore().list_actors(created_ws.id)
    assert BoardStore().list_cards(board_models.default_board_id(created_ws.id))


def test_cli_create_blank_stays_blank():
    created = _run_harness("workspace", "create", "--name", "Truly Blank", "--json")
    assert created.returncode == 0, created.stderr
    row = json.loads(created.stdout)
    assert "template_workspace_id" not in row
    assert not OfficeStore().surface_exists(row["id"])
    assert not BoardStore().exists(board_models.default_board_id(row["id"]))


def test_cli_copy_without_from_workspace_is_rejected():
    result = _run_harness("workspace", "create", "--name", "Bad Copy", "--copy", "office", "--json")
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "invalid_request"


# ── realm-sync tombstones ─────────────────────────────────────────────────


def test_resolve_artifacts_excludes_tombstoned_workspaces():
    realm = RealmStore().create(name="Sync Realm")
    keep = WorkspaceStore().create(name="Keep", realm_id=realm.id)
    dead = WorkspaceStore().create(name="Dead", realm_id=realm.id)
    realm.workspace_ids.extend([keep.id, dead.id])
    realm.deleted_workspace_ids = [dead.id]
    RealmStore().save(realm)

    artifacts = resolve_realm_sync_artifacts(realm.id)
    workspace_paths = [artifact.relative_path for artifact in artifacts if artifact.kind == "workspace"]
    assert any(keep.id in path for path in workspace_paths)
    assert not any(dead.id in path for path in workspace_paths)


def test_apply_workspace_tombstones_deletes_local_copy():
    realm = RealmStore().create(name="Tombstone Realm")
    dead = WorkspaceStore().create(name="Deleted Elsewhere", realm_id=realm.id)
    _seed_office_actor(dead.id)
    realm.workspace_ids.append(dead.id)
    realm.deleted_workspace_ids = [dead.id]
    RealmStore().save(realm)

    summary = _apply_workspace_tombstones(realm.id)

    assert summary["deleted"] == [dead.id]
    assert summary["archived"] == []
    assert not paths.workspace_path(dead.id).exists()
    assert not paths.office_dir(dead.id).exists()


def test_apply_workspace_tombstones_archives_when_goals_survive_locally():
    realm = RealmStore().create(name="Tombstone Realm 2")
    dead = WorkspaceStore().create(name="Has Local Goals", realm_id=realm.id)
    TaskStore().create(_task_for(dead.id, "task_local_evidence"))
    realm.workspace_ids.append(dead.id)
    realm.deleted_workspace_ids = [dead.id]
    RealmStore().save(realm)

    summary = _apply_workspace_tombstones(realm.id)

    assert summary["deleted"] == []
    assert summary["archived"] == [dead.id]
    assert paths.workspace_path(dead.id).exists()
    assert WorkspaceStore().get(dead.id).archived is True
    assert summary["warnings"] and summary["warnings"][0]["code"] == "workspace_tombstone_archived"
