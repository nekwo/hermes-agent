from importlib.util import find_spec


def test_test_qa_intelligence_hardening_dispatch_contract_is_retired() -> None:
    assert find_spec("agent_runtime.ticker") is None
