from __future__ import annotations

import json
from typing import Any

from utils import atomic_json_write

from . import paths
from .persona_assignments import safe_assignment_token


def _queue_dir():
    return paths.store_root() / "queued_skills"


def _queue_path(persona_id: str, session_id: str):
    persona = safe_assignment_token(persona_id) or "unknown"
    session = safe_assignment_token(session_id) or "unknown"
    return _queue_dir() / f"{persona}__{session}.json"


def queue_skill_for_next_turn(
    *,
    persona_id: str,
    session_id: str,
    skill: str,
    persona_instance_id: str | None = None,
) -> dict[str, Any]:
    """Queue one skill to be preloaded for the next Mission Control chat turn."""

    safe_persona = safe_assignment_token(persona_id)
    safe_session = safe_assignment_token(session_id)
    safe_skill = safe_assignment_token(skill)
    if not safe_persona:
        raise ValueError("persona_id is required")
    if not safe_session:
        raise ValueError("session_id is required")
    if not safe_skill:
        raise ValueError("skill is required")
    queued = pending_skills_for_next_turn(
        persona_id=safe_persona,
        session_id=safe_session,
    )
    if safe_skill not in queued:
        queued.append(safe_skill)
    payload = {
        "persona_id": safe_persona,
        "session_id": safe_session,
        "persona_instance_id": safe_assignment_token(persona_instance_id),
        "skills": queued,
    }
    path = _queue_path(safe_persona, safe_session)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, payload, indent=2, sort_keys=True)
    return payload


def pending_skills_for_next_turn(*, persona_id: str, session_id: str) -> list[str]:
    path = _queue_path(persona_id, session_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw = data.get("skills") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for item in raw:
        token = safe_assignment_token(item)
        if token and token not in result:
            result.append(token)
    return result


def consume_skills_for_next_turn(*, persona_id: str, session_id: str) -> list[str]:
    path = _queue_path(persona_id, session_id)
    skills = pending_skills_for_next_turn(persona_id=persona_id, session_id=session_id)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    return skills
