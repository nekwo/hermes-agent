"""Opt-in real-binary capability proof. Uses its own port, preset, and owned job."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from agent_runtime.local_llama.process import OwnedProcess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--context", type=int, default=32768)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    alias = "hermes-local-probe"
    preset = args.output / "models.ini"
    def write_preset(context):
        preset.write_text(f"version = 1\n[{alias}]\nmodel = {args.model.as_posix()}\n"
                          f"c = {context}\nn-gpu-layers = auto\njinja = true\n"
                          "parallel = 1\nload-on-startup = false\n", encoding="utf-8")
    write_preset(args.context)
    token = secrets.token_urlsafe(32)
    environment = {k: v for k, v in os.environ.items() if not k.startswith("LLAMA_")}
    environment["LLAMA_API_KEY"] = token
    session = requests.Session()
    session.trust_env = False
    session.headers["Authorization"] = f"Bearer {token}"
    base = f"http://127.0.0.1:{port}"
    result = {"binary_sha256": hashlib.file_digest(args.executable.open("rb"), "sha256").hexdigest(),
              "context": args.context, "checks": {}, "complete": False}
    def request(path, payload=None, timeout=30):
        response = session.request("GET" if payload is None else "POST", base + path,
                                   json=payload, timeout=(2, timeout))
        response.raise_for_status()
        return response.json()
    def wait_ready(path, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                return request(path, timeout=2)
            except requests.RequestException:
                time.sleep(.5)
        raise TimeoutError(path)
    def record(name, value):
        result["checks"][name] = value
        (args.output / "receipt.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(name, flush=True)
    def wait_unloaded():
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            models = request("/models")
            if all(row["status"]["value"] == "unloaded" for row in models["data"]):
                return models
            time.sleep(.25)
        raise TimeoutError("model unload")
    try:
        with (args.output / "server.log").open("w", encoding="utf-8") as log:
            with OwnedProcess([str(args.executable), "--host", "127.0.0.1", "--port", str(port),
                               "--models-preset", str(preset), "--models-max", "1",
                               "--no-models-autoload"], cwd=args.output, output=log, env=environment):
                record("router_health", wait_ready("/health", 30))
                record("initial_models", request("/models"))
                record("unauthenticated_models_http_status", requests.get(base + "/models", timeout=2).status_code)
                record("load", request("/models/load", {"model": alias}, timeout=600))
                record("loaded_models", request("/v1/models"))
                record("props", wait_ready("/props?model=" + alias, 600))
                payload = {"model": alias, "messages": [{"role": "user", "content": "Reply with the word READY."}],
                           "max_tokens": 256, "chat_template_kwargs": {"enable_thinking": False}}
                record("text", request("/v1/chat/completions", payload, timeout=180))
                payload.update(messages=[{"role": "user", "content": "Use the echo tool with text hello."}],
                               tools=[{"type": "function", "function": {"name": "echo", "description": "Echo text",
                                 "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                                                "required": ["text"]}}}], tool_choice="required")
                tool = request("/v1/chat/completions", payload, timeout=180)
                record("tool_call", tool)
                message = tool["choices"][0]["message"]
                call = message["tool_calls"][0]
                payload["messages"] += [message, {"role": "tool", "tool_call_id": call["id"], "content": "hello"}]
                payload["tool_choice"] = "none"
                record("tool_result", request("/v1/chat/completions", payload, timeout=180))
                record("unload", request("/models/unload", {"model": alias}, timeout=60))
                record("unloaded_models", wait_unloaded())
                write_preset(4096)
                record("reload_presets", request("/models?reload=1"))
                record("reload_model", request("/models/load", {"model": alias}))
                record("reloaded_props", wait_ready("/props?model=" + alias, 600))
                record("final_unload", request("/models/unload", {"model": alias}, timeout=60))
                record("final_unloaded_models", wait_unloaded())
                result["complete"] = True
        record("owned_process_stopped", True)
    except BaseException as exc:
        record("failure", {"type": type(exc).__name__, "message": str(exc)})
        raise


if __name__ == "__main__":
    main()
