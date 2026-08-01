"""Cross-repo stream-frame contract goldens (transport plan W0, 2026-07-16).

The launcher commits byte-identical copies of ``tests/fixtures/stream_frames/``
under ``test/fixtures/harness_stream/`` and parses them through its real
decode + read-model pipeline. These tests hold the hermes side of that
contract: the frames the producer builds TODAY must keep the golden key-set
shape, and the fixture bytes must match the manifest so either repo drifting
alone turns a CI red instead of a silently-null field.

Update rule (both fixture dirs' README): fixtures change only in a
cross-stack change that lands both repos together.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from agent_runtime.events import EventLog
from agent_runtime.models import Event
from agent_runtime.stream import delta_frame, heartbeat_frame, hydrate_frame

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "stream_frames"

# The launcher consumes exactly these; they may never leave a frame.
LAUNCHER_LOAD_BEARING_KEYS = {"type", "watermark"}


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _seed_event(log: EventLog, *, task: str, fingerprint: str) -> Event:
    evt = Event(
        ts=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc),
        type="state.reconciled",
        task_id=task,
        run_id=None,
        persona_id="dev",
        payload={"fingerprint": fingerprint},
    )
    log.append(evt)
    return evt


def test_manifest_pins_fixture_bytes():
    manifest = (FIXTURES / "MANIFEST.sha256").read_text(encoding="utf-8")
    entries = dict(
        reversed(line.split("  ", 1)) for line in manifest.strip().splitlines()
    )
    assert set(entries) == {
        "hydrate.json",
        "delta.json",
        "heartbeat.json",
        "delta_batch.json",
        "patch.json",
        "patch_upsert_profile.json",
        "patch_remove.json",
        "patch_coverage_manifest.json",
    }
    for name, digest in entries.items():
        actual = hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()
        assert actual == digest, (
            f"{name} drifted from MANIFEST.sha256 — stream goldens change only "
            "in a cross-stack change that lands hermes + launcher together"
        )


def test_hydrate_frame_matches_golden_shape(isolate_agent_runtime_root):
    live = hydrate_frame()
    golden = _fixture("hydrate.json")
    assert set(live) == set(golden)
    assert set(live["watermark"]) == set(golden["watermark"])
    assert LAUNCHER_LOAD_BEARING_KEYS <= set(live)
    assert live["type"] == "hydrate"
    assert isinstance(live["core"], dict)


def test_hydrate_core_pins_contract_version(isolate_agent_runtime_root):
    live = hydrate_frame()
    golden = _fixture("hydrate.json")
    for frame, origin in ((live, "live"), (golden, "golden")):
        parity = (frame.get("core") or {}).get("parity") or {}
        assert parity.get("contract_version") == 46, (
            f"{origin} hydrate core carries contract_version="
            f"{parity.get('contract_version')} — bumping it is a cross-stack "
            "change (launcher pins kSupportedMissionContractVersion)"
        )


def test_delta_frame_matches_golden_shape(isolate_agent_runtime_root):
    log = EventLog()
    _seed_event(log, task="task_shape", fingerprint="shape-fp")
    ((offset, event),) = list(log.iter_from_offset(0))
    live = delta_frame(event, offset=offset)
    golden = _fixture("delta.json")
    assert set(live) == set(golden)
    assert set(live["watermark"]) == set(golden["watermark"])
    assert set(live["entity"]) == set(golden["entity"])
    assert live["type"] == "delta"
    assert isinstance(live["core"], dict), (
        "a delta without a core is a launcher drop (delta_without_core) — "
        "removing the core is a contract change, not an optimization"
    )


def test_heartbeat_frame_matches_golden_shape(isolate_agent_runtime_root):
    live = heartbeat_frame(offset=7)
    golden = _fixture("heartbeat.json")
    assert set(live) == set(golden)
    assert set(live["watermark"]) == set(golden["watermark"])
    assert live["type"] == "heartbeat"
    assert "core" not in live


def test_delta_batch_golden_is_additive_over_delta():
    """The W1 coalescing shape, pinned ahead of its implementation: everything
    a single delta carries, plus `events` (the batch's entities) and
    `coalesced_count`, with `entity` remaining the LAST event (back-compat).
    schema_version stays 1 — the launcher reads only type/watermark/
    identity_map/core, so the additions must stay additions."""

    single = _fixture("delta.json")
    batch = _fixture("delta_batch.json")
    assert set(batch) == set(single) | {"events", "coalesced_count"}
    assert batch["type"] == "delta"
    assert batch["schema_version"] == single["schema_version"] == 1
    assert isinstance(batch["events"], list) and len(batch["events"]) == 2
    assert batch["coalesced_count"] == len(batch["events"])
    # entity == the last batched event, so pre-batch consumers keep working.
    assert batch["entity"] == batch["events"][-1]
    # Watermark sits at the FINAL offset — strictly newer than the single
    # delta's, so the launcher's `>`-only sequence gate applies it once.
    assert (
        batch["watermark"]["event_offset"] > single["watermark"]["event_offset"]
    )
