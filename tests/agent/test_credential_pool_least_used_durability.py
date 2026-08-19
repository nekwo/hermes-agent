"""MCF-45: ``least_used`` distributes across PROCESSES, not only inside one.

``_select_unlocked`` picked ``min(available, key=request_count)`` and incremented
through ``_replace_entry`` — a pure in-memory list swap. Nothing on that path
called ``_persist``; the only write on the whole selection path was round-robin's.
``load_pool()`` rebuilds the pool from disk on every call, so the counter reached
disk on **no path at all**: every fresh process re-derived the same "least used"
entry and the strategy was inert across processes while looking perfectly correct
inside one of them. Same class as MCF-13 — state that never reaches the place
that reads it.

**What was NOT broken, and is pinned here so a later change cannot "fix" it into
a regression.** The escalation's parenthetical said pools are not cached. They
are: the agent runtime holds one pool per agent object for the life of the
process (``agent._credential_pool``), which predates the row. So a long-lived
process ALREADY distributed, and gate 3 keeps that true. The row's title — the
cross-process claim — is the part that was exactly right, and gate 1 is that.

Gate 4 covers the merge rule the fix introduces, and gate 5 the boundary the fix
must not cross: a durable counter must not be paid for by rewriting credentials.

CREDENTIAL HYGIENE: every token here is an inert marker string under pytest's
per-test ``HERMES_HOME`` tempdir, and gate 5 asserts the sidecar holds no token
material.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest


PROVIDER = "deepseek"
MARKER_A = "inert-marker-alpha-not-a-credential"
MARKER_B = "inert-marker-bravo-not-a-credential"


def _entry(idx: int, marker: str, *, request_count: int = 0) -> dict:
    return {
        "id": f"cred-{idx}",
        "label": f"slot-{idx}",
        "auth_type": "api_key",
        "priority": idx,
        "source": "manual",
        "access_token": marker,
        "request_count": request_count,
    }


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A sandboxed home on ``least_used``, with host discovery held off."""

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(
        "agent.credential_pool.get_pool_strategy", lambda _provider: "least_used"
    )
    monkeypatch.setattr(
        "agent.credential_pool._seed_from_singletons",
        lambda provider, entries: (False, set()),
    )
    monkeypatch.setattr(
        "agent.credential_pool._seed_from_env",
        lambda provider, entries: (False, set()),
    )
    monkeypatch.setattr(
        "agent.credential_pool._prune_stale_seeded_entries",
        lambda *args, **kwargs: False,
    )
    return hermes_home


def _seed(home: Path, entries) -> Path:
    auth_path = home / "auth.json"
    auth_path.write_text(
        json.dumps({"version": 1, "credential_pool": {PROVIDER: entries}}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return auth_path


def _rotation_path(home: Path) -> Path:
    return home / "credential_rotation.json"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _selected_id(entry) -> str:
    """Ids only into assertions — a ``PooledCredential`` repr carries tokens."""

    assert entry is not None, "the pool returned no credential at all"
    return str(entry.id)


def _select_in_a_fresh_process(count: int) -> list[str]:
    """``count`` selections, each through its OWN ``load_pool``.

    That is what a separate process does: ``load_pool`` rebuilds from disk every
    call, so a pool object per selection is the honest simulation of N processes
    each resolving a runtime provider once.
    """
    from agent.credential_pool import load_pool

    return [_selected_id(load_pool(PROVIDER).select()) for _ in range(count)]


def _durable_counts(home: Path) -> dict:
    raw = json.loads(_rotation_path(home).read_text(encoding="utf-8"))
    return raw["providers"][PROVIDER]["request_counts"]


# -- Gate 1 ------------------------------------------------------------------


def test_the_usage_counter_reaches_disk_and_distributes_across_processes(home):
    """Four one-shot processes must not all pick the same credential.

    Killing mutation: drop the ``write_pool_rotation_cursor`` call from the
    ``least_used`` branch — the counter goes back to reaching disk on no path,
    every fresh load re-derives the same minimum, and all four picks are
    ``cred-0``.
    """
    _seed(home, [_entry(0, MARKER_A), _entry(1, MARKER_B)])

    picks = _select_in_a_fresh_process(4)

    assert picks == ["cred-0", "cred-1", "cred-0", "cred-1"], (
        "least_used did not distribute across fresh pool loads: the usage "
        f"counter is not reaching disk (picks={picks})"
    )


# -- Gate 2 ------------------------------------------------------------------


def test_a_preexisting_row_count_is_still_honoured_before_any_sidecar_exists(home):
    """The persisted ``request_count`` on the rows is not thrown away.

    A store that has been running on ``least_used`` carries counts in
    ``auth.json``; the first selection after this change must start from those,
    not from zero, or the busiest key looks idle exactly once per install.

    Killing mutation: seed the in-memory counters from the sidecar ALONE
    (ignore ``entry.request_count`` in ``_sync_usage_counts_from_store``) — the
    heavily used ``cred-0`` is picked first.
    """
    _seed(
        home,
        [
            _entry(0, MARKER_A, request_count=100),
            _entry(1, MARKER_B, request_count=10),
        ],
    )

    assert _select_in_a_fresh_process(1) == ["cred-1"], (
        "the pre-existing per-row usage counts were discarded, so the busiest "
        "credential is treated as the least used one"
    )


# -- Gate 3 ------------------------------------------------------------------


def test_one_long_lived_pool_still_distributes_within_its_own_process(home):
    """The in-process behaviour that ALREADY worked must keep working.

    The agent runtime caches one pool per agent object, so this is the live
    path for a long-running agent. Pinned because the cross-process fix touches
    the same branch, and "fixing" a defect by regressing the half that was
    correct is the outcome this gate exists to make impossible.

    Killing mutation: drop the ``_replace_entry`` in-memory increment and rely
    on the sidecar alone — the second call re-picks ``cred-0``.
    """
    from agent.credential_pool import load_pool

    _seed(home, [_entry(0, MARKER_A), _entry(1, MARKER_B)])
    pool = load_pool(PROVIDER)

    picks = [_selected_id(pool.select()) for _ in range(4)]

    assert picks == ["cred-0", "cred-1", "cred-0", "cred-1"], (
        f"one cached pool stopped distributing within its own process: {picks}"
    )


# -- Gate 4 ------------------------------------------------------------------


def test_a_sibling_process_count_is_merged_by_max_not_overwritten(home):
    """A long-lived pool must see a sibling's usage, and must not lose its own.

    Both halves in one case because they are one rule: the merge is by MAX.
    Left as assignment-from-disk, a cached pool would forget its own unflushed
    increments; left as memory-wins, it would never see the sibling at all —
    which is the original defect, one layer in.

    Killing mutation: replace the ``seen > entry.request_count`` merge in
    ``_sync_usage_counts_from_store`` with an unconditional assignment from the
    sidecar, or delete the re-read entirely. The first loses this pool's own
    count for ``cred-0``; the second ignores the sibling's for ``cred-1``.
    """
    from agent.credential_pool import load_pool
    from hermes_cli.auth import write_pool_rotation_state

    _seed(home, [_entry(0, MARKER_A), _entry(1, MARKER_B)])
    pool = load_pool(PROVIDER)
    assert _selected_id(pool.select()) == "cred-0"  # this pool's own usage: 1

    # A sibling process hammered cred-1 while this pool object stayed alive.
    write_pool_rotation_state(PROVIDER, {"request_counts": {"cred-1": 50}})

    assert _selected_id(pool.select()) == "cred-0", (
        "the cached pool did not see the sibling's usage and kept alternating "
        "onto a credential another process has used 50 times"
    )
    assert _durable_counts(home)["cred-0"] == 2, (
        "the sidecar re-read overwrote this pool's own in-flight count instead "
        "of merging by max"
    )


# -- Gate 5 ------------------------------------------------------------------


def test_the_durable_counter_costs_no_credential_write(home):
    """A counter is selection state; persisting it must rewrite no credential.

    This is MCF-44's boundary applied to MCF-45's fix, and it is the reason the
    counter did not simply start calling ``_persist()``: that would have made
    every request rewrite the whole credential store and moved ``auth.json`` on
    a quiescent store all over again (MCF-16).

    Killing mutation: persist the counter with ``self._persist()`` instead of
    ``write_pool_rotation_cursor`` — the digest moves and the marker appears in
    the store the counter is written to.
    """
    auth_path = _seed(home, [_entry(0, MARKER_A), _entry(1, MARKER_B)])
    from agent.credential_pool import load_pool

    load_pool(PROVIDER)  # let any load-time normalization settle
    before = _digest(auth_path)

    picks = _select_in_a_fresh_process(4)

    assert _digest(auth_path) == before, (
        "persisting the usage counter rewrote the credential store: advancing a "
        "counter must not be a full-store credential write"
    )
    assert picks == ["cred-0", "cred-1", "cred-0", "cred-1"], (
        f"anti-vacuity: nothing was counted at all, so no write was owed ({picks})"
    )
    raw = _rotation_path(home).read_text(encoding="utf-8")
    assert MARKER_A not in raw and MARKER_B not in raw, (
        "the rotation sidecar contains credential material; it may hold only "
        "ids and integers"
    )
