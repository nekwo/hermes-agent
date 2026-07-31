from types import SimpleNamespace

from agent.usage_pricing import (
    CanonicalUsage,
    estimate_usage_cost,
    get_pricing_entry,
    normalize_usage,
    resolve_billing_route,
)








def test_normalize_usage_reads_deepseek_native_cache_hit_tokens():
    """DeepSeek's native API (api.deepseek.com) reports context-cache hits as
    top-level prompt_cache_hit_tokens / prompt_cache_miss_tokens (with
    prompt_tokens = hit + miss), not OpenAI's nested
    prompt_tokens_details.cached_tokens. Before this fix, direct DeepSeek
    sessions always normalized to cache_read_tokens=0 — cache hits were
    invisible in accounting and billed at the full input rate (#61871)."""
    usage = SimpleNamespace(
        prompt_tokens=2000,
        completion_tokens=400,
        prompt_cache_hit_tokens=1500,
        prompt_cache_miss_tokens=500,
    )

    normalized = normalize_usage(usage, provider="deepseek", api_mode="chat_completions")

    assert normalized.cache_read_tokens == 1500
    # prompt_tokens includes cached; input = 2000 - 1500 = the miss bucket
    assert normalized.input_tokens == 500
    assert normalized.output_tokens == 400




def test_normalize_usage_openai_reads_top_level_anthropic_cache_fields():
    """Some OpenAI-compatible proxies (OpenRouter, Vercel AI Gateway, Cline) expose
    Anthropic-style cache token counts at the top level of the usage object when
    routing Claude models, instead of nesting them in prompt_tokens_details.

    Regression guard for the bug fixed in cline/cline#10266 — before this fix,
    the chat-completions branch of normalize_usage() only read
    prompt_tokens_details.cache_write_tokens and completely missed the
    cache_creation_input_tokens case, so cache writes showed as 0 and reflected
    inputTokens were overstated by the cache-write amount.
    """
    usage = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=200,
        prompt_tokens_details=SimpleNamespace(cached_tokens=500),
        cache_creation_input_tokens=300,
    )

    normalized = normalize_usage(usage, provider="openrouter", api_mode="chat_completions")

    # Expected: cache read from prompt_tokens_details.cached_tokens (preferred),
    # cache write from top-level cache_creation_input_tokens (fallback).
    assert normalized.cache_read_tokens == 500
    assert normalized.cache_write_tokens == 300
    # input_tokens = prompt_total - cache_read - cache_write = 1000 - 500 - 300 = 200
    assert normalized.input_tokens == 200
    assert normalized.output_tokens == 200


# Fork-retained (Context-Cost T4, 2026-07-18): the codex_responses metering
# branch is a live-turn path with no upstream coverage. Keep both guards.
def test_normalize_usage_codex_responses_reads_cached_from_input_tokens_details():
    """Codex Responses (``api_mode='codex_responses'``, e.g. openai-codex /
    gpt-5.6-luna) reports usage in the OpenAI *Responses* shape: ``input_tokens``
    is the FULL prompt (cached tokens included) and the cached portion is broken
    out under ``input_tokens_details.cached_tokens`` — NOT ``prompt_tokens`` /
    ``prompt_tokens_details`` (that's the Chat Completions shape).

    Regression guard for the Context-Cost T4 investigation (2026-07-18): the
    codex_responses branch of normalize_usage() was the one live-metering path
    with no unit coverage even though live turns depend on it for cache-hit
    accounting. Live frames confirmed multi-call codex turns record nonzero
    ``cache_read_tokens`` precisely because this mapping works; this test locks
    that contract so a future refactor can't silently drop it back to 0.

    Contract asserted (not a frozen snapshot): input_tokens = input_total −
    cached, cache_read = cached, and prompt_tokens (the billed bucket) =
    input_tokens + cache_read == input_total.
    """
    usage = SimpleNamespace(
        input_tokens=14822,
        output_tokens=30,
        input_tokens_details=SimpleNamespace(cached_tokens=12800),
        output_tokens_details=SimpleNamespace(reasoning_tokens=12),
        total_tokens=14852,
    )

    normalized = normalize_usage(usage, provider="openai-codex", api_mode="codex_responses")

    assert normalized.cache_read_tokens == 12800
    assert normalized.cache_write_tokens == 0
    # input_tokens = input_total - cached (the Responses total is inclusive)
    assert normalized.input_tokens == 14822 - 12800
    # prompt bucket = uncached input + cache read == the reported input total
    assert normalized.prompt_tokens == 14822
    assert normalized.output_tokens == 30
    assert normalized.reasoning_tokens == 12


def test_normalize_usage_codex_responses_ignores_chat_completions_fields():
    """The codex_responses branch must key off ``input_tokens`` /
    ``input_tokens_details``, never ``prompt_tokens`` / ``prompt_tokens_details``.

    A Responses-shape payload has no ``prompt_tokens``; if the wrong branch ran,
    prompt_total would resolve to 0 and cache_read would be missed. This proves
    the api_mode dispatch selects the Responses reader, and that a cold first
    call (no cached prefix yet — the sampled T4 frame) correctly reports 0
    cache_read without inventing one.
    """
    usage = SimpleNamespace(
        input_tokens=14822,
        output_tokens=30,
        input_tokens_details=SimpleNamespace(cached_tokens=0),
        total_tokens=14852,
    )

    normalized = normalize_usage(usage, provider="openai-codex", api_mode="codex_responses")

    assert normalized.cache_read_tokens == 0
    assert normalized.input_tokens == 14822
    assert normalized.prompt_tokens == 14822


def test_deepseek_v4_pro_pricing_entry_exists():
    """Regression test: deepseek-v4-pro must have a pricing entry.

    Before this fix, deepseek-v4-pro sessions showed as unknown cost
    in hermes insights because the _OFFICIAL_DOCS_PRICING table had no
    entry for that model.  See #24218.  Rates track the 2026-07 price cut
    ($1.74/$3.48 → $0.435/$0.87).
    """
    entry = get_pricing_entry(
        "deepseek-v4-pro",
        provider="deepseek",
    )

    assert entry is not None
    assert entry.input_cost_per_million is not None
    assert entry.output_cost_per_million is not None
    assert float(entry.input_cost_per_million) == 0.435
    assert float(entry.output_cost_per_million) == 0.87
    assert float(entry.cache_read_cost_per_million) == 0.003625




def test_deepseek_deprecated_aliases_price_as_v4_flash():
    """Invariant: deepseek-chat / deepseek-reasoner are deprecated aliases for
    deepseek-v4-flash's non-thinking / thinking modes (deprecation 2026-07-24)
    — they must bill at identical rates to the flash entry, or sessions on the
    legacy names over/under-report cost."""
    flash = get_pricing_entry("deepseek-v4-flash", provider="deepseek")
    assert flash is not None
    for alias in ("deepseek-chat", "deepseek-reasoner"):
        entry = get_pricing_entry(alias, provider="deepseek")
        assert entry is not None, alias
        assert entry.input_cost_per_million == flash.input_cost_per_million, alias
        assert entry.output_cost_per_million == flash.output_cost_per_million, alias
        assert (
            entry.cache_read_cost_per_million == flash.cache_read_cost_per_million
        ), alias




def test_bedrock_claude_rows_all_carry_cache_pricing():
    """Invariant: every Bedrock Claude pricing row must carry cache-read AND
    cache-write rates, otherwise a cached session prices as ``unknown``.

    Bedrock Claude routes through the AnthropicBedrock SDK and injects
    cache_control, so cached tokens are always reported — the pricing layer
    must be able to value them.  See #50295.
    """
    from agent.usage_pricing import _OFFICIAL_DOCS_PRICING

    claude_rows = [
        (prov, model)
        for (prov, model) in _OFFICIAL_DOCS_PRICING
        if prov == "bedrock" and "claude" in model
    ]
    assert claude_rows, "expected at least one bedrock Claude pricing row"
    for key in claude_rows:
        entry = _OFFICIAL_DOCS_PRICING[key]
        assert entry.input_cost_per_million is not None, key
        assert entry.cache_read_cost_per_million is not None, key
        assert entry.cache_write_cost_per_million is not None, key
        # Cache reads are cheaper than fresh input; cache writes cost more.
        assert entry.cache_read_cost_per_million < entry.input_cost_per_million, key
        assert entry.cache_write_cost_per_million > entry.input_cost_per_million, key


def test_bedrock_current_gen_claude_rows_resolve():
    """Current-gen Claude models (Opus 4.8/4.7, Sonnet 5) must have Bedrock
    pricing rows so cached sessions report a dollar cost, not ``unknown``.
    Assert each resolves via the bare id and a cross-region inference profile
    (us./global. prefix), that every id for a given model resolves to the same
    entry, and that the row carries the cache fields a Bedrock Claude session
    needs.

    (Version-suffixed IDs like ``...-v1:0`` are covered separately by the
    normalizer test in the suffix-strip change; this test intentionally sticks
    to id shapes that resolve on ``main`` so it is independent of that PR.)
    """
    url = "https://bedrock-runtime.us-east-1.amazonaws.com"
    for bare in (
        "anthropic.claude-opus-4-8",
        "anthropic.claude-opus-4-7",
        "anthropic.claude-sonnet-5",
    ):
        ref = get_pricing_entry(bare, provider="bedrock", base_url=url)
        assert ref is not None, bare
        assert ref.input_cost_per_million is not None, bare
        assert ref.output_cost_per_million is not None, bare
        # Output costs more than input across the Claude line; sanity-check the
        # row isn't malformed (input < output).
        assert ref.output_cost_per_million > ref.input_cost_per_million, bare
        # Cache fields present so cached sessions price correctly (the #50295
        # symptom was unknown cost on cached Bedrock Claude sessions).
        assert ref.cache_read_cost_per_million is not None, bare
        assert ref.cache_write_cost_per_million is not None, bare
        # Cross-region inference profiles resolve to the same entry.
        for mid in (f"us.{bare}", f"global.{bare}"):
            entry = get_pricing_entry(mid, provider="bedrock", base_url=url)
            assert entry is not None, mid
            assert entry.input_cost_per_million == ref.input_cost_per_million, mid
            assert entry.output_cost_per_million == ref.output_cost_per_million, mid




def test_bedrock_versioned_inference_profile_resolves_to_bare_pricing():
    """Bedrock profile IDs may include the provider's dated version suffix.

    The pricing table intentionally uses shorter model-family IDs, so the
    lookup needs a longest-prefix fallback after stripping the region scope.
    """
    bare = get_pricing_entry("anthropic.claude-sonnet-4-6", provider="bedrock")
    assert bare is not None

    for model in (
        "us.anthropic.claude-sonnet-4-6-20250514-v1:0",
        "global.anthropic.claude-sonnet-4-6-20250514-v1:0",
    ):
        scoped = get_pricing_entry(model, provider="bedrock")
        assert scoped is not None, model
        assert scoped.input_cost_per_million == bare.input_cost_per_million
        assert scoped.output_cost_per_million == bare.output_cost_per_million
        assert scoped.cache_read_cost_per_million == bare.cache_read_cost_per_million
        assert scoped.cache_write_cost_per_million == bare.cache_write_cost_per_million






def test_bedrock_claude_cached_session_estimates_cost_not_unknown():
    """A Bedrock Claude session with cache hits must produce a dollar estimate,
    not ``unknown`` — the user-visible symptom in #50295.
    """
    bedrock_url = "https://bedrock-runtime.us-east-1.amazonaws.com"
    usage = SimpleNamespace(
        input_tokens=55,
        output_tokens=7113,
        cache_read_input_tokens=1369379,
        cache_creation_input_tokens=42135,
    )
    canonical = normalize_usage(usage, provider="bedrock", api_mode="anthropic_messages")
    assert canonical.cache_read_tokens == 1369379
    assert canonical.cache_write_tokens == 42135

    result = estimate_usage_cost(
        "us.anthropic.claude-opus-4-6",
        canonical,
        provider="bedrock",
        base_url=bedrock_url,
    )
    assert result.status == "estimated"
    assert result.amount_usd is not None







def test_fireworks_router_fast_tier_prices_distinctly():
    """Fast serving tiers live under accounts/fireworks/routers/<name>-fast and
    bill at higher rates than the standard model — the routing layer's
    rsplit("/", 1) must land on the distinct fast-tier entry."""
    standard = get_pricing_entry(
        "accounts/fireworks/models/kimi-k2p6",
        provider="fireworks",
        base_url="https://api.fireworks.ai/inference/v1",
    )
    fast = get_pricing_entry(
        "accounts/fireworks/routers/kimi-k2p6-fast",
        provider="fireworks",
        base_url="https://api.fireworks.ai/inference/v1",
    )
    assert standard is not None and fast is not None
    assert fast.input_cost_per_million > standard.input_cost_per_million
    assert fast.output_cost_per_million > standard.output_cost_per_million












def test_google_and_vertex_routes_share_official_pricing_snapshot():
    """Direct Gemini, Vertex, and Vertex's OpenAI-compatible hostname must
    all normalize to the Google official-pricing route.
    """
    routes = (
        resolve_billing_route("model", provider="gemini"),
        resolve_billing_route("google/model", provider="vertex"),
        resolve_billing_route(
            "google/model",
            provider="custom",
            base_url="https://aiplatform.googleapis.com/v1/projects/example",
        ),
    )

    assert all(route.provider == "google" for route in routes)
    assert all(route.billing_mode == "official_docs_snapshot" for route in routes)


def test_vertex_default_model_estimates_cached_usage(monkeypatch):
    """The bundled Vertex profile's default auxiliary model must fall back to
    Google snapshot pricing when the OpenAI-compatible endpoint has no model
    metadata, including for cache-read accounting.
    """
    from providers import get_provider_profile

    monkeypatch.setattr(
        "agent.usage_pricing.fetch_endpoint_model_metadata",
        lambda *_args, **_kwargs: {},
    )
    vertex = get_provider_profile("vertex")
    result = estimate_usage_cost(
        vertex.default_aux_model,
        CanonicalUsage(input_tokens=100, output_tokens=100, cache_read_tokens=100),
        provider=vertex.name,
        base_url=vertex.base_url,
    )

    assert result.status == "estimated"
    assert result.amount_usd is not None and result.amount_usd > 0
