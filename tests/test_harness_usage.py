"""Tests for `hermes harness usage` — the typed account-usage envelope.

No live network: every seam (`_usage_lane_detected`, `_fetch_usage_lane`,
`_resolve_active_provider_id`) is monkeypatched on ``hermes_cli.harness`` so the
tests exercise the envelope build / serialization / failure-isolation logic
without touching a real provider account.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
from hermes_cli import harness


def _snapshot(provider="openai-codex"):
    return AccountUsageSnapshot(
        provider=provider,
        source="usage_api",
        fetched_at=datetime(2026, 7, 23, 12, 34, 56, tzinfo=timezone.utc),
        plan="Pro",
        windows=(
            AccountUsageWindow(
                label="Session",
                used_percent=15.0,
                reset_at=datetime(2026, 7, 23, 15, 0, 0, tzinfo=timezone.utc),
            ),
            AccountUsageWindow(
                label="Weekly",
                used_percent=40.0,
                reset_at=datetime(2026, 7, 27, 0, 0, 0, tzinfo=timezone.utc),
            ),
        ),
        details=("Credits balance: $12.50",),
    )


def test_usage_codex_logged_in_active(monkeypatch):
    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(harness, "_usage_lane_detected", lambda p: p == "openai-codex")
    monkeypatch.setattr(
        harness, "_fetch_usage_lane", lambda p: _snapshot() if p == "openai-codex" else None
    )

    payload = harness.build_account_usage(timeout=5.0)

    assert payload["schema"] == "hermes.account_usage/v1"
    # generated_at is ISO-8601 UTC and parses cleanly.
    assert datetime.fromisoformat(payload["generated_at"]).tzinfo is not None
    assert payload["active_provider"] == "openai-codex"
    assert len(payload["lanes"]) == 1

    lane = payload["lanes"][0]
    assert lane["provider"] == "openai-codex"
    assert lane["active"] is True
    assert lane["available"] is True
    assert lane["plan"] == "Pro"
    assert lane["source"] == "usage_api"
    assert lane["fetched_at"] == "2026-07-23T12:34:56+00:00"
    assert lane["unavailable_reason"] is None

    session, weekly = lane["windows"]
    assert session["label"] == "Session"
    assert session["used_percent"] == 15.0
    assert session["reset_at"] == "2026-07-23T15:00:00+00:00"
    assert session["detail"] is None
    assert weekly["label"] == "Weekly"
    assert weekly["reset_at"] == "2026-07-27T00:00:00+00:00"
    assert lane["details"] == ["Credits balance: $12.50"]


def test_usage_lane_fetch_raising_is_isolated(monkeypatch):
    """A raising fetch degrades ONLY its own lane, with a class-name-only reason
    that never leaks the exception message (which could carry a token / URL)."""
    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: None)
    monkeypatch.setattr(
        harness, "_usage_lane_detected", lambda p: p in {"openai-codex", "anthropic"}
    )

    def fake_fetch(provider_id):
        if provider_id == "anthropic":
            raise ValueError("Bearer sk-secret-token https://host/path?key=leak")
        return AccountUsageSnapshot(
            provider="openai-codex",
            source="usage_api",
            fetched_at=datetime.now(timezone.utc),
            windows=(AccountUsageWindow(label="Session", used_percent=5.0),),
        )

    monkeypatch.setattr(harness, "_fetch_usage_lane", fake_fetch)

    payload = harness.build_account_usage(timeout=5.0)
    lanes = {lane["provider"]: lane for lane in payload["lanes"]}
    assert set(lanes) == {"openai-codex", "anthropic"}

    # Healthy lane unaffected.
    assert lanes["openai-codex"]["available"] is True
    assert lanes["openai-codex"]["windows"][0]["label"] == "Session"

    # Failed lane: available False, class-name-only reason, empty windows/details.
    failed = lanes["anthropic"]
    assert failed["available"] is False
    assert failed["unavailable_reason"] == "usage fetch failed (ValueError)"
    assert "secret" not in failed["unavailable_reason"]
    assert "leak" not in failed["unavailable_reason"]
    assert "https://" not in failed["unavailable_reason"]
    assert failed["windows"] == []
    assert failed["details"] == []
    assert failed["plan"] is None


def test_usage_none_snapshot_on_detected_lane_is_no_usage_data(monkeypatch):
    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: None)
    monkeypatch.setattr(harness, "_usage_lane_detected", lambda p: p == "openrouter")
    monkeypatch.setattr(harness, "_fetch_usage_lane", lambda p: None)

    payload = harness.build_account_usage(timeout=5.0)
    assert len(payload["lanes"]) == 1
    lane = payload["lanes"][0]
    assert lane["provider"] == "openrouter"
    assert lane["available"] is False
    assert lane["unavailable_reason"] == "no usage data"


def test_usage_no_logins_emits_empty_lanes(monkeypatch):
    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: None)
    monkeypatch.setattr(harness, "_usage_lane_detected", lambda p: False)

    payload = harness.build_account_usage(timeout=5.0)
    assert payload["schema"] == "hermes.account_usage/v1"
    assert payload["active_provider"] is None
    assert payload["lanes"] == []


def test_usage_provider_filter_restricts_to_one_lane(monkeypatch):
    # All providers "detected", but the filter narrows to codex only.
    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(harness, "_usage_lane_detected", lambda p: True)
    monkeypatch.setattr(
        harness,
        "_fetch_usage_lane",
        lambda p: AccountUsageSnapshot(
            provider=p,
            source="usage_api",
            fetched_at=datetime.now(timezone.utc),
            windows=(AccountUsageWindow(label="Session", used_percent=1.0),),
        ),
    )

    payload = harness.build_account_usage(only_provider="openai-codex", timeout=5.0)
    assert [lane["provider"] for lane in payload["lanes"]] == ["openai-codex"]
    assert payload["lanes"][0]["active"] is True


def test_usage_provider_filter_unknown_provider_yields_empty(monkeypatch):
    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: None)
    monkeypatch.setattr(harness, "_usage_lane_detected", lambda p: True)
    monkeypatch.setattr(harness, "_fetch_usage_lane", lambda p: _snapshot(p))

    payload = harness.build_account_usage(only_provider="not-a-provider", timeout=5.0)
    assert payload["lanes"] == []


def test_cmd_usage_json_emits_schema_exit_zero(monkeypatch, capsys):
    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: None)
    monkeypatch.setattr(harness, "_usage_lane_detected", lambda p: False)

    args = SimpleNamespace(json=True, provider=None, timeout=5.0)
    rc = harness._cmd_usage(args)

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["schema"] == "hermes.account_usage/v1"
    assert data["lanes"] == []


def test_cmd_usage_human_mode_exit_zero(monkeypatch, capsys):
    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(harness, "_usage_lane_detected", lambda p: p == "openai-codex")
    monkeypatch.setattr(
        harness, "_fetch_usage_lane", lambda p: _snapshot() if p == "openai-codex" else None
    )

    args = SimpleNamespace(json=False, provider=None, timeout=5.0)
    rc = harness._cmd_usage(args)

    assert rc == 0
    out = capsys.readouterr().out
    assert "Active provider: openai-codex" in out
    # Reuses the shared renderer: "% remaining (% used)" grammar + resets line.
    assert "Session:" in out
    assert "85% remaining (15% used)" in out
