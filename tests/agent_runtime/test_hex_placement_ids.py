"""Hex-shaped deliberate-placement id coverage (`personainst_<persona>_agent_<hex8>`).

The launcher now mints a NEW instance on EVERY palette drop, using the id shape
``personainst_<persona>_agent_<hex8>`` (8 lowercase hex chars) instead of the
legacy ``personainst_<persona>_agent_<n>`` counter. This locks in that every
hermes id-shape chokepoint the launcher-minted id flows through treats the hex
form IDENTICALLY to the counter form — the hermes side needs no code change
because all of it is shape-agnostic (token normalization, ``startswith`` /
``rpartition`` exact-mint), but these tests mirror the existing ``_agent_2``
coverage so a future regression is caught.
"""

from __future__ import annotations

from agent_runtime.persona_assignments import (
    PersonaInstanceStore,
    canonical_persona_instance_id,
    chat_session_is_foreign_to_instance,
    chat_session_owner_instance_id,
    is_canonical_persona_channel,
    persona_chat_session_id_for,
    persona_instance_id_for,
    persona_instance_id_for_placement,
)

HEX = "a1b2c3d4"
PLACEMENT_TOKEN = f"qa_agent_{HEX}"
PLACEMENT_ID = f"personainst_qa_agent_{HEX}"
PRIMARY_ID = "personainst_qa"


def test_placement_id_for_hex_token_is_preserved_verbatim():
    assert persona_instance_id_for_placement(PLACEMENT_TOKEN) == PLACEMENT_ID


def test_canonical_persona_instance_id_does_not_fold_the_hex_placement():
    # No `_agent_<n>` (or hex) special-casing: a placement id survives verbatim,
    # never collapsing onto the persona's canonical channel.
    assert canonical_persona_instance_id(PLACEMENT_ID, persona_id="qa") == PLACEMENT_ID
    assert canonical_persona_instance_id(PLACEMENT_ID) == PLACEMENT_ID
    # It is distinct from the canonical channel the bare persona resolves to.
    assert canonical_persona_instance_id(PLACEMENT_ID, persona_id="qa") != persona_instance_id_for("qa")


def test_actor_token_drift_still_stripped_on_the_hex_placement():
    # `persona_personainst_..._agent_<hex8>` (actor-token leak) canonicalizes back
    # to the bare placement id, same drift-strip as the counter form.
    drifted = f"persona_{PLACEMENT_ID}"
    assert canonical_persona_instance_id(drifted) == PLACEMENT_ID


def test_hex_placement_chat_session_owner_is_the_full_placement_id():
    # The prefix-collision trap: the primary's session prefix
    # (`persona_chat_personainst_qa_`) is a PREFIX of the placement's session,
    # but exact-mint owner resolution (rpartition on the trailing 12-hex block)
    # must return the FULL placement id, never the primary.
    placement_session = persona_chat_session_id_for(PLACEMENT_ID)
    primary_session = persona_chat_session_id_for(PRIMARY_ID)

    assert chat_session_owner_instance_id(placement_session) == PLACEMENT_ID
    assert chat_session_owner_instance_id(primary_session) == PRIMARY_ID
    # The placement session's owner is NOT the primary (the collision bug).
    assert chat_session_owner_instance_id(placement_session) != PRIMARY_ID


def test_hex_placement_session_is_foreign_to_the_primary_and_vice_versa():
    placement_session = persona_chat_session_id_for(PLACEMENT_ID)
    primary_session = persona_chat_session_id_for(PRIMARY_ID)

    # The sibling-steal guard: neither instance may adopt the other's session.
    assert chat_session_is_foreign_to_instance(placement_session, PRIMARY_ID) is True
    assert chat_session_is_foreign_to_instance(primary_session, PLACEMENT_ID) is True
    # Each owns its own session.
    assert chat_session_is_foreign_to_instance(placement_session, PLACEMENT_ID) is False
    assert chat_session_is_foreign_to_instance(primary_session, PRIMARY_ID) is False


def test_session_belongs_to_chat_lane_excludes_hex_sibling_from_primary_lane():
    # agent_chat_open's lane check: the primary's lane must NOT swallow the hex
    # placement's session (its tail after the primary prefix is not a bare
    # 12-hex block); the placement's own lane owns it.
    from tools.agent_chat_tool import _session_belongs_to_chat_lane

    placement_session = persona_chat_session_id_for(PLACEMENT_ID)

    assert not _session_belongs_to_chat_lane(
        placement_session, handle=PRIMARY_ID, default_session=None
    )
    assert _session_belongs_to_chat_lane(
        placement_session, handle=PLACEMENT_ID, default_session=None
    )


def test_add_instance_mints_distinct_hex_placement_row(isolate_agent_runtime_root):
    # The launcher-minted create/open-chat path: `add_instance` with a hex
    # placement_id materializes a distinct `personainst_qa_agent_<hex8>` row with
    # its OWN session, leaving the canonical primary untouched.
    store = PersonaInstanceStore()
    primary = store.create_operator_chat(persona_id="qa", display_name="QA Agent")
    placement = store.add_instance(
        persona_id="qa",
        placement_id=PLACEMENT_TOKEN,
        display_name="QA Agent (2)",
    )

    assert primary.id == PRIMARY_ID
    assert placement.id == PLACEMENT_ID
    assert placement.persona_id == "qa"
    assert placement.session_id != primary.session_id
    assert placement.session_id.startswith(f"persona_chat_{PLACEMENT_ID}_")
    # The canonical primary keeps its own pointer (no sibling-steal).
    assert store.get(PRIMARY_ID).session_id == primary.session_id


def test_hex_placement_is_not_the_canonical_channel_so_retire_is_allowed(
    isolate_agent_runtime_root,
):
    # `is_canonical_persona_channel` gates the retire verb: a hex placement is NOT
    # the persona's global-singleton canonical channel, so retiring it is allowed
    # (same as the counter form); the bare `personainst_qa` stays protected.
    store = PersonaInstanceStore()
    primary = store.create_operator_chat(persona_id="qa", display_name="QA Agent")
    placement = store.add_instance(
        persona_id="qa", placement_id=PLACEMENT_TOKEN, display_name="QA Agent (2)"
    )

    assert is_canonical_persona_channel(primary) is True
    assert is_canonical_persona_channel(placement) is False


def test_ambiguous_target_counts_hex_placements_as_live_instances():
    # target_policy is count-based / shape-agnostic; a bare-persona send with a
    # canonical primary + a hex placement live is ambiguous exactly as with
    # `_agent_2`.
    from agent_runtime.target_policy import TargetCandidate, evaluate_target

    decision = evaluate_target(
        persona_id="qa",
        candidates=[
            TargetCandidate(instance_id=PRIMARY_ID, display_name="QA Agent"),
            TargetCandidate(instance_id=PLACEMENT_ID, display_name="QA Agent (2)"),
        ],
        caller_pinned_instance=False,
    )
    assert decision.allowed is False
    assert f"@{PLACEMENT_ID}" in decision.reason
