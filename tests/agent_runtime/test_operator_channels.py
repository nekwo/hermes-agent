from agent_runtime.models import PersonaInstance
from agent_runtime.operator_channels import operator_channel_summary
from agent_runtime.states import WorkerSessionState


def _instance(instance_id: str, *, session_id: str, updated_at: str) -> PersonaInstance:
    return PersonaInstance(
        id=instance_id,
        persona_id="profile:alice",
        role="profile",
        display_name="Alice Agent",
        profile_id="alice",
        runtime_root="test-runtime",
        state=WorkerSessionState.IDLE,
        mode="chat",
        session_id=session_id,
        updated_at=updated_at,
    )


def test_operator_channel_collapses_duplicate_instances_and_keeps_trace():
    session_id = "persona_chat_personainst_profile_alice_e898c1dc3794"
    channels = operator_channel_summary(
        persona_instances=[
            _instance(
                "personainst_operator_2c1f1de674e74942",
                session_id=session_id,
                updated_at="2026-06-25T21:54:04Z",
            ),
            _instance(
                "personainst_profile_alice",
                session_id=session_id,
                updated_at="2026-06-25T21:53:47Z",
            ),
        ],
        persona_chat_history=[
            {
                "session_id": session_id,
                "persona_id": "profile:alice",
                "persona_instance_id": "personainst_profile_alice",
                "title": "Alice Agent chat",
                "message_count": 1,
                "messages": [{"role": "operator", "text": "run date"}],
                "updated_at": "2026-06-25T21:54:04Z",
            }
        ],
        persona_chat_trace=[
            {
                "session_id": session_id,
                "persona_id": "profile:alice",
                "persona_instance_id": "personainst_operator_2c1f1de674e74942",
                "task_id": None,
                "entries": [
                    {
                        "event": "tool_started",
                        "tool_name": "terminal",
                        "summary": "Started tool terminal: date",
                        "status": "started",
                        "ts": "2026-06-25T21:54:00Z",
                    }
                ],
            }
        ],
    )

    assert len(channels) == 1
    channel = channels[0]
    assert channel["persona_instance_id"] == "personainst_profile_alice"
    assert channel["session_id"] == session_id
    assert channel["tool_trace_count"] == 1
    assert channel["trace"]["entries"][0]["tool_name"] == "terminal"
    assert set(channel["source_instance_ids"]) == {
        "personainst_operator_2c1f1de674e74942",
        "personainst_profile_alice",
    }
    assert any(
        warning["code"] == "duplicate_instances_same_channel"
        for warning in channel["warnings"]
    )


def test_operator_channel_reports_missing_history_loudly():
    session_id = "persona_chat_personainst_profile_alice_missing"
    channels = operator_channel_summary(
        persona_instances=[
            _instance(
                "personainst_profile_alice",
                session_id=session_id,
                updated_at="2026-06-25T21:54:04Z",
            )
        ],
        persona_chat_history=[],
        persona_chat_trace=[],
    )

    assert len(channels) == 1
    assert any(
        warning["code"] == "session_without_history"
        for warning in channels[0]["warnings"]
    )
    assert any(warning["code"] == "trace_empty" for warning in channels[0]["warnings"])
