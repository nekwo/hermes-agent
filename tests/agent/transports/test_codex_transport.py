"""Tests for the ResponsesApiTransport (Codex)."""

import json
import pytest
from types import SimpleNamespace

from agent.chat_completion_helpers import build_api_kwargs
from agent.transports import get_transport
from agent.transports.types import NormalizedResponse


@pytest.fixture
def transport():
    import agent.transports.codex  # noqa: F401
    return get_transport("codex_responses")


class TestCodexTransportBasic:

    def test_api_mode(self, transport):
        assert transport.api_mode == "codex_responses"

    def test_registered_on_import(self, transport):
        assert transport is not None

    def test_convert_tools(self, transport):
        tools = [{
            "type": "function",
            "function": {
                "name": "terminal",
                "description": "Run a command",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
            }
        }]
        result = transport.convert_tools(tools)
        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["name"] == "terminal"


class TestCodexBuildKwargs:

    def test_build_api_kwargs_copies_final_cache_routing_to_agent(self, transport):
        agent = SimpleNamespace(
            tools=[],
            api_mode="codex_responses",
            provider="openai-codex",
            base_url="https://chatgpt.com/backend-api/codex/responses",
            _base_url_hostname="chatgpt.com",
            _base_url_lower="https://chatgpt.com/backend-api/codex/responses",
            model="gpt-5.4",
            reasoning_config=None,
            session_id=None,
            cache_scope_id="private-conversation-alpha",
            max_tokens=None,
            request_overrides=None,
            _get_transport=lambda: transport,
            _prepare_messages_for_non_vision_model=lambda messages: messages,
            _resolved_api_call_timeout=lambda: None,
            _github_models_reasoning_extra_body=lambda: None,
            _codex_reasoning_replay_enabled=True,
        )

        build_api_kwargs(agent, [{"role": "user", "content": "Hi"}])

        routing = agent._last_cache_routing_observability
        assert routing["backend"] == "openai_codex"
        assert routing["cache_scope_source"] == "cache_scope_id"
        assert routing["session_header_fingerprint"].startswith("sha256:")
        assert "private-conversation-alpha" not in json.dumps(routing)

    def test_basic_kwargs(self, transport):
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        kw = transport.build_kwargs(
            model="gpt-5.4",
            messages=messages,
            tools=[],
        )
        assert kw["model"] == "gpt-5.4"
        assert kw["instructions"] == "You are helpful."
        assert "input" in kw
        assert kw["store"] is False






    def test_cache_key_is_content_addressed_not_session_id(self, transport):
        """prompt_cache_key is content-addressed from the static prefix
        (instructions + tools), not the session_id. This keeps recurring cron
        jobs — whose session_id carries a per-fire timestamp — on a stable warm
        cache key. The key is a 'pck_' hash and must NOT equal session_id."""
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="gpt-5.4", messages=messages, tools=[],
            session_id="cron_job42_20260624_143000",
        )
        pck = kw.get("prompt_cache_key", "")
        assert pck.startswith("pck_")
        assert pck != "cron_job42_20260624_143000"

    def test_cache_key_stable_across_session_ids(self, transport):
        """Same static prefix + different session_id (e.g. two cron fires of the
        same job) must yield the same prompt_cache_key — the whole point of the
        fix: repeated fires reuse the warm prefix instead of going cold."""
        messages = [{"role": "user", "content": "Hi"}]
        kw1 = transport.build_kwargs(
            model="gpt-5.4", messages=messages, tools=[],
            session_id="cron_job42_20260624_143000",
        )
        kw2 = transport.build_kwargs(
            model="gpt-5.4", messages=messages, tools=[],
            session_id="cron_job42_20260624_143500",
        )
        assert kw1["prompt_cache_key"] == kw2["prompt_cache_key"]


    def test_github_responses_drops_message_item_id_end_to_end(self, transport):
        # #32716: Copilot binds codex_message_items ids to a backend
        # "connection" that doesn't survive credential rotation, a gateway
        # restart, or load-balancer churn — replaying a stale id gets HTTP
        # 401 "input item ID does not belong to this connection", even for
        # ids well under the #27038 64-char length cap. build_kwargs must
        # thread is_github_responses through to the input converter so the
        # id never reaches the request.
        messages = [
            {"role": "system", "content": "You are Hermes."},
            {
                "role": "assistant",
                "content": "pong",
                "codex_message_items": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [{"type": "output_text", "text": "pong"}],
                        "id": "msg_short_but_connection_scoped",
                        "phase": "final_answer",
                    }
                ],
            },
        ]
        kw = transport.build_kwargs(
            model="gpt-5.5", messages=messages, tools=[],
            is_github_responses=True,
        )
        message_item = next(item for item in kw["input"] if item.get("type") == "message")
        assert "id" not in message_item
        assert message_item["phase"] == "final_answer"
        assert message_item["status"] == "in_progress"
        assert message_item["content"] == [{"type": "output_text", "text": "pong"}]



    def test_non_github_responses_keeps_message_item_id_end_to_end(self, transport):
        messages = [
            {"role": "system", "content": "You are Hermes."},
            {
                "role": "assistant",
                "content": "pong",
                "codex_message_items": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "pong"}],
                        "id": "msg_short_id",
                    }
                ],
            },
        ]
        kw = transport.build_kwargs(
            model="gpt-5.5", messages=messages, tools=[],
            is_codex_backend=True,
        )
        message_item = next(item for item in kw["input"] if item.get("type") == "message")
        assert message_item["id"] == "msg_short_id"

    @pytest.mark.parametrize("model", [
        "gpt-5.5",
        "gpt-5.5-pro",
        "gpt-5.4",
        "gpt-5.2",
        "gpt-5.1-codex-max",
        "gpt-5.1",
        "gpt-5.1-codex",
        "gpt-5.1-codex-mini",
        "gpt-5.1-chat-latest",
        "gpt-5",
        "gpt-5-codex",
        "gpt-4.1",
        "openai.gpt-5.5-pro",
        "openai/gpt-5.1-codex-2026-01-01",
    ])
    def test_extended_cache_models_set_24h_prompt_cache_retention(self, transport, model):
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model=model, messages=messages, tools=[],
            session_id="test-session",
            base_url="https://bedrock-mantle.us-west-2.api.aws/v1",
        )
        assert kw["prompt_cache_retention"] == "24h"

    @pytest.mark.parametrize("model", ["gpt-5.6", "gpt-4o", "o3"])
    def test_prompt_cache_retention_omitted_for_other_model_families(self, transport, model):
        kw = transport.build_kwargs(
            model=model,
            messages=[{"role": "user", "content": "Hi"}],
            tools=[],
            session_id="test-session",
            base_url="https://bedrock-mantle.us-west-2.api.aws/v1",
        )
        assert "prompt_cache_retention" not in kw

    @pytest.mark.parametrize("base_url", [
        "https://api.openai.com/v1",
        "https://example.openai.azure.com/openai/v1",
        "https://api.x.ai/v1",
        "https://models.github.ai/inference",
        "https://api.githubcopilot.com",
        "https://chatgpt.com/backend-api/codex",
        "https://responses.example.com/v1",
        "https://bedrock-mantle.us-west-2.api.aws.example/v1",
        "https://example.com/bedrock-mantle.us-west-2.api.aws/v1",
    ])
    def test_prompt_cache_retention_omitted_for_non_mantle_endpoints(self, transport, base_url):
        kw = transport.build_kwargs(
            model="gpt-5.4",
            messages=[{"role": "user", "content": "Hi"}],
            tools=[],
            base_url=base_url,
        )
        assert "prompt_cache_retention" not in kw

    def test_xai_responses_sends_cache_key_via_extra_body(self, transport):
        """xAI's Responses API documents ``prompt_cache_key`` as the
        body-level cache-routing key (the ``x-grok-conv-id`` header is
        Chat-Completions-only). Passing it via ``extra_body`` is robust
        against openai SDK builds whose ``Responses.stream()`` kwarg
        signature ever drops the field — the body field still serializes
        and reaches xAI either way. The ``x-grok-conv-id`` header is kept
        as a belt-and-braces fallback so cache routing survives even
        when the body field would be stripped by an intermediate proxy.
        Ref: https://docs.x.ai/developers/advanced-api-usage/prompt-caching/maximizing-cache-hits
        """
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="grok-4.3", messages=messages, tools=[],
            session_id="conv-xai-1",
            is_xai_responses=True,
        )
        assert "prompt_cache_key" not in kw
        # Body-level prompt_cache_key is content-addressed (pck_ hash), not the
        # raw session_id, so recurring cron fires stay on a stable warm key.
        eb_pck = kw.get("extra_body", {}).get("prompt_cache_key", "")
        assert eb_pck.startswith("pck_")
        assert eb_pck != "conv-xai-1"
        # x-grok-conv-id stays the session/transcript id, not the cache key.
        assert kw.get("extra_headers", {}).get("x-grok-conv-id") == "conv-xai-1"

    def test_xai_responses_extra_body_preserves_caller_fields(self, transport):
        """When the caller already supplies ``extra_body`` (e.g. via
        request_overrides), the xAI cache-key injection must merge into
        the existing dict instead of overwriting it. Caller-supplied
        ``prompt_cache_key`` wins (setdefault semantics) so user overrides
        aren't silently clobbered by the transport."""
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="grok-4.3", messages=messages, tools=[],
            session_id="conv-xai-1",
            is_xai_responses=True,
            request_overrides={"extra_body": {"prompt_cache_key": "caller-override", "other_field": 42}},
        )
        eb = kw.get("extra_body", {})
        assert eb.get("prompt_cache_key") == "caller-override"
        assert eb.get("other_field") == 42





    @pytest.mark.parametrize("length", [64, 65])
    def test_codex_cache_scope_boundary(self, transport, length):
        session_id = "s" * length
        scope = transport.build_kwargs(
            model="gpt-5.4",
            messages=[{"role": "user", "content": "Hi"}],
            tools=[],
            session_id=session_id,
            is_codex_backend=True,
            request_overrides={"extra_headers": {"x-test": "1"}},
        )["extra_headers"]

        assert scope["x-test"] == "1"
        assert len(scope["session_id"]) <= 64
        assert scope["x-client-request-id"] == scope["session_id"]
        if length == 64:
            assert scope["session_id"] == session_id
        else:
            assert scope["session_id"].startswith("pck_")
            assert scope["session_id"] != session_id





    def test_cache_routing_observability_fingerprints_final_request(self, transport):
        """Same static prefix stays comparable while conversation scope moves.

        The evidence is captured from final kwargs and must never retain the raw
        persona-chat scope/header values it is meant to diagnose.
        """
        messages = [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "Hi"},
        ]

        transport.build_kwargs(
            model="gpt-5.4",
            messages=messages,
            tools=[],
            cache_scope_id="private-conversation-alpha",
            is_codex_backend=True,
        )
        first = dict(transport._last_cache_routing_observability)
        transport.build_kwargs(
            model="gpt-5.4",
            messages=messages,
            tools=[],
            cache_scope_id="private-conversation-beta",
            is_codex_backend=True,
        )
        second = dict(transport._last_cache_routing_observability)

        assert first["prompt_cache_key_source"] == "static_prefix"
        assert first["prompt_cache_key_fingerprint"].startswith("sha256:")
        assert (
            first["prompt_cache_key_fingerprint"]
            == second["prompt_cache_key_fingerprint"]
        )
        assert first["cache_scope_source"] == "cache_scope_id"
        assert (
            first["session_header_fingerprint"]
            != second["session_header_fingerprint"]
        )
        assert (
            first["session_header_fingerprint"]
            == first["client_request_header_fingerprint"]
        )
        assert first["scope_headers_match"] is True
        assert first["raw_values_omitted"] is True
        encoded = json.dumps([first, second], sort_keys=True)
        assert "private-conversation-alpha" not in encoded
        assert "private-conversation-beta" not in encoded

    def test_non_codex_responses_preserves_caller_extra_headers(self, transport):
        messages = [{"role": "user", "content": "Hi"}]

        kw = transport.build_kwargs(
            model="gpt-5.4",
            messages=messages,
            tools=[],
            is_codex_backend=False,
            request_overrides={"extra_headers": {"x-test": "1"}},
        )

        assert kw["extra_headers"] == {"x-test": "1"}

    # ── T10c: cache_scope_id header-behavior table ──────────────────────────
    # cache_scope_id is a header-only cache-scope override, distinct from the
    # run session_id, so the persona-chat lane (session_id=None to avoid
    # transcript reload) can still route a STABLE per-conversation cache scope.
    #
    #   scope set  · session None → headers present, value = scope
    #   scope set  · session set  → headers present, value = scope (scope wins)
    #   scope None · session set  → headers present, value = session (unchanged)
    #   scope None · session None → no headers (unchanged)

    def test_codex_scope_set_session_none_emits_scope_headers(self, transport):
        """Persona-chat shape: session_id=None but a stable cache_scope_id →
        cache-scope headers ARE emitted, carrying the scope value."""
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="gpt-5.4",
            messages=messages,
            tools=[],
            session_id=None,
            cache_scope_id="chat-persona-123",
            is_codex_backend=True,
        )
        headers = kw.get("extra_headers", {})
        assert headers.get("session_id") == "chat-persona-123"
        assert headers.get("x-client-request-id") == "chat-persona-123"

    def test_codex_scope_takes_precedence_over_session(self, transport):
        """When both are present, cache_scope_id wins for the routing headers."""
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="gpt-5.4",
            messages=messages,
            tools=[],
            session_id="run-session-abc",
            cache_scope_id="chat-persona-123",
            is_codex_backend=True,
        )
        headers = kw.get("extra_headers", {})
        assert headers.get("session_id") == "chat-persona-123"
        assert headers.get("x-client-request-id") == "chat-persona-123"

    def test_codex_session_used_when_scope_absent(self, transport):
        """No cache_scope_id → the headers fall back to session_id exactly as
        before (worker/mission-run lanes are unchanged)."""
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="gpt-5.4",
            messages=messages,
            tools=[],
            session_id="run-session-abc",
            cache_scope_id=None,
            is_codex_backend=True,
        )
        headers = kw.get("extra_headers", {})
        assert headers.get("session_id") == "run-session-abc"
        assert headers.get("x-client-request-id") == "run-session-abc"

    def test_codex_no_headers_when_neither_scope_nor_session(self, transport):
        """Neither present → no cache-scope headers (current behavior held)."""
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="gpt-5.4",
            messages=messages,
            tools=[],
            session_id=None,
            cache_scope_id=None,
            is_codex_backend=True,
        )
        assert "extra_headers" not in kw

    @pytest.mark.parametrize("scope_param", ["cache_scope_id", "session_id"])
    def test_codex_cache_scope_headers_bound_long_ids(self, transport, scope_param):
        """Cache-routing headers must satisfy the provider's 64-character
        limit whether their source is the persona-chat override or the normal
        session fallback. The live Alice operator-chat id is the regression
        case: it is 66 characters and was previously forwarded verbatim."""
        live_alice_scope = (
            "persona_chat_personainst_profile_alice_agent_0f044056_7b2f618f1998"
        )
        assert len(live_alice_scope) == 66

        common = dict(
            model="gpt-5.4",
            messages=[{"role": "system", "content": "You are Alice."}],
            tools=[],
            is_codex_backend=True,
        )
        first = transport.build_kwargs(**common, **{scope_param: live_alice_scope})
        repeated = transport.build_kwargs(**common, **{scope_param: live_alice_scope})
        without_scope = transport.build_kwargs(**common)
        different = transport.build_kwargs(
            **common,
            **{scope_param: f"{live_alice_scope[:-1]}0"},
        )

        first_scope = first["extra_headers"]["session_id"]
        assert first_scope == first["extra_headers"]["x-client-request-id"]
        assert first_scope != live_alice_scope
        assert 0 < len(first_scope) <= 64
        assert repeated["extra_headers"]["session_id"] == first_scope
        assert different["extra_headers"]["session_id"] != first_scope
        # Header normalization must not alter the content-addressed body key.
        assert first["prompt_cache_key"] == without_scope["prompt_cache_key"]

    def test_codex_cache_scope_preserves_id_at_provider_limit(self, transport):
        """Existing cache buckets remain stable when an id already satisfies
        the provider contract, including the exact 64-character boundary."""
        boundary_scope = "s" * 64
        kw = transport.build_kwargs(
            model="gpt-5.4",
            messages=[{"role": "user", "content": "Hi"}],
            tools=[],
            cache_scope_id=boundary_scope,
            is_codex_backend=True,
        )

        assert kw["extra_headers"]["session_id"] == boundary_scope
        assert kw["extra_headers"]["x-client-request-id"] == boundary_scope

    def test_cache_scope_id_is_header_only_not_transcript_or_cache_key(self, transport):
        """cache_scope_id must ONLY change the cache-scope headers — never the
        input items (transcript), instructions, prompt_cache_key body field, or
        anything session-load related. Build the SAME request with and without a
        scope and assert everything but extra_headers is byte-identical."""
        messages = [
            {"role": "system", "content": "You are Neko."},
            {"role": "user", "content": "status?"},
        ]
        common = dict(
            model="gpt-5.4",
            messages=messages,
            tools=[],
            session_id=None,
            is_codex_backend=True,
        )
        without = transport.build_kwargs(**common)
        with_scope = transport.build_kwargs(cache_scope_id="chat-persona-123", **common)

        # The scope only adds routing headers; the request body is untouched.
        assert with_scope["input"] == without["input"]
        assert with_scope["instructions"] == without["instructions"]
        assert with_scope.get("prompt_cache_key") == without.get("prompt_cache_key")
        # prompt_cache_key is the content-addressed hash, NOT the scope id.
        assert with_scope.get("prompt_cache_key", "").startswith("pck_")
        assert "chat-persona-123" not in with_scope.get("prompt_cache_key", "")
        # The ONLY difference is the added cache-scope headers.
        assert "extra_headers" not in without
        assert with_scope["extra_headers"]["session_id"] == "chat-persona-123"

    def test_cache_scope_id_ignored_off_codex_backend(self, transport):
        """The scope headers are codex-backend-only. A non-codex responses call
        with a cache_scope_id must NOT sprout session_id/x-client-request-id."""
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="gpt-5.4",
            messages=messages,
            tools=[],
            cache_scope_id="chat-persona-123",
            is_codex_backend=False,
        )
        headers = kw.get("extra_headers", {})
        assert "session_id" not in headers
        assert "x-client-request-id" not in headers

    def test_xai_headers(self, transport):
        messages = [{"role": "user", "content": "Hi"}]
        kw = transport.build_kwargs(
            model="grok-3", messages=messages, tools=[],
            session_id="conv-123",
            is_xai_responses=True,
        )
        assert kw.get("extra_headers", {}).get("x-grok-conv-id") == "conv-123"




    def test_xai_injects_native_web_search_when_client_web_search_present(self, transport):
        """xAI path swaps a client-side ``web_search`` function for xAI's
        native server-side ``web_search`` built-in so grok server-side search
        runs to completion (otherwise the turn stalls as
        reasoning-with-no-answer -> false 'incomplete' -> 3 retries -> fail).
        Non-conflicting client tools are preserved.
        """
        messages = [{"role": "user", "content": "Find current prices."}]
        kw = transport.build_kwargs(
            model="grok-composer-2.5-fast", messages=messages,
            tools=[
                {"type": "function", "function": {
                    "name": "read_file", "description": "Read a file.",
                    "parameters": {"type": "object",
                                   "properties": {"path": {"type": "string"}}}}},
                {"type": "function", "function": {
                    "name": "web_search", "description": "Search the web.",
                    "parameters": {"type": "object",
                                   "properties": {"query": {"type": "string"}}}}},
            ],
            is_xai_responses=True,
        )
        tool_types = [t.get("type") for t in kw.get("tools", [])]
        assert "web_search" in tool_types, kw.get("tools")
        # Non-conflicting client-side tools are preserved.
        names = [t.get("name") for t in kw.get("tools", []) if t.get("type") == "function"]
        assert "read_file" in names

    def test_xai_does_not_inject_native_web_search_without_client_web_search(self, transport):
        """The native ``web_search`` built-in is a 1:1 swap for an
        already-requested client ``web_search`` — NOT an additive grant.  A
        turn whose toolset has no ``web_search`` (user never enabled the web
        toolset) must not get Grok server-side search force-injected, which
        would silently bypass Hermes's web-provider config and tool-trace
        plumbing for every xai-oauth turn.
        """
        messages = [{"role": "user", "content": "Read this file."}]
        kw = transport.build_kwargs(
            model="grok-composer-2.5-fast", messages=messages,
            tools=[{"type": "function", "function": {
                "name": "read_file", "description": "Read a file.",
                "parameters": {"type": "object",
                               "properties": {"path": {"type": "string"}}}}}],
            is_xai_responses=True,
        )
        tools = kw.get("tools", [])
        assert not any(t.get("type") == "web_search" for t in tools), tools
        names = [t.get("name") for t in tools if t.get("type") == "function"]
        assert "read_file" in names


    def test_non_xai_path_does_not_inject_native_web_search(self, transport):
        """Native web_search injection is scoped to xAI — Codex/GitHub paths
        keep the client-side web_search function untouched."""
        messages = [{"role": "user", "content": "Search."}]
        kw = transport.build_kwargs(
            model="gpt-5.4", messages=messages,
            tools=[{"type": "function", "function": {
                "name": "web_search", "description": "Search the web.",
                "parameters": {"type": "object",
                               "properties": {"query": {"type": "string"}}}}}],
            is_xai_responses=False,
        )
        tools = kw.get("tools", [])
        assert not any(t.get("type") == "web_search" for t in tools)
        assert any(
            t.get("type") == "function" and t.get("name") == "web_search"
            for t in tools
        )



    # --- Grok reasoning-effort capability allowlist ---
    # api.x.ai 400s with "Model X does not support parameter reasoningEffort"
    # on grok-4 / grok-4-fast / grok-3 / grok-code-fast / grok-4.20-0309-*.
    # Those models reason natively but don't expose the dial. The transport
    # must omit the `reasoning` key for them.  As of May 2026 we DO request
    # ``reasoning.encrypted_content`` back from xAI on every model —
    # see test_xai_reasoning_effort_passed for the rationale.





    def test_xai_grok_4_20_0309_variants_omit_reasoning_effort(self, transport):
        """grok-4.20-0309-(non-)reasoning reject the effort dial.

        Counterintuitively, only grok-4.20-multi-agent-0309 accepts it.
        """
        messages = [{"role": "user", "content": "Hi"}]
        for model in ("grok-4.20-0309-reasoning", "grok-4.20-0309-non-reasoning"):
            kw = transport.build_kwargs(
                model=model, messages=messages, tools=[],
                is_xai_responses=True,
                reasoning_config={"effort": "high"},
            )
            assert "reasoning" not in kw, f"{model} must not receive reasoning"





class TestCodexValidateResponse:

    def test_none_response(self, transport):
        assert transport.validate_response(None) is False


    def test_valid_output(self, transport):
        r = SimpleNamespace(output=[{"type": "message", "content": []}])
        assert transport.validate_response(r) is True




    @pytest.mark.parametrize("reason", ["max_output_tokens", "length", "", None])
    def test_empty_output_other_incomplete_reasons_remain_invalid(self, transport, reason):
        r = SimpleNamespace(
            status="incomplete",
            incomplete_details=SimpleNamespace(reason=reason),
            output=[],
            output_text="",
        )
        assert transport.validate_response(r) is False


class TestCodexMapFinishReason:

    def test_completed(self, transport):
        assert transport.map_finish_reason("completed") == "stop"





class TestCodexNormalizeResponse:

    def test_text_response(self, transport):
        """Normalize a simple text Codex response."""
        r = SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="message",
                    role="assistant",
                    content=[SimpleNamespace(type="output_text", text="Hello world")],
                    status="completed",
                ),
            ],
            status="completed",
            incomplete_details=None,
            usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                  input_tokens_details=None, output_tokens_details=None),
        )
        nr = transport.normalize_response(r)
        assert isinstance(nr, NormalizedResponse)
        assert nr.content == "Hello world"
        assert nr.finish_reason == "stop"

    def test_message_items_preserved_in_provider_data(self, transport):
        """Codex assistant message item ids/phases must survive transport normalization."""
        r = SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="message",
                    role="assistant",
                    id="msg_abc",
                    phase="final_answer",
                    content=[SimpleNamespace(type="output_text", text="Hello world")],
                    status="completed",
                ),
            ],
            status="completed",
            incomplete_details=None,
            usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                  input_tokens_details=None, output_tokens_details=None),
        )
        nr = transport.normalize_response(r)
        assert nr.codex_message_items == [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "Hello world"}],
                "id": "msg_abc",
                "phase": "final_answer",
            }
        ]

    def test_tool_call_response(self, transport):
        """Normalize a Codex response with tool calls."""
        r = SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="function_call",
                    call_id="call_abc123",
                    name="terminal",
                    arguments=json.dumps({"command": "ls"}),
                    id="fc_abc123",
                    status="completed",
                ),
            ],
            status="completed",
            incomplete_details=None,
            usage=SimpleNamespace(input_tokens=10, output_tokens=20,
                                  input_tokens_details=None, output_tokens_details=None),
        )
        nr = transport.normalize_response(r)
        assert nr.finish_reason == "tool_calls"
        assert len(nr.tool_calls) == 1
        tc = nr.tool_calls[0]
        assert tc.name == "terminal"
        assert '"command"' in tc.arguments



class TestCodexTransportTimeout:
    """Forward per-request timeout from build_kwargs to the SDK kwargs."""

    def test_positive_timeout_preserved(self, transport):
        kw = transport.build_kwargs(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            timeout=600.0,
        )
        assert kw.get("timeout") == 600.0



    def test_inf_timeout_dropped(self, transport):
        kw = transport.build_kwargs(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            timeout=float("inf"),
        )
        assert "timeout" not in kw




class TestCodexTransportXaiServiceTierStrip:
    """xAI Responses API rejects ``service_tier`` (#28490).

    ``resolve_fast_mode_overrides`` only returns ``service_tier`` for
    OpenAI fast-eligible models, so on paper the field should never
    reach a Grok request.  But ``self.service_tier`` lingers across
    model switches and can also be set directly via ``agent.service_tier``
    in config.yaml — both leak paths plumb through ``request_overrides``
    and would 400 against xAI's ``/v1/responses``.
    Strip defensively when targeting xAI.
    """

    @pytest.fixture
    def transport(self):
        from agent.transports.codex import ResponsesApiTransport
        return ResponsesApiTransport()

    def test_xai_strips_service_tier_from_request_overrides(self, transport):
        """Headline #28490 case: service_tier=priority leaks through
        request_overrides, must not reach the xAI request body."""
        kw = transport.build_kwargs(
            model="grok-4.3",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            is_xai_responses=True,
            request_overrides={"service_tier": "priority"},
        )
        assert "service_tier" not in kw, (
            f"service_tier must be stripped on xAI requests, "
            f"got {kw.get('service_tier')!r}"
        )

    def test_non_xai_codex_preserves_service_tier(self, transport):
        """The strip is xAI-only — native Codex DOES accept
        service_tier=priority (OpenAI Priority Processing).  Stripping
        it elsewhere would silently disable the user's fast-mode opt-in.
        """
        kw = transport.build_kwargs(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            is_xai_responses=False,
            is_codex_backend=True,
            request_overrides={"service_tier": "priority"},
        )
        assert kw.get("service_tier") == "priority", (
            "non-xAI codex_responses providers must keep service_tier"
        )

    def test_github_responses_preserves_service_tier(self, transport):
        """GitHub Models (Copilot) is another codex_responses surface
        that should not be affected by the xAI strip."""
        kw = transport.build_kwargs(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            is_github_responses=True,
            request_overrides={"service_tier": "priority"},
        )
        assert kw.get("service_tier") == "priority"


class TestPreflightSlashEnumStrip:
    """xAI Responses safety-net: strip slash-containing enum values
    when the model name indicates a Grok target (#28490).

    Native Codex accepts ``/``-containing enums; xAI rejects them with
    HTTP 400 "Invalid arguments passed to the model".  The main agent
    loop and the auxiliary client already sanitize at request-build
    time; this preflight catches any future code path that bypasses
    those — gated on model name so we don't unnecessarily strip on
    non-xAI providers.
    """

    def _make_kwargs(self, model: str, enum_values: list[str]) -> dict:
        return {
            "model": model,
            "instructions": "test",
            "input": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "type": "function",
                    "name": "pick_model",
                    "description": "pick a model",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "model_id": {
                                "type": "string",
                                "enum": enum_values,
                            },
                        },
                    },
                },
            ],
        }

    def test_grok_model_strips_slash_enum_values(self):
        """When the model name is Grok-family, slash-containing enum
        values are stripped so xAI doesn't 400 on the tool schema."""
        from agent.codex_responses_adapter import _preflight_codex_api_kwargs
        kwargs = self._make_kwargs(
            "grok-4.3",
            ["Qwen/Qwen3.5-0.8B", "openai/gpt-oss-20b", "plain-id"],
        )
        result = _preflight_codex_api_kwargs(kwargs)
        # The enum keyword itself is stripped (per strip_slash_enum's
        # semantics — it removes the constraint entirely when any value
        # contains /).
        params = result["tools"][0]["parameters"]
        assert "enum" not in params["properties"]["model_id"], (
            "slash-containing enum must be stripped on Grok"
        )

    def test_aggregator_prefixed_grok_also_strips(self):
        """Aggregator-prefixed (x-ai/grok-*) names hit the same path."""
        from agent.codex_responses_adapter import _preflight_codex_api_kwargs
        kwargs = self._make_kwargs(
            "x-ai/grok-4.3",
            ["Qwen/Qwen3.5-0.8B"],
        )
        result = _preflight_codex_api_kwargs(kwargs)
        assert "enum" not in result["tools"][0]["parameters"]["properties"]["model_id"]

    def test_non_grok_model_preserves_slash_enum_values(self):
        """Native Codex / GitHub Models DO accept slash-containing
        enums.  The safety-net must NOT strip there or we silently
        degrade tool-schema constraints on every codex_responses
        provider that isn't xAI."""
        from agent.codex_responses_adapter import _preflight_codex_api_kwargs
        kwargs = self._make_kwargs(
            "gpt-5.5",
            ["Qwen/Qwen3.5-0.8B", "plain-id"],
        )
        result = _preflight_codex_api_kwargs(kwargs)
        params = result["tools"][0]["parameters"]
        # The enum must survive on non-xAI providers.
        assert params["properties"]["model_id"].get("enum") == [
            "Qwen/Qwen3.5-0.8B", "plain-id"
        ]
