from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable

from .models import PersonaInstance
from .persona_assignments import persona_instance_id_for, safe_assignment_text, safe_assignment_token
from .redaction import TEXT_SECRET_ASSIGNMENT_RE
from .run_budget import ACCOUNTING_KEY as RUN_BUDGET_ACCOUNTING_KEY
from .persona_chat_history import (
    _canonical_persona_id,
    canonical_persona_chat_turn_id,
    logical_persona_chat_client_message_id,
    PERSONA_HARNESS_DELIVERY_KIND,
    PERSONA_PRE_TRACE_ACK_KIND,
    PERSONA_RELAYED_MESSAGE_KIND,
    PERSONA_TURN_BUDGET_EXHAUSTED_KIND,
    PERSONA_TURN_INTERRUPTED_KIND,
)
from .relay_policy import HARNESS_DELIVERY_UNKNOWN_STATE
from .transcript_order import TURN_SEQ_CONTENT, order_transcript_rows

OPERATOR_CHANNELS_SCHEMA_VERSION = 1
# v2: goal-run turn flow — thinking_summary / turn / tool_call / turns_collapsed
# message kinds projected from run summaries + the task trace lane, so a live
# goal reads as a conversation instead of a lone goal_input bubble.
OPERATOR_CONVERSATION_SCHEMA_VERSION = 2

# Hard per-channel message budget. Flow kinds (turn/tool/thinking/progress) are
# trimmed oldest-first past this cap; operator/reply/proof/blocker/handoff/final
# and goal_input are protected. A turns_collapsed marker records the trim.
_CONVERSATION_MESSAGE_CAP = 200
_CONVERSATION_TRIMMABLE_KINDS = {"thinking_summary", "turn", "tool_call", "agent_update"}
_TOOL_OK_STATUSES = {"passed", "ok", "completed", "success", "succeeded", "done"}
_TOOL_FAILED_STATUSES = {"failed", "error", "blocked", "crashed", "timeout"}

_CHAT_INSTANCE_MODES = {"chat", "free_floating"}

# ── terminal turn markers, as the conversation contract renders them ─────────
#
# Keyed on the marker kind ``persona_chat_history`` synthesizes (see
# ``TERMINAL_TURN_MARKERS`` there — that table is the producer, this one the
# presenter). ``status`` and ``display_title`` are the wire values the Launcher
# adapter already reads (``mission_agent_chat_adapter.dart``:
# ``_turnInterruptedFlowMessage`` / ``_budgetExhaustedFlowMessage``), so no
# launcher change is required to render either marker.
_TERMINAL_TURN_MARKER_PRESENTATION = {
    PERSONA_TURN_INTERRUPTED_KIND: {
        "status": "interrupted",
        "display_title": "Turn interrupted",
    },
    PERSONA_TURN_BUDGET_EXHAUSTED_KIND: {
        "status": "budget_exhausted",
        "display_title": "Wall budget reached",
    },
}
# The status a still-``running`` tool_call is settled to when its turn ended.
# ``interrupted`` describes the CALL — it was cut off and will never finish —
# and is the only settled-tool vocabulary the Launcher's trace renderer already
# knows (``mission_trace_content_renderer.dart``: interrupted|cancelled|
# canceled|aborted → stop glyph). The turn-level reason (graceful wall-budget
# checkpoint vs. a killed turn) is carried separately as ``settled_reason``, so
# a settled call never has to lie about WHY to stop spinning.
_SETTLED_TOOL_CALL_STATUS = "interrupted"

# Single-homed in ``agent_runtime.redaction`` — see the header there for the
# JSON blind spot every local spelling shared. Detection only here (a matching
# line is dropped whole), so the shared pattern's group(2) is inert.
_SECRET_RE = TEXT_SECRET_ASSIGNMENT_RE
_TELEMETRY_SUMMARY_RE = re.compile(
    r"(?i)\b("
    r"agent init|provider client|provider responses|provider stream|provider call|"
    r"agent thinking process|agent decision process|"
    r"persona runtime|profile conversation call|profile runtime|profile agent|profile result normalize|"
    r"profile budget checks|conversation request build|conversation provider dispatch|"
    r"conversation response validate|conversation pre api hook|context build|autonomy packet|prompt render|"
    r"api mode setup|core state setup|tool setup|session setup|memory skill setup|"
    r"final state setup|decision apply"
    r")\b"
)


def operator_channel_summary(
    *,
    persona_instances: Iterable[PersonaInstance],
    persona_chat_history: list[dict[str, Any]],
    persona_chat_trace: list[dict[str, Any]],
    accountant: Any = None,
    intentionally_omitted_history_session_ids: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Project the Agent Console's single render contract.

    Raw persona instances, curated chat history, and tool trace are useful
    diagnostics, but the Launcher console must not have to join them in widget
    code. This projection owns that join and emits loud warnings when the raw
    sources disagree.

    S47 removed the ``tasks`` parameter and the ``_TaskLookup`` it built. Both
    production callers (``snapshot.build_snapshot`` and ``status.build_status``)
    passed a ``[]`` literal, so the resolved task was permanently ``None`` and
    everything downstream of it — the synthetic goal-input message, the title
    and run-id fallbacks, the ``goal_id`` / ``task_id`` / ``updated_at``
    fallbacks — was unreachable, not optional.
    """

    omitted_history_session_ids = {
        session_id
        for item in (intentionally_omitted_history_session_ids or [])
        if (session_id := _safe_session(item))
    }
    channels: dict[str, _OperatorChannelBuilder] = {}
    by_session: dict[str, _OperatorChannelBuilder] = {}
    by_instance: dict[str, _OperatorChannelBuilder] = {}
    # instance id → display name, built once from the FULL roster so a relayed
    # message can name the sending agent (the sender may be any instance, not
    # just this channel's owner). Threaded down to the history projection as an
    # additive lookup; never re-derived per row.
    display_names: dict[str, str] = {}

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
            name = safe_assignment_text(getattr(instance, "display_name", None), limit=120)
            if name:
                display_names[instance_id] = name

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

    conversation_relationships = _operator_conversation_relationships(channels.values())

    return [
        channel
        for channel in (
            builder.build(
                accountant=accountant,
                display_names=display_names,
                omitted_history_session_ids=omitted_history_session_ids,
                conversation_relationships=conversation_relationships,
            )
            for builder in channels.values()
        )
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

    def _bound_history(self) -> dict[str, Any] | None:
        """History row for an instance's BOUND session, when present.

        ``persona.instance.open_chat`` rebinds an instance to an arbitrary
        saved session; the channel must project that binding, not whichever
        curated row happens to carry the newest timestamp. Observed live
        2026-07-07: rebinding Alice to an older chat left her channel on the
        newest session, so the Launcher console never switched chats.
        """
        bound = {
            session
            for session in (
                _safe_session(getattr(instance, "session_id", None))
                for instance in self.instances
            )
            if session
        }
        if not bound:
            return None
        matches = [
            row
            for row in self.history_rows
            if _safe_session(row.get("session_id")) in bound
        ]
        if not matches:
            return None
        return _latest_history(matches)

    def build(
        self,
        *,
        accountant: Any = None,
        display_names: dict[str, str] | None = None,
        omitted_history_session_ids: set[str] | None = None,
        conversation_relationships: dict[str, tuple[str, str | None]] | None = None,
    ) -> dict[str, Any] | None:
        history = self._bound_history() or _latest_history(self.history_rows)
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
                        if not row.get("_mirrored_to_root")
                    ),
                ]
                if item
            }
        )
        warnings = list(self.warnings)
        if _source_instance_ids_conflict(
            source_instance_ids,
            instances=self.instances,
            history_rows=self.history_rows,
            trace_rows=self.trace_rows,
        ):
            warnings.append(
                {
                    "code": "duplicate_instances_same_channel",
                    "detail": "multiple persona instances projected to one operator channel",
                    "entity_ids": source_instance_ids,
                }
            )
        task_id = _first_text(
            getattr(canonical, "current_task_id", None) if canonical is not None else None,
            history.get("task_id") if history else None,
            trace.get("task_id") if trace else None,
        )

        entries = list(trace.get("entries") or []) if trace else []
        channel_id = f"{persona_id}::{session_id or canonical_id}"
        root_thread_id, parent_thread_id = (conversation_relationships or {}).get(
            canonical_id,
            (channel_id, None),
        )
        goal_id = _first_text(
            getattr(canonical, "goal_id", None) if canonical is not None else None,
            history.get("goal_id") if history else None,
        )
        conversation = _conversation_contract(
            channel_id=channel_id,
            persona_id=persona_id,
            persona_instance_id=canonical_id,
            session_id=session_id,
            task_id=task_id,
            goal_id=goal_id,
            title=_first_text(
                history.get("title") if history else None,
                getattr(canonical, "current_chat_goal", None) if canonical is not None else None,
                "Mission run",
            )
            or "Mission run",
            state=safe_assignment_token(getattr(canonical, "state", None)) if canonical is not None else "unknown",
            history=history,
            trace=trace,
            accountant=accountant,
            display_names=display_names,
            root_thread_id=root_thread_id,
            parent_thread_id=parent_thread_id,
        )
        if _turn_identity_dropped(entries, conversation.get("messages") or []):
            warnings.append(
                {
                    "code": "operator_conversations.turn_identity_dropped",
                    "detail": "trace entries carry turn_id but projected tool/thinking conversation rows dropped it",
                    "entity_id": channel_id,
                }
            )
        if _turn_identity_mismatched(conversation.get("messages") or []):
            warnings.append(
                {
                    "code": "operator_conversations.turn_identity_mismatched",
                    "detail": "a projected terminal reply turn_id disagrees with its typed assistant client_message_id",
                    "entity_id": channel_id,
                }
            )
        # session_without_history and trace_empty are both evaluated AFTER the
        # conversation is built: a channel whose goal turns already flow as
        # canonical messages is not an empty channel, even when the legacy trace
        # lane happens to be null.
        conversation_messages = conversation.get("messages") or []
        has_flow_messages = any(
            message.get("kind") in {"thinking_summary", "turn", "tool_call"}
            for message in conversation_messages
        )
        # ONE shared predicate for the NEWBORN channel state: a freshly-created
        # chat that has a session id but into which nothing has flowed yet — no
        # curated history row, no trace, no task binding, and zero projected
        # conversation messages (operator rows included). A newborn is neither a
        # projection loss nor an empty-trace anomaly; both warnings stay silent
        # until real content arrives (live 2026-07-18: creating a fresh
        # neko_supervisor chat surfaced two false-positive contract warnings).
        is_newborn_channel = (
            bool(session_id)
            and history is None
            and trace is None
            and task_id is None
            and not conversation_messages
        )
        # session_without_history is the genuine projection-loss signal: real
        # content flowed (conversation messages or a trace) but no curated
        # history row backs it. A newborn — nothing has flowed yet — stays silent.
        if (
            history is None
            and session_id
            and not is_newborn_channel
            and session_id not in (omitted_history_session_ids or set())
        ):
            warnings.append(
                {
                    "code": "session_without_history",
                    "detail": "operator channel has a session id but no curated chat history row",
                    "entity_id": session_id,
                }
            )
        # A dormant instance channel — no session, no history, no task binding,
        # and an empty conversation — has never had anything to trace; flagging
        # it would emit a permanent false-positive parity warning for every
        # idle seeded/probe persona instance.
        dormant_channel = (
            history is None
            and session_id is None
            and task_id is None
            and not conversation_messages
        )
        if (
            trace is None
            and not has_flow_messages
            and (history is None or task_id)
            and not dormant_channel
            and not is_newborn_channel
        ):
            warnings.append(
                {
                    "code": "trace_empty",
                    "detail": "operator channel has no tool/progress trace rows",
                }
            )
        return {
            "schema_version": OPERATOR_CHANNELS_SCHEMA_VERSION,
            "channel_id": channel_id,
            "persona_id": persona_id,
            "persona_instance_id": canonical_id,
            "session_id": session_id,
            "task_id": task_id,
            "goal_id": goal_id,
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
            "conversation": conversation,
            "conversation_status": conversation.get("status"),
            "message_count": int(history.get("message_count") or len(history.get("messages") or [])) if history else 0,
            "trace_count": len(entries),
            "tool_trace_count": len([entry for entry in entries if entry.get("tool_name")]),
            "warnings": warnings,
        }


def _operator_conversation_relationships(
    builders: Iterable["_OperatorChannelBuilder"],
) -> dict[str, tuple[str, str | None]]:
    """Resolve conversation ancestry from the persisted instance graph.

    ``PersonaInstance.steered_by`` is the authority.  The conversation wire has
    one parent field, so it follows the store's primary-parent convention (the
    first entry, mirrored by ``spawned_by``).  A missing/out-of-roster parent or
    a cycle degrades to a standalone thread; no persona id is promoted to root
    by convention.
    """

    instances_by_id: dict[str, PersonaInstance] = {}
    channel_by_instance_id: dict[str, str] = {}
    canonical_instances: dict[str, PersonaInstance] = {}
    for builder in builders:
        history = builder._bound_history() or _latest_history(builder.history_rows)
        trace = _merged_trace(builder.trace_rows)
        canonical = _canonical_instance(builder.instances, history=history)
        if canonical is None:
            continue
        persona_id = _first_text(
            getattr(canonical, "persona_id", None),
            history.get("persona_id") if history else None,
            trace.get("persona_id") if trace else None,
        )
        persona_id = _canonical_persona_id(persona_id) or persona_id or "unknown"
        canonical_id = _first_text(
            getattr(canonical, "id", None),
            history.get("persona_instance_id") if history else None,
            trace.get("persona_instance_id") if trace else None,
            persona_instance_id_for(persona_id),
        )
        session_id = _first_text(
            history.get("session_id") if history else None,
            trace.get("session_id") if trace else None,
            getattr(canonical, "session_id", None),
        )
        channel_id = f"{persona_id}::{session_id or canonical_id}"
        canonical_instances[canonical_id] = canonical
        for instance in builder.instances:
            instance_id = _safe_instance_id(instance)
            if not instance_id:
                continue
            instances_by_id[instance_id] = instance
            channel_by_instance_id[instance_id] = channel_id

    relationships: dict[str, tuple[str, str | None]] = {}
    for canonical_id, instance in canonical_instances.items():
        instance_id = _safe_instance_id(instance)
        if not instance_id:
            continue
        own_channel_id = channel_by_instance_id[instance_id]
        parent_ids = [
            parent_id
            for raw in list(getattr(instance, "steered_by", None) or [])
            if (parent_id := safe_assignment_text(raw, limit=160))
        ]
        primary_parent_id = parent_ids[0] if parent_ids else None
        parent_thread_id = channel_by_instance_id.get(primary_parent_id or "")
        root_thread_id = own_channel_id
        cursor = primary_parent_id
        seen = {instance_id}
        ancestry_valid = True
        while cursor:
            if cursor in seen:
                ancestry_valid = False
                break
            seen.add(cursor)
            parent_channel_id = channel_by_instance_id.get(cursor)
            parent = instances_by_id.get(cursor)
            if parent_channel_id is None or parent is None:
                ancestry_valid = False
                break
            root_thread_id = parent_channel_id
            next_parents = [
                parent_id
                for raw in list(getattr(parent, "steered_by", None) or [])
                if (parent_id := safe_assignment_text(raw, limit=160))
            ]
            cursor = next_parents[0] if next_parents else None
        if not ancestry_valid:
            root_thread_id = own_channel_id
            parent_thread_id = None
        relationships[canonical_id] = (root_thread_id, parent_thread_id)
    return relationships


def _turn_identity_dropped(entries: list[Any], messages: list[Any]) -> bool:
    for message in messages:
        if not isinstance(message, dict):
            continue
        if safe_assignment_token(message.get("kind")) not in {"tool_call", "thinking_summary"}:
            continue
        refs = message.get("refs")
        if not isinstance(refs, dict) or refs.get("source") != "persona_chat_trace":
            continue
        if safe_assignment_text(message.get("turn_id"), limit=160):
            continue
        timestamp = safe_assignment_text(message.get("timestamp"), limit=200)
        tool_name = safe_assignment_text(refs.get("tool_name"), limit=160)
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if safe_assignment_text(entry.get("ts"), limit=200) != timestamp:
                continue
            if tool_name and safe_assignment_text(entry.get("tool_name"), limit=160) != tool_name:
                continue
            if safe_assignment_text(entry.get("turn_id"), limit=160):
                return True
            break
    return False


def _turn_identity_mismatched(messages: list[Any]) -> bool:
    """Detect a projector regression without relying on reply body matching."""

    for message in messages:
        if not isinstance(message, dict):
            continue
        if safe_assignment_token(message.get("role")) != "agent":
            continue
        client_message_id = safe_assignment_text(
            message.get("client_message_id"), limit=240
        )
        expected_turn_id = canonical_persona_chat_turn_id(client_message_id)
        if (
            not expected_turn_id
            or client_message_id
            == logical_persona_chat_client_message_id(client_message_id)
        ):
            continue
        actual_turn_id = safe_assignment_token(message.get("turn_id"))
        if actual_turn_id != expected_turn_id:
            return True
    return False


def _conversation_contract(
    *,
    channel_id: str,
    persona_id: str,
    persona_instance_id: str | None,
    session_id: str | None,
    task_id: str | None,
    goal_id: str | None,
    title: str,
    state: str | None,
    history: dict[str, Any] | None,
    trace: dict[str, Any] | None,
    accountant: Any = None,
    display_names: dict[str, str] | None = None,
    root_thread_id: str | None = None,
    parent_thread_id: str | None = None,
) -> dict[str, Any]:
    # S47: the leading synthetic "Goal:" input message went with the ``task``
    # parameter — no caller could ever supply a task to mint it from.
    messages: list[dict[str, Any]] = []
    for index, row in enumerate(list((history or {}).get("messages") or [])):
        if isinstance(row, dict):
            message = _conversation_history_message(
                row,
                channel_id=channel_id,
                index=index,
                persona_id=persona_id,
                persona_instance_id=persona_instance_id,
                display_names=display_names,
            )
            if message is not None:
                messages.append(message)
    for index, entry in enumerate(list((trace or {}).get("entries") or [])):
        if isinstance(entry, dict):
            message = _conversation_trace_message(
                entry,
                channel_id=channel_id,
                index=index,
                persona_id=persona_id,
                persona_instance_id=persona_instance_id,
            )
            if message is not None:
                messages.append(message)
    messages.extend(
        _conversation_tool_call_messages(
            list((trace or {}).get("entries") or []),
            channel_id=channel_id,
            persona_id=persona_id,
            persona_instance_id=persona_instance_id,
            accountant=accountant,
        )
    )

    _settle_terminal_tool_calls(messages, history=history)

    messages = _order_conversation_messages(messages)
    messages = _dedupe_conversation_messages(messages)
    messages = _apply_conversation_cap(messages, channel_id=channel_id, accountant=accountant)
    for seq, message in enumerate(messages, start=1):
        message["seq"] = seq

    # "incomplete" is a contract breach: source rows existed but nothing
    # projected. A brand-new chat with no sources at all is simply "empty" —
    # it must NOT surface an intervention row in Mission Control.
    had_sources = bool(
        (history or {}).get("messages")
        or (trace or {}).get("entries")
    )
    if messages:
        status = "complete"
    elif had_sources:
        status = "incomplete"
    else:
        status = "empty"
    reason = None
    if status == "incomplete":
        reason = "No canonical conversation messages were projected for this operator channel."
    return {
        "schema_version": OPERATOR_CONVERSATION_SCHEMA_VERSION,
        "thread_id": channel_id,
        "goal_id": goal_id,
        "task_id": task_id,
        "owner_persona_id": persona_id,
        "persona_instance_id": persona_instance_id,
        "session_id": session_id,
        "root_thread_id": root_thread_id or channel_id,
        "parent_thread_id": parent_thread_id,
        "title": title,
        "state": state or "unknown",
        "updated_at": _latest_message_timestamp(messages) or (history or {}).get("updated_at"),
        "status": status,
        "incomplete_reason": reason,
        "messages": messages,
    }


def _settle_terminal_tool_calls(
    messages: list[dict[str, Any]],
    *,
    history: dict[str, Any] | None,
) -> None:
    """Settle still-``running`` tool_call rows of TERMINALLY-ended turns.

    A turn that ends mid-flight — killed (``turn_interrupted``) or landed at the
    wall-budget checkpoint (``budget_exhausted``) — leaves ``tool_started``
    trace entries with no finish row, so the paired tool_call projects
    ``running`` forever. The turn store's terminal marker is the truth that
    those calls will never finish; settle them at the contract source so every
    consumer stops rendering a live spinner.

    This keyed on ``turn_interrupted`` ALONE until 2026-07-26, which is exactly
    why a wall-budget turn spun in the cockpit after it was over: a second
    terminal state existed and only one marker was consumed. The recognised set
    is now driven by ``_TERMINAL_TURN_MARKER_PRESENTATION``, so a new terminal
    marker settles its tools by construction.

    Terminal turn ids come from the history SOURCE rows, not the projected
    messages, so a capped/deduped marker still settles its tools. The settled
    status is uniform (``interrupted`` — the call was cut off); ``settled_reason``
    carries WHICH terminal state ended the turn, typed, on both the message and
    its tool payload.
    """

    settled_reason_by_turn: dict[str, str] = {}
    for row in (history or {}).get("messages") or []:
        if not isinstance(row, dict):
            continue
        kind = safe_assignment_token(row.get("kind"))
        if kind not in _TERMINAL_TURN_MARKER_PRESENTATION:
            continue
        turn_id = safe_assignment_token(row.get("turn_id")) or safe_assignment_token(
            row.get("client_message_id")
        )
        if not turn_id:
            continue
        # The turn-store state the marker settled at, with the marker kind as
        # the fallback for a pre-``settled_state`` row (archive-never-delete).
        settled_reason_by_turn[turn_id] = (
            safe_assignment_token(row.get("settled_state")) or kind
        )
    if not settled_reason_by_turn:
        return
    for message in messages:
        if message.get("kind") != "tool_call" or message.get("status") != "running":
            continue
        reason = settled_reason_by_turn.get(
            safe_assignment_token(message.get("turn_id")) or ""
        )
        if reason is None:
            continue
        message["status"] = _SETTLED_TOOL_CALL_STATUS
        message["settled_reason"] = reason
        tool = message.get("tool")
        if isinstance(tool, dict):
            tool["status"] = _SETTLED_TOOL_CALL_STATUS
            tool["settled_reason"] = reason


# S47 removed ``_conversation_goal_input`` (the synthetic "Goal:" operator
# message) with the ``task`` parameter that was its only input.


def _conversation_history_message(
    row: dict[str, Any],
    *,
    channel_id: str,
    index: int,
    persona_id: str,
    persona_instance_id: str | None,
    display_names: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    text = _safe_conversation_text(row.get("text"), limit=20000)
    if not text:
        return None
    role = safe_assignment_token(row.get("role")) or "system"
    if role == "user":
        role = "operator"
    if role == "assistant":
        role = "agent"
    if role not in {"operator", "agent", "system", "proof", "blocker"}:
        role = "system"
    redaction_status = safe_assignment_token(row.get("redaction_status")) or "safe"
    if redaction_status in {"redacted", "unsafe"}:
        text = "Message hidden by redaction boundary."
    client_message_id = safe_assignment_text(row.get("client_message_id"), limit=240)
    marker_kind = safe_assignment_token(row.get("kind")) or ""
    presentation = _TERMINAL_TURN_MARKER_PRESENTATION.get(marker_kind)
    if presentation is not None:
        # Terminal turn-status marker synthesized by persona_chat_history for a
        # turn that ended without a recorded reply — killed
        # (``turn_interrupted``) or landed at the wall-budget checkpoint
        # (``budget_exhausted``). Keep the typed kind + turn identity so the
        # Launcher renders the right affordance (retry vs. graceful checkpoint)
        # and so the still-"running" tool rows of the same turn get settled.
        turn_id = canonical_persona_chat_turn_id(
            client_message_id
        ) or safe_assignment_token(row.get("turn_id"))
        message = {
            "id": safe_assignment_text(row.get("id"), limit=180) or f"{channel_id}:history:{index}",
            "seq": 0,
            "timestamp": row.get("timestamp"),
            "actor_persona_id": persona_id,
            "actor_instance_id": persona_instance_id,
            "role": "system",
            "kind": marker_kind,
            "status": presentation["status"],
            "display_title": presentation["display_title"],
            "display_text": text,
            "redaction_status": "redacted" if redaction_status in {"redacted", "unsafe"} else "safe",
            "refs": {"source": "persona_chat_history"},
        }
        settled_state = safe_assignment_token(row.get("settled_state"))
        if settled_state:
            message["settled_state"] = settled_state
        if client_message_id:
            message["client_message_id"] = client_message_id
        if turn_id:
            message["turn_id"] = turn_id
        _carry_turn_seq(message, row)
        _carry_history_run_budget(message, row)
        return message
    # A canned pre-trace ack keeps its typed kind end-to-end so the Launcher
    # collapses/drops it structurally (never as a settled reply bubble ahead of
    # the real tool run), instead of matching the tool-specific ack prose.
    is_pre_trace_ack = (
        role == "agent"
        and safe_assignment_token(row.get("kind")) == PERSONA_PRE_TRACE_ACK_KIND
    )
    # A relayed incoming message is a role="operator" row that persona_chat_history
    # tagged with the SENDING agent's identity (finish_reason marker). Attribute
    # it to that agent, but keep role="operator" so the lane semantics — and any
    # consumer that ignores the typed kind — degrade to today's operator render.
    is_relayed_message = (
        role == "operator"
        and safe_assignment_token(row.get("kind")) == PERSONA_RELAYED_MESSAGE_KIND
    )
    relay_sender_instance_id = (
        safe_assignment_text(row.get("relay_sender_instance_id"), limit=160)
        if is_relayed_message
        else None
    )
    # A dispatch delivery is a role="operator" row the HARNESS forged into the
    # sender's own thread. Same shape as the relay case and the same reason for
    # keeping role="operator" (lane semantics; a consumer that ignores the typed
    # kind degrades to today's render) — but the actor is the runtime itself,
    # not an agent, so there is no sender persona to name and none is invented.
    is_harness_delivery = (
        role == "operator"
        and safe_assignment_token(row.get("kind")) == PERSONA_HARNESS_DELIVERY_KIND
    )
    if is_harness_delivery:
        default_kind = PERSONA_HARNESS_DELIVERY_KIND
        actor_persona_id = "harness"
        actor_instance_id = None
    elif is_relayed_message:
        default_kind = PERSONA_RELAYED_MESSAGE_KIND
        actor_persona_id = (
            safe_assignment_text(row.get("relay_sender_persona_id"), limit=160) or "agent"
        )
        actor_instance_id = relay_sender_instance_id or None
    else:
        if role == "agent":
            default_kind = PERSONA_PRE_TRACE_ACK_KIND if is_pre_trace_ack else "reply"
        elif role == "operator":
            default_kind = "operator_message"
        else:
            default_kind = "system_message"
        actor_persona_id = "operator" if role == "operator" else persona_id
        actor_instance_id = None if role == "operator" else persona_instance_id
    message = {
        "id": safe_assignment_text(row.get("id"), limit=180) or f"{channel_id}:history:{index}",
        "seq": 0,
        "timestamp": row.get("timestamp"),
        "actor_persona_id": actor_persona_id,
        "actor_instance_id": actor_instance_id,
        "role": role,
        "kind": default_kind,
        "status": "delivered",
        "display_title": "",
        "display_text": text,
        "redaction_status": "redacted" if redaction_status in {"redacted", "unsafe"} else "safe",
        "refs": {"source": "persona_chat_history"},
    }
    runtime_context = row.get("runtime_context")
    if role == "operator" and isinstance(runtime_context, dict):
        message["runtime_context"] = {
            key: value
            for key in ("context_id", "revision", "delivery")
            if (value := safe_assignment_text(runtime_context.get(key), limit=200))
        }
    # The delivery's own facts, as a typed sub-block rather than loose keys: a
    # consumer needs ALL THREE to act, and a field that can go missing
    # independently of the id it belongs to eventually gets read against the
    # wrong dispatch. `notify_operator` and `state` are always present — "the
    # agent did not flag this" and "nobody knows how it ended" are answers, not
    # absences.
    #
    # `state` is why this block is not just an id: ``pending_deliveries``
    # selects ``state != running``, so a FAILED dispatch is delivered exactly
    # like a successful one and only the prose body says which. A consumer
    # without this would have to read that prose to phrase its own notification,
    # which is the sentence-matching the typed marker exists to retire.
    if is_harness_delivery:
        message["delivery"] = {
            "dispatch_id": safe_assignment_text(
                row.get("delivery_dispatch_id"), limit=200
            )
            or "",
            "notify_operator": bool(row.get("delivery_notify_operator")),
            "state": safe_assignment_token(row.get("delivery_state"))
            or HARNESS_DELIVERY_UNKNOWN_STATE,
        }
    # Name the sending agent ONLY when its instance id resolves in the roster —
    # never fabricate a name for an instance we cannot see.
    if is_relayed_message and relay_sender_instance_id:
        resolved_name = (display_names or {}).get(relay_sender_instance_id)
        if resolved_name:
            message["actor_display_name"] = resolved_name
    if role == "operator" and client_message_id:
        message["client_message_id"] = client_message_id
        message["turn_id"] = canonical_persona_chat_turn_id(client_message_id)
    if role == "agent" and client_message_id:
        message["turn_id"] = canonical_persona_chat_turn_id(
            client_message_id
        ) or safe_assignment_token(row.get("turn_id"))
        message["client_message_id"] = client_message_id
    # C8 ordering key: the history projection stamps the intra-turn position
    # (operator opens, terminal reply/interrupt closes); carry it through this
    # contract unchanged so every representation sorts on the same key.
    _carry_turn_seq(message, row)
    _carry_history_run_budget(message, row)
    return message


def _carry_turn_seq(message: dict[str, Any], row: dict[str, Any]) -> None:
    turn_seq = row.get("turn_seq")
    if isinstance(turn_seq, int) and not isinstance(turn_seq, bool):
        message["turn_seq"] = turn_seq


def _carry_history_run_budget(message: dict[str, Any], row: dict[str, Any]) -> None:
    # The turn's run_budget accounting block, decorated onto the history row by
    # persona_chat_history._carry_run_budget. Verbatim and absence-preserving:
    # the store already bounded it through run_budget.safe_accounting_block,
    # and a row without the block projects without the key so "nobody
    # accounted this turn" stays distinguishable from "nothing bounded it".
    block = row.get(RUN_BUDGET_ACCOUNTING_KEY)
    if isinstance(block, dict) and block:
        message[RUN_BUDGET_ACCOUNTING_KEY] = block


def _conversation_trace_message(
    entry: dict[str, Any],
    *,
    channel_id: str,
    index: int,
    persona_id: str,
    persona_instance_id: str | None,
) -> dict[str, Any] | None:
    event = safe_assignment_token(entry.get("event"))
    if event in {"assignment_created", "assignment_closed"}:
        return _conversation_assignment_message(
            entry,
            channel_id=channel_id,
            index=index,
            parent_persona_id=persona_id,
        )
    if event != "progress":
        return None
    if entry.get("tool_name"):
        return None
    # Per-step reasoning renders as a first-class Thinking message — this is
    # the streamed think→act loop, one message per thinking callback, distinct
    # from the per-run summary emitted by _conversation_turn_messages.
    reasoning = _safe_conversation_text(entry.get("reasoning_summary"), limit=1200)
    if reasoning:
        refs: dict[str, Any] = {"source": "persona_chat_trace"}
        for key in ("task_id", "run_id", "stage_id"):
            value = safe_assignment_text(entry.get(key), limit=160)
            if value:
                refs[key] = value
        turn_id = safe_assignment_text(entry.get("turn_id"), limit=160)
        message = {
            "id": f"{channel_id}:thinking:{refs.get('run_id', 'run')}:{index}",
            "seq": 0,
            "timestamp": entry.get("ts"),
            "actor_persona_id": safe_assignment_token(entry.get("persona_id")) or persona_id,
            "actor_instance_id": persona_instance_id,
            "role": "agent",
            "kind": "thinking_summary",
            "status": safe_assignment_token(entry.get("status")) or "running",
            "display_title": "Thinking",
            "display_text": reasoning,
            "redaction_status": "safe",
            "refs": refs,
        }
        if turn_id:
            message["turn_id"] = turn_id
            message["turn_seq"] = TURN_SEQ_CONTENT
        return message
    summary = _safe_conversation_text(
        entry.get("summary") or entry.get("rationale"),
        limit=1200,
    )
    if not summary or _TELEMETRY_SUMMARY_RE.search(summary):
        return None
    status = safe_assignment_token(entry.get("status")) or "running"
    role = "blocker" if status in {"blocked", "failed", "needs_input"} else "proof" if "proof" in summary.lower() else "agent"
    kind = "blocker" if role == "blocker" else "proof" if role == "proof" else _conversation_kind_from_status(status)
    refs: dict[str, Any] = {"source": "persona_chat_trace"}
    for key in ("task_id", "run_id", "stage_id"):
        value = safe_assignment_text(entry.get(key), limit=160)
        if value:
            refs[key] = value
    turn_id = safe_assignment_text(entry.get("turn_id"), limit=160)
    message = {
        "id": f"{channel_id}:progress:{refs.get('run_id', 'run')}:{index}",
        "seq": 0,
        "timestamp": entry.get("ts"),
        "actor_persona_id": safe_assignment_token(entry.get("persona_id")) or persona_id,
        "actor_instance_id": persona_instance_id,
        "role": role,
        "kind": kind,
        "status": status,
        "display_title": _conversation_title_for_kind(kind),
        "display_text": summary,
        "redaction_status": "safe",
        "refs": refs,
    }
    if turn_id:
        message["turn_id"] = turn_id
        message["turn_seq"] = TURN_SEQ_CONTENT
    return message


def _conversation_assignment_message(
    entry: dict[str, Any],
    *,
    channel_id: str,
    index: int,
    parent_persona_id: str,
) -> dict[str, Any] | None:
    target_persona_id = safe_assignment_token(entry.get("persona_id")) or "agent"
    title = _safe_conversation_text(entry.get("title"), limit=240)
    message = _safe_conversation_text(entry.get("message"), limit=1200)
    if not title and not message:
        return None
    parts = [f"Prompted {target_persona_id}."]
    if title:
        parts.append(f"Stage: {title}")
    if message:
        parts.append(f"Prompt: {message}")
    proof_targets = _safe_conversation_list(entry.get("proof_targets"), limit=160)
    if proof_targets:
        parts.append("Proof expected: " + "; ".join(proof_targets))
    allowed_decisions = _safe_conversation_list(entry.get("allowed_decisions"), limit=80)
    if allowed_decisions:
        parts.append("Allowed decisions: " + ", ".join(allowed_decisions))
    refs: dict[str, Any] = {"source": "persona_assignment"}
    event = safe_assignment_token(entry.get("event"))
    if event:
        refs["event"] = event
    for key in ("task_id", "stage_id", "assignment_id", "persona_instance_id", "repo"):
        value = safe_assignment_text(entry.get(key), limit=160)
        if value:
            refs[key] = value
    return {
        "id": f"{channel_id}:assignment:{refs.get('assignment_id', index)}",
        "seq": 0,
        "timestamp": entry.get("ts"),
        "actor_persona_id": parent_persona_id,
        "actor_instance_id": None,
        "target_persona_id": target_persona_id,
        "target_persona_instance_id": safe_assignment_text(entry.get("persona_instance_id"), limit=160),
        "role": "agent",
        "kind": "handoff",
        "status": "delivered",
        "display_title": "Subagent prompt",
        "display_text": "\n".join(parts),
        "redaction_status": "safe",
        "refs": refs,
    }


def _conversation_tool_call_messages(
    entries: list[Any],
    *,
    channel_id: str,
    persona_id: str,
    persona_instance_id: str | None,
    accountant: Any = None,
) -> list[dict[str, Any]]:
    """Collapse tool_started/tool_finished trace pairs into one tool_call each.

    The trace lane already carries redaction-safe tool rows; this pairs them by
    ``(run_id, tool_name)`` in timestamp order so the conversation shows one
    compact row per call — running until its finish row lands, then ok/failed
    with a duration. Ids are ``{channel}:tool:{run}:{ordinal}`` (ordinal = the
    call's index within its run), stable across polls.
    """

    messages: list[dict[str, Any]] = []
    open_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    ordinals: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        event = safe_assignment_token(entry.get("event"))
        if event not in {"tool_started", "tool_finished"}:
            continue
        turn_id = safe_assignment_text(entry.get("turn_id"), limit=160)
        real_run_id = safe_assignment_text(entry.get("run_id"), limit=160)
        bucket_id = turn_id or real_run_id or "run"
        tool_name = safe_assignment_text(entry.get("tool_name"), limit=120) or "tool"
        key = (bucket_id, tool_name)
        summary = _safe_conversation_text(entry.get("summary"), limit=1200)
        files = _safe_conversation_list(entry.get("files"), limit=200)
        if event == "tool_started":
            if accountant is not None:
                accountant.consider(1)
            ordinal = ordinals.get(bucket_id, 0)
            ordinals[bucket_id] = ordinal + 1
            message = _tool_call_message(
                channel_id=channel_id,
                persona_id=persona_id,
                persona_instance_id=persona_instance_id,
                bucket_id=bucket_id,
                run_id=real_run_id,
                turn_id=turn_id,
                ordinal=ordinal,
                tool_name=tool_name,
                status="running",
                timestamp=entry.get("ts"),
                summary=summary,
                files=files,
                stage_id=safe_assignment_text(entry.get("stage_id"), limit=160),
                task_id=safe_assignment_text(entry.get("task_id"), limit=160),
                entry=entry,
            )
            message["_started_ts"] = entry.get("ts")
            open_by_key.setdefault(key, []).append(message)
            messages.append(message)
            continue
        status = _tool_status_token(entry.get("status"))
        pending = open_by_key.get(key)
        if pending:
            message = pending.pop(0)
            message["status"] = status
            message["tool"]["status"] = status
            if summary:
                message["display_text"] = summary
            if files:
                message["tool"]["files"] = files
            _merge_tool_detail(message["tool"], entry)
            started = _parse_time(message.pop("_started_ts", None))
            finished = _parse_time(entry.get("ts"))
            if "duration_ms" not in message["tool"] and started is not None and finished is not None and finished >= started:
                message["tool"]["duration_ms"] = int((finished - started).total_seconds() * 1000)
            continue
        if accountant is not None:
            accountant.consider(1)
        ordinal = ordinals.get(bucket_id, 0)
        ordinals[bucket_id] = ordinal + 1
        messages.append(
            _tool_call_message(
                channel_id=channel_id,
                persona_id=persona_id,
                persona_instance_id=persona_instance_id,
                bucket_id=bucket_id,
                run_id=real_run_id,
                turn_id=turn_id,
                ordinal=ordinal,
                tool_name=tool_name,
                status=status,
                timestamp=entry.get("ts"),
                summary=summary,
                files=files,
                stage_id=safe_assignment_text(entry.get("stage_id"), limit=160),
                task_id=safe_assignment_text(entry.get("task_id"), limit=160),
                entry=entry,
            )
        )
    for message in messages:
        message.pop("_started_ts", None)
    if accountant is not None and messages:
        accountant.include(len(messages))
    return messages


def _tool_call_message(
    *,
    channel_id: str,
    persona_id: str,
    persona_instance_id: str | None,
    bucket_id: str,
    run_id: str | None,
    turn_id: str | None,
    ordinal: int,
    tool_name: str,
    status: str,
    timestamp: Any,
    summary: str | None,
    files: list[str],
    stage_id: str | None,
    task_id: str | None,
    entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    refs: dict[str, Any] = {"source": "persona_chat_trace", "tool_name": tool_name}
    if run_id:
        refs["run_id"] = run_id
    if stage_id:
        refs["stage_id"] = stage_id
    if task_id:
        refs["task_id"] = task_id
    tool: dict[str, Any] = {"tool_name": tool_name, "status": status}
    if files:
        tool["files"] = files
    if entry is not None:
        _merge_tool_detail(tool, entry)
    message = {
        "id": f"{channel_id}:tool:{bucket_id}:{ordinal}",
        "seq": 0,
        "timestamp": timestamp,
        "actor_persona_id": persona_id,
        "actor_instance_id": persona_instance_id,
        "role": "agent",
        "kind": "tool_call",
        "status": status,
        "display_title": f"Tool · {tool_name}",
        "display_text": summary or f"Tool {tool_name}",
        "redaction_status": "safe",
        "refs": refs,
        "tool": tool,
    }
    if turn_id:
        message["turn_id"] = turn_id
        # C8 ordering key: turn-anchored content without an emitter seq sits in
        # the content band — after the turn's operator row, before its terminal
        # reply — keeping its relative order among band peers from the fallback.
        message["turn_seq"] = TURN_SEQ_CONTENT
    return message


# Operator-detail fields carried from a trace entry onto the tool_call payload.
# The values were already operator-sanitized (secret-scrubbed, bounded) when the
# trace entry was rendered; this is a straight, newest-wins merge.
_TOOL_DETAIL_STR_FIELDS = (
    "command", "target", "detail", "output",
    # First-class agent-to-agent dispatch (G2): the target persona chip + the
    # full order, carried onto the tool{} payload under the same names the
    # launcher reads. Already operator-sanitized upstream; straight newest-wins.
    "dispatch_target", "dispatch_order",
    # Generic tool input/result record (tools with no dedicated detail field) —
    # feeds the console's collapsed Input/Result dropdowns.
    "tool_input", "tool_result",
)
_TOOL_DETAIL_INT_FIELDS = ("duration_ms", "exit_code")


def _merge_tool_detail(tool: dict[str, Any], entry: dict[str, Any]) -> None:
    for field in _TOOL_DETAIL_STR_FIELDS:
        value = entry.get(field)
        if isinstance(value, str) and value.strip():
            tool[field] = value
    for field in _TOOL_DETAIL_INT_FIELDS:
        value = entry.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            tool[field] = value
    paths = entry.get("paths")
    if isinstance(paths, list) and paths:
        tool["paths"] = [str(item) for item in paths if str(item or "").strip()][:12]
    skill_id = entry.get("skill_id")
    if isinstance(skill_id, str) and skill_id.strip():
        tool["skill_id"] = skill_id.strip()


def _tool_status_token(value: Any) -> str:
    status = safe_assignment_token(value) or ""
    if status in _TOOL_FAILED_STATUSES:
        return "failed"
    if status in _TOOL_OK_STATUSES or not status:
        return "ok"
    return status


def _apply_conversation_cap(
    messages: list[dict[str, Any]],
    *,
    channel_id: str,
    accountant: Any = None,
) -> list[dict[str, Any]]:
    """Bound the per-channel message count without dropping load-bearing rows.

    Flow kinds trim oldest-first; goal_input / operator / reply / proof /
    blocker / handoff / final always survive. A ``turns_collapsed`` marker
    replaces the trimmed span so the launcher can render an honest divider.
    """

    if len(messages) <= _CONVERSATION_MESSAGE_CAP:
        return messages
    protected = [m for m in messages if m.get("kind") not in _CONVERSATION_TRIMMABLE_KINDS]
    trimmable = [m for m in messages if m.get("kind") in _CONVERSATION_TRIMMABLE_KINDS]
    budget = max(_CONVERSATION_MESSAGE_CAP - len(protected) - 1, 0)
    dropped = trimmable[: len(trimmable) - budget] if budget else trimmable
    kept = trimmable[len(trimmable) - budget :] if budget else []
    if not dropped:
        return messages
    marker = {
        "id": f"{channel_id}:turns_collapsed",
        "seq": 0,
        "timestamp": dropped[-1].get("timestamp"),
        "actor_persona_id": "system",
        "actor_instance_id": None,
        "role": "system",
        "kind": "turns_collapsed",
        "status": "delivered",
        "display_title": "Earlier activity collapsed",
        "display_text": f"Earlier activity collapsed ({len(dropped)} messages). Newest turns are shown.",
        "redaction_status": "safe",
        "refs": {"collapsed_count": len(dropped)},
    }
    if accountant is not None:
        # Deliberate bound: the channel keeps the newest turns and the trimmed
        # span is disclosed in-band by the ``turns_collapsed`` marker above.
        accountant.drop(
            "turn_cap_trimmed",
            count=len(dropped),
            entity_id=channel_id,
            by_design=True,
        )
        accountant.mark_truncated()
    return _order_conversation_messages([*protected, marker, *kept])


def _conversation_kind_from_status(status: str) -> str:
    if status in {"handoff", "ready_for_qa", "next_stage_ready", "backend_join_ready"}:
        return "handoff"
    if status in {"done", "completed", "passed", "approved"}:
        return "final"
    return "agent_update"


def _conversation_title_for_kind(kind: str) -> str:
    return {
        "handoff": "Handoff",
        "proof": "Proof update",
        "blocker": "Blocked",
        "final": "Final update",
    }.get(kind, "Agent update")


def _safe_conversation_text(value: Any, *, limit: int) -> str | None:
    text = str(value or "").replace("\x00", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Mask secret-bearing lines in place instead of dropping the whole message —
    # a dev rationale that quotes one env assignment must not vanish wholesale.
    # Preserve intra-line whitespace on the survivors: conversation text carries
    # code blocks and aligned output whose indentation must reach the display.
    lines = [
        "[redacted line — contained a secret]"
        if _SECRET_RE.search(line)
        else line.rstrip()
        for line in text.split("\n")
    ]
    normalized = "\n".join(lines).strip()
    normalized = re.sub(r"\n{4,}", "\n\n\n", normalized)
    if not normalized:
        return None
    if len(normalized) > limit:
        # Truncation must be visible, never silent. Single-line marker: this
        # sanitizer also feeds single-line fields (titles, list items).
        normalized = normalized[:limit].rstrip() + " … [truncated]"
    return normalized


def _safe_conversation_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    safe: list[str] = []
    for item in value:
        text = _safe_conversation_text(item, limit=limit)
        if text:
            safe.append(text)
    return safe[:8]


def _conversation_message_sort_key(message: dict[str, Any]) -> tuple[int, str, str]:
    if message.get("kind") == "goal_input":
        return (0, "", str(message.get("id") or ""))
    parsed = _parse_time(message.get("timestamp"))
    if parsed is not None:
        return (1, parsed.isoformat(), str(message.get("id") or ""))
    return (2, str(message.get("timestamp") or ""), str(message.get("id") or ""))


def _order_conversation_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """C8: order the conversation by the ONE turn-scoped key.

    Turn-anchored rows (anchor = token(turn_id | logical client_message_id), position =
    ``turn_seq``) sort inside their turn by the stamped position — operator row
    first, content band between, terminal reply/interrupt last — immune to the
    two-clock skew between SessionDB stamps and trace ``ts`` values (F17). Rows
    without the key (pre-C8 history, goal_input, warnings) keep the pre-C8
    timestamp fallback, which also anchors where each turn sits among them.
    """

    return order_transcript_rows(
        messages,
        anchor=lambda message: (
            safe_assignment_token(message.get("turn_id"))
            or canonical_persona_chat_turn_id(message.get("client_message_id"))
        ),
        turn_seq=lambda message: message.get("turn_seq")
        if isinstance(message.get("turn_seq"), int)
        and not isinstance(message.get("turn_seq"), bool)
        else None,
        fallback_key=lambda message, _index: _conversation_message_sort_key(message),
    )


def _dedupe_conversation_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_assignments: set[str] = set()
    seen_thinking_texts: set[str] = set()
    # A run's reasoning often lands twice: once as the run-summary flow message
    # (thinking_summary/turn) and once as a trace progress row (agent_update).
    # The flow message wins; the duplicate progress row is curated out.
    flow_texts = {
        message.get("display_text")
        for message in messages
        if message.get("kind") in {"thinking_summary", "turn"} and message.get("display_text")
    }
    # The model's final text segment is often captured as a trailing "thinking"
    # step whose text IS the reply verbatim. Rendering both paints the reply
    # twice (an untimestamped Thinking bubble above the real one) — the reply
    # wins, the echo is curated out.
    reply_texts = {
        str(message.get("display_text") or "").strip()
        for message in messages
        if message.get("kind") == "reply" and message.get("display_text")
    }
    deduped: list[dict[str, Any]] = []
    for message in messages:
        refs = message.get("refs")
        assignment_id = (
            safe_assignment_text(refs.get("assignment_id"), limit=160)
            if isinstance(refs, dict)
            else None
        )
        if (
            assignment_id
            and message.get("kind") == "handoff"
            and message.get("display_title") == "Subagent prompt"
        ):
            if assignment_id in seen_assignments:
                continue
            seen_assignments.add(assignment_id)
        if message.get("kind") == "agent_update" and message.get("display_text") in flow_texts:
            continue
        # Per-step trace thinking and the per-run summary can carry the same
        # text (the final reasoning step often IS the decision rationale).
        # Keep the first occurrence in timeline order; drop later repeats.
        if message.get("kind") == "thinking_summary":
            text = message.get("display_text")
            if text and str(text).strip() in reply_texts:
                continue
            if text and text in seen_thinking_texts:
                continue
            if text:
                seen_thinking_texts.add(text)
        deduped.append(message)
    return deduped


def _latest_message_timestamp(messages: list[dict[str, Any]]) -> Any:
    dated = [message.get("timestamp") for message in messages if message.get("timestamp")]
    return dated[-1] if dated else None


# S47 removed ``_task_time`` — the conversation's ``updated_at`` fallback of
# last resort, reachable only through a task no caller could supply.


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


def _source_instance_ids_conflict(
    source_instance_ids: list[str],
    *,
    instances: list[PersonaInstance],
    history_rows: list[dict[str, Any]],
    trace_rows: list[dict[str, Any]],
) -> bool:
    if len(source_instance_ids) <= 1:
        return False
    rows = [
        *history_rows,
        *(row for row in trace_rows if not row.get("_mirrored_to_root")),
    ]
    sessions = {
        session
        for session in [
            *(_safe_session(getattr(instance, "session_id", None)) for instance in instances),
            *(_safe_session(row.get("session_id")) for row in rows),
        ]
        if session
    }
    if len(sessions) > 1:
        return True
    personas = {
        persona
        for persona in [
            *(
                _canonical_persona_id(getattr(instance, "persona_id", None))
                for instance in instances
            ),
            *(_canonical_persona_id(row.get("persona_id")) for row in rows),
        ]
        if persona
    }
    if len(personas) > 1:
        return True
    if any(item.startswith("personainst_operator_") for item in source_instance_ids):
        return False
    return True


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
    first = next((row for row in rows if not row.get("_mirrored_to_root")), rows[0])
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
