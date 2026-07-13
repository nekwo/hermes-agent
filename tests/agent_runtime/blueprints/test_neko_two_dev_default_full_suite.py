"""Deterministic full-suite drive of neko's DEFAULT blueprint
(``neko_two_dev_default``) end to end — the regression-suite twin of the
live no-edit cross-stack proof goals (see Launcher_Brain
mission-control-decision-hud-verification-2026-07-01, R1/R5).

No live model calls: a scripted persona runtime plays neko + both devs
under the collapsed contract while the REAL ``TickEngine`` routes stages,
records handoff observations, and re-runs the authoritative gate. Repo
grounding is stubbed onto throwaway git repos in tmp (the real
``isolated_repo_context_for_run`` runs against them), so the whole shape
that live runs prove is baked into the suite:

* neko scopes and routes to the two dev owners;
* the plan stages carry the right repos
  (``backend_implementation`` -> EterniaBackend, ``implement`` -> EterniaLauncher);
* each dev grounds in its OWN stage repo via an isolated worktree
  (``isolated=True``, ``detached_head=True``, not the source checkout);
* every dev stage gets a ``stage_observations`` entry with a passed
  authoritative gate AND a linked observed (agent self-test) proof;
* the goal reaches DONE with no incidents and zero ``model_invalid_output``;
* the collapsed contract is used (public decisions ``scope_route`` /
  ``hand_off``; parity events logged).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from hermes_time import now

from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.default_plan import DEFAULT_TASK_BLUEPRINT_ID
from agent_runtime.events import EventLog
from agent_runtime.goal_runner import (
    DEFAULT_GOAL_BLUEPRINT_ID,
    GoalRunOptions,
    MissionRuntimeController,
)
from agent_runtime.models import Proof
from agent_runtime.progress import RunProgressSink
from agent_runtime.proof_rules import ProofType
from agent_runtime.repo_context import (
    RepoExecutionContext,
    capture_repo_baseline,
    isolated_repo_context_for_run,
)
from agent_runtime.runtime_config import RuntimeConfig, SimplifiedAgentContractConfig
from agent_runtime.states import StageStatus, TaskState
from agent_runtime.store import IncidentStore, ProofStore, RunStore, TaskStore
from agent_runtime.ticker import TickEngine

STAGE_REPOS = {
    "backend_implementation": "EterniaBackend",
    "implement": "EterniaLauncher",
}

STAGE_SELF_TESTS = {
    "backend_implementation": "python manage.py check",
    "implement": "flutter analyze lib/core/qa/stagec_qa_marionette_hooks.dart",
}


def _make_source_repo(root: Path, label: str) -> Path:
    """Throwaway git repo standing in for a product checkout."""
    repo = root / label
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text(f"{label} baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "baseline"], cwd=repo, check=True, stdout=subprocess.DEVNULL
    )
    return repo


class ScriptedCrossStackRuntime:
    """Plays neko + backend_dev + dev with collapsed-contract decisions.

    Dev turns ground in an isolated worktree of the STAGE's repo (created
    with the real ``isolated_repo_context_for_run``) and run one terminal
    self-test through the real ``RunProgressSink`` capture path before
    handing off — mirroring what a live dev session does.
    """

    def __init__(self, *, run_store: RunStore, task_store: TaskStore, sources: dict[str, Path]):
        self.run_store = run_store
        self.task_store = task_store
        self.sources = sources
        self.turns: list[tuple[str, str]] = []  # (persona_id, stage_id)
        self.groundings: dict[str, dict] = {}  # stage_id -> repo_execution

    def run_tick(self, persona, ctx, *, run):
        persona_id = run.persona_id
        stage_id = run.stage_id
        self.turns.append((persona_id, stage_id))

        if persona_id == "neko_supervisor":
            return AgentDecision(
                type=DecisionType.SCOPE_ROUTE,
                summary="Scope the no-edit cross-stack verification and route backend first.",
                rationale="Both dev stages are read-only proof stages; backend gates launcher.",
                payload={
                    "objective": "Run the no-edit cross-stack verification.",
                    "acceptance_criteria": [
                        "Backend and Launcher proof stages pass their gates."
                    ],
                    "target_owner": "backend_dev",
                    "target_repo": "EterniaBackend",
                    "proof_gate": {
                        "required": True,
                        "required_proof_types": ["test_run"],
                        "minimum_status": "passed",
                        "visual_required": False,
                    },
                },
            )

        # Dev turn: ground in an isolated worktree of the STAGE's repo.
        task = self.task_store.get(run.task_id)
        stage = next(s for s in task.mission_plan.stages if s.id == stage_id)
        source = self.sources[stage.repo]
        isolated = isolated_repo_context_for_run(
            RepoExecutionContext(workdir=source, repo_label=stage.repo, source="test"),
            task_id=run.task_id,
            run_id=run.id,
        )
        repo_execution = {
            "workdir": str(isolated.workdir),
            "isolated": True,
            "detached_head": True,
            "workdir_label": isolated.workdir.name,
            "repo_label": stage.repo,
        }
        run.progress = {
            "repo_execution": repo_execution,
            "repo_baseline": capture_repo_baseline(isolated.workdir),
        }
        self.run_store.update(run)
        self.groundings[stage_id] = repo_execution

        # Real in-session terminal self-test -> observed-lane capture.
        RunProgressSink(run_store=self.run_store, run_id=run.id).emit(
            "run.tool.finished",
            {
                "type": "run.tool.finished",
                "tool_name": "terminal",
                "status": "passed",
                "command_full": STAGE_SELF_TESTS[stage_id],
                "summary": "Finished tool terminal: passed",
            },
        )

        return AgentDecision(
            type=DecisionType.HAND_OFF,
            summary=f"{stage_id} verified with a focused read-only self-test.",
            rationale="Harness should observe the isolated diff and rerun the gate.",
            payload={"stage_id": stage_id},
        )


class RecordingProofRunner:
    """Authoritative-gate stand-in: records commands, attaches passed proofs."""

    def __init__(self, store: ProofStore):
        self.store = store
        self.commands_by_stage: dict[str, list[str]] = {}

    def run_commands(self, task, *, stage_id, run_id, actor, commands, **kwargs):
        self.commands_by_stage.setdefault(stage_id, []).extend(commands)
        proof = Proof(
            id=f"proof_gate_{task.id}_{stage_id}_{len(self.commands_by_stage[stage_id])}",
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


def test_neko_two_dev_default_full_suite_no_edit_cross_stack(
    isolate_agent_runtime_root, tmp_path
):
    assert DEFAULT_GOAL_BLUEPRINT_ID == "neko_two_dev_default"
    assert DEFAULT_TASK_BLUEPRINT_ID == "neko_two_dev_default"

    sources = {
        "EterniaBackend": _make_source_repo(tmp_path, "EterniaBackend"),
        "EterniaLauncher": _make_source_repo(tmp_path, "EterniaLauncher"),
    }

    task_store = TaskStore()
    run_store = RunStore()
    proof_store = ProofStore()
    incident_store = IncidentStore()
    runtime = ScriptedCrossStackRuntime(
        run_store=run_store, task_store=task_store, sources=sources
    )
    gate_runner = RecordingProofRunner(proof_store)
    config = RuntimeConfig(
        simplified_agent_contract=SimplifiedAgentContractConfig(enabled=True)
    )

    def engine_factory(**kwargs):
        return TickEngine(
            persona_runtime=runtime,
            proof_runner=gate_runner,
            **kwargs,
        )

    result = MissionRuntimeController(
        config=config,
        task_store=task_store,
        run_store=run_store,
        proof_store=proof_store,
        incident_store=incident_store,
        engine_factory=engine_factory,
    ).run_goal(
        GoalRunOptions(
            title="No-edit cross-stack proof",
            description=(
                "No-edit cross-stack proof for Backend and Launcher; "
                "do not modify product files."
            ),
            acceptance_criteria=["Backend and Launcher no-product-edit proofs pass."],
            affected_repos=["EterniaBackend", "EterniaLauncher"],
            max_actions=16,
        )
    )

    # --- Terminal shape: done, clean, zero invalid-output. -------------
    assert result.ok is True, result.blocker_summary
    assert result.final_task_state == "done"
    assert result.open_incident_ids == []
    task = task_store.get(result.task_id)
    assert task.state == TaskState.DONE

    incidents = incident_store.list_for_task(task.id) if hasattr(incident_store, "list_for_task") else []
    assert [i for i in incidents if getattr(i, "kind", "") == "model_invalid_output"] == []
    events = EventLog().for_task(task.id, limit=0)
    assert [e for e in events if "model_invalid_output" in (e.type or "")] == []

    # --- Blueprint + routing shape. -------------------------------------
    plan = task.mission_plan
    assert plan.blueprint_id == "neko_two_dev_default"
    assert plan.bindings["lead"] == "neko_supervisor"
    assert plan.bindings["backend_builder"] == "backend_dev"
    assert plan.bindings["builder"] == "dev"
    stages = {s.id: s for s in plan.stages}
    for stage_id, repo in STAGE_REPOS.items():
        assert stages[stage_id].repo == repo
        assert stages[stage_id].status == StageStatus.PASSED
        assert stages[stage_id].proof_gate["observed_lane_required"] is True
        assert "agent_tool_trace" in stages[stage_id].proof_gate["observed_lane_requirement"]
    assert stages["scope"].status == StageStatus.PASSED

    # Personas ran in blueprint order: neko scoped, backend first, then dev.
    assert runtime.turns[0] == ("neko_supervisor", "scope")
    dev_turns = [t for t in runtime.turns if t[0] in {"backend_dev", "dev"}]
    assert dev_turns[0] == ("backend_dev", "backend_implementation")
    assert ("dev", "implement") in dev_turns

    # --- Isolated-worktree grounding per stage repo. ---------------------
    for stage_id, repo in STAGE_REPOS.items():
        grounding = runtime.groundings[stage_id]
        assert grounding["isolated"] is True
        assert grounding["detached_head"] is True
        assert grounding["repo_label"] == repo
        workdir = Path(grounding["workdir"])
        source = sources[repo]
        assert workdir != source, "dev must not run in the live checkout"
        assert repo in workdir.name, "worktree must derive from the stage repo"

    # --- Harness observations: observed lane + authoritative gate. ------
    observations = task.harness_self_heal["stage_observations"]
    observed_by_stage = {}
    for stage_id in STAGE_REPOS:
        obs = observations[stage_id]
        assert obs["authoritative_gate_status"] == "passed", (stage_id, obs)
        assert obs["observed_proof_ids"], f"{stage_id} observed lane must be populated"
        observed_by_stage[stage_id] = obs["observed_proof_ids"]
        assert gate_runner.commands_by_stage.get(stage_id), (
            f"{stage_id} authoritative gate must have re-run harness-side commands"
        )

    # Observed proofs are the captured agent self-tests, stage-linked.
    for stage_id, proof_ids in observed_by_stage.items():
        for proof_id in proof_ids:
            proof = proof_store.get(proof_id)
            assert (proof.metadata or {}).get("source") == "agent_tool_trace"
            assert proof.stage_id == stage_id

    # --- Collapsed contract: public decisions + parity events. ----------
    parity = [e for e in events if e.type == "decision_contract.parity"]
    assert parity, "collapsed contract must log parity events"
    public_types = {e.payload.get("public_decision_type") for e in parity}
    assert {"scope_route", "hand_off"} <= public_types
    assert all(
        e.payload.get("mode") != "legacy" for e in parity
    ), "simplified contract must be active for every decision"
