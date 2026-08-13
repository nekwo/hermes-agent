"""End to end, against a REAL spawned ``harness serve`` child.

Everything else in this slice exercises ``serve_loop`` in-process. That is the
right seam for behaviour, and the wrong one for the two claims this file makes:
that the CLI verb an operator actually types finds the service, authenticates to
it, and can restart it — and that a drain asked from OUTSIDE the process is
observed to completion by the process it drains. Slice 2 could not exercise the
drain path at all for exactly this reason: over stdio, the drain ends the only
connection that could have watched it.

Sandboxing, deliberately paranoid
---------------------------------

A real serve boot publishes the MACHINE-GLOBAL root anchor
(``%LOCALAPPDATA%/hermes/config.yaml``) — that is its job, and it is precisely
what must not happen from a test. So the child gets a sandboxed
``LOCALAPPDATA`` / ``HOME`` / ``USERPROFILE`` / ``HERMES_HOME`` alongside its
temporary runtime root, and ``PYTHONPATH`` pins THIS checkout ahead of the
editable install (the live venv points at the primary tree; without the pin this
file would test code it did not change).
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


def _sandbox_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    local = tmp_path / "localappdata"
    for path in (home, local, tmp_path / "runtime"):
        path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "HERMES_AGENT_RUNTIME_ROOT": str(tmp_path / "runtime"),
            "HERMES_HOME": str(home),
            "LOCALAPPDATA": str(local),
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
    def __init__(self, process: subprocess.Popen, env: dict[str, str]) -> None:
        self.process = process
        self.env = env
        self.frames: list[dict] = []

    def read_frame(self, timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.process.stdout.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.frames.append(frame)
            return frame
        return None

    def wait_for(self, event: str, timeout: float = BOOT_TIMEOUT_SECONDS) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self.read_frame(timeout=deadline - time.monotonic())
            if frame is None:
                raise AssertionError(
                    f"serve child ended before {event!r}; frames so far: "
                    f"{[f.get('event') for f in self.frames]}"
                )
            if frame.get("event") == event:
                return frame
        raise AssertionError(f"no {event!r} from the serve child within {timeout}s")


def _connect(env: dict[str, str], *args: str) -> tuple[int, dict | None, str]:
    completed = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "harness", "serve", "connect", *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_SECONDS,
    )
    payload = None
    stdout = completed.stdout or ""
    # The verb prints ONE JSON object; anything else on stdout is noise from a
    # dependency and must not make the reply unreadable.
    start = stdout.find("{")
    if start >= 0:
        try:
            payload = json.loads(stdout[start:])
        except json.JSONDecodeError:
            payload = None
    return completed.returncode, payload, stdout + (completed.stderr or "")


def test_probe_then_drain_over_the_socket_against_a_real_serve_child(tmp_path):
    env = _sandbox_env(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-m", "hermes_cli.main", "harness", "serve", "--ndjson"],
        cwd=str(REPO_ROOT),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    child = _Child(process, env)
    try:
        ready = child.wait_for("ready")
        # Positive proof this child is running THIS branch, not the editable
        # install: the socket block does not exist before slice 3.
        assert ready["socket"]["outcome"] == "listening", ready["socket"]
        assert ready["runtime_root"] == str(tmp_path / "runtime")

        # 1. The probe: discovery + hello + version, as an operator types it.
        code, probe, output = _connect(env, "--probe")
        assert code == 0, output
        assert probe["ok"] is True
        assert probe["hello"]["event"] == "hello_ok"
        assert probe["hello"]["boot_id"] == ready["boot_id"]
        assert probe["hello"]["contract"] == ready["schema_version"]
        assert probe["target"]["port"] == ready["socket"]["port"]
        assert probe["target"]["source"] == "registry"
        assert probe["target"]["classification"] == "live"
        assert probe["version"]["event"] == "version"
        assert probe["version"]["transport"] == "socket"
        # A probe running from the same checkout is on the same build.
        assert probe["hello"]["build_mismatch"] in (False, None)
        # The token is never echoed by the client or the service.
        from agent_runtime.serve_auth import serve_auth_token_path

        token = (
            serve_auth_token_path(tmp_path / "runtime")
            .read_bytes()
            .decode()
            .strip()
        )
        assert token
        assert token not in json.dumps(probe)

        # 2. The drain, asked over the socket and watched to its terminal frame.
        code, drained, output = _connect(env, "--drain", "--deadline-seconds", "30")
        assert code == 0, output
        assert drained["drain_outcome"] == "drain_complete"
        events = [frame.get("event") for frame in drained["drain"]]
        assert events[0] == "draining"
        assert events[-1] == "drain_complete"

        # 3. The service really went: the child exits, and the lane it owned is
        # released — registry entry, socket lock, and owner sidecar all gone.
        assert process.wait(timeout=60) == 0
        from agent_runtime.serve_socket import socket_lock_path, socket_owner_path

        runtime = tmp_path / "runtime"
        assert list((runtime / "serve_instances").glob("*.json")) == []
        assert not socket_owner_path(runtime).exists()
        assert not socket_lock_path(runtime).exists()

        # 4. And a client that arrives afterwards is told there is nothing to
        # connect to, rather than hanging or inventing a target.
        code, gone, output = _connect(env, "--probe")
        assert code != 0, output
        assert gone["error"] == "no_socket_service"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
