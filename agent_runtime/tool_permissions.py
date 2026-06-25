from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from utils import atomic_json_write

from . import paths
from .models import AgentPersona
from .tool_visibility import ToolVisibilityOptions, permission_state_for_persona

READ_ONLY_BLOCKS = frozenset({"apply_patch", "edit_file", "file.edit", "file.write", "patch", "terminal", "write_file"})
PERMISSION_MODE_PROFILE_DEFAULT = "profile_default"
PERMISSION_MODE_READ_ONLY = "read_only"
PERMISSION_MODE_UNBOUNDED = "unbounded"
SUPPORTED_PERMISSION_MODES = frozenset(
    {
        PERMISSION_MODE_PROFILE_DEFAULT,
        PERMISSION_MODE_READ_ONLY,
        PERMISSION_MODE_UNBOUNDED,
    }
)


@dataclass(slots=True)
class ChatToolPermission:
    persona_id: str
    session_id: str
    mode: str = PERMISSION_MODE_PROFILE_DEFAULT
    reason: str = ""
    source: str = "operator"
    updated_at: str = ""


class ChatToolPermissionStore:
    def __init__(self, path=None):
        self.path = path or paths.store_root() / "tool_permissions.json"

    def get(self, *, persona_id: str, session_id: str | None) -> ChatToolPermission | None:
        if not session_id:
            return None
        raw = self._read()
        item = raw.get(_key(persona_id, session_id))
        if not isinstance(item, dict):
            return None
        mode = str(item.get("mode") or PERMISSION_MODE_PROFILE_DEFAULT)
        if mode not in SUPPORTED_PERMISSION_MODES:
            mode = PERMISSION_MODE_PROFILE_DEFAULT
        return ChatToolPermission(
            persona_id=str(item.get("persona_id") or persona_id),
            session_id=str(item.get("session_id") or session_id),
            mode=mode,
            reason=str(item.get("reason") or ""),
            source=str(item.get("source") or "operator"),
            updated_at=str(item.get("updated_at") or ""),
        )

    def set(self, *, persona_id: str, session_id: str, mode: str, reason: str, source: str = "operator") -> ChatToolPermission:
        resolved_mode = mode if mode in SUPPORTED_PERMISSION_MODES else PERMISSION_MODE_PROFILE_DEFAULT
        record = ChatToolPermission(
            persona_id=persona_id,
            session_id=session_id,
            mode=resolved_mode,
            reason=reason,
            source=source,
            updated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        raw = self._read()
        raw[_key(persona_id, session_id)] = {
            "persona_id": record.persona_id,
            "session_id": record.session_id,
            "mode": record.mode,
            "reason": record.reason,
            "source": record.source,
            "updated_at": record.updated_at,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(self.path, raw, indent=2, sort_keys=True)
        return record

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}


def permission_options_for_chat(
    persona: AgentPersona,
    *,
    session_id: str | None,
    task_id: str | None = None,
    goal_id: str | None = None,
    runtime_root: str | None = None,
    store: ChatToolPermissionStore | None = None,
) -> ToolVisibilityOptions:
    record = (store or ChatToolPermissionStore()).get(persona_id=persona.id, session_id=session_id)
    mode = record.mode if record is not None else PERMISSION_MODE_PROFILE_DEFAULT
    return ToolVisibilityOptions(
        permission_mode=mode,
        permission_source=record.source if record is not None else "persona_role_policy",
        session_id=session_id,
        task_id=task_id,
        goal_id=goal_id,
        runtime_root=runtime_root,
        blocked_tool_names=extra_blocked_tools_for_permission_mode(mode),
    )


def permission_state_for_chat(persona: AgentPersona, *, session_id: str | None) -> dict[str, Any]:
    return permission_state_for_persona(persona, permission_options_for_chat(persona, session_id=session_id))


def extra_blocked_tools_for_permission_mode(mode: str) -> list[str]:
    if mode == PERMISSION_MODE_READ_ONLY:
        return sorted(READ_ONLY_BLOCKS)
    return []


def permission_mode_is_unbounded(mode: str | None) -> bool:
    return str(mode or "").strip().lower() == PERMISSION_MODE_UNBOUNDED


def _key(persona_id: str, session_id: str) -> str:
    return f"{persona_id.strip()}::{session_id.strip()}"
