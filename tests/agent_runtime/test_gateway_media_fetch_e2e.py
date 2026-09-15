"""Gateway Stage 8's acceptance: a paired device pulls a real image off a REAL serve.

Real ``serve_loop``, real second listener, real TLS with a real certificate pin,
real HMAC over a real per-device credential, real PNG bytes on a real disk. The
transport is faked nowhere, for ``test_serve_gateway_lane.py``'s reason: the
claims worth pinning here — that the bytes a device receives are the bytes on
disk, that a path never becomes an argument this lane accepts, and that a caller
who proved nothing is turned away before the filesystem is touched — are exactly
the ones a fake transport cannot fail.

The harness is imported from ``test_serve_gateway_lane`` rather than copied. A
second ``running_serve`` would be a second pairing ceremony, a second port
discovery and a second chance for the two to drift apart while both stay green.
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from agent_runtime import chat_live_log, media_handles, paths
from agent_runtime.call_authorization import TIER_CONSOLE, TIER_READ, REASON_SCOPE_DENIED
from agent_runtime.serve_socket import ServeSocketClient
from tests.agent_runtime.test_serve_gateway_lane import (  # noqa: F401 - fixtures
    WAIT,
    _rpc,
    device_client,
    gateway_on,
    pair_device,
    running_serve,
)

#: A byte string that is not a decodable PNG and does not have to be: nothing in
#: this lane decodes an image. What it has to be is BYTES that survive a base64
#: round trip unchanged, so it carries every value a byte can take.
PROOF_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 97 + b"\xff\x00\xfe"


@pytest.fixture
def seeded_media(tmp_path):
    """One Stage-C-shaped proof artifact, declared in one chat mirror.

    Seeded through ``capture_chat_live_log_root`` — the mirror's own resolution
    — because the design claim under test is that the scope is DERIVED from the
    store that already knows the artifacts. A test that wrote a handle into a
    registry would be testing a registry this stage deliberately does not have.

    The serve runs on a THREAD in this process, so this capture is the one the
    handler reads: same process, same module-global, which is exactly the
    coupling ``chat_live_log``'s docstring describes and not a shortcut.
    """

    chat_live_log.reset_chat_live_log_state()
    media_handles.reset_digest_memo()
    head_home = paths.store_root().parent
    chat_live_log.capture_chat_live_log_root(head_home=head_home)

    artifacts = head_home / "stagec" / "screenshots"
    artifacts.mkdir(parents=True, exist_ok=True)
    shot = artifacts / "w8_media_fetch_proof.png"
    shot.write_bytes(PROOF_BYTES)

    oversize = artifacts / "w8_oversize.png"
    oversize.write_bytes(b"\x00" * (media_handles.MAX_FETCH_BYTES + 11))

    logs = head_home / chat_live_log.CHAT_LIVE_LOG_DIRNAME
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "persona_chat_stage8.jsonl").write_text(
        "\n".join(
            json.dumps({"ts": "2026-08-28T00:00:00Z", "kind": "message", "role": "agent", "text": text})
            for text in (
                f"proof captured\n\nMEDIA:{shot}\n\nok=true\nbyte_count={len(PROOF_BYTES)}",
                f"and the one nobody may pull\n\nMEDIA:{oversize}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        yield shot, oversize
    finally:
        chat_live_log.reset_chat_live_log_state()
        media_handles.reset_digest_memo()


# ── the acceptance sentence ─────────────────────────────────────────────────


def test_a_paired_device_fetches_a_real_artifact_and_the_bytes_are_the_file(
    gateway_on, seeded_media
):
    """THE stage's sentence, end to end.

    A device paired at ``console`` over a real TLS socket asks what media exists,
    joins the answer to the reference it would have read out of a chat message,
    fetches by handle, and gets bytes whose sha256 equals the file on disk. That
    last equality is the whole of content addressing: a client that verifies it
    can cache the handle forever with no invalidation protocol at all.
    """

    shot, oversize = seeded_media
    credential = pair_device(tier=TIER_CONSOLE, name="the phone")

    with running_serve() as handle:
        with device_client(handle, credential) as (connection, hello_ok):
            # The manifest is how a client learns the family exists. Membership,
            # not a negotiation — the set-plus-integer rule, unchanged.
            assert "runtime.media.index" in hello_ok["rpc"]["methods"]
            assert "runtime.media.get" in hello_ok["rpc"]["methods"]
            assert hello_ok["rpc"]["tiers"]["runtime.media.get"] == "console"
            assert hello_ok["rpc"]["contract"] == 1

            index = _rpc(connection, "runtime.media.index")["result"]
            rows = {row["reference"]: row for row in index["artifacts"]}
            assert str(shot) in rows
            row = rows[str(shot)]
            assert row["size_bytes"] == len(PROOF_BYTES)
            assert row["media_type"] == "image/png"
            assert row["fetchable"] is True

            fetched = _rpc(
                connection, "runtime.media.get", {"handle": row["handle"]}
            )["result"]

    assert fetched["encoding"] == "base64"
    received = base64.b64decode(fetched["data"])
    assert received == shot.read_bytes()
    assert len(received) == len(PROOF_BYTES) == fetched["size_bytes"]
    assert hashlib.sha256(received).hexdigest() == hashlib.sha256(PROOF_BYTES).hexdigest()
    assert row["handle"] == "sha256:" + hashlib.sha256(received).hexdigest()


def test_the_oversize_artifact_is_indexed_unfetchable_and_refused_with_the_cap(
    gateway_on, seeded_media
):
    shot, oversize = seeded_media
    credential = pair_device(tier=TIER_CONSOLE)

    with running_serve() as handle:
        with device_client(handle, credential) as (connection, _):
            index = _rpc(connection, "runtime.media.index")["result"]
            row = next(
                r for r in index["artifacts"] if r["reference"] == str(oversize)
            )
            refused = _rpc(connection, "runtime.media.get", {"handle": row["handle"]})

    assert row["fetchable"] is False
    assert refused["error"]["data"] == {
        "reason": media_handles.REASON_ARTIFACT_TOO_LARGE,
        "cap_bytes": media_handles.MAX_FETCH_BYTES,
        "size_bytes": media_handles.MAX_FETCH_BYTES + 11,
    }


def test_a_path_and_an_unknown_handle_are_refused_over_the_real_wire(
    gateway_on, seeded_media
):
    """The path is the one this lane must never take, so it is sent for real:
    the absolute path of a file that IS in scope, which is the argument a client
    would reach for if the handle namespace did not exist."""

    shot, _ = seeded_media
    credential = pair_device(tier=TIER_CONSOLE)

    with running_serve() as handle:
        with device_client(handle, credential) as (connection, _):
            as_path = _rpc(connection, "runtime.media.get", {"handle": str(shot)})
            traversal = _rpc(
                connection,
                "runtime.media.get",
                {"handle": "../../../Windows/win.ini"},
            )
            guessed = _rpc(
                connection, "runtime.media.get", {"handle": "sha256:" + "0" * 64}
            )

    assert as_path["error"]["data"]["reason"] == media_handles.REASON_HANDLE_INVALID
    assert "does not accept a path" in as_path["error"]["message"]
    assert traversal["error"]["data"]["reason"] == media_handles.REASON_HANDLE_INVALID
    assert guessed["error"]["data"]["reason"] == media_handles.REASON_UNKNOWN_HANDLE


def test_a_device_paired_at_read_is_refused_the_whole_family_on_the_real_lane(
    gateway_on, seeded_media
):
    """A viewer is a viewer of the LEVEL. The refusal is the typed
    ``scope_denied`` the launcher's decoders already branch on, so no new client
    vocabulary was minted for this family."""

    credential = pair_device(tier=TIER_READ, name="the viewer")

    with running_serve() as handle:
        with device_client(handle, credential) as (connection, hello_ok):
            index = _rpc(connection, "runtime.media.index")
            fetch = _rpc(
                connection, "runtime.media.get", {"handle": "sha256:" + "0" * 64}
            )

    for reply in (index, fetch):
        assert reply["error"]["data"]["reason"] == REASON_SCOPE_DENIED
        assert reply["error"]["data"]["tier"] == "console"
        assert reply["error"]["data"]["caller"] == "device"


def test_an_unpaired_caller_never_reaches_the_verb_because_it_never_reaches_the_lane(
    gateway_on, seeded_media
):
    """"Refused before any filesystem touch" is stronger than a handler check on
    this lane, and this is where that is proven: an unknown device is refused at
    the HELLO, so the dispatcher is never entered at all. The handler-level arm —
    an authenticated caller the transport could not place — is
    ``test_serve_rpc_media.py``'s, where a monkeypatched derivation explodes if
    it is ever reached."""

    credential = pair_device(tier=TIER_CONSOLE)

    with running_serve() as handle:
        connection = ServeSocketClient(
            "127.0.0.1",
            handle.gateway_port,
            timeout_seconds=WAIT,
            tls=True,
            cert_fingerprint=handle.fingerprint,
        )
        connection.connect()
        try:
            rejected = connection.device_hello(
                device_id="dev_never_paired",
                token=credential.token,
                client="impostor",
            )
            # And the connection is DEAD, so the verb is not merely refused —
            # it is unreachable. A frame written after the rejection gets no
            # reply because there is nothing on the other end to read it.
            connection.send(
                {
                    "jsonrpc": "2.0",
                    "id": "after",
                    "method": "runtime.media.index",
                    "params": {},
                }
            )
            after = connection.read_frame()
        finally:
            connection.close()

    assert rejected["reason"] == "bad_proof"
    assert "rpc" not in rejected
    assert after is None
