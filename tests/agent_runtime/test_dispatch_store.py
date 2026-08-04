"""The durable dispatch store's delivery contract.

Every test here pins a rule that, if relaxed, loses somebody's answer silently.
A detached dispatch is a PROMISE — "keep working, I'll bring you their reply" —
and the only thing standing between that promise and a quiet failure is this
store: a row that survives the process, a claim that cannot be double-taken, an
attempt counter that converges, and an owner identity that a recycled PID cannot
impersonate.
"""

from __future__ import annotations

import sqlite3

import pytest

from agent_runtime import dispatch_store
from agent_runtime.dispatch_store import (
    CLAIM_EXPIRY_SECONDS,
    DELIVERY_DELIVERED,
    DELIVERY_DROPPED,
    DELIVERY_PENDING,
    MAX_DELIVERY_ATTEMPTS,
    STATE_COMPLETED,
    STATE_RUNNING,
    STATE_UNKNOWN,
    claim_delivery,
    dispatch_db_path,
    drop_delivery,
    get_dispatch,
    list_dispatches,
    mark_delivered,
    mint_dispatch_id,
    pending_deliveries,
    record_completion,
    record_dispatch,
    release_delivery_claim,
    restore_undelivered_dispatches,
    running_dispatches,
)


@pytest.fixture
def store_home(tmp_path, monkeypatch):
    """An isolated background-work home for the store to land in."""

    home = tmp_path / "bg-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_HEAD_HOME", str(home))
    return home


def _dispatch(**overrides):
    payload = {
        "dispatch_id": overrides.pop("dispatch_id", mint_dispatch_id()),
        "sender_session_id": "persona_chat_personainst_neko_aaaaaaaaaaaa",
        "target_persona": "dev",
        "title": "Run the suite",
        "ask": "Please run the full launcher suite and report failures.",
    }
    payload.update(overrides)
    record_dispatch(**payload)
    return payload["dispatch_id"]


def test_round_trip_from_dispatch_to_delivered(store_home):
    dispatch_id = _dispatch(notify_operator=True)

    row = get_dispatch(dispatch_id)
    assert row["state"] == STATE_RUNNING
    assert row["delivery_state"] == DELIVERY_PENDING
    assert row["notify_operator"] is True
    # The row is written BEFORE the turn runs, so a crash one instruction in
    # still leaves something the boot sweep can settle and deliver.
    assert row["owner_pid"]
    assert [item["dispatch_id"] for item in running_dispatches()] == [dispatch_id]
    assert pending_deliveries() == []

    assert record_completion(
        dispatch_id, state=STATE_COMPLETED, reply="3 failures", target_session_id="persona_chat_dev_1"
    )
    assert running_dispatches() == []
    pending = pending_deliveries()
    assert [item["dispatch_id"] for item in pending] == [dispatch_id]
    assert pending[0]["result"]["reply"] == "3 failures"
    assert pending[0]["target_session_id"] == "persona_chat_dev_1"

    assert claim_delivery(dispatch_id, "claim-a")
    assert mark_delivered(dispatch_id)
    assert get_dispatch(dispatch_id)["delivery_state"] == DELIVERY_DELIVERED
    assert pending_deliveries() == []


def test_a_second_claimant_loses_until_the_claim_expires(store_home, monkeypatch):
    """Two consumers must never both forge the same delivery turn."""

    dispatch_id = _dispatch()
    record_completion(dispatch_id, state=STATE_COMPLETED, reply="done")

    assert claim_delivery(dispatch_id, "claim-a")
    assert not claim_delivery(dispatch_id, "claim-b")

    # Age the claim past the expiry: a claimant killed mid-delivery must not
    # strand the completion until the next reboot.
    path = dispatch_db_path()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE mission_chat_dispatches SET delivery_claimed_at = delivery_claimed_at - ?",
            (CLAIM_EXPIRY_SECONDS + 60,),
        )
    assert claim_delivery(dispatch_id, "claim-b")


def test_release_hands_the_row_back_without_delivering_it(store_home):
    """A BUSY sender is not a failure — the completion waits, it is not lost."""

    dispatch_id = _dispatch()
    record_completion(dispatch_id, state=STATE_COMPLETED, reply="done")
    assert claim_delivery(dispatch_id, "claim-a")
    release_delivery_claim(dispatch_id)

    assert get_dispatch(dispatch_id)["delivery_state"] == DELIVERY_PENDING
    assert claim_delivery(dispatch_id, "claim-b")


def test_attempts_converge_to_a_terminal_drop(store_home):
    """An unroutable row must stop replaying forever."""

    dispatch_id = _dispatch()
    record_completion(dispatch_id, state=STATE_COMPLETED, reply="done")

    for _ in range(MAX_DELIVERY_ATTEMPTS):
        assert claim_delivery(dispatch_id, "claim")
        release_delivery_claim(dispatch_id)

    # The next claim finds the cap spent and settles the row instead.
    assert not claim_delivery(dispatch_id, "claim")
    assert get_dispatch(dispatch_id)["delivery_state"] == DELIVERY_DROPPED
    assert pending_deliveries() == []


def test_an_explicit_drop_records_its_reason(store_home):
    dispatch_id = _dispatch()
    record_completion(dispatch_id, state=STATE_COMPLETED, reply="done")

    assert drop_delivery(dispatch_id, reason="sender_session_unresolvable")
    assert get_dispatch(dispatch_id)["delivery_state"] == DELIVERY_DROPPED
    # Terminal: a dropped row is not silently re-armed by a second drop.
    assert not drop_delivery(dispatch_id, reason="again")


def test_restore_settles_a_dispatch_whose_owner_is_gone(store_home, monkeypatch):
    """A dispatch nothing is left to run becomes a deliverable 'unknown'.

    Not silence: the sender is owed an answer even when the answer is "the
    process died and nobody knows".
    """

    dispatch_id = _dispatch()
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)

    assert restore_undelivered_dispatches()["restored"] == 1
    row = get_dispatch(dispatch_id)
    assert row["state"] == STATE_UNKNOWN
    assert row["delivery_state"] == DELIVERY_PENDING
    assert "unknown" in row["result"]["error"].lower()


def test_restore_leaves_a_live_owner_alone(store_home, monkeypatch):
    dispatch_id = _dispatch()
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: True)
    monkeypatch.setattr(
        "gateway.status.get_process_start_time",
        lambda pid: get_dispatch(dispatch_id)["owner_started_at"],
    )

    assert restore_undelivered_dispatches()["restored"] == 0
    assert get_dispatch(dispatch_id)["state"] == STATE_RUNNING


def test_restore_treats_an_unreadable_start_time_as_no_proof(store_home, monkeypatch):
    """An access-denied probe must not bury a running dispatch.

    ``None`` from the start-time probe is an ABSENCE of proof, not a mismatch —
    collapsing the two is how a psutil hiccup silently kills live work.
    """

    dispatch_id = _dispatch()
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: True)
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda pid: None)

    assert restore_undelivered_dispatches()["restored"] == 0
    assert get_dispatch(dispatch_id)["state"] == STATE_RUNNING


def test_listing_is_scoped_to_the_calling_session(store_home):
    """``agent_chat_dispatches`` must never show another agent's work."""

    mine = _dispatch(sender_session_id="persona_chat_personainst_neko_aaaaaaaaaaaa")
    _dispatch(sender_session_id="persona_chat_personainst_qa_bbbbbbbbbbbb")

    rows = list_dispatches(sender_session_id="persona_chat_personainst_neko_aaaaaaaaaaaa")
    assert [row["dispatch_id"] for row in rows] == [mine]
    # No caller identity lists NOTHING, not everything.
    assert list_dispatches(sender_session_id="") == []


def test_reads_never_create_the_database(store_home):
    """A projection asking 'what is running' must not create the store."""

    assert not dispatch_db_path().exists()
    assert running_dispatches() == []
    assert pending_deliveries() == []
    assert not dispatch_db_path().exists()


def test_the_store_follows_the_head_home_not_the_flipped_ambient_home(
    tmp_path, monkeypatch
):
    """The HERMES_HOME-flip trap, reproduced.

    A dispatch is made from INSIDE a persona turn, where
    ``persona_profile_context`` has flipped ``HERMES_HOME`` process-globally to
    the persona's profile. Writing through the ambient home would persist it
    into a database the serve drain and the operator's Activity HUD never open —
    the row would exist, nothing would ever read it, and the sender would wait
    forever for a delivery nobody was going to attempt.
    """

    head = tmp_path / "base"
    persona = tmp_path / "profiles" / "neko"
    head.mkdir(parents=True)
    persona.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HEAD_HOME", str(head))
    # The flip: ambient home now points at the persona profile.
    monkeypatch.setenv("HERMES_HOME", str(persona))

    dispatch_id = _dispatch()

    assert (head / "state.db").exists()
    assert not (persona / "state.db").exists()
    assert get_dispatch(dispatch_id) is not None


def test_every_mutation_emits_its_registered_event(store_home, monkeypatch):
    """Store rule: an event-less mutation is invisible to gated consumers."""

    seen = []
    monkeypatch.setattr(dispatch_store, "_emit", lambda kind, **kw: seen.append(kind))

    dispatch_id = _dispatch()
    record_completion(dispatch_id, state=STATE_COMPLETED, reply="done")
    claim_delivery(dispatch_id, "claim")
    mark_delivered(dispatch_id)

    assert seen == ["dispatch.recorded", "dispatch.completed", "dispatch.delivered"]


def test_the_event_types_are_registered_contracts():
    from agent_runtime.decision_contract_registry import allowed_event_types

    allowed = allowed_event_types()
    for kind in (
        "dispatch.recorded",
        "dispatch.completed",
        "dispatch.delivered",
        "dispatch.dropped",
    ):
        assert kind in allowed, kind


def test_a_real_completion_event_stays_inside_the_payload_cap(store_home):
    """An 8KB reply must not blow the 4096-byte EventLog payload limit.

    The event carries a SIZE, never the text; this is the test that keeps a
    future 'just add the reply to the payload' edit from silently turning every
    completion into a swallowed append failure.
    """

    from agent_runtime.events import EVENT_PAYLOAD_LIMIT_BYTES
    import json

    dispatch_id = _dispatch(ask="x" * 4000)
    captured = {}
    original = dispatch_store._emit

    def _capture(kind, **payload):
        captured[kind] = payload
        return original(kind, **payload)

    dispatch_store._emit = _capture
    try:
        record_completion(dispatch_id, state=STATE_COMPLETED, reply="y" * 8000)
    finally:
        dispatch_store._emit = original

    encoded = json.dumps(captured["dispatch.completed"]).encode("utf-8")
    assert len(encoded) < EVENT_PAYLOAD_LIMIT_BYTES


def test_the_owner_moves_to_the_process_actually_doing_the_work(store_home):
    """The row is recorded by the sender, then EXECUTED by a child process.

    "Is this dispatch still running?" is a question about the process running the
    turn — not the supervisor thread waiting on it, and not the sender that
    asked. Stamping the child is what keeps the orphan sweep coherent when the
    supervisor itself dies.
    """

    from agent_runtime.dispatch_store import set_dispatch_owner

    dispatch_id = _dispatch()
    assert set_dispatch_owner(dispatch_id, owner_pid=98765, owner_started_at=4242) is True

    row = get_dispatch(dispatch_id)
    assert row["owner_pid"] == 98765
    assert row["owner_started_at"] == 4242


def test_a_settled_row_is_never_re_owned(store_home):
    """Bookkeeping must not resurrect a dispatch that already finished."""

    from agent_runtime.dispatch_store import set_dispatch_owner

    dispatch_id = _dispatch()
    record_completion(dispatch_id, state=STATE_COMPLETED, reply="done")

    assert set_dispatch_owner(dispatch_id, owner_pid=1, owner_started_at=2) is False
    assert get_dispatch(dispatch_id)["state"] == STATE_COMPLETED


def test_a_refunded_release_does_not_walk_a_busy_row_toward_dropped(store_home):
    """Racing a live operator is not a failure, so it must not spend the budget.

    Without the refund, eight `chat_busy` races would march a perfectly
    deliverable completion to a terminal `dropped` having never once failed.
    """

    dispatch_id = _dispatch()
    record_completion(dispatch_id, state=STATE_COMPLETED, reply="done")

    for _ in range(MAX_DELIVERY_ATTEMPTS + 3):
        assert claim_delivery(dispatch_id, "claim")
        release_delivery_claim(dispatch_id, refund_attempt=True)

    row = get_dispatch(dispatch_id)
    assert row["delivery_state"] == DELIVERY_PENDING
    assert row["delivery_attempts"] == 0


def test_a_delivered_row_is_never_re_armed(store_home):
    """Once the sender has been told, a later write must not queue it again.

    The only thing standing between a re-armed `delivered` row and the sender
    receiving the same result twice is a replay cache that can rotate or be
    compressed away. So the invariant lives in the store, not in a cache.
    """

    dispatch_id = _dispatch()
    record_completion(dispatch_id, state=STATE_COMPLETED, reply="done")
    assert claim_delivery(dispatch_id, "claim")
    assert mark_delivered(dispatch_id)

    # A late write still corrects the OUTCOME, but does not re-queue delivery.
    assert record_completion(dispatch_id, state=STATE_COMPLETED, reply="corrected")
    row = get_dispatch(dispatch_id)
    assert row["result"]["reply"] == "corrected"
    assert row["delivery_state"] == DELIVERY_DELIVERED
    assert pending_deliveries() == []


def test_only_if_running_refuses_to_overwrite_a_settled_row(store_home):
    """The guessing writer must never outrank the observing one.

    The sweep INFERS `unknown` from a dead PID. The supervisor WATCHED the turn.
    When both write, the observation has to win, so the sweep's write is scoped
    to rows still in flight.
    """

    dispatch_id = _dispatch()
    record_completion(dispatch_id, state=STATE_COMPLETED, reply="the real answer")

    assert (
        record_completion(
            dispatch_id, state=STATE_UNKNOWN, error="guessed", only_if_running=True
        )
        is False
    )
    row = get_dispatch(dispatch_id)
    assert row["state"] == STATE_COMPLETED
    assert row["result"]["reply"] == "the real answer"

    # Unguarded, the supervisor's own correction still lands.
    assert record_completion(dispatch_id, state=STATE_COMPLETED, reply="corrected")
    assert get_dispatch(dispatch_id)["result"]["reply"] == "corrected"


def test_the_orphan_copy_points_at_the_thread_instead_of_inviting_rework(
    store_home, monkeypatch
):
    """After the subprocess move the dominant orphan is a recycled serve.

    The child very likely finished and wrote its reply into the target's own
    thread; only the bookkeeping row was orphaned. "Re-send if you still need
    it" invites duplicated work in exactly that case.
    """

    dispatch_id = _dispatch()
    record_completion(
        dispatch_id, state=STATE_COMPLETED, reply="x", target_session_id="persona_chat_dev_9"
    )
    # Re-open it as running with a dead owner, the shape a recycled serve leaves.
    import sqlite3

    with sqlite3.connect(dispatch_db_path()) as conn:
        conn.execute(
            "UPDATE mission_chat_dispatches SET state='running', delivery_state='pending' "
            "WHERE dispatch_id=?",
            (dispatch_id,),
        )
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)

    assert restore_undelivered_dispatches()["restored"] == 1
    error = get_dispatch(dispatch_id)["result"]["error"]
    assert "persona_chat_dev_9" in error
    assert "agent_chat_open" in error
    assert "Re-send if you still need it" not in error
