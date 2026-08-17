"""Cross-repo stream-frame contract goldens (transport plan W0, 2026-07-16).

The launcher commits byte-identical copies of ``tests/fixtures/stream_frames/``
under ``test/fixtures/harness_stream/`` and parses them through its real
decode + read-model pipeline. These tests hold the hermes side of that
contract: the frames the producer builds TODAY must have the golden's shape all
the way down, and the fixture bytes must match the manifest so either repo
drifting alone turns a CI red instead of a silently-null field.

The shape comparison is by nested KEY PATH (``_key_paths``), not by top-level
key set. The top-level version was green while
``core.runtime_config.mission_chat.compaction_threshold_tokens`` sat missing
from every golden, and the launcher mirrored those stale bytes cross-repo: a
key added one level below a key set that already matched changed nothing the
old assertions looked at.

Update rule (both fixture dirs' README): fixtures change only in a
cross-stack change that lands both repos together.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.events import EventLog
from agent_runtime.models import Event
from agent_runtime.serde import to_jsonable
from agent_runtime.snapshot import SNAPSHOT_CONTRACT_VERSION
from agent_runtime.stream import (
    delta_batch_frame,
    delta_frame,
    heartbeat_frame,
    hydrate_frame,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "stream_frames"
GENERATOR = REPO_ROOT / "scripts" / "generate_agent_runtime_stream_fixtures.py"
REGENERATE = f"python {GENERATOR.relative_to(REPO_ROOT).as_posix()}"

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


# --------------------------------------------------------------------------- #
# Shape: every nested key path, not just the top-level neighbourhood
# --------------------------------------------------------------------------- #
def _key_paths(value: Any, prefix: str = "") -> set[str]:
    """Every nested key path in a JSON-shaped value, dotted.

    ``{"core": {"runtime_config": {"mission_chat": {"x": 1}}}}`` yields
    ``core``, ``core.runtime_config``, ``core.runtime_config.mission_chat`` and
    ``core.runtime_config.mission_chat.x`` — so an addition BELOW a key set that
    already matched is visible, which a top-level ``set(...) == set(...)`` is
    blind to by construction.

    List elements collapse onto a single ``[]`` segment. That is the deliberate
    line between contract and data: a golden holding two entries where the
    producer holds three is not drift and must not churn, but an entry gaining
    or losing a KEY is drift and must fail.
    """

    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            found.add(path)
            found |= _key_paths(item, path)
    elif isinstance(value, list):
        for item in value:
            found |= _key_paths(item, f"{prefix}[]")
    return found


def _shape_drift(live: Any, golden: Any) -> tuple[list[str], list[str]]:
    """``(producer_only, golden_only)`` key paths. Empty pair == agreement.

    Extracted so the anti-vacuity tests below can drive the comparator on
    planted data instead of trusting that it still discriminates.
    """

    live_paths, golden_paths = _key_paths(live), _key_paths(golden)
    return sorted(live_paths - golden_paths), sorted(golden_paths - live_paths)


def _live_generated_frames() -> dict[str, dict]:
    """The goldens ``scripts/generate_agent_runtime_stream_fixtures.py`` writes,
    rebuilt HERE by the same production builders it calls, with the same seeded
    two-event log.

    Values still differ from the committed bytes (the generator normalizes
    timestamps, timings, the root spelling and ``repo_scopes[*].resolved``);
    only the SHAPE is compared, which is why this needs no normalization of its
    own and cannot rot into a second normalizer disagreeing with the first.
    """

    hydrate = hydrate_frame()
    core = hydrate["core"]
    log = EventLog()
    first = Event(
        ts=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc),
        type="state.reconciled",
        task_id="task_shape",
        run_id=None,
        persona_id="custom_agent",
        payload={"fingerprint": "shape-fp"},
    )
    second = Event(
        ts=first.ts + timedelta(seconds=1),
        type="state.reconciled",
        task_id="task_shape",
        run_id=None,
        persona_id="custom_agent",
        payload={"fingerprint": "shape-fp-2"},
    )
    log.append(first)
    log.append(second)
    batch = list(log.iter_from_offset(0))
    # Seeded LAST, through the generator's own function rather than a copy of it:
    # a second seeder here could drift from the one that wrote the bytes, and the
    # gate would then compare a frame nobody ships against a golden nobody
    # rebuilds. Order matches the generator — everything above is built before
    # the seed lands, so only the last frame carries running work.
    _generator_module()._seed_running_work_owner()
    return {
        "hydrate.json": hydrate,
        "delta.json": delta_frame(first, offset=batch[0][0], snapshot=core),
        "heartbeat.json": heartbeat_frame(offset=7),
        "delta_batch.json": delta_batch_frame(batch, snapshot=core),
        "hydrate_running_work_owner.json": hydrate_frame(),
    }


def _generator_module():
    spec = importlib.util.spec_from_file_location(
        "_stream_fixture_generator_for_tests", GENERATOR
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_goldens_are_the_generators_bytes(tmp_path, monkeypatch):
    """Run the real generator into a temp root; the committed goldens must be
    the bytes it writes.

    The shape gate below is value-blind ON PURPOSE (values churn), and that
    blindness has a measured cost: 3ad5d6cbf2 flipped the shipped
    ``read_model.delta_patches`` default and every golden went on carrying
    ``false`` — hermes CI green throughout, because a value flip changes no key
    path — while the Launcher's ``check_producer_contracts.py`` byte-compare
    went red on every launcher push. The value-blindness is only safe when a
    VALUE-level gate exists on the producer side, which is exactly the split
    the response-envelope file already has
    (``test_every_fixture_is_re_derivable_from_the_producer``).

    Byte equality does not resurrect the churn problem the shape gate was
    guarding against: the generator normalizes every volatile value before
    writing (timestamps, pids, roots, machine-probed flags — see its module
    docstring's reproducibility claim), so regeneration is deterministic and
    this only reddens when the PRODUCER changes. Verified before landing:
    three independent regenerations, identical bytes.
    """

    generator = _generator_module()
    staged = tmp_path / "stream_frames"
    staged.mkdir()
    # The manifest also hashes the hand-authored pinned goldens, so stage the
    # committed bytes for those — this gate owns the GENERATED files only.
    for name in generator.PINNED_ONLY_FILES:
        (staged / name).write_bytes((FIXTURES / name).read_bytes())
    # main() rewires these itself; setting them through monkeypatch first is
    # what guarantees they are restored for the rest of the file.
    for key in (
        "HERMES_HOME",
        "HERMES_HEAD_HOME",
        "HERMES_AGENT_RUNTIME_ROOT",
        "LOCALAPPDATA",
    ):
        monkeypatch.setenv(key, str(tmp_path / "pre"))
    monkeypatch.setattr(generator, "FIXTURE_ROOT", staged)

    assert generator.main() == 0

    for name in (*generator.GENERATED_FRAME_FILES, "MANIFEST.sha256"):
        assert (staged / name).read_bytes() == (FIXTURES / name).read_bytes(), (
            f"{name}: the committed golden is not what the generator writes "
            f"today. Regenerate with `{REGENERATE}`, mirror the bytes into the "
            "Launcher's test/fixtures/harness_stream/, and update BOTH "
            "manifests — stream goldens change only in a cross-stack landing."
        )


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
        "hydrate_running_work_owner.json",
        "patch.json",
        "patch_upsert_profile.json",
        "patch_remove.json",
        "patch_office_actor.json",
        # The office fold-promotion milestone's cross-stack pin (O-H3,
        # 2026-08-16): the DELETE gesture as one patch frame carrying two
        # removes at one watermark.
        "patch_delete_gesture.json",
        # The office write-verbs milestone's cross-stack pin (WV-H3,
        # 2026-08-16): a FOLDER change as one ``office_surface`` SUBSET upsert.
        "patch_office_surface.json",
        "patch_coverage_manifest.json",
    }
    for name, digest in entries.items():
        actual = hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()
        assert actual == digest, (
            f"{name} drifted from MANIFEST.sha256 — stream goldens change only "
            "in a cross-stack change that lands hermes + launcher together"
        )


def test_hydrate_frame_is_the_frame_the_launcher_decodes(isolate_agent_runtime_root):
    """The hydrate frame's own contract. Its SHAPE agreement with the golden is
    owned once, by ``test_every_generated_golden_has_the_producer_shape`` — a
    second key-set assertion here would be a second mechanism satisfying the
    same pin, which is how a sabotaged gate stays green."""

    live = hydrate_frame()
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
# The Activity ownership correction (2026-08-04) moved it 52 -> 53 and
# regenerated every frame after retiring connected MCP transports as work.
#
# 2026-08-09: DERIVED from the producer instead of restated. This is not a
# weakening. The assertion below compares the GOLDEN BYTES against it, so
# deriving means the moment the producer moves, every stale golden goes red —
# exactly the drift this gate exists to catch. Restating it had the opposite
# effect: a bump that forgot this file left the goldens stale AND the gate
# green. The one deliberate literal now lives in
# `test_snapshot_contract_version_authority.py`.
CONTRACT_VERSION = SNAPSHOT_CONTRACT_VERSION


def test_every_frame_bearing_golden_pins_contract_version(isolate_agent_runtime_root):
    live = hydrate_frame()
    frames = [(live, "live hydrate")]
    frames.extend(
        (_fixture(name), f"golden {name}")
        for name in (
            "hydrate.json",
            "delta.json",
            "delta_batch.json",
            "hydrate_running_work_owner.json",
        )
    )
    for frame, origin in frames:
        parity = (frame.get("core") or {}).get("parity") or {}
        assert parity.get("contract_version") == CONTRACT_VERSION, (
            f"{origin} core carries contract_version="
            f"{parity.get('contract_version')} — bumping it is a cross-stack "
            "change (launcher pins kSupportedMissionContractVersion)"
        )


# --------------------------------------------------------------------------- #
# THE GATE
# --------------------------------------------------------------------------- #
def test_every_generated_golden_has_the_producer_shape(isolate_agent_runtime_root):
    """Rebuild each generated frame from the production builders; the committed
    golden must have the SAME nested key paths.

    This is the check whose absence let
    ``core.runtime_config.mission_chat.compaction_threshold_tokens`` (added by
    `3d6f9ae81`, which never regenerated) sit in a stale golden with the gate
    green — and then be mirrored, stale, into the Launcher. The old assertions
    compared the TOP-LEVEL key sets of ``core`` and ``runtime_config``, so a key
    appearing one level down changed nothing they looked at. They pinned the
    neighbourhood; this pins the guarantee.

    Paths, not values, on purpose. Values are what churns — timings, counts, the
    generator's normalized stamps — and a gate that reddens on churn is a gate
    people regenerate without reading, which is how goldens rot in the first
    place. A nested addition or removal is never churn: it is a cross-repo
    contract change, and it fails here naming the exact path.
    """

    for name, live in _live_generated_frames().items():
        producer_only, golden_only = _shape_drift(to_jsonable(live), _fixture(name))
        assert not producer_only and not golden_only, (
            f"{name} no longer has the shape the producer builds.\n"
            f"  producer only (the golden is stale): {producer_only}\n"
            f"  golden only (the producer dropped these): {golden_only}\n"
            f"Regenerate with `{REGENERATE}`, mirror the bytes into the "
            "Launcher's test/fixtures/harness_stream/, and update BOTH "
            "manifests — stream goldens change only in a cross-stack landing."
        )


def test_the_gate_covers_every_frame_the_generator_writes():
    """A frame the generator writes but this gate never rebuilds would be
    unpinned by construction — the S59 failure (a partial fixture bump the gate
    could not see) one level up."""

    generated = _generator_module().GENERATED_FRAME_FILES
    assert set(_live_generated_frames()) == set(generated), (
        f"gate rebuilds {sorted(_live_generated_frames())} but the generator "
        f"writes {sorted(generated)}"
    )


def test_every_frame_bearing_golden_carries_the_generated_core(
    isolate_agent_runtime_root,
):
    """The value-level agreements shape cannot express.

    Delta and delta_batch ship the SAME core object hydrate does, so their
    goldens must be byte-equal to hydrate's core — and the parity capabilities
    the launcher branches on must equal the live ones, not merely have the same
    keys.
    """

    live_core = hydrate_frame()["core"]
    hydrate_golden_core = _fixture("hydrate.json")["core"]
    assert "default_flow" not in live_core["prompt_observability"]
    for name in ("hydrate.json", "delta.json", "delta_batch.json"):
        golden_core = _fixture(name)["core"]
        assert golden_core == hydrate_golden_core, (
            f"{name} does not carry the exact generated hydrate core"
        )
        assert golden_core["parity"]["capabilities"] == live_core["parity"][
            "capabilities"
        ], f"{name} core.parity.capabilities drifted"


# --------------------------------------------------------------------------- #
# Anti-vacuity: drive the comparator on planted drift
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "plant, expected_path",
    [
        pytest.param(
            lambda core: core["runtime_config"]["mission_chat"].pop(
                "compaction_threshold_tokens"
            ),
            "core.runtime_config.mission_chat.compaction_threshold_tokens",
            id="the-real-2026-08-nested-addition-the-old-gate-missed",
        ),
        pytest.param(
            lambda core: core["runtime_config"]["read_model"].__setitem__(
                "invented_switch", True
            ),
            "core.runtime_config.read_model.invented_switch",
            id="a-nested-key-invented-in-the-golden",
        ),
        pytest.param(
            lambda core: core["repo_scopes"]["frontend"].pop("label"),
            "core.repo_scopes.frontend.label",
            id="a-contractual-leaf-dropped-two-levels-down",
        ),
        pytest.param(
            lambda core: core["parity"]["completeness"].pop("running_work"),
            "core.parity.completeness.running_work",
            id="a-whole-completeness-section-dropped",
        ),
    ],
)
def test_the_shape_comparison_notices_drift_below_the_top_level(plant, expected_path):
    """Each drift class planted into a copy of the real golden.

    The old gate is the cautionary tale: none of these change
    ``set(core)`` or ``set(core["runtime_config"])``, so none of them could have
    failed it. Driving the comparator on planted data is what stops this rewrite
    from decaying the same way — if it ever stops discriminating, this fails
    while the tree is clean.
    """

    golden = _fixture("hydrate.json")
    drifted = json.loads(json.dumps(golden))
    plant(drifted["core"])

    # Precondition: the RETIRED comparison is blind to every one of these.
    assert set(drifted["core"]) == set(golden["core"])
    assert set(drifted["core"]["runtime_config"]) == set(
        golden["core"]["runtime_config"]
    )

    producer_only, golden_only = _shape_drift(golden, drifted)
    assert expected_path in producer_only or expected_path in golden_only, (
        f"the comparator accepted drift at {expected_path}: "
        f"producer_only={producer_only} golden_only={golden_only}"
    )


def test_the_shape_comparison_does_not_redden_on_values_or_list_length():
    """The other half of the balance: churn must NOT fail.

    A gate that reddens on a changed timestamp or a differently-sized list gets
    regenerated reflexively, unread — the habit that lets a real nested change
    ride along unnoticed.
    """

    golden = _fixture("hydrate.json")
    churned = json.loads(json.dumps(golden))
    churned["core"]["generated_at"] = "2099-01-01T00:00:00.000000Z"
    churned["core"]["runtime_config"]["store_root"] = "D:/somewhere/else"
    churned["core"]["runtime_config"]["lock_acquire_timeout_seconds"] = 999
    churned["core"]["runtime_config"]["mission_chat"][
        "compaction_threshold_tokens"
    ] = 1
    # Same element shape, different length — data, not contract. (The parity
    # capabilities' VALUES are pinned separately against the live producer, so
    # tolerating length here opens no hole.)
    churned["core"]["parity"]["capabilities"] = [
        *golden["core"]["parity"]["capabilities"],
        "another_capability",
    ]

    assert _shape_drift(golden, churned) == ([], []), (
        "value churn registered as shape drift — this gate would teach people "
        "to regenerate without reading"
    )


def test_delta_frame_is_never_shipped_without_a_core(isolate_agent_runtime_root):
    log = EventLog()
    _seed_event(log, task="task_shape", fingerprint="shape-fp")
    ((offset, event),) = list(log.iter_from_offset(0))
    live = delta_frame(event, offset=offset)
    assert live["type"] == "delta"
    assert isinstance(live["core"], dict), (
        "a delta without a core is a launcher drop (delta_without_core) — "
        "removing the core is a contract change, not an optimization"
    )


def test_heartbeat_frame_stays_core_free(isolate_agent_runtime_root):
    live = heartbeat_frame(offset=7)
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
