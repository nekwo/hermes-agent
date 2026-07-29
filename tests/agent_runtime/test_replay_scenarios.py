import importlib.util


def test_mission_replay_scenarios_are_removed():
    assert importlib.util.find_spec("agent_runtime.replay_scenarios") is None
