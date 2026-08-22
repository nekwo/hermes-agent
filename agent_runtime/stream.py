from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections.abc import Iterable, Iterator
from typing import Any

from hermes_time import now

from . import core_cache, paths
from .events import EventLog
from .models import Event
from .parity import events_watermark
from .patch_coverage import batch_is_patch_coverable, normalize_fold_entities
from .redaction import ENV_SECRET_ASSIGNMENT_RE
from .request_control import request_cancelled
from .serde import optional_text, section_rows, to_jsonable
from .snapshot import (
    BUILD_CALLER_UNKNOWN,
    BUILD_SECTIONS_WAIT_THRESHOLD_MS,
    build_receipt_facts,
    build_snapshot,
)
from .state_patches import STATE_PATCHED_EVENT_TYPE, delta_patches_enabled

STREAM_SCHEMA_VERSION = 1

#: S7-A: the op-based ``patch`` frame is the v2 stream frame. It ships ALONGSIDE
#: the v1 full-core frames (hydrate + uncovered/resync delta batches) — the plan
#: staged this additive, exactly as W1 staged coalescing: a v1 consumer keeps
#: reading full cores; a v2-aware consumer folds patch frames. Each patch entry
#: carries ``{seq, ts, entity, id, op, changed?}`` (op ∈ upsert/remove/refresh —
#: the producer's WIRE-LEVEL contract). The existing frame builders stay
#: ``schema_version 1`` so flag-off behavior is byte-identical (Ruling 0: the
#: flag is a new-lane activation gate, not an old-shape toggle).
STREAM_PATCH_SCHEMA_VERSION = 2

logger = logging.getLogger(__name__)

# Single-homed in ``agent_runtime.redaction`` — see the header there for the
# JSON blind spot every local spelling shared. group(1) is still the full key,
# so the ``f"{match.group(1)}=[redacted]"`` rebuild below is unchanged.
_SECRET_ASSIGNMENT_RE = ENV_SECRET_ASSIGNMENT_RE


#: The CLI stream command is the caller a ``stream_frames`` with nothing
#: threaded through it is: ``hermes harness stream`` on a terminal. The serve
#: hub names itself ``hub`` explicitly (``serve.py``'s producer factory) — the
#: default is not a guess about who is asking, it is the historical answer.
DEFAULT_STREAM_CALLER = "cli"


def _log_snapshot_build(
    *,
    reason: str,
    waited_ms: int,
    offset: int | None,
    events: int | None = None,
    snapshot: dict[str, Any] | None = None,
    build_info: dict[str, Any] | None = None,
) -> None:
    """Record one caller's WAIT for a ``build_snapshot()`` and what it cost.

    **Why this is not the heartbeat's number.** The liveness envelope below
    already ships a ``snapshot_build`` activity block, but its ``elapsed_ms``
    is a MID-BUILD sample taken on the heartbeat cadence (5s from
    ``harness serve``): a 6.5s build reports one sample at ~5000 and a warm
    1.2s build reports NOTHING, because it finishes before the first
    heartbeat is due. Neither value is the total. This line is the total,
    taken on the build thread itself, and it is the exact number the
    2026-08-16 performance pass spent hours reconstructing by subtraction
    from ``events_archive/*.jsonl`` timestamps and then by profiling an
    isolated probe copy of the runtime root.

    **Why the number is now called ``waited_ms``.** It was ``elapsed_ms``, and
    that name is what let three of these lines read as three builds on the
    2026-08-17 boot: the value is measured around ``build_snapshot()`` at the
    CALL SITE, and under coalescing that call may be a short ride on a build
    somebody else led. So this line reports the caller's WAIT, and
    ``build_ms`` — read off the parity envelope of the core it actually got —
    reports the build underneath it. ``role``/``caller``/``generation`` say
    which of the two happened (see
    :data:`agent_runtime.snapshot.BUILD_ROLE_LED` and friends), and the build
    itself has its own one-per-build receipt
    (``snapshot_build_core``). ``elapsed_ms`` is still emitted, with the same
    value as ``waited_ms``, for one release: a launcher in the field parses it.

    ``offset`` anchors the cost to the watermark the resulting frame carries,
    so a build can be tied back to the events that paid for it; ``reason``
    names the lane that paid (``hydrate`` / ``demote`` / ``resync`` /
    ``full_core``). A number with no anchor is barely better than no number.

    ``pid`` is the JOIN KEY across the repo boundary (BO-3). A launcher boot
    receipt and a serve's ``agent.log`` had no common identifier at all: the
    only joins available were wall-clock matching — across a live zone trap, the
    diag log's header being UTC while its per-line stamps are local
    time-of-day — and ``build_ms``+``sections_top`` equality, the deliberate
    weak join the launcher's own ``mission_boot_timeline`` documents as weak.
    The launcher already holds this number three ways (the spawn's
    ``process.pid``, and the ``booting``/``ready`` frames' ``pid``); hermes
    logged it nowhere.

    **An additive field, not a formatter change.** ``%(process)d`` on the
    formatter was considered and rejected: it re-shapes EVERY line this runtime
    emits, so every existing grep that anchors on the family token's neighbour
    breaks at once. ``pid=`` goes LAST on all three of the families a boot
    investigation joins on (this one, ``snapshot_build_core``,
    ``stream_attach``), so no existing adjacency moves and a reader that ignores
    the key behaves exactly as before.

    Rides the ordinary ``Logger`` family, so ``hermes serve`` (mode ``gui``)
    lands it in ``<HERMES_HOME>/logs/agent.log`` at INFO with no extra flag.
    """

    facts = build_receipt_facts(snapshot)
    info = build_info if isinstance(build_info, dict) else {}
    build_ms = facts["build_ms"]
    if build_ms is None and isinstance(info.get("build_ms"), (int, float)):
        build_ms = int(info["build_ms"])
    line = (
        "snapshot_build reason=%s waited_ms=%d elapsed_ms=%d build_ms=%s "
        "role=%s caller=%s generation=%s offset=%s events=%s"
    )
    values: list[Any] = [
        reason,
        int(waited_ms),
        # The deprecated twin, deliberately the SAME value rather than a second
        # measurement — a rename that shipped two different numbers under two
        # keys would be worse than the name it replaced.
        int(waited_ms),
        "unknown" if build_ms is None else int(build_ms),
        str(info.get("role") or "unknown"),
        str(info.get("caller") or BUILD_CALLER_UNKNOWN),
        "-" if info.get("generation") is None else int(info["generation"]),
        "unknown" if offset is None else int(offset),
        "-" if events is None else int(events),
    ]
    # The section split rides a WAIT line only when the build under it was slow
    # enough that "of what?" is the next question (HC-0). Every build carries its
    # own split on its ``snapshot_build_core`` receipt regardless.
    if build_ms is not None and int(build_ms) >= BUILD_SECTIONS_WAIT_THRESHOLD_MS:
        line += " sections_top=%s"
        values.append(facts["sections_top"])
    # LAST, after the conditional split, so "pid is the last field" holds on
    # both shapes of this line — see the docstring for why it is additive here
    # rather than a formatter change.
    line += " pid=%d"
    values.append(os.getpid())
    logger.info(line, *values)


def log_stream_attach(*, op: str, purpose: str, **fields: Any) -> None:
    """ONE line per ATTACHMENT to the shared stream, at subscribe time.

    Three different calls attach a reader to the same producer — the socket
    lane's ``{"op":"subscribe"}``, the RPC office lane's
    ``runtime.office.subscribe``, and ``hermes harness stream`` on a terminal —
    and until this line existed the serve child's own log named NONE of them.
    Two costs of that silence, both paid: the 2026-08-17 boot's third hydrate
    rider could not be identified at all (the subscriber census had to be
    reconstructed from timestamps, and one attachment stayed unattributed), and
    a 12 MB serve-child log carried zero ``office`` lines, which made
    "is the office push lane even attached?" unanswerable from the log the
    operator actually has (plan §8 item 5).

    ``op`` is the call as the client made it; ``purpose`` is what the attachment
    is FOR. Both, because neither implies the other: two ops can serve one
    purpose (the office lane and the legacy stream both fold patches) and one op
    serves several (``subscribe`` is the boot hydrate and every resubscribe).

    ``pid`` rides LAST (BO-3), the same additive field the two build families
    carry, so an attachment and the builds it paid for join on one key instead
    of on wall clocks across a timezone boundary. It is emitted here rather than
    left to each caller's ``**fields`` precisely because the callers are three
    different modules: a field each of them had to remember is a field one of
    them would eventually not.

    Single-homed here, next to the build lines a reader greps alongside it, and
    imported function-locally by the two non-stream callers so this module's
    projection-import weight stays off their import paths. Never raises: an
    instrument must not be the reason a subscribe fails.
    """

    try:
        extras = " ".join(
            f"{key}={'-' if value is None else value}" for key, value in fields.items()
        )
        logger.info(
            "stream_attach op=%s purpose=%s%s pid=%d",
            op,
            purpose,
            f" {extras}" if extras else "",
            os.getpid(),
        )
    except Exception:  # pragma: no cover - observability must never fail a lane
        pass


def hydrate_frame(
    snapshot: dict[str, Any] | None = None,
    *,
    delta_patches: bool = False,
    fold_entities: Iterable[str] | None = None,
    caller: str = DEFAULT_STREAM_CALLER,
) -> dict[str, Any]:
    """Build the initial warm-stream hydrate frame.

    The hydrate carries the existing full snapshot as the read model payload so
    the stream is additive: the one-shot snapshot remains the canonical fallback
    and consumers can converge by applying this frame exactly like a fresh
    snapshot response.

    S6: when the ``read_model.delta_patches`` lane is on, the hydrate carries an
    additive ``delta_patches: true`` marker — the signal that tells a fold-aware
    launcher to RETAIN this frame's raw core as the patch base (so the next
    ``patch`` frame folds instead of resyncing). The marker is absent when the
    flag is off, so a flag-off hydrate stays byte-identical (its golden asserts
    the key-set, Ruling 0).

    Beside it rides the ACCEPTED ``fold_entities`` (sorted), completing that
    handshake in the direction it was missing: ``delta_patches: true`` told the
    client the lane exists, but nothing told the client which of the entities it
    declared the server actually honoured. On the socket lane that answer is not
    a restatement of the request — the producer is SHARED, so the accepted set is
    the intersection across every attached subscriber (see
    :func:`patch_coverage.accepted_fold_entities`) and a client can be honoured
    for strictly less than it asked for, by somebody else's declaration. A client
    that cannot read the echo is unaffected: it is one additive key on a frame
    that only exists when the lane is on, and the echo is absent entirely when
    the flag is off, so the flag-off hydrate stays byte-identical.
    """

    # ``accept_inflight``: this hydrate may ride the build that is ALREADY
    # running (serve prewarms one right after ``ready``). It loses no event —
    # the frame's ``watermark.event_offset`` is read back out of the snapshot
    # below and ``stream_frames`` tails from exactly that offset, so anything
    # appended after the shared build arrives as the first delta instead.
    # Requiring a newer build would make the launcher's boot hydrate wait for
    # the prewarm AND then pay a second build — strictly worse than no prewarm.
    build_info: dict[str, Any] = {"caller": caller}
    if snapshot is not None:
        snap = snapshot
        waited_ms: int | None = None
    else:
        build_started = time.monotonic()
        snap = build_snapshot(accept_inflight=True, build_info=build_info)
        # NOTE what this measures: the hydrate's WAIT, which under
        # ``accept_inflight`` may be a short ride on a build somebody else
        # started (serve prewarms one right after ``ready``) rather than a
        # build of its own. That is the number the client actually paid, which
        # is the one worth logging here — and ``build_info["role"]`` is what
        # says which of the two this line is reporting.
        waited_ms = int((time.monotonic() - build_started) * 1000)
    parity = snap.get("parity") if isinstance(snap.get("parity"), dict) else {}
    watermark = parity.get("watermark") if isinstance(parity.get("watermark"), dict) else {}
    frame: dict[str, Any] = {
        "type": "hydrate",
        "schema_version": STREAM_SCHEMA_VERSION,
        "generated_at": snap.get("generated_at") or now(),
        "watermark": dict(watermark or {}),
        "identity_map": _identity_map(snap),
        "core": snap,
        "completeness": parity.get("completeness") or {},
        "drops": parity.get("drops") or [],
        "parity_warnings": parity.get("warnings") or [],
    }
    if delta_patches:
        frame["delta_patches"] = True
        frame["fold_entities"] = sorted(normalize_fold_entities(fold_entities))
    if waited_ms is not None:
        _log_snapshot_build(
            reason="hydrate",
            waited_ms=waited_ms,
            offset=(frame.get("watermark") or {}).get("event_offset"),
            snapshot=snap,
            build_info=build_info,
        )
    return frame


def _resume_offset(frame: dict[str, Any]) -> int | None:
    """The tail position a frame says to resume from, or ``None`` if unknown.

    Two distinct absences used to collapse into ``0`` at the call site: a frame
    carrying no watermark key at all, and one carrying an explicit ``None``
    because ``events_watermark`` could not stat the log. Both mean "no position";
    neither means "the head of the log".
    """

    value = (frame.get("watermark") or {}).get("event_offset")
    return None if value is None else int(value)


def _bounded_sleep(poll_interval_seconds: float) -> None:
    """Sleep the poll interval in cancellation-latency-bounded slices."""

    remaining = max(0.01, float(poll_interval_seconds))
    while remaining > 0 and not request_cancelled():
        interval = min(_SNAPSHOT_CANCEL_POLL_SECONDS, remaining)
        time.sleep(interval)
        remaining -= interval


def heartbeat_frame(
    *, offset: int | None, activity: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Liveness frame that advances the stream watermark without a core delta.

    Pure liveness telemetry: consumers merge it fire-and-forget and a dropped
    frame only ages the HUD, never runtime state. (This frame previously also
    carried the Mission Daemon status block; the background daemon was retired.)

    ``offset=None`` is the honest heartbeat of a stream that has not been able
    to read the log's tail: liveness without a position. It must not be stamped
    ``0``, which every watermark-gated reader would take as a real cursor at the
    head of the log.
    """

    frame = {
        "type": "heartbeat",
        "schema_version": STREAM_SCHEMA_VERSION,
        "generated_at": now(),
        "watermark": {
            "event_offset": None if offset is None else int(offset),
            "captured_at": now(),
        },
    }
    if activity:
        frame["activity"] = activity
    return frame


def _delta_entity(event: Event) -> dict[str, Any]:
    """One event's redaction-safe entity block, shared by the single-delta
    shape and the batched ``events`` list so the two can never drift."""

    payload = _redaction_safe_json(event.payload)
    return {
        "event": {
            **to_jsonable(event),
            "payload": payload,
        },
        "task_id": event.task_id,
        "goal_id": event.task_id,
        "run_id": event.run_id,
        "persona_id": event.persona_id,
        "session_id": event.session_id,
        "correlation_id": payload.get("correlation_id") if isinstance(payload, dict) else None,
    }


def delta_frame(event: Event, *, offset: int, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "delta",
        "schema_version": STREAM_SCHEMA_VERSION,
        "generated_at": now(),
        "watermark": {
            "event_offset": int(offset or 0),
            "last_event_ts": event.ts,
            "captured_at": now(),
        },
        "seq": int(offset or 0),
        "op": _delta_op(event),
        "entity": _delta_entity(event),
        "core": snapshot if snapshot is not None else build_snapshot(),
    }


def delta_batch_frame(
    batch: list[tuple[int, Event]], *, snapshot: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One delta frame for a whole drained event batch (transport plan W1).

    The old loop shipped one full ``build_snapshot()`` core PER EVENT — a
    ~9MB rebuild + serialize per append, measured live 2026-07-16 — which is
    why a 30-event burst cost thirty rebuilds on this side and thirty full
    decodes on the launcher side. This frame carries the SAME core exactly
    once for the batch.

    Shape is strictly additive over :func:`delta_frame` (schema_version stays
    1; pinned by ``tests/fixtures/stream_frames/delta_batch.json`` and the
    launcher's byte-identical golden): ``watermark``/``seq`` sit at the FINAL
    offset so the launcher's ``>``-only sequence gate applies the batch once;
    ``entity``/``op`` remain the LAST event for pre-batch consumers; the new
    ``events`` list carries every batched entity and ``coalesced_count`` its
    length. The launcher reads only type/watermark/identity_map/core, so the
    additions must stay additions.
    """

    if not batch:
        raise ValueError("delta_batch_frame requires a non-empty batch")
    last_offset, last_event = batch[-1]
    core = snapshot if snapshot is not None else build_snapshot()
    frame = delta_frame(last_event, offset=last_offset, snapshot=core)
    frame["events"] = [_delta_entity(event) for _, event in batch]
    frame["coalesced_count"] = len(batch)
    return frame


def batch_carries_patch_rows(batch: list[tuple[int, Event]]) -> bool:
    """Whether :func:`patch_batch_frame` would find at least one row in ``batch``.

    The same filter the builder applies, asked as a predicate and deliberately
    WITHOUT materializing the rows: the promotion gate runs on every drained
    batch and only needs the emptiness answer, while building the list costs a
    redaction-safe copy of every payload in it.

    **Why the question exists at all.** Coverability is decided per EVENT
    (:func:`~agent_runtime.patch_coverage.batch_is_patch_coverable` is an
    ``all(...)`` with no "at least one patch" requirement), and a COVERED DOMAIN
    EVENT is coverable on its own — it carries no fold state precisely because
    its paired ``state.patched`` is supposed to ride the same batch. When the
    pair does not arrive, the batch is still coverable, and the frame it used to
    ship was ``{"type": "patch", "patches": [], "watermark": <batch>}``: the
    client advances its watermark having folded NOTHING, and the row it should
    have folded is stale until some unrelated full core happens by. There is no
    downstream gate that can see that — the watermark says the span was applied.

    Five producer-side paths re-open the missing pair, which is why the guard is
    HERE and not in a producer: the three best-effort patch-emit swallows in
    :class:`~agent_runtime.office_store.OfficeStore`, ``update_surface``'s
    no-exception skip when the surface did not previously exist, and the
    cross-process split of ``delta_patches_enabled`` (the writer and the stream
    producer evaluate it independently, so a transient root-config fault in the
    writer suppresses the patch while the stream happily promotes the event-only
    batch). This predicate is the one place that sees all five.
    """

    return any(event.type == STATE_PATCHED_EVENT_TYPE for _, event in batch)


def patch_batch_frame(
    batch: list[tuple[int, Event]], *, base_offset: int
) -> dict[str, Any]:
    """One coalesced batch of foldable ``state.patched`` entries as a v2 ``patch``
    frame — op-based wire patches only, **no full core** (F7's ~9MB rebuild is
    gone on this lane; the per-update transfer drops from a fused megabyte core to
    a sub-4KB patch, the ~99.96% reduction the plan's S6/S7 acceptance names).

    ``base_offset`` is the watermark the batch applies FROM (the offset before
    its first entry); the launcher folds only when its held watermark equals
    ``base_offset`` — a mismatch is a **sequence gap** → checkpoint resync.
    ``watermark.event_offset`` is the post-batch offset the fold advances to,
    keeping the launcher's ``>``-only sequence gate applicable exactly as the
    full-core lane does. ``patches`` is the ordered list of the batch's
    ``{seq, ts, entity, id, op, changed?}`` entries (the op-based wire contract —
    ``changed`` present only for ``upsert``); ``coalesced_count`` is the whole
    batch length (parity with :func:`delta_batch_frame`).

    Refuses an EMPTY FILTERED LIST as well as an empty batch — a frame that
    advances a watermark must carry the state that justifies it, and a batch of
    covered domain events with no paired ``state.patched`` justifies nothing (see
    :func:`batch_carries_patch_rows` for the five paths that produce one). Belt
    and braces with the promotion gate in :func:`_batch_frames_with_liveness`:
    the gate decides the honest lane, this refuses to BUILD the dishonest frame,
    and one authority saying so at each end is cheaper than two that can
    disagree. The caller's guard is the reachable one; this is the one that keeps
    a future caller from re-opening the hole quietly.
    """

    if not batch:
        raise ValueError("patch_batch_frame requires a non-empty batch")
    last_offset, last_event = batch[-1]
    patches = [
        {"seq": int(offset or 0), "ts": event.ts, **_redaction_safe_json(event.payload)}
        for offset, event in batch
        if event.type == STATE_PATCHED_EVENT_TYPE
    ]
    if not patches:
        raise ValueError(
            "patch_batch_frame requires at least one state.patched row: "
            f"{len(batch)} events, none of them {STATE_PATCHED_EVENT_TYPE}"
        )
    return {
        "type": "patch",
        "schema_version": STREAM_PATCH_SCHEMA_VERSION,
        "generated_at": now(),
        "watermark": {
            "event_offset": int(last_offset or 0),
            "last_event_ts": last_event.ts,
            "captured_at": now(),
        },
        "base_offset": int(base_offset or 0),
        "patches": patches,
        "coalesced_count": len(batch),
    }


#: Upper bound on events carried by one batched delta frame. Bounds both the
#: frame's `events` list and the drain's in-memory buffer; a longer backlog
#: emits multiple batch frames, each with its own (single) core.
_DELTA_BATCH_CAP = 256

_SNAPSHOT_CANCEL_POLL_SECONDS = 0.1


def _is_one_shot(max_frames: int | None) -> bool:
    """Is this request's whole budget one frame?

    Its own predicate because two different boot-lane decisions turn on it and
    they must not drift apart: the stale-first core is refused for a one-shot,
    and the boot build's liveness heartbeats must not consume its budget. Both
    exist so that ``harness stream --max-frames 1`` — the launcher's forced-
    refresh lane — always answers with an AUTHORITATIVE core, never with a stale
    one and never with a heartbeat carrying nothing.
    """

    return max_frames is not None and int(max_frames) <= 1


class _SnapshotBuildJob:
    """Finite daemon build used by the stream's liveness envelope.

    Snapshot construction is synchronous and can involve filesystem parsing.
    Running it on a daemon worker lets the stream generator keep emitting the
    already-applied watermark as a heartbeat and observe cooperative request
    cancellation. A cancelled consumer does not wait for a finite build to
    finish; the build may complete in the background and remains protected by
    ``build_snapshot``'s existing coalescing contract.
    """

    def __init__(
        self,
        *,
        caller: str = DEFAULT_STREAM_CALLER,
        accept_inflight: bool = False,
    ) -> None:
        #: Whether this job may RIDE a build that is already running rather than
        #: waiting for the next one. Load-bearing for exactly one caller and
        #: default-off for every other, which is why it is a field rather than a
        #: constant: the boot hydrate is the one place ``build_snapshot`` names
        #: as safe for it (its frame carries its own watermark and the tail
        #: resumes from exactly that offset, so nothing is lost — see that
        #: function's own argument). Moving the boot build onto this job WITHOUT
        #: carrying the flag would have made every boot pay a second full build
        #: behind the serve's prewarm, silently: the receipt would still say
        #: ``reason=hydrate`` and only a second ``role=led`` line per boot would
        #: have shown it.
        self.accept_inflight = bool(accept_inflight)
        self.done = threading.Event()
        self.snapshot: dict[str, Any] | None = None
        self.error: BaseException | None = None
        #: Wall time of this job's wait for a core, measured ON the build
        #: thread. Taken here rather than around ``job.done.wait`` because that
        #: wait polls at ``_SNAPSHOT_CANCEL_POLL_SECONDS``, which would round
        #: every build up by as much as 100ms. Set before ``done`` so any reader
        #: that has observed completion has also observed the number. It is a
        #: WAIT, not necessarily a build: the coalescer may hand this job a copy
        #: of somebody else's build, which is what ``build_info`` records.
        self.elapsed_ms: int | None = None
        #: Filled by the builder: role / caller / generation / build_ms. See
        #: :func:`agent_runtime.snapshot.build_snapshot`.
        self.build_info: dict[str, Any] = {"caller": caller}

    def run(self) -> None:
        started = time.monotonic()
        try:
            self.snapshot = build_snapshot(
                accept_inflight=self.accept_inflight, build_info=self.build_info
            )
        except BaseException as exc:  # re-raised on the stream worker
            self.error = exc
        finally:
            self.elapsed_ms = int((time.monotonic() - started) * 1000)
            self.done.set()


def _build_with_liveness(
    job: _SnapshotBuildJob,
    *,
    heartbeat_offset: int | None,
    heartbeat_interval_seconds: float,
    emit_liveness: bool = True,
) -> Iterator[dict[str, Any]]:
    """Run ``job`` on a daemon worker, yielding liveness until it finishes.

    Shared by the two lanes that pay for a full core inside a stream: the boot
    hydrate and an uncovered batch. It owns exactly what the two have in common
    and nothing either of them decides — the worker, the
    ``_SNAPSHOT_CANCEL_POLL_SECONDS`` wait, the ``request_cancelled`` probe, the
    heartbeat cadence, and the ``snapshot_build`` activity block. Which FRAME
    the finished build becomes and which receipt it bills stay at each call
    site, because those are the parts that differ and folding them in would
    have needed a discriminator argument per difference — a helper shaped like
    a switch, which is how one loop becomes two loops wearing one name.

    ``heartbeat_offset`` is the watermark to advertise, and the two callers
    answer it differently on purpose. A batch build keeps the last APPLIED
    offset: advertising the drained batch's future offset would make the
    launcher infer a missed delta and start a second hydrate while this build is
    perfectly healthy. A BOOT build has no applied core at all, so its honest
    answer is ``None`` — ``heartbeat_frame``'s own contract, "liveness without a
    position … must not be stamped ``0``", which every watermark-gated reader
    would take as a real cursor at the head of the log.

    Returns EARLY on cancellation, leaving ``job.done`` unset; callers re-probe
    ``request_cancelled()`` before touching the result, exactly as they must
    after any generator that can return without finishing.

    ``emit_liveness=False`` still runs and waits for the build — it only
    suppresses the frames. See ``_is_one_shot``.
    """

    started = time.monotonic()
    threading.Thread(
        target=job.run,
        name="harness-stream-snapshot",
        daemon=True,
    ).start()
    heartbeat_interval = max(0.05, float(heartbeat_interval_seconds or 0.05))
    next_heartbeat = started + heartbeat_interval
    while not job.done.wait(_SNAPSHOT_CANCEL_POLL_SECONDS):
        if request_cancelled():
            return
        current = time.monotonic()
        if current >= next_heartbeat:
            if emit_liveness:
                yield heartbeat_frame(
                    offset=heartbeat_offset,
                    activity={
                        "kind": "snapshot_build",
                        "state": "busy",
                        "elapsed_ms": int((current - started) * 1000),
                    },
                )
            next_heartbeat = current + heartbeat_interval


def _core_event_offset(snapshot: dict[str, Any] | None) -> int | None:
    """The offset a CORE says its own content reaches, or ``None`` if unknown.

    The core's intrinsic position, not the frame's: ``parity.watermark.
    event_offset`` is captured at the instant the build starts reading (that is
    what ``7204896978`` pinned), so it is a LOWER bound on the content — events
    after it may already be folded in, but nothing before it can be missing.
    That direction is what makes it usable as a floor below.

    ``None`` for a core with no watermark (an unreadable log at build time, a
    pair persisted before the field existed) and it must stay ``None`` rather
    than ``0``: zero is a real position and would make every such core look
    infinitely behind — see ``parity.events_watermark`` on the same trap.
    """

    if not isinstance(snapshot, dict):
        return None
    parity = snapshot.get("parity")
    if not isinstance(parity, dict):
        return None
    watermark = parity.get("watermark")
    if not isinstance(watermark, dict):
        return None
    value = watermark.get("event_offset")
    return None if value is None else int(value)


def _full_core_batch_frames(
    batch: list[tuple[int, Event]],
    *,
    base_offset: int,
    heartbeat_interval_seconds: float,
    reason: str = "full_core",
    caller: str = DEFAULT_STREAM_CALLER,
) -> Iterator[dict[str, Any]]:
    """Emit liveness while one uncovered batch builds its authoritative core.

    ``reason`` names why this batch is paying for a full core — it is the
    caller's classification, not something this function can re-derive, and it
    is what makes the emitted ``snapshot_build`` line actionable. ``caller``
    names WHO is paying, which this function likewise cannot re-derive: the same
    generator serves the serve hub and a terminal.

    **THE CORE MUST REACH THE OFFSET IT IS ABOUT TO BE STAMPED WITH (MCF-Q1).**
    This lane is the designated CARRIER of everything the patch lane cannot
    express (``patch_coverage``: "The demote to a full core is what carries
    it"), and that design assumes the core it demotes to is FRESH. When it is
    not, the frame is the worst shape this producer can emit: a ``delta`` at an
    offset strictly ahead of the client's, whose core the launcher applies
    WHOLESALE — so a section present only via folded patches (a persona instance
    dragged onto the canvas seconds ago) is replaced away by the older core's
    complete copy of that section, while everything that predates the core
    survives. That asymmetry is the 2026-08-21 "the new agent disappears, the old
    one does not" report.

    The producer of a stale core here is the boot cache lane, and closing that
    lane's window correctly (``core_cache.shadow_validate``) is the root fix — but
    it is NOT sufficient, which is why this guard is not belt-and-braces. The
    operator's log shows the same shape on 2026-08-20 at 18:30:11 and 18:38:43,
    where the boot's shadow validation DIVERGED and closed the window about ten
    seconds in: the erasing frames landed inside a window that was going to close
    anyway. Only a check at the frame — where the core and the offset it is about
    to be stamped with are both in hand — refuses that one.

    ``7204896978`` established this invariant INSIDE a build (an offset stamped
    ahead of the content it was read from); this is the same invariant one layer
    out, over a core the builder handed back rather than built. The check costs
    two dict lookups and fires only when the invariant is already broken.
    """

    job = _SnapshotBuildJob(caller=caller)
    # Keep the watermark at the last APPLIED core — see ``_build_with_liveness``
    # for why this caller answers ``heartbeat_offset`` differently from the boot.
    yield from _build_with_liveness(
        job,
        heartbeat_offset=base_offset,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
    )
    if request_cancelled():
        return
    if job.error is not None:
        raise job.error
    if job.snapshot is None:
        raise RuntimeError("snapshot build completed without a result")
    last_offset = int(batch[-1][0] or 0)
    core_offset = _core_event_offset(job.snapshot)
    if core_offset is not None and core_offset < last_offset:
        # The ONE reachable producer of this shape is the boot cache lane: a
        # genuine build in this lane cannot be behind, because the batch was
        # DRAINED before the build started and this job never rides an in-flight
        # one (``accept_inflight`` is default-off here, deliberately). So the
        # answer is to stop the lane and pay for the build once, rather than to
        # ship a frame whose watermark is a lie about its own core.
        core_cache.close_cache_lane(
            reason=core_cache.REFUSAL_CORE_BEHIND_FRAME,
            caller=caller,
            detail=f"core_offset={core_offset} frame_offset={last_offset}",
        )
        job = _SnapshotBuildJob(caller=caller)
        yield from _build_with_liveness(
            job,
            heartbeat_offset=base_offset,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
        )
        if request_cancelled():
            return
        if job.error is not None:
            raise job.error
        if job.snapshot is None:
            raise RuntimeError("snapshot build completed without a result")
    frame = delta_batch_frame(batch, snapshot=job.snapshot)
    if job.elapsed_ms is not None:
        _log_snapshot_build(
            reason=reason,
            waited_ms=job.elapsed_ms,
            offset=(frame.get("watermark") or {}).get("event_offset"),
            events=len(batch),
            snapshot=job.snapshot,
            build_info=job.build_info,
        )
    yield frame


def _batch_frames_with_liveness(
    batch: list[tuple[int, Event]],
    *,
    base_offset: int,
    delta_patches: bool,
    resync: bool,
    heartbeat_interval_seconds: float,
    fold_entities: Iterable[str] | None = None,
    caller: str = DEFAULT_STREAM_CALLER,
) -> Iterator[dict[str, Any]]:
    if (
        delta_patches
        and not resync
        and batch_is_patch_coverable(
            (event for _, event in batch), fold_entities=fold_entities
        )
        # Coverable is not the same as EXPRESSIBLE. A covered domain event with
        # no paired `state.patched` in the batch is coverable on its own and
        # would ship an empty `patches` list — a watermark the client advances
        # having folded nothing. The honest answer for that batch is the core:
        # state moved and this lane has no patch to say what. See
        # `batch_carries_patch_rows` for the five producer paths that reach here.
        and batch_carries_patch_rows(batch)
    ):
        yield patch_batch_frame(batch, base_offset=base_offset)
        return
    # Classified HERE because this is the only place that holds all three
    # facts. `resync` is a re-baseline the client asked for; with the lane off
    # every batch is a full core by design (not a demotion); otherwise either the
    # coverage gate rejected the batch or it carried no patch row to express, and
    # a foldable update just paid for a whole snapshot — the case worth grepping
    # for. Both demote reasons bill the same `snapshot_build reason=demote`
    # receipt, which is what makes the empty-frame paths attributable at all.
    yield from _full_core_batch_frames(
        batch,
        base_offset=base_offset,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        reason="resync" if resync else ("demote" if delta_patches else "full_core"),
        caller=caller,
    )


def stream_frames(
    *,
    event_log: EventLog | None = None,
    poll_interval_seconds: float = 0.25,
    heartbeat_interval_seconds: float = 5.0,
    delta_debounce_seconds: float = 0.2,
    max_frames: int | None = None,
    resync: bool = False,
    fold_entities: Iterable[str] | None = None,
    caller: str = DEFAULT_STREAM_CALLER,
    wants_stale_first: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield hydrate, delta/patch, and heartbeat frames for ``hermes harness stream``.

    Freshness backstop (Stage 12): every store mutation is supposed to append
    an EventLog event (enforced by test_store_event_invariant), but a write
    that slips the rule would freeze watermark-gated consumers FOREVER — they
    drop same-offset re-hydrates, so only an offset advance converges them.
    At heartbeat cadence this loop fingerprints the scope/catalog state that
    isn't guarded by evented stores at runtime; if the fingerprint changed
    while the offset did not, it appends a synthetic ``state.reconciled``
    event, which flows out as an ordinary full-core delta. Declared SLO:
    client staleness ≤ 2× heartbeat interval for ANY write. Every
    ``state.reconciled`` in the log names a producer bug to fix at source.

    S6: when ``read_model.delta_patches`` is on, a fully-coverable batch ships
    as a sub-4KB v2 ``patch`` frame instead of a full-core delta; uncovered
    batches keep the full-core lane. Each
    batch carries the ``base_offset`` it applies from so the launcher's fold can
    detect a gap. ``resync=True`` forces the FIRST post-hydrate batch to a full
    core — the "explicit resync request" a reconnecting client makes to re-baseline
    before folding. Flag off → every batch is the byte-identical full-core frame.

    ``fold_entities`` is the CLIENT's declaration of which entity classes it can
    fold in place; a batch naming any other entity is demoted to the full core
    (see :mod:`agent_runtime.patch_coverage`). ``None`` — nothing declared, which
    is what every client in the field sends today — resolves to the historical
    ``{persona_instance, incident}``, so an un-updated launcher gets exactly the
    wire it gets now. Resolved ONCE, here: what "absent" means must not be
    re-decided per batch on a hot path, and the hydrate must echo the same answer
    the promotion decision uses.

    **An unknown resume position is not byte 0.** ``or 0`` folded BOTH a missing
    watermark key and an explicitly unknown one (``events_watermark`` returns
    ``None`` when the log's end offset could not be stat'ed) into 0, and then
    tailed from there — replaying the entire event log as fresh activity at the
    root of every Mission Control surface. Unknown now takes the resync lane
    this function already has: the first batch ships as a full core, and the
    tailer waits to learn a real tail instead of inventing one.

    ``caller`` is who this generator produces for — ``hub`` for the serve hub's
    shared producer, ``cli`` for ``hermes harness stream``. It reaches
    ``build_snapshot``'s ``build_info`` and lands on every build/wait line this
    generator emits, so a boot's log says WHICH attachment paid for a build
    instead of leaving the census to be reconstructed from timestamps.

    ``wants_stale_first`` is whether anyone this generator feeds will PAINT the
    boot's one stale-labelled core (EG-3.1's mismatch half, MC-4 / P6). It is a
    property of the ROOM, which is why it is stated by the caller and cannot be
    re-derived here: the serve hub is one producer for N subscribers, and the
    subscriber that attaches FIRST at boot is the RPC office lane, whose
    ``office_patch_sink`` discards every row that is not an ``office_actor``.
    Measured 2026-08-18: two boots in three handed the stale paint to that sink
    and the launcher watched an empty canvas for the length of a full build.
    ``serve.py::_room_wants_stale_first`` derives the hub's answer from its two
    subscriber tables at producer-build time; ``_cmd_stream`` states ``True``
    because the argv lane exists to feed a painting consumer. The default is
    ``False`` — the SAFE direction, and deliberately so: a caller that has not
    said it paints gets exactly the pre-EG-3.1 wire (one authoritative hydrate),
    whereas a ``True`` default would let a third, non-painting caller silently
    eat the boot's single stale core again. ``test_stream_stale_first_routing``
    pins by AST that both production call sites state it rather than inheriting
    it.
    """

    log = event_log or EventLog()
    delta_patches = delta_patches_enabled()
    declared_entities = normalize_fold_entities(fold_entities)
    resync_pending = bool(resync)
    emitted = 0
    # EG-3.1's mismatch half. A persisted core whose fingerprint does NOT match
    # is not authority — but it is also not nothing, and the alternative is
    # showing the operator an empty canvas for the length of a full build. So it
    # goes out FIRST, wearing the stale label
    # (``parity.freshness.state = "stale"``, the field the launcher's envelope
    # already maps to ``MissionSnapshotHealth.stale``), and the authoritative
    # hydrate below replaces it when the build completes.
    #
    # It is an ordinary ``hydrate`` frame, not a new type: the hydrate's own
    # contract is "apply this exactly like a fresh snapshot", so a second one
    # re-baselines a client with no new wire vocabulary. The stale frame's
    # watermark is deliberately NOT used to seed ``offset`` — the tail is
    # resumed from the AUTHORITATIVE frame below, so nothing between the two is
    # skipped.
    #
    # TWO conditions, and they are different questions. ``wants_stale_first``
    # asks whether anybody in this generator's room paints (see the parameter).
    # ``_is_one_shot`` asks whether this request has room for a second frame at
    # all: the stale frame is yielded at the HEAD and the budget check below
    # returns immediately after it, so a one-shot that took the stale core would
    # answer with a core that is by definition NOT authoritative — and the
    # launcher's forced-refresh lane
    # (``mission_control_bridge.dart::_loadSnapshotFromStreamHydrate``, read
    # 2026-08-18) scans that stdout for a ``type == "hydrate"`` line and applies
    # whatever it finds through ``applyForcedSnapshot``, i.e. PAST its own
    # sequence gate. Refusing here is what keeps "force a refresh" from meaning
    # "re-paint the projection you were already unhappy with".
    stale_core = (
        core_cache.take_stale_first_core(caller=caller)
        if wants_stale_first and not _is_one_shot(max_frames)
        else None
    )
    if stale_core is not None:
        yield hydrate_frame(
            snapshot=stale_core,
            delta_patches=delta_patches,
            fold_entities=declared_entities,
            caller=caller,
        )
        emitted += 1
        if max_frames is not None and emitted >= max_frames:
            return
    # The boot's authoritative core, built with the stream SAYING SO while it
    # runs (MC-4 / P6, evidence A-x2). This used to be a bare synchronous
    # ``hydrate_frame()``: on 2026-08-18 that build took 29,560 ms and the lane
    # emitted nothing for the whole of it, so the launcher's watchdog fired
    # ``stream_teardown cause=liveness_deadline`` 0.67 s before the frame
    # arrived, and the finished core was delivered to a retired request and
    # discarded with no receipt. A batch build has heartbeat through its build
    # since ``_full_core_batch_frames`` shipped; the BOOT build — the longest one
    # any consumer ever waits on — was the one lane that stayed silent.
    #
    # ``accept_inflight=True`` is carried deliberately and is the whole reason
    # the job grew the flag: ``hydrate_frame``'s own build sets it (the serve
    # prewarms a build right after ``ready``, and this frame is allowed to ride
    # it because its watermark comes from the snapshot itself and the tail below
    # resumes from exactly that offset). A job without it would make every boot
    # wait for the prewarm and THEN pay a second full build.
    boot_job = _SnapshotBuildJob(caller=caller, accept_inflight=True)
    for liveness in _build_with_liveness(
        boot_job,
        # No applied core exists yet, so there is no position to advertise. See
        # ``heartbeat_frame``: liveness without a position must not be stamped 0.
        heartbeat_offset=None,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        # A one-shot is answered with a CORE or with nothing — see
        # ``_is_one_shot``. Suppressed rather than merely uncounted so the
        # frames a one-shot consumer sees stay exactly what it asked for.
        emit_liveness=not _is_one_shot(max_frames),
    ):
        # NOT counted toward ``emitted``, and this is the deliberate half of the
        # ``max_frames`` decision. These frames are emitted while the FIRST
        # content frame is still being built, so counting them would let a
        # budget be spent before any core existed — a ``--max-frames 1`` request
        # returning a heartbeat and no core. That is not hypothetical for the
        # consumer: the launcher's forced-refresh lane
        # (``mission_control_bridge.dart::_loadSnapshotFromStreamHydrate``, read
        # 2026-08-18) scans stdout for a ``type == "hydrate"`` line and silently
        # returns null when it finds none, so the refresh would no-op with no
        # receipt. The budget counts CONTENT; the tail loop's own heartbeats
        # below still count, because by then a core has been delivered and the
        # consumer is being kept alive rather than kept waiting.
        yield liveness
    if request_cancelled():
        return
    if boot_job.error is not None:
        raise boot_job.error
    if boot_job.snapshot is None:
        raise RuntimeError("snapshot build completed without a result")
    hydrate = hydrate_frame(
        snapshot=boot_job.snapshot,
        delta_patches=delta_patches,
        fold_entities=declared_entities,
        caller=caller,
    )
    if boot_job.elapsed_ms is not None:
        # The receipt ``hydrate_frame`` would have billed itself, billed here
        # because the build moved out from under it. Byte-shaped identically —
        # same reason, same fields, same order — and ``waited_ms`` comes from the
        # job's own measurement, taken ON the build thread: measuring around the
        # wait here would round every build up by as much as one
        # ``_SNAPSHOT_CANCEL_POLL_SECONDS``, which is the exact reason
        # ``_SnapshotBuildJob.elapsed_ms`` exists.
        _log_snapshot_build(
            reason="hydrate",
            waited_ms=boot_job.elapsed_ms,
            offset=(hydrate.get("watermark") or {}).get("event_offset"),
            snapshot=boot_job.snapshot,
            build_info=boot_job.build_info,
        )
    offset = _resume_offset(hydrate)
    if offset is None:
        # Cannot resume from an unknown position. Re-baseline the client on the
        # first batch and re-measure the tail below until the log is readable.
        resync_pending = True
    # Memoize BEFORE the first yield: a generator body pauses at yield, so a
    # memo taken after it would absorb any write racing the consumer's first
    # pull — exactly the writes the watchdog exists to catch.
    known_fingerprint = _scope_fingerprint()
    yield hydrate
    emitted += 1
    if max_frames is not None and emitted >= max_frames:
        return

    last_heartbeat = time.monotonic()
    while True:
        if request_cancelled():
            return
        if offset is None:
            # Still no readable tail. Emit liveness (with an honestly null
            # position) and retry the measurement — never fall back to 0, which
            # would tail the whole log from its head as if it were new.
            if events_watermark().get("event_offset") is None:
                if time.monotonic() - last_heartbeat >= heartbeat_interval_seconds:
                    yield heartbeat_frame(offset=None)
                    emitted += 1
                    last_heartbeat = time.monotonic()
                    if max_frames is not None and emitted >= max_frames:
                        return
                _bounded_sleep(poll_interval_seconds)
                continue
            # The tail is readable again, but the client's baseline predates
            # whatever landed while it was not — and there is no cursor to
            # replay that span from. Re-baseline explicitly (the "explicit
            # resync" this lane exists for): a fresh full core, tailed from ITS
            # OWN measured offset, so the recovery leaves neither a gap nor a
            # replay. Resuming from the newly measured tail alone would silently
            # drop the span; resuming from 0 would re-render the whole log.
            rebaseline = hydrate_frame(
                delta_patches=delta_patches,
                fold_entities=declared_entities,
                caller=caller,
            )
            offset = _resume_offset(rebaseline)
            if offset is None:
                continue
            batch_base = offset
            resync_pending = True
            known_fingerprint = _scope_fingerprint()
            yield rebaseline
            emitted += 1
            last_heartbeat = time.monotonic()
            if max_frames is not None and emitted >= max_frames:
                return
        # Fingerprint BEFORE reading events. A delta batch rebuilds one full
        # snapshot per BATCH (W1 coalescing — it was per event, ~9MB a time);
        # a memo taken AFTER the batch would absorb any event-less write that
        # raced the batch — swallowing forever the exact violations the
        # watchdog exists to catch (found by live proof). Taken before the
        # read, a racing write always lands in a LATER iteration's candidate
        # and reconciles at the next heartbeat.
        fingerprint_candidate = _scope_fingerprint()
        emitted_delta = False
        pending: list[tuple[int, Event]] = []
        # The offset a flushed batch applies FROM (S6 gap detection): the cursor
        # before the batch's first entry. Advanced to the flushed offset after
        # every emit so contiguous batches chain base→watermark→base with no gap.
        batch_base = offset
        for next_offset, event in log.iter_from_offset(offset):
            offset = int(next_offset)
            pending.append((offset, event))
            if len(pending) >= _DELTA_BATCH_CAP:
                for frame in _batch_frames_with_liveness(
                    pending,
                    base_offset=batch_base,
                    delta_patches=delta_patches,
                    resync=resync_pending,
                    heartbeat_interval_seconds=heartbeat_interval_seconds,
                    fold_entities=declared_entities,
                    caller=caller,
                ):
                    yield frame
                    emitted += 1
                    last_heartbeat = time.monotonic()
                    if frame.get("type") != "heartbeat":
                        emitted_delta = True
                    if max_frames is not None and emitted >= max_frames:
                        return
                resync_pending = False
                batch_base = offset
                pending = []
        if pending and delta_debounce_seconds > 0:
            # Settle window: an event burst usually lands over a few tens of
            # milliseconds — one bounded sleep lets the tail join the SAME
            # frame instead of costing a full core each. 200ms sits well
            # inside the declared ≤2×heartbeat staleness SLO.
            time.sleep(delta_debounce_seconds)
            for next_offset, event in log.iter_from_offset(offset):
                offset = int(next_offset)
                pending.append((offset, event))
                if len(pending) >= _DELTA_BATCH_CAP:
                    for frame in _batch_frames_with_liveness(
                        pending,
                        base_offset=batch_base,
                        delta_patches=delta_patches,
                        resync=resync_pending,
                        heartbeat_interval_seconds=heartbeat_interval_seconds,
                        fold_entities=declared_entities,
                        caller=caller,
                    ):
                        yield frame
                        emitted += 1
                        last_heartbeat = time.monotonic()
                        if frame.get("type") != "heartbeat":
                            emitted_delta = True
                        if max_frames is not None and emitted >= max_frames:
                            return
                    resync_pending = False
                    batch_base = offset
                    pending = []
        if pending:
            for frame in _batch_frames_with_liveness(
                pending,
                base_offset=batch_base,
                delta_patches=delta_patches,
                resync=resync_pending,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
                fold_entities=declared_entities,
                caller=caller,
            ):
                yield frame
                emitted += 1
                last_heartbeat = time.monotonic()
                if frame.get("type") != "heartbeat":
                    emitted_delta = True
                if max_frames is not None and emitted >= max_frames:
                    return
            resync_pending = False
        if emitted_delta:
            # Evented mutations legitimately move the fingerprint; adopt the
            # pre-batch candidate so the watchdog only fires on offset-less
            # changes. (An evented write landing between the candidate and the
            # batch read can cause one spurious reconcile — harmless: it is
            # just an extra full-core delta.)
            known_fingerprint = fingerprint_candidate

        if not emitted_delta and time.monotonic() - last_heartbeat >= heartbeat_interval_seconds:
            if fingerprint_candidate != known_fingerprint and _append_state_reconciled(log, fingerprint_candidate):
                known_fingerprint = fingerprint_candidate
                # Skip the sleep: the next iteration reads the appended event
                # and emits the reconcile delta (which resets the heartbeat).
                continue
            yield heartbeat_frame(offset=offset)
            emitted += 1
            last_heartbeat = time.monotonic()
            if max_frames is not None and emitted >= max_frames:
                return

        # Cancellation latency is bounded even when a caller chooses a long
        # poll interval; the production default remains 250ms.
        _bounded_sleep(poll_interval_seconds)


def _scope_fingerprint() -> str:
    """Cheap mtime/size fingerprint of scope/catalog state (Stage 12 backstop).

    Covers exactly the state whose writers have historically slipped the
    event rule or sit outside ``agent_runtime/store.py``: the active-scope
    pointer files, the workspace/realm/persona stores, the blueprint
    catalog, and the head-home SessionDB. Evented, high-churn stores
    (tasks/runs/proofs/incidents) are guarded by the store/event CI
    invariant instead — fingerprinting them here would only mask violations
    that test already prevents.

    The SessionDB matters because the persona-chat directory (Chat History)
    is derived from it and its writers emit no EventLog events: with the S6
    patch lane on, a chat-session mint never appears in any patch frame, so
    watermark-gated consumers kept their hydrate-time chat list for the
    stream's whole lifetime (live incident 2026-07-25: the Launcher's Chat
    History froze for ~36h until a restart re-hydrated). The per-session
    turn-element files are deliberately NOT statted here: element flushes
    land many times per second during a streaming turn and would make the
    watchdog append a reconcile (= one full-core delta) every heartbeat.

    The ``running_work`` durable stores (``processes.json`` + the
    background-work ``state.db``) are the same class as the SessionDB: a
    background process starting or exiting rewrites the checkpoint, and a
    delegation dispatch/finalize writes ``async_delegations`` — with NO
    EventLog event either way. The serve read-model cache adopted them on
    2026-08-03 (``_runtime_state_fingerprint``); this backstop did not, so a
    stream consumer rendered the last pre-exit ``running_work`` row forever
    (live incident 2026-08-11: a 20-second terminal task showed
    "Terminal · running" in the Launcher's Activity panel minutes after the
    durable side had settled). Resolved through the writers' own path
    authority, ``running_work_store_paths`` — never a second path list free
    to drift. Note the background-work ``state.db`` can be a DIFFERENT file
    from the chat SessionDB statted above: the chat scope consults a durable
    head-home pointer the background-work writers do not.

    Both SQLite stores are keyed through ``core_cache.sqlite_fingerprint_triples``
    — the same masked triple the boot-cache lane keys them by — and NOT by a raw
    stat of the three siblings, which is what this function did until
    2026-08-21. The mask collapses "no ``-wal`` on disk" and "a zero-length
    ``-wal`` on disk" into one triple, because SQLite deletes the WAL on a clean
    last-close and re-creates it EMPTY on the next open: the difference between
    those two states is the lifetime of somebody's connection, never content.
    Under the raw stat a poll landing while any process merely HELD the database
    open read a fresh ``mtime_ns``, and a poll landing at rest read ``absent`` —
    so a database nobody was writing flapped the fingerprint twice per open, and
    each flap costs one synthetic ``state.reconciled``, which ``patch_coverage``
    classifies UNCOVERED, which demotes the whole batch to a full core rebuild.
    Measured on the operator's runtime over the 22.16 h to 2026-08-21 09:06:
    2 433 ``snapshot_build reason=demote`` against 35 hydrates (median build_ms
    3 083, max 37 266 — 2.29 h of CPU), and 1 239 ``state.reconciled`` — 96.9 %
    of every event appended in the window — at a median 9.0 s spacing, i.e. the
    watchdog reconciling on roughly every other heartbeat, indefinitely, with
    nothing to reconcile. 3 338 distinct fingerprints over 4 597 reconciles
    against a recurring at-rest anchor is that flip's signature and not a
    write's: real writes do not come back to the same value.

    The narrowing is a NARROWING, not a disabling, and the line it holds is the
    same one the turn-element exclusion above holds. A committed write still
    moves this fingerprint within one poll, by one of two paths that SQLite's
    durability rules leave no gap between: uncheckpointed, the ``-wal`` sibling
    is non-empty and is keyed in full (mask suspended); checkpointed, the frames
    are in ``state.db``, whose own mtime and size are the FIRST triple here. So
    the ≤2×heartbeat staleness SLO that 2026-07-25 and 2026-08-11 bought is
    intact — ``test_scope_fingerprint_covers_head_home_session_db`` and
    ``test_scope_fingerprint_covers_running_work_stores`` still pin those two
    incidents, and ``test_scope_fingerprint_moves_on_committed_chat_write``
    pins the direction a constant fingerprint would trivially break.

    ``PRAGMA data_version`` was evaluated first and REJECTED, recorded here so
    it is not re-proposed as the obvious answer it looks like. Measured on
    SQLite 3.45.3: (a) its value is only comparable WITHIN one connection — a
    fresh connection per poll, which is the only shape a stateless fingerprint
    can take, returns a constant and detects nothing; (b) making it work
    therefore means the stream process holding a SessionDB connection open for
    its whole lifetime, which is the exact shape of MCF-27 (every full snapshot
    build leaked a chat SessionDB connection) two days after that was found;
    (c) it is NOT checkpoint-immune as its reputation suggests — a
    ``wal_checkpoint(TRUNCATE)`` with no data change bumps it, so it does not
    even buy a clean answer for the case the mask leaves uncovered; and (d) it
    buys nothing here anyway. Scenario-by-scenario against this mask — read-only
    open/close, write-capable open/close with no write, WAL creation, WAL
    deletion, ``utime`` on the WAL, uncommitted write, rollback, PASSIVE
    checkpoint, committed write from another PROCESS — the two agree on every
    state except the PASSIVE checkpoint, which cannot occur without a preceding
    commit that both already reported.
    """

    parts: list[str] = []
    for path in (paths.active_realm_path(), paths.active_workspace_path()):
        try:
            stat = path.stat()
            parts.append(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}")
        except OSError:
            parts.append(f"{path.name}:absent")
    directories = [paths.workspaces_dir(), paths.realms_dir(), paths.agents_dir()]
    for directory in directories:
        try:
            entries = [
                entry
                for pattern in ("*.json", "*.yaml", "*.yml")
                for entry in directory.glob(pattern)
            ]
        except OSError:
            continue
        for entry in sorted(entries):
            try:
                stat = entry.stat()
                parts.append(f"{entry.name}:{stat.st_mtime_ns}:{stat.st_size}")
            except OSError:
                continue
    try:
        from .chat_session_scope import chat_session_db_path

        db_path = chat_session_db_path()
        for suffix, mtime_ns, size in core_cache.sqlite_fingerprint_triples(db_path):
            parts.append(f"{db_path.name}{suffix}:{mtime_ns}:{size}")
    except Exception:  # noqa: BLE001 — chat persistence absence is itself stable
        parts.append("session_db:unresolved")
    try:
        from .running_work import running_work_store_paths

        store_paths = running_work_store_paths()
        if not store_paths:
            # An empty tuple means "the home could not be resolved", not
            # "nothing to watch" — same sentinel rule as the serve cache: the
            # part is stable, so an unresolvable home never flaps the
            # fingerprint, but the absence is recorded rather than silent.
            parts.append("running_work_stores:unresolved")
        for store_path in store_paths:
            # The checkpoint is plain JSON; the delegation store is SQLite,
            # whose mutations can land in the WAL without moving the main
            # file's mtime — key the siblings through the shared authority like
            # the chat DB above.
            if store_path.suffix == ".db":
                for suffix, mtime_ns, size in core_cache.sqlite_fingerprint_triples(
                    store_path
                ):
                    parts.append(f"bgwork:{store_path.name}{suffix}:{mtime_ns}:{size}")
                continue
            try:
                stat = store_path.stat()
                parts.append(
                    f"bgwork:{store_path.name}:{stat.st_mtime_ns}:{stat.st_size}"
                )
            except OSError:
                parts.append(f"bgwork:{store_path.name}:absent")
    except Exception:  # noqa: BLE001 — same posture as the chat DB above
        parts.append("running_work_stores:unresolved")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _append_state_reconciled(log: EventLog, fingerprint: str) -> bool:
    """Append the synthetic watchdog event; True when the offset advanced.

    Cross-process guard: if another stream consumer just reconciled the same
    fingerprint, its event already advanced the offset — skip the duplicate
    and let the normal delta path deliver it. Best effort: a broken event log
    degrades to plain heartbeats (bounded UI ageing), never a stream crash.
    """

    try:
        tail = log.tail(1)
        if tail and tail[0].type == "state.reconciled" and tail[0].payload.get("fingerprint") == fingerprint:
            return True
        log.append(
            Event(
                now(),
                "state.reconciled",
                None,
                None,
                None,
                {"fingerprint": fingerprint, "source": "stream_watchdog"},
            )
        )
        return True
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("state.reconciled append failed", exc_info=True)
        return False


def _delta_op(event: Event) -> str:
    # S21 removed three arms whose whole event family is de-registered, so
    # ``EventLog.append`` refuses them and no frame can ever carry them: the
    # task pair (task.upserted / task.state_changed), proof.attached, and the
    # daemon.* prefix. They now fall through to the generic arm like any other
    # unrouted type. Keep this table in step with the event catalog — an arm for
    # a type that cannot be appended is a classifier branch that reads as live.
    event_type = str(event.type or "")
    if event_type.startswith("run.tool.") or event_type == "run.progress":
        return "chat.trace.appended"
    if event_type.startswith("incident."):
        return event_type
    if event_type.startswith("persona_assignment."):
        return "instance.upserted"
    return "event.appended"


def _identity_map(snapshot: dict[str, Any]) -> dict[str, str]:
    identity: dict[str, str] = {}
    for instance in section_rows(snapshot.get("persona_instances")):
        if not isinstance(instance, dict):
            continue
        canonical = _first_text(instance, "persona_instance_id", "instance_id", "id")
        if not canonical:
            continue
        for key in ("persona_instance_id", "instance_id", "id", "agent_profile_id"):
            alias = optional_text(instance.get(key))
            if alias:
                identity[alias] = canonical
        persona_id = optional_text(instance.get("persona_id"))
        if persona_id and persona_id.startswith("profile:"):
            identity[persona_id.replace(":", "_")] = persona_id
    for channel in section_rows(snapshot.get("operator_channels")):
        if not isinstance(channel, dict):
            continue
        canonical = _first_text(channel, "persona_instance_id", "channel_id", "id")
        if not canonical:
            continue
        for key in ("persona_instance_id", "channel_id", "id", "session_id"):
            alias = optional_text(channel.get(key))
            if alias:
                identity[alias] = canonical
    # The snapshot's legacy->canonical aliases (reconciler registry + live
    # structural drift) OVERRIDE the per-row self aliases above: a retired id
    # must resolve to its canonical channel, not to itself.
    for key, value in (snapshot.get("identity_map") or {}).items():
        if isinstance(key, str) and isinstance(value, str) and key and value:
            identity[key] = value
    return identity


def _redaction_safe_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redaction_safe_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redaction_safe_json(item) for item in value[:200]]
    if isinstance(value, tuple):
        return [_redaction_safe_json(item) for item in value[:200]]
    if isinstance(value, str):
        return _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[redacted]", value)
    return to_jsonable(value)


def _first_text(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        text = optional_text(payload.get(key))
        if text:
            return text
    return None

