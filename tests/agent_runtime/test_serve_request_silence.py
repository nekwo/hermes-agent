"""A serve request that goes quiet must SAY SO, on the lane that asked.

Measured 2026-08-27 on the live serve: an authenticated socket connection sent
``{"id":…,"argv":["harness","characters","list","--json"]}`` and read ZERO
frames for over 120 seconds; a fresh connection minutes later got the identical
argv complete, exit 0, in ~6s. Same pid, same home, no restart in between. The
client had no way to tell "queued behind four chat turns" from "the handler
wedged" from "the service died" — all three are the same silence on the wire.

THREE defects are pinned here. DEFECT C is the MECHANISM — what actually ate
the pool's workers — and A and B are why it was undiagnosable from the wire.

DEFECT A — an argv request emits its FIRST frame only when its handler writes
    one. ``_handle_message`` registers the request and calls ``pool.submit``;
    with ``DEFAULT_POOL_SIZE == 4`` and mission-chat turns holding a worker for
    the length of an LLM turn, a fifth request sits in the executor's queue
    producing nothing. Nothing anywhere reports that state — not the request's
    own sink, not the liveness pump, not the ``busy`` frame (which carries a
    count and no ids). A queued request is byte-identical, on the wire, to a
    hung one.

DEFECT B — the anti-silence liveness pump never reached the socket lane. Its
    own comment names the launcher's stream watchdog ("no frames for N
    seconds") as the thing it exists to satisfy, and it emits to ``frames`` —
    stdout — only. The drain path learned this lesson already and broadcasts
    its progress frame through ``_broadcast_lanes`` with a comment saying why:
    "the socket client IS such a watchdog: it reads with a finite timeout and
    reports ``transport_failed`` on silence." The pump was left behind. A
    socket client attached to a serve that is busy for minutes therefore reads
    nothing at all, and cannot distinguish a working service from a dead one.

Both are pinned at the ``serve_loop`` seam with injected streams — the same
seam the drain accounting tests use — because that is where they reproduce.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import pytest

from hermes_cli.harness_parts import serve as serve_module
from hermes_cli.harness_parts.serve import serve_loop

WAIT = 20.0


class _Pipe:
    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()

    def __iter__(self) -> "_Pipe":
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
            except json.JSONDecodeError:
                continue
        return rows

    def wait_for_frame(self, match, timeout: float = WAIT, what: str = "frame") -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for frame in self.frames():
                if match(frame):
                    return frame
            time.sleep(0.01)
        raise AssertionError(f"no {what} within {timeout}s; saw {self.frames()!r}")

    def wait_for(self, event: str, timeout: float = WAIT) -> dict:
        return self.wait_for_frame(
            lambda frame: frame.get("event") == event, timeout, repr(event)
        )


class _Hog:
    """A dispatch that parks on ``harness block`` and answers anything else."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Semaphore(0)

    def __call__(self, argv: list[str]) -> int:
        if len(argv) > 1 and argv[1] == "block":
            self.started.release()
            self.release.wait(WAIT)
        return 0

    def wait_started(self, count: int) -> None:
        for _ in range(count):
            assert self.started.acquire(timeout=WAIT), "hog never reached dispatch"


def _serve(pipe: _Pipe, sink: _Sink, **kwargs) -> dict:
    result: dict = {}

    def _go() -> None:
        result["code"] = serve_loop(pipe, sink, **kwargs)

    thread = threading.Thread(target=_go, name="serve-under-test", daemon=True)
    thread.start()
    result["thread"] = thread
    return result


@pytest.fixture()
def impatient(monkeypatch):
    """Shrink the silence budget so the test costs milliseconds, not seconds.

    Read from the module at call time on purpose — the same seam
    ``_DRAIN_EXIT_DEADLINE_SECONDS`` uses, and for the same reason.
    """

    monkeypatch.setattr(serve_module, "_REQUEST_SILENCE_SECONDS", 0.05)


# ── DEFECT A ────────────────────────────────────────────────────────────────


def test_a_request_queued_behind_a_full_pool_reports_itself_as_queued(impatient):
    """The measured shape, shrunk: the pool is full and the next request waits.

    Before the fix this asserted on an empty frame list — the queued request
    produced NOTHING until a worker freed up, which on the live serve was over
    two minutes.
    """

    hog = _Hog()
    pipe, sink = _Pipe(), _Sink()
    result = _serve(
        pipe,
        sink,
        dispatch=hog,
        pool_size=1,
        liveness_pump_interval_seconds=0.05,
    )
    sink.wait_for("ready")
    pipe.send({"id": "hog-1", "argv": ["harness", "block"]})
    hog.wait_started(1)

    pipe.send({"id": "quiet", "argv": ["harness", "characters", "list", "--json"]})
    progress = sink.wait_for_frame(
        lambda frame: frame.get("id") == "quiet"
        and frame.get("event") == "request_progress",
        what="request_progress for the queued request",
    )
    # WHICH state it is in is the whole point: "queued" says the pool is the
    # problem and the handler has not been entered, so no side effect has run.
    assert progress["state"] == "queued"
    assert progress["waited_ms"] >= 0
    assert progress["pending"] >= 2
    assert progress["pool_size"] == 1

    hog.release.set()
    sink.wait_for_frame(
        lambda frame: frame.get("id") == "quiet" and frame.get("event") == "exit",
        what="the queued request's exit",
    )
    pipe.send({"op": "shutdown"})
    result["thread"].join(WAIT)


def test_a_running_request_that_writes_nothing_is_reported_as_running(impatient):
    """The other half of the diagnosis: entered the handler, produced no output.

    A client that sees ``state: "running"`` knows the queue is not the problem
    and that side effects may already have landed — which is the difference
    between retrying and not.
    """

    hog = _Hog()
    pipe, sink = _Pipe(), _Sink()
    result = _serve(
        pipe,
        sink,
        dispatch=hog,
        pool_size=2,
        liveness_pump_interval_seconds=0.05,
    )
    sink.wait_for("ready")
    pipe.send({"id": "slow-1", "argv": ["harness", "block"]})
    hog.wait_started(1)

    progress = sink.wait_for_frame(
        lambda frame: frame.get("id") == "slow-1"
        and frame.get("event") == "request_progress",
        what="request_progress for the running request",
    )
    assert progress["state"] == "running"

    hog.release.set()
    sink.wait_for_frame(
        lambda frame: frame.get("id") == "slow-1" and frame.get("event") == "exit",
        what="the running request's exit",
    )
    pipe.send({"op": "shutdown"})
    result["thread"].join(WAIT)


def test_a_prompt_request_is_never_given_a_progress_frame():
    """The cost of the fix on the normal path is ZERO frames.

    ``harness status --json`` is polled on a fixed cadence by the launcher. A
    progress frame on every one of those would be a new per-poll cost paid to
    diagnose a case that is not happening.
    """

    pipe, sink = _Pipe(), _Sink()
    result = _serve(
        pipe,
        sink,
        dispatch=lambda argv: 0,
        pool_size=2,
        liveness_pump_interval_seconds=0.02,
    )
    sink.wait_for("ready")
    pipe.send({"id": "fast", "argv": ["harness", "status", "--json"]})
    sink.wait_for_frame(
        lambda frame: frame.get("id") == "fast" and frame.get("event") == "exit",
        what="the fast request's exit",
    )
    time.sleep(0.2)  # several pump ticks after it finished
    assert [
        frame
        for frame in sink.frames()
        if frame.get("event") == "request_progress"
    ] == []
    pipe.send({"op": "shutdown"})
    result["thread"].join(WAIT)


# ── DEFECT B ────────────────────────────────────────────────────────────────


def test_the_busy_liveness_frame_reaches_an_attached_socket_client(impatient):
    """The socket client is a watchdog, and the pump never spoke to it.

    Before the fix the ``busy`` frames went to stdout only: an attached socket
    client read nothing at all while the serve worked, which is precisely the
    ">120 s of zero frames" the field notes recorded.
    """

    from tests.agent_runtime.test_serve_socket_lane import client, running_serve

    hog = _Hog()
    with running_serve(
        dispatch=hog, pool_size=1, liveness_pump_interval_seconds=0.05
    ) as handle:
        with client(handle) as (connection, hello):
            assert hello.get("event") == "hello_ok"
            connection.send({"id": "hog-1", "argv": ["harness", "block"]})
            hog.wait_started(1)

            seen: list[dict] = []
            busy = None
            for _ in range(60):
                frame = connection.read_frame()
                if frame is None:
                    break
                seen.append(frame)
                if frame.get("event") == "busy":
                    busy = frame
                    break
            assert busy is not None, f"no busy frame reached the socket; saw {seen!r}"
            assert busy["pending"] >= 1
            hog.release.set()


# ── DEFECT C — the mechanism the two above make visible ─────────────────────


def test_a_dropped_connection_releases_the_worker_its_stream_was_holding():
    """The pool is four wide and ``harness stream`` never returns.

    ``request_control``'s own module docstring states the contract — the stream
    handler "must release its worker when its consumer disconnects" — and only
    the explicit ``{"op":"cancel"}`` ever set the event. A client that died,
    rather than reconnecting and cancelling politely, left its stream running
    on a pool worker for the life of the process; the ``cancel`` branch's
    comment prices that at "four watchdog cycles exhaust the entire serve
    pool".

    Before the fix ``pending`` stayed at 1 forever after the socket closed, and
    the queued request below never ran.
    """

    from tests.agent_runtime.test_serve_socket_lane import client, running_serve

    from agent_runtime.request_control import request_cancelled

    entered = threading.Semaphore(0)

    def _dispatch(argv: list[str]) -> int:
        if len(argv) > 1 and argv[1] == "stream":
            entered.release()
            deadline = time.monotonic() + WAIT
            while time.monotonic() < deadline:
                if request_cancelled():
                    return 130
                time.sleep(0.01)
            raise AssertionError("stream was never cancelled")
        return 0

    with running_serve(dispatch=_dispatch, pool_size=1) as handle:
        with client(handle) as (connection, hello):
            assert hello.get("event") == "hello_ok"
            connection.send({"id": "watch-1", "argv": ["harness", "stream"]})
            assert entered.acquire(timeout=WAIT), "stream never reached dispatch"
        # ``client`` closed the socket on the way out of the block: the consumer
        # is gone and nothing will ever read this stream again.

        # The ONLY worker comes back, which is provable from stdio: a fresh
        # request runs to exit. Before the fix it queued behind the abandoned
        # stream and produced nothing.
        handle.pipe.send({"id": "after", "argv": ["harness", "status", "--json"]})
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            if any(
                frame.get("id") == "after" and frame.get("event") == "exit"
                for frame in handle.sink.frames()
            ):
                break
            time.sleep(0.02)
        else:
            raise AssertionError(
                "the request that needed the reclaimed worker never ran; "
                f"frames were {handle.sink.frames()!r}"
            )
