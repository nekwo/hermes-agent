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
from agent_runtime.store import WorkspaceStore


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
    """A surface with a real placement and NO workspace record behind it."""

    store = OfficeStore()
    store.ensure_surface(workspace_id)
    store.upsert_actor(workspace_id, _actor_payload())
    return store


def _offices() -> list[dict]:
    return _offices_summary(
        OfficeStore(), WorkspaceStore().list_all(include_archived=True)
    )


def _warning_ids(code: str = "orphaned_office") -> list[str]:
    warnings = _office_parity_warnings({"offices": _offices()})
    return [w["entity_id"] for w in warnings if w["code"] == code]


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
    assert store.list_actors(workspace.id)[0].actor_key == "personainst_qa_agent_0001"


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
