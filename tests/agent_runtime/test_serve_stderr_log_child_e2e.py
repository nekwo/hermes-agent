"""RL-19, against REAL spawned processes with the launcher's own stdio.

The claim cannot be made in-process. What this file is about is precisely the
arrangement RL-17 gave the live runtime — ``stdin``/``stdout``/``stderr`` all on
``DEVNULL``, no console, nobody reading anything — and in that arrangement the
question "where did the traceback go" has exactly one honest answer, which is
the file the runtime opened for itself. A unit test that calls the opener
directly proves the file has a header; it cannot prove that the interpreter's
own traceback machinery lands in it, because the moment that matters is AFTER
``serve_loop`` has undone its stdio swap and is on its way out.

So each arm here spawns ``python -m hermes_cli.main harness serve --ndjson``
with the real stdio shape, ends it a specific way, and reads what was left in
``serve_instances/``:

* ``--service`` + ``HERMES_SERVE_BOOT_FAULT=raise`` — the 09:09:18Z case with a
  cause: the log must hold the traceback, and the RL-16 sidecar must
  independently say ``uncaught:RuntimeError``. Two records, one death, and they
  have to agree.
* ``--service`` drained cleanly — the ordinary end. The log survives the row's
  removal (the row is deleted on a clean exit; the forensics are not), and its
  first line names the pid and boot_id of the runtime that wrote it.
* NO ``--service`` — the other arm, pinned. The launcher's stdio child still
  writes its stderr to the parent's pipe, and writes no log at all. RL-19 is
  scoped to the runtime nobody is listening to.

Because stdout is ``DEVNULL`` there is no ``ready`` frame to read, which is the
point: every pid in this file is learned the way the launcher learns it, from
the registry row and the sidecar.

Sandboxing is the paranoid shape of the sibling e2es — ``HERMES_HOME``,
``HERMES_HEAD_HOME``, ``HOME``, ``USERPROFILE``, ``LOCALAPPDATA``, ``APPDATA``
and ``HERMES_AGENT_RUNTIME_ROOT`` all under one temp root — so nothing here can
reach the operator's live store.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOT_TIMEOUT_SECONDS = 180.0
CLI_TIMEOUT_SECONDS = 180.0
EXIT_TIMEOUT_SECONDS = 60.0
E2E_TEST_TIMEOUT_SECONDS = 300

#: Same bypass, same reason, as the sibling e2e files: the spawn IS the claim.
_REAL_CHILD_SPAWN = pytest.mark.live_system_guard_bypass

pytestmark = [
    _REAL_CHILD_SPAWN,
    pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS),
]


def _sandbox_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    local = tmp_path / "localappdata"
    roaming = tmp_path / "appdata"
    head = tmp_path / "head"
    for path in (home, local, roaming, head, tmp_path / "runtime"):
        path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.pop("HERMES_SERVE_BOOT_FAULT", None)
    env.update(
        {
            "HERMES_AGENT_RUNTIME_ROOT": str(tmp_path / "runtime"),
            "HERMES_HOME": str(home),
            "HERMES_HEAD_HOME": str(head),
            "LOCALAPPDATA": str(local),
            "APPDATA": str(roaming),
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PYTHONPATH": str(REPO_ROOT)
            + os.pathsep
            + str(env.get("PYTHONPATH") or ""),
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


def _root(env: dict[str, str]) -> Path:
    return Path(env["HERMES_AGENT_RUNTIME_ROOT"])


def _spawn(
    env: dict[str, str],
    *extra_args: str,
    stderr=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
) -> subprocess.Popen:
    """The launcher's shape by default: all three handles on ``DEVNULL``."""

    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "harness",
            "serve",
            "--ndjson",
            *extra_args,
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        text=True,
        bufsize=1,
    )


def _wait_for_live_row(env: dict[str, str], timeout: float = BOOT_TIMEOUT_SECONDS):
    """The pid the way the LAUNCHER learns it: off the registry row.

    There is no ``ready`` frame to read here — stdout is ``DEVNULL``, which is
    the whole arrangement under test — so the row with a bound socket port is
    the signal that this runtime is up and answerable.
    """

    from agent_runtime.serve_registry import list_serve_instances

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for row in list_serve_instances(_root(env)):
            if row.get("classification") == "live" and row.get("port"):
                return row
        time.sleep(0.1)
    raise AssertionError(f"no live serve row appeared within {timeout}s")


def _wait_for_ended(env: dict[str, str], timeout: float = EXIT_TIMEOUT_SECONDS):
    from agent_runtime.serve_registry import list_serve_ended

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = list_serve_ended(_root(env))
        if rows:
            return rows[0]
        time.sleep(0.05)
    raise AssertionError(f"no end-reason sidecar was written within {timeout}s")


def _log_text(env: dict[str, str], pid: int) -> str:
    from agent_runtime.serve_registry import serve_stderr_log_path

    path = serve_stderr_log_path(_root(env), pid)
    assert path.exists(), f"no {pid}.stderr.log was written"
    return path.read_text(encoding="utf-8", errors="replace")


def _logs(env: dict[str, str]) -> list[Path]:
    from agent_runtime.serve_registry import SERVE_STDERR_SUFFIX

    directory = _root(env) / "serve_instances"
    if not directory.exists():
        return []
    return sorted(directory.glob(f"*{SERVE_STDERR_SUFFIX}"))


def _connect(env: dict[str, str], *args: str) -> int:
    completed = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "harness", "serve", "connect", *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_SECONDS,
    )
    return int(completed.returncode)


def _wait_gone(process: subprocess.Popen, timeout: float = EXIT_TIMEOUT_SECONDS) -> int:
    try:
        return int(process.wait(timeout=timeout))
    except subprocess.TimeoutExpired:  # pragma: no cover - failure path
        process.kill()
        raise AssertionError(f"serve child was still alive after {timeout}s")


# ── the arm RL-19 was ruled for ─────────────────────────────────────────────


def test_a_faulting_service_runtime_leaves_its_traceback_in_its_own_log(
    tmp_path: Path,
) -> None:
    """The 09:09:18Z case, with a cause this time.

    A runtime whose three handles are ``DEVNULL`` dies of an uncaught
    ``RuntimeError``. Before RL-19 the only thing left on the machine was a
    stale row and, since RL-16, a four-key reason — "it crashed" without a line
    number. The traceback now lands in the runtime's own file.

    *Killing mutation:* drop the traceback write from ``serve_loop``'s uncaught
    arm and this fails while the sidecar assertion above still passes — which is
    exactly the failure worth catching, and exactly the state main is in: the
    reason is recorded, the cause is not. Nothing else in this process prints a
    traceback for it, which is why the arm writes it rather than relying on the
    restored ``sys.stderr``.
    """

    env = _sandbox_env(tmp_path)
    env["HERMES_SERVE_BOOT_FAULT"] = "raise"
    process = _spawn(env, "--service")
    _wait_gone(process, timeout=BOOT_TIMEOUT_SECONDS)

    ended = _wait_for_ended(env)
    assert ended["reason"] == "uncaught:RuntimeError"
    pid = int(ended["pid"])

    text = _log_text(env, pid)
    assert "Traceback (most recent call last)" in text
    assert "RuntimeError" in text
    assert "HERMES_SERVE_BOOT_FAULT" in text
    # Two records, one death: the sidecar's boot_id is the log's boot_id.
    assert f"boot_id={ended['boot_id']}" in text.splitlines()[0]


def test_a_drained_service_runtimes_log_names_it_on_the_first_line(
    tmp_path: Path,
) -> None:
    """The ordinary end, and the cold-read property.

    A clean exit REMOVES the registry row — that is RL-16's whole reason for
    being a separate file — so the log has to carry its own identity. After the
    drain there is no row left on this machine, and the file still says which
    pid and which boot wrote it.
    """

    env = _sandbox_env(tmp_path)
    process = _spawn(env, "--service")
    row = _wait_for_live_row(env)
    pid = int(row["pid"])
    boot_id = str(row["boot_id"])

    header = _log_text(env, pid).splitlines()[0]
    assert header.startswith("# harness serve --service")
    assert f"pid={pid}" in header
    assert f"boot_id={boot_id}" in header
    assert "started=" in header

    assert _connect(env, "--drain") == 0
    _wait_gone(process)

    from agent_runtime.serve_registry import list_serve_instances, serve_stderr_log_path

    assert list_serve_instances(_root(env)) == []
    assert serve_stderr_log_path(_root(env), pid).exists()
    assert _wait_for_ended(env)["reason"] == "drained"


def test_a_non_service_childs_stderr_still_reaches_the_parents_pipe(
    tmp_path: Path,
) -> None:
    """The other arm, pinned.

    RL-19 is scoped to the runtime nobody is listening to. A serve started the
    old way is a child whose parent is holding its stderr pipe open and reading
    it, and moving that output into a file would take it away from the process
    that asked for it. So the boot's own stderr writing still arrives on the
    pipe, and the directory holds no log at all.

    The fault is injected here for a second reason, and this arm is where the
    asymmetry is stated: this child ALSO dies uncaught, and its traceback goes
    nowhere. That is not a regression this stage introduced — it is what
    2026-09-06 measured on main, where an uncaught serve fault produces three
    frames and exit 1 and no account of itself on any channel. RL-19 buys the
    account for the service arm, which is the arm that had no channel at all;
    the stdio child still has its pipe and its exit code, and changing what
    lands on that pipe is not this ruling.

    *Killing mutation:* arm the redirect unconditionally and this fails on the
    file that should not exist.
    """

    env = _sandbox_env(tmp_path)
    env["HERMES_SERVE_BOOT_FAULT"] = "raise"
    process = _spawn(env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout_text, stderr_text = process.communicate(timeout=BOOT_TIMEOUT_SECONDS)

    assert "skill install" in stderr_text, (
        "the non-service child's own stderr must still reach the parent's pipe"
    )
    assert '"event": "ready"' in stdout_text
    assert _logs(env) == []
