from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
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


@contextmanager
def _queue_lock(path, *, timeout_seconds: float = 3.0):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    fd = None
    while fd is None:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time()}".encode("ascii"))
        except FileExistsError:
            if time.monotonic() >= deadline:
                try:
                    stale = time.time() - lock_path.stat().st_mtime > 30.0
                except OSError:
                    stale = False
                if stale:
                    try:
                        lock_path.unlink()
                    except OSError:
                        pass
                    deadline = time.monotonic() + timeout_seconds
                    continue
                raise TimeoutError("queued skill lock is busy")
            time.sleep(0.025)
    try:
        yield
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except OSError:
            pass


def queue_skills_for_next_turn(
    *,
    persona_id: str,
    session_id: str,
    skills: list[str],
    persona_instance_id: str | None = None,
) -> dict[str, Any]:
    """Atomically merge a validated skill selection into the next-turn queue."""

    safe_persona = safe_assignment_token(persona_id)
    safe_session = safe_assignment_token(session_id)
    if not safe_persona:
        raise ValueError("persona_id is required")
    if not safe_session:
        raise ValueError("session_id is required")
    safe_skills = list(
        dict.fromkeys(
            token
            for item in skills
            if (token := safe_assignment_token(item))
        )
    )
    if not safe_skills:
        raise ValueError("at least one skill is required")
    path = _queue_path(safe_persona, safe_session)
    with _queue_lock(path):
        queued = pending_skills_for_next_turn(
            persona_id=safe_persona,
            session_id=safe_session,
        )
        queued = list(dict.fromkeys([*queued, *safe_skills]))
        payload = {
            "persona_id": safe_persona,
            "session_id": safe_session,
            "persona_instance_id": safe_assignment_token(persona_instance_id),
            "skills": queued,
        }
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
    with _queue_lock(path):
        skills = pending_skills_for_next_turn(persona_id=persona_id, session_id=session_id)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return skills
