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

from pathlib import Path

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
from agent_runtime.profile_context import PersonaProfileBinding, persona_profile_context


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


# --------------------------------------------------------------------------- #
# Sibling-instance chat-session ownership (2026-07-18 channel-fold incident)   #
#                                                                             #
# Two live instances of one persona (canonical personainst_qa + placement      #
# personainst_qa_agent_2) folded onto ONE operator channel: the console's      #
# open-chat of the sibling bound its session onto the canonical primary's      #
# pointer, then a bare-persona relay ADOPTED that poisoned pointer, so both     #
# instances shared a session. A chat session encodes its owning instance       #
# (persona_chat_<instance>_<hex>); ownership is enforced at both chokepoints.   #
# --------------------------------------------------------------------------- #


def test_bare_persona_relay_never_adopts_a_sibling_session(
    isolate_agent_runtime_root,
):
    # THE live repro, as a fixture: a handle-targeted relay to the sibling, then
    # a bare-persona relay to the canonical primary. They must resolve to TWO
    # sessions on TWO pointers — no cross-adoption.
    store = PersonaInstanceStore()
    sibling = store.add_instance(
        persona_id="qa", placement_id="qa_agent_2", display_name="QA Agent (2)"
    )
    sibling_session = sibling.session_id

    # SEND #1 — handle-targeted at the sibling instance.
    s1 = default_chat_session_id_for_instance(
        store, persona_id="qa", persona_instance_id="personainst_qa_agent_2"
    )
    store.open_chat(persona_id="qa", persona_instance_id="personainst_qa_agent_2", session_id=s1)
    assert s1 == sibling_session

    # SEND #2 — bare persona id → the canonical primary. It must NOT adopt the
    # sibling's session; it mints the primary's own.
    s2 = default_chat_session_id_for_instance(store, persona_id="qa")
    store.open_chat(persona_id="qa", session_id=s2)

    assert s2 != sibling_session, "bare-persona send stole the sibling's session"
    primary = store.get(persona_instance_id_for("qa"))
    assert primary.session_id == s2
    assert primary.session_id != store.get("personainst_qa_agent_2").session_id
    assert store.get("personainst_qa_agent_2").session_id == sibling_session


def test_open_chat_refuses_binding_a_siblings_session_onto_the_canonical(
    isolate_agent_runtime_root,
):
    # The write chokepoint every send/open flows through must refuse to bind a
    # session minted for ANOTHER instance onto this one — the exact poison write.
    store = PersonaInstanceStore()
    sibling = store.add_instance(
        persona_id="qa", placement_id="qa_agent_2", display_name="QA Agent (2)"
    )
    with pytest.raises(ValueError, match="belongs to instance"):
        store.open_chat(persona_id="qa", session_id=sibling.session_id)
    # The canonical primary's pointer was never written.
    assert resolve_default_chat_session_id_for_instance(store, persona_id="qa") is None


def test_resolve_default_ignores_a_foreign_pointer_and_self_heals(
    isolate_agent_runtime_root,
):
    # A pointer already poisoned with a sibling's session (pre-fix corruption)
    # must be ignored, and the next send mints the instance's OWN session —
    # self-healing the corrupted pointer with no manual store repair.
    store = PersonaInstanceStore()
    sibling = store.add_instance(
        persona_id="qa", placement_id="qa_agent_2", display_name="QA Agent (2)"
    )
    # Poison the canonical pointer directly (bypassing the write-guard) to model
    # the on-disk corrupted state.
    primary = store.open_chat(persona_id="qa", session_id=default_chat_session_id_for_instance(store, persona_id="qa"))
    primary.session_id = sibling.session_id
    store.update(primary)
    assert store.get(persona_instance_id_for("qa")).session_id == sibling.session_id

    # Read side ignores the foreign session; send mints the primary's own.
    assert resolve_default_chat_session_id_for_instance(store, persona_id="qa") is None
    healed = default_chat_session_id_for_instance(store, persona_id="qa")
    assert healed.startswith(f"persona_chat_{persona_instance_id_for('qa')}_")
    store.open_chat(persona_id="qa", session_id=healed)
    assert store.get(persona_instance_id_for("qa")).session_id == healed
    assert store.get("personainst_qa_agent_2").session_id == sibling.session_id


# --------------------------------------------------------------------------- #
# Relay-target transcript persists to the HEAD home (2026-07-18 incident A)     #
#                                                                             #
# An in-process ``agent_chat_send`` relay runs inside the caller/target        #
# persona's profile-home override (``persona_profile_context`` diverts          #
# ``get_hermes_home()``). A bare ``SessionDB()`` there wrote the chat session   #
# row + messages into the PROFILE's ``state.db`` — invisible to Mission         #
# Control, whose projection reads the OPERATOR/head home. The runtime-root-     #
# scoped turn store + trace survived (they are not diverted by the override),   #
# so the console showed only trace thinking rows while the relayed message +    #
# reply never rendered. The persona-chat SessionDB must bind to the head home   #
# regardless of the active override.                                           #
# --------------------------------------------------------------------------- #


def _qa_profile_binding(profile_home: Path) -> PersonaProfileBinding:
    # A profile-backed relay caller/target: binding.profile_home set (so
    # persona_profile_context pushes the override) without needing a real
    # on-disk profile to exist.
    return PersonaProfileBinding(
        persona_id="qa",
        hermes_profile="qa_fake_profile",
        profile_home=profile_home,
    )


def test_persona_session_db_binds_to_head_home_under_profile_override(
    isolate_agent_runtime_root, tmp_path
):
    from hermes_cli import harness
    from hermes_constants import get_hermes_head_home, get_hermes_home

    head_home = get_hermes_home()  # hermetic HERMES_HOME — the projection's home
    profile_home = tmp_path / "qa_profile"
    profile_home.mkdir()

    # Top level (no override): ordinary default DB, head resolves to itself.
    assert get_hermes_head_home() == head_home
    assert Path(harness._default_persona_session_db().db_path) == head_home / "state.db"

    # Inside the relay's profile-home override: get_hermes_home() is diverted to
    # the profile, but the operator-visible chat DB still binds to the head home.
    with persona_profile_context(_qa_profile_binding(profile_home)):
        assert get_hermes_home() == profile_home  # the override IS active
        assert get_hermes_head_home() == head_home  # …but head is preserved
        db = harness._default_persona_session_db()
        assert Path(db.db_path) == head_home / "state.db"
        assert Path(db.db_path) != profile_home / "state.db"


def test_relay_under_profile_override_persists_transcript_to_the_projection_home(
    isolate_agent_runtime_root, tmp_path
):
    # End-to-end: run the handler-equivalent persistence UNDER a profile-home
    # override, then read the projection from the head home. The relayed message
    # + reply must render as conversation, and the profile home must hold NO
    # chat state.db at all.
    from hermes_state import SessionDB

    from hermes_cli import harness
    from hermes_constants import get_hermes_home

    head_home = get_hermes_home()
    profile_home = tmp_path / "qa_profile"
    profile_home.mkdir()
    store = PersonaInstanceStore()

    with persona_profile_context(_qa_profile_binding(profile_home)):
        session_id = default_chat_session_id_for_instance(store, persona_id="qa")
        store.open_chat(persona_id="qa", session_id=session_id, default_display_name="QA Agent")
        db = harness._default_persona_session_db()
        # The write path resolved the head home even though the override is live.
        assert Path(db.db_path) == head_home / "state.db"
        harness._ensure_persona_chat_session(
            session_db=db,
            session_id=session_id,
            persona_id="qa",
            title="QA Agent chat",
        )
        db.append_message(session_id=session_id, role="user", content="From Neko: status?")
        db.append_message(session_id=session_id, role="assistant", content="QA here — all green.")

    # The profile home never received a chat DB — the whole point.
    assert not (profile_home / "state.db").exists()

    # The projection (head home) sees the relayed exchange as one visible row.
    rows = persona_chat_history_summary(persona_instances=store.list_all(), session_db=SessionDB())
    qa_rows = [row for row in rows if row["persona_id"] == "qa"]
    assert len(qa_rows) == 1, "the relay exchange must project as one visible chat row"
    row = qa_rows[0]
    assert row["session_id"] == session_id
    texts = [message["text"] for message in row["messages"]]
    assert "From Neko: status?" in texts and "QA here — all green." in texts


def test_head_home_is_the_outermost_across_nested_relay_hops(
    isolate_agent_runtime_root, tmp_path
):
    # operator -> Neko -> QA: each hop pushes its own profile-home override, but
    # the head home stays the OUTERMOST (operator) home the projection reads.
    from hermes_cli import harness
    from hermes_constants import get_hermes_head_home, get_hermes_home

    head_home = get_hermes_home()
    neko_home = tmp_path / "neko_profile"
    neko_home.mkdir()
    qa_home = tmp_path / "qa_profile"
    qa_home.mkdir()

    with persona_profile_context(
        PersonaProfileBinding(persona_id="neko", hermes_profile="neko", profile_home=neko_home)
    ):
        assert get_hermes_head_home() == head_home
        with persona_profile_context(_qa_profile_binding(qa_home)):
            assert get_hermes_home() == qa_home  # deepest override
            assert get_hermes_head_home() == head_home  # still the operator home
            assert Path(harness._default_persona_session_db().db_path) == head_home / "state.db"


# --------------------------------------------------------------------------- #
# Relay SENDER attribution (finish_reason marker → relayed_message projection)  #
#                                                                             #
# A relayed incoming message persists as a role="user" row. Without a sender    #
# marker the target's chat renders it as the OPERATOR ("Tony") instead of the   #
# sending agent. The handler resolves the caller once and stamps the sending    #
# identity onto the row's finish_reason; the read side + conversation           #
# projection attribute the message to the sending agent, role stays "operator". #
# --------------------------------------------------------------------------- #


def _target_channel(channels, session_id):
    matches = [channel for channel in channels if channel.get("session_id") == session_id]
    assert len(matches) == 1, f"expected one channel for {session_id}, got {len(matches)}"
    return matches[0]


def test_resolve_sender_none_for_operator_and_non_relay_requests(isolate_agent_runtime_root):
    # Only requested_by="agent:<token>" is a relay. Operator/CLI/coordinator
    # sends resolve to None → no marker → byte-identical persistence.
    from hermes_cli import harness

    store = PersonaInstanceStore()
    for requested_by in ("operator", "cli", "agent-chat-relay", None, "agent:"):
        assert (
            harness._resolve_relay_sender_marker(
                requested_by, instance_store=store, relay_chain_in=("neko",)
            )
            is None
        ), requested_by


def test_resolve_sender_tier1_chat_session_owner_full_identity(isolate_agent_runtime_root):
    # The caller session is the SENDER's minted chat session; its exact-mint
    # owner + store row give the full sender identity (persona + instance).
    from hermes_cli import harness
    from agent_runtime.relay_policy import build_relay_sender_marker

    store = PersonaInstanceStore()
    sender_session = default_chat_session_id_for_instance(store, persona_id="neko")
    store.open_chat(persona_id="neko", session_id=sender_session, display_name="Neko Mission Lead")
    sender_id = persona_instance_id_for("neko")

    marker = harness._resolve_relay_sender_marker(
        f"agent:{sender_session}", instance_store=store, relay_chain_in=("neko",)
    )
    assert marker == build_relay_sender_marker("neko", sender_id)


def test_resolve_sender_tier2_worker_session_scan(isolate_agent_runtime_root):
    # A worker/task-lane caller: the token is the instance's active worker
    # session (not chat-shaped), matched by the store scan — full identity, and
    # it wins over the tier-3 chain fallback.
    from hermes_cli import harness
    from agent_runtime.relay_policy import build_relay_sender_marker

    store = PersonaInstanceStore()
    inst = store.open_chat(
        persona_id="dev", session_id="persona_chat_seed_000000000000", display_name="Dev"
    )
    inst.active_worker_session_id = "worker_dev_task_7"
    store.update(inst)

    marker = harness._resolve_relay_sender_marker(
        "agent:worker_dev_task_7", instance_store=store, relay_chain_in=("neko",)
    )
    assert marker == build_relay_sender_marker("dev", inst.id)


def test_resolve_sender_tier3_persona_chain_fallback(isolate_agent_runtime_root):
    # The token resolves to no instance, but the relay chain names the immediate
    # caller persona → persona-only marker (no instance).
    from hermes_cli import harness
    from agent_runtime.relay_policy import build_relay_sender_marker

    store = PersonaInstanceStore()
    marker = harness._resolve_relay_sender_marker(
        "agent:unresolvable_token", instance_store=store, relay_chain_in=("dev", "neko")
    )
    assert marker == build_relay_sender_marker("neko", None)


def test_relay_incoming_row_carries_marker_and_projects_relayed_with_sender_name(
    isolate_agent_runtime_root,
):
    # (a) the relay-persisted row carries the sender marker with the caller's
    # instance identity; (c) the conversation projection exposes
    # kind=relayed_message + actor_persona_id/actor_instance_id/actor_display_name.
    from hermes_state import SessionDB

    from hermes_cli import harness
    from agent_runtime.operator_channels import operator_channel_summary
    from agent_runtime.persona_chat_history import PERSONA_RELAYED_MESSAGE_KIND

    store = PersonaInstanceStore()
    sender_session = default_chat_session_id_for_instance(store, persona_id="neko")
    store.open_chat(persona_id="neko", session_id=sender_session, display_name="Neko Mission Lead")
    sender_id = persona_instance_id_for("neko")

    target_session = default_chat_session_id_for_instance(store, persona_id="qa")
    store.open_chat(persona_id="qa", session_id=target_session, display_name="QA")

    marker = harness._resolve_relay_sender_marker(
        f"agent:{sender_session}", instance_store=store, relay_chain_in=("neko",)
    )

    db = SessionDB()
    harness._ensure_persona_chat_session(
        session_db=db, session_id=target_session, persona_id="qa", title="QA chat"
    )
    harness._append_persona_operator_turn(
        session_db=db,
        session_id=target_session,
        message="From Neko: status?",
        client_message_id="cm-relay-1",
        relay_marker=marker,
    )

    # Read side: the target history row is tagged relayed with the sender ids.
    rows = persona_chat_history_summary(persona_instances=store.list_all(), session_db=db)
    target_rows = [row for row in rows if row["session_id"] == target_session]
    assert len(target_rows) == 1
    relayed_history = [
        message
        for message in target_rows[0]["messages"]
        if message.get("kind") == PERSONA_RELAYED_MESSAGE_KIND
    ]
    assert len(relayed_history) == 1
    assert relayed_history[0]["relay_sender_persona_id"] == "neko"
    assert relayed_history[0]["relay_sender_instance_id"] == sender_id

    # Conversation projection: attributed to the SENDING agent, named, role
    # still "operator".
    channels = operator_channel_summary(
        persona_instances=store.list_all(),
        persona_chat_history=rows,
        persona_chat_trace=[],
    )
    messages = _target_channel(channels, target_session)["conversation"]["messages"]
    relayed = [m for m in messages if m.get("kind") == PERSONA_RELAYED_MESSAGE_KIND]
    assert len(relayed) == 1
    message = relayed[0]
    assert message["actor_persona_id"] == "neko"
    assert message["actor_instance_id"] == sender_id
    assert message["actor_display_name"] == "Neko Mission Lead"
    assert message["role"] == "operator"


def test_operator_row_carries_no_marker_and_projects_as_operator(isolate_agent_runtime_root):
    # (b) an operator-persisted row carries NO marker and projects exactly as
    # today: actor_persona_id="operator", kind="operator_message", no name.
    from hermes_state import SessionDB

    from hermes_cli import harness
    from agent_runtime.operator_channels import operator_channel_summary

    store = PersonaInstanceStore()
    target_session = default_chat_session_id_for_instance(store, persona_id="qa")
    store.open_chat(persona_id="qa", session_id=target_session, display_name="QA")

    marker = harness._resolve_relay_sender_marker(
        "operator", instance_store=store, relay_chain_in=()
    )
    assert marker is None

    db = SessionDB()
    harness._ensure_persona_chat_session(
        session_db=db, session_id=target_session, persona_id="qa", title="QA chat"
    )
    harness._append_persona_operator_turn(
        session_db=db,
        session_id=target_session,
        message="Operator: ping",
        client_message_id="cm-op-1",
        relay_marker=marker,
    )

    stored = db.get_messages(target_session)
    user_rows = [row for row in stored if row.get("role") == "user"]
    assert user_rows and all(row.get("finish_reason") in (None, "") for row in user_rows)

    rows = persona_chat_history_summary(persona_instances=store.list_all(), session_db=db)
    channels = operator_channel_summary(
        persona_instances=store.list_all(),
        persona_chat_history=rows,
        persona_chat_trace=[],
    )
    messages = _target_channel(channels, target_session)["conversation"]["messages"]
    operator_messages = [m for m in messages if m.get("role") == "operator"]
    assert operator_messages
    message = operator_messages[0]
    assert message["kind"] == "operator_message"
    assert message["actor_persona_id"] == "operator"
    assert message["actor_instance_id"] is None
    assert "actor_display_name" not in message


def test_unresolvable_sender_projects_as_agent_without_a_name(isolate_agent_runtime_root):
    # (d) an unresolvable sender (bogus token + empty chain) yields the bare
    # marker and projects actor_persona_id="agent" with no actor_display_name —
    # the honest unknown, never the operator.
    from hermes_state import SessionDB

    from hermes_cli import harness
    from agent_runtime.operator_channels import operator_channel_summary
    from agent_runtime.persona_chat_history import PERSONA_RELAYED_MESSAGE_KIND
    from agent_runtime.relay_policy import build_relay_sender_marker

    store = PersonaInstanceStore()
    target_session = default_chat_session_id_for_instance(store, persona_id="qa")
    store.open_chat(persona_id="qa", session_id=target_session, display_name="QA")

    marker = harness._resolve_relay_sender_marker(
        "agent:worker_session_bogus_999", instance_store=store, relay_chain_in=()
    )
    assert marker == build_relay_sender_marker(None, None)  # relay_from::

    db = SessionDB()
    harness._ensure_persona_chat_session(
        session_db=db, session_id=target_session, persona_id="qa", title="QA chat"
    )
    harness._append_persona_operator_turn(
        session_db=db,
        session_id=target_session,
        message="From ???: hi",
        client_message_id="cm-relay-x",
        relay_marker=marker,
    )

    rows = persona_chat_history_summary(persona_instances=store.list_all(), session_db=db)
    channels = operator_channel_summary(
        persona_instances=store.list_all(),
        persona_chat_history=rows,
        persona_chat_trace=[],
    )
    messages = _target_channel(channels, target_session)["conversation"]["messages"]
    relayed = [m for m in messages if m.get("kind") == PERSONA_RELAYED_MESSAGE_KIND]
    assert len(relayed) == 1
    message = relayed[0]
    assert message["actor_persona_id"] == "agent"
    assert message["actor_instance_id"] is None
    assert "actor_display_name" not in message
    assert message["role"] == "operator"
