from importlib.util import find_spec


def test_no_freeze_monitor_mission_dispatch_surface_is_retired() -> None:
    assert find_spec("agent_runtime.no_freeze_monitor") is None
