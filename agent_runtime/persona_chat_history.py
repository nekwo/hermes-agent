from __future__ import annotations

import json
import re
from typing import Any, Iterable

from .models import PersonaInstance
from .persona_assignments import persona_instance_id_for, safe_assignment_text, safe_assignment_token

_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|passwd|authorization|bearer)\s*[:=]\s*\S+"
)


def persona_chat_history_summary(
    *,
    persona_instances: Iterable[PersonaInstance],
    session_db: Any | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return redaction-safe persona chat-history rows for Harness snapshots.

    The Harness snapshot is the Launcher contract boundary. This helper only
    projects sessions already bound to a persona instance; it includes a
    bounded redaction-safe message tail and never starts/ticks a model turn. The
    optional ``session_db`` parameter keeps tests hermetic and lets production
    use the normal ``hermes_state.SessionDB`` lazily.
    """

    bound_by_session: dict[str, PersonaInstance] = {}
    for instance in persona_instances:
        session_id = safe_assignment_text(getattr(instance, "session_id", None), limit=200)
        if not session_id:
            continue
        bound_by_session[session_id] = instance
    if not bound_by_session:
        return []

    db = session_db or _default_session_db()
    if db is None:
        return []

    try:
        sessions = db.list_sessions_rich(
            exclude_sources=["tool"],
            limit=max(limit * 4, len(bound_by_session)),
            include_children=False,
            min_message_count=0,
            order_by_last_active=True,
            include_archived=True,
        )
    except TypeError:
        # Some tests/fakes may implement an older subset of the signature.
        try:
            sessions = db.list_sessions_rich(limit=max(limit * 4, len(bound_by_session)))
        except Exception:
            return []
    except Exception:
        return []

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in sessions or []:
        if not isinstance(raw, dict):
            continue
        session_id = safe_assignment_text(raw.get("id"), limit=200)
        if not session_id or session_id in seen:
            continue
        instance = bound_by_session.get(session_id)
        if instance is None:
            continue
        row = _history_row(raw, instance, session_id=session_id, session_db=db)
        rows.append(row)
        seen.add(session_id)
        if len(rows) >= limit:
            break

    # If SessionDB doesn't know about a bound session yet, still expose a safe
    # placeholder so Launcher can show that the persona instance is chat-bound.
    for session_id, instance in bound_by_session.items():
        if session_id in seen:
            continue
        rows.append(_history_row({}, instance, session_id=session_id, session_db=db))
        if len(rows) >= limit:
            break

    return rows


def _default_session_db() -> Any | None:
    try:
        from hermes_state import SessionDB

        return SessionDB()
    except Exception:
        return None


def _history_row(
    raw: dict[str, Any],
    instance: PersonaInstance,
    *,
    session_id: str,
    session_db: Any | None = None,
) -> dict[str, Any]:
    title, title_status = _safe_display_text(raw.get("title"), fallback="Untitled persona chat", limit=120)
    preview, preview_status = _safe_display_text(
        raw.get("preview"), fallback="Preview hidden by redaction boundary", limit=180
    )
    messages, messages_status = _safe_recent_messages(session_db, session_id=session_id)
    redaction_status = (
        "redacted" if "redacted" in {title_status, preview_status, messages_status} else "safe"
    )
    persona_id = safe_assignment_token(getattr(instance, "persona_id", None)) or "unknown"
    return {
        "session_id": session_id,
        "persona_id": persona_id,
        "persona_instance_id": safe_assignment_text(
            getattr(instance, "id", None) or persona_instance_id_for(persona_id),
            limit=160,
        ),
        "title": title,
        "last_message_preview": preview,
        "message_count": _safe_int(raw.get("message_count")),
        "created_at": raw.get("started_at"),
        "updated_at": raw.get("last_active") or raw.get("ended_at") or raw.get("started_at"),
        "state": "archived" if bool(raw.get("archived")) else "open",
        "redaction_status": redaction_status,
        "messages": messages,
    }


def _safe_recent_messages(session_db: Any | None, *, session_id: str, limit: int = 8) -> tuple[list[dict[str, Any]], str]:
    if session_db is None:
        return [], "safe"
    try:
        raw_messages = session_db.get_messages(session_id)
    except Exception:
        return [], "safe"
    rows: list[dict[str, Any]] = []
    redacted = False
    # Curate the agent's raw working transcript into an operator-facing one.
    # The bound session is the agent's internal session, so its raw rows are
    # verbose tick-context prompts (role=user) and serialized decision dicts
    # (role=assistant) plus tool/system noise. We surface only clean operator
    # turns and decision summaries; scaffolding/tool/system rows are dropped.
    for index, raw in enumerate(raw_messages or []):
        if not isinstance(raw, dict):
            continue
        role = _safe_message_role(raw.get("role"))
        if role not in {"operator", "agent"}:
            continue
        curated = _curate_chat_message_text(
            role, raw.get("content") or raw.get("safe_text") or raw.get("text")
        )
        if not curated:
            continue
        text, status = _safe_display_text(
            curated,
            fallback="Message hidden by redaction boundary",
            limit=1200,
        )
        if not text:
            continue
        if status == "redacted":
            redacted = True
        rows.append(
            {
                "id": safe_assignment_text(raw.get("id"), limit=120)
                or f"{session_id}:{index}",
                "role": role,
                "safe_text": text,
                "timestamp": raw.get("created_at")
                or raw.get("timestamp")
                or raw.get("time")
                or raw.get("updated_at"),
                "redaction_status": status,
            }
        )
    rows = rows[-limit:]
    return rows, "redacted" if redacted else "safe"


# Markers that identify the agent's internal scaffolding (never operator-facing).
_INTERNAL_SCAFFOLDING_MARKERS = (
    "# Agent Runtime Tick Context",
    "## Task Snapshot",
    "Repo-Grounded Execution",
    "Prior persona chat context",
)


def _curate_chat_message_text(role: str, content: Any) -> str | None:
    """Project a raw agent-session row into clean operator-facing text.

    Agent rows that are serialized decision dicts collapse to their summary
    (+ rationale); operator rows that are internal tick-context scaffolding are
    dropped (the clean operator message is shown via the optimistic UI path).
    Returns ``None`` for rows that should not appear in the operator transcript.
    """

    if role == "agent":
        summary = _decision_summary_text(content)
        if summary:
            return summary
        text = safe_assignment_text(content, limit=4000)
        if not text or text.startswith("{"):
            # Empty assistant turn or an unparseable raw dict — not presentable.
            return None
        return text
    if role == "operator":
        text = safe_assignment_text(content, limit=4000)
        if not text:
            return None
        if any(marker in text for marker in _INTERNAL_SCAFFOLDING_MARKERS):
            return None
        return text
    return None


def _decision_summary_text(content: Any) -> str | None:
    raw = content if isinstance(content, str) else str(content or "")
    raw = raw.strip()
    if not raw.startswith("{"):
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    parts = [
        str(data[key]).strip()
        for key in ("summary", "rationale")
        if isinstance(data.get(key), str) and str(data.get(key)).strip()
    ]
    if not parts:
        return None
    return "\n\n".join(parts)


def _safe_message_role(value: Any) -> str | None:
    role = safe_assignment_token(value)
    if role in {"user", "operator"}:
        return "operator"
    if role in {"assistant", "agent"}:
        return "agent"
    if role == "system":
        return "system"
    return None


def _safe_display_text(value: Any, *, fallback: str, limit: int) -> tuple[str, str]:
    text = safe_assignment_text(value, limit=limit)
    if not text:
        return fallback, "redacted" if "redaction" in fallback.lower() else "safe"
    if _SECRET_RE.search(text):
        return fallback, "redacted"
    return text, "safe"


def _safe_int(value: Any) -> int:
    try:
        parsed = int(value)
    except Exception:
        return 0
    return max(parsed, 0)
