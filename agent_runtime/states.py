from enum import StrEnum


class TaskState(StrEnum):
    CREATED = "created"
    PM_TRIAGE = "pm_triage"
    PM_READY_FOR_DEV = "pm_ready_for_dev"
    DEV_AUDIT = "dev_audit"
    DEV_STAGE_PLANNING = "dev_stage_planning"
    DEV_TEST_DESIGN = "dev_test_design"
    QA_REVIEW_PLAN = "qa_review_plan"
    DEV_IMPLEMENTING = "dev_implementing"
    DEV_READY_FOR_QA = "dev_ready_for_qa"
    QA_TESTING = "qa_testing"
    QA_NEEDS_FIXES = "qa_needs_fixes"
    QA_APPROVED = "qa_approved"
    PM_PROOF_REVIEW = "pm_proof_review"
    PM_READY_FOR_INTEGRATION = "pm_ready_for_integration"
    INTEGRATING = "integrating"
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentState(StrEnum):
    IDLE = "idle"
    ASSIGNED = "assigned"
    READING_CONTEXT = "reading_context"
    AUDITING = "auditing"
    PLANNING = "planning"
    DESIGNING_TESTS = "designing_tests"
    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"
    TESTING = "testing"
    CAPTURING_PROOF = "capturing_proof"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    WAITING_FOR_FIXES = "waiting_for_fixes"
    BLOCKED = "blocked"
    CRASHED = "crashed"
    COMPLETE = "complete"


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


class PossessionState(StrEnum):
    AVAILABLE = "available"
    REQUESTED = "requested"
    POSSESSED = "possessed"
    RELEASE_PENDING = "release_pending"
    DISABLED = "disabled"


class StageStatus(StrEnum):
    DRAFT = "draft"
    AUDITED = "audited"
    READY = "ready"
    IMPLEMENTING = "implementing"
    READY_FOR_QA = "ready_for_qa"
    PASSED = "passed"
    NEEDS_FIXES = "needs_fixes"
    BLOCKED = "blocked"
