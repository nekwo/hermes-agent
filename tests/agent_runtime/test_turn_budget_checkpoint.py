"""Wall-budget visibility + graceful checkpoint (live incident 2026-07-26).

The failure this covers: a ``--max-seconds 540`` Neko turn relayed to Dev over
``agent_chat_send`` (one process, one shared wall). Neither agent could see the
remaining budget, so a ~50-minute plan was dispatched into a ~9-minute window;
at exhaustion the harness killed mid-API-call and BOTH turns froze as
``outcome_unknown``, each needing a manual ``turn-resolve --action abandon`` and
a full re-brief.

Coverage:

* threshold math — 60s floor, 15% proportional term, the working-window cap
  that keeps a tiny budget from opening the checkpoint at t=0;
* the relay clamp — a hop never outlives the shared chain deadline, never drops
  below the per-hop minimum, and the TARGET sees the shared remaining budget;
* the loop gate — under threshold the checkpoint engages, nudges, and stops new
  tool work, all exactly once and without interrupting the agent;
* the state machine — ``budget_exhausted`` is terminal, is never in-flight,
  needs no ``turn-resolve``, and still honours the legacy late-commit
  convention;
* the HUD lane — the volatile budget line survives an ``unchanged`` delivery and
  never churns the HUD revision.

Every clock is injected, so nothing here sleeps.
"""

from __future__ import annotations

import pytest

from agent_runtime import mission_chat_turns, runtime_hud, turn_budget
from agent_runtime.mission_chat_turns import (
    INFLIGHT_TURN_STATES,
    TERMINAL_TURN_STATES,
    MissionChatTurnPersistOutcome,
    abandon_mission_chat_turn,
    mark_stale_inflight_turns_interrupted,
    mission_chat_turn_record,
    transition_mission_chat_turn,
)
from agent_runtime.profile_runner import WallBudgetCheckpoint
from agent_runtime.turn_budget import (
    TurnBudgetPhase,
    TurnWallBudget,
    checkpoint_reserve_seconds,
    drain_iteration_budget,
    render_turn_budget_line,
    resolve_turn_wall_budget,
    synthesize_checkpoint_summary,
)

_MIN_RELAY = 10.0


# ---------------------------------------------------------------------------
# Threshold math
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("total", "expected"),
    [
        # 15% term wins once the budget is large enough (the incident's 540s).
        (540.0, 81.0),
        (1200.0, 180.0),
        # 60s floor wins for ordinary budgets (15% of 240 is only 36s).
        (240.0, 60.0),
        (400.0, 60.0),
        # Exactly at the crossover: 15% of 400 == 60.
        (401.0, 60.15),
    ],
)
def test_reserve_is_the_larger_of_the_floor_and_the_proportional_term(total, expected):
    assert checkpoint_reserve_seconds(total) == pytest.approx(expected)


def test_reserve_never_eats_the_whole_working_window():
    # A 60s budget would reserve its entire self under the raw max(60, 15%)
    # rule, so the agent would never run a single tool. The cap leaves
    # CHECKPOINT_MIN_WORKING_SECONDS of actual working time.
    assert checkpoint_reserve_seconds(60.0) == pytest.approx(30.0)
    assert checkpoint_reserve_seconds(90.0) == pytest.approx(60.0)


def test_budget_too_small_to_checkpoint_has_no_graceful_phase():
    assert checkpoint_reserve_seconds(20.0) == 0.0
    assert checkpoint_reserve_seconds(0) == 0.0
    assert checkpoint_reserve_seconds(None) == 0.0
    tiny = TurnWallBudget(total_seconds=20.0, deadline_epoch=1_000.0)
    assert tiny.supports_checkpoint is False
    # Honest degradation: with no reserve the turn stays NORMAL right up to the
    # deadline and the last-resort hard wall owns it.
    assert tiny.phase(now=999.0) is TurnBudgetPhase.NORMAL
    assert tiny.phase(now=1_001.0) is TurnBudgetPhase.EXHAUSTED


# ---------------------------------------------------------------------------
# Phase table
# ---------------------------------------------------------------------------


def test_phase_walks_normal_then_checkpoint_then_exhausted():
    budget = TurnWallBudget(total_seconds=540.0, deadline_epoch=1_540.0)
    assert budget.reserve_seconds == pytest.approx(81.0)

    # 200s left — plenty of room, new work allowed.
    assert budget.phase(now=1_340.0) is TurnBudgetPhase.NORMAL
    assert budget.may_start_new_work(now=1_340.0) is True
    # Exactly at the reserve boundary is still NORMAL (strictly-less compare).
    assert budget.phase(now=1_459.0) is TurnBudgetPhase.NORMAL
    # One second inside the reserve: no new work.
    assert budget.phase(now=1_459.5) is TurnBudgetPhase.CHECKPOINT
    assert budget.may_start_new_work(now=1_459.5) is False
    # Past the deadline: the hard wall's territory.
    assert budget.phase(now=1_541.0) is TurnBudgetPhase.EXHAUSTED


def test_seconds_until_checkpoint_is_clamped_at_zero():
    budget = TurnWallBudget(total_seconds=240.0, deadline_epoch=1_240.0)
    assert budget.seconds_until_checkpoint(now=1_000.0) == pytest.approx(180.0)
    assert budget.seconds_until_checkpoint(now=1_235.0) == 0.0


# ---------------------------------------------------------------------------
# Relay clamp / shared deadline
# ---------------------------------------------------------------------------


def test_root_turn_mints_its_own_deadline():
    budget = resolve_turn_wall_budget(
        max_seconds=540.0, min_relay_seconds=_MIN_RELAY, now=1_000.0
    )
    assert budget.total_seconds == pytest.approx(540.0)
    assert budget.deadline_epoch == pytest.approx(1_540.0)
    assert budget.shared is False


def test_relay_hop_is_clamped_to_the_inherited_chain_deadline():
    # Caller minted a 540s window and already burned 400s of it. A hop asking
    # for the full 540 may only have the 140 that remain on the SHARED clock.
    budget = resolve_turn_wall_budget(
        max_seconds=540.0,
        relay_deadline_epoch=1_540.0,
        relay_chain=("neko", "dev"),
        min_relay_seconds=_MIN_RELAY,
        now=1_400.0,
    )
    assert budget.total_seconds == pytest.approx(140.0)
    assert budget.deadline_epoch == pytest.approx(1_540.0)
    assert budget.shared is True
    assert budget.relay_chain == ("neko", "dev")


def test_relay_hop_never_drops_below_the_per_hop_minimum():
    budget = resolve_turn_wall_budget(
        max_seconds=540.0,
        relay_deadline_epoch=1_002.0,
        min_relay_seconds=_MIN_RELAY,
        now=1_000.0,
    )
    assert budget.total_seconds == pytest.approx(_MIN_RELAY)


def test_relay_hop_keeps_its_own_smaller_ask():
    budget = resolve_turn_wall_budget(
        max_seconds=60.0,
        relay_deadline_epoch=1_540.0,
        min_relay_seconds=_MIN_RELAY,
        now=1_000.0,
    )
    assert budget.total_seconds == pytest.approx(60.0)


def test_relay_target_hud_line_names_the_shared_clock_and_the_remaining_budget():
    budget = resolve_turn_wall_budget(
        max_seconds=540.0,
        relay_deadline_epoch=1_540.0,
        relay_chain=("neko", "dev"),
        min_relay_seconds=_MIN_RELAY,
        now=1_400.0,
    )
    line = render_turn_budget_line(budget, now=1_400.0)
    assert "~140s left" in line
    assert "SAME clock" in line
    # The reserve is advertised so the agent can plan to land before it
    # (140s inherited -> max(60, 21) = 60s reserve, under the 110s cap).
    assert budget.reserve_seconds == pytest.approx(60.0)
    assert "under 60s left" in line
    hud = budget.hud_block(now=1_400.0)
    assert hud["remaining_seconds"] == pytest.approx(140.0)
    assert hud["shared"] is True
    assert hud["relay_chain"] == ["neko", "dev"]
    assert hud["phase"] == TurnBudgetPhase.NORMAL.value


def test_root_hud_line_does_not_claim_a_shared_clock():
    budget = resolve_turn_wall_budget(
        max_seconds=240.0, min_relay_seconds=_MIN_RELAY, now=1_000.0
    )
    line = render_turn_budget_line(budget, now=1_000.0)
    assert "this turn only" in line
    assert "SAME clock" not in line
    assert render_turn_budget_line(None) == ""


# ---------------------------------------------------------------------------
# Loop gate — stop launching tools under the threshold
# ---------------------------------------------------------------------------


class _FakeIterationBudget:
    def __init__(self, max_total: int):
        self.max_total = max_total
        self._used = 0

    def consume(self) -> bool:
        if self._used >= self.max_total:
            return False
        self._used += 1
        return True

    @property
    def remaining(self) -> int:
        return max(0, self.max_total - self._used)


class _FakeAgent:
    def __init__(self, max_total: int = 90):
        self.iteration_budget = _FakeIterationBudget(max_total)
        self.steers: list[str] = []
        self.interrupts: list[str] = []

    def steer(self, text: str) -> bool:
        self.steers.append(text)
        return True

    def interrupt(self, message: str | None = None) -> None:
        self.interrupts.append(message or "")


def _checkpoint(now_holder, *, total=540.0, deadline=1_540.0, events=None):
    checkpoint = WallBudgetCheckpoint(
        TurnWallBudget(total_seconds=total, deadline_epoch=deadline),
        progress_callback=(events.append if events is not None else None),
        clock=lambda: now_holder[0],
    )
    return checkpoint


def test_gate_allows_new_tool_work_while_the_budget_is_healthy():
    now = [1_000.0]
    agent = _FakeAgent()
    checkpoint = _checkpoint(now)
    checkpoint.bind(agent)

    assert checkpoint.gate() is True
    assert checkpoint.engaged is False
    assert agent.steers == []
    assert agent.iteration_budget.remaining == 90


def test_gate_stops_new_tool_work_and_nudges_once_under_the_threshold():
    now = [1_000.0]
    events: list[dict] = []
    agent = _FakeAgent()
    checkpoint = _checkpoint(now, events=events)
    checkpoint.bind(agent)
    assert checkpoint.gate() is True

    # 40s left of a 540s budget: inside the 81s reserve.
    now[0] = 1_500.0
    assert checkpoint.gate() is False
    assert checkpoint.engaged is True

    # The agent is TOLD, in-band, to produce a final checkpoint reply...
    assert len(agent.steers) == 1
    assert "final checkpoint reply" in agent.steers[0].lower()
    # ...and the loop can launch no further iterations, so no new tool batch and
    # no new tool-bearing provider call starts.
    assert agent.iteration_budget.remaining == 0
    assert agent.iteration_budget.consume() is False
    # Crucially NOT an interrupt: the mid-flight kill is what we are replacing.
    assert agent.interrupts == []

    # A typed progress event names the checkpoint (never a matched string).
    assert [event["step"] for event in events] == ["wall_budget_checkpoint_opened"]
    assert events[0]["phase"] == "wall_budget_checkpoint"
    assert events[0]["wall_budget"]["engaged"] is True

    # Idempotent: later gates and a racing timer add nothing.
    now[0] = 1_530.0
    assert checkpoint.gate() is False
    assert checkpoint.engage(trigger=turn_budget.CHECKPOINT_TRIGGER_TIMER) is False
    assert len(agent.steers) == 1
    assert len(events) == 1


def test_gate_is_inert_for_a_budget_too_small_to_checkpoint():
    now = [1_000.0]
    agent = _FakeAgent()
    checkpoint = _checkpoint(now, total=20.0, deadline=1_020.0)
    checkpoint.bind(agent)

    now[0] = 1_019.0
    assert checkpoint.gate() is True
    assert checkpoint.engaged is False
    assert agent.iteration_budget.remaining == 90


def test_checkpoint_summary_records_the_trigger_and_the_window():
    now = [1_490.0]
    agent = _FakeAgent()
    checkpoint = _checkpoint(now)
    checkpoint.bind(agent)
    checkpoint.engage(trigger=turn_budget.CHECKPOINT_TRIGGER_TIMER)

    summary = checkpoint.summary()
    assert summary["engaged"] is True
    assert summary["trigger"] == turn_budget.CHECKPOINT_TRIGGER_TIMER
    assert summary["iterations_reclaimed"] == 90
    assert summary["remaining_at_checkpoint_seconds"] == pytest.approx(50.0)
    assert summary["total_seconds"] == pytest.approx(540.0)


def test_unbound_checkpoint_never_engages():
    now = [1_539.0]
    checkpoint = _checkpoint(now)
    assert checkpoint.engage(trigger=turn_budget.CHECKPOINT_TRIGGER_TIMER) is False
    assert checkpoint.engaged is False


def test_the_tool_start_seam_actually_consults_the_checkpoint():
    """Wiring proof, not just the class in isolation.

    The gate has to run on the SAME seam the tool-calling loop already signals
    (``tool_start_callback`` -> ``_progress_adapter``); a checkpoint that only
    works when called directly would never fire in production.
    """

    from agent_runtime.profile_runner import _progress_adapter, _ToolBudgetGuard

    now = [1_000.0]
    agent = _FakeAgent()
    checkpoint = _checkpoint(now)
    checkpoint.bind(agent)
    guard = _ToolBudgetGuard()
    guard.wall_checkpoint = checkpoint
    seen: list[dict] = []
    emit = _progress_adapter(seen.append, "run.tool.started", guard=guard)

    emit("run.tool.started", "read_file", {"path": "x"})
    assert checkpoint.engaged is False
    assert agent.iteration_budget.remaining == 90

    now[0] = 1_500.0
    emit("run.tool.started", "read_file", {"path": "y"})
    assert checkpoint.engaged is True
    assert agent.iteration_budget.remaining == 0
    # The seam still delivers its normal progress payloads either way.
    assert [payload["step"] for payload in seen] == ["tool_started", "tool_started"]


def test_drain_iteration_budget_is_tolerant_of_foreign_agents():
    assert drain_iteration_budget(object()) == 0

    class _Broken:
        iteration_budget = object()

    assert drain_iteration_budget(_Broken()) == 0


# ---------------------------------------------------------------------------
# Journal state machine
# ---------------------------------------------------------------------------


_SESSION = "chat_budget_session"
_CLIENT = "cmid_budget_1"
_TURN = "turn_budget_1"


def _start_turn(client_message_id: str = _CLIENT) -> None:
    assert (
        transition_mission_chat_turn(
            session_id=_SESSION,
            client_message_id=client_message_id,
            turn_id=_TURN,
            state="pending",
        )
        is MissionChatTurnPersistOutcome.PERSISTED
    )
    assert (
        transition_mission_chat_turn(
            session_id=_SESSION,
            client_message_id=client_message_id,
            turn_id=_TURN,
            state="executing",
        )
        is MissionChatTurnPersistOutcome.PERSISTED
    )


def test_executing_settles_into_budget_exhausted_instead_of_outcome_unknown():
    _start_turn()
    outcome = transition_mission_chat_turn(
        session_id=_SESSION,
        client_message_id=_CLIENT,
        turn_id=_TURN,
        state="budget_exhausted",
        metadata={
            "budget_exhausted": True,
            "budget_trigger": turn_budget.CHECKPOINT_TRIGGER_TOOL_GATE,
            "budget_summary": "wall budget 540s; 0s remaining",
        },
    )
    assert outcome is MissionChatTurnPersistOutcome.PERSISTED

    record = mission_chat_turn_record(session_id=_SESSION, client_message_id=_CLIENT)
    assert record["state"] == "budget_exhausted"
    assert record["budget_exhausted"] is True
    assert record["budget_trigger"] == turn_budget.CHECKPOINT_TRIGGER_TOOL_GATE
    assert record["budget_summary"].startswith("wall budget 540s")


def test_budget_exhausted_is_terminal_and_never_in_flight():
    assert "budget_exhausted" not in INFLIGHT_TURN_STATES
    assert "budget_exhausted" in TERMINAL_TURN_STATES

    _start_turn()
    transition_mission_chat_turn(
        session_id=_SESSION,
        client_message_id=_CLIENT,
        turn_id=_TURN,
        state="budget_exhausted",
    )
    # No repair may reopen it: the next-send / serve-boot sweep flips only
    # genuinely in-flight corpses.
    assert (
        mark_stale_inflight_turns_interrupted(
            session_id=_SESSION, active_client_message_id="cmid_other"
        )
        == []
    )
    record = mission_chat_turn_record(session_id=_SESSION, client_message_id=_CLIENT)
    assert record["state"] == "budget_exhausted"


@pytest.mark.parametrize("requested", ["pending", "executing", "outcome_unknown", "abandoned", "projected"])
def test_budget_exhausted_does_not_resurrect_or_reroute(requested):
    _start_turn()
    transition_mission_chat_turn(
        session_id=_SESSION,
        client_message_id=_CLIENT,
        turn_id=_TURN,
        state="budget_exhausted",
    )
    assert (
        transition_mission_chat_turn(
            session_id=_SESSION,
            client_message_id=_CLIENT,
            turn_id=_TURN,
            state=requested,
        )
        is MissionChatTurnPersistOutcome.REJECTED_STALE_TRANSITION
    )
    record = mission_chat_turn_record(session_id=_SESSION, client_message_id=_CLIENT)
    assert record["state"] == "budget_exhausted"


def test_budget_exhausted_requires_no_turn_resolve():
    # ``turn-resolve --action abandon`` exists only for the genuinely ambiguous
    # ``outcome_unknown``; a budget-settled turn must not be resolvable, and must
    # not need to be.
    _start_turn()
    transition_mission_chat_turn(
        session_id=_SESSION,
        client_message_id=_CLIENT,
        turn_id=_TURN,
        state="budget_exhausted",
    )
    assert (
        abandon_mission_chat_turn(
            session_id=_SESSION,
            client_message_id=_CLIENT,
            turn_id=_TURN,
            resolution_actor="operator",
        )
        is MissionChatTurnPersistOutcome.REJECTED_STALE_TRANSITION
    )


def test_late_native_commit_after_budget_exhausted_still_wins():
    # Legacy-interrupted convention: a reply proven durable AFTER the record was
    # settled must never be lost to the settle.
    _start_turn()
    transition_mission_chat_turn(
        session_id=_SESSION,
        client_message_id=_CLIENT,
        turn_id=_TURN,
        state="budget_exhausted",
        metadata={"budget_exhausted": True},
    )
    assert (
        transition_mission_chat_turn(
            session_id=_SESSION,
            client_message_id=_CLIENT,
            turn_id=_TURN,
            state="native_committed",
            metadata={"native_committed": True, "stored_reply": "late checkpoint"},
        )
        is MissionChatTurnPersistOutcome.PERSISTED
    )
    assert (
        transition_mission_chat_turn(
            session_id=_SESSION,
            client_message_id=_CLIENT,
            turn_id=_TURN,
            state="projected",
            metadata={"projection_committed": True},
        )
        is MissionChatTurnPersistOutcome.PERSISTED
    )
    record = mission_chat_turn_record(session_id=_SESSION, client_message_id=_CLIENT)
    assert record["state"] == "projected"
    assert record["stored_reply"] == "late checkpoint"
    # Provenance survives: the reply was a budget checkpoint, not a full answer.
    assert record["budget_exhausted"] is True


def test_a_new_client_message_id_proceeds_normally_after_a_budget_settle():
    _start_turn()
    transition_mission_chat_turn(
        session_id=_SESSION,
        client_message_id=_CLIENT,
        turn_id=_TURN,
        state="budget_exhausted",
    )
    assert (
        transition_mission_chat_turn(
            session_id=_SESSION,
            client_message_id="cmid_budget_2",
            turn_id="turn_budget_2",
            state="pending",
        )
        is MissionChatTurnPersistOutcome.PERSISTED
    )


def test_outcome_unknown_can_still_be_reclassified_as_a_budget_settle():
    # A turn that already reached the ambiguous state (e.g. a repair raced the
    # runner) can be settled honestly once the wall-budget cause is known,
    # without an operator abandon.
    _start_turn()
    transition_mission_chat_turn(
        session_id=_SESSION,
        client_message_id=_CLIENT,
        turn_id=_TURN,
        state="outcome_unknown",
    )
    assert (
        transition_mission_chat_turn(
            session_id=_SESSION,
            client_message_id=_CLIENT,
            turn_id=_TURN,
            state="budget_exhausted",
        )
        is MissionChatTurnPersistOutcome.PERSISTED
    )


def test_budget_exhausted_is_a_recognised_journal_state():
    assert "budget_exhausted" in mission_chat_turns.JOURNAL_TURN_STATES


# ---------------------------------------------------------------------------
# HUD lane — volatile budget rides every delivery, churns no revision
# ---------------------------------------------------------------------------


def test_volatile_budget_is_emitted_on_every_delivery():
    for delivery in (
        runtime_hud.RUNTIME_CONTEXT_DELIVERY_SNAPSHOT,
        runtime_hud.RUNTIME_CONTEXT_DELIVERY_UNCHANGED,
        runtime_hud.RUNTIME_CONTEXT_DELIVERY_UNAVAILABLE,
    ):
        envelope = runtime_hud.render_runtime_context_envelope(
            context_id="ctx_abc",
            revision="hud_deadbeefdeadbeef",
            delivery=delivery,
            situational_hud_content="## Runtime Situation\n- Mission: none",
            volatile_content="- Wall budget: ~140s left of 540s.",
        )
        assert "- Wall budget: ~140s left of 540s." in envelope
    # And the whole envelope still strips cleanly from the transcript row.
    envelope = runtime_hud.render_runtime_context_envelope(
        context_id="ctx_abc",
        revision="hud_deadbeefdeadbeef",
        delivery=runtime_hud.RUNTIME_CONTEXT_DELIVERY_SNAPSHOT,
        situational_hud_content="## Runtime Situation\n- Mission: none",
        volatile_content="- Wall budget: ~140s left of 540s.",
    )
    remainder, metadata = runtime_hud.extract_runtime_context_envelope(
        f"operator text\n\n{envelope}"
    )
    assert remainder == "operator text"
    assert metadata["delivery"] == runtime_hud.RUNTIME_CONTEXT_DELIVERY_SNAPSHOT


def test_turn_budget_never_churns_the_hud_revision():
    stable = {"scope": {"realm": "eternia"}, "steering": {"steered_by": [], "steers": []}}
    baseline = runtime_hud.situational_hud_revision(stable)
    for remaining in (539.0, 300.0, 12.0):
        hud = dict(stable)
        hud["turn_budget"] = TurnWallBudget(
            total_seconds=540.0, deadline_epoch=1_540.0
        ).hud_block(now=1_540.0 - remaining)
        assert runtime_hud.situational_hud_revision(hud) == baseline
    # A HUD carrying ONLY the volatile block has nothing stable to describe.
    assert (
        runtime_hud.situational_hud_revision({"turn_budget": {"remaining_seconds": 5}})
        == "hud_unavailable"
    )


def test_resolve_situational_hud_carries_the_budget_for_operator_parity():
    class _Instance:
        id = "personainst_dev"
        display_name = "Dev"

    hud = runtime_hud.resolve_situational_hud(
        _Instance(),
        turn_budget=TurnWallBudget(total_seconds=540.0, deadline_epoch=1_540.0).hud_block(
            now=1_400.0
        ),
    )
    assert hud["turn_budget"]["remaining_seconds"] == pytest.approx(140.0)
    # The stable rendered block stays free of the countdown (it is delivered on
    # the volatile tail instead).
    assert "Wall budget" not in runtime_hud.render_situational_hud_block(hud)


# ---------------------------------------------------------------------------
# Synthesized summary when not even the final call fits
# ---------------------------------------------------------------------------


def test_synthesized_summary_names_what_ran_and_refuses_to_ask_for_a_resolve():
    summary = synthesize_checkpoint_summary(
        TurnWallBudget(total_seconds=540.0, deadline_epoch=1_540.0),
        tool_names=["read_file", "agent_chat_send"],
        now=1_541.0,
    )
    assert "read_file, agent_chat_send" in summary
    assert "no turn resolution is required" in summary
    assert "turn-resolve" not in summary
    assert "new client_message_id" in summary


def test_synthesized_summary_is_honest_when_nothing_ran():
    summary = synthesize_checkpoint_summary(None, tool_names=[])
    assert "No tool call completed" in summary
