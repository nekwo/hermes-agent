"""Stage P4's acceptance: a device on install A opens a picture that only B has.

Two real serve children, two real gateway listeners, two real certificates, two
real credential stores, real TLS with a real pin, real HMAC over a real paired
credential, and real PNG bytes that exist on exactly one of the two roots. The
transport is faked nowhere, for the reason ``test_gateway_media_fetch_e2e``
gives about its own single-root version: the claims worth pinning — that the
bytes a device receives are the bytes on the OTHER install's disk, that a second
fetch spends no dial, and that a peer edge is what carries it — are exactly the
ones a fake transport cannot fail.

The harness is imported from ``test_gateway_peer_two_roots_e2e`` rather than
copied, for its reason: a second pairing ceremony is a second chance for the two
to drift apart while both stay green.

What is REAL here, and what is not
-----------------------------------

Real: everything above, plus the fetch crossing an actual TLS socket between two
processes that cannot read each other's disks.

Not real, and named rather than implied, twice:

* **One machine.** Stage 1's inherited gap, unchanged: both listeners bind
  loopback. "Install A reached install B across a LAN" stays unproven until
  somebody runs the O2 session on two boxes.
* **The dispatch that produced the map is SYNTHESISED.** A real cross-install
  reply needs a provider turn on B, which no test may depend on. So this file
  writes onto A's dispatch store exactly the row
  ``agent_chat_dispatch._run_remote_dispatch`` writes — and that supervisor's
  own write of it is pinned separately, on the real payload shape, by
  ``test_cross_install_media.test_the_remote_leg_puts_the_far_installs_map_on_the_row``.
  What this file proves is everything downstream of that row, which is the half
  that crosses a wire.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from tests.agent_runtime.test_gateway_peer_two_roots_e2e import (  # noqa: F401
    E2E_TEST_TIMEOUT_SECONDS,
    _REAL_CHILD_SPAWN,
    _payload_of,
    two_installs,
)

#: Bytes that are not a decodable PNG and do not have to be — nothing in this
#: lane decodes an image. What they have to be is bytes that survive a base64
#: round trip over a real socket unchanged, so they carry every value a byte
#: can take.
PROOF_BYTES_SOURCE = "b'\\x89PNG\\r\\n\\x1a\\n' + bytes(range(256)) * 40"
PROOF_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 40
PROOF_HANDLE = "sha256:" + hashlib.sha256(PROOF_BYTES).hexdigest()
GUESSED_HANDLE = "sha256:" + "9" * 64


#: Run on install B. Puts a real image on B's disk and declares it in B's OWN
#: live-log mirror — the store the media scope is DERIVED from — then reports
#: what B's own derivation minted for it. Nothing here writes a registry,
#: because there is no registry: the scope is a derivation and the whole design
#: rests on that.
_SEED_ON_B = f"""
import json, sys
from pathlib import Path
from agent_runtime import chat_live_log, media_handles

root = chat_live_log.capture_chat_live_log_root()
if root is None:
    raise SystemExit("B could not resolve its live-log root")
root.mkdir(parents=True, exist_ok=True)

shot = root.parent / "artifacts" / "only-on-b.png"
shot.parent.mkdir(parents=True, exist_ok=True)
shot.write_bytes({PROOF_BYTES_SOURCE})

(root / "persona_chat_b.jsonl").write_text(
    json.dumps({{
        "ts": "2026-09-01T00:00:00Z",
        "kind": "message",
        "role": "agent",
        "text": "ran the suite\\n\\nMEDIA:" + str(shot) + "\\n\\nall green",
    }}) + "\\n",
    encoding="utf-8",
)

scope = media_handles.build_media_scope()
rows = [a.describe() | {{"path": str(a.path)}} for a in scope.artifacts.values()]
print(json.dumps({{"root": str(root), "shot": str(shot), "artifacts": rows}}))
"""


#: Run on install A. Writes the completion row a cross-install dispatch leaves
#: behind — the same shape ``_run_remote_dispatch`` writes, carrying the map B
#: minted — so A's media scope gains a REMOTE row for a file it cannot read.
_RECORD_ON_A = """
import json, sys
from agent_runtime import dispatch_store, media_handles

peer_install_id, reference, handle, size = sys.argv[1:5]
dispatch_store.record_dispatch(
    dispatch_id="dispatch-p4",
    sender_session_id="persona_chat_personainst_neko_aaaaaaaaaaaa",
    target_persona="@b/dev",
    ask="take a screenshot",
    remote_install_id=peer_install_id,
)
dispatch_store.record_completion(
    "dispatch-p4",
    state=dispatch_store.STATE_COMPLETED,
    reply="ran the suite\\n\\nMEDIA:" + reference,
    remote={"install_id": peer_install_id, "attempts": 1},
    media=[{
        "reference": reference,
        "handle": handle,
        "media_type": "image/png",
        "size_bytes": int(size),
    }],
)
scope = media_handles.build_media_scope()
print(json.dumps({
    "completions": [c["peer_install_id"] for c in dispatch_store.remote_media_completions()],
    "remote": [a.describe() for a in scope.remote.values()],
}))
"""


#: Run on install A. Pairs a device with A the way an operator would, dials A's
#: OWN gateway listener over real TLS with the fingerprint A published, and
#: spends the handles it is given. Every fetch A answers here for a remote row
#: costs A a dial to B — which is the thing under test.
_DEVICE_ON_A = """
import base64, hashlib, json, sys
from agent_runtime import paths
from agent_runtime.serve_gateway_auth import mint_pairing_code, redeem_pairing_code
from agent_runtime.serve_socket import ServeSocketClient

port, fingerprint, tier = int(sys.argv[1]), sys.argv[2], sys.argv[3]
handles = sys.argv[4:]

root = paths.store_root()
code = mint_pairing_code(root, tier=tier, name="the phone")
credential = redeem_pairing_code(root, code.code)

connection = ServeSocketClient(
    "127.0.0.1", port, timeout_seconds=90.0, tls=True, cert_fingerprint=fingerprint
)
connection.connect()
answers = {}
try:
    hello = connection.device_hello(
        device_id=credential.device_id, token=credential.token, client="phone"
    )

    def call(rid, method, params):
        connection.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            frame = connection.read_frame()
            if frame is None:
                raise SystemExit("the edge closed before a reply to " + method)
            if frame.get("id") == rid:
                return frame

    answers["index"] = call("i1", "runtime.media.index", {})
    for position, handle in enumerate(handles):
        frame = call("g%d" % position, "runtime.media.get", {"handle": handle})
        result = frame.get("result")
        if result and result.get("data"):
            result["sha256"] = hashlib.sha256(
                base64.b64decode(result["data"])
            ).hexdigest()
            result["data"] = "<%d base64 chars>" % len(result["data"])
        answers["get_%d" % position] = frame
finally:
    connection.close()
print(json.dumps({"hello": hello, "answers": answers}))
"""


def _last_json(output: str) -> dict:
    """The last complete JSON object a snippet printed.

    A child's stdout legitimately carries other lines (a SQLite WAL advisory,
    a provider warning), so this is a decode-from-every-brace scan rather than
    a line read — ``agent_chat_dispatch.parse_child_payload``'s shape, for its
    reason.
    """

    decoder = json.JSONDecoder()
    found = None
    index = output.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(output, index)
        except ValueError:
            index = output.find("{", index + 1)
            continue
        if isinstance(value, dict):
            found = value
        index = output.find("{", max(end, index + 1))
    assert found is not None, output[-4000:]
    return found


@_REAL_CHILD_SPAWN
@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
def test_a_device_on_A_opens_a_picture_that_exists_only_on_B(two_installs):
    """Ruling R-P3's acceptance, in the order the machinery performs it."""

    a, b = two_installs
    a_id = a.ready["install"]["install_id"]
    b_id = b.ready["install"]["install_id"]

    # ── 0. Two installs, genuinely two, both listening ───────────────────────
    assert a.ready["runtime_root"] != b.ready["runtime_root"]
    assert a.ready["gateway"]["outcome"] == "listening", a.ready["gateway"]
    assert b.ready["gateway"]["outcome"] == "listening", b.ready["gateway"]

    # ── 1. The two-approval ceremony, so A may dial B ────────────────────────
    code, minted, output = a.cli("gateway", "peers", "pair", "--note", "install B")
    assert code == 0, output
    code, joined, output = b.cli(
        "gateway", "peers", "join", _payload_of(minted), "--timeout", "60"
    )
    assert code == 0, output
    assert joined["peer_install_id"] == a_id

    # ── 2. B holds the bytes, and B mints the handle for them ────────────────
    # Nobody else can: a handle is a digest, and this file exists on B's root.
    code, output = b.python(_SEED_ON_B)
    assert code == 0, output
    seeded = _last_json(output)
    assert len(seeded["artifacts"]) == 1, seeded
    artifact = seeded["artifacts"][0]
    assert artifact["handle"] == PROOF_HANDLE
    assert artifact["size_bytes"] == len(PROOF_BYTES)
    reference = seeded["shot"]

    # …and the file is on B's root and NOT on A's. The whole problem, stated.
    assert not (a.root.parent / "artifacts" / "only-on-b.png").exists()

    # ── 3. The completion lands on A carrying the map ────────────────────────
    code, output = a.python(
        _RECORD_ON_A, b_id, reference, PROOF_HANDLE, str(len(PROOF_BYTES))
    )
    assert code == 0, output
    recorded = _last_json(output)
    assert recorded["completions"] == [b_id]
    assert recorded["remote"] == [
        {
            "handle": PROOF_HANDLE,
            "reference": reference,
            "media_type": "image/png",
            "size_bytes": len(PROOF_BYTES),
            "fetchable": True,
            "remote": True,
            "peer_install_id": b_id,
        }
    ]

    # ── 4. A console device on A indexes, fetches, and gets B's bytes ────────
    code, output = a.python(
        _DEVICE_ON_A,
        str(a.gateway_port),
        a.ready["gateway"]["cert_fingerprint"],
        "console",
        PROOF_HANDLE,
        GUESSED_HANDLE,
        reference,
    )
    assert code == 0, output
    session = _last_json(output)
    assert session["hello"]["event"] == "hello_ok", session["hello"]
    answers = session["answers"]

    # The index names the remote row and claims NO local path for it.
    rows = answers["index"]["result"]["artifacts"]
    assert rows == [
        {
            "handle": PROOF_HANDLE,
            "reference": reference,
            "media_type": "image/png",
            "size_bytes": len(PROOF_BYTES),
            "fetchable": True,
            "remote": True,
            "peer_install_id": b_id,
        }
    ]
    assert answers["index"]["result"]["scanned"]["completions"] == 1

    # THE claim: the bytes a device on A received are the bytes on B's disk,
    # carried over a peer edge two operators approved, and they hash to the
    # handle B minted — which is what makes the verification free.
    fetched = answers["get_0"]["result"]
    assert fetched["handle"] == PROOF_HANDLE
    assert fetched["media_type"] == "image/png"
    assert fetched["size_bytes"] == len(PROOF_BYTES)
    assert fetched["sha256"] == hashlib.sha256(PROOF_BYTES).hexdigest()
    # A proxied artifact is indistinguishable from a local one on the wire.
    assert "peer_install_id" not in fetched

    # A guessed digest is unknown on the near hop, and a PATH never becomes an
    # argument this lane accepts — both unchanged by the remote half existing.
    assert answers["get_1"]["error"]["data"] == {"reason": "unknown_handle"}
    assert answers["get_2"]["error"]["data"] == {"reason": "handle_invalid"}

    # ── 5. THE CACHE: B is switched off, and the picture still opens ─────────
    # The strongest form of "a second fetch costs zero peer dials" available —
    # not a counter this test could have miscounted, but B's serve gone.
    b.stop()

    code, output = a.python(
        _DEVICE_ON_A,
        str(a.gateway_port),
        a.ready["gateway"]["cert_fingerprint"],
        "console",
        PROOF_HANDLE,
    )
    assert code == 0, output
    cached = _last_json(output)["answers"]["get_0"]["result"]
    assert cached["sha256"] == hashlib.sha256(PROOF_BYTES).hexdigest()

    # ── 6. …and an UNCACHED remote handle now converges on the honest word ───
    other = "sha256:" + "7" * 64
    code, output = a.python(
        _RECORD_ON_A.replace("dispatch-p4", "dispatch-p4b"),
        b_id,
        reference.replace("only-on-b.png", "second-on-b.png"),
        other,
        "64",
    )
    assert code == 0, output
    code, output = a.python(
        _DEVICE_ON_A,
        str(a.gateway_port),
        a.ready["gateway"]["cert_fingerprint"],
        "console",
        other,
    )
    assert code == 0, output
    unreachable = _last_json(output)["answers"]["get_0"]["error"]["data"]
    assert unreachable == {"reason": "peer_unreachable", "peer_install_id": b_id}


@_REAL_CHILD_SPAWN
@pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS)
def test_a_read_tier_device_is_refused_the_whole_family_remote_rows_included(
    two_installs,
):
    """The remote half did not open a door for a viewer.

    A ``read`` device was refused this family on one root at Stage 8; the row
    being on another machine changes nothing about who may ask, which is worth
    asserting rather than assuming, because the scope grew and the gate did not.
    """

    a, b = two_installs
    b_id = b.ready["install"]["install_id"]

    code, minted, output = a.cli("gateway", "peers", "pair")
    assert code == 0, output
    code, _joined, output = b.cli(
        "gateway", "peers", "join", _payload_of(minted), "--timeout", "60"
    )
    assert code == 0, output

    code, output = a.python(
        _RECORD_ON_A, b_id, "X:\\Eternia\\artifacts\\on-b.png", PROOF_HANDLE, "512"
    )
    assert code == 0, output

    code, output = a.python(
        _DEVICE_ON_A,
        str(a.gateway_port),
        a.ready["gateway"]["cert_fingerprint"],
        "read",
        PROOF_HANDLE,
    )
    assert code == 0, output
    answers = _last_json(output)["answers"]

    for key in ("index", "get_0"):
        assert answers[key]["error"]["data"]["reason"] == "scope_denied", answers[key]
        assert answers[key]["error"]["data"]["caller"] == "device"
