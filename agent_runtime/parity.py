"""Snapshot parity / observability primitives (S0 of the read-model architecture).

The Mission Control snapshot is a lossy projection of harness truth. Historically
every drop (truncation, redaction, identity mismatch, mode gate) was *silent*, so
when the UI diverged nobody could say what was dropped or why. These primitives
turn drops into data:

* :class:`ProjectionAccountant` — a side-channel a projection records into
  (considered / included / dropped + per-reason tallies + a bounded drop sample).
  Passing ``None`` keeps the projection's behavior byte-for-byte identical, so
  instrumentation is non-invasive.
* :func:`events_watermark` — a cheap source-position marker (event-log byte
  offset + last event ts) so a snapshot is self-dating and a reader can tell how
  far behind it is.

See `Launcher_Brain/20 — Active Initiatives/mission-control-snapshot-architecture.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hermes_time import now

from . import event_rotation, paths

PARITY_ENVELOPE_VERSION = 1

# Cap the per-projection sample of concrete drop records carried in the snapshot
# (the full tallies are unbounded in `reasons`; the samples are for the inspector
# and must not bloat the payload).
_MAX_DROP_SAMPLE = 50
_TEXT_LIMIT = 200


@dataclass(slots=True)
class DropRecord:
    """One concrete dropped/diverged entity, for the inspector."""

    hop: str
    code: str
    entity_id: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"hop": self.hop, "code": self.code, "entity_id": self.entity_id, "detail": self.detail}


class ProjectionAccountant:
    """Counts what one snapshot projection considered / included / dropped.

    A projection takes an optional accountant and records into it; the caller
    reads :meth:`summary` afterward. Reason codes are short, stable tokens
    (``persona_mismatch``, ``tail_truncated``, …) so the UI can group them.
    """

    def __init__(self, projection: str):
        self.projection = projection
        self._considered = 0
        self._included = 0
        self._reasons: dict[str, int] = {}
        self._truncated = False
        self._drops: list[DropRecord] = []

    def consider(self, n: int = 1) -> None:
        self._considered += max(0, int(n))

    def include(self, n: int = 1) -> None:
        self._included += max(0, int(n))

    def drop(
        self,
        code: str,
        *,
        count: int = 1,
        entity_id: Any = None,
        detail: Any = None,
        hop: str | None = None,
    ) -> None:
        count = max(1, int(count))
        self._reasons[code] = self._reasons.get(code, 0) + count
        if len(self._drops) < _MAX_DROP_SAMPLE:
            self._drops.append(
                DropRecord(
                    hop=hop or self.projection,
                    code=str(code),
                    entity_id=_safe_text(entity_id),
                    detail=_safe_text(detail),
                )
            )

    def mark_truncated(self) -> None:
        self._truncated = True

    @property
    def dropped(self) -> int:
        return sum(self._reasons.values())

    def summary(self) -> dict[str, Any]:
        return {
            "considered": self._considered,
            "included": self._included,
            "dropped": self.dropped,
            "reasons": dict(self._reasons),
            "truncated": self._truncated,
        }

    def drop_samples(self) -> list[dict[str, Any]]:
        return [record.as_dict() for record in self._drops]


def events_watermark(*, last_event_ts: Any = None) -> dict[str, Any]:
    """Source-position marker for the append-only event log.

    ``event_offset`` is the log's total byte size — the cursor a streaming tailer
    would resume from (S1) — read cheaply via ``stat`` without scanning the log.
    Under rotation (C6a) that is the LOGICAL tail (live base offset + live slice
    size), spanning sealed slices, so ``iter_from_offset(event_offset)`` still
    resolves to "nothing past the tail". Equals ``getsize(events.jsonl)`` in the
    pristine (pre-rotation) state. ``last_event_ts`` is passed in by the caller
    (which has already tailed the log for the snapshot) so this stays O(1).
    """

    try:
        offset = event_rotation.log_end_offset()
    except OSError:
        offset = 0
    return {"event_offset": offset, "last_event_ts": last_event_ts, "captured_at": now()}


def _safe_text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    return text[: _TEXT_LIMIT - 1] + "…" if len(text) > _TEXT_LIMIT else text
