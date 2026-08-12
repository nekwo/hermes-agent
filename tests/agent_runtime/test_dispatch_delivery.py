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


# --------------------------------------------------------------------------
# attempts must count real failures only
# --------------------------------------------------------------------------


def test_losing_a_race_with_a_live_operator_does_not_burn_an_attempt(
    store_home, resolvable_sender, idle_sender
):
    """`chat_busy` at forge time is the system working, not a delivery failing.

    The sender took its chat-root lease between the drain's idle probe and the
    forge. Nothing failed — the completion is perfectly deliverable and will be,
    moments later. Counting it would let eight unlucky races march a good
    completion to a terminal `dropped` with no failure anywhere in its history.
    """

    dispatch_id = _completed()

    class _Busy:
        def __call__(self, **kwargs):
            return False, {"ok": False, "error_kind": "chat_busy"}

    tally = drain_once(forge=_Busy())

    assert tally["busy"] == 1 and tally["failed"] == 0
    row = get_dispatch(dispatch_id)
    assert row["delivery_state"] == DELIVERY_PENDING
    assert row["delivery_attempts"] == 0


def test_a_genuine_failure_still_burns_its_attempt(store_home, resolvable_sender, idle_sender):
    """The cap has to keep converging for the failures it exists for."""

    dispatch_id = _completed()

    tally = drain_once(forge=_Forge(ok=False))

    assert tally["failed"] == 1
    assert get_dispatch(dispatch_id)["delivery_attempts"] == 1


# --------------------------------------------------------------------------
# the periodic orphan sweep
# --------------------------------------------------------------------------


def test_the_sweep_settles_orphans_without_waiting_for_a_reboot(store_home, monkeypatch):
    """Boot-only was a real gap: serve can stay up for days.

    A dispatch whose child died at 09:00 stayed `running` — in the HUD and in
    the sender's mental model — until the next restart. The sender is owed "the
    outcome is unknown" within a minute of it becoming true.
    """

    from agent_runtime.dispatch_store import STATE_UNKNOWN, mint_dispatch_id, record_dispatch

    dispatch_id = mint_dispatch_id()
    record_dispatch(
        dispatch_id=dispatch_id,
        sender_session_id=SENDER_ROOT,
        target_persona="dev",
        ask="run the suite",
    )
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)

    assert dispatch_delivery.sweep_orphaned_dispatches() == 1
    row = get_dispatch(dispatch_id)
    assert row["state"] == STATE_UNKNOWN
    # And it is now DELIVERABLE, not merely reclassified.
    assert row["delivery_state"] == DELIVERY_PENDING


def test_the_sweep_never_takes_the_drain_down(store_home, monkeypatch):
    monkeypatch.setattr(
        "agent_runtime.dispatch_store.restore_undelivered_dispatches",
        lambda: (_ for _ in ()).throw(RuntimeError("store gone")),
    )

    assert dispatch_delivery.sweep_orphaned_dispatches() == 0


# --------------------------------------------------------------------------
# drain liveness backs the capability promise
# --------------------------------------------------------------------------


def test_the_drain_reports_its_own_liveness(monkeypatch):
    """"serve is running" was only ever a PROXY for "a consumer exists".

    The drain starts best-effort, so a serve whose drain failed used to go on
    promising deliveries nothing would perform. The capability binding reads
    this instead.
    """

    import threading

    assert dispatch_delivery.delivery_drain_is_live() is False
    stop = threading.Event()
    thread = dispatch_delivery.start_delivery_drain(stop_event=stop, interval_seconds=0.05)
    try:
        assert dispatch_delivery.delivery_drain_is_live() is True
    finally:
        stop.set()
        thread.join(timeout=5)
    assert dispatch_delivery.delivery_drain_is_live() is False


# --------------------------------------------------------------------------
# the queue lane converges too
# --------------------------------------------------------------------------


def test_a_background_completion_that_cannot_be_delivered_stops_retrying(
    store_home, resolvable_sender, idle_sender, monkeypatch
):
    """No durable row means no attempt column, so the counter lives in the drain.

    Without it a persistently-failing event re-queues every five seconds
    forever — an invisible hot loop that also blocks the queue behind it. It
    ends LOUDLY instead, because a silently-abandoned completion is the exact
    failure class this lane exists to retire.
    """

    from tools.process_registry import process_registry

    evt = {"type": "completion", "session_id": SENDER_ROOT, "session_key": SENDER_ROOT}
    monkeypatch.setattr(
        process_registry,
        "drain_notifications",
        lambda **kw: [(evt, "the build finished")]
        if not process_registry.completion_queue.empty()
        else [],
    )
    dispatch_delivery._background_attempts.clear()

    seen = []
    for _ in range(dispatch_delivery.MAX_BACKGROUND_DELIVERY_ATTEMPTS + 1):
        process_registry.completion_queue.put(evt)
        seen.append(dispatch_delivery.drain_background_completions(forge=_Forge(ok=False)))

    # Every pass but the last retried; the last abandoned it, once.
    assert sum(pass_["failed"] for pass_ in seen) == dispatch_delivery.MAX_BACKGROUND_DELIVERY_ATTEMPTS
    assert seen[-1]["abandoned"] == 1
    dispatch_delivery._background_attempts.clear()


# --------------------------------------------------------------------------
# the forge, driven through the REAL mission-chat handler
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("persisted_persona_samples")
def test_forge_delivery_turn_lands_a_real_turn_and_dedupes_a_retry(
    monkeypatch, capsys, isolate_agent_runtime_root, tmp_path
):
    """Everything above stubs the forge; this drives the real one.

    Real handler, real transcript persistence, real turn journal, real chat-root
    lease — only the provider is stubbed. That matters because the two claims
    this lane rests on are claims about the HANDLER, not about the drain: that a
    delivery becomes an ordinary turn in the sender's own thread, and that a
    retried delivery converges on ONE turn because its client_message_id is
    derived from the dispatch rather than minted per attempt. A stubbed forge
    cannot show either.
    """

    from types import SimpleNamespace

    from hermes_cli import harness
    from tests.agent_runtime.test_persona_assignments import _assignment_config, _TranscriptDB

    monkeypatch.setenv("HERMES_HEAD_HOME", str(tmp_path))
    db = _TranscriptDB()
    calls = []

    class _ProviderSpy:
        def __init__(self, *args, **kwargs):
            pass

        def mission_chat_reply(self, persona, message, **kwargs):
            calls.append(message)
            return SimpleNamespace(
                final_response="told the operator",
                input_tokens=1,
                output_tokens=2,
                total_tokens=3,
                raw={},
            )

    monkeypatch.setattr(harness, "load_agent_runtime_config", _assignment_config)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)
    monkeypatch.setattr(harness, "GPTPersonaRuntime", _ProviderSpy)

    dispatch_id = "dispatch-realforge01"
    message = format_dispatch_delivery(
        {
            "dispatch_id": dispatch_id,
            "target_persona": "qa",
            "ask": "run the suite",
            "state": "completed",
            "result": {"reply": "3 failures"},
            "dispatched_at": 1_700_000_000.0,
            "completed_at": 1_700_000_060.0,
            "notify_operator": True,
        }
    )

    ok, payload = dispatch_delivery.forge_delivery_turn(
        root_session_id="persona_chat_personainst_dev",
        persona_id="dev",
        persona_instance_id="personainst_dev",
        message=message,
        client_message_id=delivery_client_message_id(dispatch_id),
    )
    capsys.readouterr()

    assert ok is True, payload
    # It is a REAL turn: the model saw the delivery block, and the reply was
    # persisted into the sender's own thread.
    assert "BACKGROUND DISPATCH COMPLETE" in calls[0]
    assert "OPERATOR IS WAITING ON THIS" in calls[0]

    # The retry. Same derived id ⇒ the chat lane's own replay dedup absorbs it,
    # so the sender never sees the same result twice.
    ok_again, replay = dispatch_delivery.forge_delivery_turn(
        root_session_id="persona_chat_personainst_dev",
        persona_id="dev",
        persona_instance_id="personainst_dev",
        message=message,
        client_message_id=delivery_client_message_id(dispatch_id),
    )
    capsys.readouterr()

    assert ok_again is True
    assert replay.get("idempotent_replay") is True
    # And the provider was NOT asked a second time: one delivery, one turn.
    assert len(calls) == 1


def test_a_restarted_drain_is_not_nulled_by_the_old_loops_teardown(store_home):
    """`delivery_drain_is_live()` gates EVERY dispatch, so a false negative
    refuses them all.

    The teardown used to clear the registration unconditionally, so an old
    loop's `finally` could null a drain that had already been restarted — after
    which the runtime reported no consumer forever and refused work it was
    perfectly able to deliver.
    """

    import threading

    first_stop = threading.Event()
    first = dispatch_delivery.start_delivery_drain(stop_event=first_stop, interval_seconds=0.05)
    second_stop = threading.Event()
    second = dispatch_delivery.start_delivery_drain(stop_event=second_stop, interval_seconds=0.05)
    try:
        # Retire the FIRST loop while the second is the registered drain.
        first_stop.set()
        first.join(timeout=5)
        assert dispatch_delivery.delivery_drain_is_live() is True
    finally:
        second_stop.set()
        second.join(timeout=5)
    assert dispatch_delivery.delivery_drain_is_live() is False


# --------------------------------------------------------------------------
# the delegation lane settles its OWN durable row
#
# `delegate_task` completions are persisted to `async_delegations` before they
# are ever queued, and that row carries the whole delivery protocol. The
# gateway, the interactive CLI and the TUI gateway have always driven it. Serve
# did not: it drained the in-memory queue, forged a real turn, and left the row
# `pending / 0 / None` forever.
#
# Live specimen, 2026-08-10: delegation `deleg_1f53b4be` was delivered into the
# operator's own thread 8 seconds after it completed — the journal record, the
# `messages` rows and a `persona_chat.projected` event all prove it — while its
# durable row reported `delivery_attempts: 0`. An entire incident investigation
# was routed by that row into concluding the drain had never run.
# --------------------------------------------------------------------------


@pytest.fixture
def durable_delegation(store_home):
    """A COMPLETED `delegate_task` row plus the event that was queued for it."""

    import time

    from tools import async_delegation

    delegation_id = "deleg_testspecimen"
    async_delegation._persist_dispatch(
        {
            "delegation_id": delegation_id,
            "session_key": SENDER_ROOT,
            "origin_ui_session_id": "",
            "parent_session_id": SENDER_ROOT,
            "dispatched_at": time.time(),
            "goal": "run a harmless background UI test",
            "role": "leaf",
        }
    )
    event = {
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "session_key": SENDER_ROOT,
        "origin_ui_session_id": "",
        "parent_session_id": SENDER_ROOT,
        "goal": "run a harmless background UI test",
        "status": "completed",
        "summary": "Background UI test completed successfully.",
        "completed_at": time.time(),
    }
    async_delegation._persist_completion(
        event, {"status": "completed", "summary": event["summary"]}
    )
    return event


def _queue_once(monkeypatch, event, text="the delegation finished"):
    """Hand the drain exactly one queued completion."""

    from tools.process_registry import process_registry

    # `completion_queue` is a process-global singleton that other tests in this
    # file deliberately leave loaded, so take ownership of it before asserting
    # on what this drain did or did not put back.
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()

    served: list[bool] = []

    def _drain(**_kwargs):
        if served:
            return []
        served.append(True)
        return [(event, text)]

    monkeypatch.setattr(process_registry, "drain_notifications", _drain)
    return process_registry


def test_a_delivered_delegation_is_acknowledged_on_its_own_durable_row(
    durable_delegation, resolvable_sender, idle_sender, monkeypatch
):
    """The row is the ledger, and a successful delivery has to reach it.

    Without this the durable record permanently misreports a delivery that
    demonstrably happened as one that was never attempted — the false negative
    that derailed the 2026-08-10 investigation.
    """

    from tools import async_delegation

    _queue_once(monkeypatch, durable_delegation)
    dispatch_delivery._background_attempts.clear()

    tally = dispatch_delivery.drain_background_completions(forge=_Forge(ok=True))

    assert tally["delivered"] == 1
    row = async_delegation.get_durable_delegation("deleg_testspecimen")
    assert row["delivery_state"] == "delivered"
    assert row["delivery_attempts"] == 1


def test_a_delivered_delegation_is_not_re_enqueued_by_the_next_boot_restore(
    durable_delegation, resolvable_sender, idle_sender, monkeypatch
):
    """Serve re-arms every completion it delivers, until the row is acked.

    ``restore_undelivered_completions`` selects exactly ``state != 'running'
    AND delivery_state = 'pending'``, so an unacked row is re-queued at every
    boot and re-forged — suppressed only by the chat lane's
    ``client_message_id`` replay dedup, and only for as long as the turn
    journal still holds that record.
    """

    import queue as _queue

    from tools.async_delegation import restore_undelivered_completions

    _queue_once(monkeypatch, durable_delegation)
    dispatch_delivery._background_attempts.clear()

    assert dispatch_delivery.drain_background_completions(forge=_Forge(ok=True))["delivered"] == 1

    next_boot: _queue.Queue = _queue.Queue()
    assert restore_undelivered_completions(next_boot) == 0
    assert next_boot.empty()


def test_a_completion_another_consumer_holds_is_never_double_delivered(
    durable_delegation, resolvable_sender, idle_sender, monkeypatch
):
    """The durable row, not this queue, decides whether a result is still owed.

    A queued event whose row is already claimed belongs to the consumer holding
    it. Delivering anyway would double the message in the sender's thread; and
    re-queueing it would spin on work somebody else owns, starving the events
    behind it.
    """

    from tools.async_delegation import claim_completion_delivery

    registry = _queue_once(monkeypatch, durable_delegation)
    dispatch_delivery._background_attempts.clear()
    assert claim_completion_delivery("deleg_testspecimen", "someone-else") is True

    forge = _Forge(ok=True)
    tally = dispatch_delivery.drain_background_completions(forge=forge)

    assert tally["unclaimed"] == 1
    assert tally["delivered"] == 0
    assert forge.calls == []
    assert registry.completion_queue.empty()


def test_a_failed_delegation_delivery_releases_its_claim_for_the_next_pass(
    durable_delegation, resolvable_sender, idle_sender, monkeypatch
):
    """A failure returns the row to the pool with its attempt honestly counted.

    Holding the claim would strand the completion behind a dead consumer; not
    counting the attempt is how an undeliverable row spins forever.
    """

    from tools import async_delegation

    registry = _queue_once(monkeypatch, durable_delegation)
    dispatch_delivery._background_attempts.clear()

    tally = dispatch_delivery.drain_background_completions(forge=_Forge(ok=False))

    assert tally["failed"] == 1
    row = async_delegation.get_durable_delegation("deleg_testspecimen")
    assert row["delivery_state"] == "pending"
    assert row["delivery_attempts"] == 1
    assert not registry.completion_queue.empty()
    # The process-local counter covers LEDGERLESS events only. A second counter
    # for a row that already counts its own attempts is the duplicate ledger
    # this change exists to retire.
    assert dispatch_delivery._background_attempts == {}


# --------------------------------------------------------------------------
# the delivery block tells the sender WHICH kind of nothing it got
# --------------------------------------------------------------------------


def _row_with(result_extra: dict) -> dict:
    return {
        "dispatch_id": "dispatch-1",
        "target_persona": "dev",
        "ask": "run the suite",
        "state": "completed",
        "dispatched_at": 1.0,
        "completed_at": 2.0,
        "target_session_id": "persona_chat_personainst_dev_bbbbbbbbbbbb",
        "result": {"reply": "", "error": "", **result_extra},
    }


def test_a_silent_turn_is_named_as_silence_not_as_a_missing_reply():
    """Re-dispatching is right for a lost answer and pointless for a silent one.

    The generic line left the sender to guess between the two, and guessing
    wrong costs a whole extra turn on both ends.
    """

    block = format_dispatch_delivery(
        _row_with(
            {
                "visibility": {
                    "state": "silent",
                    "reason": "truncated",
                    "finish_reason": "incomplete",
                    "reply_chars": 0,
                }
            }
        )
    )

    assert "ENDED IN SILENCE" in block
    assert "finish_reason=incomplete" in block
    assert "They returned no reply text." not in block


def test_an_unrecorded_verdict_keeps_the_honest_shrug():
    """A row from a process that recorded no verdict must not be given one."""

    block = format_dispatch_delivery(_row_with({}))

    assert "They returned no reply text." in block
    assert "ENDED IN SILENCE" not in block


def test_a_real_reply_says_neither():
    block = format_dispatch_delivery(
        _row_with({"reply": "3 failures, all in the chat panel"})
    )

    assert "--- THEIR REPLY ---" in block
    assert "no reply text" not in block
    assert "ENDED IN SILENCE" not in block
