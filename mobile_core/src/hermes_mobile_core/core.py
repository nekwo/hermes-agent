"""JSON-safe facade consumed by Android/iOS native hosts."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any

from .events import EventEmitter, SCHEMA_VERSION
from .exceptions import InvalidRequest
from .providers import get_provider, list_supported_providers
from .turn_runner import TurnCancellation, TurnRunner, classify_error

_CONFIG_DEFAULTS: dict[str, float | int] = {
    "connect_timeout_seconds": 10.0,
    "read_timeout_seconds": 60.0,
    "write_timeout_seconds": 30.0,
    "pool_timeout_seconds": 10.0,
    "max_retries": 2,
    "retry_backoff_seconds": 0.25,
}


def _contains_secret_field(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("_", "-")
            if normalized in {
                "api-key",
                "apikey",
                "authorization",
                "cookie",
                "access-token",
                "refresh-token",
            }:
                return True
            if _contains_secret_field(item):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_field(item) for item in value)
    return False


def _validate_request(request: Mapping[str, Any]) -> None:
    try:
        json.dumps(request, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise InvalidRequest("request must contain only JSON-safe values") from exc
    if request.get("schema_version") != SCHEMA_VERSION:
        raise InvalidRequest(f"schema_version must be {SCHEMA_VERSION}")
    if _contains_secret_field(request):
        raise InvalidRequest("API keys and authorization data must not appear in request")
    allowed = {"schema_version", "request_id", "provider", "model", "base_url", "messages", "options"}
    unknown = set(request) - allowed
    if unknown:
        raise InvalidRequest(f"unknown request fields: {', '.join(sorted(unknown))}")
    if not isinstance(request.get("request_id"), str) or not request["request_id"].strip():
        raise InvalidRequest("request_id must be a non-empty string")
    if not isinstance(request.get("provider"), str) or get_provider(request["provider"]) is None:
        raise InvalidRequest("provider is not supported")
    if not isinstance(request.get("model"), str) or not request["model"].strip():
        raise InvalidRequest("model must be a non-empty string")
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        raise InvalidRequest("messages must be a non-empty list")
    if not all(isinstance(item, Mapping) for item in messages):
        raise InvalidRequest("every message must be an object")
    options = request.get("options", {})
    if not isinstance(options, Mapping):
        raise InvalidRequest("options must be an object")
    unknown_options = set(options) - {"temperature", "max_output_tokens", "reasoning"}
    if unknown_options:
        raise InvalidRequest(f"unknown option fields: {', '.join(sorted(unknown_options))}")
    temperature = options.get("temperature")
    if temperature is not None and (
        not isinstance(temperature, (int, float))
        or isinstance(temperature, bool)
        or not 0 <= float(temperature) <= 2
    ):
        raise InvalidRequest("temperature must be a number from 0 through 2")
    max_output = options.get("max_output_tokens")
    if max_output is not None and (
        not isinstance(max_output, int) or isinstance(max_output, bool) or max_output <= 0
    ):
        raise InvalidRequest("max_output_tokens must be a positive integer")
    reasoning = options.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, Mapping):
        raise InvalidRequest("reasoning must be an object")
    descriptor = get_provider(str(request["provider"]))
    assert descriptor is not None
    if descriptor.requires_base_url and not str(request.get("base_url") or "").strip():
        raise InvalidRequest("base_url is required for compatible providers")


class HermesMobileCore:
    """Synchronous host facade; native callers run `start_turn` off the UI thread."""

    def __init__(self, *, runner: TurnRunner | None = None) -> None:
        self._runner = runner or TurnRunner()
        self._config = dict(_CONFIG_DEFAULTS)
        self._turns: dict[str, TurnCancellation] = {}
        self._lock = Lock()

    def configure(self, request: Mapping[str, Any]) -> dict[str, Any]:
        try:
            json.dumps(request, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise InvalidRequest("configuration must contain only JSON-safe values") from exc
        if _contains_secret_field(request):
            raise InvalidRequest("configuration must not contain credentials")
        unknown = set(request) - set(_CONFIG_DEFAULTS) - {"schema_version"}
        if unknown:
            raise InvalidRequest(f"unknown configuration fields: {', '.join(sorted(unknown))}")
        if "schema_version" in request and request["schema_version"] != SCHEMA_VERSION:
            raise InvalidRequest(f"schema_version must be {SCHEMA_VERSION}")

        updated = dict(self._config)
        for key in _CONFIG_DEFAULTS:
            if key not in request:
                continue
            value = request[key]
            if key == "max_retries":
                if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 5:
                    raise InvalidRequest("max_retries must be an integer from 0 through 5")
                updated[key] = value
            else:
                if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                    raise InvalidRequest(f"{key} must be a non-negative number")
                updated[key] = float(value)
        with self._lock:
            self._config = updated
        return {"schema_version": SCHEMA_VERSION, "configured": True, "config": dict(updated)}

    def start_turn(
        self,
        request: Mapping[str, Any],
        api_key: str,
        emit: Callable[[dict[str, Any]], None],
    ) -> None:
        _validate_request(request)
        if not callable(emit):
            raise InvalidRequest("emit must be callable")
        request_id = str(request["request_id"])
        provider = get_provider(str(request["provider"]))
        assert provider is not None
        if not isinstance(api_key, str) or not api_key.strip():
            emitter = EventEmitter(
                request_id=request_id,
                provider=provider.id,
                model=str(request["model"]),
                emit=emit,
            )
            emitter.send("turn.started", {})
            emitter.send(
                "turn.failed",
                {"error": classify_error(InvalidRequest("API key is required")).to_dict()},
            )
            return

        cancellation = TurnCancellation()
        with self._lock:
            if request_id in self._turns:
                raise InvalidRequest("request_id is already active")
            self._turns[request_id] = cancellation
            config = dict(self._config)
        try:
            self._runner.run(
                request=request,
                api_key=api_key,
                provider=provider,
                emit=emit,
                cancellation=cancellation,
                config=config,
            )
        finally:
            with self._lock:
                if self._turns.get(request_id) is cancellation:
                    self._turns.pop(request_id, None)

    def cancel_turn(self, request_id: str) -> dict[str, Any]:
        with self._lock:
            cancellation = self._turns.get(str(request_id))
        if cancellation is not None:
            cancellation.cancel()
        return {
            "schema_version": SCHEMA_VERSION,
            "request_id": str(request_id),
            "cancel_requested": cancellation is not None,
        }

    def list_supported_providers(self) -> list[dict[str, Any]]:
        return list_supported_providers()
