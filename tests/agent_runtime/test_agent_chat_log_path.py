"""``agent_chat_log_path`` — hand a teammate thread's LIVE log path to a head agent.

Lives beside ``test_agent_chat_tool.py`` (not ``tests/tools/``) because the whole
agent-chat tool family needs this package's ``isolate_agent_runtime_root`` +
``persisted_persona_samples`` fixtures to have a persona roster and an isolated
runtime root at all.
"""

import json

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from agent_runtime.chat_live_log import reset_chat_live_log_state
from tools.agent_chat_tool import AGENT_CHAT_LOG_PATH_SCHEMA, agent_chat_log_path
from tools.registry import registry


@pytest.fixture(autouse=True)
def _clean_mirror_state():
    reset_chat_live_log_state()
    yield
    reset_chat_live_log_state()


def test_tool_is_registered_on_the_agent_chat_toolset():
    entry = registry.get_entry("agent_chat_log_path")
    assert entry is not None
    assert entry.toolset == "agent_chat"
    assert AGENT_CHAT_LOG_PATH_SCHEMA["parameters"]["required"] == ["persona_id"]
    # The disambiguator matters more here than anywhere else in this family: the
    # model has to know WHEN to reach for a file path over the bounded tail.
    description = AGENT_CHAT_LOG_PATH_SCHEMA["description"]
    assert "grep" in description and "agent_chat_open" in description


def test_returns_a_live_path_backfilled_from_the_same_projection(isolate_agent_runtime_root):
    from pathlib import Path

    from tests.agent_runtime.test_agent_chat_tool import _seed_persona_chat

    session_id = _seed_persona_chat("qa", [("hi there", "QA here — hi."), ("more", "ack")])

    data = json.loads(agent_chat_log_path(persona_id="qa"))
    assert data["ok"] is True
    assert data["count"] == 1
    thread = data["threads"][0]
    assert thread["session_id"] == session_id
    assert thread["live"] is True
    assert thread["message_count"] == 4  # 2 operator + 2 agent, materialized once

    path = Path(thread["path"])
    assert path.is_absolute() and path.exists()
    assert path.parent.name == "chat_live_logs"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows[0]["kind"] == "log_opened"
    assert [row["text"] for row in rows if row.get("kind") == "message"] == [
        "hi there",
        "QA here — hi.",
        "more",
        "ack",
    ]


def test_the_file_keeps_growing_after_the_path_is_handed_out(isolate_agent_runtime_root):
    """LIVE, not an export: a head agent re-reads the same path and sees more."""

    from pathlib import Path

    from hermes_cli.harness import _append_persona_assistant_text
    from hermes_state import SessionDB
    from tests.agent_runtime.test_agent_chat_tool import _seed_persona_chat

    session_id = _seed_persona_chat("qa", [("hi", "ack")])
    thread = json.loads(agent_chat_log_path(persona_id="qa"))["threads"][0]
    path = Path(thread["path"])
    before = path.read_text(encoding="utf-8")

    # A new message crosses the persist chokepoint — no second tool call.
    _append_persona_assistant_text(
        session_db=SessionDB(),
        session_id=session_id,
        text="status update while you were away",
        client_message_id="cm-live-1",
    )

    after = path.read_text(encoding="utf-8")
    assert len(after) > len(before)
    assert "status update while you were away" in after


def test_refuses_a_foreign_session(isolate_agent_runtime_root):
    from tests.agent_runtime.test_agent_chat_tool import _seed_persona_chat

    _seed_persona_chat("qa", [("hi", "ack")])
    # Handing over a FILE PATH is at least as strong a capability as reading a
    # tail, so it gets the identical guard and the identical typed refusal.
    data = json.loads(
        agent_chat_log_path(
            persona_id="qa", session_id="persona_chat_personainst_dev_deadbeef0000"
        )
    )
    assert data["ok"] is False
    assert data["error_kind"] == "foreign_session"
    assert "path" not in data


def test_sibling_prefix_collision_is_still_refused(isolate_agent_runtime_root):
    from agent_runtime.persona_assignments import PersonaInstanceStore
    from tests.agent_runtime.test_agent_chat_tool import _seed_persona_chat

    sibling = PersonaInstanceStore().add_instance(
        persona_id="qa", placement_id="qa_agent_2", display_name="QA Agent 2"
    )
    _seed_persona_chat("qa", [("primary hi", "primary ack")])
    sibling_session = _seed_persona_chat(
        "qa", [("sib", "ok")], persona_instance_id=sibling.id
    )

    refused = json.loads(
        agent_chat_log_path(persona_id="personainst_qa", session_id=sibling_session)
    )
    assert refused["ok"] is False and refused["error_kind"] == "foreign_session"


def test_all_threads_lists_only_this_lane(isolate_agent_runtime_root):
    from tests.agent_runtime.test_agent_chat_tool import _seed_persona_chat

    qa_session = _seed_persona_chat("qa", [("hi", "ack")])
    dev_session = _seed_persona_chat("dev", [("hi", "ack")])

    data = json.loads(agent_chat_log_path(persona_id="qa", all_threads=True))
    assert data["ok"] is True
    sessions = {thread["session_id"] for thread in data["threads"]}
    assert qa_session in sessions
    assert dev_session not in sessions


def test_no_thread_yet_is_an_honest_empty_answer(isolate_agent_runtime_root):
    data = json.loads(agent_chat_log_path(persona_id="qa"))
    assert data["ok"] is True
    assert data["count"] == 0 and data["threads"] == []
    assert "no thread" in data["next_expected"]


def test_blank_persona_and_scope_off_are_refused(monkeypatch):
    assert json.loads(agent_chat_log_path(persona_id=""))["ok"] is False
    monkeypatch.setenv("HERMES_AGENT_CHAT_SCOPE", "off")
    data = json.loads(agent_chat_log_path(persona_id="qa"))
    assert data["ok"] is False and "disabled" in data["error"]
