"""``perform_agent_retire`` — the inverse of the placement verb, and its receipt.

What this suite has to prove is not "a dict comes back". The store method it
wraps has archived both halves since long before this service existed; what did
NOT exist was any way for a caller to learn whether the office half happened.
So every test here is about a fact the pre-S5 code could not express:

1. **Both halves leave, and the ack NAMES the actors.** The witness is the
   ``archived_actor_keys`` list read against the store, never the count — a
   count of 1 is what a lucky mutant returns too.
2. **A failure in the office half is REPORTED, not swallowed.** The witness is
   an ``office_archive_failures`` entry naming the actor and its error, against
   an injected fault; the pre-S5 path swallowed the same fault into ``None``.
3. **A retire asked twice answers twice.** The second call is a REPLAY, not a
   ``not_found`` — same ids, same archive path, same actor keys.
4. **The refusal vocabulary is the store's, one to one.** A code this service
   invented would be a second vocabulary for one set of guards.

Every placement is minted through ``perform_agent_create`` — the real create
door — rather than by hand, so the actor these tests retire is the actor a
launcher drop writes, with the same key derivation and the same items.
"""

from __future__ import annotations

import pytest

from agent_runtime.agent_retire import perform_agent_retire
from tests.agent_runtime.office_seed import seed_workspace_record

WORKSPACE = "ws_agent_retire_test"


@pytest.fixture
def qa_persona():
    from agent_runtime.models import AgentPersona
    from agent_runtime.store import AgentStore

    persona = AgentPersona(
        id="qa",
        display_name="QA Agent",
        role="qa",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=[],
        system_prompt_path="",
    )
    AgentStore().save(persona)
    return persona


@pytest.fixture
def seeded_workspace():
    from agent_runtime.office_store import OfficeStore

    seed_workspace_record(WORKSPACE)
    store = OfficeStore()
    store.ensure_surface(WORKSPACE, created_by="seed")
    return store


def _place(placement_id: str = "qa_retire_1", persona_id: str = "qa") -> dict:
    """A REAL placement through the real create door. Returns its ack."""

    from agent_runtime.agent_create import perform_agent_create

    outcome = perform_agent_create(
        {
            "persona_id": persona_id,
            "workspace_id": WORKSPACE,
            "position": [2.0, -3.0],
            "idempotency_key": f"retire-fixture-{placement_id}",
            "placement_id": placement_id,
        }
    )
    assert outcome.refusal is None, outcome.refusal
    return outcome.result


def _live_actor_keys(workspace_id: str = WORKSPACE) -> set:
    from agent_runtime.office_store import OfficeStore

    return {actor.actor_key for actor in OfficeStore().list_actors(workspace_id)}


# ── both halves leave, and the ack names the actors ──────────────────────────


def test_retiring_a_placed_agent_archives_the_row_and_names_every_actor(
    qa_persona, seeded_workspace
):
    """KILLING MUTATION: make ``_archive_office_placements`` a no-op (drop the
    office half) — ``archived_actor_keys`` comes back EMPTY and this reds on the
    membership assertion, not on a count.

    The actor key is asserted against the OFFICE STORE as well as against the
    ack, because an ack that merely echoed the request would satisfy the list
    while the desk stayed on the canvas — which is precisely the half-state this
    verb exists to abolish.
    """

    from agent_runtime import paths

    placed = _place()
    actor_key = placed["actor_key"]
    assert actor_key in _live_actor_keys()

    outcome = perform_agent_retire(
        {"persona_instance_id": placed["persona_instance_id"], "reason": "S5 test"}
    )

    assert outcome.refusal is None, outcome.refusal
    ack = outcome.result
    assert ack["persona_instance_id"] == placed["persona_instance_id"]
    assert ack["persona_id"] == "qa"
    assert ack["already_retired"] is False
    # THE pin: the office half is named, not counted.
    assert ack["archived_actor_keys"] == [actor_key]
    # Empty failures is a positive claim, and it is checked against the store.
    assert ack["office_archive_failures"] == []
    assert actor_key not in _live_actor_keys()

    # And the roster half is durable where the ack says it is.
    assert not paths.persona_instance_path(placed["persona_instance_id"]).exists()
    from pathlib import Path

    assert Path(ack["archive_path"]).is_file()


def test_a_persona_keyed_actor_is_not_this_instances_and_is_left_standing(
    qa_persona, seeded_workspace
):
    """ANTI-VACUITY for the list above: it must name the actors BOUND to this
    instance, never every actor in the workspace.

    Persona-id-keyed placements survive instance churn by design
    (``archive_actors_for_instance``'s own contract), so a second actor that is
    not instance-bound is the witness that separates "archived what it should"
    from "archived everything it could reach".
    """

    from agent_runtime.office_store import OfficeStore

    placed = _place(placement_id="qa_retire_2")
    OfficeStore().upsert_actor(
        WORKSPACE,
        {
            "actor_key": "dev",
            "persona_id": "dev",
            "items": [
                {
                    "item_id": "dev",
                    "kind": "agent",
                    "position": [0.0, 0.0],
                    "folder": "Agents",
                }
            ],
        },
    )

    ack = perform_agent_retire(
        {"persona_instance_id": placed["persona_instance_id"]}
    ).result

    assert ack["archived_actor_keys"] == [placed["actor_key"]]
    assert "dev" in _live_actor_keys()


# ── the office half's failures are reported, not swallowed ───────────────────


def test_an_office_archive_fault_lands_on_the_ack_instead_of_being_swallowed(
    qa_persona, seeded_workspace, monkeypatch
):
    """KILLING MUTATION: restore the swallow — have
    ``archive_actors_for_instance`` drop its failures (or
    ``_archive_office_placements`` return ``None`` again) and
    ``office_archive_failures`` is EMPTY, which reds here.

    The fault is injected at ``remove_actor`` because that is where a real one
    lands (a share violation on a desk file held by an AV scanner is what this
    platform actually raises), and the subject is the LOOP's accounting rather
    than any one cause.

    Two things are asserted TOGETHER, and they are the whole point: the retire
    still succeeded (the roster row is authoritative with or without the office
    projection) AND it said so — the desk is still on the canvas and the ack
    names it. Either alone is the pre-S5 behaviour.
    """

    from agent_runtime.office_store import OfficeStore

    placed = _place(placement_id="qa_retire_3")
    actor_key = placed["actor_key"]

    def _refuse(*_args, **_kwargs):
        raise OSError("share violation")

    monkeypatch.setattr(OfficeStore, "remove_actor", _refuse)

    outcome = perform_agent_retire(
        {"persona_instance_id": placed["persona_instance_id"]}
    )

    assert outcome.refusal is None, "the office half must never fail the retire"
    ack = outcome.result
    assert ack["archived_actor_keys"] == []
    assert len(ack["office_archive_failures"]) == 1
    failure = ack["office_archive_failures"][0]
    assert failure["actor_key"] == actor_key
    assert failure["workspace_id"] == WORKSPACE
    assert "OSError" in failure["error"] and "share violation" in failure["error"]
    # The row DID archive; the desk did not. That asymmetry is now visible,
    # which is the entire delta over the swallowed version.
    assert ack["already_retired"] is False
    assert actor_key in _live_actor_keys()


def test_a_fault_in_the_office_projection_itself_is_not_blamed_on_an_actor(
    qa_persona, seeded_workspace, monkeypatch
):
    """A store that will not CONSTRUCT is not one actor's failure.

    The entry carries ``actor_key: None`` rather than a guessed key, because the
    value of this list is that it names what it knows and nothing else.
    """

    import agent_runtime.office_store as office_store_module

    placed = _place(placement_id="qa_retire_4")

    def _explode(*_args, **_kwargs):
        raise RuntimeError("office root unreadable")

    monkeypatch.setattr(
        office_store_module.OfficeStore, "list_workspaces", _explode
    )

    ack = perform_agent_retire(
        {"persona_instance_id": placed["persona_instance_id"]}
    ).result

    assert ack["archived_actor_keys"] == []
    assert [entry["actor_key"] for entry in ack["office_archive_failures"]] == [None]
    assert "RuntimeError" in ack["office_archive_failures"][0]["error"]


# ── idempotence ──────────────────────────────────────────────────────────────


def test_a_second_retire_replays_the_ack_rather_than_refusing(
    qa_persona, seeded_workspace
):
    """KILLING MUTATION: let the ``not_found`` arm answer 4001 unconditionally
    (delete the tombstone probe) — the second call comes back as a REFUSAL and
    this reds on ``outcome.refusal is None``.

    A remote client that lost the first ack must be able to ask again, so the
    replay is asserted to agree with the original on every DURABLE field: the
    id, the archive path, and — the one a reconstruction would get wrong — the
    actor keys, which the replay reads back out of the office ARCHIVE because
    the prune that produced them cannot find them a second time.
    """

    placed = _place(placement_id="qa_retire_5")
    first = perform_agent_retire(
        {"persona_instance_id": placed["persona_instance_id"]}
    ).result

    outcome = perform_agent_retire(
        {"persona_instance_id": placed["persona_instance_id"]}
    )

    assert outcome.refusal is None
    second = outcome.result
    assert second["already_retired"] is True
    assert first["already_retired"] is False
    assert second["persona_instance_id"] == first["persona_instance_id"]
    assert second["archive_path"] == first["archive_path"]
    assert second["archived_actor_keys"] == first["archived_actor_keys"] == [
        placed["actor_key"]
    ]
    assert second["persona_id"] == first["persona_id"] == "qa"
    assert second["display_name"] == first["display_name"]


def test_an_id_that_was_never_placed_is_still_not_found(qa_persona):
    """ANTI-VACUITY for the replay above: ``already_retired`` must be a TOMBSTONE
    reading, never "anything I cannot find is fine".

    KILLING MUTATION: answer every ``not_found`` with the replay ack — this reds,
    because a typo'd id would then report a successful retirement of nothing.
    """

    outcome = perform_agent_retire(
        {"persona_instance_id": "personainst_never_placed_at_all"}
    )

    assert outcome.result is None
    assert outcome.refusal.code == 4001
    assert outcome.refusal.data["reason"] == "not_found"


# ── the refusal vocabulary is the store's ────────────────────────────────────


def test_the_canonical_operator_channel_refuses_as_a_conflict(qa_persona):
    """The 1:1 map, measured on the guard that is easiest to reach.

    ``canonical_persona_channel`` is a CONFLICT (4090) and the code rides
    ``data.reason`` verbatim, because the launcher decodes ``data.reason`` first
    and the numeric code second.
    """

    from agent_runtime import paths
    from agent_runtime.persona_assignments import PersonaInstanceStore

    canonical = PersonaInstanceStore().ensure_for_persona(qa_persona)

    outcome = perform_agent_retire({"persona_instance_id": canonical.id})

    assert outcome.result is None
    assert outcome.refusal.code == 4090
    assert outcome.refusal.data["reason"] == "canonical_persona_channel"
    assert outcome.refusal.data["persona_instance_id"] == canonical.id
    # The store's own detail rides through rather than being re-derived here.
    assert outcome.refusal.data["persona_id"] == "qa"
    # A refusal wrote nothing.
    assert paths.persona_instance_path(canonical.id).exists()


def test_a_request_with_no_target_refuses_before_the_store():
    outcome = perform_agent_retire({})

    assert outcome.result is None
    assert outcome.refusal.code == -32602
    assert outcome.refusal.data["reason"] == "persona_instance_id_required"


def test_the_error_codes_equal_serve_rpcs(qa_persona):
    """The drift fence ``agent_create`` carries, for the same reason it carries
    it: these constants are RE-SPELLED here to keep the CLI process from
    importing the whole method registry, so nothing but a test stops the two
    copies from disagreeing.
    """

    from agent_runtime import agent_retire, serve_rpc

    assert agent_retire.ERR_INVALID_PARAMS == serve_rpc.ERR_INVALID_PARAMS
    assert agent_retire.ERR_NOT_FOUND == serve_rpc.ERR_NOT_FOUND
    assert agent_retire.ERR_CONFLICT == serve_rpc.ERR_CONFLICT
