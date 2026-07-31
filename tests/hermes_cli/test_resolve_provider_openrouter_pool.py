"""Regression tests for issue #42130.

A credential added via `hermes auth add openrouter` lives in the credential
pool, NOT as an OPENROUTER_API_KEY env var. Before the fix, resolve_provider()
auto-detection only checked env vars, so such a credential was invisible:
the provider failed to resolve (AuthError) or resolved without a key, and
requests went out with no Authorization header — OpenRouter's
"HTTP 401: Missing Authentication header".

These tests lock in that auto-detection consults the OpenRouter pool.
"""

import uuid

import pytest


@pytest.fixture(autouse=True)
def _clean_inference_env(monkeypatch, tmp_path):
    """Strip credential-shaped env vars so the pool is the only source."""
    for key in (
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "NOUS_API_KEY",
        "HERMES_INFERENCE_PROVIDER",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "missing-credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "missing-config"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


def _seed_openrouter_pool(token: str = "sk-or-FAKEKEY123") -> None:
    """Mimic `hermes auth add openrouter <token>` — a manual pool entry."""
    from agent.credential_pool import (
        AUTH_TYPE_API_KEY,
        SOURCE_MANUAL,
        PooledCredential,
        load_pool,
    )

    pool = load_pool("openrouter")
    pool.add_entry(
        PooledCredential(
            provider="openrouter",
            id=uuid.uuid4().hex[:6],
            label="api-key-1",
            auth_type=AUTH_TYPE_API_KEY,
            priority=0,
            source=SOURCE_MANUAL,
            access_token=token,
            base_url="https://openrouter.ai/api/v1",
        )
    )


def test_auto_detects_openrouter_from_pool(tmp_path, monkeypatch):
    """With only a pool credential (no env var), auto-detection finds it."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    (tmp_path / "hermes").mkdir(parents=True, exist_ok=True)
    _seed_openrouter_pool()

    from hermes_cli.auth import resolve_provider

    assert resolve_provider("auto") == "openrouter"


