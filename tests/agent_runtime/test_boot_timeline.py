"""The self-attributing boot timeline (``agent_runtime.boot_timeline``).

A cold serve boot has been recorded at >25s and cannot be reproduced on
demand, so the boot has to measure itself. These pin the properties an
operator reading a cold-boot log actually relies on: phases that are complete
and monotone, an interpreter term that is ABSENT rather than fabricated when
the platform will not report a process creation time, and durations that a
clock adjustment can never turn negative.
"""

import time

from agent_runtime.boot_timeline import BootTimeline


def test_phases_are_recorded_in_completion_order_and_sum_into_elapsed():
    timeline = BootTimeline(process_start_monotonic=time.monotonic() - 1.5)

    timeline.mark("imports_ms")
    # Measure the sleep instead of assuming it. Windows' timer granularity is
    # ~15.6ms, so `sleep(0.02)` routinely returns after 14ms of monotonic time
    # — a bare `>= 15` here failed on real runs while the instrument was
    # perfectly correct. The property is "the phase records the interval that
    # actually elapsed", which is what this now asserts.
    before = time.monotonic()
    time.sleep(0.02)
    slept_ms = (time.monotonic() - before) * 1000
    timeline.mark("sweep_ms")

    phases = timeline.phases()
    assert list(phases) == ["imports_ms", "sweep_ms"]
    # -2ms absorbs integer truncation plus the mark() call itself.
    assert phases["sweep_ms"] >= slept_ms - 2
    # Complete: every phase is inside the elapsed window, and the elapsed
    # window is inside the total (which also carries the interpreter term).
    assert sum(phases.values()) <= timeline.elapsed_ms() + 5
    assert timeline.elapsed_ms() <= timeline.total_ms()


def test_interpreter_term_is_the_gap_before_the_timeline_started():
    timeline = BootTimeline(process_start_monotonic=time.monotonic() - 2.0)

    interpreter = timeline.interpreter_ms
    assert interpreter is not None
    # ~2s of process life happened before this timeline began.
    assert 1900 <= interpreter <= 2200
    assert timeline.stamps()["interpreter_ms"] == interpreter
    assert timeline.total_ms() >= interpreter


def test_an_unresolvable_process_start_reports_no_interpreter_term(monkeypatch):
    """Absent means "not measured". A fabricated 0 would read as "the
    interpreter cost nothing", which is exactly the wrong conclusion on the
    boot this instrument exists for."""

    monkeypatch.setattr(
        "agent_runtime.boot_timeline._process_start_monotonic", lambda: None
    )
    timeline = BootTimeline()

    assert timeline.interpreter_ms is None
    assert "interpreter_ms" not in timeline.stamps()
    # Totals still work — they fall back to the timeline's own start.
    assert timeline.total_ms() >= 0


def test_a_repeated_phase_accumulates_instead_of_overwriting():
    timeline = BootTimeline(process_start_monotonic=time.monotonic())

    # TWO comparable intervals, so accumulate and overwrite give visibly
    # different answers: ~100ms vs ~50ms. The previous shape slept once and
    # asserted a fixed floor, which discriminated nothing — an implementation
    # that overwrote would have passed it just as happily — while still being
    # flaky against the platform's ~15.6ms timer granularity.
    time.sleep(0.05)
    timeline.mark("retry_ms")
    time.sleep(0.05)
    timeline.mark("retry_ms")

    # A boot that repeated a step paid for BOTH attempts; reporting only the
    # last one would understate the boot it is attributing.
    assert timeline.phases()["retry_ms"] >= 80


def test_a_process_start_in_the_future_is_refused(monkeypatch):
    """A clock that disagrees with itself is not evidence."""

    class _FutureProcess:
        def __init__(self, _pid):
            pass

        def create_time(self):
            return time.time() + 3600

    monkeypatch.setattr(
        "psutil.Process", _FutureProcess
    )
    from agent_runtime import boot_timeline as module

    assert module._process_start_monotonic() is None


def test_log_line_renders_every_stamp_as_one_greppable_line():
    timeline = BootTimeline(process_start_monotonic=time.monotonic() - 0.5)
    timeline.mark("imports_ms")

    line = timeline.log_line("harness serve boot timeline:")

    assert line.startswith("harness serve boot timeline: ")
    assert "\n" not in line
    for key, value in timeline.stamps().items():
        if key in {"elapsed_ms", "total_ms"}:
            continue  # they advance between the render and this assertion
        assert f"{key}={value}" in line
