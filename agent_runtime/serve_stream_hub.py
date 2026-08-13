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
* the buffer is bounded TWICE — by frame count and by bytes — and whichever
  trips first drops the subscriber. A count alone was not a bound: these frames
  are full hydrates, so 256 of them is a multi-gigabyte buffer per stalled
  client, which is a memory bound the runtime never agreed to. The drop reports
  WHICH bound tripped and both values, because "dropped for backpressure" with
  no numbers cannot distinguish a client that fell behind by 256 heartbeats
  from one that pinned a gigabyte;
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

Lifecycle (and the leak it used to have)
----------------------------------------

Every subscribe supersedes the running producer. That used to be ALL it did:
the old generator was noticed only when it next yielded a frame, nothing ever
stopped it when the room emptied, and ``stop()`` joined the newest producer
alone. Five subscribes therefore left five producer threads and five open
generators alive forever, rebuilding projections for ``subscriber_count: 0``.

So a producer now carries an explicit per-GENERATION stop event, checked at
every point between two frames; the hub tracks EVERY live producer, stops them
all when the last subscriber leaves, joins them all in ``stop()`` under one
shared deadline, and reports each one in ``stats()``.

The generator is still never closed from another thread — it is asked to stop
and observed to finish on its own next iteration, because a generator torn down
mid-frame from outside is how a producer leaves a half-written projection
behind, and CPython refuses ``close()`` on a generator that is executing
anyway. That leaves exactly one case the stop event cannot end promptly: a
source parked inside ``next()`` that never yields again. It is not silent — the
producer's state is reported as ``awaiting_frame`` with the age of its last
frame, so a hub with two producers where one has not yielded in minutes is
VISIBLE in ``stats()`` rather than being a thread nobody counted.

Root/scope is NOT this module's business: it takes a ``source_factory`` and
calls it on the producer thread. No environment reads, no path resolution.
"""

from __future__ import annotations

import inspect
import json
import queue
import threading
import time
from typing import Any, Callable, Iterator

__all__ = [
    "BOUND_BYTES",
    "BOUND_FRAMES",
    "DEFAULT_BUFFER_LIMIT",
    "DEFAULT_BYTE_LIMIT",
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

#: The OTHER bound, and the one that actually bounds memory. A stream frame is
#: not a fixed cost — the hydrate at the head of every generation is a complete
#: projection — so a count-only limit let one stalled subscriber pin
#: 256 × (whatever a hydrate weighs). 32 MiB is generous for a client that is
#: merely bursty and decisive against one that has stopped reading.
DEFAULT_BYTE_LIMIT = 32 * 1024 * 1024

#: Which bound tripped, named on the drop so the number is actionable.
BOUND_FRAMES = "frames"
BOUND_BYTES = "bytes"

DROP_REASON_BACKPRESSURE = "backpressure"
DROP_REASON_PRODUCER_ENDED = "producer_ended"
DROP_REASON_PRODUCER_ERROR = "producer_error"

#: Charged to a frame whose size could not be measured. Never zero: an
#: unmeasurable frame that costs nothing is how a byte bound gets bypassed by
#: the exact payloads it exists to bound.
_UNMEASURED_FRAME_BYTES = 4096

#: Wakes a pump thread that must terminate (unsubscribe, drop, or hub stop).
_WAKE = object()


def _accepts_stop_argument(factory: Callable[..., Any]) -> bool:
    """True when *factory* can be called with one positional argument."""

    try:
        inspect.signature(factory).bind(threading.Event())
    except (TypeError, ValueError):
        return False
    return True


def _frame_bytes(frame: Any) -> int:
    """The wire cost of one frame, measured once per frame by the PRODUCER.

    Measured, not estimated: the bound is only a bound if the number is real,
    and an estimate that under-counts the big frames under-counts exactly the
    frames the byte limit exists to catch. Once per frame in the producer
    rather than once per subscriber in ``offer`` — the fan-out is the reason
    this class exists, so the accounting must not scale with it.
    """

    try:
        return len(json.dumps(frame, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return _UNMEASURED_FRAME_BYTES


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
        "_drop_bound",
        "_closed",
        "_buffer_limit",
        "_byte_limit",
        "_buffered_bytes",
        "frames_offered",
        "frames_delivered",
        "frames_discarded",
        "bytes_discarded",
    )

    def __init__(
        self,
        key: str,
        *,
        sink: Callable[[dict[str, Any]], None],
        on_drop: Callable[[str, dict[str, Any]], None] | None,
        buffer_limit: int,
        byte_limit: int = DEFAULT_BYTE_LIMIT,
    ) -> None:
        self.key = key
        self._sink = sink
        self._on_drop = on_drop
        self._buffer_limit = max(1, int(buffer_limit))
        self._byte_limit = max(1, int(byte_limit))
        self._queue: queue.Queue = queue.Queue(maxsize=self._buffer_limit)
        self._lock = threading.Lock()
        self._drop_reason: str | None = None
        #: Which bound tripped (``frames`` / ``bytes``), or None.
        self._drop_bound: str | None = None
        self._closed = False
        self._buffered_bytes = 0
        self.frames_offered = 0
        self.frames_delivered = 0
        self.frames_discarded = 0
        self.bytes_discarded = 0
        self._thread = threading.Thread(
            target=self._pump, name=f"serve-stream-sub-{key}", daemon=True
        )
        self._thread.start()

    # ── producer side (never blocks) ────────────────────────────────────────

    def offer(self, frame: dict[str, Any], frame_bytes: int = 0) -> bool:
        """Queue *frame*. False means this subscriber has just been dropped.

        Called from the producer thread ONLY. It never blocks, never calls the
        sink, and never waits on a lock the sink holds.

        Two bounds, and whichever trips first wins: the queue's own frame count,
        and the running byte total (measured once per frame by the producer and
        passed in, so the fan-out does not pay for the accounting N times).
        """

        size = max(0, int(frame_bytes))
        with self._lock:
            if self._closed or self._drop_reason is not None:
                return False
            over_bytes = (self._buffered_bytes + size) > self._byte_limit
            if not over_bytes:
                self.frames_offered += 1
                self._buffered_bytes += size
        if over_bytes:
            self.mark_dropped(DROP_REASON_BACKPRESSURE, discard=True, bound=BOUND_BYTES)
            return False
        try:
            self._queue.put_nowait((frame, size))
            return True
        except queue.Full:
            with self._lock:
                self._buffered_bytes = max(0, self._buffered_bytes - size)
            self.mark_dropped(
                DROP_REASON_BACKPRESSURE, discard=True, bound=BOUND_FRAMES
            )
            return False

    def mark_dropped(
        self, reason: str, *, discard: bool = False, bound: str | None = None
    ) -> None:
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
            self._drop_bound = bound
        discarded = 0
        discarded_bytes = 0
        if discard:
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                discarded += 1
                discarded_bytes += _item_bytes(item)
        try:
            self._queue.put_nowait(_WAKE)
        except queue.Full:
            # Keeping the tail costs one frame at the head: without room for
            # the sentinel the pump would never learn why it stopped.
            try:
                item = self._queue.get_nowait()
                discarded += 1
                discarded_bytes += _item_bytes(item)
                self._queue.put_nowait(_WAKE)
            except (queue.Empty, queue.Full):  # pragma: no cover - defensive
                pass
        if discarded:
            with self._lock:
                self.frames_discarded += discarded
                self.bytes_discarded += discarded_bytes
                self._buffered_bytes = max(0, self._buffered_bytes - discarded_bytes)

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
            frame, size = item
            with self._lock:
                self._buffered_bytes = max(0, self._buffered_bytes - size)
            try:
                self._sink(frame)
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
                "bytes_discarded": self.bytes_discarded,
                "dropped": self._drop_reason is not None,
                "drop_reason": self._drop_reason,
                # WHICH bound tripped, and both of them, so a drop is a fact an
                # operator can act on instead of an adjective.
                "drop_bound": self._drop_bound,
                "buffered": self._queue.qsize(),
                "buffered_bytes": self._buffered_bytes,
                "frame_limit": self._buffer_limit,
                "byte_limit": self._byte_limit,
            }

    @property
    def dropped(self) -> bool:
        with self._lock:
            return self._drop_reason is not None


def _item_bytes(item: Any) -> int:
    """The measured size carried alongside a queued frame (0 for a sentinel)."""

    if isinstance(item, tuple) and len(item) == 2:
        try:
            return max(0, int(item[1]))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return 0
    return 0


class _ProducerHandle:
    """One generation of the shared producer, and everything that can stop it.

    The stop event is per GENERATION rather than per hub: superseding a
    producer and shutting the hub down are different facts, and a single flag
    could not express "this one is finished, the lane is not".
    """

    __slots__ = (
        "generation",
        "stop",
        "finished",
        "thread",
        "started_monotonic",
        "last_frame_monotonic",
        "frames",
        "state",
        "_lock",
    )

    def __init__(self, generation: int) -> None:
        self.generation = generation
        self.stop = threading.Event()
        self.finished = threading.Event()
        self.thread: threading.Thread | None = None
        self.started_monotonic = time.monotonic()
        self.last_frame_monotonic = self.started_monotonic
        self.frames = 0
        #: ``starting`` | ``awaiting_frame`` | ``fanning_out`` | ``closing`` |
        #: ``finished``. ``awaiting_frame`` is the load-bearing one: a producer
        #: parked in ``next()`` cannot be interrupted from another thread, so
        #: the honest answer is to REPORT it — a stale generation sitting in
        #: ``awaiting_frame`` with a minutes-old last frame is the leak,
        #: visible.
        self.state = "starting"
        self._lock = threading.Lock()

    def note_frame(self) -> None:
        with self._lock:
            self.frames += 1
            self.last_frame_monotonic = time.monotonic()

    def stats(self, *, current_generation: int) -> dict[str, Any]:
        thread = self.thread
        with self._lock:
            frames = self.frames
            idle_ms = int((time.monotonic() - self.last_frame_monotonic) * 1000)
            age_ms = int((time.monotonic() - self.started_monotonic) * 1000)
        return {
            "generation": self.generation,
            "state": self.state,
            "alive": bool(thread is not None and thread.is_alive()),
            "superseded": self.generation != current_generation,
            "stopping": self.stop.is_set(),
            "frames": frames,
            "idle_ms": idle_ms,
            "age_ms": age_ms,
        }


class StreamHub:
    """The shared producer and its subscriber table."""

    def __init__(
        self,
        source_factory: Callable[..., Iterator[dict[str, Any]]],
        *,
        buffer_limit: int = DEFAULT_BUFFER_LIMIT,
        byte_limit: int = DEFAULT_BYTE_LIMIT,
        log: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._source_factory = source_factory
        #: Does this factory want the per-generation stop event? Answered ONCE,
        #: by signature, never by calling it and catching ``TypeError`` — that
        #: would swallow a TypeError raised inside a factory that simply takes
        #: no argument, and retry it with a different arity.
        #:
        #: A source that accepts it can abandon its own park, which is the only
        #: way an abandoned generation stops before its next frame: a generator
        #: blocked inside ``next()`` cannot be interrupted from another thread,
        #: and closing it from outside is both refused by CPython (it is
        #: executing) and wrong (a half-written projection). The real stream
        #: source polls at a quarter second and heartbeats at five, so it is
        #: never parked for long; a source that CAN wait on this stops at once.
        self._factory_accepts_stop = _accepts_stop_argument(source_factory)
        self._buffer_limit = int(buffer_limit)
        self._byte_limit = int(byte_limit)
        self._log = log
        self._subscriptions: dict[str, StreamSubscription] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        #: EVERY producer that has not yet finished, by generation — not just
        #: the newest. Tracking only the newest is what let superseded ones
        #: accumulate unstopped, unjoined, and uncounted.
        self._producers: dict[int, _ProducerHandle] = {}
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
                key,
                sink=sink,
                on_drop=on_drop,
                buffer_limit=self._buffer_limit,
                byte_limit=self._byte_limit,
            )
            self._generation += 1
            generation = self._generation
            # Every older producer is told to stop HERE, explicitly, rather
            # than being left to notice on a frame that may never come.
            superseded = [
                handle
                for gen, handle in self._producers.items()
                if gen != generation
            ]
            handle = _ProducerHandle(generation)
            producer = threading.Thread(
                target=self._produce,
                args=(handle,),
                name=f"serve-stream-producer-{generation}",
                daemon=True,
            )
            handle.thread = producer
            self._producers[generation] = handle
        for stale in superseded:
            stale.stop.set()
        producer.start()
        return True

    def unsubscribe(self, key: str) -> bool:
        with self._lock:
            subscription = self._subscriptions.pop(key, None)
            room_empty = not self._subscriptions
            producers = list(self._producers.values()) if room_empty else []
        if subscription is None:
            return False
        subscription.close()
        # The room is empty: stop producing. The producer's own in-loop check
        # only fires when a frame arrives, so a source between frames would
        # have kept a projection lane alive for nobody.
        for handle in producers:
            handle.stop.set()
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
            generation = self._generation
            handles = list(self._producers.values())
        rows = [handle.stats(current_generation=generation) for handle in handles]
        live = [row for row in rows if row["alive"]]
        return {
            "subscribers": len(subscriptions),
            "frames_produced": produced,
            # Kept as it was — the CURRENT generation's producer — so existing
            # consumers read the same field. The list beside it is the new
            # truth: a leak is a number here, not an absence.
            "producer_running": any(
                row["alive"] and not row["superseded"] for row in rows
            ),
            "producers_live": len(live),
            "producers_superseded": len(
                [row for row in live if row["superseded"]]
            ),
            "producers": rows,
            "generation": generation,
            "producer_error": error,
            "buffer_limit": self._buffer_limit,
            "byte_limit": self._byte_limit,
            "subscriptions": [item.stats() for item in subscriptions],
        }

    def stop(self, *, join_timeout: float = 2.0) -> None:
        """Stop EVERY producer, release every subscriber. Never raises.

        ``join_timeout`` is one TOTAL deadline for the whole teardown, not a
        budget per join. Summed budgets are how a shutdown path with 32
        subscribers and a couple of stale producers turned a two-second
        intention into a minute — which is the whole reason the drain's exit
        watchdog now has to be armed before this is called.
        """

        self._stop.set()
        with self._lock:
            subscriptions = list(self._subscriptions.values())
            self._subscriptions.clear()
            producers = list(self._producers.values())
        for handle in producers:
            handle.stop.set()
        for subscription in subscriptions:
            subscription.close()
        deadline = time.monotonic() + max(0.0, float(join_timeout))
        for subscription in subscriptions:
            subscription.join(max(0.0, deadline - time.monotonic()))
        for handle in producers:
            thread = handle.thread
            if thread is not None and thread.is_alive():
                thread.join(max(0.0, deadline - time.monotonic()))

    # ── producer ────────────────────────────────────────────────────────────

    def _produce(self, handle: _ProducerHandle) -> None:
        source: Iterator[dict[str, Any]] | None = None
        reason = DROP_REASON_PRODUCER_ENDED
        superseded = False
        generation = handle.generation
        try:
            source = (
                self._source_factory(handle.stop)
                if self._factory_accepts_stop
                else self._source_factory()
            )
            iterator = iter(source)
            while True:
                # Checked BEFORE every pull and again after it: those are the
                # two points where a producer can be abandoned without tearing
                # a generator down from another thread. The pull itself is the
                # one uninterruptible step, and it is the one ``stats()``
                # reports as ``awaiting_frame``.
                if self._should_stop(handle):
                    superseded = True
                    return
                handle.state = "awaiting_frame"
                try:
                    frame = next(iterator)
                except StopIteration:
                    break
                handle.note_frame()
                handle.state = "fanning_out"
                if self._should_stop(handle):
                    superseded = True
                    return
                # Measured once, here, and handed to every subscriber: the
                # byte bound must not cost the fan-out one serialization per
                # client on top of the one the sink already does.
                size = _frame_bytes(frame)
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
                        subscription.offer(frame, size)
        except BaseException as exc:  # noqa: BLE001 - reported, never raised out
            reason = f"{DROP_REASON_PRODUCER_ERROR}:{type(exc).__name__}"
            with self._lock:
                self._producer_error = reason
            self._emit_log(
                {"event": "serve_stream_producer_error", "reason": reason}
            )
            self._fail_all(reason)
        finally:
            handle.state = "closing"
            if source is not None:
                close = getattr(source, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # pragma: no cover - defensive
                        pass
            with self._lock:
                self._producers.pop(generation, None)
            handle.state = "finished"
            handle.finished.set()
            if (
                not superseded
                and reason == DROP_REASON_PRODUCER_ENDED
                and not self._stop.is_set()
                and not handle.stop.is_set()
                and self.subscriber_count()
            ):
                # The generator finished on its own (``max_frames``, a cancelled
                # request scope). Subscribers are owed the reason: a stream that
                # simply went quiet is indistinguishable from a wedged one.
                self._fail_all(DROP_REASON_PRODUCER_ENDED)

    def _should_stop(self, handle: _ProducerHandle) -> bool:
        """Three ways one generation ends: itself, the hub, or a newer one."""

        if handle.stop.is_set() or self._stop.is_set():
            return True
        with self._lock:
            return handle.generation != self._generation

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
