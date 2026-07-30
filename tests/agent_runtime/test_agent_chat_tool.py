"""Agent-to-agent chat relay tool (tools/agent_chat_tool.py)."""

import json

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from agent_runtime.relay_policy import RELAY_CHAIN, RELAY_DEADLINE
from tools.agent_chat_tool import (
    AGENT_CHAT_OPEN_SCHEMA,
    AGENT_CHAT_SEND_SCHEMA,
    AGENT_CHAT_THREADS_SCHEMA,
    agent_chat_open,
    agent_chat_send,
    agent_chat_threads,
)
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


@pytest.mark.parametrize("role", ["custom-reviewer", "dev", "qa", "profile"])
def test_every_configured_role_gets_chat_capabilities(role):
    from agent_runtime.persona_runtime import _augment_chat_capabilities
    from tests.agent_runtime.persona_samples import sample_persona

    assert "agent_chat" in _augment_chat_capabilities(sample_persona(role=role), ["search"])


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


# --------------------------------------------------------------------------- #
# new_session lane + read-only companions (agent_chat_threads / agent_chat_open)
# --------------------------------------------------------------------------- #


def test_read_only_companions_are_registered_on_the_agent_chat_toolset():
    for name in ("agent_chat_threads", "agent_chat_open"):
        entry = registry.get_entry(name)
        assert entry is not None, name
        assert entry.toolset == "agent_chat", name
    assert AGENT_CHAT_THREADS_SCHEMA["parameters"]["required"] == []
    assert AGENT_CHAT_OPEN_SCHEMA["parameters"]["required"] == ["persona_id"]


def test_send_schema_teaches_the_verb_set():
    # The scope addition: the send description teaches the @handle addressing
    # model and the thread contract, and exposes the thread-target parameters.
    text = AGENT_CHAT_SEND_SCHEMA["description"]
    assert "new_session" in text
    assert "session_id" in text
    assert "personainst_" in text  # @handle addressing is taught
    props = AGENT_CHAT_SEND_SCHEMA["parameters"]["properties"]
    assert "new_session" in props and props["new_session"]["type"] == "boolean"
    assert "title" in props and props["title"]["type"] == "string"


def test_send_schema_teaches_the_task_scoped_thread_contract():
    # The model reads this description to decide whether to pass a session_id.
    # It must say a NEW task gets a fresh thread and that continuing an exchange
    # means passing the returned session back — otherwise the agent keeps the old
    # "omit to continue" habit and every follow-up opens another empty thread.
    text = AGENT_CHAT_SEND_SCHEMA["description"].lower()
    assert "fresh" in text
    assert "session_id" in text and "continue" in text


def test_new_session_has_no_schema_default_so_unset_stays_unset():
    # A `"default": false` here (or in the registry handler) collapses the
    # tri-state: providers fill declared defaults, so "the caller said nothing"
    # would arrive as an explicit "continue the durable thread" and silently pin
    # every dispatch to the pre-policy mega-thread behavior.
    assert "default" not in AGENT_CHAT_SEND_SCHEMA["parameters"]["properties"]["new_session"]


def test_read_tool_descriptions_say_when_to_use_them():
    threads = AGENT_CHAT_THREADS_SCHEMA["description"].lower()
    opened = AGENT_CHAT_OPEN_SCHEMA["description"].lower()
    assert "read-only" in threads and "read-only" in opened
    assert "list" in threads  # threads → list your threads
    assert "review" in opened  # open → review before continuing


def test_new_session_flag_is_forwarded_to_the_handler(monkeypatch):
    # The tool forwards new_session as a TRI-STATE and never decides the thread
    # itself; the handler's policy resolver owns the answer and the mint (through
    # the default-session chokepoint). The tool must NOT invent a session id.
    seen = {}

    def fake_handler(args):
        seen["new_session"] = getattr(args, "new_session", None)
        seen["session_id"] = args.session_id
        print(json.dumps({"ok": True, "reply": "ack", "session_id": "persona_chat_personainst_qa_fresh"}))
        return 0

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", fake_handler)
    # Absent → forwarded as None ("no opinion"), NOT False. Coercing to False
    # here would answer the policy question inside the tool and pin every
    # dispatch to the durable pair thread.
    assert json.loads(agent_chat_send(persona_id="qa", message="hi"))["ok"]
    assert seen["new_session"] is None and seen["session_id"] is None
    # new_session=True forwarded; session stays None so the handler mints via the
    # chokepoint (one minting authority), not a tool-side mint.
    assert json.loads(agent_chat_send(persona_id="qa", message="hi", new_session=True))["ok"]
    assert seen["new_session"] is True and seen["session_id"] is None
    # Explicit False is a real answer of its own: continue the durable thread.
    assert json.loads(agent_chat_send(persona_id="qa", message="hi", new_session=False))["ok"]
    assert seen["new_session"] is False and seen["session_id"] is None


def test_registry_handler_does_not_default_new_session_to_false(monkeypatch):
    # The registry lambda is the real model-facing entry point; a `False`
    # default there would defeat the tri-state just as thoroughly as a schema
    # default. Drive it exactly as the tool dispatcher does.
    seen = {}

    def fake_handler(args):
        seen["new_session"] = getattr(args, "new_session", None)
        print(json.dumps({"ok": True, "reply": "ack"}))
        return 0

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", fake_handler)
    entry = registry.get_entry("agent_chat_send")
    assert json.loads(entry.handler({"persona_id": "qa", "message": "hi"}))["ok"]
    assert seen["new_session"] is None


def test_string_boolean_new_session_is_not_inverted(monkeypatch):
    # bool("false") is True. A provider that serializes booleans as text would
    # have turned "continue our thread" into "start a fresh one".
    seen = {}

    def fake_handler(args):
        seen["new_session"] = getattr(args, "new_session", None)
        print(json.dumps({"ok": True, "reply": "ack"}))
        return 0

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", fake_handler)
    assert json.loads(agent_chat_send(persona_id="qa", message="hi", new_session="false"))["ok"]
    assert seen["new_session"] is False


def test_title_names_the_thread_this_dispatch_opens(monkeypatch):
    # Task-scoped threads are only navigable when named. The tool forwards the
    # caller's title; deriving one from the message when absent is the handler's
    # job (it is the side that knows whether this send actually mints).
    seen = {}

    def fake_handler(args):
        seen["title"] = getattr(args, "title", None)
        print(json.dumps({"ok": True, "reply": "ack"}))
        return 0

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", fake_handler)
    assert json.loads(
        agent_chat_send(persona_id="qa", message="triage this", title="Flaky login triage")
    )["ok"]
    assert seen["title"] == "Flaky login triage"
    # No title → None forwarded, never a placeholder like "Agent relay to qa",
    # which would name every dispatch thread after the plumbing.
    assert json.loads(agent_chat_send(persona_id="qa", message="triage this"))["ok"]
    assert seen["title"] is None
    assert json.loads(agent_chat_send(persona_id="qa", message="triage this", title="   "))["ok"]
    assert seen["title"] is None


def test_session_established_lineage_is_returned_to_the_caller(monkeypatch):
    # The dispatching agent must be able to tell "I opened a fresh task thread
    # (superseding this one)" from "I continued what we had" — and it needs the
    # session id to continue THIS exchange rather than opening another thread.
    def fake_handler(args):
        print(
            json.dumps(
                {
                    "ok": True,
                    "reply": "ack",
                    "session_id": "persona_chat_personainst_qa_ffffffffffff",
                    "session_established": {
                        "fresh": True,
                        "reason": "policy_new_per_dispatch",
                        "predecessor_session_id": "persona_chat_personainst_qa_aaaaaaaaaaaa",
                    },
                }
            )
        )
        return 0

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", fake_handler)
    data = json.loads(agent_chat_send(persona_id="qa", message="hi"))
    assert data["session_established"] == {
        "fresh": True,
        "reason": "policy_new_per_dispatch",
        "predecessor_session_id": "persona_chat_personainst_qa_aaaaaaaaaaaa",
    }
    assert data["session_id"] == "persona_chat_personainst_qa_ffffffffffff"


def test_result_stays_compact_when_the_handler_reports_no_lineage(monkeypatch):
    # The compact result is the whole point of this lane (the handler payload is
    # ~75KB); an absent block must not become a null key on every reply.
    def fake_handler(args):
        print(json.dumps({"ok": True, "reply": "ack"}))
        return 0

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", fake_handler)
    assert "session_established" not in json.loads(agent_chat_send(persona_id="qa", message="hi"))


def test_new_session_with_explicit_session_is_a_typed_refusal(monkeypatch):
    def fake_handler(args):  # pragma: no cover - must not be reached
        raise AssertionError("contradictory thread target must refuse before dispatch")

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", fake_handler)
    data = json.loads(
        agent_chat_send(
            persona_id="qa",
            message="hi",
            new_session=True,
            session_id="persona_chat_personainst_qa_abc",
        )
    )
    assert data["ok"] is False
    assert data["error_kind"] == "contradictory_thread_target"


def test_clarify_token_is_offered_and_forwarded_verbatim(monkeypatch):
    # The echo half of the clarify binding. The tool never RESOLVES the token —
    # one ticket-store authority, and it lives with the handler that owns the
    # session lane — so all this surface owes is an honest schema slot and an
    # untouched forward.
    assert "clarify_token" in AGENT_CHAT_SEND_SCHEMA["parameters"]["properties"]
    assert "clarify_token" not in AGENT_CHAT_SEND_SCHEMA["parameters"]["required"]

    seen = {}

    def fake_handler(args):
        seen["clarify_token"] = args.clarify_token
        print(json.dumps({"ok": True, "reply": "ack"}))
        return 0

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", fake_handler)
    assert json.loads(
        agent_chat_send(persona_id="qa", message="launcher", clarify_token="clarify-9f2c4ab17d03")
    )["ok"]
    assert seen["clarify_token"] == "clarify-9f2c4ab17d03"
    # An omitted token reaches the handler as None, never as an empty string a
    # store lookup would have to special-case.
    assert json.loads(agent_chat_send(persona_id="qa", message="hi"))["ok"]
    assert seen["clarify_token"] is None


def test_the_registry_handler_passes_the_clarify_token_through(monkeypatch):
    # The model calls this tool through the registry, not the Python function;
    # a kwarg the registry lambda forgets is a kwarg the model can never use.
    seen = {}

    def fake_handler(args):
        seen["clarify_token"] = args.clarify_token
        print(json.dumps({"ok": True, "reply": "ack"}))
        return 0

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", fake_handler)
    entry = registry.get_entry("agent_chat_send")
    entry.handler(
        {"persona_id": "qa", "message": "launcher", "clarify_token": "clarify-abcdef123456"}
    )
    assert seen["clarify_token"] == "clarify-abcdef123456"


def test_clarify_token_with_new_session_is_a_typed_refusal(monkeypatch):
    # The ONE clarify combination with no correct reading: "put this answer
    # where the question was" and "put it somewhere new" cannot both hold. A
    # stale session_id alongside a token is NOT this case — the token
    # deliberately wins that one downstream, because getting session_id wrong is
    # the exact failure the token absorbs.
    def fake_handler(args):  # pragma: no cover - must not be reached
        raise AssertionError("contradictory thread target must refuse before dispatch")

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", fake_handler)
    data = json.loads(
        agent_chat_send(
            persona_id="qa",
            message="launcher",
            new_session=True,
            clarify_token="clarify-9f2c4ab17d03",
        )
    )
    assert data["ok"] is False
    assert data["error_kind"] == "contradictory_thread_target"
    assert "clarify_token" in data["error"]


def test_clarify_binding_is_returned_to_the_answering_agent(monkeypatch):
    # Where the answer actually landed, and why — including a session_id the
    # token outranked, so the override is never silent to the caller either.
    def fake_handler(args):
        print(
            json.dumps(
                {
                    "ok": True,
                    "reply": "ack",
                    "session_id": "persona_chat_personainst_qa_ffffffffffff",
                    "clarify_binding": {
                        "token": "clarify-9f2c4ab17d03",
                        "state": "bound",
                        "bound_via": "clarify_token",
                        "bound_session_id": "persona_chat_personainst_qa_ffffffffffff",
                        "overrode_session_id": "persona_chat_personainst_qa_aaaaaaaaaaaa",
                    },
                }
            )
        )
        return 0

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", fake_handler)
    data = json.loads(agent_chat_send(persona_id="qa", message="launcher"))
    assert data["clarify_binding"]["bound_via"] == "clarify_token"
    assert (
        data["clarify_binding"]["overrode_session_id"]
        == "persona_chat_personainst_qa_aaaaaaaaaaaa"
    )

    # Absent on every ordinary turn — the compact result must not grow a null key.
    def quiet_handler(args):
        print(json.dumps({"ok": True, "reply": "ack"}))
        return 0

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", quiet_handler)
    assert "clarify_binding" not in json.loads(agent_chat_send(persona_id="qa", message="hi"))


def test_threads_lists_default_thread_without_minting_for_never_chatted(isolate_agent_runtime_root):
    from agent_runtime.persona_assignments import (
        PersonaInstanceStore,
        resolve_default_chat_session_id_for_instance,
    )

    data = json.loads(agent_chat_threads())
    assert data["ok"] is True
    by_persona = {row["persona_id"]: row for row in data["threads"]}
    assert "qa" in by_persona, "reachable teammates must be listed"
    qa = by_persona["qa"]
    assert qa["handle"] == "personainst_qa"
    assert qa["has_thread"] is False and qa["session_id"] is None
    # Listing must not have created a session for qa.
    assert resolve_default_chat_session_id_for_instance(PersonaInstanceStore(), persona_id="qa") is None


def test_threads_shows_the_thread_with_a_count_after_a_send(isolate_agent_runtime_root):
    session_id = _seed_persona_chat("qa", [("From Neko: hi", "QA here — hi."), ("again", "ack")])

    data = json.loads(agent_chat_threads(persona_id="qa"))
    rows = [row for row in data["threads"] if row["persona_id"] == "qa"]
    assert len(rows) == 1
    row = rows[0]
    assert row["has_thread"] is True
    assert row["session_id"] == session_id
    assert row["message_count"] == 4  # 2 operator + 2 agent


def test_open_returns_the_bounded_tail_and_canonicalizes_handle_input(isolate_agent_runtime_root):
    session_id = _seed_persona_chat("qa", [("From Neko: hi", "QA here — hi."), ("more", "ack")])

    # Handle input canonicalizes to the persona (the resolution send already does).
    data = json.loads(agent_chat_open(persona_id="personainst_qa", limit=1))
    assert data["ok"] is True
    assert data["target_persona"] == "qa"
    assert data["session_id"] == session_id
    assert data["has_thread"] is True
    assert data["count"] == 1  # bounded by limit
    assert set(data["messages"][0]) >= {"role", "text", "timestamp"}


def test_open_refuses_a_foreign_session(isolate_agent_runtime_root):
    _seed_persona_chat("qa", [("hi", "ack")])
    # A session belonging to a different teammate's chat lane is refused — this is
    # "review OUR thread", not a transcript browser.
    data = json.loads(
        agent_chat_open(persona_id="qa", session_id="persona_chat_personainst_dev_deadbeef0000")
    )
    assert data["ok"] is False
    assert data["error_kind"] == "foreign_session"


def test_send_forwards_a_personainst_handle_as_the_target_instance(monkeypatch):
    # Multi-instance targeting: a personainst_* handle in the persona slot must be
    # forwarded as persona_instance_id so the handler threads THAT instance (not
    # the persona's canonical channel). A bare persona id forwards no handle.
    seen = {}

    def fake_handler(args):
        seen["persona_id"] = args.persona_id
        seen["persona_instance_id"] = args.persona_instance_id
        print(json.dumps({"ok": True, "reply": "ack"}))
        return 0

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", fake_handler)

    assert json.loads(agent_chat_send(persona_id="personainst_qa_agent_2", message="hi"))["ok"]
    assert seen["persona_id"] == "personainst_qa_agent_2"
    assert seen["persona_instance_id"] == "personainst_qa_agent_2"

    assert json.loads(agent_chat_send(persona_id="qa", message="hi"))["ok"]
    assert seen["persona_id"] == "qa"
    assert seen["persona_instance_id"] is None


def test_threads_lists_each_placement_distinctly_and_shadows_canonical(isolate_agent_runtime_root):
    # Placements shadow canonical: two in-scope placements are listed distinctly,
    # each by its own handle + own thread, and the plumbing canonical row is NOT
    # advertised (the agent addresses the deliberate placements on its level).
    from agent_runtime.persona_assignments import PersonaInstanceStore

    store = PersonaInstanceStore()
    sib2 = store.add_instance(persona_id="qa", placement_id="qa_agent_2", display_name="QA Agent 2")
    sib3 = store.add_instance(persona_id="qa", placement_id="qa_agent_3", display_name="QA Agent 3")
    # The canonical row even has its own thread — but it is still shadowed.
    _seed_persona_chat("qa", [("primary hi", "primary ack")])
    s2 = _seed_persona_chat("qa", [("s2 hi", "s2 ack")], persona_instance_id=sib2.id)
    s3 = _seed_persona_chat("qa", [("s3 hi", "s3 ack")], persona_instance_id=sib3.id)
    assert s2 != s3

    data = json.loads(agent_chat_threads(persona_id="qa"))
    by_handle = {row["handle"]: row for row in data["threads"] if row["persona_id"] == "qa"}
    assert set(by_handle) == {"personainst_qa_agent_2", "personainst_qa_agent_3"}
    assert "personainst_qa" not in by_handle  # canonical shadowed by the placements
    assert by_handle["personainst_qa_agent_2"]["session_id"] == s2
    assert by_handle["personainst_qa_agent_3"]["session_id"] == s3

    # A handle filter is explicit targeting and narrows to that one instance.
    only = json.loads(agent_chat_threads(persona_id=sib2.id))
    handles = [row["handle"] for row in only["threads"]]
    assert handles == ["personainst_qa_agent_2"]


def test_threads_shows_canonical_when_no_placement_shadows_it(isolate_agent_runtime_root):
    # Reachability fallback: a persona with only its canonical row (no placement)
    # is still listed — the canonical channel stays addressable.
    primary_session = _seed_persona_chat("qa", [("primary hi", "primary ack")])

    data = json.loads(agent_chat_threads(persona_id="qa"))
    by_handle = {row["handle"]: row for row in data["threads"] if row["persona_id"] == "qa"}
    assert "personainst_qa" in by_handle
    assert by_handle["personainst_qa"]["session_id"] == primary_session


def test_open_bare_persona_routes_to_the_in_scope_placement(isolate_agent_runtime_root):
    from agent_runtime.persona_assignments import PersonaInstanceStore

    sibling = PersonaInstanceStore().add_instance(
        persona_id="qa", placement_id="qa_agent_2", display_name="QA Agent 2"
    )
    primary_session = _seed_persona_chat("qa", [("primary hi", "primary ack")])
    sibling_session = _seed_persona_chat(
        "qa", [("sibling hi", "sibling ack")], persona_instance_id=sibling.id
    )

    # An explicit handle reviews the SIBLING's thread (deliberate targeting).
    opened_sibling = json.loads(agent_chat_open(persona_id=sibling.id))
    assert opened_sibling["session_id"] == sibling_session
    assert [m["text"] for m in opened_sibling["messages"]] == ["sibling hi", "sibling ack"]

    # A BARE persona now resolves through addressability: qa_agent_2 is the single
    # in-scope placement, so "open qa" reviews the PLACEMENT's lane (placements
    # shadow canonical), not the plumbing canonical channel.
    opened_bare = json.loads(agent_chat_open(persona_id="qa"))
    assert opened_bare["handle"] == "personainst_qa_agent_2"
    assert opened_bare["session_id"] == sibling_session

    # The explicit CANONICAL handle still reaches the canonical thread (explicit
    # targeting bypasses the shadow)...
    opened_canonical = json.loads(agent_chat_open(persona_id="personainst_qa"))
    assert opened_canonical["session_id"] == primary_session

    # ...and it must NOT be able to open the sibling's session (prefix-collision
    # guard: personainst_qa must not swallow personainst_qa_agent_2's session).
    refused = json.loads(agent_chat_open(persona_id="personainst_qa", session_id=sibling_session))
    assert refused["ok"] is False and refused["error_kind"] == "foreign_session"


def _seed_persona_chat(persona_id: str, turns, *, persona_instance_id=None):
    """Handler-equivalent persistence (no LLM turn): resolve the default session
    through the chokepoint (optionally for a SPECIFIC instance), bind it via
    open_chat, and write the SessionDB row + messages the projection keys on.
    Mirrors test_relay_session_lifecycle's end-to-end helper."""
    from agent_runtime.persona_assignments import (
        PersonaInstanceStore,
        default_chat_session_id_for_instance,
    )
    from agent_runtime.persona_chat_history import PERSONA_CHAT_SESSION_SOURCE
    from hermes_state import SessionDB

    store = PersonaInstanceStore()
    db = SessionDB()
    session_id = default_chat_session_id_for_instance(
        store, persona_id=persona_id, persona_instance_id=persona_instance_id
    )
    store.open_chat(
        persona_id=persona_id,
        persona_instance_id=persona_instance_id,
        session_id=session_id,
        display_name=persona_id.upper(),
    )
    db.create_session(
        session_id=session_id,
        source=PERSONA_CHAT_SESSION_SOURCE,
        model=None,
        system_prompt=f"Mission Control persona chat for {persona_id}",
    )
    for message, reply in turns:
        db.append_message(session_id=session_id, role="user", content=message)
        db.append_message(session_id=session_id, role="assistant", content=reply)
    return session_id
