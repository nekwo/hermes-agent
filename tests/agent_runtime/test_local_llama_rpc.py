import uuid

import pytest

from agent_runtime import serve_rpc
from agent_runtime.call_authorization import UNKNOWN_CALLER, RpcCaller, CALLER_DEVICE, CALLER_PEER
from agent_runtime.local_llama.manager import LocalLlamaManager
from agent_runtime.local_llama import rpc


@pytest.fixture
def manager(tmp_path, monkeypatch):
    manager = LocalLlamaManager(tmp_path / "runtime", tmp_path / "config.yaml", "install-test")
    monkeypatch.setattr(rpc, "get_manager", lambda: manager)
    yield manager
    manager.close()


def call(suffix, params=None, caller=None):
    context = serve_rpc.RpcContext() if caller is None else serve_rpc.RpcContext(caller=caller)
    return serve_rpc.handle_request({"jsonrpc": "2.0", "id": "test-call", "method": "runtime.local_llama." + suffix,
                                     "params": params or {}}, context)


def test_real_dispatch_returns_typed_state_and_config(manager):
    assert call("status")["result"]["server"]["state"] == "off"
    assert call("config.get")["result"]["config"]["executable_path"] is None
    manifest = serve_rpc.manifest()
    for name in rpc.METHODS:
        assert "runtime.local_llama." + name in manifest["methods"]


def test_mutation_validation_and_unknown_lookup_are_typed(manager):
    assert call("start")["error"]["code"] == -32602
    assert call("status", {"request_id": str(uuid.uuid4())})["error"]["code"] == 4001


def test_unknown_caller_cannot_mutate_or_read_config(manager, monkeypatch):
    def forbidden():
        pytest.fail("unauthorized call reached manager")
    monkeypatch.setattr(rpc, "get_manager", forbidden)
    for suffix, tier in rpc.METHODS.items():
        if tier == "console":
            assert "error" in call(suffix, caller=UNKNOWN_CALLER)


def test_status_preserves_existing_read_tier_policy(manager):
    assert call("status", caller=UNKNOWN_CALLER)["result"]["server"]["state"] == "off"


def test_peer_cannot_reach_even_status(manager, monkeypatch):
    def forbidden():
        pytest.fail("peer call reached manager")
    monkeypatch.setattr(rpc, "get_manager", forbidden)
    peer = RpcCaller(kind=CALLER_PEER, transport="gateway", peer_install_id="peer-test")
    for suffix in rpc.METHODS:
        assert "error" in call(suffix, caller=peer)


def test_peer_allowlist_does_not_gain_local_controls():
    from agent_runtime.call_authorization import PEER_METHOD_ALLOWLIST
    assert not any("runtime.local_llama." + name in PEER_METHOD_ALLOWLIST for name in rpc.METHODS)
