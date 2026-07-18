#!/usr/bin/env python3
"""Agent chat tool — agent-to-agent orchestration over the canonical chat lane.

Lets any chat persona brief, deploy, or steer ANOTHER persona by sending a
message into that persona's own Mission Control chat session ("Alice, deploy
Neko on X" -> Alice calls this tool -> the prompt lands in Neko's session,
Neko replies there, and the whole exchange is visible in Mission Control with
real provenance). This is the CHAT lane: no task, no daemon, no proof gates —
``mission_goal_create`` remains the escalation path for tracked goals.

Runs fully in-process by invoking the same handler behind
``hermes harness mission-chat message`` (session dedup, transcript persistence,
prompt observability, trace — one canonical lane, nothing re-implemented).
In-process matters: one Hermes process shelling out to another can hit the
``agent.log`` rotation lock (see mission_goal_tool.py), and the operator lane
must never fork a second, slightly different chat pipeline.

Scope contract (V2 — chained relays enabled 2026-07-08):
- relays may chain (operator -> Alice -> Neko -> Dev). Depth, cycle, and
  budget decisions are owned by ONE authority — ``agent_runtime.relay_policy``
  evaluated by the mission-chat handler at the canonical persona chokepoint
  (after persona-id canonicalization, so instance-id targets cannot dodge the
  cycle guard). This tool only CARRIES the envelope: it reads the current
  chain/deadline from the policy ContextVars (seeded per turn by the handler;
  tool workers inherit them via ``copy_context``) and forwards them as
  explicit ``relay_chain`` / ``relay_deadline_epoch`` request fields, so
  provenance survives process boundaries;
- typed refusals propagate honestly: ``relay_depth_limit``, ``relay_cycle``,
  ``relay_budget_exhausted`` (all carry ``relay_chain``);
- chained hops share ONE wall deadline — a relay does not reset the clock;
- ``HERMES_AGENT_CHAT_SCOPE=off`` disables the tool with a typed refusal;
  a blueprint-graph allow-list (only message agents wired to yours) is the
  planned ``graph`` scope and is not implemented yet — the tool passes the
  caller session id as ``requested_by`` provenance so the graph check has an
  anchor when it lands.
"""

import contextlib
import io
import json
import logging
import os
import time
import uuid
from types import SimpleNamespace

from agent_runtime import relay_policy
from tools.registry import registry

logger = logging.getLogger(__name__)

_REPLY_LIMIT = 8000
_MESSAGE_LIMIT = 12000

AGENT_CHAT_SEND_SCHEMA = {
    "name": "agent_chat_send",
    "description": (
        "Send a conversational message to ANOTHER Harness persona (persona id e.g. neko_supervisor/dev/qa, or a @personainst_* handle for a specific instance; display names refused). Omit session_id to continue the durable pair thread; new_session=true starts a fresh one. Disambiguator: does NOT start tracked work -- use mission_goal_create for that."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "persona_id": {
                "type": "string",
                "description": (
                    "Target persona id, e.g. 'neko_supervisor' (reaches the canonical primary "
                    "instance). A personainst_* handle is also accepted and targets THAT specific "
                    "instance — use it to reach a non-primary instance when a persona runs several."
                ),
            },
            "message": {
                "type": "string",
                "description": (
                    "The message to deliver, written TO the target agent: the ask, relevant context, "
                    "and what they should come back with. Include who the request originates from."
                ),
            },
            "session_id": {
                "type": "string",
                "description": (
                    "Optional target chat session id. Omit to continue the target's default chat "
                    "session (repeated sends thread into one conversation). Mutually exclusive with "
                    "new_session."
                ),
            },
            "new_session": {
                "type": "boolean",
                "description": (
                    "Start a FRESH clean thread with the target instead of continuing the default "
                    "one. Use sparingly — one durable thread per pair is the norm. Cannot be combined "
                    "with session_id."
                ),
                "default": False,
            },
            "max_seconds": {
                "type": "number",
                "description": "Wall budget for the target's reply turn. Default 240.",
                "default": 240,
            },
        },
        "required": ["persona_id", "message"],
    },
}


def _refusal(error: str, **extra) -> str:
    return json.dumps({"ok": False, "error": error, **extra})


def _looks_like_instance_handle(value) -> bool:
    """True when *value* is a ``personainst_*`` instance handle (not a bare
    persona id). A persona can run more than one live instance, so a handle in a
    persona slot must TARGET THAT INSTANCE — it is forwarded as the instance id
    rather than collapsed to the persona's canonical channel."""
    from agent_runtime.persona_assignments import safe_assignment_token

    return safe_assignment_token(value).startswith("personainst_")


def agent_chat_send(
    *,
    persona_id,
    message,
    session_id=None,
    new_session=False,
    max_seconds=240,
    requested_by_session=None,
):
    scope = (os.environ.get("HERMES_AGENT_CHAT_SCOPE") or "open").strip().lower()
    if scope == "off":
        return _refusal(
            "agent_chat_send is disabled on this runtime (HERMES_AGENT_CHAT_SCOPE=off). "
            "Tell the operator instead of retrying."
        )
    persona_id = (persona_id or "").strip()
    message = (message or "").strip()
    resolved_session_id = (str(session_id).strip() or None) if session_id else None
    new_session = bool(new_session)
    if not persona_id:
        return _refusal("agent_chat_send requires a persona_id.")
    if not message:
        return _refusal("agent_chat_send requires a non-empty message.")
    if len(message) > _MESSAGE_LIMIT:
        return _refusal(f"message exceeds the {_MESSAGE_LIMIT}-character relay limit; send a briefing, not a dump.")
    if new_session and resolved_session_id:
        # Contradictory: new_session asks for a fresh thread; session_id names an
        # existing one. Refuse rather than silently picking one — the caller must
        # decide which thread they mean.
        return _refusal(
            "agent_chat_send: new_session=true and session_id are contradictory — omit session_id "
            "to start a fresh thread, or drop new_session to continue that specific thread.",
            error_kind="contradictory_thread_target",
        )

    # Envelope provenance: the chain/deadline for the CURRENT turn, seeded by
    # the mission-chat handler. Depth/cycle policy is decided downstream at
    # the canonical chokepoint; here we only clamp this hop's wall budget to
    # the shared chain deadline and fast-fail when nothing usable is left.
    chain = relay_policy.RELAY_CHAIN.get()
    deadline_epoch = relay_policy.RELAY_DEADLINE.get()

    try:
        wall_budget = max(10.0, min(float(max_seconds or 240), 600.0))
    except (TypeError, ValueError):
        wall_budget = 240.0
    remaining = relay_policy.remaining_budget_seconds(deadline_epoch)
    if remaining is not None:
        if remaining < relay_policy.MIN_RELAY_BUDGET_SECONDS:
            return _refusal(
                f"relay budget exhausted: {max(remaining, 0.0):.1f}s left on the shared "
                f"chain deadline (minimum {relay_policy.MIN_RELAY_BUDGET_SECONDS:.0f}s "
                "per hop). Answer your caller with what you have.",
                error_kind="relay_budget_exhausted",
                relay_chain=list(chain),
            )
        wall_budget = min(wall_budget, remaining)
    effective_deadline = deadline_epoch if deadline_epoch is not None else time.time() + wall_budget

    requested_by = "agent-chat-relay"
    source_token = str(requested_by_session or "").strip()
    if source_token:
        requested_by = f"agent:{source_token[:120]}"

    # Instance targeting: a personainst_* handle in the persona slot addresses
    # THAT specific instance (a persona may have more than one live instance).
    # Forward it as the instance id so the handler threads the specific
    # instance's default session; the handler's canonical_persona_instance_id
    # preserves placement-backed sibling ids (personainst_<persona>_agent_2). A
    # bare persona id forwards no handle → the persona's canonical primary.
    target_instance_id = persona_id if _looks_like_instance_handle(persona_id) else None

    args = SimpleNamespace(
        persona_id=persona_id,
        persona_instance_id=target_instance_id,
        session_id=resolved_session_id,
        # Fresh-thread lane: the handler mints a new canonical session through the
        # SAME default-session chokepoint (mint= mode), never a tool-side mint —
        # keeping ONE minting authority (the orphaned-relay fix's whole point).
        new_session=new_session,
        task_id=None,
        goal_id=None,
        title=f"Agent relay to {persona_id}",
        message=message,
        provider=None,
        model=None,
        use_agent_default=False,
        surface_prompt="",
        intent_hint="chat",
        requested_by=requested_by,
        client_message_id=f"agent-relay-{uuid.uuid4().hex[:12]}",
        stream=False,
        max_seconds=wall_budget,
        json=True,
        # Explicit relay envelope — the handler's chokepoint guard reads
        # these; ambient ContextVars never cross a transport boundary.
        relay_chain=list(chain),
        relay_deadline_epoch=effective_deadline,
    )

    # The CLI handler prints its JSON payload; capture it so a nested reply can
    # never interleave with the OUTER turn's stdout protocol.
    buffer = io.StringIO()
    try:
        # persona_commands.py is exec'd into hermes_cli.harness globals by
        # _load_command_parts(); the handler is NOT importable from the part
        # module itself.
        from hermes_cli import harness as _harness

        with contextlib.redirect_stdout(buffer):
            exit_code = _harness._cmd_mission_chat_message(args)
    except Exception as exc:  # pragma: no cover - defensive; surfaced to the model
        logger.exception("agent_chat_send relay failed")
        return _refusal(f"{type(exc).__name__}: {exc}", target_persona=persona_id)

    raw = buffer.getvalue().strip()
    payload = _parse_last_json_object(raw)
    if payload is None:
        return _refusal(
            "relay produced no parseable reply payload",
            target_persona=persona_id,
            exit_code=exit_code,
            output_excerpt=raw[-400:],
        )

    # Compact result: the caller needs the reply and the thread pointers, not
    # the ~75KB prompt-observability block.
    reply = str(payload.get("reply") or "")[:_REPLY_LIMIT]
    result = {
        "ok": bool(payload.get("ok")) and exit_code == 0,
        "target_persona": persona_id,
        "reply": reply,
        "session_id": payload.get("session_id"),
        "chat_session_id": payload.get("chat_session_id"),
        "persona_instance_id": payload.get("persona_instance_id"),
        "total_tokens": payload.get("total_tokens"),
        "requested_by": requested_by,
    }
    # Clarify-back: the briefed agent asked a question instead of answering
    # (it holds context you don't — e.g. "which dev, launcher or backend?").
    # Forward the structured question so you can answer it by sending the choice
    # back into this same session_id; that continues the exchange as chat.
    if payload.get("clarify_request") is not None:
        result["clarify_request"] = payload.get("clarify_request")
    if payload.get("relay_chain") is not None:
        result["relay_chain"] = payload.get("relay_chain")
    if not result["ok"]:
        result["error"] = str(payload.get("error") or payload.get("blocker") or "relay turn failed")[:400]
        if payload.get("error_kind"):
            result["error_kind"] = str(payload.get("error_kind"))[:60]
        result["exit_code"] = exit_code
    return json.dumps(result, indent=2, default=str)


def _parse_last_json_object(raw: str):
    if not raw:
        return None
    # The handler emits exactly one JSON object in non-stream mode, but stay
    # tolerant of stray log lines before it.
    start = raw.find("{")
    while start != -1:
        candidate = raw[start:]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            start = raw.find("{", start + 1)
    return None


# --------------------------------------------------------------------------- #
# Read-only companions: list your threads / review a thread before continuing. #
#                                                                             #
# Both derive from EXISTING stores (persona-instance roster + persona-chat     #
# history / SessionDB read paths) — no new store, no new index, no mint. They  #
# resolve a target's DEFAULT thread through the SAME chokepoint the send lane  #
# uses (``resolve_default_chat_session_id_for_instance``), so what they list   #
# is exactly the thread an omitted-session ``agent_chat_send`` would continue. #
# --------------------------------------------------------------------------- #


AGENT_CHAT_THREADS_SCHEMA = {
    "name": "agent_chat_threads",
    "description": (
        "List your agent-to-agent chat threads with teammates on your level: persona id, display name, @personainst_* handle, and the default thread's session/title/activity when one exists. Read-only. Disambiguator: lists threads; agent_chat_open reads one, agent_chat_send sends."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "persona_id": {
                "type": "string",
                "description": (
                    "Optional filter: a single persona id or personainst_* handle to list just that "
                    "teammate's thread. Omit to list every reachable teammate."
                ),
            },
        },
        "required": [],
    },
}


AGENT_CHAT_OPEN_SCHEMA = {
    "name": "agent_chat_open",
    "description": (
        "Read the recent message tail of your shared thread with ONE teammate (persona id, or a @personainst_* handle for a specific instance). Read-only; never creates a session. Disambiguator: agent_chat_open READS a thread; agent_chat_send replies; agent_chat_threads lists your threads."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "persona_id": {
                "type": "string",
                "description": (
                    "Target teammate: a persona id (e.g. 'dev', reaches the canonical primary "
                    "instance) or a personainst_* handle (reaches THAT specific instance). "
                    "Display names are not accepted."
                ),
            },
            "session_id": {
                "type": "string",
                "description": (
                    "Optional specific thread to review. Omit to review the default thread with the "
                    "target. Must belong to the target's chat lane; otherwise the read is refused."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "How many of the newest messages to return. Default 20, clamped to 1..40.",
                "default": 20,
            },
        },
        "required": ["persona_id"],
    },
}


def _scope_off() -> bool:
    return (os.environ.get("HERMES_AGENT_CHAT_SCOPE") or "open").strip().lower() == "off"


def _canonical_persona_token(value) -> str:
    from agent_runtime.persona_assignments import safe_assignment_token

    return safe_assignment_token(value)


def agent_chat_threads(*, persona_id=None):
    if _scope_off():
        return _refusal(
            "agent_chat is disabled on this runtime (HERMES_AGENT_CHAT_SCOPE=off). "
            "Tell the operator instead of retrying."
        )

    from agent_runtime.config import ensure_persisted_personas, load_agent_runtime_config
    from agent_runtime.persona_assignments import (
        PersonaInstanceStore,
        canonical_persona_instance_id,
        resolve_default_chat_session_id_for_instance,
        safe_assignment_text,
    )
    from agent_runtime.persona_chat_history import persona_chat_history_summary
    from agent_runtime.worker_sessions import WorkerSessionStore
    from hermes_cli import harness as _harness

    # Optional filter → the canonical persona the caller means (accepts a handle).
    # A bare persona id lists ALL of that persona's instances; a personainst_*
    # handle narrows to THAT specific instance.
    wanted_persona = None
    wanted_instance_id = None
    filter_token = str(persona_id or "").strip()
    if filter_token:
        try:
            wanted_persona = _harness._resolve_mission_chat_persona_id(filter_token, filter_token)
        except ValueError as exc:
            return _refusal(safe_assignment_text(str(exc), limit=240), error_kind="unsupported_persona")
        if _looks_like_instance_handle(filter_token):
            wanted_instance_id = canonical_persona_instance_id(filter_token, persona_id=wanted_persona)

    cfg = load_agent_runtime_config()
    store = PersonaInstanceStore()
    store.derive_from_workers(list(ensure_persisted_personas(cfg)), WorkerSessionStore().list_all())
    instances = store.list_all()

    # Title / last-activity / message-count come from the SAME projection the
    # persona-chat-history frame uses; keyed by the session the row renders under.
    history_by_session: dict[str, dict] = {}
    try:
        for row in persona_chat_history_summary(persona_instances=instances):
            session_id = row.get("session_id")
            if session_id:
                history_by_session[session_id] = row
    except Exception:  # pragma: no cover - defensive; a projection glitch must not blank the list
        history_by_session = {}

    # ONE ROW PER INSTANCE: a persona can run more than one live instance
    # (canonical primary + placement-backed siblings). Each is addressed by its
    # OWN handle and threads its OWN default session — collapsing them to the
    # persona's canonical channel would make siblings unreachable/invisible.
    threads = []
    seen_handles: set[str] = set()
    for instance in instances:
        instance_persona = safe_assignment_text(getattr(instance, "persona_id", None), limit=160)
        instance_handle = safe_assignment_text(getattr(instance, "id", None), limit=160)
        if not instance_persona or not instance_handle:
            continue
        # Only real, reachable teammates: the address must resolve as an
        # agent_chat_send target (skips mothballed/unroutable rows honestly).
        try:
            reachable_persona = _harness._resolve_mission_chat_persona_id(instance_persona, instance_persona)
        except ValueError:
            continue
        if wanted_persona is not None and _canonical_persona_token(reachable_persona) != _canonical_persona_token(wanted_persona):
            continue
        if wanted_instance_id is not None and instance_handle != wanted_instance_id:
            continue
        if instance_handle in seen_handles:
            continue
        seen_handles.add(instance_handle)
        # Resolve THIS instance's default thread WITHOUT minting — honest "no
        # thread yet" when it has never chatted.
        session_id = resolve_default_chat_session_id_for_instance(
            store, persona_id=reachable_persona, persona_instance_id=instance_handle
        )
        entry = {
            "persona_id": reachable_persona,
            "persona_instance_id": instance_handle,
            "display_name": safe_assignment_text(getattr(instance, "display_name", None), limit=120) or instance_handle,
            "handle": instance_handle,
            "session_id": session_id,
            "has_thread": bool(session_id),
        }
        row = history_by_session.get(session_id) if session_id else None
        if row is not None:
            entry["title"] = row.get("title")
            entry["last_activity"] = row.get("updated_at")
            entry["message_count"] = row.get("message_count")
        threads.append(entry)

    threads.sort(key=lambda item: (0 if item["has_thread"] else 1, item["persona_id"], item["handle"]))
    return json.dumps({"ok": True, "count": len(threads), "threads": threads}, default=str)


def _session_belongs_to_chat_lane(session_id: str, *, handle: str, default_session: str | None) -> bool:
    """True when ``session_id`` is the target instance's chat lane.

    Tight by design (this is 'review OUR thread', not a transcript browser): the
    target instance's current default pointer, or a session minted for exactly
    that instance — ``persona_chat_<handle>_<12 hex>`` (see
    ``persona_chat_session_id_for``). The trailing segment MUST be a bare 12-hex
    suffix so a sibling's session (``persona_chat_<handle>_agent_2_<hex>``) is not
    swallowed by the primary's prefix: ``personainst_qa`` must not match
    ``personainst_qa_agent_2``'s session. Anything else — another teammate's or
    sibling's chat, a task/worker session — is foreign and refused.
    """
    if default_session and session_id == default_session:
        return True
    if not handle:
        return False
    prefix = f"persona_chat_{handle}_"
    if not session_id.startswith(prefix):
        return False
    tail = session_id[len(prefix):]
    return len(tail) == 12 and all(ch in "0123456789abcdef" for ch in tail.lower())


def agent_chat_open(*, persona_id, session_id=None, limit=20):
    if _scope_off():
        return _refusal(
            "agent_chat is disabled on this runtime (HERMES_AGENT_CHAT_SCOPE=off). "
            "Tell the operator instead of retrying."
        )

    from agent_runtime.config import ensure_persisted_personas, load_agent_runtime_config
    from agent_runtime.persona_assignments import (
        PersonaInstanceStore,
        canonical_chat_instance_id,
        resolve_default_chat_session_id_for_instance,
        safe_assignment_text,
    )
    from agent_runtime.persona_chat_history import (
        MAX_PERSONA_CHAT_MESSAGE_TAIL,
        persona_chat_session_messages,
    )
    from agent_runtime.worker_sessions import WorkerSessionStore
    from hermes_cli import harness as _harness

    target = str(persona_id or "").strip()
    if not target:
        return _refusal("agent_chat_open requires a persona_id.")
    try:
        resolved_persona = _harness._resolve_mission_chat_persona_id(target, target)
    except ValueError as exc:
        return _refusal(safe_assignment_text(str(exc), limit=240), error_kind="unsupported_persona")
    # A personainst_* handle targets THAT specific instance's thread (a persona may
    # run several); a bare persona id targets the canonical primary. Preserved by
    # canonical_persona_instance_id (placement-backed sibling ids survive).
    target_instance_id = target if _looks_like_instance_handle(target) else None

    try:
        bounded = max(1, min(int(limit or 20), MAX_PERSONA_CHAT_MESSAGE_TAIL))
    except (TypeError, ValueError):
        bounded = 20

    cfg = load_agent_runtime_config()
    store = PersonaInstanceStore()
    store.derive_from_workers(list(ensure_persisted_personas(cfg)), WorkerSessionStore().list_all())

    handle = canonical_chat_instance_id(resolved_persona, target_instance_id)
    default_session = resolve_default_chat_session_id_for_instance(
        store, persona_id=resolved_persona, persona_instance_id=target_instance_id
    )

    requested_session = (str(session_id).strip() or None) if session_id else None
    if requested_session is not None:
        if not _session_belongs_to_chat_lane(requested_session, handle=handle, default_session=default_session):
            return _refusal(
                f"session {requested_session!r} is not part of {resolved_persona}'s chat lane; "
                "agent_chat_open only reviews your shared thread with the target, not arbitrary sessions.",
                error_kind="foreign_session",
                target_persona=resolved_persona,
            )
        target_session = requested_session
    else:
        target_session = default_session

    if not target_session:
        # Never chatted — honest empty result, no session minted.
        return json.dumps(
            {
                "ok": True,
                "target_persona": resolved_persona,
                "handle": handle,
                "session_id": None,
                "has_thread": False,
                "count": 0,
                "messages": [],
            },
            default=str,
        )

    data = persona_chat_session_messages(session_id=target_session, limit=bounded)
    messages = [
        {
            "role": message.get("role"),
            "text": message.get("text"),
            "timestamp": message.get("timestamp"),
        }
        for message in (data.get("messages") or [])
    ]
    return json.dumps(
        {
            "ok": True,
            "target_persona": resolved_persona,
            "handle": handle,
            "session_id": target_session,
            "has_thread": True,
            "count": len(messages),
            "redaction_status": data.get("redaction_status"),
            "messages": messages,
        },
        default=str,
    )


registry.register(
    name="agent_chat_send",
    toolset="agent_chat",
    schema=AGENT_CHAT_SEND_SCHEMA,
    handler=lambda args, **kw: agent_chat_send(
        persona_id=args.get("persona_id"),
        message=args.get("message"),
        session_id=args.get("session_id"),
        new_session=args.get("new_session", False),
        max_seconds=args.get("max_seconds", 240),
        requested_by_session=kw.get("session_id"),
    ),
    description="Send a chat message to another Harness persona and return their reply (agent-to-agent chat).",
    emoji="🤝",
)

registry.register(
    name="agent_chat_threads",
    toolset="agent_chat",
    schema=AGENT_CHAT_THREADS_SCHEMA,
    handler=lambda args, **kw: agent_chat_threads(persona_id=args.get("persona_id")),
    description="List your agent-to-agent chat threads with reachable teammates (read-only, no mint).",
    emoji="🧵",
)

registry.register(
    name="agent_chat_open",
    toolset="agent_chat",
    schema=AGENT_CHAT_OPEN_SCHEMA,
    handler=lambda args, **kw: agent_chat_open(
        persona_id=args.get("persona_id"),
        session_id=args.get("session_id"),
        limit=args.get("limit", 20),
    ),
    description="Review the recent message tail of your shared thread with a teammate (read-only, no mint).",
    emoji="📖",
)
