"""The PUSH half's smallest piece: a method that can talk back UNPROMPTED.

Increment 1 of the push leg, and deliberately NOT an office feature. What is
pinned here is the one structural change that ``runtime.office.subscribe``
cannot be written without — a handler learning WHO called it — plus the frame
shape it will push. The office method itself is a later commit and this file
must stay true when it lands.

The correction this file encodes, because it cost a day of wrong sequencing:
the emitter was never the missing piece. ``SocketConnection.emit`` (one
connection) and ``ServeSocketServer.broadcast`` (all of them) have shipped for
as long as the socket lane has, and ``serve.py``'s dispatcher has held the
per-connection ``sink`` in scope at the very line that answers a method call.
What was missing was the ARGUMENT: ``handle_request(req)`` -> ``fn(rid,
params)`` gave a handler no name for its caller, so a subscription had nothing
to register. That is the whole of the change under test.

Three things are asserted and each has a specific way of going wrong silently:

1. the notification SHAPE — ``id`` must be an absent KEY, not ``null``. A test
   that checks ``frame.get("id") is None`` passes against both and would let
   the wrong one ship;
2. the push REACHES the caller's own stream, through the REAL ``serve_loop``
   rather than a stub sink — the wiring in ``serve.py`` is the part that can
   silently not happen, and a unit test of ``RpcContext`` alone would go green
   with that line deleted;
3. a context with NO channel refuses instead of pretending. ``push`` returning
   True on a dead context is how a subscribe method would report success while
   the client heard nothing.
"""

from __future__ import annotations

import io
import json

from agent_runtime import serve_rpc
from hermes_cli.harness_parts.serve import serve_loop

SHUTDOWN = json.dumps({"op": "shutdown"}) + "\n"


# ── harness ─────────────────────────────────────────────────────────────────


def _run(requests, *, dispatch=lambda argv: 0, **kwargs) -> list[dict]:
    """Drive the REAL stdio serve loop and hand back every emitted frame."""

    out = io.StringIO()
    assert serve_loop(iter(requests), out, dispatch=dispatch, **kwargs) == 0
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


class _TemporaryMethod:
    """Register a method for one test and REMOVE it again.

    Not a convenience: ``method_names()`` feeds :func:`serve_rpc.manifest`,
    which rides the ``ready`` / ``hello_ok`` greetings and is asserted whole by
    the contract-version authority test. A test method left in the registry
    would fail that suite from three files away, and the failure would name the
    wrong lane.
    """

    def __init__(self, name: str, fn) -> None:
        self._name = name
        self._fn = fn

    def __enter__(self):
        assert self._name not in serve_rpc._METHODS, "name collides with a real method"
        serve_rpc._METHODS[self._name] = self._fn
        return self

    def __exit__(self, *_exc) -> None:
        serve_rpc._METHODS.pop(self._name, None)


def _call(method_name: str, params: dict | None = None, rid: str = "r1") -> str:
    return (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": method_name,
                "params": params or {},
            }
        )
        + "\n"
    )


# ── the frame shape ─────────────────────────────────────────────────────────


def test_a_notification_omits_the_id_key_rather_than_sending_it_null():
    """JSON-RPC 2.0 §4.1: a notification is a request WITHOUT an ``id`` member.

    Asserted as an exact key set, because every weaker form of this assertion
    passes against ``{"id": None}``. ``null`` is a legal request id, so a strict
    client correlating on key PRESENCE would file the push into its pending-call
    table and leak that entry for the life of the connection.
    """

    frame = serve_rpc.notification("runtime.office.patch", {"seq": 7})

    assert set(frame) == {"jsonrpc", "method", "params"}
    assert "id" not in frame
    assert frame == {
        "jsonrpc": "2.0",
        "method": "runtime.office.patch",
        "params": {"seq": 7},
    }


def test_a_notification_is_not_confused_with_a_reply_by_the_lane_router():
    """``is_rpc_frame`` must claim our own pushes too.

    The router is what a FUTURE bidirectional client hits, and the argv lane's
    ``invalid_request`` is the wrong answer for a well-formed notification. This
    also pins that the discrimination does not secretly depend on ``id``.
    """

    assert serve_rpc.is_rpc_frame(serve_rpc.notification("x.y", {}))


# ── the caller context ──────────────────────────────────────────────────────


def test_a_context_without_a_channel_refuses_the_push_instead_of_dropping_it():
    """The honest answer for stdio-with-no-sink, a test double, or a future
    non-duplex transport. Returning True here is how a subscribe method would
    report success into a void."""

    assert serve_rpc.RpcContext().push("runtime.office.patch", {}) is False


def test_the_context_reports_the_caller_and_defaults_to_stdio():
    """``connection_key`` is the SUBSCRIPTION identity — the only token the
    teardown path (``connection_sinks.pop`` on drop) can sweep a registry by.
    On stdio there is no key, and that is a real difference rather than a
    missing value: a stdio subscribe has exactly one implicit caller."""

    sent: list[dict] = []
    context = serve_rpc.RpcContext(
        connection_key="conn-7", transport="socket", emit=sent.append
    )

    assert context.push("runtime.office.patch", {"seq": 1}) is True
    assert sent == [
        {"jsonrpc": "2.0", "method": "runtime.office.patch", "params": {"seq": 1}}
    ]
    assert serve_rpc.RpcContext().transport == "stdio"
    assert serve_rpc.RpcContext().connection_key is None


def test_the_default_caller_is_the_stdio_owner_because_that_is_who_can_build_one():
    """Stage A2. The default is the HONEST value, not a convenient one.

    An ``RpcCaller`` can only be constructed by code already inside this
    process — nothing on either wire reaches the constructor — so a context
    assembled with no arguments describes an in-process caller, which is exactly
    what the ``transport = "stdio"`` default beside it has always said. The
    transport builder fills the field explicitly on every real dispatch, so this
    default is never what a remote peer gets.
    """

    from agent_runtime.call_authorization import CALLER_STDIO_OWNER

    assert serve_rpc.RpcContext().caller.kind == CALLER_STDIO_OWNER
    assert serve_rpc.RpcContext().caller.transport == "stdio"
    assert serve_rpc.RpcContext().caller.connection_key is None


def test_the_caller_does_not_borrow_the_subscription_key_field():
    """``connection_key`` stays the subscription identity. The caller REPEATS
    the key rather than the gate reading it off the field a teardown sweep and
    the office subscription registry both index on — two meanings on one field
    is how the next rename breaks a system that never mentioned it."""

    from agent_runtime.call_authorization import LOCAL_CONSOLE, RpcCaller

    context = serve_rpc.RpcContext(
        connection_key="conn-7",
        transport="socket",
        caller=RpcCaller(
            kind=LOCAL_CONSOLE.kind, connection_key="conn-7", transport="socket"
        ),
    )

    assert context.connection_key == context.caller.connection_key == "conn-7"
    # And the two are independently settable, which is what makes them two facts.
    assert (
        serve_rpc.RpcContext(connection_key="conn-9").caller.connection_key is None
    )


def test_a_caller_describes_itself_without_leaking_anything_secret():
    """What a refusal or a log line may carry: a kind, a lane, and a key the
    server already echoed back to that same peer on ``hello_ok``."""

    from agent_runtime.call_authorization import caller_for_connection

    class _Conn:
        key = "conn-3"
        transport = "socket"
        authenticated = True

    assert caller_for_connection(_Conn()).describe() == {
        "kind": "local_console",
        "transport": "socket",
        "connection_key": "conn-3",
    }


def test_a_connection_that_never_finished_the_handshake_is_not_the_console():
    """``caller_for_connection`` reads ``authenticated`` rather than assuming it.

    ``ServeSocketServer`` only enters a connection into ``_connections`` after
    ``verify_hello_proof``, so today this arm is unreachable from the dispatcher
    — which is the point of asserting it. A future transport that hands the
    dispatcher a pre-handshake connection gets ``unknown`` instead of silently
    inheriting a guarantee it never made.
    """

    from agent_runtime.call_authorization import (
        CALLER_LOCAL_CONSOLE,
        CALLER_STDIO_OWNER,
        CALLER_UNKNOWN,
        caller_for_connection,
    )

    class _Conn:
        key = "conn-4"
        transport = "socket"
        authenticated = False

    assert caller_for_connection(_Conn()).kind == CALLER_UNKNOWN
    # stdio has no connection object at all, and is the process owner.
    assert caller_for_connection(None).kind == CALLER_STDIO_OWNER
    # A connection missing the attribute entirely fails closed the same way.
    assert caller_for_connection(object()).kind == CALLER_UNKNOWN
    assert caller_for_connection(object()).kind != CALLER_LOCAL_CONSOLE


def test_a_handler_that_omits_the_context_still_answers():
    """Back-compat, and the reason ``handle_request`` defaults to an EMPTY
    context rather than to None: the direct-call sites (the rekey script's own
    end-to-end test, every probe) pass two arguments and must keep working."""

    frame = serve_rpc.handle_request(
        {"jsonrpc": "2.0", "id": "r1", "method": "runtime.office.get", "params": {}}
    )

    # The workspace is missing, so this is the typed -32602 — the point is that
    # it ANSWERED rather than raising a TypeError on arity.
    assert frame["error"]["code"] == serve_rpc.ERR_INVALID_PARAMS


# ── end to end through the real dispatcher ──────────────────────────────────


def test_a_pushed_notification_reaches_the_callers_own_stream_before_the_reply():
    """The wiring test, and the one that fails if ``serve.py``'s context is
    dropped.

    Ordering is asserted, not incidental: the reply is emitted by the DISPATCHER
    after the handler returns, so anything the handler pushed is already on the
    wire. That is the seam ``runtime.office.subscribe`` has to design around — a
    patch pushed between registration and the subscribe reply arrives BEFORE the
    baseline it rebases on, which is why the patch lane carries a sequence the
    client can discard against, and not why the handler needs a buffer.
    """

    def _pushy(rid, params, context):
        context.push("test.push", {"note": "before the reply"})
        return serve_rpc.ok(rid, {"pushed": True})

    with _TemporaryMethod("test.pushes", _pushy):
        frames = _run([_call("test.pushes"), SHUTDOWN])

    rpc = [f for f in frames if f.get("jsonrpc") == "2.0"]
    assert [f.get("method") for f in rpc] == ["test.push", None]
    assert "id" not in rpc[0]
    assert rpc[0]["params"] == {"note": "before the reply"}
    assert rpc[1] == {"jsonrpc": "2.0", "id": "r1", "result": {"pushed": True}}


def test_the_stdio_caller_is_told_it_is_stdio_and_has_no_connection_key():
    """Read back through the REAL loop rather than asserted on a constructed
    context: ``serve.py`` builds this from ``getattr(connection, ...)`` with
    ``connection`` None on stdio, and a typo there would still produce a
    plausible-looking context."""

    seen: list[dict] = []

    def _reflect(rid, params, context):
        seen.append(
            {"transport": context.transport, "key": context.connection_key}
        )
        return serve_rpc.ok(rid, {})

    with _TemporaryMethod("test.reflects", _reflect):
        _run([_call("test.reflects"), SHUTDOWN])

    assert seen == [{"transport": "stdio", "key": None}]


def test_a_push_that_fails_becomes_a_typed_error_on_the_call_that_tried_it():
    """Deliberately NOT swallowed. A push raised inside a handler is still
    inside ``handle_request``'s boundary, which is the one moment a dead channel
    is reportable at all — the notification itself has no reply to carry an
    error on. Fan-out to OTHER subscribers is a different path with a different
    answer (drop, account, close) and must not borrow this one."""

    def _broken(rid, params, context):
        context.push("test.push", {})
        return serve_rpc.ok(rid, {})

    def _explode(_frame):
        raise ConnectionError("the client went away")

    with _TemporaryMethod("test.explodes", _broken):
        frame = serve_rpc.handle_request(
            {"jsonrpc": "2.0", "id": "r1", "method": "test.explodes"},
            serve_rpc.RpcContext(connection_key="conn-9", emit=_explode),
        )

    assert frame["error"]["code"] == serve_rpc.ERR_HANDLER_FAILED
    assert frame["error"]["data"]["method"] == "test.explodes"


# ── the lane stays additive ─────────────────────────────────────────────────


def test_the_push_lane_itself_contributes_no_method_and_no_version_bump():
    """The context is a DISPATCHER change, not a surface change.

    Deliberately NOT an exact method-set assertion. Two files already own that
    (``test_serve_rpc_office`` and ``..._upsert``), and a third copy would mean
    every future method edits three files and learns nothing from the third.
    What is this file's to guard is narrower and does not move when a method is
    added: the transport machinery published no method of its own, no test
    fixture leaked into the registry, and the contract integer stayed put
    because nothing about an EXISTING method's shape changed.

    **The prefix list is two families, and it became two in gateway Stage 6
    rather than Stage 7 — this assertion was left red by that stage's manifest
    sweep and stayed red until Stage 7 ran the file.** ``peer.*`` is a declared
    family, not a leak: ``runtime.*`` verbs act on this install's level, ``peer.*``
    verbs are about the EDGE between two installs and touch no level
    (``serve_rpc``'s section comment). What this line is actually guarding is
    that the PUSH lane contributed no name at all, and a family it does not know
    about is exactly what it should still fail on — so the tuple is enumerated
    rather than replaced with something permissive.
    """

    assert all(
        name.startswith(("runtime.", "peer."))
        for name in serve_rpc.method_names()
    )
    assert not any(name.startswith("test.") for name in serve_rpc.method_names())
    assert serve_rpc.RPC_CONTRACT_VERSION == 1
