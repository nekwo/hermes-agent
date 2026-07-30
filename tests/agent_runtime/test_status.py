from hermes_time import now
import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")
from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.models import Incident
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




def test_status_lane_only_does_not_report_background_task_ids(isolate_agent_runtime_root):
    ts = TaskStore()
    n = now()
    ts.create(Task(id="task_foreground", title="F", description="d", state=TaskState.CREATED, created_at=n, updated_at=n, requested_by="tony"))
    ts.create(Task(id="task_background", title="B", description="d", state=TaskState.BLOCKED, created_at=n, updated_at=n, requested_by="tony"))
    runtimes = GoalRuntimeInstanceStore()
    runtimes.create_lane(task_id="task_foreground", started_by="test", state="running")
    runtimes.park_open_task("task_background", reason="test parked")

    s = build_status(task_store=ts)

    assert s["open_tasks"] == 2
    assert s["background_open_tasks"] == 0
    assert s["background_task_ids"] == []


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


def test_status_surfaces_lanes_repo_locks_and_swarm_budget(isolate_agent_runtime_root):
    runs = RunStore()
    lane = GoalRuntimeInstanceStore().create_lane(task_id="task_lane", started_by="test")
    GoalRuntimeInstanceStore().transition(lane.id, "activating", reason="activate")
    GoalRuntimeInstanceStore().transition(lane.id, "running", reason="run")
    acquire_repo_bundle_locks(lane_id=lane.id, task_id="task_lane", bundle_ids=["bundle_backend"], mode="write")
    run = runs.open_run("dev", "task_lane")
    run.llm = {"total_tokens": 12, "api_calls": 2}
    runs.update(run)

    s = build_status(run_store=runs)

    assert s["lanes"][0]["lane_id"] == lane.id
    assert s["repo_locks"]["lock_count"] == 1
    assert s["swarm_budget"]["global"] == {"total_tokens": 12, "api_calls": 2}
    assert s["production_envelope"]["production_ready"] is True
    assert {item["id"] for item in s["production_envelope"]["items"]} >= {"H5", "H6", "H7", "H8", "H9", "H10"}


def test_status_marks_next_action_blocked_by_open_incident():
    ts=TaskStore(); n=now(); ts.create(Task(id="t", title="T", description="d", state=TaskState.CREATED, created_at=n, updated_at=n, requested_by="tony"))
    incidents=IncidentStore(); incidents.open(Incident(id="i", task_id="t", run_id=None, kind="provider_failure", summary="auth", detail_path=None, opened_at=n))
    s=build_status(task_store=ts, incident_store=incidents)
    assert s["next_actions"][0]["action"] == "blocked_by_incident"








def test_status_blocks_budget_incident_after_continuation_cap():
    ts = TaskStore()
    runs = RunStore()
    incidents = IncidentStore()
    n = now()
    ts.create(Task(id="t", title="T", description="d", state=TaskState.RUNNING, created_at=n, updated_at=n, requested_by="tony"))
    for idx in range(2):
        approved = runs.open_run("dev", "t", stage_id="stage_1", session_id="session_budget")
        approved.state = RunState.WAITING_ON_APPROVAL
        approved.error = {"type": "run_budget_exceeded"}
        runs.update(approved)
        runs.approve_continuation(approved.id)
    waiting = runs.open_run("dev", "t", stage_id="stage_1", session_id="session_budget")
    waiting.state = RunState.WAITING_ON_APPROVAL
    waiting.error = {"type": "run_budget_exceeded"}
    runs.update(waiting)
    incidents.open(Incident(id="inc_budget", task_id="t", run_id=waiting.id, kind="run_budget_exceeded", summary="budget", detail_path=None, opened_at=n))

    s = build_status(task_store=ts, run_store=runs, incident_store=incidents)

    assert s["next_actions"][0]["action"] == "blocked_by_incident"
    assert s["next_actions"][0]["reason"] == "budget continuation cap reached; human review required"
    assert s["next_actions"][0]["stopped_progress"]["reason"] == "budget_continuation_blocked"


def test_status_routes_read_search_budget_loop_to_neko_scope_recovery(isolate_agent_runtime_root):
    ts = TaskStore()
    runs = RunStore()
    incidents = IncidentStore()
    n = now()
    ts.create(Task(id="t", title="T", description="d", state=TaskState.RUNNING, created_at=n, updated_at=n, requested_by="tony", open_incident_ids=["inc_loop"]))
    waiting = runs.open_run("backend_dev", "t", stage_id="backend_implementation", session_id="session_budget")
    waiting.progress = {
        "loop_warning": "read_search_without_patch_threshold",
        "read_search_count": 6,
        "read_search_limit": 6,
        "patch_count": 0,
        "proof_count": 0,
    }
    waiting.state = RunState.WAITING_ON_APPROVAL
    waiting.error = {"type": "run_budget_exceeded"}
    runs.update(waiting)
    incidents.open(Incident(id="inc_loop", task_id="t", run_id=waiting.id, kind="run_budget_exceeded", summary="budget", detail_path=None, opened_at=n))

    s = build_status(task_store=ts, run_store=runs, incident_store=incidents)

    assert s["next_actions"][0]["action"] == "run_slot"
    assert s["next_actions"][0]["reason"] == "Dev exhausted read/search without patch or proof; Neko must split or narrow the stage before retry"
    assert s["next_actions"][0]["stopped_progress"]["owner"] == "neko_supervisor"


def test_status_reports_retired_dispatch_lane(isolate_agent_runtime_root):
    ts = TaskStore()
    n = now()
    ts.create(
        Task(
            id="t_broken",
            title="T",
            description="d",
            state=TaskState.RUNNING,
            created_at=n,
            updated_at=n,
            requested_by="tony",
        )
    )

    status = build_status(task_store=ts)

    assert status["next_actions"][0]["action"] == "undispatchable"
    assert status["undispatchable_missions"] == [
        {"task_id": "t_broken", "reason": "the task dispatch lane has been retired"}
    ]
