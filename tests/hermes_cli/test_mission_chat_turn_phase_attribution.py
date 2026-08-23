"""Stage 4 — cold-init and build-contention ATTRIBUTION, not remedy.

Two remedy candidates for chat-turn latency rest on correlation today:

* is first-turn-per-profile cold init dominated by duplicate tool-registry
  probing? (the storm ran the registry checks more than once, read off
  repetition patterns in a shared, pid-less log);
* do concurrent snapshot builds actually steal turn time? (they interleaved
  every sampled turn — and the warm turn stayed fast WITH one running, which
  argues they mostly do not).

Neither is settled here. These counters exist to say whether the suspects were
even present, and the rows below defend the one property that makes such a
count worth having: **``0`` is a verdict and ABSENT is a shrug, and the record
must never confuse them.** ``builds_overlapped: 0`` acquits build contention
for that turn. A ``0`` written because nothing could be observed would acquit it
falsely, which is worse than saying nothing.
"""

from __future__ import annotations

import pytest

from agent_runtime import mission_chat_phases, snapshot_build_ledger
from agent_runtime.mission_chat_phases import TURN_PHASES_KEY, TurnPhaseMarks

from tests.hermes_cli.test_mission_chat_budget_payload import (  # type: ignore
    _seed,
    isolate_agent_runtime_root,  # noqa: F401  (re-exported fixture)
)
from tests.hermes_cli.test_mission_chat_turn_phases import (  # type: ignore
    _TickClock,
    _args,
    _record_on_disk,
    _streaming_provider,
)


# --------------------------------------------------------------------------- #
# The build ledger, on its own                                                 #
# --------------------------------------------------------------------------- #
@pytest.fixture
def empty_ledger():
    snapshot_build_ledger.reset_for_tests()
    yield snapshot_build_ledger
    snapshot_build_ledger.reset_for_tests()


def test_a_process_that_never_built_answers_UNKNOWN_not_zero(empty_ledger):
    """A chat turn in a CLI child sees no builds because it cannot see them.

    Reporting ``0`` there would say "no builds overlapped this turn" — a claim
    about the serve process made by a process that cannot observe it.
    """

    assert empty_ledger.overlapping_builds(start=0.0, end=100.0) is None


def test_a_process_that_HAS_built_answers_zero_for_a_quiet_window(empty_ledger):
    """...and once it has built, ``0`` becomes a real measurement."""

    empty_ledger.record_build(started=500.0, ended=501.0)
    assert empty_ledger.overlapping_builds(start=0.0, end=100.0) == 0


@pytest.mark.parametrize(
    "span,expected",
    [
        ((10.0, 20.0), 1),   # fully inside
        ((-5.0, 5.0), 1),    # straddles the anchor
        ((90.0, 150.0), 1),  # straddles the end
        ((-50.0, 500.0), 1), # swallows the window whole
        ((100.0, 110.0), 1), # touches the closing edge
        ((200.0, 300.0), 0), # entirely after
        ((-30.0, -1.0), 0),  # entirely before
    ],
)
def test_overlap_is_interval_intersection_not_containment(empty_ledger, span, expected):
    empty_ledger.record_build(started=span[0], ended=span[1])
    assert empty_ledger.overlapping_builds(start=0.0, end=100.0) == expected


def test_a_build_that_RAISED_still_occupied_the_process(empty_ledger):
    """The span scope records on the way out, exception or not.

    A build that died halfway still spent that time in this process, and a turn
    that overlapped it paid exactly the same price as if it had succeeded.
    """

    with pytest.raises(RuntimeError):
        with snapshot_build_ledger.build_span_scope():
            raise RuntimeError("build blew up")
    assert empty_ledger.overlapping_builds(start=0.0, end=1e18) == 1


# --------------------------------------------------------------------------- #
# The registry's probe-round counter                                           #
# --------------------------------------------------------------------------- #
def test_a_pass_that_really_probes_counts_one_round_and_a_cached_pass_counts_none():
    """A round is WORK, not a call.

    The 30 s TTL cache is what makes the second pass free; counting it would
    inflate exactly the number the "is cold init duplicate probing?" decision
    turns on, and inflate it in the direction that argues for the remedy.
    """

    from tools import registry as registry_module

    probes = {"n": 0}

    def _check():
        probes["n"] += 1
        return True

    # A private registry instance and a closure nothing else holds: this row
    # must not reach for ``invalidate_check_fn_cache()``, which would clear the
    # TTL cache the WHOLE process shares and hand every later test in the run a
    # cold re-probe of docker/playwright/sockets. The closure is unique, so its
    # cache entry starts absent and is evicted by name below.
    reg = registry_module.ToolRegistry()
    reg.register(
        "phase_probe_tool",
        "phase_probe_toolset",
        {"description": "d", "parameters": {}},
        lambda **kw: "ok",
        check_fn=_check,
    )
    before = registry_module.probe_rounds_this_thread()

    reg.get_definitions({"phase_probe_tool"})
    assert registry_module.probe_rounds_this_thread() == before + 1
    assert probes["n"] == 1

    reg.get_definitions({"phase_probe_tool"})
    assert registry_module.probe_rounds_this_thread() == before + 1, (
        "a pass served entirely from the TTL cache did no probing and is not a round"
    )
    assert probes["n"] == 1

    registry_module._check_fn_cache.pop(_check, None)
    reg.get_definitions({"phase_probe_tool"})
    assert registry_module.probe_rounds_this_thread() == before + 2
    assert probes["n"] == 2
    registry_module._check_fn_cache.pop(_check, None)
    registry_module._check_fn_last_good.pop(_check, None)


def test_the_counter_is_cumulative_so_overlapping_observers_cannot_reset_each_other():
    """``harness serve`` runs concurrent turns; a shared reset would be a race.

    The turn takes a DIFFERENCE across its own window instead, which is why
    ``set_baseline`` exists and why an unknown baseline yields no key at all.
    """

    marks = TurnPhaseMarks(monotonic=_TickClock(), wall_now=lambda: "stamp")
    marks.set_baseline("registry_probe_rounds", 17)
    marks.count_delta("registry_probe_rounds", 20)
    assert marks.snapshot()["registry_probe_rounds"] == 3


def test_an_unknown_baseline_records_NOTHING_rather_than_a_lifetime_total():
    marks = TurnPhaseMarks(monotonic=_TickClock(), wall_now=lambda: "stamp")
    marks.set_baseline("registry_probe_rounds", None)
    marks.count_delta("registry_probe_rounds", 4096)
    assert "registry_probe_rounds" not in marks.snapshot(), (
        "with no baseline the process's lifetime total is not this turn's count"
    )


# --------------------------------------------------------------------------- #
# ...and the same facts, on a REAL turn's record                               #
# --------------------------------------------------------------------------- #
#: With the scripted tick clock the anchor is 0.0 and marks land one "second"
#: apart, so a build fixture at [1.0, 2.0] is inside the turn window and one at
#: [1e6, 1e6+1] is far outside it. The row below asserts that placement rather
#: than assuming it.
_INSIDE_BUILD = (1.0, 2.0)
_OUTSIDE_BUILD = (1_000_000.0, 1_000_001.0)


@pytest.fixture
def attributed_turn(request, monkeypatch, capsys, isolate_agent_runtime_root, empty_ledger):  # noqa: F811
    """Drive one real turn against a scripted clock and a fixture build ledger."""

    builds = request.param if hasattr(request, "param") else (_INSIDE_BUILD,)
    for started, ended in builds:
        empty_ledger.record_build(started=started, ended=ended)

    def _factory():
        return TurnPhaseMarks(
            monotonic=_TickClock(), wall_now=lambda: "2026-08-21T21:17:43.400000Z"
        )

    monkeypatch.setattr(mission_chat_phases, "TurnPhaseMarks", _factory)

    from tools import registry as registry_module

    rounds = iter([7, 11])
    monkeypatch.setattr(
        registry_module, "probe_rounds_this_thread", lambda: next(rounds, 11)
    )

    from agent_runtime import chat_lane_bundle as bundle_module

    # Same shape, same window: baseline at the anchor, second read at
    # ``agent_ready``. Scripted rather than measured for the same reason the
    # probe rounds are — this row is about the DELTA arithmetic reaching the
    # record, not about how many bundles this test process happened to build.
    bundle_builds = iter([3, 4])
    monkeypatch.setattr(
        bundle_module, "bundle_builds_this_thread", lambda: next(bundle_builds, 4)
    )

    harness = _seed(
        monkeypatch,
        _streaming_provider(profile_timing={"resident_actor_reused": 0}),
    )
    harness._cmd_mission_chat_message(_args("stage4_turn", stream=True))
    capsys.readouterr()
    return _record_on_disk(isolate_agent_runtime_root, "stage4_turn")[TURN_PHASES_KEY]


@pytest.mark.parametrize("attributed_turn", [(_INSIDE_BUILD,)], indirect=True)
def test_the_fixture_build_is_actually_inside_the_turn_window(attributed_turn):
    """Ground the next row: the window really does contain [1.0, 2.0] seconds."""

    assert attributed_turn["stream_done"] >= 2000, (
        "the scripted timeline got shorter than the fixture build; the overlap "
        "row below would be asserting nothing"
    )


@pytest.mark.parametrize("attributed_turn", [(_INSIDE_BUILD,)], indirect=True)
def test_a_build_inside_the_turn_window_is_counted(attributed_turn):
    assert attributed_turn["builds_overlapped"] == 1


@pytest.mark.parametrize("attributed_turn", [(_OUTSIDE_BUILD,)], indirect=True)
def test_a_build_shifted_outside_the_turn_window_is_not_counted(attributed_turn):
    """The named sabotage for this stage, as a permanent row.

    Same ledger, same turn, one build moved off the window: the count must fall
    to a hard ``0``. If it stayed at 1 the counter would be measuring "a build
    happened at some point", which acquits or convicts nothing.
    """

    assert attributed_turn["builds_overlapped"] == 0


@pytest.mark.parametrize(
    "attributed_turn", [(_INSIDE_BUILD, _OUTSIDE_BUILD)], indirect=True
)
def test_only_the_overlapping_build_of_a_mixed_ledger_is_counted(attributed_turn):
    assert attributed_turn["builds_overlapped"] == 1


@pytest.mark.parametrize("attributed_turn", [(_INSIDE_BUILD,)], indirect=True)
def test_the_turn_records_the_registry_probe_rounds_it_paid_for(attributed_turn):
    """Baseline at the anchor, second read at ``agent_ready``: 11 - 7."""

    assert attributed_turn["registry_probe_rounds"] == 4


@pytest.mark.parametrize("attributed_turn", [(_INSIDE_BUILD,)], indirect=True)
def test_the_turn_records_the_visibility_bundle_builds_it_paid_for(attributed_turn):
    """The Stage 1 receipt reaches the durable record: 4 - 3.

    ``agent_runtime.chat_lane_bundle`` resolves the chat lane's visibility once
    per turn and reuses it while the lane's identity holds, so a warm
    steady-state turn should report ``0`` here and a turn that pays for a
    genuine change should report ``1``. Anything above ``1`` means something is
    re-resolving what the bundle was supposed to hold.
    """

    assert attributed_turn["visibility_bundle_builds"] == 1


def test_a_warm_turn_reports_a_HARD_zero_bundle_build_not_an_absence():
    """``0`` is the interesting answer and must be distinguishable from "could
    not ask" — the same rule ``registry_probe_rounds`` obeys."""

    marks = TurnPhaseMarks(monotonic=_TickClock(), wall_now=lambda: "stamp")
    marks.set_baseline("visibility_bundle_builds", 9)
    marks.count_delta("visibility_bundle_builds", 9)
    assert marks.snapshot()["visibility_bundle_builds"] == 0

    unknown = TurnPhaseMarks(monotonic=_TickClock(), wall_now=lambda: "stamp")
    unknown.set_baseline("visibility_bundle_builds", None)
    unknown.count_delta("visibility_bundle_builds", 4096)
    assert "visibility_bundle_builds" not in unknown.snapshot(), (
        "an unaskable counter must stay ABSENT, never report a lifetime total"
    )


@pytest.mark.parametrize("attributed_turn", [(_INSIDE_BUILD,)], indirect=True)
def test_a_freshly_constructed_agent_is_reported_COLD(attributed_turn):
    """``resident_actor_reused: 0`` is the runner saying it built one."""

    assert attributed_turn["agent_init_cold"] is True


@pytest.fixture
def warm_turn(monkeypatch, capsys, isolate_agent_runtime_root, empty_ledger):  # noqa: F811
    empty_ledger.record_build(started=_INSIDE_BUILD[0], ended=_INSIDE_BUILD[1])
    monkeypatch.setattr(
        mission_chat_phases,
        "TurnPhaseMarks",
        lambda: TurnPhaseMarks(monotonic=_TickClock(), wall_now=lambda: "stamp"),
    )
    harness = _seed(
        monkeypatch,
        _streaming_provider(profile_timing={"resident_actor_reused": 1}),
    )
    harness._cmd_mission_chat_message(_args("stage4_warm", stream=True))
    capsys.readouterr()
    return _record_on_disk(isolate_agent_runtime_root, "stage4_warm")[TURN_PHASES_KEY]


def test_a_reused_resident_actor_is_reported_WARM(warm_turn):
    assert warm_turn["agent_init_cold"] is False


@pytest.fixture
def unattributable_turn(monkeypatch, capsys, isolate_agent_runtime_root, empty_ledger):  # noqa: F811
    """A runner that reports no reuse timing at all, and an unseen build lane."""

    monkeypatch.setattr(
        mission_chat_phases,
        "TurnPhaseMarks",
        lambda: TurnPhaseMarks(monotonic=_TickClock(), wall_now=lambda: "stamp"),
    )
    harness = _seed(monkeypatch, _streaming_provider(profile_timing={}))
    harness._cmd_mission_chat_message(_args("stage4_blind", stream=True))
    capsys.readouterr()
    return _record_on_disk(isolate_agent_runtime_root, "stage4_blind")[TURN_PHASES_KEY]


def test_an_unreported_cold_warm_fact_is_ABSENT_not_false(unattributable_turn):
    """``False`` would claim the turn reused a resident actor. Nobody said that."""

    assert "agent_init_cold" not in unattributable_turn


def test_an_unobservable_build_lane_is_ABSENT_not_zero(unattributable_turn):
    """The ledger never saw a build in this process, so it makes no claim.

    This is the row that separates "builds are innocent" from "I was not
    looking" — the distinction the whole counter is for.
    """

    assert "builds_overlapped" not in unattributable_turn
