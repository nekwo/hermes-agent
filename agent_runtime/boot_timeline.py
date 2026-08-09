"""Self-attributing boot timeline for the ``hermes harness serve`` child.

Why this exists (T5/T9 of the 2026-08-09 Mission Control startup analysis):
a cold serve boot has been recorded at >25s while a warm one is ~1.6s, and
NOTHING in the protocol said where that time went — the supervising launcher
saw only ``booting`` and, eventually, ``ready``. Reproducing a true cold boot
means evicting the OS file cache for a whole checkout, so the honest fix is not
a repro but an instrument: every boot stamps its own phase durations into the
frames it already emits, and logs one structured line. The next real cold boot
in the operator's log then attributes itself with zero repro effort.

Contract:

- Phases are recorded as they COMPLETE (``mark``), so a boot that wedges before
  ``ready`` still carries everything it did finish.
- Durations are whole milliseconds measured on ``time.monotonic`` — never
  wall-clock deltas, which a clock adjustment can make negative.
- ``interpreter_ms`` (process creation → the first hermes code that runs in
  this module's caller) is resolved through psutil and is simply ABSENT when
  the platform will not give a creation time. Absent means "not measured";
  it never reports a fabricated zero.
- Every value is additive protocol surface: consumers that predate a phase
  ignore unknown keys.
"""

from __future__ import annotations

import os
import time
from typing import Any


def _process_start_monotonic() -> float | None:
    """Best-effort ``time.monotonic`` reading of this process's creation.

    psutil reports creation as a wall-clock epoch; the phase clock is
    monotonic, so translate once through the current offset between the two
    rather than mixing the two clocks per phase.
    """

    try:
        import psutil

        created_epoch = float(psutil.Process(os.getpid()).create_time())
    except Exception:
        return None
    now_epoch = time.time()
    if created_epoch <= 0 or created_epoch > now_epoch:
        # A clock that disagrees with itself is not evidence; report nothing.
        return None
    return time.monotonic() - (now_epoch - created_epoch)


class BootTimeline:
    """Ordered, append-only wall-time attribution of ONE process boot."""

    __slots__ = ("_started", "_last", "_phases", "_process_start")

    def __init__(self, *, process_start_monotonic: float | None = None) -> None:
        self._started = time.monotonic()
        self._last = self._started
        self._phases: list[tuple[str, int]] = []
        self._process_start = (
            process_start_monotonic
            if process_start_monotonic is not None
            else _process_start_monotonic()
        )

    @property
    def interpreter_ms(self) -> int | None:
        """Process creation → this timeline's start (interpreter + CLI import).

        On a cold boot this is the dominant term, and it is the one a
        supervising launcher cannot see any other way.
        """

        if self._process_start is None:
            return None
        return _ms(self._started - self._process_start)

    def mark(self, phase: str) -> int:
        """Record [phase] as having just completed; return its duration in ms."""

        now = time.monotonic()
        duration = _ms(now - self._last)
        self._last = now
        self._phases.append((phase, duration))
        return duration

    def elapsed_ms(self) -> int:
        """Milliseconds since this timeline started (excludes interpreter)."""

        return _ms(time.monotonic() - self._started)

    def total_ms(self) -> int:
        """Milliseconds since process creation when known, else since start."""

        anchor = self._process_start if self._process_start is not None else self._started
        return _ms(time.monotonic() - anchor)

    def phases(self) -> dict[str, int]:
        """Recorded phases in completion order.

        A phase recorded twice ACCUMULATES rather than overwriting, so a boot
        that repeats a step reports the whole cost of that step instead of
        only its last attempt.
        """

        merged: dict[str, int] = {}
        for name, duration in self._phases:
            merged[name] = merged.get(name, 0) + duration
        return merged

    def stamps(self) -> dict[str, Any]:
        """The additive frame block: phases + interpreter + totals."""

        block: dict[str, Any] = {}
        interpreter = self.interpreter_ms
        if interpreter is not None:
            block["interpreter_ms"] = interpreter
        block.update(self.phases())
        block["elapsed_ms"] = self.elapsed_ms()
        block["total_ms"] = self.total_ms()
        return block

    def log_line(self, label: str) -> str:
        """One structured line for ``agent.log`` — greppable, copy-pasteable."""

        stamps = self.stamps()
        rendered = " ".join(f"{key}={value}" for key, value in stamps.items())
        return f"{label} {rendered}"


def _ms(seconds: float) -> int:
    """Whole milliseconds, never negative (a clamped 0 beats a nonsense -3)."""

    return int(max(0.0, seconds) * 1000)
