"""provider_visibility v2 (transport plan W4): the typed fields that retire
the launcher's ◆-box status scrape — model/provider, API-key presence, OAuth
login state — each failure-isolated so the credential payload (which the
launcher's model switcher depends on) can never be broken by a status probe.
"""

from __future__ import annotations

import hermes_cli.harness as harness


def test_v2_schema_and_credential_payload_intact(monkeypatch):
    payload = harness.build_provider_visibility()
    assert payload["schema"] == "hermes.provider_visibility/v2"
    # The v1 contract the launcher's model switcher consumes is untouched.
    assert isinstance(payload["providers"], list)


def test_v2_environment_block_carries_model_and_provider():
    payload = harness.build_provider_visibility()
    environment = payload.get("environment")
    assert isinstance(environment, dict)
    assert "model" in environment
    assert "provider" in environment


def test_v2_api_keys_mirror_the_status_box_registry():
    from hermes_cli.status import STATUS_API_KEYS

    payload = harness.build_provider_visibility()
    api_keys = payload.get("api_keys")
    assert isinstance(api_keys, list)
    names = {row["name"] for row in api_keys}
    assert names == set(STATUS_API_KEYS.keys()), (
        "the typed contract reports the SAME registry the status box renders "
        "— hoisted, not copied, so drift is structurally impossible"
    )
    for row in api_keys:
        assert isinstance(row["configured"], bool)


def test_v2_auth_logins_report_the_oauth_lanes():
    payload = harness.build_provider_visibility()
    logins = payload.get("auth_logins")
    assert isinstance(logins, list)
    names = {row["name"] for row in logins}
    assert {"Nous Portal", "OpenAI Codex", "Qwen", "MiniMax"} <= names
    for row in logins:
        assert isinstance(row["logged_in"], bool)


def test_v2_blocks_are_failure_isolated(monkeypatch):
    """A broken status probe drops its block; it never breaks the credential
    payload — the launcher treats an absent block as 'fall back to the
    scrape', exactly like a v1 hermes."""

    def _boom() -> dict:
        raise RuntimeError("status probe broken")

    monkeypatch.setattr(harness, "_provider_visibility_environment", _boom)
    monkeypatch.setattr(harness, "_provider_visibility_api_keys", _boom)
    payload = harness.build_provider_visibility()
    assert payload["schema"] == "hermes.provider_visibility/v2"
    assert "environment" not in payload
    assert "api_keys" not in payload
    assert isinstance(payload["providers"], list)
    # auth_logins was not sabotaged and still reports.
    assert isinstance(payload.get("auth_logins"), list)
