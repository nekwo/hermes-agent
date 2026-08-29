"""Stage 3b — ``process notify``: end the turn, get the receipt on exit.

The contract this pins: an agent fires a long verb in the background, calls
``process notify`` on the session, and ENDS ITS TURN. When the process exits,
the completion becomes a delivery turn in the agent's own chat thread — the
same forged-turn path a background ``agent_chat_send`` reply already travels
(``agent_runtime.dispatch_delivery.drain_background_completions`` →
``forge_delivery_turn``). Nothing here builds a second delivery road: this file
proves the ARMING half and the EXIT half, and that the event they produce is
shaped exactly like the one the existing drain already consumes.

Four edges, all measured against the plan
(docs/agent-runtime-harness/planned/charsheet-turn-efficiency-2026-08-29.md,
Stage 3b):

* the process has ALREADY exited when notify is called — deliver immediately,
  never lose the wake-up;
* the process never exits — nothing is delivered and no timer is invented; the
  turn wall/budget system is the guard, and an agent that chooses to block
  instead is bounded by the Stage 3a wait ceiling;
* duplicate notify calls are idempotent — one row, one delivery;
* the requesting persona instance is gone by exit time — the row is DROPPED
  with a logged line and the reaper survives.
"""

from __future__ import annotations

import json
import logging
import time

import pytest

from agent_runtime.persona_chat_continuity import chat_root_session_key_scope
from tools.process_registry import ProcessRegistry, ProcessSession

ROOT = "persona_chat_personainst_chara_a2_7b31d0e4_a238c5f9c4c2"
INSTANCE = "personainst_chara_a2_7b31d0e4"
PERSONA = "chara_a2"


@pytest.fixture()
def notify_home(tmp_path, monkeypatch):
    """Isolate the drain file — it lives beside ``processes.json``."""

    import hermes_constants

    home = tmp_path / "background-home"
    home.mkdir()
    monkeypatch.setattr(
        hermes_constants, "get_hermes_background_work_home", lambda: home
    )
    import tools.process_notify_store as store

    store.reset_cache()
    return home


@pytest.fixture()
def owned_root(monkeypatch):
    """The chat root resolves to a live persona instance."""

    import agent_runtime.persona_assignments as assignments

    monkeypatch.setattr(
        assignments,
        "chat_session_owner_persona",
        lambda session_id: (PERSONA, INSTANCE) if session_id == ROOT else None,
    )


def _session(sid="proc_rows", exited=False, exit_code=None) -> ProcessSession:
    return ProcessSession(
        id=sid,
        command="hermes harness characters rows --draft d1 --json",
        task_id="t1",
        session_key=ROOT,
        started_at=time.time(),
        exited=exited,
        exit_code=exit_code,
        output_buffer="row 1 ok\nrow 2 ok\n",
    )


def _arm(registry: ProcessRegistry, session: ProcessSession) -> dict:
    with chat_root_session_key_scope(ROOT):
        return registry.notify_on_exit(session.id)


def _queued(registry: ProcessRegistry) -> list[dict]:
    events = []
    while not registry.completion_queue.empty():
        events.append(registry.completion_queue.get_nowait())
    return events


# ── the durable drain file ──────────────────────────────────────────────────


class TestNotifyStore:
    def test_a_request_is_recorded_with_the_three_named_fields(self, notify_home):
        from tools.process_notify_store import (
            STATE_PENDING,
            pending_notify_request,
            record_notify_request,
        )

        row, created = record_notify_request(
            session_id="proc_a",
            chat_session_id=ROOT,
            persona_instance_id=INSTANCE,
            persona_id=PERSONA,
            turn_id="agent-chat-send-abc",
        )
        assert created is True
        assert row["state"] == STATE_PENDING
        assert row["session_id"] == "proc_a"
        assert row["persona_instance_id"] == INSTANCE
        assert row["turn_id"] == "agent-chat-send-abc"
        assert row["chat_session_id"] == ROOT
        again = pending_notify_request("proc_a")
        assert again is not None and again["turn_id"] == "agent-chat-send-abc"

    def test_the_file_lands_beside_the_process_checkpoint(self, notify_home):
        from tools.process_notify_store import notify_store_path, record_notify_request

        record_notify_request(
            session_id="proc_a", chat_session_id=ROOT, persona_instance_id=INSTANCE
        )
        path = notify_store_path()
        assert path.parent == notify_home
        assert path.exists()
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)

    def test_recording_twice_is_one_row(self, notify_home):
        from tools.process_notify_store import notify_requests, record_notify_request

        first, created_first = record_notify_request(
            session_id="proc_a", chat_session_id=ROOT, persona_instance_id=INSTANCE
        )
        second, created_second = record_notify_request(
            session_id="proc_a", chat_session_id=ROOT, persona_instance_id=INSTANCE
        )
        assert created_first is True
        assert created_second is False
        assert second["requested_at"] == first["requested_at"]
        assert len(notify_requests()) == 1

    def test_settling_clears_the_pending_answer(self, notify_home):
        from tools.process_notify_store import (
            STATE_FIRED,
            pending_notify_request,
            record_notify_request,
            settle_notify_request,
        )

        record_notify_request(
            session_id="proc_a", chat_session_id=ROOT, persona_instance_id=INSTANCE
        )
        assert settle_notify_request("proc_a", state=STATE_FIRED) is True
        assert pending_notify_request("proc_a") is None

    def test_the_row_ceiling_evicts_settled_rows_first(self, notify_home):
        from tools.process_notify_store import (
            MAX_NOTIFY_ROWS,
            STATE_FIRED,
            notify_requests,
            pending_notify_request,
            record_notify_request,
            settle_notify_request,
        )

        for index in range(MAX_NOTIFY_ROWS):
            record_notify_request(
                session_id=f"proc_settled_{index:03d}",
                chat_session_id=ROOT,
                persona_instance_id=INSTANCE,
            )
            settle_notify_request(f"proc_settled_{index:03d}", state=STATE_FIRED)
        record_notify_request(
            session_id="proc_live", chat_session_id=ROOT, persona_instance_id=INSTANCE
        )
        assert len(notify_requests()) <= MAX_NOTIFY_ROWS
        # The promise nobody has kept yet is the LAST thing thrown away.
        assert pending_notify_request("proc_live") is not None

    def test_evicting_an_undelivered_promise_is_never_silent(
        self, notify_home, caplog
    ):
        from tools.process_notify_store import MAX_NOTIFY_ROWS, record_notify_request

        with caplog.at_level(logging.WARNING, logger="tools.process_notify_store"):
            for index in range(MAX_NOTIFY_ROWS + 2):
                record_notify_request(
                    session_id=f"proc_pending_{index:03d}",
                    chat_session_id=ROOT,
                    persona_instance_id=INSTANCE,
                )
        assert any("evicted un-delivered" in r.getMessage() for r in caplog.records)

    def test_a_corrupt_file_never_raises(self, notify_home):
        from tools.process_notify_store import (
            notify_requests,
            notify_store_path,
            pending_notify_request,
            record_notify_request,
            reset_cache,
        )

        record_notify_request(
            session_id="proc_a", chat_session_id=ROOT, persona_instance_id=INSTANCE
        )
        notify_store_path().write_text("{not json", encoding="utf-8")
        reset_cache()
        assert pending_notify_request("proc_a") is None
        assert notify_requests() == []
        # And the store is still writable afterwards.
        row, created = record_notify_request(
            session_id="proc_b", chat_session_id=ROOT, persona_instance_id=INSTANCE
        )
        assert created is True


# ── arming ──────────────────────────────────────────────────────────────────


class TestArming:
    def test_arming_a_running_session_records_and_delivers_nothing_yet(
        self, notify_home, owned_root
    ):
        from tools.process_notify_store import pending_notify_request

        registry = ProcessRegistry()
        session = _session()
        registry._running[session.id] = session

        result = _arm(registry, session)

        assert result["status"] == "armed"
        assert result["already_armed"] is False
        assert result["persona_instance_id"] == INSTANCE
        row = pending_notify_request(session.id)
        assert row is not None and row["chat_session_id"] == ROOT
        # THE POINT of the lane: arming costs nothing until the process exits.
        assert _queued(registry) == []

    def test_arming_twice_is_idempotent(self, notify_home, owned_root):
        from tools.process_notify_store import notify_requests

        registry = ProcessRegistry()
        session = _session()
        registry._running[session.id] = session

        first = _arm(registry, session)
        second = _arm(registry, session)

        assert first["already_armed"] is False
        assert second["already_armed"] is True
        assert len(notify_requests()) == 1
        assert _queued(registry) == []

    def test_an_unknown_session_is_refused_and_writes_nothing(
        self, notify_home, owned_root
    ):
        from tools.process_notify_store import notify_requests

        registry = ProcessRegistry()
        with chat_root_session_key_scope(ROOT):
            result = registry.notify_on_exit("proc_nope")
        assert result["status"] == "not_found"
        assert notify_requests() == []

    def test_off_the_mission_chat_lane_notify_is_refused_honestly(
        self, notify_home, owned_root
    ):
        """No chat root anywhere means no thread to deliver into.

        Positive proof only — the #64484 rule the delivery lane already holds.
        The refusal names the fallback so the agent is not left guessing.

        Note what is NOT tested here: a session SPAWNED inside a persona run
        carries the chat root on its own ``session_key`` (stamped by
        ``chat_root_session_key_scope``), and notify honors that even when the
        call itself arrives with no scope bound. That is positive knowledge from
        the spawn, not a guess. Only a session whose key names something else —
        a gateway tab, a bare task — is refused.
        """

        from tools.process_notify_store import notify_requests

        registry = ProcessRegistry()
        session = _session()
        session.session_key = "session:telegram-123"
        registry._running[session.id] = session
        result = registry.notify_on_exit(session.id)
        assert result["status"] == "unavailable"
        assert "wait" in str(result.get("error", "")).lower()
        assert notify_requests() == []

    def test_the_spawn_stamped_root_is_honored_without_a_bound_scope(
        self, notify_home, owned_root
    ):
        """The terminal tool stamped the root at spawn; that is knowledge."""

        registry = ProcessRegistry()
        session = _session()
        registry._running[session.id] = session
        result = registry.notify_on_exit(session.id)
        assert result["status"] == "armed"
        assert result["chat_session_id"] == ROOT

    def test_a_root_owned_by_a_different_instance_is_refused(
        self, notify_home, monkeypatch
    ):
        """A re-owned thread is not this agent's thread."""

        import agent_runtime.persona_assignments as assignments
        from tools.process_notify_store import notify_requests

        registry = ProcessRegistry()
        session = _session()
        registry._running[session.id] = session
        monkeypatch.setattr(
            assignments, "chat_session_owner_persona", lambda session_id: None
        )
        result = registry.notify_on_exit(session.id)
        assert result["status"] == "unavailable"
        assert notify_requests() == []


# ── exit: the reaper posts the delivery ─────────────────────────────────────


class TestExitDelivery:
    def test_exit_enqueues_a_routable_completion_for_the_armed_session(
        self, notify_home, owned_root
    ):
        from tools.process_notify_store import STATE_FIRED, notify_requests

        registry = ProcessRegistry()
        session = _session()
        registry._running[session.id] = session
        _arm(registry, session)

        session.exited = True
        session.exit_code = 0
        registry._move_to_finished(session)

        events = _queued(registry)
        assert len(events) == 1
        evt = events[0]
        assert evt["type"] == "completion"
        assert evt["session_id"] == session.id
        assert evt["exit_code"] == 0
        assert evt["notify_requested"] is True
        # The routing key the serve drain resolves a chat root from.
        assert evt["origin_ui_session_id"] == ROOT
        assert notify_requests()[0]["state"] == STATE_FIRED

    def test_the_queued_event_resolves_to_the_chat_root_the_drain_delivers_into(
        self, notify_home, owned_root, monkeypatch
    ):
        """The join: the REAL producer's event, read by the REAL consumer rule."""

        from agent_runtime import dispatch_delivery

        registry = ProcessRegistry()
        session = _session()
        registry._running[session.id] = session
        _arm(registry, session)
        session.exited = True
        registry._move_to_finished(session)
        evt = _queued(registry)[0]

        monkeypatch.setattr(
            dispatch_delivery,
            "_sender_persona",
            lambda sid: (PERSONA, INSTANCE) if sid == ROOT else None,
        )
        assert dispatch_delivery._chat_root_of_completion(evt) == ROOT

    def test_the_delivered_text_says_why_it_arrived(self, notify_home, owned_root):
        """The turn that reads this is a NEW turn; the block must stand alone."""

        from tools.process_registry import format_process_notification

        registry = ProcessRegistry()
        session = _session()
        registry._running[session.id] = session
        _arm(registry, session)
        session.exited = True
        session.exit_code = 0
        registry._move_to_finished(session)

        text = format_process_notification(_queued(registry)[0])
        assert "BACKGROUND PROCESS COMPLETE" in text
        assert "process notify" in text
        assert "rows --draft d1" in text

    def test_an_unrequested_completion_keeps_its_historical_text(self):
        from tools.process_registry import format_process_notification

        text = format_process_notification(
            {
                "type": "completion",
                "session_id": "proc_legacy",
                "command": "pytest -q",
                "exit_code": 0,
                "output": "ok",
            }
        )
        assert text.startswith("[IMPORTANT: Background process proc_legacy")

    def test_a_session_that_never_exits_delivers_nothing(self, notify_home, owned_root):
        """No timer of our own: the turn wall is the guard, by design."""

        from tools.process_notify_store import STATE_PENDING, notify_requests

        registry = ProcessRegistry()
        session = _session()
        registry._running[session.id] = session
        _arm(registry, session)

        assert _queued(registry) == []
        assert notify_requests()[0]["state"] == STATE_PENDING

    def test_an_already_exited_session_delivers_immediately(
        self, notify_home, owned_root
    ):
        from tools.process_notify_store import STATE_FIRED, notify_requests

        registry = ProcessRegistry()
        session = _session(exited=True, exit_code=0)
        registry._finished[session.id] = session

        result = _arm(registry, session)

        assert result["status"] == "exited"
        assert result["delivery"] == "queued"
        events = _queued(registry)
        assert len(events) == 1
        assert events[0]["notify_requested"] is True
        assert notify_requests()[0]["state"] == STATE_FIRED

    def test_an_already_consumed_exit_is_still_delivered_when_notify_asks(
        self, notify_home, owned_root
    ):
        """No lost wake-up: an EXPLICIT notify outranks inline consumption.

        ``_drain_should_skip`` exists so a completion the agent already read in
        this turn is not injected twice. A notify request is the agent asking
        for the out-of-turn delivery on purpose, so the skip must not swallow
        it — that would be the wake-up silently disappearing.
        """

        registry = ProcessRegistry()
        session = _session(exited=True, exit_code=0)
        registry._finished[session.id] = session
        registry._completion_consumed.add(session.id)

        _arm(registry, session)

        drained = registry.drain_notifications()
        assert len(drained) == 1
        assert drained[0][0]["notify_requested"] is True

    def test_a_retired_persona_instance_drops_the_row_and_logs(
        self, notify_home, owned_root, monkeypatch, caplog
    ):
        import agent_runtime.persona_assignments as assignments
        from tools.process_notify_store import STATE_DROPPED, notify_requests

        registry = ProcessRegistry()
        session = _session()
        registry._running[session.id] = session
        _arm(registry, session)

        # The instance is retired between arming and exit.
        monkeypatch.setattr(
            assignments, "chat_session_owner_persona", lambda session_id: None
        )
        session.exited = True
        with caplog.at_level(logging.WARNING, logger="tools.process_registry"):
            registry._move_to_finished(session)

        assert _queued(registry) == []
        assert notify_requests()[0]["state"] == STATE_DROPPED
        assert any(INSTANCE in record.getMessage() for record in caplog.records)
        # The reaper still finished its job.
        assert session.id in registry._finished

    def test_a_thread_re_owned_by_another_instance_drops_the_row(
        self, notify_home, owned_root, monkeypatch, caplog
    ):
        """Resolvable root, WRONG instance — same verdict, different cause."""

        import agent_runtime.persona_assignments as assignments
        from tools.process_notify_store import STATE_DROPPED, notify_requests

        registry = ProcessRegistry()
        session = _session()
        registry._running[session.id] = session
        _arm(registry, session)

        monkeypatch.setattr(
            assignments,
            "chat_session_owner_persona",
            lambda session_id: ("neko_supervisor", "personainst_someone_else"),
        )
        session.exited = True
        with caplog.at_level(logging.WARNING, logger="tools.process_registry"):
            registry._move_to_finished(session)

        assert _queued(registry) == []
        assert notify_requests()[0]["state"] == STATE_DROPPED
        assert caplog.records

    def test_a_store_failure_never_crashes_the_reaper(
        self, notify_home, owned_root, monkeypatch
    ):
        import tools.process_notify_store as store

        registry = ProcessRegistry()
        session = _session()
        registry._running[session.id] = session
        _arm(registry, session)

        def _boom(*args, **kwargs):
            raise OSError("drain file is gone")

        monkeypatch.setattr(store, "pending_notify_request", _boom)
        session.exited = True
        registry._move_to_finished(session)  # must not raise
        assert session.id in registry._finished

    def test_the_legacy_notify_on_complete_path_is_untouched(self, notify_home):
        """A spawn-time notify_on_complete session keeps its exact old event."""

        registry = ProcessRegistry()
        session = _session()
        session.notify_on_complete = True
        registry._running[session.id] = session

        session.exited = True
        registry._move_to_finished(session)

        events = _queued(registry)
        assert len(events) == 1
        assert "notify_requested" not in events[0]
        assert events[0]["session_key"] == ROOT


# ── the tool surface ────────────────────────────────────────────────────────


class TestProcessToolSurface:
    def test_notify_is_a_declared_action(self):
        from tools.process_registry import PROCESS_SCHEMA

        enum = PROCESS_SCHEMA["parameters"]["properties"]["action"]["enum"]
        assert "notify" in enum
        assert "notify" in PROCESS_SCHEMA["description"]

    def test_the_handler_routes_notify(self, notify_home, owned_root, monkeypatch):
        import tools.process_registry as mod

        registry = ProcessRegistry()
        session = _session()
        registry._running[session.id] = session
        monkeypatch.setattr(mod, "process_registry", registry)

        with chat_root_session_key_scope(ROOT):
            raw = mod._handle_process({"action": "notify", "session_id": session.id})
        assert json.loads(raw)["status"] == "armed"

    def test_notify_without_a_session_id_is_an_error(self, notify_home):
        import tools.process_registry as mod

        raw = mod._handle_process({"action": "notify"})
        assert "session_id is required" in raw
