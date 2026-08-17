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

import pytest

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


# --- EG-0.3: the fall-through is REAPED, and cannot come back quietly ---------
#
# `_fetch_usage_lane` used to end with `return fetch_account_usage(provider_id)`
# for any id its four arms did not match. EG-0.2 §3.2 proved that arm dead (the
# id producer only filters a closed tuple, and an unknown `--provider` returns
# from `_cmd_usage` before dispatch) — and also proved it dangerous: it was the
# last route back into `agent/account_usage.py`'s blanket
# `except Exception: return None`, i.e. the exact defect the S1 tests above
# exist to fence. A fifth provider added to `_USAGE_LANE_PROVIDERS` without its
# fetcher would have re-armed it silently.
#
# The two witnesses below are the pair Plan EG §2 row 2 specifies. They are
# deliberately different in kind: (a) probes the REPLACEMENT (what a fifth
# provider gets today), (b) probes the ABSENCE (nothing routes into the swallow
# on the happy path). Either alone can pass over a partially-restored
# fall-through.


def _usage_stub_snapshot(provider):
    return AccountUsageSnapshot(
        provider=provider,
        source="usage_api",
        fetched_at=datetime(2026, 8, 17, 9, 0, 0, tzinfo=timezone.utc),
        windows=(AccountUsageWindow(label="Session", used_percent=7.0),),
    )


def test_usage_lane_with_no_fetcher_raises_typed_failure_naming_the_provider():
    """WITNESS (a). An id outside `_USAGE_LANE_PROVIDERS` is a LOUD typed error.

    Pinned at both ends, because only the pair is honest: the raise itself (a
    caller other than `_fetch_usage_lanes` must be able to branch on the TYPE,
    not parse a string), and the operator-facing reason it becomes (the id has to
    survive `_usage_failure_reason`'s class-name-only discipline, which is the
    half a plain `ValueError` would have lost).

    MUTATION (proves non-vacuity): restore
    ``return fetch_account_usage(provider_id)`` as the terminal arm. Upstream
    normalizes the unknown id and returns None WITHOUT raising, so nothing is
    raised at all, the lane degrades to the unfalsifiable ``no usage data``, and
    both halves go red. The probed field is written by the mutated path too — it
    just writes a provably different string.
    """
    with pytest.raises(harness.UnknownUsageLaneError) as excinfo:
        harness._fetch_usage_lane("nous-v2")
    assert excinfo.value.provider_id == "nous-v2"

    lanes = harness._fetch_usage_lanes(
        ["nous-v2"], active_provider=None, timeout=5.0
    )
    assert len(lanes) == 1
    lane = lanes[0]
    assert lane["provider"] == "nous-v2"
    assert lane["available"] is False
    assert lane["unavailable_reason"] == (
        "usage fetch failed (UnknownUsageLaneError: nous-v2)"
    )
    # The reason names the id, and nothing else from the exception: the message
    # text ("no account-usage fetcher for lane ...") must not leak in, because
    # the exemption granted in `_usage_failure_reason` is for `exc.provider_id`
    # alone, not for `str(exc)`.
    assert "no account-usage fetcher" not in lane["unavailable_reason"]


def test_no_usage_lane_routes_through_fetch_account_usage(monkeypatch):
    """WITNESS (b). ZERO `fetch_account_usage` calls across a FULL four-lane run.

    Every per-provider fetcher is stubbed, so no network is touched and every
    lane succeeds — which is the point: the swallow re-entry has to be absent on
    the path that WORKS, not only on an error path. The recorder is installed on
    `agent.account_usage`, the module `_fetch_usage_lane`'s function-local
    imports resolve against, so a restored import binds the recorder.

    MUTATION: point ANY one arm at the wrapper (e.g. make the openrouter arm
    ``return fetch_account_usage(provider_id)``). The recorder logs the call and
    the count assertion goes red; the lane also collapses to ``no usage data``
    because upstream's `_fetch_openrouter_account_usage` is stubbed to a snapshot
    the wrapper's own normalization never reaches. Removing the terminal arm's
    id from `_USAGE_LANE_PROVIDERS` cannot make this pass vacuously either — the
    candidate list is asserted to be all four.
    """
    import agent.account_usage as account_usage
    import hermes_cli.nous_account as nous_account

    swallow_calls: list[tuple] = []

    def recorder(*args, **kwargs):
        swallow_calls.append((args, kwargs))
        return None

    monkeypatch.setattr(account_usage, "fetch_account_usage", recorder)
    monkeypatch.setattr(
        account_usage,
        "_fetch_codex_account_usage",
        lambda *_a, **_k: _usage_stub_snapshot("openai-codex"),
    )
    monkeypatch.setattr(
        account_usage,
        "_fetch_anthropic_account_usage",
        lambda *_a, **_k: _usage_stub_snapshot("anthropic"),
    )
    monkeypatch.setattr(
        account_usage,
        "_fetch_openrouter_account_usage",
        lambda *_a, **_k: _usage_stub_snapshot("openrouter"),
    )
    monkeypatch.setattr(
        account_usage,
        "build_nous_credits_snapshot",
        lambda _account: _usage_stub_snapshot("nous"),
    )
    monkeypatch.setattr(
        nous_account,
        "get_nous_portal_account_info",
        lambda *_a, **_k: {"credits": 1},
    )

    candidates = list(harness._USAGE_LANE_PROVIDERS)
    assert candidates == ["openai-codex", "anthropic", "openrouter", "nous"]

    lanes = harness._fetch_usage_lanes(
        candidates, active_provider=None, timeout=5.0
    )

    assert swallow_calls == [], (
        "a usage lane routed back into agent.account_usage.fetch_account_usage, "
        "whose blanket `except Exception: return None` erases the failure class "
        f"EG-0.3 reaped this route to preserve. Calls: {swallow_calls}"
    )
    # Non-vacuity for the count: the run really did fetch all four lanes.
    assert [lane["provider"] for lane in lanes] == candidates
    assert all(lane["available"] is True for lane in lanes), lanes


# --- EG-6.1: the DETECTION half stops deleting lanes, and the envelope stops --
# --- claiming an auth state it never observed --------------------------------
#
# S1 fixed the fetch half: a raised fetch is reported by class instead of
# collapsing to "no usage data". Its two siblings survived on the same surface:
#
#  * `_usage_lane_detected` ended in `except Exception: return False`, and the
#    docstring's rule is that undetected lanes are OMITTED — so a detector fault
#    made the row VANISH from the Limits panel. Strictly worse than the defect
#    S1 fixed: there was no row left to carry any reason at all.
#  * `_render_account_usage_human` printed "no signed-in providers detected" on
#    any empty lane list. That is a POSITIVE CLAIM about the operator's auth
#    state, and it was false whenever detection or fetch had collapsed.
#
# Both halves are pinned as PAIRS below, because a single fixture is passed by a
# blanket mutant in each direction ("always emit the lane" / "never print the
# claim").


class _DetectorExploded(RuntimeError):
    """Injected detector fault #1 — a distinct class, deliberately."""


class _DetectorTimedOut(TimeoutError):
    """Injected detector fault #2."""


@pytest.mark.parametrize("error_class", [_DetectorExploded, _DetectorTimedOut])
def test_a_raising_detector_emits_the_lane_with_its_class(monkeypatch, error_class):
    """Fixture (a) of the pair: detection RAISED for anthropic.

    The lane is EMITTED, unavailable, naming the exception class — and the
    healthy sibling lane is untouched, so the isolation is per lane exactly as
    it already is on the fetch half.

    MUTATION (proves non-vacuity): restore `_usage_lane_detected`'s
    `except Exception: return False`. The anthropic lane disappears from
    `lanes` and both the membership and the reason assertions go red.

    Driven with two distinct classes so a hardcoded reason string cannot pass.
    The injected message is token- and URL-shaped: the reason carries the class
    NAME only.
    """

    def fake_detect(provider_id):
        if provider_id == "anthropic":
            raise error_class("Bearer sk-LEAKME via https://example.invalid/whoami")
        return provider_id == "openai-codex"

    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(harness, "_usage_lane_detected", fake_detect)
    monkeypatch.setattr(harness, "_fetch_usage_lane", lambda p: _snapshot(p))

    payload = harness.build_account_usage(timeout=5.0)
    lanes = {lane["provider"]: lane for lane in payload["lanes"]}

    assert set(lanes) == {"openai-codex", "anthropic"}
    failed = lanes["anthropic"]
    assert failed["available"] is False
    assert failed["unavailable_reason"] == f"usage detection failed ({error_class.__name__})"
    # The grammar is the fetch half's, one phase word over — NOT a second idiom.
    assert "usage fetch failed" not in failed["unavailable_reason"]
    assert failed["windows"] == []
    assert failed["details"] == []
    # Emission order is still `_USAGE_LANE_PROVIDERS` order: the failed lane
    # holds its natural slot rather than being appended after the healthy ones.
    assert [lane["provider"] for lane in payload["lanes"]] == [
        "openai-codex",
        "anthropic",
    ]
    # A detector fault is not an envelope-level degrade: lanes were collected.
    assert "degraded" not in payload

    serialized = repr(payload)
    assert "sk-LEAKME" not in serialized
    assert "https://" not in serialized


@pytest.mark.parametrize(
    ("provider", "module_path", "probe", "error_class"),
    [
        (
            "anthropic",
            "agent.anthropic_adapter",
            "resolve_anthropic_token",
            _DetectorExploded,
        ),
        (
            "nous",
            "hermes_cli.auth",
            "get_provider_auth_state",
            _DetectorTimedOut,
        ),
    ],
)
def test_a_raising_REAL_detector_emits_the_lane_not_nothing(
    monkeypatch, provider, module_path, probe, error_class
):
    """The witness that watches the lane that RUNS.

    Written second, and deliberately: the sibling tests above stub
    `harness._usage_lane_detected`, and the mutation campaign proved that a
    restored `except Exception: return False` INSIDE that function survives all
    of them — the RD-L2 lesson, reproduced live. A test that replaces the seam
    holding the defect cannot see the defect.

    So this one patches the seam BELOW — the real per-provider probe
    `_usage_lane_detected` calls — exactly the discipline the S1 tests adopted
    when they refused to stub `_fetch_usage_lane`. `--provider` narrows the scope
    to the one lane, so no other detector and no fetcher runs (no network).

    MUTATION (proves non-vacuity): restore `_usage_lane_detected`'s
    `except Exception: return False`. The lane is OMITTED, `lanes` is empty, and
    the membership assertion goes red. THIS is the mutation the stage exists to
    kill.

    Two providers × two classes, through the two inline detector arms; the two
    feeder arms are fenced by
    `test_the_detector_feeders_raise_instead_of_answering_not_signed_in`.
    """
    import importlib

    module = importlib.import_module(module_path)

    def boom(*_a, **_k):
        raise error_class("Bearer sk-LEAKME probing https://example.invalid")

    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: None)
    monkeypatch.setattr(module, probe, boom)

    payload = harness.build_account_usage(only_provider=provider, timeout=5.0)

    assert [lane["provider"] for lane in payload["lanes"]] == [provider], (
        "a detector that RAISED deleted its own lane — the pre-EG-6.1 defect, "
        "strictly worse than S1's 'no usage data' because no row survives to "
        f"carry a reason. payload={payload}"
    )
    lane = payload["lanes"][0]
    assert lane["available"] is False
    assert lane["unavailable_reason"] == (
        f"usage detection failed ({error_class.__name__})"
    )
    serialized = repr(payload)
    assert "sk-LEAKME" not in serialized
    assert "https://" not in serialized


def test_absent_credentials_still_omit_the_lane(monkeypatch):
    """Fixture (b) of the pair: detection RETURNED False for anthropic.

    Omission is now the EXCLUSIVE meaning of "credentials genuinely absent".

    MUTATION (kill): emit a lane for every provider in `_USAGE_LANE_PROVIDERS`
    regardless of detection (the blanket "always emit" mutant that passes
    fixture (a)) — the membership equality goes red. The pair is what pins the
    discriminator on the detector's RAISE-vs-False, and neither blanket mutant
    passes both.
    """
    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(
        harness, "_usage_lane_detected", lambda p: p == "openai-codex"
    )
    monkeypatch.setattr(harness, "_fetch_usage_lane", lambda p: _snapshot(p))

    payload = harness.build_account_usage(timeout=5.0)

    assert [lane["provider"] for lane in payload["lanes"]] == ["openai-codex"]
    assert "degraded" not in payload


def test_a_detector_fault_does_not_suppress_the_no_providers_claim(monkeypatch):
    """Every detector raised: lanes are all present-and-named, so the claim
    branch is never reached at all.

    This is the third leg the two-fixture pair cannot cover: it proves the fix
    is "emit the lane", not "stop rendering" — the operator still gets four rows
    to read reasons off.
    """

    def fake_detect(provider_id):
        raise _DetectorExploded("every detector is broken")

    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: None)
    monkeypatch.setattr(harness, "_usage_lane_detected", fake_detect)

    payload = harness.build_account_usage(timeout=5.0)
    assert [lane["provider"] for lane in payload["lanes"]] == list(
        harness._USAGE_LANE_PROVIDERS
    )
    assert {lane["unavailable_reason"] for lane in payload["lanes"]} == {
        "usage detection failed (_DetectorExploded)"
    }

    harness._render_account_usage_human(payload)


@pytest.mark.parametrize("error_class", [_DetectorExploded, _DetectorTimedOut])
def test_a_degraded_envelope_never_prints_the_no_providers_claim(
    monkeypatch, capsys, error_class
):
    """Fixture (a) of the claim pair: the whole detection SCAN collapsed.

    `_detect_usage_candidates` itself raises (not one provider's detector — the
    scan), so there are no lanes to emit and nothing is known about the
    operator's auth state. The claim line must be ABSENT and the degrade line
    present, naming the class.

    MUTATION (proves non-vacuity): keep the old `except Exception: return
    payload` with no stamp. `degraded` is never written, `_usage_lanes_suppressed`
    returns None, the claim prints — and both the absence assertion and the
    degrade-line assertion go red.

    Two distinct injected classes, so the degrade line cannot be a constant.
    """

    def fake_detect(_only_provider):
        raise error_class("Bearer sk-LEAKME scanning https://example.invalid")

    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: "openai-codex")
    monkeypatch.setattr(harness, "_detect_usage_candidates", fake_detect)

    payload = harness.build_account_usage(timeout=5.0)
    assert payload["lanes"] == []
    assert payload["degraded"] == {"detect": error_class.__name__}

    harness._render_account_usage_human(payload)
    out = capsys.readouterr().out
    assert "no signed-in providers detected" not in out
    assert f"usage lanes unavailable ({error_class.__name__})" in out
    assert "sk-LEAKME" not in out
    assert "https://" not in out


def test_a_genuinely_empty_scan_still_prints_the_no_providers_claim(
    monkeypatch, capsys
):
    """Fixture (b) of the claim pair: detection RAN and found none.

    MUTATION (kill): drop the claim entirely, or gate it on something other than
    the degrade (e.g. never print it) — red. The pair pins the discriminator on
    `degraded`; a mutant that always prints fails fixture (a) and a mutant that
    never prints fails this one.

    Anti-vacuity: `degraded` is asserted ABSENT, so this fixture is provably the
    honest-empty case and not a second copy of (a).
    """
    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: None)
    monkeypatch.setattr(harness, "_usage_lane_detected", lambda p: False)

    payload = harness.build_account_usage(timeout=5.0)
    assert payload["lanes"] == []
    assert "degraded" not in payload

    harness._render_account_usage_human(payload)
    out = capsys.readouterr().out
    assert "no signed-in providers detected" in out
    assert "usage lanes unavailable" not in out


def test_a_failed_fetch_scan_degrades_and_states_it(monkeypatch, capsys):
    """The second lane-suppressing seam: `_fetch_usage_lanes` raises WHOLESALE
    (not per lane — its own per-lane handler is byte-unchanged and pinned above).

    MUTATION (kill): keep `payload["lanes"] = []` with no stamp — the envelope
    reports zero lanes right after detecting two of them, the claim prints, red.
    """

    def boom(*_a, **_k):
        raise _DetectorTimedOut("the whole fetch pool broke")

    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: None)
    monkeypatch.setattr(
        harness, "_usage_lane_detected", lambda p: p in {"openai-codex", "anthropic"}
    )
    monkeypatch.setattr(harness, "_fetch_usage_lanes", boom)

    payload = harness.build_account_usage(timeout=5.0)
    assert payload["lanes"] == []
    assert payload["degraded"] == {"fetch": "_DetectorTimedOut"}

    harness._render_account_usage_human(payload)
    out = capsys.readouterr().out
    assert "no signed-in providers detected" not in out
    assert "usage lanes unavailable (_DetectorTimedOut)" in out


def test_a_failed_active_provider_resolve_is_named_and_does_not_eat_the_claim(
    monkeypatch, capsys
):
    """The third seam, and the reason `degraded` is a per-stage MAP rather than
    one scalar: a failed `active_provider` resolve suppresses NO lanes.

    So an envelope degraded only here may still carry an honest empty lane list,
    and the claim stays legitimate — while `active_provider: null` stops meaning
    both "none selected" and "the resolver threw".

    MUTATION (kill): treat any `degraded` as lane-suppressing (drop the stage
    filter in `_usage_lanes_suppressed`) — the claim vanishes and this goes red.
    The reverse mutant (never stamp the resolve seam) reds the `degraded`
    assertion. Together they pin the stage keying.
    """

    def boom():
        raise _DetectorExploded("provider resolution broke")

    monkeypatch.setattr(harness, "_resolve_active_provider_id", boom)
    monkeypatch.setattr(harness, "_usage_lane_detected", lambda p: False)

    payload = harness.build_account_usage(timeout=5.0)
    assert payload["active_provider"] is None
    assert payload["degraded"] == {"active_provider": "_DetectorExploded"}
    assert payload["lanes"] == []

    harness._render_account_usage_human(payload)
    out = capsys.readouterr().out
    # Detection ran and found none, so the claim is TRUE and must still print.
    assert "no signed-in providers detected" in out
    assert "usage lanes unavailable" not in out
    # And the null active provider names its cause instead of rendering "(none)",
    # which would itself be a claim about the operator's configuration.
    assert "resolution failed (_DetectorExploded)" in out
    assert "Active provider: (none)" not in out


def test_the_serialization_fallback_envelope_names_the_failure(monkeypatch, capsys):
    """`_emit_usage_json`'s last-resort envelope reports zero lanes at the exact
    moment it knows the real payload could not be written.

    MUTATION (kill): drop the `("serialize", exc)` argument — the fallback is
    again an unlabeled empty envelope, indistinguishable on the wire from an
    idle account, and the `degraded` assertion goes red.

    The sibling pin `test_cmd_usage_json_branch_isolates_serialization_failure`
    is byte-unchanged and keeps the exit-0 / valid-JSON contract.
    """
    monkeypatch.setattr(harness, "_resolve_active_provider_id", lambda: None)
    monkeypatch.setattr(harness, "_usage_lane_detected", lambda p: False)

    def boom(_payload):
        raise _DetectorExploded("Bearer sk-LEAKME could not be serialized")

    monkeypatch.setattr(harness, "emit_json", boom)

    rc = harness._cmd_usage(SimpleNamespace(json=True, provider=None, timeout=5.0))
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["schema"] == "hermes.account_usage/v1"
    assert data["lanes"] == []
    assert data["degraded"] == {"serialize": "_DetectorExploded"}
    assert "sk-LEAKME" not in out


def test_the_build_fallback_envelope_names_the_failure(monkeypatch, capsys):
    """The same for `_cmd_usage`'s outermost guard.

    MUTATION (kill): drop the `("build", exc)` argument — red. Defence in depth
    (`build_account_usage` isolates internally), but this arm is the one that
    fires when that isolation is itself broken, which is the worst moment to
    emit a silent empty envelope.
    """

    def boom(**_kwargs):
        raise _DetectorTimedOut("build broke")

    monkeypatch.setattr(harness, "build_account_usage", boom)

    rc = harness._cmd_usage(SimpleNamespace(json=True, provider=None, timeout=5.0))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["lanes"] == []
    assert data["degraded"] == {"build": "_DetectorTimedOut"}


def test_the_detector_feeders_raise_instead_of_answering_not_signed_in(monkeypatch):
    """The two OR-of-two-sources feeders had their own terminal
    `except Exception: return False`, which would have made the lane-emission fix
    above VACUOUS for half the providers: `_usage_lane_detected` can only raise
    if its feeders do.

    Two fixtures per feeder, because the OR is the subtlety:
      * primary raises, secondary AFFIRMS  → True (the yes is the truth);
      * primary raises, secondary declines → RAISE (we could not tell).

    MUTATION (kill): restore either terminal `except Exception: return False` —
    the raise fixtures go red while the True fixtures still pass, which is
    exactly the half-fix this pair exists to catch.
    """
    import agent.credential_pool as credential_pool
    import hermes_cli.auth as auth
    import hermes_cli.runtime_provider as runtime_provider

    def _pool(entries):
        return SimpleNamespace(entries=lambda: entries)

    # --- codex: OAuth status probe raises -----------------------------------
    def status_boom():
        raise _DetectorExploded("codex oauth status broke")

    monkeypatch.setattr(auth, "get_codex_auth_status", status_boom)
    monkeypatch.setattr(credential_pool, "load_pool", lambda _p: _pool(["entry"]))
    assert harness._codex_usage_login_detected() is True

    monkeypatch.setattr(credential_pool, "load_pool", lambda _p: _pool([]))
    with pytest.raises(_DetectorExploded):
        harness._codex_usage_login_detected()

    # --- openrouter: pool read raises ---------------------------------------
    def pool_boom(_provider):
        raise _DetectorTimedOut("openrouter pool read broke")

    monkeypatch.setattr(credential_pool, "load_pool", pool_boom)
    monkeypatch.setattr(
        runtime_provider, "resolve_runtime_provider", lambda **_k: {"api_key": "k" * 8}
    )
    assert harness._openrouter_usage_login_detected() is True

    monkeypatch.setattr(
        runtime_provider, "resolve_runtime_provider", lambda **_k: {"api_key": ""}
    )
    with pytest.raises(_DetectorTimedOut):
        harness._openrouter_usage_login_detected()
