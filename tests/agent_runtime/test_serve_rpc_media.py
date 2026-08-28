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
    assert result["scanned"] == {"logs": 1, "declarations": 1}
    assert result["artifacts"] == [
        {
            "handle": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "reference": str(shot),
            "media_type": "image/png",
            "size_bytes": len(payload),
            "fetchable": True,
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
