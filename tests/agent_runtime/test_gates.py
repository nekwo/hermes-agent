from hermes_time import now
from agent_runtime.gates import can_enter_dev_implementing
from agent_runtime.models import Task, TaskStage
from agent_runtime.plan_review import PlanReview, PlanReviewVerdict
from agent_runtime.states import TaskState, StageStatus


def make_task(review=False):
    ts=now(); t=Task(id="t", title="T", description="d", state=TaskState.RUNNING, created_at=ts, updated_at=ts, requested_by="tony", stages=[TaskStage(id="s1", title="S", objective="O", status=StageStatus.READY, acceptance_criteria=["ok"], test_plan=["pytest"])])
    if review:
        t.plan_review=PlanReview(id="r", task_id="t", reviewer_agent_id="qa", verdict=PlanReviewVerdict.APPROVED, reviewed_stage_ids=["s1"], proof_requirements_confirmed=True, test_plan_confirmed=True)
    return t


def test_dev_implementing_gate_blocks_before_qa_approval():
    result=can_enter_dev_implementing(make_task(False))
    assert not result.allowed
    assert "missing approved QA plan review" in result.missing


def test_dev_implementing_gate_allows_after_qa_approval():
    assert can_enter_dev_implementing(make_task(True)).allowed


def test_dev_implementing_gate_can_be_waived():
    t=make_task(False); t.waiver={"gate":"qa_plan_review", "actor":"tony"}
    assert can_enter_dev_implementing(t).allowed
