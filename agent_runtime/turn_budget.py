"""Wall-clock turn budget — one authority for a mission-chat turn's time window.

Before this module the wall budget was a number that only the *killer* knew
about: ``profile_runner`` armed a ``Timer(max_wall_seconds)`` that called
``agent.interrupt()`` mid-API-call, and everything else (the agent, the
operator, the relay target) was blind to it. Two failures fell out of that, both
observed live on 2026-07-26 (a ``--max-seconds 540`` Neko turn that relayed to
Dev over ``agent_chat_send``):

1. **No visibility.** The agent could not see how much wall it had left, so it
   dispatched ~50 minutes of work into a 9-minute window. A supervisor relaying
   over the shared chain deadline had the same blind spot.
2. **No checkpoint.** Exhaustion was a hard kill in the middle of a provider
   call, so BOTH turns settled as ``outcome_unknown`` and needed a manual
   ``turn-resolve --action abandon`` plus a full re-brief.

This module is the pure decision surface for both fixes:

* :func:`resolve_turn_wall_budget` folds ``--max-seconds`` and the shared
  ``--relay-deadline-epoch`` into ONE :class:`TurnWallBudget` value object, so
  the relay clamp is computed in exactly one place (the mission-chat command
  and the HUD render read the same object).
* :meth:`TurnWallBudget.phase` answers "may this turn still start new work?"
  from a typed table instead of scattered ``if remaining < X`` comparisons.
* :func:`render_turn_budget_line` / :meth:`TurnWallBudget.hud_block` render the
  agent-visible and operator-visible projections of the same fact.

Pure and stdlib-only (``time.time`` only as the default clock) so the threshold
math is unit-testable without sleeping. Nothing here performs I/O, touches an
agent, or decides policy for the goal/daemon pipeline — the enforcement wiring
lives in ``profile_runner`` and the terminal-state wiring in the mission-chat
command.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

# Reserve floor/fraction for the graceful checkpoint. The window reserved at the
# END of a turn is ``max(60s, 15% of the original budget)``: 60s is roughly one
# unhurried toolless summary call on a slow provider, and the proportional term
# keeps long turns (the 540s incident) from reserving a token amount.
CHECKPOINT_RESERVE_FLOOR_SECONDS = 60.0
CHECKPOINT_RESERVE_FRACTION = 0.15
# A budget smaller than the reserve would open the checkpoint at t=0 and the
# agent would never get to run a single tool. Cap the reserve so at least this
# much working window always survives; when even that cannot be honoured the
# budget simply has no graceful phase (``supports_checkpoint`` is False) and the
# old hard wall is the only boundary — honest, not silently degraded.
CHECKPOINT_MIN_WORKING_SECONDS = 30.0

# Typed reason recorded on the turn record + emitted on the progress lane when
# the graceful checkpoint fires. Never a matched string anywhere downstream.
CHECKPOINT_TRIGGER_TIMER = "wall_budget_timer"
CHECKPOINT_TRIGGER_TOOL_GATE = "wall_budget_tool_gate"


class TurnBudgetPhase(str, Enum):
    """Where a turn stands against its wall budget.

    ``NORMAL``      — free to start new provider calls and tool executions.
    ``CHECKPOINT``  — inside the reserved window: launch NO new tool work, take
                      one final (toolless) provider call and report state.
    ``EXHAUSTED``   — past the deadline: the last-resort hard wall owns it.
    """

    NORMAL = "normal"
    CHECKPOINT = "checkpoint"
    EXHAUSTED = "exhausted"


def checkpoint_reserve_seconds(total_seconds: float | None) -> float:
    """Seconds reserved at the end of ``total_seconds`` for the final checkpoint.

    ``max(60s, 15%)``, then capped so :data:`CHECKPOINT_MIN_WORKING_SECONDS` of
    working window survives. Returns ``0.0`` for a budget too small to
    checkpoint at all (the caller treats that as "no graceful phase").
    """

    total = _positive_seconds(total_seconds)
    if total is None:
        return 0.0
    reserve = max(CHECKPOINT_RESERVE_FLOOR_SECONDS, CHECKPOINT_RESERVE_FRACTION * total)
    return max(0.0, min(reserve, total - CHECKPOINT_MIN_WORKING_SECONDS))


@dataclass(frozen=True)
class TurnWallBudget:
    """The wall window one mission-chat turn may spend, and where its edges are.

    ``deadline_epoch`` is absolute so a relayed hop and its caller reason about
    the SAME clock; ``shared`` records whether that deadline arrived on the
    relay envelope (``--relay-deadline-epoch``) rather than being minted by this
    turn — the difference the agent must see, because a shared deadline is spent
    by the whole chain, not by this hop alone.
    """

    total_seconds: float
    deadline_epoch: float
    shared: bool = False
    relay_chain: tuple[str, ...] = ()

    @property
    def reserve_seconds(self) -> float:
        return checkpoint_reserve_seconds(self.total_seconds)

    @property
    def supports_checkpoint(self) -> bool:
        """False when the budget is too small to reserve a final-reply window."""
        return self.reserve_seconds > 0.0

    @property
    def checkpoint_at_epoch(self) -> float:
        return self.deadline_epoch - self.reserve_seconds

    def remaining_seconds(self, *, now: float | None = None) -> float:
        return self.deadline_epoch - (time.time() if now is None else now)

    def seconds_until_checkpoint(self, *, now: float | None = None) -> float:
        """Delay before the graceful checkpoint opens (never negative)."""
        return max(0.0, self.checkpoint_at_epoch - (time.time() if now is None else now))

    def phase(self, *, now: float | None = None) -> TurnBudgetPhase:
        remaining = self.remaining_seconds(now=now)
        if remaining <= 0.0:
            return TurnBudgetPhase.EXHAUSTED
        if self.supports_checkpoint and remaining < self.reserve_seconds:
            return TurnBudgetPhase.CHECKPOINT
        return TurnBudgetPhase.NORMAL

    def may_start_new_work(self, *, now: float | None = None) -> bool:
        """Whether a NEW provider call / tool execution may still be launched."""
        return self.phase(now=now) is TurnBudgetPhase.NORMAL

    def hud_block(self, *, now: float | None = None) -> dict[str, Any]:
        """Typed projection for the situational HUD / observability row.

        Deliberately volatile: the caller keeps this OUT of the HUD revision
        hash (see ``runtime_hud.situational_hud_revision``) so a per-second
        countdown never re-snapshots the whole stable HUD block every turn.
        """

        return {
            "total_seconds": round(float(self.total_seconds), 1),
            "remaining_seconds": round(max(0.0, self.remaining_seconds(now=now)), 1),
            "checkpoint_reserve_seconds": round(self.reserve_seconds, 1),
            "phase": self.phase(now=now).value,
            "shared": bool(self.shared),
            **({"relay_chain": list(self.relay_chain)} if self.relay_chain else {}),
        }

    def summary(self, *, now: float | None = None) -> str:
        """One-line human/JSON summary (``blocker`` text, progress payloads)."""
        scope = "shared relay deadline" if self.shared else "this turn's wall budget"
        return (
            f"wall budget {self.total_seconds:g}s ({scope}); "
            f"{max(0.0, self.remaining_seconds(now=now)):.0f}s remaining"
        )


def resolve_turn_wall_budget(
    *,
    max_seconds: Any,
    relay_deadline_epoch: float | None = None,
    relay_chain: Iterable[str] = (),
    min_relay_seconds: float,
    default_seconds: float = 240.0,
    now: float | None = None,
) -> TurnWallBudget:
    """Fold the requested budget and any shared relay deadline into one budget.

    Reproduces the mission-chat clamp exactly — a hop may never outlive the
    chain deadline it inherited, but is never squeezed below the relay policy's
    per-hop minimum. When no deadline was inherited this turn MINTS one, so
    deeper hops share this hop's clock.
    """

    clock = time.time() if now is None else now
    total = _positive_seconds(max_seconds) or float(default_seconds)
    if relay_deadline_epoch is not None:
        inherited_remaining = relay_deadline_epoch - clock
        total = max(float(min_relay_seconds), min(total, inherited_remaining))
    return TurnWallBudget(
        total_seconds=total,
        deadline_epoch=(
            relay_deadline_epoch if relay_deadline_epoch is not None else clock + total
        ),
        shared=relay_deadline_epoch is not None,
        relay_chain=tuple(str(item) for item in (relay_chain or ()) if str(item).strip()),
    )


def render_turn_budget_line(
    budget: TurnWallBudget | None,
    *,
    now: float | None = None,
) -> str:
    """The agent-visible budget line appended to the runtime-context envelope.

    Emitted on EVERY turn (snapshot and ``unchanged`` deliveries alike) because
    it is the one genuinely volatile fact in that envelope — a cached
    "unchanged" stub would show the agent a stale countdown, which is worse than
    showing none.
    """

    if budget is None:
        return ""
    remaining = max(0.0, budget.remaining_seconds(now=now))
    scope = (
        "shared with every agent on this relay chain — a hop you dispatch spends "
        "the SAME clock"
        if budget.shared
        else "this turn only"
    )
    line = (
        f"- Wall budget: ~{remaining:.0f}s left of {budget.total_seconds:.0f}s ({scope}). "
        "Scope the work you start to what fits."
    )
    if budget.supports_checkpoint:
        line += (
            f" With under {budget.reserve_seconds:.0f}s left the harness stops new tool "
            "calls and asks you for a final checkpoint reply, so finish or hand off "
            "before then."
        )
    return line


def checkpoint_nudge_text(
    budget: TurnWallBudget | None,
    *,
    now: float | None = None,
) -> str:
    """The system-side nudge injected when the graceful checkpoint opens."""

    remaining = 0.0 if budget is None else max(0.0, budget.remaining_seconds(now=now))
    return (
        "[harness] Wall budget nearly exhausted — about "
        f"{remaining:.0f}s left on this turn's clock, and no further tool calls will "
        "run. Produce your FINAL checkpoint reply NOW: report honestly what you "
        "completed, what is still in flight, and exactly what the next turn must "
        "pick up. Do not start new work and do not claim anything you did not "
        "verify."
    )


def synthesize_checkpoint_summary(
    budget: TurnWallBudget | None,
    *,
    tool_names: Iterable[str] = (),
    now: float | None = None,
) -> str:
    """Honest stand-in reply when not even the final provider call fits.

    Names what actually ran (from the turn's recorded tool elements) and says
    plainly that no model reply was produced — never a fabricated answer.
    """

    names = [str(name).strip() for name in (tool_names or ()) if str(name).strip()]
    head = (
        "This turn ended on its wall-clock budget before the agent could produce a "
        "final reply."
    )
    if names:
        shown = ", ".join(names[:12])
        overflow = f" (+{len(names) - 12} more)" if len(names) > 12 else ""
        body = f" Tool calls that completed first: {shown}{overflow}."
    else:
        body = " No tool call completed before the budget ran out."
    tail = (
        " Nothing was resolved automatically and no turn resolution is required — "
        "send a new message (new client_message_id) with a smaller scope or a "
        "larger --max-seconds."
    )
    if budget is not None:
        head += f" ({budget.summary(now=now)})"
    return head + body + tail


def drain_iteration_budget(agent: Any, *, limit: int = 10_000) -> int:
    """Consume an agent's remaining loop iterations so it launches no new work.

    This is how the graceful checkpoint stops the tool-calling loop WITHOUT the
    mid-flight kill: the upstream loop exits at its next iteration boundary and
    its finalizer then takes exactly one toolless "summarise" call — the final
    checkpoint reply. The in-flight tool batch is deliberately allowed to finish;
    aborting a running tool is precisely the mid-kill this replaces.

    Uses only the documented ``IterationBudget.consume()`` API (no upstream
    edit, no private attribute). Returns how many iterations were reclaimed.
    """

    budget = getattr(agent, "iteration_budget", None)
    consume = getattr(budget, "consume", None)
    if not callable(consume):
        return 0
    drained = 0
    while drained < limit:
        try:
            if not consume():
                break
        except Exception:
            break
        drained += 1
    return drained


def _positive_seconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


__all__ = [
    "CHECKPOINT_MIN_WORKING_SECONDS",
    "CHECKPOINT_RESERVE_FLOOR_SECONDS",
    "CHECKPOINT_RESERVE_FRACTION",
    "CHECKPOINT_TRIGGER_TIMER",
    "CHECKPOINT_TRIGGER_TOOL_GATE",
    "TurnBudgetPhase",
    "TurnWallBudget",
    "checkpoint_nudge_text",
    "checkpoint_reserve_seconds",
    "drain_iteration_budget",
    "render_turn_budget_line",
    "resolve_turn_wall_budget",
    "synthesize_checkpoint_summary",
]
