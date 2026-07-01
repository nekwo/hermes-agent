import subprocess

from agent_runtime.repo_context import capture_repo_baseline, git_diff_since_baseline


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
