from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .models import PersonaInstance
from .mission_chat_turns import (
    TERMINAL_TURN_STATES,
    mission_chat_turn_elements,
    mission_chat_turn_records,
)
from .parity import ProjectionAccountant
from .persona_assignments import persona_instance_id_for, safe_assignment_text, safe_assignment_token
from .redaction import TEXT_SECRET_ASSIGNMENT_RE
from .redaction_mode import redaction_observe_enabled
from .relay_policy import parse_harness_delivery_marker, parse_relay_sender_marker
from .run_budget import ACCOUNTING_KEY as RUN_BUDGET_ACCOUNTING_KEY
from .runtime_hud import (
    extract_runtime_context_envelope,
    extract_skill_preload_envelope,
)
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

# Typed kind for the role="user" row a dispatch DELIVERY turn is forged under
# (finish_reason=harness_delivery:<dispatch_id>:<0|1>). Same column, same
# precedent, different origin: nobody sent this message — the harness brought
# back the answer to work the agent dispatched. Without the kind it renders as
# something the operator typed, which is the one thing it is not.
PERSONA_HARNESS_DELIVERY_KIND = "harness_delivery"

# ── terminal-turn marker vocabulary (ONE table, keyed on the turn state) ─────
#
# A turn that settles TERMINALLY without a recorded reply synthesizes a typed
# system marker row. The marker is what tells every downstream projection the
# turn is over — most concretely, it is the input
# ``operator_channels._settle_terminal_tool_calls`` reads to stop a
# ``tool_started``-without-``tool_finished`` row rendering a live spinner
# forever.
#
# The table exists because the filter used to be ``state != "interrupted"``: one
# hardcoded legacy state. When the wall-budget work (2026-07-26) added the
# ``budget_exhausted`` terminal state, that single-state filter silently
# excluded it — no marker, no settlement, and the launcher cockpit spun a tool
# row for a turn that had been over for minutes. Adding a terminal state must
# extend a TABLE, not require finding every string comparison.
#
# ``kind`` values are the wire vocabulary the Launcher already consumes
# (``mission_agent_chat_adapter.dart``: ``turn_interrupted`` →
# retry affordance, ``budget_exhausted`` → graceful-checkpoint marker). Do not
# mint a new kind for a state the Launcher already renders.
PERSONA_TURN_INTERRUPTED_KIND = "turn_interrupted"
PERSONA_TURN_BUDGET_EXHAUSTED_KIND = "budget_exhausted"


@dataclass(frozen=True, slots=True)
class TerminalTurnMarker:
    """How one terminal turn state presents when it recorded no reply."""

    kind: str
    id_slug: str
    text: str


TERMINAL_TURN_MARKERS: dict[str, TerminalTurnMarker] = {
    "interrupted": TerminalTurnMarker(
        kind=PERSONA_TURN_INTERRUPTED_KIND,
        id_slug="turn-interrupted",
        text=(
            "Agent turn interrupted before a reply was recorded. Retry the message "
            "to run a fresh turn."
        ),
    ),
    "budget_exhausted": TerminalTurnMarker(
        kind=PERSONA_TURN_BUDGET_EXHAUSTED_KIND,
        id_slug="turn-budget-exhausted",
        text=(
            "Agent turn reached its wall budget and settled at a graceful checkpoint "
            "before a reply was recorded. Any work it committed stands; send a new "
            "message to continue from there."
        ),
    ),
}
# Guard, not decoration: a marker may only be declared for a state the turn
# store itself calls terminal. A typo, or a state that is still in flight, would
# otherwise mark a LIVE turn as over — the opposite failure of the one this
# table fixes, and a worse one. Raised (not asserted) so ``python -O`` cannot
# strip the contract. The turn store's own vocabulary guards
# (``mission_chat_turns._guard_turn_state_vocabulary``) already prove
# ``TERMINAL_TURN_STATES`` partitions the known universe with the in-flight and
# settling buckets, so this check is the last link of one chain, not a second
# opinion about which states are terminal.
_UNKNOWN_MARKER_STATES = sorted(set(TERMINAL_TURN_MARKERS) - TERMINAL_TURN_STATES)
if _UNKNOWN_MARKER_STATES:  # pragma: no cover - import-time contract guard
    raise RuntimeError(
        "TERMINAL_TURN_MARKERS declares non-terminal turn state(s): "
        f"{_UNKNOWN_MARKER_STATES}"
    )

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

# Single-homed in ``agent_runtime.redaction`` — see the header there for the
# JSON blind spot every local spelling shared. Detection here (a matching line
# is dropped/blocked whole), so the shared pattern's group(2) is inert.
# ``snapshot`` imports this name; it stays a module attribute on purpose.
_SECRET_RE = TEXT_SECRET_ASSIGNMENT_RE
_ASSISTANT_CLIENT_MESSAGE_ID_RE = re.compile(r"^(.+):assistant:\d+$")


def logical_persona_chat_client_message_id(value: Any) -> str | None:
    """Return the durable operator-turn identity for a persisted chat row."""

    client_message_id = safe_assignment_text(value, limit=240)
    if not client_message_id:
        return None
    match = _ASSISTANT_CLIENT_MESSAGE_ID_RE.fullmatch(client_message_id)
    return match.group(1) if match else client_message_id


def canonical_persona_chat_turn_id(value: Any) -> str | None:
    """Return the tokenized turn id shared by operator and assistant rows."""

    logical_id = logical_persona_chat_client_message_id(value)
    return safe_assignment_token(logical_id) or None


def persona_chat_history_summary(
    *,
    persona_instances: Iterable[PersonaInstance],
    session_db: Any | None = None,
    limit: int = 50,
    message_tail: int = DEFAULT_PERSONA_CHAT_MESSAGE_TAIL,
    accountant: ProjectionAccountant | None = None,
    persona_assignments: Iterable[Any] | None = None,
    omitted_session_ids: set[str] | None = None,
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
    instances_by_id: dict[str, PersonaInstance] = {}
    instances_by_persona: dict[str, PersonaInstance] = {}
    for instance in persona_instances:
        instance_id = safe_assignment_text(getattr(instance, "id", None), limit=160)
        if instance_id:
            instances_by_id[instance_id] = instance
        persona_id = _canonical_persona_id(getattr(instance, "persona_id", None))
        if persona_id:
            instances_by_persona[persona_id] = instance
        session_id = safe_assignment_text(
            getattr(instance, "default_chat_session_id", None), limit=200
        )
        active_session_id = safe_assignment_text(
            getattr(instance, "session_id", None), limit=200
        )
        # A task-bound mission mirrors its live run id into the default-chat
        # pointer. That id belongs to the mission/event lane, not SessionDB;
        # the synthetic mission row below is its authoritative projection.
        if (
            safe_assignment_token(getattr(instance, "mode", None)) == "task_bound"
            and getattr(instance, "current_task_id", None)
            and session_id
            and session_id == active_session_id
        ):
            session_id = None
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

    # Candidate discovery/accounting stays lightweight. Message tails, lineage,
    # and runtime observations are hydrated only after the creation-order bound
    # is applied; on a busy runtime this avoids reading every historical chat to
    # render the newest 50.
    candidates: list[tuple[dict[str, Any], PersonaInstance, str, str, str | None]] = []
    seen: set[str] = set()
    for raw in sessions or []:
        if not isinstance(raw, dict):
            continue
        session_id = safe_assignment_text(raw.get("id"), limit=200)
        if not session_id or session_id in seen:
            continue
        is_source_chat = safe_assignment_token(raw.get("source")) == PERSONA_CHAT_SESSION_SOURCE
        root_meta = _model_config(raw.get("model_config")).get("mission_chat_root_id")
        if root_meta and safe_assignment_text(root_meta, limit=200) != session_id:
            # Compression descendants are projected through their stable root.
            seen.add(session_id)
            continue
        # Only persona-chat sessions and sessions bound to a live instance are
        # candidates for this projection. Unrelated SessionDB sources (cron,
        # telegram, cli, scratch) can never render as chat rows — counting them
        # as no_instance_match drops floods parity with by-design noise.
        if not is_source_chat and session_id not in bound_by_session:
            seen.add(session_id)
            continue
        if accountant is not None:
            accountant.consider(1)
        persisted_instance_id = _persisted_persona_instance_id(raw)
        inferred_persona = _infer_persona_id(raw, session_id=session_id)
        instance = (
            bound_by_session.get(session_id)
            or instances_by_id.get(persisted_instance_id or "")
            or (
                instances_by_persona.get(inferred_persona)
                if is_source_chat and inferred_persona
                else None
            )
        )
        if instance is None:
            if accountant is not None:
                accountant.drop("no_instance_match", entity_id=session_id)
            # A session id can appear in both the source and broad pools;
            # mark it seen so a drop is only accounted once.
            seen.add(session_id)
            continue
        candidates.append((raw, instance, session_id, "chat", None))
        seen.add(session_id)

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
                # Anomalous on purpose: the instance still points at a session
                # SessionDB no longer has. Hiding the row is correct here (this
                # projection is READ-ONLY), but the stale binding is a real
                # defect a write path must clear —
                # ``PersonaInstanceStore.repair_missing_chat_session_bindings``
                # via ``harness persona-instance reconcile``.
                accountant.consider(1)
                accountant.drop("session_not_in_db", entity_id=session_id)
            continue
        candidates.append((raw, instance, session_id, "chat", None))
        seen.add(session_id)
        if accountant is not None:
            accountant.consider(1)

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
        candidates.append((synthetic, instance, session_id, "mission", task_id))
        seen.add(session_id)
        if accountant is not None:
            accountant.consider(1)

    # The directory contract is creation order, not activity order. Opening or
    # continuing an older chat may advance ``updated_at`` but must never move it
    # above a conversation created later. Resolve every eligible row first, then
    # sort and truncate so an active old chat cannot crowd a newer chat out of
    # the bounded projection. Session id is the deterministic tie-breaker for
    # legacy rows whose creation timestamp is missing. Candidate fields are the
    # same fields ``_history_row`` projects into the final row, so selection is
    # byte-equivalent while hydration is bounded to the visible slice.
    candidates.sort(key=_persona_chat_candidate_sort_key, reverse=True)
    visible_candidates = candidates[: max(0, limit)]
    if omitted_session_ids is not None:
        omitted_session_ids.update(
            session_id
            for _raw, _instance, session_id, _kind, _task_id in candidates[len(visible_candidates):]
            if session_id
        )
    visible: list[dict[str, Any]] = []
    for raw, instance, session_id, kind, task_id in visible_candidates:
        row = _history_row(
            raw,
            instance,
            session_id=session_id,
            session_db=db,
            message_tail=message_tail,
            kind=kind,
        )
        if task_id is not None:
            row["task_id"] = task_id
        visible.append(row)
    if accountant is not None:
        accountant.include(len(visible))
        omitted = len(candidates) - len(visible)
        if omitted > 0:
            # Deliberate bound: the directory keeps the newest ``limit`` rows by
            # creation order and every omitted row stays fetchable per-session.
            # A busy runtime drops here on EVERY build — steady state, not a
            # symptom — so it is declared by-design; a reader that counts it as
            # an anomaly pins its health pill amber forever.
            accountant.drop("limit", count=omitted, by_design=True)
            accountant.mark_truncated()
    return visible


def persona_chat_session_messages(
    *,
    session_id: str,
    limit: int = DEFAULT_PERSONA_CHAT_MESSAGE_TAIL,
    before: str | None = None,
    session_db: Any | None = None,
) -> dict[str, Any]:
    """Paged on-demand read of one persona chat session's complete transcript.

    Backs ``harness persona chat history`` — the fetch that replaces the message
    tail S2 evicts from the steady-state frame. Each page is bounded, but the
    opaque ``before`` cursor can walk all curated rows across the root's native
    compression lineage. The log IS the history: no new storage, just a
    per-session read of the durable SessionDB the frame previously projected.
    """

    bounded = _bounded_message_tail(limit)
    db = session_db or _default_session_db()
    if db is None:
        return {
            "ok": True,
            "session_id": session_id,
            "limit": bounded,
            "count": 0,
            "total_count": 0,
            "has_more": False,
            "next_before": None,
            "history_revision": _history_revision(session_id, []),
            "redaction_status": "safe",
            "messages": [],
        }
    messages, status = _safe_curated_messages(db, session_id=session_id)
    end = len(messages)
    if before:
        cursor = _decode_history_cursor(before)
        if cursor is None or cursor.get("session_id") != session_id:
            return {
                "ok": False,
                "error_kind": "invalid_history_cursor",
                "error": "history cursor is malformed or belongs to another session",
                "session_id": session_id,
            }
        before_id = safe_assignment_text(cursor.get("before_id"), limit=160)
        match = next(
            (index for index, row in enumerate(messages) if row.get("id") == before_id),
            None,
        )
        if match is None:
            return {
                "ok": False,
                "error_kind": "invalid_history_cursor",
                "error": "history cursor no longer resolves in this session",
                "session_id": session_id,
            }
        end = match
    start = max(0, end - bounded)
    page = messages[start:end]
    has_more = start > 0
    return {
        "ok": True,
        "session_id": session_id,
        "limit": bounded,
        "count": len(page),
        "total_count": len(messages),
        "has_more": has_more,
        "next_before": (
            _encode_history_cursor(session_id, page[0]["id"])
            if has_more and page
            else None
        ),
        "history_revision": _history_revision(session_id, messages, session_db=db),
        "redaction_status": status,
        "messages": page,
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

    * **historical task-run trace** — persisted events from the retired task lane,
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

    # No event log, no trace lane to project — an honest empty page. This read
    # `event_log or _default_event_log()`, naming a helper that has never
    # existed in this module: any caller that omitted the argument got a
    # NameError instead of the empty list two lines below. It never fired only
    # because both production callers (snapshot.build_snapshot,
    # status.build_status) resolve their own CachedEventLog first. Found by the
    # F821 gate.
    log = event_log
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
                # Deliberate bound: the trace lane keeps a tail window.
                accountant.drop(
                    "tail_truncated",
                    count=truncated,
                    entity_id=self.instance_id,
                    by_design=True,
                )
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
                # Chat History is a conversation directory, not an inbox.
                # Creation order is immutable; activity must not reshuffle it.
                order_by_last_active=False,
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
    # History pointers, on-demand message tails, open/send validation and
    # transcript writes must all resolve the same operator-visible database.
    # A Launcher-selected profile changes HERMES_HOME, but not the chat scope:
    # ``chat_session_scope`` is the ONE place that decides which database that
    # is (relay context > HERMES_HEAD_HOME > the shared runtime root's recorded
    # head pointer > the degraded ambient home).
    from .chat_session_scope import open_chat_session_db

    return open_chat_session_db()


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
    active_session_id = session_id
    try:
        active_session_id = session_db.resolve_resume_session_id(session_id)
    except Exception:
        pass
    try:
        from .persona_chat_continuity import (
            native_lineage_summary,
            persona_chat_runtime_registry,
        )

        registry = persona_chat_runtime_registry()
        lineage = native_lineage_summary(session_db, session_id)
        runtime = (
            registry.observation(session_id, owning_process=True)
            if registry is not None
            else {
                "runtime_state": "unknown",
                "runtime_observer_id": "external_cli",
            }
        )
    except Exception:
        lineage = {"active_session_id": active_session_id, "continuation_depth": 0}
        runtime = {
            "runtime_state": "unknown",
            "runtime_observer_id": "external_cli",
        }
    lineage_aggregate = _lineage_aggregate(
        session_db,
        root_session_id=session_id,
        active_session_id=lineage["active_session_id"],
    )
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
        "message_count": lineage_aggregate.get(
            "message_count", _safe_int(raw.get("message_count"))
        ),
        "created_at": _iso_timestamp(raw.get("started_at")),
        "updated_at": _iso_timestamp(
            lineage_aggregate.get("last_active")
            or raw.get("last_active")
            or raw.get("ended_at")
            or raw.get("started_at")
        ),
        "state": "archived" if bool(raw.get("archived")) else "open",
        "redaction_status": redaction_status,
        **({"would_redact": would_redact} if would_redact else {}),
        **_token_usage_fields({**raw, **lineage_aggregate}),
        **_chat_model_fields(raw),
        **_cache_policy_fields(raw),
        "messages": messages,
        "root_chat_session_id": session_id,
        "active_session_id": lineage["active_session_id"],
        "runtime_state": runtime.get("runtime_state", "unknown"),
        "last_runtime_transition": runtime.get("last_runtime_transition"),
        "runtime_observer_id": runtime.get("runtime_observer_id"),
        "runtime_observed_at": runtime.get("runtime_observed_at"),
        "continuation_depth": lineage["continuation_depth"],
        "last_resumed_at": runtime.get("last_resumed_at"),
    }


def _lineage_aggregate(
    session_db: Any | None,
    *,
    root_session_id: str,
    active_session_id: str,
) -> dict[str, Any]:
    """Aggregate usage/activity exactly once across root→compression tip."""

    if session_db is None:
        return {}
    current = active_session_id
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    while current and current not in seen:
        seen.add(current)
        try:
            row = session_db.get_session(current)
        except Exception:
            return {}
        if not isinstance(row, dict):
            return {}
        rows.append(row)
        if current == root_session_id:
            break
        current = safe_assignment_text(row.get("parent_session_id"), limit=240)
    if not rows or current != root_session_id:
        return {}
    result: dict[str, Any] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "message_count",
    ):
        result[key] = sum(_safe_int(row.get(key)) for row in rows)
    activity = [
        _iso_timestamp(
            row.get("last_active") or row.get("ended_at") or row.get("started_at")
        )
        for row in rows
    ]
    # Some SessionDB implementations omit computed activity fields from
    # ``get_session``. Only in that case derive a fallback from durable message
    # timestamps; an explicit session activity value remains authoritative.
    if not any(row.get("last_active") or row.get("ended_at") for row in rows):
        try:
            lineage_loader = getattr(session_db, "get_messages_as_conversation", None)
            native_messages = (
                lineage_loader(active_session_id, include_ancestors=True)
                if callable(lineage_loader)
                else session_db.get_messages(root_session_id)
            )
        except Exception:
            native_messages = []
        activity.extend(
            _iso_timestamp(
                message.get("created_at")
                or message.get("timestamp")
                or message.get("time")
                or message.get("updated_at")
            )
            for message in native_messages or []
            if isinstance(message, dict)
        )
    result["last_active"] = max((item for item in activity if item), default=None)
    return result


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


def _persisted_persona_instance_id(raw: dict[str, Any]) -> str | None:
    """Read the session's authoritative owning instance binding.

    Modern Mission Control sessions persist this in ``model_config``. It must
    outrank persona inference from a display prompt or session-id shape: prompts
    legitimately contain the complete persona system prompt, and placement ids
    such as ``personainst_neko_supervisor_agent_f6f7a51b`` are intentionally not
    reducible to a bare persona id without the instance registry.
    """

    return safe_assignment_text(
        _model_config(raw.get("model_config")).get("persona_instance_id"),
        limit=160,
    )


def _persona_chat_creation_sort_key(row: dict[str, Any]) -> tuple[bool, str, str]:
    created_at = _iso_timestamp(row.get("created_at"))
    return (
        created_at is not None,
        created_at or "",
        safe_assignment_text(row.get("session_id"), limit=200),
    )


def _persona_chat_candidate_sort_key(
    candidate: tuple[dict[str, Any], PersonaInstance, str, str, str | None]
) -> tuple[bool, str, str]:
    raw, _instance, session_id, _kind, _task_id = candidate
    created_at = _iso_timestamp(raw.get("started_at"))
    return (created_at is not None, created_at or "", session_id)


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
    rows, status = _safe_curated_messages(session_db, session_id=session_id)
    return rows[-_bounded_message_tail(limit):], status


def _safe_curated_messages(
    session_db: Any | None,
    *,
    session_id: str,
) -> tuple[list[dict[str, Any]], str]:
    """Return every redaction-safe operator-facing row for one logical chat.

    Snapshot callers continue to take a bounded tail through
    :func:`_safe_recent_messages`; the on-demand authority pages this complete
    ordered list with opaque cursors.
    """

    if session_db is None:
        return [], "safe"
    try:
        lineage_loader = getattr(session_db, "get_messages_as_conversation", None)
        try:
            native_tip = session_db.resolve_resume_session_id(session_id)
        except Exception:
            native_tip = session_id
        raw_messages = (
            lineage_loader(native_tip, include_ancestors=True)
            if callable(lineage_loader)
            else session_db.get_messages(session_id)
        )
    except Exception:
        return [], "safe"
    rows: list[dict[str, Any]] = []
    redacted = False
    assistant_client_message_ids: set[str] = set()
    seen_logical_rows: set[tuple[str, str, str]] = set()
    # ONE read of this chat's turn journal, shared by the reply rows below and
    # by the terminal-marker rows appended after them. The journal is a
    # per-session file with no read cache, so fetching it again per row — just
    # to add one small key — would have turned a bounded page into N file reads.
    turn_records = mission_chat_turn_records(session_id=session_id)
    turn_records_by_message = {
        str(record.get("client_message_id") or ""): record for record in turn_records
    }
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
            raw.get("platform_message_id")
            or raw.get("message_id")
            or raw.get("client_message_id"),
            limit=240,
        )
        logical_client_message_id = logical_persona_chat_client_message_id(
            client_message_id
        )
        turn_id = canonical_persona_chat_turn_id(client_message_id)
        if role == "agent" and logical_client_message_id:
            assistant_client_message_ids.add(logical_client_message_id)
        raw_content = raw.get("content") or raw.get("text")
        runtime_context = None
        skill_preload = None
        if role == "operator":
            # Composition order is message · skill_preload · runtime_context,
            # so strip the end-anchored HUD envelope first; the skill-preload
            # envelope is end-anchored on the remainder.
            raw_content, runtime_context = extract_runtime_context_envelope(raw_content)
            raw_content, skill_preload = extract_skill_preload_envelope(raw_content)
        curated = _curate_chat_message_text(role, raw_content)
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
        if runtime_context is not None:
            row["runtime_context"] = runtime_context
        if skill_preload is not None:
            row["skill_preload"] = skill_preload
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
        # HARNESS DELIVERY ATTRIBUTION: a dispatch delivery turn is forged into
        # the SENDER's own thread as a role="user" row, so at rest it is
        # indistinguishable from an operator message — the same defect the
        # relay marker below retires, one lane over. The two facts carried are
        # what the row settles and whether the operator was flagged; both come
        # from the marker because by the time this is read the dispatch has
        # already left every live projection.
        if role == "operator":
            delivery = parse_harness_delivery_marker(raw.get("finish_reason"))
            relay_sender = (
                None
                if delivery is not None
                else parse_relay_sender_marker(raw.get("finish_reason"))
            )
            if delivery is not None:
                row["kind"] = PERSONA_HARNESS_DELIVERY_KIND
                row["delivery_dispatch_id"] = delivery.dispatch_id
                row["delivery_notify_operator"] = delivery.notify_operator
                row["delivery_state"] = delivery.state
            elif relay_sender is not None:
                row["kind"] = PERSONA_RELAYED_MESSAGE_KIND
                row["relay_sender_persona_id"] = relay_sender.persona_id
                row["relay_sender_instance_id"] = relay_sender.instance_id
        if client_message_id:
            logical_key = (role, logical_client_message_id or client_message_id, text)
            if logical_key in seen_logical_rows:
                continue
            seen_logical_rows.add(logical_key)
            row["client_message_id"] = client_message_id
            if turn_id:
                row["turn_id"] = turn_id
            # C8 ordering key: the turn anchor is the canonical logical turn id;
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
                client_message_id=logical_client_message_id,
            )
            if elements:
                row["turn_elements"] = elements
            # "What bounded this turn?" — carried verbatim off the turn record
            # (see mission_chat_turns._JOURNAL_RUN_BUDGET_FIELD). The row shapes
            # here are an explicit allowlist, so an unknown key does NOT ride
            # through on its own; this is the additive extension that lets the
            # cockpit read the block the settle point persisted. Read-only: the
            # projection reads the journal, it never writes it.
            _carry_run_budget(
                row, turn_records_by_message.get(str(logical_client_message_id or ""))
            )
        rows.append(row)
    rows.extend(
        _terminal_turn_marker_rows(
            session_id=session_id,
            assistant_client_message_ids=assistant_client_message_ids,
            records=turn_records,
        )
    )
    rows = _ordered_message_rows(rows)
    return rows, "redacted" if redacted else "safe"


def _history_revision(
    session_id: str,
    messages: list[dict[str, Any]],
    *,
    session_db: Any | None = None,
) -> str:
    """Stable revision for cache invalidation without exposing transcript text."""

    if session_db is not None:
        try:
            from .persona_chat_continuity import native_history_revision

            return native_history_revision(session_db, session_id)
        except Exception:
            pass
    payload = json.dumps(messages, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{session_id}:{digest}"


def _encode_history_cursor(session_id: str, before_id: str) -> str:
    payload = json.dumps(
        {"v": 1, "session_id": session_id, "before_id": before_id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_history_cursor(value: str) -> dict[str, Any] | None:
    try:
        token = str(value or "").strip()
        padded = token + "=" * (-len(token) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if not isinstance(decoded, dict) or decoded.get("v") != 1:
            return None
        if not safe_assignment_text(decoded.get("before_id"), limit=160):
            return None
        return decoded
    except Exception:
        return None


def _carry_run_budget(row: dict[str, Any], record: dict[str, Any] | None) -> None:
    """Ride the turn record's accounting block onto a projected row, or nothing.

    Absence-preserving in both directions: a record with no block (an older
    turn, or one that declared no budget) leaves the row untouched, so a reader
    can still tell "nothing bounded this turn" from "nobody accounted it". The
    block is copied verbatim — the store already bounded it through the one
    ``run_budget.safe_accounting_block`` reader.
    """

    block = (record or {}).get(RUN_BUDGET_ACCOUNTING_KEY)
    if isinstance(block, dict) and block:
        row[RUN_BUDGET_ACCOUNTING_KEY] = block


def _terminal_turn_marker_rows(
    *,
    session_id: str,
    assistant_client_message_ids: set[str],
    records: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Synthesize the typed marker row for every terminally-settled, reply-less turn.

    Driven by ``TERMINAL_TURN_MARKERS`` — one table, one row shape, one code
    path per terminal state. This used to test ``state != "interrupted"``, which
    made ``budget_exhausted`` (the wall-budget terminal state, 2026-07-26) a
    turn that ended and never said so: no marker, so
    ``operator_channels._settle_terminal_tool_calls`` never fired and a
    ``tool_started`` row with no finish spun in the cockpit forever.

    ``settled_state`` rides along so downstream projections carry the REASON the
    turn is over without re-deriving it from the marker kind or the prose.

    ``records`` lets the caller hand in the journal read it already did; left
    None (direct callers, tests) this reads the journal itself. The marker row
    is the ONLY row a reply-less budget-exhausted turn gets, so the accounting
    block has to ride here too — otherwise the exact turns whose bound is worth
    reading are the ones that carry no bound.
    """

    rows: list[dict[str, Any]] = []
    if records is None:
        records = mission_chat_turn_records(session_id=session_id)
    for record in records:
        state = safe_assignment_token(record.get("state"))
        marker = TERMINAL_TURN_MARKERS.get(state or "")
        if marker is None:
            continue
        client_message_id = safe_assignment_text(record.get("client_message_id"), limit=240)
        if not client_message_id or client_message_id in assistant_client_message_ids:
            continue
        turn_id = safe_assignment_token(record.get("turn_id")) or safe_assignment_token(client_message_id)
        if not turn_id:
            continue
        marker_row: dict[str, Any] = {
            "id": f"{session_id}:{marker.id_slug}:{client_message_id}",
            "role": "system",
            # Typed marker: downstream projections (operator conversation,
            # Mission Control tiles) key on this instead of matching text.
            "kind": marker.kind,
            "text": marker.text,
            "timestamp": _iso_timestamp(record.get("updated_at")),
            "redaction_status": "safe",
            "client_message_id": client_message_id,
            "turn_id": turn_id,
            # The canonical turn-store state, not a second vocabulary.
            "settled_state": state,
            # C8 ordering key: the terminal marker IS the turn's terminal
            # row (a turn has a reply or a marker, never both).
            "turn_seq": TURN_SEQ_TERMINAL,
        }
        _carry_run_budget(marker_row, record)
        rows.append(marker_row)
    return rows


def _ordered_message_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # C8: ONE ordering authority. Rows carrying the turn key (anchor =
    # canonical turn_id, position = turn_seq) sort by that key inside their
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
        anchor=lambda row: (
            safe_assignment_token(row.get("turn_id"))
            or canonical_persona_chat_turn_id(row.get("client_message_id"))
        ),
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
        # Generic tool input/result record (tools with no dedicated field):
        # key-per-line blocks; block scrub keeps line structure so the console
        # dropdown renders one key per line. Limits sit ABOVE the progress-sink
        # ceiling (1100/1700 + its truncation marker, which can inflate the
        # producer bound by re-redacting lines with the broader marker set) so
        # this tail-bounded scrub never truncates — a truncation here would cut
        # the HEAD, which is this record's operator signal.
        "tool_input": _safe_trace_operator_block(payload.get("tool_input"), limit=1200),
        "tool_result": _safe_trace_operator_block(payload.get("tool_result"), limit=1800),
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
    "[CONTEXT COMPACTION — REFERENCE ONLY]",
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
        if any(marker in text for marker in _INTERNAL_SCAFFOLDING_MARKERS):
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
