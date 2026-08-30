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


# ── H1: the adopt arm emits, like the archive arm always has ──────────────


#: The EventLog is the STORE's log, not the instance's — every ``EventLog()``
#: reads the same file — so a test that asks "what did THIS pull emit" has to
#: slice the tail from a mark taken before the call, not filter the whole log.
def _event_mark() -> int:
    from agent_runtime.events import EventLog

    return len(EventLog().tail(2000))


def _events_since(mark: int) -> list:
    from agent_runtime.events import EventLog

    return EventLog().tail(2000)[mark:]


def _office_events(mark: int) -> list:
    return [evt for evt in _events_since(mark) if str(evt.type).startswith("office.")]


def _actor_patches(mark: int, workspace_id: str) -> list:
    from agent_runtime.state_patches import STATE_PATCHED_EVENT_TYPE, office_patch_scope

    return [
        evt
        for evt in _events_since(mark)
        if evt.type == STATE_PATCHED_EVENT_TYPE
        and (evt.payload or {}).get("entity") == "office_actor"
        and office_patch_scope(evt.payload or {}) == workspace_id
    ]


def test_adopting_a_remote_actor_emits_the_same_pair_the_archive_arm_does(tmp_path):
    """The H1 defect in one test: a pull that GIVES you a desk was invisible to
    every live consumer while a pull that TOOK one away was not."""

    realm_id, ws = _make_realm_workspace()
    subtree = tmp_path / "subtree"
    _write_remote_office(subtree, ws, [_remote_actor(ws, "dev")])

    mark = _event_mark()
    summary = apply_office_pull(realm_id, subtree)
    assert summary.adopted == 1

    upserted = [evt for evt in _office_events(mark) if evt.type == "office.actor.upserted"]
    assert [evt.payload.get("actor_key") for evt in upserted] == ["dev"]
    assert upserted[0].payload.get("workspace_id") == ws
    # …and its paired patch, without which the covered domain event would ship a
    # patch frame with an EMPTY patches list.
    assert len(_actor_patches(mark, ws)) == 1


def test_the_adopted_row_keeps_the_remote_revision_and_names_the_sync(tmp_path):
    """(a) and (b) of H1's three preserved properties. A local re-numbering would
    make the next classify read an untouched desk as a local edit."""

    realm_id, ws = _make_realm_workspace()
    subtree = tmp_path / "subtree"
    remote = _remote_actor(ws, "dev")
    remote.revision = 7
    _write_remote_office(subtree, ws, [remote])

    apply_office_pull(realm_id, subtree)
    adopted = OfficeStore().get_actor(ws, "dev")
    assert adopted.revision == 7
    assert adopted.updated_by == "realm_sync"


def test_the_adopted_baseline_stays_keyed_off_the_remote_hash(tmp_path):
    """(c). The stamped ``updated_by`` is excluded from ``office_content_hash``,
    so the row the store wrote still hashes to the baseline the pull recorded —
    which is exactly what makes the second pull a NOOP instead of a conflict."""

    realm_id, ws = _make_realm_workspace()
    subtree = tmp_path / "subtree"
    remote = _remote_actor(ws, "dev")
    _write_remote_office(subtree, ws, [remote])
    apply_office_pull(realm_id, subtree)

    baseline = read_office_baseline(realm_id)
    on_disk = OfficeStore().get_actor(ws, "dev")
    assert baseline[f"{ws}:actor:dev"] == office_models.office_content_hash(on_disk)

    again = apply_office_pull(realm_id, subtree)
    assert (again.adopted, again.converged, again.conflicts, again.kept_local) == (0, 0, 0, 0)


def test_the_archive_arms_events_are_unchanged(tmp_path):
    realm_id, ws = _make_realm_workspace()
    subtree = tmp_path / "subtree"
    _write_remote_office(subtree, ws, [_remote_actor(ws, "dev")])
    apply_office_pull(realm_id, subtree)

    _write_remote_office(subtree, ws, [])
    mark = _event_mark()
    summary = apply_office_pull(realm_id, subtree)
    assert summary.archived == 1
    removed = [evt for evt in _office_events(mark) if evt.type == "office.actor.removed"]
    assert [evt.payload.get("reason") for evt in removed] == ["remote_removed"]


def test_adopting_a_remote_surface_emits_for_a_workspace_that_already_had_one(tmp_path):
    """The surface half. A CREATE stays on the full-core lane (no patch), which is
    the same ruling ``update_surface`` rides."""

    realm_id, ws = _make_realm_workspace()
    subtree = tmp_path / "subtree"
    _write_remote_office(subtree, ws, [_remote_actor(ws, "dev")])

    created_mark = _event_mark()
    apply_office_pull(realm_id, subtree)
    assert [
        evt.type for evt in _office_events(created_mark) if evt.type.startswith("office.surface.")
    ] == ["office.surface.created"]

    # Now a remote-only folder edit against an office this machine already holds
    # (local untouched, so the classifier answers WRITE_REMOTE rather than the
    # surface arm's keep-local-wins).
    store = OfficeStore()
    remote_surface = office_models.default_surface(ws, created_at=None)
    remote_surface.folders = [*remote_surface.folders, "Peers"]
    office_dir = subtree / "store" / "office" / ws
    atomic_json_write(office_dir / "office.json", to_jsonable(remote_surface), indent=2, sort_keys=True)

    mark = _event_mark()
    apply_office_pull(realm_id, subtree)
    surface_events = [
        evt for evt in _office_events(mark) if evt.type.startswith("office.surface.")
    ]
    assert [evt.type for evt in surface_events] == ["office.surface.updated"]
    assert surface_events[0].payload.get("change") == "realm_sync"
    assert "Peers" in store.get_surface(ws).folders


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


# ── ML-8b/2: an unreadable PULLED actor cannot read as a peer delete ───────


def _blind_pulled_actor(subtree, ws: str, actor_key: str):
    """Corrupt one actor file inside the PULLED subtree. The peer published it
    intact; this side cannot decode it — which is the state the pull's decision
    table used to receive as "the peer removed this desk"."""

    path = subtree / "store" / "office" / ws / "actors" / f"{office_models.actor_file_token(actor_key)}.json"
    assert path.exists(), path
    path.write_text("{truncated", encoding="utf-8")
    return path


def test_an_unreadable_pulled_actor_cannot_read_as_a_peer_delete(tmp_path):
    """THE pull-read witness.

    *Probed:* ``unreadable_remote`` on the summary (driven 1 then 2), that no
    delete-shaped decision was taken for those keys (the ``delete_fenced``
    recorder names each one), and that the desks are still there afterwards.

    *Mutation:* restore the bare ``continue`` in ``_read_remote_office`` (drop
    ``unreadable += 1``). The summary then lacks the count the fixture drives,
    the fence never engages, and both desks are archived on the strength of a
    parse error.
    """

    realm_id, ws = _make_realm_workspace()
    subtree = tmp_path / "subtree"
    _write_remote_office(
        subtree,
        ws,
        [_remote_actor(ws, "dev"), _remote_actor(ws, "edu_tutor"), _remote_actor(ws, "qa_lead")],
    )
    adopted = apply_office_pull(realm_id, subtree)
    assert adopted.adopted == 3 and adopted.unreadable_remote == 0

    store = OfficeStore()
    for driven, keys in ((1, ["dev"]), (2, ["dev", "edu_tutor"])):
        for key in keys:
            _blind_pulled_actor(subtree, ws, key)
        summary = apply_office_pull(realm_id, subtree)
        assert summary.unreadable_remote == driven, summary.as_dict()
        assert summary.archived == 0, summary.as_dict()
        assert summary.delete_fenced == [
            {
                "workspace_id": ws,
                "actor_key": key,
                "reason": "unreadable_remote",
                "unreadable_remote": driven,
            }
            for key in sorted(keys)
        ], summary.as_dict()
        for key in keys:
            assert store.actor_exists(ws, key), key
            assert key not in store.get_surface(ws).archived_actor_keys
        # The held desk keeps its baseline row, so a repaired remote still
        # converges it rather than arriving as a fresh adopt.
        assert f"{ws}:actor:dev" in read_office_baseline(realm_id)
    # The bystander that WAS readable and unchanged is untouched throughout.
    assert store.actor_exists(ws, "qa_lead")


def test_a_readable_remote_removal_still_archives_beside_a_fenced_one(tmp_path):
    """The discriminator that stops the fence from becoming "never archive".

    A remote office that reads COMPLETELY still propagates its removals — the
    absence means what it says there. Neither fence-everywhere nor fence-nowhere
    passes this test together with the one above.
    """

    realm_id, ws = _make_realm_workspace()
    subtree = tmp_path / "subtree"
    _write_remote_office(subtree, ws, [_remote_actor(ws, "dev"), _remote_actor(ws, "edu_tutor")])
    apply_office_pull(realm_id, subtree)

    # Peer genuinely removed edu_tutor; every remaining file decodes.
    _write_remote_office(subtree, ws, [_remote_actor(ws, "dev")])
    summary = apply_office_pull(realm_id, subtree)
    assert summary.unreadable_remote == 0
    assert summary.delete_fenced == []
    assert summary.archived == 1
    assert not OfficeStore().actor_exists(ws, "edu_tutor")


def test_an_unreadable_remote_surface_is_counted_not_skipped(tmp_path):
    """The other half of the same read. A directory whose ``office.json`` will
    not decode names no workspace, so nothing in it can be attributed — but it
    is a real remote office this pull did not apply, and the count says so
    instead of the directory vanishing."""

    realm_id, ws = _make_realm_workspace()
    subtree = tmp_path / "subtree"
    _write_remote_office(subtree, ws, [_remote_actor(ws, "dev")])
    (subtree / "store" / "office" / ws / "office.json").write_text("{truncated", encoding="utf-8")

    summary = apply_office_pull(realm_id, subtree)
    assert summary.unreadable_remote == 1, summary.as_dict()
    assert summary.workspaces == []
    assert summary.adopted == 0
