"""S1 tests for the realm skill-delete ledger and its store chokepoints.

The model half of the skill-delete lane (canon:
``docs/agent-runtime-harness/01-system-architecture.md`` §Skills, third lane;
the plan file was retired at the 2026-08-28 canon fold): the
``SkillTombstone`` record, the bounded
``Realm.skill_tombstones`` ledger, the two write chokepoints
(``RealmStore.tombstone_skill`` / ``restore_skill``) and the ONE match rule
(``store.skill_tombstoned``) every enforcement point asks through.

What is deliberately NOT here: pull/publish enforcement (S2, exercised through
the real sync verbs in ``test_realm_sync_skill_inbox.py``) and the operator
verbs (S3).
"""

from __future__ import annotations

import json

import pytest

from agent_runtime import store as store_module
from agent_runtime.errors import SkillTombstoneRefused
from agent_runtime.models import Realm, SkillTombstone
from agent_runtime.serde import from_jsonable, to_jsonable
from agent_runtime.store import RealmStore, active_skill_tombstones, skill_tombstoned
from hermes_constants import CANONICAL_SHARED_SKILL_IDS


def _realm(name: str = "Tombstone Realm") -> Realm:
    return RealmStore().create(name=name)


# ── mint / dedupe / cap ──────────────────────────────────────────────────────


def test_tombstone_mints_a_record_and_dedupes_by_slug():
    realm = _realm()

    stored = RealmStore().tombstone_skill(realm.id, "doomed", deleted_hash="abc123")

    assert [entry.slug for entry in stored.skill_tombstones] == ["doomed"]
    first_at = stored.skill_tombstones[0].deleted_at
    assert stored.skill_tombstones[0].deleted_hash == "abc123"

    # Re-tombstoning REFRESHES the entry instead of appending a second one; the
    # hash follows the newer delete (it is evidence of what was deleted now).
    again = RealmStore().tombstone_skill(realm.id, "doomed", deleted_hash="def456")

    assert [entry.slug for entry in again.skill_tombstones] == ["doomed"]
    assert again.skill_tombstones[0].deleted_hash == "def456"
    assert again.skill_tombstones[0].deleted_at >= first_at


def test_tombstone_ledger_is_bounded_oldest_first(monkeypatch):
    # The real cap is 200; the eviction ORDER is the behaviour under test, so
    # the bound is narrowed rather than minting 201 realm saves for it.
    assert store_module.SKILL_TOMBSTONE_LEDGER_CAP == 200
    monkeypatch.setattr(store_module, "SKILL_TOMBSTONE_LEDGER_CAP", 3)
    realm = _realm()

    for index in range(5):
        stored = RealmStore().tombstone_skill(realm.id, f"skill-{index}")

    assert [entry.slug for entry in stored.skill_tombstones] == [
        "skill-2",
        "skill-3",
        "skill-4",
    ]


def test_tombstone_dry_run_validates_without_writing():
    realm = _realm()

    preview = RealmStore().tombstone_skill(realm.id, "doomed", dry_run=True)

    assert [entry.slug for entry in preview.skill_tombstones] == ["doomed"]
    assert RealmStore().get(realm.id).skill_tombstones == []


# ── typed refusals ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "slug",
    ["", "  ", "../escape", "a/b/c", ".hidden", "with:colon", "back\\slash"],
)
def test_tombstone_refuses_invalid_slug(slug):
    realm = _realm()

    with pytest.raises(SkillTombstoneRefused) as excinfo:
        RealmStore().tombstone_skill(realm.id, slug)

    assert excinfo.value.code == "skill_slug_invalid"
    assert RealmStore().get(realm.id).skill_tombstones == []


def test_tombstone_refuses_installer_owned_slug():
    # R-B: every pull reinstalls these from repo source, so a ledger entry for
    # one would lose that argument forever — the door refuses instead of
    # minting a tombstone that silently does nothing.
    slug = sorted(CANONICAL_SHARED_SKILL_IDS)[0]
    realm = _realm()

    with pytest.raises(SkillTombstoneRefused) as excinfo:
        RealmStore().tombstone_skill(realm.id, slug)

    assert excinfo.value.code == "skill_installer_owned"
    assert excinfo.value.safe_details == {"slug": slug}
    # The refusal names the real delete lane for those ids.
    assert "CANONICAL_SHARED_SKILL_IDS" in str(excinfo.value)
    assert RealmStore().get(realm.id).skill_tombstones == []


# ── the ONE match rule ───────────────────────────────────────────────────────


def test_skill_tombstoned_matches_exact_slug_and_categorized_child():
    realm = _realm()
    stored = RealmStore().tombstone_skill(realm.id, "doomed")

    assert skill_tombstoned(stored, "doomed") is not None
    # Categorized packages are matched by their bare child name, exactly like
    # ``realm_sync._skill_slug_selected`` matches a selection entry.
    assert skill_tombstoned(stored, "category/doomed") is not None
    assert skill_tombstoned(stored, "keeper") is None
    assert skill_tombstoned(stored, "category/keeper") is None
    assert skill_tombstoned(stored, "") is None


def test_skill_tombstoned_matches_a_categorized_entry_by_its_full_slug():
    realm = _realm()
    stored = RealmStore().tombstone_skill(realm.id, "category/doomed")

    assert skill_tombstoned(stored, "category/doomed") is not None
    # A categorized ENTRY does not block the bare name: it names one package,
    # and ``<other>/doomed`` is a different package than ``category/doomed``.
    assert skill_tombstoned(stored, "doomed") is None
    assert skill_tombstoned(stored, "other/doomed") is None


def test_skill_tombstoned_on_a_realm_without_the_field():
    # Old-member compat: a realm loaded from a record predating the ledger has
    # an empty list, and the matcher must answer without a branch at the call
    # site (§5 — the field is additive at schema_version 1).
    realm = _realm()

    assert realm.skill_tombstones == []
    assert skill_tombstoned(realm, "anything") is None


# ── R-F: selection pruning at the same write ─────────────────────────────────


def test_tombstone_prunes_the_slug_from_skill_selection():
    realm = _realm()
    RealmStore().set_skill_selection(
        realm.id, mode="selected", selection=["doomed", "keeper"]
    )

    stored = RealmStore().tombstone_skill(realm.id, "doomed")

    assert stored.skill_selection == ["keeper"]
    assert stored.skill_publish_mode == "selected"


def test_tombstone_prunes_a_categorized_selection_entry_by_child_name():
    realm = _realm()
    # The CLI validator rejects a '/' selection slug (documented follow-up), so
    # the categorized entry is persisted directly, as the publish test does.
    item = RealmStore().get(realm.id)
    item.skill_publish_mode = "selected"
    item.skill_selection = ["category/doomed", "keeper"]
    RealmStore().save(item)

    stored = RealmStore().tombstone_skill(realm.id, "doomed")

    assert stored.skill_selection == ["keeper"]


def test_restore_does_not_re_add_the_selection_entry():
    # R-F's second half: selection is a separate, deliberate act, so lifting a
    # tombstone never re-publishes the slug by itself.
    realm = _realm()
    RealmStore().set_skill_selection(
        realm.id, mode="selected", selection=["doomed", "keeper"]
    )
    RealmStore().tombstone_skill(realm.id, "doomed")

    stored = RealmStore().restore_skill(realm.id, "doomed")

    # RD-11: the lift stamps the entry instead of removing it, so what "no
    # tombstone" means here is the ACTIVE ledger, not the register.
    assert active_skill_tombstones(stored) == []
    assert stored.skill_selection == ["keeper"]


# ── restore ──────────────────────────────────────────────────────────────────


def test_restore_lifts_the_entry_and_is_idempotent():
    realm = _realm()
    RealmStore().tombstone_skill(realm.id, "doomed")
    RealmStore().tombstone_skill(realm.id, "other")

    stored = RealmStore().restore_skill(realm.id, "doomed")
    # RD-11: the entry SURVIVES the lift carrying ``restored_at`` — the register
    # keeps a restore representable, which is what the union merge needs — but
    # it stops blocking the moment it is stamped.
    assert [entry.slug for entry in stored.skill_tombstones] == ["doomed", "other"]
    assert [entry.slug for entry in active_skill_tombstones(stored)] == ["other"]
    assert stored.skill_tombstones[0].restored_at is not None
    assert skill_tombstoned(stored, "doomed") is None

    # Already-lifted entry: not an error, no second write (the stamp is the
    # first lift's, so an idempotent re-run cannot move the merge's clock).
    lifted_at = stored.skill_tombstones[0].restored_at
    again = RealmStore().restore_skill(realm.id, "doomed")
    assert [entry.slug for entry in again.skill_tombstones] == ["doomed", "other"]
    assert again.skill_tombstones[0].restored_at == lifted_at
    # Absent entry: not an error, no write.
    assert RealmStore().restore_skill(realm.id, "never-tombstoned").id == realm.id


def test_a_re_delete_after_a_restore_replaces_the_register_entry():
    # RD-11: the register records the CURRENT state of a slug. A delete that
    # landed while a restore stamp was standing must not leave both stamps on
    # one entry — the merge ranks by the LATER stamp, so a stale ``restored_at``
    # riding a fresh delete would let an old restore win a comparison it lost.
    realm = _realm()
    RealmStore().tombstone_skill(realm.id, "doomed")
    RealmStore().restore_skill(realm.id, "doomed")

    stored = RealmStore().tombstone_skill(realm.id, "doomed", deleted_hash="def456")

    assert [entry.slug for entry in stored.skill_tombstones] == ["doomed"]
    assert stored.skill_tombstones[0].restored_at is None
    assert stored.skill_tombstones[0].deleted_hash == "def456"
    assert skill_tombstoned(stored, "doomed") is not None


def test_the_ledger_cap_evicts_settled_history_before_a_live_block():
    # RD-11's cost: restored entries linger, so the bound must not let inert
    # history push a LIVE block off the front — an evicted block is a
    # resurrected skill, which is the one thing this ledger exists to prevent.
    monkey = pytest.MonkeyPatch()
    monkey.setattr(store_module, "SKILL_TOMBSTONE_LEDGER_CAP", 2)
    try:
        realm = _realm()
        # The settled entry sits BETWEEN the two live blocks, so a plain
        # oldest-first ``[-cap:]`` would keep it and evict ``live-old`` — the
        # case that makes this test discriminating rather than incidentally
        # agreeing with the ordinary bound.
        RealmStore().tombstone_skill(realm.id, "live-old")
        RealmStore().tombstone_skill(realm.id, "settled")
        RealmStore().restore_skill(realm.id, "settled")
        stored = RealmStore().tombstone_skill(realm.id, "live-new")
    finally:
        monkey.undo()

    assert [entry.slug for entry in stored.skill_tombstones] == ["live-old", "live-new"]


def test_restore_names_a_ledger_entry_not_a_package():
    # A categorized package blocked by a BARE-name tombstone is restored by
    # naming that bare name; naming the package leaves the block standing, so
    # the operator is never told a block was lifted that was not.
    realm = _realm()
    RealmStore().tombstone_skill(realm.id, "doomed")

    unchanged = RealmStore().restore_skill(realm.id, "category/doomed")

    assert [entry.slug for entry in unchanged.skill_tombstones] == ["doomed"]
    assert skill_tombstoned(unchanged, "category/doomed") is not None


# ── serde: round-trip + the documented old-member halves ─────────────────────


def test_tombstone_round_trips_through_the_realm_record():
    realm = _realm()
    RealmStore().tombstone_skill(realm.id, "category/doomed", deleted_hash="sha256:ab")

    reread = RealmStore().get(realm.id)

    assert len(reread.skill_tombstones) == 1
    entry = reread.skill_tombstones[0]
    assert isinstance(entry, SkillTombstone)
    assert entry.slug == "category/doomed"
    assert entry.deleted_hash == "sha256:ab"
    assert entry.deleted_at.tzinfo is not None

    # The nested record travels as a plain object inside the realm JSON — no new
    # serde code, and no schema_version bump on either side (§2.1 / §5).
    raw = json.loads(
        store_module.paths.realm_path(realm.id).read_text(encoding="utf-8")
    )
    assert raw["schema_version"] == 1
    assert raw["skill_tombstones"] == [
        {
            "slug": "category/doomed",
            "deleted_at": raw["skill_tombstones"][0]["deleted_at"],
            "deleted_hash": "sha256:ab",
            # RD-11 added this key to the record. Still no schema_version bump:
            # additive at v1, so an older member drops it on load exactly like
            # the field below — the same compat argument, one field deeper.
            "restored_at": None,
        }
    ]


def test_serde_drops_unknown_keys_and_saves_declared_fields_only():
    # The two halves of the §5 old-member constraint, asserted where they are
    # actually decided. A newer member's realm JSON LOADS on older code (unknown
    # key dropped) — and an older member's next SAVE writes only ITS declared
    # fields, which is why delete propagation is guaranteed only once every
    # member runs >= S1 code.
    realm = _realm()
    payload = to_jsonable(RealmStore().get(realm.id))

    assert set(payload) == {field for field in Realm.__dataclass_fields__}
    assert payload["skill_tombstones"] == []

    payload["a_field_this_code_does_not_know"] = ["ghost"]
    loaded = from_jsonable(Realm, payload)

    assert loaded.id == realm.id
    assert not hasattr(loaded, "a_field_this_code_does_not_know")
