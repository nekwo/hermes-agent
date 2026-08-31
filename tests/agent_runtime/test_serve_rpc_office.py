"""The METHOD lane: ``runtime.office.get``, the first typed JSON-RPC method.

Increment 1 of the CALL half (launcher
``docs/mission_control/DECISION_push_and_rpc_2026-08-13.md`` §3). What is worth
pinning here is not that a dict comes back — it is the four things that make
this a template the next 37 methods can be copied from:

1. the WIRE SHAPE is upstream's JSON-RPC 2.0, verbatim, so the launcher can be
   written against ``tui_gateway``'s frames and not against a fork dialect;
2. a failure is a TYPED ERROR FRAME, asserted whole — a test that only checks
   "no result" passes against a runtime that returns the wrong code;
3. the ARGV LANE IS UNTOUCHED — asserted by running the same argv request with
   and without a method call beside it and comparing the emitted lines BYTE FOR
   BYTE, because "additive" is a claim, not a proof;
4. the RESULT IS BOUNDED — asserted by exact key sets and by seeding the store
   with the fields that must NOT cross (Stage 2b: pointers, never cosmetics).

Both transports run the REAL ``serve_loop``. The socket half reuses the lane
suite's own harness rather than a second copy of it.
"""

from __future__ import annotations

import io
import json

from agent_runtime import serve_rpc
from tests.agent_runtime.office_seed import seed_workspace_record
from hermes_cli.harness_parts.serve import serve_loop

SHUTDOWN = json.dumps({"op": "shutdown"}) + "\n"
WORKSPACE = "ws_rpc_office_test"


# ── seeding ─────────────────────────────────────────────────────────────────


def _seed_office(workspace_id: str = WORKSPACE):
    """Two actors, four items — the live canvas's shape in miniature.

    Every field the projection must DROP is deliberately populated: a
    ``backing_profile``, a ``persona_instance_id``, an ``updated_by`` other than
    the default, and (via the removal below) a non-empty ``archived_actor_keys``
    ledger. A boundedness assertion against a store where those happen to be
    empty proves nothing.
    """

    from agent_runtime.office_store import OfficeStore

    store = OfficeStore()
    seed_workspace_record(workspace_id)
    store.ensure_surface(workspace_id, created_by="seed")
    store.upsert_actor(
        workspace_id,
        {
            "persona_id": "neko_supervisor",
            "items": [
                {
                    "item_id": "personainst_neko_agent",
                    "kind": "agent",
                    "persona_id": "neko_supervisor",
                    "position": [0.5, 6.75],
                    "folder": "Agents",
                    "display_name": "Neko Mission Lead",
                    "pet_slug": "shushu",
                    "scale": 1.25,
                },
                {
                    "item_id": "desk-neko_supervisor",
                    "kind": "desk",
                    "persona_id": "neko_supervisor",
                    "position": [0.75, 8.5],
                    "folder": "Desks",
                },
            ],
        },
        updated_by="seed-operator",
    )
    store.upsert_actor(
        workspace_id,
        {
            "persona_id": "qa",
            "backing_profile": "qa_profile",
            "persona_instance_id": "personainst_qa_agent_9c8a382f",
            "items": [
                {
                    "item_id": "personainst_qa_agent_9c8a382f",
                    "kind": "agent",
                    "persona_id": "qa",
                    "position": [-8.0, -2.0],
                    "folder": "Agents",
                    "display_name": "QA Agent",
                },
                {
                    "item_id": "qa_desk",
                    "kind": "desk",
                    "persona_id": "qa",
                    "position": [-8.25, 0.5],
                    "folder": "Desks",
                },
            ],
        },
        updated_by="seed-operator",
    )
    # A removal fills the resurrection-guard ledger — the one genuinely
    # unbounded field on the surface (capped at 5000 keys) and therefore the
    # one whose absence from the wire has to be tested, not assumed.
    store.upsert_actor(
        workspace_id,
        {"persona_id": "ghost", "items": [{"item_id": "ghost", "position": [1.0, 1.0]}]},
    )
    store.remove_actor(workspace_id, "ghost", updated_by="seed-operator")
    return store


def _rpc(rid: str, method: str, params: dict | None = None) -> str:
    frame: dict = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        frame["params"] = params
    return json.dumps(frame) + "\n"


def _argv(rid: str, argv: list[str]) -> str:
    return json.dumps({"id": rid, "argv": argv}) + "\n"


def _lines(buffer: io.StringIO) -> list[str]:
    return [line for line in buffer.getvalue().splitlines() if line]


def _frames(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in _lines(buffer)]


def _run(requests, *, dispatch=lambda argv: 0, **kwargs) -> io.StringIO:
    out = io.StringIO()
    assert serve_loop(iter(requests), out, dispatch=dispatch, **kwargs) == 0
    return out


def _reply(buffer: io.StringIO, rid: str) -> dict:
    matches = [f for f in _frames(buffer) if f.get("id") == rid and "jsonrpc" in f]
    assert len(matches) == 1, f"expected exactly one JSON-RPC reply for {rid}: {matches}"
    return matches[0]


# ── the happy path, shaped exactly ──────────────────────────────────────────


EXPECTED_RESULT_KEYS = {
    "workspace_id",
    "folders",
    "revision",
    "updated_at",
    "items",
    "actors_truncated",
    # EG-1.5: the second way this projection can be short. ``actors_truncated``
    # counts a cut the runtime CHOSE; this one counts actor files that exist and
    # would not decode, which the store's skip-and-continue used to hide — and
    # which made the sibling above compute 0 from an already-shortened list.
    "actors_unreadable",
}
EXPECTED_ITEM_KEYS = {
    "item_id",
    "kind",
    "persona_id",
    # The 9th key, added WITHOUT moving the contract integer: the launcher's
    # item decoder gates on required-key PRESENCE (``containsKey``, launcher
    # ``mission_office_rpc.dart:484``) and never on the key count, so the client
    # already shipped folds a 9-key item unchanged. See
    # ``test_the_contract_integer_does_not_move_for_a_purely_additive_key``.
    "persona_instance_id",
    # The 10th key, added on the same terms and for the WRITE leg: the OWNING
    # ACTOR's revision, which is the token ``runtime.office.upsert``'s
    # ``expect_revision`` is checked against. A client had no other honest
    # source for it — the surface-level ``revision`` beside ``folders`` is a
    # DIFFERENT number and does not move when an actor moves. See
    # ``tests/agent_runtime/test_serve_rpc_office_upsert.py``.
    "revision",
    "folder",
    "position",
    "scale",
    "display_name",
    "pet_slug",
}


def test_the_office_method_answers_one_workspace_with_the_canvas_projection():
    """The template's happy path, asserted as a whole frame rather than field
    by field: the launcher has to write a decoder against these exact keys, and
    a per-field assertion would pass against a result carrying extras."""

    store = _seed_office()
    surface = store.get_surface(WORKSPACE)

    out = _run([_rpc("rpc-1", "runtime.office.get", {"workspace_id": WORKSPACE}), SHUTDOWN])
    reply = _reply(out, "rpc-1")

    assert reply["jsonrpc"] == "2.0"
    assert reply["id"] == "rpc-1"
    assert "error" not in reply
    result = reply["result"]
    assert set(result) == EXPECTED_RESULT_KEYS

    assert result["workspace_id"] == WORKSPACE
    assert result["folders"] == ["Agents", "Desks"]
    assert result["revision"] == surface.revision
    assert result["updated_at"].endswith("Z")
    assert result["actors_truncated"] == 0
    assert result["actors_unreadable"] == 0

    # Flattened across actors, in ``scan_actors`` order (actor_key ascending),
    # file order within an actor. Deterministic on purpose: a client that
    # caches the last result compares it against the next one.
    assert [(item["item_id"], item["kind"]) for item in result["items"]] == [
        ("personainst_neko_agent", "agent"),
        ("desk-neko_supervisor", "desk"),
        ("personainst_qa_agent_9c8a382f", "agent"),
        ("qa_desk", "desk"),
    ]
    assert result["items"][0] == {
        "item_id": "personainst_neko_agent",
        "kind": "agent",
        # The character-class POINTER (Stage 2b). Everything the launcher
        # renders for that class is a bundled app asset on its side.
        "persona_id": "neko_supervisor",
        # Class-keyed actor → explicit null. Note the item id BESIDE it is
        # instance-shaped (``personainst_neko_agent``): the two are independent,
        # which is exactly why the binding has to be sent rather than sniffed.
        "persona_instance_id": None,
        # The owning ACTOR's revision — first write, so 1. This is the number
        # ``runtime.office.upsert``'s ``expect_revision`` is checked against,
        # and NOT the surface-level ``revision`` asserted above.
        "revision": 1,
        "folder": "Agents",
        "position": [0.5, 6.75],
        "scale": 1.25,
        "display_name": "Neko Mission Lead",
        "pet_slug": "shushu",
    }
    # A desk carries the same key set with nulls, never a shorter object: a
    # client decoding into a typed struct must not have to special-case which
    # keys exist on which kind.
    assert set(result["items"][1]) == EXPECTED_ITEM_KEYS
    assert result["items"][1]["display_name"] is None
    assert result["items"][1]["pet_slug"] is None


def test_the_projection_carries_pointers_and_never_cosmetics_or_a_ledger():
    """Stage 2b, and the whole point of the exercise.

    Asserted two ways because either alone is weak: exact key sets catch a
    field that was added, and a raw-substring sweep catches one that leaked
    inside a nested structure a key-set check would walk past.
    """

    _seed_office()
    out = _run([_rpc("rpc-1", "runtime.office.get", {"workspace_id": WORKSPACE}), SHUTDOWN])
    result = _reply(out, "rpc-1")["result"]

    assert set(result) == EXPECTED_RESULT_KEYS
    for item in result["items"]:
        assert set(item) == EXPECTED_ITEM_KEYS

    blob = json.dumps(result)
    for leaked in (
        # The surface's append-only resurrection ledger — seeded non-empty
        # above, and the one field here with no upper bound worth trusting.
        "archived_actor_keys",
        "ghost",
        # Actor provenance: another surface's payload, never the canvas's.
        "updated_by",
        "seed-operator",
        "created_at",
        "backing_profile",
        "qa_profile",
        "actor_key",
        "schema_version",
        # Conflict/sync bookkeeping, and the archived placements themselves.
        "conflict",
        "orphaned",
        "unpublished",
        "state",
    ):
        assert leaked not in blob, f"{leaked!r} leaked into the office projection"

    # And it stays small. The live workspace projects under 2 KB for four
    # actors; a regression that re-attached actor rows would blow past this.
    assert len(blob) < 4096


# ── the persona-instance binding (the 9th key) ──────────────────────────────


def test_every_item_carries_the_instance_binding_and_it_is_the_owning_actors():
    """The binding is the ACTOR's, repeated onto each of its flattened items.

    Asserted per item and not once, because the wire shape is flat while the
    binding lives one level up: a projection that read the binding off the ITEM
    (``OfficeItem`` has no such field) or that attached it only to ``agent``
    items would produce a desk whose binding disagrees with its own agent's —
    two rows the launcher pairs by ``item_id`` + ``kind`` and would then render
    as one bound and one unbound occupant of the same desk.
    """

    _seed_office()
    out = _run([_rpc("rpc-1", "runtime.office.get", {"workspace_id": WORKSPACE}), SHUTDOWN])
    result = _reply(out, "rpc-1")["result"]

    # Present on EVERY item — the key, not merely a truthy value.
    for item in result["items"]:
        assert "persona_instance_id" in item, item

    assert [(i["item_id"], i["persona_instance_id"]) for i in result["items"]] == [
        # Class-keyed actor: explicit null on BOTH its items, agent and desk.
        ("personainst_neko_agent", None),
        ("desk-neko_supervisor", None),
        # Instance-keyed actor: the real id on BOTH its items, agent and desk.
        ("personainst_qa_agent_9c8a382f", "personainst_qa_agent_9c8a382f"),
        ("qa_desk", "personainst_qa_agent_9c8a382f"),
    ]


def test_a_class_keyed_actor_sends_an_explicit_null_never_an_omitted_key():
    """The standing rule desks already follow for ``display_name`` /
    ``pet_slug``, now load-bearing for identity: EVERY live actor is class-keyed
    today, so a projection that omitted the key on the null branch would look
    correct in every hand-check and hand the launcher a ``badItemShape``
    degrade — its decoder gates on ``containsKey``, not on truthiness."""

    _seed_office()
    out = _run([_rpc("rpc-1", "runtime.office.get", {"workspace_id": WORKSPACE}), SHUTDOWN])
    result = _reply(out, "rpc-1")["result"]

    class_keyed = [i for i in result["items"] if i["persona_id"] == "neko_supervisor"]
    assert class_keyed, "seed lost its class-keyed actor"
    for item in class_keyed:
        assert set(item) == EXPECTED_ITEM_KEYS, item
        assert item["persona_instance_id"] is None

    # Present as an explicit JSON null on the wire, not merely absent-and-falsy.
    for line in _lines(out):
        if '"persona_instance_id"' in line:
            assert '"persona_instance_id": null' in line
            break
    else:
        raise AssertionError("the binding never reached the wire at all")


def test_the_binding_is_read_from_the_actor_and_never_sniffed_from_the_item_id():
    """The defect this field exists to prevent, pinned from both sides.

    The seed is built so that item-id sniffing gives the WRONG answer twice:
    ``personainst_neko_agent`` is instance-SHAPED but belongs to a class-keyed
    actor (must be null), and ``qa_desk`` is not instance-shaped at all but
    belongs to an instance-keyed actor (must carry the id). A projection that
    derived the binding from ``item_id`` passes neither.
    """

    _seed_office()
    out = _run([_rpc("rpc-1", "runtime.office.get", {"workspace_id": WORKSPACE}), SHUTDOWN])
    items = {i["item_id"]: i for i in _reply(out, "rpc-1")["result"]["items"]}

    assert items["personainst_neko_agent"]["item_id"].startswith("personainst_")
    assert items["personainst_neko_agent"]["persona_instance_id"] is None

    assert not items["qa_desk"]["item_id"].startswith("personainst_")
    assert items["qa_desk"]["persona_instance_id"] == "personainst_qa_agent_9c8a382f"


def test_the_binding_on_the_wire_is_the_canonical_id_and_equals_the_actor_key():
    """What crosses is the CANONICAL binding — ``OfficeStore`` canonicalizes at
    the write chokepoint (``office_store.py`` header, plan §4.3), so the wire
    value is byte-equal to the actor's own sync key. Seeded through the
    ``persona_personainst_*`` actor-token drift alias precisely so a raw echo of
    the caller's token would differ from the canonical one."""

    from agent_runtime.office_store import OfficeStore

    store = _seed_office()
    store.upsert_actor(
        WORKSPACE,
        {
            "persona_id": "dev",
            "persona_instance_id": "persona_personainst_dev_agent_3ebfce41",
            "items": [{"item_id": "desk-dev", "kind": "desk", "position": [3.0, 4.0]}],
        },
    )
    actor = OfficeStore().get_actor(WORKSPACE, "personainst_dev_agent_3ebfce41")

    out = _run([_rpc("rpc-1", "runtime.office.get", {"workspace_id": WORKSPACE}), SHUTDOWN])
    dev_items = [i for i in _reply(out, "rpc-1")["result"]["items"] if i["persona_id"] == "dev"]

    assert [i["item_id"] for i in dev_items] == ["desk-dev"]
    assert dev_items[0]["persona_instance_id"] == "personainst_dev_agent_3ebfce41"
    assert dev_items[0]["persona_instance_id"] == actor.actor_key


def test_the_contract_integer_does_not_move_for_a_purely_additive_key():
    """Why a 9th key did NOT bump the contract — a reader will expect it to.

    The integer moves when a client that folds v1 must REFUSE v2. The launcher's
    item decoder checks required-key presence (``containsKey``,
    ``mission_office_rpc.dart:484``) and never a key count, so the client
    already shipped folds the 9-key item and simply ignores the new key. Bumping
    would have made that shipped client refuse a payload it can read.

    Asserted together with the key's presence on purpose: an unchanged integer
    is only interesting while the field is actually there.
    """

    _seed_office()
    out = _run(
        [
            _rpc("rpc-1", "runtime.office.get", {"workspace_id": WORKSPACE}),
            json.dumps({"op": "version"}) + "\n",
            SHUTDOWN,
        ]
    )

    for item in _reply(out, "rpc-1")["result"]["items"]:
        assert "persona_instance_id" in item

    assert serve_rpc.RPC_CONTRACT_VERSION == 1
    assert serve_rpc.manifest()["contract"] == 1
    frames = _frames(out)
    assert next(f for f in frames if f.get("event") == "ready")["rpc"]["contract"] == 1
    assert next(f for f in frames if f.get("event") == "version")["rpc"]["contract"] == 1


def test_the_snapshot_office_lane_carries_the_same_binding_field():
    """Cross-LANE parity, the divergence class this program keeps paying for.

    The launcher reads the office twice — this method and the office section of
    the ~842 KB snapshot. The snapshot row has carried ``persona_instance_id``
    since it was written (``snapshot.py``'s ``_office_actor_summary_row``); this
    pins that the two lanes agree on the field NAME and on the value for the
    same actor, so neither lane can quietly drop it alone.
    """

    from agent_runtime.snapshot import office_summary_row

    store = _seed_office()
    row = office_summary_row(
        store.get_surface(WORKSPACE), store.scan_actors(WORKSPACE).actors, actors_unreadable=0
    )
    snapshot_bindings = {a["actor_key"]: a["persona_instance_id"] for a in row["actors"]}
    assert snapshot_bindings == {
        "neko_supervisor": None,
        "personainst_qa_agent_9c8a382f": "personainst_qa_agent_9c8a382f",
    }

    out = _run([_rpc("rpc-1", "runtime.office.get", {"workspace_id": WORKSPACE}), SHUTDOWN])
    result = _reply(out, "rpc-1")["result"]

    # Every binding the RPC lane emits is one the snapshot lane also emits, for
    # the actor that owns the item — same field name, same value, both lanes.
    rpc_bindings = {i["persona_instance_id"] for i in result["items"]}
    assert rpc_bindings == set(snapshot_bindings.values())


def test_the_snapshot_office_item_row_is_pinned_byte_for_byte_and_its_gap_to_the_wire_row_named():
    """The SNAPSHOT lane's item shape, pinned as bytes, and the exact distance
    from it to ``office_models.office_item_wire_row`` — the shared projection the
    RPC lane already flattens through.

    Written for §AX1 ("one office-actor wire projection"), which proposes that
    ``snapshot._office_actor_summary_row`` stop re-spelling the item dict inline
    and call the shared function instead. The unification is BEHAVIOUR-PRESERVING
    only if the two shapes already agree, and they do not: this test states the
    disagreement in the two forms a refactor can break it in, so the change can
    never land unannounced.

    * **The bytes.** ``json.dumps`` of the projected items, not a key set: a key
      set survives a reorder and a ``12`` → ``12.0`` coercion, and both of those
      are wire changes a golden or a strict decoder can see. The shared function
      coerces (``float(item.position[0])``, ``float(item.scale)``); this lane
      copies (``list(item.position)``, ``item.scale``) and therefore re-emits
      whatever the store file held — ``from_jsonable`` does not widen an int, and
      an actor ADOPTED from a peer (``adopt_remote_surface``) never passes through
      ``_normalize_item``'s float boundary.
    * **The keys.** The shared row carries two ACTOR-level fields repeated onto
      every item (``persona_instance_id``, ``revision``) that this lane states
      once, on the actor. That is the whole delta, and it is asserted as an exact
      pair so a third field appearing on either side reds here.

    ``minted_kind`` (H-H12) is on neither side and must stay on neither: it is
    deliberately off the wire and out of ``office_content_hash``, so a projection
    that started carrying it would make a field no observer can see suddenly
    observable. The assertion below is what holds that.
    """

    from agent_runtime.office_models import office_item_wire_row
    from agent_runtime.snapshot import _office_actor_summary_row

    store = _seed_office()
    actors = {a.actor_key: a for a in store.scan_actors(WORKSPACE).actors}
    actor = actors["personainst_qa_agent_9c8a382f"]

    row = _office_actor_summary_row(actor, unpublished=None)

    assert json.dumps(row["items"]) == (
        '[{"item_id": "personainst_qa_agent_9c8a382f", "persona_id": "qa", "kind": "agent", '
        '"position": [-8.0, -2.0], "folder": "Agents", "display_name": "QA Agent", '
        '"pet_slug": null, "scale": 1.0}, '
        '{"item_id": "qa_desk", "persona_id": "qa", "kind": "desk", '
        '"position": [-8.25, 0.5], "folder": "Desks", "display_name": null, '
        '"pet_slug": null, "scale": 1.0}]'
    )

    for item, projected in zip(actor.items, row["items"]):
        shared = office_item_wire_row(actor, item)
        assert sorted(set(shared) - set(projected)) == ["persona_instance_id", "revision"]
        assert sorted(set(projected) - set(shared)) == []
        assert "minted_kind" not in shared and "minted_kind" not in projected


# ── typed failures, asserted as whole frames ────────────────────────────────


def test_an_unknown_workspace_is_a_typed_error_and_not_an_empty_office():
    """``office show`` answers an unauthored office with an honest empty
    because its reader is a human. A PROGRAM cannot tell an empty office from a
    mistyped workspace id, and would paint a blank canvas for a typo — so this
    lane refuses, with upstream's own not-found code."""

    _seed_office()
    out = _run([_rpc("rpc-2", "runtime.office.get", {"workspace_id": "ws_nope"}), SHUTDOWN])

    assert _reply(out, "rpc-2") == {
        "jsonrpc": "2.0",
        "id": "rpc-2",
        "error": {
            "code": 4001,
            "message": "unknown workspace: ws_nope",
            "data": {"reason": "workspace_not_found", "workspace_id": "ws_nope"},
        },
    }


def test_a_missing_or_unusable_workspace_id_is_invalid_params_not_a_crash():
    _seed_office()
    out = _run(
        [
            _rpc("no-params", "runtime.office.get"),
            _rpc("blank", "runtime.office.get", {"workspace_id": "   "}),
            _rpc("wrong-type", "runtime.office.get", {"workspace_id": 7}),
            SHUTDOWN,
        ]
    )

    for rid in ("no-params", "blank", "wrong-type"):
        assert _reply(out, rid) == {
            "jsonrpc": "2.0",
            "id": rid,
            "error": {
                "code": -32602,
                "message": "invalid params: workspace_id must be a non-empty string",
                "data": {"reason": "workspace_id_required"},
            },
        }, rid


def test_the_envelope_itself_is_validated_with_upstreams_codes():
    """-32600 / -32601 / -32602 are ``tui_gateway/server.py``'s, reused rather
    than renumbered, so one decoder can read both dispatchers."""

    out = _run(
        [
            _rpc("unknown", "runtime.nope.get", {}),
            json.dumps({"jsonrpc": "1.0", "id": "old", "method": "runtime.office.get"}) + "\n",
            json.dumps({"jsonrpc": "2.0", "id": "nameless", "method": ""}) + "\n",
            json.dumps(
                {"jsonrpc": "2.0", "id": "listy", "method": "runtime.office.get", "params": []}
            )
            + "\n",
            SHUTDOWN,
        ]
    )

    unknown = _reply(out, "unknown")
    assert unknown["error"]["code"] == -32601
    assert unknown["error"]["message"] == "unknown method: runtime.nope.get"
    # The refusal names what DOES exist — the manifest a client may have missed.
    assert unknown["error"]["data"]["methods"] == [
        # Gateway Stage 6, and the first name outside the ``runtime.*``
        # family: ``peer.*`` verbs are about the EDGE between two
        # installs and touch no level. Additive, so the integer holds.
        "peer.agent_chat.execute",
        "peer.ping",
        "runtime.agent.create",
        "runtime.agent.retire",
        # Gateway Stage 3, additive: the set grows, the integer does not.
        "runtime.chat.message",
        "runtime.chat.steer",
        # Gateway Stage 8, additive: the fetch family joins the set.
        "runtime.media.get",
        "runtime.media.index",
        "runtime.office.get",
        "runtime.office.remove",
        "runtime.office.resolve_conflict",
        "runtime.office.subscribe",
        "runtime.office.surface.update",
        "runtime.office.unsubscribe",
        "runtime.office.upsert",
        "runtime.persona.prewarm",
    ]

    assert _reply(out, "old")["error"]["code"] == -32600
    assert _reply(out, "old")["error"]["data"]["reason"] == "bad_jsonrpc_version"
    assert _reply(out, "nameless")["error"]["code"] == -32600
    assert _reply(out, "listy")["error"]["code"] == -32602


def test_a_raising_handler_becomes_a_typed_error_and_never_reaches_the_loop(monkeypatch):
    """The method lane is answered INLINE on the transport's own thread. A
    handler that escaped would take the reader loop — and the durable service —
    down with it, which no read method is permitted to do."""

    def _boom(rid, params, context):
        raise RuntimeError("store on fire")

    monkeypatch.setitem(serve_rpc._METHODS, "runtime.office.get", _boom)

    out = _run(
        [
            _rpc("boom", "runtime.office.get", {"workspace_id": WORKSPACE}),
            _argv("after", ["harness", "status"]),
            SHUTDOWN,
        ]
    )

    error = _reply(out, "boom")["error"]
    assert error["code"] == -32000
    assert "store on fire" in error["message"]
    assert error["data"] == {"reason": "handler_failed", "method": "runtime.office.get"}
    # Still serving: the argv request behind it landed.
    assert {"id": "after", "event": "exit", "code": 0} in _frames(out)


# ── the argv lane is untouched ──────────────────────────────────────────────


def _argv_lane_lines(buffer: io.StringIO, rid: str) -> list[str]:
    """The raw JSON LINES the argv lane produced for one request id.

    Lines, not parsed dicts: "byte-identical" is a claim about what goes on the
    wire, and a dict comparison would forgive a key-order or formatting change
    a consumer's incremental parser might not.
    """

    return [
        line
        for line in _lines(buffer)
        if json.loads(line).get("id") == rid and "jsonrpc" not in json.loads(line)
    ]


def test_the_argv_lane_emits_the_same_bytes_with_the_method_lane_beside_it():
    """The critical one. The method lane is additive or it is a regression.

    Two sessions, same argv request, one with three method calls interleaved
    around it — including one that ERRORS, since an error path that corrupted
    the shared stdout proxy is exactly how this would break. The argv lane's
    lines must be identical, character for character, in both.
    """

    _seed_office()

    def dispatch(argv):
        print(f"row for {argv[-1]}")
        print("no trailing newline", end="")
        return 3

    baseline = _run([_argv("r1", ["harness", "office", "show"]), SHUTDOWN], dispatch=dispatch)
    mixed = _run(
        [
            _rpc("m1", "runtime.office.get", {"workspace_id": WORKSPACE}),
            _argv("r1", ["harness", "office", "show"]),
            _rpc("m2", "runtime.office.get", {"workspace_id": "ws_nope"}),
            _rpc("m3", "runtime.nope", {}),
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

    # And the method calls really did run beside it, so the equality above is
    # not the equality of two argv-only sessions.
    assert "result" in _reply(mixed, "m1")
    assert _reply(mixed, "m2")["error"]["code"] == 4001
    assert _reply(mixed, "m3")["error"]["code"] == -32601

    # A method call is answered ONCE, by the method lane. The id filter above
    # would not notice a frame that also fell through into the argv lane and
    # collected its ``invalid_request`` on the way out — a second answer to one
    # request, which no client should have to deduplicate.
    for rid in ("m1", "m2", "m3"):
        assert _argv_lane_lines(mixed, rid) == [], rid


def test_a_frame_with_neither_jsonrpc_nor_method_still_gets_the_argv_error():
    """The discrimination, from the other side: the method lane claims a frame
    ONLY when it names ``jsonrpc`` or ``method``. Everything else — including
    every malformed request the argv lane has always typed — is left alone."""

    out = _run(
        [
            "not json at all\n",
            json.dumps({"id": "", "argv": ["harness"]}) + "\n",
            json.dumps({"id": "x", "argv": []}) + "\n",
            json.dumps(["not", "an", "object"]) + "\n",
            SHUTDOWN,
        ],
        dispatch=lambda argv: (_ for _ in ()).throw(AssertionError("must not dispatch")),
    )

    errors = [f for f in _frames(out) if f.get("event") == "error"]
    assert len(errors) == 4
    assert {f["error"] for f in errors} == {"invalid_request"}
    assert not [f for f in _frames(out) if "jsonrpc" in f]


# ── the capability manifest ─────────────────────────────────────────────────


def test_stdio_learns_the_method_set_from_ready_and_can_re_ask_version():
    """How a client learns the surface, following ``hello_contract``'s
    precedent: the server advertises on the greeting the client already reads,
    and restates it on the re-askable stamp — because a durable service
    outlives the install it was started from."""

    out = _run([json.dumps({"op": "version"}) + "\n", SHUTDOWN])
    frames = _frames(out)

    expected = {
        "contract": 1,
        "methods": [
            "peer.agent_chat.execute",
            "peer.ping",
            "runtime.agent.create",
            # S5's inverse. It joined the SET; the integer beside it did not
            # move, which is the whole discipline this frame advertises.
            "runtime.agent.retire",
            # Gateway Stage 3, additive: the set grows, the integer does not.
            "runtime.chat.message",
            "runtime.chat.steer",
            # Gateway Stage 8, additive: the fetch family joins the set.
            "runtime.media.get",
            "runtime.media.index",
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
            "peer.agent_chat.execute": "console",
            "peer.ping": "read",
            "runtime.agent.create": "console",
            "runtime.agent.retire": "console",
            "runtime.chat.message": "console",
            "runtime.chat.steer": "console",
            "runtime.media.get": "console",
            "runtime.media.index": "console",
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
    ready = next(f for f in frames if f.get("event") == "ready")
    assert ready["rpc"] == expected
    version = next(f for f in frames if f.get("event") == "version")
    assert version["rpc"] == expected
    assert serve_rpc.manifest() == expected
    assert serve_rpc.RPC_CONTRACT_VERSION == 1


# ── the socket lane ─────────────────────────────────────────────────────────


def _read_rpc(connection, rid: str, *, limit: int = 200) -> dict:
    for _ in range(limit):
        frame = connection.read_frame()
        if frame is None:
            raise AssertionError(f"connection closed before a reply to {rid!r}")
        if frame.get("id") == rid and "jsonrpc" in frame:
            return frame
    raise AssertionError(f"no JSON-RPC reply for {rid!r} within {limit} frames")


def test_the_method_surface_is_transport_agnostic_and_answers_on_the_socket():
    """One dispatcher, N transports — so a method written once is on every
    lane. Proven over a REAL loopback socket with the REAL handshake, because
    "the dispatcher is shared" is exactly the kind of claim a faked transport
    cannot falsify."""

    from tests.agent_runtime.test_serve_socket_lane import client, running_serve

    _seed_office()

    with running_serve() as handle:
        with client(handle, name="rpc-peer") as (connection, hello_ok):
            # The socket's greeting carries the manifest — a socket client
            # never sees ``ready``, and should not need a round trip for it.
            assert hello_ok["rpc"] == {
                "contract": 1,
                "methods": [
                    "peer.agent_chat.execute",
                    "peer.ping",
                    "runtime.agent.create",
                    "runtime.agent.retire",
                    # Gateway Stage 3, additive: the set grows, the integer does not.
                    "runtime.chat.message",
                    "runtime.chat.steer",
                    # Gateway Stage 8, additive: the fetch family joins the set.
                    "runtime.media.get",
                    "runtime.media.index",
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
                    "peer.agent_chat.execute": "console",
                    "peer.ping": "read",
                    "runtime.agent.create": "console",
                    "runtime.agent.retire": "console",
                    "runtime.chat.message": "console",
                    "runtime.chat.steer": "console",
                    "runtime.media.get": "console",
                    "runtime.media.index": "console",
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
                    "id": "sock-1",
                    "method": "runtime.office.get",
                    "params": {"workspace_id": WORKSPACE},
                }
            )
            result = _read_rpc(connection, "sock-1")["result"]
            assert set(result) == EXPECTED_RESULT_KEYS
            assert [item["item_id"] for item in result["items"]] == [
                "personainst_neko_agent",
                "desk-neko_supervisor",
                "personainst_qa_agent_9c8a382f",
                "qa_desk",
            ]

            connection.send(
                {
                    "jsonrpc": "2.0",
                    "id": "sock-2",
                    "method": "runtime.office.get",
                    "params": {"workspace_id": "ws_nope"},
                }
            )
            assert _read_rpc(connection, "sock-2")["error"]["code"] == 4001

            # The reply went to the ASKER and not onto the stdio owner's
            # stdout — the socket lane's standing rule, which a new op is the
            # easiest thing to get wrong.
            assert '"sock-1"' not in handle.sink.text()
