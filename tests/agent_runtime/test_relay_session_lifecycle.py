"""Agent-to-agent relay session lifecycle (agent_chat_send → mission-chat).

The 2026-07-18 incident: a relay with ``session_id`` omitted minted a FRESH
random ``persona_chat_*`` session on every send (``persona_chat_session_id_for``
appends a uuid; the handler called it per send). So repeated relays never
threaded, and each send left an unpointed session the snapshot projection never
considered — the exchange was invisible in Mission Control, violating the tool's
own contract ("omit to continue the target's default chat session ... the whole
exchange is visible in Mission Control").

The fix routes the omitted-session path through
``default_chat_session_id_for_instance`` — the single resolve-or-mint chokepoint:
continue the CANONICAL persona instance's current chat session, minting a fresh
one only when the target has never chatted. These tests pin threading,
canonical-id use (no phantom mints), and end-to-end visibility in the projection.
"""

from __future__ import annotations

import pytest

from agent_runtime.persona_assignments import (
    PersonaInstanceStore,
    default_chat_session_id_for_instance,
    persona_instance_id_for,
    resolve_default_chat_session_id_for_instance,
)
from agent_runtime.persona_chat_history import (
    PERSONA_CHAT_SESSION_SOURCE,
    persona_chat_history_summary,
)


def test_omitted_session_reuses_targets_existing_default_chat_session(
    isolate_agent_runtime_root,
):
    store = PersonaInstanceStore()
    # The target already has a bound default chat session (e.g. an earlier
    # operator/clarify chat). An omitted-session relay must CONTINUE it.
    existing = store.create_operator_chat(persona_id="qa", display_name="QA")

    first = default_chat_session_id_for_instance(store, persona_id="qa")
    second = default_chat_session_id_for_instance(store, persona_id="qa")

    assert first == existing.session_id
    assert second == existing.session_id  # repeated relays thread into ONE session


def test_omitted_session_mints_canonical_default_when_target_never_chatted(
    isolate_agent_runtime_root,
):
    store = PersonaInstanceStore()

    minted = default_chat_session_id_for_instance(store, persona_id="qa")

    # The minted default embeds the CANONICAL instance id (personainst_qa), not a
    # phantom personainst_qa_<hex>: prefix + single 12-hex session suffix.
    assert minted.startswith(f"persona_chat_{persona_instance_id_for('qa')}_")
    tail = minted[len(f"persona_chat_{persona_instance_id_for('qa')}_") :]
    assert len(tail) == 12 and all(ch in "0123456789abcdef" for ch in tail)
    # No phantom double-hex instance segment.
    assert "personainst_qa_" not in minted[: minted.rfind("_")]


def test_two_relays_thread_after_the_first_creates_the_session(
    isolate_agent_runtime_root,
):
    # Mirrors the handler cycle: resolve → open_chat (binds the pointer) →
    # resolve again. The second relay must land on the SAME session the first
    # created, under the canonical instance id.
    store = PersonaInstanceStore()

    relay1 = default_chat_session_id_for_instance(store, persona_id="qa")
    instance = store.open_chat(persona_id="qa", session_id=relay1)
    assert instance.id == persona_instance_id_for("qa")  # canonical, no phantom mint

    relay2 = default_chat_session_id_for_instance(store, persona_id="qa")
    assert relay2 == relay1
    # The pointer is the canonical instance, updated through the normal write
    # path — no stray personainst_qa_<hex> row was created.
    assert {inst.id for inst in store.list_all()} == {persona_instance_id_for("qa")}
    assert store.get(persona_instance_id_for("qa")).session_id == relay1


def test_instance_shaped_target_canonicalizes_and_does_not_mint_a_variant(
    isolate_agent_runtime_root,
):
    store = PersonaInstanceStore()
    existing = store.create_operator_chat(persona_id="qa", display_name="QA")

    # A caller may hand an instance id (with actor-token drift) instead of a
    # persona id; it must resolve to the SAME canonical instance's session.
    from_instance = default_chat_session_id_for_instance(
        store, persona_id="qa", persona_instance_id="personainst_qa"
    )
    from_drift = default_chat_session_id_for_instance(
        store, persona_id="qa", persona_instance_id="persona_personainst_qa"
    )
    assert from_instance == existing.session_id
    assert from_drift == existing.session_id


def test_task_bound_session_pointer_is_not_absorbed_as_a_chat_lane(
    isolate_agent_runtime_root,
):
    # If the instance's pointer is a non-chat (task/worker) session id, a chat
    # relay must NOT thread its transcript onto it — mint a fresh chat session.
    store = PersonaInstanceStore()
    instance = store.open_chat(persona_id="qa", session_id="persona_chat_seed_000000000000")
    instance.session_id = "worker_session_task_42"  # not a persona_chat_* id
    store.update(instance)

    minted = default_chat_session_id_for_instance(store, persona_id="qa")
    assert minted != "worker_session_task_42"
    assert minted.startswith(f"persona_chat_{persona_instance_id_for('qa')}_")


def test_relay_exchange_is_visible_in_the_snapshot_projection(
    isolate_agent_runtime_root,
):
    # End-to-end proof of the contract: after the handler-equivalent steps
    # (resolve → open_chat → ensure SessionDB row → append the relayed message +
    # reply), the exchange renders as ONE persona-chat history row, and a second
    # relay threads into the SAME row rather than orphaning a new session.
    from hermes_state import SessionDB

    store = PersonaInstanceStore()
    db = SessionDB()

    def _relay(message: str, reply: str) -> str:
        session_id = default_chat_session_id_for_instance(store, persona_id="qa")
        store.open_chat(persona_id="qa", session_id=session_id, display_name="QA")
        db.create_session(
            session_id=session_id,
            source=PERSONA_CHAT_SESSION_SOURCE,
            model=None,
            system_prompt="Mission Control persona chat for qa",
        )
        db.append_message(session_id=session_id, role="user", content=message)
        db.append_message(session_id=session_id, role="assistant", content=reply)
        return session_id

    first_session = _relay("From Neko: hi", "QA here — hi Neko.")
    second_session = _relay("From Neko: follow up", "QA here — ack.")
    assert first_session == second_session  # threaded, not orphaned

    rows = persona_chat_history_summary(persona_instances=store.list_all(), session_db=db)
    qa_rows = [row for row in rows if row["persona_id"] == "qa"]
    assert len(qa_rows) == 1, "the relay exchange must render as one visible chat row"
    row = qa_rows[0]
    assert row["session_id"] == first_session
    assert row["persona_instance_id"] == persona_instance_id_for("qa")
    texts = [message["text"] for message in row["messages"]]
    assert "From Neko: hi" in texts and "QA here — hi Neko." in texts
    assert "From Neko: follow up" in texts and "QA here — ack." in texts


# --------------------------------------------------------------------------- #
# new_session lane (mint= mode) + the read verbs' non-minting resolve          #
# --------------------------------------------------------------------------- #


def test_resolve_default_never_mints_for_a_never_chatted_target(
    isolate_agent_runtime_root,
):
    # The read verbs (agent_chat_threads / agent_chat_open) resolve WITHOUT
    # minting: a never-chatted target reports None, and no instance row is written.
    store = PersonaInstanceStore()
    assert resolve_default_chat_session_id_for_instance(store, persona_id="qa") is None
    assert PersonaInstanceStore().list_all() == [], "resolving must not create a session/row"


def test_mint_mode_forces_a_fresh_canonical_session_even_with_a_default(
    isolate_agent_runtime_root,
):
    # new_session=True routes through the SAME chokepoint with mint=True: force a
    # fresh canonical session even when a default already exists — no parallel
    # pipeline, just skip the reuse read.
    store = PersonaInstanceStore()
    existing = store.create_operator_chat(persona_id="qa", display_name="QA")

    assert default_chat_session_id_for_instance(store, persona_id="qa") == existing.session_id
    fresh = default_chat_session_id_for_instance(store, persona_id="qa", mint=True)
    assert fresh != existing.session_id
    assert fresh.startswith(f"persona_chat_{persona_instance_id_for('qa')}_")
    tail = fresh[len(f"persona_chat_{persona_instance_id_for('qa')}_"):]
    assert len(tail) == 12 and all(ch in "0123456789abcdef" for ch in tail)


def test_new_session_mint_is_canonical_and_visible_in_the_projection(
    isolate_agent_runtime_root,
):
    # End-to-end: minting a fresh session (new_session lane) and running the
    # handler-equivalent steps repoints the canonical instance and renders as a
    # visible chat row under the canonical instance id — no orphaned session.
    from hermes_state import SessionDB

    store = PersonaInstanceStore()
    db = SessionDB()

    def _relay(message: str, reply: str, *, mint: bool) -> str:
        session_id = default_chat_session_id_for_instance(store, persona_id="qa", mint=mint)
        store.open_chat(persona_id="qa", session_id=session_id, display_name="QA")
        db.create_session(
            session_id=session_id,
            source=PERSONA_CHAT_SESSION_SOURCE,
            model=None,
            system_prompt="Mission Control persona chat for qa",
        )
        db.append_message(session_id=session_id, role="user", content=message)
        db.append_message(session_id=session_id, role="assistant", content=reply)
        return session_id

    first = _relay("thread one", "ack one", mint=False)
    fresh = _relay("thread two — clean", "ack two", mint=True)
    assert fresh != first, "new_session must start a distinct thread"
    # The fresh session became the instance's default going forward.
    assert resolve_default_chat_session_id_for_instance(store, persona_id="qa") == fresh
    assert default_chat_session_id_for_instance(store, persona_id="qa") == fresh

    rows = persona_chat_history_summary(persona_instances=store.list_all(), session_db=db)
    fresh_rows = [row for row in rows if row["session_id"] == fresh]
    assert len(fresh_rows) == 1, "the fresh thread must render as a visible chat row"
    assert fresh_rows[0]["persona_instance_id"] == persona_instance_id_for("qa")
