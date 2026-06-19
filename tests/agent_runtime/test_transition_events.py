from __future__ import annotations

from hermes_time import now

from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.events import EventLog
from agent_runtime.models import Task
from agent_runtime.states import TaskState
from agent_runtime.store import TaskStore
from agent_runtime.ticker import TickEngine


class PMRuntime:
    def run_tick(self, persona, ctx, *, run):
        return AgentDecision(
            type=DecisionType.PROPOSE_ACCEPTANCE,
            summary="scope",
            rationale="r",
            payload={"objective": "obj", "acceptance_criteria": ["done"]},
        )


def test_pm_decision_emits_exactly_one_transition_event():
    ts = now()
    task = Task(id="task_transition", title="T", description="d", state=TaskState.CREATED, created_at=ts, updated_at=ts, requested_by="tony")
    store = TaskStore()
    store.create(task)

    TickEngine(task_store=store, persona_runtime=PMRuntime()).tick_once()

    transitions = [event for event in EventLog().tail(20) if event.type == "task.transition" and event.task_id == task.id]
    assert len(transitions) == 1
    assert transitions[0].payload["from"] == "created"
    assert transitions[0].payload["to"] == "pm_ready_for_dev"
