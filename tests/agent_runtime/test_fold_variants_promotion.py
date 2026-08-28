"""Per-subscriber promotion: R10's assigned consequence, closed at the hub.

The intersection rule (``patch_coverage.accepted_fold_entities``) is the only
safe promotion rule while a fan-out can deliver exactly ONE shape of a frame, and
it has a cost the drop-latency tables never priced: a gateway Stage 5 phone
declaring a narrow chat-first fold DEMOTES every subscriber beside it to a full
~1 MB core on every office write. Correct, and paid by a client that did nothing.

What this file pins, in the order the change happens:

1. **The equivalence.** ``batch_required_fold_tokens`` is
   ``batch_is_patch_coverable`` re-expressed as the SET it tests rather than the
   boolean it returns. That equivalence is the whole basis of the split gate, and
   it is checked here over the real event vocabulary and a matrix of declarations
   rather than asserted in a docstring.
2. **The single-subscriber pin.** A room whose declarations all agree — which
   INCLUDES every room of one — takes the same branch it always took and emits
   the same bytes. Not a convention: the split branch is unreachable when the
   floor and the union are equal.
3. **The split itself.** A room that disagrees ships one envelope carrying both
   halves; the wide declaration resolves to the patch, the narrow one to the
   core, and no sink ever sees the envelope.
4. **The hub end-to-end**, over the real ``StreamHub`` with two real
   subscriptions and two real pump threads.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest

from agent_runtime.models import Event
from agent_runtime.patch_coverage import (
    HISTORICAL_FOLD_ENTITIES,
    OFFICE_ACTOR_LIFECYCLE_CAPABILITY,
    OFFICE_SURFACE_FOLD_CAPABILITY,
    PERSONA_INSTANCE_CREATE_CAPABILITY,
    accepted_fold_entities,
    batch_is_patch_coverable,
    batch_required_fold_tokens,
    event_is_patch_coverable,
    event_required_fold_tokens,
    normalize_fold_entities,
    union_fold_entities,
)
from agent_runtime.serve_stream_hub import StreamHub
from agent_runtime.state_patches import (
    OFFICE_ACTOR_ENTITY,
    OFFICE_SURFACE_ENTITY,
    PATCH_OP_REFRESH,
    PATCH_OP_REMOVE,
    PATCH_OP_UPSERT,
    PERSONA_INSTANCE_ENTITY,
    STATE_PATCHED_EVENT_TYPE,
)
from agent_runtime.stream import (
    FOLD_VARIANTS_FRAME_TYPE,
    _batch_frames_with_liveness,
    fold_variants_frame,
    resolve_fold_variant,
)

_TS = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)


def _op_event(offset: int, entity: str, entity_id: str, op: str, **payload):
    body = {"entity": entity, "id": entity_id, "op": op, **payload}
    return offset, Event(
        ts=_TS,
        type=STATE_PATCHED_EVENT_TYPE,
        task_id=None,
        run_id=None,
        persona_id=None,
        payload=body,
    )


def _plain_event(offset: int, event_type: str):
    return offset, Event(
        ts=_TS,
        type=event_type,
        task_id=None,
        run_id=None,
        persona_id=None,
        payload={"fingerprint": "fp"},
    )


#: Every shape the coverage classifier branches on, so the equivalence below is
#: checked against the vocabulary and not against three convenient cases.
_EVENT_MATRIX = [
    _op_event(1, PERSONA_INSTANCE_ENTITY, "p1", PATCH_OP_UPSERT, changed={"model": "x"}),
    _op_event(2, PERSONA_INSTANCE_ENTITY, "p2", PATCH_OP_REMOVE),
    _op_event(
        3, PERSONA_INSTANCE_ENTITY, "p3", PATCH_OP_UPSERT, changed={"a": 1}, created=True
    ),
    _op_event(4, "incident", "i1", PATCH_OP_REMOVE),
    _op_event(5, OFFICE_ACTOR_ENTITY, "a1", PATCH_OP_UPSERT, changed={"x": 1}),
    _op_event(6, OFFICE_ACTOR_ENTITY, "a2", PATCH_OP_REMOVE),
    _op_event(7, OFFICE_ACTOR_ENTITY, "a3", PATCH_OP_UPSERT, changed={"x": 1}, created=True),
    _op_event(8, OFFICE_SURFACE_ENTITY, "s1", PATCH_OP_UPSERT, changed={"folders": []}),
    _op_event(9, "task", "t1", PATCH_OP_REFRESH),
    _op_event(10, PERSONA_INSTANCE_ENTITY, "p4", PATCH_OP_UPSERT, changed={}),
    _plain_event(11, "persona_instance.steered"),
    _plain_event(12, "incident.closed"),
    _plain_event(13, "office.surface.updated"),
    _plain_event(14, "state.reconciled"),
    _plain_event(15, "task.transition"),
    _plain_event(16, "persona_assignment.closed"),
]

_DECLARATION_MATRIX = [
    None,
    frozenset(),
    HISTORICAL_FOLD_ENTITIES,
    frozenset({PERSONA_INSTANCE_ENTITY}),
    frozenset({OFFICE_ACTOR_ENTITY}),
    frozenset({OFFICE_ACTOR_ENTITY, OFFICE_ACTOR_LIFECYCLE_CAPABILITY}),
    frozenset({PERSONA_INSTANCE_ENTITY, PERSONA_INSTANCE_CREATE_CAPABILITY}),
    frozenset({OFFICE_SURFACE_ENTITY, OFFICE_SURFACE_FOLD_CAPABILITY}),
    frozenset(
        {
            PERSONA_INSTANCE_ENTITY,
            "incident",
            OFFICE_ACTOR_ENTITY,
            OFFICE_ACTOR_LIFECYCLE_CAPABILITY,
            PERSONA_INSTANCE_CREATE_CAPABILITY,
            OFFICE_SURFACE_ENTITY,
            OFFICE_SURFACE_FOLD_CAPABILITY,
        }
    ),
]


# --------------------------------------------------------------------------- #
# 1. The equivalence the split gate rests on
# --------------------------------------------------------------------------- #
def test_required_tokens_is_the_coverage_boolean_as_a_set():
    """``coverable(e, d)`` iff ``req(e) is not None and req(e) <= d`` — for every
    event shape the classifier branches on, against every declaration shape.

    If this ever fails, the split gate is promoting or demoting on a rule the
    coverage classifier does not agree with, and the two would drift silently:
    the bare-patch path would still look right in every homogeneous test.
    """

    for offset, event in _EVENT_MATRIX:
        required = event_required_fold_tokens(event)
        for declared in _DECLARATION_MATRIX:
            normalized = normalize_fold_entities(declared)
            expected = event_is_patch_coverable(event, fold_entities=declared)
            actual = required is not None and required <= normalized
            assert actual == expected, (
                f"offset={offset} type={event.type} payload={event.payload} "
                f"declared={sorted(normalized)} required="
                f"{None if required is None else sorted(required)}"
            )


def test_batch_required_tokens_matches_the_batch_boolean():
    batches = [
        [_EVENT_MATRIX[0], _EVENT_MATRIX[10]],  # steer upsert + its domain event
        [_EVENT_MATRIX[4], _EVENT_MATRIX[0]],  # office move + a persona upsert
        [_EVENT_MATRIX[6]],  # office lifecycle create
        [_EVENT_MATRIX[7], _EVENT_MATRIX[12]],  # surface patch + its gated event
        [_EVENT_MATRIX[0], _EVENT_MATRIX[13]],  # a reconcile poisons the batch
        [_EVENT_MATRIX[8]],  # a refresh is foldable for nobody
        [],
    ]
    for batch in batches:
        events = [event for _, event in batch]
        required = batch_required_fold_tokens(events)
        for declared in _DECLARATION_MATRIX:
            expected = batch_is_patch_coverable(events, fold_entities=declared)
            actual = required is not None and required <= normalize_fold_entities(
                declared
            )
            assert actual == expected, (
                f"batch={[e.type for e in events]} declared={sorted(normalize_fold_entities(declared))}"
            )


def test_uncovered_batch_has_no_covering_declaration():
    """A structurally uncovered batch demotes for EVERYONE — the union cannot buy
    its way out of a ``refresh`` or a chokepoint-less write."""

    for batch in ([_EVENT_MATRIX[8]], [_EVENT_MATRIX[13]], [_EVENT_MATRIX[14]], []):
        events = [event for _, event in batch]
        assert batch_required_fold_tokens(events) is None


# --------------------------------------------------------------------------- #
# 2. Union vs intersection
# --------------------------------------------------------------------------- #
def test_union_and_intersection_agree_for_a_room_of_one():
    """The single-subscriber guarantee, at its source. Whatever one client says,
    the floor and the promotion set are the SAME set — so the split branch has no
    input that can reach it."""

    for declared in _DECLARATION_MATRIX:
        room = [declared]
        assert accepted_fold_entities(room) == union_fold_entities(room)


def test_union_widens_where_the_intersection_narrows():
    phone = frozenset({PERSONA_INSTANCE_ENTITY, "incident"})
    desktop = frozenset({PERSONA_INSTANCE_ENTITY, "incident", OFFICE_ACTOR_ENTITY})
    room = [phone, desktop]
    assert accepted_fold_entities(room) == phone
    assert union_fold_entities(room) == desktop


def test_empty_room_answers_historical_on_both_operators():
    assert union_fold_entities([]) == HISTORICAL_FOLD_ENTITIES
    assert accepted_fold_entities([]) == HISTORICAL_FOLD_ENTITIES


# --------------------------------------------------------------------------- #
# 3. The resolver
# --------------------------------------------------------------------------- #
def test_resolver_passes_ordinary_frames_through_untouched():
    for frame in (
        {"type": "hydrate", "core": {}},
        {"type": "patch", "patches": []},
        {"type": "heartbeat"},
        {"type": "delta"},
    ):
        assert resolve_fold_variant(frame, None) is frame
        assert resolve_fold_variant(frame, frozenset({"anything"})) is frame


def test_resolver_hands_the_patch_only_to_a_covering_declaration():
    envelope = fold_variants_frame(
        patch={"type": "patch", "patches": [{"entity": OFFICE_ACTOR_ENTITY}]},
        core={"type": "delta", "core": {"big": True}},
        required_tokens={OFFICE_ACTOR_ENTITY},
    )
    wide = frozenset({PERSONA_INSTANCE_ENTITY, "incident", OFFICE_ACTOR_ENTITY})
    narrow = frozenset({PERSONA_INSTANCE_ENTITY, "incident"})
    assert resolve_fold_variant(envelope, wide)["type"] == "patch"
    assert resolve_fold_variant(envelope, narrow)["type"] == "delta"
    # Declared NOTHING resolves through the historical set, exactly as the
    # coverage gate reads it — never as the empty set, and never as the patch.
    assert resolve_fold_variant(envelope, None)["type"] == "delta"


def test_resolver_never_returns_an_envelope_to_a_sink():
    envelope = fold_variants_frame(
        patch={"type": "patch"},
        core={"type": "delta"},
        required_tokens={OFFICE_ACTOR_ENTITY},
    )
    for declared in _DECLARATION_MATRIX:
        assert resolve_fold_variant(envelope, declared)["type"] != FOLD_VARIANTS_FRAME_TYPE


def test_resolver_falls_back_to_core_when_the_patch_half_is_malformed():
    """The safe direction, stated as a test rather than as an intention: a
    subscriber is never handed a patch this producer could not build."""

    broken = {
        "type": FOLD_VARIANTS_FRAME_TYPE,
        "required_fold_tokens": [OFFICE_ACTOR_ENTITY],
        "patch": None,
        "core": {"type": "delta"},
    }
    assert resolve_fold_variant(broken, frozenset({OFFICE_ACTOR_ENTITY}))["type"] == "delta"


# --------------------------------------------------------------------------- #
# 4. The producer's split gate
# --------------------------------------------------------------------------- #
def _frames(batch, *, accepted, promote=None, delta_patches=True, resync=False):
    return list(
        _batch_frames_with_liveness(
            batch,
            base_offset=0,
            delta_patches=delta_patches,
            resync=resync,
            heartbeat_interval_seconds=60.0,
            fold_entities=accepted,
            promote_fold_entities=promote,
            caller="test",
        )
    )


def test_homogeneous_room_emits_a_bare_patch_and_builds_no_core(monkeypatch):
    """The single-subscriber pin. With the floor and the union equal, the frames
    are the ones this lane emitted before the parameter existed — and the proof
    that no core was built is that the snapshot builder is never called."""

    import agent_runtime.stream as stream_module

    def _refuse(*args, **kwargs):
        raise AssertionError("a homogeneous room must not pay for a core")

    monkeypatch.setattr(stream_module, "build_snapshot", _refuse)

    batch = [_EVENT_MATRIX[4], _EVENT_MATRIX[0]]  # office move + persona upsert
    wide = frozenset({PERSONA_INSTANCE_ENTITY, OFFICE_ACTOR_ENTITY})

    without = _frames(batch, accepted=wide)
    with_equal_union = _frames(batch, accepted=wide, promote=wide)

    assert [f["type"] for f in without] == ["patch"]
    assert [f["type"] for f in with_equal_union] == ["patch"]
    # Byte-for-byte the same decision, modulo the timestamps the frame stamps
    # itself with.
    assert without[0]["patches"] == with_equal_union[0]["patches"]
    assert without[0]["base_offset"] == with_equal_union[0]["base_offset"]


def test_split_room_emits_one_envelope_carrying_both_halves(monkeypatch):
    import agent_runtime.stream as stream_module

    monkeypatch.setattr(
        stream_module,
        "build_snapshot",
        lambda **kwargs: {"generated_at": "2026-08-27T12:00:00Z", "core": {"big": True}},
    )
    monkeypatch.setattr(stream_module, "core_event_offset", lambda snapshot: None)

    batch = [_EVENT_MATRIX[4]]  # an office_actor move upsert
    phone = frozenset({PERSONA_INSTANCE_ENTITY, "incident"})
    desktop = phone | {OFFICE_ACTOR_ENTITY}

    frames = _frames(batch, accepted=phone, promote=desktop)
    envelopes = [f for f in frames if f["type"] == FOLD_VARIANTS_FRAME_TYPE]
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope["required_fold_tokens"] == [OFFICE_ACTOR_ENTITY]
    assert resolve_fold_variant(envelope, desktop)["type"] == "patch"
    assert resolve_fold_variant(envelope, phone)["type"] == "delta"


def test_a_batch_nobody_can_fold_stays_a_bare_core(monkeypatch):
    """The union buys nothing for an uncovered batch — no envelope, no patch,
    and every subscriber gets the same core it always got."""

    import agent_runtime.stream as stream_module

    monkeypatch.setattr(
        stream_module,
        "build_snapshot",
        lambda **kwargs: {"generated_at": "2026-08-27T12:00:00Z", "core": {}},
    )
    monkeypatch.setattr(stream_module, "core_event_offset", lambda snapshot: None)

    batch = [_EVENT_MATRIX[0], _EVENT_MATRIX[13]]  # a reconcile poisons it
    frames = _frames(
        batch,
        accepted=frozenset({PERSONA_INSTANCE_ENTITY}),
        promote=frozenset({PERSONA_INSTANCE_ENTITY, OFFICE_ACTOR_ENTITY}),
    )
    assert [f["type"] for f in frames] == ["delta"]


def test_resync_is_never_split(monkeypatch):
    """An explicit re-baseline is a request for a core, and a room that disagrees
    does not turn it into a patch for the half that could have folded one."""

    import agent_runtime.stream as stream_module

    monkeypatch.setattr(
        stream_module,
        "build_snapshot",
        lambda **kwargs: {"generated_at": "2026-08-27T12:00:00Z", "core": {}},
    )
    monkeypatch.setattr(stream_module, "core_event_offset", lambda snapshot: None)

    batch = [_EVENT_MATRIX[4]]
    frames = _frames(
        batch,
        accepted=frozenset({PERSONA_INSTANCE_ENTITY}),
        promote=frozenset({PERSONA_INSTANCE_ENTITY, OFFICE_ACTOR_ENTITY}),
        resync=True,
    )
    assert [f["type"] for f in frames] == ["delta"]


# --------------------------------------------------------------------------- #
# 5. The hub, with two real subscriptions and two real pumps
# --------------------------------------------------------------------------- #
def test_hub_hands_each_subscriber_the_half_it_declared():
    """The whole point, end to end: the desktop KEEPS its patch while the phone
    beside it takes the demoted core, off ONE producer and ONE frame."""

    envelope = fold_variants_frame(
        patch={"type": "patch", "patches": [{"entity": OFFICE_ACTOR_ENTITY}]},
        core={"type": "delta", "core": {"bytes": "many"}},
        required_tokens={OFFICE_ACTOR_ENTITY},
    )
    released = threading.Event()

    def _source():
        yield {"type": "hydrate", "core": {}}
        yield envelope
        released.wait(5.0)

    hub = StreamHub(_source)
    desktop: list[dict] = []
    phone: list[dict] = []
    try:
        hub.subscribe(
            "desktop",
            sink=desktop.append,
            declared=frozenset(
                {PERSONA_INSTANCE_ENTITY, "incident", OFFICE_ACTOR_ENTITY}
            ),
        )
        hub.subscribe(
            "phone",
            sink=phone.append,
            declared=frozenset({PERSONA_INSTANCE_ENTITY, "incident"}),
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if len(desktop) >= 2 and len(phone) >= 2:
                break
            time.sleep(0.02)
    finally:
        released.set()
        hub.stop()

    assert [f["type"] for f in desktop[:2]] == ["hydrate", "patch"]
    assert [f["type"] for f in phone[:2]] == ["hydrate", "delta"]
    # And neither of them ever saw the envelope.
    assert all(f["type"] != FOLD_VARIANTS_FRAME_TYPE for f in desktop + phone)


def test_hub_subscriber_that_declares_nothing_takes_the_core():
    """A lane that never learned to declare degrades to what it always had."""

    envelope = fold_variants_frame(
        patch={"type": "patch"},
        core={"type": "delta"},
        required_tokens={OFFICE_ACTOR_ENTITY},
    )
    released = threading.Event()

    def _source():
        yield envelope
        released.wait(5.0)

    hub = StreamHub(_source)
    seen: list[dict] = []
    try:
        hub.subscribe("legacy", sink=seen.append)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not seen:
            time.sleep(0.02)
    finally:
        released.set()
        hub.stop()

    assert [f["type"] for f in seen[:1]] == ["delta"]


@pytest.mark.parametrize("declared", [None, frozenset({OFFICE_ACTOR_ENTITY})])
def test_hub_delivers_ordinary_frames_unchanged_whatever_was_declared(declared):
    """The homogeneous lane pays a dict lookup and nothing else — the frame that
    reaches the sink is the identical object the producer yielded."""

    frame = {"type": "hydrate", "core": {"identity": object()}}
    released = threading.Event()

    def _source():
        yield frame
        released.wait(5.0)

    hub = StreamHub(_source)
    seen: list[dict] = []
    try:
        hub.subscribe("one", sink=seen.append, declared=declared)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not seen:
            time.sleep(0.02)
    finally:
        released.set()
        hub.stop()

    assert seen and seen[0] is frame
