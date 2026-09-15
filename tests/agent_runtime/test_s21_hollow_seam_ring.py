"""S21 shrinks the surviving status/observe payloads to what can still be true.

The mission lane took its producers with it, but four modules kept *publishing*
the shapes those producers used to fill: ``build_status`` still emitted a
``blocked_tasks`` count over a hardcoded ``tasks = []``, ``runtime_instances``
still advertised a foreground lane that ``TaskStoreStub`` makes structurally
unreachable, ``_delta_op`` still routed ``task.*`` / ``proof.attached`` /
``daemon.*`` prefixes that ``ALLOWED_EVENT_TYPES`` now refuses on append, and
``OPERATOR_SUMMARY_EVENT_TYPES`` still listed four de-registered types.

A constant field is not harmless. An operator reading ``blocked_tasks: 0`` or
``foreground: null`` reads it as a *measurement* — "nothing is blocked right
now" — when it is a literal. That is the same class of lie as a fake button:
the verb answers, but the answer carries no information. This stage keeps every
verb and every field a reader can still learn something from, and drops the rest.

Scope note (S21, CLOSED by S28): ``open_tasks`` and ``running_runs`` are the
same class of constant, but ``hermes_cli/harness_parts/runtime_commands.py::_cmd_status``
indexed both for its human-readable line, and that module was owned by another
lane — removing them without that one-line edit would have broken the
``harness status`` verb, which is the opposite of the goal. S28 landed both
halves together once the module was free; the two fields are gone, and the pin
below moved to
``tests/agent_runtime/test_s28_status_observe_shrink.py::test_status_drops_the_two_fields_s21_could_not_reach``.
The keep-side list here now covers only fields S21 itself never questioned.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from agent_runtime import observability
from agent_runtime.decision_contract_registry import event_catalog
from agent_runtime.events import ALLOWED_EVENT_TYPES, OPERATOR_SUMMARY_EVENT_TYPES, Event, operator_event_summary
from agent_runtime.runtime_instances import GoalRuntimeInstanceStore, runtime_instances_summary
from tests.agent_runtime.test_s53_lane_write_lane_removal import seed_lane_row
from agent_runtime.status import build_status
from agent_runtime.stream import _delta_op
from hermes_time import now


# --------------------------------------------------------------------------
# status.py — the task half of the payload was computed over `tasks = []`
# --------------------------------------------------------------------------


def test_status_drops_the_fields_computed_over_the_empty_task_list(isolate_agent_runtime_root):
    data = build_status()

    # `tasks` is a hardcoded `[]` literal, so each of these could only ever be
    # 0 / [] — a constant reported in the shape of a measurement.
    assert "blocked_tasks" not in data
    assert "next_actions" not in data
    assert "undispatchable_missions" not in data




def test_status_keeps_every_field_an_operator_can_still_learn_from(isolate_agent_runtime_root):
    """Keep-side pin: shrinking the payload must not hollow out the verb."""

    data = build_status()

    for key in (
        # Live, data-driven, and the reason `harness status` exists.
        # (`open_tasks` / `running_runs` were pinned here as deliberate S21
        # residue; S28 removed them — see the module docstring's scope note.)
        "open_incidents",
        "dirty",
        "dirty_summary",
        "dirty_state",
        "runtime_health",
        "runtime_config",
        "agents",
        "observability",
        "runtime_instances",
        "parity",
        "event_contract_hash",
    ):
        assert key in data, f"S21 dropped a live status field: {key}"
    # RETARGETED at S56 (2026-08-01), not deleted: `lanes` was pinned here as a
    # live top-level status field. It was a verbatim duplicate of
    # `runtime_instances["lanes"]`, so S56 took the top-level copy off the wire
    # and kept the block. The claim ("an operator can still learn lanes from
    # `harness status`") is preserved by moving the pin to the surviving path,
    # and the removal is asserted so the reversal stays visible here.
    assert "lanes" not in data
    assert "lanes" in data["runtime_instances"]


# --------------------------------------------------------------------------
# runtime_instances.py — a foreground lane that cannot resolve
# --------------------------------------------------------------------------




def test_the_lane_summary_drops_its_hardcoded_foreground_block(isolate_agent_runtime_root):
    summary = runtime_instances_summary([])

    for key in ("foreground", "foreground_active_count", "background_parked_count", "background_task_ids"):
        assert key not in summary, f"runtime_instances_summary still publishes constant {key!r}"
    # Keep-side: the lane rows themselves are real and stay.
    assert "instances" in summary
    assert "lanes" in summary


def test_the_lane_summary_still_carries_real_lanes(isolate_agent_runtime_root):
    """S53 note: the lane is now SEEDED on disk rather than minted through
    ``create_lane``, which was deleted with the write lane. The assertion is
    unchanged and still the point of the test -- S21's finding was that the
    summary's foreground fields were constants while the lane ROWS are real, and
    the rows are still real."""

    lane = seed_lane_row("goalrt_s21", task_id="task_s21", state="running")
    store = GoalRuntimeInstanceStore()

    summary = runtime_instances_summary(store.list_all())

    assert [row["lane_id"] for row in summary["lanes"]] == [lane.id]
    assert summary["instances"] == summary["lanes"]


# --------------------------------------------------------------------------
# stream.py — `_delta_op` arms for event families that cannot be appended
# --------------------------------------------------------------------------


UNREACHABLE_DELTA_OPS = frozenset({"task.upserted", "task.state_changed", "proof.added", "daemon.status"})


def test_no_registered_event_type_can_reach_a_removed_delta_op():
    for event_type in event_catalog():
        op = _delta_op(Event(ts=now(), type=event_type, task_id=None, run_id=None, persona_id=None))
        assert op not in UNREACHABLE_DELTA_OPS, f"{event_type} still routes to {op}"


def test_the_removed_delta_op_arms_no_longer_classify_their_old_prefixes():
    """A de-registered type must fall through to the generic arm, not its old label."""

    for event_type in ("task.transition", "task.blocked", "proof.attached", "daemon.started"):
        assert event_type not in ALLOWED_EVENT_TYPES
        assert _delta_op(Event(ts=now(), type=event_type, task_id=None, run_id=None, persona_id=None)) == "event.appended"


def test_the_surviving_delta_op_routing_is_untouched():
    cases = {
        "run.tool.started": "chat.trace.appended",
        "run.tool.finished": "chat.trace.appended",
        "run.progress": "chat.trace.appended",
        "persona_assignment.created": "instance.upserted",
        "persona_assignment.closed": "instance.upserted",
            "board.card.created": "event.appended",
    }
    for event_type, expected in cases.items():
        assert event_type in ALLOWED_EVENT_TYPES
        assert _delta_op(Event(ts=now(), type=event_type, task_id=None, run_id=None, persona_id=None)) == expected




# --------------------------------------------------------------------------
# events.py — operator-summary rows for types the registry dropped
# --------------------------------------------------------------------------


DE_REGISTERED_OPERATOR_SUMMARY_TYPES = frozenset(
    # S25 added run.opened: its contract went with the last two appends that
    # minted it, so its summary row and formatter arm went too (the arm is
    # unreachable once the type leaves OPERATOR_SUMMARY_EVENT_TYPES). See
    # tests/agent_runtime/test_s25_run_opened_retirement.py.
    {"delivery.intent", "patch.proposed", "role_session.closed", "run.approval_required", "run.opened"}
)


def test_every_live_operator_summary_type_is_still_registered():
    assert OPERATOR_SUMMARY_EVENT_TYPES - {"run.closed"} <= frozenset(event_catalog())
    assert "run.closed" not in event_catalog()
    assert OPERATOR_SUMMARY_EVENT_TYPES & DE_REGISTERED_OPERATOR_SUMMARY_TYPES == frozenset()


def test_the_de_registered_types_get_no_operator_summary():
    for event_type in DE_REGISTERED_OPERATOR_SUMMARY_TYPES:
        evt = Event(ts=now(), type=event_type, task_id=None, run_id=None, persona_id="dev", payload={})
        assert operator_event_summary(evt) is None


def test_the_surviving_operator_summaries_still_render():
    closed = Event(
        ts=now(),
        type="run.closed",
        task_id=None,
        run_id="run_1",
        persona_id="dev",
        payload={"state": "completed", "decision_type": "deliver"},
    )
    assert operator_event_summary(closed) == "Closed dev run as completed after deliver."

    tool = Event(
        ts=now(),
        type="run.tool.started",
        task_id=None,
        run_id="run_1",
        persona_id="dev",
        payload={"tool_name": "terminal"},
    )
    assert operator_event_summary(tool) == "terminal started."

    # S52: the third case here rendered ``repo_bundle.assigned`` as
    # "Assigned launcher bundle (assigned)." That type was the last
    # repo_bundle.* survivor in OPERATOR_SUMMARY_EVENT_TYPES and it left the
    # frozenset with the RepoBundleStore write lane, taking its shared formatter
    # arm with it. Replaced by a still-live arm rather than dropped, so this test
    # keeps covering more than one renderer branch.
    progress = Event(
        ts=now(),
        type="run.progress",
        task_id=None,
        run_id="run_1",
        persona_id="dev",
        payload={"phase": "proof", "step": "compile", "status": "running"},
    )
    assert operator_event_summary(progress) == "Progress: proof compile running."


# --------------------------------------------------------------------------
# observability.py — display arms keyed on de-registered event types
# --------------------------------------------------------------------------


def test_the_event_display_kind_arms_for_de_registered_types_are_gone():
    for event_type in ("qa.verdict_recorded", "task.blocked"):
        assert event_type not in ALLOWED_EVENT_TYPES
        assert observability._event_display_kind(event_type, {}) == "event"


def test_the_surviving_event_display_kinds_are_untouched():
    assert observability._event_display_kind("incident.opened", {}) == "blocker"
    assert observability._event_display_kind("self_test.recorded", {}) == "self_test"
    assert observability._event_display_kind("run.tool.started", {}) == "tool_call"
    assert observability._event_display_kind("packet.recorded", {"packet_type": "delivery"}) == "delivery"
    assert observability._event_display_kind("packet.recorded", {}) == "handoff"
    assert (
        observability._event_display_kind("run.progress", {"step": "reasoning_summary"}) == "thinking_summary"
    )
