from __future__ import annotations

import json
import re
from typing import Any, Iterable

from .models import PersonaInstance
from .mission_chat_turns import mission_chat_turn_elements, mission_chat_turn_records
from .parity import ProjectionAccountant
from .persona_assignments import persona_instance_id_for, safe_assignment_text, safe_assignment_token
from .redaction_mode import redaction_observe_enabled
from .relay_policy import parse_relay_sender_marker
from .transcript_order import (
    TURN_SEQ_CONTENT,
    TURN_SEQ_OPERATOR,
    TURN_SEQ_TERMINAL,
    order_transcript_rows,
)

PERSONA_CHAT_SESSION_SOURCE = "agent_runtime_persona_chat"
# Structural marker for the canned "I'll … then report back with …" pre-trace
# acknowledgment the persona-chat turn writes ahead of the real LLM reply. The
# ack text is tool-specific (several variants) and changes over time, so we stamp
# a machine flag at persist time (``finish_reason``) and re-emit it as a typed
# message ``kind`` through every projection. Consumers (operator conversation,
# the Launcher) key on the kind instead of matching the prose.
PERSONA_PRE_TRACE_ACK_FINISH_REASON = "pre_trace_ack"
PERSONA_PRE_TRACE_ACK_KIND = "pre_trace_ack"

# Typed kind for an incoming role="user" row that a relay tagged with the
# sending agent's identity (finish_reason=relay_from:<persona>:<instance>). The
# conversation projection keys on this to attribute the message to the sending
# AGENT instead of the operator. See agent_runtime/relay_policy.py.
PERSONA_RELAYED_MESSAGE_KIND = "relayed_message"
_CHAT_INSTANCE_MODES = {"chat", "free_floating"}
DEFAULT_PERSONA_CHAT_MESSAGE_TAIL = 40
MAX_PERSONA_CHAT_MESSAGE_TAIL = 40
PERSONA_CHAT_MESSAGE_TEXT_LIMIT = 20000
_CHAT_MODEL_OVERRIDE_CONFIG_KEY = "mission_control_chat_model_override"
_TRACE_EVENT_TYPES = {
    "run.tool.started",
    "run.tool.finished",
    "run.progress",
    "task.transition",
    "persona_assignment.created",
    "persona_assignment.closed",
}
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
    limit: int = 50,
    message_tail: int = DEFAULT_PERSONA_CHAT_MESSAGE_TAIL,
    accountant: ProjectionAccountant | None = None,
    persona_assignments: Iterable[Any] | None = None,
) -> list[dict[str, Any]]:
    """Return redaction-safe persona chat-history rows for Harness snapshots.

    The Harness snapshot is the Launcher contract boundary. This helper only
    projects sessions already bound to a persona instance; it includes a
    bounded redaction-safe message tail and never starts/ticks a model turn. The
    optional ``session_db`` parameter keeps tests hermetic and lets production
    use the normal ``hermes_state.SessionDB`` lazily. ``persona_assignments``
    is the already-loaded assignment list the snapshot/status builders hold —
    synthetic live-mission rows anchor their timestamps to the bound
    assignment's persisted ``created_at`` (R3); this helper never scans the
    assignment store itself.
    """

    bound_by_session: dict[str, PersonaInstance] = {}
    instances_by_persona: dict[str, PersonaInstance] = {}
    for instance in persona_instances:
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
        sessions = list(source_sessions) + list(broad_sessions)
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
        # Only persona-chat sessions and sessions bound to a live instance are
        # candidates for this projection. Unrelated SessionDB sources (cron,
        # telegram, cli, scratch) can never render as chat rows — counting them
        # as no_instance_match drops floods parity with by-design noise.
        if not is_source_chat and session_id not in bound_by_session:
            seen.add(session_id)
            continue
        if accountant is not None:
            accountant.consider(1)
        inferred_persona = _infer_persona_id(raw, session_id=session_id)
        instance = bound_by_session.get(session_id) or (
            instances_by_persona.get(inferred_persona) if is_source_chat and inferred_persona else None
        )
        if instance is None:
            if accountant is not None:
                accountant.drop("no_instance_match", entity_id=session_id)
            # A session id can appear in both the source and broad pools;
            # mark it seen so a drop is only accounted once.
            seen.add(session_id)
            continue
        row = _history_row(raw, instance, session_id=session_id, session_db=db, message_tail=message_tail)
        rows.append(row)
        seen.add(session_id)
        if accountant is not None:
            accountant.include(1)
        if len(rows) >= limit:
            if accountant is not None:
                accountant.mark_truncated()
            break

    # Create/open paths now persist persona chat sessions before exposing them.
    # If the active instance still points at a session that SessionDB no longer
    # knows about, treat SessionDB as authoritative: the operator may have
    # deleted that chat, and resurrecting it as an empty placeholder is worse
    # than temporarily hiding a failed write.
    for session_id, instance in bound_by_session.items():
        if session_id in seen:
            continue
        raw = _get_session_row(db, session_id)
        if raw is None:
            if accountant is not None:
                accountant.consider(1)
                accountant.drop("session_not_in_db", entity_id=session_id)
            continue
        rows.append(_history_row(raw, instance, session_id=session_id, session_db=db, message_tail=message_tail))
        seen.add(session_id)
        if accountant is not None:
            accountant.consider(1)
            accountant.include(1)
        if len(rows) >= limit:
            break

    # Live mission sessions. A task-bound instance runs its mission turns in a
    # session that lives in the run/event stream, not the operator SessionDB, so
    # the loops above never surface it — leaving the console on a stale operator
    # chat with nothing to switch to. Emit a minimal, live-marked row (no
    # messages; the persona_chat_trace lane carries the actual mission activity)
    # so the session is selectable and the console can switch to what is running.
    # This is NOT resurrecting a deleted chat: it is an active mission, and the
    # row only exists while the instance is bound to a task.
    assignment_rows = [item for item in (persona_assignments or []) if item is not None]
    assignments_by_id: dict[str, Any] = {}
    for item in assignment_rows:
        assignment_id = safe_assignment_text(getattr(item, "id", None), limit=160)
        if assignment_id:
            assignments_by_id[assignment_id] = item
    for instance in persona_instances:
        if safe_assignment_token(getattr(instance, "mode", None)) != "task_bound":
            continue
        session_id = safe_assignment_text(getattr(instance, "session_id", None), limit=200)
        task_id = safe_assignment_text(getattr(instance, "current_task_id", None), limit=160)
        if not session_id or not task_id or session_id in seen:
            continue
        goal = safe_assignment_text(getattr(instance, "current_chat_goal", None), limit=120)
        # R3 anchor: PersonaInstance persists no assigned_at, and its updated_at
        # restamps on every derive_from_workers pass — the bound assignment's
        # created_at is the only byte-stable "assigned at" truth in reach. Both
        # synthetic timestamps use it (never build time); unknown stays null so
        # it sorts as unknown rather than newest.
        assignment = _mission_assignment_for(instance, assignments_by_id, assignment_rows, task_id=task_id)
        assigned = _iso_timestamp(getattr(assignment, "created_at", None)) if assignment is not None else None
        if assigned is None:
            assigned = _iso_timestamp(getattr(instance, "assigned_at", None))
        synthetic = {
            "id": session_id,
            "title": goal or "Mission run",
            "message_count": 0,
            "started_at": assigned,
            "last_active": assigned,
        }
        row = _history_row(
            synthetic,
            instance,
            session_id=session_id,
            session_db=db,
            message_tail=message_tail,
            kind="mission",
        )
        row["task_id"] = task_id
        rows.append(row)
        seen.add(session_id)
        if accountant is not None:
            accountant.consider(1)
            accountant.include(1)
        if len(rows) >= limit:
            break

    return rows


def persona_chat_session_messages(
    *,
    session_id: str,
    limit: int = DEFAULT_PERSONA_CHAT_MESSAGE_TAIL,
    session_db: Any | None = None,
) -> dict[str, Any]:
    """Paged on-demand read of one persona chat session's message tail.

    Backs ``harness persona chat history`` — the fetch that replaces the message
    tail S2 evicts from the steady-state frame. Reuses the exact curation the
    in-frame projection used (``_safe_recent_messages``), so the fetched tail is
    identical to what ``persona_chat_history[].messages`` used to carry. The log
    IS the history: no new storage, just a per-session read of the durable
    SessionDB the frame previously projected inline.
    """

    bounded = _bounded_message_tail(limit)
    db = session_db or _default_session_db()
    if db is None:
        return {
            "ok": True,
            "session_id": session_id,
            "limit": bounded,
            "count": 0,
            "redaction_status": "safe",
            "messages": [],
        }
    messages, status = _safe_recent_messages(db, session_id=session_id, limit=bounded)
    return {
        "ok": True,
        "session_id": session_id,
        "limit": bounded,
        "count": len(messages),
        "redaction_status": status,
        "messages": messages,
    }


def persona_chat_trace_summary(
    *,
    persona_instances: Iterable[PersonaInstance],
    event_log: Any | None = None,
    message_tail: int = DEFAULT_PERSONA_CHAT_MESSAGE_TAIL,
    accountant: "ProjectionAccountant | None" = None,
) -> list[dict[str, Any]]:
    """Return redaction-safe tool/progress trace rows for persona chats.

    This is an additive snapshot projection of already-persisted EventLog rows.
    The curated persona chat history intentionally keeps dropping tool/system
    noise; trace rows live in this separate channel and are merged client-side.

    Two disjoint lanes feed a persona instance's trace, merged chronologically:

    * **task-run trace** — events a task-bound run emits via ``RunProgressSink``,
      keyed on ``task_id`` (``session_id`` is ``None``). Grouped per task so each
      task's event log is scanned once and the fetch window can be sized for
      *all* its agents at once — a flat per-persona window let a busy
      multi-agent task starve a quiet persona's trace out of the window.
    * **chat-turn trace** — tool calls an operator chat turn makes via
      ``ChatProgressSink``, keyed on ``session_id`` (``task_id`` is ``None``).
      Surfaced for *any* instance with a bound session, regardless of mode, so a
      conversational tool call shows in the operator channel's Trace lane even
      when no task is attached.

    The lanes never overlap (task events carry no ``session_id``; chat events
    carry no ``task_id``), so merging is a plain union with no double counting.
    """

    log = event_log or _default_event_log()
    if log is None:
        return []
    tail = _bounded_message_tail(message_tail)

    instances = list(persona_instances)
    # Preserve instance order for stable row output while accumulating each
    # instance's events from both lanes before rendering.
    accumulators: "dict[str, _TraceAccumulator]" = {}
    order: list[str] = []

    def _accumulator(instance: Any, persona_id: str) -> "_TraceAccumulator":
        instance_id = safe_assignment_text(
            getattr(instance, "id", None) or persona_instance_id_for(persona_id),
            limit=160,
        )
        acc = accumulators.get(instance_id)
        if acc is None:
            acc = _TraceAccumulator(
                instance_id=instance_id,
                persona_id=persona_id,
                task_id=safe_assignment_text(getattr(instance, "current_task_id", None), limit=160),
                session_id=safe_assignment_text(getattr(instance, "session_id", None), limit=200),
            )
            accumulators[instance_id] = acc
            order.append(instance_id)
        return acc

    # --- Lane 1: task-run trace, grouped per task. ---
    # Persona identity is canonicalized the same way the chat-history projection
    # does (``_canonical_persona_id``), NOT via ``safe_assignment_token``: the
    # latter mangles ids like "profile:alice" → "profile_alice", which never
    # matches the raw "profile:alice" stored on the events, silently dropping
    # every profile-instance trace row. Canonicalizing both sides also keeps the
    # row's ids identical to the history row so the Launcher matches them.
    members_by_task: dict[str, list[tuple[Any, str]]] = {}
    for instance in instances:
        mode = safe_assignment_token(getattr(instance, "mode", None))
        if mode != "task_bound":
            continue
        task_id = safe_assignment_text(getattr(instance, "current_task_id", None), limit=160)
        persona_id = _canonical_persona_id(getattr(instance, "persona_id", None))
        if not task_id or not persona_id:
            continue
        members_by_task.setdefault(task_id, []).append((instance, persona_id))

    for task_id, members in members_by_task.items():
        fetch_limit = _trace_fetch_limit(tail, len(members))
        trace_by_persona: dict[str, list[Any]] = {}
        for event in _fetch_trace_events(log.for_task, task_id, limit=fetch_limit):
            if getattr(event, "type", None) not in _TRACE_EVENT_TYPES:
                continue
            event_persona = _canonical_persona_id(getattr(event, "persona_id", None))
            if event_persona:
                trace_by_persona.setdefault(event_persona, []).append(event)
        for instance, persona_id in members:
            _accumulator(instance, persona_id).extend(trace_by_persona.get(persona_id, []))

    # --- Lane 2: conversational chat-turn trace, keyed on the bound session. ---
    for instance in instances:
        session_id = safe_assignment_text(getattr(instance, "session_id", None), limit=200)
        persona_id = _canonical_persona_id(getattr(instance, "persona_id", None))
        if not session_id or not persona_id:
            continue
        if not _supports_for_session(log):
            break
        fetch_limit = _trace_fetch_limit(tail, 1)
        chat_events: list[Any] = []
        for event in _fetch_trace_events(log.for_session, session_id, limit=fetch_limit):
            if getattr(event, "type", None) not in _TRACE_EVENT_TYPES:
                continue
            event_persona = _canonical_persona_id(getattr(event, "persona_id", None))
            if event_persona and event_persona != persona_id:
                if accountant is not None:
                    accountant.consider(1)
                    accountant.drop("persona_mismatch", entity_id=session_id, detail=event_persona)
                continue
            chat_events.append(event)
        _accumulator(instance, persona_id).extend(chat_events)

    rows: list[dict[str, Any]] = []
    for instance_id in order:
        acc = accumulators[instance_id]
        entries = acc.entries(tail=tail, accountant=accountant)
        if not entries:
            continue
        row: dict[str, Any] = {
            "persona_instance_id": acc.instance_id,
            "persona_id": acc.persona_id,
            "task_id": acc.task_id,
            "entries": entries,
        }
        if acc.session_id:
            row["session_id"] = acc.session_id
        rows.append(row)
    return rows


def _supports_for_session(log: Any) -> bool:
    return callable(getattr(log, "for_session", None))


def _fetch_trace_events(fetch: Any, key: str, *, limit: int) -> list[Any]:
    """Fetch trace-lane events with a type-aware limit when the log supports it.

    ``types=_TRACE_EVENT_TYPES`` makes ``limit`` count matched trace rows, so a
    task whose recent event tail is flooded with non-trace rows (e.g. a
    budget-incident loop) cannot starve the window. Test fakes (and any legacy
    log) without the ``types`` keyword fall back to the untyped fetch — same
    tolerance pattern as ``_list_sessions``.
    """

    try:
        return list(fetch(key, limit=limit, types=_TRACE_EVENT_TYPES))
    except TypeError:
        return list(fetch(key, limit=limit))


class _TraceAccumulator:
    """Collects a persona instance's trace events across lanes, then renders
    them chronologically into a bounded list of redaction-safe entry dicts."""

    __slots__ = ("instance_id", "persona_id", "task_id", "session_id", "_events")

    def __init__(self, *, instance_id: str, persona_id: str, task_id: str | None, session_id: str | None):
        self.instance_id = instance_id
        self.persona_id = persona_id
        self.task_id = task_id or None
        self.session_id = session_id or None
        self._events: list[Any] = []

    def extend(self, events: Iterable[Any]) -> None:
        self._events.extend(events)

    def entries(self, *, tail: int, accountant: "ProjectionAccountant | None" = None) -> list[dict[str, Any]]:
        ordered = sorted(self._events, key=_trace_event_sort_key)
        rendered: list[dict[str, Any]] = []
        unrenderable = 0
        for event in ordered:
            entry = _trace_entry(event)
            if entry is None:
                unrenderable += 1
                continue
            rendered.append(entry)
        kept = _retain_trace_tail(rendered, tail=tail)
        if accountant is not None:
            accountant.consider(len(self._events))
            accountant.include(len(kept))
            if unrenderable:
                accountant.drop("unrenderable_entry", count=unrenderable, entity_id=self.instance_id)
            truncated = len(rendered) - len(kept)
            if truncated > 0:
                accountant.drop("tail_truncated", count=truncated, entity_id=self.instance_id)
                accountant.mark_truncated()
        return kept


def _retain_trace_tail(rendered: list[dict[str, Any]], *, tail: int) -> list[dict[str, Any]]:
    if len(rendered) <= tail:
        return rendered
    latest_start = max(0, len(rendered) - tail)
    keep = {index for index in range(latest_start, len(rendered))}
    keep.update(
        index
        for index, entry in enumerate(rendered)
        if _priority_trace_entry(entry)
    )
    while len(keep) > tail:
        removable = [index for index in sorted(keep) if not _priority_trace_entry(rendered[index])]
        if not removable:
            break
        keep.remove(removable[0])
    if len(keep) > tail:
        keep = set(sorted(keep)[-tail:])
    return [entry for index, entry in enumerate(rendered) if index in keep]


def _priority_trace_entry(entry: dict[str, Any]) -> bool:
    return safe_assignment_token(entry.get("event")) in {
        "assignment_created",
        "assignment_closed",
    }


def _trace_event_sort_key(event: Any) -> tuple[int, float, str]:
    """Chronological sort key tolerant of missing/odd timestamps. Events with a
    real ``ts`` sort by time; anything unparseable sinks to the front in a
    stable, comparison-safe way (no naive/aware datetime mixing)."""

    ts = getattr(event, "ts", None)
    try:
        return (1, ts.timestamp(), "")
    except Exception:
        return (0, 0.0, str(ts or ""))


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


def _get_session_row(db: Any, session_id: str) -> dict[str, Any] | None:
    try:
        raw = db.get_session(session_id)
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def _default_session_db() -> Any | None:
    try:
        from hermes_state import SessionDB

        return SessionDB()
    except Exception:
        return None


def _mission_assignment_for(
    instance: Any,
    assignments_by_id: dict[str, Any],
    assignment_rows: list[Any],
    *,
    task_id: str,
) -> Any | None:
    """Resolve the assignment record a task-bound instance is running under.

    Primary join is ``instance.current_assignment_id``; when that is unset the
    newest assignment matching ``(persona_instance_id, task_id)`` vouches. Both
    lookups stay inside the caller-provided list — no store scan.
    """

    assignment_id = safe_assignment_text(getattr(instance, "current_assignment_id", None), limit=160)
    if assignment_id and assignment_id in assignments_by_id:
        return assignments_by_id[assignment_id]
    instance_id = safe_assignment_text(getattr(instance, "id", None), limit=160)
    if not instance_id:
        return None
    candidates = [
        item
        for item in assignment_rows
        if safe_assignment_text(getattr(item, "persona_instance_id", None), limit=160) == instance_id
        and safe_assignment_text(getattr(item, "task_id", None), limit=160) == task_id
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: _iso_timestamp(getattr(item, "created_at", None)) or "")


def _history_row(
    raw: dict[str, Any],
    instance: PersonaInstance,
    *,
    session_id: str,
    session_db: Any | None = None,
    message_tail: int = DEFAULT_PERSONA_CHAT_MESSAGE_TAIL,
    kind: str = "chat",
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
        "would_redact"
        if "would_redact" in {title_status, preview_status, messages_status}
        else "redacted"
        if "redacted" in {title_status, preview_status, messages_status}
        else "safe"
    )
    would_redact = {
        label: status
        for label, status in {
            "title": title_status,
            "preview": preview_status,
            "messages": messages_status,
        }.items()
        if status == "would_redact"
    }
    return {
        "session_id": session_id,
        "persona_id": persona_id,
        "persona_instance_id": safe_assignment_text(
            getattr(instance, "id", None) or persona_instance_id_for(persona_id),
            limit=160,
        ),
        "kind": "mission" if kind == "mission" or bool(raw.get("live_mission")) else "chat",
        "live_mission": bool(kind == "mission" or raw.get("live_mission")),
        "title": title,
        "last_message_preview": preview,
        "message_count": _safe_int(raw.get("message_count")),
        "created_at": _iso_timestamp(raw.get("started_at")),
        "updated_at": _iso_timestamp(raw.get("last_active") or raw.get("ended_at") or raw.get("started_at")),
        "state": "archived" if bool(raw.get("archived")) else "open",
        "redaction_status": redaction_status,
        **({"would_redact": would_redact} if would_redact else {}),
        **_token_usage_fields(raw),
        **_chat_model_fields(raw),
        **_cache_policy_fields(raw),
        "messages": messages,
    }


def _cache_policy_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """Emit the session's prompt-cache policy so the Launcher can render an
    honest freshness/expiry indicator (see agent_runtime.cache_policy).

    Provider/model are resolved the same way the token label picks its effective
    identity: a chat-scoped model override wins over the session's own record.
    """
    from .cache_policy import resolve_cache_policy

    model_fields = _chat_model_fields(raw)
    provider = model_fields.get("effective_provider") or safe_assignment_text(
        raw.get("provider"), limit=220
    ) or None
    model = model_fields.get("effective_model") or safe_assignment_text(
        raw.get("model"), limit=220
    ) or None
    policy = resolve_cache_policy(
        provider=provider,
        model=model,
        api_mode=safe_assignment_text(raw.get("api_mode"), limit=60) or None,
        base_url=safe_assignment_text(raw.get("base_url"), limit=400) or None,
    )
    return policy.as_snapshot_fields()


def _chat_model_fields(raw: dict[str, Any]) -> dict[str, Any]:
    model_config = _model_config(raw.get("model_config"))
    override = model_config.get(_CHAT_MODEL_OVERRIDE_CONFIG_KEY)
    if not isinstance(override, dict):
        override = {}
    provider = safe_assignment_text(override.get("provider"), limit=220) or None
    model = safe_assignment_text(override.get("model"), limit=220) or None
    fallback_provider = safe_assignment_text(raw.get("provider"), limit=220) or None
    fallback_model = safe_assignment_text(raw.get("model"), limit=220) or None
    if not (provider or model or fallback_provider or fallback_model):
        return {}
    return {
        "chat_provider": provider,
        "chat_model": model,
        "chat_model_scope": "mission_control_chat_session" if provider or model else None,
        "chat_model_is_default": not bool(provider or model),
        "effective_provider": provider or fallback_provider,
        "effective_model": model or fallback_model,
    }


def _model_config(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except Exception:
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


def _token_usage_fields(raw: dict[str, Any]) -> dict[str, int]:
    input_tokens = _safe_int(raw.get("input_tokens"))
    output_tokens = _safe_int(raw.get("output_tokens"))
    total_tokens = _safe_int(raw.get("total_tokens"))
    if total_tokens == 0 and (input_tokens or output_tokens):
        total_tokens = input_tokens + output_tokens
    # Cache split (Launcher contract): ``input_tokens`` is already the UNCACHED,
    # full-price input (canonical usage subtracts cache reads/writes; see
    # agent/usage_pricing.CanonicalUsage). Forwarding the cache buckets lets the
    # Launcher show a cache hit % and a full-price count so operators can tell a
    # warm cache from a stale one that is being re-billed at full rate. The
    # session DB already accumulates these columns per API call — this projection
    # simply stops dropping them at the snapshot boundary.
    cache_read_tokens = _safe_int(raw.get("cache_read_tokens"))
    cache_write_tokens = _safe_int(raw.get("cache_write_tokens"))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
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
    assistant_client_message_ids: set[str] = set()
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
        client_message_id = safe_assignment_text(
            raw.get("platform_message_id") or raw.get("client_message_id"),
            limit=240,
        )
        if role == "agent" and client_message_id:
            assistant_client_message_ids.add(client_message_id)
        curated = _curate_chat_message_text(
            role, raw.get("content") or raw.get("text")
        )
        if not curated:
            continue
        text, status = _safe_display_body_text(
            curated,
            fallback="Message hidden by redaction boundary",
            limit=PERSONA_CHAT_MESSAGE_TEXT_LIMIT,
        )
        if not text:
            continue
        if status == "redacted":
            redacted = True
        row = {
            "id": safe_assignment_text(raw.get("id"), limit=120)
            or f"{session_id}:{index}",
            "role": role,
            "text": text,
            "timestamp": _iso_timestamp(
                raw.get("created_at")
                or raw.get("timestamp")
                or raw.get("time")
                or raw.get("updated_at")
            ),
            "redaction_status": status,
        }
        # PRE-C8 RESIDUE PATH: acks stopped entering SessionDB with C8 (they
        # are a presentation-only `turn.ack` stream frame now), but rows
        # persisted before that still carry the finish_reason marker
        # (archive-never-delete). Keep re-emitting the typed kind so the
        # Launcher's persisted-row render path can keep suppressing them
        # structurally. New turns can never take this branch.
        is_pre_trace_ack = role == "agent" and (
            safe_assignment_token(raw.get("finish_reason"))
            == PERSONA_PRE_TRACE_ACK_FINISH_REASON
        )
        if is_pre_trace_ack:
            row["kind"] = PERSONA_PRE_TRACE_ACK_KIND
        # RELAY SENDER ATTRIBUTION: an incoming role="user" row persisted by the
        # agent_chat_send relay lane carries the sending agent's identity in
        # finish_reason (relay_from:<persona>:<instance>) — the same typed-marker
        # -in-finish_reason precedent as the pre_trace_ack rows above. Surface it
        # as a typed kind + sender fields so the conversation projection can
        # attribute the message to the sending AGENT rather than the operator.
        # Operator/CLI sends (finish_reason=None) parse to None → skipped, so the
        # operator row is byte-identical to today.
        if role == "operator":
            relay_sender = parse_relay_sender_marker(raw.get("finish_reason"))
            if relay_sender is not None:
                row["kind"] = PERSONA_RELAYED_MESSAGE_KIND
                row["relay_sender_persona_id"] = relay_sender.persona_id
                row["relay_sender_instance_id"] = relay_sender.instance_id
        if client_message_id:
            row["client_message_id"] = client_message_id
            # C8 ordering key: the turn anchor is the client_message_id; the
            # intra-turn position is stamped HERE (one authority) — the
            # operator message opens its turn, the recorded reply closes it.
            # Elements between them carry the emitter's 1..N seq. Pre-C8 ack
            # rows carry no anchor and stay on the fallback order.
            if role == "operator":
                row["turn_seq"] = TURN_SEQ_OPERATOR
            elif role == "agent" and not is_pre_trace_ack:
                row["turn_seq"] = TURN_SEQ_TERMINAL
        if role == "agent" and client_message_id:
            elements = mission_chat_turn_elements(
                session_id=session_id,
                client_message_id=client_message_id,
            )
            if elements:
                row["turn_elements"] = elements
        rows.append(row)
    rows.extend(
        _interrupted_turn_rows(
            session_id=session_id,
            assistant_client_message_ids=assistant_client_message_ids,
        )
    )
    rows = _ordered_message_rows(rows)
    rows = rows[-_bounded_message_tail(limit):]
    return rows, "redacted" if redacted else "safe"


def _interrupted_turn_rows(
    *,
    session_id: str,
    assistant_client_message_ids: set[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in mission_chat_turn_records(session_id=session_id):
        if safe_assignment_token(record.get("state")) != "interrupted":
            continue
        client_message_id = safe_assignment_text(record.get("client_message_id"), limit=240)
        if not client_message_id or client_message_id in assistant_client_message_ids:
            continue
        turn_id = safe_assignment_token(record.get("turn_id")) or safe_assignment_token(client_message_id)
        if not turn_id:
            continue
        rows.append(
            {
                "id": f"{session_id}:turn-interrupted:{client_message_id}",
                "role": "system",
                # Typed marker: downstream projections (operator conversation,
                # Mission Control tiles) key on this instead of matching text.
                "kind": "turn_interrupted",
                "text": "Agent turn interrupted before a reply was recorded. Retry the message to run a fresh turn.",
                "timestamp": _iso_timestamp(record.get("updated_at")),
                "redaction_status": "safe",
                "client_message_id": client_message_id,
                "turn_id": turn_id,
                # C8 ordering key: the interrupt marker is the turn's terminal
                # row (a turn has a reply or an interrupt, never both).
                "turn_seq": TURN_SEQ_TERMINAL,
            }
        )
    return rows


def _ordered_message_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # C8: ONE ordering authority. Rows carrying the turn key (anchor =
    # client_message_id, position = turn_seq) sort by that key inside their
    # turn — the operator row first, elements by emitter seq, the terminal
    # reply/interrupt last — regardless of clock skew between SessionDB stamps
    # and turn-store settle times (the F17 reorder seam). Rows that predate the
    # key keep the pre-C8 fallback: timestamp order with the original index as
    # tie-breaker, a row without a parseable timestamp inheriting the preceding
    # row's so it holds its transcript position instead of front-loading.
    fallback: list[tuple[str, int]] = []
    last_timestamp = ""
    for index, row in enumerate(rows):
        timestamp = str(row.get("timestamp") or "") or last_timestamp
        last_timestamp = timestamp
        fallback.append((timestamp, index))
    return order_transcript_rows(
        rows,
        # Token-normalized so the anchor is byte-equal to the `turn_id` the
        # emitter/turn store mint from the same client_message_id.
        anchor=lambda row: safe_assignment_token(row.get("client_message_id")) or None,
        turn_seq=lambda row: row.get("turn_seq")
        if isinstance(row.get("turn_seq"), int)
        else None,
        fallback_key=lambda _row, index: fallback[index],
    )


def _iso_timestamp(value: Any) -> str | None:
    """Normalize SessionDB timestamps to the same ISO-8601 ``Z`` form as traces.

    SessionDB stores message timestamps as epoch-seconds floats (``time.time()``),
    while harness-trace rows carry ISO strings (``Event.ts`` via ``to_jsonable``).
    The Launcher merges the two channels by parsing each ``ts`` with
    ``DateTime.tryParse`` and orders them — an epoch float is unparseable there, so
    without this the curated rows lose their time and the trace block jumps
    ahead of them. Project message and session timestamps in one comparable UTC
    format, and never pass raw unparseable values through the snapshot contract.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        return None
    from datetime import datetime, timezone

    def _format(moment: datetime) -> str:
        return moment.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return _format(moment)
    if isinstance(value, (int, float)):
        epoch = float(value)
        if epoch > 1e12:  # tolerate millisecond clocks
            epoch /= 1000.0
        try:
            return _format(datetime.fromtimestamp(epoch, tz=timezone.utc))
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            epoch = float(text)
        except ValueError:
            pass
        else:
            if epoch > 1e12:  # tolerate millisecond clocks
                epoch /= 1000.0
            try:
                return _format(datetime.fromtimestamp(epoch, tz=timezone.utc))
            except (OverflowError, OSError, ValueError):
                return None
        parse_text = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(parse_text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return _format(parsed)
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
        "task.transition": "progress",
        "persona_assignment.created": "assignment_created",
        "persona_assignment.closed": "assignment_closed",
    }.get(event_type)
    if trace_event is None:
        return None

    tool_name = _safe_trace_text(payload.get("tool_name") or payload.get("tool"), limit=120)
    summary = _trace_summary(event_type, payload)
    status = _safe_trace_text(payload.get("status") or payload.get("to") or payload.get("exit_code"), limit=80)
    files = _safe_trace_file_labels(payload.get("changed_files") or payload.get("files_touched"))
    turn_id = safe_assignment_text(
        getattr(event, "turn_id", None) or payload.get("turn_id"), limit=160
    )
    return {
        "kind": "harness_trace",
        "task_id": safe_assignment_text(getattr(event, "task_id", None), limit=160),
        "persona_id": safe_assignment_token(getattr(event, "persona_id", None)) or "unknown",
        "run_id": safe_assignment_text(getattr(event, "run_id", None), limit=160),
        "turn_id": turn_id,
        # C8 ordering key: turn-anchored trace content sits in the content band
        # (after the turn's operator row, before its terminal reply) so the
        # launcher's history+trace merge sorts on the key, never on the skew
        # between SessionDB stamps and this trace clock (F17).
        **({"turn_seq": TURN_SEQ_CONTENT} if turn_id else {}),
        "stage_id": _safe_trace_text(payload.get("stage_id"), limit=120),
        "event": trace_event,
        "tool_name": tool_name,
        "summary": summary,
        "files": files,
        "status": status,
        "ts": getattr(event, "ts", None),
        # Operator-console detail lane (Mission Control only): real command,
        # tool target, bounded output tail, and full changed paths — the
        # per-line secret scrub already ran at the progress sink. Key names
        # mirror what the launcher's trace item parser already reads.
        "command": _safe_trace_operator_line(
            payload.get("command_full") or payload.get("command_label"), limit=500
        ),
        # Per-step reasoning from the thinking callback. "_thinking" is the
        # legacy placeholder some historical events recorded — never content.
        "reasoning_summary": (
            None
            if payload.get("reasoning_summary") == "_thinking"
            else _safe_trace_operator_line(payload.get("reasoning_summary"), limit=500)
        ),
        "target": _safe_trace_operator_line(payload.get("target_label"), limit=300),
        # First-class agent-to-agent dispatch (G2): structured target persona +
        # the FULL order, carried straight from the agent_chat_send progress
        # payload. dispatch_order keeps its newline structure (block scrub), so
        # the console renders the whole briefing, not the 90-char target excerpt.
        "dispatch_target": _safe_trace_operator_line(payload.get("dispatch_target"), limit=120),
        "dispatch_order": _safe_trace_operator_block(payload.get("dispatch_order"), limit=1500),
        "detail": _safe_trace_operator_line(payload.get("detail"), limit=500),
        "output": _safe_trace_operator_block(payload.get("output"), limit=1600),
        "paths": _safe_trace_operator_paths(payload.get("changed_paths")),
        "duration_ms": _safe_trace_int(payload.get("duration_ms")),
        "exit_code": _safe_trace_int(payload.get("exit_code")),
        "skill_id": _safe_trace_text(payload.get("skill_name"), limit=120),
        "assignment_id": _safe_trace_text(payload.get("assignment_id"), limit=160),
        "persona_instance_id": _safe_trace_text(payload.get("persona_instance_id"), limit=160),
        "title": _safe_trace_text(payload.get("title"), limit=240),
        "message": _safe_trace_text(payload.get("message"), limit=1200),
        "repo": _safe_trace_text(payload.get("repo"), limit=160),
        "affected_paths": _safe_trace_file_labels(payload.get("affected_paths")),
        "proof_targets": _safe_trace_list_text(payload.get("proof_targets"), limit=240),
        "acceptance": _safe_trace_list_text(payload.get("acceptance"), limit=500),
        "non_goals": _safe_trace_list_text(payload.get("non_goals"), limit=500),
        "allowed_decisions": _safe_trace_list_text(payload.get("allowed_decisions"), limit=80),
    }


def _first_safe_trace_text(*values: Any, limit: int) -> str | None:
    for value in values:
        safe = _safe_trace_text(value, limit=limit)
        if safe:
            return safe
    return None


def _trace_summary(event_type: str, payload: dict[str, Any]) -> str | None:
    if event_type.startswith("run.tool."):
        return _first_safe_trace_text(
            payload.get("command_label"),
            payload.get("file_summary"),
            payload.get("patch_summary"),
            payload.get("code_summary"),
            payload.get("summary"),
            limit=500,
        )
    if event_type == "run.progress":
        summary = _safe_trace_text(payload.get("summary"), limit=500)
        if summary in {"Run progress update.", "Run progress update"}:
            return None
        return summary
    return _first_safe_trace_text(
        payload.get("reason") if event_type == "task.transition" else None,
        payload.get("summary"),
        payload.get("patch_summary"),
        payload.get("code_summary"),
        payload.get("command_label"),
        payload.get("file_summary"),
        limit=500,
    )


def _safe_trace_text(value: Any, *, limit: int) -> str | None:
    text = safe_assignment_text(value, limit=limit)
    if not text:
        return None
    if _SECRET_RE.search(text) or _looks_pathish(text):
        return None
    return text


def _safe_trace_operator_line(value: Any, *, limit: int) -> str | None:
    """Operator-console single line: paths allowed, secrets blocked, bounded."""

    text = " ".join(str(value or "").strip().split())
    if not text or _SECRET_RE.search(text):
        return None
    return f"{text[: limit - 1]}…" if len(text) > limit else text


def _safe_trace_operator_block(value: Any, *, limit: int) -> str | None:
    """Operator-console multi-line block (command output): keeps line structure,
    redacts secret-bearing lines, tail-bounded."""

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return None
    lines = [
        "[redacted line — contained a secret]" if _SECRET_RE.search(line) else line
        for line in text.split("\n")
    ]
    text = "\n".join(lines)
    if len(text) > limit:
        text = f"…(earlier output truncated)…\n{text[-limit:]}"
    return text


def _safe_trace_operator_paths(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    paths: list[str] = []
    for item in value:
        text = " ".join(str(item or "").strip().split()).replace("\\", "/")
        if not text or _SECRET_RE.search(text):
            continue
        if len(text) > 200:
            text = f"…{text[-199:]}"
        if text not in paths:
            paths.append(text)
        if len(paths) >= 12:
            break
    return paths


def _safe_trace_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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


def _safe_trace_list_text(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = safe_assignment_text(item, limit=limit)
        if not text or _SECRET_RE.search(text):
            continue
        items.append(text)
    return items[:12]


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
        text = _safe_chat_body_text(content, limit=PERSONA_CHAT_MESSAGE_TEXT_LIMIT)
        if not text or text.startswith("{"):
            # Empty assistant turn or an unparseable raw dict — not presentable.
            return None
        return text
    if role == "operator":
        text = _safe_chat_body_text(content, limit=PERSONA_CHAT_MESSAGE_TEXT_LIMIT)
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
        if redaction_observe_enabled():
            return _mask_secret_lines(text, limit=limit), "would_redact"
        return redacted_fallback or fallback, "redacted"
    return text, "safe"


def _safe_display_body_text(
    value: Any,
    *,
    fallback: str,
    limit: int,
    redacted_fallback: str | None = None,
) -> tuple[str, str]:
    text = _safe_chat_body_text(value, limit=limit)
    if not text:
        return fallback, "safe"
    if _SECRET_RE.search(text):
        if redaction_observe_enabled():
            return _mask_secret_lines(text, limit=limit), "would_redact"
        return redacted_fallback or fallback, "redacted"
    return text, "safe"


def _mask_secret_lines(value: str, *, limit: int) -> str:
    lines = [
        "[redacted line — contained a secret]" if _SECRET_RE.search(line) else line
        for line in str(value or "").split("\n")
    ]
    text = "\n".join(lines).strip()
    return text[:limit].rstrip()


def _safe_chat_body_text(value: Any, *, limit: int) -> str:
    text = str(value or "").replace("\x00", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    normalized = "\n".join(lines).strip()
    normalized = re.sub(r"\n{4,}", "\n\n\n", normalized)
    return normalized[:limit].rstrip()


def _safe_int(value: Any) -> int:
    try:
        parsed = int(value)
    except Exception:
        return 0
    return max(parsed, 0)
