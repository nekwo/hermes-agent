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
