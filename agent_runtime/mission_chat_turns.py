from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import paths
from .persona_assignments import safe_assignment_text, safe_assignment_token

_STORE_NAME = "mission_chat_turns.json"
_MAX_ELEMENTS = 80
_MAX_TEXT = 20000
_SENSITIVE_FILE_MARKERS = ("private_token", "secret_token", "api_key", "apikey", "credential")
_VALID_TURN_STATES = {"running", "completed", "failed", "interrupted"}


def persist_mission_chat_turn(
    *,
    session_id: str | None,
    client_message_id: str | None,
    turn_id: str | None,
    elements: list[dict[str, Any]] | None,
    state: str | None = None,
) -> None:
    session_key = safe_assignment_text(session_id, limit=240)
    message_key = safe_assignment_text(client_message_id, limit=240)
    if not session_key or not message_key:
        return
    requested_state = _safe_turn_state(state) if state is not None else None
    if state is not None and requested_state is None:
        return
    if state is None and not elements:
        return
    safe_elements = _safe_elements(elements)
    if state is None and not safe_elements:
        return
    store = _read_store()
    existing = (store.get(session_key) or {}).get(message_key)
    existing_state = _record_state(existing)
    next_state = requested_state or existing_state or "running"
    store.setdefault(session_key, {})[message_key] = {
        "schema_version": 1,
        "turn_id": safe_assignment_token(turn_id) or safe_assignment_token(message_key),
        "state": next_state,
        "updated_at": _utc_now_iso(),
        "elements": safe_elements,
    }
    _write_store(store)


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
    store = _read_store()
    raw_session = store.get(session_key)
    if not isinstance(raw_session, dict):
        return []
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
    if flipped:
        _write_store(store)
    return flipped


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
