from __future__ import annotations

import json
from pathlib import Path
import time
from urllib.parse import urlparse
from typing import Callable, Protocol

from hermes_constants import get_hermes_home

from . import paths
from .config import load_agent_runtime_config
from .context_builder import AgentContext, build_context, render_context
from .decision_contract_registry import prompt_contract_markdown
from .decision_schema import (
    DECISION_SCHEMA,
    AgentDecision,
    DecisionPayloadInvalid,
    parse_structured_decision,
    validate_decision_for_role,
)
from .decision_contracts import validate_planning_decision
from .models import AgentPersona, AgentRun
from .mission_plan import current_plan_stage
from .personas import blocked_tool_names, effective_toolsets, load_bundled_prompt, role_from_persona
from .profile_context import resolve_persona_profile
from .provider_health import assert_provider_health_for_persona
from .profile_runner import AgentRunRequest, AgentRunResult, ProfileAgentRunner, RunBudgetExceeded
from .progress import RunProgressSink
from .repo_context import RepoExecutionContext, repo_execution_context_for_task
from .stage_intent import stage_requires_product_edit
from .store import RunStore, _safe_session_id
from .tool_permissions import extra_blocked_tools_for_permission_mode, permission_options_for_chat


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
    ):
        self._default_provider = default_provider
        self._default_model = default_model
        self._runner = agent_runner or ProfileAgentRunner(agent_factory=agent_factory, credential_pool=credential_pool, session_db=session_db)

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
            ctx.repo_context = _repo_context_for_render(repo_ctx)
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
        max_wall_seconds: float | None = 120.0,
        max_api_calls: int | None = 8,
        max_total_tokens: int | None = None,
        stream_callback: Callable[[str | None], None] | None = None,
    ) -> AgentRunResult:
        """Run one plain conversational turn for an operator persona chat.

        This deliberately bypasses the decision-contract pipeline: the agent
        gets a conversational system prompt (no decision menu, no task scoping)
        and the operator's raw message, and returns free-text. This is the
        chat-first path — the harness task/decision machinery only engages when
        the operator explicitly asks for work.

        Recall: when the runtime is constructed with a ``session_db`` the agent
        can ``session_search`` the operator's prior (redaction-safe) persona
        chats and prefetch its durable memory. The agent's own scratch turns
        are persisted under :data:`PERSONA_CHAT_SCRATCH_SOURCE`, which is hidden
        from recall, so this never double-writes the curated operator transcript
        (the caller owns the redacted canonical writes) and never leaks an
        unredacted copy back into recall.
        """

        binding = resolve_persona_profile(persona)
        if binding.readiness == "missing_profile":
            raise ValueError(binding.summary)
        assert_provider_health_for_persona(persona)
        return self._runner.run(
            AgentRunRequest(
                profile=binding.hermes_profile,
                provider=persona.provider or self._default_provider,
                model=persona.model or self._default_model or "",
                api_mode=persona.api_mode,
                enabled_toolsets=effective_toolsets(persona),
                blocked_tool_names=_blocked_tool_names_for_chat(persona, session_id=session_id),
                quiet_mode=True,
                skip_context_files=not bool(getattr(persona, "include_core_context_files", False)),
                skip_memory=False,
                platform=PERSONA_CHAT_SCRATCH_SOURCE,
                session_id=session_id,
                max_wall_seconds=max_wall_seconds,
                max_api_calls=max_api_calls,
                max_total_tokens=max_total_tokens,
                user_message=message,
                system_message=_persona_chat_system_prompt(persona),
                stream_callback=stream_callback,
                runtime_root=paths.store_root(),
            )
        )

    def mission_chat_reply(
        self,
        persona: AgentPersona,
        message: str,
        *,
        session_id: str | None = None,
        provider_override: str | None = None,
        model_override: str | None = None,
        surface_prompt: str | None = "",
        max_wall_seconds: float | None = 120.0,
        max_api_calls: int | None = 8,
        max_total_tokens: int | None = None,
        stream_callback: Callable[[str | None], None] | None = None,
    ) -> AgentRunResult:
        """Run the canonical Mission Control chat path.

        Unlike the older free-floating helper, this uses the normal Hermes
        profile context stack: SOUL.md, profile memory, skills/context files,
        and the profile's standard chat behavior. Mission Control contributes
        only an optional surface prompt, blank by default.
        """

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
        return self._runner.run(
            AgentRunRequest(
                profile=binding.hermes_profile,
                provider=runtime_provider,
                model=runtime_model,
                api_mode=persona.api_mode,
                enabled_toolsets=effective_toolsets(persona),
                blocked_tool_names=_blocked_tool_names_for_chat(persona, session_id=session_id),
                quiet_mode=True,
                skip_context_files=False,
                skip_memory=False,
                platform=PERSONA_CHAT_SCRATCH_SOURCE,
                session_id=session_id,
                max_wall_seconds=max_wall_seconds,
                max_api_calls=max_api_calls,
                max_total_tokens=max_total_tokens,
                user_message=message,
                system_message=surface_prompt or None,
                stream_callback=stream_callback,
                runtime_root=paths.store_root(),
            )
        )


# Source label for the agent's own scratch turns during an operator chat reply.
# The caller persists the redacted canonical transcript under
# ``agent_runtime_persona_chat``; this scratch lineage is registered as a hidden
# session source (see tools/session_search_tool.py) so the agent's raw, in-flight
# copy never becomes recall-reachable while real cross-session recall stays on.
PERSONA_CHAT_SCRATCH_SOURCE = "agent_runtime_persona_chat_scratch"


def _persona_chat_system_prompt(persona: AgentPersona) -> str:
    display = getattr(persona, "display_name", None) or getattr(persona, "id", "the agent")
    role = role_from_persona(persona).value
    return (
        f"You are {display}, a Mission Control operator-channel agent (role: {role}). "
        "You are in a direct, real-time chat with a single human operator — your teammate, not an end user. "
        f"{_persona_chat_voice(role, display)} "
        "Voice: warm, plain text, teammate-tight. Lead with the answer; skip preamble, filler, and restating the question. "
        "A sentence or two is usually enough — only go longer when the operator clearly wants depth. "
        "Hard rules: never emit JSON, decision objects, task scopes, acceptance criteria, handoff packets, or tool/tick chatter. "
        "Do NOT scope, create, dispatch, or route tasks from here — chat is the conversational layer, not the work pipeline. "
        "If the operator just greets you or makes small talk, talk back like a teammate. "
        "Recall: lean on the inline chat history for continuity. Reach for session_search only when the operator points at "
        "something specific from a past session you can't already see, and consult your durable memory only when it actually "
        "bears on the reply — don't fish. "
        "Escalation: when the operator genuinely asks you to DO work (build, fix, investigate, run, change code), don't start "
        "executing from chat — confirm the ask in a sentence and tell them to hand it off as real work (the Assign Work action / "
        "task pipeline) so it runs with proof and budgets. You can help shape the scope conversationally first."
    )


def _persona_chat_voice(role: str, display: str) -> str:
    if role == "alice_supervisor":
        return (
            f"As {display} you run point for the operator across the mission: you coordinate the dev/QA personas, track what's "
            "in flight, and give crisp, decisive read-outs. Chief-of-staff energy, not cheerleader."
        )
    if role == "qa":
        return (
            "You are the quality gate: skeptical, precise, evidence-first. In chat you talk through risks, what you'd verify, "
            "and what proof you'd want — without actually running a gate."
        )
    if role == "dev":
        return (
            "You are a senior engineer: concrete, pragmatic, fluent in the repo. In chat you reason about approach, tradeoffs, "
            "and what you'd change — without editing files until it's handed off as real work."
        )
    return f"You speak as {display}: a capable, straight-talking teammate."


def _repo_context_for_persona(persona: AgentPersona, ctx: AgentContext) -> RepoExecutionContext | None:
    if role_from_persona(persona) != "dev":
        return None
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
    names = set(blocked_tool_names(persona))
    names.update(extra_blocked_tools_for_permission_mode(options.permission_mode))
    return sorted(names)


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
    payload_contracts = prompt_contract_markdown()
    parts = [load_bundled_prompt(role)]
    overlay = Path(__file__).with_name("prompts") / "shared_harness_overlay.md"
    if overlay.exists():
        parts.append(overlay.read_text(encoding="utf-8").strip())
    soul_overlay = _safe_read_soul_overlay(persona.soul_overlay_path)
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
    parts.extend(
        [
            "# Universal Harness Rules\n"
            "You are a task-bound Agent Runtime Harness persona. Return exactly one AgentDecision JSON object. "
            "Do not ask the human questions from inside a tick. Do not claim proof you did not receive from the harness. "
            "Do not use delegated agents, cron jobs, memory writes, messaging, or Kanban side effects. "
            "Do not use Kanban vocabulary or mutate Kanban state. The Autonomy / Tool Economy Contract in the tick context is Harness-generated public operating context; obey its budgets and do not add new AgentDecision keys unless the payload contract allows them. "
            "When Mission HUD is present, treat mission_hud.agent_hud as the only live control panel: choose exactly one visible agent_hud.options item, prefer agent_hud.recommended_action, use only recommended_action.allowed_payload_keys, and shape the payload from recommended_action.payload_skeleton. Unknown payload keys are invalid. Open only the named recommended_action.skill_ref when the HUD says deeper guidance is needed. "
            "Generic Hermes core guidance about tool persistence, task completion, profile identity, or manual-session workflow is subordinate to this Harness contract: returning a valid AgentDecision is the action for this tick. "
            "If the stage is no-edit, proof-backed, or explicitly requests Harness-owned proof, do not call extra tools just to satisfy generic tool-use guidance; emit the precise AgentDecision instead.",
            "# Stage Ownership and Handoff\n"
            "Act like an accountable teammate, not a stateless robot. Know your role, current task state, current stage, available proof_ids, and the next owner. "
            "A stage is complete only when your payload says what you finished, what proof_ids support it, known gaps, and who receives the handoff. "
            "PM/Neko hands scoped work to Dev. Dev notifies Neko for QA coordination by requesting QA review only with real proof_ids and handoff.to=qa; the Harness routes dev_ready_for_qa through Neko before QA in the multi-Dev specialist model. "
            "QA approves implementation only after reviewing proof_ids and attaching/verifying an implementation verdict; otherwise request tests/fixes or block with exact gaps. "
            "If you discover a repeated workflow failure, report it as a Harness/skill intervention rather than looping.",
            f"# AgentDecision JSON Schema\n```json\n{compact_schema}\n```",
            f"# AgentDecision Payload Contracts\n{payload_contracts}\n"
            "Use `recipe_id` when the Autonomy packet lists a matching `available_proof_recipes` entry; then omit commands and let the Harness supply the exact recipe commands, sandbox, dirty-check, marker checks, and proof metadata. "
            "After Harness attaches command proof IDs, hand off to QA with those existing proof_ids. "
            "Before blocking, inspect/grep your own run or event logs, keep the reason brief, and point at the redaction-safe log line number that proves the blocker. "
            "If a previous decision parse failed, fix the exact missing/invalid key named in the repair context.",
        ]
    )
    return "\n\n".join(part for part in parts if part)


def _specialist_dev_guidance(persona: AgentPersona) -> str:
    if role_from_persona(persona) != "dev":
        return ""
    shared = (
        "# Specialist Dev Loop Guard\n"
        "Operate with Alice/Neko-style budget discipline: use one bounded repo-scoped search/read pass, then choose target files, patch, test, request deterministic proof, or block with exact evidence. "
        "If repeated read/search/tool-loop warnings appear before patch/test/proof progress, stop immediately and return a smaller stage plan, `request_test_run`, or `block` so Neko can slice or steer. "
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
            "For product-edit stages, do the work like one uninterrupted competent developer: inspect narrowly, edit files, run focused self-tests in-session with terminal/code tools, then deliver with `propose_patch`. "
            "Include changed files and concise self-test results in `tests`; include `delivery.self_test_evidence_ids` when the Harness HUD lists recorded self-test evidence. "
            "Do not use `request_test_run` as your normal inner loop. Use Harness proof only when the Mission HUD exposes `request_gate`, the stage is no-edit/certification, QA requests a missing gate, or you are repairing a failed final gate. "
            "After delivery, the Harness owns the final deterministic gate and will return failed proof IDs to this same worker if repair is needed."
        )
    if role == "qa":
        return (
            "# Normal Worker Flow QA\n"
            "Self-test evidence helps triage but is not release proof. Base implementation approval on Harness final gate proof IDs and required visual/MCP artifacts. "
            "Request exactly one missing command or visual gate when proof is absent or stale; otherwise emit `report_qa_verdict` with cited proof IDs."
        )
    if role == "alice_supervisor":
        return (
            "# Normal Worker Flow Neko\n"
            "Prefer same-worker repair over spawning new work. Wait/request-human only at kickoff or for true human/safety blockers. "
            "Route by attached evidence, failed proof IDs, and worker HUD state; release QA only after final gate proof is attached."
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


def _safe_read_soul_overlay(path_value: str | None) -> str | None:
    if not path_value:
        return None
    raw = Path(path_value)
    if raw.is_absolute() or not _is_safe_soul_overlay_path(raw):
        return None
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


def _is_safe_soul_overlay_path(path: Path) -> bool:
    if path.suffix.lower() != ".md":
        return False
    unsafe_parts = {".env", "env", "auth", "credentials", "credential", "secrets", "secret", "tokens", "token", "config"}
    return not any(part.lower() in unsafe_parts or part.startswith(".") for part in path.parts)
