from hermes_time import now

import pytest

from agent_runtime.decision_schema import AgentDecision, DecisionPayloadInvalid, DecisionType
from agent_runtime.events import EventLog
from agent_runtime.models import AgentRun, Task, TaskStage
from agent_runtime.packets import make_packet
from agent_runtime.self_test_evidence import SelfTestEvidenceStore, record_self_test_from_progress
from agent_runtime.states import RunState, StageStatus, TaskState
from agent_runtime.store import ProofStore


def _run() -> AgentRun:
    ts = now()
    return AgentRun(
        id="run_selftest",
        persona_id="dev",
        task_id="task_selftest",
        stage_id="stage_1",
        state=RunState.RUNNING,
        started_at=ts,
        last_heartbeat_at=ts,
    )


def test_records_redaction_safe_self_test_evidence_from_terminal_progress():
    run = _run()

    evidence = record_self_test_from_progress(
        run,
        "run.tool.finished",
        {
            "tool_name": "terminal",
            "command": "flutter test test/features/mission_control/mission_control_page_test.dart",
            "exit_code": 0,
            "stdout": "00:01 +1: all tests passed",
            "stderr": "",
            "duration_ms": 1234,
            "repo_label": "EterniaLauncher",
            "workdir_label": "Launcher",
        },
    )

    assert evidence is not None
    assert evidence.status == "passed"
    assert evidence.satisfies_release_gate is False
    assert evidence.stdout_path is not None
    saved = SelfTestEvidenceStore().get(evidence.evidence_id)
    assert saved.command_label == "flutter test test/features/mission_control/mission_control_page_test.dart"
    assert saved.redaction_status == "safe"
    events = EventLog().for_task(run.task_id, limit=0)
    event = next(item for item in events if item.type == "self_test.recorded")
    assert event.payload["evidence_id"] == evidence.evidence_id
    proof = ProofStore().get(f"proof_observed_{evidence.evidence_id.removeprefix('selftest_')}")
    assert proof.metadata["source"] == "agent_tool_trace"
    assert proof.metadata["authoritative"] is False
    assert proof.metadata["exit_code"] == 0
    assert proof.path_or_value.endswith(f"{evidence.evidence_id}.json")


def test_preflight_commands_do_not_become_self_test_evidence():
    assert (
        record_self_test_from_progress(
            _run(),
            "run.tool.finished",
            {"tool_name": "terminal", "command": "flutter --version", "exit_code": 0},
        )
        is None
    )
    assert SelfTestEvidenceStore().list_for_task("task_selftest") == []


def test_repeated_failed_self_test_emits_loop_detection_event():
    run = _run()
    payload = {"tool_name": "terminal", "command": "pytest tests/agent_runtime/test_context_builder.py -q", "exit_code": 1}

    first = record_self_test_from_progress(run, "run.tool.finished", payload)
    second = record_self_test_from_progress(run, "run.tool.finished", payload)

    assert first is not None
    assert second is not None
    events = EventLog().for_task(run.task_id, limit=0)
    assert [event.type for event in events].count("self_test.recorded") == 2
    assert [event.type for event in events].count("proof.attached") == 2
    loop_event = next(item for item in events if item.type == "self_test.loop_detected")
    assert loop_event.payload["repeat_count"] == 2


def test_delivery_packet_rejects_unknown_self_test_evidence_ids():
    run = _run()
    evidence = record_self_test_from_progress(
        run,
        "run.tool.finished",
        {"tool_name": "terminal", "command": "pytest tests/agent_runtime/test_self_test_evidence.py -q", "exit_code": 0},
    )
    assert evidence is not None
    task = Task(
        id=run.task_id,
        title="Mission Control test",
        description="d",
        state=TaskState.RUNNING,
        created_at=now(),
        updated_at=now(),
        requested_by="tony",
        current_stage_id="stage_1",
        stages=[TaskStage(id="stage_1", title="Stage", objective="Do it", status=StageStatus.IMPLEMENTING)],
    )
    decision = AgentDecision(
        type=DecisionType.PROPOSE_PATCH,
        summary="delivery",
        rationale="self-test passed",
        payload={},
    )

    packet = make_packet(
        task=task,
        decision=decision,
        packet_type="delivery",
        body={"work_status": "patch_proposed", "self_test_evidence_ids": [evidence.evidence_id]},
        actor="dev",
        run_id=run.id,
        stage_id="stage_1",
    )

    assert packet.body["self_test_evidence_ids"] == [evidence.evidence_id]
    with pytest.raises(DecisionPayloadInvalid, match="unknown evidence id"):
        make_packet(
            task=task,
            decision=decision,
            packet_type="delivery",
            body={"work_status": "patch_proposed", "self_test_evidence_ids": ["selftest_missing"]},
            actor="dev",
            run_id=run.id,
            stage_id="stage_1",
        )
