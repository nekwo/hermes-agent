"""Only the socket-owning serve may create a local-model manager."""
from __future__ import annotations

from pathlib import Path
import threading

from .config import LocalLlamaError

_lock = threading.RLock()
_bindings = {}


def bind(root: Path, config_path: Path):
    with _lock:
        key = str(root.resolve())
        if key not in _bindings:
            _bindings[key] = {"root": root, "config": config_path, "manager": None}


def get_manager(*, root=None, create=True):
    from agent_runtime.paths import store_root
    from agent_runtime.gateway_identity import ensure_install_identity
    from .manager import LocalLlamaManager
    key = str(Path(root).resolve() if root is not None else store_root().resolve())
    with _lock:
        binding = _bindings.get(key)
        if binding is None:
            raise LocalLlamaError("manager_unavailable", "Connect to the owning Hermes service to manage local llama", code=-32000)
        if binding["manager"] is None:
            if not create:
                raise LocalLlamaError("model_not_ready", "Turn on llama and load the selected model", code=4090)
            identity = ensure_install_identity(binding["root"])
            if not identity.install_id:
                raise LocalLlamaError("manager_unavailable", "Hermes installation identity is unavailable", code=-32000)
            binding["manager"] = LocalLlamaManager(binding["root"], binding["config"], identity.install_id)
        return binding["manager"]


def shutdown(*, root=None):
    with _lock:
        if root is None:
            bindings = list(_bindings.values())
            _bindings.clear()
        else:
            binding = _bindings.pop(str(Path(root).resolve()), None)
            bindings = [binding] if binding else []
    for binding in bindings:
        if binding["manager"] is not None:
            binding["manager"].close()
