"""R0 (2026-08-09) — the chat-root lease covers the turn's WRITES, not its tail.

Incident: the operator's follow-up was refused ``chat_busy`` **56 seconds after
the reply he was answering was already on his screen**. The refusal was correct
— the root really was leased — but the lease was being held by ~46 seconds of
finalization tail that wrote nothing to the root: auxiliary-provider resolution,
a lazy ``pip install`` of ``provider.anthropic``, and a 401-refresh-retry loop,
all of it the session auto-title's synchronous auxiliary-LLM round trip.

The tail was already placed *after* the terminal frame was emitted (finding F4).
That is a different boundary from *after the lease releases*, and the operator
experiences the second one. These tests pin the second one.

Two guarantees, pinned where they live:

* **Ordering** — the auto-title runs with the root FREE. Pinned by having the
  title itself try to take the lease (and, in the incident's own shape, by
  having it send a whole second message on the same root).
* **Evidence** — a refused send leaves a durable row. Before this, every durable
  write in the lane lived inside the lease, so a send refused on the way to that
  lease wrote nothing anywhere and a lost operator message was undiagnosable.

The structural half of the ordering guarantee (the title is *packaged*, never
called, inside the commit phase) is pinned by AST guard in
``tests/hermes_cli/test_mission_chat_title_offpath.py``. These are behavioral.
"""

from __future__ import annotations

import json

import pytest

from agent_runtime.events import EventLog
from agent_runtime.mission_chat_outcome import MissionChatDeferredFinalization
from agent_runtime.persona_chat_continuity import (
    PersonaChatBusyError,
    persona_chat_root_lease,
)


# The chat lane refuses ``unsupported_persona`` without a roster, so every test
# below would stop short of the lease it is about to assert on.
pytestmark = pytest.mark.usefixtures("persisted_persona_samples")


ROOT = "persona_chat_personainst_dev"
OPERATOR_TEXT = "please answer"


def _install_chat_lane(monkeypatch, reply: str = "the recorded reply"):
    """Wire the real ``_cmd_mission_chat_message`` onto a stub provider + DB.

    Same rig ``test_agent_chat_log_path`` uses to drive this handler; the point
    is that the LEASE and the finalization tail are the production ones.
    """

    from types import SimpleNamespace

    from hermes_cli import harness
    from tests.agent_runtime.test_persona_assignments import (
        _TranscriptDB,
        _assignment_config,
    )

    class _ProviderSpy:
        def __init__(self, *args, **kwargs):
            pass

        def mission_chat_reply(self, persona, message, **kwargs):
            return SimpleNamespace(
                final_response=reply,
                input_tokens=1,
                output_tokens=2,
                total_tokens=3,
                raw={},
            )

    monkeypatch.setattr(harness, "load_agent_runtime_config", _assignment_config)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: _TranscriptDB())
    monkeypatch.setattr(harness, "GPTPersonaRuntime", _ProviderSpy)
    return harness


def _args(client_message_id: str):
    from tests.agent_runtime.test_persona_assignments import _mission_chat_test_args

    return _mission_chat_test_args(client_message_id)


def _envelopes(capsys) -> list[dict]:
    """Decode every JSON object on stdout.

    ``emit_json`` pretty-prints, so the frames are multi-line and a line-wise
    parser silently finds nothing — which would make every assertion below pass
    vacuously against an empty list. Scan with ``raw_decode`` instead.
    """

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


# --------------------------------------------------------------------------- #
# Ordering: the lease is released BEFORE the finalization tail runs            #
# --------------------------------------------------------------------------- #


def test_the_root_lease_is_released_before_the_auto_title_runs(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """The direct pin: from inside the title, the root must be acquirable.

    ``persona_chat_root_lease`` is an OS file lock taken on a fresh descriptor,
    so this is a real answer even from the same process — a still-held lease
    raises ``PersonaChatBusyError`` here exactly as it did for the operator.
    """

    harness = _install_chat_lane(monkeypatch)
    observed = {}

    def _titling_probe(**kwargs):
        try:
            with persona_chat_root_lease(ROOT, observer_kind="cli"):
                observed["root_free_during_title"] = True
        except PersonaChatBusyError:
            observed["root_free_during_title"] = False

    monkeypatch.setattr(harness, "_maybe_auto_title_persona_chat", _titling_probe)

    assert harness._cmd_mission_chat_message(_args("cm-lease-1")) == 0
    capsys.readouterr()

    assert "root_free_during_title" in observed, (
        "the auto-title never ran at all — the deferred thunk is not being "
        "invoked, so this test is not pinning anything"
    )
    assert observed["root_free_during_title"] is True, (
        "the chat-root lease was STILL HELD while the auto-title ran. That is "
        "the 2026-08-09 defect verbatim: the operator's next send is refused "
        "chat_busy for the whole duration of an auxiliary-LLM round trip that "
        "writes nothing to the root"
    )


def test_a_second_send_on_the_same_root_succeeds_while_the_title_is_in_flight(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """The incident's own shape, reproduced end to end.

    Tony's follow-up landed *during* the title tail. Here the title tail IS the
    thing sending the follow-up, so "the second send is not refused" is asserted
    at exactly the moment that failed live — no threads, no timing.
    """

    harness = _install_chat_lane(monkeypatch)
    follow_up = {}

    def _titling_sends_a_follow_up(**kwargs):
        if follow_up:
            return  # the nested turn titles too; do not recurse forever
        follow_up["code"] = harness._cmd_mission_chat_message(_args("cm-lease-follow-up"))

    monkeypatch.setattr(harness, "_maybe_auto_title_persona_chat", _titling_sends_a_follow_up)

    assert harness._cmd_mission_chat_message(_args("cm-lease-2")) == 0
    frames = _envelopes(capsys)

    assert follow_up.get("code") == 0, (
        "the follow-up sent during the first turn's title tail was refused; "
        f"exit code {follow_up.get('code')!r}"
    )
    refusals = [f for f in frames if f.get("error_kind") == "chat_busy"]
    assert refusals == [], f"a chat_busy refusal was emitted for the follow-up: {refusals}"


def test_a_failing_title_no_longer_marks_the_turn(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """Stated failure semantics, pinned.

    Moving the tail past the lease also moves it past the point where the exit
    code is decided. A title that raises can no longer reach the commit phase's
    crash-tail guard — it is swallowed by ``run_once`` — so the turn stays
    ``ok`` and stdout keeps its one-JSON-object contract. This is a change, and
    it is the intended direction: the turn was complete, durable and reported
    before the title was ever attempted.
    """

    harness = _install_chat_lane(monkeypatch)

    def _boom(**kwargs):
        raise RuntimeError("title provider exhausted the fallback chain")

    monkeypatch.setattr(harness, "_maybe_auto_title_persona_chat", _boom)

    assert harness._cmd_mission_chat_message(_args("cm-lease-3")) == 0
    frames = _envelopes(capsys)
    terminal = [f for f in frames if f.get("capability_id") == "mission.chat.message"]
    assert terminal, "no terminal mission.chat.message frame was emitted"
    assert terminal[-1].get("ok") is True
    assert len(terminal) == 1, "a title failure added a second JSON object to stdout"


# --------------------------------------------------------------------------- #
# Evidence: a refused send leaves a durable row                                #
# --------------------------------------------------------------------------- #


def test_a_send_refused_by_a_busy_root_records_a_durable_event(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    harness = _install_chat_lane(monkeypatch)

    with persona_chat_root_lease(ROOT, owner_id="held-by-the-test", observer_kind="cli"):
        assert harness._cmd_mission_chat_message(_args("cm-refused-1")) == 2

    frames = _envelopes(capsys)
    refusals = [f for f in frames if f.get("error_kind") == "chat_busy"]
    assert refusals, f"expected a typed chat_busy refusal, got {frames}"

    rows = [e for e in EventLog().tail(20) if e.type == "persona_chat.send_refused"]
    assert len(rows) == 1, (
        "a refused send left no durable record. Every durable write in this lane "
        "lives inside the lease the refusal never acquired, so without this event "
        "a message lost to a busy root is unrecoverable AND undiagnosable"
    )
    payload = rows[0].payload
    assert payload["root_chat_session_id"] == ROOT
    assert payload["client_message_id"] == "cm-refused-1"
    assert payload["error_kind"] == "chat_busy"
    # The refusing holder is named, so "who was busy" is answerable after the
    # fact rather than only from a lock file that is deleted on release.
    assert isinstance(payload["lease_owner_pid"], int)
    assert rows[0].ts, "the record must carry a timestamp"
    assert rows[0].session_id == ROOT


def test_the_refusal_record_never_carries_the_operator_message_text(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """The text stays on the client. Non-negotiable, and easy to regress.

    Operator prose has exactly two sanitising chokepoints, both inside the lease
    a refusal never takes. ``events.jsonl`` feeds the snapshot/read-model
    pipeline, so text written here is text shipped to every consumer of it.
    """

    harness = _install_chat_lane(monkeypatch)

    with persona_chat_root_lease(ROOT, observer_kind="cli"):
        harness._cmd_mission_chat_message(_args("cm-refused-2"))
    capsys.readouterr()

    rows = [e for e in EventLog().tail(20) if e.type == "persona_chat.send_refused"]
    assert rows, "no refusal record to check"
    wire = json.dumps(rows[0].payload)
    assert OPERATOR_TEXT not in wire, (
        f"the operator's message text leaked into the refusal event: {wire}"
    )


def test_the_refusal_record_satisfies_its_registered_contract(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """``EventLog.append`` refuses an unregistered type, and every emitter here
    sits inside a ``try/except`` — so a missing registration is a SILENT drop of
    the record, which is the exact failure mode this event exists to end."""

    from agent_runtime.decision_contract_registry import event_catalog, validate_event_payload
    from agent_runtime.events import ALLOWED_EVENT_TYPES

    assert "persona_chat.send_refused" in ALLOWED_EVENT_TYPES
    assert "persona_chat.send_refused" in event_catalog()

    harness = _install_chat_lane(monkeypatch)
    with persona_chat_root_lease(ROOT, observer_kind="cli"):
        harness._cmd_mission_chat_message(_args("cm-refused-3"))
    capsys.readouterr()

    rows = [e for e in EventLog().tail(20) if e.type == "persona_chat.send_refused"]
    assert rows
    assert validate_event_payload("persona_chat.send_refused", rows[0].payload) == ()


# --------------------------------------------------------------------------- #
# Convergence: "the root is busy" is FOUR answers, not one (2026-08-24)        #
# --------------------------------------------------------------------------- #
#
# Incident shape: the Launcher's 60 s streaming-inactivity fallback re-presented
# its OWN still-running ``client_message_id`` while Neko's synchronous
# ``agent_chat_send`` ran for 66 frameless seconds. The root lease was held by
# that very turn, so the duplicate died ``chat_busy`` — a refusal that says
# "someone else has the root, your message did not land". The Launcher believed
# it, painted the delivered turn as rejected, and the agent's reply committed
# 20 s later into a feed whose stream subscription was already dead.
#
# The dedupe/idempotent-replay machinery that would have answered correctly
# lives INSIDE ``_mission_chat_commit_turn``, i.e. after the lease. These pin
# that the busy seam now reads the (lease-free) turn journal first and tells
# apart: this message still running, this message already answered, this message
# unprovable, and somebody else's turn.


def _journal_bytes() -> dict[str, bytes]:
    """Every turn-journal file, verbatim. The read-only fence's evidence."""

    from agent_runtime.mission_chat_turns import _store_dir

    root = _store_dir()
    if not root.exists():
        return {}
    return {path.name: path.read_bytes() for path in sorted(root.glob("*.json"))}


def _seed_journal(client_message_id: str, state: str, *, stored_reply=None) -> None:
    """Walk a journal record to ``state`` through the real transition table."""

    from agent_runtime.mission_chat_turns import (
        TURN_STATE_EXECUTING,
        TURN_STATE_NATIVE_COMMITTED,
        TURN_STATE_PENDING,
        TURN_STATE_PROJECTED,
        MissionChatTurnPersistOutcome,
        transition_mission_chat_turn,
    )

    walk = [TURN_STATE_PENDING, TURN_STATE_EXECUTING, TURN_STATE_NATIVE_COMMITTED, TURN_STATE_PROJECTED]
    for step in walk[: walk.index(state) + 1]:
        metadata = {"root_chat_session_id": ROOT}
        if step == TURN_STATE_EXECUTING:
            metadata["provider_submitted"] = True
        if stored_reply is not None and step in (
            TURN_STATE_NATIVE_COMMITTED,
            TURN_STATE_PROJECTED,
        ):
            metadata["stored_reply"] = stored_reply
        outcome = transition_mission_chat_turn(
            session_id=ROOT,
            client_message_id=client_message_id,
            turn_id=client_message_id,
            state=step,
            metadata=metadata,
        )
        assert outcome == MissionChatTurnPersistOutcome.PERSISTED, (
            f"seeding {client_message_id} -> {step} did not persist: {outcome}"
        )


@pytest.mark.parametrize("state", ["pending", "executing"])
def test_a_duplicate_of_the_running_turn_is_not_chat_busy(
    monkeypatch, capsys, isolate_agent_runtime_root, state
):
    """The whole incident, at the seam that caused it.

    ``chat_busy`` here is a lie about a message that DID land: the root is busy
    running this exact ``client_message_id``. The honest answer is non-terminal
    — do not resend a new id, do not resolve, re-present THIS id later.
    """

    harness = _install_chat_lane(monkeypatch)
    _seed_journal("cm-inflight", state)
    before = _journal_bytes()

    with persona_chat_root_lease(ROOT, owner_id="the-running-turn", observer_kind="cli"):
        assert harness._cmd_mission_chat_message(_args("cm-inflight")) == 2

    frames = [f for f in _envelopes(capsys) if f.get("capability_id") == "mission.chat.message"]
    assert frames, "no terminal frame was emitted"
    refusal = frames[-1]
    assert refusal["error_kind"] == "chat_turn_duplicate_in_flight", (
        "a duplicate of the CURRENTLY RUNNING turn was answered "
        f"{refusal['error_kind']!r}. chat_busy tells the caller its message "
        "never landed, which is how a delivered turn gets painted as rejected"
    )
    assert refusal["execution_state"] == "blocked"
    assert refusal["duplicate_in_flight"] is True
    assert refusal["chat_busy"] is False
    assert refusal["turn_resolution_required"] is False
    assert refusal["client_message_id"] == "cm-inflight"
    assert refusal["journal_state"] == state
    assert _journal_bytes() == before, (
        "the busy seam WROTE to the turn journal. It runs outside the lease it "
        "just failed to acquire, against a root another turn is actively "
        "writing — every branch there must be read-only"
    )


def test_the_duplicate_in_flight_refusal_leaves_its_own_forensics_row(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    harness = _install_chat_lane(monkeypatch)
    _seed_journal("cm-inflight-evidence", "executing")

    with persona_chat_root_lease(ROOT, observer_kind="cli"):
        harness._cmd_mission_chat_message(_args("cm-inflight-evidence"))
    capsys.readouterr()

    rows = [e for e in EventLog().tail(20) if e.type == "persona_chat.send_refused"]
    assert len(rows) == 1, "the new refusal kind lost the durable counter-record"
    assert rows[0].payload["error_kind"] == "chat_turn_duplicate_in_flight"
    assert rows[0].payload["client_message_id"] == "cm-inflight-evidence"
    wire = json.dumps(rows[0].payload)
    assert OPERATOR_TEXT not in wire, f"operator text leaked into the row: {wire}"


def test_a_busy_root_running_a_DIFFERENT_message_is_still_chat_busy(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """The narrowing must be about identity, not about busyness.

    A root busy with somebody else's turn is exactly today's refusal: the
    operator's message really did not land, and ``chat_busy`` is the truth.
    """

    harness = _install_chat_lane(monkeypatch)
    _seed_journal("cm-somebody-elses-turn", "executing")

    with persona_chat_root_lease(ROOT, observer_kind="cli"):
        assert harness._cmd_mission_chat_message(_args("cm-mine")) == 2

    frames = [f for f in _envelopes(capsys) if f.get("capability_id") == "mission.chat.message"]
    assert frames[-1]["error_kind"] == "chat_busy", (
        "a send that lost the root to ANOTHER message was answered "
        f"{frames[-1]['error_kind']!r}; only the running message's own duplicate "
        "may take the convergence branch"
    )
    assert frames[-1]["chat_busy"] is True


@pytest.mark.parametrize("state", ["native_committed", "projected"])
def test_a_duplicate_of_an_ANSWERED_turn_replays_read_only(
    monkeypatch, capsys, isolate_agent_runtime_root, state
):
    """The convergence loop's landing: the reply is served while the root is busy.

    The turn committed; a DIFFERENT turn now holds the lease. The stored reply
    is already durable in the journal, so it is served from there — with no
    transition, and deliberately without the projection event (that helper
    writes a ``projection_event_emitted`` marker through the journal).
    """

    harness = _install_chat_lane(monkeypatch)
    _seed_journal("cm-answered", state, stored_reply="the recorded reply")
    before = _journal_bytes()

    with persona_chat_root_lease(ROOT, owner_id="a-later-turn", observer_kind="cli"):
        assert harness._cmd_mission_chat_message(_args("cm-answered")) == 0

    frames = [f for f in _envelopes(capsys) if f.get("capability_id") == "mission.chat.message"]
    assert frames[-1]["ok"] is True
    assert frames[-1]["idempotent_replay"] is True
    assert frames[-1]["reply"] == "the recorded reply"
    assert frames[-1]["journal_state"] == state
    assert _journal_bytes() == before, (
        "the read-only replay branch wrote to the turn journal while another "
        "turn held the chat root — an unleased write against a live root"
    )


def test_a_duplicate_of_an_unprovable_turn_still_routes_to_turn_resolve(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    harness = _install_chat_lane(monkeypatch)
    _seed_journal("cm-unknown", "executing")
    from agent_runtime.mission_chat_turns import transition_mission_chat_turn

    transition_mission_chat_turn(
        session_id=ROOT,
        client_message_id="cm-unknown",
        turn_id="cm-unknown",
        state="outcome_unknown",
        metadata={"provider_submitted": True},
    )
    before = _journal_bytes()

    with persona_chat_root_lease(ROOT, observer_kind="cli"):
        assert harness._cmd_mission_chat_message(_args("cm-unknown")) == 2

    frames = [f for f in _envelopes(capsys) if f.get("capability_id") == "mission.chat.message"]
    assert frames[-1]["error_kind"] == "chat_turn_outcome_unknown"
    assert _journal_bytes() == before


def test_an_UNLEASED_resend_of_an_executing_turn_keeps_todays_path(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """The seam only fires when the lease is HELD.

    A process that died mid-turn leaves an ``executing`` record and a free root.
    That resend must still flip to ``outcome_unknown`` inside the lease and
    demand a resolve — nothing about this change relaxes it.
    """

    harness = _install_chat_lane(monkeypatch)
    _seed_journal("cm-orphan", "executing")

    assert harness._cmd_mission_chat_message(_args("cm-orphan")) == 2
    frames = [f for f in _envelopes(capsys) if f.get("capability_id") == "mission.chat.message"]
    assert frames[-1]["error_kind"] == "chat_turn_outcome_unknown"

    from agent_runtime.mission_chat_turns import mission_chat_turn_record

    record = mission_chat_turn_record(session_id=ROOT, client_message_id="cm-orphan")
    assert record["state"] == "outcome_unknown", (
        "the leased path no longer settles an orphaned executing turn"
    )


# --------------------------------------------------------------------------- #
# The carrier itself                                                           #
# --------------------------------------------------------------------------- #


def test_deferred_finalization_runs_its_thunk_exactly_once():
    calls = []
    deferred = MissionChatDeferredFinalization()
    deferred.defer(lambda: calls.append(1))

    assert deferred.run_once() is True
    assert deferred.run_once() is False, "a second run would double the tail"
    assert calls == [1]


def test_deferred_finalization_swallows_a_raising_thunk():
    deferred = MissionChatDeferredFinalization()

    def _boom():
        raise RuntimeError("aux provider chain exhausted")

    deferred.defer(_boom)
    # Past this point the turn's exit code is already decided and its terminal
    # frame is already on stdout; a decoration failure may not change either.
    assert deferred.run_once() is False


def test_deferred_finalization_refuses_a_second_thunk():
    deferred = MissionChatDeferredFinalization()
    deferred.defer(lambda: None)
    with pytest.raises(ValueError):
        deferred.defer(lambda: None)


def test_deferred_finalization_is_a_no_op_when_nothing_was_packaged():
    assert MissionChatDeferredFinalization().run_once() is False
