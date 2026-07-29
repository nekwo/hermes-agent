from __future__ import annotations

from hermes_time import now

from agent_runtime.context_builder import build_context, render_context
from agent_runtime.context_requests import add_context_request, has_unresolved_context_request
from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.models import AgentRun, Task
from agent_runtime.observability import build_observability
from agent_runtime.states import RunState, TaskState
from tests.agent_runtime.conftest import release_to_implementation


def _task():
    ts = now()
    return Task(id="task_ctx", title="T", description="d", state=TaskState.RUNNING, created_at=ts, updated_at=ts, requested_by="tony")


def _run(task):
    ts = now()
    return AgentRun(id="run_ctx", persona_id="dev", task_id=task.id, stage_id=None, state=RunState.RUNNING, started_at=ts, last_heartbeat_at=ts)


def test_context_request_fulfills_safe_file_and_renders_next_dev_context(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n    return 'ok'\n", encoding="utf-8")
    task = _task()

    req = add_context_request(task, actor="dev", payload={"paths": ["src/app.py"], "reason": "need code"}, root=tmp_path)
    ctx = build_context(task, _run(task))
    rendered = render_context(ctx)

    assert req["status"] == "fulfilled"
    assert req["bundle_id"]
    assert "## Fulfilled File Context" in rendered
    assert "def main" in rendered
    assert has_unresolved_context_request(task) is False


def test_context_request_uses_later_affected_repo_when_cwd_lacks_file(tmp_path):
    cwd_root = tmp_path / "cwd"
    repo_root = tmp_path / "repo"
    (repo_root / "src").mkdir(parents=True)
    (cwd_root).mkdir()
    (repo_root / "src" / "app.py").write_text("def repo_only():\n    return 'ok'\n", encoding="utf-8")
    task = _task()
    task.affected_repos = [str(repo_root)]

    req = add_context_request(task, actor="dev", payload={"paths": ["src/app.py"], "reason": "need repo file"}, root=cwd_root)

    assert req["status"] == "fulfilled"
    assert "def repo_only" in req["bundle"]["files"][0]["content"]
    assert has_unresolved_context_request(task) is False


def test_context_request_returns_partial_bundle_with_per_path_feedback(tmp_path):
    cwd_root = tmp_path / "cwd"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    cwd_root.mkdir()
    (repo_root / "manage.py").write_text("print('manage')\n", encoding="utf-8")
    task = _task()
    task.affected_repos = [str(repo_root)]

    req = add_context_request(
        task,
        actor="dev",
        payload={"paths": ["manage.py", "missing.py"], "reason": "need locator files"},
        root=cwd_root,
    )
    ctx = build_context(task, _run(task))

    assert req["status"] == "fulfilled_partial"
    assert req["failure_reason"] == "partial_context_unavailable"
    assert req["bundle"]["files"][0]["path"] == "manage.py"
    assert {"path": "missing.py", "status": "unsupported", "failure_reason": "path_not_found"} in req["path_results"]
    assert has_unresolved_context_request(task) is False
    feedback = ctx.mission_hud["terminal_feedback"]
    assert feedback["action_result"] == "context_available_partial"
    assert feedback["next_expected"] == "use_partial_context_then_request_one_missing_path_or_block"


def test_context_request_returns_bounded_directory_listing(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / "moderation" / "nested").mkdir(parents=True)
    (repo_root / "moderation" / "services.py").write_text("def check(): pass\n", encoding="utf-8")
    (repo_root / "moderation" / "nested" / "models.py").write_text("class Rule: pass\n", encoding="utf-8")
    task = _task()
    task.affected_repos = [str(repo_root)]

    req = add_context_request(task, actor="dev", payload={"paths": ["moderation"], "reason": "find moderation files"}, root=tmp_path / "cwd")

    assert req["status"] == "fulfilled"
    assert req["bundle"]["files"][0]["kind"] == "directory_listing"
    assert "Directory listing for moderation" in req["bundle"]["files"][0]["content"]
    assert "services.py" in req["bundle"]["files"][0]["content"]
    assert "nested/models.py" in req["bundle"]["files"][0]["content"]


def test_context_request_prefers_affected_repo_for_relative_directory(tmp_path):
    cwd_root = tmp_path / "cwd"
    repo_root = tmp_path / "repo"
    (cwd_root / "wrong").mkdir(parents=True)
    (repo_root / "moderation").mkdir(parents=True)
    (cwd_root / "wrong" / "harness.py").write_text("print('wrong root')\n", encoding="utf-8")
    (repo_root / "moderation" / "services.py").write_text("def check(): pass\n", encoding="utf-8")
    task = _task()
    task.affected_repos = [str(repo_root)]

    req = add_context_request(task, actor="dev", payload={"paths": ["."], "reason": "list repo root"}, root=cwd_root)

    content = req["bundle"]["files"][0]["content"]
    assert req["status"] == "fulfilled"
    assert "moderation/services.py" in content
    assert "wrong/harness.py" not in content


def test_context_request_returns_partial_bundle_when_directory_exists_and_file_missing(tmp_path):
    cwd_root = tmp_path / "cwd"
    repo_root = tmp_path / "repo"
    (repo_root / "posts").mkdir(parents=True)
    cwd_root.mkdir()
    (repo_root / "posts" / "models.py").write_text("class Post: pass\n", encoding="utf-8")
    task = _task()
    task.affected_repos = [str(repo_root)]

    req = add_context_request(
        task,
        actor="dev",
        payload={"paths": ["posts", "backend/settings.py"], "reason": "need app map"},
        root=cwd_root,
    )

    assert req["status"] == "fulfilled_partial"
    assert req["bundle"]["files"][0]["kind"] == "directory_listing"
    assert {"path": "backend/settings.py", "status": "unsupported", "failure_reason": "path_not_found"} in req["path_results"]
    assert has_unresolved_context_request(task) is False


def test_context_request_uses_git_root_for_nested_affected_repo(tmp_path):
    cwd_root = tmp_path / "cwd"
    repo_root = tmp_path / "repo"
    nested = repo_root / "pkg" / "service"
    nested.mkdir(parents=True)
    cwd_root.mkdir()
    (repo_root / ".git").mkdir()
    (repo_root / "AGENTS.md").write_text("# repo instructions\n", encoding="utf-8")
    task = _task()
    task.affected_repos = [str(nested)]

    req = add_context_request(task, actor="qa", payload={"paths": ["AGENTS.md"], "reason": "need root context"}, root=cwd_root)

    assert req["status"] == "fulfilled"
    assert "repo instructions" in req["bundle"]["files"][0]["content"]


def test_context_request_can_read_safe_proof_artifact_from_runtime_root(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    artifact = runtime_root / "proofs" / "task_ctx" / "artifacts" / "proof.log"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("command: pytest\nexit_code: 0\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(runtime_root))
    task = _task()

    req = add_context_request(task, actor="qa", payload={"paths": ["proofs/task_ctx/artifacts/proof.log"], "reason": "inspect proof"}, root=tmp_path / "cwd")

    assert req["status"] == "fulfilled"
    assert "exit_code: 0" in req["bundle"]["files"][0]["content"]


def test_context_request_duplicate_fingerprint_includes_repo_scope(tmp_path):
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()
    (repo_a / "a.py").write_text("print('a')\n", encoding="utf-8")
    (repo_b / "b.py").write_text("print('b')\n", encoding="utf-8")
    task = _task()
    task.affected_repos = [str(repo_a)]

    first = add_context_request(task, actor="dev", payload={"paths": ["."], "reason": "list repo"}, root=tmp_path / "cwd")
    task.affected_repos = [str(repo_b)]
    second = add_context_request(task, actor="dev", payload={"paths": ["."], "reason": "list repo"}, root=tmp_path / "cwd")

    assert first["status"] == "fulfilled"
    assert second["status"] == "fulfilled"
    assert first["fingerprint"] != second["fingerprint"]
    assert "a.py" in first["bundle"]["files"][0]["content"]
    assert "b.py" in second["bundle"]["files"][0]["content"]


def test_context_request_supports_line_windows_and_masks_secret_lines(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    lines = [f"line {idx}" for idx in range(1, 21)]
    lines[5] = "Authorization: Bearer abcdefghijklmnop"
    (repo_root / "service.py").write_text("\n".join(lines), encoding="utf-8")
    task = _task()
    task.affected_repos = [str(repo_root)]

    req = add_context_request(task, actor="dev", payload={"paths": ["service.py#L5-L8"], "reason": "need narrow excerpt"}, root=tmp_path)

    file = req["bundle"]["files"][0]
    assert req["status"] == "fulfilled"
    assert file["kind"] == "file_window"
    assert file["start_line"] == 5
    assert file["end_line"] == 8
    assert "<line 2 redacted>" in file["content"]
    assert "line 8" in file["content"]


def test_context_request_oversize_file_returns_skeleton_with_window_hint(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    body = "\n".join([f"def fn_{idx}(): pass" for idx in range(12000)])
    (repo_root / "large.py").write_text(body, encoding="utf-8")
    task = _task()
    task.affected_repos = [str(repo_root)]

    req = add_context_request(task, actor="dev", payload={"paths": ["large.py"], "reason": "need map"}, root=tmp_path)

    file = req["bundle"]["files"][0]
    assert req["status"] == "fulfilled_partial"
    assert file["kind"] == "file_skeleton"
    assert "Use path#Lstart-Lend" in file["content"]
    assert any(item["failure_reason"] == "file_too_large_use_windows" for item in req["path_results"])
