"""C7 (hermes half, finding F4) — auto-title is OFF the chat turn's critical path.

The first turn of a persona-chat session pays a synchronous auxiliary-LLM title
call. It writes SessionDB-only state that NOTHING in the terminal frame depends
on, so it must run AFTER the terminal frame is emitted — not between the last
streamed delta and `chat.final`, where the operator's console visibly hangs.

`persona_commands.py` is an exec'd command part (harness._load_command_parts),
not an importable module for its full turn handlers, so the ORDERING is pinned
with an AST guard over the exact source text that gets exec'd (the same pattern
`test_mission_chat_records_injection.py` uses). The SWALLOW contract the
post-emit placement relies on — a title failure can never propagate and corrupt
the one-JSON-object stdout / flip the exit code — is pinned behaviorally through
the importable `_maybe_auto_title_persona_chat` seam.

One handler carries the chat lane and is guarded here:
  * `_cmd_mission_chat_message` / `_mission_chat_commit_turn` — emits inline
    (`_mission_chat_emit`); the title runs after that emit, wrapped so it
    cannot raise into the crash-tail guard.

(The free-floating runner variant this file also guarded —
`_run_free_floating_assignment_once` + `_queue_free_floating_assignment` — was
removed with the free-floating assignment lane at S70; see the tombstone
registry.)

R0 (2026-08-09) moved the boundary again, and the guards below moved with it.
Post-emit turned out not to be far enough: the title still ran inside the
chat-root lease, so for the whole auxiliary-LLM round trip — 46 seconds in the
incident, including a lazy `pip install` and a 401 retry — the root refused the
operator's next send with `chat_busy` while the reply he was answering sat on
his screen. `_mission_chat_commit_turn` now only PACKAGES the title, into a
`MissionChatDeferredFinalization` its caller runs after `with
persona_chat_root_lease(...)` has exited. What is guarded here is that split:
the commit phase never titles inline, and the caller never runs the tail before
the lease is gone. The behavioral counterpart — the root is genuinely acquirable
while the title runs — lives in
`tests/agent_runtime/test_chat_lease_finalization_tail.py`.
"""

from __future__ import annotations

import ast
from pathlib import Path

TITLE = "_maybe_auto_title_persona_chat"
# WP-H2 routed every mission-chat terminal payload through ONE seam
# (``_mission_chat_emit``), which owns the stream/print branch this guard used
# to read directly. The main handler is pinned on the seam; the free-floating
# lane still calls ``_emit_chat_final`` itself and is pinned on that. Naming
# both is what keeps the guard following the code instead of quietly finding
# nothing and passing.
EMIT_SEAM = "_mission_chat_emit"


def _persona_commands_tree() -> ast.Module:
    # Parse the exact bytes harness._load_command_parts() exec's — the handlers
    # are not importable functions, so structural ordering is asserted on source.
    import hermes_cli.harness as harness

    path = Path(harness.__file__).with_name("harness_parts") / "persona_commands.py"
    return ast.parse(path.read_text(encoding="utf-8"))


def _func(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in persona_commands.py")


# The mission-chat turn body was split on 2026-07-31: the PLAN phase kept the
# name ``_cmd_mission_chat_message`` (resolve, refuse, decide) and every durable
# write moved into ``_mission_chat_commit_turn``, the sole writer, which runs
# under the chat-root lease. What these guards pin lives in the writer; both
# halves are named so the assertion follows the code if the boundary moves
# again, rather than silently finding nothing and passing.
_TURN_BODY_FUNCTIONS = ("_mission_chat_commit_turn", "_cmd_mission_chat_message")


def _call_name(node: ast.Call):
    callee = node.func
    if isinstance(callee, ast.Name):
        return callee.id
    return getattr(callee, "attr", None)


def _calls_named(node: ast.AST, name: str) -> list[ast.Call]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Call) and _call_name(n) == name]


def _stmt_has_call(stmt: ast.stmt, name: str) -> bool:
    return bool(_calls_named(stmt, name))


# --------------------------------------------------------------------------- #
# Main handler: _cmd_mission_chat_message                                      #
# --------------------------------------------------------------------------- #


DEFERRED_THUNK = "_deferred_auto_title"
LEASE = "persona_chat_root_lease"


def test_main_handler_emits_terminal_frame_before_packaging_the_title():
    func = _func(_persona_commands_tree(), _TURN_BODY_FUNCTIONS[0])
    # The success turn tail is one try-body that both emits the terminal frame
    # AND (now) packages the title. Find that try and assert emit precedes it.
    outer = None
    for node in ast.walk(func):
        if not isinstance(node, ast.Try):
            continue
        if any(_stmt_has_call(s, EMIT_SEAM) for s in node.body) and any(
            _stmt_has_call(s, TITLE) for s in node.body
        ):
            outer = node
            break
    assert outer is not None, (
        "no single try-body both emits the terminal payload AND carries the title — the "
        "success turn tail structure changed; re-verify the F4/R0 ordering guards"
    )
    emit_idx = next(i for i, s in enumerate(outer.body) if _stmt_has_call(s, EMIT_SEAM))
    title_idx = next(i for i, s in enumerate(outer.body) if _stmt_has_call(s, TITLE))
    assert emit_idx < title_idx, (
        "auto-title must come AFTER the terminal chat.final/print emit — not "
        "between the last streamed delta and the terminal frame (F4)"
    )
    # R0: and it must be a DEFINITION, not a call. The commit phase runs under
    # the chat-root lease; anything it executes here holds the root against the
    # operator's next send for as long as it takes.
    assert isinstance(outer.body[title_idx], ast.FunctionDef), (
        "the commit phase must PACKAGE the title as a nested thunk, not run it: "
        "it executes under the chat-root lease, and an auxiliary-LLM round trip "
        "there refuses the operator's next send for its whole duration (R0)"
    )
    assert outer.body[title_idx].name == DEFERRED_THUNK


def test_the_commit_phase_never_titles_outside_the_deferred_thunk():
    func = _func(_persona_commands_tree(), _TURN_BODY_FUNCTIONS[0])
    thunk = next(
        (
            n
            for n in ast.walk(func)
            if isinstance(n, ast.FunctionDef) and n.name == DEFERRED_THUNK
        ),
        None,
    )
    assert thunk is not None, "the commit phase must package the title as a deferred thunk"
    all_title_calls = _calls_named(func, TITLE)
    assert all_title_calls, "the deferred thunk must still title the session"
    assert len(all_title_calls) == 1, (
        "the moved title call must not be duplicated across paths"
    )
    assert len(all_title_calls) == len(_calls_named(thunk, TITLE)), (
        "the commit phase titles inline somewhere — that runs an auxiliary-LLM "
        "round trip under the chat-root lease, which is the 2026-08-09 defect"
    )
    # The metadata event reports a title CHANGE, so it belongs to the title and
    # travels with it. Left behind, it would read a title the thunk has not
    # written yet and never fire.
    assert _calls_named(func, "_publish_persona_chat_metadata_event") == _calls_named(
        thunk, "_publish_persona_chat_metadata_event"
    ), "the title-change metadata event must move with the title it reports"


def test_the_caller_runs_the_deferred_tail_only_after_the_lease_block_exits():
    caller = _func(_persona_commands_tree(), _TURN_BODY_FUNCTIONS[1])
    lease_blocks = [
        node
        for node in ast.walk(caller)
        if isinstance(node, ast.With)
        and any(_calls_named(item.context_expr, LEASE) for item in node.items)
    ]
    assert len(lease_blocks) == 1, (
        f"expected exactly one chat-root lease block in {_TURN_BODY_FUNCTIONS[1]}, "
        f"found {len(lease_blocks)}"
    )
    lease = lease_blocks[0]
    runs = _calls_named(caller, "run_once")
    assert runs, (
        "the caller must run the deferred finalization — otherwise the title is "
        "packaged and then silently dropped, and sessions never get titled"
    )
    for call in runs:
        assert call.lineno > lease.end_lineno, (
            "the deferred tail runs INSIDE the chat-root lease block. That is the "
            "whole defect: the root stays leased for an auxiliary-LLM round trip "
            "that writes nothing to it, and the operator's next send is refused "
            "chat_busy the entire time (R0, 2026-08-09)"
        )
    # Nothing may execute the packaged thunk under the lease by another name.
    for call in _calls_named(caller, DEFERRED_THUNK):
        assert call.lineno > lease.end_lineno


# --------------------------------------------------------------------------- #
# Behavioral: the swallow contract the post-emit placement relies on          #
# --------------------------------------------------------------------------- #


def test_maybe_auto_title_swallows_a_raising_title_generator(monkeypatch):
    import agent.title_generator as tg
    import hermes_cli.harness as harness

    def _boom(*args, **kwargs):
        raise RuntimeError("title provider exhausted the fallback chain")

    monkeypatch.setattr(tg, "auto_title_session", _boom)
    # Must swallow and return None — never propagate. The post-emit call sites
    # rely on this so a first-turn title failure cannot corrupt the emitted JSON
    # or flip the exit code after the terminal frame is already on stdout.
    assert (
        harness._maybe_auto_title_persona_chat(
            session_db=object(),
            session_id="s1",
            user_message="hello",
            assistant_response="hi there",
        )
        is None
    )


def test_maybe_auto_title_still_titles_on_success(monkeypatch):
    import agent.title_generator as tg
    import hermes_cli.harness as harness

    seen = []
    monkeypatch.setattr(tg, "auto_title_session", lambda *a, **k: seen.append((a, k)))
    harness._maybe_auto_title_persona_chat(
        session_db=object(),
        session_id="s1",
        user_message="hello",
        assistant_response="hi there",
    )
    assert len(seen) == 1, "the title worker must still be invoked on success"
    # session_id + reply are forwarded positionally to the title worker.
    assert seen[0][0][1] == "s1"
    assert seen[0][0][3] == "hi there"
