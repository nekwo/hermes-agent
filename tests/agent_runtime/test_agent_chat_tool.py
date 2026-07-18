"""Agent-to-agent chat relay tool (tools/agent_chat_tool.py)."""

import json

import pytest

from agent_runtime.relay_policy import RELAY_CHAIN, RELAY_DEADLINE
from tools.agent_chat_tool import AGENT_CHAT_SEND_SCHEMA, agent_chat_send
from tools.registry import registry


def test_tool_is_registered_on_the_agent_chat_toolset():
    entry = registry.get_entry("agent_chat_send")
    assert entry is not None
    assert entry.toolset == "agent_chat"
    assert AGENT_CHAT_SEND_SCHEMA["parameters"]["required"] == ["persona_id", "message"]


def test_refuses_blank_persona_and_message():
    assert not json.loads(agent_chat_send(persona_id="", message="hi"))["ok"]
    assert not json.loads(agent_chat_send(persona_id="dev", message="  "))["ok"]


def test_instance_shaped_ids_are_relayed_for_canonical_resolution(monkeypatch):
    # Agents copy personainst_* ids straight out of Mission Control payloads;
    # the canonical mission-chat handler resolves them, so the tool forwards
    # instead of refusing.
    seen = {}

    def fake_handler(args):
        seen["persona_id"] = args.persona_id
        print(json.dumps({"ok": True, "reply": "ack"}))
        return 0

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", fake_handler)
    data = json.loads(agent_chat_send(persona_id="personainst_dev", message="hi"))
    assert data["ok"] is True
    assert seen["persona_id"] == "personainst_dev"


def test_omitted_session_is_forwarded_as_none_so_the_handler_threads(monkeypatch):
    # The tool must NOT invent a session id: omitting session_id forwards None,
    # so the mission-chat handler's default-session resolution owns "continue the
    # target's default chat session" (repeated relays thread into one). A
    # tool-side mint would be a parallel authority and re-open the orphaned-relay
    # defect.
    seen = {}

    def fake_handler(args):
        seen["session_id"] = args.session_id
        print(json.dumps({"ok": True, "reply": "ack"}))
        return 0

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", fake_handler)
    assert json.loads(agent_chat_send(persona_id="qa", message="hi"))["ok"]
    assert seen["session_id"] is None
    # An explicit session id still passes through untouched (continue THAT thread).
    assert json.loads(
        agent_chat_send(persona_id="qa", message="hi", session_id="persona_chat_personainst_qa_abc")
    )["ok"]
    assert seen["session_id"] == "persona_chat_personainst_qa_abc"


def test_scope_off_disables_the_tool(monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_CHAT_SCOPE", "off")
    data = json.loads(agent_chat_send(persona_id="dev", message="hi"))
    assert data["ok"] is False
    assert "disabled" in data["error"]


def test_relay_envelope_is_forwarded_explicitly(monkeypatch):
    # The tool carries the CURRENT turn's chain/deadline (seeded by the
    # handler into the policy ContextVars) as explicit request fields —
    # provenance must survive transports that don't share this process.
    seen = {}

    def fake_handler(args):
        seen["relay_chain"] = args.relay_chain
        seen["relay_deadline_epoch"] = args.relay_deadline_epoch
        seen["max_seconds"] = args.max_seconds
        print(json.dumps({"ok": True, "reply": "ack"}))
        return 0

    import time

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", fake_handler)
    deadline = time.time() + 60.0
    chain_token = RELAY_CHAIN.set(("neko_supervisor",))
    deadline_token = RELAY_DEADLINE.set(deadline)
    try:
        data = json.loads(agent_chat_send(persona_id="dev", message="hi", max_seconds=240))
    finally:
        RELAY_CHAIN.reset(chain_token)
        RELAY_DEADLINE.reset(deadline_token)
    assert data["ok"] is True
    assert seen["relay_chain"] == ["neko_supervisor"]
    assert seen["relay_deadline_epoch"] == deadline
    # This hop's wall budget is clamped to the shared chain deadline.
    assert seen["max_seconds"] <= 60.0


def test_root_relay_mints_the_shared_deadline(monkeypatch):
    seen = {}

    def fake_handler(args):
        seen["relay_chain"] = args.relay_chain
        seen["relay_deadline_epoch"] = args.relay_deadline_epoch
        print(json.dumps({"ok": True, "reply": "ack"}))
        return 0

    import time

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", fake_handler)
    before = time.time()
    data = json.loads(agent_chat_send(persona_id="dev", message="hi", max_seconds=120))
    assert data["ok"] is True
    assert seen["relay_chain"] == []
    assert before + 110 <= seen["relay_deadline_epoch"] <= time.time() + 121


def test_exhausted_shared_deadline_fast_fails_before_the_send(monkeypatch):
    import time

    def fake_handler(args):  # pragma: no cover - must not be reached
        raise AssertionError("relay must fast-fail before dispatch")

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", fake_handler)
    deadline_token = RELAY_DEADLINE.set(time.time() + 1.0)
    try:
        data = json.loads(agent_chat_send(persona_id="dev", message="hi"))
    finally:
        RELAY_DEADLINE.reset(deadline_token)
    assert data["ok"] is False
    assert data["error_kind"] == "relay_budget_exhausted"


def test_typed_chokepoint_refusals_propagate(monkeypatch):
    # Depth/cycle authority lives in the mission-chat handler; the tool must
    # surface its typed refusal, not re-decide.
    def fake_handler(args):
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_kind": "relay_cycle",
                    "error": "relay cycle detected: 'dev' is already on this relay chain",
                    "relay_chain": ["neko_supervisor", "dev"],
                }
            )
        )
        return 2

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", fake_handler)
    data = json.loads(agent_chat_send(persona_id="dev", message="hi"))
    assert data["ok"] is False
    assert data["error_kind"] == "relay_cycle"
    assert data["relay_chain"] == ["neko_supervisor", "dev"]


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


def test_tool_does_not_mutate_ambient_relay_state(monkeypatch):
    # Seeding the turn chain is the HANDLER's job (single write path); the
    # tool only reads and forwards the envelope.
    def fake_handler(args):
        assert RELAY_CHAIN.get() == ()
        print(json.dumps({"ok": True, "reply": "ack"}))
        return 0

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", fake_handler)
    assert json.loads(agent_chat_send(persona_id="dev", message="hi"))["ok"]
    assert RELAY_CHAIN.get() == ()
    assert RELAY_DEADLINE.get() is None


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


def test_conversation_drops_thinking_rows_that_echo_the_reply():
    from agent_runtime.operator_channels import _dedupe_conversation_messages

    messages = [
        {"kind": "operator_message", "display_text": "say hi", "id": "1"},
        {
            "kind": "thinking_summary",
            "display_text": "Hello Master — I'm online.",
            "id": "2",
        },
        {"kind": "reply", "display_text": "Hello Master — I'm online.", "id": "3"},
        {
            "kind": "thinking_summary",
            "display_text": "Planning the relay first.",
            "id": "4",
        },
    ]
    deduped = _dedupe_conversation_messages(messages)
    kinds = [(m["kind"], m["id"]) for m in deduped]
    # The echo thinking row (id 2) is curated out; genuine thinking survives.
    assert kinds == [
        ("operator_message", "1"),
        ("reply", "3"),
        ("thinking_summary", "4"),
    ]
