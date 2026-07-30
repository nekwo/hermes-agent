from __future__ import annotations

from pathlib import Path
import time
from urllib.parse import urlparse
from typing import Callable, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from .tool_visibility import ToolVisibilityOptions

from hermes_constants import get_hermes_home

from . import paths
from .chat_lane_toolsets import (
    ChatLaneDrop,
    chat_lane_blocked_tools,
    chat_lane_tool_drops,
    chat_lane_toolset_drops,
    scope_chat_lane_toolsets,
)
from .config import chat_lane_restore_toolsets
from .context_builder import AgentContext, build_context, render_context
from .decision_schema import (
    AgentDecision,
    DecisionPayloadInvalid,
    parse_structured_decision,
    validate_decision_for_role,
)
from .decision_contracts import validate_planning_decision
from .mcp_admission import (
    LANE_MISSION_CHAT,
    admission_enabled,
    admitted_operating_skill_ids,
    render_mcp_admission_line,
    resolve_mcp_admission,
    scope_toolsets_to_admission,
)
from .mcp_lane import mission_chat_mcp_lane_line
from .models import AgentPersona, AgentRun
from .mission_chat_clarify import MissionChatClarifyCapture
from .mission_chat_workdir import mission_chat_workdir_for_persona
from .personas import all_registered_toolsets, blocked_tool_names, effective_toolsets, role_from_persona
from .profile_context import resolve_persona_profile
from .provider_health import assert_provider_health_for_persona
from .terminal_envelope import (
    LANE_MISSION_WORKER as TERMINAL_ENVELOPE_LANE_MISSION_WORKER,
    LANE_PERSONA_CHAT as TERMINAL_ENVELOPE_LANE_PERSONA_CHAT,
    scope_for_persona as terminal_envelope_scope_for_persona,
)
from .profile_runner import (
    AgentRunRequest,
    AgentRunResult,
    ProfileAgentRunner,
    RunBudgetExceeded,
    _blocked_tool_names_with_registry_hygiene,
)
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
        # T10c follow-up: header-only cache-scope routing identity (codex
        # cache-scope headers), NEVER a transcript/session-load key. The
        # free-floating lane binds its chat session but calls with
        # session_id=None (history is baked into the message) — without this,
        # that lane ships no cache-scope headers and every turn is cache-cold.
        cache_scope_id: str | None = None,
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
        # Audit Q2: free-chat is harness-constructed, so it binds a scope and
        # stops being decided by whether HERMES_AGENT_RUNTIME_ROOT happens to be
        # exported. Its lane is NOT governed by the grant table — free-chat is a
        # conversational surface, not the primary work lane — so gated classes
        # keep the legacy hard block, now reached by construction.
        #
        # This is NOT ``hermes chat``: that is the operator's own shell, never
        # reaches AgentRunRequest, and must never carry an envelope.
        envelope_scope = terminal_envelope_scope_for_persona(
            persona,
            lane=TERMINAL_ENVELOPE_LANE_PERSONA_CHAT,
            session_id=session_id,
            runtime_root=paths.store_root(),
        )
        result = self._runner.run(
            AgentRunRequest(
                profile=binding.hermes_profile,
                provider=persona.provider or self._default_provider,
                model=persona.model or self._default_model or "",
                api_mode=persona.api_mode,
                terminal_envelope_scope=envelope_scope,
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
                skill_surface="mission_chat",
                skill_root_node_mode=False,
                session_id=session_id,
                cache_scope_id=cache_scope_id,
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
        # Absolute path of the operator-selected workspace ``AGENTS.md`` (the
        # ``--agents-file`` receipt). Content and PATH are threaded separately on
        # purpose: the content is prompt material, the path is the workspace
        # POINTER rung of the workdir ladder (G6) — the directory the operator
        # aimed this turn at. Never read for content here.
        workspace_agents_path: str | None = None,
        situational_hud_content: str | None = None,
        conversation_history: list[dict] | None = None,
        reuse_current_user_message: bool = False,
        root_chat_session_id: str | None = None,
        client_message_id: str | None = None,
        runtime_registry=None,
        runtime_signature: str | None = None,
        native_revision: str | None = None,
        compression_threshold_tokens_override: int | None = None,
        compression_protect_first_n_override: int | None = None,
        compression_protect_last_n_override: int | None = None,
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
        # Resolve MCP admission ONCE per turn (pure policy — no spawn, no
        # registration) and thread the same answer through the toolset scope and
        # the runner, so the tools the turn asks for and the servers the runner
        # registers can never disagree. Disabled by default: with the flag off
        # this resolves to "nothing admitted" and the request is byte-identical
        # to what it was before admission existed.
        admission = resolve_mcp_admission(
            persona,
            lane=LANE_MISSION_CHAT,
            permission_mode=permission_options_for_chat(
                persona, session_id=perm_session_id
            ).permission_mode,
        )
        # Repo grounding for this turn (G6). Resolved ONCE, here, and handed to
        # the EXISTING ``AgentRunRequest.workdir`` seam the worker lane already
        # uses — ``profile_runner`` chdirs and exports ``TERMINAL_CWD`` under its
        # workdir lock, which is what puts a real repo in front of the terminal /
        # file tools. ``None`` (nothing configured, nothing derivable) keeps the
        # pre-G6 behavior exactly: the turn runs in the process cwd. A configured
        # path that does not exist degrades to that same safe cwd and is reported
        # as a typed row on the preview lane — it never fails the turn.
        workdir = mission_chat_workdir_for_persona(
            persona, workspace_agents_path=workspace_agents_path
        )
        # Lane/role identity for the terminal safety envelope. Bound for the
        # WHOLE run so envelope enforcement on this lane is deterministic and
        # operator-governed instead of keyed on whether the persona happens to
        # bind a Hermes profile (the historical fail-open/fail-closed split —
        # see agent_runtime/terminal_envelope.py). Carrying the runtime root on
        # the scope also means the decision receipt lands even for a persona
        # that never exports HERMES_AGENT_RUNTIME_ROOT.
        #
        # Note how G6 and this slice compose: the workdir above puts a REAL repo
        # in front of the terminal tool, which is exactly what makes the envelope
        # gate load-bearing rather than theoretical — a grounded turn can now
        # actually reach a git remote.
        envelope_scope = terminal_envelope_scope_for_persona(
            persona,
            lane=LANE_MISSION_CHAT,
            session_id=perm_session_id,
            runtime_root=paths.store_root(),
        )
        result = self._runner.run(
            AgentRunRequest(
                profile=binding.hermes_profile,
                provider=runtime_provider,
                model=runtime_model,
                api_mode=persona.api_mode,
                reasoning_effort=reasoning_effort,
                terminal_envelope_scope=envelope_scope,
                mcp_admission=admission,
                enabled_toolsets=_enabled_toolsets_for_chat(
                    persona,
                    session_id=perm_session_id,
                    admission=admission,
                ),
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
                skill_surface="mission_chat",
                skill_root_node_mode=False,
                session_id=session_id,
                # session_id stays None on this lane (the transcript is already
                # baked into the message), but the ChatGPT-Codex prompt cache is
                # scoped by the session_id / x-client-request-id HTTP headers.
                # Feed the STABLE chat session identity (perm_session_id — the id
                # that names the turn store / observability session) as the
                # header-only cache_scope_id so the warm prefix survives across
                # turns. Header/routing value ONLY — never a transcript-load key
                # (T10c). Worker/mission-run lanes leave this unset.
                cache_scope_id=perm_session_id,
                tool_execution_scope_id=root_chat_session_id or perm_session_id,
                conversation_history=conversation_history,
                reuse_current_user_message=reuse_current_user_message,
                root_chat_session_id=root_chat_session_id or perm_session_id,
                client_message_id=client_message_id,
                turn_id=turn_id,
                persona_chat_runtime_registry=runtime_registry,
                persona_chat_runtime_signature=runtime_signature,
                persona_chat_native_revision=native_revision,
                compression_threshold_tokens_override=compression_threshold_tokens_override,
                compression_protect_first_n_override=compression_protect_first_n_override,
                compression_protect_last_n_override=compression_protect_last_n_override,
                max_wall_seconds=max_wall_seconds,
                max_api_calls=max_api_calls,
                max_total_tokens=max_total_tokens,
                # Byte-stable system prompt (T5 + T9a): the volatile Runtime
                # Situation HUD *and* the queued-skill preload ride the operator's
                # user turn, not the codex ``instructions``, so the cross-turn
                # prompt cache prefix survives every follow-up turn — including a
                # turn on which the operator loads a skill mid-conversation. See
                # ``_mission_chat_user_message`` / ``_mission_chat_surface_message``.
                user_message=_mission_chat_user_message(
                    message,
                    situational_hud_content,
                    preloaded_skill_prompt=preloaded_skill_prompt,
                ),
                system_message=_mission_chat_surface_message(
                    persona,
                    surface_prompt,
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
                workdir=Path(workdir.path) if workdir.grounded else None,
            )
        )
        ChatToolPermissionStore().consume_turn(persona_id=persona.id, session_id=perm_session_id)
        if clarify_capture.requested and isinstance(result.raw, dict):
            result.raw["clarify_request"] = clarify_capture.request
        if isinstance(result.raw, dict):
            # Per-turn receipt of where the turn actually ran (and of any
            # configured-but-unusable path it degraded past). The caller records
            # it beside the turn's other receipts; the persona-level preview
            # carries the same typed rows in ``requirement_failures``.
            result.raw["mission_chat_workdir"] = workdir.receipt()
        return result


# Source label for the agent's own scratch turns during an operator chat reply.
# The caller persists the redacted canonical transcript under
# ``agent_runtime_persona_chat``; this scratch lineage is registered as a hidden
# session source (see tools/session_search_tool.py) so the agent's raw, in-flight
# copy never becomes recall-reachable while real cross-session recall stays on.
PERSONA_CHAT_SCRATCH_SOURCE = "agent_runtime_persona_chat_scratch"


def _persona_chat_system_prompt(persona: AgentPersona) -> str:
    display = getattr(persona, "display_name", None) or getattr(persona, "id", "the agent")
    role = str(role_from_persona(persona))
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
    # Same profile-owned SOUL lane as the mission-chat surface.
    soul = _mission_chat_soul_overlay(persona)
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
        "change state — actually use your tools and report the real result; there is no separate 'hand it off first' step.\n"
        "- Exactly TWO things gate what you can do, and both name themselves when they refuse: (1) the operator's current "
        "permission grant, and (2) the terminal safety envelope, which requires a per-role operator grant in the ROOT "
        "config.yaml for a small set of command classes (git push, destructive git, recursive delete, network egress) and "
        "hard-blocks a few others outright. An envelope refusal tells you the class, the exact config key that would grant "
        "it, and whether a grant is even possible. Relay that to the operator — do not retry, reword, or split the command, "
        "and never claim a capability gap you have not actually hit.\n"
        "- Never fabricate. Do not claim to have run a command, read a file, opened a path, or produced output unless you "
        "actually invoked the tool and are reporting its real result. If a capability isn't available, or your permission "
        "grant blocks it, say so plainly instead of inventing output.\n"
        "- When the operator asks you to send, brief, or coordinate named agents, use `agent_chat_send` for each agent. "
        "Ordinary persona chat is chat-only for every role: investigations, verification, MCP calls, and multi-agent work "
        "stay in chat and never imply goal creation or create hidden durable work.\n"
        "- If an order is ambiguous or underspecified — an unclear target, a missing detail, or a routing choice with more "
        "than one plausible answer — use the `clarify` tool to ask before acting, rather than guessing. Pass the question, and "
        "when the answer is one of a few known options pass them as `choices` (up to 4) so they render as pickable rows. On "
        "this channel `clarify` does NOT block: it ends your turn with your question, and the answer arrives as their next "
        "message in this same conversation. This is the operator channel, not an autonomous goal run: here, ask. Reach for it "
        "especially when you hold context the asker can't see (e.g. which of several same-role agents they mean).\n"
        "- When an agent you briefed replies with a clarifying question of their own, answer it by sending the choice back to "
        "them with `agent_chat_send` carrying the `clarify_token` that came inside their `clarify_request` — that lands your "
        "answer in the thread the question was asked in, so you don't have to get `session_id` right. Their reply's "
        "`session_id` still works too. Don't drop their question or answer it by guessing, and don't send the answer with "
        "neither (a send with no clarify_token and no session_id opens a NEW thread and they lose the question's context).\n"
        "- Teammates on your level are addressable by the `@personainst_*` handles in your Runtime Situation HUD. Threads are "
        "TASK-SCOPED with `agent_chat_send`: each new task you dispatch starts a fresh thread by default — just send, no flag. "
        "To continue an exchange you already started (their clarifying question, an in-task follow-up, a correction), pass the "
        "`session_id` that came back in their reply; the reply's `session_established` block tells you which thread you are in "
        "and which one it superseded. Optionally pass a short `title` to name the thread after the task. `new_session: false` "
        "continues that teammate's CURRENT thread — the most recently established one, which every fresh dispatch repoints, so "
        "it is not a stable per-pair home; to continue a SPECIFIC conversation, name its `session_id`. To recall "
        "earlier work with a teammate, search past sessions (`session_search`) or read a thread with `agent_chat_open` — do not "
        "keep an unrelated task thread alive just to preserve memory. `agent_chat_threads` lists your threads.\n"
        "- When a persona runs more than one instance on your level, a BARE persona id is ambiguous and the send is refused "
        "(`ambiguous_target`) with the candidate @personainst_* handles — address the exact instance you mean by its @handle.\n"
        "- Keep replies as clean teammate prose. Don't paste decision JSON, task scopes, acceptance criteria, handoff "
        "packets, or raw tool/tick scaffolding into the message — your tool calls are tracked separately in the trace lane.\n"
        "- One carve-out to that: image lines are content, not scaffolding. When you relay, quote, or summarize a "
        "teammate's reply that carries a MEDIA:<absolute image path> line, reproduce that line VERBATIM on a line of "
        "its own — never wrap it in backticks or a code fence, never fold it into a sentence, never retype or shorten "
        "the path. Same for a bare absolute screenshot path standing alone on its own line. WHY: a MEDIA: line alone "
        "on its own line is a DECLARATION, and the operator's console renders it as a titled image attachment card. "
        "Wrapping that line in backticks or a code fence un-declares it — the console never sees the prefix, and "
        "NOTHING renders. Retyping the path into a sentence, or dropping the prefix, is the quieter loss: the image "
        "still previews, but untitled, with the raw path left sitting in your prose, and it competes for the small "
        "per-message preview budget a declared line claims first. Either way the operator stops seeing the picture "
        "the way it was meant to be seen — so copy the line through exactly as it arrived, and put your provenance "
        "prose around it, never inside it."
    )


def _mission_chat_identity_prompt(persona: AgentPersona) -> str:
    """First-person identity block for the canonical Mission Control chat lane.

    This runtime-owned envelope names the selected Mission Control persona and
    makes self-relay impossible. It is distinct from the profile-owned SOUL
    overlay, which is resolved and inserted immediately after this block."""

    display = str(getattr(persona, "display_name", None) or getattr(persona, "id", "the agent")).strip()
    persona_id = str(getattr(persona, "id", "") or "").strip()
    role = str(role_from_persona(persona))
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
        f"voice.{never_self} Other runtime personas are teammates you may brief with "
        "`agent_chat_send`; you are not your own relay target."
    )


#: Fixed preamble prepended to the operator-selected workspace ``AGENTS.md``
#: body inside the surface message. Kept as one constant so the per-file
#: in-prompt attribution in ``prompt_observability`` can measure the workspace
#: part's contributed chars (preamble + body) WITHOUT drifting from the text
#: actually pasted here (T8, 2026-07-18).
MISSION_CHAT_WORKSPACE_AGENTS_PREAMBLE = (
    "Workspace instructions from the operator-selected AGENTS.md "
    "(apply these instructions to this turn):\n\n"
)


def _mission_chat_surface_message(
    persona: AgentPersona,
    surface_prompt: str | None,
    *,
    workspace_agents_content: str | None = None,
) -> str:
    """Compose the operator-chat system message (the codex ``instructions``):
    the persona's first-person identity block first, then the non-negotiable
    operative rules, then the operator's optional per-session surface prompt.
    The identity block gives the channel its selected runtime persona, the
    profile's own SOUL overlay supplies durable character and voice, and the
    rules always apply so the anti-fabrication invariant holds even when the
    operator supplies a session surface override.

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
    (T9a, 2026-07-18: the queued-skill preload — the secondary content-driven
    invalidation vector T5 flagged — was likewise moved OUT of this builder onto
    the operator user turn via ``_mission_chat_user_message``. It must NOT come
    back here: loading a skill mid-conversation would otherwise rotate the whole
    stable prefix for that turn.)"""

    identity = _mission_chat_identity_prompt(persona)
    operator_surface = (surface_prompt or "").strip()
    workspace_agents = (workspace_agents_content or "").strip()
    rules = _mission_chat_operative_rules()
    # The persona's OWN SOUL is the profile-owned identity layer between the
    # Mission Control identity hat and the channel rules. Profile personas used
    # to require a duplicated `soul_overlay_path: SOUL.md` binding on the
    # Mission Control persona row; profile-derived/free personas do not carry
    # that field, so a perfectly valid profile SOUL could be observed yet not
    # injected. `_mission_chat_soul_overlay` makes the profile's canonical
    # SOUL.md the default while preserving explicit safe relative overrides.
    soul = _mission_chat_soul_overlay(persona)
    parts = [identity, soul or "", rules]
    if workspace_agents:
        parts.append(MISSION_CHAT_WORKSPACE_AGENTS_PREAMBLE + workspace_agents)
    if operator_surface:
        parts.append(operator_surface)
    return "\n\n".join(part for part in parts if part)


def _mission_chat_user_message(
    message: str,
    situational_hud_content: str | None = None,
    *,
    preloaded_skill_prompt: str | None = None,
) -> str:
    """Compose the operator turn's user message: the operator's message (which
    already carries the redaction-safe rolling chat history baked in by
    the native structured conversation history), then the queued-skill preload (when
    the operator loaded a skill this turn), then the per-turn Runtime Situation
    HUD.

    Why the HUD *and* the skill preload ride here and not in the system prompt:
    the codex transport keys its cross-turn prompt cache on
    ``sha256(instructions + tools)``. A HUD whose roster / scope / mission state
    rotates every turn — e.g. ``QA Agent`` vs ``QA Agent (2)`` — would evict the
    ~13K-token stable prefix on every follow-up turn's first call; likewise a
    skill preload layered into ``instructions`` (T5's flagged secondary
    invalidation vector) would rotate the whole prefix on any turn the operator
    loads a skill. Riding both in the operator's user turn keeps the system
    prompt byte-stable for the life of the conversation while still giving the
    model the loaded skill and the same live picture the operator sees.

    Placement is load-bearing: the skill preload and HUD TRAIL the history +
    current operator message rather than leading it, and the HUD stays last. The
    user turn is already per-turn volatile (history grows, the message changes),
    so appending these at its tail keeps the append-only, cache-friendly ordering
    the spec requires — a volatile block ahead of the history would push it
    earlier in the (already uncached) input. This mirrors Hermes's own per-turn
    ephemeral-context injection, which appends recall / plugin context onto the
    current user turn rather than mutating the cached system prompt
    (agent/conversation_loop.py), and the skill-command pattern that injects the
    loaded skill as a user message to preserve caching (agent/skill_commands.py).

    Transport note: on the codex Responses path a mid-conversation ``system``
    message is dropped by the input converter and two consecutive ``user`` items
    violate the role-alternation invariant, so neither the HUD nor the skill
    preload can be a distinct non-user message without either vanishing or
    breaking alternation. Folding them onto the operator user turn is the
    transport-safe realization of "a per-turn message adjacent to the current
    operator message."
    """

    skill_prompt = (preloaded_skill_prompt or "").strip()
    hud = (situational_hud_content or "").strip()
    body = message if isinstance(message, str) else ("" if message is None else str(message))
    parts = [body, skill_prompt, hud]
    return "\n\n".join(part for part in parts if part)


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
                type("RepoScopeTask", (), {"affected_repos": [stage_repo]})()
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
    """Resolve the grounding repo from the current legacy stage, if any."""
    stage = getattr(ctx, "current_stage", None)
    stage_repo = str(getattr(stage, "repo", "") or "").strip() if stage is not None else ""
    if stage_repo not in {"EterniaBackend", "EterniaLauncher", "hermes-agent"}:
        return None
    persona_scope = getattr(persona, "repo_scope", None)
    if persona_scope:
        try:
            scoped = repo_execution_context_for_task(type("TaskPersonaScope", (), {"affected_repos": [persona_scope]})())
            stage_ctx = repo_execution_context_for_task(type("RepoScopeTask", (), {"affected_repos": [stage_repo]})())
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
    if _is_no_edit_context_stage(ctx) and str(role_from_persona(persona)) == "dev":
        names.update({"read_file", "search_files", "session_search", "browser_snapshot"})
    return sorted(names)


def _blocked_tool_names_for_chat(persona: AgentPersona, *, session_id: str | None) -> list[str]:
    options = permission_options_for_chat(persona, session_id=session_id)
    if permission_mode_is_unbounded(options.permission_mode):
        return []
    names = set(blocked_tool_names(persona))
    names.update(extra_blocked_tools_for_permission_mode(options.permission_mode))
    # T6a chat-lane cost policy: drop single heavy tools whose whole toolset must
    # stay enabled. ``skill_manage`` (skill authoring) rides here so the ``skills``
    # toolset keeps skill_search / skill_view / skills_list for read-only recall.
    # Shares the per-persona ``chat_lane_restore_toolsets`` knob with the toolset
    # exclusion, so an operator can restore it the same way. This applies only on
    # the bounded lane — the unbounded escape hatch returns [] above, though the
    # T6c registry-hygiene names are still unioned in at agent construction
    # (profile_runner) on every lane: hygiene is registry junk removal, not a
    # permission tier, so unbounded does not resurrect kanban/feishu.
    names.update(chat_lane_blocked_tools(restore=chat_lane_restore_toolsets(persona.id)))
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


def _enabled_toolsets_for_chat(
    persona: AgentPersona,
    *,
    session_id: str | None,
    admission=None,
) -> list[str]:
    """The single chat-lane toolset chokepoint (both the free-chat and operator/
    mission chat call sites funnel through here).

    Resolution order: permission mode → role/persona toolset resolution → chat
    capability augmentation → the chat-lane cost policy
    (``scope_chat_lane_toolsets``) that drops browser / vision / heavy-dev from a
    conversational lane → the MCP admission
    scope. ``unbounded`` permission mode bypasses the cost policy, but never the
    global chat-only default. A persona that wants a
    specific cost-excluded toolset back on its *bounded* chat lane restores it via
    ``agent_runtime.personas.<id>.chat_lane_restore_toolsets`` (see
    ``config.chat_lane_restore_toolsets``). Worker/dev task lanes never call this
    — they resolve toolsets via ``effective_toolsets`` directly.

    The MCP admission scope is applied LAST, after permission-mode resolution, on
    purpose: ``unbounded`` resolves ``all_registered_toolsets()``, which in a warm
    multi-persona process can contain another persona's admitted ``mcp-*``
    toolsets. Scoping after the mode is what makes "no permission mode can widen
    the admitted MCP set" true rather than aspirational. The same pure helper
    runs again at agent construction (``profile_runner._enabled_toolsets_for_run``)
    so no lane can bypass it; running it here keeps the operator-facing preview
    honest about the same boundary."""

    options = permission_options_for_chat(persona, session_id=session_id)
    if permission_mode_is_unbounded(options.permission_mode):
        resolved = all_registered_toolsets()
    else:
        resolved = _augment_chat_capabilities(persona, list(effective_toolsets(persona)))
        resolved = scope_chat_lane_toolsets(
            resolved, restore=chat_lane_restore_toolsets(persona.id)
        )
    admitted = admission.server_names if admission is not None else ()
    if admission is None and admission_enabled():
        # Only pay the policy resolve when the kill switch is on. With it off the
        # answer is always "nothing admitted" — and the scope below still strips
        # any MCP toolset that reached the resolved set, which is what keeps the
        # isolation property independent of the flag.
        admitted = resolve_mcp_admission(
            persona, lane=LANE_MISSION_CHAT, permission_mode=options.permission_mode
        ).server_names
    return scope_toolsets_to_admission(resolved, admitted_servers=admitted)


def chat_lane_capability_drops(
    persona: AgentPersona,
    *,
    session_id: str | None = None,
    permission_mode: str | None = None,
) -> tuple[ChatLaneDrop, ...]:
    """What the chat-lane cost policy REMOVES for this persona, typed (G5).

    The accounting twin of :func:`_enabled_toolsets_for_chat`: it walks the same
    resolution in the same order — permission mode, role/persona resolution,
    chat capability augmentation — and then asks the same droppers what they
    took out instead of what they left in. Same inputs, same policy, one
    authority; the kept list and the drop list cannot disagree.

    ``unbounded`` returns no drops because that mode genuinely bypasses the cost
    policy (``_enabled_toolsets_for_chat`` resolves the full registry) — a row
    there would report a drop that did not happen. ``permission_mode`` may be
    passed to account for a HYPOTHETICAL mode (the ``persona tool-diff
    --permission-mode`` preview); left ``None`` the stored chat permission for
    ``session_id`` is resolved, exactly as a live turn would.

    Pure accounting: it registers nothing, restores nothing, and is never
    consulted to decide what a turn ships.
    """

    mode = str(permission_mode or "").strip() or permission_options_for_chat(
        persona, session_id=session_id
    ).permission_mode
    if permission_mode_is_unbounded(mode):
        return ()
    restore = chat_lane_restore_toolsets(persona.id)
    resolved = _augment_chat_capabilities(persona, list(effective_toolsets(persona)))
    kept = scope_chat_lane_toolsets(resolved, restore=restore)
    # Local import: the tool→toolset map is the REGISTRY's answer, never a mirror
    # kept here (a silently drifting mirror is the bug class ``mcp_lane`` needed a
    # guard test for), and importing it lazily keeps ``persona_runtime``'s module
    # import free of ``model_tools``.
    from model_tools import get_toolset_for_tool

    return chat_lane_toolset_drops(
        resolved, restore=restore, persona_id=persona.id
    ) + chat_lane_tool_drops(
        restore=restore,
        persona_id=persona.id,
        enabled_toolsets=kept,
        toolset_for_tool=get_toolset_for_tool,
    )


def mission_chat_admission_line(
    persona: AgentPersona, *, session_id: str | None
) -> str:
    """The agent-visible MCP line for this turn's volatile envelope tail.

    ONE slot on the tail, two producers behind it, because the agent must hear
    one voice about MCP:

    * **Admission ON** — design §D3. Resolves the SAME pure policy the turn
      itself resolves (same function, same inputs, so the line and the turn's
      admission can never disagree) and renders the compact denial line.
    * **Admission OFF** — the R0 half (``mcp_lane``). Admission is inert with
      the flag off, so this used to return ``""`` and a declared-but-dark server
      was reported to the OPERATOR (``requirement_failures``) and to NOBODY the
      agent could hear. That blind spot was G5: the agent saw a tool list with
      no ``mcp__<server>__*`` entries and no explanation, and improvised — the
      exact W3 failure the design says is cheaper to prevent by telling the
      truth. It now renders the same honest fact from the same rows the operator
      reads.

    The kill switch still gates ADMISSION, not honesty. The flag-off path pays
    neither a root-config load nor a persona-profile read (see
    ``mcp_lane.mission_chat_mcp_lane_line`` for how that is preserved), and a
    persona that declares no MCP server pays nothing and renders nothing — so
    the envelope stays byte-identical for every turn that had nothing to be told.

    Returns ``""`` when there is nothing to say.
    """

    if not admission_enabled():
        return mission_chat_mcp_lane_line(persona)
    try:
        admission = resolve_mcp_admission(
            persona,
            lane=LANE_MISSION_CHAT,
            permission_mode=permission_options_for_chat(
                persona, session_id=session_id
            ).permission_mode,
        )
    except Exception:  # pragma: no cover - a context line must never fail a turn
        return ""
    return render_mcp_admission_line(admission)


def mission_chat_operating_skills(
    persona: AgentPersona, *, session_id: str | None
) -> list[str]:
    """The operating manual(s) this turn's ADMITTED MCP surface comes with.

    The twin of :func:`mission_chat_admission_line`, and deliberately built from
    the SAME pure policy with the SAME inputs: the line tells the agent which
    declared servers it did NOT get, and this tells the turn which manuals it
    must be handed for the ones it DID. Resolved once here rather than inferred
    from the rendered line, so the two can never describe different admissions.

    Flag-off costs nothing — no root-config load past the kill switch, no
    persona-profile read, no filesystem — because with admission off nothing is
    ever admitted and there is no surface to document. A persona whose admitted
    servers have no registered manual, or who was never granted it, gets ``[]``
    and the turn's preload is byte-identical to what it was before.

    Never raises: an unavailable policy must degrade the turn's context, never
    fail the turn.
    """

    if not admission_enabled():
        return []
    try:
        admission = resolve_mcp_admission(
            persona,
            lane=LANE_MISSION_CHAT,
            permission_mode=permission_options_for_chat(
                persona, session_id=session_id
            ).permission_mode,
        )
    except Exception:  # pragma: no cover - a context input must never fail a turn
        return []
    return admitted_operating_skill_ids(
        admission, granted_skills=getattr(persona, "skills", None) or ()
    )


def chat_runtime_tool_contract(
    persona: AgentPersona, *, session_id: str | None
) -> dict[str, list[str]]:
    """Return the exact tool inputs used to construct an operator-chat actor."""

    return {
        "enabled_toolsets": _enabled_toolsets_for_chat(
            persona, session_id=session_id
        ),
        "blocked_tool_names": _blocked_tool_names_for_chat(
            persona, session_id=session_id
        ),
    }


def apply_chat_lane_tool_scope(
    persona: AgentPersona,
    options: "ToolVisibilityOptions",
    *,
    session_id: str | None,
) -> "ToolVisibilityOptions":
    """Thread the REAL chat-lane resolution onto a tool-visibility PREVIEW (T9b).

    The operator-facing permission preview (``persona_instance_summary`` /
    ``persona_instance_tool_detail``) resolved ``effective_toolsets(persona)`` —
    the persona's raw configured set — so it omitted BOTH the operator-chat
    capability augmentation (agent_chat / board / clarify) and the
    T3/T6a chat-lane cost scoping (browser / vision / file / terminal /
    skill_manage cut). The preview therefore lied about the actual chat lane.

    This mutates ``options`` so the preview reuses the ONE chat-lane authority:
    ``enabled_toolsets`` becomes the chat-lane-scoped toolset list
    (``_enabled_toolsets_for_chat``) and ``chat_lane_blocked_tool_names`` becomes
    the chat lane's authoritative block (``_blocked_tool_names_for_chat`` unioned
    with the fork registry hygiene the runner enforces on every lane, minus the
    ``clarify`` unblock the chat bridge grants). ``resolve_tool_visibility`` then
    emits ``final_model_tools`` byte-identical to the schema the chat lane ships.
    Display-parity only — no policy change, no parallel resolver.

    G5: it also threads the TYPED account of what that scoping removed
    (:func:`chat_lane_capability_drops`) and of this persona's repo grounding
    (``mission_chat_workdir_for_persona``), so one preview reports what SURVIVED
    *and* what was taken away and why. A list of survivors was never an account
    of the removals — which is how "I have no terminal" read as an unexplained
    absence instead of a by-design, restorable cost cut.
    """

    permission = permission_options_for_chat(persona, session_id=session_id)
    if permission_mode_is_unbounded(permission.permission_mode):
        configured = all_registered_toolsets()
    else:
        configured = _augment_chat_capabilities(
            persona, list(effective_toolsets(persona))
        )
    options.configured_toolsets = configured
    options.enabled_toolsets = _enabled_toolsets_for_chat(persona, session_id=session_id)
    options.chat_lane_blocked_tool_names = _blocked_tool_names_with_registry_hygiene(
        _blocked_tool_names_for_chat(persona, session_id=session_id)
    )
    options.chat_lane_capability_drops = chat_lane_capability_drops(
        persona, session_id=session_id
    )
    # Preview scope: the CONFIG rung of the workdir ladder only. A live turn also
    # offers the workspace pointer (``--agents-file``), which is a per-turn fact
    # this persona-level preview has no honest access to.
    options.mission_chat_workdir = mission_chat_workdir_for_persona(persona)
    return options


# Operator-chat first-class capabilities that a chat persona gets regardless of
# what its persisted/config toolset list happens to enumerate. This is capability
# *discovery* — it does not widen any downstream gate.
#
# `agent_chat`, `board` and `clarify` are UNCONDITIONAL on purpose (mission-lane
# removal, S1). This is the ONLY path that puts `board` and `agent_chat` on a chat lane, and
# it used to gate them on a hardcoded role map. Such a gate silently strips the Mission Board
# and agent-to-agent chat from every chat persona the moment either happens. Both
# are explicit KEEP. The gate had no protective value either: all four roles in the
# dict already allow `board` and `agent_chat`, so removing it changes nothing for a
# known role and *restores* the intended surface for an unknown one.
#
# `clarify` is likewise universal: ask a question, get the answer as the next
# message in the same session.
_CHAT_CAPABILITY_TOOLSETS = ("agent_chat", "board", "clarify")


def _augment_chat_capabilities(persona: AgentPersona, toolsets: list[str]) -> list[str]:
    augmented = list(toolsets)
    for toolset in _CHAT_CAPABILITY_TOOLSETS:
        if toolset in augmented:
            continue
        augmented.append(toolset)
    return augmented


def _is_no_edit_context_stage(ctx: AgentContext) -> bool:
    stage = ctx.current_stage
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
    run_budget = None
    if isinstance(profile_timing, dict):
        # ``run_budget`` is the ONE structured entry in an otherwise
        # ``_ms``/``_count`` integer map, so ``_record_timing_value`` filters it
        # out by design — and the run record therefore lost the only answer to
        # "what bounded this run?". Lift it onto ``run.llm`` as its own key: the
        # timing map keeps its integer contract, and nothing has to smuggle a
        # nested dict through a filter written for scalars.
        run_budget = _safe_run_budget_block(profile_timing.get("run_budget"))
        for key, value in profile_timing.items():
            timing_key = key if str(key).startswith("profile_") else f"profile_{key}"
            _record_timing_value(timing_map, timing_key, value)
    if run_budget is None:
        # A later result without an accounting block must not erase the block an
        # earlier one recorded — the same carry-forward the timing map gets.
        previous = getattr(run, "llm", None)
        if isinstance(previous, dict):
            run_budget = _safe_run_budget_block(previous.get("run_budget"))
    llm["run_budget"] = run_budget
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


def _safe_run_budget_block(value: object) -> dict[str, object] | None:
    """The run's WHOLE budget accounting block, kept structured.

    Delegates to ``run_budget.safe_accounting_block`` — ONE reader for the block
    at every persistence boundary (run record, mission-chat turn journal,
    chat-history projection). This used to be a local copy of that bounding
    logic; a second copy is how the two boundaries start disagreeing about what
    the block IS, which is the defect ``run_budget`` exists to retire. Kept as a
    module-local seam because callers/tests patch it here.
    """

    from .run_budget import safe_accounting_block

    return safe_accounting_block(value)


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


def _mission_chat_soul_overlay(persona: AgentPersona) -> str | None:
    """Resolve the profile-owned SOUL text used by Mission Control chat.

    A profile-backed persona owns ``SOUL.md`` by convention.  An explicit safe
    relative ``soul_overlay_path`` still wins, while an unbound legacy persona
    keeps the old opt-in behavior.  Resolution remains profile-isolated through
    :func:`_safe_read_soul_overlay`, so a missing persona profile can never fall
    through to the operator's SOUL.
    """

    hermes_profile = getattr(persona, "hermes_profile", None)
    configured_path = getattr(persona, "soul_overlay_path", None)
    path_value = configured_path or ("SOUL.md" if hermes_profile else None)
    return _safe_read_soul_overlay(path_value, hermes_profile=hermes_profile)


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
