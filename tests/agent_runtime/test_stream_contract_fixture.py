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


@pytest.fixture(autouse=True)
def _reset_core_cache_lane():
    """This file now BUILDS cores, so it owns the lane it leaves behind.

    ``_build_stale_first_convergence_pair`` converges a persisted core and then
    pays for a gated rebuild, and a completed build closes the process's cache
    lane. Before BO-1 nothing in this file touched that state; now it ends every
    generating case with the lane DISARMED, which would hand the next file a
    process that can never be served a cache — a case elsewhere asserting a
    cache-hit boot would fail for a reason with no connection to itself.

    The conftest already resets the captured fingerprint home for exactly this
    class of leak; the lane and its memo are the same kind of process-global
    state and get the same treatment, here rather than globally because this is
    the file that started building.
    """

    from agent_runtime import core_cache

    core_cache.reset_process_state()
    yield
    core_cache.reset_process_state()


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
    owner_hydrate = hydrate_frame()
    # LAST, and again through the generator's own function: this one converges a
    # persisted core and pays for a gated rebuild, so running it earlier would
    # rebuild every frame above against a store it had moved.
    stale_first, authoritative = _generator_module()._build_stale_first_convergence_pair()
    # LAST of all, and again through the generator's own function: this one
    # performs a REAL agent create, which seeds a persona, a workspace and an
    # office surface and puts four events on the log. Running it earlier would
    # rebuild every frame above against a store it had moved.
    create_patch, create_demoted = _generator_module()._build_agent_create_frames()
    return {
        "hydrate.json": hydrate,
        "delta.json": delta_frame(first, offset=batch[0][0], snapshot=core),
        "heartbeat.json": heartbeat_frame(offset=7),
        "delta_batch.json": delta_batch_frame(batch, snapshot=core),
        "hydrate_running_work_owner.json": owner_hydrate,
        "hydrate_stale_first.json": stale_first,
        "hydrate_authoritative_same_offset.json": authoritative,
        "patch_agent_create.json": create_patch,
        "delta_agent_create_narrow_profile.json": create_demoted,
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
        # BO-1's same-offset convergence pair (2026-08-21): the boot's stale
        # paint and its authoritative replacement, both at the idle store's one
        # offset. Read as a PAIR — the relation between them is the contract.
        "hydrate_stale_first.json",
        "hydrate_authoritative_same_offset.json",
        # The placement verb's S0 pair (2026-08-26): one out-of-process
        # ``agent create`` seen by a wide-profile subscriber and by a
        # narrow-profile one. Read as a PAIR — see
        # ``test_the_agent_create_pair_is_one_batch_seen_two_ways``.
        "patch_agent_create.json",
        "delta_agent_create_narrow_profile.json",
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
            "hydrate_stale_first.json",
            "hydrate_authoritative_same_offset.json",
            "delta_agent_create_narrow_profile.json",
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


def test_the_stale_first_pair_is_same_offset_and_carries_both_freshness_tokens():
    """BO-1's cross-repo pin, asserted on the committed BYTES.

    The producer-side behaviour is pinned separately against the live builders
    (``test_core_fingerprint_cache``'s stale-then-authoritative case). This one
    is about the goldens the launcher mirrors: a manifest hash proves the bytes
    did not move, and proves nothing about what they SAY. If the pair ever
    regenerates to two different offsets — or to two frames wearing the same
    freshness token — the launcher's convergence case would be driving a shape
    that no longer exists on the wire, green in both repos.

    The equal offset is the exemption's whole precondition: the launcher's
    ordinary sequence gate is strict ``>``, and only
    ``staleHeldAwaitsAuthoritative`` lets frame 2 land at frame 1's offset. The
    token pair is BO-6's fixture-drift finding closed on real bytes — the
    non-stale spelling is ``fresh`` here, while the launcher's wiring-test
    fixtures had drifted to ``live`` with nothing comparing them.
    """

    stale = _fixture("hydrate_stale_first.json")
    authoritative = _fixture("hydrate_authoritative_same_offset.json")

    assert stale["type"] == authoritative["type"] == "hydrate"
    assert (
        stale["watermark"]["event_offset"]
        == authoritative["watermark"]["event_offset"]
    ), (
        "the convergence pair is no longer same-offset, so the launcher's "
        "stale-held exemption is not what these goldens exercise"
    )

    stale_parity = stale["core"]["parity"]
    authoritative_parity = authoritative["core"]["parity"]
    assert stale_parity["freshness"]["state"] == "stale"
    assert stale_parity["core_stale"] is True
    assert stale_parity["core_source"] == "cache"
    assert authoritative_parity["freshness"]["state"] == "fresh"
    assert "core_stale" not in authoritative_parity
    assert authoritative_parity["core_source"] == "rebuilt"

    assert stale["core"] != authoritative["core"], (
        "both frames carry the same core, so a launcher that dropped frame 2 "
        "entirely would still satisfy every content assertion downstream"
    )


# --------------------------------------------------------------------------- #
# S0 of the placement verb: one create, two subscribers
# --------------------------------------------------------------------------- #
def _events_from_demoted_golden() -> list:
    """The batch's events, reconstructed from the demoted golden's own bytes.

    ``delta_batch_frame`` carries every batched event under ``events[].event``,
    so the demoted golden is a complete record of what the create appended. The
    tests below feed those bytes back through the LIVE classifier rather than
    re-deriving them from a store: a coverage rule that changed under the
    goldens would then fail here, on the exact frames both repos ship, instead
    of quietly agreeing with itself.
    """

    from agent_runtime.models import Event

    rows = _fixture("delta_agent_create_narrow_profile.json")["events"]
    return [
        Event(
            ts=datetime.fromisoformat(
                str(row["event"]["ts"]).replace("Z", "+00:00")
            ),
            type=row["event"]["type"],
            task_id=row["event"].get("task_id"),
            run_id=row["event"].get("run_id"),
            persona_id=row["event"].get("persona_id"),
            payload=row["event"].get("payload") or {},
        )
        for row in rows
    ]


def test_the_agent_create_patch_golden_carries_exactly_the_two_creates():
    """S0's answer, on the committed bytes.

    F6 left open whether one out-of-process ``agent create`` reaches a
    connected client as ONE ``patch`` frame carrying both halves — the roster
    row and the placement — or demotes, or is dropped at the fold. It does not
    demote: this golden is what the promotion decision produced for a
    subscriber declaring the launcher's own fold set, and it carries exactly
    two rows, both stamped ``created``.

    ``created`` is asserted rather than assumed because it is part of the FOLD
    contract, not merely of the negotiation: the launcher's generic
    persona-instance fold inserts-on-absent only when the stamp is present, and
    answers ``patch_without_target`` — a full re-hydrate — without it.
    """

    frame = _fixture("patch_agent_create.json")

    assert frame["type"] == "patch", (
        "the create's batch is no longer promoted — S0's answer would be no, "
        "and every slice built on D3 is built on a lane that was assumed"
    )
    assert frame["schema_version"] == 2
    assert "core" not in frame

    rows = frame["patches"]
    assert [(row["entity"], row["op"]) for row in rows] == [
        ("persona_instance", "upsert"),
        ("office_actor", "upsert"),
    ], rows
    assert all(row["created"] is True for row in rows), rows
    assert rows[0]["id"] == "personainst_qa_fixture_agent_2"
    assert rows[1]["id"] == "ws_office_pilot/personainst_qa_fixture_agent_2"
    # The placement is instance-keyed by construction
    # (``agent_create.placement_actor_payload``), which is what keeps the office
    # store's class-key fence unreachable from this method.
    assert rows[1]["changed"]["persona_instance_id"] == rows[0]["id"]
    # ONE agent item and no desk: the verb authors no desk (plan D6), and a
    # golden that grew one would be pinning a behaviour the launcher's own drop
    # does not have.
    items = rows[1]["changed"]["items"]
    assert [item["kind"] for item in items] == ["agent"], items

    # The fold's own precondition: the client folds only when its held
    # watermark equals ``base_offset``, and its sequence gate is strict ``>``.
    base = frame["base_offset"]
    watermark = frame["watermark"]["event_offset"]
    assert base < rows[0]["seq"] < rows[1]["seq"] <= watermark, frame


def test_the_narrow_profile_golden_is_the_honest_full_core():
    """The other arm of the plan's A.11 hazard, pinned on bytes.

    ``accepted_fold_entities`` intersects across every subscriber in the room,
    so ONE narrow-profile client demotes every placement to a full core for
    everyone. Correct — never a wrong render — and expensive, which is the
    whole point of pinning it: the demote is now observed rather than reasoned
    about, and a producer that started shipping the patch to a client that
    never declared ``office_actor`` would redden here on ``type``.
    """

    frame = _fixture("delta_agent_create_narrow_profile.json")

    assert frame["type"] == "delta", (
        "the narrow-profile subscriber was promoted to a patch it cannot fold "
        "— that client pays the patch AND the re-hydrate, which is strictly "
        "worse than the full core this frame is"
    )
    core = frame["core"]
    assert isinstance(core, dict), "a demote that carries no core carries nothing"
    # The demote is only correct because the core re-baselines what the patch
    # would have folded: BOTH halves of the create are in it.
    actors = core["offices"]["ws_office_pilot"]["actors"]
    assert any(
        actor["actor_key"] == "personainst_qa_fixture_agent_2" for actor in actors
    ), actors
    assert "personainst_qa_fixture_agent_2" in core["persona_instances"]


def test_the_agent_create_pair_is_one_batch_seen_two_ways():
    """Neither golden says anything alone; the relation between them is the
    fixture.

    They must describe the SAME create — same batch length, same final
    watermark — because the claim is about the SUBSCRIBER's declaration and
    nothing else. A pair regenerated from two different creates would pin two
    unrelated frames and prove nothing about the negotiation.
    """

    patch = _fixture("patch_agent_create.json")
    demoted = _fixture("delta_agent_create_narrow_profile.json")

    assert patch["coalesced_count"] == demoted["coalesced_count"] == 4
    assert (
        patch["watermark"]["event_offset"]
        == demoted["watermark"]["event_offset"]
        == demoted["seq"]
    ), (patch["watermark"], demoted["watermark"], demoted["seq"])
    assert [row["event"]["type"] for row in demoted["events"]] == [
        "state.patched",
        "persona_instance.chat_opened",
        "state.patched",
        "office.actor.upserted",
    ], demoted["events"]


def test_the_live_classifier_still_splits_the_two_profiles(
    isolate_agent_runtime_root,
):
    """The goldens' own events, back through the LIVE coverage rule.

    A manifest hash proves the bytes did not move and proves nothing about what
    they MEAN. This drives ``batch_is_patch_coverable`` — the predicate
    ``_batch_frames_with_liveness`` consults — over the events the demoted
    golden records, and asserts the split the pair claims: promotable for the
    launcher's declared set, demoted for the historical two.

    It is the anti-vacuity half of the pair. Un-gate the office lifecycle
    token, or widen ``HISTORICAL_FOLD_ENTITIES``, and the narrow arm goes
    coverable here while both goldens sit unchanged and green.
    """

    from agent_runtime.patch_coverage import batch_is_patch_coverable

    generator = _generator_module()
    events = _events_from_demoted_golden()

    assert batch_is_patch_coverable(
        iter(events), fold_entities=generator.FIXTURE_WIDE_FOLD_ENTITIES
    ), "the create's batch stopped being coverable for the launcher's own set"
    assert not batch_is_patch_coverable(
        iter(events), fold_entities=generator.FIXTURE_NARROW_FOLD_ENTITIES
    ), (
        "the create's batch became coverable for a subscriber that declares "
        "neither office_actor nor the lifecycle token — that client would be "
        "sent a frame it answers with a full re-hydrate"
    )


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
