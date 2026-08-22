"""ONE per-run budget authority — "what bounded this turn?", answered once.

The gap this closes
-------------------
A persona run is bounded by several independent mechanisms, each of which used
to keep its own private bookkeeping and express exhaustion in its own way:

* ``profile_runner._ToolBudgetGuard`` — the read/search loop bounds. **TRIPS the
  run**: it raises ``RunBudgetExceeded``.
* ``profile_runner.WallBudgetCheckpoint`` — the graceful wall-clock checkpoint.
  **LANDS the turn**: it steers a nudge, drains the iteration budget and lets the
  agent write a final reply; the hard wall behind it still trips.
* ``mcp_admission.McpCallBudget`` — the per-run admitted-MCP call bound.
  **REFUSES the CALL**, never the turn.
* ``profile_runner._enforce_result_budgets`` — the post-run api-call / token
  bounds. **TRIPS the run**, like the first.

Three different trip semantics is not the problem — each is deliberate and this
module preserves all of them EXACTLY. The problem was that the bookkeeping was
scattered: nothing answered "what bounded this turn?" in one place, trip reasons
were free-form strings assembled at the raise site, and a budget that tripped
was invisible after the fact unless an operator happened to read the exception
message or grep the progress lane. Two of the four wrote *some* numbers into
``profile_timing`` (``mcp_calls_spent`` …), the other two wrote none, and no
budget recorded its HEADROOM — so "the turn stopped at 6 read/search calls out
of 6" and "the turn used 2 of 6" were indistinguishable from the run record.

What this module is
-------------------
A pure value-object ledger. It holds no policy, makes no decision, enforces
nothing, and never raises: each mechanism still decides on its own terms and
then *declares* itself here. The ledger's only product is
:meth:`RunBudgetLedger.accounting` — one block, uniform across mechanisms
(``kind``, ``enforcement``, ``limit``, ``consumed``, ``remaining``, ``tripped``,
typed ``trip_reason``), written into the run's ``profile_timing`` under
``run_budget`` and attached to ``RunBudgetExceeded.run_budget`` so it survives
the raised path too.

Deliberately NOT here: any of the enforcement wiring. Moving the timers,
the tool-start gate, the registry handler swap or the reserve math into this
module would fold three intentionally different trip semantics into one, which
is the opposite of the goal. This is the accounting seam, not a scheduler.

Reading the block back
----------------------
:func:`safe_accounting_block` is the ONE reader every persistence/projection
boundary goes through, and :func:`turn_run_budget_metadata` is the one adapter
that turns "the run that just ended" into the journal metadata fragment a
settle point splices in. Both are absence-preserving: a run that declared no
budget yields nothing, never an empty claim. Without them each boundary would
grow its own copy of the shape and they would drift — which is the exact defect
this module was created to retire, one layer up.

Pure and stdlib-only, so the accounting shape is unit-testable without a
harness, an agent, or a clock that sleeps.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class RunBudgetKind(str, Enum):
    """WHICH bound. One member per mechanism an operator can hit."""

    WALL = "wall"
    API_CALLS = "api_calls"
    TOTAL_TOKENS = "total_tokens"
    MCP_CALLS = "mcp_calls"


class RunBudgetEnforcement(str, Enum):
    """HOW exhaustion is expressed — the three semantics, kept distinct.

    ``TRIPS_RUN``    — raises ``RunBudgetExceeded``; the turn ends without a
                       reply and the caller settles it from the exception.
    ``LANDS_TURN``   — no exception: the agent is steered to a final checkpoint
                       reply and the turn settles as ``budget_exhausted``.
    ``REFUSES_CALL`` — no exception and no turn-level effect: the individual
                       call is refused with a typed row and the agent keeps
                       working with what it has.

    Recorded per budget so the accounting block states not only *that* a bound
    was hit but what hitting it did — which is the difference between "the turn
    died" and "one tool call was declined".
    """

    TRIPS_RUN = "trips_run"
    LANDS_TURN = "lands_turn"
    REFUSES_CALL = "refuses_call"

    @property
    def severity(self) -> int:
        """How much of the turn this expression costs. Higher = more terminal.

        Used for two things and nothing else: picking the run's headline
        ``bounded_by``, and letting one budget ESCALATE (the wall's graceful
        checkpoint opening, then its hard wall firing anyway — both true, and
        the row must report the worse one).
        """

        return _ENFORCEMENT_SEVERITY[self]


_ENFORCEMENT_SEVERITY: dict[RunBudgetEnforcement, int] = {
    RunBudgetEnforcement.REFUSES_CALL: 1,
    RunBudgetEnforcement.LANDS_TURN: 2,
    RunBudgetEnforcement.TRIPS_RUN: 3,
}


class RunBudgetTripReason(str, Enum):
    """WHY it tripped. Typed so no downstream reader matches on a message.

    Each member names one concrete exhaustion path, not a category. The two
    wall reasons are distinct because one lands the turn and the other kills it.
    """

    #: The graceful checkpoint opened; the turn lands with a final reply.
    WALL_CHECKPOINT_ENGAGED = "wall_checkpoint_engaged"
    #: The last-resort hard wall fired; the run is interrupted.
    WALL_CLOCK_EXCEEDED = "wall_clock_exceeded"
    #: Post-run api-call bound.
    API_CALLS_EXCEEDED = "api_calls_exceeded"
    #: Post-run total-token bound.
    TOTAL_TOKENS_EXCEEDED = "total_tokens_exceeded"
    #: An admitted MCP call was refused because the run's call budget was spent.
    MCP_CALLS_EXHAUSTED = "mcp_calls_exhausted"


#: Units, so a reader never has to guess whether ``limit`` is seconds or calls.
UNIT_SECONDS = "seconds"
UNIT_CALLS = "calls"
UNIT_TOKENS = "tokens"


@dataclass(slots=True)
class RunBudgetEntry:
    """One declared budget's limit, consumption and trip state.

    ``consumed_provider`` lets a mechanism that already owns a live counter
    (elapsed wall, the MCP meter, the guard's aggregate) be read at accounting
    time instead of pushing an update on every increment — one authority for the
    number, no second copy to drift.
    """

    kind: RunBudgetKind
    enforcement: RunBudgetEnforcement
    unit: str
    limit: float | None = None
    consumed: float | None = None
    consumed_provider: Callable[[], float | None] | None = field(default=None, repr=False)
    trip_reason: RunBudgetTripReason | None = None
    detail: str | None = None
    #: Monotonic order in which this budget first tripped; ties are broken by it.
    trip_seq: int | None = field(default=None, repr=False)

    @property
    def tripped(self) -> bool:
        return self.trip_reason is not None

    def resolved_consumed(self) -> float | None:
        """The consumption number, preferring an explicitly recorded value."""

        if self.consumed is not None:
            return self.consumed
        if self.consumed_provider is None:
            return None
        try:
            value = self.consumed_provider()
        except Exception:  # pragma: no cover - accounting must never fail a turn
            return None
        return _as_number(value)

    def row(self) -> dict[str, Any]:
        consumed = _round(self.resolved_consumed())
        limit = _round(self.limit)
        row: dict[str, Any] = {
            "kind": self.kind.value,
            "enforcement": self.enforcement.value,
            "unit": self.unit,
            "limit": limit,
            "consumed": consumed,
            "tripped": self.tripped,
            "trip_reason": self.trip_reason.value if self.trip_reason else None,
        }
        if limit is not None and consumed is not None:
            row["remaining"] = _round(max(0.0, limit - consumed))
        else:
            row["remaining"] = None
        if self.detail:
            row["detail"] = self.detail
        return row


class RunBudgetLedger:
    """Per-run bookkeeping for every budget the run declares.

    Thread-safe: the wall checkpoint records from a timer thread, the MCP meter
    from whichever thread dispatched a tool, and the guard from the tool-progress
    seam — the same reason ``McpCallBudget`` takes a lock.

    Never raises. Accounting that can fail a turn is worse than no accounting.
    """

    __slots__ = ("_lock", "_entries", "_order", "_trips")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[RunBudgetKind, RunBudgetEntry] = {}
        self._order: list[RunBudgetKind] = []
        self._trips = 0

    # -- declaration ----------------------------------------------------
    def declare(
        self,
        kind: RunBudgetKind,
        *,
        enforcement: RunBudgetEnforcement,
        unit: str,
        limit: float | None,
        consumed: float | None = None,
        consumed_provider: Callable[[], float | None] | None = None,
    ) -> RunBudgetEntry:
        """Record that this run is bounded by ``kind``, tripped or not.

        Only DECLARED budgets appear in the accounting block: a run with no wall
        budget must not show a wall row, or the block would report a bound that
        does not exist. Re-declaring a kind updates it in place, preserving any
        trip already recorded against it.
        """

        with self._lock:
            entry = self._entries.get(kind)
            if entry is None:
                entry = RunBudgetEntry(kind=kind, enforcement=enforcement, unit=unit)
                self._entries[kind] = entry
                self._order.append(kind)
            entry.enforcement = enforcement
            entry.unit = unit
            entry.limit = _as_number(limit)
            if consumed is not None:
                entry.consumed = _as_number(consumed)
            if consumed_provider is not None:
                entry.consumed_provider = consumed_provider
            return entry

    def observe(self, kind: RunBudgetKind, consumed: float | None) -> None:
        """Record consumption for an already-declared budget. No-op otherwise."""

        with self._lock:
            entry = self._entries.get(kind)
            if entry is None:
                return
            entry.consumed = _as_number(consumed)

    # -- trips ----------------------------------------------------------
    def trip(
        self,
        kind: RunBudgetKind,
        reason: RunBudgetTripReason,
        *,
        consumed: float | None = None,
        detail: str | None = None,
        enforcement: RunBudgetEnforcement | None = None,
    ) -> None:
        """Record that ``kind`` was exhausted, and how it was expressed.

        First trip per kind wins the row, with ONE exception: a trip that
        ESCALATES the same kind's enforcement replaces it. The wall budget is the
        reason that exists — its graceful checkpoint may open (``LANDS_TURN``)
        and its hard wall may then fire anyway (``TRIPS_RUN``); both happened,
        and the row an operator reads must report the worse one, not the first.

        Undeclared kinds are declared implicitly so a trip can never be lost to
        a missing declaration.
        """

        with self._lock:
            entry = self._entries.get(kind)
            if entry is None:
                entry = self.declare(
                    kind,
                    enforcement=enforcement
                    or _DEFAULT_ENFORCEMENT.get(kind, RunBudgetEnforcement.TRIPS_RUN),
                    unit=_DEFAULT_UNIT.get(kind, UNIT_CALLS),
                    limit=None,
                )
            if consumed is not None:
                entry.consumed = _as_number(consumed)
            escalates = (
                enforcement is not None
                and entry.trip_reason is not None
                and enforcement.severity > entry.enforcement.severity
            )
            if entry.trip_reason is None or escalates:
                if entry.trip_reason is None:
                    self._trips += 1
                    entry.trip_seq = self._trips
                if enforcement is not None:
                    entry.enforcement = enforcement
                entry.trip_reason = reason
                if detail:
                    entry.detail = detail

    @property
    def bounded_by(self) -> RunBudgetKind | None:
        """The budget that actually bounded this turn, or ``None``.

        The MOST TERMINAL tripped budget, first-trip order breaking ties: a
        refused MCP call did not bound the turn, it bounded one call, and a run
        that both refused a call and then hit its wall was bounded by the wall.
        """

        with self._lock:
            tripped = [entry for entry in self._entries.values() if entry.tripped]
            if not tripped:
                return None
            tripped.sort(key=lambda e: (-e.enforcement.severity, e.trip_seq or 0))
            return tripped[0].kind

    def entry(self, kind: RunBudgetKind) -> RunBudgetEntry | None:
        with self._lock:
            return self._entries.get(kind)

    # -- projection -----------------------------------------------------
    def accounting(self) -> dict[str, Any]:
        """The single accounting block. Safe to call repeatedly.

        Shape::

            {
              "bounded_by": "wall" | None,        # see the property: the MOST
                                                 # terminal tripped budget
              "trip_reason": "wall_clock_exceeded" | None,
              "enforcement": "trips_run" | None,  # what that did to the turn
              "tripped": ["wall"],                # every bound that was hit
              "budgets": [ {kind, enforcement, unit, limit, consumed,
                            remaining, tripped, trip_reason, detail?}, ... ],
            }

        ``budgets`` carries every DECLARED budget, tripped or not, so headroom is
        readable after the fact and "stopped at the bound" is distinguishable
        from "finished with room to spare".
        """

        with self._lock:
            rows = [self._entries[kind].row() for kind in self._order]
            bounded_by = self.bounded_by
            headline = self._entries.get(bounded_by) if bounded_by else None
            return {
                "bounded_by": bounded_by.value if bounded_by else None,
                "trip_reason": (
                    headline.trip_reason.value
                    if headline is not None and headline.trip_reason
                    else None
                ),
                "enforcement": headline.enforcement.value if headline is not None else None,
                "tripped": [row["kind"] for row in rows if row["tripped"]],
                "budgets": rows,
            }


_DEFAULT_ENFORCEMENT: dict[RunBudgetKind, RunBudgetEnforcement] = {
    RunBudgetKind.WALL: RunBudgetEnforcement.LANDS_TURN,
    RunBudgetKind.API_CALLS: RunBudgetEnforcement.TRIPS_RUN,
    RunBudgetKind.TOTAL_TOKENS: RunBudgetEnforcement.TRIPS_RUN,
    RunBudgetKind.MCP_CALLS: RunBudgetEnforcement.REFUSES_CALL,
}

_DEFAULT_UNIT: dict[RunBudgetKind, str] = {
    RunBudgetKind.WALL: UNIT_SECONDS,
    RunBudgetKind.API_CALLS: UNIT_CALLS,
    RunBudgetKind.TOTAL_TOKENS: UNIT_TOKENS,
    RunBudgetKind.MCP_CALLS: UNIT_CALLS,
}


#: Bound on the per-budget rows any persistence boundary will carry. The ledger
#: emits one row per declared mechanism (a handful); the cap exists so a
#: malformed/foreign block can never make a record unbounded.
ACCOUNTING_ROW_CAP = 32

#: The key the block is filed under everywhere it is carried: the run's
#: ``profile_timing``, the mission-chat turn journal record, and the
#: chat-history projection rows built from it. One spelling. S34 retired the
#: writerless ``AgentRun.llm`` carrier.
ACCOUNTING_KEY = "run_budget"


def safe_accounting_block(value: Any) -> dict[str, Any] | None:
    """The WHOLE accounting block, kept structured — or ``None``.

    See ``docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/run-budget-accounting.md`` §3 for the shape
    (``bounded_by`` / ``trip_reason`` / ``enforcement`` / ``tripped`` /
    ``budgets``). The block is carried **verbatim**: this reader bounds it and
    drops non-string keys, it never renames, reshapes or fills anything in, so
    every consumer reads the one contract the doc documents.

    Returns ``None`` when there is nothing to carry, so a caller can drop the
    key entirely rather than record an empty claim. "No block" and "a block that
    says nothing was bounded" are different facts and must stay different.
    """

    if not isinstance(value, dict):
        return None
    block: dict[str, Any] = {key: item for key, item in value.items() if isinstance(key, str)}
    if not block:
        return None
    rows = block.get("budgets")
    if isinstance(rows, list):
        block["budgets"] = [row for row in rows[:ACCOUNTING_ROW_CAP] if isinstance(row, dict)]
    return block


def turn_run_budget_metadata(*, result: Any = None, error: BaseException | None = None) -> dict[str, Any]:
    """The journal-metadata fragment for the run that just ended.

    A settle point splices this in (``**turn_run_budget_metadata(...)``) so the
    ledger block lands on the durable turn record under
    :data:`ACCOUNTING_KEY`. Two sources, because a run ends two ways:

    * ``result`` — a completed run, whose block rides ``profile_timing``;
    * ``error`` — a tripped run, where no result exists and the block rides
      ``RunBudgetExceeded.run_budget``.

    Returns ``{}`` when neither source carries a block: a result that never went
    through the runner, or an exception that is not a budget trip. Absent stays
    absent — no empty-dict backfill, so an older record stays honestly silent
    instead of claiming an accounting nobody took.

    Note the distinction this preserves: a real run that declared NO budget
    still produces a block, with zero ``budgets`` rows ("accounted, nothing
    bounded this turn"). That is carried, because collapsing it to absence would
    make an unbounded turn indistinguishable from a record written before any of
    this existed.
    """

    block = None
    if error is not None:
        block = safe_accounting_block(getattr(error, ACCOUNTING_KEY, None))
    if block is None and result is not None:
        timing = getattr(result, "profile_timing", None)
        if isinstance(timing, dict):
            block = safe_accounting_block(timing.get(ACCOUNTING_KEY))
    return {ACCOUNTING_KEY: block} if block is not None else {}


def _as_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return number


def _round(value: float | None) -> float | int | None:
    """Ints stay ints (call counts, tokens); seconds round to 0.1."""

    if value is None:
        return None
    if float(value).is_integer():
        return int(value)
    return round(float(value), 1)
