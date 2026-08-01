"""C8 guards — the retired lanes stay retired (ruling: one ordering authority).

`persona_commands.py` is an exec'd command part (harness._load_command_parts),
so the retirements are pinned with AST/source guards over the exact bytes that
get exec'd (the accepted pattern — see test_mission_chat_title_offpath.py):

* the legacy ``chat.delta`` wire lane is GONE — no emitter method, no frame
  writer, no double-emit per token (ruling 0: one shape);
* the pre-trace ack lane never touches the durable record — no SessionDB
  injection helper, the callback wires to the emitter's presentation-only
  ``ack`` frame, and ``ack`` never appends elements / never persists.
"""

from __future__ import annotations

import ast
from pathlib import Path


def _persona_commands_source() -> str:
    import hermes_cli.harness as harness

    path = Path(harness.__file__).with_name("harness_parts") / "persona_commands.py"
    return path.read_text(encoding="utf-8")


def _tree() -> ast.Module:
    return ast.parse(_persona_commands_source())


def _func(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in persona_commands.py")


def _class(tree: ast.AST, name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in persona_commands.py")


def _call_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            callee = n.func
            if isinstance(callee, ast.Name):
                names.add(callee.id)
            else:
                attr = getattr(callee, "attr", None)
                if attr:
                    names.add(attr)
    return names


# --------------------------------------------------------------------------- #
# Legacy chat.delta lane is gone                                              #
# --------------------------------------------------------------------------- #


def test_legacy_chat_delta_lane_is_retired():
    source = _persona_commands_source()
    assert "emit_legacy_delta" not in source, (
        "the legacy chat.delta emitter method must stay deleted (C8 ruling 0)"
    )
    assert "_emit_chat_delta" not in source, (
        "the legacy chat.delta frame writer must stay deleted (C8 ruling 0)"
    )
    assert '"chat.delta"' not in source and "'chat.delta'" not in source, (
        "no code path may mint a chat.delta frame — v2 segment.delta is the "
        "one wire shape per token"
    )


def test_emitter_has_no_legacy_method():
    emitter = _class(_tree(), "_ChatProtocolV2Emitter")
    method_names = {
        node.name for node in emitter.body if isinstance(node, ast.FunctionDef)
    }
    assert "emit_legacy_delta" not in method_names
    assert "ack" in method_names, "the presentation-only ack frame method must exist"


# --------------------------------------------------------------------------- #
# Pre-trace acks never enter the durable record                               #
# --------------------------------------------------------------------------- #


def test_sessiondb_ack_injection_helper_is_gone():
    source = _persona_commands_source()
    assert "_append_persona_pre_trace_ack" not in source, (
        "the SessionDB ack-injection helper must stay deleted — acks are a "
        "presentation-only turn.ack stream frame (C8)"
    )
    assert "PERSONA_PRE_TRACE_ACK_FINISH_REASON" not in source, (
        "persona_commands must no longer stamp the ack finish_reason marker; "
        "it survives ONLY as the projections' pre-C8 read-side residue"
    )


def test_pre_trace_callback_wires_to_the_emitter_ack():
    # The ack callback is wired at the provider call, which moved into the sole
    # writer when the turn body was split (2026-07-31).
    func = _func(_tree(), "_mission_chat_commit_turn")
    wired = False
    for node in ast.walk(func):
        if not isinstance(node, ast.keyword) or node.arg != "pre_trace_callback":
            continue
        value = node.value
        wired = (
            isinstance(value, ast.Attribute)
            and value.attr == "ack"
            and isinstance(value.value, ast.Name)
            and value.value.id == "stream_emitter"
        )
    assert wired, (
        "pre_trace_callback must be the emitter's ack method (presentation "
        "frame), never a SessionDB writer"
    )


def test_ack_method_is_presentation_only():
    emitter = _class(_tree(), "_ChatProtocolV2Emitter")
    ack = next(
        node
        for node in emitter.body
        if isinstance(node, ast.FunctionDef) and node.name == "ack"
    )
    calls = _call_names(ack)
    # Never persists, never becomes an element, never triggers the turn-store
    # on_update flush — the ONLY effect is one v2 frame write.
    assert "_notify_update" not in calls, "ack must not trigger a turn-store flush"
    assert "append" not in calls, "ack must not append a turn element"
    assert "persist_mission_chat_turn" not in calls
    assert "_emit_chat_frame" in calls, "ack must emit exactly the v2 frame"
    # And the frame it emits is the typed turn.ack kind.
    ack_source = ast.get_source_segment(_persona_commands_source(), ack) or ""
    assert '"turn.ack"' in ack_source


def test_assistant_append_lost_its_finish_reason_seam():
    func = _func(_tree(), "_append_persona_assistant_text")
    args = {a.arg for a in func.args.args} | {a.arg for a in func.args.kwonlyargs}
    assert "finish_reason" not in args, (
        "the finish_reason marker parameter existed only for the ack lane; "
        "its removal keeps this writer a real-replies-only chokepoint"
    )
