from importlib.util import find_spec


def test_worker_actions_mission_dispatch_surface_is_retired() -> None:
    assert find_spec("agent_runtime.worker_actions") is None
