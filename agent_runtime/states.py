from enum import StrEnum


class TaskState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str) and value in _LEGACY_RUNNING_TASK_STATE_VALUES:
            return cls.RUNNING
        return None


class RunState(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    WAITING_ON_TOOL = "waiting_on_tool"
    WAITING_ON_APPROVAL = "waiting_on_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    STALE = "stale"
    CANCELLED = "cancelled"


class WorkerSessionState(StrEnum):
    IDLE = "idle"
    ASSIGNED = "assigned"
    RUNNING = "running"
    WAITING_ON_TOOL = "waiting_on_tool"
    WAITING_ON_PROOF = "waiting_on_proof"
    SELF_HEALING = "self_healing"
    WAITING_ON_HUMAN = "waiting_on_human"
    POSSESSED = "possessed"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CLOSED = "closed"


_LEGACY_RUNNING_TASK_STATE_VALUES = frozenset(
    {
        "pm_triage",
        "pm_ready_for_dev",
        "dev_audit",
        "dev_stage_planning",
        "dev_test_design",
        "qa_review_plan",
        "dev_implementing",
        "dev_ready_for_qa",
        "qa_testing",
        "qa_needs_fixes",
        "qa_approved",
        "pm_proof_review",
        "pm_ready_for_integration",
        "integrating",
    }
)
