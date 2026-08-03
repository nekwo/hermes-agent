"""A delivered result must not read as something the operator typed.

A dispatch delivery is forged into the SENDER's own chat session as a
``role="user"`` row. At rest that is byte-identical to a message the operator
wrote, so without a marker every consumer attributes the harness's work to the
human — the exact defect the relay lane already paid for once, one lane over.

These tests pin the whole chain end to end, because the two halves being
individually correct is precisely what the relay sender-attribution regression
proved is not enough: marker in, marker out, typed kind, typed facts.
"""

from __future__ import annotations

from agent_runtime import dispatch_delivery
from agent_runtime.operator_channels import _conversation_history_message
from agent_runtime.persona_chat_history import (
    PERSONA_HARNESS_DELIVERY_KIND,
    PERSONA_RELAYED_MESSAGE_KIND,
)
from agent_runtime.relay_policy import (
    build_harness_delivery_marker,
    build_relay_sender_marker,
    parse_harness_delivery_marker,
    parse_relay_sender_marker,
)


# ---------------------------------------------------------------------------
# the marker — one build/parse authority
# ---------------------------------------------------------------------------


def test_a_delivery_marker_round_trips_both_facts():
    marker = build_harness_delivery_marker("dispatch-abc123", True)

    origin = parse_harness_delivery_marker(marker)

    assert origin is not None
    assert origin.dispatch_id == "dispatch-abc123"
    assert origin.notify_operator is True


def test_an_unflagged_delivery_is_still_a_delivery():
    """The kind is load-bearing on its own; the flag only gates notification."""

    origin = parse_harness_delivery_marker(
        build_harness_delivery_marker("dispatch-abc123", False)
    )

    assert origin is not None
    assert origin.notify_operator is False


def test_a_corrupt_flag_is_read_as_not_flagged():
    """Notifying on an unreadable marker would manufacture operator interrupts."""

    assert parse_harness_delivery_marker("harness_delivery:d1:").notify_operator is False
    assert parse_harness_delivery_marker("harness_delivery:d1").notify_operator is False
    assert parse_harness_delivery_marker("harness_delivery:d1:yes").notify_operator is False


def test_the_two_marker_vocabularies_never_answer_for_each_other():
    """A delivery is not a relay: no agent sent it and none may be named."""

    delivery = build_harness_delivery_marker("dispatch-abc123", True)
    relay = build_relay_sender_marker("dev", "personainst_dev")

    assert parse_relay_sender_marker(delivery) is None
    assert parse_harness_delivery_marker(relay) is None
    # And neither claims an ordinary finish_reason.
    for value in (None, "", "stop", "pre_trace_ack", 17):
        assert parse_harness_delivery_marker(value) is None


# ---------------------------------------------------------------------------
# requested_by provenance — the road from the forge to the handler
# ---------------------------------------------------------------------------


def test_requested_by_carries_the_dispatch_identity_to_the_handler():
    value = dispatch_delivery.delivery_requested_by("dispatch-abc123", notify_operator=True)

    assert value.startswith(dispatch_delivery.DELIVERY_REQUESTED_BY)
    origin = dispatch_delivery.parse_delivery_requested_by(value)
    assert origin.dispatch_id == "dispatch-abc123"
    assert origin.notify_operator is True


def test_a_bare_harness_delivery_provenance_still_reads_as_a_delivery():
    """Rows a pre-attribution build stamped are deliveries and must render so.

    They simply carry no dispatch id and no flag — an honest partial, never a
    fall-through into "the operator typed this"."""

    origin = dispatch_delivery.parse_delivery_requested_by("harness-delivery")

    assert origin is not None
    assert origin.dispatch_id is None
    assert origin.notify_operator is False


def test_every_other_provenance_is_left_alone():
    for value in (None, "", "cli", "operator", "coordinator:neko", "agent:sess-1", 3):
        assert dispatch_delivery.parse_delivery_requested_by(value) is None


# ---------------------------------------------------------------------------
# the handler seam — the one site that types the persisted row
# ---------------------------------------------------------------------------


def _marker_for(requested_by):
    from hermes_cli import harness

    class _Store:
        def list_all(self):
            return []

    return harness._resolve_relay_sender_marker(
        requested_by, instance_store=_Store(), relay_chain_in=()
    )


def test_the_handler_types_a_delivery_row_with_the_delivery_marker():
    marker = _marker_for(
        dispatch_delivery.delivery_requested_by("dispatch-abc123", notify_operator=True)
    )

    origin = parse_harness_delivery_marker(marker)
    assert origin.dispatch_id == "dispatch-abc123"
    assert origin.notify_operator is True


def test_the_handler_still_leaves_operator_sends_unmarked():
    """Byte-identical persistence for every send that is not machine-origin."""

    for value in (None, "", "cli", "operator", "coordinator:neko"):
        assert _marker_for(value) is None


# ---------------------------------------------------------------------------
# the read side — finish_reason marker becomes a typed history row
# ---------------------------------------------------------------------------


def test_the_read_side_types_a_delivery_row_and_leaves_the_others_alone():
    from agent_runtime.persona_chat_history import _safe_recent_messages
    from tests.agent_runtime.test_persona_chat_history_curation import FakeSessionDB

    db = FakeSessionDB(
        [
            {
                "id": "m1",
                "role": "user",
                "content": "[BACKGROUND DISPATCH COMPLETE — dispatch-abc123]",
                "finish_reason": build_harness_delivery_marker("dispatch-abc123", True),
                "platform_message_id": "dispatch-delivery-dispatch-abc123",
                "created_at": "2026-08-03T00:00:00Z",
            },
            {
                "id": "m2",
                "role": "user",
                "content": "From Neko: status?",
                "finish_reason": build_relay_sender_marker("neko", "personainst_neko"),
                "platform_message_id": "cm-relay-1",
                "created_at": "2026-08-03T00:01:00Z",
            },
            {
                "id": "m3",
                "role": "user",
                "content": "Operator: ping",
                "platform_message_id": "cm-op-1",
                "created_at": "2026-08-03T00:02:00Z",
            },
        ]
    )

    rows, status = _safe_recent_messages(
        db, session_id="persona_chat_personainst_qa_abc123def456"
    )

    assert status == "safe"
    delivered = [row for row in rows if row.get("kind") == PERSONA_HARNESS_DELIVERY_KIND]
    assert len(delivered) == 1
    assert delivered[0]["role"] == "operator"
    assert delivered[0]["delivery_dispatch_id"] == "dispatch-abc123"
    assert delivered[0]["delivery_notify_operator"] is True
    # A delivery is NOT a relay and must not acquire a sending agent.
    assert "relay_sender_persona_id" not in delivered[0]

    relayed = [row for row in rows if row.get("kind") == PERSONA_RELAYED_MESSAGE_KIND]
    assert len(relayed) == 1
    assert relayed[0]["relay_sender_persona_id"] == "neko"

    plain = [row for row in rows if row.get("client_message_id") == "cm-op-1"]
    assert len(plain) == 1
    assert "kind" not in plain[0]


# ---------------------------------------------------------------------------
# the projection — what the launcher actually receives
# ---------------------------------------------------------------------------


def _project(row):
    return _conversation_history_message(
        row,
        channel_id="chan-1",
        index=0,
        persona_id="neko_supervisor",
        persona_instance_id="personainst_neko",
        display_names={},
    )


def _history_row(**overrides):
    row = {
        "id": "row-1",
        "role": "operator",
        "text": "[BACKGROUND DISPATCH COMPLETE — dispatch-abc123]",
        "timestamp": "2026-08-03T10:00:00+00:00",
        "redaction_status": "safe",
    }
    row.update(overrides)
    return row


def test_a_delivery_projects_as_a_delivery_not_as_the_operator():
    message = _project(
        _history_row(
            kind=PERSONA_HARNESS_DELIVERY_KIND,
            delivery_dispatch_id="dispatch-abc123",
            delivery_notify_operator=True,
        )
    )

    assert message["kind"] == PERSONA_HARNESS_DELIVERY_KIND
    # The actor is the runtime, never the human and never an invented agent.
    assert message["actor_persona_id"] == "harness"
    assert message["actor_instance_id"] is None
    # role stays "operator" so a consumer that ignores the kind degrades to
    # exactly today's render rather than losing the row.
    assert message["role"] == "operator"
    assert message["delivery"] == {
        "dispatch_id": "dispatch-abc123",
        "notify_operator": True,
    }


def test_an_unflagged_delivery_says_so_explicitly():
    """Absence would be indistinguishable from an older producer's silence."""

    message = _project(
        _history_row(
            kind=PERSONA_HARNESS_DELIVERY_KIND,
            delivery_dispatch_id="dispatch-abc123",
            delivery_notify_operator=False,
        )
    )

    assert message["delivery"]["notify_operator"] is False


def test_an_ordinary_operator_message_gains_nothing():
    message = _project(_history_row())

    assert message["kind"] == "operator_message"
    assert message["actor_persona_id"] == "operator"
    assert "delivery" not in message


def test_a_relayed_message_keeps_its_own_attribution():
    """The delivery branch must not swallow the lane it sits beside."""

    message = _project(
        _history_row(
            kind=PERSONA_RELAYED_MESSAGE_KIND,
            relay_sender_persona_id="dev",
            relay_sender_instance_id="personainst_dev",
        )
    )

    assert message["kind"] == PERSONA_RELAYED_MESSAGE_KIND
    assert message["actor_persona_id"] == "dev"
    assert "delivery" not in message
