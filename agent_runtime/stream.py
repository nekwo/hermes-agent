from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Iterator
from typing import Any

from hermes_time import now

from . import paths
from .events import EventLog
from .models import Event
from .patch_coverage import batch_is_patch_coverable
from .redaction import ENV_SECRET_ASSIGNMENT_RE
from .request_control import request_cancelled
from .serde import to_jsonable
from .snapshot import build_snapshot
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

# Single-homed in ``agent_runtime.redaction`` — see the header there for the
# JSON blind spot every local spelling shared. group(1) is still the full key,
# so the ``f"{match.group(1)}=[redacted]"`` rebuild below is unchanged.
_SECRET_ASSIGNMENT_RE = ENV_SECRET_ASSIGNMENT_RE


def hydrate_frame(
    snapshot: dict[str, Any] | None = None, *, delta_patches: bool = False
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
    """

    # ``accept_inflight``: this hydrate may ride the build that is ALREADY
    # running (serve prewarms one right after ``ready``). It loses no event —
    # the frame's ``watermark.event_offset`` is read back out of the snapshot
    # below and ``stream_frames`` tails from exactly that offset, so anything
    # appended after the shared build arrives as the first delta instead.
    # Requiring a newer build would make the launcher's boot hydrate wait for
    # the prewarm AND then pay a second build — strictly worse than no prewarm.
    snap = snapshot if snapshot is not None else build_snapshot(accept_inflight=True)
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
    return frame


def heartbeat_frame(
    *, offset: int, activity: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Liveness frame that advances the stream watermark without a core delta.

    Pure liveness telemetry: consumers merge it fire-and-forget and a dropped
    frame only ages the HUD, never runtime state. (This frame previously also
    carried the Mission Daemon status block; the background daemon was retired.)
    """

    frame = {
        "type": "heartbeat",
        "schema_version": STREAM_SCHEMA_VERSION,
        "generated_at": now(),
        "watermark": {"event_offset": int(offset or 0), "captured_at": now()},
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
    """

    if not batch:
        raise ValueError("patch_batch_frame requires a non-empty batch")
    last_offset, last_event = batch[-1]
    patches = [
        {"seq": int(offset or 0), "ts": event.ts, **_redaction_safe_json(event.payload)}
        for offset, event in batch
        if event.type == STATE_PATCHED_EVENT_TYPE
    ]
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


class _SnapshotBuildJob:
    """Finite daemon build used by the stream's liveness envelope.

    Snapshot construction is synchronous and can involve filesystem parsing.
    Running it on a daemon worker lets the stream generator keep emitting the
    already-applied watermark as a heartbeat and observe cooperative request
    cancellation. A cancelled consumer does not wait for a finite build to
    finish; the build may complete in the background and remains protected by
    ``build_snapshot``'s existing coalescing contract.
    """

    def __init__(self) -> None:
        self.done = threading.Event()
        self.snapshot: dict[str, Any] | None = None
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.snapshot = build_snapshot()
        except BaseException as exc:  # re-raised on the stream worker
            self.error = exc
        finally:
            self.done.set()


def _full_core_batch_frames(
    batch: list[tuple[int, Event]],
    *,
    base_offset: int,
    heartbeat_interval_seconds: float,
) -> Iterator[dict[str, Any]]:
    """Emit liveness while one uncovered batch builds its authoritative core."""

    job = _SnapshotBuildJob()
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
            # Keep the watermark at the last APPLIED core. Advertising the
            # drained batch's future offset here would make the launcher infer a
            # missed delta and start a second hydrate while this build is healthy.
            yield heartbeat_frame(
                offset=base_offset,
                activity={
                    "kind": "snapshot_build",
                    "state": "busy",
                    "elapsed_ms": int((current - started) * 1000),
                },
            )
            next_heartbeat = current + heartbeat_interval
    if request_cancelled():
        return
    if job.error is not None:
        raise job.error
    if job.snapshot is None:
        raise RuntimeError("snapshot build completed without a result")
    yield delta_batch_frame(batch, snapshot=job.snapshot)


def _batch_frames_with_liveness(
    batch: list[tuple[int, Event]],
    *,
    base_offset: int,
    delta_patches: bool,
    resync: bool,
    heartbeat_interval_seconds: float,
) -> Iterator[dict[str, Any]]:
    if delta_patches and not resync and batch_is_patch_coverable(event for _, event in batch):
        yield patch_batch_frame(batch, base_offset=base_offset)
        return
    yield from _full_core_batch_frames(
        batch,
        base_offset=base_offset,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
    )


def stream_frames(
    *,
    event_log: EventLog | None = None,
    poll_interval_seconds: float = 0.25,
    heartbeat_interval_seconds: float = 5.0,
    delta_debounce_seconds: float = 0.2,
    max_frames: int | None = None,
    resync: bool = False,
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
    """

    log = event_log or EventLog()
    delta_patches = delta_patches_enabled()
    resync_pending = bool(resync)
    emitted = 0
    hydrate = hydrate_frame(delta_patches=delta_patches)
    offset = int(((hydrate.get("watermark") or {}).get("event_offset")) or 0)
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
        sleep_remaining = max(0.01, float(poll_interval_seconds))
        while sleep_remaining > 0 and not request_cancelled():
            interval = min(_SNAPSHOT_CANCEL_POLL_SECONDS, sleep_remaining)
            time.sleep(interval)
            sleep_remaining -= interval


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
        for suffix in ("", "-wal", "-journal"):
            candidate = db_path.with_name(db_path.name + suffix)
            try:
                stat = candidate.stat()
                parts.append(f"{candidate.name}:{stat.st_mtime_ns}:{stat.st_size}")
            except OSError:
                parts.append(f"{candidate.name}:absent")
    except Exception:  # noqa: BLE001 — chat persistence absence is itself stable
        parts.append("session_db:unresolved")
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


def _rows(value: Any) -> list:
    """A snapshot section S4 emits as an id-keyed map, read as an ordered list
    of rows (map values). Also accepts a plain list / ``None``."""

    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, list):
        return list(value)
    return []


def _identity_map(snapshot: dict[str, Any]) -> dict[str, str]:
    identity: dict[str, str] = {}
    for instance in _rows(snapshot.get("persona_instances")):
        if not isinstance(instance, dict):
            continue
        canonical = _first_text(instance, "persona_instance_id", "instance_id", "id")
        if not canonical:
            continue
        for key in ("persona_instance_id", "instance_id", "id", "agent_profile_id"):
            alias = _text(instance.get(key))
            if alias:
                identity[alias] = canonical
        persona_id = _text(instance.get("persona_id"))
        if persona_id and persona_id.startswith("profile:"):
            identity[persona_id.replace(":", "_")] = persona_id
    for channel in _rows(snapshot.get("operator_channels")):
        if not isinstance(channel, dict):
            continue
        canonical = _first_text(channel, "persona_instance_id", "channel_id", "id")
        if not canonical:
            continue
        for key in ("persona_instance_id", "channel_id", "id", "session_id"):
            alias = _text(channel.get(key))
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
        text = _text(payload.get(key))
        if text:
            return text
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
