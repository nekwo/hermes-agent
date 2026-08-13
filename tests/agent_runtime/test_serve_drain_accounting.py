"""Two defects in the drain lane, found by an independent review of slice 2 and
fixed here because slice 3 builds the socket drain on top of both.

FINDING A — a drain terminated by ``shutdown`` or by EOF exited SILENTLY.
    ``{"op":"shutdown"}`` and a closed pipe both break the reader loop, which
    then fell straight through to ``return drain_exit_code``: no
    ``drain_complete``, no ``drain_timeout``, no ``shutdown`` frame, the
    ``serve_instances`` entry left on disk, and exit code 0 even when the drain
    had timed out. A drain that exits without accounting for what it refused and
    what it completed is exactly the crash the drain exists to replace.

FINDING B — ``drain_complete.requests_completed`` could UNDER-COUNT.
    The pop from ``inflight`` and the completion increment were two separate
    critical sections, so a request could be invisible to both at once: gone
    from pending, not yet counted. The monitor, seeing an empty pending set,
    published a completion count lower than the number of exits it had actually
    let land. The counters are the drain's only evidence — an under-count reads
    to an operator as work the restart dropped.

Both are pinned at the ``serve_loop`` seam with injected streams, which is where
they were reproduced.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import pytest

from hermes_cli.harness_parts import serve as serve_module
from hermes_cli.harness_parts.serve import DRAIN_TIMEOUT_EXIT_CODE, serve_loop

WAIT = 20.0


class _Pipe:
    """A reader the test drives: send lines, then close (or just close = EOF)."""

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

    def wait_for(self, event: str, timeout: float = WAIT) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for frame in self.frames():
                if frame.get("event") == event:
                    return frame
            time.sleep(0.01)
        raise AssertionError(f"no {event!r} frame within {timeout}s")

    def events(self) -> list[str]:
        return [frame.get("event") for frame in self.frames()]


def _run_serve(pipe: _Pipe, sink: _Sink, **kwargs) -> dict:
    result: dict = {}

    def _go() -> None:
        result["code"] = serve_loop(
            pipe,
            sink,
            liveness_pump_interval_seconds=60.0,
            **kwargs,
        )

    thread = threading.Thread(target=_go, daemon=True)
    thread.start()
    result["thread"] = thread
    return result


def _instances():
    from agent_runtime import paths
    from agent_runtime.serve_registry import list_serve_instances

    return list_serve_instances(paths.store_root())


# ── FINDING A ───────────────────────────────────────────────────────────────


def test_a_shutdown_during_a_drain_still_publishes_a_terminal_frame():
    """Reproduced by the reviewer: request → drain → shutdown, with work in
    flight. The frames used to end at ``exit`` and the registry entry stayed."""

    def _dispatch(argv):
        time.sleep(0.3)
        return 0

    pipe, sink = _Pipe(), _Sink()
    result = _run_serve(pipe, sink, dispatch=_dispatch)
    sink.wait_for("ready")
    pipe.send({"id": "req-1", "argv": ["harness", "status"]})
    pipe.send({"op": "drain", "deadline_seconds": 10})
    sink.wait_for("draining")
    pipe.send({"op": "shutdown"})
    result["thread"].join(WAIT)

    events = sink.events()
    assert "exit" in events
    # The terminal frame the reviewer found missing.
    complete = sink.wait_for("drain_complete")
    assert complete["requests_completed"] == 1
    assert complete["requests_refused"] == 0
    assert result["code"] == 0
    # ...and the entry is gone, so the registry does not advertise a serve that
    # has exited.
    assert _instances() == []


def test_pipe_eof_during_a_drain_gets_the_same_terminal_treatment():
    def _dispatch(argv):
        time.sleep(0.3)
        return 0

    pipe, sink = _Pipe(), _Sink()
    result = _run_serve(pipe, sink, dispatch=_dispatch)
    sink.wait_for("ready")
    pipe.send({"id": "req-1", "argv": ["harness", "status"]})
    pipe.send({"op": "drain", "deadline_seconds": 10})
    sink.wait_for("draining")
    pipe.close()  # the launcher went away mid-drain
    result["thread"].join(WAIT)

    assert sink.wait_for("drain_complete")["requests_completed"] == 1
    assert result["code"] == 0
    assert _instances() == []


def test_a_drain_the_reader_outran_is_declared_abandoned_in_a_frame(monkeypatch):
    """When the monitor cannot publish in time, the loop says so ITSELF.

    Silence here was the defect: the process exited 0 with no terminal frame,
    which a supervisor reads as a clean drain. Now it emits ``drain_abandoned``
    and returns the same nonzero code a timeout does.
    """

    monkeypatch.setattr(serve_module, "_DRAIN_ABANDON_GRACE_SECONDS", 0.2)

    pipe, sink = _Pipe(), _Sink()
    result = _run_serve(
        pipe,
        sink,
        dispatch=lambda argv: time.sleep(0.05) or 0,
        # The monitor's poll is longer than the abandon grace, so it is still
        # sleeping when the reader unwinds — the race the fix has to survive.
        drain_poll_interval_seconds=5.0,
    )
    sink.wait_for("ready")
    pipe.send({"id": "req-1", "argv": ["harness", "status"]})
    pipe.send({"op": "drain", "deadline_seconds": 30})
    sink.wait_for("draining")
    pipe.send({"op": "shutdown"})
    result["thread"].join(WAIT)

    abandoned = sink.wait_for("drain_abandoned")
    assert abandoned["detail"]
    assert abandoned["requests_completed"] == 1
    assert result["code"] == DRAIN_TIMEOUT_EXIT_CODE
    assert _instances() == []
    # It must NOT claim a clean end it did not observe.
    assert "drain_complete" not in sink.events()
    assert "shutdown" not in sink.events()


def test_an_ordinary_shutdown_still_ends_with_the_shutdown_frame_and_code_zero():
    """The regression guard for the fix above: nothing changes when no drain is
    in progress."""

    pipe, sink = _Pipe(), _Sink()
    pipe.send({"op": "shutdown"})
    code = serve_loop(pipe, sink, dispatch=lambda argv: 0)

    assert code == 0
    assert sink.events()[-1] == "shutdown"
    assert "drain_abandoned" not in sink.events()
    assert _instances() == []


def test_a_blocking_wakeup_cannot_stop_the_forced_exit(monkeypatch):
    """Found live, against a real serve child, by this slice's own e2e probe.

    ``drain_wakeup`` closes the protocol descriptor the reader is parked on, and
    on Windows closing a handle with a synchronous read pending BLOCKS until
    that read returns. The forced exit used to sit behind that call, so a child
    that had published ``drain_complete``, released its socket lane and removed
    its registry entry then ran forever — a drained runtime that never exits is
    the hang the deadline exists to bound, arriving through the front door.
    """

    monkeypatch.setattr(serve_module, "_DRAIN_EXIT_DEADLINE_SECONDS", 0.2)

    exits: list[int] = []
    wakeup_entered = threading.Event()
    never = threading.Event()

    def _blocking_wakeup() -> None:
        wakeup_entered.set()
        never.wait(30.0)  # the close that does not return

    pipe, sink = _Pipe(), _Sink()
    # The pipe is never closed: the reader stays parked exactly as it does in a
    # real child, so nothing but the watchdog can end this.
    result = _run_serve(
        pipe,
        sink,
        dispatch=lambda argv: 0,
        drain_poll_interval_seconds=0.005,
        drain_wakeup=_blocking_wakeup,
        hard_exit=exits.append,
    )
    try:
        sink.wait_for("ready")
        pipe.send({"op": "drain", "deadline_seconds": 10})
        sink.wait_for("drain_complete")
        assert wakeup_entered.wait(WAIT)

        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline and not exits:
            time.sleep(0.01)
        assert exits == [0], "the drained runtime was never forced down"
    finally:
        never.set()
        pipe.close()
        result["thread"].join(WAIT)


# ── FINDING F6 — the teardown ran outside the deadline that bounds it ───────


class _StallingSink(_Sink):
    """A stdout that PARKS on one named frame, then lets go.

    Not an artificial hazard. Publishing the terminal frame, broadcasting it to
    attached clients and tearing the socket lane down are all writes against
    peers that may have stopped reading — a wedged subscriber parks a
    ``sendall`` for the whole socket timeout, and the hub's joins used to be a
    per-subscriber budget that SUMMED. This reproduces "the tail of the drain
    blocks" at the one seam a unit test can hold still.
    """

    def __init__(self, stall_on: str, release: threading.Event) -> None:
        super().__init__()
        self._stall_on = stall_on
        self._release = release
        self.entered = threading.Event()

    def write(self, text: str) -> int:
        if self._stall_on in text and not self.entered.is_set():
            self.entered.set()
            self._release.wait(30.0)
        return super().write(text)


def test_the_exit_watchdog_covers_the_teardown_not_just_what_follows_it(monkeypatch):
    """F6: the watchdog is armed as the FIRST act of the drain's ending.

    It used to be armed AFTER the terminal frame, the broadcast and the socket
    teardown — which is to say, after every step that can actually hang, so the
    steps most likely to need a watchdog ran unwatched. Here the very first of
    those steps parks forever. With the watchdog armed first, the process is
    still forced down on schedule; armed late, it is never armed at all.
    """

    monkeypatch.setattr(serve_module, "_DRAIN_EXIT_DEADLINE_SECONDS", 0.3)

    exits: list[int] = []
    release = threading.Event()
    pipe = _Pipe()
    sink = _StallingSink("drain_complete", release)
    result = _run_serve(
        pipe,
        sink,
        dispatch=lambda argv: 0,
        drain_poll_interval_seconds=0.005,
        hard_exit=exits.append,
    )
    try:
        sink.wait_for("ready")
        pipe.send({"op": "drain", "deadline_seconds": 10})
        # The drain has DECIDED how it ended and is now parked publishing it.
        assert sink.entered.wait(WAIT)
        # Positive proof the hang is where the test thinks it is: the terminal
        # frame has not been written, so nothing downstream of it has run.
        assert "drain_complete" not in sink.events()

        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline and not exits:
            time.sleep(0.01)
        assert exits == [0], (
            "the drain parked in its own teardown and was never forced down — "
            "the watchdog is armed too late to cover it"
        )
    finally:
        release.set()
        pipe.close()
        result["thread"].join(WAIT)


def test_a_completed_drain_is_never_relabelled_abandoned(monkeypatch):
    """F6's other half, and the contradiction it produced.

    A mid-drain EOF made the reader publish ``drain_abandoned`` and exit 3 —
    on top of a drain that had ALREADY decided it completed. The frame and the
    code were both wrong, about work that had actually landed. The terminal
    latch is taken before anything is published, so the two paths cannot both
    claim the ending.

    The stall below puts the process in exactly that window: the drain has
    latched and is mid-publish, and the reader reaches its EOF path first.
    """

    monkeypatch.setattr(serve_module, "_DRAIN_ABANDON_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(serve_module, "_DRAIN_EXIT_DEADLINE_SECONDS", 30.0)

    release = threading.Event()
    pipe = _Pipe()
    sink = _StallingSink("drain_complete", release)
    result = _run_serve(
        pipe, sink, dispatch=lambda argv: 0, drain_poll_interval_seconds=0.005
    )
    try:
        sink.wait_for("ready")
        pipe.send({"op": "drain", "deadline_seconds": 10})
        assert sink.entered.wait(WAIT)
        assert "drain_complete" not in sink.events()

        # The transport goes away while the drain is mid-publish.
        pipe.close()
        # Long enough for the abandon grace to lapse several times over.
        time.sleep(1.0)
        assert "drain_abandoned" not in sink.events(), (
            "a drain that had already decided it COMPLETED was relabelled "
            "abandoned by the reader"
        )
    finally:
        release.set()
        result["thread"].join(WAIT)

    # Released, the drain finishes publishing on its OWN thread — which the
    # join above does not cover, so this waits for the frame rather than
    # assuming the reader's exit dragged it along.
    sink.wait_for("drain_complete")
    assert "drain_abandoned" not in sink.events()
    # ...and the code is the drain's verdict, not the EOF path's.
    assert result["code"] == 0


# ── FINDING B ───────────────────────────────────────────────────────────────


def test_the_completion_count_cannot_miss_a_request_that_just_landed(monkeypatch):
    """The under-count, made deterministic.

    The old code popped the request from ``inflight`` and only THEN took the
    counter's lock, leaving a window in which the request was in neither. This
    test widens that window to 250ms: with the pop and the count in one critical
    section the monitor simply waits and reports 1; with them apart it observes
    an empty pending set first and publishes 0 — an operator reading "the
    restart dropped that request" about work that completed.
    """

    original = serve_module._DrainState.note_completed

    def _slow_note_completed(self):
        time.sleep(0.25)
        original(self)

    monkeypatch.setattr(
        serve_module._DrainState, "note_completed", _slow_note_completed
    )

    pipe, sink = _Pipe(), _Sink()
    result = _run_serve(
        pipe,
        sink,
        dispatch=lambda argv: time.sleep(0.1) or 0,
        drain_poll_interval_seconds=0.005,
    )
    sink.wait_for("ready")
    pipe.send({"id": "req-1", "argv": ["harness", "status"]})
    pipe.send({"op": "drain", "deadline_seconds": 30})
    sink.wait_for("draining")
    complete = sink.wait_for("drain_complete")
    pipe.close()
    result["thread"].join(WAIT)

    exits = [f for f in sink.frames() if f.get("event") == "exit"]
    assert len(exits) == 1
    assert complete["requests_completed"] == len(exits) == 1


@pytest.mark.parametrize("concurrency", [8])
def test_the_completion_count_matches_the_exits_under_concurrency(concurrency):
    """The shape the reviewer saw flake (5 reported for 8 exits), kept as a
    live-fire check next to the deterministic pin above."""

    pipe, sink = _Pipe(), _Sink()
    result = _run_serve(
        pipe,
        sink,
        dispatch=lambda argv: time.sleep(0.05) or 0,
        pool_size=concurrency,
        drain_poll_interval_seconds=0.005,
    )
    sink.wait_for("ready")
    for index in range(concurrency):
        pipe.send({"id": f"req-{index}", "argv": ["harness", "status"]})
    pipe.send({"op": "drain", "deadline_seconds": 30})
    complete = sink.wait_for("drain_complete")
    pipe.close()
    result["thread"].join(WAIT)

    exits = [f for f in sink.frames() if f.get("event") == "exit"]
    assert complete["requests_completed"] == len(exits)
