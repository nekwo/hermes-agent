"""Office push: a RE-ENVELOPE of an existing derivation, never a second one.

Operator ruling 2026-08-15, option (A). The office's delta lane already exists
and works: ``state_patches.emit_office_actor_patch`` writes an ``office_actor``
``upsert`` into the event log, and ``stream.patch_batch_frame`` assembles a
batch of them into ``{"type": "patch", base_offset, watermark, patches}``. The
only thing wrong with it is the ENVELOPE — a bare NDJSON frame with no
correlation rule, which is how a push could arrive and be dropped on the floor
with nothing to say so.

So this module adds no derivation. It registers with the SAME
:class:`~agent_runtime.serve_stream_hub.StreamHub` every stream client uses,
under a sink that re-wraps the frames it cares about as JSON-RPC
notifications. Three things follow from that choice, and each was a reason to
prefer it over tailing the event log a second time:

* one producer, so a patch cannot exist on one lane and not the other;
* an RPC subscriber COUNTS as a subscriber. This is load-bearing rather than
  incidental: the hub stops producing when its room empties, so once the
  launcher stops joining the legacy stream — the whole point of the ruling —
  a fan-out that merely OBSERVED the hub would observe a producer that had
  already exited, and the office would silently stop updating;
* backpressure, drop accounting and the bounded buffer are inherited whole.

What crosses, and what does not
-------------------------------
The sink is ADDRESSED where a frame is broadcast. It keeps only patches whose
entity is ``office_actor`` and whose id sits under this subscription's
workspace — which is what ``office_actor_patch_id``'s ``"<workspace_id>/"``
prefix was built for. A batch with nothing for this workspace sends NOTHING,
rather than a frame the client opens and discards.

A ``hydrate`` or ``delta`` frame means the batch was NOT patch-coverable
(``batch_is_patch_coverable`` said no — a create moved ``actor_count``, an
archive rewrote the resurrection ledger), so there is no honest office patch to
send. That becomes :data:`OFFICE_RESYNC_METHOD` and the client re-subscribes.
An UNKNOWN frame type takes the same branch deliberately: a type this module
has not been taught is, by definition, a change it cannot express as a patch,
and the conservative direction is a refetch rather than a silent gap.

``heartbeat`` is skipped. It is the legacy lane's liveness signal and carries no
state; the subscribe lane's own bookmark/heartbeat is separate, undesigned work
and must not be faked out of this.

The baseline rule
-----------------
ONE rule absorbs two different seams, which is why it is stated as an offset
comparison rather than as two guards:

* :meth:`StreamHub.subscribe` deliberately restarts the producer so a late
  joiner's first frame is a hydrate. We do not want that hydrate — the
  subscribe REPLY already carried the baseline.
* the dispatcher emits a handler's reply AFTER the handler returns, so a patch
  pushed in between lands BEFORE the baseline it rebases on.

Both are "this frame is at or behind what the client already has", so both are
answered by dropping any frame whose ``watermark.event_offset`` does not exceed
the offset captured with the baseline. That capture happens under the office
lock in :mod:`agent_runtime.serve_rpc`; here it is just a number.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from .serve_rpc import notification
from .state_patches import OFFICE_ACTOR_ENTITY

#: The push. Params mirror the patch frame's own body minus its envelope, so a
#: client's fold is byte-identical work on either lane — which is what makes
#: the legacy frames deletable rather than merely deprecated.
OFFICE_PATCH_METHOD = "runtime.office.patch"

#: "I cannot express what just happened as a patch — call subscribe again."
#: Carries the workspace and a reason and nothing else: a resync that shipped
#: partial state would be a third lane.
OFFICE_RESYNC_METHOD = "runtime.office.resync"

#: Frame types that are a FULL CORE for this lane's purposes: the batch was not
#: patch-coverable, so no office patch exists to forward.
_FULL_CORE_FRAME_TYPES = frozenset({"hydrate", "delta"})

#: Carries no state, so it is neither a patch nor a resync.
_LIVENESS_FRAME_TYPES = frozenset({"heartbeat"})


def office_subscription_key(connection_key: str | None, workspace_id: str) -> str:
    """The hub key for one connection's subscription to one workspace.

    Namespaced, and that is not cosmetic. ``serve.py``'s stream lane already
    subscribes under the BARE connection key (``_owner_of``), and
    :meth:`StreamHub.subscribe` refuses a duplicate key — so an office
    subscription that reused it would either be refused or, worse, silently
    take the stream client's place. The prefix also lets the release path find
    every office subscription a departing connection owns without keeping a
    second index in sync with the hub's.
    """

    return f"rpc:office:{connection_key or 'stdio'}:{workspace_id}"


def _event_offset_of(watermark: Any) -> int | None:
    """The frame's post-batch offset, or None when it carries none.

    None is a THIRD answer, not a zero. A frame we cannot place must not be
    silently dropped by the baseline gate — an unplaceable frame is exactly the
    case where a resync is the honest reply, and coercing it to 0 would make it
    look like ancient history and drop it.
    """

    if not isinstance(watermark, dict):
        return None
    raw = watermark.get("event_offset")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def office_patch_sink(
    *,
    workspace_id: str,
    baseline_offset: int,
    emit: Callable[[dict[str, Any]], None],
) -> Callable[[dict[str, Any]], None]:
    """Adapt stream frames to office notifications for ONE subscriber.

    Returned as a closure rather than a method so it can be unit-tested against
    hand-built frames without a hub, a socket or a serve loop — the filtering
    rules are where this lane can go quietly wrong, and they deserve a test that
    is not an integration test.
    """

    prefix = f"{workspace_id}/"

    def _deliver(frame: dict[str, Any]) -> None:
        if not isinstance(frame, dict):
            return
        frame_type = frame.get("type")
        if frame_type in _LIVENESS_FRAME_TYPES:
            return
        watermark = frame.get("watermark") or {}
        event_offset = _event_offset_of(watermark)
        # THE BASELINE GATE RUNS BEFORE THE TYPE BRANCH, and that ordering is
        # the whole rule rather than a detail.
        #
        # It was written the other way round first, and the lane did not work:
        # `StreamHub.subscribe` deliberately restarts the producer so a late
        # joiner's first frame is a HYDRATE — a full core — which the type
        # branch answered with an unconditional resync. That is a LOOP, not a
        # slow start: the client resyncs, re-subscribes, the hub restarts the
        # producer, the new hydrate resyncs it again, and every other
        # subscriber on that shared producer pays a fresh core each lap. The 23
        # fake-hub tests could not see it, because a fake never delivers a
        # frame nobody asked for.
        #
        # Stated positively: a frame at or behind what the subscribe reply
        # already carried has nothing to tell this client, WHATEVER its type.
        # Only frames that are genuinely new get classified.
        if event_offset is not None and event_offset <= int(baseline_offset or 0):
            return
        if frame_type != "patch":
            # Includes the unknown-type branch on purpose — see the module
            # docstring. A resync is recoverable; a dropped change is not.
            #
            # An UNPLACEABLE frame (no readable watermark) reaches here too, and
            # must: the gate above deliberately does not fire without an offset,
            # because silently dropping a frame we cannot place would turn the
            # one recoverable outcome into the unrecoverable one.
            emit(
                notification(
                    OFFICE_RESYNC_METHOD,
                    {
                        "workspace_id": workspace_id,
                        "reason": "full_core"
                        if frame_type in _FULL_CORE_FRAME_TYPES
                        else "unknown_frame_type",
                    },
                )
            )
            return
        rows = [
            patch
            for patch in frame.get("patches") or []
            if isinstance(patch, dict)
            and patch.get("entity") == OFFICE_ACTOR_ENTITY
            and str(patch.get("id") or "").startswith(prefix)
        ]
        if not rows:
            # Addressed, not broadcast: a batch that moved another workspace's
            # actors is not this subscriber's business and costs it nothing.
            return
        emit(
            notification(
                OFFICE_PATCH_METHOD,
                {
                    "workspace_id": workspace_id,
                    "base_offset": frame.get("base_offset"),
                    "watermark": watermark,
                    "patches": rows,
                },
            )
        )

    return _deliver


class OfficeSubscriptions:
    """Which connections are subscribed to which workspaces, and the teardown.

    Process-global by the same reasoning as ``serve_rpc._METHODS``: a method
    handler is reached through a module-level registry and has no server object
    to ask, and threading a hub through :class:`~agent_runtime.serve_rpc.
    RpcContext` would put a server-wide service on a per-request value that is
    documented as answering WHO called.

    The hub arrives as a FACTORY, not an instance. ``serve.py`` builds its hub
    lazily on first use and can close and replace it across a drain, so a
    captured instance would go stale exactly when a client reconnects.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hub_factory: Callable[[], Any] | None = None
        #: connection key -> the hub keys it owns. The hub is the authority on
        #: whether a subscription is live; this is only the index the release
        #: path needs, and it is pruned in the same breath as the hub call.
        self._owned: dict[str, set[str]] = {}

    # ── wiring ──────────────────────────────────────────────────────────────

    def bind(self, hub_factory: Callable[[], Any] | None) -> None:
        with self._lock:
            self._hub_factory = hub_factory
            if hub_factory is None:
                self._owned.clear()

    def bound(self) -> bool:
        with self._lock:
            return self._hub_factory is not None

    # ── the lane ────────────────────────────────────────────────────────────

    def subscribe(
        self,
        *,
        connection_key: str | None,
        workspace_id: str,
        baseline_offset: int,
        emit: Callable[[dict[str, Any]], None],
    ) -> bool:
        """Register one subscription. False when the lane cannot carry it.

        False is returned — rather than raised — for the two states a caller
        must answer differently from a crash: no hub bound (this runtime has no
        socket lane, so there is nothing to push over) and a duplicate key (the
        client is already subscribed to this workspace). The method handler
        turns each into its own typed error.
        """

        with self._lock:
            factory = self._hub_factory
        if factory is None:
            return False
        hub = factory()
        if hub is None:
            return False
        key = office_subscription_key(connection_key, workspace_id)
        sink = office_patch_sink(
            workspace_id=workspace_id,
            baseline_offset=baseline_offset,
            emit=emit,
        )

        def _on_drop(_key: str, payload: dict[str, Any]) -> None:
            # A dropped subscriber missed frames by definition, so the honest
            # thing is not silence: tell it to refetch. Best-effort — the drop
            # may itself be a dead socket, and the hub has already accounted it.
            try:
                emit(
                    notification(
                        OFFICE_RESYNC_METHOD,
                        {
                            "workspace_id": workspace_id,
                            "reason": str(payload.get("reason") or "subscription_dropped"),
                        },
                    )
                )
            except Exception:
                pass

        if not hub.subscribe(key, sink=sink, on_drop=_on_drop):
            return False
        with self._lock:
            self._owned.setdefault(str(connection_key or "stdio"), set()).add(key)
        return True

    def release(self, connection_key: str | None) -> int:
        """Drop every office subscription this connection owns. Returns a count.

        Called from ``serve.py``'s ``_release_subscription``, which already
        unsubscribes the STREAM lane's bare key. The office keys are namespaced
        away from that one, so neither release can take the other's
        subscription — and a connection that never subscribed here costs one
        dictionary lookup.
        """

        owner = str(connection_key or "stdio")
        with self._lock:
            keys = self._owned.pop(owner, set())
            factory = self._hub_factory
        if not keys:
            return 0
        hub = factory() if factory is not None else None
        released = 0
        for key in keys:
            if hub is None:
                continue
            try:
                if hub.unsubscribe(key):
                    released += 1
            except Exception:
                # Teardown never raises into a disconnect path: the client is
                # already gone and the hub's own accounting has the truth.
                pass
        return released

    def owned_keys(self, connection_key: str | None) -> set[str]:
        with self._lock:
            return set(self._owned.get(str(connection_key or "stdio"), set()))


#: The process's registry. One per runtime, like the method table beside it.
OFFICE_SUBSCRIPTIONS = OfficeSubscriptions()
