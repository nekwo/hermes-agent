"""BW-0: ``interpreter_ms`` splits into named segments, and the split is exact.

Why this file exists. The 2026-08-17 cold Mission Control boot reported one
number for everything from process creation to the serve command's first
instruction — ``interpreter_ms=20421``, against a warm baseline of ~2,000 ms —
while everything AFTER that instruction was attributed phase by phase to 1,437 ms
in total. Two of the boot-window plan's optimisation stages aim at costs that
live inside the opaque 20 s, so the split lands before them.

**Every assertion here is on a value or a key set, never on elapsed time.** The
anchors are injected, so the expected numbers are computed in the test from
instants the production code cannot see — which is what makes a mutant that
stamps a plausible constant fail rather than pass.
"""

from __future__ import annotations

import pytest

from hermes_cli import _boot_clock


@pytest.fixture(autouse=True)
def _clean_anchors():
    """Every case owns the module globals outright.

    ``hermes_cli.main`` is imported by an autouse fixture in this directory's
    conftest, so the anchors are ALREADY set by the time any test here runs;
    without this reset the cases would be measuring the test session's own
    import instead of their fixtures.
    """

    _boot_clock.reset_for_tests()
    yield
    _boot_clock.reset_for_tests()


def _anchor(monkeypatch, **values) -> None:
    for name, value in values.items():
        monkeypatch.setattr(_boot_clock, name, value, raising=True)


# ---------------------------------------------------------------------------
# The derived spans
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "process_start, import_started, import_completed, main_entered, dispatch_reached,"
    " expected_boot, expected_import, expected_dispatch",
    [
        # A cold-boot-shaped set: a slow interpreter start, a slow module import,
        # and a very slow dispatch (the harness import + a bytecode sweep).
        # Every instant is a binary-exact fraction, so the expected millisecond
        # values are the arithmetic ones rather than a float-repr artefact.
        (1000.0, 1002.5, 1006.0, 1006.125, 1024.625, 2500, 3500, 18500),
        # A warm-boot-shaped set with DIFFERENT values for all three, so a
        # constant cannot satisfy both rows of this table.
        (500.0, 500.25, 500.875, 500.9375, 502.0625, 250, 625, 1125),
    ],
)
def test_each_segment_is_the_exact_span_between_its_two_anchors(
    monkeypatch,
    process_start,
    import_started,
    import_completed,
    main_entered,
    dispatch_reached,
    expected_boot,
    expected_import,
    expected_dispatch,
):
    """Anti-vacuity: the expected values are computed from the fixture's own
    injected instants, and the two rows disagree on all three, so neither a
    hardcoded constant nor a mis-derivation off the wrong pair of anchors passes
    both."""

    _anchor(
        monkeypatch,
        MAIN_IMPORT_STARTED=import_started,
        MAIN_IMPORT_COMPLETED=import_completed,
        MAIN_ENTERED=main_entered,
    )

    segments = _boot_clock.import_tax_segments(
        process_start=process_start, dispatch_reached=dispatch_reached
    )

    assert segments["interpreter_boot_ms"] == expected_boot
    assert segments["main_import_ms"] == expected_import
    assert segments["dispatch_ms"] == expected_dispatch


def test_the_segments_sum_to_the_whole_they_decompose(monkeypatch):
    """The point of the split: the parts account for ``interpreter_ms``.

    Anti-vacuity note. *Mutation:* derive ``main_import_ms`` from
    ``(dispatch_reached - import_started)`` — a plausible-looking off-by-one-anchor
    slip. *Probed field:* the SUM against an independently computed whole. The
    fixture leaves a deliberate 40 ms residue between ``MAIN_IMPORT_COMPLETED``
    and ``MAIN_ENTERED`` (the argparse-build gap), so the mis-derivation
    overshoots the whole and this assertion goes red; a test that only checked
    ``main_import_ms <= interpreter_ms`` would not notice.
    """

    process_start = 100.0
    dispatch_reached = 110.0
    import_completed = 104.0
    main_entered = 104.0625  # 62.5 ms of argparse-build gap, deliberately non-zero
    _anchor(
        monkeypatch,
        MAIN_IMPORT_STARTED=100.5,
        MAIN_IMPORT_COMPLETED=import_completed,
        MAIN_ENTERED=main_entered,
    )

    segments = _boot_clock.import_tax_segments(
        process_start=process_start, dispatch_reached=dispatch_reached
    )

    interpreter_ms = int((dispatch_reached - process_start) * 1000)
    accounted = (
        segments["interpreter_boot_ms"]
        + segments["main_import_ms"]
        + segments["dispatch_ms"]
    )
    expected_gap_ms = int((main_entered - import_completed) * 1000)
    # Three independent truncations to whole milliseconds, so allow 3 ms of
    # rounding — but not the 5,937 ms the mis-derivation would add.
    assert abs((interpreter_ms - accounted) - expected_gap_ms) <= 3, (
        interpreter_ms,
        segments,
        expected_gap_ms,
    )
    for key in ("interpreter_boot_ms", "main_import_ms", "dispatch_ms"):
        assert segments[key] <= interpreter_ms, key


# ---------------------------------------------------------------------------
# Absence means "not measured", never zero
# ---------------------------------------------------------------------------


def test_an_unmeasurable_segment_is_absent_rather_than_zero(monkeypatch):
    """Anti-vacuity: this is the case that kills ``emit 0 unconditionally``.

    *Mutation:* stamp every segment key with 0 when its anchors are missing.
    *Probed field:* key MEMBERSHIP, not the value — a fabricated 0 is
    indistinguishable from a genuinely instant phase, which is the whole reason
    ``interpreter_ms`` itself is absent rather than zero when psutil declines.
    The mutation cannot satisfy this by writing a value of any kind.
    """

    _anchor(
        monkeypatch,
        MAIN_IMPORT_STARTED=10.0,
        MAIN_IMPORT_COMPLETED=None,
        MAIN_ENTERED=None,
    )

    # No process-creation anchor (the psutil-declines platform) and no
    # completion/entry anchors (a process that never went through main()).
    segments = _boot_clock.import_tax_segments(
        process_start=None, dispatch_reached=12.0
    )

    assert "interpreter_boot_ms" not in segments
    assert "main_import_ms" not in segments
    assert "dispatch_ms" not in segments
    assert segments == {}


def test_a_negative_span_clamps_instead_of_reporting_nonsense(monkeypatch):
    """A clock that disagrees with itself is not evidence — clamp, never negate."""

    _anchor(monkeypatch, MAIN_IMPORT_STARTED=5.0, MAIN_IMPORT_COMPLETED=4.0)

    segments = _boot_clock.import_tax_segments(
        process_start=9.0, dispatch_reached=None
    )

    assert segments["interpreter_boot_ms"] == 0
    assert segments["main_import_ms"] == 0


# ---------------------------------------------------------------------------
# The two recorded durations (not spans)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("recorded", [0, 7, 9314])
def test_the_bytecode_sweep_duration_is_reported_verbatim(recorded):
    """Anti-vacuity: ``0`` is in the table on purpose.

    *Mutation:* report the sweep only when it is expensive (``if ms: ...``).
    *Probed field:* the key's presence with a value of 0 — the "sweep decided in
    no time that there was nothing to do" case, which is the NORMAL case and
    must still be distinguishable from "nobody measured".
    """

    _boot_clock.record_bytecode_sweep_ms(recorded)

    segments = _boot_clock.import_tax_segments(
        process_start=None, dispatch_reached=None
    )

    assert segments["bytecode_sweep_ms"] == recorded


def test_a_second_sweep_accumulates_rather_than_replacing():
    """Two sweeps in one process cost the sum, the same rule ``phases()`` keeps."""

    _boot_clock.record_bytecode_sweep_ms(120)
    _boot_clock.record_bytecode_sweep_ms(30)

    segments = _boot_clock.import_tax_segments(
        process_start=None, dispatch_reached=None
    )

    assert segments["bytecode_sweep_ms"] == 150


def test_the_harness_parser_duration_is_reported_and_is_not_the_sweep():
    """Two separately-named costs inside ``dispatch_ms``, distinguishable.

    Anti-vacuity: *Mutation:* record both durations into one global (a
    copy-paste slip). *Probed fields:* both keys, with DIFFERENT injected
    values — a single shared global cannot report 2200 and 40 at once.
    """

    _boot_clock.record_harness_parser_ms(2200)
    _boot_clock.record_bytecode_sweep_ms(40)

    segments = _boot_clock.import_tax_segments(
        process_start=None, dispatch_reached=None
    )

    assert segments["harness_parser_ms"] == 2200
    assert segments["bytecode_sweep_ms"] == 40


# ---------------------------------------------------------------------------
# The anchors themselves
# ---------------------------------------------------------------------------


def test_an_anchor_is_written_once_so_a_module_reload_cannot_move_it():
    """``hermes update`` reloads runtime modules in-process; a reload must not
    re-anchor a boot that happened minutes earlier."""

    _boot_clock.mark_main_import_started()
    first = _boot_clock.MAIN_IMPORT_STARTED
    assert first is not None

    _boot_clock.mark_main_import_started()

    assert _boot_clock.MAIN_IMPORT_STARTED == first


def test_the_real_process_sets_all_three_module_anchors():
    """The instrument is wired to the code path it claims to measure.

    Anti-vacuity: *Mutation:* delete one of ``main.py``'s three
    ``mark_*`` calls. *Probed field:* the anchor's value being non-None after a
    plain ``import hermes_cli.main`` + ``mark_main_entered`` — which no other
    test in this file can provide, because every other case injects its anchors.
    ``MAIN_ENTERED`` is checked separately below: importing the module cannot set
    it (it is written inside ``main()``), so asserting it here would be a lie.
    """

    import importlib
    import sys

    # main.py is already imported by this directory's autouse conftest fixture,
    # and the autouse reset above cleared the anchors it set — so re-run the
    # module's own two module-scope marks the way a fresh process would.
    module = sys.modules.get("hermes_cli.main") or importlib.import_module(
        "hermes_cli.main"
    )
    module._boot_clock.mark_main_import_started()
    module._boot_clock.mark_main_import_completed()

    assert _boot_clock.MAIN_IMPORT_STARTED is not None
    assert _boot_clock.MAIN_IMPORT_COMPLETED is not None
    assert _boot_clock.MAIN_ENTERED is None
    # The module's `_boot_clock` name must BE this module, not a copy — a
    # separate instance would make every anchor main.py writes invisible here
    # and to the serve child's frame.
    assert module._boot_clock is _boot_clock
