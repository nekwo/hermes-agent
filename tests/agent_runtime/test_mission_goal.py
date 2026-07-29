import importlib.util

from tools.registry import registry


def test_mission_goal_creation_modules_are_removed():
    assert importlib.util.find_spec("agent_runtime.mission_goal") is None
    assert importlib.util.find_spec("tools.mission_goal_tool") is None


def test_mission_goal_creation_tool_is_not_registered():
    assert registry.get_entry("mission_goal_create") is None
    assert "mission_goal" not in registry.get_registered_toolset_names()
