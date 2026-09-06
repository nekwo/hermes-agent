"""L-h: the runtime's lifetime stops being its stdin's.

The claim this file pins is a LIFETIME, so every test here runs the real
``serve_loop`` with the real socket lane against the isolated runtime root the
autouse fixture provides, and drives it through the two events that used to be
the same event: a stdio ``shutdown`` (an order) and stdin EOF (an observation).
Nothing is faked — the drain arrives on a real loopback socket, from a real
authenticated client, exactly as ``harness serve connect --drain`` sends it.

Why in-process AND in the child e2e file
----------------------------------------

``test_serve_socket_child_e2e.py`` proves the same four outcomes against real
spawned processes, which is the only place "stdin was CLOSED at spawn" and "a
second starter loses the OS lock" are real rather than arranged. That file costs
a cold interpreter boot per arm. This one holds the seam: it can park the loop,
look at what the stdio sink stopped receiving, and settle the drain in
milliseconds. Neither replaces the other, and the e2e file says so too.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from contextlib import contextmanager

import pytest

from agent_runtime.serve_auth import read_token
from agent_runtime.serve_registry import list_serve_instances
from agent_runtime.serve_socket import ServeSocketClient, SocketOwnerLock
from hermes_cli.harness_parts import serve as serve_module
from hermes_cli.harness_parts.serve import serve_loop

WAIT = 20.0


class _StdioPipe:
    """The starter's pipe: an iterable the test feeds lines into and closes."""

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
        """EOF — which is what "the starter detached" looks like from here."""

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

    def frames(self) -> list[dict]:
        with self._lock:
            raw = "".join(self._parts)
        rows = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:  # a partially flushed tail
                continue
        return rows

    def events(self) -> list[str]:
        return [frame.get("event") for frame in self.frames()]

    def wait_for(self, event: str, timeout: float = WAIT) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for frame in self.frames():
                if frame.get("event") == event:
                    return frame
            time.sleep(0.01)
        raise AssertionError(f"no {event!r} frame within {timeout}s")


class _Serve:
    def __init__(self, pipe: _StdioPipe, sink: _Sink) -> None:
        self.pipe = pipe
        self.sink = sink
        self.thread: threading.Thread | None = None
        self.ready: dict = {}
        self.code: int | None = None

    @property
    def port(self) -> int:
        return int(self.ready["socket"]["port"])


@contextmanager
def _serve(**kwargs):
    """A live serve, torn down at the end of the block whatever state it is in."""

    handle = _Serve(_StdioPipe(), kwargs.pop("sink", None) or _Sink())

    def _run() -> None:
        handle.code = serve_loop(
            handle.pipe,
            handle.sink,
            socket_lane=kwargs.pop("socket_lane", True),
            dispatch=kwargs.pop("dispatch", lambda argv: 0),
            liveness_pump_interval_seconds=kwargs.pop(
                "liveness_pump_interval_seconds", 60.0
            ),
            **kwargs,
        )

    thread = threading.Thread(target=_run, name="serve-service-under-test", daemon=True)
    handle.thread = thread
    thread.start()
    try:
        handle.ready = handle.sink.wait_for("ready")
        yield handle
    finally:
        # A parked service ignores a closed pipe — that is the whole feature —
        # so teardown asks it to stop the way an operator would, and only then
        # closes the pipe for the non-service arms.
        if thread.is_alive():
            try:
                _drain_over_socket(handle)
            except Exception:  # noqa: BLE001 - teardown, never the assertion
                pass
        handle.pipe.close()
        thread.join(WAIT)


@contextmanager
def _client(handle: _Serve, *, name: str = "service-test-client"):
    from agent_runtime import paths

    token = read_token(paths.store_root()) or ""
    connection = ServeSocketClient("127.0.0.1", handle.port, timeout_seconds=WAIT)
    connection.connect()
    try:
        reply = connection.hello(token=token, client=name, client_build=None)
        yield connection, reply
    finally:
        connection.close()


def _read_until(connection: ServeSocketClient, *events: str, limit: int = 200) -> dict:
    wanted = set(events)
    for _ in range(limit):
        frame = connection.read_frame()
        if frame is None:
            raise AssertionError(f"connection closed before any of {sorted(wanted)}")
        if frame.get("event") in wanted:
            return frame
    raise AssertionError(f"none of {sorted(wanted)} within {limit} frames")


def _drain_over_socket(handle: _Serve) -> dict:
    """The operator's stop verb, exactly as ``serve connect --drain`` sends it."""

    with _client(handle, name="drainer") as (connection, hello_ok):
        assert hello_ok.get("event") == "hello_ok", hello_ok
        connection.send({"op": "drain", "force": True})
        return _read_until(connection, "drain_complete", "drain_timeout")


def _wait_for_detach(handle: _Serve, timeout: float = WAIT) -> dict:
    """Wait for the detach receipt on the STDIO lane, where it always lands.

    A socket client attached at the moment of detach also gets it as a frame,
    but a test that closes the pipe with nobody attached has only this one — and
    that is the state that matters most (it is exactly what a launcher closing
    leaves behind), so it gets a waiter rather than a sleep.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for frame in handle.sink.frames():
            if frame.get("event") != "stderr":
                continue
            try:
                payload = json.loads(frame.get("line") or "")
            except (json.JSONDecodeError, TypeError):
                continue
            if payload.get("event") == "stdio_owner_detached":
                return payload
        time.sleep(0.01)
    raise AssertionError(f"no stdio_owner_detached receipt within {timeout}s")


def _instances():
    from agent_runtime import paths

    return list_serve_instances(paths.store_root())


# ── the unchanged half ──────────────────────────────────────────────────────


def test_without_service_stdin_eof_still_ends_the_runtime():
    """The regression twin of everything below, and the reason it is first.

    ``--service`` is a lever, not a change of default. With it off, EOF is what
    it has always been: the reader ends, the loop emits ``shutdown``, the
    registry entry goes, and the code is 0.
    """

    with _serve() as handle:
        assert handle.ready["service"] is False
        handle.pipe.close()
        handle.thread.join(WAIT)

        assert handle.thread.is_alive() is False
        assert handle.sink.events()[-1] == "shutdown"
        assert handle.code == 0
        assert _instances() == []


# ── the lifetime ────────────────────────────────────────────────────────────


def test_a_service_survives_stdin_eof_and_keeps_serving_the_socket():
    """The finding, stated as a test.

    Before this, the thread that read stdin WAS the runtime: EOF joined the
    pool, closed both socket lanes and removed the registry entry, so a launcher
    closing its end of the pipe took the runtime with it. Here the same EOF
    produces a receipt and nothing else — and the proof that "nothing else" is
    true is that an argv request answers over the socket AFTERWARDS.
    """

    with _serve(service=True) as handle:
        assert handle.ready["service"] is True
        boot_id = handle.ready["boot_id"]

        with _client(handle) as (connection, hello_ok):
            assert hello_ok["service"] is True

            # THE EVENT. The starter closes its end and walks away.
            handle.pipe.close()

            detached = _read_until(connection, "stdio_owner_detached")
            assert detached["boot_id"] == boot_id
            assert detached["pid"] == handle.ready["pid"]

            # The runtime did not end...
            time.sleep(0.2)
            assert handle.thread.is_alive() is True
            assert handle.code is None

            # ...and it is still SERVING, which is the claim. An argv request
            # over the socket is answered by the pool the EOF used to join.
            connection.send({"id": "after-eof", "argv": ["harness", "status"]})
            answer = _read_until(connection, "exit")
            assert answer["id"] == "after-eof"
            assert answer["code"] == 0

            # The registry row is still there for an attaching client to find,
            # port and all. Its CLASSIFICATION is deliberately not asserted
            # here: this loop is running inside pytest, so the registry's
            # command-line check reads a pytest argv and answers ``unknown``,
            # which is the fail-safe direction working correctly on a process
            # that genuinely is not a ``hermes serve``. Live classification is
            # proven where the process really is one — the child e2e file, via
            # ``serve connect --probe``'s ``target.classification``.
            rows = [row for row in _instances() if row.get("boot_id") == boot_id]
            assert len(rows) == 1
            assert rows[0]["service"] is True
            assert rows[0]["port"] == handle.port


def test_the_detached_service_writes_nothing_more_to_the_starters_pipe():
    """The other half of the swap, and the one a live pipe would hide.

    A parked service that kept writing frames into a pipe whose reader has gone
    is one full pipe buffer away from blocking forever — the failure would look
    like a wedged runtime and would be nowhere near the write that caused it. So
    the detach receipt is the LAST thing that lane ever carries.

    It arrives there as the ordinary ``stderr`` line frame every service-log
    line rides (correlatable by ``boot_id`` against ``ready``, which is the
    channel's whole contract); the same payload reaches attached socket clients
    as a frame of its own, which is what the previous test reads.
    """

    with _serve(service=True) as handle:
        with _client(handle) as (connection, _hello_ok):
            handle.pipe.close()
            _read_until(connection, "stdio_owner_detached")
            before = handle.sink.frames()
            assert before[-1]["event"] == "stderr"
            assert json.loads(before[-1]["line"]) == {
                "event": "stdio_owner_detached",
                "pid": handle.ready["pid"],
                "boot_id": handle.ready["boot_id"],
                "starter_pid": os.getppid(),
            }

            # Work that WOULD have produced stdio frames on an attached serve.
            connection.send({"id": "quiet-1", "argv": ["harness", "status"]})
            assert _read_until(connection, "exit")["id"] == "quiet-1"
            handle.pipe.send({"op": "ping"})  # into a closed pipe: never read

            time.sleep(0.2)
            assert handle.sink.frames() == before


def test_a_drain_over_the_socket_ends_a_detached_service_and_unregisters_it():
    """The stop verb, from outside, on a runtime with no stdio owner left.

    This is the one that could not exist before: over stdio the drain ends the
    only connection that could observe it, and a detached service has no stdio
    connection at all. The park's wakeup and the drain's terminal frame are the
    same act, and the finalization that follows is the untouched one.
    """

    with _serve(service=True) as handle:
        boot_id = handle.ready["boot_id"]
        # Detached with NOBODY attached — the state the launcher actually
        # leaves behind when it closes, and the one where a missed wakeup
        # would strand the process forever with no client to notice.
        handle.pipe.close()
        _wait_for_detach(handle)

        terminal = _drain_over_socket(handle)
        assert terminal["event"] == "drain_complete"

        handle.thread.join(WAIT)
        assert handle.thread.is_alive() is False
        # Same exit code the stdio path returns, from the same finalization.
        assert handle.code == 0
        assert [row for row in _instances() if row.get("boot_id") == boot_id] == []


def test_a_stdio_shutdown_before_eof_still_ends_a_service_exactly_as_today():
    """An ORDER is not an observation, and ``--service`` does not blur them.

    A caller that still owns the pipe keeps its verb: this is what lets Update /
    Repair keep the behaviour they already rely on while the same binary gains
    the detached lifetime.
    """

    with _serve(service=True) as handle:
        boot_id = handle.ready["boot_id"]
        handle.pipe.send({"op": "shutdown"})
        handle.thread.join(WAIT)

        assert handle.thread.is_alive() is False
        assert handle.code == 0
        # The shutdown frame reaches the pipe, because the owner never detached.
        assert handle.sink.events()[-1] == "shutdown"
        assert "stdio_owner_detached" not in handle.sink.events()
        assert [row for row in _instances() if row.get("boot_id") == boot_id] == []


# ── the lock loser (L-h item 2) ─────────────────────────────────────────────


def test_a_service_that_loses_the_ownership_lock_serves_nothing_and_exits_zero():
    """F1's explicit requirement: never an extra stdio executor.

    A STDIO serve that loses this race keeps serving stdio, and that is
    unchanged — it has an owner waiting on a pipe. A SERVICE that lost it has
    nobody: it would be a second execution process against one store that
    nothing discovers, nothing drains and nothing knows to stop. So it names the
    winner and leaves, before a pool, a registry row or a ready frame exists.

    The lock is held by THIS process for the duration, which is a real OS lock
    contending exactly as two processes contend (verified: a second handle on
    the same root answers ``lock_held_by``).
    """

    from agent_runtime import paths

    root = paths.store_root()
    incumbent = SocketOwnerLock(root)
    assert incumbent.acquire().acquired is True
    incumbent.publish_owner(
        {
            "pid": os.getpid(),
            "boot_id": "incumbent-boot",
            "host": "127.0.0.1",
            "port": 4242,
            "started_at": "2026-09-05T00:00:00.000Z",
            "store_root": str(root),
        }
    )
    sink = _Sink()
    try:
        code = serve_loop(
            iter(()),
            sink,
            socket_lane=True,
            service=True,
            dispatch=lambda argv: 0,
            liveness_pump_interval_seconds=60.0,
        )
    finally:
        incumbent.release()

    assert code == 0
    events = sink.events()
    # It never got as far as a runtime: no ready frame, and no registry row.
    assert "ready" not in events
    assert events[-1] == "serve_owner_exists"
    assert _instances() == []

    exists = sink.frames()[-1]
    # The WINNER's coordinates, so the caller can attach instead of retrying.
    assert exists["pid"] == os.getpid()
    assert exists["port"] == 4242
    assert exists["socket"]["outcome"] == "lock_held_by"


def test_a_stdio_serve_that_loses_the_lock_still_serves_stdio():
    """The pre-existing contract, pinned beside the new one.

    The change above must be reachable ONLY through ``--service``. Without it, a
    lock loser degrades exactly as it always has: stdio keeps working, the ready
    frame says ``lock_held_by``, and the process is a normal serve.
    """

    from agent_runtime import paths

    root = paths.store_root()
    incumbent = SocketOwnerLock(root)
    assert incumbent.acquire().acquired is True
    try:
        with _serve() as handle:
            assert handle.ready["socket"]["outcome"] == "lock_held_by"
            handle.pipe.send({"id": "stdio-1", "argv": ["harness", "status"]})
            assert handle.sink.wait_for("exit")["id"] == "stdio-1"
    finally:
        incumbent.release()


# ── the published fields (L-h item 3) ───────────────────────────────────────


@pytest.mark.parametrize("service", [False, True])
def test_every_greeting_frame_and_the_registry_row_state_the_service_fields(service):
    """Always present, on all three frames and the row, and they agree.

    Present-and-false is a different fact from ABSENT: the first says "this
    runtime dies with its starter", the second says "this runtime predates
    service mode", and a client deciding whether to attach or to spawn has to
    tell them apart. ``starter_pid`` is read at boot for the reason the registry
    docstring gives — a detached service is reparented the moment its starter
    exits, so a value read later names somebody else.
    """

    with _serve(service=service) as handle:
        ready = handle.ready
        assert ready["service"] is service
        assert ready["starter_pid"] == os.getppid()
        assert ready["ops"]["service"] is service
        # The contract integer is untouched: this is an added key.
        assert ready["ops"]["contract"] == serve_module.OPS_CONTRACT_VERSION

        row = [r for r in _instances() if r.get("boot_id") == ready["boot_id"]][0]
        assert row["service"] is service
        assert row["starter_pid"] == os.getppid()

        with _client(handle) as (connection, hello_ok):
            assert hello_ok["service"] is service
            assert hello_ok["starter_pid"] == os.getppid()
            assert hello_ok["ops"]["service"] is service

            connection.send({"op": "version"})
            version = _read_until(connection, "version")
            assert version["service"] is service
            assert version["starter_pid"] == os.getppid()
            assert version["ops"]["service"] is service


def test_the_ops_manifest_advertises_service_on_every_transport():
    """RL-2's fallback condition is a MEMBERSHIP test, so the key is universal.

    The launcher decides "stdio pipes or attach-first" by asking whether this
    runtime's manifest has a ``service`` key at all. A key that appeared on one
    transport and not another would make that decision depend on which door the
    client happened to come through.
    """

    for transport in ("stdio", "socket", serve_module.GATEWAY_TRANSPORT):
        assert serve_module.ops_manifest(transport=transport)["service"] is False
        assert (
            serve_module.ops_manifest(transport=transport, service=True)["service"]
            is True
        )
        # …and nothing else moved.
        assert (
            serve_module.ops_manifest(transport=transport)["contract"]
            == serve_module.OPS_CONTRACT_VERSION
        )


def test_a_boot_prunes_revoked_device_rows_past_their_retention(tmp_path):
    """RL-23's floor, wired where the other two boot prunes are.

    Supersession keeps the row it revokes — that is what makes a column of dead
    credentials readable as one device's history — so something has to remove
    them eventually or the store grows for the life of the machine. Seeded here
    through the real ceremony rather than by writing JSON: the row a prune is
    allowed to delete is exactly the row a redeem and a revoke produce.
    """

    import time as _time

    from agent_runtime import paths
    from agent_runtime.serve_gateway_auth import (
        list_devices,
        mint_pairing_code,
        redeem_pairing_code,
        revoke_device,
    )

    root = paths.store_root()
    ancient = redeem_pairing_code(root, mint_pairing_code(root, now=0.0).code, now=1.0)
    revoke_device(root, ancient.device_id, now=2.0)
    now = _time.time()
    live = redeem_pairing_code(root, mint_pairing_code(root, now=now).code, now=now)
    assert {record.device_id for record in list_devices(root)} == {
        ancient.device_id,
        live.device_id,
    }

    with _serve() as handle:
        assert handle.ready["event"] == "ready"
        # The prune runs BEFORE the ready frame, beside the registry prunes, so
        # reading the store here is reading what the boot left.
        assert [record.device_id for record in list_devices(root)] == [live.device_id]
