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
            # The EXPLICIT head, exactly as the Launcher always starts serve
            # (`HERMES_HOME=profiles/<profile>`, `HERMES_HEAD_HOME=profiles/base`;
            # one profile here, so one directory). It is load-bearing rather
            # than decoration: ``publish_chat_head_home`` is a no-op for a
            # process that named no head, so without this the boot publishes no
            # chat-head pointer and every in-serve transcript read degrades to
            # the ambient rung — which is env-gated and refuses.
            #
            # S2b's ``peer.thread.read`` found that live: the far read came back
            # ``thread_unreadable / chat_scope_unresolved``, which is the
            # CORRECT failure (closed, typed, never an empty page) for a runtime
            # nobody told where the transcripts live. Setting the head here
            # makes the sandbox model the configuration that actually ships,
            # instead of one no launcher produces.
            "HERMES_HEAD_HOME": str(home),
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
    # D12 — the one MEASURED address on this ack. ``endpoints`` above is what B
    # DIALLED (its own candidate list, an inference); ``reached_at`` is what A's
    # kernel says the accepted connection actually landed on. They agree here
    # because both roots are on loopback, and the case the field exists for is
    # the one where they do not: a wildcard-bound install behind three
    # interfaces learns which of them carried a packet, and learns it from an
    # arrival rather than from a routing table it can only infer with.
    assert joined["reached_at"] == {"host": "127.0.0.1", "port": a.gateway_port}

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


@_REAL_CHILD_SPAWN
@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
def test_a_join_walks_past_an_unroutable_first_candidate_and_lands_on_the_second(
    two_installs,
):
    """R-D3 against two real serves: the edge lands even though the address the
    payload offers FIRST cannot be reached.

    A machine with several adapters cannot know which of its addresses the far
    side can reach — that is the whole reason it advertises a list rather than a
    choice — so the joining side has to walk it. Before this, ``peers join``
    dialled exactly one address and reported ``runtime_unavailable``, which is
    what S4's hardware attempt got at 12:00:13 with a perfectly healthy listener
    at the other end.

    ``192.0.2.1`` is TEST-NET-1 (RFC 5737): reserved for documentation, routed
    nowhere, and therefore a dial that fails the way a wrong LAN address fails
    rather than the way a closed port does. A's real payload is doctored rather
    than synthesised so everything else about it — the code, the fingerprint,
    the install id — is the one A actually minted.
    """

    a, b = two_installs

    _c, minted, _o = a.cli("gateway", "peers", "pair")
    payload = json.loads(_payload_of(minted))
    assert payload["endpoints"] == [{"host": "127.0.0.1", "port": a.gateway_port}]

    payload["endpoints"] = [
        {"host": "192.0.2.1", "port": a.gateway_port}
    ] + payload["endpoints"]
    # ``host``/``port`` stay equal to ``endpoints[0]`` — the contract the far
    # side reads — so the legacy keys point at the unroutable row too. A join
    # that only consulted them is exactly the join that fails here.
    payload["host"] = "192.0.2.1"

    code, joined, output = b.cli(
        "gateway", "peers", "join", json.dumps(payload), "--timeout", "5"
    )

    assert code == 0, output
    assert joined["peer_install_id"] == a.ready["install"]["install_id"]
    # The row holds the candidate that ANSWERED. Storing the payload's first row
    # would make every later dial from B start with a failure this run proved.
    assert joined["endpoints"] == [{"host": "127.0.0.1", "port": a.gateway_port}]


def _cache_row(install: "_Install", peer_install_id: str) -> dict:
    """One row of an install's ``peers_cache.json``, read off its own disk."""

    path = install.root / "gateway" / "peers_cache.json"
    assert path.exists(), f"{install.name} wrote no peer cache at all"
    payload = json.loads(path.read_bytes().decode("utf-8"))
    row = payload["peers"].get(peer_install_id)
    assert row is not None, (
        f"{install.name}'s cache holds {sorted(payload['peers'])} and not "
        f"{peer_install_id}"
    )
    return row


@_REAL_CHILD_SPAWN
@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
def test_a_completed_join_leaves_both_caches_saying_reachable(two_installs):
    """R-D16 across two real roots: a handshake is a reachability fact on BOTH
    sides of it.

    D3 run #2 measured the hole from the joining side. At 20:19:19 the join
    dialled the far listener, redeemed, stored the secret and emitted
    ``gateway.peer.recorded source=join`` — and the cache row for that peer
    still read ``unreachable``, from a failure at 18:03. ``note_dial_result``
    had exactly one caller, the chat lane's ``dial_peer``, so no ceremony had
    ever written the word. The launcher read the cache, called the edge
    unusable, and re-requested a pairing code every minute for an edge that was
    already up.

    The MINTING side had the same hole for the mirror reason: its row is
    written by ``redeem_peer_code`` inside the serve's hello authenticator,
    which is not a dial and did not record one either — even though an install
    that just completed TLS against this listener and spent a code minted here
    seconds ago is exactly as reached as one this side dialled.

    Two roots is the only place both halves are visible at once: A never runs a
    CLI join and B never runs a listener redemption, so a single-root test can
    only ever see one of them.
    """

    a, b = two_installs

    _c, minted, _o = a.cli("gateway", "peers", "pair")
    code, _joined, output = b.cli(
        "gateway", "peers", "join", _payload_of(minted), "--timeout", "60"
    )
    assert code == 0, output

    joiner = _cache_row(b, a.ready["install"]["install_id"])
    listener = _cache_row(a, b.ready["install"]["install_id"])

    assert joiner["reachability"] == "reachable", joiner
    assert listener["reachability"] == "reachable", listener
    # Cleared and not merely stamped beside the new word: "down since <t>"
    # under a row that reads reachable is the same wrong answer in a smaller
    # font, and the launcher's sheet renders that field.
    assert joiner["unreachable_since"] is None
    assert listener["unreachable_since"] is None

    # The word is in the SIDECAR. ``peers.json`` is the trust half and no cache
    # writer may open it — the property ``test_gateway_peers_store.py`` pins
    # from the inside, asserted here against what the two ceremonies actually
    # wrote to disk.
    for install in (a, b):
        trust = (install.root / "gateway" / "peers.json").read_bytes().decode("utf-8")
        assert "reachability" not in trust

    # …and the flip is on the log a stream consumer is watermark-gated on, from
    # each of the two processes that wrote it: B's CLI join, A's serve.
    for install in (a, b):
        code, output = install.python(
            """
import json
from agent_runtime.events import EventLog
print(json.dumps({"types": [e.type for e in EventLog().tail(50)]}))
"""
        )
        assert code == 0, output
        assert "gateway.peer.reachability" in _json_line(output)["types"], install.name


def _json_line(output: str) -> dict:
    """The last JSON object in a snippet's combined stdout+stderr.

    ``_Install.python`` concatenates both streams, and the runtime legitimately
    writes warnings to stderr (the SQLite WAL advisory fires once per process
    per database, and a seed that opens a SessionDB trips it AFTER printing).
    Taking the last line blindly reads that warning as the answer; scanning for
    the last decodable object reads the answer.
    """

    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    raise AssertionError(f"no JSON object in snippet output: {output[-400:]}")



# ══ S2 / S2b / S2c acceptance, across the same two real installs ═════════════


#: Seeded into B's sandbox config so B has ONE addressable agent for the roster
#: and the thread read to be about. Confirmed against ``agent_runtime/config.py``:
#: ``load_agent_runtime_config`` reads ``top["agent_runtime"]["personas"]``
#: (`:169`), ``persona_records_from_config`` builds an ``AgentPersona`` per KEY
#: (`:532-541`), ``ensure_persisted_personas`` merges that catalog under the
#: store (`:631-640`), and ``PersonaInstanceStore.ensure_for_personas`` turns
#: each into a canonical instance — which is what ``addressable_roster``
#: projects. No store write and no CLI call is needed: the config catalog IS a
#: persona for every reader in that chain.
_PERSONA_CONFIG = b"""agent_runtime:
  personas:
    dev:
      display_name: Dev
      role: dev
"""


def _seed_persona(install: "_Install") -> None:
    """Give one install an addressable agent, the way its own config would."""

    config = install.base / "home" / "config.yaml"
    config.write_bytes(config.read_bytes() + b"\n" + _PERSONA_CONFIG)


#: Mint a real persona chat thread on THIS install, with two messages in it, so
#: a far ``peer.thread.read`` has something to read. Runs in the install's own
#: environment, through the same durability door the chat lane uses — the point
#: is a REAL SessionDB row under the head this serve published, because §0.10's
#: fact 1 is that a transcript read INSIDE the serve process has no precedent.
_SEED_THREAD_SOURCE = """
import json, secrets
from agent_runtime.persona_assignments import (
    PersonaInstanceStore, canonical_chat_instance_id,
)
from agent_runtime.persona_chat_durability import (
    default_persona_session_db, ensure_persona_chat_session,
)
from agent_runtime.config import ensure_persisted_personas, load_agent_runtime_config

store = PersonaInstanceStore()
store.ensure_for_personas(list(ensure_persisted_personas(load_agent_runtime_config())))
handle = canonical_chat_instance_id("dev", None)
session_id = "persona_chat_" + handle + "_" + secrets.token_hex(6)

db = default_persona_session_db()
assert ensure_persona_chat_session(
    session_db=db, session_id=session_id, persona_id="dev", title="the far thread"
)
db.append_message(session_id, "user", "how did the build go?")
db.append_message(session_id, "assistant", "green on both lanes")
print(json.dumps({"handle": handle, "session_id": session_id}))
"""

#: Dialled from A with A's own peer row: one JSON-RPC call, one reply printed.
#: ``sys.argv[1]`` is the method and ``sys.argv[2]`` the params as JSON, so one
#: snippet serves the roster, the thread read and the announce.
_PEER_CALL_SOURCE = """
import json, sys
from agent_runtime import paths
from agent_runtime.gateway_peers import list_peers
from tools.agent_chat_remote import call_peer_method

root = paths.store_root()
target = [p.peer_install_id for p in list_peers(root)][0]
outcome = call_peer_method(
    root, target, sys.argv[1], json.loads(sys.argv[2]),
    dial_timeout=30.0, reply_timeout=30.0,
)
print(json.dumps(outcome))
"""

#: A's own directory tool, read from A's environment. Proves the tool and the
#: resolver agree about which installs exist, on real stores.
_INSTALLS_SOURCE = """
import json
from tools.agent_chat_tool import agent_chat_installs
print(agent_chat_installs())
"""

_CACHE_SOURCE = """
import json
from agent_runtime import paths
from agent_runtime.gateway_peers import read_peer_cache
print(json.dumps({k: v.payload() for k, v in read_peer_cache(paths.store_root()).items()}))
"""


@_REAL_CHILD_SPAWN
@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
def test_introduce_on_b_join_on_a_and_the_device_half_redeems(two_installs):
    """S2's acceptance, in the order the backend drives it.

    B runs ONE verb and prints one envelope; A joins from it with the account's
    attested fingerprint; the device half redeems against B as a phone would.
    Everything that follows — the expiry at both ends, the scoping refusal, the
    capabilities on the greeting — is asserted against two real serves rather
    than against a fake, because the whole point of ``introduce`` is that a
    machine on the other side of a grant can act on its output unattended.
    """

    a, b = two_installs
    a_install = a.ready["install"]["install_id"]

    # ── B introduces itself to A, for one account device, under one grant ────
    code, envelope, output = b.cli(
        "gateway",
        "introduce",
        "--for-install",
        a_install,
        "--for-device",
        "dev-acct-1",
        "--correlation",
        "g-two-roots-1",
    )
    assert code == 0, output
    assert envelope["kind"] == "gateway_introduction"
    assert envelope["install_id"] == b.ready["install"]["install_id"]
    assert envelope["correlation"] == "g-two-roots-1"
    assert envelope["endpoints_source"] == "live"
    assert envelope["cert_fingerprint"] == b.ready["gateway"]["cert_fingerprint"]
    # The launcher POSTs this object verbatim; its key set is the backend's.
    assert set(envelope["grant_payload"]) == {
        "peer_join_payload",
        "device_pair_payload",
        "install_id",
        "endpoints",
        "cert_fingerprint",
        "correlation",
    }

    # ── A joins with the fingerprint the ACCOUNT attests ─────────────────────
    code, joined, output = a.cli(
        "gateway",
        "peers",
        "join",
        envelope["grant_payload"]["peer_join_payload"],
        "--expect-fingerprint",
        b.ready["gateway"]["cert_fingerprint"],
        "--correlation",
        "g-two-roots-1",
        "--timeout",
        "60",
    )
    assert code == 0, output
    assert joined["peer_install_id"] == b.ready["install"]["install_id"]
    assert joined["fingerprint_attested"] is True
    assert joined["correlation"] == "g-two-roots-1"

    # ── ONE expiry, both ends ───────────────────────────────────────────────
    # A read it off the ``hello_ok.peered`` frame; B computed it at redemption.
    # Two ends that each derived their own would lapse minutes — or, with a
    # skewed clock, days — apart.
    _c, a_rows, _o = a.cli("gateway", "peers", "list")
    _c, b_rows, _o = b.cli("gateway", "peers", "list")
    a_row = a_rows["items"][0]
    b_row = next(
        row for row in b_rows["items"] if row["peer_install_id"] == a_install
    )
    assert a_row["expires_at"] == b_row["expires_at"]
    assert a_row["expires_at"] and a_row["expired"] is False
    assert a_row["usable"] is True
    # ~30 days out, as a window rather than an equality.
    from datetime import datetime, timezone

    lifetime = datetime.fromisoformat(a_row["expires_at"]) - datetime.now(timezone.utc)
    assert 29 * 86400 < lifetime.total_seconds() <= 30 * 86400

    # ── the device half redeems against B, as a phone would ─────────────────
    scanned = json.loads(envelope["grant_payload"]["device_pair_payload"])
    from agent_runtime.serve_socket import ServeSocketClient

    connection = ServeSocketClient(
        scanned["host"],
        int(scanned["port"]),
        timeout_seconds=30.0,
        tls=True,
        cert_fingerprint=scanned["cert_fingerprint"],
    )
    try:
        connection.connect()
        reply = connection.pair_hello(pairing_code=scanned["code"], client="the phone")
    finally:
        connection.close()
    assert reply["event"] == "hello_ok", reply
    assert reply["paired"]["tier"] == "console"

    _c, devices, _o = b.cli("gateway", "devices", "list")
    device = devices["items"][0]
    # The ACCOUNT's device id, carried onto the row as a label. It is the join
    # key an operator's sheet relates this row by, and nothing authenticates
    # against it — the row's own id was minted here.
    assert device["account_device_id"] == "dev-acct-1"
    assert device["device_id"].startswith("dev_")
    assert device["expires_at"] and device["expired"] is False

    # ── the edge works, and the greeting feature-detects ────────────────────
    code, output = a.python(_PING_SOURCE)
    assert code == 0, output
    answer = _json_line(output)
    assert answer["reply"]["result"]["pong"] is True
    assert answer["hello"]["gateway"]["capabilities"] == [
        "announce",
        "introduce",
        "roster",
        "thread_read",
    ]


@_REAL_CHILD_SPAWN
@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
def test_a_peer_code_scoped_to_one_install_is_refused_to_any_other_on_the_wire(
    two_installs, tmp_path
):
    """R-S2-4's scoping, proved where it matters: over a real handshake.

    B mints for A. A THIRD install spends the code — and is refused with the
    same words a code that never existed gets, so the wrong install cannot use
    the difference to learn a pairing is in flight. The code SURVIVES: A can
    still redeem it, because A's operator did nothing wrong.
    """

    a, b = two_installs
    c = _Install("C", tmp_path / "c")
    c.start()
    try:
        code, envelope, output = b.cli(
            "gateway", "introduce", "--for-install", a.ready["install"]["install_id"]
        )
        assert code == 0, output
        payload = envelope["grant_payload"]["peer_join_payload"]

        code, _joined, output = c.cli(
            "gateway", "peers", "join", payload, "--timeout", "60"
        )
        assert code != 0
        assert "refused the join" in output
        _c, rows, _o = b.cli("gateway", "peers", "list")
        assert [row["peer_install_id"] for row in rows["items"]] == []

        # …and the install it WAS for still gets its edge.
        code, joined, output = a.cli(
            "gateway", "peers", "join", payload, "--timeout", "60"
        )
        assert code == 0, output
        assert joined["peer_install_id"] == b.ready["install"]["install_id"]
    finally:
        c.stop()


@_REAL_CHILD_SPAWN
@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
def test_the_roster_and_one_far_thread_cross_the_wire_on_real_serves(tmp_path):
    """S2b's acceptance, and the proof §0.10 fact 1 asks for.

    **A transcript read inside the serve process had no precedent.** Every
    transcript read before S2b ran in a CLI or child process;
    ``persona_chat_session_messages`` resolves its store through
    ``resolve_chat_session_scope``, whose ambient rung is env-gated and whose
    head mismatch refuses. This test seeds a REAL thread on B, then reads it
    from A through ``peer.thread.read`` — so the read happens inside B's live
    serve, against the head B's own boot published, with the env var unset.

    The roster crosses first, because that is the order an operator works in:
    who is over there, then what did they say.
    """

    a = _Install("A", tmp_path / "a")
    b = _Install("B", tmp_path / "b")
    _seed_persona(b)
    a.start()
    b.start()
    try:
        _c, minted, output = b.cli("gateway", "peers", "pair")
        code, _joined, output = a.cli(
            "gateway", "peers", "join", _payload_of(minted), "--timeout", "60"
        )
        assert code == 0, output

        # ── who is on B ─────────────────────────────────────────────────────
        code, output = a.python(_PEER_CALL_SOURCE, "peer.roster.list", "{}")
        assert code == 0, output
        roster = _json_line(output)
        assert "refusal" not in roster, roster
        rows = roster["result"]["rows"]
        assert [row["persona_id"] for row in rows] == ["dev"], roster
        row = rows[0]
        # Exactly the projection fields — no session id, no transcript, no path.
        assert set(row) == {
            "handle",
            "persona_id",
            "label",
            "is_canonical_primary",
            "last_turn_at",
            "workspace_id",
        }
        assert roster["result"]["peer"] == a.ready["install"]["install_id"]

        # ── a real thread on B, then read from A ────────────────────────────
        code, output = b.python(_SEED_THREAD_SOURCE)
        assert code == 0, output
        seeded = _json_line(output)

        code, output = a.python(
            _PEER_CALL_SOURCE,
            "peer.thread.read",
            json.dumps({"target": "dev", "session_id": seeded["session_id"]}),
        )
        assert code == 0, output
        thread = _json_line(output)
        assert "refusal" not in thread, thread
        result = thread["result"]
        assert result["ok"] is True
        assert result["has_thread"] is True
        assert result["session_id"] == seeded["session_id"]
        assert [message["text"] for message in result["messages"]] == [
            "how did the build go?",
            "green on both lanes",
        ]
        assert result["peer"] == a.ready["install"]["install_id"]

        # ── and a session that is NOT that lane is refused, over the wire ───
        code, output = a.python(
            _PEER_CALL_SOURCE,
            "peer.thread.read",
            json.dumps(
                {"target": "dev", "session_id": "persona_chat_somebody_else_0123456789ab"}
            ),
        )
        assert code == 0, output
        refused = _json_line(output)
        assert refused["refusal"]["reason"] == "foreign_session", refused

        # ── A's directory tool sees B, and caches its roster ────────────────
        code, output = a.python(_INSTALLS_SOURCE)
        assert code == 0, output
        installs = _json_line(output)
        assert installs["count"] == 1
        assert installs["installs"][0]["install_id"] == (
            b.ready["install"]["install_id"]
        )
        # No ``usable`` key, and its absence is the contract: this list IS
        # ``usable_peers``, so every row in it is usable by construction and a
        # flag would be a column that is always true. ``peers list`` is the
        # surface that shows the unusable ones with their reason.
        assert installs["installs"][0]["ref"]
        assert installs["installs"][0]["reachability"] in {
            "unknown",
            "reachable",
            "unreachable",
        }
    finally:
        a.stop()
        b.stop()


@_REAL_CHILD_SPAWN
@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
def test_a_revoke_on_b_reaches_a_as_revoked_you_before_the_next_send(two_installs):
    """S2c's acceptance (R-IP12 E3 + R-S2-15), end to end.

    Before the announce edge, a revoke on the far side was indistinguishable
    from that install being down: the send was written, the dial was attempted,
    and the refusal arrived after the work. Here B's revoke tells A first — over
    the still-working edge, because the announce goes out BEFORE the local write
    — and A's very next resolution refuses deterministically, with a reason that
    names whose decision it was.
    """

    a, b = two_installs

    _c, minted, _o = b.cli("gateway", "peers", "pair")
    code, _joined, output = a.cli(
        "gateway", "peers", "join", _payload_of(minted), "--timeout", "60"
    )
    assert code == 0, output

    # A dials once so B knows where to reach it back (the hello refreshes B's
    # cache with A's own endpoints — the S2c edge that makes the announce
    # possible at all).
    code, output = a.python(_PING_SOURCE)
    assert code == 0, output

    code, revoked, output = b.cli(
        "gateway", "peers", "revoke", a.ready["install"]["install_id"]
    )
    assert code == 0, output
    assert revoked["revoked"] is True
    assert revoked["announced"] is True, revoked

    # A heard it — in its CACHE, and only there. B has no authority over A's
    # credential and did not touch it.
    code, output = a.python(_CACHE_SOURCE)
    assert code == 0, output
    cache = _json_line(output)
    b_id = b.ready["install"]["install_id"]
    assert cache[b_id]["revoked_you"] is True
    assert cache[b_id]["revoked_you_at"]
    _c, a_rows, _o = a.cli("gateway", "peers", "list")
    assert a_rows["items"][0]["revoked"] is False
    assert a_rows["items"][0]["usable"] is False
    assert a_rows["items"][0]["unusable_reason"] == "peer_revoked_you"

    # …and the next send refuses on that word, deterministically.
    code, output = a.python(
        """
import json
from agent_runtime import paths
from agent_runtime.gateway_targets import parse_install_target, resolve_install_target
from agent_runtime.gateway_peers import list_peers
target = [p.peer_install_id for p in list_peers(paths.store_root())][0]
outcome = resolve_install_target(paths.store_root(), parse_install_target("@" + target + "/dev"))
print(json.dumps({"reason": getattr(outcome, "reason", None)}))
"""
    )
    assert code == 0, output
    assert _json_line(output)["reason"] == "peer_revoked_you"


@_REAL_CHILD_SPAWN
@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
def test_a_cli_join_beside_a_running_serve_is_visible_with_no_restart(two_installs):
    """R-IP12 E1, proved rather than assumed.

    The join runs in its own CLI process while A's serve is up. Every peer write
    emits its event from the WRITING process (the ``realm.sync`` precedent), so
    the serve's own next read sees the row with no restart and no watcher — and
    the event is on the log a stream consumer is watermark-gated on.
    """

    a, b = two_installs

    _c, minted, _o = b.cli("gateway", "peers", "pair")
    code, _joined, output = a.cli(
        "gateway", "peers", "join", _payload_of(minted), "--timeout", "60"
    )
    assert code == 0, output

    # The SERVE — still the process that booted before the row existed —
    # answers about it, because nothing here is cached in memory.
    code, output = a.python(_INSTALLS_SOURCE)
    assert code == 0, output
    installs = _json_line(output)
    assert [row["install_id"] for row in installs["installs"]] == [
        b.ready["install"]["install_id"]
    ]

    # …and the event landed on A's own log, from the CLI process that wrote it.
    code, output = a.python(
        """
import json
from agent_runtime.events import EventLog
tail = [e.type for e in EventLog().tail(50)]
print(json.dumps({"recorded": "gateway.peer.recorded" in tail}))
"""
    )
    assert code == 0, output
    assert _json_line(output)["recorded"] is True
