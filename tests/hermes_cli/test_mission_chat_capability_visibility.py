"""The mission-chat CLI body stays COMPOSITION — the assembly lives in a unit.

What this file used to be
-------------------------
Before ``agent_runtime.mission_chat_turn_context``, the entire per-turn assembly
(wall budget, capability account, situational HUD, volatile tail) lived inside
``_cmd_mission_chat_message``. That function is in an ``exec``-loaded command
part (``harness._load_command_parts``), not an importable module, so the only
guards available were AST source-shape assertions: "the body calls
``capability_block_for_persona`` exactly once", "the result of
``render_capability_block`` appears in a list literal named ``volatile_lines``",
"``render_runtime_context_envelope`` is passed a ``volatile_content=`` kwarg".

Those assertions pinned the SHAPE of the code and said nothing about the BYTES
the agent receives. Every contract they were written to protect —

* the capability account reaches the agent AND the operator from ONE resolve,
* it rides the always-emitted volatile tail, never the hashed HUD body,
* the MCP admission line stays a SEPARATE voice from the capability block,

— is now asserted on the composed output in
``tests/agent_runtime/test_mission_chat_turn_context.py``, which is where a
contract about output belongs.

What survives here
------------------
Exactly one property, and it genuinely lives in this file's source shape: the
CLI body must remain composition. It calls the builder once and reads the built
context; it must not grow a second, parallel assembly of its own. That is a
statement about the CLI's source and cannot be made anywhere else.
"""

import ast
from pathlib import Path

import pytest

#: Names that resolve per-turn policy. The CLI body must reach these ONLY
#: through the builder — a direct call here is a second assembly, and two
#: assemblies is how the operator's recorded view and the agent's rendered view
#: start describing different turns.
_POLICY_CALLS = (
    "resolve_turn_wall_budget",
    "capability_block_for_persona",
    "render_capability_block",
    "situational_hud_for_instance",
    "render_situational_hud_block",
    "runtime_context_delivery",
    "situational_hud_revision",
    "render_runtime_context_envelope",
    "render_skill_preload_envelope",
    "skill_preload_revision",
    "skill_preload_delivery",
    "consume_skills_for_next_turn",
    "required_preload_skill_ids",
    "build_preloaded_skills_prompt",
    "load_workspace_agents_context",
    "mission_chat_admission_line",
)


# The mission-chat turn body was split on 2026-07-31: the PLAN phase kept the
# name ``_cmd_mission_chat_message`` (resolve, refuse, decide) and every durable
# write moved into ``_mission_chat_commit_turn``, the sole writer, which runs
# under the chat-root lease. What these guards pin lives in the writer; both
# halves are named so the assertion follows the code if the boundary moves
# again, rather than silently finding nothing and passing.
_TURN_BODY_FUNCTIONS = ("_mission_chat_commit_turn", "_cmd_mission_chat_message")


def _mission_chat_message_func() -> ast.FunctionDef:
    import hermes_cli.harness as harness

    path = Path(harness.__file__).with_name("harness_parts") / "persona_commands.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for name in _TURN_BODY_FUNCTIONS:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
    raise AssertionError(
        "the mission-chat turn body "
        f"({' / '.join(_TURN_BODY_FUNCTIONS)}) is not in persona_commands"
    )


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


def test_the_turn_context_is_built_exactly_once():
    """One builder call per turn. Two would consume the queued-skill list twice
    and resolve two different wall clocks for one turn."""

    func = _mission_chat_message_func()
    assert len(_calls_named(func, "build_mission_chat_turn_context")) == 1


@pytest.mark.parametrize("name", _POLICY_CALLS)
def test_the_cli_body_resolves_no_per_turn_policy_of_its_own(name):
    """The extraction's load-bearing invariant.

    Everything in ``_POLICY_CALLS`` is per-turn policy that now belongs to
    ``agent_runtime.mission_chat_turn_context``, where it is unit-testable. A
    call reappearing here means the assembly leaked back into the exec'd body —
    and with it, the AST-guards-only regime this refactor retired.
    """

    func = _mission_chat_message_func()
    assert not _calls_named(func, name), (
        f"{name}() is per-turn policy: call it through "
        "build_mission_chat_turn_context, not inside the exec'd CLI body "
        "(see agent_runtime/mission_chat_turn_context.py)"
    )


def test_the_envelope_the_turn_feeds_comes_from_the_built_context():
    """Composition, stated: the fed envelope is rendered off the ONE built
    context (which owns both the hashed body and the volatile tail), not
    re-assembled from parts the CLI gathered itself."""

    func = _mission_chat_message_func()
    calls = _calls_named(func, "runtime_context_envelope")
    assert calls, "the turn no longer renders its runtime-context envelope"
    for call in calls:
        assert isinstance(call.func, ast.Attribute)
        assert isinstance(call.func.value, ast.Name)
        assert call.func.value.id == "turn_context"
        assert {kw.arg for kw in call.keywords} == {"context_id"}
