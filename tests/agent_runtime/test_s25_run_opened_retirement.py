"""S25 — ``run.opened`` retirement (the delta S17 deliberately deferred).

Its writer (``RunStore.open_run``) went with the Tier-2 store residue at S17.
The contract stayed registered for one reason only: two filler appends in
``tests/agent_runtime/test_events.py`` still minted it, and de-registering first
would have converted a stale test into a crash. Those appends are retargeted
onto live types in the same commit, so the contract now has no producer at all —
and an ``EventContract`` with no producer is a shape the prompt contract
advertises, the manifest publishes, and ``EventLog.append`` accepts (S15's
standing rule).

The operator-summary row and its formatter branch go WITH the registration, not
because history stopped mattering but because the branch becomes unreachable by
construction: ``operator_event_summary`` early-returns ``None`` for any type
outside ``OPERATOR_SUMMARY_EVENT_TYPES``, and that frozenset may not name a
de-registered type (events.py's own invariant, pinned by
``test_s21_hollow_seam_ring``). Keeping the ``run.opened`` arm would leave dead
code that looks like history support.

Historical rows are unaffected. ``ALLOWED_EVENT_TYPES`` is enforced on WRITE
only, so the 1,882 ``run.opened`` rows in the live log (2026-07-30, root
``agent-runtime``; 16 of them carry a stamped ``summary``) still decode and read
back verbatim — which the last test pins.
"""

from __future__ import annotations

import json

from hermes_time import now

from agent_runtime import paths
from agent_runtime.events import (
    ALLOWED_EVENT_TYPES,
    OPERATOR_SUMMARY_EVENT_TYPES,
    Event,
    EventLog,
    operator_event_summary,
)
from agent_runtime.serde import to_jsonable

RETIRED = "run.opened"






def test_the_operator_summary_row_and_its_formatter_went_with_the_contract():
    assert RETIRED not in OPERATOR_SUMMARY_EVENT_TYPES
    evt = Event(ts=now(), type=RETIRED, task_id=None, run_id="run_1", persona_id="dev", payload={"stage_id": "s1"})
    assert operator_event_summary(evt) is None


def test_historical_run_closed_rendering_is_not_collateral():
    """The write contract retired later; historical display remains."""

    assert "run.closed" not in ALLOWED_EVENT_TYPES
    assert "run.closed" in OPERATOR_SUMMARY_EVENT_TYPES


def test_historical_rows_still_read_back(isolate_agent_runtime_root):
    """De-registration is a WRITE gate. The live log's 1,882 rows must survive
    it — a reader that choked on them would lose the whole slice, not one row."""

    log = EventLog()
    log.append(
        Event(
            ts=now(),
            type="persona_instance.created",
            task_id=None,
            run_id=None,
            persona_id="dev",
            payload={"persona_instance_id": "personainst_dev"},
        )
    )
    historical = Event(
        ts=now(),
        type=RETIRED,
        task_id="task_8b07842b",
        run_id="run_555056e522c8",
        persona_id="pm",
        payload={"stage_id": None, "tick_id": "tick_b91b2627"},
    )
    with open(paths.events_path(), "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(to_jsonable(historical), ensure_ascii=False, separators=(",", ":")) + "\n")

    tail = EventLog().tail(5)
    assert [event.type for event in tail] == ["persona_instance.created", RETIRED]
    assert tail[-1].run_id == "run_555056e522c8"
