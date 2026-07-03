from __future__ import annotations

from hermes_time import now

from agent_runtime.models import Task
from agent_runtime.runtime_config import RuntimeConfig, SupervisionConfig, SwarmConfig
from agent_runtime.states import TaskState
from agent_runtime.store import RunStore, TaskStore
from agent_runtime.ticker import _runtime_budget_block


def _task(task_id: str = "task_budget_tree") -> Task:
    ts = now()
    return Task(id=task_id, title="Budget tree", description="d", state=TaskState.RUNNING, created_at=ts, updated_at=ts, requested_by="test")


def _config() -> RuntimeConfig:
    return RuntimeConfig(
        supervision=SupervisionConfig(hierarchical_budget_enabled=True),
        swarm=SwarmConfig(enabled=False, global_token_hard_limit=100, per_lane_token_limit=40),
        mission_max_total_tokens=1_000,
    )


def test_hierarchical_child_budget_blocks_upward_without_swarm_flag(isolate_agent_runtime_root):
    task = TaskStore().create(_task())
    runs = RunStore()
    prior = runs.open_run("dev", task.id)
    prior.llm = {"total_tokens": 40}
    runs.update(prior)
    runs.close_run(prior.id, state="completed", final_decision={"type": "hand_off"})

    block = _runtime_budget_block(task, persona_id="dev", run_store=runs, config=_config())

    assert block is not None
    assert block["event_type"] == "swarm_budget_exceeded"
    assert block["total_tokens"] == 40
    assert block["limit"] == 40


def test_hierarchical_global_pool_never_overspends(isolate_agent_runtime_root):
    task = TaskStore().create(_task())
    other = TaskStore().create(_task("task_other_budget_tree"))
    runs = RunStore()
    prior = runs.open_run("backend_dev", other.id)
    prior.llm = {"total_tokens": 101}
    runs.update(prior)
    runs.close_run(prior.id, state="completed", final_decision={"type": "hand_off"})

    block = _runtime_budget_block(task, persona_id="dev", run_store=runs, config=_config())

    assert block is not None
    assert block["event_type"] == "swarm_budget_exceeded"
    assert block["total_tokens"] == 101
    assert block["limit"] == 100
