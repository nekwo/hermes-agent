"""The boot hydrate says it is building, instead of looking wedged (MC-4 / P6).

Measured on the operator's launcher 2026-08-18 (investigation A-x2): the boot
build took 29,560 ms and the lane emitted NOTHING for the whole of it. The
launcher's watchdog fired ``stream_teardown cause=liveness_deadline`` 0.67 s
before the authoritative frame arrived; that frame was delivered to a retired
request and discarded with no receipt, the launcher respawned, bought a second
full build, and the result landed as a same-offset duplicate. Three cores for
one paint. A delta batch's build has heartbeat through itself since
``_full_core_batch_frames`` shipped — the BOOT build, the longest one any
consumer ever waits on, was the one lane that stayed silent.

MC-5 fixed the launcher half (a stale first paint keeps the 120 s startup
budget). Either half alone would have prevented the 09:33 teardown; both are
right, and this is the half that makes a long build legible to ANY consumer
rather than to the one that was taught about it.

**Why nothing here sleeps to make a build slow.** Every wait is a bounded poll
on a CONDITION. The build is gated on a ``threading.Event`` the case releases,
and "long enough that a heartbeat was due" is proven by counting the wait loop's
own cancellation polls — not by a wall clock, and never by asserting an elapsed
number (ruling #60). The heartbeat interval is INJECTED at 0.05 s so the cadence
is a property of the case rather than of the machine.
"""

from __future__ import annotations

import logging
import re
import threading

import pytest

import agent_runtime.stream as stream_mod
from agent_runtime.request_control import request_cancel_scope
from agent_runtime.stream import stream_frames

_PREFIX = "snapshot_build "
_PAIR = re.compile(r"(?P<key>[a-z_]+)=(?P<value>\S+)")

#: Long enough that a 0.05s heartbeat cadence was due MANY times over — the wait
#: loop polls every ``_SNAPSHOT_CANCEL_POLL_SECONDS`` (0.1 s), so six polls is
#: ~0.6 s, i.e. about a dozen intervals. Counted, never timed.
_POLLS_PROVING_THE_CADENCE_WAS_DUE = 6

#: A cancellation is observed within one ``_SNAPSHOT_CANCEL_POLL_SECONDS`` (0.1 s),
#: during which a 0.05 s cadence can emit two or three frames. A generator that
#: instead waits out the held build emits that many every tenth of a second for
#: the gate's whole 20 s bound — some four hundred. Loose on purpose: the claim
#: is which of the two happened, and the gap between them is enormous.
_FRAMES_A_PROMPT_CANCELLATION_CAN_EMIT = 8


def _pairs(message: str) -> dict[str, str]:
    return {m["key"]: m["value"] for m in _PAIR.finditer(message[len(_PREFIX) :])}


def _build_lines(caplog) -> list[dict[str, str]]:
    return [
        _pairs(record.getMessage())
        for record in caplog.records
        if record.name == "agent_runtime.stream"
        and record.getMessage().startswith(_PREFIX)
    ]


class _GatedBuild:
    """``build_snapshot`` held open until the case lets it finish.

    ``started`` fires ON the build thread, so a releaser can wait for the build
    to actually be in flight rather than guessing that it is.
    """

    def __init__(self, monkeypatch) -> None:
        self.started = threading.Event()
        self.gate = threading.Event()
        self.calls: list[dict] = []
        real = stream_mod.build_snapshot

        def _gated(*args, **kwargs):
            self.calls.append(dict(kwargs))
            self.started.set()
            assert self.gate.wait(20), "the gated build was never released"
            return real(*args, **kwargs)

        monkeypatch.setattr(stream_mod, "build_snapshot", _gated)

    def release(self) -> None:
        self.gate.set()


class _PollCounter:
    """Counts the build-wait loop's cancellation probes.

    The instrument that lets a case say "a heartbeat was due" without saying how
    many milliseconds passed. It also proves the probe is THERE, which is what
    the cancellation case below turns into its own claim.
    """

    def __init__(self, monkeypatch) -> None:
        self.count = 0
        real = stream_mod.request_cancelled

        def _counting():
            self.count += 1
            return real()

        monkeypatch.setattr(stream_mod, "request_cancelled", _counting)

    def wait_for_cadence(self, gated: _GatedBuild) -> None:
        """Release ``gated`` once the wait loop has polled enough times."""

        def _run() -> None:
            assert gated.started.wait(20), "the boot build never started"
            deadline = threading.Event()
            while self.count < _POLLS_PROVING_THE_CADENCE_WAS_DUE:
                # Bounded poll on a CONDITION (the counter), not a fixed sleep
                # sized to the thing under test.
                if deadline.wait(0.01):  # pragma: no cover - never set
                    break
            gated.release()

        threading.Thread(target=_run, name="cadence-releaser", daemon=True).start()


def _drive(**kwargs) -> list[dict]:
    return list(
        stream_frames(poll_interval_seconds=0.01, heartbeat_interval_seconds=0.05, **kwargs)
    )


# --------------------------------------------------------------------------- #
# 1. A slow boot build yields liveness BEFORE its hydrate
# --------------------------------------------------------------------------- #
def test_a_slow_boot_build_emits_liveness_before_its_hydrate(
    isolate_agent_runtime_root, monkeypatch
):
    gated = _GatedBuild(monkeypatch)

    frames: list[dict] = []
    # max_frames=2, not 1: a one-shot SUPPRESSES boot liveness by design (see
    # the one-shot case below), so a one-frame budget here would assert about a
    # lane this case is not testing — and would deadlock on its own gate.
    for frame in stream_frames(
        poll_interval_seconds=0.01, heartbeat_interval_seconds=0.05, max_frames=2
    ):
        frames.append(frame)
        if frame["type"] == "heartbeat" and len(frames) >= 2:
            # Released only once liveness has DEMONSTRABLY arrived while the
            # build was held — the release is driven by the evidence, so a
            # generator that emitted nothing would hang the gate and fail loudly
            # rather than pass on an empty list.
            gated.release()
        if frame["type"] == "hydrate":
            break

    types = [frame["type"] for frame in frames]
    assert types[-1] == "hydrate", types
    heartbeats = [frame for frame in frames if frame["type"] == "heartbeat"]
    assert heartbeats, "the boot build emitted no liveness at all; it looks wedged"
    for beat in heartbeats:
        activity = beat["activity"]
        assert activity["kind"] == "snapshot_build", activity
        assert activity["state"] == "busy", activity
        # The same block ``_full_core_batch_frames`` ships, so a consumer has one
        # vocabulary for "a build is running" regardless of which lane pays.
        assert set(activity) == {"kind", "state", "elapsed_ms"}, activity
        assert "core" not in beat


def test_the_boot_heartbeat_carries_no_position_not_a_zero_one(
    isolate_agent_runtime_root, monkeypatch
):
    """Its own case, its own kill (C30).

    ``heartbeat_frame``'s contract is the authority: liveness without a position
    "must not be stamped ``0``", which every watermark-gated reader would take as
    a real cursor at the head of the log. A boot build has no APPLIED core, so
    unlike ``_full_core_batch_frames`` — which honestly advertises the last
    applied offset — the boot's only truthful answer is ``None``.
    """

    gated = _GatedBuild(monkeypatch)

    beats: list[dict] = []
    # max_frames=2, not 1: a one-shot SUPPRESSES boot liveness by design (see
    # the one-shot case below), so a one-frame budget here would assert about a
    # lane this case is not testing — and would deadlock on its own gate.
    for frame in stream_frames(
        poll_interval_seconds=0.01, heartbeat_interval_seconds=0.05, max_frames=2
    ):
        if frame["type"] == "heartbeat":
            beats.append(frame)
            if len(beats) >= 2:
                gated.release()
        if frame["type"] == "hydrate":
            break

    assert beats, "no boot heartbeat to inspect"
    for beat in beats:
        assert beat["watermark"]["event_offset"] is None, beat["watermark"]


# --------------------------------------------------------------------------- #
# 2. Ordering, and exactly one hydrate
# --------------------------------------------------------------------------- #
def test_every_heartbeat_precedes_the_one_hydrate_that_carries_the_built_core(
    isolate_agent_runtime_root, monkeypatch
):
    gated = _GatedBuild(monkeypatch)

    frames: list[dict] = []
    # max_frames=2, not 1: a one-shot SUPPRESSES boot liveness by design (see
    # the one-shot case below), so a one-frame budget here would assert about a
    # lane this case is not testing — and would deadlock on its own gate.
    for frame in stream_frames(
        poll_interval_seconds=0.01, heartbeat_interval_seconds=0.05, max_frames=2
    ):
        frames.append(frame)
        if frame["type"] == "heartbeat" and len(frames) >= 2:
            gated.release()
        if frame["type"] == "hydrate":
            break

    types = [frame["type"] for frame in frames]
    assert types.count("hydrate") == 1, types
    assert types.index("hydrate") == len(types) - 1, types
    assert set(types[:-1]) == {"heartbeat"}, types

    hydrate = frames[-1]
    # The frame carries the JOB's core, not a second build's: the gated build
    # ran exactly once, so a hydrate built after the drain would have hung.
    assert len(gated.calls) == 1, gated.calls
    assert hydrate["core"]["parity"]["watermark"] == hydrate["watermark"]


def test_the_boot_job_rides_an_inflight_build_instead_of_buying_a_second_one(
    isolate_agent_runtime_root, monkeypatch
):
    """The flag the move onto ``_SnapshotBuildJob`` would have dropped silently.

    ``hydrate_frame``'s own build passes ``accept_inflight=True`` — the serve
    prewarms a build right after ``ready``, and this frame is allowed to ride it
    because its watermark comes from the snapshot itself and the tail resumes
    from exactly that offset. ``_SnapshotBuildJob`` did NOT pass it, so moving
    the boot build onto the job as written would have made every boot wait for
    the prewarm and THEN pay a second full build — visible in the field only as
    a second ``snapshot_build_core role=led`` line, with the receipt still
    reading ``reason=hydrate``.

    Asserted on the ARGUMENT, because the cost it prevents is invisible in the
    frame: both a ride and a fresh build produce a correct hydrate.
    """

    gated = _GatedBuild(monkeypatch)
    gated.release()

    list(
        stream_frames(
            poll_interval_seconds=0.01, heartbeat_interval_seconds=60, max_frames=1
        )
    )

    assert gated.calls, "the boot never built"
    assert gated.calls[0].get("accept_inflight") is True, gated.calls[0]


# --------------------------------------------------------------------------- #
# 3. A one-shot still gets a CORE
# --------------------------------------------------------------------------- #
def test_a_one_shot_request_still_receives_a_core_not_a_heartbeat(
    isolate_agent_runtime_root, monkeypatch
):
    """The ``max_frames`` decision, pinned.

    ``stream_frames`` counts every yielded frame toward ``emitted``, so a boot
    heartbeat that counted would let a ``--max-frames 1`` request return having
    delivered NO core. That is not hypothetical for the consumer: the launcher's
    forced-refresh lane
    (``mission_control_bridge.dart::_loadSnapshotFromStreamHydrate``, read
    2026-08-18) scans that stdout for a ``type == "hydrate"`` line and silently
    returns null when it finds none — the refresh would no-op with no receipt.

    Chosen: boot liveness does NOT count toward the budget, and is suppressed
    outright for a one-shot so the frames such a consumer sees are exactly what
    it asked for. The alternative — suppress only, and count when present — was
    rejected because it makes the ONE lane most exposed to a command timeout the
    only lane that goes silent for thirty seconds.

    Non-vacuity is the whole difficulty here, and it is why the poll counter
    exists: the gate is held until the wait loop has polled enough times that a
    0.05 s cadence was due many times over. A generator emitting boot heartbeats
    would therefore have emitted several before this returns.
    """

    gated = _GatedBuild(monkeypatch)
    polls = _PollCounter(monkeypatch)
    polls.wait_for_cadence(gated)

    frames = _drive(max_frames=1)

    # THE CLAIM FIRST, the non-vacuity guard second, and the order is deliberate.
    # A mutant that counts boot liveness returns after its first heartbeat —
    # before the releaser has reached its poll target — so a guard asserted first
    # would red with "released too early" and hide the defect it caught.
    assert [frame["type"] for frame in frames] == ["hydrate"], [
        frame["type"] for frame in frames
    ]
    assert "core" in frames[0]
    assert polls.count >= _POLLS_PROVING_THE_CADENCE_WAS_DUE, (
        f"the build was released after only {polls.count} polls; a heartbeat was "
        "never due and this case cannot see whether one was suppressed"
    )


# --------------------------------------------------------------------------- #
# 4. Cancellation still lands mid-build
# --------------------------------------------------------------------------- #
def test_a_cancelled_request_returns_without_waiting_for_the_boot_build(
    isolate_agent_runtime_root, monkeypatch
):
    """The property the wait loop's probe exists for, at the boot lane.

    The build is NEVER released, so the generator must abandon it — the
    cooperative contract ``_SnapshotBuildJob`` documents ("a cancelled consumer
    does not wait for a finite build to finish").

    **"No hydrate" is not enough, and measuring that was the point.** Removing
    the probe from the wait loop left this case GREEN: the generator simply
    waited out the gate's own 20 s bound, saw the build fail, and returned
    empty — the right answer for the wrong reason, in twenty seconds. So the
    claim is a COUNT (ruling #60): the loop emits liveness every 0.05 s while it
    waits, so a generator that returns within a poll of the cancellation has
    emitted a HANDFUL of frames, and one that waits out the gate has emitted
    hundreds. The bound below is loose by two orders of magnitude on purpose —
    it is a claim about which of the two happened, not a latency budget.
    """

    gated = _GatedBuild(monkeypatch)
    stop = threading.Event()

    frames: list[dict] = []
    with request_cancel_scope(stop):

        def _cancel_once_building() -> None:
            assert gated.started.wait(20), "the boot build never started"
            stop.set()

        threading.Thread(target=_cancel_once_building, daemon=True).start()
        frames = _drive(max_frames=5)

    types = [frame["type"] for frame in frames]
    assert "hydrate" not in types, types
    assert set(types) <= {"heartbeat"}, types
    assert len(frames) <= _FRAMES_A_PROMPT_CANCELLATION_CAN_EMIT, (
        f"the generator emitted {len(frames)} frames before returning; it waited "
        "out the held build instead of observing the cancellation"
    )
    # The build is still held: abandoned, not drained.
    assert not gated.gate.is_set()


# --------------------------------------------------------------------------- #
# 5. The build receipt is unchanged, and reports the JOB's own measurement
# --------------------------------------------------------------------------- #
#: A value no clock can produce, so the assertion below is an IDENTITY — "the
#: receipt reports the number the build thread measured" — rather than a
#: duration compared against a threshold (ruling #60: never elapsed-ms).
_SENTINEL_ELAPSED_MS = 424242


def test_the_hydrate_receipt_reports_the_number_measured_on_the_build_thread(
    isolate_agent_runtime_root, monkeypatch, caplog
):
    """``waited_ms`` comes from ``job.elapsed_ms``, not from a timer here.

    ``_SnapshotBuildJob.elapsed_ms`` is taken ON the build thread precisely
    because the generator's own wait polls at ``_SNAPSHOT_CANCEL_POLL_SECONDS``
    and would round every build up by as much as 100 ms. A receipt measured
    around ``done.wait`` would still look plausible in the field and would be
    wrong by up to a poll interval on every line.

    Forced to a sentinel rather than compared to a threshold, so the claim is
    WHICH measurement the line carries.
    """

    real_job = stream_mod._SnapshotBuildJob

    class _SentinelJob(real_job):  # type: ignore[misc, valid-type]
        def run(self) -> None:
            super().run()
            self.elapsed_ms = _SENTINEL_ELAPSED_MS

    monkeypatch.setattr(stream_mod, "_SnapshotBuildJob", _SentinelJob)

    with caplog.at_level(logging.INFO, logger="agent_runtime.stream"):
        frames = list(
            stream_frames(
                poll_interval_seconds=0.01,
                heartbeat_interval_seconds=60,
                max_frames=1,
            )
        )

    lines = _build_lines(caplog)
    assert len(lines) == 1, [record.getMessage() for record in caplog.records]
    line = lines[0]
    assert line["reason"] == "hydrate"
    assert int(line["waited_ms"]) == _SENTINEL_ELAPSED_MS, line
    # The deprecated twin still ships the SAME value — a rename that shipped two
    # different numbers under two keys would be worse than the name it replaced.
    assert int(line["elapsed_ms"]) == _SENTINEL_ELAPSED_MS, line
    # The anchor and the attribution fields still ride the line, unchanged.
    assert line["offset"] == str(frames[0]["watermark"]["event_offset"])
    for key in ("build_ms", "role", "caller", "generation"):
        assert key in line, line


# --------------------------------------------------------------------------- #
# 6. The boundary check: the committed frame goldens did not move
# --------------------------------------------------------------------------- #
def test_a_build_shorter_than_one_heartbeat_interval_puts_nothing_extra_on_the_wire(
    isolate_agent_runtime_root, monkeypatch
):
    """Why this stage is golden-NEUTRAL, stated as a property rather than a hope.

    A heartbeat is emitted only once a full interval has elapsed with the build
    still running. The committed frame fixtures under
    ``tests/fixtures/stream_frames/`` are produced by builds that finish
    immediately, at the production 5 s cadence — so no boot heartbeat can appear
    in them and no golden's bytes can move. If this case ever reds, the goldens
    are the next thing to check, and regenerating them would make this a
    cross-stack landing (the launcher mirrors them byte-for-byte).
    """

    gated = _GatedBuild(monkeypatch)
    gated.release()

    frames = list(
        stream_frames(
            poll_interval_seconds=0.01, heartbeat_interval_seconds=60, max_frames=1
        )
    )

    assert [frame["type"] for frame in frames] == ["hydrate"], [
        frame["type"] for frame in frames
    ]
