"""Coverage for the snapshot-boundary cache-policy resolver.

The resolver is the single authority the Launcher's cache-freshness indicator
depends on: contractual TTLs must count down for real, estimated ones must be
labelled as estimates, and unknown providers must not fabricate either.
"""

from agent_runtime.cache_policy import (
    CACHE_MODE_AUTOMATIC,
    CACHE_MODE_EXPLICIT,
    CACHE_MODE_NONE,
    resolve_cache_policy,
)


def test_anthropic_is_contractual():
    policy = resolve_cache_policy(
        provider="anthropic",
        model="claude-sonnet-5",
        api_mode="anthropic_messages",
        base_url="https://api.anthropic.com",
    )
    assert policy.mode == CACHE_MODE_EXPLICIT
    assert policy.ttl_basis == "contractual"
    assert policy.ttl_seconds in (300, 3600)


def test_openai_codex_is_estimated_automatic():
    policy = resolve_cache_policy(
        provider="openai-codex",
        model="gpt-5.6-luna",
        api_mode="codex",
    )
    assert policy.mode == CACHE_MODE_AUTOMATIC
    assert policy.ttl_basis == "estimated"
    assert policy.ttl_seconds == 300


def test_plain_openai_chat_is_estimated_automatic():
    policy = resolve_cache_policy(provider="openai", model="gpt-4.1", api_mode="chat_completions")
    assert policy.mode == CACHE_MODE_AUTOMATIC
    assert policy.ttl_basis == "estimated"


def test_unknown_provider_reports_none_not_a_guess():
    policy = resolve_cache_policy(provider="some-local-llm", model="mystery-7b")
    assert policy.mode == CACHE_MODE_NONE
    assert policy.ttl_seconds is None
    assert policy.ttl_basis is None


def test_empty_identity_is_none():
    assert resolve_cache_policy(provider=None, model=None).mode == CACHE_MODE_NONE


def test_snapshot_fields_are_always_present():
    fields = resolve_cache_policy(provider="openai-codex", model="gpt-5.6-luna").as_snapshot_fields()
    assert set(fields) == {"cache_mode", "cache_ttl_seconds", "cache_ttl_basis"}
