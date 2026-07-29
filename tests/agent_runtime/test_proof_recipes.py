import importlib.util


def test_mission_proof_recipes_are_removed():
    assert importlib.util.find_spec("agent_runtime.proof_recipes") is None
