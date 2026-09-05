"""Verify scripts/run_tests_parallel.py kills test-spawned grandchildren.

Setup
-----
A test in this file spawns a long-lived Python grandchild that writes
its PID + a nonce to a tempfile, then exits without cleaning up.
With the old ``subprocess.run`` runner, that grandchild would orphan
and outlive the test (and the whole runner). With the current Popen +
``start_new_session`` + ``_kill_tree`` runner, the grandchild gets
SIGKILL'd via process-group kill when its file's pytest exits.

The leaker test always passes — its only job is to spawn a grandchild
and walk away. The verifier runs the runner over the leaker file in a
subprocess, then waits for the grandchild PID to disappear from the
kernel's process table.

POSIX-only: Windows has its own grandchild lifecycle (no shared session,
``taskkill /F /T`` semantics). Marked accordingly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest


# Both tests share the same handoff file: the leaker writes here, the
# verifier reads here. We park it in $TMPDIR with a unique-per-run name
# so concurrent invocations of the suite don't clobber each other.
_HANDOFF_DIR = Path(os.environ.get("TMPDIR", "/tmp")) / "hermes-isolation-probe"
_HANDOFF_DIR.mkdir(exist_ok=True)


def _handoff_path_for(nonce: str) -> Path:
    return _HANDOFF_DIR / f"grandchild-{nonce}.json"


def _pid_alive(pid: int) -> bool:
    """POSIX: send signal 0 to probe whether ``pid`` is still alive.

    ``os.kill(pid, 0)`` raises ``ProcessLookupError`` if the process is
    gone, ``PermissionError`` if it exists but we can't signal it
    (someone else's pid). We treat PermissionError as "alive" because
    the process exists and that's all we need to know.
    """
    if sys.platform == "win32":  # pragma: no cover — POSIX-only test
        # On Windows we'd use OpenProcess + GetExitCodeProcess; this
        # test is skipped on Windows so the path is unreachable.
        raise RuntimeError("_pid_alive POSIX-only")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only probe")
@pytest.mark.live_system_guard_bypass
def test_grandchild_leak_is_killed_by_runner(tmp_path: Path) -> None:
    """Run the parallel runner over a probe file and verify cleanup.

    1. Materialize a probe file that spawns a long-lived grandchild and
       writes its PID to disk before exiting.
    2. Invoke ``scripts/run_tests_parallel.py`` against the probe file.
    3. Wait for the grandchild PID to vanish (poll for ~5s).
    4. Assert the runner exited cleanly AND the grandchild is dead.
    """
    repo_root = Path(__file__).resolve().parent.parent
    runner = repo_root / "scripts" / "run_tests_parallel.py"
    assert runner.exists(), f"runner missing at {runner}"

    # Probe lives in a temp dir, NOT under tests/, so the regular suite
    # never picks it up — only our explicit invocation does.
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    _root_the_probe(probe_dir)
    probe = probe_dir / "test_probe_leaker.py"
    nonce = f"{os.getpid()}-{int(time.time() * 1000)}"
    handoff = _handoff_path_for(nonce)
    if handoff.exists():
        handoff.unlink()

    probe_src = textwrap.dedent(f"""
        import json, os, subprocess, sys, time
        from pathlib import Path

        HANDOFF = Path({str(handoff)!r})

        def test_spawns_grandchild_and_walks_away():
            # Long-lived grandchild: detached, ignores SIGTERM (we want
            # SIGKILL or process-group kill to be the only thing that
            # works, simulating a misbehaving server).
            child = subprocess.Popen(
                [
                    sys.executable, "-c",
                    "import os, signal, sys, time; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "sys.stdout.write(f'gc-pgid={{os.getpgid(0)}} gc-pid={{os.getpid()}}\\\\n'); "
                    "sys.stdout.flush(); "
                    "time.sleep(600)",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                # IMPORTANT: do NOT pass start_new_session here. We want
                # the grandchild to inherit the pytest subprocess's
                # process group, so when the runner kills the group the
                # grandchild dies too.
            )
            # Read the first line so we can record gc's pgid in the
            # handoff, then walk away — don't close the pipe (would
            # signal EOF and let the child see SIGPIPE on next write).
            first_line = child.stdout.readline().decode().strip()
            HANDOFF.write_text(json.dumps({{
                "pid": child.pid,
                "diag": first_line,
                "test_pid": os.getpid(),
                "test_pgid": os.getpgid(0),
            }}))
            assert child.pid > 0
    """).strip()
    probe.write_text(probe_src + "\n")

    # Run the parallel runner against just the probe file. The runner
    # discovers under ``tests/`` by default, so we override via --paths.
    proc = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--paths",
            str(probe_dir),
            "-j",
            "1",
            # Tight per-file timeout: the probe finishes in <1s, no
            # need for 10min.
            "--file-timeout",
            "30",
        ],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # The runner declares its stdio UTF-8 (see _make_stdio_glyph_safe);
        # decode the same way so ✓-glyph assertions hold on Windows, where
        # text=True alone would decode with the locale codec (cp1252).
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )

    assert handoff.exists(), (
        f"probe never wrote handoff file; runner output:\n{proc.stdout}"
    )
    handoff_data = json.loads(handoff.read_text())
    grandchild_pid = handoff_data["pid"]
    diag = handoff_data.get("diag", "(no diag)")
    test_pid = handoff_data.get("test_pid")
    test_pgid = handoff_data.get("test_pgid")
    handoff.unlink()

    # The runner must have exited cleanly (probe test passes).
    assert proc.returncode == 0, (
        f"runner exited {proc.returncode}; output:\n{proc.stdout}"
    )

    # The grandchild must be gone. Poll for a bit because process-group
    # SIGKILL + reaping isn't synchronous; on a loaded box it can take
    # a beat.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _pid_alive(grandchild_pid):
            break
        time.sleep(0.05)
    else:
        # Test cleanup: kill the leaked grandchild ourselves so a
        # FAILED assertion doesn't leave a sleep(600) running.
        try:
            os.kill(grandchild_pid, 9)
        except ProcessLookupError:
            pass
        pytest.fail(
            f"grandchild PID {grandchild_pid} survived runner exit; "
            f"diag={diag!r} test_pid={test_pid} test_pgid={test_pgid}; "
            f"runner output:\n{proc.stdout}"
        )


# ── Bare pytest-flag passthrough ─────────────────────────────────────────────
#
# The runner routes any token starting with ``-`` that isn't one of its own
# options (``-j``/``--jobs``, ``--paths``, ``--slice``, ``--file-timeout``,
# ``--generate-slices``, ``--files``, ``--include-integration``) straight
# through to each per-file pytest invocation — no ``--`` separator required.
# Before this, a bare ``-q`` errored out with "unrecognized arguments",
# forcing a retry on every run. These tests are behavior contracts, not
# snapshots: they assert that bare flags reach pytest and that value-taking
# flags (``-k expr``) keep their value instead of having it stolen by the
# positional-path discovery.


def _root_the_probe(directory: Path) -> Path:
    """Give a probe tree its own ``pytest.ini``, and return the directory.

    Without one, the inner pytest finds no ini file at or above the probe —
    ``tmp_path`` is under the system temp, which has none — and its rootdir
    falls back ABOVE the probe. The collection tree is then rooted over the
    shared temp directory, which on this box means walking every other test
    process's hermetic home while those are being created and deleted.

    Measured 2026-09-04 on the Windows dev box, the same probe both ways:
    ``python -m pytest --collect-only <probe>`` cost 58.5 s and died with
    ``FileNotFoundError`` on a sibling temp dir that vanished underneath the
    walk, against 1.0 s with this file present. That is why this test FILE
    could not finish inside its own budget — one inner invocation cost more
    than the repo-wide per-test cap, so the file timed out at 8 workers and
    passed only on the runner's 1-worker retry (185.8 s total, 111.8 s of it
    the retry).

    An empty ``[pytest]`` section is the entire content: what is wanted is a
    rootdir anchor, not configuration. The probe deliberately does not inherit
    this repo's ``addopts`` — it is a couple of trivial asserts, and the bound
    that matters to it is the runner's own ``--file-timeout``.
    """

    (directory / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    return directory


def _make_probe_dir(tmp_path: Path) -> Path:
    """Two trivial passing tests, one named test_alpha, one test_beta."""
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    (probe_dir / "test_flagprobe.py").write_text(
        "def test_alpha():\n    assert True\n\n"
        "def test_beta():\n    assert True\n"
    )
    return _root_the_probe(probe_dir)


def _run_runner(probe_dir: Path, *extra: str) -> subprocess.CompletedProcess:
    repo_root = Path(__file__).resolve().parent.parent
    runner = repo_root / "scripts" / "run_tests_parallel.py"
    return subprocess.run(
        [sys.executable, str(runner), "--paths", str(probe_dir),
         "-j", "1", "--file-timeout", "30", *extra],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # The runner declares its stdio UTF-8 (see _make_stdio_glyph_safe);
        # decode the same way so ✓-glyph assertions hold on Windows, where
        # text=True alone would decode with the locale codec (cp1252).
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )




def test_bare_value_flag_keeps_its_value(tmp_path: Path) -> None:
    """``-k test_alpha`` reaches pytest as a selector, not as a path.

    The value token (``test_alpha``) must NOT be swallowed by the runner's
    positional-path discovery — if it were, discovery would look for a path
    named ``test_alpha``, find nothing, and the run would degrade. We assert
    the run succeeds AND only one of the two tests was selected (proving the
    ``-k`` filter actually applied inside pytest).
    """
    probe_dir = _make_probe_dir(tmp_path)
    proc = _run_runner(probe_dir, "-k", "test_alpha")
    assert proc.returncode == 0, proc.stdout
    # Exactly one test selected: the per-file summary shows "1✓" (1 passed).
    # test_beta is deselected by the -k filter.
    assert "1✓" in proc.stdout or "1 passed" in proc.stdout, proc.stdout
    assert "2✓" not in proc.stdout, (
        f"both tests ran — -k filter did not apply:\n{proc.stdout}"
    )




def test_positional_path_not_treated_as_flag(tmp_path: Path) -> None:
    """A positional path arg still overrides discovery (not routed to pytest)."""
    probe_dir = _make_probe_dir(tmp_path)
    repo_root = Path(__file__).resolve().parent.parent
    runner = repo_root / "scripts" / "run_tests_parallel.py"
    # Pass the probe dir positionally (no --paths), plus a bare -q.
    proc = subprocess.run(
        [sys.executable, str(runner), str(probe_dir), "-j", "1",
         "--file-timeout", "30", "-q"],
        cwd=repo_root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        # Decode as UTF-8 like the runner emits (see _run_runner's note);
        # text=True alone uses the locale codec and blows up on cp1252.
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert proc.returncode == 0, proc.stdout
    # Discovery found the probe file (2 tests), proving the positional path
    # was consumed as a root, not forwarded to pytest as a bad flag.
    assert "test_flagprobe.py" in proc.stdout, proc.stdout


def test_file_retry_self_heals_and_prints_both_attempts(tmp_path: Path) -> None:
    """A pass-on-retry is green, loud, and retains the failing traceback."""
    repo_root = Path(__file__).resolve().parent.parent
    runner = repo_root / "scripts" / "run_tests_parallel.py"
    marker = tmp_path / "ran-once"
    # This probe is a bare file rather than a directory, so `tmp_path` itself is
    # what the inner pytest has to root on.
    probe = _root_the_probe(tmp_path) / "test_flaky_probe.py"
    probe.write_text(
        textwrap.dedent(
            f"""
            from pathlib import Path

            def test_flaky_once():
                marker = Path({str(marker)!r})
                if not marker.exists():
                    marker.write_text("failed once")
                    assert False, "simulated first-attempt flake"
                assert True
            """
        ),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--files",
            str(probe),
            "--file-retries",
            "1",
            "-j",
            "1",
            "-q",
        ],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )

    assert proc.returncode == 0, proc.stdout
    assert "FLAKY file" in proc.stdout
    assert "simulated first-attempt flake" in proc.stdout
    assert "first-attempt output" in proc.stdout
    assert "retry output" in proc.stdout




# ---------------------------------------------------------------------------
# Zero-collection is not a pass; node ids are translated, not dropped.
#
# Both behaviors were real foot-guns: a run where NOTHING was collected printed
# "0 tests passed, 0 failed (100% complete)" (reads green), and a pytest node id
# (`file.py::Class::test`) was silently discarded by path discovery so the run
# ended with "No test files to run" while looking like an accepted selector.


def test_a_probe_dir_roots_the_inner_pytest_on_itself(tmp_path: Path) -> None:
    """The reason this file could run at all inside its own budget.

    Every subprocess test here hands the inner pytest a probe under the system
    temp. With no ini file at or above it, pytest's rootdir lands ABOVE the
    probe and the collection tree is rooted over the whole shared temp
    directory — measured 2026-09-04 at 58.5 s and a `FileNotFoundError` on
    another test process's hermetic home, deleted underneath the walk, against
    1.0 s once the probe carries its own `pytest.ini`.

    ANTI-VACUITY: the rootdir is read out of pytest's own header rather than
    inferred from the file existing, so a `pytest.ini` that pytest declined to
    honour would fail this.
    """

    probe_dir = _make_probe_dir(tmp_path)

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", str(probe_dir)],
        cwd=Path(__file__).resolve().parent.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )

    rootdir = [
        line.split(":", 1)[1].strip()
        for line in proc.stdout.splitlines()
        if line.startswith("rootdir:")
    ]
    assert proc.returncode == 0, proc.stdout
    assert rootdir == [str(probe_dir)], proc.stdout
    assert "2 tests collected" in proc.stdout, proc.stdout


def test_zero_collected_across_run_fails_and_says_so(tmp_path: Path) -> None:
    """A -k that matches nothing must FAIL, not report a green summary."""
    probe_dir = _make_probe_dir(tmp_path)
    proc = _run_runner(probe_dir, "-k", "zzz_matches_nothing")
    assert proc.returncode == 1, proc.stdout
    assert "NO TESTS RAN" in proc.stdout
    assert "NOT a pass" in proc.stdout


def test_node_id_selector_runs_the_named_test(tmp_path: Path) -> None:
    """``file.py::test_alpha`` runs that test instead of discovering nothing."""
    probe_dir = _make_probe_dir(tmp_path)
    target = probe_dir / "test_flagprobe.py"
    repo_root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "run_tests_parallel.py"),
         f"{target}::test_alpha", "-j", "1", "--file-timeout", "30"],
        cwd=repo_root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        # Decode as UTF-8 like the runner emits (see _run_runner's note);
        # text=True alone uses the locale codec and blows up on cp1252.
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert proc.returncode == 0, proc.stdout
    assert "No test files to run" not in proc.stdout
    assert "node id" in proc.stdout  # explains the translation
    # Ran exactly the one selected test, not both in the file.
    assert "1 tests passed" in proc.stdout


def test_explicit_k_wins_over_node_id_inference(tmp_path: Path) -> None:
    """A caller's own ``-k`` is not overridden by the node-id translation."""
    probe_dir = _make_probe_dir(tmp_path)
    target = probe_dir / "test_flagprobe.py"
    repo_root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "run_tests_parallel.py"),
         f"{target}::test_alpha", "-k", "test_beta",
         "-j", "1", "--file-timeout", "30"],
        cwd=repo_root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        # Decode as UTF-8 like the runner emits (see _run_runner's note);
        # text=True alone uses the locale codec and blows up on cp1252.
        encoding="utf-8", errors="replace", timeout=60,
    )
    # -k test_beta wins: one test ran, and it wasn't filtered to nothing.
    assert proc.returncode == 0, proc.stdout
    assert "1 tests passed" in proc.stdout


# ---------------------------------------------------------------------------
# Retry composition: two mechanisms, disjoint ownership.
#
# The runner carries BOTH an in-pool one-shot flake retry (--file-retries, the
# `test_file_retry_self_heals_and_prints_both_attempts` case above) and a
# post-drain straggler pass that re-runs TIMEOUT-shaped results once, serially,
# at 1-worker isolation. Naively stacking them would run a hung file three
# times — twice inside the pool (each paying the full --file-timeout) and once
# at isolation, for no signal: an immediate re-run happens under exactly the
# contention the timeout is blamed on. The contract pinned here is the split:
#   * timeout-shaped nonzero  -> straggler pass only (no in-pool retry)
#   * any other nonzero       -> in-pool retry only (flake self-heal)


def _load_runner_module():
    """Import scripts/run_tests_parallel.py as a module for unit-level checks."""
    import importlib.util

    repo_root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "_rtp_under_test", repo_root / "scripts" / "run_tests_parallel.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_TIMEOUT_OUTPUT = "(timed out after 5s; process tree terminated)\n(no output)"


def test_timeout_shape_is_not_retried_in_pool(tmp_path: Path, monkeypatch) -> None:
    """A timeout-shaped result is left for the 1-worker straggler pass."""
    mod = _load_runner_module()
    probe = tmp_path / "test_hang.py"
    calls: list[Path] = []

    def fake_once(file, pytest_args, repo_root, file_timeout):
        calls.append(file)
        return file, 124, _TIMEOUT_OUTPUT, {"passed": 0, "failed": 0}, 5.0

    monkeypatch.setattr(mod, "_run_one_file_once", fake_once)

    _f, rc, output, _summary, _wall = mod._run_one_file(
        probe, [], tmp_path, 5.0, 1
    )

    assert rc == 124
    assert len(calls) == 1, "in-pool retry must not fire for a timeout shape"
    assert "FLAKY" not in output
    assert mod._FLAKY_RESULTS == []
    # …and the straggler pass does claim it.
    assert mod._is_retryable_timeout_result(rc, output, {"passed": 0, "failed": 0})


def test_non_timeout_failure_still_retries_in_pool(tmp_path: Path, monkeypatch) -> None:
    """A plain assertion failure keeps upstream's one-shot flake self-heal."""
    mod = _load_runner_module()
    probe = tmp_path / "test_flake.py"
    calls: list[Path] = []

    def fake_once(file, pytest_args, repo_root, file_timeout):
        calls.append(file)
        if len(calls) == 1:
            return file, 1, "E assert False", {"passed": 0, "failed": 1}, 1.0
        return file, 0, "1 passed", {"passed": 1, "failed": 0}, 1.0

    monkeypatch.setattr(mod, "_run_one_file_once", fake_once)

    _f, rc, output, _summary, wall = mod._run_one_file(probe, [], tmp_path, 5.0, 1)

    assert rc == 0
    assert len(calls) == 2, "the flake retry must still fire for non-timeouts"
    assert "FLAKY" in output
    assert wall == 2.0  # both attempts' wall time is accumulated
    assert [f for f, _out in mod._FLAKY_RESULTS] == [probe]


def test_adaptive_default_jobs_is_capped() -> None:
    """The fork's worker cap survives alongside upstream's retry knob."""
    mod = _load_runner_module()

    assert mod._DEFAULT_MAX_WORKERS == 8
    assert mod._DEFAULT_FILE_RETRIES == 1
    # Small boxes are not inflated; big boxes are capped.
    assert mod._adaptive_default_jobs(2) == 2
    assert mod._adaptive_default_jobs(64) == mod._DEFAULT_MAX_WORKERS
    assert mod._adaptive_default_jobs(None) == 4


def test_file_list_split_keeps_windows_drive_letters(tmp_path: Path) -> None:
    """``--files`` is colon-joined; a bare split() shreds ``C:\\repo\\t.py``.

    Regression: the runner opened a file literally named ``C`` under the repo
    root, so every Windows caller of ``--files`` (CI matrix jobs, the flake
    tests below) died before running anything.
    """
    mod = _load_runner_module()

    assert mod._split_path_list(r"C:\repo\tests\test_a.py") == [
        r"C:\repo\tests\test_a.py"
    ]
    assert mod._split_path_list(r"C:\repo\test_a.py:D:/repo/test_b.py") == [
        r"C:\repo\test_a.py",
        "D:/repo/test_b.py",
    ]
    # POSIX lists are untouched.
    assert mod._split_path_list("tests/a.py:tests/b.py") == [
        "tests/a.py",
        "tests/b.py",
    ]
    assert mod._split_discovery_roots("tests:packages") == ["tests", "packages"]


def test_a_file_list_longer_than_the_os_will_stat_still_splits(monkeypatch) -> None:
    """The list CI actually passes, and the reason nobody saw it break.

    ``--files`` carries one colon-joined slice — 8 slices over ~3,040 files is
    roughly 30 KB of argument — and the split began with an ``exists()`` probe
    for the "the argument IS one path with a colon in it" case. A joined list is
    not a path, and the two hosts disagree about how to say so: Linux's
    ``stat()`` answers ``ENAMETOOLONG`` for anything past ``PATH_MAX`` and
    ``pathlib`` does not ignore that errno, so the probe RAISED; Windows folds
    the same overflow into its ignored winerror set and answers ``False``.

    So every CI matrix slice died in ``_split_path_list`` before collecting a
    test — measured on nekwo/hermes-agent, all 8 slices red with
    ``OSError: [Errno 36] File name too long`` on every main push from
    2026-08-04 (the first run after the probe landed) through 2026-09-05 — while
    the identical call was green on every developer's Windows box.

    Both halves are asserted because either alone is a half-test: the real
    over-long list is what CI passes and is enough on POSIX, and the forced
    ``OSError`` is what makes a Windows author trip here too rather than
    shipping the same asymmetry again.
    """
    mod = _load_runner_module()

    joined = ":".join(f"tests/dir{index}/test_module_{index}.py" for index in range(600))
    assert len(joined) > 4096, "the case needs a list past PATH_MAX"
    assert mod._split_path_list(joined) == joined.split(":")

    def _too_long(self) -> bool:
        raise OSError(36, "File name too long")

    monkeypatch.setattr(mod.Path, "exists", _too_long)
    assert mod._split_path_list("tests/a.py:tests/b.py") == ["tests/a.py", "tests/b.py"]
    assert mod._split_discovery_roots("tests:packages") == ["tests", "packages"]


def _drive_main_over_one_file(
    mod, monkeypatch, tmp_path: Path, attempts: list[tuple[int, str, dict]]
) -> int:
    """Run the runner's ``main`` over one probe file, scripting each attempt.

    In-process on purpose: the thing under test is the SUMMARY's accounting,
    and driving it through a real pytest subprocess would pay ~30 s of this
    repo's collection per attempt to observe an integer.
    """
    probe = tmp_path / "test_straggler_probe.py"
    probe.write_text("def test_one():\n    assert True\n", encoding="utf-8")
    calls: list[Path] = []

    def fake_once(file, pytest_args, repo_root, file_timeout):
        rc, output, summary = attempts[min(len(calls), len(attempts) - 1)]
        calls.append(file)
        return file, rc, output, summary, 1.0

    monkeypatch.setattr(mod, "_run_one_file_once", fake_once)
    # The real one writes test_durations.json into the checkout.
    monkeypatch.setattr(mod, "_save_durations", lambda *a, **k: None)
    monkeypatch.setattr(
        sys, "argv",
        ["run_tests_parallel.py", "--files", str(probe), "-j", "1",
         "--file-retries", "0", "--file-timeout", "5"],
    )
    return mod.main()


_KILLED_ATTEMPT = (124, _TIMEOUT_OUTPUT, {"passed": 0, "failed": 0})


def test_a_straggler_retry_that_passes_is_not_reported_as_zero_collected(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The straggler's collection counts, or the run contradicts itself.

    Observed 2026-09-02 on ``tests/hermes_cli/test_harness_characters_cli.py``:
    the file tripped the per-file wall clock at 8 workers, collected nothing
    into the killed attempt, then passed at 1-worker isolation — and the run
    printed ``RETRY PASS … (95 tests)`` and ``Summary: 95 tests passed, 0
    failed`` followed by ``✗ NO TESTS RAN — 0 collected``. ``tests_collected``
    was accumulated ONLY inside the pool's ``_on_done`` callback, so every
    outcome the straggler pass recovered was invisible to the nothing-ran
    guard. A banner that says "not a pass" over a pass is one an operator
    learns to ignore, which is the whole reason the guard exists.
    """
    mod = _load_runner_module()

    code = _drive_main_over_one_file(
        mod, monkeypatch, tmp_path,
        [_KILLED_ATTEMPT, (0, "95 passed", {"passed": 95, "failed": 0})],
    )
    out = capsys.readouterr().out

    assert "RETRY PASS" in out, out
    assert "95 tests passed" in out, out
    assert "NO TESTS RAN" not in out, out
    assert code == 0, out


def test_a_straggler_that_recovers_nothing_still_trips_the_guard(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """ANTI-VACUITY: the guard is quieted by a real COLLECTION, not by the
    straggler pass having run. Same shape, but the retry is killed too, so the
    run really did collect nothing and must still say so."""
    mod = _load_runner_module()

    code = _drive_main_over_one_file(
        mod, monkeypatch, tmp_path, [_KILLED_ATTEMPT, _KILLED_ATTEMPT]
    )
    out = capsys.readouterr().out

    assert "RETRY FAIL" in out, out
    assert "NO TESTS RAN" in out, out
    assert code == 1, out


def test_a_straggler_that_recovers_only_skips_still_counts_as_collection(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The pool callback counts skips, xfails and errors as collection, and the
    straggler must count them the same way: a platform-gated file that reports
    "2 skipped" on its isolation re-run DID collect, and the guard exists for a
    run that collected nothing at all."""
    mod = _load_runner_module()

    code = _drive_main_over_one_file(
        mod, monkeypatch, tmp_path,
        [_KILLED_ATTEMPT, (0, "2 skipped", {"passed": 0, "failed": 0, "skipped": 2})],
    )
    out = capsys.readouterr().out

    assert "NO TESTS RAN" not in out, out
    assert code == 0, out


# ── the branch-measurement seam ─────────────────────────────────────────────


def test_an_untraced_run_spawns_exactly_what_it_always_did(monkeypatch) -> None:
    """No config var, no wrapper — the argv is byte-identical to the old one.

    The seam has to be free when nobody asked for it: a runner that could be
    traced by accident would report suite numbers measured under an
    instrumented interpreter, which is a different run from the one everyone
    else's results came from.
    """
    mod = _load_runner_module()
    monkeypatch.delenv(mod._COVERAGE_RC_ENV, raising=False)

    probe = Path("tests/agent/test_x.py")
    assert mod._pytest_argv(probe, ["-q"]) == [
        sys.executable, "-m", "pytest", str(probe), "-q",
    ]


def test_the_config_var_wraps_each_file_in_coverage_run(monkeypatch) -> None:
    """``coverage run`` goes BEFORE ``-m pytest``, and carries the rcfile.

    Position is the whole assertion. ``python -m pytest -m coverage run`` would
    hand pytest a ``-m`` marker expression and trace nothing, and the run would
    still be green — the failure mode the report cannot see from its own side,
    because it would simply find no data and blame the suite.
    """
    mod = _load_runner_module()
    monkeypatch.setenv(mod._COVERAGE_RC_ENV, "/tmp/rc")

    probe = Path("tests/agent/test_x.py")
    assert mod._pytest_argv(probe, ["-q"]) == [
        sys.executable, "-m", "coverage", "run", "--rcfile=/tmp/rc",
        "-m", "pytest", str(probe), "-q",
    ]


def test_a_blank_config_var_is_the_same_as_an_absent_one(monkeypatch) -> None:
    """An empty value must not produce ``--rcfile=``, which coverage refuses."""
    mod = _load_runner_module()
    monkeypatch.setenv(mod._COVERAGE_RC_ENV, "   ")

    assert "coverage" not in mod._pytest_argv(Path("tests/agent/test_x.py"), [])
