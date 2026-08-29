"""Durable drain file for ``process notify`` — process-exit delivery requests.

One row per background process an agent asked to be TOLD ABOUT: the agent fires
a long verb, calls ``process notify``, and ends its turn; when the process
exits, ``ProcessRegistry._move_to_finished`` reads the row here and publishes a
completion the serve delivery drain forges into a real turn in that agent's own
chat thread (``agent_runtime.dispatch_delivery.drain_background_completions``).

Why the row is durable and not a flag on the session
----------------------------------------------------
The whole value of the lane is that the agent does not sit still: its turn ENDS
between the request and the exit. A flag on an in-memory session says nothing
about WHO asked or WHERE the answer goes, and the two facts the delivery needs —
the chat root and the persona instance that owns it — are only knowable at
request time, inside the turn, from the run's own scope. Recorded there, read at
exit, exactly the way the dispatch lane records a sender before its target
starts working (``agent_runtime.dispatch_store``).

Where the file lives — and why that is not ``get_hermes_home()``
----------------------------------------------------------------
Beside ``processes.json``, through the SAME resolver
(:func:`hermes_constants.get_hermes_background_work_home`) the process
checkpoint, the ``async_delegations`` store and the ``running_work`` projection
already agree on. ``persona_profile_context`` flips ``HERMES_HOME``
process-globally for the duration of a persona turn, and a notify request is
MADE FROM INSIDE such a turn while the exit that consumes it may be reaped
outside one — resolving through the ambient home would write the request into a
profile directory the reaper never opens. That is the exact failure that
resolver was extracted to close, so this module never re-derives it.

Divergence from the dispatch store, stated so it is not read as an oversight
----------------------------------------------------------------------------
``dispatch_store`` is a SQLite table with a claim protocol and an attempt cap,
because several consumers in several processes race to deliver one dispatch.
This lane has neither race: the row is written and read by the SAME serve
process, and the actual DELIVERY (with its idempotency, attempt cap and
telemetry) is performed downstream by the existing background-completion drain
on the existing completion queue. So the plan's own words are followed — "a
drain file" — with the store's states and its never-raises posture kept.

What this file deliberately does NOT do
---------------------------------------
There is no timer. A process that never exits delivers nothing, and that is the
design: the mission-chat turn wall/budget system is the guard on a runaway, and
an agent that would rather block is bounded by the Stage 3a wait ceiling
(``process_registry.MISSION_CHAT_WAIT_MAX_SECONDS``). A timer here would be a
second, quieter deadline competing with the one that already exists.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_NOTIFY_ROWS",
    "NOTIFY_STORE_FILENAME",
    "SETTLED_TTL_SECONDS",
    "STATE_DROPPED",
    "STATE_FIRED",
    "STATE_PENDING",
    "notify_requests",
    "notify_store_path",
    "pending_notify_request",
    "record_notify_request",
    "reset_cache",
    "settle_notify_request",
]

#: Sibling of ``processes.json`` in the background-work home.
NOTIFY_STORE_FILENAME = "process_notify_requests.json"

#: The row is waiting for its process to exit.
STATE_PENDING = "pending"
#: The exit happened and the completion was published to the delivery queue.
STATE_FIRED = "fired"
#: The exit happened and the request could not be honored — today only because
#: the requesting persona instance no longer exists. Terminal, and logged.
STATE_DROPPED = "dropped"

#: Bound on the file. Rows are tiny, but a store that grows without a ceiling is
#: a store that eventually costs a turn. Settled rows are evicted first, oldest
#: first; pending rows are only evicted once nothing settled is left to drop.
MAX_NOTIFY_ROWS = 64

#: How long a settled row is kept for forensics before pruning. Long enough to
#: explain a delivery an operator asks about the same session; short enough that
#: the file stays a working set rather than a ledger.
SETTLED_TTL_SECONDS = 6 * 3600

_LOCK = threading.Lock()
#: ``(stat signature, payload)``. The reaper reads this file on EVERY process
#: exit, so the read is memoized — but keyed on the file's own mtime/size, so a
#: write from another process invalidates it rather than being missed.
_CACHE: tuple[tuple[float, int], dict[str, Any]] | None = None


def notify_store_path() -> Path:
    """The drain file, resolved at call time (never bound at import).

    Same reasoning as ``process_registry.checkpoint_path``: the home a persona
    turn resolves is not the home the process that reaps its exit resolves,
    unless both go through the one background-work authority.
    """

    from hermes_constants import get_hermes_background_work_home

    return Path(get_hermes_background_work_home()) / NOTIFY_STORE_FILENAME


def reset_cache() -> None:
    """Drop the memoized payload. Test seam; production reads re-stat instead."""

    global _CACHE
    with _LOCK:
        _CACHE = None


def _stat_signature(path: Path) -> tuple[float, int] | None:
    try:
        stat = path.stat()
    except Exception:
        return None
    return (stat.st_mtime, stat.st_size)


def _load_locked() -> dict[str, Any]:
    """The file's ``{session_id: row}`` map. Never raises; ``{}`` on any fault.

    A corrupt or unreadable drain file must not be able to kill a process
    reaper, so it reads as "no requests" — the same posture every accounting
    surface on the delivery lane holds. The consequence is bounded and honest:
    a delivery is missed, an agent falls back to the wait ceiling, and nothing
    crashes mid-exit.
    """

    global _CACHE
    path = notify_store_path()
    signature = _stat_signature(path)
    if signature is None:
        _CACHE = None
        return {}
    cached = _CACHE
    if cached is not None and cached[0] == signature:
        return cached[1]
    try:
        import json

        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("process notify store unreadable at %s", path, exc_info=True)
        _CACHE = None
        return {}
    requests = raw.get("requests") if isinstance(raw, dict) else None
    payload = {
        str(key): dict(value)
        for key, value in (requests or {}).items()
        if isinstance(value, dict)
    }
    _CACHE = (signature, payload)
    return payload


def _write_locked(payload: dict[str, Any]) -> bool:
    """Atomically persist *payload*. Returns whether the write landed."""

    global _CACHE
    from utils import atomic_json_write

    path = notify_store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(
            path, {"version": 1, "requests": payload}, indent=None, sort_keys=True
        )
    except Exception:
        logger.debug("process notify store write failed at %s", path, exc_info=True)
        _CACHE = None
        return False
    signature = _stat_signature(path)
    _CACHE = (signature, payload) if signature is not None else None
    return True


def _pruned(payload: dict[str, Any]) -> dict[str, Any]:
    """Evict settled rows past their TTL, then trim to :data:`MAX_NOTIFY_ROWS`."""

    now = time.time()
    rows = dict(payload)
    for key, row in list(rows.items()):
        if row.get("state") in {STATE_FIRED, STATE_DROPPED}:
            settled_at = float(row.get("settled_at") or 0.0)
            if settled_at and now - settled_at > SETTLED_TTL_SECONDS:
                rows.pop(key, None)
    if len(rows) <= MAX_NOTIFY_ROWS:
        return rows
    # Settled first, oldest first — a pending row is a promise nobody has kept
    # yet and is the last thing to throw away.
    order = sorted(
        rows.items(),
        key=lambda item: (
            item[1].get("state") == STATE_PENDING,
            float(item[1].get("requested_at") or 0.0),
        ),
    )
    for key, row in order:
        if len(rows) <= MAX_NOTIFY_ROWS:
            break
        if row.get("state") == STATE_PENDING:
            # A promise nobody has kept yet, thrown away because the store is
            # full. Vanishingly rare (the registry tracks at most MAX_PROCESSES
            # sessions at all), and it must never be the SILENT kind of rare:
            # the agent that armed this will end its turn and wait for a receipt
            # that is no longer coming.
            logger.warning(
                "process notify request for %s evicted un-delivered: the drain file"
                " is at its %d-row ceiling",
                key,
                MAX_NOTIFY_ROWS,
            )
        rows.pop(key, None)
    return rows


def record_notify_request(
    *,
    session_id: str,
    chat_session_id: str,
    persona_instance_id: str,
    persona_id: str = "",
    turn_id: str = "",
    command: str = "",
) -> tuple[dict[str, Any], bool]:
    """Record one delivery request. Returns ``(row, created)``.

    IDEMPOTENT by session id, which is the whole of the duplicate-notify edge
    case: a second call on the same process returns the FIRST row untouched
    (``created=False``), so two notifies can never become two deliveries. The
    row is not re-armed either — a settled row stays settled, because the
    delivery it names already happened.
    """

    session_id = str(session_id or "")
    if not session_id:
        return {}, False
    with _LOCK:
        payload = dict(_load_locked())
        existing = payload.get(session_id)
        if isinstance(existing, dict) and existing:
            return dict(existing), False
        row = {
            "session_id": session_id,
            "chat_session_id": str(chat_session_id or ""),
            "persona_instance_id": str(persona_instance_id or ""),
            "persona_id": str(persona_id or ""),
            "turn_id": str(turn_id or ""),
            "command": str(command or "")[:500],
            "requested_at": time.time(),
            "state": STATE_PENDING,
            "settled_at": 0.0,
            "detail": "",
        }
        payload[session_id] = row
        _write_locked(_pruned(payload))
        return dict(row), True


def pending_notify_request(session_id: str) -> dict[str, Any] | None:
    """The PENDING row for a process, or None. Never raises."""

    session_id = str(session_id or "")
    if not session_id:
        return None
    with _LOCK:
        row = _load_locked().get(session_id)
    if not isinstance(row, dict) or row.get("state") != STATE_PENDING:
        return None
    return dict(row)


def notify_requests() -> list[dict[str, Any]]:
    """Every row, oldest request first. Never raises."""

    with _LOCK:
        rows = [dict(row) for row in _load_locked().values()]
    return sorted(rows, key=lambda row: float(row.get("requested_at") or 0.0))


def settle_notify_request(session_id: str, *, state: str, detail: str = "") -> bool:
    """Move a row to :data:`STATE_FIRED` or :data:`STATE_DROPPED`. Never raises."""

    session_id = str(session_id or "")
    if not session_id or state not in {STATE_FIRED, STATE_DROPPED}:
        return False
    with _LOCK:
        payload = dict(_load_locked())
        row = payload.get(session_id)
        if not isinstance(row, dict):
            return False
        row = dict(row)
        row["state"] = state
        row["settled_at"] = time.time()
        row["detail"] = str(detail or "")[:240]
        payload[session_id] = row
        return _write_locked(_pruned(payload))
