"""RL-16, against REAL spawned processes: one arm per way a runtime can end.

The sidecar's whole purpose is forensic — it exists so the NEXT unexplained
death names itself instead of a Fable guessing from a leftover registry row —
and a forensic record that has only ever been written by a unit test calling
the writer directly proves nothing about the moment it is actually needed. Each
test here spawns ``python -m hermes_cli.main harness serve --ndjson --service``,
kills it a different way, waits for the process to be gone, and reads the file
it left behind.

What each arm is really testing
-------------------------------

* ``drained`` / ``shutdown_op`` / ``stdin_eof`` — the three ordinary ends, each
  down a different code path (the drain's terminal, the stdio order, the pipe
  closing). ``drained`` additionally proves the write survives ``hard_exit``,
  which is ``os._exit`` and runs no ``atexit`` hook.
* ``ctrl_c`` — a real console control event, generated into the child's own
  process group. ``CTRL_CLOSE_EVENT`` has no generator in the Windows API at
  all (it is what the OS sends when a console window is closed), so the mapping
  for it is pinned as a table in ``test_serve_ended_sidecar.py`` and the
  handler's installation is proven here by the sibling arm that fires.
* ``sigterm`` — POSIX only; on Windows ``os.kill(pid, SIGTERM)`` is
  ``TerminateProcess`` and no handler runs, which is precisely the "absent is
  the reading" case below.
* ``uncaught:RuntimeError`` and ``unknown_exit`` — through the boot-fault seam,
  which is inert without ``HERMES_SERVE_BOOT_FAULT``.
* **absent** — ``os._exit`` writes nothing. That is the ``TerminateProcess``
  analogue and the launcher words it ``ended=absent``.
* **no console** — the child is started ``DETACHED_PROCESS``, so it has no
  console at all. RL-17 is about to make that the normal case
  (``CREATE_NO_WINDOW``), and a handler installation that errored or aborted
  the boot there would take the runtime with it.

Sandboxing is the same paranoid shape as ``test_serve_socket_child_e2e.py``,
one env pin wider: ``HERMES_HEAD_HOME`` and ``APPDATA`` are pinned too, so
nothing in this file can reach the operator's live store.
"""

from __future__ import annotations

import json
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

#: Same bypass, same reason, as the sibling e2e file: the spawn IS the claim.
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


class _Child:
    """A spawned serve, and — separately — the pid of the process it became.

    Those are two different numbers on Windows and it matters here, because
    every assertion in this file is keyed on a pid. ``sys.executable`` inside a
    venv is a REDIRECTOR: it launches the base interpreter as its own child and
    waits, so ``Popen.pid`` names the redirector and the runtime (the process
    that writes the registry row and the sidecar) is one below it. The
    ``ready`` frame carries the runtime's own ``pid``, which is the authority
    the launcher uses too — this file uses nothing else. The same shape is what
    §8.8c measured on the live chain.

    A console control event still reaches the runtime: it inherits the
    redirector's process group, which is what ``CREATE_NEW_PROCESS_GROUP``
    creates and what the event is aimed at.
    """

    def __init__(self, process: subprocess.Popen) -> None:
        self.process = process
        self.frames: list[dict] = []
        self.pid: int = 0

    def boot(self) -> dict:
        """Wait for ``ready`` and remember the RUNTIME's pid."""

        ready = self.wait_for("ready")
        self.pid = int(ready["pid"])
        return ready

    def wait_for(self, event: str, timeout: float = BOOT_TIMEOUT_SECONDS) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.process.stdout.readline()
            if not line:
                raise AssertionError(
                    f"serve child ended before {event!r}; frames so far: "
                    f"{[f.get('event') for f in self.frames]}"
                )
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.frames.append(frame)
            if frame.get("event") == event:
                return frame
        raise AssertionError(f"no {event!r} from the serve child within {timeout}s")

    def wait_gone(self, timeout: float = EXIT_TIMEOUT_SECONDS) -> int:
        try:
            return int(self.process.wait(timeout=timeout))
        except subprocess.TimeoutExpired:  # pragma: no cover - failure path
            self.process.kill()
            raise AssertionError(f"serve child was still alive after {timeout}s")


def _spawn(
    env: dict[str, str],
    *extra_args: str,
    stdin_pipe: bool = False,
    creationflags: int = 0,
) -> _Child:
    return _Child(
        subprocess.Popen(
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
            stdin=subprocess.PIPE if stdin_pipe else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            creationflags=creationflags,
        )
    )


def _ended(env: dict[str, str], pid: int) -> dict | None:
    from agent_runtime.serve_registry import read_serve_ended

    root = Path(env["HERMES_AGENT_RUNTIME_ROOT"])
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        record = read_serve_ended(root, pid)
        if record is not None:
            return record
        time.sleep(0.05)
    return None


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


def _assert_reason(record: dict | None, reason: str, pid: int) -> None:
    assert record is not None, f"no {pid}.ended.json was written"
    assert record["reason"] == reason
    assert record["pid"] == pid
    assert isinstance(record["boot_id"], str) and len(record["boot_id"]) == 32
    assert record["at"].endswith("Z")


# ── the ordinary ends ───────────────────────────────────────────────────────


def test_a_drain_says_drained(tmp_path: Path) -> None:
    """The restart verb. Also the only arm that proves the record survives
    ``hard_exit`` — ``os._exit`` runs no ``atexit`` hook, so a sidecar written
    only from the fallback would be missing exactly here."""

    env = _sandbox_env(tmp_path)
    child = _spawn(env, "--service")
    child.boot()
    assert _connect(env, "--drain") == 0
    child.wait_gone()
    _assert_reason(_ended(env, child.pid), "drained", child.pid)


def test_a_stdio_shutdown_order_says_shutdown_op(tmp_path: Path) -> None:
    """``{"op":"shutdown"}`` is an ORDER, and stays distinguishable from the
    pipe merely closing — which is the distinction service mode created."""

    env = _sandbox_env(tmp_path)
    child = _spawn(env, "--service", stdin_pipe=True)
    child.boot()
    child.process.stdin.write(json.dumps({"op": "shutdown"}) + "\n")
    child.process.stdin.flush()
    child.wait_gone()
    _assert_reason(_ended(env, child.pid), "shutdown_op", child.pid)


def test_a_non_service_serve_that_reaches_eof_says_stdin_eof(tmp_path: Path) -> None:
    """The launcher's stdio child, ending the way it always has. Not reachable
    under ``--service`` at all — there EOF parks instead of exiting — which is
    why this word can never reach the launcher's service-mode reader."""

    env = _sandbox_env(tmp_path)
    child = _spawn(env)
    child.boot()
    child.wait_gone()
    _assert_reason(_ended(env, child.pid), "stdin_eof", child.pid)


# ── the console control handler ─────────────────────────────────────────────


def _has_console() -> bool:
    if sys.platform != "win32":
        return False
    import ctypes

    try:
        return bool(ctypes.windll.kernel32.GetConsoleCP())
    except Exception:  # pragma: no cover - defensive
        return False


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="console control events are a Windows mechanism; the POSIX analogue "
    "is the SIGTERM arm below",
)
@pytest.mark.skipif(
    not _has_console(),
    reason="GenerateConsoleCtrlEvent needs a console the child can share; this "
    "runner has none",
)
def test_a_console_control_event_says_ctrl_c(tmp_path: Path) -> None:
    """A REAL console control event, delivered by the OS to the handler.

    ``CTRL_BREAK_EVENT`` rather than ``CTRL_C_EVENT`` for a documented Windows
    reason, not a convenience: ``GenerateConsoleCtrlEvent`` succeeds for
    ``CTRL_C_EVENT`` with a non-zero group id and then does not deliver it —
    Ctrl-C can only be sent to group 0, i.e. every process on this console,
    which would include the test runner. Break is the one that can be aimed. It
    lands on the same handler, through the same table, and both words are
    ``ctrl_c``: the operator interrupted it.
    """

    import ctypes
    import signal

    env = _sandbox_env(tmp_path)
    child = _spawn(env, "--service", creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    child.boot()
    # The GROUP id — which is the process ``CREATE_NEW_PROCESS_GROUP`` made a
    # group leader, i.e. the redirector. The runtime is inside that group.
    assert ctypes.windll.kernel32.GenerateConsoleCtrlEvent(
        signal.CTRL_BREAK_EVENT, child.process.pid
    ), "GenerateConsoleCtrlEvent failed"
    child.wait_gone()
    _assert_reason(_ended(env, child.pid), "ctrl_c", child.pid)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="on Windows os.kill(pid, SIGTERM) is TerminateProcess: no handler "
    "runs, which is the 'absent is the reading' arm instead",
)
def test_sigterm_says_sigterm(tmp_path: Path) -> None:
    """The POSIX stop verb. ``--service`` parks the main thread on an event and
    installs its OWN SIGTERM handler over the recorder's for the duration, so
    this arm is also the proof that the park's handler still names the cause."""

    env = _sandbox_env(tmp_path)
    child = _spawn(env, "--service")
    child.boot()
    child.process.terminate()
    child.wait_gone()
    _assert_reason(_ended(env, child.pid), "sigterm", child.pid)


@pytest.mark.skipif(
    sys.platform != "win32", reason="DETACHED_PROCESS is a Windows creation flag"
)
def test_the_handler_installs_on_a_runtime_with_no_console_at_all(
    tmp_path: Path,
) -> None:
    """RL-17's shape, today. ``DETACHED_PROCESS`` gives the child no console,
    so ``SetConsoleCtrlHandler`` has nothing to attach to — it must install,
    never fire, and cost the boot nothing.

    *Killing mutation:* let the install raise instead of reporting, and this
    child never reaches ``ready``.
    """

    env = _sandbox_env(tmp_path)
    child = _spawn(env, "--service", creationflags=subprocess.DETACHED_PROCESS)
    child.boot()
    assert _connect(env, "--drain") == 0
    child.wait_gone()
    _assert_reason(_ended(env, child.pid), "drained", child.pid)


# ── the fault seam: uncaught, plain exit, and absence ───────────────────────


def test_an_uncaught_exception_names_its_type(tmp_path: Path) -> None:
    env = _sandbox_env(tmp_path)
    env["HERMES_SERVE_BOOT_FAULT"] = "raise"
    child = _spawn(env, "--service")
    child.boot()
    child.wait_gone()
    _assert_reason(
        _ended(env, child.pid), "uncaught:RuntimeError", child.pid
    )


def test_a_plain_exit_that_set_no_reason_says_unknown_exit(tmp_path: Path) -> None:
    """``SystemExit`` unwinds the interpreter normally, so ``atexit`` runs and
    the fallback is what lands. Not a failure — a route nobody taught the
    recorder about, said plainly rather than guessed at."""

    env = _sandbox_env(tmp_path)
    env["HERMES_SERVE_BOOT_FAULT"] = "exit"
    child = _spawn(env, "--service")
    child.boot()
    child.wait_gone()
    _assert_reason(_ended(env, child.pid), "unknown_exit", child.pid)


def test_a_hard_exit_writes_nothing_and_the_absence_is_the_reading(
    tmp_path: Path,
) -> None:
    """``os._exit`` is the in-process stand-in for ``TerminateProcess``: no
    ``atexit``, no unwinding, no record. RL-16 makes that silence load-bearing —
    the launcher reads a stale row with no sidecar as ``ended=absent``, i.e.
    *something killed this without asking* — so an implementation that
    "helpfully" wrote ``unknown_exit`` from somewhere earlier would erase the
    only evidence of a hard kill.

    The registry ROW is still there, which is the other half of that reading.
    """

    from agent_runtime.serve_registry import serve_instance_path

    env = _sandbox_env(tmp_path)
    env["HERMES_SERVE_BOOT_FAULT"] = "hard"
    child = _spawn(env, "--service")
    child.boot()
    child.wait_gone()
    root = Path(env["HERMES_AGENT_RUNTIME_ROOT"])
    assert _ended(env, child.pid) is None
    assert serve_instance_path(root, child.pid).exists()


# ── retention, on a real boot ───────────────────────────────────────────────


def test_a_boot_prunes_the_sidecar_directory_to_the_newest_twenty(
    tmp_path: Path,
) -> None:
    """The retention floor, armed where it actually runs: serve boot.

    *Killing mutation:* drop the prune call from the boot and the 25 planted
    records all survive.
    """

    from agent_runtime.serve_registry import list_serve_ended, write_serve_ended

    env = _sandbox_env(tmp_path)
    root = Path(env["HERMES_AGENT_RUNTIME_ROOT"])
    for index in range(25):
        write_serve_ended(
            root,
            reason="drained",
            boot_id=f"planted{index}",
            pid=900000 + index,
            at=f"2020-01-01T00:00:{index:02d}.000Z",
        )
    child = _spawn(env, "--service")
    child.boot()
    assert _connect(env, "--drain") == 0
    child.wait_gone()

    rows = list_serve_ended(root)
    # The boot prunes to 20; this runtime's OWN record is written afterwards,
    # on its way out, and the next boot is what re-floors the directory.
    planted = {row["pid"] for row in rows if row["pid"] >= 900000}
    assert planted == set(range(900005, 900025))
    assert child.pid in {row["pid"] for row in rows}
    assert len(rows) == 21
