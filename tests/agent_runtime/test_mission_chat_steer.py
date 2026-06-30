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
