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
    # not an importable module — its names (e.g. ChatBusyError) live in
    # hermes_cli.harness's globals. Parse the file source, which is exactly the
    # text that gets exec'd.
    import hermes_cli.harness as harness

    path = Path(harness.__file__).with_name("harness_parts") / "persona_commands.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_cmd_mission_chat_message":
            return node
    raise AssertionError("_cmd_mission_chat_message not found in persona_commands")


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
    # The fed block and the recorded row must come from ONE resolved dict:
    # `situational_hud_for_instance(...)` then `render_situational_hud_block(...)`
    # over the same object. Guarding the import keeps a refactor from quietly
    # reintroducing a second authority (e.g. re-deriving at record time).
    func = _mission_chat_message_func()
    called = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            callee = node.func
            name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
            if name:
                called.add(name)
    assert "situational_hud_for_instance" in called
    assert "render_situational_hud_block" in called
