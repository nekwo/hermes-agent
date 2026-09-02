"""``agent_browser_runnable`` spawns ``--version`` once per path, not once per ask.

THE MEASUREMENT. W1-H2 counted the operator's live
``.hermes/profiles/alice/node/agent-browser.CMD --version`` executing **56
times in one suite run**. The path is import-bound (``doctor.HERMES_HOME =
get_hermes_home()`` at module scope), so a hermetic test home does not redirect
it, and the gateway fence exempts the spawn on the argued grounds that a
``--version`` call starts nothing. That exemption makes the calls LEGAL. It does
not make them free: four resolvers walk overlapping candidate lists (``doctor``
four, ``browser_tool`` five, ``dep_ensure`` and ``nous_subscription`` three
each), and each miss on a HUNG binary costs the full 10 s timeout.

WHAT IS PINNED HERE, and the split is the whole point:

* the SPAWN is memoised per path — the expensive half;
* every cheap gate still runs on every call — so the verdict is identical.

The second is the one that could have been broken quietly. Memoising the whole
function would have cached ``False`` for a path that did not exist yet, and
``True`` for one that has since been deleted or turned into a dangling symlink —
which is precisely the #48521 case this function was written for. So the
deletion tests are not decoration: they are the guarantee that the optimisation
did not eat the behaviour.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

import hermes_constants
from hermes_constants import agent_browser_runnable, reset_agent_browser_probe_cache


@pytest.fixture(autouse=True)
def _cold_cache():
    """Every test starts and ends with an empty cache.

    The cache is process-wide by design, so a test that left rows behind would
    hand the next one a verdict it never took — the same cross-test pollution
    class the hermes_cli conftest spends 200 lines on.
    """

    reset_agent_browser_probe_cache()
    yield
    reset_agent_browser_probe_cache()


class _SpawnCounter:
    """Counts ``subprocess.run`` calls and answers them with a fixed code."""

    def __init__(self, returncode: int = 0):
        self.calls: list[list[str]] = []
        self.returncode = returncode

    def __call__(self, argv, *args, **kwargs):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, self.returncode, b"", b"")


def _runnable_stub(tmp_path, name: str = "agent-browser"):
    """A real, executable file on disk — the cheap gates must actually pass."""

    tmp_path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        path = tmp_path / f"{name}.cmd"
        path.write_text("@echo off\r\necho 1.0.0\r\n", encoding="utf-8")
    else:
        path = tmp_path / name
        path.write_text("#!/bin/sh\necho 1.0.0\n", encoding="utf-8")
        os.chmod(path, 0o755)
    return path


def test_the_version_probe_runs_once_per_path_however_often_it_is_asked(
    tmp_path, monkeypatch
):
    """Ten asks, one spawn — and the same answer all ten times.

    Ten rather than two because the measured defect was not "it probed twice",
    it was fifty-six: a per-CALL memo that somehow re-armed would still pass a
    two-ask assertion.
    """

    good = _runnable_stub(tmp_path)
    counter = _SpawnCounter(returncode=0)
    monkeypatch.setattr(subprocess, "run", counter)

    verdicts = [agent_browser_runnable(str(good)) for _ in range(10)]

    assert verdicts == [True] * 10
    assert len(counter.calls) == 1
    assert counter.calls[0] == [str(good), "--version"]


def test_a_failing_probe_is_cached_too(tmp_path, monkeypatch):
    """The arm that pays most for a cache, because a broken candidate is the
    one every resolver walks PAST — and on a hung binary each walk-past is the
    full 10 s timeout, not a fast non-zero exit."""

    dead = _runnable_stub(tmp_path, "dead-browser")
    counter = _SpawnCounter(returncode=127)
    monkeypatch.setattr(subprocess, "run", counter)

    verdicts = [agent_browser_runnable(str(dead)) for _ in range(5)]

    assert verdicts == [False] * 5
    assert len(counter.calls) == 1


def test_a_raising_probe_is_cached_as_false_without_re_spawning(tmp_path, monkeypatch):
    """``TimeoutExpired`` is the expensive failure by definition. It must reach
    the same ``False`` it always did, and must not be re-paid."""

    hung = _runnable_stub(tmp_path, "hung-browser")
    calls: list[list[str]] = []

    def _boom(argv, *args, **kwargs):
        calls.append(list(argv))
        raise subprocess.TimeoutExpired(argv, 10)

    monkeypatch.setattr(subprocess, "run", _boom)

    assert agent_browser_runnable(str(hung)) is False
    assert agent_browser_runnable(str(hung)) is False
    assert len(calls) == 1


def test_two_different_paths_each_get_their_own_probe(tmp_path, monkeypatch):
    """Keyed on the path, not a single global verdict. ``doctor`` resolves four
    DIFFERENT candidates and falls through on each miss; one shared row would
    make the second candidate inherit the first one's answer."""

    good = _runnable_stub(tmp_path / "a")
    other = _runnable_stub(tmp_path / "b")
    counter = _SpawnCounter(returncode=0)
    monkeypatch.setattr(subprocess, "run", counter)

    assert agent_browser_runnable(str(good)) is True
    assert agent_browser_runnable(str(other)) is True
    assert agent_browser_runnable(str(good)) is True

    assert [call[0] for call in counter.calls] == [str(good), str(other)]


def test_a_cached_true_does_not_survive_the_file_disappearing(tmp_path, monkeypatch):
    """The #48521 guarantee, and the reason only the SPAWN is cached.

    ``hermes update`` wipes ``node_modules`` mid-session and leaves a dangling
    global symlink behind. A whole-function memo would keep answering ``True``
    for a binary that no longer exists, and every browser tool would then fail
    at exec with 127 — exactly the silent breakage this function was written to
    prevent.
    """

    good = _runnable_stub(tmp_path)
    counter = _SpawnCounter(returncode=0)
    monkeypatch.setattr(subprocess, "run", counter)

    assert agent_browser_runnable(str(good)) is True

    good.unlink()

    assert agent_browser_runnable(str(good)) is False
    # And no second spawn was paid to learn that: the cheap gate answered.
    assert len(counter.calls) == 1


def test_a_path_that_never_existed_is_never_cached_so_an_install_is_seen(
    tmp_path, monkeypatch
):
    """The install direction. ``ensure_dependency`` re-checks after the install
    script returns 0, and the candidate it re-checks may be a path this process
    already asked about while it was absent. An absent path never reaches the
    subprocess, so it never enters the cache, and the re-check sees the truth.
    """

    later = tmp_path / ("agent-browser.cmd" if sys.platform == "win32" else "agent-browser")
    counter = _SpawnCounter(returncode=0)
    monkeypatch.setattr(subprocess, "run", counter)

    assert agent_browser_runnable(str(later)) is False
    assert counter.calls == []

    installed = _runnable_stub(tmp_path)
    assert installed == later

    assert agent_browser_runnable(str(later)) is True
    assert len(counter.calls) == 1


def test_reset_forgets_a_non_zero_verdict_so_a_repair_is_seen(tmp_path, monkeypatch):
    """The ONE direction a verdict can go stale: a candidate that exists and
    exits non-zero, repaired IN PLACE inside the same process. That is the
    installer's path, which is why ``dep_ensure.ensure_dependency`` clears the
    cache after a successful install and before its re-check."""

    path = _runnable_stub(tmp_path)
    broken = _SpawnCounter(returncode=127)
    monkeypatch.setattr(subprocess, "run", broken)
    assert agent_browser_runnable(str(path)) is False

    repaired = _SpawnCounter(returncode=0)
    monkeypatch.setattr(subprocess, "run", repaired)
    # Without the reset the stale row wins — that is the point of the hook.
    assert agent_browser_runnable(str(path)) is False
    assert repaired.calls == []

    reset_agent_browser_probe_cache()
    assert agent_browser_runnable(str(path)) is True
    assert len(repaired.calls) == 1


def test_ensure_dependency_clears_the_cache_after_a_successful_install():
    """The hook is WIRED, not merely exported.

    Asserted through the element model rather than by reading the source: the
    function object ``dep_ensure`` calls is looked up on ``hermes_constants`` at
    call time, so patching the module attribute proves the call actually
    happens on the success path.
    """

    from hermes_cli import dep_ensure

    cleared: list[bool] = []
    checked: list[bool] = []

    def _spy_reset():
        cleared.append(True)

    def _check():
        # The re-check must run AFTER the clear, or it replays a pre-install
        # verdict — the ordering is the whole reason the hook is here.
        checked.append(bool(cleared))
        return True

    class _Ok:
        returncode = 0

    original_reset = hermes_constants.reset_agent_browser_probe_cache
    original_checks = dict(dep_ensure._DEP_CHECKS)
    original_run = dep_ensure.subprocess.run
    original_find = dep_ensure._find_install_script
    try:
        hermes_constants.reset_agent_browser_probe_cache = _spy_reset
        dep_ensure._find_install_script = lambda *a, **k: (__file__, "bash")
        dep_ensure.subprocess.run = lambda *a, **k: _Ok()
        # Absent on the pre-install gate, present on the post-install re-check —
        # the only shape that reaches the code path under test.
        dep_ensure._DEP_CHECKS["browser"] = _make_two_phase(_check)

        assert dep_ensure.ensure_dependency("browser", interactive=False) is True
    finally:
        hermes_constants.reset_agent_browser_probe_cache = original_reset
        dep_ensure._DEP_CHECKS.clear()
        dep_ensure._DEP_CHECKS.update(original_checks)
        dep_ensure.subprocess.run = original_run
        dep_ensure._find_install_script = original_find

    assert cleared == [True]
    # First call (the pre-install gate) said "absent"; the second is the
    # re-check, and it ran with the cache already cleared.
    assert checked == [True]


def _make_two_phase(after):
    """Answer ``False`` the first time (so the install runs), then delegate."""

    state = {"first": True}

    def _check():
        if state["first"]:
            state["first"] = False
            return False
        return after()

    return _check
