import importlib.util


def test_mission_proof_runner_is_removed():
    assert importlib.util.find_spec("agent_runtime.proof_runner") is None
