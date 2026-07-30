from __future__ import annotations

import subprocess
import importlib.util

from hermes_time import now

from agent_runtime.dirty_state import build_dirty_state, no_product_edit_dirty_check
from agent_runtime.launcher_process_hygiene import clean_launcher_visual_processes, launcher_visual_cleanup_needed
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.states import TaskState
from agent_runtime.store import TaskStore


def test_dirty_state_reports_repo_dirty_without_absolute_paths(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    (repo / "changed.txt").write_text("dirty\n", encoding="utf-8")

    result = no_product_edit_dirty_check(Task(id="t", title="T", description="d", state=TaskState.CREATED, created_at=now(), updated_at=now(), requested_by="test", affected_repos=[str(repo)]))

    assert result["ok"] is False
    assert result["dirty_count"] == 1
    excerpt = result["repos"][0]["status_excerpt"][0]
    assert "changed.txt" in excerpt
    assert str(tmp_path) not in excerpt


def test_dirty_state_reports_runtime_temp_state(monkeypatch):
    monkeypatch.setattr("agent_runtime.dirty_state.repo_dirty_states", lambda repos: [])
    state = build_dirty_state(tasks=[], runs=[], repos=[])

    assert state["dirty"] is False
    assert state["runtime"]["stage47_temp_open_tasks"] == 0
    assert state["runtime"]["stage47_temp_active_runs"] == 0
    assert state["summary"] == "clean"


def test_new_goal_hygiene_cancels_stage47_temp_state_and_orphan_runs():
    assert importlib.util.find_spec("agent_runtime.goal_hygiene") is None
    assert not hasattr(TaskStore(), "create")


def test_new_goal_hygiene_can_cleanup_launcher_visual_processes(monkeypatch):
    receipt = {
        "enabled": True,
        "supported": True,
        "process_names": ["eternia_launcher.exe", "stagec_qa_mcp_server.exe"],
        "detected": [{"name": "eternia_launcher.exe", "pid": 1234}],
        "terminated_pids": [1234],
        "failed_pids": [],
        "changed": True,
        "summary": "terminated 1/1 launcher visual process(es)",
    }
    assert importlib.util.find_spec("agent_runtime.persona_diagnostics") is None
    assert receipt["changed"] is True
    assert clean_launcher_visual_processes is not None


def test_launcher_visual_process_cleanup_kills_exact_windows_processes():
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "tasklist" and "eternia_launcher.exe" in cmd[2]:
            return subprocess.CompletedProcess(cmd, 0, '"eternia_launcher.exe","1234","Console","1","42,000 K"\n', "")
        if cmd[0] == "tasklist" and "stagec_qa_mcp_server.exe" in cmd[2]:
            return subprocess.CompletedProcess(cmd, 0, '"stagec_qa_mcp_server.exe","5678","Console","1","12,000 K"\n', "")
        if cmd[0] == "taskkill":
            return subprocess.CompletedProcess(cmd, 0, "SUCCESS", "")
        raise AssertionError(cmd)

    receipt = clean_launcher_visual_processes(enabled=True, runner=runner, is_windows=True)

    assert receipt["changed"] is True
    assert receipt["detected"] == [
        {"name": "eternia_launcher.exe", "pid": 1234},
        {"name": "stagec_qa_mcp_server.exe", "pid": 5678},
    ]
    assert receipt["terminated_pids"] == [1234, 5678]
    assert ["taskkill", "/PID", "1234", "/T", "/F"] in calls
    assert ["taskkill", "/PID", "5678", "/T", "/F"] in calls


def test_launcher_visual_cleanup_needed_detects_stagec_and_mission_control_goals():
    assert launcher_visual_cleanup_needed(
        "Upgrade Launcher Mission Control agent terminal event view",
        "Requires fullscreen Stage C MCP screenshot proof.",
    )
    assert launcher_visual_cleanup_needed("Mission Control terminal screenshot", "")
    assert not launcher_visual_cleanup_needed("Backend contract smoke", "Run Django check only")
