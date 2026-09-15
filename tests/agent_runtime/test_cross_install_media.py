"""Stage P4 (ruling R-P3) — cross-install media, everything but the wire.

The wire itself is proven once, against two real serve children, by
``test_gateway_peer_cross_install_media_e2e``. What is under test HERE is the
four seams that acceptance cannot isolate:

1. **The mint on B.** A reply's ``MEDIA:`` lines become handles on the install
   that holds the bytes — and only for a PEER-executed turn, because a local
   turn's path already resolves and hashing it would spend I/O to write a field
   with no reader.
2. **The carry.** The map rides the dispatch completion durably, and the
   EventLog payload carries a COUNT rather than the map, because that lane's cap
   is 4096 bytes.
3. **The fold on A.** A stored map becomes REMOTE scope rows — with every field
   re-derived rather than trusted, because the map was written by another
   install and a trusted ``media_type`` is a hole in the image allowlist.
4. **The proxy.** One dial, bytes verified against the handle, cached by content
   address; a second fetch dials nothing; a peer that answers the wrong bytes
   gets nothing cached and no channel.

The seam these four join at is the one the acceptance covers. These are the
halves.
"""

from __future__ import annotations

import hashlib
import json
import types

import pytest

from agent_runtime import dispatch_store, media_handles, media_proxy
from agent_runtime.chat_turn import PEER_REQUESTED_BY_PREFIX
from agent_runtime.dispatch_store import (
    STATE_COMPLETED,
    get_dispatch,
    record_completion,
    record_dispatch,
    remote_media_completions,
)
from tools import agent_chat_dispatch

PROOF_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 5
PROOF_HANDLE = "sha256:" + hashlib.sha256(PROOF_BYTES).hexdigest()
GUESSED_HANDLE = "sha256:" + "0" * 64


@pytest.fixture
def store_home(tmp_path, monkeypatch):
    home = tmp_path / "bg-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_HEAD_HOME", str(home))
    return home


@pytest.fixture
def cache_root(tmp_path):
    root = tmp_path / "install-root"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def shot(tmp_path):
    """A real image on a real disk, named the way a ``MEDIA:`` line names one."""

    media_handles.reset_digest_memo()
    path = tmp_path / "artifacts" / "proof.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PROOF_BYTES)
    return path


def _remote(handle: str = PROOF_HANDLE, *, size: int | None = None):
    return media_handles.RemoteMediaArtifact(
        handle=handle,
        reference="X:\\Eternia\\artifacts\\proof.png",
        peer_install_id="install-b",
        media_type="image/png",
        size_bytes=len(PROOF_BYTES) if size is None else size,
    )


# ── 1. the mint on B ─────────────────────────────────────────────────────────


def test_a_reply_mints_a_handle_for_the_image_it_declares(shot):
    minted = media_handles.mint_reply_media(
        f"Here is the run.\n\nMEDIA:{shot}\n\nEverything passed."
    )

    assert minted == [
        {
            "reference": str(shot),
            "handle": PROOF_HANDLE,
            "media_type": "image/png",
            "size_bytes": len(PROOF_BYTES),
        }
    ]


def test_the_mint_carries_no_fetchable_verdict(shot):
    """``fetchable`` is a function of the SERVING install's cap, so the minting
    install does not get to decide it. A row that carried one would be install
    B's policy travelling into install A's answer."""

    assert "fetchable" not in media_handles.mint_reply_media(f"MEDIA:{shot}")[0]


def test_the_mint_refuses_everything_the_scope_derivation_refuses(tmp_path, shot):
    """The allowlist and the absolute-path rule are the parser's, not a second
    copy — so a credential, a relative path and a file that is not there all
    mint nothing, exactly as they contribute nothing to a local scope."""

    secret = tmp_path / "id_rsa"
    secret.write_bytes(b"-----BEGIN OPENSSH PRIVATE KEY-----")
    missing = tmp_path / "artifacts" / "gone.png"

    assert media_handles.mint_reply_media(f"MEDIA:{secret}") == []
    assert media_handles.mint_reply_media(f"MEDIA:{missing}") == []
    assert media_handles.mint_reply_media("MEDIA:artifacts/proof.png") == []
    # …and the prose form every skill doc contains is prose, not a declaration.
    assert media_handles.mint_reply_media("MEDIA:<absolute path>") == []
    # The control: the same call on the real one does mint.
    assert media_handles.mint_reply_media(f"MEDIA:{shot}")


def test_the_mint_is_bounded_by_its_own_constant(tmp_path):
    lines = []
    for index in range(media_handles.MAX_REPLY_ARTIFACTS + 4):
        path = tmp_path / f"shot-{index}.png"
        path.write_bytes(PROOF_BYTES + bytes([index]))
        lines.append(f"MEDIA:{path}")

    minted = media_handles.mint_reply_media("\n".join(lines))

    assert len(minted) == media_handles.MAX_REPLY_ARTIFACTS


def _args(requested_by: str):
    return types.SimpleNamespace(requested_by=requested_by)


def _stamp_reply_media():
    """The producer under test, reached the way ``harness.py`` reaches it.

    ``persona_commands`` is exec'd into the harness module's globals rather than
    imported as a module with a public surface, so this is the one honest way to
    get at the function — and it is the same object the five reply sites call.
    """

    from hermes_cli.harness_parts import persona_commands

    return persona_commands._stamp_reply_media


def test_a_peer_executed_reply_carries_the_map_and_a_local_one_carries_nothing(shot):
    """The gate, both directions, on identical input.

    This is the whole reason the mint is gated rather than unconditional: the
    ONLY difference between these two calls is who asked, and a local turn's
    ``MEDIA:`` path already resolves on the machine that will render it.
    """

    reply = f"Done.\n\nMEDIA:{shot}"

    peer_payload: dict = {"reply": reply}
    _stamp_reply_media()(peer_payload, reply, _args(f"{PEER_REQUESTED_BY_PREFIX}install-a"))
    local_payload: dict = {"reply": reply}
    _stamp_reply_media()(local_payload, reply, _args("agent:root-1"))

    assert peer_payload["media"] == [
        {
            "reference": str(shot),
            "handle": PROOF_HANDLE,
            "media_type": "image/png",
            "size_bytes": len(PROOF_BYTES),
        }
    ]
    # ABSENT, not empty: a local payload is byte-identical to what it was before
    # this stage, so no consumer has to tell "no pictures" from "older runtime".
    assert "media" not in local_payload


def test_a_peer_reply_with_no_image_carries_no_key_either():
    payload: dict = {"reply": "Nothing to show."}
    _stamp_reply_media()(
        payload, "Nothing to show.", _args(f"{PEER_REQUESTED_BY_PREFIX}install-a")
    )

    assert "media" not in payload


def test_a_mint_that_explodes_costs_the_turn_nothing(monkeypatch, shot):
    """One of the five call sites is inside an exception handler. A raise here
    would replace a real failure with this one and corrupt the
    one-JSON-object stdout contract on the way out."""

    def boom(*_a, **_k):
        raise RuntimeError("the disk went away")

    monkeypatch.setattr(media_handles, "mint_reply_media", boom)
    payload: dict = {"reply": "x"}

    _stamp_reply_media()(payload, f"MEDIA:{shot}", _args(f"{PEER_REQUESTED_BY_PREFIX}b"))

    assert payload == {"reply": "x"}


# ── 2. the carry ─────────────────────────────────────────────────────────────


def _completed_remote_row(store_home, *, media, dispatch_id="dispatch-m1"):
    record_dispatch(
        dispatch_id=dispatch_id,
        sender_session_id="persona_chat_personainst_neko_aaaaaaaaaaaa",
        target_persona="@workstation/dev",
        ask="go",
        remote_install_id="install-b",
    )
    record_completion(
        dispatch_id,
        state=STATE_COMPLETED,
        reply="here it is",
        media=media,
        remote={"install_id": "install-b", "attempts": 1},
    )
    return dispatch_id


def test_the_map_lands_on_the_row_and_the_store_hands_it_back(store_home, shot):
    entry = {
        "reference": str(shot),
        "handle": PROOF_HANDLE,
        "media_type": "image/png",
        "size_bytes": len(PROOF_BYTES),
    }

    dispatch_id = _completed_remote_row(store_home, media=[entry])

    assert get_dispatch(dispatch_id)["result"]["media"] == [entry]
    assert remote_media_completions() == [
        {
            "dispatch_id": dispatch_id,
            "peer_install_id": "install-b",
            "media": [entry],
        }
    ]


def test_a_local_completion_is_byte_identical_to_before_this_stage(store_home):
    record_dispatch(
        dispatch_id="dispatch-local",
        sender_session_id="persona_chat_personainst_neko_aaaaaaaaaaaa",
        target_persona="dev",
        ask="go",
    )
    record_completion("dispatch-local", state=STATE_COMPLETED, reply="done")

    assert "media" not in get_dispatch("dispatch-local")["result"]
    assert remote_media_completions() == []


def test_the_completion_event_carries_a_count_and_never_the_map(
    store_home, monkeypatch, shot
):
    """The 4096-byte payload cap, honoured by construction. Sixteen absolute
    Windows paths plus 71-character handles is kilobytes; a store write whose
    event was refused for size is a write no watermark-gated consumer sees."""

    captured: dict = {}
    original = dispatch_store._emit

    def _capture(kind, **payload):
        captured[kind] = payload
        return original(kind, **payload)

    monkeypatch.setattr(dispatch_store, "_emit", _capture)
    entries = [
        {
            "reference": f"X:\\Eternia\\artifacts\\{'p' * 180}-{index}.png",
            "handle": "sha256:" + f"{index:064x}",
            "media_type": "image/png",
            "size_bytes": 1024,
        }
        for index in range(media_handles.MAX_REPLY_ARTIFACTS)
    ]

    _completed_remote_row(store_home, media=entries)

    event = captured["dispatch.completed"]
    assert event["media_count"] == media_handles.MAX_REPLY_ARTIFACTS
    assert "media" not in event
    assert len(json.dumps(event).encode("utf-8")) < 4096


def test_a_local_completions_event_is_unchanged(store_home, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        dispatch_store, "_emit", lambda kind, **payload: captured.setdefault(kind, payload)
    )
    record_dispatch(
        dispatch_id="dispatch-local2",
        sender_session_id="persona_chat_personainst_neko_aaaaaaaaaaaa",
        target_persona="dev",
        ask="go",
    )
    record_completion("dispatch-local2", state=STATE_COMPLETED, reply="done")

    assert captured["dispatch.completed"]["media_count"] is None


def test_the_store_bounds_a_map_the_far_install_chose(store_home):
    """A bound only the SENDER applies is a bound. Sixty rows from a peer become
    the receiver's own limit, and a malformed row is dropped rather than stored
    as a picture that will never resolve."""

    hostile = [{"reference": f"X:\\a\\{i}.png", "handle": "sha256:" + f"{i:064x}"} for i in range(60)]
    hostile += [{"reference": "", "handle": "x"}, "not a row", {"handle": "only"}]

    dispatch_id = _completed_remote_row(store_home, media=hostile)

    stored = get_dispatch(dispatch_id)["result"]["media"]
    assert len(stored) == dispatch_store.MEDIA_MAP_LIMIT
    assert all(row["reference"] and row["handle"] for row in stored)


def test_the_remote_leg_puts_the_far_installs_map_on_the_row(store_home, monkeypatch, shot):
    """The join the supervisor owns: B's payload key → A's durable row.

    Asserted at ``_run_remote_dispatch`` rather than at ``record_completion``
    because the seam that can be forgotten is the ARGUMENT, and a test of the
    store alone would stay green through a supervisor that never passed it.
    """

    entry = {
        "reference": str(shot),
        "handle": PROOF_HANDLE,
        "media_type": "image/png",
        "size_bytes": len(PROOF_BYTES),
    }
    payload = {
        "capability_id": "mission.chat.message",
        "ok": True,
        "reply": f"MEDIA:{shot}",
        "session_id": "sess-1",
        "media": [entry],
    }
    record_dispatch(
        dispatch_id="dispatch-leg",
        sender_session_id="persona_chat_personainst_neko_aaaaaaaaaaaa",
        target_persona="@workstation/dev",
        ask="go",
        remote_install_id="install-b",
    )

    class _Connection:
        def __init__(self, frames):
            self._frames = list(frames)

        def send(self, _message):
            pass

        def read_frame(self):
            return self._frames.pop(0) if self._frames else None

        def set_timeout(self, _seconds):
            pass

        def close(self):
            pass

    frames = [
        {
            "jsonrpc": "2.0",
            "id": "peer-exec-dispatch-leg",
            "result": {"accepted": True, "request_id": "chat-1"},
        }
    ]
    frames += [
        {"id": "chat-1", "event": agent_chat_dispatch.SERVE_STDOUT_EVENT, "line": line}
        for line in json.dumps(payload, indent=2).split("\n")
    ]
    frames.append({"id": "chat-1", "event": "exit", "code": 0})
    monkeypatch.setattr(
        "agent_runtime.gateway_peers.dial_peer",
        lambda *_a, **_k: (_Connection(frames), {"event": "hello_ok"}),
    )

    agent_chat_dispatch._run_remote_dispatch(
        "dispatch-leg",
        {
            "client_message_id": "agent-dispatch-dispatch-leg",
            "remote_install_id": "install-b",
            "remote_display_name": "workstation",
            "remote_target": "dev",
            "message": "go",
            "max_seconds": 60.0,
        },
    )

    assert get_dispatch("dispatch-leg")["result"]["media"] == [entry]


# ── 3. the fold on A ─────────────────────────────────────────────────────────


def _completion(**entry):
    row = {
        "reference": "X:\\Eternia\\artifacts\\proof.png",
        "handle": PROOF_HANDLE,
        "media_type": "image/png",
        "size_bytes": len(PROOF_BYTES),
    }
    row.update(entry)
    return {"peer_install_id": "install-b", "media": [row]}


def test_a_stored_map_becomes_a_remote_row_with_no_path():
    rows, truncated = media_handles.remote_artifacts_from_completions([_completion()])

    assert truncated is False
    assert rows[PROOF_HANDLE].describe() == {
        "handle": PROOF_HANDLE,
        "reference": "X:\\Eternia\\artifacts\\proof.png",
        "media_type": "image/png",
        "size_bytes": len(PROOF_BYTES),
        "fetchable": True,
        "remote": True,
        "peer_install_id": "install-b",
    }
    assert not hasattr(rows[PROOF_HANDLE], "path")


@pytest.mark.parametrize(
    "entry",
    [
        {"handle": "X:\\Eternia\\artifacts\\proof.png"},  # a path in the handle slot
        {"handle": "sha256:nothex"},
        {"reference": "artifacts/proof.png"},  # relative
        {"reference": "X:\\Eternia\\secrets\\id_rsa"},  # not an image
        {"size_bytes": "big"},
        {"size_bytes": -1},
    ],
)
def test_a_row_the_far_install_malformed_never_enters_the_namespace(entry):
    rows, _truncated = media_handles.remote_artifacts_from_completions(
        [_completion(**entry)]
    )

    assert rows == {}


def test_the_media_type_is_re_derived_and_never_read_off_the_peers_row():
    """The hole this closes: a paired install that could name its own media type
    could put a credential into this install's namespace under ``image/png``.
    The extension decides, here, the same way it decides for a local artifact."""

    rows, _ = media_handles.remote_artifacts_from_completions(
        [_completion(reference="X:\\a\\shot.gif", media_type="text/plain")]
    )

    assert rows[PROOF_HANDLE].media_type == "image/gif"


def test_a_map_with_no_peer_to_ask_contributes_nothing():
    rows, _ = media_handles.remote_artifacts_from_completions(
        [{"peer_install_id": "", "media": [_completion()["media"][0]]}]
    )

    assert rows == {}


def test_the_remote_fold_is_bounded_and_says_so():
    completions = [
        {
            "peer_install_id": "install-b",
            "media": [
                {
                    "reference": f"X:\\a\\{index}.png",
                    "handle": "sha256:" + f"{index:064x}",
                    "media_type": "image/png",
                    "size_bytes": 10,
                }
            ],
        }
        for index in range(media_handles.MAX_REMOTE_ARTIFACTS + 5)
    ]

    rows, truncated = media_handles.remote_artifacts_from_completions(completions)

    assert len(rows) == media_handles.MAX_REMOTE_ARTIFACTS
    assert truncated is True


def test_a_scope_reports_both_halves_and_prefers_the_local_row(tmp_path, shot):
    """A file present on BOTH installs mints ONE handle — that is what content
    addressing means — and the local row wins, so a fetch this disk can answer
    never spends a peer dial."""

    mirror = tmp_path / "mirrors"
    mirror.mkdir()
    (mirror / "s.jsonl").write_bytes(
        json.dumps({"text": f"MEDIA:{shot}"}).encode("utf-8") + b"\n"
    )

    scope = media_handles.build_media_scope(
        root=mirror,
        remote_completions=[_completion(reference=str(shot))],
    )

    assert PROOF_HANDLE in scope.artifacts
    assert PROOF_HANDLE in scope.remote
    resolved = media_handles.resolve_handle(PROOF_HANDLE, scope)
    assert isinstance(resolved, media_handles.MediaArtifact)
    assert resolved.path == shot.resolve()
    # …and the merged listing shows the picture ONCE, not twice.
    assert [row.handle for row in scope.rows()] == [PROOF_HANDLE]


def test_a_remote_only_handle_resolves_to_the_remote_row(tmp_path):
    scope = media_handles.build_media_scope(
        root=tmp_path / "no-mirrors", remote_completions=[_completion()]
    )

    resolved = media_handles.resolve_handle(PROOF_HANDLE, scope)

    assert isinstance(resolved, media_handles.RemoteMediaArtifact)
    assert resolved.peer_install_id == "install-b"
    assert scope.completions_scanned == 1


def test_an_over_cap_remote_artifact_is_refused_before_any_dial(tmp_path):
    scope = media_handles.build_media_scope(
        root=tmp_path / "none",
        remote_completions=[_completion(size_bytes=media_handles.MAX_FETCH_BYTES + 1)],
    )

    refusal = media_handles.resolve_handle(PROOF_HANDLE, scope)

    assert isinstance(refusal, media_handles.MediaRefusal)
    assert refusal.reason == media_handles.REASON_ARTIFACT_TOO_LARGE
    assert refusal.detail["cap_bytes"] == media_handles.MAX_FETCH_BYTES


def test_a_guessed_digest_is_unknown_on_a_scope_that_has_remote_rows(tmp_path):
    scope = media_handles.build_media_scope(
        root=tmp_path / "none", remote_completions=[_completion()]
    )

    refusal = media_handles.resolve_handle(GUESSED_HANDLE, scope)

    assert isinstance(refusal, media_handles.MediaRefusal)
    assert refusal.reason == media_handles.REASON_UNKNOWN_HANDLE


def test_a_store_that_will_not_open_costs_the_local_pictures_nothing(
    tmp_path, monkeypatch, shot
):
    """The failure DIRECTION. A remote source that raises must degrade this
    install to the scope it had before Stage P4, never to no scope at all."""

    mirror = tmp_path / "mirrors"
    mirror.mkdir()
    (mirror / "s.jsonl").write_bytes(
        json.dumps({"text": f"MEDIA:{shot}"}).encode("utf-8") + b"\n"
    )

    def boom(**_kwargs):
        raise sqlite_error()

    def sqlite_error():
        return RuntimeError("the store is locked")

    monkeypatch.setattr(dispatch_store, "remote_media_completions", boom)

    scope = media_handles.build_media_scope(root=mirror)

    assert PROOF_HANDLE in scope.artifacts
    assert scope.remote == {}


# ── 4. the proxy ─────────────────────────────────────────────────────────────


class _PeerConnection:
    """A far install that answers one ``peer.media.get``."""

    def __init__(self, frame):
        self._frame = frame
        self.sent: list[dict] = []
        self.closed = False

    def send(self, message):
        self.sent.append(message)

    def read_frame(self):
        frame, self._frame = self._frame, None
        return frame

    def set_timeout(self, _seconds):
        pass

    def close(self):
        self.closed = True


def _result_frame(data: bytes, *, encoding="base64"):
    import base64 as b64

    return {
        "jsonrpc": "2.0",
        "id": "peer-media-get",
        "result": {
            "contract": 1,
            "handle": PROOF_HANDLE,
            "media_type": "image/png",
            "size_bytes": len(data),
            "encoding": encoding,
            "data": b64.b64encode(data).decode("ascii"),
        },
    }


def _dialler(frame, dials: list):
    def dial(peer_install_id):
        dials.append(peer_install_id)
        return _PeerConnection(frame), {"event": "hello_ok"}

    return dial


def test_a_first_fetch_dials_once_and_a_second_dials_never(cache_root):
    """The acceptance's cache proof, at the seam. Content addressing is what
    makes it sound: the key IS the digest, so an entry can never be stale."""

    dials: list[str] = []
    dial = _dialler(_result_frame(PROOF_BYTES), dials)

    first = media_proxy.fetch_remote_artifact(_remote(), store_root=cache_root, dial=dial)
    second = media_proxy.fetch_remote_artifact(_remote(), store_root=cache_root, dial=dial)

    assert first == PROOF_BYTES
    assert second == PROOF_BYTES
    assert dials == ["install-b"]


def test_the_cache_answers_after_the_peer_is_switched_off(cache_root):
    """The property worth having, and the reason the cache is consulted FIRST:
    a picture the operator has already opened keeps opening when the other
    machine is off."""

    media_proxy.fetch_remote_artifact(
        _remote(), store_root=cache_root, dial=_dialler(_result_frame(PROOF_BYTES), [])
    )

    def dead(_peer):
        raise ConnectionError("no endpoint answered")

    assert (
        media_proxy.fetch_remote_artifact(_remote(), store_root=cache_root, dial=dead)
        == PROOF_BYTES
    )


def test_the_request_carries_the_handle_and_no_path(cache_root):
    connection = _PeerConnection(_result_frame(PROOF_BYTES))
    media_proxy.fetch_remote_artifact(
        _remote(),
        store_root=cache_root,
        dial=lambda _peer: (connection, {"event": "hello_ok"}),
    )

    assert connection.sent[0]["method"] == media_proxy.PEER_MEDIA_GET_METHOD
    assert connection.sent[0]["params"] == {"handle": PROOF_HANDLE}
    # The reference is the join key A holds and the one thing it must never
    # send: the asymmetry the whole family is built on, across an install edge.
    assert "reference" not in json.dumps(connection.sent[0])
    assert connection.closed is True


def test_an_unreachable_peer_is_transport_and_says_so(cache_root):
    def dead(_peer):
        raise ConnectionError("no endpoint answered")

    refusal = media_proxy.fetch_remote_artifact(_remote(), store_root=cache_root, dial=dead)

    assert isinstance(refusal, media_handles.MediaRefusal)
    assert refusal.reason == media_proxy.REASON_PEER_UNREACHABLE
    assert refusal.detail == {"peer_install_id": "install-b"}


def test_the_unreachable_word_is_the_one_the_dispatch_lane_already_uses():
    """One condition, one word, two lanes. An operator reading Activity must not
    have to learn that ``peer_unreachable`` and some second spelling mean the
    same machine being off."""

    assert media_proxy.REASON_PEER_UNREACHABLE == dispatch_store.REMOTE_UNREACHABLE_REASON


def test_a_peer_that_refuses_is_not_a_transport_failure(cache_root):
    """R8's distinction, at the media hop: unreachable might work tomorrow, and
    refused is a fact about the other install's state. The far side's own word
    rides under a key that says whose it is."""

    frame = {
        "jsonrpc": "2.0",
        "id": "peer-media-get",
        "error": {
            "code": -32000,
            "message": "peer.media.get refused: unknown_handle",
            "data": {"reason": media_handles.REASON_UNKNOWN_HANDLE},
        },
    }
    refusal = media_proxy.fetch_remote_artifact(
        _remote(), store_root=cache_root, dial=_dialler(frame, [])
    )

    assert refusal.reason == media_proxy.REASON_PEER_REFUSED
    assert refusal.detail == {"peer_reason": media_handles.REASON_UNKNOWN_HANDLE}


def test_bytes_that_do_not_hash_to_the_handle_are_refused_and_never_cached(cache_root):
    """The arm that makes content addressing load-bearing rather than
    decorative. A paired install is another runtime, not a trusted subsystem:
    whatever it returned, it is not what the handle names."""

    dials: list[str] = []
    refusal = media_proxy.fetch_remote_artifact(
        _remote(),
        store_root=cache_root,
        dial=_dialler(_result_frame(b"not the proof"), dials),
    )

    assert refusal.reason == media_handles.REASON_UNKNOWN_HANDLE
    # NOT cached — otherwise one lie would poison the namespace forever, with no
    # invalidation protocol able to reach it.
    assert media_handles.read_cached_bytes(PROOF_HANDLE, root=cache_root) is None


def test_an_encoding_this_install_does_not_know_is_refused_never_guessed(cache_root):
    refusal = media_proxy.fetch_remote_artifact(
        _remote(),
        store_root=cache_root,
        dial=_dialler(_result_frame(PROOF_BYTES, encoding="hex"), []),
    )

    assert refusal.detail == {"peer_reason": "unsupported_encoding"}


def test_an_edge_that_dies_mid_fetch_is_transport(cache_root):
    class _Dead(_PeerConnection):
        def read_frame(self):
            return None

    refusal = media_proxy.fetch_remote_artifact(
        _remote(),
        store_root=cache_root,
        dial=lambda _p: (_Dead(None), {"event": "hello_ok"}),
    )

    assert refusal.reason == media_proxy.REASON_PEER_UNREACHABLE


# ── the cache itself ─────────────────────────────────────────────────────────


def test_the_cache_path_is_the_digest_and_a_bad_handle_has_none(cache_root):
    path = media_handles.remote_cache_path(PROOF_HANDLE, root=cache_root)

    assert path.name == PROOF_HANDLE[len("sha256:") :] + ".bin"
    assert cache_root in path.parents
    # No caller-chosen string ever reaches a path segment, so there is no
    # traversal question here — only the grammar there already was.
    assert media_handles.remote_cache_path("X:\\Windows\\win.ini", root=cache_root) is None
    assert media_handles.remote_cache_path("../../etc/passwd", root=cache_root) is None


def test_a_tampered_cache_entry_is_deleted_rather_than_served(cache_root):
    """The cache sits on a disk other things can touch. Re-hashing on READ is
    what keeps a handle naming bytes rather than naming a filename."""

    assert media_handles.write_cached_bytes(PROOF_HANDLE, PROOF_BYTES, root=cache_root)
    path = media_handles.remote_cache_path(PROOF_HANDLE, root=cache_root)
    path.write_bytes(b"something else entirely")

    assert media_handles.read_cached_bytes(PROOF_HANDLE, root=cache_root) is None
    assert not path.exists()


def test_the_cache_refuses_to_write_bytes_that_are_not_the_handle(cache_root):
    assert media_handles.write_cached_bytes(PROOF_HANDLE, b"nope", root=cache_root) is False
    assert media_handles.read_cached_bytes(PROOF_HANDLE, root=cache_root) is None
