from importlib.util import find_spec


def test_test_runtime_root_request_ordering_dispatch_contract_is_retired() -> None:
    assert find_spec("agent_runtime.ticker") is None
