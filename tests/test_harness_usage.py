"""Tests for `hermes harness usage` — the typed account-usage envelope.

No live network: every seam (`_usage_lane_detected`, `_fetch_usage_lane`,
`_resolve_active_provider_id`) is monkeypatched on ``hermes_cli.harness`` so the
tests exercise the envelope build / serialization / failure-isolation logic
without touching a real provider account.
"""

import argparse
import json
import time
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


def _reject_non_finite_json_token(name):
    raise AssertionError(f"non-finite JSON token in output: {name!r}")


def test_serialize_window_drops_non_finite_percent_and_json_is_strict(monkeypatch):
    """A NaN/inf ``used_percent`` would serialize (via emit_json → json.dumps
    with allow_nan=True) as the bare ``NaN``/``Infinity`` token — invalid JSON
    that sinks the whole envelope on the Launcher's strict parser. The non-finite
    window is dropped; the finite window survives; the full output is strict JSON.
    """
    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(harness, "_usage_lane_detected", lambda p: p == "openai-codex")
    monkeypatch.setattr(
        harness,
        "_fetch_usage_lane",
        lambda p: AccountUsageSnapshot(
            provider="openai-codex",
            source="usage_api",
            fetched_at=datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc),
            windows=(
                AccountUsageWindow(label="Session", used_percent=float("nan")),
                AccountUsageWindow(label="Overage", used_percent=float("inf")),
                AccountUsageWindow(label="Weekly", used_percent=42.0),
            ),
        ),
    )

    payload = harness.build_account_usage(timeout=5.0)
    lane = payload["lanes"][0]
    # Only the finite window survives; the lane stays available (honest data).
    assert [w["label"] for w in lane["windows"]] == ["Weekly"]
    assert lane["windows"][0]["used_percent"] == 42.0
    assert lane["available"] is True

    # The FULL envelope must be strict JSON — parse_constant fires on any
    # NaN/Infinity/-Infinity token and would fail the test if one leaked.
    rendered = harness.emit_json(payload)
    parsed = json.loads(rendered, parse_constant=_reject_non_finite_json_token)
    assert parsed["lanes"][0]["windows"][0]["label"] == "Weekly"


def test_cmd_usage_json_branch_isolates_serialization_failure(monkeypatch, capsys):
    """If emit_json raises inside the --json branch, the verb still prints a
    minimal valid empty-lanes envelope and exits 0 (never propagates)."""
    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: None)
    monkeypatch.setattr(harness, "_usage_lane_detected", lambda p: False)

    calls = {"n": 0}

    def boom(_payload):
        calls["n"] += 1
        raise RuntimeError("serialization exploded")

    monkeypatch.setattr(harness, "emit_json", boom)

    args = SimpleNamespace(json=True, provider=None, timeout=5.0)
    rc = harness._cmd_usage(args)

    assert rc == 0
    assert calls["n"] >= 1  # emit_json was attempted and failed
    data = json.loads(capsys.readouterr().out)
    assert data["schema"] == "hermes.account_usage/v1"
    assert data["lanes"] == []


# --- S1: a failed usage fetch says WHAT failed --------------------------------
#
# These tests deliberately do NOT monkeypatch `harness._fetch_usage_lane`. The
# whole defect lived inside it (routing through `fetch_account_usage`, whose
# blanket `except Exception: return None` erased the class), so a test that
# stubs that seam out cannot see the bug. They patch the UPSTREAM per-provider
# fetcher instead — the one object both the old and the new routing call — so
# the only variable under test is which path `_fetch_usage_lane` takes.


def _http_status_error(status: int):
    """A real ``httpx.HTTPStatusError`` carrying ``status``, shaped like the one
    ``raise_for_status()`` throws at ``account_usage.py:524``. The message text
    is deliberately token- and URL-shaped so the leak assertions below are not
    vacuous."""
    import httpx

    request = httpx.Request("GET", "https://example.invalid/usage?key=sk-LEAKME")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(
        "Bearer sk-LEAKME rejected by https://example.invalid/usage",
        request=request,
        response=response,
    )


def test_usage_codex_401_reports_http_status_not_no_usage_data(monkeypatch):
    """The operator's live defect: the Codex ``/usage`` endpoint 401s, the
    blanket upstream except turned it into None, and the console rendered the
    unfalsifiable "no usage data". The lane must now name the status.

    MUTATION (proves non-vacuity): restore ``_fetch_usage_lane``'s
    ``return fetch_account_usage(provider_id)`` for the shared lanes — the
    exception is swallowed upstream, the lane regresses to "no usage data", and
    this assertion goes red. The probed field (``unavailable_reason``) IS
    written by the mutated path too — it just writes a provably different
    string, which is why probing it is not vacuous.
    """
    import agent.account_usage as account_usage

    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(harness, "_usage_lane_detected", lambda p: p == "openai-codex")

    def raising(*_args, **_kwargs):
        raise _http_status_error(401)

    monkeypatch.setattr(account_usage, "_fetch_codex_account_usage", raising)

    payload = harness.build_account_usage(timeout=5.0)
    assert len(payload["lanes"]) == 1
    lane = payload["lanes"][0]
    assert lane["provider"] == "openai-codex"
    assert lane["available"] is False
    reason = lane["unavailable_reason"]
    assert reason == "usage fetch failed (HTTP 401 — re-auth may be required)"
    # The status code is the ONLY thing borrowed from the exception. Nothing
    # from its message (token- and URL-shaped above) may appear.
    assert "sk-LEAKME" not in reason
    assert "https://" not in reason
    assert "example.invalid" not in reason


def test_usage_codex_500_reports_status_without_reauth_hint(monkeypatch):
    """Reauth-vs-connectivity discipline: only a confirmed auth rejection may
    suggest signing in again. A 500 is a server failure and gets the bare code.

    MUTATION: drop the ``status in (401, 403)`` guard in
    ``_usage_failure_reason`` and always append the hint — red. The probed fact
    is the ABSENCE of the suffix on a non-auth status; the unguarded path writes
    the same field with the suffix present, so it cannot pass by accident.
    """
    import agent.account_usage as account_usage

    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: None)
    monkeypatch.setattr(harness, "_usage_lane_detected", lambda p: p == "openai-codex")

    def raising(*_args, **_kwargs):
        raise _http_status_error(500)

    monkeypatch.setattr(account_usage, "_fetch_codex_account_usage", raising)

    lane = harness.build_account_usage(timeout=5.0)["lanes"][0]
    assert lane["unavailable_reason"] == "usage fetch failed (HTTP 500)"
    assert "re-auth" not in lane["unavailable_reason"]


def test_usage_non_http_exception_keeps_class_name_only(monkeypatch):
    """Everything that is not an HTTP status error keeps the class-name-only
    discipline — the leak guard is unchanged for the general case.

    MUTATION: make ``_usage_failure_reason`` fall back to ``str(exc)`` — red,
    and the token/URL assertions below fire.
    """
    import agent.account_usage as account_usage

    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: None)
    monkeypatch.setattr(harness, "_usage_lane_detected", lambda p: p == "anthropic")

    def raising(*_args, **_kwargs):
        raise ValueError("Bearer sk-secret https://host/path?key=leak")

    monkeypatch.setattr(account_usage, "_fetch_anthropic_account_usage", raising)

    lane = harness.build_account_usage(timeout=5.0)["lanes"][0]
    assert lane["unavailable_reason"] == "usage fetch failed (ValueError)"
    assert "secret" not in lane["unavailable_reason"]
    assert "leak" not in lane["unavailable_reason"]


def test_usage_raising_anthropic_does_not_sink_the_codex_lane(monkeypatch):
    """Per-lane isolation survives the direct dispatch: one provider's 401 must
    not blank another provider's real numbers.

    MUTATION: hoist the ``try/except`` out of the per-future loop in
    ``_fetch_usage_lanes`` — the codex lane disappears and this goes red.
    """
    import agent.account_usage as account_usage

    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: None)
    monkeypatch.setattr(
        harness, "_usage_lane_detected", lambda p: p in {"openai-codex", "anthropic"}
    )

    def raising(*_args, **_kwargs):
        raise _http_status_error(401)

    monkeypatch.setattr(account_usage, "_fetch_anthropic_account_usage", raising)
    monkeypatch.setattr(
        account_usage,
        "_fetch_codex_account_usage",
        lambda *_a, **_k: AccountUsageSnapshot(
            provider="openai-codex",
            source="usage_api",
            fetched_at=datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc),
            windows=(AccountUsageWindow(label="Session", used_percent=1.0),),
        ),
    )

    lanes = {
        lane["provider"]: lane
        for lane in harness.build_account_usage(timeout=5.0)["lanes"]
    }
    assert lanes["openai-codex"]["available"] is True
    assert lanes["openai-codex"]["windows"][0]["label"] == "Session"
    assert (
        lanes["anthropic"]["unavailable_reason"]
        == "usage fetch failed (HTTP 401 — re-auth may be required)"
    )


def test_usage_declining_fetcher_still_reads_no_usage_data(monkeypatch):
    """"no usage data" survives, narrowed to its one honest meaning: a fetcher
    that returned None WITHOUT raising.

    MUTATION: make ``_serialize_usage_lane`` emit a failure reason for None —
    red.
    """
    import agent.account_usage as account_usage

    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: None)
    monkeypatch.setattr(harness, "_usage_lane_detected", lambda p: p == "openrouter")
    monkeypatch.setattr(
        account_usage, "_fetch_openrouter_account_usage", lambda *_a, **_k: None
    )

    lane = harness.build_account_usage(timeout=5.0)["lanes"][0]
    assert lane["provider"] == "openrouter"
    assert lane["unavailable_reason"] == "no usage data"


def test_usage_argparse_wires_json_provider_timeout(monkeypatch):
    """The real parser build routes `harness usage --json --provider X --timeout N`
    to _cmd_usage with those values."""
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command")
    harness.build_parser(subs)

    args = parser.parse_args(
        ["harness", "usage", "--json", "--provider", "openai-codex", "--timeout", "5"]
    )
    assert args.func is harness._cmd_usage
    assert args.json is True
    assert args.provider == "openai-codex"
    assert args.timeout == 5.0


def test_usage_lane_slower_than_deadline_times_out_bounded(monkeypatch):
    """A lane fetch that sleeps past the wall-clock deadline degrades to an
    unavailable lane (TimeoutError reason) without blocking the fast lanes or the
    overall command."""
    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: None)
    monkeypatch.setattr(
        harness, "_usage_lane_detected", lambda p: p in {"openai-codex", "anthropic"}
    )

    def fake_fetch(provider_id):
        if provider_id == "anthropic":
            time.sleep(0.5)  # far slower than the 0.15s deadline
            return AccountUsageSnapshot(
                provider="anthropic",
                source="usage_api",
                fetched_at=datetime.now(timezone.utc),
                windows=(AccountUsageWindow(label="Weekly", used_percent=10.0),),
            )
        return AccountUsageSnapshot(
            provider="openai-codex",
            source="usage_api",
            fetched_at=datetime.now(timezone.utc),
            windows=(AccountUsageWindow(label="Session", used_percent=5.0),),
        )

    monkeypatch.setattr(harness, "_fetch_usage_lane", fake_fetch)

    started = time.monotonic()
    payload = harness.build_account_usage(timeout=0.15)
    elapsed = time.monotonic() - started

    lanes = {lane["provider"]: lane for lane in payload["lanes"]}
    assert set(lanes) == {"openai-codex", "anthropic"}
    # Fast lane unaffected.
    assert lanes["openai-codex"]["available"] is True
    assert lanes["openai-codex"]["windows"][0]["label"] == "Session"
    # Slow lane exceeded the deadline → class-name-only timeout reason.
    assert lanes["anthropic"]["available"] is False
    assert lanes["anthropic"]["unavailable_reason"] == "usage fetch failed (TimeoutError)"
    # Wall clock bounded well under the slow fetch (0.5s) and far under
    # sleep × lane count (1.0s): the deadline short-circuits the hung lane.
    assert elapsed < 0.4
