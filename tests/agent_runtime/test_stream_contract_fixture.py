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
    assert set(live["core"]) == set(golden["core"])
    assert set(live["core"]["runtime_config"]) == set(
        golden["core"]["runtime_config"]
    )
    assert LAUNCHER_LOAD_BEARING_KEYS <= set(live)
    assert live["type"] == "hydrate"
    assert isinstance(live["core"], dict)


# S56 (2026-08-01) moved the live frame to contract 47, and the golden moved
# WITH it in the same change, per this fixture set's update rule: stream
# goldens change only in a cross-stack landing that carries hermes + launcher
# together (regenerate/edit, copy the bytes, update BOTH manifests). The golden
# edit followed the S47 precedent — remove the keys the wave removed
# (`enterprise_worker_sessions`, `swarm`, `normal_worker_flow`,
# `repo_bundle_routing`, `simplified_agent_contract`,
# `continuous_role_sessions`, `production_envelope`; `supervision` pruned to its
# one live field) and bump `parity.contract_version` — rather than regenerating
# from a fresh seeded root, which would also churn unrelated pre-existing
# staleness in the same bytes.
#
# ONE constant, not two: the live frame and the golden must agree. A split pin
# would let the launcher's `kSupportedMissionContractVersion` sit against a
# golden nobody bumped, which is the drift this file exists to catch.
#
# S57 (2026-08-01) moved it again, 47 -> 48, and edited the goldens by the same
# rule: drop the 29 reader-less `runtime_config` scalars and
# `migration.counts.repo_bundles`, bump `parity.contract_version`, copy the bytes
# to the Launcher, update BOTH manifests.
#
# S59 repaired the delta-batch fixture that the 48 -> 49 move missed. Pin every
# frame-bearing golden here, not only hydrate: a partial fixture bump must fail
# even when the live hydrate frame and its own golden agree.
# Round 4 moved the contract through 50 to 51 as the event registry and its
# writerless task/proof/run projection fields retired. These goldens are now
# generated from the current production builders rather than hand-edited.
# WP-H1 (2026-08-03) moved it 51 -> 52 and REGENERATED the four frames: the
# `running_work` section joins the core, so this is a key-set change and not a
# value edit — the shape assertions below would catch a hand-bumped version that
# left the goldens without the section.
CONTRACT_VERSION = 52


def test_every_frame_bearing_golden_pins_contract_version(isolate_agent_runtime_root):
    live = hydrate_frame()
    frames = [(live, "live hydrate")]
    frames.extend(
        (_fixture(name), f"golden {name}")
        for name in ("hydrate.json", "delta.json", "delta_batch.json")
    )
    for frame, origin in frames:
        parity = (frame.get("core") or {}).get("parity") or {}
        assert parity.get("contract_version") == CONTRACT_VERSION, (
            f"{origin} core carries contract_version="
            f"{parity.get('contract_version')} — bumping it is a cross-stack "
            "change (launcher pins kSupportedMissionContractVersion)"
        )


def test_every_frame_bearing_golden_matches_live_core_shape(
    isolate_agent_runtime_root,
):
    """A contract-version match is insufficient when nested keys drift.

    Hydrate is the live full-core producer authority. Delta and delta_batch
    carry that same full core, so all three committed frame goldens must agree
    with its core and runtime_config key sets.
    """

    live_core = hydrate_frame()["core"]
    hydrate_golden_core = _fixture("hydrate.json")["core"]
    assert "default_flow" not in live_core["prompt_observability"]
    for name in ("hydrate.json", "delta.json", "delta_batch.json"):
        golden_core = _fixture(name)["core"]
        assert golden_core == hydrate_golden_core, (
            f"{name} does not carry the exact generated hydrate core"
        )
        assert set(golden_core) == set(live_core), f"{name} core keys drifted"
        assert set(golden_core["runtime_config"]) == set(
            live_core["runtime_config"]
        ), f"{name} core.runtime_config keys drifted"
        assert golden_core["parity"]["capabilities"] == live_core["parity"][
            "capabilities"
        ], f"{name} core.parity.capabilities drifted"
        assert set(golden_core["parity"]["completeness"]) == set(
            live_core["parity"]["completeness"]
        ), f"{name} core.parity.completeness keys drifted"
        for section, live_completeness in live_core["parity"][
            "completeness"
        ].items():
            assert set(golden_core["parity"]["completeness"][section]) == set(
                live_completeness
            ), f"{name} completeness.{section} keys drifted"
        assert "default_flow" not in golden_core["prompt_observability"]


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
