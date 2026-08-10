"""The persona-chat WIRE boundary — every character it removes is named.

=============================================================================
THE COUPLING THIS PINS
=============================================================================

``run_agent`` flushes each turn's messages to the session DB for crash
resilience. That flush hands every persona-chat row through
:func:`~agent_runtime.persona_chat_continuity.native_wire_row` and then writes
the result back into the LIVE actor's message list::

    bound = native_wire_row({...})
    native = bound.row
    msg.clear()
    msg.update(native)

``msg`` is the same dict object the provider call reads, and the flush runs
BEFORE the first API call of the turn. So a function that reads as persistence
is in fact the last thing to touch the prompt: **whatever it cuts, the model
never sees.**

On 2026-08-09 that cost a real turn. ``_MAX_CONTENT = 20_000`` — a persistence
bound by every appearance — was applied to a composed operator row and delivered
37% of a required skill cut mid-sentence, with the runtime HUD amputated
entirely; the cut took the ``</skill_preload>`` closing tag with it, which made
the ``unchanged`` dedupe structurally unreachable and re-shipped ~3.7 k tokens
every turn thereafter. The per-part bounds that fixed it landed in ``23c684cb3``.

What did NOT land was an invariant. In the fixing agent's own words: *"There is
still no invariant asserting 'what the boundary produced == what was submitted'.
The receipt reports drift after the fact rather than preventing it."* Every
future change to a bound in that module could still silently change the prompt,
and three of the four roles were still losing content with no note at all.

=============================================================================
THE INVARIANT, AND WHY IT IS NOT "wire == submitted"
=============================================================================

The boundary is SUPPOSED to shorten content — that is its job. A bare equality
assertion could therefore only ever be false, which is precisely why the
previous receipt could report drift and never prevent it. The checkable
invariant is one level up:

    every character between the submitted content and the wire content is
    accounted for, either by redaction or by a typed :class:`ContentBoundNote`.

:attr:`WireBoundaryRow.unaccounted_loss` is that residue, and
:attr:`~WireBoundaryRow.holds` is the invariant. A non-zero residue means
content is reaching the model in a shape no receipt describes.

**Assertion here, fail-loud row in production.** The hard assertions live in
this file, over the PURE function, where they cost nothing. Production reports
via :func:`record_wire_boundary_drift` instead: the flush runs on the live agent
turn loop, and raising there would convert an accounting bug into a lost turn on
a conversation that is otherwise healthy — worse than the drift being reported,
in the one place a user cannot cheaply retry. That split is the answer to "why
not just assert in the runtime": the runtime is concurrency-adjacent core code
and the invariant is fully checkable off it.

RED-PROOF (each reverted after):

* deleting the ``notes=`` argument from ``_bounded_free_text``'s truncating
  return makes :func:`test_a_truncated_free_text_row_accounts_for_every_lost_char`
  and the assistant/tool/system cases fail with a 10,000-char residue — this is
  the silent class the change retires, reproduced;
* restoring ``_safe_text(...)`` in place of the accounted call for non-user
  roles fails the same tests;
* netting :attr:`argument_loss` into :attr:`accounted_loss` (the bug this file's
  ``CONTENT_BOUND_PARTS`` split exists to prevent) makes
  :func:`test_argument_loss_cannot_cancel_a_content_residue` fail;
* removing ``record_wire_boundary_drift`` from the flush fails
  :func:`test_the_flush_site_uses_the_typed_boundary_and_reports_drift`.
"""

from __future__ import annotations

import ast
import inspect
import logging
from pathlib import Path

import pytest

from agent_runtime.persona_chat_continuity import (
    BOUND_ACTION_DROPPED,
    BOUND_ACTION_TRUNCATED,
    BOUND_PART_CONTENT,
    BOUND_PART_SKILL_PRELOAD,
    BOUND_PART_TOOL_ARGUMENTS,
    CONTENT_BOUND_PARTS,
    WIRE_BOUNDARY,
    ContentBoundNote,
    WireBoundaryRow,
    _MAX_ARGUMENTS,
    _MAX_CONTENT,
    native_wire_row,
    record_wire_boundary_drift,
    safe_native_message,
)
from agent_runtime.runtime_hud import (
    SKILL_PRELOAD_CODEC,
    SKILL_PRELOAD_DELIVERY_SNAPSHOT,
    render_runtime_context_envelope,
    render_skill_preload_envelope,
    skill_preload_revision,
    split_composed_user_row,
)


# The qa persona's required preload measured 54,114 chars on 2026-08-09 — ~3x
# the flat cap. Fixtures are sized to genuinely exceed every bound they test: a
# fixture that fits under the limit makes the bounded and the unbounded boundary
# identical and would pass against either.
#: A distinctive marker at the very END, so "the whole body arrived" is checked
#: rather than inferred from a length. A trailing SPACE would be stripped by the
#: envelope codec, which is a fixture artifact, not a bound — hence a real tail.
SKILL_BODY_TAIL = "PITFALL: a refused screenshot means relaunch, never improvise."


def _skill_body(chars: int = 54_114) -> str:
    filler = "Step: capture the window through the MCP marionette path. " * (chars // 58 + 1)
    return filler[: chars - len(SKILL_BODY_TAIL)] + SKILL_BODY_TAIL


def _composed_row(*, message: str = "screenshot the NEWS tab", skill_chars: int = 54_114) -> str:
    body = _skill_body(skill_chars)
    preload = render_skill_preload_envelope(
        skill_names=["launcher-stagec-mcp-screenshot"],
        skill_preload_content=body,
        revision=skill_preload_revision(body),
        delivery=SKILL_PRELOAD_DELIVERY_SNAPSHOT,
    )
    hud = render_runtime_context_envelope(
        context_id="ctx_0123456789abcdef",
        revision="hud_" + "a" * 16,
        delivery="snapshot",
        situational_hud_content="## Runtime Situation\nscope: eternia · launcher",
        volatile_content="Turn budget: with under 270 s left, wrap up and report.",
    )
    return "\n\n".join([message, preload, hud])


# --------------------------------------------------------------------------- #
# THE INVARIANT
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", ["user", "assistant", "tool", "system"])
def test_a_short_row_reaches_the_wire_whole_and_unbounded(role: str):
    bound = native_wire_row({"role": role, "content": "a modest sentence"})

    assert bound.row["content"] == "a modest sentence"
    assert bound.notes == ()
    assert bound.wire_chars == bound.redacted_chars
    assert bound.unaccounted_loss == 0
    assert bound.holds


@pytest.mark.parametrize("role", ["assistant", "tool", "system"])
def test_a_truncated_free_text_row_accounts_for_every_lost_char(role: str):
    """The silent class. These three roles were bounded with no note at all.

    A 30 KB tool result was cut to 20 K on the way to the MODEL — not merely on
    the way to disk — and nothing anywhere recorded it. The per-part accounting
    added for the composed operator row covered ``user`` only.
    """

    content = "x" * 30_000
    bound = native_wire_row({"role": role, "content": content})

    assert bound.wire_chars == _MAX_CONTENT
    assert bound.redacted_chars == 30_000
    assert [(n.part, n.action) for n in bound.notes] == [
        (BOUND_PART_CONTENT, BOUND_ACTION_TRUNCATED)
    ]
    assert bound.accounted_loss == 30_000 - _MAX_CONTENT
    assert bound.unaccounted_loss == 0, "the bound must be fully explained by its note"
    assert bound.holds


def test_the_composed_operator_row_closes_its_arithmetic():
    """The three-part row: the split, the per-part bounds and the rejoin must
    lose nothing that the notes do not name."""

    composed = _composed_row()
    assert len(composed) > 50_000, "fixture must exceed the retired 20k cap"

    bound = native_wire_row({"role": "user", "content": composed})

    # Nothing was cut at all here — the per-part ceiling holds 256 KiB.
    assert bound.notes == ()
    assert bound.wire_chars == bound.redacted_chars == len(composed)
    assert bound.holds
    # ...and the skill survived intact, which is the F1 regression itself.
    body = SKILL_PRELOAD_CODEC.body(split_composed_user_row(bound.row["content"]).skill_preload)
    assert body is not None
    assert body.endswith(SKILL_BODY_TAIL), "the skill's TAIL must survive, not just its length"
    assert len(body) == len(_skill_body())


def test_a_dropped_part_is_accounted_as_a_drop_not_a_silent_absence():
    """A part too large to survive is dropped and SAID SO. An honest absence is
    the design; an unrecorded one is the defect."""

    composed = _composed_row(skill_chars=400_000)
    bound = native_wire_row({"role": "user", "content": composed})

    assert bound.bounded
    assert any(n.action == BOUND_ACTION_DROPPED for n in bound.notes) or any(
        n.action == BOUND_ACTION_TRUNCATED for n in bound.notes
    )
    assert bound.unaccounted_loss == 0, (
        f"unexplained residue of {bound.unaccounted_loss} chars; notes were "
        f"{[(n.part, n.action, n.original_chars, n.bounded_chars) for n in bound.notes]}"
    )
    assert bound.holds


def test_tool_call_arguments_are_bounded_and_accounted():
    bound = native_wire_row(
        {
            "role": "assistant",
            "content": "calling a tool",
            "tool_calls": [{"id": "c1", "function": {"name": "terminal", "arguments": "a" * 9_000}}],
        }
    )

    assert len(bound.row["tool_calls"][0]["function"]["arguments"]) == _MAX_ARGUMENTS
    assert [n.part for n in bound.notes] == [BOUND_PART_TOOL_ARGUMENTS]
    assert bound.argument_loss == 9_000 - _MAX_ARGUMENTS
    assert bound.holds


def test_argument_loss_cannot_cancel_a_content_residue():
    """Argument loss is REPORTED, never netted into the content arithmetic.

    Summing both into ``accounted_loss`` would let a truncated argument blob
    cancel out a real content residue and drive ``unaccounted_loss`` to zero —
    a check that hides exactly what it exists to find. Asserted on a
    hand-built row so the two quantities can be forced apart.
    """

    row = WireBoundaryRow(
        row={"role": "assistant", "content": "..."},
        notes=(
            # An argument bound big enough to swallow the content residue below.
            ContentBoundNote(
                part=BOUND_PART_TOOL_ARGUMENTS,
                action=BOUND_ACTION_TRUNCATED,
                original_chars=9_000,
                bounded_chars=4_000,
                limit=_MAX_ARGUMENTS,
            ),
        ),
        submitted_chars=1_000,
        redacted_chars=1_000,
        wire_chars=600,
    )

    assert row.accounted_loss == 0, "an argument note must not count as content accounting"
    assert row.argument_loss == 5_000
    assert row.unaccounted_loss == 400
    assert not row.holds


def test_applying_the_boundary_twice_is_stable_and_loses_nothing_further():
    """Warm memory and cold persistence share this boundary, so a second pass
    must be a no-op. If it were not, a resumed session would drift from a live
    one by exactly one more truncation per reload."""

    once = native_wire_row({"role": "assistant", "content": "y" * 30_000})
    twice = native_wire_row(once.row)

    assert twice.row["content"] == once.row["content"]
    assert twice.notes == ()
    assert twice.holds


# --------------------------------------------------------------------------- #
# ANTI-VACUITY — the residue detector must actually discriminate
# --------------------------------------------------------------------------- #
def test_the_residue_detector_catches_an_unaccounted_loss():
    """A ``holds`` that could never be False would pass this file forever.

    This is the shape of the defect the whole change targets: content shorter on
    the wire than it was submitted, with no note naming the difference.
    """

    silent = WireBoundaryRow(
        row={"role": "assistant", "content": "z" * 20_000},
        notes=(),
        submitted_chars=30_000,
        redacted_chars=30_000,
        wire_chars=20_000,
    )

    assert silent.unaccounted_loss == 10_000
    assert not silent.holds

    drift = silent.drift_row()
    assert drift is not None
    assert drift["boundary"] == WIRE_BOUNDARY
    assert drift["unaccounted_loss"] == 10_000
    assert drift["role"] == "assistant"


def test_redaction_is_not_reported_as_an_unaccounted_bound():
    """Redaction legitimately changes length. Folding it into the residue would
    make every row carrying a secret look like a silent amputation, and a check
    that cries wolf gets muted."""

    bound = native_wire_row({"role": "assistant", "content": "api_key: topsecretvalue"})

    assert "topsecretvalue" not in bound.row["content"]
    assert bound.submitted_chars != bound.redacted_chars or bound.holds
    assert bound.unaccounted_loss == 0
    assert bound.holds


def test_the_content_part_split_is_exhaustive():
    """Every content part must be inside ``CONTENT_BOUND_PARTS``; a new part
    added outside it would silently stop counting toward the arithmetic."""

    assert BOUND_PART_CONTENT in CONTENT_BOUND_PARTS
    assert BOUND_PART_SKILL_PRELOAD in CONTENT_BOUND_PARTS
    assert BOUND_PART_TOOL_ARGUMENTS not in CONTENT_BOUND_PARTS


# --------------------------------------------------------------------------- #
# The fail-loud row
# --------------------------------------------------------------------------- #
def test_record_wire_boundary_drift_is_silent_when_the_invariant_holds():
    bound = native_wire_row({"role": "user", "content": "fine"})

    assert record_wire_boundary_drift(bound) is None


def test_record_wire_boundary_drift_reports_loudly_and_never_raises(caplog):
    """Fail LOUD, not fatal. This runs on the live turn loop before the first
    provider call; raising would lose a healthy turn over an accounting bug."""

    silent = WireBoundaryRow(
        row={"role": "tool", "content": "z" * 10},
        notes=(),
        submitted_chars=5_000,
        redacted_chars=5_000,
        wire_chars=10,
    )

    with caplog.at_level(logging.ERROR):
        row = record_wire_boundary_drift(silent)

    assert row is not None and row["unaccounted_loss"] == 4_990
    assert any(record.levelno >= logging.ERROR for record in caplog.records)
    assert "unaccounted" in caplog.text


# --------------------------------------------------------------------------- #
# The coupling site itself
# --------------------------------------------------------------------------- #
def test_the_flush_site_uses_the_typed_boundary_and_reports_drift():
    """The flush must go through the TYPED boundary and check the residue.

    Structural (AST) rather than textual, per the repo rule: a reformat must not
    fail this, and a comment mentioning the function must not satisfy it. What
    is pinned is the shape of the coupling — the write-back into the live actor
    is what makes this the wire, so the boundary that feeds it has to be the one
    that reports.
    """

    source = Path(__file__).resolve().parents[2] / "run_agent.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    def called(name: str) -> list[ast.Call]:
        return [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == name)
                or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
            )
        ]

    assert called("native_wire_row"), (
        "the flush no longer goes through the typed wire boundary; "
        "`safe_native_message` alone drops the accounting that makes the "
        "silent-amputation class detectable"
    )
    assert called("record_wire_boundary_drift"), (
        "the flush no longer reports wire-boundary drift — an unaccounted loss "
        "would again reach the model with nothing recording it"
    )

    # The write-back is what makes this the wire rather than the record. If it
    # ever stops happening, this file's premise is void and it should be
    # rewritten rather than left asserting a coupling that no longer exists.
    assert called("clear") and called("update"), (
        "the flush no longer writes the bounded row back into the live actor"
    )


def test_safe_native_message_still_returns_the_plain_row():
    """Back-compat: every existing caller keeps working, and gets the same row
    the typed form carries."""

    message = {"role": "assistant", "content": "w" * 30_000}

    assert safe_native_message(message) == native_wire_row(message).row
    assert isinstance(safe_native_message(message), dict)


def test_the_wire_bound_is_documented_as_a_wire_bound():
    """The whole defect was a wire bound that read as a persistence bound.

    Naming is the fix that prevents recurrence, so it is pinned: the constant's
    own documentation has to say what it governs. Checked on the module doc
    comment rather than on layout.
    """

    from agent_runtime import persona_chat_continuity

    source = inspect.getsource(persona_chat_continuity)
    header = source[: source.index("_MAX_CONTENT = ")]
    assert "WIRE BOUND, NOT A PERSISTENCE BOUND" in header, (
        "the ceiling that governs the prompt must say so where it is defined"
    )
