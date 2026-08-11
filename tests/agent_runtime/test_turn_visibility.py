"""One typed answer to "did this turn produce anything a human can see".

The fact used to be re-derived by every lane that needed it, from whatever
evidence was in reach — the delivery drain compared reply text to the empty
string, cron wrote an English sentence into `error`, cron-health read that
sentence back with a substring match, the error classifier kept five phrasings
of it, and the gateway had both a predicate and a prose filter. They disagreed
about whether an empty turn was an error, normal traffic, or something to
suppress. Everything here pins the single authority that replaces them.

The rule the whole module turns on: **only PROVEN silence is silence.** Missing
evidence is `unknown`, and `unknown` is neither visible nor silent — a bool
would have forced that third case to masquerade as one of the other two, which
is exactly how a delivery into an empty transcript came to be reported as a
clean delivery.
"""

from __future__ import annotations

import pytest

from agent_runtime.turn_visibility import (
    TURN_VISIBILITY_KEY,
    TurnVisibility,
    VisibilityReason,
    VisibilityState,
    classify_turn_visibility,
)


def _assistant(finish_reason: str, content: str = "") -> dict:
    return {"role": "assistant", "content": content, "finish_reason": finish_reason}


# ---------------------------------------------------------------------------
# 1. the truth table
# ---------------------------------------------------------------------------


def test_text_is_visible():
    result = classify_turn_visibility(reply_text="3 failures, all in the chat panel")

    assert result.state is VisibilityState.VISIBLE
    assert result.reason is VisibilityReason.CONTENT
    assert result.is_visible and not result.is_silent
    assert result.reply_chars == len("3 failures, all in the chat panel")


@pytest.mark.parametrize("empty", ["", "   ", "\n\t "])
def test_an_empty_reply_is_measured_silence(empty):
    """Reported and empty. NOT the same as "not reported" — see below."""

    result = classify_turn_visibility(reply_text=empty)

    assert result.state is VisibilityState.SILENT
    assert result.reason is VisibilityReason.EMPTY
    assert result.reply_chars == 0


def test_no_reply_reported_is_unknown_not_silence():
    """`None` means nobody said. Calling that silence invents an incident."""

    result = classify_turn_visibility(reply_text=None)

    assert result.state is VisibilityState.UNKNOWN
    assert result.reason is VisibilityReason.NO_EVIDENCE
    assert not result.is_silent and not result.is_visible


@pytest.mark.parametrize(
    "finish_reason,expected",
    [
        ("incomplete", VisibilityReason.TRUNCATED),
        ("length", VisibilityReason.TRUNCATED),
        ("max_output_tokens", VisibilityReason.TRUNCATED),
        ("content_filter", VisibilityReason.FILTERED),
        ("content_policy_blocked", VisibilityReason.FILTERED),
    ],
)
def test_the_finish_reason_explains_an_empty_reply(finish_reason, expected):
    result = classify_turn_visibility(
        reply_text="", messages=[_assistant(finish_reason)]
    )

    assert result.state is VisibilityState.SILENT
    assert result.reason is expected
    assert result.finish_reason == finish_reason


def test_an_unrecognised_finish_reason_does_not_invent_a_cause():
    """`stop` with no content is a DIFFERENT incident from a truncation.

    Upgrading it to `truncated` would assert a cause nobody proved — and would
    hide the strangest case of all: a model that stopped normally, having said
    nothing.
    """

    result = classify_turn_visibility(reply_text="", messages=[_assistant("stop")])

    assert result.reason is VisibilityReason.EMPTY
    assert result.finish_reason == "stop"


def test_a_failed_run_explains_an_otherwise_unexplained_silence():
    result = classify_turn_visibility(
        reply_text="", raw={"failed": True, "error": "provider returned nothing"}
    )

    assert result.reason is VisibilityReason.FAILED


def test_a_real_finish_reason_outranks_the_error_key():
    """`error` is set on soft and partial outcomes too.

    Letting it win would relabel every truncation as a failure, which is the
    exact conflation the cron lane made when it wrote "empty response (model
    error, timeout, or misconfiguration)" for all of them at once.
    """

    result = classify_turn_visibility(
        reply_text="",
        messages=[_assistant("incomplete")],
        raw={"error": "partial", "partial": True},
    )

    assert result.reason is VisibilityReason.TRUNCATED


def test_truncation_never_overrides_content():
    """A cut-off turn that still produced words is a SHORT ANSWER, not silence.

    The operator can read it. Reporting it as silent would send someone
    hunting for a delivery failure that did not happen.
    """

    result = classify_turn_visibility(
        reply_text="here is what I found so far", messages=[_assistant("length")]
    )

    assert result.state is VisibilityState.VISIBLE
    assert result.finish_reason == "length"


# ---------------------------------------------------------------------------
# 2. which message the finish reason comes from
# ---------------------------------------------------------------------------


def test_the_finish_reason_comes_from_the_LAST_assistant_message():
    """Tool-call rounds carry their own reasons and would mis-explain the turn."""

    result = classify_turn_visibility(
        reply_text="",
        messages=[
            {"role": "user", "content": "run the suite", "finish_reason": "stop"},
            _assistant("tool_calls", "calling terminal"),
            {"role": "tool", "content": "ok", "finish_reason": "length"},
            _assistant("incomplete"),
        ],
    )

    assert result.finish_reason == "incomplete"
    assert result.reason is VisibilityReason.TRUNCATED


def test_messages_without_an_assistant_row_leave_the_finish_reason_empty():
    result = classify_turn_visibility(
        reply_text="", messages=[{"role": "user", "finish_reason": "stop"}]
    )

    assert result.finish_reason == ""
    assert result.reason is VisibilityReason.EMPTY


# ---------------------------------------------------------------------------
# 3. totality — this runs inside an exception handler and on the stdout path
# ---------------------------------------------------------------------------


class _Hostile:
    def __str__(self):
        raise RuntimeError("no string for you")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"reply_text": _Hostile()},
        {"reply_text": "", "messages": _Hostile()},
        {"reply_text": "", "messages": [None, 7, "not a dict"]},
        {"reply_text": "", "raw": "not a mapping"},
    ],
)
def test_classification_never_raises(kwargs):
    """A raise here would replace a real failure with this function's own.

    One of the four call sites is an exception handler and all four are on the
    path that writes the single JSON object serve's frame protocol expects.
    """

    assert classify_turn_visibility(**kwargs) is not None


# ---------------------------------------------------------------------------
# 4. the wire shape, and reading it back
# ---------------------------------------------------------------------------


def test_the_block_round_trips():
    original = classify_turn_visibility(
        reply_text="", messages=[_assistant("content_filter")]
    )

    restored = TurnVisibility.from_dict(original.as_dict())

    assert restored == original


@pytest.mark.parametrize(
    "block",
    [
        None,
        "not a dict",
        {},
        {"state": "sideways", "reason": "content"},
        {"state": "silent", "reason": "invented"},
    ],
)
def test_an_unreadable_block_is_unknown_not_a_crash(block):
    """Read off payloads this process did not build: older serve, test doubles."""

    assert TurnVisibility.from_dict(block).state is VisibilityState.UNKNOWN


def test_a_hostile_reply_chars_value_does_not_break_the_read():
    restored = TurnVisibility.from_dict(
        {"state": "silent", "reason": "empty", "reply_chars": "lots"}
    )

    assert restored.state is VisibilityState.SILENT
    assert restored.reply_chars == 0


# ---------------------------------------------------------------------------
# 5. from_payload — the block is authority, reply text is the fallback
# ---------------------------------------------------------------------------


def test_the_typed_block_beats_the_reply_text():
    """The block was computed at the source, with evidence the reader lacks.

    A reader that preferred its own re-derivation would be the second authority
    this module exists to retire.
    """

    payload = {
        "ok": True,
        "reply": "",
        TURN_VISIBILITY_KEY: {
            "state": "visible",
            "reason": "content",
            "finish_reason": "stop",
            "reply_chars": 42,
        },
    }

    assert TurnVisibility.from_payload(payload).is_visible


def test_a_payload_without_the_block_falls_back_to_reply_text():
    """A landed fix is not a running one: serve keeps its old code until restart."""

    assert TurnVisibility.from_payload({"ok": True, "reply": ""}).is_silent
    assert TurnVisibility.from_payload({"ok": True, "reply": "done"}).is_visible


@pytest.mark.parametrize("payload", [None, "nope", {}, {"ok": True}])
def test_a_payload_with_no_evidence_at_all_is_unknown(payload):
    result = TurnVisibility.from_payload(payload)

    assert result.state is VisibilityState.UNKNOWN
    assert not result.is_silent


# ---------------------------------------------------------------------------
# 6. describe() — one phrasing, so nobody has to grep for another one
# ---------------------------------------------------------------------------


def test_describe_names_the_cause_and_carries_the_finish_reason():
    line = classify_turn_visibility(
        reply_text="", messages=[_assistant("incomplete")]
    ).describe()

    assert "empty reply" in line
    assert "cut off" in line
    assert "finish_reason=incomplete" in line


def test_describe_distinguishes_unknown_from_silence():
    assert "unknown" in TurnVisibility.unknown().describe()
