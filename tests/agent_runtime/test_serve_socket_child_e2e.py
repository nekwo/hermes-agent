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


#: Both tests below SPAWN a real backend (``python -m hermes_cli.main harness
#: serve --ndjson``, plus the ``harness serve connect`` CLI verb in
#: :func:`_connect`), so they are refused by the root conftest's backend-spawn
#: arm (ML-14 / B20(i)) unless they say so. They say so here rather than being
#: converted, because the spawn IS the claim in both cases: the whole file
#: exists to prove that the verb an operator types finds the service and that a
#: drain asked from OUTSIDE is observed to completion — neither is answerable
#: in-process (see the module docstring). The child's roots are sandboxed by
#: :func:`_sandbox_env` — HERMES_AGENT_RUNTIME_ROOT, HERMES_HOME, LOCALAPPDATA,
#: HOME and USERPROFILE all land in ``tmp_path`` — which is what keeps a real
#: boot from publishing the machine-global root anchor, and it is the reason
#: this bypass is narrow rather than a hole. The marker drops the whole
#: live-system guard for these two tests, not just this arm; that granularity
#: is the guard's, not this file's.
_REAL_CHILD_SPAWN = pytest.mark.live_system_guard_bypass


def _spawn_serve(
    env: dict[str, str], *extra_args: str, detached_stdin: bool = False
) -> "_Child":
    """A real ``harness serve`` child.

    ``detached_stdin`` hands it the null device instead of a pipe, which is
    L-h's case stated literally: nothing will ever be written to this process's
    stdin, so the reader reaches EOF the moment the boot finishes. Everything
    before ``ready`` is unaffected — the reader is only iterated after the pool
    exists — so ``ready`` still arrives on stdout and is read here as usual.
    """

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
            stdin=subprocess.DEVNULL if detached_stdin else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        ),
        env,
    )


def _argv_over_socket(
    env: dict[str, str], port: int, argv: list[str], *, rid: str = "e2e-argv-1"
) -> tuple[dict, list[dict]]:
    """Run ONE argv request over the child's authenticated loopback socket.

    The real handshake against the real token file the child wrote, on the lane
    the launcher will use. "The detached runtime still EXECUTES" is a different
    claim from "it still answers a probe", and this is the one that proves it:
    the request pool the EOF path used to join has to still be there.
    """

    from agent_runtime.serve_auth import serve_auth_token_path
    from agent_runtime.serve_socket import ServeSocketClient

    root = Path(env["HERMES_AGENT_RUNTIME_ROOT"])
    token = serve_auth_token_path(root).read_bytes().decode().strip()
    assert token
    connection = ServeSocketClient("127.0.0.1", int(port), timeout_seconds=120.0)
    connection.connect()
    try:
        hello = connection.hello(token=token, client="e2e-argv", client_build=None)
        assert isinstance(hello, dict) and hello.get("event") == "hello_ok", hello
        connection.send({"id": rid, "argv": list(argv)})
        frames: list[dict] = []
        for _ in range(500):
            frame = connection.read_frame()
            if frame is None:
                break
            frames.append(frame)
            if frame.get("event") == "exit" and frame.get("id") == rid:
                return hello, frames
        raise AssertionError(
            f"no exit frame for {rid!r}; saw {[f.get('event') for f in frames]}"
        )
    finally:
        connection.close()


def _registry_rows(env: dict[str, str]) -> list[Path]:
    """The REGISTRY ROWS in ``serve_instances/`` — which is not everything in it.

    Since RL-16 the directory holds a second file shape, ``<pid>.ended.json``,
    the end-reason sidecar. It is written on the way out precisely so it
    OUTLIVES the row's removal, so a helper that means "the row is gone" has to
    say so; a bare ``*.json`` here turned every "registry entry gone" assertion
    in this file into "the runtime left no forensic record", which is the
    opposite claim. Same exclusion, same reason, as
    ``serve_registry.list_serve_instances``.
    """

    from agent_runtime.serve_registry import SERVE_ENDED_SUFFIX

    runtime = Path(env["HERMES_AGENT_RUNTIME_ROOT"])
    return [
        entry
        for entry in (runtime / "serve_instances").glob("*.json")
        if not entry.name.endswith(SERVE_ENDED_SUFFIX)
    ]


@_REAL_CHILD_SPAWN
@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
def test_a_killed_serves_successor_takes_the_lane_and_opens_the_listener(tmp_path):
    """L1's whole point, against two REAL processes and a real ``taskkill``.

    This is the operator's 2026-09-04 session reproduced end to end, and it is
    the one claim the in-process tests cannot make: the first serve is a real
    child holding a real OS lock, and it is KILLED — not shut down — so the
    sidecar it leaves behind is a genuine leftover rather than a file a test
    wrote. Its successor must then (a) take the socket lane over and say whose
    it was, and (b) actually open the LAN listener the config asked for, which
    is the door that never opened on the operator's machine.

    ``remote_gateway.listen`` is written into the sandbox home's ``config.yaml``
    rather than monkeypatched, because the config read is half of what failed:
    the launcher's write path was correct and the greeting still said the
    feature was off.
    """

    env = _sandbox_env(tmp_path)
    # Loopback and an ephemeral port. The LAN bind is this same config with a
    # different host string — what CI cannot exercise is a second machine and a
    # firewall prompt, and those are named in the field notes, not faked here.
    (Path(env["HERMES_HOME"]) / "config.yaml").write_text(
        "remote_gateway:\n  listen: 127.0.0.1\n  port: 0\n", encoding="utf-8"
    )

    first = _spawn_serve(env)
    try:
        first_ready = first.wait_for("ready")
        assert first_ready["socket"]["outcome"] == "listening"
        assert "took_over_from" not in first_ready["socket"]
        assert first_ready["gateway"]["outcome"] == "listening"
        first_pid = first_ready["pid"]

        # KILLED, not drained and not shut down: no atexit, no `release()`, no
        # unlink. The owner sidecar survives naming a pid that is about to stop
        # existing — which is exactly the state the launcher's un-awaited
        # respawn left behind.
        first.process.kill()
        first.process.wait(timeout=30)
        owner = json.loads(
            (Path(env["HERMES_AGENT_RUNTIME_ROOT"]) / "serve_socket.owner.json")
            .read_text(encoding="utf-8")
        )
        assert owner["pid"] == first_pid
    finally:
        if first.process.poll() is None:
            first.process.kill()
            first.process.wait(timeout=30)

    second = _spawn_serve(env)
    try:
        ready = second.wait_for("ready")
        # (a) The lane was taken over, and the receipt names the corpse.
        assert ready["socket"]["outcome"] == "listening"
        assert ready["socket"]["took_over_from"] == first_pid
        assert ready["socket"]["owner_started_at"] == first_ready["socket"]["started_at"]
        assert ready["pid"] != first_pid
        # (b) …and therefore the second door opened. Before L1 this boot read
        # `socket: lock_held_by` and `gateway: disabled`, and an operator who
        # had just enabled the listener was told the feature did not exist.
        assert ready["gateway"]["outcome"] == "listening"
        assert ready["gateway"]["host"] == "127.0.0.1"
        assert isinstance(ready["gateway"]["port"], int)
        assert ready["gateway"]["cert_fingerprint"]
        # The sidecar now names the living owner, so a THIRD boot inherits a
        # truthful file rather than the corpse's.
        owner = json.loads(
            (Path(env["HERMES_AGENT_RUNTIME_ROOT"]) / "serve_socket.owner.json")
            .read_text(encoding="utf-8")
        )
        assert owner["pid"] == ready["pid"]
    finally:
        if second.process.poll() is None:
            second.process.kill()
            second.process.wait(timeout=30)


@_REAL_CHILD_SPAWN
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
        # The ROW, not the directory: the RL-16 end-reason sidecar lives here
        # too and is written to survive exactly this removal (see
        # :func:`_registry_rows`, which this arm predates).
        assert _registry_rows({"HERMES_AGENT_RUNTIME_ROOT": str(runtime)}) == []
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


@_REAL_CHILD_SPAWN
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
                "proof": hello_proof(token, greeting["nonce"], port=port),
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
        assert answer["proof"] == hello_proof(token, greeting["nonce"], port=port)

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


# ── L-h: the service lifetime, against real processes ───────────────────────
#
# The seam tests (``test_serve_service_mode.py``) hold the same four outcomes in
# process, where the park can be inspected and the drain settles in
# milliseconds. These four are here because two of their premises cannot exist
# at that seam: a stdin that was NEVER a pipe (the null device, closed at spawn
# — which is how a detached launcher starts a runtime), and two processes
# contending for one OS lock. Neither file replaces the other.


@_REAL_CHILD_SPAWN
@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
def test_a_service_child_spawned_with_no_stdin_keeps_serving_and_drains_to_zero(
    tmp_path,
):
    """L-h item 4(a). The whole row, in the order an operator would meet it.

    Spawned with the null device on stdin, so EOF is not a thing that happens
    later — it is the first thing the reader sees. Before ``--service`` that
    ended the process outright: the pool joined, both lanes closed and the
    registry entry went, all before anybody could connect. Here the child says
    ``stdio_owner_detached`` and carries on, and the proof is that the operator
    verb finds it, an argv request EXECUTES on it, and the drain then ends it
    cleanly.
    """

    env = _sandbox_env(tmp_path)
    child = _spawn_serve(env, "--service", detached_stdin=True)
    try:
        ready = child.wait_for("ready")
        assert ready["socket"]["outcome"] == "listening", ready["socket"]
        assert ready["service"] is True
        assert isinstance(ready["starter_pid"], int)
        assert ready["ops"]["service"] is True
        # The registry row says the same thing to a client that has not
        # connected yet, which is the whole point of publishing it there.
        rows = _registry_rows(env)
        assert len(rows) == 1
        row = json.loads(rows[0].read_bytes())
        assert row["service"] is True
        assert row["starter_pid"] == ready["starter_pid"]
        assert row["port"] == ready["socket"]["port"]

        # 1. The verb an operator types still finds it, and says what it found.
        code, probe, output = _connect(env, "--probe")
        assert code == 0, output
        assert probe["ok"] is True
        assert probe["service"] is True
        assert probe["starter_pid"] == ready["starter_pid"]
        assert probe["hello"]["service"] is True
        assert probe["hello"]["boot_id"] == ready["boot_id"]
        assert probe["version"]["service"] is True
        assert probe["version"]["ops"]["service"] is True
        # Classified live by the registry's own read-time probe — the check the
        # in-process seam cannot make, because a serve running inside pytest is
        # not a process whose command line looks like a hermes serve.
        assert probe["target"]["classification"] == "live"

        # 2. It EXECUTES, not merely answers: the pool that EOF used to join.
        hello, frames = _argv_over_socket(
            env, ready["socket"]["port"], ["harness", "status", "--json"]
        )
        assert hello["service"] is True
        assert frames[-1] == {"id": "e2e-argv-1", "event": "exit", "code": 0}

        # 3. And the stop verb ends it, from outside, with nothing on stdin.
        code, drained, output = _connect(env, "--drain", "--deadline-seconds", "30")
        assert code == 0, output
        assert drained["drain_outcome"] == "drain_complete"

        assert child.process.wait(timeout=60) == 0
        assert _registry_rows(env) == []
    finally:
        if child.process.poll() is None:
            child.process.kill()
            child.process.wait(timeout=30)


@_REAL_CHILD_SPAWN
@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
def test_a_second_service_starter_names_the_winner_and_exits_without_serving(tmp_path):
    """L-h item 4(b), and F1's "never an extra stdio executor" stated as a test.

    Two real processes, one real OS lock, no arrangement: the loser is decided
    by the kernel. It must exit 0 (losing this race is the ORDINARY outcome of
    two starters — a launcher that respawned, a second launcher — and the
    caller's next act is to attach to the winner), it must name the winner's pid
    and port so the caller can do that without guessing, and it must leave
    nothing behind: no registry row, no ready frame, no request pool.
    """

    env = _sandbox_env(tmp_path)
    first = _spawn_serve(env, "--service", detached_stdin=True)
    try:
        first_ready = first.wait_for("ready")
        assert first_ready["socket"]["outcome"] == "listening"
        first_port = first_ready["socket"]["port"]

        loser = _spawn_serve(env, "--service", detached_stdin=True)
        try:
            exists = loser.wait_for("serve_owner_exists")
            assert exists["pid"] == first_ready["pid"]
            assert exists["port"] == first_port
            assert exists["socket"]["outcome"] == "lock_held_by"
            assert loser.process.wait(timeout=60) == 0
            # It never became a runtime: no ready frame anywhere in its output.
            assert "ready" not in [frame.get("event") for frame in loser.frames]
        finally:
            if loser.process.poll() is None:
                loser.process.kill()
                loser.process.wait(timeout=30)

        # ONE registry row for this root — the winner's — and the winner is
        # unharmed by the attempt.
        rows = _registry_rows(env)
        assert len(rows) == 1
        assert json.loads(rows[0].read_bytes())["pid"] == first_ready["pid"]

        code, probe, output = _connect(env, "--probe")
        assert code == 0, output
        assert probe["hello"]["boot_id"] == first_ready["boot_id"]
        assert probe["target"]["port"] == first_port
    finally:
        if first.process.poll() is None:
            first.process.kill()
            first.process.wait(timeout=30)


@_REAL_CHILD_SPAWN
@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
def test_a_stdio_shutdown_before_eof_still_ends_a_service_child(tmp_path):
    """L-h item 4(c). An ORDER is not an observation.

    ``--service`` splits the two things EOF used to mean; it does not take the
    stdio owner's verb away. A caller that still holds the pipe keeps exactly
    the behaviour Update / Repair already depend on, which is what makes the
    flag safe to turn on for them.
    """

    env = _sandbox_env(tmp_path)
    child = _spawn_serve(env, "--service")
    try:
        ready = child.wait_for("ready")
        assert ready["service"] is True

        child.process.stdin.write(json.dumps({"op": "shutdown"}) + "\n")
        child.process.stdin.flush()

        assert child.wait_for("shutdown")["pid"] == ready["pid"]
        assert child.process.wait(timeout=60) == 0
        # No detach receipt: the owner ordered a stop, it did not walk away.
        assert "stdio_owner_detached" not in [f.get("event") for f in child.frames]
        assert _registry_rows(env) == []
    finally:
        if child.process.poll() is None:
            child.process.kill()
            child.process.wait(timeout=30)


@_REAL_CHILD_SPAWN
@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
def test_without_service_a_child_still_ends_when_its_stdin_closes(tmp_path):
    """L-h item 4(d) — the regression arm, against a real process.

    ``--service`` is a lever and not a change of default. Every launcher and
    script that spawns ``harness serve --ndjson`` over pipes today still owns
    the child's lifetime, and closing the pipe still ends it: shutdown frame,
    exit 0, registry entry gone.
    """

    env = _sandbox_env(tmp_path)
    child = _spawn_serve(env)
    try:
        ready = child.wait_for("ready")
        assert ready["service"] is False
        assert isinstance(ready["starter_pid"], int)
        assert ready["ops"]["service"] is False
        assert len(_registry_rows(env)) == 1

        child.process.stdin.close()

        assert child.wait_for("shutdown")["pid"] == ready["pid"]
        assert child.process.wait(timeout=60) == 0
        assert _registry_rows(env) == []
    finally:
        if child.process.poll() is None:
            child.process.kill()
            child.process.wait(timeout=30)


def _dead_pid() -> int:
    """A pid that is provably gone: spawned, waited on, reaped."""

    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=30)
    return process.pid


@_REAL_CHILD_SPAWN
@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
def test_a_service_boots_and_writes_what_its_prune_removed_into_its_own_log(
    tmp_path,
):
    """RO-3, against the two real files it has to join.

    The 2026-09-06 field run could not say who removed the registry row for a
    runtime that had died unexplained: the boot that pruned it wrote nothing a
    person could read. Both halves of the fix are here and neither can be
    faked in process — the row is deleted by a REAL boot's prune, and the line
    lands in the file RL-17's ``DEVNULL`` stdio would otherwise have swallowed
    (``<pid>.stderr.log``, which exists only under ``--service``).

    *Killing mutation:* arm the stderr log AFTER the prune (its position before
    2026-09-06) and this row goes red — the events are written to a stderr
    nothing is reading, and the log carries no ``serve_registry_pruned`` line.
    """

    from agent_runtime.serve_registry import (
        SERVE_REGISTRY_PRUNED_EVENT,
        register_serve_instance,
        serve_instance_path,
    )

    env = _sandbox_env(tmp_path)
    runtime_root = Path(env["HERMES_AGENT_RUNTIME_ROOT"])
    wreckage = _dead_pid()
    # A row exactly like the one a hard-killed runtime leaves: written by the
    # real writer, and provably dead by the time the boot classifies it.
    assert register_serve_instance(runtime_root, pid=wreckage).registered is True
    assert serve_instance_path(runtime_root, wreckage).exists()

    child = _spawn_serve(env, "--service", detached_stdin=True)
    try:
        ready = child.wait_for("ready")
        assert ready["service"] is True
        # The prune ran: the dead row is gone and this boot's own row is not.
        assert not serve_instance_path(runtime_root, wreckage).exists()
        assert [path.stem for path in _registry_rows(env)] == [str(ready["pid"])]

        log = runtime_root / "serve_instances" / f"{ready['pid']}.stderr.log"
        assert log.is_file(), sorted(
            p.name for p in (runtime_root / "serve_instances").iterdir()
        )
        events = [
            json.loads(line)
            for line in log.read_text(encoding="utf-8").splitlines()
            if line.startswith("{") and SERVE_REGISTRY_PRUNED_EVENT in line
        ]
        assert len(events) == 1, log.read_text(encoding="utf-8")
        assert events[0] == {
            "event": SERVE_REGISTRY_PRUNED_EVENT,
            "action": "removed",
            "pid": wreckage,
            # The classifier's own two words, unchanged on the way to the log.
            "reason": "stale_dead_pid",
            "classification_reason": "pid_not_running",
            "by_pid": ready["pid"],
            "row_boot_id": events[0]["row_boot_id"],
            # Joins this line to this boot's own ``ready`` frame.
            "boot_id": ready["boot_id"],
        }
        assert isinstance(events[0]["row_boot_id"], str)
    finally:
        if child.process.poll() is None:
            child.process.kill()
            child.process.wait(timeout=30)
