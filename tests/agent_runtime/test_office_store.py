"""OfficeStore tests (office plan W-H1): event-per-mutation, actor-key
canonicalization at the store boundary, filename truncation collision-proofing,
write-time secret rejection of display names, archive ledger + restore,
revision guard, the prune-lane hook, and the snapshot offices projection.
Autouse conftest fixtures isolate the runtime root.
"""

from __future__ import annotations

import pytest

from agent_runtime import office_models, paths
from agent_runtime.errors import (
    ArchiveUnreadable,
    NotFound,
    StaleRevision,
    SyncConflict,
)
from agent_runtime.events import EventLog
from agent_runtime.office_store import OfficeStore
from agent_runtime.snapshot import SNAPSHOT_CONTRACT_VERSION, build_snapshot
from agent_runtime.store import WorkspaceStore


def _event_types() -> list[str]:
    return [evt.type for _, evt in EventLog().iter_from_offset(0)]


def _make_workspace(name: str = "Default") -> str:
    ws = WorkspaceStore().create(name=name)
    WorkspaceStore().set_active(ws.id)
    return ws.id


def _actor_payload(persona_id: str = "dev", **overrides) -> dict:
    payload = {
        "persona_id": persona_id,
        "items": [
            {"item_id": persona_id, "persona_id": persona_id, "kind": "agent", "position": [1.5, 2.0], "folder": "Agents"},
            {"item_id": f"desk-{persona_id}", "persona_id": persona_id, "kind": "desk", "position": [1.5, 3.6], "folder": "Desks"},
        ],
    }
    payload.update(overrides)
    return payload


# ── event per mutation + round trip ───────────────────────────────────────


def test_upsert_remove_restore_round_trip_emits_event_per_mutation():
    ws = _make_workspace()
    store = OfficeStore()
    actor = store.upsert_actor(ws, _actor_payload("dev"))
    assert actor.actor_key == "dev"
    assert len(actor.items) == 2
    removed = store.remove_actor(ws, "dev")
    assert removed.state == "archived"
    restored = store.restore_actor(ws, "dev")
    assert restored.state == "active"
    store.update_surface(ws, folders=["West Wing"])

    types = _event_types()
    for expected in (
        "office.surface.created",
        "office.actor.upserted",
        "office.actor.removed",
        "office.actor.restored",
        "office.surface.updated",
    ):
        assert expected in types, (expected, types)


def test_surface_created_once_and_deterministic():
    ws = _make_workspace()
    store = OfficeStore()
    first = store.ensure_surface(ws)
    second = store.ensure_surface(ws)
    assert _event_types().count("office.surface.created") == 1
    assert office_models.office_content_hash(first) == office_models.office_content_hash(second)
    assert list(first.folders) == list(office_models.DEFAULT_FOLDERS)


# ── identity: canonicalization at the boundary (plan §4.3) ────────────────


def test_actor_key_canonicalizes_drifted_instance_id():
    ws = _make_workspace()
    store = OfficeStore()
    actor = store.upsert_actor(
        ws,
        _actor_payload("dev", persona_instance_id="persona_personainst_goal1_dev"),
    )
    # persona_personainst_* actor-token drift collapses at the store boundary.
    assert actor.actor_key == "personainst_goal1_dev"
    assert actor.persona_instance_id == "personainst_goal1_dev"
    assert paths.office_actor_path(ws, "personainst_goal1_dev").exists()


def test_actor_key_falls_back_to_persona_id_and_normalizes_case():
    ws = _make_workspace()
    store = OfficeStore()
    actor = store.upsert_actor(ws, _actor_payload("Backend_Dev"))
    assert actor.actor_key == "backend_dev"
    assert actor.persona_id == "backend_dev"


def test_long_actor_keys_truncate_without_colliding():
    shared_prefix = "p" * 70
    token_a = office_models.actor_file_token(shared_prefix + "alpha")
    token_b = office_models.actor_file_token(shared_prefix + "beta")
    assert token_a != token_b, "truncated filenames must stay collision-proof (hash suffix)"
    assert len(token_a) <= 64 + 11
    # Deterministic: same key, same token, every machine.
    assert token_a == office_models.actor_file_token(shared_prefix + "alpha")


# ── write-time secret rejection (plan §4.2) ───────────────────────────────


def test_secret_shaped_display_name_rejected_at_write():
    ws = _make_workspace()
    store = OfficeStore()
    payload = _actor_payload("dev")
    payload["items"][0]["display_name"] = "token: abcdefgh12345678"
    with pytest.raises(ValueError):
        store.upsert_actor(ws, payload)
    # Nothing was written; the surface may exist but no actor file does.
    assert not store.actor_exists(ws, "dev")


# ── validation + revision guard ────────────────────────────────────────────


def test_invalid_payloads_rejected():
    ws = _make_workspace()
    store = OfficeStore()
    with pytest.raises(ValueError):
        store.upsert_actor(ws, {"persona_id": "dev", "items": []})
    with pytest.raises(ValueError):
        store.upsert_actor(ws, _actor_payload("dev", items=[{"item_id": "dev", "position": ["nan", 0]}]))
    with pytest.raises(ValueError):
        store.upsert_actor(ws, {"items": [{"item_id": "x", "position": [0, 0]}]})


def test_stale_revision_guard():
    ws = _make_workspace()
    store = OfficeStore()
    actor = store.upsert_actor(ws, _actor_payload("dev"))
    with pytest.raises(StaleRevision):
        store.upsert_actor(ws, _actor_payload("dev"), expect_revision=actor.revision + 5)
    updated = store.upsert_actor(ws, _actor_payload("dev"), expect_revision=actor.revision)
    assert updated.revision == actor.revision + 1


def test_scale_clamped_defensively():
    ws = _make_workspace()
    store = OfficeStore()
    payload = _actor_payload("dev")
    payload["items"][0]["scale"] = 99.0
    actor = store.upsert_actor(ws, payload)
    assert actor.items[0].scale == office_models.SCALE_MAX


# ── archive ledger + restore + re-add ─────────────────────────────────────


def test_remove_records_ledger_and_blocks_nothing_else():
    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _actor_payload("dev"))
    store.remove_actor(ws, "dev", reason="operator")
    surface = store.get_surface(ws)
    assert "dev" in surface.archived_actor_keys
    assert not store.actor_exists(ws, "dev")
    assert paths.office_archived_actor_path(ws, "dev").exists()
    # Idempotent remove returns the archived copy.
    again = store.remove_actor(ws, "dev")
    assert again.state == "archived"


def test_upsert_after_archive_clears_ledger():
    """The store's re-add contract, now behind the class-key fence's consent.

    "An explicit upsert of an archived key is intent to re-add, so clear the
    resurrection ledger" is still the contract — but since EG-6.6 the fence that
    used to sit at four callers sits in ``upsert_actor`` itself, and a CLASS-keyed
    re-add of an archived key is the exact write it refuses
    (``resurrects_archived_class_key``). Every production caller already refused
    this before the hoist; what changed is that the store no longer takes the
    intent on faith from whoever called it.

    So the intent is spelled: ``allow_class_key=True`` is the sanctioned override
    (``harness office actor-upsert --allow-class-key``, and the reason
    ``restore_actor`` exists). The refusal WITHOUT it is pinned in
    ``test_office_class_key_one_fence.py``; what this test still owns is that
    consent really does clear the ledger and the archive copy.
    """

    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _actor_payload("dev"))
    store.remove_actor(ws, "dev")
    readded = store.upsert_actor(ws, _actor_payload("dev"), allow_class_key=True)
    assert readded.state == "active"
    surface = store.get_surface(ws)
    assert "dev" not in surface.archived_actor_keys
    assert not paths.office_archived_actor_path(ws, "dev").exists()


def test_restore_missing_raises():
    ws = _make_workspace()
    store = OfficeStore()
    with pytest.raises(NotFound):
        store.restore_actor(ws, "ghost")


# ── EG-1.5 / RD-H4: the scan counts what it could not read ─────────────────


@pytest.mark.parametrize("corrupt_count", [1, 2])
def test_a_corrupt_actor_file_is_counted_not_vanished(corrupt_count, caplog):
    """``continue`` alone made a shortened office indistinguishable from a
    smaller one.

    ``_read_actor_dir`` has always skipped an actor file it could not decode, and
    the skip has to stay — a whole office must not vanish because one file is
    mid-write or held by an AV scanner. What could not stay is the SILENCE: every
    reader downstream got a shorter list that described itself as complete.

    **Anti-vacuity.** Restoring the bare ``continue`` is the mutation. The count
    is driven to TWO distinct values by this parametrize, so a mutant reporting a
    constant matches at most one; a mutant that skips silently reports 0 and
    matches neither. The readable actors are asserted in the same breath, which
    is what stops the opposite over-correction (refusing the whole scan) from
    passing.
    """

    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _actor_payload("dev"))
    store.upsert_actor(ws, _actor_payload("qa"))
    for index in range(corrupt_count):
        # A file the glob finds and the decoder cannot use — the shape an
        # interrupted write or a partially-scanned file arrives in.
        (paths.office_actors_dir(ws) / f"broken{index}.json").write_text(
            "{not json", encoding="utf-8"
        )

    with caplog.at_level("WARNING"):
        scan = store.scan_actors(ws)

    assert scan.unreadable == corrupt_count
    assert [actor.actor_key for actor in scan.actors] == ["dev", "qa"]
    # ONE line for that ONE scan, naming the exception CLASS — never one line per
    # file (a directory of stale files would flood the log on every office read)
    # and never the decoder's message. Read before the second scan below, which
    # would legitimately add its own line.
    unreadable_lines = [
        record.getMessage()
        for record in caplog.records
        if "office actor files unreadable" in record.getMessage()
    ]
    assert len(unreadable_lines) == 1
    assert f": {corrupt_count} (" in unreadable_lines[0]
    assert "JSONDecodeError" in unreadable_lines[0]
    # The list view keeps its old signature and its old answer, so the sixteen
    # callers that only want rows are untouched by this stage.
    assert [actor.actor_key for actor in store.list_actors(ws)] == ["dev", "qa"]


def test_an_unreadable_archive_refuses_the_re_add_instead_of_minting_revision_1():
    """The revision guard is only as honest as the token it spends.

    ``upsert_actor`` bases a re-added key's revision on the ARCHIVED copy on
    purpose: a key that left and came back carries its history forward, so the
    number a peer holds stays meaningful. The swallow turned an unreadable
    archive into ``archived = None`` → base 0 → **revision 1**, a token below the
    one every launcher read model and every peer already holds. The next guarded
    write then reads as a stale prediction against a server that silently
    rewound — and RD-L2/EG-5.1 arms exactly that comparison.

    **Anti-vacuity.** Falling through to base 0 is the mutation. *Probed fields:*
    the typed refusal class, AND that the archive file is still on disk with no
    new actor file beside it — the mutant returns an actor at revision 1 and
    writes it, and a store that merely raised something untyped could not satisfy
    the first probe.

    ``allow_class_key=True`` on the re-add is not incidental: since EG-6.6 the
    class-key fence runs FIRST inside ``upsert_actor``, so consent is what gets
    this write as far as the archive read at all — and the point of the test is
    that consenting to the resurrection does NOT also consent to inventing the
    revision token. Two fences, two decisions.
    """

    ws = _make_workspace()
    store = OfficeStore()
    actor = store.upsert_actor(ws, _actor_payload("dev"))
    for _ in range(6):
        actor = store.upsert_actor(ws, _actor_payload("dev"))
    assert actor.revision == 7
    store.remove_actor(ws, "dev")
    archived_path = paths.office_archived_actor_path(ws, "dev")
    assert archived_path.exists()
    archived_path.write_text("{truncated", encoding="utf-8")

    with pytest.raises(ArchiveUnreadable):
        store.upsert_actor(ws, _actor_payload("dev"), allow_class_key=True)

    # Nothing was written on the refusal path: no revision-1 actor file, and the
    # archive copy is left exactly as found for an operator to repair.
    assert not paths.office_actor_path(ws, "dev").exists()
    assert archived_path.read_text(encoding="utf-8") == "{truncated"


def test_an_already_archived_remove_over_an_unreadable_archive_refuses_typed():
    """The idempotent remove branch reads the same token, so it refuses the same
    way.

    That branch exists to make a repeated delete gesture harmless, and its ack
    carries the POST-archive revision — the token a later guarded write on this
    key must present. An undecodable archive there used to surface as whatever
    the JSON decoder happened to raise, which the RPC lane could only report as
    an untyped handler crash. One condition, one reason string, on both verbs.
    """

    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _actor_payload("dev"))
    store.remove_actor(ws, "dev")
    paths.office_archived_actor_path(ws, "dev").write_text("{truncated", encoding="utf-8")

    with pytest.raises(ArchiveUnreadable):
        store.remove_actor(ws, "dev")


# ── conflict guard ─────────────────────────────────────────────────────────


def test_conflict_sidecar_blocks_upsert_until_resolved():
    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _actor_payload("dev"))
    sidecar = paths.office_conflict_path(ws, "dev")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text('{"actor_key": "dev", "kind": "both_changed", "remote_actor": null}', encoding="utf-8")
    with pytest.raises(SyncConflict):
        store.upsert_actor(ws, _actor_payload("dev"))
    resolved = store.resolve_conflict(ws, "dev", take="local")
    assert resolved is not None and resolved.actor_key == "dev"
    assert "office.actor.conflict_resolved" in _event_types()
    # Resolution archives the sidecar; writes flow again.
    assert not sidecar.exists()
    store.upsert_actor(ws, _actor_payload("dev"))


# ── prune lane (plan §4.3) ─────────────────────────────────────────────────


def test_archive_actors_for_instance_archives_only_instance_bound():
    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _actor_payload("dev"))  # persona-keyed: survives
    store.upsert_actor(ws, _actor_payload("qa", persona_instance_id="personainst_goal9_qa"))
    result = store.archive_actors_for_instance("persona_personainst_goal9_qa")
    assert result == {"archived": 1, "failed": 0}
    assert store.actor_exists(ws, "dev")
    assert not store.actor_exists(ws, "personainst_goal9_qa")
    surface = store.get_surface(ws)
    assert "personainst_goal9_qa" in surface.archived_actor_keys


def test_a_prune_that_could_not_archive_its_match_says_so_instead_of_zero(monkeypatch):
    """``0`` used to mean two opposite things: nothing matched, and every match
    failed.

    The per-actor swallow STAYS — a prune must not die on one bad file, and the
    persona-instance retirement it serves is authoritative with or without the
    office projection — so the honest repair is a failure count, not a raise. The
    archive call is made to fail directly (a share violation is what this
    platform actually raises here) because the subject is the LOOP's accounting,
    not any one cause of failure.

    *Probed fields:* ``archived == 0`` AND ``failed == 1``, together. The old
    bare-int return could express only the first, and that is the very same
    answer "nothing matched" gives.
    """

    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _actor_payload("qa", persona_instance_id="personainst_goal9_qa"))

    def _refuse(*_args, **_kwargs):
        raise OSError("share violation")

    monkeypatch.setattr(store, "remove_actor", _refuse)

    result = store.archive_actors_for_instance("persona_personainst_goal9_qa")

    assert result == {"archived": 0, "failed": 1}
    # The loop survived the failure rather than propagating it, so the placement
    # is still there for an operator to prune again.
    assert store.actor_exists(ws, "personainst_goal9_qa")


# ── snapshot projection (W-H3) ─────────────────────────────────────────────


def test_snapshot_offices_section_and_conflict_parity_warning():
    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _actor_payload("dev"))
    sidecar = paths.office_conflict_path(ws, "dev")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text('{"actor_key": "dev", "kind": "both_changed", "remote_actor": null}', encoding="utf-8")

    snap = build_snapshot(event_log=EventLog())
    offices = snap["offices"]
    assert ws in offices
    row = offices[ws]
    assert row["actor_count"] == 1
    assert row["actors"][0]["actor_key"] == "dev"
    assert row["actors"][0]["items"][0]["position"] == [1.5, 2.0]
    assert row["conflict_actor_keys"] == ["dev"]
    assert row["orphaned"] is False
    codes = {w.get("code") for w in snap["parity"]["warnings"]}
    assert "office_actor_conflict" in codes
    assert snap["parity"]["contract_version"] == SNAPSHOT_CONTRACT_VERSION


# ── dry-run: full validation, zero writes, zero events (mutation-arg trap) ──
#
# The Stage-42 mutation scaffolding auto-registers ``--dry-run`` on every office
# verb; each MUST actually honor it. A dry-run performs full validation incl. the
# revision guard, returns the WOULD-BE result, and leaves the store byte-identical
# with no EventLog event (dry-runs are not mutations). Each test asserts the actor
# / surface file is byte-identical across the dry-run AND that no event was
# appended, then that the real run mutates + emits.


def _office_event_count() -> int:
    return sum(1 for _ in EventLog().iter_from_offset(0))


def test_upsert_dry_run_is_byte_identical_and_eventless():
    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _actor_payload("dev"))
    path = paths.office_actor_path(ws, "dev")
    before_bytes = path.read_bytes()
    before_events = _office_event_count()

    would_be = store.upsert_actor(ws, _actor_payload("dev"), dry_run=True)
    assert would_be.revision == 2, "dry-run must report the would-be (bumped) revision"
    assert path.read_bytes() == before_bytes, "dry-run must not rewrite the actor file"
    assert _office_event_count() == before_events, "dry-run must emit no event"

    # The real run mutates + emits. TWO events, not one: the S7-A office leg
    # pairs every actor-only upsert with an ``office_actor`` ``state.patched``
    # from inside the same lock (``OfficeStore._emit_actor_patch``). Asserted by
    # TYPE rather than as ``+2`` so the pairing is the thing under test — a bare
    # count would go green again if either half were replaced by an unrelated
    # event.
    real = store.upsert_actor(ws, _actor_payload("dev"))
    assert real.revision == 2
    assert path.read_bytes() != before_bytes
    emitted = [event.type for _, event in EventLog().iter_from_offset(0)][before_events:]
    assert emitted == ["state.patched", "office.actor.upserted"]


def test_upsert_dry_run_on_fresh_office_creates_nothing():
    ws = _make_workspace()
    store = OfficeStore()
    before_events = _office_event_count()
    would_be = store.upsert_actor(ws, _actor_payload("dev"), dry_run=True)
    assert would_be.actor_key == "dev"
    assert not store.actor_exists(ws, "dev"), "dry-run created an actor file"
    assert not store.surface_exists(ws), "dry-run lazily created the surface"
    assert _office_event_count() == before_events, "dry-run must emit no event"


def test_upsert_dry_run_still_enforces_revision_guard_and_validation():
    ws = _make_workspace()
    store = OfficeStore()
    actor = store.upsert_actor(ws, _actor_payload("dev"))
    with pytest.raises(StaleRevision):
        store.upsert_actor(ws, _actor_payload("dev"), expect_revision=actor.revision + 5, dry_run=True)
    with pytest.raises(ValueError):
        store.upsert_actor(ws, {"persona_id": "dev", "items": []}, dry_run=True)


def test_remove_dry_run_is_byte_identical_and_eventless():
    ws = _make_workspace()
    store = OfficeStore()
    created = store.upsert_actor(ws, _actor_payload("dev"))
    path = paths.office_actor_path(ws, "dev")
    before_bytes = path.read_bytes()
    before_events = _office_event_count()

    would_be = store.remove_actor(ws, "dev", dry_run=True)
    assert would_be.state == "archived"
    assert would_be.revision == created.revision + 1
    assert store.actor_exists(ws, "dev"), "dry-run archived the actor for real"
    assert not paths.office_archived_actor_path(ws, "dev").exists()
    assert path.read_bytes() == before_bytes
    assert _office_event_count() == before_events

    # The real run archives + emits. TWO events since 2026-08-16, for exactly
    # the reason the sibling upsert test above states: an archive is now PAIRED
    # with an ``office_actor`` ``state.patched`` (op ``remove``) from inside the
    # same lock, so ``office.actor.removed`` can be covered without shipping a
    # promoted frame whose office row never arrives (office fold-promotion plan
    # §V2/O-H1). Asserted by TYPE rather than as ``+2``, matching the upsert
    # test: a bare count goes green again if either half is replaced by an
    # unrelated event.
    removed = store.remove_actor(ws, "dev")
    assert removed.state == "archived"
    assert not store.actor_exists(ws, "dev")
    emitted = [event.type for _, event in EventLog().iter_from_offset(0)][before_events:]
    assert emitted == ["state.patched", "office.actor.removed"]


def test_restore_dry_run_is_byte_identical_and_eventless():
    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _actor_payload("dev"))
    store.remove_actor(ws, "dev")
    archive_path = paths.office_archived_actor_path(ws, "dev")
    before_bytes = archive_path.read_bytes()
    before_events = _office_event_count()

    would_be = store.restore_actor(ws, "dev", dry_run=True)
    assert would_be.state == "active"
    assert archive_path.exists(), "dry-run consumed the archive copy"
    assert not store.actor_exists(ws, "dev"), "dry-run restored the actor for real"
    assert archive_path.read_bytes() == before_bytes
    assert _office_event_count() == before_events

    restored = store.restore_actor(ws, "dev")
    assert restored.state == "active"
    assert store.actor_exists(ws, "dev")
    assert _office_event_count() == before_events + 1


def test_set_folders_dry_run_is_byte_identical_and_eventless():
    ws = _make_workspace()
    store = OfficeStore()
    store.ensure_surface(ws)
    surface_path = paths.office_surface_path(ws)
    before_bytes = surface_path.read_bytes()
    before_events = _office_event_count()
    before_revision = store.get_surface(ws).revision

    would_be = store.update_surface(ws, folders=["West Wing"], dry_run=True)
    assert "West Wing" in would_be.folders, "dry-run must report the would-be folders"
    assert would_be.revision == before_revision + 1
    assert surface_path.read_bytes() == before_bytes
    assert _office_event_count() == before_events
    assert store.get_surface(ws).revision == before_revision

    updated = store.update_surface(ws, folders=["West Wing"])
    assert "West Wing" in updated.folders
    assert surface_path.read_bytes() != before_bytes
    # TWO events, not one, since WV-H3 (2026-08-16): the ``office_surface``
    # ``state.patched`` row rides inside the lock beside its domain event, which
    # is what lets a folder-change batch be promoted instead of demoting the
    # whole thing to a full core. The DRY RUN's count above is the assertion
    # this test is about, and it is unchanged — a preview that logged either of
    # them would be claiming a revision the store does not hold.
    assert _office_event_count() == before_events + 2


def test_resolve_conflict_dry_run_leaves_sidecar_and_is_eventless():
    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _actor_payload("dev"))
    sidecar = paths.office_conflict_path(ws, "dev")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text('{"actor_key": "dev", "kind": "both_changed", "remote_actor": null}', encoding="utf-8")
    actor_path = paths.office_actor_path(ws, "dev")
    before_actor_bytes = actor_path.read_bytes()
    before_events = _office_event_count()

    would_be = store.resolve_conflict(ws, "dev", take="local", dry_run=True)
    assert would_be is not None and would_be.actor_key == "dev"
    assert sidecar.exists(), "dry-run archived the conflict sidecar for real"
    assert actor_path.read_bytes() == before_actor_bytes
    assert _office_event_count() == before_events
    # A dry-run does not unblock writes: the sidecar still guards upserts.
    with pytest.raises(SyncConflict):
        store.upsert_actor(ws, _actor_payload("dev"))

    resolved = store.resolve_conflict(ws, "dev", take="local")
    assert resolved is not None
    assert not sidecar.exists()
    assert _office_event_count() == before_events + 1
