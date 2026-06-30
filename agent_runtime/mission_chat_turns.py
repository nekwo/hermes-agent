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


def persist_mission_chat_turn(
    *,
    session_id: str | None,
    client_message_id: str | None,
    turn_id: str | None,
    elements: list[dict[str, Any]] | None,
) -> None:
    session_key = safe_assignment_text(session_id, limit=240)
    message_key = safe_assignment_text(client_message_id, limit=240)
    if not session_key or not message_key or not elements:
        return
    safe_elements = _safe_elements(elements)
    if not safe_elements:
        return
    store = _read_store()
    store.setdefault(session_key, {})[message_key] = {
        "schema_version": 1,
        "turn_id": safe_assignment_token(turn_id) or safe_assignment_token(message_key),
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
