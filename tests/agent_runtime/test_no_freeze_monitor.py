from __future__ import annotations

from datetime import timedelta

from hermes_time import now

from agent_runtime.no_freeze_monitor import classify_freezes, record_freeze_findings
from agent_runtime.store import IncidentStore, ProofStore


def test_no_freeze_monitor_classifies_stale_run_and_repeated_actions():
    ref = now()
    snapshot = {
        "observability": {
            "active_runs": [
                {
                    "run_id": "run_1",
                    "task_id": "task_1",
                    "last_heartbeat_at": (ref - timedelta(seconds=180)).isoformat(),
                }
            ]
        }
    }
    history = [
        {"task_id": "task_1", "next_action": "run_dev", "persona_stage": "dev:stage_1"},
        {"task_id": "task_1", "next_action": "run_dev", "persona_stage": "dev:stage_1"},
        {"task_id": "task_1", "next_action": "run_dev", "persona_stage": "dev:stage_1"},
    ]

    findings = classify_freezes(snapshot=snapshot, history=history, reference_time=ref)

    assert {finding["kind"] for finding in findings} >= {
        "run_stalled",
        "same_next_action_repeated",
        "same_stage_retry_without_signal_change",
    }


def test_no_freeze_monitor_classifies_autonomy_and_tool_budget_gaps():
    snapshot = {
        "observability": {
            "active_runs": [
                {
                    "run_id": "run_missing_auto",
                    "task_id": "task_1",
                    "persona_id": "dev",
                    "last_heartbeat_at": now().isoformat(),
                    "progress": {"status": "running"},
                },
                {
                    "run_id": "run_budget",
                    "task_id": "task_1",
                    "persona_id": "backend_dev",
                    "last_heartbeat_at": now().isoformat(),
                    "progress": {
                        "autonomy_packet_id": "auto_run_budget_1",
                        "context_receipt_id": "ctxr_run_budget_1",
                        "read_search_count": 4,
                        "read_search_limit": 4,
                        "loop_warning": "read_search_without_patch_threshold",
                    },
                },
                {
                    "run_id": "run_resume",
                    "task_id": "task_1",
                    "persona_id": "qa",
                    "session_id": "session_safe",
                    "last_heartbeat_at": now().isoformat(),
                    "progress": {"autonomy_packet_id": "auto_run_resume_1"},
                },
            ]
        }
    }

    findings = classify_freezes(snapshot=snapshot)

    kinds_by_run = {(finding["kind"], finding["run_id"]) for finding in findings}
    assert ("autonomy_packet_missing", "run_missing_auto") in kinds_by_run
    assert ("tool_budget_exceeded_without_new_signal", "run_budget") in kinds_by_run
    assert ("context_resume_without_receipt", "run_resume") in kinds_by_run


def test_no_freeze_monitor_counts_only_open_invalid_output_incidents():
    snapshot = {
        "incidents": [
            {"kind": "model_invalid_output", "is_open": False, "closed_at": now().isoformat()},
            {"kind": "model_invalid_output", "is_open": False, "closed_at": now().isoformat()},
            {"kind": "model_invalid_output", "is_open": True, "closed_at": None},
        ]
    }

    findings = classify_freezes(snapshot=snapshot)

    assert "persona_invalid_output_loop" not in {finding["kind"] for finding in findings}


def test_no_freeze_monitor_records_redaction_safe_proof_and_incident():
    proof_store = ProofStore()
    incident_store = IncidentStore()
    findings = [
        {
            "kind": "run_stalled",
            "severity": "high",
            "task_id": "task_1",
            "run_id": "run_1",
            "summary": "Active run heartbeat is stale",
        }
    ]

    result = record_freeze_findings(task_id="task_1", findings=findings, proof_store=proof_store, incident_store=incident_store)

    assert result["finding_count"] == 1
    proof = proof_store.get(result["proof_ids"][0])
    assert proof.redaction_status == "safe"
    assert proof.metadata["finding"]["summary"] == "Active run heartbeat is stale"
    incident = incident_store.get(result["incident_ids"][0])
    assert incident.kind == "runtime_freeze"
    assert incident.metadata["proof_id"] == proof.id
