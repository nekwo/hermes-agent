"""``agent_chat_send(wait=false)`` — the detached dispatch lane.

The lane's whole promise is "go do this, I'll keep working, bring me the answer
later". These tests pin the three things that make that promise keepable: the
call returns without running the turn, the row is durable BEFORE the work starts,
and the relay envelope is reshaped by an explicit operator ruling rather than by
accident.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from agent_runtime.dispatch_store import (
    DELIVERY_PENDING,
    STATE_ERROR,
    STATE_RUNNING,
    get_dispatch,
    running_dispatches,
)
from agent_runtime.relay_policy import RELAY_CHAIN, RELAY_DEADLINE
from tools import agent_chat_dispatch
from tools.agent_chat_tool import (
    AGENT_CHAT_DISPATCHES_SCHEMA,
    AGENT_CHAT_SEND_SCHEMA,
    agent_chat_dispatches,
    agent_chat_send,
)
from tools.registry import registry

SENDER_ROOT = "persona_chat_personainst_neko_aaaaaaaaaaaa"


@pytest.fixture
def store_home(tmp_path, monkeypatch):
    home = tmp_path / "bg-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_HEAD_HOME", str(home))
    return home


@pytest.fixture
def queued(monkeypatch):
    """Capture what would have been queued, without running a real turn."""

    calls = []

    def fake_queue(*, dispatch_id, args, max_concurrent):
        calls.append({"dispatch_id": dispatch_id, "args": args, "max_concurrent": max_concurrent})

    monkeypatch.setattr(agent_chat_dispatch, "dispatch_detached_turn", fake_queue)
    return calls


def _send(**overrides):
    payload = {
        "persona_id": "dev",
        "message": "Run the full launcher suite and report failures.",
        "wait": False,
        "requested_by_session": SENDER_ROOT,
    }
    payload.update(overrides)
    return json.loads(agent_chat_send(**payload))


# --------------------------------------------------------------------------
# the tool surface
# --------------------------------------------------------------------------


def test_wait_false_returns_a_handle_without_the_reply(store_home, queued):
    result = _send()

    assert result["ok"] is True
    assert result["dispatched"] is True
    assert result["dispatch_id"]
    assert result["target_persona"] == "dev"
    assert result["started_at"] > 0
    # The reply is deliberately ABSENT: an agent handed an empty `reply` field
    # would read it as "they had nothing to say" and act on the silence.
    assert "reply" not in result
    assert "do not re-send" in result["next_expected"]


def test_the_row_is_durable_before_the_turn_is_queued(store_home, queued):
    result = _send()

    row = get_dispatch(result["dispatch_id"])
    assert row is not None
    assert row["state"] == STATE_RUNNING
    assert row["delivery_state"] == DELIVERY_PENDING
    assert row["sender_session_id"] == SENDER_ROOT
    assert row["target_persona"] == "dev"
    assert row["ask"].startswith("Run the full launcher suite")
    assert [item["dispatch_id"] for item in running_dispatches()] == [result["dispatch_id"]]
    # And only THEN was it queued.
    assert queued[0]["dispatch_id"] == result["dispatch_id"]


def test_notify_operator_rides_the_row(store_home, queued):
    result = _send(notify_operator=True)

    assert result["notify_operator"] is True
    assert get_dispatch(result["dispatch_id"])["notify_operator"] is True


def test_wait_true_is_unchanged_and_never_touches_the_store(store_home, monkeypatch):
    """The default lane must behave exactly as it did before this feature."""

    def fake_handler(args):
        args.payload_sink({"ok": True, "reply": "ack", "session_id": "s1"})
        return 0

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", fake_handler)
    result = json.loads(agent_chat_send(persona_id="dev", message="hi"))

    assert result["ok"] is True
    assert result["reply"] == "ack"
    assert "dispatched" not in result
    assert running_dispatches() == []


def test_a_lane_with_no_chat_session_refuses_the_promise(store_home, queued):
    """No session ⇒ nowhere to deliver ⇒ refuse, never run-and-drop."""

    result = _send(requested_by_session=None)

    assert result["ok"] is False
    assert result["error_kind"] == "async_delivery_unavailable"
    assert queued == []
    assert running_dispatches() == []


def test_a_store_failure_refuses_rather_than_running_blind(store_home, queued, monkeypatch):
    monkeypatch.setattr(
        "agent_runtime.dispatch_store.record_dispatch",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("disk full")),
    )

    result = _send()

    assert result["ok"] is False
    assert result["error_kind"] == "dispatch_store_unavailable"
    # Nothing was queued: work whose result has nowhere to go must not run.
    assert queued == []


def test_a_queue_failure_settles_the_row_instead_of_stranding_it(
    store_home, monkeypatch
):
    """A row that can never complete must not sit `running` in the HUD forever."""

    def boom(**kw):
        raise RuntimeError("executor gone")

    monkeypatch.setattr(agent_chat_dispatch, "dispatch_detached_turn", boom)
    result = _send()

    assert result["ok"] is False
    assert result["error_kind"] == "dispatch_queue_failed"
    assert running_dispatches() == []


# --------------------------------------------------------------------------
# the relay ruling
# --------------------------------------------------------------------------


def test_a_detached_dispatch_mints_a_fresh_deadline(store_home, queued):
    """Operator ruling: a detached dispatch is a NEW CHAIN ROOT with its own clock.

    The shared chain deadline exists so a SYNCHRONOUS hop cannot outlive the
    caller blocked on it. Nobody is blocked here, so inheriting a conversational
    window would kill exactly the long work this lane exists to host.
    """

    import time

    chain_token = RELAY_CHAIN.set(("neko_supervisor",))
    deadline_token = RELAY_DEADLINE.set(time.time() + 12.0)
    try:
        result = _send()
    finally:
        RELAY_CHAIN.reset(chain_token)
        RELAY_DEADLINE.reset(deadline_token)

    assert result["ok"] is True
    args = queued[0]["args"]
    # Fresh, and sized by the dispatch budget rather than the 12s that was left.
    assert args.relay_deadline_epoch > time.time() + 600
    assert args.max_seconds >= 1800


def test_a_detached_dispatch_still_forwards_the_chain(store_home, queued):
    """Fresh CLOCK, same CHAIN — the budget is what resets, never the reach.

    Depth and cycle detection read the chain, not the deadline, and they are
    decided downstream by ``evaluate_relay`` at the handler chokepoint. Dropping
    the chain here would turn "buy more time" into "escape the cycle guard".
    """

    chain_token = RELAY_CHAIN.set(("neko_supervisor", "qa"))
    try:
        result = _send()
    finally:
        RELAY_CHAIN.reset(chain_token)

    assert result["relay_chain"] == ["neko_supervisor", "qa"]
    assert queued[0]["args"].relay_chain == ["neko_supervisor", "qa"]


def test_an_exhausted_chain_budget_still_fast_fails_a_WAITING_send(store_home):
    """The clamp is skipped for detached sends ONLY — the inline lane is unchanged."""

    import time

    deadline_token = RELAY_DEADLINE.set(time.time() + 1.0)
    try:
        result = json.loads(agent_chat_send(persona_id="dev", message="hi"))
    finally:
        RELAY_DEADLINE.reset(deadline_token)

    assert result["ok"] is False
    assert result["error_kind"] == "relay_budget_exhausted"


def test_the_worker_records_a_typed_chokepoint_refusal_as_a_result(
    store_home, monkeypatch
):
    """A cycle refusal on a detached hop must still REACH the sender.

    The guard runs where it always ran (the handler); detaching only changes
    when the sender hears about it. The refusal becomes the dispatch's recorded
    outcome, which the drain then delivers — it does not vanish.
    """

    from agent_runtime.dispatch_store import mint_dispatch_id, record_dispatch
    from types import SimpleNamespace

    dispatch_id = mint_dispatch_id()
    record_dispatch(
        dispatch_id=dispatch_id,
        sender_session_id=SENDER_ROOT,
        target_persona="dev",
        ask="hi",
    )

    def refuse(args):
        args.payload_sink(
            {"ok": False, "error_kind": "relay_cycle", "error": "relay cycle detected"}
        )
        return 2

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", refuse)
    agent_chat_dispatch._run_dispatch(dispatch_id, SimpleNamespace())

    row = get_dispatch(dispatch_id)
    assert row["state"] == STATE_ERROR
    assert "cycle" in row["result"]["error"]
    assert row["delivery_state"] == DELIVERY_PENDING


def test_the_worker_records_a_missing_payload_as_unknown_not_as_success(
    store_home, monkeypatch
):
    from agent_runtime.dispatch_store import mint_dispatch_id, record_dispatch
    from types import SimpleNamespace

    dispatch_id = mint_dispatch_id()
    record_dispatch(
        dispatch_id=dispatch_id, sender_session_id=SENDER_ROOT, target_persona="dev", ask="hi"
    )

    import hermes_cli.harness as harness

    monkeypatch.setattr(harness, "_cmd_mission_chat_message", lambda args: 0)
    agent_chat_dispatch._run_dispatch(dispatch_id, SimpleNamespace())

    assert get_dispatch(dispatch_id)["state"] == "unknown"


# --------------------------------------------------------------------------
# agent_chat_dispatches
# --------------------------------------------------------------------------


def test_dispatches_lists_only_the_callers_own_work(store_home, queued):
    mine = _send()["dispatch_id"]
    _send(requested_by_session="persona_chat_personainst_qa_bbbbbbbbbbbb")

    listed = json.loads(agent_chat_dispatches(requested_by_session=SENDER_ROOT))

    assert listed["ok"] is True
    assert [row["dispatch_id"] for row in listed["dispatches"]] == [mine]
    assert listed["running"] == 1


def test_dispatches_without_a_session_lists_nothing_not_everything(store_home, queued):
    _send()

    listed = json.loads(agent_chat_dispatches(requested_by_session=None))

    assert listed["count"] == 0
    assert listed["dispatches"] == []


def test_dispatches_never_carries_the_whole_reply(store_home, queued):
    """A status check must not cost the same context as the delivery itself."""

    from agent_runtime.dispatch_store import STATE_COMPLETED, record_completion

    dispatch_id = _send()["dispatch_id"]
    record_completion(dispatch_id, state=STATE_COMPLETED, reply="y" * 5000)

    row = json.loads(agent_chat_dispatches(requested_by_session=SENDER_ROOT))["dispatches"][0]

    assert row["reply_chars"] == 5000
    assert len(row["reply_excerpt"]) == 400


def test_state_filter_splits_running_from_done(store_home, queued):
    from agent_runtime.dispatch_store import STATE_COMPLETED, record_completion

    done = _send()["dispatch_id"]
    running = _send()["dispatch_id"]
    record_completion(done, state=STATE_COMPLETED, reply="ok")

    only_running = json.loads(
        agent_chat_dispatches(requested_by_session=SENDER_ROOT, state="running")
    )
    only_done = json.loads(
        agent_chat_dispatches(requested_by_session=SENDER_ROOT, state="done")
    )

    assert [row["dispatch_id"] for row in only_running["dispatches"]] == [running]
    assert [row["dispatch_id"] for row in only_done["dispatches"]] == [done]


# --------------------------------------------------------------------------
# schema / registration
# --------------------------------------------------------------------------


def test_both_tools_are_registered_on_the_agent_chat_toolset():
    for name in ("agent_chat_send", "agent_chat_dispatches"):
        entry = registry.get_entry(name)
        assert entry is not None, name
        assert entry.toolset == "agent_chat"
    assert AGENT_CHAT_DISPATCHES_SCHEMA["parameters"]["required"] == []


def test_max_seconds_has_no_schema_default_so_the_dispatch_budget_survives():
    """A materialised 240 default would silently cap every background dispatch."""

    assert "default" not in AGENT_CHAT_SEND_SCHEMA["parameters"]["properties"]["max_seconds"]


def test_the_send_schema_teaches_when_to_detach():
    text = AGENT_CHAT_SEND_SCHEMA["parameters"]["properties"]["wait"]["description"]
    assert "false" in text
    assert "background" in text
    assert "agent_chat_dispatches" in text
