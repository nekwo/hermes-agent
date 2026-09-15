from __future__ import annotations

import json
import socket
import ssl
from pathlib import Path

import httpx
import pytest

from hermes_mobile_core.core import HermesMobileCore
from hermes_mobile_core.exceptions import InvalidRequest, MalformedSSE
from hermes_mobile_core.providers import PROVIDERS
from hermes_mobile_core.turn_runner import (
    ProviderHTTPError,
    TurnCancellation,
    TurnRunner,
    classify_error,
)


def _request(**overrides):
    value = {
        "schema_version": 1,
        "request_id": "request-fixture-1",
        "provider": "openrouter",
        "model": "example/model",
        "base_url": "https://provider.example/v1",
        "messages": [{"role": "user", "content": "Hello"}],
        "options": {"temperature": 0.7, "max_output_tokens": 128},
    }
    value.update(overrides)
    return value


def _stream_fixture(name: str):
    data = json.loads(
        (Path(__file__).parent / "golden" / "streams.json").read_text(encoding="utf-8")
    )
    value = data[name]
    return "".join(value) if isinstance(value, list) else value


def _runner(handler, *, sleep=lambda _: None):
    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        kwargs.pop("verify", None)
        return httpx.Client(transport=transport, **kwargs)

    return TurnRunner(client_factory=factory, sleep=sleep)


def _run(runner: TurnRunner, request=None, cancellation=None, config=None):
    events = []
    runner.run(
        request=request or _request(),
        api_key="fixture-secret-never-emitted",
        provider=PROVIDERS["openrouter"],
        emit=events.append,
        cancellation=cancellation,
        config=config or {"max_retries": 0},
    )
    return events


def test_full_mocked_turn_streams_through_mobile_dependencies_only() -> None:
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["body"] = json.loads(request.content)
        observed["authorization"] = request.headers["authorization"]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_stream_fixture("successful").encode(),
        )

    events = _run(_runner(handler))
    assert [event["kind"] for event in events] == [
        "turn.started",
        "reasoning.delta",
        "content.delta",
        "reasoning.delta",
        "content.delta",
        "usage.updated",
        "turn.completed",
    ]
    assert [event["seq"] for event in events] == list(range(len(events)))
    assert all(event["schema_version"] == 1 for event in events)
    assert all(event["request_id"] == "request-fixture-1" for event in events)
    completed = events[-1]["payload"]
    assert completed["content"] == "Hello world"
    assert completed["reasoning"] == "Think carefully."
    assert completed["finish_reason"] == "stop"
    assert completed["usage"]["total_tokens"] == 9
    assert observed["url"] == "https://provider.example/v1/chat/completions"
    assert observed["body"]["stream"] is True
    assert observed["body"]["stream_options"] == {"include_usage": True}
    assert observed["body"]["temperature"] == 0.7
    assert observed["body"]["max_tokens"] == 128
    assert observed["authorization"] == "Bearer fixture-secret-never-emitted"
    assert "fixture-secret-never-emitted" not in json.dumps(events)


def test_cancellation_emits_one_terminal_and_drops_late_deltas() -> None:
    cancellation = TurnCancellation()
    events = []

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_stream_fixture("successful").encode(),
        )

    def emit(event):
        events.append(event)
        if event["kind"] == "content.delta":
            cancellation.cancel()

    _runner(handler).run(
        request=_request(),
        api_key="fixture-secret",
        provider=PROVIDERS["openrouter"],
        emit=emit,
        cancellation=cancellation,
        config={"max_retries": 2, "retry_backoff_seconds": 0},
    )
    terminals = [event for event in events if event["kind"].startswith("turn.")][1:]
    assert [event["kind"] for event in terminals] == ["turn.cancelled"]
    cancelled_index = next(i for i, event in enumerate(events) if event["kind"] == "turn.cancelled")
    assert not any(event["kind"].endswith(".delta") for event in events[cancelled_index + 1 :])


def test_retryable_5xx_retries_before_any_delta() -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"error": {"message": "fixture unavailable"}})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_stream_fixture("successful").encode(),
        )

    events = _run(
        _runner(handler),
        config={"max_retries": 1, "retry_backoff_seconds": 0},
    )
    assert attempts == 2
    assert events[-1]["kind"] == "turn.completed"
    assert len([event for event in events if event["kind"] == "turn.started"]) == 1


def test_network_failure_after_a_delta_is_not_retried() -> None:
    attempts = 0

    class BrokenAfterFirstDelta(httpx.SyncByteStream):
        def __iter__(self):
            yield (
                b'data: {"choices":[{"index":0,"delta":{"content":"once"},'
                b'"finish_reason":null}]}\n\n'
            )
            raise httpx.ReadError("stream disconnected")

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=BrokenAfterFirstDelta(),
        )

    events = _run(
        _runner(handler),
        config={"max_retries": 3, "retry_backoff_seconds": 0},
    )
    assert attempts == 1
    assert [event["payload"]["text"] for event in events if event["kind"] == "content.delta"] == ["once"]
    assert events[-1]["kind"] == "turn.failed"


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [(401, "authentication", False), (403, "authentication", False), (429, "rate_limited", True), (500, "provider_5xx", True)],
)
def test_http_error_classes(status: int, code: str, retryable: bool) -> None:
    info = classify_error(ProviderHTTPError(status, "fixture error"))
    assert (info.code, info.retryable, info.status_code) == (code, retryable, status)


@pytest.mark.parametrize(
    ("exc", "code", "retryable"),
    [
        (httpx.ReadTimeout("slow"), "timeout", True),
        (socket.gaierror("name resolution failed"), "dns", True),
        (ssl.SSLError("certificate verify failed"), "tls", False),
        (httpx.ConnectError("network unreachable"), "offline", True),
        (MalformedSSE("bad event"), "malformed_sse", False),
    ],
)
def test_network_and_stream_error_classes(exc: Exception, code: str, retryable: bool) -> None:
    info = classify_error(exc)
    assert (info.code, info.retryable) == (code, retryable)


def test_malformed_sse_fails_once_without_retry() -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_stream_fixture("malformed").encode(),
        )

    events = _run(_runner(handler), config={"max_retries": 3, "retry_backoff_seconds": 0})
    assert attempts == 1
    assert events[-1]["kind"] == "turn.failed"
    assert events[-1]["payload"]["error"]["code"] == "malformed_sse"
    assert len([event for event in events if event["kind"] in {"turn.completed", "turn.cancelled", "turn.failed"}]) == 1


def test_provider_error_cannot_echo_api_key_into_events() -> None:
    secret = "fixture-secret-echoed-by-provider"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"invalid credential {secret}")

    events = []
    _runner(handler).run(
        request=_request(),
        api_key=secret,
        provider=PROVIDERS["openrouter"],
        emit=events.append,
        config={"max_retries": 0},
    )
    assert events[-1]["kind"] == "turn.failed"
    assert secret not in json.dumps(events)


def test_core_facade_rejects_serialized_keys_and_returns_json_safe_results() -> None:
    core = HermesMobileCore(runner=_runner(lambda _: httpx.Response(500)))
    configured = core.configure({"schema_version": 1, "max_retries": 1})
    json.dumps(configured)
    providers = core.list_supported_providers()
    json.dumps(providers)
    assert len(providers) >= 37
    assert {"openrouter", "openai-codex", "anthropic", "bedrock", "custom"} <= {
        item["id"] for item in providers
    }

    bad = _request(api_key="must-not-be-here")
    with pytest.raises(InvalidRequest, match="must not appear"):
        core.start_turn(bad, "separate-secret", lambda _: None)
    with pytest.raises(InvalidRequest, match="unknown request fields"):
        core.start_turn(_request(tools=[]), "separate-secret", lambda _: None)


def test_codex_responses_stream_uses_subscription_backend_and_normalizes_usage() -> None:
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["body"] = json.loads(request.content)
        observed["originator"] = request.headers.get("originator")
        content = (
            'data: {"type":"response.reasoning_summary_text.delta","delta":"Think"}\n\n'
            'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n'
            'data: {"type":"response.completed","response":{"id":"resp_1","usage":'
            '{"input_tokens":7,"output_tokens":2,"total_tokens":9,"input_tokens_details":{"cached_tokens":3}}}}\n\n'
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)

    events = []
    _runner(handler).run(
        request=_request(provider="openai-codex", model="gpt-5.4", base_url=None),
        api_key="fixture-oauth-token",
        provider=PROVIDERS["openai-codex"],
        emit=events.append,
        config={"max_retries": 0},
    )
    assert events[-1]["kind"] == "turn.completed"
    assert events[-1]["payload"]["content"] == "Hello"
    assert events[-1]["payload"]["usage"]["cached_tokens"] == 3
    assert observed["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert observed["originator"] == "codex_cli_rs"
    assert observed["body"]["store"] is False

def test_missing_api_key_is_a_typed_failed_turn() -> None:
    core = HermesMobileCore()
    events = []
    core.start_turn(_request(), "", events.append)
    assert [event["kind"] for event in events] == ["turn.started", "turn.failed"]
    assert events[-1]["payload"]["error"]["code"] == "invalid_request"
