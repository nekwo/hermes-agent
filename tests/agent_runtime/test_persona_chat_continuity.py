from __future__ import annotations

import json
import time

import pytest

from agent_runtime.mission_chat_turns import (
    MissionChatTurnPersistOutcome,
    abandon_mission_chat_turn,
    mission_chat_turn_record,
    transition_mission_chat_turn,
)
from agent_runtime.models import PersonaInstance
from agent_runtime.persona_assignments import PersonaInstanceStore
from agent_runtime.persona_chat_continuity import (
    PersonaChatBusyError,
    PersonaChatMintReceiptStore,
    PersonaChatRuntimeRegistry,
    current_tool_execution_scope,
    initialize_persona_chat_runtime_registry,
    native_lineage_summary,
    persona_chat_runtime_registry,
    persona_chat_root_lease,
    safe_native_history,
    safe_native_message,
    tool_execution_scope,
)
from agent_runtime.states import WorkerSessionState
from agent_runtime.profile_runner import _finish_resident_persona_chat_agent
from hermes_state import SessionDB


def _instance(**overrides) -> PersonaInstance:
    values = {
        "id": "personainst_dev",
        "persona_id": "dev",
        "role": "dev",
        "display_name": "Dev",
        "profile_id": "dev",
        "runtime_root": "runtime",
        "state": WorkerSessionState.IDLE,
    }
    values.update(overrides)
    return PersonaInstance(**values)


def test_01_legacy_chat_pointer_migrates_without_worker_alias():
    row = _instance(session_id="persona_chat_personainst_dev_old")
    assert row.default_chat_session_id == "persona_chat_personainst_dev_old"


def test_02_worker_update_never_overwrites_default_chat(isolate_agent_runtime_root):
    store = PersonaInstanceStore()
    row = store.open_chat(persona_id="dev", session_id="persona_chat_root")
    row.active_worker_session_id = "worker_1"
    row.session_id = "worker_session"
    store.update(row)
    assert store.get(row.id).default_chat_session_id == "persona_chat_root"


def test_03_multiple_roots_repoint_only_the_default(isolate_agent_runtime_root):
    store = PersonaInstanceStore()
    first = store.open_chat(persona_id="dev", session_id="persona_chat_first")
    second = store.open_chat(persona_id="dev", session_id="persona_chat_second")
    assert first.id == second.id
    assert second.default_chat_session_id == "persona_chat_second"


def test_04_mint_same_key_replays_same_root(isolate_agent_runtime_root, tmp_path):
    db = SessionDB(tmp_path / "state.db")
    store = PersonaInstanceStore()
    receipts = PersonaChatMintReceiptStore()
    one = receipts.mint(instance_store=store, session_db=db, persona_id="dev", persona_instance_id="personainst_dev", idempotency_key="same")
    two = receipts.mint(instance_store=store, session_db=db, persona_id="dev", persona_instance_id="personainst_dev", idempotency_key="same")
    assert one["root_chat_session_id"] == two["root_chat_session_id"]


def test_05_mint_different_keys_create_different_roots(isolate_agent_runtime_root, tmp_path):
    db = SessionDB(tmp_path / "state.db")
    store = PersonaInstanceStore()
    receipts = PersonaChatMintReceiptStore()
    one = receipts.mint(instance_store=store, session_db=db, persona_id="dev", persona_instance_id="personainst_dev", idempotency_key="one", title="Dev chat")
    two = receipts.mint(instance_store=store, session_db=db, persona_id="dev", persona_instance_id="personainst_dev", idempotency_key="two", title="Dev chat")
    assert one["root_chat_session_id"] != two["root_chat_session_id"]
    assert db.get_session_title(one["root_chat_session_id"]) != db.get_session_title(two["root_chat_session_id"])


def test_06_reserved_mint_receipt_survives_retry(isolate_agent_runtime_root, tmp_path):
    db = SessionDB(tmp_path / "state.db")
    store = PersonaInstanceStore()
    receipts = PersonaChatMintReceiptStore()
    path = receipts._path("personainst_dev", "reserved")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"root_chat_session_id": "persona_chat_reserved", "state": "reserved"}), encoding="utf-8")
    replay = receipts.mint(instance_store=store, session_db=db, persona_id="dev", persona_instance_id="personainst_dev", idempotency_key="reserved")
    assert replay["root_chat_session_id"] == "persona_chat_reserved"
    assert replay["state"] == "completed"


def test_07_mint_stamps_exact_owner_metadata(isolate_agent_runtime_root, tmp_path):
    db = SessionDB(tmp_path / "state.db")
    receipt = PersonaChatMintReceiptStore().mint(instance_store=PersonaInstanceStore(), session_db=db, persona_id="dev", persona_instance_id="personainst_dev", idempotency_key="owner")
    meta = json.loads(db.get_session(receipt["root_chat_session_id"])["model_config"])
    assert meta["persona_instance_id"] == "personainst_dev"


def test_08_same_root_lease_is_exclusive(isolate_agent_runtime_root):
    with persona_chat_root_lease("root_a"):
        with pytest.raises(PersonaChatBusyError):
            with persona_chat_root_lease("root_a"):
                pass


def test_09_different_root_leases_do_not_contend(isolate_agent_runtime_root):
    with persona_chat_root_lease("root_a"):
        with persona_chat_root_lease("root_b"):
            assert True


def test_10_journal_happy_path_has_six_state_subset(isolate_agent_runtime_root):
    for state in ("pending", "executing", "native_committed", "projected"):
        assert transition_mission_chat_turn(session_id="root", client_message_id="client", turn_id="turn", state=state) is MissionChatTurnPersistOutcome.PERSISTED
    assert mission_chat_turn_record(session_id="root", client_message_id="client")["state"] == "projected"


def test_11_journal_rejects_backward_transition(isolate_agent_runtime_root):
    transition_mission_chat_turn(session_id="root", client_message_id="client", turn_id="turn", state="pending")
    transition_mission_chat_turn(session_id="root", client_message_id="client", turn_id="turn", state="executing")
    assert transition_mission_chat_turn(session_id="root", client_message_id="client", turn_id="turn", state="pending") is MissionChatTurnPersistOutcome.REJECTED_STALE_TRANSITION


def test_12_native_committed_can_repair_projection(isolate_agent_runtime_root):
    for state in ("pending", "executing", "native_committed"):
        transition_mission_chat_turn(session_id="root", client_message_id="client", turn_id="turn", state=state, metadata={"stored_reply": "done"})
    transition_mission_chat_turn(session_id="root", client_message_id="client", turn_id="turn", state="projected")
    assert mission_chat_turn_record(session_id="root", client_message_id="client")["stored_reply"] == "done"


def test_13_executing_can_become_outcome_unknown(isolate_agent_runtime_root, monkeypatch):
    from hermes_cli import harness

    transition_mission_chat_turn(session_id="root", client_message_id="client", turn_id="turn", state="pending")
    transition_mission_chat_turn(session_id="root", client_message_id="client", turn_id="turn", state="executing")
    transition_mission_chat_turn(session_id="root", client_message_id="client", turn_id="turn", state="outcome_unknown")
    assert mission_chat_turn_record(session_id="root", client_message_id="client")["state"] == "outcome_unknown"
    monkeypatch.setenv("HERMES_PERSONA_CHAT_FAULT_INJECTION", "after_provider_boundary")
    with pytest.raises(RuntimeError, match="after_provider_boundary"):
        harness._persona_chat_fault_injection("after_provider_boundary")


def test_14_exact_unknown_turn_can_be_abandoned(isolate_agent_runtime_root):
    for state in ("pending", "executing", "outcome_unknown"):
        transition_mission_chat_turn(session_id="root", client_message_id="client", turn_id="turn", state=state)
    assert abandon_mission_chat_turn(session_id="root", client_message_id="client", turn_id="turn") is MissionChatTurnPersistOutcome.PERSISTED


def test_15_abandon_rejects_turn_mismatch(isolate_agent_runtime_root):
    for state in ("pending", "executing", "outcome_unknown"):
        transition_mission_chat_turn(session_id="root", client_message_id="client", turn_id="turn", state=state)
    assert abandon_mission_chat_turn(session_id="root", client_message_id="client", turn_id="other") is MissionChatTurnPersistOutcome.REJECTED_STALE_TRANSITION


def test_16_safe_message_redacts_secrets():
    assert "topsecret" not in safe_native_message({"role": "user", "content": "api_key=topsecret"})["content"]


def test_17_safe_message_caps_content():
    assert "[truncated]" in safe_native_message({"role": "user", "content": "x" * 30000})["content"]


def test_18_safe_history_drops_orphan_tool_results():
    assert safe_native_history([{"role": "tool", "tool_call_id": "missing", "content": "x"}]) == []


def test_19_safe_history_preserves_tool_pair_structure():
    rows = safe_native_history([{"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "function": {"name": "read", "arguments": "{}"}}]}, {"role": "tool", "tool_call_id": "call_1", "content": "ok"}])
    assert [row["role"] for row in rows] == ["assistant", "tool"]


def test_safe_history_deduplicates_compression_lineage_turn_copies():
    rows = safe_native_history(
        [
            {"role": "user", "content": "continue", "client_message_id": "turn-1"},
            {"role": "user", "content": "continue", "client_message_id": "turn-1"},
            {
                "role": "assistant",
                "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary",
                "client_message_id": "turn-1:assistant:0",
            },
            {
                "role": "assistant",
                "content": "done",
                "client_message_id": "turn-1:assistant:2",
            },
            {
                "role": "assistant",
                "content": "done",
                "client_message_id": "turn-1:assistant:4",
            },
        ]
    )

    assert [(row["role"], row["content"]) for row in rows] == [
        ("user", "continue"),
        ("assistant", "[CONTEXT COMPACTION — REFERENCE ONLY] summary"),
        ("assistant", "done"),
    ]


def test_20_registry_reuses_same_root_and_revision():
    registry = PersonaChatRuntimeRegistry()
    one, reused, _ = registry.acquire(root_session_id="root", active_session_id="tip", signature="sig", revision="rev", factory=object)
    two, reused_again, _ = registry.acquire(root_session_id="root", active_session_id="tip", signature="sig", revision="rev", factory=object)
    assert not reused and reused_again and one.agent is two.agent


def test_21_registry_rebuilds_on_signature_change():
    registry = PersonaChatRuntimeRegistry()
    registry.acquire(root_session_id="root", active_session_id="tip", signature="one", revision="rev", factory=object)
    _, reused, reason = registry.acquire(root_session_id="root", active_session_id="tip", signature="two", revision="rev", factory=object)
    assert not reused and reason == "runtime_signature_changed"


def test_22_registry_rebuilds_on_disk_revision_change():
    registry = PersonaChatRuntimeRegistry()
    registry.acquire(root_session_id="root", active_session_id="tip", signature="sig", revision="one", factory=object)
    _, reused, reason = registry.acquire(root_session_id="root", active_session_id="tip", signature="sig", revision="two", factory=object)
    assert not reused and reason == "disk_revision_changed"


def test_23_registry_lru_is_bounded():
    registry = PersonaChatRuntimeRegistry(max_entries=1)
    registry.acquire(root_session_id="one", active_session_id="one", signature="sig", revision="rev", factory=object)
    registry.acquire(root_session_id="two", active_session_id="two", signature="sig", revision="rev", factory=object)
    assert registry.observation("one", owning_process=True)["runtime_state"] == "cold"


def test_24_registry_observer_truth_and_transitions():
    registry = PersonaChatRuntimeRegistry()
    registry.transition("root", "busy")
    assert registry.observation("root", owning_process=True)["runtime_state"] == "busy"
    assert registry.observation("root", owning_process=False)["runtime_state"] == "unknown"


def test_25_tool_scope_is_root_stable_and_restored():
    assert current_tool_execution_scope() is None
    with tool_execution_scope("root"):
        assert current_tool_execution_scope() == "root"
    assert current_tool_execution_scope() is None


def test_26_delete_root_removes_compression_lineage_but_preserves_branch(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("root", "agent_runtime_persona_chat")
    db.end_session("root", "compression")
    db.create_session("tip", "agent_runtime_persona_chat", parent_session_id="root")
    db.create_session("branch", "agent_runtime_persona_chat", parent_session_id="root", model_config={"_branched_from": "root"})
    assert db.delete_compression_lineage("root") == ["root", "tip"]
    assert db.get_session("branch")["parent_session_id"] is None


def test_27_json_shaped_tool_secret_is_redacted():
    row = safe_native_message(
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {
                        "name": "terminal",
                        "arguments": '{"token":"topsecret","command":"ok"}',
                    },
                }
            ],
        }
    )
    assert "topsecret" not in row["tool_calls"][0]["function"]["arguments"]


def test_28_safe_history_drops_unpaired_assistant_tool_call():
    rows = safe_native_history(
        [
            {
                "role": "assistant",
                "content": "still useful",
                "tool_calls": [
                    {
                        "id": "call_missing",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
            }
        ]
    )
    assert rows == [{"role": "assistant", "content": "still useful"}]


def test_29_lineage_depth_is_compression_depth_not_turn_count(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("root", "agent_runtime_persona_chat")
    db.end_session("root", "compression")
    db.create_session("tip", "agent_runtime_persona_chat", parent_session_id="root")
    assert native_lineage_summary(db, "root") == {
        "active_session_id": "tip",
        "continuation_depth": 1,
    }


def test_30_hot_registry_can_be_dark_launched():
    initialize_persona_chat_runtime_registry(enabled=False)
    assert persona_chat_runtime_registry() is None
    initialize_persona_chat_runtime_registry(enabled=True)
    assert persona_chat_runtime_registry() is not None


def test_31_resident_finish_detaches_turn_handles_without_erasing_history():
    marker = object()
    agent = type("Resident", (), {})()
    agent.messages = [{"role": "user", "content": "durable"}]
    agent.status_callback = marker
    agent.tool_progress_callback = marker
    agent.tool_start_callback = marker
    agent.tool_complete_callback = marker
    agent.clarify_callback = marker
    agent._stream_callback = marker
    agent._persona_chat_client_message_id = "client-old"
    agent._persona_chat_turn_id = "turn-old"

    _finish_resident_persona_chat_agent(agent)

    assert agent.messages == [{"role": "user", "content": "durable"}]
    assert agent.status_callback is None
    assert agent.tool_progress_callback is None
    assert agent.tool_start_callback is None
    assert agent.tool_complete_callback is None
    assert agent.clarify_callback is None
    assert agent._stream_callback is None
    assert agent._persona_chat_client_message_id is None
    assert agent._persona_chat_turn_id is None
