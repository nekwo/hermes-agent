"""Mission Control queue, "Owed when the kind is known" (2026-09-03, w10/hermes).

``_publish_persona_chat_send_refused_event`` (persona_commands.py) existed for
one refusal only — ``chat_busy`` — and its own docstring named the gap: the
three OTHER pre-lease guard refusals a ``mission.chat.message`` send can hit
before it ever reaches the chat-root lease (``unknown_chat_session``,
``foreign_chat_session``, ``retired_persona_instance``) wrote no durable
trace, exactly the defect class the 2026-08-09 investigation could not close
for the ``chat_busy`` case before this event existed at all — an operator
send refused on the way to the lease is otherwise unrecoverable AND
undiagnosable, because every durable write in the lane lives inside
``_mission_chat_commit_turn``, under a lease none of these guards ever take.

This file pins that the three guards now route through the same event, one
test per kind, mirroring the evidence shape ``test_chat_lease_finalization_tail
.test_a_send_refused_by_a_busy_root_records_a_durable_event`` already pins for
``chat_busy``: exactly one ``persona_chat.send_refused`` row, the right
``error_kind``, and the operator's message text nowhere in it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_runtime.events import EventLog
from agent_runtime.persona_assignments import PersonaInstanceStore


pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

OPERATOR_TEXT = "here is the answer you asked for"


def _canonical_db():
    """A transcript double the ``unknown``/``foreign`` guards will actually run
    against — see ``is_canonical_session_persistence``'s docstring: an unmarked
    double is silently NOT canonical, and every guard in this file is a no-op
    against one."""

    from tests.agent_runtime.test_persona_assignments import _TranscriptDB

    class _CanonicalTranscriptDB(_TranscriptDB):
        __hermes_canonical_session_persistence__ = True

    return _CanonicalTranscriptDB()


def _chat_lane(monkeypatch, db):
    from hermes_cli import harness
    from tests.agent_runtime.test_persona_assignments import _assignment_config

    class _ProviderSpy:
        def __init__(self, *args, **kwargs):
            pass

        def mission_chat_reply(self, persona, message, **kwargs):
            return SimpleNamespace(
                final_response="delivered reply",
                input_tokens=1,
                output_tokens=2,
                total_tokens=3,
                raw={},
            )

    monkeypatch.setattr(harness, "load_agent_runtime_config", _assignment_config)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)
    monkeypatch.setattr(harness, "GPTPersonaRuntime", _ProviderSpy)
    return harness


def _owned_root(db, *, persona_id="dev", display_name="Dev"):
    instance = PersonaInstanceStore().create_operator_chat(
        persona_id=persona_id, display_name=display_name
    )
    db.create_session(
        instance.session_id,
        "agent_runtime_persona_chat",
        model_config=json.dumps(
            {
                "source": "agent_runtime_persona_chat",
                "persona_instance_id": instance.id,
            }
        ),
    )
    return instance


def _send_args(*, persona_id, persona_instance_id, session_id, client_message_id):
    return SimpleNamespace(
        persona_id=persona_id,
        persona_instance_id=persona_instance_id,
        session_id=session_id,
        task_id=None,
        goal_id=None,
        message=OPERATOR_TEXT,
        surface_prompt="",
        intent_hint="chat",
        requested_by="test",
        client_message_id=client_message_id,
        stream=False,
        max_seconds=5.0,
        json=True,
    )


def _refused_rows():
    return [e for e in EventLog().tail(20) if e.type == "persona_chat.send_refused"]


def _assert_one_clean_refusal(*, error_kind: str, client_message_id: str):
    rows = _refused_rows()
    assert len(rows) == 1, (
        f"expected exactly one persona_chat.send_refused row, got {len(rows)}: "
        f"{[r.payload for r in rows]}"
    )
    payload = rows[0].payload
    assert payload["error_kind"] == error_kind
    assert payload["client_message_id"] == client_message_id
    wire = json.dumps(payload)
    assert OPERATOR_TEXT not in wire, (
        f"the operator's message text leaked into the refusal event: {wire}"
    )
    return payload


# --------------------------------------------------------------------------- #
# unknown_chat_session                                                        #
# --------------------------------------------------------------------------- #


def test_unknown_chat_session_send_refusal_is_durably_recorded(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """An explicit session id that names no row in the canonical store."""

    db = _canonical_db()
    harness = _chat_lane(monkeypatch, db)

    code = harness._cmd_mission_chat_message(
        _send_args(
            persona_id="dev",
            persona_instance_id=None,
            session_id="persona_chat_personainst_dev_never_minted",
            client_message_id="cm-unknown-1",
        )
    )
    capsys.readouterr()

    assert code == 2
    payload = _assert_one_clean_refusal(
        error_kind="unknown_chat_session", client_message_id="cm-unknown-1"
    )
    assert payload["root_chat_session_id"] == "persona_chat_personainst_dev_never_minted"


# --------------------------------------------------------------------------- #
# foreign_chat_session                                                        #
# --------------------------------------------------------------------------- #


def test_foreign_chat_session_send_refusal_is_durably_recorded(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """A live root that exists, owned by a persona instance the caller is not."""

    db = _canonical_db()
    harness = _chat_lane(monkeypatch, db)
    owner = _owned_root(db, persona_id="dev", display_name="Dev")

    code = harness._cmd_mission_chat_message(
        _send_args(
            persona_id="qa",
            persona_instance_id=None,
            session_id=owner.session_id,
            client_message_id="cm-foreign-1",
        )
    )
    capsys.readouterr()

    assert code == 2
    payload = _assert_one_clean_refusal(
        error_kind="foreign_chat_session", client_message_id="cm-foreign-1"
    )
    assert payload["root_chat_session_id"] == owner.session_id


# --------------------------------------------------------------------------- #
# retired_persona_instance                                                    #
# --------------------------------------------------------------------------- #


def test_retired_persona_instance_send_refusal_is_durably_recorded(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """A dispatch (no explicit session id) targeting a retired placement.

    Hits the pre-mint gate (`_mission_chat_retired_target_refusal`), the FIRST
    of the two retired-target arms in the no-session mint lane — the second
    (the mint's own `RetiredPersonaInstanceError` catch, defense against a
    `retire` landing mid-mint) is the same typed refusal one race narrower and
    shares this call site's coverage.
    """

    db = _canonical_db()
    harness = _chat_lane(monkeypatch, db)
    store = PersonaInstanceStore()
    instance = store.add_instance(persona_id="dev", placement_id="dev_agent_retired")
    db.create_session(
        instance.session_id,
        "agent_runtime_persona_chat",
        model_config=json.dumps(
            {
                "source": "agent_runtime_persona_chat",
                "persona_instance_id": instance.id,
            }
        ),
    )
    store.retire(instance.id, reason="placement deleted")
    assert instance.id not in {row.id for row in store.list_all()}

    code = harness._cmd_mission_chat_message(
        _send_args(
            persona_id="dev",
            persona_instance_id=instance.id,
            session_id=None,
            client_message_id="cm-retired-1",
        )
    )
    capsys.readouterr()

    assert code == 2
    _assert_one_clean_refusal(
        error_kind="retired_persona_instance", client_message_id="cm-retired-1"
    )


# --------------------------------------------------------------------------- #
# the registered contract, once for the family                                #
# --------------------------------------------------------------------------- #


def test_all_three_guard_refusals_satisfy_the_registered_event_contract(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """One check standing in for three: the same emitter, the same contract,
    already pinned per-kind above by ``_assert_one_clean_refusal``. This just
    confirms ``EventLog.append`` did not silently drop any of them — every
    emitter call sits inside a ``try/except`` that swallows a validation
    failure, which is exactly the failure mode that would make the tests above
    pass on an empty ``EventLog``."""

    from agent_runtime.decision_contract_registry import validate_event_payload

    db = _canonical_db()
    harness = _chat_lane(monkeypatch, db)

    harness._cmd_mission_chat_message(
        _send_args(
            persona_id="dev",
            persona_instance_id=None,
            session_id="persona_chat_personainst_dev_never_minted",
            client_message_id="cm-contract-1",
        )
    )
    capsys.readouterr()

    rows = _refused_rows()
    assert rows
    assert validate_event_payload("persona_chat.send_refused", rows[0].payload) == ()
