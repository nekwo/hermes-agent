import argparse
import json

import pytest

from hermes_time import now
from agent_runtime.config import AgentRuntimeConfig, persona_records_from_config, load_agent_runtime_config
from agent_runtime.decision_schema import AgentDecision, DecisionPayloadInvalid, DecisionType
from agent_runtime.models import AgentPersona, AgentRun, Incident, Proof, Task, TaskStage
from agent_runtime.plan_review import PlanReviewVerdict
from agent_runtime.planning import apply_planning_decision
from agent_runtime.proof_gates import task_verdict_proof_satisfied
from agent_runtime.proof_rules import ProofType
from agent_runtime.snapshot import build_snapshot
from agent_runtime.states import RunState, StageStatus, TaskState
from agent_runtime.store import AgentStore, ProofStore, RunStore, TaskStore
from agent_runtime.ticker import TickEngine


def _task(state=TaskState.RUNNING):
    ts = now()
    return Task(
        id="task_gap",
        title="Gap",
        description="d",
        state=state,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        stages=[TaskStage(id="stage_1", title="S", objective="o", status=StageStatus.READY, acceptance_criteria=["ok"], test_plan=["pytest"])],
        current_stage_id="stage_1",
    )


def _decision(payload):
    return AgentDecision(type=DecisionType.APPROVE, summary="approved", rationale="r", payload=payload)


def test_plan_approval_requires_explicit_reviewed_stage_ids_and_confirmations():
    t = _task()
    with pytest.raises(DecisionPayloadInvalid):
        apply_planning_decision(t, _decision({"review_scope": "plan"}), actor="qa")
    with pytest.raises(DecisionPayloadInvalid):
        apply_planning_decision(t, _decision({"review_scope": "plan", "reviewed_stage_ids": ["stage_1"], "proof_requirements_confirmed": True}), actor="qa")


def test_explicit_plan_approval_enters_dev_implementing():
    t = _task()
    apply_planning_decision(t, _decision({"review_scope": "plan", "reviewed_stage_ids": ["stage_1"], "proof_requirements_confirmed": True, "test_plan_confirmed": True}), actor="qa")
    assert t.plan_review.verdict == PlanReviewVerdict.APPROVED
    assert t.plan_review.reviewed_stage_ids == ["stage_1"]
    assert t.state == TaskState.RUNNING


def _proof(pt, redaction_status="safe", **meta):
    return Proof(id=f"p_{pt.value}_{redaction_status}", task_id="task_gap", stage_id=None, type=pt, title=str(pt), path_or_value="artifact", created_by="qa", created_at=now(), metadata=meta, redaction_status=redaction_status)


def test_unsafe_or_unscanned_proofs_do_not_satisfy_qa_gate():
    t = _task(TaskState.RUNNING)
    unsafe = [_proof(ProofType.TEST_RUN, "unsafe", exit_code=0), _proof(ProofType.QA_VERDICT, "needs_scan", verdict="approved")]
    result = task_verdict_proof_satisfied(t, unsafe)
    assert not result.allowed
    assert "missing passed test proof" in result.missing
    assert "missing approved QA verdict" in result.missing


def test_snapshot_summarizes_without_raw_errors_paths_or_decisions(tmp_path):
    ts = now()
    t = Task(id="task_gap", title="T", description="contains secret", state=TaskState.CREATED, created_at=ts, updated_at=ts, requested_by="tony", open_incident_ids=["inc_1"])
    run = AgentRun(id="run_1", persona_id="pm", task_id=t.id, stage_id=None, state=RunState.FAILED, started_at=ts, last_heartbeat_at=ts, finished_at=ts, final_decision={"type": "approve", "summary": "raw model output"}, error={"message": "Bearer SECRET"})
    inc = Incident(id="inc_1", task_id=t.id, run_id=run.id, kind="provider_failure", summary="API_KEY=SECRET", detail_path="C:/secret.txt", opened_at=ts)
    proof = _proof(ProofType.SCREENSHOT, path_or_value="C:/secret/screenshot.png")

    class TS:
        def list_all(self): return [t]
    class RS:
        def list_all(self): return [run]
    class AS:
        def list_all(self): return []
    class PS:
        def list_for_task(self, task_id): return [proof]
    class IS:
        def list_all(self): return [inc]
        # The contract build_snapshot reads since cc9db651f: open rows plus a
        # count of the closed tail, never the coerced closed history.
        def list_open_with_closed_count(self): return [inc], 0

    snap = build_snapshot(task_store=TS(), run_store=RS(), agent_store=AS(), proof_store=PS(), incident_store=IS())
    encoded = json.dumps(snap, default=str)
    assert "Bearer SECRET" not in encoded
    assert "API_KEY=SECRET" not in encoded
    assert "C:/secret" not in encoded
    assert "raw model output" not in encoded
    assert list(snap["goals"].values())[0]["open_incident_count"] == 1
    assert snap["proofs"][0]["has_artifact"] is True


def test_tick_engine_uses_stored_persona_configuration():
    ts = now()
    task = Task(id="task_gap", title="T", description="d", state=TaskState.CREATED, created_at=ts, updated_at=ts, requested_by="tony")
    TaskStore().create(task)
    AgentStore().save(AgentPersona(id="neko_supervisor", display_name="Neko Custom", role="alice_supervisor", model="custom-model", provider="custom-provider", api_mode="codex_responses", toolsets=[], system_prompt_path="neko.md"))

    seen = {}
    class Runtime:
        def run_tick(self, persona, ctx, *, run):
            seen["persona"] = persona
            return AgentDecision(type=DecisionType.PROPOSE_ACCEPTANCE, summary="ok", rationale="r", payload={"objective": "obj", "acceptance_criteria": ["done"]})

    result = TickEngine(persona_runtime=Runtime()).tick_once()
    assert result.actions_taken[0].ok is True
    assert seen["persona"].model == "custom-model"


def test_global_default_persona_config_applies_without_per_persona_overrides():
    cfg = AgentRuntimeConfig(default_provider="openai", default_model="gpt-test", personas={})
    personas = {p.id: p for p in persona_records_from_config(cfg)}
    assert "pm" not in personas
    assert personas["neko_supervisor"].provider == "openai"
    assert personas["neko_supervisor"].model == "gpt-test"
    assert personas["dev"].provider == "openai"
    assert personas["qa"].model == "gpt-test"


def test_pm_cannot_rescope_after_qa_approval_with_proof():
    t = _task(TaskState.RUNNING)
    t.proof_ids = ["proof_test", "proof_qa"]
    t.stages[0].status = StageStatus.PASSED
    original_description = t.description
    original_stage_criteria = list(t.stages[0].acceptance_criteria)
    decision = AgentDecision(
        type=DecisionType.PROPOSE_ACCEPTANCE,
        summary="accidental rescope",
        rationale="PM should not re-open accepted work after QA proof approval.",
        payload={"objective": "new scope", "acceptance_criteria": ["new criterion"]},
    )

    apply_planning_decision(t, decision, actor="pm")

    assert t.state == TaskState.DONE
    assert t.description == original_description
    assert t.stages[0].acceptance_criteria == original_stage_criteria


def test_dev_request_qa_review_before_all_stages_complete_stays_in_dev():
    t = _task(TaskState.RUNNING)
    t.stages.append(TaskStage(id="stage_2", title="S2", objective="o2", status=StageStatus.READY, acceptance_criteria=["ok2"], test_plan=["pytest 2"]))
    store = ProofStore()
    proof = _proof(ProofType.TEST_RUN, exit_code=0, status="passed")
    proof.stage_id = "stage_1"
    store.attach(proof)
    decision = AgentDecision(
        type=DecisionType.REQUEST_QA_REVIEW,
        summary="stage 1 ready",
        rationale="Stage 1 has proof but Stage 2 remains.",
        payload={"stage_id": "stage_1", "proof_ids": [proof.id], "handoff": {"to": "qa", "stage_complete": True, "summary": "Stage complete; continue remaining stages before QA.", "changed_paths": [], "proof_ids": [proof.id]}},
    )

    apply_planning_decision(t, decision, actor="dev", proof_store=store)

    assert t.stages[0].status == StageStatus.READY_FOR_QA
    assert t.current_stage_id == "stage_2"
    assert t.state == TaskState.RUNNING


def test_dev_request_qa_review_after_all_stages_complete_enters_neko_coordination_checkpoint():
    t = _task(TaskState.RUNNING)
    t.stages.append(TaskStage(id="stage_2", title="S2", objective="o2", status=StageStatus.READY_FOR_QA, acceptance_criteria=["ok2"], test_plan=["pytest 2"]))
    store = ProofStore()
    proof = _proof(ProofType.TEST_RUN, exit_code=0, status="passed")
    proof.stage_id = "stage_1"
    store.attach(proof)
    decision = AgentDecision(
        type=DecisionType.REQUEST_QA_REVIEW,
        summary="all stages ready",
        rationale="Final stage has proof and prior stages are ready for QA.",
        payload={"stage_id": "stage_1", "proof_ids": [proof.id], "handoff": {"to": "qa", "stage_complete": True, "summary": "Stage complete; continue remaining stages before QA.", "changed_paths": [], "proof_ids": [proof.id]}},
    )

    apply_planning_decision(t, decision, actor="dev", proof_store=store)

    assert all(stage.status == StageStatus.READY_FOR_QA for stage in t.stages)
    assert t.state == TaskState.RUNNING

    action = TickEngine().state_machine.next_action(t)

    assert action.type.value == "run_slot"
    assert action.slot_id == "dev"
    assert "stage_2" in action.reason


def test_neko_qa_coordination_release_allows_qa_verification():
    t = _task(TaskState.RUNNING)
    t.stages[0].status = StageStatus.READY_FOR_QA
    t.proof_ids = ["proof_test_safe"]
    decision = AgentDecision(
        type=DecisionType.PROPOSE_ACCEPTANCE,
        summary="QA handoff is coordinated",
        rationale="Neko verified stage/proof completeness and selected QA as next owner.",
        payload={"objective": "verify implementation", "acceptance_criteria": ["QA reviews proof ids"]},
    )

    apply_planning_decision(t, decision, actor="neko_supervisor")
    action = TickEngine().state_machine.next_action(t)

    assert t.state == TaskState.RUNNING
    assert action.type.value == "run_slot"
    assert action.slot_id == "qa"


def test_neko_visual_recovery_repairs_stale_mission_control_stagec_test_plan(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "agent-runtime"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "alice"))
    t = _task(TaskState.BLOCKED)
    t.title = "Upgrade Launcher Mission Control agent terminal event view"
    t.description = "Mission Control needs fullscreen Stage C screenshot proof."
    t.current_stage_id = "stage_launcher_analyze_and_visual_proof"
    t.stages = [
        TaskStage(
            id="stage_launcher_analyze_and_visual_proof",
            title="Collect analyze, targeted tests, and fullscreen Stage C screenshot proof",
            objective="Request Harness-owned proof and visual QA evidence.",
            status=StageStatus.IMPLEMENTING,
            acceptance_criteria=["Fullscreen Stage C MCP/QA screenshot proof shows Mission Control loaded."],
            test_plan=[
                "flutter analyze lib/features/mission_control",
                "powershell.exe -NoProfile -ExecutionPolicy Bypass -File docs/stages/qa-reboot/scripts/Invoke-LauncherQaMcpTool.ps1 -Tool mcp_launcher_qa_open_app_tab -ArgsJson '{\"tab\":\"missionControl\",\"browser_login\":true,\"credential_profile\":\"stagec-smoke\",\"screenshot\":false,\"reap_stale\":true}' -CallTimeoutSeconds 240 && powershell.exe -NoProfile -ExecutionPolicy Bypass -File docs/stages/qa-reboot/scripts/Invoke-LauncherQaMcpTool.ps1 -Tool mcp_launcher_qa_screenshot_window -ArgsJson '{\"window_title_prefix\":\"Eternia Launcher\",\"label\":\"mission_control_old\",\"out_dir\":\"X:/tmp/stagec/screenshots\",\"screenshot_stabilize_ms\":4000,\"screenshot_max_retries\":5}' -CallTimeoutSeconds 180",
            ],
        )
    ]
    decision = AgentDecision(
        type=DecisionType.PROPOSE_ACCEPTANCE,
        summary="Release corrected visual recovery",
        rationale="Neko steers the missing visual proof.",
        payload={
            "objective": t.description,
            "acceptance_criteria": ["Fullscreen Stage C screenshot proof is attached."],
            "affected_repos": ["EterniaLauncher"],
            "handoff_packet": {
                "packet_kind": "bounded_visual_proof_recovery",
                "mission_phase": "visual_proof_recovery",
                "handoff_mode": "single_specialist",
                "target_owner": "dev",
                "target_repo": "EterniaLauncher",
                "target_dev_persona": "dev",
                "proof_gate": {
                    "required": True,
                    "minimum_status": "passed",
                    "visual_required": True,
                    "required_proof_types": ["fullscreen_stage_c_screenshot"],
                },
                "join_gate": {"release_condition": "screenshot proof attached"},
            },
        },
    )

    apply_planning_decision(t, decision, actor="neko_supervisor")

    stage = t.stages[0]
    plan_text = "\n".join(stage.test_plan)
    assert t.state == TaskState.RUNNING
    assert stage.requires_visual_proof is None
    assert len(stage.test_plan) == 2
    assert "flutter analyze lib/features/mission_control" in stage.test_plan
    assert "mcp_launcher_qa_screenshot_window" in plan_text
    assert stage.audit_notes == []


def test_neko_visual_recovery_targets_noncomplete_visual_stage_not_prior_ui_stage(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "agent-runtime"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "alice"))
    t = _task(TaskState.RUNNING)
    t.title = "Upgrade Launcher Mission Control agent terminal event view"
    t.description = "Mission Control needs fullscreen Stage C screenshot proof."
    t.current_stage_id = "stage_agent_event_feed_ui"
    t.risk_flags = ["sequential_specialist_handoff", "cross_stack_backend_proof_missing_before_launcher_release"]
    ui_stage = TaskStage(
        id="stage_agent_event_feed_ui",
        title="Mission Control UI feed",
        objective="Render the selected-agent UI rows.",
        status=StageStatus.READY_FOR_QA,
        acceptance_criteria=["Mission Control event rows render."],
        test_plan=["Collect fullscreen Stage C MCP screenshot proof only after deterministic tests pass."],
    )
    visual_stage = TaskStage(
        id="stage_launcher_analyze_and_visual_proof",
        title="Collect analyze, targeted tests, and fullscreen Stage C screenshot proof",
        objective="Request Harness-owned proof and visual QA evidence.",
        status=StageStatus.IMPLEMENTING,
        acceptance_criteria=["Fullscreen Stage C MCP/QA screenshot proof shows Mission Control loaded."],
        test_plan=[
            "flutter analyze lib/features/mission_control",
            "powershell.exe -NoProfile -ExecutionPolicy Bypass -File docs/stages/qa-reboot/scripts/Invoke-LauncherQaMcpTool.ps1 -Tool mcp_launcher_qa_open_app_tab -ArgsJson '{\"tab\":\"missionControl\",\"browser_login\":true,\"credential_profile\":\"stagec-smoke\",\"screenshot\":false,\"reap_stale\":true}' -CallTimeoutSeconds 240 && powershell.exe -NoProfile -ExecutionPolicy Bypass -File docs/stages/qa-reboot/scripts/Invoke-LauncherQaMcpTool.ps1 -Tool mcp_launcher_qa_screenshot_window -ArgsJson '{\"window_title_prefix\":\"Eternia Launcher\",\"label\":\"mission_control_old\",\"out_dir\":\"X:/tmp/stagec/screenshots\",\"screenshot_stabilize_ms\":4000}' -CallTimeoutSeconds 180",
        ],
    )
    t.stages = [ui_stage, visual_stage]
    decision = AgentDecision(
        type=DecisionType.PROPOSE_ACCEPTANCE,
        summary="Release corrected visual recovery",
        rationale="Neko steers the missing visual proof.",
        payload={
            "objective": t.description,
            "acceptance_criteria": ["Fullscreen Stage C screenshot proof is attached."],
            "affected_repos": ["EterniaLauncher"],
            "handoff_packet": {
                "packet_kind": "bounded_visual_proof_recovery",
                "mission_phase": "visual_proof_recovery",
                "handoff_mode": "single_specialist",
                "target_owner": "dev",
                "target_repo": "EterniaLauncher",
                "proof_gate": {
                    "required": True,
                    "minimum_status": "passed",
                    "visual_required": True,
                    "required_proof_types": ["fullscreen_stage_c_screenshot"],
                },
                "join_gate": {"release_condition": "screenshot proof attached"},
            },
        },
    )

    apply_planning_decision(t, decision, actor="neko_supervisor")

    assert t.state == TaskState.RUNNING
    assert t.current_stage_id == "stage_agent_event_feed_ui"
    assert "cross_stack_backend_proof_missing_before_launcher_release" not in t.risk_flags
    assert "sequential_specialist_handoff" not in t.risk_flags
    assert len(ui_stage.test_plan) == 1
    assert len(visual_stage.test_plan) == 2
    visual_plan_text = "\n".join(visual_stage.test_plan)
    assert "mcp_launcher_qa_screenshot_window" in visual_plan_text


def test_visual_stage_request_qa_review_does_not_require_bridge_snapshot_command():
    t = _task(TaskState.RUNNING)
    t.current_stage_id = "stage_launcher_analyze_and_visual_proof"
    t.stages = [
        TaskStage(
            id="stage_bridge_archive_regression",
            title="Preserve Mission Control runtime root/profile snapshot and archive bridge behavior",
            objective="Run existing bridge/archive regression coverage.",
            status=StageStatus.READY_FOR_QA,
            acceptance_criteria=["Existing Mission Control bridge tests pass."],
            test_plan=[
                "flutter test test/features/mission_control/mission_control_snapshot_test.dart test/features/mission_control/mission_control_bridge_test.dart"
            ],
        ),
        TaskStage(
            id="stage_launcher_analyze_and_visual_proof",
            title="Collect analyze, targeted tests, and fullscreen Stage C screenshot proof",
            objective="Request Harness-owned proof and visual QA evidence.",
            status=StageStatus.IMPLEMENTING,
            acceptance_criteria=[
                "Existing bridge/archive regression tests pass.",
                "Fullscreen Stage C MCP/QA screenshot proof shows Mission Control loaded.",
            ],
            test_plan=[
                "flutter analyze lib/features/mission_control",
                "powershell.exe -NoProfile -ExecutionPolicy Bypass -File docs/stages/qa-reboot/scripts/Invoke-LauncherQaMcpTool.ps1 -Tool mcp_launcher_qa_open_app_tab -ArgsJson '{\"tab\":\"missionControl\",\"screenshot\":true}'",
            ],
        ),
    ]
    store = ProofStore()
    visual_proof = Proof(
        id="proof_visual_stagec",
        task_id=t.id,
        stage_id="stage_launcher_analyze_and_visual_proof",
        type=ProofType.TEST_RUN,
        title="Stage C screenshot proof",
        path_or_value="proof.log",
        created_by="harness",
        created_at=now(),
        metadata={
            "status": "passed",
            "exit_code": 0,
            "command": "mcp_launcher_qa_open_app_tab missionControl screenshot true",
        },
        redaction_status="safe",
    )
    store.attach(visual_proof)
    decision = AgentDecision(
        type=DecisionType.REQUEST_QA_REVIEW,
        summary="visual proof ready",
        rationale="Mission Control screenshot proof passed.",
        payload={
            "stage_id": "stage_launcher_analyze_and_visual_proof",
            "proof_ids": [visual_proof.id],
            "handoff": {"to": "qa", "stage_complete": True},
        },
    )

    apply_planning_decision(t, decision, actor="dev", proof_store=store)

    assert t.state == TaskState.RUNNING
    assert t.stages[-1].status == StageStatus.READY_FOR_QA
    assert visual_proof.id in t.proof_ids


def test_request_test_run_from_premature_dev_ready_returns_to_dev_implementing():
    t = _task(TaskState.RUNNING)
    t.stages[0].status = StageStatus.READY_FOR_QA
    t.stages.append(TaskStage(id="stage_2", title="S2", objective="o2", status=StageStatus.IMPLEMENTING, acceptance_criteria=["ok2"], test_plan=["pytest 2"]))
    t.current_stage_id = "stage_2"
    decision = AgentDecision(
        type=DecisionType.REQUEST_TEST_RUN,
        summary="collect stage 2 proof",
        rationale="Stage 2 needs command proof before any QA handoff.",
        payload={"stage_id": "stage_2", "commands": ["pytest stage2"]},
    )

    apply_planning_decision(t, decision, actor="dev")

    assert t.state == TaskState.RUNNING
    assert t.current_stage_id == "stage_2"


def test_qa_approval_marks_all_stages_passed_only_after_full_stage_handoff():
    t = _task(TaskState.RUNNING)
    t.stages.append(TaskStage(id="stage_2", title="S2", objective="o2", status=StageStatus.READY_FOR_QA, acceptance_criteria=["ok2"], test_plan=["pytest 2"]))
    t.stages[0].status = StageStatus.READY_FOR_QA
    store = ProofStore()
    proof = _proof(ProofType.TEST_RUN, exit_code=0, status="passed")
    store.attach(proof)
    decision = AgentDecision(
        type=DecisionType.REPORT_QA_VERDICT,
        summary="approved full implementation",
        rationale="QA reviewed the complete implementation handoff.",
        payload={"review_scope": "implementation", "verdict": "approved", "proof_ids": [proof.id], "findings": []},
    )

    apply_planning_decision(t, decision, actor="qa", proof_store=store)

    assert t.state == TaskState.RUNNING
    assert [stage.status for stage in t.stages] == [StageStatus.PASSED, StageStatus.PASSED]


def test_complete_task_is_blocked_until_all_stages_passed():
    t = _task(TaskState.RUNNING)
    t.proof_ids = ["proof_test", "proof_qa"]
    t.stages.append(TaskStage(id="stage_2", title="S2", objective="o2", status=StageStatus.READY, acceptance_criteria=["ok2"], test_plan=["pytest 2"]))

    action = TickEngine().state_machine.next_action(t)

    assert action.type.value != "complete_task"
    assert action.type.value == "run_slot"
    assert action.slot_id == "dev"


def test_pm_approve_after_qa_approval_closes_instead_of_reopening_review():
    t = _task(TaskState.RUNNING)
    t.proof_ids = ["proof_test", "proof_qa"]
    t.stages[0].status = StageStatus.PASSED
    decision = AgentDecision(
        type=DecisionType.APPROVE,
        summary="ready to close",
        rationale="QA approved with proof.",
        payload={"review_scope": "implementation", "proof_ids": ["proof_test", "proof_qa"], "verdict": "approved"},
    )

    apply_planning_decision(t, decision, actor="pm")

    assert t.state == TaskState.DONE


def test_qa_blocked_implementation_verdict_marks_task_recoverable_for_dev():
    t = _task(TaskState.RUNNING)
    store = ProofStore()
    store.attach(_proof(ProofType.TEST_RUN, exit_code=1, status="failed"))
    decision = AgentDecision(
        type=DecisionType.REPORT_QA_VERDICT,
        summary="blocked on proof gaps",
        rationale="QA cannot approve missing proof mapping.",
        payload={
            "review_scope": "implementation",
            "verdict": "blocked",
            "proof_ids": ["p_test_run_safe"],
            "findings": [{"severity": "blocking", "issue": "missing proof manifest", "required_fix": "attach mapping"}],
        },
    )

    apply_planning_decision(t, decision, actor="qa", proof_store=store)

    assert t.state == TaskState.RUNNING
    assert t.harness_self_heal["evidence_stack"][-1]["kind"] == "blocked_escalation"
    assert "qa_blocked_verdict_needs_dev_recovery" in t.risk_flags
    assert t.stages[0].status == StageStatus.BLOCKED
