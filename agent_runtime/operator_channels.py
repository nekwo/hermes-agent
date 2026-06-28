from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from .models import PersonaInstance
from .persona_assignments import persona_instance_id_for, safe_assignment_text, safe_assignment_token
from .persona_chat_history import _canonical_persona_id

OPERATOR_CHANNELS_SCHEMA_VERSION = 1

_CHAT_INSTANCE_MODES = {"chat", "free_floating"}


def operator_channel_summary(
    *,
    persona_instances: Iterable[PersonaInstance],
    persona_chat_history: list[dict[str, Any]],
    persona_chat_trace: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project the Agent Console's single render contract.

    Raw persona instances, curated chat history, and tool trace are useful
    diagnostics, but the Launcher console must not have to join them in widget
    code. This projection owns that join and emits loud warnings when the raw
    sources disagree.
    """

    channels: dict[str, _OperatorChannelBuilder] = {}
    by_session: dict[str, _OperatorChannelBuilder] = {}
    by_instance: dict[str, _OperatorChannelBuilder] = {}

    for instance in persona_instances:
        key = _channel_key_for_instance(instance)
        builder = channels.get(key)
        if builder is None:
            builder = _OperatorChannelBuilder(key)
            channels[key] = builder
        builder.add_instance(instance)
        session_id = _safe_session(getattr(instance, "session_id", None))
        instance_id = _safe_instance_id(instance)
        if session_id:
            by_session[session_id] = builder
        if instance_id:
            by_instance[instance_id] = builder

    for row in persona_chat_history:
        session_id = _safe_session(row.get("session_id"))
        instance_id = safe_assignment_text(row.get("persona_instance_id"), limit=160)
        builder = by_session.get(session_id or "") or by_instance.get(instance_id or "")
        if builder is None:
            key = f"session:{session_id}" if session_id else f"history:{instance_id or len(channels)}"
            builder = channels.setdefault(key, _OperatorChannelBuilder(key))
            if session_id:
                by_session[session_id] = builder
            builder.warn(
                "history_without_instance",
                "chat history row had no matching persona instance",
                entity_id=session_id or instance_id,
            )
        builder.add_history(row)

    for row in persona_chat_trace:
        session_id = _safe_session(row.get("session_id"))
        instance_id = safe_assignment_text(row.get("persona_instance_id"), limit=160)
        builder = by_session.get(session_id or "") or by_instance.get(instance_id or "")
        if builder is None:
            key = f"session:{session_id}" if session_id else f"trace:{instance_id or len(channels)}"
            builder = channels.setdefault(key, _OperatorChannelBuilder(key))
            if session_id:
                by_session[session_id] = builder
            builder.warn(
                "trace_without_instance",
                "trace row had no matching persona instance",
                entity_id=session_id or instance_id,
            )
        builder.add_trace(row)

    return [
        channel
        for channel in (builder.build() for builder in channels.values())
        if channel is not None
    ]


class _OperatorChannelBuilder:
    def __init__(self, key: str):
        self.key = key
        self.instances: list[PersonaInstance] = []
        self.history_rows: list[dict[str, Any]] = []
        self.trace_rows: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []

    def add_instance(self, instance: PersonaInstance) -> None:
        self.instances.append(instance)

    def add_history(self, row: dict[str, Any]) -> None:
        self.history_rows.append(row)

    def add_trace(self, row: dict[str, Any]) -> None:
        self.trace_rows.append(row)

    def warn(self, code: str, detail: str, *, entity_id: str | None = None) -> None:
        warning: dict[str, Any] = {"code": code, "detail": detail}
        if entity_id:
            warning["entity_id"] = entity_id
        self.warnings.append(warning)

    def build(self) -> dict[str, Any] | None:
        history = _latest_history(self.history_rows)
        trace = _merged_trace(self.trace_rows)
        canonical = _canonical_instance(self.instances, history=history)
        persona_id = _first_text(
            getattr(canonical, "persona_id", None) if canonical is not None else None,
            history.get("persona_id") if history else None,
            trace.get("persona_id") if trace else None,
        )
        persona_id = _canonical_persona_id(persona_id) or persona_id or "unknown"
        canonical_id = _first_text(
            getattr(canonical, "id", None) if canonical is not None else None,
            history.get("persona_instance_id") if history else None,
            trace.get("persona_instance_id") if trace else None,
            persona_instance_id_for(persona_id),
        )
        session_id = _first_text(
            history.get("session_id") if history else None,
            trace.get("session_id") if trace else None,
            getattr(canonical, "session_id", None) if canonical is not None else None,
        )
        if canonical is None and history is None and trace is None:
            return None

        source_instance_ids = sorted(
            {
                item
                for item in [
                    *(_safe_instance_id(instance) for instance in self.instances),
                    *(
                        safe_assignment_text(row.get("persona_instance_id"), limit=160)
                        for row in self.history_rows
                    ),
                    *(
                        safe_assignment_text(row.get("persona_instance_id"), limit=160)
                        for row in self.trace_rows
                    ),
                ]
                if item
            }
        )
        warnings = list(self.warnings)
        if len(source_instance_ids) > 1:
            warnings.append(
                {
                    "code": "duplicate_instances_same_channel",
                    "detail": "multiple persona instances projected to one operator channel",
                    "entity_ids": source_instance_ids,
                }
            )
        if history is None and session_id:
            warnings.append(
                {
                    "code": "session_without_history",
                    "detail": "operator channel has a session id but no curated chat history row",
                    "entity_id": session_id,
                }
            )
        task_id = _first_text(
            getattr(canonical, "current_task_id", None) if canonical is not None else None,
            history.get("task_id") if history else None,
            trace.get("task_id") if trace else None,
        )
        if trace is None and (history is None or task_id):
            warnings.append(
                {
                    "code": "trace_empty",
                    "detail": "operator channel has no tool/progress trace rows",
                }
            )

        entries = list(trace.get("entries") or []) if trace else []
        channel_id = f"{persona_id}::{session_id or canonical_id}"
        return {
            "schema_version": OPERATOR_CHANNELS_SCHEMA_VERSION,
            "channel_id": channel_id,
            "persona_id": persona_id,
            "persona_instance_id": canonical_id,
            "session_id": session_id,
            "task_id": task_id,
            "goal_id": _first_text(
                getattr(canonical, "goal_id", None) if canonical is not None else None,
                history.get("goal_id") if history else None,
            ),
            "display_name": _first_text(
                getattr(canonical, "display_name", None) if canonical is not None else None,
                _display_name_from_history(history),
                persona_id,
            ),
            "state": safe_assignment_token(getattr(canonical, "state", None)) if canonical is not None else "unknown",
            "mode": safe_assignment_token(getattr(canonical, "mode", None)) if canonical is not None else None,
            "source_instance_ids": source_instance_ids,
            "history": history,
            "trace": trace,
            "message_count": int(history.get("message_count") or len(history.get("messages") or [])) if history else 0,
            "trace_count": len(entries),
            "tool_trace_count": len([entry for entry in entries if entry.get("tool_name")]),
            "warnings": warnings,
        }


def _channel_key_for_instance(instance: PersonaInstance) -> str:
    mode = safe_assignment_token(getattr(instance, "mode", None))
    session_id = _safe_session(getattr(instance, "session_id", None))
    persona_id = _canonical_persona_id(getattr(instance, "persona_id", None)) or "unknown"
    if session_id and mode in _CHAT_INSTANCE_MODES:
        return f"session:{session_id}"
    task_id = safe_assignment_text(getattr(instance, "current_task_id", None), limit=160)
    if task_id:
        return f"task:{task_id}:{persona_id}:{_safe_instance_id(instance)}"
    return f"instance:{_safe_instance_id(instance) or persona_instance_id_for(persona_id)}"


def _canonical_instance(
    instances: list[PersonaInstance],
    *,
    history: dict[str, Any] | None,
) -> PersonaInstance | None:
    if not instances:
        return None
    history_instance = safe_assignment_text(
        (history or {}).get("persona_instance_id"), limit=160
    )
    if history_instance:
        for instance in instances:
            if _safe_instance_id(instance) == history_instance:
                return instance
    canonical_profile = [
        instance
        for instance in instances
        if (_safe_instance_id(instance) or "").startswith("personainst_profile_")
    ]
    if canonical_profile:
        return _newest_instance(canonical_profile)
    return _newest_instance(instances)


def _newest_instance(instances: list[PersonaInstance]) -> PersonaInstance:
    return sorted(instances, key=_instance_recency, reverse=True)[0]


def _instance_recency(instance: PersonaInstance) -> tuple[int, str]:
    for value in (
        getattr(instance, "updated_at", None),
        getattr(instance, "last_heartbeat_at", None),
    ):
        parsed = _parse_time(value)
        if parsed is not None:
            return (1, parsed.isoformat())
    return (0, _safe_instance_id(instance) or "")


def _latest_history(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    return sorted(rows, key=lambda row: _row_recency(row), reverse=True)[0]


def _row_recency(row: dict[str, Any]) -> tuple[int, str]:
    for key in ("updated_at", "created_at"):
        parsed = _parse_time(row.get(key))
        if parsed is not None:
            return (1, parsed.isoformat())
    return (0, str(row.get("session_id") or ""))


def _merged_trace(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    first = rows[0]
    entries_by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        for entry in list(row.get("entries") or []):
            if not isinstance(entry, dict):
                continue
            entries_by_key[_trace_entry_key(entry)] = entry
    entries = sorted(entries_by_key.values(), key=_trace_entry_sort_key)
    return {
        "persona_instance_id": first.get("persona_instance_id"),
        "persona_id": first.get("persona_id"),
        "task_id": first.get("task_id"),
        "session_id": first.get("session_id"),
        "entries": entries,
    }


def _trace_entry_key(entry: dict[str, Any]) -> str:
    return "|".join(
        str(entry.get(key) or "")
        for key in ("ts", "event", "tool_name", "summary", "run_id", "status")
    )


def _trace_entry_sort_key(entry: dict[str, Any]) -> tuple[int, str]:
    parsed = _parse_time(entry.get("ts"))
    if parsed is not None:
        return (1, parsed.isoformat())
    return (0, str(entry.get("ts") or ""))


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _safe_instance_id(instance: PersonaInstance) -> str | None:
    return safe_assignment_text(getattr(instance, "id", None), limit=160)


def _safe_session(value: Any) -> str | None:
    return safe_assignment_text(value, limit=200) or None


def _display_name_from_history(history: dict[str, Any] | None) -> str | None:
    title = safe_assignment_text((history or {}).get("title"), limit=120)
    if title and title.lower().endswith(" chat"):
        return title[:-5].strip() or None
    return None


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = safe_assignment_text(value, limit=240)
        if text:
            return text
    return None
