import pytest

from hermes_time import now

from agent_runtime.errors import InvalidTransition
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.states import TaskState
from agent_runtime.transitions import apply_transition


def make_task(state=TaskState.CREATED):
    ts = now()
    return Task(
        id="task_abc",
        title="Task",
        description="desc",
        state=state,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
    )


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        (TaskState.CREATED, TaskState.RUNNING),
        (TaskState.RUNNING, TaskState.BLOCKED),
        (TaskState.RUNNING, TaskState.DONE),
        (TaskState.RUNNING, TaskState.FAILED),
        (TaskState.BLOCKED, TaskState.RUNNING),
    ],
)
def test_allowed_transitions_update_state_and_timestamp(from_state, to_state):
    task = make_task(from_state)
    before = task.updated_at

    apply_transition(task, to_state, actor="test", reason="unit")

    assert task.state == to_state
    assert task.updated_at >= before


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        (TaskState.RUNNING, TaskState.RUNNING),
        (TaskState.DONE, TaskState.RUNNING),
        (TaskState.CANCELLED, TaskState.RUNNING),
        (TaskState.FAILED, TaskState.RUNNING),
        (TaskState.BLOCKED, TaskState.DONE),
    ],
)
def test_invalid_transitions_raise_and_do_not_mutate(from_state, to_state):
    task = make_task(from_state)
    before_state = task.state
    before_updated_at = task.updated_at

    with pytest.raises(InvalidTransition):
        apply_transition(task, to_state, actor="test", reason="bad")

    assert task.state == before_state
    assert task.updated_at == before_updated_at
