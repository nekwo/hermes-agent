"""Strict local-model settings, scoped to the Hermes root configuration."""
from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
import uuid

import yaml

LOAD_DEFAULTS = {"context_size": 32768, "gpu_layers": "auto", "flash_attention": "auto",
                 "cache_type_k": "f16", "cache_type_v": "f16", "chat_template_path": None}
GENERATION_DEFAULTS = {"temperature": .7, "top_p": .9, "top_k": 40,
                       "max_output_tokens": 4096, "thinking": "auto"}


class LocalLlamaError(Exception):
    def __init__(self, reason: str, message: str, *, code: int = -32602, **details):
        super().__init__(message)
        self.reason, self.code, self.details = reason, code, details

    def as_error(self):
        return {"reason": self.reason, "message": str(self), "retryable": False, "log_ref": None}


def identifier(value, name="model_id") -> str:
    try:
        if not isinstance(value, str):
            raise ValueError()
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise LocalLlamaError("invalid_parameter", f"{name} must be a UUID") from None


def integer(value, name, minimum=0, maximum=2**31-1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise LocalLlamaError("invalid_parameter", f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _keys(value, expected, name):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise LocalLlamaError("invalid_parameter", f"{name} must contain exactly: {', '.join(expected)}")


def _choice(value, options, name):
    if not isinstance(value, str) or value not in options:
        raise LocalLlamaError("invalid_parameter", f"Unsupported {name}")


def local_path(value, name, *, directory=False, nullable=False, exists=True):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value or any(c in value for c in "\r\n\0"):
        raise LocalLlamaError("invalid_parameter", f"{name} must be an absolute path on the Hermes host")
    path = Path(value)
    if not path.is_absolute():
        raise LocalLlamaError("invalid_parameter", f"{name} must be absolute")
    if exists and not (path.is_dir() if directory else path.is_file()):
        raise LocalLlamaError("missing_file", f"{name} does not exist on the Hermes host", code=-32000)
    return str(path.resolve())


def validate_parameters(load, generation, *, check_paths=True):
    _keys(load, LOAD_DEFAULTS, "load")
    _keys(generation, GENERATION_DEFAULTS, "generation")
    context = integer(load["context_size"], "context_size", 4096)
    if context % 256:
        raise LocalLlamaError("invalid_parameter", "context_size must be a multiple of 256")
    if load["gpu_layers"] != "auto":
        integer(load["gpu_layers"], "gpu_layers")
    _choice(load["flash_attention"], ("auto", "on", "off"), "flash_attention")
    for key in ("cache_type_k", "cache_type_v"):
        _choice(load[key], ("f16", "q8_0", "q4_0"), key)
    if load["cache_type_v"] != "f16" and load["flash_attention"] != "on":
        raise LocalLlamaError("invalid_parameter", "Quantized V cache requires Flash Attention on")
    local_path(load["chat_template_path"], "chat_template_path", nullable=True, exists=check_paths)
    for key, low, high in (("temperature", 0, 2), ("top_p", 0, 1)):
        value = generation[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise LocalLlamaError("invalid_parameter", f"Invalid {key}")
    if generation["top_p"] == 0:
        raise LocalLlamaError("invalid_parameter", "top_p must be positive")
    integer(generation["top_k"], "top_k", 0, 1000)
    integer(generation["max_output_tokens"], "max_output_tokens", 1, context - 1)
    # H0 proves the supplied Qwen's enable_thinking template parameter. Other
    # templates stay auto until their capability is independently established.
    _choice(generation["thinking"], ("auto", "on", "off"), "thinking")
    return deepcopy(load), deepcopy(generation)


def default_config():
    return {"schema_version": 1, "executable_path": None, "port": 8080,
            "model_roots": [], "presets": []}


def validate_config(value, *, check_paths=True):
    _keys(value, default_config(), "config")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise LocalLlamaError("invalid_parameter", "Unsupported configuration schema")
    integer(value["port"], "port", 1024, 65535)
    out = deepcopy(value)
    out["executable_path"] = local_path(value["executable_path"], "executable_path", nullable=True, exists=check_paths)
    if not isinstance(value["model_roots"], list) or len(value["model_roots"]) > 32:
        raise LocalLlamaError("invalid_parameter", "model_roots must be a list of at most 32 folders")
    out["model_roots"] = [local_path(p, "model_root", directory=True, exists=check_paths) for p in value["model_roots"]]
    if not isinstance(value["presets"], list) or len(value["presets"]) > 10000:
        raise LocalLlamaError("invalid_parameter", "presets must be a bounded list")
    seen = set()
    for preset in out["presets"]:
        _keys(preset, ("model_id", "display_name", "gguf_path", "revision", "load", "generation"), "preset")
        preset["model_id"] = identifier(preset["model_id"])
        if preset["model_id"] in seen:
            raise LocalLlamaError("invalid_parameter", "Duplicate model ID")
        seen.add(preset["model_id"])
        name = preset["display_name"]
        if not isinstance(name, str) or not name.strip() or len(name) > 120 or any(c in name for c in "\r\n\0"):
            raise LocalLlamaError("invalid_parameter", "Model name must be 1–120 characters")
        integer(preset["revision"], "preset revision")
        # Missing saved files remain visible and repairable through Settings.
        preset["gguf_path"] = local_path(preset["gguf_path"], "gguf_path", exists=False)
        if Path(preset["gguf_path"]).suffix.lower() != ".gguf":
            raise LocalLlamaError("invalid_parameter", "Weights must be a GGUF file")
        validate_parameters(preset["load"], preset["generation"], check_paths=check_paths)
    return out


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path

    def read(self):
        if not self.path.exists():
            return default_config()
        try:
            document = yaml.safe_load(self.path.read_bytes()) or {}
        except (yaml.YAMLError, OSError) as exc:
            raise LocalLlamaError("invalid_config", "Cannot read the Hermes root configuration") from exc
        if not isinstance(document, dict):
            raise LocalLlamaError("invalid_config", "Root config must be a mapping")
        return validate_config(document.get("local_llama", default_config()), check_paths=False)

    def write(self, config):
        from agent_runtime.store_file_io import store_lock
        from utils import atomic_roundtrip_yaml_update
        with store_lock(self.path.with_suffix(".local_llama.lock")):
            atomic_roundtrip_yaml_update(self.path, "local_llama", config)
