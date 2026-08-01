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

Two handler variants carry the chat lane; both are guarded:
  * `_cmd_mission_chat_message` — emits inline (`_emit_chat_final` / `print`);
    the title now runs after that emit, wrapped so it cannot raise into the
    crash-tail guard.
  * `_run_free_floating_assignment_once` — never writes stdout; its caller
    `_queue_free_floating_assignment` emits. The runner packages the title as a
    deferred thunk (3rd return element) the caller runs AFTER its own emit.
"""

from __future__ import annotations

import ast
from pathlib import Path

TITLE = "_maybe_auto_title_persona_chat"
EMIT = "_emit_chat_final"


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


def test_main_handler_emits_terminal_frame_before_titling():
    func = _func(_persona_commands_tree(), _TURN_BODY_FUNCTIONS[0])
    # The success turn tail is one try-body that both emits the terminal frame
    # AND (now) titles. Find that try and assert emit precedes title within it.
    outer = None
    for node in ast.walk(func):
        if not isinstance(node, ast.Try):
            continue
        if any(_stmt_has_call(s, EMIT) for s in node.body) and any(
            _stmt_has_call(s, TITLE) for s in node.body
        ):
            outer = node
            break
    assert outer is not None, (
        "no single try-body both emits chat.final AND titles — the success turn "
        "tail structure changed; re-verify the F4 ordering guard"
    )
    emit_idx = next(i for i, s in enumerate(outer.body) if _stmt_has_call(s, EMIT))
    title_idx = next(i for i, s in enumerate(outer.body) if _stmt_has_call(s, TITLE))
    assert emit_idx < title_idx, (
        "auto-title must run AFTER the terminal chat.final/print emit — not "
        "between the last streamed delta and the terminal frame (F4)"
    )
    # The title statement must be wrapped so a title failure cannot raise into
    # the crash-tail guard (`terminal_outcome is not None -> raise`), which would
    # corrupt the one-JSON-object stdout contract or flip the exit code.
    assert isinstance(outer.body[title_idx], ast.Try), (
        "post-emit auto-title must be wrapped in try/except so it cannot raise"
    )


def test_main_handler_titles_exactly_once():
    func = _func(_persona_commands_tree(), _TURN_BODY_FUNCTIONS[0])
    assert len(_calls_named(func, TITLE)) == 1, (
        "the moved title call must not be duplicated across paths"
    )


# --------------------------------------------------------------------------- #
# Free-floating runner + its caller                                           #
# --------------------------------------------------------------------------- #


def test_runner_titles_only_inside_the_deferred_thunk():
    runner = _func(_persona_commands_tree(), "_run_free_floating_assignment_once")
    thunk = next(
        (
            n
            for n in ast.walk(runner)
            if isinstance(n, ast.FunctionDef) and n.name == "_deferred_auto_title"
        ),
        None,
    )
    assert thunk is not None, "runner must package the title as a deferred thunk"
    all_title_calls = _calls_named(runner, TITLE)
    thunk_title_calls = _calls_named(thunk, TITLE)
    assert all_title_calls, "the deferred thunk must still title the session"
    # Every title call lives INSIDE the thunk — the runner never titles inline
    # before returning, which would keep the auxiliary-LLM RTT on the critical
    # path (the whole point of F4).
    assert len(all_title_calls) == len(thunk_title_calls), (
        "the runner titles inline before returning — that keeps the title RTT "
        "on the critical path; it must only run via the caller-invoked thunk"
    )


def test_runner_returns_thunk_as_third_tuple_element():
    runner = _func(_persona_commands_tree(), "_run_free_floating_assignment_once")
    returns_thunk = any(
        isinstance(node, ast.Return)
        and isinstance(node.value, ast.Tuple)
        and isinstance(node.value.elts[-1], ast.Name)
        and node.value.elts[-1].id == "_deferred_auto_title"
        for node in ast.walk(runner)
    )
    assert returns_thunk, (
        "the success return must hand the deferred title thunk to the caller as "
        "the 3rd tuple element (exit_code, payload, deferred_title)"
    )


def test_caller_runs_deferred_title_after_emit_and_wrapped():
    caller = _func(_persona_commands_tree(), "_queue_free_floating_assignment")
    # The 3-tuple unpack pins the runner's return contract at the call site.
    unpacks_three = any(
        isinstance(node, ast.Assign)
        and _calls_named(node, "_run_free_floating_assignment_once")
        and isinstance(node.targets[0], ast.Tuple)
        and len(node.targets[0].elts) == 3
        and "deferred_title"
        in [e.id for e in node.targets[0].elts if isinstance(e, ast.Name)]
        for node in ast.walk(caller)
    )
    assert unpacks_three, "caller must unpack (exit_code, payload, deferred_title)"

    deferred_calls = _calls_named(caller, "deferred_title")
    assert deferred_calls, "caller must run the deferred title thunk"
    latest_emit = max(c.lineno for c in _calls_named(caller, EMIT))
    latest_print = max((c.lineno for c in _calls_named(caller, "print")), default=0)
    for call in deferred_calls:
        assert call.lineno > latest_emit and call.lineno > latest_print, (
            "the deferred title must run AFTER the terminal frame is emitted "
            "(both the stream `_emit_chat_final` and the non-stream `print` paths)"
        )
    # Wrapped so a title failure can never change stdout / the exit code.
    assert any(
        _calls_named(t, "deferred_title") for t in ast.walk(caller) if isinstance(t, ast.Try)
    ), "the deferred title call must be wrapped in try/except"


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
