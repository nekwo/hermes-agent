"""A turn that ended saying nothing must project SOMETHING (2026-08-12).

The live incident, reproduced verbatim below from
``profiles/base/state.db`` rows 2750-2758: an operator asks for a background
task, the agent starts it, and then answers with three empty ``incomplete``
rows. The harness later forges the completion notice into the same thread, and
that turn answers with two more empty rows. Every one of those rows was dropped
by ``_curate_chat_message_text``, so the transcript projected exactly two rows —
the request and the completion notice, each apparently answered by nothing at
all.

Dropping empty assistant rows is not itself wrong: the same profiles hold
22,179 of them that are intermediate tool-call rounds, and rendering those would
put a blank bubble under every tool call. The defect was that nothing on the row
separated the two, so the 5 real silences went out with the scaffolding.
"""

from agent_runtime.persona_chat_history import (
    PERSONA_TURN_SILENT_KIND,
    SILENT_TURN_MARKER_TEXTS,
    _safe_curated_messages,
)
from agent_runtime.turn_visibility import (
    SILENT_REASONS,
    TURN_VISIBILITY_KEY,
    VisibilityReason,
)

SESSION = "persona_chat_personainst_silent_turn"
TURN = "agent-chat-send-149683f2"


class FakeSessionDB:
    def __init__(self, messages):
        self._messages = messages

    def get_messages(self, session_id, include_inactive=False):
        return list(self._messages)

    def resolve_resume_session_id(self, session_id):
        return session_id


def _row(row_id, role, content, *, finish_reason=None, tool_calls=None, seq=None, turn=TURN):
    return {
        "id": str(row_id),
        "session_id": SESSION,
        "role": role,
        "content": content,
        "finish_reason": finish_reason,
        "tool_calls": tool_calls,
        "platform_message_id": turn if seq is None else f"{turn}:assistant:{seq}",
        "timestamp": 1786460649.0 + row_id,
    }


def _project(messages):
    rows, status, error = _safe_curated_messages(
        session_id=SESSION, session_db=FakeSessionDB(messages)
    )
    assert error is None and status == "safe"
    return rows


def _kinds(rows):
    return [row.get("kind") or row["role"] for row in rows]


def test_silent_turn_projects_a_marker_instead_of_nothing():
    rows = _project(
        [
            _row(1, "user", "Start a 30-second background task."),
            _row(2, "assistant", "", finish_reason="incomplete", seq=1),
        ]
    )

    assert _kinds(rows) == ["operator", PERSONA_TURN_SILENT_KIND]
    marker = rows[-1]
    assert marker["role"] == "system"
    assert marker["text"] == SILENT_TURN_MARKER_TEXTS[VisibilityReason.TRUNCATED]
    assert marker[TURN_VISIBILITY_KEY] == {
        "state": "silent",
        "reason": "truncated",
        "finish_reason": "incomplete",
        "reply_chars": 0,
    }


def test_marker_body_is_never_empty_for_any_silent_reason():
    # A marker row with no body is dropped by the transcript reader, which would
    # restore the original defect through the back door. The table is proven
    # total against the visibility authority's own vocabulary, not a copy of it.
    assert SILENT_REASONS <= set(SILENT_TURN_MARKER_TEXTS)
    assert all(text.strip() for text in SILENT_TURN_MARKER_TEXTS.values())


def test_retried_silences_in_one_turn_collapse_to_one_marker():
    # Rows 2753/2754/2755 of the live incident: one turn, three attempts, each
    # persisting its own empty row. Three markers would misreport one silent
    # turn as three.
    rows = _project(
        [
            _row(1, "user", "Start a 30-second background task."),
            _row(2, "assistant", "", finish_reason="incomplete", seq=3),
            _row(3, "assistant", "", finish_reason="incomplete", seq=4),
            _row(4, "assistant", "", finish_reason="incomplete", seq=5),
        ]
    )

    assert _kinds(rows) == ["operator", PERSONA_TURN_SILENT_KIND]


def test_a_turn_whose_retry_spoke_gets_no_marker():
    # The failure that makes a marker WORSE than the blank: attempt one comes
    # back empty, attempt two answers. The operator saw a reply, so claiming the
    # turn ended in silence would be a fresh lie on top of a real message.
    rows = _project(
        [
            _row(1, "user", "Start a 30-second background task."),
            _row(2, "assistant", "", finish_reason="incomplete", seq=3),
            _row(3, "assistant", "Started it.", finish_reason="stop", seq=4),
        ]
    )

    assert _kinds(rows) == ["operator", "agent"]
    assert rows[-1]["text"] == "Started it."


def test_tool_call_scaffolding_is_still_dropped():
    # The 22,179-row majority. An assistant row is empty here because the model
    # called a tool, not because the turn ended.
    rows = _project(
        [
            _row(1, "user", "Start a 30-second background task."),
            _row(
                2,
                "assistant",
                "",
                finish_reason="tool_calls",
                tool_calls=[{"id": "call_54uAB", "type": "function"}],
                seq=1,
            ),
            _row(3, "assistant", "Started it.", finish_reason="stop", seq=3),
        ]
    )

    assert _kinds(rows) == ["operator", "agent"]


def test_a_turn_ending_on_a_tool_call_does_not_become_a_marker():
    # The case the test above CANNOT see: there, the turn also had a spoken
    # reply, so the visible-turn subtraction removed the candidate no matter
    # what the classifier said about scaffolding. Here the tool-call row is the
    # last thing in the turn, so nothing else can hide a misclassification —
    # only the scaffolding suppression itself keeps this transcript clean.
    rows = _project(
        [
            _row(1, "user", "Start a 30-second background task."),
            _row(
                2,
                "assistant",
                "",
                finish_reason="tool_calls",
                tool_calls=[{"id": "call_54uAB", "type": "function"}],
                seq=1,
            ),
        ]
    )

    assert _kinds(rows) == ["operator"]


def test_forged_delivery_turn_that_ends_silent_is_rendered():
    # Rows 2756-2758: the harness brings back the answer to dispatched work, and
    # THAT turn ends silent too. This is the case the operator actually hit —
    # a completion notice sitting in the thread answered by nothing.
    delivery_turn = "bg-completion-completion:proc_5e591b91a09c"
    rows = _project(
        [
            _row(1, "user", "Start a 30-second background task."),
            _row(2, "assistant", "", finish_reason="incomplete", seq=3),
            {
                **_row(3, "user", "[IMPORTANT: Background process completed]", turn=delivery_turn),
                "finish_reason": "harness_delivery::0:unknown",
            },
            _row(4, "assistant", "", finish_reason="incomplete", seq=7, turn=delivery_turn),
        ]
    )

    assert _kinds(rows) == [
        "operator",
        PERSONA_TURN_SILENT_KIND,
        "harness_delivery",
        PERSONA_TURN_SILENT_KIND,
    ]


def test_marker_takes_the_terminal_position_of_the_turn_it_stands_for():
    # A turn has a reply or a marker, never both, so the marker inherits the
    # reply's ordering slot. Without it the marker sorts on the pre-C8 timestamp
    # fallback and can drift away from the turn it describes.
    rows = _project(
        [
            _row(1, "user", "Start a 30-second background task."),
            _row(2, "assistant", "", finish_reason="incomplete", seq=3),
        ]
    )

    marker = rows[-1]
    assert marker["turn_id"] == rows[0]["turn_id"]
    assert marker["turn_seq"] > rows[0]["turn_seq"]


def test_unpresentable_but_non_empty_agent_rows_are_still_dropped():
    # The curator also rejects rows that HAVE content — a raw serialized dict.
    # Those are not silences and must not acquire a marker.
    rows = _project(
        [
            _row(1, "user", "Start a 30-second background task."),
            _row(2, "assistant", '{"unparseable": ', finish_reason="stop", seq=1),
        ]
    )

    assert _kinds(rows) == ["operator"]
