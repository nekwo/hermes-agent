"""Stage 7's acceptance: two isolated roots, and a chat turn crossing between them.

The sibling of ``test_gateway_peer_two_roots_e2e``, and it reuses that file's
``_Install`` wholesale rather than growing a second harness — same two real
``harness serve`` children, same sandboxed roots, same operator verbs run as
separate CLI invocations, same reason for processes over threads (a runtime root
resolves from a process-global environment, so two serve loops in one
interpreter would answer for whichever booted last).

What is REAL here
-----------------

* Two installs that cannot see each other's disk, paired through both halves of
  the Stage 6 ceremony.
* The ``@install/target`` grammar resolved on install A against A's OWN
  ``peers.json`` — the display name and the install id both, with the id
  outranking the name.
* ``peer.agent_chat.execute`` dialled A→B over B's gateway listener with the
  credential the ceremony minted, authorized on B as caller kind ``peer``, and
  ACCEPTED — then the turn's own frames read back on the same socket to their
  exit, and the ``--json`` payload parsed out of them by the same
  ``parse_child_payload`` a local child's stdout goes through.
* B answering out of ITS OWN roster. The two roots are fresh, so the persona
  named does not exist on B and B refuses — which is the proof that matters
  here: the argv reached B's real mission-chat handler, in B's runtime, and was
  answered by B's state rather than by anything install A said.
* The edge staying exactly two verbs wide: ``runtime.chat.message`` — the
  console door onto the same service — is refused to a peer.
* R8's transport class, against a genuinely dead listener: B is stopped, and the
  remote leg dials its real recorded endpoint, exhausts the attempts cap, and
  settles the row with ``peer_unreachable``.
* The deterministic class, against a revoked edge: refused before a row exists.

What is NOT real, and named rather than implied
-----------------------------------------------

1. **One machine.** Every listener binds loopback. Stage 1's gap, inherited
   unchanged — a config VALUE, not different code, but "install A reached
   install B across a LAN" stays unproven.
2. **No model turn on B.** The two roots are empty, and running a real persona
   turn would need a provider this suite does not have. So what is proven is
   that the turn reached B's mission-chat handler and was answered by B's own
   admission — not that an agent on B composed a reply. The reply-forge into A's
   chat is likewise proven by the unit lane (``test_remote_dispatch_leg``) and
   by the delivery lane's own suites, not here.
"""

from __future__ import annotations

import json

import pytest

from tests.agent_runtime.test_gateway_peer_two_roots_e2e import (
    E2E_TEST_TIMEOUT_SECONDS,
    _Install,
    _payload_of,
    _REAL_CHILD_SPAWN,
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


#: Dialled from install A's environment. Resolves the `@install/target` spelling
#: through the SAME functions the tool uses, dials with the SAME `dial_peer` the
#: Stage 6 acceptance uses, and reads the turn's frames with the same rule the
#: supervisor's remote leg reads them by.
_EXECUTE_SOURCE = r"""
import json, sys
from agent_runtime.gateway_peers import dial_peer
from agent_runtime.gateway_targets import (
    parse_install_target,
    peer_store_root,
    resolve_install_target,
)
from tools.agent_chat_dispatch import SERVE_STDOUT_EVENT, parse_child_payload

spelling, turn_request_id, method = sys.argv[1], sys.argv[2], sys.argv[3]
root = peer_store_root()
parsed = parse_install_target(spelling)
resolved = resolve_install_target(root, parsed)
if not hasattr(resolved, "install_id"):
    print(json.dumps({"refused": {"reason": resolved.reason, "message": resolved.message}}))
    raise SystemExit(0)

params = {
    "turn_request_id": turn_request_id,
    "target": resolved.target,
    "message": "Stage 7 acceptance: take a turn on your own install.",
    "max_seconds": 60.0,
}
if method != "peer.agent_chat.execute":
    params = {"turn_request_id": turn_request_id, "persona_id": resolved.target,
              "message": params["message"]}

connection, hello = dial_peer(root, resolved.install_id, timeout_seconds=30.0)
ack = None
lines = []
turn_frames = []
exit_code = None
try:
    connection.send({"jsonrpc": "2.0", "id": "x1", "method": method, "params": params})
    while ack is None:
        frame = connection.read_frame()
        if frame is None:
            raise SystemExit("the edge closed before an ack")
        if frame.get("id") == "x1" and ("result" in frame or "error" in frame):
            ack = frame
    result = ack.get("result") or {}
    request_id = result.get("request_id")
    # A replay emits no frames: they went to the connection that asked.
    if request_id and not result.get("idempotent_replay"):
        connection.set_timeout(120.0)
        while True:
            frame = connection.read_frame()
            if frame is None:
                break
            if frame.get("id") != request_id:
                continue
            turn_frames.append(frame)
            if frame.get("event") == SERVE_STDOUT_EVENT:
                lines.append(frame.get("line") or "")
            elif frame.get("event") == "exit":
                exit_code = frame.get("code")
                break
finally:
    connection.close()

print(json.dumps({
    "resolved": {
        "install_id": resolved.install_id,
        "display_name": resolved.display_name,
        "target": resolved.target,
    },
    "hello_install": (hello.get("install") or {}).get("install_id"),
    "ack": ack,
    "exit_code": exit_code,
    "payload": parse_child_payload("\n".join(lines)),
    "stdout_lines": lines[-40:],
    # Every frame the turn produced, event names included. Carried so a failure
    # here can say WHY the payload is missing rather than only that it is —
    # which is the difference between "the reader collected nothing" and "the
    # turn printed nothing", and is how the `line`-vs-`stdout` defect was found.
    "turn_frames": turn_frames[-40:],
}))
"""

#: Run in A's environment with B stopped. Records a real dispatch row and drives
#: the supervisor's remote leg against B's REAL recorded endpoint, which is now
#: a dead port. The backoff is flattened so the acceptance converges in seconds
#: instead of two minutes — the CAP is the thing under test, not the wall clock.
_UNREACHABLE_SOURCE = r"""
import json, sys
from agent_runtime import dispatch_store
from agent_runtime.gateway_targets import (
    parse_install_target,
    peer_store_root,
    resolve_install_target,
)
from tools import agent_chat_dispatch

agent_chat_dispatch.PEER_RETRY_BACKOFF_SECONDS = 0.0
agent_chat_dispatch.PEER_DIAL_TIMEOUT_SECONDS = 2.0

resolved = resolve_install_target(peer_store_root(), parse_install_target(sys.argv[1]))
dispatch_id = "dispatch-accept01"
dispatch_store.record_dispatch(
    dispatch_id=dispatch_id,
    sender_session_id="persona_chat_personainst_sender",
    target_persona=sys.argv[1],
    ask="Stage 7 acceptance: an install that is not there.",
    remote_install_id=resolved.install_id,
)
agent_chat_dispatch._run_remote_dispatch(
    dispatch_id,
    {
        "client_message_id": "agent-dispatch-" + dispatch_id,
        "remote_install_id": resolved.install_id,
        "remote_display_name": resolved.display_name,
        "remote_target": resolved.target,
        "message": "Stage 7 acceptance: an install that is not there.",
        "max_seconds": 30.0,
    },
)
row = dispatch_store.get_dispatch(dispatch_id)
print(json.dumps({"row": row, "cap": dispatch_store.MAX_DELIVERY_ATTEMPTS}))
"""


def _last_json(output: str) -> dict:
    """The last JSON object in a child's combined output.

    Scanned from the end rather than taken as the last LINE, for the reason
    ``parse_child_payload`` exists: a cold interpreter in this repo legitimately
    prints other things around the payload — the SQLite WAL advisory lands on
    stderr after it — and a test that assumed the payload was last would fail on
    an unrelated warning.
    """

    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise AssertionError(f"no JSON object in child output:\n{output[-4000:]}")


@_REAL_CHILD_SPAWN
@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
def test_a_chat_turn_crosses_an_operator_approved_install_edge(two_installs):
    """The stage's acceptance, in the order the lane performs it."""

    a, b = two_installs
    b_name = b.ready["install"]["display_name"]

    # ── 0. Two installs, and they are genuinely two ──────────────────────────
    assert a.ready["install"]["install_id"] != b.ready["install"]["install_id"]
    assert a.ready["gateway"]["outcome"] == "listening", a.ready["gateway"]
    assert b.ready["gateway"]["outcome"] == "listening", b.ready["gateway"]

    # ── 1. The Stage 6 ceremony, both operator halves ────────────────────────
    code, minted, output = a.cli("gateway", "peers", "pair", "--note", "install B")
    assert code == 0, output
    code, joined, output = b.cli(
        "gateway", "peers", "join", _payload_of(minted), "--timeout", "60"
    )
    assert code == 0, output
    assert joined["peer_install_id"] == a.ready["install"]["install_id"]

    # ── 2. A cross-install target resolves on A, against A's own store ───────
    # …and nothing here reached B: the name, the id and the endpoint all come
    # from the row the ceremony wrote.
    _c, a_rows, _o = a.cli("gateway", "peers", "list")
    row = a_rows["items"][0]
    assert row["peer_install_id"] == b.ready["install"]["install_id"]

    # ── 3. peer.agent_chat.execute, A → B, over B's gateway listener ─────────
    code, output = a.python(
        _EXECUTE_SOURCE,
        f"@{b_name}/dev",
        "agent-dispatch-dispatch-accept00",
        "peer.agent_chat.execute",
    )
    assert code == 0, output
    answer = _last_json(output)

    # The grammar resolved to the install the ceremony paired, by NAME.
    assert answer["resolved"]["install_id"] == b.ready["install"]["install_id"]
    assert answer["resolved"]["target"] == "dev"
    # A reached B and not itself.
    assert answer["hello_install"] == b.ready["install"]["install_id"]

    # The ack is an ACCEPT — B's dispatcher answers the method lane inline, so
    # it cannot run a turn there — and it names the install B PROVED about A.
    ack = answer["ack"]["result"]
    assert ack["accepted"] is True
    assert ack["peer"] == a.ready["install"]["install_id"]
    assert ack["request_id"].startswith("chat-")
    assert ack["idempotent_replay"] is False

    # …and the turn then ran on B and answered out of B's OWN roster. These two
    # roots are fresh, so `dev` does not exist there — which is the proof: the
    # argv reached B's real mission-chat handler and was answered by B's state.
    assert answer["exit_code"] == 2, answer["stdout_lines"]
    payload = answer["payload"]
    assert payload is not None, json.dumps(answer["turn_frames"])[:2000]
    assert payload["capability_id"] == "mission.chat.message"
    assert payload["ok"] is False
    assert payload["error_kind"] == "unsupported_persona"
    assert payload["error"] == "unknown persona dev"
    assert payload["persona_id"] == "dev"

    # ── 3b. …and the SAME turn_request_id replays rather than running twice ──
    # This is the property R8's retry posture rests on, proven on the real wire
    # rather than against a fake connection: a dial that dies after B accepted
    # the turn is retried with the same key, and B answers out of its receipt.
    code, output = a.python(
        _EXECUTE_SOURCE,
        f"@{b_name}/dev",
        "agent-dispatch-dispatch-accept00",
        "peer.agent_chat.execute",
    )
    assert code == 0, output
    replay = _last_json(output)["ack"]["result"]
    assert replay["idempotent_replay"] is True
    assert replay["request_id"] == ack["request_id"]

    # ── 4. The edge is still exactly two verbs wide ──────────────────────────
    code, output = a.python(
        _EXECUTE_SOURCE,
        f"@{b_name}/dev",
        "agent-dispatch-dispatch-accept02",
        "runtime.chat.message",
    )
    assert code == 0, output
    refused = _last_json(output)["ack"]
    assert refused["error"]["data"]["reason"] == "scope_denied"
    assert refused["error"]["data"]["caller"] == "peer"

    # ── 5. The credential never appeared in anything either side printed ─────
    verifier = json.loads(
        (a.root / "gateway" / "peers.json").read_bytes().decode("utf-8")
    )["peers"][b.ready["install"]["install_id"]]["secret_verifier"]
    assert verifier not in json.dumps(minted)
    assert verifier not in json.dumps(joined)
    assert verifier not in json.dumps(a_rows)
    assert verifier not in output
    assert verifier not in json.dumps(answer)


@_REAL_CHILD_SPAWN
@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
def test_an_install_that_stops_answering_converges_to_peer_unreachable(two_installs):
    """R8's transport class, against a genuinely dead listener rather than a
    stubbed dial: B's serve is stopped and its recorded endpoint stays in A's
    row, which is exactly the condition an install that was switched off
    produces."""

    a, b = two_installs
    b_name = b.ready["install"]["display_name"]

    _c, minted, output = a.cli("gateway", "peers", "pair")
    code, _joined, output = b.cli(
        "gateway", "peers", "join", _payload_of(minted), "--timeout", "60"
    )
    assert code == 0, output

    b.stop()

    code, output = a.python(_UNREACHABLE_SOURCE, f"@{b_name}/dev")
    assert code == 0, output
    answer = _last_json(output)
    row = answer["row"]

    # NOT a dropped delivery: the sender is owed this fact, and it is armed for
    # delivery like any other completion.
    assert row["state"] == "error"
    assert row["delivery_state"] == "pending"
    assert row["result"]["remote"]["reason"] == "peer_unreachable"
    assert row["result"]["remote"]["attempts"] == answer["cap"]
    assert row["result"]["remote"]["install_id"] == b.ready["install"]["install_id"]
    assert "peer_unreachable" in row["result"]["error"]


@_REAL_CHILD_SPAWN
@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
def test_a_revoked_edge_is_refused_before_a_row_exists(two_installs):
    """The deterministic class. A revocation is one-sided, so this is A's own
    row being cut — and the send is refused at resolution, which is what makes
    it cost no attempt."""

    a, b = two_installs
    b_name = b.ready["install"]["display_name"]

    _c, minted, _o = a.cli("gateway", "peers", "pair")
    code, _joined, output = b.cli(
        "gateway", "peers", "join", _payload_of(minted), "--timeout", "60"
    )
    assert code == 0, output

    code, revoked, output = a.cli(
        "gateway", "peers", "revoke", b.ready["install"]["install_id"]
    )
    assert code == 0, output
    assert revoked["revoked"] is True

    code, output = a.python(
        _EXECUTE_SOURCE, f"@{b_name}/dev", "agent-dispatch-dispatch-accept03", "peer.ping"
    )
    assert code == 0, output
    answer = _last_json(output)
    assert answer["refused"]["reason"] == "peer_revoked"
    # …and B is still up, so this is a verdict about the ROW rather than about
    # reachability — which is the distinction R8 rests on.
    assert b.ready["gateway"]["outcome"] == "listening"
