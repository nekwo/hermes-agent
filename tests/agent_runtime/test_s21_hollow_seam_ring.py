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

Scope note (S21): ``open_tasks`` and ``running_runs`` are the same class of
constant, but ``hermes_cli/harness_parts/runtime_commands.py::_cmd_status``
indexes both for its human-readable line. That module is upstream-adjacent and
owned by another lane, so the two fields are deliberately RETAINED here and
pinned below — removing them without that one-line edit would break the
``harness status`` verb, which is the opposite of the goal.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from agent_runtime import observability, runtime_instances, status, stream
from agent_runtime.decision_contract_registry import event_catalog
from agent_runtime.events import ALLOWED_EVENT_TYPES, OPERATOR_SUMMARY_EVENT_TYPES, Event, operator_event_summary
from agent_runtime.runtime_instances import GoalRuntimeInstanceStore, runtime_instances_summary
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


def test_status_drops_the_retired_dispatch_routing_helpers():
    """The `next_actions` chain: routing policy for a lane that no longer exists."""

    for name in (
        "_next_action",
        "_stopped_progress",
        "_owner_for_action",
        "_has_budget_approval_path",
        "_has_budget_scope_recovery_path",
        "_has_budget_incident",
    ):
        assert not hasattr(status, name), f"status.{name} survived S21"


def test_status_keeps_every_field_an_operator_can_still_learn_from(isolate_agent_runtime_root):
    """Keep-side pin: shrinking the payload must not hollow out the verb."""

    data = build_status()

    for key in (
        # Retained on purpose — see the module docstring's scope note.
        "open_tasks",
        "running_runs",
        # Live, data-driven, and the reason `harness status` exists.
        "open_incidents",
        "dirty",
        "dirty_summary",
        "dirty_state",
        "runtime_health",
        "runtime_config",
        "agents",
        "observability",
        "runtime_instances",
        "lanes",
        "parity",
        "decision_contract_hash",
    ):
        assert key in data, f"S21 dropped a live status field: {key}"


# --------------------------------------------------------------------------
# runtime_instances.py — a foreground lane that cannot resolve
# --------------------------------------------------------------------------


def test_the_foreground_lane_resolver_is_gone():
    """`active_foreground` walked lanes into `TaskStore.get`, which always raises.

    `TaskStoreStub.get` raises `NotFound` unconditionally (ruling R-3), so every
    iteration hit `continue` and the method returned `None` by construction — not
    because no lane was live.
    """

    assert not hasattr(GoalRuntimeInstanceStore, "active_foreground")
    # Same hollow shape: it took a task_id and a reason and returned `[]`.
    assert not hasattr(GoalRuntimeInstanceStore, "park_foreground_except")


def test_the_lane_summary_drops_its_hardcoded_foreground_block(isolate_agent_runtime_root):
    summary = runtime_instances_summary([])

    for key in ("foreground", "foreground_active_count", "background_parked_count", "background_task_ids"):
        assert key not in summary, f"runtime_instances_summary still publishes constant {key!r}"
    # Keep-side: the lane rows themselves are real and stay.
    assert "instances" in summary
    assert "lanes" in summary


def test_the_lane_summary_still_carries_real_lanes(isolate_agent_runtime_root):
    store = GoalRuntimeInstanceStore()
    lane = store.create_lane(task_id="task_s21", started_by="test", state="running")

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
        "incident.opened": "incident.opened",
        "incident.closed": "incident.closed",
        "board.card.created": "event.appended",
    }
    for event_type, expected in cases.items():
        assert event_type in ALLOWED_EVENT_TYPES
        assert _delta_op(Event(ts=now(), type=event_type, task_id=None, run_id=None, persona_id=None)) == expected


def test_delta_op_has_no_leftover_prefix_literals():
    source = (stream.__file__ or "")
    assert source
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    for literal in ('"task."', '"proof.attached"', '"daemon."', '"proof.added"', '"daemon.status"'):
        assert literal not in text, f"stream.py still references {literal}"


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


def test_every_operator_summary_type_is_still_registered():
    assert OPERATOR_SUMMARY_EVENT_TYPES <= frozenset(event_catalog())
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

    bundle = Event(
        ts=now(),
        type="repo_bundle.assigned",
        task_id=None,
        run_id=None,
        persona_id=None,
        payload={"repo": "launcher", "state": "assigned"},
    )
    assert operator_event_summary(bundle) == "Assigned launcher bundle (assigned)."


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


def test_the_event_display_title_drops_kinds_its_classifier_cannot_produce():
    """`_event_display_kind` never returns 'proof' or 'qa_verdict' — both arms were unreachable."""

    source = observability.__file__ or ""
    assert source
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert 'kind == "proof"' not in text
    assert 'kind == "qa_verdict"' not in text
    assert '"qa.verdict_recorded"' not in text
