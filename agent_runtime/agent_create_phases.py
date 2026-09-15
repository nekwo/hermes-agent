"""Sub-phase spans for ONE ``runtime.agent.create``, as a LOG receipt.

**The question this answers.** ``perform_agent_create`` reports one number for
the whole mint — ``phases.instance_ms`` on the create result, which the
launcher's ``[MissionDropTiming]`` line echoes — and W3 arrived with that
number unattributable. Live receipts, 2026-08-22 14:50Z boot: the prewarm
warmed the whole persona catalog in ~1.2 s (its own ``persona_prewarm done``
lines land at boot, qa at 157 ms), and the first drop of the session STILL paid
``rpc_instance_ms=2030`` while the second, eleven seconds later, paid 78 ms. So
roughly two seconds of the first create is not filled by ``warm_persona_memos``
— and with one number for the whole mint there was no way to say WHICH of the
mint's half-dozen cost blocks owned them. This module splits the number.

**Why a log receipt and not new wire fields.** ``phases`` rides the create's RPC
result, and the lane one wave back learned what that costs: ``sections_ms`` was
added to the parity envelope as "an observability nicety" and turned out to ride
the hydrate frame the Launcher byte-pins, so it landed as a cross-stack fixture
change (``0e4567f5fd``). The create's ``phases`` block is the same shape of
hazard — a client-visible dict a launcher parser reads — so nothing here touches
it. These spans go to ``agent.log`` at INFO, beside ``snapshot_build_core`` and
``persona_prewarm done``, and join them on ``pid``.

**Honesty contract** — the same four rules ``mission_chat_phases`` inherited
from the launcher's ``mission_boot_timeline``:

1. **an unrecorded phase is ABSENT, never a fake ``0``.** A create that resumed
   a reservation minted nothing and therefore measured nothing, and its receipt
   says ``phases=-`` rather than a row of zeros. A phase that RAN and cost under
   a millisecond does report ``0`` — that is a measurement, and suppressing it
   would make a cheap block indistinguishable from a skipped one.
2. **monotonic clock only.** ``time.monotonic`` by construction; no span here is
   a wall-clock delta and none is ever subtracted from a mark taken in another
   process.
3. **accumulate, never overwrite.** A key timed across more than one span sums
   (``+=``), exactly as ``snapshot._timed_section`` does, so a block entered
   twice bills twice rather than silently reporting only its last visit.
4. **release-visible.** Ordinary INFO logging, no flag. ``hermes serve`` lands
   it in ``<HERMES_HOME>/logs/agent.log`` with nothing extra turned on.

**Nesting is deliberate and the order encodes it.** :data:`PHASE_ORDER` runs
outer-to-inner within each block, and three of the keys are strict subsets of a
fourth:

``create_patch_ms`` ⊃ ``wire_row_ms`` ⊃ {``permission_options_ms``,
``chat_lane_scope_ms``, ``tool_visibility_ms``}

Summing the printed keys therefore over-counts, and that is the correct trade:
the whole point is to say how much of the create's cost the wire-row projection
owns AND which of the projection's three reads owns it, and a flat partition
could only answer one of those. A reader adding the numbers up is reading the
line wrong; a reader comparing ``tool_visibility_ms`` against ``wire_row_ms``
against ``instance_ms`` is reading it right.

**Cost when nobody is recording.** Every span site is on a path other callers
share — ``persona_instance_summary`` runs once per persona per snapshot build,
seventeen times on the operator's roster — so the instrumentation must be free
for them. It is: :func:`timed_create_subphase` reads one
:class:`~contextvars.ContextVar` and returns without touching the clock when no
recorder is active. A recorder is only ever installed by
``perform_agent_create``, and a ``ContextVar`` is per-thread by construction, so
a concurrent snapshot build on another thread cannot land its spans in a
create's receipt.

**Timings only.** The receipt names the persona id it was asked to mint and
integers. Never a display name, never a resolved toolset, never anything the
projection read.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

logger = logging.getLogger(__name__)

#: One INFO line per completed mint, format-pinned by
#: ``tests/agent_runtime/test_agent_create_subphases.py``.
#:
#: ``instance_ms`` is repeated from the create's own result deliberately: the
#: receipt has to be readable on its own, and a reader who has to fetch the RPC
#: reply to learn what the sub-phases add up against would be back to the
#: cross-source correlation this line exists to end. It is the SAME number the
#: result carries, passed in — never a second measurement of the same span.
AGENT_CREATE_PHASES_RECEIPT = (
    "agent_create_phases persona=%s instance_ms=%d phases=%s pid=%d"
)

#: What a receipt prints when nothing was recorded at all. Spelled as a token
#: rather than an empty value for the same reason ``sections_top`` is: a parser
#: reading ``phases=`` off the line must never have to tell "no such key" apart
#: from "nothing ran".
NO_PHASES = "-"

#: Every span this module knows how to bill, in the order a healthy mint passes
#: through them, outer before inner. A key absent from a receipt was never
#: entered; a key absent from THIS tuple is a bug at the call site, and
#: :meth:`CreateSubphases.record` refuses it loudly rather than printing a span
#: nothing documents.
PHASE_ORDER: tuple[str, ...] = (
    # ``PersonaInstanceStore.add_instance`` — the pre-write refusals (sibling
    # steal, retirement tombstone), which scan the instance directory.
    "bindable_ms",
    # ``_durable_chat_root`` — the SessionDB row that makes the minted chat root
    # real. The one span here that is a cross-store write rather than a read.
    "chat_root_ms",
    # ``open_chat``'s row write (``PersonaInstanceStore.update``).
    "instance_write_ms",
    # ``emit_persona_instance_create`` end to end: projection, cap check, and the
    # ``state.patched`` append.
    "create_patch_ms",
    # ``project_persona_instance_full_wire_row`` alone — the projection inside
    # the patch emit, without the append.
    "wire_row_ms",
    # The three reads ``persona_instance_summary`` performs inside that
    # projection, which is where the prewarm expects to have left its memos.
    "permission_options_ms",
    "chat_lane_scope_ms",
    "tool_visibility_ms",
    # ``persona_instance.chat_opened`` — the plain domain-event append.
    "event_append_ms",
    # ``agent_create``'s own second store write, stamping ``spawned_by``.
    "spawned_by_write_ms",
)

_PHASE_KEYS = frozenset(PHASE_ORDER)


class CreateSubphases:
    """The spans recorded for one mint. Ordinary dict, guarded key set."""

    __slots__ = ("_spans",)

    def __init__(self) -> None:
        self._spans: dict[str, int] = {}

    def record(self, key: str, elapsed_ms: int) -> None:
        if key not in _PHASE_KEYS:
            raise KeyError(
                f"{key!r} is not a documented agent-create sub-phase; add it to "
                "PHASE_ORDER with a comment saying what it spans, or the receipt "
                "prints a number nothing explains"
            )
        self._spans[key] = self._spans.get(key, 0) + int(max(0, elapsed_ms))

    def snapshot(self) -> dict[str, int]:
        """Only what was recorded, in :data:`PHASE_ORDER`. Never a default."""

        return {key: self._spans[key] for key in PHASE_ORDER if key in self._spans}

    def formatted(self) -> str:
        """``name:ms,name:ms`` in :data:`PHASE_ORDER`, or :data:`NO_PHASES`.

        Same shape as ``snapshot._sections_top`` prints, on purpose: an operator
        reading ``agent.log`` should not have to learn a second grammar for the
        second family of timing receipts in the same file.
        """

        spans = self.snapshot()
        if not spans:
            return NO_PHASES
        return ",".join(f"{key}:{value}" for key, value in spans.items())


_ACTIVE: ContextVar[CreateSubphases | None] = ContextVar(
    "agent_create_subphases", default=None
)


@contextmanager
def using_create_subphases(recorder: CreateSubphases) -> Iterator[CreateSubphases]:
    """Make ``recorder`` the active one for the duration of the block.

    Takes the recorder rather than minting one so a mint whose spans are spread
    across two non-adjacent blocks — ``perform_agent_create`` bills
    ``add_instance`` and then, past a chain of typed refusal arms, the
    ``spawned_by`` write — can bill both into ONE receipt without wrapping the
    hundred-odd lines of refusal handling between them in an indentation level
    that says nothing about what it contains.

    Reset by token rather than by setting ``None`` back, so a nested activation
    restores the enclosing recorder instead of silently disabling it.
    """

    token = _ACTIVE.set(recorder)
    try:
        yield recorder
    finally:
        _ACTIVE.reset(token)


@contextmanager
def capture_create_subphases() -> Iterator[CreateSubphases]:
    """Install a FRESH recorder for the duration of one mint."""

    with using_create_subphases(CreateSubphases()) as recorder:
        yield recorder


@contextmanager
def timed_create_subphase(key: str) -> Iterator[None]:
    """Bill the wrapped block to ``key`` — or do nothing at all.

    The no-recorder arm returns before reading the clock, which is what makes
    this safe to leave on ``persona_instance_summary``: a snapshot build calls
    that function once per persona and must not pay for a create's instrument.

    Billed in ``finally``, so a block that RAISED still reports the time it
    burned. A create that failed inside the wire-row projection is exactly the
    case where the split is worth having.
    """

    recorder = _ACTIVE.get()
    if recorder is None:
        yield
        return
    started = time.monotonic()
    try:
        yield
    finally:
        recorder.record(key, int(max(0.0, time.monotonic() - started) * 1000))


def log_create_subphases(
    recorder: CreateSubphases, *, persona_id: str, instance_ms: int
) -> None:
    """Emit the receipt for one mint. Never raises, never measures."""

    logger.info(
        AGENT_CREATE_PHASES_RECEIPT,
        persona_id,
        int(instance_ms),
        recorder.formatted(),
        os.getpid(),
    )
