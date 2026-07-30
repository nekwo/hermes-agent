from __future__ import annotations

import subprocess
from datetime import timedelta

from hermes_time import now

from agent_runtime.dirty_state import build_dirty_state, no_product_edit_dirty_check
from agent_runtime.goal_hygiene import prepare_new_goal_runtime
from agent_runtime.launcher_process_hygiene import clean_launcher_visual_processes, launcher_visual_cleanup_needed
from agent_runtime.models import AgentRun
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.states import RunState, TaskState
from agent_runtime.store import RunStore, TaskStore


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


def test_dirty_state_reports_runtime_temp_state():
    ts = TaskStore()
    runs = RunStore()
    stamp = now()
    task = ts.create(Task(id="task_burn_old", title="Old burn-in", description="d", state=TaskState.RUNNING, created_at=stamp, updated_at=stamp, requested_by="stage47_burn_in"))
    runs.open_run("dev", task.id)

    state = build_dirty_state(tasks=ts.list_all(), runs=runs.list_all(), repos=[])

    assert state["dirty"] is True
    assert state["runtime"]["stage47_temp_open_tasks"] == 1
    assert state["runtime"]["stage47_temp_active_runs"] == 1
    assert "stage47_temp=1 task(s)/1 run(s)" in state["summary"]


def test_new_goal_hygiene_cancels_stage47_temp_state_and_orphan_runs():
    ts = TaskStore()
    runs = RunStore()
    stamp = now()
    temp = ts.create(Task(id="task_burn_old", title="Old burn-in", description="d", state=TaskState.RUNNING, created_at=stamp, updated_at=stamp, requested_by="stage47_burn_in"))
    keep = ts.create(Task(id="task_real", title="Real goal", description="d", state=TaskState.RUNNING, created_at=stamp, updated_at=stamp, requested_by="tony"))
    temp_run = runs.open_run("dev", temp.id)
    runs.open_run("dev", keep.id)
    orphan = AgentRun(id="run_orphan", persona_id="qa", task_id="missing_task", stage_id=None, state=RunState.RUNNING, started_at=stamp, last_heartbeat_at=stamp - timedelta(seconds=999))
    from agent_runtime import paths
    from utils import atomic_json_write

    atomic_json_write(paths.run_path(orphan.id), {
        "id": orphan.id,
        "persona_id": orphan.persona_id,
        "task_id": orphan.task_id,
        "stage_id": orphan.stage_id,
        "state": orphan.state.value,
        "started_at": orphan.started_at.isoformat(),
        "last_heartbeat_at": orphan.last_heartbeat_at.isoformat(),
    })

    report = prepare_new_goal_runtime(task_store=ts, run_store=runs, cleanup_stage47_temp=True, heartbeat_ttl_seconds=1)

    assert report["preserved_evidence"] is True
    assert temp.id in report["cancelled_task_ids"]
    assert temp_run.id in report["cancelled_run_ids"]
    assert runs.get(temp_run.id).state == RunState.CANCELLED
    assert ts.get(temp.id).state == TaskState.CANCELLED
    assert ts.get(keep.id).state == TaskState.RUNNING
    assert runs.find_active(task_id=keep.id)


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
    monkeypatch.setattr("agent_runtime.goal_hygiene.clean_launcher_visual_processes", lambda *, enabled: receipt if enabled else {"enabled": False})

    report = prepare_new_goal_runtime(
        task_store=TaskStore(),
        run_store=RunStore(),
        cleanup_launcher_visual_processes=True,
    )

    assert report["cleanup_launcher_visual_processes"] is True
    assert report["launcher_visual_process_cleanup"] == receipt
    assert report["preserved_evidence"] is True
    assert report["product_repos_modified"] is False


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
