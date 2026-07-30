from importlib.util import find_spec


def test_test_dev_discipline_dispatch_contract_is_retired() -> None:
    assert find_spec("agent_runtime.ticker") is None
