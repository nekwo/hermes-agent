"""Performance-slice coverage for the mission-chat turn store.

P1: streamed deltas debounce the incremental on_update flush to at most one
    store rewrite per interval; segment boundaries and tool events flush
    immediately; no accumulated text is ever lost to suppression.
P2: retention bounds the store on write (per-session tail + session count)
    inside the _mutate_store lock, never evicting running records or the
    record being written, invisibly to the caller's typed outcome.

The durable lane (write-ahead ``running`` marker, terminal
``completed``/``failed`` persists, ``mark_stale`` repair) is handler-owned and
never passes through the debounced path — asserted here by construction:
only ``delta()``-driven notifies are debounced.
"""

from __future__ import annotations

import pytest

from agent_runtime import mission_chat_turns
from agent_runtime.mission_chat_turns import (
    MissionChatTurnPersistOutcome,
    mission_chat_turn_record,
    mission_chat_turn_records,
    persist_mission_chat_turn,
)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def store_write_counter(monkeypatch):
    """Count REAL turn-store file writes (post-lock, post-mutation)."""

    counter = {"writes": 0}
    original = mission_chat_turns._write_store

    def _counting_write(data):
        counter["writes"] += 1
        original(data)

    monkeypatch.setattr(mission_chat_turns, "_write_store", _counting_write)
    return counter


def _emitter(clock, *, session_id="s1", client_message_id="m1"):
    from hermes_cli import harness

    return harness._ChatProtocolV2Emitter(
        turn_id="turn_perf",
        client_message_id=client_message_id,
        emit_frames=False,
        clock=clock,
        on_update=lambda em: persist_mission_chat_turn(
            session_id=session_id,
            client_message_id=client_message_id,
            turn_id=em.turn_id,
            elements=em.elements,
            state="running",
        ),
    )


def _interval() -> float:
    from hermes_cli import harness

    return harness._CHAT_TURN_INCREMENTAL_FLUSH_INTERVAL_SECONDS


# ---------------------------------------------------------------------------
# P1 — debounced incremental flushes
# ---------------------------------------------------------------------------


def test_deltas_within_one_interval_flush_once(store_write_counter):
    clock = _FakeClock()
    emitter = _emitter(clock)

    for index in range(50):
        emitter.delta(f"chunk{index} ")

    assert store_write_counter["writes"] == 1
    record = mission_chat_turn_record(session_id="s1", client_message_id="m1")
    assert record["state"] == "running"


def test_deltas_across_intervals_flush_once_per_interval(store_write_counter):
    clock = _FakeClock()
    emitter = _emitter(clock)
    interval = _interval()

    # 10 deltas per interval window, across 4 windows.
    for window in range(4):
        for index in range(10):
            emitter.delta(f"w{window}c{index} ")
            clock.advance(interval / 10)

    assert store_write_counter["writes"] == 4


def test_tool_event_mid_interval_flushes_immediately_with_full_text(
    store_write_counter,
):
    clock = _FakeClock()
    emitter = _emitter(clock)
    interval = _interval()

    emitter.delta("Hel")  # first delta flushes (opens the window)
    clock.advance(interval / 5)
    emitter.delta("lo")  # suppressed: within the window
    assert store_write_counter["writes"] == 1
    persisted = mission_chat_turn_record(session_id="s1", client_message_id="m1")
    assert persisted["elements"][0]["text"] == "Hel"

    clock.advance(interval / 5)  # still inside the window
    emitter.progress(
        {"type": "run.tool.started", "tool_name": "terminal", "summary": "run tests"}
    )

    # The tool event flushed immediately even though the interval had not
    # elapsed, and its flush carried the suppressed delta's text — the last
    # delta before a suppressed window is never lost.
    assert store_write_counter["writes"] > 1
    record = mission_chat_turn_record(session_id="s1", client_message_id="m1")
    kinds = [element["kind"] for element in record["elements"]]
    assert kinds == ["segment", "tool"]
    assert record["elements"][0]["text"] == "Hello"


def test_terminal_persist_carries_complete_text_after_suppressed_deltas(
    store_write_counter,
):
    clock = _FakeClock()
    emitter = _emitter(clock)

    for index in range(50):
        emitter.delta(f"chunk{index} ")
    assert store_write_counter["writes"] == 1  # 49 deltas suppressed

    emitter.finish(state="completed")
    outcome = persist_mission_chat_turn(
        session_id="s1",
        client_message_id="m1",
        turn_id=emitter.turn_id,
        elements=emitter.elements,
        state="completed",
    )

    assert outcome is MissionChatTurnPersistOutcome.PERSISTED
    record = mission_chat_turn_record(session_id="s1", client_message_id="m1")
    assert record["state"] == "completed"
    expected = " ".join(f"chunk{index}" for index in range(50))
    assert record["elements"][0]["text"] == expected


def test_segment_end_flushes_immediately(store_write_counter):
    clock = _FakeClock()
    emitter = _emitter(clock)
    interval = _interval()

    emitter.delta("partial")
    clock.advance(interval / 5)
    emitter.end_segment(state="settled")

    assert store_write_counter["writes"] == 2
    record = mission_chat_turn_record(session_id="s1", client_message_id="m1")
    assert record["elements"][0]["state"] == "settled"


# ---------------------------------------------------------------------------
# P2 — retention inside the locked mutation
# ---------------------------------------------------------------------------


def _persist_completed(session_id: str, client_message_id: str) -> None:
    outcome = persist_mission_chat_turn(
        session_id=session_id,
        client_message_id=client_message_id,
        turn_id=f"turn_{client_message_id}",
        elements=[],
        state="completed",
    )
    assert outcome is MissionChatTurnPersistOutcome.PERSISTED


def test_per_session_tail_bound_keeps_most_recent(monkeypatch):
    monkeypatch.setattr(mission_chat_turns, "_RETENTION_MAX_TURNS_PER_SESSION", 5)
    for index in range(8):
        _persist_completed("s1", f"m{index}")

    kept = [item["client_message_id"] for item in mission_chat_turn_records(session_id="s1")]
    assert kept == ["m3", "m4", "m5", "m6", "m7"]


def test_session_bound_drops_oldest_sessions_wholesale(monkeypatch):
    monkeypatch.setattr(mission_chat_turns, "_RETENTION_MAX_SESSIONS", 3)
    for index in range(5):
        _persist_completed(f"s{index}", "m1")

    store = mission_chat_turns._read_store()
    assert sorted(store.keys()) == ["s2", "s3", "s4"]


def test_running_records_survive_per_session_retention(monkeypatch):
    monkeypatch.setattr(mission_chat_turns, "_RETENTION_MAX_TURNS_PER_SESSION", 3)
    persist_mission_chat_turn(
        session_id="s1",
        client_message_id="m_live",
        turn_id="turn_live",
        elements=[],
        state="running",
        write_ahead=True,
    )
    for index in range(5):
        _persist_completed("s1", f"m{index}")

    records = {
        item["client_message_id"]: item["state"]
        for item in mission_chat_turn_records(session_id="s1")
    }
    # The write-ahead marker is the oldest record yet outlives every eviction.
    assert records["m_live"] == "running"
    assert set(records) == {"m_live", "m3", "m4"}


def test_session_with_running_record_never_dropped_wholesale(monkeypatch):
    monkeypatch.setattr(mission_chat_turns, "_RETENTION_MAX_SESSIONS", 2)
    persist_mission_chat_turn(
        session_id="s_live",
        client_message_id="m_live",
        turn_id="turn_live",
        elements=[],
        state="running",
        write_ahead=True,
    )
    for index in range(3):
        _persist_completed(f"s{index}", "m1")

    store = mission_chat_turns._read_store()
    assert "s_live" in store
    record = mission_chat_turn_record(session_id="s_live", client_message_id="m_live")
    assert record["state"] == "running"


def test_active_record_immune_even_when_oldest():
    # Direct retention unit: the record being written is protected even when
    # its timestamp ranks it for eviction.
    store = {
        "s1": {
            "m_old_active": {"state": "completed", "updated_at": "2026-01-01T00:00:00Z"},
            "m_mid": {"state": "completed", "updated_at": "2026-01-02T00:00:00Z"},
            "m_new": {"state": "completed", "updated_at": "2026-01-03T00:00:00Z"},
        }
    }
    original_turns = mission_chat_turns._RETENTION_MAX_TURNS_PER_SESSION
    mission_chat_turns._RETENTION_MAX_TURNS_PER_SESSION = 2
    try:
        mission_chat_turns._apply_retention(
            store, protected_session="s1", protected_message="m_old_active"
        )
    finally:
        mission_chat_turns._RETENTION_MAX_TURNS_PER_SESSION = original_turns

    assert sorted(store["s1"].keys()) == ["m_new", "m_old_active"]


def test_active_session_immune_to_session_bound():
    store = {
        "s_old_active": {
            "m1": {"state": "completed", "updated_at": "2026-01-01T00:00:00Z"}
        },
        "s_mid": {"m1": {"state": "completed", "updated_at": "2026-01-02T00:00:00Z"}},
        "s_new": {"m1": {"state": "completed", "updated_at": "2026-01-03T00:00:00Z"}},
    }
    original_sessions = mission_chat_turns._RETENTION_MAX_SESSIONS
    mission_chat_turns._RETENTION_MAX_SESSIONS = 2
    try:
        mission_chat_turns._apply_retention(
            store, protected_session="s_old_active", protected_message="m1"
        )
    finally:
        mission_chat_turns._RETENTION_MAX_SESSIONS = original_sessions

    assert sorted(store.keys()) == ["s_new", "s_old_active"]


def test_retention_is_invisible_to_the_typed_outcome(monkeypatch):
    monkeypatch.setattr(mission_chat_turns, "_RETENTION_MAX_TURNS_PER_SESSION", 2)
    for index in range(4):
        outcome = persist_mission_chat_turn(
            session_id="s1",
            client_message_id=f"m{index}",
            turn_id=f"turn_{index}",
            elements=[],
            state="completed",
        )
        assert outcome is MissionChatTurnPersistOutcome.PERSISTED

    assert len(mission_chat_turn_records(session_id="s1")) == 2


def test_defaults_exceed_projection_message_tail():
    # The projection displays at most MAX_PERSONA_CHAT_MESSAGE_TAIL rows per
    # session; retention must keep at least that many turn records so no
    # displayable agent row can lose its turn_elements.
    from agent_runtime.persona_chat_history import MAX_PERSONA_CHAT_MESSAGE_TAIL

    assert mission_chat_turns._RETENTION_MAX_TURNS_PER_SESSION >= (
        MAX_PERSONA_CHAT_MESSAGE_TAIL * 2
    )
