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


def test_the_manifest_did_not_grow_a_method_for_any_of_this():
    """The context is a DISPATCHER change, not a surface change. If this file
    ever has to be edited because the method set moved, the push leg leaked
    into the CALL contract and the version integer is now wrong."""

    assert serve_rpc.method_names() == ["runtime.office.get", "runtime.office.upsert"]
    assert serve_rpc.RPC_CONTRACT_VERSION == 1
