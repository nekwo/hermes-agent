"""Stage A2 — the identity a REAL handshake produces, end to end.

The unit half is next door (``test_serve_rpc_notification_lane.py``: the
default, the field separation, ``caller_for_connection``'s arms). What only this
file can prove is the JOIN — that the caller a handler receives on a live
connection is the one the transport actually proved, and not a value some
in-process constructor happened to default to.

That distinction is the whole reason the field exists, so it is measured over
the lane suite's own harness: a real loopback socket, a real HMAC
challenge-response, the real ``serve_loop``. A faked transport cannot falsify
"the authenticated connection becomes ``local_console``", because a fake is free
to say it is authenticated.

The probe method is registered onto the real registry and removed in a finally.
It exists because nothing on the shipped method surface ECHOES its caller —
which is correct (a caller identity is the server's business, not a payload) and
is exactly what makes the fact otherwise unobservable from outside.
"""

from __future__ import annotations

import json

from agent_runtime import serve_rpc
from agent_runtime.call_authorization import (
    CALLER_LOCAL_CONSOLE,
    CALLER_STDIO_OWNER,
    TIER_READ,
)
SHUTDOWN = json.dumps({"op": "shutdown"}) + "\n"
PROBE = "runtime.test.whoami"


def _register_probe():
    @serve_rpc.method(PROBE, tier=TIER_READ)
    def _whoami(rid, params, context=None):  # pragma: no cover - driven by tests
        context = context or serve_rpc.RpcContext()
        return serve_rpc.ok(rid, context.caller.describe())


def _unregister_probe():
    serve_rpc._METHODS.pop(PROBE, None)
    serve_rpc._METHOD_TIERS.pop(PROBE, None)


def test_an_authenticated_socket_peer_arrives_as_the_local_console():
    """The proven fact, over the real handshake.

    ``verify_hello_proof`` fails closed on a missing token, so a connection that
    reaches the dispatcher at all has presented THIS install's serve token. Until
    Stage A5 mints per-device credentials there is one token, so holding it IS
    being the machine owner — and the ``connection_key`` the caller carries is
    the same one ``hello_ok`` echoed back, which is what makes it usable as the
    join into a paired-device record later.
    """

    from tests.agent_runtime.test_serve_socket_lane import client, running_serve
    from tests.agent_runtime.test_serve_rpc_office import _read_rpc

    _register_probe()
    try:
        with running_serve() as handle:
            with client(handle, name="rpc-identity") as (connection, hello_ok):
                connection.send(
                    {"jsonrpc": "2.0", "id": "who-1", "method": PROBE, "params": {}}
                )
                answered = _read_rpc(connection, "who-1")["result"]

        assert answered["kind"] == CALLER_LOCAL_CONSOLE
        assert answered["transport"] == "socket"
        # The key both sides can say: the greeting echoed it, the caller repeats
        # it, and nothing had to be guessed to join the two.
        assert answered["connection_key"] == hello_ok["connection"]
    finally:
        _unregister_probe()


def test_the_stdio_owner_arrives_as_itself_and_carries_no_key():
    """There is no connection object on the owner's own pipe, and that absence
    is a fact rather than a missing value: whoever holds this process's stdin
    already holds the process."""

    from tests.agent_runtime.test_serve_rpc_office import _frames, _run

    _register_probe()
    try:
        out = _run(
            [
                json.dumps(
                    {"jsonrpc": "2.0", "id": "who-2", "method": PROBE, "params": {}}
                )
                + "\n",
                SHUTDOWN,
            ]
        )
        frames = _frames(out)
    finally:
        _unregister_probe()

    reply = next(f for f in frames if f.get("id") == "who-2" and "jsonrpc" in f)
    assert reply["result"] == {
        "kind": CALLER_STDIO_OWNER,
        "transport": "stdio",
        "connection_key": None,
    }


def test_the_caller_cannot_be_named_by_the_request():
    """The property the whole design rests on: a request that could name its own
    caller would be a request that authorizes itself.

    ``params`` is the only thing a client controls, and it never reaches the
    context. Asserted by sending the WHOLE caller shape as params and reading
    back the transport's answer instead.
    """

    from tests.agent_runtime.test_serve_rpc_office import _frames, _run

    _register_probe()
    try:
        out = _run(
            [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "who-3",
                        "method": PROBE,
                        "params": {
                            "caller": {"kind": "admin"},
                            "kind": "admin",
                            "tier": "console",
                            "connection_key": "forged",
                        },
                    }
                )
                + "\n",
                SHUTDOWN,
            ]
        )
        frames = _frames(out)
    finally:
        _unregister_probe()

    reply = next(f for f in frames if f.get("id") == "who-3" and "jsonrpc" in f)
    assert reply["result"]["kind"] == CALLER_STDIO_OWNER
    assert reply["result"]["connection_key"] is None


def test_the_probe_leaves_the_shipped_method_surface_exactly_as_it_found_it():
    """A registry this file mutates is a registry the next test inherits. The
    manifest is the assertion, because the manifest is what a client reads."""

    assert PROBE not in serve_rpc.manifest()["methods"]
    assert PROBE not in serve_rpc.manifest()["tiers"]
    assert PROBE not in serve_rpc._METHODS
