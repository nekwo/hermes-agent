from __future__ import annotations


def test_mobile_descriptors_match_desktop_provider_authorities() -> None:
    from hermes_cli.auth import PROVIDER_REGISTRY
    from providers import get_provider_profile

    from hermes_mobile_core.providers import PROVIDERS

    desktop_openrouter = get_provider_profile("openrouter")
    desktop_custom = get_provider_profile("custom")
    assert desktop_openrouter is not None
    assert desktop_custom is not None
    assert PROVIDERS["openrouter"].default_base_url == desktop_openrouter.base_url
    assert PROVIDERS["compatible"].default_base_url == desktop_custom.base_url
    assert (
        PROVIDERS["openai"].default_base_url
        == PROVIDER_REGISTRY["openai-api"].inference_base_url
    )
