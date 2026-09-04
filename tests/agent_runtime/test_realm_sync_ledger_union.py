"""S4 tests for the pull-time UNION of the realm's resurrection-guard ledgers.

RD-11 (decision-close wave, 2026-08-31) upgraded R-D from "LWW acceptable" to
"union": ``Realm.skill_tombstones`` and ``Realm.deleted_workspace_ids`` used to
be adopted wholesale with the rest of the pulled realm JSON, so two members
publishing concurrently silently dropped each other's entries and a
realm-deleted skill became publishable again.

Two layers, deliberately:

- the MERGE MATRIX against the pure functions — local-only, incoming-only, both
  sides differing by timestamp, restore-vs-stale-delete and its inverse, plus
  the tie/unreadable-stamp/malformed-row rules a peer's bytes can produce;
- the same shapes end to end through ``pull_realm_sync`` on a local realm, so
  what is pinned is the verb an operator runs, not a helper's signature.

The S1 register semantics (``restored_at`` as a positive marker) live in
``test_realm_skill_tombstones.py``; enforcement lives in
``test_realm_sync_skill_inbox.py``.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.skill_utils import _content_hash_cache_clear
from agent_runtime import paths as runtime_paths
from agent_runtime import store as store_module
from agent_runtime.realm_sync import (
    merge_deleted_workspace_ledgers,
    merge_skill_tombstone_ledgers,
    merge_workspace_lift_ledgers,
    pull_realm_sync,
    realm_sync_status,
)
from agent_runtime.store import (
    RealmStore,
    active_skill_tombstones,
    active_workspace_lifts,
    lift_deleted_workspace,
    skill_tombstoned,
)
from hermes_constants import get_shared_skills_dir

T0 = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)


def _stamp(offset_seconds: int) -> str:
    return (T0 + timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z")


def _row(slug: str, *, deleted: int = 0, restored: int | None = None, **extra) -> dict:
    row = {"slug": slug, "deleted_at": _stamp(deleted), "deleted_hash": None, **extra}
    if restored is not None:
        row["restored_at"] = _stamp(restored)
    return row


def _slugs(rows: list[dict]) -> list[str]:
    return [row["slug"] for row in rows]


def _by_slug(rows: list[dict], slug: str) -> dict:
    return next(row for row in rows if row["slug"] == slug)


# ── the merge matrix ─────────────────────────────────────────────────────────


def test_local_only_entry_survives_an_incoming_ledger_that_lacks_it():
    # THE regression RD-11 exists for: under LWW this entry was dropped and the
    # skill became publishable again.
    merged = merge_skill_tombstone_ledgers([_row("mine")], [])

    assert _slugs(merged) == ["mine"]


def test_incoming_only_entry_is_adopted():
    merged = merge_skill_tombstone_ledgers([], [_row("theirs")])

    assert _slugs(merged) == ["theirs"]


def test_both_sides_travel_and_the_result_is_oldest_transition_first():
    merged = merge_skill_tombstone_ledgers(
        [_row("mine", deleted=30)], [_row("theirs", deleted=10)]
    )

    # Oldest first — the append order every ledger chokepoint keeps, so the
    # shared settled-first bound evicts in the same direction here as there.
    assert _slugs(merged) == ["theirs", "mine"]


def test_the_newer_stamp_wins_for_one_slug():
    older = _row("doomed", deleted=0, deleted_hash="sha256:old")
    newer = _row("doomed", deleted=60, deleted_hash="sha256:new")

    assert _by_slug(merge_skill_tombstone_ledgers([older], [newer], ), "doomed")[
        "deleted_hash"
    ] == "sha256:new"
    assert _by_slug(merge_skill_tombstone_ledgers([newer], [older]), "doomed")[
        "deleted_hash"
    ] == "sha256:new"


def test_a_restore_beats_a_stale_delete_from_either_side():
    # The case that forced ``restored_at`` into the record: a lift is a NEWER
    # fact about the same ``deleted_at``, and a union of absences could not
    # express it.
    stale = _row("doomed", deleted=0)
    restored = _row("doomed", deleted=0, restored=60)

    for local, incoming in ((stale, restored), (restored, stale)):
        merged = merge_skill_tombstone_ledgers([local], [incoming])
        assert _by_slug(merged, "doomed").get("restored_at") == _stamp(60)


def test_a_fresh_re_delete_beats_an_older_restore():
    # The inverse, and the reason the comparison key is the LATER of the two
    # stamps rather than ``deleted_at``: a member who re-deleted after someone
    # else's lift must win, or a delete could never be re-asserted.
    restored = _row("doomed", deleted=0, restored=60)
    re_deleted = _row("doomed", deleted=120)

    merged = merge_skill_tombstone_ledgers([restored], [re_deleted])

    assert _by_slug(merged, "doomed").get("restored_at") is None


def test_an_equal_stamp_tie_resolves_to_the_delete():
    # Fail-safe: a restore that loses a microsecond tie is one explicit verb
    # away from being re-run; a BLOCK that loses one is a deleted skill quietly
    # publishable again.
    blocking = _row("doomed", deleted=60)
    lifted = _row("doomed", deleted=0, restored=60)

    for local, incoming in ((blocking, lifted), (lifted, blocking)):
        merged = merge_skill_tombstone_ledgers([local], [incoming])
        assert _by_slug(merged, "doomed").get("restored_at") is None


def test_an_unreadable_stamp_never_outranks_a_readable_one():
    # A peer's bytes, not ours: an entry whose stamps will not parse costs that
    # one entry its rank and nothing more — the merge still runs.
    garbage = {"slug": "doomed", "deleted_at": "not-a-date", "deleted_hash": "junk"}
    readable = _row("doomed", deleted=0, deleted_hash="sha256:real")

    for local, incoming in ((garbage, readable), (readable, garbage)):
        merged = merge_skill_tombstone_ledgers([local], [incoming])
        assert _by_slug(merged, "doomed")["deleted_hash"] == "sha256:real"


def test_rows_without_a_usable_slug_are_dropped():
    # ``skill_tombstoned`` reads ``entry.slug``, so such a row blocks nothing;
    # carrying it forward only risks breaking the next realm load.
    merged = merge_skill_tombstone_ledgers(
        ["a bare string", {"deleted_at": _stamp(0)}, {"slug": "   "}, _row("real")],
        None,
    )

    assert _slugs(merged) == ["real"]


def test_a_non_list_ledger_is_tolerated_rather_than_iterated():
    merged = merge_skill_tombstone_ledgers({"slug": "doomed"}, [_row("real")])

    assert _slugs(merged) == ["real"]


def test_the_merge_bounds_the_ledger_and_evicts_settled_history_first(monkeypatch):
    monkeypatch.setattr(store_module, "SKILL_TOMBSTONE_LEDGER_CAP", 2)
    # Rebind the module-level import the merge closes over, the way the store's
    # own cap test narrows the bound rather than minting 201 entries.
    monkeypatch.setattr(
        "agent_runtime.realm_sync.SKILL_TOMBSTONE_LEDGER_CAP", 2, raising=True
    )

    # The settled entry's transition is the NEWEST of the three, so a plain
    # oldest-first ``[-cap:]`` would keep it and evict a live block — which is
    # the eviction this ledger must never make.
    merged = merge_skill_tombstone_ledgers(
        [_row("settled", deleted=0, restored=50), _row("live-old", deleted=20)],
        [_row("live-new", deleted=30)],
    )

    assert _slugs(merged) == ["live-old", "live-new"]


def test_deleted_workspace_ids_union_dedupes_and_keeps_local_order():
    merged = merge_deleted_workspace_ledgers(["a", "b"], ["b", "c"])

    assert merged == ["a", "b", "c"]


def test_deleted_workspace_ids_union_drops_blanks_and_tolerates_absence():
    assert merge_deleted_workspace_ledgers(None, ["", "  ", "x"]) == ["x"]
    assert merge_deleted_workspace_ledgers(["x"], None) == ["x"]


# ── the propagating LIFT marker (w13/h2, RULED 2026-09-04) ───────────────────
# The union above cannot express a lift: `default_scope` removed the reserved
# local-default workspace's id from the ledger, and a removal is an ABSENCE,
# which the union reads as "that peer never heard about this delete". The lift
# therefore stayed local until the cap aged the id out (MEASURED by W2-H5). A
# lift is now a positive marker with a clock that travels in the realm JSON.


def _lift(workspace_id: str, *, restored: int, deleted: int | None = None) -> dict:
    row = {"workspace_id": workspace_id, "restored_at": _stamp(restored)}
    if deleted is not None:
        row["deleted_at"] = _stamp(deleted)
    return row


def test_a_live_lift_subtracts_its_id_from_the_deleted_union():
    """THE regression this marker exists for.

    A peer that still carries the id must not be able to re-delete a workspace
    this member has restored — under a bare union it always could.
    """

    merged = merge_deleted_workspace_ledgers(
        [], ["ws_default", "ws_other"], lifts=[_lift("ws_default", restored=10)]
    )

    assert merged == ["ws_other"]


def test_a_lift_a_later_delete_superseded_no_longer_subtracts():
    """The other direction, and it is the reason the marker carries two stamps.

    A re-delete stamps ``deleted_at`` on the marker instead of dropping it —
    dropping it would be an absence again, and a peer's surviving lift would
    out-rank the fresh delete.
    """

    merged = merge_deleted_workspace_ledgers(
        [], ["ws_default"], lifts=[_lift("ws_default", restored=10, deleted=20)]
    )

    assert merged == ["ws_default"]


def test_an_equal_stamp_tie_on_a_lift_resolves_to_the_delete():
    """Same tie rule as the skill ledger, and for the same reason: a lift that
    loses a tie is one explicit verb away from being re-run; a delete that loses
    one is a resurrected workspace."""

    merged = merge_deleted_workspace_ledgers(
        [], ["ws_default"], lifts=[_lift("ws_default", restored=10, deleted=10)]
    )

    assert merged == ["ws_default"]


def test_an_absent_lift_register_reproduces_the_old_union_exactly():
    """Back-compat floor: an old member's realm JSON carries no lift register,
    and must merge exactly as it did before this field existed."""

    assert merge_deleted_workspace_ledgers(["a"], ["b"]) == ["a", "b"]
    assert merge_deleted_workspace_ledgers(["a"], ["b"], lifts=None) == ["a", "b"]
    assert merge_deleted_workspace_ledgers(["a"], ["b"], lifts=[]) == ["a", "b"]


def test_the_lift_register_keeps_the_newest_transition_per_workspace():
    merged = merge_workspace_lift_ledgers(
        [_lift("ws_a", restored=10), _lift("ws_b", restored=5)],
        [_lift("ws_a", restored=10, deleted=30)],
    )

    assert [row["workspace_id"] for row in merged] == ["ws_b", "ws_a"]
    # ws_a's newer fact is the re-delete, so it stops subtracting.
    assert merge_deleted_workspace_ledgers([], ["ws_a", "ws_b"], lifts=merged) == ["ws_a"]


def test_the_lift_register_drops_rows_nothing_can_key_on():
    """A peer's bytes, not ours: a row with no workspace_id keys nothing
    downstream, and carrying it forward only risks breaking the next realm load.
    """

    merged = merge_workspace_lift_ledgers(
        ["not-a-dict", {"restored_at": _stamp(1)}, {"workspace_id": "  "}],
        [_lift("ws_ok", restored=1)],
    )

    assert [row["workspace_id"] for row in merged] == ["ws_ok"]


# ── end to end through the pull verb ─────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_hash_cache(monkeypatch):
    monkeypatch.delenv("HERMES_SHARED_SKILLS", raising=False)
    _content_hash_cache_clear()
    yield
    _content_hash_cache_clear()


def _local_realm(tmp_path: Path, name: str = "Union Realm"):
    """A server-less realm whose sync repo is a local git repo (no remote).

    Server-LESS on purpose: RD-11 ended the asymmetry by which
    ``_pulled_artifact_bytes`` only ran for a server-bound realm, and a
    local-only realm is where a dropped tombstone used to be unguarded.
    """

    realm = RealmStore().create(name=name)
    repo = tmp_path / "realm-sync-repo"
    subprocess.run(
        ["git", "-C", str(tmp_path), "init", "realm-sync-repo"],
        check=True,
        capture_output=True,
        text=True,
    )
    realm.sync_manifest_ref = str(repo)
    realm = RealmStore().save(realm)
    return realm, repo


def _publish_record(repo: Path, realm) -> None:
    """Copy this member's realm JSON into the subtree, as a publish would."""

    token = runtime_paths.safe_path_token(realm.id)
    dest = repo / "realms" / realm.id / "store" / "realms" / f"{token}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(runtime_paths.realm_path(realm.id).read_bytes())


def _set_ledger(realm_id: str, entries: list) -> None:
    item = RealmStore().get(realm_id)
    item.skill_tombstones = entries
    RealmStore().save(item)


def _seed_canonical(slug: str) -> Path:
    pkg = get_shared_skills_dir().joinpath(*slug.split("/"))
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "SKILL.md").write_bytes(f"---\nname: {slug}\n---\n# Live\n".encode("utf-8"))
    return pkg


def test_a_pull_keeps_this_members_tombstone_the_publisher_never_had(tmp_path):
    # The concurrent-publish drop, end to end: member A published a ledger
    # naming only THEIR delete; before RD-11 this pull adopted it wholesale and
    # this member's own delete vanished from the realm.
    realm, repo = _local_realm(tmp_path)
    RealmStore().tombstone_skill(realm.id, "theirs", deleted_hash="sha256:theirs")
    _publish_record(repo, realm)
    _set_ledger(realm.id, [])
    RealmStore().tombstone_skill(realm.id, "mine", deleted_hash="sha256:mine")

    pull_realm_sync(realm.id)

    pulled = RealmStore().get(realm.id)
    assert sorted(entry.slug for entry in active_skill_tombstones(pulled)) == [
        "mine",
        "theirs",
    ]
    # The receipt surfaces both, so the operator sees the union they now have.
    assert [row["slug"] for row in realm_sync_status(realm.id)["skill_tombstones"]] == [
        "mine",
        "theirs",
    ]


def test_a_pull_does_not_undo_this_members_restore_with_a_stale_delete(tmp_path):
    # The restore-vs-stale-delete row, end to end, and the reason a union needed
    # ``restored_at`` at all: the subtree still carries the ACTIVE delete this
    # member has since lifted. A union of bare absences would re-block the slug
    # — and the same pull would archive the live package.
    realm, repo = _local_realm(tmp_path)
    _seed_canonical("doomed")
    RealmStore().tombstone_skill(realm.id, "doomed", deleted_hash="sha256:ab")
    _publish_record(repo, realm)
    RealmStore().restore_skill(realm.id, "doomed")

    result = pull_realm_sync(realm.id)

    pulled = RealmStore().get(realm.id)
    assert skill_tombstoned(pulled, "doomed") is None
    assert [entry.slug for entry in pulled.skill_tombstones] == ["doomed"]
    assert pulled.skill_tombstones[0].restored_at is not None
    # Not archived: the enforcement lane reads the merged ledger, not the
    # arriving one.
    assert (get_shared_skills_dir() / "doomed" / "SKILL.md").is_file()
    assert result.get("skill_tombstones", {}).get("archived", []) == []


def test_a_pull_adopts_a_delete_that_lands_after_a_restore(tmp_path):
    # The inverse, so the restore marker cannot become a permanent immunity: a
    # member who re-deleted AFTER this member's lift wins on the newer stamp.
    realm, repo = _local_realm(tmp_path)
    _seed_canonical("doomed")
    RealmStore().tombstone_skill(realm.id, "doomed")
    RealmStore().restore_skill(realm.id, "doomed")
    local_ledger = RealmStore().get(realm.id).skill_tombstones
    # Publish a record whose delete is NEWER than this member's restore.
    _publish_record(repo, realm)
    token = runtime_paths.safe_path_token(realm.id)
    published = repo / "realms" / realm.id / "store" / "realms" / f"{token}.json"
    raw = json.loads(published.read_text(encoding="utf-8"))
    later = local_ledger[0].restored_at + timedelta(seconds=30)
    raw["skill_tombstones"] = [
        {
            "slug": "doomed",
            "deleted_at": later.isoformat().replace("+00:00", "Z"),
            "deleted_hash": "sha256:re",
        }
    ]
    published.write_text(json.dumps(raw), encoding="utf-8")

    result = pull_realm_sync(realm.id)

    pulled = RealmStore().get(realm.id)
    assert skill_tombstoned(pulled, "doomed") is not None
    assert result["skill_tombstones"]["archived"] == ["doomed"]


def test_a_pull_unions_the_deleted_workspace_ledger(tmp_path):
    realm, repo = _local_realm(tmp_path)
    item = RealmStore().get(realm.id)
    item.deleted_workspace_ids = ["ws_theirs"]
    RealmStore().save(item)
    _publish_record(repo, realm)
    item = RealmStore().get(realm.id)
    item.deleted_workspace_ids = ["ws_mine"]
    RealmStore().save(item)

    pull_realm_sync(realm.id)

    assert RealmStore().get(realm.id).deleted_workspace_ids == ["ws_mine", "ws_theirs"]


def test_a_pull_cannot_undo_a_lift_the_publisher_never_heard_about(tmp_path):
    """END TO END, through the verb an operator runs.

    Before the lift marker this asserted the opposite by construction: the local
    removal was an absence, the peer's copy still carried the id, and the union
    put it straight back. The workspace stayed deleted on this machine until the
    id aged out of the 500-entry cap.
    """

    realm, repo = _local_realm(tmp_path)
    item = RealmStore().get(realm.id)
    item.deleted_workspace_ids = ["ws_default", "ws_theirs"]
    RealmStore().save(item)
    _publish_record(repo, realm)

    item = RealmStore().get(realm.id)
    assert lift_deleted_workspace(item, "ws_default") is True
    RealmStore().save(item)

    pull_realm_sync(realm.id)

    pulled = RealmStore().get(realm.id)
    assert pulled.deleted_workspace_ids == ["ws_theirs"]
    # And the marker itself survives the pull, so the NEXT peer to publish
    # learns about the restore too.
    assert [lift.workspace_id for lift in active_workspace_lifts(pulled)] == ["ws_default"]


def test_a_delete_after_a_lift_supersedes_the_marker_end_to_end(tmp_path):
    """The lift must not become a permanent immunity to deletion."""

    realm, repo = _local_realm(tmp_path)
    item = RealmStore().get(realm.id)
    item.deleted_workspace_ids = ["ws_default"]
    RealmStore().save(item)
    _publish_record(repo, realm)

    item = RealmStore().get(realm.id)
    lift_deleted_workspace(item, "ws_default")
    RealmStore().save(item)

    # The re-delete's supersede stamp, written the way WorkspaceStore.delete
    # writes it (the workspace row itself is long gone in this fixture).
    item = RealmStore().get(realm.id)
    item.workspace_lifts[0].deleted_at = datetime.now(timezone.utc)
    RealmStore().save(item)

    pull_realm_sync(realm.id)

    pulled = RealmStore().get(realm.id)
    assert pulled.deleted_workspace_ids == ["ws_default"]
    assert active_workspace_lifts(pulled) == []


def test_a_pull_that_reconciles_nothing_leaves_the_record_byte_identical(tmp_path):
    # The ``merged == incoming`` guard: re-serializing an unchanged record would
    # report ``changed`` on formatting alone, and a no-op pull must not.
    realm, repo = _local_realm(tmp_path)
    RealmStore().tombstone_skill(realm.id, "doomed")
    _publish_record(repo, realm)
    before = runtime_paths.realm_path(realm.id).read_bytes()

    result = pull_realm_sync(realm.id)

    assert runtime_paths.realm_path(realm.id).read_bytes() == before
    assert result["changed"] is False
