"""Tests for surrogate character sanitization in user input.

Surrogates (U+D800..U+DFFF) are invalid in UTF-8 and crash json.dumps()
inside the OpenAI SDK. They can appear via clipboard paste from rich-text
editors like Google Docs, OR from byte-level reasoning models (xiaomi/mimo,
kimi, glm) emitting lone halves in reasoning output.
"""
import json
import socket

import pytest
from unittest.mock import MagicMock, patch

from run_agent import (
    _sanitize_surrogates,
    _sanitize_messages_surrogates,
    _sanitize_structure_surrogates,
)

# The endpoint the agent-construction test points at. Named once so the fake
# below and the assertion that convicts it cannot drift apart.
MODEL_BASE_URL = "http://localhost:1234/v1"
MODEL_ENDPOINT_PORT = 1234


class _OfflineModelEndpoint:
    """The model endpoint, faked one layer above the socket.

    Constructing a real ``AIAgent`` resolves the model's context length by
    probing the endpoint — an Ollama ``/api/show`` POST and, when that fails,
    a probe-down ladder. Against a ``base_url`` nothing answers, each probe
    blocks until its own timeout: this file took 32.7s and died on the 30s
    per-test cap (ledger row F2). None of that is what these tests are about;
    they are about surrogate sanitization.

    So the fake sits at httpx's transport — the layer that owns "how do I
    reach the network" — and answers 404, which is what a server that is not
    Ollama says. The production code still builds its client, sends its
    request and reads the response; only the socket never happens. Mocking
    the probe functions themselves would have worked too, and would have been
    wrong: it would pin this test to today's call graph and stop telling us
    anything about whether construction reaches the network.

    ``connect_attempts`` is the witness, and it is deliberately a DIFFERENT
    layer from the fake: the socket boundary records anything dialled at the
    endpoint's port. If the transport fake ever stops covering a path, the
    probe reaches the socket and the count says so — which is how "this test
    does not touch the network" fails out loud instead of just getting slow
    again. Sockets to anywhere else (asyncio's self-pipe, for one) are passed
    straight through: a blanket ban breaks the interpreter's own plumbing.
    """

    def __init__(self):
        self.connect_attempts = []

    def install(self, monkeypatch):
        import httpx
        import requests
        from requests.adapters import HTTPAdapter

        def _no_server_here(request):
            return httpx.Response(404, json={"error": "offline in tests"})

        # Construction probes the endpoint through BOTH http stacks — httpx for
        # the Ollama/LM-Studio/llama.cpp/vLLM detection ladder, `requests` for
        # `fetch_endpoint_model_metadata`'s /models fetch. Faking one and not
        # the other is how the timeout survives at reduced volume, so each gets
        # its own transport: httpx's MockTransport, and requests' HTTPAdapter,
        # which IS requests' transport.
        def _offline_adapter_send(adapter_self, request, **kwargs):
            response = requests.Response()
            response.status_code = 404
            response.url = request.url
            response.request = request
            response._content = b'{"error": "offline in tests"}'
            return response

        monkeypatch.setattr(HTTPAdapter, "send", _offline_adapter_send)

        class _OfflineClient(httpx.Client):
            """A real ``httpx.Client`` whose transport answers locally.

            A subclass rather than a factory function on purpose: the OpenAI
            SDK subclasses ``httpx.Client`` for its own default client, and a
            function cannot be subclassed — swapping one in fails deep inside
            client construction with an error that names neither httpx nor
            this test.
            """

            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(_no_server_here)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(httpx, "Client", _OfflineClient)

        real_connect = socket.socket.connect
        real_create_connection = socket.create_connection

        def _is_the_endpoint(address):
            return (
                isinstance(address, tuple)
                and len(address) >= 2
                and address[1] == MODEL_ENDPOINT_PORT
            )

        def _refuse(address):
            self.connect_attempts.append(address)
            raise ConnectionRefusedError(
                f"the model endpoint is offline in this test (dialled {address!r})"
            )

        def _connect(sock, address, *args, **kwargs):
            if _is_the_endpoint(address):
                _refuse(address)
            return real_connect(sock, address, *args, **kwargs)

        def _create_connection(address, *args, **kwargs):
            if _is_the_endpoint(address):
                _refuse(address)
            return real_create_connection(address, *args, **kwargs)

        monkeypatch.setattr(socket.socket, "connect", _connect)
        monkeypatch.setattr(socket, "create_connection", _create_connection)


@pytest.fixture(autouse=True)
def offline_model_endpoint(monkeypatch):
    """Autouse so it cannot be forgotten; requestable so it can be asserted on.

    The pure sanitization tests never dial anything, so the fixture costs them
    nothing — but the next test that constructs an agent inherits the fence
    instead of re-discovering the 30s timeout.
    """
    endpoint = _OfflineModelEndpoint()
    endpoint.install(monkeypatch)
    return endpoint


class TestSanitizeSurrogates:
    """Test the _sanitize_surrogates() helper."""

    def test_normal_text_unchanged(self):
        text = "Hello, this is normal text with unicode: café ñ 日本語 🎉"
        assert _sanitize_surrogates(text) == text


    def test_single_surrogate_replaced(self):
        result = _sanitize_surrogates("Hello \udce2 world")
        assert result == "Hello \ufffd world"



    def test_result_is_json_serializable(self):
        """Sanitized text must survive json.dumps + utf-8 encoding."""
        dirty = "data \udce2\udcb0 from clipboard"
        clean = _sanitize_surrogates(dirty)
        serialized = json.dumps({"content": clean}, ensure_ascii=False)
        # Must not raise UnicodeEncodeError
        serialized.encode("utf-8")



class TestSanitizeMessagesSurrogates:
    """Test the _sanitize_messages_surrogates() helper for message lists."""

    def test_clean_messages_returns_false(self):
        msgs = [
            {"role": "user", "content": "all clean"},
            {"role": "assistant", "content": "me too"},
        ]
        assert _sanitize_messages_surrogates(msgs) is False

    def test_dirty_string_content_sanitized(self):
        msgs = [
            {"role": "user", "content": "text with \udce2 surrogate"},
        ]
        assert _sanitize_messages_surrogates(msgs) is True
        assert "\ufffd" in msgs[0]["content"]
        assert "\udce2" not in msgs[0]["content"]

    def test_dirty_multimodal_content_sanitized(self):
        msgs = [
            {"role": "user", "content": [
                {"type": "text", "text": "multimodal \udce2 content"},
                {"type": "image_url", "image_url": {"url": "http://example.com"}},
            ]},
        ]
        assert _sanitize_messages_surrogates(msgs) is True
        assert "\ufffd" in msgs[0]["content"][0]["text"]
        assert "\udce2" not in msgs[0]["content"][0]["text"]



    def test_tool_messages_sanitized(self):
        """Tool results could also contain surrogates from file reads etc."""
        msgs = [
            {"role": "tool", "content": "result with \udce2 data", "tool_call_id": "x"},
        ]
        assert _sanitize_messages_surrogates(msgs) is True
        assert "\ufffd" in msgs[0]["content"]


class TestReasoningFieldSurrogates:
    """Surrogates in reasoning fields (byte-level reasoning models).

    xiaomi/mimo, kimi, glm and similar byte-level tokenizers can emit lone
    surrogates in reasoning output. These fields are carried through to the
    API as `reasoning_content` on assistant messages, and must be sanitized
    or json.dumps() crashes with 'utf-8' codec can't encode surrogates.
    """

    def test_reasoning_field_sanitized(self):
        msgs = [
            {"role": "assistant", "content": "ok", "reasoning": "thought \udce2 here"},
        ]
        assert _sanitize_messages_surrogates(msgs) is True
        assert "\udce2" not in msgs[0]["reasoning"]
        assert "\ufffd" in msgs[0]["reasoning"]


    def test_reasoning_details_nested_sanitized(self):
        """reasoning_details is a list of dicts with nested string fields."""
        msgs = [
            {
                "role": "assistant",
                "content": "ok",
                "reasoning_details": [
                    {"type": "reasoning.summary", "summary": "summary \udce2 text"},
                    {"type": "reasoning.text", "text": "chain \udc00 of thought"},
                ],
            },
        ]
        assert _sanitize_messages_surrogates(msgs) is True
        assert "\udce2" not in msgs[0]["reasoning_details"][0]["summary"]
        assert "\ufffd" in msgs[0]["reasoning_details"][0]["summary"]
        assert "\udc00" not in msgs[0]["reasoning_details"][1]["text"]
        assert "\ufffd" in msgs[0]["reasoning_details"][1]["text"]


    def test_reasoning_end_to_end_json_serialization(self):
        """After sanitization, the full message dict must serialize clean."""
        msgs = [
            {
                "role": "assistant",
                "content": "answer",
                "reasoning_content": "reasoning with \udce2 surrogate",
                "reasoning_details": [
                    {"summary": "nested \udcb0 surrogate"},
                ],
            },
        ]
        _sanitize_messages_surrogates(msgs)
        # Must round-trip through json + utf-8 encoding without error
        payload = json.dumps(msgs, ensure_ascii=False).encode("utf-8")
        assert b"\\" not in payload[:0]  # sanity — just ensure we got bytes
        assert len(payload) > 0



class TestSanitizeStructureSurrogates:
    """Test the _sanitize_structure_surrogates() helper for nested payloads."""


    def test_flat_dict(self):
        payload = {"a": "clean", "b": "dirty \udce2 text"}
        assert _sanitize_structure_surrogates(payload) is True
        assert payload["a"] == "clean"
        assert "\ufffd" in payload["b"]







class TestApiMessagesSurrogateRecovery:
    """Integration: verify the recovery block sanitizes api_messages.

    The bug this guards against: a surrogate in `reasoning_content` on
    api_messages (transformed from `reasoning` during build) crashes the
    OpenAI SDK's json.dumps(), and the recovery block previously only
    sanitized the canonical `messages` list — not `api_messages` — so the
    next retry would send the same broken payload and fail 3 times.
    """

    def test_api_messages_reasoning_content_sanitized(self):
        """The extended sanitizer catches reasoning_content in api_messages."""
        api_messages = [
            {"role": "system", "content": "sys"},
            {
                "role": "assistant",
                "content": "response",
                "reasoning_content": "thought \udce2 trail",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {"name": "tool", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "content": "result", "tool_call_id": "call_1"},
        ]
        assert _sanitize_messages_surrogates(api_messages) is True
        assert "\udce2" not in api_messages[1]["reasoning_content"]
        # Full payload must now serialize clean
        json.dumps(api_messages, ensure_ascii=False).encode("utf-8")


class TestRunConversationSurrogateSanitization:
    """Integration: verify run_conversation sanitizes user_message."""

    @patch("run_agent.AIAgent._build_system_prompt")
    @patch("run_agent.AIAgent._interruptible_streaming_api_call")
    @patch("run_agent.AIAgent._interruptible_api_call")
    def test_user_message_surrogates_sanitized(
        self, mock_api, mock_stream, mock_sys, offline_model_endpoint
    ):
        """Surrogates in user_message are stripped before API call."""
        from run_agent import AIAgent

        mock_sys.return_value = "system prompt"

        # Mock streaming to return a simple response
        mock_choice = MagicMock()
        mock_choice.message.content = "response"
        mock_choice.message.tool_calls = None
        mock_choice.message.refusal = None
        mock_choice.finish_reason = "stop"
        # Every reasoning field the extractor looks at has to be spelled out:
        # an unset attribute on a MagicMock is not absent, it is a truthy
        # MagicMock, and the extractor joins those into a string. Left unset,
        # the turn errors and the loop retries to its iteration ceiling — which
        # the 30s network timeout used to hide, because this test never got
        # this far.
        mock_choice.message.reasoning_content = None
        mock_choice.message.reasoning = None
        mock_choice.message.reasoning_details = None

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        mock_response.model = "test-model"
        mock_response.id = "test-id"

        mock_stream.return_value = mock_response
        mock_api.return_value = mock_response

        agent = AIAgent(model="test/model", api_key="test-key", base_url=MODEL_BASE_URL, quiet_mode=True, skip_memory=True, skip_context_files=True)
        agent.client = MagicMock()

        # Pass a message with surrogates
        result = agent.run_conversation(
            user_message="test \udce2 message",
            conversation_history=[],
        )

        # The message stored in history should have surrogates replaced
        checked = 0
        for msg in result.get("messages", []):
            if msg.get("role") == "user":
                checked += 1
                assert "\udce2" not in msg["content"], "Surrogate leaked into stored message"
                assert "\ufffd" in msg["content"], "Replacement char not in stored message"

        # The sanitization claim above is only worth anything if a user message
        # was actually stored to check \u2014 an empty loop asserts nothing.
        assert checked == 1, f"expected one stored user message, saw {checked}"

        # ...and none of it went near the network. Zero, not "few": construction
        # dialling the endpoint is exactly what timed this file out.
        assert offline_model_endpoint.connect_attempts == []
