"""Gateway Stage 8: the two ``fetch``-family verbs at the dispatcher.

The predicate is pinned in ``test_media_handles.py`` and the wire in
``test_gateway_media_fetch_e2e.py``. What is pinned HERE is the layer between:
the refusal frames a client decoder branches on, the tier declaration, and the
manifest arithmetic — a set that grew and an integer that did not.
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from agent_runtime import chat_live_log, media_handles, serve_rpc
from agent_runtime.call_authorization import (
    CALLER_DEVICE,
    CALLER_PEER,
    REASON_SCOPE_DENIED,
    STDIO_OWNER,
    TIER_CONSOLE,
    TIER_READ,
    TRANSPORT_GATEWAY,
    UNKNOWN_CALLER,
    RpcCaller,
)

INDEX = "runtime.media.index"
GET = "runtime.media.get"

PEER = RpcCaller(
    kind=CALLER_PEER,
    connection_key="c1",
    transport=TRANSPORT_GATEWAY,
    peer_install_id="inst_far_away",
)


def _device(tier: str = TIER_CONSOLE) -> RpcCaller:
    return RpcCaller(
        kind=CALLER_DEVICE,
        connection_key="c1",
        transport=TRANSPORT_GATEWAY,
        device_id="dev_phone",
        device_tier=tier,
    )


def _call(method: str, params: dict | None = None, *, caller=STDIO_OWNER) -> dict:
    return serve_rpc.handle_request(
        {"jsonrpc": "2.0", "id": "m1", "method": method, "params": params or {}},
        serve_rpc.RpcContext(caller=caller),
    )


@pytest.fixture
def seeded(tmp_path):
    """Point THIS process's live-log resolution at a sandbox and seed it.

    Through ``capture_chat_live_log_root`` rather than by monkeypatching a path
    into ``media_handles``: the whole design claim is that the scope is derived
    from the mirror's OWN resolution, and a test that reached around it would
    prove something else.
    """

    chat_live_log.reset_chat_live_log_state()
    media_handles.reset_digest_memo()
    chat_live_log.capture_chat_live_log_root(head_home=tmp_path)

    payload = b"\x89PNG\r\n\x1a\n" + b"proof-bytes" * 40
    shot = tmp_path / "artifacts" / "stagec_proof.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(payload)

    logs = tmp_path / chat_live_log.CHAT_LIVE_LOG_DIRNAME
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "persona_chat_test.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-08-28T00:00:00Z",
                "kind": "message",
                "role": "agent",
                "text": f"proof captured\n\nMEDIA:{shot}\n\nok=true",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        yield shot, payload
    finally:
        chat_live_log.reset_chat_live_log_state()
        media_handles.reset_digest_memo()


# ── the index ───────────────────────────────────────────────────────────────


def test_the_index_names_the_artifact_the_cap_and_what_the_scan_cost(seeded):
    shot, payload = seeded

    result = _call(INDEX)["result"]

    assert result["contract"] == serve_rpc.MEDIA_CONTRACT
    assert result["cap_bytes"] == media_handles.MAX_FETCH_BYTES
    assert result["truncated"] is False
    # Stage P4 added the second source's counter. ``completions: 0`` is the
    # honest reading of a machine that has dispatched nothing across an install
    # boundary, and it is STATED rather than absent so a client can tell "no
    # remote pictures" from "this runtime never derives them".
    assert result["scanned"] == {"logs": 1, "declarations": 1, "completions": 0}
    assert result["artifacts"] == [
        {
            "handle": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "reference": str(shot),
            "media_type": "image/png",
            "size_bytes": len(payload),
            "fetchable": True,
            # Stage P4, and present on EVERY row for ``peer.ping``'s reason: a
            # client must never read a fact out of a key's absence.
            "remote": False,
        }
    ]


def test_the_index_takes_no_argument_that_could_narrow_or_widen_its_scope(seeded):
    """A path, a directory and a chat id are all ignored rather than honoured.
    A verb whose scope a caller can narrow is a verb whose scope a caller can
    WIDEN the day the narrowing argument is mis-parsed."""

    hostile = _call(
        INDEX, {"path": "C:\\Windows", "root": "/", "session_id": "anything"}
    )["result"]

    assert hostile["artifacts"] == _call(INDEX)["result"]["artifacts"]


def test_an_index_with_no_mirror_is_an_empty_list_not_an_error(tmp_path):
    chat_live_log.reset_chat_live_log_state()
    chat_live_log.capture_chat_live_log_root(head_home=tmp_path / "empty")
    try:
        result = _call(INDEX)["result"]
    finally:
        chat_live_log.reset_chat_live_log_state()

    assert result["artifacts"] == []
    assert result["truncated"] is False


# ── the fetch ───────────────────────────────────────────────────────────────


def test_a_handle_from_the_index_comes_back_as_the_bytes_on_disk(seeded):
    shot, payload = seeded
    handle = _call(INDEX)["result"]["artifacts"][0]["handle"]

    result = _call(GET, {"handle": handle})["result"]

    assert result["handle"] == handle
    assert result["media_type"] == "image/png"
    assert result["encoding"] == "base64"
    assert result["size_bytes"] == len(payload)
    decoded = base64.b64decode(result["data"])
    assert decoded == payload == shot.read_bytes()
    assert media_handles.handle_for_bytes(decoded) == handle


def test_a_PATH_is_refused_typed_and_the_message_says_what_the_lane_takes(seeded):
    shot, _ = seeded

    for hostile in (
        str(shot),
        "..\\..\\..\\Windows\\win.ini",
        "/etc/passwd",
        "sha256:" + "z" * 64,
        "",
        7,
        None,
    ):
        reply = _call(GET, {"handle": hostile})
        assert reply["error"]["data"]["reason"] == media_handles.REASON_HANDLE_INVALID, hostile


def test_a_missing_handle_is_an_invalid_params_and_not_a_scan(seeded):
    reply = _call(GET, {})

    assert reply["error"]["code"] == serve_rpc.ERR_INVALID_PARAMS
    assert reply["error"]["data"]["reason"] == media_handles.REASON_HANDLE_INVALID


def test_a_well_formed_handle_nobody_declared_is_unknown(seeded):
    reply = _call(GET, {"handle": "sha256:" + "0" * 64})

    assert reply["error"]["data"] == {"reason": media_handles.REASON_UNKNOWN_HANDLE}


def test_an_over_cap_artifact_is_refused_with_the_cap_NAMED(tmp_path):
    chat_live_log.reset_chat_live_log_state()
    media_handles.reset_digest_memo()
    chat_live_log.capture_chat_live_log_root(head_home=tmp_path)
    big = tmp_path / "huge.png"
    size = media_handles.MAX_FETCH_BYTES + 7
    big.write_bytes(b"\x00" * size)
    logs = tmp_path / chat_live_log.CHAT_LIVE_LOG_DIRNAME
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "s.jsonl").write_text(
        json.dumps({"kind": "message", "role": "agent", "text": f"MEDIA:{big}"}) + "\n",
        encoding="utf-8",
    )
    try:
        indexed = _call(INDEX)["result"]["artifacts"][0]
        reply = _call(GET, {"handle": indexed["handle"]})
    finally:
        chat_live_log.reset_chat_live_log_state()
        media_handles.reset_digest_memo()

    # The index already said so, so a client need never spend the round trip…
    assert indexed["fetchable"] is False
    assert indexed["size_bytes"] == size
    # …and the fetch says it too, because a client is free to ignore an index it
    # did not read. The NUMBER is on the frame: a cap a client has to guess is a
    # cap it will guess wrong.
    assert reply["error"]["data"] == {
        "reason": media_handles.REASON_ARTIFACT_TOO_LARGE,
        "cap_bytes": media_handles.MAX_FETCH_BYTES,
        "size_bytes": size,
    }


def test_an_artifact_that_vanished_between_index_and_fetch_is_unreadable(seeded):
    shot, _ = seeded
    handle = _call(INDEX)["result"]["artifacts"][0]["handle"]
    shot.unlink()

    reply = _call(GET, {"handle": handle})

    # It is gone from the derivation too, so the scope answers first — and that
    # is the right answer: the handle names bytes this install no longer has.
    assert reply["error"]["data"]["reason"] == media_handles.REASON_UNKNOWN_HANDLE


def test_a_malformed_correlation_id_is_refused_at_the_same_boundary_as_every_write(
    seeded,
):
    for method in (INDEX, GET):
        reply = _call(method, {"handle": "sha256:" + "0" * 64, "correlation_id": "a b"})
        assert reply["error"]["code"] == serve_rpc.ERR_INVALID_PARAMS
        assert (
            reply["error"]["data"]["reason"] == serve_rpc.CORRELATION_ID_INVALID_REASON
        )


def test_a_correlation_id_rides_both_replies_back(seeded):
    handle = _call(INDEX, {"correlation_id": "gesture-1"})["result"]
    assert handle["correlation_id"] == "gesture-1"
    fetched = _call(
        GET, {"handle": handle["artifacts"][0]["handle"], "correlation_id": "gesture-2"}
    )["result"]
    assert fetched["correlation_id"] == "gesture-2"


# ── who may call it ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("method", [INDEX, GET])
def test_an_unknown_caller_is_refused_at_the_CHOKEPOINT_before_any_scan(
    method, monkeypatch
):
    """The reason the tier is ``console`` and not ``read``. The read arm is open
    to everyone including ``unknown`` — deliberately, and A5 kept it — so a read
    tier here would have let a caller the transport could not place pull files
    off the disk."""

    def _explode(*args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("a refused caller reached the derivation")

    monkeypatch.setattr(media_handles, "build_media_scope", _explode)

    reply = serve_rpc.handle_request(
        {"jsonrpc": "2.0", "id": "m1", "method": method, "params": {}},
        serve_rpc.RpcContext(caller=UNKNOWN_CALLER),
    )

    assert reply["error"]["data"] == {
        "reason": REASON_SCOPE_DENIED,
        "tier": TIER_CONSOLE,
        "caller": UNKNOWN_CALLER.kind,
    }


@pytest.mark.parametrize("method", [INDEX, GET])
def test_a_read_tier_device_is_a_viewer_of_the_level_not_of_the_disk(method):
    reply = _call(method, {"handle": "sha256:" + "0" * 64}, caller=_device(TIER_READ))

    assert reply["error"]["data"] == {
        "reason": REASON_SCOPE_DENIED,
        "tier": TIER_CONSOLE,
        "caller": CALLER_DEVICE,
    }


def test_a_console_device_may_fetch_because_that_is_what_the_stage_is_for(seeded):
    handle = _call(INDEX, caller=_device(TIER_CONSOLE))["result"]["artifacts"][0][
        "handle"
    ]
    reply = _call(GET, {"handle": handle}, caller=_device(TIER_CONSOLE))

    assert base64.b64decode(reply["result"]["data"]) == seeded[1]


@pytest.mark.parametrize("method", [INDEX, GET])
def test_a_peer_is_refused_and_the_allowlist_was_not_touched_to_arrange_it(method):
    """Registering a verb excludes it from the peer surface BY CONSTRUCTION.
    Stage 6's iterated registry test asserts the rule; this asserts the frame a
    cross-install caller actually receives."""

    from agent_runtime.call_authorization import PEER_METHOD_ALLOWLIST

    assert method not in PEER_METHOD_ALLOWLIST

    reply = _call(method, {"handle": "sha256:" + "0" * 64}, caller=PEER)

    assert reply["error"]["data"] == {
        "reason": REASON_SCOPE_DENIED,
        "tier": TIER_CONSOLE,
        "caller": CALLER_PEER,
    }


# ── the manifest arithmetic ─────────────────────────────────────────────────


def test_the_family_joined_the_set_without_moving_the_integer():
    manifest = serve_rpc.manifest()

    assert {INDEX, GET} <= set(manifest["methods"])
    assert manifest["tiers"][INDEX] == TIER_CONSOLE
    assert manifest["tiers"][GET] == TIER_CONSOLE
    assert manifest["contract"] == 1
    assert serve_rpc.RPC_CONTRACT_VERSION == 1
    assert set(manifest) == {"contract", "methods", "tiers"}


# ── Stage P4: peer.media.get, and the proxy arm on runtime.media.get ─────────
#
# The keyhole and the door it is a keyhole in. What is pinned here is the
# dispatcher layer for both: who is turned away, what a path-shaped argument
# lands in, and — the property the whole lane's acyclicity rests on — that the
# peer verb resolves the LOCAL half of the scope and nothing else.

PEER_GET = "peer.media.get"


def _peer_call(params: dict, rid: str = "p1") -> dict:
    return serve_rpc.handle_request(
        {"jsonrpc": "2.0", "id": rid, "method": PEER_GET, "params": params},
        serve_rpc.RpcContext(caller=PEER, transport=TRANSPORT_GATEWAY),
    )


def _far_completions(monkeypatch, handle: str, size: int) -> None:
    """Stand a stored cross-install map in front of the scope derivation.

    Patched at ``dispatch_store``'s function rather than at the scope's
    parameter, so what is exercised is the wiring ``build_media_scope`` actually
    uses in production — the seam a test that passed ``remote_completions=``
    directly would step around.
    """

    monkeypatch.setattr(
        "agent_runtime.dispatch_store.remote_media_completions",
        lambda **_k: [
            {
                "dispatch_id": "d1",
                "peer_install_id": "install-b",
                "media": [
                    {
                        "reference": "X:\\Eternia\\artifacts\\on-b.png",
                        "handle": handle,
                        "media_type": "image/png",
                        "size_bytes": size,
                    }
                ],
            }
        ],
    )


def test_the_peer_keyhole_serves_a_local_artifact_to_a_paired_install(seeded):
    reply = _peer_call({"handle": "sha256:" + hashlib.sha256(seeded[1]).hexdigest()})

    assert base64.b64decode(reply["result"]["data"]) == seeded[1]
    assert reply["result"]["encoding"] == "base64"
    # Echoed for ``peer.ping``'s reason: the dialler confirms it was recognised
    # as the install it meant to be, which no client-side check can tell it.
    assert reply["result"]["peer"] == "inst_far_away"


def test_the_peer_keyhole_refuses_a_caller_that_proved_no_install(seeded):
    """A local console client is refused too, and that is the case worth
    spelling: it already has ``runtime.media.get``, whose scope is strictly
    larger."""

    reply = _call(
        PEER_GET, {"handle": "sha256:" + hashlib.sha256(seeded[1]).hexdigest()}
    )

    assert reply["error"]["data"]["reason"] == serve_rpc.PEER_CHAT_NOT_A_PEER_REASON


def test_the_peer_keyhole_takes_a_handle_and_never_a_path(seeded):
    reply = _peer_call({"handle": str(seeded[0])}, rid="p2")

    assert reply["error"]["data"] == {"reason": media_handles.REASON_HANDLE_INVALID}


def test_the_peer_keyhole_resolves_no_remote_row_so_the_lane_cannot_chain(
    seeded, monkeypatch
):
    """THE acyclicity pin. A handle this install holds only as a REMOTE row —
    one it learned from a third install — is ``unknown_handle`` to a peer, not
    a second proxy hop. So there is no A to B to C fan-out to bound, and no way
    for two paired installs to bounce a fetch between them."""

    far = "sha256:" + "b" * 64
    _far_completions(monkeypatch, far, 64)

    # This install DOES hold it in scope for its own console client — the row
    # resolves and the fetch gets as far as the proxy…
    local_view = _call(GET, {"handle": far})
    assert local_view["error"]["data"]["reason"] != media_handles.REASON_UNKNOWN_HANDLE

    # …and a peer asking for the same handle is told nobody HERE has those bytes.
    reply = _peer_call({"handle": far}, rid="p3")

    assert reply["error"]["data"] == {"reason": media_handles.REASON_UNKNOWN_HANDLE}


def test_the_index_names_a_remote_row_without_claiming_a_local_path(
    seeded, monkeypatch
):
    far = "sha256:" + "c" * 64
    _far_completions(monkeypatch, far, 64)

    rows = _call(INDEX)["result"]

    remote = [row for row in rows["artifacts"] if row["remote"]]
    assert remote == [
        {
            "handle": far,
            "reference": "X:\\Eternia\\artifacts\\on-b.png",
            "media_type": "image/png",
            "size_bytes": 64,
            "fetchable": True,
            "remote": True,
            "peer_install_id": "install-b",
        }
    ]
    # The local artifact is still there and still says it is local.
    assert any(row["remote"] is False for row in rows["artifacts"])
    assert rows["scanned"]["completions"] == 1


def test_a_console_client_fetching_a_remote_handle_is_answered_by_the_proxy(
    seeded, monkeypatch
):
    """The arm's whole point: the reply is shaped exactly like a local one, so a
    client cannot tell a proxied artifact from a local one and has nothing it
    would do differently if it could."""

    from agent_runtime import media_proxy

    bytes_on_b = b"\x89PNG\r\n\x1a\n" + b"over-there" * 7
    far = "sha256:" + hashlib.sha256(bytes_on_b).hexdigest()
    _far_completions(monkeypatch, far, len(bytes_on_b))
    monkeypatch.setattr(
        media_proxy, "fetch_remote_artifact", lambda artifact, **_k: bytes_on_b
    )

    reply = _call(GET, {"handle": far}, caller=_device(TIER_CONSOLE))["result"]

    assert base64.b64decode(reply["data"]) == bytes_on_b
    assert reply["handle"] == far
    assert reply["media_type"] == "image/png"
    assert set(reply) == {
        "contract",
        "handle",
        "media_type",
        "size_bytes",
        "encoding",
        "data",
    }


def test_a_proxy_refusal_reaches_the_client_as_the_lanes_own_typed_frame(
    seeded, monkeypatch
):
    from agent_runtime import media_proxy

    far = "sha256:" + "d" * 64
    _far_completions(monkeypatch, far, 64)
    monkeypatch.setattr(
        media_proxy,
        "fetch_remote_artifact",
        lambda artifact, **_k: media_handles.MediaRefusal(
            media_proxy.REASON_PEER_UNREACHABLE, {"peer_install_id": "install-b"}
        ),
    )

    reply = _call(GET, {"handle": far})

    assert reply["error"]["data"] == {
        "reason": media_proxy.REASON_PEER_UNREACHABLE,
        "peer_install_id": "install-b",
    }


def test_the_keyhole_joined_the_manifest_without_moving_the_integer():
    manifest = serve_rpc.manifest()

    assert PEER_GET in manifest["methods"]
    assert manifest["tiers"][PEER_GET] == TIER_CONSOLE
    assert manifest["contract"] == 1
