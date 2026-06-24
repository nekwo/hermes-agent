from __future__ import annotations

import json
import re
from typing import Any, Iterable

from .models import PersonaInstance
from .persona_assignments import persona_instance_id_for, safe_assignment_text, safe_assignment_token

PERSONA_CHAT_SESSION_SOURCE = "agent_runtime_persona_chat"
_CHAT_INSTANCE_MODES = {"chat", "free_floating"}
DEFAULT_PERSONA_CHAT_MESSAGE_TAIL = 40
MAX_PERSONA_CHAT_MESSAGE_TAIL = 40
_TRACE_EVENT_TYPES = {"run.tool.started", "run.tool.finished", "run.progress"}
# Per-task trace fetch sizing: headroom over tail*agents to survive dilution by
# non-trace event rows, and a hard ceiling on the reverse log scan.
_TRACE_FETCH_HEADROOM = 6
_TRACE_FETCH_CEILING = 2000

_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|passwd|authorization|bearer)\s*[:=]\s*\S+"
)


def persona_chat_history_summary(
    *,
    persona_instances: Iterable[PersonaInstance],
    session_db: Any | None = None,
    event_log: Any | None = None,
    limit: int = 50,
    message_tail: int = DEFAULT_PERSONA_CHAT_MESSAGE_TAIL,
) -> list[dict[str, Any]]:
    """Return redaction-safe persona chat-history rows for Harness snapshots.

    The Harness snapshot is the Launcher contract boundary. This helper only
    projects sessions already bound to a persona instance; it includes a
    bounded redaction-safe message tail and never starts/ticks a model turn. The
    optional ``session_db`` parameter keeps tests hermetic and lets production
    use the normal ``hermes_state.SessionDB`` lazily.
    """

    bound_by_session: dict[str, PersonaInstance] = {}
    instances_by_id: dict[str, PersonaInstance] = {}
    instances_by_persona: dict[str, PersonaInstance] = {}
    for instance in persona_instances:
        instance_id = safe_assignment_text(getattr(instance, "id", None), limit=160)
        if instance_id:
            instances_by_id[instance_id] = instance
        persona_id = _canonical_persona_id(getattr(instance, "persona_id", None))
        if persona_id:
            instances_by_persona[persona_id] = instance
        mode = safe_assignment_token(getattr(instance, "mode", None))
        if mode not in _CHAT_INSTANCE_MODES:
            continue
        session_id = safe_assignment_text(getattr(instance, "session_id", None), limit=200)
        if session_id:
            bound_by_session[session_id] = instance

    db = session_db or _default_session_db()
    if db is None:
        return []

    broad_sessions = _list_sessions(
        db,
        exclude_sources=["tool"],
        limit=max(limit * 4, len(bound_by_session), 1),
        include_children=False,
    )
    source_sessions = _list_sessions(
        db,
        source=PERSONA_CHAT_SESSION_SOURCE,
        limit=max(limit * 4, len(bound_by_session), 1),
        include_children=True,
    )

    try:
        event_sessions = _list_chat_opened_event_sessions(event_log, limit=max(limit * 4, 1))
        sessions = list(source_sessions) + list(broad_sessions) + list(event_sessions)
    except Exception:
        sessions = list(broad_sessions)

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in sessions or []:
        if not isinstance(raw, dict):
            continue
        session_id = safe_assignment_text(raw.get("id"), limit=200)
        if not session_id or session_id in seen:
            continue
        is_source_chat = safe_assignment_token(raw.get("source")) == PERSONA_CHAT_SESSION_SOURCE
        inferred_persona = _infer_persona_id(raw, session_id=session_id)
        raw_instance_id = safe_assignment_text(raw.get("persona_instance_id"), limit=160)
        instance = bound_by_session.get(session_id) or instances_by_id.get(raw_instance_id) or (
            instances_by_persona.get(inferred_persona) if is_source_chat and inferred_persona else None
        )
        if instance is None:
            continue
        row = _history_row(raw, instance, session_id=session_id, session_db=db, message_tail=message_tail)
        rows.append(row)
        seen.add(session_id)
        if len(rows) >= limit:
            break

    # If SessionDB doesn't know about a bound session yet, still expose a safe
    # placeholder so Launcher can show that the persona instance is chat-bound.
    for session_id, instance in bound_by_session.items():
        if session_id in seen:
            continue
        rows.append(_history_row({}, instance, session_id=session_id, session_db=db, message_tail=message_tail))
        if len(rows) >= limit:
            break

    return rows


def persona_chat_trace_summary(
    *,
    persona_instances: Iterable[PersonaInstance],
    event_log: Any | None = None,
    message_tail: int = DEFAULT_PERSONA_CHAT_MESSAGE_TAIL,
) -> list[dict[str, Any]]:
    """Return redaction-safe tool/progress trace rows for task-bound chats.

    This is an additive snapshot projection of already-persisted EventLog rows.
    The curated persona chat history intentionally keeps dropping tool/system
    noise; trace rows live in this separate channel and are merged client-side.
    """

    log = event_log or _default_event_log()
    if log is None:
        return []
    tail = _bounded_message_tail(message_tail)

    # Group task-bound instances by task so each task's event log is scanned once
    # and the fetch window can be sized for *all* its agents at once. Fetching per
    # persona with a fixed window let a busy multi-agent task starve a quiet
    # persona's trace out of the window (its rows pushed past the cap by other
    # agents' events).
    members_by_task: dict[str, list[tuple[Any, str]]] = {}
    for instance in persona_instances:
        mode = safe_assignment_token(getattr(instance, "mode", None))
        if mode != "task_bound":
            continue
        task_id = safe_assignment_text(getattr(instance, "current_task_id", None), limit=160)
        persona_id = safe_assignment_token(getattr(instance, "persona_id", None))
        if not task_id or not persona_id:
            continue
        members_by_task.setdefault(task_id, []).append((instance, persona_id))

    rows: list[dict[str, Any]] = []
    for task_id, members in members_by_task.items():
        fetch_limit = _trace_fetch_limit(tail, len(members))
        trace_by_persona: dict[str, list[Any]] = {}
        for event in log.for_task(task_id, limit=fetch_limit):
            if getattr(event, "type", None) not in _TRACE_EVENT_TYPES:
                continue
            event_persona = getattr(event, "persona_id", None)
            if event_persona:
                trace_by_persona.setdefault(event_persona, []).append(event)
        for instance, persona_id in members:
            entries = [
                entry
                for entry in (_trace_entry(event) for event in trace_by_persona.get(persona_id, []))
                if entry is not None
            ][-tail:]
            if not entries:
                continue
            rows.append(
                {
                    "persona_instance_id": safe_assignment_text(
                        getattr(instance, "id", None) or persona_instance_id_for(persona_id),
                        limit=160,
                    ),
                    "persona_id": persona_id,
                    "task_id": task_id,
                    "entries": entries,
                }
            )
    return rows


def _list_sessions(
    db: Any,
    *,
    limit: int,
    include_children: bool,
    source: str | None = None,
    exclude_sources: list[str] | None = None,
) -> list[dict[str, Any]]:
    try:
        return list(
            db.list_sessions_rich(
                source=source,
                exclude_sources=exclude_sources,
                limit=limit,
                include_children=include_children,
                min_message_count=0,
                order_by_last_active=True,
                include_archived=True,
            )
            or []
        )
    except TypeError:
        # Some tests/fakes may implement an older subset of the signature.
        try:
            rows = list(db.list_sessions_rich(limit=limit) or [])
        except Exception:
            return []
        if source:
            rows = [row for row in rows if isinstance(row, dict) and row.get("source") == source]
        if exclude_sources:
            blocked = set(exclude_sources)
            rows = [row for row in rows if isinstance(row, dict) and row.get("source") not in blocked]
        return rows
    except Exception:
        return []


def _list_chat_opened_event_sessions(event_log: Any | None, *, limit: int) -> list[dict[str, Any]]:
    log = event_log or _default_event_log()
    if log is None:
        return []
    try:
        events = list(log.iter_all())
    except Exception:
        return []

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for evt in reversed(events):
        if safe_assignment_text(getattr(evt, "type", None), limit=120) != "persona_instance.chat_opened":
            continue
        payload = getattr(evt, "payload", None)
        if not isinstance(payload, dict):
            payload = {}
        session_id = safe_assignment_text(payload.get("session_id"), limit=200)
        if not session_id or session_id in seen:
            continue
        persona_id = _canonical_persona_id(getattr(evt, "persona_id", None)) or _infer_persona_id(
            {}, session_id=session_id
        )
        instance_id = safe_assignment_text(payload.get("persona_instance_id"), limit=160)
        rows.append(
            {
                "id": session_id,
                "source": PERSONA_CHAT_SESSION_SOURCE,
                "system_prompt": f"Mission Control persona chat for {persona_id or 'persona'}",
                "persona_instance_id": instance_id,
                "title": None,
                "preview": None,
                "message_count": 0,
                "started_at": _event_timestamp(getattr(evt, "ts", None)),
                "last_active": _event_timestamp(getattr(evt, "ts", None)),
                "archived": 0,
            }
        )
        seen.add(session_id)
        if len(rows) >= limit:
            break
    return rows


def _event_timestamp(value: Any) -> Any:
    try:
        return value.isoformat().replace("+00:00", "Z")
    except Exception:
        return value


def _default_session_db() -> Any | None:
    try:
        from hermes_state import SessionDB

        return SessionDB()
    except Exception:
        return None


def _default_event_log() -> Any | None:
    try:
        from .events import EventLog

        return EventLog()
    except Exception:
        return None


def _history_row(
    raw: dict[str, Any],
    instance: PersonaInstance,
    *,
    session_id: str,
    session_db: Any | None = None,
    message_tail: int = DEFAULT_PERSONA_CHAT_MESSAGE_TAIL,
) -> dict[str, Any]:
    persona_id = _canonical_persona_id(getattr(instance, "persona_id", None)) or "unknown"
    raw_title = safe_assignment_text(raw.get("title"), limit=120)
    title_fallback = "Untitled persona chat" if raw_title else _fallback_title(raw, persona_id=persona_id)
    title, title_status = _safe_display_text(raw.get("title"), fallback=title_fallback, limit=120)
    preview, preview_status = _safe_display_text(
        raw.get("preview"),
        fallback="No messages yet.",
        redacted_fallback="Preview hidden by redaction boundary",
        limit=180,
    )
    messages, messages_status = _safe_recent_messages(session_db, session_id=session_id, limit=message_tail)
    redaction_status = (
        "redacted" if "redacted" in {title_status, preview_status, messages_status} else "safe"
    )
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
        **_token_usage_fields(raw),
        "messages": messages,
    }


def _token_usage_fields(raw: dict[str, Any]) -> dict[str, int]:
    input_tokens = _safe_int(raw.get("input_tokens"))
    output_tokens = _safe_int(raw.get("output_tokens"))
    total_tokens = _safe_int(raw.get("total_tokens"))
    if total_tokens == 0 and (input_tokens or output_tokens):
        total_tokens = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def _infer_persona_id(raw: dict[str, Any], *, session_id: str) -> str | None:
    system_prompt = safe_assignment_text(raw.get("system_prompt"), limit=240)
    marker = "Mission Control persona chat for "
    if marker in system_prompt:
        return _canonical_persona_id(system_prompt.split(marker, 1)[1])
    prefix = "persona_chat_personainst_"
    if session_id.startswith(prefix):
        return _persona_token_from_chat_session_tail(session_id[len(prefix) :])
    prefix = "persona_chat_"
    if session_id.startswith(prefix):
        value = session_id[len(prefix) :]
        if value.startswith("personainst_"):
            value = value[len("personainst_") :]
        return _persona_token_from_chat_session_tail(value)
    return None


def _persona_token_from_chat_session_tail(value: str) -> str | None:
    token = safe_assignment_token(value)
    if not token:
        return None
    parts = token.rsplit("_", 1)
    if len(parts) == 2 and len(parts[1]) == 12 and all(ch in "0123456789abcdef" for ch in parts[1].lower()):
        token = parts[0]
    return _canonical_persona_id(token)


def _canonical_persona_id(value: Any) -> str | None:
    raw = safe_assignment_text(value, limit=160)
    if not raw:
        return None
    if raw.lower().startswith("profile:"):
        profile = safe_assignment_token(raw.split(":", 1)[1])
        return f"profile:{profile}" if profile else None
    token = safe_assignment_token(raw)
    if token.startswith("profile_") and len(token) > len("profile_"):
        return f"profile:{token[len('profile_'):]}"
    return token or None


def _fallback_title(raw: dict[str, Any], *, persona_id: str) -> str:
    preview, status = _safe_display_text(raw.get("preview"), fallback="", limit=80)
    if status == "safe" and preview and not any(marker in preview for marker in _INTERNAL_SCAFFOLDING_MARKERS):
        return preview
    label = persona_id.replace("_", " ").strip().title() if persona_id else "Persona"
    return f"{label} chat"


def _safe_recent_messages(
    session_db: Any | None,
    *,
    session_id: str,
    limit: int = DEFAULT_PERSONA_CHAT_MESSAGE_TAIL,
) -> tuple[list[dict[str, Any]], str]:
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
                "timestamp": _iso_timestamp(
                    raw.get("created_at")
                    or raw.get("timestamp")
                    or raw.get("time")
                    or raw.get("updated_at")
                ),
                "redaction_status": status,
            }
        )
    rows = rows[-_bounded_message_tail(limit):]
    return rows, "redacted" if redacted else "safe"


def _iso_timestamp(value: Any) -> str | None:
    """Normalize a message timestamp to the same ISO-8601 ``Z`` form as traces.

    SessionDB stores message timestamps as epoch-seconds floats (``time.time()``),
    while harness-trace rows carry ISO strings (``Event.ts`` via ``to_jsonable``).
    The Launcher merges the two channels by parsing each ``ts`` with
    ``DateTime.tryParse`` and orders them — an epoch float is unparseable there, so
    without this the curated rows lose their time and the trace block jumps ahead
    of them. Project both channels in one comparable format.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)):
        from datetime import datetime, timezone

        epoch = float(value)
        if epoch > 1e12:  # tolerate millisecond clocks
            epoch /= 1000.0
        try:
            moment = datetime.fromtimestamp(epoch, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return None


def _trace_entry(event: Any) -> dict[str, Any] | None:
    payload = getattr(event, "payload", None)
    if not isinstance(payload, dict):
        payload = {}
    event_type = safe_assignment_text(getattr(event, "type", None), limit=80)
    trace_event = {
        "run.tool.started": "tool_started",
        "run.tool.finished": "tool_finished",
        "run.progress": "progress",
    }.get(event_type)
    if trace_event is None:
        return None

    tool_name = _safe_trace_text(payload.get("tool_name") or payload.get("tool"), limit=120)
    summary = _first_safe_trace_text(
        payload.get("summary"),
        payload.get("patch_summary"),
        payload.get("code_summary"),
        payload.get("command_label"),
        payload.get("file_summary"),
        limit=500,
    )
    status = _safe_trace_text(payload.get("status") or payload.get("exit_code"), limit=80)
    files = _safe_trace_file_labels(payload.get("changed_files") or payload.get("files_touched"))
    return {
        "kind": "harness_trace",
        "task_id": safe_assignment_text(getattr(event, "task_id", None), limit=160),
        "persona_id": safe_assignment_token(getattr(event, "persona_id", None)) or "unknown",
        "run_id": safe_assignment_text(getattr(event, "run_id", None), limit=160),
        "stage_id": _safe_trace_text(payload.get("stage_id"), limit=120),
        "event": trace_event,
        "tool_name": tool_name,
        "summary": summary,
        "files": files,
        "status": status,
        "ts": getattr(event, "ts", None),
    }


def _first_safe_trace_text(*values: Any, limit: int) -> str | None:
    for value in values:
        safe = _safe_trace_text(value, limit=limit)
        if safe:
            return safe
    return None


def _safe_trace_text(value: Any, *, limit: int) -> str | None:
    text = safe_assignment_text(value, limit=limit)
    if not text:
        return None
    if _SECRET_RE.search(text) or _looks_pathish(text):
        return None
    return text


def _safe_trace_file_labels(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    labels: list[str] = []
    for item in value:
        text = safe_assignment_text(item, limit=120)
        if not text:
            continue
        if _SECRET_RE.search(text) or _looks_pathish(text):
            continue
        label = text.replace("\\", "/").rsplit("/", 1)[-1]
        if not label or _SECRET_RE.search(label) or _looks_pathish(label):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", label):
            continue
        labels.append(label)
    return labels[:12]


def _looks_pathish(value: str) -> bool:
    if ":/" in value or "\\" in value or value.startswith(("/", "~")):
        return True
    return bool(re.search(r"(^|\s)([A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+", value))


def _bounded_message_tail(value: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = DEFAULT_PERSONA_CHAT_MESSAGE_TAIL
    return min(max(parsed, 1), MAX_PERSONA_CHAT_MESSAGE_TAIL)


def _trace_fetch_limit(tail: int, member_count: int) -> int:
    """Size the per-task event fetch so each agent can reach ``tail`` trace rows.

    The task's EventLog interleaves every agent's events plus non-trace rows
    (worker_session.*, heartbeats, …), so a flat window starves quiet agents.
    Scale by the agent count with headroom for that dilution, then hard-cap so
    the reverse file scan stays bounded.
    """

    agents = max(int(member_count or 0), 1)
    return min(max(tail * agents * _TRACE_FETCH_HEADROOM, tail), _TRACE_FETCH_CEILING)


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


def _safe_display_text(
    value: Any,
    *,
    fallback: str,
    limit: int,
    redacted_fallback: str | None = None,
) -> tuple[str, str]:
    text = safe_assignment_text(value, limit=limit)
    if not text:
        return fallback, "safe"
    if _SECRET_RE.search(text):
        return redacted_fallback or fallback, "redacted"
    return text, "safe"


def _safe_int(value: Any) -> int:
    try:
        parsed = int(value)
    except Exception:
        return 0
    return max(parsed, 0)
