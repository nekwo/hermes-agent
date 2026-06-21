from hermes_time import now
from agent_runtime.models import Incident, Proof, Task
from agent_runtime.proof_gates import implementation_proof_satisfied, verification_proof_satisfied, integration_proof_satisfied
from agent_runtime.proof_rules import ProofType
from agent_runtime.states import TaskState


def task(visual=False):
    ts=now(); return Task(id="t", title="T", description="d", state=TaskState.QA_TESTING, created_at=ts, updated_at=ts, requested_by="tony", requires_visual_proof=visual)

def proof(pt, **meta):
    return Proof(id=f"p{pt}", task_id="t", stage_id=None, type=pt, title=str(pt), path_or_value=meta.pop("path", "x"), created_by="h", created_at=now(), metadata=meta, redaction_status="safe")


def test_visual_task_requires_screenshot_or_video_and_tests():
    proofs=[proof(ProofType.TEST_RUN, exit_code=0), proof(ProofType.QA_VERDICT, verdict="approved")]
    r=verification_proof_satisfied(task(True), proofs)
    assert not r.allowed and "missing screenshot or video proof" in r.missing


def test_non_visual_task_passes_with_test_and_verdict():
    proofs=[proof(ProofType.TEST_RUN, exit_code=0), proof(ProofType.QA_VERDICT, verdict="approved")]
    assert verification_proof_satisfied(task(False), proofs).allowed



def test_null_exit_code_test_proof_is_treated_as_not_passed_not_crash():
    proofs=[proof(ProofType.TEST_RUN, exit_code=None), proof(ProofType.QA_VERDICT, verdict="approved")]

    result = verification_proof_satisfied(task(False), proofs)

    assert not result.allowed
    assert "missing passed test proof" in result.missing



def test_invalid_exit_code_test_proofs_are_not_passing_and_do_not_crash():
    for raw_exit_code in (None, "not-an-int", False, True, float("inf"), object()):
        proofs=[proof(ProofType.TEST_RUN, exit_code=raw_exit_code), proof(ProofType.QA_VERDICT, verdict="approved")]

        qa_result = verification_proof_satisfied(task(False), proofs)
        dev_result = implementation_proof_satisfied(task(False), proofs)

        assert not qa_result.allowed
        assert "missing passed test proof" in qa_result.missing
        assert not dev_result.allowed
        assert "missing passed test proof" in dev_result.missing



def test_pm_integration_blocks_on_open_incident():
    proofs=[proof(ProofType.TEST_RUN, exit_code=0), proof(ProofType.QA_VERDICT, verdict="approved"), proof(ProofType.COMMIT)]
    inc=Incident(id="i", task_id="t", run_id=None, kind="critical", summary="bad", detail_path=None, opened_at=now())
    assert not integration_proof_satisfied(task(False), proofs, [inc]).allowed
