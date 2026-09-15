"""RS-4: a contender WAITS for an owner that is leaving, bounded, and says how long.

The field (launcher plan ``restart-drain-fence.md`` §0, 2026-09-07 16:25Z on the
operator's PC): a build-behind restart drained the old runtime and, 14 s into
that drain, a replacement's ``acquire()`` answered
``serve_socket_lock_held_elsewhere lock_held_by:35944``. The old owner was alive
and it was LEAVING — its listener had already closed — but the OS lock is held
for the whole in-flight wait, and the loser has no way to tell "alive and
leaving" from "alive and serving". It ran stdio-only for the rest of the
session: no socket, no hub stream, no LAN listener.

So the refusal is right for one of those two shapes and wrong for the other, and
the sidecar is what separates them (RS-3 writes ``draining_at`` at drain start;
a runtime that has already dropped its register row is leaving too). This file
pins all three arms:

* the owner is leaving → the contender polls the OS lock and TAKES the lane when
  it frees, inside :data:`SOCKET_LOCK_DRAIN_WAIT_SECONDS`, and reports
  ``waited_for_drain_ms``;
* the owner is alive and NOT leaving (two serves on one root — the QA lane) →
  ``lock_held_by`` at once, exactly as before, and NOT one poll later;
* the bound expires → ``lock_held_by``, degrading exactly as today, carrying
  ``waited_for_drain_ms`` so the operator reads how long it gave the incumbent.

**The clock and the sleep are injected**, so a test of a 25 s bound costs
milliseconds and — the part a wall-clock test cannot do — the number of polls
and the elapsed time are the test's variables rather than the box's load.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from agent_runtime.serve_registry import serve_instance_path
from agent_runtime.serve_socket import (
    SOCKET_LOCK_DRAIN_POLL_SECONDS,
    SOCKET_LOCK_DRAIN_WAIT_SECONDS,
    SocketOwnerLock,
    socket_owner_path,
)


class _ScriptedClock:
    """A monotonic clock and a sleep that only MOVE it.

    The two are one object because the bound under test is elapsed time and the
    thing that advances it is the poll: a scripted sleep that did not move the
    clock would make the wait infinite, and a clock that moved on its own would
    make the poll count unobservable. ``sleeps`` is the record the assertions
    below are actually about — "it polled every 250 ms and gave up at the
    bound" is a statement about this list.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)
        self.sleeps: list[float] = []
        #: Called on each sleep with the clock's value AFTER it advanced, so a
        #: test can free the lock partway through the wait without a thread.
        self.on_sleep = None

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.now += float(seconds)
        if self.on_sleep is not None:
            self.on_sleep(len(self.sleeps))


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _publish(root: Path, record: dict) -> None:
    path = socket_owner_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def _register_row(root: Path, pid: int) -> None:
    """The owner's ``serve_instances/<pid>.json`` — presence means "serving"."""

    path = serve_instance_path(root, pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "transport": "stdio+socket"}), "utf-8")


# ── the arm the field needed ────────────────────────────────────────────────


def test_a_draining_owner_is_waited_out_and_the_lane_is_taken(tmp_path, live_foreign_pid):
    """The 16:25:23 line, inverted: the contender waits and wins the lane.

    The incumbent is a real held OS lock plus a sidecar naming a LIVE pid with
    ``draining_at`` on it — which is the exact on-disk state the operator's
    machine was in, and the state that used to produce ``lock_held_by`` on the
    first try.
    """

    root = _root(tmp_path)
    incumbent = SocketOwnerLock(root)
    assert incumbent.acquire().acquired is True
    _publish(
        root,
        {
            "pid": live_foreign_pid,
            "port": 61000,
            "started_at": "2026-09-07T16:25:09.771Z",
            "draining_at": "2026-09-07T16:25:09.780Z",
        },
    )
    _register_row(root, live_foreign_pid)

    clock = _ScriptedClock()
    # The incumbent lets go on the fourth poll — one second in, well inside the
    # bound, and long enough that a lock taken on the first try would prove
    # nothing.
    clock.on_sleep = lambda count: incumbent.release() if count == 4 else None

    contender = SocketOwnerLock(root, clock=clock, sleep=clock.sleep)
    try:
        result = contender.acquire()
    finally:
        incumbent.release()
        contender.release()

    assert result.acquired is True
    assert result.outcome == "acquired"
    assert result.took_over_from == live_foreign_pid
    assert result.waited_for_drain_ms == 1000
    # The BLOCK is what the launcher reads, not the object.
    block = result.payload()
    assert block["outcome"] == "acquired"
    assert block["took_over_from"] == live_foreign_pid
    assert block["waited_for_drain_ms"] == 1000
    assert clock.sleeps == [SOCKET_LOCK_DRAIN_POLL_SECONDS] * 4


def test_an_owner_that_dropped_its_register_row_counts_as_leaving(
    tmp_path, live_foreign_pid
):
    """The second half of RS-4's condition, and it is not redundant.

    ``draining_at`` is written by a serve that reached its drain op. A runtime
    that has already unregistered — the last statement of ``_finish_drain``
    before its ended note — is just as much on its way out, and a contender that
    demanded the sidecar word would refuse a lane whose owner has provably
    stopped advertising itself.
    """

    root = _root(tmp_path)
    incumbent = SocketOwnerLock(root)
    assert incumbent.acquire().acquired is True
    _publish(root, {"pid": live_foreign_pid, "port": 61000})
    assert not serve_instance_path(root, live_foreign_pid).exists()

    clock = _ScriptedClock()
    clock.on_sleep = lambda count: incumbent.release() if count == 2 else None
    contender = SocketOwnerLock(root, clock=clock, sleep=clock.sleep)
    try:
        result = contender.acquire()
    finally:
        incumbent.release()
        contender.release()

    assert result.acquired is True
    assert result.took_over_from == live_foreign_pid
    assert result.waited_for_drain_ms == 500


# ── the arm that must NOT change ────────────────────────────────────────────


def test_a_live_owner_that_is_not_leaving_is_refused_at_once(
    tmp_path, live_foreign_pid
):
    """Two serves on one root, the QA lane. Unchanged, byte for byte.

    The whole point of the lock is that "connect to the service for root X" has
    one answer, and a wait that fired against a HEALTHY owner would spend a QA
    serve's boot on a lock it is never going to get. Asserting ``sleeps == []``
    rather than only the outcome is the difference between "it refused" and "it
    refused without waiting" — and the second is the claim.
    """

    root = _root(tmp_path)
    incumbent = SocketOwnerLock(root)
    assert incumbent.acquire().acquired is True
    _publish(root, {"pid": live_foreign_pid, "port": 61001, "started_at": "x"})
    _register_row(root, live_foreign_pid)

    clock = _ScriptedClock()
    contender = SocketOwnerLock(root, clock=clock, sleep=clock.sleep)
    try:
        result = contender.acquire()
    finally:
        incumbent.release()

    assert result.outcome == "lock_held_by"
    assert result.pid == live_foreign_pid
    assert result.took_over_from is None
    assert result.waited_for_drain_ms is None
    assert "waited_for_drain_ms" not in result.payload()
    assert clock.sleeps == []


# ── the arm that expires ────────────────────────────────────────────────────


def test_a_wait_that_expires_degrades_as_today_and_says_how_long_it_gave(
    tmp_path, live_foreign_pid
):
    """A drain that never ends is still a lost lane — but a MEASURED one.

    Degrading exactly as today matters: the runtime has a job (stdio) and must
    not park on a lock forever. What is new is only the number, and the number
    is what tells an operator the difference between "it never even tried" and
    "it gave the incumbent 25 s".
    """

    root = _root(tmp_path)
    incumbent = SocketOwnerLock(root)
    assert incumbent.acquire().acquired is True
    _publish(
        root,
        {
            "pid": live_foreign_pid,
            "port": 61002,
            "started_at": "2026-09-07T16:25:09.771Z",
            "draining_at": "2026-09-07T16:25:09.780Z",
        },
    )
    _register_row(root, live_foreign_pid)

    clock = _ScriptedClock()
    lines: list[dict] = []
    contender = SocketOwnerLock(root, log=lines.append, clock=clock, sleep=clock.sleep)
    try:
        result = contender.acquire()
    finally:
        incumbent.release()

    assert result.outcome == "lock_held_by"
    assert result.acquired is False
    assert result.pid == live_foreign_pid
    assert result.took_over_from is None
    assert result.waited_for_drain_ms == int(SOCKET_LOCK_DRAIN_WAIT_SECONDS * 1000)
    assert result.payload()["waited_for_drain_ms"] == result.waited_for_drain_ms
    # It polled for the whole bound and not one lap longer.
    assert clock.sleeps == [SOCKET_LOCK_DRAIN_POLL_SECONDS] * int(
        SOCKET_LOCK_DRAIN_WAIT_SECONDS / SOCKET_LOCK_DRAIN_POLL_SECONDS
    )
    # And it is still one structured line, with the wait on it.
    stale = [row for row in lines if row["event"] == "serve_socket_owner_stale"]
    assert len(stale) == 1
    assert stale[0]["waited_for_drain_ms"] == result.waited_for_drain_ms


# ── the bound itself ────────────────────────────────────────────────────────


def test_the_bound_is_one_named_constant_above_the_launchers_drain_deadline():
    """25 s, named once, and larger than the 20 s deadline it is derived from.

    The launcher's ``drainDeadline`` is 20 s
    (`EterniaLauncher/lib/features/mission_control/data/mission_control_serve_session_io.dart`).
    A bound BELOW it would give up while the drain it is waiting for is still
    legitimately running, which is the failure this whole ruling exists to end;
    the margin is the point, so it is asserted rather than left to a comment.
    """

    assert SOCKET_LOCK_DRAIN_WAIT_SECONDS == 25.0
    assert SOCKET_LOCK_DRAIN_WAIT_SECONDS > 20.0
    assert SOCKET_LOCK_DRAIN_POLL_SECONDS == 0.25


def test_the_default_wait_uses_real_time_and_is_not_left_to_the_caller(tmp_path):
    """The seam exists FOR THE TESTS; production must not depend on it.

    A lock constructed the way ``serve.py`` constructs it — no clock, no sleep —
    has to carry the real ones, or the injected pair would be a second
    behaviour nobody runs.
    """

    lock = SocketOwnerLock(_root(tmp_path))
    assert lock._clock is time.monotonic
    assert lock._sleep is time.sleep
