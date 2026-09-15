"""``hermes harness realm sync revert`` — the VERB, through the real argparse
tree.

The engine's decision table is proven in
``tests/agent_runtime/test_realm_revert.py``. What is proven HERE is the
operator/client contract: the verb is routed, it is ``--yes`` gated like its
publish/resolve neighbours, ``--dry-run`` needs no confirmation, and the
``--json`` envelope keeps the exact key set the launcher's Mission Control sheet
parses per row. A handler nothing routes to is a verb no operator can run, and a
row shape nobody pins is a wire contract that drifts on the next edit.
"""

from __future__ import annotations

import argparse
import json

import pytest

from utils import atomic_json_write

from agent_runtime import paths
from agent_runtime.office_store import OfficeStore
from agent_runtime.office_sync import update_office_baseline_after_sync
from agent_runtime.serde import to_jsonable
from agent_runtime.store import RealmStore, WorkspaceStore

#: The envelope keys the launcher reads. Additive growth is fine; a REMOVAL or
#: a rename is what this set is here to catch.
ENVELOPE_KEYS = {
    "schema_version",
    "kind",
    "id",
    "realm_id",
    "dry_run",
    "selection",
    "count",
    "reverted",
    "refused",
    "items",
    "store_drift_after",
    "sync_repo",
    "resolution",
}
ROW_KEYS = {"family", "container", "item_key", "kind", "outcome", "detail"}


@pytest.fixture(autouse=True)
def hermetic_runtime_root(tmp_path, monkeypatch):
    root = tmp_path / "agent-runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    resolved = paths.store_root().resolve()
    assert resolved == root.resolve() or root.resolve() in resolved.parents, (
        f"store_root() resolved to {resolved}, OUTSIDE {root}: this test archives "
        "real rows and would otherwise archive the OPERATOR's."
    )
    return root


def _dispatch(argv: list[str]) -> int:
    from hermes_cli import harness

    root = argparse.ArgumentParser(prog="hermes")
    harness.build_parser(root.add_subparsers(dest="command"))
    args = root.parse_args(argv)
    return args.func(args)


@pytest.fixture
def drifted_realm(tmp_path):
    """A realm whose office is published, whose subtree matches, and whose one
    actor was then archived locally — the 2026-08-31 live shape."""

    realm = RealmStore().create(name="Realm")
    ws = WorkspaceStore().create(name="WS", realm_id=realm.id)
    realm = RealmStore().get(realm.id)
    realm.workspace_ids.append(ws.id)
    realm.sync_manifest_ref = str(tmp_path / "sync_repo")
    RealmStore().save(realm)
    WorkspaceStore().set_active(ws.id)

    store = OfficeStore()
    store.upsert_actor(
        ws.id,
        {
            "persona_id": "dev",
            "items": [
                {"item_id": "dev", "persona_id": "dev", "kind": "agent", "position": [1.0, 2.0], "folder": "Agents"}
            ],
        },
    )
    update_office_baseline_after_sync(realm.id, [ws.id])
    subtree = tmp_path / "sync_repo" / "realms" / paths.safe_path_token(realm.id)
    office_dir = subtree / "store" / "office" / paths.safe_path_token(ws.id)
    atomic_json_write(office_dir / "office.json", to_jsonable(store.get_surface(ws.id)), indent=2, sort_keys=True)
    for actor in store.scan_actors(ws.id).actors:
        atomic_json_write(
            office_dir / "actors" / f"{actor.actor_key}.json", to_jsonable(actor), indent=2, sort_keys=True
        )
    store.remove_actor(ws.id, "dev")
    return realm.id, ws.id


def test_the_verb_is_routed_and_its_envelope_holds_its_shape(drifted_realm, capsys):
    realm_id, ws = drifted_realm

    code = _dispatch(["harness", "realm", "sync", "revert", realm_id, "--all", "--yes", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert payload["kind"] == "realm_sync_revert"
    assert set(payload) == ENVELOPE_KEYS
    assert payload["id"] == realm_id == payload["realm_id"]
    assert payload["selection"] == "all"
    assert payload["dry_run"] is False
    assert payload["count"] == payload["reverted"] == 2 and payload["refused"] == 0
    for row in payload["items"]:
        assert set(row) == ROW_KEYS
    assert {row["outcome"] for row in payload["items"]} == {
        "restored_from_upstream",
        "reverted_to_upstream",
    }
    assert payload["store_drift_after"]["office"]["actors_removed"] == 0
    assert OfficeStore().actor_exists(ws, "dev")


def test_a_single_item_is_addressed_by_its_status_spec(drifted_realm, capsys):
    """The launcher sends back the exact ``FAMILY:CONTAINER:KEY`` it read from
    ``store_drift.items`` — the round trip is the contract."""

    realm_id, ws = drifted_realm

    code = _dispatch(
        [
            "harness", "realm", "sync", "revert", realm_id,
            "--item", f"office_actor:{ws}:dev",
            "--yes", "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert payload["selection"] == "items"
    assert [row["item_key"] for row in payload["items"]] == ["dev"]
    assert payload["items"][0]["outcome"] == "restored_from_upstream"


def test_it_is_confirmation_gated_like_its_neighbours(drifted_realm, capsys):
    realm_id, _ws = drifted_realm

    code = _dispatch(["harness", "realm", "sync", "revert", realm_id, "--all", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 8
    assert payload["error"]["code"] == "confirmation_required"
    # …and nothing moved.
    assert not OfficeStore().actor_exists(_ws, "dev")


def test_a_dry_run_needs_no_confirmation_and_changes_nothing(drifted_realm, capsys):
    realm_id, ws = drifted_realm

    code = _dispatch(["harness", "realm", "sync", "revert", realm_id, "--all", "--dry-run", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert payload["dry_run"] is True and payload["reverted"] == 2
    assert not OfficeStore().actor_exists(ws, "dev")


def test_a_missing_clone_exits_on_the_precondition_family(drifted_realm, capsys, tmp_path):
    realm_id, _ws = drifted_realm
    realm = RealmStore().get(realm_id)
    realm.sync_manifest_ref = str(tmp_path / "nowhere")
    RealmStore().save(realm)

    code = _dispatch(["harness", "realm", "sync", "revert", realm_id, "--all", "--yes", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "sync_repo_missing"
    assert code == 6  # precondition family: the next move is `realm sync pull`
