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

Why a second subscribe REPLACES rather than being refused
---------------------------------------------------------
Registering and answering in one call is what makes the baseline honest, and it
has one consequence that was not thought through when the lane landed: by the
time the client has PARSED the reply the subscription already exists. A client
that reads the baseline and finds it unusable — a truncated projection, an
unreadable watermark, a workspace id it did not ask for — is right to refuse it
rather than fold a knowingly-partial office as authoritative. But refusing left
a live subscription it would never fold against, and the old
``already_subscribed`` answer meant a retry on the same connection could not
reclaim it. Short of dropping the connection, the runtime could not take that
subscriber back.

So a second subscribe for the same ``(connection, workspace)`` now TEARS DOWN
the existing registration and installs a fresh sink at a fresh baseline. The
stuck subscription stops being a state that can exist, because the cure for a
bad baseline is the same call that produced it. The refusal it replaces existed
only to stop a subscriber leaking per retry, and replacement prevents that leak
by the same mechanism — one key, one subscription — while being useful rather
than merely safe.

It is not free, and it must not look free
-----------------------------------------
:meth:`StreamHub.subscribe` restarts the producer on purpose, so every OTHER
subscriber attached to that hub pays a fresh full core when this connection
re-baselines. Under the old refusal a redundant subscribe cost nothing at all —
the hub declined the duplicate key before it ever bumped a generation — so this
is a real new cost on the path a confused client takes repeatedly. (It is one
restart and not two: the teardown below stops a producer without starting one,
and the re-subscribe starts the single new generation any first subscribe would
have started. What changed is that a REDUNDANT subscribe went from free to
costing exactly what a first one costs.)

That is why a replacement is a RECEIPT rather than a silent success: the reply
carries ``replaced`` and a bound service log is told. A client in a retry loop
taxing the whole room is something an operator must be able to see in the log
and the client must be able to see in its own answer. The one shape this
program keeps refusing to ship is the cost nobody is billed for.

Giving a subscription back without dropping the connection
----------------------------------------------------------
:meth:`OfficeSubscriptions.release_one` is the other half. ``release`` sweeps a
DEPARTING connection and is the disconnect path's; a client that merely gave up
on a workspace, or navigated away from it, needs to hand one subscription back
while keeping the socket it is still using for everything else. Releasing
something already released answers False rather than raising, because that is
precisely what a recovering client does and an error there would make recovery
indistinguishable from a fault.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
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


@dataclass(frozen=True)
class SubscribeOutcome:
    """What a subscribe DID — not merely whether it worked.

    A bare ``bool`` was enough while the only two answers were "registered" and
    "refused". Re-baselining adds a third fact that the caller has to be able to
    put on the wire: whether this registration DISPLACED a live one, and
    therefore whether every other subscriber on the shared producer just paid a
    fresh full core for it. That is the receipt, and a receipt that the handler
    cannot see is not a receipt.

    ``reason`` is the second thing a bool could not carry, and it is not
    cosmetic either. ``StreamHub.subscribe`` answers False for two unrelated
    situations, and until now the handler guessed between them by asking
    ``bound()`` — which cannot tell "no hub at all" from "a bound factory whose
    hub is draining", and so told a client racing ``_close_socket_lane`` that it
    was ALREADY SUBSCRIBED. That was a lie in the one direction that matters: it
    points the client at a cure (stop retrying, you are already registered) for
    a state where the only cure is to reconnect. Removing the
    ``already_subscribed`` arm would otherwise have silently rehomed that
    mislabel onto ``push_lane_unavailable``, which is nearly as wrong — a
    permanent runtime fact where the truth is a transient one.

    ``__bool__`` is defined deliberately rather than left to the dataclass
    default. Every dataclass instance is truthy, so a caller who wrote the
    natural ``if not registered:`` against this type would get a branch that can
    never be taken — a refusal reported as a success, which is the exact failure
    shape this lane exists to delete.
    """

    registered: bool
    replaced: bool = False
    #: ``None`` when registered. Otherwise the typed reason the handler puts on
    #: the wire verbatim, because the registry is the only layer that can tell
    #: these two apart.
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.registered


#: No hub is bound at all: this runtime has no socket lane, or the serve loop
#: has not reached its bind yet. A fact about the runtime, not about the call.
NO_PUSH_LANE = "push_lane_unavailable"

#: A hub IS bound but refused the registration, which after re-baselining has
#: exactly one remaining cause: the hub is stopping (``StreamHub.subscribe``
#: returns False once its stop event is set). Transient by nature — the cure is
#: to reconnect and subscribe again, never to assume this runtime cannot push.
PUSH_LANE_DRAINING = "push_lane_draining"


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
        #: Where a re-baseline is BILLED. Optional because this registry is
        #: process-global and reachable from a runtime that has no service log
        #: (a test, a stdio probe); ``None`` means the receipt still reaches the
        #: client on its reply, just not an operator's tail.
        self._log: Callable[[dict[str, Any]], None] | None = None

    # ── wiring ──────────────────────────────────────────────────────────────

    def bind(
        self,
        hub_factory: Callable[[], Any] | None,
        *,
        log: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Point the lane at a hub factory, and optionally at a service log.

        ``log`` rides ``bind`` rather than being a constructor argument for the
        same reason the hub does: this object outlives any one serve loop, and
        the log it should write to belongs to whichever loop is currently
        running. Unbinding clears it, so a stopped loop's sink cannot be handed
        a line by the next one.
        """

        with self._lock:
            self._hub_factory = hub_factory
            self._log = log if hub_factory is not None else None
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
    ) -> SubscribeOutcome:
        """Register one subscription, REPLACING this connection's existing one.

        A falsy outcome — rather than a raise — is reserved for the two states a
        caller must answer differently from a crash, and ``reason`` is what
        tells them apart: no hub is bound at all (:data:`NO_PUSH_LANE`), or a
        bound hub refused because it is stopping (:data:`PUSH_LANE_DRAINING`).
        Neither is a fault, and their cures are opposites — give up on pushes,
        versus reconnect and ask again — which is why the registry names them
        here rather than leaving the handler to guess from ``bound()``.

        A duplicate key is no longer among those states. It used to be, and the
        module docstring above says why it stopped being: refusing left the
        client holding a subscription it had already decided not to fold
        against, with no way back short of dropping the connection.
        """

        with self._lock:
            factory = self._hub_factory
            log = self._log
        if factory is None:
            return SubscribeOutcome(False, reason=NO_PUSH_LANE)
        hub = factory()
        if hub is None:
            # A BOUND factory that answers None is still "no lane", not a
            # duplicate — the case the old ``bound()`` guess reported as
            # ``already_subscribed`` to a client that held no subscription.
            return SubscribeOutcome(False, reason=NO_PUSH_LANE)
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

        # RE-BASELINE: the old registration goes FIRST, and it has to. The hub
        # refuses a duplicate key, so there is no atomic swap available to us
        # and no ordering in which the new sink is installed before the old one
        # is gone. What makes that safe for this lane rather than a gap is the
        # caller: ``_runtime_office_subscribe`` holds ``office_lock`` across the
        # projection, the watermark and this call, so no office write can land
        # inside the window the teardown opens. A caller that dropped the lock
        # here would be re-introducing exactly the unreportable window that
        # made subscribe one call in the first place.
        try:
            replaced = bool(hub.unsubscribe(key))
        except Exception:
            # The hub's own accounting has the truth; a teardown that raised
            # must not turn a re-baseline into a handler error, because the
            # client asking for one is already trying to recover.
            replaced = False
        if not hub.subscribe(key, sink=sink, on_drop=_on_drop):
            # ONE cause remains now that a duplicate key is impossible here: the
            # hub is stopping (``StreamHub.subscribe`` refuses once its stop
            # event is set), i.e. this call raced ``_close_socket_lane``. It is
            # reported as its own transient reason rather than folded into
            # ``push_lane_unavailable``, because the cures differ — reconnect,
            # versus give up on pushes entirely.
            #
            # ``replaced`` is carried out with it. The teardown above ran, so a
            # client that DID hold a subscription no longer does, and telling it
            # only "refused" would leave it believing its old lane survived.
            # Tearing that lane down early costs nothing: ``StreamHub.stop``
            # releases every subscriber moments later anyway.
            #
            # The index must lose the key in the same breath — a key kept here
            # that the hub does not hold would make ``release`` report a
            # teardown it never performed.
            self._forget(connection_key, key)
            return SubscribeOutcome(False, replaced=replaced, reason=PUSH_LANE_DRAINING)
        with self._lock:
            self._owned.setdefault(str(connection_key or "stdio"), set()).add(key)
        if replaced and log is not None:
            try:
                log(
                    {
                        "event": "serve_office_subscription_rebaselined",
                        "connection": str(connection_key or "stdio"),
                        "workspace_id": workspace_id,
                        "key": key,
                        "baseline_offset": int(baseline_offset or 0),
                        # Named on the line rather than left to be inferred:
                        # this is the whole reason the line exists. A retry loop
                        # shows up here as a repeating cost, not as a mystery in
                        # the hub's generation counter.
                        "producer_restarted": True,
                    }
                )
            except Exception:
                # A logging sink is never allowed to fail a subscribe that has
                # already succeeded against the hub.
                pass
        return SubscribeOutcome(True, replaced=replaced)

    def _forget(self, connection_key: str | None, key: str) -> None:
        """Drop one key from the index only. Never touches the hub."""

        owner = str(connection_key or "stdio")
        with self._lock:
            owned = self._owned.get(owner)
            if owned is None:
                return
            owned.discard(key)
            if not owned:
                self._owned.pop(owner, None)

    def release_one(self, connection_key: str | None, workspace_id: str) -> bool:
        """Give ONE workspace's subscription back, keeping the connection.

        ``release`` is the disconnect sweep and takes everything a departing
        connection owns. This is the opposite situation: the client is still
        here and still talking, it has simply stopped caring about one
        workspace — it navigated away, or it refused a baseline it could not
        use. Without this the only way to stop a push lane was to drop the
        socket carrying every other call too.

        The answer is the HUB's, not the index's. A key sitting in ``_owned``
        that the hub no longer holds is a bookkeeping error, and reporting it as
        a successful release would hide the one leak this whole registry exists
        to make visible. False therefore means "nothing was live here" — an
        answer, not a fault, because a client recovering from a half-finished
        subscribe releases what it is not sure it holds.
        """

        key = office_subscription_key(connection_key, workspace_id)
        # Pruned FIRST and unconditionally: whatever the hub says, this
        # connection is no longer claiming the key, and an index that outlived
        # the claim would have the disconnect sweep chase a phantom.
        self._forget(connection_key, key)
        with self._lock:
            factory = self._hub_factory
        hub = factory() if factory is not None else None
        if hub is None:
            return False
        try:
            return bool(hub.unsubscribe(key))
        except Exception:
            return False

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
