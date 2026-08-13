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
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOT_TIMEOUT_SECONDS = 180.0
CLI_TIMEOUT_SECONDS = 180.0

#: The suite's global cap is 30s (``pyproject.toml`` addopts), which is right
#: for a Python-level hang and wrong for these two: each boots a REAL serve
#: child and then spawns the CLI verb two or three more times, and a cold
#: interpreter start on Windows is seconds each. Measured at ~25s on the
#: reference machine — inside 30 only by luck, and a cap a test passes by luck
#: is a flake generator, not a bound. The file already declares 180s budgets of
#: its own for exactly this reason; this makes the outer bound agree with them
#: instead of silently contradicting them.
E2E_TEST_TIMEOUT_SECONDS = 300


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


@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
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
        # The challenge it answered, carried on the report, so "which
        # contract did the thing that worked use" is never archaeology.
        from agent_runtime.serve_socket import HELLO_CONTRACT_VERSION, NONCE_BYTES

        assert probe["hello_contract"] == HELLO_CONTRACT_VERSION
        assert probe["server_hello"]["hello_contract"] == HELLO_CONTRACT_VERSION
        assert probe["server_hello"]["algorithm"] == "hmac-sha256"
        assert len(probe["server_hello"]["nonce"]) == 2 * NONCE_BYTES
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
        # The lock FILE survives on purpose — see SocketOwnerLock.release.
        # Unlinking it is a two-owner race on POSIX, where flock is held on
        # an open description rather than on a path. What must be gone is the
        # LOCK, and the proof of that is that it is immediately re-takeable.
        assert socket_lock_path(runtime).exists()
        from agent_runtime.serve_socket import SocketOwnerLock

        successor = SocketOwnerLock(runtime)
        assert successor.acquire().acquired is True
        successor.release()

        # 4. And a client that arrives afterwards is told there is nothing to
        # connect to, rather than hanging or inventing a target.
        code, gone, output = _connect(env, "--probe")
        assert code != 0, output
        assert gone["error"] == "no_socket_service"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)


def _raw_socket_exchange(port: int, answer_for) -> tuple[dict | None, list[dict], bytes]:
    """Speak the handshake to a REAL child by hand, over a raw socket.

    ``ServeSocketClient`` is deliberately not used here. It is the code under
    test on the client side, so driving the child with it would prove the two
    halves agree with each other and nothing about what actually crosses the
    wire. This assembles the bytes itself and returns them for inspection.

    ``answer_for`` receives the parsed ``server_hello`` and returns the object
    to send back, or None to send nothing.
    """

    sock = socket.create_connection(("127.0.0.1", int(port)), timeout=30.0)
    sock.settimeout(30.0)
    sent = b""
    frames: list[dict] = []
    try:
        stream = sock.makefile("rb")
        line = stream.readline()
        greeting = json.loads(line) if line.strip() else None
        answer = answer_for(greeting)
        if answer is not None:
            sent = (json.dumps(answer) + "\n").encode("utf-8")
            sock.sendall(sent)
        while True:
            line = stream.readline()
            if not line:
                break
            if not line.strip():
                continue
            frames.append(json.loads(line))
            break
        return greeting, frames, sent
    finally:
        try:
            sock.close()
        except OSError:
            pass


@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
def test_the_challenge_response_handshake_against_a_real_serve_child(tmp_path):
    """F1, end to end, against a process this test actually spawned.

    Everything else about the handshake is exercised in-process. This is the
    one place the whole thing is real: a real child, a real token file it wrote
    itself, a real loopback socket, and bytes assembled by hand. The claim being
    proven is the CRITICAL finding's second half — the token does not travel —
    and the only way to prove it is to hold the bytes and look.
    """

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
        assert ready["socket"]["outcome"] == "listening", ready["socket"]
        port = int(ready["socket"]["port"])

        from agent_runtime.serve_auth import serve_auth_token_path
        from agent_runtime.serve_socket import (
            HELLO_CONTRACT_VERSION,
            NONCE_BYTES,
            hello_proof,
        )

        runtime = tmp_path / "runtime"
        token = serve_auth_token_path(runtime).read_bytes().decode().strip()
        assert token

        # 1. The real exchange. The SERVER speaks first.
        transcript: dict = {}

        def _answer(greeting):
            transcript["server_hello"] = greeting
            return {
                "op": "hello",
                "client": "raw-e2e",
                "client_build": None,
                "proof": hello_proof(token, greeting["nonce"]),
            }

        greeting, frames, sent = _raw_socket_exchange(port, _answer)
        assert greeting["event"] == "server_hello"
        assert greeting["hello_contract"] == HELLO_CONTRACT_VERSION
        assert greeting["algorithm"] == "hmac-sha256"
        assert len(greeting["nonce"]) == 2 * NONCE_BYTES
        assert frames and frames[0]["event"] == "hello_ok", frames
        assert frames[0]["boot_id"] == ready["boot_id"]

        # THE assertion: the exact bytes this client put on the wire, checked
        # against the exact secret the child wrote to disk.
        assert token.encode() not in sent
        answer = json.loads(sent.decode().strip())
        assert "token" not in answer
        assert answer["proof"] == hello_proof(token, greeting["nonce"])

        # 2. The nonce is per CONNECTION, so that transcript is unreplayable.
        replayed_nonce = greeting["nonce"]
        second_greeting, replay_frames, _sent = _raw_socket_exchange(
            port,
            lambda g: (
                transcript.setdefault("second", g),
                {"op": "hello", "client": "replayer", "proof": answer["proof"]},
            )[1],
        )
        assert second_greeting["nonce"] != replayed_nonce
        assert replay_frames == [{"event": "hello_rejected", "reason": "bad_proof"}]

        # 3. The OLD contract — the raw token in the hello — is refused. There
        #    is no compatibility shim, on purpose: one would keep the cleartext
        #    lane open forever.
        _g, old_frames, old_sent = _raw_socket_exchange(
            port, lambda g: {"op": "hello", "client": "old", "token": token}
        )
        assert old_frames == [{"event": "hello_rejected", "reason": "bad_proof"}]
        assert token.encode() in old_sent  # the old client really did send it...
        # ...and it bought nothing.

        # 4. The service is unharmed by all of that, and the operator verb still
        #    works — the rejections above were not charged in a way that locks
        #    a legitimate client out (the live-proven F3 defect).
        code, probe, output = _connect(env, "--probe")
        assert code == 0, output
        assert probe["ok"] is True
        assert probe["hello"]["event"] == "hello_ok"
        assert probe["server_hello"]["hello_contract"] == HELLO_CONTRACT_VERSION
        assert probe["hello_contract"] == HELLO_CONTRACT_VERSION
        assert token not in json.dumps(probe)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
