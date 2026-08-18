"""S1 Mission Board store/order/projection tests.

Store round-trip + event-per-mutation + revision guard; order-key properties;
default-board determinism; legacy goal-link field tolerance; bounded/redacted projection; parity warnings; serve fingerprint
invalidation. The autouse conftest fixtures isolate the runtime root per test.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from agent_runtime import board_models, board_order, paths
from agent_runtime.board_store import BoardStore
from agent_runtime.errors import NotFound, StaleRevision, SyncConflict
from agent_runtime.events import EventLog
from agent_runtime.snapshot import build_snapshot
from agent_runtime.store import WorkspaceStore


def _event_types() -> list[str]:
    return [evt.type for _, evt in EventLog().iter_from_offset(0)]


def _make_workspace(name: str = "Default") -> str:
    ws = WorkspaceStore().create(name=name)
    WorkspaceStore().set_active(ws.id)
    return ws.id


# ── order-key math ────────────────────────────────────────────────────────


def test_order_key_midpoint_and_bounds():
    start = board_order.allocate_between(None, None)
    after = board_order.allocate_between(start, None)
    assert start < after
    mid = board_order.allocate_between(start, after)
    assert start < mid < after


def test_order_key_adjacent_descends_not_collides():
    # adjacent single digits leave no room at this position → descend deeper
    mid = board_order.allocate_between("h", "i")
    assert "h" < mid < "i"


def test_order_key_out_of_order_raises():
    with pytest.raises(ValueError):
        board_order.allocate_between("q", "h")


def test_rebalance_is_strictly_increasing_and_unique():
    keys = board_order.rebalance(500)
    assert len(keys) == 500
    assert keys == sorted(keys)
    assert len(set(keys)) == 500


# ── default-board determinism ─────────────────────────────────────────────


def test_default_board_content_hash_is_machine_independent():
    a = board_models.default_board("ws_1", created_at=datetime(2020, 1, 1, tzinfo=timezone.utc), updated_by="alice")
    b = board_models.default_board("ws_1", created_at=datetime(2024, 6, 6, tzinfo=timezone.utc), updated_by="bob")
    # Timestamps + updated_by differ, but semantic content hash is identical.
    assert board_models.board_content_hash(a) == board_models.board_content_hash(b)
    # Different workspace → different hash.
    c = board_models.default_board("ws_2", created_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert board_models.board_content_hash(a) != board_models.board_content_hash(c)


def test_default_columns_have_fixed_ids_and_kinds():
    cols = board_models.default_board_columns()
    assert [c.column_id for c in cols] == ["col_queued", "col_active", "col_review", "col_done"]
    assert [c.kind for c in cols] == ["queued", "active", "review", "done"]


# ── store round-trip + events + revision guard ────────────────────────────


def test_add_move_edit_round_trip_emits_event_per_mutation():
    ws = _make_workspace()
    store = BoardStore()
    card = store.add_card(workspace_id=ws, title="Wire it", description="do it", priority="p1")
    assert card.column_id == "col_queued"
    assert card.created_by == "operator"
    moved = store.move_card(card.card_id, column_id="col_active")
    assert moved.column_id == "col_active"
    edited = store.edit_card(card.card_id, title="Wire it v2", expect_revision=moved.revision)
    assert edited.title == "Wire it v2"

    types = _event_types()
    for expected in ("board.created", "board.card.created", "board.card.moved", "board.card.edited"):
        assert expected in types, (expected, types)


def test_move_before_and_after_neighbor_semantics():
    ws = _make_workspace()
    store = BoardStore()
    a = store.add_card(workspace_id=ws, title="A")
    b = store.add_card(workspace_id=ws, title="B")
    c = store.add_card(workspace_id=ws, title="C")
    board_id = board_models.default_board_id(ws)
    ordered = [card.title for card in store.list_cards(board_id)]
    assert ordered == ["A", "B", "C"], ordered
    # move C to sit immediately AFTER A → order A, C, B
    store.move_card(c.card_id, column_id="col_queued", after=a.card_id)
    ordered = [card.title for card in store.list_cards(board_id)]
    assert ordered == ["A", "C", "B"], ordered
    # move A to sit immediately BEFORE B → order C, A, B
    store.move_card(a.card_id, column_id="col_queued", before=b.card_id)
    ordered = [card.title for card in store.list_cards(board_id)]
    assert ordered == ["C", "A", "B"], ordered


def test_lazy_default_board_created_once():
    ws = _make_workspace()
    store = BoardStore()
    board_id = board_models.default_board_id(ws)
    assert not store.exists(board_id)
    store.add_card(workspace_id=ws, title="First")
    assert store.exists(board_id)
    # second add does not re-emit board.created
    store.add_card(workspace_id=ws, title="Second")
    assert _event_types().count("board.created") == 1


def test_expect_revision_mismatch_raises_stale_revision():
    ws = _make_workspace()
    store = BoardStore()
    card = store.add_card(workspace_id=ws, title="Guarded")
    with pytest.raises(StaleRevision):
        store.edit_card(card.card_id, title="x", expect_revision=999)


def test_idempotent_add_replay_returns_same_card():
    ws = _make_workspace()
    store = BoardStore()
    a = store.add_card(workspace_id=ws, title="Idem", idempotency_key="k-1")
    b = store.add_card(workspace_id=ws, title="Idem", idempotency_key="k-1")
    assert a.card_id == b.card_id
    # only one card actually created
    assert len(store.list_cards(board_models.default_board_id(ws))) == 1


# ── archive / restore + resurrection ledger ───────────────────────────────


def test_archive_records_ledger_and_restore_clears_it():
    ws = _make_workspace()
    store = BoardStore()
    board_id = board_models.default_board_id(ws)
    card = store.add_card(workspace_id=ws, title="Temp")
    store.archive_card(card.card_id)
    board = store.get(board_id)
    assert card.card_id in board.archived_card_ids
    assert not paths.board_card_path(board_id, card.card_id).exists()
    assert paths.board_archived_card_path(board_id, card.card_id).exists()

    restored = store.restore_card(card.card_id)
    assert restored.state == "active"
    board = store.get(board_id)
    assert card.card_id not in board.archived_card_ids
    types = _event_types()
    assert "board.card.archived" in types and "board.card.restored" in types


# ── retired board → goal bridge compatibility ─────────────────────────────


def test_legacy_linked_goal_id_is_ignored_when_loading_a_card():
    """S3 removes the decorative goal link without breaking persisted cards."""

    ws = _make_workspace()
    store = BoardStore()
    card = store.add_card(workspace_id=ws, title="Legacy")
    path = paths.board_card_path(card.board_id, card.card_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["linked_goal_id"] = "goal_retired"
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = store.get_card(card.card_id)
    assert not hasattr(loaded, "linked_goal_id")


# ── bounded / redacted projection ─────────────────────────────────────────


def test_projection_masks_secrets_and_flags_truncation():
    ws = _make_workspace()
    store = BoardStore()
    long_desc = "x" * 5000
    store.add_card(workspace_id=ws, title="Big", description=long_desc)
    store.add_card(workspace_id=ws, title="Leak", description="api_key=DEADBEEFDEADBEEF12345 secret here")
    snap = build_snapshot()
    board = next(b for b in list(snap["boards"].values()) if b["workspace_id"] == ws)
    big = next(c for c in board["cards"] if c["title"] == "Big")
    assert big["description_truncated"] is True
    assert len(big["description"]) == 2048
    leak = next(c for c in board["cards"] if c["title"] == "Leak")
    assert "DEADBEEF" not in leak["description"]
    assert "[redacted]" in leak["description"]


def test_projection_parity_warnings_orphan_and_conflict():
    ws = _make_workspace()
    store = BoardStore()
    card = store.add_card(workspace_id=ws, title="A")
    # orphaned board: workspace_id that does not resolve
    store.create(workspace_id="ws_ghost", title="Ghost")
    # conflict sidecar
    board_id = board_models.default_board_id(ws)
    conflict = paths.board_conflict_path(board_id, card.card_id)
    conflict.parent.mkdir(parents=True, exist_ok=True)
    conflict.write_text("{}", encoding="utf-8")

    snap = build_snapshot()
    codes = {w.get("code") for w in (snap.get("parity", {}).get("warnings") or [])}
    assert "orphaned_board" in codes
    assert "board_card_conflict" in codes


# ── serve fingerprint invalidation ────────────────────────────────────────


def test_serve_fingerprint_flips_on_card_mutations():
    from hermes_cli.harness_parts.serve import _runtime_state_fingerprint

    ws = _make_workspace()
    store = BoardStore()
    card = store.add_card(workspace_id=ws, title="One")
    fp1 = _runtime_state_fingerprint()
    store.add_card(workspace_id=ws, title="Two")  # new file → dir mtime
    fp2 = _runtime_state_fingerprint()
    assert fp1 != fp2
    store.edit_card(card.card_id, title="One v2")  # in-place rewrite → file mtime/size
    fp3 = _runtime_state_fingerprint()
    assert fp2 != fp3


# ── conflict resolution ───────────────────────────────────────────────────


def test_resolve_conflict_take_local_clears_sidecar():
    ws = _make_workspace()
    store = BoardStore()
    card = store.add_card(workspace_id=ws, title="Contested")
    board_id = board_models.default_board_id(ws)
    conflict = paths.board_conflict_path(board_id, card.card_id)
    conflict.parent.mkdir(parents=True, exist_ok=True)
    conflict.write_text('{"card_id": "%s", "kind": "both_changed"}' % card.card_id, encoding="utf-8")
    # a mutation is blocked while the conflict is open
    with pytest.raises(SyncConflict):
        store.edit_card(card.card_id, title="nope")
    resolved = store.resolve_conflict(card.card_id, take="local")
    assert resolved.card_id == card.card_id
    assert not conflict.exists()
    assert "board.card.conflict_resolved" in _event_types()
    # mutation works again
    store.edit_card(card.card_id, title="ok now")


# ── ML-8b/4: the board lister's two classes, split ────────────────────────
#
# ``_scan_card_dir`` skips a card file it cannot decode. Two different things
# read that short list: the order-key ALLOCATOR (a writer — class (i), refuses
# typed) and the projection (a reader — class (ii), the count travels).


def _blind_card(board_id: str, card_id: str):
    path = paths.board_card_path(board_id, card_id)
    assert path.exists(), path
    path.write_text("{truncated", encoding="utf-8")
    return path


def _seeded_board(n: int = 3):
    """A workspace + default board carrying ``n`` cards in one column."""

    from agent_runtime.errors import CardsUnreadable  # noqa: F401 — import proof

    ws = _make_workspace("Boards")
    store = BoardStore()
    board = store.ensure_default_board(ws)
    cards = [store.add_card(board_id=board.board_id, title=f"card {i}") for i in range(n)]
    return store, board.board_id, cards


def test_an_unreadable_card_file_refuses_order_key_allocation_typed():
    """THE board write-guard witness (class (i)).

    Every order-key decision — append, move-between, column rebalance — is
    computed from the active-card list: the neighbour keys it brackets between
    and the keys a rebalance rewrites wholesale. A card the platform will not
    open is a card the allocator places ON TOP of.

    *Probed:* the typed code, the count carried in the refusal (driven 1 then
    2), and that NO new card file was written (the directory census is the
    recorder).

    *Mutation:* drop the guard from ``_ordering_cards`` (return the scan's cards
    unconditionally). The allocation lands, a new card file appears, and both
    probes red.
    """

    from agent_runtime.errors import CardsUnreadable

    store, board_id, cards = _seeded_board(3)
    cards_dir = paths.board_cards_dir(board_id)

    for driven in (1, 2):
        _blind_card(board_id, cards[driven - 1].card_id)
        before = sorted(p.name for p in cards_dir.glob("*.json"))

        with pytest.raises(CardsUnreadable) as refused:
            store.add_card(board_id=board_id, title="appended")
        assert refused.value.code == "cards_unreadable"
        assert f"unreadable={driven}" in str(refused.value), str(refused.value)

        # A move allocates against the same column and refuses identically.
        with pytest.raises(CardsUnreadable):
            store.move_card(cards[2].card_id, board_id=board_id, column_id="col_active")

        assert sorted(p.name for p in cards_dir.glob("*.json")) == before, (
            "the allocator wrote a card while it could not see the column"
        )
        # The unreadable file is left exactly as found, for an operator to repair.
        assert _blind_card.__name__  # helper kept honest
        assert paths.board_card_path(board_id, cards[0].card_id).read_text(
            encoding="utf-8"
        ) == "{truncated"


def test_a_readable_board_still_allocates_beside_an_unreadable_one():
    """The scope boundary. A refusal that fired on any unreadable card anywhere
    would freeze every board in the root — a worse failure than the one being
    prevented."""

    from agent_runtime.errors import CardsUnreadable

    store, blinded_board, cards = _seeded_board(2)
    other_ws = _make_workspace("Boards Two")
    other = store.ensure_default_board(other_ws)
    _blind_card(blinded_board, cards[0].card_id)

    with pytest.raises(CardsUnreadable):
        store.add_card(board_id=blinded_board, title="nope")
    added = store.add_card(board_id=other.board_id, title="fine")
    assert added.order_key


def test_the_board_projection_states_how_many_rows_it_could_not_read():
    """THE board projection witness (class (ii)): the counts TRAVEL.

    *Probed:* the row's ``cards_unreadable`` and the core's
    ``boards_unreadable``, each driven with two distinct values, while the
    readable rows still project.

    *Mutation:* drop the ``unreadable += 1`` accumulators in ``_scan_card_dir``
    / ``scan_all``. Constant zero cannot match two driven counts.
    """

    store, board_id, cards = _seeded_board(3)
    clean = build_snapshot()
    assert clean["boards"][board_id]["cards_unreadable"] == 0
    assert clean["boards_unreadable"] == 0
    assert clean["boards"][board_id]["active_card_count"] == 3

    for driven in (1, 2):
        _blind_card(board_id, cards[driven - 1].card_id)
        snapshot = build_snapshot()
        row = snapshot["boards"][board_id]
        assert row["cards_unreadable"] == driven, row["cards_unreadable"]
        # The rows that DID decode are still projected — the count is not
        # bought by dropping the board.
        assert row["active_card_count"] == 3 - driven

    # And a board whose own def will not decode is counted, not vanished.
    for driven, extra_ws in ((1, "Extra One"), (2, "Extra Two")):
        ws = _make_workspace(extra_ws)
        extra = store.ensure_default_board(ws)
        paths.board_def_path(extra.board_id).write_text("{truncated", encoding="utf-8")
        snapshot = build_snapshot()
        assert snapshot["boards_unreadable"] == driven, snapshot["boards_unreadable"]
        assert board_id in snapshot["boards"]


def test_a_board_with_an_unreadable_card_file_refuses_realm_publish_typed():
    """The board twin of the office publish refusal: publish copies card FILES
    verbatim and absences ARE removals, so a card that merely would not decode
    here becomes a card archived on every peer."""

    from agent_runtime.realm_sync import _resolve_artifacts_with_projection
    from agent_runtime.store import RealmStore

    realm = RealmStore().create(name="Realm")
    ws = WorkspaceStore().create(name="WS", realm_id=realm.id)
    realm = RealmStore().get(realm.id)
    realm.workspace_ids.append(ws.id)
    RealmStore().save(realm)
    store = BoardStore()
    board = store.ensure_default_board(ws.id)
    card = store.add_card(board_id=board.board_id, title="travels")

    def _board_paths() -> list[str]:
        resolved = _resolve_artifacts_with_projection(realm.id)
        return [a.relative_path for a in resolved.artifacts if a.kind in ("board", "board_card")]

    assert any(p.endswith(f"cards/{card.card_id}.json") for p in _board_paths()), _board_paths()

    _blind_card(board.board_id, card.card_id)
    resolved = _resolve_artifacts_with_projection(realm.id)
    assert list(resolved.board_refused) == [
        {"board_id": board.board_id, "reason": "sync_unknowable", "unreadable": 1}
    ], resolved.board_refused
    assert [a.relative_path for a in resolved.artifacts if a.kind in ("board", "board_card")] == []
