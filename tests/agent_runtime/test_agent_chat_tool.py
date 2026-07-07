"""Agent-to-agent chat relay tool (tools/agent_chat_tool.py)."""

import json

import pytest

from tools.agent_chat_tool import AGENT_CHAT_SEND_SCHEMA, _RELAY_STATE, agent_chat_send
from tools.registry import registry


def test_tool_is_registered_on_the_agent_chat_toolset():
    entry = registry.get_entry("agent_chat_send")
    assert entry is not None
    assert entry.toolset == "agent_chat"
    assert AGENT_CHAT_SEND_SCHEMA["parameters"]["required"] == ["persona_id", "message"]


def test_refuses_blank_persona_and_message():
    assert not json.loads(agent_chat_send(persona_id="", message="hi"))["ok"]
    assert not json.loads(agent_chat_send(persona_id="dev", message="  "))["ok"]


def test_refuses_instance_ids_with_guidance():
    data = json.loads(agent_chat_send(persona_id="personainst_dev", message="hi"))
    assert data["ok"] is False
    assert "instance id" in data["error"]
    assert "neko_supervisor" in data["error"]


def test_scope_off_disables_the_tool(monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_CHAT_SCOPE", "off")
    data = json.loads(agent_chat_send(persona_id="dev", message="hi"))
    assert data["ok"] is False
    assert "disabled" in data["error"]


def test_depth_guard_blocks_chained_relays():
    _RELAY_STATE.depth = 1
    try:
        data = json.loads(agent_chat_send(persona_id="dev", message="hi"))
    finally:
        _RELAY_STATE.depth = 0
    assert data["ok"] is False
    assert "depth" in data["error"]


def test_happy_path_returns_compact_reply_without_observability(monkeypatch):
    def fake_handler(args):
        assert args.persona_id == "neko_supervisor"
        assert args.stream is False and args.json is True
        assert args.requested_by == "agent:persona_chat_personainst_alice_x"
        print(
            json.dumps(
                {
                    "ok": True,
                    "reply": "On it — briefing received.",
                    "session_id": "persona_chat_personainst_neko_supervisor",
                    "chat_session_id": "persona_chat_personainst_neko_supervisor",
                    "persona_instance_id": "personainst_operator_abc",
                    "total_tokens": 12345,
                    "prompt_observability": {"huge": "x" * 1000},
                }
            )
        )
        return 0

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", fake_handler)
    data = json.loads(
        agent_chat_send(
            persona_id="neko_supervisor",
            message="Operator asks: prepare the stage 48 briefing.",
            requested_by_session="persona_chat_personainst_alice_x",
        )
    )
    assert data["ok"] is True
    assert data["reply"] == "On it — briefing received."
    assert data["target_persona"] == "neko_supervisor"
    assert data["session_id"] == "persona_chat_personainst_neko_supervisor"
    assert "prompt_observability" not in data


def test_failed_target_turn_surfaces_typed_error(monkeypatch):
    def fake_handler(args):
        print(json.dumps({"ok": False, "error": "unknown persona pm2"}))
        return 2

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", fake_handler)
    data = json.loads(agent_chat_send(persona_id="pm2", message="hi"))
    assert data["ok"] is False
    assert data["error"] == "unknown persona pm2"
    assert data["exit_code"] == 2


def test_depth_state_resets_after_relay(monkeypatch):
    def fake_handler(args):
        # While the relay runs, depth must be exactly 1 so the TARGET turn
        # cannot relay onward.
        assert int(getattr(_RELAY_STATE, "depth", 0)) == 1
        print(json.dumps({"ok": True, "reply": "ack"}))
        return 0

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", fake_handler)
    assert json.loads(agent_chat_send(persona_id="dev", message="hi"))["ok"]
    assert int(getattr(_RELAY_STATE, "depth", 0)) == 0


@pytest.mark.parametrize("role_key", ["PM", "DEV", "QA", "ALICE_SUPERVISOR"])
def test_every_role_is_allowed_the_agent_chat_toolset(role_key):
    from agent_runtime.personas import ALLOWED_TOOLSETS_BY_ROLE, AgentRole

    assert "agent_chat" in ALLOWED_TOOLSETS_BY_ROLE[AgentRole[role_key]]


def test_profile_chat_fallback_includes_agent_chat():
    from agent_runtime.personas import PROFILE_CHAT_FALLBACK_TOOLSETS

    assert "agent_chat" in PROFILE_CHAT_FALLBACK_TOOLSETS


def test_chat_capability_augmentation_includes_agent_chat():
    from agent_runtime.persona_runtime import _CHAT_CAPABILITY_TOOLSETS

    assert "agent_chat" in _CHAT_CAPABILITY_TOOLSETS


def test_relay_trace_carries_target_and_briefing_excerpt():
    from agent_runtime.profile_runner import _tool_finished_payload, _tool_started_payload

    invocation = {
        "persona_id": "neko_supervisor",
        "message": "From Tony via Alice: review the Stage 47 results and report readiness.",
    }
    started = _tool_started_payload("run.tool.started", "agent_chat_send", invocation=invocation)
    assert started["target_label"].startswith("→ neko_supervisor: From Tony via Alice")
    assert "neko_supervisor" in started["summary"]

    finished = _tool_finished_payload(
        "run.tool.finished",
        "agent_chat_send",
        duration=None,
        is_error=False,
        result=None,
        invocation=invocation,
    )
    assert finished["target_label"].startswith("→ neko_supervisor")


def test_relay_trace_drops_secret_bearing_briefings():
    from agent_runtime.profile_runner import _tool_started_payload

    started = _tool_started_payload(
        "run.tool.started",
        "agent_chat_send",
        invocation={"persona_id": "dev", "message": "use API_KEY=sk-123456789 for the deploy"},
    )
    assert started["target_label"] == "→ dev"
    assert "sk-123456789" not in str(started)
