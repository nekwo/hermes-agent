"""The METHOD lane's WRITE leg: ``runtime.office.upsert``.

Sibling of ``test_serve_rpc_office.py`` and built on its helpers rather than a
second copy of them. What a write leg has to prove that a read leg does not:

1. **The ack is LIGHT and is enough.** Asserted as a whole frame — a test that
   only checked ``"revision" in result`` would pass against a runtime that
   returned the entire actor on the hot path of a drag.
2. **The reconciliation LOOP closes.** The number the read leg publishes is
   fed straight back as ``expect_revision`` and must be accepted; the number
   the ack returns must be accepted by the write after it. Asserted as a round
   trip, because either half alone passes against two lanes that disagree about
   whose revision they mean (surface's vs actor's — they differ, and the
   surface's does not move when an actor moves).
3. **A refusal WRITES NOTHING.** Every error test re-reads the store and pins
   the placement it was trying to move. A typed conflict frame in front of a
   store that took the write anyway is the worst possible outcome: the client
   rolls its prediction back and the server keeps it.
4. **The identity binding SURVIVES the write.** A write that drops
   ``persona_instance_id`` re-keys the actor onto its persona class and splits
   one placement into two actor files.
5. **The argv lane is untouched** — byte-for-byte, with a WRITE beside it, and
   with the second-answer guard the read leg's suite already carries.
"""

from __future__ import annotations

import json

from agent_runtime import serve_rpc
from tests.agent_runtime.office_seed import seed_workspace_record
from tests.agent_runtime.test_serve_rpc_office import (
    SHUTDOWN,
    _argv,
    _argv_lane_lines,
    _frames,
    _lines,
    _reply,
    _rpc,
    _run,
)

WORKSPACE = "ws_rpc_upsert_test"

#: The instance-bound actor the seed places, and the key the store canonicalizes
#: its identity triple onto.
QA_INSTANCE = "personainst_qa_agent_9c8a382f"


# ── seeding ─────────────────────────────────────────────────────────────────


def _store():
    from agent_runtime.office_store import OfficeStore

    return OfficeStore()


def _actor_payload(x: float, y: float, *, instance: str | None = QA_INSTANCE) -> dict:
    """The identity triple + items, exactly the shape ``--actor-json`` takes.

    Deliberately ONE schema across both lanes: a second payload shape for the
    method lane would be a second normalizer to keep in step with the store's.
    """

    payload: dict = {
        "persona_id": "qa",
        "items": [
            {
                "item_id": QA_INSTANCE,
                "kind": "agent",
                "position": [x, y],
                "folder": "Agents",
                "display_name": "QA Agent",
            },
            {
                "item_id": "qa_desk",
                "kind": "desk",
                "position": [x, y - 2.5],
                "folder": "Desks",
            },
        ],
    }
    if instance is not None:
        payload["persona_instance_id"] = instance
    return payload


def _seed(workspace_id: str = WORKSPACE):
    """One instance-bound actor at revision 1, in an authored office."""

    store = _store()
    seed_workspace_record(workspace_id)
    store.ensure_surface(workspace_id, created_by="seed")
    store.upsert_actor(workspace_id, _actor_payload(-8.0, -2.0), updated_by="seed-operator")
    return store


def _positions(workspace_id: str = WORKSPACE) -> dict[str, list[float]]:
    """Every placed item's position, read back off disk — server truth."""

    return {
        item.item_id: [float(item.position[0]), float(item.position[1])]
        for actor in _store().list_actors(workspace_id)
        for item in actor.items
    }


def _office(rid: str = "read", workspace_id: str = WORKSPACE) -> dict:
    out = _run([_rpc(rid, "runtime.office.get", {"workspace_id": workspace_id}), SHUTDOWN])
    return _reply(out, rid)["result"]


def _upsert(rid: str, params: dict) -> dict:
    return _reply(_run([_rpc(rid, "runtime.office.upsert", params), SHUTDOWN]), rid)


# ── the happy path, and the whole ack ───────────────────────────────────────


def test_the_upsert_acks_light_with_only_the_key_and_the_new_revision():
    """The ruling's ack, asserted as a WHOLE frame.

    ``{actor_key, revision}`` and nothing else: returning the re-projected actor
    would hand the client back its own input plus a number, on the hot path of a
    drag, and would tempt it to adopt the echo instead of keeping the prediction
    it already drew. Whole-frame equality is what makes "light" a property of
    this test rather than a comment.
    """

    _seed()
    assert _positions()[QA_INSTANCE] == [-8.0, -2.0]

    reply = _upsert("w1", {"workspace_id": WORKSPACE, "actor": _actor_payload(3.5, 9.0)})

    assert reply == {
        "jsonrpc": "2.0",
        "id": "w1",
        "result": {"actor_key": QA_INSTANCE, "revision": 2},
    }
    # And the write actually landed — an ack in front of an unchanged store is
    # the one failure a shape assertion alone cannot see.
    assert _positions() == {QA_INSTANCE: [3.5, 9.0], "qa_desk": [3.5, 6.5]}


def test_the_acked_actor_key_is_canonical_and_is_not_what_the_client_sent():
    """Why ``actor_key`` is in the ack at all.

    The store canonicalizes the identity triple at its own write boundary, so a
    client that sent the ``persona_personainst_*`` drift alias gets back a key
    it did not send and could not have derived. That key — not the token it
    typed — is what the read projection reports and what a later remove needs.
    """

    seed_workspace_record(WORKSPACE)
    _store().ensure_surface(WORKSPACE, created_by="seed")

    reply = _upsert(
        "w-alias",
        {
            "workspace_id": WORKSPACE,
            "actor": {
                "persona_id": "dev",
                "persona_instance_id": "persona_personainst_dev_agent_3ebfce41",
                "items": [{"item_id": "desk-dev", "kind": "desk", "position": [3.0, 4.0]}],
            },
        },
    )

    acked = reply["result"]["actor_key"]
    assert acked == "personainst_dev_agent_3ebfce41"
    assert acked != "persona_personainst_dev_agent_3ebfce41"
    # The store agrees, so the ack is truth and not a re-derivation beside it.
    assert [a.actor_key for a in _store().list_actors(WORKSPACE)] == [acked]


def test_an_upsert_of_an_existing_actor_updates_it_and_never_forks_a_second_file():
    """Idempotent by key (``office_store`` header). Two writes, one actor file —
    the invariant that makes a drag stream safe to send repeatedly."""

    _seed()
    first = _upsert("w1", {"workspace_id": WORKSPACE, "actor": _actor_payload(1.0, 1.0)})
    second = _upsert("w2", {"workspace_id": WORKSPACE, "actor": _actor_payload(2.0, 2.0)})

    assert first["result"] == {"actor_key": QA_INSTANCE, "revision": 2}
    assert second["result"] == {"actor_key": QA_INSTANCE, "revision": 3}
    assert [a.actor_key for a in _store().list_actors(WORKSPACE)] == [QA_INSTANCE]


# ── the desk fence, on this lane (D6) ───────────────────────────────────────


def _desk_payload(item_id: str, *, instance: str | None = None) -> dict:
    """A desk-only actor for persona ``qa`` — the shape the 2026-08-24 incident
    hand-assembled, minus the item ids that would trip the OLDER fence."""

    payload: dict = {
        "persona_id": "qa",
        "items": [{"item_id": item_id, "kind": "desk", "position": [0.0, 0.0], "folder": "Desks"}],
    }
    if instance is not None:
        payload["persona_instance_id"] = instance
    return payload


def test_a_second_desk_for_one_persona_is_refused_as_a_whole_frame():
    """The store's fence, TRANSLATED — asserted as a whole frame.

    The seed already places ``qa_desk`` under the instance-keyed actor, so this
    write is the second desk for one persona. It is instance-keyed (a different
    instance) with a distinct item id, so neither arm of the class-key fence can
    be the thing refusing it — a test that let ``class_key_collision`` answer
    here would pass against a runtime with no desk fence at all.

    Whole-frame equality rather than ``data["reason"] == ...`` because the
    ``data`` is the whole point: a client that cannot see WHICH actor holds the
    desk has nothing to offer the operator but a retry, and a retry never clears
    this. The message is this lane's own sentence — the store's ends by naming
    ``harness office actor-remove``, a verb no wire caller has.
    """

    _seed()
    reply = _upsert(
        "desk-dup",
        {
            "workspace_id": WORKSPACE,
            "actor": _desk_payload("qa_desk_second", instance="personainst_qa_agent_00000002"),
        },
    )

    assert reply == {
        "jsonrpc": "2.0",
        "id": "desk-dup",
        "error": {
            "code": 4090,
            "message": (
                "desk write for persona 'qa' refused: "
                f"{QA_INSTANCE!r} already holds desk 'qa_desk'. A persona has one "
                "desk on a level; move that desk, or remove it with "
                "runtime.office.remove before placing another."
            ),
            "data": {
                "reason": "duplicate_desk",
                "workspace_id": WORKSPACE,
                "persona_id": "qa",
                "holding_actor_key": QA_INSTANCE,
                "holding_item_id": "qa_desk",
            },
        },
    }
    # A typed refusal in front of a store that took the write is the worst
    # outcome: the client rolls its prediction back and the server keeps it.
    assert [a.actor_key for a in _store().list_actors(WORKSPACE)] == [QA_INSTANCE]
    assert _positions() == {QA_INSTANCE: [-8.0, -2.0], "qa_desk": [-8.0, -4.5]}


def test_the_seeded_actor_may_still_move_its_own_desk_over_this_lane():
    """The acceptance beside the refusal, on the SAME lane.

    Without it the test above passes against a fence that refuses every desk
    write, which would take the office canvas offline for every desk drag while
    reporting a correct-looking refusal reason.
    """

    _seed()
    reply = _upsert("desk-move", {"workspace_id": WORKSPACE, "actor": _actor_payload(2.0, 7.0)})

    assert reply["result"] == {"actor_key": QA_INSTANCE, "revision": 2}
    assert _positions() == {QA_INSTANCE: [2.0, 7.0], "qa_desk": [2.0, 4.5]}


# ── the instance binding, which the write must not drop ─────────────────────


def test_the_write_honours_the_instance_binding_and_the_fence_refuses_the_rekey():
    """Design question 4, from both sides.

    WITH the binding the write lands on the instance-keyed actor already there.
    WITHOUT it the same persona_id would key onto the CLASS — a second actor
    file holding the same ``item_id`` values, one agent rendered twice, and a
    client whose next guarded write targets whichever of the two it happened to
    read. ``office_class_key_guard`` refuses that write, and this pins both the
    pass-through and the refusal in one place because they are one behaviour:
    the binding is what distinguishes them.
    """

    _seed()
    bound = _upsert("bound", {"workspace_id": WORKSPACE, "actor": _actor_payload(1.0, 1.0)})
    assert bound["result"]["actor_key"] == QA_INSTANCE
    assert [a.actor_key for a in _store().list_actors(WORKSPACE)] == [QA_INSTANCE]

    unbound = _upsert(
        "unbound",
        {"workspace_id": WORKSPACE, "actor": _actor_payload(1.0, 1.0, instance=None)},
    )
    assert unbound["error"]["data"]["reason"] == "class_key_collision"
    # No second actor file, which is the harm the reason names.
    assert [a.actor_key for a in _store().list_actors(WORKSPACE)] == [QA_INSTANCE]


def test_a_class_keyed_write_that_would_duplicate_a_placement_is_a_typed_refusal():
    """The fence, as a whole frame, with its own ``data.reason``.

    Not ``stale_revision``: the client is not behind and refetching tells it
    nothing new. Not ``actor_invalid``: the payload is well-formed and was legal
    before the class→instance re-key. The remedy is a third thing — name WHICH
    instance you are placing — so it gets a third reason. ``data`` carries the
    guard's own structured facts, including the key it collided WITH, because a
    refusal that does not name the other actor is one nobody can act on.
    """

    _seed()
    reply = _upsert(
        "dup",
        {"workspace_id": WORKSPACE, "actor": _actor_payload(1.0, 1.0, instance=None)},
    )

    assert reply == {
        "jsonrpc": "2.0",
        "id": "dup",
        "error": {
            "code": 4090,
            "message": (
                "class-keyed write for persona 'qa' refused: duplicate_item_placement "
                f"(conflicts with {QA_INSTANCE}). The class→instance re-key archived "
                "this class key; writing it back undoes that migration. Send "
                "persona_instance_id to place a specific instance."
            ),
            "data": {
                "reason": "class_key_collision",
                "workspace_id": WORKSPACE,
                "persona_id": "qa",
                "class_actor_key": "qa",
                "reasons": ["duplicate_item_placement"],
                "conflicting_actor_keys": [QA_INSTANCE],
            },
        },
    }
    assert _positions()[QA_INSTANCE] == [-8.0, -2.0]


def test_a_class_keyed_write_that_would_resurrect_an_archived_key_is_refused():
    """The guard's OTHER narrow reason, which no item-id overlap would catch.

    ``upsert_actor`` treats an explicit upsert of an archived key as operator
    intent to re-add and CLEARS the resurrection ledger. Over the wire nobody is
    holding that intent, so the same write that is a feature at a terminal is
    silent corruption from a client. Seeded through a real
    ``remove_actor`` so the ledger entry is the store's own, not a fixture's.
    """

    store = _seed()
    store.upsert_actor(
        WORKSPACE,
        {
            "persona_id": "ghost",
            "items": [{"item_id": "ghost_desk", "kind": "desk", "position": [1.0, 1.0]}],
        },
    )
    store.remove_actor(WORKSPACE, "ghost", updated_by="seed-operator")
    assert "ghost" in store.get_surface(WORKSPACE).archived_actor_keys

    reply = _upsert(
        "resurrect",
        {
            "workspace_id": WORKSPACE,
            "actor": {
                "persona_id": "ghost",
                # Fresh item id, so ONLY the archived-key reason can fire — the
                # two reasons are proven independent rather than co-triggered.
                "items": [{"item_id": "ghost_desk_2", "kind": "desk", "position": [2.0, 2.0]}],
            },
        },
    )

    assert reply["error"]["code"] == 4090
    assert reply["error"]["data"]["reason"] == "class_key_collision"
    assert reply["error"]["data"]["reasons"] == ["resurrects_archived_class_key"]
    assert reply["error"]["data"]["conflicting_actor_keys"] == []
    # Nothing to point AT on this branch, so the message must not invent one —
    # the ledger is the whole evidence.
    assert "conflicts with" not in reply["error"]["message"]
    assert "resurrects_archived_class_key." in reply["error"]["message"]
    # The ledger still guards the key, which is the thing the write would have
    # cleared on its way through.
    assert "ghost" in _store().get_surface(WORKSPACE).archived_actor_keys
    assert not _store().actor_exists(WORKSPACE, "ghost")


def test_a_class_keyed_write_on_a_clean_canvas_still_goes_through():
    """The fence is CONDITIONAL, not a ban — and this is the assertion that
    keeps it that way. Class-keyed placements are a supported shape (the store
    says so itself: "Persona-id-keyed placements survive instance churn by
    design"). A guard that refused every unbound write would outlaw a legal
    canvas, and would pass every test above."""

    _seed()
    reply = _upsert(
        "clean",
        {
            "workspace_id": WORKSPACE,
            "actor": {
                "persona_id": "archivist",
                "items": [{"item_id": "desk-archivist", "kind": "desk", "position": [5.0, 5.0]}],
            },
        },
    )

    assert reply["result"] == {"actor_key": "archivist", "revision": 1}
    assert sorted(a.actor_key for a in _store().list_actors(WORKSPACE)) == [
        "archivist",
        QA_INSTANCE,
    ]


def test_no_wire_parameter_can_force_a_class_keyed_write_past_the_fence():
    """The CLI's ``--allow-class-key`` has no equivalent here, and the asymmetry
    is deliberate.

    A flag is consent: an operator read the refusal, typed the override, and
    owns the double placement. A wire PARAMETER is not — it becomes a constant
    in a client build, set once by whoever was debugging the day drags started
    failing, and sent forever by every install with no human in any loop. The
    wire client also needs it least: the read projection hands it
    ``persona_instance_id`` on every item, so its remedy is to send back the
    binding it was already given.

    Pinned by trying the names an implementer would reach for. Unknown params
    are IGNORED by this method, so a future author who adds one has to delete
    this test rather than merely not notice it.
    """

    _seed()
    for rid, extra in {
        "allow": {"allow_class_key": True},
        "force": {"force": True},
        "override": {"allow_class_key": "yes", "force_class_key": True},
    }.items():
        reply = _upsert(
            rid,
            {
                "workspace_id": WORKSPACE,
                "actor": _actor_payload(1.0, 1.0, instance=None),
                **extra,
            },
        )
        assert reply["error"]["data"]["reason"] == "class_key_collision", rid

    assert [a.actor_key for a in _store().list_actors(WORKSPACE)] == [QA_INSTANCE]


def test_the_binding_survives_the_round_trip_onto_the_read_projection():
    """Cross-LEG parity: what the write bound is what the read reports. A write
    that stored the raw token instead of the canonical one would still ack a
    canonical key (the store re-reads) while the projection disagreed."""

    seed_workspace_record(WORKSPACE)
    _store().ensure_surface(WORKSPACE, created_by="seed")
    _upsert(
        "w",
        {
            "workspace_id": WORKSPACE,
            "actor": {
                "persona_id": "dev",
                "persona_instance_id": "persona_personainst_dev_agent_3ebfce41",
                "items": [{"item_id": "desk-dev", "kind": "desk", "position": [3.0, 4.0]}],
            },
        },
    )

    items = {i["item_id"]: i for i in _office()["items"]}
    assert items["desk-dev"]["persona_instance_id"] == "personainst_dev_agent_3ebfce41"


# ── the reconciliation loop ─────────────────────────────────────────────────


def test_the_revision_the_read_publishes_is_the_one_expect_revision_guards():
    """THE round trip, and the reason the read leg grew a per-item ``revision``.

    Read → take the item's revision → write guarded with it → ack a revision one
    higher → write guarded with THAT. Asserted end to end because each half
    alone passes against two lanes that disagree about whose revision they mean:
    the surface-level ``revision`` sits right beside ``items`` in the same result
    and is a DIFFERENT number that does not move when an actor moves.
    """

    _seed()
    result = _office()
    item = next(i for i in result["items"] if i["item_id"] == QA_INSTANCE)

    # The trap: the surface's revision is in scope and is not the actor's.
    assert item["revision"] == 1
    assert result["revision"] == 1  # equal here only by coincidence of seeding

    first = _upsert(
        "w1",
        {
            "workspace_id": WORKSPACE,
            "actor": _actor_payload(4.0, 4.0),
            "expect_revision": item["revision"],
        },
    )
    assert first["result"] == {"actor_key": QA_INSTANCE, "revision": 2}

    # The ack's number is immediately good as the next guard — the client never
    # has to refetch on the happy path, which is the entire point of the ack.
    second = _upsert(
        "w2",
        {
            "workspace_id": WORKSPACE,
            "actor": _actor_payload(5.0, 5.0),
            "expect_revision": first["result"]["revision"],
        },
    )
    assert second["result"] == {"actor_key": QA_INSTANCE, "revision": 3}
    assert _positions()[QA_INSTANCE] == [5.0, 5.0]

    # ...and the surface's revision never moved through any of it. A client that
    # had used THAT as its guard would have been reading a constant.
    assert _office("read2")["revision"] == 1


def test_the_surface_revision_does_not_move_when_an_actor_moves():
    """Stated as its own fact because it is the assumption a client will make.

    ``OfficeStore.upsert_actor`` rewrites the actor file and leaves
    ``office.json`` alone, so the surface-level ``revision`` — and
    ``updated_at`` — are unchanged after a drag. A poller that diffed them would
    conclude nothing moved.
    """

    _seed()
    before = _office("r1")
    _upsert("w", {"workspace_id": WORKSPACE, "actor": _actor_payload(7.0, 7.0)})
    after = _office("r2")

    assert after["revision"] == before["revision"]
    assert after["updated_at"] == before["updated_at"]
    # The ONLY thing that moved is the per-item revision this leg added.
    moved = next(i for i in after["items"] if i["item_id"] == QA_INSTANCE)
    assert moved["revision"] == 2
    assert moved["position"] == [7.0, 7.0]


def test_every_item_carries_ITS_OWN_actors_revision_and_not_some_other_actors():
    """The revision is the OWNING actor's, repeated onto its flattened items —
    the same rule ``persona_instance_id`` follows, and it fails the same two
    ways.

    Seeded with TWO actors at DIFFERENT revisions on purpose. A single-actor
    seed passes against a projection that hoisted one revision out of the loop
    and stamped it on every row (``projected[0].revision``), which is the shape
    a flattening bug actually takes — and the client would then send a
    stale/ahead guard for whichever actor it dragged. It also pins the per-KIND
    half: a desk whose revision disagreed with its own agent's would hand a
    client two different guards for one write.
    """

    _seed()
    _store().upsert_actor(
        WORKSPACE,
        {
            "persona_id": "dev",
            "persona_instance_id": "personainst_dev_agent_3ebfce41",
            "items": [{"item_id": "desk-dev", "kind": "desk", "position": [3.0, 4.0]}],
        },
    )
    # Move only the qa actor, so the two actors sit at different revisions and a
    # hoisted-constant projection has a wrong answer available to it.
    _upsert("w", {"workspace_id": WORKSPACE, "actor": _actor_payload(0.0, 0.0)})

    by_item = {i["item_id"]: i["revision"] for i in _office()["items"]}
    assert by_item == {
        # Instance-keyed, sorts first, untouched since its create.
        "desk-dev": 1,
        # Both of the qa actor's items — agent AND desk — at ITS revision.
        QA_INSTANCE: 2,
        "qa_desk": 2,
    }
    # Stated as its own fact: the two actors genuinely disagree, so the equality
    # above is not the equality of two identical numbers.
    assert by_item["desk-dev"] != by_item[QA_INSTANCE]


def test_expect_revision_cannot_guard_a_create_and_says_so_rather_than_writing():
    """A documented limit of the store's primitive, pinned so it cannot drift.

    ``_check_revision`` compares against ``None`` for an actor that does not
    exist, so EVERY value — including the ``0`` a client would naturally reach
    for to mean "I expect this not to exist yet" — refuses. A create is
    therefore necessarily unguarded. Pinned because the alternative failure is
    silent: a client sending ``expect_revision: 0`` on every write would have
    every create refused and would look like a conflict storm.
    """

    seed_workspace_record(WORKSPACE)
    _store().ensure_surface(WORKSPACE, created_by="seed")

    reply = _upsert(
        "create-guarded",
        {
            "workspace_id": WORKSPACE,
            "actor": {
                "persona_id": "brandnew",
                "items": [{"item_id": "d", "kind": "desk", "position": [0.0, 0.0]}],
            },
            "expect_revision": 0,
        },
    )

    assert reply["error"]["code"] == 4090
    assert reply["error"]["data"]["reason"] == "stale_revision"
    assert _store().list_actors(WORKSPACE) == []


def test_the_write_records_WHO_made_it_and_defaults_to_the_argv_lanes_operator():
    """Provenance, asserted on the STORE rather than the wire.

    ``updated_by`` is deliberately not in the projection — it is another
    surface's payload — so nothing on the ack can show that the method honoured
    it. That is exactly why it needs its own assertion: a handler that accepted
    the param and then dropped it would attribute every launcher drag to
    ``operator``, making a canvas edit indistinguishable from a CLI one in the
    one place an operator goes to tell them apart. The default is the argv
    lane's own, so an omitted param and the CLI agree.
    """

    _seed()
    _upsert(
        "named",
        {
            "workspace_id": WORKSPACE,
            "actor": _actor_payload(1.0, 1.0),
            "updated_by": "launcher-canvas",
        },
    )
    assert _store().get_actor(WORKSPACE, QA_INSTANCE).updated_by == "launcher-canvas"

    _upsert("anon", {"workspace_id": WORKSPACE, "actor": _actor_payload(2.0, 2.0)})
    assert _store().get_actor(WORKSPACE, QA_INSTANCE).updated_by == "operator"

    # And it still does not cross the wire — provenance is not canvas payload.
    assert "updated_by" not in json.dumps(_office())


# ── typed refusals, asserted whole, and each one writes nothing ─────────────


def test_a_stale_expect_revision_is_a_typed_conflict_and_the_store_is_untouched():
    """The conflict the whole leg exists for, as a whole frame.

    ``data.reason`` is ``stale_revision`` — distinct from not-found and from
    bad-params because the client's response is completely different: refetch
    and rebase, not degrade and not fix-the-payload. ``data`` deliberately does
    NOT carry the current revision: handing back a bare integer invites a retry
    with it, which is exactly the lost update the guard refuses. The number
    rides ``message``, for an operator's eyes.
    """

    _seed()
    _upsert("w1", {"workspace_id": WORKSPACE, "actor": _actor_payload(1.0, 1.0)})
    assert _positions()[QA_INSTANCE] == [1.0, 1.0]

    reply = _upsert(
        "stale",
        {
            "workspace_id": WORKSPACE,
            "actor": _actor_payload(99.0, 99.0),
            "expect_revision": 1,
        },
    )

    assert reply == {
        "jsonrpc": "2.0",
        "id": "stale",
        "error": {
            "code": 4090,
            "message": "stale_revision: expected 1, have 2",
            "data": {
                "reason": "stale_revision",
                "workspace_id": WORKSPACE,
                "expect_revision": 1,
            },
        },
    }
    # A typed refusal in front of a store that took the write anyway is worse
    # than no guard at all: the client rolls back and the server keeps it.
    assert _positions()[QA_INSTANCE] == [1.0, 1.0]


def test_an_unresolved_sync_conflict_is_a_different_reason_from_a_stale_revision():
    """Two refusals under one code, and conflating them would be the bug.

    ``stale_revision`` is cured by refetch-and-rebase. A realm-sync sidecar is
    not cured by anything the client can do — it needs an operator running
    ``harness office actor-resolve``. A client that retried this one would spin
    forever, which is why it gets its own ``data.reason``.
    """

    from agent_runtime import paths
    from utils import atomic_json_write

    _seed()
    atomic_json_write(
        paths.office_conflict_path(WORKSPACE, QA_INSTANCE),
        {"actor_key": QA_INSTANCE, "remote_actor": {}},
        indent=2,
        sort_keys=True,
    )

    reply = _upsert("sync", {"workspace_id": WORKSPACE, "actor": _actor_payload(9.0, 9.0)})

    assert reply == {
        "jsonrpc": "2.0",
        "id": "sync",
        "error": {
            "code": 4090,
            "message": f"actor_conflict:{QA_INSTANCE}",
            "data": {"reason": "sync_conflict", "workspace_id": WORKSPACE},
        },
    }
    assert reply["error"]["data"]["reason"] != "stale_revision"
    assert _positions()[QA_INSTANCE] == [-8.0, -2.0]


def test_a_re_add_over_an_unreadable_archive_refuses_typed_and_acks_no_revision_1():
    """EG-1.5 / RD-H4. The guard token cannot be re-minted from nothing.

    An archived key's revision is where the office's concurrency token LIVES
    between a remove and the re-add that follows it, and ``upsert_actor`` bases
    the new revision on it precisely so the number a peer holds stays meaningful.
    The store used to swallow a decode failure there — ``archived = None`` → base
    0 → **revision 1** — handing every client a token BELOW the one they already
    had. EG-5.1 arms exactly that comparison, which is why this is hard-before
    it: a guard is worth nothing if the server can silently rewind the number it
    is guarding.

    **Anti-vacuity.** Falling through to base 0 is the mutation. *Probed fields:*
    the whole error frame (a mutant produces a RESULT and cannot carry
    ``archive_unreadable`` at all) AND the absence of any actor file — the mutant
    writes one at revision 1, which is the very ack this test's name refuses.
    """

    from agent_runtime import paths

    store = _seed()
    for _ in range(6):
        store.upsert_actor(WORKSPACE, _actor_payload(1.0, 1.0), updated_by="seed-operator")
    assert store.get_actor(WORKSPACE, QA_INSTANCE).revision == 7
    store.remove_actor(WORKSPACE, QA_INSTANCE)
    archived_path = paths.office_archived_actor_path(WORKSPACE, QA_INSTANCE)
    archived_path.write_text("{truncated", encoding="utf-8")

    reply = _upsert("readd", {"workspace_id": WORKSPACE, "actor": _actor_payload(5.0, 5.0)})

    assert reply == {
        "jsonrpc": "2.0",
        "id": "readd",
        "error": {
            "code": -32600,
            "message": f"archive_unreadable:{QA_INSTANCE} (JSONDecodeError)",
            "data": {
                "reason": "archive_unreadable",
                "workspace_id": WORKSPACE,
            },
        },
    }
    # No ack, and no write behind the refusal: no revision-1 actor file, and the
    # archive copy left as found for an operator to repair.
    assert not paths.office_actor_path(WORKSPACE, QA_INSTANCE).exists()
    assert archived_path.read_text(encoding="utf-8") == "{truncated"


def test_an_unknown_workspace_is_refused_and_no_office_is_authored_for_the_typo():
    """The read leg refuses an unknown workspace so a typo cannot render as a
    blank canvas. The write leg must refuse the SAME way, and the reason it
    matters more here is durable: ``OfficeStore.upsert_actor`` calls
    ``ensure_surface`` and would lazily author a whole office for the typo,
    which no later poll repaints. The existence check runs BEFORE the store."""

    from agent_runtime import paths

    _seed()
    reply = _upsert("typo", {"workspace_id": "ws_nope", "actor": _actor_payload(1.0, 1.0)})

    assert reply == {
        "jsonrpc": "2.0",
        "id": "typo",
        "error": {
            "code": 4001,
            "message": "unknown workspace: ws_nope",
            "data": {"reason": "workspace_not_found", "workspace_id": "ws_nope"},
        },
    }
    # Byte-identical to the read leg's refusal for the same typo, so one client
    # branch covers both lanes.
    read_back = _reply(
        _run([_rpc("typo-read", "runtime.office.get", {"workspace_id": "ws_nope"}), SHUTDOWN]),
        "typo-read",
    )
    assert reply["error"] == read_back["error"]

    # Nothing was authored on the way out.
    assert not paths.office_dir("ws_nope").exists()
    assert _store().list_workspaces() == [WORKSPACE]


def test_bad_params_are_typed_and_the_store_is_never_reached():
    """Each param fault gets its own ``data.reason`` — the client's fix differs
    per field, and a single ``invalid_params`` would make a launcher bug a
    guessing game. ``expect_revision: true`` is called out because ``bool`` is
    an ``int`` in Python and would otherwise silently mean revision 1: a WRONG
    guard, which is worse than no guard."""

    _seed()
    actor = _actor_payload(1.0, 1.0)
    cases = {
        "no-ws": ({"actor": actor}, -32602, "workspace_id_required"),
        "blank-ws": ({"workspace_id": "  ", "actor": actor}, -32602, "workspace_id_required"),
        "no-actor": ({"workspace_id": WORKSPACE}, -32602, "actor_required"),
        "list-actor": ({"workspace_id": WORKSPACE, "actor": []}, -32602, "actor_required"),
        "bool-rev": (
            {"workspace_id": WORKSPACE, "actor": actor, "expect_revision": True},
            -32602,
            "expect_revision_invalid",
        ),
        "str-rev": (
            {"workspace_id": WORKSPACE, "actor": actor, "expect_revision": "1"},
            -32602,
            "expect_revision_invalid",
        ),
        "bad-by": (
            {"workspace_id": WORKSPACE, "actor": actor, "updated_by": 7},
            -32602,
            "updated_by_invalid",
        ),
    }

    out = _run(
        [_rpc(rid, "runtime.office.upsert", params) for rid, (params, _, _) in cases.items()]
        + [SHUTDOWN]
    )

    for rid, (_, code, reason) in cases.items():
        error = _reply(out, rid)["error"]
        assert error["code"] == code, rid
        assert error["data"]["reason"] == reason, rid

    # Not one of them moved the placement.
    assert _positions()[QA_INSTANCE] == [-8.0, -2.0]


def test_a_malformed_actor_payload_is_invalid_params_and_never_a_handler_error():
    """The store's own ``invalid_request: …`` refusals, translated rather than
    escaping as ``-32000``. One ``reason`` for all of them because the client's
    response is identical — fix the payload, it is a launcher bug — while the
    store's sentence rides ``message`` so the developer knows which field."""

    _seed()
    cases = {
        "no-persona": {"items": [{"item_id": "x", "position": [0.0, 0.0]}]},
        "no-items": {"persona_id": "qa"},
        "empty-items": {"persona_id": "qa", "items": []},
        "no-item-id": {"persona_id": "qa", "items": [{"position": [0.0, 0.0]}]},
        "bad-position": {"persona_id": "qa", "items": [{"item_id": "x", "position": "nope"}]},
        "infinite": {
            "persona_id": "qa",
            "items": [{"item_id": "x", "position": [float("1e999"), 0.0]}],
        },
        "secret-name": {
            "persona_id": "qa",
            "items": [
                {
                    "item_id": "x",
                    "position": [0.0, 0.0],
                    "display_name": "API_KEY=sk-live-0123456789abcdef",
                }
            ],
        },
    }

    out = _run(
        [
            _rpc(rid, "runtime.office.upsert", {"workspace_id": WORKSPACE, "actor": payload})
            for rid, payload in cases.items()
        ]
        + [SHUTDOWN]
    )

    for rid in cases:
        error = _reply(out, rid)["error"]
        assert error["code"] == -32602, (rid, error)
        assert error["data"] == {"reason": "actor_invalid", "workspace_id": WORKSPACE}, rid
        assert error["message"].startswith("invalid_request:"), (rid, error["message"])

    assert [a.actor_key for a in _store().list_actors(WORKSPACE)] == [QA_INSTANCE]
    assert _positions()[QA_INSTANCE] == [-8.0, -2.0]


def test_a_raising_store_becomes_a_typed_error_and_the_loop_keeps_serving():
    """A WRITE handler that escaped would take the reader loop — and the durable
    service — down mid-drag, with the actor's file in whatever state the raise
    left it. Same boundary the read leg has, asserted for the write."""

    import agent_runtime.office_store as office_store_module

    _seed()

    class _Exploding(office_store_module.OfficeStore):
        def upsert_actor(self, *args, **kwargs):
            raise RuntimeError("disk on fire")

    original = office_store_module.OfficeStore
    office_store_module.OfficeStore = _Exploding
    try:
        out = _run(
            [
                _rpc(
                    "boom",
                    "runtime.office.upsert",
                    {"workspace_id": WORKSPACE, "actor": _actor_payload(1.0, 1.0)},
                ),
                _argv("after", ["harness", "status"]),
                SHUTDOWN,
            ]
        )
    finally:
        office_store_module.OfficeStore = original

    error = _reply(out, "boom")["error"]
    assert error["code"] == -32000
    assert "disk on fire" in error["message"]
    assert error["data"] == {"reason": "handler_failed", "method": "runtime.office.upsert"}
    assert {"id": "after", "event": "exit", "code": 0} in _frames(out)


# ── the argv lane is untouched, with a WRITE beside it ──────────────────────


def test_the_argv_lane_emits_the_same_bytes_with_the_write_method_beside_it():
    """Additive or it is a regression — now with a mutation in the mix.

    The read leg's suite already proves this for reads. A write is the harder
    case: it holds the office lock, rewrites files, and (in the conflict branch)
    unwinds — any of which touching the shared stdout proxy would corrupt the
    argv lane's frames. Compared as LINES, not dicts: "byte-identical" is a
    claim about the wire, and a dict compare forgives a key-order change an
    incremental parser might not.
    """

    _seed()

    def dispatch(argv):
        print(f"row for {argv[-1]}")
        print("no trailing newline", end="")
        return 3

    baseline = _run([_argv("r1", ["harness", "office", "show"]), SHUTDOWN], dispatch=dispatch)
    mixed = _run(
        [
            _rpc(
                "m1",
                "runtime.office.upsert",
                {"workspace_id": WORKSPACE, "actor": _actor_payload(2.0, 2.0)},
            ),
            _argv("r1", ["harness", "office", "show"]),
            # A stale-revision refusal — the unwind path, beside the argv lane.
            _rpc(
                "m2",
                "runtime.office.upsert",
                {
                    "workspace_id": WORKSPACE,
                    "actor": _actor_payload(3.0, 3.0),
                    "expect_revision": 1,
                },
            ),
            # And a payload the store refuses, which raises inside the store.
            _rpc("m3", "runtime.office.upsert", {"workspace_id": WORKSPACE, "actor": {}}),
            SHUTDOWN,
        ],
        dispatch=dispatch,
    )

    assert _argv_lane_lines(baseline, "r1") == [
        json.dumps({"id": "r1", "event": "line", "line": "row for show"}),
        json.dumps({"id": "r1", "event": "line", "line": "no trailing newline"}),
        json.dumps({"id": "r1", "event": "exit", "code": 3}),
    ]
    assert _argv_lane_lines(mixed, "r1") == _argv_lane_lines(baseline, "r1")

    # The method calls really did run, so the equality above is not the equality
    # of two argv-only sessions.
    assert _reply(mixed, "m1")["result"] == {"actor_key": QA_INSTANCE, "revision": 2}
    assert _reply(mixed, "m2")["error"]["data"]["reason"] == "stale_revision"
    assert _reply(mixed, "m3")["error"]["data"]["reason"] == "actor_invalid"

    # A method call is answered ONCE. The id filter above would not notice a
    # frame that ALSO fell through into the argv lane and collected its
    # ``invalid_request`` on the way out — a second answer to one request, which
    # no client should have to deduplicate. (The read leg's suite found exactly
    # this class of bug; a write frame is claimed by the same discriminator, so
    # it is re-asserted here rather than assumed to carry over.)
    for rid in ("m1", "m2", "m3"):
        assert _argv_lane_lines(mixed, rid) == [], rid
    assert len([f for f in _frames(mixed) if f.get("id") == "m1"]) == 1


def test_the_argv_upsert_verb_still_writes_through_the_same_store():
    """The argv lane STAYS until the method is proven. Both verbs are pointed at
    one actor in sequence: the revision must advance across lanes rather than
    each lane keeping its own, which is what "one write chokepoint" means."""

    _seed()
    reply = _upsert("m", {"workspace_id": WORKSPACE, "actor": _actor_payload(1.0, 1.0)})
    assert reply["result"]["revision"] == 2

    # The argv verb's own handler, called directly — the parser tree is another
    # agent's file this session and does not need to be built to prove the seam.
    actor = _store().upsert_actor(WORKSPACE, _actor_payload(2.0, 2.0), updated_by="operator")
    assert actor.revision == 3

    after = _upsert("m2", {"workspace_id": WORKSPACE, "actor": _actor_payload(3.0, 3.0)})
    assert after["result"]["revision"] == 4


# ── the manifest ────────────────────────────────────────────────────────────


def test_the_method_set_grew_and_the_contract_integer_did_not_move():
    """Design question 1, answered where a reader will look for it.

    The manifest is a SET plus an integer. The set grew — a client only ever
    calls a method it FOUND in the set, so a runtime that publishes one more
    cannot break a client that does not know it. The integer moves only when a
    client that folds v1 must REFUSE v2, and nothing here changed the shape of
    an existing method: ``runtime.office.get`` gained a key, and the launcher's
    item decoder gates on required-key PRESENCE (``mission_office_rpc.dart:260``)
    and never on a count, so the shipped client folds the wider item unchanged.
    Bumping would have made that client refuse a payload it can read.
    """

    out = _run([json.dumps({"op": "version"}) + "\n", SHUTDOWN])
    frames = _frames(out)

    expected = {"contract": 1, "methods": [
            "runtime.agent.create",
            # S5's inverse. The literal here predated it and was never grown,
            # so this pin had been red since ``runtime.agent.retire`` landed —
            # closed in passing by the Stage A1 refresh that had to touch it.
            "runtime.agent.retire",
            # Gateway Stage 3, additive.
            "runtime.chat.message",
            "runtime.chat.steer",
            "runtime.office.get",
            "runtime.office.remove",
            "runtime.office.resolve_conflict",
            "runtime.office.subscribe",
            "runtime.office.surface.update",
            "runtime.office.unsubscribe",
            "runtime.office.upsert",
            "runtime.persona.prewarm",
    ],
        "tiers": {
            "runtime.agent.create": "console",
            "runtime.agent.retire": "console",
            # A chat turn runs an agent with tools; see _runtime_chat_message.
            "runtime.chat.message": "console",
            "runtime.chat.steer": "console",
            "runtime.office.get": "read",
            "runtime.office.remove": "console",
            "runtime.office.resolve_conflict": "console",
            "runtime.office.subscribe": "read",
            "runtime.office.surface.update": "console",
            "runtime.office.unsubscribe": "read",
            "runtime.office.upsert": "console",
            "runtime.persona.prewarm": "read",
        },
    }
    assert serve_rpc.manifest() == expected
    assert serve_rpc.RPC_CONTRACT_VERSION == 1
    assert next(f for f in frames if f.get("event") == "ready")["rpc"] == expected
    assert next(f for f in frames if f.get("event") == "version")["rpc"] == expected

    # The write method is advertised, so a client can discover it rather than
    # probe for it — and an unknown method still names what DOES exist.
    unknown = _reply(_run([_rpc("u", "runtime.nope", {}), SHUTDOWN]), "u")
    assert unknown["error"]["data"]["methods"] == expected["methods"]


def test_the_read_projection_grew_a_key_without_dropping_one():
    """Additive means ADDITIVE. Asserted as an exact key set so a "refactor"
    that swapped the surface-level revision for the per-item one — the very
    confusion this key exists to end — cannot pass."""

    _seed()
    result = _office()

    assert set(result) == {
        "workspace_id",
        "folders",
        "revision",
        "updated_at",
        "items",
        "actors_truncated",
        "actors_unreadable",
    }
    for item in result["items"]:
        assert set(item) == {
            "item_id",
            "kind",
            "persona_id",
            "persona_instance_id",
            "revision",
            "folder",
            "position",
            "scale",
            "display_name",
            "pet_slug",
        }


# ── the drain posture, which a WRITE makes a decision rather than a default ──


def test_a_write_during_a_drain_lands_because_it_cannot_be_cut_off_half_done():
    """A DECISION on the record, not an oversight — and the reason the comment
    at the dispatch site had to stop saying "a read".

    A drain refuses new argv work because that work can be cut off half-done: a
    chat turn whose frames stop mid-stream when the process exits. An inline
    method cannot be. ``OfficeStore`` has written the actor file atomically and
    released the office lock before the ack is emitted, and the replacement
    runtime reads that same file — so refusing would fail an operator's drag
    during a restart to protect against a loss that cannot occur. The argv lane
    beside it is still refused, which is what makes this a choice.
    """

    from tests.agent_runtime.test_serve_drain_accounting import WAIT, _Pipe, _Sink, _run_serve

    _seed()
    pipe, sink = _Pipe(), _Sink()
    result = _run_serve(pipe, sink, dispatch=lambda argv: 0)
    sink.wait_for("ready")

    pipe.send({"op": "drain", "deadline_seconds": 10})
    sink.wait_for("draining")

    pipe.send(
        {
            "jsonrpc": "2.0",
            "id": "drain-write",
            "method": "runtime.office.upsert",
            "params": {"workspace_id": WORKSPACE, "actor": _actor_payload(6.0, 6.0)},
        }
    )
    # The argv lane in the same drained session IS refused — the contrast is the
    # point, and without it this test would pass on a serve that never drained.
    pipe.send({"id": "drain-argv", "argv": ["harness", "status"]})
    pipe.send({"op": "shutdown"})
    result["thread"].join(WAIT)

    frames = sink.frames()
    ack = next(f for f in frames if f.get("id") == "drain-write" and "jsonrpc" in f)
    assert ack["result"] == {"actor_key": QA_INSTANCE, "revision": 2}
    refused = [f for f in frames if f.get("id") == "drain-argv"]
    assert [f["event"] for f in refused] == ["draining", "exit"]
    assert refused[1]["draining"] is True

    # Durable before the ack: the file the replacement runtime will read already
    # has the new placement.
    assert _positions()[QA_INSTANCE] == [6.0, 6.0]


# ── the socket lane ─────────────────────────────────────────────────────────


def test_the_write_method_is_transport_agnostic_and_answers_on_the_socket():
    """One dispatcher, N transports — proven for a MUTATION over a real
    loopback socket with the real handshake, because a write's reply going to
    the wrong peer would leak one client's ack onto another's stream."""

    from tests.agent_runtime.test_serve_rpc_office import _read_rpc
    from tests.agent_runtime.test_serve_socket_lane import client, running_serve

    _seed()

    with running_serve() as handle:
        with client(handle, name="rpc-writer") as (connection, hello_ok):
            assert hello_ok["rpc"] == {
                "contract": 1,
                "methods": [
                    "runtime.agent.create",
                    "runtime.agent.retire",
                    "runtime.chat.message",
                    "runtime.chat.steer",
                    "runtime.office.get",
                    "runtime.office.remove",
                    "runtime.office.resolve_conflict",
                    "runtime.office.subscribe",
                    "runtime.office.surface.update",
                    "runtime.office.unsubscribe",
                    "runtime.office.upsert",
                    "runtime.persona.prewarm",
                ],
                "tiers": {
                    "runtime.agent.create": "console",
                    "runtime.agent.retire": "console",
                    "runtime.chat.message": "console",
                    "runtime.chat.steer": "console",
                    "runtime.office.get": "read",
                    "runtime.office.remove": "console",
                    "runtime.office.resolve_conflict": "console",
                    "runtime.office.subscribe": "read",
                    "runtime.office.surface.update": "console",
                    "runtime.office.unsubscribe": "read",
                    "runtime.office.upsert": "console",
                    "runtime.persona.prewarm": "read",
                },
            }

            connection.send(
                {
                    "jsonrpc": "2.0",
                    "id": "sock-w1",
                    "method": "runtime.office.upsert",
                    "params": {
                        "workspace_id": WORKSPACE,
                        "actor": _actor_payload(11.0, 11.0),
                        "expect_revision": 1,
                    },
                }
            )
            assert _read_rpc(connection, "sock-w1")["result"] == {
                "actor_key": QA_INSTANCE,
                "revision": 2,
            }

            connection.send(
                {
                    "jsonrpc": "2.0",
                    "id": "sock-w2",
                    "method": "runtime.office.upsert",
                    "params": {
                        "workspace_id": WORKSPACE,
                        "actor": _actor_payload(12.0, 12.0),
                        "expect_revision": 1,
                    },
                }
            )
            conflict = _read_rpc(connection, "sock-w2")["error"]
            assert conflict["code"] == 4090
            assert conflict["data"]["reason"] == "stale_revision"

            # The ack went to the ASKER, not onto the stdio owner's stdout.
            assert '"sock-w1"' not in handle.sink.text()

    assert _positions()[QA_INSTANCE] == [11.0, 11.0]


# ── the wire, captured verbatim ─────────────────────────────────────────────


def test_the_wire_shape_is_stable_enough_to_hand_to_a_client_author():
    """One frame of each kind, asserted as raw JSON LINES.

    The launcher is written against these bytes. Every other test here asserts
    decoded dicts; this one pins what actually crosses, which is what a client
    author copies.
    """

    _seed()
    out = _run(
        [
            _rpc("ok", "runtime.office.upsert", {"workspace_id": WORKSPACE, "actor": _actor_payload(1.0, 1.0)}),
            _rpc(
                "conflict",
                "runtime.office.upsert",
                {"workspace_id": WORKSPACE, "actor": _actor_payload(1.0, 1.0), "expect_revision": 1},
            ),
            SHUTDOWN,
        ]
    )
    lines = {json.loads(line)["id"]: line for line in _lines(out) if "jsonrpc" in line}

    assert lines["ok"] == (
        '{"jsonrpc": "2.0", "id": "ok", "result": '
        '{"actor_key": "personainst_qa_agent_9c8a382f", "revision": 2}}'
    )
    assert lines["conflict"] == (
        '{"jsonrpc": "2.0", "id": "conflict", "error": {"code": 4090, '
        '"message": "stale_revision: expected 1, have 2", "data": '
        '{"reason": "stale_revision", "workspace_id": "ws_rpc_upsert_test", '
        '"expect_revision": 1}}}'
    )
