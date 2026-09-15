"""Codex readiness answers from the RUN PATH's credential sources, not the pool alone.

THE FIELD STATE THIS REPRODUCES (2026-09-08, profile ``neko``, persona role
``alice_supervisor``). The launcher's Mission Control office card carried an
amber "Provider credential attention req…" line — ``persona.readinessSummary``,
rendered verbatim from hermes' ``agents_readiness`` snapshot section — while
every one of that agent's chat turns succeeded. From the profile's own
``agent.log``::

    16:46:02 agent.credential_pool: credential pool: no available entries
                                    (all exhausted or empty)
    16:46:06 agent.conversation_loop: API call #1: model=gpt-5.6-luna
                                    provider=openai-codex ... latency=4.5s
    (again at 16:47:44 / 16:47:47)

Two authorities disagreeing, not a race. ``profile_readiness`` asked
``load_pool("openai-codex").peek()`` and NOTHING else; the turn was served by
``resolve_codex_runtime_credentials()`` from the singleton token set in
``providers.openai-codex.tokens``, which the readiness pass never looked at. The
store held exactly one pool entry — ``source: "manual:device_code"``,
``last_status: "exhausted"`` inside its default one-hour cooldown, its own
access token months past expiry — beside a singleton whose access token was
valid for another five days. So ``peek()`` returned ``None`` and the card went
amber for an agent with a working credential.

WHAT EACH TEST BELOW PINS, and the mutation that kills it, is written at the
test. The pair that matters is the first two: an absence assertion (no issue)
and its POSITIVE CONTROL — the same fixture with the singleton removed, where
the attention line MUST appear. Narrowing a false positive is only a fix if the
true positive survives, and a green "no issue" looks identical whether the new
auth-store lane answered or the whole readiness call quietly stopped happening.

CREDENTIAL HYGIENE: every token in this module is an inert marker string
written under pytest's per-test ``HERMES_HOME`` tempdir (the ``_isolate_hermes_
home`` autouse fixture in ``tests/conftest.py``). Nothing here is, resembles, or
is derived from real credential material, and no assertion in this module puts
store content in a failure message.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from agent_runtime.models import AgentPersona


PROVIDER = "openai-codex"
MODEL = "gpt-5.6-luna"

#: Inert markers. The production reads care only that a string is non-empty.
POOL_STALE_ACCESS = "inert-marker-pool-access-not-a-credential"
POOL_STALE_REFRESH = "inert-marker-pool-refresh-not-a-credential"
SINGLETON_ACCESS = "inert-marker-singleton-access-not-a-credential"
SINGLETON_REFRESH = "inert-marker-singleton-refresh-not-a-credential"

ATTENTION_SUMMARY = "Provider credential attention required"


def _hermes_home() -> Path:
    return Path(os.environ["HERMES_HOME"])


def _auth_path() -> Path:
    return _hermes_home() / "auth.json"


def _cooling_pool_entry(*, access_token: str = POOL_STALE_ACCESS) -> dict:
    """The field entry: exhausted, ``manual:device_code``, inside its cooldown.

    ``last_error_reset_at`` is null exactly as it was on disk, so the cooldown
    is derived from ``last_status_at + EXHAUSTED_TTL_DEFAULT_SECONDS`` (one
    hour in the original fork). Upstream shortens sole-credential cooldowns;
    stamp exhaustion now to keep this fixture inside the actual cooldown, and
    ``_available_entries`` skips it, so ``peek()`` answers ``None``.

    The source string is the field's, not ``device_code``: the codex re-auth
    resync in ``_available_entries`` is gated on the bare ``device_code``
    spelling and therefore never runs for this row. That prefix mismatch is a
    SEPARATE defect (it is what strands the row as permanently stale); it is
    reproduced here only because the field state has it.
    """

    return {
        "id": "poolslot",
        "label": "pool-slot",
        "auth_type": "oauth",
        "priority": 1,
        "source": "manual:device_code",
        "access_token": access_token,
        "refresh_token": POOL_STALE_REFRESH,
        "last_status": "exhausted",
        "last_status_at": time.time(),
        "last_error_code": None,
        "last_error_reset_at": None,
    }


def _singleton() -> dict:
    return {
        "tokens": {
            "access_token": SINGLETON_ACCESS,
            "refresh_token": SINGLETON_REFRESH,
        },
        "last_refresh": "2026-09-03T14:23:54.454502Z",
    }


def _seed(*, pool: list[dict], singleton: dict | None) -> Path:
    """Write a codex auth store under the sandboxed home.

    ``suppressed_sources`` carries the field's marker, and it is load-bearing
    rather than decorative: without it ``_seed_from_singletons`` would
    materialise a fresh ``device_code`` pool row from the singleton on the next
    ``load_pool()``, the pool would have an available entry, and every test here
    would pass through the pool lane while claiming to measure the other one.
    """

    home = _hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "version": 1,
        "credential_pool": {PROVIDER: list(pool)},
        "suppressed_sources": {PROVIDER: ["device_code"]},
    }
    if singleton is not None:
        payload["providers"] = {PROVIDER: singleton}
    auth_path = _auth_path()
    auth_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return auth_path


def _persona() -> AgentPersona:
    # ``hermes_profile`` unset ⇒ the binding carries no profile home, so the
    # readiness pass reads the ambient (sandboxed) home rather than diverting
    # into a per-profile one. Same provider branch, one less moving part.
    return AgentPersona(
        id="alice-supervisor",
        display_name="Neko Mission Lead",
        role="alice_supervisor",
        model=MODEL,
        provider=PROVIDER,
        api_mode=None,
        toolsets=[],
        system_prompt_path="personas/alice/system.md",
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def _clear_provider_issue_memo():
    from agent_runtime import profile_readiness

    profile_readiness._provider_issue_cache_clear()
    yield
    profile_readiness._provider_issue_cache_clear()


def _count_auth_store_reads(monkeypatch) -> dict[str, int]:
    """Count the auth-store lane on the real module, preserving its answer.

    The anti-vacuity instrument for every assertion below: "no issue" and "the
    store did not move" are both trivially true for a readiness pass that never
    reached a credential at all.
    """

    from hermes_cli import auth as auth_mod

    counts = {"auth_store": 0}
    real = auth_mod.codex_auth_store_credentials_present

    def counted() -> bool:
        counts["auth_store"] += 1
        return real()

    monkeypatch.setattr(auth_mod, "codex_auth_store_credentials_present", counted)
    return counts


# ── The defect, and its positive control ────────────────────────────────────


def test_a_cooling_pool_entry_does_not_flag_an_agent_whose_singleton_can_serve(
    monkeypatch,
):
    """The reproduced field state reports READY.

    Killing mutation: restore the pool-only answer — in
    ``profile_readiness._codex_provider_issue`` replace the call with
    ``load_pool("openai-codex").peek()`` and its emptiness check (i.e. the
    body this change removed) — and the amber line comes back on a fixture
    whose singleton can serve the turn.
    """

    from agent.credential_pool import load_pool
    from agent_runtime.profile_readiness import _provider_issue

    _seed(pool=[_cooling_pool_entry()], singleton=_singleton())
    counts = _count_auth_store_reads(monkeypatch)

    issue = _provider_issue(_persona())

    assert issue is None, (
        "readiness flagged an agent whose credential resolves: the card and the "
        f"turn are answering from different authorities again (issue={issue!r})"
    )
    # Anti-vacuity, in two parts. First: the auth-store lane is what answered.
    assert counts == {"auth_store": 1}, (
        "the readiness pass did not consult the auth store exactly once "
        f"(counts={counts}), so 'no issue' proves nothing about the fix"
    )
    # Second: the pool lane genuinely REFUSES on this fixture, so the green
    # above is not the old pool-only path passing for its own reasons.
    assert load_pool(PROVIDER).peek() is None, (
        "the pool handed back an entry, so this fixture no longer reproduces "
        "the field state (a cooling entry the pool will not serve) and the "
        "assertion above is measuring the wrong lane"
    )


def test_the_same_fixture_without_a_singleton_still_reports_attention(monkeypatch):
    """Positive control for the test above: one variable, and the amber MUST appear.

    Identical store, identical cooling pool entry, ``providers`` removed. If
    this does not go amber, the test above is not evidence — it would be green
    for a readiness pass that had stopped reporting anything.

    Killing mutation: make ``codex_auth_store_credentials_present`` return
    ``True`` unconditionally — the attention line disappears for a lane with no
    credential at all.
    """

    from agent_runtime.profile_readiness import (
        READINESS_AUTH_ATTENTION,
        _provider_issue,
    )

    _seed(pool=[_cooling_pool_entry()], singleton=None)
    counts = _count_auth_store_reads(monkeypatch)

    issue = _provider_issue(_persona())

    assert issue == (READINESS_AUTH_ATTENTION, ATTENTION_SUMMARY), (
        "the fix swallowed the TRUE positive: a cooling pool entry with no "
        f"auth-store credential behind it must still ask for attention "
        f"(issue={issue!r})"
    )
    assert counts == {"auth_store": 1}, (
        f"the auth-store lane was not reached (counts={counts}), so the amber "
        "line above came from somewhere other than the lane under test"
    )


def test_a_lane_with_no_credential_anywhere_still_reports_attention(monkeypatch):
    """The genuinely dead lane — empty pool, no singleton — is unchanged.

    Killing mutation: return ``None`` unconditionally from
    ``_codex_provider_issue`` — every codex agent reads ready forever, which is
    the shape a careless fix for the false positive would take.
    """

    from agent_runtime.profile_readiness import (
        READINESS_AUTH_ATTENTION,
        _provider_issue,
    )

    _seed(pool=[], singleton=None)
    counts = _count_auth_store_reads(monkeypatch)

    issue = _provider_issue(_persona())

    assert issue == (READINESS_AUTH_ATTENTION, ATTENTION_SUMMARY), (
        "a codex lane holding no credential in the pool, the profile singleton "
        f"or the global root reported no issue (issue={issue!r})"
    )
    assert counts == {"auth_store": 1}, (
        f"the auth-store lane was not reached (counts={counts})"
    )


# ── Order, and the read-only guarantee that made this branch exist ──────────


def test_a_servable_pool_entry_answers_alone(monkeypatch):
    """The pool stays FIRST, exactly as in the run path — the store is a fallback.

    Not a style point: reversing the order would let a stale singleton mask a
    pool the run path is actually rotating through.

    Killing mutation: swap the two lanes in
    ``codex_credentials_resolvable_read_only`` (ask the auth store first) — the
    count below becomes 1.
    """

    from agent_runtime.profile_readiness import _provider_issue

    healthy = _cooling_pool_entry()
    healthy["last_status"] = None
    healthy["last_status_at"] = None
    _seed(pool=[healthy], singleton=None)
    counts = _count_auth_store_reads(monkeypatch)

    assert _provider_issue(_persona()) is None, (
        "a pool entry the pool itself calls available was reported as needing "
        "attention"
    )
    assert counts == {"auth_store": 0}, (
        "the auth store was consulted for a lane the pool had already answered "
        f"(counts={counts}): the run path's order is not being mirrored"
    )


def test_codex_readiness_leaves_the_credential_store_byte_identical(monkeypatch):
    """MCF-16 survives the new lane: readiness READS credentials, never writes them.

    The reason ``_compute_provider_issue`` has a codex branch at all is that the
    plain resolver refreshes tokens and rewrites ``auth.json`` — a file outside
    the snapshot's declared input closure, so a build that moves it serves a
    real credential change as a false cache HIT. Widening readiness to a second
    credential source must not widen it to a second WRITER.

    Killing mutation: answer the auth-store half by calling
    ``resolve_codex_runtime_credentials()`` instead of the read-only probe (or
    give ``codex_auth_store_credentials_present`` a
    ``suppress_credential_source`` call) — the digest moves.
    """

    from agent_runtime.profile_readiness import _provider_issue

    auth_path = _seed(pool=[_cooling_pool_entry()], singleton=_singleton())
    counts = _count_auth_store_reads(monkeypatch)
    before = _digest(auth_path)

    issue = _provider_issue(_persona())

    assert _digest(auth_path) == before, (
        "the codex readiness pass rewrote the credential store: the snapshot is "
        "perturbing an input it does not declare, so a REAL credential change "
        f"on a quiescent store is served as a false cache HIT (issue={issue!r})"
    )
    assert counts == {"auth_store": 1}, (
        "vacuous gate: the readiness pass never reached the auth store "
        f"(counts={counts}), so 'the store did not move' proves nothing"
    )
