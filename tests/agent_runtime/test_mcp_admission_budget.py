"""Selective MCP admission — the per-run call budget (design §3 / §7).

R1 admitted servers, R2 gave the scope back at the end of the run. Neither
bounded how many times an admitted agent may CALL what it was admitted. The
existing controls answer a different question: single-flight bounds how many
admissions may be in flight, the wall budget and the AS0 liveness watchdog bound
the turn's clock. A model that loops ``kill_launcher`` for twelve minutes
violates none of them.

This file pins the bound that closes that gap:

1. **The meter counts admitted MCP calls and only those.** One dispatch = one
   call, including the batched ``run_actions`` multiplexer; a non-admitted tool
   never touches the counter.
2. **Exhaustion refuses the call, not the turn.** The refused tool returns the
   typed ``mcp_admission_budget_exhausted`` row (same shape family as
   ``mcp_not_admitted_for_role``, with a real ``fix_hint``), the underlying MCP
   handler is never reached, and the agent keeps every non-MCP tool so it can
   still land a reply.
3. **The budget is per RUN.** A new admission mints a new meter — there is no
   reset path to forget to call.
4. **The default is generous enough for the live drills** and cannot be
   configured away.

No test here spawns a real MCP server or connects a transport.
"""

from __future__ import annotations

import json
import threading
import types

import pytest

from agent_runtime.mcp_admission import (
    LANE_MISSION_CHAT,
    MCP_ADMISSION_BUDGET_EXHAUSTED,
    MCP_NOT_REGISTERED_ON_LANE,
    McpAdmission,
    McpAdmissionOutcome,
    McpCallBudget,
    admit_mcp_servers,
    resolve_mcp_admission,
    teardown_mcp_admission,
)
from tests.agent_runtime.persona_samples import sample_personas
from agent_runtime.runtime_config import McpAdmissionConfig

_QA_ALLOW = {"qa": {LANE_MISSION_CHAT: ["launcher_qa"]}}
_TOOLSET = "mcp-launcher_qa"

#: A representative admitted drill: the 6–9 action Stage C QA drill plus one
#: batched ``run_actions`` call. The budget must fit this many times over.
_DRILL_CALLS = 10


# ── fixtures / helpers ──────────────────────────────────────────────────────


def _persona(persona_id: str):
    return {persona.id: persona for persona in sample_personas()}[persona_id]


def _cfg(**kwargs) -> types.SimpleNamespace:
    return types.SimpleNamespace(mcp_admission=McpAdmissionConfig(**kwargs))


@pytest.fixture
def clean_registry():
    """Leave the process-global tool registry exactly as we found it."""

    from tools.registry import registry

    yield registry
    for toolset in list(registry.get_registered_toolset_names() or []):
        if not str(toolset).startswith("mcp-"):
            continue
        for name in list(registry.get_tool_names_for_toolset(toolset) or []):
            registry.deregister(name)


def _admission(*, limit: int = 3, servers: tuple[str, ...] = ("launcher_qa",)) -> McpAdmission:
    return McpAdmission(
        lane=LANE_MISSION_CHAT,
        role="qa",
        permission_mode="profile_default",
        enabled=True,
        requested=servers,
        server_names=servers,
        server_configs={name: {"command": "noop"} for name in servers},
        max_tool_calls_per_run=limit,
    )


class _StubTools:
    """Register plain handlers under an ``mcp-<server>`` toolset, and count calls.

    Stands in for a connected MCP server's registered surface: the budget's job
    is to intercept the registry DISPATCH, which is the same operation whether
    the handler talks to a real transport or appends to a list.
    """

    def __init__(self, registry, server: str = "launcher_qa", tools: tuple[str, ...] = ()):
        self.registry = registry
        self.server = server
        self.tools = tools or ("get_buttons", "run_actions", "kill_launcher")
        self.calls: list[str] = []

    def prefixed(self, tool: str) -> str:
        return f"mcp__{self.server}__mcp_{self.server}_{tool}"

    def register(self, _servers=None) -> list[str]:
        names = []
        for tool in self.tools:
            name = self.prefixed(tool)
            self.registry.register(
                name=name,
                toolset=f"mcp-{self.server}",
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


def _refusal(result: str) -> dict:
    payload = json.loads(result)
    assert payload["code"] == MCP_ADMISSION_BUDGET_EXHAUSTED
    return payload


# ── the meter itself (pure) ─────────────────────────────────────────────────


def test_each_admitted_call_decrements_the_budget():
    budget = McpCallBudget(3)

    assert budget.remaining == 3
    assert budget.consume("launcher_qa", "get_buttons") is None
    assert (budget.spent, budget.remaining) == (1, 2)
    assert budget.consume("launcher_qa", "get_buttons") is None
    assert (budget.spent, budget.remaining) == (2, 1)
    assert not budget.exhausted


def test_a_batched_run_actions_call_costs_exactly_one():
    """§7 says nothing else, and the seam counts DISPATCHES.

    ``run_actions`` executes an ordered list of other verbs in one call. Charging
    it per inner action would mean parsing a payload hermes does not own, and
    would tax the batched lane the QA drills are supposed to prefer.
    """

    budget = McpCallBudget(10)

    assert budget.consume("launcher_qa", "run_actions") is None

    assert budget.spent == 1


def test_exhaustion_returns_the_typed_row_with_a_fix_hint():
    budget = McpCallBudget(1)

    assert budget.consume("launcher_qa", "screenshot_window") is None
    denial = budget.consume("launcher_qa", "kill_launcher")

    assert denial is not None
    row = denial.row()
    assert row["code"] == MCP_ADMISSION_BUDGET_EXHAUSTED
    assert row["server"] == "launcher_qa"
    assert "kill_launcher" in row["summary"]
    assert row["fix_hint"]
    # The same row shape family as every other admission denial, so operator
    # surfaces that render mcp_not_admitted_for_role need no new case.
    assert set(row) == {"code", "server", "summary", "fix_hint"}


def test_the_fix_hint_forbids_the_workarounds_and_names_the_operator_knob():
    denial = McpCallBudget(1)._denial("launcher_qa", "kill_launcher")

    hint = denial.fix_hint.lower()
    assert "do not retry" in hint
    assert "powershell" in hint
    assert "max_tool_calls_per_run" in hint


def test_a_refused_call_does_not_spend_budget_it_does_not_have():
    budget = McpCallBudget(1)

    budget.consume("launcher_qa", "a")
    budget.consume("launcher_qa", "b")
    budget.consume("launcher_qa", "c")

    snapshot = budget.snapshot()
    assert snapshot["spent"] == 1
    assert snapshot["refused"] == 2
    assert snapshot["remaining"] == 0
    assert snapshot["exhausted"] is True


def test_the_budget_is_a_run_total_across_servers_not_a_per_server_allowance():
    """The conservative reading of the two the design contains.

    §3 says "per admitted server per run", §7 says "the per-run call budget".
    With one admissible server they are identical; with two, the per-server
    reading silently authorises 2x the calls. The bound may only surprise an
    operator downward.
    """

    budget = McpCallBudget(2)

    assert budget.consume("launcher_qa", "a") is None
    assert budget.consume("other_server", "b") is None
    denial = budget.consume("other_server", "c")

    assert denial is not None
    assert budget.snapshot()["per_server"] == {"launcher_qa": 1, "other_server": 1}


def test_the_meter_cannot_be_raced_past_its_limit():
    """A bound that N threads can all read as "one left" is not a bound."""

    budget = McpCallBudget(50)
    outcomes: list[bool] = []
    lock = threading.Lock()

    def _spend():
        for _ in range(20):
            allowed = budget.consume("launcher_qa", "get_buttons") is None
            with lock:
                outcomes.append(allowed)

    threads = [threading.Thread(target=_spend) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(outcomes) == 50
    assert budget.spent == 50
    assert budget.refused == len(outcomes) - 50


# ── the counting seam: dispatch through the registry ────────────────────────


def test_the_budget_is_installed_over_every_admitted_tool(clean_registry):
    stub = _StubTools(clean_registry)

    outcome = admit_mcp_servers(_admission(limit=5), register=stub.register)

    assert outcome.admitted == ("launcher_qa",)
    assert outcome.call_budget is not None
    assert outcome.call_budget.limit == 5
    for tool in stub.tools:
        entry = clean_registry.get_entry(stub.prefixed(tool))
        assert hasattr(entry.handler, "_mcp_admission_unmetered_handler")


def test_dispatch_spends_the_budget_and_still_reaches_the_real_handler(clean_registry):
    stub = _StubTools(clean_registry)
    outcome = admit_mcp_servers(_admission(limit=3), register=stub.register)

    result = clean_registry.dispatch(stub.prefixed("get_buttons"), {})

    assert json.loads(result)["ok"] is True
    assert stub.calls == [stub.prefixed("get_buttons")]
    assert outcome.call_budget.spent == 1


def test_the_call_past_the_budget_is_refused_and_never_dispatched(clean_registry):
    """§7: "no further dispatch". The refusal is not a post-hoc complaint."""

    stub = _StubTools(clean_registry)
    outcome = admit_mcp_servers(_admission(limit=2), register=stub.register)

    clean_registry.dispatch(stub.prefixed("get_buttons"), {})
    clean_registry.dispatch(stub.prefixed("run_actions"), {})
    refused = clean_registry.dispatch(stub.prefixed("kill_launcher"), {})

    payload = _refusal(refused)
    assert payload["tool"] == stub.prefixed("kill_launcher")
    assert payload["budget"]["limit"] == 2
    assert payload["error"] == payload["summary"]
    # The dangerous call never reached the server.
    assert stub.calls == [stub.prefixed("get_buttons"), stub.prefixed("run_actions")]
    assert outcome.call_budget.refused == 1


def test_the_budget_is_shared_across_the_admitted_tools_of_a_run(clean_registry):
    stub = _StubTools(clean_registry)
    admit_mcp_servers(_admission(limit=2), register=stub.register)

    clean_registry.dispatch(stub.prefixed("get_buttons"), {})
    clean_registry.dispatch(stub.prefixed("run_actions"), {})

    assert _refusal(clean_registry.dispatch(stub.prefixed("get_buttons"), {}))


def test_a_non_admitted_tool_never_touches_the_budget(clean_registry):
    """Only the admitted MCP surface is metered — nothing else is even wrapped."""

    from tools.registry import registry

    calls: list[str] = []

    def _plain(args, **kwargs):
        calls.append("plain")
        return json.dumps({"ok": True})

    registry.register(
        name="mcp__other_server__mcp_other_server_get_state",
        toolset="mcp-other_server",
        schema={"name": "x", "description": "", "parameters": {}},
        handler=_plain,
        is_async=False,
        description="",
    )
    stub = _StubTools(clean_registry)
    outcome = admit_mcp_servers(_admission(limit=1), register=stub.register)

    for _ in range(5):
        registry.dispatch("mcp__other_server__mcp_other_server_get_state", {})

    assert calls == ["plain"] * 5
    assert outcome.call_budget.spent == 0
    # ... and the admitted tool still has its full budget.
    assert json.loads(clean_registry.dispatch(stub.prefixed("get_buttons"), {}))["ok"] is True


def test_the_turn_keeps_running_after_the_budget_trips(clean_registry):
    """Exhaustion refuses the CALL, never the turn.

    An agent that loses the launcher can still write up what it captured, which
    is a strictly better outcome than a turn that dies at the bound.
    """

    stub = _StubTools(clean_registry)
    admit_mcp_servers(_admission(limit=1), register=stub.register)

    clean_registry.dispatch(stub.prefixed("get_buttons"), {})
    refused = clean_registry.dispatch(stub.prefixed("kill_launcher"), {})

    # A tool RESULT, not an exception: the agent loop keeps going.
    assert isinstance(refused, str)
    _refusal(refused)
    assert clean_registry.get_entry(stub.prefixed("get_buttons")) is not None


def test_the_first_refusal_notifies_the_operator_exactly_once(clean_registry):
    stub = _StubTools(clean_registry)
    seen: list[tuple[str, dict]] = []

    admit_mcp_servers(
        _admission(limit=1),
        register=stub.register,
        on_budget_exhausted=lambda denial, snapshot: seen.append((denial.code, snapshot)),
    )

    clean_registry.dispatch(stub.prefixed("get_buttons"), {})
    for _ in range(3):
        clean_registry.dispatch(stub.prefixed("kill_launcher"), {})

    assert [code for code, _ in seen] == [MCP_ADMISSION_BUDGET_EXHAUSTED]
    assert seen[0][1]["refused"] == 1


def test_a_notification_that_raises_never_fails_the_tool_call(clean_registry):
    stub = _StubTools(clean_registry)

    def _boom(denial, snapshot):
        raise RuntimeError("the operator surface blew up")

    admit_mcp_servers(_admission(limit=1), register=stub.register, on_budget_exhausted=_boom)

    clean_registry.dispatch(stub.prefixed("get_buttons"), {})

    assert _refusal(clean_registry.dispatch(stub.prefixed("kill_launcher"), {}))


# ── per-run scope: a new admission is a new budget ──────────────────────────


def test_the_budget_resets_on_the_next_run(clean_registry):
    stub = _StubTools(clean_registry)
    first = admit_mcp_servers(_admission(limit=1), register=stub.register)
    clean_registry.dispatch(stub.prefixed("get_buttons"), {})
    assert first.call_budget.exhausted

    teardown_mcp_admission(("launcher_qa",))
    second = admit_mcp_servers(_admission(limit=1), register=stub.register)

    assert second.call_budget is not first.call_budget
    assert second.call_budget.spent == 0
    assert json.loads(clean_registry.dispatch(stub.prefixed("get_buttons"), {}))["ok"] is True
    assert second.call_budget.spent == 1
    assert first.call_budget.spent == 1  # the previous run's accounting is untouched


def test_teardown_removes_the_meter_with_the_scope(clean_registry):
    stub = _StubTools(clean_registry)
    admit_mcp_servers(_admission(limit=2), register=stub.register)

    teardown_mcp_admission(("launcher_qa",))

    assert clean_registry.get_tool_names_for_toolset(_TOOLSET) == []


def test_a_re_admission_after_a_failed_teardown_does_not_stack_meters(clean_registry):
    """The scope survived (teardown failed); the budget must not be double-charged."""

    stub = _StubTools(clean_registry)
    admit_mcp_servers(_admission(limit=5), register=stub.register)
    # No teardown: re-admit straight over the live, already-metered scope. The
    # registrar is a no-op here because the tools are still registered.
    second = admit_mcp_servers(_admission(limit=5), register=lambda servers: [])

    clean_registry.dispatch(stub.prefixed("get_buttons"), {})

    assert second.call_budget.spent == 1
    assert stub.calls == [stub.prefixed("get_buttons")]


# ── failing closed ──────────────────────────────────────────────────────────


def test_a_tool_that_cannot_be_metered_is_removed_rather_than_left_unbounded(clean_registry):
    """An admitted tool with no bound is the exposure the budget exists to close.

    ``is_async`` is the concrete case: the refusal path returns a string, which an
    async entry's dispatch would try to await. Upstream registers every MCP tool
    with ``is_async=False`` (pinned below), so this is drift insurance, and the
    conservative answer to drift is to drop the tool.
    """

    from tools.registry import registry

    def _handler(args, **kwargs):
        return json.dumps({"ok": True})

    name = "mcp__launcher_qa__mcp_launcher_qa_async_tool"
    registry.register(
        name=name,
        toolset=_TOOLSET,
        schema={"name": name, "description": "", "parameters": {}},
        handler=_handler,
        is_async=True,
        description="",
    )

    outcome = admit_mcp_servers(_admission(limit=5), register=lambda servers: [name])

    assert registry.get_entry(name) is None
    # Nothing left in the scope ⇒ the existing typed row, not a silent success.
    assert outcome.admitted == ()
    assert [denial.code for denial in outcome.execution_denied] == [MCP_NOT_REGISTERED_ON_LANE]


def test_an_admission_that_registers_nothing_still_mints_no_unmetered_surface(clean_registry):
    outcome = admit_mcp_servers(_admission(limit=5), register=lambda servers: [])

    assert outcome.admitted == ()
    assert outcome.call_budget is not None
    assert outcome.call_budget.spent == 0


# ── the upstream contract the seam depends on (drift guard) ─────────────────


def test_the_dispatch_seam_the_budget_intercepts_still_exists():
    """``registry.dispatch`` must read ``entry.handler`` PER CALL.

    That is the whole reason the meter can be installed without an upstream edit
    or a parallel dispatch path. If upstream ever snapshots handlers at
    definition time, this test fails instead of the bound silently evaporating.
    """

    import inspect

    from tools.registry import ToolEntry, ToolRegistry

    source = inspect.getsource(ToolRegistry.dispatch)
    assert "entry.handler(args, **kwargs)" in source
    assert "handler" in ToolEntry.__slots__


def test_upstream_registers_mcp_tools_synchronously():
    """The meter's refusal path returns a string, which requires a sync entry."""

    import inspect

    import tools.mcp_tool as mcp_tool

    source = inspect.getsource(mcp_tool._register_server_tools)
    assert "is_async=False" in source
    assert "is_async=True" not in source


# ── config: the bound, and the fact it cannot be configured away ────────────


def test_the_default_budget_fits_the_live_drills_many_times_over():
    """Generous by construction: it is a LOOP bound, not a work bound."""

    limit = McpAdmissionConfig().max_tool_calls_per_run
    budget = McpCallBudget(limit)

    # a 6-9 action drill plus one batched run_actions call, six times over —
    # the whole Stage C acceptance matrix — and the budget is still not spent.
    for _ in range(6 * _DRILL_CALLS):
        assert budget.consume("launcher_qa", "get_buttons") is None
    assert not budget.exhausted
    assert limit >= 6 * _DRILL_CALLS * 2


def test_the_parsed_config_defaults_to_the_generous_bound():
    from agent_runtime.config import _mcp_admission_config

    assert _mcp_admission_config({}).max_tool_calls_per_run == 120


@pytest.mark.parametrize("value", [0, -1, "", "abc", None, True, 10**9])
def test_no_config_value_can_retire_the_bound(value):
    from agent_runtime.config import MCP_ADMISSION_MAX_TOOL_CALLS_CEILING, _mcp_admission_config

    parsed = _mcp_admission_config({"max_tool_calls_per_run": value})

    assert 1 <= parsed.max_tool_calls_per_run <= MCP_ADMISSION_MAX_TOOL_CALLS_CEILING


def test_an_operator_can_raise_or_lower_the_bound():
    from agent_runtime.config import _mcp_admission_config

    assert _mcp_admission_config({"max_tool_calls_per_run": 40}).max_tool_calls_per_run == 40
    assert _mcp_admission_config({"max_tool_calls_per_run": 400}).max_tool_calls_per_run == 400


def test_the_resolved_admission_carries_the_configured_bound(monkeypatch, tmp_path):
    from agent_runtime import profile_context, profile_readiness
    from agent_runtime.profile_context import PersonaProfileBinding

    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        "mcp_servers:\n  launcher_qa:\n    command: noop\n", encoding="utf-8"
    )
    binding = lambda persona: PersonaProfileBinding(  # noqa: E731 - one-line stub
        persona_id=persona.id,
        hermes_profile="launcher-qa",
        profile_home=home,
        readiness="ready",
        summary="profile exists",
    )
    monkeypatch.setattr(profile_readiness, "resolve_persona_profile", binding)
    monkeypatch.setattr(profile_context, "resolve_persona_profile", binding)

    admission = resolve_mcp_admission(
        _persona("qa"),
        lane=LANE_MISSION_CHAT,
        cfg=_cfg(enabled=True, roles=_QA_ALLOW, max_tool_calls_per_run=42),
    )

    assert admission.server_names == ("launcher_qa",)
    assert admission.max_tool_calls_per_run == 42
    # The operator can read the bound BEFORE flipping the flag.
    assert admission.explain()["max_tool_calls_per_run"] == 42


def test_the_bound_is_reported_even_when_nothing_is_admitted():
    """"How many MCP calls could this persona make" precedes "should I admit it"."""

    admission = resolve_mcp_admission(
        _persona("dev"), lane=LANE_MISSION_CHAT, cfg=_cfg(max_tool_calls_per_run=77)
    )

    assert admission.is_empty
    assert admission.explain()["max_tool_calls_per_run"] == 77


# ── the runner's wiring ─────────────────────────────────────────────────────


class _Agent:
    def __init__(self, **kwargs):
        self.session_id = kwargs.get("session_id") or "session_budget"
        self.provider = kwargs.get("provider")
        self.model = kwargs.get("model")
        self.base_url = None
        self.tools = []

    def run_conversation(self, user_message, system_message=None, task_id=None):
        return {
            "final_response": "ok",
            "session_id": self.session_id,
            "messages": [],
            "api_calls": 1,
            "total_tokens": 1,
        }


def _request(**kwargs):
    from agent_runtime.profile_runner import AgentRunRequest

    return AgentRunRequest(
        profile=None, user_message="hi", mcp_admission=_admission(limit=9), **kwargs
    )


def test_the_runner_records_the_run_s_call_accounting(monkeypatch):
    """The meter dies with the scope, so end-of-run is the last honest read."""

    import agent_runtime.mcp_admission as mcp_admission
    import agent_runtime.profile_runner as profile_runner
    from agent_runtime.mcp_admission import McpTeardownOutcome
    from agent_runtime.profile_runner import ProfileAgentRunner

    budget = McpCallBudget(9)
    budget.consume("launcher_qa", "get_buttons")
    budget.consume("launcher_qa", "run_actions")
    for _ in range(9):
        budget.consume("launcher_qa", "kill_launcher")

    monkeypatch.setattr(
        mcp_admission,
        "teardown_mcp_admission",
        lambda servers, **kwargs: McpTeardownOutcome(servers=tuple(servers)),
    )
    monkeypatch.setattr(
        profile_runner.ProfileAgentRunner,
        "_admit_mcp_servers",
        lambda self, request, timing, **_kwargs: McpAdmissionOutcome(
            attempted=True, admitted=("launcher_qa",), call_budget=budget
        ),
    )

    result = ProfileAgentRunner(agent_factory=_Agent).run(_request())

    assert result.profile_timing["mcp_calls_spent"] == 9
    assert result.profile_timing["mcp_calls_refused"] == 2


def test_the_runner_reports_the_bound_it_armed(monkeypatch, clean_registry):
    from agent_runtime.profile_runner import ProfileAgentRunner

    stub = _StubTools(clean_registry)
    import agent_runtime.mcp_admission as mcp_admission

    monkeypatch.setattr(mcp_admission, "_default_registrar", stub.register)

    result = ProfileAgentRunner(agent_factory=_Agent).run(_request())

    assert result.profile_timing["mcp_call_budget"] == 9
    assert result.profile_timing["mcp_admitted_servers"] == 1


def test_the_runner_emits_one_operator_row_when_the_budget_trips(monkeypatch, clean_registry):
    import agent_runtime.mcp_admission as mcp_admission
    from agent_runtime.profile_runner import ProfileAgentRunner

    stub = _StubTools(clean_registry)
    monkeypatch.setattr(mcp_admission, "_default_registrar", stub.register)
    events: list[dict] = []

    class _DispatchingAgent(_Agent):
        def run_conversation(self, user_message, system_message=None, task_id=None):
            for _ in range(4):
                clean_registry.dispatch(stub.prefixed("kill_launcher"), {})
            return super().run_conversation(user_message, system_message, task_id)

    request = _request(progress_callback=events.append)
    request.mcp_admission = _admission(limit=2)

    ProfileAgentRunner(agent_factory=_DispatchingAgent).run(request)

    tripped = [
        event for event in events if event.get("step") == "mcp_admission_budget_exhausted"
    ]
    assert len(tripped) == 1
    assert tripped[0]["severity"] == "warning"
    assert tripped[0]["mcp_call_budget"]["limit"] == 2
    assert tripped[0]["mcp_admission"]["denied"][0]["code"] == MCP_ADMISSION_BUDGET_EXHAUSTED
    assert stub.calls == [stub.prefixed("kill_launcher")] * 2
