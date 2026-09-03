#!/usr/bin/env python3
"""Agent chat tool — agent-to-agent orchestration over the canonical chat lane.

Lets any chat persona brief, deploy, or steer ANOTHER persona by sending a
message into that persona's own Mission Control chat session ("Alice, deploy
Neko on X" -> Alice calls this tool -> the prompt lands in Neko's session,
Neko replies there, and the whole exchange is visible in Mission Control with
real provenance). This is the CHAT lane: no task, no daemon, no proof gates,
and no tracked-work creation.

Runs fully in-process by invoking the same handler behind
``hermes harness mission-chat message`` (session dedup, transcript persistence,
prompt observability, trace — one canonical lane, nothing re-implemented).
In-process matters: one Hermes process shelling out to another can hit the
``agent.log`` rotation lock, and the operator lane
must never fork a second, slightly different chat pipeline.

Thread contract (V3 — task-scoped dispatch, 2026-07-27): a send that names no
thread opens a FRESH one per dispatched task (``agent_runtime.mission_chat.
dispatch_session_policy``, default ``new_per_dispatch``); continuation is
explicit — pass back the ``session_id`` the reply returned, or ``new_session:
false`` to continue the target's CURRENT default thread (the most recently
established one: every dispatch mint repoints that pointer through
``open_chat``, so it is NOT a stable per-pair thread — naming the ``session_id``
is the only way to continue a SPECIFIC conversation). Every reply carries
``session_established`` {fresh, reason, predecessor_session_id}, and a fresh
thread records its predecessor in the session meta (``_dispatched_from``). The
decision itself belongs to ``agent_runtime.dispatch_session_policy``; this tool
only forwards the caller's tri-state intent uncoerced.

Clarify continuity (2026-07-27): a ``clarify_request`` carries a
``clarify_token``. Echoing it on the reply binds the answer to the thread the
question was asked in — outranking policy AND a stale ``session_id`` — so the
one exchange that MUST stay threaded no longer depends on a model reproducing an
opaque session id. The token resolves in the handler (one ticket-store
authority); this tool forwards it and reports the resulting ``clarify_binding``.

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

import json
import logging
import os
import time
import uuid
from types import SimpleNamespace

from agent_runtime import relay_policy
from agent_runtime.dispatch_session_policy import coerce_optional_flag
from tools.registry import registry

logger = logging.getLogger(__name__)

#: The relay's own reply bound — the same number as ``dispatch_store.REPLY_LIMIT``
#: and ``dispatch_delivery.REPLY_LIMIT`` under a private spelling, and fenced
#: equal to both by ``tests/agent_runtime/test_mirrored_constant_fences.py``.
_REPLY_LIMIT = 8000
_MESSAGE_LIMIT = 12000

AGENT_CHAT_SEND_SCHEMA = {
    "name": "agent_chat_send",
    "description": (
        "Send a conversational message to ANOTHER Harness persona (persona id e.g. neko_supervisor/dev/qa, or a @personainst_* handle for a specific instance; display names refused). Each new task you dispatch starts a FRESH thread by default; to continue an exchange (an in-task follow-up) pass back the session_id their reply returned. Answering their clarifying question: pass back the clarify_token from their clarify_request and your answer lands in that question's thread. new_session=false continues the target's CURRENT default thread (the most recent one) instead. This is conversational only and does not create tracked work."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "persona_id": {
                "type": "string",
                "description": (
                    "Target persona id, e.g. 'neko_supervisor' (reaches the canonical primary "
                    "instance). A personainst_* handle is also accepted and targets THAT specific "
                    "instance — use it to reach a non-primary instance when a persona runs several. "
                    "To reach an agent on a PAIRED INSTALL on another machine, qualify the target "
                    "with that install: '@workstation/dev'. Cross-install sends need wait=false. "
                    "Anything without an @install/ prefix is on this install."
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
                    "Continue THIS exact thread — pass back the session_id a previous reply "
                    "returned to answer their clarifying question or send an in-task follow-up. "
                    "Omit when dispatching a new task. Cannot be combined with new_session=true."
                ),
            },
            "clarify_token": {
                "type": "string",
                "description": (
                    "Answering a teammate's clarifying question: pass back the clarify_token that "
                    "came inside their clarify_request. It puts your answer in the thread the "
                    "question was asked in, so you do not have to get session_id right. Omit for "
                    "anything else. Cannot be combined with new_session=true."
                ),
            },
            "new_session": {
                "type": "boolean",
                "description": (
                    "Omit this: a new task dispatch already gets its own fresh thread. Pass false to "
                    "continue the target's CURRENT default thread instead — that is the most "
                    "recently established one, not a stable per-pair thread, so name the session_id "
                    "when you mean a SPECIFIC conversation. Pass true to force a fresh thread where "
                    "the default would not. Cannot be combined with session_id."
                ),
            },
            "title": {
                "type": "string",
                "description": (
                    "Optional short name for the thread this dispatch opens (e.g. 'Flaky login "
                    "test triage'). Defaults to the opening words of your message. Ignored when "
                    "you continue an existing thread."
                ),
            },
            # No schema ``default``, for the same reason ``new_session`` has
            # none: a provider that materialises schema defaults would send 240
            # on every call, and a wait=false dispatch would then silently be
            # capped at a conversational window instead of the 30-minute
            # background budget. Absent must stay distinguishable from stated.
            "max_seconds": {
                "type": "number",
                "description": (
                    "Wall budget for the target's reply turn. Omit unless you need a specific "
                    "window: waiting sends default to 240s, and a wait=false dispatch defaults to "
                    "the background budget (30 min)."
                ),
            },
            "wait": {
                "type": "boolean",
                "description": (
                    "Pass false to DISPATCH AND KEEP WORKING: the call returns a dispatch_id "
                    "immediately, their turn runs in the background on its own longer budget "
                    "(default 30 min), and their answer is delivered to you as a new message in "
                    "this conversation once you are idle. Use it for anything that takes real time "
                    "— test suites, builds, long reviews — instead of blocking your turn on it. "
                    "Default true: you wait for the reply inline, exactly as before. Check on "
                    "in-flight work with agent_chat_dispatches."
                ),
            },
            "notify_operator": {
                "type": "boolean",
                "description": (
                    "Only meaningful with wait=false. Pass true when the operator is waiting on "
                    "this result: the delivered turn will instruct you to tell them what came back. "
                    "Default false."
                ),
            },
        },
        "required": ["persona_id", "message"],
    },
}


AGENT_CHAT_DISPATCHES_SCHEMA = {
    "name": "agent_chat_dispatches",
    "description": (
        "List the background dispatches YOU sent with agent_chat_send(wait=false): who they went to, what you asked, whether they are still running or finished, and whether their answer has been delivered back to you yet. Read-only, bounded. Use it to check on long-running work without pestering the teammate. Disambiguator: this lists YOUR background dispatches; agent_chat_threads lists your threads; agent_chat_open reads one."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "How many of your most recent dispatches to return. Default 10, clamped to 1..100.",
                "default": 10,
            },
            "state": {
                "type": "string",
                "description": (
                    "Optional filter: 'running' for only what is still in flight, 'done' for only "
                    "what has finished. Omit for both."
                ),
            },
        },
        "required": [],
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
    clarify_token=None,
    new_session=None,
    title=None,
    max_seconds=None,
    wait=None,
    notify_operator=None,
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
    resolved_clarify_token = (str(clarify_token).strip() or None) if clarify_token else None
    # TRI-STATE, not a bool: True = fresh thread, False = continue the target's
    # current default thread, UNSET = let agent_runtime.mission_chat.dispatch_session_policy
    # decide (default: one fresh thread per dispatched task). `bool()` here would
    # collapse "unset" into an explicit "sticky" and silently pin every caller to
    # the old mega-thread behavior.
    new_session = coerce_optional_flag(new_session)
    resolved_title = (str(title).strip() or None) if title else None
    if not persona_id:
        return _refusal("agent_chat_send requires a persona_id.")
    if not message:
        return _refusal("agent_chat_send requires a non-empty message.")
    if len(message) > _MESSAGE_LIMIT:
        return _refusal(f"message exceeds the {_MESSAGE_LIMIT}-character relay limit; send a briefing, not a dump.")
    if new_session is True and resolved_session_id:
        # Contradictory: new_session asks for a fresh thread; session_id names an
        # existing one. Refuse rather than silently picking one — the caller must
        # decide which thread they mean.
        return _refusal(
            "agent_chat_send: new_session=true and session_id are contradictory — omit session_id "
            "to start a fresh thread, or drop new_session to continue that specific thread.",
            error_kind="contradictory_thread_target",
        )
    if new_session is True and resolved_clarify_token:
        # The symmetric contradiction, and the ONE clarify case with no correct
        # reading: a clarify token means "put this answer where the question
        # was", new_session=true means "put it somewhere new". A stale
        # session_id alongside a token is NOT this — the token deliberately wins
        # that one downstream, because getting session_id wrong is exactly the
        # failure the token exists to absorb.
        return _refusal(
            "agent_chat_send: new_session=true and clarify_token are contradictory — a clarify "
            "answer belongs in the thread its question was asked in. Drop new_session to answer "
            "them, or drop clarify_token to dispatch something new.",
            error_kind="contradictory_thread_target",
        )

    # Gateway Stage 7 (R4). SYNTAX first, and before anything else looks at the
    # target: an `@install/target` that will not parse must refuse rather than
    # fall through to the local lane, because falling through would silently
    # address somebody on THIS machine with a briefing written for another one.
    # `None` here is the ordinary case and means "unqualified, i.e. local" —
    # which is a property of the parser rather than a default (see
    # `agent_runtime.gateway_targets`).
    from agent_runtime.gateway_targets import (
        TargetRefusal,
        parse_install_target,
        peer_store_root,
        resolve_install_target,
    )

    install_qualifier = parse_install_target(persona_id)
    if isinstance(install_qualifier, TargetRefusal):
        return _refusal(
            install_qualifier.message,
            error_kind=install_qualifier.reason,
            target_persona=persona_id,
        )

    # Envelope provenance: the chain/deadline for the CURRENT turn, seeded by
    # the mission-chat handler. Depth/cycle policy is decided downstream at
    # the canonical chokepoint; here we only clamp this hop's wall budget to
    # the shared chain deadline and fast-fail when nothing usable is left.
    chain = relay_policy.RELAY_CHAIN.get()
    deadline_epoch = relay_policy.RELAY_DEADLINE.get()

    # Detached or inline? TRI-STATE for the same reason ``new_session`` is —
    # ``bool(None)`` would read "the caller said nothing" as "the caller said
    # inline", which happens to be the right default but would make the two
    # indistinguishable to anything downstream that needs to know which it was.
    detached = coerce_optional_flag(wait) is False
    notify = coerce_optional_flag(notify_operator) is True
    sender_session = str(requested_by_session or "").strip()

    remote_target = None
    if install_qualifier is not None:
        if not detached:
            # **A cross-install send is the DETACHED lane only, and this is a
            # scope statement rather than a limitation discovered late.** The
            # synchronous relay runs the target's turn inside the sender's own
            # turn, under the sender's process lock and the shared chain
            # deadline — a safety story made entirely of facts about THIS
            # process. None of it holds across a machine boundary: the far
            # install's turn obeys its own concurrency cap, its own budget and
            # its own drain, so a sender blocked on it would be blocked on a
            # clock nobody here owns. The durable row is what replaces that,
            # and the durable row is what `wait: false` means.
            return _refusal(
                f"{persona_id} is on another install, and a cross-install send "
                "returns a handle rather than a reply: send it with wait=false "
                "and their answer will arrive as its own message in this "
                "conversation.",
                error_kind="remote_requires_detached",
                target_persona=persona_id,
            )
        if resolved_clarify_token:
            # **R-IP10's one exception, said out loud.** Every local messaging
            # feature crosses an install boundary except this: a clarify token
            # is minted by THIS install's clarify gateway and resolves against
            # THIS install's pending questions, so carrying it to another
            # machine would hand the far side a token it cannot look up. The
            # continuity that DOES cross is the session id — which the dispatch
            # delivery already reported as "Their thread" — so the refusal names
            # the route rather than only the wall.
            return _refusal(
                f"a clarify_token belongs to this install and cannot answer a "
                f"question asked on {install_qualifier.install_ref}. Answer them "
                "by naming their thread instead: send with session_id set to the "
                "session_id your dispatch delivery reported as 'Their thread', "
                "and your answer lands in the conversation the question came "
                "from.",
                error_kind="clarify_token_not_portable",
                target_persona=persona_id,
            )
        remote_target = resolve_install_target(peer_store_root(), install_qualifier)
        if isinstance(remote_target, TargetRefusal):
            # DETERMINISTIC, so it fails fast here and burns no attempt — the
            # `dispatch_delivery.py:123-132` class, reached before any row
            # exists rather than after eight identical retries.
            extra = (
                {"candidates": list(remote_target.candidates)}
                if remote_target.candidates
                else {}
            )
            return _refusal(
                remote_target.message,
                error_kind=remote_target.reason,
                target_persona=persona_id,
                **extra,
            )

    if detached and not sender_session:
        # A detached dispatch is a PROMISE to deliver the answer back into the
        # caller's conversation. With no caller session there is nowhere to
        # deliver it, so refuse the promise rather than run the work and drop
        # the result on the floor — the failure mode a wait:false lane must
        # never have is "it ran and nobody was told".
        return _refusal(
            "agent_chat_send(wait=false) needs a chat session to deliver the reply back into, "
            "and this lane has none. Send it with wait=true (the default) and use the reply "
            "inline.",
            error_kind="async_delivery_unavailable",
            target_persona=persona_id,
        )

    if detached and not _async_delivery_available():
        # The SAME question the capability contract already answers, asked by the
        # lane that most needs it. `async_delivery_supported()` is False here for
        # a concrete reason — this turn's process ends when the turn does, and
        # nothing in it will be alive to notice a background result — and a
        # dispatch accepted on such a lane would leave an orphaned child process
        # writing into a chat thread with no recorder left to settle its row.
        #
        # This is exactly the reasoning `delegate_task` follows when the same
        # flag is False: fall back to the synchronous path and return the result
        # INSIDE the turn that asked for it, rather than promise a delivery the
        # channel cannot make. Refusing here is the same ruling, applied to the
        # tool that made the promise.
        return _refusal(
            "agent_chat_send(wait=false) is not available on this lane: it ends when this turn "
            "ends, so nothing would be left to receive their answer or to record what happened "
            "to the work. Send it with wait=true (the default) and use the reply inline — same "
            "fallback delegate_task takes when a channel cannot take a late completion.",
            error_kind="async_delivery_unavailable",
            target_persona=persona_id,
        )

    sender_persona = _persona_of_chat_root(sender_session) if detached else ""
    if detached and not sender_persona:
        # Admission check, run BEFORE any work starts. The delivery drain
        # addresses its forged turn by resolving this root to a persona; a root
        # it cannot resolve gets a typed ``sender_session_unresolvable`` drop —
        # AFTER the target has already done the work. Running real work whose
        # answer is guaranteed to be discarded is the exact run-and-drop this
        # lane must never have, so the same resolver decides admission here.
        return _refusal(
            "agent_chat_send(wait=false) can only deliver back into a persona chat thread, and "
            "this session does not resolve to one. Send it with wait=true (the default) and use "
            "the reply inline.",
            error_kind="async_delivery_unavailable",
            target_persona=persona_id,
        )

    if detached:
        # RELAY RULING (operator-approved 2026-08-03): a detached dispatch is a
        # NEW CHAIN ROOT with its own deadline, and the chain is still forwarded.
        #
        # Both halves matter. Minting a fresh deadline is the whole point: the
        # shared 4-minute chain budget exists so a synchronous hop cannot
        # out-live the caller who is BLOCKED on it, and nobody is blocked here —
        # clamping a 30-minute background job to whatever was left of a
        # conversational window would kill exactly the work this lane exists to
        # host, and the remaining-budget fast-fail would refuse most dispatches
        # outright. Forwarding the chain unchanged is what keeps that from being
        # a loophole: depth and cycle detection are decided downstream by
        # ``evaluate_relay`` at the handler's canonical persona chokepoint, and
        # they read the CHAIN, not the clock. So A→B→A is still refused across a
        # detach, and the depth ceiling still holds — a detached hop buys time,
        # never reach.
        #
        # The policy itself stays where it lives. Nothing here decides anything:
        # this branch chooses which BUDGET rides the envelope, and the guard
        # that could refuse the send runs, unchanged, in the handler.
        from agent_runtime.config import resolve_mission_chat_dispatch_max_seconds

        try:
            stated = float(max_seconds) if max_seconds is not None else None
        except (TypeError, ValueError):
            stated = None
        wall_budget = float(resolve_mission_chat_dispatch_max_seconds(stated))
        # NO deadline is minted here. The child mints it when the turn actually
        # starts (``agent_chat_dispatch.build_dispatch_argv``), because a
        # dispatch queued behind the concurrency cap would otherwise burn its
        # wall budget sitting in a queue — the budget the sender was told about
        # and the budget the turn got would drift apart with nothing reporting it.
        effective_deadline = None
    else:
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
    #
    # A cross-install target forwards NO local handle: the string after the
    # `/` is spelled in the far install's vocabulary and is split by the far
    # install's own rule (`chat_turn.normalize_peer_chat_execute`), so running
    # this machine's handle test over it would be this machine answering a
    # question only the other one can.
    target_instance_id = (
        None
        if remote_target is not None
        else (persona_id if _looks_like_instance_handle(persona_id) else None)
    )

    args = SimpleNamespace(
        persona_id=persona_id,
        persona_instance_id=target_instance_id,
        session_id=resolved_session_id,
        # Clarify continuity: the handler resolves this token to the thread the
        # question was asked in and outranks everything else with it. Forwarded
        # verbatim — the tool never resolves it (one ticket-store authority, and
        # it lives with the handler that owns the session lane).
        clarify_token=resolved_clarify_token,
        # Fresh-thread lane: the handler mints a new canonical session through the
        # SAME default-session chokepoint (mint= mode), never a tool-side mint —
        # keeping ONE minting authority (the orphaned-relay fix's whole point).
        # Forwarded UNCOERCED (None stays None) so the handler's policy resolver
        # can tell "the caller said nothing" from "the caller said continue".
        new_session=new_session,
        task_id=None,
        goal_id=None,
        # Names the thread only when this send mints one; the handler derives a
        # title from the message when the caller offered none.
        title=resolved_title,
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
        # Sender provenance (envelope field, not guard logic): the sender's
        # chat-root session id lets the handler's target chokepoint scope
        # bare-persona resolution to the SENDER's workspace.
        requested_by_session=source_token or None,
        # Explicit relay envelope — the handler's chokepoint guard reads
        # these; ambient ContextVars never cross a transport boundary.
        relay_chain=list(chain),
        relay_deadline_epoch=effective_deadline,
    )

    if detached:
        # The child is a separate PROCESS, so it takes argv and an environment,
        # not an args object. The spec below is that invocation's whole input;
        # nothing about the turn is smuggled through ambient state.
        return _dispatch_detached(
            spec={
                "persona_id": persona_id,
                "persona_instance_id": target_instance_id,
                "session_id": resolved_session_id,
                "clarify_token": resolved_clarify_token,
                "new_session": new_session,
                "title": resolved_title,
                "message": message,
                "intent_hint": "chat",
                "requested_by": requested_by,
                "requested_by_session": source_token or None,
                "relay_chain": list(chain),
                "max_seconds": wall_budget,
            },
            persona_id=persona_id,
            target_instance_id=target_instance_id,
            sender_session=sender_session,
            sender_persona=sender_persona,
            message=message,
            title=resolved_title,
            notify_operator=notify,
            chain=chain,
            wall_budget=wall_budget,
            remote_target=remote_target,
        )

    # The handler hands its payload dict straight over through the
    # ``payload_sink`` seam. This used to be a ``contextlib.redirect_stdout``
    # capture plus a JSON re-parse of the captured text, which was wrong in two
    # ways worth naming: ``redirect_stdout`` rebinds ``sys.stdout``
    # PROCESS-GLOBALLY (so any other thread in this process — every other serve
    # request — briefly wrote into this buffer), and a payload that already
    # existed as a dict was serialised only to be parsed back. The seam removes
    # both, and it is what makes the detached lane above safe to run concurrently.
    payloads: list[dict] = []
    args.payload_sink = payloads.append
    try:
        # persona_commands.py is exec'd into hermes_cli.harness globals by
        # _load_command_parts(); the handler is NOT importable from the part
        # module itself.
        from hermes_cli import harness as _harness

        exit_code = _harness._cmd_mission_chat_message(args)
    except Exception as exc:  # pragma: no cover - defensive; surfaced to the model
        logger.exception("agent_chat_send relay failed")
        return _refusal(f"{type(exc).__name__}: {exc}", target_persona=persona_id)

    payload = payloads[-1] if payloads else None
    if payload is None:
        return _refusal(
            "relay produced no reply payload",
            target_persona=persona_id,
            exit_code=exit_code,
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
    # Thread lineage: whether this send opened a fresh task-scoped thread, the
    # typed reason, and the thread it superseded. The caller needs it to know
    # that continuing THIS exchange means passing `session_id` back — under the
    # new_per_dispatch default, omitting it opens another fresh thread.
    if payload.get("session_established") is not None:
        result["session_established"] = payload.get("session_established")
    # Where a clarify answer actually landed, and why. Present only when this
    # send carried a token or settled an open question; `overrode_session_id`
    # names a session_id the token outranked, so the override is never silent.
    if payload.get("clarify_binding") is not None:
        result["clarify_binding"] = payload.get("clarify_binding")
    # Clarify-back: the briefed agent asked a question instead of answering
    # (it holds context you don't — e.g. "which dev, launcher or backend?").
    # Forwarded WHOLESALE, which is why the `clarify_token` inside it needs no
    # code here: answer by sending the choice back with that token (or this
    # session_id) and the exchange continues as one conversation.
    if payload.get("clarify_request") is not None:
        result["clarify_request"] = payload.get("clarify_request")
    if payload.get("relay_chain") is not None:
        result["relay_chain"] = payload.get("relay_chain")
    # Ambiguous-target refusal: forward the candidate instance handles so the
    # calling agent can immediately retry against an exact @personainst_ handle
    # (the whole point of the typed refusal — the CLI error text alone is not
    # enough for the model to pick a sibling).
    if payload.get("candidates") is not None:
        result["candidates"] = payload.get("candidates")
    if not result["ok"]:
        result["error"] = str(payload.get("error") or payload.get("blocker") or "relay turn failed")[:400]
        if payload.get("error_kind"):
            result["error_kind"] = str(payload.get("error_kind"))[:60]
        result["exit_code"] = exit_code
    return json.dumps(result, indent=2, default=str)


def _async_delivery_available() -> bool:
    """Whether THIS lane can receive a background completion after the turn ends.

    The one capability contract, consulted rather than re-derived. It is bound
    per request by the mission-chat handler and answers True only for a
    serve-hosted turn whose delivery drain is actually running.
    """

    try:
        from gateway.session_context import (
            async_delivery_declared,
            async_delivery_supported,
        )

        # A POSITIVE declaration is required, not merely a non-False answer.
        # `async_delivery_supported()` returns True for an unbound session, and
        # the mission-chat binding has exactly one call site — so any lane that
        # reaches this tool without going through `_cmd_mission_chat_message`
        # inherits an unexamined True and would be granted a durable promise on
        # the strength of nobody having said otherwise. Both real lanes bind it;
        # this makes the silence a refusal rather than trusting the default.
        return bool(async_delivery_declared() and async_delivery_supported())
    except Exception:  # pragma: no cover - defensive
        # Cannot read the capability ⇒ cannot promise delivery. Fail toward the
        # inline lane, which always works.
        return False


def _persona_of_chat_root(root_session_id) -> str:
    """The persona that owns a chat root, or ``""`` when it owns none.

    Deliberately THE SAME resolver the delivery drain routes on
    (``dispatch_delivery._sender_persona``), not a second one. That is what makes
    the admission check below sound: if this cannot name the sender, the drain
    will not be able to either, and accepting the dispatch would mean running
    real work whose answer is guaranteed to be dropped.
    """

    try:
        from agent_runtime.dispatch_delivery import _sender_persona

        owner = _sender_persona(str(root_session_id or ""))
    except Exception:  # pragma: no cover - defensive
        return ""
    return owner[0] if owner else ""


def _dispatch_homes() -> tuple[str, str]:
    """``(ambient_home, background_work_home)`` for the child process.

    Both come from the existing authorities, and it is worth being precise about
    what the first one actually is, because the name invites a wrong reading.

    ``get_hermes_home()`` is read HERE, inside the sender's turn — which means
    inside that persona's ``persona_profile_context``. So the value is the
    SENDER PERSONA's profile home, not the operator's. That is deliberate and it
    matches the synchronous relay lane, where the target's turn has always run
    nested inside the sender's flipped environment: a dispatched turn resolves
    the same profile-scoped state whether it was awaited or detached. It is
    still a behaviour change worth naming against the ORIGINAL in-process
    dispatch lane, which inherited whatever the process-global happened to be at
    the moment the worker thread ran — a value that depended on which unrelated
    persona turn was in flight, and was therefore not reliably anything.

    Reading it here rather than in the supervisor is what makes it deterministic
    at all: by the time the supervisor thread spawns, another persona turn may
    have flipped the process-global out from under it.

    ``get_hermes_background_work_home()`` is the one resolver for where
    background work is recorded, so the child's own background writers land
    exactly where this parent, the drain and the Activity projection read.
    """

    from hermes_constants import get_hermes_background_work_home, get_hermes_home

    return str(get_hermes_home()), str(get_hermes_background_work_home())


def _dispatch_detached(
    *,
    spec,
    persona_id,
    target_instance_id,
    sender_session,
    sender_persona,
    message,
    title,
    notify_operator,
    chain,
    wall_budget,
    remote_target=None,
):
    """Record a detached dispatch durably, queue its child turn, return the handle.

    ORDER IS THE CONTRACT. The durable row is written BEFORE the work is handed
    to the supervisor and before the caller is told anything, so there is no
    window in which the target's turn is running (or has already finished)
    against a dispatch nothing remembers. Written after, a process that died
    early would leave work whose result had nowhere to go and no record that it
    was ever asked for; a caller told ``dispatched: true`` for a row that failed
    to persist would wait forever for a delivery that can never be attempted.
    A store failure therefore REFUSES the dispatch instead of running it blind.

    **Gateway Stage 7: the row is the same row when the target is on another
    install.** Everything above is unchanged — the row is written first, on the
    SENDER's install, and the sender's operator sees the outstanding ask in
    Activity exactly as they do for a local one. What changes is one key on the
    spec, and therefore which leg the supervisor takes: a spec carrying
    ``remote_install_id`` is performed by a peer-tier call instead of by a child
    process. There is no distributed row and no two-phase state to reconcile;
    install B records the turn it ran in its own chat store, which is its own
    business and not this row's.
    """

    from agent_runtime.config import mission_chat_dispatch_max_concurrent
    from agent_runtime.dispatch_store import mint_dispatch_id, record_dispatch
    from tools.agent_chat_dispatch import dispatch_detached_turn

    dispatch_id = mint_dispatch_id()
    # The delivery turn is deduped on this id (see dispatch_delivery), so it is
    # minted HERE, once, and travels with the row.
    spec["client_message_id"] = f"agent-dispatch-{dispatch_id}"
    ambient_home, background_home = _dispatch_homes()
    spec["hermes_home"] = ambient_home
    spec["head_home"] = background_home
    if remote_target is not None:
        # The ID, not the name. A display name is chrome two installs can share
        # (Stage 6's field note #4), and the supervisor dials by id.
        spec["remote_install_id"] = remote_target.install_id
        spec["remote_display_name"] = remote_target.display_name
        spec["remote_target"] = remote_target.target
    started_at = time.time()
    try:
        record_dispatch(
            dispatch_id=dispatch_id,
            sender_session_id=sender_session,
            # The sender's own persona, already resolved by the admission check
            # above through the SAME resolver the drain routes on. Recorded
            # rather than left blank so a store row answers "who asked for this"
            # without a second lookup.
            sender_persona_id=sender_persona,
            target_persona=persona_id,
            target_instance_id=target_instance_id or "",
            title=title or "",
            ask=message,
            notify_operator=bool(notify_operator),
            relay_chain=list(chain),
            dispatched_at=started_at,
            remote_install_id=(
                "" if remote_target is None else remote_target.install_id
            ),
        )
    except Exception as exc:
        logger.exception("agent_chat_send could not record dispatch %s", dispatch_id)
        return _refusal(
            f"could not record the dispatch durably ({type(exc).__name__}); nothing was sent. "
            "Send it with wait=true instead.",
            error_kind="dispatch_store_unavailable",
            target_persona=persona_id,
        )

    try:
        dispatch_detached_turn(
            dispatch_id=dispatch_id,
            spec=spec,
            max_concurrent=mission_chat_dispatch_max_concurrent(),
        )
    except Exception as exc:
        from agent_runtime.dispatch_store import STATE_ERROR, record_completion

        # The row exists and can never complete on its own, so settle it here.
        # It becomes a delivered "this never started" message rather than a
        # dispatch that stays `running` in the operator's HUD forever.
        record_completion(
            dispatch_id,
            state=STATE_ERROR,
            error=f"the dispatch could not be queued: {type(exc).__name__}",
        )
        logger.exception("agent_chat_send could not queue dispatch %s", dispatch_id)
        return _refusal(
            f"could not start the background dispatch ({type(exc).__name__}).",
            error_kind="dispatch_queue_failed",
            target_persona=persona_id,
        )

    return json.dumps(
        {
            "ok": True,
            "dispatched": True,
            "dispatch_id": dispatch_id,
            "target_persona": persona_id,
            "session_id": None,
            "started_at": started_at,
            "max_seconds": wall_budget,
            "notify_operator": bool(notify_operator),
            "relay_chain": list(chain),
            # Say plainly what happens next. An agent that thinks this call
            # failed to return a reply will re-send; an agent that knows the
            # answer is coming as its own message will move on, which is the
            # entire behaviour change this lane is for.
            "next_expected": (
                "Their reply is NOT in this result — they are working on it now. It will arrive "
                "as a new message in this conversation when they finish and you are idle. Carry "
                "on with something else; do not re-send. Use agent_chat_dispatches to check "
                "whether it is still running."
            ),
        },
        indent=2,
        default=str,
    )


def agent_chat_dispatches(*, limit=10, state=None, requested_by_session=None):
    """List the caller's background dispatches. Read-only; creates nothing."""

    if _scope_off():
        return _refusal(
            "agent_chat is disabled on this runtime (HERMES_AGENT_CHAT_SCOPE=off). "
            "Tell the operator instead of retrying."
        )

    from agent_runtime.dispatch_store import STATE_RUNNING, list_dispatches
    from tools.agent_chat_dispatch import summarize_for_caller

    scope = str(requested_by_session or "").strip()
    if not scope:
        # Scoped to the caller's own chat root. With no caller identity the
        # honest answer is "I cannot tell which are yours", NOT everyone's.
        return json.dumps(
            {
                "ok": True,
                "count": 0,
                "dispatches": [],
                "next_expected": (
                    "this lane carries no chat session, so there are no dispatches of yours to list"
                ),
            }
        )

    wanted = str(state or "").strip().lower()
    try:
        bounded = max(1, min(int(limit or 10), 100))
    except (TypeError, ValueError):
        bounded = 10

    rows = list_dispatches(sender_session_id=scope, limit=bounded)
    if wanted == "running":
        rows = [row for row in rows if row.get("state") == STATE_RUNNING]
    elif wanted == "done":
        rows = [row for row in rows if row.get("state") != STATE_RUNNING]

    summaries = [summarize_for_caller(row) for row in rows]
    running = sum(1 for row in summaries if row["state"] == STATE_RUNNING)
    return json.dumps(
        {
            "ok": True,
            "count": len(summaries),
            "running": running,
            "dispatches": summaries,
            "next_expected": (
                "still running — their full reply will be delivered to you as a new message when "
                "they finish; nothing to do"
                if running
                else "nothing in flight"
            ),
        },
        indent=2,
        default=str,
    )


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
                    "teammate's thread. Omit to list your addressable teammates on this level — each "
                    "persona's deliberate placement, with the plumbing canonical row shown only when "
                    "the persona has no placement on your level. Prefix with @install/ (e.g. "
                    "'@mac/dev') to list a teammate on another paired install instead; "
                    "agent_chat_installs names the installs you can reach."
                ),
            },
        },
        "required": [],
    },
}


AGENT_CHAT_OPEN_SCHEMA = {
    "name": "agent_chat_open",
    "description": (
        "Review the recent message tail of a thread with ONE teammate (persona id, or a @personainst_* handle for a specific instance) — 'what did we last say to each other?' before you continue it or inspect what a dispatched task actually said. Reviews their current default thread, or the session_id you name. Read-only; never creates a session. Disambiguator: agent_chat_open READS a thread; agent_chat_send replies; agent_chat_threads lists your threads."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "persona_id": {
                "type": "string",
                "description": (
                    "Target teammate: a persona id (e.g. 'dev' — reaches the persona's deliberate "
                    "placement on your level, or the canonical channel when it has none) or a "
                    "personainst_* handle (reaches THAT specific instance). Display names are not "
                    "accepted."
                ),
            },
            "session_id": {
                "type": "string",
                "description": (
                    "Optional specific thread to review. Omit to review the default thread with the "
                    "target. Must belong to the target's chat lane; otherwise the read is refused. "
                    "REQUIRED when persona_id names another install (@mac/dev): there is no shared "
                    "default thread across installs — pass the session_id your dispatch delivery "
                    "reported as 'Their thread'."
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


AGENT_CHAT_INSTALLS_SCHEMA = {
    "name": "agent_chat_installs",
    "description": (
        "List the other INSTALLS (machines) paired with this one — which are reachable right now, and optionally who is on one of them. Read-only: it never pairs, never mints and never sends. Use it before addressing @install/persona so you name a machine that is actually reachable and a teammate that is actually there. Disambiguator: agent_chat_installs lists MACHINES; agent_chat_threads lists teammates on this machine (or, with @install/, on one of theirs)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "install": {
                "type": "string",
                "description": (
                    "Optional: an install's name or id (from a previous call) to fetch THAT "
                    "install's roster of addressable agents. Omit to list the paired installs "
                    "themselves without contacting any of them."
                ),
            },
        },
        "required": [],
    },
}


AGENT_CHAT_LOG_PATH_SCHEMA = {
    "name": "agent_chat_log_path",
    "description": (
        "Get the FILE PATH of a teammate thread's transcript log so you can grep/glob/tail it with your own file tools — use this when 40 messages is not enough, when you need to search a long thread for something specific, or when you want to watch what a teammate is doing while they are still working. The file is append-only JSONL and redaction-safe. It holds the thread's materialized history, and it KEEPS GROWING: appended as they happen are operator/relay messages, mid-turn steers, each turn's final reply, and compact tool start/finish lines (so you can see what they are doing right now). Not appended live: the runtime's non-final rows — the intermediate assistant messages between tool calls. Those are in the materialized history but will not show up in a live tail, so do not read their absence as 'it never happened'. Disambiguator: agent_chat_log_path gives you the thread as a greppable file; agent_chat_open is the quick bounded 40-message tail; agent_chat_threads lists your threads; agent_chat_send sends. Read-only: it never creates a chat session and never sends anything."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "persona_id": {
                "type": "string",
                "description": (
                    "Target teammate: a persona id (e.g. 'dev') or a personainst_* handle for a "
                    "specific instance. Display names are not accepted."
                ),
            },
            "session_id": {
                "type": "string",
                "description": (
                    "Optional specific thread. Omit for the default thread with the target. Must "
                    "belong to that teammate's chat lane; otherwise the request is refused."
                ),
            },
            "all_threads": {
                "type": "boolean",
                "description": (
                    "Pass true to get a path for EVERY thread you share with this teammate instead "
                    "of just the current default one. Useful when a task was dispatched into its own "
                    "thread and you do not know which."
                ),
                "default": False,
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


def agent_chat_threads(*, persona_id=None, requested_by_session=None):
    if _scope_off():
        return _refusal(
            "agent_chat is disabled on this runtime (HERMES_AGENT_CHAT_SCOPE=off). "
            "Tell the operator instead of retrying."
        )

    # S2b (R-S2-12). SYNTAX first, before anything looks at the filter: an
    # ``@install/target`` that will not parse must refuse rather than fall
    # through to the local roster, which would list teammates on THIS machine
    # under a heading naming another. ``None`` means local, exactly as before.
    remote = _remote_roster_rows(persona_id)
    if remote is not None:
        return remote

    from agent_runtime import workspace_scope
    from agent_runtime.config import ensure_persisted_personas, load_agent_runtime_config
    from agent_runtime.persona_assignments import (
        PersonaInstanceStore,
        canonical_persona_instance_id,
        is_canonical_persona_channel,
        resolve_default_chat_session_id_for_instance,
        safe_assignment_text,
        sender_scope_workspace_id,
    )
    from agent_runtime.persona_chat_history import persona_chat_history_summary
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
    store.ensure_for_personas(list(ensure_persisted_personas(cfg)))
    instances = store.list_all()

    # The DEFAULT / bare-persona listing is the sender-scoped ADDRESSABLE roster:
    # each persona's canonical row is shadowed behind an in-scope placement, and
    # out-of-scope placements are hidden — so an agent is offered the deliberate
    # placements on its own level, not the plumbing canonical rows. Sender scope
    # comes from the caller's chat-root session (threaded from the registry
    # handler), falling back to the active workspace. An explicit personainst_*
    # filter (wanted_instance_id) is deliberate targeting and BYPASSES the shadow:
    # that exact instance stays listable cross-workspace (ruling — explicit handle
    # targeting is never shadowed).
    if wanted_instance_id is None:
        scope_workspace_id = sender_scope_workspace_id(
            requested_by_session, instance_store=store
        )
        instances = workspace_scope.addressable_roster(
            instances,
            scope_workspace_id=scope_workspace_id,
            is_canonical=is_canonical_persona_channel,
        )

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


def _resolve_chat_lane_target(persona_id, *, requested_by_session=None, verb="agent_chat_open"):
    """Resolve a teammate address to THIS caller's chat lane with them.

    ONE authority for "which thread is *our* thread with this teammate", shared
    by the read-only companions: ``agent_chat_open`` reads its bounded tail,
    ``agent_chat_log_path`` hands over its live log file. Keeping the addressing
    and default-thread resolution here is what makes the scope guard impossible
    to drift between the two — a widening in one would otherwise be invisible in
    the other.

    Returns ``(target, refusal_json)``; exactly one of the two is ``None``.
    """

    from agent_runtime import workspace_scope
    from agent_runtime.config import ensure_persisted_personas, load_agent_runtime_config
    from agent_runtime.persona_assignments import (
        PersonaInstanceStore,
        canonical_chat_instance_id,
        is_canonical_persona_channel,
        resolve_default_chat_session_id_for_instance,
        safe_assignment_text,
        sender_scope_workspace_id,
    )
    from hermes_cli import harness as _harness

    target = str(persona_id or "").strip()
    if not target:
        return None, _refusal(f"{verb} requires a persona_id.")
    try:
        resolved_persona = _harness._resolve_mission_chat_persona_id(target, target)
    except ValueError as exc:
        return None, _refusal(
            safe_assignment_text(str(exc), limit=240), error_kind="unsupported_persona"
        )
    # A personainst_* handle targets THAT specific instance's thread (a persona may
    # run several) and is used as-is; a BARE persona id is resolved through the
    # sender-scoped addressable roster below (placements shadow canonical), not
    # collapsed straight onto the canonical channel.
    target_instance_id = target if _looks_like_instance_handle(target) else None

    cfg = load_agent_runtime_config()
    store = PersonaInstanceStore()
    store.ensure_for_personas(list(ensure_persisted_personas(cfg)))

    # A BARE persona resolves through the sender-scoped ADDRESSABLE roster: a
    # single in-scope placement is the target (placements shadow canonical), so
    # "open your thread with qa" reviews the deliberate placement's lane, not the
    # plumbing canonical channel. With no in-scope placement (or two — which the
    # send guard would refuse) the canonical channel is the fallback. An explicit
    # personainst_* handle is deliberate targeting and is used as-is, never
    # shadowed.
    if target_instance_id is None:
        scope_workspace_id = sender_scope_workspace_id(
            requested_by_session, instance_store=store
        )
        addressable = workspace_scope.addressable_roster(
            (
                instance
                for instance in store.list_all()
                if getattr(instance, "persona_id", None) == resolved_persona
            ),
            scope_workspace_id=scope_workspace_id,
            is_canonical=is_canonical_persona_channel,
        )
        placements = [
            instance
            for instance in addressable
            if not is_canonical_persona_channel(instance)
        ]
        if len(placements) == 1:
            target_instance_id = placements[0].id

    return (
        SimpleNamespace(
            persona=resolved_persona,
            instance_id=target_instance_id,
            handle=canonical_chat_instance_id(resolved_persona, target_instance_id),
            default_session=resolve_default_chat_session_id_for_instance(
                store, persona_id=resolved_persona, persona_instance_id=target_instance_id
            ),
            store=store,
        ),
        None,
    )


def agent_chat_open(*, persona_id, session_id=None, limit=20, requested_by_session=None):
    if _scope_off():
        return _refusal(
            "agent_chat is disabled on this runtime (HERMES_AGENT_CHAT_SCOPE=off). "
            "Tell the operator instead of retrying."
        )

    from agent_runtime.peer_directory import read_chat_lane_tail

    # S2b (R-S2-12). SYNTAX first, before anything looks at the target, for
    # ``agent_chat_send``'s reason: an ``@install/target`` that will not parse
    # must refuse rather than fall through to the local lane, because falling
    # through would read a thread on THIS machine when the caller named another.
    # ``None`` is the ordinary case and means local — a property of the parser
    # rather than a default.
    remote = _remote_thread_read(
        persona_id, session_id=session_id, limit=limit
    )
    if remote is not None:
        return remote

    # ONE implementation, two doors: this body and ``peer.thread.read`` both
    # call ``read_chat_lane_tail``. The lane guard is what stands between
    # "review our thread" and "read any transcript on this machine", and a
    # second copy of it for the far door would be a second place to widen it by
    # accident.
    data = read_chat_lane_tail(
        persona_id,
        session_id=session_id,
        limit=limit,
        requested_by_session=requested_by_session,
    )
    return json.dumps(data, default=str)


def _bounded_limit(value, *, default: int = 20) -> int:
    """``value`` as an int, or *default*. Never raises, never a string."""

    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _remote_thread_read(persona_id, *, session_id, limit):
    """The ``@install/`` branch of :func:`agent_chat_open`, or ``None`` for local.

    Split out rather than inlined so the local path above reads as it did before
    S2b: one call to the shared reader, nothing else.
    """

    from agent_runtime.gateway_targets import (
        TargetRefusal,
        parse_install_target,
        peer_store_root,
        resolve_install_target,
    )

    parsed = parse_install_target(persona_id)
    if parsed is None:
        return None
    if isinstance(parsed, TargetRefusal):
        return _refusal(
            parsed.message, error_kind=parsed.reason, target_persona=persona_id
        )

    if not str(session_id or "").strip():
        # **There is no "our default thread" on another install.** Locally the
        # default session is the one this pair has been using; across a boundary
        # the far install has its own default, which is the most recently
        # established thread with ANYBODY and is almost never the one the caller
        # means. So the thread must be named — and the refusal says where the
        # name came from, because the dispatch delivery already printed it.
        return _refusal(
            f"{persona_id} is on another install, and there is no shared default "
            "thread across installs. Name the thread: pass session_id — the "
            "session_id your dispatch delivery reported as 'Their thread'.",
            error_kind="remote_session_required",
            target_persona=persona_id,
        )

    root = peer_store_root()
    resolved = resolve_install_target(root, parsed)
    if isinstance(resolved, TargetRefusal):
        extra = (
            {"candidates": list(resolved.candidates)} if resolved.candidates else {}
        )
        return _refusal(
            resolved.message,
            error_kind=resolved.reason,
            target_persona=persona_id,
            **extra,
        )

    from tools.agent_chat_remote import call_peer_method

    outcome = call_peer_method(
        root,
        resolved.install_id,
        "peer.thread.read",
        {
            "target": parsed.target,
            "session_id": str(session_id).strip(),
            # Coerced HERE rather than passed through, because the far handler
            # fences ``limit`` as an int and refuses anything else — and a
            # provider that hands a tool its numbers as strings would otherwise
            # turn a legal call into an invalid-params refusal the caller cannot
            # act on. The far side still clamps to 1..40; this only makes the
            # value the right TYPE.
            "limit": _bounded_limit(limit),
        },
    )
    refusal = outcome.get("refusal")
    if refusal:
        return _refusal(
            str(refusal.get("message") or refusal.get("reason")),
            error_kind=str(refusal.get("reason")),
            target_persona=persona_id,
        )
    result = dict(outcome.get("result") or {})
    result["install"] = {
        "install_id": resolved.install_id,
        "display_name": resolved.display_name,
    }
    # Spelled the way the grammar accepts, so the answer names an address the
    # caller could use again rather than a bare persona id that would resolve
    # locally.
    result["target_persona"] = f"@{parsed.install_ref}/{result.get('target_persona') or parsed.target}"
    return json.dumps(result, default=str)


def agent_chat_log_path(
    *, persona_id, session_id=None, all_threads=None, requested_by_session=None
):
    """Hand back the PATH of a teammate thread's live transcript log.

    The 40-message tail ``agent_chat_open`` returns is the right size for "what
    did we last say"; it is the wrong tool for "search this whole thread" or
    "watch what they are doing right now". SessionDB is not greppable, so the
    runtime keeps an append-only JSONL mirror per session
    (``agent_runtime/chat_live_log.py``) and this tool tells the caller where it
    is. The head agent then uses its OWN file tools on it — no new transport, no
    payload in the tool result, no context flood.

    Live, not an export: the file keeps growing while the teammate works (message
    lines from the persist chokepoints, compact tool lines from the chat progress
    sink), so a caller can tail it mid-task.

    Scope: IDENTICAL to ``agent_chat_open`` — the caller's shared threads with
    that teammate only, via ``_resolve_chat_lane_target`` +
    ``_session_belongs_to_chat_lane``. Handing over a filesystem path is if
    anything a stronger capability than reading a tail, so it gets exactly the
    same guard and the same typed ``foreign_session`` refusal.
    """

    if _scope_off():
        return _refusal(
            "agent_chat is disabled on this runtime (HERMES_AGENT_CHAT_SCOPE=off). "
            "Tell the operator instead of retrying."
        )

    from agent_runtime.chat_live_log import chat_live_log_stats, ensure_chat_live_log

    target, refusal = _resolve_chat_lane_target(
        persona_id, requested_by_session=requested_by_session, verb="agent_chat_log_path"
    )
    if refusal is not None:
        return refusal

    requested_session = (str(session_id).strip() or None) if session_id else None
    if requested_session is not None:
        if not _session_belongs_to_chat_lane(
            requested_session, handle=target.handle, default_session=target.default_session
        ):
            return _refusal(
                f"session {requested_session!r} is not part of {target.persona}'s chat lane; "
                "agent_chat_log_path only hands over logs for your shared threads with the "
                "target, not arbitrary sessions.",
                error_kind="foreign_session",
                target_persona=target.persona,
            )
        wanted = [requested_session]
    elif coerce_optional_flag(all_threads) is True:
        wanted = _chat_lane_session_ids(target)
    else:
        wanted = [target.default_session] if target.default_session else []

    threads = []
    for candidate in wanted:
        # THIS is the lane that materializes history. The chat persist seams
        # deliberately never do (they would pay the full curated projection —
        # turn-journal re-parse per row — inside a live turn); they create a
        # header-only file and append. A deliberate agent request can afford the
        # read, so it completes any file still marked backfill_pending.
        path = ensure_chat_live_log(candidate, materialize=True)
        if path is None:
            continue
        stats = chat_live_log_stats(candidate) or {}
        threads.append(
            {
                "session_id": candidate,
                "path": str(path),
                "bytes": stats.get("bytes", 0),
                "message_count": stats.get("message_count", 0),
                "tool_count": stats.get("tool_count", 0),
                "last_activity": stats.get("last_activity"),
                "rotated_path": stats.get("rotated_path"),
                # Honest on both ways history can be short: never materialized,
                # or materialized only up to the declared bound. Either way the
                # file may start later than the conversation does.
                "history_complete": not (
                    stats.get("backfill_pending", False)
                    or stats.get("backfill_truncated", False)
                ),
                # Not a snapshot: this file keeps growing while the teammate
                # works, so re-reading it later shows new activity.
                "live": True,
            }
        )

    return json.dumps(
        {
            "ok": True,
            "target_persona": target.persona,
            "handle": target.handle,
            "count": len(threads),
            "threads": threads,
            "format": "jsonl",
            # Say exactly what the growing part of the file carries. An agent
            # that believes "full history, live" would draw wrong conclusions
            # from the absence of a mid-turn steer it knows was sent.
            "live_coverage": (
                "appended as they happen: operator/relay messages, mid-turn steers "
                "(steered: true), each turn's final reply, and tool start/finish "
                "lines. The runtime's non-final rows (intermediate assistant messages "
                "between tool calls) are NOT appended live — they appear in the "
                "materialized history, not in the live tail."
            ),
            "next_expected": (
                "grep / tail the path with your own file tools; each line is JSON with "
                "{ts, kind: message|tool, ...}. Re-read later to see new activity."
            )
            if threads
            else "no thread with this teammate yet — nothing to read",
        },
        default=str,
    )


def _chat_lane_session_ids(target) -> list:
    """Every session in the caller's chat lane with *target*, newest last.

    Derived from the SAME history projection ``agent_chat_threads`` reads and
    filtered through the SAME ``_session_belongs_to_chat_lane`` guard, so
    ``all_threads`` can never widen the scope beyond what a single-thread
    request would allow — it only saves the caller from guessing which
    task-scoped thread a dispatch opened.
    """

    from agent_runtime.persona_chat_history import persona_chat_history_summary

    found: list[str] = []
    try:
        rows = persona_chat_history_summary(persona_instances=target.store.list_all())
    except Exception:  # pragma: no cover - a projection glitch must not blank the answer
        rows = []
    for row in rows or []:
        candidate = str((row or {}).get("session_id") or "")
        if not candidate or candidate in found:
            continue
        if _session_belongs_to_chat_lane(
            candidate, handle=target.handle, default_session=target.default_session
        ):
            found.append(candidate)
    if target.default_session and target.default_session not in found:
        found.append(target.default_session)
    return found


def _remote_roster_rows(persona_id):
    """The ``@install/`` branch of :func:`agent_chat_threads`, or ``None`` locally.

    Answers in the LOCAL threads-row shape rather than in the peer method's own
    projection shape, because a tool that returned two different row schemas
    depending on where the teammate lives would make every consumer branch on
    residency to read a name. What differs is the ``handle`` — spelled as the
    full ``@install/handle`` address, so a row an agent reads is a row it can
    send to — and ``has_thread``, which is ``null`` rather than ``false``: this
    install genuinely does not know whether the caller has a thread with that
    far teammate, and ``false`` would be a claim.
    """

    from agent_runtime.gateway_targets import (
        TargetRefusal,
        parse_install_target,
        peer_store_root,
        resolve_install_target,
    )

    parsed = parse_install_target(persona_id)
    if parsed is None:
        return None
    if isinstance(parsed, TargetRefusal):
        # ``@mac/`` is refused by the parser today (``install_qualifier_target_empty``)
        # and STAYS refused: the verb for "everyone on that machine" is
        # ``agent_chat_installs``, and letting an empty target mean "all" would
        # give one spelling two meanings.
        return _refusal(
            parsed.message, error_kind=parsed.reason, target_persona=persona_id
        )

    root = peer_store_root()
    resolved = resolve_install_target(root, parsed)
    if isinstance(resolved, TargetRefusal):
        extra = {"candidates": list(resolved.candidates)} if resolved.candidates else {}
        return _refusal(
            resolved.message,
            error_kind=resolved.reason,
            target_persona=persona_id,
            **extra,
        )

    from agent_runtime.gateway_peers import cache_peer_roster
    from tools.agent_chat_remote import call_peer_method

    outcome = call_peer_method(
        root,
        resolved.install_id,
        "peer.roster.list",
        {"target": parsed.target},
    )
    refusal = outcome.get("refusal")
    if refusal:
        return _refusal(
            str(refusal.get("message") or refusal.get("reason")),
            error_kind=str(refusal.get("reason")),
            target_persona=persona_id,
        )
    result = outcome.get("result") or {}
    rows = [row for row in (result.get("rows") or []) if isinstance(row, dict)]

    # Cache what we just fetched, so the HUD's install lines can render a roster
    # without dialling anything (R-IP11). The fetch already happened; keeping it
    # is free, and NOT keeping it would mean the only way to see who is on
    # another machine is to ask again every turn.
    try:
        cache_peer_roster(
            root,
            resolved.install_id,
            workspace_id=result.get("workspace_id"),
            rows=rows,
        )
    except Exception:  # pragma: no cover - bookkeeping is never the work
        pass

    wanted = str(parsed.target or "").strip()
    install_block = {
        "install_id": resolved.install_id,
        "display_name": resolved.display_name,
    }
    threads = []
    for row in rows:
        handle = str(row.get("handle") or "")
        persona = str(row.get("persona_id") or "")
        if wanted and wanted not in {handle, persona}:
            continue
        threads.append(
            {
                "persona_id": persona,
                "persona_instance_id": handle,
                "handle": f"@{parsed.install_ref}/{handle}",
                "display_name": row.get("label") or handle,
                "has_thread": None,
                "last_activity": row.get("last_turn_at"),
                "install": install_block,
            }
        )
    return json.dumps(
        {
            "ok": True,
            "count": len(threads),
            "install": install_block,
            "workspace_id": result.get("workspace_id"),
            "threads": threads,
        },
        default=str,
    )


def agent_chat_installs(*, install=None, requested_by_session=None):
    """Which other machines this install can reach, and optionally who is on one.

    ``requested_by_session`` is accepted and NOT used, deliberately: every
    handler in this module takes it (the registry threads it in from
    ``kw["session_id"]``), and a directory of MACHINES has no sender scope to
    narrow — the peers of an install are the same set whoever asks. Dropping the
    parameter would make this the one tool whose registration reads differently
    for a reason nobody could see from the call site.

    Two shapes from one verb, and the split is the network cost: with no
    ``install`` it reads THIS install's own two files and DIALS NOTHING; with
    one, it makes exactly one call to that machine and caches what comes back.
    An operator (or an agent) orienting itself should not have to spend a round
    trip per paired machine to find out which ones are worth addressing.

    The rows are ``usable_peers`` verbatim — the SAME predicate the resolver
    reads — so every install listed here is one a send would resolve, and every
    paired install NOT listed is one a send would refuse. A directory that
    offered an edge the resolver had written off would be a list an agent
    learns to distrust.
    """

    if _scope_off():
        return _refusal(
            "agent_chat is disabled on this runtime (HERMES_AGENT_CHAT_SCOPE=off). "
            "Tell the operator instead of retrying."
        )

    from agent_runtime.gateway_peers import usable_peers
    from agent_runtime.gateway_targets import peer_store_root

    root = peer_store_root()

    wanted = str(install or "").strip()
    if wanted:
        return _install_roster(root, wanted)

    try:
        peers = usable_peers(root)
    except Exception as exc:  # pragma: no cover - defensive; an unreadable store
        return _refusal(
            f"this install's peer store could not be read ({type(exc).__name__}), "
            "so no other install can be named.",
            error_kind="peer_store_unreadable",
        )

    installs = []
    for peer in peers:
        cache = peer.cache
        installs.append(
            {
                # The spelling the grammar accepts — a display name when it is
                # unique, otherwise the id — so a value read here is an address
                # ``@ref/target`` resolves rather than one the resolver would
                # refuse as ambiguous.
                "ref": peer.ref,
                "install_id": peer.record.peer_install_id,
                "display_name": (
                    (cache.announced_display_name if cache is not None else None)
                    or peer.record.display_name
                ),
                "endpoints_count": len(peer.record.endpoints),
                "approved_at": peer.record.approved_at,
                "expires_at": peer.record.expires_at,
                "reachability": cache.reachability if cache is not None else "unknown",
                "last_seen": cache.last_seen if cache is not None else None,
                "roster_cached_at": (
                    (cache.roster or {}).get("fetched_at")
                    if cache is not None and isinstance(cache.roster, dict)
                    else None
                ),
            }
        )
    return json.dumps(
        {"ok": True, "count": len(installs), "installs": installs}, default=str
    )


def _install_roster(root, install_ref: str):
    """One install's roster of addressable agents, fetched and cached."""

    from agent_runtime.gateway_peers import cache_peer_roster
    from agent_runtime.gateway_targets import TargetRefusal, resolve_install_ref
    from tools.agent_chat_remote import call_peer_method

    resolved = resolve_install_ref(root, install_ref)
    if isinstance(resolved, TargetRefusal):
        extra = {"candidates": list(resolved.candidates)} if resolved.candidates else {}
        return _refusal(resolved.message, error_kind=resolved.reason, **extra)

    outcome = call_peer_method(
        root, resolved.peer_install_id, "peer.roster.list", {}
    )
    refusal = outcome.get("refusal")
    if refusal:
        return _refusal(
            str(refusal.get("message") or refusal.get("reason")),
            error_kind=str(refusal.get("reason")),
        )
    result = outcome.get("result") or {}
    rows = [row for row in (result.get("rows") or []) if isinstance(row, dict)]
    try:
        cache_peer_roster(
            root,
            resolved.peer_install_id,
            workspace_id=result.get("workspace_id"),
            rows=rows,
        )
    except Exception:  # pragma: no cover - bookkeeping is never the work
        pass
    return json.dumps(
        {
            "ok": True,
            "install": {
                "install_id": resolved.peer_install_id,
                "display_name": resolved.display_name,
            },
            "workspace_id": result.get("workspace_id"),
            "count": len(rows),
            "truncated": bool(result.get("truncated")),
            # Each row spelled as the FULL address, so a roster line is
            # actionable rather than merely informative.
            "roster": [
                {**row, "address": f"@{install_ref}/{row.get('handle')}"}
                for row in rows
            ],
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
        clarify_token=args.get("clarify_token"),
        # No `False` default: an omitted new_session must reach the policy
        # resolver as "unset", not as an explicit request to continue.
        new_session=args.get("new_session"),
        title=args.get("title"),
        # No `240` default here either: an omitted max_seconds must reach the
        # budget resolvers as "unset" so a wait=false dispatch gets the
        # background budget rather than a silently-applied conversational one.
        max_seconds=args.get("max_seconds"),
        # Tri-state like new_session: omitted stays unset (= wait inline).
        wait=args.get("wait"),
        notify_operator=args.get("notify_operator"),
        requested_by_session=kw.get("session_id"),
    ),
    description="Send a chat message to another Harness persona and return their reply, or dispatch it in the background with wait=false (agent-to-agent chat).",
    emoji="🤝",
)

registry.register(
    name="agent_chat_dispatches",
    toolset="agent_chat",
    schema=AGENT_CHAT_DISPATCHES_SCHEMA,
    handler=lambda args, **kw: agent_chat_dispatches(
        limit=args.get("limit", 10),
        state=args.get("state"),
        requested_by_session=kw.get("session_id"),
    ),
    description="List your in-flight and recent background dispatches (read-only).",
    emoji="📡",
)

registry.register(
    name="agent_chat_threads",
    toolset="agent_chat",
    schema=AGENT_CHAT_THREADS_SCHEMA,
    handler=lambda args, **kw: agent_chat_threads(
        persona_id=args.get("persona_id"),
        requested_by_session=kw.get("session_id"),
    ),
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
        requested_by_session=kw.get("session_id"),
    ),
    description="Review the recent message tail of your shared thread with a teammate (read-only, no mint).",
    emoji="📖",
)

registry.register(
    name="agent_chat_installs",
    toolset="agent_chat",
    schema=AGENT_CHAT_INSTALLS_SCHEMA,
    handler=lambda args, **kw: agent_chat_installs(
        install=args.get("install"),
        requested_by_session=kw.get("session_id"),
    ),
    description="List the other installs (machines) paired with this one, or one install's agent roster (read-only, no mint).",
    emoji="🖧",
)

registry.register(
    name="agent_chat_log_path",
    toolset="agent_chat",
    schema=AGENT_CHAT_LOG_PATH_SCHEMA,
    handler=lambda args, **kw: agent_chat_log_path(
        persona_id=args.get("persona_id"),
        session_id=args.get("session_id"),
        all_threads=args.get("all_threads"),
        requested_by_session=kw.get("session_id"),
    ),
    description="Get the file path of a teammate thread's live transcript log to grep/tail the full history (read-only, no mint).",
    emoji="🗂️",
)
