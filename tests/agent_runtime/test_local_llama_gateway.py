"""Real TLS device -> owning serve -> local manager; no transport mocks."""
import time
import uuid

from agent_runtime.call_authorization import TIER_CONSOLE, TIER_READ
from agent_runtime.local_llama import service
from tests.agent_runtime.test_serve_gateway_lane import (
    gateway_on, device_client, pair_device, running_serve, _rpc,
)


def test_console_device_can_configure_only_the_served_host(gateway_on):
    credential = pair_device(tier=TIER_CONSOLE)
    with running_serve() as handle:
        with device_client(handle, credential) as (connection, hello):
            assert "runtime.local_llama.config.set" in hello["rpc"]["methods"]
            state = _rpc(connection, "runtime.local_llama.status")["result"]
            settings = _rpc(connection, "runtime.local_llama.config.get")["result"]
            assert state["install_id"] == settings["install_id"]
            config = settings["config"]
            config["port"] = 8189
            params = {"request_id": str(uuid.uuid4()), "expect_epoch": state["epoch"],
                      "expect_revision": state["revision"], "expect_config_revision": state["config_revision"],
                      "config": config}
            first = _rpc(connection, "runtime.local_llama.config.set", params)["result"]
            duplicate = _rpc(connection, "runtime.local_llama.config.set", params)["result"]
            assert first["operation"]["operation_id"] == duplicate["operation"]["operation_id"]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                result = _rpc(connection, "runtime.local_llama.status", {"request_id": params["request_id"]})["result"]
                if result["operation"]["state"] == "succeeded":
                    break
                time.sleep(.01)
            assert result["operation"]["state"] == "succeeded"
            assert _rpc(connection, "runtime.local_llama.config.get")["result"]["config"]["port"] == 8189
    assert not service._bindings


def test_read_device_cannot_view_paths_or_mutate(gateway_on):
    credential = pair_device(tier=TIER_READ)
    with running_serve() as handle:
        with device_client(handle, credential) as (connection, _):
            assert _rpc(connection, "runtime.local_llama.status")["result"]["server"]["state"] == "off"
            for method in ("config.get", "config.set", "start", "stop", "load", "unload", "catalog.scan", "logs.get"):
                assert "error" in _rpc(connection, "runtime.local_llama." + method)
