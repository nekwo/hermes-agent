"""Static descriptors for the certified OpenAI-compatible mobile lane."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderDescriptor:
    id: str
    display_name: str
    default_base_url: str
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    reasoning_delta: bool = True
    model_id_hint: str = "Provider model identifier"
    requires_base_url: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_PROVIDERS: tuple[ProviderDescriptor, ...] = (
    ProviderDescriptor(
        id="openrouter",
        display_name="OpenRouter",
        default_base_url="https://openrouter.ai/api/v1",
        model_id_hint="vendor/model, for example anthropic/claude-sonnet-4.6",
    ),
    ProviderDescriptor(
        id="openai",
        display_name="OpenAI",
        default_base_url="https://api.openai.com/v1",
        model_id_hint="OpenAI model identifier",
    ),
    ProviderDescriptor(
        id="compatible",
        display_name="OpenAI-compatible endpoint",
        default_base_url="",
        model_id_hint="Model identifier accepted by the configured endpoint",
        requires_base_url=True,
    ),
)

PROVIDERS = {item.id: item for item in _PROVIDERS}


def get_provider(provider_id: str) -> ProviderDescriptor | None:
    return PROVIDERS.get(str(provider_id or "").strip().lower())


def list_supported_providers() -> list[dict[str, Any]]:
    return [item.to_dict() for item in _PROVIDERS]
