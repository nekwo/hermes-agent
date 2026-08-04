"""``agent_chat_send(wait=false)`` — the detached dispatch lane.

The lane's whole promise is "go do this, I'll keep working, bring me the answer
later". These tests pin what makes that promise keepable: the call returns
without running the turn, the row is durable BEFORE the work starts, the turn
runs in its OWN PROCESS (never on a thread that would serialize behind
``profile_runner._WORKDIR_LOCK`` and freeze the whole runtime), and the lane
refuses outright whenever the answer would have nowhere to land.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from agent_runtime import dispatch_delivery
from agent_runtime.dispatch_store import (
    DELIVERY_PENDING,
    STATE_ERROR,
    STATE_RUNNING,
    get_dispatch,
    mint_dispatch_id,
    record_dispatch,
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
def deliverable_lane(monkeypatch):
    """A lane where a detached dispatch's answer genuinely has somewhere to land.

    Both halves are real preconditions the admission checks enforce: a live
    delivery consumer, and a sender root that resolves to a persona the drain can
    forge a turn back into.
    """

    from gateway.session_context import _SESSION_ASYNC_DELIVERY, declare_async_delivery_channel

    # The REAL contextvar, not a stub of the getter: the lane now requires a
    # POSITIVE declaration, so a test that stubbed only the value would pass
    # while the mechanism it is about went unexercised.
    token = _SESSION_ASYNC_DELIVERY.set(_SESSION_ASYNC_DELIVERY.get())
    declare_async_delivery_channel()
    monkeypatch.setattr(
        dispatch_delivery,
        "_sender_persona",
        lambda root: ("neko_supervisor", "personainst_neko") if root == SENDER_ROOT else None,
    )
    yield
    _SESSION_ASYNC_DELIVERY.reset(token)


@pytest.fixture
def queued(monkeypatch):
    """Capture the spec that would have been queued, without spawning anything."""

    calls = []

    def fake_queue(*, dispatch_id, spec, max_concurrent):
        calls.append({"dispatch_id": dispatch_id, "spec": spec, "max_concurrent": max_concurrent})

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


def test_wait_false_returns_a_handle_without_the_reply(store_home, deliverable_lane, queued):
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


def test_the_row_is_durable_before_the_turn_is_queued(store_home, deliverable_lane, queued):
    result = _send()

    row = get_dispatch(result["dispatch_id"])
    assert row is not None
    assert row["state"] == STATE_RUNNING
    assert row["delivery_state"] == DELIVERY_PENDING
    assert row["sender_session_id"] == SENDER_ROOT
    # Recorded, not left blank: the row answers "who asked for this" on its own.
    assert row["sender_persona_id"] == "neko_supervisor"
    assert row["target_persona"] == "dev"
    assert row["ask"].startswith("Run the full launcher suite")
    assert [item["dispatch_id"] for item in running_dispatches()] == [result["dispatch_id"]]
    # And only THEN was it queued.
    assert queued[0]["dispatch_id"] == result["dispatch_id"]


def test_notify_operator_rides_the_row(store_home, deliverable_lane, queued):
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


# --------------------------------------------------------------------------
# admission: never accept a promise this lane cannot keep
# --------------------------------------------------------------------------


def test_a_lane_with_no_chat_session_refuses_the_promise(store_home, deliverable_lane, queued):
    """No session ⇒ nowhere to deliver ⇒ refuse, never run-and-drop."""

    result = _send(requested_by_session=None)

    assert result["ok"] is False
    assert result["error_kind"] == "async_delivery_unavailable"
    assert queued == []
    assert running_dispatches() == []


def test_a_cold_cli_lane_refuses_instead_of_orphaning_the_work(store_home, monkeypatch, queued):
    """The capability contract, enforced by the tool that makes the promise.

    ``async_delivery_supported()`` is False when this turn's process ends with
    the turn. Accepting a dispatch there would leave a child process writing into
    a chat thread with no recorder left to settle its row — so the tool refuses,
    exactly as ``delegate_task`` falls back to its inline path on the same signal.
    """

    from gateway.session_context import _SESSION_ASYNC_DELIVERY, declare_stateless_channel

    token = _SESSION_ASYNC_DELIVERY.set(_SESSION_ASYNC_DELIVERY.get())
    declare_stateless_channel()
    monkeypatch.setattr(
        dispatch_delivery, "_sender_persona", lambda root: ("neko_supervisor", "personainst_neko")
    )
    try:
        result = _send()
    finally:
        _SESSION_ASYNC_DELIVERY.reset(token)

    assert result["ok"] is False
    assert result["error_kind"] == "async_delivery_unavailable"
    assert "wait=true" in result["error"]
    assert queued == []
    assert running_dispatches() == []


def test_a_sender_root_the_drain_cannot_resolve_is_refused_before_running(
    store_home, monkeypatch, queued
):
    """Run-and-drop is the one outcome this lane must never have.

    The drain addresses its forged turn by resolving the sender root to a
    persona; a root it cannot resolve is dropped AFTER the target has already
    done the work. The admission check uses the SAME resolver, so what the drain
    could not deliver never runs.
    """

    from gateway.session_context import declare_async_delivery_channel

    declare_async_delivery_channel()
    monkeypatch.setattr(dispatch_delivery, "_sender_persona", lambda root: None)

    result = _send()

    assert result["ok"] is False
    assert result["error_kind"] == "async_delivery_unavailable"
    assert queued == []
    assert running_dispatches() == []


def test_a_store_failure_refuses_rather_than_running_blind(
    store_home, deliverable_lane, queued, monkeypatch
):
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
    store_home, deliverable_lane, monkeypatch
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


def test_a_detached_dispatch_gets_the_background_budget_not_the_chain_remainder(
    store_home, deliverable_lane, queued
):
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
    # Sized by the dispatch budget rather than the 12s that was left.
    assert queued[0]["spec"]["max_seconds"] >= 1800


def test_the_deadline_is_minted_when_the_child_starts_not_when_it_is_queued(
    store_home, deliverable_lane, queued
):
    """Budget erosion: a queued dispatch must not burn its window waiting.

    The spec carries a BUDGET, never a deadline; the absolute epoch is computed
    inside ``build_dispatch_argv`` at spawn. Minting it at enqueue made the
    budget the sender was told about and the budget the turn actually got drift
    apart, silently, by however long the concurrency cap held it.
    """

    import time

    _send()
    spec = queued[0]["spec"]

    assert "relay_deadline_epoch" not in spec
    argv = agent_chat_dispatch.build_dispatch_argv(
        spec, deadline_epoch=time.time() + spec["max_seconds"]
    )
    minted = float(argv[argv.index("--relay-deadline-epoch") + 1])
    assert minted >= time.time() + spec["max_seconds"] - 5


def test_a_detached_dispatch_still_forwards_the_chain(store_home, deliverable_lane, queued):
    """Fresh CLOCK, same CHAIN — the budget is what resets, never the reach.

    Depth and cycle detection read the chain, not the deadline, and they are
    decided downstream by ``evaluate_relay`` at the handler chokepoint inside the
    child. Dropping the chain here would turn "buy more time" into "escape the
    cycle guard".
    """

    chain_token = RELAY_CHAIN.set(("neko_supervisor", "qa"))
    try:
        result = _send()
    finally:
        RELAY_CHAIN.reset(chain_token)

    assert result["relay_chain"] == ["neko_supervisor", "qa"]
    argv = agent_chat_dispatch.build_dispatch_argv(queued[0]["spec"], deadline_epoch=1.0)
    assert argv[argv.index("--relay-chain") + 1] == "neko_supervisor,qa"


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


def test_a_refusal_is_rewritten_for_the_lane_it_will_be_read_on():
    """"Answer your caller with what you have" is nonsense in a delivered turn.

    The sync copy addresses a caller blocked on the reply. A detached refusal is
    read minutes later, as its own turn, by an agent with no caller waiting — so
    the guidance has to change even though the refusal did not.
    """

    text = agent_chat_dispatch._detached_error_text(
        {"error_kind": "relay_budget_exhausted", "error": "Answer your caller with what you have."}
    )
    assert "Answer your caller" not in text
    assert "Re-dispatch" in text

    cycle = agent_chat_dispatch._detached_error_text(
        {"error_kind": "relay_cycle", "error": "relay cycle detected"}
    )
    assert "Nothing was executed" in cycle

    # Anything without lane-specific guidance passes through verbatim.
    assert (
        agent_chat_dispatch._detached_error_text({"error": "provider exploded"})
        == "provider exploded"
    )


# --------------------------------------------------------------------------
# the child invocation
# --------------------------------------------------------------------------


def test_the_child_argv_is_a_plain_mission_chat_turn():
    spec = {
        "persona_id": "dev",
        "message": "run the suite",
        "max_seconds": 1800.0,
        "new_session": None,
        "client_message_id": "agent-dispatch-x",
        "requested_by_session": SENDER_ROOT,
        "relay_chain": ["neko_supervisor"],
    }
    argv = agent_chat_dispatch.build_dispatch_argv(spec, deadline_epoch=1000.0)

    assert argv[0] == sys.executable
    assert argv[1:6] == ["-m", "hermes_cli.main", "harness", "mission-chat", "message"]
    assert "--json" in argv
    assert argv[argv.index("--persona") + 1] == "dev"
    assert argv[argv.index("--max-seconds") + 1] == "1800.000"
    assert argv[argv.index("--requested-by-session") + 1] == SENDER_ROOT


def test_the_thread_tri_state_survives_the_process_boundary():
    """argparse can say True and False but not UNSET, and UNSET is the default.

    Without its own spelling every dispatch would silently stop opening its own
    task thread and pile back into one sticky per-pair thread.
    """

    def flags(new_session):
        return agent_chat_dispatch.build_dispatch_argv(
            {"persona_id": "dev", "message": "x", "max_seconds": 1.0, "new_session": new_session},
            deadline_epoch=1.0,
        )

    assert "--new-session" in flags(True) and "--defer-thread-policy" not in flags(True)
    assert "--defer-thread-policy" in flags(None) and "--new-session" not in flags(None)
    # False is argparse's own absent-default; stating it would be a second spelling.
    assert "--new-session" not in flags(False) and "--defer-thread-policy" not in flags(False)


def test_the_child_environment_states_both_homes_and_pins_the_tree(tmp_path):
    env = agent_chat_dispatch.child_environment(
        {"hermes_home": str(tmp_path / "ambient"), "head_home": str(tmp_path / "head")}
    )

    # Stated, never inherited: the ambient home at DISPATCH time, and the
    # background-work home the parent and the projection read.
    assert env["HERMES_HOME"] == str(tmp_path / "ambient")
    assert env["HERMES_HEAD_HOME"] == str(tmp_path / "head")
    # And the child runs THIS tree's code, not whatever the interpreter would
    # otherwise import — a serve booted from a worktree spawns from it.
    import pathlib

    import hermes_cli

    root = str(pathlib.Path(hermes_cli.__file__).resolve().parents[1])
    assert env["PYTHONPATH"].split(os.pathsep)[0] == root


def test_the_payload_survives_noise_on_both_sides_of_it():
    """``emit_json`` is indented multi-line, and children print other things.

    A line scan cannot find it, and a naive decode of everything after the first
    brace chokes on the trailing advisory. Both kinds of noise are real: the
    SQLite WAL warning arrives before, provider teardown chatter after.
    """

    text = (
        "state.db: linked SQLite is vulnerable {not json}\n"
        '{\n  "ok": true,\n  "reply": "done"\n}\n'
        "some trailing chatter\n"
    )
    assert agent_chat_dispatch.parse_child_payload(text) == {"ok": True, "reply": "done"}
    assert agent_chat_dispatch.parse_child_payload("") is None
    assert agent_chat_dispatch.parse_child_payload("no json at all") is None


# --------------------------------------------------------------------------
# the supervisor: spawn → wait → record
# --------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, *, stdout="", stderr="", returncode=0, hang=False):
        import io

        self.pid = 4242
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode
        self._hang = hang

    def wait(self, timeout=None):
        if self._hang:
            self._hang = False  # the post-kill wait succeeds
            raise subprocess.TimeoutExpired(cmd="child", timeout=timeout or 0)
        return self.returncode


def _armed_dispatch(**overrides):
    dispatch_id = mint_dispatch_id()
    record_dispatch(
        dispatch_id=dispatch_id,
        sender_session_id=SENDER_ROOT,
        target_persona="dev",
        ask="run the suite",
        **overrides,
    )
    return dispatch_id


def _spec(**overrides):
    spec = {"persona_id": "dev", "message": "run the suite", "max_seconds": 1.0}
    spec.update(overrides)
    return spec


def test_a_child_that_replies_is_recorded_as_completed(store_home, monkeypatch):
    dispatch_id = _armed_dispatch()
    proc = _FakeProc(stdout=json.dumps({"ok": True, "reply": "3 failures", "session_id": "s-dev"}))
    monkeypatch.setattr(agent_chat_dispatch.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(agent_chat_dispatch, "_child_identity", lambda pid: 777)

    agent_chat_dispatch._run_dispatch(dispatch_id, _spec())

    row = get_dispatch(dispatch_id)
    assert row["state"] == "completed"
    assert row["result"]["reply"] == "3 failures"
    assert row["target_session_id"] == "s-dev"
    assert row["delivery_state"] == DELIVERY_PENDING
    # The row's owner became the CHILD, which is what keeps the orphan sweep
    # coherent when this supervisor is not around to answer for it.
    assert row["owner_pid"] == 4242
    assert row["owner_started_at"] == 777


def test_a_child_that_prints_nothing_is_unknown_not_a_silent_success(store_home, monkeypatch):
    dispatch_id = _armed_dispatch()
    proc = _FakeProc(stdout="", stderr="Traceback: boom\n", returncode=1)
    monkeypatch.setattr(agent_chat_dispatch.subprocess, "Popen", lambda *a, **k: proc)

    agent_chat_dispatch._run_dispatch(dispatch_id, _spec())

    row = get_dispatch(dispatch_id)
    assert row["state"] == "unknown"
    # stderr rides into the record: a failure with no diagnosis is unactionable.
    assert "boom" in row["result"]["error"]


def test_a_child_that_overruns_its_budget_is_killed_and_recorded(store_home, monkeypatch):
    dispatch_id = _armed_dispatch()
    proc = _FakeProc(hang=True)
    killed = []
    monkeypatch.setattr(agent_chat_dispatch.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(agent_chat_dispatch, "_child_identity", lambda pid: 777)
    monkeypatch.setattr(
        agent_chat_dispatch, "_kill_child", lambda pid, started: killed.append((pid, started))
    )

    agent_chat_dispatch._run_dispatch(dispatch_id, _spec(max_seconds=0.01))

    # Identity-verified kill: the recorded start time travels with the pid, so a
    # recycled number can never turn a timeout into a killed stranger.
    assert killed == [(4242, 777)]
    row = get_dispatch(dispatch_id)
    assert row["state"] == STATE_ERROR
    assert "budget" in row["result"]["error"]
    assert row["delivery_state"] == DELIVERY_PENDING


def test_a_spawn_failure_settles_the_row(store_home, monkeypatch):
    dispatch_id = _armed_dispatch()

    def boom(*a, **k):
        raise OSError("no exec for you")

    monkeypatch.setattr(agent_chat_dispatch.subprocess, "Popen", boom)

    agent_chat_dispatch._run_dispatch(dispatch_id, _spec())

    row = get_dispatch(dispatch_id)
    assert row["state"] == STATE_ERROR
    assert "could not be started" in row["result"]["error"]


def test_a_typed_chokepoint_refusal_still_reaches_the_sender(store_home, monkeypatch):
    """A cycle refusal on a detached hop must not vanish.

    The guard runs where it always ran (the handler, now inside the child);
    detaching only changes WHEN the sender hears about it. The refusal becomes
    the dispatch's recorded outcome, which the drain then delivers.
    """

    dispatch_id = _armed_dispatch()
    proc = _FakeProc(
        stdout=json.dumps(
            {"ok": False, "error_kind": "relay_cycle", "error": "relay cycle detected"}
        ),
        returncode=2,
    )
    monkeypatch.setattr(agent_chat_dispatch.subprocess, "Popen", lambda *a, **k: proc)

    agent_chat_dispatch._run_dispatch(dispatch_id, _spec())

    row = get_dispatch(dispatch_id)
    assert row["state"] == STATE_ERROR
    assert "Nothing was executed" in row["result"]["error"]
    assert row["delivery_state"] == DELIVERY_PENDING


def test_a_real_child_process_runs_and_uses_the_homes_it_was_given(store_home, tmp_path):
    """One end-to-end pass through the ACTUAL subprocess mechanics.

    Everything above stubs ``Popen``, which proves the bookkeeping and proves
    nothing about whether the command line is real. This spawns the genuine argv
    against a persona that does not exist, so the child boots the real CLI,
    reaches the real handler, and refuses in a second or two without a provider
    call. It proves exactly what the stubs cannot: the argv is valid, the child
    starts, BOTH pipes drain, the indented multi-line payload parses back out,
    and the completion is recorded — with the child resolving the homes it was
    handed rather than inheriting this process's.
    """

    child_head = tmp_path / "child-head"
    child_head.mkdir()
    dispatch_id = _armed_dispatch()

    agent_chat_dispatch._run_dispatch(
        dispatch_id,
        _spec(
            persona_id="no_such_persona_wp_h2",
            max_seconds=120.0,
            hermes_home=str(tmp_path / "child-ambient"),
            head_home=str(child_head),
        ),
    )

    row = get_dispatch(dispatch_id)
    # A refused turn is a RESULT, not a hang: terminal, deliverable, and typed.
    assert row["state"] == STATE_ERROR
    assert row["delivery_state"] == DELIVERY_PENDING
    assert "persona" in row["result"]["error"].lower()
    # The child was a real, separate process: its own pid, not this one's.
    assert row["owner_pid"] and row["owner_pid"] != os.getpid()


# --------------------------------------------------------------------------
# agent_chat_dispatches
# --------------------------------------------------------------------------


def test_dispatches_lists_only_the_callers_own_work(
    store_home, deliverable_lane, queued, monkeypatch
):
    mine = _send()["dispatch_id"]
    monkeypatch.setattr(dispatch_delivery, "_sender_persona", lambda root: ("qa", "personainst_qa"))
    _send(requested_by_session="persona_chat_personainst_qa_bbbbbbbbbbbb")

    listed = json.loads(agent_chat_dispatches(requested_by_session=SENDER_ROOT))

    assert listed["ok"] is True
    assert [row["dispatch_id"] for row in listed["dispatches"]] == [mine]
    assert listed["running"] == 1


def test_dispatches_without_a_session_lists_nothing_not_everything(
    store_home, deliverable_lane, queued
):
    _send()

    listed = json.loads(agent_chat_dispatches(requested_by_session=None))

    assert listed["count"] == 0
    assert listed["dispatches"] == []


def test_dispatches_never_carries_the_whole_reply(store_home, deliverable_lane, queued):
    """A status check must not cost the same context as the delivery itself."""

    from agent_runtime.dispatch_store import STATE_COMPLETED, record_completion

    dispatch_id = _send()["dispatch_id"]
    record_completion(dispatch_id, state=STATE_COMPLETED, reply="y" * 5000)

    row = json.loads(agent_chat_dispatches(requested_by_session=SENDER_ROOT))["dispatches"][0]

    assert row["reply_chars"] == 5000
    assert len(row["reply_excerpt"]) == 400


def test_state_filter_splits_running_from_done(store_home, deliverable_lane, queued):
    from agent_runtime.dispatch_store import STATE_COMPLETED, record_completion

    done = _send()["dispatch_id"]
    running = _send()["dispatch_id"]
    record_completion(done, state=STATE_COMPLETED, reply="ok")

    only_running = json.loads(
        agent_chat_dispatches(requested_by_session=SENDER_ROOT, state="running")
    )
    only_done = json.loads(agent_chat_dispatches(requested_by_session=SENDER_ROOT, state="done"))

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


def test_a_dispatch_runs_while_another_thread_holds_the_workdir_lock(store_home):
    """THE regression this whole execution model exists to prevent.

    ``profile_runner._WORKDIR_LOCK`` is a PROCESS-WIDE RLock held for the entire
    duration of a persona turn. The first implementation ran detached dispatches
    in-process, so one 30-minute dispatch froze every foreground operator turn,
    the sender's own next turn, the delivery drain, and every other queued
    dispatch — with the wall budget only starting INSIDE the lock, so nothing
    could even expire the wait.

    Here the lock is held by another thread for the whole test. A dispatch that
    still needed it would block until the timeout and fail; a dispatch that runs
    in its own process does not care at all. That difference is the fix, and
    this is what proves it.
    """

    import threading

    from agent_runtime import profile_runner

    dispatch_id = _armed_dispatch()
    holding = threading.Event()
    release = threading.Event()

    def _hold_the_lock():
        with profile_runner._WORKDIR_LOCK:
            holding.set()
            release.wait(30)

    holder = threading.Thread(target=_hold_the_lock, daemon=True)
    holder.start()
    try:
        assert holding.wait(10), "could not take the workdir lock"
        # The lock is held by ANOTHER thread right now (an RLock would let the
        # same thread straight through, which is why this is not inline).
        agent_chat_dispatch._run_dispatch(
            dispatch_id,
            _spec(persona_id="no_such_persona_wp_h2", max_seconds=120.0),
        )
    finally:
        release.set()
        holder.join(timeout=10)

    # It ran, it finished, and it recorded a terminal outcome — all while the
    # process-wide turn lock was held by someone else.
    row = get_dispatch(dispatch_id)
    assert row["state"] == STATE_ERROR
    assert row["delivery_state"] == DELIVERY_PENDING


# --------------------------------------------------------------------------
# payload integrity: a silent wrong answer is the failure class here
# --------------------------------------------------------------------------


def test_an_over_cap_chatty_child_still_yields_its_payload(store_home, monkeypatch):
    """The bound keeps the TAIL, because the payload is printed LAST.

    Head-keeping was a data-loss bug, not a tuning choice: reproduced at 583,070
    characters in and 512,033 captured, with the payload past the cap and gone —
    so a SUCCESSFUL thirty-minute dispatch was recorded `unknown`, and the stderr
    excerpt was empty because all the noise was on stdout.
    """

    dispatch_id = _armed_dispatch()
    noise = "x" * 200 + "\n"
    payload = json.dumps(
        {"ok": True, "capability_id": "mission.chat.message", "reply": "3 failures", "session_id": "s-dev"}
    )
    stdout = noise * 3000 + payload  # ~600KB, well past the 512KB cap
    assert len(stdout) > agent_chat_dispatch._MAX_STREAM_CHARS
    proc = _FakeProc(stdout=stdout)
    monkeypatch.setattr(agent_chat_dispatch.subprocess, "Popen", lambda *a, **k: proc)

    agent_chat_dispatch._run_dispatch(dispatch_id, _spec())

    row = get_dispatch(dispatch_id)
    assert row["state"] == "completed"
    assert row["result"]["reply"] == "3 failures"


def test_the_tail_buffer_drops_the_head_and_bounds_itself():
    tail = agent_chat_dispatch._BoundedTail(100)
    for _ in range(50):
        tail.append("y" * 10)

    text = tail.text()
    assert len(text) <= 110  # bounded (one chunk of slack by construction)
    assert tail.dropped_chars > 0
    # The END survived, which is the only property that matters here.
    tail.append("PAYLOAD")
    assert tail.text().endswith("PAYLOAD")


def test_trailing_json_after_the_payload_does_not_become_the_result(store_home, monkeypatch):
    """"Last object wins" was wrong in BOTH directions, and both were live.

    A shutdown notice printed after a successful turn turned it into an error; a
    trailing `{}` turned it into a success with an EMPTY reply, which reads to an
    agent as "they had nothing to report". The handler's own `capability_id` is
    the discriminator, so ordering stops deciding.
    """

    dispatch_id = _armed_dispatch()
    payload = json.dumps(
        {"ok": True, "capability_id": "mission.chat.message", "reply": "3 failures"}
    )
    stdout = payload + '\n{"event":"mcp_shutdown","ok":false}\n'
    proc = _FakeProc(stdout=stdout)
    monkeypatch.setattr(agent_chat_dispatch.subprocess, "Popen", lambda *a, **k: proc)

    agent_chat_dispatch._run_dispatch(dispatch_id, _spec())

    row = get_dispatch(dispatch_id)
    assert row["state"] == "completed"
    assert row["result"]["reply"] == "3 failures"


def test_a_trailing_empty_object_does_not_become_an_empty_success():
    payload = {"ok": True, "capability_id": "mission.chat.message", "reply": "done"}
    assert agent_chat_dispatch.parse_child_payload(json.dumps(payload) + "\n{}\n") == payload
    assert (
        agent_chat_dispatch.parse_child_payload(json.dumps(payload) + '\n{"ok": true}\n')
        == payload
    )
    # A payload with no marker at all is still better than reporting nothing.
    assert agent_chat_dispatch.parse_child_payload('{"ok": true, "reply": "hi"}') == {
        "ok": True,
        "reply": "hi",
    }


def test_a_prespawn_failure_still_settles_the_row(store_home, monkeypatch):
    """"Never raises" is now structural, not a docstring promise.

    A raise before the spawn used to escape entirely — `executor.submit` never
    retrieves the Future, so nothing observed it — leaving the row `running` and
    owned by the sender's still-live serve PID, which the orphan sweep can
    therefore never settle. Running forever, silently.
    """

    dispatch_id = _armed_dispatch()
    monkeypatch.setattr(
        agent_chat_dispatch,
        "build_dispatch_argv",
        lambda spec, deadline_epoch: (_ for _ in ()).throw(KeyError("persona_id")),
    )

    agent_chat_dispatch._run_dispatch(dispatch_id, _spec())  # must not raise

    row = get_dispatch(dispatch_id)
    assert row["state"] == STATE_ERROR
    assert "supervisor failed" in row["result"]["error"]
    assert row["delivery_state"] == DELIVERY_PENDING


def test_a_supervised_dispatch_is_not_an_orphan(store_home, monkeypatch):
    """THE race the periodic sweep introduced.

    Between the child exiting and the supervisor's `record_completion` — across
    `proc.wait()` and two pump joins — the sweep sees a dead PID on a running
    row. If it answers there, the sender is told "the outcome is unknown" for a
    dispatch that COMPLETED, and the real answer arriving moments later is
    swallowed by the delivery-turn replay dedup: told nothing, never corrected.
    """

    from agent_runtime.dispatch_store import restore_undelivered_dispatches

    dispatch_id = _armed_dispatch()
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)

    agent_chat_dispatch._mark_supervised(dispatch_id)
    try:
        assert restore_undelivered_dispatches()["restored"] == 0
        assert get_dispatch(dispatch_id)["state"] == STATE_RUNNING
    finally:
        agent_chat_dispatch._forget_supervised(dispatch_id)

    # Once nobody is supervising it, it IS an orphan and settles normally.
    assert restore_undelivered_dispatches()["restored"] == 1
    assert get_dispatch(dispatch_id)["state"] == "unknown"


def test_the_supervisors_observed_outcome_beats_the_sweeps_guess(store_home, monkeypatch):
    """End-to-end of the race: the sender must receive the REAL answer.

    Even with the sweep having already settled the row `unknown`, the
    supervisor's observed completion has to win — and re-arm delivery so the
    answer actually goes out.
    """

    from agent_runtime.dispatch_store import restore_undelivered_dispatches

    dispatch_id = _armed_dispatch()
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)

    # The sweep gets there first (nothing supervising, e.g. after a restart).
    assert restore_undelivered_dispatches()["restored"] == 1
    assert get_dispatch(dispatch_id)["state"] == "unknown"

    # The supervisor then reports what it actually observed.
    proc = _FakeProc(
        stdout=json.dumps(
            {"ok": True, "capability_id": "mission.chat.message", "reply": "3 failures"}
        )
    )
    monkeypatch.setattr(agent_chat_dispatch.subprocess, "Popen", lambda *a, **k: proc)
    agent_chat_dispatch._run_dispatch(dispatch_id, _spec())

    row = get_dispatch(dispatch_id)
    assert row["state"] == "completed"
    assert row["result"]["reply"] == "3 failures"
    assert row["delivery_state"] == DELIVERY_PENDING
