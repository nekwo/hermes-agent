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

The sink is tested against hand-built frames rather than a live hub: these are
FILTERING rules, and an integration test that happened to produce no frame
would look identical to a filter that drops everything.
"""

from __future__ import annotations

import pytest

from agent_runtime import serve_rpc
from agent_runtime.serve_office_subscriptions import (
    OFFICE_PATCH_METHOD,
    OFFICE_RESYNC_METHOD,
    OFFICE_SUBSCRIPTIONS,
    office_patch_sink,
    office_subscription_key,
)

WORKSPACE = "ws_rpc_subscribe_test"
OTHER = "ws_somebody_else"


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
    the REFUSAL of a duplicate key (which is what `already_subscribed` is
    derived from) and returning False from `unsubscribe` for a key it never
    held, and a permissive mock would let a bug in either pass.
    """

    def __init__(self) -> None:
        self.sinks: dict[str, object] = {}
        self.drops: dict[str, object] = {}

    def subscribe(self, key, *, sink, on_drop=None) -> bool:
        if key in self.sinks:
            return False
        self.sinks[key] = sink
        self.drops[key] = on_drop
        return True

    def unsubscribe(self, key) -> bool:
        self.drops.pop(key, None)
        return self.sinks.pop(key, None) is not None


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


def _subscribe(rid="r1", workspace_id=WORKSPACE, context=None) -> dict:
    return serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "runtime.office.subscribe",
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


def test_a_mixed_batch_carries_only_this_workspaces_rows():
    """The filter is per ROW, not per frame. A frame-level test passes against
    a sink that forwards a mixed batch whole — which would hand the client
    another workspace's actors to fold into its own canvas."""

    sent, deliver = _sink()

    deliver(
        _patch_frame(
            base_offset=10,
            event_offset=12,
            ids=[
                f"{OTHER}/personainst_dev_3ebfce41",
                f"{WORKSPACE}/personainst_qa_agent_9c8a382f",
                f"{OTHER}/personainst_neko_f6f7a51b",
            ],
        )
    )

    assert [row["id"] for row in sent[0]["params"]["patches"]] == [
        f"{WORKSPACE}/personainst_qa_agent_9c8a382f"
    ]


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
    """A full core means ``batch_is_patch_coverable`` said no — a create moved
    ``actor_count``, an archive rewrote the resurrection ledger. There is no
    honest office patch to forward, and silence would leave the client
    believing it is current. That is the exact failure class this lane exists
    to delete."""

    sent, deliver = _sink()

    deliver({"type": frame_type, "watermark": {"event_offset": 99}})

    assert sent[0]["method"] == OFFICE_RESYNC_METHOD
    assert sent[0]["params"] == {"workspace_id": WORKSPACE, "reason": "full_core"}


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
    assert subscribed == got
    assert isinstance(watermark["event_offset"], int)
    assert set(hub.sinks) == {office_subscription_key("c1", WORKSPACE)}


def test_a_second_subscribe_on_one_connection_is_typed_not_a_silent_reregister():
    """Idempotence with a receipt. A silent re-register would leak a subscriber
    per retry, and a retry is exactly what a client does when it is unsure."""

    _seed_office()
    # ONE hub, held across both calls — `_ensure_stream_hub` memoizes under a
    # lock, so a factory minting a fresh hub per call would be testing a
    # runtime that does not exist (and would make the duplicate invisible).
    hub = _FakeHub()
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub)
    context = serve_rpc.RpcContext(connection_key="c1", emit=lambda _f: None)

    assert "result" in _subscribe(context=context)
    frame = _subscribe(rid="r2", context=context)

    assert frame["error"]["code"] == serve_rpc.ERR_CONFLICT
    assert frame["error"]["data"]["reason"] == "already_subscribed"


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


# ── the lane stays additive ─────────────────────────────────────────────────


def test_subscribe_joins_the_manifest_without_moving_the_contract_version():
    """The set grows when a method is ADDED; the integer moves only when an
    existing method's shape changes incompatibly. A client only ever calls
    methods it found in the set, so adding one needs no bump — the reasoning
    the module docstring already commits to, asserted rather than assumed."""

    assert serve_rpc.method_names() == [
        "runtime.office.get",
        "runtime.office.subscribe",
        "runtime.office.upsert",
    ]
    assert serve_rpc.RPC_CONTRACT_VERSION == 1
