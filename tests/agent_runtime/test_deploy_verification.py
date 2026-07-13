from hermes_time import now
from agent_runtime.events import EventLog
from agent_runtime.models import MissionIntent, MissionPlan, MissionPlanStage, Task
from agent_runtime.persona_assignments import PersonaAssignmentSpec, PersonaAssignmentStore
from agent_runtime.runtime_config import EnterpriseWorkerSessionsConfig, RuntimeConfig, SupervisionConfig
from agent_runtime.states import StageStatus, TaskState
from agent_runtime.store import RunStore, TaskStore
from agent_runtime.ticker import TickEngine


class RuntimeMustNotDeploy:
    def run_tick(self, persona, ctx, *, run):
        raise AssertionError("deploy verification should fail before model dispatch")


def _deploy_config() -> RuntimeConfig:
    return RuntimeConfig(
        enterprise_worker_sessions=EnterpriseWorkerSessionsConfig(
            enabled=True,
            worker_session_store=True,
            persona_assignment_store=True,
        ),
        supervision=SupervisionConfig(
            child_events_enabled=True,
            deploy_verification_enabled=True,
        ),
    )


def _dev_task() -> Task:
    return Task(
        id="task_deploy",
        title="Deploy verification",
        description="Deploy dev child.",
        state=TaskState.RUNNING,
        created_at=now(),
        updated_at=now(),
        requested_by="test",
        current_stage_id="implement",
        mission_plan=MissionPlan(
            blueprint_id="deploy_test",
            mission_intent=MissionIntent(title="Deploy", objective="Verify spawn."),
            current_stage_id="implement",
            stages=[
                MissionPlanStage(
                    id="implement",
                    title="Implement",
                    objective="Implement",
                    owner="dev",
                    repo="hermes-agent",
                    kind="implementation",
                    status=StageStatus.READY,
                )
            ],
        ),
    )


def _other_live_task() -> Task:
    return Task(
        id="task_other",
        title="Other",
        description="Existing live owner.",
        state=TaskState.RUNNING,
        created_at=now(),
        updated_at=now(),
        requested_by="test",
    )


def test_deploy_verification_surfaces_assignment_starvation_as_child_event(isolate_agent_runtime_root):
    TaskStore().create(_other_live_task())
    PersonaAssignmentStore().create_or_resume(
        PersonaAssignmentSpec(
            persona_id="dev",
            kind="task_stage",
            title="Other goal",
            message="Existing active assignment",
            task_id="task_other",
            goal_id="goal_other",
            stage_id="implement",
            state="assigned",
        )
    )
    tasks = TaskStore()
    task = _dev_task()
    tasks.create(task)

    result = TickEngine(task_store=tasks, persona_runtime=RuntimeMustNotDeploy(), config=_deploy_config()).tick_once(task_id=task.id)

    assert len(result.actions_taken) == 1
    assert result.actions_taken[0].ok is False
    assert "child deploy failed" in result.actions_taken[0].summary
    assert result.actions_taken[0].payload["assignment_id"]
    assert RunStore().list_for_task(task.id) == []
    events = [event for _offset, event in EventLog().iter_from_offset(0)]
    deploy_events = [event for event in events if event.type == "child.deploy_failed" and event.task_id == task.id]
    assert len(deploy_events) == 1
    payload = deploy_events[0].payload
    assert payload["reason"] == "dev already has an active assignment on another goal."
    assert payload["persona_id"] == "dev"
    assert payload["retryable"] is False


def test_deploy_verification_flag_off_preserves_existing_assignment_behavior(isolate_agent_runtime_root):
    TaskStore().create(_other_live_task())
    PersonaAssignmentStore().create_or_resume(
        PersonaAssignmentSpec(
            persona_id="dev",
            kind="task_stage",
            title="Other goal",
            message="Existing active assignment",
            task_id="task_other",
            goal_id="goal_other",
            stage_id="implement",
            state="assigned",
        )
    )
    tasks = TaskStore()
    task = _dev_task()
    tasks.create(task)
    config = _deploy_config()
    config.supervision.deploy_verification_enabled = False

    result = TickEngine(task_store=tasks, persona_runtime=RuntimeMustNotDeploy(), config=config).tick_once(task_id=task.id)

    assert len(result.actions_taken) == 1
    assert result.actions_taken[0].ok is False
    assert "deploy failed" not in result.actions_taken[0].summary
    assert RunStore().list_for_task(task.id)
