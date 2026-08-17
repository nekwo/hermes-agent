"""S7-A wire goldens: op-based state-patch stream frames (flagged, dark).

Covers the S7-A wire contract:

* flag off (default) → the stream is BYTE-IDENTICAL to today; every batch is a
  full-core delta frame, never a ``patch`` frame (the flag-off inertness golden);
* flag on + a coverable batch (steer/profile ``upsert``, incident/instance
  ``remove``) → a v2 ``patch`` frame carrying the op entries and NO core,
  base_offset chaining to the prior watermark;
* flag on + an UNCOVERED event in the batch (task transition's ``refresh`` op +
  its assignment-close fan-out, a reconcile, a planning-style mutation) → the
  honest fallback: a full-core delta frame;
* an explicit ``resync`` request → a full core even for a coverable batch.

Plus the cross-repo ``patch*.json`` fixtures + manifest pins.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_runtime.events import EVENT_PAYLOAD_LIMIT_BYTES, EventLog
from agent_runtime.models import Event
from agent_runtime.patch_coverage import (
    batch_is_patch_coverable,
    event_is_patch_coverable,
    state_patch_is_foldable,
)
from agent_runtime.persona_assignments import PersonaInstanceStore
from agent_runtime.serde import to_jsonable
from agent_runtime.state_patches import (
    PATCH_OP_REFRESH,
    PATCH_OP_REMOVE,
    PATCH_OP_UPSERT,
    STATE_PATCHED_EVENT_TYPE,
)
from agent_runtime.stream import (
    STREAM_PATCH_SCHEMA_VERSION,
    delta_batch_frame,
    patch_batch_frame,
    stream_frames,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "stream_frames"


@pytest.fixture
def set_delta_patches(monkeypatch):
    """Flip ``read_model.delta_patches`` for BOTH the producer chokepoints
    (root-pinned via ``load_root_runtime_config``) and the stream lane."""

    from agent_runtime import state_patches as sp
    from agent_runtime import stream as st
    from agent_runtime.config import load_agent_runtime_config

    def _apply(enabled: bool):
        def _loader(*args, **kwargs):
            cfg = load_agent_runtime_config(*args, **kwargs)
            cfg.read_model.delta_patches = enabled
            return cfg

        # The producer flag reader (_delta_patches_enabled) is pinned to the
        # ROOT config via load_root_runtime_config(); patch that symbol so the
        # fixture still injects the flag through the reader's actual loader.
        monkeypatch.setattr(sp, "load_root_runtime_config", _loader)
        monkeypatch.setattr(st, "delta_patches_enabled", lambda config=None: enabled)

    return _apply


def _op_event(offset: int, entity: str, entity_id: str, op: str, changed: dict | None = None) -> tuple[int, Event]:
    payload = {"entity": entity, "id": entity_id, "op": op}
    if changed is not None:
        payload["changed"] = changed
    return offset, Event(
        ts=datetime(2026, 7, 16, 12, 0, 1, tzinfo=timezone.utc),
        type=STATE_PATCHED_EVENT_TYPE,
        task_id=None, run_id=None, persona_id=None, payload=payload,
    )


def _plain_event(offset: int, event_type: str) -> tuple[int, Event]:
    return offset, Event(
        ts=datetime(2026, 7, 16, 12, 0, 2, tzinfo=timezone.utc),
        type=event_type, task_id="task_x", run_id=None, persona_id=None,
        payload={"fingerprint": "fp"},
    )


# --------------------------------------------------------------------------- #
# Coverage classifier (pure) — op-based
# --------------------------------------------------------------------------- #
def test_upsert_and_remove_are_foldable_refresh_is_not():
    assert state_patch_is_foldable(
        {"entity": "persona_instance", "id": "p", "op": "upsert", "changed": {"model": "x", "effective_model": "x"}}
    )
    assert state_patch_is_foldable({"entity": "incident", "id": "i", "op": "remove"})
    # A refresh is the accounted "too big to fold" → NOT foldable (forces a core).
    assert not state_patch_is_foldable({"entity": "task", "id": "t", "op": "refresh"})
    # An upsert with an empty/absent changed is malformed → not foldable.
    assert not state_patch_is_foldable({"entity": "persona_instance", "id": "p", "op": "upsert", "changed": {}})
    assert not state_patch_is_foldable({"entity": "persona_instance", "id": "p", "op": "upsert"})
    # A payload with no op is not foldable.
    assert not state_patch_is_foldable({"entity": "x", "id": "y", "changed": {"a": 1}})


def test_covered_batch_vs_uncovered_batch():
    steer = _op_event(10, "persona_instance", "p", PATCH_OP_UPSERT, {"steered_by": ["a"], "spawned_by": "a"})
    steer_domain = _plain_event(11, "persona_instance.steered")
    assert batch_is_patch_coverable([e for _, e in [steer, steer_domain]])
    # A remove batch (incident close + its domain event) is coverable.
    close = _op_event(12, "incident", "i", PATCH_OP_REMOVE)
    close_domain = _plain_event(13, "incident.closed")
    assert batch_is_patch_coverable([e for _, e in [close, close_domain]])
    # A single uncovered event (reconcile / task transition / refresh) demotes it.
    reconcile = _plain_event(14, "state.reconciled")
    assert not batch_is_patch_coverable([e for _, e in [steer, reconcile]])
    refresh = _op_event(15, "task", "t", PATCH_OP_REFRESH)
    assert not batch_is_patch_coverable([e for _, e in [steer, refresh]])
    assert not event_is_patch_coverable(_plain_event(16, "task.transition")[1])
    # persona_assignment domain events are NOT foldable (no keyed section).
    assert not event_is_patch_coverable(_plain_event(17, "persona_assignment.closed")[1])
    assert not batch_is_patch_coverable([])


# --------------------------------------------------------------------------- #
# Frame builders + selector
# --------------------------------------------------------------------------- #
def test_patch_batch_frame_shape():
    batch = [_op_event(382, "persona_instance", "personainst_child", PATCH_OP_UPSERT,
                       {"steered_by": ["personainst_parent"], "spawned_by": "personainst_parent"})]
    frame = patch_batch_frame(batch, base_offset=191)
    assert frame["type"] == "patch"
    assert frame["schema_version"] == STREAM_PATCH_SCHEMA_VERSION == 2
    assert "core" not in frame, "a patch frame must NOT carry a full core"
    assert frame["base_offset"] == 191
    assert frame["watermark"]["event_offset"] == 382
    assert frame["coalesced_count"] == 1
    ((patch,)) = frame["patches"]
    assert patch["entity"] == "persona_instance"
    assert patch["id"] == "personainst_child"
    assert patch["op"] == "upsert"
    assert patch["changed"] == {"steered_by": ["personainst_parent"], "spawned_by": "personainst_parent"}
    assert patch["seq"] == 382
    wire = json.dumps(to_jsonable(frame), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(wire) <= EVENT_PAYLOAD_LIMIT_BYTES


def test_patch_batch_frame_carries_remove_op():
    batch = [_op_event(400, "incident", "inc_x", PATCH_OP_REMOVE)]
    ((patch,)) = patch_batch_frame(batch, base_offset=399)["patches"]
    assert patch["op"] == "remove"
    assert "changed" not in patch


# --------------------------------------------------------------------------- #
# End-to-end stream_frames (seeded root)
# --------------------------------------------------------------------------- #
def _instances(store: PersonaInstanceStore):
    parent = store.create_free_floating("profile:parent")
    child = store.create_free_floating("profile:child")
    return parent, child


def _stream(**kwargs):
    return stream_frames(
        poll_interval_seconds=0.01, heartbeat_interval_seconds=60, delta_debounce_seconds=0, **kwargs
    )


def test_stream_flag_off_never_emits_patch(set_delta_patches, isolate_agent_runtime_root):
    set_delta_patches(False)
    store = PersonaInstanceStore()
    parent, child = _instances(store)

    frames = _stream(max_frames=2)
    hydrate = next(frames)
    assert hydrate["type"] == "hydrate"
    assert "delta_patches" not in hydrate, "flag-off hydrate carries no patch marker"
    store.set_parents(child.id, [parent.id])
    frame = next(frames)
    assert frame["type"] == "delta" and "core" in frame, "flag off → full-core delta, never a patch"


def test_stream_flag_on_steer_emits_patch_frame(set_delta_patches, isolate_agent_runtime_root):
    set_delta_patches(True)
    store = PersonaInstanceStore()
    parent, child = _instances(store)

    frames = _stream(max_frames=2)
    hydrate = next(frames)
    assert hydrate["type"] == "hydrate"
    assert hydrate.get("delta_patches") is True, "flag-on hydrate signals the fold client to retain the base"

    store.set_parents(child.id, [parent.id])
    patch_frame = next(frames)
    assert patch_frame["type"] == "patch", f"expected a patch frame, got {patch_frame['type']}"
    assert "core" not in patch_frame
    assert patch_frame["base_offset"] == hydrate["watermark"]["event_offset"]
    steer_patches = [p for p in patch_frame["patches"] if p["entity"] == "persona_instance"]
    assert steer_patches, "the steer patch rides the patch frame"
    assert child.id in {p["id"] for p in steer_patches}
    assert steer_patches[0]["op"] == "upsert"
    assert set(steer_patches[0]["changed"]) <= {"steered_by", "spawned_by"}


def test_stream_flag_on_profile_update_emits_patch_frame(set_delta_patches, isolate_agent_runtime_root):
    set_delta_patches(True)
    store = PersonaInstanceStore()
    instance = store.create_free_floating("profile:reviewer")

    frames = _stream(max_frames=2)
    assert next(frames)["type"] == "hydrate"
    store.update_profile(instance.id, model="claude-opus-4-8", provider="anthropic")
    frame = next(frames)
    assert frame["type"] == "patch", f"expected a patch frame, got {frame['type']}"
    ((patch,)) = [p for p in frame["patches"] if p["entity"] == "persona_instance"]
    assert patch["op"] == "upsert"
    # The recomputed derived wire fields ride (the S7-A fidelity fix).
    assert patch["changed"]["effective_model"] == "claude-opus-4-8"
    assert patch["changed"]["model_is_override"] is True


def test_stream_flag_on_task_transition_falls_back_to_full_core(set_delta_patches, isolate_agent_runtime_root):
    import agent_runtime.models as runtime_models

    assert not hasattr(runtime_models, "Task")


def test_stream_resync_first_batch_is_full_core(set_delta_patches, isolate_agent_runtime_root):
    set_delta_patches(True)
    store = PersonaInstanceStore()
    parent, child = _instances(store)

    frames = _stream(max_frames=2, resync=True)
    assert next(frames)["type"] == "hydrate"
    store.set_parents(child.id, [parent.id])
    frame = next(frames)
    assert frame["type"] == "delta" and "core" in frame, (
        "resync forces the first post-hydrate batch to a full core even though it is coverable"
    )


# --------------------------------------------------------------------------- #
# Cross-repo fixtures (byte-pinned; launcher mirrors them)
# --------------------------------------------------------------------------- #
_PATCH_FIXTURES = {
    "patch.json": ("upsert", {"seq", "ts", "entity", "id", "op", "changed"}),
    "patch_upsert_profile.json": ("upsert", {"seq", "ts", "entity", "id", "op", "changed"}),
    "patch_remove.json": ("remove", {"seq", "ts", "entity", "id", "op"}),
}


def test_patch_fixtures_manifest_and_shape():
    manifest = (FIXTURES / "MANIFEST.sha256").read_text(encoding="utf-8")
    entries = dict(reversed(line.split("  ", 1)) for line in manifest.strip().splitlines())
    for name, (op, keys) in _PATCH_FIXTURES.items():
        assert name in entries, f"{name} missing from MANIFEST.sha256"
        actual = hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()
        assert actual == entries[name], f"{name} drifted from MANIFEST.sha256 (cross-stack pin)"

        frame = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        assert frame["type"] == "patch"
        assert frame["schema_version"] == 2
        assert "core" not in frame
        assert {"base_offset", "patches", "coalesced_count", "watermark"} <= set(frame)
        assert frame["watermark"]["event_offset"] > frame["base_offset"]
        ((patch,)) = frame["patches"]
        assert patch["op"] == op
        assert set(patch) == keys


def test_delete_gesture_fixture_is_the_frame_the_producer_builds():
    """The office fold-promotion milestone's cross-stack golden (O-H3).

    The launcher commits byte-identical bytes and folds them through its real
    read-model pipeline, so this side must show the same bytes are what the
    REAL ``patch_batch_frame`` produces for the real delete-gesture batch — not
    merely that a hand-written file parses.

    The property that makes it worth pinning at all is the pairing: ONE frame,
    ONE watermark, BOTH removes. The persona row and the office row leave
    together, and a client that applied only one of them at this watermark would
    hold a roster or a canvas that disagrees with the store forever. That is
    exactly what the office sink's old filtered forwarding did (§V6), which is
    why this fixture carries the mixed batch rather than an office row alone.
    """

    from datetime import datetime, timezone

    from agent_runtime.models import Event
    from agent_runtime.stream import patch_batch_frame

    name = "patch_delete_gesture.json"
    entries = dict(
        reversed(line.split("  ", 1))
        for line in (FIXTURES / "MANIFEST.sha256").read_text(encoding="utf-8").strip().splitlines()
    )
    assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == entries[name], (
        f"{name} drifted from MANIFEST.sha256 (cross-stack pin)"
    )
    golden = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert golden["type"] == "patch"
    assert golden["schema_version"] == 2
    assert "core" not in golden
    rows = golden["patches"]
    assert [(row["entity"], row["op"]) for row in rows] == [
        ("persona_instance", "remove"),
        ("office_actor", "remove"),
    ]
    # Both rows sit inside the span the frame claims, and the office id is the
    # workspace-scoped identity the sink's prefix match depends on.
    assert golden["base_offset"] < rows[0]["seq"] < rows[1]["seq"]
    assert rows[1]["seq"] <= golden["watermark"]["event_offset"]
    assert rows[1]["id"].partition("/")[0] == "ws_office_pilot"
    # ``coalesced_count`` is the WHOLE batch — the two paired domain events ride
    # it and fold to nothing, which is what "covered" means.
    assert golden["coalesced_count"] == 4
    assert golden["coalesced_count"] > len(rows)

    # The live builder, over the same rows: every key the golden carries must be
    # one the producer still emits, or the launcher is folding a shape hermes no
    # longer sends.
    ts = datetime(2026, 7, 16, 12, 20, 0, tzinfo=timezone.utc)
    batch = [
        (
            row["seq"],
            Event(
                ts=ts,
                type="state.patched",
                task_id=None,
                run_id=None,
                persona_id=None,
                payload={"entity": row["entity"], "id": row["id"], "op": row["op"]},
            ),
        )
        for row in rows
    ]
    live = patch_batch_frame(batch, base_offset=golden["base_offset"])
    assert set(live) == set(golden)
    assert [
        {key: value for key, value in row.items() if key != "ts"} for row in live["patches"]
    ] == [{key: value for key, value in row.items() if key != "ts"} for row in rows]

    # And the classifier really does promote this batch for a widened client
    # while refusing it for a fielded one — the fixture is the milestone's
    # evidence, so it must not be green against a runtime that promotes nothing.
    from agent_runtime.patch_coverage import batch_is_patch_coverable

    events = [event for _, event in batch] + [
        Event(ts=ts, type="persona_instance.retired", task_id=None, run_id=None, persona_id=None, payload={}),
        Event(ts=ts, type="office.actor.removed", task_id=None, run_id=None, persona_id=None, payload={}),
    ]
    widened = frozenset(
        {"persona_instance", "incident", "office_actor", "office_actor_lifecycle"}
    )
    assert batch_is_patch_coverable(events, fold_entities=widened)
    assert not batch_is_patch_coverable(
        events, fold_entities=frozenset({"persona_instance", "incident", "office_actor"})
    )


def test_office_surface_fixture_is_the_frame_the_producer_builds():
    """The office write-verbs milestone's cross-stack golden (WV-H3).

    The launcher commits byte-identical bytes and folds them through its real
    read-model pipeline, so this side must show the same bytes are what the REAL
    ``patch_batch_frame`` produces for the real folder-change batch.

    The property worth pinning is the SUBSET. Its ``office_actor`` sibling ships
    a complete row and the launcher REPLACES; this one ships exactly the three
    fields ``update_surface`` moves and the launcher MERGES. A frame carrying a
    fourth key would be a producer quietly taking ownership of state the actor
    folds maintain — so the key set is asserted as an equality against the
    producer's own constant, not as a containment.

    And the token gate is asserted on this batch in BOTH directions, because a
    coverage assertion that only shows promotion is green against a classifier
    that promotes everything.
    """

    from datetime import datetime, timezone

    from agent_runtime.models import Event
    from agent_runtime.patch_coverage import (
        OFFICE_SURFACE_FOLD_CAPABILITY,
        batch_is_patch_coverable,
    )
    from agent_runtime.state_patches import (
        OFFICE_SURFACE_ENTITY,
        OFFICE_SURFACE_PATCH_FIELDS,
    )
    from agent_runtime.stream import patch_batch_frame

    name = "patch_office_surface.json"
    entries = dict(
        reversed(line.split("  ", 1))
        for line in (FIXTURES / "MANIFEST.sha256").read_text(encoding="utf-8").strip().splitlines()
    )
    assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == entries[name], (
        f"{name} drifted from MANIFEST.sha256 (cross-stack pin)"
    )
    golden = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert golden["type"] == "patch"
    assert golden["schema_version"] == 2
    assert "core" not in golden
    ((row,)) = golden["patches"]
    assert (row["entity"], row["op"]) == (OFFICE_SURFACE_ENTITY, "upsert")
    # The id is the BARE workspace, not the actor fold's ``workspace/key``
    # composite: a workspace is unique on its own, and a composite here would
    # address a row the launcher's office section does not have.
    assert row["id"] == "ws_office_pilot"
    assert set(row["changed"]) == set(OFFICE_SURFACE_PATCH_FIELDS)
    assert golden["base_offset"] < row["seq"] <= golden["watermark"]["event_offset"]
    # The paired domain event rides the batch and folds to nothing — which is
    # what "covered" means, and why the count exceeds the row.
    assert golden["coalesced_count"] == 2
    assert golden["coalesced_count"] > len(golden["patches"])

    ts = datetime(2026, 7, 16, 12, 20, 0, tzinfo=timezone.utc)
    payload = {
        "entity": row["entity"],
        "id": row["id"],
        "op": row["op"],
        "changed": row["changed"],
    }
    batch = [
        (
            row["seq"],
            Event(
                ts=ts,
                type="state.patched",
                task_id=None,
                run_id=None,
                persona_id=None,
                payload=payload,
            ),
        )
    ]
    live = patch_batch_frame(batch, base_offset=golden["base_offset"])
    assert set(live) == set(golden)
    assert [
        {key: value for key, value in entry.items() if key != "ts"}
        for entry in live["patches"]
    ] == [{key: value for key, value in entry.items() if key != "ts"} for entry in golden["patches"]]

    events = [event for _, event in batch] + [
        Event(
            ts=ts,
            type="office.surface.updated",
            task_id=None,
            run_id=None,
            persona_id=None,
            payload={},
        )
    ]
    # The widest declaration any FIELDED launcher sends still demotes: the token
    # is what changed, not "the office got covered".
    fielded = frozenset(
        {
            "persona_instance",
            "incident",
            "office_actor",
            "office_actor_lifecycle",
            "persona_instance_create",
        }
    )
    assert not batch_is_patch_coverable(events, fold_entities=fielded)
    assert batch_is_patch_coverable(
        events,
        fold_entities=fielded | {OFFICE_SURFACE_ENTITY, OFFICE_SURFACE_FOLD_CAPABILITY},
    )


def test_coverage_manifest_agrees_with_classifier():
    """The cross-repo coverage golden (plan §S7-A, item 3): the byte-pinned
    ``patch_coverage_manifest.json`` (the launcher folds the SAME bytes) must
    agree with hermes's live op/coverage classifier — the lockstep guard."""

    from agent_runtime.patch_coverage import COVERED_DOMAIN_EVENT_TYPES
    from agent_runtime.state_patches import FOLDABLE_PATCH_OPS

    name = "patch_coverage_manifest.json"
    entries = dict(
        reversed(line.split("  ", 1))
        for line in (FIXTURES / "MANIFEST.sha256").read_text(encoding="utf-8").strip().splitlines()
    )
    assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == entries[name], (
        "patch_coverage_manifest.json drifted from MANIFEST.sha256 (cross-stack pin)"
    )
    manifest = json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    assert set(manifest["foldable_ops"]) == set(FOLDABLE_PATCH_OPS)
    assert set(manifest["covered_domain_events"]) == set(COVERED_DOMAIN_EVENT_TYPES)
    for case in manifest["cases"]:
        op = case["op"]
        if op is None:
            continue
        payload = {"entity": case["entity"], "id": "x", "op": op}
        if op == "upsert":
            payload["changed"] = {"field": "value"}
        assert state_patch_is_foldable(payload) is case["foldable"], case
        # Every covered domain event has a paired op case; a chokepoint-less case
        # (planning.write / incident.opened) is correctly NOT foldable.
        if case["chokepoint"].endswith((".steered", ".profile_updated", ".reaped", ".closed")):
            assert case["chokepoint"] in COVERED_DOMAIN_EVENT_TYPES


def test_every_covered_domain_event_is_registered_or_declared_historical():
    """S66: a fold vocabulary must not silently outlive its producer.

    ``COVERED_DOMAIN_EVENT_TYPES`` names domain events the launcher classifies.
    S65 de-registered two of them (``persona_instance.reaped``,
    ``incident.closed``) with their last writers, and the flat set could not
    tell those apart from the live entries — the same "wire that can only report
    a constant" class the ledger has hit repeatedly, one level down.

    The partition is now explicit and gated BOTH ways: a LIVE entry must have a
    registered contract (so a new entry cannot be invented without a producer,
    and a de-registration cannot be done out from under one), and a HISTORICAL
    entry must NOT be registered (so a resurrected contract forces the entry
    back onto the live half instead of sitting in the compatibility bucket).
    """

    from agent_runtime.decision_contract_registry import event_catalog
    from agent_runtime.patch_coverage import (
        COVERED_DOMAIN_EVENT_TYPES,
        HISTORICAL_COVERED_DOMAIN_EVENT_TYPES,
        LIVE_COVERED_DOMAIN_EVENT_TYPES,
    )

    catalog = set(event_catalog())
    # Non-vacuity: the catalog really is populated and really does overlap.
    assert len(catalog) > 10, catalog
    assert LIVE_COVERED_DOMAIN_EVENT_TYPES

    assert LIVE_COVERED_DOMAIN_EVENT_TYPES <= catalog, (
        "a covered domain event lost its registered contract without being "
        "moved to HISTORICAL_COVERED_DOMAIN_EVENT_TYPES: "
        f"{sorted(LIVE_COVERED_DOMAIN_EVENT_TYPES - catalog)}"
    )
    assert HISTORICAL_COVERED_DOMAIN_EVENT_TYPES.isdisjoint(catalog), (
        "a historical fold entry is registered again — promote it back to "
        "LIVE_COVERED_DOMAIN_EVENT_TYPES: "
        f"{sorted(HISTORICAL_COVERED_DOMAIN_EVENT_TYPES & catalog)}"
    )
    # The two halves partition the exported set exactly — no third bucket, no
    # member that belongs to neither.
    assert (
        LIVE_COVERED_DOMAIN_EVENT_TYPES | HISTORICAL_COVERED_DOMAIN_EVENT_TYPES
    ) == COVERED_DOMAIN_EVENT_TYPES
    assert LIVE_COVERED_DOMAIN_EVENT_TYPES.isdisjoint(
        HISTORICAL_COVERED_DOMAIN_EVENT_TYPES
    )
