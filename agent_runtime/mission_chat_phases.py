"""Per-turn phase spans for one Mission Control chat turn (turn record v3).

**The question this answers.** "My messaging feels slow" was, until this
module, answerable only by hand-correlating four unstructured sources across
two clocks: ``agent.log`` prose, the turn record's wall stamps, the emitter's
``ttft_ms`` (whose clock starts LATE — see below), and the launcher's dateless
local-time diag lines. The turn record now carries the breakdown itself, so a
single turn answers "how much of this was hermes admission and how much was the
provider?" without dispatching an investigation.

**What a mark is.** One integer: elapsed **milliseconds from the monotonic
anchor** taken at command-handler entry. Nothing here is ever a wall-clock
delta, and no mark is ever subtracted from a mark taken in another process.
``anchored_at`` is the single wall stamp, and it exists for ONE purpose — to
cross-reference this turn against ``agent.log`` by eye. Subtracting it from
anything is a bug.

**Honesty contract** — inherited verbatim from
``lib/features/mission_control/data/mission_boot_timeline.dart`` in the
launcher, which is the reason the boot investigation worked:

1. **an unresolved phase is ABSENT, never a fake ``0``.** A turn that died
   before it reached the provider has NO ``provider_first_byte`` key. Not
   ``0``, not ``null``, not present-and-empty. This is the rule the whole plan
   exists to protect: a zero is a measurement, and a phase that never happened
   was not measured. :func:`safe_turn_phases` drops keys it cannot read rather
   than defaulting them, and :meth:`TurnPhaseMarks.snapshot` emits only what
   was marked.
2. **monotonic clock only.** ``time.monotonic`` by construction — the anchor
   and every mark come from the same injected callable, so a unit test drives
   the whole timeline with a scripted clock and a live turn cannot be poisoned
   by an NTP step.
3. **first mark wins.** ``provider_first_byte`` is marked from the emitter's
   ``delta()``, which fires once per token; a second mark must not move the
   first. Enforced here rather than at the call site, because "the call site
   remembered to guard" is not a property anything can assert.
4. **release-visible.** These are real accounting on the durable record, not a
   debug aid behind a flag. They ride the persists the turn already performs
   (write-ahead + terminal); this module adds no writes and no I/O of its own.

**Why the emitter's ``ttft_ms`` is not enough** (gap G1). The
``_ChatProtocolV2Emitter`` is constructed only AFTER replay checks, the native
history load, the turn-context build and the prompt-observability row build, so
its clock cannot see the several seconds of profile bootstrap that precede it
on a cold turn. ``phases`` is a SUPERSET of that measurement, not a
replacement: ``ttft_ms`` keeps its meaning and its origin.

**Cost.** One small dict and ~10 ``time.monotonic()`` reads per TURN. Nothing
per delta — the emitter's first-byte mark is guarded by a plain boolean read
before this module is ever entered.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

#: The one key this whole module adds to a turn record.
TURN_PHASES_KEY = "phases"

#: The turn-record schema version that carries :data:`TURN_PHASES_KEY`.
#:
#: A v2 record has no ``phases`` key and is NOT migrated: records already on
#: disk stay exactly as they were written, and every reader must tolerate their
#: absence. Bumping is how a reader tells "this turn predates phase spans" from
#: "this turn was instrumented and never reached that phase" — two different
#: facts that a silent version would collapse.
TURN_RECORD_SCHEMA_VERSION = 3

#: The elapsed-ms marks, in the order a healthy turn passes through them.
#:
#: ``request_received`` is 0 BY DEFINITION — it is the anchor, not a
#: measurement, and it is the only key here that is always present.
PHASE_ORDER: tuple[str, ...] = (
    "request_received",
    "context_built",
    "observability_built",
    "emitter_created",
    "write_ahead",
    "agent_ready",
    "provider_request_started",
    "provider_first_byte",
    "stream_done",
    "native_committed",
    "projected",
)

#: Booleans. Absent when the turn could not establish the fact honestly.
PHASE_FLAGS: tuple[str, ...] = ("agent_init_cold",)

#: Non-negative counts (Stage 4 attribution). Absent when unobservable — see
#: the field notes in :meth:`TurnPhaseMarks.count`.
PHASE_COUNTERS: tuple[str, ...] = ("registry_probe_rounds", "builds_overlapped")

#: Every key the block may carry, in the order a human reads the turn.
#: ``agent_init_cold`` sits next to ``agent_ready`` because it qualifies it.
#:
#: This is the order :meth:`TurnPhaseMarks.snapshot` builds and the order
#: :func:`safe_turn_phases` re-emits — NOT the order on disk. The turn store
#: serializes with ``sort_keys=True``, so a persisted block is alphabetical.
#: The join contract is the key NAMES and their meaning; nothing may depend on
#: ordering, and this tuple is also the closed set a reader is allowed to see.
_BLOCK_ORDER: tuple[str, ...] = (
    "request_received",
    "context_built",
    "observability_built",
    "emitter_created",
    "write_ahead",
    "agent_ready",
    "agent_init_cold",
    "provider_request_started",
    "provider_first_byte",
    "stream_done",
    "native_committed",
    "projected",
    "registry_probe_rounds",
    "builds_overlapped",
)

_KNOWN_MARKS = frozenset(PHASE_ORDER)
_KNOWN_FLAGS = frozenset(PHASE_FLAGS)
_KNOWN_COUNTERS = frozenset(PHASE_COUNTERS)

#: A turn is bounded by its wall budget (240 s by default, minutes at worst). A
#: "phase" a full day after the anchor is not a slow turn, it is a corrupt or
#: adversarial record, and admitting it would let one bad row blow up a
#: consumer's axis. Rejected on READ only — the writer cannot produce one.
_MAX_ELAPSED_MS = 24 * 60 * 60 * 1000

#: Same reasoning for the counters: a plausible ceiling, not a semantic one.
_MAX_COUNT = 1_000_000

_ANCHORED_AT_KEY = "anchored_at"
_ANCHORED_AT_MAX_CHARS = 80


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


class TurnPhaseMarks:
    """The turn's monotonic timeline. Created ONCE, at command-handler entry.

    Construction IS the anchor: there is no separate ``start()`` to forget, and
    no window in which a mark could be taken against an anchor that does not
    exist yet. The instance is carried down the turn on the (frozen) turn plan
    — it is an instrument, never a decision, so nothing downstream may read a
    mark back out to change what the turn does.
    """

    __slots__ = (
        "_monotonic",
        "_anchor",
        "_anchored_at",
        "_marks",
        "_flags",
        "_counters",
        "_baselines",
        "_lock",
    )

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_now: Callable[[], str] = _utc_now_iso,
    ) -> None:
        self._monotonic = monotonic
        self._anchor = float(monotonic())
        self._anchored_at = str(wall_now())
        self._marks: dict[str, int] = {"request_received": 0}
        self._flags: dict[str, bool] = {}
        self._counters: dict[str, int] = {}
        self._baselines: dict[str, int] = {}
        # Marks are taken from the turn thread with ONE exception —
        # ``provider_first_byte`` rides the stream callback, which the runtime
        # may deliver from a worker thread. The lock is per-PHASE (about eleven
        # acquisitions for a whole turn), not per delta, so first-mark-wins is
        # an actual guarantee rather than a benign-looking race.
        self._lock = threading.Lock()

    # ── reading ────────────────────────────────────────────────────────────

    @property
    def anchored_at(self) -> str:
        """The one wall stamp. For eyeballing ``agent.log``; never arithmetic."""

        return self._anchored_at

    @property
    def anchor_monotonic(self) -> float:
        """The raw monotonic origin, for callers that must compare SPANS.

        Stage 4's ``builds_overlapped`` needs "did this build's span intersect
        the turn's?", which is an interval question and cannot be answered from
        elapsed-ms alone. Exposed deliberately and used only for that.
        """

        return self._anchor

    def get(self, name: str) -> int | None:
        """The standing value of one mark, or ``None`` if it was never taken.

        The ONLY read-back the turn does, and it exists for one caller: Stage
        4's overlap window needs ``stream_done``. Nothing may branch on a mark
        — an instrument that steers the thing it measures is not an instrument.
        """

        return self._marks.get(name)

    # ── writing ────────────────────────────────────────────────────────────

    def mark(self, name: str) -> int:
        """Record ``name`` at NOW. First mark wins; returns the standing value.

        Raises on an unknown phase name on purpose. Every call site passes a
        literal from :data:`PHASE_ORDER`, so a typo is a coding error that must
        surface in the test run — the alternative (silently accepting it) is a
        phase that vanishes from the record with nothing to show it ever
        existed, which is precisely the failure this instrument exists to make
        impossible.
        """

        if name not in _KNOWN_MARKS:
            raise ValueError(f"unknown turn phase {name!r}")
        with self._lock:
            existing = self._marks.get(name)
            if existing is not None:
                return existing
            value = self._elapsed_ms_now()
            self._marks[name] = value
            return value

    def flag(self, name: str, value: bool | None) -> None:
        """Record a boolean qualifier, or record NOTHING when it is unknown.

        ``value=None`` is the honest "I could not establish this" and leaves the
        key absent. ``agent_init_cold`` uses it: the cold/warm fact comes from
        the runner's ``resident_actor_reused`` timing, and a turn whose runner
        never reported one must not be described as either.
        """

        if name not in _KNOWN_FLAGS:
            raise ValueError(f"unknown turn phase flag {name!r}")
        if value is None:
            return
        with self._lock:
            self._flags.setdefault(name, bool(value))

    def count(self, name: str, value: int | None) -> None:
        """Record a Stage 4 count, or record NOTHING when it is unobservable.

        Same rule as :meth:`flag`, and it matters more here because ``0`` is a
        legitimate, INTERESTING answer for both counters ("no probe rounds",
        "no builds overlapped this turn"). If "unobservable" were also written
        as ``0`` the two would be indistinguishable and the attribution these
        counters exist for would be worthless.
        """

        if name not in _KNOWN_COUNTERS:
            raise ValueError(f"unknown turn phase counter {name!r}")
        if value is None:
            return
        try:
            coerced = int(value)
        except (TypeError, ValueError):
            return
        if coerced < 0:
            return
        with self._lock:
            self._counters.setdefault(name, coerced)

    def set_baseline(self, name: str, value: int | None) -> None:
        """Pin the NEAR end of a cumulative counter, at the anchor.

        Some Stage 4 inputs are process- or thread-cumulative counters that
        nothing may reset (two overlapping observers resetting a shared counter
        would destroy each other's measurement). The turn's number is therefore
        a difference, and a difference needs both ends: this is the end that has
        to be taken before the turn does any work. ``None`` pins nothing, and
        :meth:`count_delta` then records nothing — the counter stays ABSENT
        rather than reporting the process's lifetime total as this turn's.
        """

        if name not in _KNOWN_COUNTERS:
            raise ValueError(f"unknown turn phase counter {name!r}")
        if value is None:
            return
        try:
            coerced = int(value)
        except (TypeError, ValueError):
            return
        with self._lock:
            self._baselines.setdefault(name, coerced)

    def count_delta(self, name: str, current: int | None) -> None:
        """Record ``current - baseline``. Records nothing if either end is unknown."""

        if current is None:
            return
        with self._lock:
            baseline = self._baselines.get(name)
        if baseline is None:
            return
        try:
            coerced = int(current)
        except (TypeError, ValueError):
            return
        self.count(name, max(0, coerced - baseline))

    # ── serialization ──────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """The ``phases`` block as it goes onto the record.

        Cumulative and idempotent: every persist writes the fullest block known
        at that moment, and a later persist simply carries more keys. Nothing
        that was not marked appears. Never raises — an instrument may not be
        the reason a turn fails to persist.
        """

        with self._lock:
            marks = dict(self._marks)
            flags = dict(self._flags)
            counters = dict(self._counters)
        block: dict[str, Any] = {_ANCHORED_AT_KEY: self._anchored_at}
        for key in _BLOCK_ORDER:
            if key in marks:
                block[key] = marks[key]
            elif key in flags:
                block[key] = flags[key]
            elif key in counters:
                block[key] = counters[key]
        return block

    # ── internals ──────────────────────────────────────────────────────────

    def _elapsed_ms_now(self) -> int:
        # Clamped at zero rather than trusted: an injected test clock is free to
        # be silly, and a negative "elapsed" is not a fact about any turn.
        return max(0, int((float(self._monotonic()) - self._anchor) * 1000))


def safe_turn_phases(value: Any) -> dict[str, Any] | None:
    """Sanitize a ``phases`` block for the durable record. ``None`` = no block.

    Defensive in ONE direction only: it drops what it cannot read. It never
    supplies a default, never coerces an absence into ``0``, and never invents
    ``request_received`` for a record that does not carry one — a record
    written before phase spans existed reads back as having none, which is the
    truth about it.
    """

    if not isinstance(value, dict):
        return None
    block: dict[str, Any] = {}
    anchored_at = value.get(_ANCHORED_AT_KEY)
    if isinstance(anchored_at, str):
        text = anchored_at.strip()[:_ANCHORED_AT_MAX_CHARS]
        if text:
            block[_ANCHORED_AT_KEY] = text
    for key in _BLOCK_ORDER:
        if key not in value:
            continue
        raw = value[key]
        if key in _KNOWN_FLAGS:
            if isinstance(raw, bool):
                block[key] = raw
            continue
        ceiling = _MAX_ELAPSED_MS if key in _KNOWN_MARKS else _MAX_COUNT
        # bool is an int subclass; a True that landed in an elapsed-ms slot is
        # corruption, not a one-millisecond phase.
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        coerced = int(raw)
        if coerced < 0 or coerced > ceiling:
            continue
        block[key] = coerced
    return block or None
