"""Managed local identity, whole-turn lease, and context-local inference routing."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path

from . import DISPLAY_NAME, PROVIDER_ID
from .config import ConfigStore, LocalLlamaError
from .service import get_manager


def provider_profile():
    from providers.base import ProviderProfile
    return ProviderProfile(name=PROVIDER_ID, display_name=DISPLAY_NAME, api_mode="chat_completions")


def catalog_visibility():
    from agent_runtime.config import harness_root_config_path
    config = ConfigStore(harness_root_config_path()).read()
    return {"schema": "hermes.local_llama.catalog/v1", "provider_id": PROVIDER_ID,
            "display_name": DISPLAY_NAME, "configured": bool(config.get("executable_path")),
            "models": [{"model_id": p["model_id"], "display_name": p["display_name"],
                        "context_length": p["load"]["context_size"],
                        "selectable": Path(p["gguf_path"]).is_file(),
                        "unavailable_reason": None if Path(p["gguf_path"]).is_file() else "missing_file"}
                       for p in config.get("presets", [])]}


def resolve(model_id, *, root=None):
    return get_manager(root=root, create=False).runtime(model_id)


def project_config(config, runtime):
    parameters = runtime["local_parameters"]
    model = config.get("model")
    model = dict(model) if isinstance(model, dict) else {}
    model.update(provider="custom", default=runtime["model"], base_url=runtime["base_url"],
                 context_length=parameters["effective_context_size"])
    config["model"] = model
    # Each auxiliary model task inherits the managed runtime. No API credential
    # enters the config projection; scoped_runtime_main carries it in memory.
    auxiliary = config.setdefault("auxiliary", {})
    if not isinstance(auxiliary, dict):
        auxiliary = config["auxiliary"] = {}
    for key in set(auxiliary) | {"compression"}:
        if key == "compression" or isinstance(auxiliary.get(key), dict):
            auxiliary[key] = {"provider": "main", "model": runtime["model"], "fallback_chain": [],
                              "context_length": parameters["effective_context_size"]}
    return config


@contextmanager
def turn_scope(request):
    if request.provider != PROVIDER_ID:
        yield
        return
    from agent.auxiliary_client import scoped_runtime_main
    from hermes_cli.config_read_scope import readonly_config_scope
    manager = get_manager(root=request.runtime_root, create=False)
    with manager.lease(request.model, request.turn_id or request.session_id or "local-turn",
                       request.persona_instance_id or "local-agent") as runtime:
        with scoped_runtime_main(runtime), readonly_config_scope(lambda cfg: project_config(cfg, runtime)):
            yield


@contextmanager
def prewarm_scope(request):
    if request.provider != PROVIDER_ID or not request.prewarm_only:
        yield
        return
    from agent.auxiliary_client import scoped_runtime_main
    from hermes_cli.config_read_scope import readonly_config_scope
    runtime = resolve(request.model, root=request.runtime_root)
    with scoped_runtime_main(runtime), readonly_config_scope(lambda cfg: project_config(cfg, runtime)):
        yield


def construction_kwargs(runtime):
    if "local_parameters" not in runtime:
        return {}
    generation = runtime["local_parameters"]["generation"]
    return {"requested_provider": PROVIDER_ID, "max_tokens": generation["max_output_tokens"],
            "fallback_model": [], "request_overrides": {"temperature": generation["temperature"],
              "top_p": generation["top_p"], "extra_body": {"top_k": generation["top_k"]}}}


def actor_signature(runtime, original):
    if "local_parameters" not in runtime:
        return original
    # A reload with changed parameters/credential cannot reuse the prior client.
    digest = hashlib.sha256(json.dumps({"parameters": runtime["local_parameters"],
                                       "endpoint": runtime["base_url"], "credential": runtime["api_key"]},
                                      sort_keys=True).encode()).hexdigest()
    return original + ":local-llama:" + digest
