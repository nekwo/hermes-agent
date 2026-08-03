from __future__ import annotations

import time

from agent_runtime.mission_chat_steer import start_active_mission_chat_turn, submit_mission_chat_steer


class FakeSteerAgent:
    def __init__(self):
        self.steers: list[str] = []

    def steer(self, text: str) -> None:
        self.steers.append(text)


def test_steer_with_no_active_turn_returns_structured_rejection(tmp_path):
    result = submit_mission_chat_steer(
        runtime_root=tmp_path,
        session_id="session_missing",
        message="for neko",
        client_message_id="client_1",
        timeout_seconds=0.1,
    )

    assert result["ok"] is False
    assert result["capability_id"] == "mission.chat.steer"
    assert result["execution_state"] == "rejected"
    assert result["session_id"] == "session_missing"
    assert result["client_message_id"] == "client_1"
    assert result["error_kind"] == "no_active_turn"


def test_active_steer_calls_agent_once_and_acknowledges(tmp_path):
    agent = FakeSteerAgent()
    handle = start_active_mission_chat_turn(
        runtime_root=tmp_path,
        session_id="session_live",
        agent=agent,
    )
    try:
        result = submit_mission_chat_steer(
            runtime_root=tmp_path,
            session_id="session_live",
            message="fold this into the current answer",
            client_message_id="client_1",
        )
    finally:
        handle.close()

    assert result["ok"] is True
    assert result["execution_state"] == "accepted"
    assert result["client_message_id"] == "client_1"
    assert agent.steers == ["fold this into the current answer"]


def test_duplicate_client_message_id_is_idempotent(tmp_path):
    agent = FakeSteerAgent()
    handle = start_active_mission_chat_turn(
        runtime_root=tmp_path,
        session_id="session_live",
        agent=agent,
    )
    try:
        first = submit_mission_chat_steer(
            runtime_root=tmp_path,
            session_id="session_live",
            message="first steer",
            client_message_id="client_dup",
        )
        second = submit_mission_chat_steer(
            runtime_root=tmp_path,
            session_id="session_live",
            message="second steer should not run",
            client_message_id="client_dup",
        )
    finally:
        handle.close()

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["idempotent_replay"] is True
    assert agent.steers == ["first steer"]


def test_empty_steer_text_is_rejected_before_inbox_write(tmp_path):
    agent = FakeSteerAgent()
    handle = start_active_mission_chat_turn(
        runtime_root=tmp_path,
        session_id="session_live",
        agent=agent,
    )
    try:
        result = submit_mission_chat_steer(
            runtime_root=tmp_path,
            session_id="session_live",
            message="   ",
            client_message_id="client_empty",
            timeout_seconds=0.1,
        )
        time.sleep(0.1)
    finally:
        handle.close()

    inbox = tmp_path / "mission_chat_steer" / "session_live" / "inbox"
    assert result["ok"] is False
    assert result["error_kind"] == "invalid_request"
    assert list(inbox.glob("*.json")) == []
    assert agent.steers == []


def test_accepted_steer_lands_in_the_session_live_log(tmp_path, monkeypatch):
    """A head agent tailing the thread must see WHY the agent changed course.

    Steered text is persisted by the runtime's native flush, outside every seam
    the live-log mirror hooks, so without this the stream showed an agent
    visibly redirecting itself with nothing explaining it until a rebuild.
    """

    import json

    from agent_runtime.chat_live_log import chat_live_log_path, reset_chat_live_log_state

    monkeypatch.setenv("HERMES_HEAD_HOME", str(tmp_path / "head"))
    (tmp_path / "head").mkdir(parents=True, exist_ok=True)
    reset_chat_live_log_state()
    try:
        agent = FakeSteerAgent()
        handle = start_active_mission_chat_turn(
            runtime_root=tmp_path,
            session_id="session_live",
            agent=agent,
        )
        try:
            result = submit_mission_chat_steer(
                runtime_root=tmp_path,
                session_id="session_live",
                message="actually check the backend first",
                client_message_id="client_steer_1",
            )
        finally:
            handle.close()

        assert result["ok"] is True
        path = chat_live_log_path("session_live")
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        messages = [row for row in rows if row.get("kind") == "message"]
        assert len(messages) == 1
        assert messages[0]["text"] == "actually check the backend first"
        assert messages[0]["role"] == "operator"
        # Typed, so a reader can tell a mid-turn injection from the order that
        # opened the turn.
        assert messages[0]["steered"] is True
    finally:
        reset_chat_live_log_state()


def test_a_broken_mirror_never_rejects_an_accepted_steer(tmp_path, monkeypatch):
    from agent_runtime import chat_live_log

    def _boom(**kwargs):
        raise OSError("mirror volume gone")

    monkeypatch.setattr(chat_live_log, "record_chat_message", _boom)
    agent = FakeSteerAgent()
    handle = start_active_mission_chat_turn(
        runtime_root=tmp_path,
        session_id="session_live",
        agent=agent,
    )
    try:
        result = submit_mission_chat_steer(
            runtime_root=tmp_path,
            session_id="session_live",
            message="still steers",
            client_message_id="client_steer_2",
        )
    finally:
        handle.close()

    assert result["ok"] is True and result["execution_state"] == "accepted"
    assert agent.steers == ["still steers"]
