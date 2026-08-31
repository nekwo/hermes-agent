"""The class→instance re-key migration's durability fence
(``agent_runtime/office_class_key_guard.py``) at its three remaining writers.

The migration (``scripts/office_actor_rekey_to_instance.py``) re-keys every live
placement from its persona CLASS key to its persona-INSTANCE key and archives
the old key. ``OfficeStore.upsert_actor`` then treats an explicit upsert of an
archived key as intent to re-add and CLEARS the resurrection guard, so ONE
surviving class-keyed write silently undoes the migration and leaves the agent
placed twice — with no conflict warning, because the two actor keys are
different strings and every store guard keys on the actor key.

Three writers could still do that:

- ``agent_runtime/workspace_template.py`` — the create-from-template copy.
- ``hermes_cli/harness_parts/office.py`` — ``harness office actor-upsert``,
  which is ALSO the launcher's save path (the Flutter bridge shells out to it).
- ``OfficeStore.resolve_conflict(take="remote")`` — which does not go through
  ``upsert_actor`` at all: it deserializes a PEER's actor from the conflict
  sidecar and calls ``_write_actor`` directly, so no caller-side guard is even
  reachable. Its fence therefore lives in the store
  (``OfficeStore._guard_class_keyed_adoption``), and its escape hatch is
  ``harness office resolve-conflict --allow-class-key``.

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


def _instance_file(persona_instance_id: str = INSTANCE, *, persona_id: str = "backend_dev") -> None:
    """A REAL persona-instance row for the migration to find.

    This used to write ``{"id": ...}`` — enough for the migration script, which
    only checks that the file EXISTS, but not a row any production writer can
    produce: ``PersonaInstanceStore._write`` always serializes a whole
    ``PersonaInstance``. The stub therefore sat in the store as a row that JSON
    -decodes and then fails model construction, which every reader silently
    skipped.

    ML-15 made that visible: the persona-lane write arms now refuse when a row
    will not decode, so a fixture carrying an undecodable row started failing a
    rollback that has nothing to do with what these tests are about. Writing the
    row the way the store writes it removes an unreality from the fixture rather
    than working around the fence — the migration still finds the file, and the
    guard's subject (class-keyed placement refusal) is untouched.
    """

    from agent_runtime import paths
    from agent_runtime.models import PersonaInstance
    from agent_runtime.serde import to_jsonable
    from agent_runtime.states import WorkerSessionState

    instance = PersonaInstance(
        id=persona_instance_id,
        persona_id=persona_id,
        role=persona_id,
        display_name=persona_id.replace("_", " ").title(),
        profile_id=persona_id,
        runtime_root="runtime",
        state=WorkerSessionState.IDLE,
    )
    path = paths.persona_instance_path(persona_instance_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(instance)), encoding="utf-8")


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
    return {actor.actor_key for actor in OfficeStore().scan_actors(workspace_id).actors}


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
    persona lands as the bound copy plus a named refusal. ``scan_actors`` sorts
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
    placement is a recorded operator decision rather than an invisible one.

    Since D1 it takes BOTH flags, because the migration archived ``backend_dev``
    and so this write is two overrides at once: a class-keyed shape AND the
    raising of a deleted key. Each puts its own warning on the record, which is
    the property worth having — an operator reading the receipt can tell which
    of the two they consented to.
    """

    workspace = _workspace("Migrated Office")
    _seed_class_keyed(workspace, "backend_dev")
    _migrate(workspace)

    result = _run_harness(
        "office", "actor-upsert", "--workspace", workspace,
        "--actor-json", json.dumps(_payload("backend_dev", agent_item_id=INSTANCE)),
        "--allow-class-key", "--resurrect", "--json",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    row = json.loads(result.stdout)
    assert [w["code"] for w in row["warnings"]] == [
        "office_actor_class_key_forced",
        "office_actor_resurrect_forced",
    ]
    assert row["warnings"][0]["conflicting_actor_keys"] == [INSTANCE]
    assert _keys(workspace) == {"backend_dev", INSTANCE}


def test_cli_allow_class_key_alone_no_longer_raises_a_deleted_key():
    """The two consents do not stand in for each other (D1).

    ``--allow-class-key`` used to be enough to re-add an archived class key,
    which meant an operator consenting to a KEY SHAPE also, silently, consented
    to clearing a tombstone. Now the second override has to be typed, and the
    refusal names it.
    """

    workspace = _workspace("Migrated Office")
    _seed_class_keyed(workspace, "backend_dev")
    _migrate(workspace)

    result = _run_harness(
        "office", "actor-upsert", "--workspace", workspace,
        "--actor-json", json.dumps(_payload("backend_dev", agent_item_id=INSTANCE)),
        "--allow-class-key", "--json",
    )
    assert result.returncode != 0, result.stdout + result.stderr
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "actor_archived"
    assert "--resurrect" in error["message"]
    # Nothing was written, and the tombstone the operator did not consent to
    # clearing is still there.
    assert _keys(workspace) == {INSTANCE}
    assert "backend_dev" in OfficeStore().get_surface(workspace).archived_actor_keys


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
    ``scan_actors(include_archived=True).actors`` left every other test green.
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
    must stay writable — otherwise the fence outlaws a supported shape.

    The spare item is an AGENT, and that is load-bearing rather than incidental.
    It used to be a second DESK, which the class-key guard correctly waved
    through (different item id, no migration undone) and which the store's desk
    fence now refuses under a different rule — one persona, one live desk
    (``OfficeStore._guard_duplicate_desk``, D6). Two fences, two questions: this
    test asks the class-key one, so its fixture must not also trip the other, or
    a green here would stop meaning what the docstring says. The companion
    assertion below pins the new boundary so the two rules are stated together
    instead of one silently shadowing the other — and an agent item is also what
    the launcher actually emits for an unbound group, since its drop authors no
    desk at all.
    """

    workspace = _workspace("Mixed Office")
    OfficeStore().upsert_actor(workspace, _payload("backend_dev", instance=INSTANCE, agent_item_id=INSTANCE))

    result = _run_harness(
        "office", "actor-upsert", "--workspace", workspace,
        "--actor-json", json.dumps(
            {
                "persona_id": "backend_dev",
                "items": [{"item_id": "spare-agent-2", "kind": "agent", "position": [9.0, 9.0], "folder": "Agents"}],
            }
        ),
        "--json",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _keys(workspace) == {"backend_dev", INSTANCE}


def test_the_same_write_carrying_a_second_desk_is_refused_by_the_other_fence():
    """The boundary between the two fences, stated once.

    Byte-for-byte the write above with ``kind`` flipped to ``desk`` and an id to
    match. The class-key guard still has nothing to say — no archived key, no
    overlapping item id — so a pass here would mean the desk rule does not exist
    on this lane. Exit 4 with ``duplicate_desk`` is what says it does, and
    naming a DIFFERENT code from ``duplicate_conflict`` is what stops the two
    rules from being read as one.
    """

    workspace = _workspace("Second Desk Office")
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
    assert result.returncode == 4, result.stdout + result.stderr
    envelope = json.loads(result.stdout)
    assert envelope["error"]["code"] == "duplicate_desk"
    assert "already holds 'desk-backend_dev'" in envelope["error"]["message"]
    # Refused before any write: the class-keyed actor never appeared.
    assert _keys(workspace) == {INSTANCE}


# -- writer 3: OfficeStore.resolve_conflict(take="remote") -----------------
#
# Different shape from the two above, so the fence is a different shape too.
# ``upsert_actor`` takes an OPERATOR-authored payload, which is why its guard
# sits at the callers; ``resolve_conflict`` takes a PEER-authored actor and
# writes it with ``_write_actor``, bypassing ``upsert_actor`` entirely. The
# guard therefore sits inside the store method, where a future second caller
# inherits it instead of having to remember it.
#
# ``take=remote`` on a migrated class key is strictly WORSE than the unfenced
# upsert it mirrors: ``upsert_actor`` at least clears the resurrection ledger
# and deletes the archive copy, so the state it leaves is merely wrong.
# ``resolve_conflict`` writes the active class-keyed file and touches NEITHER,
# leaving an ACTIVE actor whose key is simultaneously listed as archived.


def _seed_remote_conflict(
    workspace_id: str,
    actor_key: str,
    *,
    persona_id: str,
    item_ids: list[str],
    instance: str | None = None,
) -> None:
    """Drop a realm-sync conflict sidecar carrying a peer's actor.

    Written through the REAL sidecar writer (``office_sync._write_conflict_sidecar``)
    so the bytes ``resolve_conflict`` reads are the bytes a pull produces.
    """

    from agent_runtime.models import OfficeActor, OfficeItem
    from agent_runtime.office_sync import _write_conflict_sidecar

    remote = OfficeActor(
        actor_key=actor_key,
        workspace_id=workspace_id,
        persona_id=persona_id,
        persona_instance_id=instance,
        items=[
            OfficeItem(item_id=item_id, persona_id=persona_id, kind="agent", position=[9.0, 9.0], folder="Agents")
            for item_id in item_ids
        ],
        revision=7,
    )
    _write_conflict_sidecar(
        workspace_id,
        actor_key,
        kind="archive_vs_edit",
        remote_actor=remote,
        local_hash=None,
        remote_hash="remote-hash",
    )


def _migrated_workspace(name: str = "Migrated Sync Office") -> tuple[str, list[str]]:
    """A workspace in exactly the state the applied migration leaves behind,
    plus the item ids the surviving instance-keyed actor now owns."""

    workspace = _workspace(name)
    _seed_class_keyed(workspace, "backend_dev")
    _migrate(workspace)
    assert _keys(workspace) == {INSTANCE}
    assert "backend_dev" in OfficeStore().get_surface(workspace).archived_actor_keys
    return workspace, [item.item_id for item in OfficeStore().get_actor(workspace, INSTANCE).items]


def test_resolve_conflict_take_remote_refuses_the_archived_class_key():
    """THE scenario the migration script named as the one operator action it
    makes dangerous. A peer that never migrated republishes ``backend_dev``;
    the resurrection ledger turns that into a conflict sidecar (``sync_merge``:
    archived-vs-edit), and ``--take remote`` used to write the class key back
    ACTIVE beside the instance-keyed actor.

    Both reasons fire here on purpose - this is real post-migration state, and
    the peer's copy carries the same item ids the instance-keyed actor holds.
    """

    from agent_runtime import paths
    from agent_runtime.office_class_key_guard import ClassKeyedPlacementRefused

    workspace, item_ids = _migrated_workspace()
    _seed_remote_conflict(workspace, "backend_dev", persona_id="backend_dev", item_ids=item_ids)

    store = OfficeStore()
    try:
        store.resolve_conflict(workspace, "backend_dev", take="remote")
    except ClassKeyedPlacementRefused as exc:
        message = str(exc)
        details = exc.safe_details
    else:
        raise AssertionError("take=remote adopted a class-keyed peer actor over the migration")

    assert sorted(details["reasons"]) == ["duplicate_item_placement", "resurrects_archived_class_key"]
    assert details["conflicting_actor_keys"] == [INSTANCE]
    assert details["take"] == "remote"
    assert "--take local" in message, "the refusal must name the exit an operator can actually take"
    # Nothing written, nothing resolved: one placement, ledger intact, and the
    # sidecar still there so --take local remains available.
    assert _keys(workspace) == {INSTANCE}
    assert "backend_dev" in store.get_surface(workspace).archived_actor_keys
    assert paths.office_conflict_path(workspace, "backend_dev").exists(), (
        "a refused resolution must not consume the conflict it refused"
    )


def test_resolve_conflict_take_local_still_resolves_the_migrated_conflict():
    """The documented safe exit, now the one the refusal points at. It must
    actually work, or the fence is a dead end rather than a fence."""

    from agent_runtime import paths

    workspace, item_ids = _migrated_workspace()
    _seed_remote_conflict(workspace, "backend_dev", persona_id="backend_dev", item_ids=item_ids)

    resolved = OfficeStore().resolve_conflict(workspace, "backend_dev", take="local")

    assert resolved is None, "there is no local class-keyed actor - the migration archived it"
    assert not paths.office_conflict_path(workspace, "backend_dev").exists()
    assert _keys(workspace) == {INSTANCE}
    assert "backend_dev" in OfficeStore().get_surface(workspace).archived_actor_keys


def test_resolve_conflict_take_remote_adopts_an_instance_keyed_peer_on_a_migrated_office():
    """The legitimate path, run against the exact state the fence guards. An
    instance-keyed remote IS the migration's shape and can never undo it, so a
    fence that fires here would break realm sync for every migrated office."""

    workspace, item_ids = _migrated_workspace()
    _seed_remote_conflict(workspace, INSTANCE, persona_id="backend_dev", item_ids=item_ids, instance=INSTANCE)

    resolved = OfficeStore().resolve_conflict(workspace, INSTANCE, take="remote")

    assert resolved is not None
    assert resolved.actor_key == INSTANCE
    assert resolved.items[0].position == [9.0, 9.0], "the peer's copy really was adopted"
    assert _keys(workspace) == {INSTANCE}


def test_resolve_conflict_take_remote_adopts_a_class_keyed_peer_with_no_archived_sibling():
    """The other legitimate path. Class-keyed placements are a supported shape
    (``archive_actors_for_instance``: they survive instance churn by design), so
    a class-keyed remote onto an office that never migrated must still resolve.
    This is the shape ``test_office_sync``'s take-remote case relies on."""

    workspace = _workspace("Unmigrated Sync Office")
    _seed_class_keyed(workspace, "dev")
    _seed_remote_conflict(workspace, "dev", persona_id="dev", item_ids=["dev"])

    resolved = OfficeStore().resolve_conflict(workspace, "dev", take="remote")

    assert resolved is not None and resolved.actor_key == "dev"
    assert resolved.items[0].position == [9.0, 9.0]
    assert _keys(workspace) == {"dev"}


def test_resolve_conflict_take_remote_refuses_a_duplicate_placement_with_no_archived_key():
    """The second reason, isolated. The class key was never archived here - the
    office was authored instance-keyed from the start and a peer published an
    unbound placement of the same persona holding the SAME canvas items.

    Kept separate from the flagship because that one fires both reasons at once:
    a fence that only ever consulted the archive ledger would pass it.
    """

    from agent_runtime.office_class_key_guard import ClassKeyedPlacementRefused

    workspace = _workspace("Duplicated Sync Office")
    OfficeStore().upsert_actor(workspace, _payload("backend_dev", instance=INSTANCE, agent_item_id=INSTANCE))
    assert "backend_dev" not in OfficeStore().get_surface(workspace).archived_actor_keys
    _seed_remote_conflict(workspace, "backend_dev", persona_id="backend_dev", item_ids=[INSTANCE])

    try:
        OfficeStore().resolve_conflict(workspace, "backend_dev", take="remote")
    except ClassKeyedPlacementRefused as exc:
        assert exc.safe_details["reasons"] == ["duplicate_item_placement"]
        assert exc.safe_details["conflicting_actor_keys"] == [INSTANCE]
    else:
        raise AssertionError("a class-keyed peer duplicated the instance-keyed actor's items")

    assert _keys(workspace) == {INSTANCE}


def test_resolve_conflict_take_remote_normalizes_the_peers_key_spelling():
    """The peer's bytes are the ONE input on this path that never met
    ``_normalize_persona_id`` - ``upsert_actor``'s normalizers are precisely
    what ``_write_actor`` skips. A member whose launcher wrote ``Backend_Dev``
    publishes that spelling verbatim, and a class-key test that compares raw
    reads it as instance-keyed and waves the resurrection straight through.

    (``actor_file_token`` does not lowercase, so on a case-insensitive
    filesystem this even lands on the archived key's own filename.)
    """

    from agent_runtime.office_class_key_guard import ClassKeyedPlacementRefused

    workspace, item_ids = _migrated_workspace("Foreign Casing Office")
    _seed_remote_conflict(workspace, "Backend_Dev", persona_id="Backend_Dev", item_ids=item_ids)

    try:
        OfficeStore().resolve_conflict(workspace, "Backend_Dev", take="remote")
    except ClassKeyedPlacementRefused as exc:
        assert "resurrects_archived_class_key" in exc.safe_details["reasons"]
    else:
        raise AssertionError("a peer's un-normalized class key slipped past the fence")

    assert _keys(workspace) == {INSTANCE}
    assert "backend_dev" in OfficeStore().get_surface(workspace).archived_actor_keys


def test_resolve_conflict_dry_run_surfaces_the_refusal_instead_of_previewing_it():
    """A dry run exists to show what the real run would do. Previewing a happy
    adoption and only refusing on the real invocation is the worst of both."""

    from agent_runtime.office_class_key_guard import ClassKeyedPlacementRefused

    workspace, item_ids = _migrated_workspace("Dry Run Office")
    _seed_remote_conflict(workspace, "backend_dev", persona_id="backend_dev", item_ids=item_ids)

    try:
        OfficeStore().resolve_conflict(workspace, "backend_dev", take="remote", dry_run=True)
    except ClassKeyedPlacementRefused:
        pass
    else:
        raise AssertionError("--dry-run previewed a write the real run refuses")


def test_resolve_conflict_allow_class_key_really_adopts_the_class_keyed_peer():
    """The override. It is the whole reason this is refusal-with-consent rather
    than a flat refusal: an operator who has decided the peer's class-keyed
    state is the right one must be able to say so. It really writes - the
    double placement becomes a recorded decision instead of an accident."""

    workspace, item_ids = _migrated_workspace("Forced Office")
    _seed_remote_conflict(workspace, "backend_dev", persona_id="backend_dev", item_ids=item_ids)

    resolved = OfficeStore().resolve_conflict(workspace, "backend_dev", take="remote", allow_class_key=True)

    assert resolved is not None and resolved.actor_key == "backend_dev"
    assert _keys(workspace) == {"backend_dev", INSTANCE}


def test_cli_resolve_conflict_take_remote_refuses_with_duplicate_conflict():
    """The operator-facing surface. ``emit_harness_error`` maps an unnamed
    ``AgentRuntimeError`` to ``internal_error``, so the CLI has to catch this
    one explicitly or the refusal reads as a crash."""

    workspace, item_ids = _migrated_workspace("CLI Refused Office")
    _seed_remote_conflict(workspace, "backend_dev", persona_id="backend_dev", item_ids=item_ids)

    result = _run_harness(
        "office", "resolve-conflict", "--workspace", workspace,
        "--actor", "backend_dev", "--take", "remote", "--json",
    )

    assert result.returncode == 4, result.stdout + result.stderr
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "duplicate_conflict"
    assert "resurrects_archived_class_key" in error["message"]
    assert "--take local" in error["message"]
    assert _keys(workspace) == {INSTANCE}


def test_cli_resolve_conflict_allow_class_key_forces_the_adoption():
    """The hatch, through the verb an operator actually types."""

    workspace, item_ids = _migrated_workspace("CLI Forced Office")
    _seed_remote_conflict(workspace, "backend_dev", persona_id="backend_dev", item_ids=item_ids)

    result = _run_harness(
        "office", "resolve-conflict", "--workspace", workspace,
        "--actor", "backend_dev", "--take", "remote", "--allow-class-key", "--json",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["id"] == "backend_dev"
    assert _keys(workspace) == {"backend_dev", INSTANCE}


INSTANCE_QA = "personainst_qa_agent_5b1c2d3e"


def test_resolve_conflict_take_remote_refuses_a_second_persona_class_too():
    """The same refusal for a persona that is not ``backend_dev``.

    Found by mutation: hard-coding the class-key test to the flagship fixture's
    own persona (``actor.actor_key == "backend_dev"``) left every other test in
    this section green, because every one of them re-used that persona. The live
    canvas carries four archived class keys, not one.
    """

    from agent_runtime.office_class_key_guard import ClassKeyedPlacementRefused

    workspace = _workspace("Second Persona Office")
    OfficeStore().upsert_actor(
        workspace,
        {
            "persona_id": "qa",
            "persona_instance_id": INSTANCE_QA,
            "items": [{"item_id": INSTANCE_QA, "kind": "agent", "position": [1.0, 1.0], "folder": "Agents"}],
        },
    )
    assert _keys(workspace) == {INSTANCE_QA}
    _seed_remote_conflict(workspace, "qa", persona_id="qa", item_ids=[INSTANCE_QA])

    try:
        OfficeStore().resolve_conflict(workspace, "qa", take="remote")
    except ClassKeyedPlacementRefused as exc:
        assert exc.safe_details["reasons"] == ["duplicate_item_placement"]
        assert exc.safe_details["persona_id"] == "qa"
        assert exc.safe_details["conflicting_actor_keys"] == [INSTANCE_QA]
    else:
        raise AssertionError("the fence only recognizes one persona class")

    assert _keys(workspace) == {INSTANCE_QA}


def test_resolve_conflict_take_remote_fences_the_key_that_gets_WRITTEN_not_the_sidecar_name():
    """``_write_actor`` routes on the REMOTE RECORD's ``actor_key``, never on
    the sidecar filename or the key the operator typed. So the fence has to read
    the same field, or it guards one key while the store writes another.

    Found by mutation, and the only mutation in this campaign that stayed GREEN:
    handing the fence ``resolve_conflict``'s own ``actor_key`` argument instead
    of ``actor.actor_key`` passed every other test here, because a sidecar
    produced by ``apply_office_pull`` always keys the file by the record
    (``office_sync._read_remote_office``: "Payload is truth; the filename is
    routing only"). Every fixture agreed, so nothing distinguished the two.
    A sidecar whose two keys DISAGREE is exactly the case where the store's own
    doctrine says the record wins.
    """

    from agent_runtime import paths
    from agent_runtime.office_class_key_guard import ClassKeyedPlacementRefused

    workspace, item_ids = _migrated_workspace("Split Key Office")
    # Filed under the instance key; the record inside says the CLASS key, which
    # is the one ``_write_actor`` would resurrect.
    _seed_remote_conflict(workspace, INSTANCE, persona_id="backend_dev", item_ids=item_ids)
    sidecar_path = paths.office_conflict_path(workspace, INSTANCE)
    body = json.loads(sidecar_path.read_text(encoding="utf-8"))
    body["remote_actor"]["actor_key"] = "backend_dev"
    sidecar_path.write_text(json.dumps(body), encoding="utf-8")

    try:
        OfficeStore().resolve_conflict(workspace, INSTANCE, take="remote")
    except ClassKeyedPlacementRefused as exc:
        assert "resurrects_archived_class_key" in exc.safe_details["reasons"]
    else:
        raise AssertionError("the fence guarded the sidecar name while the store wrote the class key")

    assert _keys(workspace) == {INSTANCE}
    assert not paths.office_actor_path(workspace, "backend_dev").exists()
