"""Unit coverage for the persona chat SessionDB persistence path (audit Stage 2A/2B).

These exercise the harness helpers that wire free-floating persona turns into the
shared SessionDB: operator/agent persistence, multi-turn continuity, and the
redaction-on-write boundary. The previous gap was that this orchestration was
exception-wrapped and never directly asserted, so a regression would fail silently.
"""

import pytest

from hermes_cli.harness import (
    _append_persona_assistant_text,
    _append_persona_operator_turn,
    _ensure_persona_chat_session,
    _persona_chat_session_owner,
    _redact_persona_chat_text,
    _update_persona_chat_token_counts,
)
from agent_runtime.persona_chat_continuity import safe_native_history


class FakeSessionDB:
    def __init__(self):
        self.messages: dict[str, list[dict]] = {}
        self.sessions: dict[str, dict] = {}
        self.titles: dict[str, str] = {}
        self.token_updates: list[dict] = []

    def create_session(self, session_id, source, **kwargs):
        self.sessions[session_id] = {"source": source, **kwargs}
        self.messages.setdefault(session_id, [])
        return session_id

    def append_message(self, session_id, role, content=None, **kwargs):
        self.messages.setdefault(session_id, []).append({"role": role, "content": content})

    def get_messages(self, session_id, include_inactive=False):
        return list(self.messages.get(session_id, []))

    def get_session(self, session_id):
        session = self.sessions.get(session_id)
        return {"id": session_id, **session} if session is not None else None

    def get_session_title(self, session_id):
        return self.titles.get(session_id)

    def set_session_title(self, session_id, title):
        self.titles[session_id] = title

    def update_token_counts(
        self,
        session_id,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
        api_call_count=0,
        model=None,
    ):
        self.token_updates.append(
            {
                "session_id": session_id,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read_tokens,
                "cache_write_tokens": cache_write_tokens,
                "reasoning_tokens": reasoning_tokens,
                "api_call_count": api_call_count,
                "model": model,
            }
        )


def test_operator_turn_is_persisted():
    db = FakeSessionDB()
    _append_persona_operator_turn(session_db=db, session_id="s1", message="hi neko")
    assert db.get_messages("s1") == [{"role": "user", "content": "hi neko"}]


def test_ensure_session_returns_true_when_title_already_exists():
    db = FakeSessionDB()
    db.titles["s1"] = "Existing title"

    assert _ensure_persona_chat_session(
        session_db=db,
        session_id="s1",
        persona_id="dev",
        title="Replacement title",
        required=True,
    ) is True
    assert db.titles["s1"] == "Existing title"


def test_ensure_session_persists_exact_owner_metadata_for_future_roots():
    db = FakeSessionDB()
    session_id = "persona_chat_personainst_neko_supervisor_agent_f6f7a51b_012345abcdef"

    assert _ensure_persona_chat_session(
        session_db=db,
        session_id=session_id,
        persona_id="neko_supervisor",
        required=True,
    ) is True

    assert db.sessions[session_id]["model_config"] == {
        "source": "agent_runtime_persona_chat",
        "persona_id": "neko_supervisor",
        "persona_instance_id": "personainst_neko_supervisor_agent_f6f7a51b",
    }
    assert _persona_chat_session_owner(db, session_id) == (
        "personainst_neko_supervisor_agent_f6f7a51b"
    )


def test_legacy_persona_chat_without_duplicate_metadata_keeps_exact_owner():
    db = FakeSessionDB()
    session_id = "persona_chat_personainst_neko_supervisor_agent_f6f7a51b_012345abcdef"
    db.create_session(session_id, "agent_runtime_persona_chat")

    assert _persona_chat_session_owner(db, session_id) == (
        "personainst_neko_supervisor_agent_f6f7a51b"
    )


def test_persona_chat_owner_rejects_non_chat_source_and_conflicting_metadata():
    session_id = "persona_chat_personainst_dev_012345abcdef"
    wrong_source = FakeSessionDB()
    wrong_source.create_session(session_id, "cli")
    assert _persona_chat_session_owner(wrong_source, session_id) is None

    conflicting = FakeSessionDB()
    conflicting.create_session(
        session_id,
        "agent_runtime_persona_chat",
        model_config={
            "source": "agent_runtime_persona_chat",
            "persona_instance_id": "personainst_qa",
        },
    )
    assert _persona_chat_session_owner(conflicting, session_id) is None


def test_assistant_turn_is_persisted():
    db = FakeSessionDB()
    _append_persona_assistant_text(session_db=db, session_id="s1", text="hey, doing great")
    assert db.get_messages("s1") == [{"role": "assistant", "content": "hey, doing great"}]


def test_persona_chat_token_counts_are_persisted():
    # The bound-session write must forward the COMPLETE canonical usage — cache
    # reads/writes and reasoning, not just input/output. Dropping the cache
    # buckets here is what previously left the Launcher's source-of-truth session
    # reporting zero cache; this asserts the accounting stays canonical.
    class Result:
        input_tokens = 120
        output_tokens = 30
        cache_read_tokens = 9000
        cache_write_tokens = 300
        reasoning_tokens = 45
        api_calls = 2
        model = "gpt-test"

    db = FakeSessionDB()
    _update_persona_chat_token_counts(session_db=db, session_id="s1", result=Result())

    assert db.token_updates == [
        {
            "session_id": "s1",
            "input_tokens": 120,
            "output_tokens": 30,
            "cache_read_tokens": 9000,
            "cache_write_tokens": 300,
            "reasoning_tokens": 45,
            "api_call_count": 2,
            "model": "gpt-test",
        }
    ]


def test_persona_chat_records_a_cache_only_turn():
    # A turn that was served entirely from cache (no fresh full-price input, no
    # completion recorded on this hop) still carries billable cache activity —
    # the write must not be skipped just because input/output are zero.
    class Result:
        input_tokens = 0
        output_tokens = 0
        cache_read_tokens = 20000
        cache_write_tokens = 0
        reasoning_tokens = 0
        api_calls = 0
        model = "gpt-test"

    db = FakeSessionDB()
    _update_persona_chat_token_counts(session_db=db, session_id="s1", result=Result())

    assert len(db.token_updates) == 1
    assert db.token_updates[0]["cache_read_tokens"] == 20000


def test_continuity_preserves_prior_turns_as_native_structure():
    db = FakeSessionDB()
    _append_persona_operator_turn(session_db=db, session_id="s1", message="first")
    _append_persona_assistant_text(session_db=db, session_id="s1", text="ack")
    history = safe_native_history(db.get_messages("s1"))
    assert history == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ack"},
    ]


def test_no_history_returns_empty_native_structure():
    db = FakeSessionDB()
    assert safe_native_history(db.get_messages("s1")) == []


def test_none_session_db_is_safe_for_optional_callers():
    assert not _append_persona_operator_turn(
        session_db=None, session_id="s1", message="hi"
    )
    assert not _append_persona_assistant_text(
        session_db=None, session_id="s1", text="reply"
    )


def test_redaction_on_write_operator_turn():
    db = FakeSessionDB()
    _append_persona_operator_turn(
        session_db=db, session_id="s1", message="my api_key=sk-supersecret123 ok"
    )
    written = db.get_messages("s1")[0]["content"]
    assert "sk-supersecret123" not in written
    assert "[redacted]" in written


def test_redaction_on_write_assistant_turn():
    db = FakeSessionDB()
    _append_persona_assistant_text(
        session_db=db,
        session_id="s1",
        text="here is the token: ghp_leakedtoken00000",
    )
    written = db.get_messages("s1")[0]["content"]
    assert "ghp_leakedtoken00000" not in written
    assert "[redacted]" in written


def test_redaction_on_native_continuity_history():
    db = FakeSessionDB()
    # A prior turn containing a secret must not be echoed verbatim into the
    # enriched context handed to the model.
    db.append_message("s1", "user", "password: hunter2secret")
    history = safe_native_history(db.get_messages("s1"))
    assert "hunter2secret" not in str(history)
    assert "***" in str(history)


def test_redactor_passes_clean_text():
    assert _redact_persona_chat_text("just a normal message", limit=100) == "just a normal message"
    assert _redact_persona_chat_text(None, limit=100) == ""


def test_chat_body_preserves_code_indentation_and_inline_spacing():
    # Message fidelity: persisted chat bodies must keep code indentation and
    # aligned columns — collapsing intra-line whitespace mangles agent replies.
    body = "def f():\n    return   1\n\ncol_a    col_b"
    assert _redact_persona_chat_text(body, limit=500) == body


def test_chat_body_truncation_is_marked_not_silent():
    safe = _redact_persona_chat_text("x" * 250, limit=200)
    assert safe.startswith("x" * 200)
    assert safe.endswith("… [truncated]")


# ── live-log mirror at the persist chokepoint ──────────────────────────────
#
# ``_persist_persona_chat_row`` is the ONE explicit-append seam both helpers
# funnel through, and the ONE place this lane mirrors from. These pin that the
# mirror rides the write (not a separate call site that can be forgotten) and
# that it inherits the redaction boundary rather than re-deciding it.


def _mirror_lines(session_id):
    import json

    from agent_runtime.chat_live_log import chat_live_log_path

    path = chat_live_log_path(session_id)
    if path is None or not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _mirror_messages(session_id):
    return [row for row in _mirror_lines(session_id) if row.get("kind") == "message"]


@pytest.fixture(autouse=True)
def _clean_live_log_state():
    from agent_runtime.chat_live_log import reset_chat_live_log_state

    reset_chat_live_log_state()
    yield
    reset_chat_live_log_state()


def test_persisted_rows_are_mirrored_into_the_live_log():
    db = FakeSessionDB()
    _append_persona_operator_turn(
        session_db=db, session_id="s1", message="hi neko", client_message_id="cm-1"
    )
    _append_persona_assistant_text(
        session_db=db, session_id="s1", text="on it", client_message_id="cm-1"
    )
    rows = _mirror_messages("s1")
    assert [(row["role"], row["text"]) for row in rows] == [
        ("operator", "hi neko"),
        ("agent", "on it"),
    ]
    assert [row["client_message_id"] for row in rows] == ["cm-1", "cm-1"]


def test_mirror_never_carries_a_secret_seeded_through_the_chokepoint():
    # The redactor runs BEFORE the mirror sees the text (the mirror re-applies
    # the shared rule as well), so a secret can never reach the greppable file.
    db = FakeSessionDB()
    _append_persona_operator_turn(
        session_db=db, session_id="s1", message="deploy with api_key=sk-supersecret123 please"
    )
    _append_persona_assistant_text(
        session_db=db, session_id="s1", text="using token: ghp_leakedtoken00000"
    )
    blob = str(_mirror_lines("s1"))
    assert "sk-supersecret123" not in blob
    assert "ghp_leakedtoken00000" not in blob


def test_a_failed_sessiondb_append_is_not_mirrored():
    # The mirror follows the durable write; it must never claim a message the
    # transcript of record never accepted.
    class ExplodingDB(FakeSessionDB):
        def append_message(self, *args, **kwargs):
            raise RuntimeError("db down")

    assert not _append_persona_operator_turn(
        session_db=ExplodingDB(), session_id="s1", message="hi"
    )
    assert _mirror_messages("s1") == []
