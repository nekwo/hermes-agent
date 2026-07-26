"""CLI wiring for the agent-visible capability account on a mission-chat turn.

The typed capability drops (``toolset_dropped_by_chat_lane_policy`` &c.) and the
terminal-envelope refusal classes were both computed and both typed by wave-2,
and neither reached the model: the agent could not see what this lane had taken
away, or why, or which key would restore it. The fix folds both into the runtime
situational HUD's capability block.

``persona_commands.py`` is an exec'd command part (``harness._load_command_parts``)
rather than an importable module, so — like the budget-payload guards next door —
these assertions read the exact source text that gets exec'd.

What is pinned here is the DELIVERY CONTRACT, because that is the part a later
edit can silently break: the account rides the volatile envelope tail (emitted
on every delivery) and the HUD dict (operator CONTEXT-peek parity), and never
the hashed body.
"""

import ast
from pathlib import Path


def _mission_chat_message_func() -> ast.FunctionDef:
    import hermes_cli.harness as harness

    path = Path(harness.__file__).with_name("harness_parts") / "persona_commands.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_cmd_mission_chat_message":
            return node
    raise AssertionError("_cmd_mission_chat_message not found in persona_commands")


def _calls_named(func: ast.FunctionDef, name: str) -> list[ast.Call]:
    calls = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        called = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
        if called == name:
            calls.append(node)
    return calls


def test_the_capability_account_reaches_both_the_agent_and_the_operator():
    """One resolve, two consumers — the ``turn_budget`` precedent exactly.

    ``capability=`` on ``situational_hud_for_instance`` is what makes the
    operator's CONTEXT peek show the SAME account the agent was told (parity by
    construction, never a later re-derivation); ``render_capability_block`` on
    the volatile tail is what makes the agent see it mid-turn.
    """

    func = _mission_chat_message_func()

    hud_kwargs: set[str] = set()
    for call in _calls_named(func, "situational_hud_for_instance"):
        hud_kwargs.update(kw.arg for kw in call.keywords if kw.arg)
    assert "capability" in hud_kwargs

    assert _calls_named(func, "capability_block_for_persona"), (
        "the capability account must be resolved on the mission-chat turn"
    )
    assert _calls_named(func, "render_capability_block"), (
        "the resolved account must be rendered for the agent"
    )


def test_the_capability_block_is_resolved_once_so_the_two_views_cannot_drift():
    """Resolving it twice would let the operator's recorded account and the
    agent's rendered line describe different turns — the exact drift the
    wall-budget slice's single ``resolve_turn_wall_budget`` call prevents."""

    func = _mission_chat_message_func()
    assert len(_calls_named(func, "capability_block_for_persona")) == 1


def test_the_capability_block_rides_the_volatile_tail_not_the_hashed_body():
    """The whole delivery contract in one assertion.

    ``render_situational_hud_block`` is the body the HUD revision hashes; a
    capability claim rendered into it would be cached behind an ``unchanged``
    delivery and dropped entirely by an ``unavailable`` one. It belongs on
    ``volatile_content``, beside the wall-budget and MCP-admission lines.
    """

    func = _mission_chat_message_func()

    # The rendered account is assigned into the volatile-lines list...
    volatile_assignments = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "volatile_lines"
            for target in node.targets
        )
    ]
    assert volatile_assignments, "volatile_lines assignment not found"
    rendered = ast.dump(volatile_assignments[0])
    assert "render_capability_block" in rendered

    # ...and never into the hashed body.
    for call in _calls_named(func, "render_situational_hud_block"):
        assert "render_capability_block" not in ast.dump(call)

    envelope_kwargs: set[str] = set()
    for call in _calls_named(func, "render_runtime_context_envelope"):
        envelope_kwargs.update(kw.arg for kw in call.keywords if kw.arg)
    assert "volatile_content" in envelope_kwargs


def test_the_mcp_admission_line_stays_a_separate_voice():
    """Deliberate non-merge, recorded as a test so nobody "tidies" it.

    MCP denials are resolved at a different lifecycle point (execution-time
    degradations reach the agent through ``agent.steer``, after this envelope is
    sealed) and are gated on the admission kill switch. Folding them into the
    capability block would give one fact two voices — which is how an agent
    learns to discount both.
    """

    func = _mission_chat_message_func()
    assert _calls_named(func, "mission_chat_admission_line")
    for call in _calls_named(func, "capability_block_for_persona"):
        assert "mission_chat_admission_line" not in ast.dump(call)
