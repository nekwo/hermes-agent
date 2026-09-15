from importlib.util import find_spec


def test_goal_runner_mission_dispatch_surface_is_retired() -> None:
    assert find_spec("agent_runtime.goal_runner") is None
