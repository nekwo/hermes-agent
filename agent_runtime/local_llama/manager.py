"""Serialized lifecycle, durable mutation receipts, and turn leases for local llama."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from collections import deque
import json
import hashlib
from pathlib import Path
import threading
import time
import uuid

from agent_runtime.store_file_io import iso_stamp, read_json_object, store_lock
from agent_runtime.locks import HarnessLockUnavailable
from utils import atomic_json_write

from . import DISPLAY_NAME, PROVIDER_ID, SCHEMA
from .catalog import validate_model, scan
from .config import ConfigStore, LocalLlamaError, identifier, integer, validate_config, validate_parameters
from .router_client import RouterClient


class LocalLlamaManager:
    def __init__(self, root: Path, config_path: Path, install_id: str, *, router_factory=RouterClient):
        self.directory = root / "local_llama"
        self.directory.mkdir(parents=True, exist_ok=True)
        self._ownership = store_lock(self.directory / "owner.lock", timeout_seconds=0)
        try:
            self._ownership.__enter__()
        except HarnessLockUnavailable as exc:
            raise LocalLlamaError("manager_unavailable", "Another Hermes process owns local llama", code=-32000) from exc
        try:
            self._initialize(config_path, install_id, router_factory)
        except BaseException:
            self._ownership.__exit__(None, None, None)
            raise

    def _initialize(self, config_path, install_id, router_factory):
        self.lock = threading.RLock()
        self.config_store = ConfigStore(config_path)
        self.config = self.config_store.read()
        self.install_id = install_id
        self.epoch = str(uuid.uuid4())
        self.revision = 0
        self.config_revision = read_json_object(self.directory / "state.json").get("config_revision", 0)
        self.router = router_factory(self.directory)
        self.server = "off"
        self.server_error = None
        self.loaded = None
        self.model_states = {}
        self.leases = {}
        self.operation = None
        self._worker = None
        self.logs = deque(maxlen=2000)
        self._log_sequence = 0
        self.closed = False
        self.unavailable = {p["model_id"]: "missing_file" for p in self.config["presets"]
                            if not Path(p["gguf_path"]).is_file()}
        self.stop_event = threading.Event()
        self.receipts = read_json_object(self.directory / "operations.json")
        for row in self.receipts.values():
            op = row["operation"]
            if op["state"] in ("queued", "running"):
                op.update(state="interrupted", finished_at=iso_stamp(None),
                          error=LocalLlamaError("interrupted", "Hermes restarted during this operation").as_error())
        self._persist()
        self._watcher = threading.Thread(target=self._watch, name="local-llama-health", daemon=True)
        self._watcher.start()

    def _persist(self):
        terminal = sorted((k for k, v in self.receipts.items() if v["operation"]["state"] not in ("queued", "running")),
                          key=lambda k: self.receipts[k]["accepted_at"])
        for key in terminal:
            if len(self.receipts) <= 1000 and time.time() - self.receipts[key]["accepted_at"] < 7 * 86400:
                break
            del self.receipts[key]
        atomic_json_write(self.directory / "operations.json", self.receipts)
        atomic_json_write(self.directory / "state.json", {"config_revision": self.config_revision})

    def _log(self, message):
        self._log_sequence += 1
        self.logs.append({"sequence": self._log_sequence, "time": iso_stamp(None), "level": "info", "message": message})

    def _model(self, model_id):
        row = next((p for p in self.config["presets"] if p["model_id"] == model_id), None)
        if row is None:
            raise LocalLlamaError("model_not_found", "Saved model does not exist", code=4001)
        return row

    def capabilities(self):
        return {"supported": True, "reason": None, "router_load_unload": True,
                "parameter_schema_version": 1,
                "supported_values": {"thinking": ["auto"], "flash_attention": ["auto", "on", "off"],
                                     "cache_type_k": ["f16", "q8_0", "q4_0"], "cache_type_v": ["f16", "q8_0", "q4_0"]}}

    def status(self, *, operation_id=None, request_id=None):
        with self.lock:
            operation = self.operation
            if operation_id and request_id:
                raise LocalLlamaError("invalid_parameter", "Choose one operation lookup")
            if request_id:
                operation = self.receipts.get(identifier(request_id, "request_id"), {}).get("operation")
            if operation_id:
                identifier(operation_id, "operation_id")
                operation = next((r["operation"] for r in self.receipts.values()
                                  if r["operation"]["operation_id"] == operation_id), None)
            if (operation_id or request_id) and operation is None:
                raise LocalLlamaError("operation_not_found", "Operation receipt is no longer available", code=4001)
            rows = []
            for preset in self.config["presets"]:
                state = self.model_states.get(preset["model_id"], {})
                rows.append({"model_id": preset["model_id"], "display_name": preset["display_name"],
                             "context_length": preset["load"]["context_size"],
                             "preset_revision": preset["revision"], "state": state.get("state", "unloaded"),
                             "selectable": preset["model_id"] not in self.unavailable,
                             "unavailable_reason": self.unavailable.get(preset["model_id"]),
                             "active_parameters": state.get("active_parameters"), "error": state.get("error")})
            return deepcopy({"schema": SCHEMA, "install_id": self.install_id, "epoch": self.epoch,
                             "revision": self.revision, "config_revision": self.config_revision,
                             "configured": bool(self.config.get("executable_path")),
                             "capabilities": self.capabilities(), "server": {"state": self.server, "error": self.server_error},
                             "models": rows, "active_turns": list(self.leases.values()), "operation": operation})

    def config_get(self):
        with self.lock:
            return {"schema": SCHEMA, "install_id": self.install_id, "config_revision": self.config_revision,
                    "config": deepcopy(self.config), "capabilities": self.capabilities()}

    def logs_get(self, cursor=None, limit=100):
        integer(limit, "limit", 1, 200)
        try:
            after = int(cursor) if cursor is not None else None
        except (ValueError, TypeError):
            raise LocalLlamaError("invalid_parameter", "Invalid log cursor") from None
        with self.lock:
            candidates = [r for r in self.logs if after is None or r["sequence"] > after]
            rows = candidates[-limit:] if after is None else candidates[:limit]
            return {"schema": SCHEMA, "install_id": self.install_id,
                    "lines": [{k: v for k, v in row.items() if k != "sequence"} for row in rows],
                    "next_cursor": str(rows[-1]["sequence"]) if rows else cursor,
                    "truncated": len(candidates) > len(rows)}

    def submit(self, kind, params):
        allowed = {"config.set": ("config",), "catalog.scan": (), "start": (), "stop": (),
                   "load": ("model_id", "preset_revision", "load", "generation", "replace_model_id"),
                   "unload": ("model_id",)}
        if kind not in allowed:
            raise LocalLlamaError("unknown_operation", "Unknown local llama operation")
        request_id = identifier(params.get("request_id"), "request_id")
        keys = ("request_id", "expect_epoch", "expect_revision", "expect_config_revision") + allowed[kind]
        if set(params) - set(keys):
            raise LocalLlamaError("invalid_parameter", "Unknown local llama operation parameter")
        params = dict(params)
        for key in ("model_id", "replace_model_id"):
            if params.get(key) is not None:
                params[key] = identifier(params[key], key)
        normalized = {k: params.get(k) for k in keys}
        try:
            canonical = json.dumps({"kind": kind, "params": normalized}, sort_keys=True, allow_nan=False)
            fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        except (ValueError, TypeError):
            raise LocalLlamaError("invalid_parameter", "Parameters must contain finite JSON values") from None
        with self.lock:
            if self.closed:
                raise LocalLlamaError("manager_unavailable", "Local llama manager is stopping", code=-32000)
            if request_id in self.receipts:
                receipt = self.receipts[request_id]
                if receipt["fingerprint"] != fingerprint:
                    raise LocalLlamaError("idempotency_conflict", "Request ID was already used for a different operation", code=4090)
                return self._reply(receipt["operation"])
            if normalized["expect_epoch"] != self.epoch:
                raise LocalLlamaError("stale_epoch", "Hermes restarted; refresh local llama state", code=4090)
            for name, actual in (("expect_revision", self.revision), ("expect_config_revision", self.config_revision)):
                integer(normalized[name], name)
                if normalized[name] != actual:
                    raise LocalLlamaError("stale_revision", "Local llama state changed; refresh and retry", code=4090)
            if self.operation and self.operation["state"] in ("queued", "running"):
                raise LocalLlamaError("operation_busy", "A local llama operation is already running", code=4090)
            if self.leases and kind in ("start", "stop", "load", "unload"):
                raise LocalLlamaError("active_turns", "A local model turn is active", code=4090,
                                     active_turns=deepcopy(list(self.leases.values())))
            if kind == "load":
                model = self._model(identifier(params.get("model_id")))
                integer(params.get("preset_revision"), "preset_revision")
                if params.get("preset_revision") != model["revision"]:
                    raise LocalLlamaError("stale_revision", "Model preset changed", code=4090)
                validate_parameters(params.get("load"), params.get("generation"))
                if params["generation"]["thinking"] != "auto":
                    raise LocalLlamaError("unsupported_parameter", "This preset has no verified thinking override")
                if self.server != "running":
                    raise LocalLlamaError("model_not_ready", "Turn on llama first", code=4090)
                if self.loaded and params.get("replace_model_id") != self.loaded:
                    active = self.model_states[self.loaded]["active_parameters"]
                    if self.loaded != model["model_id"] or active["load"] != params["load"] or active["generation"] != params["generation"]:
                        raise LocalLlamaError("replacement_required", "Explicitly name the loaded model to replace", code=4090)
            elif kind == "unload":
                self._model(identifier(params.get("model_id")))
            elif kind == "start" and not self.config.get("executable_path"):
                raise LocalLlamaError("unconfigured", "Configure the llama.cpp executable first", code=-32000)
            op = {"operation_id": str(uuid.uuid4()), "request_id": request_id, "kind": kind, "state": "queued",
                  "model_id": normalized.get("model_id"), "progress": None, "error": None,
                  "started_at": None, "finished_at": None}
            previous_operation, previous_revision = self.operation, self.revision
            self.receipts[request_id] = {"fingerprint": fingerprint, "accepted_at": time.time(), "operation": op}
            self.operation = op
            self.revision += 1
            try:
                self._persist()
            except OSError as exc:
                self.receipts.pop(request_id, None)
                self.operation, self.revision = previous_operation, previous_revision
                raise LocalLlamaError("storage_unavailable", "Cannot save the local llama operation receipt", code=-32000) from exc
            worker = threading.Thread(target=self._execute, args=(kind, deepcopy(normalized), op), daemon=True,
                                      name="local-llama-operation")
            self._worker = worker
            worker.start()
            return self._reply(op)

    def _reply(self, operation):
        return {"schema": SCHEMA, "install_id": self.install_id,
                "operation": deepcopy(operation), "state": self.status()}

    def _transition(self, server=None, model=None, state=None, parameters=None):
        with self.lock:
            if self.closed:
                raise LocalLlamaError("interrupted", "Hermes is stopping", code=-32000)
            if server is not None:
                self.server, self.server_error = server, None
            if model is not None:
                self.model_states[model] = {"state": state, "active_parameters": parameters, "error": None}
            self.revision += 1

    def _save(self, config):
        with self.lock:
            if self.closed:
                raise LocalLlamaError("interrupted", "Hermes is stopping", code=-32000)
            self.config_store.write(config)
            self.config = config
            self.unavailable = {p["model_id"]: "missing_file" for p in config["presets"]
                                if not Path(p["gguf_path"]).is_file()}
            self.config_revision += 1
            self.revision += 1

    def _execute(self, kind, params, op):
        load_started = False
        with self.lock:
            op.update(state="running", started_at=iso_stamp(None))
        try:
            if kind == "config.set":
                config = validate_config(params["config"])
                with self.lock:
                    if self.server != "off" and any(config[k] != self.config[k] for k in ("executable_path", "port", "model_roots")):
                        raise LocalLlamaError("restart_required", "Turn off llama before changing server settings", code=4090)
                    if self.loaded and not any(p["model_id"] == self.loaded for p in config["presets"]):
                        raise LocalLlamaError("restart_required", "Unload a model before removing its preset", code=4090)
                    old = {p["model_id"]: p for p in self.config["presets"]}
                    for preset in config["presets"]:
                        previous = old.get(preset["model_id"])
                        if previous and preset["revision"] != previous["revision"]:
                            raise LocalLlamaError("stale_revision", "Model preset changed", code=4090)
                        preset["revision"] = previous["revision"] + (preset != previous) if previous else 0
                self._save(config)
            elif kind == "catalog.scan":
                config, report = scan(self.config)
                self._save(config)
                op["scan"] = report
            elif kind == "start" and self.server != "running":
                if self.server != "off":
                    raise LocalLlamaError("restart_required", "Turn off llama to clear the previous failure", code=4090)
                self._transition(server="starting")
                self.router.start(self.config)
                self._transition(server="running")
            elif kind == "stop":
                self._transition(server="stopping")
                try:
                    if self.loaded:
                        self.router.unload(self.loaded)
                finally:
                    self.router.stop()
                with self.lock:
                    self.loaded = None
                    self.model_states.clear()
                self._transition(server="off")
            elif kind == "load":
                model = deepcopy(self._model(params["model_id"]))
                model.update(load=params["load"], generation=params["generation"])
                if self.loaded == model["model_id"] and self.model_states[self.loaded]["active_parameters"] == {
                    "load": model["load"], "generation": model["generation"], "effective_context_size": model["load"]["context_size"]}:
                    pass
                else:
                    info = validate_model(Path(model["gguf_path"]))
                    contexts = [v for k, v in info.items() if k.endswith(".context_length") and type(v) is int]
                    if contexts and model["load"]["context_size"] > min(contexts):
                        raise LocalLlamaError("invalid_parameter", "Context exceeds the model metadata maximum")
                    if self.loaded:
                        self._unload(self.loaded)
                    self._transition(model=model["model_id"], state="loading")
                    load_started = True
                    props = self.router.load(model)
                    caps = props.get("chat_template_caps", {})
                    if not caps.get("supports_tools") or not caps.get("supports_tool_calls"):
                        raise LocalLlamaError("unsupported_model", "This model template does not support agent tool calls", code=-32000)
                    context = props.get("default_generation_settings", {}).get("n_ctx")
                    if type(context) is not int or context != model["load"]["context_size"]:
                        self.router.unload(model["model_id"])
                        raise LocalLlamaError("context_mismatch", "Loaded context does not match the requested context", code=-32000)
                    with self.lock:
                        self.loaded = model["model_id"]
                    self._transition(model=self.loaded, state="ready", parameters={"load": model["load"],
                                     "generation": model["generation"], "effective_context_size": context})
            elif kind == "unload" and self.loaded == params["model_id"]:
                self._unload(self.loaded)
            with self.lock:
                if self.closed:
                    raise LocalLlamaError("interrupted", "Hermes stopped", code=-32000)
                op.update(state="succeeded", finished_at=iso_stamp(None))
                self._log(kind + " succeeded")
        except Exception as exc:
            error = exc if isinstance(exc, LocalLlamaError) else LocalLlamaError("operation_failed", "Local llama operation failed; verify configuration and available memory", code=-32000)
            router_failed = kind in ("start", "stop", "unload")
            if kind == "load" and load_started:
                try:
                    self.router.unload(params["model_id"])
                except Exception:
                    router_failed = True
            if router_failed:
                self.router.stop()
            with self.lock:
                if router_failed:
                    self.server = "failed"
                    self.server_error = error.as_error()
                    self.loaded = None
                    self.model_states.clear()
                elif kind == "load" and load_started:
                    self.loaded = None
                    self.model_states[params["model_id"]] = {"state": "failed", "active_parameters": None,
                                                            "error": error.as_error()}
                op.update(state="interrupted" if self.closed else "failed", error=error.as_error(), finished_at=iso_stamp(None))
                self._log(kind + " failed: " + error.reason)
        finally:
            with self.lock:
                self.revision += 1
                if not self.closed:
                    self._persist()

    def _unload(self, model_id):
        self._transition(model=model_id, state="unloading")
        self.router.unload(model_id)
        with self.lock:
            self.loaded = None
        self._transition(model=model_id, state="unloaded")

    @contextmanager
    def lease(self, model_id, turn_id, persona_instance_id):
        key = str(uuid.uuid4())
        with self.lock:
            if self.operation and self.operation["state"] in ("queued", "running"):
                raise LocalLlamaError("operation_busy", "Local llama is changing state", code=4090)
            if self.closed or self.server != "running" or self.loaded != model_id:
                raise LocalLlamaError("model_not_ready", "Load the selected local model before sending a message", code=4090)
            self.leases[key] = {"turn_id": turn_id, "persona_instance_id": persona_instance_id, "model_id": model_id}
            self.revision += 1
        try:
            yield self.runtime(model_id)
        finally:
            with self.lock:
                self.leases.pop(key, None)
                self.revision += 1

    def runtime(self, model_id):
        with self.lock:
            if self.closed or self.server != "running" or self.loaded != model_id:
                raise LocalLlamaError("model_not_ready", "Load the selected local model before sending a message", code=4090)
            return {"provider": "custom", "model": "hermes-local-" + model_id,
                    "api_mode": "chat_completions", "base_url": self.router.base_url + "/v1",
                    "api_key": self.router.token, "local_parameters": deepcopy(self.model_states[model_id]["active_parameters"])}

    def _watch(self):
        failures = 0
        while not self.stop_event.wait(2):
            with self.lock:
                if self.server != "running" or (self.operation and self.operation["state"] in ("queued", "running")):
                    failures = 0
                    continue
                process = self.router.process
                revision = self.revision
            if process is None:
                continue
            exited = process.poll() is not None
            try:
                healthy = not exited and self.router.request("/health").get("status") == "ok"
            except LocalLlamaError:
                healthy = False
            failures = 0 if healthy else failures + 1
            with self.lock:
                # A concurrent command/turn invalidates this observation. Never
                # reconcile an old probe against a replacement process or load.
                if self.closed or self.revision != revision or self.router.process is not process:
                    failures = 0
                    continue
                if exited or failures >= 3:
                    self.router.stop()
                    self.server = "failed"
                    self.server_error = LocalLlamaError("router_failed", "The managed llama.cpp process exited or stopped responding").as_error()
                    self.loaded = None
                    self.model_states.clear()
                    self.revision += 1
                    self._log("router failed health check")

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.stop_event.set()
            if self.operation and self.operation["state"] in ("queued", "running"):
                self.operation.update(state="interrupted", finished_at=iso_stamp(None),
                                      error=LocalLlamaError("interrupted", "Hermes service stopped").as_error())
            self._persist()
        self.router.close()
        if self._worker is not None:
            self._worker.join(timeout=5)
        self._watcher.join(timeout=3)
        self._ownership.__exit__(None, None, None)
