from __future__ import annotations


def test_mobile_catalog_matches_desktop_provider_universe() -> None:
    from hermes_cli.provider_catalog import provider_catalog
    from hermes_mobile_core.providers import PROVIDERS

    assert tuple(PROVIDERS) == tuple(item.slug for item in provider_catalog())


def test_every_provider_has_explicit_mobile_capability() -> None:
    from hermes_mobile_core.providers import PROVIDERS

    assert len(PROVIDERS) >= 37
    assert all(item.mobile_capability for item in PROVIDERS.values())
    assert PROVIDERS["openai-codex"].supports_usage is True
    assert PROVIDERS["openai-codex"].auth_type == "oauth_external"
    assert PROVIDERS["copilot-acp"].mobile_capability == "desktop_only"
    assert PROVIDERS["bedrock"].mobile_capability == "desktop_only"


def test_certified_openai_chat_urls_match_desktop_catalog() -> None:
    from hermes_cli.providers import get_provider
    from hermes_mobile_core.providers import PROVIDERS

    for slug, mobile in PROVIDERS.items():
        desktop = get_provider(slug)
        if desktop and desktop.base_url and mobile.default_base_url and mobile.mobile_capability == "available":
            assert mobile.default_base_url == desktop.base_url
