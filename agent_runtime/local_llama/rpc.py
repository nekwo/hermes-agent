"""Thin typed handlers on the existing authorization/JSON-RPC dispatcher."""
from __future__ import annotations

from agent_runtime.call_authorization import TIER_CONSOLE, TIER_READ
from .config import LocalLlamaError
from .service import get_manager


METHODS = {"status": TIER_READ, "config.get": TIER_CONSOLE, "config.set": TIER_CONSOLE,
           "catalog.scan": TIER_CONSOLE, "start": TIER_CONSOLE, "stop": TIER_CONSOLE,
           "load": TIER_CONSOLE, "unload": TIER_CONSOLE, "logs.get": TIER_CONSOLE}


def register(method, ok, err):
    for suffix, tier in METHODS.items():
        def handler(rid, params, context, operation=suffix):
            try:
                manager = get_manager()
                if operation == "status":
                    result = manager.status(operation_id=params.get("operation_id"), request_id=params.get("request_id"))
                elif operation == "config.get":
                    result = manager.config_get()
                elif operation == "logs.get":
                    result = manager.logs_get(cursor=params.get("cursor"), limit=params.get("limit", 100))
                else:
                    result = manager.submit(operation, params)
                return ok(rid, result)
            except LocalLlamaError as exc:
                return err(rid, exc.code, str(exc), {"reason": exc.reason, **exc.details})
        method("runtime.local_llama." + suffix, tier=tier)(handler)
