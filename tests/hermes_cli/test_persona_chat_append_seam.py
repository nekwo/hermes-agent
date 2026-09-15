"""The explicit persona-chat append seam has a contract, so it is not "dead".

WHY THIS FILE EXISTS
====================

The 2026-08-18 dead-code audit's NEW-1 row proposed reaping
`_append_persona_operator_turn`, `_append_persona_assistant_text` and
`_persist_persona_chat_row` from `hermes_cli/harness_parts/persona_commands.py`
as a test-only-alive island: ~184 production lines with zero production
callers, plus ~250 test lines. **The census was correct. The conclusion was
refused on 2026-08-19**, and this file is the other half of that refusal —
because "we decided to keep it" written in a docstring is a preference, while a
contract is a reason.

Zero production callers is a phase, not a verdict, when the subject is a
chokepoint. The mission-chat lane stopped coming through here when native
session continuity landed (the runtime now persists the operator/assistant rows
WITH the turn), not because the door was wrong. The relay lane still drives
`_append_persona_operator_turn(relay_marker=)`, and that wire behaviour has
already broken silently for eleven days once.

What deleting it would actually delete is five enforcement points that the next
explicit append site would hand-roll:

1. redaction at the per-role limit,
2. the assistant-row idempotency check (never two replies for one
   `client_message_id`),
3. the live-log mirror, bound by a CONTEXT MANAGER rather than a trailing call
   — a shape chosen because the first cut hooked append sites by convention and
   immediately missed one, so its rows never reached the live log,
4. typed `PersonaChatPersistenceError` reporting rather than a bare exception,
5. the `required=` split between raise and degrade.

Each of those is pinned below. A future author who wants the seam gone has to
argue with a behaviour, which is the right conversation to have.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

PERSONA_COMMANDS = (
    pathlib.Path(__file__).resolve().parents[2]
    / "hermes_cli"
    / "harness_parts"
    / "persona_commands.py"
)

#: The three functions the audit proposed reaping together.
_SEAM = (
    "_append_persona_operator_turn",
    "_append_persona_assistant_text",
    "_persist_persona_chat_row",
)


def _tree() -> ast.Module:
    source = PERSONA_COMMANDS.read_text(encoding="utf-8")
    assert len(source) > 100_000, "persona_commands.py read came back too small — vacuous"
    return ast.parse(source)


def _func(name: str) -> ast.FunctionDef:
    for node in ast.walk(_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(
        f"{name} is gone from persona_commands.py. It is the explicit "
        "persona-chat append seam, kept deliberately (see its docstring). If "
        "its removal is intended, the five guarantees pinned in this file must "
        "be re-homed first and this file deleted in the same commit — not left "
        "behind asserting a contract nothing implements."
    )


def _call_names(func: ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            target = node.func
            names.add(getattr(target, "id", None) or getattr(target, "attr", "") or "")
    return names


@pytest.mark.parametrize("name", _SEAM)
def test_the_seam_still_exists(name: str):
    assert _func(name) is not None


@pytest.mark.parametrize(
    "writer,limit",
    [
        ("_append_persona_operator_turn", "PERSONA_CHAT_OPERATOR_MESSAGE_LIMIT"),
        ("_append_persona_assistant_text", "PERSONA_CHAT_REPLY_LIMIT"),
    ],
)
def test_every_writer_redacts_at_its_own_limit(writer: str, limit: str):
    """Guarantee 1. Two roles, two limits — one shared limit is a regression."""

    source = ast.unparse(_func(writer))
    assert "_redact_persona_chat_text" in source, (
        f"{writer} no longer redacts. Persona-chat text reaches SessionDB and "
        "the live log, which an operator pastes into chat."
    )
    assert limit in source, f"{writer} no longer applies {limit}"


def test_the_assistant_writer_refuses_a_second_reply_for_one_client_message_id():
    """Guarantee 2. Without it a retried send doubles the reply in the thread."""

    source = ast.unparse(_func("_append_persona_assistant_text"))
    assert "_persona_chat_existing_turn" in source, (
        "the assistant writer lost its idempotency check — a retry now appends "
        "a second assistant row for the same client_message_id"
    )


def test_the_write_is_wrapped_by_the_mirror_context_manager():
    """Guarantee 3, and the shape matters as much as the presence.

    A trailing `mirror(...)` CALL after the append is the version that already
    failed: `agent_runtime.continuity.return_summary_to_parent_session` was
    missed by exactly that convention and its rows never reached the live log.
    A `with` block is the version a reviewer can check, so this asserts the
    append happens INSIDE the context manager, not merely beside it.
    """

    func = _func("_persist_persona_chat_row")
    withs = [node for node in ast.walk(func) if isinstance(node, ast.With)]
    assert withs, "the mirror is no longer bound by a context manager"

    wrapping = [
        node
        for node in withs
        if any(
            "mirrored_persona_chat_append"
            in (getattr(item.context_expr.func, "id", "") or getattr(item.context_expr.func, "attr", ""))
            for item in node.items
            if isinstance(item.context_expr, ast.Call)
        )
    ]
    assert wrapping, "no `with mirrored_persona_chat_append(...)` block found"
    inner = {
        getattr(call.func, "attr", "")
        for block in wrapping
        for call in ast.walk(block)
        if isinstance(call, ast.Call)
    }
    assert "append_message" in inner, (
        "`session_db.append_message` moved OUT of the mirror context manager. "
        "The SessionDB row and its live-log line can now drift apart — the "
        "exact defect this seam's shape exists to make un-writable."
    )


@pytest.mark.parametrize("name", _SEAM)
def test_every_writer_reports_failure_through_the_typed_reporter(name: str):
    """Guarantees 4 and 5: a typed error, and required= decides raise vs degrade."""

    func = _func(name)
    assert "_persona_chat_persistence_failed" in _call_names(func), (
        f"{name} no longer routes failure through the typed reporter; a bare "
        "exception here loses the operation name the operator needs"
    )
    args = {a.arg for a in func.args.args} | {a.arg for a in func.args.kwonlyargs}
    assert "required" in args, (
        f"{name} lost its `required` parameter — the raise-or-degrade decision "
        "would move back to each call site"
    )


def test_the_relay_marker_still_reaches_the_persisted_row():
    """The lane that is NOT hypothetical.

    Relay attribution travels as `finish_reason` on the persisted row. It broke
    silently for eleven days once; the path from the operator writer's
    `relay_marker=` through to `append_message(finish_reason=...)` is what makes
    the sender visible at all.
    """

    operator = ast.unparse(_func("_append_persona_operator_turn"))
    assert "relay_marker=relay_marker" in operator, (
        "the operator writer no longer forwards relay_marker to the persist "
        "seam — relayed rows lose their sender attribution"
    )
    persist = ast.unparse(_func("_persist_persona_chat_row"))
    assert "finish_reason=relay_marker" in persist, (
        "the persist seam no longer writes relay_marker into finish_reason"
    )


def test_the_keep_decision_is_recorded_where_the_next_sweep_will_look():
    """A refusal nobody can find gets re-litigated every audit."""

    doc = inspect.cleandoc(ast.get_docstring(_func("_persist_persona_chat_row")) or "")
    assert "KEPT DELIBERATELY" in doc, (
        "the keep decision left `_persist_persona_chat_row`'s docstring. Zero "
        "production callers plus no recorded reason reads as dead code to the "
        "next sweep, and it will propose the same reap again."
    )
