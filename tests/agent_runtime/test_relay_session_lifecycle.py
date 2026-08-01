"""Agent-to-agent relay session lifecycle (agent_chat_send → mission-chat).

The 2026-07-18 incident: a relay with ``session_id`` omitted minted a FRESH
random ``persona_chat_*`` session on every send (``persona_chat_session_id_for``
appends a uuid; the handler called it per send). So repeated relays never
threaded, and each send left an unpointed session the snapshot projection never
considered — the exchange was invisible in Mission Control, violating the tool's
own contract ("omit to continue the target's default chat session ... the whole
exchange is visible in Mission Control").

The fix routes the omitted-session path through
``default_chat_session_id_for_instance`` — the single resolve-or-mint chokepoint,
so every session this lane establishes is canonical, pointed, and visible.

The live contract on top of that chokepoint is V3 (task-scoped dispatch,
2026-07-27): an omitted session no longer means "continue", it means "open this
task's own thread" (``agent_runtime.dispatch_session_policy``, default
``new_per_dispatch``). Continuation is explicit — pass back the returned
``session_id``; ``new_session: false`` continues the target's CURRENT default
thread, which every fresh mint repoints, so it is not a durable pair thread.
These tests pin the 2026-07-18 chokepoint guarantees (canonical id, pointed,
projection-visible, no phantom mints) AND the V3 decisions layered on them:
fresh-per-dispatch, recorded lineage, the reported ``session_established``, and
that a send refused on its own arguments mints nothing at all.
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


@pytest.fixture(autouse=True)
def _persist_explicit_chat_persona_data():
    from agent_runtime.store import AgentStore
    from tests.agent_runtime.persona_samples import sample_personas

    store = AgentStore()
    for persona in sample_personas():
        store.save(persona)


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
    # A legacy worker pointer cannot displace the dedicated chat pointer.
    store = PersonaInstanceStore()
    instance = store.open_chat(persona_id="qa", session_id="persona_chat_seed_000000000000")
    instance.session_id = "worker_session_task_42"  # not a persona_chat_* id
    store.update(instance)

    minted = default_chat_session_id_for_instance(store, persona_id="qa")
    assert minted == "persona_chat_seed_000000000000"


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
    # Poisoning the retired scalar cannot corrupt the dedicated default.
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

    # Read side ignores the foreign legacy scalar and keeps the dedicated root.
    own_root = primary.default_chat_session_id
    assert resolve_default_chat_session_id_for_instance(store, persona_id="qa") == own_root
    healed = default_chat_session_id_for_instance(store, persona_id="qa")
    assert healed == own_root
    store.open_chat(persona_id="qa", session_id=healed)
    assert store.get(persona_instance_id_for("qa")).default_chat_session_id == healed
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


def test_explicit_head_home_is_stable_across_launcher_profile_selection(
    isolate_agent_runtime_root, tmp_path, monkeypatch
):
    from hermes_cli import harness
    from agent_runtime import persona_chat_history, snapshot
    from agent_runtime.profile_context import persona_profile_context
    from hermes_constants import get_hermes_head_home, get_hermes_home

    shared_head = tmp_path / "profiles" / "base"
    selected_home = tmp_path / "profiles" / "alice"
    persona_home = tmp_path / "profiles" / "neko"
    for path in (shared_head, selected_home, persona_home):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(selected_home))
    monkeypatch.setenv("HERMES_HEAD_HOME", str(shared_head))

    assert get_hermes_home() == selected_home
    assert get_hermes_head_home() == shared_head
    assert Path(harness._default_persona_session_db().db_path) == shared_head / "state.db"
    assert Path(snapshot._default_persona_session_db().db_path) == shared_head / "state.db"
    assert Path(persona_chat_history._default_session_db().db_path) == shared_head / "state.db"

    with persona_profile_context(_qa_profile_binding(persona_home)):
        assert get_hermes_home() == persona_home
        assert get_hermes_head_home() == shared_head
        assert Path(harness._default_persona_session_db().db_path) == shared_head / "state.db"
        assert Path(persona_chat_history._default_session_db().db_path) == shared_head / "state.db"


def test_head_bound_persona_override_equal_to_head_home_is_the_same_db(
    isolate_agent_runtime_root, tmp_path, monkeypatch
):
    # A persona bound to the operator's own head profile (e.g. Neko on the
    # seeded ``base`` profile) relays in-process with the override EQUAL to the
    # authoritative head home. That is the same database, not a lost head —
    # the acquire must succeed and bind to the head DB. The former
    # path-equality fail-closed check raised here and killed every relay such
    # a persona sent (live 2026-07-23: agent_chat_send →
    # chat_session_db_unavailable in ~20ms).
    from hermes_cli import harness
    from hermes_constants import get_hermes_head_home, get_hermes_home

    shared_head = tmp_path / "profiles" / "base"
    shared_head.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HEAD_HOME", str(shared_head))

    with persona_profile_context(_qa_profile_binding(shared_head)):
        assert get_hermes_home() == shared_head
        assert get_hermes_head_home() == shared_head
        db = harness._default_persona_session_db()
        assert Path(db.db_path) == shared_head / "state.db"


def test_override_without_any_head_authority_still_fails_closed(
    isolate_agent_runtime_root, tmp_path, monkeypatch
):
    # The original 2026-07-18 incident shape: an override is active but no
    # head was recorded and no HERMES_HEAD_HOME is configured, so the "head"
    # degenerates to the override and the operator home is unknown. Writing
    # there would create a transcript invisible to Mission Control — the
    # acquire must keep failing closed.
    from hermes_cli import harness
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    monkeypatch.delenv("HERMES_HEAD_HOME", raising=False)
    profile_home = tmp_path / "orphan_profile"
    profile_home.mkdir()

    token = set_hermes_home_override(str(profile_home))
    try:
        with pytest.raises(harness.PersonaChatPersistenceError):
            harness._default_persona_session_db()
    finally:
        reset_hermes_home_override(token)


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


def test_resolve_sender_tier2_bound_session_scan(isolate_agent_runtime_root):
    # A task-lane caller whose token is the instance's bound session but whose
    # exact-mint owner is not a live row: tier 1 misses, the tier-2 store scan
    # matches on default_chat_session_id — full identity, and it wins over the
    # tier-3 chain fallback. S56 removed the active_worker_session_id candidate
    # from this scan with the worker store that was its only writer; the
    # bound-session candidate is what survives.
    from hermes_cli import harness
    from agent_runtime.relay_policy import build_relay_sender_marker

    store = PersonaInstanceStore()
    inst = store.open_chat(
        persona_id="dev", session_id="persona_chat_seed_000000000000", display_name="Dev"
    )
    assert inst.default_chat_session_id == "persona_chat_seed_000000000000"

    marker = harness._resolve_relay_sender_marker(
        "agent:persona_chat_seed_000000000000", instance_store=store, relay_chain_in=("neko",)
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


# --------------------------------------------------------------------------- #
# The relay sender-attribution WIRE (2026-07-31)                              #
#                                                                             #
# The four tests above pin the two ENDS: the resolver produces a marker, and a #
# row that already carries one projects as the sending agent. They pinned      #
# nothing in between — so when native session continuity (c60413e17) deleted   #
# the mission-chat lane's own `_append_persona_operator_turn(relay_marker=)`   #
# call site, all seven stayed green while every live relay went back to        #
# rendering as the OPERATOR. These tests walk the middle: CLI chokepoint ->    #
# mission_chat_reply -> AgentRunRequest -> the staged user-row dict -> the     #
# native message projection -> the persisted row -> history parse ->          #
# attribution.                                                                 #
# --------------------------------------------------------------------------- #


def _persona_commands_source() -> str:
    import hermes_cli.harness as harness

    return (
        Path(harness.__file__).with_name("harness_parts") / "persona_commands.py"
    ).read_text(encoding="utf-8")


# The mission-chat turn body was split on 2026-07-31 into a PLAN phase
# (``_cmd_mission_chat_message``: resolve, refuse, decide) and the SOLE WRITER
# (``_mission_chat_commit_turn``: everything under the chat-root lease). The
# relay marker is resolved and forwarded inside the writer. Both halves are
# searched so this guard follows the code if the boundary moves again, rather
# than silently finding nothing and passing.
_TURN_BODY_FUNCTIONS = ("_mission_chat_commit_turn", "_cmd_mission_chat_message")


def _mission_chat_reply_call_in_chat_command():
    """The `mission_chat_reply(...)` call inside the mission-chat turn body.

    persona_commands.py is exec'd into harness globals rather than imported, so
    its wiring is pinned by parsing the exact source text that gets exec'd —
    the same idiom as the record-at-injection and usage-single-writer guards.
    """
    import ast

    tree = ast.parse(_persona_commands_source())
    for name in _TURN_BODY_FUNCTIONS:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                for call in ast.walk(node):
                    if (
                        isinstance(call, ast.Call)
                        and getattr(call.func, "attr", None) == "mission_chat_reply"
                    ):
                        return node, call
    raise AssertionError(
        "mission_chat_reply call not found in the mission-chat turn body "
        f"({' / '.join(_TURN_BODY_FUNCTIONS)})"
    )


def test_the_chat_lane_resolves_the_sender_and_hands_it_to_the_runtime():
    # Hop 1: the live lane must both CALL the resolver and forward its answer.
    # A resolver with no caller is exactly the state c60413e17 left behind.
    import ast

    func, call = _mission_chat_reply_call_in_chat_command()
    resolver_targets = [
        node.targets[0].id
        for node in ast.walk(func)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", None) == "_resolve_relay_sender_marker"
        and isinstance(node.targets[0], ast.Name)
    ]
    assert resolver_targets, (
        "_cmd_mission_chat_message never calls _resolve_relay_sender_marker; "
        "relayed rows will persist unattributed"
    )
    forwarded = [
        keyword.value.id
        for keyword in call.keywords
        if keyword.arg == "relay_sender_marker" and isinstance(keyword.value, ast.Name)
    ]
    assert forwarded, "mission_chat_reply is not given relay_sender_marker"
    assert set(forwarded) <= set(resolver_targets), (
        "relay_sender_marker is forwarded from something other than the resolver"
    )


def test_mission_chat_reply_puts_the_marker_on_the_run_request(monkeypatch):
    # Hop 2: the runtime request carries the marker to the runner.
    from agent_runtime import persona_runtime
    from agent_runtime.store import AgentStore

    persona = next(item for item in AgentStore().list_all() if item.id == "qa")
    captured: dict[str, object] = {}

    class _StubRunner:
        def run(self, request):
            captured["request"] = request
            raise RuntimeError("stop after request assembly")

    runtime = persona_runtime.GPTPersonaRuntime()
    runtime._runner = _StubRunner()
    monkeypatch.setattr(
        persona_runtime, "assert_provider_health_for_persona", lambda _persona: None
    )
    monkeypatch.setattr(
        persona_runtime,
        "resolve_persona_profile",
        lambda _persona: PersonaProfileBinding(
            persona_id="qa",
            hermes_profile="qa",
            profile_home=None,
            readiness="ready",
            summary="stubbed for the wire test",
        ),
    )
    with pytest.raises(RuntimeError, match="stop after request assembly"):
        runtime.mission_chat_reply(
            persona,
            "From Neko: status?",
            session_id="persona_chat_personainst_qa_deadbeef",
            permission_session_id="persona_chat_personainst_qa_deadbeef",
            root_chat_session_id="persona_chat_personainst_qa_deadbeef",
            client_message_id="cm-relay-wire",
            relay_sender_marker="relay_from:neko:personainst_neko",
        )
    assert captured["request"].persona_chat_user_finish_reason == (
        "relay_from:neko:personainst_neko"
    )


def test_the_runner_stages_the_marker_where_the_prologue_will_adopt_it():
    # Hop 3: the runner stages the exact dict `build_turn_context` adopts.
    # The adoption predicate is an equality check against the prologue's
    # SANITIZED copy of this turn's message, so the staged content is asserted
    # against that same sanitizer rather than the raw string.
    from agent.message_sanitization import _sanitize_surrogates
    from agent_runtime.profile_runner import (
        AgentRunRequest,
        stage_persona_chat_user_row_marker,
    )

    class _Agent:
        pass

    agent = _Agent()
    request = AgentRunRequest(
        profile="qa",
        user_message="From Neko: status?\ud800",
        root_chat_session_id="persona_chat_personainst_qa_deadbeef",
        client_message_id="cm-relay-wire",
        persona_chat_user_finish_reason="relay_from:neko:personainst_neko",
    )
    staged = stage_persona_chat_user_row_marker(agent, request)
    assert staged is agent._pending_cli_user_message
    assert staged["role"] == "user"
    assert staged["finish_reason"] == "relay_from:neko:personainst_neko"
    assert staged["content"] == _sanitize_surrogates(request.user_message)


def test_an_operator_send_stages_nothing_and_clears_a_resident_leftover():
    # The byte-identical guarantee, and the resident-agent hazard that comes
    # with it: a marker staged for one relay turn must never be adopted by the
    # NEXT turn on the same resident chat actor.
    from agent_runtime.profile_runner import (
        AgentRunRequest,
        stage_persona_chat_user_row_marker,
    )

    class _Agent:
        pass

    agent = _Agent()
    agent._pending_cli_user_message = {
        "role": "user",
        "content": "From Neko: status?",
        "finish_reason": "relay_from:neko:personainst_neko",
    }
    request = AgentRunRequest(
        profile="qa",
        user_message="Operator: ping",
        root_chat_session_id="persona_chat_personainst_qa_deadbeef",
        client_message_id="cm-op-wire",
    )
    assert stage_persona_chat_user_row_marker(agent, request) is None
    assert agent._pending_cli_user_message is None


def test_the_staged_row_survives_the_native_projection_and_attributes(
    isolate_agent_runtime_root,
):
    # Hops 4-6: the staged dict goes through the SAME native-message projection
    # the session flush applies, is persisted as the turn's user row, and the
    # read side attributes it to the SENDING agent.
    from hermes_state import SessionDB

    from hermes_cli import harness
    from agent_runtime.operator_channels import operator_channel_summary
    from agent_runtime.persona_chat_continuity import safe_native_message
    from agent_runtime.persona_chat_history import PERSONA_RELAYED_MESSAGE_KIND
    from agent_runtime.profile_runner import (
        AgentRunRequest,
        stage_persona_chat_user_row_marker,
    )

    store = PersonaInstanceStore()
    sender_session = default_chat_session_id_for_instance(store, persona_id="neko")
    store.open_chat(
        persona_id="neko", session_id=sender_session, display_name="Neko Mission Lead"
    )
    sender_id = persona_instance_id_for("neko")
    target_session = default_chat_session_id_for_instance(store, persona_id="qa")
    store.open_chat(persona_id="qa", session_id=target_session, display_name="QA")

    marker = harness._resolve_relay_sender_marker(
        f"agent:{sender_session}", instance_store=store, relay_chain_in=("neko",)
    )

    class _Agent:
        pass

    agent = _Agent()
    request = AgentRunRequest(
        profile="qa",
        user_message="From Neko: status?",
        root_chat_session_id=target_session,
        client_message_id="cm-relay-wire",
        persona_chat_user_finish_reason=marker,
    )
    staged = stage_persona_chat_user_row_marker(agent, request)

    # The flush hands every persona-chat row through safe_native_message before
    # writing it; a projection that dropped finish_reason would silently strip
    # the attribution.
    native = safe_native_message(
        {
            **staged,
            "root_chat_session_id": target_session,
            "client_message_id": request.client_message_id,
        }
    )
    assert native["finish_reason"] == marker

    db = SessionDB()
    harness._ensure_persona_chat_session(
        session_db=db, session_id=target_session, persona_id="qa", title="QA chat"
    )
    db.append_message(
        session_id=target_session,
        role=native["role"],
        content=native["content"],
        finish_reason=native["finish_reason"],
        platform_message_id=request.client_message_id,
    )

    rows = persona_chat_history_summary(
        persona_instances=store.list_all(), session_db=db
    )
    channels = operator_channel_summary(
        persona_instances=store.list_all(),
        persona_chat_history=rows,
        persona_chat_trace=[],
    )
    messages = _target_channel(channels, target_session)["conversation"]["messages"]
    relayed = [m for m in messages if m.get("kind") == PERSONA_RELAYED_MESSAGE_KIND]
    assert len(relayed) == 1
    assert relayed[0]["actor_persona_id"] == "neko"
    assert relayed[0]["actor_instance_id"] == sender_id
    assert relayed[0]["actor_display_name"] == "Neko Mission Lead"


def test_the_two_upstream_seams_the_marker_rides_are_still_present():
    # The staging seam is only as durable as the two upstream lines it rides.
    # Pin them by source so an upstream merge that reshapes either one fails
    # HERE — loudly — instead of silently un-attributing every relay again.
    import agent.turn_context as turn_context
    import run_agent

    prologue = Path(turn_context.__file__).read_text(encoding="utf-8")
    assert 'pending_cli_message = getattr(agent, "_pending_cli_user_message", None)' in prologue
    assert "user_msg = pending_cli_message" in prologue

    flush = Path(run_agent.__file__).read_text(encoding="utf-8")
    assert 'finish_reason=msg.get("finish_reason")' in flush


# --------------------------------------------------------------------------- #
# Task-scoped dispatch sessions (2026-07-27)                                  #
#                                                                             #
# The 2026-07-18 fix above made an omitted-session relay CONTINUE the target's #
# default thread. Correct for a conversation, wrong for a dispatch: a mission  #
# lead briefing one teammate on ten unrelated tasks piled all ten into one     #
# thread that was re-fed to the provider every turn (observed: 293K input      #
# tokens for a 1.7K-output task). A dispatch now opens its OWN thread by       #
# default, records the thread it superseded, and reports how the thread was    #
# established — while the 2026-07-18 guarantees hold: the mint still rides the #
# canonical chokepoint, so the session is pointed, canonical, and visible.     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def dispatch_home(tmp_path, monkeypatch):
    """A throwaway home for the handler lane.

    Driving the real handler reaches ``active_profile_name()``, which reads
    ``Path.home()/.hermes/active_profile``. The hermetic runner blanks the home
    env vars, so ``Path.home()`` raises there — and pointing at the developer's
    REAL home would make the test read live profile state. Give it a temp one."""

    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True, exist_ok=True)
    for var in ("HOME", "USERPROFILE", "HERMES_HOME"):
        monkeypatch.setenv(var, str(home))
    return home


class _DispatchTranscriptDB:
    """Minimal SessionDB stand-in for driving the handler end to end."""

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.messages: dict[str, list] = {}
        self.titles: dict[str, str] = {}

    def create_session(self, session_id, source, **kwargs):
        self.sessions.setdefault(session_id, {"source": source, **kwargs})
        self.messages.setdefault(session_id, [])
        return session_id

    def get_session(self, session_id):
        session = self.sessions.get(session_id)
        if session is None:
            return None
        return {
            "id": session_id,
            "source": session.get("source"),
            "system_prompt": session.get("system_prompt"),
            "model": session.get("model"),
            "model_config": session.get("model_config"),
            "title": self.titles.get(session_id),
            "preview": None,
            "message_count": len(self.messages.get(session_id, [])),
            "started_at": None,
            "last_active": None,
            "archived": 0,
        }

    def list_sessions_rich(self, **kwargs):
        source = kwargs.get("source")
        exclude_sources = set(kwargs.get("exclude_sources") or [])
        rows = []
        for session_id, session in self.sessions.items():
            row_source = session.get("source")
            if source and row_source != source:
                continue
            if row_source in exclude_sources:
                continue
            rows.append({**self.get_session(session_id)})
        return rows

    def append_message(self, session_id, role, content=None, **kwargs):
        self.messages.setdefault(session_id, []).append({"role": role, "content": content, **kwargs})
        return len(self.messages[session_id])

    def get_messages(self, session_id, include_inactive=False):
        return list(self.messages.get(session_id, []))

    def get_session_title(self, session_id):
        return self.titles.get(session_id)

    def set_session_title(self, session_id, title):
        self.titles[session_id] = title

    def update_session_meta(self, session_id, model_config_json, model=None):
        session = self.sessions.setdefault(session_id, {})
        session["model_config"] = model_config_json
        if model is not None:
            session["model"] = model

    def delete_session(self, session_id, **kwargs):
        return bool(self.sessions.pop(session_id, None))

    def session_meta(self, session_id) -> dict:
        import json as _json

        raw = (self.sessions.get(session_id) or {}).get("model_config")
        return _json.loads(raw) if raw else {}


def _dispatch_args(message: str, client_message_id: str, **overrides):
    """Args as agent_chat_send builds them: no session, no new_session opinion."""

    from types import SimpleNamespace

    base = dict(
        persona_id="dev",
        persona_instance_id=None,
        session_id=None,
        task_id=None,
        goal_id=None,
        title=None,
        message=message,
        provider=None,
        model=None,
        use_agent_default=False,
        surface_prompt="",
        intent_hint="chat",
        requested_by="agent:worker_session_lead",
        requested_by_session=None,
        client_message_id=client_message_id,
        stream=False,
        max_seconds=5.0,
        json=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _install_dispatch_handler_doubles(monkeypatch, *, clarify_request=None):
    """Stub the model turn + transcript store; keep the REAL session lane."""

    from agent_runtime.config import AgentRuntimeConfig
    from hermes_cli import harness

    db = _DispatchTranscriptDB()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: AgentRuntimeConfig())
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)
    monkeypatch.setattr(harness, "_maybe_auto_title_persona_chat", lambda **_kwargs: None)

    class _FakeRuntime:
        def __init__(self, *args, **kwargs):
            pass

        def mission_chat_reply(self, persona_arg, message, **kwargs):
            from types import SimpleNamespace

            raw = {
                "model_input_observability": {
                    "kind": "redaction_safe_final_model_input",
                    "message_count": 1,
                    "messages": [],
                }
            }
            if clarify_request is not None:
                raw["clarify_request"] = clarify_request
            return SimpleNamespace(
                final_response="ack",
                input_tokens=1,
                output_tokens=1,
                total_tokens=2,
                raw=raw,
            )

    monkeypatch.setattr(harness, "GPTPersonaRuntime", _FakeRuntime)
    return db


def _send(capsys, args) -> dict:
    import json as _json

    from hermes_cli import harness

    code = harness._cmd_mission_chat_message(args)
    payload = _json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert payload["ok"] is True
    return payload


def _refused(capsys, args) -> dict:
    """Drive the same handler for a send it must REFUSE."""

    import json as _json

    from hermes_cli import harness

    code = harness._cmd_mission_chat_message(args)
    payload = _json.loads(capsys.readouterr().out)
    assert code == 2, payload
    assert payload["ok"] is False
    return payload


def _dev_chat_sessions(db) -> list[str]:
    prefix = f"persona_chat_{persona_instance_id_for('dev')}_"
    return [session_id for session_id in db.sessions if session_id.startswith(prefix)]


def test_dispatch_default_opens_a_fresh_thread_per_task(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    _install_dispatch_handler_doubles(monkeypatch)

    first = _send(capsys, _dispatch_args("triage the flaky login test", "cm-dispatch-1"))
    second = _send(capsys, _dispatch_args("audit the download resume path", "cm-dispatch-2"))

    assert first["session_id"] != second["session_id"], (
        "two unrelated dispatches must not share one mega-thread"
    )
    for payload in (first, second):
        assert payload["session_established"]["fresh"] is True
        assert payload["session_established"]["reason"] == "policy_new_per_dispatch"


def test_fresh_dispatch_thread_is_canonical_pointed_and_visible(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    # The 2026-07-18 orphaned-relay guarantee must survive the new default: the
    # mint rides the canonical chokepoint, so the session belongs to the
    # canonical instance, becomes its pointer, and renders in the projection.
    db = _install_dispatch_handler_doubles(monkeypatch)

    payload = _send(capsys, _dispatch_args("investigate the cold-start cost", "cm-dispatch-3"))
    session_id = payload["session_id"]
    handle = persona_instance_id_for("dev")

    assert session_id.startswith(f"persona_chat_{handle}_")
    store = PersonaInstanceStore()
    assert resolve_default_chat_session_id_for_instance(store, persona_id="dev") == session_id
    rows = persona_chat_history_summary(persona_instances=store.list_all(), session_db=db)
    visible = [row for row in rows if row["session_id"] == session_id]
    assert len(visible) == 1
    assert visible[0]["persona_instance_id"] == handle


def test_fresh_dispatch_records_the_thread_it_superseded(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    # Lineage, not amnesia: the retired thread is reachable from the new one via
    # the session meta, and named in the reply envelope.
    db = _install_dispatch_handler_doubles(monkeypatch)

    first = _send(capsys, _dispatch_args("task one", "cm-lineage-1"))
    second = _send(
        capsys,
        _dispatch_args("task two", "cm-lineage-2", requested_by_session="worker_session_lead"),
    )

    assert second["session_established"]["predecessor_session_id"] == first["session_id"]
    lineage = db.session_meta(second["session_id"])["_dispatched_from"]
    assert lineage["predecessor_chat_session_id"] == first["session_id"]
    assert lineage["requested_by_session"] == "worker_session_lead"
    # The very first dispatch had nothing to supersede — honest null, not a
    # self-reference.
    assert first["session_established"]["predecessor_session_id"] is None
    assert "_dispatched_from" not in db.session_meta(first["session_id"])


def test_dispatch_lineage_never_borrows_parent_session_id(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    # parent_session_id is claimed by native-compression lineage on this lane
    # (native_lineage_summary raises on a foreign parent; usage aggregation
    # blanks). Relay provenance must ride its own marker key.
    db = _install_dispatch_handler_doubles(monkeypatch)

    _send(capsys, _dispatch_args("task one", "cm-parent-1"))
    second = _send(capsys, _dispatch_args("task two", "cm-parent-2"))

    meta = db.session_meta(second["session_id"])
    assert "_dispatched_from" in meta
    assert "parent_session_id" not in meta


def test_explicit_continuation_keeps_the_durable_pair_thread(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    # new_session=False is a real answer, not "unset": it continues the durable
    # thread even while the policy default is new_per_dispatch. This is also the
    # CLI/serve operator-console lane (argparse store_true gives explicit False).
    _install_dispatch_handler_doubles(monkeypatch)

    first = _send(capsys, _dispatch_args("hello", "cm-sticky-1", new_session=False))
    second = _send(capsys, _dispatch_args("still here", "cm-sticky-2", new_session=False))

    assert second["session_id"] == first["session_id"]
    # Asserted key BY KEY, not as an exact dict: the exact-dict form pinned the
    # ABSENCE of every other envelope field as contract, so additive work
    # elsewhere broke a test that has nothing to say about it. All three values
    # this test actually cares about are still pinned, exactly as before.
    established = second["session_established"]
    assert established["fresh"] is False
    assert established["reason"] == "sticky_default"
    assert established["predecessor_session_id"] is None
    # The first send had no thread to continue, so it minted one — reported
    # honestly as fresh, with the sticky reason it was actually decided by.
    assert first["session_established"]["fresh"] is True
    assert first["session_established"]["reason"] == "sticky_default"


def test_sticky_continuation_follows_the_latest_thread_not_a_pair_thread(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    # The retired vocabulary said new_session=false continues "the durable pair
    # thread". There is no such thread: every fresh dispatch repoints the
    # instance's default-thread pointer through open_chat, so "continue" means
    # the MOST RECENTLY established thread. Interleave the two lanes and pin the
    # actual pointer semantics — a conversation you want to keep is continued by
    # its session_id, never by a flag.
    _install_dispatch_handler_doubles(monkeypatch)

    conversation = _send(
        capsys, _dispatch_args("how are the builds looking", "cm-interleave-1", new_session=False)
    )
    dispatched = _send(capsys, _dispatch_args("audit the download resume path", "cm-interleave-2"))
    followed = _send(
        capsys, _dispatch_args("and the flaky login test?", "cm-interleave-3", new_session=False)
    )

    assert dispatched["session_id"] != conversation["session_id"], (
        "the dispatch must open its own task thread"
    )
    # THE point: the sticky send lands in the DISPATCH-minted thread, because
    # that is what the pointer now names — not back in the earlier conversation.
    assert followed["session_id"] == dispatched["session_id"]
    # Key by key, not an exact dict — same reason as the sibling sticky case
    # above: the shape of the whole envelope is not what this test is about, and
    # pinning it here only made additive work fail in the wrong place.
    established = followed["session_established"]
    assert established["fresh"] is False
    assert established["reason"] == "sticky_default"
    assert established["predecessor_session_id"] is None
    # The earlier conversation is not lost, just no longer the default: it is
    # continued by naming it, which is exactly what the contract now says.
    resumed = _send(
        capsys,
        _dispatch_args("back to the builds", "cm-interleave-4", session_id=conversation["session_id"]),
    )
    assert resumed["session_id"] == conversation["session_id"]
    assert resumed["session_established"]["reason"] == "explicit_session_id"


def test_a_dispatch_refused_on_its_own_arguments_mints_nothing(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    # Minting before validating meant every retried or refused dispatch left a
    # titled empty thread in the sidebar. Under new_per_dispatch that is not a
    # cosmetic leak: the mint also repoints the default-thread pointer, so a
    # refusal would steal the thread the next sticky send continues.
    db = _install_dispatch_handler_doubles(monkeypatch)

    blank = _refused(capsys, _dispatch_args("   ", "cm-reject-message"))
    assert blank["error"] == "message is required"

    override = _refused(
        capsys,
        _dispatch_args(
            "triage the flaky login test",
            "cm-reject-override",
            use_agent_default=True,
            model="gpt-5-codex",
        ),
    )
    assert override["error_kind"] == "invalid_chat_model_override"

    assert _dev_chat_sessions(db) == [], "a refused dispatch must not create a thread"
    assert (
        resolve_default_chat_session_id_for_instance(PersonaInstanceStore(), persona_id="dev")
        is None
    ), "a refused dispatch must not establish a default-thread pointer"


def test_a_refused_dispatch_leaves_an_established_thread_pointed_where_it_was(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    # The repoint half of the same bug: with a live thread already in place, a
    # refused dispatch must not mint a successor and repoint onto it.
    db = _install_dispatch_handler_doubles(monkeypatch)

    established = _send(capsys, _dispatch_args("task one", "cm-keep-pointer-1"))
    _refused(
        capsys,
        _dispatch_args(
            "task two",
            "cm-keep-pointer-2",
            use_agent_default=True,
            provider="not a provider",
        ),
    )

    assert _dev_chat_sessions(db) == [established["session_id"]]
    assert resolve_default_chat_session_id_for_instance(
        PersonaInstanceStore(), persona_id="dev"
    ) == established["session_id"]


def test_a_replayed_dispatch_reports_the_same_thread_lineage(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    # A resend of the same client_message_id is the SAME turn answered again:
    # the mint receipt is idempotency-keyed, so it lands back in the thread it
    # established. The envelope must say so — a caller that reads
    # session_established to decide where its follow-up goes cannot have that
    # answer vanish on a retry.
    db = _install_dispatch_handler_doubles(monkeypatch)

    earlier = _send(capsys, _dispatch_args("task one", "cm-replay-1"))
    second = _send(capsys, _dispatch_args("triage the flaky login test", "cm-replay-2"))
    replay = _send(capsys, _dispatch_args("triage the flaky login test", "cm-replay-2"))

    assert replay["idempotent_replay"] is True
    assert replay["session_id"] == second["session_id"]
    assert replay["session_established"] == second["session_established"]
    assert replay["session_established"]["predecessor_session_id"] == earlier["session_id"]
    # …and the retry did not rewrite the session's own lineage into a loop: by
    # the time it ran, the default-thread pointer WAS this thread.
    lineage = db.session_meta(second["session_id"])["_dispatched_from"]
    assert lineage["predecessor_chat_session_id"] == earlier["session_id"]


def test_a_replayed_relay_dispatch_keeps_the_provenance_it_was_born_with(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    # THE relay lane, not a hypothetical: agent_chat_send always forwards
    # requested_by_session, so a replay's recomputed lineage is never empty —
    # the self-referential predecessor drops out and the sender key keeps the
    # dict truthy. Re-deriving would then write {requested_by_session} over the
    # stored block (this meta write replaces it wholesale) and ERASE the real
    # predecessor. Lineage is established once, by the mint that created the
    # thread; a retry carries it, it does not recompute it.
    db = _install_dispatch_handler_doubles(monkeypatch)

    earlier = _send(
        capsys,
        _dispatch_args("task one", "cm-relay-replay-1", requested_by_session="worker_lead"),
    )
    dispatched = _send(
        capsys,
        _dispatch_args(
            "triage the flaky login test",
            "cm-relay-replay-2",
            requested_by_session="worker_lead",
        ),
    )
    # The retry arrives from a DIFFERENT sender session: who asked for the thread
    # is a fact about the mint that opened it, not about whoever resent the turn.
    replay = _send(
        capsys,
        _dispatch_args(
            "triage the flaky login test",
            "cm-relay-replay-2",
            requested_by_session="worker_second",
        ),
    )

    assert replay["session_id"] == dispatched["session_id"]
    assert replay["session_established"] == dispatched["session_established"]
    assert replay["session_established"]["predecessor_session_id"] == earlier["session_id"]
    lineage = db.session_meta(dispatched["session_id"])["_dispatched_from"]
    assert lineage["predecessor_chat_session_id"] == earlier["session_id"]
    assert lineage["requested_by_session"] == "worker_lead"


def test_an_interleaved_retry_never_claims_the_thread_that_followed_it(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    # Replay of dispatch A AFTER dispatch B ran. The pointer now names B, so A's
    # re-read "predecessor" is not even a self-reference any more — it survives
    # the drop rule and would record "A superseded B". Backwards fabrication: A
    # came first and superseded nothing. The arrow a lineage reader follows must
    # never be invented by the order retries happen to arrive in.
    db = _install_dispatch_handler_doubles(monkeypatch)

    first = _send(capsys, _dispatch_args("task one", "cm-interleaved-1"))
    second = _send(capsys, _dispatch_args("task two", "cm-interleaved-2"))
    assert second["session_established"]["predecessor_session_id"] == first["session_id"]

    replay = _send(capsys, _dispatch_args("task one", "cm-interleaved-1"))

    assert replay["session_id"] == first["session_id"]
    assert replay["session_established"]["predecessor_session_id"] is None
    assert "_dispatched_from" not in db.session_meta(first["session_id"])
    # …and B's lineage is untouched by A's retry: it still names A.
    assert db.session_meta(second["session_id"])["_dispatched_from"] == {
        "predecessor_chat_session_id": first["session_id"]
    }


def test_sticky_policy_restores_the_durable_thread_for_unset_callers(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    import agent_runtime.config as runtime_config_module

    monkeypatch.setattr(
        runtime_config_module, "mission_chat_dispatch_session_policy", lambda cfg=None: "sticky"
    )
    _install_dispatch_handler_doubles(monkeypatch)

    first = _send(capsys, _dispatch_args("task one", "cm-policy-sticky-1"))
    second = _send(capsys, _dispatch_args("task two", "cm-policy-sticky-2"))

    assert second["session_id"] == first["session_id"]
    assert second["session_established"]["reason"] == "policy_sticky"
    assert second["session_established"]["fresh"] is False


def test_explicit_session_id_continues_that_exact_thread(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    _install_dispatch_handler_doubles(monkeypatch)

    opened = _send(capsys, _dispatch_args("start the task", "cm-explicit-1"))
    followed = _send(
        capsys,
        _dispatch_args("one more detail", "cm-explicit-2", session_id=opened["session_id"]),
    )

    assert followed["session_id"] == opened["session_id"]
    # Asserted key BY KEY, not as an exact dict. The exact-dict form pinned the
    # ABSENCE of every other field as contract, so any additive envelope work
    # broke a test that has nothing to say about it (the clarify-binding block
    # is a top-level sibling for exactly this reason). What this test is about
    # is the three values below.
    established = followed["session_established"]
    assert established["fresh"] is False
    assert established["reason"] == "explicit_session_id"
    assert established["predecessor_session_id"] is None


def test_clarify_round_trip_stays_in_one_session_under_the_new_default(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    # THE correctness check for the flipped default. A briefed agent that asks a
    # clarifying question must be answerable IN THAT THREAD — the answer carries
    # the session_id the reply returned. If the answer relied on stickiness it
    # would now open a third thread and the agent would read the choice with no
    # question attached.
    db = _install_dispatch_handler_doubles(
        monkeypatch,
        clarify_request={"question": "launcher or backend?", "choices": ["launcher", "backend"]},
    )

    dispatched = _send(capsys, _dispatch_args("fix the failing test", "cm-clarify-1"))
    assert dispatched["clarify_request"] is not None
    assert dispatched["session_established"]["fresh"] is True

    answer = _send(
        capsys,
        _dispatch_args("launcher", "cm-clarify-2", session_id=dispatched["session_id"]),
    )

    assert answer["session_id"] == dispatched["session_id"]
    assert answer["session_established"]["reason"] == "explicit_session_id"
    # One thread for the whole exchange — the question and its answer are in it.
    chat_sessions = [
        session_id
        for session_id in db.sessions
        if session_id.startswith(f"persona_chat_{persona_instance_id_for('dev')}_")
    ]
    assert chat_sessions == [dispatched["session_id"]]
    # …and the answer did not mint a second one behind the caller's back.
    assert answer["session_established"]["fresh"] is False
    assert answer["session_established"]["predecessor_session_id"] is None
    assert resolve_default_chat_session_id_for_instance(
        PersonaInstanceStore(), persona_id="dev"
    ) == dispatched["session_id"]


class _StrictDispatchTranscriptDB(_DispatchTranscriptDB):
    """The same double, but seen as real persistence by the handler's guards.

    ``unknown_chat_session`` / ``foreign_chat_session`` may only refuse against a
    store where "no row" MEANS "no such session", so they ask
    ``is_canonical_session_persistence``. The clarify design's load-bearing claim
    is that a token gets BOTH guards for free by resolving before them, and
    proving that requires a double those guards actually look at.

    It declares itself under its OWN name. This class used to spoof
    ``__module__ = "hermes_state"`` — a double claiming to be a module it is not,
    purely in order to be believed — which is what forced the predicate to exist:
    a guard reachable only by a lying test asset is not honestly tested."""

    __hermes_canonical_session_persistence__ = True


def _mint_clarify_ticket(session_id: str, **overrides) -> str:
    from agent_runtime.persona_chat_continuity import PersonaChatClarifyTicketStore

    token = PersonaChatClarifyTicketStore().mint(
        chat_session_id=session_id,
        persona_instance_id=persona_instance_id_for("dev"),
        persona_id="dev",
        asked_by_client_message_id="agent-relay-aaaaaaaaaaaa",
        **overrides,
    )
    assert token
    return token


def test_clarify_token_binds_the_answer_when_the_parent_names_no_session(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    """THE headline case, and the reason the token exists.

    Under ``new_per_dispatch`` an answer that names no session opens a THIRD
    thread and the child reads a bare choice with no question attached. The
    only thing that used to prevent it was prompt text asking a model to
    reproduce an opaque session id. Now the runtime owns the binding: the
    answer carries only the token and still lands in the question's thread."""

    db = _install_dispatch_handler_doubles(
        monkeypatch,
        clarify_request={"question": "launcher or backend?", "choices": ["launcher", "backend"]},
    )

    dispatched = _send(capsys, _dispatch_args("fix the failing test", "cm-token-1"))
    token = dispatched["clarify_request"]["clarify_token"]
    assert token.startswith("clarify-")

    answer = _send(
        capsys,
        _dispatch_args("launcher", "cm-token-2", clarify_token=token),
    )

    assert answer["session_id"] == dispatched["session_id"]
    assert answer["session_established"]["reason"] == "clarify_token"
    assert answer["session_established"]["fresh"] is False
    assert answer["clarify_binding"]["bound_via"] == "clarify_token"
    assert answer["clarify_binding"]["bound_session_id"] == dispatched["session_id"]
    assert answer["clarify_binding"]["overrode_session_id"] is None
    # One thread for the whole exchange — no third thread behind the caller's back.
    assert _dev_chat_sessions(db) == [dispatched["session_id"]]


def test_clarify_token_overrides_a_wrong_session_id_and_says_so(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    # A caller unreliable enough to need the token is exactly the caller who
    # will also attach a stale session id. Refusing that would defeat the
    # design; landing it silently would hide a real disagreement. It lands, and
    # the override is named.
    _install_dispatch_handler_doubles(
        monkeypatch,
        clarify_request={"question": "launcher or backend?"},
    )

    stale = _send(capsys, _dispatch_args("earlier task", "cm-override-0"))
    asked = _send(capsys, _dispatch_args("fix the failing test", "cm-override-1"))
    token = asked["clarify_request"]["clarify_token"]
    assert stale["session_id"] != asked["session_id"]

    answer = _send(
        capsys,
        _dispatch_args(
            "launcher",
            "cm-override-2",
            clarify_token=token,
            session_id=stale["session_id"],
        ),
    )

    assert answer["session_id"] == asked["session_id"]
    assert answer["session_established"]["reason"] == "clarify_token"
    assert answer["clarify_binding"]["overrode_session_id"] == stale["session_id"]


def test_an_unknown_clarify_token_degrades_instead_of_refusing(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    # Tickets are swept on a TTL, so a token CAN legitimately outlive its
    # record. Turning that into a hard failure would punish the parent that did
    # exactly the right thing — so the turn falls through to normal precedence
    # and reports why.
    _install_dispatch_handler_doubles(monkeypatch)

    answer = _send(
        capsys,
        _dispatch_args("launcher", "cm-unknown-1", clarify_token="clarify-deadbeefdead"),
    )

    assert answer["clarify_binding"]["state"] == "unknown_token"
    assert answer["clarify_binding"]["bound_via"] == "none"
    assert answer["session_established"]["reason"] == "policy_new_per_dispatch"


def test_a_clarify_token_cannot_smuggle_in_a_foreign_session(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    """Placement, not a new guard: resolution happens BEFORE the existing
    ownership checks, so a token naming another instance's thread is refused by
    the same ``foreign_chat_session`` every explicit session id already hits."""

    import json as _json

    from hermes_cli import harness

    db = _StrictDispatchTranscriptDB()
    _install_dispatch_handler_doubles(monkeypatch)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)

    foreign = f"persona_chat_{persona_instance_id_for('qa')}_abcdef123456"
    db.create_session(foreign, "agent_runtime_persona_chat")
    db.update_session_meta(
        foreign,
        _json.dumps(
            {
                "mission_chat_root_id": foreign,
                "persona_instance_id": persona_instance_id_for("qa"),
                "source": "agent_runtime_persona_chat",
            }
        ),
    )
    token = _mint_clarify_ticket(foreign)

    refusal = _refused(
        capsys, _dispatch_args("launcher", "cm-foreign-1", clarify_token=token)
    )

    assert refusal["error_kind"] == "foreign_chat_session"
    assert refusal["session_id"] == foreign


def test_a_prompt_compliant_answer_still_settles_the_open_question(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    # A parent that answered correctly WITHOUT the token must not leave the
    # ticket open forever: the adoption metric would then read pessimistically
    # for every caller doing the right thing, which is the reading that would
    # argue for building more machinery than this needs.
    from agent_runtime.persona_chat_continuity import PersonaChatClarifyTicketStore

    _install_dispatch_handler_doubles(
        monkeypatch,
        clarify_request={"question": "launcher or backend?"},
    )

    asked = _send(capsys, _dispatch_args("fix the failing test", "cm-settle-1"))
    token = asked["clarify_request"]["clarify_token"]

    answer = _send(
        capsys,
        _dispatch_args("launcher", "cm-settle-2", session_id=asked["session_id"]),
    )

    assert answer["session_established"]["reason"] == "explicit_session_id"
    assert answer["clarify_binding"]["bound_via"] == "session_id"
    assert PersonaChatClarifyTicketStore().resolve(token)["state"] == "answered"


def test_the_clarify_gate_off_restores_the_pre_token_lane(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    # The whole rollback: no token minted, none resolved, and the wire shape
    # reverts to {question, choices}. Nothing to migrate, nothing to unwind.
    from hermes_cli import harness

    monkeypatch.setattr(
        harness, "mission_chat_clarify_token_binding", lambda cfg=None: False
    )
    _install_dispatch_handler_doubles(
        monkeypatch,
        clarify_request={"question": "launcher or backend?"},
    )

    asked = _send(capsys, _dispatch_args("fix the failing test", "cm-gate-1"))
    assert asked["clarify_request"] == {"question": "launcher or backend?"}
    assert "clarify_binding" not in asked


def test_fresh_dispatch_threads_are_named_after_the_task(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    # Task-scoped threads are only navigable when named; a sidebar of identical
    # "Dev chat" rows is worse than the mega-thread it replaced.
    db = _install_dispatch_handler_doubles(monkeypatch)

    derived = _send(
        capsys, _dispatch_args("Triage the flaky login test on Windows", "cm-title-1")
    )
    explicit = _send(
        capsys,
        _dispatch_args("anything", "cm-title-2", title="Download resume audit"),
    )

    assert db.get_session_title(derived["session_id"]) == "Triage the flaky login test on Windows"
    assert db.get_session_title(explicit["session_id"]) == "Download resume audit"


def test_the_durable_thread_keeps_its_persona_title(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    # The operator console mints its first thread through this same lane and
    # passes a generic --title; that lane must keep the stable per-persona name
    # rather than being renamed after one message.
    db = _install_dispatch_handler_doubles(monkeypatch)

    opened = _send(
        capsys,
        _dispatch_args("hello there", "cm-console-1", new_session=False, title="Operator message"),
    )

    title = db.get_session_title(opened["session_id"])
    assert title.endswith(" chat"), title  # "<display name> chat", the durable name
    assert title not in ("Operator message", "hello there")


# ── dispatching at a retired placement ──────────────────────────────────────


def _retire_a_placement(placement_id: str = "dev_agent_2") -> tuple[str, str]:
    """A retired ``dev`` placement: the id, and its archive path."""

    store = PersonaInstanceStore()
    placement = store.add_instance(
        persona_id="dev", placement_id=placement_id, display_name="Dev (2)"
    )
    archived = store.retire(placement.id, reason="placement deleted")
    return placement.id, archived["archive_path"]


def _mint_receipt_files() -> list[Path]:
    from agent_runtime import paths

    receipts = paths.store_root() / "persona_chat_mint_receipts"
    return sorted(receipts.glob("*.json")) if receipts.exists() else []


def test_a_dispatch_to_a_retired_placement_mints_nothing(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    # A retired placement is an end-of-life tombstone: `open_chat` refuses to
    # bind it. But the mint reaches `open_chat` LAST — after reserving the
    # receipt, creating the session row and titling it — so every dispatch at a
    # corpse left a titled thread in Mission Control, and the refusal arrived
    # from outside this call site's typed handler, i.e. as a traceback.
    db = _install_dispatch_handler_doubles(monkeypatch)
    retired_id, archive_path = _retire_a_placement()

    refusal = _refused(
        capsys,
        _dispatch_args(
            "triage the flaky login test",
            "cm-retired-1",
            persona_instance_id=retired_id,
        ),
    )

    assert refusal["error_kind"] == "retired_persona_instance"
    assert refusal["execution_state"] == "refused"
    assert refusal["persona_instance_id"] == retired_id
    assert refusal["archive_path"] == archive_path
    assert refusal["history_preserved"] is True
    # (a) no session row, (b) no mint receipt, (c) no default-thread pointer.
    assert db.sessions == {}, "a dispatch at a retired placement must not create a thread"
    assert _mint_receipt_files() == []
    store = PersonaInstanceStore()
    assert (
        resolve_default_chat_session_id_for_instance(
            store, persona_id="dev", persona_instance_id=retired_id
        )
        is None
    )
    # …and the refusal never revived the tombstoned row into the live roster.
    assert retired_id not in {row.id for row in store.list_all()}


def test_a_dispatch_to_a_retired_placement_leaves_live_threads_alone(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    # The repoint half: a live sibling's established thread must survive a
    # refused dispatch at the corpse untouched — pointer and all.
    db = _install_dispatch_handler_doubles(monkeypatch)
    retired_id, _ = _retire_a_placement()

    established = _send(capsys, _dispatch_args("task one", "cm-retired-live-1"))
    receipts_before = _mint_receipt_files()

    _refused(
        capsys,
        _dispatch_args("task two", "cm-retired-live-2", persona_instance_id=retired_id),
    )

    assert _dev_chat_sessions(db) == [established["session_id"]]
    assert list(db.sessions) == [established["session_id"]]
    assert _mint_receipt_files() == receipts_before
    assert resolve_default_chat_session_id_for_instance(
        PersonaInstanceStore(), persona_id="dev"
    ) == established["session_id"]


def test_a_streamed_dispatch_to_a_retired_placement_refuses_the_same_way(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    # The serve/stream lane must not be the one that still tracebacks: the
    # refusal is one `chat.final` frame carrying the same typed payload.
    import json as _json

    from hermes_cli import harness

    db = _install_dispatch_handler_doubles(monkeypatch)
    retired_id, archive_path = _retire_a_placement()

    code = harness._cmd_mission_chat_message(
        _dispatch_args(
            "triage the flaky login test",
            "cm-retired-stream",
            persona_instance_id=retired_id,
            stream=True,
            json=False,
        )
    )

    frames = [
        _json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert code == 2
    assert [frame["type"] for frame in frames] == ["chat.final"]
    final = frames[0]
    assert final["ok"] is False
    assert final["error_kind"] == "retired_persona_instance"
    assert final["persona_instance_id"] == retired_id
    assert final["archive_path"] == archive_path
    assert db.sessions == {}
    assert _mint_receipt_files() == []


def test_a_retired_target_is_refused_before_the_mint_lane_is_ever_entered(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    """Pins the PRE-FLIGHT specifically, not just its outcome.

    Three enforcement points render one refusal body, so the tests above pass
    on any ONE of them: delete the pre-flight and the mint's own precondition
    answers byte-identically, green suite, no signal. But they are not
    interchangeable — the pre-flight is what keeps the lane from being ENTERED,
    and everything downstream of entering it (the receipt reserve, the session
    row, the retire race that opens once the mint is running) only stays
    impossible while the refusal lands first. So make the reach itself the
    failure: this ``mint`` cannot be called without failing the test.
    """

    from agent_runtime.persona_chat_continuity import PersonaChatMintReceiptStore

    db = _install_dispatch_handler_doubles(monkeypatch)
    retired_id, archive_path = _retire_a_placement()

    def _never_reached(self, **kwargs):
        raise AssertionError(
            "the dispatch lane entered the mint for a retired target — the "
            "pre-mint refusal is no longer doing the work"
        )

    monkeypatch.setattr(PersonaChatMintReceiptStore, "mint", _never_reached)

    refusal = _refused(
        capsys,
        _dispatch_args(
            "triage the flaky login test",
            "cm-retired-preflight",
            persona_instance_id=retired_id,
        ),
    )

    # Same contract as the reachable-mint path: the operator cannot tell which
    # enforcement point fired, only that it fired before anything was written.
    assert refusal["error_kind"] == "retired_persona_instance"
    assert refusal["execution_state"] == "refused"
    assert refusal["persona_instance_id"] == retired_id
    assert refusal["archive_path"] == archive_path
    assert refusal["history_preserved"] is True
    assert db.sessions == {}
    assert _mint_receipt_files() == []


# --------------------------------------------------------------------------- #
# The removed operator route: task-bound instance chat roots (2026-07-29)     #
#                                                                             #
# Persona instances carry a lifecycle. The chat-mode on-level instances       #
# (`personainst_<role>_agent_<hash>`) are the operator's chat targets; the    #
# bare task-bound rows (`personainst_qa`, `personainst_goal_*`, …) belong to  #
# the goal graph and its daemon. Messaging a task-bound row's chat root       #
# WORKED mechanically — which is what made it worth removing, because the     #
# Mission Control console then misattributed the thread and the operator's    #
# chat rode a broken mission path with nothing to read. Operator ruling:      #
# remove the route, typed, with the redirect named.                           #
#                                                                             #
# The exemption is the whole design. The daemon/graph/worker lanes, root-node #
# engine turns and every agent-relay hop legitimately speak to these          #
# instances, so the guard fires ONLY on the bare operator send — no relay     #
# envelope, no --task/--goal.                                                 #
# --------------------------------------------------------------------------- #


def _task_bound_goal_instance(goal_id: str = "goal_alpha", persona_id: str = "dev"):
    """A goal-graph instance: placement-derived, ``task_bound``, with a root.

    Built through ``ensure_for_goal`` — the store method the graph itself uses —
    rather than by hand-writing ``mode``, so the fixture is the real shape the
    guard has to recognise and cannot drift away from it.
    """

    from agent_runtime.models import AgentPersona

    persona = AgentPersona(
        id=persona_id,
        role="dev",
        display_name="Dev Agent",
        model="gpt-test",
        provider="openai-codex",
        api_mode="codex_responses",
        toolsets=["file"],
        system_prompt_path="agent_runtime/prompts/dev.md",
        hermes_profile=None,
    )
    store = PersonaInstanceStore()
    instance = store.ensure_for_goal(persona, goal_id=goal_id, spawned_by=None)
    assert instance.mode == "task_bound", instance.mode
    return instance


def _seed_chat_root(db, instance) -> str:
    """Give *instance* a persona-chat root the handler's owner lookup resolves."""

    import json as _json

    root = f"persona_chat_{instance.id}_abcdef123456"
    db.create_session(root, PERSONA_CHAT_SESSION_SOURCE)
    db.update_session_meta(
        root,
        _json.dumps(
            {
                "mission_chat_root_id": root,
                "persona_instance_id": instance.id,
                "source": PERSONA_CHAT_SESSION_SOURCE,
            }
        ),
    )
    return root


def _install_strict_db(monkeypatch):
    """The dispatch doubles, but with a store the ownership guards may refuse on."""

    from hermes_cli import harness

    _install_dispatch_handler_doubles(monkeypatch)
    db = _StrictDispatchTranscriptDB()
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)
    return db


def test_a_bare_operator_send_to_a_legacy_instance_root_is_accepted(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    """Legacy instance metadata no longer creates a separate mission lane."""

    db = _install_strict_db(monkeypatch)
    goal_instance = _task_bound_goal_instance()
    root = _seed_chat_root(db, goal_instance)
    # The redirect target: a real on-level placement, with its own bound root.
    on_level = PersonaInstanceStore().add_instance(
        persona_id="dev", placement_id="dev_agent_2", display_name="Dev (2)"
    )
    assert on_level.mode == "chat"

    payload = _send(
        capsys, _dispatch_args("status please", "cm-task-bound-1", session_id=root)
    )
    assert payload["session_id"] == root
    assert payload["persona_instance_id"] == goal_instance.id
    assert payload["reply"] == "ack"
    assert on_level.id != goal_instance.id


def test_legacy_root_guard_helpers_are_removed(
    isolate_agent_runtime_root,
):
    """No hidden compatibility helper can restore the removed route split."""

    from hermes_cli import harness

    names = ["_" + "task_bound" + "_chat_root_refusal", "_mission_chat_" + "carries_mission_context"]
    assert all(not hasattr(harness, name) for name in names)


def test_a_chat_mode_instance_root_is_still_accepted(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    """The route that REPLACES it. A guard that also broke this would be a
    regression dressed as a fix."""

    db = _install_strict_db(monkeypatch)
    on_level = PersonaInstanceStore().add_instance(
        persona_id="dev", placement_id="dev_agent_2", display_name="Dev (2)"
    )
    root = _seed_chat_root(db, on_level)

    payload = _send(
        capsys, _dispatch_args("status please", "cm-chat-mode-1", session_id=root)
    )

    assert payload["session_id"] == root
    assert payload["persona_instance_id"] == on_level.id


def test_a_relay_hop_reaches_a_legacy_instance_root(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    """EXEMPT LANE 1 — the agent-relay envelope.

    ``agent_chat_send`` always carries the running turn's chain, so a non-empty
    ``relay_chain`` is exactly "an agent is speaking, not the operator". The
    daemon/graph/worker lanes and root-node engine turns reach these instances
    through this same envelope, and removing the operator route must not touch
    any of them."""

    db = _install_strict_db(monkeypatch)
    goal_instance = _task_bound_goal_instance()
    root = _seed_chat_root(db, goal_instance)

    payload = _send(
        capsys,
        _dispatch_args(
            "stage 2 is ready for you",
            "cm-task-bound-relay",
            session_id=root,
            relay_chain=["neko_supervisor"],
        ),
    )

    assert payload["session_id"] == root
    assert payload["persona_instance_id"] == goal_instance.id
    # The turn actually RAN in the goal's own thread — the exemption is not just
    # "no refusal printed", it is a reply the relaying agent can read.
    assert payload["reply"] == "ack"


def test_an_explicit_legacy_goal_argument_does_not_change_chat_routing(
    monkeypatch, capsys, isolate_agent_runtime_root, dispatch_home
):
    """EXEMPT LANE 2 — mission context named on the turn itself.

    Not a proxy for the mission lane but the definition of it: the handler
    already treats ``--task``/``--goal`` as the authority that MAKES an instance
    task-bound, so a send carrying one is addressing the mission lane by
    construction."""

    db = _install_strict_db(monkeypatch)
    goal_instance = _task_bound_goal_instance()
    root = _seed_chat_root(db, goal_instance)

    payload = _send(
        capsys,
        _dispatch_args(
            "stage 2 is ready for you",
            "cm-task-bound-goal",
            session_id=root,
            goal_id="goal_alpha",
        ),
    )

    assert payload["session_id"] == root
    assert payload["persona_instance_id"] == goal_instance.id


def test_no_incoming_envelope_exemption_helper_remains(
    isolate_agent_runtime_root,
):
    """Chat routing no longer branches on mission-shaped envelope metadata."""
    from hermes_cli import harness

    assert not hasattr(harness, "_mission_chat_" + "carries_mission_context")
