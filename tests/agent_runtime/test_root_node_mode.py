from importlib.util import find_spec


def test_root_node_engine_mission_dispatch_surface_is_retired() -> None:
    assert find_spec("agent_runtime.root_node_engine") is None
