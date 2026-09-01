"""Stage 5: a demote-cadence core build stands aside for a live agent run.

``planned/chat-turn-prep-cost.md`` §5 Stage 5. ``builds_overlapped`` was already
on the record and the correlation was already argued from live data — §2.5
(a led build billing ``build_ms=3979`` of in-process CPU during the 4a80f05e
turn) and Stage 2a item 7 (turns overlapping a readiness walk billing
1,796/2,343 ms against 453 ms for a non-overlapping one). What was missing was
the yield.

These pin the three properties that make the yield safe rather than merely
present:

* it fires on the DEMOTE cadence only — not on boot, hydrate, or the
  ``full_core`` lane a consumer is actually waiting on;
* it is BOUNDED by a constant, so a back-to-back operator cannot starve the
  launcher's HUD; and
* the bound is that constant's value, not a hope about how long a turn runs.

The clock and the sleeper are injected, so the bound is proven at its exact
edge without the suite sleeping for a second.
"""

from __future__ import annotations

import pytest

import agent_runtime.stream as stream_mod
from agent_runtime.stream import (
    BATCH_REASON_DEMOTE,
    SNAPSHOT_DEMOTE_DEFERRAL_MAX_MS,
    _defer_demote_build_for_active_turns,
)


class _FakeClock:
    """A monotonic clock the sleeper advances, so no test-time sleep happens."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += max(0.0, float(seconds))


def _in_flight(monkeypatch, values):
    """Drive ``_agent_runs_in_flight`` from a script; the last value repeats."""

    script = list(values)

    def _read():
        return script.pop(0) if len(script) > 1 else script[0]

    monkeypatch.setattr(stream_mod, "_agent_runs_in_flight", _read)


def test_the_bound_is_one_second_and_is_a_constant_not_a_literal():
    """The doctrine value itself, pinned.

    The plan says "hundreds of ms, not a starvation" and the module comment
    commits to one second. Asserting the behaviour against the constant alone
    would let a later edit move both together and stay green, so the VALUE is
    pinned here and the behaviour is pinned against the constant below.
    """

    assert SNAPSHOT_DEMOTE_DEFERRAL_MAX_MS == 1000


def test_a_non_demote_lane_never_defers(monkeypatch):
    """Boot, hydrate and ``full_core`` are consumers waiting, not a cadence."""

    _in_flight(monkeypatch, [5])
    clock = _FakeClock()
    waited = _defer_demote_build_for_active_turns(
        reason="full_core", caller="hub", sleeper=clock.sleep, clock=clock
    )
    assert waited == 0
    assert clock.slept == [], "a non-demote lane must not sleep at all"


def test_a_demote_build_with_no_run_in_flight_does_not_wait(monkeypatch):
    """Nothing to yield to is not a reason to pause the cadence."""

    _in_flight(monkeypatch, [0])
    clock = _FakeClock()
    waited = _defer_demote_build_for_active_turns(
        reason=BATCH_REASON_DEMOTE, caller="hub", sleeper=clock.sleep, clock=clock
    )
    assert waited == 0
    assert clock.slept == []


def test_a_demote_build_waits_while_a_run_is_in_flight_and_resumes_when_it_ends(
    monkeypatch,
):
    """The common case: the turn ends mid-wait and the build proceeds at once."""

    # First read (the pre-check) sees a run; three poll reads see it; then 0.
    _in_flight(monkeypatch, [1, 1, 1, 1, 0])
    clock = _FakeClock()
    waited = _defer_demote_build_for_active_turns(
        reason=BATCH_REASON_DEMOTE, caller="hub", sleeper=clock.sleep, clock=clock
    )
    assert clock.slept, "a run in flight must produce at least one wait slice"
    assert 0 < waited < SNAPSHOT_DEMOTE_DEFERRAL_MAX_MS, (
        "a turn that ended mid-wait must release the build EARLY — waiting the "
        "full bound anyway would make the yield indistinguishable from a sleep"
    )


def test_a_run_that_never_ends_releases_the_build_at_exactly_the_bound(monkeypatch):
    """The starvation refusal, at its edge.

    This is the property the launcher's HUD freshness depends on: an operator
    sending turns back to back must not be able to hold the demote cadence off
    indefinitely. The build proceeds regardless once the bound elapses.
    """

    _in_flight(monkeypatch, [3])  # never drops
    clock = _FakeClock()
    waited = _defer_demote_build_for_active_turns(
        reason=BATCH_REASON_DEMOTE, caller="hub", sleeper=clock.sleep, clock=clock
    )
    assert waited == SNAPSHOT_DEMOTE_DEFERRAL_MAX_MS
    assert sum(clock.slept) == pytest.approx(
        SNAPSHOT_DEMOTE_DEFERRAL_MAX_MS / 1000.0
    ), "the wait slices must sum to the bound, never overshoot it"


def test_an_unreadable_run_counter_does_not_defer(monkeypatch):
    """Unknown is not "a turn is running".

    A deferral is an optimization; one that fires on an unmeasured premise is
    the exact failure mode this plan exists to end.
    """

    monkeypatch.setattr(stream_mod, "_agent_runs_in_flight", lambda: None)
    clock = _FakeClock()
    waited = _defer_demote_build_for_active_turns(
        reason=BATCH_REASON_DEMOTE, caller="hub", sleeper=clock.sleep, clock=clock
    )
    assert waited == 0
    assert clock.slept == []


def test_a_cancelled_request_abandons_the_wait_immediately(monkeypatch):
    """A consumer that went away must not be waited FOR."""

    _in_flight(monkeypatch, [2])
    monkeypatch.setattr(stream_mod, "request_cancelled", lambda: True)
    clock = _FakeClock()
    waited = _defer_demote_build_for_active_turns(
        reason=BATCH_REASON_DEMOTE, caller="hub", sleeper=clock.sleep, clock=clock
    )
    assert waited == 0
    assert clock.slept == []


def test_the_deferral_logs_one_line_naming_the_lane_and_the_wait(
    monkeypatch, caplog
):
    """The receipt, in the 07-observability closed vocabulary: ids and timings.

    A deferral that leaves no trace is indistinguishable from a slow build, and
    the whole point of the stage is being able to tell those apart.
    """

    _in_flight(monkeypatch, [1])
    clock = _FakeClock()
    with caplog.at_level("INFO", logger=stream_mod.logger.name):
        _defer_demote_build_for_active_turns(
            reason=BATCH_REASON_DEMOTE,
            caller="hub",
            sleeper=clock.sleep,
            clock=clock,
        )
    lines = [r.getMessage() for r in caplog.records if "snapshot_build_deferred" in r.getMessage()]
    assert len(lines) == 1, "exactly one line per deferral, never one per poll"
    line = lines[0]
    assert f"reason={BATCH_REASON_DEMOTE}" in line
    assert "caller=hub" in line
    assert f"waited_ms={SNAPSHOT_DEMOTE_DEFERRAL_MAX_MS}" in line
    assert f"bound_ms={SNAPSHOT_DEMOTE_DEFERRAL_MAX_MS}" in line


def test_a_wait_that_resolves_to_zero_ms_logs_nothing(monkeypatch, caplog):
    """Absent-never-zero, applied to the log lane.

    The interesting case is NOT "nothing was in flight" — that returns before
    the log guard is reachable at all, so a test written that way pins nothing
    (proven: mutating the guard to ``>= 0`` left such a test green). This drives
    the path that REACHES the guard with a zero measurement: a run is in flight
    at the pre-check and gone by the first poll, so the loop is entered and
    exits without the clock advancing. ``waited_ms == 0`` there is a real
    measurement of no wait, and a line claiming ``waited_ms=0`` would be noise
    on every quiet demote build.
    """

    _in_flight(monkeypatch, [1, 0])
    clock = _FakeClock()
    with caplog.at_level("INFO", logger=stream_mod.logger.name):
        waited = _defer_demote_build_for_active_turns(
            reason=BATCH_REASON_DEMOTE,
            caller="hub",
            sleeper=clock.sleep,
            clock=clock,
        )
    assert waited == 0
    assert clock.slept == [], "the run was gone by the first poll; nothing to sleep for"
    assert not [
        r for r in caplog.records if "snapshot_build_deferred" in r.getMessage()
    ]


def test_the_signal_is_the_runners_counter_and_not_a_second_authority():
    """The forwarder resolves to ``profile_runner.agent_runs_in_flight``.

    Asked of the runtime rather than of the source: the counter is incremented
    through the runner's own context manager and read back through the stream's
    forwarder, so a re-spelling that quietly minted a second counter here would
    read 0 while the runner reads 1.
    """

    from agent_runtime import profile_runner

    assert stream_mod._agent_runs_in_flight() == profile_runner.agent_runs_in_flight()
    with profile_runner._counted_agent_run():
        assert profile_runner.agent_runs_in_flight() == 1
        assert stream_mod._agent_runs_in_flight() == 1
    assert stream_mod._agent_runs_in_flight() == 0


def test_the_demote_lane_actually_calls_the_deferral_before_it_builds(monkeypatch):
    """The wiring, proven at the call site rather than by reading it.

    ``_full_core_batch_frames`` is driven with its build arm stubbed, so the
    test observes the ORDER that matters: the yield decision is taken before a
    worker thread is started, not after.
    """

    calls: list[tuple[str, str]] = []
    order: list[str] = []

    def _fake_defer(*, reason, caller, sleeper=None, clock=None):
        calls.append((reason, caller))
        order.append("defer")
        return 0

    class _FakeJob:
        def __init__(self, caller, accept_inflight=False):
            order.append("job")
            self.snapshot = {"schema_version": 1, "sections": {}}
            self.error = None
            self.elapsed_ms = None
            self.build_info = {"caller": caller, "role": "led", "generation": 1}

    monkeypatch.setattr(
        stream_mod, "_defer_demote_build_for_active_turns", _fake_defer
    )
    monkeypatch.setattr(stream_mod, "_SnapshotBuildJob", _FakeJob)
    monkeypatch.setattr(
        stream_mod, "_build_with_liveness", lambda *a, **k: iter(())
    )
    monkeypatch.setattr(stream_mod, "request_cancelled", lambda: False)
    monkeypatch.setattr(stream_mod.demote_core_reuse, "consult", lambda floor: None)
    monkeypatch.setattr(stream_mod.demote_core_reuse, "remember", lambda snap: None)
    monkeypatch.setattr(stream_mod, "core_event_offset", lambda snap: None)
    monkeypatch.setattr(
        stream_mod, "delta_batch_frame", lambda batch, snapshot: {"watermark": {}}
    )

    batch = [(7, object())]
    list(
        stream_mod._full_core_batch_frames(
            batch,
            base_offset=6,
            heartbeat_interval_seconds=5.0,
            reason=BATCH_REASON_DEMOTE,
            caller="hub",
        )
    )
    assert calls == [(BATCH_REASON_DEMOTE, "hub")]
    assert order == ["defer", "job"], (
        "the yield must be decided BEFORE the build job exists — deferring "
        "after the worker starts yields nothing"
    )
