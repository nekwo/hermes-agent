"""S6 wire goldens: state-carrying field-patch stream frames (flagged, dark).

Covers the four acceptance axes from the plan's S6 wire half:

* flag off (default) → the stream is BYTE-IDENTICAL to today; every batch is a
  full-core delta frame, never a ``patch`` frame (the flag-off inertness golden);
* flag on + a coverable (steer) batch → a v2 ``patch`` frame carrying the
  field-patch and NO core, base_offset chaining to the prior watermark;
* flag on + an UNCOVERED event in the batch (task transition / reconcile /
  planning-style mutation) → the honest fallback: a full-core delta frame;
* an explicit ``resync`` request → a full core even for a coverable batch.

Plus the cross-repo ``patch.json`` fixture shape + manifest pin.
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
from agent_runtime.state_patches import STATE_PATCHED_EVENT_TYPE
from agent_runtime.stream import (
    STREAM_PATCH_SCHEMA_VERSION,
    delta_batch_frame,
    patch_batch_frame,
    select_batch_frame,
    stream_frames,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "stream_frames"


@pytest.fixture
def set_delta_patches(monkeypatch):
    """Flip ``read_model.delta_patches`` for BOTH the producer chokepoints and
    the stream lane (both read it through ``load_agent_runtime_config``)."""

    from agent_runtime import state_patches as sp
    from agent_runtime import stream as st
    from agent_runtime.config import load_agent_runtime_config

    def _apply(enabled: bool):
        def _loader(*args, **kwargs):
            cfg = load_agent_runtime_config(*args, **kwargs)
            cfg.read_model.delta_patches = enabled
            return cfg

        monkeypatch.setattr(sp, "load_agent_runtime_config", _loader)
        monkeypatch.setattr(st, "delta_patches_enabled", lambda config=None: enabled)

    return _apply


def _patch_event(offset: int, entity: str, entity_id: str, changed: dict) -> tuple[int, Event]:
    return offset, Event(
        ts=datetime(2026, 7, 16, 12, 0, 1, tzinfo=timezone.utc),
        type=STATE_PATCHED_EVENT_TYPE,
        task_id=None,
        run_id=None,
        persona_id=None,
        payload={"entity": entity, "id": entity_id, "changed": changed},
    )


def _plain_event(offset: int, event_type: str) -> tuple[int, Event]:
    return offset, Event(
        ts=datetime(2026, 7, 16, 12, 0, 2, tzinfo=timezone.utc),
        type=event_type,
        task_id="task_x",
        run_id=None,
        persona_id=None,
        payload={"fingerprint": "fp"},
    )


# --------------------------------------------------------------------------- #
# Coverage classifier (pure)
# --------------------------------------------------------------------------- #
def test_steer_patch_is_foldable():
    assert state_patch_is_foldable(
        {"entity": "persona_instance", "id": "p", "changed": {"steered_by": ["a"], "spawned_by": "a"}}
    )


def test_non_steer_patches_are_not_foldable():
    # task/incident/profile patches carry hermes-row derivations a raw merge
    # can't reproduce → uncovered for S6 (full-core lane).
    assert not state_patch_is_foldable({"entity": "task", "id": "t", "changed": {"state": "done"}})
    assert not state_patch_is_foldable({"entity": "incident", "id": "i", "changed": {"closed_at": "x"}})
    assert not state_patch_is_foldable(
        {"entity": "persona_instance", "id": "p", "changed": {"model": "claude-opus-4-8"}}
    )
    # A steer patch mixed with a non-foldable field is not foldable as a whole.
    assert not state_patch_is_foldable(
        {"entity": "persona_instance", "id": "p", "changed": {"steered_by": ["a"], "model": "x"}}
    )


def test_covered_batch_vs_uncovered_batch():
    steer_patch = _patch_event(10, "persona_instance", "p", {"steered_by": ["a"], "spawned_by": "a"})
    steer_domain = _plain_event(11, "persona_instance.steered")
    assert batch_is_patch_coverable([e for _, e in [steer_patch, steer_domain]])
    # A single uncovered event demotes the whole batch.
    reconcile = _plain_event(12, "state.reconciled")
    assert not batch_is_patch_coverable([e for _, e in [steer_patch, reconcile]])
    task_transition = _plain_event(13, "task.transition")
    assert not event_is_patch_coverable(task_transition[1])
    # Empty batch is never coverable (nothing to ship).
    assert not batch_is_patch_coverable([])


# --------------------------------------------------------------------------- #
# Frame builders + selector
# --------------------------------------------------------------------------- #
def test_patch_batch_frame_shape():
    batch = [_patch_event(382, "persona_instance", "personainst_child", {"steered_by": ["personainst_parent"], "spawned_by": "personainst_parent"})]
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
    assert patch["changed"] == {"steered_by": ["personainst_parent"], "spawned_by": "personainst_parent"}
    assert patch["seq"] == 382
    # Fits the 4 KB EventLog cap by construction (it IS a log entry).
    wire = json.dumps(to_jsonable(frame), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(wire) <= EVENT_PAYLOAD_LIMIT_BYTES


def test_select_batch_frame_flag_off_is_full_core():
    batch = [_patch_event(10, "persona_instance", "p", {"steered_by": ["a"], "spawned_by": "a"})]
    frame = select_batch_frame(batch, base_offset=0, delta_patches=False, snapshot={"stub": True})
    assert frame["type"] == "delta"
    assert frame["core"] == {"stub": True}


def test_select_batch_frame_covered_is_patch():
    batch = [_patch_event(10, "persona_instance", "p", {"steered_by": ["a"], "spawned_by": "a"})]
    frame = select_batch_frame(batch, base_offset=5, delta_patches=True, snapshot={"stub": True})
    assert frame["type"] == "patch"
    assert "core" not in frame
    assert frame["base_offset"] == 5


def test_select_batch_frame_uncovered_falls_back_to_full_core():
    batch = [
        _patch_event(10, "persona_instance", "p", {"steered_by": ["a"], "spawned_by": "a"}),
        _plain_event(11, "task.transition"),
    ]
    frame = select_batch_frame(batch, base_offset=0, delta_patches=True, snapshot={"stub": True})
    assert frame["type"] == "delta", "one uncovered event → the whole batch is a full core"
    assert frame["core"] == {"stub": True}


def test_select_batch_frame_resync_forces_full_core():
    batch = [_patch_event(10, "persona_instance", "p", {"steered_by": ["a"], "spawned_by": "a"})]
    frame = select_batch_frame(
        batch, base_offset=0, delta_patches=True, resync=True, snapshot={"stub": True}
    )
    assert frame["type"] == "delta", "an explicit resync request re-baselines with a full core"
    assert frame["core"] == {"stub": True}


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
    # Steer AFTER the hydrate so the write is emitted as a delta (not baked in).
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

    # Steer after the hydrate — its steered + state.patched events form one
    # coverable batch → a patch frame.
    store.set_parents(child.id, [parent.id])
    patch_frame = next(frames)
    assert patch_frame["type"] == "patch", f"expected a patch frame, got {patch_frame['type']}"
    assert "core" not in patch_frame
    assert patch_frame["base_offset"] == hydrate["watermark"]["event_offset"], (
        "the patch batch applies from the hydrate watermark (contiguous, no gap)"
    )
    steer_patches = [p for p in patch_frame["patches"] if p["entity"] == "persona_instance"]
    assert steer_patches, "the steer patch rides the patch frame"
    assert child.id in {p["id"] for p in steer_patches}
    assert set(steer_patches[0]["changed"]) <= {"steered_by", "spawned_by"}


def test_stream_flag_on_task_transition_falls_back_to_full_core(set_delta_patches, isolate_agent_runtime_root):
    from agent_runtime.states import TaskState
    from agent_runtime.store import TaskStore
    from agent_runtime.models import Task
    from hermes_time import now

    set_delta_patches(True)
    tasks = TaskStore()
    t = Task(
        id="task_patch", title="t", description="d", state=TaskState.RUNNING,
        created_at=now(), updated_at=now(), requested_by="tony",
        affected_repos=["hermes-agent"], current_stage_id="s1",
    )
    tasks.create(t)

    frames = _stream(max_frames=2)
    assert next(frames)["type"] == "hydrate"
    # A transition batch carries task.transition (uncovered) → full core, even
    # though the producer ALSO logs a state.patched(task) alongside it.
    t.state = TaskState.BLOCKED
    tasks.update(t, actor="harness", reason="blocked")
    frame = next(frames)
    assert frame["type"] == "delta" and "core" in frame, (
        "task transition is an uncovered batch → full-core delta, never a patch"
    )


def test_stream_resync_first_batch_is_full_core(set_delta_patches, isolate_agent_runtime_root):
    set_delta_patches(True)
    store = PersonaInstanceStore()
    parent, child = _instances(store)

    frames = _stream(max_frames=2, resync=True)
    assert next(frames)["type"] == "hydrate"
    store.set_parents(child.id, [parent.id])  # a coverable steer batch
    frame = next(frames)
    assert frame["type"] == "delta" and "core" in frame, (
        "resync forces the first post-hydrate batch to a full core even though it is coverable"
    )


# --------------------------------------------------------------------------- #
# Cross-repo fixture (byte-pinned; launcher mirrors it)
# --------------------------------------------------------------------------- #
def test_patch_fixture_manifest_and_shape():
    manifest = (FIXTURES / "MANIFEST.sha256").read_text(encoding="utf-8")
    entries = dict(reversed(line.split("  ", 1)) for line in manifest.strip().splitlines())
    assert "patch.json" in entries
    actual = hashlib.sha256((FIXTURES / "patch.json").read_bytes()).hexdigest()
    assert actual == entries["patch.json"], "patch.json drifted from MANIFEST.sha256 (cross-stack pin)"

    frame = json.loads((FIXTURES / "patch.json").read_text(encoding="utf-8"))
    assert frame["type"] == "patch"
    assert frame["schema_version"] == 2
    assert "core" not in frame
    assert {"base_offset", "patches", "coalesced_count", "watermark"} <= set(frame)
    assert frame["watermark"]["event_offset"] > frame["base_offset"]
    ((patch,)) = frame["patches"]
    assert set(patch) == {"seq", "ts", "entity", "id", "changed"}
