"""Raw HTTPS + SSE execution for the certified chat-completions lane."""

from __future__ import annotations

import json
import socket
import ssl
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from threading import Event, Lock
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import httpx

from ._vendor.agent.transports import get_transport
from .events import EventEmitter
from .exceptions import InvalidRequest, MalformedSSE
from .providers import ProviderDescriptor
from .redact import redact_text


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    message: str
    retryable: bool
    status_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProviderHTTPError(Exception):
    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"provider returned HTTP {status_code}: {body}")


class ProviderStreamError(Exception):
    pass


class TurnCancellation:
    """Thread-safe cancellation token which also closes an active response."""

    def __init__(self) -> None:
        self.event = Event()
        self._response: httpx.Response | None = None
        self._lock = Lock()

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    def attach(self, response: httpx.Response) -> None:
        with self._lock:
            self._response = response
            cancelled = self.event.is_set()
        if cancelled:
            response.close()

    def detach(self, response: httpx.Response) -> None:
        with self._lock:
            if self._response is response:
                self._response = None

    def cancel(self) -> None:
        self.event.set()
        with self._lock:
            response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:
                # Cancellation is best-effort transport teardown; the event is
                # authoritative and the runner will drop all subsequent data.
                pass


def _causes(exc: BaseException) -> Iterable[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def classify_error(exc: BaseException, *, secrets: tuple[str, ...] = ()) -> ErrorInfo:
    """Map network/provider/SSE failures onto the stable mobile error taxonomy."""
    safe_message = redact_text(str(exc), secrets=secrets) or exc.__class__.__name__
    if isinstance(exc, MalformedSSE):
        return ErrorInfo("malformed_sse", safe_message, False)
    if isinstance(exc, ProviderStreamError):
        return ErrorInfo("provider_stream_error", safe_message, False)
    if isinstance(exc, ProviderHTTPError):
        status = exc.status_code
        if status in (401, 403):
            return ErrorInfo("authentication", safe_message, False, status)
        if status == 429:
            return ErrorInfo("rate_limited", safe_message, True, status)
        if 500 <= status <= 599:
            return ErrorInfo("provider_5xx", safe_message, True, status)
        return ErrorInfo("provider_http_error", safe_message, False, status)
    if isinstance(exc, httpx.TimeoutException):
        return ErrorInfo("timeout", safe_message, True)

    chain = tuple(_causes(exc))
    lowered = " ".join(str(item).lower() for item in chain)
    if any(isinstance(item, socket.gaierror) for item in chain) or any(
        marker in lowered
        for marker in ("name resolution", "nodename nor servname", "getaddrinfo", "dns")
    ):
        return ErrorInfo("dns", safe_message, True)
    if any(isinstance(item, ssl.SSLError) for item in chain) or any(
        marker in lowered for marker in ("ssl", "tls", "certificate verify")
    ):
        return ErrorInfo("tls", safe_message, False)
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)) or any(
        isinstance(item, OSError) for item in chain
    ):
        return ErrorInfo("offline", safe_message, True)
    if isinstance(exc, InvalidRequest):
        return ErrorInfo("invalid_request", safe_message, False)
    return ErrorInfo("internal", safe_message, False)


def _namespace(value: Any) -> Any:
    """Adapt parsed JSON to the attribute interface used by upstream normalize_response."""
    if isinstance(value, Mapping):
        return SimpleNamespace(**{str(key): _namespace(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_namespace(item) for item in value]
    return value


def normalize_parsed_response(transport: Any, response: Mapping[str, Any]):
    """Normalize parsed JSON through an upstream or vendored transport."""
    return transport.normalize_response(_namespace(response))


def normalized_payload(response: Any) -> dict[str, Any]:
    usage = asdict(response.usage) if response.usage is not None else None
    tool_calls = None
    if response.tool_calls:
        tool_calls = [
            {
                "id": item.id,
                "type": "function",
                "function": {"name": item.name, "arguments": item.arguments},
                "extensions": item.provider_data or {},
            }
            for item in response.tool_calls
        ]
    return {
        "content": response.content,
        "reasoning": response.reasoning,
        "finish_reason": response.finish_reason,
        "usage": usage,
        "tool_calls": tool_calls,
        "extensions": response.provider_data or {},
    }


def _endpoint(base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    parts = urlsplit(normalized)
    if parts.scheme.lower() != "https" or not parts.netloc:
        raise InvalidRequest("base_url must be an absolute HTTPS URL")
    if parts.path.endswith("/chat/completions"):
        return normalized
    return normalized + "/chat/completions"


def _usage_payload(raw: Mapping[str, Any]) -> dict[str, int]:
    prompt = int(raw.get("prompt_tokens") or 0)
    completion = int(raw.get("completion_tokens") or 0)
    total = int(raw.get("total_tokens") or prompt + completion)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cached_tokens": int(raw.get("cached_tokens") or 0),
    }


class _Assembly:
    def __init__(self, model: str) -> None:
        self.model = model
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.finish_reason = "stop"
        self.usage: dict[str, int] | None = None
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.response_id: str | None = None

    def add_chunk(self, chunk: Mapping[str, Any], emitter: EventEmitter, cancelled: Callable[[], bool]) -> bool:
        if chunk.get("error"):
            raise ProviderStreamError(str(chunk["error"]))
        self.response_id = str(chunk.get("id") or self.response_id or "") or None
        if chunk.get("model"):
            self.model = str(chunk["model"])
        usage = chunk.get("usage")
        if isinstance(usage, Mapping):
            self.usage = _usage_payload(usage)
            if not cancelled():
                emitter.send("usage.updated", self.usage)

        choices = chunk.get("choices")
        if choices is None:
            return False
        if not isinstance(choices, list):
            raise MalformedSSE("SSE chunk choices must be a list")
        emitted_delta = False
        for choice in choices:
            if not isinstance(choice, Mapping):
                raise MalformedSSE("SSE choice must be an object")
            if choice.get("finish_reason") is not None:
                self.finish_reason = str(choice["finish_reason"])
            delta = choice.get("delta") or {}
            if not isinstance(delta, Mapping):
                raise MalformedSSE("SSE choice delta must be an object")
            content = delta.get("content")
            if isinstance(content, str) and content:
                self.content.append(content)
                emitted_delta = True
                if not cancelled():
                    emitter.send("content.delta", {"text": content})
            reasoning = delta.get("reasoning")
            if reasoning is None:
                reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                self.reasoning.append(reasoning)
                emitted_delta = True
                if not cancelled():
                    emitter.send("reasoning.delta", {"text": reasoning})
            raw_tool_calls = delta.get("tool_calls")
            if isinstance(raw_tool_calls, list):
                self._add_tool_calls(raw_tool_calls)
                emitted_delta = True
        return emitted_delta

    def _add_tool_calls(self, raw_calls: list[Any]) -> None:
        for fallback_index, raw in enumerate(raw_calls):
            if not isinstance(raw, Mapping):
                continue
            index = int(raw.get("index", fallback_index))
            target = self.tool_calls.setdefault(
                index,
                {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
            )
            if raw.get("id"):
                target["id"] = raw["id"]
            function = raw.get("function")
            if isinstance(function, Mapping):
                if function.get("name"):
                    target["function"]["name"] += str(function["name"])
                if function.get("arguments"):
                    target["function"]["arguments"] += str(function["arguments"])
            if raw.get("extra_content") is not None:
                target["extra_content"] = raw["extra_content"]

    def completed_response(self) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(self.content) or None,
            "reasoning": "".join(self.reasoning) or None,
            "reasoning_content": "".join(self.reasoning) or None,
            "tool_calls": [self.tool_calls[key] for key in sorted(self.tool_calls)] or None,
            "refusal": None,
        }
        return {
            "id": self.response_id,
            "model": self.model,
            "choices": [{"index": 0, "message": message, "finish_reason": self.finish_reason}],
            "usage": self.usage,
        }


def _iter_sse(response: httpx.Response) -> Iterable[str]:
    data_lines: list[str] = []
    for line in response.iter_lines():
        if line == "":
            if data_lines:
                yield "\n".join(data_lines)
                data_lines.clear()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            data_lines.append(value)
        elif line.startswith(("event:", "id:", "retry:")):
            continue
        else:
            raise MalformedSSE(f"invalid SSE field: {line[:40]}")
    if data_lines:
        yield "\n".join(data_lines)


class TurnRunner:
    def __init__(
        self,
        *,
        client_factory: Callable[..., httpx.Client] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client_factory = client_factory or httpx.Client
        self._sleep = sleep

    def run(
        self,
        *,
        request: Mapping[str, Any],
        api_key: str,
        provider: ProviderDescriptor,
        emit: Callable[[dict[str, Any]], None],
        cancellation: TurnCancellation | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        cancellation = cancellation or TurnCancellation()
        config = dict(config or {})
        request_id = str(request["request_id"])
        model = str(request["model"])
        emitter = EventEmitter(
            request_id=request_id,
            provider=provider.id,
            model=model,
            emit=emit,
        )
        emitter.send("turn.started", {})
        if cancellation.cancelled:
            emitter.send("turn.cancelled", {})
            return

        emitted_delta = False
        attempts = max(1, int(config.get("max_retries", 2)) + 1)
        for attempt in range(attempts):
            try:
                assembly, attempt_delta = self._attempt(
                    request=request,
                    api_key=api_key,
                    provider=provider,
                    emitter=emitter,
                    cancellation=cancellation,
                    config=config,
                )
                emitted_delta = emitted_delta or attempt_delta
                if cancellation.cancelled:
                    emitter.send("turn.cancelled", {})
                    return
                transport = get_transport("chat_completions")
                if transport is None:
                    raise RuntimeError("vendored chat_completions transport is unavailable")
                normalized = normalize_parsed_response(transport, assembly.completed_response())
                emitter.send("turn.completed", normalized_payload(normalized))
                return
            except Exception as exc:
                if cancellation.cancelled:
                    emitter.send("turn.cancelled", {})
                    return
                emitted_delta = emitted_delta or emitter.next_seq > 1
                info = classify_error(exc, secrets=(api_key,))
                can_retry = info.retryable and not emitted_delta and attempt + 1 < attempts
                if can_retry:
                    backoff = max(0.0, float(config.get("retry_backoff_seconds", 0.25)))
                    self._sleep(backoff * (2**attempt))
                    continue
                emitter.send("turn.failed", {"error": info.to_dict()})
                return

    def _attempt(
        self,
        *,
        request: Mapping[str, Any],
        api_key: str,
        provider: ProviderDescriptor,
        emitter: EventEmitter,
        cancellation: TurnCancellation,
        config: Mapping[str, Any],
    ) -> tuple[_Assembly, bool]:
        options = request.get("options") or {}
        transport = get_transport("chat_completions")
        if transport is None:
            raise RuntimeError("vendored chat_completions transport is unavailable")

        max_output = options.get("max_output_tokens")
        request_overrides: dict[str, Any] = {
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if options.get("temperature") is not None:
            request_overrides["temperature"] = options["temperature"]
        built = transport.build_kwargs(
            model=str(request["model"]),
            messages=list(request["messages"]),
            tools=None,
            max_tokens=max_output,
            max_tokens_param_fn=lambda value: {"max_tokens": value},
            reasoning_config=options.get("reasoning"),
            is_openrouter=provider.id == "openrouter",
            provider_name=provider.id,
            base_url=request.get("base_url") or provider.default_base_url,
            request_overrides=request_overrides,
        )
        built.pop("timeout", None)
        extra_body = built.pop("extra_body", None)
        if isinstance(extra_body, Mapping):
            built.update(extra_body)
        extra_headers = built.pop("extra_headers", None)

        base_url = str(request.get("base_url") or provider.default_base_url)
        url = _endpoint(base_url)
        auth_value = f"{provider.auth_scheme} {api_key}".strip()
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            provider.auth_header: auth_value,
        }
        if isinstance(extra_headers, Mapping):
            headers.update({str(key): str(value) for key, value in extra_headers.items()})

        timeout = httpx.Timeout(
            connect=float(config.get("connect_timeout_seconds", 10.0)),
            read=float(config.get("read_timeout_seconds", 60.0)),
            write=float(config.get("write_timeout_seconds", 30.0)),
            pool=float(config.get("pool_timeout_seconds", 10.0)),
        )
        emitted_delta = False
        assembly = _Assembly(str(request["model"]))
        with self._client_factory(timeout=timeout, verify=True) as client:
            with client.stream("POST", url, headers=headers, json=built) as response:
                cancellation.attach(response)
                try:
                    if response.status_code < 200 or response.status_code >= 300:
                        body = response.read()[:16384].decode("utf-8", errors="replace")
                        raise ProviderHTTPError(response.status_code, body)
                    content_type = response.headers.get("content-type", "").lower()
                    if content_type and "text/event-stream" not in content_type:
                        raise MalformedSSE(f"expected text/event-stream, got {content_type}")
                    saw_done = False
                    for data in _iter_sse(response):
                        if cancellation.cancelled:
                            break
                        if data.strip() == "[DONE]":
                            saw_done = True
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError as exc:
                            raise MalformedSSE(f"invalid SSE JSON at column {exc.colno}") from exc
                        if not isinstance(chunk, Mapping):
                            raise MalformedSSE("SSE data must decode to an object")
                        emitted_delta = assembly.add_chunk(
                            chunk, emitter, lambda: cancellation.cancelled
                        ) or emitted_delta
                    if not cancellation.cancelled and not saw_done:
                        raise MalformedSSE("provider stream ended before [DONE]")
                finally:
                    cancellation.detach(response)
        return assembly, emitted_delta
