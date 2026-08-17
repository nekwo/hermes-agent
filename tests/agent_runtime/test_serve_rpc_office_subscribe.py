"""``runtime.office.subscribe`` and the re-envelope sink behind it.

Operator ruling 2026-08-15, option (A): the office push lane RE-ENVELOPES the
patch batches ``stream.patch_batch_frame`` already produces rather than tailing
the event log a second time. So what needs pinning is not "does a patch reach a
client" — the derivation is the one already under test in ``test_stream_patch``
— but the four decisions the adapter makes on top of it, each of which fails
SILENTLY when wrong:

1. **addressed, not broadcast.** A batch that moved another workspace's actors
   must send NOTHING, not an empty envelope the client opens and discards. A
   test that only checks the happy path passes against a sink with no filter at
   all.
2. **the baseline rule.** One offset comparison absorbs both the hub's
   mandatory re-hydrate on subscribe and the reply-lands-after-push ordering.
   Get it backwards and the client's first push is a duplicate of its baseline.
3. **an unforwardable change becomes a RESYNC, never silence.** A full core
   means the batch was not patch-coverable; staying quiet there is precisely
   the "client believes it is current" failure the whole lane exists to end.
   The unknown-frame-type branch takes the same exit on purpose.
4. **teardown.** The office keys are namespaced away from the stream lane's
   bare connection key, so a disconnect that swept only the latter would leak a
   subscriber — and a leaked subscriber keeps a producer alive for nobody.
5. **reclaim.** Subscribe registers and answers in ONE call, so the
   subscription outlives the client's decision about the reply that created it.
   A client that refuses an unusable baseline used to be stuck with a live
   subscription it would never fold against, and no method existed that could
   release it. Two answers close that: a repeat subscribe RE-BASELINES rather
   than refusing, and ``runtime.office.unsubscribe`` hands one back without
   dropping the connection. What is pinned HERE is the shape half — the
   ``replaced`` receipt, the ``released`` boolean, which typed reason a refusal
   carries, what the index holds afterwards. The behaviour half (a genuinely
   FRESHER baseline, delivery actually stopping) is pinned in the live-hub file,
   because a fake hub agrees with the registry's own bookkeeping and so cannot
   contradict it.

The sink is tested against hand-built frames rather than a live hub: these are
FILTERING rules, and an integration test that happened to produce no frame
would look identical to a filter that drops everything.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_runtime import serve_rpc
from agent_runtime.serve_office_subscriptions import (
    OFFICE_FOLD_ENTITIES,
    OFFICE_PATCH_METHOD,
    OFFICE_RESYNC_METHOD,
    OFFICE_SUBSCRIPTIONS,
    normalize_office_fold_entities,
    office_patch_sink,
    office_subscription_key,
)

WORKSPACE = "ws_rpc_subscribe_test"
OTHER = "ws_somebody_else"

#: "the param was not sent at all", which the fail-open rule must keep
#: distinguishable from ``[]`` ("I fold nothing"). A default of ``None`` would
#: have collapsed the two at the test boundary — the exact conflation the
#: production normalizer is written to avoid.
_ABSENT = object()


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry is PROCESS-GLOBAL, like the method table beside it.

    Unbinding after each test is not tidiness: a factory left bound would hand
    the next test a hub from a serve loop that has already closed, and the
    failure would surface in an unrelated file.
    """

    OFFICE_SUBSCRIPTIONS.bind(None)
    yield
    OFFICE_SUBSCRIPTIONS.bind(None)


class _FakeHub:
    """Just enough StreamHub to answer subscribe/unsubscribe truthfully.

    Deliberately NOT a mock that records calls: the behaviours that matter are
    the REFUSAL of a duplicate key and returning False from `unsubscribe` for a
    key it never held, and a permissive mock would let a bug in either pass.
    The duplicate refusal is still modelled even though the office lane no
    longer relies on it — it is what forces the re-baselining path to actually
    tear the old key down rather than write over it, which is the difference
    between a replacement and a silent second registration.

    ``draining`` models the one remaining way a real ``StreamHub.subscribe``
    answers False: its stop event is set. That is a serve-loop shutdown race,
    unreachable from a test that owns its own hub, and it is the state the old
    ``already_subscribed`` arm mislabelled.

    ``restart_producer`` and ``generation`` model O-H5's half of the contract,
    and they are modelled rather than ignored for the reason the duplicate
    refusal is: the whole point of the flag is that a join can be made NOT to
    bump the generation, so a fake that accepted the argument and always bumped
    anyway would make the cheap path indistinguishable from the expensive one.
    ``producer_running`` follows the real hub's meaning — a producer exists once
    a first subscriber has arrived, and stops when the room empties.
    """

    def __init__(self, *, draining: bool = False) -> None:
        self.sinks: dict[str, object] = {}
        self.drops: dict[str, object] = {}
        self.draining = draining
        #: Every key ever handed to `subscribe`, in order, kept ONLY so a test
        #: can tell a replacement from a no-op: both leave one live key behind.
        self.subscribe_calls: list[str] = []
        self.generation = 0
        self.producer_running = False
        #: What each `subscribe` was ASKED for, so a test can separate the
        #: request from what the hub did with it.
        self.restart_requests: list[bool] = []

    def subscribe(self, key, *, sink, on_drop=None, restart_producer: bool = True) -> bool:
        self.subscribe_calls.append(key)
        self.restart_requests.append(restart_producer)
        if self.draining or key in self.sinks:
            return False
        self.sinks[key] = sink
        self.drops[key] = on_drop
        # The hub's own FLOOR: a subscriber attached to nothing receives
        # nothing, so a restart-free join with no live producer starts one
        # anyway.
        if restart_producer or not self.producer_running:
            self.generation += 1
            self.producer_running = True
        return True

    def unsubscribe(self, key) -> bool:
        self.drops.pop(key, None)
        removed = self.sinks.pop(key, None) is not None
        if not self.sinks:
            self.producer_running = False
        return removed

    def stats(self) -> dict:
        return {"generation": self.generation, "producer_running": self.producer_running}


def _patch_frame(*, base_offset: int, event_offset: int, ids: list[str]) -> dict:
    """A frame shaped exactly like ``stream.patch_batch_frame``'s output."""

    return {
        "type": "patch",
        "schema_version": 1,
        "base_offset": base_offset,
        "watermark": {"event_offset": event_offset, "last_event_ts": "t"},
        "patches": [
            {
                "seq": event_offset,
                "ts": "t",
                "entity": "office_actor",
                "id": entity_id,
                "op": "upsert",
                "changed": {"actor_key": entity_id.split("/", 1)[-1]},
            }
            for entity_id in ids
        ],
        "coalesced_count": len(ids),
    }


#: The cross-repo goldens the launcher mirrors byte-for-byte. Read here so the
#: folder-only frame these tests deliver cannot drift from the wire shape the
#: producer actually builds (``test_stream_patch`` pins that half).
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "stream_frames"


def _surface_golden_row() -> dict:
    """The one ``office_surface`` row out of ``patch_office_surface.json``.

    Asserts the property that made task #57 reachable rather than assuming it:
    this entity addresses the office row ITSELF, so its id is the BARE workspace
    id with no ``/`` — which is what the sink's old ``office_actor``-plus-slash
    predicate failed BOTH halves of.
    """

    golden = json.loads(
        (_FIXTURES / "patch_office_surface.json").read_text(encoding="utf-8")
    )
    ((row,)) = golden["patches"]
    assert row["entity"] == "office_surface"
    assert "/" not in row["id"]
    return row


def _surface_patch_frame(*, base_offset: int, event_offset: int, workspace_id: str) -> dict:
    """A FOLDER-ONLY patch frame: one ``office_surface`` row, no actor row.

    The reachable shape is every folder rename with no actor write in the same
    ~450 ms coalescing window — a mixed batch was always forwarded whole (any
    actor row admits the frame), which is why the drop survived so much testing.
    """

    row = _surface_golden_row()
    return {
        "type": "patch",
        "schema_version": 2,
        "base_offset": base_offset,
        "watermark": {"event_offset": event_offset, "last_event_ts": "t"},
        "patches": [{**row, "id": workspace_id, "seq": event_offset}],
        "coalesced_count": 1,
    }


def _sink(baseline: int = 10):
    sent: list[dict] = []
    return sent, office_patch_sink(
        workspace_id=WORKSPACE, baseline_offset=baseline, emit=sent.append
    )


def _seed_office(workspace_id: str = WORKSPACE) -> None:
    from agent_runtime.office_store import OfficeStore

    store = OfficeStore()
    store.ensure_surface(workspace_id, created_by="seed")
    store.upsert_actor(
        workspace_id,
        {
            "persona_id": "qa",
            "persona_instance_id": "personainst_qa_agent_9c8a382f",
            "items": [
                {
                    "item_id": "personainst_qa_agent_9c8a382f",
                    "kind": "agent",
                    "persona_id": "qa",
                    "position": [1.0, 2.0],
                    "scale": 1.0,
                }
            ],
        },
        updated_by="seed",
    )


def _subscribe(
    rid="r1",
    workspace_id=WORKSPACE,
    context=None,
    fold_entities=_ABSENT,
    reason=_ABSENT,
) -> dict:
    params: dict = {"workspace_id": workspace_id}
    if fold_entities is not _ABSENT:
        params["fold_entities"] = fold_entities
    if reason is not _ABSENT:
        params["reason"] = reason
    return serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "runtime.office.subscribe",
            "params": params,
        },
        context,
    )


def _unsubscribe(rid="u1", workspace_id=WORKSPACE, context=None) -> dict:
    return serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "runtime.office.unsubscribe",
            "params": {"workspace_id": workspace_id},
        },
        context,
    )


# ── the sink: what crosses ──────────────────────────────────────────────────


def test_a_batch_for_this_workspace_becomes_one_office_patch_notification():
    sent, deliver = _sink()

    deliver(
        _patch_frame(
            base_offset=10,
            event_offset=12,
            ids=[f"{WORKSPACE}/personainst_qa_agent_9c8a382f"],
        )
    )

    assert len(sent) == 1
    assert sent[0]["method"] == OFFICE_PATCH_METHOD
    assert "id" not in sent[0]
    params = sent[0]["params"]
    assert params["workspace_id"] == WORKSPACE
    assert params["base_offset"] == 10
    assert params["watermark"]["event_offset"] == 12
    # The rows cross UNCHANGED — same {seq, ts, entity, id, op, changed} the
    # legacy frame carried, so the client's fold is byte-identical work on
    # either lane. That is what makes the frames deletable rather than merely
    # deprecated.
    assert params["patches"] == _patch_frame(
        base_offset=10,
        event_offset=12,
        ids=[f"{WORKSPACE}/personainst_qa_agent_9c8a382f"],
    )["patches"]


def test_another_workspaces_batch_sends_nothing_at_all():
    """Addressed, not broadcast — and asserted as SILENCE, not as an empty
    payload. An envelope with zero rows would still wake the client, and would
    advance nothing."""

    sent, deliver = _sink()

    deliver(
        _patch_frame(
            base_offset=10, event_offset=12, ids=[f"{OTHER}/personainst_dev_3ebfce41"]
        )
    )

    assert sent == []


def test_a_mixed_batch_in_scope_is_forwarded_WHOLE_not_filtered():
    """SPEC INVERSION (office fold-promotion plan §V6, 2026-08-16).

    This asserted the opposite until 2026-08-16 — that a mixed batch carried
    only this workspace's rows — on the reasoning that another workspace's
    actors are not this subscriber's business. What that reasoning missed is the
    WATERMARK: the notification is stamped with the FULL batch's watermark while
    carrying a filtered subset of its rows, and the launcher folds both
    transports into ONE read model with ONE sequence.

    So the delete gesture's batch — ``[persona_instance remove, office_actor
    remove]`` — forwarded only the office row while claiming the whole span. If
    this lane folded first, the stream lane's frame at the same watermark was
    dropped as stale and the persona remove NEVER APPLIED: a silently stale
    roster, unrecoverable by any gate, because the watermark says the span was
    applied. Latent only because nothing has ever promoted (``folded 0`` on
    every subscribe); it arms the moment O-H3 lands, which is why this stage is
    sequenced before it rather than after.

    The scope test is now per FRAME (is anything here mine?) and the payload is
    the whole batch. Addressed-not-broadcast survives intact — see the sibling
    test below, where nothing in scope still sends nothing.
    """

    sent, deliver = _sink()
    ids = [
        f"{OTHER}/personainst_dev_3ebfce41",
        f"{WORKSPACE}/personainst_qa_agent_9c8a382f",
        f"{OTHER}/personainst_neko_f6f7a51b",
    ]

    deliver(_patch_frame(base_offset=10, event_offset=12, ids=ids))

    assert [row["id"] for row in sent[0]["params"]["patches"]] == ids
    # The watermark it claims and the rows it carries now describe the same
    # span, which is the whole property.
    assert sent[0]["params"]["watermark"]["event_offset"] == 12
    assert sent[0]["params"]["base_offset"] == 10


def test_a_non_office_row_rides_along_when_the_batch_is_in_scope():
    """The V6 race one entity over.

    A ``persona_instance`` row in a batch this lane forwards is real state at
    the watermark it stamps. Dropping it while claiming the watermark is exactly
    the same bug as dropping another workspace's office row, and it is the
    version that actually bit: the delete gesture's batch is a persona remove
    beside an office remove.

    Kill-mutation: restore the entity filter on the forwarded rows — the persona
    row disappears from ``patches`` and this goes red while the office-only
    assertions above stay green.
    """

    sent, deliver = _sink()
    frame = _patch_frame(
        base_offset=10,
        event_offset=12,
        ids=[f"{WORKSPACE}/personainst_qa_agent_9c8a382f", "personainst_qa_agent_9c8a382f"],
    )
    frame["patches"][1]["entity"] = "persona_instance"
    frame["patches"][1]["op"] = "remove"
    frame["patches"][1].pop("changed")

    deliver(frame)

    rows = sent[0]["params"]["patches"]
    assert [row["entity"] for row in rows] == ["office_actor", "persona_instance"]
    assert rows[1]["op"] == "remove"


def test_a_workspace_that_merely_shares_a_prefix_is_not_matched():
    """``office_actor_patch_id`` joins on ``/`` precisely so this cannot
    collide. Pinned because a naive ``startswith(workspace_id)`` would forward
    ``ws_rpc_subscribe_test_2``'s actors into ``ws_rpc_subscribe_test``."""

    sent, deliver = _sink()

    deliver(
        _patch_frame(
            base_offset=10, event_offset=12, ids=[f"{WORKSPACE}_2/personainst_qa_x"]
        )
    )

    assert sent == []


def test_a_folder_only_patch_frame_is_forwarded_not_dropped():
    """RD-H1 / task #57 — the silent-gap class, closed.

    ``update_surface`` emits an ``office_surface`` row whose id is the BARE
    workspace id. The sink's scope test was a private restatement of the id
    scheme that knew only ``office_actor`` and a slash-prefixed id, so a
    folder-only frame failed both conjuncts and took the bare ``return``: no
    patch AND no resync, on a lane whose own docstring says a resync is
    recoverable and a dropped change is not. It was masked because the argv
    ``harness stream`` child still folded the same batch for the launcher.

    Kill-mutation: restore the ``office_actor``-only predicate. The mutant emits
    NOTHING, so it cannot satisfy any probe below — the probes are all on the
    CONTENT of a message it never constructs. A second-order mutant that emitted
    a RESYNC instead fails them too: a resync carries a different method and no
    ``patches`` at all.
    """

    sent, deliver = _sink()

    deliver(_surface_patch_frame(base_offset=10, event_offset=12, workspace_id=WORKSPACE))

    ((message,)) = sent
    assert message["method"] == OFFICE_PATCH_METHOD
    ((row,)) = message["params"]["patches"]
    assert row["entity"] == "office_surface"
    assert row["id"] == WORKSPACE
    # The folder list crosses verbatim, and the expected value comes from the
    # committed cross-repo golden rather than from this file.
    assert row["changed"]["folders"] == _surface_golden_row()["changed"]["folders"]
    assert message["params"]["watermark"]["event_offset"] == 12
    assert message["params"]["base_offset"] == 10


def test_another_workspaces_folder_change_sends_nothing():
    """The other driven value — the pair is what pins the id COMPARISON.

    Neither a drop-all predicate nor a forward-all one passes both this and the
    test above: the first sends nothing where a folder change was ours, the
    second sends something where it was not.

    Kill-mutation: scope every ``office_surface`` row in regardless of its id.
    """

    sent, deliver = _sink()

    deliver(_surface_patch_frame(base_offset=10, event_offset=12, workspace_id=OTHER))

    assert sent == []


def test_a_non_office_entity_never_crosses_this_lane():
    """The office lane forwards office rows. A ``persona_instance`` patch in the
    same batch is another surface's business."""

    sent, deliver = _sink()
    frame = _patch_frame(
        base_offset=10, event_offset=12, ids=[f"{WORKSPACE}/personainst_qa_agent"]
    )
    frame["patches"][0]["entity"] = "persona_instance"

    deliver(frame)

    assert sent == []


# ── the sink: the baseline rule ─────────────────────────────────────────────


@pytest.mark.parametrize("event_offset", [1, 9, 10])
def test_a_frame_at_or_below_the_baseline_is_dropped(event_offset):
    """ONE comparison, TWO seams — the hub's mandatory re-hydrate on subscribe,
    and the dispatcher emitting the subscribe reply after the handler returns.
    The boundary is inclusive: a frame whose watermark EQUALS the baseline
    offset contains nothing the baseline did not already carry."""

    sent, deliver = _sink(baseline=10)

    deliver(
        _patch_frame(
            base_offset=0,
            event_offset=event_offset,
            ids=[f"{WORKSPACE}/personainst_qa_agent_9c8a382f"],
        )
    )

    assert sent == []


def test_the_first_frame_past_the_baseline_does_cross():
    """The other half of the boundary. Without this, a sink that dropped
    everything would satisfy every drop-test above."""

    sent, deliver = _sink(baseline=10)

    deliver(
        _patch_frame(
            base_offset=10,
            event_offset=11,
            ids=[f"{WORKSPACE}/personainst_qa_agent_9c8a382f"],
        )
    )

    assert len(sent) == 1


# ── the sink: what cannot be expressed as a patch ───────────────────────────


@pytest.mark.parametrize("frame_type", ["hydrate", "delta"])
def test_a_full_core_frame_becomes_a_resync_rather_than_silence(frame_type):
    """A full core means ``batch_is_patch_coverable`` said no — an uncovered
    office event, a restore, a surface edit. There is no honest office patch to
    forward, and silence would leave the client believing it is current. That is
    the exact failure class this lane exists to delete.

    Neither frame here carries an ``events`` list, so both take the conservative
    arm of O-H4's scoping: a batch this lane cannot enumerate is one it cannot
    prove irrelevant. The scoped cases are the three tests below.
    """

    sent, deliver = _sink()

    deliver({"type": frame_type, "watermark": {"event_offset": 99}})

    assert sent[0]["method"] == OFFICE_RESYNC_METHOD
    assert sent[0]["params"] == {"workspace_id": WORKSPACE, "reason": "full_core"}


# ── O-H4: the resync is scoped to batches that touched this workspace ────────


def _delta_frame(*events: dict, event_offset: int = 99) -> dict:
    """A coalesced delta shaped like ``stream.delta_batch_frame``'s output.

    Only the two fields the scoping reads are populated — the real frame also
    carries an 822 KB ``core``, which is the whole point: this lane never wanted
    it and now does not pay a resync for it either.
    """

    return {
        "type": "delta",
        "watermark": {"event_offset": event_offset},
        "events": [{"event": event} for event in events],
        "coalesced_count": len(events),
    }


def test_a_delta_carrying_nothing_for_this_workspace_does_not_resync():
    """The cost O-H4 removes.

    An agent turn or a board write demoted its own batch for reasons that have
    nothing to do with any office, and this lane used to answer each one with a
    full re-subscribe — which restarts the shared producer and bills every other
    subscriber a fresh core. The operator's session shows the backoff ladder
    being walked by exactly this.

    Kill-mutation: restore the unconditional resync.
    """

    sent, deliver = _sink()

    deliver(
        _delta_frame(
            {"type": "persona_chat.projected", "payload": {"persona_instance_id": "p1"}},
            {"type": "board.card.moved", "payload": {"board_id": "b1"}},
        )
    )

    assert sent == []


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(
            {"type": "office.actor.restored", "payload": {"workspace_id": WORKSPACE}},
            id="an uncovered office domain event for this workspace",
        ),
        pytest.param(
            {
                "type": "state.patched",
                "payload": {
                    "entity": "office_actor",
                    "id": f"{WORKSPACE}/personainst_qa_agent_9c8a382f",
                    "op": "upsert",
                },
            },
            id="an office patch that rode an uncoverable batch",
        ),
        pytest.param(
            {
                "type": "state.patched",
                "payload": {"entity": "office_surface", "id": WORKSPACE, "op": "upsert"},
            },
            id="a folder patch that rode an uncoverable batch (RD-H1's twin)",
        ),
    ],
)
def test_a_delta_that_did_touch_this_workspace_still_resyncs(event):
    """Anti-vacuity, and the two ways a demoted batch can still be ours.

    The first is the obvious one: an office write this lane cannot express.
    The second is subtler and is why the scoping reads BOTH lists — an office
    ``state.patched`` can ride a batch that some OTHER event demoted, so the
    actor row moved and no patch frame is coming for it. Scoping on domain
    events alone would silently drop that, which is the one outcome worse than
    an unnecessary resync.

    The third case is RD-H1's twin: the same row one ENTITY over. This arm read
    ``office_actor`` and its slash-prefixed id only, so a bare-id
    ``office_surface`` row failed it — saved in practice by the ``office.*`` arm
    catching the paired domain event, which is a neighbour's accident and not an
    invariant. Both arms now derive scope from ``office_patch_scope``.

    Kill-mutation: over-scope the skip (drop either arm of the check); or, for
    the third case, restore the ``office_actor``-only scope in the patched arm.
    """

    sent, deliver = _sink()

    deliver(_delta_frame(event))

    assert sent[0]["method"] == OFFICE_RESYNC_METHOD
    assert sent[0]["params"]["reason"] == "full_core"


@pytest.mark.parametrize(
    "events",
    [
        pytest.param([{"type": "office.actor.removed", "payload": {"workspace_id": OTHER}}],
                     id="another workspace's office write"),
        pytest.param(
            [
                {
                    "type": "state.patched",
                    "payload": {"entity": "office_actor", "id": f"{OTHER}/x", "op": "remove"},
                }
            ],
            id="another workspace's office patch",
        ),
        pytest.param(
            [
                {
                    "type": "state.patched",
                    "payload": {"entity": "office_surface", "id": OTHER, "op": "upsert"},
                }
            ],
            id="another workspace's folder patch",
        ),
        pytest.param(
            [
                {
                    "type": "state.patched",
                    "payload": {"entity": "persona_instance", "id": "p1", "op": "remove"},
                }
            ],
            id="a persona patch that named no office",
        ),
    ],
)
def test_another_workspaces_uncovered_batch_is_not_this_subscribers_business(events):
    """Scoping is per WORKSPACE, not merely per topic.

    Two offices on one runtime are two independent canvases, and the prefix
    match is the same one the patch path uses — so a workspace whose id merely
    shares a prefix cannot smuggle a resync either (its own test lives above).
    """

    sent, deliver = _sink()

    deliver(_delta_frame(*events))

    assert sent == []


@pytest.mark.parametrize(
    "frame",
    [
        pytest.param({"type": "delta", "watermark": {"event_offset": 99}}, id="no events list"),
        pytest.param(
            {"type": "delta", "watermark": {"event_offset": 99}, "events": "not-a-list"},
            id="an unreadable events value",
        ),
        pytest.param(
            {"type": "delta", "watermark": {"event_offset": 99}, "events": [{"event": None}]},
            id="an entry with no event block",
        ),
        pytest.param(
            {
                "type": "delta",
                "watermark": {"event_offset": 99},
                "events": [{"event": {"type": "office.actor.removed", "payload": {}}}],
            },
            id="an office event that does not name its workspace",
        ),
    ],
)
def test_a_delta_this_lane_cannot_enumerate_still_resyncs(frame):
    """ANTI-VACUITY: the conservative arm must be REACHABLE.

    A scoping rule whose "I cannot tell" branch is dead is a scoping rule that
    will one day skip a frame it should have resynced, and no test above would
    notice. Each case here is a real shape — an older producer with no
    ``events``, a malformed entry, and the one that is genuinely load-bearing:
    an office event carrying no ``workspace_id`` is UNPLACEABLE, and unplaceable
    must take the same conservative arm as an unknown frame type rather than
    being read as "not mine".

    Kill-mutation: make ``_delta_touches_workspace`` return False instead of
    None for any of these.
    """

    sent, deliver = _sink()

    deliver(frame)

    assert sent[0]["method"] == OFFICE_RESYNC_METHOD
    assert sent[0]["params"]["reason"] == "full_core"


def test_a_hydrate_is_never_scoped_away():
    """A hydrate says "here is everything" and enumerates nothing, so its
    relevance is not decidable — upstream's ``full`` bit. Pinned separately from
    the delta cases because the asymmetry between the two frame types IS the
    rule, and a scoping that leaked onto hydrates would drop the one frame a
    late joiner cannot do without."""

    sent, deliver = _sink()

    deliver(
        {
            "type": "hydrate",
            "watermark": {"event_offset": 99},
            # Even WITH an enumerable-looking list that names nothing of ours.
            "events": [{"event": {"type": "board.card.moved", "payload": {}}}],
        }
    )

    assert sent[0]["method"] == OFFICE_RESYNC_METHOD


def test_an_unknown_frame_type_also_resyncs_and_says_so():
    """Conservative on purpose: a frame type this module has not been taught is
    by definition a change it cannot express as a patch. A resync is
    recoverable; a dropped change is not. The distinct ``reason`` keeps it from
    being mistaken for a coverage degrade in the logs."""

    sent, deliver = _sink()

    deliver({"type": "something_new_landed", "watermark": {"event_offset": 99}})

    assert sent[0]["params"]["reason"] == "unknown_frame_type"


def test_a_heartbeat_is_neither_a_patch_nor_a_resync():
    """It carries no state. Forwarding it as a resync would make an idle
    runtime refetch the office every five seconds."""

    sent, deliver = _sink()

    deliver({"type": "heartbeat", "watermark": {"event_offset": 99}})

    assert sent == []


# ── the method ──────────────────────────────────────────────────────────────


def test_a_caller_with_no_push_channel_is_refused_rather_than_registered():
    """The whole reason ``RpcContext.emit`` is allowed to be None. Registering
    a channel-less caller would report success into a void — the shape of
    failure this program keeps finding."""

    _seed_office()
    OFFICE_SUBSCRIPTIONS.bind(lambda: _FakeHub())

    frame = _subscribe(context=serve_rpc.RpcContext(connection_key="c1"))

    assert frame["error"]["data"]["reason"] == "push_channel_unavailable"


def test_a_runtime_with_no_push_lane_says_so_typed():
    """No hub bound means no socket lane — a runtime configuration fact, not
    something the client can retry its way out of. Distinct from
    ``already_subscribed`` because the cures differ."""

    _seed_office()
    sent: list[dict] = []

    frame = _subscribe(
        context=serve_rpc.RpcContext(connection_key="c1", emit=sent.append)
    )

    assert frame["error"]["data"]["reason"] == "push_lane_unavailable"
    assert sent == []


# ── EG-1.1 / RD-H2: the baseline is read, never invented ────────────────────


def _record_baselines(monkeypatch) -> list[int]:
    """Every ``baseline_offset`` the registry actually registered a sink with.

    The fake hub cannot answer this on its own — the baseline is baked into the
    sink closure, not passed to ``subscribe`` — and the baseline is the whole
    subject here, so the recorder wraps the REAL ``office_patch_sink`` rather
    than replacing it. Test-owned by construction: the production code has to
    call it to register anything at all.
    """

    from agent_runtime import serve_office_subscriptions as subs

    seen: list[int] = []
    real = subs.office_patch_sink

    def _recording(*, workspace_id, baseline_offset, emit):
        seen.append(baseline_offset)
        return real(workspace_id=workspace_id, baseline_offset=baseline_offset, emit=emit)

    monkeypatch.setattr(subs, "office_patch_sink", _recording)
    return seen


def test_an_unreadable_watermark_refuses_typed_and_registers_nothing(monkeypatch):
    """``int(… or 0)`` minted the one value the watermark module calls maximally
    damaging, out of the one input it documents as routine on this platform.

    ``events_watermark`` answers ``{"event_offset": None, "event_offset_error":
    ...}`` when the log cannot be stat'ed — AV scanning, a share violation — and
    the old expression turned that into 0 with no exception raised and the error
    string dropped. A subscription baselined at 0 has no baseline gate (the
    sink's test is ``<= baseline``), so the hub's mandatory post-subscribe
    hydrate came back to the client as a resync; the client re-subscribed; the
    producer restarted; the next hydrate resynced it again — a full core per lap
    for every subscriber in the room.

    **Anti-vacuity.** Restoring ``or 0`` is the mutation, and TWO independent
    witnesses convict it: the mutant's reply is a SUCCESS, which cannot carry a
    refusal reason at all, and it registers a sink, which the fake hub's own
    call record shows. Either assertion alone kills it.
    """

    _seed_office()
    hub = _FakeHub()
    lines: list[dict] = []
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub, log=lines.append)
    baselines = _record_baselines(monkeypatch)
    monkeypatch.setattr(
        "agent_runtime.parity.events_watermark",
        lambda **_kw: {"event_offset": None, "event_offset_error": "OSError"},
    )

    frame = _subscribe(
        context=serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)
    )

    assert frame["error"]["data"]["reason"] == "baseline_unavailable"
    assert frame["error"]["data"]["workspace_id"] == WORKSPACE
    # Nothing was registered — not by this call and not by the registry: no key
    # was ever offered to the hub, so there is no leaked subscriber keeping a
    # producer alive for a client that was refused.
    assert hub.subscribe_calls == []
    assert hub.sinks == {}
    assert baselines == []
    # The discarded half, now attributable: the class, not the message.
    assert [line["event"] for line in lines] == ["serve_office_subscribe_refused"]
    assert lines[0]["error"] == "OSError"


def test_a_readable_watermark_of_literally_zero_from_an_empty_log_still_subscribes(
    monkeypatch,
):
    """The discriminator. An empty log is a REAL position, and 0 is its name.

    Without this test the cheapest way to pass the one above is to refuse every
    zero — which would break the first subscribe against a freshly initialised
    runtime, i.e. exactly the case a new operator meets first. What separates the
    honest zero from the fabricated one is not the number: it is whether
    ``event_offset`` was present and readable at all.

    *Probed field:* one registration, at baseline 0 — read off the sink the
    registry actually built, not off the reply, so a handler that echoed 0 into
    the reply while registering something else could not pass.
    """

    _seed_office()
    hub = _FakeHub()
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub)
    baselines = _record_baselines(monkeypatch)
    monkeypatch.setattr(
        "agent_runtime.parity.events_watermark",
        lambda **_kw: {"event_offset": 0},
    )

    frame = _subscribe(
        context=serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)
    )

    assert "error" not in frame
    assert baselines == [0]
    assert len(hub.subscribe_calls) == 1
    assert frame["result"]["watermark"]["event_offset"] == 0


# ── EG-1.5 / RD-H4: the baseline counts what it could not read ──────────────


def test_the_subscribe_baseline_carries_the_unreadable_count_get_reports():
    """THE chokepoint witness — a second, different reader of one projection.

    ``actors_unreadable`` is only worth having if the subscribe BASELINE carries
    it: a client folds patches onto the body this reply hands it, so a count that
    reached ``runtime.office.get`` and not this reply would tell the launcher its
    office was complete on exactly the lane where it matters most.

    Both readers already share ``_office_projection``, and that is the property
    under test. This test cannot pass if the fix forked the chokepoint —
    patching ``get``'s path and leaving the baseline alone is the mutation, and
    the assertion is equality between the two replies' fields, not two separate
    literals that could drift apart with the code.
    """

    from agent_runtime import paths

    _seed_office()
    (paths.office_actors_dir(WORKSPACE) / "broken.json").write_text(
        "{not json", encoding="utf-8"
    )
    hub = _FakeHub()
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub)

    subscribed = _subscribe(
        context=serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)
    )["result"]
    got = serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "r2",
            "method": "runtime.office.get",
            "params": {"workspace_id": WORKSPACE},
        }
    )["result"]

    assert subscribed["actors_unreadable"] == 1
    assert subscribed["actors_unreadable"] == got["actors_unreadable"]
    # And the readable actor still crossed: counting the loss must not become
    # refusing the projection.
    assert [item["item_id"] for item in subscribed["items"]] == [
        "personainst_qa_agent_9c8a382f"
    ]


def test_an_unknown_workspace_is_refused_before_anything_is_registered():
    """Same 4001 ``runtime.office.get`` gives, and for the same reason. Asserted
    together with an empty hub: a subscribe that registered first and validated
    second would leak a subscription per typo."""

    hub = _FakeHub()
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub)

    frame = _subscribe(
        workspace_id="ws_never_authored",
        context=serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None),
    )

    assert frame["error"]["code"] == serve_rpc.ERR_NOT_FOUND
    assert frame["error"]["data"]["reason"] == "workspace_not_found"
    assert hub.sinks == {}


def test_the_reply_carries_the_get_projection_plus_the_baseline_watermark():
    """The baseline and the offset it was read at, in ONE call.

    Asserted against ``runtime.office.get``'s own result rather than field by
    field: a subscribe whose baseline could disagree with a get would put the
    client back in the two-readers-of-one-truth state this lane exists to end.
    """

    _seed_office()
    hub = _FakeHub()
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub)

    subscribed = _subscribe(
        context=serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)
    )["result"]
    got = serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "r2",
            "method": "runtime.office.get",
            "params": {"workspace_id": WORKSPACE},
        }
    )["result"]

    watermark = subscribed.pop("watermark")
    # ``replaced`` is the OTHER key subscribe adds on top of the get body — the
    # re-baselining receipt. Popped here for the same reason the watermark is:
    # what this test is about is that everything ELSE is byte-identical to the
    # get, so the two derivations cannot drift.
    replaced = subscribed.pop("replaced")
    # Third additive key (2026-08-16): the accepted fold declaration. Popped for
    # the same reason — it is subscribe's own, not part of the projection — and
    # asserted below, because an echo nobody checks is an echo that can go
    # silently wrong.
    declared = subscribed.pop("fold_entities")
    assert subscribed == got
    assert isinstance(watermark["event_offset"], int)
    assert replaced is False
    # This client declared nothing, so it is held to the fail-open legacy
    # constant — which is what an un-updated launcher must keep getting.
    assert declared == sorted(OFFICE_FOLD_ENTITIES)
    assert set(hub.sinks) == {office_subscription_key("c1", WORKSPACE)}


def test_a_second_subscribe_on_one_connection_re_baselines_instead_of_refusing():
    """The gap this change exists to close, at its narrowest.

    Subscribe registers and answers in one call, so the subscription is live
    before the client has read the reply. A client that finds the baseline
    unusable — truncated, an unreadable watermark, a workspace id it did not ask
    for — is RIGHT to refuse it, and the old ``already_subscribed`` answer then
    left it holding a subscription it would never fold against with no way to
    reclaim it short of dropping the connection. Asking again is the natural
    move, and it now works.

    The refusal is asserted GONE by its code as well as its reason: an error
    frame carrying some other 4090 would satisfy a reason-only check.
    """

    _seed_office()
    # ONE hub, held across both calls — `_ensure_stream_hub` memoizes under a
    # lock, so a factory minting a fresh hub per call would be testing a
    # runtime that does not exist (and would hide the duplicate entirely).
    hub = _FakeHub()
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub)
    context = serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)

    first = _subscribe(context=context)
    second = _subscribe(rid="r2", context=context)

    assert "error" not in second, second
    assert first["result"]["replaced"] is False
    assert second["result"]["replaced"] is True
    # The projection came back whole on the retry, not a bare acknowledgement:
    # the point of re-subscribing is to get a baseline, and an answer that
    # merely said "yes, still subscribed" would leave the client exactly as
    # stuck as the refusal did.
    assert second["result"]["items"] == first["result"]["items"]
    assert isinstance(second["result"]["watermark"]["event_offset"], int)


def test_a_re_baseline_leaves_exactly_one_subscription_and_it_is_the_new_sink():
    """Replacement, not a second registration and not a no-op.

    Both failure modes leave one live key behind and are invisible from the
    key set alone: a hub that silently accepted a duplicate would leak a
    subscriber per retry (the leak ``already_subscribed`` existed to prevent),
    and a handler that skipped the hub entirely would report a fresh baseline
    while the client kept being fed by the STALE sink — the worse of the two,
    because the sink still carries the old baseline offset and would re-deliver
    frames the new baseline already contains.

    So the old sink is caught by identity: it must no longer be the one the hub
    holds, and the hub must have been asked twice.
    """

    _seed_office()
    hub = _FakeHub()
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub)
    context = serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)
    key = office_subscription_key("c1", WORKSPACE)

    _subscribe(context=context)
    first_sink = hub.sinks[key]
    _subscribe(rid="r2", context=context)

    assert set(hub.sinks) == {key}
    assert hub.sinks[key] is not first_sink
    assert hub.subscribe_calls == [key, key]
    assert OFFICE_SUBSCRIPTIONS.owned_keys("c1") == {key}


def test_a_refused_outcome_is_falsy_so_the_natural_guard_cannot_invert():
    """``SubscribeOutcome`` replaced a bare ``bool``, and that is a trap.

    Every dataclass instance is truthy by default, so the guard a caller reaches
    for without thinking — ``if not outcome:`` — would be a branch that can
    never be taken, reporting every refusal as a success. Today's handler asks
    ``outcome.registered`` and so never exercises ``__bool__`` at all, which is
    exactly why the guard needs a test of its own rather than incidental
    coverage: an untested safety rail is one that quietly stops being a rail.
    """

    from agent_runtime.serve_office_subscriptions import (
        NO_PUSH_LANE,
        PUSH_LANE_DRAINING,
        SubscribeOutcome,
    )

    assert not SubscribeOutcome(False, reason=NO_PUSH_LANE)
    assert not SubscribeOutcome(False, replaced=True, reason=PUSH_LANE_DRAINING)
    assert SubscribeOutcome(True)
    assert SubscribeOutcome(True, replaced=True)
    # A refusal never carries a null reason: the handler puts this straight on
    # the wire, and an unnamed refusal is the mislabel this type exists to end.
    assert SubscribeOutcome(True).reason is None


def test_a_re_baseline_is_billed_to_the_service_log_not_only_to_the_client():
    """The cost has to be visible to the party that pays for it.

    ``StreamHub.subscribe`` restarts the producer, so a re-baseline makes every
    OTHER subscriber on that hub rebuild a full core. Under the old refusal a
    redundant subscribe cost nothing at all — the duplicate key was declined
    before a generation was ever bumped — so this is a real new cost on the path
    a confused client takes repeatedly. The client learns of it from
    ``replaced``; the operator has no reply to read, and without this line a
    retry loop would surface only as an unexplained climb in the hub's
    generation counter.

    The FIRST subscribe must write nothing: a line per subscribe would bury the
    signal in the ordinary case, which is the same as not having it.
    """

    _seed_office()
    hub = _FakeHub()
    lines: list[dict] = []
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub, log=lines.append)
    context = serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)

    _subscribe(context=context)
    assert lines == []

    _subscribe(rid="r2", context=context)

    assert len(lines) == 1
    assert lines[0]["event"] == "serve_office_subscription_rebaselined"
    assert lines[0]["workspace_id"] == WORKSPACE
    assert lines[0]["connection"] == "c1"
    assert lines[0]["key"] == office_subscription_key("c1", WORKSPACE)
    # Named rather than left to be inferred from the hub's stats — this is the
    # cost the line exists to report. TRUE here because no
    # ``accepted_fold_entities`` probe was bound, so the lane cannot establish
    # that the rejoin is non-narrowing and takes the expensive-and-correct arm
    # (O-H5's degrade rule). The cheap arm has its own tests below.
    assert lines[0]["producer_restarted"] is True


# ── O-H5: a non-narrowing rejoin does not bill the room for a fresh core ─────


def test_a_non_narrowing_rejoin_attaches_instead_of_restarting_the_producer():
    """The second ~822 KB build per re-baseline, removed.

    ``StreamHub.subscribe`` restarts by contract so a late joiner opens on a
    hydrate. This lane does not want that hydrate — the subscribe REPLY carried
    the baseline and the sink provably discards everything at or behind it — so
    every re-baseline manufactured a full core to throw away and billed every
    other subscriber in the room for it.

    A rejoin that declares a SUPERSET of the accepted set in force cannot narrow
    what the room may promote, so the running producer is still promoting only
    rows everyone (including this joiner) can fold. It attaches.

    Kill-mutation: always restart — the generation moves and the receipt says
    so.
    """

    _seed_office()
    hub = _FakeHub()
    lines: list[dict] = []
    OFFICE_SUBSCRIPTIONS.bind(
        lambda: hub,
        log=lines.append,
        accepted_fold_entities=lambda: OFFICE_FOLD_ENTITIES,
    )
    context = serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)

    _subscribe(context=context, fold_entities=sorted(OFFICE_FOLD_ENTITIES))
    # Anti-vacuity: the counter really does move — the FIRST subscriber has no
    # producer to attach to, so it starts one (the hub's floor).
    assert hub.generation == 1
    assert hub.restart_requests[-1] is True

    # A SECOND connection, so the teardown below does not empty the room — an
    # emptied room stops the producer and the hub's floor restarts it, which is
    # correct but is not the case under test. It also shows the saving is NOT
    # limited to rejoins: a genuinely new office subscriber does not want the
    # hydrate either, because its baseline rode its own subscribe reply.
    _subscribe(
        rid="r2",
        context=serve_rpc.RpcContext(connection_key="c2", emit=lambda _f: None),
        fold_entities=sorted(OFFICE_FOLD_ENTITIES),
    )
    settled_generation = hub.generation
    assert settled_generation == 1, "a non-narrowing second join billed the room a core"

    # c1 re-baselines with the same declaration: no narrowing.
    _subscribe(rid="r3", context=context, fold_entities=sorted(OFFICE_FOLD_ENTITIES))

    assert hub.generation == settled_generation, (
        "a non-narrowing rejoin bumped the generation and billed the room a core"
    )
    assert hub.restart_requests[-1] is False
    rebaselines = [line for line in lines if line["connection"] == "c1"]
    assert rebaselines[-1]["producer_restarted"] is False


def test_a_narrowing_rejoin_still_restarts_the_producer():
    """The safety half, and it is not symmetry for its own sake.

    A producer built against a wider accepted set is promoting rows this joiner
    cannot fold. Attaching to it would hand those rows to a client whose fold
    answers with a re-hydrate — the patch AND the core, which is the regression
    the whole negotiation exists to prevent. Only a restart re-derives the
    accepted set, so a narrowing join must pay for one.

    Kill-mutation: never restart (drop the superset test) — this goes red.
    """

    from agent_runtime.patch_coverage import HISTORICAL_FOLD_ENTITIES

    _seed_office()
    hub = _FakeHub()
    lines: list[dict] = []
    OFFICE_SUBSCRIPTIONS.bind(
        lambda: hub,
        log=lines.append,
        accepted_fold_entities=lambda: OFFICE_FOLD_ENTITIES,
    )
    context = serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)

    _subscribe(context=context, fold_entities=sorted(OFFICE_FOLD_ENTITIES))
    _subscribe(
        rid="r2",
        context=serve_rpc.RpcContext(connection_key="c2", emit=lambda _f: None),
        fold_entities=sorted(OFFICE_FOLD_ENTITIES),
    )
    settled_generation = hub.generation

    # c1 comes back declaring LESS than the set in force.
    _subscribe(rid="r3", context=context, fold_entities=sorted(HISTORICAL_FOLD_ENTITIES))

    assert hub.generation > settled_generation
    assert hub.restart_requests[-1] is True
    assert [line for line in lines if line["connection"] == "c1"][-1][
        "producer_restarted"
    ] is True


def test_a_first_subscriber_always_gets_a_producer():
    """The floor, asserted from this side too.

    A restart-free join with no live producer would attach a subscriber to
    nothing, and nothing is exactly what it would then receive — the silent
    failure this lane has already paid for once. The hub owns the floor; this
    pins that the office registry cannot defeat it, even when its own
    superset test says a restart is unnecessary.
    """

    _seed_office()
    hub = _FakeHub()
    OFFICE_SUBSCRIPTIONS.bind(
        lambda: hub, accepted_fold_entities=lambda: OFFICE_FOLD_ENTITIES
    )

    _subscribe(
        context=serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None),
        fold_entities=sorted(OFFICE_FOLD_ENTITIES),
    )

    assert hub.producer_running is True
    assert hub.generation == 1


def test_a_rejoin_that_emptied_the_room_reports_the_restart_it_actually_caused():
    """The receipt reports what HAPPENED, not what was asked for.

    A lone subscriber's re-baseline tears its own key down first, which empties
    the room and stops the producer; the hub's floor then starts a fresh one
    however the call was flagged. Reporting the REQUEST would print
    ``producer_restarted: false`` for a join that restarted — a cost nobody is
    billed for, which is the one shape this lane's receipts exist to prevent.

    Kill-mutation: log the requested flag instead of the observed generation
    delta.
    """

    _seed_office()
    hub = _FakeHub()
    lines: list[dict] = []
    OFFICE_SUBSCRIPTIONS.bind(
        lambda: hub,
        log=lines.append,
        accepted_fold_entities=lambda: OFFICE_FOLD_ENTITIES,
    )
    context = serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)

    _subscribe(context=context, fold_entities=sorted(OFFICE_FOLD_ENTITIES))
    _subscribe(rid="r2", context=context, fold_entities=sorted(OFFICE_FOLD_ENTITIES))

    # It ASKED not to restart — the declaration is a superset and a producer was
    # live when the question was put ...
    assert hub.restart_requests[-1] is False
    # ... but the teardown emptied the room, so one really did start.
    assert lines[-1]["producer_restarted"] is True


def test_a_failing_accepted_probe_falls_back_to_restarting():
    """A missing or broken probe degrades to correct-and-expensive.

    The whole cheap path rests on knowing what the room accepts. If that cannot
    be read — no serve loop bound one, or the derivation raised — the honest
    answer is the restart this stage exists to avoid, never a guess. Both
    absence and failure are pinned, because they arrive by different routes and
    only one of them looks like a bug.
    """

    _seed_office()

    def _explode():
        raise RuntimeError("declaration table is mid-swap")

    for probe in (None, _explode):
        hub = _FakeHub()
        OFFICE_SUBSCRIPTIONS.bind(lambda: hub, accepted_fold_entities=probe)
        context = serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)
        _subscribe(context=context, fold_entities=sorted(OFFICE_FOLD_ENTITIES))
        _subscribe(
            rid="r2",
            context=serve_rpc.RpcContext(connection_key="c2", emit=lambda _f: None),
            fold_entities=sorted(OFFICE_FOLD_ENTITIES),
        )
        generation = hub.generation
        _subscribe(rid="r3", context=context, fold_entities=sorted(OFFICE_FOLD_ENTITIES))
        assert hub.restart_requests[-1] is True, probe
        assert hub.generation > generation, probe
        OFFICE_SUBSCRIPTIONS.bind(None)


def test_unbinding_drops_the_log_so_a_stopped_loops_sink_is_never_written_to():
    """The registry outlives any one serve loop; its log must not.

    ``_close_socket_lane`` unbinds on the way down, and the next serve_loop in
    the same process binds its own ``_service_log``. A log left behind would
    have the new loop's re-baselines written into the old loop's sink — which in
    a test suite is a closed stream and in production is a shutdown path's
    buffer nobody reads."""

    _seed_office()
    hub = _FakeHub()
    lines: list[dict] = []
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub, log=lines.append)
    OFFICE_SUBSCRIPTIONS.bind(None)
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub)
    context = serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)

    _subscribe(context=context)
    _subscribe(rid="r2", context=context)

    assert lines == []


def test_a_subscribe_racing_the_drain_is_typed_draining_and_not_no_lane():
    """The mislabel that removing ``already_subscribed`` would have inherited.

    ``StreamHub.subscribe`` also answers False once its stop event is set, and
    the old branch derived its reason by asking ``bound()`` — which is True for
    a draining hub exactly as for a live one. A client racing
    ``_close_socket_lane`` was therefore told "already subscribed to this
    workspace" while holding nothing, and pointed at the one cure (stop
    retrying) that cannot work.

    Collapsing that arm into ``push_lane_unavailable`` would be nearly as wrong:
    that reason means this runtime does not push at all, and a client that
    believes it will not reconnect. The two are separate names because the cures
    are separate, and the ``prior_subscription_released`` flag is asserted False
    here so the True case below is a real distinction rather than a constant.
    """

    _seed_office()
    OFFICE_SUBSCRIPTIONS.bind(lambda: _FakeHub(draining=True))

    frame = _subscribe(
        context=serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)
    )

    assert frame["error"]["data"]["reason"] == "push_lane_draining"
    assert frame["error"]["data"]["prior_subscription_released"] is False
    assert frame["error"]["data"]["workspace_id"] == WORKSPACE


def test_a_re_baseline_that_loses_the_drain_race_admits_it_took_the_old_lane():
    """The re-baseline tears down BEFORE it registers, and it cannot not.

    The hub refuses a duplicate key, so there is no ordering in which the new
    sink is installed before the old one is gone. If the hub then refuses the
    re-registration because it is stopping, the client's previous subscription
    has already been destroyed — and a refusal that said only "draining" would
    leave it believing its old lane survived, waiting on pushes that will never
    come.

    The index must lose the key too. A key kept there that the hub does not hold
    would have the disconnect sweep report a teardown it never performed, which
    is the accounting error the whole registry exists to make impossible.
    """

    _seed_office()
    hub = _FakeHub()
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub)
    context = serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)
    assert "result" in _subscribe(context=context)

    hub.draining = True
    frame = _subscribe(rid="r2", context=context)

    assert frame["error"]["data"]["reason"] == "push_lane_draining"
    assert frame["error"]["data"]["prior_subscription_released"] is True
    assert hub.sinks == {}
    assert OFFICE_SUBSCRIPTIONS.owned_keys("c1") == set()
    assert OFFICE_SUBSCRIPTIONS.release("c1") == 0


def test_a_re_baseline_is_per_connection_and_never_displaces_another_client():
    """The key is per connection per workspace, and replacement respects that.

    A replace keyed on the workspace alone would take the OTHER operator's
    launcher off the lane — and it would go quiet with no error to show for it,
    because a hub unsubscribe is silent by design. The two-connection case is
    already pinned for a first subscribe; this pins it for the new path, which
    is the one that actively tears a key down.
    """

    _seed_office()
    hub = _FakeHub()
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub)
    for key in ("c1", "c2"):
        _subscribe(context=serve_rpc.RpcContext(connection_key=key, emit=lambda _f: None))
    other_sink = hub.sinks[office_subscription_key("c2", WORKSPACE)]

    _subscribe(
        rid="r3", context=serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)
    )

    assert set(hub.sinks) == {
        office_subscription_key("c1", WORKSPACE),
        office_subscription_key("c2", WORKSPACE),
    }
    assert hub.sinks[office_subscription_key("c2", WORKSPACE)] is other_sink


# ── the method: giving a subscription back ──────────────────────────────────


def test_unsubscribe_releases_this_connections_subscription_and_says_so():
    """The other half of reclaim: a client that gives up, without a disconnect.

    Before this method the only way to stop a push lane was to close the socket
    — which also took every unrelated call riding it. Asserted against the HUB's
    key set rather than the registry's index, because the registry pruning its
    own dictionary while the hub kept the subscription is precisely the leak
    that keeps a producer rebuilding projections for nobody.
    """

    _seed_office()
    hub = _FakeHub()
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub)
    context = serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)
    _subscribe(context=context)

    frame = _unsubscribe(context=context)

    assert frame["result"] == {"workspace_id": WORKSPACE, "released": True}
    assert hub.sinks == {}
    assert OFFICE_SUBSCRIPTIONS.owned_keys("c1") == set()


def test_unsubscribing_something_never_subscribed_is_a_typed_no_op_not_an_error():
    """The recovering client's case, and the reason this is a result and not a
    4001.

    A client that lost track of whether its subscribe landed releases what it is
    not sure it holds — that is what recovery looks like. An error there would
    make ordinary recovery indistinguishable from a fault, and a client that
    cannot tell those apart either logs noise forever or stops looking at the
    channel that would have told it something real.

    Both no-op shapes are pinned: never-subscribed, and released twice.
    """

    _seed_office()
    hub = _FakeHub()
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub)
    context = serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)

    fresh = _unsubscribe(context=context)
    assert fresh["result"] == {"workspace_id": WORKSPACE, "released": False}

    _subscribe(context=context)
    assert _unsubscribe(rid="u2", context=context)["result"]["released"] is True
    again = _unsubscribe(rid="u3", context=context)

    assert "error" not in again
    assert again["result"] == {"workspace_id": WORKSPACE, "released": False}


def test_unsubscribe_needs_no_push_channel_and_no_bound_hub():
    """Neither is a reason to refuse, and both would strand a subscription.

    Subscribe demands an emitter because it REGISTERS one; unsubscribe only
    names a key. And a runtime with no hub bound holds no subscription for this
    caller either — which ``released: false`` already says truthfully. Refusing
    on either would mean a client that reconnected into a drained runtime could
    never tidy up after itself.
    """

    _seed_office()

    no_hub = _unsubscribe(context=serve_rpc.RpcContext(connection_key="c1"))
    assert no_hub["result"] == {"workspace_id": WORKSPACE, "released": False}

    OFFICE_SUBSCRIPTIONS.bind(lambda: _FakeHub())
    no_channel = _unsubscribe(
        rid="u2", context=serve_rpc.RpcContext(connection_key="c1")
    )
    assert no_channel["result"] == {"workspace_id": WORKSPACE, "released": False}


def test_unsubscribe_still_refuses_a_missing_workspace_id():
    """Tolerance has a floor. Every other refusal this method retires is about a
    state the client can legitimately be IN; a missing ``workspace_id`` names no
    key at all, so there is nothing to answer False about and answering False
    anyway would swallow a caller bug."""

    OFFICE_SUBSCRIPTIONS.bind(lambda: _FakeHub())

    for params in ({}, {"workspace_id": "  "}, {"workspace_id": 7}):
        frame = serve_rpc.handle_request(
            {
                "jsonrpc": "2.0",
                "id": "u1",
                "method": "runtime.office.unsubscribe",
                "params": params,
            },
            serve_rpc.RpcContext(connection_key="c1"),
        )
        assert frame["error"]["code"] == serve_rpc.ERR_INVALID_PARAMS, params
        assert frame["error"]["data"]["reason"] == "workspace_id_required", params


def test_unsubscribe_asks_the_HUB_even_when_the_index_has_forgotten_the_key():
    """The index is a cache of the hub's truth, and divergence resolves the
    hub's way.

    This is not hypothetical. ``bind(None)`` clears ``_owned`` and deliberately
    does NOT unsubscribe anything — the drain unbinds first so a subscribe
    racing it is refused — so a hub that outlives one bind/rebind cycle holds
    keys the index has never heard of. That is the orphan shape: a live
    subscription nobody is indexed as owning, keeping a producer rebuilding
    projections for a client that may well have gone.

    A ``release_one`` that consulted the index BEFORE the hub would short-circuit
    to ``released: false`` and leave the orphan attached forever — and it would
    look correct in every other test here, because in the ordinary case the two
    tables agree. Only a divergence can tell them apart.
    """

    _seed_office()
    hub = _FakeHub()
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub)
    context = serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)
    _subscribe(context=context)
    key = office_subscription_key("c1", WORKSPACE)

    # A drain and a fresh loop over the SAME hub: the index is emptied, the
    # hub's subscription is not.
    OFFICE_SUBSCRIPTIONS.bind(None)
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub)
    assert OFFICE_SUBSCRIPTIONS.owned_keys("c1") == set()
    assert set(hub.sinks) == {key}

    frame = _unsubscribe(context=context)

    assert frame["result"]["released"] is True
    assert hub.sinks == {}


def test_unsubscribe_touches_only_this_connection_and_only_this_workspace():
    """Two axes, one test, because the key joins them and a bug in either
    direction is silent: a release keyed on the workspace alone would take the
    other operator's launcher off the lane, and one keyed on the connection
    alone would drop every workspace that connection watches."""

    _seed_office()
    _seed_office(OTHER)
    hub = _FakeHub()
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub)
    for connection in ("c1", "c2"):
        for workspace in (WORKSPACE, OTHER):
            _subscribe(
                workspace_id=workspace,
                context=serve_rpc.RpcContext(connection_key=connection, emit=lambda _f: None),
            )

    _unsubscribe(context=serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None))

    assert set(hub.sinks) == {
        office_subscription_key("c1", OTHER),
        office_subscription_key("c2", WORKSPACE),
        office_subscription_key("c2", OTHER),
    }
    assert OFFICE_SUBSCRIPTIONS.owned_keys("c1") == {
        office_subscription_key("c1", OTHER)
    }


def test_unsubscribe_then_subscribe_gets_a_fresh_registration_not_a_replace():
    """A released subscription is genuinely gone, so the next subscribe is a
    FIRST one.

    ``replaced: false`` is the assertion that matters here. If unsubscribe had
    pruned only the registry index and left the hub's key in place, the
    re-subscribe would report ``replaced: true`` — which is the leak showing up
    as a receipt, and the one way this pair can be wrong while both halves
    individually look right."""

    _seed_office()
    hub = _FakeHub()
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub)
    context = serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)

    _subscribe(context=context)
    _unsubscribe(context=context)
    again = _subscribe(rid="r2", context=context)

    assert again["result"]["replaced"] is False
    assert set(hub.sinks) == {office_subscription_key("c1", WORKSPACE)}


def test_two_connections_may_each_subscribe_to_the_same_workspace():
    """The key is per CONNECTION per workspace. Keying on the workspace alone
    would let the second operator's launcher displace the first's — and the
    displaced one would go quiet with no error to show for it."""

    _seed_office()
    hub = _FakeHub()
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub)

    for key in ("c1", "c2"):
        frame = _subscribe(
            context=serve_rpc.RpcContext(connection_key=key, emit=lambda _f: None)
        )
        assert "result" in frame

    assert set(hub.sinks) == {
        office_subscription_key("c1", WORKSPACE),
        office_subscription_key("c2", WORKSPACE),
    }


# ── teardown ────────────────────────────────────────────────────────────────


def test_release_drops_this_connections_subscriptions_and_leaves_the_others():
    """The leak this exists to prevent keeps a PRODUCER alive for nobody, which
    is why it is worth a test that names both halves."""

    _seed_office()
    hub = _FakeHub()
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub)
    for key in ("c1", "c2"):
        _subscribe(context=serve_rpc.RpcContext(connection_key=key, emit=lambda _f: None))

    assert OFFICE_SUBSCRIPTIONS.release("c1") == 1

    assert set(hub.sinks) == {office_subscription_key("c2", WORKSPACE)}
    assert OFFICE_SUBSCRIPTIONS.owned_keys("c1") == set()


def test_release_is_safe_for_a_connection_that_never_subscribed():
    """``_release_subscription`` runs on EVERY disconnect, and most connections
    never touch the office lane. It must cost a lookup and never raise."""

    OFFICE_SUBSCRIPTIONS.bind(lambda: _FakeHub())

    assert OFFICE_SUBSCRIPTIONS.release("never-here") == 0
    assert OFFICE_SUBSCRIPTIONS.release(None) == 0


# ── O-H2: per-client fold declarations on the office lane ───────────────────


def test_an_absent_fold_entities_param_declares_the_legacy_constant():
    """FAIL-OPEN, and it is the load-bearing half of the pair below.

    Every launcher in the field sends this method exactly one param. If the
    absent case resolved to the empty set, the room's INTERSECTION would zero
    out the moment such a client subscribed — the persona-instance patch lane
    that works today would go dark for everyone, which is the regression wearing
    a fix's clothes that ``OFFICE_FOLD_ENTITIES``' own comment already warns
    about one layer up.

    Kill-mutation: make the registry store ``frozenset()`` for a ``None``
    declaration.
    """

    _seed_office()
    OFFICE_SUBSCRIPTIONS.bind(lambda: _FakeHub())

    reply = _subscribe(
        context=serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)
    )["result"]

    assert reply["fold_entities"] == sorted(OFFICE_FOLD_ENTITIES)
    assert OFFICE_SUBSCRIPTIONS.declarations() == [OFFICE_FOLD_ENTITIES]


def test_a_declared_param_is_what_the_room_intersects_over():
    """The hole O-H2 closes (plan §V4).

    The declaration was a SERVER-side constant, which can only ever report a
    fact about the runtime — so a launcher whose fold had been widened could
    never have its widened rows promoted here: the intersection cannot contain a
    token nobody told the server about. Both directions are asserted on the same
    registry, because a test that only shows the widening is green against a
    handler that ignores the param and returns a hard-coded superset.

    Kill-mutation: drop ``fold_entities`` from the ``subscribe`` call in
    ``_runtime_office_subscribe`` — the widened case falls back to the constant
    and the token assertion goes red.
    """

    from agent_runtime.patch_coverage import HISTORICAL_FOLD_ENTITIES, accepted_fold_entities

    _seed_office()
    OFFICE_SUBSCRIPTIONS.bind(lambda: _FakeHub())
    widened = sorted(OFFICE_FOLD_ENTITIES | {"office_actor_lifecycle"})

    reply = _subscribe(
        context=serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None),
        fold_entities=widened,
    )["result"]

    assert reply["fold_entities"] == widened
    assert OFFICE_SUBSCRIPTIONS.declarations() == [frozenset(widened)]
    # And the room really does accept the token now — which it provably could
    # not before, whatever the client declared.
    assert "office_actor_lifecycle" in accepted_fold_entities(
        OFFICE_SUBSCRIPTIONS.declarations()
    )

    # NARROWING is honoured too, on the same registry: a second connection that
    # declares less takes the room down with it, which is the intersection rule
    # doing its job rather than a bug.
    _subscribe(
        rid="r2",
        context=serve_rpc.RpcContext(connection_key="c2", emit=lambda _f: None),
        fold_entities=sorted(HISTORICAL_FOLD_ENTITIES),
    )
    accepted = accepted_fold_entities(OFFICE_SUBSCRIPTIONS.declarations())
    assert accepted == HISTORICAL_FOLD_ENTITIES


def test_the_persona_instance_create_token_rides_this_lane_too():
    """D3's token must reach the OFFICE lane, not just the NDJSON child's argv.

    The launcher folds both transports with ONE body and declares one list on
    both channels, so a token that armed only the stream lane would leave the
    office push lane demoting the very batch the stream lane folds — and the add
    gesture's two halves ride different lanes. Nothing in the handler needed to
    change for this (the channel has never interpreted its strings), which is
    exactly why it needs an assertion: "it works because nobody filters it" is a
    property that a future allowlist would silently take away.

    Kill-mutation: filter the subscribe param to a known vocabulary — this and
    the ``a_token_from_the_future`` normalizer test both go red.
    """

    from agent_runtime.patch_coverage import (
        PERSONA_INSTANCE_CREATE_CAPABILITY,
        accepted_fold_entities,
    )

    _seed_office()
    OFFICE_SUBSCRIPTIONS.bind(lambda: _FakeHub())
    declared = sorted(
        OFFICE_FOLD_ENTITIES
        | {"office_actor_lifecycle", PERSONA_INSTANCE_CREATE_CAPABILITY}
    )

    reply = _subscribe(
        context=serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None),
        fold_entities=declared,
    )["result"]

    assert PERSONA_INSTANCE_CREATE_CAPABILITY in reply["fold_entities"]
    assert PERSONA_INSTANCE_CREATE_CAPABILITY in accepted_fold_entities(
        OFFICE_SUBSCRIPTIONS.declarations()
    )
    # And the LEGACY default must NOT carry it: a client that says nothing is a
    # fielded launcher whose fold refuses a create with ``patch_without_target``.
    assert PERSONA_INSTANCE_CREATE_CAPABILITY not in OFFICE_FOLD_ENTITIES


def test_an_explicitly_empty_declaration_is_honoured_as_empty():
    """"I fold nothing, send me full cores" is a thing a client may say.

    It must stay distinguishable from silence — which resolves to the legacy
    constant — or the fail-open default becomes un-overridable and a client with
    a broken fold has no way to opt out of promotion.
    """

    _seed_office()
    OFFICE_SUBSCRIPTIONS.bind(lambda: _FakeHub())

    reply = _subscribe(
        context=serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None),
        fold_entities=[],
    )["result"]

    assert reply["fold_entities"] == []
    assert OFFICE_SUBSCRIPTIONS.declarations() == [frozenset()]


def test_the_normalizer_cleans_degenerate_members_and_passes_unknown_ones_through():
    """Upstream's boundary discipline, both halves.

    Degenerate MEMBERS are normalized away (blanks, whitespace, non-strings) —
    a declaration is a set of names and a blank is not one. UNKNOWN members ride
    through untouched: this channel has never interpreted its strings, and a
    server that filtered to a known vocabulary would have dropped
    ``office_actor_lifecycle`` exactly as it would drop the next token.
    """

    assert normalize_office_fold_entities(
        ["  office_actor  ", "", "   ", 7, None, "a_token_from_the_future"]
    ) == frozenset({"office_actor", "a_token_from_the_future"})
    # A non-list is not a declaration; the handler refuses it rather than
    # filing the client as legacy (asserted at the handler below).
    assert normalize_office_fold_entities("office_actor") is None
    assert normalize_office_fold_entities({"office_actor": True}) is None
    assert normalize_office_fold_entities(None) is None


def test_a_non_list_fold_entities_is_refused_rather_than_treated_as_legacy():
    """A client sending the wrong shape should learn it.

    Silently filing it as "said nothing" would give it the legacy constant and a
    reply that echoes a declaration it never made — the class of silent
    mislabelling this method's typed refusals exist to end.
    """

    _seed_office()
    hub = _FakeHub()
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub)

    reply = _subscribe(
        context=serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None),
        fold_entities="office_actor",
    )

    assert reply["error"]["data"]["reason"] == "fold_entities_invalid"
    # Refused BEFORE anything was registered — a bad param must not leave a
    # subscription behind.
    assert hub.sinks == {}
    assert OFFICE_SUBSCRIPTIONS.declarations() == []


def test_a_re_baseline_replaces_the_declaration_it_displaced():
    """A re-subscribe re-declares, and the OLD declaration must not survive it.

    A stale declaration would keep narrowing (or widening) the room's
    intersection on behalf of a subscription that no longer exists — the same
    class of bookkeeping leak the ``_owned`` index is pruned against, one field
    over.
    """

    _seed_office()
    OFFICE_SUBSCRIPTIONS.bind(lambda: _FakeHub())
    context = serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)

    _subscribe(context=context, fold_entities=["office_actor", "office_actor_lifecycle"])
    _subscribe(rid="r2", context=context, fold_entities=["incident"])

    assert OFFICE_SUBSCRIPTIONS.declarations() == [frozenset({"incident"})]


def test_releasing_a_subscription_takes_its_declaration_with_it():
    """Both release paths — the single-workspace hand-back and the disconnect
    sweep — must drop the declaration, or a departed client keeps voting on what
    the room may promote."""

    _seed_office()
    _seed_office(OTHER)
    OFFICE_SUBSCRIPTIONS.bind(lambda: _FakeHub())
    context = serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)

    _subscribe(context=context, fold_entities=["office_actor"])
    _subscribe(rid="r2", workspace_id=OTHER, context=context, fold_entities=["incident"])
    assert len(OFFICE_SUBSCRIPTIONS.declarations()) == 2

    OFFICE_SUBSCRIPTIONS.release_one("c1", WORKSPACE)
    assert OFFICE_SUBSCRIPTIONS.declarations() == [frozenset({"incident"})]

    OFFICE_SUBSCRIPTIONS.release("c1")
    assert OFFICE_SUBSCRIPTIONS.declarations() == []


def test_the_declaration_index_does_not_outlive_its_subscriptions():
    """FOUND BY MUTATION, and stated for what it is: a LEAK guard.

    Deleting the declaration prune from ``_forget`` stayed green against every
    assertion above, and that is not an oversight in them — it is arithmetic.
    ``declarations()`` walks the ``_owned`` index and looks each key up, so a
    declaration whose key has already left ``_owned`` is never visited. It
    cannot change what the room accepts.

    What it CAN do is accumulate. This registry is process-global and outlives
    every serve loop; a connection that subscribes and releases in a loop —
    which is precisely what a client recovering from a bad baseline does — would
    grow a dict entry per lap forever, on a long-lived service. So the prune is
    real work with no public surface to observe it through, and the honest test
    is one that reads the index directly rather than one that pretends the
    intersection can see it.
    """

    _seed_office()
    OFFICE_SUBSCRIPTIONS.bind(lambda: _FakeHub())
    context = serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)

    for lap in range(3):
        _subscribe(rid=f"r{lap}", context=context, fold_entities=["office_actor"])
        OFFICE_SUBSCRIPTIONS.release_one("c1", WORKSPACE)

    assert OFFICE_SUBSCRIPTIONS._declared == {}, OFFICE_SUBSCRIPTIONS._declared
    assert OFFICE_SUBSCRIPTIONS.owned_keys("c1") == set()


def test_a_dropped_subscriber_is_told_to_resync_rather_than_left_quiet():
    """Backpressure eviction is the hub's answer to a client that cannot keep
    up. Without this the client keeps its stale canvas forever and nothing on
    either side says why."""

    _seed_office()
    hub = _FakeHub()
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub)
    sent: list[dict] = []
    _subscribe(context=serve_rpc.RpcContext(connection_key="c1", emit=sent.append))

    on_drop = hub.drops[office_subscription_key("c1", WORKSPACE)]
    on_drop("k", {"reason": "backpressure"})

    assert sent[-1]["method"] == OFFICE_RESYNC_METHOD
    assert sent[-1]["params"] == {
        "workspace_id": WORKSPACE,
        "reason": "backpressure",
    }


# ── the receipt reaches a REAL operator ─────────────────────────────────────


def test_the_real_serve_loop_bills_a_re_baseline_to_its_own_service_log():
    """The wire from ``serve.py`` to the log, over a real socket.

    Everything above binds the registry itself and hands it a list, which proves
    the registry writes when told to and nothing about whether the SERVE LOOP
    ever tells it. That one keyword argument is the whole operator-facing half of
    the receipt, and deleting it breaks no unit test — a client in a retry loop
    would go on taxing every other subscriber on the hub with the log silent,
    which is precisely the invisible cost this change refuses to ship.

    So the real loop is started with the real socket lane, a real client
    subscribes twice over a real connection, and the assertion is made against
    the line as it actually reaches an operator: ``_service_log`` writes to the
    serve's stderr, which arrives on the supervisor's NDJSON stream as an
    ordinary ``{"event":"stderr","line":…}`` frame. No new sink, and no reading
    of a closure this test has no business knowing about.

    A socket client is required rather than convenient: stdio has no push
    channel, so a stdio subscribe is refused before it can ever re-baseline.
    """

    from tests.agent_runtime.test_serve_socket_lane import client, running_serve

    _seed_office()

    with running_serve() as handle:
        with client(handle, name="rebaseline-peer") as (connection, _hello):
            for rid in ("sub-1", "sub-2"):
                connection.send(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "method": "runtime.office.subscribe",
                        "params": {"workspace_id": WORKSPACE},
                    }
                )
                reply = _read_socket_rpc(connection, rid)
                assert "error" not in reply, reply
            # The receipt the CLIENT gets, asserted on the same round trip: the
            # two halves are one decision, and a test that pinned only the log
            # would let the wire half rot.
            assert reply["result"]["replaced"] is True

            lines = _poll_service_log(
                handle, "serve_office_subscription_rebaselined"
            )

    assert len(lines) == 1, f"expected exactly one re-baseline line, got {lines}"
    assert lines[0]["workspace_id"] == WORKSPACE
    assert lines[0]["producer_restarted"] is True
    # The connection key is the serve loop's own, not one this test invented —
    # asserted as merely present and non-empty, because pinning its format here
    # would couple this file to the socket lane's naming.
    assert lines[0]["connection"]
    assert lines[0]["key"].startswith("rpc:office:")


def _read_socket_rpc(connection, rid: str, *, limit: int = 200) -> dict:
    """Read past the notifications. The office lane pushes UNPROMPTED frames on
    this very connection, so a reader that took the next frame would race a
    hydrate-driven resync and fail intermittently."""

    for _ in range(limit):
        frame = connection.read_frame()
        if frame is None:
            raise AssertionError(f"connection closed before a reply to {rid!r}")
        if frame.get("id") == rid and "jsonrpc" in frame:
            return frame
    raise AssertionError(f"no JSON-RPC reply for {rid!r} within {limit} frames")


def _poll_service_log(handle, event: str, timeout: float = 5.0) -> list[dict]:
    """Poll the serve's stderr frames for structured log lines naming *event*.

    Polled rather than read once because ``_service_log`` writes on the
    connection's own thread: the reply this test already has in hand is no
    guarantee the line has been flushed yet, and a single read would fail on
    scheduling rather than on behaviour."""

    import time as _time

    deadline = _time.monotonic() + timeout
    while True:
        found = [
            json.loads(row["line"])
            for row in handle.sink.frames()
            if row.get("event") == "stderr" and row.get("line", "").startswith("{")
        ]
        matched = [row for row in found if row.get("event") == event]
        if matched or _time.monotonic() >= deadline:
            return matched
        _time.sleep(0.01)


# ── the lane stays additive ─────────────────────────────────────────────────


def test_the_reclaim_pair_joins_the_manifest_without_moving_the_contract_version():
    """The set grows when a method is ADDED; the integer moves only when an
    existing method's shape changes incompatibly. A client only ever calls
    methods it found in the set, so adding one needs no bump — the reasoning
    the module docstring already commits to, asserted rather than assumed.

    Both halves of this change are additive under that rule, and the second is
    the one worth stating out loud because it looks like it might not be:
    ``runtime.office.subscribe``'s RESULT gained ``replaced``. Adding a key to a
    result does not move the integer either — the same precedent
    ``runtime.office.get`` set when it gained ``persona_instance_id``, because
    the launcher's decoder gates on required-key PRESENCE
    (``mission_office_rpc.dart:260``) and never on a key count. A client that
    folds today's reply folds tomorrow's unchanged; bumping would have made it
    REFUSE a payload it can read.

    What WOULD move the integer is the refusal that went away: a client keyed on
    ``already_subscribed`` would now never see it. No such client exists — the
    launcher has no ``runtime.office.subscribe`` caller yet, which is the whole
    reason this contract hole is being closed before one ships — and a refusal
    that could only ever be answered by giving up is not a shape anything can
    depend on having.
    """

    assert serve_rpc.method_names() == [
        "runtime.agent.create",
        "runtime.office.get",
        "runtime.office.remove",
        "runtime.office.subscribe",
        "runtime.office.surface.update",
        "runtime.office.unsubscribe",
        "runtime.office.upsert",
    ]
    assert serve_rpc.RPC_CONTRACT_VERSION == 1
    assert serve_rpc.manifest()["contract"] == 1


# ── FC-H1: the subscribe carries its cause, so the server log can join the
# ladder ────────────────────────────────────────────────────────────────────


def test_a_resubscribe_cause_reaches_the_service_log_verbatim():
    """The one fact the re-baseline receipt could never derive.

    Every re-subscribe in the launcher flows through ONE door and already knows
    exactly why it is happening (``fold:fenced``, ``push:full_core``, ...). That
    string used to die in the launcher's log: the server saw N re-baselines and
    could not tell a fold-fence storm from a demote storm, so splitting the
    classes meant joining two logs on timestamps — the same adjacency inference
    that had already misattributed this lane once.

    TWO DIFFERENT causes are driven through the same code, and that is the
    anti-vacuity design rather than duplication. The probed field is the
    receipt's ``reason``; a mutant that stamps a constant — or stamps the
    workspace, or the key — can match at most ONE of the two, and the expected
    value originates in this test's own input, so there is no constant the
    implementation could hold that satisfies both.
    """

    for cause in ("fold:fenced", "push:full_core"):
        _seed_office()
        hub = _FakeHub()
        lines: list[dict] = []
        OFFICE_SUBSCRIPTIONS.bind(lambda: hub, log=lines.append)
        context = serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)

        _subscribe(context=context, reason="start")
        # Fixture control, and the pinned rule this must not break: the FIRST
        # subscribe still writes nothing at all, cause or no cause.
        assert lines == []

        reply = _subscribe(rid="r2", context=context, reason=cause)

        assert "error" not in reply
        assert len(lines) == 1
        assert lines[0]["event"] == "serve_office_subscription_rebaselined"
        assert lines[0]["reason"] == cause
        OFFICE_SUBSCRIPTIONS.bind(None)


def test_a_subscribe_with_no_cause_prints_the_sentinel_not_a_missing_key():
    """Silence has to be a VALUE on the line, not a hole in it.

    Every launcher in the field today omits the param. If the key were omitted
    with it, an operator reading the log could not tell "this client is too old
    to say" from "this line predates the field" — and a key that appears only
    sometimes is one a reader learns to stop looking for. The sentinel is also
    the third driven value the mutation table needs: no constant equals
    ``fold:fenced``, ``push:full_core`` AND ``-``.
    """

    _seed_office()
    hub = _FakeHub()
    lines: list[dict] = []
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub, log=lines.append)
    context = serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)

    _subscribe(context=context)
    _subscribe(rid="r2", context=context)

    assert len(lines) == 1
    assert "reason" in lines[0]
    assert lines[0]["reason"] == "-"


def test_a_cause_off_the_charset_is_refused_before_any_store_or_hub_call():
    """A param this method only ever logs must not be able to cost a producer.

    Order is the assertion. The workspace is deliberately NOT seeded: if the
    reason check ran after the projection, this would answer
    ``workspace_not_found`` instead — so the refusal reason itself proves the
    check sits ahead of the office lock, the store read and the hub.

    The charset is boundary discipline, not decoration: this string is written
    verbatim into an operator's service log on a path a confused client takes
    repeatedly, so a newline in it would forge a log line and an unbounded one
    is a free write primitive into that tail.
    """

    hub = _FakeHub()
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub)
    context = serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)

    for bad in (
        "Fold:Fenced",  # uppercase
        "fold fenced",  # whitespace
        "fold\nfenced",  # the forged-line case
        "",  # blank: absence is already expressible by omission
        "x" * 65,  # one over the bound
        17,  # not a string at all
        ["fold:fenced"],
    ):
        reply = _subscribe(context=context, reason=bad)
        assert reply["error"]["code"] == -32602, bad
        assert reply["error"]["data"]["reason"] == "reason_invalid", bad

    # Nothing was registered on the way to any of those refusals.
    assert hub.subscribe_calls == []
    assert OFFICE_SUBSCRIPTIONS.owned_keys("c1") == set()


def test_the_cause_is_accepted_at_exactly_the_limit_and_kept_verbatim():
    """The bound is ``<=64``, and the value is not repaired on its way through.

    Both halves matter. An off-by-one that refused 64 would reject a legal
    token; a normalizer that stripped or lower-cased would make the two logs
    agree about a string neither side actually used, which is exactly what the
    verbatim rule exists to prevent.
    """

    from agent_runtime.serve_office_subscriptions import (
        SUBSCRIBE_REASON_MAX_CHARS,
        normalize_office_subscribe_reason,
    )

    assert SUBSCRIBE_REASON_MAX_CHARS == 64
    at_limit = "d" * 64
    assert normalize_office_subscribe_reason(at_limit) == at_limit
    assert normalize_office_subscribe_reason("d" * 65) is None
    # Every character class the launcher's own cause strings already use.
    for token in ("start", "fold:fenced", "push:full_core", "deferred:re-subscribe", "v1.2"):
        assert normalize_office_subscribe_reason(token) == token


def test_an_unknown_param_is_still_ignored_so_a_new_launcher_never_refuses():
    """A-1, verified rather than assumed — the precondition FC-H1 rests on.

    The mixed pair that matters is a NEW launcher against an OLD runtime: it
    will send ``reason`` to a handler that has never heard of it. That degrades
    to "the cause is dropped" only if the handler reads known keys and ignores
    the rest; if it validated its param set, the same launcher would be REFUSED
    a subscription entirely and the office would go dark on every old runtime.

    The property is tested from this side because it is the one that is
    testable: an unknown key here stands in for ``reason`` there.
    """

    _seed_office()
    hub = _FakeHub()
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub)
    context = serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)

    reply = serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "r1",
            "method": "runtime.office.subscribe",
            "params": {
                "workspace_id": WORKSPACE,
                "a_param_from_two_versions_hence": {"nested": True},
            },
        },
        context,
    )

    assert "error" not in reply
    assert reply["result"]["watermark"]["event_offset"] >= 0
    assert hub.subscribe_calls == [office_subscription_key("c1", WORKSPACE)]
