import importlib.util


def test_mission_proof_gates_are_removed():
    assert importlib.util.find_spec("agent_runtime.proof_gates") is None
