"""Fetch ONE artifact's bytes from a paired install, verify them, cache them.

Stage P4, ruling R-P3. The second hop of the cross-install media lane, and the
only hop that leaves this machine.

Why the device does not dial B itself
-------------------------------------

The obvious shape is the cheap one: A's launcher already knows the artifact
lives on install B, so let the device open its own connection to B and ask.
R-P3 rejected it and the reason is the pairing model, not the transport. A
device is paired with ONE install — that is the whole content of
``gateway/devices.json``, and it is what lets an operator say "this phone may
drive this machine" without saying anything about every machine that machine
happens to know. A device that dialled B would need a credential B minted,
which means the operator pairs the phone again for every peer, and the
device-talks-to-one-install contract is gone. So the fetch is PROXIED: the
device asks its own install, and its own install spends the peer edge two
operators already approved.

Why not send the bytes with the reply
--------------------------------------

The other cheap shape is eager: B already read the file to hash it, so put the
base64 on the completion and be done. Two things forbid it. The completion
rides the EventLog lane whose payload cap is 4096 bytes, so a megabyte of
picture is not merely wasteful there, it is unrepresentable. And it is eager
delivery of artifacts nobody may ever open — a dispatch that returns a
screenshot the operator never clicks would have paid for it anyway, on every
retry. The map is small, the bytes are lazy, and the cache means "lazy" costs
one dial ever.

What this module refuses to do
-------------------------------

It never sends a path and never sends a reference. What crosses is the handle B
minted, which B can only answer for out of its own scope — so this is A
spending a name B gave it, not A browsing B. It never chains: ``peer.media.get``
on the far side resolves LOCAL rows only, so a handle that is remote on B is
``unknown_handle`` rather than a third hop, and there is no cycle to bound.

And it never trusts what comes back. The bytes are re-hashed against the handle
before anything is served or cached, which content addressing makes free: if
they do not hash to the name, they are not the artifact, and the answer is
``unknown_handle`` — the same word this install would use for a digest nobody
has. A paired install that lied gets no channel out of it.

Where this runs, and what still bounds it
-----------------------------------------

NOT on the serve reader loop. Stage P4 shipped it there, because
``runtime.media.get`` is answered there, and filed the cost: a dial to a machine
that is off stalled that loop — and every other request from that client — for
this module's timeouts. Since 2026-09-02 the dispatcher hands the fetch to the
serve worker pool through ``RpcContext.spawn_reply`` and the reply is emitted
from the worker on the request's own id; ``_runtime_media_get``'s docstring is
the authority on that seam.

The timeouts below did NOT relax with the move, and that is deliberate. They are
this work's own bound, at its source, which is the whole reason no watchdog was
added on the reader when the dial was there: a second clock over work that
already carries one is how a wait becomes unattributable. A pool worker is
cheaper to hold than a reader, not free.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from . import media_handles

logger = logging.getLogger(__name__)

__all__ = [
    "PEER_DIAL_TIMEOUT_SECONDS",
    "PEER_READ_TIMEOUT_SECONDS",
    "PEER_MEDIA_GET_METHOD",
    "REASON_PEER_REFUSED",
    "REASON_PEER_UNREACHABLE",
    "fetch_remote_artifact",
]

#: The verb this module spends on the far install. One name, one place.
PEER_MEDIA_GET_METHOD = "peer.media.get"

#: How long ONE dial may take before the far install counts as unreachable.
#: Deliberately a THIRD of ``agent_chat_dispatch.PEER_DIAL_TIMEOUT_SECONDS``
#: rather than the same number: that constant is spent on a supervisor thread
#: that may block as long as it likes. This one was chosen while the dial was on
#: the reader loop; it is KEPT at that number now the dial is on a pool worker,
#: because the pool is bounded too — a wedged fetch holds a worker every other
#: request on this serve may be queued for. A different question gets a
#: different number, and both say why.
PEER_DIAL_TIMEOUT_SECONDS = 5.0

#: How long the far install has to answer once dialled. Larger than the dial —
#: it may be reading and base64-ing up to 5 MiB — and still bounded, because an
#: install that accepted the connection and then went quiet is a stall on this
#: loop exactly like an unreachable one.
PEER_READ_TIMEOUT_SECONDS = 30.0

#: Every attempt to reach the paired install failed. Spelled to equal
#: ``dispatch_store.REMOTE_UNREACHABLE_REASON`` and FENCED against it by test,
#: because the operator reads one word for one condition on two lanes.
REASON_PEER_UNREACHABLE = "peer_unreachable"

#: The far install answered, and its answer was a refusal. Distinct from
#: unreachable for the R8 distinction this whole program rests on: unreachable
#: might work tomorrow, refused is a fact about the other install's state.
REASON_PEER_REFUSED = "peer_refused"


def fetch_remote_artifact(
    artifact: media_handles.RemoteMediaArtifact,
    *,
    store_root: Any = None,
    dial: Any = None,
) -> bytes | media_handles.MediaRefusal:
    """The bytes behind a remote handle: cache, else one dial, verified.

    ``dial`` and ``store_root`` are injection seams, not configuration. A test
    that had to stand up a second TLS listener to prove "a mismatched digest is
    refused" would be proving the listener; the wire itself is proven by the
    two-roots acceptance, once, against real serves.

    Returns the bytes, or a :class:`~agent_runtime.media_handles.MediaRefusal`
    whose reason is one of :data:`REASON_PEER_UNREACHABLE`,
    :data:`REASON_PEER_REFUSED`,
    ``media_handles.REASON_UNKNOWN_HANDLE`` (the bytes did not hash to the name)
    or ``media_handles.REASON_ARTIFACT_TOO_LARGE``.

    **The cache is consulted before anything else and that is the acceptance's
    "second fetch costs zero dials".** It is checked even for an install that is
    currently unreachable, which is the property worth having: a picture the
    operator has already seen keeps opening after the other machine is switched
    off.
    """

    cached = media_handles.read_cached_bytes(artifact.handle, root=store_root)
    if cached is not None:
        return cached

    dialler = dial if dial is not None else _dial_peer
    try:
        connection, _hello = dialler(artifact.peer_install_id)
    except Exception as exc:  # noqa: BLE001 - every dial failure is transport
        logger.info(
            "media proxy: %s is unreachable (%s)",
            artifact.peer_install_id,
            type(exc).__name__,
        )
        return media_handles.MediaRefusal(
            REASON_PEER_UNREACHABLE,
            {"peer_install_id": artifact.peer_install_id},
        )

    try:
        answer = _ask(connection, artifact.handle)
    except Exception as exc:  # noqa: BLE001 - a dead edge mid-request is transport
        logger.info(
            "media proxy: the edge to %s died mid-fetch (%s)",
            artifact.peer_install_id,
            type(exc).__name__,
        )
        return media_handles.MediaRefusal(
            REASON_PEER_UNREACHABLE,
            {"peer_install_id": artifact.peer_install_id},
        )
    finally:
        try:
            connection.close()
        except Exception:  # pragma: no cover - defensive
            pass

    if isinstance(answer, media_handles.MediaRefusal):
        return answer

    # Everything below is the far install's answer being CHECKED rather than
    # believed. A peer is another runtime, not a trusted subsystem.
    if len(answer) > media_handles.MAX_FETCH_BYTES:
        return media_handles.MediaRefusal(
            media_handles.REASON_ARTIFACT_TOO_LARGE,
            {"cap_bytes": media_handles.MAX_FETCH_BYTES, "size_bytes": len(answer)},
        )
    if media_handles.handle_for_bytes(answer) != artifact.handle:
        # The one arm that makes content addressing load-bearing rather than
        # decorative: whatever came back, it is not what the handle names, so it
        # is refused with the word for "this install cannot produce those
        # bytes" — and it is NOT cached, so a peer cannot poison the namespace.
        logger.warning(
            "media proxy: %s answered bytes that do not match the handle",
            artifact.peer_install_id,
        )
        return media_handles.MediaRefusal(media_handles.REASON_UNKNOWN_HANDLE)

    media_handles.write_cached_bytes(artifact.handle, answer, root=store_root)
    return answer


# ── internals ────────────────────────────────────────────────────────────────


def _dial_peer(peer_install_id: str):
    from .gateway_peers import dial_peer
    from .gateway_targets import peer_store_root

    connection, hello = dial_peer(
        peer_store_root(),
        peer_install_id,
        client="hermes-media-proxy",
        timeout_seconds=PEER_DIAL_TIMEOUT_SECONDS,
    )
    connection.set_timeout(PEER_READ_TIMEOUT_SECONDS)
    return connection, hello


def _ask(connection: Any, handle: str) -> bytes | media_handles.MediaRefusal:
    """One ``peer.media.get`` round trip on an open edge."""

    rid = "peer-media-get"
    connection.send(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": PEER_MEDIA_GET_METHOD,
            "params": {"handle": handle},
        }
    )
    while True:
        frame = connection.read_frame()
        if frame is None:
            raise ConnectionError("the edge closed before an answer")
        if frame.get("id") != rid:
            # Another request's frames or a push on the same socket. Not ours.
            continue
        if "error" in frame:
            error = frame.get("error") or {}
            peer_reason = str((error.get("data") or {}).get("reason") or "")
            return media_handles.MediaRefusal(
                REASON_PEER_REFUSED,
                # The far install's own word, forwarded under a key that says
                # WHOSE it is. Collapsing it into this install's `reason` would
                # make "B says it has never heard of that handle" and "A has
                # never heard of it" the same answer, and they lead to
                # different repairs.
                {"peer_reason": peer_reason} if peer_reason else None,
            )
        if "result" not in frame:
            continue
        result = frame.get("result") or {}
        return _decode(result)


def _decode(result: dict) -> bytes | media_handles.MediaRefusal:
    """A ``peer.media.get`` result's ``data`` field → bytes.

    The encoding is READ rather than assumed, for the reason
    ``runtime.media.get``'s own docstring states about stating it: a reply that
    silently changed encoding is an image that decodes to noise. An encoding
    this install does not know is refused, never guessed at.
    """

    if str(result.get("encoding") or "") != "base64":
        return media_handles.MediaRefusal(
            REASON_PEER_REFUSED, {"peer_reason": "unsupported_encoding"}
        )
    raw = result.get("data")
    if not isinstance(raw, str):
        return media_handles.MediaRefusal(
            REASON_PEER_REFUSED, {"peer_reason": "no_data"}
        )
    try:
        return base64.b64decode(raw, validate=True)
    except Exception:  # noqa: BLE001 - undecodable is refused, never partial
        return media_handles.MediaRefusal(
            REASON_PEER_REFUSED, {"peer_reason": "undecodable_data"}
        )
