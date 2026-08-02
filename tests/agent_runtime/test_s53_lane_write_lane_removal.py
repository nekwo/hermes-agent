"""S53 removes the ``GoalRuntimeInstanceStore`` WRITE lane and four contracts.

Operator-ruled CUT on 2026-08-01, and the twin of S52: a write-lane cut, NOT a
store removal. ``status.py`` still projects ``lanes`` off ``list_all``, so the
read side is live and stays.

What was re-verified against the tree before the cut:

* ``create_lane`` — callers were four tests, no production code.
* ``transition`` — the one that needed care, because a bare ``transition`` grep
  returns 268 hits across the tree (``mission_chat_turns``,
  ``persona_chat_continuity``, ``mcp_lane`` and others all have their own).
  Checked by receiver: this store's ``transition`` was reached only from
  ``park_lane``, ``resume_lane`` and one test.
* ``park_open_task`` and ``mark_terminal_for_task`` — **zero** callers of any
  kind, not even a test. Neither had been reachable since the mission lane went.
* ``park_lane`` — exactly ONE non-test caller, and it was
  ``operator_control.py``. So the CUT ORDER inside this wave mattered:
  ``park_lane`` only became callerless once S49 landed, which is why S53 was
  re-verified after S49 rather than from the scout's original snapshot.
* ``resume_lane`` — **not on the original cut list**, and included because its
  entire body is a single ``transition`` call. Keeping it while removing
  ``transition`` would have left a method that raises ``AttributeError`` on its
  first line — a worse outcome than either cutting or keeping both. It has zero
  production callers in its own right.
* ``save`` — the chokepoint all six funnelled through, and the actual emitter of
  three of the four contracts. With its callers gone it had none.

**The transitive closure that went with them**, or it would have been residue:
``_ALLOWED_TRANSITIONS`` (``transition`` was its only reader — a state machine
with no writer to police is not a policy), the ``LANE_STATES`` vocabulary,
``TERMINAL_STATE`` (readers were ``mark_terminal_for_task`` and the two tables),
the write-time sanitisers ``_safe_reason`` / ``_safe_token``, and six imports.
``ACTIVE_STATE`` / ``PARKED_STATE`` / ``WAITING_STATE`` survive because
``active_for_task`` still reads them.

Count: 72 -> 68. ``foreground_runtime.closed`` is the LAST of the
``foreground_runtime.*`` family — S15 de-registered the other six when the
mission lane went, and this one outlived them only because
``mark_terminal_for_task`` was still standing. None of the four was ever in
``events.OPERATOR_SUMMARY_EVENT_TYPES``, so unlike S52 there is no summary arm
to retire alongside.

-----------------------------------------------------------------------------
MIGRATED TO ``test_tombstone_registry.py`` (2026-08-01)
-----------------------------------------------------------------------------

Three pure-ABSENCE cases left this file:

* ``test_every_lane_writer_is_gone`` -> nine ``CLASS_ATTR`` rows on
  ``runtime_instances.GoalRuntimeInstanceStore``. The registry carries TWO MORE
  than ``REMOVED_STORE_METHODS`` did (``active_foreground``,
  ``park_foreground_except``), so the migration widened the pin.
  ``REMOVED_STORE_METHODS`` went with the case.
* ``test_the_orphaned_module_level_names_are_gone`` -> five ``ATTR`` rows scoped
  to ``agent_runtime.runtime_instances`` (``LANE_STATES``,
  ``_ALLOWED_TRANSITIONS``, ``TERMINAL_STATE``, ``_safe_reason``,
  ``_safe_token``). ``REMOVED_MODULE_NAMES`` went with it.
* ``test_the_four_contracts_are_deregistered`` -> four ``EVENT`` rows.

WHY THE SURVIVORS STAYED. Everything left is either the READ side proving it
still works, a wire pin, or a set-level property:

* ``seed_lane_row`` is EXPORTED — ``test_s21_hollow_seam_ring`` imports it, and
  it is the only way any test can mint a persisted lane now that every writer is
  gone. It is not migratable at all.
* ``test_the_read_path_still_projects_a_persisted_lane`` /
  ``test_status_still_projects_lanes_off_the_surviving_read_path`` — behaviour
  against real on-disk state and the emitted ``harness status`` frame, including
  the S56 retarget (``status["lanes"]`` gone, ``runtime_instances["lanes"]``
  kept).
* ``test_the_three_state_constants_the_read_path_uses_survive`` and
  ``test_the_read_side_survives_whole`` — KEEPs.
* ``test_the_foreground_runtime_family_is_now_empty`` — a PREFIX property
  (``lane.*`` and ``foreground_runtime.*`` are both empty families). A list of
  rows cannot state that about a type nobody has invented yet.
* The APPEND refusal, ``test_historical_rows_still_read_back``,
  ``test_no_operator_summary_arm_went_missing`` and the delta-vs-S15 count pin —
  the same non-migratable set S44/S49/S52 keep.
"""

from __future__ import annotations

import inspect

import pytest
from hermes_time import now
from utils import atomic_json_write

from agent_runtime import paths, runtime_instances, status
from agent_runtime.decision_contract_registry import event_catalog
from agent_runtime.events import ALLOWED_EVENT_TYPES, OPERATOR_SUMMARY_EVENT_TYPES
from agent_runtime.models import GoalRuntimeInstance
from agent_runtime.runtime_instances import GoalRuntimeInstanceStore
from agent_runtime.serde import to_jsonable


#: The four contracts retired with the write lane.
RETIRED_EVENT_TYPES = (
    "lane.created",
    "lane.transitioned",
    "lane.transition_rejected",
    "foreground_runtime.closed",
)


def seed_lane_row(
    instance_id: str,
    *,
    task_id: str,
    state: str = "running",
    lane_kind: str = "production",
    priority: int = 5,
    **fields,
) -> GoalRuntimeInstance:
    """Write one lane row straight to disk, for READ-side tests.

    S53 deleted every lane writer, so a test that needs a persisted lane to read
    back can no longer mint one through the store. Seeding the row directly is
    the honest replacement: it exercises the surviving read path against real
    on-disk state instead of quietly dropping the coverage, and it is the same
    move ``test_status`` already makes for runs (``RunStore.update`` seeds the
    row because ``open_run`` went at S17).
    """

    ts = now()
    instance = GoalRuntimeInstance(
        id=instance_id,
        task_id=task_id,
        lane=instance_id,
        state=state,
        created_at=ts,
        updated_at=ts,
        started_by="test",
        lane_kind=lane_kind,
        priority=priority,
        **fields,
    )
    path = paths.runtime_instance_path(instance.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, to_jsonable(instance), indent=2, sort_keys=True)
    return instance


def test_the_three_state_constants_the_read_path_uses_survive():
    """Negative gate: the cut takes the constants nothing reads, and only those.
    ``active_for_task`` still elects a live row with these three."""

    assert runtime_instances.ACTIVE_STATE == "active"
    assert runtime_instances.PARKED_STATE == "parked"
    assert runtime_instances.WAITING_STATE == "waiting"
    source = inspect.getsource(GoalRuntimeInstanceStore.active_for_task)
    for name in ("ACTIVE_STATE", "WAITING_STATE", "PARKED_STATE"):
        assert name in source, name


def test_the_read_side_survives_without_the_orphaned_latest_alias():
    for reader in ("get", "list_all", "list_for_task", "active_for_task"):
        assert callable(getattr(GoalRuntimeInstanceStore, reader)), reader
    assert not hasattr(GoalRuntimeInstanceStore, "latest_for_task")
    assert callable(runtime_instances.runtime_instance_summary)
    assert callable(runtime_instances.runtime_instances_summary)


def test_the_read_path_still_projects_a_persisted_lane(isolate_agent_runtime_root):
    """The cut must not break reading; seeded on disk because nothing can mint."""

    seeded = seed_lane_row(
        "goalrt_s53read",
        task_id="task_s53",
        state="running",
        priority=2,
        current_stage_id="stage_1",
        current_owner="dev",
    )
    store = GoalRuntimeInstanceStore()

    assert [row.id for row in store.list_all()] == [seeded.id]
    assert store.get(seeded.id).state == "running"
    assert store.active_for_task("task_s53").id == seeded.id

    summary = runtime_instances.runtime_instance_summary(store.get(seeded.id))
    assert summary["lane_id"] == seeded.id
    assert summary["lane_kind"] == "production"
    assert summary["priority"] == 2
    assert summary["current_stage_id"] == "stage_1"
    assert summary["current_owner"] == "dev"

    lanes = runtime_instances.runtime_instances_summary(store.list_all())
    assert [row["lane_id"] for row in lanes["lanes"]] == [seeded.id]
    assert lanes["instances"] == lanes["lanes"]


def test_status_still_projects_lanes_off_the_surviving_read_path(isolate_agent_runtime_root):
    seed_lane_row("goalrt_s53status", task_id="task_s53_status", state="running")
    data = status.build_status()
    # RETARGETED at S56 (2026-08-01), not deleted: S53's claim is that the READ
    # path still projects lanes into `harness status` after the write lane went.
    # That claim survives verbatim; only the wire key moved. `status["lanes"]`
    # was a verbatim duplicate of `status["runtime_instances"]["lanes"]`, and
    # S56 took the duplicate off the wire. The removal is asserted alongside the
    # retargeted read so the pin cannot pass against a status that lost both.
    assert "lanes" not in data
    assert [row["lane_id"] for row in data["runtime_instances"]["lanes"]] == ["goalrt_s53status"]


def test_the_foreground_runtime_family_is_now_empty():
    """``foreground_runtime.closed`` was the last of its family; S15 took the
    other six with the mission lane."""

    assert [name for name in ALLOWED_EVENT_TYPES if name.startswith("foreground_runtime.")] == []
    assert [name for name in ALLOWED_EVENT_TYPES if name.startswith("lane.")] == []


def test_appending_a_retired_lane_event_is_refused():
    from agent_runtime.events import Event, EventLog

    for event_type in RETIRED_EVENT_TYPES:
        with pytest.raises(ValueError):
            EventLog().append(
                Event(ts=now(), type=event_type, task_id="task_1", run_id=None, persona_id=None)
            )


def test_historical_rows_still_read_back(isolate_agent_runtime_root):
    """S36/S44 precedent: deregistration gates APPENDS, not reads."""

    import json

    from agent_runtime.events import EventLog

    line = {
        "ts": "2026-07-01T00:00:00+00:00",
        "type": "lane.transitioned",
        "task_id": "task_historical",
        "run_id": None,
        "persona_id": None,
        "payload": {"runtime_instance_id": "goalrt_abc", "task_id": "task_historical", "state": "running"},
    }
    paths.events_path().parent.mkdir(parents=True, exist_ok=True)
    paths.events_path().write_text(json.dumps(line) + "\n", encoding="utf-8")

    rows = list(EventLog().iter_from_offset(0))
    assert [evt.type for _offset, evt in rows] == ["lane.transitioned"]
    assert rows[0][1].payload["runtime_instance_id"] == "goalrt_abc"


def test_no_operator_summary_arm_went_missing():
    """Unlike S52, none of the four was an operator-summary type."""

    assert set(RETIRED_EVENT_TYPES) & OPERATOR_SUMMARY_EVENT_TYPES == set()
    assert OPERATOR_SUMMARY_EVENT_TYPES <= ALLOWED_EVENT_TYPES


def test_the_registry_lost_exactly_four_contracts():
    """Delta-only; the absolute authority is S15's SURVIVING_EVENT_COUNT."""

    from tests.agent_runtime.test_s15_event_contract_pruning import SURVIVING_EVENT_COUNT

    assert [name for name in RETIRED_EVENT_TYPES if name in event_catalog()] == []
    assert len(event_catalog()) == SURVIVING_EVENT_COUNT
