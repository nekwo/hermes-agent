"""Operator-facing curation of the agent's raw working session (audit Stage 2C / S3).

The persona instance binds the agent's *internal* session, whose raw rows are
verbose tick-context prompts and serialized decision dicts. The operator must
see a clean transcript: decision summaries, no internal scaffolding/tool/system
noise. Regression guard for the live-smoke breakage (raw JSON + tick context
leaking into the Agent Console).
"""

import json

from agent_runtime.persona_chat_history import _safe_recent_messages


class FakeSessionDB:
    def __init__(self, messages):
        self._messages = messages

    def get_messages(self, session_id, include_inactive=False):
        return list(self._messages)


_DECISION = json.dumps(
    {
        "type": "propose_acceptance",
        "summary": "Route the greeting as a scope clarification.",
        "rationale": "The mission description is only 'hi'.",
        "payload": {"objective": "Clarify", "risk_flags": ["scope_missing"]},
        "handoff_packet": {"packet_kind": "fresh_scope"},
    }
)


def test_decision_dict_collapses_to_summary_and_rationale():
    db = FakeSessionDB([{"role": "assistant", "content": _DECISION}])
    rows, status = _safe_recent_messages(db, session_id="s1")
    assert status == "safe"
    assert len(rows) == 1
    assert rows[0]["role"] == "agent"
    assert "Route the greeting as a scope clarification." in rows[0]["safe_text"]
    assert "The mission description is only 'hi'." in rows[0]["safe_text"]
    # Internal structure must not leak.
    assert "risk_flags" not in rows[0]["safe_text"]
    assert "handoff_packet" not in rows[0]["safe_text"]
    assert "payload" not in rows[0]["safe_text"]


def test_internal_scaffolding_operator_rows_are_dropped():
    db = FakeSessionDB(
        [
            {"role": "user", "content": "# Agent Runtime Tick Context\n## Task\n- id: task_1\n..."},
            {"role": "assistant", "content": _DECISION},
        ]
    )
    rows, _ = _safe_recent_messages(db, session_id="s1")
    # Only the curated agent reply survives; the tick-context prompt is dropped.
    assert [r["role"] for r in rows] == ["agent"]


def test_clean_operator_message_is_kept():
    db = FakeSessionDB([{"role": "user", "content": "hi neko"}])
    rows, _ = _safe_recent_messages(db, session_id="s1")
    assert len(rows) == 1
    assert rows[0]["role"] == "operator"
    assert rows[0]["safe_text"] == "hi neko"


def test_tool_system_and_empty_rows_are_dropped():
    db = FakeSessionDB(
        [
            {"role": "system", "content": "you are an agent"},
            {"role": "tool", "content": '{"success": true}'},
            {"role": "assistant", "content": ""},
            {"role": "assistant", "content": _DECISION},
        ]
    )
    rows, _ = _safe_recent_messages(db, session_id="s1")
    assert [r["role"] for r in rows] == ["agent"]


def test_unparseable_raw_dict_is_not_shown():
    # A serialized dict we can't parse must not dump as raw JSON to the operator.
    db = FakeSessionDB([{"role": "assistant", "content": '{"type": broken json'}])
    rows, _ = _safe_recent_messages(db, session_id="s1")
    assert rows == []
