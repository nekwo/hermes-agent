from __future__ import annotations

import base64
import json

import httpx
import pytest

from hermes_mobile_core.auth import MobileAuthManager, ProviderAuthError


def _jwt(account: str = "acct_fixture") -> str:
    payload = base64.urlsafe_b64encode(json.dumps({
        "exp": 2000000000,
        "https://api.openai.com/auth": {"chatgpt_account_id": account},
    }).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


def _factory(handler):
    transport = httpx.MockTransport(handler)
    def factory(**kwargs):
        kwargs.pop("verify", None)
        return httpx.Client(transport=transport, **kwargs)
    return factory


def test_codex_device_login_poll_exchange_and_weekly_usage() -> None:
    calls = []
    token = _jwt()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), request.headers))
        if str(request.url).endswith("/deviceauth/usercode"):
            return httpx.Response(200, json={"user_code": "ABCD-EFGH", "device_auth_id": "dev_1", "interval": 3})
        if str(request.url).endswith("/deviceauth/token"):
            return httpx.Response(200, json={"authorization_code": "code_1", "code_verifier": "verify_1"})
        if str(request.url).endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": token, "refresh_token": "refresh_1"})
        if str(request.url).endswith("/wham/usage"):
            assert request.headers["ChatGPT-Account-ID"] == "acct_fixture"
            return httpx.Response(200, json={
                "plan_type": "plus",
                "rate_limit": {
                    "primary_window": {"used_percent": 21, "reset_at": 1779846359},
                    "secondary_window": {"used_percent": 4, "reset_at": 1780230796},
                },
            })
        raise AssertionError(str(request.url))

    manager = MobileAuthManager(client_factory=_factory(handler), clock=lambda: 1000)
    login = manager.begin_login("openai-codex")
    assert login["verification_url"] == "https://auth.openai.com/codex/device"
    assert "device_auth_id" not in login
    complete = manager.poll_login(login["login_id"])
    assert complete["status"] == "complete"
    credential = complete["credential"]
    assert credential["access_token"] == token
    limits = manager.rate_limits("openai-codex", credential)
    assert limits["plan"] == "Plus"
    assert limits["windows"][1]["label"] == "Weekly"
    assert limits["windows"][1]["used_percent"] == 4


def test_codex_refresh_rotates_refresh_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert b"grant_type=refresh_token" in request.content
        return httpx.Response(200, json={"access_token": _jwt(), "refresh_token": "refresh_2"})

    manager = MobileAuthManager(client_factory=_factory(handler))
    refreshed = manager.refresh("openai-codex", {"access_token": "old", "refresh_token": "refresh_1"})
    assert refreshed["refresh_token"] == "refresh_2"


def test_xai_subscription_device_login_uses_hermes_oauth_contract() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"token_endpoint": "https://auth.x.ai/oauth2/token"})
        if url.endswith("/oauth2/device/code"):
            return httpx.Response(200, json={
                "device_code": "xdev", "user_code": "XAI-CODE",
                "verification_uri": "https://auth.x.ai/activate",
                "verification_uri_complete": "https://auth.x.ai/activate?code=XAI-CODE",
                "expires_in": 900, "interval": 1,
            })
        if url.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "xai-access", "refresh_token": "xai-refresh", "expires_in": 3600})
        raise AssertionError(url)

    manager = MobileAuthManager(client_factory=_factory(handler), clock=lambda: 1000)
    login = manager.begin_login("xai-oauth")
    assert login["user_code"] == "XAI-CODE"
    complete = manager.poll_login(login["login_id"])
    assert complete["credential"]["base_url"] == "https://api.x.ai/v1"
    assert complete["credential"]["refresh_token"] == "xai-refresh"


def test_xai_rejects_discovered_token_endpoint_outside_xai_domain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"token_endpoint": "https://attackerx.ai/oauth2/token"},
        )

    manager = MobileAuthManager(client_factory=_factory(handler))
    with pytest.raises(ProviderAuthError, match="unsafe token endpoint"):
        manager.begin_login("xai-oauth")


def test_xai_access_denied_is_terminal_not_pending() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={"token_endpoint": "https://auth.x.ai/oauth2/token"},
            )
        if url.endswith("/oauth2/device/code"):
            return httpx.Response(
                200,
                json={
                    "device_code": "xdev",
                    "user_code": "XAI-CODE",
                    "verification_uri": "https://auth.x.ai/activate",
                    "expires_in": 900,
                    "interval": 1,
                },
            )
        return httpx.Response(400, json={"error": "access_denied"})

    manager = MobileAuthManager(client_factory=_factory(handler), clock=lambda: 1000)
    login = manager.begin_login("xai-oauth")
    with pytest.raises(ProviderAuthError, match="polling returned HTTP 400"):
        manager.poll_login(login["login_id"])


def test_claude_subscription_pkce_requires_matching_returned_state() -> None:
    seen = {}
    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"access_token": "cc-access", "refresh_token": "cc-refresh", "expires_in": 3600})

    manager = MobileAuthManager(client_factory=_factory(handler), clock=lambda: 1000)
    login = manager.begin_login("anthropic")
    assert login["status"] == "code_required"
    state = login["verification_url"].split("state=", 1)[1].split("&", 1)[0]
    complete = manager.complete_login(login["login_id"], f"auth-code#{state}")
    assert complete["credential"]["access_token"] == "cc-access"
    assert seen["body"]["code_verifier"]
