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
}
EXPECTED_ITEM_KEYS = {
    "item_id",
    "kind",
    "persona_id",
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

    # Flattened across actors, in ``list_actors`` order (actor_key ascending),
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
        "persona_instance_id",
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
    assert unknown["error"]["data"]["methods"] == ["runtime.office.get"]

    assert _reply(out, "old")["error"]["code"] == -32600
    assert _reply(out, "old")["error"]["data"]["reason"] == "bad_jsonrpc_version"
    assert _reply(out, "nameless")["error"]["code"] == -32600
    assert _reply(out, "listy")["error"]["code"] == -32602


def test_a_raising_handler_becomes_a_typed_error_and_never_reaches_the_loop(monkeypatch):
    """The method lane is answered INLINE on the transport's own thread. A
    handler that escaped would take the reader loop — and the durable service —
    down with it, which no read method is permitted to do."""

    def _boom(rid, params):
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

    expected = {"contract": 1, "methods": ["runtime.office.get"]}
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
            assert hello_ok["rpc"] == {"contract": 1, "methods": ["runtime.office.get"]}

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
