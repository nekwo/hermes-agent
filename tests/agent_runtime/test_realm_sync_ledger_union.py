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
    pull_realm_sync,
    realm_sync_status,
)
from agent_runtime.store import RealmStore, active_skill_tombstones, skill_tombstoned
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
