"""Regression tests for xAI provider label disambiguation."""

from __future__ import annotations

import pytest

from hermes_cli.providers import get_label


@pytest.fixture(autouse=True)
def _seed_models_dev_xai(monkeypatch):
    """Seed the models.dev catalog this file's first assertion reads from.

    ``get_label("xai")`` has no ``_LABEL_OVERRIDES`` entry -- deliberately, and
    that is the point of the claim below: ``xai`` takes its display name from
    the models.dev catalog, ``xai-oauth`` takes its from the override table,
    and the bug was the two collapsing onto one string. The catalog reaches
    ``get_provider`` through ``agent.models_dev.fetch_models_dev``, which
    resolves ``HERMES_HOME/models_dev_cache.json`` and then the network.

    Under the suite's hermetic home that cache file does not exist, so the
    answer depended on whether the host could reach models.dev: on a networked
    box the catalog supplied ``"xAI"`` and the file passed, and on this
    workstation ``get_provider`` fell through to the Hermes overlay, whose name
    is ``_LABEL_OVERRIDES.get("xai", "xai")`` -- the lowercase id. That is a
    fixture gap, not a defect: this is the only file asserting on the label and
    it never provided the catalog it reads. It is also the single failure in
    the 2026-08-30 Windows baseline that reproduced in isolation.

    Seeding the real catalog record makes the claim hermetic and keeps it
    honest: the OAuth half still comes from production's override table, so a
    regression that collapsed the two labels reds here exactly as before.
    """
    from agent import models_dev

    monkeypatch.setattr(
        models_dev,
        "fetch_models_dev",
        lambda *_a, **_k: {
            "xai": {
                "name": "xAI",
                "api": "https://api.x.ai/v1",
                "env": ["XAI_API_KEY"],
                "doc": "https://docs.x.ai/",
                "models": {},
            }
        },
    )


def test_xai_oauth_provider_label_is_not_collapsed_to_api_key_label():
    """The model picker must distinguish xAI API-key and OAuth providers."""
    assert get_label("xai") == "xAI"
    assert get_label("xai-oauth") == "xAI Grok OAuth (SuperGrok / Premium+)"
    assert get_label("grok-oauth") == "xAI Grok OAuth (SuperGrok / Premium+)"
