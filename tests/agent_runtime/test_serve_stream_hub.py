"""The subscription fan-out: one producer, N subscribers, and a slow-consumer
policy that is explicit, typed, and unable to hurt anybody else.

The hazard this file exists to pin is structural, not incidental. A shared
producer means one client's socket can stall every other client's view of the
runtime — the exact failure the durable multi-client service is supposed to
make impossible. So the tests below do not merely observe that a
``subscription_dropped`` notification appeared: they prove the FAST subscriber
kept receiving while the slow one was stuck, and that the producer never
blocked on either.
"""

from __future__ import annotations

import threading
import time

import pytest

from agent_runtime.serve_stream_hub import (
    BOUND_BYTES,
    BOUND_FRAMES,
    DEFAULT_BYTE_LIMIT,
    DROP_REASON_BACKPRESSURE,
    DROP_REASON_PRODUCER_ENDED,
    StreamHub,
)


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class _Recorder:
    """A sink that records what it was handed, and can be made to block."""

    def __init__(self, gate: threading.Event | None = None):
        self.frames: list[dict] = []
        self.gate = gate
        self._lock = threading.Lock()

    def __call__(self, frame: dict) -> None:
        if self.gate is not None:
            # Blocks the SUBSCRIBER's pump thread — the socket-write stall this
            # whole policy exists to survive.
            self.gate.wait(30.0)
        with self._lock:
            self.frames.append(frame)

    def count(self) -> int:
        with self._lock:
            return len(self.frames)


def _counting_source(total: int, *, gate: threading.Event | None = None):
    """A producer of *total* frames.

    ``gate`` holds it open afterwards the way the real stream stays open — by
    POLLING (a heartbeat lap), not by parking on an event. That distinction is
    load-bearing for these tests: ``stream_frames`` notices a stop request
    between frames, so a fake that blocked inside the generator would make a
    superseded or unsubscribed producer look like it never exits.
    """

    def _factory():
        def _generate():
            for index in range(total):
                yield {"type": "delta", "index": index}
            while gate is not None and not gate.is_set():
                time.sleep(0.005)
                yield {"type": "heartbeat", "index": -1}

        return _generate()

    return _factory


# ── the point of the whole module: ONE producer ─────────────────────────────


def _generational_source(total: int, gate: threading.Event):
    """A factory whose every invocation opens with its own ``hydrate``.

    Mirrors ``stream_frames``: frame 0 of any generator is the full baseline,
    and everything after it is a delta that only means something relative to it.
    """

    generations = {"count": 0}
    alive: set[int] = set()
    alive_lock = threading.Lock()

    def _factory():
        generations["count"] += 1
        generation = generations["count"]

        def _generate():
            with alive_lock:
                alive.add(generation)
            try:
                yield {"type": "hydrate", "generation": generation, "index": 0}
                for index in range(1, total):
                    yield {"type": "delta", "generation": generation, "index": index}
                while not gate.is_set():
                    time.sleep(0.005)
                    yield {"type": "heartbeat", "generation": generation, "index": -1}
            finally:
                with alive_lock:
                    alive.discard(generation)

        return _generate()

    def _alive() -> set[int]:
        with alive_lock:
            return set(alive)

    return _factory, generations, _alive


def test_a_frame_is_produced_once_and_delivered_to_every_subscriber():
    """The reason there is a hub at all: N clients, ONE projection build."""

    gate = threading.Event()
    factory, generations, alive = _generational_source(30, gate)
    hub = StreamHub(factory, buffer_limit=64)
    first, second = _Recorder(), _Recorder()
    try:
        hub.subscribe("a", sink=first)
        hub.subscribe("b", sink=second)
        live = generations["count"]
        assert _wait_until(
            lambda: len([f for f in first.frames if f["generation"] == live]) >= 30
            and len([f for f in second.frames if f["generation"] == live]) >= 30
        )
        first_live = [f for f in first.frames if f["generation"] == live]
        second_live = [f for f in second.frames if f["generation"] == live]
        # Identical sequences...
        assert [f["index"] for f in first_live[:30]] == list(range(30))
        assert [f["index"] for f in second_live[:30]] == list(range(30))
        # ...from ONE generator serving both, not one generator per client.
        # That is the entire reason this class exists: a delta batch rebuilds a
        # full snapshot core, so a per-client generator would multiply the
        # dominant cost of the stream by the number of attached clients.
        assert _wait_until(lambda: alive() == {live}, timeout=5.0)
    finally:
        gate.set()
        hub.stop()


def test_every_subscriber_starts_at_a_hydrate_even_when_it_joins_late():
    """A late joiner must never fold deltas onto a baseline it never received.

    The lane re-baselines on subscribe: the newcomer's FIRST frame is a
    hydrate, and the already-attached subscriber receives that same fresh
    hydrate rather than being left on a generation the newcomer cannot see.
    """

    gate = threading.Event()
    factory, _generations, _alive = _generational_source(6, gate)
    hub = StreamHub(factory, buffer_limit=64)
    early, late = _Recorder(), _Recorder()
    try:
        hub.subscribe("early", sink=early)
        assert _wait_until(lambda: early.count() >= 6)
        assert early.frames[0]["type"] == "hydrate"
        assert {f["generation"] for f in early.frames} == {1}

        hub.subscribe("late", sink=late)
        assert _wait_until(lambda: late.count() >= 6)

        # The newcomer opened on a hydrate, not on a mid-stream delta.
        assert late.frames[0]["type"] == "hydrate"
        assert late.frames[0]["generation"] == 2
        assert [f["index"] for f in late.frames[:6]] == list(range(6))

        # And the incumbent was re-baselined onto the same generation rather
        # than left folding a stream the newcomer is not on.
        rebaselined = [f for f in early.frames if f["generation"] == 2]
        assert rebaselined and rebaselined[0]["type"] == "hydrate"

        # No frame of the superseded generation arrived AFTER the new hydrate:
        # a stale delta landing past a re-baseline would corrupt the fold.
        generations_seen = [f["generation"] for f in early.frames]
        assert generations_seen == sorted(generations_seen)
    finally:
        gate.set()
        hub.stop()


def test_a_restart_free_join_attaches_to_the_running_generation(
):
    """``restart_producer=False``: the join that does not bill the room a core.

    The restart exists so a late joiner's first frame is a hydrate, and it stays
    the DEFAULT for that reason. But the office push lane's baseline rides its
    own ``runtime.office.subscribe`` reply and its sink then provably discards
    the hydrate at that baseline — so every one of its joins was manufacturing a
    full core to throw away and superseding the running producer to do it.

    What is pinned here is both halves of "attaches": the generation does not
    move (bumping it is what supersedes the live producer, so a restart-free
    join that bumped would stop the very lane it meant to keep), and the newcomer
    genuinely receives the RUNNING generation's frames rather than nothing.
    """

    gate = threading.Event()
    factory, generations, _alive = _generational_source(6, gate)
    hub = StreamHub(factory, buffer_limit=64)
    early, late = _Recorder(), _Recorder()
    try:
        hub.subscribe("early", sink=early)
        assert _wait_until(lambda: early.count() >= 3)
        assert hub.stats()["generation"] == 1

        assert hub.subscribe("late", sink=late, restart_producer=False) is True

        # No supersession: one generation, one generator body ever built.
        assert hub.stats()["generation"] == 1
        assert generations["count"] == 1
        # And the newcomer is actually being served by it — a join that attached
        # to nothing would satisfy the counters above and receive silence, which
        # is the failure mode this lane has already paid for once.
        assert _wait_until(lambda: late.count() >= 1)
        assert {frame["generation"] for frame in late.frames} == {1}
        # The incumbent was NOT re-baselined: no second hydrate reached it.
        assert [f["type"] for f in early.frames].count("hydrate") == 1
    finally:
        gate.set()
        hub.stop()


def test_a_restart_free_join_still_starts_a_producer_when_none_is_running():
    """The FLOOR, and it lives in the hub rather than in its callers.

    A subscriber attached to nothing receives nothing — silently, forever. The
    caller's non-narrowing check is about whether a restart is NECESSARY; it
    cannot be allowed to answer whether a producer EXISTS, because that state
    changes underneath it (a re-baseline's own teardown empties the room and
    stops the producer between the check and the join).

    Both entry points are pinned: the very first subscriber, and a join that
    arrives after the room has emptied.
    """

    gate = threading.Event()
    factory, generations, _alive = _generational_source(4, gate)
    hub = StreamHub(factory, buffer_limit=64)
    first, second = _Recorder(), _Recorder()
    try:
        # No producer at all yet.
        assert hub.subscribe("first", sink=first, restart_producer=False) is True
        assert hub.stats()["generation"] == 1
        assert _wait_until(lambda: first.count() >= 1)
        assert first.frames[0]["type"] == "hydrate"

        # The room empties, which stops the producer ...
        hub.unsubscribe("first")
        assert _wait_until(lambda: hub.stats()["producers_live"] == 0, timeout=10.0)

        # ... so the next restart-free join must start a fresh one regardless.
        assert hub.subscribe("second", sink=second, restart_producer=False) is True
        assert _wait_until(lambda: second.count() >= 1)
        assert second.frames[0]["type"] == "hydrate"
        assert hub.stats()["generation"] == 2
        assert generations["count"] == 2
    finally:
        gate.set()
        hub.stop()


def test_the_default_join_still_restarts_so_stream_clients_are_unchanged():
    """The stream lane's semantics are NOT touched by O-H5.

    Every other caller of ``subscribe`` — the socket stream lane included —
    needs its first frame to be a hydrate, and the flag defaults to True so none
    of them has to know this option exists. Pinned separately from the
    late-joiner test above because that one asserts the BEHAVIOUR while this
    asserts the DEFAULT: a flag whose default silently flipped would leave that
    test green only until somebody passed the argument explicitly.
    """

    gate = threading.Event()
    factory, generations, _alive = _generational_source(4, gate)
    hub = StreamHub(factory, buffer_limit=64)
    early, late = _Recorder(), _Recorder()
    try:
        hub.subscribe("early", sink=early)
        assert _wait_until(lambda: early.count() >= 2)
        hub.subscribe("late", sink=late)
        assert _wait_until(lambda: late.count() >= 1)

        assert hub.stats()["generation"] == 2
        assert generations["count"] == 2
        assert late.frames[0]["type"] == "hydrate"
    finally:
        gate.set()
        hub.stop()


def test_a_second_subscribe_for_the_same_key_is_refused_not_duplicated():
    gate = threading.Event()
    hub = StreamHub(_counting_source(5, gate=gate), buffer_limit=8)
    try:
        assert hub.subscribe("a", sink=_Recorder()) is True
        assert hub.subscribe("a", sink=_Recorder()) is False
        assert hub.subscriber_count() == 1
    finally:
        gate.set()
        hub.stop()


# ── slow consumers ──────────────────────────────────────────────────────────


def test_a_slow_subscriber_is_dropped_while_the_fast_one_keeps_receiving():
    """The anti-vacuity test for backpressure.

    A ``subscription_dropped`` frame appearing proves nothing on its own — it
    is equally consistent with a hub that dropped BOTH subscribers, or with a
    producer that stalled and delivered nothing to anyone. So this asserts all
    three halves: the slow one was dropped for backpressure, the fast one
    received every frame anyway, and the producer ran to completion.
    """

    blocked = threading.Event()
    producer_finished = threading.Event()
    slow = _Recorder(gate=blocked)
    fast = _Recorder()
    drops: list[tuple[str, dict]] = []

    def _factory():
        def _generate():
            for index in range(400):
                # Paced like the real producer, which rebuilds a snapshot core
                # per delta batch and polls between them. A consumer whose only
                # job is a socket write keeps up with that easily — which is
                # exactly why a subscriber that CANNOT keep up is a wedged one,
                # not a busy one, and why the drop is the right answer.
                time.sleep(0.0005)
                yield {"type": "delta", "index": index}
            producer_finished.set()
            # Hold the lane open the way the real stream does — by polling, so
            # the run does not end for its own reasons mid-assertion.
            while not blocked.is_set():
                time.sleep(0.005)
                yield {"type": "heartbeat", "index": -1}

        return _generate()

    hub = StreamHub(_factory, buffer_limit=8)
    try:
        # Both attached BEFORE any frame flows, so the frames below are one
        # generation and the comparison between the two clients is exact.
        hub.subscribe(
            "slow",
            sink=slow,
            on_drop=lambda reason, stats: drops.append((reason, stats)),
        )
        hub.subscribe("fast", sink=fast)

        # The fast subscriber gets everything...
        assert _wait_until(lambda: fast.count() >= 400, timeout=15.0)
        # ...and the producer was never held up by the wedged one.
        assert producer_finished.is_set()

        # The slow subscriber is marked dropped as soon as its bound is
        # exceeded — before it is even unstuck.
        assert _wait_until(lambda: hub.stats()["subscriptions"] is not None)
        slow_stats = next(
            row for row in hub.stats()["subscriptions"] if row["key"] == "slow"
        )
        assert slow_stats["dropped"] is True
        assert slow_stats["drop_reason"] == DROP_REASON_BACKPRESSURE

        # Releasing it delivers the ONE typed notification and ends it.
        blocked.set()
        assert _wait_until(lambda: bool(drops))
        reason, stats = drops[0]
        assert reason == DROP_REASON_BACKPRESSURE
        assert stats["frames_discarded"] > 0
        # It fell behind: it did NOT receive everything the fast one did.
        assert slow.count() < fast.count()
    finally:
        blocked.set()
        hub.stop()


def test_a_sink_that_raises_drops_only_that_subscriber():
    gate = threading.Event()
    drops: list[str] = []
    healthy = _Recorder()

    def _explode(frame: dict) -> None:
        raise ConnectionError("client went away")

    hub = StreamHub(_counting_source(30, gate=gate), buffer_limit=64)
    try:
        hub.subscribe(
            "broken", sink=_explode, on_drop=lambda reason, stats: drops.append(reason)
        )
        hub.subscribe("healthy", sink=healthy)
        assert _wait_until(lambda: bool(drops))
        assert drops[0].startswith("sink_error:")
        assert _wait_until(lambda: healthy.count() >= 30)
    finally:
        gate.set()
        hub.stop()


def test_a_producer_that_ends_tells_its_subscribers_why():
    """A stream that simply went quiet is indistinguishable from a wedged one."""

    drops: list[str] = []
    recorder = _Recorder()
    hub = StreamHub(_counting_source(3), buffer_limit=8)
    try:
        hub.subscribe(
            "a", sink=recorder, on_drop=lambda reason, stats: drops.append(reason)
        )
        assert _wait_until(lambda: bool(drops))
        assert drops[0] == DROP_REASON_PRODUCER_ENDED
        assert recorder.count() == 3
    finally:
        hub.stop()


def test_a_producer_that_raises_is_reported_and_never_escapes():
    drops: list[str] = []

    def _factory():
        def _generate():
            yield {"type": "delta", "index": 0}
            raise RuntimeError("projection exploded")

        return _generate()

    logged: list[dict] = []
    hub = StreamHub(_factory, buffer_limit=8, log=logged.append)
    try:
        hub.subscribe("a", sink=_Recorder(), on_drop=lambda reason, _s: drops.append(reason))
        assert _wait_until(lambda: bool(drops))
        assert drops[0] == "producer_error:RuntimeError"
        assert hub.stats()["producer_error"] == "producer_error:RuntimeError"
        assert logged and logged[0]["event"] == "serve_stream_producer_error"
    finally:
        hub.stop()


# ── lifecycle ───────────────────────────────────────────────────────────────


def _parking_source(gate: threading.Event, alive: set):
    """A source that yields one hydrate and then PARKS on its stop event.

    The counting fakes above hold the lane open by polling, which is what the
    real stream does between frames — and which means a producer notices a
    superseding generation or an empty room on its own next lap. That is fine
    for the fan-out tests and useless for the LIFECYCLE ones: it gives the hub
    a second way to be right, so removing the first leaves the test green.

    This one ends only when something sets the stop event handed to it, so
    "who stops this producer" is the only variable left.
    """

    def _factory(stop: threading.Event):
        def _generate():
            token = object()
            alive.add(token)
            try:
                yield {"type": "hydrate", "index": 0}
                # Parks here. Not a poll: the stop event is the only way out.
                stop.wait(30.0)
                gate.wait(0.0)
            finally:
                alive.discard(token)

        return _generate()

    return _factory


def test_unsubscribing_the_last_client_stops_a_producer_that_is_parked():
    """F5's first half, with the producer's own polling taken away.

    ``unsubscribe`` sets the stop event when the room empties precisely because
    the producer's in-loop check only fires when a frame arrives — so a source
    between frames would keep a projection lane alive for nobody. A parked
    producer is exactly that case.
    """

    gate = threading.Event()
    alive: set = set()
    hub = StreamHub(_parking_source(gate, alive), buffer_limit=64)
    try:
        hub.subscribe("a", sink=_Recorder())
        assert _wait_until(lambda: hub.stats()["frames_produced"] >= 1)
        assert _wait_until(lambda: len(alive) == 1)

        hub.unsubscribe("a")
        assert _wait_until(lambda: hub.stats()["producers_live"] == 0, timeout=5.0), (
            "the room emptied and the parked producer kept the lane alive"
        )
        assert _wait_until(lambda: alive == set(), timeout=5.0)
    finally:
        gate.set()
        hub.stop()


def test_the_producer_stops_when_the_last_subscriber_leaves():
    """A service nobody is watching must not keep rebuilding projections.

    ``producer_running`` alone was never enough to see the leak: it reports the
    CURRENT generation, so five abandoned producers all showed False. The
    count is the assertion, and zero is the number.
    """

    gate = threading.Event()
    hub = StreamHub(_counting_source(1000, gate=gate), buffer_limit=256)
    try:
        hub.subscribe("a", sink=_Recorder())
        assert _wait_until(lambda: hub.stats()["frames_produced"] > 0)
        assert hub.stats()["producers_live"] == 1
        hub.unsubscribe("a")
        assert _wait_until(lambda: hub.stats()["producer_running"] is False, timeout=10.0)
        assert _wait_until(lambda: hub.stats()["producers_live"] == 0, timeout=10.0)
        assert hub.stats()["producers"] == []
    finally:
        gate.set()
        hub.stop()


def test_producers_do_not_accumulate_one_per_subscribe():
    """F5, the leak itself.

    Every subscribe supersedes the running producer. That used to be ALL it
    did: the old generator was noticed only when it next yielded, nothing
    stopped it when the room emptied, and ``stop()`` joined the newest alone.
    Five subscribes left five producer threads and five open generators alive
    forever, each rebuilding a full snapshot core for ``subscriber_count: 0`` —
    so launcher reconnect churn multiplied the exact cost the one-producer
    design exists to avoid.

    Both halves are asserted: the count does not GROW with subscribes, and it
    returns to ZERO when the room empties. Either alone passes vacuously — a
    hub that never started a producer satisfies the second.
    """

    gate = threading.Event()
    factory, generations, alive = _generational_source(3, gate)
    hub = StreamHub(factory, buffer_limit=64)
    recorders = {}
    try:
        for index in range(5):
            key = f"client-{index}"
            recorders[key] = _Recorder()
            hub.subscribe(key, sink=recorders[key])
            # Each newcomer really is served — otherwise "one producer" would
            # be trivially satisfied by a hub that stopped producing.
            assert _wait_until(lambda k=key: recorders[k].count() >= 3), key
            assert _wait_until(lambda: hub.stats()["producers_live"] <= 1), index

        stats = hub.stats()
        assert stats["subscribers"] == 5
        assert stats["producers_live"] == 1
        assert stats["producers_superseded"] == 0
        assert stats["generation"] == 5
        # ...and the FACTORY's own view agrees: one generator body is running.
        assert _wait_until(lambda: len(alive()) == 1)
        assert generations["count"] == 5

        for key in list(recorders):
            hub.unsubscribe(key)
        assert _wait_until(lambda: hub.stats()["producers_live"] == 0, timeout=10.0)
        # The generator bodies really exited; the count is not just bookkeeping.
        assert _wait_until(lambda: alive() == set(), timeout=10.0)
    finally:
        gate.set()
        hub.stop()


def _staggered_source(alive: set, exits: list):
    """Uninterruptible producers whose parks get SHORTER with each generation.

    Two things have to be true at once for this to test the join and nothing
    else. The producers must be genuinely uninterruptible between frames — which
    is what ``stop()`` has to cope with in the general case, and what the real
    ``_stream_source`` USED to be: it now takes the per-generation stop event and
    binds it to ``request_control``'s cancellation seam (EG-4.1 / TC-1), so it
    abandons its park within ~100ms. That fix is a property of one source, not of
    the hub, and the hub must still join a source that has no such seam — an
    injected factory, or a future producer over a blocking client library. Hence
    these deliberately deaf generators. And the NEWEST one must not be the
    slowest.

    That second point is the whole design. With every generation sleeping the
    same 0.4s, joining only the newest still waited 0.4s, and the older ones
    (whose sleeps started earlier) finished inside that window all by
    themselves — so a ``stop()`` that ignored them looked correct. Staggering
    it the other way removes the accident: generation 1 is parked for 3s,
    generation 4 for 0.75s, so joining the newest alone returns while the first
    three are demonstrably still running.
    """

    counter = {"n": 0}

    def _factory():
        counter["n"] += 1
        delay = 3.0 / counter["n"]

        def _generate():
            token = object()
            alive.add(token)
            try:
                yield {"type": "hydrate", "index": 0}
                while True:
                    time.sleep(delay)
                    yield {"type": "heartbeat", "index": -1}
            finally:
                alive.discard(token)
                exits.append(time.monotonic())

        return _generate()

    return _factory


def test_stop_joins_every_live_producer_not_only_the_newest():
    """``stop()`` used to join ``self._producer`` — one handle, the last one
    written. Anything superseded and still running was never waited for.

    Asserted two ways, because "they were gone afterwards" is not the claim.
    The claim is that ``stop()`` DID NOT RETURN until they were gone, so every
    producer's exit timestamp is compared against the moment ``stop()``
    returned. A hub that merely outlived them fails that.
    """

    alive: set = set()
    exits: list = []
    recorders = []
    hub = StreamHub(_staggered_source(alive, exits), buffer_limit=64)
    try:
        for index in range(4):
            recorder = _Recorder()
            recorders.append(recorder)
            hub.subscribe(f"client-{index}", sink=recorder)
            # Its hydrate landed, so this producer is now inside its sleep —
            # which is where the next subscribe will supersede it.
            assert _wait_until(lambda r=recorder: r.count() >= 1)

        # The state the leak used to leave behind permanently: four live
        # generations, three of them superseded and unable to act on it yet.
        assert len(alive) == 4, f"only {len(alive)} producers were live"

        hub.stop(join_timeout=20.0)
        returned = time.monotonic()

        assert alive == set(), f"{len(alive)} producers outlived stop()"
        assert len(exits) == 4, f"only {len(exits)} producers ever exited"
        late = [t for t in exits if t > returned]
        assert not late, (
            f"{len(late)} producers were still running when stop() returned — "
            "it joined the newest and left the rest"
        )
        assert hub.stats()["producers_live"] == 0
        assert hub.subscriber_count() == 0
    finally:
        hub.stop()


def test_a_stats_row_names_every_producer_and_whether_it_is_superseded():
    """A leak has to be a NUMBER an operator can read, not an absence."""

    gate = threading.Event()
    factory, _generations, _alive = _generational_source(2, gate)
    hub = StreamHub(factory, buffer_limit=64)
    try:
        hub.subscribe("a", sink=_Recorder())
        assert _wait_until(lambda: hub.stats()["frames_produced"] > 0)
        row = hub.stats()["producers"][0]
        assert set(row) == {
            "generation",
            "state",
            "alive",
            "superseded",
            "stopping",
            "frames",
            "idle_ms",
            "age_ms",
        }
        assert row["generation"] == 1
        assert row["superseded"] is False
        assert row["alive"] is True
        assert row["frames"] >= 1
        # ``awaiting_frame`` is the load-bearing state: a producer parked in
        # ``next()`` cannot be interrupted from another thread, so the honest
        # answer is to report it with the age of its last frame.
        assert row["state"] in {"awaiting_frame", "fanning_out"}
        assert row["idle_ms"] >= 0
    finally:
        gate.set()
        hub.stop()


def test_unsubscribe_is_typed_and_idempotent():
    gate = threading.Event()
    hub = StreamHub(_counting_source(5, gate=gate), buffer_limit=8)
    try:
        hub.subscribe("a", sink=_Recorder())
        assert hub.unsubscribe("a") is True
        assert hub.unsubscribe("a") is False
        assert hub.has("a") is False
    finally:
        gate.set()
        hub.stop()


# ── the two bounds ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("limit", [1, 4, 64])
def test_the_buffer_bound_is_reported_so_a_client_can_reason_about_it(limit):
    gate = threading.Event()
    hub = StreamHub(_counting_source(2, gate=gate), buffer_limit=limit)
    try:
        assert hub.stats()["buffer_limit"] == limit
        assert hub.stats()["byte_limit"] == DEFAULT_BYTE_LIMIT
    finally:
        gate.set()
        hub.stop()


def test_the_byte_bound_trips_before_the_frame_bound_and_says_which():
    """F8. A frame count was never a memory bound.

    These frames are not a fixed cost — the hydrate at the head of every
    generation is a complete projection — so 256 of them is a multi-gigabyte
    buffer per stalled client, times 32 connections. The buffer is bounded
    TWICE and whichever trips first wins; this sets the frame bound absurdly
    high so the ONLY thing that can stop the subscriber is the byte total.
    """

    blocked = threading.Event()
    slow = _Recorder(gate=blocked)
    drops: list[tuple[str, dict]] = []
    payload = "x" * 4096

    def _factory():
        def _generate():
            index = 0
            while not blocked.is_set():
                index += 1
                time.sleep(0.0005)
                yield {"type": "delta", "index": index, "blob": payload}

        return _generate()

    hub = StreamHub(_factory, buffer_limit=100_000, byte_limit=64 * 1024)
    try:
        hub.subscribe(
            "slow", sink=slow, on_drop=lambda reason, stats: drops.append((reason, stats))
        )
        assert _wait_until(lambda: hub.stats()["subscriptions"][0]["dropped"] is True)
        stats = hub.stats()["subscriptions"][0]
        assert stats["drop_reason"] == DROP_REASON_BACKPRESSURE
        # WHICH bound, named — not "dropped for backpressure" with no numbers.
        assert stats["drop_bound"] == BOUND_BYTES
        assert stats["byte_limit"] == 64 * 1024
        # It was nowhere near the frame bound, which is the whole point.
        assert stats["frame_limit"] == 100_000
        assert stats["buffered"] < 100_000

        blocked.set()
        assert _wait_until(lambda: bool(drops))
        _reason, drop_stats = drops[0]
        assert drop_stats["drop_bound"] == BOUND_BYTES
        assert drop_stats["bytes_discarded"] > 0
        assert drop_stats["frames_discarded"] > 0
    finally:
        blocked.set()
        hub.stop()


def test_the_frame_bound_still_trips_when_the_frames_are_small():
    """The other bound, on the other side of the same decision."""

    blocked = threading.Event()
    slow = _Recorder(gate=blocked)
    drops: list[tuple[str, dict]] = []

    def _factory():
        def _generate():
            index = 0
            while not blocked.is_set():
                index += 1
                time.sleep(0.0005)
                yield {"type": "delta", "index": index}

        return _generate()

    hub = StreamHub(_factory, buffer_limit=4, byte_limit=64 * 1024 * 1024)
    try:
        hub.subscribe(
            "slow", sink=slow, on_drop=lambda reason, stats: drops.append((reason, stats))
        )
        assert _wait_until(lambda: hub.stats()["subscriptions"][0]["dropped"] is True)
        stats = hub.stats()["subscriptions"][0]
        assert stats["drop_reason"] == DROP_REASON_BACKPRESSURE
        assert stats["drop_bound"] == BOUND_FRAMES
        blocked.set()
        assert _wait_until(lambda: bool(drops))
        assert drops[0][1]["drop_bound"] == BOUND_FRAMES
    finally:
        blocked.set()
        hub.stop()


def test_a_frame_is_measured_once_by_the_producer_not_once_per_subscriber():
    """The accounting must not scale with the fan-out it exists to serve."""

    from agent_runtime import serve_stream_hub as hub_module

    measured: list[int] = []
    original = hub_module._frame_bytes

    def _counting(frame):
        measured.append(1)
        return original(frame)

    gate = threading.Event()
    hub_module._frame_bytes = _counting
    hub = StreamHub(_counting_source(10, gate=gate), buffer_limit=64)
    try:
        for index in range(4):
            hub.subscribe(f"client-{index}", sink=_Recorder())
        assert _wait_until(lambda: hub.stats()["frames_produced"] >= 10)
        produced = hub.stats()["frames_produced"]
        # One measurement per produced frame — not four (one per subscriber).
        assert len(measured) <= produced + 2
    finally:
        gate.set()
        hub.stop()
        hub_module._frame_bytes = original
