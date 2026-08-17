"""Tests for the launch-time stale-bytecode sweep (checkout fingerprint guard).

Bug class: the checkout's ``.py`` files change (``hermes update``, manual
``git pull``, ZIP update) while ``__pycache__`` retains bytecode compiled
from the previous revision; the next process to import trusts the stale
``.pyc`` and dies with ``cannot import name ...`` (#6207, #60242).

The launch-time guard compares the current checkout fingerprint against the
last-validated stamp and sweeps ``__pycache__`` once when they diverge —
covering paths no update-time clear can reach (manual pulls, pre-hardening
updaters).
"""

import logging
from pathlib import Path

import pytest

from hermes_cli import _boot_clock
from hermes_cli import main as hermes_main


def _make_repo(tmp_path: Path, sha: str = "a" * 40) -> Path:
    """Minimal git checkout layout that _read_git_revision_fingerprint groks."""
    repo = tmp_path / "repo"
    git_dir = repo / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text(sha + "\n", encoding="utf-8")
    return repo


def _make_pycache(repo: Path, subdir: str = "hermes_cli") -> Path:
    cache = repo / subdir / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "main.cpython-311.pyc").write_bytes(b"stale")
    return cache


def test_sweep_clears_pycache_when_checkout_changed(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path, sha="b" * 40)
    cache = _make_pycache(repo)
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", repo)
    # Stamp records a different (older) fingerprint.
    (repo / hermes_main._BYTECODE_FINGERPRINT_FILE).write_text(
        "git:refs/heads/main:" + "a" * 40, encoding="utf-8"
    )

    hermes_main._sweep_stale_bytecode_if_checkout_changed()

    assert not cache.exists()
    # Stamp updated to the current fingerprint.
    recorded = (repo / hermes_main._BYTECODE_FINGERPRINT_FILE).read_text(encoding="utf-8")
    assert recorded.strip().endswith("b" * 40)


# ---------------------------------------------------------------------------
# BW-0: the sweep reports what it cost
#
# The 2026-08-17 cold Mission Control boot had TWO processes each clear ~175
# ``__pycache__`` directories 12 ms apart and then race each other recompiling
# the import set they had just deleted. The log line said how many directories
# went and nothing about how long it took, so the share of that boot's 20.4 s
# import tax owed to this function was pure inference — and one of the plan's
# optimisation stages is aimed at exactly that share.
# ---------------------------------------------------------------------------


@pytest.fixture
def _clean_sweep_anchor():
    _boot_clock.reset_for_tests()
    yield
    _boot_clock.reset_for_tests()


def test_a_sweep_records_its_duration_for_the_boot_frame(
    monkeypatch, tmp_path, caplog, _clean_sweep_anchor
):
    """Anti-vacuity. *Mutation:* drop the ``record_bytecode_sweep_ms`` call.
    *Probed field:* ``_boot_clock.BYTECODE_SWEEP_MS`` is not None afterwards —
    a module global this test cleared itself, which nothing else in the sweep
    writes, so the mutant cannot set it by taking any other branch. Second,
    independent witness: the log line's ``swept_ms=`` token, which lives in a
    different mechanism (the ``logger.info`` format string) and would survive a
    mutation of the recorder, and vice versa.
    """

    repo = _make_repo(tmp_path, sha="c" * 40)
    _make_pycache(repo)
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", repo)
    (repo / hermes_main._BYTECODE_FINGERPRINT_FILE).write_text(
        "git:refs/heads/main:" + "a" * 40, encoding="utf-8"
    )

    assert _boot_clock.BYTECODE_SWEEP_MS is None
    with caplog.at_level(logging.INFO, logger=hermes_main.logger.name):
        hermes_main._sweep_stale_bytecode_if_checkout_changed()

    assert _boot_clock.BYTECODE_SWEEP_MS is not None
    assert _boot_clock.BYTECODE_SWEEP_MS >= 0
    purge_lines = [r.getMessage() for r in caplog.records if "__pycache__" in r.getMessage()]
    assert len(purge_lines) == 1
    assert "swept_ms=" in purge_lines[0]


def test_a_no_op_sweep_still_records_a_duration(
    monkeypatch, tmp_path, _clean_sweep_anchor
):
    """The cheap path is measured too — otherwise every warm boot looks unmeasured.

    Anti-vacuity. *Mutation:* record only inside the ``if removed:`` branch (the
    natural place a first draft puts it). *Probed field:*
    ``BYTECODE_SWEEP_MS`` after a run that takes the ``recorded == fingerprint``
    early return, where ``removed`` is never even computed. "The sweep decided in
    2 ms that it had nothing to do" is an answer; a missing key would be read as
    "nobody measured", and a warm boot is the baseline every cold-boot claim is
    compared against.
    """

    repo = _make_repo(tmp_path, sha="d" * 40)
    cache = _make_pycache(repo)
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", repo)
    # Stamp already matches — the early return.
    (repo / hermes_main._BYTECODE_FINGERPRINT_FILE).write_text(
        "git:refs/heads/main:" + "d" * 40, encoding="utf-8"
    )

    hermes_main._sweep_stale_bytecode_if_checkout_changed()

    assert cache.exists(), "nothing should have been swept"
    assert _boot_clock.BYTECODE_SWEEP_MS is not None


def test_a_non_git_install_still_records_a_duration(
    monkeypatch, tmp_path, _clean_sweep_anchor
):
    """The other early return: no ``.git``, so no fingerprint, so no sweep."""

    repo = tmp_path / "zip-install"
    (repo / "hermes_cli").mkdir(parents=True)
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", repo)

    hermes_main._sweep_stale_bytecode_if_checkout_changed()

    assert _boot_clock.BYTECODE_SWEEP_MS is not None







# ---------------------------------------------------------------------------
# Plugin-update sibling site: __pycache__ under ~/.hermes/plugins/<name>
# ---------------------------------------------------------------------------

def test_clear_plugin_bytecode_removes_nested_caches(tmp_path):
    from hermes_cli import plugins_cmd

    plugin = tmp_path / "myplugin"
    top = plugin / "__pycache__"
    nested = plugin / "sub" / "__pycache__"
    top.mkdir(parents=True)
    nested.mkdir(parents=True)
    (top / "a.pyc").write_bytes(b"stale")
    (nested / "b.pyc").write_bytes(b"stale")

    removed = plugins_cmd._clear_plugin_bytecode(plugin)

    assert removed == 2
    assert not top.exists()
    assert not nested.exists()


