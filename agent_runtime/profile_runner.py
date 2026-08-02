from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
from threading import Event, RLock, Timer
import time
from typing import Any, Callable
import re

from hermes_cli.profiles import get_profile_dir, normalize_profile_name, profile_exists
from hermes_cli.runtime_provider import resolve_runtime_provider

from . import turn_budget
from .personas import REGISTRY_HYGIENE_BLOCKED_TOOLS
from .profile_context import PersonaProfileBinding, persona_profile_context
from .redaction import TEXT_SECRET_VALUE_ASSIGNMENT_RE
from .run_budget import (
    UNIT_CALLS,
    UNIT_SECONDS,
    UNIT_TOKENS,
    RunBudgetEnforcement,
    RunBudgetKind,
    RunBudgetLedger,
    RunBudgetTripReason,
)
from .turn_budget import TurnWallBudget


def _blocked_tool_names_with_registry_hygiene(requested: list[str] | None) -> list[str]:
    """Union the fork registry-hygiene block into a request's ``blocked_tool_names``.

    This is the single fork-owned chokepoint that makes the deregistered upstream
    toolsets (``kanban`` + ``feishu_doc`` / ``feishu_drive``) unresolvable on EVERY
    agent-runtime lane — the persona chat/run lanes already carry them via
    ``PERSONA_BLOCKED_TOOLS``, but the worker / root-node lanes construct their
    request with ``blocked_tool_names=[]`` and would otherwise resolve them. Applied
    here (agent construction) so no call site can opt out. Order-preserving; the
    downstream tool-def cache keys on the set, so duplicates/order are harmless."""

    names = list(requested or [])
    seen = set(names)
    for name in sorted(REGISTRY_HYGIENE_BLOCKED_TOOLS):
        if name not in seen:
            names.append(name)
            seen.add(name)
    return names


def _blocked_tool_names_for_run(request: "AgentRunRequest") -> list[str]:
    """Registry hygiene plus this run's admission-scoped MCP tool block."""

    names = _blocked_tool_names_with_registry_hygiene(request.blocked_tool_names)
    admission = request.mcp_admission
    if admission is None:
        return names
    seen = set(names)
    for name in admission.blocked_tool_names:
        if name not in seen:
            names.append(name)
            seen.add(name)
    return names


def _enabled_toolsets_for_run(
    request: "AgentRunRequest", admitted_servers: tuple[str, ...] = ()
) -> list[str] | None:
    """Scope this run's toolsets to the MCP servers it was ADMITTED.

    The second fork-owned chokepoint at agent construction, and the one that
    makes the cross-persona isolation property hold on every lane rather than
    only on the chat lane: MCP registration is process-global, so in a warm
    multi-persona harness process any run whose toolsets were resolved from the
    live registry (``unbounded`` resolves ``all_registered_toolsets()``) would
    otherwise inherit another persona's admitted ``mcp-*`` toolsets. Applied
    here, no call site can opt out.

    With no admission on the request — every lane today except an admitted
    mission-chat turn — this strips any MCP toolset the run did not earn, which
    is a no-op while the harness lane registers nothing at all.
    ``enabled_toolsets=None`` (the "everything" sentinel) is passed through
    untouched: narrowing it would change what a default run resolves.
    """

    from .mcp_admission import scope_toolsets_to_admission

    if request.enabled_toolsets is None:
        return None
    return scope_toolsets_to_admission(
        request.enabled_toolsets, admitted_servers=admitted_servers
    )


class ProfileRunnerError(RuntimeError):
    """Raised before agent construction when a profile-bound run cannot start."""


class RunBudgetExceeded(ProfileRunnerError):
    """Raised when a live persona run exceeds its configured budget.

    ``wall_budget`` carries the typed budget projection when the WALL budget
    (not the api-call / token / read-search budgets) is what tripped, so the
    caller can settle the turn as ``budget_exhausted`` — a known, terminal
    outcome — instead of the ambiguous ``outcome_unknown``.

    ``run_budget`` carries the run's WHOLE budget accounting block (see
    ``run_budget.RunBudgetLedger.accounting``) — the same block the completed
    path writes into ``profile_timing``. Without it a tripped run is the one
    case where the accounting is unreadable after the fact, because the result
    that would have carried it is never returned.
    """

    def __init__(
        self,
        message: str,
        *,
        session_id: str | None = None,
        wall_budget: dict[str, Any] | None = None,
        run_budget: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.session_id = session_id
        self.wall_budget = wall_budget
        self.run_budget = run_budget


# Stand-in "budget" for runs that carry no wall budget at all. The checkpoint is
# still constructed (one unconditional shape for the tool-start gate) but with a
# deadline it can never reach, so it never engages and never arms a timer.
_NO_WALL_BUDGET_SECONDS = 3650.0 * 24 * 3600

READ_SEARCH_TOOLS = frozenset({"read_file", "search_files", "session_search", "browser_snapshot"})
PATCH_TOOLS = frozenset({"patch", "apply_patch", "write_file", "edit_file", "file.write", "file.edit"})


@dataclass(slots=True)
class AgentRunRequest:
    profile: str | None
    provider: str | None = None
    model: str | None = None
    api_mode: str | None = None
    # Optional per-run reasoning-effort override (one of
    # hermes_constants.VALID_REASONING_EFFORTS or "none"). None = inherit the
    # global config default at the transport. Threaded into the agent's
    # reasoning_config so a per-agent-instance choice actually takes effect.
    reasoning_effort: str | None = None
    enabled_toolsets: list[str] | None = None
    disabled_toolsets: list[str] | None = None
    blocked_tool_names: list[str] | None = None
    skills: list[str] | None = None
    session_id: str | None = None
    # Codex cache-scope routing hint (header-only), DISTINCT from ``session_id``.
    # The persona-chat lane passes ``session_id=None`` so the runtime does not
    # re-load a transcript it already baked into the message, but the
    # ChatGPT-Codex backend routes its prompt cache on the ``session_id`` /
    # ``x-client-request-id`` HTTP headers. ``cache_scope_id`` supplies a STABLE
    # per-conversation value for those headers without ever touching transcript
    # or session loading (which stay keyed on ``session_id``). None ⇒ the codex
    # transport falls back to ``session_id`` (worker/mission-run lanes carry a
    # real one, so their behavior is unchanged). See T10c / codex.py header seam.
    cache_scope_id: str | None = None
    # Stable persona-chat root used by terminal/file tool ephemeral state.
    # It is intentionally separate from task_id and native compression tip.
    tool_execution_scope_id: str | None = None
    conversation_history: list[dict[str, Any]] | None = None
    reuse_current_user_message: bool = False
    # Typed presentation marker stamped on the NATIVE user row this turn
    # persists (the row's ``finish_reason`` column; see
    # ``hermes_state._rows_to_conversation`` and ``agent_runtime.relay_policy``).
    # Its only producer today is relay sender attribution: an
    # ``agent_chat_send`` hop resolves WHO is speaking at the CLI chokepoint,
    # and the target's transcript must show the SENDING agent rather than the
    # operator. ``None`` on operator/CLI sends leaves those rows byte-identical.
    persona_chat_user_finish_reason: str | None = None
    client_message_id: str | None = None
    turn_id: str | None = None
    root_chat_session_id: str | None = None
    persona_chat_runtime_registry: Any | None = None
    persona_chat_runtime_signature: str | None = None
    persona_chat_native_revision: str | None = None
    # Explicit one-turn proof/debug seam. Normal persona-chat turns leave these
    # unset and inherit the model/profile compressor configuration.
    compression_threshold_tokens_override: int | None = None
    compression_protect_first_n_override: int | None = None
    compression_protect_last_n_override: int | None = None
    platform: str = "agent_runtime"
    skill_surface: str | None = None
    skill_root_node_mode: bool = False
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
    agent_ready_callback: Callable[[Any], Callable[[], None] | None] | None = None
    # Interactive clarify bridge: ``callback(question, choices) -> str``. On the
    # operator/relay chat lane this is a NON-blocking capture (records the
    # question and ends the turn) rather than the CLI's blocking human prompt;
    # unset on autonomous runs so ``clarify`` stays inert there.
    clarify_callback: Callable[[str, list[str] | None], str] | None = None
    runtime_root: Path | None = None
    workdir: Path | None = None
    stop_on_repeated_read_search: bool = False
    tool_budget_limits: dict[str, Any] | None = None
    # Resolved, side-effect-free MCP admission for this run (agent_runtime.
    # mcp_admission.resolve_mcp_admission). Unset on every lane that declares no
    # MCP servers or runs with the admission flag off — which is all of them
    # until an operator enables it. The RUNNER performs the registration, inside
    # the persona profile context and before agent construction, so the decision
    # (policy) and the side effect (spawn) stay separable and separately tested.
    mcp_admission: Any | None = None
    # Lane/role identity for the terminal safety envelope
    # (agent_runtime.terminal_envelope.TerminalEnvelopeScope). Set ONLY by
    # lanes the envelope grant policy governs — mission-chat today. Left None
    # everywhere else, which is how "no other lane changes" is enforced
    # structurally: with no scope bound, ``envelope_decision`` returns None and
    # the terminal tool keeps its legacy pattern-table behavior byte-for-byte.
    terminal_envelope_scope: Any | None = None


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
    # Canonical cache/reasoning buckets. ``input_tokens`` is already the uncached,
    # full-price remainder (canonical usage subtracts these; see
    # agent/usage_pricing.CanonicalUsage). Carrying them here keeps the accounting
    # object complete end-to-end so downstream writers never have to reconstruct
    # a lossy subset — the persona-chat bound-session record and the Launcher
    # cache indicator both read from these.
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    # Per-call canonical usage rows for this turn (call_index/prompt_tokens/…),
    # in call order. The token fields above are turn-cumulative and answer "what
    # did this turn burn"; row 1 answers "how big was the assembled context",
    # which is the only honest source for Mission Control's context budget.
    # Written at the accrual sites (agent.usage_pricing.record_api_call_usage).
    usage_ledger: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: int | None = None
    # Mostly ``_ms`` / ``_count`` integers, plus the one structured entry
    # ``run_budget`` (the accounting block from ``run_budget.RunBudgetLedger``).
    # Live downstream readers copy the dict wholesale; S34 retired the dead
    # run-record accumulator that used to filter it to ``_ms``/``_count`` keys.
    profile_timing: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


class WallBudgetCheckpoint:
    """Graceful end-of-turn checkpoint for a run's wall-clock budget.

    Retires the mid-API-call kill as the FIRST thing that happens when a turn
    runs out of wall clock (live incident 2026-07-26). When the reserved window
    opens — see ``turn_budget.checkpoint_reserve_seconds`` — this:

    1. steers a system-side nudge into the agent's next tool result, so the
       model is told, in-band, to produce its final checkpoint reply;
    2. drains the agent's iteration budget, so the tool-calling loop launches NO
       further tool executions and exits at its next iteration boundary. The
       upstream finalizer then takes exactly ONE toolless provider call — the
       final reply — using the mechanism it already has for iteration
       exhaustion. No upstream edit, no private attribute, no mid-flight abort
       of an already-running tool (aborting that is the very kill we replace).

    The old hard wall stays armed at the real deadline as the last resort: if
    even the final call cannot fit, the run still dies — but it dies with a
    typed ``wall_budget`` on the exception, so the turn settles as
    ``budget_exhausted`` instead of ``outcome_unknown``.

    Thread-safe and idempotent: the timer thread and the tool-start gate race
    freely; exactly one of them engages.
    """

    def __init__(
        self,
        budget: TurnWallBudget,
        *,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] | None = None,
        ledger: RunBudgetLedger | None = None,
    ):
        self.budget = budget
        self._progress_callback = progress_callback
        self._clock = clock or time.time
        # The run's budget ledger, for accounting only — the checkpoint's own
        # decision path is untouched by it. Defaulted so a directly-constructed
        # checkpoint (tests, and any future caller) still records somewhere.
        self.ledger = ledger or RunBudgetLedger()
        self._lock = RLock()
        self._agent: Any | None = None
        self._engaged = False
        self._trigger: str | None = None
        self._remaining_at_engage: float | None = None
        self._iterations_drained = 0

    # -- wiring ---------------------------------------------------------
    def bind(self, agent: Any) -> None:
        """Bind the agent AFTER any resident-registry swap, so the nudge and the
        iteration drain land on the object that actually runs the turn."""
        with self._lock:
            self._agent = agent

    @property
    def engaged(self) -> bool:
        with self._lock:
            return self._engaged

    # -- decision -------------------------------------------------------
    def gate(self) -> bool:
        """Pre-work check: engage when the reserved window has opened.

        Called before each tool execution (via the tool-start progress seam) so
        the stop lands deterministically at a tool boundary rather than waiting
        on the timer thread. Returns True when new work may still start.
        """

        if self.engaged:
            return False
        if not self.budget.supports_checkpoint:
            return True
        if self.budget.may_start_new_work(now=self._clock()):
            return True
        self.engage(trigger=turn_budget.CHECKPOINT_TRIGGER_TOOL_GATE)
        return False

    def engage(self, *, trigger: str) -> bool:
        """Open the checkpoint exactly once. Returns True if this call did it."""

        with self._lock:
            if self._engaged or self._agent is None:
                return False
            self._engaged = True
            self._trigger = trigger
            self._remaining_at_engage = self.budget.remaining_seconds(now=self._clock())
            agent = self._agent
        nudge = turn_budget.checkpoint_nudge_text(self.budget, now=self._clock())
        steer = getattr(agent, "steer", None)
        if callable(steer):
            try:
                steer(nudge)
            except Exception:
                pass
        drained = turn_budget.drain_iteration_budget(agent)
        with self._lock:
            self._iterations_drained = drained
        # Accounting only — the wall bound LANDS the turn, and recording that
        # here is what makes "the reply you are reading is a checkpoint reply"
        # readable from the run record instead of only from the progress lane.
        self.ledger.trip(
            RunBudgetKind.WALL,
            RunBudgetTripReason.WALL_CHECKPOINT_ENGAGED,
            consumed=self.consumed_seconds(),
            detail=f"trigger={trigger}",
        )
        self._emit_progress()
        return True

    def consumed_seconds(self) -> float:
        """Wall spent so far, from the same budget the enforcement reads."""

        return max(0.0, self.budget.total_seconds - self.budget.remaining_seconds(now=self._clock()))

    def summary(self) -> dict[str, Any]:
        with self._lock:
            block = self.budget.hud_block(now=self._clock())
            block.update(
                {
                    "engaged": self._engaged,
                    "trigger": self._trigger,
                    "iterations_reclaimed": self._iterations_drained,
                }
            )
            if self._remaining_at_engage is not None:
                block["remaining_at_checkpoint_seconds"] = round(
                    max(0.0, self._remaining_at_engage), 1
                )
            return block

    def _emit_progress(self) -> None:
        callback = self._progress_callback
        if callback is None:
            return
        try:
            callback(
                {
                    "type": "run.progress",
                    "phase": "wall_budget_checkpoint",
                    "severity": "warning",
                    "step": "wall_budget_checkpoint_opened",
                    "status": "warning",
                    "summary": (
                        "Wall budget nearly exhausted — no further tool calls will run; "
                        "asking the agent for a final checkpoint reply "
                        f"({self.budget.summary(now=self._clock())})."
                    ),
                    "wall_budget": self.summary(),
                }
            )
        except Exception:
            return


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
    # Wall-clock checkpoint consulted before each tool execution. Distinct from
    # the tool-count budgets above: those TRIP the run, this one lands it.
    wall_checkpoint: WallBudgetCheckpoint | None = None
    # The run's budget ledger. Accounting only: every enforcement decision below
    # is made exactly where it was before, then declared here.
    ledger: RunBudgetLedger = field(default_factory=RunBudgetLedger)

    @classmethod
    def from_limits(
        cls,
        *,
        stop_on_repeated_read_search: bool,
        tool_budget_limits: dict[str, Any] | None,
        ledger: RunBudgetLedger | None = None,
    ):
        limits = tool_budget_limits or {}
        guard = cls(
            stop_on_repeated_read_search=stop_on_repeated_read_search,
            read_search_limit=_positive_limit(limits.get("read_search_limit"), fallback=6),
            skill_load_limit=_positive_limit(limits.get("skill_load_limit"), fallback=2),
            has_patch_progress=bool(limits.get("has_patch_progress")),
            ledger=ledger or RunBudgetLedger(),
        )
        if stop_on_repeated_read_search:
            # Declared only when the bound is actually enforced: a run that does
            # not stop on read/search loops is not bounded by one, and a row
            # claiming otherwise would be a bound that does not exist.
            guard.ledger.declare(
                RunBudgetKind.READ_SEARCH,
                enforcement=RunBudgetEnforcement.TRIPS_RUN,
                unit=UNIT_CALLS,
                limit=guard.read_search_limit,
                consumed_provider=lambda: guard.aggregate_read_search_count,
            )
        return guard

    @property
    def skill_warning_threshold(self) -> int:
        return max(3, self.skill_load_limit + 1)

    def set_interrupt_callback(self, callback: Callable[[str], None]) -> None:
        self.interrupt_callback = callback

    def trip(
        self,
        reason: str,
        *,
        kind: RunBudgetKind = RunBudgetKind.READ_SEARCH,
        trip_reason: RunBudgetTripReason = RunBudgetTripReason.AGGREGATE_READ_SEARCH_EXCEEDED,
    ) -> None:
        """Trip this run. ``reason`` stays the exception's message, verbatim.

        ``kind`` / ``trip_reason`` are the typed half of the SAME fact, recorded
        for the accounting block so no downstream reader has to parse the
        message string to learn which bound fired.
        """

        if not self.tripped_reason:
            self.tripped_reason = reason
            self.ledger.trip(kind, trip_reason, detail=reason)
        if self.interrupt_callback is None:
            return
        try:
            self.interrupt_callback(reason)
        except Exception:
            return


def _prepare_resident_persona_chat_agent(agent: Any, candidate: Any) -> None:
    """Refresh turn-scoped state without erasing native compressor memory."""

    for name in (
        "status_callback", "tool_progress_callback", "tool_start_callback",
        "tool_complete_callback", "clarify_callback", "cache_scope_id", "max_iterations",
    ):
        if hasattr(candidate, name):
            setattr(agent, name, getattr(candidate, name))
    for name in (
        "session_prompt_tokens", "session_completion_tokens", "session_total_tokens",
        "session_api_calls", "session_input_tokens", "session_output_tokens",
        "session_cache_read_tokens", "session_cache_write_tokens",
        "session_reasoning_tokens", "session_estimated_cost_usd", "_api_call_count",
    ):
        if hasattr(agent, name):
            setattr(agent, name, 0.0 if name.endswith("cost_usd") else 0)
    agent.session_usage_ledger = []
    for name, value in (
        ("_stream_callback", None), ("_interrupt_requested", False),
        ("_interrupt_reason", None), ("_current_api_request_id", ""),
        ("_current_turn_id", None), ("_current_task_id", None),
    ):
        if hasattr(agent, name):
            setattr(agent, name, value)


def _finish_resident_persona_chat_agent(agent: Any) -> None:
    """Detach every turn-local handle while preserving conversation state."""

    for name in (
        "status_callback",
        "tool_progress_callback",
        "tool_start_callback",
        "tool_complete_callback",
        "clarify_callback",
        "_stream_callback",
    ):
        if hasattr(agent, name):
            setattr(agent, name, None)
    for name, value in (
        ("_interrupt_requested", False),
        ("_interrupt_reason", None),
        ("_current_api_request_id", ""),
        ("_current_turn_id", None),
        ("_current_task_id", None),
        ("_persona_chat_client_message_id", None),
        ("_persona_chat_turn_id", None),
        ("_pending_cli_user_message", None),
    ):
        if hasattr(agent, name):
            setattr(agent, name, value)


def _sanitized_user_message_text(text: str) -> str:
    """The prologue's own view of this turn's clean user text.

    ``build_turn_context`` sanitizes surrogates BEFORE comparing the staged
    message below, so an unsanitized copy of a surrogate-bearing message would
    silently fail the match and drop the marker. Falls back to the raw text if
    the upstream helper ever moves — a no-op for every non-surrogate message.
    """

    try:
        from agent.message_sanitization import _sanitize_surrogates
    except Exception:  # pragma: no cover - upstream helper relocated
        return text
    try:
        return _sanitize_surrogates(text)
    except Exception:  # pragma: no cover - defensive
        return text


def stage_persona_chat_user_row_marker(
    agent: Any, request: "AgentRunRequest"
) -> dict[str, Any] | None:
    """Stage this turn's user-message dict so its NATIVE row carries a marker.

    Since native session continuity landed, the mission-chat lane no longer
    appends the incoming operator row itself — the runtime persists it as part
    of the turn. ``build_turn_context`` adopts an already-staged
    ``_pending_cli_user_message`` whose clean text matches this turn's message,
    appends THAT dict as the turn's user message, and preserves every extra key
    on it; the session flush then writes ``finish_reason=msg.get(
    "finish_reason")`` onto the persisted row. Staging here is therefore the one
    seam where a fork-owned lane can TYPE the row the runtime writes — no second
    write, no duplicate row, and the model's prompt is untouched.

    Always writes the attribute: a stale dict left on a RESIDENT chat agent
    would otherwise be adopted by a later turn. No marker (every operator/CLI
    send) clears it, and the turn behaves exactly as it did before attribution
    existed. Returns the staged dict, or ``None`` when nothing was staged.
    """

    marker = getattr(request, "persona_chat_user_finish_reason", None)
    if (
        not marker
        # The retry lane reuses a row that is already durable (and already
        # carries the marker from the attempt that wrote it); re-staging would
        # be a no-op the prologue never reads.
        or request.reuse_current_user_message
        or not isinstance(request.user_message, str)
    ):
        agent._pending_cli_user_message = None
        return None
    staged = {
        "role": "user",
        "content": _sanitized_user_message_text(request.user_message),
        "finish_reason": str(marker),
    }
    agent._pending_cli_user_message = staged
    return staged


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
        resident = bool(
            request.persona_chat_runtime_registry is not None
            and request.root_chat_session_id
        )
        # ONE ledger per run, minted here so the post-run budgets enforced below
        # land in the same accounting block as the in-run ones. Every mechanism
        # keeps its own enforcement; only the bookkeeping is shared.
        ledger = RunBudgetLedger()
        try:
            raw_result, agent, profile_timing = self._execute_agent_run(
                binding, request, ledger=ledger
            )
        except Exception:
            if resident:
                request.persona_chat_runtime_registry.evict(
                    request.root_chat_session_id
                )
            raise
        normalize_started = time.perf_counter()
        try:
            result = _normalize_result(raw_result, agent=agent)
            profile_timing["result_normalize_ms"] = _emit_request_timing(
                request, "result_normalize", normalize_started
            )
        finally:
            if resident:
                _finish_resident_persona_chat_agent(agent)
        result.latency_ms = _elapsed_ms(started)
        profile_timing["run_budget"] = ledger.accounting()
        result.profile_timing = profile_timing
        if isinstance(result.raw, dict):
            result.raw["profile_timing"] = dict(profile_timing)
        budget_started = time.perf_counter()
        _emit_budget_pressure_warning(result, request)
        _enforce_result_budgets(result, request, ledger=ledger)
        profile_timing["budget_checks_ms"] = _emit_request_timing(request, "budget_checks", budget_started)
        # Re-rendered AFTER the post-run budgets so the block an operator reads
        # covers every bound this turn had, not only the in-run ones.
        profile_timing["run_budget"] = ledger.accounting()
        result.profile_timing = profile_timing
        if isinstance(result.raw, dict):
            result.raw["profile_timing"] = dict(profile_timing)
        if result.raw.get("failed") and result.raw.get("error"):
            raise ProfileRunnerError(str(result.raw.get("error")))
        return result

    def _admit_mcp_servers(
        self,
        request: AgentRunRequest,
        timing: dict[str, Any],
        *,
        ledger: RunBudgetLedger | None = None,
    ):
        """Register this run's admitted MCP servers; return the typed outcome.

        Never raises and never blocks past the admission budget — the outcome is
        typed either way, and an empty ``admitted`` simply means "this run gets
        no MCP tools", which is the state every run is in today.

        ``ledger`` is accounting only, and only for the ONE fact this function
        can observe that the caller cannot: the per-run MCP call budget trips
        mid-turn, on whichever thread dispatched the tool, so its trip has to be
        recorded from the exhaustion callback installed here. The budget's
        limit/consumption rows are declared by the caller.
        """

        admission = request.mcp_admission
        if admission is None or getattr(admission, "is_empty", True):
            return None
        from .mcp_admission import admit_mcp_servers

        started = time.perf_counter()
        try:
            outcome = admit_mcp_servers(
                admission,
                # The per-run MCP call budget trips DURING the turn, long after
                # this function has returned, so the operator-facing half of that
                # event rides this callback rather than the outcome. The
                # agent-facing half is the refused tool's own typed result — the
                # one surface a looping model cannot miss.
                on_budget_exhausted=lambda denial, snapshot: _on_mcp_budget_exhausted(
                    request, denial, snapshot, ledger=ledger
                ),
            )
        except Exception:  # pragma: no cover - admit_mcp_servers already swallows
            timing["mcp_admission_ms"] = _emit_request_timing(
                request, "mcp_admission", started, status="failed"
            )
            return None
        timing["mcp_admission_ms"] = _emit_request_timing(request, "mcp_admission", started)
        timing["mcp_admitted_servers"] = len(outcome.admitted)
        timing["mcp_call_budget"] = int(getattr(admission, "max_tool_calls_per_run", 0) or 0)
        if request.progress_callback is not None and (outcome.admitted or outcome.denied):
            try:
                request.progress_callback(
                    {
                        "type": "run.progress",
                        "phase": "mcp_admission",
                        "severity": "info" if outcome.admitted else "warning",
                        "step": "mcp_admission_resolved",
                        "status": "ok" if outcome.admitted else "warning",
                        "summary": (
                            "MCP admission: "
                            + (", ".join(outcome.admitted) if outcome.admitted else "nothing admitted")
                        ),
                        "mcp_admission": {
                            "admitted": list(outcome.admitted),
                            "denied": outcome.denial_rows(),
                            "duration_ms": outcome.duration_ms,
                        },
                    }
                )
            except Exception:
                pass
        return outcome

    def _teardown_mcp_admission(
        self,
        request: AgentRunRequest,
        servers: tuple[str, ...],
        timing: dict[str, Any],
        budget: Any | None = None,
    ) -> None:
        """Remove this run's MCP registry scope. Advisory — never fails the turn.

        Runs on the way out of ``_execute_agent_run`` for BOTH the completed and
        the raised path, while the run still holds ``_WORKDIR_LOCK`` and is still
        inside ``persona_profile_context``, so no other persona's run can observe
        the scope between the last tool call and its removal. The transport stays
        warm; only the registry entries and the toolset alias go.

        ``budget`` is this run's call meter, read here for its final accounting:
        the meter dies with the scope, so end-of-run is the last moment "how many
        admitted MCP calls did this turn actually make" is answerable.
        """

        if not servers:
            return
        if budget is not None:
            try:
                snapshot = budget.snapshot()
                timing["mcp_calls_spent"] = int(snapshot.get("spent") or 0)
                timing["mcp_calls_refused"] = int(snapshot.get("refused") or 0)
            except Exception:  # pragma: no cover - accounting must never fail a turn
                pass
        from .mcp_admission import teardown_mcp_admission

        started = time.perf_counter()
        try:
            outcome = teardown_mcp_admission(servers)
        except Exception:  # pragma: no cover - teardown_mcp_admission already swallows
            timing["mcp_teardown_ms"] = _emit_request_timing(
                request, "mcp_teardown", started, status="failed"
            )
            return
        timing["mcp_teardown_ms"] = _emit_request_timing(
            request, "mcp_teardown", started, status="ok" if outcome.ok else "warning"
        )
        timing["mcp_teardown_tools"] = len(outcome.removed_tool_names)
        if request.progress_callback is None:
            return
        try:
            request.progress_callback(
                {
                    "type": "run.progress",
                    "phase": "mcp_admission",
                    "severity": "info" if outcome.ok else "warning",
                    "step": "mcp_admission_torn_down",
                    "status": "ok" if outcome.ok else "warning",
                    "summary": (
                        "MCP admission scope removed: "
                        + ", ".join(outcome.servers)
                        + f" ({len(outcome.removed_tool_names)} tool(s))"
                    ),
                    "mcp_teardown": {
                        "servers": list(outcome.servers),
                        "removed_tool_names": list(outcome.removed_tool_names),
                        "failures": outcome.failure_rows(),
                        "duration_ms": outcome.duration_ms,
                    },
                }
            )
        except Exception:
            pass

    def _execute_agent_run(
        self,
        binding: PersonaProfileBinding,
        request: AgentRunRequest,
        *,
        ledger: RunBudgetLedger | None = None,
    ) -> tuple[Any, Any, dict[str, Any]]:
        timing: dict[str, Any] = {}
        ledger = ledger if ledger is not None else RunBudgetLedger()
        from .persona_chat_continuity import tool_execution_scope
        from .terminal_envelope import terminal_envelope_scope
        from agent.skill_utils import skill_runtime_scope

        with (
            _WORKDIR_LOCK,
            persona_profile_context(binding, runtime_root=request.runtime_root),
            _agent_workdir(request.workdir),
            tool_execution_scope(request.tool_execution_scope_id),
            # Bound for the whole run so the terminal tool can resolve which
            # lane/role it is executing under. Deliberately INSIDE
            # persona_profile_context but independent of it: the envelope's
            # historical activation signal (HERMES_AGENT_RUNTIME_ROOT, exported
            # only when the persona binds a Hermes profile) is exactly what made
            # enforcement nondeterministic on this lane.
            terminal_envelope_scope(request.terminal_envelope_scope),
            skill_runtime_scope(
                surface=request.skill_surface,
                root_node_mode=request.skill_root_node_mode,
            ),
            # The admitted MCP registry scope belongs to THIS run. Entered last
            # so it unwinds FIRST — teardown therefore runs while the run still
            # holds _WORKDIR_LOCK and is still inside persona_profile_context,
            # on the raised path as well as the returned one. Using a stack
            # rather than a try/finally keeps the (large) run body unindented.
            ExitStack() as mcp_scope,
        ):
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
                ledger=ledger,
            )
            # Wall-clock checkpoint. Built even when the run carries no wall
            # budget (deadline in the far future ⇒ it never engages) so the
            # tool-start gate below has one unconditional shape. The clock
            # starts HERE — agent construction and the resident-registry probe
            # are on the turn's wall, so the countdown the agent sees must
            # include them.
            wall_checkpoint = WallBudgetCheckpoint(
                turn_budget.TurnWallBudget(
                    total_seconds=_positive_float(request.max_wall_seconds) or _NO_WALL_BUDGET_SECONDS,
                    deadline_epoch=time.time()
                    + (_positive_float(request.max_wall_seconds) or _NO_WALL_BUDGET_SECONDS),
                ),
                progress_callback=request.progress_callback,
                ledger=ledger,
            )
            budget_guard.wall_checkpoint = wall_checkpoint
            # Declared only for a run that HAS a wall budget — the stand-in
            # above exists to give the tool-start gate one unconditional shape,
            # and accounting a 115-year "limit" would be a bound nobody has.
            if _positive_float(request.max_wall_seconds) is not None:
                ledger.declare(
                    RunBudgetKind.WALL,
                    enforcement=RunBudgetEnforcement.LANDS_TURN,
                    unit=UNIT_SECONDS,
                    limit=wall_checkpoint.budget.total_seconds,
                    consumed_provider=wall_checkpoint.consumed_seconds,
                )
            # MCP admission. Inside persona_profile_context (so HERMES_HOME
            # already points at the persona's own profile) and before agent
            # construction (so the admitted tools exist in the registry by the
            # time the factory resolves tool definitions). Bounded and
            # non-raising: a capability probe must never be able to fail a turn,
            # so every degradation comes back as a typed row and the turn
            # continues on the fallback lane.
            admission_outcome = self._admit_mcp_servers(request, timing, ledger=ledger)
            admitted_servers = admission_outcome.admitted if admission_outcome else ()
            if admitted_servers:
                # Declared only for a run that actually got MCP tools: a run with
                # nothing admitted has no MCP calls to bound. The MCP meter is
                # its own authority for the count, so the ledger READS it rather
                # than keeping a second tally that could disagree with the one
                # the refusal itself used.
                call_budget = getattr(admission_outcome, "call_budget", None)
                ledger.declare(
                    RunBudgetKind.MCP_CALLS,
                    enforcement=RunBudgetEnforcement.REFUSES_CALL,
                    unit=UNIT_CALLS,
                    limit=(
                        getattr(call_budget, "limit", None)
                        if call_budget is not None
                        else _positive_int(
                            getattr(request.mcp_admission, "max_tool_calls_per_run", None)
                        )
                    ),
                    consumed=0 if call_budget is None else None,
                    consumed_provider=(
                        None
                        if call_budget is None
                        else lambda: (call_budget.snapshot() or {}).get("spent")
                    ),
                )
                mcp_scope.callback(
                    self._teardown_mcp_admission,
                    request,
                    admitted_servers,
                    timing,
                    budget=call_budget,
                )
            status_callback = _profile_status_callback(request, timing)
            construct_started = time.perf_counter()
            # Per-run reasoning override → agent reasoning_config. Only passed
            # when explicitly requested so an unset run keeps the current
            # behavior (transport reads the global agent.reasoning_effort). The
            # transport reads params["reasoning_config"] = {"enabled": .., "effort": ..}.
            reasoning_kwargs: dict[str, Any] = {}
            if request.reasoning_effort:
                from hermes_constants import parse_reasoning_effort

                reasoning_config = parse_reasoning_effort(request.reasoning_effort)
                if reasoning_config is not None:
                    reasoning_kwargs["reasoning_config"] = reasoning_config
            agent = self._agent_factory(
                provider=runtime.get("provider") or request.provider,
                model=runtime.get("model") or request.model or "",
                api_mode=request.api_mode or runtime.get("api_mode"),
                base_url=runtime.get("base_url"),
                api_key=runtime.get("api_key"),
                **reasoning_kwargs,
                enabled_toolsets=_enabled_toolsets_for_run(request, admitted_servers),
                disabled_toolsets=request.disabled_toolsets,
                blocked_tool_names=_blocked_tool_names_for_run(request),
                quiet_mode=request.quiet_mode,
                skip_context_files=request.skip_context_files,
                skip_memory=request.skip_memory,
                platform=request.platform,
                session_id=request.session_id,
                # Header-only codex cache-scope hint; the default factory applies
                # it to the constructed agent (never to session/transcript load).
                cache_scope_id=request.cache_scope_id,
                credential_pool=self._credential_pool,
                session_db=self._session_db,
                status_callback=status_callback,
                max_iterations=request.max_iterations,
                tool_progress_callback=_progress_adapter(request.progress_callback, "run.progress", guard=budget_guard),
                tool_start_callback=_progress_adapter(request.progress_callback, "run.tool.started", guard=budget_guard),
                tool_complete_callback=_progress_adapter(request.progress_callback, "run.tool.finished", guard=budget_guard),
                clarify_callback=request.clarify_callback,
            )
            if request.persona_chat_runtime_registry is not None and request.root_chat_session_id:
                active_id = request.session_id or request.root_chat_session_id
                entry, reused, rebuild_reason = request.persona_chat_runtime_registry.acquire(
                    root_session_id=request.root_chat_session_id,
                    active_session_id=active_id,
                    signature=request.persona_chat_runtime_signature or "default",
                    revision=request.persona_chat_native_revision or "unknown",
                    factory=lambda: agent,
                )
                if reused:
                    _prepare_resident_persona_chat_agent(entry.agent, agent)
                    agent = entry.agent
                timing["resident_actor_reused"] = 1 if reused else 0
                if rebuild_reason:
                    timing[f"resident_rebuild_{rebuild_reason}"] = 1
            if request.root_chat_session_id:
                agent._persona_chat_root_session_id = request.root_chat_session_id
                agent._persona_chat_client_message_id = request.client_message_id
                agent._persona_chat_turn_id = request.turn_id
                # Type the user row the runtime is about to persist (relay
                # sender attribution). Single write path, marker or not.
                stage_persona_chat_user_row_marker(agent, request)
                # Persona-chat continuity deliberately keeps a stable logical
                # root while native compression advances to a child SessionDB
                # tip.  Hermes' global default is in-place compaction, which
                # cannot express that lineage (and makes the Launcher observe
                # depth=0 forever), so this lane must always use rotation.
                agent.compression_in_place = False
                compressor = getattr(agent, "context_compressor", None)
                if compressor is not None:
                    if request.compression_threshold_tokens_override is not None:
                        threshold_tokens = int(request.compression_threshold_tokens_override)
                        if threshold_tokens <= 0:
                            raise ValueError("compression threshold tokens must be positive")
                        compressor.threshold_tokens = threshold_tokens
                        context_length = int(getattr(compressor, "context_length", 0) or 0)
                        if context_length > 0:
                            compressor.threshold_percent = threshold_tokens / context_length
                    if request.compression_protect_first_n_override is not None:
                        compressor.protect_first_n = max(
                            0, int(request.compression_protect_first_n_override)
                        )
                    if request.compression_protect_last_n_override is not None:
                        compressor.protect_last_n = max(
                            0, int(request.compression_protect_last_n_override)
                        )
            timing["agent_construct_ms"] = _emit_request_timing(request, "agent_construct", construct_started)
            _steer_mcp_admission_notice(agent, request, admission_outcome)
            budget_guard.set_interrupt_callback(lambda reason: _interrupt_agent_for_budget(agent, reason))
            agent_ready_cleanup = _notify_agent_ready(request, agent)
            max_wall_seconds = _positive_float(request.max_wall_seconds)
            if max_wall_seconds is None:
                try:
                    conversation_started = time.perf_counter()
                    conversation_kwargs: dict[str, Any] = {
                        "user_message": request.user_message,
                        "system_message": request.system_message,
                        "task_id": request.task_id,
                    }
                    if request.conversation_history is not None:
                        conversation_kwargs["conversation_history"] = request.conversation_history
                    if request.reuse_current_user_message:
                        conversation_kwargs["reuse_current_user_message"] = True
                    if request.stream_callback is not None:
                        conversation_kwargs["stream_callback"] = request.stream_callback
                    raw_result = agent.run_conversation(**conversation_kwargs)
                    _attach_model_input_observability(raw_result, agent=agent, request=request)
                    timing["conversation_call_ms"] = _emit_request_timing(request, "conversation_call", conversation_started)
                    if budget_guard.tripped_reason:
                        raise RunBudgetExceeded(
                            budget_guard.tripped_reason,
                            session_id=getattr(agent, "session_id", None),
                            run_budget=ledger.accounting(),
                        )
                    timing["run_budget"] = ledger.accounting()
                    return raw_result, agent, timing
                finally:
                    _cleanup_agent_ready(agent_ready_cleanup, request)

            expired = Event()
            wall_checkpoint.bind(agent)

            def interrupt_for_budget() -> None:
                expired.set()
                # Recorded where the bound actually FIRED (the timer thread), so
                # the accounting is already true by the time the main thread
                # raises. An ESCALATION: if the graceful checkpoint had already
                # opened, this replaces the `lands_turn` row with the hard kill
                # that followed it — both happened, and the kill is the fact.
                ledger.trip(
                    RunBudgetKind.WALL,
                    RunBudgetTripReason.WALL_CLOCK_EXCEEDED,
                    consumed=wall_checkpoint.consumed_seconds(),
                    detail=f"wall_seconds={max_wall_seconds:g}",
                    enforcement=RunBudgetEnforcement.TRIPS_RUN,
                )
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

            # The graceful checkpoint opens BEFORE the hard wall (by the
            # reserved window). The hard wall below stays armed at the real
            # deadline as the last resort — it is no longer the first thing that
            # happens when a turn runs long.
            checkpoint_timer: Timer | None = None
            if wall_checkpoint.budget.supports_checkpoint:
                checkpoint_timer = Timer(
                    wall_checkpoint.budget.seconds_until_checkpoint(),
                    lambda: wall_checkpoint.engage(
                        trigger=turn_budget.CHECKPOINT_TRIGGER_TIMER
                    ),
                )
                checkpoint_timer.daemon = True
                checkpoint_timer.start()
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
                if request.conversation_history is not None:
                    conversation_kwargs["conversation_history"] = request.conversation_history
                if request.reuse_current_user_message:
                    conversation_kwargs["reuse_current_user_message"] = True
                if request.stream_callback is not None:
                    conversation_kwargs["stream_callback"] = request.stream_callback
                raw_result = agent.run_conversation(**conversation_kwargs)
                _attach_model_input_observability(raw_result, agent=agent, request=request)
                timing["conversation_call_ms"] = _emit_request_timing(request, "conversation_call", conversation_started)
            except BaseException:
                timing["conversation_call_ms"] = _emit_request_timing(request, "conversation_call", conversation_started, status="failed")
                if expired.is_set():
                    raise RunBudgetExceeded(
                        f"live run budget exceeded: wall_seconds={max_wall_seconds:g}",
                        session_id=getattr(agent, "session_id", None),
                        wall_budget=wall_checkpoint.summary(),
                        run_budget=ledger.accounting(),
                    )
                raise
            finally:
                if checkpoint_timer is not None:
                    checkpoint_timer.cancel()
                timer.cancel()
                _cleanup_agent_ready(agent_ready_cleanup, request)
            if expired.is_set():
                raise RunBudgetExceeded(
                    f"live run budget exceeded: wall_seconds={max_wall_seconds:g}",
                    session_id=getattr(agent, "session_id", None),
                    wall_budget=wall_checkpoint.summary(),
                    run_budget=ledger.accounting(),
                )
            if budget_guard.tripped_reason:
                raise RunBudgetExceeded(
                    budget_guard.tripped_reason,
                    session_id=getattr(agent, "session_id", None),
                    run_budget=ledger.accounting(),
                )
            # The checkpoint fired and the turn still landed a reply: hand the
            # caller the typed provenance so it settles the turn as a
            # budget-ended turn instead of a plain completion.
            if wall_checkpoint.engaged and isinstance(raw_result, dict):
                raw_result["wall_budget_checkpoint"] = wall_checkpoint.summary()
            timing["run_budget"] = ledger.accounting()
            return raw_result, agent, timing


def _enforce_result_budgets(
    result: AgentRunResult,
    request: AgentRunRequest,
    *,
    ledger: RunBudgetLedger | None = None,
) -> None:
    """The two POST-run bounds. Same order, same messages, same exception.

    ``ledger`` is accounting only: each bound declares its limit and actual
    consumption whether or not it trips, so an operator can read the headroom of
    a turn that finished as easily as the bound of one that did not.
    """

    ledger = ledger if ledger is not None else RunBudgetLedger()
    max_api_calls = _positive_int(request.max_api_calls)
    if max_api_calls is not None:
        api_calls = _positive_int(result.api_calls)
        ledger.declare(
            RunBudgetKind.API_CALLS,
            enforcement=RunBudgetEnforcement.TRIPS_RUN,
            unit=UNIT_CALLS,
            limit=max_api_calls,
            consumed=api_calls,
        )
        if api_calls is not None and api_calls > max_api_calls:
            message = f"live run budget exceeded: api_calls={api_calls}/{max_api_calls}"
            ledger.trip(
                RunBudgetKind.API_CALLS,
                RunBudgetTripReason.API_CALLS_EXCEEDED,
                consumed=api_calls,
                detail=message,
            )
            raise RunBudgetExceeded(
                message, session_id=result.session_id, run_budget=ledger.accounting()
            )
    max_total_tokens = _positive_int(request.max_total_tokens)
    if max_total_tokens is not None:
        total_tokens = _positive_int(result.total_tokens)
        ledger.declare(
            RunBudgetKind.TOTAL_TOKENS,
            enforcement=RunBudgetEnforcement.TRIPS_RUN,
            unit=UNIT_TOKENS,
            limit=max_total_tokens,
            consumed=total_tokens,
        )
        if total_tokens is not None and total_tokens > max_total_tokens:
            message = f"live run budget exceeded: total_tokens={total_tokens}/{max_total_tokens}"
            ledger.trip(
                RunBudgetKind.TOTAL_TOKENS,
                RunBudgetTripReason.TOTAL_TOKENS_EXCEEDED,
                consumed=total_tokens,
                detail=message,
            )
            raise RunBudgetExceeded(
                message, session_id=result.session_id, run_budget=ledger.accounting()
            )


def _steer_mcp_admission_notice(agent: Any, request: AgentRunRequest, outcome: Any) -> bool:
    """In-band backstop for admission failures the turn's ENVELOPE could not carry.

    Design §D3 says the agent's own turn context must state a denial, and the
    guaranteed lane for that is the runtime-context envelope's volatile tail —
    rendered before the turn by the mission-chat command, which is why it can
    only carry RESOLUTION-time denials (``mcp_admission_disabled``,
    ``mcp_server_not_configured``, the machine-roots codes).

    EXECUTION-time degradations (``mcp_admission_timeout``,
    ``mcp_admission_lane_busy``, and "admitted but did not register") are only
    known here, after that envelope was sealed. They ride ``agent.steer`` — the
    same in-band lane ``turn_budget``'s checkpoint nudge uses, appended to the
    next tool result — so an agent that starts reaching for tools it does not
    have is told why on its next iteration instead of improvising. An agent that
    calls no tool never needed them.
    """

    if outcome is None or not getattr(outcome, "degraded", False):
        return False
    steer = getattr(agent, "steer", None)
    if not callable(steer):
        return False
    from .mcp_admission import render_mcp_admission_line

    # ``admission=None`` on purpose: only the EXECUTION half belongs here. The
    # policy half is already on this turn's envelope, and repeating it in a
    # second voice teaches the model to discount both.
    line = render_mcp_admission_line(None, outcome=outcome)
    if not line:
        return False
    try:
        steer(f"[harness] {line.removeprefix('- ')}")
    except Exception:  # pragma: no cover - a notice must never fail a turn
        return False
    return True


def _on_mcp_budget_exhausted(
    request: AgentRunRequest,
    denial: Any,
    snapshot: dict[str, Any],
    *,
    ledger: RunBudgetLedger | None = None,
) -> None:
    """First refusal of an admitted MCP call: record it, then tell the operator.

    Fires once per run (``_metered_handler`` gates on ``refused == 1``) from the
    dispatching thread. The ledger record is what makes a refusal readable from
    the run record afterwards — the refusal itself only ever reached the agent's
    tool result and one progress warning, both of which are gone by the time
    anyone asks "why did that turn stop driving the launcher?".
    """

    if ledger is not None:
        try:
            ledger.trip(
                RunBudgetKind.MCP_CALLS,
                RunBudgetTripReason.MCP_CALLS_EXHAUSTED,
                consumed=(snapshot or {}).get("spent"),
                detail=getattr(denial, "code", None) or None,
            )
        except Exception:  # pragma: no cover - accounting must never fail a tool call
            pass
    _emit_mcp_budget_exhausted(request, denial, snapshot)


def _emit_mcp_budget_exhausted(request: AgentRunRequest, denial: Any, snapshot: dict[str, Any]) -> None:
    """Operator-facing half of ``mcp_admission_budget_exhausted``. Never raises.

    Fires ONCE per run, on the first refused call, from whichever thread
    dispatched the tool. It is deliberately a ``run.progress`` WARNING and not an
    interrupt: the agent keeps its non-MCP tools and can still land a reply, and
    a turn that lands honestly beats a turn that dies at the bound.
    """

    callback = request.progress_callback
    if callback is None:
        return
    try:
        callback(
            {
                "type": "run.progress",
                "phase": "mcp_admission",
                "severity": "warning",
                "step": "mcp_admission_budget_exhausted",
                "status": "warning",
                "summary": (
                    "MCP call budget exhausted "
                    f"({snapshot.get('spent')}/{snapshot.get('limit')} admitted call(s)); "
                    "further MCP calls are refused for this turn."
                ),
                "mcp_call_budget": dict(snapshot),
                "mcp_admission": {"denied": [denial.row()]},
            }
        )
    except Exception:  # pragma: no cover - accounting must never fail a tool call
        return


def _notify_agent_ready(request: AgentRunRequest, agent: Any) -> Callable[[], None] | None:
    callback = request.agent_ready_callback
    if callback is None:
        return None
    try:
        return callback(agent)
    except Exception as exc:
        _emit_agent_ready_callback_warning(request, exc, phase="start")
        return None


def _cleanup_agent_ready(cleanup: Callable[[], None] | None, request: AgentRunRequest) -> None:
    if cleanup is None:
        return
    try:
        cleanup()
    except Exception as exc:
        _emit_agent_ready_callback_warning(request, exc, phase="cleanup")


def _emit_agent_ready_callback_warning(request: AgentRunRequest, exc: Exception, *, phase: str) -> None:
    if request.progress_callback is None:
        return
    try:
        request.progress_callback(
            {
                "type": "run.progress",
                "phase": "agent_ready_callback",
                "severity": "warning",
                "step": phase,
                "status": "failed",
                "summary": f"Agent ready callback {phase} failed: {type(exc).__name__}",
            }
        )
    except Exception:
        return


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


def _profile_status_callback(request: AgentRunRequest, timing: dict[str, Any]):
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
            # Wall-budget gate, evaluated BEFORE a tool execution starts. Unlike
            # every other guard here it does not raise: engaging the checkpoint
            # lands the turn (final reply, typed terminal state) rather than
            # tripping it. The already-signalled tool still runs — this stops
            # the NEXT loop iteration from launching more.
            if guard.wall_checkpoint is not None and step == "tool_started":
                guard.wall_checkpoint.gate()
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
                    guard.trip(
                        f"repeated read/search loop: {tool_name}",
                        kind=RunBudgetKind.READ_SEARCH,
                        trip_reason=RunBudgetTripReason.REPEATED_READ_SEARCH_LOOP,
                    )
                    raise RunBudgetExceeded(
                        f"repeated read/search loop: {tool_name}",
                        run_budget=guard.ledger.accounting(),
                    )
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
    guard.trip(
        reason,
        kind=RunBudgetKind.READ_SEARCH,
        trip_reason=RunBudgetTripReason.AGGREGATE_READ_SEARCH_EXCEEDED,
    )
    raise RunBudgetExceeded(reason, run_budget=guard.ledger.accounting())


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


def _agent_chat_target_label(tool_name: str | None, invocation: Any) -> str | None:
    """Operator label for an agent-to-agent relay: who was messaged plus a
    bounded excerpt of the briefing. Secret-bearing text is dropped whole —
    the relay stays legible, never leaky."""

    if tool_name != "agent_chat_send" or not isinstance(invocation, dict):
        return None
    persona = str(invocation.get("persona_id") or "").strip()
    if not persona or _line_has_secret(persona):
        return None
    excerpt = " ".join(str(invocation.get("message") or "").split())
    if excerpt and _line_has_secret(excerpt):
        excerpt = ""
    if len(excerpt) > 90:
        excerpt = excerpt[:89] + "…"
    return f"→ {persona}: {excerpt}" if excerpt else f"→ {persona}"


_DISPATCH_TARGET_MAX = 120
_DISPATCH_ORDER_MAX = 1500


def _scrub_dispatch_order(message: Any) -> str | None:
    """The FULL relay order for the operator console's dispatch tile: per-line
    secret drop (any line matching :func:`_line_has_secret` is removed, the rest
    kept), newlines PRESERVED (never whitespace-collapsed like ``target_label``),
    capped at :data:`_DISPATCH_ORDER_MAX` chars with a trailing ellipsis."""

    text = str(message or "").replace("\r\n", "\n").replace("\r", "\n")
    kept = [line for line in text.split("\n") if not _line_has_secret(line)]
    order = "\n".join(kept).strip()
    if not order:
        return None
    if len(order) > _DISPATCH_ORDER_MAX:
        order = f"{order[: _DISPATCH_ORDER_MAX - 1]}…"
    return order


def _agent_chat_dispatch_fields(tool_name: str | None, invocation: Any) -> dict[str, str]:
    """Structured dispatch fields for an ``agent_chat_send`` relay so the
    operator console renders a first-class ``→ target`` chip and the FULL order
    without re-parsing the prose ``target_label`` (which excerpts to 90 chars).
    ``target_label``/``summary`` prose stay byte-identical — these are additive
    keys alongside them. Consistent with :func:`_agent_chat_target_label`: when
    the persona carries a secret, both the label and these fields drop it."""

    if tool_name != "agent_chat_send" or not isinstance(invocation, dict):
        return {}
    fields: dict[str, str] = {}
    persona = str(invocation.get("persona_id") or "").strip()
    if persona and not _line_has_secret(persona):
        fields["dispatch_target"] = persona[:_DISPATCH_TARGET_MAX]
    order = _scrub_dispatch_order(invocation.get("message"))
    if order:
        fields["dispatch_order"] = order
    return fields


def _tool_started_payload(event_type: str, tool_name: str | None, *, invocation: Any = None) -> dict[str, Any]:
    payload = {"type": event_type, "phase": "tool", "step": "tool_started", "status": "started"}
    if tool_name:
        payload["tool_name"] = tool_name
        payload["summary"] = f"Started tool {tool_name}"
    else:
        payload["summary"] = "Started tool"
    agent_chat_label = _agent_chat_target_label(tool_name, invocation)
    if agent_chat_label:
        payload["target_label"] = agent_chat_label
        payload["summary"] = f"Started tool {tool_name}: {agent_chat_label}"
        # Additive G2 dispatch fields (structured target + full order). The
        # started event is the authoritative carrier; the launcher merges the
        # started/finished pair so finished-only is not needed here.
        payload.update(_agent_chat_dispatch_fields(tool_name, invocation))
        return payload
    command_label = _safe_command_label(invocation)
    if command_label:
        payload["command_label"] = command_label
        if tool_name:
            payload["summary"] = f"Started tool {tool_name}: {command_label}"
    command_full = _safe_operator_command(invocation)
    if command_full:
        payload["command_full"] = command_full
    target_label = _safe_operator_target(invocation)
    if target_label:
        payload["target_label"] = target_label
        if tool_name and not command_label:
            payload["summary"] = f"Started tool {tool_name}: {target_label}"
    skill_name = _safe_skill_tool_name(tool_name, invocation)
    if skill_name:
        payload["skill_name"] = skill_name
    _attach_tool_io(payload, invocation=invocation)
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
    skill_name = _safe_skill_tool_name(tool_name, invocation) or _safe_skill_tool_name(tool_name, result)
    if skill_name:
        payload["skill_name"] = skill_name
    dev_work_payload = _dev_work_payload(tool_name, status=status, result=result, invocation=invocation)
    if dev_work_payload:
        # No target echo and NO generic tool_input/tool_result for dev-work
        # tools: changed_paths/changed_files ARE the record, and the raw
        # invocation/result carry the diff body and machine-absolute paths
        # that this lane deliberately never persists.
        payload.update(dev_work_payload)
        return payload
    target_label = (
        _agent_chat_target_label(tool_name, invocation) or _safe_operator_target(invocation)
    )
    if target_label:
        payload["target_label"] = target_label
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
    command_full = _safe_operator_command(invocation)
    if command_full:
        payload["command_full"] = command_full
    output = _safe_operator_output(tool_name, result)
    if output:
        payload["output"] = output
    todo_state = _todo_state_payload(tool_name, result, invocation)
    if todo_state is not None:
        payload["todo_state"] = todo_state
    # agent_chat_send input is already first-class (dispatch_target/dispatch_order
    # on the started event) — re-attaching the order as tool_input would spend
    # the same bytes twice against the event cap. Its RESULT still attaches.
    _attach_tool_io(
        payload,
        invocation=None if tool_name == "agent_chat_send" else invocation,
        result=result,
    )
    return payload


def _dev_work_payload(tool_name: str | None, *, status: str, result: Any, invocation: Any) -> dict[str, Any] | None:
    normalized_tool = (tool_name or "").lower()
    if normalized_tool in {"patch", "apply_patch"}:
        # The tool RESULT often returns no file list; the diff headers in the
        # INVOCATION are the reliable record of what an edit call touched.
        candidates = _candidate_file_values(result, None) or _patch_paths_from_invocation(invocation)
        labels = _safe_file_labels(candidates)
        operator_paths = _safe_operator_paths(candidates)
        payload: dict[str, Any] = {"phase": "dev_work", "step": "patch"}
        if operator_paths:
            payload["changed_paths"] = operator_paths
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
        candidates = _candidate_file_values(result, invocation)
        labels = _safe_file_labels(candidates)
        operator_paths = _safe_operator_paths(candidates)
        payload = {"phase": "dev_work", "step": "write_file" if normalized_tool == "write_file" else "code_edit"}
        if operator_paths:
            payload["changed_paths"] = operator_paths
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


def _safe_skill_tool_name(tool_name: str | None, value: Any) -> str | None:
    if (tool_name or "").lower() != "skill_view":
        return None
    return _safe_skill_identifier_from_value(value)


def _safe_skill_identifier_from_value(value: Any) -> str | None:
    if isinstance(value, str):
        return _safe_label(value)
    if not isinstance(value, dict):
        return None
    for key in ("skill_name", "skill", "identifier", "name"):
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            return _safe_label(raw)
    for key in ("input", "tool_input", "invocation", "metadata", "result"):
        nested = value.get(key)
        found = _safe_skill_identifier_from_value(nested)
        if found:
            return found
    return None


# Operator-console field extractors. These feed the trusted Mission Control
# operator chat (the v2 stream + persisted turn elements) and are deliberately
# LESS strict than `_safe_command_label` (which is path-stripped for Telegram and
# other surfaces): the operator wants to see the real command and its output,
# including paths. Secrets are still scrubbed line-by-line and everything is
# bounded. Do NOT route these into untrusted surfaces.
_OPERATOR_SECRET_MARKERS = (
    "password",
    "passwd",
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "bearer ",
    "credential",
    "secret",
    "private_key",
    "private-key",
    "access_key",
    "session_token",
    " token=",
    "x-api-key",
    "sk-",
)
_OPERATOR_COMMAND_MAX = 1000
_OPERATOR_OUTPUT_MAX_LINES = 200
_OPERATOR_OUTPUT_MAX_CHARS = 8000
_OPERATOR_TERMINAL_TOOLS = {
    "terminal",
    "shell",
    "bash",
    "sh",
    "command",
    "run_command",
    "run",
    "code_execution",
    "code_execution_tool",
    "execute",
    "exec",
}


def _line_has_secret(line: str) -> bool:
    lowered = line.lower()
    return any(marker in lowered for marker in _OPERATOR_SECRET_MARKERS)


def _safe_operator_command(invocation: Any) -> str | None:
    if not isinstance(invocation, dict):
        return None
    command = invocation.get("command") or invocation.get("cmd")
    if not isinstance(command, str):
        return None
    text = command.strip()
    if not text:
        return None
    # Scrub a command that itself embeds a secret (e.g. `curl -H "authorization: …"`).
    if _line_has_secret(text):
        return "[command withheld — contained a secret]"
    if len(text) > _OPERATOR_COMMAND_MAX:
        text = f"{text[: _OPERATOR_COMMAND_MAX - 1]}…"
    return text


def _safe_operator_output(tool_name: str | None, result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    if (tool_name or "").lower() not in _OPERATOR_TERMINAL_TOOLS:
        return None
    raw = result.get("output")
    if not isinstance(raw, str):
        raw = result.get("stdout")
    if not isinstance(raw, str):
        return None
    text = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return None
    lines = [
        "[redacted line — contained a secret]" if _line_has_secret(line) else line
        for line in text.split("\n")
    ]
    truncated = False
    if len(lines) > _OPERATOR_OUTPUT_MAX_LINES:
        lines = lines[-_OPERATOR_OUTPUT_MAX_LINES :]
        truncated = True
    text = "\n".join(lines)
    if len(text) > _OPERATOR_OUTPUT_MAX_CHARS:
        text = text[-_OPERATOR_OUTPUT_MAX_CHARS :]
        truncated = True
    if truncated:
        text = f"…(earlier output truncated)…\n{text}"
    return text


# Generic tool-call input/result record (the "what was it called with / what
# came back" lane). Terminal-class calls keep their dedicated fields
# (command_full / output) and dev-work calls keep changed_paths; every tool
# call additionally gets a bounded, secret-scrubbed rendering of its raw
# invocation and result when no dedicated field captured them — so the
# operator console never has to show a bare "no detail was emitted" row.
# Dict payloads render one `key: <json>` line per top-level key so the
# per-line secret scrub drops only the offending pair, never the whole record.
_OPERATOR_TOOL_INPUT_MAX = 1000
_OPERATOR_TOOL_RESULT_MAX = 1600
# Result-envelope keys that dedicated payload fields already carry.
_TOOL_RESULT_ECHO_KEYS = ("exit_code",)


def _render_kv_line_token(value: Any) -> str:
    """One-line rendering of a dict KEY (or the last-resort repr). Newlines and
    NULs are REMOVED (not replaced with spaces) — the per-line secret scrub
    keys on contiguous marker words, so a hostile key like ``"pass\\nword"``
    must reconstitute to ``password`` on ONE line rather than split the marker
    across two lines and defeat every scrub layer downstream."""

    return re.sub(r"[\r\n\x00]+", "", str(value))


def _render_operator_kv_block(value: Any) -> str | None:
    try:
        if isinstance(value, dict):
            text = "\n".join(
                f"{_render_kv_line_token(key)}: {json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)}"
                for key, item in value.items()
            )
        elif isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        # Last-resort repr, itself guarded: str() can re-raise on pathological
        # values (RecursionError on deep nesting). Losing the IO record is
        # acceptable; losing the WHOLE tool event (via the sink's best-effort
        # boundary swallowing the raise) is not.
        try:
            text = _render_kv_line_token(value)
        except Exception:
            return None
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return text or None


def _scrub_operator_block_head(text: str, *, limit: int) -> str | None:
    """Per-line secret scrub, HEAD-bounded: the leading keys/fields are the
    operator signal (unlike command output, where the tail is), so truncation
    keeps the front and marks the cut explicitly. A record whose EVERY line was
    redacted carries zero signal — dropped whole rather than persisted as a
    marker-only blob."""

    kept_any = False
    lines: list[str] = []
    for line in text.split("\n"):
        if _line_has_secret(line):
            lines.append("[redacted line — contained a secret]")
        else:
            lines.append(line)
            if line.strip():
                kept_any = True
    if not kept_any:
        return None
    out = "\n".join(lines).strip()
    if not out:
        return None
    if len(out) > limit:
        out = f"{out[:limit]}\n…(rest truncated)…"
    return out


def _safe_operator_tool_input(invocation: Any) -> str | None:
    if not isinstance(invocation, dict) or not invocation:
        return None
    rendered = _render_operator_kv_block(invocation)
    if rendered is None:
        return None
    return _scrub_operator_block_head(rendered, limit=_OPERATOR_TOOL_INPUT_MAX)


def _safe_operator_tool_result(result: Any) -> str | None:
    if result is None:
        return None
    if isinstance(result, dict):
        slim = {key: item for key, item in result.items() if key not in _TOOL_RESULT_ECHO_KEYS}
        if not slim:
            return None
        rendered = _render_operator_kv_block(slim)
    else:
        rendered = _render_operator_kv_block(result)
    if rendered is None:
        return None
    return _scrub_operator_block_head(rendered, limit=_OPERATOR_TOOL_RESULT_MAX)


def _attach_tool_io(payload: dict[str, Any], *, invocation: Any, result: Any = None) -> None:
    """Attach the generic ``tool_input`` / ``tool_result`` record to a tool payload.

    Dedicated fields stay authoritative and are never duplicated against the
    4KB event cap: a call whose input already surfaced as a command keeps
    command_full as its input record, and a terminal-class call's result IS its
    ``output`` tail. Everything else gets the generic record.
    """

    if "command_full" not in payload and "command_label" not in payload:
        tool_input = _safe_operator_tool_input(invocation)
        if tool_input:
            payload["tool_input"] = tool_input
    if result is not None and "output" not in payload:
        tool_result = _safe_operator_tool_result(result)
        if tool_result:
            payload["tool_result"] = tool_result


_OPERATOR_TARGET_MAX = 300
_OPERATOR_TARGET_PATH_KEYS = ("path", "file_path", "target_path", "file", "filename", "directory", "dir")
_OPERATOR_TARGET_QUERY_KEYS = ("pattern", "query", "glob", "regex", "search", "name")
# Diff/patch header forms the runner sees in the wild: the OpenAI apply_patch
# envelope (*** Update|Add|Delete File: path), unified diff (+++ b/path), and
# git headers (diff --git a/p b/p).
_PATCH_HEADER_RE = re.compile(
    r"^\*\*\* (?:Update|Add|Delete) File: (.+?)\s*$"
    r"|^\+\+\+ b/(.+?)\s*$"
    r"|^diff --git a/\S+ b/(\S+)\s*$",
    re.MULTILINE,
)


# Bare-word markers for operator path/target scrubbing: stricter than
# _OPERATOR_SECRET_MARKERS (bare "token", not just " token=") because a path
# named private_token.dart must never surface, even relative.
_OPERATOR_PATH_SENSITIVE_MARKERS = (
    "secret", "token", "password", "passwd", "api_key", "apikey",
    "authorization", "bearer", "credential", "cookie", "private_key", "sk-",
)
_ABSOLUTE_PATHISH_RE = re.compile(r"^([A-Za-z]:/|//|/|~)")


def _operator_path_sensitive(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _OPERATOR_PATH_SENSITIVE_MARKERS)


def _safe_operator_target(invocation: Any) -> str | None:
    """Operator-console label for what a read/search/list tool acted on.

    Operator-grade but path-disciplined: repo-relative paths surface verbatim;
    absolute paths are trimmed to their trailing segments; anything carrying a
    secret-looking token is dropped. Do NOT route into untrusted surfaces; the
    Telegram-safe lane stays ``command_label``.
    """

    if not isinstance(invocation, dict):
        return None

    def _clean(value: Any) -> str | None:
        text = " ".join(str(value).strip().split()).replace("\\", "/")
        if not text or _line_has_secret(text) or _operator_path_sensitive(text):
            return None
        if _ABSOLUTE_PATHISH_RE.match(text):
            segments = [segment for segment in text.split("/") if segment]
            if len(segments) < 2:
                return None
            text = "…/" + "/".join(segments[-3:])
        return text

    path = next(
        (
            cleaned
            for key in _OPERATOR_TARGET_PATH_KEYS
            if isinstance(invocation.get(key), str) and invocation.get(key).strip()
            if (cleaned := _clean(invocation.get(key))) is not None
        ),
        None,
    )
    query = next(
        (
            cleaned
            for key in _OPERATOR_TARGET_QUERY_KEYS
            if isinstance(invocation.get(key), str) and invocation.get(key).strip()
            if (cleaned := _clean(invocation.get(key))) is not None
        ),
        None,
    )
    if query and path:
        label = f"{query} in {path}"
    else:
        label = query or path
    if not label:
        return None
    return f"{label[: _OPERATOR_TARGET_MAX - 1]}…" if len(label) > _OPERATOR_TARGET_MAX else label


def _patch_paths_from_invocation(invocation: Any) -> list[str]:
    """Changed-file paths recovered from the patch text itself.

    The patch tool's RESULT frequently returns no file list ("changed-file
    list unavailable"), but the file paths are right there in the diff headers
    of the INVOCATION. Parsing them makes edit calls legible to the operator.
    """

    if not isinstance(invocation, dict):
        return []
    text = next(
        (
            invocation.get(key)
            for key in ("patch", "diff", "patch_text", "input", "content")
            if isinstance(invocation.get(key), str) and invocation.get(key).strip()
        ),
        None,
    )
    if not text:
        return []
    paths: list[str] = []
    for match in _PATCH_HEADER_RE.finditer(text):
        raw = next((group for group in match.groups() if group), None)
        if not raw:
            continue
        cleaned = raw.strip().replace("\\", "/")
        if not cleaned or cleaned == "/dev/null" or _line_has_secret(cleaned):
            continue
        if cleaned not in paths:
            paths.append(cleaned)
        if len(paths) >= 20:
            break
    return paths


def _safe_operator_paths(values: list[Any]) -> list[str]:
    """Operator-grade changed-path list: RELATIVE paths only, bounded.

    Absolute paths never surface (machine-identifying); their basenames still
    reach the operator through ``changed_files``. Secret-looking names drop.
    """

    paths: list[str] = []
    for item in values:
        text = " ".join(str(item or "").strip().split()).replace("\\", "/")
        if not text or _line_has_secret(text) or _operator_path_sensitive(text):
            continue
        if _ABSOLUTE_PATHISH_RE.match(text):
            continue
        if len(text) > 200:
            text = f"…{text[-199:]}"
        if text not in paths:
            paths.append(text)
        if len(paths) >= 12:
            break
    return paths


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
        # Harness tool envelope: a top-level ok:false IS the failure verdict
        # (agent_chat_send, harness verbs). Missing this projected failed
        # dispatches as status="passed" → green OK chips on the operator
        # console for sends that never reached their target (2026-07-23).
        if result.get("ok") is False:
            return True
        exit_code = _safe_exit_code(result.get("exit_code"))
        if exit_code is not None:
            return exit_code != 0
    if isinstance(result, str):
        lowered = result.strip().lower()
        if lowered.startswith(("error", "traceback", "exception")) or '"success": false' in lowered:
            return True
        # Serialized harness envelope. Parse-confirm before trusting the
        # substring so a tool result that merely CONTAINS such text (e.g. a
        # read_file of a JSON fixture) can never be misread as a failure —
        # only a top-level {"ok": false, ...} object counts.
        if lowered.startswith("{") and ('"ok": false' in lowered or '"ok":false' in lowered):
            try:
                parsed = json.loads(result)
            except (ValueError, TypeError):
                return False
            return isinstance(parsed, dict) and parsed.get("ok") is False
    return False


# Bounds for the todo checklist mirrored onto the trace/turn-store lane. The
# store itself caps content at MAX_TODO_CONTENT_CHARS (4000) / MAX_TODO_ITEMS
# (256); the operator-console projection is a COMPACT checklist, so the wire
# copy is tighter — a checklist row is a short line, and the frame must stay
# minimal (T7: smallest honest emit, no snapshot-frame growth beyond a bounded
# payload).
#
# Over-cap content is marked with an ellipsis, never dropped silently.
#
# T9c: id/content are whitespace-collapsed to the SAME shape the persist
# re-bound (`mission_chat_turns._safe_todo_state`, via `safe_assignment_text`)
# produces — whitespace runs collapsed to single spaces. The persist lane
# re-runs `safe_assignment_text` over THIS output, and that function is
# idempotent on an already-collapsed string (including the over-cap
# `…`-terminated form), so the live `tool.finished` frame and the reloaded
# turn-store element carry byte-identical text. (Before T9c the producer only
# `.strip()`ped, so multi-line/multi-space content diverged: the live lane kept
# the internal whitespace the persist lane collapsed.)
_TODO_STATE_MAX_ITEMS = 64
_TODO_STATE_MAX_CONTENT = 240
_TODO_STATE_MAX_ID = 120
_TODO_STATE_VALID_STATUS = {"pending", "in_progress", "completed", "cancelled"}


def _todo_state_payload(tool_name: str | None, result: Any, invocation: Any) -> list[dict[str, str]] | None:
    """Minimal structured todo-checklist state for the operator console (T7).

    The ``todo`` tool keeps its list in-memory per session and re-injects it into
    the prompt; nothing on the trace/turn-store lane carried the list itself (the
    element ``args`` is a human summary and ``output`` is gated to terminal
    tools). This copies the tool RESULT — the authoritative post-write list (all
    statuses) returned by :func:`tools.todo_tool.todo_tool` — onto the finished
    event, bounded and validated. Falls back to the invocation's ``todos`` when
    the result is unparseable. Returns ``None`` for any non-todo tool or when no
    list is recoverable (unparseable result AND invocation) — absence means "no
    todo involvement", never fabricate. A recovered but EMPTY list returns an
    explicit ``[]`` (T9d): a todo write that clears the checklist must tell the
    operator console to clear it, a state distinct from absence."""

    if (tool_name or "").lower() != "todo":
        return None
    todos = _todo_items_from(result)
    if todos is None:
        todos = _todo_items_from(invocation)
    if todos is None:
        # Neither the result nor the invocation yielded a list — unrecoverable,
        # so stay silent (absent). This is NOT an empty checklist: a cleared list
        # comes back as ``[]`` from _todo_items_from and falls through to the
        # explicit-empty return below (T9d cleared-todo contract).
        return None
    items: list[dict[str, str]] = []
    for raw in todos[:_TODO_STATE_MAX_ITEMS]:
        if not isinstance(raw, dict):
            continue
        # T9c: collapse whitespace to the persisted `safe_assignment_text` shape
        # (id is a straight cap; content keeps the over-cap ellipsis, which the
        # persist re-run preserves because it operates on this already-collapsed
        # output). See the note on the bound constants above.
        item_id = _collapse_todo_ws(raw.get("id"))[:_TODO_STATE_MAX_ID] or "?"
        content = _collapse_todo_ws(raw.get("content"))
        status = str(raw.get("status", "")).strip().lower()
        if status not in _TODO_STATE_VALID_STATUS:
            status = "pending"
        if not content:
            content = "(no description)"
        elif len(content) > _TODO_STATE_MAX_CONTENT:
            content = content[: _TODO_STATE_MAX_CONTENT - 1] + "…"
        items.append({"id": item_id, "content": content, "status": status})
    # T9d: return ``items`` directly (NOT ``items or None``) so a recovered but
    # empty list stays an explicit ``[]`` — the cleared-checklist signal. ``None``
    # is reserved for non-todo tools / unrecoverable payloads (handled above).
    return items


def _collapse_todo_ws(value: Any) -> str:
    """Collapse whitespace runs to single spaces, mirroring the normalization in
    ``persona_assignments.safe_assignment_text`` (minus its length cap).

    The persist re-bound (``mission_chat_turns._safe_todo_state``) runs
    ``safe_assignment_text`` over THIS output; that function is idempotent on an
    already-collapsed string, so the live ``tool.finished`` frame and the
    reloaded turn-store element carry byte-identical todo text (T9c)."""

    return " ".join(str(value or "").replace("\x00", " ").split())


def _todo_items_from(source: Any) -> list[Any] | None:
    """Recover the ``todos`` list from a todo tool result/invocation.

    Accepts the JSON-string result (``{"todos": [...], "summary": {...}}``), a
    dict result/invocation carrying ``todos``, or a bare list. Returns ``None``
    when no list is recoverable."""

    value: Any = source
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
    if isinstance(value, dict):
        todos = value.get("todos")
        return todos if isinstance(todos, list) else None
    if isinstance(value, list):
        return value
    return None


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
    """Reasoning text from a thinking callback, operator-grade.

    Two positional shapes reach this: the subagent relay
    ``("_thinking", first_line)`` and the structured emission
    ``("reasoning.available", "_thinking", text, None)`` — ``"_thinking"`` is
    the channel placeholder in both, never content. Paths are allowed
    (operator console); secret-bearing lines are masked in place.
    """

    candidates: list[Any] = [
        kwargs.get("reasoning_summary"),
        kwargs.get("summary"),
        kwargs.get("reasoning"),
    ]
    candidates.extend(arg for arg in args[1:4] if arg != "_thinking")
    for value in candidates:
        if not isinstance(value, str):
            continue
        masked = " ".join(
            "[redacted line — contained a secret]" if _line_has_secret(line) else line
            for line in value.strip().splitlines()
        )
        text = " ".join(masked.split())
        if not text:
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
    system_prompt_sections: list[dict[str, Any]] = []
    if system_prompt:
        system_preview = _message_preview(
            "system", str(system_prompt), source="hermes_system_prompt"
        )
        messages.append(system_preview)
        system_prompt_sections = _system_prompt_section_receipts(
            agent=agent,
            system_message=request.system_message,
            system_prompt=str(system_prompt),
            captured_content=system_preview["content"],
        )
    messages.append(_message_preview("user", request.user_message, source="mission_chat_user_message"))
    cache_routing = _agent_cache_routing_observability(agent)
    return {
        "schema_version": 1,
        "kind": "redaction_safe_final_model_input",
        "platform": request.platform,
        "profile": request.profile,
        "session_id": request.session_id,
        "task_id": request.task_id,
        "enabled_toolsets": list(request.enabled_toolsets or []),
        "disabled_toolsets": list(request.disabled_toolsets or []),
        "tool_schema": {
            "schema_version": 1,
            "kind": "actual_model_tools",
            "final_model_tools": _agent_tool_names(agent),
            "tool_count": len(_agent_tool_names(agent)),
            # Wire size of the tool schemas, which ship in FULL on every API call
            # (agent/conversation_loop.py passes tools=agent.tools each time).
            # Names alone hid the largest fixed slice of the prompt from the
            # context inspector. Same measurement as `hermes prompt-size`.
            "json_bytes": _agent_tools_json_bytes(agent),
        },
        **({"cache_routing": cache_routing} if cache_routing is not None else {}),
        "skip_context_files": bool(request.skip_context_files),
        "skip_memory": bool(request.skip_memory),
        "system_message_supplied": request.system_message is not None,
        "message_count": len(messages),
        "messages": messages,
        **(
            {"system_prompt_sections": system_prompt_sections}
            if system_prompt_sections
            else {}
        ),
        # T8 (2026-07-18): the rendered compact skills-index text's char count —
        # what ``.skills_prompt_snapshot.json`` ACTUALLY contributed to the
        # prompt this turn, distinct from the loaded-file byte estimate the
        # context-file row carries (the 41 KB snapshot renders to ~9 KB of index
        # text in the prompt). Measured against the AGENT's own resolved tool
        # set so the launcher attributes the real in-prompt cost, not the file
        # size. Omitted (never fabricated) when unmeasurable. See
        # ``_rendered_skills_prompt_chars``.
        **(
            {"skills_prompt_chars": _skills_chars}
            if (_skills_chars := _rendered_skills_prompt_chars(agent)) is not None
            else {}
        ),
    }


def _system_prompt_section_receipts(
    *,
    agent: Any,
    system_message: str | None,
    system_prompt: str,
    captured_content: str,
) -> list[dict[str, Any]]:
    """Return exact offsets for Hermes' stable/context/volatile prompt tiers.

    The profile runner may restore a cached system prompt from SessionDB.  We
    therefore re-render the three canonical parts only to prove byte equality;
    if they do not join to the exact prompt sent this turn, no section metadata
    is emitted.  Offsets target the already-redacted captured message so the
    Launcher can slice it without duplicating the full prompt on the wire.
    """

    try:
        from agent.system_prompt import build_system_prompt_parts

        parts = build_system_prompt_parts(agent, system_message=system_message)
    except Exception:
        return []
    ordered = [
        ("stable", "Stable Hermes foundation", str(parts.get("stable") or "")),
        ("context", "Mission Control context", str(parts.get("context") or "")),
        ("volatile", "Volatile profile context", str(parts.get("volatile") or "")),
    ]
    nonempty = [(kind, name, value) for kind, name, value in ordered if value]
    if "\n\n".join(value for _, _, value in nonempty) != system_prompt:
        return []

    safe_parts = [
        (kind, name, _redact_prompt_text(value)) for kind, name, value in nonempty
    ]
    safe_joined = "\n\n".join(value for _, _, value in safe_parts)
    if not safe_joined.startswith(captured_content):
        return []

    receipts: list[dict[str, Any]] = []
    cursor = 0
    capture_length = len(captured_content)
    for kind, name, value in safe_parts:
        start = cursor
        end = start + len(value)
        if start < capture_length:
            captured_end = min(end, capture_length)
            receipts.append(
                {
                    "kind": kind,
                    "name": name,
                    "start_char": start,
                    "end_char": captured_end,
                    "chars": len(value),
                    "truncated": captured_end < end,
                }
            )
        cursor = end + 2
    return receipts


def _agent_cache_routing_observability(agent) -> dict[str, Any] | None:
    """Read the agent-owned redaction-safe final-request cache facts.

    The request builder copies these from the short-lived Responses transport
    only after request overrides and provider-specific header/body routing.
    Other transports and test doubles simply omit the block; absence is honest
    and never fabricated.
    """

    value = getattr(agent, "_last_cache_routing_observability", None)
    return dict(value) if isinstance(value, dict) else None


def _rendered_skills_prompt_chars(agent) -> int | None:
    """Char count of the compact skills index this turn actually rendered.

    Recomputes ``build_skills_system_prompt`` with the AGENT's own resolved tool
    set — byte-for-byte the same call ``agent/system_prompt.py`` made while
    assembling this turn's system prompt, so it is a guaranteed in-process LRU
    cache HIT: zero re-scan, zero disk I/O, zero snapshot write (this runs inside
    the persona profile-home override, so the cache key matches the turn's). The
    result is the EXACT rendered text, not a size heuristic.

    Returns ``None`` (the launcher then omits the in-prompt chip and keeps the
    loaded-file estimate) when the lane ships no skills tools, the render is
    empty, or anything is unmeasurable — never a fabricated number."""

    try:
        valid = getattr(agent, "valid_tool_names", None)
        if not valid:
            return None
        # Mirror the gate in agent/system_prompt.py: the skills index only
        # renders when the lane ships one of the skills tools.
        if not any(name in valid for name in ("skills_list", "skill_view", "skill_manage")):
            return None
        import run_agent

        avail_toolsets = {
            toolset
            for toolset in (run_agent.get_toolset_for_tool(name) for name in valid)
            if toolset
        }
        try:
            from agent.coding_context import coding_compact_skill_categories
            from agent.runtime_cwd import resolve_context_cwd

            compact = (
                coding_compact_skill_categories(
                    platform=getattr(agent, "platform", None), cwd=resolve_context_cwd()
                )
                or None
            )
        except Exception:
            compact = None
        rendered = run_agent.build_skills_system_prompt(
            available_tools=valid,
            available_toolsets=avail_toolsets,
            compact_categories=compact,
        )
        if not isinstance(rendered, str):
            return None
        return len(rendered)
    except Exception:
        return None


def _agent_tools_json_bytes(agent) -> int | None:
    """UTF-8 byte size of the serialized tool schemas, or None if unmeasurable."""
    try:
        tools = list(getattr(agent, "tools", None) or [])
        if not tools:
            return 0
        return len(json.dumps(tools, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return None


def _agent_tool_names(agent) -> list[str]:
    names: list[str] = []
    for tool in list(getattr(agent, "tools", []) or []):
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return sorted(names)


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


# The assignment rule is single-homed in ``agent_runtime.redaction`` (see the
# header there for the JSON blind spot every local spelling shared). This lane
# captures the FINAL MODEL PROMPT for observability, so a JSON-encoded
# credential inside a prompt used to be persisted verbatim. The two-group
# contract is preserved — ``_redact_prompt_text`` branches on ``lastindex >= 2``
# to decide between ``key=<redacted>`` and a whole-match ``<redacted>``.
_PROMPT_SECRET_PATTERNS = [
    TEXT_SECRET_VALUE_ASSIGNMENT_RE,
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
            cache_read_tokens=result.get("cache_read_tokens"),
            cache_write_tokens=result.get("cache_write_tokens"),
            reasoning_tokens=result.get("reasoning_tokens"),
            usage_ledger=[
                row for row in (result.get("usage_ledger") or []) if isinstance(row, dict)
            ]
            if isinstance(result.get("usage_ledger"), list)
            else [],
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

    # ``cache_scope_id`` is a fork-runtime, header-only codex cache-scope hint —
    # NOT part of the upstream AIAgent constructor. Pop it before construction so
    # the upstream signature is untouched, then apply it as an attribute the
    # codex build seam reads via ``getattr(agent, "cache_scope_id", None)``. It
    # never participates in session/transcript loading.
    cache_scope_id = kwargs.pop("cache_scope_id", None)
    agent = AIAgent(**kwargs)
    if cache_scope_id:
        agent.cache_scope_id = cache_scope_id
    return agent
