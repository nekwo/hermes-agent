"""Slice 3: serve becomes a durable, multi-client, reconnectable service.

These run the REAL ``serve_loop`` with the REAL socket lane over REAL loopback
sockets, against the isolated runtime root the autouse fixture provides. No
transport is faked, because the things worth pinning here are exactly the ones a
fake transport cannot fail: that an unauthenticated peer gets NOTHING, that two
clients cannot collide, and that a drain reaches every attached client before
the lane closes under them.

The stdio lane is asserted alongside on purpose: this slice refactored the op
dispatcher to be transport-agnostic, and "stdio is unchanged" is a claim that
has to be tested, not asserted in a commit message.
"""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
from contextlib import contextmanager

import pytest

from agent_runtime import serve_socket
from agent_runtime.serve_auth import read_token
from agent_runtime.serve_socket import (
    HELLO_CONTRACT_VERSION,
    MAX_LINE_BYTES,
    NONCE_BYTES,
    REJECT_BAD_PROOF,
    REJECT_DRAINING,
    REJECT_HELLO_MALFORMED,
    REJECT_HELLO_REQUIRED,
    REJECT_HANDSHAKE_THROTTLED,
    REJECT_HELLO_TIMEOUT,
    REJECT_HELLO_TOO_LONG,
    REJECT_RATE_LIMITED,
    REJECT_TOO_MANY_CONNECTIONS,
    REJECT_TOO_MANY_PENDING,
    HelloRateLimiter,
    ServeHelloProtocolError,
    ServeSocketClient,
    ServeSocketServer,
    SocketOwnerLock,
    hello_proof,
    read_socket_owner,
    resolve_socket_target,
    socket_lock_path,
    socket_owner_path,
    verify_hello_proof,
)
from hermes_cli.harness_parts import serve as serve_module
from hermes_cli.harness_parts.serve import serve_loop

WAIT = 15.0


class _Clock:
    """A monotonic clock the test moves by hand.

    The rate limiter's window is real seconds. Waiting them out would make the
    recovery assertion below either slow or flaky, and — worse — a wall-clock
    test cannot distinguish "recovered because the window lapsed" from
    "recovered because something else reset it". Driving the clock makes the
    lapse the only variable.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


@contextmanager
def bare_server(**kwargs):
    """A real ``ServeSocketServer`` on real loopback, with its knobs exposed.

    ``running_serve`` below is the whole runtime and is the right seam for
    behaviour that involves the dispatcher, the drain, or the registry. It is
    the WRONG seam for the admission policy: capacity, the pre-auth bound and
    the two rate limiters are constructor arguments the serve loop fixes at
    production values, and a test that cannot set them can only assert the
    policy exists, never that it admits and refuses the right peers. This binds
    the same class the loop binds, over the same loopback, with those knobs in
    the test's hands.
    """

    logs: list[dict] = []
    token = kwargs.pop("token", "the-shared-secret")
    server = ServeSocketServer(
        _store_root(),
        boot_id=kwargs.pop("boot_id", "test-boot"),
        dispatch_line=kwargs.pop(
            "dispatch_line",
            lambda line, connection: connection.emit({"event": "echo", "line": line}),
        ),
        hello_payload=kwargs.pop(
            "hello_payload",
            lambda message, connection: {
                "event": "hello_ok",
                "connection": connection.key,
            },
        ),
        token_provider=kwargs.pop("token_provider", lambda: token),
        log=logs.append,
        **kwargs,
    )
    port = server.bind()
    server.start_accepting()
    try:
        yield server, port, logs
    finally:
        server.close()


def raw_handshake(port: int, *, token: str | None, client: str = "peer",
                  proof: object = None, hello: dict | None = None) -> tuple[dict | None, dict | None]:
    """One handshake driven by hand. Returns ``(server_hello, reply)``.

    Deliberately NOT ``ServeSocketClient``: half the cases below are peers that
    answer the challenge WRONGLY, and the reference client is written to answer
    it correctly.

    A peer can be refused BEFORE the challenge — capacity, the pre-auth bound,
    the two rate limiters and a draining lane are all decided without minting a
    nonce — in which case the FIRST frame is the rejection and there is nothing
    to answer. That is returned as ``(None, rejection)``, because sending a
    hello into a closed connection and then reading end-of-stream would report
    ``None`` for every one of those refusals and hide which it was.
    """

    connection = ServeSocketClient("127.0.0.1", port, timeout_seconds=WAIT)
    connection.connect()
    try:
        greeting = connection.read_frame()
        if not isinstance(greeting, dict) or greeting.get("event") != "server_hello":
            return None, greeting
        if hello is not None:
            connection.send(hello)
        else:
            answer: dict = {"op": "hello", "client": client}
            if proof is not None:
                answer["proof"] = proof
            elif token is not None:
                answer["proof"] = hello_proof(token, greeting.get("nonce") or "", port=port)
            connection.send(answer)
        return greeting, connection.read_frame()
    finally:
        connection.close()


class _WireTap:
    """A socket that keeps a copy of every byte written through it.

    Wrapped around the client's socket AFTER ``connect``, so the reference
    client is still the thing under test and the reader still holds the real
    socket. ``socket.socket`` refuses attribute assignment, which is why this
    is a proxy rather than a patched method.
    """

    def __init__(self, sock, sent: list[bytes]) -> None:
        self._sock = sock
        self._sent = sent

    def sendall(self, data) -> None:
        self._sent.append(bytes(data))
        self._sock.sendall(data)

    def __getattr__(self, name):
        return getattr(self._sock, name)


class _StdioPipe:
    """The inherited-pipe side: an iterable the test feeds lines into."""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()

    def __iter__(self) -> "_StdioPipe":
        return self

    def __next__(self) -> str:
        item = self._queue.get()
        if item is None:
            raise StopIteration
        return item

    def send(self, message: dict) -> None:
        self._queue.put(json.dumps(message) + "\n")

    def close(self) -> None:
        self._queue.put(None)


class _Sink:
    """serve's stdout, readable from the test thread while serve writes."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            self._parts.append(text)
        return len(text)

    def flush(self) -> None:
        return None

    def text(self) -> str:
        with self._lock:
            return "".join(self._parts)

    def frames(self) -> list[dict]:
        rows = []
        for line in self.text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:  # a partially flushed tail
                continue
        return rows

    def wait_for(self, event: str, timeout: float = WAIT) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for frame in self.frames():
                if frame.get("event") == event:
                    return frame
            time.sleep(0.01)
        raise AssertionError(f"no {event!r} frame within {timeout}s")


class _RunningServe:
    def __init__(self, pipe: _StdioPipe, sink: _Sink, thread: threading.Thread) -> None:
        self.pipe = pipe
        self.sink = sink
        self.thread = thread
        self.ready: dict = {}
        self.code: int | None = None

    @property
    def port(self) -> int:
        return int(self.ready["socket"]["port"])


@contextmanager
def running_serve(**kwargs):
    """A live serve with the socket lane on, torn down at the end of the block."""

    pipe = _StdioPipe()
    sink = kwargs.pop("sink", None) or _Sink()
    handle = _RunningServe(pipe, sink, None)  # type: ignore[arg-type]

    def _run() -> None:
        handle.code = serve_loop(
            pipe,
            sink,
            socket_lane=True,
            dispatch=kwargs.pop("dispatch", lambda argv: 0),
            liveness_pump_interval_seconds=kwargs.pop(
                "liveness_pump_interval_seconds", 60.0
            ),
            **kwargs,
        )

    thread = threading.Thread(target=_run, name="serve-under-test", daemon=True)
    handle.thread = thread
    thread.start()
    try:
        handle.ready = sink.wait_for("ready")
        yield handle
    finally:
        pipe.close()
        thread.join(WAIT)


@contextmanager
def client(handle: _RunningServe, *, token: str | None = None, name: str = "test-client",
           build: str | None = None):
    root = _store_root()
    resolved = token if token is not None else (read_token(root) or "")
    connection = ServeSocketClient("127.0.0.1", handle.port, timeout_seconds=WAIT)
    connection.connect()
    try:
        reply = connection.hello(token=resolved, client=name, client_build=build)
        yield connection, reply
    finally:
        connection.close()


def _store_root():
    from agent_runtime import paths

    return paths.store_root()


def _read_until(connection: ServeSocketClient, event: str, *, limit: int = 200) -> dict:
    for _ in range(limit):
        frame = connection.read_frame()
        if frame is None:
            raise AssertionError(f"connection closed before {event!r}")
        if frame.get("event") == event:
            return frame
    raise AssertionError(f"no {event!r} within {limit} frames")


# ── the lane comes up, and says so ──────────────────────────────────────────


def test_the_ready_frame_publishes_the_port_and_the_registry_records_it():
    from agent_runtime.serve_registry import list_serve_instances

    with running_serve() as handle:
        assert handle.ready["socket"]["outcome"] == "listening"
        assert handle.ready["socket"]["host"] == "127.0.0.1"
        port = handle.ready["socket"]["port"]
        assert isinstance(port, int) and port > 0

        rows = list_serve_instances(_store_root())
        assert len(rows) == 1
        assert rows[0]["transport"] == "stdio+socket"
        assert rows[0]["port"] == port
        assert rows[0]["socket_started_at"]

        # Discovery works from the sidecar too, and neither file leaks the token.
        owner = read_socket_owner(_store_root())
        assert owner["port"] == port
        assert "token" not in json.dumps(owner)
        assert "token" not in json.dumps(rows[0])


def test_a_stdio_only_serve_says_the_socket_is_disabled_and_records_no_port():
    from agent_runtime.serve_registry import list_serve_instances

    pipe, sink = _StdioPipe(), _Sink()
    pipe.send({"op": "shutdown"})
    code = serve_loop(pipe, sink, dispatch=lambda argv: 0)

    ready = next(f for f in sink.frames() if f.get("event") == "ready")
    assert code == 0
    assert ready["socket"] == {"outcome": "disabled"}
    assert not socket_lock_path(_store_root()).exists()
    assert list_serve_instances(_store_root()) == []


def test_the_second_serve_for_a_root_degrades_to_stdio_and_names_the_owner():
    """One socket owner per root, decided by a lock and REPORTED, never silent."""

    root = _store_root()
    incumbent = SocketOwnerLock(root)
    assert incumbent.acquire().acquired is True
    incumbent.publish_owner({"pid": 4242, "port": 61000, "boot_id": "incumbent"})
    try:
        with running_serve() as handle:
            assert handle.ready["socket"]["outcome"] == "lock_held_by"
            assert handle.ready["socket"]["pid"] == 4242

            from agent_runtime.serve_registry import list_serve_instances

            row = list_serve_instances(root)[0]
            assert row["transport"] == "stdio"
            assert row["port"] is None
    finally:
        incumbent.release()


# ── auth is first, and it is everything ─────────────────────────────────────


def test_a_good_token_gets_the_build_handshake():
    with running_serve() as handle:
        with client(handle, name="probe", build="deadbeefdeadbeef") as (_conn, reply):
            assert reply["event"] == "hello_ok"
            assert reply["contract"] == serve_module.SERVE_SCHEMA_VERSION
            assert reply["boot_id"] == handle.ready["boot_id"]
            assert set(reply["build"]) == {"commit", "dirty", "source", "resolved_at"}
            assert reply["transport"] == "socket"
            assert reply["draining"] is False
            # The client named a build; either it disagrees with this runtime's
            # commit (True) or the runtime could not measure its own (None).
            assert reply["build_mismatch"] in (True, None)


def test_the_socket_greeting_names_which_install_the_client_reached():
    """A socket client never reads ``ready``, and from Stage 1 of the remote
    gateway a client on another machine reads nothing else — so the handshake it
    already performs has to answer "which install is this", identically to the
    stdio greeting rather than in a second spelling."""

    with running_serve() as handle:
        with client(handle, name="probe") as (_conn, reply):
            assert set(reply["install"]) == {"install_id", "display_name", "state"}
            assert reply["install"] == handle.ready["install"]
            assert reply["install"]["install_id"]
            # Still additive: the handshake integers did not move.
            assert reply["contract"] == serve_module.SERVE_SCHEMA_VERSION


def test_a_client_on_the_same_build_is_not_flagged_and_one_on_another_build_is():
    from agent_runtime.build_stamp import build_stamp

    commit = build_stamp().commit
    if not commit:
        pytest.skip("this checkout could not measure its own commit")
    with running_serve() as handle:
        with client(handle, name="same", build=commit) as (_c, same):
            assert same["build_mismatch"] is False
        with client(handle, name="short", build=commit[:10]) as (_c, short):
            # A short hash is the same build, not a different one.
            assert short["build_mismatch"] is False
        with client(handle, name="other", build="0" * 40) as (_c, other):
            assert other["build_mismatch"] is True
            # Visible, never fatal: it is still a working connection.
            assert other["event"] == "hello_ok"


def test_the_server_speaks_first_and_the_token_never_travels():
    """F1, half two: the handshake is a challenge, not a credential hand-off.

    The transcript below is the whole point. The SERVER opens with a nonce; the
    client answers with an HMAC over it; the token appears in neither
    direction. A captured transcript is therefore unreplayable — the next
    connection demands a proof over a different nonce — and an impostor that
    binds a dead serve's port learns one HMAC over a challenge it chose, which
    authenticates it nowhere.
    """

    sent: list[bytes] = []
    with running_serve() as handle:
        token = read_token(_store_root()) or ""
        assert token

        connection = ServeSocketClient("127.0.0.1", handle.port, timeout_seconds=WAIT)
        connection.connect()
        connection._sock = _WireTap(connection._sock, sent)
        try:
            greeting = connection.read_frame()
            # The server spoke first, and what it said is only a challenge.
            assert greeting["event"] == "server_hello"
            assert greeting["hello_contract"] == HELLO_CONTRACT_VERSION
            assert greeting["algorithm"] == "hmac-sha256"
            assert len(greeting["nonce"]) == 2 * NONCE_BYTES
            int(greeting["nonce"], 16)  # hex, so it is the value it claims to be
            # An unauthenticated peer learns the boot id and nothing else: no
            # build, no runtime root, no ports, no answer to any op.
            assert set(greeting) == {
                "event",
                "nonce",
                "boot_id",
                "contract",
                "hello_contract",
                "algorithm",
            }

            connection.send(
                {
                    "op": "hello",
                    "client": "transcript",
                    "client_build": None,
                    "proof": hello_proof(token, greeting["nonce"], port=handle.port),
                }
            )
            reply = connection.read_frame()
            assert reply["event"] == "hello_ok"
        finally:
            connection.close()

    # Every byte this client put on the wire, checked against the secret.
    wire = b"".join(sent)
    assert token.encode() not in wire
    answer = json.loads(wire.decode().strip())
    assert answer["proof"] == hello_proof(token, greeting["nonce"], port=handle.port)
    assert "token" not in answer
    # ...and the nonce is per CONNECTION, so the proof above cannot be reused.
    with running_serve() as second:
        first_nonce = greeting["nonce"]
        peer = ServeSocketClient("127.0.0.1", second.port, timeout_seconds=WAIT)
        peer.connect()
        try:
            fresh = peer.read_frame()
            assert fresh["nonce"] != first_nonce
        finally:
            peer.close()


def test_a_wrong_proof_gets_one_typed_rejection_and_no_data_at_all():
    """The anti-vacuity test for auth.

    A ``hello_rejected`` frame appearing proves only that SOMETHING was
    refused. What matters is that the peer learned nothing and could do
    nothing: after the challenge it is sent EXACTLY ONE more frame, that frame
    is the rejection, no op it pipelined is answered, and the stream ends.
    """

    with running_serve() as handle:
        connection = ServeSocketClient("127.0.0.1", handle.port, timeout_seconds=WAIT)
        connection.connect()
        try:
            greeting = connection.read_frame()
            assert greeting["event"] == "server_hello"
            connection.send(
                {
                    "op": "hello",
                    "client": "thief",
                    "proof": hello_proof("not-the-token", greeting["nonce"], port=handle.port),
                }
            )
            # Sent regardless of the rejection: an unauthenticated peer must not
            # be able to run ops by simply continuing to talk.
            connection.send({"op": "version"})
            connection.send({"id": "req-1", "argv": ["harness", "status", "--json"]})
            received = []
            while True:
                frame = connection.read_frame()
                if frame is None:
                    break
                received.append(frame)
            # ONE frame after the challenge, and then end of stream. Not "a
            # rejection appeared somewhere in the transcript".
            assert received == [
                {"event": "hello_rejected", "reason": REJECT_BAD_PROOF}
            ]
            blob = json.dumps(received)
            for leaked in ("build", "runtime_root", "commit", "version", "port"):
                assert leaked not in blob
        finally:
            connection.close()

        # The refusal did not take the lane down: a real client still works.
        with client(handle, name="legit") as (_conn, reply):
            assert reply["event"] == "hello_ok"


def test_a_missing_or_malformed_hello_is_refused_the_same_way():
    """Every way of not answering the challenge, and the reason for each.

    The reasons are not decoration: three of these charge the auth rate limiter
    and two must not, and the limiter derives that from the reason alone.
    """

    # A generous limiter, deliberately: five of these cases ARE auth
    # failures, so against the production limit of five the last of them
    # would be answered ``rate_limited`` and this test would be asserting
    # the limiter rather than the taxonomy. The limiter has its own tests.
    token = "the-shared-secret"
    with bare_server(
        rate_limiter=HelloRateLimiter(limit=100, window_seconds=600.0),
        reject_penalty_seconds=0.0,
    ) as (_server, port, _logs):
        cases = [
            ({"op": "version"}, REJECT_HELLO_REQUIRED),
            ({"op": "hello", "client": "no-proof"}, REJECT_BAD_PROOF),
            ({"op": "hello", "proof": 12345, "client": "wrong-type"}, REJECT_BAD_PROOF),
            ({"op": "hello", "proof": "", "client": "empty"}, REJECT_BAD_PROOF),
            # The token itself, offered the OLD way. There is no compatibility
            # shim by design: accepting it would keep the cleartext lane open.
            ({"op": "hello", "token": token, "client": "old-contract"}, REJECT_BAD_PROOF),
        ]
        for message, reason in cases:
            greeting, reply = raw_handshake(port, token=None, hello=message)
            # Every one of these got as far as the challenge, so the
            # rejection below is a verdict on the ANSWER, not a refusal
            # that happened before the peer said anything.
            assert greeting is not None and greeting["event"] == "server_hello", message
            assert reply == {"event": "hello_rejected", "reason": reason}, message

        # And a line that is not a JSON object at all.
        connection = ServeSocketClient("127.0.0.1", port, timeout_seconds=WAIT)
        connection.connect()
        try:
            assert connection.read_frame()["event"] == "server_hello"
            connection._sock.sendall(b"this is not json\n")
            assert connection.read_frame() == {
                "event": "hello_rejected",
                "reason": REJECT_HELLO_MALFORMED,
            }
        finally:
            connection.close()


def test_a_peer_that_says_nothing_is_timed_out_and_is_not_an_auth_failure():
    """F3's other half: silence presented no credential to be wrong about."""

    clock = _Clock()
    limiter = HelloRateLimiter(limit=2, window_seconds=10.0, clock=clock)
    with bare_server(
        rate_limiter=limiter, hello_deadline_seconds=0.2, reject_penalty_seconds=0.0
    ) as (server, port, _logs):
        for _ in range(3):
            silent = socket.create_connection(("127.0.0.1", port), timeout=WAIT)
            try:
                reader = silent.makefile("rb")
                assert json.loads(reader.readline())["event"] == "server_hello"
                frame = json.loads(reader.readline())
                assert frame == {
                    "event": "hello_rejected",
                    "reason": REJECT_HELLO_TIMEOUT,
                }
            finally:
                silent.close()

        # Three timeouts against a limit of two: had they been charged to the
        # AUTH limiter this next handshake would be refused. It is not.
        assert limiter.blocked() is False
        _greeting, reply = raw_handshake(port, token="the-shared-secret")
        assert reply["event"] == "hello_ok"
        assert server.connections_payload()["hello_timeouts"] == 3


def test_the_rate_limit_bites_and_then_lets_a_valid_client_back_IN():
    """F3, the defect and its recovery in one transcript.

    The old code charged EVERY rejection to the auth limiter, including the
    ``rate_limited`` rejection itself — so each polite retry pushed the window
    forward and a client holding the RIGHT credential could never get back in
    (proven live: 12 retries over 12s, never recovering). Both halves are
    asserted here: the window closes on real auth failures, and it OPENS again
    once they age out, with nothing the blocked client did extending it.
    """

    clock = _Clock()
    limiter = HelloRateLimiter(limit=3, window_seconds=10.0, clock=clock)
    with bare_server(rate_limiter=limiter, reject_penalty_seconds=0.0) as (
        server,
        port,
        _logs,
    ):
        for _ in range(3):
            _g, reply = raw_handshake(port, token="wrong-secret")
            assert reply == {"event": "hello_rejected", "reason": REJECT_BAD_PROOF}

        # The window is closed for everyone — the honest cost of a shared door.
        # The client does what a well-behaved client does: it waits and retries,
        # holding the RIGHT credential. THREE retries, not one, because that is
        # what makes the lockout self-sustaining: each refusal charged to the
        # limiter refills the window it was refused by, so a single retry
        # inside a limit of three would recover even against the defect and
        # prove nothing. (The live repro was twelve retries over twelve
        # seconds, never recovering.)
        clock.advance(5.0)
        for _ in range(3):
            _g, blocked = raw_handshake(port, token="the-shared-secret")
            assert blocked == {"event": "hello_rejected", "reason": REJECT_RATE_LIMITED}

        # THE RECOVERY. Now 11s past the last genuine auth failure — so those
        # have aged out — but only 6s past the refusals above. If a refusal
        # caused by the SERVER's own state had been charged to the limiter, the
        # window is still full and this client is still locked out, forever, by
        # its own politeness.
        clock.advance(6.0)
        _g, recovered = raw_handshake(port, token="the-shared-secret")
        assert recovered["event"] == "hello_ok", (
            "a valid client never recovered — the blocked rejections extended "
            "their own block"
        )
        counts = server.connections_payload()["rejected_by_reason"]
        assert counts[REJECT_BAD_PROOF] == 3
        assert counts[REJECT_RATE_LIMITED] == 3


def test_a_capacity_refusal_is_not_charged_to_the_auth_limiter():
    """F3's first half, at the sharpest setting the knobs allow.

    One auth failure is enough to close this window, and the capacity refusal
    below is the only rejection that happens. If it were charged — as every
    non-auth rejection used to be — the valid client afterwards would be
    ``rate_limited``. The server's OWN state can never block a client.
    """

    clock = _Clock()
    limiter = HelloRateLimiter(limit=1, window_seconds=600.0, clock=clock)
    with bare_server(
        max_connections=1, rate_limiter=limiter, reject_penalty_seconds=0.0
    ) as (server, port, _logs):
        incumbent = ServeSocketClient("127.0.0.1", port, timeout_seconds=WAIT)
        incumbent.connect()
        try:
            assert incumbent.hello(
                token="the-shared-secret", client="incumbent"
            )["event"] == "hello_ok"

            _g, refused = raw_handshake(port, token="the-shared-secret")
            assert refused == {
                "event": "hello_rejected",
                "reason": REJECT_TOO_MANY_CONNECTIONS,
            }
            assert limiter.blocked() is False
        finally:
            incumbent.close()

        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline and server.connections_payload()["count"]:
            time.sleep(0.02)
        _g, admitted = raw_handshake(port, token="the-shared-secret")
        assert admitted["event"] == "hello_ok"


def test_peers_that_prove_nothing_are_bounded_separately_from_authenticated_ones():
    """F4: 64 silent sockets once sat on 64 threads and drew not one rejection,
    because ``at_capacity`` counted AUTHENTICATED connections only."""

    with bare_server(
        max_connections=32,
        max_pending_connections=2,
        hello_deadline_seconds=WAIT,
        reject_penalty_seconds=0.0,
    ) as (server, port, _logs):
        silent = []
        try:
            for _ in range(2):
                sock = socket.create_connection(("127.0.0.1", port), timeout=WAIT)
                silent.append(sock)
                assert json.loads(sock.makefile("rb").readline())["event"] == "server_hello"

            deadline = time.monotonic() + WAIT
            while (
                time.monotonic() < deadline
                and server.connections_payload()["pending"] < 2
            ):
                time.sleep(0.02)
            payload = server.connections_payload()
            # The lane is nowhere near its AUTHENTICATED bound...
            assert payload["count"] == 0
            assert payload["max_connections"] == 32
            # ...and is nonetheless full, on the bound that watches this phase.
            assert payload["pending"] == 2
            assert payload["max_pending_connections"] == 2

            _g, refused = raw_handshake(port, token="the-shared-secret")
            assert refused == {
                "event": "hello_rejected",
                "reason": REJECT_TOO_MANY_PENDING,
            }
        finally:
            for sock in silent:
                sock.close()

        # And it is a bound, not a wall: the slots come back.
        deadline = time.monotonic() + WAIT
        while (
            time.monotonic() < deadline and server.connections_payload()["pending"]
        ):
            time.sleep(0.02)
        _g, admitted = raw_handshake(port, token="the-shared-secret")
        assert admitted["event"] == "hello_ok"
        assert server.connections_payload()["pending_peak"] >= 2


def test_the_accept_loop_reports_how_it_ended_instead_of_dying_quietly():
    """F4's other half. A loop that stopped without a word leaves a service
    that answers its discovery record and then never answers anything."""

    with bare_server() as (server, port, logs):
        payload = server.connections_payload()
        # Both facts are stated while healthy, so an operator reading them
        # after a failure is reading the same two fields, not a new one.
        assert payload["accept_errors"] == 0
        assert payload["accept_loop_exited"] is None
        server.close()
        deadline = time.monotonic() + WAIT
        while (
            time.monotonic() < deadline
            and server.connections_payload()["accept_loop_exited"] is None
        ):
            time.sleep(0.02)
        assert server.connections_payload()["accept_loop_exited"] == "listener_closed"
        assert [row for row in logs if row["event"] == "serve_socket_accept_loop_exit"]


def test_the_token_never_appears_in_any_frame_the_service_writes():
    token = None
    with running_serve() as handle:
        token = read_token(_store_root())
        assert token
        with client(handle, name="probe") as (connection, reply):
            connection.send({"op": "version"})
            version = _read_until(connection, "version")
            connection.send({"op": "connections"})
            connections = _read_until(connection, "socket_connections")
            assert token not in json.dumps([reply, version, connections])
    assert token not in handle.sink.text()


# ── the impostor: the live-proven attack, from the attacker's chair ─────────
#
# The CRITICAL finding was not "the handshake is weak in theory". Discovery
# handed a client a row it had ITSELF classified ``stale_dead_pid``, the client
# checked only that the row existed, and the raw token went out in cleartext to
# whatever had taken the dead serve's port. Two independent halves close it, so
# the tests come in two halves: what the CLIENT will put on a wire (never the
# token), and what discovery will hand it in the first place (live rows only).


class _ImpostorServer:
    """A plain listener that is not this service, and records what it is told.

    Not a mock of anything: a local process that binds a recycled port and
    speaks whatever it likes is exactly the situation, so this speaks whatever
    the test tells it to and keeps every byte it receives.
    """

    def __init__(self, greeting: dict | None) -> None:
        self.greeting = greeting
        self.received = bytearray()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self.port = int(self._listener.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._done = threading.Event()
        self._thread.start()

    def _serve(self) -> None:
        try:
            sock, _peer = self._listener.accept()
        except OSError:
            self._done.set()
            return
        try:
            if self.greeting is not None:
                sock.sendall((json.dumps(self.greeting) + "\n").encode())
            sock.settimeout(2.0)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    chunk = sock.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                self.received += chunk
        finally:
            self._done.set()
            try:
                sock.close()
            except OSError:
                pass

    def wait(self, timeout: float = WAIT) -> None:
        self._done.wait(timeout)

    def close(self) -> None:
        try:
            self._listener.close()
        except OSError:
            pass


def test_an_impostor_on_a_recycled_port_harvests_a_proof_and_never_the_token():
    """The attack, replayed against the fixed client.

    The impostor gets to choose the nonce, so it gets the strongest thing this
    handshake can ever give it: one HMAC over a challenge of its own. That
    authenticates it to nothing — it is not the key — and the key itself never
    leaves this process.
    """

    # The REAL service's token, so the relay below fails for the right reason.
    # An arbitrary secret would be refused because it is the wrong key, which
    # would make this test pass without the binding existing at all.
    with running_serve() as real:
        secret = read_token(_store_root()) or ""
        assert secret

        impostor = _ImpostorServer(
            {
                "event": "server_hello",
                # The RELAY: the impostor presents a challenge it took from the
                # real service, so nonce freshness has nothing left to say.
                "nonce": _server_nonce(real.port),
                "boot_id": "impostor",
                "contract": 1,
                "hello_contract": HELLO_CONTRACT_VERSION,
                "algorithm": "hmac-sha256",
            }
        )
        try:
            assert impostor.port != real.port
            connection = ServeSocketClient(
                "127.0.0.1", impostor.port, timeout_seconds=5.0
            )
            connection.connect()
            try:
                connection.hello(
                    token=secret, client="victim", client_build="deadbeef"
                )
            except Exception:
                # The impostor answers nothing, so the read after the proof
                # ends or times out. What it was SENT is the whole question.
                pass
            finally:
                connection.close()
            impostor.wait()
            relayed_nonce = impostor.greeting["nonce"]
        finally:
            impostor.close()

        wire = bytes(impostor.received)
        assert wire, "the client sent nothing at all — this proves nothing"
        assert secret.encode() not in wire
        answer = json.loads(wire.decode().strip())
        assert set(answer) == {"op", "client", "client_build", "proof"}
        assert answer["op"] == "hello"
        assert "token" not in answer
        # Bound to the port the victim DIALLED — the impostor's — even though
        # the nonce came from the real service.
        assert answer["proof"] == hello_proof(
            secret, relayed_nonce, port=impostor.port
        )
        # THE POINT, and what freshness alone never gave us. The harvested
        # proof carries the real service's own live nonce and the real token,
        # and it is still refused there, because it is not bound to that port.
        assert not verify_hello_proof(
            answer["proof"], relayed_nonce, secret, port=real.port
        ), "a proof harvested by an impostor verified at the real service"
        # ...and the same token dialling the real port IS admitted, so the
        # refusal above is the binding biting rather than the key being wrong.
        _, reply = raw_handshake(real.port, token=secret, client="victim")
        assert reply["event"] == "hello_ok"


def _server_nonce(port: int) -> str:
    """One live challenge from the service on [port], taken and abandoned.

    Exactly what a relaying impostor does: connect, read the challenge, hang
    up, and reuse it as its own.
    """

    peer = ServeSocketClient("127.0.0.1", port, timeout_seconds=WAIT)
    peer.connect()
    try:
        greeting = peer.read_frame()
        assert greeting["event"] == "server_hello"
        return greeting["nonce"]
    finally:
        peer.close()


@pytest.mark.parametrize(
    ("greeting", "expected"),
    [
        (None, "the peer did not open with a server_hello challenge"),
        ({"event": "banner", "service": "something else"}, "did not open with"),
        ({"event": "server_hello", "hello_contract": 2}, "no usable nonce"),
        (
            {"event": "server_hello", "nonce": "c" * 64, "hello_contract": 1},
            "unsupported hello_contract",
        ),
        (
            {"event": "hello_rejected", "reason": "rate_limited"},
            "did not open with",
        ),
    ],
)
def test_a_client_sends_nothing_at_all_to_a_peer_that_does_not_speak_this_contract(
    greeting, expected
):
    """Refused, not adapted.

    A client that guesses at an unknown handshake is a client that will
    eventually guess "send the token" — which is the shape this contract exists
    to retire. Each case asserts the STRONGEST form of that: zero bytes sent.
    """

    secret = "S3CRET-" + "d" * 40
    impostor = _ImpostorServer(greeting)
    try:
        connection = ServeSocketClient("127.0.0.1", impostor.port, timeout_seconds=3.0)
        connection.connect()
        try:
            with pytest.raises(ServeHelloProtocolError) as caught:
                connection.hello(token=secret, client="victim")
            assert expected in str(caught.value)
            if greeting is not None and greeting.get("event") == "hello_rejected":
                # The refusal's own reason IS the answer, and it is carried.
                assert caught.value.reason == "rate_limited"
        finally:
            connection.close()
        impostor.wait()
    finally:
        impostor.close()

    assert bytes(impostor.received) == b""


# ── discovery returns live rows, and callers gate on the classification ─────


def _write_registry_row(**overrides) -> int:
    from agent_runtime.serve_registry import register_serve_instance

    pid = int(overrides.pop("pid"))
    register_serve_instance(
        _store_root(),
        transport=overrides.pop("transport", "stdio+socket"),
        pid=pid,
        boot_id=overrides.pop("boot_id", "row-boot"),
        port=overrides.pop("port", 61999),
        socket_started_at="2026-08-13T00:00:00Z",
        probe=overrides.pop("probe", None),
    )
    return pid


def _dead_pid() -> int:
    """A pid this machine will classify as gone, FOUND rather than guessed.

    Asked through the registry's own probe, deliberately. ``os.kill(pid, 0)``
    is the obvious way to ask and is wrong on Windows — CPython routes signal 0
    through ``GenerateConsoleCtrlEvent``, so the liveness probe Ctrl-C's the
    target's console group (bpo-14484), which ``serve_registry._pid_alive``
    documents at length. Asking the same way the classifier asks also means
    this helper cannot disagree with the verdict the test is about to assert.
    """

    from agent_runtime.serve_registry import default_process_probe

    probe = default_process_probe()
    for candidate in range(60_000, 65_000):
        if probe.alive(candidate) is False:
            return candidate
    raise AssertionError("no provably-dead pid available on this machine")


def test_discovery_returns_no_target_at_all_when_the_only_row_is_dead():
    """The CRITICAL finding's first half.

    ``resolve_socket_target`` used to fall back to ``candidates`` — every row
    with a port, including one it had classified ``stale_dead_pid`` — and then
    to the owner sidecar with no classification at all. A dead serve's port is
    reusable by any local process, so that fallback was a target chosen from a
    record that says the owner is gone.
    """

    dead = _dead_pid()
    _write_registry_row(pid=dead, port=61999)

    from agent_runtime.serve_registry import list_serve_instances

    rows = list_serve_instances(_store_root())
    assert [row["classification"] for row in rows] == ["stale_dead_pid"]

    # By default: nothing. Not a row "you could try".
    assert resolve_socket_target(_store_root()) is None

    # The diagnostic form returns it CARRYING the verdict, so a caller can
    # refuse it by name rather than report the far less useful "not found".
    stale = resolve_socket_target(_store_root(), allow_stale=True)
    assert stale is not None
    assert stale.classification == "stale_dead_pid"
    assert stale.live is False
    assert stale.port == 61999


_LIVE_TICKS = 4242424242


def _probe_for(live_pid: int):
    """A probe that answers ``live`` for exactly one pid.

    Injected, because classification is a question about the OS and this test
    is about what the RESOLVER does with the three different answers it can
    get. An in-process serve can never classify itself ``live`` here anyway:
    the classifier requires a command line containing both "hermes" and
    "serve", and the process running these tests is pytest. The live path
    against a real serve is covered end to end in
    ``test_serve_socket_child_e2e.py``; the CHOICE is covered here.
    """

    from agent_runtime.serve_registry import ProcessProbe

    return ProcessProbe(
        alive=lambda pid: pid == live_pid,
        start_time=lambda pid: _LIVE_TICKS if pid == live_pid else None,
        cmdline=lambda pid: (
            "hermes harness serve --ndjson" if pid == live_pid else "notepad.exe"
        ),
    )


def test_discovery_prefers_the_live_row_over_the_dead_one_beside_it():
    from agent_runtime.serve_registry import list_serve_instances

    dead = _dead_pid()
    live = dead - 1
    probe = _probe_for(live)
    _write_registry_row(pid=dead, port=61999)
    _write_registry_row(pid=live, port=62000, probe=probe)

    rows = {
        row["pid"]: row["classification"]
        for row in list_serve_instances(_store_root(), probe=probe)
    }
    # The two rows really are classified DIFFERENTLY. Without this the test
    # would pass against a resolver that returned whatever came first.
    assert rows[dead] == "stale_dead_pid"
    assert rows[live] == "live"

    target = resolve_socket_target(_store_root(), probe=probe)
    assert target is not None
    assert target.live is True
    assert target.classification == "live"
    assert target.pid == live
    assert target.port == 62000
    assert target.source == "registry"

    # ...and the dead row is not the answer even on the diagnostic path, which
    # returns the BEST candidate rather than any candidate.
    stale = resolve_socket_target(_store_root(), probe=probe, allow_stale=True)
    assert stale.port == 62000


def test_the_owner_sidecar_alone_is_never_a_live_target():
    """The sidecar is discovery data written by a process that may be gone. It
    used to be returned with NO classification, which read as "no objection"
    at the one call site that had to object."""

    root = _store_root()
    lock = SocketOwnerLock(root)
    assert lock.acquire().acquired is True
    lock.publish_owner({"pid": 4242, "port": 61998, "boot_id": "sidecar-only"})
    try:
        assert resolve_socket_target(root) is None
        stale = resolve_socket_target(root, allow_stale=True)
        assert stale.source == "owner_file"
        assert stale.classification == "unverified_owner_file"
        assert stale.live is False
    finally:
        lock.release()


def test_the_connect_verb_refuses_a_target_that_is_not_live_and_names_why():
    """The CLIENT half of the same finding, at the verb an operator types."""

    import argparse
    import io
    from contextlib import redirect_stdout

    from hermes_cli.harness_parts.serve import (
        SERVE_CONNECT_NO_SERVICE_EXIT_CODE,
        _cmd_serve_connect,
    )

    dead = _dead_pid()
    _write_registry_row(pid=dead, port=61999)
    args = argparse.Namespace(probe=False, drain=False, deadline_seconds=None,
                              client=None, timeout=2.0)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = _cmd_serve_connect(args)
    report = json.loads(buffer.getvalue())

    assert code == SERVE_CONNECT_NO_SERVICE_EXIT_CODE
    assert report["ok"] is False
    assert report["error"] == "socket_service_not_live"
    # Named, not merely refused: "nothing found" would have hidden the row.
    assert report["classification"] == "stale_dead_pid"
    assert report["target"]["port"] == 61999
    assert report["target"]["live"] is False


# ── requests: one dispatcher, N transports ──────────────────────────────────


def test_a_socket_request_is_answered_on_the_socket_and_not_on_stdout():
    def _dispatch(argv):
        print(f"answer:{argv[-1]}")
        return 0

    with running_serve(dispatch=_dispatch) as handle:
        with client(handle, name="cli") as (connection, _reply):
            connection.send({"id": "req-1", "argv": ["harness", "status", "alpha"]})
            line = _read_until(connection, "line")
            assert line == {"id": "req-1", "event": "line", "line": "answer:alpha"}
            exit_frame = _read_until(connection, "exit")
            assert exit_frame == {"id": "req-1", "event": "exit", "code": 0}

        # The answer went to the asker, not to whoever owns stdout.
        assert not [f for f in handle.sink.frames() if f.get("id") == "req-1"]

        # ...and the stdio lane still answers its own requests, unchanged.
        handle.pipe.send({"id": "s-1", "argv": ["harness", "status", "beta"]})
        stdio_exit = next(
            f
            for f in _poll(handle.sink, lambda rows: [r for r in rows if r.get("id") == "s-1" and r.get("event") == "exit"])
        )
        assert stdio_exit == {"id": "s-1", "event": "exit", "code": 0}
        assert {"id": "s-1", "event": "line", "line": "answer:beta"} in handle.sink.frames()


def _poll(sink: _Sink, select, timeout: float = WAIT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = select(sink.frames())
        if found:
            return found
        time.sleep(0.01)
    raise AssertionError("nothing matched within the timeout")


def test_two_clients_may_use_the_same_request_id_without_colliding():
    """Request ids are chosen by CLIENTS. Once the service is multi-client, two
    of them WILL pick ``req-1``, and neither may see the other's answer."""

    def _dispatch(argv):
        print(f"from:{argv[-1]}")
        return 0

    with running_serve(dispatch=_dispatch) as handle:
        with client(handle, name="a") as (first, _r1), client(handle, name="b") as (second, _r2):
            first.send({"id": "req-1", "argv": ["harness", "status", "first"]})
            second.send({"id": "req-1", "argv": ["harness", "status", "second"]})
            first_line = _read_until(first, "line")
            second_line = _read_until(second, "line")
            assert first_line["line"] == "from:first"
            assert second_line["line"] == "from:second"
            assert _read_until(first, "exit")["code"] == 0
            assert _read_until(second, "exit")["code"] == 0


def test_a_client_cannot_cancel_another_clients_request():
    started = threading.Event()
    release = threading.Event()

    def _dispatch(argv):
        started.set()
        release.wait(WAIT)
        return 0

    with running_serve(dispatch=_dispatch, pool_size=4) as handle:
        with client(handle, name="owner") as (owner, _r1), client(handle, name="stranger") as (stranger, _r2):
            owner.send({"id": "req-1", "argv": ["harness", "status"]})
            assert started.wait(WAIT)
            stranger.send({"op": "cancel", "id": "req-1"})
            denied = _read_until(stranger, "cancel_denied")
            # Not even visible: another connection's request id is not a thing
            # this client has any standing to name.
            assert denied["state"] == "unknown"
            release.set()
            assert _read_until(owner, "exit")["code"] == 0


def test_shutdown_is_refused_on_the_socket_because_drain_is_the_multi_client_verb():
    with running_serve() as handle:
        with client(handle, name="impatient") as (connection, _reply):
            connection.send({"op": "shutdown"})
            error = _read_until(connection, "error")
            assert error["error"] == "op_not_available_on_socket"
            # Still serving everybody else.
            connection.send({"op": "ping"})
            assert _read_until(connection, "busy")["pending"] == 0


# ── subscriptions ───────────────────────────────────────────────────────────


def _fake_stream(gate: threading.Event):
    def _factory():
        def _generate():
            index = 0
            yield {"type": "hydrate", "index": index}
            while not gate.is_set():
                index += 1
                time.sleep(0.005)
                yield {"type": "delta", "index": index}

        return _generate()

    return _factory


def test_subscribe_pushes_the_stream_to_every_subscriber_and_unsubscribe_ends_it():
    gate = threading.Event()
    try:
        with running_serve(stream_source_factory=_fake_stream(gate)) as handle:
            with client(handle, name="a") as (first, _r1), client(handle, name="b") as (second, _r2):
                first.send({"op": "subscribe", "lane": "stream"})
                assert _read_until(first, "subscribed")["lane"] == "stream"
                second.send({"op": "subscribe"})
                assert _read_until(second, "subscribed")["lane"] == "stream"

                # Both are fed, and both start at a hydrate.
                for connection in (first, second):
                    frames = [connection.read_frame() for _ in range(4)]
                    assert any(f.get("type") == "hydrate" for f in frames)
                    assert any(f.get("type") == "delta" for f in frames)

                first.send({"op": "unsubscribe"})
                ended = _read_until(first, "unsubscribed")
                assert ended["was_subscribed"] is True

                # Still reported honestly for the one that stayed.
                second.send({"op": "connections"})
                summary = _read_until(second, "socket_connections")
                assert summary["subscriptions"]["subscribers"] == 1
    finally:
        gate.set()


def test_a_disconnect_unsubscribes_and_does_nothing_else():
    gate = threading.Event()
    try:
        with running_serve(stream_source_factory=_fake_stream(gate)) as handle:
            with client(handle, name="stayer") as (stayer, _r1):
                leaver = ServeSocketClient("127.0.0.1", handle.port, timeout_seconds=WAIT)
                leaver.connect()
                leaver.hello(
                    token=read_token(_store_root()) or "", client="leaver", client_build=None
                )
                leaver.send({"op": "subscribe"})
                _read_until(leaver, "subscribed")
                leaver.close()

                # The runtime outlives its client: the subscription is gone, the
                # connection is gone, and everything else is exactly as it was.
                deadline = time.monotonic() + WAIT
                summary = None
                while time.monotonic() < deadline:
                    stayer.send({"op": "connections"})
                    summary = _read_until(stayer, "socket_connections")
                    if summary["count"] == 1:
                        break
                    time.sleep(0.05)
                assert summary["count"] == 1
                assert summary["subscriptions"]["subscribers"] == 0
                assert [row["client"] for row in summary["connections"]] == ["stayer"]
    finally:
        gate.set()


def test_an_unsupported_lane_is_refused_by_name():
    with running_serve() as handle:
        with client(handle, name="a") as (connection, _reply):
            connection.send({"op": "subscribe", "lane": "telemetry"})
            denied = _read_until(connection, "subscribe_denied")
            assert denied == {
                "event": "subscribe_denied",
                "lane": "telemetry",
                "reason": "unsupported_lane",
            }


def test_a_subscriber_declares_what_it_can_fold_and_the_ack_says_what_was_accepted():
    """Patch-lane capability negotiation, over the subscribe op.

    The producer here is SHARED — one generator, N subscribers — so a client's
    declaration is a REQUEST, not a setting: what the lane may actually promote
    is the INTERSECTION over everybody attached, because every frame is fanned
    out to all of them. A client that assumed its own request was the answer
    would fold against a lane narrower than it believes, so the accepted set is
    stated back on the ack.
    """

    gate = threading.Event()
    try:
        with running_serve(stream_source_factory=_fake_stream(gate)) as handle:
            with client(handle, name="new") as (first, _r1), client(
                handle, name="legacy"
            ) as (second, _r2):
                # A fold-aware client naming an entity nothing else folds yet.
                first.send(
                    {
                        "op": "subscribe",
                        "lane": "stream",
                        "fold_entities": ["persona_instance", "office_actor"],
                    }
                )
                alone = _read_until(first, "subscribed")
                # Anti-vacuity: alone in the room, its declaration IS the answer,
                # so the narrowing below can only come from the second client.
                assert alone["fold_entities"] == ["office_actor", "persona_instance"]

                # A client that says nothing declares the historical set, and it
                # NARROWS the shared lane: the new entity stops being promotable
                # for anyone, because this one would re-hydrate on it.
                second.send({"op": "subscribe"})
                joined = _read_until(second, "subscribed")
                assert joined["fold_entities"] == ["persona_instance"]
    finally:
        gate.set()


def test_the_negotiated_set_reaches_the_producer_and_not_only_the_ack():
    """The ack could be honest and the producer still ignore the declaration.

    So this asserts at the other end of the plumbing: the source factory — the
    seam the real one builds ``stream_frames(fold_entities=…)`` from — is handed
    the accepted set, and is handed it AGAIN when a joiner narrows the room.
    """

    gate = threading.Event()
    handed: list[frozenset] = []

    def _recording_factory(fold_entities):
        handed.append(fold_entities)
        return _fake_stream(gate)()

    try:
        with running_serve(stream_source_factory=_recording_factory) as handle:
            with client(handle, name="new") as (first, _r1), client(
                handle, name="legacy"
            ) as (second, _r2):
                first.send(
                    {
                        "op": "subscribe",
                        "lane": "stream",
                        "fold_entities": ["persona_instance", "office_actor"],
                    }
                )
                _read_until(first, "subscribed")
                deadline = time.monotonic() + WAIT
                while not handed and time.monotonic() < deadline:
                    time.sleep(0.01)
                # Anti-vacuity: the first producer really was built with the
                # declared set, so the narrowing below is not "it never got one".
                assert handed[0] == frozenset({"persona_instance", "office_actor"})

                second.send({"op": "subscribe"})
                _read_until(second, "subscribed")
                deadline = time.monotonic() + WAIT
                while len(handed) < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert handed[-1] == frozenset({"persona_instance"})
    finally:
        gate.set()


@contextmanager
def _rpc_office_subscription(workspace_id: str, *, connection_key: str):
    """One RPC office subscription against the serve under test, released.

    The registry is PROCESS-GLOBAL, so a subscription leaked out of a failing
    assertion would widen the accepted set for every test that ran afterwards
    — which is the shape of bug that makes a suite pass in one order and fail
    in another.

    The wait for ``bound()`` is not padding. ``serve_loop`` announces ``ready``
    at line ~1436 and binds this registry several hundred lines later, so a
    client that subscribes the instant it sees ``ready`` is genuinely refused
    with ``push_lane_unavailable``. Without the wait this helper races that
    window and fails on scheduling.
    """

    from agent_runtime.serve_office_subscriptions import OFFICE_SUBSCRIPTIONS

    deadline = time.monotonic() + WAIT
    while not OFFICE_SUBSCRIPTIONS.bound() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert OFFICE_SUBSCRIPTIONS.bound(), "serve never bound the office push lane"

    registered = OFFICE_SUBSCRIPTIONS.subscribe(
        connection_key=connection_key,
        workspace_id=workspace_id,
        baseline_offset=0,
        emit=lambda frame: None,
    )
    # `.registered`, never the object's truthiness: `SubscribeOutcome.__bool__`
    # is unconditionally true, so `assert registered` would pass against every
    # refusal this helper exists to catch.
    assert registered.registered is True, "the office lane refused to register"
    try:
        yield
    finally:
        OFFICE_SUBSCRIPTIONS.release(connection_key)


def test_an_rpc_office_subscriber_declares_into_the_shared_producer():
    """The defect this lane spent its whole production life inside.

    ``_accepted_fold_entities`` read the STREAM lane's declaration table and
    nothing else. An RPC office subscriber registers against the SAME hub and
    is fanned the SAME frames, but contributed no declaration — so a room
    holding only office subscribers resolved to the historical
    ``{persona_instance, incident}``, ``office_actor`` was never promotable,
    and every office write demoted to a full core. The push lane could emit
    nothing but resync, on every transport, for its entire life.

    Asserted at the producer rather than at an ack, because the office lane has
    no ack to carry an accepted set: the factory is the seam the real
    ``stream_frames(fold_entities=…)`` is built from, and being handed the set
    is the only observable that means the promotion can actually happen.
    """

    from agent_runtime.serve_office_subscriptions import OFFICE_FOLD_ENTITIES

    gate = threading.Event()
    handed: list[frozenset] = []

    def _recording_factory(fold_entities):
        handed.append(fold_entities)
        return _fake_stream(gate)()

    try:
        with running_serve(stream_source_factory=_recording_factory) as handle:
            assert handle.ready["socket"]["outcome"] == "listening"
            with _rpc_office_subscription("ws_decl", connection_key="rpc-1"):
                deadline = time.monotonic() + WAIT
                while not handed and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert handed, "the office subscribe never built a producer"
                assert handed[0] == OFFICE_FOLD_ENTITIES
                assert "office_actor" in handed[0]
    finally:
        gate.set()


def test_a_legacy_stream_client_beside_an_office_subscriber_does_not_zero_the_room():
    """The trap the SUPERSET declaration exists to avoid, at serve level.

    The accepted set is an INTERSECTION over everyone attached. Had the office
    lane declared ``{office_actor}`` alone — the obvious-looking fix — then the
    moment a legacy stream client joined (declaring nothing, i.e. the
    historical set) the intersection would be EMPTY: nothing promotable for
    anybody, and the persona-instance patch lane that works today would go dark
    the instant an office subscriber existed. A regression, not a fix, and one
    that would look like a fix in every office-only test.

    So the office lane declares the historical set PLUS ``office_actor``, and
    the mixed room degrades to exactly today's wire instead of to nothing.
    Both halves are asserted: the accepted set is the historical one, and it is
    not empty.
    """

    from agent_runtime.patch_coverage import HISTORICAL_FOLD_ENTITIES
    from agent_runtime.serve_office_subscriptions import OFFICE_FOLD_ENTITIES

    gate = threading.Event()
    handed: list[frozenset] = []

    def _recording_factory(fold_entities):
        handed.append(fold_entities)
        return _fake_stream(gate)()

    try:
        with running_serve(stream_source_factory=_recording_factory) as handle:
            with _rpc_office_subscription("ws_mixed", connection_key="rpc-1"):
                deadline = time.monotonic() + WAIT
                while not handed and time.monotonic() < deadline:
                    time.sleep(0.01)
                # Anti-vacuity: alone in the room the office subscriber really
                # did widen it, so the narrowing below is the legacy client's
                # doing and not "the declaration never arrived".
                assert handed[0] == OFFICE_FOLD_ENTITIES

                with client(handle, name="legacy") as (connection, _reply):
                    connection.send({"op": "subscribe", "lane": "stream"})
                    ack = _read_until(connection, "subscribed")
                    assert ack["fold_entities"] == ["incident", "persona_instance"]
                    assert ack["fold_entities"] != []

                    deadline = time.monotonic() + WAIT
                    while len(handed) < 2 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    assert handed[-1] == HISTORICAL_FOLD_ENTITIES
                    assert handed[-1] != frozenset(), (
                        "the office declaration zeroed the room: a legacy "
                        "client beside it can promote nothing at all"
                    )
    finally:
        gate.set()


def test_a_subscriber_that_declares_nothing_is_told_the_historical_set():
    """The un-updated client sends exactly what it sends today and is answered
    with today's wire — NOT with an empty set, which would demote it to full
    cores forever."""

    gate = threading.Event()
    try:
        with running_serve(stream_source_factory=_fake_stream(gate)) as handle:
            with client(handle, name="a") as (connection, _reply):
                connection.send({"op": "subscribe", "lane": "stream"})
                ack = _read_until(connection, "subscribed")
                assert ack["fold_entities"] == ["incident", "persona_instance"]
    finally:
        gate.set()


# ── who the boot's ONE stale-first core is produced for (MC-4 / P6) ─────────
#
# The room derivation lives in a ``serve_loop`` closure
# (``_room_wants_stale_first``), so these run the REAL loop and the REAL
# subscribe paths. What they do NOT run is the real producer: ``_stream_source``
# imports ``stream_frames`` from ``agent_runtime.stream`` at call time, so a
# recorder installed there intercepts the argument the closure computed without
# any of these cases paying for a snapshot build.
#
# Asserted at the producer's ARGUMENT rather than at a delivered frame, for the
# same reason ``test_an_rpc_office_subscriber_declares_into_the_shared_producer``
# above asserts at the factory: the office sink discards a stale hydrate either
# way, so a frame-shaped assertion stays green against exactly the producer that
# shipped — the one that TOOK the boot's stale core and threw it away.


def _recording_stream_frames(monkeypatch, gate: threading.Event) -> list[dict]:
    """Intercept the hub's real ``stream_frames`` and record its kwargs."""

    from agent_runtime import stream as stream_module

    recorded: list[dict] = []

    def _recorder(**kwargs):
        recorded.append(dict(kwargs))

        def _generate():
            yield {"type": "hydrate", "watermark": {"event_offset": 0}}
            while not gate.is_set():
                time.sleep(0.005)
                yield {"type": "heartbeat", "watermark": {"event_offset": 0}}

        return _generate()

    monkeypatch.setattr(stream_module, "stream_frames", _recorder)
    return recorded


def test_an_office_only_room_does_not_ask_the_producer_for_a_stale_paint(monkeypatch):
    """A-x1's measured defect, at the seam that decides it.

    The RPC office subscribe attaches 0.1-0.2s before the launcher asks for
    anything, and it is what starts the hub producer. Under the process-global
    one-shot that producer consumed the boot's single stale core and
    ``office_patch_sink`` discarded it — two boots in three on 2026-08-18. A
    room of office-only sinks must answer False so the paint survives for the
    lane that shows it to somebody.
    """

    gate = threading.Event()
    recorded = _recording_stream_frames(monkeypatch, gate)
    try:
        with running_serve() as handle:
            assert handle.ready["socket"]["outcome"] == "listening"
            with _rpc_office_subscription("ws_stale_none", connection_key="rpc-1"):
                deadline = time.monotonic() + WAIT
                while not recorded and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert recorded, "the office subscribe never built a producer"
                assert recorded[0]["caller"] == "hub"
                assert recorded[0]["wants_stale_first"] is False, recorded[0]
    finally:
        gate.set()


def test_a_stream_subscriber_joining_AFTER_the_office_lane_still_gets_the_paint(
    monkeypatch,
):
    """The attach ORDER the field actually produces, and the harder half.

    Office first, painter second — the ordering that lost on 2026-08-18. The
    producer the office subscribe built takes nothing; the painting subscriber's
    own join restarts it (``StreamHub.subscribe`` does, by contract) and THAT
    generation is built for a room with a painter in it. Its own case, separate
    from the reverse order below: they are two claims about two orderings and
    one of them used to be the only one that worked.
    """

    gate = threading.Event()
    recorded = _recording_stream_frames(monkeypatch, gate)
    try:
        with running_serve() as handle:
            with _rpc_office_subscription("ws_stale_late", connection_key="rpc-1"):
                deadline = time.monotonic() + WAIT
                while not recorded and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert recorded, "the office subscribe never built a producer"
                # Anti-vacuity: the FIRST generation really did answer False, so
                # the True below is the painter's join and not a constant.
                assert recorded[0]["wants_stale_first"] is False

                with client(handle, name="painter") as (connection, _reply):
                    connection.send({"op": "subscribe", "lane": "stream"})
                    _read_until(connection, "subscribed")
                    deadline = time.monotonic() + WAIT
                    while len(recorded) < 2 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    assert len(recorded) >= 2, (
                        "the painting subscribe did not restart the producer, so "
                        "no generation was ever built for a room that paints"
                    )
                    assert recorded[-1]["wants_stale_first"] is True, recorded[-1]
    finally:
        gate.set()


def test_a_stream_subscriber_joining_BEFORE_the_office_lane_gets_the_paint(monkeypatch):
    """The reverse order, asserted separately (C30).

    Painter first: the very first generation is built for a room that paints, so
    the stale core goes out without waiting for a restart. The office subscribe
    behind it must not narrow that back — the operator is union, not
    intersection, and one painter in the room is enough.

    HOW the office join leaves it alone is worth naming, because it is not the
    restart this file's other case relies on: the office declaration is a
    SUPERSET of the accepted set in force, so ``OfficeSubscriptions.subscribe``
    takes its restart-free rejoin (O-H5) and the painter's generation is never
    replaced at all. Measured here, not assumed — the assertion is over EVERY
    generation recorded, so it holds whether the join restarts or not, and a
    change to that rejoin rule cannot silently turn this case vacuous.
    """

    gate = threading.Event()
    recorded = _recording_stream_frames(monkeypatch, gate)
    try:
        with running_serve() as handle:
            with client(handle, name="painter") as (connection, _reply):
                connection.send({"op": "subscribe", "lane": "stream"})
                _read_until(connection, "subscribed")
                deadline = time.monotonic() + WAIT
                while not recorded and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert recorded, "the stream subscribe never built a producer"
                assert recorded[0]["wants_stale_first"] is True, recorded[0]
                built_for_painter = len(recorded)

                with _rpc_office_subscription("ws_stale_early", connection_key="rpc-2"):
                    # Bounded settle: if the join DOES restart, the new
                    # generation must be recorded before the assertion reads it.
                    deadline = time.monotonic() + 1.0
                    while len(recorded) == built_for_painter and time.monotonic() < deadline:
                        time.sleep(0.01)
                    assert all(
                        entry["wants_stale_first"] is True for entry in recorded
                    ), (
                        "an office sink in a room that already had a painter "
                        f"narrowed it back to False; the operator is a union: {recorded}"
                    )
    finally:
        gate.set()


def test_a_malformed_fold_declaration_is_refused_instead_of_read_as_absent():
    """Reading a malformed declaration as absence would WIDEN a client that
    meant to narrow — handing it patches it cannot fold, which is precisely the
    failure this negotiation exists to prevent. It is refused by name."""

    with running_serve() as handle:
        with client(handle, name="a") as (connection, _reply):
            for malformed in ("persona_instance", [""], [7], {"persona_instance": True}):
                connection.send(
                    {"op": "subscribe", "lane": "stream", "fold_entities": malformed}
                )
                assert _read_until(connection, "subscribe_denied") == {
                    "event": "subscribe_denied",
                    "lane": "stream",
                    "reason": "invalid_fold_entities",
                }


# ── observability ───────────────────────────────────────────────────────────


def test_the_version_reply_carries_the_connections_block_on_both_transports():
    with running_serve() as handle:
        with client(handle, name="cockpit", build="0" * 40) as (connection, _reply):
            connection.send({"op": "version"})
            version = _read_until(connection, "version")
            assert version["transport"] == "socket"
            assert version["socket"]["outcome"] == "listening"
            rows = version["connections"]["connections"]
            assert [row["client"] for row in rows] == ["cockpit"]
            assert rows[0]["client_build"] == "0" * 40
            assert rows[0]["frames_out"] >= 1
            assert rows[0]["bytes_out"] > 0
            assert rows[0]["subscribed"] is False

            # The stdio lane's own version reply still says stdio.
            handle.pipe.send({"op": "version"})
            stdio_version = handle.sink.wait_for("version")
            assert stdio_version["transport"] == "stdio"
            assert stdio_version["connections"]["count"] == 1


def test_every_connection_open_close_and_rejection_emits_one_structured_log_line():
    with running_serve() as handle:
        with client(handle, name="watched") as (_conn, _reply):
            pass
        # A proof this test can recognise on sight, so "the proof was not
        # logged" is a check against THAT value rather than against the
        # word — which is a substring of the rejection reason itself.
        presented = "f" * 64
        _greeting, rejected_frame = raw_handshake(
            handle.port, token=None, proof=presented, client="thief"
        )
        assert rejected_frame["reason"] == REJECT_BAD_PROOF

        def _service_logs(rows):
            found = [
                json.loads(row["line"])
                for row in rows
                if row.get("event") == "stderr" and row.get("line", "").startswith("{")
            ]
            # Wait for the LAST of the three, not for any log at all: the
            # rejection is written on the rejected connection's own thread and
            # lands after the ones this test already provoked.
            events = {row.get("event") for row in found}
            return found if "serve_socket_connection_rejected" in events else []

        logs = _poll(handle.sink, _service_logs)
        by_event = {row["event"]: row for row in logs}
        assert by_event["serve_socket_connection_open"]["client"] == "watched"
        assert by_event["serve_socket_connection_open"]["boot_id"] == handle.ready["boot_id"]
        assert by_event["serve_socket_connection_close"]["client"] == "watched"
        assert by_event["serve_socket_connection_close"]["reason"]
        rejected = by_event["serve_socket_connection_rejected"]
        assert rejected["reason"] == REJECT_BAD_PROOF
        # The rejected peer's own label is recorded; nothing derived from the
        # real secret is — and the proof it DID present is not logged either,
        # because a log full of HMACs over known nonces is an offline corpus.
        assert rejected["peer"]
        token = read_token(_store_root()) or ""
        blob = json.dumps(logs)
        assert token not in blob
        # A log full of HMACs over known nonces is an offline corpus, so
        # the presented proof is not written down either.
        assert presented not in blob


# ── drain over the socket ───────────────────────────────────────────────────


def test_a_drain_asked_over_the_socket_tells_every_client_then_closes_the_lane():
    """The live proof slice 2 could not run: a drain observed from OUTSIDE the
    process, by a client that stays attached to watch it finish."""

    from agent_runtime.serve_registry import list_serve_instances

    root = _store_root()
    with running_serve() as handle:
        with client(handle, name="watcher") as (watcher, _r1), client(handle, name="operator") as (operator, _r2):
            operator.send({"op": "drain", "force": True, "deadline_seconds": 10})

            # Every attached client hears it, not just the one that asked.
            assert _read_until(watcher, "draining")["pid"] == handle.ready["pid"]
            assert _read_until(operator, "draining")["boot_id"] == handle.ready["boot_id"]

            complete = _read_until(operator, "drain_complete")
            assert complete["requests_refused"] == 0
            assert complete["requests_completed"] == 0

            # The watcher — which asked for nothing — is told how it ended too.
            assert _read_until(watcher, "drain_complete")["drain_ms"] >= 0

            # And then the lane closes under both of them, which is the point:
            # a client's next read returns end-of-stream, not silence.
            assert watcher.read_frame() is None
            assert operator.read_frame() is None

        # The lane is released: registry entry gone, owner sidecar gone.
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline and list_serve_instances(root):
            time.sleep(0.05)
        assert list_serve_instances(root) == []
        assert not socket_owner_path(root).exists()

        # The LOCK FILE, however, stays — deliberately, and this is the
        # assertion that changed. Releasing used to unlink it, and on POSIX
        # that is a two-owner race: ``flock`` is held on an open DESCRIPTION,
        # not on a path, so a contender that has opened the file but not yet
        # locked it ends up holding a lock on an unnamed inode while the next
        # boot creates a fresh inode at that path and locks THAT. Both would
        # believe they own the one root. A persistent zero-byte file costs
        # nothing and closes it.
        assert socket_lock_path(root).exists()
        assert socket_lock_path(root).stat().st_size <= 1

        # And it is still just a file with a lock on it: a fresh serve takes
        # the lane over immediately, which is the property the unlink was
        # (wrongly) thought to provide.
        replacement = SocketOwnerLock(root)
        assert replacement.acquire().acquired is True
        replacement.release()
        assert socket_lock_path(root).exists()

    assert handle.code == 0


def test_a_drain_over_the_socket_requires_force_and_the_refusal_is_typed():
    """`shutdown` is refused on this lane outright; `drain` is the safe
    replacement verb — but it still ends the service for every attached
    client, so it has to be asked for on purpose."""

    with running_serve() as handle:
        with client(handle, name="careless") as (connection, _reply):
            connection.send({"op": "drain"})
            error = _read_until(connection, "error")
            assert error["error"] == "drain_requires_force"
            assert error["transport"] == "socket"
            # Nothing was started: the service is still serving.
            connection.send({"op": "ping"})
            assert _read_until(connection, "busy")["pending"] == 0
            connection.send({"op": "version"})
            assert _read_until(connection, "version")["draining"] is False


def test_a_socket_client_cannot_clamp_the_drain_deadline_to_nothing():
    """F2's first half. ``drain`` on this lane is reachable by any local
    process holding the root's secret, and the timeout path calls ``hard_exit``
    — which is ``os._exit`` — so a client that could ask for 0.05s could turn
    the restart verb into a kill over work it cannot see."""

    with running_serve(drain_socket_minimum_deadline_seconds=25.0) as handle:
        with client(handle, name="impatient") as (connection, _reply):
            connection.send({"op": "drain", "force": True, "deadline_seconds": 0.05})
            draining = _read_until(connection, "draining")
            # Floored, and SAYING SO: what was asked for sits beside what was
            # granted, so a client cannot mistake a floor for an honoured ask.
            assert draining["deadline_seconds"] == 25.0
            assert draining["requested_deadline_seconds"] == 0.05
            assert draining["minimum_deadline_seconds"] == 25.0


def test_a_stdio_drain_deadline_is_still_the_askers_to_choose():
    """The other side of the same fix, and the reason the floor is per-lane.

    The stdio asker is the PARENT that spawned this process and owns its stdin.
    It can end this runtime with a signal whether or not the drain cooperates,
    so flooring it buys no safety — and would silently rewrite a contract the
    socket slice promised to leave byte-identical.
    """

    with running_serve(drain_socket_minimum_deadline_seconds=25.0) as handle:
        handle.pipe.send({"op": "drain", "deadline_seconds": 0.5})
        draining = handle.sink.wait_for("draining")
        assert draining["deadline_seconds"] == 0.5
        assert draining["minimum_deadline_seconds"] == 0.05
        handle.sink.wait_for("drain_complete")


def test_a_live_chat_turn_survives_a_socket_drain_and_the_hold_is_reported():
    """F2's second half, and the finding that made it [HIGH].

    ``serve``'s own contract says a supervisor must never recycle this process
    while ``chat_turns > 0`` — recording safety. A drain deadline firing
    ``hard_exit(3)`` over a live turn is that recycle by another name, and any
    local process holding the token could ask for it. So an expiry with a chat
    turn in flight is NOT terminal: it says so, keeps serving, and re-arms.

    The assertion that matters is the one that would pass vacuously if the turn
    had simply been killed: the turn LANDS, with its own exit frame, AFTER the
    deadline it outlived.
    """

    started = threading.Event()
    release = threading.Event()
    exits: list[int] = []

    def _dispatch(argv):
        started.set()
        release.wait(WAIT)
        return 0

    with running_serve(
        dispatch=_dispatch,
        drain_socket_minimum_deadline_seconds=0.2,
        drain_poll_interval_seconds=0.01,
        hard_exit=exits.append,
    ) as handle:
        with client(handle, name="turn-holder") as (connection, _reply):
            # The argv shape serve recognises as a chat turn.
            connection.send(
                {"id": "turn-1", "argv": ["harness", "mission-chat", "message", "hi"]}
            )
            assert started.wait(WAIT)

            connection.send({"op": "drain", "force": True, "deadline_seconds": 0.2})
            assert _read_until(connection, "draining")["pending"] == 1

            # The deadline lapses, repeatedly, and is HELD each time.
            held = _read_until(connection, "drain_timeout")
            assert held["terminal"] is False
            assert held["held_by_chat_turns"] == 1
            assert held["chat_turn_request_ids"] == ["conn-1:turn-1"]
            assert held["deadline_seconds"] == 0.2

            # Nothing was killed while it was held — the whole point.
            assert exits == []
            connection.send({"op": "ping"})
            assert _read_until(connection, "busy")["chat_turns"] == 1

            # A second lapse proves it re-arms rather than stopping at one.
            second = _read_until(connection, "drain_timeout")
            assert second["terminal"] is False
            assert second["deadline_holds"] >= 2

            # And now the turn is allowed to LAND.
            release.set()
            assert _read_until(connection, "exit") == {
                "id": "turn-1",
                "event": "exit",
                "code": 0,
            }
            complete = _read_until(connection, "drain_complete")
            assert complete["requests_completed"] == 1
            assert complete["deadline_holds"] >= 2

    # A clean drain, never a forced kill.
    assert exits == [] or exits == [0]
    assert handle.code == 0


def test_a_drain_refuses_new_connections_while_it_finishes():
    started = threading.Event()
    release = threading.Event()

    def _dispatch(argv):
        started.set()
        release.wait(WAIT)
        return 0

    with running_serve(dispatch=_dispatch) as handle:
        with client(handle, name="worker") as (worker, _r1):
            worker.send({"id": "slow-1", "argv": ["harness", "status"]})
            assert started.wait(WAIT)
            worker.send({"op": "drain", "force": True, "deadline_seconds": 10})
            assert _read_until(worker, "draining")["pending"] == 1

            # New work is refused on the socket exactly as it is on stdio —
            # typed, plus the terminal exit a waiting client needs.
            worker.send({"id": "late-1", "argv": ["harness", "status"]})
            refusal = _read_until(worker, "draining")
            assert refusal["id"] == "late-1"
            refused_exit = _read_until(worker, "exit")
            assert refused_exit == {
                "id": "late-1",
                "event": "exit",
                "code": serve_module.DRAINING_EXIT_CODE,
                "draining": True,
            }

            latecomer = ServeSocketClient("127.0.0.1", handle.port, timeout_seconds=WAIT)
            try:
                latecomer.connect()
            except OSError:
                # The listener is already closed — the strongest form of refusal.
                pass
            else:
                try:
                    frame = latecomer.read_frame()
                    # Refused BEFORE the challenge, which is stronger than
                    # refused after it: a draining lane does not mint a nonce
                    # for a peer it has already decided not to serve.
                    assert frame is None or frame == {
                        "event": "hello_rejected",
                        "reason": REJECT_DRAINING,
                    }
                finally:
                    latecomer.close()

            release.set()
            complete = _read_until(worker, "drain_complete")
            # The in-flight request was allowed to LAND, which is the whole
            # difference between a drain and a kill.
            assert complete["requests_completed"] == 1


def test_two_connections_asking_to_drain_at_once_start_exactly_one_drain():
    """F7: the ``drain_state`` transition is a guarded critical section.

    It was a bare read-modify-write on a closure variable — harmless while the
    only caller was the single stdio reader, and a genuine race the moment N
    connection threads could ask. Two of them could both observe ``None``, both
    install a ``_DrainState``, and the process would run two monitors, publish
    two terminal frames, and split its counters across two objects.

    The window is normally microseconds, so it is widened here to 250ms INSIDE
    the state's own constructor — which is inside the critical section, so with
    the lock the loser simply waits and is told ``drain_in_progress``, and
    without it the loser has already decided ``None`` and proceeds to install a
    second state of its own.
    """

    original_init = serve_module._DrainState.__init__

    def _slow_init(self, deadline_seconds):
        time.sleep(0.25)
        original_init(self, deadline_seconds)

    started = threading.Event()
    release = threading.Event()

    def _dispatch(argv):
        started.set()
        release.wait(WAIT)
        return 0

    serve_module._DrainState.__init__ = _slow_init
    try:
        with running_serve(dispatch=_dispatch, drain_poll_interval_seconds=0.01) as handle:
            with client(handle, name="first") as (first, _r1), client(handle, name="second") as (second, _r2):
                # Real work in flight, so the drain cannot finish before both
                # asks land and the race is actually run.
                first.send({"id": "slow-1", "argv": ["harness", "status"]})
                assert started.wait(WAIT)

                seen: dict[str, list[dict]] = {"first": [], "second": []}

                def _drain_reader(name, connection):
                    while True:
                        try:
                            frame = connection.read_frame()
                        except Exception:
                            return
                        if frame is None:
                            return
                        seen[name].append(frame)

                readers = [
                    threading.Thread(
                        target=_drain_reader, args=(name, connection), daemon=True
                    )
                    for name, connection in (("first", first), ("second", second))
                ]
                for thread in readers:
                    thread.start()

                barrier = threading.Barrier(2)
                errors: list[BaseException] = []

                def _ask(connection):
                    try:
                        barrier.wait(WAIT)
                        connection.send(
                            {"op": "drain", "force": True, "deadline_seconds": 30}
                        )
                    except BaseException as exc:  # noqa: BLE001 - reported below
                        errors.append(exc)

                askers = [
                    threading.Thread(target=_ask, args=(connection,), daemon=True)
                    for connection in (first, second)
                ]
                for thread in askers:
                    thread.start()
                for thread in askers:
                    thread.join(WAIT)
                assert not errors, errors

                # The loser is TOLD, by name, on its own connection.
                def _refusals():
                    return [
                        row
                        for rows in seen.values()
                        for row in rows
                        if row.get("event") == "drain_in_progress"
                    ]

                deadline = time.monotonic() + WAIT
                while time.monotonic() < deadline and not _refusals():
                    time.sleep(0.02)
                assert len(_refusals()) == 1

                release.set()
                completions = _poll(
                    handle.sink,
                    lambda rows: [r for r in rows if r.get("event") == "drain_complete"],
                )
                # Exactly ONE drain started, exactly ONE terminal frame, and its
                # counters are not split across two states: the single request
                # landed and is counted once.
                announcements = [
                    row
                    for row in handle.sink.frames()
                    if row.get("event") == "draining" and row.get("id") is None
                ]
                assert len(announcements) == 1, (
                    f"{len(announcements)} drains started — the transition is "
                    "not a critical section"
                )
                assert len(completions) == 1
                assert completions[0]["requests_completed"] == 1
    finally:
        release.set()
        serve_module._DrainState.__init__ = original_init


class _StallingSink(_Sink):
    """A stdout that PARKS on the first frame matching a marker.

    A subscriber's pump is the thing that writes to its transport, so a
    transport that has stopped draining IS a parked write. This reproduces
    that at the one seam a test can hold still, which is what makes the
    backpressure numbers below deterministic rather than a race.
    """

    def __init__(self, marker: str, release: threading.Event) -> None:
        super().__init__()
        self._marker = marker
        self._release = release
        self.entered = threading.Event()

    def write(self, text: str) -> int:
        if self._marker in text and not self.entered.is_set():
            self.entered.set()
            self._release.wait(30.0)
        return super().write(text)


def test_a_dropped_subscription_tells_the_client_which_bound_it_tripped():
    """F8 at the JOIN, which is where it was still broken.

    The hub measures ``drop_bound``, ``bytes_discarded`` and ``byte_limit``;
    the frame that reaches a CLIENT threw all three away and reported a frame
    count against a ``buffer_limit`` read from the config — None whenever it
    was left at the default. Both halves of that seam were right and the join
    between them was not, which is this workstream's recurring shape.

    The bounds here are set so only ONE of them can possibly trip: 100k frames
    is unreachable, 64 KiB of 8 KiB frames is eight. So "which bound" has a
    correct answer that a hard-coded one would get wrong.
    """

    release = threading.Event()
    sink = _StallingSink('"type": "delta"', release)
    payload = "x" * 8192

    def _factory():
        def _generate():
            index = 0
            while not release.is_set():
                index += 1
                time.sleep(0.001)
                yield {"type": "delta", "index": index, "blob": payload}

        return _generate()

    with running_serve(
        sink=sink,
        stream_source_factory=_factory,
        stream_buffer_limit=100_000,
        stream_byte_limit=64 * 1024,
    ) as handle:
        handle.pipe.send({"op": "subscribe"})
        # The pump is parked mid-write: the subscriber has stopped draining.
        assert sink.entered.wait(WAIT)
        # Long enough for the producer to blow past 64 KiB and nowhere near
        # 100k frames — so the bound that trips is not in doubt.
        time.sleep(0.5)
        release.set()

        dropped = sink.wait_for("subscription_dropped")
        assert dropped["reason"] == "backpressure"
        assert dropped["bound"] == "bytes"
        # BOTH bounds and both numbers, so the drop is a fact an operator can
        # act on instead of an adjective.
        assert dropped["byte_limit"] == 64 * 1024
        assert dropped["buffer_limit"] == 100_000
        assert dropped["bytes_discarded"] > 0
        assert dropped["frames_discarded"] > 0
        # The service log line carries the same verdict, not a thinner one.
        service = [
            json.loads(row["line"])
            for row in sink.frames()
            if row.get("event") == "stderr" and row.get("line", "").startswith("{")
        ]
        drop_logs = [
            row for row in service
            if row.get("event") == "serve_stream_subscription_dropped"
        ]
        assert drop_logs and drop_logs[0]["bound"] == "bytes"
        assert drop_logs[0]["bytes_discarded"] > 0


# ── fingerprint exclusion ───────────────────────────────────────────────────


def test_the_socket_files_move_no_freshness_fingerprint():
    """A file that appears at first boot must not cold the read-model cache or
    make the stream emit ``state.reconciled`` on every restart. Same standing
    precedent as ``dispatch_delivery.DRAIN_STATE_FILENAME``."""

    from agent_runtime import stream as stream_module

    root = _store_root()
    root.mkdir(parents=True, exist_ok=True)
    before = (
        serve_module._runtime_state_fingerprint(),
        stream_module._scope_fingerprint(),
    )
    lock = SocketOwnerLock(root)
    assert lock.acquire().acquired is True
    lock.publish_owner({"pid": 1, "port": 12345})
    try:
        assert socket_lock_path(root).exists()
        assert socket_owner_path(root).exists()
        after = (
            serve_module._runtime_state_fingerprint(),
            stream_module._scope_fingerprint(),
        )
        assert after == before
    finally:
        lock.release()


# ── the pre-auth path is driven by an unauthenticated peer ──────────────────


def test_a_non_ascii_proof_is_refused_like_any_other_wrong_one():
    """The [HIGH] finding: one accented character escaped the handshake.

    ``hmac.compare_digest`` RAISES ``TypeError`` when either ``str`` operand is
    non-ASCII, and the comparison sat on a path an unauthenticated peer drives.
    The exception unwound out of the connection thread, so the peer got no
    rejection frame, the attempt was counted nowhere, the rate limiter was
    never charged — the F3 bound bypassed by one byte — and the traceback went
    to a stderr the serve loop has redirected onto the NDJSON protocol stream.

    Every clause below is the absence of one of those. The rejection alone
    would be a weak assertion: a peer being refused proves nothing about
    whether the refusal was DECIDED or merely survived.
    """

    clock = _Clock()
    # Four hostile proofs against a limit of FOUR: every one of them must be
    # judged on its merits, and only the fifth connection meets a closed window.
    limiter = HelloRateLimiter(limit=4, window_seconds=10.0, clock=clock)
    with bare_server(rate_limiter=limiter, reject_penalty_seconds=0.0) as (
        server,
        port,
        _logs,
    ):
        for proof in ("é" * 64, "☃" + "a" * 63, "prøøf", "​" * 64):
            _g, reply = raw_handshake(port, token=None, proof=proof)
            assert reply == {
                "event": "hello_rejected",
                "reason": REJECT_BAD_PROOF,
            }, f"a non-ASCII proof {proof[:8]!r} did not get a typed rejection"

        payload = server.connections_payload()
        # COUNTED. The old path left no number anywhere.
        assert payload["rejected_by_reason"][REJECT_BAD_PROOF] == 4
        assert payload["rejected_total"] == 4
        # ...and it did not raise on the way there, so the defence-in-depth
        # conversion never ran: this is a decided rejection, not a rescued one.
        assert payload["handshake_errors"] == 0
        assert payload["last_handshake_error"] is None

        # CHARGED. Four auth failures against a limit of four closes the
        # window — the guarantee the escaping exception used to skip entirely.
        _g, blocked = raw_handshake(port, token="the-shared-secret")
        assert blocked == {"event": "hello_rejected", "reason": REJECT_RATE_LIMITED}


def test_a_handshake_that_RAISES_still_rejects_charges_and_is_counted():
    """Defence in depth for the class, not just the one member of it.

    The finding was a specific call that could raise. The DESIGN defect was
    that a single uncaught call existed on a path an unauthenticated peer
    drives — so fixing only the comparison leaves the next such call to
    rediscover it. Any exception escaping the handshake is now converted into
    a refusal, charged like one, and counted as the distinct operational fact
    that it is.
    """

    clock = _Clock()
    limiter = HelloRateLimiter(limit=2, window_seconds=10.0, clock=clock)
    with bare_server(rate_limiter=limiter, reject_penalty_seconds=0.0) as (
        server,
        port,
        _logs,
    ):
        original = serve_socket.verify_hello_proof

        def _explode(*args, **kwargs):
            raise RuntimeError("boom from inside the handshake")

        serve_socket.verify_hello_proof = _explode
        try:
            for _ in range(2):
                _g, reply = raw_handshake(port, token="the-shared-secret")
                assert reply == {
                    "event": "hello_rejected",
                    "reason": REJECT_HELLO_MALFORMED,
                }
        finally:
            serve_socket.verify_hello_proof = original

        payload = server.connections_payload()
        assert payload["handshake_errors"] == 2
        assert payload["last_handshake_error"] == "RuntimeError"

        # Charged as an auth failure, so it cannot be used to hammer the
        # handshake for free. The limit is 2 and both raised.
        _g, blocked = raw_handshake(port, token="the-shared-secret")
        assert blocked == {"event": "hello_rejected", "reason": REJECT_RATE_LIMITED}

        # ...and the server is still serving: the escape did not take the lane
        # down with it.
        clock.advance(11.0)
        _g, recovered = raw_handshake(port, token="the-shared-secret")
        assert recovered["event"] == "hello_ok"


def test_a_peer_that_FLOODS_is_not_accounted_as_a_peer_that_was_SILENT():
    """A 1 MiB garbage line was classified ``hello_timeout``.

    The two throttles exist to be different: auth failures close the shared
    door, and silence gets its own budget so a burst of abandoned connections
    cannot lock out a client holding the right credential. Charging a flood to
    the silence budget spends the quiet clients' allowance on an attack, and
    records the attack as an absence.
    """

    with bare_server(reject_penalty_seconds=0.0) as (server, port, _logs):
        connection = ServeSocketClient("127.0.0.1", port, timeout_seconds=WAIT)
        connection.connect()
        try:
            greeting = connection.read_frame()
            assert greeting["event"] == "server_hello"
            # No newline: one line, larger than the reader will ever accept.
            connection._sock.sendall(b"x" * (MAX_LINE_BYTES + 4096))
            reply = connection.read_frame()
        finally:
            connection.close()

        assert reply == {"event": "hello_rejected", "reason": REJECT_HELLO_TOO_LONG}
        payload = server.connections_payload()
        assert payload["rejected_by_reason"] == {REJECT_HELLO_TOO_LONG: 1}
        assert payload["hello_timeouts"] == 0, (
            "a flood was accounted as silence, spending the budget reserved "
            "for peers that presented nothing"
        )


def test_a_completed_handshake_clears_the_SILENCE_throttle_too():
    """The asymmetry the first pass left behind.

    ``record_success`` was wired to the auth limiter and not to the timeout
    limiter, so the silence throttle was cleared only by the passage of time.
    A burst of abandoned connections therefore went on refusing every peer —
    including one holding the right credential — which is the same "the
    server's own state locks out a good client" shape F3 set out to retire,
    applied to the other half of the pair.
    """

    clock = _Clock()
    timeouts = HelloRateLimiter(limit=2, window_seconds=60.0, clock=clock)
    with bare_server(
        timeout_limiter=timeouts,
        hello_deadline_seconds=0.05,
        reject_penalty_seconds=0.0,
    ) as (server, port, _logs):
        # STAGGERED on purpose. Two abandoned connections at the same instant
        # age out together, and then nothing downstream can tell "the window
        # emptied by itself" from "the handshake cleared it" — the test would
        # pass against the defect. These are 30s apart so exactly one of them
        # can be expired at a time.
        reply = _silent_peer(port)
        assert reply == {"event": "hello_rejected", "reason": REJECT_HELLO_TIMEOUT}
        clock.advance(30.0)
        reply = _silent_peer(port)
        assert reply == {"event": "hello_rejected", "reason": REJECT_HELLO_TIMEOUT}
        assert server.connections_payload()["hello_timeouts"] == 2

        # The throttle is closed. A GOOD client is refused — the honest cost.
        _g, throttled = raw_handshake(port, token="the-shared-secret")
        assert throttled == {
            "event": "hello_rejected",
            "reason": REJECT_HANDSHAKE_THROTTLED,
        }

        # Now 61s past the FIRST timeout and 31s past the second, so the window
        # holds exactly one entry and the good client gets in.
        clock.advance(31.0)
        _g, admitted = raw_handshake(port, token="the-shared-secret")
        assert admitted["event"] == "hello_ok"

        # THE ASSERTION. One more silent peer must not re-close a throttle that
        # a completed handshake proved unnecessary.
        reply = _silent_peer(port)
        assert reply == {"event": "hello_rejected", "reason": REJECT_HELLO_TIMEOUT}
        _g, still_in = raw_handshake(port, token="the-shared-secret")
        assert still_in["event"] == "hello_ok", (
            "a completed handshake did not clear the silence throttle, so one "
            "abandoned connection re-locked a client holding the right secret"
        )


def _silent_peer(port: int) -> dict | None:
    """A peer that connects, reads the challenge, and then says NOTHING.

    ``raw_handshake`` always answers something, so it can never produce the
    hello TIMEOUT — an empty ``hello`` object is a malformed answer, not an
    absent one, and the server rightly classifies it ``hello_required``. The
    distinction is the whole subject of the throttle under test.
    """

    connection = ServeSocketClient("127.0.0.1", port, timeout_seconds=WAIT)
    connection.connect()
    try:
        greeting = connection.read_frame()
        assert greeting["event"] == "server_hello"
        return connection.read_frame()
    finally:
        connection.close()


def test_drain_progress_reaches_the_SOCKET_client_and_not_only_stdio(monkeypatch):
    """The one drain frame that was emitted to stdout and nowhere else.

    Its entire stated purpose is that "a draining service never looks dead to a
    watchdog" — and the socket client IS such a watchdog: it reads with a
    finite timeout (10s by default) and reports ``transport_failed`` on
    silence. Every other drain frame was broadcast to both lanes; this one was
    not, and the socket lane's own minimum deadline made the gap wider than the
    client's patience. A healthy, completing drain therefore reported a
    transport failure.

    The assertion that would pass vacuously is "a drain_progress exists" — it
    always did, on stdio. What is checked here is that one arrives ON THE
    SOCKET, before the deadline that used to be the first thing the socket ever
    saw, and that it carries the in-flight request it is reporting about.
    """

    monkeypatch.setattr(serve_module, "_DRAIN_PROGRESS_INTERVAL_SECONDS", 0.05)

    started = threading.Event()
    release = threading.Event()

    def _dispatch(argv):
        started.set()
        release.wait(WAIT)
        return 0

    with running_serve(
        dispatch=_dispatch,
        # PRODUCTION floor, deliberately: the defect only bites because the
        # socket lane holds a drain open far longer than a client will wait,
        # and a test that shortens the floor cannot see it.
        drain_poll_interval_seconds=0.01,
    ) as handle:
        with client(handle, name="watchdog") as (connection, _reply):
            connection.send({"id": "slow-1", "argv": ["harness", "status", "--json"]})
            assert started.wait(WAIT)

            connection.send({"op": "drain", "force": True, "deadline_seconds": 120.0})
            assert _read_until(connection, "draining")["pending"] == 1

            # THE ASSERTION. Before the fix this read ran until the client's
            # own timeout and raised, because the next socket-visible frame was
            # the terminal one — up to two minutes away.
            progress = _read_until(connection, "drain_progress")
            assert progress["pending"] == 1
            assert progress["request_ids"] == ["conn-1:slow-1"]
            assert progress["drain_ms"] >= 0

            # ...and it keeps arriving, so a watchdog watching a long drain
            # sees liveness repeatedly rather than once.
            again = _read_until(connection, "drain_progress")
            assert again["drain_ms"] >= progress["drain_ms"]

            release.set()
            assert _read_until(connection, "exit")["id"] == "slow-1"
