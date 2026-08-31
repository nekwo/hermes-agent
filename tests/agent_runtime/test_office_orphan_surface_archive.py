"""`harness office archive-surface` — the operator's exit from an orphan.

An office surface whose workspace record has gone is projected anyway, ACCOUNTED
rather than hidden: ``snapshot._offices_summary`` stamps ``orphaned: true`` and
``snapshot._office_parity_warnings`` turns that into an ``orphaned_office``
warning, which the Launcher renders as a ``parity warnings`` chip. There was no
way to clear it. Board cards have had an "archive to repair" route since
inception; the office side had none, so the only exit was deleting directories by
hand inside the operator's live runtime root.

That is not academic: the 2026-08-17 investigation (EG-0.1 / HC §3) found the HUD
chip lit by exactly one such surface — ``ws_office_patch_test``, a workspace-less
office the test suite had been writing into the live root on every run. The leak
itself is fixed elsewhere in this stage (the three scoped-context sites plus
``isolate_agent_runtime_root``'s teardown tripwire); this file covers the verb
that lets the operator clear what the leak left behind.

THE VERB IS SHIPPED, NOT RUN. Clearing the real surface in ``X:/Eternia/.hermes``
is live-root surgery and stays the operator's hands only (standing rule, the
unified-create plan §7). Everything below runs against the per-test sandbox.

WHAT THE TESTS PROBE. The warning list ``_office_parity_warnings`` computes over
a real fixture store, before and after — presence then absence of the exact
``entity_id`` — plus the projection row itself. Both directions on the refusal,
because a guard test that only shows the refusal is green against a verb that
refuses everything. No timing anywhere.
"""

from __future__ import annotations

import argparse
import json

import pytest

from agent_runtime import paths
from agent_runtime.events import EventLog
from agent_runtime.office_store import OfficeStore
from agent_runtime.patch_coverage import (
    HISTORICAL_FOLD_ENTITIES,
    OFFICE_ACTOR_LIFECYCLE_CAPABILITY,
    OFFICE_SURFACE_FOLD_CAPABILITY,
    batch_is_patch_coverable,
)
from agent_runtime.snapshot import _offices_summary, _office_parity_warnings
from agent_runtime.state_patches import (
    OFFICE_ACTOR_ENTITY,
    OFFICE_SURFACE_ENTITY,
    PATCH_OP_REFRESH,
    STATE_PATCHED_EVENT_TYPE,
)
from agent_runtime.office_models import (
    ORPHANED_OFFICE_REASON_UNKNOWN,
    ORPHANED_OFFICE_WORKSPACE_DELETED,
    ORPHANED_OFFICE_WORKSPACE_NEVER_RECORDED,
    classify_orphaned_office_workspace,
)
from agent_runtime.store import DELETED_WORKSPACE_LEDGER_CAP, RealmStore, WorkspaceStore
from tests.agent_runtime.office_seed import (
    seed_workspace_record,
    unlink_workspace_record,
)


GHOST = "ws_ghost_office"


def _actor_payload(persona_id: str = "qa") -> dict:
    key = f"personainst_{persona_id}_agent_0001"
    return {
        "persona_id": persona_id,
        "persona_instance_id": key,
        "items": [
            {
                "item_id": key,
                "kind": "agent",
                "persona_id": persona_id,
                "position": [1.0, 2.0],
                "folder": "Agents",
                "display_name": f"{persona_id} agent",
            }
        ],
    }


def _seed_orphan(workspace_id: str = GHOST) -> OfficeStore:
    """A surface with a real placement and NO workspace record behind it.

    Built record-first and then UNLINKED, because MC-8 / P10 shut the door this
    fixture used to walk through: ``ensure_surface`` refuses an id no workspace
    record resolves, so the old "call it on a bare string" seed is now exactly
    the thing under test and cannot be the thing that builds the fixture.

    The replacement is also the more faithful model. An orphan does not arise in
    the field from an office authored out of nothing — that path is closed now —
    but from a workspace record that went away while its office stayed, which is
    literally what these two steps do. ``WorkspaceStore.delete`` is deliberately
    NOT used: its cascade ``rmtree``s the office subtree, so driving it would
    destroy the surface this file exists to examine.
    """

    seed_workspace_record(workspace_id)
    store = OfficeStore()
    store.ensure_surface(workspace_id)
    store.upsert_actor(workspace_id, _actor_payload())
    unlink_workspace_record(workspace_id)
    assert not store.workspace_resolves(workspace_id), (
        "the fixture left a resolvable workspace record behind, so nothing below "
        "is testing an orphan"
    )
    return store


def _offices() -> list[dict]:
    # ``.offices``: the projection now carries the rows AND the count of
    # workspaces whose surface would not decode (ML-8b/3).
    #
    # The realm list is passed exactly as ``build_snapshot`` passes it — it is
    # what lets an orphaned row say WHICH KIND of orphan it is. Omitting it here
    # would leave every case below reading ``unknown``, and the reason gates
    # would be measuring the fixture rather than the classifier.
    return _offices_summary(
        OfficeStore(),
        WorkspaceStore().list_all(include_archived=True),
        RealmStore().list_all(include_archived=True),
    ).offices


def _warnings(code: str = "orphaned_office") -> list[dict]:
    return [w for w in _office_parity_warnings({"offices": _offices()}) if w["code"] == code]


def _warning_ids(code: str = "orphaned_office") -> list[str]:
    return [w["entity_id"] for w in _warnings(code)]


def _tombstone(workspace_id: str, *, realm_name: str = "Ledger Realm"):
    """A realm whose delete ledger already holds ``workspace_id``.

    Written directly rather than by driving ``WorkspaceStore.delete``, because
    that cascade ``rmtree``s the office subtree — driving it would destroy the
    very surface under test. This reproduces the state that ACTUALLY reaches the
    classifier in the field: a tombstone in the ledger beside a surface the
    cascade did not manage to remove.
    """

    realm = RealmStore().create(name=realm_name)
    realm.deleted_workspace_ids = [workspace_id]
    RealmStore().save(realm, emit_event=False)
    return realm


def _run_verb(*argv: str) -> int:
    from hermes_cli.harness import build_parser

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["harness", *argv])
    return args.func(args)


def _log_end() -> int:
    # Verbatim from test_office_state_patches._log_end: the log's end offset, so
    # ``iter_from_offset`` returns only the rows this test caused.
    return max((o for o, _ in EventLog().iter_from_offset(0)), default=0)


# --------------------------------------------------------------------------- #
# The happy path: the warning goes away
# --------------------------------------------------------------------------- #
def test_archiving_an_orphaned_surface_clears_its_parity_warning(capsys):
    """THE property the operator cares about, on the real warning computation."""

    _seed_orphan()

    # Before: projected, flagged, and the chip's entity_id is this workspace.
    assert [row["workspace_id"] for row in _offices()] == [GHOST]
    assert _warning_ids() == [GHOST]

    assert _run_verb("office", "archive-surface", "--workspace", GHOST, "--json") == 0
    row = json.loads(capsys.readouterr().out)
    assert row["kind"] == "office_archived"
    assert row["workspace_id"] == GHOST
    assert row["actor_count"] == 1

    # After: gone from the projection, and no warning left to render.
    assert _offices() == []
    assert _warning_ids() == []


def test_the_surface_is_moved_and_not_deleted(capsys):
    """Archive-never-delete, like every other removal in this store.

    A verb that unlinked the directory would clear the chip too — and would make
    a mis-aimed archive unrecoverable. The placement JSON has to still exist.
    """

    _seed_orphan()
    actor_path = paths.office_actor_path(GHOST, "personainst_qa_agent_0001")
    assert actor_path.exists()

    assert _run_verb("office", "archive-surface", "--workspace", GHOST, "--json") == 0
    capsys.readouterr()

    assert not paths.office_dir(GHOST).exists()
    archived = paths.office_archived_surface_dir(GHOST)
    assert (archived / "office.json").exists()
    assert (archived / "actors" / f"{actor_path.name}").exists()


def test_the_archive_root_is_outside_the_projected_tree(capsys):
    """Why the archive is a SIBLING of ``office/`` and not a child.

    ``OfficeStore.list_workspaces()`` enumerates ``office_root()``'s children by
    the presence of ``office.json``. An archive nested under it would still be
    listed, still be projected, and still raise its warning — the verb would
    report success and change nothing an operator can see. This pins the layout
    that makes the clearing real rather than the assertion that it happened.
    """

    _seed_orphan()
    assert _run_verb("office", "archive-surface", "--workspace", GHOST, "--json") == 0
    capsys.readouterr()

    archive_root = paths.office_surface_archive_root()
    office_root = paths.office_root()
    assert archive_root.exists()
    assert office_root not in archive_root.resolve().parents
    assert archive_root != office_root
    # The store's own enumerator is the authority the projection reads.
    assert OfficeStore().list_workspaces() == []


def test_archiving_a_second_orphan_of_the_same_id_does_not_collide(capsys):
    """A re-created orphan archived twice must not wedge the operator.

    Refusing the second archive would leave the chip lit with no first-class way
    out — the state this verb exists to end.
    """

    _seed_orphan()
    assert _run_verb("office", "archive-surface", "--workspace", GHOST, "--json") == 0
    capsys.readouterr()

    _seed_orphan()
    assert _run_verb("office", "archive-surface", "--workspace", GHOST, "--json") == 0
    row = json.loads(capsys.readouterr().out)

    assert row["archived_as"] == f"{GHOST}-2"
    assert paths.office_archived_surface_dir(GHOST).exists()
    assert (paths.office_surface_archive_root() / f"{GHOST}-2").exists()
    assert _warning_ids() == []


# --------------------------------------------------------------------------- #
# The refusal — the other direction
# --------------------------------------------------------------------------- #
def test_a_surface_whose_workspace_still_resolves_is_refused(capsys):
    """THE anti-vacuity check for this verb.

    Without this, "archives an orphan" is satisfied by a verb that archives
    ANYTHING — and aimed at a live workspace this one moves every placement on
    the canvas out of the projection. So the refusal is driven on a surface that
    differs from the orphan above in exactly one respect: a workspace record
    exists. Same verb, same store, same seeding.
    """

    workspace = WorkspaceStore().create(name="Live Canvas")
    store = OfficeStore()
    store.ensure_surface(workspace.id)
    store.upsert_actor(workspace.id, _actor_payload())

    # Not an orphan, so nothing was flagged in the first place.
    assert _warning_ids() == []

    code = _run_verb("office", "archive-surface", "--workspace", workspace.id, "--json")
    assert code == 2, "a refusal is invalid_request (exit 2), not an internal error"
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "invalid_request"
    assert "NOT orphaned" in error["message"]

    # Nothing moved: the surface, its placement and the projection row survive.
    assert paths.office_surface_path(workspace.id).exists()
    assert not paths.office_surface_archive_root().exists()
    assert [row["workspace_id"] for row in _offices()] == [workspace.id]
    assert store.scan_actors(workspace.id).actors[0].actor_key == "personainst_qa_agent_0001"


def test_an_archived_workspace_still_resolves_and_is_refused(capsys):
    """An ARCHIVED workspace's office is not an orphan.

    ``snapshot.py:450`` builds the projection against
    ``list_all(include_archived=True)``, so an archived workspace's surface is
    never flagged — and archiving it would be data loss for a workspace the
    operator may still restore. The predicate has to read the same list the
    warning does, which is a different answer from ``list_all()``'s default.
    """

    workspace = WorkspaceStore().create(name="Retired Canvas")
    OfficeStore().ensure_surface(workspace.id)
    workspace.archived = True
    WorkspaceStore().save(workspace)

    assert workspace.id not in {w.id for w in WorkspaceStore().list_all()}
    assert workspace.id in {
        w.id for w in WorkspaceStore().list_all(include_archived=True)
    }
    assert _warning_ids() == []

    assert _run_verb("office", "archive-surface", "--workspace", workspace.id, "--json") == 2
    assert "NOT orphaned" in json.loads(capsys.readouterr().out)["error"]["message"]
    assert paths.office_surface_path(workspace.id).exists()


def test_a_surface_that_does_not_exist_is_a_lookup_miss(capsys):
    assert _run_verb("office", "archive-surface", "--workspace", "ws_nothing", "--json") == 3
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "not_found"


# --------------------------------------------------------------------------- #
# Dry run, and the event
# --------------------------------------------------------------------------- #
def test_dry_run_previews_the_archive_without_moving_anything(capsys):
    _seed_orphan()
    before = _log_end()

    assert (
        _run_verb("office", "archive-surface", "--workspace", GHOST, "--dry-run", "--json")
        == 0
    )
    row = json.loads(capsys.readouterr().out)

    assert row["dry_run"] is True
    assert row["workspace_id"] == GHOST
    assert row["actor_count"] == 1
    assert row["archived_as"] == GHOST
    # Untouched store, and no event: a preview that emitted would put a
    # mutation on the log for a mutation that never happened.
    assert paths.office_surface_path(GHOST).exists()
    assert not paths.office_surface_archive_root().exists()
    assert _warning_ids() == [GHOST]
    assert [e.type for _, e in EventLog().iter_from_offset(before)] == []


def test_the_archive_batch_can_never_be_promoted_to_a_patch_frame(capsys):
    """THE fold fence, probed on the classifier rather than on a constant.

    ``office.surface.updated`` — the domain event this archive rides — is patch
    COVERED: it promises a folding client that an equivalent ``office_surface``
    patch rides the same batch. An archive removes the offices row AND every
    actor row under it and has no patch shape, so left alone that promise is a
    silent loss: ``batch_is_patch_coverable`` is an ``all(...)`` with no
    "at least one patch" requirement, so the batch would ship a patch frame with
    an EMPTY patches list, the client would advance its watermark having folded
    nothing, and the archived surface — and its chip — would stay forever.

    The ``office_surface`` ``refresh`` beside it is what demotes the batch. So
    the probe is ``batch_is_patch_coverable`` itself, asked with EVERY entity and
    capability token declared — the most generous client that can exist. It must
    still say no.
    """

    _seed_orphan()
    before = _log_end()

    assert _run_verb("office", "archive-surface", "--workspace", GHOST, "--json") == 0
    capsys.readouterr()

    events = [e for _, e in EventLog().iter_from_offset(before)]
    types = [e.type for e in events]

    updated = [e for e in events if e.type == "office.surface.updated"]
    assert len(updated) == 1, types
    assert updated[0].payload["change"] == "archived"
    assert updated[0].payload["workspace_id"] == GHOST

    patches = [e for e in events if e.type == STATE_PATCHED_EVENT_TYPE]
    assert [p.payload["op"] for p in patches] == [PATCH_OP_REFRESH], [
        p.payload for p in patches
    ]
    assert patches[0].payload["entity"] == OFFICE_SURFACE_ENTITY
    assert patches[0].payload["id"] == GHOST

    # The maximal client: every fold entity plus every capability token.
    everything = HISTORICAL_FOLD_ENTITIES | {
        OFFICE_SURFACE_ENTITY,
        OFFICE_ACTOR_ENTITY,
        OFFICE_SURFACE_FOLD_CAPABILITY,
        OFFICE_ACTOR_LIFECYCLE_CAPABILITY,
    }
    assert not batch_is_patch_coverable(events, fold_entities=everything), types
    # And the promise the demote is protecting really is a promise: the domain
    # event on its own IS coverable for that client, which is the loss this
    # test exists to keep impossible.
    assert batch_is_patch_coverable(updated, fold_entities=everything)


def test_the_store_predicate_and_the_projection_cannot_disagree():
    """``workspace_resolves`` is the verb's whole safety property, and the
    ``orphaned`` flag is the warning's. They must be the same answer.

    Asserted as an equivalence over both cases rather than by re-deriving the
    rule, because a copy of the membership test is exactly how the two would
    drift — and the drift direction that matters (predicate says orphan, the
    projection says live) is a silent mass delete.
    """

    live = WorkspaceStore().create(name="Both Ways")
    store = OfficeStore()
    store.ensure_surface(live.id)
    _seed_orphan()

    by_projection = {row["workspace_id"]: bool(row["orphaned"]) for row in _offices()}
    by_predicate = {
        wsid: not store.workspace_resolves(wsid) for wsid in store.list_workspaces()
    }

    assert by_projection == by_predicate
    assert by_projection == {live.id: False, GHOST: True}


def test_the_verb_needs_a_workspace(capsys):
    assert _run_verb("office", "archive-surface", "--json") == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_request"


def test_the_envelope_says_which_root_answered(capsys):
    """Root observability, per the 2026-08-12 ambient-root incident: a verb whose
    whole subject is "which surfaces exist in this root" must name the root."""

    _seed_orphan()
    assert _run_verb("office", "archive-surface", "--workspace", GHOST, "--json") == 0
    row = json.loads(capsys.readouterr().out)

    assert "resolution" in row
    assert row["resolution"].get("error_kind") is None


# --------------------------------------------------------------------------- #
# MC-8 / P10 — the warning says WHICH KIND of orphan it is
# --------------------------------------------------------------------------- #
#
# Every orphan used to be worded "which no longer resolves", a sentence that
# PRESUMES the workspace once did. The live field case had never had a workspace
# record at all — 135 events for the id, zero ``workspace.created``/``deleted`` —
# so the operator was told to look for a deletion that had not happened.
#
# The distinction is decided from the realm ``deleted_workspace_ids`` ledgers,
# and the reason is derived beside ``orphaned`` (in ``_offices_summary``) rather
# than recomputed at the warning, so the flag and its explanation cannot come
# from two different readings of the store.


def test_a_deleted_workspace_reads_workspace_deleted():
    """A tombstone in a realm ledger is PROOF, and the wording follows it.

    *Kill:* collapse the detail map to one wording for every reason (make
    ``_ORPHANED_OFFICE_DETAIL`` return the never-recorded sentence for all three
    keys). The reason field still reads ``workspace_deleted`` — which is why the
    DETAIL is asserted here too, and why this case reds while the classifier
    itself is untouched.
    """

    _seed_orphan()
    _tombstone(GHOST)

    warning = next(w for w in _warnings() if w["entity_id"] == GHOST)
    assert warning["reason"] == ORPHANED_OFFICE_WORKSPACE_DELETED, (
        "an id sitting in a realm's deleted_workspace_ids ledger is a recorded "
        "deletion, and the warning must say so rather than leaving the operator "
        "to guess whether a workspace ever existed"
    )
    # Asserted against LITERAL discriminating phrases, not against the module's
    # own detail map: reading the map would make this pass under exactly the
    # mutation it is written to catch, since a collapsed map returns the same
    # string for every key.
    assert "was deleted" in warning["detail"], (
        "the reason is typed but the sentence beside it does not follow it, so "
        "the operator still reads one wording for two different situations"
    )
    assert "ever created" not in warning["detail"], (
        "the deleted case is being described with the never-recorded sentence"
    )


def test_a_never_recorded_workspace_reads_workspace_never_recorded():
    """The live field case: an office minted for an id no record ever named.

    Run as its own case against its own mutation rather than sharing the one
    above: two arms of a discrimination are two claims, and a single kill that
    reds "some case" proves neither of them individually (C30).

    *Kill:* the same detail-map collapse, asserted from THIS side — make every
    reason render the DELETED sentence. The reason field still reads
    ``workspace_never_recorded`` and the detail assertion reds.
    """

    _seed_orphan()
    # A realm EXISTS with an empty ledger: that is what makes the negative
    # meaningful. With no realms at all the honest answer is ``unknown``, which
    # the case below pins.
    RealmStore().create(name="Empty Ledger Realm")

    warning = next(w for w in _warnings() if w["entity_id"] == GHOST)
    assert warning["reason"] == ORPHANED_OFFICE_WORKSPACE_NEVER_RECORDED, (
        "no realm ledger holds this id and none of them is at the cap, so the "
        "honest reading is that no workspace record was ever created — the "
        "measured shape of the ws_office_patch_test leak"
    )
    assert "ever created" in warning["detail"], (
        "the reason is typed but the sentence beside it still describes a "
        "deletion, which is the wording this whole arm exists to stop"
    )
    assert "was deleted" not in warning["detail"], (
        "the never-recorded case is being described as a deletion — the exact "
        "sentence that sent the operator hunting a deletion that never happened"
    )


def test_a_full_ledger_reads_unknown_rather_than_claiming_never_recorded():
    """The cap is not lied about — an evicted id is not a "never".

    ``deleted_workspace_ids`` is bounded at ``DELETED_WORKSPACE_LEDGER_CAP`` with
    the OLDEST entries falling off first. A ledger sitting AT the cap has been
    evicting, so a miss against it proves nothing at all, and answering
    ``workspace_never_recorded`` there would be a confident sentence built on a
    truncated list — the same shape as a lister that drops rows it could not read
    and reports the remainder as complete.

    *Kill:* delete the cap arm from ``classify_orphaned_office_workspace`` (the
    ``any(len(ledger) >= ledger_cap …)`` branch). The full ledger then reads
    ``workspace_never_recorded`` and this reds, while both cases above stay green
    — which is what makes this a claim of its own rather than a restatement.
    """

    _seed_orphan()
    realm = RealmStore().create(name="Full Ledger Realm")
    realm.deleted_workspace_ids = [f"ws_evicted_{i:04d}" for i in range(DELETED_WORKSPACE_LEDGER_CAP)]
    RealmStore().save(realm, emit_event=False)
    assert GHOST not in realm.deleted_workspace_ids, "the fixture put the id IN the ledger"

    warning = next(w for w in _warnings() if w["entity_id"] == GHOST)
    assert warning["reason"] == ORPHANED_OFFICE_REASON_UNKNOWN, (
        f"a realm ledger at its {DELETED_WORKSPACE_LEDGER_CAP}-entry cap has "
        "been dropping its oldest entries, so this id may have been recorded and "
        "evicted. Reporting 'never recorded' from a list that is known to be "
        "incomplete states as fact something the store cannot support."
    )


def test_no_realms_at_all_reads_unknown():
    """Nothing could have recorded anything, so a miss carries no information.

    The C16 rule at this site: an arm that cannot compute its answer says so in
    its own words instead of borrowing the confident sentence next to it.

    *Kill:* drop the ``if not ledgers`` arm so an empty ledger set falls through
    to ``workspace_never_recorded``. This reds and the never-recorded case above
    stays green, because that one deliberately creates a realm.
    """

    _seed_orphan()
    assert not RealmStore().list_all(include_archived=True), "the fixture created a realm"

    warning = next(w for w in _warnings() if w["entity_id"] == GHOST)
    assert warning["reason"] == ORPHANED_OFFICE_REASON_UNKNOWN, (
        "with no realms on the store there is no ledger that could have recorded "
        "a deletion, so 'never recorded' would be inferred from the absence of "
        "any evidence either way"
    )


def test_the_warning_code_token_does_not_carry_the_reason():
    """One token, discrimination in a FIELD.

    ``code`` is what a census greps and what the launcher's ``warningCodes``
    reads. Splitting this condition into ``orphaned_office_workspace_deleted`` /
    ``…_never_recorded`` would silently zero every existing count of it and break
    the chip without any consumer erroring.

    *Kill:* rename the emitted code to carry the reason
    (``f"orphaned_office_{reason}"``). This reds, and so does the parity-warning
    catalog's producibility gate — two independent witnesses for the same rule.
    """

    _seed_orphan()
    _tombstone(GHOST)
    second = "ws_ghost_two"
    _seed_orphan(second)

    codes = {w["code"] for w in _office_parity_warnings({"offices": _offices()})}
    assert "orphaned_office" in codes, (
        "the orphaned-office warning no longer spells the code every census and "
        "the launcher's parity chip look for"
    )
    # Only GHOST is tombstoned, so the two orphans classify DIFFERENTLY under the
    # one shared code — which is a sharper statement of the rule than two rows
    # agreeing would have been.
    reasons = {w["entity_id"]: w["reason"] for w in _warnings()}
    assert reasons == {
        GHOST: ORPHANED_OFFICE_WORKSPACE_DELETED,
        second: ORPHANED_OFFICE_WORKSPACE_NEVER_RECORDED,
    }, (
        "two orphans in one projection did not each get their own reason under "
        "the one shared code — the discrimination has to live in the field, per "
        "row, and it has to be computed per row rather than once for the frame"
    )
    assert not any(code.startswith("orphaned_office_") for code in codes), (
        f"a reason leaked into the code token: {sorted(codes)}. A consumer "
        "counting 'orphaned_office' silently reads zero the day that ships."
    )


def test_a_resolving_workspace_produces_no_warning_and_no_reason():
    """Archived included — ``workspace_resolves``' own doctrine.

    An archived workspace still resolves: its office is not an orphan, and
    archiving it would be data loss rather than cleanup. The row must also carry
    NO reason, because a reason on a non-orphan is a sentence looking for a
    warning to attach itself to.

    *Kill:* make ``_offices_summary`` second-guess its caller and filter archived
    rows out of the workspace set it was handed
    (``{w.id for w in workspaces if not w.archived}``). The archived workspace's
    office becomes an orphan, gains a reason, and this reds. Aimed INSIDE the
    projection deliberately: this case drives ``_offices_summary`` with a set
    that already includes archived workspaces, so a mutation of ``build_snapshot``'s
    call site could not reach it — the caller's own ``include_archived=True`` is
    pinned by the store-predicate equivalence case above.
    """

    live = WorkspaceStore().create(name="Still Here")
    archived = WorkspaceStore().create(name="Archived But Real")
    WorkspaceStore().archive(archived.id)
    store = OfficeStore()
    store.ensure_surface(live.id)
    store.ensure_surface(archived.id)

    rows = {row["workspace_id"]: row for row in _offices()}
    for wsid, label in ((live.id, "live"), (archived.id, "archived")):
        assert rows[wsid]["orphaned"] is False, f"the {label} workspace's office reads as an orphan"
        assert rows[wsid]["orphan_reason"] is None, (
            f"the {label} workspace's office is not an orphan yet carries an "
            f"orphan reason ({rows[wsid]['orphan_reason']!r})"
        )
    assert _warning_ids() == [], (
        "a workspace that still resolves raised an orphaned_office warning; for "
        "the archived one that would send the operator to archive-surface, which "
        "moves a whole live office out of the projection"
    )


# --------------------------------------------------------------------------- #
# The classifier as a pure function — the rule, without a store in the way
# --------------------------------------------------------------------------- #
def test_the_classifier_is_a_pure_rule_over_the_ledgers():
    """Every arm of the rule, driven directly, including the ones a fixture
    cannot cheaply reach (a hit in the SECOND realm's ledger; a whitespace-padded
    ledger entry; an empty id).

    These are not restatements of the cases above: those prove the projection
    ASKS this question, this proves the answer is right at every branch. A
    classifier tested only through the projection is one whose branches are
    exercised by whatever fixtures happened to be written.
    """

    call = classify_orphaned_office_workspace
    cap = DELETED_WORKSPACE_LEDGER_CAP

    assert call("ws_a", deleted_ledgers=[["ws_a"]], ledger_cap=cap) == ORPHANED_OFFICE_WORKSPACE_DELETED
    assert call("ws_a", deleted_ledgers=[[], ["ws_a"]], ledger_cap=cap) == ORPHANED_OFFICE_WORKSPACE_DELETED, (
        "only the first realm's ledger is being consulted"
    )
    assert call("ws_a", deleted_ledgers=[["  ws_a  "]], ledger_cap=cap) == ORPHANED_OFFICE_WORKSPACE_DELETED, (
        "a padded ledger entry is the same id and must match"
    )
    assert call("ws_a", deleted_ledgers=[["ws_b"]], ledger_cap=cap) == ORPHANED_OFFICE_WORKSPACE_NEVER_RECORDED
    assert call("ws_a", deleted_ledgers=[[]], ledger_cap=cap) == ORPHANED_OFFICE_WORKSPACE_NEVER_RECORDED, (
        "a realm with an empty ledger is evidence: it exists and recorded nothing"
    )
    assert call("ws_a", deleted_ledgers=[], ledger_cap=cap) == ORPHANED_OFFICE_REASON_UNKNOWN
    assert call("ws_a", deleted_ledgers=[["ws_b"] * cap], ledger_cap=cap) == ORPHANED_OFFICE_REASON_UNKNOWN
    assert call("ws_a", deleted_ledgers=[[], ["ws_b"] * cap], ledger_cap=cap) == ORPHANED_OFFICE_REASON_UNKNOWN, (
        "one full ledger anywhere makes the negative unprovable, even beside an "
        "under-cap one"
    )
    assert call("", deleted_ledgers=[["ws_a"]], ledger_cap=cap) == ORPHANED_OFFICE_WORKSPACE_NEVER_RECORDED, (
        "an empty id must not match a ledger entry by accident"
    )
