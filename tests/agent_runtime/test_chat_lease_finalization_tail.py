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
