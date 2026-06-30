from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from utils import atomic_json_write

from . import paths

MAX_TOOL_TURN_HISTORY = 25


def persist_tool_turn_actual(
    *,
    persona_id: str,
    session_id: str | None,
    task_id: str | None = None,
    goal_id: str | None = None,
    turn_id: str | None = None,
    model_input: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(model_input, dict):
        return None
    tool_schema = model_input.get("tool_schema")
    if not isinstance(tool_schema, dict):
        return None
    persona = _safe_token(persona_id)
    if not persona:
        return None
    key = _history_key(persona_id=persona, session_id=session_id)
    path = _history_path(key)
    payload = _read_history(path)
    entry = {
        "schema_version": 1,
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "persona_id": persona,
        "session_id": _safe_text(session_id),
        "task_id": _safe_token(task_id),
        "goal_id": _safe_token(goal_id),
        "turn_id": _safe_token(turn_id),
        "enabled_toolsets": list(model_input.get("enabled_toolsets") or []),
        "disabled_toolsets": list(model_input.get("disabled_toolsets") or []),
        "final_model_tools": list(tool_schema.get("final_model_tools") or []),
        "tool_count": int(tool_schema.get("tool_count") or 0),
        "tool_schema": _safe_tool_schema(tool_schema),
    }
    history = [entry, *list(payload.get("history") or [])]
    payload = {
        "schema_version": 1,
        "persona_id": persona,
        "session_id": _safe_text(session_id),
        "last_actual": entry,
        "history": history[:MAX_TOOL_TURN_HISTORY],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, payload, indent=2, sort_keys=True)
    return entry


def load_tool_turn_history(*, persona_id: str, session_id: str | None, limit: int = 10) -> dict[str, Any]:
    persona = _safe_token(persona_id)
    if not persona:
        return {"last_actual": None, "history": []}
    candidates = []
    if session_id:
        candidates.append(_history_path(_history_key(persona_id=persona, session_id=session_id)))
    root = _history_root()
    if root.exists():
        candidates.extend(sorted(root.glob(f"{persona}__*.json"), key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True))
    seen: set[str] = set()
    history: list[dict[str, Any]] = []
    last_actual: dict[str, Any] | None = None
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        payload = _read_history(path)
        if last_actual is None and isinstance(payload.get("last_actual"), dict):
            last_actual = payload["last_actual"]
        for item in list(payload.get("history") or []):
            if isinstance(item, dict):
                history.append(item)
            if len(history) >= limit:
                break
        if len(history) >= limit:
            break
    return {"last_actual": last_actual, "history": history[:limit]}


def _history_root():
    return paths.store_root() / "tool_turn_context"


def _history_path(key: str):
    return _history_root() / f"{key}.json"


def _history_key(*, persona_id: str, session_id: str | None) -> str:
    return f"{_safe_token(persona_id) or 'persona'}__{_safe_token(session_id) or 'no_session'}"


def _read_history(path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _safe_tool_schema(value: dict[str, Any]) -> dict[str, Any]:
    schema = dict(value)
    schema.pop("blocked_tool_names", None)
    return schema


def _safe_token(value: object) -> str | None:
    text = "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in str(value or "").strip())
    return text.strip("._")[:120] or None


def _safe_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text[:240] if text else None
