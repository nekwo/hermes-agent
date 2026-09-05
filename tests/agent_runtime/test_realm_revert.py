"""``hermes harness realm sync revert`` — the per-item revert-to-upstream lane.

Plan: ``docs/mission_control/planned/realm-sync-local-changes-resolution.md``
(launcher repo) Stage H. Three properties carry the design and each has its own
section below: the PURE transition table, the ruling that a revert never mints a
realm-visible tombstone (§AX7 authored-vs-diagnostic), and the accounting —
counts derived from the itemized rows, one walk, and a baseline realigned so
drift reads zero without lying.

Autouse conftest fixtures isolate the runtime root.
"""

from __future__ import annotations

import json

import pytest

from utils import atomic_json_write

from agent_runtime import board_models, office_models, paths
from agent_runtime.board_store import BoardStore
from agent_runtime.board_sync import read_board_baseline, update_board_baseline_after_sync
from agent_runtime.office_store import OfficeStore
from agent_runtime.office_sync import read_office_baseline, update_office_baseline_after_sync
from agent_runtime.realm_revert import (
    FAMILIES,
    OUTCOME_ARCHIVED_LOCAL_ONLY,
    OUTCOME_BASELINE_DROPPED,
    OUTCOME_RESTORED,
    OUTCOME_REVERTED,
    REFUSED_ADMISSION,
    REFUSED_NO_UPSTREAM,
    REFUSED_UNKNOWN_ITEM,
    REFUSED_UNREADABLE_UPSTREAM,
    RevertAction,
    classify_revert,
    revert_realm_sync,
)
from agent_runtime.realm_sync import (
    DRIFT_FAMILY_BOARD,
    DRIFT_FAMILY_BOARD_CARD,
    DRIFT_FAMILY_FLOW_GRAPH,
    DRIFT_FAMILY_OFFICE_ACTOR,
    DRIFT_FAMILY_OFFICE_SURFACE,
    DRIFT_FAMILY_PERSONA_INSTANCE,
    DRIFT_KIND_ADDED,
    DRIFT_KIND_CHANGED,
    DRIFT_KIND_REMOVED,
    RealmSyncError,
    _board_store_drift,
    _office_store_drift,
    _workspaces_for_realm,
    store_drift_items,
)
from agent_runtime.serde import to_jsonable
from agent_runtime.store import RealmStore, WorkspaceStore


# ── fixture: a realm, a workspace, and a local "last-pulled" subtree ───────


def _make_realm_workspace(tmp_path) -> tuple[str, str]:
    realm = RealmStore().create(name="Realm")
    ws = WorkspaceStore().create(name="WS", realm_id=realm.id)
    realm = RealmStore().get(realm.id)
    realm.workspace_ids.append(ws.id)
    # A LOCAL path ref, so ``_sync_repo_path`` resolves here and no clone,
    # fetch or credential is ever in play — the lane under test is local-only.
    realm.sync_manifest_ref = str(tmp_path / "sync_repo")
    RealmStore().save(realm)
    WorkspaceStore().set_active(ws.id)
    return realm.id, ws.id


def _subtree(realm_id: str, tmp_path):
    path = tmp_path / "sync_repo" / "realms" / paths.safe_path_token(realm_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _publish_office_to_subtree(subtree, ws: str) -> None:
    """Mirror what a publish would have pushed for this workspace: the surface
    and every ACTIVE actor, as they stand right now."""

    import shutil

    store = OfficeStore()
    office_dir = subtree / "store" / "office" / paths.safe_path_token(ws)
    if office_dir.exists():
        shutil.rmtree(office_dir)
    atomic_json_write(office_dir / "office.json", to_jsonable(store.get_surface(ws)), indent=2, sort_keys=True)
    for actor in store.scan_actors(ws).actors:
        atomic_json_write(
            office_dir / "actors" / f"{office_models.actor_file_token(actor.actor_key)}.json",
            to_jsonable(actor),
            indent=2,
            sort_keys=True,
        )


def _publish_board_to_subtree(subtree, board_id: str) -> None:
    import shutil

    store = BoardStore()
    board_dir = subtree / "store" / "boards" / paths.safe_path_token(board_id)
    if board_dir.exists():
        shutil.rmtree(board_dir)
    atomic_json_write(board_dir / "board.json", to_jsonable(store.get(board_id)), indent=2, sort_keys=True)
    for card in store.list_cards(board_id):
        atomic_json_write(
            board_dir / "cards" / f"{paths.safe_path_token(card.card_id)}.json",
            to_jsonable(card),
            indent=2,
            sort_keys=True,
        )


def _payload(persona_id: str = "dev", x: float = 1.0) -> dict:
    return {
        "persona_id": persona_id,
        "items": [
            {
                "item_id": persona_id,
                "persona_id": persona_id,
                "kind": "agent",
                "position": [x, 2.0],
                "folder": "Agents",
            }
        ],
    }


def _drift(realm_id: str):
    return store_drift_items(realm_id, _workspaces_for_realm(RealmStore().get(realm_id)))


def _counts(realm_id: str) -> dict:
    workspaces = _workspaces_for_realm(RealmStore().get(realm_id))
    return {
        "boards": _board_store_drift(realm_id, workspaces),
        "office": _office_store_drift(realm_id, workspaces),
    }


def _synced_office(tmp_path) -> tuple[str, str, object]:
    """A realm whose office is PUBLISHED and whose subtree matches it — the
    starting point every drift shape below diverges from."""

    realm_id, ws = _make_realm_workspace(tmp_path)
    subtree = _subtree(realm_id, tmp_path)
    OfficeStore().upsert_actor(ws, _payload("dev"))
    update_office_baseline_after_sync(realm_id, [ws])
    _publish_office_to_subtree(subtree, ws)
    return realm_id, ws, subtree


# ── the pure transition table ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "family,kind,upstream_present,action,outcome",
    [
        # rows (office_actor / board_card): the only families with a
        # local-only archive lane.
        (DRIFT_FAMILY_OFFICE_ACTOR, DRIFT_KIND_REMOVED, True, RevertAction.RESTORE, OUTCOME_RESTORED),
        (DRIFT_FAMILY_OFFICE_ACTOR, DRIFT_KIND_REMOVED, False, RevertAction.DROP_BASELINE, OUTCOME_BASELINE_DROPPED),
        (DRIFT_FAMILY_OFFICE_ACTOR, DRIFT_KIND_CHANGED, True, RevertAction.ADOPT, OUTCOME_REVERTED),
        (DRIFT_FAMILY_OFFICE_ACTOR, DRIFT_KIND_CHANGED, False, RevertAction.DROP_BASELINE, OUTCOME_BASELINE_DROPPED),
        (DRIFT_FAMILY_OFFICE_ACTOR, DRIFT_KIND_ADDED, True, RevertAction.ADOPT, OUTCOME_REVERTED),
        (DRIFT_FAMILY_OFFICE_ACTOR, DRIFT_KIND_ADDED, False, RevertAction.ARCHIVE_LOCAL, OUTCOME_ARCHIVED_LOCAL_ONLY),
        (DRIFT_FAMILY_BOARD_CARD, DRIFT_KIND_REMOVED, True, RevertAction.RESTORE, OUTCOME_RESTORED),
        (DRIFT_FAMILY_BOARD_CARD, DRIFT_KIND_REMOVED, False, RevertAction.DROP_BASELINE, OUTCOME_BASELINE_DROPPED),
        (DRIFT_FAMILY_BOARD_CARD, DRIFT_KIND_CHANGED, True, RevertAction.ADOPT, OUTCOME_REVERTED),
        (DRIFT_FAMILY_BOARD_CARD, DRIFT_KIND_CHANGED, False, RevertAction.DROP_BASELINE, OUTCOME_BASELINE_DROPPED),
        (DRIFT_FAMILY_BOARD_CARD, DRIFT_KIND_ADDED, True, RevertAction.ADOPT, OUTCOME_REVERTED),
        (DRIFT_FAMILY_BOARD_CARD, DRIFT_KIND_ADDED, False, RevertAction.ARCHIVE_LOCAL, OUTCOME_ARCHIVED_LOCAL_ONLY),
        # containers (board def / office surface): no local-only archive lane,
        # so "no upstream to revert to" is a REFUSAL, never a delete.
        (DRIFT_FAMILY_OFFICE_SURFACE, DRIFT_KIND_CHANGED, True, RevertAction.ADOPT, OUTCOME_REVERTED),
        (DRIFT_FAMILY_OFFICE_SURFACE, DRIFT_KIND_CHANGED, False, RevertAction.DROP_BASELINE, OUTCOME_BASELINE_DROPPED),
        (DRIFT_FAMILY_OFFICE_SURFACE, DRIFT_KIND_ADDED, True, RevertAction.ADOPT, OUTCOME_REVERTED),
        (DRIFT_FAMILY_OFFICE_SURFACE, DRIFT_KIND_ADDED, False, RevertAction.REFUSE, REFUSED_NO_UPSTREAM),
        (DRIFT_FAMILY_BOARD, DRIFT_KIND_CHANGED, True, RevertAction.ADOPT, OUTCOME_REVERTED),
        (DRIFT_FAMILY_BOARD, DRIFT_KIND_CHANGED, False, RevertAction.DROP_BASELINE, OUTCOME_BASELINE_DROPPED),
        (DRIFT_FAMILY_BOARD, DRIFT_KIND_ADDED, True, RevertAction.ADOPT, OUTCOME_REVERTED),
        (DRIFT_FAMILY_BOARD, DRIFT_KIND_ADDED, False, RevertAction.REFUSE, REFUSED_NO_UPSTREAM),
    ],
)
def test_transition_table(family, kind, upstream_present, action, outcome):
    decision = classify_revert(family=family, kind=kind, upstream_present=upstream_present)
    assert (decision.action, decision.outcome) == (action, outcome)


def test_an_added_row_the_realm_already_has_is_adopted_not_archived():
    """The one row of the table that is easy to get wrong. "No baseline entry"
    says what THIS install last published, not what the realm holds — so an
    ``added`` row the subtree also carries is a revert TO upstream, and
    archiving it would delete a row the realm actually has."""

    assert classify_revert(
        family=DRIFT_FAMILY_OFFICE_ACTOR, kind=DRIFT_KIND_ADDED, upstream_present=True
    ).action is RevertAction.ADOPT


# ── the live-measured shape: a baseline actor archived locally ────────────


def test_restores_the_actor_the_census_archived_and_zeroes_the_drift(tmp_path):
    """The 2026-08-31 live shape, end to end: ``offices_changed 1,
    actors_removed 1`` for one baseline actor archived locally and never
    published. Before this lane the operator's only exit was Publish."""

    realm_id, ws, _subtree_path = _synced_office(tmp_path)
    store = OfficeStore()
    store.remove_actor(ws, "dev")  # the census's archive — tombstone and all
    assert _counts(realm_id)["office"] == {
        "offices_changed": 1,
        "actors_changed": 0,
        "actors_added": 0,
        "actors_removed": 1,
    }

    result = revert_realm_sync(realm_id, revert_all=True)

    outcomes = {(row["family"], row["outcome"]) for row in result["items"]}
    assert (DRIFT_FAMILY_OFFICE_ACTOR, OUTCOME_RESTORED) in outcomes
    assert (DRIFT_FAMILY_OFFICE_SURFACE, OUTCOME_REVERTED) in outcomes
    assert store.actor_exists(ws, "dev")
    # The desk is back AND the resurrection guard let go of it — a live row
    # still named in the ledger would publish a tombstone for itself.
    assert "dev" not in store.get_surface(ws).archived_actor_keys
    assert result["store_drift_after"]["office"] == {
        "offices_changed": 0,
        "actors_changed": 0,
        "actors_added": 0,
        "actors_removed": 0,
    }
    assert _counts(realm_id)["office"]["actors_removed"] == 0


def test_a_changed_actor_is_overwritten_with_the_subtree_copy(tmp_path):
    realm_id, ws, _ = _synced_office(tmp_path)
    store = OfficeStore()
    store.upsert_actor(ws, _payload("dev", x=99.0))  # local edit
    assert _counts(realm_id)["office"]["actors_changed"] == 1

    result = revert_realm_sync(realm_id, item_specs=[f"office_actor:{ws}:dev"])

    assert [row["outcome"] for row in result["items"]] == [OUTCOME_REVERTED]
    assert store.get_actor(ws, "dev").items[0].position[0] == 1.0
    assert _counts(realm_id)["office"]["actors_changed"] == 0


# ── the ruling: a revert NEVER mints a realm-visible tombstone ────────────


def test_reverting_a_local_only_actor_archives_without_a_tombstone(tmp_path):
    """§AX7 authored-vs-diagnostic. ``archived_actor_keys`` is the ledger that
    CROSSES MACHINES (``adopt_remote_surface`` merges it), so a tombstone minted
    from a local-only revert removes the desk on every peer to clean up one
    install's projection."""

    realm_id, ws, subtree = _synced_office(tmp_path)
    store = OfficeStore()
    store.upsert_actor(ws, _payload("scratch"))  # local-only, never published
    _publish_office_to_subtree(subtree, ws)  # …but upstream must NOT have it
    import shutil

    shutil.rmtree(subtree / "store" / "office")
    _publish_office_to_subtree(subtree, ws)
    (subtree / "store" / "office" / paths.safe_path_token(ws) / "actors" / "scratch.json").unlink()

    result = revert_realm_sync(realm_id, item_specs=[f"office_actor:{ws}:scratch"])

    assert [row["outcome"] for row in result["items"]] == [OUTCOME_ARCHIVED_LOCAL_ONLY]
    assert not store.actor_exists(ws, "scratch")
    # archive-never-delete: the copy is on disk and ``actor-restore`` reaches it
    assert paths.office_archived_actor_path(ws, "scratch").exists()
    # THE guarantee.
    assert "scratch" not in store.get_surface(ws).archived_actor_keys


def test_reverting_a_local_only_card_archives_without_a_tombstone(tmp_path):
    """The board twin of the guarantee above: ``archived_card_ids`` rides the
    published board def, so it is realm-visible for the same reason."""

    realm_id, ws = _make_realm_workspace(tmp_path)
    subtree = _subtree(realm_id, tmp_path)
    store = BoardStore()
    store.add_card(workspace_id=ws, title="Published")
    board_id = board_models.default_board_id(ws)
    update_board_baseline_after_sync(realm_id, [board_id])
    _publish_board_to_subtree(subtree, board_id)
    added = store.add_card(workspace_id=ws, title="Local only")
    assert _counts(realm_id)["boards"]["cards_added"] == 1

    result = revert_realm_sync(realm_id, item_specs=[f"board_card:{board_id}:{added.card_id}"])

    assert [row["outcome"] for row in result["items"]] == [OUTCOME_ARCHIVED_LOCAL_ONLY]
    assert paths.board_archived_card_path(board_id, added.card_id).exists()
    assert added.card_id not in store.get(board_id).archived_card_ids
    # …and the board def therefore did not drift into "unpublished" either.
    assert _counts(realm_id)["boards"] == {
        "boards_changed": 0,
        "cards_changed": 0,
        "cards_added": 0,
        "cards_removed": 0,
    }


def test_reverting_an_edited_card_writes_through_the_stores_evented_door(tmp_path):
    """RED-FIRST (2026-09-02): this lane's ``_adopt_from_upstream`` wrote board
    rows with a raw ``atomic_json_write`` — matching the pull arm, which did the
    same — so a revert that PUT a card back emitted nothing, while a revert that
    archived one emitted ``board.card.archived`` through ``archive_card``.

    What this pins is the module's own promise ("A revert writes nothing a pull
    could not have written", and the ``REVERT_EVENT_TYPE`` note beside it: a live
    subscriber that never heard would render rows the store no longer has). Now
    that the pull routes through ``BoardStore.adopt_remote_card``, so does this
    — and the attribution stays this lane's own ``realm_sync_revert``, which is
    the whole reason ``REVERT_ACTOR_REF`` exists.
    """

    from agent_runtime.events import EventLog
    from agent_runtime.realm_revert import REVERT_ACTOR_REF

    realm_id, ws = _make_realm_workspace(tmp_path)
    subtree = _subtree(realm_id, tmp_path)
    store = BoardStore()
    card = store.add_card(workspace_id=ws, title="Upstream title")
    board_id = board_models.default_board_id(ws)
    update_board_baseline_after_sync(realm_id, [board_id])
    _publish_board_to_subtree(subtree, board_id)
    store.edit_card(card.card_id, title="Local edit nobody wanted")

    before = max((offset for offset, _ in EventLog().iter_from_offset(0)), default=0)
    result = revert_realm_sync(realm_id, item_specs=[f"board_card:{board_id}:{card.card_id}"])

    assert [row["outcome"] for row in result["items"]] == [OUTCOME_REVERTED]
    assert store.get_card(card.card_id).title == "Upstream title"

    events = [event for _, event in EventLog().iter_from_offset(before)]
    edited = [e for e in events if e.type == "board.card.edited"]
    assert [e.payload["card_id"] for e in edited] == [card.card_id], [
        e.type for e in events
    ]
    assert store.get_card(card.card_id).updated_by == REVERT_ACTOR_REF


# ── dry run, idempotence, and the subtree-absent fallback ─────────────────


def test_dry_run_writes_nothing(tmp_path):
    realm_id, ws, _ = _synced_office(tmp_path)
    OfficeStore().remove_actor(ws, "dev")
    before_baseline = read_office_baseline(realm_id)
    before_surface = json.loads(paths.office_surface_path(ws).read_text(encoding="utf-8"))

    result = revert_realm_sync(realm_id, revert_all=True, dry_run=True)

    assert result["dry_run"] is True
    assert result["reverted"] == 2  # what it WOULD do, reported honestly
    assert not OfficeStore().actor_exists(ws, "dev")  # …and did not do
    assert read_office_baseline(realm_id) == before_baseline
    assert json.loads(paths.office_surface_path(ws).read_text(encoding="utf-8")) == before_surface
    assert _counts(realm_id)["office"]["actors_removed"] == 1


def test_second_pass_is_a_no_op(tmp_path):
    realm_id, ws, _ = _synced_office(tmp_path)
    OfficeStore().remove_actor(ws, "dev")
    first = revert_realm_sync(realm_id, revert_all=True)
    assert first["reverted"] == 2

    second = revert_realm_sync(realm_id, revert_all=True)

    assert second["count"] == 0 and second["reverted"] == 0 and second["items"] == []
    assert second["store_drift_after"]["office"]["actors_removed"] == 0


def test_a_baseline_entry_with_no_subtree_artifact_is_dropped(tmp_path):
    """Accounted, never silent: the baseline claims a publish the subtree cannot
    back, so the stale entry goes and the item stops counting."""

    realm_id, ws, subtree = _synced_office(tmp_path)
    OfficeStore().remove_actor(ws, "dev")
    (subtree / "store" / "office" / paths.safe_path_token(ws) / "actors" / "dev.json").unlink()

    result = revert_realm_sync(realm_id, item_specs=[f"office_actor:{ws}:dev"])

    assert [row["outcome"] for row in result["items"]] == [OUTCOME_BASELINE_DROPPED]
    assert f"{ws}:actor:dev" not in read_office_baseline(realm_id)
    assert result["store_drift_after"]["office"]["actors_removed"] == 0


def test_an_item_that_is_not_drifting_is_refused_not_guessed_at(tmp_path):
    realm_id, ws, _ = _synced_office(tmp_path)

    result = revert_realm_sync(realm_id, item_specs=[f"office_actor:{ws}:nobody"])

    assert [row["outcome"] for row in result["items"]] == [REFUSED_UNKNOWN_ITEM]
    assert result["reverted"] == 0


def test_a_missing_sync_clone_refuses_typed(tmp_path):
    """The refusal that keeps the ``added`` arm honest: with no subtree, EVERY
    row would read as "upstream does not have this" and a ``--all`` would
    archive the operator's whole office."""

    realm_id, ws = _make_realm_workspace(tmp_path)
    OfficeStore().upsert_actor(ws, _payload("dev"))

    with pytest.raises(RealmSyncError) as excinfo:
        revert_realm_sync(realm_id, revert_all=True)
    assert excinfo.value.code == "sync_repo_missing"
    assert OfficeStore().actor_exists(ws, "dev")


def test_selection_must_be_unambiguous(tmp_path):
    realm_id, ws, _ = _synced_office(tmp_path)
    with pytest.raises(RealmSyncError) as both:
        revert_realm_sync(realm_id, revert_all=True, item_specs=[f"office_actor:{ws}:dev"])
    assert both.value.code == "invalid_request"
    with pytest.raises(RealmSyncError) as neither:
        revert_realm_sync(realm_id)
    assert neither.value.code == "invalid_request"


def test_a_malformed_item_selector_is_a_request_fault(tmp_path):
    realm_id, _ws, _ = _synced_office(tmp_path)
    with pytest.raises(RealmSyncError) as excinfo:
        revert_realm_sync(realm_id, item_specs=["office_actor:missing_key"])
    assert excinfo.value.code == "invalid_request"


def test_an_undecodable_subtree_artifact_is_refused_not_read_as_absence(tmp_path):
    """Absence is what drives the two delete-shaped arms (archive local-only,
    drop the baseline), so a parse error must never arrive there as ``False``.
    The pull fences its deletes on exactly this fact; so does the revert."""

    realm_id, ws, subtree = _synced_office(tmp_path)
    OfficeStore().remove_actor(ws, "dev")
    actor_file = subtree / "store" / "office" / paths.safe_path_token(ws) / "actors" / "dev.json"
    actor_file.write_text("{not json", encoding="utf-8")

    result = revert_realm_sync(realm_id, item_specs=[f"office_actor:{ws}:dev"])

    assert [row["outcome"] for row in result["items"]] == [REFUSED_UNREADABLE_UPSTREAM]
    # untouched: the baseline entry stays, so a repaired subtree still converges
    assert f"{ws}:actor:dev" in read_office_baseline(realm_id)
    assert result["store_drift_after"]["office"]["actors_removed"] == 1


def test_the_admission_door_holds_against_the_subtree(tmp_path):
    """A revert adopts bytes this machine did not author, so it passes the same
    ``sync_admission`` door the pull holds — not a second one beside it."""

    realm_id, ws, subtree = _synced_office(tmp_path)
    OfficeStore().upsert_actor(ws, _payload("dev", x=99.0))  # local edit → changed
    actor_file = subtree / "store" / "office" / paths.safe_path_token(ws) / "actors" / "dev.json"
    payload = json.loads(actor_file.read_text(encoding="utf-8"))
    payload["backing_profile"] = "C:\\Users\\somebody\\.hermes"  # machine-shaped
    atomic_json_write(actor_file, payload, indent=2, sort_keys=True)

    result = revert_realm_sync(realm_id, item_specs=[f"office_actor:{ws}:dev"])

    assert [row["outcome"] for row in result["items"]] == [REFUSED_ADMISSION]
    assert result["items"][0]["detail"] == "nonportable_path"
    assert OfficeStore().get_actor(ws, "dev").items[0].position[0] == 99.0  # untouched


def test_a_repo_without_this_realms_subtree_refuses_too(tmp_path):
    """The clone can exist and still hold nothing for THIS realm — the same
    "upstream has nothing" misreading, one directory down."""

    realm_id, ws = _make_realm_workspace(tmp_path)
    (tmp_path / "sync_repo").mkdir(parents=True, exist_ok=True)
    OfficeStore().upsert_actor(ws, _payload("dev"))

    with pytest.raises(RealmSyncError) as excinfo:
        revert_realm_sync(realm_id, revert_all=True)
    assert excinfo.value.code == "sync_repo_missing"
    assert excinfo.value.safe_details["missing"] == "realm_subtree"
    assert OfficeStore().actor_exists(ws, "dev")


# ── the accounting: counts are DERIVED from the itemized rows ─────────────


def test_counts_are_derived_from_the_items_one_walk(tmp_path):
    """The property, not a fixed example: every count equals the number of rows
    it names. Two independent walks could disagree; a derivation cannot."""

    realm_id, ws, _ = _synced_office(tmp_path)
    office = OfficeStore()
    office.upsert_actor(ws, _payload("changed_one"))
    office.upsert_actor(ws, _payload("removed_one"))
    update_office_baseline_after_sync(realm_id, [ws])
    office.upsert_actor(ws, _payload("changed_one", x=42.0))
    office.upsert_actor(ws, _payload("added_one"))
    office.remove_actor(ws, "removed_one")
    boards = BoardStore()
    boards.add_card(workspace_id=ws, title="Card")

    items = _drift(realm_id)
    counts = _counts(realm_id)

    def rows(family, kind=None):
        return [i for i in items if i.family == family and (kind is None or i.kind == kind)]

    assert counts["office"]["actors_changed"] == len(rows(DRIFT_FAMILY_OFFICE_ACTOR, DRIFT_KIND_CHANGED)) == 1
    assert counts["office"]["actors_added"] == len(rows(DRIFT_FAMILY_OFFICE_ACTOR, DRIFT_KIND_ADDED)) == 1
    assert counts["office"]["actors_removed"] == len(rows(DRIFT_FAMILY_OFFICE_ACTOR, DRIFT_KIND_REMOVED)) == 1
    assert counts["office"]["offices_changed"] == len(rows(DRIFT_FAMILY_OFFICE_SURFACE)) == 1
    assert counts["boards"]["cards_added"] == len(rows(DRIFT_FAMILY_BOARD_CARD, DRIFT_KIND_ADDED)) == 1
    assert counts["boards"]["boards_changed"] == len(rows(DRIFT_FAMILY_BOARD)) == 1
    # Every row lands in exactly one counter — so the totals cannot drift apart.
    assert sum(counts["office"].values()) + sum(counts["boards"].values()) == len(items)


def test_status_carries_the_items_beside_the_unchanged_counts(tmp_path, monkeypatch):
    """The wire contract the launcher parses: ``store_drift.items`` is ADDITIVE
    and the four-key count shapes are untouched."""

    from agent_runtime import realm_sync

    realm_id, ws, _ = _synced_office(tmp_path)
    OfficeStore().remove_actor(ws, "dev")
    # Status fetches/clones; this lane's subject is the drift block, so the git
    # half is stubbed rather than reimplemented here.
    monkeypatch.setattr(realm_sync, "_ensure_sync_repo", lambda realm, credential=None: tmp_path / "sync_repo")
    monkeypatch.setattr(realm_sync, "_refresh_remote_tracking", lambda repo, credential=None: {"checked": False, "error": None})
    monkeypatch.setattr(realm_sync, "_git_state", lambda repo: {"ahead": 0, "behind": 0, "conflicts": [], "dirty": False})

    status = realm_sync.realm_sync_status(realm_id)

    drift = status["store_drift"]
    assert set(drift["office"]) == {"offices_changed", "actors_changed", "actors_added", "actors_removed"}
    assert drift["office"]["actors_removed"] == 1
    assert {
        "family": DRIFT_FAMILY_OFFICE_ACTOR,
        "container": ws,
        "item_key": "dev",
        "kind": DRIFT_KIND_REMOVED,
    } in drift["items"]
    assert status["unpublished_changes"] is True


# ── the CANVAS family (w13/h2 replication, revert arm w17/hb) ─────────────
#
# The canvas joined the drift set in the same change as this arm, and that
# ordering IS the design: ``revert_realm_sync`` subscripts
# ``_PROCESS_ORDER[row.family]`` and dispatches on family for the upstream
# lookup, the baseline and the store door, so a drift family with no arm here
# hands ``revert --all`` a ``KeyError`` and offers the operator an exit that
# does not exist.

CANVAS_INSTANCE_ID = "personainst_dev_agent_9682caf4"
#: What the launcher ASKS for. ``parse_flow_graph_doc`` runs it through
#: ``safe_assignment_token``, which rewrites the ``:`` separator, so the id the
#: store, the projection, the baseline and therefore the drift row all carry is
#: the underscore spelling below. Both are here because both are real: a test
#: that only knew one would pass against a lane that had lost the other.
CANVAS_GRAPH_ID = f"runtime:{CANVAS_INSTANCE_ID}"
CANVAS_STORED_GRAPH_ID = f"runtime_{CANVAS_INSTANCE_ID}"


def _desk_with_instance(tmp_path):
    """A realm whose one desk names a persona INSTANCE — the canvas's owner.

    The canvas projection is scoped to ``_office_publish_scan(...).instance_ids``
    (the desks a publish already ships), so an actor with no
    ``persona_instance_id`` has no canvas to publish and no row to revert.
    """

    realm_id, ws = _make_realm_workspace(tmp_path)
    subtree = _subtree(realm_id, tmp_path)
    OfficeStore().upsert_actor(
        ws,
        {
            "persona_id": "dev",
            "persona_instance_id": CANVAS_INSTANCE_ID,
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
    return realm_id, ws, subtree


def _store_canvas(x: int = 10) -> None:
    from agent_runtime.flow_graph import FlowGraphStore, parse_flow_graph_doc

    FlowGraphStore().set_doc(
        parse_flow_graph_doc(
            {
                "graph_id": CANVAS_GRAPH_ID,
                "nodes": [{"id": "n_owner", "agent": CANVAS_INSTANCE_ID, "x": x, "y": 2}],
                "edges": [],
            }
        ),
        requested_by="operator",
    )


def _publish_canvas(realm_id: str, subtree):
    """Write the projection into the last-pulled subtree AND record the
    baseline — the two halves a real publish does, so the drift walk starts at
    zero the way it does after one."""

    from agent_runtime.flow_graph_sync import update_flow_graph_baseline_after_publish
    from agent_runtime.realm_sync import _resolve_artifacts_with_projection

    projection = _resolve_artifacts_with_projection(realm_id).flow_graph_projection
    path = subtree / "store" / "flow_graphs.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(projection.to_bytes())
    update_flow_graph_baseline_after_publish(realm_id, projection)
    return projection


def _canvas_x():
    from agent_runtime.flow_graph import FlowGraphStore

    stored = FlowGraphStore().get(CANVAS_GRAPH_ID)
    return stored["doc"]["nodes"][0]["x"]


def _canvas_rows(realm_id):
    return [item for item in _drift(realm_id) if item.family == DRIFT_FAMILY_FLOW_GRAPH]


def test_every_drift_family_the_walk_can_produce_has_a_revert_arm():
    """The invariant the canvas row was blocked on, stated once.

    ``_PROCESS_ORDER`` is subscripted directly for every selected row, so a
    family present in the walk and absent here is not a missing feature — it is
    a ``KeyError`` in the middle of a ``--all`` pass, after earlier rows have
    already been written. Asserting the SETS is what makes the next family's
    author trip here rather than in an operator's store.
    """

    from agent_runtime.realm_revert import _PROCESS_ORDER

    assert set(_PROCESS_ORDER) == FAMILIES
    assert DRIFT_FAMILY_FLOW_GRAPH in FAMILIES


def test_a_canvas_is_reverted_after_the_agents_its_nodes_bind():
    """Ordering, as a relationship rather than a snapshot of two integers.

    The pull runs ``apply_flow_graph_pull`` after ``apply_persona_instance_pull``
    because owner-liveness reaping archives a drawing whose owner is gone, and a
    canvas restored before its owner instance looks exactly like that. A
    ``--all`` revert carries both families in one pass, and without this the
    sort key would order them by family NAME — ``flow_graph`` before
    ``persona_instance``.
    """

    from agent_runtime.realm_revert import _PROCESS_ORDER

    assert _PROCESS_ORDER[DRIFT_FAMILY_FLOW_GRAPH] > _PROCESS_ORDER[DRIFT_FAMILY_PERSONA_INSTANCE]


def test_an_edited_canvas_is_reverted_to_the_published_drawing(tmp_path):
    realm_id, _, subtree = _desk_with_instance(tmp_path)
    _store_canvas(x=10)
    _publish_canvas(realm_id, subtree)
    assert _canvas_rows(realm_id) == []

    _store_canvas(x=99)
    (item,) = _canvas_rows(realm_id)
    assert item.kind == DRIFT_KIND_CHANGED
    assert item.item_key == CANVAS_STORED_GRAPH_ID
    assert item.container == CANVAS_INSTANCE_ID

    result = revert_realm_sync(realm_id, item_specs=[item.spec])

    assert [row["outcome"] for row in result["items"]] == [OUTCOME_REVERTED]
    assert _canvas_x() == 10
    # The baseline was realigned from the store's own post-write content, so a
    # second status reads zero without anything being republished.
    assert _canvas_rows(realm_id) == []


def test_a_reaped_canvas_is_restored_from_the_published_drawing(tmp_path):
    """``removed`` for this family is a drawing that was archived HERE.

    Owner-liveness reaping archives a canvas whose owner instance is gone, and
    hand cleanup moves the same file. Either way the realm still carries it, so
    the revert writes it back through the pull's own door rather than reading a
    local absence as a realm-wide removal.
    """

    from agent_runtime.flow_graph import FlowGraphStore

    realm_id, _, subtree = _desk_with_instance(tmp_path)
    _store_canvas(x=10)
    _publish_canvas(realm_id, subtree)

    store = FlowGraphStore()
    store.archive(CANVAS_GRAPH_ID, store.stale_dir())
    assert store.get(CANVAS_GRAPH_ID) is None

    (item,) = _canvas_rows(realm_id)
    assert item.kind == DRIFT_KIND_REMOVED

    result = revert_realm_sync(realm_id, item_specs=[item.spec])

    assert [row["outcome"] for row in result["items"]] == [OUTCOME_RESTORED]
    assert _canvas_x() == 10


def test_reverting_a_local_only_canvas_archives_it_and_never_deletes_it(tmp_path):
    """The ruling's shape for this family: archive, no ledger entry, no delete.

    A canvas carries no realm-visible tombstone anywhere, so
    ``record_tombstone=False`` is structural here rather than a parameter — and
    the operator's own last local bytes land in ``flow_graphs_stale/``, which is
    where owner-liveness reaping puts them too.
    """

    from agent_runtime.flow_graph import FlowGraphStore

    realm_id, _, subtree = _desk_with_instance(tmp_path)
    # The realm has published nothing for this family: no projection artifact at
    # all, which is the normal shape for a realm where nobody has drawn one.
    (subtree / "store").mkdir(parents=True, exist_ok=True)
    _store_canvas(x=7)

    (item,) = _canvas_rows(realm_id)
    assert item.kind == DRIFT_KIND_ADDED

    result = revert_realm_sync(realm_id, item_specs=[item.spec])

    assert [row["outcome"] for row in result["items"]] == [OUTCOME_ARCHIVED_LOCAL_ONLY]
    store = FlowGraphStore()
    assert store.get(CANVAS_GRAPH_ID) is None
    assert list(store.stale_dir().glob("*.json")), "the drawing was deleted, not archived"
    assert _canvas_rows(realm_id) == []


def test_an_unreadable_canvas_projection_is_refused_not_read_as_absence(tmp_path):
    """Absence drives the archive arm, so a parse failure must never fold into
    it — the same rule every other family in this lane holds."""

    from agent_runtime.flow_graph import FlowGraphStore

    realm_id, _, subtree = _desk_with_instance(tmp_path)
    _store_canvas(x=7)
    path = subtree / "store" / "flow_graphs.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("kind: realm_flow_graphs\ngraphs: [oops\n", encoding="utf-8")

    (item,) = _canvas_rows(realm_id)
    result = revert_realm_sync(realm_id, item_specs=[item.spec])

    assert [row["outcome"] for row in result["items"]] == [REFUSED_UNREADABLE_UPSTREAM]
    assert FlowGraphStore().get(CANVAS_GRAPH_ID) is not None


def test_a_remote_canvas_the_pull_would_reject_is_refused_here_too(tmp_path):
    """The canvas family's admission door is ``parse_flow_graph_doc`` and only
    it — the exact door ``apply_flow_graph_pull`` holds, so a revert neither
    writes what a pull could not have written nor refuses what it would have
    admitted."""

    import yaml

    realm_id, _, subtree = _desk_with_instance(tmp_path)
    _store_canvas(x=7)
    path = subtree / "store" / "flow_graphs.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "kind": "realm_flow_graphs",
                "schema_version": 1,
                # Two nodes sharing one id: a document the parser rejects.
                "graphs": {
                    CANVAS_STORED_GRAPH_ID: {
                        "graph_id": CANVAS_STORED_GRAPH_ID,
                        "nodes": [
                            {"id": "n_owner", "agent": CANVAS_INSTANCE_ID, "x": 1, "y": 1},
                            {"id": "n_owner", "agent": CANVAS_INSTANCE_ID, "x": 2, "y": 2},
                        ],
                        "edges": [],
                    }
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    (item,) = _canvas_rows(realm_id)
    result = revert_realm_sync(realm_id, item_specs=[item.spec])

    assert [row["outcome"] for row in result["items"]] == [REFUSED_ADMISSION]
    assert _canvas_x() == 7, "a refused document was written anyway"


def test_a_canvas_dry_run_writes_nothing(tmp_path):
    realm_id, _, subtree = _desk_with_instance(tmp_path)
    _store_canvas(x=10)
    _publish_canvas(realm_id, subtree)
    _store_canvas(x=99)

    (item,) = _canvas_rows(realm_id)
    result = revert_realm_sync(realm_id, item_specs=[item.spec], dry_run=True)

    assert result["dry_run"] is True
    assert [row["outcome"] for row in result["items"]] == [OUTCOME_REVERTED]
    assert _canvas_x() == 99
    assert [item.kind for item in _canvas_rows(realm_id)] == [DRIFT_KIND_CHANGED]
