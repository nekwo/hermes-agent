"""Mission Office realm-sync tests (office plan W-H2): the classifier-lift
no-op proof, apply_office_pull integration (adopt / take-remote / converge /
keep-local / archive / conflict / resurrection guard), the artifact family +
exclusions + wanted-persona union, and the baseline round-trip. Autouse
conftest fixtures isolate the runtime root.
"""

from __future__ import annotations

from utils import atomic_json_write

from agent_runtime import office_models, paths
from agent_runtime.models import OfficeActor, OfficeItem
from agent_runtime.office_store import OfficeStore
from agent_runtime.office_sync import (
    apply_office_pull,
    read_office_baseline,
    update_office_baseline_after_sync,
    write_office_baseline,
)
from agent_runtime.realm_sync import (
    _assert_no_secret_artifacts,
    _destination_for_sync_path,
    _office_wanted_persona_ids,
    resolve_realm_sync_artifacts,
)
from agent_runtime.serde import to_jsonable
from agent_runtime.store import RealmStore, WorkspaceStore


def _make_realm_workspace() -> tuple[str, str]:
    realm = RealmStore().create(name="Realm")
    ws = WorkspaceStore().create(name="WS", realm_id=realm.id)
    realm = RealmStore().get(realm.id)
    realm.workspace_ids.append(ws.id)
    RealmStore().save(realm)
    WorkspaceStore().set_active(ws.id)
    return realm.id, ws.id


def _payload(persona_id: str = "dev", **overrides) -> dict:
    payload = {
        "persona_id": persona_id,
        "items": [
            {"item_id": persona_id, "persona_id": persona_id, "kind": "agent", "position": [1.0, 2.0], "folder": "Agents"},
        ],
    }
    payload.update(overrides)
    return payload


def _remote_actor(ws: str, actor_key: str, *, x: float = 5.0) -> OfficeActor:
    return OfficeActor(
        actor_key=actor_key,
        workspace_id=ws,
        persona_id=actor_key,
        items=[OfficeItem(item_id=actor_key, persona_id=actor_key, kind="agent", position=[x, 1.0], folder="Agents")],
        revision=1,
    )


def _write_remote_office(subtree, ws: str, actors: list[OfficeActor]) -> None:
    import shutil

    surface = office_models.default_surface(ws, created_at=None)
    office_dir = subtree / "store" / "office" / ws
    # Publish rmtree-rebuilds the realm subtree, so absences ARE removals —
    # the fixture must mimic that or stale actor files leak between writes.
    if office_dir.exists():
        shutil.rmtree(office_dir)
    atomic_json_write(office_dir / "office.json", to_jsonable(surface), indent=2, sort_keys=True)
    for actor in actors:
        atomic_json_write(
            office_dir / "actors" / f"{office_models.actor_file_token(actor.actor_key)}.json",
            to_jsonable(actor),
            indent=2,
            sort_keys=True,
        )


# ── the lift is a no-op (plan §5) ─────────────────────────────────────────


def test_board_classifier_is_the_shared_classifier():
    from agent_runtime.board_sync import classify_board_pull
    from agent_runtime.sync_merge import classify_three_way_pull

    assert classify_board_pull is classify_three_way_pull


# ── apply_office_pull decision integration ────────────────────────────────


def test_adopt_remote_into_fresh_root(tmp_path):
    realm_id, ws = _make_realm_workspace()
    subtree = tmp_path / "subtree"
    _write_remote_office(subtree, ws, [_remote_actor(ws, "dev")])

    summary = apply_office_pull(realm_id, subtree)
    assert summary.adopted == 1 and summary.conflicts == 0
    store = OfficeStore()
    assert store.actor_exists(ws, "dev")
    # Baseline recorded so the next pull is a NOOP.
    again = apply_office_pull(realm_id, subtree)
    assert again.adopted == 0 and again.conflicts == 0


def test_take_remote_when_local_unchanged(tmp_path):
    realm_id, ws = _make_realm_workspace()
    subtree = tmp_path / "subtree"
    _write_remote_office(subtree, ws, [_remote_actor(ws, "dev", x=1.0)])
    apply_office_pull(realm_id, subtree)

    moved = _remote_actor(ws, "dev", x=9.0)
    _write_remote_office(subtree, ws, [moved])
    summary = apply_office_pull(realm_id, subtree)
    assert summary.adopted == 1  # take_remote counts as adopt (non-converged write)
    actor = OfficeStore().get_actor(ws, "dev")
    assert actor.items[0].position[0] == 9.0


def test_local_edit_kept_when_remote_unchanged(tmp_path):
    realm_id, ws = _make_realm_workspace()
    subtree = tmp_path / "subtree"
    _write_remote_office(subtree, ws, [_remote_actor(ws, "dev", x=1.0)])
    apply_office_pull(realm_id, subtree)

    store = OfficeStore()
    payload = _payload("dev")
    payload["items"][0]["position"] = [7.0, 7.0]
    store.upsert_actor(ws, payload)

    summary = apply_office_pull(realm_id, subtree)
    assert summary.kept_local == 1 and summary.conflicts == 0
    assert store.get_actor(ws, "dev").items[0].position[0] == 7.0


def test_same_actor_divergence_is_loud_conflict(tmp_path):
    realm_id, ws = _make_realm_workspace()
    subtree = tmp_path / "subtree"
    _write_remote_office(subtree, ws, [_remote_actor(ws, "dev", x=1.0)])
    apply_office_pull(realm_id, subtree)

    store = OfficeStore()
    payload = _payload("dev")
    payload["items"][0]["position"] = [7.0, 7.0]
    store.upsert_actor(ws, payload)
    _write_remote_office(subtree, ws, [_remote_actor(ws, "dev", x=9.0)])

    summary = apply_office_pull(realm_id, subtree)
    assert summary.conflicts == 1
    sidecar = paths.office_conflict_path(ws, "dev")
    assert sidecar.exists()
    # Local kept, never silently overwritten.
    assert store.get_actor(ws, "dev").items[0].position[0] == 7.0
    # take=remote adopts the sidecar copy.
    resolved = store.resolve_conflict(ws, "dev", take="remote")
    assert resolved is not None and resolved.items[0].position[0] == 9.0


def test_remote_removed_archives_never_deletes(tmp_path):
    realm_id, ws = _make_realm_workspace()
    subtree = tmp_path / "subtree"
    _write_remote_office(subtree, ws, [_remote_actor(ws, "dev")])
    apply_office_pull(realm_id, subtree)

    _write_remote_office(subtree, ws, [])  # publish rmtree semantics: absence = removal
    summary = apply_office_pull(realm_id, subtree)
    assert summary.archived == 1
    store = OfficeStore()
    assert not store.actor_exists(ws, "dev")
    assert paths.office_archived_actor_path(ws, "dev").exists()
    assert "dev" in store.get_surface(ws).archived_actor_keys


def test_resurrection_guard_blocks_pulled_copy_of_archived_actor(tmp_path):
    realm_id, ws = _make_realm_workspace()
    subtree = tmp_path / "subtree"
    _write_remote_office(subtree, ws, [_remote_actor(ws, "dev", x=1.0)])
    apply_office_pull(realm_id, subtree)

    store = OfficeStore()
    store.remove_actor(ws, "dev", reason="operator")
    # Remote still carries the identical (baseline-unchanged) copy → NOOP,
    # never a re-materialized desk.
    summary = apply_office_pull(realm_id, subtree)
    assert summary.adopted == 0 and summary.archived == 0
    assert not store.actor_exists(ws, "dev")
    # A remote EDIT of the archived actor is a loud conflict, not a revive.
    _write_remote_office(subtree, ws, [_remote_actor(ws, "dev", x=9.0)])
    summary = apply_office_pull(realm_id, subtree)
    assert summary.conflicts == 1
    assert not store.actor_exists(ws, "dev")


def test_converged_edits_settle_silently(tmp_path):
    realm_id, ws = _make_realm_workspace()
    subtree = tmp_path / "subtree"
    _write_remote_office(subtree, ws, [_remote_actor(ws, "dev", x=1.0)])
    apply_office_pull(realm_id, subtree)

    # Both sides land on the same content (hash-equal) → converge, no conflict.
    store = OfficeStore()
    remote = _remote_actor(ws, "dev", x=4.0)
    payload = _payload("dev")
    payload["items"][0]["position"] = [4.0, 1.0]
    store.upsert_actor(ws, payload)
    _write_remote_office(subtree, ws, [remote])
    summary = apply_office_pull(realm_id, subtree)
    assert summary.converged == 1 and summary.conflicts == 0


# ── baseline round trip ────────────────────────────────────────────────────


def test_baseline_round_trip_and_publish_hook():
    realm_id, ws = _make_realm_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _payload("dev"))
    update_office_baseline_after_sync(realm_id, [ws])
    baseline = read_office_baseline(realm_id)
    assert f"{ws}:office" in baseline
    assert f"{ws}:actor:dev" in baseline
    write_office_baseline(realm_id, {})
    assert read_office_baseline(realm_id) == {}


# ── artifact family + exclusions + wanted union (plan §5) ─────────────────


def test_office_artifacts_join_realm_sync_and_exclusions_hold(tmp_path):
    realm_id, ws = _make_realm_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _payload("dev"))
    store.upsert_actor(ws, _payload("edu_tutor"))
    store.remove_actor(ws, "edu_tutor")  # archive must NOT publish
    sidecar = paths.office_conflict_path(ws, "dev2")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("{}", encoding="utf-8")

    artifacts = resolve_realm_sync_artifacts(realm_id)
    kinds = {a.kind for a in artifacts}
    assert "office" in kinds and "office_actor" in kinds
    office_paths = [a.relative_path for a in artifacts if a.kind in ("office", "office_actor")]
    assert f"store/office/{ws}/office.json" in office_paths
    assert f"store/office/{ws}/actors/dev.json" in office_paths
    assert not any("archive" in p or "conflicts" in p or "baseline" in p for p in office_paths)
    _assert_no_secret_artifacts([a for a in artifacts if a.kind in ("office", "office_actor")])


def test_office_wanted_persona_union():
    realm_id, ws = _make_realm_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _payload("edu_tutor"))  # not in any workspace.agent_ids
    workspaces = [WorkspaceStore().get(ws)]
    assert "edu_tutor" in _office_wanted_persona_ids(workspaces)


def test_generic_pull_loop_never_touches_office_paths():
    assert _destination_for_sync_path("store/office/ws1/office.json") is None
    assert _destination_for_sync_path("store/office/ws1/actors/dev.json") is None
