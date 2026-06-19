from types import SimpleNamespace

from agent.codex_runtime import _consume_codex_event_stream, run_codex_stream


def test_consume_codex_event_stream_records_stats():
    stats = {}
    events = [
        {"type": "response.output_text.delta", "delta": "hello"},
        {"type": "response.reasoning.delta", "delta": "thinking"},
        {"type": "response.function_call_arguments.delta", "delta": "{}"},
        {"type": "response.output_item.done", "item": SimpleNamespace(type="function_call")},
        {"type": "response.completed", "response": {"id": "resp_1", "status": "completed", "usage": {"total_tokens": 5}}},
    ]

    result = _consume_codex_event_stream(events, model="gpt-test", stream_stats=stats)

    assert result.status == "completed"
    assert result._stream_stats["event_count"] == 5
    assert result._stream_stats["text_delta_count"] == 1
    assert result._stream_stats["reasoning_delta_count"] == 1
    assert result._stream_stats["tool_call_event_count"] == 1
    assert result._stream_stats["output_item_count"] == 1
    assert result._stream_stats["saw_terminal_count"] == 1
    assert result._stream_stats["terminal_event_type"] == "response.completed"
    assert result._stream_stats["first_event_ms"] >= 0
    assert result._stream_stats["terminal_event_ms"] >= 0
    assert result._stream_stats["consume_ms"] >= 0


def test_run_codex_stream_emits_provider_timing_events():
    events = []

    class FakeResponses:
        def create(self, **kwargs):
            assert kwargs["stream"] is True
            return iter(
                [
                    {"type": "response.output_text.delta", "delta": "ok"},
                    {
                        "type": "response.completed",
                        "response": {"id": "resp_1", "status": "completed", "usage": {"total_tokens": 3}},
                    },
                ]
            )

    class FakeAgent:
        provider = "openai-codex"
        model = "gpt-test"
        api_mode = "codex_responses"
        _interrupt_requested = False
        _codex_streamed_text_parts = []

        def __init__(self):
            self.status_callback = events.append

        def _ensure_primary_openai_client(self, reason):
            return SimpleNamespace(responses=FakeResponses())

        def _fire_stream_delta(self, text):
            self._codex_streamed_text_parts.append(text)

        def _fire_reasoning_delta(self, text):
            pass

        def _touch_activity(self, message):
            pass

    result = run_codex_stream(FakeAgent(), {"model": "gpt-test"})

    assert result.output_text == "ok"
    by_step = {event["step"]: event for event in events}
    assert "provider_client_resolve" in by_step
    assert "provider_responses_create" in by_step
    assert "provider_stream_first_event" in by_step
    consume = by_step["provider_stream_consume"]
    assert consume["timing_values"]["provider_stream_event_count"] == 2
    assert consume["timing_values"]["provider_stream_text_delta_count"] == 1
    assert consume["timing_values"]["provider_stream_saw_terminal_count"] == 1
    assert consume["terminal_event_type"] == "response.completed"
