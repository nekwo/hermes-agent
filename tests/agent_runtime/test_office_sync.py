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


# ── ML-8b/1: the sync arms refuse rather than decide on a short list ───────
#
# ``OfficeStore._read_actor_dir`` skips a file it cannot decode. Publish copies
# actor FILES verbatim, so the undecodable one travels, and "removals propagate
# as absences" then turns every peer's pull into a desk removal for an actor
# whose file merely would not open here. These witnesses drive the count with
# TWO distinct values so a constant-1 (or constant-0) arm cannot fake them.


def _blind_one_actor(ws: str, actor_key: str):
    """Make one live actor file undecodable — an AV quarantine stub, a
    half-flushed write, a disk error. The row is still THERE; it just cannot be
    read, which is the state ``list_actors`` reports as "absent"."""

    path = paths.office_actor_path(ws, actor_key)
    assert path.exists()
    path.write_text("{truncated", encoding="utf-8")
    return path


def _office_relative_paths(realm_id: str) -> list[str]:
    return [
        a.relative_path
        for a in resolve_realm_sync_artifacts(realm_id)
        if a.kind in ("office", "office_actor")
    ]


def _resolve_office_refusals(realm_id: str) -> list[dict]:
    from agent_runtime.realm_sync import _resolve_artifacts_with_projection

    return list(_resolve_artifacts_with_projection(realm_id).office_refused)


def test_a_workspace_with_an_unreadable_actor_file_refuses_realm_publish_typed():
    """THE publish-side witness.

    *Probed:* the typed reason and its COUNT (driven 1 then 2), that the
    workspace contributes ZERO published office artifacts, and that the baseline
    recorder was called and carried no key for that workspace.

    *Mutation:* swap the arms back to the thin ``list_actors`` view
    (``ActorScan(store.list_actors(ws), 0)``). The mutant cannot mint a reason
    from a count it never took, and it publishes the workspace's artifacts —
    the recorder convicts it on both.
    """

    from agent_runtime import office_sync
    from agent_runtime.office_sync import SYNC_UNKNOWABLE

    realm_id, ws = _make_realm_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _payload("dev"))
    store.upsert_actor(ws, _payload("edu_tutor"))
    store.upsert_actor(ws, _payload("qa_lead"))
    # Readable state first: this workspace really does publish, so the refusal
    # below is a change of answer and not a fixture that never published.
    assert f"store/office/{ws}/actors/dev.json" in _office_relative_paths(realm_id)

    for driven, keys in ((1, ["dev"]), (2, ["dev", "edu_tutor"])):
        for key in keys:
            _blind_one_actor(ws, key)

        refused = _resolve_office_refusals(realm_id)
        assert [row["workspace_id"] for row in refused] == [ws], refused
        assert refused[0]["reason"] == SYNC_UNKNOWABLE
        assert refused[0]["unreadable"] == driven, refused

        # Zero publish writes for this workspace: no artifact rows at all.
        assert _office_relative_paths(realm_id) == [], driven

        # ... and the baseline arm records nothing for it either. The recorder
        # proves the write HAPPENED and simply carried no key for this
        # workspace — an absence caused by a refusal, not by a crash.
        handed: list[dict[str, str]] = []
        original = office_sync.write_office_baseline

        def _recording_write(realm, entries, _o=original, _h=handed):
            _h.append(dict(entries))
            _o(realm, entries)

        office_sync.write_office_baseline = _recording_write
        try:
            summary = update_office_baseline_after_sync(realm_id, [ws])
        finally:
            office_sync.write_office_baseline = original
        assert len(handed) == 1, handed
        assert [k for k in handed[0] if k.startswith(f"{ws}:")] == [], handed
        assert summary.recorded == []
        assert summary.refused == [
            {"workspace_id": ws, "reason": SYNC_UNKNOWABLE, "unreadable": driven}
        ]


def test_a_readable_workspace_still_publishes_beside_a_refused_one():
    """The scope boundary: the refusal is per-workspace, never global.

    An arm that froze the whole realm on one bad file would be a worse failure
    than the one being prevented, and would get deleted. Neither
    refuse-everywhere nor refuse-nowhere passes this pair with the witness above.
    """

    realm_id, ws = _make_realm_workspace()
    other = WorkspaceStore().create(name="WS2", realm_id=realm_id)
    realm = RealmStore().get(realm_id)
    realm.workspace_ids.append(other.id)
    RealmStore().save(realm)

    store = OfficeStore()
    store.upsert_actor(ws, _payload("dev"))
    store.upsert_actor(other.id, _payload("edu_tutor"))
    _blind_one_actor(ws, "dev")

    published = _office_relative_paths(realm_id)
    assert f"store/office/{other.id}/actors/edu_tutor.json" in published
    assert not any(p.startswith(f"store/office/{ws}/") for p in published), published

    summary = update_office_baseline_after_sync(realm_id, [ws, other.id])
    assert summary.recorded == [other.id]
    assert [row["workspace_id"] for row in summary.refused] == [ws]
    baseline = read_office_baseline(realm_id)
    assert f"{other.id}:actor:edu_tutor" in baseline
    assert not any(k.startswith(f"{ws}:") for k in baseline)


def test_a_refused_workspace_pins_no_persona_definitions():
    """One scan, one answer. The persona ids a publish PINS and the office
    artifacts it writes used to be two independent walks; a workspace excluded
    from one but not the other pins a definition for a placement that is not
    travelling."""

    realm_id, ws = _make_realm_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _payload("edu_tutor"))
    workspaces = [WorkspaceStore().get(ws)]
    assert "edu_tutor" in _office_wanted_persona_ids(workspaces)

    _blind_one_actor(ws, "edu_tutor")
    # All three answers have to move TOGETHER or the coherence claim is empty:
    # the workspace is refused, so neither its artifacts nor its persona pins
    # travel. Asserting only the empty persona list would pass under the
    # ``list_actors`` mutant as well — that loses the row either way.
    assert [row["workspace_id"] for row in _resolve_office_refusals(realm_id)] == [ws]
    assert _office_wanted_persona_ids(workspaces) == []
    assert _office_relative_paths(realm_id) == []


def test_a_workspace_with_an_unreadable_actor_file_converges_nothing_on_pull(tmp_path):
    """THE compare-arm witness: the local half has to be knowable BEFORE the
    three-way classifier reads a missing local row as a local delete.

    *Probed:* the typed ``unknowable`` row and its driven count (1 then 2), and
    that the pull took no decision at all for that workspace — the remote actor
    is neither adopted nor is the local one archived.

    *Mutation:* restore ``store.list_actors(workspace_id)`` for the local read.
    The mutant classifies ``dev`` as locally absent, adopts the remote copy over
    the file it could not read, and reports it as adopted — the counts red.
    """

    from agent_runtime.office_sync import SYNC_UNKNOWABLE

    realm_id, ws = _make_realm_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _payload("dev"))
    store.upsert_actor(ws, _payload("edu_tutor"))
    subtree = tmp_path / "subtree"
    _write_remote_office(subtree, ws, [_remote_actor(ws, "dev", x=9.0)])

    for driven, keys in ((1, ["dev"]), (2, ["dev", "edu_tutor"])):
        for key in keys:
            _blind_one_actor(ws, key)
        summary = apply_office_pull(realm_id, subtree)
        assert summary.unknowable == [
            {"workspace_id": ws, "reason": SYNC_UNKNOWABLE, "unreadable": driven}
        ], summary.as_dict()
        assert summary.workspaces == []
        assert (
            summary.adopted,
            summary.converged,
            summary.kept_local,
            summary.archived,
            summary.conflicts,
        ) == (0, 0, 0, 0, 0), summary.as_dict()
        # The unreadable file is left exactly as found, for an operator to repair.
        assert paths.office_actor_path(ws, "dev").read_text(encoding="utf-8") == "{truncated"
        assert not paths.office_conflict_path(ws, "dev").exists()
