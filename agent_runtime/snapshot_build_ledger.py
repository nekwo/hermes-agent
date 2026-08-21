"""In-process spans of the snapshot builds this process actually LED.

**Why this exists.** Snapshot builds were observed interleaving every sampled
chat turn (2.2–7.9 s each, waits up to 9.4 s), which makes "builds are stealing
turn time" a plausible remedy candidate — and the warm turn that stayed fast
*with* a concurrent build makes it an equally plausible red herring. Neither
reading can be settled by correlation in a log, so the turn record counts the
overlap itself (``phases.builds_overlapped``). This is the counting side of
that: the builds, as spans, on the same monotonic clock a turn's anchor is
taken from.

**Scope is deliberately the PROCESS.** ``harness serve`` runs the chat turn and
the stream hub's builds in one process, so a process-local ring answers the
question for the lane that motivated it. A build in some *other* process is
invisible here, and this module does not pretend otherwise — see
:func:`overlapping_builds`, which returns ``None`` rather than ``0`` for a
process that has never built anything. "No builds overlapped" and "I cannot see
builds from here" are different facts, and a chat turn running in a CLI child
would report the first while meaning the second.

**Cost.** Two ``time.monotonic()`` reads and one bounded-deque append per BUILD
— a build costs seconds, so the instrument is free by several orders of
magnitude. Nothing here touches disk.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

#: Enough history to cover any single turn's window several times over (a turn
#: is bounded by its wall budget; builds are seconds apart at worst). Bounded so
#: a long-lived serve process cannot grow this without limit.
_MAX_SPANS = 256

_lock = threading.Lock()
_spans: deque[tuple[float, float]] = deque(maxlen=_MAX_SPANS)
#: Has this process ever LED a build? Distinct from ``len(_spans)`` because the
#: ring evicts; once true it stays true. This is the flag that separates "none
#: overlapped" from "not observable here".
_observed_any = False


def record_build(*, started: float, ended: float) -> None:
    """Record one LED build's span. Never raises.

    ``started``/``ended`` are ``time.monotonic()`` readings taken by the build
    thread itself. Only the LEADER records: a caller that rode somebody else's
    build did not pay for a second one, and counting its wait here would double
    every coalesced build.
    """

    global _observed_any
    try:
        start = float(started)
        end = float(ended)
    except (TypeError, ValueError):
        return
    if end < start:
        return
    with _lock:
        _observed_any = True
        _spans.append((start, end))


def overlapping_builds(*, start: float, end: float) -> int | None:
    """How many recorded builds intersect ``[start, end]``.

    ``None`` when this process has never led a build — the honest answer for a
    process that cannot see the lane at all. ``0`` when it has, and none of them
    touched this window: that is a measurement, and it is the answer that
    acquits build contention for a turn.

    Intersection is inclusive on both ends: a build that finished exactly as the
    turn's anchor was taken shares no time with it in any meaningful sense, but
    the millisecond resolution of everything around this makes the closed
    interval the safer, more conservative reading.
    """

    try:
        window_start = float(start)
        window_end = float(end)
    except (TypeError, ValueError):
        return None
    if window_end < window_start:
        return None
    with _lock:
        if not _observed_any:
            return None
        spans = list(_spans)
    return sum(
        1
        for build_start, build_end in spans
        if build_end >= window_start and build_start <= window_end
    )


def build_span_scope(recorder: Any = None):
    """Context manager that times a build and records it on exit.

    Returned rather than inlined at the call site so the two ``monotonic``
    reads and the "record even when the build raised" decision live in one
    place. A build that raised IS recorded: it occupied the process for that
    span whether or not it produced a core, and a turn that overlapped it paid
    the same price.
    """

    return _BuildSpan(recorder or record_build)


class _BuildSpan:
    __slots__ = ("_recorder", "_started")

    def __init__(self, recorder: Any) -> None:
        self._recorder = recorder
        self._started = 0.0

    def __enter__(self) -> "_BuildSpan":
        self._started = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self._recorder(started=self._started, ended=time.monotonic())
        except Exception:  # pragma: no cover - an instrument never fails a build
            pass
        return False


def reset_for_tests() -> None:
    """Drop all recorded spans AND the observed flag. Tests only."""

    global _observed_any
    with _lock:
        _spans.clear()
        _observed_any = False
