"""RS-3: the ORDER a draining serve leaves in, pinned as an order.

The field (launcher plan ``restart-drain-fence.md`` §0, 2026-09-07): a
replacement runtime lost the socket lock 14 s into the old one's drain, while
the old one's listener was already closed. Two questions fall out of that, and
they have different answers:

*Is the order wrong?*  No — read before this file was written, and re-read by
the assertions below: ``_finish_drain`` runs ``_close_socket_lane`` (whose last
act is ``lock.release()``) and only THEN ``_unregister_instance``. The order
RS-3 asks for is the order the code already runs, and this file exists so that
it stays the order: the release and the row-drop are two statements in one
function, one refactor apart from being swapped, and a swap would advertise a
serve in the registry that has already let go of the lane — a client that reads
the row, dials, and finds nobody.

*Then what was missing?*  The two facts that let a CONTENDER read the order from
outside the process. The sidecar did not say the owner was leaving, so RS-4's
wait had nothing to key on, and nothing on the service log marked the moment the
register row went — the drain's only visible boundary was the terminal frame,
which is published BEFORE the teardown it accounts for. Both are pinned here.

The seam is the real ``serve_loop`` with the real socket lane over the isolated
runtime root, because "the order" is a statement about that function's tail and
about nothing smaller. The steps are recorded by wrapping the four production
callables the tail invokes, which is the only way to observe a release — the
lock's departure is a kernel fact and emits no frame of its own.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import pytest

from agent_runtime import serve_socket as serve_socket_module
from agent_runtime import serve_registry as serve_registry_module
from hermes_cli.harness_parts import serve as serve_module
from hermes_cli.harness_parts.serve import serve_loop

WAIT = 25.0


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

    def service_log(self) -> list[dict]:
        """``_service_log`` writes JSON to stderr, which the loop frames.

        So a structured transport line arrives here as an ordinary
        ``{"event":"stderr","line":"{…}"}`` frame — the same shape
        ``test_serve_socket_lane`` reads them through.
        """

        rows = []
        for frame in self.frames():
            if frame.get("event") != "stderr":
                continue
            line = frame.get("line") or ""
            if not line.startswith("{"):
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


def _store_root():
    from agent_runtime import paths

    return paths.store_root()


@pytest.fixture
def drain_recorder(monkeypatch):
    """One ordered list of the four steps the drain's tail takes.

    Wrapping rather than sampling: the release and the unregister are adjacent
    statements, so any observation that polls could see them in either order on
    a slow box and the test would pin nothing. Each wrapper appends AFTER the
    real call returns, so the list is a record of completions.
    """

    steps: list[str] = []
    seen: dict = {}

    real_begin_drain = serve_socket_module.ServeSocketServer.begin_drain
    real_release = serve_socket_module.SocketOwnerLock.release
    real_unregister = serve_registry_module.unregister_serve_instance
    real_write_end = serve_module._ServeEndReason.write

    def begin_drain(self):
        # Read the sidecar BEFORE the listener closes: RS-3 wants the drain
        # word on disk from the first event on, so a contender that arrives
        # with the very first refused connection already reads "leaving".
        seen.setdefault("sidecar_at_drain_start", serve_socket_module.read_socket_owner(
            _store_root()
        ))
        result = real_begin_drain(self)
        steps.append("listener_closed")
        return result

    def release(self):
        result = real_release(self)
        steps.append("lock_released")
        return result

    def unregister(store_root):
        result = real_unregister(store_root)
        steps.append("row_unregistered")
        return result

    def write_end(self, reason=None):
        result = real_write_end(self, reason)
        steps.append("ended_note")
        return result

    monkeypatch.setattr(
        serve_socket_module.ServeSocketServer, "begin_drain", begin_drain
    )
    monkeypatch.setattr(serve_socket_module.SocketOwnerLock, "release", release)
    monkeypatch.setattr(
        serve_registry_module, "unregister_serve_instance", unregister
    )
    monkeypatch.setattr(serve_module._ServeEndReason, "write", write_end)
    # The end-reason recorder is armed below (it is what writes the ended note),
    # and arming it installs a process-wide console-control handler on Windows.
    # A unit test has no business leaving one on the pytest process.
    monkeypatch.setattr(
        serve_module, "_install_console_ctrl_reason_handler", lambda recorder: None
    )
    seen["steps"] = steps
    return seen


def _run_drain(recorder, *, dispatch=None):
    """Boot a real socket-lane serve, put work in flight, drain it, join."""

    pipe, sink = _Pipe(), _Sink()
    result: dict = {}

    def _go() -> None:
        result["code"] = serve_loop(
            pipe,
            sink,
            socket_lane=True,
            record_end_reason=True,
            dispatch=dispatch or (lambda argv: time.sleep(0.3) or 0),
            liveness_pump_interval_seconds=60.0,
        )

    thread = threading.Thread(target=_go, name="serve-drain-order", daemon=True)
    thread.start()
    ready = sink.wait_for("ready")
    assert ready["socket"]["outcome"] == "listening", ready["socket"]
    pipe.send({"id": "req-1", "argv": ["harness", "status"]})
    pipe.send({"op": "drain", "deadline_seconds": 20})
    sink.wait_for("draining")
    pipe.close()
    thread.join(WAIT)
    assert not thread.is_alive(), "the drain never returned"
    result["sink"] = sink
    result["ready"] = ready
    return result


def test_drain_releases_the_lock_before_it_drops_the_row(drain_recorder):
    """The order, end to end, as ONE list.

    listener closed → in-flight work drained → socket lock released → register
    row unregistered → ended note. Asserted as an equality on the whole
    sequence rather than as a pair of ``index() <`` comparisons, because the
    thing under protection is the sequence: a future teardown that grew a fifth
    step in the middle would slip past every pairwise check.
    """

    completed: list[str] = []

    def _dispatch(argv):
        time.sleep(0.3)
        completed.append("done")
        return 0

    run = _run_drain(drain_recorder, dispatch=_dispatch)
    steps = drain_recorder["steps"]

    assert run["code"] == 0
    # The work really was in flight when the drain began, or the order below is
    # a statement about an empty drain.
    complete = run["sink"].wait_for("drain_complete")
    assert complete["requests_completed"] == 1
    assert completed == ["done"]

    assert steps == [
        "listener_closed",
        "lock_released",
        "row_unregistered",
        "ended_note",
    ]
    # And the row really is gone, not merely reported gone.
    from agent_runtime.serve_registry import list_serve_instances

    assert list_serve_instances(_store_root()) == []


def test_the_sidecar_says_it_is_leaving_from_the_first_drain_event_on(drain_recorder):
    """RS-3's other half: ``draining_at`` on ``serve_socket.owner.json``.

    Without it a contender reading the sidecar sees a live pid and a port and
    concludes "serving" — which is precisely what the field's replacement
    concluded at 16:25:23 about a runtime whose listener had been closed for 14
    seconds. The stamp is written at drain START, before the listener closes,
    so there is no window in which the lane refuses connections while still
    advertising itself as healthy.
    """

    run = _run_drain(drain_recorder)
    sidecar = drain_recorder["sidecar_at_drain_start"]

    assert sidecar, "no owner sidecar at drain start"
    assert sidecar["pid"] == run["ready"]["pid"]
    # Still a usable discovery record: the drain stamp is ADDITIVE, and a
    # client mid-conversation still learns which port it is attached to.
    assert sidecar["port"] == run["ready"]["socket"]["port"]
    assert isinstance(sidecar.get("draining_at"), str)
    assert sidecar["draining_at"].endswith("Z")


def test_the_moment_the_register_row_goes_is_one_line_on_the_service_log(
    drain_recorder,
):
    """``serve_instance_unregistered`` — the new event, one line.

    The drain's terminal frame is published BEFORE the teardown it accounts
    for, so until this line existed nothing on the wire marked the instant the
    registry stopped advertising this runtime. That instant is the one a
    contender's ``lock_held_by`` has to be read against.
    """

    run = _run_drain(drain_recorder)
    rows = [
        row
        for row in run["sink"].service_log()
        if row.get("event") == "serve_instance_unregistered"
    ]

    assert len(rows) == 1, [r.get("event") for r in run["sink"].service_log()]
    row = rows[0]
    assert row["pid"] == run["ready"]["pid"]
    assert row["boot_id"] == run["ready"]["boot_id"]
    assert row["reason"] == "drain"
    assert row["path"].endswith(f"{run['ready']['pid']}.json")
