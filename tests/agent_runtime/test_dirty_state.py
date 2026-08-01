from __future__ import annotations

import subprocess
import importlib.util

from hermes_time import now

from agent_runtime.dirty_state import build_dirty_state, no_product_edit_dirty_check
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


def test_retired_persona_diagnostics_module_stays_gone():
    """S50 retargeted this from ``..._can_cleanup_launcher_visual_processes``.

    The old test asserted three things: that ``persona_diagnostics`` is gone,
    that a dict literal it built one line earlier had the value it had just been
    given (vacuous), and that ``clean_launcher_visual_processes`` was not None
    (an existence check on the module S50 deletes). Only the first was a real
    gate, so only the first survives — under a name that says what it checks.
    """

    assert importlib.util.find_spec("agent_runtime.persona_diagnostics") is None
