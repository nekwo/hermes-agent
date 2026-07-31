"""RunBudget — one accounting authority over four bounds with three semantics.

A persona run is bounded by several independent mechanisms, and before this
module each kept its own private bookkeeping:

===================  ====================================  ================
mechanism            expression                            enforcement
===================  ====================================  ================
``_ToolBudgetGuard`` raises ``RunBudgetExceeded``           ``trips_run``
wall checkpoint      steers + drains, turn lands a reply    ``lands_turn``
wall hard timer      interrupts, raises                     ``trips_run``
``McpCallBudget``    refuses ONE call, turn continues       ``refuses_call``
result budgets       raise after the run                    ``trips_run``
===================  ====================================  ================

The three semantics are deliberate and this file's central claim is that
unification changed **none** of them. Every mechanism is exercised twice —
untripped and tripped — and pinned against the observable behavior it had
before: same exception type, same exception message, same
``wall_budget_checkpoint`` envelope on the landed turn, same typed refusal row
on the refused call, same progress events.

What is NEW is the accounting: ``profile_timing["run_budget"]`` (and
``RunBudgetExceeded.run_budget`` on the raised path, where no result exists to
carry it) answers "what bounded this turn?" in one block, with every declared
budget's limit and consumption — so a turn that finished with room to spare is
distinguishable from one that stopped at its bound.

Nothing here spawns an MCP server, connects a transport, or sleeps longer than
a fraction of a second.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from agent_runtime import turn_budget
from agent_runtime.mcp_admission import (
    LANE_MISSION_CHAT,
    MCP_ADMISSION_BUDGET_EXHAUSTED,
    McpAdmission,
    McpCallBudget,
)
from agent_runtime.profile_runner import (
    AgentRunRequest,
    ProfileAgentRunner,
    RunBudgetExceeded,
    WallBudgetCheckpoint,
    _progress_adapter,
    _ToolBudgetGuard,
)
from agent_runtime.run_budget import (
    UNIT_CALLS,
    UNIT_SECONDS,
    UNIT_TOKENS,
    RunBudgetEnforcement,
    RunBudgetKind,
    RunBudgetLedger,
    RunBudgetTripReason,
)
from agent_runtime.turn_budget import TurnWallBudget


# ── helpers ─────────────────────────────────────────────────────────────────


def _rows(block: dict) -> dict[str, dict]:
    return {row["kind"]: row for row in block["budgets"]}


class _Agent:
    """Minimal agent: records the callbacks the runner hands it."""

    def __init__(self, **kwargs):
        self.session_id = kwargs.get("session_id") or "session_run_budget"
        self.provider = kwargs.get("provider")
        self.model = kwargs.get("model")
        self.base_url = None
        self.tools = []
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tool_start_callback = kwargs.get("tool_start_callback")
        self.tool_complete_callback = kwargs.get("tool_complete_callback")
        self.steers: list[str] = []
        self.interrupts: list[str] = []

    def steer(self, text: str) -> bool:
        self.steers.append(text)
        return True

    def interrupt(self, message: str | None = None) -> None:
        self.interrupts.append(message or "")

    def _reply(self, **extra):
        payload = {
            "final_response": "ok",
            "session_id": self.session_id,
            "messages": [],
            "api_calls": 1,
            "total_tokens": 3,
        }
        payload.update(extra)
        return payload

    def run_conversation(self, user_message, system_message=None, task_id=None):
        return self._reply()


def _read_search_agent(reads: int):
    """An agent whose only behavior is N finished read/search tool calls."""

    class _Reader(_Agent):
        def run_conversation(self, user_message, system_message=None, task_id=None):
            for _ in range(reads):
                self.tool_complete_callback("run.tool.finished", "read_file", {}, "{}")
            return self._reply()

    return _Reader


def _sleeping_agent(seconds: float):
    class _Sleeper(_Agent):
        def run_conversation(self, user_message, system_message=None, task_id=None):
            time.sleep(seconds)
            return self._reply()

    return _Sleeper


def _request(**kwargs) -> AgentRunRequest:
    return AgentRunRequest(profile=None, user_message="hi", **kwargs)


def _run(agent_factory, **kwargs):
    return ProfileAgentRunner(agent_factory=agent_factory).run(_request(**kwargs))


# ---------------------------------------------------------------------------
# The ledger itself — pure, no runner in the loop
# ---------------------------------------------------------------------------


def test_an_untripped_budget_still_accounts_its_headroom():
    """The gap this closes: "stopped at 6/6" used to look like "used 2/6"."""

    ledger = RunBudgetLedger()
    ledger.declare(
        RunBudgetKind.READ_SEARCH,
        enforcement=RunBudgetEnforcement.TRIPS_RUN,
        unit=UNIT_CALLS,
        limit=6,
        consumed=2,
    )

    block = ledger.accounting()

    assert block["bounded_by"] is None
    assert block["trip_reason"] is None
    assert block["tripped"] == []
    row = _rows(block)["read_search"]
    assert (row["limit"], row["consumed"], row["remaining"]) == (6, 2, 4)
    assert row["tripped"] is False
    assert row["enforcement"] == "trips_run"
    assert row["unit"] == UNIT_CALLS


def test_only_declared_budgets_appear():
    """A run with no wall budget must not report a wall bound it never had."""

    ledger = RunBudgetLedger()
    ledger.declare(
        RunBudgetKind.API_CALLS,
        enforcement=RunBudgetEnforcement.TRIPS_RUN,
        unit=UNIT_CALLS,
        limit=5,
        consumed=1,
    )

    assert list(_rows(ledger.accounting())) == ["api_calls"]


def test_a_live_counter_is_read_rather_than_copied():
    """One authority per number: the ledger never keeps a second tally."""

    meter = McpCallBudget(4)
    ledger = RunBudgetLedger()
    ledger.declare(
        RunBudgetKind.MCP_CALLS,
        enforcement=RunBudgetEnforcement.REFUSES_CALL,
        unit=UNIT_CALLS,
        limit=meter.limit,
        consumed_provider=lambda: meter.snapshot()["spent"],
    )

    assert _rows(ledger.accounting())["mcp_calls"]["consumed"] == 0
    meter.consume("launcher_qa", "get_buttons")
    meter.consume("launcher_qa", "run_actions")
    assert _rows(ledger.accounting())["mcp_calls"]["consumed"] == 2


def test_the_headline_is_the_most_terminal_bound_not_the_first():
    """A refused CALL did not bound the turn; the wall that killed it did.

    Ordering alone would answer "mcp_calls" here, which reads as "the turn
    stopped because of MCP" — the opposite of what happened.
    """

    ledger = RunBudgetLedger()
    ledger.trip(
        RunBudgetKind.MCP_CALLS,
        RunBudgetTripReason.MCP_CALLS_EXHAUSTED,
        enforcement=RunBudgetEnforcement.REFUSES_CALL,
    )
    ledger.trip(
        RunBudgetKind.WALL,
        RunBudgetTripReason.WALL_CLOCK_EXCEEDED,
        enforcement=RunBudgetEnforcement.TRIPS_RUN,
    )

    block = ledger.accounting()

    assert block["bounded_by"] == "wall"
    assert block["trip_reason"] == "wall_clock_exceeded"
    assert block["enforcement"] == "trips_run"
    # Both are still recorded: the refusal happened and is not erased.
    assert sorted(block["tripped"]) == ["mcp_calls", "wall"]


def test_first_trip_order_breaks_a_tie_between_equal_semantics():
    ledger = RunBudgetLedger()
    ledger.trip(
        RunBudgetKind.READ_SEARCH,
        RunBudgetTripReason.AGGREGATE_READ_SEARCH_EXCEEDED,
        enforcement=RunBudgetEnforcement.TRIPS_RUN,
    )
    ledger.trip(
        RunBudgetKind.API_CALLS,
        RunBudgetTripReason.API_CALLS_EXCEEDED,
        enforcement=RunBudgetEnforcement.TRIPS_RUN,
    )

    assert ledger.accounting()["bounded_by"] == "read_search"


def test_the_wall_escalates_from_landed_to_killed_and_never_back():
    """Both are true; the row must report the kill, not the checkpoint."""

    ledger = RunBudgetLedger()
    ledger.declare(
        RunBudgetKind.WALL,
        enforcement=RunBudgetEnforcement.LANDS_TURN,
        unit=UNIT_SECONDS,
        limit=540,
    )
    ledger.trip(
        RunBudgetKind.WALL,
        RunBudgetTripReason.WALL_CHECKPOINT_ENGAGED,
        consumed=459,
    )
    assert _rows(ledger.accounting())["wall"]["trip_reason"] == "wall_checkpoint_engaged"

    ledger.trip(
        RunBudgetKind.WALL,
        RunBudgetTripReason.WALL_CLOCK_EXCEEDED,
        consumed=540,
        enforcement=RunBudgetEnforcement.TRIPS_RUN,
    )
    row = _rows(ledger.accounting())["wall"]
    assert row["trip_reason"] == "wall_clock_exceeded"
    assert row["enforcement"] == "trips_run"

    # De-escalation is refused: a checkpoint recorded after a kill is noise.
    ledger.trip(
        RunBudgetKind.WALL,
        RunBudgetTripReason.WALL_CHECKPOINT_ENGAGED,
        enforcement=RunBudgetEnforcement.LANDS_TURN,
    )
    assert _rows(ledger.accounting())["wall"]["trip_reason"] == "wall_clock_exceeded"


def test_a_trip_is_never_lost_to_a_missing_declaration():
    ledger = RunBudgetLedger()

    ledger.trip(RunBudgetKind.TOTAL_TOKENS, RunBudgetTripReason.TOTAL_TOKENS_EXCEEDED)

    row = _rows(ledger.accounting())["total_tokens"]
    assert row["tripped"] is True
    assert row["unit"] == UNIT_TOKENS
    assert row["limit"] is None


def test_accounting_never_raises_on_a_hostile_counter():
    """Accounting that can fail a turn is worse than no accounting."""

    def _boom():
        raise RuntimeError("counter exploded")

    ledger = RunBudgetLedger()
    ledger.declare(
        RunBudgetKind.WALL,
        enforcement=RunBudgetEnforcement.LANDS_TURN,
        unit=UNIT_SECONDS,
        limit=float("inf"),
        consumed_provider=_boom,
    )

    row = _rows(ledger.accounting())["wall"]
    assert row["consumed"] is None
    assert row["limit"] is None  # inf is not a bound anyone can read
    assert row["remaining"] is None


def test_the_block_is_json_serializable():
    """It rides the run record and the mission-chat terminal frame."""

    ledger = RunBudgetLedger()
    ledger.declare(
        RunBudgetKind.WALL,
        enforcement=RunBudgetEnforcement.LANDS_TURN,
        unit=UNIT_SECONDS,
        limit=540.0,
        consumed=12.34,
    )
    ledger.trip(RunBudgetKind.WALL, RunBudgetTripReason.WALL_CHECKPOINT_ENGAGED)

    block = json.loads(json.dumps(ledger.accounting()))

    assert block["bounded_by"] == "wall"
    # Seconds round to a tenth; nobody needs microseconds in an audit block.
    assert _rows(block)["wall"]["consumed"] == 12.3


def test_concurrent_trips_do_not_corrupt_the_ledger():
    """The wall records from a timer thread, MCP from a dispatching one."""

    ledger = RunBudgetLedger()
    barrier = threading.Barrier(8)

    def _worker(index: int) -> None:
        barrier.wait()
        ledger.trip(
            RunBudgetKind.READ_SEARCH,
            RunBudgetTripReason.AGGREGATE_READ_SEARCH_EXCEEDED,
            consumed=index,
        )

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    block = ledger.accounting()
    assert block["tripped"] == ["read_search"]
    assert len(block["budgets"]) == 1


# ---------------------------------------------------------------------------
# Transition table — mechanism 1: the tool guard (TRIPS the run)
# ---------------------------------------------------------------------------


def test_read_search_untripped_completes_and_shows_headroom():
    result = _run(
        _read_search_agent(2),
        stop_on_repeated_read_search=True,
        tool_budget_limits={"read_search_limit": 4},
    )

    assert result.final_response == "ok"
    block = result.profile_timing["run_budget"]
    assert block["bounded_by"] is None
    row = _rows(block)["read_search"]
    assert (row["limit"], row["consumed"], row["tripped"]) == (4, 2, False)


def test_read_search_tripped_raises_the_same_exception_it_always_did():
    with pytest.raises(RunBudgetExceeded) as excinfo:
        _run(
            _read_search_agent(6),
            stop_on_repeated_read_search=True,
            tool_budget_limits={"read_search_limit": 3},
        )

    # Unchanged: type and message text.
    assert str(excinfo.value) == "aggregate read/search budget exceeded: 3/3"
    # New: the accounting survives the raised path, where no result exists.
    block = excinfo.value.run_budget
    assert block["bounded_by"] == "read_search"
    assert block["trip_reason"] == "aggregate_read_search_exceeded"
    assert block["enforcement"] == "trips_run"
    row = _rows(block)["read_search"]
    assert (row["limit"], row["consumed"], row["tripped"]) == (3, 3, True)


def test_the_repeated_tool_loop_reports_its_own_typed_reason():
    """Two counters, two reasons — not one blurred `read_search` verdict."""

    guard = _ToolBudgetGuard.from_limits(
        stop_on_repeated_read_search=True,
        tool_budget_limits={"read_search_limit": 2},
    )
    emit = _progress_adapter(lambda _payload: None, "run.progress", guard=guard)

    emit("tool.started", "read_file", None, {})
    with pytest.raises(RunBudgetExceeded) as excinfo:
        emit("tool.started", "read_file", None, {})

    assert str(excinfo.value) == "repeated read/search loop: read_file"
    assert excinfo.value.run_budget["trip_reason"] == "repeated_read_search_loop"
    assert guard.tripped_reason == "repeated read/search loop: read_file"


def test_a_run_that_does_not_stop_on_loops_declares_no_read_search_bound():
    result = _run(_read_search_agent(9), stop_on_repeated_read_search=False)

    assert "read_search" not in _rows(result.profile_timing["run_budget"])


# ---------------------------------------------------------------------------
# Transition table — mechanism 2: the wall checkpoint (LANDS the turn)
# ---------------------------------------------------------------------------


def test_wall_untripped_completes_and_shows_remaining_wall():
    result = _run(_Agent, max_wall_seconds=120.0)

    block = result.profile_timing["run_budget"]
    assert block["bounded_by"] is None
    row = _rows(block)["wall"]
    assert row["limit"] == 120
    assert row["unit"] == UNIT_SECONDS
    assert row["enforcement"] == "lands_turn"
    assert row["tripped"] is False
    assert 0 <= row["consumed"] < 30


def test_a_run_without_a_wall_budget_declares_no_wall_bound():
    """The runner builds a stand-in checkpoint; it is not a bound anyone has."""

    result = _run(_Agent)

    assert "wall" not in _rows(result.profile_timing["run_budget"])


def test_wall_checkpoint_engaging_lands_the_turn_exactly_as_before(monkeypatch):
    # Reserve is normally >= 30s away, so the checkpoint is unreachable in a
    # sub-second test. Replace the reserve FUNCTION (never its math) so the
    # graceful window opens 0.2s in, well clear of the 4.0s hard wall.
    monkeypatch.setattr(turn_budget, "checkpoint_reserve_seconds", lambda total: 3.8)

    result = _run(_sleeping_agent(0.6), max_wall_seconds=4.0)

    # Unchanged: the turn LANDS — no exception — and carries the typed
    # checkpoint provenance the caller settles `budget_exhausted` from.
    assert result.final_response == "ok"
    checkpoint = result.raw["wall_budget_checkpoint"]
    assert checkpoint["engaged"] is True
    assert checkpoint["trigger"] == turn_budget.CHECKPOINT_TRIGGER_TIMER
    # New: the same fact, in the one accounting block.
    block = result.profile_timing["run_budget"]
    assert block["bounded_by"] == "wall"
    assert block["trip_reason"] == "wall_checkpoint_engaged"
    assert block["enforcement"] == "lands_turn"
    assert _rows(block)["wall"]["tripped"] is True


def test_the_checkpoint_records_into_the_ledger_it_was_given():
    """Unit-level proof of the seam the runner-level test exercises live."""

    now = [1_000.0]
    ledger = RunBudgetLedger()
    checkpoint = WallBudgetCheckpoint(
        TurnWallBudget(total_seconds=540.0, deadline_epoch=1_540.0),
        clock=lambda: now[0],
        ledger=ledger,
    )
    checkpoint.bind(_Agent())

    assert checkpoint.gate() is True
    assert ledger.accounting()["tripped"] == []

    now[0] = 1_500.0  # 40s left of 540 — inside the 81s reserve
    assert checkpoint.gate() is False

    block = ledger.accounting()
    assert block["bounded_by"] == "wall"
    assert block["trip_reason"] == "wall_checkpoint_engaged"
    row = _rows(block)["wall"]
    assert row["consumed"] == 500
    assert row["detail"] == f"trigger={turn_budget.CHECKPOINT_TRIGGER_TOOL_GATE}"


# ---------------------------------------------------------------------------
# Transition table — mechanism 2b: the hard wall (TRIPS the run)
# ---------------------------------------------------------------------------


def test_wall_hard_kill_raises_with_its_unchanged_typed_projection():
    with pytest.raises(RunBudgetExceeded) as excinfo:
        _run(_sleeping_agent(0.8), max_wall_seconds=0.15)

    assert str(excinfo.value) == "live run budget exceeded: wall_seconds=0.15"
    # Unchanged: the wall projection the caller settles `budget_exhausted` from.
    assert excinfo.value.wall_budget["engaged"] is False
    # New: and the accounting says the hard wall did it, not the checkpoint.
    block = excinfo.value.run_budget
    assert block["bounded_by"] == "wall"
    assert block["trip_reason"] == "wall_clock_exceeded"
    assert block["enforcement"] == "trips_run"
    assert _rows(block)["wall"]["detail"] == "wall_seconds=0.15"


# ---------------------------------------------------------------------------
# Transition table — mechanism 3: the MCP call budget (REFUSES the call)
# ---------------------------------------------------------------------------


_MCP_TOOLS = ("get_buttons", "run_actions", "kill_launcher")


@pytest.fixture
def clean_registry():
    from tools.registry import registry

    yield registry
    for toolset in list(registry.get_registered_toolset_names() or []):
        if not str(toolset).startswith("mcp-"):
            continue
        for name in list(registry.get_tool_names_for_toolset(toolset) or []):
            registry.deregister(name)


class _StubTools:
    """A registered ``mcp-launcher_qa`` surface with plain handlers.

    The budget intercepts the registry DISPATCH, which is the same operation
    whether the handler talks to a transport or appends to a list.
    """

    def __init__(self, registry):
        self.registry = registry
        self.calls: list[str] = []

    def prefixed(self, tool: str) -> str:
        return f"mcp__launcher_qa__mcp_launcher_qa_{tool}"

    def register(self, _servers=None) -> list[str]:
        names = []
        for tool in _MCP_TOOLS:
            name = self.prefixed(tool)
            self.registry.register(
                name=name,
                toolset="mcp-launcher_qa",
                schema={"name": name, "description": tool, "parameters": {}},
                handler=self._make(name),
                is_async=False,
                description=tool,
            )
            names.append(name)
        return names

    def _make(self, name: str):
        def _handler(args, **kwargs):
            self.calls.append(name)
            return json.dumps({"ok": True, "tool": name})

        return _handler


def _mcp_admission(limit: int) -> McpAdmission:
    return McpAdmission(
        lane=LANE_MISSION_CHAT,
        role="qa",
        permission_mode="profile_default",
        enabled=True,
        requested=("launcher_qa",),
        server_names=("launcher_qa",),
        server_configs={"launcher_qa": {"command": "noop"}},
        max_tool_calls_per_run=limit,
    )


def _dispatching_agent(stub: _StubTools, tool: str, times: int):
    class _Dispatcher(_Agent):
        def run_conversation(self, user_message, system_message=None, task_id=None):
            for _ in range(times):
                stub.registry.dispatch(stub.prefixed(tool), {})
            return self._reply()

    return _Dispatcher


def test_mcp_untripped_dispatches_and_shows_headroom(monkeypatch, clean_registry):
    import agent_runtime.mcp_admission as mcp_admission

    stub = _StubTools(clean_registry)
    monkeypatch.setattr(mcp_admission, "_default_registrar", stub.register)

    result = _run(
        _dispatching_agent(stub, "get_buttons", 3),
        mcp_admission=_mcp_admission(limit=9),
    )

    assert len(stub.calls) == 3
    block = result.profile_timing["run_budget"]
    assert block["bounded_by"] is None
    row = _rows(block)["mcp_calls"]
    assert (row["limit"], row["consumed"], row["remaining"]) == (9, 3, 6)
    assert row["enforcement"] == "refuses_call"
    assert row["tripped"] is False
    # The pre-existing flat counters are untouched.
    assert result.profile_timing["mcp_calls_spent"] == 3
    assert result.profile_timing["mcp_calls_refused"] == 0


def test_mcp_tripped_refuses_the_call_and_still_lands_the_turn(monkeypatch, clean_registry):
    import agent_runtime.mcp_admission as mcp_admission

    stub = _StubTools(clean_registry)
    monkeypatch.setattr(mcp_admission, "_default_registrar", stub.register)
    events: list[dict] = []

    result = _run(
        _dispatching_agent(stub, "kill_launcher", 5),
        mcp_admission=_mcp_admission(limit=2),
        progress_callback=events.append,
    )

    # Unchanged: the turn LANDS, the underlying handler is never reached past
    # the bound, and the operator gets exactly one warning row.
    assert result.final_response == "ok"
    assert stub.calls == [stub.prefixed("kill_launcher")] * 2
    tripped = [e for e in events if e.get("step") == "mcp_admission_budget_exhausted"]
    assert len(tripped) == 1
    assert tripped[0]["mcp_admission"]["denied"][0]["code"] == MCP_ADMISSION_BUDGET_EXHAUSTED
    # New: the refusal is readable from the run record afterwards.
    block = result.profile_timing["run_budget"]
    assert block["bounded_by"] == "mcp_calls"
    assert block["trip_reason"] == "mcp_calls_exhausted"
    assert block["enforcement"] == "refuses_call"
    row = _rows(block)["mcp_calls"]
    assert (row["limit"], row["consumed"], row["remaining"]) == (2, 2, 0)
    assert row["detail"] == MCP_ADMISSION_BUDGET_EXHAUSTED


def test_the_refusal_envelope_is_unchanged(monkeypatch, clean_registry):
    """The model reads a normal tool refusal — the shape must not drift."""

    import agent_runtime.mcp_admission as mcp_admission

    stub = _StubTools(clean_registry)
    monkeypatch.setattr(mcp_admission, "_default_registrar", stub.register)
    refusals: list[dict] = []

    class _Capturing(_Agent):
        def run_conversation(self, user_message, system_message=None, task_id=None):
            clean_registry.dispatch(stub.prefixed("get_buttons"), {})
            refusals.append(
                json.loads(clean_registry.dispatch(stub.prefixed("kill_launcher"), {}))
            )
            return self._reply()

    _run(_Capturing, mcp_admission=_mcp_admission(limit=1))

    (refusal,) = refusals
    assert refusal["code"] == MCP_ADMISSION_BUDGET_EXHAUSTED
    assert refusal["tool"] == stub.prefixed("kill_launcher")
    assert refusal["error"] == refusal["summary"]
    assert refusal["budget"] == {
        "limit": 1,
        "spent": 1,
        "remaining": 0,
        "refused": 1,
        "exhausted": True,
        "per_server": {"launcher_qa": 1},
    }
    assert "do not retry" in refusal["fix_hint"]


def test_a_run_with_no_admission_declares_no_mcp_bound():
    assert "mcp_calls" not in _rows(_run(_Agent).profile_timing["run_budget"])


# ---------------------------------------------------------------------------
# Transition table — mechanism 4: the post-run result budgets (TRIP the run)
# ---------------------------------------------------------------------------


def test_result_budgets_untripped_account_their_headroom():
    result = _run(_Agent, max_api_calls=5, max_total_tokens=100)

    rows = _rows(result.profile_timing["run_budget"])
    assert (rows["api_calls"]["limit"], rows["api_calls"]["consumed"]) == (5, 1)
    assert rows["api_calls"]["tripped"] is False
    assert (rows["total_tokens"]["limit"], rows["total_tokens"]["consumed"]) == (100, 3)
    assert rows["total_tokens"]["unit"] == UNIT_TOKENS


def test_api_call_budget_tripped_raises_the_same_exception_it_always_did():
    class _Chatty(_Agent):
        def run_conversation(self, user_message, system_message=None, task_id=None):
            return self._reply(api_calls=9)

    with pytest.raises(RunBudgetExceeded) as excinfo:
        _run(_Chatty, max_api_calls=2, max_total_tokens=100)

    assert str(excinfo.value) == "live run budget exceeded: api_calls=9/2"
    assert excinfo.value.session_id == "session_run_budget"
    block = excinfo.value.run_budget
    assert block["bounded_by"] == "api_calls"
    assert block["trip_reason"] == "api_calls_exceeded"
    assert block["enforcement"] == "trips_run"
    rows = _rows(block)
    assert (rows["api_calls"]["limit"], rows["api_calls"]["consumed"]) == (2, 9)
    # The token budget is evaluated AFTER the api-call one and never reached,
    # so it is not in the block — the order the runner enforces in is preserved.
    assert "total_tokens" not in rows


def test_total_token_budget_tripped_raises_the_same_exception_it_always_did():
    class _Heavy(_Agent):
        def run_conversation(self, user_message, system_message=None, task_id=None):
            return self._reply(total_tokens=500)

    with pytest.raises(RunBudgetExceeded) as excinfo:
        _run(_Heavy, max_total_tokens=100)

    assert str(excinfo.value) == "live run budget exceeded: total_tokens=500/100"
    block = excinfo.value.run_budget
    assert block["bounded_by"] == "total_tokens"
    assert block["trip_reason"] == "total_tokens_exceeded"
    row = _rows(block)["total_tokens"]
    assert (row["limit"], row["consumed"], row["remaining"]) == (100, 500, 0)


# ---------------------------------------------------------------------------
# The block coexists with what profile_timing already carried
# ---------------------------------------------------------------------------


def test_the_existing_timing_keys_are_untouched():
    result = _run(_Agent)

    timing = result.profile_timing
    for key in (
        "runtime_resolve_ms",
        "agent_construct_ms",
        "conversation_call_ms",
        "result_normalize_ms",
        "budget_checks_ms",
    ):
        assert isinstance(timing[key], int)
    assert isinstance(timing["run_budget"], dict)
    assert result.raw["profile_timing"] == timing


# S34 retired the writerless ``AgentRun.llm`` field. The four tests that lived
# below this line exercised only its removed run-record accumulation. The live
# ``AgentRunResult.profile_timing`` accounting contract remains covered above.
