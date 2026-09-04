from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import re
from typing import Any

from .states import RunState, WorkerSessionState


# The structural prefix every persona-instance id carries. Defined at this low
# layer (no import back into persona_assignments, which would be a cycle) so the
# PersonaInstance backfill can recognize instance-shaped tokens. The id authority
# in ``persona_assignments`` re-exports this constant + predicate; do not fork a
# second copy.
PERSONA_INSTANCE_ID_PREFIX = "personainst_"


def looks_like_persona_instance_id(token: object) -> bool:
    """True when ``token`` is structurally a persona-instance id (``personainst_*``).

    A steering-parent SET (``steered_by``) and its denormalized mirror
    (``spawned_by``) may hold ONLY these. A non-instance principal — the
    operator, a bare persona/role token, any provenance string — is never a
    steering parent, so it must never be mirrored into a steering field from
    provenance. Read projections filter on this predicate so a legacy row that
    already carries such a value renders as an accounted anomaly, never as a
    phantom "steered by <principal>" edge.
    """
    return isinstance(token, str) and token.strip().startswith(PERSONA_INSTANCE_ID_PREFIX)


#: THE deliberate-placement discriminator, mirrored byte-for-byte from the
#: launcher's ``_deliberatePlacementSuffix``
#: (``mission_agent_identity.dart:121``). Both repos must answer the same
#: question the same way about the same id: hermes derives the instance id from
#: the placement id (``persona_instance_id_for_placement`` — prefix + token),
#: and the launcher then asks THIS pattern of the derived id to decide whether
#: the row is a deliberate placement or a conversational channel. An id that
#: clears one side and not the other is the wrong-alice incident of 2026-08-27:
#: a hand-typed ``--placement-id known_alice`` minted
#: ``personainst_known_alice``, which the launcher read as conversational,
#: folded into the operator-channel dedupe, and — newer-wins — evicted the
#: operator's own ``personainst_profile_alice`` from the roster.
#:
#: The ``_agent_`` marker is the load-bearing half, not the tail: a bare hex
#: tail would make any persona token ending in eight hex characters read as a
#: deliberate placement forever. Two tails are legal because two mints are
#: live — ``_agent_<hex8>`` is current, ``_agent_<n>`` is the legacy counter
#: still carried by rows that predate it.
DELIBERATE_PLACEMENT_SUFFIX = re.compile(r"_agent_(\d+|[0-9a-f]{8})$")


def looks_like_deliberate_placement(token: object) -> bool:
    """True when ``token`` ends in the deliberate-placement shape.

    Asked of a PLACEMENT id, not an instance id, because the tail survives the
    derivation unchanged (the prefix is prepended, the token is not rewritten),
    so fencing the input fences the derived id too.
    """
    return isinstance(token, str) and bool(DELIBERATE_PLACEMENT_SUFFIX.search(token.strip()))


#: The refusal a caller-supplied placement id earns when it would derive an
#: instance id neither repo's discriminator can classify. Spelled once and
#: spent by all three placement doors (``agent create`` and the two
#: ``persona instance`` verbs) so the operator reads one sentence whichever
#: door they knocked on.
PLACEMENT_ID_NOT_DISCRIMINABLE_REASON = "placement_id_not_discriminable"


def placement_id_not_discriminable_message(placement_id: str) -> str:
    """Name BOTH cures, because both are legitimate and they are not equivalent.

    Omitting the flag is right for a caller that only wants an agent placed;
    supplying the shape is right for a caller that is PREDICTING the actor key
    from the id it sent. Canonicalizing silently would serve the first and
    strand the second, which is why this refuses instead of rewriting.
    """
    return (
        f"invalid params: placement_id {placement_id!r} is not a deliberate-placement id "
        "— omit --placement-id to have one minted, or supply the "
        "<persona-token>_agent_<hex8> shape"
    )


@dataclass(slots=True)
class Workspace:
    id: str
    slug: str
    name: str
    created_at: datetime
    updated_at: datetime
    agent_ids: list[str] = field(default_factory=list)
    default_blueprint_id: str | None = None
    isolation: str = "soft"
    max_concurrent_lanes: int | None = None
    realm_id: str | None = None
    archived: bool = False
    schema_version: int = 1


@dataclass(slots=True)
class SkillTombstone:
    """One "this shared skill is deleted realm-wide" record.

    A RECORD rather than a bare id — the one place the ``deleted_workspace_ids``
    lift cannot be verbatim. Workspace ids are freshly minted, so a bare-id set
    can never block a legitimate re-creation; skill slugs are re-creatable
    NAMES, so the ledger must carry when the delete happened and be lifted by an
    explicit verb rather than out-aged by a same-name author.

    ``deleted_hash`` is the package content hash at delete time when a local
    copy existed to hash. It is receipts and forensics ("the thing you are
    restoring is/isn't the bytes you deleted") and is NEVER consulted to admit a
    package: an auto-supersede would let any member authoring a same-name skill
    silently override a realm-wide delete.

    ``restored_at`` makes this a per-slug STATE REGISTER rather than a bare
    delete record (RD-11, 2026-08-31). Before the union merge, ``restore_skill``
    removed the entry — an absence, which a union of two ledgers cannot tell
    apart from "the other member never heard about this delete", so every
    restore would have been silently undone by the next pull from a member who
    still held the tombstone. A restore is therefore a POSITIVE marker that can
    win a merge on its own timestamp. An entry with ``restored_at`` set blocks
    nothing (see ``store.active_skill_tombstones``) and is settled history the
    ledger cap prunes first; a later delete of the same slug REPLACES the whole
    entry, so ``restored_at`` returns to ``None`` rather than lingering beside a
    newer ``deleted_at``.
    """

    slug: str
    deleted_at: datetime
    deleted_hash: str | None = None
    restored_at: datetime | None = None


@dataclass(slots=True)
class WorkspaceLift:
    """One "this workspace id was taken back OFF the delete ledger" record.

    ``deleted_workspace_ids`` stays a bare-id ledger — the ids are freshly
    minted and never re-creatable, so an id that sits there forever blocks
    nothing (see :class:`SkillTombstone` for the asymmetry). What a bare id
    cannot express is a LIFT. ``default_scope`` removes the reserved local
    default workspace's id from that ledger when it turns up there, and under
    the RD-11 set-union merge a removal is an ABSENCE — indistinguishable from
    "that peer never heard about this delete" — so the next pull from any member
    still carrying the id put it straight back, and the lift stayed local until
    the cap aged it out (MEASURED 2026-08-31 by W2-H5; RULED 2026-09-04: a
    restore that never reaches peers reads as a failed restore).

    A lift is therefore a POSITIVE marker with a clock, exactly like
    ``SkillTombstone.restored_at``, and it travels in the realm JSON so it can
    win a merge on its own timestamp. ``deleted_at`` is what a LATER re-delete
    of the same id stamps here, so this is a per-workspace state register rather
    than a one-way flag: a fresh re-delete outranks a stale lift by the same
    comparison that makes a fresh lift outrank a stale delete, and an equal
    transition time resolves to the DELETE (a lift that loses a tie is one
    explicit verb away from being re-run; a delete that loses one is a
    resurrected workspace).
    """

    workspace_id: str
    restored_at: datetime
    deleted_at: datetime | None = None


@dataclass(slots=True)
class Realm:
    id: str
    slug: str
    name: str
    created_at: datetime
    updated_at: datetime
    server_id: str | None = None
    # Stable identity minted by the membership backend. This is a pointer,
    # never a copy of a member's local/stale default workspace contents.
    default_workspace_id: str | None = None
    default_workspace_name: str = "Default"
    default_workspace_version: int = 0
    workspace_ids: list[str] = field(default_factory=list)
    # Resurrection-guard ledger (ids only, bounded): workspaces DELETED from
    # this realm. Travels inside the realm JSON through realm sync so a member
    # that still holds a local copy neither republishes it nor re-adopts it on
    # pull (the Board.archived_card_ids / OfficeSurface.archived_actor_keys
    # idiom, lifted to workspace granularity).
    deleted_workspace_ids: list[str] = field(default_factory=list)
    # The PROPAGATING lift markers for the ledger above (see WorkspaceLift).
    # ADDITIVE at schema_version 1, for the same reason skill_tombstones is: a
    # bump would refuse every older member's realm load, so the compat cost is
    # instead an old member's save stripping the field — which degrades a lift
    # to exactly the local-until-age-out behaviour it replaces, never worse.
    # Written only at store.lift_deleted_workspace / WorkspaceStore.delete;
    # merged only by realm_sync.merge_workspace_lift_ledgers.
    workspace_lifts: list[WorkspaceLift] = field(default_factory=list)
    # The same resurrection guard for shared SKILLS, at record granularity (see
    # SkillTombstone). Travels in the realm JSON like the ledger above so a
    # member holding a live canonical copy of a deleted skill neither
    # republishes it nor re-adopts it on pull. ADDITIVE at schema_version 1 —
    # a bump would refuse every older member's realm load (serde.upgrade), so
    # the compat cost is instead an old member's save stripping the field.
    # Written only at RealmStore.tombstone_skill / restore_skill; matched only
    # through store.skill_tombstoned, never by open-coded slug comparison.
    skill_tombstones: list[SkillTombstone] = field(default_factory=list)
    # Which shared skills publish to this realm. Mode "all" (default,
    # back-compat) publishes every skill in the shared catalog including
    # future ones; "selected" publishes exactly skill_selection (empty list =
    # publish none). Travels realm-wide via realm sync (NOT an authority
    # field) — the selection is realm truth, converged last-publisher-wins.
    # Kept sorted + deduped at every write chokepoint
    # (RealmStore.set_skill_selection); slugs unknown to a member's local
    # catalog are preserved, never stripped, on an unrelated save.
    skill_publish_mode: str = "all"  # "all" | "selected"
    skill_selection: list[str] = field(default_factory=list)
    # Which persona definitions publish to this realm. ``workspace`` preserves
    # the pre-selection behavior: every persona required by a workspace roster
    # or Office placement travels. ``selected`` adds the explicit
    # ``agent_selection`` set while required references remain pinned so a
    # pulled workspace/office can never point at an absent persona definition.
    # The explicit list is preserved when switching back to workspace mode.
    agent_publish_mode: str = "workspace"  # "workspace" | "selected"
    agent_selection: list[str] = field(default_factory=list)
    sync_manifest_ref: str | None = None
    archived: bool = False
    schema_version: int = 1


@dataclass(slots=True)
class BoardColumn:
    """A value object living inside ``board.json`` (never its own file).

    Default columns use FIXED ids + deterministic content so two machines
    lazily creating the same default board converge on identical semantic
    content instead of conflicting on first realm sync. Behavior binds to
    ``kind`` (queued/active/review/done/custom), never to ``title``.
    """

    column_id: str
    title: str
    kind: str = "custom"
    wip_limit: int | None = None  # soft — surfaces a warning, never blocks


@dataclass(slots=True)
class BoardCard:
    """One planning card — one file each under ``boards/<board_id>/cards/``.

    A card is a PLANNING artifact only. ``created_by`` attribution is first-class
    so operator- and agent-authored cards render distinctly.
    """

    card_id: str
    board_id: str
    column_id: str
    title: str
    order_key: str
    description: str = ""
    priority: str = "p2"  # "p0".."p3"
    labels: list[str] = field(default_factory=list)
    assignee: str | None = None  # persona_id or "operator"
    checklist: list[dict[str, Any]] = field(default_factory=list)  # [{text, done}]
    state: str = "active"  # "active" | "archived"
    created_by: str = "operator"  # "operator" | persona_id
    revision: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None
    updated_by: str = "operator"
    schema_version: int = 1


@dataclass(slots=True)
class Board:
    """A workspace-scoped kanban board (def + ordered columns + card ledger).

    The default board id is deterministic (``board_default_<workspace_id>``) so
    two machines converge on it. ``archived_card_ids`` is the resurrection-guard
    ledger (ids only, bounded) that blocks a pulled remote copy from re-creating
    a locally archived card.
    """

    board_id: str
    workspace_id: str
    title: str
    columns: list[BoardColumn] = field(default_factory=list)
    archived_card_ids: list[str] = field(default_factory=list)
    revision: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None
    updated_by: str = "operator"
    schema_version: int = 1


@dataclass(slots=True)
class OfficeItem:
    """One authored Mission Office scene item (agent character or its desk) —
    a value object living inside its actor's file, never its own file.

    Geometry is scene-space ``[x, y]``; ``scale`` is the operator-authored
    render scale, clamped to the launcher's authorable range at the store
    boundary. ``display_name`` is operator text and is validated against the
    secret-assignment scanner at WRITE time (plan §4.2) so one member's name
    can never fail another member's realm publish.
    """

    item_id: str
    persona_id: str
    kind: str = "agent"  # "agent" | "desk"
    position: list[float] = field(default_factory=lambda: [0.0, 0.0])
    folder: str = ""
    display_name: str | None = None
    pet_slug: str | None = None
    scale: float = 1.0
    #: What this item was minted AS, stamped by the store at its first write and
    #: immutable thereafter (H-H5's sibling, H-H12). ``kind`` is mutable — any
    #: later upsert may re-spell it — so a reader asking "was this really an
    #: agent?" had nothing but the ``item_id`` STRING to consult, which is a
    #: launcher naming convention nothing enforces. This is the store's own
    #: record of the answer. ``None`` for every item written before it existed
    #: and for every one adopted from a peer that has not upgraded; the readers
    #: treat that as "cannot say", never as "no". Deliberately NOT on the wire
    #: (``office_item_wire_row``): no client decides anything with it — and, for
    #: the same reason, excluded from ``office_content_hash``
    #: (``_ITEM_HASH_EXCLUDE``): a field no observer can see must not be able to
    #: report an actor as changed.
    minted_kind: str | None = None


@dataclass(slots=True)
class OfficeActor:
    """One Mission Office actor placement — one file each under
    ``office/<workspace>/actors/`` (the realm-sync merge unit).

    ``actor_key`` is the canonical sync key minted ONLY by ``OfficeStore``
    (``canonical_persona_instance_id`` for instance-bound actors, else the
    persona id). The identity triple (persona/instance/profile) is the
    payload truth — the filename is routing only. All scene items bound to
    one actor (agent placements + coupled desks) live in this one file, so
    actor granularity — not item granularity — is the merge unit.
    """

    actor_key: str
    workspace_id: str
    persona_id: str
    persona_instance_id: str | None = None
    backing_profile: str | None = None
    items: list[OfficeItem] = field(default_factory=list)
    state: str = "active"  # "active" | "archived"
    revision: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None
    updated_by: str = "operator"
    schema_version: int = 1


@dataclass(slots=True)
class OfficeSurface:
    """The per-workspace Mission Office surface definition — shared taxonomy
    only (folders) + the resurrection-guard ledger. Personal view state
    (viewport, collapsed docks, hidden ids) never enters this model — it stays
    launcher-local by design (plan §4.4).
    """

    workspace_id: str
    folders: list[str] = field(default_factory=list)
    archived_actor_keys: list[str] = field(default_factory=list)
    revision: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None
    updated_by: str = "operator"
    schema_version: int = 1


@dataclass(slots=True)
class GoalRuntimeInstance:
    id: str
    task_id: str
    lane: str
    state: str
    created_at: datetime
    updated_at: datetime
    run_generation: int = 1
    active_run_ids: list[str] = field(default_factory=list)
    parked_reason: str | None = None
    lease_owner: str | None = None
    lane_kind: str = "production"
    priority: int = 5
    state_reason: str | None = None
    current_stage_id: str | None = None
    current_owner: str | None = None
    persona_instance_ids: list[str] = field(default_factory=list)
    repo_bundle_locks: list[dict[str, Any]] = field(default_factory=list)
    daemon_lease_id: str | None = None
    budget_counters: dict[str, int] = field(default_factory=dict)
    last_decision_type: str | None = None
    last_progress_at: datetime | None = None
    open_incident_ids: list[str] = field(default_factory=list)
    latest_proof_ids: list[str] = field(default_factory=list)
    schema_version: int = 1


@dataclass(slots=True)
class AgentPersona:
    id: str
    display_name: str
    role: str
    model: str | None
    provider: str | None
    api_mode: str | None
    toolsets: list[str]
    # Empty string and absent are the same thing — no dedicated prompt file
    # (resolve_persona_system_prompt_path returns None for both). Rows written
    # before 2026-08-29 may omit the key entirely.
    system_prompt_path: str = ""
    autonomy: str = "review"
    hermes_profile: str | None = None
    skills: list[str] = field(default_factory=list)
    soul_overlay_path: str | None = None
    required_mcp_servers: list[str] = field(default_factory=list)
    include_profile_memory: bool = False
    include_core_context_files: bool = False
    repo_scope: str | None = None
    repo_scope_label: str | None = None
    iteration_budget: int | None = None
    max_wall_seconds: float | None = None
    max_api_calls: int | None = None
    max_total_tokens: int | None = None
    readiness: dict[str, Any] = field(default_factory=dict)
    # issued_at of the last applied model-default write; stale writes are
    # superseded (same guard as PersonaInstance.model_override_issued_at).
    model_override_issued_at: datetime | None = None
    # issued_at of the last applied template SKILLS write (`harness persona
    # set-skills`). Its OWN clock, deliberately not shared with the model one:
    # the two verbs write disjoint fields, so a skills write must never
    # supersede — or be superseded by — a model write that happened to be
    # stamped later. Defaults to ``None`` so rows written before this field
    # existed load unchanged.
    skills_override_issued_at: datetime | None = None
    schema_version: int = 1


@dataclass(slots=True)
class AgentRun:
    id: str
    persona_id: str
    task_id: str
    stage_id: str | None
    state: RunState
    started_at: datetime
    last_heartbeat_at: datetime
    finished_at: datetime | None = None
    iteration_budget: int = 90
    max_wall_seconds: float | None = None
    max_api_calls: int | None = None
    max_total_tokens: int | None = None
    cost_usd: float = 0.0
    session_id: str | None = None
    final_decision: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    progress: dict[str, Any] | None = None
    schema_version: int = 1


# S56 deleted the ``WorkerSession`` dataclass with
# ``agent_runtime/worker_sessions.py``. Nothing constructs, reads or persists a
# worker row any more; ``WorkerSessionState`` survives because
# ``PersonaInstance.state`` is typed on it.


@dataclass(slots=True)
class PersonaInstance:
    id: str
    persona_id: str
    role: str
    display_name: str
    profile_id: str | None
    runtime_root: str
    state: WorkerSessionState
    mode: str = "configured"
    goal_id: str | None = None
    # Scope-provenance pointers: the Mission Control realm/workspace this
    # instance belongs to, stamped at placement creation from the operator
    # client's active scope (a deliberate placement is minted INSIDE one
    # workspace's scene). None = runtime-global — canonical seeded rows and
    # pre-pointer records. These are the instance's own "belongs to" claim;
    # read-side consumers resolve the ids against the live realm/workspace
    # stores and fall back to roster/goal joins, so a stale pointer degrades
    # honestly instead of inventing scope.
    realm_id: str | None = None
    workspace_id: str | None = None
    # Legacy scalar, now PROVENANCE: who caused this instance to exist. Two
    # live writers stamp a principal here — ``agent_create`` writes
    # ``"operator"`` and ``_maybe_stamp_spawned_by`` writes
    # ``coordinator_id or "operator"`` — so it is NOT a mirror of
    # ``steered_by[0]`` and no store keeps it in sync with steering. Steering
    # truth is ``steered_by``; read-side graph filters drop non-instance
    # tokens (``looks_like_persona_instance_id``), which is what keeps a
    # principal from ever rendering as a parent edge.
    spawned_by: str | None = None
    # Authoritative living-graph parent SET (Stage 77 multi-parent fan-in): the
    # persona-instance ids that steer this child. Empty = standalone owner.
    # Back-filled from ``spawned_by`` for legacy v1 records in ``__post_init__``.
    steered_by: list[str] = field(default_factory=list)
    returned_to: str | None = None
    current_chat_goal: str | None = None
    skill_overrides: list[str] | None = None
    # Instance-level model override tier: None = inherit the backing persona
    # live (cascade: chat-session override > instance > persona > cfg default).
    model: str | None = None
    provider: str | None = None
    api_mode: str | None = None
    # Per-instance reasoning-effort override (None = inherit the runtime default;
    # applies only for models that support reasoning effort). One of
    # hermes_constants.VALID_REASONING_EFFORTS or "none". Rides the same
    # model-override lane as model/provider/api_mode so a set-model write can
    # move all four together and use_profile_default clears them together.
    reasoning_effort: str | None = None
    # issued_at of the last applied model write; stale writes are superseded.
    model_override_issued_at: datetime | None = None
    current_assignment_id: str | None = None
    current_task_id: str | None = None
    active_run_id: str | None = None
    # Durable pointer to the operator-owned Mission Control chat root.  This is
    # deliberately independent from worker/run sessions: a task bind may come
    # and go without changing which operator conversation opens by default.
    default_chat_session_id: str | None = None
    # Legacy dual-purpose pointer.  Read only for v1 migration; new writers do
    # not use it for either chat or worker ownership.
    session_id: str | None = None
    # WHERE ``default_chat_session_id`` dereferences: the chat head home whose
    # ``state.db`` holds this instance's operator transcript, stamped by
    # ``PersonaInstanceStore.open_chat`` whenever the bind resolved an
    # AUTHORITATIVE chat scope — at creation, and re-affirmed on every turn
    # (the send path re-enters ``open_chat`` per turn). This is the
    # ``INSTANCE_RECORDED`` rung of
    # ``chat_session_scope.resolve_chat_session_scope``: the chat head is a
    # PER-CONVERSATION fact (different personas legitimately live in different
    # profile DBs), so no machine-level pointer can answer it alone.
    # ``None`` = UNRECORDED, deliberately distinct from a mismatch: a row that
    # predates the stamp, or one whose every bind so far ran under a degraded
    # ambient scope, falls through to the shared-root pointer exactly as
    # before. NOT projected onto the snapshot/patch wire (the wire rows are
    # explicit allowlists); persisted rows carrying it are safe under older
    # code because ``serde._coerce`` ignores unknown keys.
    chat_head_home: str | None = None
    # S70 (contract 54) removed ``context_receipt_id`` / ``compression_receipt_id``
    # / ``tool_budget_used`` / ``watchdog_warning_count`` from this record. Their
    # only writers died with the worker/goal lanes; ``ensure_for_personas`` was
    # left resetting values nothing could set. ``token_budget_used`` and
    # ``last_heartbeat_at`` are equally writer-less but STAY: both still have live
    # readers (the Launcher's token-total fallback and its roster-recency /
    # gateway-frame lanes; the orphan classifier's heartbeat hold), so retiring
    # them is a reader-side decision, not a wire cleanup. Dropping a field here is
    # safe against persisted rows: ``serde._coerce`` builds kwargs from the
    # dataclass fields and silently ignores unknown keys already on disk.
    skill_manifest_hash: str | None = None
    token_budget_used: int = 0
    last_heartbeat_at: datetime | None = None
    updated_at: datetime | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        # Back-compat: a legacy record (or any writer) that only set the scalar
        # ``spawned_by`` seeds the authoritative ``steered_by`` set, so every
        # reader sees a populated parent set. Idempotent — a writer that already
        # set ``steered_by`` (mirroring ``spawned_by`` = steered_by[0]) is a
        # no-op here. Kept out of ``upgrade()`` on purpose: schema_version stays
        # 1 (serde's shared upgrade hook hard-rejects any other version).
        #
        # Guarded on instance-shape: ``spawned_by`` doubles as a provenance
        # scalar and can legitimately hold a NON-instance principal (the
        # operator). Mirroring that into ``steered_by`` is exactly the defect
        # that made the HUD render "steered by operator" — a principal is not a
        # steering parent, so only an instance-shaped scalar seeds the set.
        if not self.steered_by and looks_like_persona_instance_id(self.spawned_by):
            self.steered_by = [self.spawned_by]
        if (
            not self.default_chat_session_id
            and isinstance(self.session_id, str)
            and self.session_id.startswith("persona_chat_")
        ):
            self.default_chat_session_id = self.session_id


def apply_instance_model_overrides(
    persona: AgentPersona, instance: PersonaInstance | None
) -> AgentPersona:
    """Overlay an instance's runtime overrides onto its backing persona.

    Pure: returns a copy, never mutates. ``None`` on the instance means inherit
    the persona value live. Both the chat lane and the run/tick lane must
    resolve model/provider/api_mode through this single overlay so two
    instances of one persona can run different models or assigned skill sets
    without drift between prompt observability and execution.
    """

    if instance is None:
        return persona
    instance_model = getattr(instance, "model", None)
    instance_provider = getattr(instance, "provider", None)
    instance_api_mode = getattr(instance, "api_mode", None)
    instance_skills = getattr(instance, "skill_overrides", None)
    if (
        instance_model is None
        and instance_provider is None
        and instance_api_mode is None
        and instance_skills is None
    ):
        return persona
    return replace(
        persona,
        model=instance_model if instance_model is not None else persona.model,
        provider=instance_provider if instance_provider is not None else persona.provider,
        api_mode=instance_api_mode if instance_api_mode is not None else persona.api_mode,
        skills=(
            list(instance_skills)
            if instance_skills is not None
            else list(persona.skills)
        ),
    )


@dataclass(slots=True)
class PersonaAssignment:
    id: str
    persona_instance_id: str
    persona_id: str
    kind: str
    state: str
    title: str
    message: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    task_id: str | None = None
    goal_id: str | None = None
    stage_id: str | None = None
    operation_id: str | None = None
    repo_bundle_id: str | None = None
    repo: str | None = None
    affected_paths: list[str] = field(default_factory=list)
    proof_targets: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    allowed_decisions: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    proof_ids: list[str] = field(default_factory=list)
    context_receipt_ids: list[str] = field(default_factory=list)
    evidence_kind: str = "task_bound"
    production_proof_eligible: bool = True
    archive_scope: str = "task"
    client_message_id: str | None = None
    last_error: str | None = None
    completed_at: datetime | None = None
    signal_hash: str | None = None
    schema_version: int = 1


# S57 (2026-08-01) removed ``RepoBundle`` (31 fields) with
# ``agent_runtime/repo_bundles.py``, the only module that ever constructed or
# decoded one. S52 had deleted every writer, S56 the four status projections that
# were the read side's only customer, and the checkpoint EntityClass row went
# with them; what was left was a typed row shape for a store no production code
# imported and no writer could fill. A model that only its own deleted store
# named is not a domain type, it is residue.


@dataclass(slots=True)
class Event:
    ts: datetime
    type: str
    task_id: str | None
    run_id: str | None
    persona_id: str | None
    payload: dict[str, Any] = field(default_factory=dict)
    # Session lineage for events that belong to a conversational (non-task)
    # persona chat turn. Task-run events leave this ``None`` and remain keyed on
    # ``task_id``; chat-turn tool/progress events set this so the snapshot trace
    # projection can surface them per chat session. Optional + trailing keeps the
    # JSONL envelope backward compatible: older event rows decode with ``None``.
    session_id: str | None = None
    # Canonical chat-turn identity: the turn key derived from the operator's
    # ``client_message_id``. This is THE reconciliation key clients use to match
    # a projected trace/conversation row to their locally streamed copy of the
    # same turn — it is minted once at the send boundary and never re-derived.
    # Task-run events leave this ``None`` (their identity is ``run_id``).
    turn_id: str | None = None


@dataclass(slots=True)
class Incident:
    id: str
    task_id: str | None
    run_id: str | None
    kind: str
    summary: str
    detail_path: str | None
    opened_at: datetime
    closed_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1
