"""CLI contract for a wall-budget-ended mission-chat turn.

The 2026-07-26 incident cost an operator a manual ``turn-resolve --action
abandon`` on BOTH ends of a relay plus a full re-brief, because a wall-budget
death was reported as ``chat_turn_outcome_unknown`` — the "I cannot prove what
the provider did" state. A budget death is the opposite: the harness knows
exactly why it stopped. These guards pin the resulting contract so a later edit
cannot quietly reintroduce the resolve instruction or drop the typed fields.

``persona_commands.py`` is an exec'd command part (``harness._load_command_parts``)
rather than an importable module, so — like the record-at-injection guard next
door — these assertions read the exact source text that gets exec'd.
"""

import ast
from pathlib import Path

import pytest

# Imperative resolve language — the copy that sent the 2026-07-26 operator to
# `turn-resolve --action abandon`. A budget-ended turn must never carry any of
# it. Mentioning the verb only to NEGATE it ("no turn-resolve is required") is
# the point of the fix, so the verb itself is checked separately below.
_RESOLVE_INSTRUCTIONS = (
    "resolve the exact",
    "resolve this turn",
    "action=abandon",
    "before resending",
)
_RESOLVE_VERBS = ("turn-resolve", "turn_resolve")
_NEGATIONS = ("no turn-resolve", "no turn_resolve", "needs no", "requires no", "without turn-resolve")


def _mission_chat_message_func() -> ast.FunctionDef:
    import hermes_cli.harness as harness

    path = Path(harness.__file__).with_name("harness_parts") / "persona_commands.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_cmd_mission_chat_message":
            return node
    raise AssertionError("_cmd_mission_chat_message not found in persona_commands")


def _literal_str_items(node: ast.Dict) -> dict[str, str]:
    """Constant ``str -> str`` pairs of a dict literal (others are ignored)."""

    items: dict[str, str] = {}
    for key, value in zip(node.keys, node.values):
        if (
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            items[key.value] = value.value
    return items


def _all_string_constants(node: ast.AST) -> list[str]:
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


def _budget_payload_dicts() -> list[ast.Dict]:
    """Every payload dict literal that types itself as a budget-ended turn."""

    found: list[ast.Dict] = []
    for node in ast.walk(_mission_chat_message_func()):
        if not isinstance(node, ast.Dict):
            continue
        items = _literal_str_items(node)
        if items.get("error_kind") == "chat_turn_budget_exhausted":
            found.append(node)
    return found


def test_budget_exhausted_payloads_exist():
    assert _budget_payload_dicts(), (
        "mission-chat no longer emits a typed chat_turn_budget_exhausted payload; "
        "a wall-budget death must never fall back to chat_turn_outcome_unknown"
    )


@pytest.mark.parametrize("field", ["execution_state", "error_kind"])
def test_budget_payloads_are_typed(field):
    for payload in _budget_payload_dicts():
        items = _literal_str_items(payload)
        assert items.get("execution_state") == "budget_exhausted"
        assert items.get("error_kind") == "chat_turn_budget_exhausted"
        assert field in items


def test_budget_payloads_never_tell_the_operator_to_turn_resolve():
    for payload in _budget_payload_dicts():
        for text in _all_string_constants(payload):
            lowered = text.lower()
            for phrase in _RESOLVE_INSTRUCTIONS:
                assert phrase not in lowered, (
                    "a budget-ended turn is settled: it must never route the "
                    f"operator to a resolution verb (found {phrase!r} in {text!r})"
                )
            if any(verb in lowered for verb in _RESOLVE_VERBS):
                assert any(negation in lowered for negation in _NEGATIONS), (
                    "a budget-ended payload may name turn-resolve only to say it "
                    f"is NOT needed (found an unnegated mention in {text!r})"
                )


def test_budget_payloads_declare_that_no_resolution_is_required():
    for payload in _budget_payload_dicts():
        keys = {
            key.value
            for key in payload.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        assert "turn_resolution_required" in keys
        assert "budget_exhausted" in keys
        assert "next_expected" in keys
        next_expected = _literal_str_items(payload).get("next_expected", "")
        assert "new client_message_id" in next_expected


def test_budget_settle_uses_the_typed_terminal_state_not_outcome_unknown():
    """The wall-budget branch must transition to ``budget_exhausted``."""

    func = _mission_chat_message_func()
    states: list[str] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
        if name != "transition_mission_chat_turn":
            continue
        for keyword in node.keywords:
            if keyword.arg == "state" and isinstance(keyword.value, ast.Constant):
                states.append(str(keyword.value.value))
    assert "budget_exhausted" in states
    # The ambiguous state stays for genuinely ambiguous outcomes — this slice
    # narrows when it is used, it does not delete it.
    assert "outcome_unknown" in states


def test_chat_turn_feeds_the_wall_budget_into_the_agent_visible_hud():
    """Budget visibility rides the SAME lane as roster/board/lane state.

    ``turn_budget=`` puts it on the resolved HUD dict (operator CONTEXT peek +
    observability parity); ``volatile_content=`` puts it on the envelope tail
    that is emitted even on an ``unchanged`` delivery, so the agent can never be
    shown a cached, stale countdown.
    """

    func = _mission_chat_message_func()
    hud_kwargs: set[str] = set()
    envelope_kwargs: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
        if name == "situational_hud_for_instance":
            hud_kwargs.update(kw.arg for kw in node.keywords if kw.arg)
        elif name == "render_runtime_context_envelope":
            envelope_kwargs.update(kw.arg for kw in node.keywords if kw.arg)
    assert "turn_budget" in hud_kwargs
    assert "volatile_content" in envelope_kwargs


def test_wall_budget_is_resolved_once_for_both_the_hud_and_the_enforcer():
    """One authority: the number the agent is told and the number the runner
    enforces come from the same ``resolve_turn_wall_budget`` object, so they can
    never drift (the pre-fix code recomputed the relay clamp inline)."""

    func = _mission_chat_message_func()
    resolve_calls = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and (
            node.func.id
            if isinstance(node.func, ast.Name)
            else getattr(node.func, "attr", None)
        )
        == "resolve_turn_wall_budget"
    ]
    assert len(resolve_calls) == 1
