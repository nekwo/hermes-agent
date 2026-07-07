import json
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_time import now
from agent_runtime.actions import HarnessAction, HarnessActionType
from agent_runtime.blueprints import BlueprintStore, instantiate_blueprint
from agent_runtime.events import EventLog
from agent_runtime.recovery_flags import NEKO_BLOCK_RECOVERY_ATTEMPTED_FLAG
from agent_runtime.ticker import TickEngine, _emit_decision_process_summary, _handoff_diff_weakens_tests, _validate_observed_trace_requirement, _validate_request_test_run_targets_current_stage
from agent_runtime.decision_schema import AgentDecision, DecisionPayloadInvalid, DecisionType
from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.errors import NotFound
from agent_runtime.models import Event, Incident, MissionIntent, MissionPlan, MissionPlanStage, Proof, RepoBundle, Task, TaskStage
from agent_runtime.proof_rules import ProofType
from agent_runtime.repo_bundles import RepoBundleStore
from agent_runtime.runtime_config import ContinuousRoleSessionConfig, MissionPlanConfig, NormalWorkerFlowConfig, RuntimeConfig, SwarmConfig
from agent_runtime.states import RunState, StageStatus, TaskState
from agent_runtime.store import AgentStore, IncidentStore, ProofStore, RunStore, TaskStore
from agent_runtime.profile_runner import RunBudgetExceeded


def test_handoff_diff_test_tampering_detector_fails_closed():
    task = make_task_with_id("task_test_tamper")
    task.harness_self_heal = {
        "stage_observations": {
            "stage_1": {
                "repo_diff": {
                    "diff": "\n".join(
                        [
                            "diff --git a/tests/test_example.py b/tests/test_example.py",
                            "-    assert result == 42",
                            "+    assert True",
                        ]
                    )
                }
            },
            "stage_product": {
                "repo_diff": {
                    "diff": "\n".join(
                        [
                            "diff --git a/app/example.py b/app/example.py",
                            "-    assert result == 42",
                        ]
                    )
                }
            },
        }
    }

    assert _handoff_diff_weakens_tests(task, "stage_1") is True
    assert _handoff_diff_weakens_tests(task, "stage_product") is False


def test_handoff_observation_links_same_stage_observed_proof_from_earlier_turn(isolate_agent_runtime_root):
    # R1 regression: dev work is multi-turn — the self-test command runs in an
    # earlier turn (run A) than the handoff turn (run B). The observed lane must
    # link a same-stage agent_tool_trace proof even when its run_id differs from
    # the handoff run; keying on run_id alone left observed_proof_ids empty.
    from agent_runtime.ticker import _record_handoff_observation
    from agent_runtime.decision_schema import AgentDecision, DecisionType
    from agent_runtime.models import Proof
    from agent_runtime.proof_rules import ProofType

    ts = TaskStore()
    task = make_task_with_id("task_obs_link")
    task.affected_repos = []
    task.current_stage_id = "implement"
    ts.create(task)
    proofs = ProofStore()
    proofs.attach(
        Proof(
            id="proof_observed_earlier_turn",
            task_id=task.id,
            stage_id="implement",
            type=ProofType.TEST_RUN,
            title="Observed agent proof: flutter analyze",
            path_or_value="observed.json",
            created_by="dev",
            created_at=now(),
            metadata={"source": "agent_tool_trace", "authoritative": False, "run_id": "run_earlier_turn", "status": "passed"},
            redaction_status="safe",
        )
    )
    handoff_run = RunStore().open_run("dev", task.id, stage_id="implement")  # a DIFFERENT run id
    _record_handoff_observation(
        task,
        AgentDecision(type=DecisionType.PROPOSE_PATCH, summary="hand off", rationale="done", payload={"stage_id": "implement"}),
        run=handoff_run,
        actor="dev",
        proof_store=proofs,
        command_workdir=None,
        task_store=ts,
    )

    obs = (ts.get(task.id).harness_self_heal or {}).get("stage_observations") or {}
    assert obs["implement"]["observed_proof_ids"] == ["proof_observed_earlier_turn"]


def test_handoff_observation_fires_on_legacy_request_test_run_delivery(isolate_agent_runtime_root):
    # Contract-parity gap: under the simplified flag a collapsed hand_off projects
    # onto PROPOSE_PATCH (observed), but on the legacy/rollback path a no-edit dev
    # delivers via REQUEST_TEST_RUN. The harness observe-the-work lane must fire for
    # it too, so the HUD diff/trace surface is populated regardless of contract mode.
    from agent_runtime.ticker import _record_handoff_observation
    from agent_runtime.decision_schema import AgentDecision, DecisionType

    ts = TaskStore()
    task = make_task_with_id("task_legacy_observe")
    task.affected_repos = []
    task.current_stage_id = "implement"
    ts.create(task)
    run = RunStore().open_run("dev", task.id, stage_id="implement")
    decision = AgentDecision(
        type=DecisionType.REQUEST_TEST_RUN,
        summary="Legacy no-edit delivery via gate request.",
        rationale="Rollback path: dev requests the authoritative gate.",
        payload={"stage_id": "implement", "commands": ["flutter analyze"]},
    )

    _record_handoff_observation(
        task,
        decision,
        run=run,
        actor="dev",
        proof_store=ProofStore(),
        command_workdir=None,
        task_store=ts,
    )

    observations = (ts.get(task.id).harness_self_heal or {}).get("stage_observations") or {}
    assert "implement" in observations
    assert observations["implement"]["source"] == "harness_observed_handoff"


def test_observed_trace_requirement_rejects_handoff_without_agent_tool_trace(isolate_agent_runtime_root):
    task = make_task_with_id("task_observed_required")
    task.current_stage_id = "implement"
    task.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title="Observed lane", objective="Require observed proof."),
        current_stage_id="implement",
        stages=[
            MissionPlanStage(
                id="implement",
                title="Implement",
                objective="Run a real self-test before handoff.",
                owner="dev",
                repo="EterniaLauncher",
                kind="proof_only",
                status=StageStatus.IMPLEMENTING,
                proof_gate={
                    "required": True,
                    "minimum_status": "passed",
                    "required_proof_types": ["test_run"],
                    "observed_lane_required": True,
                    "observed_lane_requirement": "agent_tool_trace required",
                },
            )
        ],
    )
    run = RunStore().open_run("dev", task.id, stage_id="implement")
    decision = AgentDecision(
        type=DecisionType.PROPOSE_PATCH,
        summary="handoff",
        rationale="done",
        payload={"stage_id": "implement"},
    )
    proofs = ProofStore()

    with pytest.raises(DecisionPayloadInvalid, match="observed agent_tool_trace proof required"):
        _validate_observed_trace_requirement(task, decision, run=run, proof_store=proofs)

    proofs.attach(
        Proof(
            id="proof_observed_live",
            task_id=task.id,
            stage_id="implement",
            type=ProofType.TEST_RUN,
            title="Observed self-test",
            path_or_value="self_tests/task/selftest.json",
            created_by="dev",
            created_at=now(),
            metadata={"source": "agent_tool_trace", "run_id": run.id, "status": "passed"},
            redaction_status="safe",
        )
    )

    _validate_observed_trace_requirement(task, decision, run=run, proof_store=proofs)


def test_request_test_run_allows_typed_current_stage_when_legacy_current_stage_is_stale():
    task = make_task_with_id("task_typed_current_stage")
    task.state = TaskState.RUNNING
    typed_stage_id = "verify_the_hermes_harness_mission_control_log_snapshot_contract"
    task.current_stage_id = "launcher_implementation"
    task.stages = [
        TaskStage(
            id=typed_stage_id,
            title="Implementation",
            objective="Verify Harness log snapshot proof without product edits.",
            status=StageStatus.IMPLEMENTING,
            test_plan=["python -m hermes_cli.main harness status --json"],
        ),
        TaskStage(
            id="launcher_implementation",
            title="Launcher Implementation",
            objective="Stale synthesized Launcher work.",
            status=StageStatus.IMPLEMENTING,
        ),
    ]
    task.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title="Harness thinking log smoke", objective="Verify Harness logs."),
        current_stage_id=typed_stage_id,
        stages=[
            MissionPlanStage(
                id=typed_stage_id,
                title="Implementation",
                objective="Verify Harness log snapshot proof without product edits.",
                owner="dev",
                repo="hermes-agent",
                kind="implementation",
            ),
        ],
    )
    decision = AgentDecision(
        type=DecisionType.REQUEST_TEST_RUN,
        summary="Request Harness proof.",
        rationale="Typed current stage is authoritative.",
        payload={"stage_id": typed_stage_id, "recipe_id": "harness_runtime_status_snapshot"},
    )

    _validate_request_test_run_targets_current_stage(task, decision)


def test_no_progress_guard_routes_dev_repetition_to_neko():
    task = make_task_with_id("task_no_progress_guard")
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    engine = TickEngine(task_store=TaskStore(), persona_runtime=FakeRuntime())
    envelope = engine.role_envelope_store.open_or_resume(
        task=task,
        role_id="dev",
        mission_stage_id="stage_1",
        worker_session_id="worker_1",
        run_id="run_1",
    )
    envelope.no_progress_count = 1

    engine._apply_no_progress_guard(task, SimpleNamespace(id="run_1"), "dev", "request_test_run", envelope)

    assert task.state == TaskState.RUNNING
    assert "no_progress_escalated_to_neko" in task.risk_flags
    events = EventLog().for_task(task.id, limit=5)
    assert any(event.payload.get("step") == "no_progress_guard" for event in events)


def test_request_test_run_rejects_no_edit_investigation_context_without_gate():
    task = make_task_with_id("task_context_investigation")
    task.state = TaskState.RUNNING
    task.current_stage_id = "backend_investigation"
    task.stages = [
        TaskStage(
            id="backend_investigation",
            title="Backend Investigation",
            objective="No-product-edit investigation: inspect backend moderation paths and produce findings.",
            status=StageStatus.IMPLEMENTING,
        )
    ]
    task.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title=task.title, objective=task.description),
        current_stage_id="backend_investigation",
        stages=[
            MissionPlanStage(
                id="backend_investigation",
                title="Backend Investigation",
                objective="No-product-edit investigation: inspect backend moderation paths and produce findings.",
                owner="backend_dev",
                repo="EterniaBackend",
                kind="context",
                requires_product_edit=False,
                status=StageStatus.IMPLEMENTING,
            )
        ],
    )
    decision = AgentDecision(
        type=DecisionType.REQUEST_TEST_RUN,
        summary="Incorrectly request a smoke proof for an investigation context stage.",
        rationale="This should be repaired into context reads instead.",
        payload={"stage_id": "backend_investigation", "commands": ["python -c \"print('fake')\""]},
    )

    with pytest.raises(DecisionPayloadInvalid, match="no-edit investigation context"):
        _validate_request_test_run_targets_current_stage(task, decision)


def test_qa_implementation_verdict_can_use_delivery_packet_without_command_proof():
    from agent_runtime.decision_contracts import validate_planning_decision

    decision = AgentDecision(
        type=DecisionType.REPORT_QA_VERDICT,
        summary="Approved delivery packet.",
        rationale="QA reviewed the no-edit investigation delivery packet.",
        payload={
            "review_scope": "implementation",
            "verdict": "approved",
            "delivery_packets_reviewed": ["packet_de_report"],
            "findings": [],
        },
    )

    validate_planning_decision(decision)


def test_repeated_request_file_reads_rejected_after_no_edit_investigation_context_threshold():
    from agent_runtime.planning import apply_planning_decision

    task = make_task_with_id("task_context_investigation")
    task.state = TaskState.RUNNING
    task.current_stage_id = "backend_investigation"
    task.context_requests = [
        {"id": "ctx_1", "actor": "backend_dev", "stage_id": "backend_investigation", "status": "fulfilled"},
        {"id": "ctx_2", "actor": "backend_dev", "stage_id": "backend_investigation", "status": "fulfilled_partial"},
    ]
    task.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title=task.title, objective=task.description),
        current_stage_id="backend_investigation",
        stages=[
            MissionPlanStage(
                id="backend_investigation",
                title="Backend Investigation",
                objective="No-product-edit investigation: inspect backend moderation paths and produce findings.",
                owner="backend_dev",
                repo="EterniaBackend",
                kind="investigation",
                requires_product_edit=False,
                status=StageStatus.IMPLEMENTING,
            )
        ],
    )
    decision = AgentDecision(
        type=DecisionType.REQUEST_FILE_READS,
        summary="Need even more files",
        rationale="This would otherwise keep looping.",
        payload={"paths": ["posts"], "reason": "more context"},
    )

    with pytest.raises(DecisionPayloadInvalid, match="two or more context bundles"):
        apply_planning_decision(task, decision, actor="backend_dev", mission_plan_flow=True)


def test_decision_process_summary_emits_redaction_safe_thinking_log():
    events = EventLog()
    runs = RunStore(event_log=events)
    run = runs.open_run("dev", "task_decision_summary", stage_id="stage_1")
    decision = AgentDecision(
        type=DecisionType.REQUEST_TEST_RUN,
        summary="Request Harness proof.",
        rationale="Need passed status snapshot proof before QA. C:/Users/beast/private_token.txt must not leak.",
        payload={"stage_id": "stage_1", "recipe_id": "harness_runtime_status_snapshot"},
    )

    _emit_decision_process_summary(runs, run.id, decision)

    progress_events = [event for event in events.for_task("task_decision_summary", limit=10) if event.type == "run.progress"]
    assert len(progress_events) == 1
    payload = progress_events[0].payload
    assert payload["phase"] == "thinking_process"
    assert payload["step"] == "decision_summary"
    assert payload["decision_type"] == "request_test_run"
    assert payload["reasoning_summary"] == "Request Harness proof."
    assert "C:/Users" not in repr(payload)
    assert "private_token" not in repr(payload)

class FakeRuntime:
    def __init__(self):
        self.personas = []

    def run_tick(self, persona, ctx, *, run):
        self.personas.append(persona)
        from agent_runtime.decision_schema import AgentDecision, DecisionType
        return AgentDecision(type=DecisionType.PROPOSE_ACCEPTANCE, summary="pm", rationale="r", payload={"objective":"obj","acceptance_criteria":["ok"]})


class CapturingFailureRuntime:
    def __init__(self):
        self.personas = []

    def run_tick(self, persona, ctx, *, run):
        self.personas.append(persona.id)
        raise RuntimeError("stop after persona selection")


class BadRuntime:
    def run_tick(self, persona, ctx, *, run):
        from agent_runtime.decision_schema import DecisionPayloadInvalid
        raise DecisionPayloadInvalid("bad")


class ProgressThenInvalidRuntime:
    def run_tick(self, persona, ctx, *, run):
        from agent_runtime.decision_schema import DecisionPayloadInvalid
        from agent_runtime.progress import RunProgressSink

        RunProgressSink(run_store=RunStore(), run_id=run.id).emit(
            "run.progress",
            {
                "type": "run.progress",
                "phase": "autonomy",
                "step": "autonomy_packet",
                "status": "ready",
                "autonomy_packet_id": "auto_live",
                "context_receipt_id": "ctxr_live",
                "read_search_limit": 3,
            },
        )
        run.llm = {"api_calls": 1, "total_tokens": 42, "session_id": "session_safe"}
        raise DecisionPayloadInvalid("handoff_packet.target_repo is invalid")


class MissingProofHandoffRuntime:
    def run_tick(self, persona, ctx, *, run):
        from agent_runtime.decision_schema import DecisionPayloadInvalid
        raise DecisionPayloadInvalid("proof_ids are required before handing implementation to QA")


class CaptureRepairContextRuntime:
    def __init__(self):
        self.contexts = []

    def run_tick(self, persona, ctx, *, run):
        self.contexts.append(ctx)
        from agent_runtime.decision_schema import AgentDecision, DecisionType
        return AgentDecision(type=DecisionType.PROPOSE_ACCEPTANCE, summary="pm", rationale="r", payload={"objective": "obj", "acceptance_criteria": ["ok"]})


class ContinuousRoleSessionDevRuntime:
    def __init__(self):
        self.calls = []

    def run_tick(self, persona, ctx, *, run):
        self.calls.append({"persona": persona.id, "run_id": run.id, "stage_id": ctx.task.current_stage_id})
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        if len(self.calls) == 1:
            return AgentDecision(
                type=DecisionType.CORRECT_STAGE,
                summary="tighten stage before proof",
                rationale="Stage remains owned by Dev and should continue in the same role session.",
                payload={"stage_id": "stage_1", "audit_notes": ["stage remains bounded"]},
            )
        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="collect focused proof",
            rationale="Stage is ready for deterministic proof.",
            payload={"stage_id": "stage_1", "commands": ["printf role-session-ok\\n"]},
        )


class PassingProofRunner:
    def __init__(self, proof_store: ProofStore):
        self.proof_store = proof_store

    def run_commands(self, task, *, stage_id, run_id, actor, commands):
        proof = Proof(
            id=f"proof_{task.id}_{stage_id}",
            task_id=task.id,
            stage_id=stage_id,
            type=ProofType.TEST_RUN,
            title="passing proof",
            path_or_value="passed",
            created_by="harness",
            created_at=now(),
            metadata={"status": "passed", "run_id": run_id, "actor_requested": actor},
            redaction_status="safe",
        )
        return [self.proof_store.attach(proof)]


class InvalidDeliveryThenCorrectRuntime:
    def __init__(self):
        self.contexts = []

    def run_tick(self, persona, ctx, *, run):
        self.contexts.append(ctx)
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        next_owner = "backend-dev" if len(self.contexts) == 1 else "neko_supervisor"
        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="collect proof with delivery packet",
            rationale="The stage needs a deterministic proof and then Neko should join the handoff.",
            payload={
                "stage_id": "stage_1",
                "commands": ["echo stage48-ok"],
                "delivery": {
                    "work_status": "proof_requested",
                    "next_owner": next_owner,
                },
            },
        )


class RaisedInvalidHandoffThenCorrectRuntime:
    def __init__(self):
        self.contexts = []

    def run_tick(self, persona, ctx, *, run):
        self.contexts.append(ctx)
        from agent_runtime.decision_schema import AgentDecision, DecisionPayloadInvalid, DecisionType

        if len(self.contexts) == 1:
            raise DecisionPayloadInvalid("handoff_packet.handoff_mode is required")
        return AgentDecision(
            type=DecisionType.PROPOSE_ACCEPTANCE,
            summary="scope after repair",
            rationale="The second call received the repair HUD and returned a valid decision.",
            payload={"objective": "obj", "acceptance_criteria": ["ok"]},
        )


class RepeatedInvalidDeliveryRuntime:
    def __init__(self):
        self.contexts = []

    def run_tick(self, persona, ctx, *, run):
        self.contexts.append(ctx)
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="collect proof with bad delivery packet",
            rationale="This intentionally repeats the invalid owner to prove bounded repair escalation.",
            payload={
                "stage_id": "stage_1",
                "commands": ["echo stage48-bad"],
                "delivery": {
                    "work_status": "proof_requested",
                    "next_owner": "backend-dev",
                },
            },
        )


class MissingProviderDependencyRuntime:
    def run_tick(self, persona, ctx, *, run):
        raise ModuleNotFoundError("No module named 'openai'")


class GPTPersonaRuntime:
    def run_tick(self, persona, ctx, *, run):
        raise RuntimeError("stop after preflight")


class BlueprintSmokeRuntime:
    def __init__(self):
        self.personas = []

    def run_tick(self, persona, ctx, *, run):
        self.personas.append(persona.id)
        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="collect blueprint smoke proof",
            rationale="The blueprint build stage needs deterministic proof.",
            payload={"stage_id": "build", "commands": ["printf blueprint-smoke\\n"]},
        )


class ResolveIncidentRuntime:
    def run_tick(self, persona, ctx, *, run):
        from agent_runtime.decision_schema import AgentDecision, DecisionType
        incident_id = ctx.task.open_incident_ids[0]
        return AgentDecision(
            type=DecisionType.RESOLVE_INCIDENT,
            summary="route back to dev with proof requirements clarified",
            rationale="incident has a deterministic recovery path",
            payload={"incident_id": incident_id, "resolution": "retry dev with request_test_run proof", "next_state": "dev_test_design"},
        )


class InspectIncidentContextRuntime:
    def __init__(self):
        self.incident_records = None

    def run_tick(self, persona, ctx, *, run):
        self.incident_records = ctx.incident_records
        from agent_runtime.decision_schema import AgentDecision, DecisionType
        incident_id = ctx.task.open_incident_ids[0]
        return AgentDecision(
            type=DecisionType.RESOLVE_INCIDENT,
            summary="resolve with incident context",
            rationale="incident details were supplied",
            payload={"incident_id": incident_id, "resolution": "retry", "next_state": "dev_test_design"},
        )


class TransientProviderRuntime:
    def __init__(self):
        self.calls = 0

    def run_tick(self, persona, ctx, *, run):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("Codex stream produced no bytes within 12s (TTFB threshold: 12s)")
        from agent_runtime.decision_schema import AgentDecision, DecisionType
        return AgentDecision(type=DecisionType.PROPOSE_ACCEPTANCE, summary="pm retry ok", rationale="r", payload={"objective":"obj","acceptance_criteria":["ok"]})


class OrchestrationOnlyBackendPlanRuntime:
    def __init__(self):
        self.personas = []

    def run_tick(self, persona, ctx, *, run):
        self.personas.append(persona.id)
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        return AgentDecision(
            type=DecisionType.PROPOSE_STAGE_PLAN,
            summary="repeat orchestration instead of backend proof",
            rationale="This mirrors the live Stage 47 loop: Backend Dev keeps planning Neko/Launcher/QA gates.",
            payload={
                "stages": [
                    {
                        "id": "stage_47_neko_backend_join",
                        "title": "Neko backend join gate",
                        "objective": "Neko verifies backend proof before Launcher release.",
                        "acceptance_criteria": ["Neko join is recorded."],
                        "affected_paths": ["harness event log"],
                        "test_plan": ["Neko join event"],
                    },
                    {
                        "id": "stage_47_launcher_contract_consumption",
                        "title": "Launcher contract consumption",
                        "objective": "Launcher consumes joined backend contract.",
                        "acceptance_criteria": ["Launcher proof is attached."],
                        "affected_paths": ["EterniaLauncher"],
                        "test_plan": ["flutter test test/mission_control_contract_test.dart"],
                    },
                ]
            },
        )


def make_task():
    ts=now(); return Task(id="task_1", title="T", description="d", state=TaskState.CREATED, created_at=ts, updated_at=ts, requested_by="tony")


def mark_graph_complete(task: Task) -> Task:
    task.current_stage_id = None
    task.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title=task.title, objective=task.description),
        current_stage_id=None,
        blueprint_id="neko_dev_qa_basic",
        stages=[
            MissionPlanStage(
                id="qa_release",
                title="QA Release",
                objective="Verify mission.",
                owner="qa",
                owner_slot="qa",
                repo="hermes-agent",
                kind="qa_verdict",
                status=StageStatus.PASSED,
            )
        ],
    )
    return task


def make_task_with_id(task_id: str):
    task = make_task()
    task.id = task_id
    return task


def test_assignment_spec_exact_goal_proof_targets_outrank_stage_plan():
    from agent_runtime.ticker import _assignment_spec_for_action

    task = make_task_with_id("task_exact_proof_assignment")
    task.state = TaskState.RUNNING
    task.description = "Write docs. Exact proof: `echo e2e-trust-probe`; no Flutter tests."
    task.affected_repos = ["EterniaLauncher"]
    task.current_stage_id = "implement"
    task.stages = [
        TaskStage(
            id="implement",
            title="Launcher Implementation",
            objective="Patch launcher docs.",
            status=StageStatus.IMPLEMENTING,
            test_plan=["flutter analyze lib/features/mission_control", "flutter test test/features/mission_control"],
        )
    ]

    spec = _assignment_spec_for_action(
        HarnessAction(HarnessActionType.RUN_SLOT, task.id, slot_id="dev"),
        task,
        persona_id="dev",
    )

    assert spec.proof_targets == ["echo e2e-trust-probe"]


def test_assignment_spec_exact_prose_goal_proof_targets_outrank_stage_plan():
    from agent_runtime.ticker import _assignment_spec_for_action

    task = make_task_with_id("task_exact_prose_proof_assignment")
    task.state = TaskState.RUNNING
    task.description = (
        "Write docs/scratch/e2e_trust_probe.md. "
        "The final Harness-owned command proof must run exactly: echo e2e-trust-probe. "
        "Do not run Flutter analyze/tests."
    )
    task.affected_repos = ["EterniaLauncher"]
    task.current_stage_id = "implement"
    task.stages = [
        TaskStage(
            id="implement",
            title="Launcher Implementation",
            objective="Patch launcher docs.",
            status=StageStatus.IMPLEMENTING,
            test_plan=["flutter analyze lib/features/mission_control", "flutter test test/features/mission_control"],
        )
    ]

    spec = _assignment_spec_for_action(
        HarnessAction(HarnessActionType.RUN_SLOT, task.id, slot_id="dev"),
        task,
        persona_id="dev",
    )

    assert spec.proof_targets == ["echo e2e-trust-probe"]


def test_assignment_spec_bundle_proof_targets_outrank_stage_plan(isolate_agent_runtime_root):
    from agent_runtime.ticker import _assignment_spec_for_action

    task = make_task_with_id("task_bundle_proof_assignment")
    task.state = TaskState.RUNNING
    task.current_stage_id = "implement"
    task.stages = [
        TaskStage(
            id="implement",
            title="Harness Implementation",
            objective="Patch harness docs.",
            status=StageStatus.IMPLEMENTING,
            test_plan=["python -m pytest tests/agent_runtime -q"],
        )
    ]
    bundle = RepoBundle(
        id="bundle_harness",
        task_id=task.id,
        repo="hermes-agent",
        owner_persona_id="dev",
        state="assigned",
        title="Harness bundle",
        objective="Patch harness docs.",
        stage_ids=["implement"],
        proof_targets=["python -m pytest tests/agent_runtime/test_stream.py -q"],
        created_at=now(),
        updated_at=now(),
    )
    RepoBundleStore().update(bundle)

    spec = _assignment_spec_for_action(
        HarnessAction(HarnessActionType.RUN_SLOT, task.id, slot_id="dev"),
        task,
        persona_id="dev",
        repo_bundle_id=bundle.id,
    )

    assert spec.proof_targets == ["python -m pytest tests/agent_runtime/test_stream.py -q"]


def test_assignment_spec_carries_upstream_handoff_steer_to_receiver_chat(isolate_agent_runtime_root):
    from agent_runtime.context_builder import build_context
    from agent_runtime.persona_assignments import PersonaAssignmentStore
    from agent_runtime.ticker import _assignment_spec_for_action

    task = make_task_with_id("task_handoff_steer_assignment")
    task.state = TaskState.RUNNING
    task.current_stage_id = "qa_release"
    task.proof_ids = ["proof_impl_gate"]
    task.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title="Handoff steer", objective="Verify the receiver gets upstream context."),
        current_stage_id="qa_release",
        stages=[
            MissionPlanStage(
                id="implement",
                title="Implementation",
                objective="Patch the scoped file.",
                owner="dev",
                repo="hermes-agent",
                kind="implementation",
                status=StageStatus.PASSED,
                proof_ids=["proof_impl_gate"],
            ),
            MissionPlanStage(
                id="qa_release",
                title="QA Release",
                objective="Verify the implementation proof.",
                owner="qa",
                owner_slot="qa",
                repo="hermes-agent",
                kind="qa_verdict",
                status=StageStatus.READY,
            ),
        ],
    )
    runs = RunStore()
    dev_run = runs.open_run("dev", task.id, stage_id="implement")
    runs.close_run(
        dev_run.id,
        state=RunState.COMPLETED,
        final_decision={"type": "hand_off", "summary": "Changed the scoped file and ran focused proof."},
    )

    spec = _assignment_spec_for_action(
        HarnessAction(HarnessActionType.RUN_SLOT, task.id, slot_id="qa", stage_id="qa_release"),
        task,
        persona_id="qa",
    )

    assert "Upstream handoff steer:" in spec.message
    assert "from: dev / stage implement / decision hand_off" in spec.message
    assert "proof_refs: proof_impl_gate" in spec.message
    assert "emit qa_verdict" in spec.message

    assignment = PersonaAssignmentStore().create_or_resume(spec)
    qa_run = runs.open_run("qa", task.id, stage_id="qa_release")
    qa_run.progress = {"assignment_id": assignment.id}
    runs.update(qa_run)
    hud = build_context(task, qa_run).mission_hud

    current_assignment = hud["agent_hud"]["current_assignment"]
    assert current_assignment["assignment_message"] == spec.message
    assert "Upstream handoff steer:" in current_assignment["assignment_message"]


def attach_blueprint(task: Task, blueprint_id: str, bindings: dict[str, str]) -> Task:
    bp = BlueprintStore().get(blueprint_id)
    task.mission_plan = instantiate_blueprint(bp, goal=task.description or task.title, bindings=bindings)
    task.current_stage_id = task.mission_plan.current_stage_id
    return task


def graph_stage(task: Task, stage_id: str) -> MissionPlanStage:
    return next(stage for stage in task.mission_plan.stages if stage.id == stage_id)


def graph_stage_status(task: Task, stage_id: str) -> StageStatus:
    return graph_stage(task, stage_id).status


def test_tick_advances_created_task_with_fake_pm():
    ts=TaskStore(); ts.create(make_task())
    res=TickEngine(task_store=ts, persona_runtime=FakeRuntime()).tick_once()
    assert res.actions_taken[0].ok
    assert res.actions_taken[0].action.type == HarnessActionType.RUN_SLOT
    assert ts.get("task_1").state == TaskState.RUNNING


def test_targeted_tick_runs_named_task_instead_of_older_open_task():
    ts = TaskStore()
    ts.create(make_task_with_id("task_0_old"))
    ts.create(make_task_with_id("task_z_target"))

    res = TickEngine(task_store=ts, persona_runtime=FakeRuntime()).tick_once(task_id="task_z_target")

    assert [action.payload["run_id"] for action in res.actions_taken]
    assert ts.get("task_z_target").state == TaskState.RUNNING
    assert ts.get("task_0_old").state == TaskState.CREATED
    assert res.tasks_seen == 1


def test_targeted_tick_rejects_missing_task_id():
    ts = TaskStore()
    ts.create(make_task_with_id("task_0_old"))

    with pytest.raises(NotFound):
        TickEngine(task_store=ts, persona_runtime=FakeRuntime()).tick_once(task_id="task_missing")


class InspectRunMetadataRuntime:
    def __init__(self, run_store: RunStore):
        self.run_store = run_store
        self.llm_seen = None

    def run_tick(self, persona, ctx, *, run):
        self.llm_seen = self.run_store.get(run.id).llm
        from agent_runtime.decision_schema import AgentDecision, DecisionType
        return AgentDecision(type=DecisionType.PROPOSE_ACCEPTANCE, summary="pm", rationale="r", payload={"objective":"obj","acceptance_criteria":["ok"]})


def test_tick_persists_active_run_provider_model_metadata_before_runtime_call():
    ts = TaskStore(); ts.create(make_task())
    runs = RunStore()
    runtime = InspectRunMetadataRuntime(runs)
    cfg = AgentRuntimeConfig(default_provider="openai-codex", default_model="gpt-5.5")

    res = TickEngine(task_store=ts, run_store=runs, persona_runtime=runtime, config=cfg).tick_once()

    assert res.actions_taken[0].ok
    assert runtime.llm_seen["provider"] == "openai-codex"
    assert runtime.llm_seen["model"] == "gpt-5.5"
    assert runtime.llm_seen["retry_attempt"] == 1
    assert runtime.llm_seen["retry_max_attempts"] == 1


def test_tick_uses_persona_specific_live_budget_for_dev_runs():
    task = make_task()
    task.state = TaskState.RUNNING
    ts = TaskStore(); ts.create(task)
    runs = RunStore()
    runtime = FakeRuntime()
    cfg = AgentRuntimeConfig(
        default_provider="openai-codex",
        default_model="gpt-5.5",
        live_run_iteration_budget=60,
        live_run_max_wall_seconds=300,
        live_run_max_api_calls=20,
        live_run_max_total_tokens=750000,
        personas={
            "dev": {
                "iteration_budget": 6,
                "max_wall_seconds": 180,
                "max_api_calls": 6,
                "max_total_tokens": 250000,
            }
        },
    )

    res = TickEngine(task_store=ts, run_store=runs, persona_runtime=runtime, config=cfg).tick_once()

    assert res.actions_taken[0].ok
    run = runs.list_for_task(task.id)[0]
    assert run.persona_id == "dev"
    assert run.iteration_budget == 6
    assert run.max_wall_seconds == 180
    assert run.max_api_calls == 6
    assert run.max_total_tokens == 250000


class CancelRunDuringRuntime:
    def __init__(self, run_store: RunStore, task_store: TaskStore | None = None, *, raise_after_cancel: bool = False):
        self.run_store = run_store
        self.task_store = task_store
        self.raise_after_cancel = raise_after_cancel

    def run_tick(self, persona, ctx, *, run):
        if self.task_store is not None:
            self.task_store.cancel(run.task_id, reason="operator stopped in-flight task", actor="alice")
        self.run_store.cancel(run.id, reason="operator stopped in-flight run")
        if self.raise_after_cancel:
            raise RuntimeError("runtime emitted after operator cancellation")
        from agent_runtime.decision_schema import AgentDecision, DecisionType
        return AgentDecision(type=DecisionType.PROPOSE_ACCEPTANCE, summary="stale decision", rationale="r", payload={"objective":"obj","acceptance_criteria":["ok"]})


def test_tick_does_not_apply_decision_after_run_cancelled_during_runtime():
    ts = TaskStore(); ts.create(make_task())
    runs = RunStore()
    runtime = CancelRunDuringRuntime(runs)

    res = TickEngine(task_store=ts, run_store=runs, persona_runtime=runtime).tick_once()

    assert not res.actions_taken[0].ok
    assert res.actions_taken[0].payload["run_id"]
    assert runs.list_for_task("task_1")[0].state == RunState.CANCELLED
    assert ts.get("task_1").state == TaskState.CREATED


def test_tick_does_not_overwrite_task_cancelled_during_runtime():
    ts = TaskStore(); ts.create(make_task())
    runs = RunStore()
    runtime = CancelRunDuringRuntime(runs, task_store=ts)

    res = TickEngine(task_store=ts, run_store=runs, persona_runtime=runtime).tick_once()

    assert not res.actions_taken[0].ok
    assert runs.list_for_task("task_1")[0].state == RunState.CANCELLED
    assert ts.get("task_1").state == TaskState.CANCELLED


def test_tick_exception_path_does_not_overwrite_run_cancelled_during_runtime():
    ts = TaskStore(); ts.create(make_task())
    runs = RunStore()
    runtime = CancelRunDuringRuntime(runs, raise_after_cancel=True)

    res = TickEngine(task_store=ts, run_store=runs, persona_runtime=runtime).tick_once()

    assert not res.actions_taken[0].ok
    assert runs.list_for_task("task_1")[0].state == RunState.CANCELLED
    assert ts.get("task_1").state == TaskState.CREATED


def test_tick_uses_configured_persona_when_agent_store_empty():
    ts=TaskStore(); ts.create(make_task())
    runtime = FakeRuntime()
    cfg = AgentRuntimeConfig(personas={"neko_supervisor": {"hermes_profile": "alice", "skills": ["agent-runtime-harness"]}})
    res=TickEngine(task_store=ts, persona_runtime=runtime, config=cfg).tick_once()
    assert res.actions_taken[0].ok
    assert runtime.personas[0].id == "neko_supervisor"
    assert runtime.personas[0].hermes_profile == "alice"
    assert runtime.personas[0].skills == ["agent-runtime-harness"]


def test_tick_resolves_blueprint_run_slot_through_binding():
    ts = TaskStore()
    bp = BlueprintStore().get("one_agent_smoke")
    plan = instantiate_blueprint(bp, goal="smoke", bindings={"builder": "persona:neko_supervisor"})
    task = make_task_with_id("task_blueprint_slot")
    task.mission_plan = plan
    task.current_stage_id = plan.current_stage_id
    ts.create(task)
    runtime = CapturingFailureRuntime()

    res = TickEngine(task_store=ts, persona_runtime=runtime).tick_once(task_id=task.id)

    assert not res.actions_taken[0].ok
    assert res.actions_taken[0].action.type == HarnessActionType.RUN_SLOT
    assert runtime.personas == ["neko_supervisor"]


def test_blueprint_tick_runs_slot_collects_proof_and_completes():
    ts = TaskStore()
    proof_store = ProofStore()
    bp = BlueprintStore().get("one_agent_smoke")
    plan = instantiate_blueprint(bp, goal="smoke", bindings={"builder": "persona:dev"})
    task = make_task_with_id("task_blueprint_smoke_tick")
    task.mission_plan = plan
    task.current_stage_id = plan.current_stage_id
    ts.create(task)
    runtime = BlueprintSmokeRuntime()
    engine = TickEngine(
        task_store=ts,
        proof_store=proof_store,
        persona_runtime=runtime,
        proof_runner=PassingProofRunner(proof_store),
    )

    result = engine.run_until_settled(task_id=task.id, max_actions=2)

    saved = ts.get(task.id)
    assert result.stop_reason == "task_terminal"
    assert saved.state == TaskState.DONE
    assert runtime.personas == ["dev"]
    assert saved.mission_plan.current_stage_id is None
    assert saved.mission_plan.stage_attempts == {"build": 1}
    assert saved.proof_ids


def test_invalid_persona_output_closes_incident_without_intervention_loop():
    ts=TaskStore(); ts.create(make_task())
    engine=TickEngine(task_store=ts, persona_runtime=BadRuntime())
    res=engine.tick_once()
    assert not res.actions_taken[0].ok
    task = ts.get("task_1")
    assert task.state == TaskState.RUNNING
    assert task.open_incident_ids == []
    assert engine.incident_store.list_open() == []
    incidents = engine.incident_store.list_all()
    assert len(incidents) == 1
    assert incidents[0].kind == "model_invalid_output"
    assert incidents[0].closed_at is not None


def test_live_dev_tick_emits_preflight_started_before_dispatch():
    ts = TaskStore()
    task = make_task()
    task.id = "task_preflight_event"
    task.state = TaskState.RUNNING
    ts.create(task)

    TickEngine(task_store=ts, persona_runtime=GPTPersonaRuntime()).tick_once(task_id=task.id)
    events = EventLog().for_task(task.id, limit=20)
    event_types = [event.type for event in events]

    assert "task.preflight" in event_types
    assert event_types.index("task.preflight") < event_types.index("run.opened")
    preflight_event = next(event for event in events if event.type == "task.preflight")
    assert preflight_event.payload["status"] == "started"
    assert preflight_event.payload["persona_target"] == "dev"


def test_invalid_persona_output_preserves_progress_written_by_live_runner():
    ts = TaskStore()
    runs = RunStore()
    ts.create(make_task())
    engine = TickEngine(task_store=ts, run_store=runs, persona_runtime=ProgressThenInvalidRuntime())

    res = engine.tick_once()

    assert not res.actions_taken[0].ok
    run = runs.list_for_task("task_1")[0]
    assert run.state == RunState.FAILED
    assert run.progress["autonomy_packet_id"] == "auto_live"
    assert run.progress["context_receipt_id"] == "ctxr_live"
    assert run.llm["api_calls"] == 1
    assert run.llm["total_tokens"] == 42


def test_next_run_gets_previous_model_invalid_output_repair_context():
    ts = TaskStore()
    runs = RunStore()
    ts.create(make_task())
    failed = runs.open_run("neko_supervisor", "task_1")
    failed.llm = {"validation_status": "invalid"}
    runs.update(failed)
    runs.close_run(
        failed.id,
        state=RunState.FAILED,
        error={"message": "qa_review has unsupported keys: ['notes']", "retryable": False},
    )
    runtime = CaptureRepairContextRuntime()

    res = TickEngine(task_store=ts, run_store=runs, persona_runtime=runtime).tick_once(task_id="task_1")

    assert res.actions_taken[0].ok
    assert runtime.contexts[0].requires_repair is True
    assert "qa_review has unsupported keys" in runtime.contexts[0].repair_error


def test_invalid_packet_contract_returns_repair_hud_inside_same_run():
    ts = TaskStore()
    runs = RunStore()
    incidents = IncidentStore()
    proof_store = ProofStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    task.stages = [
        TaskStage(
            id="stage_1",
            title="Command proof",
            objective="Collect focused command proof.",
            status=StageStatus.READY,
            acceptance_criteria=["proof attached"],
            test_plan=["echo stage48-ok"],
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
    ]
    ts.create(task)
    runtime = InvalidDeliveryThenCorrectRuntime()

    engine = TickEngine(
        task_store=ts,
        run_store=runs,
        incident_store=incidents,
        proof_store=proof_store,
        persona_runtime=runtime,
        proof_runner=PassingProofRunner(proof_store),
    )
    res = engine.tick_once(task_id="task_1")

    assert res.actions_taken[0].ok
    assert len(runtime.contexts) == 2
    assert runtime.contexts[0].requires_repair is False
    repair_ctx = runtime.contexts[1]
    assert repair_ctx.requires_repair is True
    assert "delivery.next_owner is invalid" in repair_ctx.repair_error
    repair_hud = repair_ctx.mission_hud["validation_repair"]
    assert repair_hud["status"] == "invalid_previous_decision"
    assert repair_hud["invalid_field"] == "payload.delivery.next_owner"
    assert repair_hud["invalid_value"] == "backend-dev"
    assert "neko_supervisor" in repair_hud["allowed_values"]
    assert repair_hud["repair_attempt"] == 1

    run = runs.list_for_task("task_1")[0]
    assert run.state == RunState.COMPLETED
    assert run.llm["validation_status"] == "valid"
    assert run.llm["schema_repair_attempts"] == 1
    assert ts.get("task_1").state == TaskState.RUNNING
    assert proof_store.get("proof_task_1_stage_1").metadata["status"] == "passed"
    assert not engine.incident_store.list_open()
    progress_events = [
        event.payload
        for event in EventLog().for_task("task_1", limit=0)
        if event.type == "run.progress" and (event.payload or {}).get("source") == "decision_contract_repair"
    ]
    assert progress_events
    assert progress_events[-1]["next_expected"] == "corrected_agent_decision"


def test_persona_runtime_raised_contract_error_returns_repair_hud_inside_same_run():
    ts = TaskStore()
    runs = RunStore()
    incidents = IncidentStore()
    ts.create(make_task())
    runtime = RaisedInvalidHandoffThenCorrectRuntime()

    res = TickEngine(task_store=ts, run_store=runs, incident_store=incidents, persona_runtime=runtime).tick_once(task_id="task_1")

    assert res.actions_taken[0].ok
    assert len(runtime.contexts) == 2
    assert runtime.contexts[0].requires_repair is False
    repair_ctx = runtime.contexts[1]
    assert repair_ctx.requires_repair is True
    assert "handoff_packet.handoff_mode is required" in repair_ctx.repair_error
    repair_hud = repair_ctx.mission_hud["validation_repair"]
    assert repair_hud["status"] == "invalid_previous_decision"
    assert repair_hud["invalid_field"] == "payload.handoff_packet.handoff_mode"
    assert "sequential_specialists" in repair_hud["allowed_values"]
    assert repair_hud["recommended_value"] == "sequential_specialists"

    run = runs.list_for_task("task_1")[0]
    assert run.state == RunState.COMPLETED
    assert run.llm["validation_status"] == "valid"
    assert run.llm["schema_repair_attempts"] == 1
    assert not incidents.list_open()
    assert ts.get("task_1").state == TaskState.RUNNING


def test_visual_screenshot_contract_repair_event_names_exact_invalid_field():
    from agent_runtime.decision_schema import DecisionPayloadInvalid
    from agent_runtime.ticker import _decision_repair_feedback, _decision_repair_progress_payload

    error = DecisionPayloadInvalid("missing payload keys: ['target']")

    repair_error = _decision_repair_feedback(error, decision=None, repair_attempt=1)
    progress = _decision_repair_progress_payload(repair_error, repair_attempt=1)

    assert progress["invalid_field"] == "payload.target"
    assert progress["summary"] == "missing payload keys: ['target']"
    assert progress["next_expected"] == "corrected_agent_decision"


def test_qa_review_coverage_repair_event_names_exact_invalid_field():
    from agent_runtime.decision_schema import DecisionPayloadInvalid
    from agent_runtime.ticker import _decision_repair_feedback, _decision_repair_progress_payload

    error = DecisionPayloadInvalid("qa_review.coverage missing ['backend_contract']")

    repair_error = _decision_repair_feedback(error, decision=None, repair_attempt=1)
    progress = _decision_repair_progress_payload(repair_error, repair_attempt=1)

    assert progress["invalid_field"] == "payload.qa_review.coverage"
    assert progress["summary"] == "qa_review.coverage missing ['backend_contract']"


class DuplicateScreenshotThenVerdictRuntime:
    def __init__(self):
        self.contexts = []

    def run_tick(self, persona, ctx, *, run):
        self.contexts.append(ctx)
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        if len(self.contexts) == 1:
            return AgentDecision(
                type=DecisionType.REQUEST_SCREENSHOT,
                summary="recapture visual proof",
                rationale="Mission Control needs visual proof.",
                payload={
                    "stage_id": "stage_1",
                    "target": "mission_control",
                    "proof_requirement": "Mission Control is not a spinner.",
                    "mcp_server": "launcher_qa",
                    "required_launch_pins": {"hermes_profile": "alice", "runtime_root_id": "agent-runtime"},
                },
            )
        return AgentDecision(
            type=DecisionType.QA_VERDICT,
            summary="QA approved existing visual proof",
            rationale="The existing command and screenshot proof IDs cover the implementation claim.",
            payload={
                "verdict": "approved",
                "proof_ids": list(ctx.proof_ids),
                "findings": [],
            },
        )


def test_duplicate_visual_request_repairs_to_verdict_instead_of_recapturing():
    ts = TaskStore()
    runs = RunStore()
    proof_store = ProofStore()
    task = make_task()
    task.state = TaskState.BLOCKED
    task.current_stage_id = "stage_1"
    task.requires_visual_proof = True
    task.risk_flags = []
    task.stages = [
        TaskStage(
            id="stage_1",
            title="Launcher visual fix",
            objective="Prove Mission Control is usable.",
            status=StageStatus.READY_FOR_QA,
            acceptance_criteria=["Mission Control is not an infinite spinner."],
            test_plan=["flutter test mission_control"],
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
    ]
    command_proof = Proof(
        id="proof_command",
        task_id=task.id,
        stage_id="stage_1",
        type=ProofType.TEST_RUN,
        title="command proof",
        path_or_value="command.log",
        created_by="harness",
        created_at=task.created_at,
        metadata={"status": "passed", "exit_code": 0, "command": "flutter test mission_control"},
        redaction_status="safe",
    )
    staging_proof = Proof(
        id="proof_staging_k8",
        task_id=task.id,
        stage_id="stage_1",
        type=ProofType.TEST_RUN,
        title="staging k8 proof",
        path_or_value="staging.log",
        created_by="harness",
        created_at=task.created_at + timedelta(minutes=1),
        metadata={"status": "passed", "command": "kubectl -n staging rollout status deploy/eternia-launcher"},
        redaction_status="safe",
    )
    prod_proof = Proof(
        id="proof_prod_rollout",
        task_id=task.id,
        stage_id="stage_1",
        type=ProofType.TEST_RUN,
        title="prod pod proof",
        path_or_value="prod.log",
        created_by="harness",
        created_at=task.created_at + timedelta(minutes=2),
        metadata={"status": "passed", "command": "kubectl -n prod rollout status deploy/eternia-launcher", "proof_intent": "prod_rollout"},
        redaction_status="safe",
    )
    screenshot_proof = Proof(
        id="proof_screenshot",
        task_id=task.id,
        stage_id="stage_1",
        type=ProofType.SCREENSHOT,
        title="screenshot proof",
        path_or_value="proofs/task_1/artifacts/mission_control.png",
        created_by="harness",
        created_at=task.created_at + timedelta(minutes=3),
        metadata={"status": "passed", "target": "mission_control", "artifact_exists": True},
        redaction_status="safe",
    )
    proof_store.attach(command_proof)
    proof_store.attach(staging_proof)
    proof_store.attach(prod_proof)
    proof_store.attach(screenshot_proof)
    task.proof_ids = [command_proof.id, staging_proof.id, prod_proof.id, screenshot_proof.id]
    ts.create(task)
    runtime = DuplicateScreenshotThenVerdictRuntime()

    res = TickEngine(task_store=ts, run_store=runs, proof_store=proof_store, persona_runtime=runtime).tick_once(task_id=task.id)

    assert res.actions_taken[0].ok
    assert len(runtime.contexts) == 2
    assert runtime.contexts[1].requires_repair is True
    assert "matching visual proof already exists" in runtime.contexts[1].repair_error
    run = runs.list_for_task(task.id)[0]
    assert run.state == RunState.COMPLETED
    assert run.llm["schema_repair_attempts"] == 1
    assert run.final_decision["type"] == "qa_verdict"
    saved = ts.get(task.id)
    assert saved.state == TaskState.RUNNING
    assert saved.stages[0].status == StageStatus.PASSED
    assert [proof.id for proof in proof_store.list_for_task(task.id) if proof.type == ProofType.SCREENSHOT] == ["proof_screenshot"]


def test_repeated_invalid_packet_contract_escalates_after_bounded_repair():
    ts = TaskStore()
    runs = RunStore()
    proof_store = ProofStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    task.stages = [
        TaskStage(
            id="stage_1",
            title="Command proof",
            objective="Collect focused command proof.",
            status=StageStatus.READY,
            acceptance_criteria=["proof attached"],
            test_plan=["echo stage48-bad"],
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
    ]
    ts.create(task)
    runtime = RepeatedInvalidDeliveryRuntime()
    incidents = IncidentStore()

    res = TickEngine(
        task_store=ts,
        run_store=runs,
        incident_store=incidents,
        proof_store=proof_store,
        persona_runtime=runtime,
        proof_runner=PassingProofRunner(proof_store),
    ).tick_once(task_id="task_1")

    assert not res.actions_taken[0].ok
    assert len(runtime.contexts) == 2
    assert runtime.contexts[1].requires_repair is True
    run = runs.list_for_task("task_1")[0]
    assert run.state == RunState.FAILED
    assert run.error["class"] == "DecisionPayloadInvalid"
    assert run.error["message"] == "delivery.next_owner is invalid"
    assert run.llm["validation_status"] == "invalid"
    assert incidents.list_open() == []
    all_incidents = incidents.list_all()
    assert len(all_incidents) == 1
    assert all_incidents[0].summary == "delivery.next_owner is invalid"
    assert all_incidents[0].closed_at is not None
    assert ts.get("task_1").state == TaskState.RUNNING


def test_role_session_observe_only_records_would_continue_without_second_invocation():
    ts = TaskStore()
    runs = RunStore()
    proof_store = ProofStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    task.stages = [
        TaskStage(
            id="stage_1",
            title="Implementation",
            objective="Keep same Dev owner",
            status=StageStatus.READY,
            acceptance_criteria=["proof"],
            test_plan=["printf ok"],
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
    ]
    ts.create(task)
    runtime = ContinuousRoleSessionDevRuntime()
    cfg = AgentRuntimeConfig(continuous_role_sessions=ContinuousRoleSessionConfig(enabled=False, observe_only=True))

    res = TickEngine(task_store=ts, run_store=runs, proof_store=proof_store, persona_runtime=runtime, config=cfg).tick_once(task_id="task_1")

    assert res.actions_taken[0].ok
    assert len(runtime.calls) == 1
    events = [event for event in EventLog().for_task("task_1", limit=0) if event.type.startswith("role_session.")]
    assert [event.type for event in events] == ["role_session.opened", "role_session.closed"]
    assert events[-1].payload["close_reason"] == "observe_only"
    assert events[-1].payload["would_continue"] is True


def test_continuous_role_session_runs_two_same_owner_decisions_in_one_run():
    ts = TaskStore()
    runs = RunStore()
    proof_store = ProofStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    task.stages = [
        TaskStage(
            id="stage_1",
            title="Implementation",
            objective="Keep same Dev owner",
            status=StageStatus.READY,
            acceptance_criteria=["proof"],
            test_plan=["printf ok"],
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
    ]
    ts.create(task)
    runtime = ContinuousRoleSessionDevRuntime()
    cfg = AgentRuntimeConfig(continuous_role_sessions=ContinuousRoleSessionConfig(enabled=True, observe_only=False))

    res = TickEngine(
        task_store=ts,
        run_store=runs,
        proof_store=proof_store,
        persona_runtime=runtime,
        proof_runner=PassingProofRunner(proof_store),
        config=cfg,
    ).tick_once(task_id="task_1")

    assert res.actions_taken[0].ok
    assert len(runtime.calls) == 2
    assert len({call["run_id"] for call in runtime.calls}) == 1
    run = runs.list_for_task("task_1")[0]
    assert run.state == RunState.COMPLETED
    assert run.progress["role_session"]["decision_count"] == 2
    assert run.progress["role_session"]["continuation_count"] == 1
    assert res.actions_taken[0].payload["role_session"]["decision_count"] == 2
    events = [event for event in EventLog().for_task("task_1", limit=0) if event.type.startswith("role_session.")]
    assert [event.type for event in events] == ["role_session.opened", "role_session.continued", "role_session.closed"]
    assert events[-1].payload["close_reason"] == "owner_change"
    assert proof_store.get("proof_task_1_stage_1").metadata["status"] == "passed"


def test_dev_missing_proof_handoff_routes_to_neko_repair_instead_of_dead_skip():
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    task.stages = [
        TaskStage(
            id="stage_1",
            title="RED tests",
            objective="Add failing tests first",
            status=StageStatus.READY,
            acceptance_criteria=["red proof exists"],
            test_plan=["pytest focused test"],
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
    ]
    ts.create(task)
    engine = TickEngine(task_store=ts, persona_runtime=MissingProofHandoffRuntime())

    res = engine.tick_once(task_id="task_1")

    assert not res.actions_taken[0].ok
    task = ts.get("task_1")
    assert task.state == TaskState.RUNNING
    assert task.open_incident_ids == []
    assert engine.incident_store.list_open() == []
    assert engine.state_machine.next_action(task).type == HarnessActionType.RUN_SLOT


def test_latest_session_id_ignores_invalid_failed_run_to_avoid_repeating_bad_handoff():
    runs = RunStore()
    completed = runs.open_run("dev", "task_1", stage_id="stage_1", session_id="20260601_000000_good")
    runs.close_run(completed.id, state=RunState.COMPLETED, final_decision={"type": "propose_stage_plan"})
    failed = runs.open_run("dev", "task_1", stage_id="stage_1", session_id="20260601_000000_bad")
    failed.llm = {"validation_status": "invalid"}
    runs.update(failed)
    runs.close_run(failed.id, state=RunState.FAILED, error={"class": "DecisionPayloadInvalid"})

    assert runs.latest_session_id(task_id="task_1", persona_id="dev", stage_id="stage_1") is None


def test_missing_provider_dependency_opens_runtime_dependency_incident():
    ts=TaskStore(); ts.create(make_task())
    engine=TickEngine(task_store=ts, persona_runtime=MissingProviderDependencyRuntime())

    res=engine.tick_once()

    assert not res.actions_taken[0].ok
    incident = engine.incident_store.list_open()[0]
    assert incident.kind == "runtime_dependency_missing"
    assert "openai" in incident.summary
    assert ts.get("task_1").state == TaskState.CREATED


def test_transient_provider_ttfb_records_single_attempt_incident():
    ts = TaskStore(); ts.create(make_task())
    runtime = TransientProviderRuntime()
    engine = TickEngine(task_store=ts, persona_runtime=runtime)

    res = engine.tick_once()

    assert not res.actions_taken[0].ok
    assert runtime.calls == 1
    incidents = engine.incident_store.list_open()
    assert len(incidents) == 1
    assert incidents[0].kind == "provider_failure"
    assert ts.get("task_1").state == TaskState.CREATED
    runs = sorted(engine.run_store.list_for_task("task_1"), key=lambda run: run.started_at)
    assert [run.state.value for run in runs] == ["failed"]
    assert runs[0].error["retryable"] is False
    assert runs[0].llm["retry_attempt"] == 1
    assert res.actions_taken[0].payload["attempts"] == 1


def test_tick_skips_mission_with_open_incident():
    ts=TaskStore(); ts.create(make_task())
    incidents = IncidentStore()
    incidents.open(Incident(id="inc_1", task_id="task_1", run_id=None, kind="provider_failure", summary="auth", detail_path=None, opened_at=now()))
    runtime = FakeRuntime()

    res=TickEngine(task_store=ts, incident_store=incidents, persona_runtime=runtime).tick_once()

    assert res.actions_taken == []
    assert res.skipped == ["task_1"]
    assert runtime.personas == []


def test_run_until_settled_closes_model_invalid_output_without_neko_loop():
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.BLOCKED
    task.open_incident_ids = ["inc_1"]
    ts.create(task)
    incidents = IncidentStore()
    incidents.open(Incident(id="inc_1", task_id="task_1", run_id=None, kind="model_invalid_output", summary="bad dev handoff", detail_path=None, opened_at=now()))
    engine = TickEngine(task_store=ts, incident_store=incidents, persona_runtime=ResolveIncidentRuntime())

    res = engine.run_until_settled(task_id="task_1", max_actions=1)

    assert res.actions_taken == []
    assert res.stop_reason == "no_eligible_action"
    assert incidents.list_open() == []
    assert incidents.get("inc_1").closed_at is not None
    assert ts.get("task_1").state == TaskState.RUNNING
    assert ts.get("task_1").open_incident_ids == []
    closed_events = [event for event in EventLog().for_task("task_1", limit=0) if event.type == "incident.closed"]
    assert closed_events[-1].payload["reason"] == "contract_simplification_normalized_model_invalid_output"


def test_run_until_settled_stops_on_open_environment_blocker_without_neko_retry():
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.BLOCKED
    task.open_incident_ids = ["inc_visual"]
    ts.create(task)
    incidents = IncidentStore()
    incidents.open(
        Incident(
            id="inc_visual",
            task_id="task_1",
            run_id="run_qa",
            kind="environment_blocker",
            summary="visual capture failed: StageCMcpError: launcher_qa MCP timed out",
            detail_path=None,
            opened_at=now(),
            metadata={"check_id": "launcher_qa_mcp", "proof_id": "visual_blocker_1"},
        )
    )
    runtime = FakeRuntime()
    engine = TickEngine(task_store=ts, incident_store=incidents, persona_runtime=runtime)

    res = engine.run_until_settled(task_id="task_1", max_actions=3)

    assert res.stop_reason == "incident_opened"
    assert res.actions_taken == []
    assert runtime.personas == []


def test_tick_skips_open_environment_blocker_without_neko_dispatch():
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.BLOCKED
    task.open_incident_ids = ["inc_visual"]
    ts.create(task)
    incidents = IncidentStore()
    incidents.open(
        Incident(
            id="inc_visual",
            task_id="task_1",
            run_id="run_qa",
            kind="environment_blocker",
            summary="visual capture failed: StageCMcpError",
            detail_path=None,
            opened_at=now(),
            metadata={"check_id": "launcher_qa_mcp"},
        )
    )
    runtime = FakeRuntime()

    res = TickEngine(task_store=ts, incident_store=incidents, persona_runtime=runtime).tick_once(task_id="task_1")

    assert res.actions_taken == []
    assert res.skipped == ["task_1"]
    assert runtime.personas == []


def test_run_until_settled_does_not_treat_blocked_task_as_boundary_when_recovery_action_exists():
    ts = TaskStore()
    proofs = ProofStore()
    task = make_task()
    task.state = TaskState.BLOCKED
    task.open_incident_ids = []
    task.proof_ids = ["proof_qa"]
    ts.create(task)
    proofs.attach(
        Proof(
            id="proof_qa",
            task_id="task_1",
            stage_id=None,
            type=ProofType.QA_VERDICT,
            title="QA",
            path_or_value="blocked",
            created_by="qa",
            created_at=now(),
            metadata={"verdict": "blocked", "findings": [{"kind": "open_incidents", "severity": "blocking"}]},
        )
    )
    engine = TickEngine(task_store=ts, proof_store=proofs, persona_runtime=FakeRuntime())

    assert engine._settled_boundary(task_id="task_1") is None


def test_model_invalid_output_incident_closes_before_neko_steering():
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.BLOCKED
    task.open_incident_ids = ["inc_1"]
    ts.create(task)
    incidents = IncidentStore()
    incidents.open(Incident(id="inc_1", task_id="task_1", run_id="run_bad", kind="model_invalid_output", summary="proof_ids are required", detail_path=None, opened_at=now()))
    runtime = InspectIncidentContextRuntime()

    res = TickEngine(task_store=ts, incident_store=incidents, persona_runtime=runtime).tick_once(task_id="task_1")

    assert res.actions_taken == []
    assert runtime.incident_records is None
    assert incidents.list_open() == []
    assert incidents.get("inc_1").closed_at is not None
    assert ts.get("task_1").open_incident_ids == []


def test_tick_skips_task_with_existing_active_run():
    ts = TaskStore(); ts.create(make_task())
    runs = RunStore()
    existing = runs.open_run("neko_supervisor", "task_1", stage_id=None)
    runtime = FakeRuntime()

    res = TickEngine(task_store=ts, run_store=runs, persona_runtime=runtime).tick_once()

    assert res.actions_taken == []
    assert res.skipped == ["task_1"]
    assert runtime.personas == []
    assert runs.get(existing.id).state == RunState.RUNNING
    assert len(runs.list_for_task("task_1")) == 1


def test_tick_allows_new_run_after_existing_run_is_terminal():
    ts = TaskStore(); ts.create(make_task())
    runs = RunStore()
    existing = runs.open_run("neko_supervisor", "task_1", stage_id=None)
    runs.close_run(existing.id, state=RunState.COMPLETED)
    runtime = FakeRuntime()

    res = TickEngine(task_store=ts, run_store=runs, persona_runtime=runtime).tick_once()

    assert res.actions_taken[0].ok
    assert runtime.personas[0].id == "neko_supervisor"
    assert len(runs.list_for_task("task_1")) == 2


def test_tick_deterministically_closes_APPROVED_mission_with_proof_without_persona_runtime():
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    mark_graph_complete(task)
    task.proof_ids = ["proof_test", "proof_qa"]
    ts.create(task)

    config = RuntimeConfig(mission_plan=MissionPlanConfig(enabled=True))
    res = TickEngine(task_store=ts, persona_runtime=None, config=config).tick_once()

    assert res.actions_taken[0].ok
    assert res.actions_taken[0].action.type == HarnessActionType.COMPLETE_TASK
    assert res.actions_taken[0].payload == {"state": "done", "proof_ids": 2}
    assert ts.get("task_1").state == TaskState.DONE


def test_tick_closes_typed_mission_when_qa_stage_passed_even_if_top_state_lags():
    ts = TaskStore()
    proofs = ProofStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "harness_runtime_status_snapshot"
    task.proof_ids = ["proof_command", "proof_qa"]
    task.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title="Stage 52 smoke", objective="Verify harness proof flow."),
        current_stage_id="qa_release",
        stages=[
            MissionPlanStage(
                id="harness_runtime_status_snapshot",
                title="Harness Runtime Status Snapshot",
                objective="Verify Harness status and snapshot commands.",
                owner="dev",
                repo="hermes-agent",
                kind="proof_only",
                proof_recipe_id="harness_runtime_status_snapshot",
                blocks_qa_until=True,
                status=StageStatus.PASSED,
                proof_ids=["proof_command"],
            ),
            MissionPlanStage(
                id="qa_release",
                title="QA Release Verdict",
                objective="Verify proof coverage.",
                owner="qa",
                repo="hermes-agent",
                kind="qa_verdict",
                blocks_qa_until=False,
                status=StageStatus.PASSED,
                depends_on=["harness_runtime_status_snapshot"],
                proof_ids=["proof_qa"],
            ),
        ],
    )
    proofs.attach(
        Proof(
            id="proof_command",
            task_id=task.id,
            stage_id="harness_runtime_status_snapshot",
            type=ProofType.TEST_RUN,
            title="Harness command proof",
            path_or_value="proofs/task_1/harness-status.log",
            created_by="harness",
            created_at=task.created_at,
            metadata={"status": "passed", "exit_code": 0},
            redaction_status="safe",
        )
    )
    ts.create(task)

    config = RuntimeConfig(mission_plan=MissionPlanConfig(enabled=True))
    res = TickEngine(task_store=ts, proof_store=proofs, persona_runtime=None, config=config).tick_once()

    assert res.actions_taken[0].ok
    assert res.actions_taken[0].action.type == HarnessActionType.COMPLETE_TASK
    assert res.actions_taken[0].summary == "blueprint has no remaining stages"
    assert ts.get("task_1").state == TaskState.DONE


class NekoResolveRuntime:
    def __init__(self):
        self.personas = []

    def run_tick(self, persona, ctx, *, run):
        self.personas.append(persona)
        from agent_runtime.decision_schema import AgentDecision, DecisionType
        return AgentDecision(
            type=DecisionType.RESOLVE_INCIDENT,
            summary="qa intervention resolved",
            rationale="Neko can steer non-critical QA interventions without Tony making a new goal.",
            payload={"incident_id": "inc_qa", "resolution": "focused proof accepted", "next_state": "pm_ready_for_integration"},
        )


def test_tick_routes_noncritical_intervention_to_neko_supervisor_instead_of_skipping():
    ts=TaskStore(); task=make_task(); task.state=TaskState.BLOCKED; task.open_incident_ids=["inc_qa"]; ts.create(task)
    incidents = IncidentStore()
    incidents.open(Incident(id="inc_qa", task_id="task_1", run_id=None, kind="qa_intervention_required", summary="needs QA steering", detail_path=None, opened_at=now()))
    runtime = NekoResolveRuntime()

    res=TickEngine(task_store=ts, incident_store=incidents, persona_runtime=runtime).tick_once()

    assert res.actions_taken[0].ok
    assert runtime.personas[0].id == "neko_supervisor"
    assert incidents.get("inc_qa").closed_at is not None
    assert ts.get("task_1").state == TaskState.RUNNING
    assert ts.get("task_1").open_incident_ids == []


class NekoNeedsContextRuntime:
    def __init__(self):
        self.personas = []

    def run_tick(self, persona, ctx, *, run):
        self.personas.append(persona)
        from agent_runtime.decision_schema import AgentDecision, DecisionType
        return AgentDecision(
            type=DecisionType.NEEDS_CONTEXT,
            summary="missing referenced task envelopes",
            rationale="Neko cannot safely reconcile without exact referenced task proof.",
            payload={"missing": ["task_envelopes", "proof_context"]},
        )


def test_neko_needs_context_blocks_instead_of_daemon_looping():
    ts=TaskStore(); task=make_task(); task.state=TaskState.RUNNING
    task.context_requests = [
        {"id": "ctx_1", "actor": "qa", "status": "unsupported", "failure_reason": "path_not_found"},
        {"id": "ctx_2", "actor": "qa", "status": "unsupported", "failure_reason": "path_not_found"},
        {"id": "ctx_3", "actor": "qa", "status": "unsupported", "failure_reason": "path_not_found"},
    ]
    ts.create(task)
    runtime = NekoNeedsContextRuntime()
    engine=TickEngine(task_store=ts, persona_runtime=runtime)

    first=engine.tick_once()
    second=engine.tick_once()

    assert first.actions_taken[0].ok
    assert first.actions_taken[0].action.type.value == "run_slot"
    assert first.actions_taken[0].action.slot_id == "neko_supervisor"
    assert runtime.personas[0].id == "neko_supervisor"
    assert ts.get("task_1").state == TaskState.RUNNING
    assert second.actions_taken
    assert len(runtime.personas) >= 1


class RequestTestRunRuntime:
    def run_tick(self, persona, ctx, *, run):
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="run focused tests",
            rationale="Need deterministic command proof before QA handoff.",
            payload={"stage_id": "implement", "commands": ["printf 'proof-ok\\n'"]},
        )


class NormalFlowPatchRuntime:
    def __init__(self):
        self.contexts = []

    def run_tick(self, persona, ctx, *, run):
        self.contexts.append(ctx)
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        # Mirror production `_attach_repo_baseline`: capture a per-run baseline so
        # the harness handoff-diff observation attributes only this run's delta and
        # excludes any pre-existing dirt in the resolved product repo. Without this
        # the tamper check (`_handoff_diff_weakens_tests`) reads the developer's
        # live working tree and the test becomes non-deterministic (a stray
        # uncommitted assertion edit in the product repo would trip the gate).
        try:
            from agent_runtime.repo_context import capture_repo_baseline, command_workdir_for_task
            from agent_runtime.store import RunStore

            run.progress = {**(run.progress or {}), "repo_baseline": capture_repo_baseline(command_workdir_for_task(ctx.task))}
            RunStore().update(run)
        except Exception:
            pass

        return AgentDecision(
            type=DecisionType.PROPOSE_PATCH,
            summary="Delivered Mission Control UI patch",
            rationale="The product edit is complete and focused self-tests passed in-session.",
            payload={
                "summary": "Implemented compact Mission Control rows.",
                "changed_files": ["lib/features/mission_control/mission_control_page.dart"],
                "tests": ["flutter test test/features/mission_control/mission_control_page_test.dart passed"],
                "delivery": {
                    "work_status": "patch_proposed",
                },
            },
        )


class NoEditFindingsRuntime:
    def __init__(self):
        self.contexts = []

    def run_tick(self, persona, ctx, *, run):
        self.contexts.append(ctx)
        return AgentDecision(
            type=DecisionType.PROPOSE_PATCH,
            summary="Delivered no-edit NSFW investigation findings",
            rationale="Fulfilled Harness context bundles are enough to produce a grounded hardening plan; no product edits were made.",
            payload={
                "summary": "NSFW leakage likely involves publish-before-moderation and stale/fallback-safe rating paths.",
                "changed_files": [],
                "tests": ["no product edits; synthesized from fulfilled Harness context bundles"],
                "delivery": {
                    "work_status": "patch_proposed",
                    "known_gaps": ["external model pricing must be APPROVED separately"],
                },
            },
        )


class CapturingAutoGateProofRunner:
    def __init__(self, proof_store: ProofStore):
        self.proof_store = proof_store
        self.calls = []

    def run_commands(self, task, *, stage_id, run_id, actor, commands, proof_intent=None):
        self.calls.append({"stage_id": stage_id, "run_id": run_id, "actor": actor, "commands": list(commands), "proof_intent": proof_intent})
        proof = Proof(
            id=f"proof_auto_{task.id}_{stage_id}",
            task_id=task.id,
            stage_id=stage_id,
            type=ProofType.TEST_RUN,
            title="auto final gate",
            path_or_value="auto-final-gate.log",
            created_by=actor,
            created_at=now(),
            metadata={"status": "passed", "exit_code": 0, "commands_requested": len(commands), "run_id": run_id, "proof_intent": proof_intent},
            redaction_status="safe",
        )
        return [self.proof_store.attach(proof)]


class RuntimeMustNotRun:
    def run_tick(self, persona, ctx, *, run):
        raise AssertionError("persona runtime should not be launched")


class RequestLauncherStageTestRunRuntime:
    def run_tick(self, persona, ctx, *, run):
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="run launcher proof",
            rationale="Need deterministic Launcher proof before QA handoff.",
            payload={"stage_id": "launcher_contract_smoke", "commands": ["printf 'launcher-proof-ok\\n'"]},
        )


class RequestLauncherSmokeMarkerRuntime:
    def run_tick(self, persona, ctx, *, run):
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="run launcher smoke marker",
            rationale="Incorrectly treats smoke proof as implementation proof.",
            payload={
                "stage_id": "launcher_contract_smoke",
                "commands": ["python -c \"print('launcher_contract_smoke contract_packet_consumed backend_proof_consumed')\""],
            },
        )


class RequestBackendStageTestRunRuntime:
    def run_tick(self, persona, ctx, *, run):
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="run backend proof",
            rationale="Need deterministic Backend proof before Launcher handoff.",
            payload={"stage_id": "backend_no_op_route_proof", "commands": ["printf 'backend-proof-ok\\n'"]},
        )


class RequestBridgeStageWrongPageProofRuntime:
    def run_tick(self, persona, ctx, *, run):
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="run wrong bridge proof",
            rationale="Incorrectly reuses the page widget proof for a bridge/archive stage.",
            payload={
                "stage_id": "stage_bridge_archive_regression",
                "commands": ["flutter test test/features/mission_control/mission_control_page_test.dart"],
            },
        )


class RequestQaWithWrongBridgeProofThenCorrectProofRuntime:
    def __init__(self):
        self.calls = 0

    def run_tick(self, persona, ctx, *, run):
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        self.calls += 1
        if self.calls == 1:
            return AgentDecision(
                type=DecisionType.REQUEST_QA_REVIEW,
                summary="handoff bridge stage with stale page proof",
                rationale="This incorrectly treats the page widget proof as bridge/archive proof.",
                payload={
                    "stage_id": "stage_bridge_archive_regression",
                    "proof_ids": ["proof_page_bridge"],
                    "handoff": {"to": "qa", "stage_complete": True, "known_gaps": []},
                },
            )
        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="request corrected bridge snapshot proof",
            rationale="The repair prompt requires current-stage bridge/snapshot proof.",
            payload={
                "stage_id": "stage_bridge_archive_regression",
                "commands": [
                    "flutter test test/features/mission_control/mission_control_snapshot_test.dart test/features/mission_control/mission_control_bridge_test.dart"
                ],
            },
        )


class RequestLaterStageProofRuntime:
    def run_tick(self, persona, ctx, *, run):
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="skip to visual analyze",
            rationale="Incorrectly skips the current bridge/archive stage.",
            payload={
                "stage_id": "stage_launcher_analyze_and_visual_proof",
                "commands": ["flutter analyze lib/features/mission_control"],
            },
        )


class BlockOnDownstreamVisualBeforeCurrentProofRuntime:
    def run_tick(self, persona, ctx, *, run):
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        return AgentDecision(
            type=DecisionType.BLOCK,
            summary="Cannot hand off to QA yet because required fullscreen Stage C screenshot proof is missing.",
            rationale="Visual proof remains required before QA.",
            payload={
                "reason": "Required fullscreen screenshot proof is missing before QA handoff.",
                "log_ref": {"path": "events.jsonl", "line": 0, "summary": "missing screenshot proof"},
            },
        )


class CaptureAutonomyRuntime:
    def __init__(self):
        self.autonomy_packets = []

    def run_tick(self, persona, ctx, *, run):
        self.autonomy_packets.append(ctx.autonomy_packet)
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        return AgentDecision(
            type=DecisionType.PROPOSE_ACCEPTANCE,
            summary="scope accepted",
            rationale="Neko has a Harness autonomy packet before work.",
            payload={"objective": "obj", "acceptance_criteria": ["ok"]},
        )


class MetadataCaptureProofRunner:
    def __init__(self, proof_store: ProofStore):
        self.proof_store = proof_store
        self.calls = []

    def run_commands(
        self,
        task,
        *,
        stage_id,
        run_id,
        actor,
        commands,
        proof_intent=None,
        environment_fingerprint=None,
        environment_fingerprint_status=None,
    ):
        self.calls.append(
            {
                "proof_intent": proof_intent,
                "environment_fingerprint": environment_fingerprint,
                "environment_fingerprint_status": environment_fingerprint_status,
            }
        )
        proof = Proof(
            id=f"proof_{task.id}_{stage_id}",
            task_id=task.id,
            stage_id=stage_id,
            type=ProofType.TEST_RUN,
            title="metadata proof",
            path_or_value="metadata-proof.log",
            created_by=actor,
            created_at=now(),
            metadata={
                "status": "passed",
                "exit_code": 0,
                "commands_requested": len(commands),
                "run_id": run_id,
                "proof_intent": proof_intent,
                "environment_fingerprint": environment_fingerprint,
                "environment_fingerprint_status": environment_fingerprint_status,
            },
            redaction_status="safe",
        )
        return [self.proof_store.attach(proof)]


class FailedProofRunner:
    def __init__(self, proof_store: ProofStore):
        self.proof_store = proof_store

    def run_commands(self, task, *, stage_id, run_id, actor, commands):
        proof = Proof(
            id=f"failed_{task.id}_{stage_id}",
            task_id=task.id,
            stage_id=stage_id,
            type=ProofType.TEST_RUN,
            title="failed command proof",
            path_or_value="failed-proof.log",
            created_by=actor,
            created_at=now(),
            metadata={"status": "failed", "exit_code": 1, "commands_requested": len(commands), "run_id": run_id},
            redaction_status="safe",
        )
        self.proof_store.attach(proof)
        return [proof]


class SettledMissionRuntime:
    def __init__(self):
        self.personas = []

    def run_tick(self, persona, ctx, *, run):
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        self.personas.append(persona.id)
        if persona.id == "neko_supervisor":
            return AgentDecision(
                type=DecisionType.PROPOSE_ACCEPTANCE,
                summary="Neko scoped the mission",
                rationale="Ready for Dev with bounded acceptance.",
                payload={"objective": "ship the smoke", "acceptance_criteria": ["proof passes"]},
            )
        if persona.id == "dev":
            return AgentDecision(
                type=DecisionType.REQUEST_TEST_RUN,
                summary="Dev requested deterministic proof",
                rationale="Command proof is needed before QA.",
                payload={"stage_id": "implement", "commands": ["printf settled\\n"]},
            )
        if persona.id == "qa":
            return AgentDecision(
                type=DecisionType.REPORT_QA_VERDICT,
                summary="QA approved the proof",
                rationale="Proof IDs cover the implementation scope.",
                payload={"review_scope": "implementation", "verdict": "approved", "proof_ids": list(ctx.proof_ids), "findings": []},
            )
        raise AssertionError(f"unexpected persona: {persona.id}")


class PassingProofRunner:
    def __init__(self, proof_store: ProofStore):
        self.proof_store = proof_store

    def run_commands(self, task, *, stage_id, run_id, actor, commands):
        proof = Proof(
            id=f"proof_{task.id}_{stage_id}",
            task_id=task.id,
            stage_id=stage_id,
            type=ProofType.TEST_RUN,
            title="settled proof",
            path_or_value="settled-proof.log",
            created_by=actor,
            created_at=now(),
            metadata={"status": "passed", "exit_code": 0, "commands_requested": len(commands)},
            redaction_status="safe",
        )
        self.proof_store.attach(proof)
        return [proof]


def test_normal_worker_flow_auto_runs_final_gate_after_patch_delivery():
    ts = TaskStore()
    task = make_task()
    task.title = "Mission Control DM bubble terminal rows"
    task.description = "Upgrade Launcher Mission Control event rows into compact DM bubbles."
    task.state = TaskState.RUNNING
    task.affected_repos = ["EterniaLauncher"]
    task.current_stage_id = "mc_terminal_dm_bubble_rows"
    task.stages = [
        TaskStage(
            id="mc_terminal_dm_bubble_rows",
            title="Implement compact Mission Control terminal DM bubble event rows",
            objective="Replace heavy block cards with compact expandable DM bubble rows.",
            status=StageStatus.IMPLEMENTING,
            affected_paths=["lib/features/mission_control/mission_control_page.dart"],
            acceptance_criteria=["Widget tests cover bubble row rendering and expansion behavior."],
            test_plan=["flutter test test/features/mission_control/mission_control_page_test.dart"],
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
    ]
    ts.create(task)
    proofs = ProofStore()
    runtime = NormalFlowPatchRuntime()
    runner = CapturingAutoGateProofRunner(proofs)
    cfg = AgentRuntimeConfig(normal_worker_flow=NormalWorkerFlowConfig(enabled=True))
    engine = TickEngine(task_store=ts, proof_store=proofs, persona_runtime=runtime, proof_runner=runner, config=cfg)

    res = engine.tick_once(task_id="task_1")

    assert res.actions_taken[0].ok
    assert runtime.contexts[0].mission_hud["decision_contract_mode"] == "normal_worker_flow"
    assert runtime.contexts[0].mission_hud["primary_worker_action"]["action_id"] == "hand_off"
    assert "dev.request_test_run" not in runtime.contexts[0].mission_hud["decision_shape_index"]
    assert len(runner.calls) == 1
    assert runner.calls[0]["stage_id"] == "mc_terminal_dm_bubble_rows"
    assert runner.calls[0]["actor"] == "dev"
    assert runner.calls[0]["commands"] == ["flutter test test/features/mission_control/mission_control_page_test.dart"]
    assert runner.calls[0]["proof_intent"] == "auto_final_gate_after_delivery"
    stored = ts.get("task_1")
    assert stored.state == TaskState.RUNNING
    assert stored.stages[0].status == StageStatus.READY_FOR_QA
    assert stored.proof_ids == ["proof_auto_task_1_mc_terminal_dm_bubble_rows"]
    proof = proofs.get(stored.proof_ids[0])
    assert proof.metadata["proof_intent"] == "auto_final_gate_after_delivery"
    events = EventLog().for_task("task_1", limit=0)
    assert any(event.type == "delivery.intent" and event.payload.get("normal_worker_flow") is True for event in events)
    assert any(event.type == "proof.gate_checked" and event.payload.get("gate_source") == "auto_after_delivery" for event in events)


def test_normal_worker_flow_auto_runs_repo_default_final_gate_when_stage_test_plan_missing():
    ts = TaskStore()
    task = make_task()
    task.title = "Commit and deploy-check backend slice"
    task.description = "Commit the approved backend product-edit slice and let Harness run deploy check."
    task.state = TaskState.RUNNING
    task.affected_repos = ["EterniaBackend"]
    task.current_stage_id = "backend_implementation"
    task.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title="Commit backend slice", objective="Commit and deploy-check backend slice."),
        current_stage_id="backend_implementation",
        stages=[
            MissionPlanStage(
                id="backend_implementation",
                title="Backend Implementation",
                objective="Commit exactly the product slice and deliver a QA-ready packet.",
                owner="backend_dev",
                repo="EterniaBackend",
                kind="implementation",
                status=StageStatus.IMPLEMENTING,
                requires_product_edit=True,
            )
        ],
    )
    task.stages = [
        TaskStage(
            id="backend_implementation",
            title="Backend Implementation",
            objective="Commit the already prepared slice.",
            status=StageStatus.IMPLEMENTING,
            affected_paths=[],
            acceptance_criteria=["Harness runs manage.py check before QA."],
            test_plan=[],
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
    ]
    ts.create(task)
    proofs = ProofStore()
    runtime = NormalFlowPatchRuntime()
    runner = CapturingAutoGateProofRunner(proofs)
    cfg = AgentRuntimeConfig(normal_worker_flow=NormalWorkerFlowConfig(enabled=True))
    engine = TickEngine(task_store=ts, proof_store=proofs, persona_runtime=runtime, proof_runner=runner, config=cfg)

    res = engine.tick_once(task_id="task_1")

    assert res.actions_taken[0].ok
    assert len(runner.calls) == 1
    assert runner.calls[0]["stage_id"] == "backend_implementation"
    assert runner.calls[0]["commands"] == [".EterniaBackendVirtualEnv/Scripts/python.exe manage.py check"]
    assert runner.calls[0]["proof_intent"] == "auto_final_gate_after_delivery"
    stored = ts.get("task_1")
    assert stored.state == TaskState.RUNNING
    assert stored.stages[0].status == StageStatus.READY_FOR_QA
    assert stored.proof_ids == ["proof_auto_task_1_backend_implementation"]


def test_normal_worker_flow_auto_runs_handoff_exact_final_gate_before_launcher_default():
    ts = TaskStore()
    task = make_task()
    task.title = "E2E trust probe exact-gate"
    task.description = (
        "Create docs/scratch/e2e_trust_probe.md in the Launcher repo. "
        "Exact proof command: echo e2e-trust-probe. Do not run Flutter analyze/tests."
    )
    task.state = TaskState.RUNNING
    task.affected_repos = ["EterniaLauncher"]
    task.current_stage_id = "implement"
    task.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title="E2E trust probe exact-gate", objective="Patch one Launcher scratch file."),
        current_stage_id="implement",
        stages=[
            MissionPlanStage(
                id="implement",
                title="Launcher Implementation",
                objective="Create the requested scratch proof file and deliver with exact proof.",
                owner="dev",
                repo="EterniaLauncher",
                kind="implementation",
                status=StageStatus.IMPLEMENTING,
                requires_product_edit=True,
            )
        ],
    )
    task.stages = [
        TaskStage(
            id="implement",
            title="Launcher Implementation",
            objective="Create docs/scratch/e2e_trust_probe.md.",
            status=StageStatus.IMPLEMENTING,
            affected_paths=["docs/scratch/e2e_trust_probe.md"],
            acceptance_criteria=["Harness runs only echo e2e-trust-probe as the final command proof."],
            test_plan=[],
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
    ]
    ts.create(task)
    EventLog().append(
        Event(
            ts=now(),
            type="packet.recorded",
            task_id=task.id,
            run_id="run_neko",
            persona_id="neko_supervisor",
            payload={
                "packet_id": "packet_handoff_launcher_exact",
                "packet_type": "handoff_packet",
                "stage_id": "implement",
                "body": {
                    "packet_kind": "fresh_scope",
                    "mission_phase": "scope_route",
                    "handoff_mode": "single_specialist",
                    "target_owner": "dev",
                    "target_repo": "EterniaLauncher",
                    "proof_gate": {
                        "required": True,
                        "commands": ["echo e2e-trust-probe"],
                        "forbidden_commands": ["flutter analyze", "flutter test"],
                        "required_proof_types": ["test_run"],
                        "minimum_status": "passed",
                        "visual_required": False,
                    },
                },
            },
        )
    )
    proofs = ProofStore()
    runtime = NormalFlowPatchRuntime()
    runner = CapturingAutoGateProofRunner(proofs)
    cfg = AgentRuntimeConfig(normal_worker_flow=NormalWorkerFlowConfig(enabled=True))
    engine = TickEngine(task_store=ts, proof_store=proofs, persona_runtime=runtime, proof_runner=runner, config=cfg)

    res = engine.tick_once(task_id=task.id)

    assert res.actions_taken[0].ok
    assert len(runner.calls) == 1
    assert runner.calls[0]["stage_id"] == "implement"
    assert runner.calls[0]["commands"] == ["echo e2e-trust-probe"]
    assert all("flutter " not in command for command in runner.calls[0]["commands"])
    assert runner.calls[0]["proof_intent"] == "auto_final_gate_after_delivery"
    stored = ts.get(task.id)
    assert stored.state == TaskState.RUNNING
    assert stored.stages[0].status == StageStatus.READY_FOR_QA
    assert stored.proof_ids == ["proof_auto_task_1_implement"]


def test_normal_worker_flow_reuses_existing_passed_final_gate_after_handoff_repair():
    ts = TaskStore()
    task = make_task()
    task.title = "NSFW handoff repair"
    task.description = "Repair delivery handoff only; existing focused proof already passed."
    task.state = TaskState.RUNNING
    task.affected_repos = ["EterniaBackend"]
    task.current_stage_id = "backend_implementation"
    task.stages = [
        TaskStage(
            id="backend_implementation",
            title="Backend Implementation",
            objective="Patch backend media safety handoff metadata after QA requested supported summary fields.",
            status=StageStatus.IMPLEMENTING,
            affected_paths=["media/models.py", "media/services.py"],
            acceptance_criteria=["Handoff names inspected paths, changed paths, proof id, and non-coverage."],
            test_plan=[
                "source .EterniaBackendVirtualEnv/Scripts/activate && python - <<'PY'\n"
                "import django\n"
                "from django.conf import settings\n"
                "print('django_import_ok', django.get_version(), settings.configured)\n"
                "PY\n"
                "scripts/test.sh media.tests.FinalizeUploadTests"
            ],
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
    ]
    existing_proof = Proof(
        id="proof_existing_passed",
        task_id=task.id,
        stage_id="backend_implementation",
        type=ProofType.TEST_RUN,
        title="existing focused proof",
        path_or_value="proof.log",
        created_by="backend_dev",
        created_at=now(),
        metadata={
            "status": "passed",
            "exit_code": 0,
            "run_id": "run_previous",
            "command": task.stages[0].test_plan[0],
            "workdir_label": "eternia-backend",
        },
        redaction_status="safe",
    )
    task.proof_ids = [existing_proof.id]
    ts.create(task)
    proofs = ProofStore()
    proofs.attach(existing_proof)
    runtime = NormalFlowPatchRuntime()
    runner = CapturingAutoGateProofRunner(proofs)
    cfg = AgentRuntimeConfig(normal_worker_flow=NormalWorkerFlowConfig(enabled=True))
    engine = TickEngine(task_store=ts, proof_store=proofs, persona_runtime=runtime, proof_runner=runner, config=cfg)

    res = engine.tick_once(task_id="task_1")

    assert res.actions_taken[0].ok
    assert runner.calls == []
    stored = ts.get("task_1")
    assert stored.state == TaskState.RUNNING
    assert stored.proof_ids == ["proof_existing_passed"]
    assert stored.stages[0].status == StageStatus.READY_FOR_QA
    events = EventLog().for_task("task_1", limit=0)
    assert any(
        event.type == "proof.gate_checked"
        and event.payload.get("gate_source") == "auto_after_delivery"
        and event.payload.get("reused_existing_proof") is True
        and event.payload.get("proof_ids") == ["proof_existing_passed"]
        for event in events
    )


def test_handoff_repair_with_existing_passed_proof_routes_to_qa_without_dev_run():
    ts = TaskStore()
    task = make_task()
    task.title = "NSFW handoff repair"
    task.description = "Delivery metadata repair only; use existing passed proof."
    task.state = TaskState.RUNNING
    task.affected_repos = ["EterniaBackend"]
    task.current_stage_id = "backend_implementation"
    task.risk_flags = ["This is a delivery metadata repair only; use existing passed proof proof_existing_passed."]
    task.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title=task.title, objective=task.description),
        current_stage_id="backend_implementation",
        stages=[
            MissionPlanStage(
                id="backend_implementation",
                title="Backend Implementation",
                objective="Patch backend media safety.",
                owner="backend_dev",
                repo="EterniaBackend",
                kind="implementation",
                status=StageStatus.IMPLEMENTING,
                blocks_qa_until=True,
            ),
            MissionPlanStage(
                id="qa_release",
                title="QA Release",
                objective="Verify",
                owner="qa",
                repo="EterniaBackend",
                kind="qa_verdict",
                status=StageStatus.READY,
                depends_on=["backend_implementation"],
            ),
        ],
    )
    task.stages = [
        TaskStage(
            id="backend_implementation",
            title="Backend Implementation",
            objective="Patch backend media safety behavior.",
            status=StageStatus.IMPLEMENTING,
            affected_paths=["media/models.py"],
            acceptance_criteria=["QA requested handoff metadata repair only."],
            test_plan=["python manage.py test media.tests.FinalizeUploadTests"],
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
    ]
    proof = Proof(
        id="proof_existing_passed",
        task_id=task.id,
        stage_id="backend_implementation",
        type=ProofType.TEST_RUN,
        title="existing focused proof",
        path_or_value="proof.log",
        created_by="backend_dev",
        created_at=now(),
        metadata={
            "status": "passed",
            "exit_code": 0,
            "run_id": "run_previous",
            "command": "python manage.py test media.tests.FinalizeUploadTests",
            "workdir_label": "eternia-backend",
        },
        redaction_status="safe",
    )
    task.proof_ids = [proof.id]
    ts.create(task)
    proofs = ProofStore()
    proofs.attach(proof)
    engine = TickEngine(task_store=ts, proof_store=proofs, persona_runtime=RuntimeMustNotRun())

    res = engine.tick_once(task_id="task_1")

    assert res.actions_taken[0].ok
    assert res.actions_taken[0].summary == "handoff repair reused existing passed proof; routed to QA"
    stored = ts.get("task_1")
    assert stored.state == TaskState.RUNNING
    assert graph_stage_status(stored, "backend_implementation") == StageStatus.READY_FOR_QA
    events = EventLog().for_task("task_1", limit=0)
    assert any(event.type == "task.transition" and event.payload.get("source") == "deterministic_handoff_repair_recovery" for event in events)


def test_normal_worker_flow_no_edit_investigation_delivery_advances_to_qa_without_command_proof():
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.affected_repos = ["EterniaBackend"]
    task.current_stage_id = "backend_investigation"
    task.context_requests = [
        {"id": "ctx_1", "actor": "backend_dev", "stage_id": "backend_investigation", "status": "fulfilled"},
        {"id": "ctx_2", "actor": "backend_dev", "stage_id": "backend_investigation", "status": "fulfilled"},
    ]
    task.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title="NSFW investigation", objective="No-product-edit investigation."),
        current_stage_id="backend_investigation",
        stages=[
            MissionPlanStage(
                id="backend_investigation",
                title="Backend Investigation",
                objective="No-product-edit investigation: inspect backend NSFW moderation paths and produce findings.",
                owner="backend_dev",
                repo="EterniaBackend",
                kind="investigation",
                requires_product_edit=False,
                status=StageStatus.READY,
                blocks_qa_until=True,
            )
        ],
    )
    ts.create(task)
    cfg = RuntimeConfig(
        normal_worker_flow=NormalWorkerFlowConfig(enabled=True),
        mission_plan=MissionPlanConfig(enabled=True),
    )
    runtime = NoEditFindingsRuntime()
    engine = TickEngine(task_store=ts, persona_runtime=runtime, config=cfg)

    res = engine.tick_once(task_id="task_1")

    assert res.actions_taken[0].ok
    stored = ts.get("task_1")
    assert stored.state == TaskState.RUNNING
    assert stored.mission_plan.stages[0].status == StageStatus.PASSED
    assert engine.state_machine.next_action(stored).type == HarnessActionType.RUN_SLOT
    assert runtime.contexts[0].mission_hud["primary_worker_action"]["action_id"] == "hand_off"


def test_run_until_settled_drives_neko_dev_qa_and_deterministic_complete():
    ts = TaskStore()
    ts.create(
        attach_blueprint(
            make_task(),
            "neko_dev_qa_basic",
            {
                "lead": "persona:neko_supervisor",
                "builder": "persona:dev",
                "verifier": "persona:qa",
            },
        )
    )
    proofs = ProofStore()
    runtime = SettledMissionRuntime()
    engine = TickEngine(task_store=ts, proof_store=proofs, persona_runtime=runtime, proof_runner=PassingProofRunner(proofs))

    res = engine.run_until_settled(task_id="task_1", max_actions=8)

    assert res.task_id == "task_1"
    assert res.stop_reason == "task_terminal"
    assert res.final_task_state == TaskState.DONE.value
    assert len(res.actions_taken) == 4
    assert [action.action.type for action in res.actions_taken] == [
        HarnessActionType.RUN_SLOT,
        HarnessActionType.RUN_SLOT,
        HarnessActionType.RUN_SLOT,
        HarnessActionType.COMPLETE_TASK,
    ]
    assert runtime.personas == ["neko_supervisor", "dev", "qa"]
    assert ts.get("task_1").state == TaskState.DONE


def test_backend_affected_repo_routes_run_dev_to_backend_dev():
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.affected_repos = ["EterniaBackend"]
    ts.create(task)
    runtime = CapturingFailureRuntime()
    engine = TickEngine(task_store=ts, persona_runtime=runtime)

    res = engine.tick_once(task_id="task_1")

    assert len(res.actions_taken) == 1
    assert res.actions_taken[0].action.type == HarnessActionType.RUN_SLOT
    assert runtime.personas == ["backend_dev"]


def test_backend_stage_that_mentions_launcher_release_still_routes_to_backend_dev():
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.affected_repos = ["EterniaBackend", "EterniaLauncher", "hermes-agent"]
    task.current_stage_id = "stage_47_backend_contract_proof"
    task.stages = [
        TaskStage(
            id="stage_47_backend_contract_proof",
            title="Backend Contract Proof",
            objective="Attach deterministic backend proof before the Launcher release gate.",
            status=StageStatus.IMPLEMENTING,
            test_plan=["python manage.py test api.tests.SystemHealthContractTests"],
        )
    ]
    ts.create(task)
    runtime = CapturingFailureRuntime()
    engine = TickEngine(task_store=ts, persona_runtime=runtime)

    res = engine.tick_once(task_id="task_1")

    assert len(res.actions_taken) == 1
    assert res.actions_taken[0].action.type == HarnessActionType.RUN_SLOT
    assert runtime.personas == ["backend_dev"]


def test_run_until_settled_blocks_orchestration_only_backend_plan_without_repeating():
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.requested_by = "stage47_burn_in"
    task.affected_repos = ["EterniaBackend", "EterniaLauncher", "hermes-agent"]
    task.risk_flags = ["cross_stack_contract_handoff", "backend_contract_first", "bounded_complex_burn_in"]
    ts.create(task)
    incidents = IncidentStore()
    runs = RunStore()
    runtime = OrchestrationOnlyBackendPlanRuntime()
    engine = TickEngine(task_store=ts, run_store=runs, incident_store=incidents, persona_runtime=runtime)

    res = engine.run_until_settled(task_id="task_1", max_actions=4)
    saved = ts.get("task_1")

    assert res.stop_reason == "max_actions"
    assert len(res.actions_taken) == 4
    assert res.actions_taken[0].action.type == HarnessActionType.RUN_SLOT
    assert runtime.personas == ["backend_dev", "backend_dev", "backend_dev", "backend_dev"]
    assert saved.state == TaskState.RUNNING
    assert len(incidents.list_open()) == 0


def test_latest_launcher_handoff_overrides_mixed_backend_affected_repos():
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    task.affected_repos = ["EterniaLauncher", "EterniaBackend", "hermes-agent"]
    ts.create(task)
    EventLog().append(
        Event(
            ts=now(),
            type="packet.recorded",
            task_id=task.id,
            run_id="run_neko",
            persona_id="neko_supervisor",
            payload={
                "packet_type": "handoff_packet",
                "stage_id": "stage_1",
                "body": {
                    "packet_kind": "contract_join",
                    "handoff_mode": "sequential_specialists",
                    "target_owner": "dev",
                    "target_repo": "EterniaLauncher",
                    "proof_gate": {"required": True, "minimum_status": "passed"},
                },
            },
        )
    )
    runtime = CapturingFailureRuntime()
    engine = TickEngine(task_store=ts, persona_runtime=runtime)

    res = engine.tick_once(task_id="task_1")

    assert len(res.actions_taken) == 1
    assert res.actions_taken[0].action.type == HarnessActionType.RUN_SLOT
    assert runtime.personas == ["dev"]


def test_launcher_stage_identity_overrides_stale_unscoped_backend_handoff():
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "launcher_contract_smoke"
    task.affected_repos = ["EterniaLauncher"]
    task.stages = [
        TaskStage(
            id="launcher_contract_smoke",
            title="Launcher Contract Smoke",
            objective="Collect deterministic Launcher command proof.",
            status=StageStatus.IMPLEMENTING,
        )
    ]
    ts.create(task)
    EventLog().append(
        Event(
            ts=now(),
            type="packet.recorded",
            task_id=task.id,
            run_id="run_neko",
            persona_id="neko_supervisor",
            payload={
                "packet_type": "handoff_packet",
                "stage_id": None,
                "body": {
                    "packet_kind": "fresh_scope",
                    "handoff_mode": "backend_first_cross_stack",
                    "target_owner": "backend_dev",
                    "target_repo": "EterniaBackend",
                    "proof_gate": {"required": True, "minimum_status": "passed"},
                },
            },
        )
    )
    runtime = CapturingFailureRuntime()
    engine = TickEngine(task_store=ts, persona_runtime=runtime)

    res = engine.tick_once(task_id="task_1")

    assert len(res.actions_taken) == 1
    assert res.actions_taken[0].action.type == HarnessActionType.RUN_SLOT
    assert runtime.personas == ["dev"]


def test_stale_approved_dev_continuation_does_not_override_backend_routing():
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.affected_repos = ["EterniaBackend"]
    ts.create(task)
    runs = RunStore()
    old = runs.open_run("dev", "task_1", stage_id=None, session_id="session_old_dev")
    runs.close_run(old.id, state=RunState.WAITING_ON_APPROVAL, error={"type": "run_budget_exceeded"})
    runs.approve_continuation(old.id)
    newer = runs.open_run("dev", "task_1", stage_id=None, session_id="session_new_cancelled")
    runs.cancel(newer.id, reason="newer run superseded old continuation")
    runtime = CapturingFailureRuntime()
    engine = TickEngine(task_store=ts, run_store=runs, persona_runtime=runtime)

    res = engine.tick_once(task_id="task_1")

    assert len(res.actions_taken) == 1
    assert res.actions_taken[0].action.type == HarnessActionType.RUN_SLOT
    assert runtime.personas == ["backend_dev"]


def test_matching_approved_backend_continuation_reuses_backend_session():
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.affected_repos = ["EterniaBackend"]
    ts.create(task)
    runs = RunStore()
    old = runs.open_run(
        "backend_dev",
        "task_1",
        stage_id=None,
        session_id="session_backend_dev",
    )
    runs.close_run(
        old.id,
        state=RunState.WAITING_ON_APPROVAL,
        error={"type": "run_budget_exceeded"},
    )
    runs.approve_continuation(old.id)
    runtime = InspectContinuationSessionRuntime()
    engine = TickEngine(task_store=ts, run_store=runs, persona_runtime=runtime)

    res = engine.tick_once(task_id="task_1")

    assert len(res.actions_taken) == 1
    assert res.actions_taken[0].action.type == HarnessActionType.RUN_SLOT
    assert runtime.seen_session_ids == [("backend_dev", "session_backend_dev")]


def test_run_until_settled_stops_on_open_incident_after_failed_action(tmp_path):
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    task.affected_repos = [str(tmp_path / "missing-product-repo")]
    ts.create(task)
    engine = TickEngine(task_store=ts, persona_runtime=RequestTestRunRuntime())

    res = engine.run_until_settled(task_id="task_1", max_actions=8)

    assert res.stop_reason in {"action_failed", "incident_opened"}
    assert len(res.actions_taken) == 1
    assert not res.actions_taken[0].ok
    assert res.open_incidents == 1
    assert ts.get("task_1").state == TaskState.RUNNING


def test_settled_boundary_allows_neko_scope_recovery_for_read_search_budget_loop(isolate_agent_runtime_root):
    ts = TaskStore()
    runs = RunStore()
    incidents = IncidentStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "backend_implementation"
    task.open_incident_ids = ["inc_loop"]
    ts.create(task)
    waiting = runs.open_run("backend_dev", "task_1", stage_id="backend_implementation", session_id="session_budget")
    waiting.progress = {
        "loop_warning": "read_search_without_patch_threshold",
        "read_search_count": 6,
        "read_search_limit": 6,
        "patch_count": 0,
        "proof_count": 0,
    }
    waiting.state = RunState.WAITING_ON_APPROVAL
    waiting.error = {"type": "run_budget_exceeded"}
    runs.update(waiting)
    incidents.open(
        Incident(
            id="inc_loop",
            task_id="task_1",
            run_id=waiting.id,
            kind="run_budget_exceeded",
            summary="budget",
            detail_path=None,
            opened_at=now(),
        )
    )
    engine = TickEngine(task_store=ts, run_store=runs, incident_store=incidents, persona_runtime=CapturingFailureRuntime())

    assert engine._settled_boundary(task_id="task_1") is None


def test_settled_boundary_untargeted_daemon_allows_neko_incident_recovery(isolate_agent_runtime_root):
    """An UNTARGETED daemon (task_id=None) must apply the per-task
    Neko-owns-recovery carve-out instead of hard-stopping on any open
    incident (observed live: settle_stop_reason=incident_opened, actions=0
    across daemon loops while a running task waited on adjudication)."""

    ts = TaskStore()
    runs = RunStore()
    incidents = IncidentStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "backend_implementation"
    task.open_incident_ids = ["inc_untargeted"]
    ts.create(task)
    waiting = runs.open_run("backend_dev", "task_1", stage_id="backend_implementation", session_id="session_budget")
    waiting.progress = {
        "loop_warning": "read_search_without_patch_threshold",
        "read_search_count": 6,
        "read_search_limit": 6,
        "patch_count": 0,
        "proof_count": 0,
    }
    waiting.state = RunState.WAITING_ON_APPROVAL
    waiting.error = {"type": "run_budget_exceeded"}
    runs.update(waiting)
    incidents.open(
        Incident(
            id="inc_untargeted",
            task_id="task_1",
            run_id=waiting.id,
            kind="run_budget_exceeded",
            summary="budget",
            detail_path=None,
            opened_at=now(),
        )
    )
    engine = TickEngine(task_store=ts, run_store=runs, incident_store=incidents, persona_runtime=CapturingFailureRuntime())

    # The same fixture stops the tick when targeted at a task with a hard
    # environment blocker; here the untargeted boundary must proceed so the
    # incident reaches Neko adjudication.
    assert engine._settled_boundary(task_id=None) is None


def test_run_until_settled_honors_max_actions_without_runaway_loop():
    ts = TaskStore()
    ts.create(make_task())
    runtime = SettledMissionRuntime()
    engine = TickEngine(task_store=ts, persona_runtime=runtime)

    res = engine.run_until_settled(task_id="task_1", max_actions=1)

    assert res.stop_reason == "max_actions"
    assert len(res.actions_taken) == 1
    assert ts.get("task_1").state == TaskState.RUNNING


def test_run_until_settled_enforces_max_actions_even_when_tick_config_allows_more():
    ts = TaskStore()
    ts.create(make_task_with_id("task_a"))
    ts.create(make_task_with_id("task_b"))
    cfg = AgentRuntimeConfig(max_actions_per_tick=2)
    engine = TickEngine(task_store=ts, persona_runtime=FakeRuntime(), config=cfg)

    res = engine.run_until_settled(max_actions=1)

    assert res.stop_reason == "max_actions"
    assert len(res.actions_taken) == 1
    assert ts.get("task_a").state == TaskState.RUNNING
    assert ts.get("task_b").state == TaskState.CREATED


def test_run_until_settled_reports_preexisting_incident_without_tick():
    ts = TaskStore()
    ts.create(make_task())
    incidents = IncidentStore()
    incidents.open(Incident(id="inc_existing", task_id="task_1", run_id=None, kind="provider_failure", summary="auth", detail_path=None, opened_at=now()))
    runtime = SettledMissionRuntime()
    engine = TickEngine(task_store=ts, incident_store=incidents, persona_runtime=runtime)

    res = engine.run_until_settled(task_id="task_1", max_actions=8)

    assert res.stop_reason == "incident_opened"
    assert res.actions_taken == []
    assert res.open_incidents == 1
    assert runtime.personas == []


def test_run_until_settled_reports_preexisting_active_and_waiting_runs_without_tick():
    for index, (state, expected) in enumerate([(RunState.RUNNING, "active_run"), (RunState.WAITING_ON_APPROVAL, "waiting_on_approval")], start=1):
        task_id = f"task_boundary_run_{index}"
        ts = TaskStore()
        ts.create(make_task_with_id(task_id))
        runs = RunStore()
        run = runs.open_run("neko_supervisor", task_id, stage_id=None)
        run.state = state
        runs.update(run)
        runtime = SettledMissionRuntime()
        engine = TickEngine(task_store=ts, run_store=runs, persona_runtime=runtime)

        res = engine.run_until_settled(task_id=task_id, max_actions=8)

        assert res.stop_reason == expected
        assert res.actions_taken == []
        assert runtime.personas == []


def test_run_until_settled_recovers_stale_active_run_before_active_boundary():
    ts = TaskStore()
    ts.create(make_task())
    runs = RunStore()
    incidents = IncidentStore()
    run = runs.open_run("neko_supervisor", "task_1", stage_id=None)
    run.last_heartbeat_at = now() - timedelta(seconds=120)
    runs.update(run)
    runtime = SettledMissionRuntime()
    cfg = AgentRuntimeConfig(heartbeat_ttl_seconds=1)
    engine = TickEngine(task_store=ts, run_store=runs, incident_store=incidents, persona_runtime=runtime, config=cfg)

    res = engine.run_until_settled(task_id="task_1", max_actions=8)

    assert res.stop_reason == "incident_opened"
    assert res.actions_taken == []
    assert runs.get(run.id).state == RunState.STALE
    assert incidents.list_open()[0].kind == "stale_run"
    assert runtime.personas == []


def test_run_until_settled_reports_preexisting_terminal_or_blocked_task_without_tick():
    for index, (state, expected) in enumerate([(TaskState.DONE, "task_terminal"), (TaskState.CANCELLED, "task_terminal"), (TaskState.BLOCKED, "task_blocked")], start=1):
        task_id = f"task_boundary_state_{index}"
        ts = TaskStore()
        task = make_task_with_id(task_id)
        task.state = state
        if state == TaskState.BLOCKED:
            task.risk_flags = [NEKO_BLOCK_RECOVERY_ATTEMPTED_FLAG]
        ts.create(task)
        runtime = SettledMissionRuntime()
        engine = TickEngine(task_store=ts, persona_runtime=runtime)

        res = engine.run_until_settled(task_id=task_id, max_actions=8)

        assert res.stop_reason == expected
        assert res.actions_taken == []
        assert runtime.personas == []


class RequestTestThenHandoffRuntime:
    def __init__(self):
        self.calls = 0

    def run_tick(self, persona, ctx, *, run):
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        self.calls += 1
        if self.calls == 1:
            return AgentDecision(
                type=DecisionType.REQUEST_TEST_RUN,
                summary="collect smoke proof",
                rationale="Need deterministic proof before QA.",
                payload={"stage_id": "implement", "commands": ["printf 'smoke-ok\\n'"]},
            )
        return AgentDecision(
            type=DecisionType.REQUEST_QA_REVIEW,
            summary="handoff with proof",
            rationale="Proof was attached by Harness.",
            payload={"stage_id": "implement", "proof_ids": list(ctx.proof_ids), "handoff": {"to": "qa", "stage_complete": True, "known_gaps": []}},
        )


class RequestTestThenQaVerdictRuntime:
    def run_tick(self, persona, ctx, *, run):
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        if persona.id == "qa":
            return AgentDecision(
                type=DecisionType.QA_VERDICT,
                summary="QA approved attached proof",
                rationale="Implementation proof is attached and passed.",
                payload={
                    "verdict": "approved",
                    "proof_ids": list(ctx.proof_ids),
                    "findings": ["command proof passed"],
                },
            )
        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="collect smoke proof",
            rationale="Need deterministic proof before QA.",
            payload={"stage_id": "implement", "commands": ["printf 'smoke-ok\\n'"]},
        )


def test_tick_collects_command_proof_for_request_test_run(tmp_path):
    ts = TaskStore()
    task = attach_blueprint(
        make_task(),
        "two_agent_build_verify",
        {
            "builder": "persona:dev",
            "verifier": "persona:qa",
        },
    )
    task.state = TaskState.RUNNING
    ts.create(task)
    engine = TickEngine(task_store=ts, persona_runtime=RequestTestRunRuntime())
    engine.command_workdir = tmp_path

    res = engine.tick_once()

    assert res.actions_taken[0].ok
    saved = ts.get("task_1")
    assert saved.state == TaskState.RUNNING
    assert len(saved.proof_ids) == 1
    assert graph_stage_status(saved, "implement") == StageStatus.PASSED
    assert saved.current_stage_id == "verify"
    proof = engine.proof_store.get(saved.proof_ids[0])
    assert proof.type.value == "test_run"
    assert proof.metadata["exit_code"] == 0
    assert proof.metadata["commands_requested"] == 1


def test_tick_attaches_qa_verdict_proof_and_closes_blueprint(tmp_path):
    ts = TaskStore()
    task = attach_blueprint(
        make_task(),
        "two_agent_build_verify",
        {
            "builder": "persona:dev",
            "verifier": "persona:qa",
        },
    )
    task.state = TaskState.RUNNING
    ts.create(task)
    engine = TickEngine(
        task_store=ts,
        persona_runtime=RequestTestThenQaVerdictRuntime(),
    )
    engine.command_workdir = tmp_path

    result = engine.run_until_settled(task_id="task_1", max_actions=4)

    assert result.stop_reason == "task_terminal"
    assert [action.payload.get("decision") for action in result.actions_taken[:2]] == [
        "request_test_run",
        "qa_verdict",
    ]
    saved = ts.get("task_1")
    assert saved.state == TaskState.DONE
    assert saved.current_stage_id is None
    assert graph_stage_status(saved, "implement") == StageStatus.PASSED
    assert graph_stage_status(saved, "verify") == StageStatus.PASSED
    proof_types = [engine.proof_store.get(proof_id).type for proof_id in saved.proof_ids]
    assert proof_types == [ProofType.TEST_RUN, ProofType.QA_VERDICT]
    verify_stage = next(stage for stage in saved.mission_plan.stages if stage.id == "verify")
    assert verify_stage.proof_ids == [saved.proof_ids[-1]]


def test_tick_injects_autonomy_packet_before_persona_runtime():
    ts = TaskStore()
    task = make_task()
    ts.create(task)
    runtime = CaptureAutonomyRuntime()

    res = TickEngine(task_store=ts, persona_runtime=runtime).tick_once(task_id="task_1")

    assert res.actions_taken[0].ok
    assert runtime.autonomy_packets
    packet = runtime.autonomy_packets[0]
    assert packet["autonomy_packet_id"].startswith("auto_run_")
    assert packet["context_receipt_id"].startswith("ctxr_run_")
    assert packet["inspection_budget"]["read_search_limit"] >= 1
    events = [event for event in EventLog().for_task("task_1", limit=0)]
    autonomy_index = next(index for index, event in enumerate(events) if event.payload.get("step") == "autonomy_packet")
    closed_index = next(index for index, event in enumerate(events) if event.type == "run.closed")
    assert autonomy_index < closed_index


def test_tick_passes_proof_intent_and_environment_fingerprint_metadata_to_runner():
    ts = TaskStore()
    proofs = ProofStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    task.harness_self_heal = {
        "stages": {
            "implement": {
                "last_environment_fingerprint": "docker_desktop:missing",
                "environment_fingerprint_status": "unchanged",
            }
        }
    }
    ts.create(task)
    runner = MetadataCaptureProofRunner(proofs)
    engine = TickEngine(task_store=ts, proof_store=proofs, persona_runtime=RequestTestRunRuntime(), proof_runner=runner)

    res = engine.tick_once(task_id="task_1")

    assert res.actions_taken[0].ok
    assert runner.calls[0]["proof_intent"].startswith("run focused tests")
    assert runner.calls[0]["environment_fingerprint"] == "docker_desktop:missing"
    assert runner.calls[0]["environment_fingerprint_status"] == "unchanged"
    proof = proofs.get(ts.get("task_1").proof_ids[0])
    assert proof.metadata["proof_intent"].startswith("run focused tests")
    assert proof.metadata["environment_fingerprint_status"] == "unchanged"


def test_tick_collects_command_proof_in_task_affected_repo_when_no_explicit_workdir(tmp_path):
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    task.affected_repos = [str(tmp_path)]
    ts.create(task)
    engine = TickEngine(task_store=ts, persona_runtime=RequestTestRunRuntime())

    res = engine.tick_once()

    assert res.actions_taken[0].ok
    saved = ts.get("task_1")
    proof = engine.proof_store.get(saved.proof_ids[0])
    from agent_runtime import paths
    artifact = paths.store_root() / proof.path_or_value
    text = artifact.read_text(encoding="utf-8")
    assert f"workdir: <workdir:{tmp_path.name}>" in text
    assert proof.metadata["exit_code"] == 0


def test_tick_collects_launcher_stage_command_proof_in_launcher_repo_when_scope_is_broad(tmp_path):
    backend_repo = tmp_path / "EterniaBackend"
    launcher_repo = tmp_path / "EterniaLauncher"
    backend_repo.mkdir()
    launcher_repo.mkdir()
    ts = TaskStore()
    task_id = f"task_launcher_stage_scope_{uuid.uuid4().hex[:8]}"
    task = make_task_with_id(task_id)
    task.state = TaskState.RUNNING
    task.current_stage_id = "launcher_contract_smoke"
    task.affected_repos = [str(backend_repo), str(launcher_repo)]
    task.stages = [
        TaskStage(
            id="launcher_contract_smoke",
            title="Launcher Contract Smoke",
            objective="Collect deterministic Launcher proof.",
            status=StageStatus.IMPLEMENTING,
            test_plan=["printf 'launcher-proof-ok\\n'"],
        )
    ]
    from agent_runtime.ticker import _command_workdir_for_task

    assert _command_workdir_for_task(task, actor="dev", stage_id="launcher_contract_smoke") == launcher_repo
    ts.create(task)
    engine = TickEngine(task_store=ts, persona_runtime=RequestLauncherStageTestRunRuntime())

    res = engine.tick_once(task_id=task_id)

    assert res.actions_taken[0].ok
    saved = ts.get(task_id)
    proof = engine.proof_store.get(saved.proof_ids[0])
    from agent_runtime import paths

    artifact = paths.store_root() / proof.path_or_value
    text = artifact.read_text(encoding="utf-8")
    assert saved.proof_ids[0].startswith(f"test_{task_id}_launcher_contract_smoke_")
    assert "workdir: <workdir:EterniaLauncher>" in text
    assert "launcher-proof-ok" in text
    assert proof.metadata["exit_code"] == 0


def test_flutter_bridge_regression_stage_uses_launcher_workdir_despite_harness_objective(tmp_path):
    launcher_repo = tmp_path / "EterniaLauncher"
    launcher_repo.mkdir()
    task = make_task_with_id("task_bridge_stage_scope")
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_bridge_archive_regression"
    task.affected_repos = [str(launcher_repo)]
    task.stages = [
        TaskStage(
            id="stage_bridge_archive_regression",
            title="Preserve Mission Control runtime root/profile snapshot and archive bridge behavior",
            objective="Run existing bridge/archive regression coverage to ensure the UI upgrade does not alter current Harness runtime root/profile snapshot/archive semantics.",
            status=StageStatus.IMPLEMENTING,
            test_plan=[
                "flutter test test/features/mission_control/mission_control_snapshot_test.dart test/features/mission_control/mission_control_bridge_test.dart",
            ],
        )
    ]

    from agent_runtime.ticker import _command_workdir_for_task

    assert _command_workdir_for_task(task, actor="dev", stage_id="stage_bridge_archive_regression") == launcher_repo


def test_tick_collects_backend_stage_command_proof_in_backend_repo_despite_launcher_path_filter(tmp_path):
    backend_repo = tmp_path / "EterniaBackend"
    launcher_repo = tmp_path / "EterniaLauncher"
    backend_repo.mkdir()
    launcher_repo.mkdir()
    ts = TaskStore()
    task_id = f"task_backend_stage_scope_{uuid.uuid4().hex[:8]}"
    task = make_task_with_id(task_id)
    task.state = TaskState.RUNNING
    task.current_stage_id = "backend_no_op_route_proof"
    task.affected_repos = [str(backend_repo), str(launcher_repo)]
    task.stages = [
        TaskStage(
            id="backend_no_op_route_proof",
            title="Backend No Op Route Proof",
            objective="Collect deterministic backend proof before Launcher Dev is released.",
            status=StageStatus.IMPLEMENTING,
            test_plan=["python - <<'PY'\nprint('launcher/')\nPY"],
        )
    ]
    from agent_runtime.ticker import _command_workdir_for_task

    assert _command_workdir_for_task(task, actor="backend_dev", stage_id="backend_no_op_route_proof") == backend_repo
    ts.create(task)
    engine = TickEngine(task_store=ts, persona_runtime=RequestBackendStageTestRunRuntime())

    res = engine.tick_once(task_id=task_id)

    assert res.actions_taken[0].ok
    saved = ts.get(task_id)
    proof = engine.proof_store.get(saved.proof_ids[0])
    from agent_runtime import paths

    artifact = paths.store_root() / proof.path_or_value
    text = artifact.read_text(encoding="utf-8")
    assert "workdir: <workdir:EterniaBackend>" in text
    assert proof.metadata["workdir_label"] == "EterniaBackend"
    assert proof.metadata["exit_code"] == 0


def test_command_proof_scope_prefers_typed_stage_repo_over_task_repo_list():
    task = make_task_with_id("task_stage_repo_authority")
    task.affected_repos = ["EterniaBackend"]
    task.current_stage_id = "implement"
    task.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title="Cross-stack", objective="Launcher consumes backend contract."),
        current_stage_id="implement",
        stages=[
            MissionPlanStage(
                id="implement",
                title="Implementation",
                objective="Consume the no-change backend contract.",
                owner="dev",
                repo="EterniaLauncher",
                kind="implementation",
                status=StageStatus.IMPLEMENTING,
            )
        ],
    )

    from agent_runtime.ticker import _command_proof_repo_scope

    assert _command_proof_repo_scope(task, actor="dev", stage_id="implement") == "EterniaLauncher"


class MismatchedWorkdirProofRunner:
    def __init__(self, proof_store: ProofStore):
        self.proof_store = proof_store

    def run_commands(self, task, *, stage_id, run_id, actor, commands):
        proof = Proof(
            id="proof_wrong_repo",
            task_id=task.id,
            stage_id=stage_id,
            type=ProofType.TEST_RUN,
            title="wrong repo proof",
            path_or_value="wrong-repo.log",
            created_by="harness",
            created_at=now(),
            metadata={
                "status": "passed",
                "exit_code": 0,
                "run_id": run_id,
                "workdir_label": "EterniaLauncher",
            },
            redaction_status="safe",
        )
        self.proof_store.attach(proof)
        return [proof]


class PassingCommandMetadataProofRunner:
    def __init__(self, proof_store: ProofStore):
        self.proof_store = proof_store

    def run_commands(self, task, *, stage_id, run_id, actor, commands):
        proof = Proof(
            id="proof_wrong_stage_command",
            task_id=task.id,
            stage_id=stage_id,
            type=ProofType.TEST_RUN,
            title="wrong stage command proof",
            path_or_value="wrong-stage-command.log",
            created_by=actor,
            created_at=now(),
            metadata={
                "status": "passed",
                "exit_code": 0,
                "run_id": run_id,
                "command": commands[0],
                "commands_requested": len(commands),
                "workdir_label": "EterniaLauncher",
            },
            redaction_status="safe",
        )
        self.proof_store.attach(proof)
        return [proof]


def test_tick_blocks_passing_backend_stage_proof_from_wrong_repo_workdir():
    ts = TaskStore()
    proofs = ProofStore()
    task = make_task_with_id("task_backend_wrong_repo")
    task.state = TaskState.RUNNING
    task.current_stage_id = "backend_no_op_route_proof"
    task.affected_repos = ["EterniaBackend", "EterniaLauncher"]
    task.stages = [
        TaskStage(
            id="backend_no_op_route_proof",
            title="Backend No Op Route Proof",
            objective="Collect deterministic backend proof before Launcher Dev is released.",
            status=StageStatus.IMPLEMENTING,
        )
    ]
    ts.create(task)
    engine = TickEngine(
        task_store=ts,
        proof_store=proofs,
        persona_runtime=RequestBackendStageTestRunRuntime(),
        proof_runner=MismatchedWorkdirProofRunner(proofs),
    )

    res = engine.tick_once()

    assert res.actions_taken[0].ok
    saved = ts.get("task_backend_wrong_repo")
    assert saved.state == TaskState.RUNNING
    assert saved.proof_ids == ["proof_wrong_repo"]
    assert graph_stage_status(saved, "backend_no_op_route_proof") == StageStatus.BLOCKED
    assert "command_proof_repo_mismatch" in saved.risk_flags


def test_bridge_archive_stage_wrong_page_proof_does_not_advance():
    ts = TaskStore()
    proofs = ProofStore()
    task = make_task_with_id("task_bridge_wrong_command")
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_bridge_archive_regression"
    task.affected_repos = ["EterniaLauncher"]
    task.stages = [
        TaskStage(
            id="stage_bridge_archive_regression",
            title="Preserve Mission Control runtime root/profile snapshot and archive bridge behavior",
            objective="Run existing bridge/archive regression coverage to ensure the UI upgrade does not alter current Harness runtime root/profile snapshot/archive semantics.",
            status=StageStatus.IMPLEMENTING,
            acceptance_criteria=[
                "Existing Mission Control bridge tests still pass.",
                "Existing snapshot/archive behavior tests still pass.",
            ],
            test_plan=[
                "flutter test test/features/mission_control/mission_control_snapshot_test.dart test/features/mission_control/mission_control_bridge_test.dart",
            ],
        )
    ]
    ts.create(task)
    engine = TickEngine(
        task_store=ts,
        proof_store=proofs,
        persona_runtime=RequestBridgeStageWrongPageProofRuntime(),
        proof_runner=PassingCommandMetadataProofRunner(proofs),
    )

    res = engine.tick_once()

    assert res.actions_taken[0].ok
    saved = ts.get("task_bridge_wrong_command")
    assert saved.state == TaskState.RUNNING
    assert saved.current_stage_id == "stage_bridge_archive_regression"
    assert graph_stage_status(saved, "stage_bridge_archive_regression") == StageStatus.IMPLEMENTING
    assert saved.proof_ids == ["proof_wrong_stage_command"]
    assert "command_proof_stage_mismatch" in saved.risk_flags


def test_bridge_archive_stage_request_qa_review_rejects_wrong_existing_page_proof_then_repairs():
    ts = TaskStore()
    proofs = ProofStore()
    task = make_task_with_id("task_bridge_wrong_qa_handoff")
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_bridge_archive_regression"
    task.affected_repos = ["EterniaLauncher"]
    task.proof_ids = ["proof_page_bridge"]
    task.risk_flags = ["command_proof_stage_mismatch"]
    task.stages = [
        TaskStage(
            id="stage_bridge_archive_regression",
            title="Preserve Mission Control runtime root/profile snapshot and archive bridge behavior",
            objective="Run existing bridge/archive regression coverage to ensure the UI upgrade does not alter current Harness runtime root/profile snapshot/archive semantics.",
            status=StageStatus.IMPLEMENTING,
            acceptance_criteria=[
                "Existing Mission Control bridge tests still pass.",
                "Existing snapshot/archive behavior tests still pass.",
            ],
            test_plan=[
                "flutter test test/features/mission_control/mission_control_snapshot_test.dart test/features/mission_control/mission_control_bridge_test.dart",
            ],
        )
    ]
    proofs.attach(
        Proof(
            id="proof_page_bridge",
            task_id=task.id,
            stage_id="stage_bridge_archive_regression",
            type=ProofType.TEST_RUN,
            title="stale page proof",
            path_or_value="page-proof.log",
            created_by="harness",
            created_at=now(),
            metadata={
                "status": "passed",
                "exit_code": 0,
                "command": "flutter test test/features/mission_control/mission_control_page_test.dart",
                "commands_requested": 1,
                "workdir_label": "EterniaLauncher",
            },
            redaction_status="safe",
        )
    )
    ts.create(task)
    runtime = RequestQaWithWrongBridgeProofThenCorrectProofRuntime()
    engine = TickEngine(
        task_store=ts,
        proof_store=proofs,
        persona_runtime=runtime,
        proof_runner=PassingCommandMetadataProofRunner(proofs),
    )

    res = engine.tick_once(task_id="task_bridge_wrong_qa_handoff")

    assert res.actions_taken[0].ok
    assert res.actions_taken[0].payload["decision"] == "request_test_run"
    assert runtime.calls == 2
    saved = ts.get("task_bridge_wrong_qa_handoff")
    assert saved.state == TaskState.RUNNING
    assert saved.current_stage_id == "verify"
    assert saved.stages[0].status == StageStatus.READY_FOR_QA
    assert saved.proof_ids == ["proof_page_bridge", "proof_wrong_stage_command"]
    repaired = proofs.get("proof_wrong_stage_command")
    assert "mission_control_bridge_test.dart" in repaired.metadata["command"]
    assert "mission_control_snapshot_test.dart" in repaired.metadata["command"]
    assert "command_proof_stage_mismatch" not in saved.risk_flags


def test_request_test_run_autocorrects_later_stage_to_unambiguous_current_stage_command():
    ts = TaskStore()
    proofs = ProofStore()
    task = make_task_with_id("task_skip_stage_guard")
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_bridge_archive_regression"
    task.affected_repos = ["EterniaLauncher"]
    task.stages = [
        TaskStage(
            id="stage_bridge_archive_regression",
            title="Bridge regression",
            objective="Collect Mission Control bridge proof.",
            status=StageStatus.IMPLEMENTING,
            test_plan=[
                "flutter test test/features/mission_control/mission_control_snapshot_test.dart test/features/mission_control/mission_control_bridge_test.dart"
            ],
        ),
        TaskStage(
            id="stage_launcher_analyze_and_visual_proof",
            title="Analyze and visual proof",
            objective="Collect analyze and screenshot proof.",
            status=StageStatus.READY,
            test_plan=["flutter analyze lib/features/mission_control"],
        ),
    ]
    ts.create(task)
    engine = TickEngine(
        task_store=ts,
        proof_store=proofs,
        persona_runtime=RequestLaterStageProofRuntime(),
        proof_runner=PassingCommandMetadataProofRunner(proofs),
    )

    res = engine.tick_once(task_id="task_skip_stage_guard")

    assert res.actions_taken[0].ok
    saved = ts.get("task_skip_stage_guard")
    assert saved.state == TaskState.RUNNING
    assert saved.current_stage_id == "stage_launcher_analyze_and_visual_proof"
    assert saved.stages[0].status == StageStatus.READY_FOR_QA
    assert saved.stages[1].status == StageStatus.READY
    assert saved.proof_ids == ["proof_wrong_stage_command"]
    proof = proofs.get(saved.proof_ids[0])
    assert proof.stage_id == "stage_bridge_archive_regression"
    assert proof.metadata["command"] == "flutter test test/features/mission_control/mission_control_snapshot_test.dart test/features/mission_control/mission_control_bridge_test.dart"
    events = EventLog().for_task("task_skip_stage_guard", limit=20)
    autocorrect_events = [
        event for event in events
        if event.payload.get("source") == "request_test_run_stage_autocorrect"
    ]
    assert autocorrect_events[-1].payload["from_stage_id"] == "stage_launcher_analyze_and_visual_proof"
    assert autocorrect_events[-1].payload["to_stage_id"] == "stage_bridge_archive_regression"


def test_downstream_visual_block_autocorrects_to_current_stage_command_proof():
    ts = TaskStore()
    proofs = ProofStore()
    task = make_task_with_id("task_visual_block_before_bridge")
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_bridge_archive_regression"
    task.affected_repos = ["EterniaLauncher"]
    task.stages = [
        TaskStage(
            id="stage_bridge_archive_regression",
            title="Bridge regression",
            objective="Collect Mission Control bridge proof.",
            status=StageStatus.IMPLEMENTING,
            test_plan=[
                "flutter test test/features/mission_control/mission_control_snapshot_test.dart test/features/mission_control/mission_control_bridge_test.dart"
            ],
        ),
        TaskStage(
            id="stage_launcher_analyze_and_visual_proof",
            title="Analyze and visual proof",
            objective="Collect analyze and screenshot proof.",
            status=StageStatus.READY,
            test_plan=["flutter analyze lib/features/mission_control"],
        ),
    ]
    ts.create(task)
    engine = TickEngine(
        task_store=ts,
        proof_store=proofs,
        persona_runtime=BlockOnDownstreamVisualBeforeCurrentProofRuntime(),
        proof_runner=PassingCommandMetadataProofRunner(proofs),
    )

    res = engine.tick_once(task_id="task_visual_block_before_bridge")

    assert res.actions_taken[0].ok
    assert res.actions_taken[0].payload["decision"] == "request_test_run"
    saved = ts.get("task_visual_block_before_bridge")
    assert saved.state == TaskState.RUNNING
    assert saved.current_stage_id == "stage_launcher_analyze_and_visual_proof"
    assert saved.stages[0].status == StageStatus.READY_FOR_QA
    assert saved.stages[1].status == StageStatus.READY
    assert saved.proof_ids == ["proof_wrong_stage_command"]
    proof = proofs.get(saved.proof_ids[0])
    assert proof.stage_id == "stage_bridge_archive_regression"
    assert "mission_control_snapshot_test.dart" in proof.metadata["command"]
    assert "mission_control_bridge_test.dart" in proof.metadata["command"]
    events = EventLog().for_task("task_visual_block_before_bridge", limit=20)
    assert any(event.payload.get("source") == "downstream_visual_block_autocorrect" for event in events)


def test_request_test_run_cannot_skip_incomplete_current_stage_without_unambiguous_command():
    ts = TaskStore()
    task = make_task_with_id("task_skip_stage_guard_ambiguous")
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_bridge_archive_regression"
    task.affected_repos = ["EterniaLauncher"]
    task.stages = [
        TaskStage(
            id="stage_bridge_archive_regression",
            title="Bridge regression",
            objective="Collect Mission Control bridge proof.",
            status=StageStatus.IMPLEMENTING,
            test_plan=["Choose the narrowest bridge proof after inspecting current files."],
        ),
        TaskStage(
            id="stage_launcher_analyze_and_visual_proof",
            title="Analyze and visual proof",
            objective="Collect analyze and screenshot proof.",
            status=StageStatus.READY,
            test_plan=["flutter analyze lib/features/mission_control"],
        ),
    ]
    ts.create(task)
    engine = TickEngine(task_store=ts, persona_runtime=RequestLaterStageProofRuntime())

    res = engine.tick_once(task_id="task_skip_stage_guard_ambiguous")

    assert not res.actions_taken[0].ok
    saved = ts.get("task_skip_stage_guard_ambiguous")
    assert saved.state == TaskState.RUNNING
    assert saved.current_stage_id == "stage_bridge_archive_regression"
    assert saved.stages[0].status == StageStatus.IMPLEMENTING
    assert saved.proof_ids == []


def test_smoke_marker_proof_reroutes_back_to_incomplete_product_edit_stage():
    ts = TaskStore()
    proofs = ProofStore()
    task = make_task_with_id("task_dm_bubble_smoke_reroute")
    task.title = "Mission Control DM bubble terminal rows"
    task.description = "Upgrade Launcher Mission Control event rows into compact DM bubbles."
    task.state = TaskState.RUNNING
    task.current_stage_id = "launcher_contract_smoke"
    task.affected_repos = ["EterniaLauncher"]
    task.stages = [
        TaskStage(
            id="mc_terminal_dm_bubble_rows",
            title="Implement compact Mission Control terminal DM bubble event rows",
            objective="Replace heavy block cards with compact expandable DM bubble rows.",
            status=StageStatus.READY,
            affected_paths=["lib/features/mission_control/", "test/features/mission_control/"],
            acceptance_criteria=["Widget tests cover bubble row rendering and expansion behavior."],
            test_plan=["flutter test test/features/mission_control"],
        ),
        TaskStage(
            id="launcher_contract_smoke",
            title="Launcher Contract Smoke",
            objective="Collect placeholder command proof.",
            status=StageStatus.IMPLEMENTING,
            test_plan=["python -c \"print('launcher_contract_smoke contract_packet_consumed backend_proof_consumed')\""],
        ),
    ]
    ts.create(task)
    engine = TickEngine(
        task_store=ts,
        proof_store=proofs,
        persona_runtime=RequestLauncherSmokeMarkerRuntime(),
        proof_runner=PassingCommandMetadataProofRunner(proofs),
    )

    res = engine.tick_once(task_id="task_dm_bubble_smoke_reroute")

    assert res.actions_taken[0].ok
    saved = ts.get("task_dm_bubble_smoke_reroute")
    assert saved.state == TaskState.RUNNING
    assert saved.current_stage_id == "mc_terminal_dm_bubble_rows"
    assert graph_stage_status(saved, "mc_terminal_dm_bubble_rows") == StageStatus.IMPLEMENTING
    assert graph_stage_status(saved, "launcher_contract_smoke") == StageStatus.READY_FOR_QA
    assert saved.proof_ids == ["proof_wrong_stage_command"]
    assert "command_proof_stage_mismatch" in saved.risk_flags


def test_tick_collects_command_proof_in_harness_repo_alias_when_no_explicit_workdir():
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    task.affected_repos = ["agent-runtime-harness"]
    ts.create(task)
    engine = TickEngine(task_store=ts, persona_runtime=RequestTestRunRuntime())

    res = engine.tick_once()

    assert res.actions_taken[0].ok
    saved = ts.get("task_1")
    proof = engine.proof_store.get(saved.proof_ids[0])
    from agent_runtime import paths
    artifact = paths.store_root() / proof.path_or_value
    text = artifact.read_text(encoding="utf-8")
    repo_root = Path(__file__).resolve().parents[2]
    assert f"workdir: <workdir:{repo_root.name}_" in text
    assert proof.metadata["workdir_is_harness_worktree"] is True
    assert proof.metadata["workdir_head_state"] == "detached"
    assert proof.metadata["exit_code"] == 0


def test_tick_collects_command_proof_in_hermes_agent_alias_when_no_explicit_workdir():
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    task.affected_repos = ["hermes-agent"]
    ts.create(task)
    engine = TickEngine(task_store=ts, persona_runtime=RequestTestRunRuntime())

    res = engine.tick_once()

    assert res.actions_taken[0].ok
    saved = ts.get("task_1")
    proof = engine.proof_store.get(saved.proof_ids[0])
    from agent_runtime import paths
    artifact = paths.store_root() / proof.path_or_value
    text = artifact.read_text(encoding="utf-8")
    repo_root = Path(__file__).resolve().parents[2]
    assert f"workdir: <workdir:{repo_root.name}_" in text
    assert proof.metadata["workdir_is_harness_worktree"] is True
    assert proof.metadata["workdir_head_state"] == "detached"
    assert proof.metadata["exit_code"] == 0


def test_tick_opens_incident_when_command_proof_affected_repo_is_missing(tmp_path):
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    task.affected_repos = [str(tmp_path / "missing-product-repo")]
    ts.create(task)
    engine = TickEngine(task_store=ts, persona_runtime=RequestTestRunRuntime())

    res = engine.tick_once()

    assert not res.actions_taken[0].ok
    assert ts.get("task_1").state == TaskState.RUNNING
    incident = engine.incident_store.list_open()[0]
    assert incident.kind == "harness_action_failure"
    run = engine.run_store.get(res.actions_taken[0].payload["run_id"])
    assert run.state == RunState.FAILED
    assert "affected repo workdir" in run.error["message"]
    assert str(tmp_path) not in run.error["message"]


def test_tick_opens_incident_when_command_proof_affected_repo_is_ambiguous_alias():
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    task.affected_repos = ["mission-control"]
    ts.create(task)
    engine = TickEngine(task_store=ts, persona_runtime=RequestTestRunRuntime())

    res = engine.tick_once()

    assert not res.actions_taken[0].ok
    incident = engine.incident_store.list_open()[0]
    assert incident.kind == "harness_action_failure"
    run = engine.run_store.get(res.actions_taken[0].payload["run_id"])
    assert run.state == RunState.FAILED
    assert "affected repo workdir" in run.error["message"]


def test_tick_opens_incident_when_command_proof_affected_repo_is_path_like_alias():
    for index, repo_alias in enumerate(
        ("agent/runtime/harness", "agent.runtime.harness", "agent@runtime@harness", ".", "./", "agent_runtime/.."),
        start=1,
    ):
        ts = TaskStore()
        task = make_task()
        task.id = f"task_path_like_{index}"
        task.state = TaskState.RUNNING
        task.current_stage_id = "stage_1"
        task.affected_repos = [repo_alias]
        ts.create(task)
        engine = TickEngine(task_store=ts, persona_runtime=RequestTestRunRuntime())

        res = engine.tick_once()

        assert not res.actions_taken[0].ok
        incident = engine.incident_store.list_open()[0]
        assert incident.kind == "harness_action_failure"
        run = engine.run_store.get(res.actions_taken[0].payload["run_id"])
        assert run.state == RunState.FAILED
        assert "affected repo workdir" in run.error["message"]


def test_request_test_run_materializes_stage_and_routes_proof_handoff_to_implementation_qa(tmp_path):
    ts = TaskStore()
    task = attach_blueprint(
        make_task(),
        "two_agent_build_verify",
        {
            "builder": "persona:dev",
            "verifier": "persona:qa",
        },
    )
    task.state = TaskState.RUNNING
    task.acceptance_criteria = ["smoke-ok proof exists"]
    ts.create(task)
    runtime = RequestTestThenHandoffRuntime()
    engine = TickEngine(task_store=ts, persona_runtime=runtime)
    engine.command_workdir = tmp_path

    first = engine.tick_once()
    after_proof = ts.get("task_1")

    assert first.actions_taken[0].ok
    assert after_proof.state == TaskState.RUNNING
    assert after_proof.current_stage_id == "verify"
    assert [stage.id for stage in after_proof.mission_plan.stages] == ["implement", "verify"]
    assert graph_stage_status(after_proof, "implement") == StageStatus.PASSED
    assert len(after_proof.proof_ids) == 1
    assert runtime.calls == 1
    assert engine.state_machine.next_action(after_proof).type == HarnessActionType.RUN_SLOT


class FailingRequestTestRunRuntime:
    def run_tick(self, persona, ctx, *, run):
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="run failing proof",
            rationale="Need deterministic command proof before QA.",
            payload={"stage_id": "implement", "commands": ["python -c 'import sys; sys.exit(7)'"]},
        )


class FailingThenPassingRetryRuntime:
    def __init__(self):
        self.contexts = []

    def run_tick(self, persona, ctx, *, run):
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        self.contexts.append(ctx)
        if len(self.contexts) == 1:
            return AgentDecision(
                type=DecisionType.REQUEST_TEST_RUN,
                summary="run failing proof",
                rationale="Need deterministic command proof before QA.",
                payload={"stage_id": "implement", "commands": ["python -c 'import sys; sys.exit(7)'"]},
            )
        failed_id = ctx.proof_ids[-1]
        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary=f"retry after failed proof {failed_id}",
            rationale=f"Reuse attached failed proof {failed_id} and run the corrected bounded command.",
            payload={"stage_id": "implement", "commands": ["printf 'retry-ok\\n'"]},
        )


class FailingThenPassingAutoAttachedRetryRuntime:
    def __init__(self):
        self.contexts = []

    def run_tick(self, persona, ctx, *, run):
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        self.contexts.append(ctx)
        if len(self.contexts) == 1:
            return AgentDecision(
                type=DecisionType.REQUEST_TEST_RUN,
                summary="run failing proof",
                rationale="Need deterministic command proof before QA.",
                payload={"stage_id": "implement", "commands": ["python -c 'import sys; sys.exit(7)'"]},
            )
        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="retry bounded proof after attached failure context",
            rationale="Use the attached failure context and run the corrected bounded command.",
            payload={"stage_id": "implement", "commands": ["printf 'retry-ok\\n'"]},
        )


class FailingTwiceRetryRuntime:
    def __init__(self):
        self.contexts = []

    def run_tick(self, persona, ctx, *, run):
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        self.contexts.append(ctx)
        if len(self.contexts) == 1 or not ctx.proof_ids:
            summary = "run failing proof"
            rationale = "Need deterministic command proof before QA."
        else:
            failed_id = ctx.proof_ids[-1]
            summary = f"retry after failed proof {failed_id}"
            rationale = f"Reuse attached failed proof {failed_id} and run one bounded retry."
        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary=summary,
            rationale=rationale,
            payload={"stage_id": "implement", "commands": ["python -c 'import sys; sys.exit(7)'"]},
        )


class FailingThenBlockingRuntime:
    def __init__(self):
        self.contexts = []

    def run_tick(self, persona, ctx, *, run):
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        self.contexts.append(ctx)
        if len(self.contexts) == 1:
            return AgentDecision(
                type=DecisionType.REQUEST_TEST_RUN,
                summary="run failing proof",
                rationale="Need deterministic command proof before QA.",
                payload={"stage_id": "implement", "commands": ["python -c 'import sys; sys.exit(7)'"]},
            )
        return AgentDecision(
            type=DecisionType.BLOCK,
            summary="blocked after failed proof",
            rationale="The failed proof is still unresolved and the environment has not changed.",
            payload={
                "reason": "previous failed proof remains unresolved",
                "log_ref": {"path": "events.jsonl", "line": 1, "summary": "previous failed proof remains unresolved"},
            },
        )


def test_request_test_run_failed_proof_stays_in_dev_for_fix_pass(tmp_path):
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    ts.create(task)
    engine = TickEngine(task_store=ts, persona_runtime=FailingRequestTestRunRuntime())
    engine.command_workdir = tmp_path

    res = engine.tick_once()
    saved = ts.get("task_1")

    assert res.actions_taken[0].ok
    assert saved.state == TaskState.RUNNING
    assert graph_stage_status(saved, "implement") == StageStatus.REWORK
    assert len(saved.proof_ids) == 1
    failed_proofs = engine.proof_store.list_for_task("task_1")
    assert len(failed_proofs) == 1
    assert failed_proofs[0].metadata["status"] == "failed"
    assert saved.proof_ids == [failed_proofs[0].id]
    stage_state = saved.harness_self_heal["stages"]["implement"]
    assert stage_state["last_failed_proof_ids"] == [failed_proofs[0].id]
    assert engine.state_machine.next_action(saved).type == HarnessActionType.RUN_SLOT


def test_failed_command_proof_is_retry_context_and_clears_after_pass(tmp_path):
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    ts.create(task)
    runtime = FailingThenPassingRetryRuntime()
    engine = TickEngine(task_store=ts, persona_runtime=runtime)
    engine.command_workdir = tmp_path

    first = engine.tick_once()
    after_failure = ts.get("task_1")
    second = engine.tick_once()
    saved = ts.get("task_1")

    assert first.actions_taken[0].ok
    assert second.actions_taken[0].ok
    assert len(runtime.contexts[1].proof_ids) == 1
    assert runtime.contexts[1].proof_ids[0] == after_failure.proof_ids[0]
    assert runtime.contexts[1].autonomy_packet["failed_proof_ids"] == [after_failure.proof_ids[0]]
    assert saved.state == TaskState.RUNNING
    assert len(saved.proof_ids) == 2
    assert [engine.proof_store.get(proof_id).metadata["status"] for proof_id in saved.proof_ids] == ["failed", "passed"]
    assert "last_failed_proof_ids" not in saved.harness_self_heal["stages"]["implement"]


def test_failed_command_proof_retry_auto_attaches_context_when_model_omits_id(tmp_path):
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    ts.create(task)
    runtime = FailingThenPassingAutoAttachedRetryRuntime()
    engine = TickEngine(task_store=ts, persona_runtime=runtime)
    engine.command_workdir = tmp_path

    first = engine.tick_once()
    after_failure = ts.get("task_1")
    second = engine.tick_once()
    saved = ts.get("task_1")

    assert first.actions_taken[0].ok
    assert second.actions_taken[0].ok
    assert runtime.contexts[1].autonomy_packet["failed_proof_ids"] == [after_failure.proof_ids[0]]
    assert saved.state == TaskState.RUNNING
    assert [engine.proof_store.get(proof_id).metadata["status"] for proof_id in saved.proof_ids] == ["failed", "passed"]
    events = [event for event in EventLog().for_task("task_1", limit=50) if event.payload.get("step") == "failed_proof_auto_attached"]
    assert events
    assert events[-1].payload["failed_proof_ids"] == [after_failure.proof_ids[0]]


def test_second_same_stage_failed_command_proof_routes_to_neko(tmp_path):
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    ts.create(task)
    engine = TickEngine(task_store=ts, persona_runtime=FailingTwiceRetryRuntime())
    engine.command_workdir = tmp_path

    first = engine.tick_once()
    second = engine.tick_once()
    saved = ts.get("task_1")

    assert first.actions_taken[0].ok
    assert second.actions_taken[0].ok
    assert [engine.proof_store.get(proof_id).metadata["status"] for proof_id in saved.proof_ids] == ["failed", "failed"]
    assert saved.harness_self_heal["stages"]["implement"]["last_failed_proof_ids"]
    next_action = engine.state_machine.next_action(saved)
    assert next_action.type == HarnessActionType.RUN_SLOT
    assert "Neko" in next_action.reason


def test_dev_block_after_failed_proof_routes_to_neko_self_heal(tmp_path):
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    ts.create(task)
    runtime = FailingThenBlockingRuntime()
    engine = TickEngine(task_store=ts, persona_runtime=runtime)
    engine.command_workdir = tmp_path

    first = engine.tick_once()
    second = engine.tick_once()
    saved = ts.get("task_1")

    assert first.actions_taken[0].ok
    assert second.actions_taken[0].ok
    assert saved.state == TaskState.RUNNING
    assert saved.harness_self_heal["stages"]["implement"]["last_failed_proof_ids"]
    next_action = engine.state_machine.next_action(saved)
    assert next_action.type == HarnessActionType.RUN_SLOT
    assert next_action.slot_id == "neko_supervisor"


def test_request_test_run_failed_proof_advances_explicit_red_stage(tmp_path):
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    task.stages = [
        TaskStage(id="stage_1", title="RED Launcher tests", objective="prove tests fail before implementation", status=StageStatus.IMPLEMENTING, test_plan=["pytest red"]),
        TaskStage(id="stage_2", title="Implement Launcher tests", objective="make red tests pass", status=StageStatus.READY, test_plan=["pytest green"]),
    ]
    ts.create(task)
    engine = TickEngine(task_store=ts, persona_runtime=FailingRequestTestRunRuntime())
    engine.command_workdir = tmp_path

    res = engine.tick_once()
    saved = ts.get("task_1")

    assert res.actions_taken[0].ok
    assert engine.proof_store.get(saved.proof_ids[0]).metadata["status"] == "failed"
    assert saved.state == TaskState.RUNNING
    assert saved.current_stage_id == "stage_1"
    assert [stage.status for stage in saved.stages] == [StageStatus.REWORK, StageStatus.READY, StageStatus.READY]


class CancellingProofRunner:
    def __init__(self, run_store: RunStore, proof_store: ProofStore):
        self.run_store = run_store
        self.proof_store = proof_store

    def run_commands(self, task, *, stage_id, run_id, actor, commands):
        self.run_store.cancel(run_id, reason="operator requested cancellation")
        proof = Proof(
            id="proof_cancel_race",
            task_id=task.id,
            stage_id=stage_id,
            type=ProofType.TEST_RUN,
            title="cancel race proof",
            path_or_value="cancel-race",
            created_by=actor,
            created_at=now(),
            metadata={"status": "passed", "exit_code": 0},
            redaction_status="safe",
        )
        self.proof_store.attach(proof)
        return [proof]


def test_request_test_run_does_not_handoff_after_run_cancelled_during_proof_collection(tmp_path):
    ts = TaskStore()
    runs = RunStore()
    proofs = ProofStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    ts.create(task)
    engine = TickEngine(
        task_store=ts,
        run_store=runs,
        proof_store=proofs,
        persona_runtime=RequestTestRunRuntime(),
        proof_runner=CancellingProofRunner(runs, proofs),
    )

    res = engine.tick_once()
    saved = ts.get("task_1")

    assert not res.actions_taken[0].ok
    assert res.actions_taken[0].summary == "run reached terminal state during proof collection"
    assert saved.state == TaskState.RUNNING
    assert saved.proof_ids == []
    assert runs.list_for_task("task_1")[0].state == RunState.CANCELLED


class HighBudgetDevRuntime:
    def run_tick(self, persona, ctx, *, run):
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        run.llm = {"api_calls": 21, "total_tokens": 800000}
        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="run focused tests",
            rationale="Need deterministic command proof before QA handoff.",
            payload={"stage_id": "implement", "commands": ["printf 'proof-ok\\n'"]},
        )


class InspectContinuationSessionRuntime:
    def __init__(self, session_id_to_write="session_next"):
        self.seen_session_ids = []
        self.seen_run_limits = []
        self.seen_progress = []
        self.session_id_to_write = session_id_to_write

    def run_tick(self, persona, ctx, *, run):
        self.seen_session_ids.append((persona.id, run.session_id))
        self.seen_run_limits.append((persona.id, run.session_id, run.max_total_tokens))
        self.seen_progress.append(dict(run.progress or {}))
        run.session_id = self.session_id_to_write
        from agent_runtime.decision_schema import AgentDecision, DecisionType
        return AgentDecision(type=DecisionType.PROPOSE_ACCEPTANCE, summary="pm", rationale="r", payload={"objective":"obj","acceptance_criteria":["ok"]})


def test_followup_neko_mission_lead_steering_reuses_previous_neko_session():
    ts = TaskStore(); ts.create(make_task())
    runs = RunStore()
    old = runs.open_run("neko_supervisor", "task_1", stage_id=None, session_id="session_neko_prior")
    runs.close_run(old.id, state=RunState.FAILED, error={"type": "test"})
    runtime = InspectContinuationSessionRuntime()

    res = TickEngine(task_store=ts, run_store=runs, persona_runtime=runtime).tick_once()

    assert res.actions_taken[0].ok
    assert runtime.seen_session_ids == [("neko_supervisor", "session_neko_prior")]


def test_followup_qa_and_neko_steering_reuse_prior_persona_sessions():
    ts = TaskStore()
    qa_task = make_task_with_id("task_qa")
    qa_task.state = TaskState.RUNNING
    qa_task.current_stage_id = "qa_release"
    qa_task.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title=qa_task.title, objective=qa_task.description),
        current_stage_id="qa_release",
        blueprint_id="neko_dev_qa_basic",
        stages=[
            MissionPlanStage(
                id="qa_release",
                title="QA Release",
                    objective="Verify mission.",
                    owner="qa",
                    owner_slot="qa",
                    repo="hermes-agent",
                    kind="qa_verdict",
                    status=StageStatus.READY,
            )
        ],
    )
    qa_task.risk_flags = []
    ts.create(qa_task)
    neko_task = make_task_with_id("task_neko")
    neko_task.state = TaskState.BLOCKED
    neko_task.open_incident_ids = ["inc_neko"]
    ts.create(neko_task)
    runs = RunStore()
    old_qa = runs.open_run("qa", "task_qa", stage_id="qa_release", session_id="session_qa_prior")
    runs.close_run(old_qa.id, state=RunState.FAILED, error={"type": "test"})
    old_neko = runs.open_run("neko_supervisor", "task_neko", stage_id=None, session_id="session_neko_prior")
    runs.close_run(old_neko.id, state=RunState.FAILED, error={"type": "test"})
    runtime = InspectContinuationSessionRuntime()

    TickEngine(task_store=ts, run_store=runs, persona_runtime=runtime).tick_once(task_id="task_qa")
    TickEngine(task_store=ts, run_store=runs, persona_runtime=runtime).tick_once(task_id="task_neko")

    assert ("qa", "session_qa_prior") in runtime.seen_session_ids
    assert ("neko_supervisor", "session_neko_prior") in runtime.seen_session_ids


class BudgetExceededRuntime:
    def __init__(self, message="live run budget exceeded: wall_seconds=1.0", session_id: str | None = None):
        self.message = message
        self.session_id = session_id

    def run_tick(self, persona, ctx, *, run):
        run.session_id = self.session_id or run.session_id
        raise RunBudgetExceeded(self.message, session_id=run.session_id)


class NekoBudgetApprovalRuntime:
    def __init__(self, *, decision_type="resolve_incident"):
        self.seen = []
        self.decision_type = decision_type

    def run_tick(self, persona, ctx, *, run):
        self.seen.append((persona.id, list(ctx.incident_records)))
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        incident_id = ctx.incident_records[0]["id"]
        if self.decision_type == "propose_acceptance":
            return AgentDecision(
                type=DecisionType.PROPOSE_ACCEPTANCE,
                summary="scope bounded Dev continuation",
                rationale="Neko tightens the recovery scope for the budget-limited Dev run but uses the fresh-mission decision type.",
                payload={
                    "objective": "continue the same Dev session only for the bounded backend proof slice",
                    "acceptance_criteria": ["same-session Dev continuation is approved", "focused proof is collected"],
                    "affected_repos": ["EterniaBackend"],
                },
            )
        return AgentDecision(
            type=DecisionType.RESOLVE_INCIDENT,
            summary="approve bounded Dev continuation",
            rationale="The Dev run hit the live budget guard but has a safe session_id, so Neko approves one same-session continuation and routes back to Dev.",
            payload={
                "incident_id": incident_id,
                "resolution": "approve same-session Dev continuation with bounded proof focus",
                "next_state": "dev_implementing",
            },
        )


class BudgetThenNekoApprovalRuntime:
    def __init__(self):
        self.seen = []

    def run_tick(self, persona, ctx, *, run):
        self.seen.append(persona.id)
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        if persona.id in {"dev", "backend_dev"}:
            run.session_id = "session_budget"
            raise RunBudgetExceeded("live run budget exceeded: wall_seconds=1.0", session_id=run.session_id)
        incident_id = ctx.incident_records[0]["id"]
        return AgentDecision(
            type=DecisionType.RESOLVE_INCIDENT,
            summary="approve bounded Dev continuation",
            rationale="The Dev run hit the live budget guard but has a safe session_id.",
            payload={
                "incident_id": incident_id,
                "resolution": "approve same-session Dev continuation with bounded proof focus",
                "next_state": "dev_implementing",
            },
        )


def test_budget_exceeded_runtime_opens_budget_incident_without_advancing_task():
    ts = TaskStore()
    runs = RunStore()
    incidents = IncidentStore()
    ts.create(make_task())
    engine = TickEngine(task_store=ts, run_store=runs, incident_store=incidents, persona_runtime=BudgetExceededRuntime(session_id="session_budget"))

    res = engine.tick_once()

    assert not res.actions_taken[0].ok
    assert ts.get("task_1").state == TaskState.CREATED
    run = runs.list_for_task("task_1")[0]
    assert run.state == RunState.WAITING_ON_APPROVAL
    assert run.error["type"] == "run_budget_exceeded"
    assert run.error["summary"] == "live run budget exceeded: wall_seconds=1.0"
    assert run.progress["next_expected"] == "approve_budget_continuation"
    assert run.session_id == "session_budget"
    incident = incidents.list_open()[0]
    assert incident.kind == "run_budget_exceeded"
    assert incident.run_id == run.id
    assert incident.id in ts.get("task_1").open_incident_ids


def test_run_until_settled_continues_to_neko_for_budget_approval_incident():
    ts = TaskStore()
    runs = RunStore()
    incidents = IncidentStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    task.stages = [
        TaskStage(
            id="stage_1",
            title="Backend proof",
            objective="finish backend proof",
            status=StageStatus.IMPLEMENTING,
            test_plan=["pytest focused"],
        )
    ]
    ts.create(task)
    runtime = BudgetThenNekoApprovalRuntime()

    result = TickEngine(task_store=ts, run_store=runs, incident_store=incidents, persona_runtime=runtime).run_until_settled(
        task_id="task_1",
        max_actions=2,
    )

    assert [action.action.type for action in result.actions_taken] == [
        HarnessActionType.RUN_SLOT,
        HarnessActionType.RUN_SLOT,
    ]
    assert runtime.seen == ["backend_dev", "neko_supervisor"]
    assert result.stop_reason == "max_actions"
    assert not incidents.list_open()
    waiting_run = next(run for run in runs.list_for_task("task_1") if run.persona_id == "backend_dev")
    assert waiting_run.progress["approved_for_continuation"] is True


def test_dev_budget_exceeded_routes_to_neko_approval_then_dev_same_session_continuation():
    _assert_budget_approval_continues_same_session(NekoBudgetApprovalRuntime())


def test_neko_budget_acceptance_scope_is_coerced_to_same_session_continuation():
    _assert_budget_approval_continues_same_session(
        NekoBudgetApprovalRuntime(decision_type="propose_acceptance"),
        clear_task_incident_ids=True,
    )


def _assert_budget_approval_continues_same_session(neko_runtime, *, clear_task_incident_ids: bool = False):
    ts = TaskStore()
    runs = RunStore()
    incidents = IncidentStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    task.stages = [
        TaskStage(
            id="stage_1",
            title="Implement bounded controls",
            objective="finish backend proof",
            status=StageStatus.IMPLEMENTING,
            test_plan=["pytest focused"],
        )
    ]
    ts.create(task)

    first = TickEngine(task_store=ts, run_store=runs, incident_store=incidents, persona_runtime=BudgetExceededRuntime(session_id="session_budget")).tick_once(task_id="task_1")
    waiting_run = runs.get(first.actions_taken[0].payload["run_id"])
    assert waiting_run.persona_id == "dev"
    assert waiting_run.state == RunState.WAITING_ON_APPROVAL
    assert incidents.list_open()[0].id in ts.get("task_1").open_incident_ids
    if clear_task_incident_ids:
        stale_task = ts.get("task_1")
        stale_task.open_incident_ids = []
        ts.update(stale_task, actor="test", reason="simulate live task/incident linkage drift")

    approval = TickEngine(task_store=ts, run_store=runs, incident_store=incidents, persona_runtime=neko_runtime).tick_once(task_id="task_1")
    assert approval.actions_taken[0].ok
    assert approval.actions_taken[0].action.type == HarnessActionType.RUN_SLOT
    assert neko_runtime.seen[0][0] == "neko_supervisor"
    approved_run = runs.get(waiting_run.id)
    assert approved_run.progress["approved_for_continuation"] is True
    assert approved_run.error["approved_for_continuation"] is True
    assert not incidents.list_open()

    continuation_runtime = InspectContinuationSessionRuntime()
    retry = TickEngine(task_store=ts, run_store=runs, incident_store=incidents, persona_runtime=continuation_runtime).tick_once(task_id="task_1")
    assert retry.actions_taken[0].ok
    assert retry.actions_taken[0].action.type == HarnessActionType.RUN_SLOT
    assert continuation_runtime.seen_session_ids == [("dev", "session_budget")]


def test_budget_approval_then_tick_continues_same_session():
    ts = TaskStore()
    runs = RunStore()
    incidents = IncidentStore()
    ts.create(make_task())
    engine = TickEngine(task_store=ts, run_store=runs, incident_store=incidents, persona_runtime=BudgetExceededRuntime(session_id="session_budget"))
    res = engine.tick_once()
    waiting_run = runs.get(res.actions_taken[0].payload["run_id"])

    approved = runs.approve_continuation(waiting_run.id)
    for incident in incidents.list_open():
        if incident.run_id == waiting_run.id:
            incidents.close(incident.id, reason="operator approved same-session continuation")
    runtime = InspectContinuationSessionRuntime()
    retry = TickEngine(task_store=ts, run_store=runs, incident_store=incidents, persona_runtime=runtime).tick_once(task_id="task_1")

    assert approved.progress["approved_for_continuation"] is True
    assert retry.actions_taken[0].ok
    assert runtime.seen_session_ids == [("neko_supervisor", "session_budget")]


def test_budget_approval_continuation_gets_incremental_token_headroom():
    ts = TaskStore()
    runs = RunStore()
    incidents = IncidentStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    task.stages = [
        TaskStage(
            id="stage_1",
            title="Implement UI",
            objective="finish launcher proof",
            status=StageStatus.IMPLEMENTING,
        )
    ]
    ts.create(task)
    cfg = AgentRuntimeConfig(live_run_max_total_tokens=100)
    engine = TickEngine(
        task_store=ts,
        run_store=runs,
        incident_store=incidents,
        persona_runtime=BudgetExceededRuntime(session_id="session_budget"),
        config=cfg,
    )
    res = engine.tick_once(task_id="task_1")
    waiting_run = runs.get(res.actions_taken[0].payload["run_id"])

    runs.approve_continuation(waiting_run.id)
    for incident in incidents.list_open():
        if incident.run_id == waiting_run.id:
            incidents.close(incident.id, reason="operator approved same-session continuation")
    runtime = InspectContinuationSessionRuntime()
    retry = TickEngine(
        task_store=ts,
        run_store=runs,
        incident_store=incidents,
        persona_runtime=runtime,
        config=cfg,
    ).tick_once(task_id="task_1")

    assert retry.actions_taken[0].ok
    assert runtime.seen_session_ids == [("dev", "session_budget")]
    assert runtime.seen_run_limits == [("dev", "session_budget", 200)]


def test_mission_token_ceiling_blocks_new_run_and_logs_event(isolate_agent_runtime_root):
    ts = TaskStore()
    runs = RunStore()
    incidents = IncidentStore()
    task = make_task()
    task.state = TaskState.RUNNING
    ts.create(task)
    prior = runs.open_run("dev", "task_1", stage_id=None)
    prior.llm = {"total_tokens": 101}
    runs.update(prior)
    runs.close_run(prior.id, state=RunState.COMPLETED, final_decision={"type": "hand_off"})

    result = TickEngine(
        task_store=ts,
        run_store=runs,
        incident_store=incidents,
        persona_runtime=RuntimeMustNotRun(),
        config=AgentRuntimeConfig(mission_max_total_tokens=100),
    ).tick_once(task_id="task_1")

    action = result.actions_taken[0]
    assert action.ok is False
    assert action.payload["budget_kind"] == "mission"
    assert len(runs.list_for_task("task_1")) == 1
    incident = incidents.list_open()[0]
    assert incident.kind == "mission_budget_exceeded"
    assert incident.id in ts.get("task_1").open_incident_ids
    assert ts.get("task_1").state == TaskState.BLOCKED
    events = EventLog().for_task("task_1", limit=10)
    assert any(event.type == "mission_budget_exceeded" and event.payload["limit"] == 100 for event in events)


def test_mission_budget_incident_dedupes_across_ticks(isolate_agent_runtime_root):
    ts = TaskStore()
    runs = RunStore()
    incidents = IncidentStore()
    task = make_task()
    task.state = TaskState.RUNNING
    ts.create(task)
    prior = runs.open_run("dev", "task_1", stage_id=None)
    prior.llm = {"total_tokens": 101}
    runs.update(prior)
    runs.close_run(prior.id, state=RunState.COMPLETED, final_decision={"type": "hand_off"})
    engine = TickEngine(
        task_store=ts,
        run_store=runs,
        incident_store=incidents,
        persona_runtime=RuntimeMustNotRun(),
        config=AgentRuntimeConfig(mission_max_total_tokens=100),
    )

    first = engine.tick_once(task_id="task_1")
    assert len(first.incidents_opened) == 1

    # Re-arm the task the way live re-entry does and tick again: the open
    # incident must be reused, never re-minted.
    rearmed = ts.get("task_1")
    rearmed.state = TaskState.RUNNING
    ts.update(rearmed, actor="test", reason="re-arm")
    second = engine.tick_once(task_id="task_1")
    assert second.incidents_opened == []

    block = {
        "kind": "mission",
        "event_type": "mission_budget_exceeded",
        "summary": "Mission token budget exceeded: total_tokens=101/100",
        "task_id": "task_1",
        "persona_id": "dev",
        "stage_id": None,
        "total_tokens": 101,
        "limit": 100,
    }
    incident, newly_opened = engine._open_runtime_budget_incident(ts.get("task_1"), block)
    assert newly_opened is False

    open_budget = [item for item in incidents.list_open() if item.kind == "mission_budget_exceeded"]
    assert len(open_budget) == 1
    assert open_budget[0].id == incident.id
    events = [event for event in EventLog().for_task("task_1", limit=50) if event.type == "mission_budget_exceeded"]
    assert len(events) == 1


def test_mission_budget_does_not_starve_neko_budget_adjudication(isolate_agent_runtime_root):
    ts = TaskStore()
    runs = RunStore()
    incidents = IncidentStore()
    task = make_task()
    task.state = TaskState.RUNNING
    ts.create(task)
    runtime = BudgetThenNekoApprovalRuntime()
    engine = TickEngine(
        task_store=ts,
        run_store=runs,
        incident_store=incidents,
        persona_runtime=runtime,
        config=AgentRuntimeConfig(mission_max_total_tokens=100),
    )

    engine.tick_once(task_id="task_1")
    waiting = next(run for run in runs.list_for_task("task_1") if run.persona_id in {"dev", "backend_dev"})
    assert waiting.state == RunState.WAITING_ON_APPROVAL
    assert any(item.kind == "run_budget_exceeded" for item in incidents.list_open())
    # Push the mission total over its ceiling: the adjudication slot must still
    # run instead of deadlocking behind a mission_budget_exceeded incident.
    waiting.llm = {"total_tokens": 500}
    runs.update(waiting)

    engine.tick_once(task_id="task_1")

    assert "neko_supervisor" in runtime.seen
    assert not [item for item in incidents.list_open() if item.kind == "mission_budget_exceeded"]
    approved = runs.get(waiting.id)
    assert approved.progress.get("approved_for_continuation") is True


def test_swarm_token_hard_limit_blocks_new_run_and_logs_event(isolate_agent_runtime_root):
    ts = TaskStore()
    runs = RunStore()
    incidents = IncidentStore()
    task = make_task()
    task.state = TaskState.RUNNING
    ts.create(task)
    other = runs.open_run("dev", "task_other", stage_id=None)
    other.llm = {"total_tokens": 150}
    runs.update(other)
    runs.close_run(other.id, state=RunState.COMPLETED, final_decision={"type": "hand_off"})

    cfg = AgentRuntimeConfig(
        mission_max_total_tokens=1_000,
        swarm=SwarmConfig(enabled=True, global_token_hard_limit=100, global_token_soft_limit=50),
    )
    result = TickEngine(
        task_store=ts,
        run_store=runs,
        incident_store=incidents,
        persona_runtime=RuntimeMustNotRun(),
        config=cfg,
    ).tick_once(task_id="task_1")

    action = result.actions_taken[0]
    assert action.ok is False
    assert action.payload["budget_kind"] == "swarm"
    assert len(runs.list_for_task("task_1")) == 0
    incident = incidents.list_open()[0]
    assert incident.kind == "swarm_budget_exceeded"
    events = EventLog().for_task("task_1", limit=10)
    assert any(event.type == "swarm_budget_exceeded" and event.payload["total_tokens"] == 150 for event in events)


def test_dev_continuation_carries_prior_stage_progress_flags():
    ts = TaskStore()
    runs = RunStore()
    incidents = IncidentStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    task.stages = [
        TaskStage(
            id="stage_1",
            title="Implement UI",
            objective="finish launcher proof",
            status=StageStatus.IMPLEMENTING,
        )
    ]
    ts.create(task)
    prior = runs.open_run("dev", "task_1", stage_id="stage_1", session_id="session_budget")
    prior.progress = {"has_patch_progress": True, "patch_count": 2}
    prior.error = {
        "type": "run_budget_exceeded",
        "approved_for_continuation": True,
    }
    prior.state = RunState.FAILED
    runs.update(prior)

    runtime = InspectContinuationSessionRuntime()
    retry = TickEngine(
        task_store=ts,
        run_store=runs,
        incident_store=incidents,
        persona_runtime=runtime,
    ).tick_once(task_id="task_1")

    assert retry.actions_taken[0].ok
    assert runtime.seen_session_ids == [("dev", "session_budget")]
    assert runtime.seen_progress == [{"has_patch_progress": True}]


def test_budget_exceeded_without_session_blocks_as_same_session_not_safe():
    ts = TaskStore()
    runs = RunStore()
    incidents = IncidentStore()
    ts.create(make_task())
    engine = TickEngine(task_store=ts, run_store=runs, incident_store=incidents, persona_runtime=BudgetExceededRuntime())

    res = engine.tick_once()

    assert not res.actions_taken[0].ok
    run = runs.list_for_task("task_1")[0]
    assert run.state == RunState.FAILED
    assert run.error["type"] == "same_session_not_safe"
    assert run.progress["step"] == "same_session_not_safe"


def test_budget_exceeded_with_unsafe_session_blocks_without_persisting_raw_session():
    ts = TaskStore()
    runs = RunStore()
    incidents = IncidentStore()
    ts.create(make_task())
    engine = TickEngine(
        task_store=ts,
        run_store=runs,
        incident_store=incidents,
        persona_runtime=BudgetExceededRuntime(session_id="session_secret_token_C:/Users/example/config"),
    )

    res = engine.tick_once()

    assert not res.actions_taken[0].ok
    run = runs.list_for_task("task_1")[0]
    assert run.state == RunState.FAILED
    assert run.session_id is None
    assert run.error["type"] == "same_session_not_safe"
    assert run.progress["session_id"] is None
    assert "secret" not in json.dumps(run.progress).lower()
    assert "users" not in json.dumps(run.progress).lower()


def test_waiting_on_approval_runs_are_not_marked_stale():
    ts = TaskStore()
    runs = RunStore()
    incidents = IncidentStore()
    ts.create(make_task())
    engine = TickEngine(task_store=ts, run_store=runs, incident_store=incidents, persona_runtime=BudgetExceededRuntime(session_id="session_budget"))
    res = engine.tick_once()
    run = runs.get(res.actions_taken[0].payload["run_id"])
    run.last_heartbeat_at = now() - timedelta(seconds=9999)
    runs.update(run)

    assert runs.find_stale(heartbeat_ttl_seconds=1) == []


def test_budget_exceeded_runtime_sanitizes_untrusted_budget_exception_summary():
    ts = TaskStore()
    incidents = IncidentStore()
    ts.create(make_task())
    engine = TickEngine(
        task_store=ts,
        incident_store=incidents,
        persona_runtime=BudgetExceededRuntime("live run budget exceeded: wall_seconds=1.0 token=SECRET C:/Users/example/config"),
    )

    res = engine.tick_once()

    assert not res.actions_taken[0].ok
    assert res.actions_taken[0].summary == "live run budget exceeded: wall_seconds=1.0"
    incident = incidents.list_open()[0]
    assert incident.summary == "live run budget exceeded: wall_seconds=1.0"
    run = engine.run_store.get(res.actions_taken[0].payload["run_id"])
    assert run.error["summary"] == "live run budget exceeded: wall_seconds=1.0"


def test_tick_opens_run_with_configured_live_iteration_budget():
    ts = TaskStore()
    runs = RunStore()
    runtime = InspectRunMetadataRuntime(runs)
    ts.create(make_task())
    cfg = AgentRuntimeConfig(live_run_iteration_budget=12)

    res = TickEngine(task_store=ts, run_store=runs, persona_runtime=runtime, config=cfg).tick_once()

    assert res.actions_taken[0].ok
    assert runs.list_for_task("task_1")[0].iteration_budget == 12


def test_request_test_run_emits_budget_warning_for_expensive_dev_run(tmp_path):
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    ts.create(task)
    engine = TickEngine(task_store=ts, persona_runtime=HighBudgetDevRuntime())
    engine.command_workdir = tmp_path

    res = engine.tick_once()

    assert res.actions_taken[0].ok
    recent = engine.run_store.get(res.actions_taken[0].payload["run_id"])
    assert recent.llm["api_calls"] == 21
    from agent_runtime.events import EventLog

    events = EventLog().tail(10)
    warning = [event for event in events if event.run_id == recent.id and event.payload.get("phase") == "runaway_warning"]
    assert warning
    assert warning[-1].payload["step"] == "run_budget_high"
    assert warning[-1].payload["api_calls"] == 21
    assert warning[-1].payload["total_tokens"] == 800000


class DevRecoveryRuntime:
    def __init__(self):
        self.personas = []
        self.contexts = []

    def run_tick(self, persona, ctx, *, run):
        self.personas.append(persona)
        self.contexts.append(ctx)
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="rerun proof requested by QA",
            rationale="QA blocked the implementation on missing proof mapping, so Dev must collect corrected deterministic proof.",
            payload={"stage_id": "implement", "commands": ["printf 'recovered-proof\\n'"]},
        )


def test_blocked_qa_verdict_without_incident_routes_dev_recovery_instead_of_skipping(tmp_path):
    ts = TaskStore()
    proof_store = ProofStore()
    task = make_task()
    task.state = TaskState.BLOCKED
    task.current_stage_id = "stage_1"
    task.proof_ids = ["proof_qa_blocked"]
    ts.create(task)
    proof_store.attach(
        Proof(
            id="proof_qa_blocked",
            task_id="task_1",
            stage_id="stage_1",
            type=ProofType.QA_VERDICT,
            title="QA verdict: blocked",
            path_or_value="blocked",
            created_by="qa",
            created_at=now(),
            metadata={
                "verdict": "blocked",
                "findings": [
                    {"severity": "blocking", "issue": "missing proof manifest", "required_fix": "attach proof mapping"}
                ],
            },
            redaction_status="safe",
        )
    )
    runtime = DevRecoveryRuntime()
    engine = TickEngine(task_store=ts, proof_store=proof_store, persona_runtime=runtime)
    engine.command_workdir = tmp_path

    res = engine.tick_once()

    assert res.skipped == []
    assert res.actions_taken[0].ok
    assert res.actions_taken[0].action.type.value == "run_slot"
    assert res.actions_taken[0].action.slot_id == "dev"
    assert runtime.personas[0].id == "dev"
    saved = ts.get("task_1")
    assert saved.state == TaskState.RUNNING
    assert len(saved.proof_ids) == 2
    assert proof_store.get(saved.proof_ids[-1]).metadata["status"] == "passed"


class MultiAgentAutonomyRuntime:
    def __init__(self):
        self.personas = []

    def run_tick(self, persona, ctx, *, run):
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        self.personas.append(persona.id)
        proof_ids = list(ctx.proof_ids)
        if persona.id == "backend_dev" and not proof_ids:
            return AgentDecision(
                type=DecisionType.REQUEST_TEST_RUN,
                summary="Backend Dev requested backend contract proof",
                rationale="Backend proof must exist before Launcher Dev is released.",
                payload={"stage_id": "backend_contract", "commands": ["printf backend-contract-ok\\n"]},
            )
        if persona.id == "backend_dev":
            return AgentDecision(
                type=DecisionType.REQUEST_QA_REVIEW,
                summary="Backend Dev handed proof to Neko join gate",
                rationale="Backend contract proof is attached for Neko verification.",
                payload={"stage_id": "backend_contract", "proof_ids": proof_ids, "handoff": {"to": "qa", "stage_complete": True, "known_gaps": []}},
            )
        if persona.id == "neko_supervisor" and "proof_task_1_backend_contract" in proof_ids and "proof_task_1_launcher_ui" not in proof_ids:
            return AgentDecision(
                type=DecisionType.PROPOSE_ACCEPTANCE,
                summary="Neko released Launcher Dev after backend proof",
                rationale="Backend contract proof is attached, so frontend can consume it without guessing.",
                payload={"objective": ctx.task.description, "acceptance_criteria": list(ctx.task.acceptance_criteria), "affected_repos": ["EterniaLauncher"]},
            )
        if persona.id == "dev" and "proof_task_1_launcher_ui" not in proof_ids:
            return AgentDecision(
                type=DecisionType.REQUEST_TEST_RUN,
                summary="Launcher Dev requested UI proof",
                rationale="Frontend proof is required before QA.",
                payload={"stage_id": "launcher_ui", "commands": ["printf launcher-ui-ok\\n"]},
            )
        if persona.id == "dev":
            return AgentDecision(
                type=DecisionType.REQUEST_QA_REVIEW,
                summary="Launcher Dev handed all proof to Neko",
                rationale="Backend and frontend proof IDs are attached.",
                payload={"stage_id": "launcher_ui", "proof_ids": proof_ids, "handoff": {"to": "qa", "stage_complete": True, "known_gaps": []}},
            )
        if persona.id == "neko_supervisor":
            return AgentDecision(
                type=DecisionType.PROPOSE_ACCEPTANCE,
                summary="Neko released QA after all specialist proof",
                rationale="Both backend and frontend proof are attached.",
                payload={"objective": ctx.task.description, "acceptance_criteria": list(ctx.task.acceptance_criteria)},
            )
        if persona.id == "qa":
            return AgentDecision(
                type=DecisionType.REPORT_QA_VERDICT,
                summary="QA approved backend and frontend proof",
                rationale="QA independently reviewed all attached proof IDs.",
                payload={"review_scope": "implementation", "verdict": "approved", "proof_ids": proof_ids, "findings": []},
            )
        raise AssertionError(f"unexpected persona: {persona.id}")


class StageNamedPassingProofRunner:
    def __init__(self, proof_store: ProofStore):
        self.proof_store = proof_store

    def run_commands(self, task, *, stage_id, run_id, actor, commands):
        proof = Proof(
            id=f"proof_{task.id}_{stage_id}",
            task_id=task.id,
            stage_id=stage_id,
            type=ProofType.TEST_RUN,
            title=f"{stage_id} proof",
            path_or_value=f"{stage_id}.log",
            created_by=actor,
            created_at=now(),
            metadata={"status": "passed", "exit_code": 0, "commands_requested": len(commands)},
            redaction_status="safe",
        )
        self.proof_store.attach(proof)
        if stage_id == "launcher_ui":
            visual = Proof(
                id=f"proof_{task.id}_{stage_id}_screenshot",
                task_id=task.id,
                stage_id=stage_id,
                type=ProofType.SCREENSHOT,
                title=f"{stage_id} screenshot",
                path_or_value=f"{stage_id}.png",
                created_by=actor,
                created_at=now(),
                metadata={"status": "passed", "artifact_exists": True},
                redaction_status="safe",
            )
            self.proof_store.attach(visual)
            return [proof, visual]
        return [proof]


class PartialFailingCommandProofRunner:
    def __init__(self, proof_store: ProofStore):
        self.proof_store = proof_store
        self.calls = 0

    def run_commands(self, task, *, stage_id, run_id, actor, commands):
        self.calls += 1
        proof = Proof(
            id="proof_partial_before_failure",
            task_id=task.id,
            stage_id=stage_id,
            type=ProofType.TEST_RUN,
            title="partial proof before failure",
            path_or_value="partial.log",
            created_by="harness",
            created_at=now(),
            metadata={"status": "passed", "exit_code": 0, "commands_requested": len(commands), "run_id": run_id},
            redaction_status="safe",
        )
        self.proof_store.attach(proof)
        raise RuntimeError("second proof command hung after first proof attached")


class RequestTwoProofCommandsRuntime:
    def run_tick(self, persona, ctx, *, run):
        from agent_runtime.decision_schema import AgentDecision, DecisionType

        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="collect two proof commands",
            rationale="The first proof should survive even if the second command fails.",
            payload={"stage_id": "implement", "commands": ["printf first\\n", "printf second\\n"]},
        )


def test_tick_persists_incremental_command_proof_when_later_command_fails():
    ts = TaskStore()
    task = make_task()
    task.state = TaskState.RUNNING
    task.current_stage_id = "stage_1"
    ts.create(task)
    proof_store = ProofStore()
    engine = TickEngine(
        task_store=ts,
        proof_store=proof_store,
        persona_runtime=RequestTwoProofCommandsRuntime(),
        proof_runner=PartialFailingCommandProofRunner(proof_store),
    )

    res = engine.tick_once(task_id="task_1")

    assert not res.actions_taken[0].ok
    saved = ts.get("task_1")
    assert saved.proof_ids == ["proof_partial_before_failure"]
    assert proof_store.get("proof_partial_before_failure").metadata["status"] == "passed"


def test_multi_agent_autonomy_runs_backend_neko_launcher_neko_qa_to_done():
    ts = TaskStore()
    task = make_task()
    task.description = "Ship backend contract then Launcher UI with independent QA."
    task.acceptance_criteria = ["backend proof", "frontend proof", "qa proof"]
    task.state = TaskState.RUNNING
    task.current_stage_id = "backend_contract"
    task.affected_repos = ["EterniaBackend"]
    task.risk_flags = ["sequential_specialists_required", "backend_contract_first", "launcher_visual_proof_required_after_join_gate"]
    task.stages = [
        TaskStage(id="backend_contract", title="Backend contract", objective="prove backend contract", status=StageStatus.IMPLEMENTING, test_plan=["backend proof"]),
        TaskStage(id="launcher_ui", title="Launcher UI", objective="prove Launcher UI", status=StageStatus.READY, test_plan=["frontend proof"], requires_visual_proof=True),
    ]
    ts.create(task)
    proofs = ProofStore()
    runtime = MultiAgentAutonomyRuntime()
    engine = TickEngine(task_store=ts, proof_store=proofs, persona_runtime=runtime, proof_runner=StageNamedPassingProofRunner(proofs))

    result = engine.run_until_settled(task_id="task_1", max_actions=12)

    assert result.stop_reason == "task_terminal", (result.stop_reason, runtime.personas, ts.get("task_1").state, ts.get("task_1").current_stage_id, ts.get("task_1").proof_ids)
    assert result.final_task_state == TaskState.DONE.value
    assert runtime.personas == ["backend_dev", "dev", "qa"]
    saved = ts.get("task_1")
    assert saved.state == TaskState.DONE
    assert saved.proof_ids[:2] == ["proof_task_1_backend_contract", "proof_task_1_launcher_ui"]
    assert proofs.get(saved.proof_ids[0]).created_by == "backend_dev"
    assert proofs.get(saved.proof_ids[1]).created_by == "dev"
    assert proofs.get("proof_task_1_launcher_ui_screenshot").type == ProofType.SCREENSHOT
    assert proofs.get(saved.proof_ids[-1]).type == ProofType.QA_VERDICT
    assert saved.stages[0].status == StageStatus.PASSED
    assert saved.stages[1].status == StageStatus.PASSED
    assert engine.incident_store.list_open() == []
    assert not [
        event
        for event in EventLog().for_task("task_1", limit=0)
        if event.type == "cross_stack.backend_contract_packet_missing"
    ]
