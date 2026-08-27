"""The create receipt's state vocabulary, and the ``placed`` migration (D4).

``agent_create_reservations`` is a MIGRATION as of plan S4: one added state, and
three rules that decide what happens to receipts written before it existed. A
migration nobody tests is a vocabulary that works until the first upgrade, so
each rule gets a test that reads or writes a real receipt FILE — never a record
constructed in memory, because the whole question is what the loader does with
bytes that predate the loader.
"""

from __future__ import annotations

import json

import pytest

from agent_runtime import paths
from agent_runtime.agent_create import perform_agent_create
from agent_runtime.agent_create_reservations import (
    STATE_DONE,
    STATE_PLACED,
    AgentCreateReservationError,
    reserve_agent_create,
)
from tests.agent_runtime.office_seed import seed_workspace_record

WORKSPACE = "ws_agent_create_reservations"


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
    OfficeStore().ensure_surface(WORKSPACE, created_by="seed")


def _receipt_path(key: str):
    import hashlib

    return paths.agent_create_reservation_path(
        hashlib.sha256(key.encode("utf-8")).hexdigest()
    )


def _receipt(key: str) -> dict:
    return json.loads(_receipt_path(key).read_text(encoding="utf-8"))


def test_legacy_done_receipt(qa_persona, seeded_workspace, monkeypatch):
    """A pre-plan ``done`` receipt is REPLAYED, never re-entered at ``skills``.

    KILLING MUTATION (plan §C): treat ``done``-without-``skills`` as resumable
    and this reds — a second install runs.

    ANTI-VACUITY, and it is the whole design of this test. The probe is not "the
    reply looked right": ``run_skills_phase`` is replaced with a function that
    RAISES, so if the resume arm ever re-enters the phase for this receipt the
    test fails with that exception rather than on an assertion someone could
    weaken. A mutant that resumed would install; this asserts it never gets the
    chance.

    The receipt is written as BYTES with no ``skills`` key at all — the exact
    shape every receipt on every live runtime carries today — rather than
    through ``mark_done``, because the question is what the LOADER does with a
    file it did not write.
    """

    from agent_runtime import agent_create

    first = perform_agent_create(
        {
            "persona_id": "qa",
            "workspace_id": WORKSPACE,
            "position": [1.0, 2.0],
            "idempotency_key": "legacy-done",
            "placement_id": "qa_legacy_agent_2",
        }
    )
    assert first.refusal is None

    # Downgrade the receipt to the pre-plan shape: state ``done``, no ``skills``.
    raw = _receipt("legacy-done")
    raw.pop("skills", None)
    assert raw["state"] == STATE_DONE
    _receipt_path("legacy-done").write_text(
        json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8"
    )

    def _never(*args, **kwargs):
        raise AssertionError(
            "the skills phase was re-entered for a PRE-PLAN done receipt"
        )

    monkeypatch.setattr(agent_create, "run_skills_phase", _never)

    replay = perform_agent_create(
        {
            "persona_id": "qa",
            "workspace_id": WORKSPACE,
            "position": [1.0, 2.0],
            "idempotency_key": "legacy-done",
            "placement_id": "qa_legacy_agent_2",
            # Asking for a skill on the replay is the sharpest form of the
            # question: a resume that read the request rather than the receipt's
            # state would install here.
            "skills": ["harness-qa-verdict"],
        }
    )

    assert replay.refusal is None
    assert replay.result["idempotent_replay"] is True
    # The recorded reply, verbatim — and no override was written.
    from agent_runtime.persona_assignments import PersonaInstanceStore

    assert (
        PersonaInstanceStore().get(replay.result["persona_instance_id"]).skill_overrides
        is None
    )


def test_a_skill_less_create_writes_a_receipt_that_is_shaped_like_a_legacy_one(
    qa_persona, seeded_workspace
):
    """The two must be INDISTINGUISHABLE, and that is the migration rule.

    A create that asked for no skills has nothing to resume, exactly like a
    pre-plan one. If this code stamped ``skills: []`` on it, "no skills key"
    would stop meaning "pre-plan" and would start meaning "written by a serve
    older than some other change" — a second, undocumented reading of the same
    absence.
    """

    outcome = perform_agent_create(
        {
            "persona_id": "qa",
            "workspace_id": WORKSPACE,
            "position": [0.0, 0.0],
            "idempotency_key": "no-skills",
            "placement_id": "qa_noskills_agent_2",
        }
    )

    assert outcome.refusal is None
    assert "skills" not in _receipt("no-skills")


def test_an_explicitly_empty_request_is_recorded_as_empty_not_absent(
    qa_persona, seeded_workspace
):
    """``[]`` and absence are different values and the receipt keeps them apart.

    ANTI-VACUITY. Read back through the LOADER as well as off the bytes: the
    obvious wrong implementation is ``raw.get("skills") or None`` in ``_read``,
    which round-trips the file correctly and collapses ``[]`` to ``None`` in
    memory — invisible to a bytes-only probe.
    """

    outcome = perform_agent_create(
        {
            "persona_id": "qa",
            "workspace_id": WORKSPACE,
            "position": [0.0, 0.0],
            "idempotency_key": "empty-skills",
            "placement_id": "qa_emptyskills_agent_2",
            "skills": [],
        }
    )

    assert outcome.refusal is None
    assert _receipt("empty-skills")["skills"] == []
    with reserve_agent_create(
        idempotency_key="empty-skills", persona_id="qa", workspace_id=WORKSPACE
    ) as reservation:
        assert reservation.record.skills == []


def test_a_placed_receipt_records_the_requested_list(qa_persona, seeded_workspace):
    """``placed`` carries ``skills`` — the normalised request, on disk.

    Reached the only honest way: through a create whose skills phase REFUSED, so
    the receipt is the one the running code actually leaves behind rather than
    one this test hand-wrote into the state it wanted to assert.
    """

    outcome = perform_agent_create(
        {
            "persona_id": "qa",
            "workspace_id": WORKSPACE,
            "position": [0.0, 0.0],
            "idempotency_key": "placed-receipt",
            "placement_id": "qa_placedreceipt_agent_2",
            "skills": ["no-such-skill-anywhere"],
        }
    )

    assert outcome.refusal is not None
    raw = _receipt("placed-receipt")
    assert raw["state"] == STATE_PLACED
    assert raw["skills"] == ["no-such-skill-anywhere"]
    # The placement ack is recorded too, because a resume must hand back what
    # the FIRST attempt wrote rather than a second read of the store.
    assert raw["result"]["actor_key"]
    assert raw["result"]["persona_instance_id"] == "personainst_qa_placedreceipt_agent_2"


def test_an_unknown_state_is_still_reservation_corrupt(qa_persona, seeded_workspace):
    """The downgrade direction, stated as a test.

    ``placed`` was ADDED rather than replacing ``done``, so an OLD serve reading
    a NEW receipt fails ``_VALID_STATES`` and refuses loudly instead of
    re-minting a second agent. This drives the same code path with a state
    neither vintage knows, which is the only way to observe that arm from this
    side of the upgrade.
    """

    perform_agent_create(
        {
            "persona_id": "qa",
            "workspace_id": WORKSPACE,
            "position": [0.0, 0.0],
            "idempotency_key": "weird-state",
            "placement_id": "qa_weird_agent_2",
        }
    )
    raw = _receipt("weird-state")
    raw["state"] = "a_state_from_the_future"
    _receipt_path("weird-state").write_text(
        json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8"
    )

    with pytest.raises(AgentCreateReservationError) as caught:
        with reserve_agent_create(
            idempotency_key="weird-state", persona_id="qa", workspace_id=WORKSPACE
        ):
            pass
    assert caught.value.code == "reservation_corrupt"
