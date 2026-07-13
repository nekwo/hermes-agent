from hermes_time import now

from agent_runtime.default_plan import ensure_default_mission_plan
from agent_runtime.models import AgentRun, Task
from agent_runtime.runtime_config import NormalWorkerFlowConfig, RuntimeConfig
from agent_runtime.models import TaskStage
from agent_runtime.states import RunState, StageStatus, TaskState
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


def make_running_task():
    ts = now()
    return Task(
        id="task_running",
        title="Running mission",
        description="d",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
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
    assert primary.shape_id == "neko.scope_route"


def test_running_default_graph_neko_menu_does_not_offer_qa_release():
    task = make_running_task()
    ensure_default_mission_plan(task)

    actions = worker_actions_for_role("neko_supervisor", task, make_neko_run(), config=normal_flow_config())

    primary = next(action for action in actions if action.primary)
    assert primary.shape_id == "neko.scope_route"
    assert primary.label == "Release Stage"


def test_running_graph_with_qa_stage_neko_menu_can_offer_qa_release():
    task = make_running_task()
    task.stages.append(
        TaskStage(
            id="qa_release",
            title="QA Release",
            objective="Verify proof.",
            status=StageStatus.READY,
        )
    )

    actions = worker_actions_for_role("neko_supervisor", task, make_neko_run(), config=normal_flow_config())

    primary = next(action for action in actions if action.primary)
    assert primary.shape_id == "neko.scope_route"
