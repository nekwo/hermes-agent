"""Stage 6's acceptance: two isolated roots on one machine, the whole ceremony.

The plan names this test by name — *"Test: two isolated roots on one machine
(probe-isolation machinery exists)"* — and everything else in this slice runs
against ONE serve, so this is the only place the stage's actual subject appears:
two installs, each with its own identity, its own certificate, its own
credential store, neither able to see the other's disk.

What is REAL here and what is not
----------------------------------

Real: two ``harness serve`` CHILD PROCESSES, each with its own runtime root and
its own gateway listener on its own port; the two operator verbs run as separate
CLI invocations against those roots, the way an operator would type them; a TLS
handshake with a pinned fingerprint; a pairing code that crosses between the two
installs only because this test carries it, exactly as a human would; and a
``peer.ping`` dialled A→B over the gateway listener with the credential the
ceremony minted.

Not real, and named rather than implied: **one machine.** Every listener binds
loopback. That is a config VALUE and not different code — the same ``bind()`` on
the same class with a host string the operator chose — but "install A reached
install B across a LAN" is unproven here and stays unproven until somebody runs
it on two machines. It is Stage 1's honest gap, unchanged, and this test does
not close it.

Why child processes rather than two ``serve_loop`` threads
-----------------------------------------------------------

A runtime root is resolved from the ENVIRONMENT, and an environment is
process-global. Two serve loops in one interpreter would race over
``HERMES_AGENT_RUNTIME_ROOT``: whichever booted last would own the ambient
value, and every later re-resolution inside the FIRST serve would answer for the
SECOND root. That is not a hypothetical — it is the same class of defect Stage
0's install-id inventory turned up, where two things that look like one root are
provably different scopes on this machine. Two processes make the isolation a
property of the operating system rather than of nobody having called
``store_root()`` at the wrong moment, which is exactly the guarantee this stage's
acceptance is supposed to be about.

The sandboxing is ``test_serve_socket_child_e2e.py``'s, for its reasons: a real
serve boot publishes the machine-global root anchor, so each child gets its own
``LOCALAPPDATA`` / ``HOME`` / ``USERPROFILE`` / ``HERMES_HOME`` beside its
temporary runtime root, and ``PYTHONPATH`` pins THIS checkout ahead of the
editable install.
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

#: Two real serve boots plus five CLI invocations, each a cold interpreter start
#: on Windows. The suite's global cap is 30s, which is right for a Python-level
#: hang and wrong for this; the sibling child-e2e file declares 300 for two
#: spawns and this one does more.
E2E_TEST_TIMEOUT_SECONDS = 600

#: Both halves SPAWN a real backend, so they are refused by the root conftest's
#: backend-spawn arm unless they say so. The spawn IS the claim here: the whole
#: file exists to prove two isolated installs pair and talk, which is not
#: answerable in one process (see the module docstring).
_REAL_CHILD_SPAWN = pytest.mark.live_system_guard_bypass


def _sandbox_env(base: Path) -> dict[str, str]:
    home = base / "home"
    local = base / "localappdata"
    for path in (home, local, base / "runtime"):
        path.mkdir(parents=True, exist_ok=True)
    # The gateway lane, turned on the way an operator's config would: a HOST
    # STRING (boolean `true` is refused by design) and port 0, so the kernel
    # picks and the `ready` frame publishes what it picked.
    (home / "config.yaml").write_bytes(
        b'remote_gateway:\n  listen: "127.0.0.1"\n  port: 0\n'
    )
    env = dict(os.environ)
    env.update(
        {
            "HERMES_AGENT_RUNTIME_ROOT": str(base / "runtime"),
            "HERMES_HOME": str(home),
            "LOCALAPPDATA": str(local),
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PYTHONPATH": str(REPO_ROOT) + os.pathsep + str(env.get("PYTHONPATH") or ""),
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


class _Install:
    """One serve child: its env, its process, and the facts its boot published."""

    def __init__(self, name: str, base: Path) -> None:
        self.name = name
        self.base = base
        self.env = _sandbox_env(base)
        self.process: subprocess.Popen | None = None
        self.ready: dict = {}

    @property
    def root(self) -> Path:
        return self.base / "runtime"

    @property
    def gateway_port(self) -> int:
        return int(self.ready["gateway"]["port"])

    def start(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-m", "hermes_cli.main", "harness", "serve", "--ndjson"],
            cwd=str(REPO_ROOT),
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self.ready = self._wait_for("ready")

    def _wait_for(self, event: str, timeout: float = BOOT_TIMEOUT_SECONDS) -> dict:
        assert self.process is not None
        deadline = time.monotonic() + timeout
        seen: list[str] = []
        while time.monotonic() < deadline:
            line = self.process.stdout.readline()
            if not line:
                raise AssertionError(
                    f"{self.name}: serve child ended before {event!r}; saw {seen}"
                )
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            seen.append(frame.get("event"))
            if frame.get("event") == event:
                return frame
        raise AssertionError(f"{self.name}: no {event!r} within {timeout}s; saw {seen}")

    def cli(self, *argv: str) -> tuple[int, dict | None, str]:
        """Run one operator verb against THIS install, as its own process."""

        completed = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "harness", *argv, "--json"],
            cwd=str(REPO_ROOT),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_SECONDS,
        )
        payload = None
        stdout = completed.stdout or ""
        start = stdout.find("{")
        if start >= 0:
            try:
                payload = json.loads(stdout[start:])
            except json.JSONDecodeError:
                payload = None
        return completed.returncode, payload, stdout + (completed.stderr or "")

    def python(self, source: str, *args: str) -> tuple[int, str]:
        """Run a snippet inside THIS install's environment.

        Used for the A→B dial, which has no CLI verb of its own in Stage 6 —
        ``peer.ping`` is the wire proof rather than an operator surface, and
        inventing a verb to make a test convenient would ship an operator door
        nobody asked for.

        ``*args`` land on the snippet's ``sys.argv`` (Stage 7, whose acceptance
        parameterises the same snippet over a target spelling and a method
        name). Variadic and defaulted to nothing, so the Stage 6 snippets that
        read ``sys.argv[1] if len(sys.argv) > 1`` behave exactly as they did.
        """

        completed = subprocess.run(
            [sys.executable, "-c", source, *args],
            cwd=str(REPO_ROOT),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_SECONDS,
        )
        return completed.returncode, (completed.stdout or "") + (completed.stderr or "")

    def stop(self) -> None:
        if self.process is None:
            return
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()


#: Dialled from install A's environment, reading A's own `peers.json` for the
#: address and the credential. Prints one JSON object so the test can assert on
#: the answer rather than on an exit code.
_PING_SOURCE = """
import json, sys
from agent_runtime import paths
from agent_runtime.gateway_peers import dial_peer

target = sys.argv[1] if len(sys.argv) > 1 else None
root = paths.store_root()
target = target or [p.peer_install_id for p in __import__(
    "agent_runtime.gateway_peers", fromlist=["list_peers"]).list_peers(root)][0]
connection, hello = dial_peer(root, target, timeout_seconds=30.0)
try:
    connection.send({"jsonrpc": "2.0", "id": "ping-1", "method": "peer.ping",
                     "params": {"echo": "two-roots"}})
    while True:
        frame = connection.read_frame()
        if frame is None:
            raise SystemExit("connection closed before a reply")
        if frame.get("id") == "ping-1":
            break
finally:
    connection.close()
print(json.dumps({"hello": hello, "reply": frame}))
"""

_RETIRE_SOURCE = _PING_SOURCE.replace('"method": "peer.ping"', '"method": "runtime.agent.retire"').replace(
    '"params": {"echo": "two-roots"}', '"params": {"persona_instance_id": "whatever"}'
)


@pytest.fixture
def two_installs(tmp_path):
    installs = [_Install("A", tmp_path / "a"), _Install("B", tmp_path / "b")]
    for install in installs:
        install.start()
    try:
        yield installs
    finally:
        for install in installs:
            install.stop()


def _payload_of(stdout_payload: dict) -> str:
    return stdout_payload["join_payload"]


@_REAL_CHILD_SPAWN
@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
def test_two_isolated_installs_pair_through_both_verbs_and_ping_across_the_edge(
    two_installs, tmp_path
):
    """The stage's acceptance, in the order an operator performs it."""

    a, b = two_installs

    # ── 0. Two installs, and they are genuinely two ──────────────────────────
    assert a.ready["runtime_root"] != b.ready["runtime_root"]
    assert a.ready["install"]["install_id"] != b.ready["install"]["install_id"]
    assert a.ready["gateway"]["outcome"] == "listening", a.ready["gateway"]
    assert b.ready["gateway"]["outcome"] == "listening", b.ready["gateway"]
    assert a.gateway_port != b.gateway_port
    # Two serves' one-owner locks are PER-ROOT, so there is no contention — the
    # plan's Stage 6 risk line, and the thing two boots on one machine would
    # otherwise have collided on.
    assert a.ready["socket"]["outcome"] == "listening"
    assert b.ready["socket"]["outcome"] == "listening"
    # Each certificate is its own, so a fingerprint pinned for one install
    # cannot authenticate the other.
    assert (
        a.ready["gateway"]["cert_fingerprint"] != b.ready["gateway"]["cert_fingerprint"]
    )

    # ── 1. The FIRST operator approval: A mints an invitation ────────────────
    code, minted, output = a.cli("gateway", "peers", "pair", "--note", "install B")
    assert code == 0, output
    assert minted["install_id"] == a.ready["install"]["install_id"]
    assert len(minted["peer_code"]) == 8
    # The endpoint is the LIVE one — A's running listener, on the ephemeral port
    # that exists nowhere else. This is the source that makes the ceremony work
    # without the operator pinning a port first.
    assert minted["endpoint"]["source"] == "live"
    assert minted["endpoint"]["port"] == a.gateway_port
    # Nothing is paired yet. A code is an invitation, and an invitation nobody
    # accepts leaves no row.
    _c, listed, _o = a.cli("gateway", "peers", "list")
    assert listed["items"] == []

    # ── 2. The SECOND operator approval: B accepts it, on the other install ──
    code, joined, output = b.cli(
        "gateway", "peers", "join", _payload_of(minted), "--timeout", "60"
    )
    assert code == 0, output
    assert joined["peer_install_id"] == a.ready["install"]["install_id"]
    assert joined["cert_fingerprint"] == a.ready["gateway"]["cert_fingerprint"]
    assert joined["endpoints"] == [{"host": "127.0.0.1", "port": a.gateway_port}]
    assert joined["revoked"] is False
    # …and the ack names what the OTHER side now holds about B, so one command
    # answers "is this edge symmetric".
    assert joined["this_install"]["install_id"] == b.ready["install"]["install_id"]
    assert joined["this_install"]["endpoints"] == [
        {"host": "127.0.0.1", "port": b.gateway_port}
    ]

    # ── 3. BOTH stores recorded the edge ─────────────────────────────────────
    _c, a_rows, _o = a.cli("gateway", "peers", "list")
    _c, b_rows, _o = b.cli("gateway", "peers", "list")
    assert [row["peer_install_id"] for row in a_rows["items"]] == [
        b.ready["install"]["install_id"]
    ]
    assert [row["peer_install_id"] for row in b_rows["items"]] == [
        a.ready["install"]["install_id"]
    ]
    # A recorded where to dial B from what B asserted at join time — which is
    # the ONLY way A could have learned it, since B's registry is on a root A
    # cannot read.
    assert a_rows["items"][0]["endpoints"] == [
        {"host": "127.0.0.1", "port": b.gateway_port}
    ]
    assert a_rows["items"][0]["cert_fingerprint"] == b.ready["gateway"][
        "cert_fingerprint"
    ]

    # ── 4. The secret was never printed by either verb ───────────────────────
    # The one frame that carries it is the join's `hello_ok`, and neither ack
    # renders it. Asserted against the STORED verifier, which is what an
    # attacker reading either operator's terminal scrollback would want.
    a_store = json.loads(
        (a.root / "gateway" / "peers.json").read_bytes().decode("utf-8")
    )["peers"][b.ready["install"]["install_id"]]
    b_store = json.loads(
        (b.root / "gateway" / "peers.json").read_bytes().decode("utf-8")
    )["peers"][a.ready["install"]["install_id"]]
    # One edge, one credential, both ends holding the same digest.
    assert a_store["secret_verifier"] == b_store["secret_verifier"]
    verifier = a_store["secret_verifier"]
    assert verifier not in json.dumps(minted)
    assert verifier not in json.dumps(joined)
    assert verifier not in json.dumps(a_rows)
    assert verifier not in json.dumps(b_rows)
    assert "secret_verifier" not in json.dumps(a_rows)

    # ── 5. peer.ping, A → B, over B's gateway listener ───────────────────────
    # Dialled from A's environment with A's row: the address comes from
    # `peers.json` and from nowhere else, which is the plan's Stage 6 risk line
    # — B's serve registry is on a root A cannot read, so a registry lookup is
    # not stale here, it is impossible.
    code, output = a.python(_PING_SOURCE)
    assert code == 0, output
    answer = json.loads(output.strip().splitlines()[-1])
    assert answer["hello"]["event"] == "hello_ok"
    # A reached B and not itself.
    assert answer["hello"]["install"]["install_id"] == b.ready["install"]["install_id"]
    assert answer["hello"]["transport"] == "gateway"
    assert answer["reply"]["result"]["pong"] is True
    assert answer["reply"]["result"]["echo"] == "two-roots"
    # B recognised A as the install A means to be — the question no client-side
    # check can answer.
    assert answer["reply"]["result"]["peer"] == a.ready["install"]["install_id"]

    # ── 6. …and the edge is exactly one verb wide ────────────────────────────
    code, output = a.python(_RETIRE_SOURCE)
    assert code == 0, output
    refused = json.loads(output.strip().splitlines()[-1])["reply"]
    assert refused["error"]["data"]["reason"] == "scope_denied"
    assert refused["error"]["data"]["caller"] == "peer"

    # ── 7. B's operator cuts the edge, and A is refused at the door ──────────
    code, revoked, output = b.cli(
        "gateway", "peers", "revoke", a.ready["install"]["install_id"]
    )
    assert code == 0, output
    assert revoked["revoked"] is True
    assert revoked["scope"] == "this_install_only"

    code, output = a.python(_PING_SOURCE)
    assert code != 0, output
    assert "no endpoint on" in output or "hello" not in output
    # A's own row is untouched — a revocation is one-sided, and B could not have
    # written into A's store even if it wanted to.
    _c, a_rows_after, _o = a.cli("gateway", "peers", "list")
    assert a_rows_after["items"][0]["revoked"] is False


@_REAL_CHILD_SPAWN
@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
def test_a_second_install_cannot_spend_the_code_a_first_one_redeemed(two_installs):
    """One-shot, across real installs: the join that arrives second is refused
    and writes nothing, so a code intercepted after the fact buys an attacker an
    edge with nobody."""

    a, b = two_installs

    _c, minted, _o = a.cli("gateway", "peers", "pair")
    code, _joined, output = b.cli(
        "gateway", "peers", "join", _payload_of(minted), "--timeout", "60"
    )
    assert code == 0, output

    # The same payload, replayed from the same install: the code is gone.
    code, _payload, output = b.cli(
        "gateway", "peers", "join", _payload_of(minted), "--timeout", "60"
    )

    assert code != 0
    assert "refused the join" in output
    # Exactly one edge on A's side, not two and not a replaced one.
    _c, a_rows, _o = a.cli("gateway", "peers", "list")
    assert len(a_rows["items"]) == 1
