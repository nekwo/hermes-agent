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

V1 scope contract:
- relay depth is capped at 1 (the target's turn cannot relay onward); chained
  orchestration needs loop detection and is deliberately deferred;
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
import threading
import uuid
from types import SimpleNamespace

from tools.registry import registry

logger = logging.getLogger(__name__)

_RELAY_STATE = threading.local()

_REPLY_LIMIT = 8000
_MESSAGE_LIMIT = 12000

AGENT_CHAT_SEND_SCHEMA = {
    "name": "agent_chat_send",
    "description": (
        "Send a chat message to ANOTHER Harness persona (agent-to-agent chat). Use this when the "
        "operator asks you to brief, prompt, deploy, hand off to, or check in with another agent "
        "conversationally — e.g. 'have Neko look at X', 'tell the dev agent to prepare Y'. The "
        "message lands in that persona's own Mission Control chat session and their reply is "
        "returned to you. This does NOT create a tracked goal, start the Mission Daemon, or run "
        "proof gates — use mission_goal_create when the operator wants real tracked work to start. "
        "Pass the persona id (e.g. neko_supervisor, dev, backend_dev, qa), not a display name or "
        "instance id."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "persona_id": {
                "type": "string",
                "description": "Target persona id, e.g. 'neko_supervisor'. Never a personainst_* instance id.",
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
                    "session (repeated sends thread into one conversation)."
                ),
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


def _relay_depth() -> int:
    return int(getattr(_RELAY_STATE, "depth", 0) or 0)


def _refusal(error: str, **extra) -> str:
    return json.dumps({"ok": False, "error": error, **extra})


def agent_chat_send(
    *,
    persona_id,
    message,
    session_id=None,
    max_seconds=240,
    requested_by_session=None,
):
    scope = (os.environ.get("HERMES_AGENT_CHAT_SCOPE") or "open").strip().lower()
    if scope == "off":
        return _refusal(
            "agent_chat_send is disabled on this runtime (HERMES_AGENT_CHAT_SCOPE=off). "
            "Tell the operator instead of retrying."
        )
    if _relay_depth() >= 1:
        return _refusal(
            "agent-to-agent relay depth limit reached: this turn was itself started by "
            "agent_chat_send, and chained relays are not enabled yet. Report back to your "
            "caller instead of relaying onward."
        )
    persona_id = (persona_id or "").strip()
    message = (message or "").strip()
    if not persona_id:
        return _refusal("agent_chat_send requires a persona_id.")
    if persona_id.startswith("personainst_"):
        return _refusal(
            f"'{persona_id}' is an instance id, not a persona id. Pass the persona id "
            "(e.g. neko_supervisor, dev, backend_dev, qa)."
        )
    if not message:
        return _refusal("agent_chat_send requires a non-empty message.")
    if len(message) > _MESSAGE_LIMIT:
        return _refusal(f"message exceeds the {_MESSAGE_LIMIT}-character relay limit; send a briefing, not a dump.")

    try:
        wall_budget = max(10.0, min(float(max_seconds or 240), 600.0))
    except (TypeError, ValueError):
        wall_budget = 240.0

    requested_by = "agent-chat-relay"
    source_token = str(requested_by_session or "").strip()
    if source_token:
        requested_by = f"agent:{source_token[:120]}"

    args = SimpleNamespace(
        persona_id=persona_id,
        persona_instance_id=None,
        session_id=(str(session_id).strip() or None) if session_id else None,
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
    )

    # The CLI handler prints its JSON payload; capture it so a nested reply can
    # never interleave with the OUTER turn's stdout protocol.
    buffer = io.StringIO()
    _RELAY_STATE.depth = _relay_depth() + 1
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
    finally:
        _RELAY_STATE.depth = _relay_depth() - 1

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
    if not result["ok"]:
        result["error"] = str(payload.get("error") or payload.get("blocker") or "relay turn failed")[:400]
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


registry.register(
    name="agent_chat_send",
    toolset="agent_chat",
    schema=AGENT_CHAT_SEND_SCHEMA,
    handler=lambda args, **kw: agent_chat_send(
        persona_id=args.get("persona_id"),
        message=args.get("message"),
        session_id=args.get("session_id"),
        max_seconds=args.get("max_seconds", 240),
        requested_by_session=kw.get("session_id"),
    ),
    description="Send a chat message to another Harness persona and return their reply (agent-to-agent chat).",
    emoji="🤝",
)
