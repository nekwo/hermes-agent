"""Hardening coverage for the mission-chat turn store.

W1: cross-process lock — concurrent writers never lose each other's records,
    and a held lock skips (never hangs) with a typed outcome.
W2: every skipped/rejected persist names its reason.
W3: the transition table is the single state authority — incremental
    on_update flushes cannot resurrect settled records; explicit terminal
    states and fresh write-ahead retries always win.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_runtime import mission_chat_turns
from agent_runtime.mission_chat_turns import (
    MissionChatTurnPersistOutcome,
    mark_stale_running_turns_interrupted,
    mission_chat_turn_record,
    next_turn_state,
    persist_mission_chat_turn,
)

_SEGMENT = {
    "kind": "segment",
    "id": "t1_seg_1",
    "turn_id": "t1",
    "seq": 1,
    "state": "streaming",
    "seg_type": "plan",
    "text": "partial",
}


# ---------------------------------------------------------------------------
# W3 — transition table (every cell)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "requested", "write_ahead", "expected"),
    [
        # Legacy elements-only calls preserve state; new records run.
        (None, None, False, "running"),
        ("running", None, False, "running"),
        ("completed", None, False, "completed"),
        ("failed", None, False, "failed"),
        ("interrupted", None, False, "interrupted"),
        # Explicit terminal / repair states always win.
        ("running", "completed", False, "completed"),
        ("interrupted", "completed", False, "completed"),
        ("completed", "failed", False, "failed"),
        ("running", "interrupted", False, "interrupted"),
        ("failed", "interrupted", False, "interrupted"),
        # Fresh write-ahead (same-client retry) always wins.
        (None, "running", True, "running"),
        ("running", "running", True, "running"),
        ("completed", "running", True, "running"),
        ("failed", "running", True, "running"),
        ("interrupted", "running", True, "running"),
        # Incremental on_update flushes may only continue a live turn.
        (None, "running", False, "running"),
        ("running", "running", False, "running"),
        ("completed", "running", False, None),
        ("failed", "running", False, None),
        ("interrupted", "running", False, None),
        # Invalid states are rejected regardless of flavor.
        ("running", "garbage", False, None),
        ("running", "garbage", True, None),
    ],
)
def test_next_turn_state_table(current, requested, write_ahead, expected):
    assert next_turn_state(current, requested, write_ahead=write_ahead) == expected


def test_stale_on_update_cannot_resurrect_interrupted_record():
    persist_mission_chat_turn(
        session_id="s1",
        client_message_id="m1",
        turn_id="t1",
        elements=[],
        state="running",
        write_ahead=True,
    )
    flipped = mark_stale_running_turns_interrupted(
        session_id="s1",
        active_client_message_id="m2",
    )
    assert flipped == ["m1"]

    # A late incremental flush from the (dead or superseded) first turn.
    outcome = persist_mission_chat_turn(
        session_id="s1",
        client_message_id="m1",
        turn_id="t1",
        elements=[_SEGMENT],
        state="running",
    )

    assert outcome is MissionChatTurnPersistOutcome.REJECTED_STALE_TRANSITION
    record = mission_chat_turn_record(session_id="s1", client_message_id="m1")
    assert record["state"] == "interrupted"
    assert record["elements"] == []  # stale flush must not overwrite either


def test_explicit_completed_still_wins_after_repair_flip():
    persist_mission_chat_turn(
        session_id="s1",
        client_message_id="m1",
        turn_id="t1",
        elements=[],
        state="running",
        write_ahead=True,
    )
    mark_stale_running_turns_interrupted(session_id="s1", active_client_message_id="m2")

    outcome = persist_mission_chat_turn(
        session_id="s1",
        client_message_id="m1",
        turn_id="t1",
        elements=[_SEGMENT],
        state="completed",
    )

    assert outcome is MissionChatTurnPersistOutcome.PERSISTED
    record = mission_chat_turn_record(session_id="s1", client_message_id="m1")
    assert record["state"] == "completed"
    assert [item["kind"] for item in record["elements"]] == ["segment"]


def test_same_client_retry_write_ahead_reopens_interrupted_record():
    persist_mission_chat_turn(
        session_id="s1",
        client_message_id="m1",
        turn_id="t1",
        elements=[_SEGMENT],
        state="interrupted",
    )

    outcome = persist_mission_chat_turn(
        session_id="s1",
        client_message_id="m1",
        turn_id="t1",
        elements=[],
        state="running",
        write_ahead=True,
    )

    assert outcome is MissionChatTurnPersistOutcome.PERSISTED
    record = mission_chat_turn_record(session_id="s1", client_message_id="m1")
    assert record["state"] == "running"
    assert record["elements"] == []


# ---------------------------------------------------------------------------
# W2 — typed outcomes, no silent loss
# ---------------------------------------------------------------------------


def test_persist_outcomes_are_typed_and_exhaustive_for_callers():
    assert (
        persist_mission_chat_turn(
            session_id=None,
            client_message_id="m1",
            turn_id="t1",
            elements=[],
            state="running",
        )
        is MissionChatTurnPersistOutcome.SKIPPED_NO_KEYS
    )
    assert (
        persist_mission_chat_turn(
            session_id="s1",
            client_message_id="m1",
            turn_id="t1",
            elements=[],
            state="definitely_not_valid",
        )
        is MissionChatTurnPersistOutcome.REJECTED_INVALID_STATE
    )
    assert (
        persist_mission_chat_turn(
            session_id="s1",
            client_message_id="m1",
            turn_id="t1",
            elements=[],
            state=None,
        )
        is MissionChatTurnPersistOutcome.SKIPPED_EMPTY_LEGACY
    )
    assert (
        persist_mission_chat_turn(
            session_id="s1",
            client_message_id="m1",
            turn_id="t1",
            elements=[],
            state="running",
            write_ahead=True,
        )
        is MissionChatTurnPersistOutcome.PERSISTED
    )
    # Nothing was written by the rejected/skipped calls above except the last.
    record = mission_chat_turn_record(session_id="s1", client_message_id="m1")
    assert record["state"] == "running"


# ---------------------------------------------------------------------------
# W1 — cross-process lock
# ---------------------------------------------------------------------------

_WORKER_SCRIPT = """
import os, sys
sys.path.insert(0, {repo_root!r})
os.environ["HERMES_AGENT_RUNTIME_ROOT"] = {runtime_root!r}
from agent_runtime.mission_chat_turns import persist_mission_chat_turn, MissionChatTurnPersistOutcome
session = sys.argv[1]
for index in range(25):
    outcome = persist_mission_chat_turn(
        session_id=session,
        client_message_id=f"msg_{{index}}",
        turn_id=f"turn_{{index}}",
        elements=[],
        state="running",
        write_ahead=True,
    )
    assert outcome is MissionChatTurnPersistOutcome.PERSISTED, outcome
print("done")
"""


def test_concurrent_processes_do_not_lose_writes(isolate_agent_runtime_root):
    repo_root = str(Path(mission_chat_turns.__file__).resolve().parents[1])
    script = _WORKER_SCRIPT.format(
        repo_root=repo_root,
        runtime_root=str(isolate_agent_runtime_root),
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, session],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for session in ("sess_a", "sess_b")
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, stderr
        assert "done" in stdout

    store_path = isolate_agent_runtime_root / "mission_chat_turns.json"
    store = json.loads(store_path.read_text(encoding="utf-8"))
    assert len(store.get("sess_a") or {}) == 25
    assert len(store.get("sess_b") or {}) == 25


def test_persist_skips_with_typed_outcome_when_lock_is_held(monkeypatch):
    monkeypatch.setattr(mission_chat_turns, "_LOCK_TIMEOUT_SECONDS", 0.05)
    lock_path = mission_chat_turns.paths.store_root() / mission_chat_turns._LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    mission_chat_turns._lock_fd_exclusive_nonblocking(fd)
    try:
        outcome = persist_mission_chat_turn(
            session_id="s1",
            client_message_id="m1",
            turn_id="t1",
            elements=[],
            state="running",
            write_ahead=True,
        )
        assert outcome is MissionChatTurnPersistOutcome.SKIPPED_LOCK_TIMEOUT
        assert mission_chat_turn_record(session_id="s1", client_message_id="m1") is None
        # The opportunistic repair skips silently by design (retries next send).
        assert (
            mark_stale_running_turns_interrupted(
                session_id="s1",
                active_client_message_id="m2",
            )
            == []
        )
    finally:
        mission_chat_turns._unlock_fd(fd)
        os.close(fd)

    # Once the lock is released the same write goes through.
    outcome = persist_mission_chat_turn(
        session_id="s1",
        client_message_id="m1",
        turn_id="t1",
        elements=[],
        state="running",
        write_ahead=True,
    )
    assert outcome is MissionChatTurnPersistOutcome.PERSISTED
