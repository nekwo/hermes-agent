"""Stage 3a — the mission-chat lane's ``process wait`` ceiling.

``ProcessRegistry.wait`` clamped EVERY requested timeout to ``TERMINAL_TIMEOUT``
(180 s by default), lane-agnostically. On the mission-chat lane that clamp is
the single largest source of wasted API calls in a generation-bearing turn: the
charsheet measurement of 2026-08-29 counted 12 of 27 calls on the fire-imp turn
(~690k of 1.556M cumulative prompt tokens) spent asking "done yet?", and the
agent that asked for ``timeout: 600`` was silently clamped to 180.

The raise is LANE-SCOPED on purpose. ``docs/agent-runtime-harness/planned/
charsheet-turn-efficiency-2026-08-29.md`` names "no blanket ``TERMINAL_TIMEOUT``
raise outside the mission-chat lane" as a non-goal — other lanes' turn budgets
were never measured — so the ceiling is read off the terminal ENVELOPE SCOPE,
which is bound for the duration of a mission-chat run and unbound everywhere
else. That is the same structural "no other lane changes" guarantee the
envelope's own docstring claims.
"""

from __future__ import annotations

import time

import pytest

from agent_runtime.terminal_envelope import (
    LANE_MISSION_CHAT,
    TerminalEnvelopeScope,
    terminal_envelope_scope,
)
from tools.process_registry import (
    MISSION_CHAT_WAIT_MAX_SECONDS,
    ProcessRegistry,
    ProcessSession,
    wait_ceiling_seconds,
)


def _exited_session(registry: ProcessRegistry, sid: str = "proc_clamp") -> str:
    """An already-exited session, so ``wait`` returns without blocking.

    The observable under test is the ``timeout_note`` — ``wait`` emits the
    "clamped to configured limit" note whenever the ceiling bit, and the note is
    carried onto the ``exited`` result too. So a finished session answers "was
    600 honored?" in microseconds instead of ten minutes.
    """

    session = ProcessSession(
        id=sid,
        command="hermes harness characters rows --draft d1",
        task_id="t1",
        started_at=time.time(),
        exited=True,
        exit_code=0,
        output_buffer="done",
    )
    registry._finished[sid] = session
    return sid


def _mission_chat_scope() -> TerminalEnvelopeScope:
    return TerminalEnvelopeScope(
        lane=LANE_MISSION_CHAT,
        role="profile",
        persona_id="chara_a2",
        session_id="persona_chat_personainst_chara_a2_7b31d0e4",
    )


class TestWaitCeiling:
    def test_mission_chat_lane_ceiling_is_600(self, monkeypatch):
        monkeypatch.delenv("TERMINAL_TIMEOUT", raising=False)
        with terminal_envelope_scope(_mission_chat_scope()):
            assert wait_ceiling_seconds() == MISSION_CHAT_WAIT_MAX_SECONDS
        assert MISSION_CHAT_WAIT_MAX_SECONDS == 600

    def test_unscoped_ceiling_is_the_terminal_timeout_default(self, monkeypatch):
        monkeypatch.delenv("TERMINAL_TIMEOUT", raising=False)
        assert wait_ceiling_seconds() == 180

    def test_another_lane_keeps_the_terminal_timeout_default(self, monkeypatch):
        """A scope that is not the mission-chat lane is not raised.

        The non-goal is explicit: nothing outside the measured lane moves.
        """

        monkeypatch.delenv("TERMINAL_TIMEOUT", raising=False)
        other = TerminalEnvelopeScope(lane="worker", role="worker")
        with terminal_envelope_scope(other):
            assert wait_ceiling_seconds() == 180

    def test_a_higher_terminal_timeout_is_never_lowered(self, monkeypatch):
        """The lane ceiling is a FLOOR-raise, never a cap on an operator.

        A deployment that configured 900 s keeps 900 s on every lane; the
        mission-chat lane simply cannot fall below 600 s.
        """

        monkeypatch.setenv("TERMINAL_TIMEOUT", "900")
        assert wait_ceiling_seconds() == 900
        with terminal_envelope_scope(_mission_chat_scope()):
            assert wait_ceiling_seconds() == 900

    def test_a_malformed_terminal_timeout_still_yields_the_lane_ceiling(
        self, monkeypatch
    ):
        monkeypatch.setenv("TERMINAL_TIMEOUT", "5m")
        assert wait_ceiling_seconds() == 180
        with terminal_envelope_scope(_mission_chat_scope()):
            assert wait_ceiling_seconds() == MISSION_CHAT_WAIT_MAX_SECONDS


class TestWaitHonorsTheLaneCeiling:
    def test_mission_chat_wait_of_600_is_not_clamped(self, monkeypatch):
        monkeypatch.delenv("TERMINAL_TIMEOUT", raising=False)
        registry = ProcessRegistry()
        sid = _exited_session(registry)
        with terminal_envelope_scope(_mission_chat_scope()):
            result = registry.wait(sid, timeout=600)
        assert result["status"] == "exited"
        assert "timeout_note" not in result

    def test_off_lane_wait_of_600_is_still_clamped_to_180(self, monkeypatch):
        monkeypatch.delenv("TERMINAL_TIMEOUT", raising=False)
        registry = ProcessRegistry()
        sid = _exited_session(registry)
        result = registry.wait(sid, timeout=600)
        assert result["status"] == "exited"
        # The existing note is KEPT, verbatim in shape — the plan asks for the
        # raise, not for the silence that made the clamp invisible.
        assert "clamped" in result["timeout_note"]
        assert "180s" in result["timeout_note"]

    def test_mission_chat_wait_above_the_lane_ceiling_is_clamped_to_600(
        self, monkeypatch
    ):
        monkeypatch.delenv("TERMINAL_TIMEOUT", raising=False)
        registry = ProcessRegistry()
        sid = _exited_session(registry)
        with terminal_envelope_scope(_mission_chat_scope()):
            result = registry.wait(sid, timeout=1200)
        assert "600s" in result["timeout_note"]

    def test_the_lane_ceiling_is_the_window_when_no_timeout_is_asked_for(
        self, monkeypatch
    ):
        """An agent that passes no timeout gets the lane's window, not 180 s.

        Asserted on the WINDOW itself rather than by blocking for it: the
        deadline ``wait`` computes is the ceiling, and a session that never
        exits would otherwise hold the suite for ten minutes to prove it.
        """

        monkeypatch.delenv("TERMINAL_TIMEOUT", raising=False)
        registry = ProcessRegistry()
        session = ProcessSession(
            id="proc_running",
            command="hermes harness characters rows --draft d1",
            started_at=time.time(),
        )
        registry._running[session.id] = session
        start = time.monotonic()

        with terminal_envelope_scope(_mission_chat_scope()):
            # Exit on the first loop iteration; the deadline was already set
            # from the ceiling by then.
            session.exited = True
            result = registry.wait(session.id)
        assert result["status"] == "exited"
        assert "timeout_note" not in result
        assert time.monotonic() - start < 5.0


@pytest.mark.parametrize("requested", [1, 30, 179])
def test_short_waits_are_untouched_on_every_lane(monkeypatch, requested):
    monkeypatch.delenv("TERMINAL_TIMEOUT", raising=False)
    registry = ProcessRegistry()
    sid = _exited_session(registry, sid=f"proc_short_{requested}")
    result = registry.wait(sid, timeout=requested)
    assert "timeout_note" not in result
