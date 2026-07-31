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

**By-design vs anomalous drops.** A drop count alone cannot tell a reader whether
a projection is healthy: a bounded lane that keeps the newest 50 of 163 rows
"drops" 113 on every build and is working exactly as specified, while one row
lost to a broken identity join is a defect. Readers that only saw ``dropped``
had to re-derive that distinction from a hardcoded reason-code allowlist on
their side — the Launcher shipped one and it went stale twice (``flow_item_cap``
first, then the persona-chat ``limit``), each time pinning the Mission Control
"projection drops" pill permanently amber on a healthy runtime. The
classification therefore belongs HERE, at the emission site, and rides the
envelope as the additive ``by_design`` key:

* ``by_design=True`` — the drop discloses a **deliberate bound** the projection
  applied on purpose: a cap, a tail window, a page limit, a collapse marker. The
  data is not lost (it stays reachable through the lane's paging/detail fetch)
  and a nonzero count is the steady state, not a symptom.
* ``by_design=False`` (the default) — the drop discloses **lost or inconsistent
  data**: an identity join that did not resolve, a referenced row missing from
  its store, a persona/session mismatch, an unrenderable entry, a redaction
  gate. A nonzero count is something an operator can act on.

The test for a new call site: *would this count still be nonzero on a perfectly
healthy runtime, purely because the projection is bounded?* Yes → ``by_design``.
No → leave it anomalous. Classification is a property of the reason CODE, not of
an individual drop, so a code must be declared the same way at every site that
emits it; once any site declares a code by-design the accountant reports that
code as by-design for the whole projection.

See `Launcher_Brain/20 — Active Initiatives/mission-control-snapshot-architecture.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hermes_time import now

from . import event_rotation

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
    by_design: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "hop": self.hop,
            "code": self.code,
            "entity_id": self.entity_id,
            "detail": self.detail,
            "by_design": self.by_design,
        }


class ProjectionAccountant:
    """Counts what one snapshot projection considered / included / dropped.

    A projection takes an optional accountant and records into it; the caller
    reads :meth:`summary` afterward. Reason codes are short, stable tokens
    (``persona_mismatch``, ``tail_truncated``, …) so the UI can group them, and
    each code is declared once as a deliberate bound (``by_design=True``) or as
    lost/inconsistent data (the default) — see the module docstring.
    """

    def __init__(self, projection: str):
        self.projection = projection
        self._considered = 0
        self._included = 0
        self._reasons: dict[str, int] = {}
        self._by_design: set[str] = set()
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
        by_design: bool = False,
    ) -> None:
        """Record ``count`` dropped entities under ``code``.

        ``by_design`` declares this reason code a deliberate bound (cap / tail /
        page limit) rather than lost or inconsistent data. The declaration is
        per-CODE and sticky: it surfaces in :meth:`summary` under ``by_design``
        so a reader can subtract bounded lanes from the anomaly count without
        maintaining its own reason allowlist.
        """

        count = max(1, int(count))
        self._reasons[code] = self._reasons.get(code, 0) + count
        if by_design:
            self._by_design.add(str(code))
        if len(self._drops) < _MAX_DROP_SAMPLE:
            self._drops.append(
                DropRecord(
                    hop=hop or self.projection,
                    code=str(code),
                    entity_id=_safe_text(entity_id),
                    detail=_safe_text(detail),
                    by_design=bool(by_design),
                )
            )

    def mark_truncated(self) -> None:
        self._truncated = True

    @property
    def dropped(self) -> int:
        return sum(self._reasons.values())

    @property
    def dropped_by_design(self) -> int:
        return sum(count for code, count in self._reasons.items() if code in self._by_design)

    def summary(self) -> dict[str, Any]:
        """The per-projection completeness row carried on the parity envelope.

        ``by_design`` is additive (envelope version unchanged): the sorted reason
        codes this projection declared as deliberate bounds. Always present —
        an empty list means every drop recorded here is anomalous. The four
        historical keys are untouched; ``dropped`` still counts EVERY drop, so a
        reader subtracts the by-design reasons itself and an older reader that
        ignores the key behaves exactly as before.
        """

        return {
            "considered": self._considered,
            "included": self._included,
            "dropped": self.dropped,
            "reasons": dict(self._reasons),
            "truncated": self._truncated,
            "by_design": sorted(self._by_design),
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
