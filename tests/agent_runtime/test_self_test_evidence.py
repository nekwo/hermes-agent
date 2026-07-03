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


def test_run_progress_sink_captures_observed_proof_independent_of_config(monkeypatch):
    """R1 regression: the observed lane must populate from a terminal
    ``run.tool.finished`` whose command lives only in ``command_full`` (the live
    runner shape), and it must NOT depend on a re-loaded RuntimeConfig in the
    run-executing process. A config-gated capture resolved the contract as
    disabled in the run process and silently dropped every observed proof even
    with the contract enabled at the file/ticker level."""

    from agent_runtime import config as _cfg
    from agent_runtime.progress import RunProgressSink
    from agent_runtime.runtime_config import (
        NormalWorkerFlowConfig,
        RuntimeConfig,
        SimplifiedAgentContractConfig,
    )
    from agent_runtime.store import RunStore

    disabled = RuntimeConfig(
        simplified_agent_contract=SimplifiedAgentContractConfig(enabled=False),
        normal_worker_flow=NormalWorkerFlowConfig(enabled=False),
    )
    monkeypatch.setattr(_cfg, "load_agent_runtime_config", lambda *a, **k: disabled, raising=False)

    runs = RunStore()
    run = runs.open_run("dev", "task_obs_gate", stage_id="implement")
    RunProgressSink(run_store=runs, run_id=run.id).emit(
        "run.tool.finished",
        {
            "type": "run.tool.finished",
            "tool_name": "terminal",
            "status": "passed",
            "command_full": "python manage.py check",
            "summary": "Finished tool terminal: passed",
        },
    )

    observed = [
        p
        for p in ProofStore().list_for_task("task_obs_gate")
        if (p.metadata or {}).get("source") == "agent_tool_trace"
    ]
    assert len(observed) == 1
    assert observed[0].stage_id == "implement"


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


def test_shell_command_alias_records_observed_self_test_evidence():
    run = _run()

    evidence = record_self_test_from_progress(
        run,
        "run.tool.finished",
        {
            "tool_name": "shell_command",
            "command_label": "python -m pytest tests/agent_runtime/test_snapshot.py -q",
            "exit_code": 0,
            "duration_ms": 222,
        },
    )

    assert evidence is not None
    proof = ProofStore().get(
        f"proof_observed_{evidence.evidence_id.removeprefix('selftest_')}"
    )
    assert proof.metadata["source"] == "agent_tool_trace"
    assert proof.metadata["run_id"] == run.id


def test_command_full_can_drive_self_test_classification():
    run = _run()

    evidence = record_self_test_from_progress(
        run,
        "run.tool.finished",
        {
            "tool_name": "powershell",
            "command_full": "flutter analyze lib/features/mission_control/mission_control_page.dart",
            "exit_code": 0,
        },
    )

    assert evidence is not None
    assert evidence.command_label.startswith("flutter analyze")


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


def _delivery_task() -> Task:
    return Task(
        id="task_selftest",
        title="Mission Control test",
        description="d",
        state=TaskState.RUNNING,
        created_at=now(),
        updated_at=now(),
        requested_by="tony",
        current_stage_id="stage_1",
        stages=[TaskStage(id="stage_1", title="Stage", objective="Do it", status=StageStatus.IMPLEMENTING)],
    )


def _attach_command_proof(*, task_id: str, stage_id: str | None, proof_id: str) -> str:
    from agent_runtime.models import Proof
    from agent_runtime.proof_rules import ProofType

    ProofStore().attach(
        Proof(
            id=proof_id,
            task_id=task_id,
            stage_id=stage_id,
            type=ProofType.TEST_RUN,
            title="Command proof: flutter analyze",
            path_or_value="artifacts/x.log",
            created_by="dev",
            created_at=now(),
            metadata={"status": "passed", "exit_code": 0, "run_id": "run_prior"},
        )
    )
    return proof_id


def test_delivery_packet_accepts_authoritative_command_proof_id(isolate_agent_runtime_root):
    # A dev routinely cites the harness command-proof id (ProofStore) rather than
    # a SelfTestEvidence id. A real proof for this task/stage must be accepted so a
    # re-run of a stage that already produced a passing proof does not loop on
    # `unknown evidence id`.
    task = _delivery_task()
    proof_id = _attach_command_proof(task_id=task.id, stage_id="stage_1", proof_id="test_task_selftest_stage_1_run_prior_0_abc123")
    decision = AgentDecision(type=DecisionType.PROPOSE_PATCH, summary="delivery", rationale="proof passed", payload={})

    packet = make_packet(
        task=task,
        decision=decision,
        packet_type="delivery",
        body={"work_status": "patch_proposed", "self_test_evidence_ids": [proof_id]},
        actor="dev",
        run_id="run_current",
        stage_id="stage_1",
    )

    assert packet.body["self_test_evidence_ids"] == [proof_id]


def test_delivery_packet_rejects_command_proof_id_from_different_stage(isolate_agent_runtime_root):
    task = _delivery_task()
    proof_id = _attach_command_proof(task_id=task.id, stage_id="other_stage", proof_id="test_task_selftest_other_stage_run_prior_0_def456")
    decision = AgentDecision(type=DecisionType.PROPOSE_PATCH, summary="delivery", rationale="proof passed", payload={})

    with pytest.raises(DecisionPayloadInvalid, match="belongs to a different stage"):
        make_packet(
            task=task,
            decision=decision,
            packet_type="delivery",
            body={"work_status": "patch_proposed", "self_test_evidence_ids": [proof_id]},
            actor="dev",
            run_id="run_current",
            stage_id="stage_1",
        )


def test_delivery_packet_rejects_command_proof_id_from_different_task(isolate_agent_runtime_root):
    task = _delivery_task()
    proof_id = _attach_command_proof(task_id="task_other", stage_id="stage_1", proof_id="test_task_other_stage_1_run_prior_0_ghi789")
    decision = AgentDecision(type=DecisionType.PROPOSE_PATCH, summary="delivery", rationale="proof passed", payload={})

    with pytest.raises(DecisionPayloadInvalid, match="belongs to a different task"):
        make_packet(
            task=task,
            decision=decision,
            packet_type="delivery",
            body={"work_status": "patch_proposed", "self_test_evidence_ids": [proof_id]},
            actor="dev",
            run_id="run_current",
            stage_id="stage_1",
        )


def test_status_unknown_when_no_status_no_exit_code_and_no_crash_signal():
    """Observed-lane honesty (live 2026-07-03): a self-test with neither an
    explicit status nor an exit code must NOT be recorded as passed. It defaults
    to "unknown" so the HUD never claims success the harness did not observe."""
    run = _run()
    evidence = record_self_test_from_progress(
        run,
        "run.tool.finished",
        {
            "tool_name": "terminal",
            "command": "python manage.py check",
            "stdout": "",
            "stderr": "",
        },
    )
    assert evidence is not None
    assert evidence.status == "unknown"
    assert evidence.exit_code is None


def test_status_failed_inferred_from_crash_signature_without_exit_code():
    """The empty-.env / emptied-venv live failures reported no exit code but a
    Django traceback; that must record as failed, not passed."""
    run = _run()
    evidence = record_self_test_from_progress(
        run,
        "run.tool.finished",
        {
            "tool_name": "terminal",
            "command": "python manage.py check",
            "stderr": (
                "Traceback (most recent call last):\n"
                "  File \"manage.py\", line 22, in <module>\n"
                "RuntimeError: DJANGO_SECRET_KEY is required but not set in the environment."
            ),
        },
    )
    assert evidence is not None
    assert evidence.status == "failed"


def test_status_missing_interpreter_inferred_as_failed():
    run = _run()
    evidence = record_self_test_from_progress(
        run,
        "run.tool.finished",
        {
            "tool_name": "terminal",
            "command": "python manage.py check",
            "stderr": "python.exe is not recognized as an internal or external command, operable program or batch file.",
        },
    )
    assert evidence is not None
    assert evidence.status == "failed"


def test_status_passing_pytest_summary_without_exit_code_is_not_false_failed():
    """'0 failed' in a passing pytest summary must not be misread as a failure —
    without an exit code it is honestly "unknown", never "failed"."""
    run = _run()
    evidence = record_self_test_from_progress(
        run,
        "run.tool.finished",
        {
            "tool_name": "terminal",
            "command": "python -m pytest tests/ -q",
            "stdout": "5 passed, 0 failed in 2.10s",
        },
    )
    assert evidence is not None
    assert evidence.status == "unknown"


def test_observed_proof_still_recorded_for_unknown_status():
    """Status honesty must not suppress the observed proof — the R1 lane is
    presence-based, so an "unknown" self-test still attaches its agent_tool_trace
    proof (the authoritative harness re-run enforces pass/fail)."""
    run = _run()
    evidence = record_self_test_from_progress(
        run,
        "run.tool.finished",
        {"tool_name": "terminal", "command": "flutter analyze lib/main.dart"},
    )
    assert evidence is not None
    assert evidence.status == "unknown"
    observed = [
        p
        for p in ProofStore().list_for_task(run.task_id)
        if (p.metadata or {}).get("source") == "agent_tool_trace"
    ]
    assert len(observed) == 1
    assert observed[0].metadata["status"] == "unknown"
