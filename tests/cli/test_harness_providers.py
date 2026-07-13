"""Typed credential-health contract for `hermes harness providers --json`.

Pins the mapping from a pooled credential's exhaustion state onto the machine
payload the Launcher consumes, so the "401 vs 429 vs dead vs healthy"
distinction can never silently regress into a bare "not connected".
"""

import time

from agent.credential_pool import STATUS_DEAD, STATUS_EXHAUSTED, PooledCredential
from hermes_cli.harness import _credential_health, build_provider_visibility


def _credential(**overrides) -> PooledCredential:
    base = dict(
        provider="opencode-zen",
        id="c1",
        label="OPENCODE_ZEN_API_KEY",
        auth_type="api_key",
        priority=0,
        source="env:OPENCODE_ZEN_API_KEY",
        access_token="tok-not-persisted",
    )
    base.update(overrides)
    return PooledCredential(**base)


def test_healthy_credential_has_no_annotation():
    assert _credential_health(_credential(last_status="ok")) == {"state": "healthy"}
    assert _credential_health(_credential(last_status=None)) == {"state": "healthy"}


def test_auth_failure_maps_to_auth_failed_with_code():
    health = _credential_health(
        _credential(
            last_status=STATUS_EXHAUSTED,
            last_status_at=time.time(),
            last_error_code=401,
        )
    )
    assert health["state"] == "auth_failed"
    assert health["code"] == 401
    assert health["retry_at"] is None  # not retryable — needs re-auth
    assert "401" in health["message"]


def test_rate_limit_maps_to_rate_limited_with_retry_window():
    health = _credential_health(
        _credential(
            last_status=STATUS_EXHAUSTED,
            last_status_at=time.time(),
            last_error_code=429,
        )
    )
    assert health["state"] == "rate_limited"
    assert health["code"] == 429
    assert health["retry_at"] is not None  # retryable — carries a window


def test_dead_credential_is_surfaced_not_hidden():
    # The human `auth list` renders a dead credential with NO annotation, so it
    # reads as healthy. The typed contract must make it explicit.
    health = _credential_health(
        _credential(last_status=STATUS_DEAD, last_error_code=401)
    )
    assert health["state"] == "dead"


def test_build_provider_visibility_shape():
    payload = build_provider_visibility()
    assert payload["schema"] == "hermes.provider_visibility/v1"
    assert isinstance(payload["providers"], list)
    for provider in payload["providers"]:
        assert provider["id"]
        assert provider["credentials"], "empty pools are skipped, mirroring auth list"
        for credential in provider["credentials"]:
            assert credential["health"]["state"] in {
                "healthy",
                "auth_failed",
                "rate_limited",
                "exhausted",
                "dead",
            }
