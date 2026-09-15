"""The serve's peer-directory door for its OWN launcher (S2d, launcher S3-R13).

Three surfaces, all additive, all answerable only to this install's own console.
They exist because of one measured fact about the launcher's stream lane, and
the fact is worth stating before the code:

**The launcher's hermes stream carries no events.** Its hydrate core is
``agents, boards, offices, persona_instances, running_work, …`` and its fold
entities are ``persona_instance, incident, office_*, scope``; hermes reads
``event_log.tail(20)`` only for parity warnings. So the five ``gateway.peer.*``
contracts S2c registers are visible to a stream consumer, a snapshot and an
operator — and are invisible to a launcher, forever, no matter how many of them
are emitted. Canon 03 invariant 6 says where new server→client push goes
instead: **JSON-RPC notifications.** That is this module.

The shape is ``runtime.office.subscribe`` → ``runtime.office.patch``'s, which is
the proven one on this lane, minus the parts the office needs and this does not:
no watermark, no fold-entity negotiation, no re-baselining receipt. A peer
directory is small, bounded by the number of machines an operator has paired,
and every notification carries the WHOLE row it is about — so a client
re-renders from the payload and never fetches, and a dropped frame costs one
row's freshness until the next change rather than a resync.

Why the door is console-only, at a ``read`` tier
-------------------------------------------------

The tier answers *what strength of credential does this want* and the honest
answer is ``read``: it reads two files and mutates nothing. What it must ALSO
say is a KIND, and the tier vocabulary has no word for that — which is the exact
gap ``LOCAL_CONSOLE_METHODS`` was added for (WS4 / R-B). A paired console
DEVICE is a real caller with a real credential, and the peer directory names
other machines and the addresses they are reachable at; that is the operator's
own map of their network, and it belongs to the console sitting at the install,
not to every phone they ever paired. So both read surfaces declare ``read`` and
join ``LOCAL_CONSOLE_METHODS``, and the third — which DIALS on the caller's
behalf — declares ``console`` and joins it too.

What a launcher does with them
-------------------------------

``subscribe`` on greet, then re-render from each ``changed`` notification.
``roster`` is the fetch-through the launcher cannot perform itself: a
``peer.roster.list`` is a PEER method and a launcher holds a DEVICE credential,
so it asks its own hermes — which IS a peer of that install — to ask, and the
answer lands in ``peers_cache.json`` where the next ``changed`` frame carries
it. One directory, two readers.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "PEER_DIRECTORY_CONTRACT",
    "PEER_DIRECTORY_CHANGED_METHOD",
    "PeerDirectorySubscriptions",
    "PEER_DIRECTORY_SUBSCRIPTIONS",
    "peer_directory_row",
    "peer_directory_rows",
]

#: This lane's own shape number, beside ``PEER_PING_CONTRACT``'s and for its
#: reason: it describes the subscribe result and the notification payload and
#: nothing else, so growing one of them does not tell every ``runtime.*`` client
#: that something moved.
PEER_DIRECTORY_CONTRACT = 1

#: The notification a subscriber receives. A constant because it is spelled in
#: three places — the fan-out, the tests, and the launcher's handler
#: registration — and a wire name spelled twice is a wire name that eventually
#: gets spelled differently once.
PEER_DIRECTORY_CHANGED_METHOD = "runtime.gateway.peers.changed"


def peer_directory_row(record: Any, cache: Any, *, usable_ref: str | None) -> dict:
    """ONE paired install, as the launcher may see it. Never a credential.

    Deliberately the SAME shape ``harness gateway peers list --json`` prints,
    because the launcher reads that shape already (it greets by running the CLI
    verb) and a push lane whose rows differed from the poll lane's would make
    every consumer branch on which door the row came through.

    The credential has no field here and none on ``PeerRecord`` either — the
    dataclass is what makes "the secret leaked into an operator surface"
    unrepresentable rather than merely unintended, and this function inherits
    that by building from it.
    """

    row = record.payload()
    row["cache"] = cache.payload() if cache is not None else None
    if cache is not None and cache.last_seen:
        # The LIVE stamp, from the file that owns it. The top-level key is kept
        # because the launcher reads it today; what changed in S2c is which
        # file answers.
        row["last_seen"] = cache.last_seen
    row["usable"] = usable_ref is not None
    row["ref"] = usable_ref
    row["unusable_reason"] = None if usable_ref is not None else _unusable_reason(
        record, cache
    )
    return row


def peer_directory_rows(store_root: Any) -> list[dict]:
    """Every paired install, revoked and expired ones included, oldest first.

    Unusable rows are KEPT and shown, for ``list_peers``' reason: a directory
    that hid them would make "never paired" and "thrown out" the same answer,
    and the second is the one an operator auditing a decommissioned machine
    needs. ``usable`` and ``unusable_reason`` are what let a sheet group them.
    """

    from .gateway_peers import list_peers, read_peer_cache, usable_peers

    cache = read_peer_cache(store_root)
    refs = {peer.record.peer_install_id: peer.ref for peer in usable_peers(store_root)}
    return [
        peer_directory_row(
            record,
            cache.get(record.peer_install_id),
            usable_ref=refs.get(record.peer_install_id),
        )
        for record in list_peers(store_root)
    ]


def _unusable_reason(record: Any, cache: Any) -> str:
    """The resolver's own vocabulary, so one condition has one word everywhere."""

    from .gateway_targets import (
        REASON_PEER_EXPIRED,
        REASON_PEER_REVOKED,
        REASON_PEER_REVOKED_YOU,
    )

    if record.revoked:
        return REASON_PEER_REVOKED
    if record.expired:
        return REASON_PEER_EXPIRED
    if cache is not None and cache.revoked_you:
        return REASON_PEER_REVOKED_YOU
    return ""


class PeerDirectorySubscriptions:
    """Which connections asked to hear about peer-directory changes.

    Process-global for ``OfficeSubscriptions``' reason: a method handler is
    reached through a module-level registry and has no server object to ask, and
    threading a hub through ``RpcContext`` would put a server-wide service on a
    per-request value documented as answering WHO called.

    Much smaller than the office's hub, and the difference is the DATA. An
    office patch is a delta against a watermark, so a subscriber that misses one
    is out of sync until it re-baselines. A peer-directory notification carries
    the WHOLE row it is about, so a dropped frame costs one row's freshness
    until that row next changes — which means this needs no watermark, no
    sequence gate and no re-baseline receipt, and inventing them would be three
    mechanisms guarding against a failure this shape does not have.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        #: connection key -> its frame sink. The key is the SUBSCRIPTION
        #: identity the teardown path uses (``RpcContext.connection_key``'s own
        #: contract), and the sink is what a notification is written to.
        self._sinks: dict[str, Callable[[dict], None]] = {}

    def register(self, key: Any, emit: Callable[[dict], None] | None) -> bool:
        """Subscribe one connection. ``False`` when it has no push channel.

        A caller with no channel is REFUSED rather than registered-and-ignored:
        a subscription that can never deliver is a promise the runtime cannot
        keep, and the honest moment to say so is the call that asked.
        """

        if emit is None or key is None:
            return False
        with self._lock:
            self._sinks[str(key)] = emit
        return True

    def release(self, key: Any) -> None:
        """A client left. Drop it, and do nothing else.

        Called from the same teardown the office lane's ``release`` is called
        from. A subscriber that outlived its connection would be a sink written
        to forever, which is how a fan-out starts holding a dead socket open.
        """

        if key is None:
            return
        with self._lock:
            self._sinks.pop(str(key), None)

    def publish(self, params: dict[str, Any]) -> int:
        """Push one notification to every subscriber. Returns how many took it.

        Every failure is SWALLOWED and the subscriber DROPPED, which is the
        opposite of ``RpcContext.push``'s posture and deliberately so: a push
        raised from inside a handler is reportable on the call that tried it,
        while a fan-out has no call to report on and a raising sink would take
        down a store write that had already succeeded. The store is the truth;
        this lane is how somebody hears about it.
        """

        with self._lock:
            targets = list(self._sinks.items())
        delivered = 0
        dead: list[str] = []
        for key, emit in targets:
            try:
                emit(
                    {
                        "jsonrpc": "2.0",
                        "method": PEER_DIRECTORY_CHANGED_METHOD,
                        "params": params,
                    }
                )
                delivered += 1
            except Exception:
                dead.append(key)
        if dead:
            with self._lock:
                for key in dead:
                    self._sinks.pop(key, None)
        return delivered

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._sinks)


#: The process-global registry. One per runtime, like ``OFFICE_SUBSCRIPTIONS``.
PEER_DIRECTORY_SUBSCRIPTIONS = PeerDirectorySubscriptions()


def publish_peer_event(
    event_type: str, payload: dict[str, Any], *, store_root: Any = None
) -> None:
    """Turn one ``gateway.peer.*`` event into a ``changed`` notification.

    Wired into ``gateway_peers``' own emitter so the two lanes cannot disagree
    about WHEN something changed: the EventLog append and this push happen at
    the same call site, from the same process, for the same write. A second
    trigger — a file watcher, a poll, a hook on the store — would be a second
    opinion about the same fact, and the two would drift on exactly the writes
    that matter.

    The row is looked up FRESH rather than carried in the event payload, because
    the event payload is deliberately ids-and-counts only (no secret, no
    endpoint list, no roster body — it is a log). The notification is a
    different audience with a different rule: it goes to this install's own
    console, over a local socket, and its whole job is to spare that console a
    fetch.

    ``store_root`` is the root the write actually LANDED in, threaded from the
    writer rather than re-derived. Every function in ``gateway_peers`` takes its
    root as an input because several roots coexist on this machine, and a
    notification that resolved its own could describe a different store from the
    one that changed — the same class of bug the input rule exists to prevent.
    ``None`` falls back to the head home's root, which is the right answer for
    the one caller that has no root in hand (a serve reading its own directory).

    ``peer`` is ``null`` when the row is gone — which today only happens if
    something removed it out of band, since a revoke KEEPS the row. Modelled
    anyway so a client never has to distinguish "absent key" from "removed".
    """

    if PEER_DIRECTORY_SUBSCRIPTIONS.subscriber_count() == 0:
        # Nothing to tell. Checked first so a runtime with no launcher attached
        # pays nothing at all for this lane — not a store read, not a lock.
        return
    peer_install_id = str(payload.get("peer_install_id") or "").strip()
    row = None
    revision: list[int] = []
    try:
        from .gateway_peers import (
            list_peers,
            peer_store_revision,
            read_peer_cache,
            usable_peers,
        )
        from .gateway_targets import peer_store_root

        root = Path(store_root) if store_root is not None else peer_store_root()
        revision = list(peer_store_revision(root))
        if peer_install_id:
            record = next(
                (
                    candidate
                    for candidate in list_peers(root)
                    if candidate.peer_install_id == peer_install_id
                ),
                None,
            )
            if record is not None:
                refs = {
                    peer.record.peer_install_id: peer.ref
                    for peer in usable_peers(root)
                }
                row = peer_directory_row(
                    record,
                    read_peer_cache(root).get(peer_install_id),
                    usable_ref=refs.get(peer_install_id),
                )
    except Exception:  # noqa: BLE001 — a notification is never the mutation
        row = None

    params: dict[str, Any] = {
        "contract": PEER_DIRECTORY_CONTRACT,
        "event": event_type,
        "peer_install_id": peer_install_id or None,
        "peer": row,
        "store_revision": revision,
    }
    grant_id = payload.get("grant_id")
    if grant_id:
        params["grant_id"] = str(grant_id)
    PEER_DIRECTORY_SUBSCRIPTIONS.publish(params)
