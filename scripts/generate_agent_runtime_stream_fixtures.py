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
:func:`main` regenerates the frames in :data:`GENERATED_FRAME_FILES`. The
files in :data:`PINNED_ONLY_FILES` (that tuple is the authority for the count)
are hand-authored and are only HASHED into
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
    # S0 of the placement verb (hermes
    # ``docs/agent-runtime-harness/06-office-and-board.md``; the plan file this
    # comment used to cite shipped and was deleted 2026-08-27). The
    # SAME one-call ``perform_agent_create`` observed by two subscribers, so the
    # pair pins both arms of that plan's §A.11 hazard. Read them as a PAIR for
    # the same reason the convergence hydrates are a pair — see
    # :func:`_build_agent_create_frames`.
    "patch_agent_create.json",
    "delta_agent_create_narrow_profile.json",
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

# ── S0 of the placement verb: the one-call create, seen from two subscribers ──
#
# Identities the agent-create pair seeds. FIXTURE constants, like the ones
# above, but their DERIVATIONS are contractual and are asserted at generation
# time rather than typed twice: the persona-instance id is
# ``persona_instance_id_for_placement(FIXTURE_CREATE_PLACEMENT_ID)`` and the
# office actor key is that same id, because ``placement_actor_payload`` is
# instance-keyed by construction.
FIXTURE_CREATE_PERSONA_ID = "qa"
FIXTURE_CREATE_PERSONA_DISPLAY_NAME = "QA Agent"
FIXTURE_CREATE_WORKSPACE_ID = "ws_office_pilot"
FIXTURE_CREATE_PLACEMENT_ID = "qa_fixture_agent_2"
FIXTURE_CREATE_INSTANCE_ID = "personainst_qa_fixture_agent_2"
FIXTURE_CREATE_IDEMPOTENCY_KEY = "fixture-agent-create"
FIXTURE_CREATE_POSITION = (0.0, 0.0)
#: ``persona_chat_session_id_for`` mints ``persona_chat_<instance>_<12 hex of a
#: uuid4>``. The tail is the one value in this create that is random rather than
#: derived, and it rides four places in the emitted frames (the roster patch's
#: three session keys and the ``chat_opened`` payload). Pinning it AT THE SOURCE
#: — see :func:`_build_agent_create_frames` — rather than rewriting it after the
#: fact keeps the frame the producer's own bytes; a post-hoc substitution would
#: be a second normalizer able to disagree with the first.
FIXTURE_CREATE_CHAT_SESSION_ID = f"persona_chat_{FIXTURE_CREATE_INSTANCE_ID}_0123456789ab"

#: What the launcher declares today (``kMissionFoldDeclaredEntities``, used
#: verbatim by both of its lanes) — the WIDE arm, whose batch is promoted.
FIXTURE_WIDE_FOLD_ENTITIES = frozenset(
    {
        "persona_instance",
        "incident",
        "office_actor",
        "office_actor_lifecycle",
        "persona_instance_create",
    }
)
#: The NARROW arm: a subscriber that declares only the historical two. It is
#: exactly ``patch_coverage.HISTORICAL_FOLD_ENTITIES`` — what a client that says
#: nothing is taken to fold, and what the placement plan's §A.11 names as the
#: room-wide set the moment one narrow subscriber joins. Spelled out rather than
#: imported so the golden's arm is legible in the file that writes it; the
#: generator asserts the two are equal.
FIXTURE_NARROW_FOLD_ENTITIES = frozenset({"persona_instance", "incident"})

#: Synthetic event-log positions for the agent-create pair, in order:
#: ``base_offset`` first, then one per batched event.
#:
#: **Why these are pinned and the other goldens' offsets are not.** Every other
#: generated frame's offsets are real byte positions in a log whose only entries
#: are two ``state.reconciled`` events with fixed payloads, so they reproduce
#: anywhere. A CREATE's events embed absolute paths — ``runtime_root`` on the
#: roster patch, ``chat_head_home`` on ``persona_instance.chat_opened`` — so the
#: log's byte positions are a function of where the generating machine puts its
#: temporary directory. That is the same class of machine-dependence
#: :data:`_MACHINE_PROBED_FLAGS` retires, and it is retired the same way.
#:
#: What the frames CONTRACT is the ORDER and the strict inequalities —
#: ``base_offset`` < every ``seq`` < ``watermark.event_offset`` — because the
#: launcher folds only when its held watermark equals ``base_offset`` and its
#: sequence gate is strict ``>``. Magnitudes say nothing to either repo. The
#: stride is deliberately round so nobody mistakes them for measured sizes.
_FIXTURE_CREATE_OFFSET_BASE = 4096
_FIXTURE_CREATE_OFFSET_STRIDE = 512

#: The two goldens the agent-create pair writes. Named once so ``main`` can ask
#: "does this frame opt into the extra normalization" without re-listing them.
AGENT_CREATE_FRAME_FILES = (
    "patch_agent_create.json",
    "delta_agent_create_narrow_profile.json",
)

#: Values in the agent-create pair that answer a question about the generating
#: INTERPRETER rather than about the wire contract, pinned to the value the
#: committed goldens carry.
#:
#: Same class and same treatment as :data:`_MACHINE_PROBED_FLAGS`, and the
#: derivation is worth stating once. ``persona_instance_summary`` enriches a row
#: with ``tool_count`` / ``blocked_tools_count`` / ``effective_toolsets`` /
#: ``mutation_boundary`` ONLY when the persona behind it resolves — which is why
#: no golden before this pair carried them (``hydrate_running_work_owner.json``
#: seeds an instance whose persona deliberately does not exist). All four come
#: out of ``tool_visibility.resolve_tool_visibility``, which counts the TOOL
#: REGISTRY, and the registry is populated by import: a machine missing an
#: optional tool package registers fewer tools and would emit different bytes.
#:
#: Only the four values are pinned. Every key path around them survives
#: untouched, so the shape gate still fails the moment the producer stops
#: emitting one of them — this is a normalization, not a rewrite of the block.
_REGISTRY_PROBED_VALUES: dict[str, Any] = {
    "mutation_boundary": {
        "can_mutate_files": True,
        "can_run_terminal": True,
        "mutating_tools": [
            "patch",
            "terminal",
            "write_file"
        ]
    },
    "tool_count": 79,
    "blocked_tools_count": 17,
    "effective_toolsets": [
        "agent_chat",
        "bfl",
        "board",
        "browser",
        "browser-cdp",
        "clarify",
        "code_execution",
        "computer_use",
        "cronjob",
        "delegation",
        "discord",
        "discord_admin",
        "feishu_doc",
        "feishu_drive",
        "file",
        "hermes-yuanbao",
        "homeassistant",
        "image_gen",
        "kanban",
        "memory",
        "project",
        "session_search",
        "skills",
        "spotify",
        "terminal",
        "todo",
        "tts",
        "video",
        "video_gen",
        "vision",
        "web",
        "x_search"
    ]
}

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
    # The instant-workspace-switching milestone (WS1, 2026-09-01): a WORKSPACE
    # SWITCH as one patch frame — a single ``scope`` upsert carrying BOTH
    # pointers, ``coalesced_count`` 2 because the paired ``workspace.activated``
    # rides the batch and folds to nothing.
    #
    # Worth pinning across both repos because ``scope`` is the first entity that
    # is not a keyed table row: it writes two TOP-LEVEL core scalars, and every
    # per-row ``active`` flag the launcher renders is DERIVED from them at parse
    # time rather than sent. The bytes pinned here are what makes "the patch and
    # the core flip the same flags" a checkable claim on both sides instead of a
    # shared intention. The second pointer is the one a reader will want to argue
    # about: a plain workspace switch does not move the realm, and the row
    # carries it anyway — that is the contract (both, always), and this fixture
    # is where it is stated in bytes.
    #
    # Pinned rather than generated for the same reason its siblings are: the
    # generator's seeded isolated root holds no realm and no second workspace to
    # switch between.
    "patch_scope.json",
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


#: Keys whose integer value is an EVENT-LOG BYTE POSITION.
#:
#: Only consulted when a caller passes an ``offset_map`` (today: the
#: agent-create pair, whose events embed absolute paths — see
#: :data:`_FIXTURE_CREATE_OFFSET_BASE`). Every other generated frame's offsets
#: are already machine-independent and are left exactly as the producer built
#: them.
_OFFSET_KEYS = {"event_offset", "base_offset", "seq"}

#: Wall-clock stamps that are volatile for SOME frames and CONTRACTUAL for
#: others, so they can never join :data:`_TIME_KEYS`.
#:
#: ``delta.json`` / ``delta_batch.json`` seed their events with fixed stamps one
#: second apart, and the ONE-second gap is the batch's own evidence that it
#: coalesced two distinct events — folding both onto :data:`FIXED_TIME` would
#: erase it. A frame built off a REAL store write has no such luxury: its
#: ``ts`` is whenever the generator ran. So the two keys are opt-in per frame.
_OPTIONAL_TIME_KEYS = frozenset({"ts", "last_event_ts"})


def _normalize(
    value: Any,
    *,
    isolated_root: Path,
    key: str = "",
    time_keys: frozenset[str] = frozenset(),
    pinned_values: dict[str, Any] | None = None,
    offset_map: dict[int, int] | None = None,
) -> Any:
    """Volatile values out, contract in.

    ``time_keys`` adds to :data:`_TIME_KEYS` for THIS frame only (see
    :data:`_OPTIONAL_TIME_KEYS`); ``pinned_values`` replaces a key's whole value
    with a fixture constant (see :data:`_REGISTRY_PROBED_VALUES`); ``offset_map``
    rewrites event-log byte positions through a rank map (see
    :data:`_FIXTURE_CREATE_OFFSET_BASE`). All three default to "do nothing", so
    every frame that predates them normalizes byte-identically.
    """

    forward = dict(
        isolated_root=isolated_root,
        time_keys=time_keys,
        pinned_values=pinned_values,
        offset_map=offset_map,
    )
    if pinned_values is not None and key in pinned_values:
        return json.loads(json.dumps(pinned_values[key]))
    if (
        offset_map is not None
        and key in _OFFSET_KEYS
        and isinstance(value, int)
        and not isinstance(value, bool)
    ):
        if value not in offset_map:
            raise AssertionError(
                f"offset {value!r} under key {key!r} was not collected by "
                "_offset_rank_map, so this frame carries a byte position the "
                "pin cannot reach and the golden would churn per machine"
            )
        return offset_map[value]
    if isinstance(value, dict):
        if key in _VOLATILE_METRIC_MAPS:
            return {str(item_key): 0 for item_key in value}
        normalized = {
            str(item_key): _normalize(item, key=str(item_key), **forward)
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
        return [_normalize(item, **forward) for item in value]
    if (key in _TIME_KEYS or key in time_keys) and value is not None:
        return FIXED_TIME
    if key in _VOLATILE_METRICS and value is not None:
        return 0
    if isinstance(value, str):
        root = str(isolated_root)
        return value.replace(root, "<isolated-root>").replace(root.replace("\\", "/"), "<isolated-root>")
    return value


def _offset_rank_map(*frames: Any) -> dict[int, int]:
    """Every event-log byte position in ``frames``, ranked onto a fixed lattice.

    Ranked BY VALUE across the whole pair at once, so the two frames stay
    mutually consistent: the patch frame's ``base_offset`` and the demoted
    frame's ``watermark.event_offset`` describe the same batch and must not be
    pinned by two independent passes.
    """

    found: set[int] = set()

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for item_key, item in value.items():
                walk(item, str(item_key))
        elif isinstance(value, list):
            for item in value:
                walk(item, key)
        elif key in _OFFSET_KEYS and isinstance(value, int) and not isinstance(value, bool):
            found.add(value)

    for frame in frames:
        walk(frame)
    return {
        real: _FIXTURE_CREATE_OFFSET_BASE + _FIXTURE_CREATE_OFFSET_STRIDE * rank
        for rank, real in enumerate(sorted(found))
    }


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


def _build_agent_create_frames() -> tuple[dict, dict]:
    """S0: ONE ``perform_agent_create``, rendered for two different subscribers.

    =========================================================================
    WHAT THE PAIR PINS, AND WHY IT IS A PAIR
    =========================================================================

    The placement plan's D3 rules that the notification a client gets from a
    create is the ``office_actor`` + ``persona_instance`` create patch batch —
    NOT a surface-revision bump. F6 recorded that nobody had ever captured that
    frame: the 2026-08-24 "nothing notified the client" was observed against a
    hand-assembled ``persona instance create`` + ``office actor-upsert`` pair,
    and whether an out-of-process ``agent create`` reaches a connected launcher
    as ONE ``patch`` frame — or demotes, or is dropped at the fold — was open.
    ``patch_agent_create.json`` is that capture.

    Its sibling is the other arm of the plan's A.11 hazard.
    ``accepted_fold_entities`` takes the INTERSECTION across every subscriber in
    the room, so the moment one narrow-profile client subscribes the room loses
    ``office_actor`` and every placement DEMOTES to a full core for everyone.
    That is correct and it is expensive, and until now it was reasoned about
    rather than observed. ``delta_agent_create_narrow_profile.json`` is the SAME
    create — same batch, same store, same instant — rendered for a subscriber
    declaring only :data:`FIXTURE_NARROW_FOLD_ENTITIES`. Neither frame says
    anything alone: together they say the promotion decision is the
    SUBSCRIBER's declaration and nothing else about the create.

    =========================================================================
    THE REAL PRODUCER PATH, AND THE TWO ARRANGEMENTS
    =========================================================================

    The create is the production service — ``agent_create.perform_agent_create``,
    the same function ``runtime.agent.create`` and ``harness agent create`` both
    call — against a real seeded workspace. The frames come out of
    ``stream._batch_frames_with_liveness``, which IS the promotion decision:
    this generator does not choose ``patch`` or ``delta``, it asks, and it
    asserts what came back. A demote on the wide arm fails generation rather
    than quietly committing a golden that pins the bug.

    Two arrangements, both forced rather than convenient:

    1. **The chat root's random tail is pinned at the source.**
       ``persona_chat_session_id_for`` mints a ``uuid4`` tail, which would make
       these bytes differ on every run. It is replaced for the duration of the
       create only — see :data:`FIXTURE_CREATE_CHAT_SESSION_ID` for why at the
       source rather than after the fact.
    2. **Personas and the workspace are seeded first**, because a hermetic
       runtime root's roster is genuinely EMPTY (``agent_create``'s own UC-0
       note) and ``OfficeStore.ensure_surface`` refuses a workspace no record
       resolves (MC-8/P10) — the same precondition
       ``tests/agent_runtime/office_seed.py`` states for every office suite.
    3. **``HERMES_HEAD_HOME`` is asserted present, not assumed.**
       ``open_chat`` puts ``chat_head_home`` on the ``persona_instance.chat_opened``
       payload only when the head home is AUTHORITATIVE, and
       ``hermes_constants.hermes_head_home_is_authoritative`` reads that off an
       explicit ``HERMES_HEAD_HOME`` env value or a context-recorded outermost
       home — an ambient resolution is deliberately NOT authoritative. So the
       emitted frame's KEY SET depends on the caller's environment: ``main``
       exports the variable and gets the key, a rebuild under a bare runtime
       root does not, and the two would disagree about the golden's shape with
       nothing saying why. Measured, not reasoned: the shape gate reddened on
       exactly ``events[].event.payload.chat_head_home`` the first time this
       builder was driven from the test suite. The builder therefore pins the
       variable for the duration of the create, which is the same "state the
       precondition rather than inherit it" rule arrangement 2 follows.

    Returns ``(patch_frame, demoted_delta_frame)`` UN-normalized; the caller
    pins their offsets and stamps.
    """

    from agent_runtime import paths, persona_assignments
    from agent_runtime.agent_create import perform_agent_create
    from agent_runtime.events import EventLog
    from agent_runtime.models import AgentPersona
    from agent_runtime.office_store import OfficeStore
    from agent_runtime.parity import events_position
    from agent_runtime.patch_coverage import HISTORICAL_FOLD_ENTITIES
    from agent_runtime.persona_assignments import persona_instance_id_for_placement
    from agent_runtime.state_patches import STATE_PATCHED_EVENT_TYPE
    from agent_runtime.store import AgentStore, WorkspaceStore
    from agent_runtime.stream import _batch_frames_with_liveness

    assert FIXTURE_NARROW_FOLD_ENTITIES == HISTORICAL_FOLD_ENTITIES, (
        "the narrow arm is supposed to BE the historical default set; it has "
        f"drifted to {sorted(HISTORICAL_FOLD_ENTITIES)}"
    )
    assert (
        persona_instance_id_for_placement(FIXTURE_CREATE_PLACEMENT_ID)
        == FIXTURE_CREATE_INSTANCE_ID
    )

    AgentStore().save(
        AgentPersona(
            id=FIXTURE_CREATE_PERSONA_ID,
            display_name=FIXTURE_CREATE_PERSONA_DISPLAY_NAME,
            role="qa",
            model=None,
            provider=None,
            api_mode=None,
            toolsets=[],
            system_prompt_path="",
        )
    )
    WorkspaceStore().create(
        name="Office Pilot", workspace_id=FIXTURE_CREATE_WORKSPACE_ID
    )
    OfficeStore().ensure_surface(
        FIXTURE_CREATE_WORKSPACE_ID, created_by="fixture-seed"
    )

    base_offset = events_position()["event_offset"]

    minted = persona_assignments.persona_chat_session_id_for
    persona_assignments.persona_chat_session_id_for = (
        lambda _instance_id: FIXTURE_CREATE_CHAT_SESSION_ID
    )
    head_home_before = os.environ.get("HERMES_HEAD_HOME")
    os.environ.setdefault(
        "HERMES_HEAD_HOME", os.environ.get("HERMES_HOME") or str(paths.store_root())
    )
    try:
        outcome = perform_agent_create(
            {
                "persona_id": FIXTURE_CREATE_PERSONA_ID,
                "workspace_id": FIXTURE_CREATE_WORKSPACE_ID,
                "position": list(FIXTURE_CREATE_POSITION),
                "idempotency_key": FIXTURE_CREATE_IDEMPOTENCY_KEY,
                "placement_id": FIXTURE_CREATE_PLACEMENT_ID,
            }
        )
    finally:
        persona_assignments.persona_chat_session_id_for = minted
        if head_home_before is None:
            os.environ.pop("HERMES_HEAD_HOME", None)
        else:
            os.environ["HERMES_HEAD_HOME"] = head_home_before

    assert outcome.refusal is None, outcome.refusal
    result = outcome.result
    assert result["persona_instance_id"] == FIXTURE_CREATE_INSTANCE_ID, result
    assert result["actor_key"] == FIXTURE_CREATE_INSTANCE_ID, result
    assert result["default_chat_session_id"] == FIXTURE_CREATE_CHAT_SESSION_ID, result

    batch = list(EventLog().iter_from_offset(base_offset))
    # The create's whole event trail, asserted at generation time. A create that
    # started emitting a fifth event — or stopped emitting one of these — would
    # otherwise silently regenerate a golden that no longer describes the
    # gesture the plan's D3 is about.
    assert [event.type for _, event in batch] == [
        STATE_PATCHED_EVENT_TYPE,
        "persona_instance.chat_opened",
        STATE_PATCHED_EVENT_TYPE,
        "office.actor.upserted",
    ], [event.type for _, event in batch]
    patched = [
        event.payload for _, event in batch if event.type == STATE_PATCHED_EVENT_TYPE
    ]
    assert [row["entity"] for row in patched] == [
        "persona_instance",
        "office_actor",
    ], patched
    assert all(row["op"] == "upsert" for row in patched), patched
    # D3's load-bearing stamp: the launcher's generic persona-instance fold
    # inserts-on-absent ONLY when ``created`` is present, so a create that
    # stopped stamping it would be answered with ``patch_without_target`` and a
    # full re-hydrate at every connected client.
    assert all(row.get("created") is True for row in patched), patched
    chat_opened = next(
        event.payload
        for _, event in batch
        if event.type == "persona_instance.chat_opened"
    )
    assert chat_opened.get("chat_head_home"), (
        "the chat_opened payload lost its head home, so the frame's key set "
        "now depends on whether HERMES_HEAD_HOME was exported — see "
        "arrangement 3"
    )

    wide = list(
        _batch_frames_with_liveness(
            batch,
            base_offset=base_offset,
            delta_patches=True,
            resync=False,
            heartbeat_interval_seconds=5.0,
            fold_entities=FIXTURE_WIDE_FOLD_ENTITIES,
            caller="cli",
        )
    )
    assert len(wide) == 1, [frame.get("type") for frame in wide]
    patch_frame = wide[0]
    # THE S0 QUESTION, asked of the promotion decision itself. A demote here is
    # the plan's D-risk arriving, and it must stop generation rather than commit
    # a golden that pins it.
    assert patch_frame["type"] == "patch", (
        "the wide-profile subscriber's batch DEMOTED to a full core — S0's "
        f"answer is no. Frame type: {patch_frame.get('type')!r}. The remedy is "
        "in patch_coverage or the read model, not in this generator."
    )
    assert [row["entity"] for row in patch_frame["patches"]] == [
        "persona_instance",
        "office_actor",
    ], patch_frame["patches"]
    assert all(row.get("created") is True for row in patch_frame["patches"])
    assert patch_frame["base_offset"] == base_offset

    narrow = list(
        _batch_frames_with_liveness(
            batch,
            base_offset=base_offset,
            delta_patches=True,
            resync=False,
            heartbeat_interval_seconds=5.0,
            fold_entities=FIXTURE_NARROW_FOLD_ENTITIES,
            caller="cli",
        )
    )
    assert len(narrow) == 1, [frame.get("type") for frame in narrow]
    demoted = narrow[0]
    assert demoted["type"] == "delta", demoted["type"]
    # The demote is only CORRECT because it carries the whole core: the client
    # that could not fold the patch is re-baselined rather than left stale.
    assert isinstance(demoted.get("core"), dict)
    actors = demoted["core"]["offices"][FIXTURE_CREATE_WORKSPACE_ID]["actors"]
    assert any(
        actor["actor_key"] == FIXTURE_CREATE_INSTANCE_ID for actor in actors
    ), actors
    return patch_frame, demoted


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

        # LAST of all, after even that: this one seeds a persona, a workspace
        # and an office surface, then performs a REAL agent create — four events
        # on the log and a store the frames above were all built against.
        # Running it earlier would rewrite every golden above with a roster row
        # and a placement that have nothing to do with them.
        agent_create_patch, agent_create_demoted = _build_agent_create_frames()

        frames = {
            "hydrate.json": hydrate,
            "delta.json": delta_frame(first, offset=batch[0][0], snapshot=core),
            "heartbeat.json": heartbeat_frame(offset=7),
            "delta_batch.json": delta_batch_frame(batch, snapshot=core),
            "hydrate_running_work_owner.json": owner_hydrate,
            "hydrate_stale_first.json": stale_first,
            "hydrate_authoritative_same_offset.json": authoritative_same_offset,
            "patch_agent_create.json": agent_create_patch,
            "delta_agent_create_narrow_profile.json": agent_create_demoted,
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
        # The agent-create pair alone carries real-store wall-clock stamps,
        # registry-probed tool scalars and path-dependent byte offsets, so it
        # alone opts into the extra normalization. Every other frame keeps the
        # rules it has always had — see :func:`_normalize`.
        agent_create_rules = dict(
            time_keys=_OPTIONAL_TIME_KEYS,
            pinned_values=_REGISTRY_PROBED_VALUES,
            offset_map=_offset_rank_map(
                to_jsonable(agent_create_patch), to_jsonable(agent_create_demoted)
            ),
        )
        for name, frame in frames.items():
            extra = agent_create_rules if name in AGENT_CREATE_FRAME_FILES else {}
            _write_json(
                name,
                _normalize(to_jsonable(frame), isolated_root=isolated_root, **extra),
            )
        _write_manifest()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
