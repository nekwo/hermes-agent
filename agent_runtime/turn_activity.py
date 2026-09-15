"""Is a chat turn ADMITTED right now? One counter, one authority.

**The gap this closes.** ``profile_runner.agent_runs_in_flight()`` counts real
agent runs: ``_counted_agent_run`` increments at ``ProfileAgentRunner.run()``'s
entry and decrements at its exit. Two consumers take decisions on it — the
snapshot demote deferral (``stream._defer_demote_build_for_active_turns``) and
the chat-actor prewarm's yield (``persona_chat_actor_prewarm.prewarm_chat_actor``,
two reads) — and both were written when "a turn is happening" and "a run is in
flight" looked like the same fact.

They are not. A mission-chat turn is admitted at the handler's monotonic anchor
and does its whole pre-admit assembly — config, persona, instance store, the
chat-lane visibility bundle, the turn context, the prompt-observability row, the
write-ahead persist — before any runner exists. Measured on this PC 2026-09-07
(``planned/chat-turn-prep-cost.md`` §0.1): that span was 906 ms on the one turn
nothing else was running and 2,796–3,172 ms on the two turns a led core build
and a 5,750 ms actor prewarm overlapped it. Both of those overlaps are things
that YIELD to a live turn, and neither could see one, because the span is
invisible to ``_ACTIVE_RUNS`` by construction. Turn 3's three
``snapshot_build_deferred`` receipts prove the mechanism works where it can see;
turns 1 and 2 prove where it cannot.

**CP-2, stated:** an ADMITTED turn owns the GIL, not only a RUNNING one. This
module is that counter — incremented where the turn's :class:`TurnPhaseMarks`
is constructed at the handler anchor, decremented when the handler exits by any
path. ``agent_runs_in_flight()`` keeps its meaning and its callers unchanged;
the two consumers above will read ``admitted() or running()``.

**What Stage 6 does with it, and what it does NOT.** Stage 6 builds this as an
INSTRUMENT and nothing more: the deferral logs ``admitted_at_exit=`` beside its
existing ``runs_in_flight_at_exit=``, and the turn record gains
``prewarm_overlapped``. No decision anywhere reads this counter yet — that is
Stage 7, and keeping the two landings apart is what lets Stage 7's numbers be
read against receipts this stage already put on the record.

**Why a module and not a flag on the marks object.** The readers are a stream
hub thread and a prewarm worker thread; neither holds the turn's marks, and
neither may. The counter is therefore process-scoped, exactly as
``_ACTIVE_RUNS`` is, and for the same reason: the question is "is this PROCESS
inside a turn", asked by something that is about to spend the same GIL.

**Cost.** One lock acquisition on entry and one on exit, per TURN.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

_LOCK = threading.Lock()
_ADMITTED = 0


def chat_turns_admitted() -> int:
    """How many mission-chat turns this process is inside RIGHT NOW.

    ``0`` is a measurement: no turn is admitted. There is no "unknown" here —
    the counter is this module's own and always answerable. Callers that reach
    it across an import boundary they cannot assume (``agent_runtime.stream``,
    the command parts exec'd into ``harness.py``) turn an unreachable module
    into ``None`` at THEIR seam, so an absence stays distinguishable from a
    zero on the record.
    """

    with _LOCK:
        return _ADMITTED


@contextmanager
def admitted_turn():
    """Hold the admitted count for the life of one turn.

    Entered at the handler anchor — the same statement that constructs the
    turn's :class:`~agent_runtime.mission_chat_phases.TurnPhaseMarks`, so the
    counted window and the measured window are the same window by construction
    rather than by two call sites agreeing.

    Released in a ``finally``: the mission-chat handler has fourteen terminal
    transitions in its commit phase and a dozen refusals above them, and a count
    that leaked on any one of them would — once Stage 7 reads it — wedge the
    snapshot demote lane for the life of the process.
    """

    global _ADMITTED
    with _LOCK:
        _ADMITTED += 1
    try:
        yield
    finally:
        with _LOCK:
            _ADMITTED -= 1


__all__ = ["admitted_turn", "chat_turns_admitted"]
