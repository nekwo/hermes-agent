"""Telling paired installs what changed here. The outbound half of ``peer.announce``.

S2c, ruling R-IP12: *one local store, pushed never polled, filtered never
copied.* This module is the "pushed" half. Something changed about this install
— its name, its addresses, its certificate, its roster, or an operator's
decision to cut an edge — and every install that holds a row about us is told
once, now, instead of finding out at its next failed call.

Why a module rather than a method on the store
-----------------------------------------------

Because it DIALS, and ``gateway_peers`` deliberately does not know how to want
anything. That module answers questions about credentials and rows; putting a
fan-out with a thread and a timeout in it would mean a credential store that can
block. Here the network is the subject and the store is an input, which is the
same split ``dial_peer`` already draws — it dials, and it is the one function in
``gateway_peers`` that does, with its own docstring saying so.

What it is NOT
--------------

**Not a protocol, and not reliable delivery.** An announce is best-effort by
design: at most two attempts, a short timeout, and a failure that is RECORDED
and then dropped. That is not a shortcut around a hard problem, it is the
correct posture for this fact — everything an announce carries is also
discoverable the slow way (the next hello refreshes the cache; the next call to
a revoked edge is refused). The push makes the news arrive in seconds instead of
at the next attempt; nothing depends on it arriving.

**Not a broadcast of anything private.** The payload is what this install
already tells any peer that dials it: a display name, the addresses it listens
on, its certificate fingerprint, and two booleans. Nothing here is a credential
and there is no field for one.

**Not a loop.** An inbound announce never triggers an outbound one — the
handler drops a stale roster rather than fetching a fresh one, precisely so two
installs cannot bounce notifications between them. Every call in this module is
started by a LOCAL event: a rename, a boot, a create, a retire, a revoke.

The ordering that matters (R-S2-15)
------------------------------------

``harness gateway peers revoke`` announces ``revoked_you`` BEFORE it writes the
local revocation, and the order is load-bearing rather than tidy: the announce
is a call to the peer, and a peer we have already revoked would be refused at
our own door on the way back. Announcing first means the far install learns it
was cut while the edge still works; announcing after would mean the news went
out over a connection this install had just closed to it. A failed announce
never blocks the revoke — the ack says ``announced: false`` and the far side
learns at its next dial, exactly as it did before this module existed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "ANNOUNCE_ATTEMPTS",
    "ANNOUNCE_METHOD",
    "ANNOUNCE_TIMEOUT_SECONDS",
    "AnnounceReceipt",
    "announce_in_background",
    "announce_to_peers",
]

#: The method every announce calls. A constant rather than a literal at four
#: call sites, for the reason every wire name in this repo is one.
ANNOUNCE_METHOD = "peer.announce"

#: Two attempts, not one and not five. One is a single dropped packet away from
#: silence on a fact that matters; more than two turns a best-effort courtesy
#: into a retry storm against an install that is simply switched off — and the
#: cache's ``unreachable`` word is the correct place for that peer to rest until
#: its next hello.
ANNOUNCE_ATTEMPTS = 2

#: Short, because nothing waits on this. The bound on the whole fan-out is
#: ``peers × attempts × timeout``, which is a number an operator can compute
#: rather than a queue they have to reason about.
ANNOUNCE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class AnnounceReceipt:
    """What happened when this install tried to tell ONE peer.

    Returned rather than logged-and-forgotten because one caller genuinely acts
    on it: ``peers revoke`` puts ``announced`` on its ack, so an operator knows
    whether the other machine has been told or will find out at its next dial.
    """

    peer_install_id: str
    ok: bool
    error: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "peer_install_id": self.peer_install_id,
            "ok": self.ok,
            "error": self.error,
        }


def announce_to_peers(
    store_root: Path | str,
    payload: dict[str, Any],
    *,
    only: Any = None,
    attempts: int = ANNOUNCE_ATTEMPTS,
    timeout: float = ANNOUNCE_TIMEOUT_SECONDS,
) -> list[AnnounceReceipt]:
    """Send one announce to every usable peer (or just to *only*). Never raises.

    ``only`` is an iterable of install ids — the revoke path names exactly one,
    because announcing "you are revoked" to every peer would tell four installs
    a fact about a fifth.

    Iterates :func:`~agent_runtime.gateway_peers.usable_peers`, so an expired
    edge, a revoked one, and one that already told us it revoked US are skipped
    without an attempt. That is the same predicate the resolver and the HUD read
    — a fan-out that used its own definition of "reachable" would eventually
    dial an edge the rest of the runtime had written off.
    """

    from .gateway_peers import note_dial_result, usable_peers

    root = Path(store_root)
    wanted = None if only is None else {str(item).strip() for item in only if item}
    receipts: list[AnnounceReceipt] = []

    try:
        candidates = usable_peers(root)
    except Exception:  # pragma: no cover - defensive; an unreadable store
        return receipts

    for peer in candidates:
        peer_install_id = peer.record.peer_install_id
        if wanted is not None and peer_install_id not in wanted:
            continue
        last_error: str | None = None
        for _attempt in range(max(1, int(attempts))):
            outcome = _announce_once(root, peer_install_id, payload, timeout=timeout)
            if outcome is None:
                last_error = None
                break
            last_error = outcome
        ok = last_error is None
        receipts.append(
            AnnounceReceipt(peer_install_id=peer_install_id, ok=ok, error=last_error)
        )
        # Recorded through the SAME door a chat dial records through, so
        # "reachable" means one thing across the runtime rather than one thing
        # per lane. A revoke's own announce is exempt: a peer we are cutting is
        # about to be unusable anyway, and marking it unreachable on the way out
        # would put a misleading word on the row an operator then reads.
        if not payload.get("revoked_you"):
            try:
                note_dial_result(root, peer_install_id, ok=ok, error=last_error)
            except Exception:
                pass
    return receipts


def announce_in_background(
    store_root: Path | str, payload: dict[str, Any], **kwargs: Any
) -> None:
    """Fire :func:`announce_to_peers` on a daemon thread and return immediately.

    For the callers whose work must not wait on a peer: a rename, a create, a
    retire, the serve's own post-boot announce. A slow install on the far side
    of a LAN must never be the reason ``harness agent create`` takes five
    seconds — the whole point of pushing is that it costs the pusher nothing.

    Daemon so it can never hold a process open, and swallowing so a broken edge
    cannot surface as a traceback on a lane that did not ask about the network.
    Deliberately returns no handle: a caller that could wait on this would
    eventually wait on it.
    """

    import threading

    def _run() -> None:
        try:
            announce_to_peers(store_root, payload, **kwargs)
        except Exception:  # noqa: BLE001 — courtesy channel, never the work
            pass

    threading.Thread(target=_run, name="gateway-announce", daemon=True).start()


def _announce_once(
    store_root: Path,
    peer_install_id: str,
    payload: dict[str, Any],
    *,
    timeout: float,
) -> str | None:
    """One attempt. ``None`` on success, else a short reason for the receipt."""

    from tools.agent_chat_remote import call_peer_method

    try:
        outcome = call_peer_method(
            store_root,
            peer_install_id,
            ANNOUNCE_METHOD,
            dict(payload),
            dial_timeout=timeout,
            reply_timeout=timeout,
        )
    except Exception as exc:  # pragma: no cover - call_peer_method returns typed
        return f"{type(exc).__name__}: {exc}"[:200]
    refusal = outcome.get("refusal")
    if refusal:
        return str(refusal.get("reason") or "refused")[:200]
    return None
