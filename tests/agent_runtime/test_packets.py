from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.packets import validate_decision_packets


def test_delivery_handoff_repair_fields_survive_validation():
    delivery = {
        "work_status": "ready_for_qa",
        "changed_paths": ["agent_runtime/packets.py"],
        "inspected_paths": [
            "agent_runtime/packets.py",
            "agent_runtime/context_builder.py",
        ],
        "dirty_baseline": {"present": True, "preserved": ["docs/operator-note.md"]},
        "coverage_claims": ["proof_passed covers packet normalization"],
        "known_non_coverage": ["visual proof not required"],
        "proof_reuse_basis": {
            "proof_ids": ["proof_passed"],
            "basis": "metadata-only handoff repair",
        },
        "failed_proof_classification": ["shell_wrapper_error:proof_failed"],
        "handoff_repair": {"metadata_only": True, "product_reedit": False},
        "proof_ids": ["proof_passed"],
        "unknown_sidecar": {"raw": "retired emitter no longer persists this"},
    }
    decision = AgentDecision(
        type=DecisionType.REQUEST_QA_REVIEW,
        summary="handoff repair",
        rationale="QA needs preserved delivery metadata.",
        payload={
            "stage_id": "stage_1",
            "proof_ids": ["proof_passed"],
            "handoff": {"to": "qa", "stage_complete": True},
            "delivery": delivery,
        },
    )

    validate_decision_packets(decision)

    body = decision.payload["delivery"]
    assert body["changed_paths"] == ["agent_runtime/packets.py"]
    assert body["dirty_baseline"]["present"] is True
    assert body["proof_reuse_basis"]["basis"] == "metadata-only handoff repair"
    assert body["handoff_repair"]["metadata_only"] is True
    assert "unknown_sidecar" not in body
    assert "unknown_sidecar" in body["_normalization"]["dropped_fields"]
