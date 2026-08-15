"""The office push lane behind a REAL ``StreamHub`` and a REAL frame producer.

Everything in ``test_serve_rpc_office_subscribe`` runs against a ``_FakeHub``
and hand-built frames. That is the right shape for the sink's FILTERING rules —
an integration test that happened to produce no frame looks identical to a
filter that drops everything — but it means the lane has never once been shown
to carry an actual office write. This file closes that gap: one real
``StreamHub``, one real ``stream_frames`` producer over the real ``EventLog``,
one real ``OfficeStore`` write, and an assertion about what a subscriber
actually received.

Two defects only a real producer could say, both since FIXED
------------------------------------------------------------
This file landed with two ``xfail(strict=True)`` tests, because the lane did
not work end to end and failed in two independent places. Both are indicative
assertions now, and the history is kept because it IS the red-proof — each
turned red before its fix and green after, by construction:

1. **Every subscribe immediately pushed a spurious ``runtime.office.resync``.**
   ``StreamHub.subscribe`` deliberately restarts the producer so a late joiner
   opens on a ``hydrate``, and ``office_patch_sink`` branched on the frame TYPE
   before it ever compared offsets — so a hydrate at or below the baseline took
   the ``full_core`` resync exit unconditionally and every subscribe answered
   itself with "subscribe again". Fixed by moving the baseline gate above the
   type branch (``2ccf3ab337``); see
   :func:`test_the_baseline_rule_absorbs_the_hubs_mandatory_rehydrate`.

2. **The producer never promoted ``office_actor``, so a real office write
   arrived as a resync and never as a patch.** Promotion is entity-NEGOTIATED
   (``patch_coverage``): a batch ships as a ``patch`` frame only when every
   ``state.patched`` in it names an entity the room declared it can fold, and
   ``serve.py``'s ``_accepted_fold_entities`` read the STREAM lane's
   declarations only. An RPC office subscriber contributed nothing to that
   table, so an office-only room resolved to ``HISTORICAL_FOLD_ENTITIES`` —
   ``{persona_instance, incident}`` — and every office write for the whole
   production life of this lane demoted to a full core. Fixed by having the
   office registry declare
   :data:`~agent_runtime.serve_office_subscriptions.OFFICE_FOLD_ENTITIES` per
   live subscription and ``_accepted_fold_entities`` intersect over BOTH lanes;
   see
   :func:`test_a_real_office_write_reaches_a_default_wired_subscriber_as_a_patch`.

Two producers, on purpose
-------------------------
``live_hub`` builds its source the way ``serve.py``'s ``_stream_source`` does
and — like it — DERIVES the fold set per producer generation from the office
registry when a test does not pin one. A test that passes an explicit
``fold_entities=OFFICE_FOLD_ENTITIES`` asserts about everything downstream of
the negotiation; a test that passes nothing asserts about the negotiation
itself. Keeping both is what stops a regression in the declaration from hiding
behind a fixture that hard-codes the answer.

The composition inside ``serve.py`` is MIRRORED here, not executed. That seam
is pinned against the real ``serve_loop`` in ``test_serve_socket_lane.py``
(``test_an_rpc_office_subscriber_declares_into_the_shared_producer`` and the
empty-intersection trap beside it).

Timing: every wait is a bounded poll on a CONDITION, never a sleep — threads
are involved and a flaky test here is worse than no test. The producer is run
at a 0.25s heartbeat so an abandoned generation is never parked in ``next()``
for longer than that; the 30s per-test cap is never approached.
"""

from __future__ import annotations

import time

import pytest

from agent_runtime import serve_rpc
from agent_runtime.patch_coverage import HISTORICAL_FOLD_ENTITIES, accepted_fold_entities
from agent_runtime.serve_office_subscriptions import (
    OFFICE_FOLD_ENTITIES,
    OFFICE_PATCH_METHOD,
    OFFICE_RESYNC_METHOD,
    OFFICE_SUBSCRIPTIONS,
    office_patch_sink,
    office_subscription_key,
)
from agent_runtime.serve_stream_hub import StreamHub

WORKSPACE = "ws_live_hub_test"
ACTOR = "personainst_qa_agent_9c8a382f"
OTHER_WORKSPACE = "ws_live_hub_somebody_else"
OTHER_ACTOR = "personainst_dev_3ebfce41"

#: Pinned locally so this file states the set it expects rather than importing
#: whatever production currently holds: a widening of
#: ``OFFICE_FOLD_ENTITIES`` that nobody meant must turn this red, and an
#: assertion written as ``x == x`` cannot do that.
EXPECTED_OFFICE_FOLD_ENTITIES = HISTORICAL_FOLD_ENTITIES | {"office_actor"}


def test_the_office_declaration_is_the_historical_set_plus_office_actor():
    """The one-line claim the rest of this file is built on top of.

    A SUPERSET, not ``{office_actor}`` alone, and that is the difference
    between a fix and a regression: the accepted set is an INTERSECTION over
    the room, so a bare ``{office_actor}`` would zero out against any legacy
    stream client sitting beside this subscriber — promoting nothing at all for
    anyone, which is strictly worse than the bug it was meant to fix. Pinned in
    both directions so neither half can be dropped silently.
    """

    assert OFFICE_FOLD_ENTITIES == EXPECTED_OFFICE_FOLD_ENTITIES
    assert HISTORICAL_FOLD_ENTITIES < OFFICE_FOLD_ENTITIES
    assert "office_actor" in OFFICE_FOLD_ENTITIES
    # The trap, stated as the arithmetic: a legacy client beside an office
    # subscriber must still accept the historical set, NOT the empty set.
    assert accepted_fold_entities([None, OFFICE_FOLD_ENTITIES]) == HISTORICAL_FOLD_ENTITIES
    # Anti-vacuity for the line above: the singleton really would have zeroed.
    assert accepted_fold_entities([None, frozenset({"office_actor"})]) == frozenset()

#: One bounded deadline for every condition wait. Generous against a cold
#: snapshot build, and four of them still sit inside the 30s per-test cap.
_DEADLINE_SECONDS = 5.0
_POLL_SECONDS = 0.01


# ── fixtures and helpers ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry is PROCESS-GLOBAL; a bound factory would outlive its hub."""

    OFFICE_SUBSCRIPTIONS.bind(None)
    yield
    OFFICE_SUBSCRIPTIONS.bind(None)


#: "derive it, do not pin it" — distinguishable from an explicit ``None``,
#: which is a real declaration meaning "this producer was told nothing".
_DERIVE = object()


@pytest.fixture
def live_hub():
    """A REAL ``StreamHub`` over the REAL ``stream_frames`` producer, bound.

    Built the way ``serve.py``'s ``_stream_source`` builds it — ``stream_frames``
    wrapped in ``to_jsonable``, one generator per generation — with only the
    cadences shortened. The stop event is taken and honoured because the hub
    cannot interrupt a generator parked in ``next()``: without it, a superseded
    or abandoned generation would sit on the default 5s heartbeat and the
    teardown assertions would be measuring the heartbeat, not the lifecycle.

    Called with NO argument it also mirrors serve.py's other half: the fold set
    is DERIVED, inside ``_source`` so it is re-read per producer generation
    exactly as ``_accepted_fold_entities`` is. That is what makes ``live_hub()``
    mean "the producer serve.py actually builds" rather than "a producer that
    was told nothing" — and it is the only way a test can catch the office
    registry failing to declare.
    """

    hubs: list[StreamHub] = []

    def _make(fold_entities=_DERIVE) -> StreamHub:
        from agent_runtime.serde import to_jsonable
        from agent_runtime.stream import stream_frames

        def _source(stop):
            declared = (
                accepted_fold_entities(OFFICE_SUBSCRIPTIONS.declarations())
                if fold_entities is _DERIVE
                else fold_entities
            )
            for frame in stream_frames(
                poll_interval_seconds=0.02,
                heartbeat_interval_seconds=0.25,
                delta_debounce_seconds=0.05,
                fold_entities=declared,
            ):
                if stop.is_set():
                    return
                yield to_jsonable(frame)

        hub = StreamHub(_source)
        hubs.append(hub)
        OFFICE_SUBSCRIPTIONS.bind(lambda: hub)
        return hub

    yield _make

    OFFICE_SUBSCRIPTIONS.bind(None)
    for hub in hubs:
        hub.stop(join_timeout=2.0)


def _wait_for(predicate, *, what: str, timeout: float = _DEADLINE_SECONDS):
    """Poll *predicate* to a bounded deadline. Never a bare sleep.

    Returns the first truthy value so a caller can wait for a thing and use it
    in one step; raises with WHAT was being waited on, because a bare timeout in
    a threaded test is the least actionable failure there is.
    """

    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for: {what}")
        time.sleep(_POLL_SECONDS)


def _store():
    from agent_runtime.office_store import OfficeStore

    return OfficeStore()


def _actor_payload(actor_key: str = ACTOR, *, position=(1.0, 2.0)) -> dict:
    return {
        "persona_id": "qa",
        "persona_instance_id": actor_key,
        "items": [
            {
                "item_id": actor_key,
                "kind": "agent",
                "persona_id": "qa",
                "position": [float(position[0]), float(position[1])],
                "scale": 1.0,
            }
        ],
    }


def _seed_office(store=None):
    """A real surface with one real actor, through the real store."""

    store = store or _store()
    store.ensure_surface(WORKSPACE, created_by="seed")
    store.upsert_actor(WORKSPACE, _actor_payload(), updated_by="seed")
    return store


def _subscribe(sent: list, *, connection_key: str = "c1", rid: str = "r1") -> dict:
    return serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "runtime.office.subscribe",
            "params": {"workspace_id": WORKSPACE},
        },
        serve_rpc.RpcContext(
            connection_key=connection_key, transport="socket", emit=sent.append
        ),
    )


def _settled_subscription(sent: list, hub: StreamHub, **kwargs) -> dict:
    """Subscribe, then wait out the hub's mandatory re-hydrate.

    Every test below has to do this, and the reason is defect (1) in the module
    docstring: the re-hydrate reaches the sink as a spurious resync instead of
    being dropped by the baseline rule. Draining it HERE — rather than in each
    test — keeps that defect stated in exactly one place, so the assertions
    downstream are about the write they made and not about the noise.

    Waiting on DELIVERY rather than production is what makes the drain sound:
    clearing ``sent`` while the hydrate was still queued would let its resync
    land afterwards and be mistaken for the notification the test's own write
    produced — a test that passes for the wrong reason, which is worse here
    than one that fails.
    """

    reply = _subscribe(sent, **kwargs)
    assert "error" not in reply, reply
    _wait_for(
        lambda: _frames_delivered(hub, kwargs.get("connection_key", "c1")) >= 1,
        what="the hub's re-baselining hydrate to reach the sink",
    )
    del sent[:]
    return reply["result"]


def _methods(sent: list) -> list[str]:
    return [frame["method"] for frame in sent]


def _frames_delivered(hub: StreamHub, connection_key: str = "c1") -> int:
    """How many frames this subscription's PUMP has finished handing the sink.

    Not ``frames_produced``, and the difference is a real race rather than a
    nicety: the hub increments ``frames_produced`` while it OFFERS a frame into
    a bounded queue under its own lock, and the sink runs later on the
    subscriber's pump thread. Waiting on the produced count therefore proves
    only that a frame was queued — a test that then asserted on what the sink
    emitted would be reading a list the pump had not written yet, and would
    pass or fail on thread scheduling. ``frames_delivered`` is incremented
    AFTER the sink call returns, so it is the only count that means "the sink
    has run".
    """

    target = office_subscription_key(connection_key, WORKSPACE)
    for row in hub.stats()["subscriptions"]:
        if row["key"] == target:
            return int(row["frames_delivered"])
    return 0


# ── claim 1: a real office write produces a notification ────────────────────


def test_a_real_office_write_reaches_a_subscriber_as_an_office_patch(live_hub):
    """The claim that had never been shown: a store write becomes a push.

    No hand-built frame anywhere in this path. ``OfficeStore.upsert_actor``
    appends to the real ``EventLog``; the real ``stream_frames`` generator
    drains it, decides it is patch-coverable and assembles a real
    ``patch_batch_frame``; the real ``StreamHub`` fans it out to the sink the
    RPC handler registered; and what the connection's ``emit`` receives is a
    JSON-RPC notification carrying THIS actor's row.

    The row is asserted down to the moved coordinate rather than merely being
    present, because a lane that forwarded a stale row would look identical at
    the envelope level and would leave the canvas silently wrong — the exact
    failure class the whole workstream exists to retire.
    """

    store = _seed_office()
    hub = live_hub(fold_entities=OFFICE_FOLD_ENTITIES)
    sent: list[dict] = []
    _settled_subscription(sent, hub)

    store.upsert_actor(
        WORKSPACE, _actor_payload(position=(42.0, 43.0)), updated_by="live-write"
    )

    frame = _wait_for(
        lambda: next(
            (item for item in sent if item["method"] == OFFICE_PATCH_METHOD), None
        ),
        what="a runtime.office.patch notification for a real store write",
    )
    assert "id" not in frame
    params = frame["params"]
    assert params["workspace_id"] == WORKSPACE
    rows = params["patches"]
    assert [row["id"] for row in rows] == [f"{WORKSPACE}/{ACTOR}"]
    assert rows[0]["entity"] == "office_actor"
    assert rows[0]["op"] == "upsert"
    assert rows[0]["changed"]["items"][0]["position"] == [42.0, 43.0]


def test_a_real_office_write_reaches_a_default_wired_subscriber_as_a_patch(
    live_hub,
):
    """The SAME write, against the producer ``serve.py`` actually builds.

    Nothing is pinned here: the fold set is derived from the office registry
    per producer generation, exactly as ``_accepted_fold_entities`` derives it
    — so this is the room the ruling creates when the launcher stops joining
    the legacy stream, holding nothing but RPC office subscribers.

    Was ``xfail(strict=True)`` when this file landed, and it earned the marker.
    ``_accepted_fold_entities`` read the STREAM lane's declaration table only;
    an RPC office subscriber contributed nothing to it, so the room resolved to
    ``HISTORICAL_FOLD_ENTITIES``, the batch was not patch-coverable, the frame
    was a full core, and the subscriber was told to refetch. That is not merely
    "no better than the legacy lane": it is worse. The client paid a resync
    round trip per office write, and each resync's re-subscribe restarted the
    producer and cost every other subscriber a fresh 822 KB core. The lane had
    therefore never carried a single patch in production, on any transport.

    The fix is a declaration, not a widening: the office registry contributes
    :data:`OFFICE_FOLD_ENTITIES` per live subscription and the producer
    intersects over both lanes. ``HISTORICAL_FOLD_ENTITIES`` is untouched, so a
    client that says nothing is answered exactly as it was yesterday.
    """

    store = _seed_office()
    hub = live_hub()
    sent: list[dict] = []
    _settled_subscription(sent, hub)

    store.upsert_actor(
        WORKSPACE, _actor_payload(position=(42.0, 43.0)), updated_by="live-write"
    )

    frame = _wait_for(
        lambda: next((item for item in sent if item.get("method")), None),
        what="any notification at all for a real store write",
    )
    assert frame["method"] == OFFICE_PATCH_METHOD, (
        "the default-wired producer sent "
        f"{frame['method']} / {frame['params'].get('reason')} instead of a patch"
    )
    # The row, not just the envelope: a lane that promoted the batch and then
    # forwarded a stale actor would satisfy the assertion above and still leave
    # the canvas wrong.
    rows = frame["params"]["patches"]
    assert [row["id"] for row in rows] == [f"{WORKSPACE}/{ACTOR}"]
    assert rows[0]["changed"]["items"][0]["position"] == [42.0, 43.0]


def test_another_workspaces_real_write_never_reaches_this_subscriber(live_hub):
    """Addressed, not broadcast — against a real producer this time.

    The fake-hub file already proves the sink FILTERS a mixed frame, and that
    is the right place for a filtering rule. What only a real producer can add
    is that the filter is load-bearing at all: the hub fans every frame out to
    every subscriber, so a second workspace's write genuinely arrives at this
    subscriber's sink and is genuinely discarded there rather than never being
    offered.

    The absence is asserted only after the frame has been DELIVERED to the
    sink — an absence measured before the pump ran would be an assertion about
    thread timing, and would pass against a sink that dropped everything.
    """

    store = _seed_office()
    store.ensure_surface(OTHER_WORKSPACE, created_by="seed")
    store.upsert_actor(OTHER_WORKSPACE, _actor_payload(OTHER_ACTOR), updated_by="seed")

    hub = live_hub(fold_entities=OFFICE_FOLD_ENTITIES)
    sent: list[dict] = []
    _settled_subscription(sent, hub)
    delivered = _frames_delivered(hub)

    store.upsert_actor(
        OTHER_WORKSPACE,
        _actor_payload(OTHER_ACTOR, position=(99.0, 99.0)),
        updated_by="live-write-elsewhere",
    )

    _wait_for(
        lambda: _frames_delivered(hub) > delivered,
        what="the other workspace's frame to reach this subscriber's sink",
    )
    assert sent == [], f"another workspace's write crossed the lane: {sent}"


# ── claim 2: the RPC subscriber is what keeps the producer alive ────────────


def test_an_rpc_subscription_alone_sustains_the_real_producer(live_hub):
    """Load-bearing and non-obvious: the office lane is a hub SUBSCRIBER.

    ``StreamHub`` stops producing the moment its room empties, and the ruling's
    whole point is that the launcher stops joining the legacy stream. So if an
    RPC subscription merely OBSERVED the hub, the room would be empty, the
    producer would exit, and the office would silently stop updating — with a
    subscriber that believes it is current, which is the worst shape this
    failure can take.

    Proven by production rather than by presence: the count is checked, and
    then the producer is required to keep emitting FRESH frames with nobody but
    the RPC subscriber attached. A one-frame check would pass against a
    producer that exited immediately after its hydrate.
    """

    _seed_office()
    hub = live_hub(fold_entities=OFFICE_FOLD_ENTITIES)
    sent: list[dict] = []
    _settled_subscription(sent, hub)

    assert hub.subscriber_count() == 1
    assert hub.has(office_subscription_key("c1", WORKSPACE))

    produced = hub.stats()["frames_produced"]
    _wait_for(
        lambda: hub.stats()["frames_produced"] > produced + 1,
        what="the producer to keep producing for the RPC subscriber alone",
    )
    stats = hub.stats()
    assert stats["producer_running"] is True
    assert stats["producers_live"] == 1
    assert stats["producer_error"] is None


# ── claim 3: teardown does not leak ─────────────────────────────────────────


def test_a_refused_duplicate_subscribe_leaves_no_declaration_behind(live_hub):
    """The declaration index is written BEFORE the hub is asked, so it can lie.

    Recording ahead of ``hub.subscribe`` is what stops the producer racing the
    index (see :meth:`OfficeSubscriptions.subscribe`), but it means a refused
    subscribe has already added an entry. Left there, the room would keep being
    widened on behalf of a subscriber that does not exist — and a phantom
    declaration is the one direction this negotiation may never be wrong in,
    because it promotes patches to clients that never promised to fold them.

    A duplicate is the reachable way to make the hub refuse: the same
    connection asking for the same workspace twice, which is a retry, not an
    error condition invented for a test.
    """

    _seed_office()
    hub = live_hub()
    sent: list[dict] = []
    _settled_subscription(sent, hub)
    assert len(OFFICE_SUBSCRIPTIONS.declarations()) == 1

    duplicate = _subscribe([], rid="r2")
    assert duplicate["error"]["data"]["reason"] == "already_subscribed"
    assert len(OFFICE_SUBSCRIPTIONS.declarations()) == 1, (
        "a refused subscribe left a declaration behind: the accepted set is "
        "now widened for a subscriber the hub does not have"
    )

    # And the one real subscription still empties cleanly, so the pruning above
    # did not simply remove the live entry instead of the refused one.
    assert OFFICE_SUBSCRIPTIONS.release("c1") == 1
    assert OFFICE_SUBSCRIPTIONS.declarations() == []


def test_a_subscribe_the_hub_refuses_withdraws_its_own_declaration(live_hub):
    """The OTHER refusal, and the only one where a declaration can truly leak.

    The duplicate above adds nothing to withdraw. A STOPPED hub is the
    reachable case where a subscribe declares first and is then refused:
    ``_close_socket_lane`` stops the hub on the drain and shutdown paths, and a
    subscribe already in flight loses that race — ``StreamHub.subscribe``
    answers False for a stopped hub before it looks at the key at all.

    Left behind, that declaration outlives the connection entirely (the client
    never got a subscription, so it will never be released) and keeps widening
    the accepted set for the next serve in the same process — the registry is
    process-global.
    """

    _seed_office()
    hub = live_hub()
    hub.stop(join_timeout=2.0)

    registered = OFFICE_SUBSCRIPTIONS.subscribe(
        connection_key="c9",
        workspace_id=WORKSPACE,
        baseline_offset=0,
        emit=lambda frame: None,
    )
    assert registered is False
    assert OFFICE_SUBSCRIPTIONS.declarations() == [], (
        "a subscribe the hub refused left its declaration in the registry"
    )
    assert OFFICE_SUBSCRIPTIONS.owned_keys("c9") == set()


def test_release_unsubscribes_from_the_real_hub_and_the_producer_stops(live_hub):
    """A leaked subscriber keeps a producer rebuilding projections for nobody.

    ``release`` is checked against the HUB's own answer, not against the
    registry's index — the registry pruning its own dictionary while the hub
    kept the subscription is exactly the leak this asserts against, and it is
    invisible to a fake hub that shares the registry's bookkeeping.

    The producer stopping is then a second, independent fact: the hub only
    stops a producer when the room empties, so a release that unsubscribed the
    wrong key would leave ``producers_live`` at 1 forever.
    """

    _seed_office()
    hub = live_hub(fold_entities=OFFICE_FOLD_ENTITIES)
    sent: list[dict] = []
    _settled_subscription(sent, hub)

    key = office_subscription_key("c1", WORKSPACE)
    assert hub.has(key)

    assert OFFICE_SUBSCRIPTIONS.release("c1") == 1
    assert hub.has(key) is False
    assert hub.subscriber_count() == 0
    assert OFFICE_SUBSCRIPTIONS.owned_keys("c1") == set()

    _wait_for(
        lambda: hub.stats()["producers_live"] == 0,
        what="the producer to stop once its last subscriber left",
    )
    assert hub.stats()["producer_running"] is False


# ── claim 4: the baseline rule against REAL offsets ─────────────────────────


def test_the_subscribe_baseline_is_the_counter_the_producer_bases_its_batch_on(
    live_hub,
):
    """One counter, or the client's fold chains onto a number nobody produced.

    The subscribe reply's ``watermark.event_offset`` comes from
    ``parity.events_watermark`` under the office lock; the frame's
    ``base_offset`` comes from the stream's own ``_resume_offset(hydrate)``.
    Nothing in the code forces those to be the same number — they are two
    different call sites reading the event log — and the launcher folds a patch
    only when its held watermark EQUALS ``base_offset``. If they disagreed by
    so much as one, every first push after a subscribe would be a sequence gap
    and the lane would resync forever while looking healthy from both ends.

    Asserted with equality, not with an inequality: "close enough" is what a
    gap detector is designed to reject.
    """

    store = _seed_office()
    hub = live_hub(fold_entities=OFFICE_FOLD_ENTITIES)
    sent: list[dict] = []
    result = _settled_subscription(sent, hub)
    baseline = result["watermark"]["event_offset"]
    assert isinstance(baseline, int) and baseline > 0

    store.upsert_actor(
        WORKSPACE, _actor_payload(position=(7.0, 8.0)), updated_by="live-write"
    )
    frame = _wait_for(
        lambda: next(
            (item for item in sent if item["method"] == OFFICE_PATCH_METHOD), None
        ),
        what="the first patch after the baseline",
    )
    params = frame["params"]
    assert params["base_offset"] == baseline
    assert params["watermark"]["event_offset"] > baseline


def test_the_baseline_boundary_drops_at_or_below_and_admits_above(live_hub):
    """Off-by-one in either direction, checked against a REAL frame's offsets.

    The frame is not hand-built: it is reconstructed from the notification a
    real producer just sent, so the offsets are the event log's own. Replaying
    it through sinks built at three baselines is what distinguishes ``<=`` from
    ``<`` — a ``<`` would re-deliver the client's own baseline as its first
    push (a duplicate fold), and a ``<= n+1`` would swallow the first real
    change after it (a silent gap, the failure class that matters more).
    """

    store = _seed_office()
    hub = live_hub(fold_entities=OFFICE_FOLD_ENTITIES)
    sent: list[dict] = []
    _settled_subscription(sent, hub)

    store.upsert_actor(
        WORKSPACE, _actor_payload(position=(11.0, 12.0)), updated_by="live-write"
    )
    frame = _wait_for(
        lambda: next(
            (item for item in sent if item["method"] == OFFICE_PATCH_METHOD), None
        ),
        what="a real patch to replay the boundary against",
    )
    params = frame["params"]
    offset = params["watermark"]["event_offset"]
    replay = {
        "type": "patch",
        "base_offset": params["base_offset"],
        "watermark": params["watermark"],
        "patches": params["patches"],
    }

    for baseline, expected, why in (
        (offset - 1, 1, "one below the frame must cross"),
        (offset, 0, "exactly at the frame is already in the baseline"),
        (offset + 1, 0, "above the frame is behind the client"),
    ):
        got: list[dict] = []
        office_patch_sink(
            workspace_id=WORKSPACE, baseline_offset=baseline, emit=got.append
        )(replay)
        assert len(got) == expected, f"{why} (baseline={baseline}, offset={offset})"


def test_the_baseline_rule_absorbs_the_hubs_mandatory_rehydrate(live_hub):
    """The docstring's first stated seam, tested against the real hub.

    Was ``xfail(strict=True)`` when this file landed, and it earned the marker:
    ``office_patch_sink`` branched on frame TYPE before it compared offsets, so
    the hydrate took the ``full_core`` resync exit and every subscribe answered
    itself with "subscribe again". The fix moved the baseline gate ABOVE the
    type branch, which is what the module docstring had claimed all along — the
    defect was the code disagreeing with its own stated rule, and only a real
    producer could show it.

    ``StreamHub.subscribe`` restarts the producer precisely so a late joiner
    opens on a full core, and ``serve_office_subscriptions`` answers that by
    declaring the hydrate at-or-below the baseline and therefore dropped. Only
    a real hub can produce that hydrate, which is why 23 fake-hub tests never
    saw this: they hand the sink a frame, and no frame arrives unbidden.

    The consequence is a loop, not a wasted frame. The client is told to
    resync, re-subscribes, the hub restarts the producer, and the new hydrate
    is another resync — while every other subscriber attached to that shared
    producer pays a fresh full core each lap.
    """

    _seed_office()
    hub = live_hub(fold_entities=OFFICE_FOLD_ENTITIES)
    sent: list[dict] = []
    reply = _subscribe(sent)
    assert "error" not in reply, reply

    # Delivery, not production: this test asserts an ABSENCE, and an absence
    # measured before the pump has run the sink is not an observation at all —
    # it would report "nothing was pushed" for a frame still sitting in the
    # bounded queue, and would flip with thread scheduling.
    _wait_for(
        lambda: _frames_delivered(hub) >= 1,
        what="the re-baselining hydrate to reach the sink",
    )
    assert _methods(sent) == [], (
        "a fresh subscriber was pushed "
        f"{_methods(sent)} before anything changed: {sent}"
    )


# ── claim 5: a non-coverable batch degrades to resync, never to silence ─────


def test_a_create_moves_the_surface_row_so_the_batch_degrades_to_a_resync(live_hub):
    """The honest degrade, produced by a real write rather than asserted about.

    A CREATE moves ``actor_count`` on the surface row, which an actor-row patch
    cannot express — so ``OfficeStore.upsert_actor`` emits a ``refresh`` op,
    ``batch_is_patch_coverable`` says no, and the stream ships a full core. The
    sink must turn that into ``runtime.office.resync``, because silence there is
    the "client believes it is current" failure the lane exists to end.

    Note this is asserted against a producer that DOES declare ``office_actor``:
    without that, the demotion would be the negotiation's doing rather than the
    surface row's, and the test would pass for the wrong reason.
    """

    store = _seed_office()
    hub = live_hub(fold_entities=OFFICE_FOLD_ENTITIES)
    sent: list[dict] = []
    _settled_subscription(sent, hub)

    store.upsert_actor(
        WORKSPACE,
        _actor_payload("personainst_dev_3ebfce41", position=(5.0, 6.0)),
        updated_by="live-create",
    )

    frame = _wait_for(
        lambda: next((item for item in sent if item.get("method")), None),
        what="a notification for a create that moved the surface row",
    )
    assert frame["method"] == OFFICE_RESYNC_METHOD
    assert frame["params"] == {"workspace_id": WORKSPACE, "reason": "full_core"}
