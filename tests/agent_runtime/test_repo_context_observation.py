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


def test_git_diff_since_baseline_attributes_only_agent_delta_with_dirty_tracked_and_untracked(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(runtime_root))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Harness Test")
    (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")

    (repo / "tracked.txt").write_text("dirty before run\n", encoding="utf-8")
    (repo / "preexisting-untracked.txt").write_text("old litter\n", encoding="utf-8")
    (repo / "media").mkdir()
    (repo / "media" / ".hermes-tmp.old").write_text("harness litter\n", encoding="utf-8")
    baseline = capture_repo_baseline(repo)

    (repo / "tracked.txt").write_text("dirty before run\nagent delta\n", encoding="utf-8")
    (repo / "agent-untracked.txt").write_text("new file from agent\n", encoding="utf-8")
    (repo / ".hermes-tmp.new").write_text("new harness litter\n", encoding="utf-8")
    diff = git_diff_since_baseline(repo, baseline)

    assert "tracked.txt" in diff["diff"]
    assert "+agent delta" in diff["diff"]
    assert "agent-untracked.txt" in diff["diff"]
    assert "+new file from agent" in diff["diff"]
    assert "preexisting-untracked.txt" not in diff["diff"]
    assert ".hermes-tmp" not in diff["diff"]
    assert diff["new_untracked_paths"] == ["agent-untracked.txt"]


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
    assert _git(first.workdir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "HEAD"
    expected_wt = runtime_root / "wt"
    assert first.workdir.parent == expected_wt or first.workdir.parent.name == "hermes-agent-wt"


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


def test_isolated_repo_context_gc_count_cap_reaps_oldest_clean_worktrees(tmp_path, monkeypatch):
    import agent_runtime.repo_context as rc

    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setattr(rc, "HARNESS_WORKTREE_GC_MAX_PER_REPO", 2)
    monkeypatch.setattr(rc, "HARNESS_WORKTREE_GC_MIN_AGE_SECONDS", 0)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Harness Test")
    (repo / "shared.txt").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    source = RepoExecutionContext(workdir=repo, repo_label="repo", source="test")

    a = isolated_repo_context_for_run(source, task_id="t", run_id="run_a")
    b = isolated_repo_context_for_run(source, task_id="t", run_id="run_b")
    c = isolated_repo_context_for_run(source, task_id="t", run_id="run_c")
    # Make age order deterministic and past the (0s) min-age grace: a < b < c.
    base = time.time() - 3600
    for idx, ctx in enumerate((a, b, c)):
        os.utime(ctx.workdir, (base + idx, base + idx))

    # Creating a 4th worktree runs GC; with cap=2 the oldest clean survivor (a) is reaped.
    d = isolated_repo_context_for_run(source, task_id="t", run_id="run_d")

    assert not a.workdir.exists()
    assert b.workdir.exists() and c.workdir.exists() and d.workdir.exists()


def test_isolated_repo_context_gc_count_cap_spares_dirty_and_fresh_worktrees(tmp_path, monkeypatch):
    import agent_runtime.repo_context as rc

    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setattr(rc, "HARNESS_WORKTREE_GC_MAX_PER_REPO", 1)
    # Large grace: every existing worktree is "fresh" so the count cap must skip them.
    monkeypatch.setattr(rc, "HARNESS_WORKTREE_GC_MIN_AGE_SECONDS", 24 * 60 * 60)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Harness Test")
    (repo / "shared.txt").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    source = RepoExecutionContext(workdir=repo, repo_label="repo", source="test")

    a = isolated_repo_context_for_run(source, task_id="t", run_id="run_a")
    b = isolated_repo_context_for_run(source, task_id="t", run_id="run_b")
    # Age `a` past the grace but leave uncommitted work in it — dirtiness protects it.
    (a.workdir / "wip.txt").write_text("in progress\n", encoding="utf-8")
    old = time.time() - (2 * 24 * 60 * 60)
    os.utime(a.workdir, (old, old))

    c = isolated_repo_context_for_run(source, task_id="t", run_id="run_c")

    # `a` survives because it is dirty; `b` survives because it is within the grace.
    assert a.workdir.exists() and b.workdir.exists() and c.workdir.exists()


def test_isolated_repo_context_fails_closed_when_worktree_create_fails(tmp_path, monkeypatch):
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

    real_run = subprocess.run

    def fake_run(command, *args, **kwargs):
        if command[:3] == ["git", "worktree", "add"]:
            return subprocess.CompletedProcess(command, 128, "", "simulated worktree failure")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    try:
        isolated_repo_context_for_run(source, task_id="task_1", run_id="run_fail")
    except ValueError as exc:
        assert "could not create isolated git worktree" in str(exc)
    else:
        raise AssertionError("worktree creation failure must fail closed")


def test_isolated_repo_context_materializes_ignored_env_placeholder(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(runtime_root))
    repo = tmp_path / "launcher"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Harness Test")
    (repo / "pubspec.yaml").write_text("name: launcher\n", encoding="utf-8")
    (repo / ".env").write_text("SECRET=value\n", encoding="utf-8")
    _git(repo, "add", "pubspec.yaml")
    _git(repo, "commit", "-m", "initial")
    source = RepoExecutionContext(workdir=repo, repo_label="launcher", source="test")

    isolated = isolated_repo_context_for_run(source, task_id="task_1", run_id="run_env")

    assert (isolated.workdir / ".env").is_file()
    assert (isolated.workdir / ".env").read_text(encoding="utf-8") == ""
    assert _git(isolated.workdir, "status", "--short").stdout.strip() == ""
