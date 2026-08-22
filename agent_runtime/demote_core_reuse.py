"""One demote build's core, reused by the next demote build at the same offset.

**The waste this removes, measured.** Live serve, 2026-08-22 10:50 local, pid
22588 (``profiles/base/logs/agent.log``): THREE ``snapshot_build reason=demote
role=led`` lines at the SAME event-log offset 89961793 — generations 17, 18 and
19, ``build_ms`` 3017 / 3210 / 2388, ``waited_ms`` 5452 / 5686 / 5453, callers
hub / cli / hub. All three wrote the core back, and ``core_cache``'s own receipt
proves they were the same core: ``snapshot_core_cache_write ok=true …
fingerprint=9d655b54f622 offset=89961793``, three times, byte-identical key.
Six such pairs appear in the two minutes around that drop (offsets 89961793,
89962008, 89974683, 89975133, 89978732), and the launcher's
``roster_confirmed=8309ms`` — the operator's felt wait for a dropped agent to
appear in the roster — is the fold sitting behind them.

**Why the existing coalescer does not catch it.** ``build_snapshot``'s
coalescer is deliberately STRICT: a caller that arrives while a build is running
waits for the NEXT build, never the in-flight one, because "an in-flight build
began earlier and may miss writes this caller has already observed". That rule
is right, and it is exactly what serialises three arrivals into three builds.
The riders coalesce WITHIN one build; the waste here is SEQUENTIAL, between
builds.

**What makes reuse safe when riding an in-flight build is not.** The strict rule
protects against a build whose content may predate the caller's arrival — a
claim about time. This module makes a claim about POSITION instead, and position
is checkable: a core stamps ``parity.watermark.event_offset``, captured BEFORE
the build starts reading (``parity.events_watermark``'s "offset captured before
the content" direction), so it is a lower bound on what the core contains. When
the event log's end offset RIGHT NOW equals that number, nothing has been
appended from before the build began until this instant — so the core is not
merely fresh enough, it is complete for this position. There is no window for a
write to have slipped between, which is the only thing the strict rule was
protecting.

That is the same authority and the same posture R1 gave ``core_cache``'s consult
memo (``core_cache._store_position`` / ``_stamp_still_stands``): equality of the
store position, and an UNKNOWN position on either side refuses rather than
comparing two ``None``\\ s as "nothing moved". An install whose log cannot be
stat'd rebuilds every time — the expensive direction, deliberately.

**It is a reuse of the CORE, never a dedupe of emissions.** The BO-1
convergence-pair invariant (``73da…``/``73d06f777f``) binds here: every consumer
still receives its own complete frame, built by ``delta_batch_frame`` from its
own batch. What is shared is the expensive dict inside it, and each consumer
gets its own deep copy of that — the build coalescer already deep-copies for
precisely this reason (``label_core`` stamps provenance IN PLACE, so one shared
dict would let a later consumer's label land on an already-emitted frame).

**Provenance is labelled, and the label is not a zero.** The reusing caller's
``snapshot_build`` receipt carries ``core_source=reused_same_offset`` — the same
``core_source=`` key ``snapshot_core_cache`` lines spell, whose existing values
are ``cache`` and ``rebuilt``. ``build_ms`` on that line stays the ORIGINAL
build's cost, read off the reused core's own envelope: it is the build
underneath the frame, and printing ``build_ms=0`` would be a lie about a build
that really did cost three seconds. ``waited_ms`` is the reusing caller's own
wait, which is genuinely near zero — that is the number the remedy moves.
"""

from __future__ import annotations

import copy
import threading
from typing import Any

from . import paths
from .parity import events_position

#: The provenance token for a core this module handed back, on the reusing
#: caller's ``snapshot_build`` receipt. Spelled in the ``core_source`` vocabulary
#: ``core_cache`` established (``cache`` / ``rebuilt``) rather than in a second
#: grammar, so "where did this core come from" has ONE key across both
#: families and a reader who knows one line can read the other.
#:
#: It cannot be miscounted as a cache verdict: ``core_cache_census`` anchors on
#: the ``snapshot_core_cache`` FAMILY token before it looks at ``core_source=``
#: at all, and this value only ever appears on a ``snapshot_build`` line.
CORE_SOURCE_REUSED_SAME_OFFSET = "reused_same_offset"

_lock = threading.Lock()

#: ``(store_root, position, core)`` — at most one, always the most recent demote
#: build's. One entry rather than a map because the only key that can ever match
#: is the CURRENT log end, and offsets never go backwards: an entry the position
#: has moved past can never become valid again, so it is dropped on the consult
#: that rejects it instead of being held for a match that cannot arrive.
#:
#: The ROOT is in the key for the same reason ``core_cache`` carries
#: ``fingerprint_home``: a position is a number, and two different stores can
#: hold the same one — an empty log answers ``0`` under every root there is. A
#: process that moved roots (every test in this tree does) would otherwise be
#: offered a core built from a store it is no longer reading.
_entry: tuple[str, int, dict[str, Any]] | None = None


def _store_root() -> str | None:
    """Which store the held core is ABOUT. ``None`` when it cannot be resolved.

    Never raises: this module is an optimisation, and a root that cannot be
    resolved must cost a rebuild, never the frame.
    """

    try:
        return str(paths.store_root())
    except Exception:  # noqa: BLE001
        return None


def _core_position(core: Any) -> int | None:
    """The offset a core says its own content reaches, or ``None``.

    ``None`` for a core with no watermark and it must stay ``None`` rather than
    ``0``: zero is a real position, and a core that claimed it would be treated
    as matching an empty log.
    """

    if not isinstance(core, dict):
        return None
    parity = core.get("parity")
    if not isinstance(parity, dict):
        return None
    watermark = parity.get("watermark")
    if not isinstance(watermark, dict):
        return None
    value = watermark.get("event_offset")
    return None if value is None else int(value)


def remember(core: Any) -> bool:
    """Hold ``core`` for the next demote build, keyed on its OWN position.

    Keyed on the core's watermark and never on a stat taken after the build.
    A post-build stat would absorb any append that landed while the build ran,
    and a later reuse at that position would then serve a core the events do not
    appear in — "authoritative for an offset it predates", which is the shape
    MCF-Q1 exists to refuse and which erased a just-created agent from Mission
    Control on 2026-08-21. The pre-build capture can only be too OLD, and too
    old means a rebuild nobody strictly needed.

    Returns whether anything was held, so a caller can tell "we chose not to"
    from "we did". A core with no readable position holds nothing.
    """

    global _entry
    position = _core_position(core)
    if position is None or not isinstance(core, dict):
        return False
    root = _store_root()
    if root is None:
        return False
    held = copy.deepcopy(core)
    with _lock:
        _entry = (root, position, held)
    return True


def consult(*, floor: int | None = None) -> dict[str, Any] | None:
    """The held core if the log has not moved since it was built, else ``None``.

    ``floor`` is the offset the resulting frame is about to be STAMPED with (the
    batch's last event offset). A core at or above it is what MCF-Q1 requires,
    and the equality check below already implies it — the batch's events are in
    the log, so its last offset cannot exceed the log's end. The check is here
    anyway, because a guarantee that holds only "by construction" is one
    refactor away from being absent, and this one is cheap.

    An unreadable log position refuses. Two unknowns comparing equal would read
    as "the log did not move" when what they say is "nobody could tell" — the
    fail-quiet default ``parity.events_watermark`` exists to forbid.
    """

    global _entry
    with _lock:
        entry = _entry
        if entry is None:
            return None
        root, position, core = entry
        if _store_root() != root:
            # A different store answers a different question; the held core is
            # not stale for THIS root, it is simply about another one.
            _entry = None
            return None
        now = events_position().get("event_offset")
        if now is None or int(now) != position:
            # Offsets only grow, so an entry the log has moved past can never
            # match again. Dropped here rather than kept, which also bounds this
            # module's memory to one core between two same-offset builds instead
            # of holding a stale one until the next demote happens by.
            _entry = None
            return None
        if floor is not None and position < int(floor):
            _entry = None
            return None

        # Every caller gets its OWN copy. The held one is never handed out, so a
        # consumer that stamps provenance in place cannot reach back into a
        # frame another consumer already emitted.
        return copy.deepcopy(core)


def reset_process_state() -> None:
    """Forget the held core. Process state, so tests reset it like the cache lane."""

    global _entry
    with _lock:
        _entry = None
