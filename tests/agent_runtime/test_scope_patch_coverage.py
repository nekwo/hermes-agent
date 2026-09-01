"""WS1: the ``scope`` fold entity, and the demote that stops firing.

Instant-workspace-switching plan §1.1 (EterniaLauncher
``docs/mission_control/planned/instant-workspace-switching.md``; hermes pointer
``docs/agent-runtime-harness/planned/instant-workspace-switching.md``).

A workspace switch is the cheapest state change this runtime has — two scalars
in a pointer file — and it was among the most expensive on the wire: neither
``workspace.activated`` nor ``realm.activated`` was patch-covered, so a switch
demoted its whole batch to a full O(world) core. This file pins the four claims
that make covering them honest:

1. **The derivability census.** ``snapshot.py`` reads ``active_id()`` in exactly
   SEVEN places, all of them pure functions of the two pointers plus rows the
   client already holds. An EIGHTH reader must redden here with instructions,
   because the module's honesty rule is that a covered event's patch carries
   everything the demoted core would have said, and only a census can tell you
   when that stops being true.
2. **The coverage predicate**, in both directions: a lone activate batch is
   coverable for a declaring client and not for anyone else, and one uncovered
   neighbour still demotes it.
3. **Per-subscriber promotion**, over the real ``StreamHub``: the declarer is
   handed the patch and the non-declarer the demoted core, from ONE batch — the
   S5 test shape, reused rather than re-derived.
4. **The producer**: ``set_active`` emits the row from inside the write, both
   pointers always, beside the domain event it pairs with.
"""

from __future__ import annotations

import ast
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from agent_runtime.models import Event
from agent_runtime.patch_coverage import (
    COVERED_DOMAIN_EVENT_TYPES,
    HISTORICAL_FOLD_ENTITIES,
    LIVE_COVERED_DOMAIN_EVENT_TYPES,
    TOKEN_GATED_DOMAIN_EVENT_TYPES,
    batch_is_patch_coverable,
    batch_required_fold_tokens,
    event_is_patch_coverable,
    event_required_fold_tokens,
    normalize_fold_entities,
)
from agent_runtime.serve_stream_hub import StreamHub
from agent_runtime.state_patches import (
    PATCH_OP_UPSERT,
    SCOPE_ENTITY,
    SCOPE_PATCH_FIELDS,
    SCOPE_PATCH_ID,
    STATE_PATCHED_EVENT_TYPE,
    build_state_patch,
)
from agent_runtime.stream import (
    FOLD_VARIANTS_FRAME_TYPE,
    fold_variants_frame,
    patch_batch_frame,
    resolve_fold_variant,
)

_TS = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "stream_frames"

#: The declaration a WS2 launcher sends: today's fielded set plus ``scope``.
_DECLARING = frozenset(
    {
        "persona_instance",
        "incident",
        "office_actor",
        "office_actor_lifecycle",
        "persona_instance_create",
        "office_surface",
        "office_surface_fold",
        SCOPE_ENTITY,
    }
)

#: The widest declaration any launcher sent BEFORE WS2 — the version-skew floor.
_FIELDED = _DECLARING - {SCOPE_ENTITY}


def _scope_patch_event(offset: int, workspace_id: str | None, realm_id: str | None):
    return offset, Event(
        ts=_TS,
        type=STATE_PATCHED_EVENT_TYPE,
        task_id=None,
        run_id=None,
        persona_id=None,
        payload=build_state_patch(
            SCOPE_ENTITY,
            SCOPE_PATCH_ID,
            PATCH_OP_UPSERT,
            {"active_workspace_id": workspace_id, "active_realm_id": realm_id},
        ),
    )


def _plain(offset: int, event_type: str, **payload):
    return offset, Event(
        ts=_TS,
        type=event_type,
        task_id=None,
        run_id=None,
        persona_id=None,
        payload=dict(payload),
    )


# --------------------------------------------------------------------------- #
# 1. The derivability census (plan §1.1's grep, pinned)
# --------------------------------------------------------------------------- #
#: Every ``active_id()`` call site in ``snapshot.py``, as the NORMALIZED source
#: line that carries it, keyed by what the reader is FOR.
#:
#: Pinned as source text rather than as line numbers, and that is a deliberate
#: departure from the plan's wording (which names ``:796,:800,:887,:927,:932,
#: :964,:965`` at the 2026-09-01 baseline — all seven verified there). Line
#: numbers make this census fail on any edit ABOVE the first reader, which is a
#: red that teaches nothing and trains people to re-pin without reading. The
#: text is stable under exactly the edits that do not add a reader, and the
#: failure message below prints the live line numbers so the plan's spelling
#: stays checkable by hand.
_ACTIVE_ID_READERS: dict[str, str] = {
    "active workspace NAME for the situational HUD": (
        '(getattr(w, "name", None) for w in workspaces '
        'if getattr(w, "id", None) == workspace_store.active_id()),'
    ),
    "active realm NAME for the situational HUD": (
        '(getattr(r, "name", None) for r in realms '
        'if getattr(r, "id", None) == realm_store.active_id()),'
    ),
    "prompt_observability roster scoping kwarg": (
        "active_workspace_id=workspace_store.active_id(),"
    ),
    "per-row active flag on the workspace summaries": (
        "active_id=workspace_store.active_id(),"
    ),
    "per-row active flag on the realm summaries": (
        "_realm_summary(item, workspaces=workspaces, active_id=realm_store.active_id())"
    ),
    "top-level active_workspace_id pointer": (
        '"active_workspace_id": workspace_store.active_id(),'
    ),
    "top-level active_realm_id pointer": ('"active_realm_id": realm_store.active_id(),'),
}

#: The three readers above whose value does NOT reach the client as a pointer or
#: as a per-row flag — the accounted residue of covering the activate events.
#:
#: They feed ``prompt_observability``: the per-lane ``situational_hud``
#: realm/workspace strings, and the addressable-roster scoping for
#: runtime-global instances only. A ``scope`` patch does not carry that section,
#: so a client folding one holds those strings one switch stale until any core
#: arrives. Named here so the residue is a listed fact with a size rather than
#: an omission — the argument for accepting it is in ``patch_coverage``'s entry
#: comment, and the plan's "nothing else in the core varies with the pointer" is
#: corrected in ``planned/iws-ws12-field-notes-2026-09-01.md``.
_INDIRECT_READERS = frozenset(
    {
        "active workspace NAME for the situational HUD",
        "active realm NAME for the situational HUD",
        "prompt_observability roster scoping kwarg",
    }
)


def test_the_snapshot_reads_the_pointers_in_exactly_seven_places():
    """The derivability pin. An eighth reader fails HERE, with instructions.

    Covering ``workspace.activated`` means claiming the ``scope`` patch says
    everything a demoted core would have. That claim is only as good as the list
    of things the core derives from the pointers, so the list is enumerated
    rather than believed.
    """

    import agent_runtime.snapshot as snapshot_module

    source = Path(snapshot_module.__file__).read_text(encoding="utf-8")
    lines = source.splitlines()
    found: dict[str, list[int]] = {}
    for number, raw in enumerate(lines, 1):
        if "active_id()" not in raw:
            continue
        normalized = " ".join(raw.split())
        found[normalized] = found.get(normalized, []) + [number]

    expected = {" ".join(text.split()) for text in _ACTIVE_ID_READERS.values()}
    actual = set(found)
    unexpected = actual - expected
    missing = expected - actual
    where = {text: found[text] for text in sorted(actual)}
    assert not unexpected, (
        "A NEW reader of the active-scope pointers appeared in snapshot.py:\n"
        + "\n".join(f"  line {found[text]}: {text}" for text in sorted(unexpected))
        + "\n\nThis census is the derivability audit behind covering "
        "workspace.activated/realm.activated (patch_coverage."
        "LIVE_COVERED_DOMAIN_EVENT_TYPES). Before re-pinning it, answer the "
        "question it exists to ask: can a client holding only "
        "{active_workspace_id, active_realm_id} plus the workspace/realm lists "
        "reproduce what your new reader put in the core?\n"
        "  * YES  -> add it to _ACTIVE_ID_READERS with a name saying what it is "
        "for, and to _INDIRECT_READERS if its value reaches the wire through "
        "another section rather than as a pointer or a per-row flag.\n"
        "  * NO   -> the scope patch now drops state the demoted core carried. "
        "Either carry it in the patch, or take the two events back out of "
        "LIVE_COVERED_DOMAIN_EVENT_TYPES and let the batch demote honestly.\n"
        f"live census: {where}"
    )
    assert not missing, (
        "An expected reader of the active-scope pointers is gone from "
        f"snapshot.py: {sorted(missing)}\nlive census: {where}"
    )
    assert len(found) == 7, where
    # Non-vacuity: the plan's seven are seven DISTINCT lines, not one line the
    # normalizer collapsed a family onto.
    assert sum(len(numbers) for numbers in found.values()) == 7, where
    assert _INDIRECT_READERS < set(_ACTIVE_ID_READERS)


def test_the_two_activate_events_are_covered_and_gated_on_the_scope_entity():
    """The coverage entries themselves, and the gate they ride.

    A covered domain event is normally NOT entity-gated — it carries no fold
    state, so the gate that matters is on the paired patch. That is unsafe for a
    patch on a NEW entity, so both events name their pair's entity in
    ``TOKEN_GATED_DOMAIN_EVENT_TYPES``. Asserted rather than assumed, because an
    un-gated entry here would promote a batch at a pre-WS2 launcher which
    answers the resulting frame with a full re-hydrate: the patch AND the core.
    """

    for event_type in ("workspace.activated", "realm.activated"):
        assert event_type in LIVE_COVERED_DOMAIN_EVENT_TYPES
        assert event_type in COVERED_DOMAIN_EVENT_TYPES
        assert TOKEN_GATED_DOMAIN_EVENT_TYPES[event_type] == SCOPE_ENTITY
        event = _plain(1, event_type)[1]
        assert event_is_patch_coverable(event, fold_entities=_DECLARING)
        assert not event_is_patch_coverable(event, fold_entities=_FIELDED)
        # "Said nothing" is the historical set, never the empty set — and it
        # never contains ``scope``.
        assert not event_is_patch_coverable(event, fold_entities=None)
        assert SCOPE_ENTITY not in HISTORICAL_FOLD_ENTITIES


def test_a_lone_activate_batch_is_coverable_for_a_declaring_client():
    """The win, stated as the smallest batch that can carry it: a switch on a
    quiet runtime is one ``scope`` row plus its domain event."""

    batch = [_scope_patch_event(10, "ws_b", "realm_a"), _plain(11, "workspace.activated", workspace_id="ws_b")]
    events = [event for _, event in batch]

    assert batch_is_patch_coverable(events, fold_entities=_DECLARING)
    assert not batch_is_patch_coverable(events, fold_entities=_FIELDED)
    assert not batch_is_patch_coverable(events, fold_entities=None)
    assert batch_required_fold_tokens(events) == frozenset({SCOPE_ENTITY})

    # A realm switch that re-parks the workspace is FOUR events and two rows,
    # and it is coverable as one batch — which is the whole reason both pointers
    # ride every row.
    realm_switch = [
        _scope_patch_event(20, "ws_b", "realm_c"),
        _plain(21, "realm.activated", realm_id="realm_c"),
        _scope_patch_event(22, "ws_d", "realm_c"),
        _plain(23, "workspace.activated", workspace_id="ws_d"),
    ]
    assert batch_is_patch_coverable(
        [event for _, event in realm_switch], fold_entities=_DECLARING
    )


def test_one_uncovered_neighbour_still_demotes_the_switch():
    """The honest half of R-W0's risk row: the batch is rarely JUST the
    activate. A switch during office churn demotes on the neighbour, and the
    module's conservative-by-construction rule is what makes that safe."""

    switch = [
        _scope_patch_event(10, "ws_b", "realm_a"),
        _plain(11, "workspace.activated", workspace_id="ws_b"),
    ]
    for neighbour_type in ("state.reconciled", "task.transition", "persona_assignment.closed"):
        events = [event for _, event in switch] + [_plain(12, neighbour_type)[1]]
        assert not batch_is_patch_coverable(events, fold_entities=_DECLARING)
        assert batch_required_fold_tokens(events) is None


def test_required_tokens_and_the_boolean_agree_on_the_new_vocabulary():
    """The split gate's equivalence, extended to WS1's shapes.

    ``test_fold_variants_promotion`` pins this over the pre-WS1 vocabulary. A
    new event kind that the two forms answer differently would promote or demote
    on a rule the classifier does not hold, and the bare-patch path would still
    look right in every homogeneous test — so the new shapes are re-checked
    here rather than assumed to inherit it.
    """

    shapes = [
        _scope_patch_event(1, "ws_a", "realm_a")[1],
        _scope_patch_event(2, None, None)[1],
        _plain(3, "workspace.activated", workspace_id="ws_a")[1],
        _plain(4, "workspace.activated", cleared=True)[1],
        _plain(5, "realm.activated", realm_id="realm_a")[1],
        _plain(6, "realm.activated", cleared=True)[1],
    ]
    declarations = [None, frozenset(), HISTORICAL_FOLD_ENTITIES, _FIELDED, _DECLARING, frozenset({SCOPE_ENTITY})]
    for event in shapes:
        required = event_required_fold_tokens(event)
        for declared in declarations:
            expected = event_is_patch_coverable(event, fold_entities=declared)
            actual = required is not None and required <= normalize_fold_entities(declared)
            assert actual == expected, (event.type, event.payload, sorted(normalize_fold_entities(declared)))


def test_a_cleared_pointer_is_a_null_on_the_wire_not_an_absent_key(monkeypatch):
    """``harness workspace use --clear`` is a real state, and it must reach the
    client as ``null``. An absent key would leave a fold that merges present keys
    holding the departed pointer forever — the row would say nothing where it
    means "nothing".

    Driven through the REAL emitter, and that is the whole point of the test.
    An earlier version asserted on ``build_state_patch``'s output, which is the
    layer BELOW the one that decides what goes in ``changed`` — so a producer
    that filtered its nulls out on the way in was invisible to it. The mutation
    gate found exactly that (``iws-ws1-a-cleared-pointer-leaves-the-row-as-an-
    absent-key`` SURVIVED), which is the assertion moving one layer up.
    """

    from agent_runtime import state_patches as sp
    from agent_runtime.config import load_agent_runtime_config
    from agent_runtime.events import EventLog

    def _loader(*args, **kwargs):
        cfg = load_agent_runtime_config(*args, **kwargs)
        cfg.read_model.delta_patches = True
        return cfg

    monkeypatch.setattr(sp, "load_root_runtime_config", _loader)

    log = EventLog()
    assert sp.emit_scope_patch(log, active_workspace_id=None, active_realm_id=None)
    payload = _read_events(log)[-1]["payload"]
    assert set(payload["changed"]) == set(SCOPE_PATCH_FIELDS)
    assert payload["changed"] == {"active_workspace_id": None, "active_realm_id": None}
    assert payload["op"] == PATCH_OP_UPSERT
    assert payload["id"] == SCOPE_PATCH_ID
    # Still foldable: a two-null upsert carries a non-empty ``changed``.
    assert event_is_patch_coverable(
        Event(
            ts=_TS,
            type=STATE_PATCHED_EVENT_TYPE,
            task_id=None,
            run_id=None,
            persona_id=None,
            payload=payload,
        ),
        fold_entities=_DECLARING,
    )

    # And a HALF-clear keeps the surviving pointer beside the null — the pair is
    # one fact, so neither half may go missing.
    assert sp.emit_scope_patch(
        log, active_workspace_id=None, active_realm_id="realm_a"
    )
    half = _read_events(log)[-1]["payload"]
    assert half["changed"] == {
        "active_workspace_id": None,
        "active_realm_id": "realm_a",
    }


# --------------------------------------------------------------------------- #
# 2. Per-subscriber promotion (the S5 shape, reused)
# --------------------------------------------------------------------------- #
def test_two_subscribers_split_one_switch_batch():
    """The version-skew answer, and it is the mechanism that already exists.

    A WS2 launcher declaring ``scope`` and an older one that does not are in one
    room. The envelope carries both halves; the declarer resolves to the patch,
    the non-declarer to the demoted core — which is byte-for-byte what it got
    before WS1. No flag, no version gate (R-W0).
    """

    batch = [_scope_patch_event(10, "ws_b", "realm_a"), _plain(11, "workspace.activated", workspace_id="ws_b")]
    required = batch_required_fold_tokens(event for _, event in batch)
    assert required == frozenset({SCOPE_ENTITY})

    envelope = fold_variants_frame(
        patch=patch_batch_frame(batch, base_offset=9),
        core={"type": "delta", "core": {"active_workspace_id": "ws_b"}},
        required_tokens=required,
    )
    assert resolve_fold_variant(envelope, _DECLARING)["type"] == "patch"
    assert resolve_fold_variant(envelope, _FIELDED)["type"] == "delta"
    assert resolve_fold_variant(envelope, None)["type"] == "delta"
    for declared in (_DECLARING, _FIELDED, None, frozenset()):
        assert resolve_fold_variant(envelope, declared)["type"] != FOLD_VARIANTS_FRAME_TYPE


def test_the_hub_hands_each_subscriber_its_own_half():
    """The same claim end-to-end, over the real ``StreamHub`` with two real
    subscriptions and two real pumps — because the resolver being right is not
    the same fact as the fan-out calling it.

    The S5 test shape (``test_fold_variants_promotion.py`` §5), reused with WS1's
    batch: a WS2 launcher and a pre-WS2 one on the same runtime, one switch.
    """

    batch = [
        _scope_patch_event(10, "ws_b", "realm_a"),
        _plain(11, "workspace.activated", workspace_id="ws_b"),
    ]
    envelope = fold_variants_frame(
        patch=patch_batch_frame(batch, base_offset=9),
        core={"type": "delta", "core": {"active_workspace_id": "ws_b"}},
        required_tokens=batch_required_fold_tokens(event for _, event in batch),
    )
    released = threading.Event()

    def _source():
        yield {"type": "hydrate", "core": {}}
        yield envelope
        released.wait(5.0)

    hub = StreamHub(_source)
    ws2: list[dict] = []
    older: list[dict] = []
    try:
        hub.subscribe("ws2", sink=ws2.append, declared=_DECLARING)
        hub.subscribe("older", sink=older.append, declared=_FIELDED)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if len(ws2) >= 2 and len(older) >= 2:
                break
            time.sleep(0.02)
    finally:
        released.set()
        hub.stop()

    assert [frame["type"] for frame in ws2[:2]] == ["hydrate", "patch"]
    assert [frame["type"] for frame in older[:2]] == ["hydrate", "delta"]
    # The declarer's half really is the switch, not an empty promotion.
    ((row,)) = ws2[1]["patches"]
    assert (row["entity"], row["id"]) == (SCOPE_ENTITY, SCOPE_PATCH_ID)
    assert row["changed"]["active_workspace_id"] == "ws_b"
    # And neither of them ever saw the envelope.
    assert all(frame["type"] != FOLD_VARIANTS_FRAME_TYPE for frame in ws2 + older)


# --------------------------------------------------------------------------- #
# 3. The producer
# --------------------------------------------------------------------------- #
def test_set_active_emits_the_scope_row_beside_its_domain_event(monkeypatch):
    """``WorkspaceStore.set_active`` is the chokepoint, so the row leaves from
    inside the write — same drain, one batch, and the event free-rides."""

    from agent_runtime import state_patches as sp
    from agent_runtime.config import load_agent_runtime_config
    from agent_runtime.events import EventLog
    from agent_runtime.store import RealmStore, WorkspaceStore

    def _loader(*args, **kwargs):
        cfg = load_agent_runtime_config(*args, **kwargs)
        cfg.read_model.delta_patches = True
        return cfg

    monkeypatch.setattr(sp, "load_root_runtime_config", _loader)

    log = EventLog()
    realms = RealmStore(event_log=log)
    workspaces = WorkspaceStore(event_log=log)
    realm = realms.create(name="Pilot Realm")
    first = workspaces.create(name="First", realm_id=realm.id)
    second = workspaces.create(name="Second", realm_id=realm.id)

    realms.set_active(realm.id)
    workspaces.set_active(first.id)
    before = _read_events(log)
    workspaces.set_active(second.id)
    after = _read_events(log)

    switch = after[len(before) :]
    kinds = [event["type"] for event in switch]
    assert kinds == [STATE_PATCHED_EVENT_TYPE, "workspace.activated"], kinds
    # The PATCH precedes the event it pairs with: an activate in the log is
    # always preceded by the row that expresses it.
    assert switch[0]["payload"]["entity"] == SCOPE_ENTITY
    assert switch[0]["payload"]["id"] == SCOPE_PATCH_ID
    assert switch[0]["payload"]["op"] == PATCH_OP_UPSERT
    # BOTH pointers, always — the realm did not move and rides anyway.
    assert switch[0]["payload"]["changed"] == {
        "active_workspace_id": second.id,
        "active_realm_id": realm.id,
    }

    # A REALM activate carries the workspace pointer for the same reason.
    realm_two = realms.create(name="Second Realm")
    baseline = _read_events(log)
    realms.set_active(realm_two.id)
    realm_switch = _read_events(log)[len(baseline) :]
    assert [event["type"] for event in realm_switch] == [
        STATE_PATCHED_EVENT_TYPE,
        "realm.activated",
    ]
    assert realm_switch[0]["payload"]["changed"] == {
        "active_workspace_id": second.id,
        "active_realm_id": realm_two.id,
    }

    # And the batch the two make is coverable for a declaring client.
    events = [
        Event(
            ts=_TS,
            type=event["type"],
            task_id=None,
            run_id=None,
            persona_id=None,
            payload=event["payload"],
        )
        for event in switch
    ]
    assert batch_is_patch_coverable(events, fold_entities=_DECLARING)
    assert not batch_is_patch_coverable(events, fold_entities=_FIELDED)


def test_a_refused_activation_emits_nothing(monkeypatch):
    """The superseded/duplicate arms return before the write. A patch there
    would tell every client the pointer moved when it did not."""

    from agent_runtime import state_patches as sp
    from agent_runtime.config import load_agent_runtime_config
    from agent_runtime.events import EventLog
    from agent_runtime.store import WorkspaceStore

    def _loader(*args, **kwargs):
        cfg = load_agent_runtime_config(*args, **kwargs)
        cfg.read_model.delta_patches = True
        return cfg

    monkeypatch.setattr(sp, "load_root_runtime_config", _loader)

    log = EventLog()
    workspaces = WorkspaceStore(event_log=log)
    first = workspaces.create(name="First")
    second = workspaces.create(name="Second")
    basis = "2026-09-01T12:00:00.000000Z"
    assert workspaces.set_active(first.id, issued_at=basis)["applied"] is True
    before = _read_events(log)

    # DUPLICATE: the exact same intent arriving twice (serve timeout -> CLI
    # fallback re-runs the argv).
    assert workspaces.set_active(first.id, issued_at=basis)["applied"] is False
    # SUPERSEDED: a stale intent draining late from a wedged child.
    stale = workspaces.set_active(second.id, issued_at="2026-08-31T12:00:00.000000Z")
    assert stale["applied"] is False
    assert stale["reason"] == "superseded"

    assert _read_events(log) == before


def _read_events(log) -> list[dict]:
    """The log's tail as plain dicts. ``EventLog`` has no path accessor — the
    rotation layer owns where the slices live — so this reads through the real
    ``tail`` the stream drains from."""

    return [
        {"type": event.type, "payload": dict(event.payload or {})}
        for event in log.tail(200)
    ]


# --------------------------------------------------------------------------- #
# 4. The cross-stack golden
# --------------------------------------------------------------------------- #
def test_scope_fixture_is_the_frame_the_producer_builds():
    """``patch_scope.json`` is the launcher's byte-identical copy, so this side
    must show the bytes are what the REAL ``patch_batch_frame`` produces — not
    merely that a hand-written file parses.

    The property worth pinning is that the row carries BOTH keys. A producer
    that shipped only the pointer that moved would leave a client assembling a
    pair from two frames, and the two could disagree at any point between them.
    """

    import hashlib

    name = "patch_scope.json"
    entries = dict(
        reversed(line.split("  ", 1))
        for line in (_FIXTURES / "MANIFEST.sha256").read_text(encoding="utf-8").strip().splitlines()
    )
    assert hashlib.sha256((_FIXTURES / name).read_bytes()).hexdigest() == entries[name], (
        f"{name} drifted from MANIFEST.sha256 (cross-stack pin)"
    )
    golden = json.loads((_FIXTURES / name).read_text(encoding="utf-8"))
    assert golden["type"] == "patch"
    assert golden["schema_version"] == 2
    assert "core" not in golden
    ((row,)) = golden["patches"]
    assert (row["entity"], row["id"], row["op"]) == (SCOPE_ENTITY, SCOPE_PATCH_ID, PATCH_OP_UPSERT)
    assert set(row["changed"]) == set(SCOPE_PATCH_FIELDS)
    assert golden["base_offset"] < row["seq"] <= golden["watermark"]["event_offset"]
    # ``coalesced_count`` is the WHOLE batch: the paired ``workspace.activated``
    # rides it and folds to nothing, which is what "covered" means.
    assert golden["coalesced_count"] == 2
    assert golden["coalesced_count"] > len(golden["patches"])

    batch = [
        (
            row["seq"],
            Event(
                ts=datetime(2026, 7, 16, 12, 20, 0, tzinfo=timezone.utc),
                type=STATE_PATCHED_EVENT_TYPE,
                task_id=None,
                run_id=None,
                persona_id=None,
                payload={
                    "entity": row["entity"],
                    "id": row["id"],
                    "op": row["op"],
                    "changed": row["changed"],
                },
            ),
        )
    ]
    live = patch_batch_frame(batch, base_offset=golden["base_offset"])
    assert set(live) == set(golden)
    assert [
        {key: value for key, value in entry.items() if key != "ts"}
        for entry in live["patches"]
    ] == [{key: value for key, value in entry.items() if key != "ts"} for entry in golden["patches"]]


def test_the_coverage_manifest_carries_the_two_new_chokepoints():
    """The launcher folds the same bytes, so the coverage table it reads must
    name the two events and the entity they ride."""

    manifest = json.loads(
        (_FIXTURES / "patch_coverage_manifest.json").read_text(encoding="utf-8")
    )
    assert {"workspace.activated", "realm.activated"} <= set(manifest["covered_domain_events"])
    scope_cases = [case for case in manifest["cases"] if case["entity"] == SCOPE_ENTITY]
    assert {case["chokepoint"] for case in scope_cases} == {
        "workspace.activated",
        "realm.activated",
    }
    for case in scope_cases:
        assert case["op"] == PATCH_OP_UPSERT
        assert case["foldable"] is True


def test_the_module_docstring_grep_is_not_the_only_census():
    """Guard against the census above silently matching nothing: the regex it
    depends on must find call sites in the file it claims to read."""

    import agent_runtime.snapshot as snapshot_module

    source = Path(snapshot_module.__file__).read_text(encoding="utf-8")
    assert len(re.findall(r"active_id\(\)", source)) == 7
    # The file really is the module the snapshot builder lives in.
    tree = ast.parse(source)
    names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert "build_snapshot" in names
