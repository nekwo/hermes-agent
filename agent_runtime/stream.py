from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Iterator
from typing import Any

from hermes_time import now

from . import paths
from .events import EventLog
from .models import Event
from .serde import to_jsonable
from .snapshot import build_snapshot

STREAM_SCHEMA_VERSION = 1

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b((?:[A-Za-z0-9]+_)*(?:SECRET|TOKEN|PASSWORD|PASS|CREDENTIAL|API_?KEY|KEY)(?:_[A-Za-z0-9]+)*)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s'\"]+)"
)


def hydrate_frame(snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the initial warm-stream hydrate frame.

    The hydrate carries the existing full snapshot as the read model payload so
    the stream is additive: the one-shot snapshot remains the canonical fallback
    and consumers can converge by applying this frame exactly like a fresh
    snapshot response.
    """

    snap = snapshot if snapshot is not None else build_snapshot()
    parity = snap.get("parity") if isinstance(snap.get("parity"), dict) else {}
    watermark = parity.get("watermark") if isinstance(parity.get("watermark"), dict) else {}
    return {
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


def heartbeat_frame(*, offset: int) -> dict[str, Any]:
    """Liveness frame that advances the stream watermark without a core delta.

    Pure liveness telemetry: consumers merge it fire-and-forget and a dropped
    frame only ages the HUD, never runtime state. (This frame previously also
    carried the Mission Daemon status block; the background daemon was retired.)
    """

    return {
        "type": "heartbeat",
        "schema_version": STREAM_SCHEMA_VERSION,
        "generated_at": now(),
        "watermark": {"event_offset": int(offset or 0), "captured_at": now()},
    }


def delta_frame(event: Event, *, offset: int, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = _redaction_safe_json(event.payload)
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
        "entity": {
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
        },
        "core": snapshot if snapshot is not None else build_snapshot(),
    }


def stream_frames(
    *,
    event_log: EventLog | None = None,
    poll_interval_seconds: float = 0.25,
    heartbeat_interval_seconds: float = 5.0,
    max_frames: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield hydrate, delta, and heartbeat frames for ``hermes harness stream``.

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
    """

    log = event_log or EventLog()
    emitted = 0
    hydrate = hydrate_frame()
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
        # Fingerprint BEFORE reading events. A delta batch rebuilds a full
        # snapshot per event (slow); a memo taken AFTER the batch would absorb
        # any event-less write that raced the batch — swallowing forever the
        # exact violations the watchdog exists to catch (found by live proof).
        # Taken before the read, a racing write always lands in a LATER
        # iteration's candidate and reconciles at the next heartbeat.
        fingerprint_candidate = _scope_fingerprint()
        emitted_delta = False
        for next_offset, event in log.iter_from_offset(offset):
            offset = int(next_offset)
            yield delta_frame(event, offset=offset)
            emitted += 1
            emitted_delta = True
            last_heartbeat = time.monotonic()
            if max_frames is not None and emitted >= max_frames:
                return
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

        time.sleep(max(0.01, float(poll_interval_seconds)))


def _scope_fingerprint() -> str:
    """Cheap mtime/size fingerprint of scope/catalog state (Stage 12 backstop).

    Covers exactly the state whose writers have historically slipped the
    event rule or sit outside ``agent_runtime/store.py``: the active-scope
    pointer files, the workspace/realm/persona stores, and the blueprint
    catalog. Evented, high-churn stores (tasks/runs/proofs/incidents) are
    guarded by the store/event CI invariant instead — fingerprinting them
    here would only mask violations that test already prevents.
    """

    parts: list[str] = []
    for path in (paths.active_realm_path(), paths.active_workspace_path()):
        try:
            stat = path.stat()
            parts.append(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}")
        except OSError:
            parts.append(f"{path.name}:absent")
    directories = [paths.workspaces_dir(), paths.realms_dir(), paths.agents_dir()]
    try:
        from .blueprints.store import BlueprintStore

        directories.extend(BlueprintStore().roots)
    except Exception:  # noqa: BLE001 — catalog fingerprint is best-effort
        pass
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
    event_type = str(event.type or "")
    if event_type.startswith("run.tool.") or event_type == "run.progress":
        return "chat.trace.appended"
    if event_type.startswith("task."):
        return "task.state_changed" if event_type in {"task.transition", "task.blocked", "task.unblocked", "task.cancelled", "task.archived"} else "task.upserted"
    if event_type == "proof.attached":
        return "proof.added"
    if event_type.startswith("incident."):
        return event_type
    if event_type.startswith("daemon."):
        return "daemon.status"
    if event_type.startswith("persona_assignment."):
        return "instance.upserted"
    return "event.appended"


def _identity_map(snapshot: dict[str, Any]) -> dict[str, str]:
    identity: dict[str, str] = {}
    for instance in snapshot.get("persona_instances") or []:
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
    for channel in snapshot.get("operator_channels") or []:
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
