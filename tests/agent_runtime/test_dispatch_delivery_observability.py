"""The drain must NAME the gate that bounced a completion.

On 2026-08-11 a background completion (``proc_379e2ddbbc4f``) sat undeliverable
for ~5 consecutive drain passes while the operator's chat was provably idle, and
the gate that bounced it was structurally unknowable after the fact: the
per-pass tally was returned and discarded, every requeue logged nothing, and no
store recorded a considered-and-bounced event.

Everything here pins the ACCOUNTING half of the fix, and — just as load-bearing
— that the accounting did not become the decision. The drain's existing tests
override ``dispatch_delivery._sender_is_idle`` BY NAME; if the decision stopped
routing through that exact attribute those overrides would quietly stop biting
and ~11 tests would pass vacuously while still printing green.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

import pytest

from agent_runtime import dispatch_delivery, persona_chat_continuity
from agent_runtime.dispatch_store import DELIVERY_DELIVERED, get_dispatch

# Reused AS-IS from the drain's own suite: these are the fixtures whose
# overrides this module has to prove are still honored.
from tests.agent_runtime.test_dispatch_delivery import (  # noqa: F401
    SENDER_ROOT,
    _completed,
    _queue_once,
    durable_delegation,
    idle_sender,
    resolvable_sender,
    store_home,
)


@pytest.fixture(autouse=True)
def fresh_telemetry():
    """Telemetry is module-global, exactly like ``_background_attempts``."""

    dispatch_delivery._telemetry.reset()
    dispatch_delivery._LAST_IDLE_PROBE.set(None)
    yield
    dispatch_delivery._telemetry.reset()


@pytest.fixture
def registry_left_as_found():
    """Hand the PROCESS-GLOBAL registry back exactly as it was found.

    ``process_registry`` is a module singleton whose ``_running``/``_finished``
    maps outlive any tmp home, so a real spawn left behind here becomes a row in
    every later projection this process builds — which is how a spawn in THIS
    module silently reddened ``test_response_contract_fixture`` seven tests
    later. Session state does not get to leak across a test boundary just
    because the object is a singleton.
    """

    from tools.process_registry import process_registry

    with process_registry._lock:
        before_running = set(process_registry._running)
        before_finished = set(process_registry._finished)
    try:
        yield process_registry
    finally:
        with process_registry._lock:
            for session_id in set(process_registry._running) - before_running:
                process_registry._running.pop(session_id, None)
            for session_id in set(process_registry._finished) - before_finished:
                process_registry._finished.pop(session_id, None)
        process_registry._completion_consumed.clear()
        process_registry._poll_observed.clear()
        while not process_registry.completion_queue.empty():
            process_registry.completion_queue.get_nowait()


def _reasons() -> list[str]:
    return [row["reason"] for row in dispatch_delivery._telemetry.snapshot()["outcomes"]]


def _row(reason: str) -> dict:
    for row in dispatch_delivery._telemetry.snapshot()["outcomes"]:
        if row["reason"] == reason:
            return row
    raise AssertionError(f"no outcome row for {reason!r}; have {_reasons()}")


def _drain_the_queue():
    from tools.process_registry import process_registry

    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    return process_registry


class _Forge:
    def __init__(self, ok=True, payload=None):
        self.calls = []
        self.ok = ok
        self.payload = payload

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.ok, (self.payload if self.payload is not None else {"ok": self.ok})


# ---------------------------------------------------------------------------
# 1. the probe classifies the four causes that used to collapse into one False
# ---------------------------------------------------------------------------


def test_an_inflight_journal_record_is_named_and_identified(store_home):
    """`journal_inflight` — and the detail has to NAME the record.

    "busy" alone is what made the live stall unclassifiable: an operator
    staring at an idle chat had no way to tell a real turn from a stale journal
    record from a stale lock.
    """

    from agent_runtime.mission_chat_turns import persist_mission_chat_turn

    persist_mission_chat_turn(
        session_id=SENDER_ROOT,
        client_message_id="cmid-inflight",
        turn_id="turn-inflight",
        elements=None,
        state="executing",
    )

    probe = dispatch_delivery._probe_sender_idle(SENDER_ROOT)

    assert probe.is_idle is False
    assert probe.busy_sub == "journal_inflight"
    assert "cmid-inflight" in probe.detail
    assert "executing" in probe.detail


def test_an_unreadable_journal_is_named_rather_than_read_as_busy(store_home, monkeypatch):
    """Fail-closed is right; fail-closed and SILENT is what this retires."""

    monkeypatch.setattr(
        "agent_runtime.mission_chat_turns.mission_chat_turn_records",
        lambda *, session_id: (_ for _ in ()).throw(RuntimeError("journal gone")),
    )

    probe = dispatch_delivery._probe_sender_idle(SENDER_ROOT)

    assert probe.is_idle is False
    assert probe.busy_sub == "journal_unreadable"
    assert "journal gone" in probe.detail


def test_a_held_lease_with_an_owner_names_its_holder(store_home):
    """Ordinary contention. The detail carries the owner payload so a dead pid
    in it is its own finding rather than an indistinguishable "busy"."""

    with persona_chat_continuity.persona_chat_root_lease(
        SENDER_ROOT, owner_id="the-holder", observer_kind="cli"
    ):
        probe = dispatch_delivery._probe_sender_idle(SENDER_ROOT)

    assert probe.is_idle is False
    assert probe.busy_sub == "lease_busy_owned"
    assert "the-holder" in probe.detail
    assert str(os.getpid()) in probe.detail


def test_a_held_lease_with_no_owner_file_is_the_stale_lock_fingerprint(store_home):
    """THE discriminator this slice exists to capture.

    Release order is unlink-owner → unlock → close, so a single ownerless sample
    can be a releasing holder caught mid-window. That is exactly why the
    sub-reason is distinct from `lease_busy_owned`: only a repeat across
    consecutive passes is proof, and the two can never be told apart if both
    collapse into one silent False.
    """

    lock_path, owner_path = persona_chat_continuity._lease_paths(SENDER_ROOT)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        persona_chat_continuity._try_lock(fd)
        assert not owner_path.exists()

        probe = dispatch_delivery._probe_sender_idle(SENDER_ROOT)

        assert probe.is_idle is False
        assert probe.busy_sub == "lease_busy_ownerless"
    finally:
        try:
            persona_chat_continuity._unlock(fd)
        except OSError:
            pass
        os.close(fd)


def test_an_idle_root_probes_idle_with_no_sub_reason(store_home):
    probe = dispatch_delivery._probe_sender_idle(SENDER_ROOT)

    assert probe.is_idle is True
    assert probe.busy_sub == ""


# ---------------------------------------------------------------------------
# 2. the DECISION did not move — the guard against making the suite vacuous
# ---------------------------------------------------------------------------


def test_an_overridden_idle_decision_is_honored_and_admitted_as_unprobed(
    store_home, resolvable_sender, monkeypatch
):
    """The accounting must never re-derive the decision it is accounting for.

    ``tests/agent_runtime/test_dispatch_delivery.py`` overrides
    ``_sender_is_idle`` by name in ~11 tests. If the drain routed its decision
    through ``_probe_sender_idle`` instead, every one of those overrides would
    stop biting — the suite would keep printing green over behaviour nothing
    tests any more. So: the override decides (the event is requeued), and
    accounting records that it could not observe the decision rather than
    quietly taking a second one.
    """

    registry = _drain_the_queue()
    monkeypatch.setattr(dispatch_delivery, "_sender_is_idle", lambda root: False)
    evt = {
        "type": "completion",
        "session_id": SENDER_ROOT,
        "session_key": SENDER_ROOT,
        "command": "sleep 20",
        "exit_code": 0,
    }
    registry.completion_queue.put(evt)
    forge = _Forge()

    tally = dispatch_delivery.drain_background_completions(forge=forge)

    # (a) the DECISION: requeued, untouched, exactly as the existing
    #     busy-requeues test pins it.
    assert tally["requeued"] == 1 and tally["delivered"] == 0
    assert forge.calls == []
    assert not registry.completion_queue.empty()
    # (b) the ACCOUNTING: it saw an override, and said so.
    assert "sender_busy:unprobed" in _reasons()
    _drain_the_queue()


def test_a_real_busy_probe_is_named_by_its_sub_reason(store_home, resolvable_sender):
    """With NO override the same site names the actual gate."""

    registry = _drain_the_queue()
    evt = {
        "type": "completion",
        "session_id": SENDER_ROOT,
        "session_key": SENDER_ROOT,
        "command": "sleep 20",
        "exit_code": 0,
    }
    registry.completion_queue.put(evt)

    with persona_chat_continuity.persona_chat_root_lease(SENDER_ROOT, owner_id="op"):
        tally = dispatch_delivery.drain_background_completions(forge=_Forge())

    assert tally["requeued"] == 1
    assert "sender_busy:lease_busy_owned" in _reasons()
    _drain_the_queue()


def test_three_consecutive_ownerless_probes_escalate_once(store_home, caplog):
    """One WARNING per episode, not one per pass — and only past the third.

    A releasing holder caught mid-window produces exactly one ownerless sample;
    warning on that would train the operator to ignore the line that matters.
    """

    telemetry = dispatch_delivery._telemetry
    assert telemetry.note_ownerless_streak(SENDER_ROOT, ownerless=True) == 0
    assert telemetry.note_ownerless_streak(SENDER_ROOT, ownerless=True) == 0
    assert telemetry.note_ownerless_streak(SENDER_ROOT, ownerless=True) == 3
    # Still stuck, but the episode has already been reported.
    assert telemetry.note_ownerless_streak(SENDER_ROOT, ownerless=True) == 0
    # The probe succeeded: the episode is over and the next one warns again.
    assert telemetry.note_ownerless_streak(SENDER_ROOT, ownerless=False) == 0
    for _ in range(2):
        assert telemetry.note_ownerless_streak(SENDER_ROOT, ownerless=True) == 0
    assert telemetry.note_ownerless_streak(SENDER_ROOT, ownerless=True) == 3


# ---------------------------------------------------------------------------
# 3. `not_owned` is visible even though upstream requeues it out of sight
# ---------------------------------------------------------------------------


def test_an_event_upstream_rejects_is_still_named(store_home, monkeypatch):
    """Upstream re-queues a non-owned event INSIDE ``drain_notifications``.

    The caller never sees it, and instrumenting upstream is out of bounds. The
    ownership callback, however, is OURS — so the bounce is recorded there,
    while the requeue behaviour stays byte-identical (queue depth unchanged).
    """

    registry = _drain_the_queue()
    evt = {
        "type": "completion",
        "session_id": "proc_ghost",
        # Routing metadata upstream demands positive proof for — and which
        # resolves to no live persona chat root in this runtime.
        "session_key": "persona_chat_ghostinstance_0123456789ab",
        "command": "sleep 20",
        "exit_code": 0,
    }
    registry.completion_queue.put(evt)

    tally = dispatch_delivery.drain_background_completions(forge=_Forge())

    assert tally["considered"] == 0  # upstream never handed it over
    assert registry.completion_queue.qsize() == 1  # …and put it straight back
    assert "not_owned" in _reasons()
    assert "persona_chat_ghostinstance" in _row("not_owned")["detail"]
    _drain_the_queue()


def test_an_unroutable_event_upstream_hands_over_is_named_no_root(store_home):
    """The pre-``c5d11f5cf`` live shape: a spawn nobody bound a session key to.

    Upstream demands positive proof only for events that CARRY routing
    metadata, so a ``session_key: ""`` completion is handed over as a legacy
    ownerless event and bounces one layer further in — at the loop's own
    re-check. Different gate, different reason, and the detail carries the empty
    key that is the whole finding.
    """

    registry = _drain_the_queue()
    evt = {
        "type": "completion",
        "session_id": "proc_unbound",
        "session_key": "",
        "command": "sleep 20",
        "exit_code": 0,
    }
    registry.completion_queue.put(evt)

    tally = dispatch_delivery.drain_background_completions(forge=_Forge())

    assert tally["considered"] == 1 and tally["requeued"] == 1
    assert "no_root" in _reasons()
    assert _row("no_root")["detail"] == "session_key=''"
    _drain_the_queue()


# ---------------------------------------------------------------------------
# 4. the telemetry is bounded BY CONSTRUCTION
# ---------------------------------------------------------------------------


def test_a_hundred_distinct_bounces_evict_oldest_first_and_stay_bounded():
    """A drain that runs for weeks must not grow a row per event forever."""

    telemetry = dispatch_delivery._telemetry
    for index in range(100):
        telemetry.record_bounce(f"completion:proc_{index:04d}", "no_root", "x")

    outcomes = telemetry.snapshot()["outcomes"]
    assert len(outcomes) == dispatch_delivery.MAX_DRAIN_OUTCOME_ROWS
    # Oldest-first eviction: the survivors are the LAST N recorded, in order.
    assert [row["event_key"] for row in outcomes] == [
        f"completion:proc_{index:04d}"
        for index in range(100 - dispatch_delivery.MAX_DRAIN_OUTCOME_ROWS, 100)
    ]


def test_a_stuck_event_occupies_one_row_with_a_rising_count():
    """The stuck-event case is the one that must NOT grow a list.

    Five passes bouncing the same completion for the same reason is one fact
    with a count, which is also the shape an operator needs: "this, five times"
    is the evidence; five identical rows is noise.
    """

    telemetry = dispatch_delivery._telemetry
    for _ in range(5):
        telemetry.record_bounce("completion:proc_stuck", "sender_busy:lease_busy_ownerless", "d")

    outcomes = telemetry.snapshot()["outcomes"]
    assert len(outcomes) == 1
    assert outcomes[0]["count"] == 5
    assert outcomes[0]["last_at"] >= outcomes[0]["first_at"]


def test_a_detail_can_never_grow_without_bound():
    dispatch_delivery._telemetry.record_bounce("k", "no_root", "x" * 5000)

    assert len(_row("no_root")["detail"]) == dispatch_delivery.DRAIN_DETAIL_LIMIT


# ---------------------------------------------------------------------------
# 5. the durable mirror — written on change, not on cadence
# ---------------------------------------------------------------------------


def _run_drain_briefly(interval=0.02, settle=0.4):
    stop = threading.Event()
    thread = dispatch_delivery.start_delivery_drain(stop_event=stop, interval_seconds=interval)
    time.sleep(settle)
    return stop, thread


def test_the_mirror_lands_at_the_frozen_path_and_carries_its_writer(store_home):
    path = dispatch_delivery._drain_state_path()
    stop, thread = _run_drain_briefly()
    try:
        deadline = time.time() + 5
        while not path.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert path.exists(), "the drain never mirrored its state"
        payload = json.loads(path.read_text(encoding="utf-8"))
    finally:
        stop.set()
        thread.join(timeout=5)

    assert payload["pid"] == os.getpid()
    assert isinstance(payload["written_at"], float)
    assert payload["live"] is True
    assert isinstance(payload["outcomes"], list)


def test_empty_passes_do_not_churn_the_mirror(store_home):
    """A file rewritten every 5s would be pure churn — and the reason it is
    kept out of every freshness fingerprint would become a live problem."""

    path = dispatch_delivery._drain_state_path()
    stop, thread = _run_drain_briefly()
    try:
        deadline = time.time() + 5
        while not path.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert path.exists()
        first = path.stat().st_mtime_ns
        # ~20 further passes, all empty and all inside the heartbeat window.
        time.sleep(0.5)
        second = path.stat().st_mtime_ns
    finally:
        stop.set()
        thread.join(timeout=5)

    assert second == first


def test_a_dead_drain_is_legible_from_the_file(store_home):
    path = dispatch_delivery._drain_state_path()
    stop, thread = _run_drain_briefly()
    stop.set()
    thread.join(timeout=5)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["live"] is False


def test_a_corrupt_mirror_reads_as_absent_not_as_an_error(store_home):
    path = dispatch_delivery._drain_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert dispatch_delivery.read_delivery_drain_state() is None
    assert dispatch_delivery.delivery_drain_status() == {"live": False, "source": "absent"}


# ---------------------------------------------------------------------------
# 6. `harness status --json` surfaces it, from whichever source exists
# ---------------------------------------------------------------------------


def test_status_reads_the_mirror_when_no_drain_runs_in_this_process(store_home):
    """THE reason the mirror is mandatory rather than a nicety.

    The launcher's visibility probe runs ``harness status --json`` as a freshly
    spawned process. In-memory telemetry does not exist there, so without the
    file the operator's only channel would be log spelunking.
    """

    from agent_runtime.status import build_status

    assert dispatch_delivery.delivery_drain_is_live() is False
    dispatch_delivery._telemetry.record_bounce(
        "completion:proc_379e2ddbbc4f", "sender_busy:lease_busy_ownerless", "stuck"
    )
    assert dispatch_delivery._write_drain_state(
        dispatch_delivery._drain_state_path(), live=True
    )

    block = build_status()["delivery_drain"]

    assert block["source"] == "state_file"
    assert block["outcomes"][0]["reason"] == "sender_busy:lease_busy_ownerless"


def test_status_prefers_this_process_when_the_drain_is_live_here(store_home):
    from agent_runtime.status import build_status

    stop = threading.Event()
    thread = dispatch_delivery.start_delivery_drain(stop_event=stop, interval_seconds=5.0)
    try:
        block = build_status()["delivery_drain"]
    finally:
        stop.set()
        thread.join(timeout=10)

    assert block["source"] == "in_process"
    assert block["live"] is True
    assert block["pid"] == os.getpid()


def test_status_says_absent_rather_than_inventing_a_drain(store_home):
    from agent_runtime.status import build_status

    assert build_status()["delivery_drain"] == {"live": False, "source": "absent"}


def test_status_never_fails_because_telemetry_did(store_home, monkeypatch):
    """Accounting must not be able to break the projection it rides on."""

    from agent_runtime.status import build_status

    monkeypatch.setattr(
        dispatch_delivery,
        "delivery_drain_is_live",
        lambda: (_ for _ in ()).throw(RuntimeError("telemetry exploded")),
    )

    assert build_status()["delivery_drain"] == {"live": False, "source": "absent"}


# ---------------------------------------------------------------------------
# 7. and it must stay OUT of every freshness fingerprint
# ---------------------------------------------------------------------------


def test_the_mirror_is_in_no_freshness_fingerprint(store_home):
    """A tombstone against a future "just add it to the fingerprint" reflex.

    The mirror changes every ≤60s BY DESIGN. Inside serve's read-model
    fingerprint that would hold the cache permanently cold; inside the stream's
    scope fingerprint it would make the watchdog emit ``state.reconciled``
    forever. Same churn rationale as the per-session turn store's documented
    exclusion. Both fingerprints are allowlists, so the guarantee is simply
    "never added" — which is precisely the kind of guarantee that decays
    silently without a pin.
    """

    import inspect

    from agent_runtime import stream
    from agent_runtime.running_work import running_work_store_paths
    from hermes_cli.harness_parts import serve

    name = dispatch_delivery.DRAIN_STATE_FILENAME
    assert name not in serve._FINGERPRINT_ROOT_FILES
    assert name not in serve._FINGERPRINT_STORE_DIRS
    assert all(name not in str(path) for path in running_work_store_paths())
    assert name not in inspect.getsource(stream._scope_fingerprint)
    assert name not in inspect.getsource(serve._runtime_state_fingerprint)


# ---------------------------------------------------------------------------
# 9. the JOIN — producer → queue → drain → forge → journal, as ONE guarantee
#
# Both halves of this seam have been individually correct while the join was
# broken (2026-08-11), which is why this is one test in one process with only
# the model stubbed. The runner-entry half — profile_runner entering
# ``chat_root_session_key_scope`` — is pinned by the test landed in 601afb326
# ("pin the runner-level chat-root session-key binding") and is deliberately
# not duplicated here.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("persisted_persona_samples")
def test_a_bound_spawn_becomes_a_delivered_turn_in_the_senders_own_thread(
    monkeypatch, capsys, tmp_path, registry_left_as_found
):
    from types import SimpleNamespace

    from agent_runtime.mission_chat_turns import (
        TERMINAL_TURN_STATES,
        mission_chat_turn_records,
    )
    from agent_runtime.persona_assignments import (
        PersonaInstanceStore,
        persona_chat_session_id_for,
    )
    from agent_runtime.store import AgentStore
    from hermes_cli import harness
    from tests.agent_runtime.test_persona_assignments import (
        _assignment_config,
        _TranscriptDB,
    )
    from tools.approval import get_current_session_key

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_HEAD_HOME", str(tmp_path / "home"))

    # A REAL owner: no `resolvable_sender` stub anywhere in this test.
    persona = next(item for item in AgentStore().list_all() if item.id == "dev")
    instance = PersonaInstanceStore().ensure_for_persona(persona)
    root = persona_chat_session_id_for(instance.id)
    assert dispatch_delivery._sender_persona(root) == (persona.id, instance.id)

    # ---- producer half -----------------------------------------------------
    registry = _drain_the_queue()
    with persona_chat_continuity.chat_root_session_key_scope(root):
        # Exactly what tools/terminal_tool.py reads at its own spawn site.
        session_key = get_current_session_key(default="") or ""
        assert session_key == root
        proc = registry.spawn_local(
            command="sleep 1",
            cwd=str(tmp_path),
            task_id="join-test",
            session_key=session_key,
        )
        proc.notify_on_complete = True

    assert proc._completion_event.wait(timeout=60), "the probe process never exited"
    deadline = time.time() + 20
    while registry.completion_queue.empty() and time.time() < deadline:
        time.sleep(0.05)
    queued = registry.completion_queue.get_nowait()
    registry.completion_queue.put(queued)
    # THE producer guarantee: the spawn carried the chat root, so the event
    # names a session the drain can resolve. `session_key: ""` here is the whole
    # 2026-08-11 defect.
    assert queued["session_key"] == root
    assert queued["session_id"] == proc.id

    # ---- consumer half -----------------------------------------------------
    db = _TranscriptDB()
    provider_calls: list[str] = []
    markers: list[str] = []

    class _ProviderSpy:
        def __init__(self, *args, **kwargs):
            pass

        def mission_chat_reply(self, persona, message, **kwargs):
            provider_calls.append(message)
            markers.append(str(kwargs.get("relay_sender_marker") or ""))
            return SimpleNamespace(
                final_response="noted, thanks",
                input_tokens=1,
                output_tokens=2,
                total_tokens=3,
                raw={},
            )

    monkeypatch.setattr(harness, "load_agent_runtime_config", _assignment_config)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)
    monkeypatch.setattr(harness, "GPTPersonaRuntime", _ProviderSpy)

    # Real `_chat_root_of_completion`, real `_sender_persona`, real
    # `_sender_is_idle`, real `forge_delivery_turn`.
    tally = dispatch_delivery.drain_background_completions()
    capsys.readouterr()

    assert tally["delivered"] == 1, tally
    assert provider_calls, "the model never saw the completion"

    # The user row is typed as a DELIVERY rather than as something the operator
    # wrote. Since native session continuity landed, the mission-chat lane does
    # NOT append that row itself — the runtime persists it with the turn and
    # `profile_runner.stage_persona_chat_user_row_marker` types it from exactly
    # this value (`request.persona_chat_user_finish_reason`). The provider is
    # the only thing stubbed here, so this marker handed to the runtime IS the
    # reachable end of that chain; asserting on `db.messages` instead would be
    # asserting on a row the stub was never going to write.
    assert markers and markers[0].startswith("harness_delivery"), markers

    # The turn journal settled the forged turn under the derived, non-minted id.
    expected_cmid = f"bg-completion-completion:{proc.id}"
    records = {
        str(record.get("client_message_id")): record
        for record in mission_chat_turn_records(session_id=root)
    }
    assert expected_cmid in records, sorted(records)
    assert records[expected_cmid]["state"] in TERMINAL_TURN_STATES

    # …and the drain SAYS it delivered, which is the half the incident lacked.
    assert f"completion:{proc.id}" == _row("delivered")["event_key"]
    _drain_the_queue()


# ---------------------------------------------------------------------------
# 10. "delivered" must not be allowed to mean "delivered into silence"
#
# 2026-08-11, second incident on this lane: the completion notice reached the
# operator's thread and the delivery turn's model answered it with no content,
# three retries deep. The forge returned ok, so the drain logged a delivery,
# `last_delivery` recorded a delivery, and `harness status` reported a delivery
# — while the operator sat looking at a thread where nothing had happened. The
# mechanism worked perfectly; only the report was false, which is the harder
# failure to catch, because everything upstream of it succeeded.
# ---------------------------------------------------------------------------


def test_an_empty_reply_is_recorded_as_a_silent_delivery(
    store_home, resolvable_sender, idle_sender
):
    """Empty reply -> `delivered_silent`, and the SETTLEMENT is unchanged.

    Both halves matter. The reason has to name the silence, and the dispatch
    still has to settle: re-queuing would re-forge into the very transcript
    that produced the silence, once per pass, with a duplicate notice per lap.
    """

    dispatch_id = _completed()

    tally = dispatch_delivery.drain_once(forge=_Forge(ok=True, payload={"ok": True, "reply": "  "}))

    assert tally["delivered"] == 1
    assert get_dispatch(dispatch_id)["delivery_state"] == DELIVERY_DELIVERED
    assert _reasons() == [dispatch_delivery.DELIVERED_SILENT_REASON]
    assert "empty reply" in _row(dispatch_delivery.DELIVERED_SILENT_REASON)["detail"]


def test_a_turn_that_actually_replied_is_still_a_plain_delivery(
    store_home, resolvable_sender, idle_sender
):
    """The other half of the truth table — silence must stay the exception."""

    _completed()

    dispatch_delivery.drain_once(
        forge=_Forge(ok=True, payload={"ok": True, "reply": "3 failures, all in the chat panel"})
    )

    assert _reasons() == [dispatch_delivery.DELIVERED_REASON]


def test_a_forge_that_reports_no_reply_at_all_is_not_called_silent(
    store_home, resolvable_sender, idle_sender
):
    """Missing evidence is not evidence of silence.

    A caller-supplied forge — this suite's own, and any other lane that borrows
    the drain — may return a payload with no `reply` key. Manufacturing
    `delivered_silent` out of that would be the same lie pointed the other way,
    and it would fire on every test in the sibling module.
    """

    _completed()

    dispatch_delivery.drain_once(forge=_Forge(ok=True, payload={"ok": True}))

    assert _reasons() == [dispatch_delivery.DELIVERED_REASON]


def test_last_delivery_itself_names_the_silence(
    store_home, resolvable_sender, idle_sender
):
    """`last_delivery` is the field a human reads first, so it carries it too.

    A silent delivery is still the last thing this drain delivered — dropping it
    from the field would trade one blindness for another.
    """

    dispatch_id = _completed()

    dispatch_delivery.drain_once(forge=_Forge(ok=True, payload={"ok": True, "reply": ""}))

    last = dispatch_delivery._telemetry.snapshot()["last_delivery"]
    assert last["event_key"] == f"dispatch:{dispatch_id}"
    assert last["reason"] == dispatch_delivery.DELIVERED_SILENT_REASON
    assert last["at"] is not None


def test_a_silent_delivery_is_logged_at_warning(
    store_home, resolvable_sender, idle_sender, caplog
):
    """Not info. Nobody greps info for a lane that reported success."""

    _completed()

    with caplog.at_level(logging.WARNING, logger="agent_runtime.dispatch_delivery"):
        dispatch_delivery.drain_once(forge=_Forge(ok=True, payload={"ok": True, "reply": ""}))

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]
    assert any("INTO SILENCE" in message for message in warnings), warnings


def test_a_silent_background_completion_keeps_its_producer_detail(
    durable_delegation, resolvable_sender, idle_sender, monkeypatch
):
    """The queue lane splits the same way — and does not lose what it had.

    Its detail already carried `producer_started_at`, which is what dates a
    completion against the process that produced it. The visibility note is
    appended to that, not substituted for it.
    """

    from tools.async_delegation import get_durable_delegation

    _queue_once(monkeypatch, durable_delegation)
    dispatch_delivery._background_attempts.clear()

    tally = dispatch_delivery.drain_background_completions(
        forge=_Forge(ok=True, payload={"ok": True, "reply": ""})
    )

    assert tally["delivered"] == 1
    detail = _row(dispatch_delivery.DELIVERED_SILENT_REASON)["detail"]
    assert "producer_started_at=" in detail and "empty reply" in detail
    # Acknowledged on its durable row exactly as a visible delivery would be:
    # the turn ran, so the next boot must not re-queue it.
    assert get_durable_delegation("deleg_testspecimen")["delivery_state"] == "delivered"
