from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from collections.abc import Collection, Iterator
from datetime import datetime
from typing import Any

from . import event_rotation, paths
from .decision_contract_registry import allowed_event_types, validate_event_payload
from .errors import EventPayloadTooLarge
from .locks import events_lock
from .models import Event
from .serde import from_jsonable, to_jsonable

EVENT_PAYLOAD_LIMIT_BYTES = 4096

# Match compact-JSON top-level id tokens while still treating the parsed event
# as authoritative. Payload copies may add candidates, but never false results.
_INDEXED_EVENT_ID_TOKEN_RE = re.compile(
    r'"(?:task_id|session_id)":(?:(?:"(?:\\.|[^"\\])*")|null)'
)

# Env override for the rotation cap (operator/test knob). When unset the
# config-derived cap is used, memoized per store root so the hot append path
# does not re-parse config on every event.
_ROTATION_CAP_ENV = "HERMES_EVENT_LOG_ROTATION_CAP_BYTES"
_ROTATION_CAP_CACHE: dict[str, int] = {}


def _rotation_cap_bytes() -> int:
    """Resolve the live-slice rotation cap. Env override wins; else the
    ``event_log.rotation_cap_bytes`` config value (default 16 MiB), memoized per
    store root. ``0`` disables rotation."""

    env = os.environ.get(_ROTATION_CAP_ENV)
    if env:
        try:
            value = int(env)
            if value >= 0:
                return value
        except (TypeError, ValueError):
            pass
    try:
        root = str(paths.store_root())
    except Exception:
        return event_rotation.DEFAULT_ROTATION_CAP_BYTES
    cached = _ROTATION_CAP_CACHE.get(root)
    if cached is not None:
        return cached
    cap = event_rotation.DEFAULT_ROTATION_CAP_BYTES
    try:
        from .config import load_root_runtime_config

        raw = getattr(getattr(load_root_runtime_config(), "event_log", None), "rotation_cap_bytes", None)
        if raw is not None:
            cap = max(0, int(raw))
    except Exception:
        cap = event_rotation.DEFAULT_ROTATION_CAP_BYTES
    _ROTATION_CAP_CACHE[root] = cap
    return cap

ALLOWED_EVENT_TYPES = allowed_event_types()

# Every row here must also be in ``ALLOWED_EVENT_TYPES`` — a summary rule for a
# de-registered type describes a shape ``EventLog.append`` refuses, so it can
# only ever be exercised by a test minting an event no reader will ever see.
# S21 dropped ``delivery.intent``, ``patch.proposed``, ``role_session.closed``,
# and ``run.approval_required`` for exactly that reason (S15 de-registered all
# four), together with their branches in ``operator_event_summary``.
# S25 dropped ``run.opened`` under the same rule once its contract went (its
# writer ``RunStore.open_run`` went at S17). Historical rows are unaffected —
# ``append`` type-checks on WRITE only, so the 1,882 ``run.opened`` rows in the
# live log still read back — and none of them lose a rendered summary in
# practice: ``operator_event_summary``'s only read-side consumer is
# ``snapshot._event_display_projection``, which is reached solely from three
# task-scoped projections that have had ZERO callers since the ``Task`` record
# went at S8. ``repo_bundle.delivered`` went in the same pass and for the same
# rule, one commit behind its emitter: S24 deleted
# ``RepoBundleStore.mark_delivered``, the only writer that ever produced it,
# while every other ``repo_bundle.*`` type still rode a live
# ``RepoBundleStore.update``.
#
# S52 (2026-08-01) finished that lane: the whole ``RepoBundleStore`` WRITE side
# was deleted for want of a production caller, so ``repo_bundle.updated`` and
# ``repo_bundle.assigned`` -- the last two ``repo_bundle.*`` types in this
# frozenset -- were de-registered with it and removed from here. The S21/S25
# invariant is why this edit is not optional: this frozenset may not name a
# de-registered type, and ``operator_event_summary`` early-returns ``None``
# outside it, so their shared formatter arm became unreachable the moment the
# registration went and was removed in the same commit.
OPERATOR_SUMMARY_EVENT_TYPES = frozenset(
    {
        "run.closed",
        "run.progress",
        "run.tool.started",
        "run.tool.finished",
    }
)


def _strict_event_contracts() -> bool:
    """Contract validation posture (Stage 12 D): strict in tests/CI via
    HERMES_EVENT_CONTRACT_STRICT, observe-and-warn live — a validation bug
    must never take down the runtime, but CI must not let a violation land."""

    return str(os.environ.get("HERMES_EVENT_CONTRACT_STRICT", "")).strip().lower() in {"1", "true", "yes", "on"}


# Observe-mode dedupe: warn once per (type, missing-fields) shape per process.
# High-frequency emitters (run.heartbeat every few seconds) must not turn a
# contract drift into log spam; one line per shape is enough to find the bug.
_WARNED_CONTRACT_SHAPES: set[tuple[str, tuple[str, ...]]] = set()


class EventLog:
    def append(self, evt: Event) -> None:
        if evt.type not in ALLOWED_EVENT_TYPES:
            raise ValueError(f"unknown event type: {evt.type}")
        missing = validate_event_payload(evt.type, evt.payload)
        if missing:
            message = f"event {evt.type} payload missing contract summary fields: {missing}"
            if _strict_event_contracts():
                raise ValueError(message)
            shape = (evt.type, missing)
            if shape not in _WARNED_CONTRACT_SHAPES:
                _WARNED_CONTRACT_SHAPES.add(shape)
                logging.getLogger(__name__).warning(message)
        evt = event_with_operator_summary(evt)
        payload_bytes = json.dumps(to_jsonable(evt.payload), ensure_ascii=False).encode("utf-8")
        if len(payload_bytes) > EVENT_PAYLOAD_LIMIT_BYTES:
            raise EventPayloadTooLarge(
                f"event payload is {len(payload_bytes)} bytes; limit is {EVENT_PAYLOAD_LIMIT_BYTES}"
            )
        line = json.dumps(to_jsonable(evt), ensure_ascii=False, separators=(",", ":"))
        with events_lock():
            # Rotate-before-write (under the lock): if the current live slice has
            # reached the cap, seal it and open a fresh one, THEN write this line
            # into the fresh slice — the live slice is never left empty after a
            # completed append (keeps tail(1)/watermark honest). Windows-safe:
            # rotation only creates a new file + rewrites the manifest, so a
            # concurrent reader holding the sealed file open is never disturbed.
            path = event_rotation.prepare_live_for_append(_rotation_cap_bytes())
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()

    def tail(self, n: int) -> list[Event]:
        if n <= 0:
            return []
        # Walk slices newest-first, prepending each older slice's lines, until we
        # have at least n. Pristine (single live slice) reads only events.jsonl —
        # byte-identical to the old ``read_text().splitlines()[-n:]``.
        collected: list[str] = []
        for sl in event_rotation.reversed_slices():
            if not sl.path.exists():
                continue
            lines = [line for line in sl.path.read_text(encoding="utf-8").splitlines() if line.strip()]
            collected = lines + collected
            if len(collected) >= n:
                break
        tail_lines = collected[-n:]
        return [from_jsonable(Event, json.loads(line)) for line in tail_lines]

    def iter_all(self) -> Iterator[Event]:
        for source in event_rotation.ordered_line_sources():
            if not source.exists():
                continue
            with open(source, encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        yield from_jsonable(Event, json.loads(line))

    def iter_from_offset(self, offset: int) -> Iterator[tuple[int, Event]]:
        # Logical-offset tailing across slices: resolve which slice(s) a logical
        # offset lives in and seek there, yielding logical offsets throughout so
        # every reader/watermark resolves unchanged across a rotation boundary.
        start = max(0, int(offset or 0))
        for slice_path, slice_start, seek_within in event_rotation.offset_reads(start):
            if not slice_path.exists():
                continue
            with open(slice_path, "rb") as handle:
                handle.seek(seek_within)
                for raw in handle:
                    if not raw.strip():
                        continue
                    logical_offset = slice_start + handle.tell()
                    yield logical_offset, from_jsonable(Event, json.loads(raw.decode("utf-8")))

    def _for_matching(
        self,
        token: str,
        match,
        *,
        limit: int,
        since: datetime | None,
        types: Collection[str] | None,
    ) -> list[Event]:
        """Newest-first reverse scan across slices (newest slice first), stopping
        once ``limit`` matches are collected, returned oldest-first. Pristine
        (single slice) is byte-identical to the old whole-file reverse scan; when
        rotation is active the scan reaches back into sealed slices only as far as
        the limit needs, so newest-N stays cheap."""

        type_tokens = _type_json_tokens(types)
        selected: list[Event] = []
        for sl in event_rotation.reversed_slices():
            if not sl.path.exists():
                continue
            for line in reversed(sl.path.read_text(encoding="utf-8").splitlines()):
                if token not in line:
                    continue
                if type_tokens is not None and not any(t in line for t in type_tokens):
                    continue
                evt = from_jsonable(Event, json.loads(line))
                if not match(evt):
                    continue
                if types is not None and evt.type not in types:
                    continue
                if since is not None and evt.ts < since:
                    continue
                selected.append(evt)
                if limit > 0 and len(selected) >= limit:
                    return list(reversed(selected))
        return list(reversed(selected))

    def for_task(
        self,
        task_id: str,
        *,
        limit: int = 50,
        since: datetime | None = None,
        types: Collection[str] | None = None,
    ) -> list[Event]:
        """Return the newest events for ``task_id``, oldest-first.

        When ``types`` is given, ``limit`` counts only events whose ``type`` is in
        that set — a busy task whose recent tail is flooded with non-matching rows
        (e.g. an incident loop) can no longer starve the window before the type
        filter runs. The type-token substring pre-filter keeps the reverse scan
        cheap even against such floods.
        """

        return self._for_matching(
            _task_id_json_token(task_id),
            lambda evt: evt.task_id == task_id,
            limit=limit,
            since=since,
            types=types,
        )

    def for_session(
        self,
        session_id: str,
        *,
        limit: int = 50,
        since: datetime | None = None,
        types: Collection[str] | None = None,
    ) -> list[Event]:
        """Return events bound to a conversational chat ``session_id``.

        Mirrors :meth:`for_task` but matches on the event ``session_id`` lineage
        instead of ``task_id``. Used by the snapshot trace projection to surface
        tool/progress events recorded during a (non-task) persona chat turn. The
        substring pre-filter keeps the reverse scan cheap; task-run events carry
        ``"session_id":null`` and are skipped by both the token and the
        post-decode equality check. ``types`` behaves as in :meth:`for_task`.
        """

        return self._for_matching(
            _session_id_json_token(session_id),
            lambda evt: evt.session_id == session_id,
            limit=limit,
            since=since,
            types=types,
        )

    def iter_since(self, ts: datetime) -> Iterator[Event]:
        for source in event_rotation.ordered_line_sources():
            if not source.exists():
                continue
            with open(source, encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    evt = from_jsonable(Event, json.loads(line))
                    if evt.ts >= ts:
                        yield evt


# S54 removed ``archive_task_events`` and ``compact_archived_task_events``.
# Both were task-scoped event archivers whose last caller went with the ``Task``
# record at S8; the only references left were their own tests.

def event_log_health() -> dict[str, Any]:
    path = paths.events_path()
    rotation = event_rotation.rotation_health()
    # Whole-log totals span all slices; ``exists`` stays keyed on the canonical
    # base file for backward compatibility (it remains present as the sealed
    # base-0 slice after rotation).
    size_bytes = rotation["total_bytes"]
    line_count = rotation["total_lines"]
    exists = path.exists() or size_bytes > 0
    archive = _archived_event_slices()
    return {
        "exists": exists,
        "size_bytes": size_bytes,
        "line_count": line_count,
        "archived_event_slices": len(archive["slices"]),
        "archived_event_rows": archive["row_count"],
        "index_health": "ok" if exists or archive["row_count"] == 0 else "archive_only",
        # Rotation accounting (C6a): the live slice is bounded; sealed rotation
        # slices are archive-never-delete and offset-load-bearing.
        "rotated_slice_count": rotation["rotated_slice_count"],
        "live_slice_file": rotation["live_slice_file"],
        "live_slice_bytes": rotation["live_slice_bytes"],
        "live_slice_lines": rotation["live_slice_lines"],
        "log_end_offset": rotation["log_end_offset"],
    }


class CachedEventLog(EventLog):
    """Build-scoped EventLog that reads the event log ONCE and serves every read
    from the cached raw lines.

    A single snapshot build calls ``for_task`` / ``for_session`` / ``tail`` dozens
    of times; the base ``EventLog`` re-reads + re-splits the entire log on each
    call (the dominant repeated cost). This caches the split lines once and keeps
    the base's *selective* parse (substring pre-filter → ``json.loads`` only on
    matching lines), so it dedupes the file I/O without paying to parse every
    event. It is a point-in-time view — created per build and discarded, so appends
    made elsewhere during the build are intentionally not reflected.

    Rotation (C6a): the cache concatenates every slice oldest-first (rotated
    archive slices + live). Slices are contiguous in logical-offset space and the
    first slice starts at logical 0, so the flat concatenation's cumulative byte
    position IS the logical offset — ``iter_from_offset`` keeps resolving watermark
    cursors unchanged. Pristine (single live slice) reads only ``events.jsonl``,
    exactly one ``read_text``, as before.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lines: list[str] | None = None
        self._lines_by_id_token: dict[str, list[str]] | None = None

    def _cached_lines(self) -> list[str]:
        if self._lines is None:
            lines: list[str] = []
            for source in event_rotation.ordered_line_sources():
                if source.exists():
                    lines.extend(source.read_text(encoding="utf-8").splitlines())
            self._lines = lines
            indexed: dict[str, list[str]] = {}
            for line in lines:
                # One line per token bucket regardless of how many times the token
                # appears in the line — a payload echoing its own id (lane.created
                # repeats task_id inside payload) must not double the event. Two
                # DIFFERENT tokens on one line still index it once each.
                for token in {match.group(0) for match in _INDEXED_EVENT_ID_TOKEN_RE.finditer(line)}:
                    indexed.setdefault(token, []).append(line)
            self._lines_by_id_token = indexed
        return self._lines

    def _scan(
        self,
        token: str,
        match,
        *,
        limit: int,
        since: datetime | None,
        types: Collection[str] | None = None,
    ) -> list[Event]:
        type_tokens = _type_json_tokens(types)
        selected: list[Event] = []
        self._cached_lines()
        candidates = (self._lines_by_id_token or {}).get(token, ())
        for line in reversed(candidates):
            if token not in line:
                continue
            if type_tokens is not None and not any(type_token in line for type_token in type_tokens):
                continue
            evt = from_jsonable(Event, json.loads(line))
            if not match(evt):
                continue
            if types is not None and evt.type not in types:
                continue
            if since is not None and evt.ts < since:
                continue
            selected.append(evt)
            if limit > 0 and len(selected) >= limit:
                break
        return list(reversed(selected))

    def for_task(
        self,
        task_id: str,
        *,
        limit: int = 50,
        since: datetime | None = None,
        types: Collection[str] | None = None,
    ) -> list[Event]:
        return self._scan(
            _task_id_json_token(task_id),
            lambda evt: evt.task_id == task_id,
            limit=limit,
            since=since,
            types=types,
        )

    def for_session(
        self,
        session_id: str,
        *,
        limit: int = 50,
        since: datetime | None = None,
        types: Collection[str] | None = None,
    ) -> list[Event]:
        return self._scan(
            _session_id_json_token(session_id),
            lambda evt: evt.session_id == session_id,
            limit=limit,
            since=since,
            types=types,
        )

    def tail(self, n: int) -> list[Event]:
        if n <= 0:
            return []
        return [from_jsonable(Event, json.loads(line)) for line in self._cached_lines()[-n:] if line.strip()]

    def iter_all(self) -> Iterator[Event]:
        for line in self._cached_lines():
            if line.strip():
                yield from_jsonable(Event, json.loads(line))

    def iter_from_offset(self, offset: int) -> Iterator[tuple[int, Event]]:
        current = 0
        start = max(0, int(offset or 0))
        for line in self._cached_lines():
            raw = (line + "\n").encode("utf-8")
            current += len(raw)
            if current <= start or not line.strip():
                continue
            yield current, from_jsonable(Event, json.loads(line))

    def iter_since(self, ts: datetime) -> Iterator[Event]:
        for line in self._cached_lines():
            if not line.strip():
                continue
            evt = from_jsonable(Event, json.loads(line))
            if evt.ts >= ts:
                yield evt


def _task_id_json_token(task_id: str) -> str:
    encoded = json.dumps(str(task_id), ensure_ascii=False, separators=(",", ":"))
    return f'"task_id":{encoded}'


def _session_id_json_token(session_id: str) -> str:
    encoded = json.dumps(str(session_id), ensure_ascii=False, separators=(",", ":"))
    return f'"session_id":{encoded}'


def _type_json_tokens(types: Collection[str] | None) -> tuple[str, ...] | None:
    """Literal ``"type":"…"`` substrings for the compact-JSON line pre-filter.

    Safe because :meth:`EventLog.append` writes every line with
    ``separators=(",", ":")`` — the token form is stable. ``None`` disables the
    pre-filter (untyped scan).
    """

    if types is None:
        return None
    return tuple(
        f'"type":{json.dumps(str(event_type), ensure_ascii=False, separators=(",", ":"))}'
        for event_type in sorted(types)
    )


# S54 also took ``_safe_event_task_filename`` and ``_line_is_compacted_event``
# with the two archivers above -- each was reachable only from them. A private
# helper outliving its only caller is the residue S25 named when it retired
# ``_safe_int`` with the formatter arm that used it.

def _archived_event_slices() -> dict[str, Any]:
    archive_root = paths.deleted_archive_dir()
    line_counts: Counter[str] = Counter()
    task_ids: set[str] = set()
    slices: list[dict[str, Any]] = []
    row_count = 0
    if not archive_root.exists():
        return {"line_counts": line_counts, "task_ids": task_ids, "slices": slices, "row_count": row_count}
    for manifest_path in sorted(archive_root.glob("*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        batch_dir = manifest_path.parent
        for item in manifest.get("archived_tasks") or []:
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("task_id") or "").strip()
            event_rel = str(item.get("events_path") or "").strip()
            if not task_id or not event_rel:
                continue
            event_path = batch_dir / event_rel
            if not event_path.exists():
                continue
            task_ids.add(task_id)
            count = 0
            with open(event_path, encoding="utf-8") as handle:
                for line in handle:
                    normalized = line if line.endswith("\n") else f"{line}\n"
                    if normalized.strip():
                        line_counts[normalized] += 1
                        count += 1
            row_count += count
            slices.append(
                {
                    "archive_batch": batch_dir.name,
                    "task_id": task_id,
                    "events_path": event_rel,
                    "event_count": count,
                }
            )
    return {"line_counts": line_counts, "task_ids": task_ids, "slices": slices, "row_count": row_count}


def event_with_operator_summary(evt: Event) -> Event:
    """Ensure operator-visible events always carry a redaction-safe summary."""

    payload = evt.payload if isinstance(evt.payload, dict) else {}
    summary = operator_event_summary(evt)
    changed = payload is not evt.payload
    if evt.type == "run.progress" and not _safe_text(payload.get("event_id")):
        event_id = _progress_event_id(evt, payload)
        if event_id:
            payload = dict(payload)
            payload["event_id"] = event_id
            changed = True
    if summary and _safe_text(payload.get("summary")) != summary:
        payload = dict(payload)
        payload["summary"] = summary
        changed = True
    if not changed:
        return evt
    return Event(
        ts=evt.ts,
        type=evt.type,
        task_id=evt.task_id,
        run_id=evt.run_id,
        persona_id=evt.persona_id,
        payload=payload,
        session_id=evt.session_id,
        turn_id=evt.turn_id,
    )


def operator_event_summary(evt: Event) -> str | None:
    if evt.type not in OPERATOR_SUMMARY_EVENT_TYPES:
        return None
    payload = evt.payload if isinstance(evt.payload, dict) else {}
    existing = _safe_text(payload.get("summary"))
    if existing:
        return existing
    event_type = evt.type
    if event_type == "run.closed":
        actor = _label(evt.persona_id, "agent")
        state = _safe_text(payload.get("state")) or "closed"
        decision = _safe_text(payload.get("decision_type"))
        return f"Closed {actor} run as {state}" + (f" after {decision}." if decision else ".")
    # S52 removed the shared ``repo_bundle.assigned`` / ``repo_bundle.updated``
    # arm that stood here. Both types left OPERATOR_SUMMARY_EVENT_TYPES with the
    # RepoBundleStore write lane, and this function early-returns None above for
    # anything outside that frozenset, so the arm was unreachable the moment the
    # registration went -- the same S25 rule that retired the ``delivered`` arm
    # and its ``_safe_int`` helper.
    if event_type == "run.progress":
        parts = [
            _safe_text(payload.get("phase")),
            _safe_text(payload.get("step")),
            _safe_text(payload.get("status")),
        ]
        text = " ".join(part for part in parts if part)
        return f"Progress: {text}." if text else "Run progress update."
    if event_type.startswith("run.tool."):
        tool = _safe_text(payload.get("tool_name")) or _safe_text(payload.get("tool")) or "tool"
        status = _safe_text(payload.get("status")) or ("started" if event_type.endswith(".started") else "finished")
        return f"{tool} {status}."
    return event_type.replace(".", " ").title() + "."


def event_summary_missing(evt: Event) -> bool:
    if evt.type not in OPERATOR_SUMMARY_EVENT_TYPES:
        return False
    payload = evt.payload if isinstance(evt.payload, dict) else {}
    return not bool(_safe_text(payload.get("summary")))


def _progress_event_id(evt: Event, payload: dict) -> str | None:
    run_id = _safe_token(evt.run_id) or _safe_token(payload.get("run_id"))
    if not run_id:
        return None
    phase = _safe_token(payload.get("phase"))
    if not phase:
        return None
    step = _safe_token(payload.get("step")) or "update"
    command_index = _safe_token(payload.get("command_index"))
    timing_key = _safe_token(payload.get("timing_key"))
    proof_id = _safe_token(payload.get("proof_id"))
    parts = ["progress", run_id, phase, step]
    for value in (command_index, timing_key, proof_id):
        if value:
            parts.append(value)
    return ":".join(parts)


def _safe_text(value, *, limit: int = 240) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).strip().split())
    if not text or _looks_sensitive_or_pathish(text):
        return None
    return f"{text[: limit - 3]}..." if len(text) > limit else text


def _label(value, fallback: str) -> str:
    return (_safe_text(value, limit=80) or fallback).replace("_", " ")


def _safe_token(value) -> str | None:
    text = str(value or "").strip()
    if not text or _looks_sensitive_or_pathish(text):
        return None
    safe = re.sub(r"[^A-Za-z0-9_.:-]+", "_", text)[:120].strip("_")
    return safe or None


def _looks_sensitive_or_pathish(value: str) -> bool:
    lowered = value.lower()
    sensitive_markers = (
        "secret",
        "token",
        "password",
        "api_key",
        "apikey",
        "authorization",
        "bearer",
        "credential",
        "cookie",
        "private_key",
        "sk-",
        "passwd",
    )
    if any(marker in lowered for marker in sensitive_markers):
        return True
    if ":/" in value or "\\" in value or value.startswith(("/", "~")):
        return True
    if re.search(r"(^|\s)([A-Za-z]:)?[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        return True
    return bool(re.search(r"(^|\s)[A-Za-z0-9_.-]+\.(dart|py|js|ts|json|yaml|yml|md|txt)/", value, re.IGNORECASE))
