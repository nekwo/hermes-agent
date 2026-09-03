import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hermes_cli import active_sessions


def test_resolve_max_concurrent_sessions_values(caplog):
    assert active_sessions.resolve_max_concurrent_sessions({}) is None
    assert active_sessions.resolve_max_concurrent_sessions({"max_concurrent_sessions": None}) is None
    assert active_sessions.resolve_max_concurrent_sessions({"max_concurrent_sessions": 0}) is None
    assert active_sessions.resolve_max_concurrent_sessions({"max_concurrent_sessions": -1}) is None
    assert active_sessions.resolve_max_concurrent_sessions({"max_concurrent_sessions": "3"}) == 3
    assert (
        active_sessions.resolve_max_concurrent_sessions(
            {"gateway": {"max_concurrent_sessions": 4}}
        )
        == 4
    )
    assert (
        active_sessions.resolve_max_concurrent_sessions(
            {"max_concurrent_sessions": 2, "gateway": {"max_concurrent_sessions": 4}}
        )
        == 2
    )

    caplog.set_level(logging.WARNING)
    assert active_sessions.resolve_max_concurrent_sessions({"max_concurrent_sessions": "many"}) is None
    assert any(
        "Ignoring invalid max_concurrent_sessions='many'" in record.message
        for record in caplog.records
    )












#: Every wait in the six-worker rendezvous below, in seconds.
#:
#: The synchronisation here is already event-based — ready files, then a go
#: file — and none of these bounds is part of what the test proves. They are
#: hang detectors, and they used to be 10 s, which is not a hang bound: it is
#: the same order as what six fresh interpreters cost to start and import
#: ``hermes_cli.active_sessions`` on a box already running eight test workers.
#: Two of them measured that startup rather than a lock: the parent's wait for
#: all six to become ready, and each worker's own wait for the go file, which
#: the parent cannot write until the SLOWEST sibling has arrived. Either
#: expiring reports as a lock defect — a failed acquire, or a non-zero worker.
#: A bound whose value can decide the verdict is measuring the box's process
#: table; this one is set high enough that it cannot.
#:
#: It is deliberately larger than ``pyproject.toml``'s repo-wide
#: ``--timeout=30``, which is why the test below carries its own marker. A
#: bound above the per-test cap can never report: pytest-timeout kills the test
#: first and prints a thread dump instead of the sentence naming which worker
#: never arrived. Declaring the marker is the pattern the repo already settled
#: on for a test whose honest cost exceeds the default (see
#: ``tests/test_coverage_claims_resolve.py``); the alternative — sizing the
#: bound to fit inside 30 s — is the thing this row is fixing.
_WORKER_SYNC_TIMEOUT = 120.0
_TEST_TIMEOUT = 180


@pytest.mark.timeout(_TEST_TIMEOUT)
def test_cross_process_acquire_claims_only_one_last_slot(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo_root = Path(__file__).resolve().parents[2]
    ready_dir = tmp_path / "ready"
    ready_dir.mkdir()
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    go_file = tmp_path / "go"
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(repo_root)
    script = (
        "import os, time\n"
        "from pathlib import Path\n"
        "from hermes_cli.active_sessions import try_acquire_active_session\n"
        "idx = os.environ['WORKER_INDEX']\n"
        "worker_count = int(os.environ['WORKER_COUNT'])\n"
        "delayed_worker = os.environ.get('DELAYED_WORKER_INDEX')\n"
        "ready_dir = Path(os.environ['READY_DIR'])\n"
        "results_dir = Path(os.environ['RESULTS_DIR'])\n"
        "go_file = Path(os.environ['GO_FILE'])\n"
        "timeout = float(os.environ['SYNC_TIMEOUT'])\n"
        "(ready_dir / idx).write_text('ready', encoding='utf-8')\n"
        "deadline = time.time() + timeout\n"
        "while not go_file.exists():\n"
        "    if time.time() > deadline:\n"
        "        raise RuntimeError('timed out waiting for go file')\n"
        "    time.sleep(0.01)\n"
        "if idx == delayed_worker:\n"
        "    time.sleep(2.5)\n"
        "lease, message = try_acquire_active_session(\n"
        "    session_id=f'process-{idx}',\n"
        "    surface='cli',\n"
        "    config={'max_concurrent_sessions': 1},\n"
        ")\n"
        "if lease is None:\n"
        "    (results_dir / idx).write_text('BLOCK', encoding='utf-8')\n"
        "    print('BLOCK', flush=True)\n"
        "else:\n"
        "    (results_dir / idx).write_text('OK', encoding='utf-8')\n"
        "    print('OK', flush=True)\n"
        "    deadline = time.time() + timeout\n"
        "    while len(list(results_dir.iterdir())) < worker_count:\n"
        "        if time.time() > deadline:\n"
        "            raise RuntimeError('timed out waiting for all workers to attempt acquire')\n"
        "        time.sleep(0.01)\n"
        "    lease.release()\n"
    )
    workers: list[subprocess.Popen[str]] = []
    try:
        for index in range(6):
            worker_env = env.copy()
            worker_env["WORKER_INDEX"] = str(index)
            worker_env["WORKER_COUNT"] = "6"
            worker_env["DELAYED_WORKER_INDEX"] = "5"
            worker_env["READY_DIR"] = str(ready_dir)
            worker_env["RESULTS_DIR"] = str(results_dir)
            worker_env["GO_FILE"] = str(go_file)
            worker_env["SYNC_TIMEOUT"] = str(_WORKER_SYNC_TIMEOUT)
            workers.append(
                subprocess.Popen(
                    [sys.executable, "-c", script],
                    env=worker_env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )

        deadline = time.time() + _WORKER_SYNC_TIMEOUT
        while len(list(ready_dir.iterdir())) < len(workers):
            if time.time() > deadline:
                raise AssertionError(
                    f"only {len(list(ready_dir.iterdir()))} of {len(workers)} "
                    f"workers became ready within {_WORKER_SYNC_TIMEOUT}s"
                )
            time.sleep(0.01)
        go_file.write_text("go", encoding="utf-8")

        outputs = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=_WORKER_SYNC_TIMEOUT)
            assert worker.returncode == 0, stderr
            outputs.append(stdout.strip())
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.communicate()

    assert outputs.count("OK") == 1
    assert outputs.count("BLOCK") == len(workers) - 1
    assert active_sessions.active_session_registry_snapshot() == []




def test_release_orphaned_leases_reclaims_only_unowned_own_pid_entries(tmp_path, monkeypatch):
    """A long-lived server must reclaim leases whose session skipped teardown.

    ``_prune_dead`` only fires when the owning pid dies, so a ``hermes
    dashboard`` running for days holds a leaked lease until restart. The
    process reconciles against the leases it still owns instead.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    cfg = {"max_concurrent_sessions": 5}
    kept, orphan = (
        active_sessions.try_acquire_active_session(
            session_id=sid, surface="desktop", config=cfg
        )[0]
        for sid in ("kept", "orphaned")
    )
    # Another live process's lease is not ours to reclaim.
    active_sessions._write_entries(
        active_sessions._state_path(),
        active_sessions._read_entries(active_sessions._state_path())
        + [{"lease_id": "elsewhere", "session_id": "other", "surface": "cli", "pid": os.getpid() }],
    )

    assert active_sessions.release_orphaned_leases({kept.lease_id, "elsewhere"}) == 1
    assert sorted(
        entry["session_id"]
        for entry in active_sessions.active_session_registry_snapshot()
    ) == ["kept", "other"]
    assert orphan is not None
