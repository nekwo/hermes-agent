import json

from agent_runtime import paths
from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.events import EventLog
from agent_runtime.packets import latest_packet, validate_decision_packets


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


def test_latest_packet_reads_historical_packet_recorded_rows(
    isolate_agent_runtime_root,
):
    row = {
        "ts": "2026-07-30T12:00:00+00:00",
        "type": "packet.recorded",
        "task_id": "task_packet",
        "run_id": "run_packet",
        "persona_id": "dev",
        "payload": {
            "packet_id": "packet_delivery_historical",
            "packet_type": "delivery",
            "stage_id": "stage_1",
            "body": {"work_status": "ready_for_qa"},
        },
    }
    paths.events_path().parent.mkdir(parents=True, exist_ok=True)
    paths.events_path().write_text(
        json.dumps(row, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    recorded = latest_packet("task_packet", "delivery", event_log=EventLog())

    assert recorded is not None
    assert recorded["packet_id"] == "packet_delivery_historical"
    assert recorded["body"]["work_status"] == "ready_for_qa"
