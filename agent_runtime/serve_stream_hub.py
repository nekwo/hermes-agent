"""One stream producer, N subscribers — the fan-out behind ``{"op":"subscribe"}``.

Why one producer
----------------

``agent_runtime.stream.stream_frames`` is not a cheap tail. Every delta batch
rebuilds a full snapshot core (W1 coalescing made that per-BATCH instead of per
event, which was the whole point of that work), and the hydrate at the head of
the stream is a complete projection. Running one generator per connected client
would multiply that cost by the number of clients — which is exactly the number
a durable multi-client service exists to make large. So the hub runs ONE
generator and hands each frame to every subscriber.

Slow consumers, explicitly
--------------------------

A shared producer inherits one hazard the per-client model did not have: a
single slow reader can stall everybody. The rule here is that it cannot, and
that nothing about it is silent:

* each subscriber owns a BOUNDED buffer and its own pump thread, so a socket
  write that blocks blocks only that subscriber;
* when a subscriber's buffer overflows, it is marked dropped with reason
  ``backpressure``, its buffered frames are discarded, and its pump is woken to
  deliver ONE typed ``subscription_dropped`` notification and unsubscribe;
* the producer never calls a subscriber's sink, and never blocks on one. It
  offers a frame and moves on.

A dropped subscriber is a client that must resubscribe (and re-hydrate). That
is a worse outcome than keeping up, and a far better one than the alternatives:
stalling the producer (every other client goes stale), or silently discarding
frames (the client believes it is current — the false-all-clear class this
whole workstream exists to retire).

Every subscriber starts at a hydrate
-----------------------------------

``stream_frames`` opens with a hydrate — a complete projection — and everything
after it is a delta applied to that baseline. A client that joined a producer
already in flight would therefore receive deltas with NOTHING to fold them
onto, and would render a runtime assembled from whatever happened to change
after it connected. That is a silent wrong answer, which is the failure class
this workstream exists to retire.

So a subscribe RE-BASELINES the lane: the producer is restarted, the new
generator opens with a fresh hydrate, and every attached subscriber receives it.
Existing subscribers pay one redundant full core — precisely what the stream's
own ``--resync`` lane already does, and what their fold already handles — and
in exchange no subscriber can ever hold a baseline it did not receive. Frames
carry a generation internally so a superseded producer can never interleave an
older frame after the new hydrate; joins are per client CONNECTION, so the cost
is bounded by connection churn, not by traffic.

Lifecycle
---------

The producer starts on the FIRST subscribe and stops when the last subscriber
leaves, so a service nobody is watching pays nothing. The generator is never
closed from another thread — it is asked to stop and observed to finish on its
own next iteration, because a generator torn down mid-frame from outside is how
a producer leaves a half-written projection behind.

Root/scope is NOT this module's business: it takes a ``source_factory`` and
calls it on the producer thread. No environment reads, no path resolution.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Iterator

__all__ = [
    "DEFAULT_BUFFER_LIMIT",
    "DROP_REASON_BACKPRESSURE",
    "DROP_REASON_PRODUCER_ENDED",
    "DROP_REASON_PRODUCER_ERROR",
    "StreamHub",
    "StreamSubscription",
]

#: Frames a subscriber may fall behind by before it is dropped. Sized for a
#: burst (a delta batch storm during a chat turn), not for an absent reader: a
#: client that is 256 frames behind is not slow, it is gone.
DEFAULT_BUFFER_LIMIT = 256

DROP_REASON_BACKPRESSURE = "backpressure"
DROP_REASON_PRODUCER_ENDED = "producer_ended"
DROP_REASON_PRODUCER_ERROR = "producer_error"

#: Wakes a pump thread that must terminate (unsubscribe, drop, or hub stop).
_WAKE = object()


class StreamSubscription:
    """One client's slot on the shared stream: a bounded buffer and a pump."""

    __slots__ = (
        "key",
        "_sink",
        "_on_drop",
        "_queue",
        "_thread",
        "_lock",
        "_drop_reason",
        "_closed",
        "frames_offered",
        "frames_delivered",
        "frames_discarded",
    )

    def __init__(
        self,
        key: str,
        *,
        sink: Callable[[dict[str, Any]], None],
        on_drop: Callable[[str, dict[str, Any]], None] | None,
        buffer_limit: int,
    ) -> None:
        self.key = key
        self._sink = sink
        self._on_drop = on_drop
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, int(buffer_limit)))
        self._lock = threading.Lock()
        self._drop_reason: str | None = None
        self._closed = False
        self.frames_offered = 0
        self.frames_delivered = 0
        self.frames_discarded = 0
        self._thread = threading.Thread(
            target=self._pump, name=f"serve-stream-sub-{key}", daemon=True
        )
        self._thread.start()

    # ── producer side (never blocks) ────────────────────────────────────────

    def offer(self, frame: dict[str, Any]) -> bool:
        """Queue *frame*. False means this subscriber has just been dropped.

        Called from the producer thread ONLY. It never blocks, never calls the
        sink, and never waits on a lock the sink holds.
        """

        with self._lock:
            if self._closed or self._drop_reason is not None:
                return False
            self.frames_offered += 1
        try:
            self._queue.put_nowait(frame)
            return True
        except queue.Full:
            self.mark_dropped(DROP_REASON_BACKPRESSURE, discard=True)
            return False

    def mark_dropped(self, reason: str, *, discard: bool = False) -> None:
        """Record the drop and wake the pump to deliver the notification.

        ``discard`` is the BACKPRESSURE case: that subscriber is leaving with a
        queue full of frames it will never read, and the space is needed to
        carry the wakeup — a full queue would deadlock the very notification
        that explains the drop.

        It is deliberately FALSE for the producer-ended / producer-error cases.
        Those subscribers are keeping up; they are owed the frames already
        queued for them, and only then the reason the stream stopped. Discarding
        there would silently swallow the tail of a healthy client's stream.
        """

        with self._lock:
            if self._closed or self._drop_reason is not None:
                return
            self._drop_reason = str(reason)
        discarded = 0
        if discard:
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
                discarded += 1
        try:
            self._queue.put_nowait(_WAKE)
        except queue.Full:
            # Keeping the tail costs one frame at the head: without room for
            # the sentinel the pump would never learn why it stopped.
            try:
                self._queue.get_nowait()
                discarded += 1
                self._queue.put_nowait(_WAKE)
            except (queue.Empty, queue.Full):  # pragma: no cover - defensive
                pass
        if discarded:
            with self._lock:
                self.frames_discarded += discarded

    def close(self) -> None:
        """Stop this subscription without a drop notification (clean leave)."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._queue.put_nowait(_WAKE)
        except queue.Full:
            # Full of frames this subscriber will never read; make room for the
            # wakeup rather than leave the pump parked forever.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(_WAKE)
            except (queue.Empty, queue.Full):  # pragma: no cover - defensive
                pass

    def join(self, timeout: float) -> None:
        self._thread.join(timeout)

    # ── consumer side (owns the sink; may block on it) ──────────────────────

    def _pump(self) -> None:
        while True:
            item = self._queue.get()
            if item is _WAKE:
                reason = self._drop_reason
                if reason is not None and not self._closed:
                    self._notify_drop(reason)
                return
            try:
                self._sink(item)
            except Exception as exc:
                # A sink that raised is a connection that is gone or wedged.
                # Its drop is recorded with the exception TYPE, never the
                # message: sink errors carry socket/OS text that is noise at
                # best and client-controlled at worst.
                with self._lock:
                    if self._drop_reason is None:
                        self._drop_reason = f"sink_error:{type(exc).__name__}"
                    reason = self._drop_reason
                if not self._closed:
                    self._notify_drop(reason)
                return
            with self._lock:
                self.frames_delivered += 1
            # A drop decided by the producer WHILE this sink call was running
            # lands here on the next lap; the wakeup is already queued.

    def _notify_drop(self, reason: str) -> None:
        if self._on_drop is None:
            return
        try:
            self._on_drop(reason, self.stats())
        except Exception:
            # The notification is best effort by construction: it is delivered
            # over the same transport that just failed for half the reasons a
            # drop happens at all.
            pass

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "key": self.key,
                "frames_offered": self.frames_offered,
                "frames_delivered": self.frames_delivered,
                "frames_discarded": self.frames_discarded,
                "dropped": self._drop_reason is not None,
                "drop_reason": self._drop_reason,
                "buffered": self._queue.qsize(),
            }

    @property
    def dropped(self) -> bool:
        with self._lock:
            return self._drop_reason is not None


class StreamHub:
    """The shared producer and its subscriber table."""

    def __init__(
        self,
        source_factory: Callable[[], Iterator[dict[str, Any]]],
        *,
        buffer_limit: int = DEFAULT_BUFFER_LIMIT,
        log: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._source_factory = source_factory
        self._buffer_limit = int(buffer_limit)
        self._log = log
        self._subscriptions: dict[str, StreamSubscription] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._producer: threading.Thread | None = None
        self._frames_produced = 0
        self._producer_error: str | None = None
        #: Bumped by every subscribe. A producer whose generation is no longer
        #: current stops offering and exits — which is what keeps a superseded
        #: generator from interleaving a stale frame after the new hydrate.
        self._generation = 0

    # ── subscription table ──────────────────────────────────────────────────

    def subscribe(
        self,
        key: str,
        *,
        sink: Callable[[dict[str, Any]], None],
        on_drop: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> bool:
        """Add *key*. False when it is already subscribed (idempotent, typed).

        Restarts the producer so this subscriber's first frame is a hydrate —
        see the module docstring. The restart is what makes a late join safe,
        not an optimisation to remove.
        """

        with self._lock:
            if self._stop.is_set():
                return False
            if key in self._subscriptions:
                return False
            self._subscriptions[key] = StreamSubscription(
                key, sink=sink, on_drop=on_drop, buffer_limit=self._buffer_limit
            )
            self._generation += 1
            generation = self._generation
            producer = threading.Thread(
                target=self._produce,
                args=(generation,),
                name=f"serve-stream-producer-{generation}",
                daemon=True,
            )
            self._producer = producer
        producer.start()
        return True

    def unsubscribe(self, key: str) -> bool:
        with self._lock:
            subscription = self._subscriptions.pop(key, None)
        if subscription is None:
            return False
        subscription.close()
        return True

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._subscriptions

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscriptions)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            subscriptions = list(self._subscriptions.values())
            produced = self._frames_produced
            error = self._producer_error
            running = bool(self._producer is not None and self._producer.is_alive())
        return {
            "subscribers": len(subscriptions),
            "frames_produced": produced,
            "producer_running": running,
            "producer_error": error,
            "buffer_limit": self._buffer_limit,
            "subscriptions": [item.stats() for item in subscriptions],
        }

    def stop(self, *, join_timeout: float = 2.0) -> None:
        """Stop the producer and release every subscriber. Never raises."""

        self._stop.set()
        with self._lock:
            subscriptions = list(self._subscriptions.values())
            self._subscriptions.clear()
            producer = self._producer
        for subscription in subscriptions:
            subscription.close()
        for subscription in subscriptions:
            subscription.join(join_timeout)
        if producer is not None and producer.is_alive():
            producer.join(join_timeout)

    # ── producer ────────────────────────────────────────────────────────────

    def _produce(self, generation: int) -> None:
        source: Iterator[dict[str, Any]] | None = None
        reason = DROP_REASON_PRODUCER_ENDED
        superseded = False
        try:
            source = self._source_factory()
            for frame in source:
                if self._stop.is_set():
                    superseded = True
                    return
                # Offered INSIDE the lock. The offer is non-blocking by
                # construction (a bounded queue and a drop decision, never a
                # sink call), and holding the lock across it is what gives the
                # lane a total order: a frame from a superseded generation can
                # never land after the newer generation's hydrate.
                with self._lock:
                    if generation != self._generation:
                        superseded = True
                        return
                    subscriptions = list(self._subscriptions.values())
                    if not subscriptions:
                        # Nobody is listening any more: a durable service must
                        # not keep rebuilding projections for an empty room.
                        superseded = True
                        return
                    self._frames_produced += 1
                    for subscription in subscriptions:
                        subscription.offer(frame)
        except BaseException as exc:  # noqa: BLE001 - reported, never raised out
            reason = f"{DROP_REASON_PRODUCER_ERROR}:{type(exc).__name__}"
            with self._lock:
                self._producer_error = reason
            self._emit_log(
                {"event": "serve_stream_producer_error", "reason": reason}
            )
            self._fail_all(reason)
        finally:
            if source is not None:
                close = getattr(source, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # pragma: no cover - defensive
                        pass
            if (
                not superseded
                and reason == DROP_REASON_PRODUCER_ENDED
                and not self._stop.is_set()
                and self.subscriber_count()
            ):
                # The generator finished on its own (``max_frames``, a cancelled
                # request scope). Subscribers are owed the reason: a stream that
                # simply went quiet is indistinguishable from a wedged one.
                self._fail_all(DROP_REASON_PRODUCER_ENDED)

    def _fail_all(self, reason: str) -> None:
        with self._lock:
            subscriptions = list(self._subscriptions.values())
        for subscription in subscriptions:
            subscription.mark_dropped(reason)

    def _emit_log(self, payload: dict[str, Any]) -> None:
        if self._log is None:
            return
        try:
            self._log(payload)
        except Exception:  # pragma: no cover - an instrument may not raise
            pass
