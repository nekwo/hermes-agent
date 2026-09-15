"""Mobile projection of Hermes's generated canonical provider manifest."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from importlib.resources import files
from typing import Any


@dataclass(frozen=True)
class ProviderDescriptor:
    id: str
    display_name: str
    description: str
    default_base_url: str
    auth_type: str
    transport: str
    mobile_capability: str = "available"
    unavailable_reason: str = ""
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    reasoning_delta: bool = True
    model_id_hint: str = "Provider model identifier"
    requires_base_url: bool = False
    supports_usage: bool = False
    supports_account_login: bool = False
    api_key_env_vars: tuple[str, ...] = ()

    @property
    def is_mobile_available(self) -> bool:
        return self.mobile_capability == "available"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["api_key_env_vars"] = list(self.api_key_env_vars)
        value["is_mobile_available"] = self.is_mobile_available
        return value


_DESKTOP_ONLY = {
    "moa": "Mixture of Agents requires the desktop Hermes orchestration runtime.",
    "lmstudio": "A phone cannot address the desktop loopback server. Configure a custom HTTPS endpoint instead.",
    "copilot-acp": "ACP requires spawning the desktop copilot process.",
    "vertex": "Vertex requires the GCP credential and SDK runtime.",
    "bedrock": "Bedrock requires the AWS SDK and platform credential chain.",
}
_TRANSPORT_UNAVAILABLE = {"gemini", "minimax", "minimax-oauth", "minimax-cn"}
_LOGIN_UNAVAILABLE = {"qwen-oauth"}
_ACCOUNT_LOGIN = {"openai-codex", "xai-oauth", "nous", "anthropic"}


def _capability(provider_id: str) -> tuple[str, str]:
    if provider_id in _DESKTOP_ONLY:
        return "desktop_only", _DESKTOP_ONLY[provider_id]
    if provider_id in _TRANSPORT_UNAVAILABLE:
        return "transport_unavailable", "This Hermes transport needs a desktop SDK or subprocess and is not packaged on mobile yet."
    if provider_id in _LOGIN_UNAVAILABLE:
        return "login_unavailable", "This provider is catalogued, but its Hermes account login flow is not mobile-certified yet."
    return "available", ""


def _load() -> tuple[ProviderDescriptor, ...]:
    raw = json.loads(files("hermes_mobile_core").joinpath("provider_catalog.json").read_text(encoding="utf-8"))
    output = []
    for row in raw["providers"]:
        capability, reason = _capability(str(row["id"]))
        default_url = str(row.get("default_base_url") or "")
        if row["id"] == "anthropic" and not default_url:
            default_url = "https://api.anthropic.com"
        output.append(ProviderDescriptor(
            id=str(row["id"]),
            display_name=str(row["display_name"]),
            description=str(row["description"]),
            default_base_url=default_url,
            auth_type=str(row.get("auth_type") or "api_key"),
            transport=str(row.get("transport") or ""),
            mobile_capability=capability,
            unavailable_reason=reason,
            requires_base_url=str(row["id"]) in {"azure-foundry", "custom"},
            supports_usage=str(row["id"]) in {"openai-codex", "anthropic"},
            supports_account_login=str(row["id"]) in _ACCOUNT_LOGIN,
            api_key_env_vars=tuple(row.get("api_key_env_vars") or ()),
        ))
    return tuple(output)


_PROVIDERS = _load()
PROVIDERS = {item.id: item for item in _PROVIDERS}


def get_provider(provider_id: str) -> ProviderDescriptor | None:
    return PROVIDERS.get(str(provider_id or "").strip().lower())


def list_supported_providers() -> list[dict[str, Any]]:
    return [item.to_dict() for item in _PROVIDERS]
