import importlib.util


def test_mission_entry_gates_are_removed():
    assert importlib.util.find_spec("agent_runtime.gates") is None
