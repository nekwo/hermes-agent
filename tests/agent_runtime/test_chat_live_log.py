"""Live chat-log mirror (``agent_runtime/chat_live_log.py``).

Covers the substrate WP-H3 rests on: head-home pathing that survives the
persona-turn ``HERMES_HOME`` flip, redaction on write, one-shot backfill,
per-line bounds, rotation, and the best-effort/counted failure posture.
"""

import json

import pytest

from agent_runtime import chat_live_log
from agent_runtime.chat_live_log import (
    LIVE_LOG_TEXT_LIMIT,
    capture_chat_live_log_root,
    chat_live_log_failures,
    chat_live_log_path,
    chat_live_log_stats,
    ensure_chat_live_log,
    record_chat_message,
    record_chat_tool,
    reset_chat_live_log_state,
)


@pytest.fixture(autouse=True)
def _clean_mirror_state():
    reset_chat_live_log_state()
    yield
    reset_chat_live_log_state()


def _lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _messages(path):
    return [row for row in _lines(path) if row.get("kind") == "message"]


class _FakeDb:
    """Just enough SessionDB surface for the projection's read path."""

    __hermes_canonical_session_persistence__ = True

    def __init__(self, db_path=None):
        self.db_path = db_path
        self.messages = {}

    def append_message(self, session_id, role, content=None, **kwargs):
        self.messages.setdefault(session_id, []).append(
            {
                "id": f"{session_id}:{len(self.messages.get(session_id, []))}",
                "role": role,
                "content": content,
                "platform_message_id": kwargs.get("platform_message_id"),
                "created_at": "2026-08-03T00:00:00+00:00",
            }
        )

    def get_messages(self, session_id, include_inactive=False):
        return list(self.messages.get(session_id, []))


def test_root_is_captured_at_init_and_survives_a_flipped_hermes_home(tmp_path, monkeypatch):
    # THE TRAP: persona_profile_context flips HERMES_HOME process-globally to the
    # persona's profile for the whole turn — which is exactly when these writes
    # happen. Resolving the directory at WRITE time would scatter one
    # conversation's mirror into per-persona profile directories.
    head_home = tmp_path / "head"
    head_home.mkdir()
    monkeypatch.setenv("HERMES_HEAD_HOME", str(head_home))
    captured = capture_chat_live_log_root()
    assert captured == head_home / "chat_live_logs"

    # Now simulate the persona turn: the head authority is gone from this
    # process's view and HERMES_HOME points at the persona profile.
    profile_home = tmp_path / "profiles" / "neko"
    profile_home.mkdir(parents=True)
    monkeypatch.delenv("HERMES_HEAD_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    assert record_chat_message(session_id="persona_chat_s1", role="user", text="hi") is True
    mirrored = head_home / "chat_live_logs" / "persona_chat_s1.jsonl"
    assert mirrored.exists()
    assert not (profile_home / "chat_live_logs").exists()
    assert [row["text"] for row in _messages(mirrored)] == ["hi"]


def test_session_db_directory_wins_over_a_scope_capture(tmp_path, monkeypatch):
    scope_home = tmp_path / "scope"
    scope_home.mkdir()
    monkeypatch.setenv("HERMES_HEAD_HOME", str(scope_home))
    capture_chat_live_log_root()

    db_home = tmp_path / "dbhome"
    db_home.mkdir()
    db = _FakeDb(db_path=db_home / "state.db")
    assert record_chat_message(
        session_id="persona_chat_s1", role="assistant", text="ack", session_db=db
    ) is True
    assert (db_home / "chat_live_logs" / "persona_chat_s1.jsonl").exists()


def test_secrets_are_masked_per_line_on_write(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HEAD_HOME", str(tmp_path))
    record_chat_message(
        session_id="persona_chat_s1",
        role="assistant",
        text='keep this line\n{"api_key": "sk-live-abcdef1234567890"}\nand this one',
    )
    path = chat_live_log_path("persona_chat_s1")
    blob = path.read_text(encoding="utf-8")
    assert "sk-live-abcdef1234567890" not in blob
    assert "redacted line" in blob
    assert "keep this line" in blob and "and this one" in blob


def test_per_line_text_is_capped_with_a_visible_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HEAD_HOME", str(tmp_path))
    record_chat_message(session_id="persona_chat_s1", role="agent", text="x" * (LIVE_LOG_TEXT_LIMIT + 500))
    row = _messages(chat_live_log_path("persona_chat_s1"))[0]
    assert len(row["text"]) <= LIVE_LOG_TEXT_LIMIT + len(" … [truncated]")
    assert row["text"].endswith("… [truncated]")


def test_rotation_keeps_one_generation(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HEAD_HOME", str(tmp_path))
    monkeypatch.setattr(chat_live_log, "LIVE_LOG_ROTATE_BYTES", 400)
    for index in range(12):
        record_chat_message(
            session_id="persona_chat_s1",
            role="agent",
            text=f"line {index} " + "y" * 60,
            client_message_id=f"cm-{index}",
        )
    path = chat_live_log_path("persona_chat_s1")
    rotated = path.with_name(path.name + ".1")
    assert path.exists() and rotated.exists()
    # One generation only: no .2 sibling is ever produced.
    assert not path.with_name(path.name + ".2").exists()
    stats = chat_live_log_stats("persona_chat_s1")
    assert stats["rotated_path"] == str(rotated)


def test_replayed_message_is_not_doubled(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HEAD_HOME", str(tmp_path))
    for _ in range(3):
        record_chat_message(
            session_id="persona_chat_s1",
            role="assistant",
            text="the one recorded reply",
            client_message_id="cm-1",
        )
    assert len(_messages(chat_live_log_path("persona_chat_s1"))) == 1


def test_dedupe_survives_a_new_process_by_reading_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HEAD_HOME", str(tmp_path))
    record_chat_message(
        session_id="persona_chat_s1", role="assistant", text="reply", client_message_id="cm-1"
    )
    # A fresh process has no in-memory index; the tail scan must rebuild it.
    reset_chat_live_log_state()
    record_chat_message(
        session_id="persona_chat_s1", role="assistant", text="reply", client_message_id="cm-1"
    )
    assert len(_messages(chat_live_log_path("persona_chat_s1"))) == 1


def test_tool_lines_land_in_the_same_stream(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HEAD_HOME", str(tmp_path))
    record_chat_message(session_id="persona_chat_s1", role="user", text="go")
    record_chat_tool(session_id="persona_chat_s1", tool="terminal", status="started")
    record_chat_tool(session_id="persona_chat_s1", tool="terminal", status="ok")
    rows = _lines(chat_live_log_path("persona_chat_s1"))
    assert [row.get("kind") for row in rows] == ["log_opened", "message", "tool", "tool"]
    stats = chat_live_log_stats("persona_chat_s1")
    assert stats["message_count"] == 1 and stats["tool_count"] == 2


def test_backfill_materializes_pre_feature_history_exactly_once(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HEAD_HOME", str(tmp_path))
    db = _FakeDb(db_path=tmp_path / "state.db")
    db.append_message("persona_chat_s1", "user", "older question")
    db.append_message("persona_chat_s1", "assistant", "older answer")
    monkeypatch.setattr(chat_live_log, "_backfill_rows", _backfill_from(db))

    path = ensure_chat_live_log("persona_chat_s1")
    assert [row["text"] for row in _messages(path)] == ["older question", "older answer"]
    header = _lines(path)[0]
    assert header["kind"] == "log_opened" and header["backfilled"] == 2

    # Second touch appends live, it does NOT re-materialize.
    record_chat_message(session_id="persona_chat_s1", role="user", text="new question")
    assert [row["text"] for row in _messages(path)] == [
        "older question",
        "older answer",
        "new question",
    ]
    assert len([row for row in _lines(path) if row.get("kind") == "log_opened"]) == 1


def test_backfill_uses_the_real_projection_and_redacts(tmp_path, monkeypatch):
    # No monkeypatched backfill here: the real persona_chat_session_messages
    # read path is what has to produce the rows (and redact them).
    monkeypatch.setenv("HERMES_HEAD_HOME", str(tmp_path))
    db = _FakeDb(db_path=tmp_path / "state.db")
    db.append_message("persona_chat_s1", "user", "what is the token")
    db.append_message("persona_chat_s1", "assistant", 'here: {"api_key": "sk-live-abcdef1234567890"}')

    path = ensure_chat_live_log("persona_chat_s1", session_db=db)
    blob = path.read_text(encoding="utf-8")
    assert "sk-live-abcdef1234567890" not in blob
    assert "what is the token" in blob


def test_write_failure_is_counted_not_swallowed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HEAD_HOME", str(tmp_path))
    ensure_chat_live_log("persona_chat_s1")

    def _boom(*args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(chat_live_log, "open", _boom, raising=False)
    assert record_chat_message(session_id="persona_chat_s1", role="user", text="hi") is False
    assert chat_live_log_failures() >= 1


def test_path_shaped_session_ids_are_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HEAD_HOME", str(tmp_path))
    assert chat_live_log_path("../../escape") is None
    assert record_chat_message(session_id="a/b", role="user", text="hi") is False


def _backfill_from(db):
    def _rows(session_id, *, session_db=None):
        return [
            {
                "ts": "2026-08-03T00:00:00+00:00",
                "kind": "message",
                "role": message["role"],
                "text": message["content"],
                "backfilled": True,
            }
            for message in db.get_messages(session_id)
        ]

    return _rows
