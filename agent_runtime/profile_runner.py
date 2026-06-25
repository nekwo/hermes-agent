from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
from threading import Event, RLock, Timer
import time
from typing import Any, Callable
import re

from hermes_cli.profiles import get_profile_dir, normalize_profile_name, profile_exists
from hermes_cli.runtime_provider import resolve_runtime_provider

from .profile_context import PersonaProfileBinding, persona_profile_context


class ProfileRunnerError(RuntimeError):
    """Raised before agent construction when a profile-bound run cannot start."""


class RunBudgetExceeded(ProfileRunnerError):
    """Raised when a live persona run exceeds its configured budget."""

    def __init__(self, message: str, *, session_id: str | None = None):
        super().__init__(message)
        self.session_id = session_id


READ_SEARCH_TOOLS = frozenset({"read_file", "search_files", "session_search", "browser_snapshot"})
PATCH_TOOLS = frozenset({"patch", "apply_patch", "write_file", "edit_file", "file.write", "file.edit"})


@dataclass(slots=True)
class AgentRunRequest:
    profile: str | None
    provider: str | None = None
    model: str | None = None
    api_mode: str | None = None
    enabled_toolsets: list[str] | None = None
    disabled_toolsets: list[str] | None = None
    blocked_tool_names: list[str] | None = None
    skills: list[str] | None = None
    session_id: str | None = None
    platform: str = "agent_runtime"
    quiet_mode: bool = True
    skip_context_files: bool = True
    skip_memory: bool = True
    max_iterations: int = 90
    max_wall_seconds: float | None = None
    max_api_calls: int | None = None
    max_total_tokens: int | None = None
    system_message: str | None = None
    user_message: str = ""
    task_id: str | None = None
    progress_callback: Callable[[dict[str, Any]], None] | None = None
    stream_callback: Callable[[str | None], None] | None = None
    runtime_root: Path | None = None
    workdir: Path | None = None
    stop_on_repeated_read_search: bool = False
    tool_budget_limits: dict[str, Any] | None = None


@dataclass(slots=True)
class AgentRunResult:
    final_response: str
    session_id: str | None
    provider: str | None
    model: str | None
    base_url: str | None
    messages: list[dict[str, Any]]
    api_calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: int | None = None
    profile_timing: dict[str, int] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class _ToolBudgetGuard:
    stop_on_repeated_read_search: bool = False
    read_search_limit: int = 6
    skill_load_limit: int = 2
    repeated_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    warned: set[tuple[str, str]] = field(default_factory=set)
    aggregate_read_search_count: int = 0
    has_patch_progress: bool = False
    tripped_reason: str | None = None
    interrupt_callback: Callable[[str], None] | None = None

    @classmethod
    def from_limits(cls, *, stop_on_repeated_read_search: bool, tool_budget_limits: dict[str, Any] | None):
        limits = tool_budget_limits or {}
        return cls(
            stop_on_repeated_read_search=stop_on_repeated_read_search,
            read_search_limit=_positive_limit(limits.get("read_search_limit"), fallback=6),
            skill_load_limit=_positive_limit(limits.get("skill_load_limit"), fallback=2),
            has_patch_progress=bool(limits.get("has_patch_progress")),
        )

    @property
    def skill_warning_threshold(self) -> int:
        return max(3, self.skill_load_limit + 1)

    def set_interrupt_callback(self, callback: Callable[[str], None]) -> None:
        self.interrupt_callback = callback

    def trip(self, reason: str) -> None:
        if not self.tripped_reason:
            self.tripped_reason = reason
        if self.interrupt_callback is None:
            return
        try:
            self.interrupt_callback(reason)
        except Exception:
            return


class ProfileAgentRunner:
    def __init__(self, *, agent_factory: Callable[..., Any] | None = None, credential_pool=None, session_db=None):
        self._uses_default_agent_factory = agent_factory is None
        self._agent_factory = agent_factory or _default_agent_factory
        self._credential_pool = credential_pool
        self._session_db = session_db

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        _validate_workdir(request.workdir)
        binding = _binding_for_profile(request.profile)
        if binding.readiness != "ready":
            raise ProfileRunnerError(binding.summary)
        started = time.perf_counter()
        raw_result, agent, profile_timing = self._execute_agent_run(binding, request)
        normalize_started = time.perf_counter()
        result = _normalize_result(raw_result, agent=agent)
        profile_timing["result_normalize_ms"] = _emit_request_timing(request, "result_normalize", normalize_started)
        result.latency_ms = _elapsed_ms(started)
        result.profile_timing = profile_timing
        if isinstance(result.raw, dict):
            result.raw["profile_timing"] = dict(profile_timing)
        budget_started = time.perf_counter()
        _emit_budget_pressure_warning(result, request)
        _enforce_result_budgets(result, request)
        profile_timing["budget_checks_ms"] = _emit_request_timing(request, "budget_checks", budget_started)
        result.profile_timing = profile_timing
        if isinstance(result.raw, dict):
            result.raw["profile_timing"] = dict(profile_timing)
        if result.raw.get("failed") and result.raw.get("error"):
            raise ProfileRunnerError(str(result.raw.get("error")))
        return result

    def _execute_agent_run(self, binding: PersonaProfileBinding, request: AgentRunRequest) -> tuple[Any, Any, dict[str, int]]:
        timing: dict[str, int] = {}
        with _WORKDIR_LOCK, persona_profile_context(binding, runtime_root=request.runtime_root), _agent_workdir(request.workdir):
            try:
                runtime_started = time.perf_counter()
                runtime = _resolve_request_runtime(request)
                timing["runtime_resolve_ms"] = _emit_request_timing(request, "runtime_resolve", runtime_started)
            except Exception:
                if self._uses_default_agent_factory:
                    raise
                runtime = {}
                timing["runtime_resolve_ms"] = _emit_request_timing(request, "runtime_resolve", runtime_started, status="failed")
            budget_guard = _ToolBudgetGuard.from_limits(
                stop_on_repeated_read_search=request.stop_on_repeated_read_search,
                tool_budget_limits=request.tool_budget_limits,
            )
            status_callback = _profile_status_callback(request, timing)
            construct_started = time.perf_counter()
            agent = self._agent_factory(
                provider=runtime.get("provider") or request.provider,
                model=runtime.get("model") or request.model or "",
                api_mode=request.api_mode or runtime.get("api_mode"),
                base_url=runtime.get("base_url"),
                api_key=runtime.get("api_key"),
                enabled_toolsets=request.enabled_toolsets,
                disabled_toolsets=request.disabled_toolsets,
                blocked_tool_names=request.blocked_tool_names,
                quiet_mode=request.quiet_mode,
                skip_context_files=request.skip_context_files,
                skip_memory=request.skip_memory,
                platform=request.platform,
                session_id=request.session_id,
                credential_pool=self._credential_pool,
                session_db=self._session_db,
                status_callback=status_callback,
                max_iterations=request.max_iterations,
                tool_progress_callback=_progress_adapter(request.progress_callback, "run.progress", guard=budget_guard),
                tool_start_callback=_progress_adapter(request.progress_callback, "run.tool.started", guard=budget_guard),
                tool_complete_callback=_progress_adapter(request.progress_callback, "run.tool.finished", guard=budget_guard),
            )
            timing["agent_construct_ms"] = _emit_request_timing(request, "agent_construct", construct_started)
            budget_guard.set_interrupt_callback(lambda reason: _interrupt_agent_for_budget(agent, reason))
            max_wall_seconds = _positive_float(request.max_wall_seconds)
            if max_wall_seconds is None:
                conversation_started = time.perf_counter()
                conversation_kwargs: dict[str, Any] = {
                    "user_message": request.user_message,
                    "system_message": request.system_message,
                    "task_id": request.task_id,
                }
                if request.stream_callback is not None:
                    conversation_kwargs["stream_callback"] = request.stream_callback
                raw_result = agent.run_conversation(**conversation_kwargs)
                _attach_model_input_observability(raw_result, agent=agent, request=request)
                timing["conversation_call_ms"] = _emit_request_timing(request, "conversation_call", conversation_started)
                if budget_guard.tripped_reason:
                    raise RunBudgetExceeded(budget_guard.tripped_reason, session_id=getattr(agent, "session_id", None))
                return raw_result, agent, timing

            expired = Event()

            def interrupt_for_budget() -> None:
                expired.set()
                if hasattr(agent, "interrupt"):
                    try:
                        agent.interrupt("live run budget exceeded")
                    except Exception:
                        pass
                if request.progress_callback is not None:
                    request.progress_callback(
                        {
                            "type": "run.progress",
                            "phase": "runaway_warning",
                            "severity": "critical",
                            "step": "wall_clock_budget_exceeded",
                            "status": "failed",
                            "summary": f"Live run exceeded wall-clock budget: wall_seconds={max_wall_seconds:g}",
                        }
                    )

            timer = Timer(max_wall_seconds, interrupt_for_budget)
            timer.daemon = True
            timer.start()
            try:
                conversation_started = time.perf_counter()
                conversation_kwargs = {
                    "user_message": request.user_message,
                    "system_message": request.system_message,
                    "task_id": request.task_id,
                }
                if request.stream_callback is not None:
                    conversation_kwargs["stream_callback"] = request.stream_callback
                raw_result = agent.run_conversation(**conversation_kwargs)
                _attach_model_input_observability(raw_result, agent=agent, request=request)
                timing["conversation_call_ms"] = _emit_request_timing(request, "conversation_call", conversation_started)
            except BaseException:
                timing["conversation_call_ms"] = _emit_request_timing(request, "conversation_call", conversation_started, status="failed")
                if expired.is_set():
                    raise RunBudgetExceeded(f"live run budget exceeded: wall_seconds={max_wall_seconds:g}", session_id=getattr(agent, "session_id", None))
                raise
            finally:
                timer.cancel()
            if expired.is_set():
                raise RunBudgetExceeded(f"live run budget exceeded: wall_seconds={max_wall_seconds:g}", session_id=getattr(agent, "session_id", None))
            if budget_guard.tripped_reason:
                raise RunBudgetExceeded(budget_guard.tripped_reason, session_id=getattr(agent, "session_id", None))
            return raw_result, agent, timing


def _enforce_result_budgets(result: AgentRunResult, request: AgentRunRequest) -> None:
    max_api_calls = _positive_int(request.max_api_calls)
    if max_api_calls is not None:
        api_calls = _positive_int(result.api_calls)
        if api_calls is not None and api_calls > max_api_calls:
            raise RunBudgetExceeded(f"live run budget exceeded: api_calls={api_calls}/{max_api_calls}", session_id=result.session_id)
    max_total_tokens = _positive_int(request.max_total_tokens)
    if max_total_tokens is not None:
        total_tokens = _positive_int(result.total_tokens)
        if total_tokens is not None and total_tokens > max_total_tokens:
            raise RunBudgetExceeded(f"live run budget exceeded: total_tokens={total_tokens}/{max_total_tokens}", session_id=result.session_id)


def _emit_budget_pressure_warning(result: AgentRunResult, request: AgentRunRequest) -> None:
    callback = request.progress_callback
    if callback is None:
        return
    max_total_tokens = _positive_int(request.max_total_tokens)
    total_tokens = _positive_int(result.total_tokens)
    if max_total_tokens is None or total_tokens is None:
        return
    if total_tokens > max_total_tokens:
        return
    threshold = int(max_total_tokens * 0.8)
    if total_tokens < threshold:
        return
    try:
        callback(
            {
                "type": "run.progress",
                "phase": "runaway_warning",
                "severity": "warning",
                "step": "budget_pressure",
                "status": "warning",
                "summary": "Run is approaching the live token budget; stop broad exploration and pivot to proof, QA handoff, or an exact blocker.",
                "budget_kind": "total_tokens",
                "budget_used": total_tokens,
                "budget_limit": max_total_tokens,
                "budget_ratio": round(total_tokens / max_total_tokens, 3),
                "next_expected": "proof_or_block_now",
            }
        )
    except Exception:
        return


def _validate_workdir(workdir: Path | None) -> None:
    if workdir is None:
        return
    path = Path(workdir).expanduser()
    if not path.is_dir():
        raise ProfileRunnerError("requested agent workdir does not exist or is not a directory")


def _interrupt_agent_for_budget(agent: Any, reason: str) -> None:
    interrupt = getattr(agent, "interrupt", None)
    if not callable(interrupt):
        return
    interrupt(reason)


@contextmanager
def _agent_workdir(workdir: Path | None):
    if workdir is None:
        yield
        return
    path = Path(workdir).expanduser().resolve()
    with _WORKDIR_LOCK:
        previous_cwd = Path.cwd()
        had_terminal_cwd = "TERMINAL_CWD" in os.environ
        previous_terminal_cwd = os.environ.get("TERMINAL_CWD")
        try:
            os.chdir(path)
            os.environ["TERMINAL_CWD"] = str(path)
        except OSError as exc:
            raise ProfileRunnerError("requested agent workdir could not be entered") from exc
        try:
            yield
        finally:
            os.chdir(previous_cwd)
            if had_terminal_cwd:
                os.environ["TERMINAL_CWD"] = previous_terminal_cwd or ""
            else:
                os.environ.pop("TERMINAL_CWD", None)


_WORKDIR_LOCK = RLock()


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _emit_request_timing(request: AgentRunRequest, timing_key: str, started: float, *, status: str = "completed") -> int:
    duration_ms = _elapsed_ms(started)
    callback = request.progress_callback
    if callback is not None:
        try:
            callback(
                {
                    "type": "run.progress",
                    "phase": "timing",
                    "step": f"profile_{timing_key}",
                    "status": status,
                    "summary": f"Profile {timing_key.replace('_', ' ').title()} {status} in {duration_ms}ms.",
                    "duration_ms": duration_ms,
                    "timing_key": f"profile_{timing_key}_ms",
                }
            )
        except Exception:
            pass
    return duration_ms


def _profile_status_callback(request: AgentRunRequest, timing: dict[str, int]):
    def emit(payload: Any) -> None:
        if isinstance(payload, dict):
            timing_key = payload.get("timing_key")
            duration_ms = payload.get("duration_ms")
            if (
                isinstance(timing_key, str)
                and timing_key.endswith("_ms")
                and timing_key.startswith(("agent_init_", "conversation_", "provider_"))
            ):
                try:
                    parsed = int(duration_ms)
                except (TypeError, ValueError):
                    parsed = -1
                if parsed >= 0:
                    timing[f"profile_{timing_key}"] = parsed
            timing_values = payload.get("timing_values")
            if isinstance(timing_values, dict):
                for key, value in timing_values.items():
                    if not isinstance(key, str) or not key.startswith(("conversation_", "provider_")):
                        continue
                    if not key.endswith(("_ms", "_count")):
                        continue
                    try:
                        parsed = int(value)
                    except (TypeError, ValueError):
                        continue
                    if parsed >= 0:
                        timing[f"profile_{key}"] = parsed
        callback = request.progress_callback
        if callback is not None:
            try:
                callback(payload)
            except Exception:
                pass

    return emit


def _positive_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0 or number != number or number == float("inf"):
        return None
    return number


def _binding_for_profile(profile: str | None) -> PersonaProfileBinding:
    if not profile:
        return PersonaProfileBinding(
            persona_id="profile_runner",
            hermes_profile=None,
            profile_home=None,
            readiness="ready",
            summary="inherits active Hermes profile",
        )
    name = normalize_profile_name(profile)
    if not profile_exists(name):
        return PersonaProfileBinding(
            persona_id="profile_runner",
            hermes_profile=name,
            profile_home=None,
            readiness="missing_profile",
            summary=f"Hermes profile '{name}' does not exist",
        )
    return PersonaProfileBinding(
        persona_id="profile_runner",
        hermes_profile=name,
        profile_home=get_profile_dir(name),
        readiness="ready",
        summary="profile exists",
    )


def _resolve_request_runtime(request: AgentRunRequest) -> dict[str, Any]:
    if not request.provider:
        return {}
    runtime = resolve_runtime_provider(requested=request.provider, target_model=request.model)
    return {
        key: value
        for key, value in runtime.items()
        if key in {"provider", "model", "api_mode", "base_url", "api_key"} and value
    }


def _progress_adapter(
    callback: Callable[[dict[str, Any]], None] | None,
    event_type: str,
    *,
    stop_on_repeated_read_search: bool = False,
    tool_budget_limits: dict[str, Any] | None = None,
    guard: _ToolBudgetGuard | None = None,
):
    if callback is None and guard is None and not stop_on_repeated_read_search:
        return None
    callback = callback or (lambda _payload: None)
    guard = guard or _ToolBudgetGuard.from_limits(
        stop_on_repeated_read_search=stop_on_repeated_read_search,
        tool_budget_limits=tool_budget_limits,
    )

    def emit(*args, **kwargs):
        try:
            payload = _progress_payload_from_callback(event_type, args, kwargs)
            callback(payload)
            tool_name = str(payload.get("tool_name") or "")
            step = str(payload.get("step") or payload.get("type") or "")
            key = (step, tool_name)
            _update_guard_progress(guard, payload)
            _enforce_aggregate_read_search_budget(guard, callback, tool_name=tool_name)
            if event_type == "run.progress" and tool_name and step in {"tool_started", "tool_finished"}:
                guard.repeated_counts[key] = guard.repeated_counts.get(key, 0) + 1
                if tool_name == "skill_view" and guard.repeated_counts[key] >= guard.skill_warning_threshold and key not in guard.warned:
                    guard.warned.add(key)
                    callback(
                        {
                            "type": event_type,
                            "phase": "runaway_warning",
                            "severity": "warning",
                            "step": "skill_loading_fanout",
                            "tool_name": tool_name,
                            "status": "warning",
                            "summary": "Repeated skill_view calls detected; stop loading additional skills and pivot to the single most relevant skill, proof collection, QA handoff, or an exact blocker.",
                            "skill_load_limit": guard.skill_load_limit,
                            "next_expected": "stop_skill_loading_and_produce_proof_or_block",
                        }
                    )
                    return None
                if (
                    guard.stop_on_repeated_read_search
                    and tool_name in READ_SEARCH_TOOLS
                    and not guard.has_patch_progress
                    and guard.repeated_counts[key] >= guard.read_search_limit
                    and key not in guard.warned
                ):
                    guard.warned.add(key)
                    warning = _read_search_warning_payload(
                        event_type,
                        tool_name=tool_name,
                        read_search_count=guard.aggregate_read_search_count,
                        read_search_limit=guard.read_search_limit,
                        summary=f"Repeated {tool_name} calls indicate a read/search loop without proof, verdict, patch, or test progress; stop and produce a bounded verdict, proof handoff, Neko slicing request, or exact blocker.",
                    )
                    callback(warning)
                    guard.trip(f"repeated read/search loop: {tool_name}")
                    raise RunBudgetExceeded(f"repeated read/search loop: {tool_name}")
                if guard.repeated_counts[key] >= 6 and key not in guard.warned:
                    guard.warned.add(key)
                    callback(
                        {
                            "type": event_type,
                            "phase": "runaway_warning",
                            "severity": "warning",
                            "step": "repeated_tool_event",
                            "tool_name": tool_name,
                            "status": "warning",
                            "summary": f"Repeated {step.replace('_', ' ')} for {tool_name}; inspect for a tool loop.",
                        }
                    )
        except RunBudgetExceeded:
            raise
        except Exception:
            return None

    return emit


def _update_guard_progress(guard: _ToolBudgetGuard, payload: dict[str, Any]) -> None:
    tool_name = str(payload.get("tool_name") or "").lower()
    if tool_name in PATCH_TOOLS or str(payload.get("phase") or "") == "dev_work":
        guard.has_patch_progress = True
    if payload.get("type") != "run.tool.finished":
        return
    if tool_name in READ_SEARCH_TOOLS:
        guard.aggregate_read_search_count += 1


def _enforce_aggregate_read_search_budget(guard: _ToolBudgetGuard, callback: Callable[[dict[str, Any]], None], *, tool_name: str) -> None:
    if not guard.stop_on_repeated_read_search:
        return
    if guard.has_patch_progress:
        return
    if guard.aggregate_read_search_count < guard.read_search_limit:
        return
    key = ("aggregate_read_search_budget", "")
    if key in guard.warned:
        return
    guard.warned.add(key)
    warning = _read_search_warning_payload(
        "run.progress",
        tool_name=tool_name,
        read_search_count=guard.aggregate_read_search_count,
        read_search_limit=guard.read_search_limit,
        summary="Aggregate read/search budget exceeded without patch, proof, or bounded handoff progress; interrupting this specialist run so Neko can steer instead of letting it burn tokens.",
    )
    callback(warning)
    reason = f"aggregate read/search budget exceeded: {guard.aggregate_read_search_count}/{guard.read_search_limit}"
    guard.trip(reason)
    raise RunBudgetExceeded(reason)


def _read_search_warning_payload(event_type: str, *, tool_name: str, read_search_count: int, read_search_limit: int, summary: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": event_type,
        "phase": "runaway_warning",
        "severity": "critical",
        "step": "repeated_read_search_loop",
        "status": "failed",
        "summary": summary,
        "read_search_count": read_search_count,
        "read_search_limit": read_search_limit,
        "next_expected": "bounded_verdict_proof_handoff_or_exact_blocker",
    }
    if tool_name:
        payload["tool_name"] = tool_name
    return payload


def _positive_limit(value: Any, *, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(1, parsed)


def _progress_payload_from_callback(event_type: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    callback_event = str(args[0]) if args else event_type
    if event_type == "run.tool.started":
        tool_name = _safe_label(args[1]) if len(args) > 1 else None
        invocation = args[2] if len(args) > 2 else kwargs.get("input") or kwargs.get("tool_input")
        return _tool_started_payload(event_type, tool_name, invocation=invocation)
    if event_type == "run.tool.finished":
        tool_name = _safe_label(args[1]) if len(args) > 1 else None
        invocation = args[2] if len(args) > 2 else None
        result = args[3] if len(args) > 3 else None
        return _tool_finished_payload(event_type, tool_name, duration=None, is_error=_is_error_result(result), result=result, invocation=invocation)

    tool_name = _safe_label(args[1]) if len(args) > 1 else None
    if callback_event == "tool.started":
        invocation = args[3] if len(args) > 3 else kwargs.get("input") or kwargs.get("tool_input")
        return _tool_started_payload(event_type, tool_name, invocation=invocation)
    if callback_event == "tool.completed":
        return _tool_finished_payload(event_type, tool_name, duration=kwargs.get("duration"), is_error=bool(kwargs.get("is_error")), result=kwargs.get("result"), invocation=kwargs.get("input") or kwargs.get("tool_input"))
    if callback_event in {"reasoning.available", "_thinking"}:
        payload = {
            "type": event_type,
            "phase": "thinking_process",
            "step": "reasoning_summary",
            "status": "running",
            "summary": "Agent thinking process updated",
        }
        reasoning = _safe_reasoning_summary(args, kwargs)
        if reasoning:
            payload["reasoning_summary"] = reasoning
        return payload
    return {"type": event_type, "phase": "tool", "step": "progress", "status": "running", "summary": "Run progress update"}


def _tool_started_payload(event_type: str, tool_name: str | None, *, invocation: Any = None) -> dict[str, Any]:
    payload = {"type": event_type, "phase": "tool", "step": "tool_started", "status": "started"}
    if tool_name:
        payload["tool_name"] = tool_name
        payload["summary"] = f"Started tool {tool_name}"
    else:
        payload["summary"] = "Started tool"
    command_label = _safe_command_label(invocation)
    if command_label:
        payload["command_label"] = command_label
        if tool_name:
            payload["summary"] = f"Started tool {tool_name}: {command_label}"
    return payload


def _tool_finished_payload(event_type: str, tool_name: str | None, *, duration: Any, is_error: bool, result: Any, invocation: Any = None) -> dict[str, Any]:
    status = "failed" if is_error else "passed"
    payload = {"type": event_type, "phase": "tool", "step": "tool_finished", "status": status}
    if tool_name:
        payload["tool_name"] = tool_name
    duration_ms = _duration_ms(duration)
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    exit_code = _safe_exit_code((result or {}).get("exit_code") if isinstance(result, dict) else None)
    if exit_code is not None:
        payload["exit_code"] = exit_code
    dev_work_payload = _dev_work_payload(tool_name, status=status, result=result, invocation=invocation)
    if dev_work_payload:
        payload.update(dev_work_payload)
        return payload
    subject = f"tool {tool_name}" if tool_name else "tool"
    if duration_ms is not None:
        payload["summary"] = f"Finished {subject}: {status} in {duration_ms}ms"
    else:
        payload["summary"] = f"Finished {subject}: {status}"
    detail = _safe_tool_result_detail(tool_name, result)
    if detail:
        payload["detail"] = detail
    command_label = _safe_command_label(invocation)
    if command_label:
        payload["command_label"] = command_label
    return payload


def _dev_work_payload(tool_name: str | None, *, status: str, result: Any, invocation: Any) -> dict[str, Any] | None:
    normalized_tool = (tool_name or "").lower()
    if normalized_tool in {"patch", "apply_patch"}:
        labels = _safe_file_labels(_candidate_file_values(result, None))
        payload: dict[str, Any] = {"phase": "dev_work", "step": "patch"}
        if labels:
            joined = ", ".join(labels[:4]) + ("…" if len(labels) > 4 else "")
            payload["changed_files"] = labels
            payload["files_touched"] = len(labels)
            payload["summary"] = f"Patched {len(labels)} files: {joined}"
            payload["detail"] = f"Changed files: {joined}"
            payload["patch_summary"] = f"Patched {len(labels)} files"
        elif status == "passed":
            payload["summary"] = "Patch completed; changed-file list unavailable"
            payload["patch_summary"] = "Patch completed"
        else:
            payload["summary"] = "Patch failed"
            payload["patch_summary"] = "Patch failed"
        return payload
    if normalized_tool in {"write_file", "edit_file", "file.write", "file.edit"}:
        labels = _safe_file_labels(_candidate_file_values(result, invocation))
        payload = {"phase": "dev_work", "step": "write_file" if normalized_tool == "write_file" else "code_edit"}
        if labels:
            joined = ", ".join(labels[:4]) + ("…" if len(labels) > 4 else "")
            payload["changed_files"] = labels
            payload["files_touched"] = len(labels)
            if len(labels) == 1:
                payload["summary"] = f"Wrote code file: {labels[0]}"
                payload["file_summary"] = "Wrote code file"
            else:
                payload["summary"] = f"Wrote code files: {len(labels)} files"
                payload["file_summary"] = "Wrote code files"
            payload["detail"] = f"Changed files: {joined}"
        elif status == "passed":
            payload["summary"] = "Wrote code file; changed-file list unavailable"
            payload["file_summary"] = "Wrote code file"
        else:
            payload["summary"] = "Code file write failed"
            payload["file_summary"] = "Code file write failed"
        return payload
    return None


def _candidate_file_values(result: Any, invocation: Any) -> list[Any]:
    values: list[Any] = []
    for source in (result, invocation):
        if not isinstance(source, dict):
            continue
        for key in ("files_modified", "modified_files", "changed_files", "files", "path", "file_path", "target_path"):
            value = source.get(key)
            if isinstance(value, list):
                values.extend(value)
            elif value:
                values.append(value)
    return values


def _safe_command_label(invocation: Any) -> str | None:
    if not isinstance(invocation, dict):
        return None
    command = invocation.get("command") or invocation.get("cmd")
    if not isinstance(command, str):
        return None
    text = " ".join(command.strip().split())
    if not text:
        return None
    lowered = text.lower()
    if any(marker in lowered for marker in ("secret", "token", "password", "api_key", "apikey", "authorization", "bearer", "credential", "cookie", "private_key", "sk-")):
        return None
    text = text.replace("\\", "/")
    if re.search(r"(^|\s)([A-Za-z]:/|//|/home/|/users/|/x/|/c/|~)", text.lower()):
        return None
    return f"{text[:237]}..." if len(text) > 240 else text


def _safe_tool_result_detail(tool_name: str | None, result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    normalized_tool = (tool_name or "").lower()
    if normalized_tool == "patch":
        files = result.get("files_modified") or result.get("modified_files") or result.get("files")
        labels = _safe_file_labels(files)
        if labels:
            return f"Patch modified {len(labels)} files: {', '.join(labels[:4])}{'…' if len(labels) > 4 else ''}"
        if result.get("success") is True:
            return "Patch completed successfully; no file list returned."
    return None


def _safe_file_labels(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    labels: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        label = Path(text.replace("\\", "/")).name
        if not label or _looks_sensitive_or_pathish(label):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", label):
            continue
        labels.append(label)
    return labels


def _is_error_result(result: Any) -> bool:
    if isinstance(result, dict):
        if result.get("error") or result.get("success") is False:
            return True
        exit_code = _safe_exit_code(result.get("exit_code"))
        if exit_code is not None:
            return exit_code != 0
    if isinstance(result, str):
        lowered = result.strip().lower()
        return lowered.startswith(("error", "traceback", "exception")) or '"success": false' in lowered
    return False


def _duration_ms(value: Any) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number != number or number == float("inf"):
        return None
    return int(round(number * 1000))


def _safe_exit_code(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_label(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or _looks_sensitive_or_pathish(text):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", text):
        return None
    return text


def _safe_reasoning_summary(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
    candidates = [
        kwargs.get("reasoning_summary"),
        kwargs.get("summary"),
        kwargs.get("reasoning"),
    ]
    if len(args) > 1:
        candidates.append(args[1])
    for value in candidates:
        if not isinstance(value, str):
            continue
        text = " ".join(value.strip().split())
        if not text or _looks_sensitive_or_pathish(text):
            continue
        if len(text) > 500:
            text = f"{text[:497]}…"
        return text
    return None


def _looks_sensitive_or_pathish(value: str) -> bool:
    lowered = value.lower()
    if any(marker in lowered for marker in ("secret", "token", "password", "api_key", "apikey", "authorization", "bearer", "credential", "cookie", "private_key", "sk-")):
        return True
    if ":/" in value or "\\" in value or value.startswith(("/", "~")):
        return True
    if re.search(r"(^|\s)([A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+", value):
        return True
    return False


def _attach_model_input_observability(raw_result: Any, *, agent, request: AgentRunRequest) -> None:
    if not isinstance(raw_result, dict):
        return
    raw_result.setdefault("model_input_observability", _model_input_observability(agent=agent, request=request))


def _model_input_observability(*, agent, request: AgentRunRequest) -> dict[str, Any]:
    system_prompt = getattr(agent, "_cached_system_prompt", None)
    if not system_prompt and hasattr(agent, "_build_system_prompt"):
        try:
            system_prompt = agent._build_system_prompt(request.system_message)
        except Exception:
            system_prompt = request.system_message or ""
    messages: list[dict[str, Any]] = []
    if system_prompt:
        messages.append(_message_preview("system", str(system_prompt), source="hermes_system_prompt"))
    messages.append(_message_preview("user", request.user_message, source="mission_chat_user_message"))
    return {
        "schema_version": 1,
        "kind": "redaction_safe_final_model_input",
        "platform": request.platform,
        "profile": request.profile,
        "session_id": request.session_id,
        "task_id": request.task_id,
        "skip_context_files": bool(request.skip_context_files),
        "skip_memory": bool(request.skip_memory),
        "system_message_supplied": request.system_message is not None,
        "message_count": len(messages),
        "messages": messages,
    }


def _message_preview(role: str, content: str, *, source: str) -> dict[str, Any]:
    raw = str(content or "")
    safe = _redact_prompt_text(raw)
    encoded = safe.encode("utf-8", errors="replace")
    limit = 60000
    preview = safe
    truncated = False
    if len(encoded) > limit:
        preview = encoded[:limit].decode("utf-8", errors="ignore")
        truncated = True
    return {
        "role": role,
        "source": source,
        "content": preview,
        "truncated": truncated,
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest().upper(),
    }


_PROMPT_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|authorization|bearer|password|secret)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)\b(sk-[A-Za-z0-9_-]{12,})\b"),
    re.compile(r"(?i)\b(xox[baprs]-[A-Za-z0-9-]{12,})\b"),
]


def _redact_prompt_text(value: str) -> str:
    text = value
    for pattern in _PROMPT_SECRET_PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1)}=<redacted>" if match.lastindex and match.lastindex >= 2 else "<redacted>", text)
    return text


def _normalize_result(result: Any, *, agent) -> AgentRunResult:
    if isinstance(result, dict):
        messages = result.get("messages") if isinstance(result.get("messages"), list) else []
        return AgentRunResult(
            final_response=str(result.get("final_response", "")),
            session_id=result.get("session_id") or getattr(agent, "session_id", None),
            provider=result.get("provider") or getattr(agent, "provider", None),
            model=result.get("model") or getattr(agent, "model", None),
            base_url=result.get("base_url") or getattr(agent, "base_url", None),
            messages=[msg for msg in messages if isinstance(msg, dict)],
            api_calls=result.get("api_calls"),
            input_tokens=result.get("input_tokens") or result.get("prompt_tokens"),
            output_tokens=result.get("output_tokens") or result.get("completion_tokens"),
            total_tokens=result.get("total_tokens"),
            raw=dict(result),
        )
    return AgentRunResult(
        final_response=str(result),
        session_id=getattr(agent, "session_id", None),
        provider=getattr(agent, "provider", None),
        model=getattr(agent, "model", None),
        base_url=getattr(agent, "base_url", None),
        messages=[],
        raw={"result_type": type(result).__name__},
    )


def _default_agent_factory(**kwargs):
    from run_agent import AIAgent

    return AIAgent(**kwargs)
