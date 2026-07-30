from pathlib import Path

from agent_runtime import paths


def test_store_root_uses_runtime_override(isolate_agent_runtime_root):
    assert paths.store_root() == Path(isolate_agent_runtime_root)
    assert paths.goals_dir() == Path(isolate_agent_runtime_root) / "goals"
    assert paths.runs_dir() == Path(isolate_agent_runtime_root) / "runs"
    assert paths.agents_dir() == Path(isolate_agent_runtime_root) / "agents"
    assert paths.incidents_dir() == Path(isolate_agent_runtime_root) / "incidents"
    assert paths.events_path() == Path(isolate_agent_runtime_root) / "events.jsonl"
    assert paths.lock_dir() == Path(isolate_agent_runtime_root) / "locks"
    assert paths.snapshot_path() == Path(isolate_agent_runtime_root) / "snapshot.json"


def test_entity_paths_are_sharded_under_store_root(isolate_agent_runtime_root):
    assert paths.task_path("task_1") == Path(isolate_agent_runtime_root) / "goals" / "task_1.json"
    assert paths.goal_path("goal_1") == Path(isolate_agent_runtime_root) / "goals" / "goal_1.json"
    assert paths.run_path("run_1") == Path(isolate_agent_runtime_root) / "runs" / "run_1.json"
    assert paths.agent_path("pm") == Path(isolate_agent_runtime_root) / "agents" / "pm.json"
    assert paths.incident_path("inc_1") == Path(isolate_agent_runtime_root) / "incidents" / "inc_1.json"
