import importlib.util


def test_mission_promotion_gates_are_removed():
    assert importlib.util.find_spec("agent_runtime.promotion_gates") is None
