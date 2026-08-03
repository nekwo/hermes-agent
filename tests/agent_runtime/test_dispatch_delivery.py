"""The serve-hosted delivery drain — the half that keeps ``wait: false`` honest.

A detached dispatch promises the sender "I will bring you their answer". Every
rule here is what stands between that promise and a broken conversation: a
delivery spliced into a live turn breaks role alternation, a delivery retried
without dedup doubles the message, a delivery into an unresolvable session is a
result nobody will ever read, and a delivery that silently gives up is the
silence the whole lane exists to retire.
"""

from __future__ import annotations

import pytest

from agent_runtime import dispatch_delivery, dispatch_store
from agent_runtime.dispatch_delivery import (
    DELIVERY_REQUESTED_BY,
    delivery_client_message_id,
    drain_once,
    format_dispatch_delivery,
)
from agent_runtime.dispatch_store import (
    DELIVERY_DELIVERED,
    DELIVERY_DROPPED,
    DELIVERY_PENDING,
    STATE_COMPLETED,
    get_dispatch,
    mint_dispatch_id,
    record_completion,
    record_dispatch,
)

SENDER_ROOT = "persona_chat_personainst_neko_aaaaaaaaaaaa"


@pytest.fixture
def store_home(tmp_path, monkeypatch):
    home = tmp_path / "bg-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_HEAD_HOME", str(home))
    return home


@pytest.fixture
def resolvable_sender(monkeypatch):
    """The sender's chat root resolves to a real persona instance.

    This IS the positive-ownership proof a restored completion needs — the drain
    refuses to forge anything into a root it cannot resolve.
    """

    monkeypatch.setattr(
        dispatch_delivery,
        "_sender_persona",
        lambda root: ("neko_supervisor", "personainst_neko") if root == SENDER_ROOT else None,
    )


@pytest.fixture
def idle_sender(monkeypatch):
    monkeypatch.setattr(dispatch_delivery, "_sender_is_idle", lambda root: True)


class _Forge:
    """A forge stub that records the turn it was asked to run."""

    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.ok, {"ok": self.ok}


def _completed(**overrides):
    dispatch_id = mint_dispatch_id()
    record_dispatch(
        dispatch_id=dispatch_id,
        sender_session_id=overrides.pop("sender_session_id", SENDER_ROOT),
        target_persona="dev",
        title="Run the suite",
        ask="Run the full launcher suite and report failures.",
        notify_operator=overrides.pop("notify_operator", False),
    )
    record_completion(
        dispatch_id,
        state=STATE_COMPLETED,
        reply=overrides.pop("reply", "3 failures, all in the chat panel tests"),
        target_session_id="persona_chat_personainst_dev_bbbbbbbbbbbb",
    )
    return dispatch_id


def test_an_idle_sender_gets_the_completion_as_a_new_turn(
    store_home, resolvable_sender, idle_sender
):
    dispatch_id = _completed()
    forge = _Forge()

    tally = drain_once(forge=forge)

    assert tally["delivered"] == 1
    assert len(forge.calls) == 1
    call = forge.calls[0]
    assert call["root_session_id"] == SENDER_ROOT
    assert call["persona_id"] == "neko_supervisor"
    assert call["client_message_id"] == delivery_client_message_id(dispatch_id)
    assert get_dispatch(dispatch_id)["delivery_state"] == DELIVERY_DELIVERED


def test_the_forged_turn_carries_the_dispatch_identity_for_attribution(
    store_home, resolvable_sender, idle_sender
):
    """Without this the delivery persists as an unmarked operator row.

    The forge is the LAST place that knows which dispatch this settles and
    whether the agent flagged the operator — by the time a consumer reads the
    thread, the dispatch has left every live projection. So both facts travel
    with the turn or they are gone.
    """

    dispatch_id = _completed(notify_operator=True)
    forge = _Forge()

    drain_once(forge=forge)

    call = forge.calls[0]
    assert call["dispatch_id"] == dispatch_id
    assert call["notify_operator"] is True


def test_an_unflagged_dispatch_forges_an_unflagged_delivery(
    store_home, resolvable_sender, idle_sender
):
    dispatch_id = _completed(notify_operator=False)
    forge = _Forge()

    drain_once(forge=forge)

    assert forge.calls[0]["dispatch_id"] == dispatch_id
    assert forge.calls[0]["notify_operator"] is False


def test_a_busy_sender_requeues_instead_of_splicing(
    store_home, resolvable_sender, monkeypatch
):
    """Role alternation is the invariant: NEVER between a tool result and a reply."""

    dispatch_id = _completed()
    monkeypatch.setattr(dispatch_delivery, "_sender_is_idle", lambda root: False)
    forge = _Forge()

    tally = drain_once(forge=forge)

    assert tally == {"considered": 1, "delivered": 0, "busy": 1, "dropped": 0, "failed": 0}
    assert forge.calls == []
    row = get_dispatch(dispatch_id)
    # Still pending, still unclaimed — the next pass can take it, and no
    # attempt was burned for a moment that was simply wrong.
    assert row["delivery_state"] == DELIVERY_PENDING
    assert row["delivery_attempts"] == 0


def test_an_inflight_turn_record_alone_blocks_delivery(store_home, monkeypatch):
    """The journal and the lease are BOTH consulted — either one says busy.

    A turn whose executor died leaves the lease free while the journal still
    shows it in flight; trusting the lease alone would deliver into it.
    """

    monkeypatch.setattr(
        "agent_runtime.mission_chat_turns.mission_chat_turn_records",
        lambda *, session_id: [{"state": "executing"}],
    )
    assert dispatch_delivery._sender_is_idle(SENDER_ROOT) is False


def test_a_failed_forge_releases_the_claim_for_a_later_retry(
    store_home, resolvable_sender, idle_sender
):
    dispatch_id = _completed()
    forge = _Forge(ok=False)

    tally = drain_once(forge=forge)

    assert tally["failed"] == 1
    row = get_dispatch(dispatch_id)
    assert row["delivery_state"] == DELIVERY_PENDING
    # The attempt WAS counted (at claim time), which is what makes an input
    # that reliably kills the drain converge to `dropped` instead of looping.
    assert row["delivery_attempts"] == 1


def test_retrying_a_delivery_lands_one_turn_not_two(
    store_home, resolvable_sender, idle_sender
):
    """Dedup rides the client_message_id, which is derived, never re-minted."""

    dispatch_id = _completed()
    forge = _Forge(ok=False)
    drain_once(forge=forge)
    forge.ok = True
    drain_once(forge=forge)

    assert len({call["client_message_id"] for call in forge.calls}) == 1
    assert forge.calls[0]["client_message_id"] == f"dispatch-delivery-{dispatch_id}"


def test_an_unresolvable_sender_is_dropped_not_delivered_blindly(
    store_home, idle_sender, monkeypatch
):
    """#64484: absence of disproof is not ownership proof."""

    dispatch_id = _completed()
    monkeypatch.setattr(dispatch_delivery, "_sender_persona", lambda root: None)
    forge = _Forge()

    tally = drain_once(forge=forge)

    assert tally["dropped"] == 1
    assert forge.calls == []
    assert get_dispatch(dispatch_id)["delivery_state"] == DELIVERY_DROPPED


def test_a_pass_is_bounded_so_a_burst_cannot_monopolise_the_thread(
    store_home, resolvable_sender, idle_sender
):
    for _ in range(6):
        _completed()
    forge = _Forge()

    tally = drain_once(forge=forge, limit=2)

    assert tally["delivered"] == 2
    assert len(forge.calls) == 2


def test_the_delivered_message_stands_on_its_own(store_home):
    dispatch_id = _completed()
    row = get_dispatch(dispatch_id)

    text = format_dispatch_delivery(row)

    # Who, what was asked, what came back, and where the thread is — the sender
    # may be deep in unrelated context by now and remember none of it.
    assert dispatch_id in text
    assert "dev" in text
    assert "Run the full launcher suite" in text
    assert "3 failures" in text
    assert row["target_session_id"] in text
    assert "agent_chat_open" in text


def test_notify_operator_puts_the_instruction_in_the_message(store_home):
    dispatch_id = _completed(notify_operator=True)

    text = format_dispatch_delivery(get_dispatch(dispatch_id))

    assert "OPERATOR IS WAITING ON THIS" in text
    assert "Tell them the result" in text


def test_without_notify_operator_there_is_no_such_instruction(store_home):
    text = format_dispatch_delivery(get_dispatch(_completed()))

    assert "OPERATOR IS WAITING" not in text


def test_delivery_turns_are_stamped_with_their_own_provenance():
    """The launcher renders turns by origin; a delivery must not read as 'You'."""

    assert DELIVERY_REQUESTED_BY == "harness-delivery"


def test_a_completion_naming_no_chat_root_is_not_adopted():
    """Gateway/CLI completions belong to their own consumers, not to this drain."""

    assert dispatch_delivery._chat_root_of_completion({"session_key": "gateway-42"}) is None
    assert dispatch_delivery._chat_root_of_completion({}) is None
