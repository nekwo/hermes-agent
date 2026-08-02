import os
import subprocess
import time
from pathlib import Path

from agent_runtime.repo_context import (
    HARNESS_WORKTREE_BASE_MAX_CHARS,
    HARNESS_WORKTREE_ADD_TIMEOUT_SECONDS,
    RepoExecutionContext,
    isolated_repo_context_for_run,
)


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


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
    assert first.workdir.parent == tmp_path / "worktrees"
    assert first.workdir.parent.name != "hermes-agent-wt"


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


def _backend_like_repo(tmp_path, *, venv_contents: bool = True):
    repo = tmp_path / "eternia-backend"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Harness Test")
    (repo / "manage.py").write_text("# manage\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".EterniaBackendVirtualEnv/\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    venv = repo / ".EterniaBackendVirtualEnv"
    if venv_contents:
        scripts = venv / "Scripts"
        scripts.mkdir(parents=True)
        (scripts / "python.exe").write_text("fake interpreter\n", encoding="utf-8")
        (venv / "pyvenv.cfg").write_text("home = fake\n", encoding="utf-8")
    else:
        venv.mkdir()
    return repo


def test_worktree_removal_severs_venv_link_and_preserves_real_venv(tmp_path, monkeypatch):
    """git worktree remove --force follows directory junctions on Windows; the
    GC must sever support links first so the REAL venv survives (live incident
    2026-07-01: the first count-cap burst emptied the backend venv)."""
    import agent_runtime.repo_context as rc

    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(runtime_root))
    repo = _backend_like_repo(tmp_path)
    source = RepoExecutionContext(workdir=repo, repo_label="eternia-backend", source="test")

    isolated = isolated_repo_context_for_run(source, task_id="t", run_id="run_a")
    link = isolated.workdir / ".EterniaBackendVirtualEnv"
    assert link.exists(), "venv support link must be materialized into the worktree"
    assert (link / "Scripts" / "python.exe").exists()

    removed = rc._remove_harness_worktree(repo, isolated.workdir, reason="test")

    assert removed is True
    assert not isolated.workdir.exists()
    assert (repo / ".EterniaBackendVirtualEnv" / "Scripts" / "python.exe").exists(), (
        "removing the worktree must never delete the real venv contents through the link"
    )


def test_gc_count_cap_with_venv_links_preserves_real_venv(tmp_path, monkeypatch):
    import agent_runtime.repo_context as rc

    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setattr(rc, "HARNESS_WORKTREE_GC_MAX_PER_REPO", 1)
    monkeypatch.setattr(rc, "HARNESS_WORKTREE_GC_MIN_AGE_SECONDS", 0)
    repo = _backend_like_repo(tmp_path)
    source = RepoExecutionContext(workdir=repo, repo_label="eternia-backend", source="test")

    a = isolated_repo_context_for_run(source, task_id="t", run_id="run_a")
    b = isolated_repo_context_for_run(source, task_id="t", run_id="run_b")
    base = time.time() - 3600
    for idx, ctx in enumerate((a, b)):
        os.utime(ctx.workdir, (base + idx, base + idx))

    c = isolated_repo_context_for_run(source, task_id="t", run_id="run_c")

    assert not a.workdir.exists(), "count cap should reap the oldest clean worktree"
    assert c.workdir.exists()
    assert (repo / ".EterniaBackendVirtualEnv" / "Scripts" / "python.exe").exists(), (
        "count-cap GC must not empty the real venv through the worktree's link"
    )


def test_materialize_worktree_support_logs_degraded_event_for_hollow_venv(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(runtime_root))
    repo = _backend_like_repo(tmp_path, venv_contents=False)
    source = RepoExecutionContext(workdir=repo, repo_label="eternia-backend", source="test")

    isolated_repo_context_for_run(source, task_id="t", run_id="run_a")

    events_path = runtime_root / "worktree_events.jsonl"
    assert events_path.is_file()
    lines = [line for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    degraded = [line for line in lines if '"worktree_support_degraded"' in line]
    assert degraded, "a hollow venv (no interpreter) must surface a degraded support event"
    assert any("venv_missing_interpreter" in line for line in degraded)


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


def test_isolated_repo_context_uses_large_checkout_timeout(tmp_path, monkeypatch):
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
    observed: list[int | None] = []
    real_run = subprocess.run

    def fake_run(command, *args, **kwargs):
        if command[:3] == ["git", "worktree", "add"]:
            observed.append(kwargs.get("timeout"))
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    isolated_repo_context_for_run(source, task_id="task_1", run_id="run_timeout")

    assert observed == [HARNESS_WORKTREE_ADD_TIMEOUT_SECONDS]


def test_long_runtime_root_uses_short_temp_worktree_base(
    tmp_path, monkeypatch, production_worktree_base_functions
):
    import tempfile
    import agent_runtime.repo_context as rc

    production_current, _ = production_worktree_base_functions
    monkeypatch.setattr(rc, "_worktree_base_dir", production_current)

    long_root = tmp_path / ("runtime_" + ("x" * HARNESS_WORKTREE_BASE_MAX_CHARS))
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(long_root))

    assert rc._worktree_base_dir() == Path(tempfile.gettempdir()) / "hermes-agent-wt"


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


def test_backend_worktree_env_is_copied_not_empty_placeholder(tmp_path, monkeypatch):
    """Django settings hard-require env values, so the backend worktree .env must
    carry the repo-local dev env; an empty placeholder made every read-only
    proof fail (live 2026-07-03, task_826869af)."""
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(runtime_root))
    repo = _backend_like_repo(tmp_path)
    (repo / ".env").write_text("DJANGO_SECRET_KEY=dev-secret\n", encoding="utf-8")
    source = RepoExecutionContext(workdir=repo, repo_label="eternia-backend", source="test")

    isolated = isolated_repo_context_for_run(source, task_id="t", run_id="run_env")

    env = isolated.workdir / ".env"
    assert env.is_file()
    assert env.read_text(encoding="utf-8") == "DJANGO_SECRET_KEY=dev-secret\n"
    assert _git(isolated.workdir, "status", "--short").stdout.strip() == ""


def test_non_django_worktree_env_stays_empty_placeholder(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(runtime_root))
    repo = tmp_path / "launcher"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Harness Test")
    (repo / "pubspec.yaml").write_text("name: launcher\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    (repo / ".env").write_text("SECRET=value\n", encoding="utf-8")
    source = RepoExecutionContext(workdir=repo, repo_label="launcher", source="test")

    isolated = isolated_repo_context_for_run(source, task_id="t", run_id="run_env")

    assert (isolated.workdir / ".env").read_text(encoding="utf-8") == ""
