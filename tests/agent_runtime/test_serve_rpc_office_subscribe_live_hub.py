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


def _subscribe(
    sent: list,
    *,
    connection_key: str = "c1",
    rid: str = "r1",
    fold_entities: list[str] | None = None,
) -> dict:
    """``fold_entities=None`` sends NO param at all — the un-updated client.

    Absence and an explicit empty list are different declarations all the way
    down (see ``normalize_office_fold_entities``), so this helper must not be
    able to express one as the other. No caller here needs the explicit-empty
    case; the fake-hub file owns it.
    """

    params: dict = {"workspace_id": WORKSPACE}
    if fold_entities is not None:
        params["fold_entities"] = fold_entities
    return serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "runtime.office.subscribe",
            "params": params,
        },
        serve_rpc.RpcContext(
            connection_key=connection_key, transport="socket", emit=sent.append
        ),
    )


def _unsubscribe(*, connection_key: str = "c1", rid: str = "u1") -> dict:
    """The METHOD, not ``release`` — and the distinction is what the tests below
    are about. ``release`` is the disconnect sweep and takes everything a
    departing connection owns; this is a live client handing ONE workspace back
    over a socket it is still using for everything else."""

    return serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "runtime.office.unsubscribe",
            "params": {"workspace_id": WORKSPACE},
        },
        serve_rpc.RpcContext(
            connection_key=connection_key, transport="socket", emit=lambda _f: None
        ),
    )


def _await_patch(sent: list, what: str) -> dict:
    return _wait_for(
        lambda: next(
            (item for item in sent if item["method"] == OFFICE_PATCH_METHOD), None
        ),
        what=what,
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


def test_a_declared_lifecycle_token_promotes_a_real_CREATE_end_to_end(live_hub):
    """O-H1 + O-H2, against the producer ``serve.py`` actually builds.

    The milestone's early half, and the first time this lane carries a gesture
    rather than a drag. A create's batch is ``[office_actor created-upsert,
    office.actor.upserted]`` — the domain event was already covered, so once the
    subscriber's own declaration reaches the producer the batch is coverable and
    the whole gesture ships as one sub-4 KB frame instead of two 822 KB cores.

    Nothing is pinned: the fold set is DERIVED per producer generation from the
    office registry, so this exercises the whole chain — the RPC param, the
    per-key declaration, the room's intersection, the coverage gate, the
    producer's promotion decision, and the sink. Passing a fixed
    ``fold_entities`` here would assert about everything downstream of the one
    seam O-H2 changed and prove nothing about it.

    The paired half is the test below: the SAME create against a subscriber that
    declares nothing must still resync, or this is green because the gate does
    not exist.
    """

    store = _seed_office()
    hub = live_hub()
    sent: list[dict] = []
    result = _settled_subscription(
        sent,
        hub,
        fold_entities=sorted(OFFICE_FOLD_ENTITIES | {"office_actor_lifecycle"}),
    )
    assert "office_actor_lifecycle" in result["fold_entities"]

    new_actor = "personainst_backend_dev_11223344"
    store.upsert_actor(
        WORKSPACE, _actor_payload(new_actor, position=(7.0, 8.0)), updated_by="live-create"
    )

    frame = _wait_for(
        lambda: next((item for item in sent if item.get("method")), None),
        what="any notification at all for a real CREATE",
    )
    assert frame["method"] == OFFICE_PATCH_METHOD, (
        "a declared-lifecycle subscriber got "
        f"{frame['method']} / {frame['params'].get('reason')} instead of a patch"
    )
    row = next(r for r in frame["params"]["patches"] if r["id"].endswith(new_actor))
    assert row["op"] == "upsert"
    assert row["created"] is True
    # The complete row, which is what makes the client's insert-on-absent sound.
    assert row["changed"]["items"][0]["position"] == [7.0, 8.0]


def test_the_same_create_still_resyncs_a_subscriber_that_declares_nothing(live_hub):
    """The anti-vacuity half, and the mixed-pair guarantee stated as a test.

    An un-updated launcher declares nothing, gets the fail-open legacy constant,
    and the create's ``created: true`` upsert is therefore NOT coverable for it —
    so the batch demotes to a full core and this lane says ``resync``, which is
    exactly what it does today. No regression, no simultaneous deploy.

    Kill-mutation: remove the ``office_actor_lifecycle`` gate from
    ``patch_coverage`` — this goes red with a patch notification, i.e. with the
    very promotion V4 says would cost a fielded client a re-hydrate on top.
    """

    store = _seed_office()
    hub = live_hub()
    sent: list[dict] = []
    _settled_subscription(sent, hub)

    store.upsert_actor(
        WORKSPACE,
        _actor_payload("personainst_backend_dev_55667788", position=(7.0, 8.0)),
        updated_by="live-create",
    )

    frame = _wait_for(
        lambda: next((item for item in sent if item.get("method")), None),
        what="any notification at all for a real CREATE",
    )
    assert frame["method"] == OFFICE_RESYNC_METHOD, frame
    assert frame["params"]["reason"] == "full_core"


def test_unrelated_runtime_activity_no_longer_resyncs_this_lane(live_hub):
    """O-H4 against a REAL producer, which is the only place it can be shown.

    The fake-hub file proves the scoping rule on hand-built frames. What only a
    real producer adds is that an unrelated write genuinely produces an
    uncovered ``delta`` — a full core — that genuinely reaches this sink, and is
    genuinely skipped there rather than never being offered. Before O-H4 this
    exact sequence answered with ``runtime.office.resync``, and each of those
    cost a re-subscribe whose producer restart billed every other subscriber a
    fresh core.

    A board write is used because it is uncovered for reasons that have nothing
    to do with any office — so a scoping bug that keyed on "was this batch
    coverable" instead of "did it touch my workspace" would still resync here.

    The absence is asserted only AFTER the frame reached the sink; measured
    before the pump ran it would be an assertion about thread timing and would
    pass against a sink that dropped everything.
    """

    from agent_runtime.board_store import BoardStore

    _seed_office()
    hub = live_hub(fold_entities=OFFICE_FOLD_ENTITIES)
    sent: list[dict] = []
    _settled_subscription(sent, hub)
    delivered = _frames_delivered(hub)

    BoardStore().add_card(workspace_id="ws_live_hub_board_noise", title="unrelated work")

    _wait_for(
        lambda: _frames_delivered(hub) > delivered,
        what="the unrelated batch's frame to reach this subscriber's sink",
    )
    assert sent == [], f"unrelated runtime activity resynced the office lane: {sent}"


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

    # This asserted `already_subscribed` when it was written. That refusal is
    # gone — a repeat subscribe now RE-BASELINES — so the failure mode moved
    # rather than disappearing: a replacement that added its key without the
    # teardown pruning the old one would leave TWO declarations for one live
    # subscription, and the accepted set would be widened on behalf of a
    # subscriber the hub does not have. Same defect, different route to it.
    duplicate = _subscribe([], rid="r2")
    assert duplicate["result"]["replaced"] is True
    assert len(OFFICE_SUBSCRIPTIONS.declarations()) == 1, (
        "a re-baselined subscribe left a second declaration behind: the "
        "accepted set is now widened for a subscriber the hub does not have"
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
    assert registered.registered is False
    # The drain race has its own reason since the re-baseline landed: folding it
    # into `push_lane_unavailable` would tell a client that CAN reconnect that
    # this runtime does not push at all.
    assert registered.reason == "push_lane_draining"
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


# ── claim 6: a stuck subscription can be reclaimed ──────────────────────────


def test_a_re_subscribe_hands_back_a_genuinely_fresher_baseline(live_hub):
    """The whole reason re-baselining replaces ``already_subscribed``.

    A client that refused an unusable baseline needs the SECOND answer to be a
    better one, not an acknowledgement that it is still registered. So the
    watermark is asserted to have MOVED across an intervening write, and the
    projection to carry the moved coordinate — not merely that a reply came
    back. A handler that returned a cached or re-wrapped first answer would
    satisfy "no error" and every shape assertion in the fake-hub file, and would
    leave the client exactly as stuck as the refusal did.

    Only a real event log can say this: the fake hub has no offsets of its own,
    so ``event_offset`` there is whatever ``events_watermark`` happened to read
    and cannot be shown to advance for the right reason.
    """

    store = _seed_office()
    hub = live_hub(fold_entities=OFFICE_FOLD_ENTITIES)
    sent: list[dict] = []
    first = _settled_subscription(sent, hub)
    assert first["replaced"] is False

    store.upsert_actor(
        WORKSPACE, _actor_payload(position=(42.0, 43.0)), updated_by="between-subscribes"
    )
    # The write must be IN the log before the second baseline is taken, or the
    # comparison below would be measuring a race rather than a re-derivation.
    _await_patch(sent, "the intervening write, so the re-subscribe follows it")

    second = _subscribe(sent, rid="r2")
    assert "error" not in second, second
    second = second["result"]

    assert second["replaced"] is True
    assert second["watermark"]["event_offset"] > first["watermark"]["event_offset"]
    positions = [item["position"] for item in second["items"]]
    assert positions == [[42.0, 43.0]], positions
    assert [item["position"] for item in first["items"]] == [[1.0, 2.0]]


def test_the_re_baselined_sink_is_the_live_one_and_the_old_sink_is_dead(live_hub):
    """Replacement means the NEW sink carries the lane, at the NEW baseline.

    Two ways this goes wrong and both are silent. If the old registration
    survived, the client is fed by a sink still holding the FIRST baseline
    offset, which would re-deliver frames the new baseline already contains — a
    duplicate fold. If the teardown ran but the re-registration did not, the
    client holds a fresh baseline and hears nothing ever again, which is the
    worst outcome available: a subscriber that believes it is current.

    Proven by delivery through the new registration after the replace, plus a
    subscriber count that never grew — a leak per retry is exactly what the
    retired refusal existed to prevent, and replacement has to prevent it too.
    """

    store = _seed_office()
    hub = live_hub(fold_entities=OFFICE_FOLD_ENTITIES)
    sent: list[dict] = []
    _settled_subscription(sent, hub)

    assert "error" not in _subscribe(sent, rid="r2")
    assert hub.subscriber_count() == 1
    assert hub.has(office_subscription_key("c1", WORKSPACE))
    _wait_for(
        lambda: _frames_delivered(hub) >= 1,
        what="the re-baselining hydrate to reach the replacement sink",
    )
    del sent[:]

    store.upsert_actor(
        WORKSPACE, _actor_payload(position=(51.0, 52.0)), updated_by="after-rebaseline"
    )

    frame = _await_patch(sent, "a patch through the REPLACEMENT sink")
    assert frame["params"]["patches"][0]["changed"]["items"][0]["position"] == [
        51.0,
        52.0,
    ]


def test_a_re_baseline_makes_every_other_subscriber_pay_a_fresh_core(live_hub):
    """The cost, charged where it actually lands — on the OTHER client.

    ``StreamHub.subscribe`` restarts the producer on purpose, so a re-baseline
    is not a private transaction between one connection and the runtime: every
    subscriber attached to that hub is handed a fresh full core it did not ask
    for. Under the retired refusal a redundant subscribe cost nothing at all,
    because the hub declined the duplicate key before bumping a generation. This
    is the receipt's justification, and it is asserted rather than described so
    that removing the log line or the ``replaced`` flag has to argue with a test.

    Measured on the peer's own delivery count rather than on ``generation``
    alone: a generation bump proves a thread was started, while the peer's count
    proves the peer was actually made to re-read a projection.

    SCOPE, since O-H5: this is the DEGRADED path, and it is still worth pinning
    because it is the one a runtime with no bound ``accepted_fold_entities``
    probe takes — ``live_hub`` deliberately binds none here. When serve.py's
    probe IS bound and the rejoin does not narrow the room, the restart (and
    therefore this cost) is gone; that is the test immediately below, and the
    two together are what say the saving is conditional rather than universal.
    """

    _seed_office()
    hub = live_hub(fold_entities=OFFICE_FOLD_ENTITIES)
    mine: list[dict] = []
    peer: list[dict] = []
    _settled_subscription(mine, hub)
    _settled_subscription(peer, hub, connection_key="c2")

    generation = hub.stats()["generation"]
    peer_delivered = _frames_delivered(hub, "c2")

    assert _subscribe(mine, rid="r2")["result"]["replaced"] is True

    assert hub.stats()["generation"] > generation
    _wait_for(
        lambda: _frames_delivered(hub, "c2") > peer_delivered,
        what="the peer subscriber to be handed a core it did not ask for",
    )
    # Paid, but not CONFUSED: the peer's own baseline gate swallows the core, so
    # nothing crosses to its client. A resync here would mean the re-baseline
    # had also kicked every other client into a refetch round trip.
    assert _methods(peer) == [], peer


def test_a_non_narrowing_re_baseline_costs_the_room_nothing(live_hub):
    """O-H5 against a REAL producer: the second ~822 KB build is gone.

    The office lane's re-subscribe restarted the shared producer to manufacture
    a hydrate its own sink then discarded at the baseline the subscribe REPLY
    had just carried. Every re-baseline therefore billed every other subscriber
    a fresh full core for a frame nobody could use — and the operator's session
    shows re-baselines happening on a backoff ladder, so the cost repeated.

    With serve.py's ``accepted_fold_entities`` bound (mirrored here, as the
    ``live_hub`` fixture mirrors ``_stream_source``) a rejoin that declares a
    superset of the set in force attaches to the running generation instead.

    The peer's silence is the real assertion and it is synchronised, not
    guessed: the re-baselining client's own patch is awaited first, which proves
    the lane is live and the producer really did keep running, and only then is
    the peer's un-moved delivery count an observation.
    """

    store = _seed_office()
    hub = live_hub()
    OFFICE_SUBSCRIPTIONS.bind(
        lambda: hub,
        accepted_fold_entities=lambda: accepted_fold_entities(
            OFFICE_SUBSCRIPTIONS.declarations()
        ),
    )
    declared = sorted(OFFICE_FOLD_ENTITIES)
    mine: list[dict] = []
    peer: list[dict] = []
    _settled_subscription(mine, hub, fold_entities=declared)
    _settled_subscription(peer, hub, connection_key="c2", fold_entities=declared)

    generation = hub.stats()["generation"]
    peer_delivered = _frames_delivered(hub, "c2")

    reply = _subscribe(mine, rid="r3", fold_entities=declared)["result"]
    assert reply["replaced"] is True

    # No supersession: the running producer kept producing.
    assert hub.stats()["generation"] == generation, (
        "a non-narrowing re-baseline restarted the producer and billed the room"
    )

    # And the lane really is live afterwards — otherwise "no restart" would be
    # trivially satisfied by a subscription that receives nothing.
    store.upsert_actor(
        WORKSPACE, _actor_payload(position=(71.0, 72.0)), updated_by="after-rebaseline"
    )
    _await_patch(mine, "the re-baselined subscriber's own patch")
    _await_patch(peer, "the peer's patch, which is what makes the count readable")

    # The peer received the office write and NOTHING ELSE: no fresh core, no
    # resync. Its delivery count moved by the write's frames alone.
    assert _methods(peer) == [OFFICE_PATCH_METHOD], peer
    assert _frames_delivered(hub, "c2") > peer_delivered


def test_unsubscribe_stops_delivery_against_the_real_producer(live_hub):
    """The claim only a live hub can make: the pushes genuinely STOP.

    The absence is synchronised on a PEER, never on a sleep. With the departing
    subscriber gone its own delivery counter can no longer advance, so there is
    nothing on its side left to wait for; a second connection stays attached,
    the write is awaited on ITS lane, and only then is the released client's
    silence an observation rather than a guess about thread scheduling.

    Asserted against the hub's key set as well as the notification list, because
    a registry that pruned only its own index would leave the hub fanning frames
    into a sink whose connection has stopped reading — a producer kept alive for
    nobody, which is the leak this method exists to make impossible.
    """

    store = _seed_office()
    hub = live_hub(fold_entities=OFFICE_FOLD_ENTITIES)
    mine: list[dict] = []
    peer: list[dict] = []
    _settled_subscription(mine, hub)
    _settled_subscription(peer, hub, connection_key="c2")

    assert _unsubscribe()["result"] == {"workspace_id": WORKSPACE, "released": True}
    assert hub.has(office_subscription_key("c1", WORKSPACE)) is False
    assert hub.has(office_subscription_key("c2", WORKSPACE)) is True
    assert hub.subscriber_count() == 1
    del mine[:]

    store.upsert_actor(
        WORKSPACE, _actor_payload(position=(61.0, 62.0)), updated_by="after-unsubscribe"
    )

    _await_patch(peer, "the peer's patch, which is what makes the silence readable")
    assert mine == [], f"a released subscriber was still being pushed to: {mine}"


def test_unsubscribe_then_subscribe_starts_a_lane_that_actually_carries(live_hub):
    """Release is not a one-way door, and the second subscribe is a FIRST one.

    ``replaced: false`` is the load-bearing half. Had unsubscribe pruned only
    the registry's index and left the hub holding the key, the re-subscribe
    would report ``replaced: true`` — the leak surfacing as a receipt, and the
    one way this pair can be wrong while each half looks right on its own. The
    delivery afterwards is the other half: a lane that reports itself restored
    and then carries nothing is the failure this whole workstream exists to end.
    """

    store = _seed_office()
    hub = live_hub(fold_entities=OFFICE_FOLD_ENTITIES)
    sent: list[dict] = []
    _settled_subscription(sent, hub)

    assert _unsubscribe()["result"]["released"] is True
    assert hub.subscriber_count() == 0

    again = _subscribe(sent, rid="r2")
    assert "error" not in again, again
    assert again["result"]["replaced"] is False
    _wait_for(
        lambda: _frames_delivered(hub) >= 1,
        what="the re-join hydrate to reach the new sink",
    )
    del sent[:]

    store.upsert_actor(
        WORKSPACE, _actor_payload(position=(71.0, 72.0)), updated_by="after-rejoin"
    )

    frame = _await_patch(sent, "a patch on the re-joined lane")
    assert frame["params"]["patches"][0]["changed"]["items"][0]["position"] == [
        71.0,
        72.0,
    ]


def test_releasing_an_unknown_subscription_is_a_clean_no_op_on_the_real_hub(live_hub):
    """A recovering client releases what it is not sure it holds.

    Two shapes, both answered ``released: false`` and neither an error: a
    connection that never subscribed, and one releasing a second time. Against a
    real hub this also has to be shown INERT — the fake shares the registry's
    bookkeeping and cannot fail this, whereas a real ``unsubscribe`` reaching the
    wrong key would take a live subscriber off the lane and stop the producer
    for everyone. So a peer subscription is held throughout and checked
    afterwards, which is the assertion a fake could not make.
    """

    _seed_office()
    hub = live_hub(fold_entities=OFFICE_FOLD_ENTITIES)
    peer: list[dict] = []
    _settled_subscription(peer, hub, connection_key="c2")

    never = _unsubscribe(connection_key="c_never_here")
    assert "error" not in never, never
    assert never["result"] == {"workspace_id": WORKSPACE, "released": False}

    mine: list[dict] = []
    _settled_subscription(mine, hub)
    assert _unsubscribe()["result"]["released"] is True
    twice = _unsubscribe(rid="u2")
    assert "error" not in twice, twice
    assert twice["result"]["released"] is False

    assert hub.has(office_subscription_key("c2", WORKSPACE)) is True
    assert hub.subscriber_count() == 1
    assert hub.stats()["producer_running"] is True


def test_the_disconnect_sweep_still_works_after_a_re_baseline(live_hub):
    """The index has to survive replacement, and the count is how you tell.

    A replace that added the key without removing the old one would leave
    ``_owned`` holding a duplicate that the hub cannot honour — the sweep would
    claim two teardowns and perform one. Because ``_owned`` stores a SET the
    duplicate cannot show up as a length, so it is the sweep's own return value
    and the hub's emptiness afterwards that pin it.
    """

    _seed_office()
    hub = live_hub(fold_entities=OFFICE_FOLD_ENTITIES)
    sent: list[dict] = []
    _settled_subscription(sent, hub)
    assert _subscribe(sent, rid="r2")["result"]["replaced"] is True
    assert _subscribe(sent, rid="r3")["result"]["replaced"] is True

    assert OFFICE_SUBSCRIPTIONS.release("c1") == 1

    assert hub.has(office_subscription_key("c1", WORKSPACE)) is False
    assert hub.subscriber_count() == 0
    assert OFFICE_SUBSCRIPTIONS.owned_keys("c1") == set()
    _wait_for(
        lambda: hub.stats()["producers_live"] == 0,
        what="the producer to stop once the re-baselined subscriber left",
    )


def test_the_disconnect_sweep_still_works_after_an_explicit_unsubscribe(live_hub):
    """``_release_subscription`` runs on EVERY disconnect, including the tidy
    ones.

    A client that released its workspace and then closed the socket must leave
    the sweep with nothing to do and no way to raise. Zero is the honest count
    — asserting 1 would mean the index had kept a key the hub no longer held —
    and the peer's survival is what proves the sweep did not go looking for
    something else to release when it found its own set empty.
    """

    _seed_office()
    hub = live_hub(fold_entities=OFFICE_FOLD_ENTITIES)
    mine: list[dict] = []
    peer: list[dict] = []
    _settled_subscription(mine, hub)
    _settled_subscription(peer, hub, connection_key="c2")

    assert _unsubscribe()["result"]["released"] is True

    assert OFFICE_SUBSCRIPTIONS.release("c1") == 0
    assert hub.has(office_subscription_key("c2", WORKSPACE)) is True
    assert hub.subscriber_count() == 1
    assert OFFICE_SUBSCRIPTIONS.release("c2") == 1
    assert hub.subscriber_count() == 0
