"""Mobile-safe provider account authentication and quota queries.

Credentials are returned to the native host for OS secure-storage persistence;
this module never writes tokens to disk and never includes them in diagnostics.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlsplit

import httpx

from .exceptions import InvalidRequest, MobileUnsupported
from .providers import get_provider

CODEX_ISSUER = "https://auth.openai.com"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_TOKEN_URL = f"{CODEX_ISSUER}/oauth/token"
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_USER_AGENT = "codex_cli_rs/0.0.0 (Hermes Agent Mobile)"


class ProviderAuthError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False, relogin: bool = False):
        self.code = code
        self.retryable = retryable
        self.relogin = relogin
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "relogin_required": self.relogin,
        }


@dataclass
class DeviceLogin:
    provider: str
    login_id: str
    user_code: str
    device_auth_id: str
    verification_url: str
    interval: int
    expires_at: float
    next_poll_at: float
    flow: str = "device"
    status: str = "pending"
    opaque: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "provider": self.provider,
            "login_id": self.login_id,
            "user_code": self.user_code,
            "verification_url": self.verification_url,
            "interval_seconds": self.interval,
            "expires_at": int(self.expires_at),
        }


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _account_id(token: str) -> str:
    auth = _jwt_claims(token).get("https://api.openai.com/auth") or {}
    return str(auth.get("chatgpt_account_id") or "") if isinstance(auth, Mapping) else ""


def _expires_at(token: str, expires_in: Any = None) -> int | None:
    exp = _jwt_claims(token).get("exp")
    if isinstance(exp, (int, float)):
        return int(exp)
    if isinstance(expires_in, (int, float)):
        return int(time.time() + float(expires_in))
    return None


def codex_headers(access_token: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": CODEX_USER_AGENT,
        "originator": "codex_cli_rs",
    }
    account = _account_id(access_token)
    if account:
        headers["ChatGPT-Account-ID"] = account
    return headers


class MobileAuthManager:
    def __init__(
        self,
        *,
        client_factory: Callable[..., httpx.Client] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client_factory = client_factory or httpx.Client
        self._clock = clock
        self._logins: dict[str, DeviceLogin] = {}

    def begin_login(self, provider_id: str) -> dict[str, Any]:
        provider = get_provider(provider_id)
        if provider is None:
            raise InvalidRequest("provider is not supported")
        if provider.id == "xai-oauth":
            return self._begin_xai()
        if provider.id == "nous":
            return self._begin_nous()
        if provider.id == "anthropic":
            return self._begin_anthropic()
        if provider.id != "openai-codex":
            raise MobileUnsupported(provider.unavailable_reason or "This provider login is not mobile-certified")
        try:
            with self._client_factory(timeout=httpx.Timeout(15.0), verify=True) as client:
                response = client.post(
                    f"{CODEX_ISSUER}/api/accounts/deviceauth/usercode",
                    json={"client_id": CODEX_CLIENT_ID},
                    headers={"Content-Type": "application/json", "User-Agent": CODEX_USER_AGENT},
                )
        except httpx.HTTPError as exc:
            raise ProviderAuthError("device_code_network", "Could not reach the OpenAI login service", retryable=True) from exc
        if response.status_code == 429:
            raise ProviderAuthError("rate_limited", "OpenAI is temporarily rate-limiting login attempts", retryable=True)
        if response.status_code != 200:
            raise ProviderAuthError("device_code_failed", f"OpenAI login returned HTTP {response.status_code}", retryable=response.status_code >= 500)
        try:
            payload = response.json()
            user_code = str(payload["user_code"])
            device_auth_id = str(payload["device_auth_id"])
            interval = max(3, int(payload.get("interval") or 5))
            expires_in = max(60, int(payload.get("expires_in") or 900))
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderAuthError("device_code_invalid", "OpenAI returned an incomplete login response") from exc
        login_id = str(uuid.uuid4())
        now = self._clock()
        login = DeviceLogin(
            provider=provider.id, login_id=login_id, user_code=user_code,
            device_auth_id=device_auth_id,
            verification_url=f"{CODEX_ISSUER}/codex/device", interval=interval,
            expires_at=now + expires_in, next_poll_at=now,
        )
        self._logins[login_id] = login
        return login.public()

    def poll_login(self, login_id: str) -> dict[str, Any]:
        login = self._logins.get(str(login_id))
        if login is None:
            raise InvalidRequest("login session was not found")
        now = self._clock()
        if now >= login.expires_at:
            self._logins.pop(login.login_id, None)
            return {"status": "expired", "provider": login.provider, "login_id": login.login_id}
        if now < login.next_poll_at:
            result = login.public()
            result["retry_after_seconds"] = max(1, int(login.next_poll_at - now))
            return result
        if login.flow == "xai_device":
            return self._poll_xai(login)
        if login.flow == "nous_device":
            return self._poll_nous(login)
        if login.flow == "anthropic_code":
            return login.public()
        login.next_poll_at = now + login.interval
        try:
            with self._client_factory(timeout=httpx.Timeout(15.0), verify=True) as client:
                response = client.post(
                    f"{CODEX_ISSUER}/api/accounts/deviceauth/token",
                    json={"device_auth_id": login.device_auth_id, "user_code": login.user_code},
                    headers={"Content-Type": "application/json", "User-Agent": CODEX_USER_AGENT},
                )
        except httpx.HTTPError:
            result = login.public()
            result["retry_after_seconds"] = login.interval
            return result
        if response.status_code in {403, 404}:
            return login.public()
        if response.status_code == 429:
            raise ProviderAuthError("rate_limited", "OpenAI is temporarily rate-limiting login polling", retryable=True)
        if response.status_code != 200:
            raise ProviderAuthError("device_code_poll_failed", f"OpenAI login polling returned HTTP {response.status_code}", retryable=response.status_code >= 500)
        payload = response.json() or {}
        code = str(payload.get("authorization_code") or "")
        verifier = str(payload.get("code_verifier") or "")
        if not code or not verifier:
            raise ProviderAuthError("device_code_exchange_invalid", "OpenAI returned an incomplete authorization response")
        credential = self._exchange(code, verifier)
        self._logins.pop(login.login_id, None)
        return {
            "status": "complete", "provider": login.provider,
            "login_id": login.login_id, "credential": credential,
        }

    def complete_login(self, login_id: str, authorization_code: str) -> dict[str, Any]:
        login = self._logins.get(str(login_id))
        if login is None:
            raise InvalidRequest("login session was not found")
        if login.flow != "anthropic_code":
            raise InvalidRequest("this login does not accept an authorization code")
        raw = str(authorization_code or "").strip()
        code, _, received_state = raw.partition("#")
        opaque = login.opaque or {}
        if not code or received_state != opaque.get("state"):
            raise ProviderAuthError("state_mismatch", "The Claude authorization code did not match this login session")
        body = {
            "grant_type": "authorization_code",
            "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
            "code": code,
            "state": received_state,
            "redirect_uri": "https://console.anthropic.com/oauth/code/callback",
            "code_verifier": opaque.get("verifier"),
        }
        response = None
        for endpoint in (
            "https://platform.claude.com/v1/oauth/token",
            "https://console.anthropic.com/v1/oauth/token",
        ):
            with self._client_factory(timeout=httpx.Timeout(15.0), verify=True) as client:
                candidate = client.post(endpoint, json=body, headers={"Content-Type": "application/json", "User-Agent": "axios/1.7.9"})
            response = candidate
            if candidate.status_code == 200:
                break
        assert response is not None
        if response.status_code != 200:
            raise ProviderAuthError("token_exchange_failed", f"Claude token exchange returned HTTP {response.status_code}", retryable=response.status_code >= 500)
        payload = response.json() or {}
        access = str(payload.get("access_token") or "")
        if not access:
            raise ProviderAuthError("token_response_invalid", "Claude token response was missing an access token")
        credential = {
            "type": "oauth", "access_token": access,
            "refresh_token": str(payload.get("refresh_token") or ""),
            "expires_at": int(self._clock() + int(payload.get("expires_in") or 3600)),
            "base_url": "https://api.anthropic.com",
        }
        self._logins.pop(login.login_id, None)
        return {"status": "complete", "provider": login.provider, "login_id": login.login_id, "credential": credential}

    def _begin_xai(self) -> dict[str, Any]:
        with self._client_factory(timeout=httpx.Timeout(15.0), verify=True) as client:
            discovery = client.get("https://auth.x.ai/.well-known/openid-configuration", headers={"Accept": "application/json"})
            if discovery.status_code != 200:
                raise ProviderAuthError("xai_discovery_failed", f"xAI discovery returned HTTP {discovery.status_code}", retryable=discovery.status_code >= 500)
            token_endpoint = str((discovery.json() or {}).get("token_endpoint") or "")
            parsed_endpoint = urlsplit(token_endpoint)
            endpoint_host = (parsed_endpoint.hostname or "").lower()
            if parsed_endpoint.scheme != "https" or not (
                endpoint_host == "x.ai" or endpoint_host.endswith(".x.ai")
            ):
                raise ProviderAuthError("xai_discovery_invalid", "xAI returned an unsafe token endpoint")
            response = client.post(
                "https://auth.x.ai/oauth2/device/code",
                data={"client_id": "b1a00492-073a-47ea-816f-4c329264a828", "scope": "openid profile email offline_access grok-cli:access api:access"},
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            )
        if response.status_code != 200:
            raise ProviderAuthError("device_code_failed", f"xAI login returned HTTP {response.status_code}", retryable=response.status_code >= 500)
        payload = response.json() or {}
        login = self._device_login_from_payload(
            provider="xai-oauth", payload=payload,
            verification_url=str(payload.get("verification_uri_complete") or payload.get("verification_uri") or ""),
            device_id=str(payload.get("device_code") or ""), flow="xai_device",
            opaque={"token_endpoint": token_endpoint},
        )
        return login.public()

    def _begin_nous(self) -> dict[str, Any]:
        with self._client_factory(timeout=httpx.Timeout(15.0), verify=True) as client:
            response = client.post(
                "https://portal.nousresearch.com/api/oauth/device/code",
                data={"client_id": "hermes-cli", "scope": "inference:invoke"},
                headers={"Accept": "application/json"},
            )
        if response.status_code != 200:
            raise ProviderAuthError("device_code_failed", f"Nous login returned HTTP {response.status_code}", retryable=response.status_code >= 500)
        payload = response.json() or {}
        login = self._device_login_from_payload(
            provider="nous", payload=payload,
            verification_url=str(payload.get("verification_uri_complete") or payload.get("verification_uri") or ""),
            device_id=str(payload.get("device_code") or ""), flow="nous_device",
        )
        return login.public()

    def _begin_anthropic(self) -> dict[str, Any]:
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        oauth_state = secrets.token_urlsafe(32)
        url = "https://claude.ai/oauth/authorize?" + urlencode({
            "code": "true", "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
            "response_type": "code", "redirect_uri": "https://console.anthropic.com/oauth/code/callback",
            "scope": "org:create_api_key user:profile user:inference",
            "code_challenge": challenge, "code_challenge_method": "S256", "state": oauth_state,
        })
        now = self._clock()
        login = DeviceLogin(
            provider="anthropic", login_id=str(uuid.uuid4()), user_code="",
            device_auth_id="", verification_url=url, interval=5,
            expires_at=now + 900, next_poll_at=now, flow="anthropic_code",
            status="code_required", opaque={"verifier": verifier, "state": oauth_state},
        )
        self._logins[login.login_id] = login
        return login.public()

    def _device_login_from_payload(self, *, provider: str, payload: Mapping[str, Any], verification_url: str, device_id: str, flow: str, opaque: dict[str, Any] | None = None) -> DeviceLogin:
        user_code = str(payload.get("user_code") or "")
        if not device_id or not user_code or not verification_url:
            raise ProviderAuthError("device_code_invalid", "Provider returned an incomplete device login response")
        now = self._clock()
        login = DeviceLogin(
            provider=provider, login_id=str(uuid.uuid4()), user_code=user_code,
            device_auth_id=device_id, verification_url=verification_url,
            interval=max(1, int(payload.get("interval") or 5)),
            expires_at=now + max(60, int(payload.get("expires_in") or 900)),
            next_poll_at=now, flow=flow, opaque=opaque,
        )
        self._logins[login.login_id] = login
        return login

    def _poll_xai(self, login: DeviceLogin) -> dict[str, Any]:
        login.next_poll_at = self._clock() + login.interval
        endpoint = str((login.opaque or {}).get("token_endpoint") or "")
        with self._client_factory(timeout=httpx.Timeout(15.0), verify=True) as client:
            response = client.post(endpoint, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": "b1a00492-073a-47ea-816f-4c329264a828",
                "device_code": login.device_auth_id,
            }, headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        return self._device_token_result(login, response, base_url="https://api.x.ai/v1", token_endpoint=endpoint)

    def _poll_nous(self, login: DeviceLogin) -> dict[str, Any]:
        login.next_poll_at = self._clock() + login.interval
        with self._client_factory(timeout=httpx.Timeout(15.0), verify=True) as client:
            response = client.post(
                "https://portal.nousresearch.com/api/oauth/token",
                data={"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "client_id": "hermes-cli", "device_code": login.device_auth_id},
                headers={"Accept": "application/json"},
            )
        return self._device_token_result(login, response, base_url="https://inference-api.nousresearch.com/v1")

    def _device_token_result(self, login: DeviceLogin, response: httpx.Response, *, base_url: str, token_endpoint: str = "") -> dict[str, Any]:
        payload = response.json() if response.content else {}
        if response.status_code != 200:
            error = str(payload.get("error") or "") if isinstance(payload, Mapping) else ""
            if error in {"authorization_pending", "slow_down"}:
                if error == "slow_down": login.interval = min(login.interval + 1, 30)
                return login.public()
            raise ProviderAuthError("device_code_poll_failed", f"{login.provider} login polling returned HTTP {response.status_code}", retryable=response.status_code >= 500)
        access = str(payload.get("access_token") or "")
        if not access:
            raise ProviderAuthError("token_response_invalid", "Provider token response was missing an access token")
        credential = {
            "type": "oauth", "access_token": access,
            "refresh_token": str(payload.get("refresh_token") or ""),
            "expires_at": _expires_at(access, payload.get("expires_in")),
            "base_url": str(payload.get("inference_base_url") or base_url),
        }
        if token_endpoint:
            credential["token_endpoint"] = token_endpoint
        self._logins.pop(login.login_id, None)
        return {"status": "complete", "provider": login.provider, "login_id": login.login_id, "credential": credential}

    def cancel_login(self, login_id: str) -> dict[str, Any]:
        found = self._logins.pop(str(login_id), None) is not None
        return {"login_id": str(login_id), "cancelled": found}

    def _exchange(self, code: str, verifier: str) -> dict[str, Any]:
        with self._client_factory(timeout=httpx.Timeout(15.0), verify=True) as client:
            response = client.post(
                CODEX_TOKEN_URL,
                data={
                    "grant_type": "authorization_code", "code": code,
                    "redirect_uri": f"{CODEX_ISSUER}/deviceauth/callback",
                    "client_id": CODEX_CLIENT_ID, "code_verifier": verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": CODEX_USER_AGENT},
            )
        return self._token_response(response, existing_refresh="")

    def refresh(self, provider_id: str, credential: Mapping[str, Any]) -> dict[str, Any]:
        if provider_id == "xai-oauth":
            return self._refresh_form_oauth(
                credential,
                endpoint=str(credential.get("token_endpoint") or "https://auth.x.ai/oauth2/token"),
                client_id="b1a00492-073a-47ea-816f-4c329264a828",
                base_url="https://api.x.ai/v1",
            )
        if provider_id == "nous":
            refresh_token = str(credential.get("refresh_token") or "")
            if not refresh_token:
                raise ProviderAuthError("missing_refresh_token", "Sign in again to refresh this profile", relogin=True)
            with self._client_factory(timeout=httpx.Timeout(15.0), verify=True) as client:
                response = client.post(
                    "https://portal.nousresearch.com/api/oauth/token",
                    headers={"x-nous-refresh-token": refresh_token, "Accept": "application/json"},
                    data={"grant_type": "refresh_token", "client_id": "hermes-cli"},
                )
            return self._generic_refresh_response(response, credential, base_url="https://inference-api.nousresearch.com/v1")
        if provider_id == "anthropic":
            refresh_token = str(credential.get("refresh_token") or "")
            if not refresh_token:
                raise ProviderAuthError("missing_refresh_token", "Sign in again to refresh this profile", relogin=True)
            response = None
            for endpoint in (
                "https://platform.claude.com/v1/oauth/token",
                "https://console.anthropic.com/v1/oauth/token",
            ):
                with self._client_factory(timeout=httpx.Timeout(15.0), verify=True) as client:
                    candidate = client.post(endpoint, data={
                        "grant_type": "refresh_token", "refresh_token": refresh_token,
                        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
                    }, headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "axios/1.7.9"})
                response = candidate
                if candidate.status_code == 200:
                    break
            assert response is not None
            return self._generic_refresh_response(response, credential, base_url="https://api.anthropic.com")
        if provider_id != "openai-codex":
            return dict(credential)
        refresh_token = str(credential.get("refresh_token") or "")
        if not refresh_token:
            raise ProviderAuthError("missing_refresh_token", "Sign in again to refresh this profile", relogin=True)
        with self._client_factory(timeout=httpx.Timeout(20.0), verify=True) as client:
            response = client.post(
                CODEX_TOKEN_URL,
                data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CODEX_CLIENT_ID},
                headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": CODEX_USER_AGENT},
            )
        return self._token_response(response, existing_refresh=refresh_token)

    def _refresh_form_oauth(self, credential: Mapping[str, Any], *, endpoint: str, client_id: str, base_url: str) -> dict[str, Any]:
        refresh_token = str(credential.get("refresh_token") or "")
        if not refresh_token:
            raise ProviderAuthError("missing_refresh_token", "Sign in again to refresh this profile", relogin=True)
        with self._client_factory(timeout=httpx.Timeout(20.0), verify=True) as client:
            response = client.post(endpoint, data={
                "grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id,
            }, headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        updated = self._generic_refresh_response(response, credential, base_url=base_url)
        updated["token_endpoint"] = endpoint
        return updated

    def _generic_refresh_response(self, response: httpx.Response, credential: Mapping[str, Any], *, base_url: str) -> dict[str, Any]:
        if response.status_code != 200:
            raise ProviderAuthError("refresh_failed", f"Provider token refresh returned HTTP {response.status_code}", retryable=response.status_code >= 500, relogin=response.status_code in {400, 401, 403})
        payload = response.json() or {}
        access = str(payload.get("access_token") or "")
        if not access:
            raise ProviderAuthError("refresh_invalid", "Provider refresh response was missing an access token", relogin=True)
        return {
            "type": "oauth", "access_token": access,
            "refresh_token": str(payload.get("refresh_token") or credential.get("refresh_token") or ""),
            "expires_at": _expires_at(access, payload.get("expires_in")),
            "base_url": str(payload.get("inference_base_url") or credential.get("base_url") or base_url),
        }

    def _token_response(self, response: httpx.Response, *, existing_refresh: str) -> dict[str, Any]:
        if response.status_code == 429:
            raise ProviderAuthError("rate_limited", "OpenAI temporarily rejected the token request; credentials remain valid", retryable=True)
        if response.status_code != 200:
            raise ProviderAuthError("token_exchange_failed", f"OpenAI token request returned HTTP {response.status_code}", retryable=response.status_code >= 500, relogin=response.status_code in {400, 401, 403})
        payload = response.json() or {}
        access = str(payload.get("access_token") or "")
        refresh = str(payload.get("refresh_token") or existing_refresh)
        if not access:
            raise ProviderAuthError("token_response_invalid", "OpenAI token response was missing an access token", relogin=True)
        return {
            "type": "oauth", "access_token": access, "refresh_token": refresh,
            "expires_at": _expires_at(access, payload.get("expires_in")),
            "base_url": CODEX_BASE_URL,
        }

    def rate_limits(self, provider_id: str, credential: Mapping[str, Any]) -> dict[str, Any]:
        if provider_id == "anthropic":
            access = str(credential.get("access_token") or "")
            if not access:
                raise InvalidRequest("OAuth access token is required")
            with self._client_factory(timeout=httpx.Timeout(15.0), verify=True) as client:
                response = client.get(
                    "https://api.anthropic.com/api/oauth/usage",
                    headers={
                        "Authorization": f"Bearer {access}", "Accept": "application/json",
                        "Content-Type": "application/json", "anthropic-beta": "oauth-2025-04-20",
                        "User-Agent": "claude-code/2.1.74",
                    },
                )
            if response.status_code != 200:
                raise ProviderAuthError("usage_failed", f"Claude usage returned HTTP {response.status_code}", retryable=response.status_code >= 500, relogin=response.status_code in {401, 403})
            payload = response.json() or {}
            windows = []
            for key, label in (("five_hour", "Session"), ("seven_day", "Weekly"), ("seven_day_opus", "Opus weekly"), ("seven_day_sonnet", "Sonnet weekly")):
                window = payload.get(key) or {}
                utilization = window.get("utilization")
                if isinstance(utilization, (int, float)):
                    used = float(utilization) * 100 if float(utilization) <= 1 else float(utilization)
                    reset = window.get("resets_at")
                    if isinstance(reset, str):
                        try:
                            reset = int(datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp())
                        except ValueError:
                            reset = None
                    windows.append({"label": label, "used_percent": used, "reset_at": reset})
            return {
                "provider": provider_id, "available": True, "plan": "Claude Pro / Max",
                "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "windows": windows,
            }
        if provider_id != "openai-codex":
            return {"provider": provider_id, "available": False, "reason": "This provider does not expose a certified account-usage endpoint."}
        access = str(credential.get("access_token") or "")
        if not access:
            raise InvalidRequest("OAuth access token is required")
        with self._client_factory(timeout=httpx.Timeout(15.0), verify=True) as client:
            response = client.get(
                "https://chatgpt.com/backend-api/wham/usage",
                headers=codex_headers(access),
            )
        if response.status_code in {401, 403}:
            raise ProviderAuthError("authentication", "The ChatGPT login has expired", relogin=True)
        if response.status_code == 429:
            raise ProviderAuthError("rate_limited", "The ChatGPT usage service is temporarily rate-limited", retryable=True)
        if response.status_code != 200:
            raise ProviderAuthError("usage_failed", f"ChatGPT usage returned HTTP {response.status_code}", retryable=response.status_code >= 500)
        payload = response.json() or {}
        rate_limit = payload.get("rate_limit") or {}
        windows = []
        for key, label in (("primary_window", "Session"), ("secondary_window", "Weekly")):
            window = rate_limit.get(key) or {}
            used = window.get("used_percent")
            if isinstance(used, (int, float)):
                windows.append({"label": label, "used_percent": float(used), "reset_at": window.get("reset_at")})
        return {
            "provider": provider_id, "available": True,
            "plan": str(payload.get("plan_type") or "").replace("_", " ").title(),
            "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "windows": windows,
        }


def credential_secret(provider_id: str, credential: Mapping[str, Any] | str) -> str:
    if isinstance(credential, str):
        return credential.strip()
    provider = get_provider(provider_id)
    if provider is None:
        return ""
    field = "access_token" if credential.get("type") == "oauth" or provider.auth_type.startswith("oauth") else "secret"
    return str(credential.get(field) or "").strip()
