from hermes_time import now

from agent_runtime.models import AgentRun, Task
from agent_runtime.runtime_config import NormalWorkerFlowConfig, RuntimeConfig
from agent_runtime.states import RunState, TaskState
from agent_runtime.worker_actions import worker_actions_for_role


def make_blocked_task(open_incident_ids=None):
    ts = now()
    return Task(
        id="task_blocked",
        title="Blocked mission",
        description="d",
        state=TaskState.BLOCKED,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        open_incident_ids=list(open_incident_ids or []),
    )


def make_neko_run():
    ts = now()
    return AgentRun(
        id="run_neko",
        persona_id="neko_supervisor",
        task_id="task_blocked",
        stage_id=None,
        state=RunState.RUNNING,
        started_at=ts,
        last_heartbeat_at=ts,
    )


def normal_flow_config():
    return RuntimeConfig(normal_worker_flow=NormalWorkerFlowConfig(enabled=True))


def test_blocked_with_open_incident_keeps_resolve_incident_primary():
    task = make_blocked_task(open_incident_ids=["inc_1"])
    actions = worker_actions_for_role("neko_supervisor", task, make_neko_run(), config=normal_flow_config())

    primary = next(action for action in actions if action.primary)
    assert primary.shape_id == "neko.resolve_incident"


def test_blocked_without_open_incident_offers_rescope_not_resolve_incident():
    task = make_blocked_task()
    actions = worker_actions_for_role("neko_supervisor", task, make_neko_run(), config=normal_flow_config())

    shape_ids = [action.shape_id for action in actions]
    assert "neko.resolve_incident" not in shape_ids
    primary = next(action for action in actions if action.primary)
    assert primary.shape_id == "neko.scoped_handoff"
