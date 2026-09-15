"""Monotonic anchors that split the CLI's own import tax into named segments.

Why this module exists (boot-window plan BW-0, 2026-08-17). The serve child's
``booting`` frame already carries ``interpreter_ms`` — process creation to the
first instruction of ``_cmd_serve`` — and on the 2026-08-17 cold boot that ONE
number was 20,421 ms against a warm baseline of ~2,000 ms. Everything after it
was attributed to the millisecond (``chat_registry_ms``, ``root_anchor_ms``,
``orphaned_turn_sweep_ms``, …, 1,437 ms in total); everything before it was one
opaque 20-second phase. **A phase nobody can attribute is a phase nobody can
fix**, and two of the plan's optimisation stages aim at numbers that live inside
it, so the split lands first and alone.

The anchors are plain module globals written by the four places that can see the
boundaries:

* ``MAIN_IMPORT_STARTED`` — ``hermes_cli/main.py``'s first statement. Everything
  before it is the interpreter itself plus ``site`` plus the ``hermes_cli``
  package import.
* ``MAIN_IMPORT_COMPLETED`` — the last statement of ``main.py``'s module scope.
  The span between the two is ``main.py``'s own ~450 module-scope imports.
* ``MAIN_ENTERED`` — the first statement of ``main()``.
* ``BYTECODE_SWEEP_MS`` / ``HARNESS_PARSER_MS`` — durations recorded by the two
  known-expensive steps between ``main()`` and the command's own entry: the
  launch-time stale-``__pycache__`` sweep, and the ``hermes_cli.harness`` import
  that pulls in the whole of ``agent_runtime`` (and, through
  ``tool_visibility`` → ``model_tools``, a full plugin-discovery walk) while the
  top-level argument parser is being assembled.

**Stdlib only, and it must stay that way.** ``main.py`` imports this at its very
top, so anything this module reaches for is paid before the first anchor is even
readable — which would put the measurement inside the thing being measured.

Absence means "not measured", never zero — the same contract
``agent_runtime.boot_timeline.interpreter_ms`` already keeps for the psutil
anchor it cannot always get. A fabricated zero is worse than a missing key:
a reader cannot tell it from a genuinely instant phase.

Nothing here is authority. Every value is additive observability, and every
writer below is written so that failing to write costs a missing key and
nothing else.
"""

from __future__ import annotations

import time

#: ``time.monotonic()`` at ``hermes_cli/main.py``'s first statement.
MAIN_IMPORT_STARTED: float | None = None

#: ``time.monotonic()`` at the end of ``hermes_cli/main.py``'s module scope.
MAIN_IMPORT_COMPLETED: float | None = None

#: ``time.monotonic()`` at the first statement of ``hermes_cli.main.main()``.
MAIN_ENTERED: float | None = None

#: Wall milliseconds spent in ``_sweep_stale_bytecode_if_checkout_changed``.
BYTECODE_SWEEP_MS: int | None = None

#: Wall milliseconds spent importing ``hermes_cli.harness`` and registering its
#: parser (``main.py``'s harness-parser block).
HARNESS_PARSER_MS: int | None = None


def mark_main_import_started() -> None:
    """Anchor ``main.py``'s module import. First write wins.

    First-write-wins rather than last: ``hermes update`` reloads a set of
    runtime modules in-process, and a reload must not re-anchor a boot that
    happened minutes ago to the moment of the reload.
    """

    global MAIN_IMPORT_STARTED
    if MAIN_IMPORT_STARTED is None:
        MAIN_IMPORT_STARTED = time.monotonic()


def mark_main_import_completed() -> None:
    """Anchor the end of ``main.py``'s module scope. First write wins."""

    global MAIN_IMPORT_COMPLETED
    if MAIN_IMPORT_COMPLETED is None:
        MAIN_IMPORT_COMPLETED = time.monotonic()


def mark_main_entered() -> None:
    """Anchor entry into ``main()``. First write wins."""

    global MAIN_ENTERED
    if MAIN_ENTERED is None:
        MAIN_ENTERED = time.monotonic()


def record_bytecode_sweep_ms(elapsed_ms: int) -> None:
    """Record what the launch-time bytecode sweep cost.

    Accumulates rather than overwrites, for the same reason
    ``BootTimeline.phases()`` does: a process that somehow sweeps twice should
    report the whole cost of sweeping, not the cheaper of the two attempts.
    """

    global BYTECODE_SWEEP_MS
    BYTECODE_SWEEP_MS = int(max(0, elapsed_ms)) + int(BYTECODE_SWEEP_MS or 0)


def record_harness_parser_ms(elapsed_ms: int) -> None:
    """Record what importing ``hermes_cli.harness`` + parser registration cost."""

    global HARNESS_PARSER_MS
    HARNESS_PARSER_MS = int(max(0, elapsed_ms)) + int(HARNESS_PARSER_MS or 0)


def import_tax_segments(
    *,
    process_start: float | None,
    dispatch_reached: float | None,
) -> dict[str, int]:
    """The named segments of ``interpreter_ms``, as far as they were measured.

    ``process_start`` is the psutil-derived process-creation instant on the
    monotonic clock (``BootTimeline.process_start_monotonic``) and
    ``dispatch_reached`` is the instant the command's own timeline started
    (``BootTimeline.started_monotonic``). Together with the module anchors above
    they close the gap:

    ``interpreter_boot_ms + main_import_ms + <argparse-build residue>
    + dispatch_ms ≈ interpreter_ms``

    Every key is omitted when either of its two endpoints is missing, so a
    platform that will not report a creation time loses ``interpreter_boot_ms``
    and keeps the rest instead of reporting a nonsense span. Spans are clamped
    at zero — a clamped 0 beats a nonsense -3, the same rule
    ``boot_timeline._ms`` already applies.

    ``bytecode_sweep_ms`` and ``harness_parser_ms`` are DURATIONS, not spans:
    they need no endpoints and are simply passed through when recorded. Both sit
    INSIDE ``dispatch_ms``; they are reported beside it because the sum is the
    point — a ``dispatch_ms`` of 18 s that is 0.3 s of sweep and 17 s of harness
    import calls for a different fix than the reverse.
    """

    segments: dict[str, int] = {}
    if process_start is not None and MAIN_IMPORT_STARTED is not None:
        segments["interpreter_boot_ms"] = _ms(MAIN_IMPORT_STARTED - process_start)
    if MAIN_IMPORT_STARTED is not None and MAIN_IMPORT_COMPLETED is not None:
        segments["main_import_ms"] = _ms(MAIN_IMPORT_COMPLETED - MAIN_IMPORT_STARTED)
    if MAIN_ENTERED is not None and dispatch_reached is not None:
        segments["dispatch_ms"] = _ms(dispatch_reached - MAIN_ENTERED)
    if BYTECODE_SWEEP_MS is not None:
        segments["bytecode_sweep_ms"] = int(BYTECODE_SWEEP_MS)
    if HARNESS_PARSER_MS is not None:
        segments["harness_parser_ms"] = int(HARNESS_PARSER_MS)
    return segments


def reset_for_tests() -> None:
    """Clear every anchor. Tests only — production has one boot per process."""

    global MAIN_IMPORT_STARTED, MAIN_IMPORT_COMPLETED, MAIN_ENTERED
    global BYTECODE_SWEEP_MS, HARNESS_PARSER_MS
    MAIN_IMPORT_STARTED = None
    MAIN_IMPORT_COMPLETED = None
    MAIN_ENTERED = None
    BYTECODE_SWEEP_MS = None
    HARNESS_PARSER_MS = None


def _ms(seconds: float) -> int:
    """Whole milliseconds, never negative (mirrors ``boot_timeline._ms``)."""

    return int(max(0.0, seconds) * 1000)
