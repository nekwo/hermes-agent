"""S3 Mission Board realm-sync tests: exhaustive classify_board_pull decision
table, apply_board_pull integration (adopt / converge / conflict / archive /
resurrection guard), the artifact family + exclusions, secret-scan fail-closed,
and baseline round-trip. Autouse conftest fixtures isolate the runtime root.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_runtime import board_models, paths
from agent_runtime.board_store import BoardStore
from agent_runtime.board_sync import (
    BoardPullAction,
    apply_board_pull,
    classify_board_pull,
    read_board_baseline,
    update_board_baseline_after_sync,
    write_board_baseline,
)
from agent_runtime.realm_sync import (
    RealmSyncError,
    _any_store_drift,
    _assert_no_secret_artifacts,
    _board_store_drift,
    _workspaces_for_realm,
    resolve_realm_sync_artifacts,
)
from agent_runtime.serde import to_jsonable
from agent_runtime.store import RealmStore, WorkspaceStore
from utils import atomic_json_write


def _make_realm_workspace() -> tuple[str, str]:
    realm = RealmStore().create(name="Realm")
    ws = WorkspaceStore().create(name="WS", realm_id=realm.id)
    realm = RealmStore().get(realm.id)
    realm.workspace_ids.append(ws.id)
    RealmStore().save(realm)
    WorkspaceStore().set_active(ws.id)
    return realm.id, ws.id


# ── exhaustive decision table (§8, all rows) ──────────────────────────────


@pytest.mark.parametrize(
    "local,remote,baseline,archived,expected,reason",
    [
        ("h", "h", "h", False, BoardPullAction.NOOP, "unchanged"),
        ("h", "x", "h", False, BoardPullAction.WRITE_REMOTE, "take_remote"),
        ("h", None, "h", False, BoardPullAction.ARCHIVE_LOCAL, "remote_removed"),
        ("y", "h", "h", False, BoardPullAction.KEEP_LOCAL, "unpublished"),
        ("y", "y", "h", False, BoardPullAction.WRITE_REMOTE, "converged"),
        ("y", "z", "h", False, BoardPullAction.CONFLICT, "both_changed"),
        ("y", None, "h", False, BoardPullAction.CONFLICT, "edit_vs_remove"),
        ("h", "h", "h", True, BoardPullAction.NOOP, "archived_local"),
        ("h", None, "h", True, BoardPullAction.NOOP, "archived_local"),
        ("h", "z", "h", True, BoardPullAction.CONFLICT, "archive_vs_edit"),
        ("h", None, None, False, BoardPullAction.KEEP_LOCAL, "new_local"),
        (None, "z", None, False, BoardPullAction.WRITE_REMOTE, "adopt_remote"),
        ("a", "a", None, False, BoardPullAction.WRITE_REMOTE, "converged"),
        ("a", "b", None, False, BoardPullAction.CONFLICT, "new_both"),
        (None, None, None, False, BoardPullAction.NOOP, "absent_both"),
    ],
)
def test_classify_board_pull_decision_table(local, remote, baseline, archived, expected, reason):
    decision = classify_board_pull(local, remote, baseline, locally_archived=archived)
    assert decision.action == expected
    assert decision.reason == reason


# ── artifact family + exclusions ──────────────────────────────────────────


def test_board_artifacts_included_archive_and_conflicts_excluded():
    realm_id, ws = _make_realm_workspace()
    store = BoardStore()
    card = store.add_card(workspace_id=ws, title="Sync me")
    archived = store.add_card(workspace_id=ws, title="Archive me")
    store.archive_card(archived.card_id)
    board_id = board_models.default_board_id(ws)
    # a conflict sidecar must never be published
    conflict = paths.board_conflict_path(board_id, card.card_id)
    conflict.parent.mkdir(parents=True, exist_ok=True)
    conflict.write_text("{}", encoding="utf-8")

    artifacts = resolve_realm_sync_artifacts(realm_id)
    rels = {a.relative_path for a in artifacts}
    assert f"store/boards/{board_id}/board.json" in rels
    assert f"store/boards/{board_id}/cards/{card.card_id}.json" in rels
    # archived card + conflict sidecar excluded
    assert not any("/archive/" in r for r in rels)
    assert not any("/conflicts/" in r for r in rels)
    assert f"store/boards/{board_id}/cards/{archived.card_id}.json" not in rels


def test_publish_secret_scan_fails_closed_on_card_prose():
    realm_id, ws = _make_realm_workspace()
    store = BoardStore()
    store.add_card(
        workspace_id=ws,
        title="Leaky",
        description="api_key=DEADBEEFDEADBEEF012345 do not ship",
    )
    artifacts = resolve_realm_sync_artifacts(realm_id)
    with pytest.raises(RealmSyncError) as exc:
        _assert_no_secret_artifacts(artifacts)
    assert exc.value.code == "sync_secret_excluded"


# ── baseline round-trip ───────────────────────────────────────────────────


def test_baseline_round_trip_and_publish_update():
    realm_id, ws = _make_realm_workspace()
    store = BoardStore()
    store.add_card(workspace_id=ws, title="One")
    board_id = board_models.default_board_id(ws)
    assert read_board_baseline(realm_id) == {}
    update_board_baseline_after_sync(realm_id, [board_id])
    baseline = read_board_baseline(realm_id)
    assert f"{board_id}:board" in baseline
    assert any(k.endswith(":card:" + c.card_id) for c in store.list_cards(board_id) for k in baseline)


# ── apply_board_pull integration ──────────────────────────────────────────


def _remote_subtree(tmp_path: Path, board, cards) -> Path:
    """Write a fake pulled realm subtree with the given board + card models."""
    subtree = tmp_path / "subtree"
    board_dir = subtree / "store" / "boards" / board.board_id
    atomic_json_write(board_dir / "board.json", to_jsonable(board), indent=2, sort_keys=True)
    for card in cards:
        atomic_json_write(board_dir / "cards" / f"{card.card_id}.json", to_jsonable(card), indent=2, sort_keys=True)
    return subtree


def test_apply_pull_adopts_new_remote_card(tmp_path):
    realm_id, ws = _make_realm_workspace()
    store = BoardStore()
    local = store.add_card(workspace_id=ws, title="Local one")
    board_id = board_models.default_board_id(ws)
    update_board_baseline_after_sync(realm_id, [board_id])  # baseline: local card known

    board = store.get(board_id)
    # remote adds a brand-new card the local machine has never seen.
    from agent_runtime.models import BoardCard
    from hermes_time import now

    remote_new = BoardCard(
        card_id="card_remote", board_id=board_id, column_id="col_queued",
        title="From peer", order_key="z", created_by="operator",
        created_at=now(), updated_at=now(),
    )
    local_card = store.get_card(local.card_id)
    subtree = _remote_subtree(tmp_path, board, [local_card, remote_new])

    summary = apply_board_pull(realm_id, subtree)
    assert summary.adopted == 1
    assert store.exists(board_id)
    titles = {c.title for c in store.list_cards(board_id)}
    assert "From peer" in titles and "Local one" in titles


def test_apply_pull_conflict_keeps_local_and_writes_sidecar(tmp_path):
    realm_id, ws = _make_realm_workspace()
    store = BoardStore()
    card = store.add_card(workspace_id=ws, title="Original")
    board_id = board_models.default_board_id(ws)
    update_board_baseline_after_sync(realm_id, [board_id])  # baseline == original

    # BOTH sides edit differently after the baseline.
    store.edit_card(card.card_id, title="Local edit")
    board = store.get(board_id)
    remote_card = store.get_card(card.card_id)
    remote_card.title = "Remote edit"
    subtree = _remote_subtree(tmp_path, board, [remote_card])

    summary = apply_board_pull(realm_id, subtree)
    assert summary.conflicts == 1
    # local copy is untouched (kept)
    assert store.get_card(card.card_id).title == "Local edit"
    # a loud conflict sidecar exists
    assert paths.board_conflict_path(board_id, card.card_id).exists()


def test_apply_pull_resurrection_guard_blocks_archived_card(tmp_path):
    realm_id, ws = _make_realm_workspace()
    store = BoardStore()
    card = store.add_card(workspace_id=ws, title="Doomed")
    board_id = board_models.default_board_id(ws)
    update_board_baseline_after_sync(realm_id, [board_id])
    board = store.get(board_id)
    remote_card = store.get_card(card.card_id)  # remote still has it
    store.archive_card(card.card_id)  # locally archived after baseline

    subtree = _remote_subtree(tmp_path, store.get(board_id), [remote_card])
    summary = apply_board_pull(realm_id, subtree)
    # resurrection guard: the archived card is NOT re-created active.
    active_ids = {c.card_id for c in store.list_cards(board_id)}
    assert card.card_id not in active_ids
    assert summary.adopted == 0


def test_apply_pull_remote_removal_archives_local(tmp_path):
    realm_id, ws = _make_realm_workspace()
    store = BoardStore()
    keep = store.add_card(workspace_id=ws, title="Keep")
    gone = store.add_card(workspace_id=ws, title="Gone remotely")
    board_id = board_models.default_board_id(ws)
    update_board_baseline_after_sync(realm_id, [board_id])

    # remote publishes only `keep` (removed `gone`).
    board = store.get(board_id)
    subtree = _remote_subtree(tmp_path, board, [store.get_card(keep.card_id)])
    summary = apply_board_pull(realm_id, subtree)
    assert summary.archived == 1
    active_ids = {c.card_id for c in store.list_cards(board_id)}
    assert gone.card_id not in active_ids
    assert keep.card_id in active_ids
    # archived, never deleted
    assert paths.board_archived_card_path(board_id, gone.card_id).exists()


# ── the adopt arm reaches the live lane (2026-09-02) ──────────────────────


def _events_since(before: int):
    from agent_runtime.events import EventLog

    return [event for _, event in EventLog().iter_from_offset(before)]


def _watermark() -> int:
    from agent_runtime.events import EventLog

    return max((offset for offset, _ in EventLog().iter_from_offset(0)), default=0)


def _remote_card(board_id: str, card_id: str, title: str):
    from hermes_time import now

    from agent_runtime.models import BoardCard

    return BoardCard(
        card_id=card_id,
        board_id=board_id,
        column_id="col_queued",
        title=title,
        order_key="z",
        created_by="operator",
        created_at=now(),
        updated_at=now(),
    )


def test_pull_that_adopts_a_card_emits_what_a_local_add_emits(tmp_path):
    """RED-FIRST (2026-09-02): ``apply_board_pull``'s adopt arm wrote the card
    with a bare ``atomic_json_write`` and emitted nothing.

    The office twin grew evented ``adopt_remote_*`` verbs in H1 for exactly this
    asymmetry: a pull that ARCHIVES a card goes through ``archive_card`` and
    emits ``board.card.archived``, so it advances the EventLog watermark and
    reaches the delta/serve lane — while a pull that GIVES you a card emitted no
    event at all, advanced no watermark, and sat on disk invisible to every live
    consumer until some unrelated write happened to wake the pipeline. This
    module's own standing rule (``board_store``'s docstring: an event on EVERY
    mutation) had one lane exempt from it by accident.

    Asserted as the SHAPE a local add emits, not merely "an event exists": same
    type, same board/card ids, same title — what changed is the same fact either
    way, and only ``updated_by`` says which lane moved it.
    """

    realm_id, ws = _make_realm_workspace()
    store = BoardStore()
    local = store.add_card(workspace_id=ws, title="Local one")
    board_id = board_models.default_board_id(ws)
    update_board_baseline_after_sync(realm_id, [board_id])

    board = store.get(board_id)
    remote_new = _remote_card(board_id, "card_remote", "From peer")
    subtree = _remote_subtree(tmp_path, board, [store.get_card(local.card_id), remote_new])

    before = _watermark()
    summary = apply_board_pull(realm_id, subtree)
    assert summary.adopted == 1

    created = [e for e in _events_since(before) if e.type == "board.card.created"]
    assert [e.payload["card_id"] for e in created] == ["card_remote"], [
        e.type for e in _events_since(before)
    ]
    assert created[0].payload["board_id"] == board_id
    assert created[0].payload["title"] == "From peer"
    # Provenance: the SYNC moved this row, not an operator. Hash-neutral
    # (``board_content_hash`` excludes ``updated_by``), so the baseline the pull
    # just recorded still keys off the remote CONTENT.
    assert store.get_card("card_remote").updated_by == "realm_sync"
    assert read_board_baseline(realm_id)[f"{board_id}:card:card_remote"] == (
        board_models.board_content_hash(remote_new)
    )


def test_pull_that_adopts_a_board_def_emits_the_board_event(tmp_path):
    """The def half of the same hole: a peer's column-taxonomy change rewrote
    ``board.json`` and emitted nothing, so the launcher's lanes moved under a
    core nobody rebuilt. ``change="realm_sync"`` is the attribution, matching
    ``adopt_remote_surface``'s spelling on the office side."""

    realm_id, ws = _make_realm_workspace()
    store = BoardStore()
    store.add_card(workspace_id=ws, title="Local one")
    board_id = board_models.default_board_id(ws)
    update_board_baseline_after_sync(realm_id, [board_id])

    remote_board = store.get(board_id)
    remote_board.title = "Renamed by a peer"
    subtree = _remote_subtree(tmp_path, remote_board, [])

    before = _watermark()
    apply_board_pull(realm_id, subtree)

    updated = [e for e in _events_since(before) if e.type == "board.updated"]
    assert [e.payload["board_id"] for e in updated] == [board_id], [
        e.type for e in _events_since(before)
    ]
    assert updated[0].payload["change"] == "realm_sync"
    assert store.get(board_id).title == "Renamed by a peer"
    # The peer's revision is adopted verbatim — no ``+1``. Renumbering here
    # would make the next classify read an untouched board as locally edited.
    assert store.get(board_id).revision == remote_board.revision


def test_adopting_a_board_a_member_has_never_had_emits_created(tmp_path):
    """A first arrival is a CREATE, not an edit — the same absence question
    ``adopt_remote_actor`` asks, answered under the same lock as the write."""

    from hermes_time import now

    realm_id, ws = _make_realm_workspace()
    store = BoardStore()
    store.ensure_default_board(ws)
    peer_board = board_models.default_board(
        "ws_peer_only", created_at=now(), updated_by="peer"
    )
    subtree = _remote_subtree(tmp_path, peer_board, [])

    before = _watermark()
    apply_board_pull(realm_id, subtree)

    created = [e for e in _events_since(before) if e.type == "board.created"]
    assert [e.payload["board_id"] for e in created] == [peer_board.board_id], [
        e.type for e in _events_since(before)
    ]
    assert created[0].payload["workspace_id"] == "ws_peer_only"


# ── H1: store-drift honesty (_board_store_drift) ──────────────────────────


def _realm_workspaces(realm_id: str):
    return _workspaces_for_realm(RealmStore().get(realm_id))


def test_board_store_drift_zero_when_baseline_matches_store():
    realm_id, ws = _make_realm_workspace()
    store = BoardStore()
    store.add_card(workspace_id=ws, title="One")
    board_id = board_models.default_board_id(ws)
    update_board_baseline_after_sync(realm_id, [board_id])  # baseline == current store

    drift = _board_store_drift(realm_id, _realm_workspaces(realm_id))
    assert drift == {"boards_changed": 0, "cards_changed": 0, "cards_added": 0, "cards_removed": 0}
    assert _any_store_drift({"boards": drift}) is False


def test_board_store_drift_counts_changed_and_added_cards():
    realm_id, ws = _make_realm_workspace()
    store = BoardStore()
    store.add_card(workspace_id=ws, title="Keep")
    changing = store.add_card(workspace_id=ws, title="Will change")
    board_id = board_models.default_board_id(ws)
    update_board_baseline_after_sync(realm_id, [board_id])

    store.edit_card(changing.card_id, title="Changed title")  # 1 changed card
    store.add_card(workspace_id=ws, title="Brand new")  # 1 added card (no baseline)

    drift = _board_store_drift(realm_id, _realm_workspaces(realm_id))
    assert drift["boards_changed"] == 0  # board def (columns/ledger) unchanged
    assert drift["cards_changed"] == 1
    assert drift["cards_added"] == 1
    assert drift["cards_removed"] == 0
    assert _any_store_drift({"boards": drift}) is True


def test_board_store_drift_counts_removed_card_and_board_ledger_change():
    realm_id, ws = _make_realm_workspace()
    store = BoardStore()
    store.add_card(workspace_id=ws, title="Keep")
    removing = store.add_card(workspace_id=ws, title="Remove me")
    board_id = board_models.default_board_id(ws)
    update_board_baseline_after_sync(realm_id, [board_id])

    store.archive_card(removing.card_id)  # baseline card no longer active locally

    drift = _board_store_drift(realm_id, _realm_workspaces(realm_id))
    assert drift["cards_removed"] == 1
    # archiving appends to the board's archived_card_ids ledger, which IS synced
    # board-def content → the board def is now unpublished too (honest).
    assert drift["boards_changed"] == 1


def test_board_store_drift_never_synced_counts_everything_unpublished():
    realm_id, ws = _make_realm_workspace()
    store = BoardStore()
    store.add_card(workspace_id=ws, title="A")
    store.add_card(workspace_id=ws, title="B")
    assert read_board_baseline(realm_id) == {}  # never published or pulled

    drift = _board_store_drift(realm_id, _realm_workspaces(realm_id))
    assert drift["boards_changed"] == 1  # board def has no baseline entry
    assert drift["cards_added"] == 2  # both cards have no baseline entry
    assert drift["cards_changed"] == 0
    assert drift["cards_removed"] == 0
    assert _any_store_drift({"boards": drift}) is True


def test_board_store_drift_excludes_boards_outside_realm():
    realm_id, ws = _make_realm_workspace()
    store = BoardStore()
    store.add_card(workspace_id=ws, title="In realm")
    board_id = board_models.default_board_id(ws)
    update_board_baseline_after_sync(realm_id, [board_id])  # in-realm board is clean

    # A second workspace NOT bound to this realm, with its own brand-new board.
    other_ws = WorkspaceStore().create(name="Other")
    store.add_card(workspace_id=other_ws.id, title="Out of realm")

    drift = _board_store_drift(realm_id, _realm_workspaces(realm_id))
    # The out-of-realm board's unpublished card must not count for this realm.
    assert drift == {"boards_changed": 0, "cards_changed": 0, "cards_added": 0, "cards_removed": 0}


# ── H2: snapshot honesty flag (_boards_summary) ───────────────────────────


def test_boards_summary_rows_carry_unpublished_flag_when_realm_bound():
    from agent_runtime.snapshot import _boards_summary

    realm_id, ws = _make_realm_workspace()
    store = BoardStore()
    synced = store.add_card(workspace_id=ws, title="Synced")
    board_id = board_models.default_board_id(ws)
    update_board_baseline_after_sync(realm_id, [board_id])  # synced card + board published
    drifted = store.add_card(workspace_id=ws, title="Not yet published")  # no baseline

    rows = _boards_summary(store, _realm_workspaces(realm_id)).boards
    board_row = next(r for r in rows if r["board_id"] == board_id)
    assert board_row["unpublished"] is False  # board def matches baseline
    by_id = {c["card_id"]: c for c in board_row["cards"]}
    assert by_id[synced.card_id]["unpublished"] is False
    assert by_id[drifted.card_id]["unpublished"] is True


def test_boards_summary_omits_unpublished_flag_without_realm():
    from agent_runtime.snapshot import _boards_summary

    store = BoardStore()
    ws = WorkspaceStore().create(name="Local only")  # no realm_id → not sync-bound
    store.add_card(workspace_id=ws.id, title="Local card")
    board_id = board_models.default_board_id(ws.id)

    rows = _boards_summary(store, [ws]).boards
    board_row = next(r for r in rows if r["board_id"] == board_id)
    assert "unpublished" not in board_row
    assert all("unpublished" not in c for c in board_row["cards"])


# ── ML-8b/5: an unreadable PULLED card cannot read as a peer delete ────────


def _blind_pulled_card(subtree: Path, board_id: str, card_id: str) -> Path:
    path = subtree / "store" / "boards" / board_id / "cards" / f"{card_id}.json"
    assert path.exists(), path
    path.write_text("{truncated", encoding="utf-8")
    return path


def test_an_unreadable_pulled_card_cannot_read_as_a_peer_delete(tmp_path):
    """The board twin of the office pull-read witness.

    *Probed:* ``unreadable_remote`` on the summary (driven 1 then 2), that no
    delete-shaped decision was taken for those ids (the ``delete_fenced``
    recorder names each), and that the cards are still active afterwards.

    *Mutation:* restore the bare ``continue`` in ``_read_remote_board``. The
    count the fixture drives never reaches the summary, the fence never engages,
    and the cards are archived on the strength of a parse error.
    """

    realm_id, ws = _make_realm_workspace()
    store = BoardStore()
    cards = [store.add_card(workspace_id=ws, title=f"card {i}") for i in range(3)]
    board_id = board_models.default_board_id(ws)
    update_board_baseline_after_sync(realm_id, [board_id])
    subtree = _remote_subtree(
        tmp_path, store.get(board_id), [store.get_card(c.card_id) for c in cards]
    )

    for driven in (1, 2):
        _blind_pulled_card(subtree, board_id, cards[driven - 1].card_id)
        summary = apply_board_pull(realm_id, subtree)
        assert summary.unreadable_remote == driven, summary.as_dict()
        assert summary.archived == 0, summary.as_dict()
        # Compared as a SET of ids: the pull iterates the card-id union in
        # sorted order, so creation order is not the guarantee here — membership
        # is. Asserting the list verbatim passed or failed on uuid luck.
        assert sorted(summary.delete_fenced, key=lambda row: row["card_id"]) == sorted(
            (
                {
                    "board_id": board_id,
                    "card_id": cards[i].card_id,
                    "reason": "unreadable_remote",
                    "unreadable_remote": driven,
                }
                for i in range(driven)
            ),
            key=lambda row: row["card_id"],
        ), summary.as_dict()
        active = {c.card_id for c in store.list_cards(board_id)}
        for i in range(driven):
            assert cards[i].card_id in active
            assert cards[i].card_id not in store.get(board_id).archived_card_ids
        # The held card keeps its baseline row, so a repaired remote converges
        # rather than arriving as a fresh adopt.
        assert f"{board_id}:card:{cards[0].card_id}" in read_board_baseline(realm_id)


def test_a_readable_remote_card_removal_still_archives_beside_a_fenced_one(tmp_path):
    """The discriminator that stops the fence from becoming "never archive": a
    remote board that reads COMPLETELY still propagates its removals."""

    realm_id, ws = _make_realm_workspace()
    store = BoardStore()
    keep = store.add_card(workspace_id=ws, title="Keep")
    drop = store.add_card(workspace_id=ws, title="Drop")
    board_id = board_models.default_board_id(ws)
    update_board_baseline_after_sync(realm_id, [board_id])

    subtree = _remote_subtree(tmp_path, store.get(board_id), [store.get_card(keep.card_id)])
    summary = apply_board_pull(realm_id, subtree)
    assert summary.unreadable_remote == 0
    assert summary.delete_fenced == []
    assert summary.archived == 1
    assert drop.card_id in store.get(board_id).archived_card_ids


def test_an_unreadable_remote_board_def_is_counted_not_skipped(tmp_path):
    """A directory whose ``board.json`` will not decode names no board, so
    nothing in it can be attributed — but it is a real remote board this pull
    did not apply, and the count says so instead of the directory vanishing."""

    realm_id, ws = _make_realm_workspace()
    store = BoardStore()
    card = store.add_card(workspace_id=ws, title="One")
    board_id = board_models.default_board_id(ws)
    subtree = _remote_subtree(tmp_path, store.get(board_id), [store.get_card(card.card_id)])
    (subtree / "store" / "boards" / board_id / "board.json").write_text(
        "{truncated", encoding="utf-8"
    )

    summary = apply_board_pull(realm_id, subtree)
    assert summary.unreadable_remote == 1, summary.as_dict()
    assert summary.boards == []
    assert summary.adopted == 0
