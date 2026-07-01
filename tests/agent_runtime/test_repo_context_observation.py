import os
import subprocess
import time

from agent_runtime.repo_context import RepoExecutionContext, capture_repo_baseline, git_diff_since_baseline, isolated_repo_context_for_run


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


def test_git_diff_since_baseline_excludes_preexisting_dirty_paths(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Harness Test")
    (repo / "preexisting.txt").write_text("clean\n", encoding="utf-8")
    (repo / "agent.txt").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")

    (repo / "preexisting.txt").write_text("dirty before run\n", encoding="utf-8")
    baseline = capture_repo_baseline(repo)
    (repo / "agent.txt").write_text("changed by run\n", encoding="utf-8")

    diff = git_diff_since_baseline(repo, baseline)

    assert "agent.txt" in diff["diff"]
    assert "changed by run" in diff["diff"]
    assert "preexisting.txt" not in diff["diff"]
    assert diff["baseline_dirty_count"] == 1
    assert diff["excluded_baseline_paths"] == ["preexisting.txt"]


def test_isolated_repo_context_uses_distinct_worktrees_for_parallel_runs(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(runtime_root))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Harness Test")
    (repo / "shared.txt").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    (repo / "shared.txt").write_text("dirty live checkout\n", encoding="utf-8")
    source = RepoExecutionContext(workdir=repo, repo_label="repo", source="test")

    first = isolated_repo_context_for_run(source, task_id="task_1", run_id="run_a")
    second = isolated_repo_context_for_run(source, task_id="task_1", run_id="run_b")
    (first.workdir / "shared.txt").write_text("first run only\n", encoding="utf-8")

    assert first.workdir != second.workdir
    assert (second.workdir / "shared.txt").read_text(encoding="utf-8") == "clean\n"
    assert (repo / "shared.txt").read_text(encoding="utf-8") == "dirty live checkout\n"


def test_isolated_repo_context_gc_removes_old_clean_runtime_worktrees(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(runtime_root))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Harness Test")
    (repo / "shared.txt").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    source = RepoExecutionContext(workdir=repo, repo_label="repo", source="test")

    old = isolated_repo_context_for_run(source, task_id="task_1", run_id="run_old")
    old_time = time.time() - (3 * 24 * 60 * 60)
    os.utime(old.workdir, (old_time, old_time))

    new = isolated_repo_context_for_run(source, task_id="task_1", run_id="run_new")

    assert new.workdir.exists()
    assert not old.workdir.exists()
