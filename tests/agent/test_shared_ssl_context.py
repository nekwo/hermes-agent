"""Shared SSL context + CA-guard memoization (serve chat-turn latency fix).

A warm ``harness serve`` process re-parsed the certifi CA bundle 3+ times per
chat turn (agent-init client, request-scoped client, auxiliary clients, and
the ssl_guard preflight) — ~900ms/turn of pure CA parsing. These tests pin:

- ``shared_ssl_context`` memoizes per CA fingerprint and rebuilds on change;
- ``build_keepalive_http_client`` feeds the shared context into transports;
- ``verify_ca_bundle`` memoizes success only, never failure.
"""

from __future__ import annotations

import pytest

import agent.process_bootstrap as pb
import agent.ssl_guard as ssl_guard


@pytest.fixture(autouse=True)
def _reset_caches(monkeypatch):
    monkeypatch.setattr(pb, "_SSL_CONTEXT_CACHE", None)
    monkeypatch.setattr(ssl_guard, "_VERIFIED_FINGERPRINT", None)
    yield


def test_shared_ssl_context_memoizes_same_object():
    first = pb.shared_ssl_context()
    second = pb.shared_ssl_context()
    assert first is not None
    assert first is second


def test_shared_ssl_context_rebuilds_when_ca_env_changes(monkeypatch, tmp_path):
    first = pb.shared_ssl_context()
    assert first is not None
    import certifi
    import shutil

    relocated = tmp_path / "cacert.pem"
    shutil.copyfile(certifi.where(), relocated)
    monkeypatch.setenv("SSL_CERT_FILE", str(relocated))
    second = pb.shared_ssl_context()
    assert second is not None
    assert second is not first


def test_keepalive_client_uses_shared_context(monkeypatch):
    captured = {}
    import httpx

    real_transport = httpx.HTTPTransport

    def capturing_transport(*args, **kwargs):
        captured["verify"] = kwargs.get("verify")
        return real_transport(*args, **kwargs)

    monkeypatch.setattr(httpx, "HTTPTransport", capturing_transport)
    client = pb.build_keepalive_http_client("https://api.example.com")
    assert client is not None
    assert captured["verify"] is pb.shared_ssl_context()


def test_verify_ca_bundle_memoizes_success(monkeypatch):
    calls = {"n": 0}
    real_validate = ssl_guard._validate_bundle_path

    def counting_validate(*args, **kwargs):
        calls["n"] += 1
        return real_validate(*args, **kwargs)

    monkeypatch.setattr(ssl_guard, "_validate_bundle_path", counting_validate)
    ssl_guard.verify_ca_bundle()
    first_calls = calls["n"]
    assert first_calls >= 1
    ssl_guard.verify_ca_bundle()
    assert calls["n"] == first_calls  # memoized — no re-parse


def test_verify_ca_bundle_reverifies_on_env_change(monkeypatch, tmp_path):
    ssl_guard.verify_ca_bundle()
    missing = tmp_path / "nope.pem"
    monkeypatch.setenv("HERMES_CA_BUNDLE", str(missing))
    with pytest.raises(Exception):
        ssl_guard.verify_ca_bundle()


def test_verify_ca_bundle_failure_is_not_cached(monkeypatch, tmp_path):
    missing = tmp_path / "nope.pem"
    monkeypatch.setenv("HERMES_CA_BUNDLE", str(missing))
    with pytest.raises(Exception):
        ssl_guard.verify_ca_bundle()
    # Same broken fingerprint must fail again, not silently pass from cache.
    with pytest.raises(Exception):
        ssl_guard.verify_ca_bundle()
