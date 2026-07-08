from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from . import paths
from .persona_assignments import safe_assignment_text, safe_assignment_token

_STORE_NAME = "mission_chat_turns.json"
_LOCK_NAME = "mission_chat_turns.lock"
_LOCK_TIMEOUT_SECONDS = 2.0
_LOCK_POLL_SECONDS = 0.01
_MAX_ELEMENTS = 80
_MAX_TEXT = 20000
_SENSITIVE_FILE_MARKERS = ("private_token", "secret_token", "api_key", "apikey", "credential")
_VALID_TURN_STATES = {"running", "completed", "failed", "interrupted"}

_T = TypeVar("_T")


class MissionChatTurnPersistOutcome(str, Enum):
    """Typed result of a turn-record persist. No write is ever lost silently:
    every skipped or rejected persist names its reason."""

    PERSISTED = "persisted"
    SKIPPED_NO_KEYS = "skipped_no_keys"
    SKIPPED_EMPTY_LEGACY = "skipped_empty_legacy"
    REJECTED_INVALID_STATE = "rejected_invalid_state"
    REJECTED_STALE_TRANSITION = "rejected_stale_transition"
    SKIPPED_LOCK_TIMEOUT = "skipped_lock_timeout"


def next_turn_state(
    current: str | None,
    requested: str | None,
    *,
    write_ahead: bool = False,
) -> str | None:
    """Single transition authority for turn-record states.

    Returns the state to store, or ``None`` when the write must be rejected.
    Rules:
    - ``requested=None`` (legacy elements-only call) preserves the current
      state; a brand-new record defaults to ``running``.
    - Explicit terminal states (``completed``/``failed``) and the repair state
      (``interrupted``) always win — a completed reply recorded after a
      repair flip must not be lost.
    - ``running`` with ``write_ahead=True`` is a fresh turn start (same-client
      retry after an interrupted/failed turn) and always wins.
    - ``running`` with ``write_ahead=False`` is an incremental on_update flush
      and must NOT resurrect a settled record: a late flush from a turn that
      another process already repaired to ``interrupted`` (or that completed)
      is stale and is rejected.
    """

    if requested is None:
        return current or "running"
    if requested not in _VALID_TURN_STATES:
        return None
    if requested != "running":
        return requested
    if write_ahead or current is None or current == "running":
        return "running"
    return None


def persist_mission_chat_turn(
    *,
    session_id: str | None,
    client_message_id: str | None,
    turn_id: str | None,
    elements: list[dict[str, Any]] | None,
    state: str | None = None,
    write_ahead: bool = False,
) -> MissionChatTurnPersistOutcome:
    session_key = safe_assignment_text(session_id, limit=240)
    message_key = safe_assignment_text(client_message_id, limit=240)
    if not session_key or not message_key:
        return MissionChatTurnPersistOutcome.SKIPPED_NO_KEYS
    requested_state = _safe_turn_state(state) if state is not None else None
    if state is not None and requested_state is None:
        return MissionChatTurnPersistOutcome.REJECTED_INVALID_STATE
    if state is None and not elements:
        return MissionChatTurnPersistOutcome.SKIPPED_EMPTY_LEGACY
    safe_elements = _safe_elements(elements)
    if state is None and not safe_elements:
        return MissionChatTurnPersistOutcome.SKIPPED_EMPTY_LEGACY

    def _mutate(store: dict[str, Any]) -> tuple[bool, MissionChatTurnPersistOutcome]:
        existing = (store.get(session_key) or {}).get(message_key)
        resolved_state = next_turn_state(
            _record_state(existing),
            requested_state,
            write_ahead=write_ahead,
        )
        if resolved_state is None:
            return False, MissionChatTurnPersistOutcome.REJECTED_STALE_TRANSITION
        store.setdefault(session_key, {})[message_key] = {
            "schema_version": 1,
            "turn_id": safe_assignment_token(turn_id) or safe_assignment_token(message_key),
            "state": resolved_state,
            "updated_at": _utc_now_iso(),
            "elements": safe_elements,
        }
        return True, MissionChatTurnPersistOutcome.PERSISTED

    return _mutate_store(
        _mutate,
        timeout_result=MissionChatTurnPersistOutcome.SKIPPED_LOCK_TIMEOUT,
    )


def mission_chat_turn_elements(
    *,
    session_id: str | None,
    client_message_id: str | None,
) -> list[dict[str, Any]]:
    session_key = safe_assignment_text(session_id, limit=240)
    message_key = safe_assignment_text(client_message_id, limit=240)
    if not session_key or not message_key:
        return []
    record = (_read_store().get(session_key) or {}).get(message_key)
    if not isinstance(record, dict):
        return []
    return _safe_elements(record.get("elements"))


def mission_chat_turn_record(
    *,
    session_id: str | None,
    client_message_id: str | None,
) -> dict[str, Any] | None:
    session_key = safe_assignment_text(session_id, limit=240)
    message_key = safe_assignment_text(client_message_id, limit=240)
    if not session_key or not message_key:
        return None
    record = (_read_store().get(session_key) or {}).get(message_key)
    if not isinstance(record, dict):
        return None
    return _safe_record(record, client_message_id=message_key)


def mission_chat_turn_records(
    *,
    session_id: str | None,
) -> list[dict[str, Any]]:
    session_key = safe_assignment_text(session_id, limit=240)
    if not session_key:
        return []
    raw_session = _read_store().get(session_key)
    if not isinstance(raw_session, dict):
        return []
    records: list[dict[str, Any]] = []
    for message_key, record in raw_session.items():
        if not isinstance(record, dict):
            continue
        safe_key = safe_assignment_text(message_key, limit=240)
        if not safe_key:
            continue
        safe_record = _safe_record(record, client_message_id=safe_key)
        if safe_record is not None:
            records.append(safe_record)
    return sorted(
        records,
        key=lambda item: (
            str(item.get("updated_at") or ""),
            str(item.get("client_message_id") or ""),
        ),
    )


def mark_stale_running_turns_interrupted(
    *,
    session_id: str | None,
    active_client_message_id: str | None,
) -> list[str]:
    session_key = safe_assignment_text(session_id, limit=240)
    active_key = safe_assignment_text(active_client_message_id, limit=240)
    if not session_key:
        return []

    def _mutate(store: dict[str, Any]) -> tuple[bool, list[str]]:
        raw_session = store.get(session_key)
        if not isinstance(raw_session, dict):
            return False, []
        flipped: list[str] = []
        now_iso = _utc_now_iso()
        for message_key, record in raw_session.items():
            safe_key = safe_assignment_text(message_key, limit=240)
            if not safe_key or safe_key == active_key or not isinstance(record, dict):
                continue
            if _record_state(record) != "running":
                continue
            record["state"] = "interrupted"
            record["updated_at"] = now_iso
            flipped.append(safe_key)
        return bool(flipped), flipped

    # Lock timeout returns [] — this repair is opportunistic by design
    # (repair-on-next-write); the next send in the session retries it.
    return _mutate_store(_mutate, timeout_result=[])


def _mutate_store(
    mutator: Callable[[dict[str, Any]], tuple[bool, _T]],
    *,
    timeout_result: _T,
) -> _T:
    """Single write chokepoint for the turn store.

    Holds an exclusive cross-process file lock for the whole
    read-modify-write window so concurrent CLI turns can never lose each
    other's records. On lock timeout the mutation is skipped and
    ``timeout_result`` is returned — a chat turn must never hang on a stuck
    lock; the skip is surfaced through the typed persist outcome.
    """

    with _store_lock() as acquired:
        if not acquired:
            return timeout_result
        store = _read_store()
        changed, result = mutator(store)
        if changed:
            _write_store(store)
        return result


@contextmanager
def _store_lock(
    timeout_seconds: float | None = None,
) -> Iterator[bool]:
    if timeout_seconds is None:
        timeout_seconds = _LOCK_TIMEOUT_SECONDS
    lock_path = paths.store_root() / _LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    acquired = False
    try:
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        while True:
            try:
                _lock_fd_exclusive_nonblocking(fd)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(_LOCK_POLL_SECONDS)
        yield acquired
    finally:
        if acquired:
            try:
                _unlock_fd(fd)
            except OSError:
                pass
        os.close(fd)


def _lock_fd_exclusive_nonblocking(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _store_path() -> Path:
    return paths.store_root() / _STORE_NAME


def _read_store() -> dict[str, Any]:
    path = _store_path()
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_store(data: dict[str, Any]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _safe_record(
    record: dict[str, Any],
    *,
    client_message_id: str,
) -> dict[str, Any] | None:
    turn_id = safe_assignment_token(record.get("turn_id")) or safe_assignment_token(client_message_id)
    if not turn_id:
        return None
    return {
        "client_message_id": client_message_id,
        "turn_id": turn_id,
        "state": _record_state(record) or "completed",
        "updated_at": safe_assignment_text(record.get("updated_at"), limit=80),
        "elements": _safe_elements(record.get("elements")),
    }


def _safe_turn_state(value: Any) -> str | None:
    state = safe_assignment_token(value)
    return state if state in _VALID_TURN_STATES else None


def _record_state(record: Any) -> str | None:
    if not isinstance(record, dict):
        return None
    state = _safe_turn_state(record.get("state"))
    return state or "completed"


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _safe_elements(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    elements: list[dict[str, Any]] = []
    for raw in value[:_MAX_ELEMENTS]:
        if not isinstance(raw, dict):
            continue
        kind = safe_assignment_token(raw.get("kind"))
        element_id = safe_assignment_text(raw.get("id"), limit=240)
        turn_id = safe_assignment_token(raw.get("turn_id"))
        try:
            seq = int(raw.get("seq"))
        except Exception:
            continue
        if kind not in {"segment", "tool"} or not element_id or not turn_id:
            continue
        base: dict[str, Any] = {
            "kind": kind,
            "id": element_id,
            "turn_id": turn_id,
            "seq": seq,
            "state": safe_assignment_token(raw.get("state")) or "settled",
        }
        if kind == "segment":
            base.update(
                {
                    "seg_type": safe_assignment_token(raw.get("seg_type")) or "answer",
                    "text": safe_assignment_text(raw.get("text"), limit=_MAX_TEXT) or "",
                    "ttft_ms": _safe_int(raw.get("ttft_ms")),
                    "duration_ms": _safe_int(raw.get("duration_ms")),
                    "redacted": bool(raw.get("redacted")),
                }
            )
        else:
            files = raw.get("files")
            safe_files = [_safe_file_label(item) for item in files[:20]] if isinstance(files, list) else []
            base.update(
                {
                    "name": safe_assignment_token(raw.get("name")) or "tool",
                    "args": safe_assignment_text(raw.get("args"), limit=800),
                    "command": safe_assignment_text(raw.get("command"), limit=1000),
                    "status": safe_assignment_token(raw.get("status")) or None,
                    "summary": safe_assignment_text(raw.get("summary"), limit=1200),
                    "detail": safe_assignment_text(raw.get("detail"), limit=1200),
                    "output": safe_assignment_text(raw.get("output"), limit=_MAX_TEXT),
                    "exit_code": _safe_exit_code(raw.get("exit_code")),
                    "duration_ms": _safe_int(raw.get("duration_ms")),
                    "files": [item for item in safe_files if item],
                    "redacted": bool(raw.get("redacted")),
                }
            )
        elements.append(base)
    return sorted(elements, key=lambda item: (int(item.get("seq") or 0), str(item.get("id") or "")))


def _safe_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except Exception:
        return None
    return parsed if parsed >= 0 else None


def _safe_exit_code(value: Any) -> int | None:
    # Exit codes can be negative (signal terminations), so unlike _safe_int we
    # keep the sign; just bound it to a sane range.
    try:
        parsed = int(value)
    except Exception:
        return None
    return parsed if -256 <= parsed <= 256 else None


def _safe_file_label(value: Any) -> str | None:
    text = safe_assignment_text(value, limit=240)
    if not text:
        return None
    lowered = text.lower()
    if any(marker in lowered for marker in _SENSITIVE_FILE_MARKERS):
        return None
    return text
