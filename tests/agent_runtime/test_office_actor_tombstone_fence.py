"""D1 — an upsert of a DELETED actor key is refused, not read as intent.

The live wedge of 2026-08-27, in order:

1. `harness persona instance retire` acked `archived_actor_keys=[...]` with an
   empty `office_archive_failures` — the positive claim that every bound actor
   was off the level.
2. A launcher that had booted nineteen seconds earlier re-pushed those same
   actors as `state: active, updated_by: operator`.
3. `upsert_actor` read ANY upsert of an archived key as operator intent to
   re-add, so it cleared the resurrection-guard ledger entry AND unlinked the
   archive copy.
4. The retire REPLAY re-reads `archived_actor_keys_for_instance` to answer. With
   the archive copy gone it answered `already_retired: true,
   archived_actor_keys: []` — forever. Only `harness office actor-remove`
   cleared it.

Step 3 is the defect: a blind re-push carries no intent, and the store could not
tell it from one that did. The door now needs a key.

The fence's two evidence sources are tested separately (archive copy, ledger
entry) because they can legitimately disagree, and the tests that matter most
are the ones asserting the archive copy and the ledger entry SURVIVE a refusal —
that is the property whose absence made the wedge permanent rather than
annoying.
"""

from __future__ import annotations

import json

import pytest

from agent_runtime import paths
from agent_runtime.errors import ActorArchived
from agent_runtime.office_store import OfficeStore
from tests.agent_runtime.office_seed import seed_workspace_record

WORKSPACE = "ws_tombstone_fence"
QA_INSTANCE = "personainst_qa_agent_a1b2c3d4"


def _payload(x: float = 1.0, y: float = 2.0) -> dict:
    return {
        "persona_id": "qa",
        "persona_instance_id": QA_INSTANCE,
        "items": [
            {
                "item_id": QA_INSTANCE,
                "kind": "agent",
                "position": [x, y],
                "folder": "Agents",
                "display_name": "QA Agent",
            }
        ],
    }


@pytest.fixture
def store():
    seed_workspace_record(WORKSPACE)
    store = OfficeStore()
    store.ensure_surface(WORKSPACE, created_by="seed")
    return store


@pytest.fixture
def deleted(store):
    """One actor placed, then deleted — the state the launcher re-pushed into."""

    store.upsert_actor(WORKSPACE, _payload(), updated_by="seed-operator")
    store.remove_actor(WORKSPACE, QA_INSTANCE)
    assert QA_INSTANCE in store.get_surface(WORKSPACE).archived_actor_keys
    assert paths.office_archived_actor_path(WORKSPACE, QA_INSTANCE).exists()
    return store


# ── the refusal, and what it leaves behind ──────────────────────────────────


def test_a_mechanical_re_add_of_a_deleted_key_is_refused(deleted):
    with pytest.raises(ActorArchived) as caught:
        deleted.upsert_actor(WORKSPACE, _payload(9.0, 9.0))

    details = caught.value.safe_details
    assert details["actor_key"] == QA_INSTANCE
    assert details["workspace_id"] == WORKSPACE
    assert details["persona_instance_id"] == QA_INSTANCE
    assert caught.value.code == "actor_archived"


def test_the_refusal_leaves_the_archive_copy_and_the_ledger_intact(deleted):
    """THE property. Everything else about D1 is presentation.

    The wedge was not that the re-add happened — it was that the re-add
    DESTROYED the evidence the retire replay reads, so nothing downstream could
    ever discover the delete had been undone. A fence that refused the write but
    still cleared either witness would leave the wedge exactly where it was.
    """

    archived_path = paths.office_archived_actor_path(WORKSPACE, QA_INSTANCE)
    before = archived_path.read_text(encoding="utf-8")

    with pytest.raises(ActorArchived):
        deleted.upsert_actor(WORKSPACE, _payload(9.0, 9.0))

    assert archived_path.exists()
    assert archived_path.read_text(encoding="utf-8") == before
    assert QA_INSTANCE in deleted.get_surface(WORKSPACE).archived_actor_keys
    # And no live row was authored behind the refusal.
    assert not paths.office_actor_path(WORKSPACE, QA_INSTANCE).exists()
    assert deleted.list_actors(WORKSPACE) == []


def test_a_ledger_entry_alone_is_enough_to_refuse(deleted):
    """The two witnesses are independent, and either one refuses.

    A realm-sync pull rewrites the surface ledger without the archive file, and
    an archive file can be moved away by hand. A fence that demanded BOTH would
    be defeated by whichever half went missing first — which is precisely how
    the original wedge became unobservable.
    """

    paths.office_archived_actor_path(WORKSPACE, QA_INSTANCE).unlink()

    with pytest.raises(ActorArchived):
        deleted.upsert_actor(WORKSPACE, _payload(9.0, 9.0))


def test_an_archive_copy_alone_is_enough_to_refuse(deleted):
    from agent_runtime.office_store import _write_surface

    surface = deleted.get_surface(WORKSPACE)
    surface.archived_actor_keys = []
    _write_surface(surface)
    assert deleted.get_surface(WORKSPACE).archived_actor_keys == []
    assert paths.office_archived_actor_path(WORKSPACE, QA_INSTANCE).exists()

    with pytest.raises(ActorArchived):
        deleted.upsert_actor(WORKSPACE, _payload(9.0, 9.0))


# ── the door still opens for whoever holds the key ──────────────────────────


def test_the_explicit_resurrect_still_re_adds_and_clears_both_witnesses(deleted):
    """The door stays; it just needs a key now.

    Asserted as the FULL delta the launcher's fold mirrors (§V1's derivation
    table): the row is live and active, the ledger entry is gone, and the
    archive copy is gone. A fence that turned the consented path into a no-op
    would be a different bug wearing this one's clothes.
    """

    actor = deleted.upsert_actor(WORKSPACE, _payload(9.0, 9.0), resurrect=True)

    assert actor.state == "active"
    assert actor.actor_key == QA_INSTANCE
    assert QA_INSTANCE not in deleted.get_surface(WORKSPACE).archived_actor_keys
    assert not paths.office_archived_actor_path(WORKSPACE, QA_INSTANCE).exists()
    assert [a.actor_key for a in deleted.list_actors(WORKSPACE)] == [QA_INSTANCE]


def test_the_resurrected_row_carries_its_history_forward(deleted):
    """The consented path still reads the archived revision as its base.

    D1 moved the fence ABOVE the archive read, so this is the assertion that the
    move did not cost the revision token its meaning: the re-add must not come
    back at revision 1 (EG-1.5 / RD-H4).
    """

    resurrected = deleted.upsert_actor(WORKSPACE, _payload(9.0, 9.0), resurrect=True)
    assert resurrected.revision > 1


# ── the ordinary writes the fence must NOT touch ────────────────────────────


def test_a_first_placement_is_not_a_resurrection(store):
    actor = store.upsert_actor(WORKSPACE, _payload())
    assert actor.actor_key == QA_INSTANCE


def test_an_ordinary_move_of_a_live_actor_is_not_a_resurrection(store):
    store.upsert_actor(WORKSPACE, _payload(1.0, 1.0))
    moved = store.upsert_actor(WORKSPACE, _payload(5.0, 5.0))
    assert moved.items[0].position == (5.0, 5.0) or list(moved.items[0].position) == [5.0, 5.0]


def test_a_live_row_whose_key_is_still_in_the_ledger_is_not_refused(store):
    """The ledger-clearing half of the arm is NOT the resurrection half.

    A live actor whose key the surface still lists as archived is a ledger that
    needs cleaning up, not a delete being undone — and the fence's
    ``existing is None`` condition is what tells them apart. Reachable through a
    realm-sync pull, and pinned here because a fence that keyed only on the
    ledger would refuse a write to an actor that is plainly present.
    """

    from agent_runtime.office_store import _write_surface

    store.upsert_actor(WORKSPACE, _payload(1.0, 1.0))
    surface = store.get_surface(WORKSPACE)
    surface.archived_actor_keys = [QA_INSTANCE]
    _write_surface(surface)

    moved = store.upsert_actor(WORKSPACE, _payload(6.0, 6.0))
    assert moved.state == "active"
    # And the stale ledger entry was cleaned up rather than left to rot.
    assert QA_INSTANCE not in store.get_surface(WORKSPACE).archived_actor_keys


# ── the wire lane: the pinned contract ──────────────────────────────────────


def test_the_wire_refuses_with_the_pinned_code_and_reason(deleted):
    """The contract the launcher builds against, asserted as the WHOLE frame.

    `4090` with `data.reason = "actor_archived"`. A test that only checked the
    reason would pass against a lane that had quietly moved the code, and the
    code is what a client's error class switches on first.
    """

    from agent_runtime import serve_rpc  # noqa: F401 - imported for the lane
    from tests.agent_runtime.test_serve_rpc_office import SHUTDOWN, _reply, _rpc, _run

    reply = _reply(
        _run([
            _rpc("t1", "runtime.office.upsert", {
                "workspace_id": WORKSPACE,
                "actor": _payload(9.0, 9.0),
            }),
            SHUTDOWN,
        ]),
        "t1",
    )

    assert reply["error"]["code"] == 4090
    assert reply["error"]["data"] == {
        "reason": "actor_archived",
        "workspace_id": WORKSPACE,
        "actor_key": QA_INSTANCE,
        "persona_instance_id": QA_INSTANCE,
    }
    # The cure is stated and it is the terminal one.
    assert "new create" in reply["error"]["message"]


def test_the_wire_lane_has_no_resurrect_parameter(deleted):
    """A parameter is not consent — the doctrine ``allow_class_key`` already
    states on this lane, applied to the flag that replaced it.

    A client build that set this once, on the day drags started failing, would
    thereafter send it on every write from every install with no human in any
    loop. So the parameter is ignored and the refusal stands.
    """

    from tests.agent_runtime.test_serve_rpc_office import SHUTDOWN, _reply, _rpc, _run

    reply = _reply(
        _run([
            _rpc("t2", "runtime.office.upsert", {
                "workspace_id": WORKSPACE,
                "actor": _payload(9.0, 9.0),
                "resurrect": True,
            }),
            SHUTDOWN,
        ]),
        "t2",
    )

    assert reply["error"]["data"]["reason"] == "actor_archived"
    assert paths.office_archived_actor_path(WORKSPACE, QA_INSTANCE).exists()


# ── the CLI lane: the operator's door ───────────────────────────────────────


def test_the_cli_refuses_without_the_flag_and_re_adds_with_it(deleted, tmp_path, capsys):
    """Both halves through the REAL argparse tree, because a flag nothing routes
    to is a flag no operator can type."""

    import argparse

    from hermes_cli import harness

    def _run_cli(*extra: str) -> int:
        root = argparse.ArgumentParser(prog="hermes")
        harness.build_parser(root.add_subparsers(dest="command"))
        args = root.parse_args([
            "harness", "office", "actor-upsert",
            "--workspace", WORKSPACE,
            "--actor-json", json.dumps(_payload(9.0, 9.0)),
            "--json",
            *extra,
        ])
        return args.func(args)

    code = _run_cli()
    payload = json.loads(capsys.readouterr().out)
    assert code != 0
    assert payload["error"]["code"] == "actor_archived"
    assert "--resurrect" in payload["error"]["message"]
    assert paths.office_archived_actor_path(WORKSPACE, QA_INSTANCE).exists()

    code = _run_cli("--resurrect")
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    # Consent on the record, so a re-add is never an invisible event.
    assert [w["code"] for w in payload["warnings"]] == ["office_actor_resurrect_forced"]
    assert not paths.office_archived_actor_path(WORKSPACE, QA_INSTANCE).exists()
