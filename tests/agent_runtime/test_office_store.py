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
    ActorsUnreadable,
    ArchiveUnreadable,
    NotFound,
    StaleRevision,
    SyncConflict,
    WorkspaceUnresolved,
)
from agent_runtime.events import EventLog
from agent_runtime.office_store import (
    ARCHIVED_LEDGER_CAP,
    DuplicateDeskRefused,
    OfficeStore,
    merge_archived_ledgers,
)
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

    D1 adds a SECOND consent to this write, and the two are not the same
    question: ``allow_class_key`` says the key shape is deliberate, ``resurrect``
    says raising a deleted key is. This write is both, so it spells both.
    """

    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _actor_payload("dev"))
    store.remove_actor(ws, "dev")
    readded = store.upsert_actor(
        ws, _actor_payload("dev"), allow_class_key=True, resurrect=True
    )
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

    The two consents on the re-add are not incidental: since EG-6.6 the
    class-key fence runs FIRST inside ``upsert_actor``, and since D1 the
    tombstone fence runs before the archive is read at all, so BOTH are what get
    this write as far as the archive read — and the point of the test is that
    consenting to the resurrection does NOT also consent to inventing the
    revision token. Three fences, three decisions.
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
        store.upsert_actor(
            ws, _actor_payload("dev"), allow_class_key=True, resurrect=True
        )

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


# ── the desk fence: one persona, one live desk (D6) ────────────────────
#
# The rule was the LAUNCHER's alone until now — a gesture guard
# (``hasAuthoredDeskForPersona``) plus a render-time count — and neither stands
# in front of ``harness office actor-upsert``, which is the door the 2026-08-24
# incident authored a second ``qa`` desk through. These tests pin the store's
# half, and they pin the two ACCEPTANCES beside the refusal on purpose: a fence
# that refuses everything is not a fence, it is an outage, and both "move your
# own desk" and "the holder is archived" are writes an operator makes routinely.


def _desk_only_payload(persona_id: str, item_id: str, *, instance: str | None = None) -> dict:
    payload = {
        "persona_id": persona_id,
        "items": [
            {"item_id": item_id, "persona_id": persona_id, "kind": "desk", "position": [0.0, 0.0]}
        ],
    }
    if instance is not None:
        payload["persona_instance_id"] = instance
    return payload


def test_a_second_actor_desking_one_persona_is_refused_naming_the_holder():
    """The refusal, and the fact that it wrote NOTHING.

    ANTI-VACUITY. The kill-mutation is deleting the guard call from
    ``upsert_actor``. Under it the second write succeeds, so the ``pytest.raises``
    fails outright — but a guard that raised AFTER writing would satisfy that
    alone, which is why the actor list is re-read off disk afterwards. The
    incoming actor is INSTANCE-keyed so the older class-key fence (which refuses
    only class-keyed payloads) cannot be the thing doing the refusing, and the
    item ids are distinct so its ``duplicate_item_placement`` arm cannot fire
    either — a test that let either happen would pass against a store with no
    desk fence at all.

    The holder is named in both the message and ``safe_details``: a refusal that
    does not say WHICH desk is already there is one the operator cannot act on.
    """

    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _desk_only_payload("dev", "desk-dev"))

    with pytest.raises(DuplicateDeskRefused) as excinfo:
        store.upsert_actor(
            ws,
            _desk_only_payload("dev", "desk-dev-second", instance="personainst_dev_agent_1"),
        )

    details = excinfo.value.safe_details
    assert details["persona_id"] == "dev"
    assert details["holding_actor_key"] == "dev"
    assert details["holding_item_id"] == "desk-dev"
    assert details["item_id"] == "desk-dev-second"
    assert excinfo.value.code == "duplicate_desk"
    assert "'dev' already holds 'desk-dev'" in str(excinfo.value)
    # Nothing was written: one actor, one desk, and no event for the refusal.
    assert [a.actor_key for a in store.list_actors(ws)] == ["dev"]
    assert _event_types().count("office.actor.upserted") == 1


def test_moving_the_same_desk_is_not_a_duplicate():
    """The acceptance the naive predicate gets wrong.

    An upsert REPLACES the target actor's items, so the desk an actor is moving
    is the same desk it already holds — a fence that scanned every live actor
    INCLUDING the one being written would refuse every drag of every desk on the
    canvas. Two moves, not one, so a mutant that accepted only the first write
    (an off-by-one on the scan) is caught too.
    """

    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _desk_only_payload("dev", "desk-dev"))

    moved = store.upsert_actor(
        ws,
        {
            "persona_id": "dev",
            "items": [
                {"item_id": "desk-dev", "persona_id": "dev", "kind": "desk", "position": [4.0, 5.0]}
            ],
        },
    )
    assert moved.revision == 2
    assert [list(i.position) for i in moved.items] == [[4.0, 5.0]]

    # And again, with the desk RE-IDENTIFIED. Still one desk after the write, so
    # still legal: the invariant is one live desk per persona, not one immortal
    # item id.
    rekeyed = store.upsert_actor(ws, _desk_only_payload("dev", "desk-dev-v2"))
    assert rekeyed.revision == 3
    assert [i.item_id for i in rekeyed.items] == ["desk-dev-v2"]


def test_a_desk_whose_only_holder_is_archived_is_accepted():
    """Archive is not a holding.

    ``remove_actor`` archives rather than deletes, and the archived copy is where
    the revision token lives — so a fence that scanned ``include_archived=True``
    would look correct, keep every other test green, and quietly make an archived
    desk permanent: no verb could ever place that persona's desk again. THE
    killing mutation for this test is exactly that flag.
    """

    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _desk_only_payload("dev", "desk-dev"))
    store.remove_actor(ws, "dev")

    placed = store.upsert_actor(
        ws, _desk_only_payload("dev", "desk-dev-new", instance="personainst_dev_agent_1")
    )
    assert placed.actor_key == "personainst_dev_agent_1"
    assert [i.item_id for i in placed.items] == ["desk-dev-new"]
    # The archived holder is still on disk — the acceptance is not a deletion.
    assert paths.office_archived_actor_path(ws, "dev").exists()


def test_two_desks_for_one_persona_in_a_single_payload_are_refused():
    """The hole a state-only predicate would leave.

    The whole fence is walkable in one call if it only asks "does another actor
    hold a desk" — the writer it was built for (a hand-assembled
    ``--actor-json``) can simply put both desks in one payload. The predicate
    asks about the POST-WRITE state instead, so this falls out of the same
    sentence rather than needing a second branch.
    """

    ws = _make_workspace()
    store = OfficeStore()
    with pytest.raises(DuplicateDeskRefused) as excinfo:
        store.upsert_actor(
            ws,
            {
                "persona_id": "dev",
                "items": [
                    {"item_id": "desk-a", "persona_id": "dev", "kind": "desk", "position": [0.0, 0.0]},
                    {"item_id": "desk-b", "persona_id": "dev", "kind": "desk", "position": [1.0, 1.0]},
                ],
            },
        )
    assert excinfo.value.safe_details["holding_item_id"] == "desk-a"
    assert store.list_actors(ws) == []


def test_the_desk_fence_refuses_on_dry_run_too():
    """A preview whose job is to show what the real run would do must show the
    refusal — the same rule ``_guard_class_keyed_write`` records. A ``dry_run``
    that returned the would-be actor here teaches the operator that the write is
    fine and then fails it."""

    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _desk_only_payload("dev", "desk-dev"))
    with pytest.raises(DuplicateDeskRefused):
        store.upsert_actor(
            ws,
            _desk_only_payload("dev", "desk-2", instance="personainst_dev_agent_1"),
            dry_run=True,
        )


def test_the_desk_fence_refuses_rather_than_answering_from_half_a_directory():
    """Unknowable is not "no holder" (EG-6.6's rule, applied to this fence).

    A desk holder can only be proven ABSENT by reading every actor that might be
    one. A fence that answered "no conflict" from a directory it could only
    partly read would fail open on exactly the corrupt store where a duplicate is
    most likely. The mutation is dropping the ``scan.unreadable`` arm: the write
    then succeeds and this ``raises`` fails.
    """

    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _desk_only_payload("dev", "desk-dev"))
    (paths.office_actors_dir(ws) / "broken.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ActorsUnreadable):
        store.upsert_actor(
            ws, _desk_only_payload("ops", "desk-ops", instance="personainst_ops_agent_1")
        )


def test_a_desk_free_payload_never_pays_for_the_scan():
    """The fence is desk-triggered, which is what keeps every agent placement —
    the launcher's drop and ``agent create``'s placement leg, neither of which
    authors a desk (D6) — off the directory scan. Proven by leaving an UNREADABLE
    file in the directory: a fence that scanned unconditionally would raise
    ``ActorsUnreadable`` here, and an agent drop onto a store holding one stale
    file would start failing."""

    ws = _make_workspace()
    store = OfficeStore()
    store.ensure_surface(ws, created_by="seed")
    actors_dir = paths.office_actors_dir(ws)
    actors_dir.mkdir(parents=True, exist_ok=True)
    (actors_dir / "broken.json").write_text("{not json", encoding="utf-8")

    placed = store.upsert_actor(
        ws,
        {
            "persona_id": "dev",
            "persona_instance_id": "personainst_dev_agent_1",
            "items": [
                {"item_id": "personainst_dev_agent_1", "kind": "agent", "position": [1.0, 2.0]}
            ],
        },
    )
    assert placed.actor_key == "personainst_dev_agent_1"


def test_one_desk_claimed_by_two_rows_is_not_two_desks():
    """The narrowing, pinned where it is load-bearing.

    A desk's identity is its ``item_id``. One desk id claimed by two actor rows
    is a duplicate PLACEMENT — ``office_class_key_guard``'s
    ``duplicate_item_placement``, a different fault with a different cure — and
    it is the state the class→instance re-key migration deliberately passes
    through: ``scripts/office_actor_rekey_to_instance.py::_apply`` mints the
    instance-keyed actor with the class-keyed actor's items COPIED VERBATIM and
    only then archives the old key.

    So counting ROWS instead of ids would refuse the one operator script whose
    whole job is to move a placement, while catching nothing this fence exists
    for. THE killing mutation is exactly that: count holders rather than
    distinct ids, and this goes red while
    ``test_a_second_actor_desking_one_persona_is_refused_naming_the_holder``
    stays green — which is what makes the two tests a boundary rather than one
    assertion twice.
    """

    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _desk_only_payload("dev", "desk-dev"))

    # The migration's shape: same persona, same item, new key, old row still live.
    minted = store.upsert_actor(
        ws, _desk_only_payload("dev", "desk-dev", instance="personainst_dev_agent_1")
    )
    assert minted.actor_key == "personainst_dev_agent_1"
    assert {a.actor_key for a in store.list_actors(ws)} == {"dev", "personainst_dev_agent_1"}

    # …and the migration's second half still runs, leaving exactly one holder.
    store.remove_actor(ws, "dev")
    assert [a.actor_key for a in store.list_actors(ws)] == ["personainst_dev_agent_1"]


def test_the_cli_door_translates_the_refusal_into_exit_4_naming_the_holder():
    """The OTHER door's translation — the exit code and the envelope.

    The store tests above prove the fence; this proves ``harness office
    actor-upsert`` renders it as ``duplicate_desk`` in exit family 4 rather than
    as ``internal_error`` (exit 1), which is what an unmapped code falls through
    to (``ERROR_EXIT_CODES.get(code, 1)``). That fall-through is not
    hypothetical: it is exactly the failure ``archive_unreadable`` was added to
    the taxonomy to fix — a refused write reported as a harness crash.

    In-process rather than a child, for the reason
    ``test_office_class_key_one_fence`` records: the registered function IS the
    code the CLI reaches, and a subprocess costs two orders of magnitude more.

    ANTI-VACUITY. Three probes, killed by three different mutations: drop the
    ``except DuplicateDeskRefused`` arm and the exit becomes 1 with
    ``internal_error``; drop the taxonomy ROW and the code is right while the
    exit is 1; drop ``message=str(exc)`` and the holder vanishes from an
    envelope whose ``safe_details`` never carried it.
    """

    import contextlib
    import io
    import json
    from types import SimpleNamespace

    from hermes_cli.harness_parts import office as office_cli

    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _desk_only_payload("dev", "desk-dev"))

    args = SimpleNamespace(
        workspace=ws,
        actor_json=json.dumps(
            _desk_only_payload("dev", "desk-dev-2", instance="personainst_dev_agent_1")
        ),
        persona_instance_id=None,
        updated_by=None,
        expect_revision=None,
        allow_class_key=False,
        dry_run=False,
        json=True,
    )

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exit_code = office_cli._cmd_office_actor_upsert(args)

    assert exit_code == 4, buffer.getvalue()
    envelope = json.loads(buffer.getvalue())
    assert envelope["kind"] == "error"
    assert envelope["error"]["code"] == "duplicate_desk"
    assert "'dev' already holds 'desk-dev'" in envelope["error"]["message"]
    # And it wrote nothing.
    assert [a.actor_key for a in store.list_actors(ws)] == ["dev"]


# ── prune lane (plan §4.3) ─────────────────────────────────────────────────


def test_archive_actors_for_instance_archives_only_instance_bound():
    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _actor_payload("dev"))  # persona-keyed: survives
    store.upsert_actor(ws, _actor_payload("qa", persona_instance_id="personainst_goal9_qa"))
    result = store.archive_actors_for_instance("persona_personainst_goal9_qa")
    # The IDENTITIES leave with the counts (plan D7): ``agent retire``'s ack
    # names every actor it took off the level, and a count cannot be named.
    assert result == {
        "archived": 1,
        "failed": 0,
        "archived_actor_keys": ["personainst_goal9_qa"],
        "failures": [],
    }
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

    assert result["archived"] == 0
    assert result["failed"] == 1
    assert result["archived_actor_keys"] == []
    # And WHICH actor, and WHY — the half `agent retire` puts on its ack as
    # ``office_archive_failures``. A count answers "something is still on the
    # canvas"; only the key answers "that one is".
    assert result["failures"] == [
        {
            "actor_key": "personainst_goal9_qa",
            "workspace_id": ws,
            "error": "OSError: share violation",
        }
    ]
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


# --------------------------------------------------------------------------- #
# MC-8 / P10 — an office is not minted for a workspace no record resolves
# --------------------------------------------------------------------------- #
#
# ``ensure_surface`` authored a default surface for ANY id that passed
# ``_safe_id``. The measured consequence (EG-0.1): a leaked test context minted a
# LIVE office in the operator's runtime root — 135 events, a ``revision 67``
# actor file — for a workspace id no verb ever authorised. The parity warning
# describes that afterwards; these cases are the door.


def test_ensure_surface_refuses_an_id_no_workspace_record_resolves():
    """Refused, and refused BEFORE anything happened.

    Three separate claims, because "it refused" and "it refused before doing
    anything" are different statements and only the second makes it safe:

    *Kills, one per claim (C30):*

    * A — restore the lazy mint (delete the ``workspace_resolves`` guard): the
      ``pytest.raises`` reds, because nothing refuses at all;
    * B — raise AFTER ``_write_surface``: the raises still passes and the
      DIRECTORY assertion reds;
    * C — raise after ``self._emit``: the raises and the directory both pass and
      the EVENT assertion reds.
    """

    store = OfficeStore()
    before = _event_types()

    with pytest.raises(WorkspaceUnresolved) as excinfo:
        store.ensure_surface("ws_nope")

    assert excinfo.value.code == "workspace_unresolved", (
        "the refusal carries no machine reason, so every envelope that maps it "
        "falls back to internal_error — an operator refusal reported as a crash"
    )
    assert excinfo.value.safe_details.get("workspace_id") == "ws_nope", (
        "the refusal does not name the id it refused, so an operator holding a "
        "typo cannot tell which of their ids was rejected"
    )
    assert not paths.office_dir("ws_nope").exists(), (
        "the refusal left an office directory on disk. A refused write that "
        "still authored the surface is the defect wearing an exception."
    )
    assert _event_types() == before, (
        "the refusal emitted an event. An office.surface.created for a surface "
        "that does not exist is worse than the silent mint it replaced: every "
        "watermark-gated consumer folds a create for a workspace nobody has."
    )


def test_a_resolving_workspace_is_unaffected_archived_included():
    """The refusal must not touch the ordinary path, and archived still resolves.

    An archived workspace is a REAL record. Its office is not an orphan and
    refusing it would break the surface of a workspace the operator can restore.

    *Kill:* derive the predicate with ``list_all()`` instead of
    ``list_all(include_archived=True)`` in ``workspace_resolves``. The live case
    stays green and the archived one reds — which is why both are driven here
    rather than only the obvious one.
    """

    live = _make_workspace("Live Workspace")
    archived = WorkspaceStore().create(name="Archived Workspace")
    WorkspaceStore().archive(archived.id)

    store = OfficeStore()
    assert store.ensure_surface(live).workspace_id == live
    assert store.ensure_surface(archived.id).workspace_id == archived.id, (
        "an ARCHIVED workspace stopped resolving, so its office can no longer be "
        "authored or read; archiving is the reversible path and this makes it "
        "quietly destructive"
    )


def test_an_existing_surface_is_still_returned_after_its_workspace_disappears():
    """THE MUTATION MOST LIKELY TO BE MISSED, and the one that breaks the live store.

    The refusal guards CREATION, never reading. An office whose workspace record
    has since gone must still be returned: the projection, the ``orphaned_office``
    parity warning and ``archive_orphaned_surface`` all read through
    ``ensure_surface``, and that verb's entire precondition is
    ``workspace_resolves() is False``. A refusal placed above the
    ``surface_exists`` short-circuit would make the live orphan UNARCHIVABLE by
    the one verb that exists to archive it — the operator's only exit, closed by
    the fix meant to protect them.

    *Kill:* move the ``workspace_resolves`` refusal above the ``surface_exists``
    short-circuit. This reds; the refusal case above stays green, which is
    exactly why it cannot stand in for this one.
    """

    ws = _make_workspace("Doomed Workspace")
    store = OfficeStore()
    store.ensure_surface(ws)
    store.upsert_actor(ws, _actor_payload("dev"))
    paths.workspace_path(ws).unlink()
    assert not store.workspace_resolves(ws), "the fixture did not orphan the office"

    surface = store.ensure_surface(ws)
    assert surface.workspace_id == ws, (
        "an EXISTING office surface stopped being readable once its workspace "
        "record went away, so an operator cannot project, warn about or archive "
        "the orphan they already have"
    )
    assert store.archive_orphaned_surface(ws, dry_run=True)["workspace_id"] == ws, (
        "the archive verb — whose precondition is that the workspace does NOT "
        "resolve — can no longer preview the surface it exists to move"
    )


def test_the_template_clone_is_unaffected_because_it_creates_the_record_first():
    """The one caller worth reading rather than assuming.

    ``workspace_template._copy_office`` calls ``ensure_surface`` on the
    DESTINATION, so a clone into a workspace whose record did not yet exist would
    now refuse. It does not: the CLI create verb creates the workspace record and
    only then copies content ("Office/board content copies AFTER the workspace
    exists", ``hermes_cli/harness.py``). Both directions are driven so the pin
    states the behaviour rather than only the happy half.

    *Kill:* the ordering change this depends on — copy into a destination whose
    record does not exist yet. That is the second half below, and it is asserted
    as a REFUSAL rather than left undefined, so a future caller that clones
    before creating gets a named failure instead of a silent orphan.
    """

    from agent_runtime.workspace_template import copy_workspace_content

    source = _make_workspace("Template Source")
    OfficeStore().upsert_actor(source, _actor_payload("dev"))

    dest = WorkspaceStore().create(name="Template Destination")
    outcome = copy_workspace_content(source, dest.id, scopes=("office",))
    assert outcome["copied"]["office_actors"] >= 1, outcome
    assert OfficeStore().surface_exists(dest.id)

    with pytest.raises(WorkspaceUnresolved):
        copy_workspace_content(source, "ws_unrecorded_destination", scopes=("office",))
    assert not paths.office_dir("ws_unrecorded_destination").exists(), (
        "a clone into an unrecorded destination authored the office anyway"
    )


def test_the_refusal_reaches_the_operator_typed_and_not_as_an_internal_error():
    """What an operator actually sees, on the lane that can actually reach this.

    WHERE IT SURFACES, established rather than assumed. The RPC office arms
    (``runtime.office.upsert`` / ``…surface_update``) CANNOT reach this refusal:
    each pre-checks ``store.surface_exists`` and returns ``ERR_NOT_FOUND`` with
    ``reason=workspace_not_found`` before the store is asked to author anything.
    A ``WorkspaceUnresolved`` handler on those arms would be a catch that can
    never fire, so none was added — an always-green production branch is the same
    defect as an always-green test. The reachable lane is the CLI/argv one, and
    that is what this pins.

    Two claims, two kills:

    * the verb PROPAGATES the refusal to the harness dispatch boundary — it is
      not swallowed and not converted to a generic failure. ``hermes_cli.main``
      forks every exception out of a ``harness`` command into
      ``emit_harness_error`` rather than letting a traceback be the response, so
      reaching that boundary is what makes the envelope below the operator's
      view. *Kill:* wrap the store call in ``except Exception: return 1`` in
      ``harness_parts/office.py`` — the raises reds;
    * the taxonomy renders it TYPED. *Kill:* delete the ``WorkspaceUnresolved``
      row from ``_error_code_for_exception``. The exception is an
      ``AgentRuntimeError``, so it falls through to the catch-all and the
      envelope reads ``internal_error`` at exit 1 — an operator refusal reported
      as a harness crash, which is the exact defect that row's neighbour
      (``ArchiveUnreadable``) was added to fix. Both assertions below red.
    """

    import argparse
    import json as _json

    from hermes_cli.harness import build_parser
    from hermes_cli.harness_support import ERROR_EXIT_CODES, emit_harness_error

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args([
        "harness", "office", "actor-upsert",
        "--workspace", "ws_typo_here",
        "--actor-json", _json.dumps(
            {"persona_id": "qa", "items": [{"item_id": "qa", "kind": "agent", "position": [1, 1]}]}
        ),
        "--json",
    ])

    with pytest.raises(WorkspaceUnresolved) as excinfo:
        args.func(args)

    exit_code = emit_harness_error(excinfo.value, args=args)
    assert exit_code == ERROR_EXIT_CODES["workspace_unresolved"] == 3, (
        f"the refusal exits {exit_code}. Without its taxonomy row it falls to "
        "the AgentRuntimeError catch-all and exits 1 as internal_error — a "
        "refusal an operator caused, reported as a harness crash."
    )
    assert not paths.office_dir("ws_typo_here").exists(), (
        "the CLI write authored the office it was refusing"
    )


# ── C1: the archived-ledger union (M4) ─────────────────────────────────────


def test_the_ledger_union_leads_with_the_peer_so_a_subset_rehashes_identically():
    """C1's hash-neutrality property, at the level it lives at. When the local
    ledger is a SUBSET of the peer's, the union must BE the peer's list — same
    members, same order — or ``office_content_hash`` disagrees with the remote
    and every converged surface adopts into a permanent phantom local edit."""

    peer = ["a", "b", "c"]
    assert merge_archived_ledgers(peer, ["b"]) == peer
    assert merge_archived_ledgers(peer, []) == peer
    assert merge_archived_ledgers(peer, list(peer)) == peer


def test_local_only_keys_land_on_the_cap_surviving_tail():
    """The cap keeps the TAIL (``[-ARCHIVED_LEDGER_CAP:]``, the idiom
    ``_archive_actor_locked`` spends), so the union must put local-only keys
    there: this store is the only witness to a resurrection it alone archived,
    while a key the peer holds is guarded on the peer too."""

    assert merge_archived_ledgers(["a", "b"], ["z"]) == ["a", "b", "z"]

    over_cap = merge_archived_ledgers(
        [f"peer{i}" for i in range(ARCHIVED_LEDGER_CAP)], ["local_only"]
    )
    assert len(over_cap) == ARCHIVED_LEDGER_CAP
    assert over_cap[-1] == "local_only"
    assert "peer0" not in over_cap


def test_the_union_deduplicates_on_first_occurrence():
    """A repeat cannot be minted locally (``_archive_actor_locked`` guards it),
    so one can only have arrived from a peer — and carrying it forward would
    spend cap budget on a key already guarded."""

    assert merge_archived_ledgers(["a", "a", "b"], ["b", "c", "c"]) == ["a", "b", "c"]


# ── C4: the retire lane's reads join the completeness discipline (M8) ───────


def test_a_prune_over_an_unreadable_directory_reports_the_shortfall():
    """C4 (M8). The prune walked ``list_actors``, which drops what it could not
    decode and reports the remainder as complete — so a bound desk whose file
    would not open was neither archived nor counted, and ``failures: []`` claimed
    every bound actor was off the level. That empty list is the retire ack's
    positive claim (``agent_retire``'s own docstring), and it was a false one.

    The loop still survives: the readable bound desk is archived in the same
    call, which is what stops the opposite over-correction (refusing the whole
    prune) from passing here.
    """

    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _actor_payload("qa", persona_instance_id="personainst_c4_qa"))
    (paths.office_actors_dir(ws) / "broken.json").write_text("{not json", encoding="utf-8")

    result = store.archive_actors_for_instance("persona_personainst_c4_qa")

    assert result["archived"] == 1
    assert result["archived_actor_keys"] == ["personainst_c4_qa"]
    assert not store.actor_exists(ws, "personainst_c4_qa")
    # ``actor_key: None`` — the key of the file that would not decode is exactly
    # what could not be decoded, so naming one would be inventing it.
    assert result["failures"] == [
        {"actor_key": None, "workspace_id": ws, "error": "ActorsUnreadable: 1"}
    ]
    assert result["failed"] == 1


def test_the_replays_evidence_read_refuses_a_short_answer():
    """C4 (M8), the read-only half. ``archived_actor_keys_for_instance`` is the
    replay's EVIDENCE — the answer to "which desks are off the level" — and it
    was built from ``list_actors``. A bound desk whose archive copy will not
    decode came back as "not archived by this instance", which is the one thing
    an empty list is supposed to rule out. A short list here is not a smaller
    truth, it is a different claim."""

    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _actor_payload("qa", persona_instance_id="personainst_c4_ev"))
    store.remove_actor(ws, "personainst_c4_ev")
    assert store.archived_actor_keys_for_instance("persona_personainst_c4_ev") == [
        "personainst_c4_ev"
    ]

    (paths.office_archive_dir(ws) / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ActorsUnreadable):
        store.archived_actor_keys_for_instance("persona_personainst_c4_ev")


# ── H-H12: the store records what an item was minted as ──────────────────────


def _one_item(persona_id: str, item_id: str, kind: str) -> dict:
    return {
        "persona_id": persona_id,
        "items": [
            {
                "item_id": item_id,
                "persona_id": persona_id,
                "kind": kind,
                "position": [1.0, 2.0],
            }
        ],
    }


def test_the_minted_kind_is_stamped_once_and_never_moves_again():
    """H-H12. ``kind`` is mutable; what an item was MINTED as is not.

    Every upsert re-sends the whole item list, so any later write may re-spell
    an agent's item as a desk and the store accepts it — which left "was this
    really an agent?" a question with no stored answer, and the doctor's
    desk-litter classifier asking the ``item_id`` STRING for launcher naming
    conventions that nothing enforces.

    RED-FIRST against the arm this replaces: before it, ``minted_kind`` did not
    exist and this reds on the attribute.

    ANTI-VACUITY: the re-kinding write is a REAL store write, and ``kind`` is
    asserted to have actually moved. A stamp that simply copied ``kind`` every
    time would satisfy the first assertion and fail the second pair — which is
    the whole difference between recording a mint and echoing the last write.

    KILLING MUTATION: stamp from ``item.kind`` unconditionally (drop the
    ``on_record`` lookup in ``_stamp_minted_kinds``) and the third assertion
    reds.
    """

    ws = _make_workspace()
    store = OfficeStore()

    minted = store.upsert_actor(ws, _one_item("dev", "dev_thing", "agent"))
    assert [(i.kind, i.minted_kind) for i in minted.items] == [("agent", "agent")]

    rekinded = store.upsert_actor(ws, _one_item("dev", "dev_thing", "desk"))
    assert rekinded.items[0].kind == "desk", "the re-kinding write did not land"
    assert rekinded.items[0].minted_kind == "agent"
    # And it is DURABLE, not an in-memory artefact of the returned object.
    assert store.get_actor(ws, "dev").items[0].minted_kind == "agent"


def test_a_new_item_id_is_minted_fresh_beside_a_sticky_one():
    """Stickiness is per ITEM ID, not per actor.

    An actor gains and loses items over its life. Carrying one item's recorded
    mint onto another — or refusing to stamp a genuinely new item because the
    actor already existed — would both be wrong, and the second is the easier
    mistake to write.

    KILLING MUTATION: key ``on_record`` on anything but ``item_id`` (the actor,
    the kind, the index) and one of the two rows below reds.
    """

    ws = _make_workspace()
    store = OfficeStore()
    # Minted a DESK, so the two rows below cannot both be explained by one rule.
    store.upsert_actor(ws, _one_item("dev", "dev_thing", "desk"))

    grown = store.upsert_actor(
        ws,
        {
            "persona_id": "dev",
            "items": [
                # Re-kinded: sticky, so it must still read ``desk``.
                {
                    "item_id": "dev_thing",
                    "persona_id": "dev",
                    "kind": "agent",
                    "position": [1.0, 2.0],
                },
                # Never seen before: stamped from the kind it arrives with.
                {
                    "item_id": "dev_newcomer",
                    "persona_id": "dev",
                    "kind": "agent",
                    "position": [3.0, 4.0],
                },
            ],
        },
    )

    assert {i.item_id: i.minted_kind for i in grown.items} == {
        "dev_thing": "desk",
        "dev_newcomer": "agent",
    }


def test_a_client_cannot_declare_what_its_item_was_minted_as():
    """The field is the STORE's record, never the caller's assertion.

    A ``minted_kind`` a payload could set would be exactly the self-declaration
    this field replaced an id-spelling with — the classifier would be right back
    to trusting something the writer controls. ``_normalize_item`` does not read
    the key, so it cannot ride in.

    KILLING MUTATION: read ``minted_kind`` off ``raw`` in ``_normalize_item``
    and this reds.
    """

    ws = _make_workspace()
    store = OfficeStore()
    payload = _one_item("dev", "dev_thing", "desk")
    payload["items"][0]["minted_kind"] = "agent"

    actor = store.upsert_actor(ws, payload)

    assert actor.items[0].minted_kind == "desk"


def test_a_re_added_key_carries_its_recorded_mint_forward_from_the_archive():
    """A resurrection is the same item, not a second one.

    The same precedence ``base_revision`` uses one line down, and for the same
    reason: an actor re-added after a removal carries its history forward rather
    than starting a fresh one. A mint that reset here would hand an operator a
    clean-looking desk whose agent origin the store had just forgotten.

    KILLING MUTATION: pass only ``existing`` to ``_stamp_minted_kinds`` and this
    reds — ``existing`` is ``None`` on the re-add, which is precisely the arm
    ``archived`` exists for.
    """

    ws = _make_workspace()
    store = OfficeStore()
    # Instance-keyed, so the class-key fence (a separate consent, deliberately
    # not implied by ``resurrect``) is not what this test is about.
    minted = _one_item("dev", "dev_thing", "agent")
    minted["persona_instance_id"] = "personainst_dev_mint"
    store.upsert_actor(ws, minted)
    store.remove_actor(ws, "personainst_dev_mint")

    re_added = _one_item("dev", "dev_thing", "desk")
    re_added["persona_instance_id"] = "personainst_dev_mint"
    restored = store.upsert_actor(ws, re_added, resurrect=True)

    assert restored.items[0].minted_kind == "agent"


def test_the_recorded_mint_is_not_content_and_never_moves_the_sync_hash():
    """H-H12's blip, closed instead of accepted.

    ``office_content_hash`` is what the realm-sync lane compares to decide that
    an actor changed. ``minted_kind`` lives inside ``items``, which
    ``_HASH_EXCLUDE`` cannot reach, so on the first write after the upgrade
    every actor's hash would have moved once with nothing observable behind it —
    an unmeasured drift spike on the one lane whose whole job is detecting real
    drift — and any peer still decoding the field away would have disagreed with
    this install for as long as it stayed unupgraded.

    ANTI-VACUITY: the hash is asserted against the pre-field payload computed
    HERE, from the same encoder, with the key absent — not against a golden
    string that would rot, and not merely against itself. And ``kind`` is
    asserted to still move it, so the exclusion is proved narrow: it hides the
    provenance, never the content.

    KILLING MUTATION: drop the ``items`` re-filter from ``office_content_hash``
    and the first assertion reds.
    """

    import hashlib
    import json

    from agent_runtime.serde import to_jsonable

    ws = _make_workspace()
    store = OfficeStore()
    stamped = store.upsert_actor(ws, _one_item("dev", "dev_thing", "agent"))
    assert stamped.items[0].minted_kind == "agent", "fixture did not stamp"

    # What this function encoded before the field existed: the same payload with
    # the key simply absent.
    payload = {
        key: value
        for key, value in to_jsonable(stamped).items()
        if key not in office_models._HASH_EXCLUDE
    }
    payload["items"] = [
        {k: v for k, v in item.items() if k != "minted_kind"} for item in payload["items"]
    ]
    pre_field = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()

    assert office_models.office_content_hash(stamped) == pre_field, (
        "stamping the mint moved the sync hash: every actor reports as a local edit once"
    )

    # ...and the exclusion is NARROW: ``kind`` is content and still moves it.
    rekinded = store.upsert_actor(ws, _one_item("dev", "dev_thing", "desk"))
    assert rekinded.items[0].minted_kind == "agent"
    assert office_models.office_content_hash(rekinded) != pre_field


# ── the unaimed placement's slot, resolved under the lock (H-H10 / M10) ──────


def _unaimed_payload(persona_id: str, *, instance: str, folder: str = "Agents") -> dict:
    """A payload with NO ``position`` key — the shape ``placement_actor_payload``
    builds when the client did not aim."""

    return {
        "persona_id": persona_id,
        "persona_instance_id": instance,
        "items": [
            {
                "item_id": instance,
                "persona_id": persona_id,
                "kind": "agent",
                "folder": folder,
            }
        ],
    }


def test_a_positionless_item_is_refused_when_no_policy_came_with_it():
    """The pairing is enforced by the STORE, not by convention at the caller.

    ``placement_actor_payload`` omits the key rather than inventing an origin,
    so this refusal is the only thing standing between "the caller forgot the
    policy" and an agent silently placed at (0, 0) forever.

    *Mutation:* default the missing point to ``(0.0, 0.0)`` inside
    ``_item_point``. The mutant writes the actor and this raises nothing.
    """

    ws = _make_workspace()
    store = OfficeStore()
    with pytest.raises(ValueError, match="item position must be"):
        store.upsert_actor(ws, _unaimed_payload("qa", instance="personainst_m10_a"))
    assert store.scan_actors(ws).actors == []


def test_the_policy_answers_from_inside_the_lock_and_its_point_is_what_is_written():
    """The hook's answer reaches the FILE, and it reaches it having seen the
    floor the write lands beside.

    *Mutation:* ignore ``position_policy`` and read ``raw.get("position")``
    anyway — the write then refuses (no key), so the surviving mutant is the
    subtler one: call the policy but discard its answer. Both are caught by the
    stored coordinates, which is why this asserts the file rather than the
    return value.
    """

    ws = _make_workspace()
    store = OfficeStore()
    seen: list = []

    def _policy(scan):
        seen.append(scan)
        return (7.25, -3.5)

    actor = store.upsert_actor(
        ws,
        _unaimed_payload("qa", instance="personainst_m10_b"),
        position_policy=_policy,
    )
    assert [float(v) for v in actor.items[0].position] == [7.25, -3.5]
    stored = store.get_actor(ws, actor.actor_key)
    assert [float(v) for v in stored.items[0].position] == [7.25, -3.5]
    assert len(seen) == 1


def test_the_policy_is_handed_the_scan_and_not_a_bare_list():
    """The completeness question stays ASKABLE at the seam that can answer it.

    A store that unwrapped ``scan.actors`` here would drop the unreadable count
    on the floor at exactly the boundary the whole ``ActorScan`` shape exists to
    stop dropping it at — the policy could then never tell a floor it read whole
    from one it read half of.

    *Mutation:* pass ``self.scan_actors(wsid).actors``. ``scan.unreadable``
    raises ``AttributeError`` on a list and the mutant convicts.
    """

    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _actor_payload("dev"))
    (paths.office_actors_dir(ws) / "broken.json").write_text("{not json", encoding="utf-8")
    seen: list = []

    def _policy(scan):
        seen.append((len(scan.actors), scan.unreadable))
        return (0.0, 0.0)

    store.upsert_actor(
        ws,
        _unaimed_payload("qa", instance="personainst_m10_c"),
        position_policy=_policy,
    )
    assert seen == [(1, 1)]


def test_the_policy_sees_the_actor_set_the_write_lands_beside(monkeypatch):
    """M10's property, at the level it lives at: the scan the policy reads and
    the write that follows it are under ONE ``office_lock`` acquisition, so
    nothing can land in between.

    Proved by contending FROM INSIDE the policy: a second writer attempted while
    the hook is running must not be able to take the lock. Before H-H10 the
    equivalent read ran with the lock unheld, and that second writer landed —
    which is exactly how two unaimed creates came to share a slot.

    The deadline is shortened because the claim is "it could not get in", and
    the configured 15 s of proving that is 15 s of nothing happening. It also
    only refuses at all because H-H6 made the POSIX deadline reachable; on the
    old blocking ``flock`` this test would have deadlocked rather than passed
    or failed, which is why the two stages ship in that order.

    *Mutation:* resolve the policy BEFORE ``with office_lock(wsid)``. The
    contending write then succeeds inside the hook and ``blocked`` is False.
    """

    from agent_runtime import locks
    from agent_runtime.locks import HarnessLockUnavailable

    monkeypatch.setattr(locks, "_lock_timeout_seconds", lambda value: 0.2)

    ws = _make_workspace()
    store = OfficeStore()
    blocked: list = []

    def _policy(scan):
        try:
            OfficeStore().upsert_actor(
                ws,
                _actor_payload("rival", persona_instance_id="personainst_m10_rival"),
            )
            blocked.append(False)
        except HarnessLockUnavailable:
            blocked.append(True)
        return (1.0, 1.0)

    store.upsert_actor(
        ws,
        _unaimed_payload("qa", instance="personainst_m10_d"),
        position_policy=_policy,
    )
    assert blocked == [True], "a concurrent write reached the store mid-policy"


def test_a_multi_item_payload_is_refused_rather_than_guessed_at():
    """One point cannot answer for several items, and a store that picked one of
    them would be inventing a rule its caller never stated. Refused BEFORE
    ``ensure_surface``, so a refused call leaves the store as it found it.

    *Mutation:* apply the resolved point to ``raw_items[0]`` and leave the rest
    to their own keys. The mutant accepts a payload whose placement is half
    policy-chosen and half caller-chosen, with nothing saying which.
    """

    ws = _make_workspace()
    store = OfficeStore()
    payload = _unaimed_payload("qa", instance="personainst_m10_e")
    payload["items"].append(
        {"item_id": "second", "persona_id": "qa", "kind": "agent", "folder": "Agents"}
    )
    with pytest.raises(ValueError, match="position_policy resolves ONE item"):
        store.upsert_actor(ws, payload, position_policy=lambda scan: (0.0, 0.0))
    assert store.scan_actors(ws).actors == []


# ── typed per-key outcomes at the best-effort loops (H-H3) ──────────────────


def test_a_conflict_sidecar_that_would_not_decode_is_named_as_a_guess():
    """H-H3. The substitution stays; the silence goes.

    ``conflict_actor_keys`` answered a decode failure with ``path.stem`` and
    said nothing. The stem is ``actor_file_token(actor_key)`` — sanitised and,
    past 64 characters, truncated with a hash suffix — so for a long key it is
    not the actor key, and ``office resolve-conflict --actor <it>`` finds
    nothing. Both readers present these to an operator as keys to act on.

    *Mutation:* mint ``conflict_read`` in the ``except`` arm too. Every entry
    then claims to have been read out of a payload and ``unreadable`` answers 0.
    """

    ws = _make_workspace()
    store = OfficeStore()
    paths.office_conflict_path(ws, "dev").parent.mkdir(parents=True, exist_ok=True)
    paths.office_conflict_path(ws, "dev").write_text(
        '{"actor_key": "dev"}', encoding="utf-8"
    )
    (paths.office_conflicts_dir(ws) / "broken.json").write_text("{not json", encoding="utf-8")

    scan = store.scan_conflicts(ws)
    assert scan.keys == ["broken", "dev"]
    assert scan.unreadable == 1
    guessed = [o for o in scan.outcomes if not o.succeeded]
    assert [o.actor_key for o in guessed] == ["broken"]
    assert guessed[0].outcome.startswith("conflict_unreadable:")
    # The CLASS, never the message, in the token a program branches on.
    assert ":" in guessed[0].outcome and " " not in guessed[0].outcome


def test_a_readable_sidecar_with_no_actor_key_is_the_same_guess():
    """A payload that decoded fine and simply did not say is still a filename
    guess, and must not pass for a read key just because the JSON parsed.

    *Mutation:* fall through to ``conflict_read(workspace_id, key or path.stem)``.
    The keys list is identical, so only the outcome convicts — which is the
    point of having one.
    """

    ws = _make_workspace()
    store = OfficeStore()
    paths.office_conflicts_dir(ws).mkdir(parents=True, exist_ok=True)
    (paths.office_conflicts_dir(ws) / "silent.json").write_text("{}", encoding="utf-8")

    scan = store.scan_conflicts(ws)
    assert scan.keys == ["silent"]
    assert scan.unreadable == 1


def test_a_resolved_sidecar_is_not_a_conflict_and_is_never_read():
    """The skip is before the read, so a resolved sidecar cannot contribute an
    outcome of any kind — including a failure if it happened to be corrupt.

    *Mutation:* drop the ``.resolved.json`` skip. The resolved record shows up
    as a live conflict and the parity warning fires for work already done.
    """

    ws = _make_workspace()
    store = OfficeStore()
    paths.office_conflicts_dir(ws).mkdir(parents=True, exist_ok=True)
    (paths.office_conflicts_dir(ws) / "dev.resolved.json").write_text(
        "{not json", encoding="utf-8"
    )
    assert store.scan_conflicts(ws) == ([], [])


def test_the_prunes_ack_is_derived_from_its_outcomes_and_cannot_disagree():
    """H-H3. The four ack keys were two lists and two tallies kept in parallel;
    any one of them could drift from the others in silence. They are one typed
    list's projections now, and the counts are its lengths.

    Both arms in one workspace so the derivation is exercised across a mixed
    outcome set, which is the case a per-arm tally gets wrong.

    *Mutation:* return a hand-kept ``"archived": len(outcomes)``. The
    unreadable-scan row is not an archive, so the count over-reports by one.
    """

    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _actor_payload("qa", persona_instance_id="personainst_hh3_a"))
    (paths.office_actors_dir(ws) / "broken.json").write_text("{not json", encoding="utf-8")

    result = store.archive_actors_for_instance("persona_personainst_hh3_a")

    assert result["archived"] == len(result["archived_actor_keys"]) == 1
    assert result["failed"] == len(result["failures"]) == 1
    assert result["archived_actor_keys"] == ["personainst_hh3_a"]
    assert result["failures"] == [
        {"actor_key": None, "workspace_id": ws, "error": "ActorsUnreadable: 1"}
    ]
