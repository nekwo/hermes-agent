"""Record-at-injection guard for the mission-chat chokepoint.

The 2026-07-16 "CONTEXT peek shows Nothing injected" incident: the chat turn
rendered a situational HUD into the model prompt but persisted its
observability row WITHOUT `situational_hud=`, leaving the record empty and the
peek dependent on a snapshot backfill whose (persona_instance_id, session_id,
persona_id) key never matches console chats. The fix records the injected dict
at the injection site; this AST guard pins that wiring so the kwarg cannot be
silently dropped again.
"""

import ast
from pathlib import Path


def _mission_chat_message_func():
    # persona_commands.py is an exec'd command part (harness._load_command_parts),
    # not an importable module — its names (e.g. PersonaChatBusyError) live in
    # hermes_cli.harness's globals. Parse the file source, which is exactly the
    # text that gets exec'd.
    import hermes_cli.harness as harness

    # Split on 2026-07-31: record-at-injection happens on the write side, which
    # is now ``_mission_chat_commit_turn`` (the sole writer, under the lease).
    # Both halves are searched so this guard follows the code if the boundary
    # moves again instead of silently finding nothing.
    path = Path(harness.__file__).with_name("harness_parts") / "persona_commands.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for name in ("_mission_chat_commit_turn", "_cmd_mission_chat_message"):
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
    raise AssertionError("the mission-chat turn body is not in persona_commands")


def _observability_calls(func: ast.FunctionDef) -> list[ast.Call]:
    calls = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
        if name == "mission_chat_prompt_observability":
            calls.append(node)
    return calls


def test_chat_turn_records_situational_hud_on_its_observability_row():
    calls = _observability_calls(_mission_chat_message_func())
    assert calls, "mission-chat turn no longer builds an observability row"
    for call in calls:
        keywords = {kw.arg for kw in call.keywords}
        assert "situational_hud" in keywords, (
            "mission-chat must record the injected situational HUD dict on the "
            "observability row it persists (record-at-injection) — the CONTEXT "
            "peek renders this row verbatim"
        )


def test_chat_turn_renders_the_same_dict_it_records():
    # The fed block and the recorded row must come from ONE resolved object.
    # Since the assembly moved into `agent_runtime.mission_chat_turn_context`,
    # that object is `turn_context`: the observability row records
    # `turn_context.situational_hud`, and the fed envelope is rendered by
    # `turn_context.runtime_context_envelope(...)` off the SAME instance. This
    # guard keeps a refactor from re-deriving either half at record time.
    #
    # (That the rendered body really IS the recorded dict is asserted on the
    # output in tests/agent_runtime/test_mission_chat_turn_context.py ::
    # test_the_body_the_envelope_carries_is_the_rendered_stable_hud — an
    # assertion the exec'd CLI body could never support.)
    func = _mission_chat_message_func()

    recorded = set()
    for call in _observability_calls(func):
        for keyword in call.keywords:
            if keyword.arg in {
                "situational_hud",
                "situational_hud_revision",
                "situational_hud_delivery",
            }:
                value = keyword.value
                assert isinstance(value, ast.Attribute), keyword.arg
                assert isinstance(value.value, ast.Name)
                assert value.value.id == "turn_context"
                recorded.add(keyword.arg)
    assert recorded == {
        "situational_hud",
        "situational_hud_revision",
        "situational_hud_delivery",
    }

    envelope_calls = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "runtime_context_envelope"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "turn_context"
    ]
    assert envelope_calls, "the fed envelope no longer comes from the built context"
