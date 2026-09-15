"""Opt-in isolated production manager/RPC/inference proof against an installed GGUF."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ["HERMES_HOME"] = str(args.output / "home")
    os.environ["HERMES_AGENT_RUNTIME_ROOT"] = str(args.output / "runtime")
    from agent_runtime import serve_rpc
    from agent_runtime.local_llama import service, PROVIDER_ID
    from agent_runtime.local_llama.config import default_config, LOAD_DEFAULTS, GENERATION_DEFAULTS
    from agent_runtime.profile_runner import AgentRunRequest, ProfileAgentRunner, _resolve_request_runtime
    from agent_runtime.local_llama.provider import turn_scope
    import requests

    service.bind(args.output / "runtime", args.output / "home" / "config.yaml")
    receipt = {"complete": False, "checks": {}}
    def record(name, value):
        receipt["checks"][name] = value
        (args.output / "runtime-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        print(name, flush=True)
    def call(method, params=None):
        reply = serve_rpc.handle_request({"jsonrpc": "2.0", "id": "probe", "method": "runtime.local_llama." + method,
                                          "params": params or {}}, serve_rpc.RpcContext())
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply["result"]
    def mutation(method, **extra):
        state = call("status")
        params = {"request_id": str(uuid.uuid4()), "expect_epoch": state["epoch"],
                  "expect_revision": state["revision"], "expect_config_revision": state["config_revision"], **extra}
        first = call(method, params)
        assert call(method, params)["operation"]["operation_id"] == first["operation"]["operation_id"]
        deadline = time.monotonic() + 660
        while time.monotonic() < deadline:
            op = call("status", {"request_id": params["request_id"]})["operation"]
            if op["state"] not in ("queued", "running"):
                record(method, op)
                assert op["state"] == "succeeded", op
                return
            time.sleep(.25)
        raise TimeoutError(method)
    try:
        config = default_config()
        with socket.socket() as port:
            port.bind(("127.0.0.1", 0))
            config["port"] = port.getsockname()[1]
        config["executable_path"] = str(args.executable)
        config["model_roots"] = [str(args.model.parent)]
        mutation("config.set", config=config)
        mutation("catalog.scan")
        preset = next(p for p in call("config.get")["config"]["presets"] if Path(p["gguf_path"]) == args.model)
        mutation("start")
        assert call("status")["server"]["state"] == "running"
        load = {**LOAD_DEFAULTS, "context_size": 8192}
        generation = {**GENERATION_DEFAULTS, "max_output_tokens": 1024}
        mutation("load", model_id=preset["model_id"], preset_revision=preset["revision"], load=load,
                 generation=generation, replace_model_id=None)
        request = AgentRunRequest(profile=None, provider=PROVIDER_ID, model=preset["model_id"],
                                  runtime_root=args.output / "runtime", turn_id="probe-turn", persona_instance_id="probe-agent")
        with turn_scope(request):
            runtime = _resolve_request_runtime(request)
            assert call("status")["active_turns"][0]["persona_instance_id"] == "probe-agent"
            session = requests.Session()
            session.trust_env = False
            response = session.post(runtime["base_url"] + "/chat/completions",
                                    headers={"Authorization": "Bearer " + runtime["api_key"]},
                                    json={"model": runtime["model"], "messages": [{"role": "user", "content": "Reply with READY."}],
                                          "max_tokens": 1024}, timeout=(2, 180))
            response.raise_for_status()
            record("runner_resolved_text", response.json())
        assert not call("status")["active_turns"]
        events = []
        request.enabled_toolsets = ["terminal"]
        request.user_message = "Run the terminal command echo LOCAL_LLAMA_PROOF, then report its output. Use the terminal tool."
        request.system_message = "Use the requested tool once and report the result concisely."
        request.max_iterations = 4
        request.max_wall_seconds = 180
        request.progress_callback = events.append
        request.workdir = args.output
        def inspect_agent(agent):
            from agent.auxiliary_client import get_text_auxiliary_client
            assert agent.context_compressor.context_length == 8192
            client, model = get_text_auxiliary_client("compression", main_runtime=agent._current_main_runtime())
            assert model == agent.model
            assert str(client.base_url).rstrip("/") == runtime["base_url"]
            assert client.api_key == runtime["api_key"]
            agent._check_compression_model_feasibility()
            assert not agent._fallback_chain
            record("auxiliary_local_route", {"same_model": True, "same_endpoint": True,
                                            "context_size": agent.context_compressor.context_length})
        request.agent_ready_callback = inspect_agent
        agent_result = ProfileAgentRunner().run(request)
        assert "auxiliary_local_route" in receipt["checks"], "Agent-ready verification failed"
        record("full_agent", {"response": agent_result.final_response,
                              "api_calls": agent_result.api_calls,
                              "tool_events": [e for e in events if "tool" in str(e.get("type", ""))]})
        assert "LOCAL_LLAMA_PROOF" in agent_result.final_response
        assert agent_result.api_calls >= 2
        assert not call("status")["active_turns"]
        mutation("unload", model_id=preset["model_id"])
        load["context_size"] = 4096
        mutation("load", model_id=preset["model_id"], preset_revision=preset["revision"], load=load,
                 generation=generation, replace_model_id=None)
        assert call("status")["models"][0]["active_parameters"]["effective_context_size"] == 4096
        mutation("stop")
        assert call("status")["server"]["state"] == "off"
        receipt["complete"] = True
        record("runtime_closed", True)
    finally:
        service.shutdown()


if __name__ == "__main__":
    main()
