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
    CLARIFY_TICKET_TTL_SECONDS,
    PersonaChatBusyError,
    PersonaChatClarifyTicketStore,
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


def test_02_non_chat_session_write_never_overwrites_default_chat(isolate_agent_runtime_root):
    # S56 deleted PersonaInstance.active_worker_session_id with the worker
    # session store; the surviving guard is that a non-``persona_chat_*``
    # session_id write must not clobber the default chat pointer.
    store = PersonaInstanceStore()
    row = store.open_chat(persona_id="dev", session_id="persona_chat_root")
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


def test_09a_a_failed_byte_unlock_is_reported_and_release_still_works(
    isolate_agent_runtime_root, caplog
):
    """A swallowed unlock failure is the producer side of the stale-lock class.

    The release `finally` may never raise and may never skip `os.close(fd)` —
    that part is unchanged and is asserted below by re-acquiring the same root.
    What changed is that the failure is no longer a bare `pass`: without a line
    naming the root, the delivery drain's `lease_busy_ownerless` on the consumer
    side has nothing to correlate against and stays a hypothesis forever.

    THE UNLOCK STUB IS SCOPED (EG-0.1). Dropping it with ``monkeypatch.undo()``
    unwound the shared per-test instance, so the second lease below was taken
    against the OPERATOR's live runtime root — the lease file out there is the
    physical evidence. A scoped context drops the stub and only the stub.
    """

    import logging

    from agent_runtime import persona_chat_continuity

    with pytest.MonkeyPatch.context() as broken_unlock:
        broken_unlock.setattr(
            persona_chat_continuity,
            "_unlock",
            lambda fd: (_ for _ in ()).throw(OSError("unlock refused")),
        )

        with caplog.at_level(logging.WARNING, logger=persona_chat_continuity.__name__):
            with persona_chat_root_lease("root_unlock_fail"):
                pass

    warnings = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
    assert any("root_unlock_fail" in message and "unlock" in message.lower() for message in warnings), warnings

    # Behaviour unchanged: the handle close still released the lock, so the
    # very next acquisition of the same root succeeds — with the real `_unlock`
    # back, because the context above has exited.
    with persona_chat_root_lease("root_unlock_fail"):
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


def test_orphan_sweep_repairs_only_lease_free_sessions(isolate_agent_runtime_root):
    """Live incident 2026-07-25: a reaped Launcher killed the serve child
    mid-turn and the QA record froze at ``executing``. The boot sweep must
    settle exactly the sessions whose root lease is acquirable (dead
    executor) and leave lease-held sessions (live turns) alone."""

    from agent_runtime.events import EventLog
    from agent_runtime.persona_chat_continuity import repair_orphaned_chat_turns

    for root in ("root_dead", "root_live"):
        transition_mission_chat_turn(
            session_id=root,
            client_message_id="m1",
            turn_id="t1",
            state="pending",
            metadata={"root_chat_session_id": root},
        )
        transition_mission_chat_turn(
            session_id=root,
            client_message_id="m1",
            turn_id="t1",
            state="executing",
        )

    with persona_chat_root_lease("root_live"):
        repaired = repair_orphaned_chat_turns()

    assert repaired == ["root_dead"]
    dead = mission_chat_turn_record(session_id="root_dead", client_message_id="m1")
    assert dead["state"] == "interrupted"
    live = mission_chat_turn_record(session_id="root_live", client_message_id="m1")
    assert live["state"] == "executing"
    # The repair is a store mutation: watermark-gated consumers must converge.
    tail = EventLog().tail(1)
    assert tail and tail[0].type == "state.reconciled"
    assert tail[0].payload["source"] == "chat_orphan_sweep"


def test_orphan_sweep_without_orphans_appends_no_event(isolate_agent_runtime_root):
    from agent_runtime.events import EventLog
    from agent_runtime.persona_chat_continuity import repair_orphaned_chat_turns

    for state in ("pending", "executing", "native_committed", "projected"):
        transition_mission_chat_turn(
            session_id="root_settled",
            client_message_id="m1",
            turn_id="t1",
            state=state,
            metadata={"root_chat_session_id": "root_settled"},
        )

    assert repair_orphaned_chat_turns() == []
    assert EventLog().tail(1) == []


# ── clarify tickets: the answer's binding to its question's thread ──────────


_CLARIFY_ROOT = "persona_chat_personainst_dev_abcdef123456"


def _clarify_ticket(store: PersonaChatClarifyTicketStore, **overrides) -> str:
    values = {
        "chat_session_id": _CLARIFY_ROOT,
        "persona_instance_id": "personainst_dev",
        "persona_id": "dev",
        "asked_by_client_message_id": "agent-relay-aaaaaaaaaaaa",
    }
    values.update(overrides)
    token = store.mint(**values)
    assert token
    return token


def test_clarify_ticket_round_trips_a_token_to_its_session(isolate_agent_runtime_root):
    store = PersonaChatClarifyTicketStore()
    token = _clarify_ticket(store)

    assert token.startswith("clarify-")
    record = store.resolve(token)
    assert record["chat_session_id"] == _CLARIFY_ROOT
    assert record["persona_instance_id"] == "personainst_dev"
    assert record["state"] == "open"


def test_an_unknown_clarify_token_resolves_to_nothing_instead_of_raising(
    isolate_agent_runtime_root,
):
    # The degrade path. A pruned or fabricated token must be an ordinary "no",
    # because the caller turns it into a fallthrough, not a refusal.
    store = PersonaChatClarifyTicketStore()
    assert store.resolve("clarify-deadbeefdead") is None
    assert store.resolve(None) is None
    assert store.resolve("") is None
    assert store.settle("clarify-deadbeefdead", client_message_id="cm-1") is None


def test_the_clarify_token_never_appears_in_a_filename(isolate_agent_runtime_root):
    """The token is a caller-supplied string from a model.

    Interpolating one into a path is traversal, so the filename is the digest —
    the same precedent the mint receipt store set."""

    import hashlib

    from agent_runtime import paths

    store = PersonaChatClarifyTicketStore()
    token = _clarify_ticket(store)

    files = list((paths.store_root() / "persona_chat_clarify_tickets").glob("*.json"))
    assert len(files) == 1
    assert files[0].stem == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert token not in files[0].name


def test_settling_the_same_message_twice_is_idempotent(isolate_agent_runtime_root):
    # A lease re-entry or a relay retry re-presents the SAME client_message_id.
    # It is one answer, presented twice — never a second one.
    store = PersonaChatClarifyTicketStore()
    token = _clarify_ticket(store)

    first = store.settle(token, client_message_id="cm-answer")
    second = store.settle(token, client_message_id="cm-answer")

    assert first["state"] == "answered"
    assert second["state"] == "answered"
    assert second["answered_by_client_message_id"] == "cm-answer"
    assert second["answered_at"] == first["answered_at"]


def test_a_second_distinct_answer_is_reported_as_a_rebind(isolate_agent_runtime_root):
    # A spent token still BINDS — a follow-up after the answer belongs in the
    # same conversation — so single-use governs accounting, not permission.
    store = PersonaChatClarifyTicketStore()
    token = _clarify_ticket(store)

    store.settle(token, client_message_id="cm-answer")
    again = store.settle(token, client_message_id="cm-follow-up")

    assert again["state"] == "rebound"
    assert store.resolve(token)["chat_session_id"] == _CLARIFY_ROOT


def test_an_expired_ticket_still_binds_until_it_is_swept(isolate_agent_runtime_root):
    # TTL governs GC only. One rule, no cliff: a ticket that outlived its TTL
    # keeps working until a sweep actually removes the file.
    store = PersonaChatClarifyTicketStore()
    token = _clarify_ticket(store)
    path = store._path(token)
    stale = json.loads(path.read_text(encoding="utf-8"))
    stale["created_at"] = time.time() - (CLARIFY_TICKET_TTL_SECONDS * 2)
    path.write_text(json.dumps(stale), encoding="utf-8")

    assert store.resolve(token)["chat_session_id"] == _CLARIFY_ROOT
    assert store.sweep() == 1
    assert store.resolve(token) is None


def test_the_sweep_keeps_tickets_inside_their_ttl(isolate_agent_runtime_root):
    store = PersonaChatClarifyTicketStore()
    token = _clarify_ticket(store)

    assert store.sweep() == 0
    assert store.resolve(token) is not None


def test_minting_a_ticket_reclaims_the_expired_ones(isolate_agent_runtime_root):
    """The GC has a CALLER — the defect a defined-but-uncalled TTL hides.

    ``sweep`` existed with no call site, so nothing ever pruned: the directory
    grew for the life of the runtime. That is not merely disk, because
    ``open_ticket_for_session`` reads EVERY file in it and the tokenless
    settlement runs that on every mission-chat turn — so an unpruned store makes
    each turn pay for every question ever asked. Pinned on the mint, the lane's
    only cold seam: asking a question is rare, taking a turn is not."""

    store = PersonaChatClarifyTicketStore()
    expired = [_clarify_ticket(store) for _ in range(3)]
    for token in expired:
        path = store._path(token)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["created_at"] = time.time() - (CLARIFY_TICKET_TTL_SECONDS * 2)
        path.write_text(json.dumps(record), encoding="utf-8")
    fresh = _clarify_ticket(store)

    # Minting the NEXT question reclaimed all three, and never itself.
    assert [store.resolve(token) for token in expired] == [None, None, None]
    assert store.resolve(fresh)["chat_session_id"] == _CLARIFY_ROOT
    # …so the per-turn lookup only ever walks the live window.
    assert store.open_ticket_for_session(_CLARIFY_ROOT)["clarify_token"] == fresh


def test_the_open_ticket_for_a_session_is_the_newest_unanswered_one(
    isolate_agent_runtime_root,
):
    # Settlement without a token rides this lookup: any turn landing in a
    # session with an open question answers it. Without it, every parent that
    # complied via session_id would leave a permanently-open ticket and the
    # adoption metric would read pessimistically forever.
    store = PersonaChatClarifyTicketStore()
    other = _clarify_ticket(store, chat_session_id="persona_chat_personainst_qa_bbbbbbbbbbbb")
    older = _clarify_ticket(store)
    newer = _clarify_ticket(store)

    found = store.open_ticket_for_session(_CLARIFY_ROOT)
    assert found["clarify_token"] == newer

    store.settle(newer, client_message_id="cm-answer", bound_via="session_id")
    assert store.open_ticket_for_session(_CLARIFY_ROOT)["clarify_token"] == older
    # …and a different session's question was never touched.
    assert store.resolve(other)["state"] == "open"
    assert store.open_ticket_for_session(None) is None


def test_a_ticket_with_no_session_is_never_minted(isolate_agent_runtime_root):
    # Nothing to bind to is not an error, it is an absence: the question still
    # ships, the answer just falls through to today's precedence.
    assert PersonaChatClarifyTicketStore().mint(chat_session_id="") is None


# ── the open-ticket lookup is O(1), and is never its own authority ──────────


def _files_read(monkeypatch) -> list[str]:
    """Every file the next call actually opens."""

    from pathlib import Path

    opened: list[str] = []
    original = Path.read_text

    def counting_read_text(self, *args, **kwargs):
        # Counted on SUCCESS: a probe for a file that is not there costs a
        # failed stat, not a read, and is exactly what "no open tickets for this
        # session" is supposed to cost.
        payload = original(self, *args, **kwargs)
        opened.append(self.name)
        return payload

    monkeypatch.setattr(Path, "read_text", counting_read_text)
    return opened


def test_the_open_ticket_lookup_does_not_read_every_ticket(
    isolate_agent_runtime_root, monkeypatch
):
    """The per-turn cost must not scale with the store.

    ``open_ticket_for_session`` runs on nearly every mission-chat turn (the
    tokenless settlement rides it). Globbing the ticket directory made each turn
    pay for every question ever asked inside the live TTL window — an O(store)
    read on the hot path of a store that is meant to be write-rare. The
    by-session index answers it in a bounded number of reads: the index file,
    then the one ticket it names."""

    store = PersonaChatClarifyTicketStore()
    for index in range(12):
        _clarify_ticket(store, chat_session_id=f"persona_chat_personainst_dev_{index:012d}")
    wanted = _clarify_ticket(store)

    opened = _files_read(monkeypatch)
    found = store.open_ticket_for_session(_CLARIFY_ROOT)

    assert found["clarify_token"] == wanted
    # One index file + one ticket file. Thirteen tickets exist; twelve of them
    # were never touched.
    assert len(opened) == 2, opened


def test_a_session_with_no_ticket_costs_no_reads(isolate_agent_runtime_root, monkeypatch):
    # The common shape by far: a turn in a session that never asked anything.
    # "No index file for this session" has to MEAN "no open tickets" — that is
    # what the marker buys — or the fallback scan would run on every turn and
    # nothing would have been fixed.
    store = PersonaChatClarifyTicketStore()
    _clarify_ticket(store)

    opened = _files_read(monkeypatch)
    assert store.open_ticket_for_session("persona_chat_personainst_qa_ffffffffffff") is None
    assert opened == []


def test_a_pre_existing_ticket_store_is_indexed_on_first_use(isolate_agent_runtime_root):
    """A store written before the index existed must still be found.

    The rebuild is the migration: it reads the tickets themselves, so a
    directory that predates this code (or a crash that lost the index) converges
    on the first call rather than needing a repair pass."""

    store = PersonaChatClarifyTicketStore()
    token = _clarify_ticket(store)
    for path in store._index_dir().glob("*.json"):
        path.unlink()

    assert store.open_ticket_for_session(_CLARIFY_ROOT)["clarify_token"] == token
    assert store._index_state_path().exists()


def test_a_stale_index_entry_degrades_to_a_miss_never_a_wrong_binding(
    isolate_agent_runtime_root,
):
    """The index is a cache of POINTERS, never a second source of truth.

    Every token it hands back is re-read from its own ticket file and verified
    against that record's own state and session before it can bind anything. So
    a hand-edited, half-written, or simply outdated entry can only ever cost a
    wasted read — it can never bind a turn onto a thread the ticket does not
    name."""

    store = PersonaChatClarifyTicketStore()
    live = _clarify_ticket(store)
    foreign = _clarify_ticket(
        store, chat_session_id="persona_chat_personainst_qa_bbbbbbbbbbbb"
    )

    # Forge the index: a token for ANOTHER session, plus one that resolves to
    # nothing, both ranked newer than the real ticket.
    now = time.time()
    store._write_index(
        _CLARIFY_ROOT,
        [
            {"clarify_token": "clarify-deadbeefdead", "created_at": now + 20},
            {"clarify_token": foreign, "created_at": now + 10},
            {"clarify_token": live, "created_at": now},
        ],
    )

    found = store.open_ticket_for_session(_CLARIFY_ROOT)

    assert found["clarify_token"] == live
    # …and the two entries that failed verification are gone, so the index
    # converges without a repair pass.
    assert [entry["clarify_token"] for entry in store._index_entries(_CLARIFY_ROOT)] == [live]


def test_settling_a_ticket_takes_it_out_of_the_index(isolate_agent_runtime_root):
    store = PersonaChatClarifyTicketStore()
    older = _clarify_ticket(store)
    newer = _clarify_ticket(store)

    store.settle(newer, client_message_id="cm-answer", bound_via="session_id")

    assert [entry["clarify_token"] for entry in store._index_entries(_CLARIFY_ROOT)] == [older]
    store.settle(older, client_message_id="cm-answer-2", bound_via="session_id")
    assert store._index_entries(_CLARIFY_ROOT) == []
    assert store.open_ticket_for_session(_CLARIFY_ROOT) is None


def _age_clarify_store(store, token: str, seconds: float) -> None:
    """Make the store look like it really sat for *seconds*.

    Both the ticket's own ``created_at`` and the pointer the index recorded
    for it, because in production those two ARE the same number — the index
    copies it off the record at mint and off the record again at rebuild.
    Aging only one of them would be testing a store shape the runtime cannot
    produce."""

    path = store._path(token)
    record = json.loads(path.read_text(encoding="utf-8"))
    aged = time.time() - seconds
    record["created_at"] = aged
    path.write_text(json.dumps(record), encoding="utf-8")
    root = str(record["chat_session_id"])
    store._write_index(
        root,
        [
            {**entry, "created_at": aged if entry["clarify_token"] == token else entry["created_at"]}
            for entry in store._index_entries(root)
        ],
    )


def test_the_sweep_reclaims_index_files_it_can_prove_are_dead(isolate_agent_runtime_root):
    """By the file's OWN record of the newest ticket it names.

    If every token an index file names is expired, the ticket loop already
    unlinked all of them, so the file can only name the dead — that is the
    whole safety argument, and it now rests on the file's content instead of
    on the filesystem's clock."""

    store = PersonaChatClarifyTicketStore()
    token = _clarify_ticket(store)
    _age_clarify_store(store, token, CLARIFY_TICKET_TTL_SECONDS * 2)
    index_path = store._index_path(_CLARIFY_ROOT)

    assert store.sweep() == 1
    assert not index_path.exists()

    # A live pointer survives the same sweep.
    fresh = _clarify_ticket(store)
    assert store.sweep() == 0
    assert store.open_ticket_for_session(_CLARIFY_ROOT)["clarify_token"] == fresh


def test_the_index_file_records_the_newest_ticket_it_names(isolate_agent_runtime_root):
    """The recorded fact the sweep reads, written by BOTH writers.

    ``_write_index`` (the add/drop path) and ``_rebuild_index`` (the migration
    and crash-recovery path) used to hand-build the same dict in two places.
    One of them gaining a field the other did not is precisely how the sweep
    would end up reading ``None`` off half the store."""

    store = PersonaChatClarifyTicketStore()
    older = _clarify_ticket(store)
    newer = _clarify_ticket(store)
    index_path = store._index_path(_CLARIFY_ROOT)

    written = json.loads(index_path.read_text(encoding="utf-8"))
    entries = {entry["clarify_token"]: entry["created_at"] for entry in written["open_tokens"]}
    assert written["newest_created_at"] == max(entries.values())
    assert written["newest_created_at"] == entries[newer] > entries[older]

    # …and the rebuild agrees, field for field, because there is one writer of
    # the shape now.
    store._invalidate_index()
    assert store._rebuild_index() is True
    rebuilt = json.loads(index_path.read_text(encoding="utf-8"))
    assert rebuilt["newest_created_at"] == written["newest_created_at"]
    assert rebuilt["schema_version"] == written["schema_version"]


def test_a_fresh_mtime_cannot_keep_a_dead_index_file_alive(isolate_agent_runtime_root):
    """The benign direction of the mtime lie, and it must still be retired.

    A restore, a sync, or an ordinary file copy rewrites mtime without
    touching a byte of content. Under the old rule that alone re-dated an
    index file naming nothing but expired tickets, and the sweep would never
    reclaim it again — the file survives every future sweep because each one
    re-reads the same fresh mtime."""

    import os

    store = PersonaChatClarifyTicketStore()
    token = _clarify_ticket(store)
    _age_clarify_store(store, token, CLARIFY_TICKET_TTL_SECONDS * 2)
    index_path = store._index_path(_CLARIFY_ROOT)
    os.utime(index_path, None)  # the copy tool's fingerprint: mtime = now

    assert store.sweep() == 1
    assert not index_path.exists(), "the sweep believed an mtime over the file's own record"


def test_a_stale_mtime_cannot_delete_a_live_index_file(isolate_agent_runtime_root):
    """The direction that is NOT survivable, which is why this changed.

    A copy that preserves source mtimes — or any restore of an archive — can
    land an ancient mtime on an index file whose content is entirely live.
    Deleting it does not merely cost a read: the marker goes on swearing the
    index is complete, so the tokenless settlement never looks for the ticket
    again and nothing rebuilds. That is the silent permanent miss this store
    already fixed once, arriving through the filesystem instead of through a
    swallowed write."""

    import os

    store = PersonaChatClarifyTicketStore()
    token = _clarify_ticket(store)
    index_path = store._index_path(_CLARIFY_ROOT)
    aged = time.time() - (CLARIFY_TICKET_TTL_SECONDS * 2)
    os.utime(index_path, (aged, aged))

    assert store.sweep() == 0
    assert index_path.exists()
    # …and the pointer still answers, which is the thing that was at stake.
    assert store.open_ticket_for_session(_CLARIFY_ROOT)["clarify_token"] == token


def test_an_index_file_that_predates_the_recorded_field_still_sweeps(
    isolate_agent_runtime_root,
):
    """The migration is free: no rebuild owed, no repair pass.

    A ``schema_version`` 1 file has no ``newest_created_at``, but the same
    fact is derivable from the entries it names — still the file's own
    content. Treating its absence as "unknown, keep forever" would leak every
    index file written before this code for the life of the store."""

    store = PersonaChatClarifyTicketStore()
    token = _clarify_ticket(store)
    _age_clarify_store(store, token, CLARIFY_TICKET_TTL_SECONDS * 2)
    index_path = store._index_path(_CLARIFY_ROOT)

    legacy = json.loads(index_path.read_text(encoding="utf-8"))
    legacy.pop("newest_created_at")
    legacy["schema_version"] = 1
    index_path.write_text(json.dumps(legacy), encoding="utf-8")

    assert store.sweep() == 1
    assert not index_path.exists()


def test_an_unreadable_index_file_is_never_swept(isolate_agent_runtime_root):
    """UNREADABLE IS NOT DEAD, and the sweep only ever deletes on proof.

    The failure this guards is not corruption, it is the ordinary Windows
    case the add path already documents: an AV or indexer holding the file for
    the instant the sweep reads it. Answering "could not read" with "therefore
    expired" would delete a fully live index file — pointers and all — over a
    transient lock. Keeping an inert file costs one small file; deleting a
    live one costs a ticket nothing will ever look for again."""

    import os

    store = PersonaChatClarifyTicketStore()
    _clarify_ticket(store)
    index_path = store._index_path(_CLARIFY_ROOT)
    index_path.write_text("{not json", encoding="utf-8")
    aged = time.time() - (CLARIFY_TICKET_TTL_SECONDS * 2)
    os.utime(index_path, (aged, aged))

    store.sweep()

    assert index_path.exists()
    assert store._index_newest_created_at(index_path) is None


def test_the_ticket_loop_and_the_index_sweep_share_one_cutoff(
    isolate_agent_runtime_root,
):
    """One cutoff authority, not two constants that agree by coincidence.

    The index sweep is only safe because "this file names nothing the ticket
    loop did not just unlink" is TRUE, and that holds only while both sides
    ask the same question of the same number. Pinned at the boundary: one TTL
    either side of the ticket's age must move BOTH, together."""

    store = PersonaChatClarifyTicketStore()
    token = _clarify_ticket(store)
    _age_clarify_store(store, token, 1_000.0)
    index_path = store._index_path(_CLARIFY_ROOT)

    # A TTL longer than the ticket's age: neither is expired.
    assert store.sweep(ttl_seconds=5_000.0) == 0
    assert index_path.exists()
    assert store.resolve(token) is not None

    # A TTL shorter than it: both go, on the same pass, from the same cutoff.
    assert store.sweep(ttl_seconds=100.0) == 1
    assert not index_path.exists()
    assert store.resolve(token) is None


def test_a_pointer_that_could_not_be_written_retracts_the_marker(
    isolate_agent_runtime_root, monkeypatch
):
    """A lost ADD may not leave the index still claiming to be complete.

    The marker is what lets "no index file for this session" be read as "no open
    tickets". If the pointer write fails — a full store, or the ordinary Windows
    case of an AV or indexer holding the replace target — the ticket is on disk
    and OPEN while the marker goes on swearing the index knows about it. Nothing
    rebuilds, so the tokenless settlement never finds it again: a silent,
    permanent miss, landing on exactly the number the adoption readout exists to
    report. Taking the claim back costs one rebuild and heals it."""

    import agent_runtime.persona_chat_continuity as continuity

    store = PersonaChatClarifyTicketStore()
    _clarify_ticket(store)  # establishes the index + marker
    index_dir = store._index_dir()
    real_atomic = continuity._atomic_json

    def failing_pointer_write(path, value):
        if path.parent == index_dir and path.name != "_index_state.json":
            raise OSError(32, "the process cannot access the file")
        return real_atomic(path, value)

    monkeypatch.setattr(continuity, "_atomic_json", failing_pointer_write)
    lost = _clarify_ticket(store)
    monkeypatch.setattr(continuity, "_atomic_json", real_atomic)

    # The ticket itself was never in doubt — only the pointer to it.
    assert store.resolve(lost)["state"] == "open"
    # The completeness claim is gone, so the next lookup rebuilds instead of
    # answering "no open tickets" from an index that silently lost one…
    assert not store._index_state_path().exists()
    assert store.open_ticket_for_session(_CLARIFY_ROOT)["clarify_token"] == lost
    # …and having healed once, it is back on the O(1) path.
    assert store._index_state_path().exists()
    assert store._index_entries(_CLARIFY_ROOT)[0]["clarify_token"] == lost


def test_a_pointer_read_that_failed_cannot_erase_the_pointers_it_missed(
    isolate_agent_runtime_root, monkeypatch
):
    """The third seam of one bug: unreadable answered as empty, then written back.

    ``_index_add`` is a read-modify-WRITE, so a read that returned "nothing
    recorded" for a file that is merely unreadable hands it a blank slate and
    the whole list is replaced by the one new pointer. The failure is the same
    ordinary Windows one the sweep now refuses to treat as death and the add
    path already documents for its write — an AV or indexer holding the file
    for the instant it is read. The cost is not symmetric with a lost read:
    the marker goes on swearing the index is complete, so the tokenless
    settlement never looks for the erased tickets again and nothing rebuilds.

    Retracting the claim is the same answer a swallowed write already gets,
    and it heals the same way: one rebuild re-derives every pointer from the
    ticket files, which are the authority the index only ever cached."""

    import pathlib

    store = PersonaChatClarifyTicketStore()
    first = _clarify_ticket(store)
    second = _clarify_ticket(store)
    index_path = store._index_path(_CLARIFY_ROOT)
    real_read = pathlib.Path.read_text

    def locked_index(self, *args, **kwargs):
        if self == index_path:
            raise PermissionError(13, "the process cannot access the file")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", locked_index)
    third = _clarify_ticket(store)
    monkeypatch.setattr(pathlib.Path, "read_text", real_read)

    # Unreadable is not empty: the earlier pointers were not overwritten with
    # a list built from a read that never happened.
    assert not store._index_state_path().exists(), (
        "an unreadable index was treated as an empty one and written back"
    )
    for token in (first, second, third):
        assert store.resolve(token)["state"] == "open"

    # The retracted claim heals on the next lookup, and every ticket is back —
    # including the two the blank-slate write would have erased for good.
    assert store.open_ticket_for_session(_CLARIFY_ROOT)["clarify_token"] == third
    recorded = {entry["clarify_token"] for entry in store._index_entries(_CLARIFY_ROOT)}
    assert recorded == {first, second, third}

    # …and settling the newest still finds the next one down, which is the
    # observable the erasure destroyed.
    store.settle(third)
    assert store.open_ticket_for_session(_CLARIFY_ROOT)["clarify_token"] == second


def test_a_rebuild_cannot_drop_a_pointer_minted_while_it_scanned(
    isolate_agent_runtime_root,
):
    """The rebuild's scan is a snapshot; its write must not act like a truth.

    A rebuild reads every ticket, then writes each session's pointers. A mint
    landing between those two steps is invisible to the scan, and a write that
    REPLACED the file would erase the pointer that mint had already recorded —
    permanently, because the marker the rebuild writes last stops any later
    rebuild from recovering it. The damage is not merely a miss: the lookup goes
    on answering with an OLDER open ticket, so the tokenless settlement closes a
    question that was already superseded — something the pre-index glob never
    did. Unioning is safe precisely because the index is a cache: an entry the
    ticket files do not back is verified away on the read path."""

    store = PersonaChatClarifyTicketStore()
    earlier = _clarify_ticket(store)
    # A store with no marker: first use after this code arrives, or any crash
    # that left a rebuild unfinished.
    store._invalidate_index()

    concurrent: list[str] = []
    fired: list[bool] = []
    # ML-15 moved the rebuild's read to ``scan_records`` — the chokepoint that
    # carries the unreadable count beside the rows — so the interception point
    # moves with it. Patching ``_iter_records`` here would no longer be reached
    # by the rebuild, and this test would pass while exercising nothing.
    real_scan = PersonaChatClarifyTicketStore.scan_records

    def scan_then_let_a_mint_land(self):
        records, unreadable = real_scan(self)
        if not fired:
            # Guard set BEFORE the nested mint: that mint scans too (sweep, and
            # its own rebuild), and re-entering here would recurse forever.
            fired.append(True)
            # The concurrent turn: it mints, and records its own pointer, while
            # the rebuild above is holding a scan that predates it.
            concurrent.append(_clarify_ticket(PersonaChatClarifyTicketStore()))
        return records, unreadable

    PersonaChatClarifyTicketStore.scan_records = scan_then_let_a_mint_land
    try:
        assert PersonaChatClarifyTicketStore()._rebuild_index() is True
    finally:
        PersonaChatClarifyTicketStore.scan_records = real_scan

    minted = concurrent[0]
    tokens = [entry["clarify_token"] for entry in store._index_entries(_CLARIFY_ROOT)]
    assert minted in tokens, "the rebuild replaced a pointer it never scanned"
    assert earlier in tokens
    # …and the lookup answers with the NEWEST open ticket, which is what the
    # pre-index glob would have returned.
    assert store.open_ticket_for_session(_CLARIFY_ROOT)["clarify_token"] == minted


# ── a mint for a target that can never be served writes nothing ─────────────


def _retired_placement(store: PersonaInstanceStore) -> tuple[PersonaInstance, dict]:
    placement = store.add_instance(
        persona_id="dev", placement_id="dev_agent_2", display_name="Dev (2)"
    )
    return placement, store.retire(placement.id, reason="placement deleted")


def test_mint_refuses_a_retired_target_before_its_first_durable_write(
    isolate_agent_runtime_root, tmp_path
):
    """The litter bug at its source.

    ``mint`` bound the instance LAST — reserve receipt, create session, write
    meta, write title, then ``open_chat``. For a retired placement that final
    bind is a refusal, so the lane always ran to completion first and left a
    titled thread in Mission Control for a dispatch that could never be served.
    The refusal is decidable from the store before any of it, so it is."""

    from agent_runtime import paths
    from agent_runtime.persona_assignments import RetiredPersonaInstanceError
    from agent_runtime.persona_chat_history import PERSONA_CHAT_SESSION_SOURCE

    db = SessionDB(tmp_path / "state.db")
    store = PersonaInstanceStore()
    placement, archived = _retired_placement(store)

    with pytest.raises(RetiredPersonaInstanceError) as excinfo:
        PersonaChatMintReceiptStore().mint(
            instance_store=store,
            session_db=db,
            persona_id="dev",
            persona_instance_id=placement.id,
            idempotency_key="dispatch-to-a-retired-placement",
            title="triage the flaky login test",
        )

    assert excinfo.value.code == "retired_persona_instance"
    assert excinfo.value.persona_instance_id == placement.id
    assert str(excinfo.value.archive_path) == archived["archive_path"]
    # Nothing durable survives the refusal: no session row, and not even the
    # receipt directory the reserve step would have created.
    assert db.list_sessions_rich(source=PERSONA_CHAT_SESSION_SOURCE, limit=50) == []
    assert not (paths.store_root() / "persona_chat_mint_receipts").exists()
    # And the tombstone is still a tombstone — the refusal never revives the row.
    assert placement.id not in {row.id for row in store.list_all()}


def test_mint_for_a_live_target_is_untouched_by_the_retirement_precondition(
    isolate_agent_runtime_root, tmp_path
):
    """The precondition must not answer "retired" for a first-ever instance.

    A brand-new instance has no live row either — retirement is that absence
    PLUS a tombstone — so a predicate that keyed on absence alone would refuse
    every first mint in the product."""

    db = SessionDB(tmp_path / "state.db")
    store = PersonaInstanceStore()
    _retired_placement(store)  # a tombstone exists, for a DIFFERENT id

    receipt = PersonaChatMintReceiptStore().mint(
        instance_store=store,
        session_db=db,
        persona_id="dev",
        persona_instance_id="personainst_dev",
        idempotency_key="first-ever",
        title="Dev chat",
    )

    root = receipt["root_chat_session_id"]
    assert receipt["state"] == "completed"
    assert db.get_session(root) is not None
    assert store.get("personainst_dev").default_chat_session_id == root


def test_a_flaky_tombstone_probe_cannot_escape_the_mint_lane_untyped(
    isolate_agent_runtime_root, tmp_path, monkeypatch, caplog
):
    """The untyped escape this fix exists to kill, one layer down.

    The precondition's answer comes from filesystem I/O — ``exists`` /
    ``iterdir`` / ``is_file`` over an archive root that in production can be a
    UNC share. This call site handles exactly ONE typed error, so an ``OSError``
    from a flaky root would sail straight past it and reach the operator as the
    traceback the whole fix was about, just with a different exception class.

    The predicate owns the posture so both call sites inherit it: a probe that
    cannot READ the archive cannot PROVE retirement, so it answers "not
    retired" — the pre-flight's existing fail-open — and warns rather than
    failing silently. ``open_chat`` is still the write chokepoint that refuses,
    so the cost of failing open is the litter, never the guarantee.
    """

    import logging

    from agent_runtime import persona_assignments

    db = SessionDB(tmp_path / "state.db")
    store = PersonaInstanceStore()

    def _flaky(_instance_id):
        raise OSError("the store root went away mid-probe")

    monkeypatch.setattr(
        persona_assignments, "_retired_persona_instance_archive_path", _flaky
    )

    with caplog.at_level(logging.WARNING, logger=persona_assignments.__name__):
        assert store.retired_instance_archive_path("personainst_dev") is None
    assert any(
        "retirement tombstone probe failed" in record.getMessage()
        for record in caplog.records
    ), "failing open must not be silent"

    # …and the lane that used to traceback now runs to completion.
    receipt = PersonaChatMintReceiptStore().mint(
        instance_store=store,
        session_db=db,
        persona_id="dev",
        persona_instance_id="personainst_dev",
        idempotency_key="flaky-probe",
        title="Dev chat",
    )

    assert db.get_session(receipt["root_chat_session_id"]) is not None


def test_a_retire_that_lands_mid_mint_still_leaves_no_titled_thread(
    isolate_agent_runtime_root, tmp_path
):
    """The race the entry precondition could not close, forced open.

    ``retire`` refuses only on a live run/worker binding or an active
    assignment, and a chat mint holds neither until it binds — so a retirement
    landing AFTER the precondition passed used to let the lane reserve, create,
    write meta and TITLE the session before the bind refused, leaving a titled
    thread in Mission Control for a placement that no longer existed. Binding
    before the first session-visible write is what closes it, and this pins the
    ordering rather than the symptom: a bind moved back to the end of the lane
    fails here."""

    from agent_runtime.persona_assignments import RetiredPersonaInstanceError
    from agent_runtime.persona_chat_history import PERSONA_CHAT_SESSION_SOURCE

    class _RetiringMidLaneStore(PersonaInstanceStore):
        """Retires the target the instant the mint's precondition passes.

        Armed only after setup: ``add_instance`` binds through ``open_chat``,
        which asks this same seam."""

        armed = False

        def assert_bindable(self, **kwargs):
            resolved = super().assert_bindable(**kwargs)
            if self.armed:
                self.armed = False
                super().retire(resolved, reason="placement deleted mid-dispatch")
            return resolved

    db = SessionDB(tmp_path / "state.db")
    store = _RetiringMidLaneStore()
    placement = store.add_instance(
        persona_id="dev", placement_id="dev_agent_2", display_name="Dev (2)"
    )
    store.armed = True

    with pytest.raises(RetiredPersonaInstanceError):
        PersonaChatMintReceiptStore().mint(
            instance_store=store,
            session_db=db,
            persona_id="dev",
            persona_instance_id=placement.id,
            idempotency_key="retired-while-minting",
            title="triage the flaky login test",
        )

    assert db.list_sessions_rich(source=PERSONA_CHAT_SESSION_SOURCE, limit=50) == []
    assert placement.id not in {row.id for row in PersonaInstanceStore().list_all()}


def test_the_mint_binds_before_it_creates_the_session_row(
    isolate_agent_runtime_root, tmp_path
):
    # The same ordering, stated positively so the invariant is readable without
    # a fault: nothing a reader would see in Mission Control is written until
    # the target has been proven bindable BY BINDING.
    order: list[str] = []

    class _OrderedStore(PersonaInstanceStore):
        def open_chat(self, **kwargs):
            order.append("bind")
            return super().open_chat(**kwargs)

    class _OrderedDB(SessionDB):
        def create_session(self, *args, **kwargs):
            order.append("create_session")
            return super().create_session(*args, **kwargs)

        def set_session_title(self, *args, **kwargs):
            order.append("title")
            return super().set_session_title(*args, **kwargs)

    PersonaChatMintReceiptStore().mint(
        instance_store=_OrderedStore(),
        session_db=_OrderedDB(tmp_path / "state.db"),
        persona_id="dev",
        persona_instance_id="personainst_dev",
        idempotency_key="ordering",
        title="Dev chat",
    )

    assert order == ["bind", "create_session", "title"]


def test_the_mint_refusal_names_its_target_the_way_every_other_refusal_does(
    isolate_agent_runtime_root, tmp_path
):
    """One refusal, three enforcement points, ONE spelling of the target id.

    ``mint`` reported the caller's raw token while the dispatch pre-flight and
    ``open_chat`` both report the id the canonical derivation resolved — so a
    caller that spelled the target with a drifted actor token got the same
    refusal under two different names depending on which point happened to
    fire. ``persona_instance_id`` is the field an operator (and the archived
    history it points at) is keyed off; it cannot depend on which guard won.
    """

    from agent_runtime.persona_assignments import RetiredPersonaInstanceError

    db = SessionDB(tmp_path / "state.db")
    store = PersonaInstanceStore()
    placement, archived = _retired_placement(store)

    with pytest.raises(RetiredPersonaInstanceError) as excinfo:
        PersonaChatMintReceiptStore().mint(
            instance_store=store,
            session_db=db,
            persona_id="dev",
            persona_instance_id=f"persona_{placement.id}",
            idempotency_key="drifted-actor-token",
            title="triage the flaky login test",
        )

    assert excinfo.value.persona_instance_id == placement.id
    assert str(excinfo.value.archive_path) == archived["archive_path"]


# ---------------------------------------------------------------------------
# The OTHER end of the early bind: the window it opens, and the retraction that
# closes it. ``open_chat`` runs before ``create_session`` on purpose (above), so
# for the instant between them the instance points at a root no transcript store
# holds — the phantom ``persona_chat_durability`` closed on the CREATE lane by
# persisting at the bind argument. This lane cannot borrow that fix: its
# ``session_db`` is the CALLER's handle and the durability helper acquires the
# process DEFAULT store, so it would write the row into a different database
# from the one this mint's reader dereferences. The bind is retracted instead.
# ---------------------------------------------------------------------------


def test_a_transcript_write_that_fails_retracts_the_bind_it_was_ordered_behind(
    isolate_agent_runtime_root, tmp_path
):
    """A failed ``create_session`` must leave NO bound phantom pointer.

    Before this, the mint bound first and wrote the row second with nothing in
    between: a ``create_session`` that raised left ``default_chat_session_id``
    naming a root that exists in the instance row and in no SessionDB. That
    pointer never heals — ``resolve_default_chat_session_id_for_instance``
    re-offers a chat-shaped own-instance pointer forever without ever asking
    whether it resolves — so every later ``mission-chat message`` to that agent
    is refused ``unknown_chat_session``.
    """

    import sqlite3

    from agent_runtime.persona_assignments import (
        resolve_default_chat_session_id_for_instance,
    )
    from agent_runtime.persona_chat_durability import PersonaChatPersistenceError

    bound: list[str] = []
    attempted: list[str] = []

    class _BindRecordingStore(PersonaInstanceStore):
        def open_chat(self, **kwargs):
            bound.append(kwargs["session_id"])
            return super().open_chat(**kwargs)

    class _RefusingDB(SessionDB):
        def create_session(self, *args, **kwargs):
            attempted.append(kwargs.get("session_id"))
            raise sqlite3.OperationalError("disk I/O error")

    store = _BindRecordingStore()
    db = _RefusingDB(tmp_path / "state.db")

    with pytest.raises(PersonaChatPersistenceError) as excinfo:
        PersonaChatMintReceiptStore().mint(
            instance_store=store,
            session_db=db,
            persona_id="dev",
            persona_instance_id="personainst_dev",
            idempotency_key="doomed",
            title="Dev chat",
        )

    # TYPED, and chained — not a raw storage exception escaping into the
    # dispatch lane, which is what an operator would otherwise be handed.
    assert excinfo.value.operation == "session_create"
    assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)
    # IDENTITY, not mere existence: the row the lane tried to make durable is
    # the very root it bound. A persistence step that "succeeded" for some other
    # id would leave the bound pointer exactly as phantom as no write at all.
    assert len(bound) == 1
    assert attempted == bound

    live = PersonaInstanceStore().get("personainst_dev")
    pointer = live.default_chat_session_id
    assert pointer is None, f"the failed mint left a phantom pointer: {pointer!r}"
    # The legacy mirror moves WITH the authority:
    # ``PersonaInstance.__post_init__`` re-derives ``default_chat_session_id``
    # from a ``persona_chat_*`` ``session_id``, so a half-retraction would
    # resurrect the phantom on the next read.
    assert live.session_id is None
    # And the read verbs answer the honest "no thread yet", so the next mint
    # makes a fresh, durable root instead of re-offering the dead one.
    assert (
        resolve_default_chat_session_id_for_instance(
            PersonaInstanceStore(), persona_id="dev"
        )
        is None
    )


def test_a_retraction_restores_the_pointer_the_instance_already_had(
    isolate_agent_runtime_root, tmp_path
):
    """Retraction restores; it does not merely clear.

    An instance that already had a working thread must keep it when a LATER
    mint's transcript write fails. Clearing unconditionally would trade a
    phantom for a silently orphaned conversation."""

    from agent_runtime.persona_chat_durability import PersonaChatPersistenceError

    class _SecondWriteFailsDB(SessionDB):
        fail = False

        def create_session(self, *args, **kwargs):
            if self.fail:
                raise RuntimeError("transcript store went away")
            return super().create_session(*args, **kwargs)

    db = _SecondWriteFailsDB(tmp_path / "state.db")
    store = PersonaInstanceStore()
    receipts = PersonaChatMintReceiptStore()

    first = receipts.mint(
        instance_store=store,
        session_db=db,
        persona_id="dev",
        persona_instance_id="personainst_dev",
        idempotency_key="first",
        title="Dev chat",
    )
    good_root = first["root_chat_session_id"]
    assert store.get("personainst_dev").default_chat_session_id == good_root

    db.fail = True
    with pytest.raises(PersonaChatPersistenceError):
        receipts.mint(
            instance_store=store,
            session_db=db,
            persona_id="dev",
            persona_instance_id="personainst_dev",
            idempotency_key="second",
            title="Dev chat",
        )

    pointer = PersonaInstanceStore().get("personainst_dev").default_chat_session_id
    assert pointer == good_root
    assert db.get_session(pointer) is not None, "the restored pointer must resolve"


def test_a_same_key_retry_after_a_failed_mint_still_resolves_the_same_root(
    isolate_agent_runtime_root, tmp_path
):
    """The receipt property has to survive the retraction.

    The retraction undoes the BIND, never the reservation: the receipt is what
    makes the lane idempotent, and a failure that dropped it would let a retry
    mint a SECOND thread for a dispatch that already has one. It is also the
    only cover for the window no in-process rollback can reach — a crash
    between the bind and the row."""

    from agent_runtime.persona_chat_durability import PersonaChatPersistenceError

    class _FirstWriteFailsDB(SessionDB):
        fail = True

        def create_session(self, *args, **kwargs):
            if self.fail:
                self.fail = False
                raise RuntimeError("transcript store went away")
            return super().create_session(*args, **kwargs)

    db = _FirstWriteFailsDB(tmp_path / "state.db")
    store = PersonaInstanceStore()
    receipts = PersonaChatMintReceiptStore()

    with pytest.raises(PersonaChatPersistenceError):
        receipts.mint(
            instance_store=store,
            session_db=db,
            persona_id="dev",
            persona_instance_id="personainst_dev",
            idempotency_key="retried",
            title="Dev chat",
        )

    reserved = json.loads(
        receipts._path("personainst_dev", "retried").read_text(encoding="utf-8")
    )
    assert reserved["state"] == "reserved"

    replay = receipts.mint(
        instance_store=store,
        session_db=db,
        persona_id="dev",
        persona_instance_id="personainst_dev",
        idempotency_key="retried",
        title="Dev chat",
    )

    assert replay["root_chat_session_id"] == reserved["root_chat_session_id"]
    assert replay["state"] == "completed"
    assert db.get_session(replay["root_chat_session_id"]) is not None
    assert (
        PersonaInstanceStore().get("personainst_dev").default_chat_session_id
        == replay["root_chat_session_id"]
    )


def test_a_retraction_never_reverts_a_pointer_another_lane_moved(
    isolate_agent_runtime_root, tmp_path
):
    """The retraction is keyed on IDENTITY, not on a pointer being present.

    Between our bind and our failure another lane can legitimately rebind this
    instance. Reverting to the value WE saw before our bind would then erase a
    newer, perfectly durable binding — trading this lane's phantom for someone
    else's. The retraction fires only while the live pointer still names OUR
    root."""

    from agent_runtime.persona_assignments import persona_chat_session_id_for
    from agent_runtime.persona_chat_durability import PersonaChatPersistenceError
    from agent_runtime.persona_chat_history import PERSONA_CHAT_SESSION_SOURCE

    other_root = persona_chat_session_id_for("personainst_dev")

    class _HijackingDB(SessionDB):
        hijack = False

        def create_session(self, *args, **kwargs):
            if not self.hijack:
                return super().create_session(*args, **kwargs)
            # The concurrent lane lands INSIDE our window and rebinds the
            # instance to a root of its own — durable, written just below.
            PersonaInstanceStore().open_chat(persona_id="dev", session_id=other_root)
            raise RuntimeError("transcript store went away")

    db = _HijackingDB(tmp_path / "state.db")
    store = PersonaInstanceStore()
    receipts = PersonaChatMintReceiptStore()

    receipts.mint(
        instance_store=store,
        session_db=db,
        persona_id="dev",
        persona_instance_id="personainst_dev",
        idempotency_key="first",
        title="Dev chat",
    )
    db.create_session(
        session_id=other_root,
        source=PERSONA_CHAT_SESSION_SOURCE,
        model=None,
        system_prompt="Mission Control persona chat for dev",
    )
    db.hijack = True

    with pytest.raises(PersonaChatPersistenceError):
        receipts.mint(
            instance_store=store,
            session_db=db,
            persona_id="dev",
            persona_instance_id="personainst_dev",
            idempotency_key="second",
            title="Dev chat",
        )

    pointer = PersonaInstanceStore().get("personainst_dev").default_chat_session_id
    assert pointer == other_root, (
        "the retraction reverted a binding it did not make — it must key on the "
        "root it bound, not on whatever pointer happens to be there"
    )
    assert db.get_session(pointer) is not None
