from __future__ import annotations

from hermes_time import now

from .errors import InvalidTransition
from .states import TaskState as T


TRANSITION_TABLE: dict[T, frozenset[T]] = {
    T.CREATED: frozenset({T.PM_TRIAGE, T.CANCELLED}),
    T.PM_TRIAGE: frozenset({T.READY_FOR_IMPLEMENTATION, T.BLOCKED, T.CANCELLED}),
    T.READY_FOR_IMPLEMENTATION: frozenset({T.DEV_AUDIT, T.BLOCKED, T.CANCELLED}),
    T.DEV_AUDIT: frozenset({T.DEV_STAGE_PLANNING, T.BLOCKED}),
    T.DEV_STAGE_PLANNING: frozenset({T.DEV_TEST_DESIGN, T.DEV_AUDIT, T.BLOCKED}),
    T.DEV_TEST_DESIGN: frozenset({T.QA_REVIEW_PLAN, T.DEV_STAGE_PLANNING, T.BLOCKED}),
    T.QA_REVIEW_PLAN: frozenset({T.DEV_IMPLEMENTING, T.DEV_STAGE_PLANNING, T.BLOCKED}),
    T.DEV_IMPLEMENTING: frozenset({T.READY_FOR_VERIFICATION, T.BLOCKED, T.FAILED}),
    T.READY_FOR_VERIFICATION: frozenset({T.QA_TESTING, T.DEV_IMPLEMENTING, T.BLOCKED}),
    T.QA_TESTING: frozenset({T.VERIFIED, T.NEEDS_FIXES, T.BLOCKED, T.FAILED}),
    T.NEEDS_FIXES: frozenset({T.DEV_IMPLEMENTING, T.DEV_STAGE_PLANNING, T.BLOCKED, T.CANCELLED}),
    T.VERIFIED: frozenset({T.PROOF_REVIEW, T.BLOCKED}),
    T.PROOF_REVIEW: frozenset({T.PM_READY_FOR_INTEGRATION, T.NEEDS_FIXES, T.BLOCKED}),
    T.PM_READY_FOR_INTEGRATION: frozenset({T.APPLYING, T.BLOCKED}),
    T.APPLYING: frozenset({T.DONE, T.FAILED, T.BLOCKED}),
    T.DONE: frozenset(),
    T.FAILED: frozenset({T.PM_TRIAGE}),
    T.CANCELLED: frozenset(),
    T.BLOCKED: frozenset(set(T) - {T.DONE}),
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
