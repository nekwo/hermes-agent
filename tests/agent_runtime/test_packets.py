from hermes_time import now

from agent_runtime.context_builder import build_context, render_context
from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.events import EventLog
from agent_runtime.models import AgentRun
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.packets import make_packet, record_packet, validate_decision_packets
from agent_runtime.states import RunState, TaskState


def _task() -> Task:
    ts = now()
    return Task(
        id="task_packet",
        title="Packet contract",
        description="Verify handoff repair fields survive normalization.",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="test",
        acceptance_criteria=["QA can review handoff repair metadata"],
        current_stage_id="stage_1",
    )


def _run() -> AgentRun:
    ts = now()
    return AgentRun(
        id="run_packet",
        persona_id="dev",
        task_id="task_packet",
        stage_id="stage_1",
        state=RunState.RUNNING,
        started_at=ts,
        last_heartbeat_at=ts,
    )


def test_delivery_handoff_repair_fields_survive_packet_projection(isolate_agent_runtime_root):
    task = _task()
    run = _run()
    delivery = {
        "work_status": "ready_for_qa",
        "changed_paths": ["agent_runtime/packets.py"],
        "inspected_paths": ["agent_runtime/packets.py", "agent_runtime/context_builder.py"],
        "dirty_baseline": {"present": True, "preserved": ["docs/operator-note.md"]},
        "coverage_claims": ["proof_passed covers packet normalization"],
        "known_non_coverage": ["visual proof not required"],
        "proof_reuse_basis": {"proof_ids": ["proof_passed"], "basis": "metadata-only handoff repair"},
        "failed_proof_classification": ["shell_wrapper_error:proof_failed"],
        "handoff_repair": {"metadata_only": True, "product_reedit": False},
        "proof_ids": ["proof_passed"],
        "unknown_sidecar": {"raw": "preserve in artifact only"},
    }
    decision = AgentDecision(
        type=DecisionType.REQUEST_QA_REVIEW,
        summary="handoff repair",
        rationale="QA needs preserved delivery metadata.",
        payload={"stage_id": "stage_1", "proof_ids": ["proof_passed"], "handoff": {"to": "qa", "stage_complete": True}, "delivery": delivery},
    )

    validate_decision_packets(decision)
    packet = make_packet(task=task, decision=decision, packet_type="delivery", body=decision.payload["delivery"], actor="dev", run_id=run.id, stage_id=run.stage_id)
    log = EventLog()
    record_packet(packet, event_log=log)
    ctx = build_context(task, run, event_log=log)
    rendered = render_context(ctx)

    body = ctx.latest_delivery["body"]
    assert body["changed_paths"] == ["agent_runtime/packets.py"]
    assert body["inspected_paths"] == ["agent_runtime/packets.py", "agent_runtime/context_builder.py"]
    assert body["dirty_baseline"]["present"] is True
    assert body["coverage_claims"] == ["proof_passed covers packet normalization"]
    assert body["known_non_coverage"] == ["visual proof not required"]
    assert body["proof_reuse_basis"]["basis"] == "metadata-only handoff repair"
    assert body["failed_proof_classification"] == ["shell_wrapper_error:proof_failed"]
    assert body["handoff_repair"]["metadata_only"] is True
    assert "unknown_sidecar" not in body
    assert "unknown_sidecar" in ctx.latest_delivery["dropped_fields"]
    assert "proof_reuse_basis" in rendered
    assert "known_non_coverage" in rendered


def test_delivery_packet_carries_harness_cited_evidence_ids(isolate_agent_runtime_root):
    task = _task()
    task.harness_self_heal["delivery_no_progress_guard"] = {
        "stage_1": {
            "cited_evidence_ids": [
                "proof_observed",
                "delivery_capture:bundle_empty:worktree_missing_or_clean",
            ],
        }
    }
    run = _run()
    decision = AgentDecision(
        type=DecisionType.REQUEST_QA_REVIEW,
        summary="handoff repair",
        rationale="QA needs preserved delivery metadata.",
        payload={
            "stage_id": "stage_1",
            "proof_ids": ["proof_passed"],
            "handoff": {"to": "qa", "stage_complete": True},
            "delivery": {"proof_ids": ["proof_passed"], "summary": "Ready for QA."},
        },
    )

    validate_decision_packets(decision)
    packet = make_packet(
        task=task,
        decision=decision,
        packet_type="delivery",
        body=decision.payload["delivery"],
        actor="dev",
        run_id=run.id,
        stage_id=run.stage_id,
    )

    assert packet.body["cited_evidence_ids"] == [
        "proof_passed",
        "proof_observed",
        "delivery_capture:bundle_empty:worktree_missing_or_clean",
    ]
