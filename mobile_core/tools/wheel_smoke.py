"""Post-build smoke run using only the installed wheel dependency set."""

from __future__ import annotations

import httpx

from hermes_mobile_core import HermesMobileCore
from hermes_mobile_core.turn_runner import TurnRunner


def main() -> None:
    sse = (
        'data: {"id":"wheel-smoke","model":"fixture/model","choices":'
        '[{"index":0,"delta":{"content":"wheel ok"},"finish_reason":"stop"}]}\n\n'
        'data: {"id":"wheel-smoke","model":"fixture/model","choices":[],"usage":'
        '{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse.encode(),
        )

    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        kwargs.pop("verify", None)
        return httpx.Client(transport=transport, **kwargs)

    core = HermesMobileCore(runner=TurnRunner(client_factory=factory))
    events = []
    core.start_turn(
        {
            "schema_version": 1,
            "request_id": "wheel-smoke-request",
            "provider": "openai",
            "model": "fixture/model",
            "messages": [{"role": "user", "content": "smoke"}],
            "options": {},
        },
        "fixture-secret",
        events.append,
    )
    assert events[-1]["kind"] == "turn.completed", events
    assert events[-1]["payload"]["content"] == "wheel ok"
    assert events[-1]["payload"]["usage"]["total_tokens"] == 3
    print("wheel-smoke: PASS")


if __name__ == "__main__":
    main()
