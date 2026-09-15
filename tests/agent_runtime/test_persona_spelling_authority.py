"""dispatch-2540634d5cf3 (2026-08-24) — one persona, one spelling authority.

Agent-to-agent reply delivery was deterministically refused for every
``profile:`` persona. The delivery forge
(``agent_runtime/dispatch_delivery.forge_delivery_turn``) hands
``_cmd_mission_chat_message`` the colon spelling off the instance record
(``chat_session_owner_persona`` -> ``"profile:alice"``) together with the
CORRECT ``persona_instance_id`` pin and the explicit ``session_id``. The
handler's ``foreign_chat_session`` fence then compared

    normalize_persona_or_template_id("profile:alice")  ->  "profile:alice"
    safe_assignment_token(owner_instance.persona_id)   ->  "profile_alice"

One persona, two normalizers, one predicate — never equal. Every delivery was
rejected, and because a guard verdict is a pure function of state that a retry
does not touch, the row then burned all eight attempts against the identical
refusal and settled as ``attempt_cap`` — so the Activity panel reported that the
attempts ran out and never that a guard had said no.

Four properties are pinned here, in the order they failed:

* ``personas_equal`` folds both sides through ONE authority.
* The fence accepts the forge's exact inputs, still refuses a genuinely foreign
  persona, and does NOT let a matching pin launder a different persona (see
  ``test_a_matching_pin_does_not_launder_a_different_persona`` for why).
* A deterministic forge rejection is terminal on the FIRST attempt, with the
  real verdict left on ``delivery_error``; a busy sender still refunds.
* ``harness mission-chat dispatch redeliver`` re-arms what that terminal drop
  left behind.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_runtime import dispatch_store
from agent_runtime.persona_assignments import (
    PersonaInstanceStore,
    personas_equal,
)


pytestmark = pytest.mark.usefixtures("persisted_persona_samples")


# --------------------------------------------------------------------------- #
# F1 — the comparison authority                                               #
# --------------------------------------------------------------------------- #


def test_personas_equal_truth_table(isolate_agent_runtime_root):
    # The incident's exact pair: colon form vs token form of ONE persona.
    assert personas_equal("profile:alice", "profile_alice") is True
    assert personas_equal("profile_alice", "profile:alice") is True
    # Identical spellings, either form.
    assert personas_equal("profile:alice", "profile:alice") is True
    assert personas_equal("dev", "dev") is True
    # Genuinely different personas stay different in every spelling pairing.
    assert personas_equal("profile:alice", "profile:bob") is False
    assert personas_equal("profile_alice", "profile:bob") is False
    assert personas_equal("dev", "qa") is False
    assert personas_equal("profile:alice", "dev") is False
    # Absence is never agreement — a guard reading "" as a match would accept
    # precisely the requests it exists to refuse.
    assert personas_equal("", "profile:alice") is False
    assert personas_equal(None, None) is False


def test_personas_equal_folds_an_instance_id_in_a_persona_slot(
    isolate_agent_runtime_root,
):
    """The launcher's fallback path spells the target ``--persona <personainst_…>``.

    That is the same one persona under a third spelling, and it is the shape
    convicted as the plausible mechanism behind the OPEN 2026-08-22
    new-chat-first-send record.
    """

    instance = PersonaInstanceStore().create_operator_chat(
        persona_id="profile:alice", display_name="Alice"
    )
    assert personas_equal(instance.id, "profile:alice") is True
    assert personas_equal(instance.id, "profile_alice") is True
    assert personas_equal(instance.id, "profile:bob") is False


# --------------------------------------------------------------------------- #
# the mission-chat fence                                                      #
# --------------------------------------------------------------------------- #


def _canonical_db():
    """``_TranscriptDB`` that DECLARES it backs the full session surface.

    ``is_canonical_session_persistence`` is the predicate behind the
    ``unknown_chat_session`` / ``foreign_chat_session`` guards, and it refuses to
    let them run against a store whose silence might only mean "I cannot
    answer". Without the marker every fence in this file is skipped and every
    assertion below passes VACUOUSLY — so the double declares it under its own
    name, which is exactly the seam the predicate exists to offer.
    """

    from tests.agent_runtime.test_persona_assignments import _TranscriptDB

    class _CanonicalTranscriptDB(_TranscriptDB):
        __hermes_canonical_session_persistence__ = True

    return _CanonicalTranscriptDB()


def test_the_double_actually_reaches_the_guards(isolate_agent_runtime_root):
    """Anti-vacuity: if this fails, every fence assertion below proves nothing."""

    from agent_runtime.chat_session_scope import is_canonical_session_persistence

    assert is_canonical_session_persistence(_canonical_db()) is True


def _chat_lane(monkeypatch, db):
    """The real ``_cmd_mission_chat_message`` on a stub provider + the test DB."""

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


def _owned_root(db, *, persona_id="profile:alice", display_name="Alice"):
    """An instance plus the canonical chat root it owns, as the store mints it."""

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


def _envelopes(capsys) -> list[dict]:
    """Every JSON object on stdout (``emit_json`` pretty-prints, so not line-wise)."""

    out = capsys.readouterr().out
    decoder = json.JSONDecoder()
    rows: list[dict] = []
    idx = 0
    while True:
        start = out.find("{", idx)
        if start < 0:
            return rows
        try:
            value, end = decoder.raw_decode(out, start)
        except ValueError:
            idx = start + 1
            continue
        if isinstance(value, dict):
            rows.append(value)
        idx = end


def _send_args(*, persona_id, persona_instance_id, session_id, client_message_id):
    return SimpleNamespace(
        persona_id=persona_id,
        persona_instance_id=persona_instance_id,
        session_id=session_id,
        task_id=None,
        goal_id=None,
        message="here is the answer you asked for",
        surface_prompt="",
        intent_hint="chat",
        requested_by="test",
        client_message_id=client_message_id,
        stream=False,
        max_seconds=5.0,
        json=True,
    )


def test_the_forge_inputs_are_accepted_regression_dispatch_2540634d5cf3(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """The live incident's EXACT inputs: colon persona + correct pin + explicit root.

    ``forge_delivery_turn`` passes ``persona_id=chat_session_owner_persona(root)``
    (colon form) with the owner's instance id and the root itself. Before the
    fix this envelope came back ``rejected / foreign_chat_session`` every single
    time, eight times per dispatch.
    """

    db = _canonical_db()
    harness = _chat_lane(monkeypatch, db)
    owner = _owned_root(db)
    assert owner.persona_id == "profile:alice"

    code = harness._cmd_mission_chat_message(
        _send_args(
            persona_id="profile:alice",  # colon form, straight off the instance record
            persona_instance_id=owner.id,  # the forge ALWAYS supplies the pin
            session_id=owner.session_id,
            client_message_id="cm-2540634d5cf3",
        )
    )

    payloads = _envelopes(capsys)
    assert payloads, "the handler emitted no envelope at all"
    assert not any(
        row.get("error_kind") == "foreign_chat_session" for row in payloads
    ), f"the delivery forge's own inputs were refused as foreign: {payloads[-1]}"
    assert code == 0


def test_a_plain_persona_id_is_still_accepted(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """The sends that DID work must not regress.

    A plain persona id (``dev``) is the case where the two normalizers happened
    to agree — ``normalize_persona_or_template_id`` and ``safe_assignment_token``
    both answer ``"dev"`` — which is the entire reason this defect looked like a
    delivery-lane problem rather than a spelling one: only the ``profile:``
    channel has a character the token fold rewrites.
    """

    db = _canonical_db()
    harness = _chat_lane(monkeypatch, db)
    owner = _owned_root(db, persona_id="dev", display_name="Dev")
    assert owner.persona_id == "dev"

    code = harness._cmd_mission_chat_message(
        _send_args(
            persona_id="dev",
            persona_instance_id=owner.id,
            session_id=owner.session_id,
            client_message_id="cm-plain-persona",
        )
    )

    payloads = _envelopes(capsys)
    assert not any(row.get("error_kind") == "foreign_chat_session" for row in payloads)
    assert code == 0


def test_a_genuinely_foreign_persona_without_a_pin_is_still_rejected(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """The protection survives the fix: a different persona is still foreign."""

    db = _canonical_db()
    harness = _chat_lane(monkeypatch, db)
    owner = _owned_root(db)

    code = harness._cmd_mission_chat_message(
        _send_args(
            persona_id="profile:bob",
            persona_instance_id=None,
            session_id=owner.session_id,
            client_message_id="cm-foreign-no-pin",
        )
    )

    assert code == 2
    payload = _envelopes(capsys)[-1]
    assert payload["ok"] is False
    assert payload["error_kind"] == "foreign_chat_session"


def test_a_matching_pin_does_not_launder_a_different_persona(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """The pin proves OWNERSHIP; it does not decide whose brain runs.

    The instruction for this task proposed an unconditional pin short-circuit:
    if the caller's ``persona_instance_id`` equals the chat owner, ownership is
    proven and the persona leg may not veto. It is right about ownership and
    wrong about the consequence, because the persona the fence sees is not a
    label — it is the value that ALREADY selected this turn's persona object,
    model, provider and profile, sixty lines earlier
    (``_resolve_mission_chat_persona_id`` -> ``_persona_by_id``). The fence is
    the last place that reads it.

    So ``--persona profile:bob --persona-instance-id <alice's instance>
    --session-id <alice's root>`` under an unconditional short-circuit would be
    accepted, and would then run BOB's persona — bob's model, bob's profile,
    bob's tools — as a turn inside alice's thread, with ``persona_instance_id``
    reassigned to alice's row for the bookkeeping. That is a worse outcome than
    the refusal, and it is a NEW hole rather than a restored one: the spelling
    bug never made this reachable.

    The pin therefore overrides the persona leg only where the persona leg has
    nothing to say — an owner row whose persona is missing or unreadable, pinned
    directly below. Everything the live incident needed is delivered by
    ``personas_equal`` alone, which is the actual defect.
    """

    db = _canonical_db()
    harness = _chat_lane(monkeypatch, db)
    owner = _owned_root(db)

    code = harness._cmd_mission_chat_message(
        _send_args(
            persona_id="profile:bob",  # a genuinely different persona…
            persona_instance_id=owner.id,  # …with a pin that DOES own the root
            session_id=owner.session_id,
            client_message_id="cm-pin-launder",
        )
    )

    assert code == 2
    payload = _envelopes(capsys)[-1]
    assert payload["error_kind"] == "foreign_chat_session"


def test_a_matching_pin_carries_a_root_whose_owner_persona_is_unreadable(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """The pin short-circuit's actual scope: the persona leg is SILENT, not wrong.

    An owner row with no readable persona can never satisfy ``personas_equal``
    (absence is not agreement), so without the pin leg a proven-owned root would
    be permanently unreachable — including by the delivery forge, which is
    exactly the population this incident stranded.
    """

    db = _canonical_db()
    harness = _chat_lane(monkeypatch, db)
    owner = _owned_root(db)

    store = PersonaInstanceStore()
    real_get = store.__class__.get

    def _personaless_get(self, instance_id):
        row = real_get(self, instance_id)
        if row.id == owner.id:
            return SimpleNamespace(
                **{**row.__dict__, "persona_id": ""}
            ) if hasattr(row, "__dict__") else row
        return row

    monkeypatch.setattr(PersonaInstanceStore, "get", _personaless_get)

    code = harness._cmd_mission_chat_message(
        _send_args(
            persona_id="profile:alice",
            persona_instance_id=owner.id,
            session_id=owner.session_id,
            client_message_id="cm-owner-persona-unreadable",
        )
    )

    payloads = _envelopes(capsys)
    assert not any(
        row.get("error_kind") == "foreign_chat_session" for row in payloads
    ), f"a proven-owned root with an unreadable owner persona was refused: {payloads[-1]}"
    assert code == 0


# --------------------------------------------------------------------------- #
# F2 — the sibling guards                                                     #
# --------------------------------------------------------------------------- #


def test_open_chat_accepts_the_colon_token_cross(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """``persona instance open-chat``, same two-normalizer shape, same fix."""

    db = _canonical_db()
    harness = _chat_lane(monkeypatch, db)
    owner = _owned_root(db)

    code = harness._cmd_persona_instance_open_chat(
        SimpleNamespace(
            persona_id="profile:alice",  # colon form vs the stored token fold
            persona_instance_id=owner.id,
            session_id=owner.session_id,
            add_instance=False,
            kill_active=False,
            json=True,
        )
    )

    payloads = _envelopes(capsys)
    assert not any(
        row.get("error_kind") == "foreign_chat_session" for row in payloads
    ), f"open-chat refused the owner of its own root: {payloads[-1] if payloads else None}"
    assert code == 0


def test_open_chat_still_refuses_a_foreign_root(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    db = _canonical_db()
    harness = _chat_lane(monkeypatch, db)
    owner = _owned_root(db)
    _owned_root(db, persona_id="profile:bob", display_name="Bob")

    code = harness._cmd_persona_instance_open_chat(
        SimpleNamespace(
            persona_id="profile:bob",
            persona_instance_id=None,
            session_id=owner.session_id,
            add_instance=False,
            kill_active=False,
            json=True,
        )
    )

    assert code == 2
    assert _envelopes(capsys)[-1]["error_kind"] == "foreign_chat_session"


def test_open_new_chat_instance_mismatch_accepts_the_colon_token_cross(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """Fourth site: ``persona_instance_mismatch`` compared the STORED persona raw.

    A spelling difference is not an instance belonging to somebody else, and
    this fence answers the strongest refusal in the family — "that instance is
    not yours" — on the strength of a colon.
    """

    db = _canonical_db()
    harness = _chat_lane(monkeypatch, db)
    owner = _owned_root(db)

    harness._cmd_persona_instance_open_new_chat(
        SimpleNamespace(
            persona_instance_id=owner.id,
            session_id=None,
            add_instance=False,
            kill_active=False,
            json=True,
        ),
        persona_id="profile_alice",  # token form vs the stored colon form
        coordinator_scope=None,
    )

    payloads = _envelopes(capsys)
    assert payloads, "the handler emitted no envelope at all"
    assert not any(
        row.get("error_kind") == "persona_instance_mismatch" for row in payloads
    ), f"a spelling difference was reported as a foreign instance: {payloads[-1]}"


def test_open_new_chat_still_refuses_a_genuinely_foreign_instance(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    db = _canonical_db()
    harness = _chat_lane(monkeypatch, db)
    foreign = _owned_root(db, persona_id="profile:bob", display_name="Bob")

    code = harness._cmd_persona_instance_open_new_chat(
        SimpleNamespace(
            persona_instance_id=foreign.id,
            session_id=None,
            add_instance=False,
            kill_active=False,
            json=True,
        ),
        persona_id="profile:alice",
        coordinator_scope=None,
    )

    assert code == 2
    assert _envelopes(capsys)[-1]["error_kind"] == "persona_instance_mismatch"


def test_chat_delete_accepts_the_colon_token_cross(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """``persona chat delete``, third site of the shape."""

    db = _canonical_db()
    harness = _chat_lane(monkeypatch, db)
    owner = _owned_root(db)
    db.append_message(owner.session_id, "user", "hello")

    code = harness._cmd_persona_chat_delete(
        SimpleNamespace(
            session_id=owner.session_id,
            persona_id="profile_alice",  # token form vs the stored colon form
            persona_instance_id=owner.id,
            requested_by="test",
            json=True,
        )
    )

    payloads = _envelopes(capsys)
    assert not any(
        row.get("error_kind") == "foreign_chat_session" for row in payloads
    ), f"delete refused the owner of its own root: {payloads[-1] if payloads else None}"
    assert code == 0


def test_chat_delete_still_refuses_a_foreign_instance(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    db = _canonical_db()
    harness = _chat_lane(monkeypatch, db)
    owner = _owned_root(db)
    foreign = _owned_root(db, persona_id="profile:bob", display_name="Bob")
    db.append_message(owner.session_id, "user", "must survive")

    code = harness._cmd_persona_chat_delete(
        SimpleNamespace(
            session_id=owner.session_id,
            persona_id="profile:bob",
            persona_instance_id=foreign.id,
            requested_by="test",
            json=True,
        )
    )

    assert code == 2
    assert _envelopes(capsys)[-1]["error_kind"] == "foreign_chat_session"
    assert db.get_session(owner.session_id) is not None


# --------------------------------------------------------------------------- #
# F4 — cap hygiene                                                            #
# --------------------------------------------------------------------------- #


def _completed_dispatch(dispatch_id="dispatch-cap-hygiene", root="persona_chat_sender"):
    dispatch_store.record_dispatch(
        dispatch_id=dispatch_id,
        sender_session_id=root,
        sender_persona_id="profile:alice",
        target_persona="profile:bob",
        target_instance_id="personainst_profile_bob",
        title="ask",
        ask="do the thing",
        notify_operator=False,
    )
    dispatch_store.record_completion(
        dispatch_id=dispatch_id, state="completed", reply="the answer"
    )
    return dispatch_id


def _drain_with(monkeypatch, forge_payload, *, root="persona_chat_sender"):
    from agent_runtime import dispatch_delivery

    monkeypatch.setattr(
        dispatch_delivery, "_sender_persona", lambda _root: ("profile:alice", "personainst_profile_alice")
    )
    monkeypatch.setattr(dispatch_delivery, "_sender_is_idle", lambda _root: True)

    def _forge(**kwargs):
        return False, dict(forge_payload)

    return dispatch_delivery.drain_once(forge=_forge)


def test_a_deterministic_forge_rejection_is_terminal_on_the_first_attempt(
    monkeypatch, isolate_agent_runtime_root
):
    """One attempt, not eight — and the panel reads the VERDICT, not ``attempt_cap``."""

    dispatch_id = _completed_dispatch()

    tally = _drain_with(
        monkeypatch, {"ok": False, "error_kind": "foreign_chat_session"}
    )

    assert tally["dropped"] == 1
    row = dispatch_store.get_dispatch(dispatch_id)
    assert row["delivery_state"] == dispatch_store.DELIVERY_DROPPED
    # One attempt was burned by the claim, and only one.
    assert row["delivery_attempts"] == 1
    assert row["delivery_error"] == "forge_rejected:foreign_chat_session"
    assert dispatch_store.DROP_REASON_ATTEMPT_CAP not in row["delivery_error"]
    # The reply is still durable — a terminal drop abandons the DELIVERY, never
    # the answer, which is what makes ``redeliver`` meaningful.
    assert row["result"]["reply"] == "the answer"


def test_a_busy_sender_still_refunds_its_attempt(
    monkeypatch, isolate_agent_runtime_root
):
    """Transient kinds keep the cap/refund semantics untouched."""

    dispatch_id = _completed_dispatch(dispatch_id="dispatch-busy-refund")

    tally = _drain_with(monkeypatch, {"ok": False, "error_kind": "chat_busy"})

    assert tally["busy"] == 1
    assert tally["dropped"] == 0
    row = dispatch_store.get_dispatch(dispatch_id)
    assert row["delivery_state"] == dispatch_store.DELIVERY_PENDING
    assert row["delivery_attempts"] == 0  # claimed +1, refunded -1
    assert not row["delivery_error"]


def test_an_unclassified_forge_failure_still_walks_the_cap(
    monkeypatch, isolate_agent_runtime_root
):
    """Only the guard-refusal class is fast-failed; everything else is transient."""

    dispatch_id = _completed_dispatch(dispatch_id="dispatch-unknown-failure")

    tally = _drain_with(
        monkeypatch, {"ok": False, "error_kind": "chat_turn_outcome_unknown"}
    )

    assert tally["failed"] == 1
    assert tally["dropped"] == 0
    row = dispatch_store.get_dispatch(dispatch_id)
    assert row["delivery_state"] == dispatch_store.DELIVERY_PENDING
    assert row["delivery_attempts"] == 1  # burned, not refunded, not terminal


# --------------------------------------------------------------------------- #
# F3 — the redeliver verb                                                     #
# --------------------------------------------------------------------------- #


def _redeliver(harness, dispatch_id, capsys):
    code = harness._cmd_mission_chat_dispatch_redeliver(
        SimpleNamespace(dispatch_id=dispatch_id, json=True)
    )
    return code, _envelopes(capsys)[-1]


def test_redeliver_rearms_a_dropped_row(capsys, isolate_agent_runtime_root):
    from hermes_cli import harness

    dispatch_id = _completed_dispatch(dispatch_id="dispatch-rearm-happy")
    dispatch_store.drop_delivery(
        dispatch_id, reason="forge_rejected:foreign_chat_session"
    )

    code, payload = _redeliver(harness, dispatch_id, capsys)

    assert code == 0
    assert payload["ok"] is True
    assert payload["outcome"] == "rearmed"
    assert payload["delivery_state"] == dispatch_store.DELIVERY_PENDING
    assert payload["delivery_attempts"] == 0
    assert payload["delivery_error"] is None
    # …and the row itself agrees, so the next drain pass will actually pick it up.
    row = dispatch_store.get_dispatch(dispatch_id)
    assert row["delivery_state"] == dispatch_store.DELIVERY_PENDING
    assert row["delivery_attempts"] == 0
    assert row["delivery_error"] == ""
    assert dispatch_store.pending_deliveries(limit=10)[0]["dispatch_id"] == dispatch_id
    # A queue repair verb never prints message bodies.
    assert "ask" not in payload and "reply" not in payload and "result" not in payload


def test_redeliver_refuses_a_row_that_is_not_dropped(capsys, isolate_agent_runtime_root):
    from hermes_cli import harness

    dispatch_id = _completed_dispatch(dispatch_id="dispatch-rearm-pending")

    code, payload = _redeliver(harness, dispatch_id, capsys)

    assert code == 2
    assert payload["ok"] is False
    assert payload["outcome"] == "not_dropped"
    assert payload["error_kind"] == "dispatch_not_dropped"
    assert payload["delivery_state"] == dispatch_store.DELIVERY_PENDING


def test_redeliver_refuses_an_already_delivered_row(capsys, isolate_agent_runtime_root):
    from hermes_cli import harness

    dispatch_id = _completed_dispatch(dispatch_id="dispatch-rearm-delivered")
    assert dispatch_store.mark_delivered(dispatch_id) is True

    code, payload = _redeliver(harness, dispatch_id, capsys)

    assert code == 2
    assert payload["outcome"] == "already_delivered"
    assert payload["error_kind"] == "dispatch_already_delivered"
    # Unchanged: re-arming would deliver a second copy of an answer already read.
    assert (
        dispatch_store.get_dispatch(dispatch_id)["delivery_state"]
        == dispatch_store.DELIVERY_DELIVERED
    )


def test_redeliver_refuses_an_unknown_dispatch(capsys, isolate_agent_runtime_root):
    from hermes_cli import harness

    code, payload = _redeliver(harness, "dispatch-does-not-exist", capsys)

    assert code == 2
    assert payload["outcome"] == "not_found"
    assert payload["error_kind"] == "dispatch_not_found"


def test_redeliver_is_reachable_from_the_cli(isolate_agent_runtime_root):
    import argparse

    from hermes_cli import harness

    parser = argparse.ArgumentParser()
    harness.build_parser(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(
        ["harness", "mission-chat", "dispatch", "redeliver", "dispatch-2540634d5cf3", "--json"]
    )

    assert args.func is harness._cmd_mission_chat_dispatch_redeliver
    assert args.dispatch_id == "dispatch-2540634d5cf3"
