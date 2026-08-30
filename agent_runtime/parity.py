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

#: How many of the 50 slots a BY-DESIGN flood may consume. The remainder is a
#: reserved floor for anomalous records, and that reservation is the whole point
#: (plan ``realm-pull-live-projection`` H3).
#:
#: The sample was one FIFO list, so on a real store the ordering of the drops
#: decided what the operator could see: 132 ``instance_retired`` rows filled all
#: 50 slots before the ONE ``session_not_in_db`` row was recorded, and the
#: launcher's amber "projection drops 1" chip shipped with ``hasDetail == false``
#: — a bare pill with nothing behind it. Naming the offending instance took a
#: Python join over a saved snapshot, which is exactly the cost ``dropSummaries``
#: was built to remove. A by-design drop is the steady state on a healthy
#: runtime; an anomalous one is the only kind an operator can act on, so the
#: bounded budget belongs to the by-design half.
_MAX_BY_DESIGN_DROP_SAMPLE = 40

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
        # TWO lists, one budget each — see ``_MAX_BY_DESIGN_DROP_SAMPLE``. They
        # are re-joined by :meth:`drop_samples`, which is the only reader.
        self._drops_anomalous: list[DropRecord] = []
        self._drops_by_design: list[DropRecord] = []

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

        ``by_design`` ALSO routes which sample budget this record spends, so a
        by-design flood can never starve the anomalous rows the chip is about.
        See :data:`_MAX_BY_DESIGN_DROP_SAMPLE`. Note the split is by the
        argument passed at THIS call, not by the sticky per-code verdict: the
        two agree at every honest site (a code is declared the same way
        everywhere, which the module docstring already requires), and a lane
        that disagreed with itself would be a bug the sample should show rather
        than a reason to re-derive the flag here.
        """

        count = max(1, int(count))
        self._reasons[code] = self._reasons.get(code, 0) + count
        if by_design:
            self._by_design.add(str(code))
        bucket = self._drops_by_design if by_design else self._drops_anomalous
        cap = _MAX_BY_DESIGN_DROP_SAMPLE if by_design else _MAX_DROP_SAMPLE
        if len(bucket) < cap:
            bucket.append(
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
        """The bounded sample of concrete drop records, ANOMALOUS ROWS FIRST.

        **This is a sampling-policy change, not an envelope-shape change**, and
        the distinction is load-bearing: the return type is still
        ``list[dict]`` in the same record shape, ``dropped`` still counts EVERY
        drop, ``reasons`` is still unbounded, and the total is still capped at
        :data:`_MAX_DROP_SAMPLE`. The launcher's ``_isAnomalousDropSample``
        (``mission_control_snapshot.dart:577``) needs no change and MUST need
        none — it filters this list by the ``by_design`` flag each record
        already carries.

        What moved is WHICH records survive the cap. Anomalous first because
        this list is read by an operator answering "what is the amber chip",
        and because the concatenation is what re-imposes the single total: a
        by-design half that was already bounded at
        :data:`_MAX_BY_DESIGN_DROP_SAMPLE` can only be trimmed further here when
        the anomalous half is genuinely large, which is a projection with
        bigger problems than a short sample.
        """

        joined = [*self._drops_anomalous, *self._drops_by_design]
        return [record.as_dict() for record in joined[:_MAX_DROP_SAMPLE]]


def core_event_offset(core: Any) -> int | None:
    """The offset a CORE says its own content reaches, or ``None`` if unknown.

    The core's INTRINSIC position, not the frame's: ``parity.watermark.
    event_offset`` is captured at the instant the build starts reading (that is
    what ``7204896978`` pinned), so it is a LOWER bound on the content — events
    after it may already be folded in, but nothing before it can be missing.
    That direction is what makes it usable as a floor.

    ``None`` for a core with no watermark (an unreadable log at build time, a
    pair persisted before the field existed) and it must stay ``None`` rather
    than ``0``: zero is a real position and would make every such core look
    infinitely behind — see :func:`events_watermark` on the same trap.

    It lives HERE, beside the producer of the field it reads, because two
    consumers need the same answer and had grown two spellings of it: the
    stream's MCF-Q1 frame guard (``stream._full_core_batch_frames``) and the
    same-offset demote reuse (``demote_core_reuse``). A second spelling of "how
    far does this core reach" is how two guards come to disagree about the one
    question the whole read-model turns on.
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


def events_position() -> dict[str, Any]:
    """Capture the event log's source position **now**, with no `last_event_ts`.

    Split out of :func:`events_watermark` so a producer whose CONTENT is read
    over a long window can capture its position BEFORE it starts reading and
    stamp that, instead of a stat taken after it finished. See
    :func:`events_watermark`'s "which instant does the offset describe" block —
    the two directions are not symmetric, and only one of them is safe.

    Carries ``captured_at`` so the instant travels WITH the offset: a position
    handed across a multi-second build must not be re-dated at the far end, or
    the envelope would claim a freshness the number does not have.
    """

    try:
        offset = event_rotation.log_end_offset()
    except OSError as exc:
        return {
            "event_offset": None,
            "event_offset_error": _safe_text(f"{type(exc).__name__}: {exc}"),
            "captured_at": now(),
        }
    return {"event_offset": offset, "captured_at": now()}


def events_watermark(*, last_event_ts: Any = None, position: dict[str, Any] | None = None) -> dict[str, Any]:
    """Source-position marker for the append-only event log.

    ``event_offset`` is the log's total byte size — the cursor a streaming tailer
    would resume from (S1) — read cheaply via ``stat`` without scanning the log.
    Under rotation (C6a) that is the LOGICAL tail (live base offset + live slice
    size), spanning sealed slices, so ``iter_from_offset(event_offset)`` still
    resolves to "nothing past the tail". Equals ``getsize(events.jsonl)`` in the
    pristine (pre-rotation) state. ``last_event_ts`` is passed in by the caller
    (which has already tailed the log for the snapshot) so this stays O(1).

    **An unreadable log yields ``None``, never ``0``.** The stat can fail — on
    this runtime's platform routinely, under AV scanning or a share violation —
    and it used to be swallowed into ``offset = 0``, indistinguishable from a
    genuinely empty log. Zero is the single most damaging value this field can
    carry, because every reader treats it as a real position: the stream
    resumes a tailer from byte 0 — replaying the ENTIRE log as fresh activity at
    the root of every Mission Control surface — and the checkpoint watermark
    records it as a position actually reached.

    Two of the readers that made this rule are gone: ``read_model.snapshot_watermark``
    documented the exact trap in its own docstring while this producer was still
    minting one, and the projector read a 0 offset as "caught up, no new rows".
    Stage 6 (2026-08-22) deleted both with the ``read_model.db`` lane. The rule
    did NOT go with them — it is a property of this producer, and this docstring
    is now its only home, which is why the argument is stated here in full rather
    than delegated to a module that no longer exists.

    ``None`` + ``event_offset_error`` is the typed unknown (the ``cron``
    orphan-sweep shape): a reader that cannot act without a position has to say
    so and resync, rather than guess a plausible one.

    **Which instant the offset describes, and why the two directions are not
    symmetric.** A consumer reads this number as "the core beside it contains
    everything up to here". For a producer that assembles its content over a
    window rather than in an instant, that claim is only honest if the offset is
    captured BEFORE the first section is read:

    * offset captured BEFORE the content — the offset is a LOWER bound. Content
      may be newer than the offset, so events after it replay as deltas the
      client has already folded. Idempotent, and the direction
      ``_full_core_batch_frames`` already takes (it drains the batch, then
      builds).
    * offset captured AFTER the content — the offset counts events the content
      does NOT carry. The client folds the core, advances its watermark past
      those events, and the stream resumes its tail from that offset, so they
      are never replayed. Whatever they created is **gone from the client until
      an unrelated full core happens by.**

    Measured 2026-08-21: an agent dropped onto the office canvas while a
    snapshot build was in flight vanished from Mission Control by exactly this
    route, while every agent that predated the build survived.

    ``build_snapshot``'s cache write-back already states this rule for its OWN
    stat set, in as many words: "PRE-build, deliberately: a stat set taken after
    the build would absorb any write that landed while the build ran, and the
    next process would then serve a core missing that write as authoritative"
    (see ``core_cache.write_back``). This producer took the opposite reading of
    the same log for the same build. Pass ``position`` from
    :func:`events_position` to capture the honest end.
    """

    captured = dict(position) if position is not None else events_position()
    offset = captured.get("event_offset")
    captured_at = captured.get("captured_at")
    if offset is None:
        return {
            "event_offset": None,
            "event_offset_error": captured.get("event_offset_error"),
            "last_event_ts": last_event_ts,
            "captured_at": captured_at,
        }
    return {"event_offset": offset, "last_event_ts": last_event_ts, "captured_at": captured_at}


def _safe_text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    return text[: _TEXT_LIMIT - 1] + "…" if len(text) > _TEXT_LIMIT else text
