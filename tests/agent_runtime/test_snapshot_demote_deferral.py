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


def _admitted(monkeypatch, values):
    """Drive ``_chat_turns_admitted`` from a script; the last value repeats.

    The mirror of :func:`_in_flight`, and deliberately a SECOND script rather
    than one fused gauge: Stage 7's whole subject is the window where the two
    counters disagree — a turn admitted at the handler anchor with no runner
    yet — so a test that could not say "admitted 1, running 0" could not state
    the bug.
    """

    script = list(values)

    def _read():
        return script.pop(0) if len(script) > 1 else script[0]

    monkeypatch.setattr(stream_mod, "_chat_turns_admitted", _read)


def test_the_bound_covers_the_measured_pre_admit_p95_and_is_a_constant():
    """The doctrine value itself, pinned — CP-3's number, not a round one.

    Stage 5 chose one second for a window that opened at
    ``ProfileAgentRunner.run()``. Stage 7 makes the wait cover the span from
    the handler's anchor instead, and that span was MEASURED: §0.1's
    ``write_ahead`` p95 on 2026-09-07 is 3,172 ms (3,172 / 2,796 / 906). A
    bound shorter than the span it now protects would release the build back
    into the middle of the turn it just stood aside for.

    Asserting the behaviour against the constant alone would let a later edit
    move both together and stay green, so the VALUE is pinned here and the
    behaviour is pinned against the constant below.
    """

    assert SNAPSHOT_DEMOTE_DEFERRAL_MAX_MS == 3500


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


# --------------------------------------------------------------------------- #
# chat-turn-prep Stage 6 item 4 — CP-2, built as a RECORDER                     #
# --------------------------------------------------------------------------- #
# §0.2 measured the thing Stage 5 cannot see: turns 1 and 2 spent 3,172 and
# 2,796 ms before ``write_ahead`` with a led build and a 5,750 ms actor prewarm
# running beside them, and neither the deferral nor the prewarm's own yield
# could tell, because both read ``profile_runner._ACTIVE_RUNS``, which
# ``_counted_agent_run`` does not increment until ``ProfileAgentRunner.run()``.
# Stage 7 makes them read the admitted counter. Stage 6 only BUILDS it and
# writes what it sees down: ``admitted_at_exit=`` on this receipt, and
# ``prewarm_overlapped`` on the turn record. Nothing here decides anything.


def test_an_admitted_turn_is_counted_from_the_anchor_and_released_by_ANY_exit():
    """CP-2's counter, at its own unit.

    The window this opens is the one ``agent_runs_in_flight`` cannot see: it
    starts where the turn's monotonic anchor is taken and ends when the handler
    leaves by any path — including the fourteen terminal transitions of the
    commit phase and every refusal above them. A counter that leaked on a raise
    would wedge Stage 7's deferral permanently, so the release is asserted
    through an exception and not only through a clean exit.
    """

    from agent_runtime import turn_activity

    assert turn_activity.chat_turns_admitted() == 0
    with turn_activity.admitted_turn():
        assert turn_activity.chat_turns_admitted() == 1
        with turn_activity.admitted_turn():
            assert turn_activity.chat_turns_admitted() == 2
        assert turn_activity.chat_turns_admitted() == 1
    assert turn_activity.chat_turns_admitted() == 0

    with pytest.raises(RuntimeError):
        with turn_activity.admitted_turn():
            raise RuntimeError("a turn that died inside its admitted window")
    assert turn_activity.chat_turns_admitted() == 0


def test_the_admitted_counter_is_its_OWN_authority_and_not_the_runners():
    """One counter, one meaning. ``agent_runs_in_flight`` keeps its callers and
    its definition (a run inside ``ProfileAgentRunner.run()``); this one counts
    a turn from its anchor. Stage 7 reads ``admitted() or running()`` precisely
    because they are different facts about different spans — collapsing them
    here would delete the distinction the whole stage turns on."""

    from agent_runtime import profile_runner, turn_activity

    with turn_activity.admitted_turn():
        assert turn_activity.chat_turns_admitted() == 1
        assert profile_runner.agent_runs_in_flight() == 0
    with profile_runner._counted_agent_run():
        assert profile_runner.agent_runs_in_flight() == 1
        assert turn_activity.chat_turns_admitted() == 0


def test_the_deferral_receipt_says_how_many_turns_were_ADMITTED_at_exit(
    monkeypatch, caplog
):
    """Stage 6's half of CP-2 on this line: a second number, no new decision.

    ``runs_in_flight_at_exit`` answers "was a run still going when I gave up",
    and on 2026-09-07 the answer was ``1`` on the three deferrals that fired —
    all of them during turn 3's PROVIDER wait, none during the pre-admit span
    turns 1 and 2 spent three seconds in. ``admitted_at_exit`` is what makes
    that second window visible on the same line, so Stage 7's before/after is
    read off receipts rather than argued.

    *Killing mutation:* log the runs counter under both names and the two
    numbers agree on every line, which is the state this key exists to end.
    """

    from agent_runtime import turn_activity

    _in_flight(monkeypatch, [1])
    clock = _FakeClock()
    with caplog.at_level("INFO", logger=stream_mod.logger.name):
        with turn_activity.admitted_turn():
            _defer_demote_build_for_active_turns(
                reason=BATCH_REASON_DEMOTE,
                caller="hub",
                sleeper=clock.sleep,
                clock=clock,
            )
    lines = [
        r.getMessage()
        for r in caplog.records
        if "snapshot_build_deferred" in r.getMessage()
    ]
    assert len(lines) == 1
    assert "admitted_at_exit=1" in lines[0]
    assert "runs_in_flight_at_exit=1" in lines[0]


def test_an_unreadable_admitted_counter_reads_unknown_and_never_zero(
    monkeypatch, caplog
):
    """The same absent-never-zero rule the whole plan is about, on a log line.

    ``admitted_at_exit=0`` is a finding — no turn was admitted when this build
    gave up waiting. "I could not ask" is not that finding, and a line that
    spelled them identically would be evidence for a conclusion nobody reached.
    """

    _in_flight(monkeypatch, [1])
    monkeypatch.setattr(stream_mod, "_chat_turns_admitted", lambda: None)
    clock = _FakeClock()
    with caplog.at_level("INFO", logger=stream_mod.logger.name):
        _defer_demote_build_for_active_turns(
            reason=BATCH_REASON_DEMOTE,
            caller="hub",
            sleeper=clock.sleep,
            clock=clock,
        )
    line = [
        r.getMessage()
        for r in caplog.records
        if "snapshot_build_deferred" in r.getMessage()
    ][0]
    assert "admitted_at_exit=unknown" in line


# ── Stage 7: the deferral DECIDES on the admitted counter ────────────────────
#
# Stage 6 built CP-2's counter and wrote what it saw onto the receipt
# (``admitted_at_exit=``) while the DECISION stayed ``_agent_runs_in_flight()``
# alone. The row above this block used to assert exactly that boundary
# ("a demote build requested while a turn is admitted but not yet running must
# still proceed today — and this row is the one Stage 7 flips"). This is that
# flip: the four tests below are the same seam read the other way round.


def test_admitted_before_runner_defers_demote(monkeypatch):
    """The window Stage 5 could not see, now held.

    §0.1's turns 1 and 2 spent 3,172 and 2,796 ms between the handler's anchor
    and ``write_ahead`` with a led core build burning the same GIL, and the
    deferral saw nothing the whole time because ``_counted_agent_run`` does not
    increment until ``ProfileAgentRunner.run()``. Admitted 1 / running 0 is
    precisely that state, and it must now hold the build.

    The release is asserted at the NEXT POLL rather than instantly, because
    that is the real mechanism: the wait loop re-samples, and a test that
    accepted an immediate return would also pass against a deferral that never
    slept at all.
    """

    _in_flight(monkeypatch, [0])
    _admitted(monkeypatch, [1, 1, 0])
    clock = _FakeClock()
    waited = _defer_demote_build_for_active_turns(
        reason=BATCH_REASON_DEMOTE,
        caller="hub",
        sleeper=clock.sleep,
        clock=clock,
    )
    assert waited > 0, (
        "a turn admitted at its anchor with no runner yet is the exact state "
        "§0.1's turns 1 and 2 were in — the demote build must stand aside"
    )
    assert clock.slept, "standing aside means sleeping, not returning a number"
    assert waited < SNAPSHOT_DEMOTE_DEFERRAL_MAX_MS, (
        "admission was released mid-wait; the build must resume at the next "
        "poll rather than serve out the whole bound"
    )


def test_admission_release_exception_and_refusal_do_not_leak(monkeypatch):
    """A leaked count would wedge the demote lane for the life of the process.

    Stage 6 could afford a leak — nothing read the counter. Stage 7 cannot: an
    admission that survived its handler would hold every subsequent demote
    build for the full 3.5 s bound, forever. So this exercises the REAL
    ``turn_activity`` cleanup (not the scripted gauge) across the two exits the
    mission-chat handler actually takes — a raise from inside the window, and
    an ordinary early refusal — and then proves the next build is free.
    """

    from agent_runtime import turn_activity

    assert turn_activity.chat_turns_admitted() == 0

    with pytest.raises(RuntimeError):
        with turn_activity.admitted_turn():
            raise RuntimeError("a turn that died between its anchor and write_ahead")
    assert turn_activity.chat_turns_admitted() == 0

    def _refuse_early():
        with turn_activity.admitted_turn():
            return "refused_before_write_ahead"

    assert _refuse_early() == "refused_before_write_ahead"
    assert turn_activity.chat_turns_admitted() == 0

    # The real counter, unscripted, reading the real zero left behind.
    _in_flight(monkeypatch, [0])
    clock = _FakeClock()
    waited = _defer_demote_build_for_active_turns(
        reason=BATCH_REASON_DEMOTE,
        caller="hub",
        sleeper=clock.sleep,
        clock=clock,
    )
    assert waited == 0
    assert clock.slept == []


def test_demote_bound_covers_two_seconds_but_releases_at_3500(monkeypatch):
    """CP-3's bound at both of its edges, on a scripted clock.

    Two claims, and they need each other. At 2,000 ms — past Stage 5's old
    ceiling, inside the measured pre-admit p95 — the build is STILL held; that
    is the whole reason the constant moved. At the bound it proceeds regardless,
    so a wedged counter cannot starve the launcher's HUD.

    The build that waited out the bound and then overlapped anyway is still
    counted overlapped: nothing here touches the ledger, and the deferral must
    not be able to launder its own failures out of ``builds_overlapped``.
    """

    _in_flight(monkeypatch, [0])
    _admitted(monkeypatch, [1])  # a turn that never ends within the bound
    clock = _FakeClock()

    still_held_at_2000 = {}

    def _sleep(seconds):
        clock.sleep(seconds)
        elapsed_ms = int((clock.now - 1_000.0) * 1000)
        if elapsed_ms >= 2_000 and "answer" not in still_held_at_2000:
            still_held_at_2000["answer"] = True

    waited = _defer_demote_build_for_active_turns(
        reason=BATCH_REASON_DEMOTE, caller="hub", sleeper=_sleep, clock=clock
    )
    assert still_held_at_2000.get("answer") is True, (
        "at 2,000 ms — past Stage 5's old 1,000 ms ceiling — an admitted turn "
        "must still be holding the build; that is why CP-3 moved the constant"
    )
    assert waited == SNAPSHOT_DEMOTE_DEFERRAL_MAX_MS == 3500, (
        "the bound is a ceiling on the WHOLE deferral: once it elapses the "
        "build proceeds no matter what is admitted"
    )


def test_hydrate_full_core_and_cancel_preserve_bypass(monkeypatch):
    """Stage 7 widens WHO the demote lane yields to, never WHICH lanes yield.

    The exclusions are the safety argument and they are unchanged: the boot /
    hydrate job and the ``full_core`` lane are a consumer waiting on an answer,
    not a cadence rebuilding state nobody asked for. Making an operator's first
    paint wait on a chat turn would trade this inflation for a worse one. And a
    consumer that went away must not be waited FOR.
    """

    for reason in ("full_core", "hydrate", "boot"):
        _in_flight(monkeypatch, [0])
        _admitted(monkeypatch, [3])
        clock = _FakeClock()
        waited = _defer_demote_build_for_active_turns(
            reason=reason, caller="hub", sleeper=clock.sleep, clock=clock
        )
        assert waited == 0, f"{reason} must never wait on an admitted turn"
        assert clock.slept == [], f"{reason} must not sleep at all"

    _in_flight(monkeypatch, [0])
    _admitted(monkeypatch, [1])
    monkeypatch.setattr(stream_mod, "request_cancelled", lambda: True)
    clock = _FakeClock()
    waited = _defer_demote_build_for_active_turns(
        reason=BATCH_REASON_DEMOTE, caller="hub", sleeper=clock.sleep, clock=clock
    )
    assert waited == 0
    assert clock.slept == []


def test_an_unreadable_admitted_counter_still_defers_for_a_live_run(monkeypatch):
    """``None`` is unknown, and unknown is not a veto over the OTHER counter.

    The forwarders answer ``None`` when the module cannot be consulted at all.
    Stage 5's rule — treat unknown as "do not defer" — was written when there
    was one gauge. With two, an unreadable admitted counter must degrade to
    Stage 5's exact behaviour rather than suppress a deferral a live run had
    already earned.
    """

    _in_flight(monkeypatch, [2, 2, 0])
    _admitted(monkeypatch, [None])
    clock = _FakeClock()
    waited = _defer_demote_build_for_active_turns(
        reason=BATCH_REASON_DEMOTE, caller="hub", sleeper=clock.sleep, clock=clock
    )
    assert waited > 0, (
        "an unreadable admitted counter must not cancel the deferral a "
        "readable run counter earned"
    )


def test_prewarm_overlapped_counts_a_construction_whose_span_intersects():
    """Stage 6's second recorder, sampled exactly the way ``builds_overlapped``
    is: spans on one monotonic clock, intersected with the turn's window.

    §0.2's prewarm ran 5,750 ms across the WHOLE of turn 1's pre-admit span —
    an LRU eviction closing eight OpenAI clients over ~1.1 s, a full ``check_fn``
    sweep, a tool-search activation — and the only trace of it was a log line
    nobody joins to a turn. Stage 7 does not move that eviction; it names it and
    says a stage for it is written only if this receipt bills it. So the receipt
    has to exist first.
    """

    from agent_runtime import persona_chat_actor_prewarm as prewarm

    prewarm.reset_construction_spans_for_tests()
    try:
        assert prewarm.overlapping_constructions(start=0.0, end=10.0) is None, (
            "a process that has never prewarmed cannot see the lane, and must "
            "say so rather than report a zero"
        )
        prewarm.record_construction(started=2.0, ended=5.0)
        assert prewarm.overlapping_constructions(start=0.0, end=1.0) == 0
        assert prewarm.overlapping_constructions(start=0.0, end=2.0) == 1
        assert prewarm.overlapping_constructions(start=3.0, end=4.0) == 1
        assert prewarm.overlapping_constructions(start=5.0, end=9.0) == 1
        assert prewarm.overlapping_constructions(start=6.0, end=9.0) == 0
    finally:
        prewarm.reset_construction_spans_for_tests()


def test_a_prewarm_that_stood_DOWN_recorded_no_construction(monkeypatch):
    """A yield is not a construction.

    ``prewarm_chat_actor`` returns ``skipped_turn_active`` before it assembles
    anything when a run is in flight, and billing that as a span would put a
    zero-cost refusal into the count that Stage 7's eviction question turns on.
    """

    from agent_runtime import persona_chat_actor_prewarm as prewarm

    monkeypatch.setattr(
        prewarm, "_persona_chat_runtime_registry_present", lambda: True, raising=False
    )
    monkeypatch.setattr(
        "agent_runtime.profile_runner.agent_runs_in_flight", lambda: 1
    )
    prewarm.reset_construction_spans_for_tests()
    try:
        outcome = prewarm.prewarm_chat_actor("chat-root-that-yields")
        assert outcome in (
            prewarm.OUTCOME_SKIPPED_TURN_ACTIVE,
            prewarm.OUTCOME_REGISTRY_OFF,
        )
        assert prewarm.overlapping_constructions(start=0.0, end=10**9) is None
    finally:
        prewarm.reset_construction_spans_for_tests()


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
