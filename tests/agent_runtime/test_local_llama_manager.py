from copy import deepcopy
from pathlib import Path
import threading
import time
import uuid

import pytest

from agent_runtime.local_llama.config import default_config, LocalLlamaError
from agent_runtime.local_llama.manager import LocalLlamaManager


class FakeRouter:
    def __init__(self, directory):
        self.process = None
        self.started = 0
        self.stopped = 0
        self.block = threading.Event()
        self.block.set()
        self.token = "test-secret"
        self.base_url = "http://127.0.0.1:8181"

    def start(self, config):
        self.started += 1
        assert self.block.wait(5)

    def stop(self):
        self.stopped += 1

    def close(self):
        self.block.set()
        self.stop()


@pytest.fixture
def manager(tmp_path):
    manager = LocalLlamaManager(tmp_path / "runtime", tmp_path / "config.yaml", "install-test", router_factory=FakeRouter)
    manager.config["executable_path"] = "test-executable"
    yield manager
    manager.close()


def params(manager, **extra):
    state = manager.status()
    return {"request_id": str(uuid.uuid4()), "expect_epoch": state["epoch"],
            "expect_revision": state["revision"], "expect_config_revision": state["config_revision"], **extra}


def settle(manager, request):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        operation = manager.status(request_id=request["request_id"])["operation"]
        if operation["state"] not in ("queued", "running"):
            return operation
        time.sleep(.01)
    pytest.fail("operation did not settle")


def test_start_ack_is_nonblocking_and_duplicate_has_one_effect(manager):
    manager.router.block.clear()
    request = params(manager)
    first = manager.submit("start", request)
    second = manager.submit("start", request)
    assert first["operation"]["operation_id"] == second["operation"]["operation_id"]
    assert first["operation"]["state"] in ("queued", "running")
    assert manager.status()["server"]["state"] in ("off", "starting")
    manager.router.block.set()
    assert settle(manager, request)["state"] == "succeeded"
    assert manager.router.started == 1
    assert manager.status()["server"]["state"] == "running"


def test_off_status_exposes_host_catalog_without_configuration_paths(manager):
    model_id = str(uuid.uuid4())
    manager.config["presets"] = [{"model_id": model_id, "display_name": "Remote weights",
        "revision": 2, "load": {"context_size": 8192}, "gguf_path": "private-host-path.gguf"}]
    state = manager.status()
    assert state["server"]["state"] == "off"
    assert state["configured"] is True
    assert state["models"][0]["model_id"] == model_id
    assert state["models"][0]["context_length"] == 8192
    assert "private-host-path" not in str(state)
    assert "test-executable" not in str(state)
    manager.config["executable_path"] = None
    assert manager.status()["configured"] is False


def test_reused_request_id_with_different_payload_refused(manager):
    request = params(manager)
    manager.submit("start", request)
    with pytest.raises(LocalLlamaError, match="different operation"):
        manager.submit("stop", request)


def test_busy_and_stale_guards_are_server_enforced(manager):
    request = params(manager)
    manager.router.block.clear()
    manager.submit("start", request)
    with pytest.raises(LocalLlamaError) as caught:
        manager.submit("stop", params(manager))
    assert caught.value.reason == "operation_busy"
    manager.router.block.set()
    settle(manager, request)
    with pytest.raises(LocalLlamaError) as caught:
        manager.submit("stop", {**request, "request_id": str(uuid.uuid4())})
    assert caught.value.reason == "stale_revision"


def test_turn_lease_prevents_unload_and_releases_on_exception(manager):
    model_id = str(uuid.uuid4())
    manager.server, manager.loaded = "running", model_id
    manager.model_states[model_id] = {"active_parameters": {"test": True}}
    with pytest.raises(RuntimeError):
        with manager.lease(model_id, "turn-1", "agent-1"):
            with pytest.raises(LocalLlamaError) as caught:
                manager.submit("stop", params(manager))
            assert caught.value.reason == "active_turns"
            raise RuntimeError("turn failed")
    assert manager.status()["active_turns"] == []


def test_old_epoch_cannot_replay_on_new_manager(tmp_path):
    manager = LocalLlamaManager(tmp_path / "runtime", tmp_path / "config.yaml", "install-test", router_factory=FakeRouter)
    request = params(manager)
    manager.submit("stop", request)
    assert settle(manager, request)["state"] == "succeeded"
    old_id = manager.status(request_id=request["request_id"])["operation"]["operation_id"]
    manager.close()
    replacement = LocalLlamaManager(tmp_path / "runtime", tmp_path / "config.yaml", "install-test", router_factory=FakeRouter)
    try:
        assert replacement.submit("stop", request)["operation"]["operation_id"] == old_id
        assert replacement.router.stopped == 0
        with pytest.raises(LocalLlamaError) as caught:
            replacement.submit("stop", {**request, "request_id": str(uuid.uuid4())})
        assert caught.value.reason == "stale_epoch"
    finally:
        replacement.close()


def test_read_projection_never_contains_internal_credentials(manager):
    assert "test-secret" not in str(manager.status())
    assert "test-secret" not in str(manager.config_get())


def test_receipt_keeps_bounded_digest_and_rejects_unknown_fields(manager):
    request = params(manager)
    manager.submit("stop", request)
    settle(manager, request)
    digest = manager.receipts[request["request_id"]]["fingerprint"]
    assert len(digest) == 64
    with pytest.raises(LocalLlamaError, match="Unknown"):
        manager.submit("stop", params(manager, shell_command="not-an-option"))


def test_second_manager_cannot_claim_same_runtime(manager):
    with pytest.raises(LocalLlamaError) as caught:
        LocalLlamaManager(manager.directory.parent, manager.config_store.path, "other", router_factory=FakeRouter)
    assert caught.value.reason == "manager_unavailable"


def test_storage_failure_cannot_start_an_unrecorded_process(manager, monkeypatch):
    def unavailable():
        raise OSError("disk unavailable")
    with monkeypatch.context() as patch:
        patch.setattr(manager, "_persist", unavailable)
        with pytest.raises(LocalLlamaError) as caught:
            manager.submit("start", params(manager))
    assert caught.value.reason == "storage_unavailable"
    assert manager.router.started == 0
    assert manager.operation is None
    request = params(manager)
    manager.submit("start", request)
    assert settle(manager, request)["state"] == "succeeded"


def test_initialization_failure_releases_process_ownership(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("local_llama: [broken\n")
    with pytest.raises(LocalLlamaError):
        LocalLlamaManager(tmp_path / "runtime", path, "test", router_factory=FakeRouter)
    path.write_text("{}")
    replacement = LocalLlamaManager(tmp_path / "runtime", path, "test", router_factory=FakeRouter)
    replacement.close()


def test_shutdown_interrupts_pending_operation_and_refuses_new_commands(manager):
    manager.router.block.clear()
    request = params(manager)
    manager.submit("start", request)
    manager.close()
    assert manager.status(request_id=request["request_id"])["operation"]["state"] == "interrupted"
    with pytest.raises(LocalLlamaError) as caught:
        manager.submit("start", params(manager))
    assert caught.value.reason == "manager_unavailable"


def test_missing_replacement_preserves_current_loaded_model(manager, tmp_path):
    from agent_runtime.local_llama.config import LOAD_DEFAULTS, GENERATION_DEFAULTS
    old, new = str(uuid.uuid4()), str(uuid.uuid4())
    manager.server, manager.loaded = "running", old
    manager.model_states[old] = {"state": "ready", "active_parameters": {
        "load": deepcopy(LOAD_DEFAULTS), "generation": deepcopy(GENERATION_DEFAULTS)}}
    manager.config["presets"] = [{"model_id": new, "display_name": "missing", "revision": 0,
        "gguf_path": str(tmp_path / "gone.gguf"), "load": deepcopy(LOAD_DEFAULTS), "generation": deepcopy(GENERATION_DEFAULTS)}]
    request = params(manager, model_id=new, preset_revision=0, load=deepcopy(LOAD_DEFAULTS),
                     generation=deepcopy(GENERATION_DEFAULTS), replace_model_id=old)
    manager.submit("load", request)
    operation = settle(manager, request)
    assert operation["error"]["reason"] == "missing_file"
    assert manager.loaded == old
    assert manager.server == "running"
