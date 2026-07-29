import importlib.util


def test_burn_in_lane_is_removed():
    assert importlib.util.find_spec("agent_runtime.burn_in") is None
