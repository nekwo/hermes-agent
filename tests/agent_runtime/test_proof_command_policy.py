import importlib.util

from agent_runtime.stagec_command_policy import reject_invalid_stagec_screenshot_window_args


def test_mission_proof_command_policy_is_removed_but_stagec_policy_survives():
    assert importlib.util.find_spec("agent_runtime.proof_command_policy") is None
    assert callable(reject_invalid_stagec_screenshot_window_args)
