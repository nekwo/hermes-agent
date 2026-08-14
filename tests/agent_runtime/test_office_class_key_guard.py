"""The class→instance re-key migration's durability fence
(``agent_runtime/office_class_key_guard.py``) at its two remaining writers.

The migration (``scripts/office_actor_rekey_to_instance.py``) re-keys every live
placement from its persona CLASS key to its persona-INSTANCE key and archives
the old key. ``OfficeStore.upsert_actor`` then treats an explicit upsert of an
archived key as intent to re-add and CLEARS the resurrection guard, so ONE
surviving class-keyed write silently undoes the migration and leaves the agent
placed twice — with no conflict warning, because the two actor keys are
different strings and every store guard keys on the actor key.

Two writers could still do that:

- ``agent_runtime/workspace_template.py`` — the create-from-template copy.
- ``hermes_cli/harness_parts/office.py`` — ``harness office actor-upsert``,
  which is ALSO the launcher's save path (the Flutter bridge shells out to it).

The tests below pin, for each writer, that the hazardous write is refused and
that the LEGITIMATE class-keyed write — a template landing on a workspace with
no office of its own, an operator placing a class-keyed actor on a clean
canvas — is completely untouched. A fence that also blocks the normal path
would just get deleted.

The flagship case runs the REAL migration script with ``--apply`` first, so the
fence is proven against the actual on-disk state the migration produces rather
than against a hand-built imitation of it.
"""

from __future__ import annotations

import json
import subprocess
import sys

from agent_runtime.office_store import OfficeStore
from agent_runtime.store import WorkspaceStore

INSTANCE = "personainst_backend_dev_agent_29fdd71a"


def _run_harness(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "harness", *args],
        capture_output=True,
        text=True,
        timeout=90,
    )


def _instance_file(persona_instance_id: str = INSTANCE) -> None:
    from agent_runtime import paths

    path = paths.persona_instance_path(persona_instance_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": persona_instance_id}), encoding="utf-8")


def _items(persona_id: str, *, agent_item_id: str | None = None) -> list[dict]:
    return [
        {"item_id": f"desk-{persona_id}", "kind": "desk", "position": [1.0, 2.0], "folder": "Desks"},
        {"item_id": agent_item_id or persona_id, "kind": "agent", "position": [1.0, 3.0], "folder": "Agents"},
    ]


def _payload(persona_id: str, *, instance: str | None = None, agent_item_id: str | None = None) -> dict:
    payload: dict = {"persona_id": persona_id, "items": _items(persona_id, agent_item_id=agent_item_id)}
    if instance:
        payload["persona_instance_id"] = instance
    return payload


def _workspace(name: str) -> str:
    return WorkspaceStore().create(name=name).id


def _seed_class_keyed(workspace_id: str, persona_id: str = "backend_dev") -> None:
    """A pre-migration placement: no binding, agent item id IS the instance id
    (the derivation the migration script relies on)."""

    OfficeStore().upsert_actor(
        workspace_id,
        _payload(persona_id, agent_item_id=INSTANCE if persona_id == "backend_dev" else None),
    )


def _migrate(workspace_id: str) -> None:
    """Run the real migration over one workspace."""

    from scripts.office_actor_rekey_to_instance import main

    _instance_file()
    assert main(["--apply", "--workspace", workspace_id]) == 0


def _keys(workspace_id: str) -> set[str]:
    return {actor.actor_key for actor in OfficeStore().list_actors(workspace_id)}


# ── writer 1: workspace_template ──────────────────────────────────────────


def test_template_copy_of_class_keyed_actors_onto_a_fresh_workspace_is_untouched():
    """The legitimate path. A workspace with no office cannot collide with
    anything, so nothing is refused and nothing is warned about."""

    from agent_runtime.workspace_template import copy_workspace_content

    source = _workspace("Class Template")
    _seed_class_keyed(source, "backend_dev")
    _seed_class_keyed(source, "qa")
    dest = _workspace("Fresh Dest")

    outcome = copy_workspace_content(source, dest, scopes=("office",))

    assert outcome["warnings"] == []
    assert outcome["copied"]["office_actors"] == 2
    assert _keys(dest) == {"backend_dev", "qa"}


def test_template_copy_threads_the_instance_binding_through():
    """Already true before this change and worth pinning: a BOUND source actor
    copies as a bound payload, so the destination store mints the instance key
    rather than the class key."""

    from agent_runtime.workspace_template import copy_workspace_content

    source = _workspace("Bound Template")
    OfficeStore().upsert_actor(source, _payload("backend_dev", instance=INSTANCE))
    dest = _workspace("Bound Dest")

    outcome = copy_workspace_content(source, dest, scopes=("office",))

    assert outcome["warnings"] == []
    assert _keys(dest) == {INSTANCE}
    assert OfficeStore().get_actor(dest, INSTANCE).persona_instance_id == INSTANCE


def test_template_copy_refuses_a_class_keyed_actor_that_would_double_place():
    """Destination already holds the persona under its INSTANCE key, holding
    the same canvas items. Copying the source's class-keyed placement would put
    both item ids on two actor files at once."""

    from agent_runtime.workspace_template import copy_workspace_content

    source = _workspace("Stale Template")
    _seed_class_keyed(source, "backend_dev")
    dest = _workspace("Migrated Dest")
    OfficeStore().upsert_actor(dest, _payload("backend_dev", instance=INSTANCE, agent_item_id=INSTANCE))

    outcome = copy_workspace_content(source, dest, scopes=("office",))

    assert outcome["copied"]["office_actors"] == 0
    assert _keys(dest) == {INSTANCE}, "the class key must not have been created"
    [warning] = outcome["warnings"]
    assert warning["code"] == "office_actor_class_key_refused"
    assert warning["reasons"] == ["duplicate_item_placement"]
    assert warning["conflicting_actor_keys"] == [INSTANCE]
    assert INSTANCE in warning["message"], "a refusal that does not name the conflict is unactionable"


def test_template_copy_refuses_a_class_key_the_destination_has_archived():
    """The resurrection-guard case, against real post-migration state: the
    class key sits in ``archived_actor_keys``, and an upsert of it would CLEAR
    that entry (office_store.py:344-351). The ledger must survive the copy."""

    from agent_runtime.workspace_template import copy_workspace_content

    source = _workspace("Stale Template")
    _seed_class_keyed(source, "backend_dev")
    dest = _workspace("Migrated Dest")
    _seed_class_keyed(dest, "backend_dev")
    _migrate(dest)
    assert "backend_dev" in OfficeStore().get_surface(dest).archived_actor_keys

    outcome = copy_workspace_content(source, dest, scopes=("office",))

    assert outcome["copied"]["office_actors"] == 0
    assert _keys(dest) == {INSTANCE}
    [warning] = outcome["warnings"]
    assert warning["code"] == "office_actor_class_key_refused"
    assert "resurrects_archived_class_key" in warning["reasons"]
    assert "backend_dev" in OfficeStore().get_surface(dest).archived_actor_keys, (
        "the resurrection guard was cleared — the migration is undone"
    )


def test_template_copy_orders_bound_source_actors_first():
    """A source that itself carries a bound AND an unbound placement of one
    persona lands as the bound copy plus a named refusal. ``list_actors`` sorts
    by actor_key, which would otherwise copy the bare class key first (because
    "backend_dev" < "personainst_…") and let both through."""

    from agent_runtime.workspace_template import copy_workspace_content

    source = _workspace("Double Template")
    OfficeStore().upsert_actor(source, _payload("backend_dev", agent_item_id=INSTANCE))
    OfficeStore().upsert_actor(source, _payload("backend_dev", instance=INSTANCE, agent_item_id=INSTANCE))
    assert _keys(source) == {"backend_dev", INSTANCE}
    dest = _workspace("Repaired Dest")

    outcome = copy_workspace_content(source, dest, scopes=("office",))

    assert _keys(dest) == {INSTANCE}
    assert outcome["copied"]["office_actors"] == 1
    assert [w["code"] for w in outcome["warnings"]] == ["office_actor_class_key_refused"]


# ── writer 2: harness office actor-upsert ─────────────────────────────────


def test_cli_class_keyed_upsert_onto_a_clean_workspace_still_works():
    """The legitimate path for the operator tool. Class-keyed placements are a
    supported shape; nothing here is refused."""

    workspace = _workspace("Clean Canvas")
    result = _run_harness(
        "office", "actor-upsert", "--workspace", workspace,
        "--actor-json", json.dumps(_payload("backend_dev")), "--json",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    row = json.loads(result.stdout)
    assert row["id"] == "backend_dev"
    assert "warnings" not in row


def test_cli_refuses_the_class_keyed_write_that_would_undo_the_migration():
    """THE scenario. Migrate for real, then replay exactly what the launcher
    used to send — a class-keyed save with the same items. Without the guard
    this returns 0 and leaves ``backend_dev`` ACTIVE beside the instance-keyed
    actor, the ledger cleared, and no warning anywhere."""

    workspace = _workspace("Migrated Office")
    _seed_class_keyed(workspace, "backend_dev")
    _migrate(workspace)
    assert _keys(workspace) == {INSTANCE}

    result = _run_harness(
        "office", "actor-upsert", "--workspace", workspace,
        "--actor-json", json.dumps(_payload("backend_dev", agent_item_id=INSTANCE)), "--json",
    )

    assert result.returncode == 4, result.stdout + result.stderr
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "duplicate_conflict"
    assert "resurrects_archived_class_key" in error["message"]
    assert "duplicate_item_placement" in error["message"]
    assert INSTANCE in error["message"]
    # Nothing was written: no second placement, ledger intact.
    assert _keys(workspace) == {INSTANCE}
    assert "backend_dev" in OfficeStore().get_surface(workspace).archived_actor_keys


def test_cli_dry_run_surfaces_the_refusal_instead_of_previewing_the_write():
    """A --dry-run whose whole job is to show what a write would do must show
    the refusal too, or the operator learns about it only from the real run."""

    workspace = _workspace("Migrated Office")
    _seed_class_keyed(workspace, "backend_dev")
    _migrate(workspace)

    result = _run_harness(
        "office", "actor-upsert", "--workspace", workspace,
        "--actor-json", json.dumps(_payload("backend_dev", agent_item_id=INSTANCE)),
        "--dry-run", "--json",
    )
    assert result.returncode == 4, result.stdout + result.stderr
    assert json.loads(result.stdout)["error"]["code"] == "duplicate_conflict"


def test_cli_persona_instance_id_flag_threads_the_binding_and_clears_the_refusal():
    """The convenience half of the answer: the same class-keyed JSON, plus the
    flag, becomes an instance-keyed write — no refusal, no new class key."""

    workspace = _workspace("Migrated Office")
    _seed_class_keyed(workspace, "backend_dev")
    _migrate(workspace)

    result = _run_harness(
        "office", "actor-upsert", "--workspace", workspace,
        "--actor-json", json.dumps(_payload("backend_dev", agent_item_id=INSTANCE)),
        "--persona-instance-id", INSTANCE, "--json",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    row = json.loads(result.stdout)
    assert row["id"] == INSTANCE
    assert row["persona_instance_id"] == INSTANCE
    assert _keys(workspace) == {INSTANCE}


def test_cli_allow_class_key_forces_the_write_and_puts_the_override_on_the_record():
    """The escape hatch. It really writes — and it warns, so the double
    placement is a recorded operator decision rather than an invisible one."""

    workspace = _workspace("Migrated Office")
    _seed_class_keyed(workspace, "backend_dev")
    _migrate(workspace)

    result = _run_harness(
        "office", "actor-upsert", "--workspace", workspace,
        "--actor-json", json.dumps(_payload("backend_dev", agent_item_id=INSTANCE)),
        "--allow-class-key", "--json",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    row = json.loads(result.stdout)
    assert [w["code"] for w in row["warnings"]] == ["office_actor_class_key_forced"]
    assert row["warnings"][0]["conflicting_actor_keys"] == [INSTANCE]
    assert _keys(workspace) == {"backend_dev", INSTANCE}


def test_cli_idempotent_resave_of_a_live_class_keyed_actor_is_not_refused():
    """An actor that is legitimately class-keyed today gets re-saved on every
    launcher canvas edit. The guard must not treat its own key as a conflict."""

    workspace = _workspace("Unmigrated Office")
    _seed_class_keyed(workspace, "backend_dev")

    result = _run_harness(
        "office", "actor-upsert", "--workspace", workspace,
        "--actor-json", json.dumps(_payload("backend_dev", agent_item_id=INSTANCE)), "--json",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["revision"] == 2
    assert _keys(workspace) == {"backend_dev"}


def test_cli_class_keyed_write_is_allowed_once_the_instance_sibling_is_archived():
    """Only ACTIVE placements can be double-placed. Once the instance-keyed
    actor is archived, the canvas holds nothing to collide with and the
    class-keyed write must go through.

    Found by mutation: switching the guard's scan to
    ``list_actors(include_archived=True)`` left every other test green.
    """

    workspace = _workspace("Emptied Office")
    OfficeStore().upsert_actor(workspace, _payload("backend_dev", instance=INSTANCE, agent_item_id=INSTANCE))
    OfficeStore().remove_actor(workspace, INSTANCE)
    assert _keys(workspace) == set()

    result = _run_harness(
        "office", "actor-upsert", "--workspace", workspace,
        "--actor-json", json.dumps(_payload("backend_dev", agent_item_id=INSTANCE)), "--json",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _keys(workspace) == {"backend_dev"}


def test_guard_normalizes_a_stored_persona_id_it_did_not_write():
    """Actor files do not all come from ``upsert_actor``. ``resolve_conflict``
    with ``take=remote`` deserializes a PEER's actor straight to disk
    (``office_store.py:458-466``), so a stored ``persona_id`` can carry casing
    the local normalizer would have stripped. The guard must still match it.

    Found by mutation: comparing ``actor.persona_id`` raw instead of through
    ``_normalize_persona_id`` left every other test green, because every actor
    the tests wrote had been normalized on the way in.
    """

    from agent_runtime import paths
    from agent_runtime.office_class_key_guard import class_key_collision

    workspace = _workspace("Synced Office")
    OfficeStore().upsert_actor(workspace, _payload("backend_dev", instance=INSTANCE, agent_item_id=INSTANCE))
    # Exactly what a take=remote adoption drops: the peer's spelling, verbatim.
    path = paths.office_actor_path(workspace, INSTANCE)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["persona_id"] = "Backend_Dev"
    path.write_text(json.dumps(body), encoding="utf-8")

    collision = class_key_collision(
        OfficeStore(), workspace, _payload("backend_dev", agent_item_id=INSTANCE)
    )
    assert collision is not None, "an un-normalized stored persona_id slipped past the guard"
    assert collision["conflicting_actor_keys"] == [INSTANCE]


def test_guard_normalizes_incoming_item_ids_the_way_the_store_will():
    """``_normalize_item`` runs every incoming ``item_id`` through ``_safe_id``,
    so "desk backend_dev" is STORED as "desk_backend_dev". A guard that
    compared the raw token would wave through a payload that the store then
    writes right on top of the instance-keyed actor's item.

    Found by mutation: dropping ``_safe_id`` from the guard left every other
    test green, because every other payload used already-safe item ids.
    """

    from agent_runtime.office_class_key_guard import class_key_collision

    workspace = _workspace("Sloppy Payload Office")
    OfficeStore().upsert_actor(
        workspace,
        {
            "persona_id": "backend_dev",
            "persona_instance_id": INSTANCE,
            "items": [{"item_id": "desk backend_dev", "kind": "desk", "position": [1.0, 2.0]}],
        },
    )
    assert [i.item_id for i in OfficeStore().get_actor(workspace, INSTANCE).items] == ["desk_backend_dev"]

    collision = class_key_collision(
        OfficeStore(),
        workspace,
        {
            "persona_id": "backend_dev",
            "items": [{"item_id": "desk backend_dev", "kind": "desk", "position": [5.0, 5.0]}],
        },
    )
    assert collision is not None, "a raw item id slipped past the guard the store would have normalized"
    assert collision["reasons"] == ["duplicate_item_placement"]


def test_cli_unbound_placement_sharing_a_persona_but_not_items_is_allowed():
    """The guard is narrowed to item-id overlap on purpose. A genuinely
    separate unbound placement of the same persona class is a legal canvas and
    must stay writable — otherwise the fence outlaws a supported shape."""

    workspace = _workspace("Mixed Office")
    OfficeStore().upsert_actor(workspace, _payload("backend_dev", instance=INSTANCE, agent_item_id=INSTANCE))

    result = _run_harness(
        "office", "actor-upsert", "--workspace", workspace,
        "--actor-json", json.dumps(
            {
                "persona_id": "backend_dev",
                "items": [{"item_id": "spare-desk-2", "kind": "desk", "position": [9.0, 9.0], "folder": "Desks"}],
            }
        ),
        "--json",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _keys(workspace) == {"backend_dev", INSTANCE}
