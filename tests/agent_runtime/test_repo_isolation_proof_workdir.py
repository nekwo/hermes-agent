import subprocess

from hermes_time import now

from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.models import Task
from agent_runtime.proof_runner import CommandProofRunner
from agent_runtime.repo_context import RepoExecutionContext, capture_repo_baseline, isolated_repo_context_for_run
from agent_runtime.states import TaskState
from agent_runtime.store import ProofStore, RunStore
from agent_runtime.ticker import TickEngine


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


def _task(repo) -> Task:
    return Task(
        id="task_repo_isolation",
        title="Repo isolation",
        description="Proof commands must run in isolated worktrees.",
        state=TaskState.RUNNING,
        created_at=now(),
        updated_at=now(),
        requested_by="test",
        affected_repos=[str(repo)],
        current_stage_id="implement",
    )


def test_command_proof_uses_run_isolated_worktree_not_live_repo(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(runtime_root))
    repo = tmp_path / "backend"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Harness Test")
    (repo / "app.py").write_text("print('clean')\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    (repo / "live-only.txt").write_text("pre-existing live dirt\n", encoding="utf-8")

    runs = RunStore()
    run = runs.open_run("backend_dev", "task_repo_isolation", stage_id="implement")
    source = RepoExecutionContext(workdir=repo, repo_label="backend", source="test")
    isolated = isolated_repo_context_for_run(source, task_id="task_repo_isolation", run_id=run.id)
    baseline = capture_repo_baseline(isolated.workdir)
    run.progress = {
        "repo_baseline": baseline,
        "repo_execution": {
            "schema_version": 1,
            "workdir": str(isolated.workdir),
            "workdir_label": isolated.workdir.name,
            "repo_label": isolated.repo_label,
            "source": isolated.source,
            "isolated": True,
            "detached_head": True,
            "git_head": baseline["git_head"],
        },
    }
    runs.update(run)
    proofs = ProofStore()
    engine = TickEngine(run_store=runs, proof_store=proofs)
    engine.config.tool_wait_timeout_seconds = 10
    decision = AgentDecision(
        type=DecisionType.REQUEST_TEST_RUN,
        summary="Run cwd proof",
        rationale="Verify proof workdir isolation.",
        payload={
            "stage_id": "implement",
            "commands": ["python -c \"from pathlib import Path; Path('proof-marker.txt').write_text('isolated', encoding='utf-8')\""],
        },
    )

    proof_ids = engine._collect_command_proof(_task(repo), decision, actor="backend_dev", run_id=run.id)

    proof = proofs.get(proof_ids[0])
    assert (isolated.workdir / "proof-marker.txt").read_text(encoding="utf-8") == "isolated"
    assert not (repo / "proof-marker.txt").exists()
    assert proof.metadata["workdir_label"] == isolated.workdir.name
    assert proof.metadata["workdir_is_harness_worktree"] is True
    assert proof.metadata["workdir_head_state"] == "detached"


def test_dev_command_proof_without_run_metadata_isolates_git_workdir(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(runtime_root))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Harness Test")
    (repo / "app.py").write_text("print('clean')\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    runs = RunStore()
    run = runs.open_run("backend_dev", "task_repo_isolation", stage_id="implement")
    proofs = ProofStore()
    engine = TickEngine(run_store=runs, proof_store=proofs)
    decision = AgentDecision(
        type=DecisionType.REQUEST_TEST_RUN,
        summary="Run proof",
        rationale="Should create a proof worktree.",
        payload={
            "stage_id": "implement",
            "commands": ["python -c \"from pathlib import Path; Path('proof-marker.txt').write_text('isolated', encoding='utf-8')\""],
        },
    )

    proof_ids = engine._collect_command_proof(_task(repo), decision, actor="backend_dev", run_id=run.id)

    proof = proofs.get(proof_ids[0])
    assert not (repo / "proof-marker.txt").exists()
    assert proof.metadata["workdir_is_harness_worktree"] is True
    assert proof.metadata["workdir_head_state"] == "detached"


def test_no_product_edit_recipe_ignores_harness_tmp_litter(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Harness Test")
    (repo / "app.py").write_text("print('clean')\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    proofs = ProofStore()
    runner = CommandProofRunner(proof_store=proofs, workdir=repo, timeout_seconds=10)

    result = runner.run_commands(
        _task(repo),
        stage_id="implement",
        run_id="run_litter",
        actor="backend_dev",
        commands=["python -c \"from pathlib import Path; Path('.hermes-tmp.probe').write_text('tmp', encoding='utf-8')\""],
        proof_recipe={"recipe_id": "no_edit", "recipe_hash": "hash123", "mode": "no_product_edit"},
    )

    assert result[0].metadata["status"] == "passed"
    assert result[0].metadata["dirty_delta_count"] == 0
