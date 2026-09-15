"""Server-side watermark resume: the tail a reconnecting client actually needs.

Gateway Stage 2 shipped resume as a CLIENT-side memory and recorded the gap in
its own field notes: ``{"op":"subscribe","lane":"stream"}`` takes no resume
parameter, so hermes answers every reattach with a fresh hydrate and the
connector's remembered watermark only ever fed its own ``>``-gate. On a desktop
that is a rebuild nobody times. On a PHONE it is the R7 line: a ~1 MB core on
every foreground resume, over WiFi, on a battery — which is why the plan names
watermark resume as the mitigation to ship BEFORE any snapshot diet.

What a resume is, and what it deliberately is not
------------------------------------------------

It is the journal's tail from the client's own position, expressed in frames the
client already folds — ordinary v2 ``patch`` frames, chained
``base_offset``→``watermark`` exactly as the live lane chains them. It is NOT a
new fold contract, NOT a new frame type, and NOT a second projection: a resumed
client folds the same rows it would have folded had it stayed connected, and the
span it missed is the span it is sent.

**The empty span is the point.** A phone that backgrounds for ninety seconds on a
quiet runtime comes back at a watermark that IS the tail, and the honest answer
to "what did I miss" is nothing at all — zero frames, zero bytes, instead of a
megabyte re-stating a projection the client already holds.

Every reason it refuses
-----------------------

A resume is honoured only when the answer can be proven complete, and every
other case takes the full hydrate — which is not a failure, it is the correct
fallback, and it is NAMED on the ack rather than left to be inferred:

* ``patch_lane_disabled`` — with ``read_model.delta_patches`` off there is no
  patch frame to express a span with. The core is the only vocabulary there is.
* ``journal_unreadable`` — the tail could not be stat'ed, or the live slice is
  gone. An unknown position is not byte 0 (the rule ``stream_frames`` records
  for its own resume) and it is not a floor either.
* ``journal_truncated`` — the client's position is older than the oldest slice
  still on disk. The span exists in an archive this reader does not open, and
  serving the part that survives would be a contiguous-looking frame with a hole
  in it. See :func:`~agent_runtime.event_rotation.resume_floor_offset`, which
  exists for exactly this question.
* ``watermark_ahead_of_journal`` — the client claims a position past the tail.
  Nothing can be replayed to it and the claim itself is evidence its baseline is
  not this runtime's.
* ``backlog_exceeds_cap`` — a client far enough behind that re-baselining is
  CHEAPER than replaying. The cap is in events rather than bytes because it
  bounds the read as well as the answer.
* ``span_not_foldable`` — the span contains an event no patch frame can carry
  (a ``refresh``, a chokepoint-less write) or one naming an entity THIS client
  did not declare. The demote-to-core that a live batch would take is, at
  resume time, exactly the hydrate — so the fallback is not a second mechanism,
  it is the same one spelled differently.
* ``span_without_patch_rows`` — a span of covered domain events whose paired
  ``state.patched`` rows are not in it. ``batch_carries_patch_rows`` names the
  five producer paths that make one; a frame built from it would advance a
  watermark having folded nothing, which is the one shape this lane must never
  emit.

What this does NOT close
------------------------

The resumed span ends at the offset read here, and the shared producer's next
frame begins wherever that producer is. A write landing in that window is
detected, never silently lost: the client's ``base_offset`` gate answers a
mismatched patch with a resync, and a full-core ``delta`` is applied wholesale
and is authoritative at its own offset. So the cost of the race is one extra
re-hydrate on a narrow window, and the benefit is that no event can go missing
without something saying so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from . import event_rotation
from .events import EventLog
from .models import Event
from .parity import events_watermark
from .patch_coverage import batch_required_fold_tokens, normalize_fold_entities
from .state_patches import delta_patches_enabled
from .stream import _DELTA_BATCH_CAP, batch_carries_patch_rows, patch_batch_frame

__all__ = [
    "STREAM_RESUME_MAX_EVENTS",
    "StreamResume",
    "resolve_stream_resume",
]

#: How far behind a client may be and still be caught up rather than
#: re-baselined. Two full delta batches: past that, the replay stops being
#: cheaper than the core it is avoiding, and the read itself stops being bounded
#: by anything a caller can reason about.
STREAM_RESUME_MAX_EVENTS = 2 * _DELTA_BATCH_CAP


@dataclass(frozen=True)
class StreamResume:
    """The answer to one resume request, honoured or refused, always explained."""

    #: Whether the caller may attach WITHOUT a fresh hydrate.
    honored: bool
    #: Why not, in the vocabulary above. ``None`` when honoured.
    reason: str | None = None
    #: The catch-up frames, in order, to emit before attaching. Empty when the
    #: client was already current — which is the cheapest honoured resume there
    #: is and the one a phone's foreground actually hits.
    frames: list[dict[str, Any]] = field(default_factory=list)
    #: The position the client claimed.
    from_offset: int | None = None
    #: The position these frames carry it to. Equals ``from_offset`` for an
    #: empty span.
    to_offset: int | None = None
    #: Events replayed, so a receipt can say how much was owed.
    events: int = 0

    def payload(self) -> dict[str, Any]:
        """The shape the ``subscribed`` ack carries. Emitted ONLY when a resume
        was actually requested, so a subscribe that asked for nothing produces
        the byte-identical ack it always did."""

        body: dict[str, Any] = {"honored": self.honored}
        if self.reason is not None:
            body["reason"] = self.reason
        if self.from_offset is not None:
            body["from_offset"] = self.from_offset
        if self.honored:
            body["to_offset"] = self.to_offset
            body["events"] = self.events
            body["frames"] = len(self.frames)
        return body


def _refuse(reason: str, requested: int | None) -> StreamResume:
    return StreamResume(honored=False, reason=reason, from_offset=requested)


def resolve_stream_resume(
    requested_offset: Any,
    *,
    fold_entities: Iterable[str] | None = None,
    event_log: EventLog | None = None,
    max_events: int = STREAM_RESUME_MAX_EVENTS,
) -> StreamResume:
    """Decide what a client resuming at ``requested_offset`` is owed.

    ``fold_entities`` is THAT client's declaration, and it is the whole reason
    this cannot be answered once for a room: a span the desktop can fold is a
    span the phone beside it may not, and the two get different answers to the
    same question. Per-subscriber promotion made that affordable on the live
    lane; this is the same rule at the join.

    Reads the journal and nothing else — no snapshot is built on this path, by
    construction. A resume that needed a core would be a hydrate, and saying so
    is cheaper than making one.
    """

    if isinstance(requested_offset, bool) or not isinstance(requested_offset, int):
        return _refuse("invalid_watermark", None)
    requested = int(requested_offset)
    if requested < 0:
        return _refuse("invalid_watermark", requested)
    if not delta_patches_enabled():
        return _refuse("patch_lane_disabled", requested)

    tail = events_watermark().get("event_offset")
    if not isinstance(tail, int):
        return _refuse("journal_unreadable", requested)
    floor = event_rotation.resume_floor_offset()
    if floor is None:
        return _refuse("journal_unreadable", requested)
    if requested < floor:
        return _refuse("journal_truncated", requested)
    if requested > tail:
        return _refuse("watermark_ahead_of_journal", requested)

    log = event_log or EventLog()
    pending: list[tuple[int, Event]] = []
    for offset, event in log.iter_from_offset(requested):
        pending.append((int(offset), event))
        if len(pending) > max_events:
            return _refuse("backlog_exceeds_cap", requested)

    if not pending:
        # Already current. The whole feature, in the case it exists for.
        return StreamResume(
            honored=True,
            frames=[],
            from_offset=requested,
            to_offset=requested,
            events=0,
        )

    declared = normalize_fold_entities(fold_entities)
    frames: list[dict[str, Any]] = []
    base = requested
    for start in range(0, len(pending), _DELTA_BATCH_CAP):
        chunk = pending[start : start + _DELTA_BATCH_CAP]
        required = batch_required_fold_tokens(event for _, event in chunk)
        if required is None or not required <= declared:
            # One unfoldable event refuses the WHOLE span, never just its own
            # chunk: a partial replay would leave the client's watermark inside
            # a gap it has no way to learn about.
            return _refuse("span_not_foldable", requested)
        if not batch_carries_patch_rows(chunk):
            return _refuse("span_without_patch_rows", requested)
        frames.append(patch_batch_frame(chunk, base_offset=base))
        base = int(chunk[-1][0])

    return StreamResume(
        honored=True,
        frames=frames,
        from_offset=requested,
        to_offset=base,
        events=len(pending),
    )
