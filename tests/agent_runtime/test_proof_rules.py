from agent_runtime.proof_rules import ProofRequirement, ProofType


def test_proof_type_values_cover_stage_one_artifacts():
    assert ProofType.DIFF == "diff"
    assert ProofType.COMMIT == "commit"
    assert ProofType.TEST_RUN == "test_run"
    assert ProofType.SCREENSHOT == "screenshot"
    assert ProofType.VIDEO == "video"
    assert ProofType.LOG == "log"


def test_proof_requirement_identifies_missing_types():
    requirement = ProofRequirement(required_types=frozenset({ProofType.TEST_RUN, ProofType.SCREENSHOT}))

    assert requirement.missing_from([ProofType.TEST_RUN]) == frozenset({ProofType.SCREENSHOT})
    assert requirement.is_satisfied_by([ProofType.TEST_RUN, ProofType.SCREENSHOT])
