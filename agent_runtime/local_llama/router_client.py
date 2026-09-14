"""Bounded, loopback-only access to the managed llama router."""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time
from urllib.parse import quote

import requests

from .config import LocalLlamaError
from .process import OwnedProcess


class RouterClient:
    def __init__(self, directory: Path):
        self.directory = directory
        self.process = None
        self.token = secrets.token_urlsafe(32)
        self.base_url = None
        self.cancel = threading.Event()
        self._process_lock = threading.RLock()
        self._closed = False
        self._session = requests.Session()
        self._session.trust_env = False
        self._session.headers["Authorization"] = f"Bearer {self.token}"

    def request(self, path, data=None, timeout=2):
        if self.cancel.is_set():
            raise LocalLlamaError("interrupted", "Local llama operation was interrupted", code=-32000)
        try:
            response = self._session.request("GET" if data is None else "POST", self.base_url + path,
                                             json=data, timeout=(2, timeout))
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            # Do not include raw server output, payloads, paths or credential URLs.
            raise LocalLlamaError("router_failed", "llama.cpp did not return a valid response", code=-32000) from exc

    @staticmethod
    def probe_binary(executable):
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            result = subprocess.run([executable, "--help"], capture_output=True, timeout=10,
                                    creationflags=flags, text=True, errors="replace")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LocalLlamaError("unsupported_binary", "Cannot inspect the configured llama.cpp executable", code=-32000) from exc
        if result.returncode or any(flag not in result.stdout for flag in
                                   ("--models-preset", "--no-models-autoload", "--models-max")):
            raise LocalLlamaError("unsupported_binary", "This llama.cpp build lacks managed router support", code=-32000)

    def _preset(self, model):
        from utils import atomic_write_text
        content = "version = 1\n"
        if model is not None:
            load = model["load"]
            alias = "hermes-local-" + model["model_id"]
            content += f"[{alias}]\nmodel = {Path(model['gguf_path']).as_posix()}\n"
            values = {"ctx-size": load["context_size"], "n-gpu-layers": load["gpu_layers"],
                      "flash-attn": load["flash_attention"], "cache-type-k": load["cache_type_k"],
                      "cache-type-v": load["cache_type_v"], "jinja": "true", "parallel": 1,
                      "load-on-startup": "false"}
            if load["chat_template_path"]:
                values["chat-template-file"] = Path(load["chat_template_path"]).as_posix()
            content += "".join(f"{k} = {v}\n" for k, v in values.items())
        atomic_write_text(self.directory / "models.ini", content)

    def start(self, config):
        self.probe_binary(config["executable_path"])
        with socket.socket() as check:
            try:
                check.bind(("127.0.0.1", config["port"]))
            except OSError as exc:
                raise LocalLlamaError("port_in_use", "Configured llama port is already in use", code=-32000) from exc
        self.directory.mkdir(parents=True, exist_ok=True)
        environment = {k: v for k, v in os.environ.items() if not k.startswith("LLAMA_")}
        environment["LLAMA_API_KEY"] = self.token
        self.base_url = f"http://127.0.0.1:{config['port']}"
        with self._process_lock:
            if self._closed:
                raise LocalLlamaError("interrupted", "Hermes is stopping", code=-32000)
            self._preset(None)
            self.cancel.clear()
            self.process = OwnedProcess([config["executable_path"], "--host", "127.0.0.1", "--port", str(config["port"]),
                                         "--models-preset", str(self.directory / "models.ini"), "--models-max", "1",
                                         "--no-models-autoload"], cwd=self.directory, env=environment)
        self._wait(lambda: self.request("/health").get("status") == "ok", 30)

    def _wait(self, predicate, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.cancel.is_set():
                raise LocalLlamaError("interrupted", "Local llama operation was interrupted", code=-32000)
            if self.process is None or self.process.poll() is not None:
                raise LocalLlamaError("router_failed", "The managed llama.cpp process exited", code=-32000)
            try:
                if predicate():
                    return
            except LocalLlamaError as exc:
                if exc.reason != "router_failed":
                    raise
            self.cancel.wait(.25)
        raise LocalLlamaError("timeout", "Timed out waiting for llama.cpp", code=-32000)

    def load(self, model):
        self._preset(model)
        self.request("/models?reload=1")
        alias = "hermes-local-" + model["model_id"]
        self.request("/models/load", {"model": alias})
        def ready():
            rows = self.request("/models").get("data", [])
            row = next((r for r in rows if r.get("id") == alias), {})
            status = row.get("status", {})
            if status.get("failed"):
                raise LocalLlamaError("model_load_failed", "llama.cpp could not load these weights/parameters", code=-32000)
            return status.get("value") == "loaded"
        self._wait(ready, 600)
        props = self.request("/props?model=" + quote(alias))
        return props

    def unload(self, model_id):
        alias = "hermes-local-" + model_id
        self.request("/models/unload", {"model": alias})
        self._wait(lambda: all(r.get("status", {}).get("value") == "unloaded"
                              for r in self.request("/models").get("data", []) if r.get("id") == alias), 60)

    def stop(self):
        # Router has no documented shutdown endpoint. Unload gracefully first;
        # Job close then terminates only this managed tree, including failed workers.
        with self._process_lock:
            process = self.process
            self.process = None
        if process is not None:
            process.close()

    def close(self):
        with self._process_lock:
            self._closed = True
            self.cancel.set()
        self.stop()
