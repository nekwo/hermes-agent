from __future__ import annotations

import json
from pathlib import Path
import time
from urllib.parse import urlparse
from typing import Callable, Protocol

from hermes_constants import get_hermes_home

from . import paths
from .chat_lane_toolsets import scope_chat_lane_toolsets
from .config import chat_lane_restore_toolsets, load_agent_runtime_config
from .context_builder import AgentContext, build_context, render_context
from .decision_contract_registry import prompt_contract_markdown
from .decision_schema import (
    DECISION_SCHEMA,
    AgentDecision,
    DecisionPayloadInvalid,
    DecisionType,
    parse_structured_decision,
    validate_decision_for_role,
)
from .decision_contracts import validate_planning_decision
from .models import AgentPersona, AgentRun
from .mission_chat_clarify import MissionChatClarifyCapture
from .mission_plan import current_plan_stage
from .personas import ALLOWED_TOOLSETS_BY_ROLE, all_registered_toolsets, blocked_tool_names, effective_toolsets, load_bundled_prompt, role_from_persona
from .profile_context import resolve_persona_profile
from .provider_health import assert_provider_health_for_persona
from .profile_runner import AgentRunRequest, AgentRunResult, ProfileAgentRunner, RunBudgetExceeded
from .progress import ChatProgressSink, RunProgressSink
from .repo_context import RepoExecutionContext, capture_repo_baseline, isolated_repo_context_for_run, repo_execution_context_for_task
from .stage_intent import stage_requires_product_edit
from .store import RunStore, _safe_session_id
from .tool_permissions import (
    ChatToolPermissionStore,
    extra_blocked_tools_for_permission_mode,
    permission_mode_is_unbounded,
    permission_options_for_chat,
)


class PersonaRuntime(Protocol):
    def run_tick(self, persona: AgentPersona, ctx: AgentContext, *, run: AgentRun) -> AgentDecision: ...


class GPTPersonaRuntime:
    def __init__(
        self,
        *,
        default_provider: str | None = None,
        default_model: str | None = None,
        credential_pool=None,
        session_db=None,
        agent_factory=None,
        agent_runner: ProfileAgentRunner | None = None,
        persist_agent_session: bool = True,
    ):
        self._default_provider = default_provider
        self._default_model = default_model
        runner_session_db = session_db if persist_agent_session else None
        self._runner = agent_runner or ProfileAgentRunner(
            agent_factory=agent_factory,
            credential_pool=credential_pool,
            session_db=runner_session_db,
        )

    def run_tick(self, persona: AgentPersona, ctx: AgentContext, *, run: AgentRun) -> AgentDecision:
        first_error: DecisionPayloadInvalid | None = None
        for attempt in range(2):
            active_ctx = ctx
            if attempt == 1:
                active_ctx = build_context(
                    ctx.task,
                    ctx.run,
                    recent_events=ctx.recent_events,
                    proof_ids=ctx.proof_ids,
                    requires_repair=True,
                    repair_error=str(first_error),
                )
                active_ctx.context_bundles = list(ctx.context_bundles)
                active_ctx.proof_records = list(ctx.proof_records)
                active_ctx.incident_records = list(ctx.incident_records)
                active_ctx.repo_context = dict(ctx.repo_context) if isinstance(ctx.repo_context, dict) else ctx.repo_context
                if isinstance(ctx.mission_hud, dict) and isinstance(active_ctx.mission_hud, dict):
                    active_ctx.mission_hud = {**ctx.mission_hud, "validation_repair": active_ctx.mission_hud.get("validation_repair")}
                else:
                    active_ctx.mission_hud = active_ctx.mission_hud or ctx.mission_hud
                active_ctx.latest_handoff_packet = dict(ctx.latest_handoff_packet) if isinstance(ctx.latest_handoff_packet, dict) else ctx.latest_handoff_packet
                active_ctx.latest_delivery = dict(ctx.latest_delivery) if isinstance(ctx.latest_delivery, dict) else ctx.latest_delivery
                active_ctx.latest_qa_review = dict(ctx.latest_qa_review) if isinstance(ctx.latest_qa_review, dict) else ctx.latest_qa_review
                active_ctx.autonomy_packet = dict(ctx.autonomy_packet) if isinstance(ctx.autonomy_packet, dict) else ctx.autonomy_packet
            raw = self._invoke_agent(persona, active_ctx, run=run)
            try:
                decision = parse_structured_decision(raw)
                validate_decision_for_role(decision, role_from_persona(persona))
                validate_planning_decision(decision)
                return decision
            except DecisionPayloadInvalid as exc:
                first_error = exc
        raise first_error or DecisionPayloadInvalid("invalid AgentDecision")

    def _invoke_agent(self, persona: AgentPersona, ctx: AgentContext, *, run: AgentRun) -> str:
        binding = resolve_persona_profile(persona)
        if binding.readiness == "missing_profile":
            raise DecisionPayloadInvalid(binding.summary)
        assert_provider_health_for_persona(persona)
        progress_sink = RunProgressSink(run_store=RunStore(), run_id=run.id)
        repo_ctx = _repo_context_for_persona(persona, ctx)
        if repo_ctx is not None:
            try:
                repo_ctx = isolated_repo_context_for_run(repo_ctx, task_id=ctx.task.id, run_id=run.id)
            except ValueError as exc:
                raise DecisionPayloadInvalid(f"Dev run repo isolation failed closed: {exc}") from exc
            ctx.repo_context = _repo_context_for_render(repo_ctx)
            _attach_repo_baseline(run, repo_ctx)
            progress_sink.emit("run.progress", _repo_context_progress_payload(repo_ctx))
        render_started = time.perf_counter()
        user_message = render_context(ctx)
        system_message = build_system_prompt(persona, task_id=run.id)
        timing: dict[str, int] = {}
        timing["prompt_render_ms"] = _emit_timing(progress_sink, "prompt_render", render_started, status="completed")
        provider_started = time.perf_counter()
        progress_sink.emit(
            "run.progress",
            {
                "type": "run.progress",
                "phase": "timing",
                "step": "provider_call",
                "status": "started",
                "summary": "Provider call started.",
                "timing_key": "provider_call_ms",
            },
        )
        try:
            result = self._runner.run(
                AgentRunRequest(
                    profile=binding.hermes_profile,
                    provider=persona.provider or self._default_provider,
                    model=persona.model or self._default_model or "",
                    api_mode=persona.api_mode,
                    enabled_toolsets=effective_toolsets(persona),
                    blocked_tool_names=_blocked_tool_names_for_run(persona, ctx),
                    quiet_mode=True,
                    # Harness owns persona identity and repo-context injection
                    # unless the persona explicitly opts into normal Hermes
                    # core context-file loading. This keeps standard Hermes
                    # behavior unchanged while preventing profile SOUL/manual
                    # session doctrine from outranking AgentDecision rules.
                    skip_context_files=not bool(getattr(persona, "include_core_context_files", False)),
                    skip_memory=not _persona_run_uses_memory(persona, ctx),
                    platform="agent_runtime",
                    session_id=run.session_id,
                    max_iterations=run.iteration_budget,
                    max_wall_seconds=run.max_wall_seconds,
                    max_api_calls=run.max_api_calls,
                    max_total_tokens=run.max_total_tokens,
                    user_message=user_message,
                    system_message=system_message,
                    task_id=run.id,
                    progress_callback=lambda payload: progress_sink.emit(str(payload.get("type", "run.progress")), payload),
                    runtime_root=paths.store_root(),
                    workdir=repo_ctx.workdir if repo_ctx is not None else None,
                    stop_on_repeated_read_search=role_from_persona(persona).value in {"dev", "qa"},
                    tool_budget_limits=_tool_budget_limits(ctx),
                )
            )
        except RunBudgetExceeded as exc:
            run.session_id = exc.session_id or run.session_id
            raise
        timing["provider_call_ms"] = _emit_timing(progress_sink, "provider_call", provider_started, status="completed")
        _apply_llm_metadata(run, result, timing=timing)
        return result.final_response

    def chat_reply(
        self,
        persona: AgentPersona,
        message: str,
        *,
        session_id: str | None = None,
        turn_id: str | None = None,
        max_wall_seconds: float | None = 120.0,
        # No API-call cap on the chat lane — align with base Hermes, where a
        # conversational turn is bounded by the tool-calling loop
        # (AgentRunRequest.max_iterations = 90) plus the wall-clock budget, not a
        # hard call count. Operator chats and agent_chat_send relays share this
        # path; both keep their wall deadline (max_wall_seconds / the shared
        # relay budget), so a runaway turn is caught by time + iterations, not an
        # arbitrary 8 that also throttled ordinary multi-step chat requests.
        max_api_calls: int | None = None,
        max_total_tokens: int | None = None,
        stream_callback: Callable[[str | None], None] | None = None,
        pre_trace_callback: Callable[[dict], None] | None = None,
        trace_callback: Callable[[dict], None] | None = None,
    ) -> AgentRunResult:
        """Run one plain conversational turn for an operator persona chat.

        This deliberately bypasses the decision-contract pipeline: the agent
        gets a conversational system prompt (no decision menu, no task scoping)
        and the operator's raw message, and returns free-text. This is the
        chat-first path — the harness task/decision machinery only engages when
        the operator explicitly asks for work.

        The Harness caller owns the redacted canonical transcript. Live
        operator-chat paths should construct this runtime with
        ``persist_agent_session=False`` so the internal model run cannot create
        a private scratch session that later needs copy-back reconciliation.
        """

        binding = resolve_persona_profile(persona)
        if binding.readiness == "missing_profile":
            raise ValueError(binding.summary)
        assert_provider_health_for_persona(persona)
        clarify_capture = MissionChatClarifyCapture()
        result = self._runner.run(
            AgentRunRequest(
                profile=binding.hermes_profile,
                provider=persona.provider or self._default_provider,
                model=persona.model or self._default_model or "",
                api_mode=persona.api_mode,
                enabled_toolsets=_enabled_toolsets_for_chat(persona, session_id=session_id),
                blocked_tool_names=_blocked_tool_names_for_chat(persona, session_id=session_id),
                quiet_mode=True,
                skip_context_files=not bool(getattr(persona, "include_core_context_files", False)),
                # Profile memory (MEMORY.md / USER.md) is identity-adjacent: it
                # carries the bound profile's worldview into the turn. Honor the
                # persona's include_profile_memory opt-in instead of loading it
                # unconditionally, so a persona bound to a supervisor profile for
                # *capabilities* does not also inherit that profile's memory-model
                # (the Alice "goal->Neko->Dev" mental model that made Neko relay
                # to itself). A persona keeps its own profile's memory when the
                # binding is its own; it drops a borrowed profile's memory.
                skip_memory=not bool(getattr(persona, "include_profile_memory", False)),
                platform=PERSONA_CHAT_SCRATCH_SOURCE,
                session_id=session_id,
                max_wall_seconds=max_wall_seconds,
                max_api_calls=max_api_calls,
                max_total_tokens=max_total_tokens,
                user_message=message,
                system_message=_persona_chat_system_prompt(persona),
                stream_callback=stream_callback,
                clarify_callback=clarify_capture.callback,
                progress_callback=_chat_trace_callback(
                    session_id=session_id,
                    persona=persona,
                    turn_id=turn_id,
                    before_first_trace=pre_trace_callback,
                    on_trace=trace_callback,
                ),
                runtime_root=paths.store_root(),
            )
        )
        ChatToolPermissionStore().consume_turn(persona_id=persona.id, session_id=session_id)
        if clarify_capture.requested and isinstance(result.raw, dict):
            result.raw["clarify_request"] = clarify_capture.request
        return result

    def mission_chat_reply(
        self,
        persona: AgentPersona,
        message: str,
        *,
        session_id: str | None = None,
        permission_session_id: str | None = None,
        turn_id: str | None = None,
        provider_override: str | None = None,
        model_override: str | None = None,
        reasoning_effort: str | None = None,
        surface_prompt: str | None = "",
        max_wall_seconds: float | None = 120.0,
        # No API-call cap on the chat lane — align with base Hermes, where a
        # conversational turn is bounded by the tool-calling loop
        # (AgentRunRequest.max_iterations = 90) plus the wall-clock budget, not a
        # hard call count. Operator chats and agent_chat_send relays share this
        # path; both keep their wall deadline (max_wall_seconds / the shared
        # relay budget), so a runaway turn is caught by time + iterations, not an
        # arbitrary 8 that also throttled ordinary multi-step chat requests.
        max_api_calls: int | None = None,
        max_total_tokens: int | None = None,
        stream_callback: Callable[[str | None], None] | None = None,
        pre_trace_callback: Callable[[dict], None] | None = None,
        trace_callback: Callable[[dict], None] | None = None,
        agent_ready_callback: Callable[[object], Callable[[], None] | None] | None = None,
        preloaded_skill_prompt: str | None = None,
        workspace_agents_content: str | None = None,
        situational_hud_content: str | None = None,
    ) -> AgentRunResult:
        """Run the canonical Mission Control chat path.

        Unlike the older free-floating helper, this uses the normal Hermes
        profile context stack: SOUL.md, profile memory, skills/context files,
        and the profile's standard chat behavior. Mission Control contributes
        only an optional surface prompt, blank by default.

        ``permission_session_id`` resolves the chat-scoped tool permission
        (e.g. an operator-granted ``unbounded`` mode) independently of the run
        ``session_id``. The Mission Control caller passes ``session_id=None`` so
        the runtime does not re-load the transcript it already baked into the
        message, but the permission record is keyed on the real chat session —
        without this, the unbounded grant is silently ignored and the chat falls
        back to the role-default toolset.
        """

        perm_session_id = permission_session_id or session_id
        binding = resolve_persona_profile(persona)
        if binding.readiness == "missing_profile":
            raise ValueError(binding.summary)
        runtime_provider = provider_override or persona.provider or self._default_provider
        runtime_model = model_override or persona.model or self._default_model or ""
        health_persona = AgentPersona(
            **{
                field: getattr(persona, field)
                for field in getattr(persona, "__dataclass_fields__", {})
            }
        )
        health_persona.provider = runtime_provider
        health_persona.model = runtime_model
        assert_provider_health_for_persona(health_persona)
        # Non-blocking clarify bridge for this lane: a clarify call records the
        # question and ends the turn instead of blocking on a human queue the
        # spawn does not have. Read back after the run and threaded to the
        # caller as a structured clarify_request.
        clarify_capture = MissionChatClarifyCapture()
        result = self._runner.run(
            AgentRunRequest(
                profile=binding.hermes_profile,
                provider=runtime_provider,
                model=runtime_model,
                api_mode=persona.api_mode,
                reasoning_effort=reasoning_effort,
                enabled_toolsets=_enabled_toolsets_for_chat(persona, session_id=perm_session_id),
                blocked_tool_names=_blocked_tool_names_for_chat(persona, session_id=perm_session_id),
                quiet_mode=True,
                # Operator chat honors the persona's core-context-file opt-in like
                # the mission-run (L143) and free-chat (L208) paths. Isolated
                # personas (the default) must NOT auto-inject the process-cwd repo
                # project docs (e.g. the 72KB hermes-agent AGENTS.md, truncated to
                # ~65K chars = ~16K tokens) into every conversational turn — that
                # is ~20K tokens of fixed overhead per turn regardless of persona.
                # Repo doctrine an operator persona needs is carried by its skills
                # or read on demand; developer repo docs are not chat-turn context.
                skip_context_files=not bool(getattr(persona, "include_core_context_files", False)),
                # Profile memory (MEMORY.md / USER.md) is identity-adjacent: it
                # carries the bound profile's worldview into the turn. Honor the
                # persona's include_profile_memory opt-in instead of loading it
                # unconditionally, so a persona bound to a supervisor profile for
                # *capabilities* does not also inherit that profile's memory-model
                # (the Alice "goal->Neko->Dev" mental model that made Neko relay
                # to itself). A persona keeps its own profile's memory when the
                # binding is its own; it drops a borrowed profile's memory.
                skip_memory=not bool(getattr(persona, "include_profile_memory", False)),
                platform=PERSONA_CHAT_SCRATCH_SOURCE,
                session_id=session_id,
                max_wall_seconds=max_wall_seconds,
                max_api_calls=max_api_calls,
                max_total_tokens=max_total_tokens,
                # Byte-stable system prompt (T5): the volatile Runtime Situation
                # HUD rides the operator's user turn, not the codex
                # ``instructions``, so the cross-turn prompt cache prefix survives
                # every follow-up turn. See ``_mission_chat_user_message`` /
                # ``_mission_chat_surface_message``.
                user_message=_mission_chat_user_message(message, situational_hud_content),
                system_message=_mission_chat_surface_message(
                    persona,
                    surface_prompt,
                    preloaded_skill_prompt=preloaded_skill_prompt,
                    workspace_agents_content=workspace_agents_content,
                ),
                stream_callback=stream_callback,
                agent_ready_callback=agent_ready_callback,
                clarify_callback=clarify_capture.callback,
                # Key chat trace on the real chat session: Mission Control passes
                # session_id=None (the transcript is already baked into the
                # message) but the permission/session lineage lives on
                # perm_session_id, which is also the persona instance's session.
                progress_callback=_chat_trace_callback(
                    session_id=perm_session_id,
                    persona=persona,
                    turn_id=turn_id,
                    before_first_trace=pre_trace_callback,
                    on_trace=trace_callback,
                ),
                runtime_root=paths.store_root(),
            )
        )
        ChatToolPermissionStore().consume_turn(persona_id=persona.id, session_id=perm_session_id)
        if clarify_capture.requested and isinstance(result.raw, dict):
            result.raw["clarify_request"] = clarify_capture.request
        return result


# Source label for the agent's own scratch turns during an operator chat reply.
# The caller persists the redacted canonical transcript under
# ``agent_runtime_persona_chat``; this scratch lineage is registered as a hidden
# session source (see tools/session_search_tool.py) so the agent's raw, in-flight
# copy never becomes recall-reachable while real cross-session recall stays on.
PERSONA_CHAT_SCRATCH_SOURCE = "agent_runtime_persona_chat_scratch"


def _persona_chat_system_prompt(persona: AgentPersona) -> str:
    display = getattr(persona, "display_name", None) or getattr(persona, "id", "the agent")
    role = role_from_persona(persona).value
    base = (
        f"You are {display}, a Mission Control operator-channel agent (role: {role}). "
        "You are in a direct, real-time chat with a single human operator — your teammate, not an end user. "
        "You are embodied in the Mission Control office — a 2D/3D space shared with the other agents — and the "
        "operator's HUD shows live state: the current realm, workspace, and each agent's name and steer handle. "
        "A workspace board also exists; when you notice follow-up work worth tracking, you may add a card with the "
        "board tools (advisory — a card is planning state only and never starts or changes a goal). "
        f"{_persona_chat_voice(role, display)} "
        "Voice: warm, plain text, teammate-tight. Lead with the answer; skip preamble, filler, and restating the question. "
        "A sentence or two is usually enough — only go longer when the operator clearly wants depth. "
        "If you need tools, acknowledge the action first in one short sentence, then use the tools, then report the result. "
        "You have real tools. When the operator asks you to do something — run a command, read or edit a file, check or "
        "change state — actually use your tools and report the real result. The operator's current permission grant is the "
        "only gate on what you can do; there is no separate 'hand it off first' step. "
        "Never fabricate: do not claim to have run a command, read a file, opened a path, or produced output unless you "
        "actually invoked the tool and are reporting its real result. If a capability isn't available, or your permission "
        "grant blocks it, say so plainly instead of inventing output. "
        "Keep replies as clean teammate prose: never paste decision JSON, task scopes, acceptance criteria, handoff packets, "
        "or raw tool/tick scaffolding into the message — your tool calls are tracked separately in the trace lane. "
        "If an order is ambiguous or underspecified, use the `clarify` tool to ask before acting instead of guessing — you are "
        "in a live channel, and on this surface clarify ends your turn with your question and the answer comes back as the "
        "asker's next message (pass `choices` when the answer is one of a few known options). "
        "If the operator just greets you or makes small talk, talk back like a teammate. "
        "Recall: lean on the inline chat history for continuity. Reach for session_search only when the operator points at "
        "something specific from a past session you can't already see, and consult your durable memory only when it actually "
        "bears on the reply — don't fish."
    )
    # Same soul lane as the mission-chat surface: the persona's own configured
    # soul overlay rides along; absent for personas that don't set one.
    soul = _safe_read_soul_overlay(
        getattr(persona, "soul_overlay_path", None),
        hermes_profile=getattr(persona, "hermes_profile", None),
    )
    return f"{base}\n\n{soul}" if soul else base


def _persona_chat_voice(role: str, display: str) -> str:
    if role == "alice_supervisor":
        return (
            f"As {display} you run point for the operator across the mission: you coordinate the dev/QA personas, track what's "
            "in flight, and give crisp, decisive read-outs — and you act directly when the operator asks. Chief-of-staff "
            "energy, not cheerleader."
        )
    if role == "qa":
        return (
            "You are the quality gate: skeptical, precise, evidence-first. You talk through risks and what you'd verify — and "
            "when the operator asks, you actually run the checks and report what the evidence shows."
        )
    if role == "dev":
        return (
            "You are a senior engineer: concrete, pragmatic, fluent in the repo. You reason about approach and tradeoffs — and "
            "when the operator asks, you make the change or run the command directly, within your granted permissions."
        )
    return f"You speak as {display}: a capable, straight-talking teammate."


def _mission_chat_operative_rules() -> str:
    """Operative rules layered on top of the persona profile's own SOUL/identity
    for the canonical Mission Control operator chat.

    The profile owns voice and identity; these rules only govern *how the chat
    surface behaves*: real tool use is allowed (permission-gated), fabricated
    tool output is forbidden, and the reply stays clean prose while tool calls
    flow to the trace lane. Appended into the system prompt's context section
    (see ``agent/system_prompt.py``), so it overrides any 'propose-only / don't
    execute' posture for this surface without rewriting the profile."""

    return (
        "Mission Control operator-chat rules (these govern this live operator channel):\n"
        "- HARD RULE, FIRST IN EVERY TURN THAT USES TOOLS: before your first tool call, send one short sentence saying "
        "what you are about to do. The operator watches the console live — never open a turn with a silent tool call. "
        "Acknowledge, then act, then report the result.\n"
        "- You are talking directly to your operator — a trusted teammate, not an end user.\n"
        "- You have real tools. When the operator asks you to do something — run a command, read or edit a file, check or "
        "change state — actually use your tools and report the real result. The operator's current permission grant is the "
        "only gate on what you can do; there is no separate 'hand it off first' step.\n"
        "- Never fabricate. Do not claim to have run a command, read a file, opened a path, or produced output unless you "
        "actually invoked the tool and are reporting its real result. If a capability isn't available, or your permission "
        "grant blocks it, say so plainly instead of inventing output.\n"
        "- When the operator asks you to start, trigger, kick off, or run a goal/mission/task, create a REAL one with the "
        "mission_goal_create tool (it returns a tracked task_id and starts the Mission Daemon so it self-drives). Do NOT "
        "run the no-model smoke test (or any temp/throwaway graph validation) as a stand-in for a real goal — the smoke "
        "never appears in Mission Control. Only fall back to the smoke if the operator explicitly asks to validate the "
        "graph without creating real work.\n"
        "- If an order is ambiguous or underspecified — an unclear target, a missing detail, or a routing choice with more "
        "than one plausible answer — use the `clarify` tool to ask before acting, rather than guessing. Pass the question, and "
        "when the answer is one of a few known options pass them as `choices` (up to 4) so they render as pickable rows. On "
        "this channel `clarify` does NOT block: it ends your turn with your question, and the answer arrives as their next "
        "message in this same conversation. This is the operator channel, not an autonomous goal run: here, ask. Reach for it "
        "especially when you hold context the asker can't see (e.g. which of several same-role agents they mean).\n"
        "- When an agent you briefed replies with a clarifying question of their own, answer it by sending the choice back to "
        "them (agent_chat_send into that same session) so the exchange continues as one conversation — don't drop their "
        "question or answer it by guessing.\n"
        "- Teammates on your level are addressable by the `@personainst_*` handles in your Runtime Situation HUD. With "
        "`agent_chat_send`: omit the session to continue your durable pair thread (the norm — one thread per teammate); pass "
        "`session_id` to continue a specific thread; pass `new_session: true` only to start a clean thread (sparingly). Use "
        "`agent_chat_open` to review a teammate's recent thread before continuing it, and `agent_chat_threads` to list your threads.\n"
        "- Keep replies as clean teammate prose. Don't paste decision JSON, task scopes, acceptance criteria, handoff "
        "packets, or raw tool/tick scaffolding into the message — your tool calls are tracked separately in the trace lane."
    )


def _mission_chat_identity_prompt(persona: AgentPersona) -> str:
    """First-person identity block for the canonical Mission Control chat lane.

    The mission-chat lane deliberately runs isolated (``skip_context_files``),
    so the bound profile's SOUL.md is NOT loaded as the stable-tier identity —
    the model falls back to the generic ``DEFAULT_AGENT_IDENTITY`` and, without
    this block, never learns *which* persona it is. That was the root cause of
    the "Neko messages itself" incident: a persona bound to a supervisor
    profile (Alice) inherited that profile's "Neko is a separate agent I brief"
    memory model with no counter-vailing "you ARE Neko" hat, and relayed the
    operator's question to its own persona id via ``agent_chat_send``.

    This asserts the persona's own identity first, names the persona id so the
    model can recognize a self-directed relay, and states plainly that the
    persona is already the one speaking in this channel."""

    display = str(getattr(persona, "display_name", None) or getattr(persona, "id", "the agent")).strip()
    persona_id = str(getattr(persona, "id", "") or "").strip()
    role = role_from_persona(persona).value
    voice = _persona_chat_voice(role, display)
    id_clause = f" (Mission Control persona id: `{persona_id}`)" if persona_id else ""
    never_self = (
        f" Never use `agent_chat_send` to message `{persona_id}`: that persona is you — "
        "answer the operator directly instead of relaying to yourself."
        if persona_id
        else ""
    )
    return (
        f"You are {display}{id_clause}. {voice} You are already the persona speaking in this "
        "channel — the operator is talking to you right now, so respond directly in your own "
        f"voice.{never_self} Dev, Backend Dev, QA, and profile agents are the separate personas "
        "you may brief with `agent_chat_send`; you are not any of them and you are not your own "
        "relay target."
    )


def _mission_chat_surface_message(
    persona: AgentPersona,
    surface_prompt: str | None,
    *,
    preloaded_skill_prompt: str | None = None,
    workspace_agents_content: str | None = None,
) -> str:
    """Compose the operator-chat system message (the codex ``instructions``):
    the persona's first-person identity block first, then the non-negotiable
    operative rules, then the operator's optional per-session surface prompt.
    The identity block gives the isolated chat lane a "you ARE <persona>" hat
    (the profile SOUL is not loaded here); the rules always apply so the
    anti-fabrication invariant holds even when the operator supplies their own
    surface prompt.

    BYTE-STABILITY INVARIANT (T5, 2026-07-18): every part of this string must be
    byte-identical across every turn of a conversation, so the codex transport's
    ``prompt_cache_key = sha256(instructions + tools)`` stops rotating and the
    ~13K-token stable prefix (system prompt + tool schema) hits the cross-turn
    prompt cache. The identity/rules are static; the persona SOUL, workspace
    AGENTS.md, and operator surface prompt change only when their source
    actually changes (legitimate content-driven invalidation, like MEMORY.md).
    The Runtime Situation HUD — whose roster/scope/mission state rotated every
    turn — is deliberately NOT here anymore: it rides the operator's user turn
    instead (see ``_mission_chat_user_message``). Do NOT reintroduce per-turn
    volatile text into this builder; it re-bills the whole prefix every turn.
    (The queued-skill preload still layers here when the operator loads a skill;
    that is content-driven, not per-turn churn, and is a known secondary
    invalidation vector tracked for a follow-up move to the user turn.)"""

    identity = _mission_chat_identity_prompt(persona)
    operator_surface = (surface_prompt or "").strip()
    skill_prompt = (preloaded_skill_prompt or "").strip()
    workspace_agents = (workspace_agents_content or "").strip()
    rules = _mission_chat_operative_rules()
    # The persona's OWN soul overlay (config `soul_overlay_path`) is the one
    # identity document this isolated lane does load — who-you-are sits between
    # the identity hat and the surface rules. For a profile-backed persona it
    # resolves inside that persona's own profile home (single source), so this
    # IS the persona's SOUL.md — the OPERATOR profile's SOUL stays not-loaded.
    soul = _safe_read_soul_overlay(
        getattr(persona, "soul_overlay_path", None),
        hermes_profile=getattr(persona, "hermes_profile", None),
    )
    parts = [identity, soul or "", rules]
    if skill_prompt:
        parts.append(skill_prompt)
    if workspace_agents:
        parts.append(
            "Workspace instructions from the operator-selected AGENTS.md "
            "(apply these instructions to this turn):\n\n" + workspace_agents
        )
    if operator_surface:
        parts.append(operator_surface)
    return "\n\n".join(part for part in parts if part)


def _mission_chat_user_message(
    message: str,
    situational_hud_content: str | None = None,
) -> str:
    """Compose the operator turn's user message: the operator's message (which
    already carries the redaction-safe rolling chat history baked in by
    ``_persona_chat_message_with_history``) followed by the per-turn Runtime
    Situation HUD.

    Why the HUD rides here and not in the system prompt: the codex transport
    keys its cross-turn prompt cache on ``sha256(instructions + tools)``. A HUD
    whose roster / scope / mission state rotates every turn — e.g. ``QA Agent``
    vs ``QA Agent (2)`` — would evict the ~13K-token stable prefix on every
    follow-up turn's first call. Riding it in the operator's user turn keeps the
    system prompt byte-stable for the life of the conversation while still
    giving the model the same live picture the operator sees.

    Placement is load-bearing: the HUD TRAILS the history + current operator
    message rather than leading it. The user turn is already per-turn volatile
    (history grows, the message changes), so appending the HUD at its tail keeps
    the append-only, cache-friendly ordering the spec requires — a HUD ahead of
    the history would push the volatile block earlier in the (already uncached)
    input. This mirrors Hermes's own per-turn ephemeral-context injection, which
    appends recall / plugin context onto the current user turn rather than
    mutating the cached system prompt (agent/conversation_loop.py), and the
    skill-command pattern that injects as a user message to preserve caching.

    Transport note: on the codex Responses path a mid-conversation ``system``
    message is dropped by the input converter and two consecutive ``user`` items
    violate the role-alternation invariant, so the HUD cannot be a distinct
    non-user message without either vanishing or breaking alternation. Folding
    it onto the operator user turn is the transport-safe realization of "a
    per-turn message adjacent to the current operator message."
    """

    hud = (situational_hud_content or "").strip()
    body = message if isinstance(message, str) else ("" if message is None else str(message))
    if not hud:
        return body
    if not body:
        return hud
    return f"{body}\n\n{hud}"


def _repo_context_for_persona(persona: AgentPersona, ctx: AgentContext) -> RepoExecutionContext | None:
    if role_from_persona(persona) != "dev":
        return None
    # Ground in the CURRENT mission-plan stage's repo first. task.affected_repos lags
    # the active stage in a graph blueprint — it holds the *previous* stage's repo — so
    # grounding off it mis-routes every cross-stack dev (backend_dev lands in
    # hermes-agent, launcher dev lands in EterniaBackend, each then fails its proof in
    # the wrong tree). The stage repo is authoritative for the slice this persona owns.
    # Falls through to the legacy affected_repos/handoff resolution when there is no plan
    # stage (e.g. CLI goal_run) or the stage repo is unknown/unresolvable.
    stage_repo = _stage_repo_scope_for_persona(persona, ctx)
    if stage_repo is not None:
        try:
            return repo_execution_context_for_task(
                type("TaskStageRepoScope", (), {"affected_repos": [stage_repo]})()
            )
        except ValueError:
            pass
    repo_task = ctx.task
    if not (getattr(repo_task, "affected_repos", []) or []):
        handoff_repo = _handoff_repo_scope_for_persona(persona, ctx)
        if handoff_repo:
            repo_task = type("TaskHandoffRepoScope", (), {"affected_repos": [handoff_repo]})()
    if not (getattr(repo_task, "affected_repos", []) or []):
        raise DecisionPayloadInvalid("Dev run requires at least one affected_repo so the session can start in a repo workdir.")
    try:
        repo_scope = _compatible_repo_scope(persona, repo_task)
        return repo_execution_context_for_task(repo_task, explicit_workdir=repo_scope if repo_scope else None)
    except ValueError as exc:
        raise DecisionPayloadInvalid("Dev run could not resolve a valid affected repo workdir; fix affected_repos before dispatching Dev.") from exc


def _stage_repo_scope_for_persona(persona: AgentPersona, ctx: AgentContext) -> str | None:
    """Resolve the grounding repo from the current mission-plan stage.

    Authoritative over ``task.affected_repos`` (which lags the active stage). Only
    returns a repo when it is a known product/harness repo and — if the persona
    declares a ``repo_scope`` — that scope agrees with the stage repo. A mismatch is
    a slot/persona mis-binding we surface via the legacy path rather than silently
    grounding in the wrong tree.
    """
    stage = current_plan_stage(ctx.task) or getattr(ctx, "current_stage", None)
    stage_repo = str(getattr(stage, "repo", "") or "").strip() if stage is not None else ""
    if stage_repo not in {"EterniaBackend", "EterniaLauncher", "hermes-agent"}:
        return None
    from .final_gate import default_blueprint_placeholder_repo_override

    # On the bundled default blueprint the stage repo is a placeholder; a goal
    # resolved to a single different repo grounds there instead (observed live
    # 2026-07-03, task_8e1e0832: backend_dev grounded in an EterniaBackend
    # worktree for a hermes-agent goal, so the goal-named gate command failed
    # with file-not-found in the wrong tree).
    stage_repo = default_blueprint_placeholder_repo_override(ctx.task, stage_repo) or stage_repo
    persona_scope = getattr(persona, "repo_scope", None)
    if persona_scope:
        try:
            scoped = repo_execution_context_for_task(type("TaskPersonaScope", (), {"affected_repos": [persona_scope]})())
            stage_ctx = repo_execution_context_for_task(type("TaskStageRepo", (), {"affected_repos": [stage_repo]})())
        except ValueError:
            return None
        if scoped is None or stage_ctx is None or scoped.workdir != stage_ctx.workdir:
            return None
    return stage_repo


def _handoff_repo_scope_for_persona(persona: AgentPersona, ctx: AgentContext) -> str | None:
    packet = ctx.latest_handoff_packet if isinstance(ctx.latest_handoff_packet, dict) else {}
    body = packet.get("body") if isinstance(packet.get("body"), dict) else {}
    target_repo = str(body.get("target_repo") or "").strip()
    if target_repo not in {"EterniaBackend", "EterniaLauncher", "hermes-agent"}:
        return None
    persona_id = str(getattr(persona, "id", "") or "").strip()
    if persona_id == "backend_dev" and target_repo != "EterniaBackend":
        return None
    if persona_id == "dev" and target_repo == "EterniaBackend":
        return None
    return target_repo


def _compatible_repo_scope(persona: AgentPersona, task) -> str | None:
    repo_scope = getattr(persona, "repo_scope", None)
    if not repo_scope:
        return None
    try:
        scoped = repo_execution_context_for_task(type("TaskRepoScope", (), {"affected_repos": [repo_scope]})())
    except ValueError:
        return None
    if scoped is None:
        return None
    for repo in getattr(task, "affected_repos", []) or []:
        try:
            task_ctx = repo_execution_context_for_task(type("TaskRepo", (), {"affected_repos": [repo]})())
        except ValueError:
            continue
        if task_ctx is not None and task_ctx.workdir == scoped.workdir:
            return repo_scope
    return None


def _persona_run_uses_memory(persona: AgentPersona, ctx: AgentContext) -> bool:
    if bool(getattr(persona, "include_profile_memory", False)):
        return True
    flags = {str(flag or "").strip().lower() for flag in getattr(ctx.task, "risk_flags", []) or []}
    return "persona_operation_kind:free_floating" in flags


def _blocked_tool_names_for_run(persona: AgentPersona, ctx: AgentContext) -> list[str]:
    names = set(blocked_tool_names(persona))
    if _is_no_edit_context_stage(ctx) and role_from_persona(persona).value == "dev":
        names.update({"read_file", "search_files", "session_search", "browser_snapshot"})
    return sorted(names)


def _blocked_tool_names_for_chat(persona: AgentPersona, *, session_id: str | None) -> list[str]:
    options = permission_options_for_chat(persona, session_id=session_id)
    if permission_mode_is_unbounded(options.permission_mode):
        return []
    names = set(blocked_tool_names(persona))
    names.update(extra_blocked_tools_for_permission_mode(options.permission_mode))
    # clarify is globally blocked (PERSONA_BLOCKED_TOOLS) because autonomous
    # runs have no interactive callback to answer it — but the operator/relay
    # chat lane provides a non-blocking clarify bridge (MissionChatClarifyCapture),
    # so it is allowed here even in bounded permission mode.
    names.discard("clarify")
    return sorted(names)


def _chat_trace_callback(
    *,
    session_id: str | None,
    persona: AgentPersona,
    turn_id: str | None = None,
    before_first_trace: Callable[[dict], None] | None = None,
    on_trace: Callable[[dict], None] | None = None,
) -> Callable[[dict], None] | None:
    """Build a runner ``progress_callback`` that records a chat turn's tool
    calls as redaction-safe trace events keyed on the chat session.

    ``turn_id`` is the turn's canonical identity (the operator's
    ``client_message_id`` token); it is stamped on every recorded event so the
    snapshot projections carry one reconciliation key for the whole turn.

    Returns ``None`` when there is no session to key on (e.g. a sandbox run with
    no durable chat), which leaves the chat turn's telemetry exactly as it was
    before — no run row is created, nothing is persisted.
    """

    if not session_id and on_trace is None:
        return None
    sink = ChatProgressSink(
        session_id=session_id or "",
        persona_id=getattr(persona, "id", None),
        turn_id=turn_id,
        before_first_trace=before_first_trace,
        on_trace=on_trace,
    )
    return sink.callback()


def _enabled_toolsets_for_chat(persona: AgentPersona, *, session_id: str | None) -> list[str]:
    """The single chat-lane toolset chokepoint (both the free-chat and operator/
    mission chat call sites funnel through here).

    Resolution order: permission mode → role/persona toolset resolution → chat
    capability augmentation → the chat-lane cost policy
    (``scope_chat_lane_toolsets``) that drops browser / vision / heavy-dev from a
    conversational lane. ``unbounded`` permission mode is the operator's explicit
    "full capability" escape hatch and is returned unfiltered; a persona that
    wants a specific excluded toolset back on its *bounded* chat lane restores it
    via ``agent_runtime.personas.<id>.chat_lane_restore_toolsets`` (see
    ``config.chat_lane_restore_toolsets``). Worker/dev task lanes never call this
    — they resolve toolsets via ``effective_toolsets`` directly."""

    options = permission_options_for_chat(persona, session_id=session_id)
    if permission_mode_is_unbounded(options.permission_mode):
        return all_registered_toolsets()
    resolved = _augment_chat_capabilities(persona, list(effective_toolsets(persona)))
    return scope_chat_lane_toolsets(
        resolved, restore=chat_lane_restore_toolsets(persona.id)
    )


# Operator-chat-only first-class capabilities that a persona's role is allowed to
# use but which a persisted/config toolset list (created before the capability
# existed) may not yet enumerate. Without this, a deployment whose supervisor
# toolsets were persisted before ``mission_goal`` shipped could never trigger a
# real Mission Control goal from chat — the very thing the tool exists for.
_CHAT_CAPABILITY_TOOLSETS = ("mission_goal", "agent_chat", "board")


def _augment_chat_capabilities(persona: AgentPersona, toolsets: list[str]) -> list[str]:
    role = role_from_persona(persona)
    allowed = ALLOWED_TOOLSETS_BY_ROLE.get(role, frozenset())
    augmented = list(toolsets)
    for toolset in _CHAT_CAPABILITY_TOOLSETS:
        if toolset in allowed and toolset not in augmented:
            augmented.append(toolset)
    # clarify is a universal operator/relay conversational primitive — ask a
    # question, get the answer as the next message in the same session — so it
    # is available to every chat persona regardless of role, unlike the
    # privileged mission_goal capability which stays gated on `allowed`.
    if "clarify" not in augmented:
        augmented.append("clarify")
    return augmented


def _is_no_edit_context_stage(ctx: AgentContext) -> bool:
    stage = current_plan_stage(ctx.task) or ctx.current_stage
    if stage is None:
        return False
    return str(getattr(stage, "kind", "") or "") == "context" and not stage_requires_product_edit(ctx.task, stage)


def _repo_context_for_render(repo_ctx: RepoExecutionContext) -> dict:
    return {
        "repo_label": repo_ctx.repo_label,
        "source": repo_ctx.source,
        "context_loaded": repo_ctx.context_loaded_label,
        "context_excerpts": [
            {"label": item.label, "content": item.content, "truncated": item.truncated}
            for item in repo_ctx.context_excerpts
        ],
    }


def _repo_context_progress_payload(repo_ctx: RepoExecutionContext) -> dict:
    return {
        "type": "run.progress",
        "phase": "inspect",
        "severity": "info",
        "step": "repo_context_loaded",
        "status": "ready",
        "summary": f"Dev session grounded in repo {repo_ctx.repo_label}; context_loaded: {repo_ctx.context_loaded_label}",
        "repo_label": repo_ctx.repo_label,
        "context_loaded": repo_ctx.context_loaded_label,
        "next_expected": "repo_scoped_audit",
    }


def _attach_repo_baseline(run: AgentRun, repo_ctx: RepoExecutionContext) -> None:
    try:
        baseline = capture_repo_baseline(repo_ctx.workdir)
        run.progress = {
            **(run.progress or {}),
            "repo_baseline": baseline,
            "repo_execution": {
                "schema_version": 1,
                "workdir": str(repo_ctx.workdir),
                "workdir_label": repo_ctx.workdir.name,
                "repo_label": repo_ctx.repo_label,
                "source": repo_ctx.source,
                "isolated": repo_ctx.source.endswith("-worktree"),
                "detached_head": baseline.get("git_branch") == "HEAD",
                "git_head": baseline.get("git_head"),
            },
        }
        RunStore().update(run)
    except Exception:
        return



def _apply_llm_metadata(run: AgentRun, result: AgentRunResult, *, timing: dict[str, int] | None = None) -> None:
    run.session_id = _safe_session_id(result.session_id) or _safe_session_id(run.session_id)
    messages = result.messages
    tool_turns = sum(1 for msg in messages if isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("tool_calls"))
    final_response = result.final_response
    llm = {
        "provider": result.provider,
        "model": result.model,
        "base_url_host": _safe_base_url_host(result.base_url),
        "session_id": run.session_id,
        "api_calls": result.api_calls,
        "tool_turns": tool_turns,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "total_tokens": result.total_tokens,
        "latency_ms": result.latency_ms,
        "finish_reason": _finish_reason_from_result(result.raw),
        "response_len": len(final_response) if isinstance(final_response, str) else None,
        "decision_metrics": _decision_metrics(run, result, tool_turns=tool_turns),
    }
    timing_map = _safe_timing_map(getattr(run, "llm", None))
    for key, value in (timing or {}).items():
        _record_timing_value(timing_map, key, value)
    profile_timing = getattr(result, "profile_timing", None)
    if isinstance(profile_timing, dict):
        for key, value in profile_timing.items():
            timing_key = key if str(key).startswith("profile_") else f"profile_{key}"
            _record_timing_value(timing_map, timing_key, value)
    if result.latency_ms is not None:
        _record_timing_value(timing_map, "profile_runner_ms", result.latency_ms)
        if "provider_call_ms" not in timing_map:
            _record_timing_value(timing_map, "provider_call_ms", result.latency_ms)
    if timing_map:
        llm["timing"] = timing_map
    run.llm = {key: value for key, value in llm.items() if value is not None}


def _emit_timing(progress_sink: RunProgressSink, timing_key: str, started: float, *, status: str) -> int:
    duration_ms = max(0, int((time.perf_counter() - started) * 1000))
    progress_sink.emit(
        "run.progress",
        {
            "type": "run.progress",
            "phase": "timing",
            "step": timing_key,
            "status": status,
            "summary": f"{timing_key.replace('_', ' ').title()} {status} in {duration_ms}ms.",
            "duration_ms": duration_ms,
            "timing_key": f"{timing_key}_ms",
        },
    )
    return duration_ms


def _record_timing_value(timing_map: dict[str, int], key: object, value: object) -> None:
    if not isinstance(key, str) or not key.endswith(("_ms", "_count")):
        return
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return
    if parsed < 0:
        return
    safe_key = key[:64]
    if safe_key.endswith("_count"):
        previous = timing_map.get(safe_key)
        timing_map[safe_key] = (previous if isinstance(previous, int) and previous >= 0 else 0) + parsed
        return
    previous = timing_map.get(safe_key)
    if isinstance(previous, int):
        base = safe_key[:-3]
        count_key = f"{base}_count"[:64]
        total_key = f"{base}_total_ms"[:64]
        max_key = f"{base}_max_ms"[:64]
        previous_count = timing_map.get(count_key)
        previous_total = timing_map.get(total_key)
        previous_max = timing_map.get(max_key)
        if not isinstance(previous_count, int) or previous_count < 1:
            previous_count = 1
        if not isinstance(previous_total, int) or previous_total < previous:
            previous_total = previous
        if not isinstance(previous_max, int):
            previous_max = previous
        timing_map[count_key] = previous_count + 1
        timing_map[total_key] = previous_total + parsed
        timing_map[max_key] = max(previous_max, parsed)
    timing_map[safe_key] = parsed


def _safe_timing_map(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    raw = value.get("timing")
    if not isinstance(raw, dict):
        return {}
    result: dict[str, int] = {}
    for key, item in raw.items():
        if not isinstance(key, str) or not (key.endswith("_ms") or key.endswith("_count")):
            continue
        try:
            parsed = int(item)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            result[key[:64]] = parsed
    return result


def _decision_metrics(run: AgentRun, result: AgentRunResult, *, tool_turns: int) -> dict[str, object]:
    progress = run.progress if isinstance(getattr(run, "progress", None), dict) else {}
    read_search_count = _safe_nonnegative_int(progress.get("read_search_count"))
    proof_count = _safe_nonnegative_int(progress.get("proof_count"))
    patch_count = _safe_nonnegative_int(progress.get("patch_count"))
    test_count = _safe_nonnegative_int(progress.get("test_count"))
    packet_repair_count = _safe_nonnegative_int(progress.get("packet_repair_count") or progress.get("repair_count"))
    classification = "necessary_progress"
    if str(progress.get("loop_warning") or "") == "read_search_without_patch_threshold":
        classification = "possible_loop"
    elif str(progress.get("status") or "") in {"blocked", "failed"} or progress.get("blocker_kind"):
        classification = "blocked_environment"
    elif packet_repair_count:
        classification = "contract_repair"
    return {
        "classification": classification,
        "api_calls": _safe_nonnegative_int(result.api_calls),
        "tool_turns": max(0, int(tool_turns or 0)),
        "read_search_count": read_search_count,
        "proof_count": proof_count,
        "patch_count": patch_count,
        "test_count": test_count,
        "packet_repair_count": packet_repair_count,
        "new_evidence_count": sum(1 for item in (proof_count, patch_count, test_count, packet_repair_count) if item > 0),
        "reason": _decision_metric_reason(classification),
    }


def _decision_metric_reason(classification: str) -> str:
    return {
        "possible_loop": "read/search budget warning without matching patch progress",
        "blocked_environment": "run progress reported blocked or failed status",
        "contract_repair": "packet or contract repair progress was recorded",
        "necessary_progress": "run produced bounded decision metadata without loop/blocker signal",
    }.get(classification, "run classified from redaction-safe counters")


def _safe_nonnegative_int(value) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed >= 0 else 0

def _safe_base_url_host(base_url: str | None) -> str | None:
    if not base_url:
        return None
    parsed = urlparse(str(base_url))
    return parsed.hostname


def _finish_reason_from_result(result: dict | object) -> str | None:
    if not isinstance(result, dict):
        return None
    raw = result.get("turn_exit_reason")
    if isinstance(raw, str) and "finish_reason=" in raw:
        return raw.split("finish_reason=", 1)[1].split(")", 1)[0]
    messages = result.get("messages") if isinstance(result.get("messages"), list) else []
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("finish_reason"):
            return str(msg.get("finish_reason"))
    return None


def build_system_prompt(persona: AgentPersona, *, task_id: str | None = None) -> str:
    role = role_from_persona(persona)
    compact_schema = json.dumps(DECISION_SCHEMA, separators=(",", ":"))
    try:
        cfg = load_agent_runtime_config()
    except Exception:
        cfg = None
    simplified_prompt = _simplified_contract_prompt_enabled(cfg)
    payload_contracts = prompt_contract_markdown(_simplified_contract_decisions_for_role(role) if simplified_prompt else None)
    parts = [load_bundled_prompt(role)]
    overlay = Path(__file__).with_name("prompts") / "shared_harness_overlay.md"
    if overlay.exists():
        parts.append(overlay.read_text(encoding="utf-8").strip())
    soul_overlay = _safe_read_soul_overlay(
        persona.soul_overlay_path,
        hermes_profile=getattr(persona, "hermes_profile", None),
    )
    if soul_overlay:
        parts.append(soul_overlay)
    skill_guidance = _recommended_skill_guidance(list(persona.skills))
    if skill_guidance:
        parts.append(skill_guidance)
    specialist_guidance = _specialist_dev_guidance(persona)
    if specialist_guidance:
        parts.append(specialist_guidance)
    normal_flow_guidance = _normal_worker_flow_guidance(persona)
    if normal_flow_guidance:
        parts.append(normal_flow_guidance)
    simplified_contract_guidance = _simplified_contract_guidance(persona, cfg=cfg)
    if simplified_contract_guidance:
        parts.append(simplified_contract_guidance)
    parts.extend(
        [
            "# Universal Harness Rules\n"
            "You are a task-bound Agent Runtime Harness persona. Return exactly one AgentDecision JSON object. "
            "Do not ask the human questions from inside a tick. Do not claim proof you did not receive from the harness. "
            "Do not use delegated agents, cron jobs, memory writes, messaging, or Kanban side effects. "
            "Do not use Kanban vocabulary or mutate Kanban state. The Autonomy / Tool Economy Contract in the tick context is Harness-generated public operating context; obey its budgets and do not add new AgentDecision keys unless the payload contract allows them. "
            "When Mission HUD is present, read mission_hud.agent_hud as a two-sided dashboard: STATUS shows Harness-observed diff/proof/gate state, ACTION shows bounded steering or handoff choices. Prefer the recommended visible action, use only allowed payload keys, and treat unknown payload keys as invalid. Open only the named recommended_action.skill_ref when the HUD says deeper guidance is needed. "
            "Generic Hermes core guidance about tool persistence, task completion, profile identity, or manual-session workflow is subordinate to this Harness contract: returning a valid AgentDecision is the action for this tick. "
            "If the stage is no-edit, proof-backed, or explicitly requests Harness-owned proof, do not call extra tools just to satisfy generic tool-use guidance; emit the precise AgentDecision instead.",
            "# Stage Ownership and Handoff\n"
            "Act like an accountable teammate, not a stateless robot. Know your role, current task state, current stage, available proof_ids, and the next owner. "
            "A stage is complete only when your payload says what you finished, what proof_ids support it, known gaps, and who receives the handoff. "
            "PM/Neko hands scoped work to the graph-selected Dev specialist. Dev completes the current stage with real proof_ids and hands off to the next graph owner; request QA review only when the active blueprint includes a QA/verifier stage. "
            "When QA is present, QA approves implementation only after reviewing proof_ids and attaching/verifying an implementation verdict; otherwise request tests/fixes or block with exact gaps. "
            "If you discover a repeated workflow failure, report it as a Harness/skill intervention rather than looping.",
            f"# AgentDecision JSON Schema\n```json\n{compact_schema}\n```",
            f"# AgentDecision Payload Contracts\n{payload_contracts}\n"
            "Use `recipe_id` when the Autonomy packet lists a matching `available_proof_recipes` entry; then omit commands and let the Harness supply the exact recipe commands, sandbox, dirty-check, marker checks, and proof metadata. "
            "After Harness attaches command proof IDs, hand off to the next graph owner with those existing proof_ids; use QA only when the active blueprint includes a QA/verifier node. "
            "Before blocking, inspect/grep your own run or event logs, keep the reason brief, and point at the redaction-safe log line number that proves the blocker. "
            "If a previous decision parse failed, fix the exact missing/invalid key named in the repair context.",
        ]
    )
    return "\n\n".join(part for part in parts if part)


def _simplified_contract_prompt_enabled(cfg) -> bool:
    simplified = getattr(cfg, "simplified_agent_contract", None)
    return bool(
        getattr(simplified, "enabled", False)
        and getattr(simplified, "expose_only_simplified_actions", True)
    )


def _simplified_contract_decisions_for_role(role) -> list[DecisionType]:
    role_value = role.value if hasattr(role, "value") else str(role)
    if role_value == "dev":
        return [DecisionType.HAND_OFF, DecisionType.ESCALATE, DecisionType.BLOCK]
    if role_value == "qa":
        return [DecisionType.QA_VERDICT, DecisionType.ESCALATE, DecisionType.BLOCK]
    if role_value == "alice_supervisor":
        return [DecisionType.SCOPE_ROUTE, DecisionType.ESCALATE, DecisionType.BLOCK, DecisionType.RESOLVE_INCIDENT]
    return [DecisionType.BLOCK]


def _simplified_contract_guidance(persona: AgentPersona, *, cfg) -> str:
    if not _simplified_contract_prompt_enabled(cfg):
        return ""
    role = role_from_persona(persona).value
    common = (
        "# Simplified Agent Contract Active\n"
        "Mission HUD mode is `simplified_agent_contract`. The visible ACTION menu is authoritative. "
        "Ignore older bundled-prompt mentions of legacy decision names unless terminal feedback explicitly asks for a one-turn repair. "
        "Do not emit legacy micro-decisions while this mode is active. "
        "The Harness keeps legacy aliases only for archive/backcompat normalization and logs parity events when it maps one forward."
    )
    if role == "dev":
        return (
            common
            + "\n\nFor Dev and Backend Dev, allowed public decisions are `hand_off`, `block`, and `escalate`. "
            "For product-edit stages, no-edit proof stages, exact proof recipes, failed-gate repairs, and contract joins, emit `hand_off` when your slice is ready for Harness attribution/proof. "
            "`hand_off` is the only Dev completion signal: the Harness captures the isolated-worktree diff and runs the authoritative gate/recipe. "
            "Use `block` only for exact missing prerequisites, and `escalate` only for a discovered issue too large or unsafe to fold into this stage."
        )
    if role == "qa":
        return (
            common
            + "\n\nFor QA, allowed public decisions are `qa_verdict`, `block`, and `escalate`. "
            "`qa_verdict` is the QA completion signal; cite existing Harness proof IDs and findings. "
            "Use `block` for missing proof and `escalate` for out-of-scope or systemic issues."
        )
    if role == "alice_supervisor":
        return (
            common
            + "\n\nFor Neko Mission Lead, the normal public routing decision is `scope_route`. "
            "Use `scope_route` for kickoff, rescope, graph-faithful owner/repo routing, and proof-gate release. "
            "Open incidents are yours to adjudicate: when an incident's underlying run is already terminal "
            "(cancelled/failed/hung-reaped), close it with `resolve_incident` and a redaction-safe reason — "
            "never answer `block` for an incident you can close. Use `block` only for true external blockers "
            "that genuinely need a human, and `escalate` for issue discovery."
        )
    return common


def _specialist_dev_guidance(persona: AgentPersona) -> str:
    if role_from_persona(persona) != "dev":
        return ""
    shared = (
        "# Specialist Dev Loop Guard\n"
        "Operate with Alice/Neko-style budget discipline: use one bounded repo-scoped search/read pass, then choose target files, patch, run a focused self-test, hand off, or block with exact evidence. "
        "If repeated read/search/tool-loop warnings appear before patch/test/proof progress, stop immediately and return a smaller stage plan, `block`, or exact missing-input report so Neko can slice or steer. "
        "Do not spend live ticks rediscovering the repo. Token management is part of correctness: narrow context beats broad audits."
    )
    persona_id = str(getattr(persona, "id", "") or "").lower()
    repo_label = str(getattr(persona, "repo_scope_label", "") or "").lower()
    if persona_id == "backend_dev" or "backend" in repo_label:
        overlay = (
            "# Backend Dev Specialist Overlay\n"
            "You are Backend Dev for EterniaBackend. Think in API contracts, migrations, auth/security boundaries, and Postgres-backed verification. "
            "Use the Postgres Docker full gate before deploy-ready claims when available; if Docker/Postgres is unavailable, block with exact environment evidence instead of substituting SQLite. "
            "When frontend behavior depends on backend shape, produce a frontend-backend contract handoff with fields, status codes, migrations, compatibility/defaults, and focused backend test commands."
        )
    else:
        overlay = (
            "# Launcher Dev Specialist Overlay\n"
            "You are Launcher Dev for EterniaLauncher. Think in Flutter widgets/state, Launcher_Brain conventions, Stage C/MCP semantic QA, and visual proof expectations. "
            "Respect the dirty tree: identify pre-existing unrelated changes before patching and avoid touching posts/voice/bootstrap files unless they are directly in scope. "
            "For UI or message/attachment work, prefer targeted Flutter tests/analyze plus MCP screenshot or local artifact proof when visual behavior is claimed."
        )
    return f"{shared}\n\n{overlay}"


def _normal_worker_flow_guidance(persona: AgentPersona) -> str:
    try:
        cfg = load_agent_runtime_config()
    except Exception:
        return ""
    flow = getattr(cfg, "normal_worker_flow", None)
    if not bool(getattr(flow, "enabled", False)):
        return ""
    role = role_from_persona(persona).value
    if role == "dev":
        return (
            "# Normal Worker Flow\n"
            "For product-edit stages, do the work like one uninterrupted competent developer: inspect narrowly, edit files, run focused self-tests in-session with terminal/code tools, then emit `hand_off`. "
            "Do not declare changed files, proof IDs, delivery packets, or `delivery.work_status`; Harness derives diff, delivery, and final-gate state. "
            "Do not use `request_test_run` as your normal inner loop. Use Harness proof only when the Mission HUD exposes `request_gate`, the stage is no-edit/certification, QA requests a missing gate, or you are repairing a failed final gate. "
            "After hand_off, the Harness owns the final deterministic gate and will return failed proof IDs to this same worker if repair is needed."
        )
    if role == "qa":
        return (
            "# Normal Worker Flow QA\n"
            "Self-test evidence helps triage but is not release proof. Base implementation approval on Harness final gate proof IDs and required visual/MCP artifacts. "
            "Request exactly one missing command or visual gate when proof is absent or stale; otherwise emit `qa_verdict` with cited proof IDs."
        )
    if role == "alice_supervisor":
        return (
            "# Normal Worker Flow Neko\n"
            "Prefer same-worker repair over spawning new work. Wait/request-human only at kickoff or for true human/safety blockers. "
            "Route with `scope_route` by attached evidence, failed proof IDs, and worker HUD state; release QA only when the active graph includes QA and final gate proof is attached. "
            "For heavy investigation, spawn/steer instead of absorbing transcripts: sample only bounded progress_peek/topology status, pass pointers and repo handles, and leave child bytes in their artifacts."
        )
    return ""


def _tool_budget_limits(ctx: AgentContext) -> dict[str, object] | None:
    packet = ctx.autonomy_packet if isinstance(ctx.autonomy_packet, dict) else {}
    budget = packet.get("inspection_budget") if isinstance(packet.get("inspection_budget"), dict) else None
    safe: dict[str, object] = {}
    if budget:
        for key in ("read_search_limit", "proof_retry_limit", "proof_command_limit", "skill_load_limit"):
            try:
                value = int(budget.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                safe[key] = min(value, 20)
    safe.update(_prior_stage_progress_flags(ctx))
    return safe or None


def _prior_stage_progress_flags(ctx: AgentContext) -> dict[str, bool]:
    flags: dict[str, bool] = {}
    run_progress = ctx.run.progress if isinstance(getattr(ctx.run, "progress", None), dict) else {}
    if run_progress.get("has_patch_progress") is True or _safe_positive_counter(run_progress.get("patch_count")) > 0:
        flags["has_patch_progress"] = True
    if run_progress.get("has_test_progress") is True or _safe_positive_counter(run_progress.get("test_count")) > 0:
        flags["has_test_progress"] = True
    if run_progress.get("has_proof_progress") is True or _safe_positive_counter(run_progress.get("proof_count")) > 0:
        flags["has_proof_progress"] = True
    for event in ctx.recent_events or []:
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict):
            payload = event if isinstance(event, dict) else {}
        if payload.get("has_patch_progress") is True or _safe_positive_counter(payload.get("patch_count")) > 0 or str(payload.get("phase") or "") == "dev_work":
            flags["has_patch_progress"] = True
        if payload.get("has_test_progress") is True or _safe_positive_counter(payload.get("test_count")) > 0:
            flags["has_test_progress"] = True
        if payload.get("has_proof_progress") is True or _safe_positive_counter(payload.get("proof_count")) > 0 or payload.get("proof_id"):
            flags["has_proof_progress"] = True
    return flags


def _safe_positive_counter(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _recommended_skill_guidance(skill_names: list[str]) -> str:
    """Render persona skill hints without preloading full skill bodies.

    Harness personas run through the normal Hermes ``AIAgent`` class, so when
    the ``skills`` toolset is enabled they already receive Hermes' compact skill
    index plus ``skills_list``/``skill_view``/``skill_manage`` tools.  Keep the
    persona manifest as a relevance hint instead of stuffing every configured
    SKILL.md into the system prompt.  This mirrors Alice-style operation: scan
    the available skills, then load only the skills that match the current task.
    """
    cleaned = []
    for name in skill_names:
        clean = str(name).strip()
        if clean and clean not in cleaned:
            cleaned.append(clean)
    if not cleaned:
        return ""
    lines = [
        "# Recommended Harness Persona Skills",
        (
            "These are persona-recommended skills, not preloaded instructions. "
            "When the skills toolset is available, skill use is the default for "
            "non-trivial Harness ticks: start with skill_search(query=...) using "
            "the current task, stage, repo, and proof gate, then load only the "
            "single most relevant installed skill. Never preload or bulk-load "
            "the whole manifest."
        ),
        (
            "Prefer specific, stage-relevant loading over loading the whole "
            "manifest. Stop after loading the single most relevant skill unless "
            "another skill is clearly required for the active proof gate. Two "
            "loaded skills is the normal maximum; more than two is allowed only "
            "when each additional skill has an explicit, current-stage purpose "
            "and you name that purpose in the final AgentDecision. After every "
            "skill load, reassess whether to pivot to proof, QA handoff, or an "
            "exact blocker. Fall back to skills_list/skill_view only when search "
            "is unavailable; do not shell out to `hermes skills search` from a "
            "Harness persona unless the native tool is missing."
        ),
        "Recommended skills:",
    ]
    lines.extend(f"- {name}" for name in cleaned)
    return "\n".join(lines)


def _safe_read_soul_overlay(
    path_value: str | None, *, hermes_profile: str | None = None
) -> str | None:
    if not path_value:
        return None
    raw = Path(path_value)
    if raw.is_absolute() or not _is_safe_soul_overlay_path(raw):
        return None
    if hermes_profile:
        # A profile-backed persona owns its soul in ITS OWN profile home —
        # `profiles/<hermes_profile>/SOUL.md` is the single source (realm sync
        # already models soul_overlay as profile-home-relative). Repo prompts
        # stay as the shipped-default fallback. Deliberately NO operator-home
        # fallthrough here: on a miss, a bare `SOUL.md` must never resolve to
        # the OPERATOR profile's SOUL (the persona-identity-leak class).
        home = _persona_profile_home(hermes_profile)
        candidates = [
            *( [home / raw] if home is not None else [] ),
            Path(__file__).with_name("prompts") / raw.name,
        ]
    else:
        candidates = [
            Path(__file__).with_name("prompts") / raw.name,
            get_hermes_home() / raw,
        ]
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_file():
                return candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return None


def _persona_profile_home(name: str) -> Path | None:
    """Home directory of the named hermes profile, or None when unresolvable.

    Prefers the canonical CLI resolver; falls back to the standard
    ``<profiles root>/<name>`` layout beside the operator home. Kept as its own
    seam so tests can pin the home without touching global profile state."""

    try:
        from hermes_cli.profiles import get_profile_dir, normalize_profile_name, profile_exists

        normalized = normalize_profile_name(name)
        if profile_exists(normalized):
            return Path(get_profile_dir(normalized))
    except Exception:
        pass
    try:
        candidate = get_hermes_home().parent / name
        if candidate.exists():
            return candidate
    except OSError:
        pass
    return None


def _is_safe_soul_overlay_path(path: Path) -> bool:
    if path.suffix.lower() != ".md":
        return False
    unsafe_parts = {".env", "env", "auth", "credentials", "credential", "secrets", "secret", "tokens", "token", "config"}
    return not any(part.lower() in unsafe_parts or part.startswith(".") for part in path.parts)
