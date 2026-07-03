import threading
import time

import pytest

from hermes_time import now
from agent_runtime.actions import HarnessActionType
from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.locks import HarnessLockUnavailable, _file_lock, repo_land_lock
from agent_runtime.models import Incident, MissionIntent, MissionPlan, MissionPlanStage, Task
from agent_runtime.runtime_config import RuntimeConfig, SwarmConfig
from agent_runtime.states import StageStatus, TaskState
from agent_runtime.store import IncidentStore, RunStore, TaskStore
from agent_runtime.ticker import TickEngine


class BarrierRuntime:
    def __init__(self, parties: int):
        self.barrier = threading.Barrier(parties, timeout=5)
        self.windows = []
        self.lock = threading.Lock()

    def run_tick(self, persona, ctx, *, run):
        started = time.monotonic()
        with self.lock:
            self.windows.append({"stage_id": run.stage_id, "started": started, "finished": None})
            index = len(self.windows) - 1
        self.barrier.wait()
        time.sleep(0.1)
        finished = time.monotonic()
        with self.lock:
            self.windows[index]["finished"] = finished
        return AgentDecision(
            type=DecisionType.BLOCK,
            summary=f"{run.stage_id} blocked after overlap proof",
            rationale="test overlap",
            payload={"reason": "test overlap complete", "log_ref": {"path": "events.jsonl", "line": 1, "summary": "overlap proof"}},
        )


def test_ready_independent_blueprint_stages_run_as_overlapping_lanes(isolate_agent_runtime_root):
    task = Task(
        id="task_parallel",
        title="Parallel mission",
        description="Two independent lanes after scope.",
        state=TaskState.RUNNING,
        created_at=now(),
        updated_at=now(),
        requested_by="test",
        current_stage_id="frontend_a",
        mission_plan=MissionPlan(
            blueprint_id="parallel_test",
            mission_intent=MissionIntent(title="Parallel", objective="Prove lane overlap."),
            current_stage_id="frontend_a",
            stages=[
                MissionPlanStage(id="scope", title="Scope", objective="Done", owner="neko_supervisor", repo="hermes-agent", kind="scope", status=StageStatus.PASSED),
                MissionPlanStage(id="frontend_a", title="Frontend A", objective="A", owner="dev", repo="hermes-agent", kind="implementation", depends_on=["scope"], status=StageStatus.READY),
                MissionPlanStage(id="frontend_b", title="Frontend B", objective="B", owner="qa", repo="hermes-agent", kind="implementation", depends_on=["scope"], status=StageStatus.READY),
                MissionPlanStage(id="join", title="Join", objective="Join", owner="neko_supervisor", repo="hermes-agent", kind="join", depends_on=["frontend_a", "frontend_b"], status=StageStatus.READY),
            ],
        ),
    )
    tasks = TaskStore()
    tasks.create(task)
    runtime = BarrierRuntime(parties=2)
    cfg = RuntimeConfig(
        max_actions_per_tick=2,
        swarm=SwarmConfig(enabled=True, requires_certification=False, max_active_lanes=2),
    )

    result = TickEngine(task_store=tasks, persona_runtime=runtime, config=cfg).tick_once(task_id=task.id)

    assert len(result.actions_taken) == 2
    assert {item.action.stage_id for item in result.actions_taken} == {"frontend_a", "frontend_b"}
    assert all(item.action.type == HarnessActionType.RUN_SLOT for item in result.actions_taken)
    assert {item["stage_id"] for item in runtime.windows} == {"frontend_a", "frontend_b"}
    first, second = sorted(runtime.windows, key=lambda item: item["started"])
    assert first["finished"] is not None and second["finished"] is not None
    assert second["started"] < first["finished"]
    runs = RunStore().list_for_task(task.id)
    assert {run.stage_id for run in runs} >= {"frontend_a", "frontend_b"}


def test_dependent_blueprint_stage_waits_for_upstream_lane(isolate_agent_runtime_root):
    task = Task(
        id="task_serial_dep",
        title="Serial dependency",
        description="Dependency should not dispatch with upstream.",
        state=TaskState.RUNNING,
        created_at=now(),
        updated_at=now(),
        requested_by="test",
        current_stage_id="frontend_a",
        mission_plan=MissionPlan(
            blueprint_id="parallel_test",
            mission_intent=MissionIntent(title="Serial", objective="Prove dependency gating."),
            current_stage_id="frontend_a",
            stages=[
                MissionPlanStage(id="frontend_a", title="Frontend A", objective="A", owner="dev", repo="hermes-agent", kind="implementation", status=StageStatus.READY),
                MissionPlanStage(id="join", title="Join", objective="Join", owner="qa", repo="hermes-agent", kind="join", depends_on=["frontend_a"], status=StageStatus.READY),
            ],
        ),
    )
    TaskStore().create(task)
    cfg = RuntimeConfig(
        max_actions_per_tick=2,
        swarm=SwarmConfig(enabled=True, requires_certification=False, max_active_lanes=2),
    )

    actions = TickEngine(config=cfg).state_machine.next_actions(task)

    assert [action.stage_id for action in actions] == ["frontend_a"]


def test_file_lock_retries_then_times_out(isolate_agent_runtime_root):
    lock_path = isolate_agent_runtime_root / "locks" / "retry.lock"
    acquired = threading.Event()
    release = threading.Event()

    def holder():
        with _file_lock(lock_path, timeout_seconds=0.5):
            acquired.set()
            release.wait(2)

    thread = threading.Thread(target=holder)
    thread.start()
    assert acquired.wait(2)
    try:
        started = time.monotonic()
        with pytest.raises(HarnessLockUnavailable):
            with _file_lock(lock_path, timeout_seconds=0.1):
                pass
        assert time.monotonic() - started >= 0.09
    finally:
        release.set()
        thread.join(timeout=2)


def test_repo_land_lock_serializes_shared_source_root(isolate_agent_runtime_root, tmp_path):
    source_root = tmp_path / "repo"
    source_root.mkdir()
    order = []

    def worker(name):
        with repo_land_lock(source_root):
            order.append(f"{name}:enter")
            time.sleep(0.05)
            order.append(f"{name}:exit")

    first = threading.Thread(target=worker, args=("a",))
    second = threading.Thread(target=worker, args=("b",))
    first.start()
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)

    assert order in (["a:enter", "a:exit", "b:enter", "b:exit"], ["b:enter", "b:exit", "a:enter", "a:exit"])


def test_incident_close_is_serialized_under_contention(isolate_agent_runtime_root):
    task = Task(
        id="task_incident_close",
        title="Incident",
        description="d",
        state=TaskState.BLOCKED,
        created_at=now(),
        updated_at=now(),
        requested_by="test",
        open_incident_ids=["inc_close"],
    )
    TaskStore().create(task)
    incidents = IncidentStore()
    incidents.open(
        Incident(
            id="inc_close",
            task_id=task.id,
            run_id=None,
            kind="blocked",
            summary="blocked",
            detail_path=None,
            opened_at=now(),
        )
    )

    threads = [threading.Thread(target=incidents.close, args=("inc_close",), kwargs={"reason": f"close {idx}"}) for idx in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    saved = TaskStore().get(task.id)
    assert saved.open_incident_ids == []
    assert saved.state == TaskState.RUNNING
    assert incidents.get("inc_close").closed_at is not None
