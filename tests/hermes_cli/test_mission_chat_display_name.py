"""Send-path display-name guard for the mission-chat chokepoint.

DEFECT B (2026-07-18): ``_cmd_mission_chat_message`` passed the persona DEFAULT
display_name as ``open_chat(display_name=...)``, which applied unconditionally
and RENAMED an existing instance — the placement sibling ``personainst_qa_agent_2``
read "QA Agent" instead of "QA Agent (2)". The "(2)" is load-bearing (the
launcher conversational fold keys on persona+displayName), so the clobber folded
a sibling onto the primary's console channel.

The fix routes the persona default through ``open_chat(default_display_name=...)``,
which stamps only a first-ever holder and never renames. This AST guard pins that
wiring so a refactor cannot silently restore the authoritative-``display_name``
clobber on the send path.
"""

import ast
from pathlib import Path


def _persona_commands_source() -> str:
    # persona_commands.py is an exec'd command part (not importable); parse the
    # file text, which is exactly what harness exec's into its globals.
    import hermes_cli.harness as harness

    path = Path(harness.__file__).with_name("harness_parts") / "persona_commands.py"
    return path.read_text(encoding="utf-8")


def _func(name: str) -> ast.FunctionDef:
    tree = ast.parse(_persona_commands_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in persona_commands")


def _open_chat_calls(func: ast.FunctionDef) -> list[ast.Call]:
    calls = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
        if name == "open_chat":
            calls.append(node)
    return calls


def test_send_path_open_chat_passes_default_display_name_not_authoritative():
    # ``open_chat`` is a durable write, so it moved into the sole writer when
    # the turn body was split (2026-07-31).
    calls = _open_chat_calls(_func("_mission_chat_commit_turn"))
    assert calls, "the mission-chat send path no longer binds a chat session via open_chat"
    for call in calls:
        keywords = {kw.arg for kw in call.keywords}
        assert "default_display_name" in keywords, (
            "the send path must pass the persona default as default_display_name "
            "(stamp-if-empty), never as the authoritative display_name — else it "
            "renames an existing placement instance ('QA Agent (2)' -> 'QA Agent')"
        )
        assert "display_name" not in keywords, (
            "the send path must NOT pass an authoritative display_name into "
            "open_chat; that unconditionally clobbers the instance's name"
        )
