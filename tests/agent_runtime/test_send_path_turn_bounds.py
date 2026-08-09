"""The composed operator turn survives to the WIRE, and the record says so.

Pinned here rather than at either half of the seam, because both halves were
already correct on 2026-08-09 and only the JOIN was broken: the composer built a
well-formed ``message · skill_preload · runtime_context`` row, the envelope
grammar deduped correctly, and the flat 20,000-char cap between them amputated
the row mid-skill — taking the HUD and the ``</skill_preload>`` closing tag with
it, which in turn made the ``unchanged`` dedupe structurally unreachable.

Every fixture below therefore uses a preload that GENUINELY exceeds the old cap
with a structured envelope that must survive intact. A fixture that fits under
20,000 chars makes the broken and the fixed boundary identical and would pass
either way.
"""

from __future__ import annotations

import pytest

from agent_runtime.persona_chat_continuity import (
    BOUND_ACTION_DROPPED,
    BOUND_ACTION_TRUNCATED,
    BOUND_PART_MESSAGE,
    BOUND_PART_RUNTIME_CONTEXT,
    BOUND_PART_SKILL_PRELOAD,
    _MAX_CONTENT,
    _MAX_RUNTIME_CONTEXT_CONTENT,
    _MAX_USER_ROW_CONTENT,
    bound_composed_user_content,
    safe_native_history,
    safe_native_message,
)
from agent_runtime.runtime_hud import (
    RUNTIME_CONTEXT_CODEC,
    SKILL_PRELOAD_CODEC,
    SKILL_PRELOAD_DELIVERY_SNAPSHOT,
    SKILL_PRELOAD_DELIVERY_UNCHANGED,
    extract_runtime_context_envelope,
    extract_skill_preload_envelope,
    render_runtime_context_envelope,
    render_skill_preload_envelope,
    skill_preload_delivery,
    skill_preload_revision,
    split_composed_user_row,
)


# The qa persona's required preload (``launcher-stagec-mcp-screenshot``) measured
# 54,114 chars on 2026-08-09 — nearly 3x the old cap. Sized to match, and with a
# distinctive marker at the very END so "the whole body arrived" is checkable
# rather than assumed from a length.
SKILL_BODY_TAIL = "PITFALL: a refused screenshot means relaunch, never improvise."
HUD_BODY_TAIL = "with under 270 s left, wrap up and report."


def _skill_body(chars: int = 54_114) -> str:
    filler = "Step: capture the window through the MCP marionette path. " * (
        chars // 58 + 1
    )
    return filler[: chars - len(SKILL_BODY_TAIL)] + SKILL_BODY_TAIL


def _hud_envelope(context_id: str = "ctx_0123456789abcdef") -> str:
    return render_runtime_context_envelope(
        context_id=context_id,
        revision="hud_" + "a" * 16,
        delivery="snapshot",
        situational_hud_content="## Runtime Situation\nscope: eternia · launcher",
        volatile_content=f"Turn budget: {HUD_BODY_TAIL}",
    )


def _composed_row(
    *, message: str = "take a screenshot of the NEWS tab", skill_chars: int = 54_114
) -> tuple[str, str]:
    """One composed operator row plus the skill revision it carries."""

    body = _skill_body(skill_chars)
    revision = skill_preload_revision(body)
    preload = render_skill_preload_envelope(
        skill_names=["launcher-stagec-mcp-screenshot"],
        skill_preload_content=body,
        revision=revision,
        delivery=SKILL_PRELOAD_DELIVERY_SNAPSHOT,
    )
    return "\n\n".join([message, preload, _hud_envelope()]), revision


# ---------------------------------------------------------------------------
# The splitter is exact — everything below depends on it being lossless
# ---------------------------------------------------------------------------


def test_splitting_a_composed_row_is_lossless_including_separators():
    composed, _ = _composed_row()

    parts = split_composed_user_row(composed)

    assert parts.joined == composed
    assert parts.message == "take a screenshot of the NEWS tab"
    assert parts.skill_preload.endswith("</skill_preload>")
    assert parts.runtime_context.endswith("</runtime_context>")
    assert parts.has_envelope


def test_a_row_with_no_envelope_is_all_message():
    parts = split_composed_user_row("just a sentence from the operator")

    assert parts.message == "just a sentence from the operator"
    assert (parts.skill_preload, parts.runtime_context) == ("", "")
    assert not parts.has_envelope


# ---------------------------------------------------------------------------
# T1 — the wire row keeps the whole skill, the HUD, and a well-formed envelope
# ---------------------------------------------------------------------------


def test_an_oversized_preload_reaches_the_wire_whole():
    composed, _ = _composed_row()
    assert len(composed) > 50_000, "fixture must exceed the retired 20k cap"

    wire = safe_native_message({"role": "user", "content": composed})["content"]

    parts = split_composed_user_row(wire)
    body = SKILL_PRELOAD_CODEC.body(parts.skill_preload)
    assert body is not None
    assert body.endswith(SKILL_BODY_TAIL), "the skill's tail must survive"
    assert len(body) == len(_skill_body())


def test_the_runtime_hud_reaches_the_wire_on_a_big_preload_persona():
    composed, _ = _composed_row()

    wire = safe_native_message({"role": "user", "content": composed})["content"]

    _, metadata = extract_runtime_context_envelope(wire)
    assert metadata is not None, "the HUD envelope must survive the boundary"
    assert HUD_BODY_TAIL in wire, "the wall-budget countdown must reach the model"


def test_the_operator_text_survives_verbatim():
    composed, _ = _composed_row(message="deploy nothing; just look at the NEWS tab")

    wire = safe_native_message({"role": "user", "content": composed})["content"]

    assert split_composed_user_row(wire).message == (
        "deploy nothing; just look at the NEWS tab"
    )


def test_turn_two_delivers_unchanged_against_the_persisted_turn_one_row():
    """The dedupe the truncation defeated: turn 2 must NOT re-ship the body."""

    composed, revision = _composed_row()
    persisted = safe_native_history([{"role": "user", "content": composed}])

    assert skill_preload_delivery(persisted, revision) == SKILL_PRELOAD_DELIVERY_UNCHANGED


def test_the_unchanged_stub_is_two_orders_of_magnitude_smaller():
    """Why the dedupe matters: the measured +4.2k tokens/turn thread growth."""

    composed, revision = _composed_row()
    persisted = safe_native_history([{"role": "user", "content": composed}])
    delivery = skill_preload_delivery(persisted, revision)

    stub = render_skill_preload_envelope(
        skill_names=["launcher-stagec-mcp-screenshot"],
        skill_preload_content=_skill_body(),
        revision=revision,
        delivery=delivery,
    )

    assert len(stub) < 400
    assert len(stub) * 100 < len(composed)


def test_persisting_the_wire_row_again_is_stable():
    """Warm memory and cold persistence must read the same bytes."""

    composed, _ = _composed_row()

    once = safe_native_message({"role": "user", "content": composed})["content"]
    twice = safe_native_message({"role": "user", "content": once})["content"]

    assert twice == once


# ---------------------------------------------------------------------------
# T1 — what the bound does when it genuinely has to cut
# ---------------------------------------------------------------------------


def test_an_over_ceiling_preload_is_cut_inside_a_still_well_formed_envelope():
    composed, revision = _composed_row(skill_chars=_MAX_USER_ROW_CONTENT * 2)

    bounded = bound_composed_user_content(composed)

    assert len(bounded.text) <= _MAX_USER_ROW_CONTENT
    remainder, metadata = extract_skill_preload_envelope(
        extract_runtime_context_envelope(bounded.text)[0]
    )
    assert metadata is not None, "the envelope must still be well formed"
    assert metadata["revision"] == revision
    assert metadata["delivery"] == SKILL_PRELOAD_DELIVERY_SNAPSHOT
    assert remainder == "take a screenshot of the NEWS tab"


def test_a_cut_is_reported_as_a_typed_note_never_silently():
    composed, _ = _composed_row(skill_chars=_MAX_USER_ROW_CONTENT * 2)

    bounded = bound_composed_user_content(composed)

    assert bounded.bounded
    assert [note.part for note in bounded.notes] == [BOUND_PART_SKILL_PRELOAD]
    note = bounded.notes[0]
    assert note.action == BOUND_ACTION_TRUNCATED
    assert note.original_chars > note.bounded_chars > 0


def test_a_row_that_fits_reports_nothing():
    composed, _ = _composed_row()

    bounded = bound_composed_user_content(composed)

    assert bounded.text == composed
    assert bounded.notes == ()
    assert not bounded.bounded


def test_the_hud_is_served_before_the_preload_so_it_cannot_be_squeezed_out():
    """The ordering IS the contract — a shared ceiling must fail soft."""

    composed, _ = _composed_row(skill_chars=_MAX_USER_ROW_CONTENT * 2)
    hud = _hud_envelope()

    bounded = bound_composed_user_content(composed)

    parts = split_composed_user_row(bounded.text)
    assert parts.runtime_context == f"\n\n{hud}", "the HUD is untouched"
    assert BOUND_PART_RUNTIME_CONTEXT not in {note.part for note in bounded.notes}


def test_operator_text_is_bounded_before_the_preload_too():
    composed, _ = _composed_row(
        message="M" * (_MAX_CONTENT * 3), skill_chars=_MAX_USER_ROW_CONTENT * 2
    )

    bounded = bound_composed_user_content(composed)

    parts = split_composed_user_row(bounded.text)
    assert len(parts.message) <= _MAX_CONTENT
    assert parts.message.endswith("… [truncated]"), "a cut says so, in band"
    by_part = {note.part: note for note in bounded.notes}
    assert set(by_part) == {BOUND_PART_MESSAGE, BOUND_PART_SKILL_PRELOAD}
    # HUD-first: the operator's 60k of text did not come out of the HUD.
    assert parts.runtime_context == f"\n\n{_hud_envelope()}"


def test_an_envelope_too_small_to_hold_its_tags_is_dropped_and_reported():
    """A part that cannot be well formed at its budget is dropped, never mangled."""

    hud = _hud_envelope()
    bounded, note = _bound_one(hud, limit=20, part=BOUND_PART_RUNTIME_CONTEXT)

    assert bounded == ""
    assert note is not None and note.action == BOUND_ACTION_DROPPED
    assert note.bounded_chars == 0


def _bound_one(envelope, *, limit, part):
    from agent_runtime.persona_chat_continuity import _bound_envelope

    return _bound_envelope(
        envelope, limit=limit, part=part, codec=RUNTIME_CONTEXT_CODEC
    )


# ---------------------------------------------------------------------------
# T1 — nothing outside the composed operator row changes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["assistant", "tool", "system"])
def test_non_user_rows_keep_the_flat_bound(role):
    row = safe_native_message({"role": role, "content": "x" * (_MAX_CONTENT * 2)})

    assert len(row["content"]) <= _MAX_CONTENT
    assert row["content"].endswith("… [truncated]")


def test_a_plain_user_row_with_no_envelope_keeps_the_flat_bound():
    row = safe_native_message({"role": "user", "content": "y" * (_MAX_CONTENT * 2)})

    assert len(row["content"]) <= _MAX_CONTENT
    assert row["content"].endswith("… [truncated]")


def test_the_hud_budget_is_a_slice_of_the_row_ceiling_not_a_rival_number():
    assert _MAX_RUNTIME_CONTEXT_CONTENT < _MAX_USER_ROW_CONTENT
    assert _MAX_CONTENT < _MAX_USER_ROW_CONTENT
