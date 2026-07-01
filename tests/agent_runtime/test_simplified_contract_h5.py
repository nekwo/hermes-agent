import subprocess
from pathlib import Path

from hermes_time import now

from agent_runtime.blueprints import BlueprintStore, instantiate_blueprint
from agent_runtime.context_builder import build_context
from agent_runtime.decision_schema import AgentDecision, DecisionPayloadInvalid, DecisionType
from agent_runtime.events import EventLog
from agent_runtime.models import AgentRun, MissionIntent, MissionPlan, MissionPlanStage, Proof, Task
from agent_runtime.proof_rules import ProofType
from agent_runtime.repo_context import capture_repo_baseline, isolated_repo_context_for_run, RepoExecutionContext
from agent_runtime.runtime_config import NormalWorkerFlowConfig, RuntimeConfig, SimplifiedAgentContractConfig
from agent_runtime.simplified_contract import project_decision_for_execution
from agent_runtime.states import RunState, StageStatus, TaskState
from agent_runtime.store import ProofStore, RunStore, TaskStore
from agent_runtime.ticker import TickEngine
from agent_runtime.progress import RunProgressSink


def _task(task_id: str = "task_h5") -> Task:
    ts = now()
    return Task(
        id=task_id,
        title="H5 simplified contract",
        description="Use collapsed signals.",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="test",
        affected_repos=["hermes-agent"],
        current_stage_id="build",
    )


def _run(task: Task, persona_id: str = "dev") -> AgentRun:
    ts = now()
    return AgentRun(
        id=f"run_{task.id}",
        persona_id=persona_id,
        task_id=task.id,
        stage_id=task.current_stage_id,
        state=RunState.RUNNING,
        started_at=ts,
        last_heartbeat_at=ts,
    )


def test_flag_on_hud_exposes_collapsed_signals_without_payload_fill_surface():
    task = _task("task_h5_hud")
    task.mission_plan = MissionPlan(
        enabled=True,
        mission_intent=MissionIntent(title=task.title, objective=task.description),
        current_stage_id="build",
        stages=[
            MissionPlanStage(
                id="build",
                title="Build",
                objective="Do the work.",
                owner="dev",
                repo="hermes-agent",
                kind="implementation",
                status=StageStatus.IMPLEMENTING,
                requires_product_edit=True,
            )
        ],
    )
    cfg = RuntimeConfig(
        normal_worker_flow=NormalWorkerFlowConfig(enabled=False),
        simplified_agent_contract=SimplifiedAgentContractConfig(enabled=True, expose_only_simplified_actions=True),
    )

    hud = build_context(task, _run(task), config=cfg).mission_hud

    assert hud["decision_contract_mode"] == "simplified_agent_contract"
    assert [item["decision_type"] for item in hud["decision_menu"]] == ["hand_off", "block", "escalate"]
    assert hud["agent_hud"]["recommended_action"]["decision_type"] == "hand_off"
    assert "payload_skeleton" not in hud["agent_hud"]["recommended_action"]
    assert all("payload_template" not in item for item in hud["decision_menu"])
    assert all("payload_template" not in shape for shape in hud["decision_shape_index"].values())


def test_simplified_projection_maps_hand_off_and_can_reject_legacy_aliases(isolate_agent_runtime_root):
    task = _task("task_h5_projection")
    enabled = RuntimeConfig(simplified_agent_contract=SimplifiedAgentContractConfig(enabled=True))
    hand_off = AgentDecision(
        type=DecisionType.HAND_OFF,
        summary="done",
        rationale="work complete",
        payload={"stage_id": "build"},
    )

    projection = project_decision_for_execution(task, hand_off, config=enabled, actor="dev", run_id="run_1")

    assert projection.public_type == DecisionType.HAND_OFF
    assert projection.execution_type == DecisionType.PROPOSE_PATCH
    assert projection.mode == "simplified_internal_projection"

    legacy_disabled = RuntimeConfig(
        simplified_agent_contract=SimplifiedAgentContractConfig(enabled=True, allow_legacy_decision_aliases=False)
    )
    legacy = AgentDecision(type=DecisionType.REQUEST_TEST_RUN, summary="legacy", rationale="old", payload={"stage_id": "build", "commands": ["pytest"]})

    rejected = project_decision_for_execution(task, legacy, config=legacy_disabled, actor="dev", run_id="run_2")

    assert rejected.public_type == DecisionType.HAND_OFF
    assert rejected.blocked_reason
    assert "allow_legacy_decision_aliases=false" in rejected.blocked_reason


def test_rollback_disabled_flag_leaves_legacy_decision_unprojected(isolate_agent_runtime_root):
    task = _task("task_h5_rollback")
    cfg = RuntimeConfig(simplified_agent_contract=SimplifiedAgentContractConfig(enabled=False))
    decision = AgentDecision(type=DecisionType.REQUEST_TEST_RUN, summary="legacy", rationale="old", payload={"stage_id": "build", "commands": ["pytest"]})

    projection = project_decision_for_execution(task, decision, config=cfg, actor="dev", run_id="run_legacy")

    assert projection.mode == "legacy"
    assert projection.execution_decision is decision
    assert projection.execution_type == DecisionType.REQUEST_TEST_RUN


def test_proof_from_trace_capture_runs_under_simplified_flag(isolate_agent_runtime_root, monkeypatch):
    cfg = RuntimeConfig(simplified_agent_contract=SimplifiedAgentContractConfig(enabled=True))
    monkeypatch.setattr("agent_runtime.progress.load_agent_runtime_config", lambda: cfg)
    runs = RunStore()
    run = runs.open_run("dev", "task_h5_trace", stage_id="build")
    sink = RunProgressSink(run_store=runs, run_id=run.id)

    sink.emit(
        "run.tool.finished",
        {
            "type": "run.tool.finished",
            "phase": "tool",
            "step": "tool_finished",
            "tool_name": "terminal",
            "status": "passed",
            "command": "python -m pytest tests/agent_runtime/test_simplified_contract_h5.py -q",
            "exit_code": 0,
        },
    )

    observed = [proof for proof in ProofStore().list_for_task("task_h5_trace") if proof.metadata.get("source") == "agent_tool_trace"]
    assert len(observed) == 1
    assert observed[0].metadata["authoritative"] is False
    assert observed[0].metadata["status"] == "passed"


class _HandOffRuntime:
    def __init__(self, *, run_store: RunStore, workdir: Path, baseline: dict):
        self.run_store = run_store
        self.workdir = workdir
        self.baseline = baseline

    def run_tick(self, persona, ctx, *, run):
        (self.workdir / "feature.txt").write_text("agent delta\n", encoding="utf-8")
        run.progress = {
            "repo_execution": {
                "workdir": str(self.workdir),
                "isolated": True,
                "detached_head": True,
                "workdir_label": self.workdir.name,
            },
            "repo_baseline": self.baseline,
        }
        self.run_store.update(run)
        return AgentDecision(
            type=DecisionType.HAND_OFF,
            summary="hand off",
            rationale="Harness should observe diff and rerun gate.",
            payload={"stage_id": "build"},
        )


class _RecordingProofRunner:
    def __init__(self, store: ProofStore):
        self.store = store
        self.commands: list[str] = []

    def run_commands(self, task, *, stage_id, run_id, actor, commands, **kwargs):
        self.commands.extend(commands)
        proof = Proof(
            id=f"proof_{task.id}_{stage_id}_{len(self.commands)}",
            task_id=task.id,
            stage_id=stage_id,
            type=ProofType.TEST_RUN,
            title="authoritative gate",
            path_or_value="gate.log",
            created_by="harness",
            created_at=now(),
            metadata={"status": "passed", "exit_code": 0, "run_id": run_id, "actor_requested": actor},
            redaction_status="safe",
        )
        return [self.store.attach(proof)]


def test_hand_off_captures_isolated_diff_and_reruns_authoritative_gate(isolate_agent_runtime_root, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init"], cwd=source, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=source, check=True)
    (source / "feature.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=source, check=True, stdout=subprocess.DEVNULL)
    isolated = isolated_repo_context_for_run(
        RepoExecutionContext(workdir=source, repo_label="source", source="test"),
        task_id="task_h5_gate",
        run_id="run_h5_gate",
    )
    baseline = capture_repo_baseline(isolated.workdir)

    proof_store = ProofStore()
    run_store = RunStore()
    task_store = TaskStore()
    bp = BlueprintStore().get("one_agent_smoke")
    plan = instantiate_blueprint(bp, goal="H5 gate", bindings={"builder": "persona:dev"})
    stage = plan.stages[0]
    stage.kind = "proof_only"
    stage.proof_recipe_id = "harness_runtime_status_snapshot"
    stage.requires_product_edit = False
    task = _task("task_h5_gate")
    task.mission_plan = plan
    task.current_stage_id = plan.current_stage_id
    task_store.create(task)
    runner = _RecordingProofRunner(proof_store)
    cfg = RuntimeConfig(simplified_agent_contract=SimplifiedAgentContractConfig(enabled=True))
    engine = TickEngine(
        task_store=task_store,
        run_store=run_store,
        proof_store=proof_store,
        persona_runtime=_HandOffRuntime(run_store=run_store, workdir=isolated.workdir, baseline=baseline),
        proof_runner=runner,
        config=cfg,
    )

    result = engine.run_until_settled(task_id=task.id, max_actions=2)

    saved = task_store.get(task.id)
    assert result.stop_reason == "task_terminal"
    assert saved.state == TaskState.DONE
    assert runner.commands
    observation = saved.harness_self_heal["stage_observations"]["build"]
    assert "agent delta" in observation["repo_diff"]["diff"]
    assert observation["authoritative_gate_status"] == "passed"
    parity = [event for event in EventLog().for_task(task.id, limit=0) if event.type == "decision_contract.parity"]
    assert parity[-1].payload["public_decision_type"] == "hand_off"
    assert parity[-1].payload["execution_decision_type"] == "propose_patch"
