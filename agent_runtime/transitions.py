from __future__ import annotations

from hermes_time import now

from .errors import InvalidTransition
from .states import TaskState as T


TRANSITION_TABLE: dict[T, frozenset[T]] = {
    T.CREATED: frozenset({T.RUNNING, T.BLOCKED, T.CANCELLED}),
    T.RUNNING: frozenset({T.BLOCKED, T.DONE, T.FAILED, T.CANCELLED}),
    T.BLOCKED: frozenset({T.RUNNING, T.BLOCKED, T.FAILED, T.CANCELLED}),
    T.FAILED: frozenset({T.CANCELLED}),
    T.DONE: frozenset(),
    T.CANCELLED: frozenset(),
}


def apply_transition(task, to_state: T | str, *, actor: str, reason: str = "") -> None:
    target = to_state if isinstance(to_state, T) else T(to_state)
    current = task.state if isinstance(task.state, T) else T(task.state)
    if target not in TRANSITION_TABLE[current]:
        raise InvalidTransition(
            f"{task.id}: cannot transition {current} -> {target} "
            f"(actor={actor}, reason={reason!r})"
        )
    task.state = target
    task.updated_at = now()
