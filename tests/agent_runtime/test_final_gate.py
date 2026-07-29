import importlib.util


def test_automatic_mission_final_gate_is_removed():
    assert importlib.util.find_spec("agent_runtime.final_gate") is None
