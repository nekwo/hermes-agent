"""One-lane-one-usage-writer guard for persona-chat token accounting.

The 2026-07-24 "in 20,208 for a bare hi" incident: the native-continuity chat
lane (`_cmd_mission_chat_message`) runs the agent bound to the real chat
session (``mission_chat_reply(session_id=active_session_id)`` with
``persist_agent_session=True``), so ``conversation_loop`` / ``codex_runtime``
already persist every API call's usage onto the bound SessionDB row. The lane
ALSO kept the pre-continuity post-turn writer
(``_update_persona_chat_token_counts``), whose docstring premise — "the runtime
uses a throwaway scratch session" — went stale when continuity landed
(c60413e17, 2026-07-20). Result: every counter (input/output/cache/reasoning/
api_call_count) accumulated at exactly 2x on every mission-chat turn, and the
Launcher's Runtime card faithfully displayed the doubled row.

The invariant this pins: for every function that invokes ``mission_chat_reply``,
exactly one usage authority exists —

* ``session_id=None`` (scratch runtime; per-call writes land on a throwaway
  row) → the function MUST call ``_update_persona_chat_token_counts``; it is
  the sole writer of the bound session.
* any real ``session_id`` (native runtime; per-call writes land on the bound
  row) → the function MUST NOT call ``_update_persona_chat_token_counts``;
  stacking the turn totals on top double-counts.

persona_commands.py is an exec'd command part (harness._load_command_parts),
not an importable module, so — like the record-at-injection guards — this
parses the exact source text that gets exec'd.
"""

import ast
from pathlib import Path


def _persona_commands_tree() -> ast.Module:
    import hermes_cli.harness as harness

    path = Path(harness.__file__).with_name("harness_parts") / "persona_commands.py"
    return ast.parse(path.read_text(encoding="utf-8"))


def _calls_in(func: ast.FunctionDef, name: str) -> list[ast.Call]:
    calls = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            callee = node.func
            found = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
            if found == name:
                calls.append(node)
    return calls


def _mission_chat_reply_lanes() -> list[tuple[ast.FunctionDef, ast.Call]]:
    lanes = []
    for node in ast.walk(_persona_commands_tree()):
        if isinstance(node, ast.FunctionDef):
            for call in _calls_in(node, "mission_chat_reply"):
                lanes.append((node, call))
    return lanes


def _session_id_is_none(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "session_id":
            return isinstance(kw.value, ast.Constant) and kw.value.value is None
    # A lane omitting session_id gets the runtime default (None) — scratch.
    return True


def test_mission_chat_reply_lanes_exist():
    lanes = _mission_chat_reply_lanes()
    assert lanes, "no mission_chat_reply call sites found in persona_commands"


def test_every_lane_has_exactly_one_usage_authority():
    for func, call in _mission_chat_reply_lanes():
        writer_calls = len(_calls_in(func, "_update_persona_chat_token_counts"))
        if _session_id_is_none(call):
            assert writer_calls >= 1, (
                f"{func.name} runs mission_chat_reply on a SCRATCH session "
                "(session_id=None): per-call runtime usage lands on a throwaway "
                "row, so this lane MUST call _update_persona_chat_token_counts "
                "or the bound session the Launcher reads records zero usage"
            )
        else:
            assert writer_calls == 0, (
                f"{func.name} runs mission_chat_reply NATIVELY BOUND to the "
                "chat session: conversation_loop/codex_runtime already persist "
                "every API call onto that row, so calling "
                "_update_persona_chat_token_counts here double-counts every "
                "usage counter at exactly 2x (the 2026-07-24 'in 20,208 for a "
                "bare hi' Runtime-card bug)"
            )
