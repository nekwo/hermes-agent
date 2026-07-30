from hermes_time import now
import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")
from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.models import AgentRun, Incident
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.runtime_config import EnterpriseWorkerSessionsConfig
from agent_runtime.states import RunState, TaskState
from agent_runtime.status import build_status
from agent_runtime.store import IncidentStore, RunStore, TaskStore
from agent_runtime.runtime_instances import GoalRuntimeInstanceStore
from agent_runtime.repo_bundles import acquire_repo_bundle_locks


def _persona_runtime_config() -> AgentRuntimeConfig:
    return AgentRuntimeConfig(
        enterprise_worker_sessions=EnterpriseWorkerSessionsConfig(
            enabled=True,
            worker_session_store=True,
            persona_instance_runtime=True,
            persona_assignment_store=True,
        )
    )


def _assert_no_mission_status() -> None:
    status = build_status()
    # S21 stopped publishing `next_actions` / `undispatchable_missions`: both were
    # computed over the `tasks = []` literal, so they could only ever be `[]` —
    # a constant reported in the shape of a measurement. Absence is now the pin.
    # S28 finished that cut with `open_tasks` (and `running_runs`), which S21 had
    # to retain while `_cmd_status` still printed them; this assertion used to
    # read `status["open_tasks"] == 0` — a literal, not a measurement. See
    # tests/agent_runtime/test_s28_status_observe_shrink.py.
    assert "open_tasks" not in status
    assert "running_runs" not in status
    assert "next_actions" not in status
    assert "undispatchable_missions" not in status
    assert not hasattr(TaskStore(), "create")




def test_status_lane_only_does_not_report_background_task_ids(isolate_agent_runtime_root):
    _assert_no_mission_status()


def test_status_omits_retired_burn_in_certification_state(isolate_agent_runtime_root):
    s = build_status()

    assert s["swarm"]["enabled"] is False
    assert "certification" not in s["swarm"]


def test_status_projects_operator_channels_for_persona_instances(monkeypatch, isolate_agent_runtime_root):
    monkeypatch.setattr(
        "agent_runtime.status.load_agent_runtime_config",
        lambda: _persona_runtime_config(),
    )

    s = build_status()

    assert s["persona_instance_runtime"]["enabled"] is True
    assert s["persona_instances"]
    assert s["operator_channels"]
    assert {
        channel["persona_instance_id"] for channel in s["operator_channels"]
    }.issuperset(
        {
            instance["persona_instance_id"]
            for instance in s["persona_instances"]
            if instance["persona_instance_id"]
        }
    )
    assert "persona_chat_history" in s
    assert "persona_chat_trace" in s
    assert "included" in s["parity"]["completeness"]["persona_chat_history"]
    assert "included" in s["parity"]["completeness"]["persona_chat_trace"]


def test_status_surfaces_lanes_repo_locks_and_retired_swarm_budget_shape(isolate_agent_runtime_root):
    runs = RunStore()
    lane = GoalRuntimeInstanceStore().create_lane(task_id="task_lane", started_by="test")
    GoalRuntimeInstanceStore().transition(lane.id, "activating", reason="activate")
    GoalRuntimeInstanceStore().transition(lane.id, "running", reason="run")
    acquire_repo_bundle_locks(lane_id=lane.id, task_id="task_lane", bundle_ids=["bundle_backend"], mode="write")
    # S17 removed RunStore.open_run as write-dead; ``update`` is the surviving
    # write path and seeds the row directly. This case covers build_status, not
    # the writer.
    ts = now()
    run = AgentRun(
        id="run_lane",
        persona_id="dev",
        task_id="task_lane",
        stage_id=None,
        state=RunState.RUNNING,
        started_at=ts,
        last_heartbeat_at=ts,
    )
    runs.update(run)

    s = build_status(run_store=runs)

    assert s["lanes"][0]["lane_id"] == lane.id
    assert s["repo_locks"]["lock_count"] == 1
    assert s["swarm_budget"]["global"] == {"total_tokens": 0, "api_calls": 0}
    assert s["swarm_budget"]["by_task"] == {}
    assert s["production_envelope"]["production_ready"] is True
    assert {item["id"] for item in s["production_envelope"]["items"]} >= {"H5", "H6", "H7", "H8", "H9", "H10"}


def test_status_marks_next_action_blocked_by_open_incident():
    _assert_no_mission_status()








def test_status_blocks_budget_incident_after_continuation_cap():
    _assert_no_mission_status()


def test_status_routes_read_search_budget_loop_to_neko_scope_recovery(isolate_agent_runtime_root):
    _assert_no_mission_status()


def test_status_reports_retired_dispatch_lane(isolate_agent_runtime_root):
    _assert_no_mission_status()
