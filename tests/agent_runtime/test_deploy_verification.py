from importlib.util import find_spec


def test_ticker_mission_dispatch_surface_is_retired() -> None:
    assert find_spec("agent_runtime.ticker") is None
