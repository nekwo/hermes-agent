"""Regenerate deterministic Agent Runtime stream contract fixtures.

The builder runs against a fresh isolated Hermes/runtime root and calls the
current production frame constructors. Volatile values are normalized only
after construction, so the fixture bytes stay reviewable.

Reproducibility, stated precisely
---------------------------------
Rerunning this script on ANY machine reproduces the committed bytes of the
frames :func:`main` writes. That claim did **not** hold before the
``_MACHINE_PROBED_FLAGS`` normalization below, and the correction is recorded
here so a future reader does not have to re-derive it.

``core.repo_scopes`` is built by ``snapshot._repo_scope_entry``, whose
``resolved`` flag is ``resolve_affected_repo_workdir(alias) is not None``. That
resolver reads the logical ``eternia_launcher`` / ``eternia_backend`` bindings
from the machine-local ``machine_roots.json`` authority. On a configured box
``frontend`` and ``backend`` resolve true; in this deliberately isolated
generator, CI, or a fresh clone they resolve false. The emitted bytes would
therefore still depend on WHO RAN THE SCRIPT without normalization. (``harness``
was never machine-dependent: it resolves through ``Path(__file__).parents[1]``,
i.e. this repo, which always exists.)

``resolved`` is now pinned to a fixture constant. That constant asserts NOTHING
about any machine's checkout layout — it is a sentinel of the same kind as
``FIXED_TIME`` and ``<isolated-root>``, and no code in either repo reads the
field (hermes pins only the three ``label`` values, in
``tests/agent_runtime/test_specialist_agents_red.py``; the launcher has no Dart
reader at all). It is pinned to the value the committed goldens already carry,
so retiring the machine-dependence cost no cross-repo byte churn; changing it
later is a cross-stack change under the fixture README's update rule. The
``label`` values are contractual and are deliberately NOT normalized.

What this script writes, and what it only pins
----------------------------------------------
:func:`main` regenerates the frames in :data:`GENERATED_FRAME_FILES`. The four
files in :data:`PINNED_ONLY_FILES` are hand-authored and are only HASHED into
``MANIFEST.sha256``; see that tuple's comment for why they cannot be generated
from the current production builders.

``hydrate_running_work_owner.json`` is the odd one out among the generated
frames: it is a SECOND hydrate taken after the isolated root is seeded with a
persona instance and two background delegations, and it is the only golden that
carries ``running_work`` rows. It exists to pin the producer/consumer JOIN on
``running_work.rows[].owner`` — see :func:`_seed_running_work_owner`.

``hydrate_stale_first.json`` and ``hydrate_authoritative_same_offset.json`` are
the other odd ones, and they are odd TOGETHER: neither says anything alone. They
are EG-3.1's mismatch half off the real producer — the boot's stale paint and the
authoritative hydrate that replaces it — and their contract is that both carry
the SAME ``watermark.event_offset``, because the store's log is idle between
them. See :func:`_build_stale_first_convergence_pair`.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "stream_frames"
FIXED_TIME = "2026-07-16T12:00:00.000000Z"
#: The frames :func:`main` builds and writes. Regenerating these reproduces the
#: committed bytes on any machine (see the module docstring).
GENERATED_FRAME_FILES = (
    "hydrate.json",
    "delta.json",
    "heartbeat.json",
    "delta_batch.json",
    "hydrate_running_work_owner.json",
    # BO-1's convergence pair. See :func:`_build_stale_first_convergence_pair`
    # for what produces them and why they must be read as a PAIR: their whole
    # contract is a relation between the two frames, not a property of either.
    "hydrate_stale_first.json",
    "hydrate_authoritative_same_offset.json",
)

#: Identities the running-work owner fixture seeds. They are FIXTURE constants,
#: not runtime constants — nothing in either repo resolves them at run time — but
#: their SHAPE is contractual: ``OWNED_CHAT_SESSION`` must satisfy
#: ``persona_assignments.chat_session_owner_instance_id`` (``persona_chat_`` +
#: instance id + ``_`` + 12 hex), because the whole point of the fixture is that
#: the producer resolves it to :data:`FIXTURE_INSTANCE_ID` and stamps the row's
#: ``owner`` block with it.
FIXTURE_PERSONA_ID = "fixture_agent"
FIXTURE_INSTANCE_ID = "personainst_fixture_agent_0f0f0f0f"
OWNED_CHAT_SESSION = f"persona_chat_{FIXTURE_INSTANCE_ID}_0123456789ab"
#: A session no chat root owns — a CLI/gateway key, the ordinary case for work
#: spawned outside a persona turn. Its row must ship an EMPTY owner rather than a
#: guessed one, and must still ship.
UNOWNED_SESSION = "cli_fixture_session"
#: Fixed spawn stamp so ``started_at`` is stable before normalization even reads
#: it (``elapsed_seconds`` is derived from wall time and is normalized instead).
FIXTURE_DISPATCHED_AT = 1_760_000_000.0

#: Hand-authored goldens this script only PINS: it hashes them into the manifest
#: and never rewrites them. They are not regenerable from the current production
#: builders, and the reason is structural rather than effort:
#:
#: * ``patch.json`` / ``patch_upsert_profile.json`` / ``patch_remove.json`` are
#:   S6 v2 field-patch frames carrying REAL wall-clock stamps
#:   (``2026-07-17T04:22:55.149761Z``, not :data:`FIXED_TIME`) and hand-chosen
#:   ``base_offset`` / ``seq`` pairs that demonstrate specific fold semantics
#:   over entities this script's seeded root does not contain.
#:   ``patch_remove.json`` is moreover UN-EMITTABLE today: it is the
#:   ``incident.closed`` remove fold, and S65 de-registered that event with its
#:   last writer. ``agent_runtime.patch_coverage`` keeps it in
#:   ``HISTORICAL_COVERED_DOMAIN_EVENT_TYPES`` precisely so a cross-stack fixture
#:   replaying an old batch still classifies the way the launcher folded it when
#:   the event was live — regenerating that frame would mean resurrecting a
#:   retired lane.
#: * ``patch_coverage_manifest.json`` is not a frame at all: it is the S7-A
#:   coverage TABLE.
#:
#: They are maintained by hand and validated by SHAPE plus live-classifier
#: agreement in ``tests/agent_runtime/test_stream_patch.py``
#: (``test_patch_fixtures_manifest_and_shape``,
#: ``test_coverage_manifest_agrees_with_classifier``), not by byte-regeneration.
PINNED_ONLY_FILES = (
    "patch.json",
    "patch_upsert_profile.json",
    "patch_remove.json",
    # The S7-A OFFICE leg: an ``office_actor`` upsert for one dragged desk. Its
    # ``changed`` is a real capture off the production generator (the actor row
    # ``snapshot._office_actor_summary_row`` builds, verbatim); only the
    # timestamps are normalized to the pinned stamp its siblings carry. Pinned
    # rather than generated for the same reason they are: the seeded isolated
    # root the generator builds holds no office surface.
    "patch_office_actor.json",
    # The office fold-promotion milestone (O-H3, 2026-08-16): the DELETE
    # gesture's coalesced batch as one patch frame — a ``persona_instance``
    # remove beside an ``office_actor`` remove, ``coalesced_count`` 4 because
    # the two paired domain events ride the batch and fold to nothing.
    #
    # It is the cross-stack pin for the shape that used to be impossible: this
    # batch demoted to two 822 KB full cores per gesture, and the office lane
    # answered with a resync it could not express any other way. It is also the
    # fixture that makes the V6 race concrete — the frame carries BOTH rows at
    # one watermark, which is exactly what the office sink must forward whole.
    #
    # Pinned rather than generated for the same reason its siblings are: the
    # generator's seeded isolated root holds no office surface and no retired
    # placement, so there is nothing there to produce this batch from.
    "patch_delete_gesture.json",
    # The office write-verbs milestone (WV-H3, 2026-08-16): a FOLDER change as
    # one patch frame — a single ``office_surface`` subset upsert carrying the
    # three fields ``update_surface`` moves, ``coalesced_count`` 2 because the
    # paired ``office.surface.updated`` rides the batch and folds to nothing.
    #
    # Worth pinning across both repos because this row is the one whose SHAPE
    # the two sides could most easily disagree about without noticing: it is a
    # SUBSET merge onto the office row, unlike its ``office_actor`` sibling's
    # complete-row replace, so a launcher that folded it as a replace would
    # silently drop the actor list on every folder rename and hermes would have
    # no way to see it. The launcher folds these exact bytes through its real
    # read model and asserts the untouched fields survive.
    #
    # Pinned rather than generated for the same reason its siblings are: the
    # generator's seeded isolated root holds no office surface.
    "patch_office_surface.json",
    "patch_coverage_manifest.json",
)

#: Everything ``MANIFEST.sha256`` pins, in manifest order.
MANIFEST_FILES = GENERATED_FRAME_FILES + PINNED_ONLY_FILES
_TIME_KEYS = {
    "generated_at",
    "captured_at",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "last_heartbeat_at",
}
_VOLATILE_METRICS = {
    "build_ms",
    "snapshot_bytes",
    "event_log_bytes",
    "projection_age_ms",
    # running_work row fields that answer questions about THIS run rather than
    # about the contract: the generator's own OS process id, and seconds counted
    # from a fixed spawn stamp to whenever the script happened to execute. Both
    # would otherwise change the committed bytes on every regeneration. The
    # honesty fields beside them — `pid_verified`, `status` — are deliberately
    # NOT normalized: the seed pins them by construction (a NULL spawn baseline
    # is unprovable identity, so the row is `unknown`/`pid_verified: false` on
    # every platform), which is what makes those two bytes reviewable.
    "pid",
    "elapsed_seconds",
}
_VOLATILE_METRIC_MAPS = {
    # Each section is timed independently while the parity snapshot is built.
    # Normalizing only the container key keeps configured/contractual durations
    # elsewhere in the frame intact while removing scheduler and filesystem
    # jitter from every current and future section name.
    "sections_ms",
}
_MACHINE_PROBED_FLAGS = {
    # container key -> (per-entry flag key, pinned value)
    #
    # The ONE value in the frame that answers a question about the operator's
    # DISK rather than about the isolated runtime root. See the module docstring
    # for the full derivation: `repo_scopes[*].resolved` is a probe of
    # machine-local root bindings, so without this pin the script emits
    # different bytes on a configured operator box and an isolated generator.
    #
    # Only the flag is pinned. `label` is contractual and survives untouched, so
    # this stays a normalization rather than a rewrite of the section.
    "repo_scopes": ("resolved", True),
}


def _normalize(value: Any, *, isolated_root: Path, key: str = "") -> Any:
    if isinstance(value, dict):
        if key in _VOLATILE_METRIC_MAPS:
            return {str(item_key): 0 for item_key in value}
        normalized = {
            str(item_key): _normalize(item, isolated_root=isolated_root, key=str(item_key))
            for item_key, item in value.items()
        }
        if key == "runtime_root" and "fingerprint" in normalized:
            normalized["fingerprint"] = "isolated-runtime"
        if key in _MACHINE_PROBED_FLAGS:
            flag_key, pinned = _MACHINE_PROBED_FLAGS[key]
            for entry in normalized.values():
                if isinstance(entry, dict) and flag_key in entry:
                    entry[flag_key] = pinned
        return normalized
    if isinstance(value, list):
        return [_normalize(item, isolated_root=isolated_root) for item in value]
    if key in _TIME_KEYS and value is not None:
        return FIXED_TIME
    if key in _VOLATILE_METRICS and value is not None:
        return 0
    if isinstance(value, str):
        root = str(isolated_root)
        return value.replace(root, "<isolated-root>").replace(root.replace("\\", "/"), "<isolated-root>")
    return value


def _fixture_persona_instance():
    """The seeded persona-instance row, built once and written by two callers.

    :func:`_seed_running_work_owner` writes it to CREATE the row;
    :func:`_build_stale_first_convergence_pair` writes it again to move a
    fingerprinted input's mtime without appending an event. Constructing it in
    one place is what keeps the second write byte-identical in intent to the
    first — a second copy of this constructor would drift and the pair would then
    be perturbing something the running-work golden never carried.
    """

    from agent_runtime import paths
    from agent_runtime.models import PersonaInstance, WorkerSessionState

    return PersonaInstance(
        id=FIXTURE_INSTANCE_ID,
        persona_id=FIXTURE_PERSONA_ID,
        role="specialist",
        display_name="Fixture Agent",
        profile_id=None,
        runtime_root=str(paths.store_root()),
        state=WorkerSessionState.IDLE,
    )


#: How many builds :func:`_build_stale_first_convergence_pair` will pay for
#: before declaring the persisted key non-convergent. Two are normally needed on
#: a virgin store (the build is not a pure reader — it materializes
#: persona-instance rows and creates the chat SessionDB — while the key is taken
#: PRE-build on purpose), so a bound of four leaves headroom and still fails
#: loudly rather than looping. Same shape and same reason as the
#: ``converge_persisted_core`` helper the cache tests use.
_CONVERGENCE_BUILDS = 4


def _build_stale_first_convergence_pair() -> tuple[dict, dict]:
    """BO-1: the boot's stale paint and its authoritative replacement, same offset.

    =========================================================================
    WHAT THE PAIR PINS, AND WHY IT IS A PAIR
    =========================================================================

    EG-3.1's mismatch half: a boot whose persisted core does not match the store
    paints that core IMMEDIATELY wearing ``parity.freshness.state = "stale"``,
    and replaces it with an authoritative hydrate when the full build finishes.
    The launcher converges on the second frame through
    ``MissionReadModel.staleHeldAwaitsAuthoritative`` / ``_convergesStaleHeld``.

    **The offsets are EQUAL, and that is the whole contract.** The launcher's
    ordinary sequence gate is strict ``>``; the stale-held convergence is its
    ONE exemption. So a producer "optimization" that deduped the same-offset
    re-hydrate would freeze every launcher on a permanently stale canvas — and,
    before these goldens, would have reddened zero tests in either repo (the
    2026-08-21 boot-observability survey's scariest unguarded edge). Neither
    frame alone says anything; the relation between them is the fixture.

    =========================================================================
    THE REAL PRODUCER PATH, AND THE ONE ARRANGEMENT
    =========================================================================

    Both frames come out of ``stream.hydrate_frame`` exactly as
    ``stream_frames`` calls it at its head: frame 1 wraps
    ``core_cache.take_stale_first_core``'s labelled core, frame 2 is a real
    gated build (lane armed, ``consult`` demotes on the mismatch, the build runs
    and ``label_core`` stamps ``core_source="rebuilt"``). Nothing is hand-shaped.

    The single arrangement is HOW the mismatch is produced, and the choice is
    forced rather than convenient: it must move a fingerprinted input WITHOUT
    appending an event, because an append would move the log's tail and the two
    frames would no longer be a same-offset pair. That writer class is not
    hypothetical — it is the one ``core_cache``'s own header names as the reason
    the refused event-offset key could never have worked ("two shipped incidents
    came from writers that mutate durable state with NO EventLog event"), and it
    is exactly what produces this boot shape in the field: the store's durable
    state moved, the log did not, so the boot's stale paint and its replacement
    describe the same log position. Here it is a real writer doing a real write
    — ``PersonaInstanceStore._write`` re-persisting the seeded row through
    ``atomic_json_write``, whose rename always moves mtime.

    Returns ``(stale_frame, authoritative_frame)``. Every producer fact the pair
    exists to carry is asserted HERE, at generation time: a silently-fresh first
    frame or a silently-advanced second offset would otherwise be committed as a
    golden and pin the bug instead of the contract.
    """

    from agent_runtime import core_cache
    from agent_runtime.parity import events_position
    from agent_runtime.persona_assignments import PersonaInstanceStore
    from agent_runtime.snapshot import build_snapshot
    from agent_runtime.stream import hydrate_frame

    # 1. Settle the persisted key against the store, so the mismatch below is
    #    the one this fixture arranges and not leftover build-time churn.
    for _ in range(_CONVERGENCE_BUILDS):
        core_cache.core_path().unlink(missing_ok=True)
        core_cache.sidecar_path().unlink(missing_ok=True)
        core_cache.reset_process_state()
        build_snapshot()
        if core_cache.read_persisted_core().matched:
            break
    else:
        raise AssertionError(
            "the persisted core's fingerprint never converged, so there is no "
            "cache-hit baseline for the stale-first pair to diverge from"
        )

    # 2. A durable write that appends NO event: the store moves, the log does
    #    not. See the docstring for why this class and no other.
    idle_offset = events_position()["event_offset"]
    PersonaInstanceStore()._write(_fixture_persona_instance())
    assert events_position()["event_offset"] == idle_offset, (
        "the perturbation appended an event, so the log's tail moved and the "
        "two frames below cannot be a SAME-OFFSET pair — which is the only "
        "thing this fixture exists to pin"
    )
    assert not core_cache.read_persisted_core().matched, (
        "the persisted core still matches, so the boot below would serve it "
        "authoritative and there would be no stale paint to pin"
    )

    # 3. A fresh serve boot over that store: the stale paint, then the real
    #    gated build, through the production constructor both times.
    core_cache.reset_process_state()
    stale_core = core_cache.take_stale_first_core(caller="cli")
    assert stale_core is not None, (
        "take_stale_first_core declined, so this fixture would pin an empty "
        "canvas rather than EG-3.1's mismatch half"
    )
    stale_frame = hydrate_frame(snapshot=stale_core, caller="cli")
    authoritative_frame = hydrate_frame(caller="cli")

    stale_parity = stale_frame["core"]["parity"]
    authoritative_parity = authoritative_frame["core"]["parity"]
    assert stale_parity["freshness"]["state"] == "stale", stale_parity["freshness"]
    assert stale_parity["core_stale"] is True, stale_parity
    assert stale_parity["core_source"] == "cache", stale_parity
    # The non-stale token, pinned on real producer bytes for the first time. The
    # launcher's wiring-test fixtures spell it ``live``; nothing compared it, so
    # the drift was free. This golden makes ``fresh`` the byte on the wire.
    assert authoritative_parity["freshness"]["state"] == "fresh", authoritative_parity[
        "freshness"
    ]
    assert "core_stale" not in authoritative_parity, authoritative_parity
    assert authoritative_parity["core_source"] == "rebuilt", authoritative_parity

    stale_offset = stale_frame["watermark"]["event_offset"]
    authoritative_offset = authoritative_frame["watermark"]["event_offset"]
    assert stale_offset == authoritative_offset == idle_offset, (
        f"the pair is not same-offset: stale={stale_offset} "
        f"authoritative={authoritative_offset} idle store at {idle_offset}. "
        "A launcher holding the stale core converges on the second frame only "
        "through the equal-offset exemption; at different offsets these goldens "
        "would pin the ordinary sequence path and prove nothing"
    )
    assert stale_frame["core"] != authoritative_frame["core"], (
        "the two cores are identical, so this pair could not tell a converging "
        "client from a frozen one"
    )
    return stale_frame, authoritative_frame


def _seed_running_work_owner() -> None:
    """Seed one OWNED and one UNOWNED background delegation into the isolated root.

    This is the cross-repo pin for the defect the ``owner`` block exists to
    prevent: ``running_work``'s delegation lane used to emit
    ``owner: {persona_id: null, persona_instance_id: null}`` on every row, and
    Mission Control's Activity surface groups BY owner — so a background
    ``delegate_task`` could never appear there at all. Producer-side and
    consumer-side tests both passed the whole time; only the JOIN was broken,
    which is exactly what this fixture family is for.

    Real writers throughout: the persona instance goes through
    ``PersonaInstanceStore``, the delegations through
    ``async_delegation._persist_dispatch``. The single seeded deviation is the
    NULL ``owner_started_at`` — see below.
    """

    from agent_runtime.persona_assignments import PersonaInstanceStore
    from tools import async_delegation

    store = PersonaInstanceStore()
    store._write(_fixture_persona_instance())

    for delegation_id, session in (
        ("deleg_fixture_owned", OWNED_CHAT_SESSION),
        ("deleg_fixture_unowned", UNOWNED_SESSION),
    ):
        async_delegation._persist_dispatch(
            {
                "delegation_id": delegation_id,
                "session_key": session,
                "parent_session_id": session,
                "dispatched_at": FIXTURE_DISPATCHED_AT,
                "goal": f"fixture goal for {delegation_id}",
            }
        )

    # ``_persist_dispatch`` stamps the writer's real kernel start ticks, which
    # makes PID identity PROVABLE on some platforms and unreadable on others —
    # i.e. the emitted `status`/`pid_verified` bytes would depend on who ran the
    # script. Clearing the baseline pins the ONE deterministic verdict every
    # platform agrees on (`no_baseline` -> unproven identity -> `unknown`), so
    # the fixture stays byte-reproducible without normalizing away the two
    # honesty fields a reviewer most needs to see. The row is still carried, and
    # carrying it is the point: owner attribution is independent of whether the
    # runtime could prove the process is alive.
    with async_delegation._transaction() as conn:
        conn.execute("UPDATE async_delegations SET owner_started_at=NULL")


def _write_json(name: str, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    (FIXTURE_ROOT / name).write_text(payload, encoding="utf-8", newline="\n")


def _write_manifest() -> None:
    lines = []
    for name in MANIFEST_FILES:
        digest = hashlib.sha256((FIXTURE_ROOT / name).read_bytes()).hexdigest()
        lines.append(f"{digest}  {name}")
    (FIXTURE_ROOT / "MANIFEST.sha256").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )


def main() -> int:
    # SessionDB keeps a process-lifetime SQLite handle on Windows.  Ignoring a
    # cleanup race here lets the interpreter release that handle at exit; the
    # fixture bytes themselves never contain or depend on the temporary path.
    with tempfile.TemporaryDirectory(
        prefix="hermes-stream-fixtures-", ignore_cleanup_errors=True
    ) as temp:
        isolated_root = Path(temp)
        hermes_home = isolated_root / "hermes"
        runtime_root = isolated_root / "runtime"
        hermes_home.mkdir()
        runtime_root.mkdir()
        os.environ["HERMES_HOME"] = str(hermes_home)
        os.environ["HERMES_HEAD_HOME"] = str(hermes_home)
        os.environ["HERMES_AGENT_RUNTIME_ROOT"] = str(runtime_root)
        os.environ["LOCALAPPDATA"] = str(isolated_root / "local")

        from datetime import datetime, timedelta, timezone

        from agent_runtime.events import EventLog
        from agent_runtime.models import Event
        from agent_runtime.serde import to_jsonable
        from agent_runtime.stream import (
            delta_batch_frame,
            delta_frame,
            heartbeat_frame,
            hydrate_frame,
        )

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

        # LAST, and after `batch` is closed: seeding writes a
        # `persona_instance.created` event, which would otherwise land in the
        # delta/delta_batch goldens and churn their bytes for an unrelated reason.
        _seed_running_work_owner()
        owner_hydrate = hydrate_frame()
        owner_rows = owner_hydrate["core"]["running_work"]["rows"]
        # Assert the PRODUCER fact this fixture exists to carry, at generation
        # time. A silently-empty or silently-ownerless section would otherwise be
        # committed as a golden and pin the bug instead of the fix.
        assert len(owner_rows) == 2, owner_rows
        by_id = {row["work_id"]: row for row in owner_rows}
        assert by_id["delegation:deleg_fixture_owned"]["owner"] == {
            "persona_id": FIXTURE_PERSONA_ID,
            "persona_instance_id": FIXTURE_INSTANCE_ID,
            "session_id": OWNED_CHAT_SESSION,
        }, by_id["delegation:deleg_fixture_owned"]
        unowned_owner = by_id["delegation:deleg_fixture_unowned"]["owner"]
        assert unowned_owner["persona_id"] is None, unowned_owner
        assert unowned_owner["persona_instance_id"] is None, unowned_owner

        # LAST of all, and after every frame above is closed: this one builds
        # real cores of its own (a convergence loop, then a gated rebuild) and
        # re-persists the seeded persona instance, so running it earlier would
        # churn the goldens above for reasons that have nothing to do with them.
        stale_first, authoritative_same_offset = _build_stale_first_convergence_pair()

        frames = {
            "hydrate.json": hydrate,
            "delta.json": delta_frame(first, offset=batch[0][0], snapshot=core),
            "heartbeat.json": heartbeat_frame(offset=7),
            "delta_batch.json": delta_batch_frame(batch, snapshot=core),
            "hydrate_running_work_owner.json": owner_hydrate,
            "hydrate_stale_first.json": stale_first,
            "hydrate_authoritative_same_offset.json": authoritative_same_offset,
        }
        # A frame that silently drops out of the built set while staying in
        # MANIFEST_FILES would become hand-maintained without anyone saying so —
        # exactly the undocumented split this constant pair exists to retire.
        assert tuple(frames) == GENERATED_FRAME_FILES, (
            "main() must build exactly GENERATED_FRAME_FILES; anything else "
            "belongs in PINNED_ONLY_FILES with a recorded reason"
        )
        prompt_observability = core.get("prompt_observability") or {}
        assert "default_flow" not in prompt_observability
        for name in ("delta.json", "delta_batch.json"):
            assert frames[name]["core"] is core
            assert frames[name]["core"]["parity"]["capabilities"] == core["parity"][
                "capabilities"
            ]
            assert frames[name]["core"]["parity"]["completeness"] == core["parity"][
                "completeness"
            ]
        for name, frame in frames.items():
            _write_json(
                name,
                _normalize(to_jsonable(frame), isolated_root=isolated_root),
            )
        _write_manifest()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
