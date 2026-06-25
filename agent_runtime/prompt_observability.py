from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable

from hermes_cli.profiles import get_profile_dir
from utils import atomic_json_write

from . import paths
from .persona_assignments import safe_assignment_text, safe_assignment_token
from .serde import to_jsonable


SAFE_PREVIEW_LIMIT = 1200
DEFAULT_CHAT_HISTORY_LIMIT = 8


def mission_chat_prompt_observability(
    *,
    persona: Any,
    persona_instance_id: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    goal_id: str | None = None,
    turn_id: str | None = None,
    surface_prompt: str | None = "",
    limiting_wrapper_active: bool = False,
    session_db: Any | None = None,
    current_message: str | None = None,
    final_model_input: dict[str, Any] | None = None,
    model_selection: dict[str, Any] | None = None,
    prompt_mode: str = "normal_hermes_profile_chat",
) -> dict[str, Any]:
    """Build redaction-safe prompt/context observability for Mission Control.

    This intentionally reports prompt layers and file provenance, not raw secret
    config values. Context files get hashes and short previews only when their
    filenames are known prompt/memory files.
    """

    persona_id = safe_assignment_token(getattr(persona, "id", None)) or "unknown"
    profile = safe_assignment_token(getattr(persona, "hermes_profile", None)) or persona_id
    context_id = "ctx_" + hashlib.sha256(
        "|".join(
            str(item or "")
            for item in (
                persona_id,
                persona_instance_id,
                session_id,
                task_id,
                goal_id,
                turn_id,
                current_message,
            )
        ).encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    surface = safe_assignment_text(surface_prompt, limit=4000) or ""
    history = _chat_history_context(session_db=session_db, session_id=session_id)
    return {
        "context_id": context_id,
        "prompt_mode": prompt_mode,
        "persona_id": persona_id,
        "persona_instance_id": safe_assignment_token(persona_instance_id),
        "profile": profile,
        "display_name": safe_assignment_text(getattr(persona, "display_name", None), limit=120) or persona_id,
        "role": safe_assignment_token(getattr(persona, "role", None)) or "agent",
        "session_id": safe_assignment_text(session_id, limit=200),
        "task_id": safe_assignment_token(task_id),
        "goal_id": safe_assignment_token(goal_id),
        "turn_id": safe_assignment_token(turn_id),
        "surface_prompt": surface,
        "surface_prompt_is_blank": surface == "",
        "limiting_wrapper_active": bool(limiting_wrapper_active),
        "prompt_layers": [
            {
                "name": "Hermes core prompt",
                "kind": "system_core",
                "status": "loaded_by_profile_runner",
                "summary": "Normal Hermes profile chat system stack.",
            },
            {
                "name": "Mission Control surface prompt",
                "kind": "surface",
                "status": "blank" if surface == "" else "configured",
                "summary": "Blank by default; no limiting wrapper is applied.",
                "preview": surface[:SAFE_PREVIEW_LIMIT],
            },
            {
                "name": "Profile SOUL and memory",
                "kind": "profile_context",
                "status": "loaded",
                "summary": "Loaded through normal Hermes profile context files and memory.",
            },
            {
                "name": "Chat history context",
                "kind": "conversation",
                "status": "loaded" if history else "empty",
                "summary": f"{len(history)} prior redaction-safe chat message(s) supplied before this turn.",
            },
        ],
        "context_files": _profile_context_files(profile),
        "chat_history_context": history,
        "retrieval_context": [],
        "final_model_input": _safe_final_model_input(final_model_input),
        "model_selection": _safe_model_selection(model_selection),
        "prompt_flags": {
            "skip_context_files": False,
            "skip_memory": False,
            "load_soul_identity": True,
            "surface_prompt_blank": surface == "",
            "limiting_wrapper_active": bool(limiting_wrapper_active),
        },
        "redaction": {
            "status": "safe",
            "notes": [
                "Prompt observability shows file provenance, hashes, and short redaction-safe previews.",
                "Secrets and raw provider credentials are not included.",
            ],
        },
    }


def _safe_model_selection(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "default_provider",
        "default_model",
        "chat_provider",
        "chat_model",
        "effective_provider",
        "effective_model",
        "model_is_default",
        "scope",
    }
    result: dict[str, Any] = {}
    for key in allowed:
        item = value.get(key)
        if isinstance(item, bool):
            result[key] = item
        elif isinstance(item, str) and item.strip():
            result[key] = safe_assignment_text(item, limit=220)
        elif item is None and key in {"chat_provider", "chat_model"}:
            result[key] = None
    return result


def snapshot_prompt_observability(
    *,
    personas: Iterable[Any],
    persona_instances: Iterable[Any],
    session_db: Any | None = None,
) -> dict[str, Any]:
    contexts: list[dict[str, Any]] = []
    by_persona = {
        safe_assignment_token(getattr(persona, "id", None)): persona
        for persona in personas
        if safe_assignment_token(getattr(persona, "id", None))
    }
    for instance in persona_instances:
        persona_id = safe_assignment_token(getattr(instance, "persona_id", None))
        persona = by_persona.get(persona_id)
        if persona is None:
            continue
        contexts.append(
            mission_chat_prompt_observability(
                persona=persona,
                persona_instance_id=getattr(instance, "id", None),
                session_id=getattr(instance, "session_id", None),
                task_id=getattr(instance, "current_task_id", None),
                goal_id=getattr(instance, "goal_id", None),
                surface_prompt="",
                limiting_wrapper_active=False,
                session_db=session_db,
            )
        )
    if not contexts:
        for persona in personas:
            persona_id = safe_assignment_token(getattr(persona, "id", None))
            if persona_id == "neko_supervisor":
                contexts.append(
                    mission_chat_prompt_observability(
                        persona=persona,
                        surface_prompt="",
                        limiting_wrapper_active=False,
                        session_db=session_db,
                    )
                )
                break
    return {
        "schema_version": 1,
        "default_flow": {
            "id": "neko_two_dev_default",
            "lead": "neko_supervisor",
            "dev_specialists": ["backend_dev", "dev"],
            "qa_default": False,
        },
        "surface_prompt_default": "",
        "chat_contexts": _merge_latest_contexts(contexts),
    }


def persist_prompt_observability_context(context: dict[str, Any]) -> None:
    context_id = safe_assignment_token(context.get("context_id"))
    if not context_id:
        return
    root = paths.prompt_observability_dir()
    root.mkdir(parents=True, exist_ok=True)
    atomic_json_write(root / f"{context_id}.json", to_jsonable(context), indent=2, sort_keys=True)


def load_latest_prompt_observability_contexts() -> list[dict[str, Any]]:
    root = paths.prompt_observability_dir()
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            import json

            data = json.loads(raw)
        except Exception:
            continue
        if isinstance(data, dict) and safe_assignment_token(data.get("context_id")):
            rows.append(data)
        if len(rows) >= 50:
            break
    return rows


def _merge_latest_contexts(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def key_for(item: dict[str, Any]) -> tuple[str, str, str]:
        return (
            safe_assignment_token(item.get("persona_instance_id")) or "",
            safe_assignment_text(item.get("session_id"), limit=200) or "",
            safe_assignment_token(item.get("persona_id")) or "",
        )

    for item in load_latest_prompt_observability_contexts():
        key = key_for(item)
        if key in seen:
            continue
        merged.append(item)
        seen.add(key)
    for item in contexts:
        key = key_for(item)
        if key in seen:
            continue
        merged.append(item)
        seen.add(key)
    return merged


def _profile_context_files(profile: str) -> list[dict[str, Any]]:
    try:
        profile_dir = get_profile_dir(profile)
    except Exception:
        profile_dir = None
    files: list[dict[str, Any]] = []
    if profile_dir is None:
        return files
    candidates = [
        profile_dir / "SOUL.md",
        profile_dir / "memories" / "MEMORY.md",
        profile_dir / "memories" / "USER.md",
        profile_dir / "AGENTS.md",
        profile_dir / ".skills_prompt_snapshot.json",
        profile_dir / "config.yaml",
    ]
    for path in candidates:
        files.append(_context_file_summary(path, included=path.exists()))
    return files


def _context_file_summary(path: Path, *, included: bool) -> dict[str, Any]:
    data: dict[str, Any] = {
        "path": str(path),
        "name": path.name,
        "kind": _file_kind(path),
        "included": bool(included),
        "status": "loaded" if included else "missing_or_not_configured",
    }
    if not included:
        return data
    try:
        raw = path.read_bytes()
    except OSError as exc:
        data["status"] = "unreadable"
        data["error"] = type(exc).__name__
        return data
    data["sha256"] = hashlib.sha256(raw).hexdigest().upper()
    data["bytes"] = len(raw)
    if path.name in {"SOUL.md", "MEMORY.md", "USER.md", "AGENTS.md"}:
        text = raw.decode("utf-8", errors="replace")
        data["preview"] = _safe_preview(text)
    elif path.name == ".skills_prompt_snapshot.json":
        data["preview"] = "Skills prompt snapshot present; body withheld from observability preview."
    elif path.name == "config.yaml":
        data["preview"] = "Profile config present; raw values withheld from observability preview."
    return data


def _file_kind(path: Path) -> str:
    name = path.name
    if name == "SOUL.md":
        return "soul"
    if name == "MEMORY.md":
        return "memory"
    if name == "USER.md":
        return "user_memory"
    if name == "AGENTS.md":
        return "project_context"
    if name == ".skills_prompt_snapshot.json":
        return "skills"
    if name == "config.yaml":
        return "profile_config"
    return "context_file"


def _safe_final_model_input(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    messages = value.get("messages") if isinstance(value.get("messages"), list) else []
    safe_messages = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = safe_assignment_text(message.get("content"), limit=65000) or ""
        safe_messages.append(
            {
                "role": safe_assignment_token(message.get("role")) or "message",
                "source": safe_assignment_token(message.get("source")) or "model_input",
                "content": content,
                "truncated": bool(message.get("truncated")),
                "bytes": _safe_int(message.get("bytes")),
                "sha256": safe_assignment_token(message.get("sha256")),
            }
        )
    return {
        "schema_version": _safe_int(value.get("schema_version")) or 1,
        "kind": safe_assignment_token(value.get("kind")) or "redaction_safe_final_model_input",
        "platform": safe_assignment_token(value.get("platform")),
        "profile": safe_assignment_token(value.get("profile")),
        "session_id": safe_assignment_text(value.get("session_id"), limit=200),
        "task_id": safe_assignment_token(value.get("task_id")),
        "skip_context_files": bool(value.get("skip_context_files")),
        "skip_memory": bool(value.get("skip_memory")),
        "system_message_supplied": bool(value.get("system_message_supplied")),
        "message_count": _safe_int(value.get("message_count")) or len(safe_messages),
        "messages": safe_messages,
    }


def _safe_int(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _chat_history_context(*, session_db: Any | None, session_id: str | None) -> list[dict[str, Any]]:
    if session_db is None or not session_id:
        return []
    try:
        messages = session_db.get_messages(session_id)
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for item in (messages or [])[-DEFAULT_CHAT_HISTORY_LIMIT:]:
        if not isinstance(item, dict):
            continue
        role = safe_assignment_token(item.get("role")) or "message"
        content = safe_assignment_text(item.get("content"), limit=SAFE_PREVIEW_LIMIT)
        if not content:
            continue
        rows.append(
            {
                "role": "operator" if role == "user" else role,
                "text": _safe_preview(content),
                "timestamp": safe_assignment_text(item.get("created_at") or item.get("timestamp"), limit=80),
                "source": "persona_chat_history",
            }
        )
    return rows


def _safe_preview(text: str) -> str:
    safe = safe_assignment_text(text, limit=SAFE_PREVIEW_LIMIT) or ""
    safe = safe.replace("\r\n", "\n").replace("\r", "\n")
    return safe[:SAFE_PREVIEW_LIMIT]
