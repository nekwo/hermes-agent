import json

from tools.terminal_tool import terminal_tool


def _blocked(command: str, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path))
    result = json.loads(terminal_tool(command))
    audit = (tmp_path / "blocked_tool_attempts.jsonl").read_text(encoding="utf-8")
    return result, json.loads(audit.splitlines()[-1])


def test_harness_terminal_blocks_push_wipe_exfil_network_and_prod_ops(tmp_path, monkeypatch):
    cases = {
        "git push origin main": "git_push_requires_operator_approval",
        "git reset --hard HEAD": "tree_wipe_blocked",
        "cat .env": "credential_read_blocked",
        "curl https://example.com": "network_command_requires_allowlist",
        "kubectl apply -f prod.yaml": "prod_operation_requires_operator_approval",
    }
    for command, reason in cases.items():
        result, audit = _blocked(command, tmp_path, monkeypatch)
        assert result["status"] == "blocked"
        assert result["blocked_by"] == "harness_execution_safety"
        assert result["block_reason"] == reason
        assert audit["reason"] == reason


def test_harness_terminal_blocks_uncommitted_work_destroyers(tmp_path, monkeypatch):
    cases = {
        "git checkout .": "tree_wipe_blocked",
        "git checkout -- lib/app.py": "tree_wipe_blocked",
        "git checkout -f feature/reset-tree": "tree_wipe_blocked",
        "git switch --force feature/reset-tree": "tree_wipe_blocked",
        "git restore --worktree lib/app.py": "tree_wipe_blocked",
        "git restore --staged lib/app.py": "tree_wipe_blocked",
        "git stash drop stash@{0}": "tree_wipe_blocked",
        "git stash clear": "tree_wipe_blocked",
        "git reset --merge": "tree_wipe_blocked",
    }
    for command, reason in cases.items():
        result, audit = _blocked(command, tmp_path, monkeypatch)
        assert result["status"] == "blocked"
        assert result["blocked_by"] == "harness_execution_safety"
        assert result["block_reason"] == reason
        assert audit["reason"] == reason
        assert audit["command_preview"] == command


def test_harness_terminal_allows_loopback_network_probe(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path))
    from tools.terminal_tool import _harness_safety_block

    assert _harness_safety_block("curl http://127.0.0.1:8000/health") is None


def test_harness_terminal_allows_non_destructive_branch_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path))
    from tools.terminal_tool import _harness_safety_block

    assert _harness_safety_block("git checkout feature/read-only-inspection") is None
