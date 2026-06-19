"""Unit coverage for the persona chat SessionDB persistence path (audit Stage 2A/2B).

These exercise the harness helpers that wire free-floating persona turns into the
shared SessionDB: operator/agent persistence, multi-turn continuity, and the
redaction-on-write boundary. The previous gap was that this orchestration was
exception-wrapped and never directly asserted, so a regression would fail silently.
"""

from hermes_cli.harness import (
    _append_persona_decision_reply,
    _append_persona_operator_turn,
    _persona_chat_message_with_history,
    _redact_persona_chat_text,
)


class FakeSessionDB:
    def __init__(self):
        self.messages: dict[str, list[dict]] = {}
        self.sessions: dict[str, dict] = {}

    def create_session(self, session_id, source, **kwargs):
        self.sessions[session_id] = {"source": source, **kwargs}
        self.messages.setdefault(session_id, [])
        return session_id

    def append_message(self, session_id, role, content=None, **kwargs):
        self.messages.setdefault(session_id, []).append({"role": role, "content": content})

    def get_messages(self, session_id, include_inactive=False):
        return list(self.messages.get(session_id, []))


def test_operator_turn_is_persisted():
    db = FakeSessionDB()
    _append_persona_operator_turn(session_db=db, session_id="s1", message="hi neko")
    assert db.get_messages("s1") == [{"role": "user", "content": "hi neko"}]


def test_decision_reply_persisted_and_deduped():
    db = FakeSessionDB()
    _append_persona_decision_reply(
        session_db=db, session_id="s1", summary="Scoped the task", rationale="No criteria yet"
    )
    # Re-appending the identical decision must not duplicate the assistant turn.
    _append_persona_decision_reply(
        session_db=db, session_id="s1", summary="Scoped the task", rationale="No criteria yet"
    )
    assistant = [m for m in db.get_messages("s1") if m["role"] == "assistant"]
    assert len(assistant) == 1
    assert "Scoped the task" in assistant[0]["content"]
    assert "No criteria yet" in assistant[0]["content"]


def test_continuity_prepends_prior_turns():
    db = FakeSessionDB()
    _append_persona_operator_turn(session_db=db, session_id="s1", message="first")
    _append_persona_decision_reply(session_db=db, session_id="s1", summary="ack", rationale=None)
    enriched = _persona_chat_message_with_history(session_db=db, session_id="s1", message="second")
    assert "Prior persona chat context" in enriched
    assert "Operator: first" in enriched
    assert "Agent: ack" in enriched
    assert enriched.strip().endswith("second")


def test_no_history_returns_bare_message():
    db = FakeSessionDB()
    assert _persona_chat_message_with_history(session_db=db, session_id="s1", message="hi") == "hi"


def test_none_session_db_is_safe():
    # Must not raise when SessionDB is unavailable.
    _append_persona_operator_turn(session_db=None, session_id="s1", message="hi")
    assert _persona_chat_message_with_history(session_db=None, session_id="s1", message="hi") == "hi"


def test_redaction_on_write_operator_turn():
    db = FakeSessionDB()
    _append_persona_operator_turn(
        session_db=db, session_id="s1", message="my api_key=sk-supersecret123 ok"
    )
    written = db.get_messages("s1")[0]["content"]
    assert "sk-supersecret123" not in written
    assert "[redacted]" in written


def test_redaction_on_write_decision_reply():
    db = FakeSessionDB()
    _append_persona_decision_reply(
        session_db=db,
        session_id="s1",
        summary="token: ghp_leakedtoken00000",
        rationale="all good",
    )
    written = db.get_messages("s1")[0]["content"]
    assert "ghp_leakedtoken00000" not in written
    assert "[redacted]" in written


def test_redaction_on_write_continuity_context():
    db = FakeSessionDB()
    # A prior turn containing a secret must not be echoed verbatim into the
    # enriched context handed to the model.
    db.append_message("s1", "user", "password: hunter2secret")
    enriched = _persona_chat_message_with_history(session_db=db, session_id="s1", message="next")
    assert "hunter2secret" not in enriched
    assert "[redacted]" in enriched


def test_redactor_passes_clean_text():
    assert _redact_persona_chat_text("just a normal message", limit=100) == "just a normal message"
    assert _redact_persona_chat_text(None, limit=100) == ""
