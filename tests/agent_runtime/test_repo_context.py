from __future__ import annotations

from agent_runtime.repo_context import repo_execution_context_for_task, safe_affected_repo_labels
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.states import TaskState
from hermes_time import now


def _task_with_repos(repos):
    ts = now()
    task = Task(id="task_repo", title="Repo", description="d", state=TaskState.RUNNING, created_at=ts, updated_at=ts, requested_by="tony")
    task.affected_repos = repos
    return task


def test_absolute_affected_repo_subdirectory_normalizes_to_git_root(tmp_path):
    repo = tmp_path / "repo-root"
    nested = repo / "packages" / "service"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "AGENTS.md").write_text("# Instructions\n", encoding="utf-8")
    task = _task_with_repos([str(nested)])

    ctx = repo_execution_context_for_task(task)

    assert ctx is not None
    assert ctx.workdir == repo.resolve()
    assert ctx.repo_label == "repo-root"
    assert ctx.context_files == ("AGENTS.md",)
    assert ctx.context_excerpts[0].label == "AGENTS.md"
    assert "Instructions" in ctx.context_excerpts[0].content
    assert safe_affected_repo_labels(task.affected_repos) == ["repo-root"]


def test_git_file_worktree_marker_normalizes_to_repo_root(tmp_path):
    repo = tmp_path / "worktree-root"
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    (repo / ".git").write_text("gitdir: ../.git/worktrees/worktree-root\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("# Instructions\n", encoding="utf-8")
    task = _task_with_repos([str(nested)])

    ctx = repo_execution_context_for_task(task)

    assert ctx is not None
    assert ctx.workdir == repo.resolve()
    assert ctx.repo_label == "worktree-root"
    assert ctx.context_files == ("AGENTS.md",)


def test_unresolved_affected_repo_error_uses_safe_labels_not_raw_paths(tmp_path):
    missing = tmp_path / "private" / "secret" / "missing-repo"
    task = _task_with_repos([str(missing)])

    try:
        repo_execution_context_for_task(task)
    except ValueError as exc:
        message = str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected unresolved repo to fail closed")

    assert "missing-repo" in message
    assert str(tmp_path) not in message
    assert "secret" not in message


def test_absolute_non_git_directory_remains_explicit_workdir(tmp_path):
    workdir = tmp_path / "standalone"
    workdir.mkdir()
    task = _task_with_repos([str(workdir)])

    ctx = repo_execution_context_for_task(task)

    assert ctx is not None
    assert ctx.workdir == workdir.resolve()
    assert ctx.repo_label == "standalone"


def test_repo_context_excerpts_are_bounded_and_redact_secret_like_assignments(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text(
        "Use focused tests.\nAPI_KEY=abc123\n" + ("x" * 3000),
        encoding="utf-8",
    )
    task = _task_with_repos([str(repo)])

    ctx = repo_execution_context_for_task(task)

    assert ctx is not None
    assert ctx.context_files == ("CLAUDE.md",)
    assert len(ctx.context_excerpts) == 1
    excerpt = ctx.context_excerpts[0]
    assert excerpt.label == "CLAUDE.md"
    assert "Use focused tests." in excerpt.content
    assert "API_KEY=<redacted>" in excerpt.content
    assert "abc123" not in excerpt.content
    assert excerpt.truncated is True
