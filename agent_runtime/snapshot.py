from __future__ import annotations

import copy
import json
import re
import threading
import time
import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone

# The snapshot roster does not render per-profile model/provider settings. Use
# the metadata-only catalog so a cold build does not parse every config.yaml.
# Keep the local alias stable for existing monkeypatch seams/tests.
from hermes_cli.profiles import available_profile_template_summaries as available_profile_templates
from hermes_time import now
from utils import atomic_json_write

from . import paths
from .board_store import BoardStore
from .office_store import OfficeStore
from .budget_approval import budget_incident_can_continue, budget_incident_needs_scope_recovery
from .config import ensure_persisted_personas, load_agent_runtime_config, load_root_runtime_config
from .decision_contract_registry import CONTRACT_SCHEMA_VERSION, contract_hash
from .delivery_directive import task_delivery_directive
from .dirty_state import build_dirty_state
from .events import CachedEventLog, EventLog, event_summary_missing, operator_event_summary
from .migrations import effective_config_summary, migration_status
from .mission_plan import mission_plan_summary, task_stage_records
from .models import looks_like_persona_instance_id
from .observability import build_observability
from .operator_channels import (
    OPERATOR_CHANNELS_SCHEMA_VERSION,
    OPERATOR_CONVERSATION_SCHEMA_VERSION,
    operator_channel_summary,
)
from .persona_assignments import (
    ACTIVE_ASSIGNMENT_STATES,
    PersonaAssignmentStore,
    PersonaInstanceStore,
    active_persona_instance_agent_summaries,
    persona_assignment_store_enabled,
    persona_assignment_summary,
    persona_instance_runtime_enabled,
    persona_instance_summary,
    persona_instance_visibility_ref,
)
from .persona_chat_history import DEFAULT_PERSONA_CHAT_MESSAGE_TAIL, _SECRET_RE, _canonical_persona_id, persona_chat_history_summary, persona_chat_trace_summary
from .redaction import TEXT_SECRET_ASSIGNMENT_RE
from .persona_instance_identity import (
    backed_persona_identity,
    classify_orphan_persona_instances,
    duplicate_persona_instance_groups,
    identity_aliases_for_rows,
)
from .parity import PARITY_ENVELOPE_VERSION, ProjectionAccountant, events_watermark
from .parse_cache import cached_by_mtime
from .resolution import resolution_payload, resolve_runtime, suspect_default_root
from .personas import blocked_tool_names, effective_toolsets, seed_personas
from .errors import LegacyOrchestratorRemoved
from .prompt_observability import _SkillObservabilityResolver, snapshot_prompt_observability
from .proof_gates import task_verdict_proof_satisfied
from .realm_sync import read_realm_sync_sidecar
from .repo_bundles import RepoBundleStore, bundle_queue_summary, qa_waiting_on, repo_bundle_delivery_summary, repo_bundle_summary, simplified_phase_for_task
from .runtime_instances import GoalRuntimeInstanceStore, runtime_instance_summary, runtime_instances_summary
from .repo_context import resolve_affected_repo_workdir
from .role_checklists import RoleChecklist, RoleChecklistStore, checklist_summary
from .role_envelopes import RoleEnvelope, RoleEnvelopeStore, role_envelope_summary
from .proof_batches import ProofBatch, ProofBatchStore, proof_batch_summary
from .scope_control import issue_discovery_counts, untriaged_issue_discoveries
from .simplified_contract import public_decision_type_value
from .state_machine import MissionStateMachine
from .serde import from_jsonable, to_jsonable
from .self_test_evidence import SelfTestEvidenceStore, self_test_summary
from .states import RunState, StageStatus, TaskState
from .steering import build_steer_actions
from .store import ACTIVE_RUN_STATES, AgentStore, IncidentStore, ProofStore, RealmStore, RunStore, TaskStore, WorkspaceStore
from .tool_visibility import (
    _profile_readiness_for_visibility,
    resolve_tool_visibility,
)
from .workspace_scope import exact_scoped_instance_ids
from .worker_sessions import WorkerSessionStore, worker_session_summary

AGENT_TOPOLOGY_NODE_ID_CAP = 20
STAGE_VERIFICATION_STAGE_CAP = 12
STAGE_VERIFICATION_PROOF_ID_CAP = 8
STAGE_VERIFICATION_PATH_CAP = 6
# Residue-slim R5(a): cap the in-head ``mission_flow_timeline.items`` at the FRONT
# window (the office renders ``items.take(5)``; keeping the front 8 preserves that
# render byte-for-byte with margin). The evicted tail — the newest coalesced
# progress events, the bulk of this projection's bytes — is accounted (count +
# ``harness goal history`` pointer) and fetched on demand, never silently dropped.
MISSION_FLOW_TIMELINE_ITEM_CAP = 8

# S2 read-model — history out of the live frame (operator move 6).
# ``archived_tasks`` (all dead), the closed/ancient tail of ``incidents``, and
# the persona-chat message tails are append-only HISTORY: read on demand at
# most, never advancing. They are evicted from the steady-state frame and served
# via paged on-demand queries over the EventLog / stores (``harness task
# history`` / ``goal history`` / ``incident list`` / ``persona chat history``).
# Archive-never-delete: eviction removes rows from the FRAME, never from disk —
# archived tasks stay in ``deleted_archive/``, closed incidents stay in
# ``incidents/``.
# Incident retention is OPEN-ONLY (operator decision 2026-07-16, supersedes the
# plan's closed-within-TTL recommendation): a closed incident is history the
# moment it closes and is served exclusively by the paged history query. Only
# OPEN incidents (live state) ship in the frame.
# Newest-N archived-task ids carried on the ``archived_tasks`` pointer stub so a
# consumer can name the most recent dead missions without shipping their rows.
#
# S7-B RULING-0 COMPAT STRIP (2026-07-16): the ``read_model.history_in_frame``
# kill-switch and its full-in-frame legacy branches were removed here — the
# evicted (pointer-stub) shape is the ONLY shape. Rollback = ``git revert``, not
# a flag flip. The helpers below always evict.
ARCHIVED_TASKS_REF_RECENT_CAP = 25


# S8 read-model — DEEP SLIM inside live rows (operator ruling 2026-07-17: "one
# file per goal + log; the UI parses it when relevant"). The goal row is a
# fusion of a compact HEAD (identity/state/current-stage/counts/delivery
# pointers + the mission-level fields the ALWAYS-VISIBLE surfaces render) and a
# heavy DETAIL body (per-role streams, envelopes, checklists, per-event
# timelines, the mission plan, the per-run persona streams). The detail body has
# no steady-state launcher reader: it was verified 2026-07-17 that
# ``MissionGoalSummary`` (the launcher's goal fold) never renders any of these
# fields off a goal — ``role_streams`` / ``stage_streams`` / ``timeline`` /
# ``mission_plan`` / ``persona_streams`` are not even parsed, and
# ``role_envelopes`` / ``role_checklists`` / ``proof_batches`` parse to unread
# fields (the top-level ``role_envelopes`` / ``role_checklists`` sections feed
# the roster; the goal-row copies are dead). They leave the head and are served
# on demand by ``harness goal detail <task_id> --json``. The KEPT mission-level
# fields (``mission_level_state`` → office/roster/page; ``mission_flow_timeline``
# / ``proof_gate_state`` / ``stage_verification`` → office scene) stay in the
# head because they render on every frame. Archive-never-delete: nothing leaves
# disk — the detail is rebuilt from the same stores + EventLog on demand.
GOAL_DETAIL_ONLY_FIELDS = frozenset(
    {
        "role_streams",
        "role_envelopes",
        "role_checklists",
        "stage_streams",
        "timeline",
        "mission_plan",
        "persona_streams",
        "proof_summaries",
        "self_test_summaries",
        "proof_batches",
        "verification_status",
        "operator_capabilities",
        "repo_bundles",
    }
)


def _goal_head(row: dict) -> dict:
    """Split a full goal projection into its compact frame HEAD.

    The heavy detail-only fields (``GOAL_DETAIL_ONLY_FIELDS``) leave the row and
    are replaced by a single typed ``detail_ref`` pointer carrying the fetch verb
    + which fields were evicted (never a silent absence — a consumer that opens a
    goal detail drawer/blueprint/replay view fetches them, an unfetched detail
    renders a loading/fetch affordance). The head still carries every field the
    always-visible surfaces read, including the mission-level state the office /
    roster / HUD render each frame."""

    head = {key: value for key, value in row.items() if key not in GOAL_DETAIL_ONLY_FIELDS}
    evicted = sorted(key for key in GOAL_DETAIL_ONLY_FIELDS if key in row)
    head["detail_ref"] = {
        "evicted": True,
        "task_id": row.get("task_id"),
        "fields": evicted,
        "fetch": "harness goal detail <task_id> --json",
    }
    return head


def _archived_tasks_frame(archived_tasks: list):
    """The ``archived_tasks`` frame value: a typed pointer stub.

    Replaces the 25-dead-row array (≈1.27 MB live) with a small honest marker
    carrying the count + newest-N ids + the fetch verb — never a silent absence.
    Full rows are fetched via ``harness task history <task_id> --json`` (which
    already reads archived batches).
    """

    recent_ids = [
        str(row.get("task_id"))
        for row in archived_tasks[:ARCHIVED_TASKS_REF_RECENT_CAP]
        if isinstance(row, dict) and row.get("task_id")
    ]
    return {
        "evicted": True,
        "count": len(archived_tasks),
        "recent_ids": recent_ids,
        "fetch": "harness task history <task_id> --json",
    }


def _open_incidents_frame(incidents: list) -> tuple[list, int]:
    """Split incidents into (in-frame open rows, closed-evicted count).

    Open incidents are live state and always in-frame. Closed incidents are
    history the moment they close (operator decision 2026-07-16: open-only, no
    TTL window) — evicted from the frame and served by the paged history query.
    """

    kept = [incident for incident in incidents if getattr(incident, "closed_at", None) is None]
    return kept, len(incidents) - len(kept)


def _persona_chat_history_frame(rows: list) -> list:
    """The ``persona_chat_history`` frame rows: recency pointers only.

    Keeps every recency pointer (session id + last-message anchors + counts +
    timestamps) but drops the heavy ``messages`` tail, flagging each row so a
    consumer distinguishes an evicted tail from a genuinely empty chat. The tail
    is fetched per session via
    ``harness persona chat history --session-id <id> --json``."""

    pointers: list = []
    for row in rows:
        if not isinstance(row, dict):
            pointers.append(row)
            continue
        pointer = {key: value for key, value in row.items() if key != "messages"}
        pointer["messages"] = []
        pointer["messages_evicted"] = True
        pointers.append(pointer)
    return pointers


# S4 read-model — normalize (operator moves 4 + 5). The on-disk stores are
# already file-per-entity keyed by id; the fuser used to de-key them into lists
# every consumer re-keyed. S4 exposes the keyed shape directly: list sections
# whose rows carry a canonical id become ``{id -> row}`` maps. GOAL is the wire
# entity name (operator decision 2026-07-16): the goals/tasks dual projection
# collapses to ONE keyed ``goals`` map and the ``tasks`` wire section retires.
# This is a NAMING/projection change only — the internal ``TaskStore`` machinery
# keeps its Task names (the 45E store rename stays deferred). Emitted
# unconditionally (no kill-switch): the rollback story is ``git revert`` of the
# landing, not a runtime legacy-shape flag.
def _keyed(rows, id_key: str) -> dict:
    """A list of id-carrying rows -> an id-keyed ``{id -> row}`` map.

    One owner per fact: the frame exposes the store's existing keyed shape
    instead of a list every consumer re-keys. First occurrence wins on a
    duplicate id (a duplicate is a parity concern surfaced elsewhere, never a
    silent overwrite); a row missing the id is dropped rather than silently
    keyed under ``""`` (no silent-drop-without-accounting — a missing canonical
    id is itself a bug the parity envelope's warnings catch).
    """

    keyed: dict = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = row.get(id_key)
        key = str(raw) if raw is not None else ""
        if not key or key in keyed:
            continue
        keyed[key] = row
    return keyed


def _rows(value) -> list:
    """Read a frame section that S4 emits as an id-keyed map as an ordered list
    of rows (map values), for the snapshot's OWN parity/self-check readers.

    The wire ships keyed maps; this is an internal convenience for the builder's
    downstream self-checks, not a legacy-shape tolerance path — it also accepts
    a plain list (sections not keyed by S4) and ``None`` (absent section)."""

    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, list):
        return list(value)
    return []


def snapshot_section_bytes(data: dict, key: str) -> int:
    """Compact-JSON byte size of one top-level snapshot section (S2 byte-budget
    goldens; deliberately independent of the parallel snapshot-audit module)."""

    if key not in data:
        return 0
    try:
        return len(
            json.dumps(
                to_jsonable(data.get(key)),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except Exception:
        return 0
# Single-homed in ``agent_runtime.redaction`` — see the header there for the
# JSON blind spot every local spelling shared. This is the same rule as
# ``persona_chat_history._SECRET_RE`` (also imported into this module); both now
# resolve to the one shared object. group(1) is still the key, so the
# ``f"{m.group(1)}: [redacted]"`` rebuild below is unchanged.
_ARCHIVED_CONVERSATION_SECRET_RE = TEXT_SECRET_ASSIGNMENT_RE


def _runtime_paths_diagnostic(available_personas: list) -> dict:
    """TEMP diagnostic: report the paths/env this process actually resolved,
    so the Launcher can see why available_personas came back empty."""
    import os as _os

    out: dict = {"available_personas_count": len(available_personas or [])}
    try:
        from hermes_constants import get_hermes_home, get_default_hermes_root

        out["env_HERMES_HOME"] = _os.environ.get("HERMES_HOME", "<unset>")
        out["env_HERMES_AGENT_RUNTIME_ROOT"] = _os.environ.get("HERMES_AGENT_RUNTIME_ROOT", "<unset>")
        out["env_LOCALAPPDATA"] = _os.environ.get("LOCALAPPDATA", "<unset>")
        out["resolved_hermes_home"] = str(get_hermes_home())
        out["resolved_default_root"] = str(get_default_hermes_root())
    except Exception as exc:  # pragma: no cover - diagnostic only
        out["error"] = repr(exc)
    try:
        from hermes_cli.profiles import _get_profiles_root

        root = _get_profiles_root()
        out["profiles_root"] = str(root)
        out["profiles_root_exists"] = root.is_dir()
        out["profiles_root_entries"] = sorted(p.name for p in root.iterdir()) if root.is_dir() else []
    except Exception as exc:  # pragma: no cover - diagnostic only
        out["profiles_error"] = repr(exc)
    return out


# Concurrent core builds are strictly additive under the GIL — measured
# 2026-07-09 on the live store: one warm build 3.3s, three concurrent builds
# 8.8s EACH (the launcher's "snapshot build 9050ms" chip). Mission Control
# boot fires hydrate + status polls together, so without coalescing every
# boot request pays the whole storm. Builds on the default-store path are
# therefore serialized and coalesced: a caller arriving while a build runs
# waits and shares the NEXT build (never the in-flight one — its state may
# predate the caller's arrival), so N concurrent requests cost at most two
# sequential fast builds. Sharing copies via copy.deepcopy, NOT a JSON
# round-trip: the core carries datetime objects, and a json.dumps here
# raised TypeError and silently disabled sharing on the live store (found
# 2026-07-09 — the unit fakes were JSON-safe). The builder deep-copies once
# into the share slot and every waiter deep-copies out, so no two callers
# ever alias one snapshot dict.
_BUILD_COALESCE = threading.Condition()
_build_coalesce_state: dict = {
    "running": False,
    "started": 0,
    "done": 0,
    "result": None,
    "waiters": 0,
}


@contextmanager
def _timed_section(sink: dict[str, int], key: str):
    """Accumulate wall time (ms) for a build section into ``sink[key]``.

    Additive/observability only — powers the parity envelope's ``sections_ms``
    next to ``build_ms``. Accumulates (``+=``) so a section timed across more than
    one span sums rather than overwrites. Keys are stable and lowercase.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        sink[key] = sink.get(key, 0) + int(max(0.0, (time.perf_counter() - start)) * 1000)


def build_snapshot(
    task_store=None,
    run_store=None,
    agent_store=None,
    proof_store=None,
    incident_store=None,
    event_log=None,
    worker_session_store=None,
    prompt_skills_catalogs=None,
) -> dict:
    custom_stores = any(
        value is not None
        for value in (
            task_store,
            run_store,
            agent_store,
            proof_store,
            incident_store,
            event_log,
            worker_session_store,
        )
    )
    if custom_stores or prompt_skills_catalogs is not None:
        # Injected stores (tests, doctors) must observe exactly their own
        # fixtures — never a coalesced result built from the default stores.
        # A detail-fetch catalog capture likewise needs the exact build's
        # transient bodies; the shared result intentionally contains hashes
        # only, so it cannot satisfy that internal projection request.
        return _build_snapshot_uncoalesced(
            task_store=task_store,
            run_store=run_store,
            agent_store=agent_store,
            proof_store=proof_store,
            incident_store=incident_store,
            event_log=event_log,
            worker_session_store=worker_session_store,
            prompt_skills_catalogs=prompt_skills_catalogs,
        )
    state = _build_coalesce_state
    with _BUILD_COALESCE:
        # The first build STARTED at/after arrival is the one that satisfies
        # this caller; an in-flight build began earlier and may miss writes
        # this caller has already observed.
        target = state["started"] + 1
        while True:
            if state["done"] >= target and state["result"] is not None:
                payload = copy.deepcopy(state["result"])
                if state["waiters"] == 0:
                    state["result"] = None
                return payload
            if not state["running"]:
                state["running"] = True
                state["started"] += 1
                generation = state["started"]
                break
            state["waiters"] += 1
            _BUILD_COALESCE.wait()
            state["waiters"] -= 1
    result = None
    try:
        result = _build_snapshot_uncoalesced()
        return result
    finally:
        with _BUILD_COALESCE:
            state["running"] = False
            if result is not None:
                state["done"] = generation
                state["result"] = None
                if state["waiters"]:
                    try:
                        state["result"] = copy.deepcopy(result)
                    except Exception:
                        # Uncopyable core: waiters fall back to building
                        # their own — never raise from finally (it would
                        # replace the builder's own return value).
                        state["result"] = None
            _BUILD_COALESCE.notify_all()


def _build_snapshot_uncoalesced(
    task_store=None,
    run_store=None,
    agent_store=None,
    proof_store=None,
    incident_store=None,
    event_log=None,
    worker_session_store=None,
    prompt_skills_catalogs=None,
) -> dict:
    _build_started = time.perf_counter()
    _sections_ms: dict[str, int] = {}
    task_store = task_store or TaskStore()
    run_store = run_store or RunStore()
    agent_store = agent_store or AgentStore()
    proof_store = proof_store or ProofStore()
    incident_store = incident_store or IncidentStore()
    # A snapshot calls for_task/for_session/tail dozens of times on the same log;
    # CachedEventLog reads events.jsonl once and serves all of them from memory.
    event_log = event_log or CachedEventLog()
    # Force + time the one-shot CachedEventLog materialization here (the ~1s
    # event-log read, measured 2026-07-23) before other consumers warm it, so
    # ``sections_ms.events`` honestly attributes that cost. ``recent_events`` is
    # a pure read reused later (build_observability / parity watermark).
    with _timed_section(_sections_ms, "events"):
        recent_events = event_log.tail(20)
    worker_session_store = worker_session_store or WorkerSessionStore(event_log=event_log)
    tasks = task_store.list_all()
    runs = run_store.list_all()
    workers = worker_session_store.list_all()
    cfg = load_agent_runtime_config()
    # S2 read-model: history is always evicted from the steady-state frame and
    # served via paged on-demand queries (S7-B RULING-0: no legacy full-in-frame
    # shape; the pointer-stub shape is the only shape).
    # The background Mission Daemon was retired; execution is always operator/
    # goal-runner driven ("manual").
    execution_mode = "manual"
    # Base-profile foundation: Mission Control shows the seeded store (base only). On a
    # cold store, fall back to the base seed itself — NOT ensure_persisted_personas, which
    # also returns the dormant typed catalog for resolution and would surface mothballed
    # pipeline personas that are not meant to be shown.
    agents = agent_store.list_all() or seed_personas()
    # Closed incidents are history-only in the steady-state frame. Avoid
    # recursively coercing the entire closed tail (thousands of files on a
    # mature runtime) merely to count it; the store still validates each JSON
    # row and materializes every open incident used by routing/observability.
    incidents, incidents_evicted_count = incident_store.list_open_with_closed_count()
    role_envelope_store = RoleEnvelopeStore(event_log=event_log)
    role_checklist_store = RoleChecklistStore(event_log=event_log)
    proof_batch_store = ProofBatchStore(event_log=event_log)
    repo_bundle_store = RepoBundleStore(event_log=event_log)
    runtime_instance_store = GoalRuntimeInstanceStore(event_log=event_log)
    role_envelopes = role_envelope_store.list_all()
    role_checklists = role_checklist_store.list_all()
    proof_batches = proof_batch_store.list_all()
    repo_bundles = repo_bundle_store.list_all()
    runtime_instances = runtime_instance_store.list_all()
    archived_tasks = _archived_task_summaries()
    workspace_store = WorkspaceStore()
    realm_store = RealmStore()
    workspaces = workspace_store.list_all(include_archived=True)
    realms = realm_store.list_all(include_archived=True)
    # Active scope names for the runtime situational HUD (the same realm/ws the
    # launcher scope line renders); resolved once and fed to every lane's HUD.
    active_workspace_name = next(
        (getattr(w, "name", None) for w in workspaces if getattr(w, "id", None) == workspace_store.active_id()),
        None,
    )
    active_realm_name = next(
        (getattr(r, "name", None) for r in realms if getattr(r, "id", None) == realm_store.active_id()),
        None,
    )
    skill_resolver = _SkillObservabilityResolver()
    with _timed_section(_sections_ms, "agents_readiness"):
        from .profile_readiness import profile_readiness_for_persona

        readiness_by_persona_id = {
            str(getattr(agent, "id", "") or ""): profile_readiness_for_persona(
                agent, skill_resolver=skill_resolver
            )
            for agent in agents
        }
        agent_summaries = [
            _agent_summary(
                agent,
                include_tool_details=True,
                readiness=readiness_by_persona_id.get(
                    str(getattr(agent, "id", "") or "")
                ),
            )
            for agent in agents
        ]
    available_personas = _available_persona_summary(agents)
    proofs = []
    self_tests = []
    for task in tasks:
        proofs.extend(proof_store.list_for_task(task.id))
        self_tests.extend(SelfTestEvidenceStore(event_log=event_log).list_for_task(task.id))
    active_runs = [run for run in runs if run.state in ACTIVE_RUN_STATES]
    active_workers = worker_session_store.find_active()
    running_runs = [run for run in active_runs if run.state == RunState.RUNNING]
    queued_runs = [run for run in active_runs if run.state == RunState.QUEUED]
    waiting_runs = [run for run in active_runs if run.state in {RunState.WAITING_ON_TOOL, RunState.WAITING_ON_APPROVAL}]
    dirty_state = build_dirty_state(tasks=tasks, runs=runs, incidents=incidents, workers=workers, runtime_instances=runtime_instances)
    persona_assignments = []
    persona_instances = []
    topology_persona_instances = PersonaInstanceStore(event_log=event_log).list_all()
    personas_by_id = {str(getattr(agent, "id", "") or ""): agent for agent in agents}
    stage_verification_accountant = ProjectionAccountant("stage_verification")
    flow_timeline_accountant = ProjectionAccountant("mission_flow_timeline")
    if persona_instance_runtime_enabled(cfg):
        instance_store = PersonaInstanceStore(event_log=event_log)
        persona_instances = instance_store.derive_from_workers(agents, workers)
        topology_persona_instances = persona_instances
        agent_summaries = [
            *agent_summaries,
            *active_persona_instance_agent_summaries(
                persona_instances, personas_by_id, readiness_by_persona_id
            ),
        ]
        if persona_assignment_store_enabled(cfg):
            persona_assignments = PersonaAssignmentStore(event_log=event_log).list_all()
    session_db = _default_persona_session_db()
    # S2 read-model: keep only OPEN incidents (live state) in-frame; every
    # closed incident is history, evicted to the paged query. The full
    # ``incidents`` list still feeds summary/observability/dirty/tasks — only the
    # ``incidents`` frame key is filtered to open.
    incident_frame_rows = incidents
    # S4: the frame carries these as id-keyed maps (``_keyed`` below). The row
    # LISTS are still needed as an ordered input to the operator-channel
    # projection (``run_summaries`` feeds the goal-turn flow), so build them once
    # here and key them into the frame.
    # ``run_rows`` (ALL runs) still feeds the operator-channel goal-turn flow
    # below; the FRAME ``runs`` map (S8) keeps only ACTIVE runs (attached to a
    # live lane/goal). Historical/terminal runs are old residue — evicted to a
    # count + pointer, fetched on demand via ``harness run list``. No disk
    # change: the run store keeps every row.
    run_rows = [_run_summary(r) for r in runs]
    active_run_id_set = {r.id for r in active_runs}
    frame_run_rows = [row for row in run_rows if row.get("run_id") in active_run_id_set]
    incident_rows = [_incident_summary(i) for i in incident_frame_rows]
    migration = migration_status()
    # Hoisted out of the ``data`` literal so their cost is attributable in
    # ``sections_ms`` (both were profiled hot: prompt_observability ~5s, the
    # skills-catalog walks inside it; boards/offices are local-only reads).
    with _timed_section(_sections_ms, "prompt_observability"):
        prompt_observability_section = snapshot_prompt_observability(
            personas=agents,
            persona_instances=persona_instances,
            session_db=session_db,
            tasks=tasks,
            proof_store=proof_store,
            daemon=None,
            realm=active_realm_name,
            workspace=active_workspace_name,
            active_workspace_id=workspace_store.active_id(),
            catalog_sink=prompt_skills_catalogs,
            skill_resolver=skill_resolver,
        )
    with _timed_section(_sections_ms, "boards_offices"):
        boards_section = _keyed(
            _boards_summary(BoardStore(event_log=event_log), workspaces),
            "board_id",
        )
        offices_section = _keyed(
            _offices_summary(OfficeStore(event_log=event_log), workspaces),
            "workspace_id",
        )
    data = {
        "schema_version": 2,
        "decision_contract_version": CONTRACT_SCHEMA_VERSION,
        "decision_contract_hash": contract_hash(),
        "event_contract_version": CONTRACT_SCHEMA_VERSION,
        "generated_at": now(),
        "summary": {
            "open_tasks": len([t for t in tasks if t.state not in {TaskState.DONE, TaskState.CANCELLED}]),
            "active_runs": len(active_runs),
            "running_runs": len(running_runs),
            "queued_runs": len(queued_runs),
            "waiting_runs": len(waiting_runs),
            "active_worker_sessions": len(active_workers),
            "blocked_tasks": len([t for t in tasks if t.state == TaskState.BLOCKED]),
            "open_incidents": len([i for i in incidents if i.closed_at is None]),
            "dirty": dirty_state["dirty"],
            "dirty_summary": dirty_state["summary"],
        },
        "dirty_state": dirty_state,
        "foreground_runtime": runtime_instances_summary(runtime_instances),
        "execution_mode": execution_mode,
        # Single runtime-default authority, resolved + provenance-stamped, as a
        # typed top-level block so surfaces (launcher model-switcher caption,
        # `hermes harness config show`) report what agents actually follow
        # without re-deriving the top-level-vs-agent_runtime precedence.
        "runtime_default": {
            "model": _safe_model_label(cfg.default_model),
            "provider": _safe_model_label(cfg.default_provider),
            "api_mode": cfg.default_api_mode,
            "model_source": getattr(cfg, "default_model_source", "unset"),
            "provider_source": getattr(cfg, "default_provider_source", "unset"),
        },
        "runtime_config": effective_config_summary(cfg, migration=migration),
        "migration": migration,
        "prompt_observability": prompt_observability_section,
        "observability": build_observability(tasks=tasks, runs=runs, incidents=incidents, proofs=proofs, daemon_status=None, events=recent_events, execution_mode=execution_mode, worker_sessions=workers),
        "repo_scopes": _repo_scopes_summary(),
        # S4: ONE keyed ``goals`` map is the wire entity (operator decision:
        # GOAL is the wire name). The old ``tasks`` wire section retires; every
        # field BOTH projections carried lives once here — the merged goal row
        # is the full ``_task_summary`` (self_tests / role_envelopes /
        # role_checklists / proof_batches / stage_verification all threaded, as
        # the old ``tasks`` section had them) PLUS the goal-only fields (``id``,
        # ``kind``, resolved ``realm_id``). Keyed by ``task_id`` — the unique
        # per-row id, matching the on-disk ``task_*.json`` store keying. NOT
        # ``goal_id``/``id``: several tasks can share one goal_id (parent/child
        # under one mission), which would collapse rows. Each row still carries
        # ``id`` (goal identity) + ``goal_id`` for goal-addressed consumers; the
        # launcher folds the map's values, so the map key is never a lookup key.
        "goals": _keyed(
            [
                _goal_head(_goal_projection_from_task(
                    t,
                    proof_store.list_for_task(t.id),
                    tasks,
                    incidents,
                    runs,
                    event_log.for_task(t.id, limit=200),
                    workers=workers,
                    run_store=run_store,
                    self_tests=SelfTestEvidenceStore(event_log=event_log).list_for_task(t.id),
                    role_envelopes=[item for item in role_envelopes if item.task_id == t.id],
                    role_checklists=[item for item in role_checklists if item.task_id == t.id],
                    proof_batches=[item for item in proof_batches if item.task_id == t.id],
                    persona_assignments=[item for item in persona_assignments if item.task_id == t.id],
                    repo_bundles=[item for item in repo_bundles if item.task_id == t.id],
                    runtime_instances=[item for item in runtime_instances if item.task_id == t.id],
                    persona_instances=topology_persona_instances,
                    stage_verification_accountant=stage_verification_accountant,
                    flow_timeline_accountant=flow_timeline_accountant,
                    workspaces=workspaces,
                    event_log=event_log,
                ))
                for t in tasks
            ],
            "task_id",
        ),
        # Rows carry a resolved ``active`` flag alongside the top-level
        # ``active_*_id`` keys — consumers (launcher scope switcher) key
        # selection off the row flag and must not re-derive it.
        "workspaces": [
            _workspace_summary(
                item,
                tasks=tasks,
                persona_instances=topology_persona_instances,
                active_id=workspace_store.active_id(),
            )
            for item in workspaces
        ],
        "realms": [
            _realm_summary(item, workspaces=workspaces, active_id=realm_store.active_id())
            for item in realms
        ],
        # Mission Board projection: board defs + bounded, redaction-safe card
        # rows, scoped by workspace. Local reads only — NO git/sync calls in the
        # snapshot path (conflict state comes from local sidecar files, never a
        # git call). Cards carry planning state only.
        "boards": boards_section,
        # Mission Office projection: surface defs + bounded actor rows, keyed by
        # workspace. Local reads only — conflict state comes from local sidecar
        # files and the `unpublished` honesty flag from the local baseline
        # sidecar; NEVER a git call in the snapshot path.
        "offices": offices_section,
        "active_workspace_id": workspace_store.active_id(),
        "active_realm_id": realm_store.active_id(),
        "warnings": _snapshot_warnings(persona_assignments),
        "archived_tasks": _archived_tasks_frame(archived_tasks),
        "agents": agent_summaries,
        "available_personas": available_personas,
        "runtime_paths_diagnostic": _runtime_paths_diagnostic(available_personas),
        "worker_sessions": [worker_session_summary(worker) for worker in workers],
        "role_envelopes": [role_envelope_summary(item, checklist_store=role_checklist_store) for item in role_envelopes],
        "role_checklists": [checklist_summary(item) for item in role_checklists],
        "proof_batches": [proof_batch_summary(item) for item in proof_batches],
        "repo_bundles": [repo_bundle_summary(item) for item in repo_bundles],
        "bundle_queue": bundle_queue_summary(repo_bundles),
        "runtime_instances": [runtime_instance_summary(item) for item in runtime_instances],
        "runs": _keyed(frame_run_rows, "run_id"),
        "incidents": _keyed(incident_rows, "incident_id"),
        "proofs": [_proof_summary(p) for p in proofs],
        "self_tests": [self_test_summary(item) for item in self_tests],
    }
    # Honest accounting for the closed incidents evicted from the ``incidents``
    # section — a typed pointer, never a silent absence. Only OPEN incidents
    # remain in ``incidents`` as live state; every closed one is history-only
    # (open-only retention, operator decision 2026-07-16).
    data["incidents_history_ref"] = {
        "evicted": True,
        "closed_evicted": True,
        "count": incidents_evicted_count,
        "fetch": "harness incident list --state closed --json",
    }
    # S8: historical (non-active) runs are evicted from the ``runs`` frame map —
    # a typed pointer accounts them, never a silent absence. Served on demand by
    # the paged ``harness run list`` query.
    data["runs_history_ref"] = {
        "evicted": True,
        "count": len(run_rows) - len(frame_run_rows),
        "active_count": len(frame_run_rows),
        "total_count": len(run_rows),
        "fetch": "harness run list --json",
    }
    if persona_instance_runtime_enabled(cfg):
        data["persona_instance_runtime"] = {
            "enabled": True,
            "assignment_store_enabled": persona_assignment_store_enabled(cfg),
        }
        # S4: persona_instances ships as an id-keyed map (the identity substrate
        # the whole roster keys on — the store is already keyed on disk). The
        # ROW LIST is built first because the identity_map alias resolver derives
        # from the ordered rows; the frame then keys it by ``persona_instance_id``.
        persona_instance_rows = [
            persona_instance_summary(
                instance,
                personas_by_id.get(str(getattr(instance, "persona_id", "") or "")),
                profile_readiness=readiness_by_persona_id.get(
                    str(getattr(instance, "persona_id", "") or "")
                ),
            )
            for instance in persona_instances
        ]
        # Legacy persona-instance id -> canonical id aliases (durable
        # reconciler registry + structurally derivable drift still live in
        # this snapshot). Consumers key dedup on this instead of heuristics.
        data["identity_map"] = identity_aliases_for_rows(persona_instance_rows)
        data["persona_instances"] = _keyed(persona_instance_rows, "persona_instance_id")
        history_accountant = ProjectionAccountant("persona_chat_history")
        trace_accountant = ProjectionAccountant("persona_chat_trace")
        # Full history (with message tails) is computed once and used to build the
        # operator_channels conversations (their tail slimming is S4's concern).
        # The FRAME carries recency pointers only (S2) — the tail bytes leave, the
        # anchors stay.
        _persona_chat_started = time.perf_counter()
        omitted_history_session_ids: set[str] = set()
        persona_chat_history_full = persona_chat_history_summary(
            persona_instances=persona_instances,
            session_db=session_db,
            message_tail=DEFAULT_PERSONA_CHAT_MESSAGE_TAIL,
            accountant=history_accountant,
            persona_assignments=persona_assignments,
            omitted_session_ids=omitted_history_session_ids,
        )
        data["persona_chat_history"] = _persona_chat_history_frame(persona_chat_history_full)
        data["persona_chat_trace"] = persona_chat_trace_summary(
            persona_instances=persona_instances,
            event_log=event_log,
            message_tail=DEFAULT_PERSONA_CHAT_MESSAGE_TAIL,
            accountant=trace_accountant,
        )
        conversation_accountant = ProjectionAccountant("operator_conversation")
        live_operator_channels = operator_channel_summary(
            persona_instances=persona_instances,
            persona_chat_history=persona_chat_history_full,
            persona_chat_trace=data["persona_chat_trace"],
            tasks=tasks,
            run_summaries=run_rows,
            accountant=conversation_accountant,
            intentionally_omitted_history_session_ids=omitted_history_session_ids,
        )
        _sections_ms["persona_chat"] = int(
            max(0.0, (time.perf_counter() - _persona_chat_started)) * 1000
        )
        live_channel_task_ids = {
            str(channel.get("task_id") or "")
            for channel in live_operator_channels
            if channel.get("task_id")
        }
        # S4: operator_channels ships as an id-keyed map (channel_id). Live
        # channels first, then the archived-task channels that don't collide —
        # ``_keyed`` keeps the first occurrence, so a live channel is never
        # shadowed by an archived one sharing an id.
        data["operator_channels"] = _keyed(
            [
                *live_operator_channels,
                *_archived_operator_channels(
                    archived_tasks,
                    live_task_ids=live_channel_task_ids,
                ),
            ],
            "channel_id",
        )
        # S8: the ``recent`` lane (last 50 assignments, ~86 KB of old residue)
        # leaves the frame — the launcher roster only needs the ACTIVE
        # assignments (a live instance's current assignment is always active).
        # ``recent`` stays an (empty) list so the launcher fold that concatenates
        # ``active`` + ``recent`` never trips; the eviction is accounted by
        # ``recent_ref`` and served on demand by ``harness persona assignments``.
        active_assignments = [
            persona_assignment_summary(item)
            for item in persona_assignments
            if item.state in ACTIVE_ASSIGNMENT_STATES
        ]
        recent_count = len(persona_assignments[-50:])
        data["persona_assignments"] = {
            "active": active_assignments,
            "recent": [],
            "recent_ref": {
                "evicted": True,
                "count": recent_count,
                "active_count": len(active_assignments),
                "fetch": "harness persona assignments --json",
            },
        }
        completeness = {
            "persona_chat_history": history_accountant.summary(),
            "persona_chat_trace": trace_accountant.summary(),
            "operator_conversation": conversation_accountant.summary(),
        }
        drop_samples = (
            history_accountant.drop_samples()
            + trace_accountant.drop_samples()
            + conversation_accountant.drop_samples()
        )
    else:
        data["persona_instance_runtime"] = {"enabled": False}
        completeness = {}
        drop_samples = []
    completeness["stage_verification"] = stage_verification_accountant.summary()
    drop_samples.extend(stage_verification_accountant.drop_samples())
    completeness["mission_flow_timeline"] = flow_timeline_accountant.summary()
    drop_samples.extend(flow_timeline_accountant.drop_samples())
    # Guarantee the documented section keys exist even when a lane was skipped
    # (e.g. persona_chat when persona-instance runtime is disabled) so consumers
    # can rely on a stable shape.
    for _section_key in (
        "agents_readiness",
        "prompt_observability",
        "events",
        "persona_chat",
        "boards_offices",
    ):
        _sections_ms.setdefault(_section_key, 0)
    _parity_started = time.perf_counter()
    data["parity"] = _parity_envelope(
        data,
        build_started=_build_started,
        last_event=recent_events[-1] if recent_events else None,
        recent_events=recent_events,
        completeness=completeness,
        drop_samples=drop_samples,
        sections_ms=_sections_ms,
    )
    # ``_sections_ms`` is stored by reference in the envelope, so recording the
    # parity section's own duration after the call is reflected in the frame.
    _sections_ms["parity"] = int(max(0.0, (time.perf_counter() - _parity_started)) * 1000)
    return data


def _parity_envelope(data, *, build_started, last_event, completeness, drop_samples, recent_events=None, sections_ms=None):
    """The S0 observability envelope: provenance + completeness + parity warnings.

    Additive and self-describing — turns the snapshot's silent drops into reported
    data and dates the snapshot against the event log so a reader knows how far
    behind it is. See the snapshot-architecture brain note.
    """

    last_ts = getattr(last_event, "ts", None) if last_event is not None else None
    watermark = events_watermark(last_event_ts=last_ts)
    resolution = resolve_runtime()
    cfg = load_agent_runtime_config()
    warnings = _parity_warnings(data)
    warnings.extend(_event_summary_warnings(recent_events or []))
    if suspect_default_root(resolution):
        warnings.append(
            {
                "code": "suspect_default_root",
                "detail": "runtime root resolved through the default layer, but no tasks/ directory exists; check runtime-root pins",
            }
        )
    return {
        "envelope_version": PARITY_ENVELOPE_VERSION,
        # S8 (DEEP SLIM inside live rows): the same history-eviction knife one
        # level deeper. Goal rows become compact HEADS (heavy detail →
        # ``harness goal detail``); the ``skills_catalogs`` table leaves the frame
        # (rows keep ``*_ref`` hashes → ``harness skills catalog --hash``); the
        # ``runs`` map keeps only ACTIVE runs (history → ``harness run list``);
        # ``persona_assignments.recent`` and stale ``chat_contexts`` rows are
        # evicted to pointers; archived operator channels become pointer stubs
        # (transcript → ``harness task history``). Every eviction is accounted
        # (typed ``*_ref`` / ``detail_ref`` / ``evicted`` markers), never a silent
        # absence. S2/S3/S4 shape unchanged. Launcher pin
        # (kSupportedMissionContractVersion) moves in lockstep.
        #
        # 44 (snapshot residue-slim R1/R2/R5a, 2026-07-17; 43 was taken by the
        # office-realm-sync landing): the dead ``capabilities`` /
        # ``observability.capabilities`` / ``event_contracts`` / ``blueprints`` /
        # ``blueprint_runs`` frame sections (zero readers in all three repos) are
        # DELETED; ``persona_instances`` / ``agents`` rows evict the heavy
        # tool-detail payloads behind a typed ``visibility_ref`` (fetched via
        # ``harness persona-instance detail``) and ``agent_hud_state`` is RETIRED;
        # ``goals[].mission_flow_timeline.items`` is capped to the front window
        # (accounted, ``harness goal history`` pointer).
        "contract_version": 44,
        "generated_at": data.get("generated_at"),
        "redaction_mode": getattr(cfg, "redaction_mode", "strict"),
        "redaction_observed": _redaction_observed(data),
        "build_ms": int(max(0.0, (time.perf_counter() - build_started)) * 1000),
        # Additive per-section wall-time breakdown (ms) alongside build_ms — a
        # small, stable, lowercase-keyed dict so a reader can see where a slow
        # build spent its time. Held BY REFERENCE so the caller can record the
        # parity section's own duration after this envelope is assembled.
        "sections_ms": sections_ms if sections_ms is not None else {},
        "snapshot_bytes": _snapshot_payload_size(data),
        "event_log_bytes": int(watermark.get("event_offset") or 0),
        "projection_age_ms": _projection_age_ms(last_ts),
        "watermark": watermark,
        "runtime_root": _runtime_root_identity(),
        "resolution": resolution_payload(resolution),
        "profile": _runtime_profile_identity(),
        "capabilities": [
            "goal_create",
            "mission_level_state",
            "agent_topology",
            "mission_flow_timeline",
            "proof_gate_state",
            "stage_verification",
            "operator_capabilities",
            "server_minted_chat_sessions",
        ],
        "freshness": {
            "state": "fresh",
            "stale_after_seconds": 30,
            "generated_at": data.get("generated_at"),
        },
        "completeness": completeness,
        "drops": drop_samples,
        "warnings": warnings,
    }


def _snapshot_payload_size(data) -> int:
    try:
        return len(json.dumps(to_jsonable(data), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    except Exception:
        return 0


def _projection_age_ms(last_event_ts) -> int | None:
    if last_event_ts is None:
        return None
    try:
        if isinstance(last_event_ts, datetime):
            ts = last_event_ts
        else:
            text = str(last_event_ts)
            ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
        current = now()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=current.tzinfo)
        return max(0, int((current - ts.astimezone(current.tzinfo)).total_seconds() * 1000))
    except Exception:
        return None


def _runtime_root_identity() -> dict:
    text = str(paths.store_root()).replace("\\", "/")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return {
        "fingerprint": digest,
        "label": _safe_repo_scope_label(text),
    }


def _runtime_profile_identity() -> dict:
    try:
        from .profile_context import active_profile_name

        name = active_profile_name()
    except Exception:
        name = None
    safe = _safe_model_label(str(name)) if name else None
    return {"name": safe or "default"}


# Mission Board projection bounds — honest accounting, never a silent cap
# (the 8.6MB-snapshot lesson): oversized boards report the remainder count and
# card bodies truncate with a flag rather than growing the snapshot unbounded.
MAX_BOARD_CARDS_PROJECTED = 500
BOARD_CARD_DESC_LIMIT = 2048


def _mask_board_secrets(text) -> str:
    """Hard-on secret mask for card prose in the projection (observe mode never
    disables it). Card text rides the same projection boundary as goal text; the
    HARD gate is the realm-publish fail-closed scan in ``realm_sync``."""

    if not text:
        return "" if text is None else text
    return _ARCHIVED_CONVERSATION_SECRET_RE.sub(lambda m: f"{m.group(1)}: [redacted]", str(text))


def _board_card_row(card, *, unpublished: bool | None = None) -> dict:
    description = card.description or ""
    truncated = len(description) > BOARD_CARD_DESC_LIMIT
    if truncated:
        description = description[:BOARD_CARD_DESC_LIMIT]
    row = {
        "card_id": card.card_id,
        "board_id": card.board_id,
        "column_id": card.column_id,
        "title": _mask_board_secrets(card.title),
        "description": _mask_board_secrets(description),
        "description_truncated": truncated,
        "priority": card.priority,
        "labels": list(card.labels),
        "assignee": card.assignee,
        "checklist": [
            {"text": _mask_board_secrets(str(item.get("text", ""))), "done": bool(item.get("done"))}
            for item in card.checklist
            if isinstance(item, dict)
        ],
        "order_key": card.order_key,
        "state": card.state,
        "created_by": card.created_by,
        "updated_at": to_jsonable(card.updated_at),
        "updated_by": card.updated_by,
        "revision": card.revision,
    }
    # Publication honesty is only meaningful for realm-bound boards; omit the
    # flag entirely otherwise (mirrors the office ``unpublished=None`` posture).
    if unpublished is not None:
        row["unpublished"] = unpublished
    return row


def _board_conflict_card_ids(board_id: str) -> list[str]:
    """Card ids with an OPEN conflict sidecar. Local file reads only — no git."""

    conflicts_dir = paths.board_conflicts_dir(board_id)
    if not conflicts_dir.exists():
        return []
    ids = [path.stem for path in conflicts_dir.glob("*.json") if not path.name.endswith(".resolved.json")]
    return sorted(ids)


def _boards_summary(board_store, workspaces) -> list[dict]:
    """Mission Board projection rows, keyed by board_id. Local reads only:
    conflict card ids from local sidecar files, ``unpublished`` (per board and
    per card) from the local realm-sync baseline sidecar — a pure file read, the
    same Decision-7 posture and baseline machinery ``_offices_summary`` uses."""

    from .board_models import board_content_hash
    from .board_sync import read_board_baseline

    workspace_ids = {getattr(w, "id", None) for w in workspaces}
    realm_by_workspace = {getattr(w, "id", None): getattr(w, "realm_id", None) for w in workspaces}
    baselines: dict[str, dict[str, str]] = {}
    boards: list[dict] = []
    for board in board_store.list_all():
        cards = board_store.list_cards(board.board_id)  # active, (order_key, card_id) sorted
        projected = cards[:MAX_BOARD_CARDS_PROJECTED]
        realm_id = realm_by_workspace.get(board.workspace_id)
        baseline: dict[str, str] | None = None
        if realm_id:
            if realm_id not in baselines:
                try:
                    baselines[realm_id] = read_board_baseline(realm_id)
                except Exception:
                    baselines[realm_id] = {}
            baseline = baselines[realm_id]

        def _card_unpublished(card) -> bool | None:
            # Publication honesty is only meaningful for realm-bound boards.
            if baseline is None:
                return None
            return baseline.get(f"{board.board_id}:card:{card.card_id}") != board_content_hash(card)

        board_unpublished: bool | None = None
        if baseline is not None:
            board_unpublished = baseline.get(f"{board.board_id}:board") != board_content_hash(board)

        row = {
            "board_id": board.board_id,
            "workspace_id": board.workspace_id,
            "title": board.title,
            "revision": board.revision,
            "updated_at": to_jsonable(board.updated_at),
            "columns": [
                {"column_id": c.column_id, "title": c.title, "kind": c.kind, "wip_limit": c.wip_limit}
                for c in board.columns
            ],
            "cards": [_board_card_row(c, unpublished=_card_unpublished(c)) for c in projected],
            "active_card_count": len(cards),
            "cards_truncated": max(0, len(cards) - len(projected)),
            "conflict_card_ids": _board_conflict_card_ids(board.board_id),
            "archived_card_ids": list(board.archived_card_ids),
            # A board whose workspace no longer resolves is accounted, never
            # silently hidden (repair via archive) — parity warning below.
            "orphaned": board.workspace_id not in workspace_ids,
        }
        if board_unpublished is not None:
            row["unpublished"] = board_unpublished
        boards.append(row)
    return boards


def _board_parity_warnings(data) -> list[dict]:
    warnings: list[dict] = []
    for board in _rows(data.get("boards")):
        if board.get("orphaned"):
            warnings.append(
                {
                    "code": "orphaned_board",
                    "entity_id": board.get("board_id"),
                    "detail": (
                        f"board '{board.get('board_id')}' points at workspace "
                        f"'{board.get('workspace_id')}' which no longer resolves; archive to repair"
                    ),
                }
            )
        for card_id in board.get("conflict_card_ids") or []:
            warnings.append(
                {
                    "code": "board_card_conflict",
                    "entity_id": card_id,
                    "board_id": board.get("board_id"),
                    "detail": (
                        f"board card '{card_id}' has an unresolved realm-sync conflict; "
                        "resolve with `harness board resolve-conflict <card_id> --take local|remote`"
                    ),
                }
            )
    return warnings


MAX_OFFICE_ACTORS_PROJECTED = 200


def _office_actor_summary_row(actor, *, unpublished: bool | None) -> dict:
    row = {
        "actor_key": actor.actor_key,
        "persona_id": actor.persona_id,
        "persona_instance_id": actor.persona_instance_id,
        "backing_profile": actor.backing_profile,
        "items": [
            {
                "item_id": item.item_id,
                "persona_id": item.persona_id,
                "kind": item.kind,
                "position": list(item.position),
                "folder": item.folder,
                "display_name": item.display_name,
                "pet_slug": item.pet_slug,
                "scale": item.scale,
            }
            for item in actor.items
        ],
        "revision": actor.revision,
        "updated_at": to_jsonable(actor.updated_at),
        "updated_by": actor.updated_by,
    }
    if unpublished is not None:
        row["unpublished"] = unpublished
    return row


def _offices_summary(office_store, workspaces) -> list[dict]:
    """Mission Office projection rows, keyed by workspace_id. Local reads only:
    conflict state from local sidecar files, ``unpublished`` from the local
    realm-sync baseline sidecar (a pure file read — Decision 7 posture)."""

    from .office_sync import read_office_baseline

    workspace_ids = {getattr(w, "id", None) for w in workspaces}
    realm_by_workspace = {getattr(w, "id", None): getattr(w, "realm_id", None) for w in workspaces}
    baselines: dict[str, dict[str, str]] = {}
    offices: list[dict] = []
    for workspace_token in office_store.list_workspaces():
        try:
            surface = office_store.get_surface(workspace_token)
        except Exception:
            continue
        actors = office_store.list_actors(workspace_token)
        projected = actors[:MAX_OFFICE_ACTORS_PROJECTED]
        realm_id = realm_by_workspace.get(surface.workspace_id)
        baseline: dict[str, str] | None = None
        if realm_id:
            if realm_id not in baselines:
                try:
                    baselines[realm_id] = read_office_baseline(realm_id)
                except Exception:
                    baselines[realm_id] = {}
            baseline = baselines[realm_id]

        def _actor_unpublished(actor) -> bool | None:
            # Publication honesty is only meaningful for realm-bound workspaces.
            if baseline is None:
                return None
            from .office_models import office_content_hash

            return baseline.get(f"{surface.workspace_id}:actor:{actor.actor_key}") != office_content_hash(actor)

        offices.append(
            {
                "workspace_id": surface.workspace_id,
                "folders": list(surface.folders),
                "actors": [_actor_summary for _actor_summary in (_office_actor_summary_row(a, unpublished=_actor_unpublished(a)) for a in projected)],
                "actor_count": len(actors),
                "actors_truncated": max(0, len(actors) - len(projected)),
                "conflict_actor_keys": office_store.conflict_actor_keys(workspace_token),
                "archived_actor_keys": list(surface.archived_actor_keys),
                "revision": surface.revision,
                "updated_at": to_jsonable(surface.updated_at),
                # A surface whose workspace no longer resolves is accounted,
                # never silently hidden — parity warning below.
                "orphaned": surface.workspace_id not in workspace_ids,
            }
        )
    return offices


def _office_parity_warnings(data) -> list[dict]:
    warnings: list[dict] = []
    for office in _rows(data.get("offices")):
        if office.get("orphaned"):
            warnings.append(
                {
                    "code": "orphaned_office",
                    "entity_id": office.get("workspace_id"),
                    "detail": (
                        f"office surface points at workspace '{office.get('workspace_id')}' "
                        "which no longer resolves"
                    ),
                }
            )
        for actor_key in office.get("conflict_actor_keys") or []:
            warnings.append(
                {
                    "code": "office_actor_conflict",
                    "entity_id": actor_key,
                    "workspace_id": office.get("workspace_id"),
                    "detail": (
                        f"office actor '{actor_key}' has an unresolved realm-sync conflict; "
                        "resolve with `harness office resolve-conflict --actor <key> --take local|remote`"
                    ),
                }
            )
    return warnings


def _parity_warnings(data) -> list[dict]:
    """Snapshot-level self-checks that flag likely UI/harness divergence."""

    # Board/office warnings do not depend on the persona-instance runtime, so
    # they are computed before the runtime-disabled early return below.
    warnings: list[dict] = _board_parity_warnings(data)
    warnings.extend(_office_parity_warnings(data))
    runtime = data.get("persona_instance_runtime") or {}
    if not runtime.get("enabled"):
        warnings.append(
            {
                "code": "persona_instance_runtime_disabled",
                "detail": "persona instance runtime is off; persona_instances / chat_history / chat_trace are absent from this snapshot",
            }
        )
        return warnings

    instances = _rows(data.get("persona_instances"))
    for group in duplicate_persona_instance_groups(instances):
        warnings.append(
            {
                "code": "duplicate_persona_instance",
                "entity_id": group["canonical_id"],
                "detail": (
                    f"{len(group['instance_ids'])} live persona-instance rows alias to one canonical id; "
                    "run `harness persona-instance reconcile`"
                ),
                "instance_ids": group["instance_ids"],
            }
        )

    # Orphan / held persona-instance accounting: rows whose backing persona/profile is
    # absent (or a mothballed role) project as phantom "on level" agents. Surface them
    # the same way duplicate rows are surfaced so nothing is silently dropped — the
    # reconciler prunes (archives) the prunable ones; held rows are protected and shown.
    template_names = [
        name
        for row in (data.get("available_personas") or [])
        if isinstance(row, dict)
        for name in (str((row or {}).get("hermes_profile") or "").strip(),)
        if name
    ]
    backed_ids, backed_profiles = backed_persona_identity(
        agents=data.get("agents") or [],
        profile_names=template_names,
    )
    orphan_classes = classify_orphan_persona_instances(
        instances,
        backed_persona_ids=backed_ids,
        backed_profile_names=backed_profiles,
        profile_catalog_authoritative=bool(template_names),
    )
    for entry in orphan_classes["prunable"]:
        warnings.append(
            {
                "code": "orphaned_persona_instance",
                "entity_id": entry["persona_instance_id"],
                "reason": entry["reason"],
                "detail": (
                    f"persona instance '{entry['persona_instance_id']}' has no backing persona/profile "
                    f"({entry['reason']}) and renders as a phantom agent; "
                    "run `harness persona-instance reconcile [--dry-run]`"
                ),
            }
        )
    for entry in orphan_classes["held"]:
        warnings.append(
            {
                "code": "held_orphan_persona_instance",
                "entity_id": entry["persona_instance_id"],
                "reason": entry["reason"],
                "detail": (
                    f"persona instance '{entry['persona_instance_id']}' is orphan-shaped but protected "
                    f"from prune ({entry['reason']})"
                ),
            }
        )
    instance_personas = {
        _canonical_persona_id(inst.get("persona_id")) for inst in instances if isinstance(inst, dict)
    }
    instance_ids = {
        str(inst.get("persona_instance_id") or inst.get("agent_profile_id") or "")
        for inst in instances
        if isinstance(inst, dict)
    }
    # Shape-valid JSON can still be referentially stale. Steering fields are
    # foreign keys into persona_instances; report every unresolved target in
    # the parity envelope so clients never have to infer corruption from a
    # missing card or a failed flow sync.
    for instance in instances:
        if not isinstance(instance, dict):
            continue
        source_id = str(
            instance.get("persona_instance_id")
            or instance.get("agent_profile_id")
            or ""
        )
        spawned_by = str(instance.get("spawned_by") or "").strip()
        if (
            spawned_by
            and looks_like_persona_instance_id(spawned_by)
            and spawned_by not in instance_ids
        ):
            warnings.append(
                {
                    "code": "fk_miss",
                    "from_entity": "persona_instance",
                    "from_id": source_id,
                    "fk_field": "spawned_by",
                    "target_entity": "persona_instances",
                    "target_id": spawned_by,
                    "detail": (
                        f"persona_instance '{source_id}' spawned_by -> "
                        f"{spawned_by} does not resolve in persona_instances; "
                        "run `harness persona-instance reconcile [--dry-run]`"
                    ),
                }
            )
        for parent_id in dict.fromkeys(instance.get("steered_by") or []):
            parent_id = str(parent_id or "").strip()
            if not parent_id or parent_id in instance_ids:
                continue
            warnings.append(
                {
                    "code": "fk_miss",
                    "from_entity": "persona_instance",
                    "from_id": source_id,
                    "fk_field": "steered_by",
                    "target_entity": "persona_instances",
                    "target_id": parent_id,
                    "detail": (
                        f"persona_instance '{source_id}' steered_by -> "
                        f"{parent_id} does not resolve in persona_instances; "
                        "run `harness persona-instance reconcile [--dry-run]`"
                    ),
                }
            )
    for row in data.get("persona_chat_trace") or []:
        if not isinstance(row, dict):
            continue
        row_persona = _canonical_persona_id(row.get("persona_id"))
        row_instance = str(row.get("persona_instance_id") or "")
        if row_persona not in instance_personas and row_instance not in instance_ids:
            warnings.append(
                {
                    "code": "trace_persona_not_in_instances",
                    "entity_id": row_instance or row_persona,
                    "detail": "trace row has no matching persona_instance; the launcher may orphan it",
                }
            )

    chat_rows = [
        row for row in data.get("persona_chat_history") or [] if isinstance(row, dict)
    ]
    for row in chat_rows:
        for field in ("created_at", "updated_at"):
            value = row.get(field)
            if value is not None and _parse_iso_timestamp(value) is None:
                warnings.append(
                    {
                        "code": "persona_chat_history.non_iso_timestamp",
                        "entity_id": row.get("session_id"),
                        "detail": f"persona_chat_history.{field} is not an ISO-8601 timestamp",
                    }
                )
                break

    chat_latest_by_persona: dict[str, datetime] = {}
    mission_latest_by_persona: dict[str, tuple[datetime, str | None]] = {}
    for row in chat_rows:
        persona_id = _canonical_persona_id(row.get("persona_id")) or ""
        if not persona_id:
            continue
        timestamp = _parse_iso_timestamp(row.get("updated_at")) or _parse_iso_timestamp(row.get("created_at"))
        if timestamp is None:
            continue
        if row.get("kind") == "mission" or row.get("live_mission") is True:
            current = mission_latest_by_persona.get(persona_id)
            if current is None or timestamp > current[0]:
                mission_latest_by_persona[persona_id] = (timestamp, row.get("session_id"))
        else:
            current = chat_latest_by_persona.get(persona_id)
            if current is None or timestamp > current:
                chat_latest_by_persona[persona_id] = timestamp
    build_moment = data.get("generated_at")
    if not isinstance(build_moment, datetime):
        build_moment = _parse_iso_timestamp(build_moment)
    for persona_id, (mission_time, session_id) in mission_latest_by_persona.items():
        chat_time = chat_latest_by_persona.get(persona_id)
        if chat_time is None or mission_time <= chat_time:
            continue
        # Mission rows anchor to the persisted assignment created_at, which is
        # legitimately newer than every chat right after a mission is assigned.
        # The regression this guard exists for is BUILD-TIME restamping — only a
        # mission timestamp hugging the snapshot's own generated_at is drift.
        if build_moment is not None and abs((mission_time - build_moment).total_seconds()) > _LIVE_MISSION_RESTAMP_EPSILON_SECONDS:
            continue
        warnings.append(
            {
                "code": "persona_chat_history.live_mission_shadow",
                "entity_id": session_id,
                "detail": "mission chat-history row tracks snapshot build time and shadows every real chat for the persona",
            }
        )

    channels = data.get("operator_channels")
    if channels is None:
        warnings.append(
            {
                "code": "operator_channels_missing",
                "detail": "persona runtime is enabled but the Agent Console channel projection is absent",
            }
        )
    elif not isinstance(channels, dict):
        # S4: operator_channels is now an id-keyed map (channel_id -> row).
        warnings.append(
            {
                "code": "operator_channels_invalid",
                "detail": "operator_channels must be an id-keyed map; Launcher Agent Console cannot render this snapshot",
            }
        )
    else:
        # S4 (delete derived copies -> FK-miss reports): each operator channel
        # carries FK ids into the owner entities (persona_instances) rather than
        # restating them. A ``persona_instance_id`` that does not resolve in the
        # keyed persona_instances map is a typed, resolvable pointer miss — not a
        # cross-projection "contract error" reconciling two copies of a fact.
        # Archived channels reference archived (frame-evicted) instances by
        # design, so they are exempt from the live-roster FK check.
        live_instance_ids = {
            str(row.get("persona_instance_id") or "")
            for row in instances
            if isinstance(row, dict) and row.get("persona_instance_id")
        }
        for channel in _rows(channels):
            if not isinstance(channel, dict):
                continue
            fk_target = str(channel.get("persona_instance_id") or "").strip()
            if (
                fk_target
                and str(channel.get("state") or "") != "archived"
                and fk_target not in live_instance_ids
            ):
                warnings.append(
                    {
                        "code": "fk_miss",
                        "from_entity": "operator_channel",
                        "from_id": channel.get("channel_id"),
                        "fk_field": "persona_instance_id",
                        "target_entity": "persona_instances",
                        "target_id": fk_target,
                        "detail": (
                            f"operator_channel '{channel.get('channel_id')}' "
                            f"persona_instance_id -> {fk_target} does not resolve in "
                            "persona_instances"
                        ),
                    }
                )
            for warning in channel.get("warnings") or []:
                if not isinstance(warning, dict):
                    continue
                code = str(warning.get("code") or "operator_channel_warning")
                warnings.append(
                    {
                        "code": f"operator_channel.{code}",
                        "entity_id": channel.get("channel_id"),
                        "detail": warning.get("detail") or "operator channel projection warning",
                    }
                )

    summary = data.get("summary") or {}
    if (summary.get("open_tasks") or 0) > 0 and not (data.get("goals")):
        warnings.append(
            {
                "code": "open_tasks_without_task_rows",
                "detail": "summary reports open tasks but no goal rows were mapped",
            }
        )
    try:
        threshold = int(getattr(load_root_runtime_config(), "open_incident_warning_threshold", 100))
    except Exception:
        threshold = 100
    try:
        open_incidents = int(summary.get("open_incidents") or 0)
    except Exception:
        open_incidents = 0
    if threshold > 0 and open_incidents > threshold:
        warnings.append(
            {
                "code": "open_incident_budget_exceeded",
                "detail": f"summary reports {open_incidents} open incidents, above the configured budget of {threshold}",
                "count": open_incidents,
                "threshold": threshold,
            }
        )
    return warnings


# A mission row anchored to its assignment's persisted created_at only matches
# the snapshot's own build moment in the transient poll right after assignment;
# a timestamp that hugs generated_at on every build is the restamping regression.
_LIVE_MISSION_RESTAMP_EPSILON_SECONDS = 10.0


def _parse_iso_timestamp(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _redaction_observed(value) -> dict[str, int]:
    counts: dict[str, int] = {}

    def visit(item) -> None:
        if isinstance(item, dict):
            marker = item.get("would_redact")
            if isinstance(marker, dict):
                for reason in marker.values():
                    key = str(reason or "unknown").strip() or "unknown"
                    counts[key] = counts.get(key, 0) + 1
            elif isinstance(marker, str) and marker.strip():
                key = marker.strip()
                counts[key] = counts.get(key, 0) + 1
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return counts


def _event_summary_warnings(events) -> list[dict]:
    warnings: list[dict] = []
    for event in events:
        try:
            missing = event_summary_missing(event)
        except Exception:
            missing = False
        if not missing:
            continue
        warnings.append(
            {
                "code": "event_summary_missing",
                "event_type": getattr(event, "type", None),
                "task_id": getattr(event, "task_id", None),
                "run_id": getattr(event, "run_id", None),
                "detail": "operator-visible event is missing a redaction-safe summary",
            }
        )
    return warnings


_STALE_SNAPSHOT_TMP_AGE_SECONDS = 3600.0


def _sweep_stale_snapshot_tmp_files() -> None:
    """Remove orphaned ``.snapshot_*.tmp`` files beside the boot cache.

    ``atomic_json_write`` stages via ``tempfile.mkstemp(prefix=".snapshot_",
    suffix=".tmp")`` in the store root; a crash between staging and
    ``os.replace`` strands the temp file forever (live root had two, one 3MB).
    Swept only at the next boot-cache write, age-gated so an in-flight writer's
    fresh temp file is never touched. Best-effort: a locked/vanished file is
    skipped, never raised.
    """

    try:
        cutoff = time.time() - _STALE_SNAPSHOT_TMP_AGE_SECONDS
        for tmp in paths.snapshot_path().parent.glob(".snapshot_*.tmp"):
            try:
                if tmp.stat().st_mtime < cutoff:
                    tmp.unlink()
            except OSError:
                continue
    except OSError:
        return


def write_snapshot(snapshot: dict | None = None) -> dict:
    snapshot = snapshot or build_snapshot()
    # Compact JSON: the boot cache is machine-read only (launcher boot decode +
    # audits), and indent=2 inflated the 9MB-era cache by ~30%. sort_keys stays
    # (stable diffs / byte-reproducible caches).
    atomic_json_write(
        paths.snapshot_path(),
        to_jsonable(snapshot),
        indent=None,
        separators=(",", ":"),
        sort_keys=True,
    )
    _sweep_stale_snapshot_tmp_files()
    cfg = load_root_runtime_config()
    read_model_cfg = getattr(cfg, "read_model", None)
    if bool(getattr(read_model_cfg, "enabled", False)):
        from .read_model import ReadModel

        watermark = (snapshot.get("parity") or {}).get("watermark") or {}
        ReadModel().apply_full_rebuild(snapshot, watermark=watermark)
    return snapshot


def _default_persona_session_db():
    # Same acquisition as the projection lane it feeds — see
    # ``chat_session_scope`` for the resolution ladder.
    from .chat_session_scope import open_chat_session_db

    return open_chat_session_db()


def _archived_task_summaries(limit: int = 25) -> list[dict]:
    archive_root = paths.deleted_archive_dir()
    if not archive_root.exists():
        return []
    summaries: list[dict] = []
    for batch_dir in sorted((p for p in archive_root.iterdir() if p.is_dir()), reverse=True):
        manifest = _read_json(batch_dir / "manifest.json")
        reason = str(manifest.get("reason") or "Archived mission batch")
        archived_at = str(
            manifest.get("created_at_utc")
            or manifest.get("created_at")
            or batch_dir.name
        )
        seen_ids: set[str] = set()
        task_dir = batch_dir / "tasks"
        if task_dir.exists():
            for task_path in sorted(task_dir.glob("*.json")):
                task = _read_json(task_path)
                task_id = str(task.get("id") or task.get("task_id") or task_path.stem)
                seen_ids.add(task_id)
                summaries.append(
                    _archived_task_summary(
                        task_id,
                        task,
                        batch_dir.name,
                        reason,
                        archived_at,
                        batch_dir,
                    )
                )
                if len(summaries) >= limit:
                    return summaries
        for archived in manifest.get("archived_tasks") or []:
            if not isinstance(archived, dict):
                continue
            task_id = str(archived.get("task_id") or "").strip()
            if not task_id or task_id in seen_ids:
                continue
            summaries.append(_archived_task_summary(task_id, archived, batch_dir.name, reason, archived_at, batch_dir))
            if len(summaries) >= limit:
                return summaries
    return summaries


def _read_json(path):
    data = cached_by_mtime(
        path,
        lambda candidate: json.loads(candidate.read_text(encoding="utf-8")),
        default={},
    )
    return data if isinstance(data, dict) else {}


def _archived_task_summary(task_id: str, raw: dict, archive_batch: str, reason: str, archived_at: str, batch_dir=None) -> dict:
    original_state = str(raw.get("state") or "archived")
    if original_state not in {"done", "cancelled", "blocked", "archived"}:
        original_state = "archived"
    runs = _archived_run_summaries(batch_dir, task_id) if batch_dir is not None else []
    proofs = _archived_proof_summaries(batch_dir, task_id) if batch_dir is not None else []
    role_envelopes = _archived_role_envelope_summaries(batch_dir, task_id) if batch_dir is not None else []
    role_checklists = _archived_role_checklist_summaries(batch_dir, task_id) if batch_dir is not None else []
    proof_batches = _archived_proof_batch_summaries(batch_dir, task_id) if batch_dir is not None else []
    persona_assignments = _archived_persona_assignment_summaries(batch_dir, task_id) if batch_dir is not None else []
    repo_bundles = _archived_repo_bundle_summaries(batch_dir, task_id) if batch_dir is not None else []
    incident_ids = raw.get("incident_ids") if isinstance(raw.get("incident_ids"), list) else raw.get("open_incident_ids") if isinstance(raw.get("open_incident_ids"), list) else []
    archive_events = _archived_event_log_events(batch_dir, task_id) if batch_dir is not None else []
    recent_events = _dedupe_archived_events(
        [
            *_archived_transcript_events(task_id, runs, proofs),
            *archive_events,
        ]
    )[-80:]
    persona_timing_summaries = _persona_timing_summaries(runs)
    return {
        "task_id": task_id,
        "goal_id": str(raw.get("goal_id") or raw.get("mission_goal_id") or "").strip() or None,
        "title": str(raw.get("title") or "Archived mission"),
        "description": str(raw.get("description") or "").strip() or None,
        "routing_scope": raw.get("routing_scope") if isinstance(raw.get("routing_scope"), dict) else None,
        "acceptance_criteria": [
            str(item)
            for item in (raw.get("acceptance_criteria") or [])
            if isinstance(item, str) and item.strip()
        ][:20],
        "state": "archived",
        "original_state": original_state,
        "archive_batch": archive_batch,
        "archive_reason": reason,
        "archived_at": archived_at,
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "runs": runs,
        "persona_timing_summaries": persona_timing_summaries,
        "proof_summaries": proofs,
        "incident_count": len(incident_ids),
        "recent_events": recent_events,
        "role_envelopes": role_envelopes,
        "role_checklists": role_checklists,
        "proof_batches": proof_batches,
        "persona_assignment_ids": [item.get("assignment_id") for item in persona_assignments if item.get("assignment_id")],
        "persona_assignments": persona_assignments,
        "repo_bundle_ids": [item.get("repo_bundle_id") for item in repo_bundles if item.get("repo_bundle_id")],
        "repo_bundles": repo_bundles,
        "bundle_queue": [item for item in repo_bundles if item.get("state") == "queued_waiting_dependency"],
        "qa_waiting_on": [
            item.get("repo_bundle_id")
            for item in repo_bundles
            if item.get("repo_bundle_id")
            and item.get("state") not in {"delivered_waiting_for_qa", "delivered", "verified", "blocked", "cancelled", "archived"}
        ],
        "persona_streams": _archived_persona_streams(task_id, persona_assignments, runs, recent_events),
        "role_streams": _archived_role_streams(
            task_id,
            raw,
            runs,
            recent_events,
            role_envelopes=role_envelopes,
            role_checklists=role_checklists,
            proof_batches=proof_batches,
        ),
    }


def _archived_operator_channels(archived_tasks: list[dict], *, live_task_ids: set[str] | None = None, limit: int = 25) -> list[dict]:
    live_task_ids = live_task_ids or set()
    channels: list[dict] = []
    for archived in archived_tasks[:limit]:
        task_id = str(archived.get("task_id") or "").strip()
        if not task_id or task_id in live_task_ids:
            continue
        channel = _archived_operator_channel(archived)
        if channel is not None:
            channels.append(channel)
    return channels


def _archived_operator_channel(archived: dict) -> dict | None:
    task_id = str(archived.get("task_id") or "").strip()
    if not task_id:
        return None
    goal_id = str(archived.get("goal_id") or "").strip() or None
    channel_id = f"neko_supervisor::archived:{task_id}"
    messages: list[dict] = []
    goal_message = _archived_goal_input_message(archived, channel_id=channel_id)
    if goal_message is not None:
        messages.append(goal_message)
    for index, assignment in enumerate(archived.get("persona_assignments") or []):
        if isinstance(assignment, dict):
            message = _archived_assignment_message(assignment, channel_id=channel_id, index=index)
            if message is not None:
                messages.append(message)
    for index, event in enumerate(archived.get("recent_events") or []):
        if isinstance(event, dict):
            message = _archived_event_message(event, channel_id=channel_id, index=index)
            if message is not None:
                messages.append(message)
    messages.sort(key=_archived_conversation_message_sort_key)
    messages = _dedupe_archived_conversation_messages(messages)
    for seq, message in enumerate(messages, start=1):
        message["seq"] = seq
    updated_at = _latest_archived_message_timestamp(messages) or archived.get("updated_at") or archived.get("archived_at")
    # S8: an archived channel is a POINTER STUB — the embedded transcript
    # (~20-25 KB/row of dead-mission messages) leaves the frame (operator ruling
    # 2026-07-17: "old residue and runs need to be purged — it should just be
    # pointers to chat history"). The full transcript stays on disk in the
    # deleted-archive batch and is fetched on demand via ``harness task history``
    # (or the persona chat-history query). The stub keeps identity + recency +
    # message_count so the console renders an honest archived row + a fetch
    # affordance, never a fake-empty conversation. ``messages`` is an explicit
    # empty list flagged ``messages_evicted`` so the launcher conversation parser
    # distinguishes an evicted transcript from a genuinely empty one.
    message_count = len(messages)
    fetch = f"harness task history {task_id} --json"
    conversation = {
        "schema_version": OPERATOR_CONVERSATION_SCHEMA_VERSION,
        "thread_id": channel_id,
        "goal_id": goal_id,
        "task_id": task_id,
        "owner_persona_id": "neko_supervisor",
        "persona_instance_id": f"archived:{task_id}:neko_supervisor",
        "session_id": None,
        "root_thread_id": channel_id,
        "parent_thread_id": None,
        "title": _archived_conversation_text(archived.get("title"), limit=240) or "Archived mission",
        "state": "archived",
        "updated_at": updated_at,
        "status": "archived",
        "incomplete_reason": None,
        "messages": [],
        "messages_evicted": True,
        "message_count": message_count,
        "fetch": fetch,
    }
    return {
        "schema_version": OPERATOR_CHANNELS_SCHEMA_VERSION,
        "channel_id": channel_id,
        "persona_id": "neko_supervisor",
        "persona_instance_id": f"archived:{task_id}:neko_supervisor",
        "session_id": None,
        "task_id": task_id,
        "goal_id": goal_id,
        "display_name": _display_name_for_persona("neko_supervisor"),
        "state": "archived",
        "mode": "archived_goal",
        "source_instance_ids": [],
        "history": None,
        "trace": None,
        "conversation": conversation,
        "conversation_status": "archived",
        "message_count": message_count,
        "messages_evicted": True,
        "fetch": fetch,
        "trace_count": 0,
        "tool_trace_count": 0,
        "warnings": [],
        "archived": True,
        "archive_batch": archived.get("archive_batch"),
        "archive_reason": archived.get("archive_reason"),
        "archived_at": archived.get("archived_at"),
        "updated_at": updated_at,
    }


def _archived_goal_input_message(archived: dict, *, channel_id: str) -> dict | None:
    task_id = str(archived.get("task_id") or "").strip()
    title = _archived_conversation_text(archived.get("title"), limit=500) or "Archived mission"
    parts = [f"Goal: {title}"]
    description = _archived_conversation_text(archived.get("description"), limit=4000)
    if description:
        parts.append(f"Objective: {description}")
    acceptance = [
        item
        for item in (
            _archived_conversation_text(value, limit=500)
            for value in (archived.get("acceptance_criteria") or [])
        )
        if item
    ]
    if acceptance:
        parts.append("Acceptance:\n" + "\n".join(f"- {item}" for item in acceptance))
    return {
        "id": f"{channel_id}:goal_input:{task_id}",
        "seq": 0,
        "timestamp": archived.get("created_at") or archived.get("archived_at"),
        "actor_persona_id": "operator",
        "actor_instance_id": None,
        "target_persona_id": "neko_supervisor",
        "target_persona_instance_id": f"archived:{task_id}:neko_supervisor",
        "role": "operator",
        "kind": "goal_input",
        "status": "delivered",
        "display_title": "",
        "display_text": "\n\n".join(parts),
        "redaction_status": "safe",
        "refs": {"task_id": task_id, "goal_id": archived.get("goal_id"), "source": "deleted_archive"},
    }


def _archived_assignment_message(assignment: dict, *, channel_id: str, index: int) -> dict | None:
    target_persona_id = str(assignment.get("persona_id") or "agent").strip() or "agent"
    title = _archived_conversation_text(assignment.get("title"), limit=240)
    prompt = _archived_conversation_text(assignment.get("message"), limit=1200)
    if not title and not prompt:
        return None
    parts = [f"Prompted {target_persona_id}."]
    if title:
        parts.append(f"Stage: {title}")
    if prompt:
        parts.append(f"Prompt: {prompt}")
    proof_targets = _archived_conversation_list(assignment.get("proof_targets"), limit=160)
    if proof_targets:
        parts.append("Proof expected: " + "; ".join(proof_targets))
    allowed_decisions = _archived_conversation_list(assignment.get("allowed_decisions"), limit=80)
    if allowed_decisions:
        parts.append("Allowed decisions: " + ", ".join(allowed_decisions))
    refs = {"source": "persona_assignment"}
    for key in ("id", "assignment_id", "task_id", "goal_id", "stage_id", "persona_instance_id"):
        value = str(assignment.get(key) or "").strip()
        if value:
            refs["assignment_id" if key == "id" else key] = value
    return {
        "id": f"{channel_id}:assignment:{refs.get('assignment_id', index)}",
        "seq": 0,
        "timestamp": assignment.get("created_at") or assignment.get("updated_at"),
        "actor_persona_id": "neko_supervisor",
        "actor_instance_id": f"archived:{refs.get('task_id', 'task')}:neko_supervisor",
        "target_persona_id": target_persona_id,
        "target_persona_instance_id": assignment.get("persona_instance_id"),
        "role": "agent",
        "kind": "handoff",
        "status": str(assignment.get("state") or "delivered"),
        "display_title": "Subagent prompt",
        "display_text": "\n".join(parts),
        "redaction_status": "safe",
        "refs": refs,
    }


def _archived_event_message(event: dict, *, channel_id: str, index: int) -> dict | None:
    event_type = str(event.get("type") or "").strip()
    if event_type == "proof.attached":
        text = _archived_conversation_text(event.get("summary"), limit=1200)
        kind = "proof"
        title = "Proof update"
        role = "proof"
    elif event_type in {"run.progress", "run.decision"}:
        text = _archived_conversation_text(
            event.get("reasoning_summary") or event.get("rationale") or event.get("summary"),
            limit=1200,
        )
        kind = "final" if event_type == "run.decision" or str(event.get("status") or "") in {"completed", "passed", "approved"} else "agent_update"
        title = "Final update" if kind == "final" else "Neko update"
        role = "agent"
    else:
        return None
    if not text:
        return None
    persona_id = str(event.get("persona_id") or "neko_supervisor").strip() or "neko_supervisor"
    refs = {"source": "deleted_archive", "event_type": event_type}
    for key in ("task_id", "run_id", "proof_id", "stage_id"):
        value = str(event.get(key) or "").strip()
        if value:
            refs[key] = value
    return {
        "id": f"{channel_id}:event:{event_type}:{refs.get('run_id') or refs.get('proof_id') or index}",
        "seq": 0,
        "timestamp": event.get("ts"),
        "actor_persona_id": persona_id,
        "actor_instance_id": None,
        "role": role,
        "kind": kind,
        "status": str(event.get("status") or "recorded"),
        "display_title": title,
        "display_text": text,
        "redaction_status": "safe",
        "refs": refs,
    }


def _archived_conversation_text(value, *, limit: int) -> str | None:
    text = str(value or "").replace("\x00", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Mask secret-bearing lines in place instead of dropping the whole message —
    # archived transcripts stay legible; only the offending line is withheld.
    lines = [
        "[redacted line — contained a secret]"
        if _ARCHIVED_CONVERSATION_SECRET_RE.search(line)
        else " ".join(line.split())
        for line in text.split("\n")
    ]
    normalized = "\n".join(lines).strip()
    normalized = re.sub(r"\n{4,}", "\n\n\n", normalized)
    if not normalized:
        return None
    return normalized[:limit].rstrip()


def _archived_conversation_list(value, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    items = [_archived_conversation_text(item, limit=limit) for item in value]
    return [item for item in items if item][:8]


def _archived_conversation_message_sort_key(message: dict) -> tuple[int, str, str]:
    if message.get("kind") == "goal_input":
        return (0, "", str(message.get("id") or ""))
    parsed = _parse_archived_time(message.get("timestamp"))
    if parsed is not None:
        return (1, parsed.isoformat(), str(message.get("id") or ""))
    return (2, str(message.get("timestamp") or ""), str(message.get("id") or ""))


def _dedupe_archived_conversation_messages(messages: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    seen_updates: set[str] = set()
    deduped: list[dict] = []
    for message in messages:
        text = str(message.get("display_text") or "")
        if message.get("kind") in {"agent_update", "final"}:
            if text in seen_updates:
                continue
            seen_updates.add(text)
        key = (str(message.get("kind") or ""), str(message.get("display_text") or ""))
        if key in seen and message.get("kind") in {"agent_update", "final"}:
            continue
        seen.add(key)
        deduped.append(message)
    return deduped


def _latest_archived_message_timestamp(messages: list[dict]):
    dated = [message.get("timestamp") for message in messages if message.get("timestamp")]
    return dated[-1] if dated else None


def _parse_archived_time(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _archived_run_summaries(batch_dir, task_id: str) -> list[dict]:
    run_dir = batch_dir / "runs"
    if not run_dir.exists():
        return []
    runs: list[dict] = []
    for run_path in sorted(run_dir.glob("*.json")):
        raw = _read_json(run_path)
        if str(raw.get("task_id") or "") != task_id:
            continue
        runs.append(_run_summary_from_mapping(raw))
    return sorted(runs, key=lambda run: str(run.get("started_at") or ""))


def _archived_proof_summaries(batch_dir, task_id: str) -> list[dict]:
    proof_dir = batch_dir / "proofs" / task_id
    if not proof_dir.exists():
        return []
    proofs: list[dict] = []
    for proof_path in sorted(proof_dir.glob("*.json")):
        raw = _read_json(proof_path)
        if raw and str(raw.get("task_id") or task_id) == task_id:
            proofs.append(_proof_summary_from_mapping(raw))
    return proofs


def _archived_role_envelope_summaries(batch_dir, task_id: str) -> list[dict]:
    role_dir = batch_dir / "role_envelopes" / task_id
    if not role_dir.exists():
        return []
    summaries: list[dict] = []
    for role_path in sorted(role_dir.glob("*.json")):
        raw = _read_json(role_path)
        if str(raw.get("task_id") or "") != task_id:
            continue
        try:
            summaries.append(role_envelope_summary(from_jsonable(RoleEnvelope, raw)))
        except Exception:
            continue
    return summaries


def _archived_role_checklist_summaries(batch_dir, task_id: str) -> list[dict]:
    checklist_dir = batch_dir / "role_checklists" / task_id
    if not checklist_dir.exists():
        return []
    summaries: list[dict] = []
    for checklist_path in sorted(checklist_dir.glob("*.json")):
        raw = _read_json(checklist_path)
        if str(raw.get("task_id") or "") != task_id:
            continue
        try:
            summaries.append(checklist_summary(from_jsonable(RoleChecklist, raw)))
        except Exception:
            continue
    return summaries


def _archived_proof_batch_summaries(batch_dir, task_id: str) -> list[dict]:
    proof_batch_dir = batch_dir / "proof_batches" / task_id
    if not proof_batch_dir.exists():
        return []
    summaries: list[dict] = []
    for proof_batch_path in sorted(proof_batch_dir.glob("*.json")):
        raw = _read_json(proof_batch_path)
        if str(raw.get("task_id") or "") != task_id:
            continue
        try:
            summaries.append(proof_batch_summary(from_jsonable(ProofBatch, raw)))
        except Exception:
            continue
    return summaries


def _archived_persona_assignment_summaries(batch_dir, task_id: str) -> list[dict]:
    assignment_dir = batch_dir / "persona_assignments"
    if not assignment_dir.exists():
        return []
    summaries: list[dict] = []
    for assignment_path in sorted(assignment_dir.glob("*.json")):
        raw = _read_json(assignment_path)
        if str(raw.get("task_id") or "") != task_id:
            continue
        summaries.append(
            {
                "assignment_id": str(raw.get("id") or raw.get("assignment_id") or assignment_path.stem),
                "persona_instance_id": raw.get("persona_instance_id"),
                "persona_id": raw.get("persona_id"),
                "kind": raw.get("kind"),
                "state": raw.get("state"),
                "title": raw.get("title"),
                "message": raw.get("message"),
                "task_id": raw.get("task_id"),
                "goal_id": raw.get("goal_id"),
                "stage_id": raw.get("stage_id"),
                "created_at": raw.get("created_at"),
                "updated_at": raw.get("updated_at"),
                "proof_targets": raw.get("proof_targets") if isinstance(raw.get("proof_targets"), list) else [],
                "allowed_decisions": raw.get("allowed_decisions") if isinstance(raw.get("allowed_decisions"), list) else [],
                "run_ids": raw.get("run_ids") if isinstance(raw.get("run_ids"), list) else [],
                "proof_ids": raw.get("proof_ids") if isinstance(raw.get("proof_ids"), list) else [],
            }
        )
    return summaries


def _archived_repo_bundle_summaries(batch_dir, task_id: str) -> list[dict]:
    bundle_dir = batch_dir / "repo_bundles" / task_id
    if not bundle_dir.exists():
        return []
    summaries: list[dict] = []
    for bundle_path in sorted(bundle_dir.glob("*.json")):
        raw = _read_json(bundle_path)
        if str(raw.get("task_id") or "") != task_id:
            continue
        summaries.append(
            {
                "repo_bundle_id": str(raw.get("id") or bundle_path.stem),
                "task_id": raw.get("task_id"),
                "repo": raw.get("repo"),
                "owner_persona_id": raw.get("owner_persona_id"),
                "state": raw.get("state"),
                "title": raw.get("title"),
                "stage_ids": raw.get("stage_ids") if isinstance(raw.get("stage_ids"), list) else [],
                "assignment_id": raw.get("assignment_id"),
                "active_run_id": raw.get("active_run_id"),
                "proof_ids": raw.get("proof_ids") if isinstance(raw.get("proof_ids"), list) else [],
                "dependency_bundle_ids": raw.get("dependency_bundle_ids") if isinstance(raw.get("dependency_bundle_ids"), list) else [],
                "queue_reason": raw.get("queue_reason"),
                "wake_condition": raw.get("wake_condition"),
            }
        )
    return summaries


def _archived_persona_streams(task_id: str, assignments: list[dict], runs: list[dict], events: list[dict]) -> dict:
    role_ids = {"neko_supervisor", "backend_dev", "dev", "qa"}
    role_ids.update(str(item.get("persona_id") or "") for item in assignments)
    role_ids.update(str(item.get("persona_id") or "") for item in runs)
    role_ids.update(str(item.get("persona_id") or "") for item in events)
    streams = {}
    for role_id in sorted(role for role in role_ids if role):
        role_assignments = [item for item in assignments if item.get("persona_id") == role_id]
        role_runs = [item for item in runs if item.get("persona_id") == role_id]
        role_events = [item for item in events if item.get("persona_id") == role_id]
        streams[role_id] = {
            "persona_id": role_id,
            "assignment_ids": [item.get("assignment_id") for item in role_assignments if item.get("assignment_id")],
            "run_ids": [item.get("run_id") for item in role_runs if item.get("run_id")],
            "event_count": len(role_events),
            "latest_event_type": role_events[-1].get("type") if role_events else None,
        }
    return streams


def _archived_event_log_events(batch_dir, task_id: str) -> list[dict]:
    """Read archived events_<task>.jsonl rows for replay/projection."""

    if batch_dir is None:
        return []
    path = batch_dir / f"events_{_safe_archive_task_filename(task_id)}.jsonl"
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    events: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except Exception:
            continue
        if not isinstance(raw, dict) or str(raw.get("task_id") or "") != task_id:
            continue
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        event_type = str(raw.get("type") or "event")
        event = {
            "type": event_type,
            "ts": raw.get("ts"),
            "task_id": task_id,
            "run_id": raw.get("run_id") or payload.get("run_id"),
            "persona_id": raw.get("persona_id") or payload.get("persona_id"),
            "summary": payload.get("summary") or payload.get("display_summary") or _archived_event_display_title(event_type, payload),
            "event_id": payload.get("event_id") or raw.get("event_id"),
            "phase": payload.get("phase"),
            "severity": payload.get("severity"),
            "step": payload.get("step"),
            "status": payload.get("status"),
            "tool_name": payload.get("tool_name") or payload.get("tool"),
            "tool_id": payload.get("tool_id"),
            "skill_id": payload.get("skill_id"),
            "detail": payload.get("detail"),
            "exit_code": payload.get("exit_code"),
            "duration_ms": payload.get("duration_ms"),
            "proof_id": payload.get("proof_id"),
            "decision_type": payload.get("decision_type"),
            "validation_status": payload.get("validation_status"),
            "next_expected": payload.get("next_expected"),
            "rationale": payload.get("rationale") or payload.get("decision_rationale"),
            "reasoning_summary": payload.get("reasoning_summary"),
            "display_kind": payload.get("display_kind") or _archived_event_display_kind(event_type),
            "display_title": payload.get("display_title") or _archived_event_display_title(event_type, payload),
            "display_summary": payload.get("display_summary") or payload.get("summary"),
            "redaction_status": payload.get("redaction_status") or "safe",
        }
        events.append({key: value for key, value in event.items() if value is not None})
    return events


def _safe_archive_task_filename(task_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(task_id).strip())[:80] or "task"


def _dedupe_archived_events(events: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for event in events:
        event_id = str(event.get("event_id") or "").strip()
        if event_id:
            key = ("event_id", event_id, "")
        else:
            key = (
                str(event.get("type") or ""),
                str(event.get("run_id") or ""),
                str(event.get("ts") or ""),
            )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)
    return deduped


def _run_summary_from_mapping(raw: dict) -> dict:
    final_decision = raw.get("final_decision") if isinstance(raw.get("final_decision"), dict) else {}
    llm = _safe_llm(raw.get("llm"))
    decision_summary = _safe_text(final_decision.get("summary"))
    decision_rationale = _safe_text(final_decision.get("rationale"))
    reasoning_summary = _safe_text(final_decision.get("reasoning_summary") or final_decision.get("process_summary")) or decision_rationale
    return {
        "run_id": str(raw.get("id") or raw.get("run_id") or ""),
        "persona_id": raw.get("persona_id"),
        "task_id": raw.get("task_id"),
        "stage_id": raw.get("stage_id"),
        "state": str(raw.get("state") or ""),
        "started_at": raw.get("started_at"),
        "finished_at": raw.get("finished_at"),
        "duration_ms": _duration_ms(raw.get("started_at"), raw.get("finished_at")),
        "decision_type": _public_decision_value(final_decision.get("type"), llm.get("public_decision_type"), llm.get("decision_type")),
        "decision_summary": decision_summary,
        "decision_rationale": decision_rationale,
        "reasoning_summary": reasoning_summary,
        "session_id": raw.get("session_id") or llm.get("session_id"),
        "llm": llm or None,
        "has_error": bool(raw.get("error")),
    }


def _persona_timing_summaries(runs: list[dict]) -> list[dict]:
    summaries: dict[str, dict] = {}
    for run in runs:
        persona_id = str(run.get("persona_id") or "").strip()
        if not persona_id:
            continue
        llm = run.get("llm") if isinstance(run.get("llm"), dict) else {}
        timing = llm.get("timing") if isinstance(llm.get("timing"), dict) else {}
        entry = summaries.setdefault(
            persona_id,
            {
                "persona_id": persona_id,
                "run_count": 0,
                "run_ids": [],
                "duration_ms": 0,
                "persona_runtime_ms": 0,
                "provider_call_ms": 0,
                "provider_call_count": 0,
                "conversation_call_ms": 0,
                "conversation_call_count": 0,
                "stream_consume_ms": 0,
                "api_calls": 0,
                "tool_turns": 0,
                "total_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
            },
        )
        entry["run_count"] += 1
        if run.get("run_id"):
            entry["run_ids"].append(run.get("run_id"))
        _add_int(entry, "duration_ms", run.get("duration_ms"))
        _add_int(entry, "persona_runtime_ms", timing.get("persona_runtime_ms"))
        _add_int(entry, "provider_call_ms", _timing_total(timing, "provider_call"))
        _add_int(entry, "provider_call_count", timing.get("provider_call_count") or (1 if _timing_total(timing, "provider_call") is not None else None))
        _add_int(entry, "conversation_call_ms", _timing_total(timing, "profile_conversation_call"))
        _add_int(entry, "conversation_call_count", timing.get("profile_conversation_call_count") or (1 if _timing_total(timing, "profile_conversation_call") is not None else None))
        _add_int(entry, "stream_consume_ms", _timing_total(timing, "profile_provider_stream_consume"))
        _add_int(entry, "api_calls", llm.get("api_calls"))
        _add_int(entry, "tool_turns", llm.get("tool_turns"))
        _add_int(entry, "total_tokens", llm.get("total_tokens"))
        _add_int(entry, "input_tokens", llm.get("input_tokens"))
        _add_int(entry, "output_tokens", llm.get("output_tokens"))
    return sorted(summaries.values(), key=lambda item: str(item.get("persona_id") or ""))


def _timing_total(timing: dict, key: str) -> int | None:
    total = timing.get(f"{key}_total_ms")
    if isinstance(total, int) and total >= 0:
        return total
    current = timing.get(f"{key}_ms")
    return current if isinstance(current, int) and current >= 0 else None


def _add_int(target: dict, key: str, value) -> None:
    if isinstance(value, bool):
        return
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return
    if parsed >= 0:
        target[key] = int(target.get(key) or 0) + parsed


def _duration_ms(started_at, finished_at) -> int | None:
    try:
        if not started_at or not finished_at:
            return None
        start = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        finish = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
        duration = int((finish - start).total_seconds() * 1000)
    except Exception:
        return None
    return duration if duration >= 0 else None


def _proof_summary_from_mapping(raw: dict) -> dict:
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    return {
        "proof_id": str(raw.get("id") or raw.get("proof_id") or ""),
        "type": str(raw.get("type") or "proof"),
        "status": metadata.get("status") or metadata.get("verdict"),
        "exit_code": metadata.get("exit_code"),
        "duration_ms": metadata.get("duration_ms"),
        "created_by": raw.get("created_by"),
        "has_artifact": bool(raw.get("path_or_value")),
    }


def _archived_transcript_events(task_id: str, runs: list[dict], proofs: list[dict]) -> list[dict]:
    events: list[dict] = []
    for run in runs:
        summary = run.get("decision_summary")
        rationale = run.get("decision_rationale")
        reasoning_summary = run.get("reasoning_summary") or rationale
        if summary or reasoning_summary:
            events.append(
                {
                    "type": "run.progress",
                    "ts": run.get("finished_at") or run.get("started_at"),
                    "task_id": task_id,
                    "run_id": run.get("run_id"),
                    "persona_id": run.get("persona_id"),
                    "summary": "Agent decision process summarized",
                    "event_id": f"progress:{run.get('run_id') or 'archived'}:thinking_process:decision_summary",
                    "phase": "thinking_process",
                    "step": "decision_summary",
                    "status": "completed",
                    "reasoning_summary": reasoning_summary or summary,
                    "decision_type": run.get("decision_type"),
                    "validation_status": (run.get("llm") or {}).get("validation_status") if isinstance(run.get("llm"), dict) else None,
                    "display_kind": "thinking_summary",
                    "display_title": "Agent decision process summarized",
                    "display_summary": "Agent decision process summarized",
                }
            )
        if summary or rationale:
            events.append(
                {
                    "type": "run.decision",
                    "ts": run.get("finished_at") or run.get("started_at"),
                    "task_id": task_id,
                    "run_id": run.get("run_id"),
                    "persona_id": run.get("persona_id"),
                    "summary": summary,
                    "rationale": rationale,
                    "reasoning_summary": reasoning_summary,
                    "decision_type": run.get("decision_type"),
                    "validation_status": (run.get("llm") or {}).get("validation_status") if isinstance(run.get("llm"), dict) else None,
                }
            )
    for proof in proofs:
        proof_id = proof.get("proof_id")
        events.append(
            {
                "type": "proof.attached",
                "ts": None,
                "task_id": task_id,
                "persona_id": proof.get("created_by"),
                "summary": f"Proof {proof_id} {proof.get('status') or 'recorded'}",
                "proof_id": proof_id,
                "status": proof.get("status"),
                "exit_code": proof.get("exit_code"),
                "duration_ms": proof.get("duration_ms"),
            }
        )
    return events[-20:]


def _archived_role_streams(
    task_id: str,
    raw: dict,
    runs: list[dict],
    events: list[dict],
    *,
    role_envelopes: list[dict] | None = None,
    role_checklists: list[dict] | None = None,
    proof_batches: list[dict] | None = None,
) -> list[dict]:
    mission_plan = raw.get("mission_plan") if isinstance(raw.get("mission_plan"), dict) else {}
    role_ids: list[str] = []
    role_envelopes = role_envelopes or []
    role_checklists = role_checklists or []
    proof_batches = proof_batches or []
    if not (mission_plan.get("enabled") is True or mission_plan.get("stages") or role_envelopes or role_checklists or proof_batches):
        return []
    role_ids.extend(["neko_supervisor", "backend_dev", "dev", "qa"])
    for stage in mission_plan.get("stages") or []:
        if not isinstance(stage, dict):
            continue
        owner = str(stage.get("owner") or "")
        if owner in {"backend_dev", "dev", "qa", "neko_supervisor"}:
            role_ids.append(owner)
    for run in runs:
        role_ids.append(str(run.get("persona_id") or ""))
    for event in events:
        role_ids.append(str(event.get("persona_id") or ""))
    for envelope in role_envelopes:
        role_ids.append(str(envelope.get("role_id") or ""))
    for checklist in role_checklists:
        role_ids.append(str(checklist.get("role_id") or ""))
    for batch in proof_batches:
        role_ids.append(str(batch.get("role_id") or ""))

    seen: set[str] = set()
    streams: list[dict] = []
    for role_id in role_ids:
        if not role_id or role_id in seen:
            continue
        seen.add(role_id)
        role_events = _coalesced_archived_progress_events([event for event in events if str(event.get("persona_id") or "") == role_id])
        stream_events = [_archived_event_stream_item(event) for event in role_events][-20:]
        stream_envelopes = [item for item in role_envelopes if str(item.get("role_id") or "") == role_id]
        stream_checklists = [item for item in role_checklists if str(item.get("role_id") or "") == role_id]
        envelope_ids = {str(item.get("envelope_id") or "") for item in stream_envelopes}
        stream_batches = [
            item
            for item in proof_batches
            if str(item.get("role_id") or "") == role_id
            or str(item.get("role_envelope_id") or "") in envelope_ids
        ]
        if not stream_events:
            stream_events = [_empty_archived_role_stream_item(raw, role_id)]
        streams.append(
            {
                "persona_id": role_id,
                "display_name": _display_name_for_persona(role_id),
                "current_stage_id": _archived_role_current_stage(raw, role_id),
                "active_run_ids": [],
                "active_worker_session_ids": [],
                "events": stream_events,
                "role_envelopes": stream_envelopes,
                "role_checklists": stream_checklists,
                "proof_batches": stream_batches,
            }
        )
    return streams


def _archived_event_stream_item(event: dict) -> dict:
    event_type = str(event.get("type") or "event")
    summary = _safe_text(
        event.get("summary")
        or event.get("display_summary")
        or event.get("status")
        or event.get("reasoning_summary")
        or _archived_event_display_title(event_type, event)
    )
    payload = {
        "display_kind": event.get("display_kind") or _archived_event_display_kind(event_type),
        "display_title": event.get("display_title") or _archived_event_display_title(event_type, event),
        "display_summary": summary,
        "redaction_status": event.get("redaction_status") or "safe",
    }
    for key in (
        "phase",
        "severity",
        "step",
        "status",
        "tool_name",
        "tool_id",
        "skill_id",
        "detail",
        "exit_code",
        "duration_ms",
        "proof_id",
        "decision_type",
        "validation_status",
        "next_expected",
        "rationale",
        "reasoning_summary",
        "artifact_refs",
        "event_id",
    ):
        if event.get(key) is not None:
            payload[key] = event.get(key)
    if summary:
        payload["summary"] = summary
    return {
        "ts": event.get("ts"),
        "type": event_type,
        "run_id": event.get("run_id"),
        "persona_id": event.get("persona_id"),
        "payload": payload,
    }


def _coalesced_archived_progress_events(events: list[dict]) -> list[dict]:
    items: list[dict] = []
    index_by_progress_id: dict[str, int] = {}
    for event in events:
        event_id = str(event.get("event_id") or "").strip()
        if str(event.get("type") or "") == "run.progress" and event_id:
            existing = index_by_progress_id.get(event_id)
            if existing is not None:
                items[existing] = event
                continue
            index_by_progress_id[event_id] = len(items)
        items.append(event)
    return items


def _empty_archived_role_stream_item(raw: dict, persona_id: str) -> dict:
    display_name = _display_name_for_persona(persona_id)
    current_stage_id = _archived_role_current_stage(raw, persona_id)
    summary = f"{display_name} archived stream is visible; no redaction-safe events were captured for this role."
    return {
        "ts": raw.get("updated_at") or raw.get("created_at"),
        "type": "role_stream.status",
        "run_id": None,
        "persona_id": persona_id,
        "payload": {
            "display_kind": "event",
            "display_title": f"{display_name} archived",
            "display_summary": summary,
            "redaction_status": "safe",
            "status": "idle",
            "summary": summary,
            "stage_id": current_stage_id,
        },
    }


def _archived_event_display_kind(event_type: str) -> str:
    if event_type == "run.progress":
        return "thinking_summary" if str(event_type or "") else "event"
    if event_type == "proof.attached":
        return "proof"
    if event_type.startswith("run.tool."):
        return "tool_call"
    if event_type in {"run.decision", "packet.recorded"}:
        return "handoff"
    return "event"


def _archived_event_display_title(event_type: str, event: dict) -> str:
    if event_type == "proof.attached":
        return f"Proof {event.get('status') or 'attached'}"
    if event_type.startswith("run.tool."):
        return f"Tool {event.get('tool_name') or event_type}"
    if event_type == "run.decision":
        decision = str(event.get("decision_type") or "decision").replace("_", " ")
        return f"Run {decision}".strip()
    return event_type


def _archived_role_current_stage(raw: dict, persona_id: str) -> str | None:
    mission_plan = raw.get("mission_plan") if isinstance(raw.get("mission_plan"), dict) else {}
    current_stage_id = mission_plan.get("current_stage_id") or raw.get("current_stage_id")
    for stage in mission_plan.get("stages") or []:
        if not isinstance(stage, dict):
            continue
        owner = stage.get("owner")
        if owner == persona_id or (owner == "dev" and persona_id == "dev"):
            status = str(stage.get("status") or "")
            if stage.get("id") == current_stage_id or status not in {"ready_for_qa", "passed"}:
                return stage.get("id")
    return current_stage_id if isinstance(current_stage_id, str) else None


def _task_summary(task, proofs, all_tasks=None, incidents=None, runs=None, events=None, workers=None, run_store=None, self_tests=None, role_envelopes=None, role_checklists=None, proof_batches=None, persona_assignments=None, repo_bundles=None, runtime_instances=None, persona_instances=None, stage_verification_accountant=None, flow_timeline_accountant=None, event_log=None):
    gate = task_verdict_proof_satisfied(task, proofs)
    current_stage_id = _task_current_stage_id(task)
    current = next((s for s in task_stage_records(task) if s.id == current_stage_id), None)
    untriaged = untriaged_issue_discoveries(task)
    child_count = len([item for item in (all_tasks or []) if getattr(item, "parent_task_id", None) == task.id])
    open_incidents = [item for item in (incidents or []) if item.task_id == task.id and item.closed_at is None]
    active_runs = [item for item in (runs or []) if item.task_id == task.id and item.state in ACTIVE_RUN_STATES]
    active_workers = [
        worker
        for worker in (workers or [])
        if getattr(worker, "task_id", None) == task.id
        and str(getattr(getattr(worker, "state", ""), "value", getattr(worker, "state", ""))) not in {"completed", "blocked", "closed"}
    ]
    next_action = _next_action_summary(
        task,
        open_incidents,
        run_store=run_store,
        event_log=event_log,
    )
    assignments = list(persona_assignments or [])
    bundles = list(repo_bundles or [])
    runtime_lane = _runtime_lane_summary(runtime_instances or [])
    return {
        "task_id": task.id,
        "goal_id": getattr(task, "goal_id", None) or task.id,
        "title": task.title,
        "description": getattr(task, "description", None),
        "acceptance_criteria": list(getattr(task, "acceptance_criteria", []) or []),
        "routing_scope": getattr(task, "routing_scope", {}) or None,
        "state": str(task.state),
        "workspace_id": getattr(task, "workspace_id", None),
        "realm_id": None,
        "mission_state": _mission_lifecycle_state(task, active_runs, open_incidents, gate),
        "parent_task_id": task.parent_task_id,
        "child_task_count": child_count,
        "current_stage_id": current_stage_id,
        "current_stage_title": current.title if current else None,
        "simplified_phase": simplified_phase_for_task(task, bundles),
        "active_assignment_id": next((assignment.id for assignment in assignments if assignment.state in ACTIVE_ASSIGNMENT_STATES), None),
        "repo_bundle_ids": [bundle.id for bundle in bundles],
        "repo_bundles": [repo_bundle_summary(bundle) for bundle in bundles],
        "repo_bundle_closeout": repo_bundle_delivery_summary(bundles) if bundles else None,
        "bundle_queue": bundle_queue_summary(bundles),
        "runtime_lane": runtime_lane,
        "qa_waiting_on": qa_waiting_on(bundles),
        "execution_status": _execution_status(task, active_runs, open_incidents),
        "active_run_ids": [run.id for run in active_runs],
        "active_worker_session_ids": [worker.id for worker in active_workers],
        "active_persona_ids": sorted({run.persona_id for run in active_runs}),
        "can_start_run": _can_start_run(task, active_runs, open_incidents),
        "run_blocked_reason": _run_blocked_reason(task, active_runs, open_incidents, run_store=run_store),
        "requires_visual_proof": task.requires_visual_proof,
        "delivery_directive": task_delivery_directive(task),
        "missing_proof": gate.missing,
        "open_incident_count": len(task.open_incident_ids) or len(open_incidents),
        "issue_discovery_counts": issue_discovery_counts(task),
        "untriaged_issue_severities": sorted({str(item.get("severity", "medium")) for item in untriaged}),
        "proof_summaries": [_proof_visibility_summary(proof) for proof in proofs],
        "self_test_summaries": [self_test_summary(item) for item in (self_tests or [])],
        "verification_status": _verification_status(task),
        "stage_verification": _stage_verification(task, proofs or [], accountant=stage_verification_accountant),
        "timeline": _task_timeline(task.id, events or []),
        "next_action": next_action,
        "why_not_done": _why_not_done(task, gate, open_incidents, next_action),
        "updated_at": task.updated_at,
        "mission_plan": mission_plan_summary(task),
        "role_streams": _role_streams(
            task,
            events or [],
            runs or [],
            workers or [],
            persona_assignments=assignments,
            role_envelopes=role_envelopes or [],
            role_checklists=role_checklists or [],
            proof_batches=proof_batches or [],
        ),
        "persona_assignment_ids": [assignment.id for assignment in assignments],
        "persona_streams": _persona_streams(task, assignments, runs or [], events or []),
        "role_envelopes": [role_envelope_summary(item) for item in (role_envelopes or [])],
        "role_checklists": [checklist_summary(item) for item in (role_checklists or [])],
        "proof_batches": [proof_batch_summary(item) for item in (proof_batches or [])],
        "stage_streams": _stage_streams(task, events or []),
        "mission_level_state": _mission_level_state(
            task,
            active_runs=active_runs,
            active_workers=active_workers,
            events=events or [],
            runtime_instances=runtime_instances or [],
            persona_instances=persona_instances or [],
            role_streams=_role_streams(
                task,
                events or [],
                runs or [],
                workers or [],
                persona_assignments=assignments,
                role_envelopes=role_envelopes or [],
                role_checklists=role_checklists or [],
                proof_batches=proof_batches or [],
            ),
        ),
        "mission_flow_timeline": _mission_flow_timeline(task, events or [], accountant=flow_timeline_accountant),
        "proof_gate_state": _proof_gate_state(task, proofs),
        "operator_capabilities": _operator_capabilities(task, next_action, gate),
    }


def _verification_status(task) -> dict:
    root = getattr(task, "harness_self_heal", None)
    observations = root.get("stage_observations") if isinstance(root, dict) else None
    if not isinstance(observations, dict):
        return {"stage_observations": []}
    rows = []
    for stage_id, item in list(observations.items())[-8:]:
        if not isinstance(item, dict):
            continue
        diff = item.get("repo_diff") if isinstance(item.get("repo_diff"), dict) else {}
        rows.append(
            {
                "stage_id": stage_id,
                "repo_diff_chars": int(diff.get("diff_chars") or 0),
                "baseline_dirty_count": int(diff.get("baseline_dirty_count") or 0),
                "observed_proof_count": len(item.get("observed_proof_ids") or []),
                "authoritative_gate_status": str(item.get("authoritative_gate_status") or "pending")[:40],
                "authoritative_gate_proof_count": len(item.get("authoritative_gate_proof_ids") or []),
            }
        )
    return {"stage_observations": rows}


def _stage_verification(task, proofs: list, *, accountant: ProjectionAccountant | None = None) -> dict:
    root = getattr(task, "harness_self_heal", None)
    observations = root.get("stage_observations") if isinstance(root, dict) else None
    if not isinstance(observations, dict):
        return {
            "schema_version": 1,
            "stages": [],
            "completeness": {
                "stage_cap": STAGE_VERIFICATION_STAGE_CAP,
                "proof_id_cap": STAGE_VERIFICATION_PROOF_ID_CAP,
                "excluded_path_cap": STAGE_VERIFICATION_PATH_CAP,
                "truncated": False,
            },
        }

    items = [(str(stage_id or "_task"), item) for stage_id, item in observations.items() if isinstance(item, dict)]
    items.sort(key=lambda pair: str(pair[1].get("captured_at") or pair[1].get("authoritative_gate_recorded_at") or pair[0]))
    if accountant is not None:
        accountant.consider(len(items))
    selected = items[-STAGE_VERIFICATION_STAGE_CAP:]
    if len(items) > len(selected):
        if accountant is not None:
            # Deliberate bound: the lane keeps the latest N stage rows.
            accountant.drop(
                "stage_cap",
                count=len(items) - len(selected),
                entity_id=getattr(task, "id", None),
                detail=f"kept latest {STAGE_VERIFICATION_STAGE_CAP} stage verification rows",
                by_design=True,
            )
            accountant.mark_truncated()

    proof_by_id = {str(getattr(proof, "id", "") or ""): proof for proof in proofs}
    owners = _stage_owner_by_id(task)
    rows = []
    truncated = len(items) > len(selected)
    for stage_id, item in selected:
        if accountant is not None:
            accountant.include()
        diff = item.get("repo_diff") if isinstance(item.get("repo_diff"), dict) else {}
        observed_ids, observed_truncated = _bounded_projection_strings(
            item.get("observed_proof_ids"),
            cap=STAGE_VERIFICATION_PROOF_ID_CAP,
            accountant=accountant,
            drop_code="observed_proof_id_cap",
            entity_id=stage_id,
        )
        authoritative_ids, authoritative_truncated = _bounded_projection_strings(
            item.get("authoritative_gate_proof_ids"),
            cap=STAGE_VERIFICATION_PROOF_ID_CAP,
            accountant=accountant,
            drop_code="authoritative_proof_id_cap",
            entity_id=stage_id,
        )
        excluded_paths, excluded_truncated = _bounded_projection_strings(
            diff.get("excluded_baseline_paths"),
            cap=STAGE_VERIFICATION_PATH_CAP,
            accountant=accountant,
            drop_code="excluded_baseline_path_cap",
            entity_id=stage_id,
        )
        source_diff_truncated = bool(diff.get("truncated"))
        if source_diff_truncated and accountant is not None:
            # Deliberate bound, applied upstream at capture time (the repo-diff
            # capture is char-capped) and mirrored honestly into the envelope —
            # the diff is bounded, not lost.
            accountant.drop("source_diff_truncated", entity_id=stage_id, by_design=True)
            accountant.mark_truncated()
        lane_truncated = observed_truncated or authoritative_truncated or excluded_truncated
        if lane_truncated and accountant is not None:
            accountant.mark_truncated()
        truncated = truncated or lane_truncated or source_diff_truncated
        rows.append(
            {
                "stage_id": str(item.get("stage_id") or stage_id)[:128],
                "owner": owners.get(stage_id) or str(item.get("actor") or "")[:128] or None,
                "source": str(item.get("source") or "stage_observation")[:80],
                "captured_at": str(item.get("captured_at") or "")[:80] or None,
                "repo_diff": {
                    "diff_chars": _safe_int(diff.get("diff_chars")),
                    "truncated": source_diff_truncated,
                    "baseline_dirty_count": _safe_int(diff.get("baseline_dirty_count")),
                    "excluded_baseline_paths": excluded_paths,
                    "excluded_baseline_path_count": _safe_int(diff.get("baseline_dirty_count"))
                    if not isinstance(diff.get("excluded_baseline_paths"), list)
                    else len(diff.get("excluded_baseline_paths") or []),
                    "error": str(diff.get("error") or "")[:120] or None,
                },
                "observed": {
                    "kind": "agent_run",
                    "run_id": str(item.get("run_id") or "")[:128] or None,
                    "status": _proof_lane_status(observed_ids, proof_by_id=proof_by_id, fallback="not_applicable"),
                    "proof_ids": observed_ids,
                    "proof": [_proof_ref(proof_id, proof_by_id=proof_by_id) for proof_id in observed_ids],
                },
                "authoritative": {
                    "kind": "harness_run",
                    "run_id": str(item.get("authoritative_gate_run_id") or "")[:128] or None,
                    "status": str(item.get("authoritative_gate_status") or "pending")[:40],
                    "proof_ids": authoritative_ids,
                    "proof": [_proof_ref(proof_id, proof_by_id=proof_by_id) for proof_id in authoritative_ids],
                    "recorded_at": str(item.get("authoritative_gate_recorded_at") or "")[:80] or None,
                },
                "tamper_flag": bool(item.get("tamper_flag") or item.get("test_tampering_detected")) or _stage_tamper_flag(task, stage_id),
            }
        )

    return {
        "schema_version": 1,
        "stages": rows,
        "completeness": {
            "stage_cap": STAGE_VERIFICATION_STAGE_CAP,
            "proof_id_cap": STAGE_VERIFICATION_PROOF_ID_CAP,
            "excluded_path_cap": STAGE_VERIFICATION_PATH_CAP,
            "truncated": truncated,
        },
    }


def _stage_owner_by_id(task) -> dict[str, str]:
    plan = getattr(task, "mission_plan", None)
    owners: dict[str, str] = {}
    for stage in list(getattr(plan, "stages", []) or []):
        stage_id = str(getattr(stage, "id", "") or "").strip()
        owner = str(getattr(stage, "owner", "") or "").strip()
        if stage_id and owner:
            owners[stage_id] = owner
    return owners


def _bounded_projection_strings(raw, *, cap: int, accountant: ProjectionAccountant | None, drop_code: str, entity_id: str) -> tuple[list[str], bool]:
    """Cap a string list and account the overflow.

    Every caller of this helper IS a deliberate cap (proof-id / excluded-path
    windows), so the drop it records is declared by-design. Do not route an
    identity/consistency drop through here.
    """

    values = [str(value).strip()[:160] for value in (raw or []) if str(value or "").strip()] if isinstance(raw, list) else []
    if len(values) <= cap:
        return values, False
    if accountant is not None:
        accountant.drop(
            drop_code,
            count=len(values) - cap,
            entity_id=entity_id,
            detail=f"kept {cap}",
            by_design=True,
        )
    return values[:cap], True


def _proof_ref(proof_id: str, *, proof_by_id: dict[str, object]) -> dict:
    proof = proof_by_id.get(proof_id)
    metadata = getattr(proof, "metadata", None) if proof is not None else None
    metadata = metadata if isinstance(metadata, dict) else {}
    return {
        "proof_id": proof_id,
        "status": _proof_status(proof),
        "type": str(getattr(proof, "type", "") or metadata.get("type") or "proof")[:80],
        "created_by": str(getattr(proof, "created_by", "") or metadata.get("created_by") or "")[:80] or None,
    }


def _proof_lane_status(proof_ids: list[str], *, proof_by_id: dict[str, object], fallback: str) -> str:
    if not proof_ids:
        return fallback
    statuses = [_proof_status(proof_by_id.get(proof_id)) for proof_id in proof_ids]
    if any(status == "failed" for status in statuses):
        return "failed"
    if statuses and all(status == "passed" for status in statuses):
        return "passed"
    if any(status == "running" for status in statuses):
        return "running"
    return "pending"


def _proof_status(proof) -> str:
    if proof is None:
        return "unknown"
    metadata = getattr(proof, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    return str(metadata.get("status") or getattr(proof, "status", None) or "captured")[:40]


def _stage_tamper_flag(task, stage_id: str) -> bool:
    flags = {str(flag).strip() for flag in (getattr(task, "risk_flags", None) or [])}
    return "test_tampering_detected" in flags


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _task_current_stage_id(task) -> str | None:
    plan = getattr(task, "mission_plan", None)
    return getattr(plan, "current_stage_id", None) if plan is not None else getattr(task, "current_stage_id", None)


def _goal_projection_from_task(task, proofs, all_tasks, incidents, runs, events, *, workers=None, run_store=None, self_tests=None, role_envelopes=None, role_checklists=None, proof_batches=None, persona_assignments=None, repo_bundles=None, runtime_instances=None, persona_instances=None, stage_verification_accountant=None, flow_timeline_accountant=None, workspaces=None, event_log=None) -> dict:
    # S4 goals/tasks merge: the goal entity is the FULL task summary (all the
    # rich lanes the old ``tasks`` section carried — self_tests, role_envelopes,
    # role_checklists, proof_batches, accounted stage_verification) UNIONED with
    # the goal-only fields below. The old ``goals`` projection omitted these
    # lanes; the merged single owner must carry them so no field the retired
    # ``tasks`` section held is lost.
    row = _task_summary(
        task,
        proofs,
        all_tasks,
        incidents,
        runs,
        events,
        workers=workers,
        run_store=run_store,
        self_tests=self_tests,
        role_envelopes=role_envelopes,
        role_checklists=role_checklists,
        proof_batches=proof_batches,
        persona_assignments=persona_assignments,
        repo_bundles=repo_bundles,
        runtime_instances=runtime_instances,
        persona_instances=persona_instances,
        stage_verification_accountant=stage_verification_accountant,
        flow_timeline_accountant=flow_timeline_accountant,
        event_log=event_log,
    )
    workspace_id = getattr(task, "workspace_id", None)
    realm_id = None
    for workspace in workspaces or []:
        if getattr(workspace, "id", None) == workspace_id:
            realm_id = getattr(workspace, "realm_id", None)
            break
    row.update(
        {
            "id": getattr(task, "goal_id", None) or task.id,
            "kind": "goal",
            "task_id": task.id,
            "workspace_id": workspace_id,
            "realm_id": realm_id,
        }
    )
    return row


def goal_detail_for_task(task_id: str, *, event_log=None) -> dict | None:
    """The FULL goal projection for ONE task, rebuilt read-only from the stores.

    S8: the steady-state frame ships only the goal HEAD (``_goal_head``); the
    heavy detail (``GOAL_DETAIL_ONLY_FIELDS``) is served on demand here — the
    exact bytes ``_goal_projection_from_task`` produced before the split, so the
    launcher's goal-detail drawer / blueprint / replay views fetch identical data
    to what the pre-S8 in-frame row carried. Read-only: lists stores + the
    EventLog, mutates nothing. Returns ``None`` when the task id does not resolve
    (an honest miss, never a fabricated empty goal)."""

    token = str(task_id or "").strip()
    if not token:
        return None
    event_log = event_log or CachedEventLog()
    task_store = TaskStore()
    # ``get_goal`` resolves BOTH a task id and a goal id (parent/child tasks can
    # share one goal_id), raising NotFound on a genuine miss.
    from .errors import NotFound

    try:
        task = task_store.get_goal(token)
    except NotFound:
        return None
    tasks = task_store.list_all()
    run_store = RunStore()
    runs = run_store.list_all()
    proof_store = ProofStore()
    incident_store = IncidentStore()
    incidents = incident_store.list_all()
    worker_session_store = WorkerSessionStore(event_log=event_log)
    workers = worker_session_store.list_all()
    role_envelope_store = RoleEnvelopeStore(event_log=event_log)
    role_checklist_store = RoleChecklistStore(event_log=event_log)
    proof_batch_store = ProofBatchStore(event_log=event_log)
    repo_bundle_store = RepoBundleStore(event_log=event_log)
    runtime_instance_store = GoalRuntimeInstanceStore(event_log=event_log)
    workspace_store = WorkspaceStore()
    workspaces = workspace_store.list_all(include_archived=True)
    cfg = load_agent_runtime_config()
    persona_instances: list = []
    persona_assignments: list = []
    if persona_instance_runtime_enabled(cfg):
        agents = AgentStore().list_all() or seed_personas()
        persona_instances = PersonaInstanceStore(event_log=event_log).derive_from_workers(agents, workers)
        if persona_assignment_store_enabled(cfg):
            persona_assignments = PersonaAssignmentStore(event_log=event_log).list_all()
    return _goal_projection_from_task(
        task,
        proof_store.list_for_task(task.id),
        tasks,
        incidents,
        runs,
        event_log.for_task(task.id, limit=200),
        workers=workers,
        run_store=run_store,
        self_tests=SelfTestEvidenceStore(event_log=event_log).list_for_task(task.id),
        role_envelopes=[item for item in role_envelope_store.list_all() if item.task_id == task.id],
        role_checklists=[item for item in role_checklist_store.list_all() if item.task_id == task.id],
        proof_batches=[item for item in proof_batch_store.list_all() if item.task_id == task.id],
        persona_assignments=[item for item in persona_assignments if item.task_id == task.id],
        repo_bundles=[item for item in repo_bundle_store.list_all() if item.task_id == task.id],
        runtime_instances=[item for item in runtime_instance_store.list_all() if item.task_id == task.id],
        persona_instances=persona_instances,
        stage_verification_accountant=None,
        workspaces=workspaces,
        event_log=event_log,
    )


def _agent_tool_detail(agent) -> dict:
    """The evicted tool-detail payloads for one persona (``agents`` section row),
    rebuilt read-only — the same fields ``_agent_summary`` carried before R2 evicted
    them (minus the retired ``agent_hud_state``)."""

    from .tool_visibility import permission_state_for_persona, turn_tool_context_for_persona

    tool_resolution = resolve_tool_visibility(agent)
    return {
        "persona_id": agent.id,
        "display_name": agent.display_name,
        "tool_resolution": tool_resolution,
        "turn_tool_context": turn_tool_context_for_persona(
            agent, visibility=tool_resolution
        ),
        "permission_state": permission_state_for_persona(
            agent, visibility=tool_resolution
        ),
        "blocked_tools": tool_resolution["blocked_tools"],
    }


def persona_instance_detail_for_id(entity_id: str, *, event_log=None) -> dict | None:
    """The tool-detail payloads R2 evicted from ``persona_instances`` / ``agents``
    rows, rebuilt read-only and served on demand by ``harness persona-instance
    detail``.

    Resolves ``entity_id`` as a live persona-instance id first (the same derived
    set the frame keys), then falls back to a persona (agent) id — both wire rows
    carry a ``visibility_ref`` pointing here. Read-only (lists stores, mutates
    nothing). Returns ``None`` on a genuine miss (an honest ``not_found`` the
    launcher surfaces as 'unavailable', never a fabricated empty payload)."""

    token = str(entity_id or "").strip()
    if not token:
        return None
    from .persona_assignments import persona_instance_tool_detail

    event_log = event_log or CachedEventLog()
    cfg = load_agent_runtime_config()
    agents = AgentStore().list_all() or seed_personas()
    personas_by_id = {str(getattr(a, "id", "") or ""): a for a in agents}
    if persona_instance_runtime_enabled(cfg):
        worker_session_store = WorkerSessionStore(event_log=event_log)
        workers = worker_session_store.list_all()
        instances = PersonaInstanceStore(event_log=event_log).derive_from_workers(agents, workers)
        for instance in instances:
            if str(getattr(instance, "id", "") or "") == token:
                persona = personas_by_id.get(str(getattr(instance, "persona_id", "") or ""))
                return persona_instance_tool_detail(instance, persona)
    persona = personas_by_id.get(token)
    if persona is not None:
        return _agent_tool_detail(persona)
    return None


def _workspace_summary(
    workspace,
    *,
    tasks,
    persona_instances=(),
    active_id: str | None = None,
) -> dict:
    goals = [task for task in tasks if getattr(task, "workspace_id", None) == workspace.id]
    roster_agent_ids = list(workspace.agent_ids or [])
    live_scoped_agent_ids = exact_scoped_instance_ids(
        persona_instances,
        workspace_id=workspace.id,
    )
    return {
        "id": workspace.id,
        "kind": "workspace",
        "name": workspace.name,
        "slug": workspace.slug,
        "realm_id": workspace.realm_id,
        # Keep the legacy ``agents``/``agent_ids`` pair internally coherent:
        # both describe exact live rows placed in this workspace.  Emitting
        # roster persona ids here made pointerless canonical operator rows look
        # workspace-scoped to older consumers, duplicating each real placement.
        # Persisted roster metadata remains available under the explicit
        # ``roster_agent_*`` contract.
        "agents": len(live_scoped_agent_ids),
        "agent_ids": live_scoped_agent_ids,
        "live_scoped_agent_count": len(live_scoped_agent_ids),
        "live_scoped_agent_ids": live_scoped_agent_ids,
        "roster_agent_count": len(roster_agent_ids),
        "roster_agent_ids": roster_agent_ids,
        "goals": len(goals),
        "isolation": workspace.isolation,
        "max_concurrent_lanes": workspace.max_concurrent_lanes,
        "default_blueprint_id": workspace.default_blueprint_id,
        "archived": bool(workspace.archived),
        "active": workspace.id == active_id,
        "updated_at": workspace.updated_at,
    }


def _realm_summary(realm, *, workspaces, active_id: str | None = None) -> dict:
    workspace_ids = [workspace.id for workspace in workspaces if getattr(workspace, "realm_id", None) == realm.id]
    configured_ids = list(getattr(realm, "workspace_ids", []) or [])
    merged_ids = list(dict.fromkeys([*configured_ids, *workspace_ids]))
    return {
        "id": realm.id,
        "kind": "realm",
        "name": realm.name,
        "slug": realm.slug,
        "server_id": realm.server_id,
        "default_workspace_id": getattr(realm, "default_workspace_id", None),
        "default_workspace_name": getattr(realm, "default_workspace_name", "Default"),
        "default_workspace_version": getattr(realm, "default_workspace_version", 0),
        "workspaces": len(merged_ids),
        "workspace_ids": merged_ids,
        # Stage 43 (Decision 7): sync state comes ONLY from the cached sidecar
        # written by the `realm sync status|pull|publish` verbs — build_snapshot
        # must never shell out to git or resolve artifacts. Absent sidecar →
        # null so the launcher renders "not checked", not a fake in_sync.
        "sync": read_realm_sync_sidecar(realm.id),
        "archived": bool(realm.archived),
        "active": realm.id == active_id,
        "updated_at": realm.updated_at,
    }


def _snapshot_warnings(persona_assignments) -> list[dict]:
    warnings: list[dict] = []
    active = [item for item in persona_assignments or [] if getattr(item, "state", None) in ACTIVE_ASSIGNMENT_STATES]
    for assignment in active:
        goal_id = getattr(assignment, "goal_id", None) or getattr(assignment, "task_id", None)
        for other in active:
            if other is assignment:
                continue
            other_goal = getattr(other, "goal_id", None) or getattr(other, "task_id", None)
            if assignment.persona_id == other.persona_id and goal_id and other_goal and goal_id != other_goal:
                warnings.append(
                    {
                        "code": "agent_already_assigned",
                        "persona_id": assignment.persona_id,
                        "goal_id": goal_id,
                        "other_goal_id": other_goal,
                        "assignment_id": assignment.id,
                    }
                )
                break
    return warnings


def _mission_lifecycle_state(task, active_runs, open_incidents, gate) -> str:
    if task.state == TaskState.CANCELLED:
        return "cancelled"
    if task.state == TaskState.FAILED:
        return "failed"
    if task.state == TaskState.DONE:
        return "ready_for_tony" if not gate.missing else "blocked"
    if open_incidents:
        return "waiting_for_operator" if any(getattr(item, "kind", "") in {"qa_intervention_required", "operator_intervention", "run_budget_exceeded", "patch_landed_nowhere", "stage_no_progress"} for item in open_incidents) else "blocked"
    if active_runs:
        return "running"
    if task.state == TaskState.BLOCKED:
        return "blocked"
    return "queued"


def _mission_level_state(task, *, active_runs, active_workers, events, runtime_instances, persona_instances, role_streams) -> dict:
    plan = getattr(task, "mission_plan", None)
    stages = list(getattr(plan, "stages", []) or [])
    stage_by_owner: dict[str, list] = {}
    for stage in stages:
        owner = str(getattr(stage, "owner", "") or "").strip()
        if owner:
            stage_by_owner.setdefault(owner, []).append(stage)
    actor_ids = list(stage_by_owner)
    for run in active_runs:
        if getattr(run, "persona_id", None) and run.persona_id not in actor_ids:
            actor_ids.append(run.persona_id)
    active_stage_id = getattr(plan, "current_stage_id", None) or getattr(task, "current_stage_id", None)
    stream_by_id = {stream.get("persona_id"): stream for stream in role_streams if isinstance(stream, dict)}
    actors = []
    for persona_id in actor_ids:
        owned_stages = stage_by_owner.get(persona_id, [])
        active_owned = next((stage for stage in owned_stages if stage.id == active_stage_id), None)
        actor_stage = active_owned if active_owned is not None else (owned_stages[0] if owned_stages else None)
        latest = _latest_actor_event(persona_id, events)
        runs = [run for run in active_runs if getattr(run, "persona_id", None) == persona_id]
        workers = [worker for worker in active_workers if getattr(worker, "persona_id", None) == persona_id]
        stream = stream_by_id.get(persona_id) or {}
        status = getattr(getattr(actor_stage, "status", None), "value", getattr(actor_stage, "status", None))
        actors.append(
            {
                "id": f"actor_{persona_id}",
                "kind": "agent",
                "actor_id": persona_id,
                "persona_id": persona_id,
                "persona_instance_id": next(iter(stream.get("persona_instance_ids") or []), None),
                "goal_id": getattr(task, "goal_id", None) or task.id,
                "task_id": task.id,
                "chat_session_id": next((getattr(worker, "session_id", None) for worker in workers if getattr(worker, "session_id", None)), None),
                "worker_session_id": next((getattr(worker, "id", None) for worker in workers), None),
                "display_name": _display_name_for_persona(persona_id),
                "label": _display_name_for_persona(persona_id),
                "role": _actor_role_for_persona(persona_id),
                "presence": _actor_presence(active_owned, owned_stages, runs, workers),
                "stage_id": getattr(actor_stage, "id", None),
                "flow": {
                    "blueprint_id": getattr(plan, "blueprint_id", None),
                    "stage": getattr(actor_stage, "id", None),
                    "status": status,
                },
                "state_label": _actor_state_label(active_owned, owned_stages, runs, workers),
                "state": _actor_state_label(active_owned, owned_stages, runs, workers),
                "active_run_ids": [run.id for run in runs],
                "active_worker_session_ids": [worker.id for worker in workers],
                "latest_safe_event": latest,
                "latest_event": latest,
                "budget": _actor_budget_summary(runs, stream_by_id.get(persona_id)),
                "hidden_by_blueprint": False,
            }
        )
    return {
        "schema_version": 1,
        "mission_id": task.id,
        "task_id": task.id,
        "blueprint_id": getattr(plan, "blueprint_id", None),
        "blueprint_version": getattr(plan, "blueprint_version", None),
        "active_stage_id": active_stage_id,
        "actors": actors,
        # S4 (delete derived copies): ``agent_topology`` was a DERIVED copy of
        # steering truth (``persona_instances[].steered_by``). It leaves the
        # frame entirely — the launcher already derives both directions from
        # ``steered_by`` (missionAgentInstanceParentIds /
        # missionAgentSteeredInstances) and its runtime-graph projection falls
        # back to that steered_by path when no topology is present. Zero
        # ``agent_topology`` bytes ship; the ``_agent_topology`` builder is now
        # unreferenced (its removal is S7 cleanup, kept out of this shrink stage
        # to bound the diff).
        "updated_at": getattr(task, "updated_at", None),
    }


def agent_topology_for_task(task) -> dict:
    """Re-derive the steering topology (incl. ``steer_actions``) for one task.

    S4 deleted the derived ``agent_topology`` copy from the snapshot frame. The
    steering executor (``steering.execute_steer_action``) was the one live
    reader of its ``steer_actions`` — it validated an operator's requested steer
    against the frame's copy. Rather than ship that derived copy on EVERY frame
    for a reader that fires only on an operator action, the executor re-derives
    it on demand here from the stores, so there is still exactly ONE producer
    (``_agent_topology``) and no per-frame duplicate. Not a hot path (one steer
    action), so a per-call store read is acceptable.
    """

    event_log = CachedEventLog()
    cfg = load_agent_runtime_config()
    all_runs = RunStore().list_all()
    runs = [run for run in all_runs if run.task_id == task.id]
    active_runs = [run for run in runs if run.state in ACTIVE_RUN_STATES]
    all_workers = WorkerSessionStore(event_log=event_log).list_all()
    workers = [w for w in all_workers if getattr(w, "task_id", None) == task.id]
    active_workers = [
        w
        for w in workers
        if str(getattr(getattr(w, "state", ""), "value", getattr(w, "state", "")))
        not in {"completed", "blocked", "closed"}
    ]
    runtime_instances = [
        item
        for item in GoalRuntimeInstanceStore(event_log=event_log).list_all()
        if item.task_id == task.id
    ]
    agents = AgentStore().list_all() or seed_personas()
    if persona_instance_runtime_enabled(cfg):
        persona_instances = PersonaInstanceStore(event_log=event_log).derive_from_workers(agents, all_workers)
    else:
        persona_instances = PersonaInstanceStore(event_log=event_log).list_all()
    persona_assignments = (
        [
            item
            for item in PersonaAssignmentStore(event_log=event_log).list_all()
            if item.task_id == task.id
        ]
        if persona_assignment_store_enabled(cfg)
        else []
    )
    role_envelopes = [
        item for item in RoleEnvelopeStore(event_log=event_log).list_all() if item.task_id == task.id
    ]
    role_checklists = [
        item for item in RoleChecklistStore(event_log=event_log).list_all() if item.task_id == task.id
    ]
    proof_batches = [
        item for item in ProofBatchStore(event_log=event_log).list_all() if item.task_id == task.id
    ]
    role_streams = _role_streams(
        task,
        event_log.for_task(task.id, limit=200),
        runs,
        workers,
        persona_assignments=persona_assignments,
        role_envelopes=role_envelopes,
        role_checklists=role_checklists,
        proof_batches=proof_batches,
    )
    return _agent_topology(
        task,
        active_runs=active_runs,
        active_workers=active_workers,
        runtime_instances=runtime_instances,
        persona_instances=persona_instances,
        role_streams=role_streams,
    )


def _agent_topology(task, *, active_runs, active_workers, runtime_instances, persona_instances, role_streams) -> dict:
    plan = getattr(task, "mission_plan", None)
    stages = list(getattr(plan, "stages", []) or [])
    topology = getattr(plan, "agent_topology", None) if plan is not None else None
    topology = topology if isinstance(topology, dict) else {}
    plan_slots = dict(getattr(plan, "slots", {}) or {}) if plan is not None else {}
    bindings = dict(getattr(plan, "bindings", {}) or {}) if plan is not None else {}
    topology_edges = [
        edge
        for edge in topology.get("edges", []) or []
        if isinstance(edge, dict)
    ]
    root_slot = str(topology.get("root") or "").strip()
    if not root_slot and topology_edges:
        root_slot = str(topology_edges[0].get("source") or "").strip()
    if not root_slot and len(plan_slots) == 1:
        root_slot = next(iter(plan_slots))

    task_goal_id = str(getattr(task, "goal_id", None) or task.id)
    runtime_instances = list(runtime_instances or [])
    persona_instances = list(persona_instances or [])
    related_instances = [
        instance
        for instance in [*runtime_instances, *persona_instances]
        if _instance_matches_task(instance, task, task_goal_id)
    ]
    # --- Steering closure -------------------------------------------------
    # The runtime graph the operator edits lives in persona-instance steering
    # (``steered_by``, keyed by instance id). Seeding topology nodes only from
    # goal-matched instances + plan slots drops any node the operator steered IN
    # whose goal does not match the task — e.g. a fan-in convergence agent on a
    # goal-less default flow. Its node (and every edge into it) is omitted, so
    # the Launcher reprojects the operator's wiring away ("connected + saved,
    # gone on refresh"). Pull the steered graph in: anchor on the goal-matched
    # set plus the lead instance bound to the plan root, then transitively add
    # any instance steered by a member. This mirrors the Launcher's own
    # related-instance expansion and stays BOUNDED to the mission — only steered
    # descendants of the seed, never the global persona pool.
    _all_instances = [*runtime_instances, *persona_instances]
    _seed_ids = {
        sid
        for sid in (str(getattr(i, "id", "") or "").strip() for i in related_instances)
        if sid
    }
    if root_slot:
        _root_persona = _topology_slot_persona_id(root_slot, bindings)
        _by_persona_all: dict[str, list] = {}
        for _inst in _all_instances:
            _pid = str(getattr(_inst, "persona_id", "") or "").strip()
            if _pid:
                _by_persona_all.setdefault(_pid, []).append(_inst)
        _root_inst = _instance_for_persona(_root_persona, _by_persona_all)
        _root_id = str(getattr(_root_inst, "id", "") or "").strip() if _root_inst else ""
        if _root_id and _root_id not in _seed_ids:
            related_instances.append(_root_inst)
            _seed_ids.add(_root_id)
    _closure_changed = True
    while _closure_changed:
        _closure_changed = False
        for _inst in _all_instances:
            _iid = str(getattr(_inst, "id", "") or "").strip()
            if not _iid or _iid in _seed_ids:
                continue
            if any(ref in _seed_ids for ref in _instance_parent_refs(_inst)):
                related_instances.append(_inst)
                _seed_ids.add(_iid)
                _closure_changed = True
    instances_by_id = {str(getattr(instance, "id", "") or ""): instance for instance in related_instances}
    instances_by_persona: dict[str, list] = {}
    for instance in related_instances:
        persona_id = str(getattr(instance, "persona_id", "") or "").strip()
        if persona_id:
            instances_by_persona.setdefault(persona_id, []).append(instance)

    stages_by_slot: dict[str, list] = {}
    stages_by_persona: dict[str, list] = {}
    for stage in stages:
        slot = str(getattr(stage, "owner_slot", None) or getattr(stage, "owner", "") or "").strip()
        owner = str(getattr(stage, "owner", "") or "").strip()
        if slot:
            stages_by_slot.setdefault(slot, []).append(stage)
        if owner:
            stages_by_persona.setdefault(owner, []).append(stage)

    stream_by_id = {stream.get("persona_id"): stream for stream in role_streams if isinstance(stream, dict)}
    nodes_by_id: dict[str, dict] = {}
    slot_node_ids: dict[str, str] = {}
    # Canonical node per persona. A persona that runs N turns (N re-instantiated
    # persona_instances) is ONE agent, not N nodes — without this the topology
    # grows O(runs/spawns) and floods the graph with duplicate "Backend Dev Agent"
    # / "Launcher Dev Agent" nodes. Multi-turn instances collapse onto the canonical
    # node; only a genuinely distinct persona (a spawned sub-agent of a new role)
    # gets its own node.
    persona_node_id: dict[str, str] = {}
    collapsed_instances = 0

    def add_node_for_slot(slot: str) -> str | None:
        slot = str(slot or "").strip()
        if not slot:
            return None
        persona_id = _topology_slot_persona_id(slot, bindings)
        instance = _instance_for_persona(persona_id, instances_by_persona)
        owned_stages = stages_by_slot.get(slot) or stages_by_persona.get(persona_id) or []
        node_id = str(getattr(instance, "id", "") or "").strip() or f"slot_{slot}"
        slot_node_ids[slot] = node_id
        nodes_by_id[node_id] = _agent_topology_node(
            node_id=node_id,
            persona_id=persona_id or slot,
            instance=instance,
            owned_stages=owned_stages,
            active_runs=active_runs,
            active_workers=active_workers,
            stream=stream_by_id.get(persona_id) or {},
            fallback_display=_display_name_for_persona(persona_id or slot),
            fallback_role=_actor_role_for_persona(persona_id or slot),
        )
        if persona_id:
            persona_node_id.setdefault(persona_id, node_id)
        return node_id

    topology_slots: list[str] = []
    if root_slot:
        topology_slots.append(root_slot)
    for edge in topology_edges:
        for key in ("source", "target"):
            slot = str(edge.get(key) or "").strip()
            if slot and slot not in topology_slots:
                topology_slots.append(slot)
    if not topology_slots and plan_slots:
        topology_slots.extend(plan_slots.keys())
    for slot in topology_slots:
        add_node_for_slot(slot)

    for instance in related_instances:
        instance_id = str(getattr(instance, "id", "") or "").strip()
        if not instance_id or instance_id in nodes_by_id:
            continue
        persona_id = str(getattr(instance, "persona_id", "") or "").strip()
        # Collapse a re-instantiated persona onto its canonical node instead of
        # minting a duplicate node per turn. Only a persona that has no node yet
        # (a genuinely distinct spawned sub-agent) earns its own node.
        if persona_id and persona_id in persona_node_id:
            collapsed_instances += 1
            continue
        owned_stages = stages_by_persona.get(persona_id) or []
        nodes_by_id[instance_id] = _agent_topology_node(
            node_id=instance_id,
            persona_id=persona_id,
            instance=instance,
            owned_stages=owned_stages,
            active_runs=active_runs,
            active_workers=active_workers,
            stream=stream_by_id.get(persona_id) or {},
            fallback_display=_display_name_for_persona(persona_id),
            fallback_role=_actor_role_for_persona(persona_id),
        )
        if persona_id:
            persona_node_id[persona_id] = instance_id

    def _canonical_node_for(instance) -> str:
        pid = str(getattr(instance, "persona_id", "") or "").strip()
        if pid and pid in persona_node_id:
            return persona_node_id[pid]
        return str(getattr(instance, "id", "") or "").strip()

    edges: list[dict] = []
    targets_with_runtime_parent: set[str] = set()
    fan_in_targets: set[str] = set()
    seen_spawn_edges: set[tuple[str, str]] = set()
    for instance in related_instances:
        # Multi-parent fan-in (Stage 77): a child can be steered by ≥1 parents.
        # Iterate the authoritative `steered_by` set (falling back to the legacy
        # scalar `spawned_by` for un-migrated records) and emit one edge per
        # distinct parent node.
        for parent_ref in _instance_parent_refs(instance):
            parent = _topology_parent_instance(parent_ref, related_instances)
            if parent is None:
                continue
            # Map lineage onto canonical persona nodes so collapsed multi-turn
            # instances don't drop the spawn edge; dedupe and skip self-edges (a
            # persona spawning another turn of itself is not a distinct steer).
            target_node = _canonical_node_for(instance)
            parent_node = _canonical_node_for(parent)
            if not target_node or not parent_node or parent_node == target_node:
                continue
            if target_node not in nodes_by_id or parent_node not in nodes_by_id:
                continue
            key = (parent_node, target_node)
            if key in seen_spawn_edges:
                continue
            seen_spawn_edges.add(key)
            if target_node in targets_with_runtime_parent:
                # A second (or later) distinct runtime parent for this node =
                # true fan-in.
                fan_in_targets.add(target_node)
            targets_with_runtime_parent.add(target_node)
            edges.append(
                {
                    "source_node_id": parent_node,
                    "target_node_id": target_node,
                    "kind": "steers",
                    "source": "runtime_spawned_by",
                }
            )

    if topology_edges:
        for edge in topology_edges:
            source_id = slot_node_ids.get(str(edge.get("source") or "").strip())
            target_id = slot_node_ids.get(str(edge.get("target") or "").strip())
            if not source_id or not target_id or target_id in targets_with_runtime_parent:
                continue
            edges.append(
                {
                    "source_node_id": source_id,
                    "target_node_id": target_id,
                    "kind": str(edge.get("kind") or "steers"),
                    "source": "blueprint_agent_topology",
                }
            )

    root_node_id = slot_node_ids.get(root_slot) if root_slot else None
    if root_node_id is None and nodes_by_id:
        child_ids = {edge["target_node_id"] for edge in edges}
        root_node_id = next((node_id for node_id in nodes_by_id if node_id not in child_ids), next(iter(nodes_by_id)))
    control_node_id = _topology_control_node_id(task, stages, slot_node_ids, root_node_id)
    steer_actions, steer_action_drops = build_steer_actions(nodes_by_id, edges, control_node_id=control_node_id, task=task)
    drop_samples: list[dict] = []
    for node in nodes_by_id.values():
        node_drops = node.pop("_drops", [])
        if isinstance(node_drops, list):
            drop_samples.extend(item for item in node_drops if isinstance(item, dict))
    drop_samples.extend(steer_action_drops)
    return {
        "schema_version": 1,
        "source": "runtime_spawned_by" if targets_with_runtime_parent else ("blueprint_agent_topology" if topology_edges or root_slot else "persona_instances"),
        "root_node_id": root_node_id,
        "control_node_id": control_node_id,
        "nodes": list(nodes_by_id.values()),
        "edges": edges,
        "steer_actions": steer_actions,
        "completeness": {
            "node_count": len(nodes_by_id),
            "edge_count": len(edges),
            "fan_in_targets": len(fan_in_targets),
            "collapsed_multi_turn_instances": collapsed_instances,
            "stream_event_cap_per_node": 3,
            "id_cap_per_node": AGENT_TOPOLOGY_NODE_ID_CAP,
            "drops": drop_samples[:50],
        },
    }


def _agent_topology_node(*, node_id: str, persona_id: str, instance, owned_stages, active_runs, active_workers, stream, fallback_display: str, fallback_role: str) -> dict:
    runs = [run for run in active_runs if getattr(run, "persona_id", None) == persona_id]
    workers = [worker for worker in active_workers if getattr(worker, "persona_id", None) == persona_id]
    active_stage = next((stage for stage in owned_stages if getattr(stage, "status", None) in {StageStatus.IMPLEMENTING, StageStatus.READY}), None)
    stage = active_stage or (owned_stages[0] if owned_stages else None)
    stage_ids, stage_drops = _bounded_topology_ids(
        [getattr(stage, "id", None) for stage in owned_stages if getattr(stage, "id", None)],
        node_id=node_id,
        field="stage_ids",
    )
    run_ids, run_drops = _bounded_topology_ids([run.id for run in runs], node_id=node_id, field="active_run_ids")
    worker_ids, worker_drops = _bounded_topology_ids([worker.id for worker in workers], node_id=node_id, field="active_worker_session_ids")
    return {
        "node_id": node_id,
        "persona_id": persona_id,
        "persona_instance_id": str(getattr(instance, "id", "") or "").strip() or None,
        "display_name": str(getattr(instance, "display_name", "") or "").strip() or fallback_display,
        "role": str(getattr(instance, "role", "") or "").strip() or fallback_role,
        "stage_ids": stage_ids,
        "presence": _actor_presence(active_stage, owned_stages, runs, workers),
        "state_label": _actor_state_label(active_stage, owned_stages, runs, workers),
        "active_run_ids": run_ids,
        "active_worker_session_ids": worker_ids,
        "budget": _actor_budget_summary(runs, stream),
        "current_stage_id": getattr(stage, "id", None),
        "spawned_by": str(getattr(instance, "spawned_by", "") or "").strip() or None,
        "steered_by": _instance_parent_refs(instance),
        "returned_to": str(getattr(instance, "returned_to", "") or "").strip() or None,
        "stream_event_count": len(stream.get("events") or []) if isinstance(stream, dict) else 0,
        "progress_peek": _progress_peek(stream),
        "_drops": [*stage_drops, *run_drops, *worker_drops],
    }


def _bounded_topology_ids(values: list, *, node_id: str, field: str) -> tuple[list[str], list[dict]]:
    clean = [str(value).strip() for value in values if str(value or "").strip()]
    if len(clean) <= AGENT_TOPOLOGY_NODE_ID_CAP:
        return clean, []
    return clean[:AGENT_TOPOLOGY_NODE_ID_CAP], [
        {
            "node_id": node_id,
            "field": field,
            "kept": AGENT_TOPOLOGY_NODE_ID_CAP,
            "dropped": len(clean) - AGENT_TOPOLOGY_NODE_ID_CAP,
            "reason": "topology_node_id_cap",
        }
    ]


def _progress_peek(stream: dict) -> list[dict]:
    events = stream.get("events") if isinstance(stream, dict) else None
    if not isinstance(events, list):
        return []
    peek: list[dict] = []
    for event in events[-3:]:
        payload = event.get("payload") if isinstance(event, dict) else None
        payload = payload if isinstance(payload, dict) else {}
        summary = str(payload.get("display_summary") or payload.get("summary") or "").strip()
        if not summary:
            continue
        peek.append(
            {
                "type": str(event.get("type") or "")[:80],
                "run_id": str(event.get("run_id") or "")[:128] or None,
                "summary": summary[:300],
                "status": str(payload.get("status") or "")[:40] or None,
            }
        )
    return peek


def _topology_control_node_id(task, stages: list, slot_node_ids: dict[str, str], root_node_id: str | None) -> str | None:
    current_id = str(getattr(getattr(task, "mission_plan", None), "current_stage_id", None) or getattr(task, "current_stage_id", "") or "").strip()
    stage = next((item for item in stages if str(getattr(item, "id", "") or "") == current_id), None)
    if stage is not None:
        slot = str(getattr(stage, "owner_slot", "") or getattr(stage, "owner", "") or "").strip()
        if slot and slot_node_ids.get(slot):
            return slot_node_ids[slot]
    return root_node_id


def _instance_matches_task(instance, task, task_goal_id: str) -> bool:
    return (
        str(getattr(instance, "goal_id", "") or "") == task_goal_id
        or str(getattr(instance, "current_task_id", "") or "") == task.id
        or str(getattr(instance, "task_id", "") or "") == task.id
    )


def _topology_slot_persona_id(slot: str, bindings: dict) -> str:
    bound = str(bindings.get(slot) or slot).strip()
    if bound.startswith("persona:") or bound.startswith("profile:"):
        return bound.split(":", 1)[1].strip()
    return bound


def _instance_for_persona(persona_id: str, instances_by_persona: dict[str, list]):
    candidates = instances_by_persona.get(persona_id) or []
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: str(getattr(item, "updated_at", "") or ""), reverse=True)[0]


def _instance_parent_refs(instance) -> list[str]:
    """The child's steering-parent refs, preferring the authoritative
    ``steered_by`` set and falling back to the legacy scalar ``spawned_by`` for
    un-migrated records. De-duplicated, order-preserving (first = primary)."""
    raw = list(getattr(instance, "steered_by", []) or [])
    if not raw:
        scalar = getattr(instance, "spawned_by", None)
        raw = [scalar] if scalar else []
    refs: list[str] = []
    seen: set[str] = set()
    for value in raw:
        ref = str(value or "").strip()
        if ref and ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs


def _topology_parent_instance(parent_ref: str, instances: list):
    if not parent_ref:
        return None
    for instance in instances:
        if str(getattr(instance, "id", "") or "") == parent_ref:
            return instance
    for instance in instances:
        if str(getattr(instance, "persona_id", "") or "") == parent_ref or str(getattr(instance, "role", "") or "") == parent_ref:
            return instance
    return None


def _mission_flow_timeline(task, events, *, accountant: ProjectionAccountant | None = None) -> dict:
    plan = getattr(task, "mission_plan", None)
    stages = list(getattr(plan, "stages", []) or [])
    items = []
    for stage in stages:
        status = getattr(getattr(stage, "status", None), "value", getattr(stage, "status", None))
        items.append(
            {
                "id": f"stage:{stage.id}",
                "kind": "stage",
                "stage_id": stage.id,
                "title": stage.title,
                "owner": getattr(stage, "owner", None),
                "owner_slot": getattr(stage, "owner_slot", None),
                "status": status,
                "proof_ids": list(getattr(stage, "proof_ids", []) or []),
                "depends_on": list(getattr(stage, "depends_on", []) or []),
            }
        )
    for event in [event for event in _coalesced_progress_events(events) if event.task_id == task.id][-20:]:
        display = _event_display_projection(event)
        items.append(
            {
                "id": f"event:{event.type}:{event.run_id or 'task'}:{event.ts}",
                "kind": display.get("display_kind") or "event",
                "ts": event.ts,
                "type": event.type,
                "persona_id": event.persona_id,
                "run_id": event.run_id,
                "summary": display.get("display_summary"),
                "redaction_status": display.get("redaction_status") or "safe",
                "artifact_refs": display.get("artifact_refs") or [],
            }
        )
    total = len(items)
    if accountant is not None:
        accountant.consider(total)
    # Keep the office-rendered FRONT window (office takes ``items.take(5)``; the
    # cap keeps the front MISSION_FLOW_TIMELINE_ITEM_CAP so that render is
    # byte-identical) and account the evicted tail with a fetch pointer.
    selected = items[:MISSION_FLOW_TIMELINE_ITEM_CAP]
    evicted = total - len(selected)
    if accountant is not None:
        accountant.include(len(selected))
        if evicted:
            # Deliberate bound: the front window is kept and the evicted tail is
            # disclosed with a typed fetch pointer below.
            accountant.drop(
                "flow_item_cap",
                count=evicted,
                entity_id=getattr(task, "id", None),
                detail=f"kept front {MISSION_FLOW_TIMELINE_ITEM_CAP} flow items",
                by_design=True,
            )
            accountant.mark_truncated()
    result = {
        "schema_version": 1,
        "mission_id": task.id,
        "active_stage_id": getattr(plan, "current_stage_id", None) if plan else getattr(task, "current_stage_id", None),
        "items": selected,
        "items_total": total,
        "items_evicted": evicted,
    }
    if evicted:
        # Honest accounting: a typed pointer to the full paged history, never a
        # silent truncation (the full timeline is rebuilt from the EventLog).
        result["items_ref"] = {
            "evicted": True,
            "count": evicted,
            "fetch": "harness goal history <task_id> --json",
        }
    return result


def _proof_gate_state(task, proofs) -> dict:
    proof_by_id = {proof.id: proof for proof in proofs}
    plan = getattr(task, "mission_plan", None)
    stages = list(getattr(plan, "stages", []) or [])
    requirements = []
    missing = []
    captured = []
    required_statuses: list[str] = []
    for stage in stages:
        gate = getattr(stage, "proof_gate", {}) if isinstance(getattr(stage, "proof_gate", None), dict) else {}
        required_types = [str(item) for item in gate.get("required_proof_types") or []]
        required = bool(gate.get("required") or required_types or gate.get("proof_recipe_id") or gate.get("commands"))
        stage_proofs = [proof_by_id[item] for item in getattr(stage, "proof_ids", []) or [] if item in proof_by_id]
        safe_refs = [_proof_evidence_ref(proof) for proof in stage_proofs if _proof_evidence_ref(proof)]
        captured.extend(safe_refs)
        status = "not_applicable"
        if required:
            status = _proof_requirement_status(stage, stage_proofs)
            required_statuses.append(status)
        if status == "missing":
            missing.append(stage.id)
        requirements.append(
            {
                "stage_id": stage.id,
                "title": stage.title,
                "required": required,
                "required_proof_types": required_types,
                "minimum_status": gate.get("minimum_status") or "passed",
                "status": status,
                "evidence_refs": safe_refs,
                "redaction_status": "safe",
            }
        )
    gate_state = _roll_up_gate_state(required_statuses, waived=bool(getattr(task, "waiver", None)))
    why_not_ready = [
        f"{req['stage_id']} {req['status']}"
        for req in requirements
        if req["required"] and req["status"] not in {"passed", "not_applicable"}
    ]
    return {
        "schema_version": 1,
        "mission_id": task.id,
        "gate_state": gate_state,
        "requirements": requirements,
        "captured_evidence": captured,
        "missing_stage_ids": missing,
        "why_not_ready": why_not_ready,
        "waiver": getattr(task, "waiver", None),
        "updated_at": getattr(task, "updated_at", None),
    }


# Roles whose proof verdict can satisfy a required gate. Worker self-reports
# (dev/backend_dev) are evidence candidates only — never a passing verdict —
# until a verifier/reviewer/Neko proof review records the result.
_PROOF_VERIFIER_ROLES = frozenset({"qa", "verifier", "reviewer", "neko_supervisor"})


def _roll_up_gate_state(required_statuses: list[str], *, waived: bool) -> str:
    """Roll per-requirement statuses into a spec gate_state value.

    Allowed: not_required | incomplete | running | blocked | failed | passed | waived.
    """

    if not required_statuses:
        return "not_required"
    if any(status == "failed" for status in required_statuses):
        return "failed"
    if any(status == "blocked_external" for status in required_statuses):
        return "blocked"
    if waived and all(status in {"passed", "waived_by_operator", "not_applicable"} for status in required_statuses):
        return "waived"
    if any(status == "missing" for status in required_statuses):
        return "incomplete"
    if any(status == "running" for status in required_statuses):
        return "running"
    if all(status in {"passed", "not_applicable"} for status in required_statuses):
        return "passed"
    return "incomplete"


def _operator_capabilities(task, next_action, gate) -> dict:
    terminal = task.state in {TaskState.DONE, TaskState.CANCELLED, TaskState.FAILED}
    return {
        "schema_version": 1,
        "mission_id": task.id,
        "actions": {
            "goal_create": {"enabled": True, "disabled_reason": None},
            "cancel_mission": {"enabled": not terminal, "disabled_reason": "mission is terminal" if terminal else None},
            "retry_stage": {"enabled": bool((next_action or {}).get("action") in {"run_slot", "blocked_by_incident"}), "disabled_reason": None},
            "waive_proof": {"enabled": bool(gate.missing), "disabled_reason": None if gate.missing else "proof gate has no missing requirements"},
            "answer_intervention": {"enabled": (next_action or {}).get("action") == "blocked_by_incident", "disabled_reason": None},
            "raw_log": {"enabled": False, "disabled_reason": "raw logs require explicit redaction-aware debug path"},
        },
    }


def _actor_presence(active_stage, owned_stages, runs, workers) -> str:
    if runs or workers:
        return "active"
    if active_stage is not None:
        return "waiting"
    if owned_stages and all(getattr(stage, "status", None) == StageStatus.PASSED for stage in owned_stages):
        return "complete"
    if owned_stages:
        return "queued"
    return "unknown"


def _actor_state_label(active_stage, owned_stages, runs, workers) -> str:
    if runs:
        return "Running"
    if workers:
        return "Assigned"
    stage = active_stage or (owned_stages[0] if owned_stages else None)
    if stage is None:
        return "Unknown"
    status = getattr(getattr(stage, "status", None), "value", getattr(stage, "status", None))
    return str(status or "waiting").replace("_", " ").title()


def _actor_role_for_persona(persona_id: str) -> str:
    return {
        "neko_supervisor": "neko_supervisor",
        "backend_dev": "backend_dev",
        "dev": "dev",
        "qa": "qa",
    }.get(persona_id, "specialist")


def _latest_actor_event(persona_id: str, events) -> dict | None:
    for event in reversed(events):
        if event.persona_id != persona_id:
            continue
        display = _event_display_projection(event)
        return {
            "ts": event.ts,
            "type": event.type,
            "run_id": event.run_id,
            "summary": display.get("display_summary"),
            "redaction_status": display.get("redaction_status") or "safe",
            "artifact_refs": display.get("artifact_refs") or [],
        }
    return None


def _actor_budget_summary(runs, role_stream) -> dict:
    token_total = 0
    for run in runs:
        llm = getattr(run, "llm", None) if isinstance(getattr(run, "llm", None), dict) else {}
        value = llm.get("total_tokens")
        if isinstance(value, int):
            token_total += value
    return {
        "active_run_count": len(runs),
        "token_budget_used": token_total or None,
        "proof_batch_count": len((role_stream or {}).get("proof_batches") or []) if isinstance(role_stream, dict) else 0,
    }


def _proof_requirement_status(stage, proofs) -> str:
    """Resolve a required proof requirement to a spec proof status.

    Returns one of: passed | failed | blocked_external | running | missing.
    A worker self-report is treated as in-progress evidence (``running``),
    never ``passed`` — only a verifier/reviewer/Neko verdict (or the gate
    machinery flipping the stage to PASSED) can pass the requirement.
    """

    if getattr(stage, "status", None) == StageStatus.PASSED:
        return "passed"
    if getattr(stage, "status", None) == StageStatus.BLOCKED:
        return "blocked_external"
    saw_evidence = False
    for proof in proofs:
        if getattr(proof, "redaction_status", None) not in {"safe", "redacted", None}:
            continue
        saw_evidence = True
        metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
        status = str(metadata.get("status") or metadata.get("verdict") or "").lower()
        author = str(getattr(proof, "created_by", "") or "").lower()
        author_role = author.split(":", 1)[0]
        is_verifier = author_role in _PROOF_VERIFIER_ROLES or author in _PROOF_VERIFIER_ROLES
        if status in {"failed", "rejected", "needs_fixes"}:
            return "failed"
        if status == "blocked_external":
            return "blocked_external"
        if status in {"passed", "approved"} and is_verifier:
            return "passed"
    return "running" if saw_evidence else "missing"


def _proof_evidence_ref(proof) -> dict | None:
    if not getattr(proof, "id", None):
        return None
    redaction_status = getattr(proof, "redaction_status", None) or "unknown"
    if redaction_status not in {"safe", "redacted", "unknown", "pending"}:
        return None
    return {
        "evidence_id": proof.id,
        "kind": str(getattr(proof, "type", "proof")),
        "uri": f"artifact://proof/{proof.task_id}/{proof.id}",
        "redaction_status": redaction_status,
    }


def _runtime_lane_summary(runtime_instances) -> dict:
    if not runtime_instances:
        return {"lane": None, "state": None, "runtime_instance_id": None}
    latest = sorted(runtime_instances, key=lambda item: (item.updated_at, item.id))[-1]
    return {
        "lane": latest.lane,
        "state": latest.state,
        "runtime_instance_id": latest.id,
        "parked_reason": latest.parked_reason,
    }


def _persona_streams(task, assignments, runs, events) -> dict:
    streams: dict[str, dict] = {}
    role_ids = ["neko_supervisor", "backend_dev", "dev", "qa"]
    for assignment in assignments or []:
        role_ids.append(str(getattr(assignment, "persona_id", "") or ""))
    for run in runs or []:
        if getattr(run, "task_id", None) == task.id:
            role_ids.append(str(getattr(run, "persona_id", "") or ""))
    for event in events or []:
        if event.task_id == task.id and event.persona_id:
            role_ids.append(str(event.persona_id))
    for role_id in sorted({role for role in role_ids if role}):
        role_assignments = [assignment for assignment in assignments or [] if assignment.task_id == task.id and assignment.persona_id == role_id]
        role_runs = [run for run in runs or [] if getattr(run, "task_id", None) == task.id and getattr(run, "persona_id", None) == role_id]
        role_events = [event for event in events or [] if event.task_id == task.id and event.persona_id == role_id]
        streams[role_id] = {
            "persona_id": role_id,
            "assignment_ids": [assignment.id for assignment in role_assignments],
            "active_assignment_ids": [assignment.id for assignment in role_assignments if assignment.state in ACTIVE_ASSIGNMENT_STATES],
            "run_ids": [run.id for run in role_runs],
            "active_run_ids": [run.id for run in role_runs if run.state in ACTIVE_RUN_STATES],
            "event_count": len(role_events),
            "latest_event_type": role_events[-1].type if role_events else None,
        }
    return streams


def _execution_status(task, active_runs, open_incidents) -> str:
    if task.state in {TaskState.DONE, TaskState.CANCELLED}:
        return "complete" if task.state == TaskState.DONE else "cancelled"
    if open_incidents:
        return "blocked"
    if not active_runs:
        return "idle"
    priority = [RunState.RUNNING, RunState.WAITING_ON_TOOL, RunState.WAITING_ON_APPROVAL, RunState.STARTING, RunState.QUEUED]
    states = {run.state for run in active_runs}
    for state in priority:
        if state in states:
            return state.value
    return "unknown"


def _can_start_run(task, active_runs, open_incidents) -> bool:
    if task.state in {TaskState.DONE, TaskState.CANCELLED}:
        return False
    return not active_runs and not open_incidents


def _run_blocked_reason(task, active_runs, open_incidents, *, run_store=None) -> str | None:
    if task.state in {TaskState.DONE, TaskState.CANCELLED}:
        return "mission is terminal"
    if open_incidents:
        runs = run_store or RunStore()
        cfg = load_root_runtime_config()
        if any(budget_incident_needs_scope_recovery(incident, runs) for incident in open_incidents):
            return "Dev exhausted read/search without patch or proof; Neko must split or narrow the stage before retry"
        if any(budget_incident_can_continue(incident, runs, cap=cfg.neko_extension_cap) for incident in open_incidents):
            return "budget continuation requires Neko approval"
        if _has_budget_incident(open_incidents):
            return "budget continuation blocked; human review required"
        return "open incident requires human review"
    if active_runs:
        return "mission already has active agent work"
    return None


def _next_action_summary(task, open_incidents, *, run_store=None, event_log=None):
    if open_incidents:
        runs = run_store or RunStore()
        cfg = load_root_runtime_config()
        if any(budget_incident_needs_scope_recovery(incident, runs) for incident in open_incidents):
            return {**_stopped_progress(task, open_incidents, "self_heal_pending", "neko_supervisor"), "action": "run_slot", "reason": "Dev exhausted read/search without patch or proof; Neko must split or narrow the stage before retry"}
        if any(budget_incident_can_continue(incident, runs, cap=cfg.neko_extension_cap) for incident in open_incidents):
            return {**_stopped_progress(task, open_incidents, "self_heal_pending", "neko_supervisor"), "action": "run_slot", "reason": "needs Neko approval to continue budget-limited Dev run"}
        reason = "budget_continuation_blocked" if _has_budget_incident(open_incidents) else "environment_blocked"
        message = "budget continuation cap reached; human review required" if reason == "budget_continuation_blocked" else "open incident requires human review"
        return {**_stopped_progress(task, open_incidents, reason, "human"), "action": "blocked_by_incident", "reason": message}
    if task.state in {TaskState.DONE, TaskState.CANCELLED}:
        return {**_stopped_progress(task, [], "settled", "harness"), "action": "terminal", "reason": "mission is terminal"}
    cfg = load_root_runtime_config()
    try:
        action = MissionStateMachine(config=cfg, event_log=event_log).next_action(task)
    except LegacyOrchestratorRemoved as exc:
        # Stage 15.4 read-surface contract, the same ruling `status.py` carries.
        # The DISPATCH path (ticker, goal runner, blueprint create) must keep
        # raising this — the typed refusal is what replaced the retired legacy
        # orchestrator's silent guess, and softening it there would resurrect the
        # failure mode the retirement removed. `build_snapshot` is a pure READ
        # surface and is the frame Mission Control renders: letting the exception
        # escape here would turn ONE undispatchable mission into a blank snapshot
        # for EVERY mission, destroying the operator's ability to diagnose the
        # very task that is broken. So report, do not swallow and do not guess —
        # the refusal becomes a typed entry with its own `action` value (never
        # confusable with a dispatchable one) plus the redaction-safe routing
        # facts. Only the typed error is caught; a bare `except Exception` here
        # would hide unrelated faults behind a routing verdict.
        progress = _stopped_progress(task, [], "routing_undispatchable", "human")
        # `tick` is the wrong advice here — ticking this mission raises again.
        progress["stopped_progress"]["next_action"] = "inspect_blocker"
        # No `task_id`: unlike a `status.py` `next_actions` entry, this dict is
        # nested under the goal row, which already carries the id.
        return {**progress, "action": "undispatchable", "reason": str(exc), "routing_failure": exc.read_surface_envelope()}
    reason = "settled" if action.type.value == "noop" else "retry_authorized" if "retry" in action.reason else "self_heal_pending" if "Neko" in action.reason else "waiting_for_preflight"
    return {**_stopped_progress(task, [], reason, _owner_for_action(action, task=task, run_store=run_store)), "action": action.type.value, "reason": action.reason}


def _why_not_done(task, gate, open_incidents, next_action):
    if task.state == TaskState.DONE:
        return []
    reasons = []
    for incident in open_incidents:
        reasons.append({"kind": "open_incident", "incident_id": incident.id, "incident_kind": incident.kind})
    for missing in gate.missing:
        reasons.append({"kind": "missing_proof", "requirement": str(missing)})
    if not reasons:
        reasons.append({"kind": "waiting_on_action", "action": next_action.get("action"), "reason": next_action.get("reason")})
    return reasons


def _stopped_progress(task, incidents, reason: str, owner: str) -> dict:
    incident = incidents[0] if incidents else None
    metadata = getattr(incident, "metadata", None) if incident else None
    metadata = metadata if isinstance(metadata, dict) else {}
    proof_ids = []
    if metadata.get("proof_id"):
        proof_ids.append(metadata["proof_id"])
    return {
        "stopped_progress": {
            "reason": reason,
            "owner": owner,
            "stage_id": getattr(task, "current_stage_id", None),
            "blocking_event_id": metadata.get("blocking_event_id") or (incident.id if incident else None),
            "related_proof_ids": proof_ids or list(getattr(task, "proof_ids", []) or [])[-3:],
            "next_action": "inspect_blocker" if incident else "tick",
        }
    }


def _owner_for_action(action, *, task=None, run_store=None) -> str:
    action_value = action.type.value if hasattr(getattr(action, "type", None), "value") else str(action)
    slot_id = str(getattr(action, "slot_id", "") or "").strip()
    if action_value == "run_slot" and slot_id == "dev" and task is not None:
        return "dev"
    if action_value == "run_slot" and slot_id:
        return slot_id
    return {
        "run_dev": "dev",
        "run_qa": "qa",
        "run_neko_supervisor": "neko_supervisor",
        "complete_task": "harness",
        "noop": "harness",
    }.get(action_value, "harness")


def _has_budget_incident(incidents) -> bool:
    return any(getattr(incident, "kind", None) == "run_budget_exceeded" for incident in incidents)


def _proof_visibility_summary(proof):
    metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
    return {
        "proof_id": proof.id,
        "type": str(proof.type),
        "status": metadata.get("status") or metadata.get("verdict"),
        "exit_code": metadata.get("exit_code"),
        "duration_ms": metadata.get("duration_ms"),
        "created_by": proof.created_by,
        "has_artifact": bool(proof.path_or_value),
    }


def _task_timeline(task_id, events):
    return [
        {
            "ts": event.ts,
            "type": event.type,
            "run_id": event.run_id,
            "persona_id": event.persona_id,
            **_event_display_projection(event),
        }
        for event in _coalesced_progress_events(events)
        if event.task_id == task_id
    ][-20:]


def _coalesced_progress_events(events):
    items = []
    index_by_progress_id: dict[str, int] = {}
    for event in events or []:
        payload = event.payload if isinstance(getattr(event, "payload", None), dict) else {}
        event_id = str(payload.get("event_id") or "").strip()
        if event.type == "run.progress" and event_id:
            existing = index_by_progress_id.get(event_id)
            if existing is not None:
                items[existing] = event
                continue
            index_by_progress_id[event_id] = len(items)
        items.append(event)
    return items


def _role_streams(task, events, runs, workers, *, persona_assignments=None, role_envelopes=None, role_checklists=None, proof_batches=None) -> list[dict]:
    plan = getattr(task, "mission_plan", None)
    role_ids = ["neko_supervisor", "backend_dev", "dev", "qa"]
    if plan is not None:
        for stage in getattr(plan, "stages", []) or []:
            owner = str(getattr(stage, "owner", "") or "")
            if owner == "dev":
                role_ids.append("dev")
            elif owner in {"backend_dev", "qa", "neko_supervisor"}:
                role_ids.append(owner)
    for run in runs:
        if getattr(run, "task_id", None) == task.id:
            role_ids.append(str(getattr(run, "persona_id", "") or ""))
    for event in events:
        if event.task_id == task.id and event.persona_id:
            role_ids.append(str(event.persona_id))
    for assignment in persona_assignments or []:
        if getattr(assignment, "task_id", None) == task.id:
            role_ids.append(str(getattr(assignment, "persona_id", "") or ""))
    seen: set[str] = set()
    streams: list[dict] = []
    for role_id in role_ids:
        if not role_id or role_id in seen:
            continue
        seen.add(role_id)
        role_events = _coalesced_progress_events([event for event in events if event.task_id == task.id and event.persona_id == role_id])
        role_runs = [run for run in runs if getattr(run, "task_id", None) == task.id and getattr(run, "persona_id", None) == role_id]
        role_workers = [worker for worker in workers if getattr(worker, "task_id", None) == task.id and getattr(worker, "persona_id", None) == role_id]
        role_assignments = [assignment for assignment in (persona_assignments or []) if assignment.task_id == task.id and assignment.persona_id == role_id]
        role_envelope_items = [item for item in (role_envelopes or []) if item.task_id == task.id and item.role_id == role_id]
        role_checklist_items = [item for item in (role_checklists or []) if item.task_id == task.id and item.role_id == role_id]
        role_proof_batch_ids = {getattr(item, "proof_batch_id", None) for item in role_envelope_items if getattr(item, "proof_batch_id", None)}
        role_proof_batches = [
            item
            for item in (proof_batches or [])
            if item.task_id == task.id
            and (
                getattr(item, "role_envelope_id", None) in {envelope.envelope_id for envelope in role_envelope_items}
                or item.proof_batch_id in role_proof_batch_ids
            )
        ]
        stream_events = [_event_stream_item(event) for event in role_events][-20:]
        if not stream_events:
            stream_events = [_empty_role_stream_item(task, role_id)]
        streams.append(
            {
                "persona_id": role_id,
                "display_name": _display_name_for_persona(role_id),
                "current_stage_id": _role_current_stage(task, role_id),
                "persona_instance_ids": [assignment.persona_instance_id for assignment in role_assignments if getattr(assignment, "persona_instance_id", None)],
                "active_run_ids": [run.id for run in role_runs if run.state in ACTIVE_RUN_STATES],
                "active_worker_session_ids": [
                    worker.id
                    for worker in role_workers
                    if str(getattr(getattr(worker, "state", ""), "value", getattr(worker, "state", ""))) not in {"completed", "blocked", "closed"}
                ],
                "assignment_ids": [assignment.id for assignment in role_assignments],
                "active_assignment_ids": [assignment.id for assignment in role_assignments if assignment.state in ACTIVE_ASSIGNMENT_STATES],
                "role_envelopes": [role_envelope_summary(item) for item in role_envelope_items],
                "role_checklists": [checklist_summary(item) for item in role_checklist_items],
                "proof_batches": [proof_batch_summary(item) for item in role_proof_batches],
                "events": stream_events,
            }
        )
    return streams


def _stage_streams(task, events) -> list[dict]:
    plan = getattr(task, "mission_plan", None)
    stages = list(getattr(plan, "stages", []) or [])
    if not stages:
        stages = list(task_stage_records(task))
    streams: list[dict] = []
    for stage in stages:
        stage_id = getattr(stage, "id", None)
        if not stage_id:
            continue
        stage_events = _coalesced_progress_events([
            event
            for event in events
            if event.task_id == task.id and (event.payload or {}).get("stage_id") == stage_id
        ])
        streams.append(
            {
                "stage_id": stage_id,
                "owner": getattr(stage, "owner", None),
                "repo": getattr(stage, "repo", None),
                "kind": getattr(stage, "kind", None),
                "status": getattr(getattr(stage, "status", None), "value", getattr(stage, "status", None)),
                "events": [_event_stream_item(event) for event in stage_events][-20:],
            }
        )
    return streams


def _event_stream_item(event) -> dict:
    return {
        "ts": event.ts,
        "type": event.type,
        "run_id": event.run_id,
        "persona_id": event.persona_id,
        "payload": _event_display_projection(event),
    }


def _empty_role_stream_item(task, persona_id: str) -> dict:
    display_name = _display_name_for_persona(persona_id)
    current_stage_id = _role_current_stage(task, persona_id)
    summary = f"{display_name} stream is visible; no redaction-safe events have been captured for this role yet."
    return {
        "ts": getattr(task, "updated_at", None),
        "type": "role_stream.status",
        "run_id": None,
        "persona_id": persona_id,
        "payload": {
            "display_kind": "event",
            "display_title": f"{display_name} ready",
            "display_summary": summary,
            "redaction_status": "safe",
            "status": "idle",
            "summary": summary,
            "stage_id": current_stage_id,
        },
    }


def _display_name_for_persona(persona_id: str) -> str:
    return {
        "neko_supervisor": "Neko Mission Lead",
        "backend_dev": "Backend Dev Agent",
        "dev": "Launcher Dev Agent",
        "qa": "QA Agent",
    }.get(persona_id, persona_id.replace("_", " ").title())


def _role_current_stage(task, persona_id: str) -> str | None:
    plan = getattr(task, "mission_plan", None)
    if plan is not None:
        current_id = getattr(plan, "current_stage_id", None)
        for stage in getattr(plan, "stages", []) or []:
            owner = getattr(stage, "owner", None)
            if owner == persona_id or (owner == "dev" and persona_id == "dev"):
                if stage.id == current_id or stage.status not in {StageStatus.READY_FOR_QA, StageStatus.PASSED}:
                    return stage.id
    return getattr(task, "current_stage_id", None)


def _event_display_projection(event) -> dict:
    payload = event.payload if isinstance(getattr(event, "payload", None), dict) else {}
    kind = _event_display_kind(event.type, payload)
    title = _event_display_title(event.type, payload, kind)
    summary = _safe_text(str(payload.get("summary") or payload.get("status") or payload.get("reason") or ""))
    if not summary:
        summary = _safe_text(operator_event_summary(event) or "")
    refs = []
    for key in ("proof_id", "evidence_id", "packet_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            refs.append({"kind": key.removesuffix("_id"), "id": value})
    result = {
        "display_kind": kind,
        "display_title": title,
        "display_summary": summary,
        "artifact_refs": refs,
        "redaction_status": payload.get("redaction_status"),
    }
    for key in ("event_id", "phase", "step", "status", "decision_type", "reasoning_summary"):
        value = payload.get(key)
        if isinstance(value, str):
            safe = _safe_text(value)
            if safe:
                result[key] = safe
    return result


def _event_display_kind(event_type: str, payload: dict) -> str:
    if event_type == "self_test.recorded":
        return "self_test"
    if event_type == "proof.attached":
        return "final_gate" if payload.get("gate_source") == "auto_after_delivery" else "proof"
    if event_type == "proof.gate_checked":
        return "final_gate"
    if event_type == "packet.recorded":
        packet_type = str(payload.get("packet_type") or "")
        return "delivery" if packet_type == "delivery" else "handoff"
    if event_type == "qa.verdict_recorded":
        return "qa_verdict"
    if event_type in {"task.blocked", "incident.opened"}:
        return "blocker"
    if event_type.startswith("run.tool."):
        return "tool_call"
    if event_type == "run.progress" and str(payload.get("step") or "") in {"reasoning_summary", "decision_summary"}:
        return "thinking_summary"
    return "event"


def _event_display_title(event_type: str, payload: dict, kind: str) -> str:
    if kind == "self_test":
        return f"Self-test {payload.get('status') or 'recorded'}"
    if kind == "final_gate":
        return f"Final gate {payload.get('status') or 'attached'}"
    if kind == "proof":
        return f"Proof {payload.get('status') or 'attached'}"
    if kind == "delivery":
        return "Delivery packet"
    if kind == "qa_verdict":
        return f"QA verdict {payload.get('verdict') or ''}".strip()
    if kind == "blocker":
        return "Blocker"
    if kind == "tool_call":
        return f"Tool {payload.get('tool_name') or payload.get('tool') or event_type}"
    return event_type


def _agent_summary(agent, *, include_tool_details: bool = False, readiness=None):
    # Route through the same TTL-memoized readiness resolve_tool_visibility uses
    # (below) instead of a direct profile_readiness_for_persona call, so one build
    # computes readiness at most once per agent instead of twice. Identical result
    # for this path: readiness is task/stage-independent here, and the memo shares
    # the exact inputs (id/profile/skills/mcp/provider/model/api_mode).
    if readiness is None:
        readiness = _profile_readiness_for_visibility(agent)
    tool_resolution = resolve_tool_visibility(agent, profile_readiness=readiness)
    summary = {
        "persona_id": agent.id,
        "display_name": agent.display_name,
        "role": agent.role,
        "hermes_profile": agent.hermes_profile,
        "profile_readiness": readiness["readiness"],
        "readiness_summary": readiness["summary"],
        "skills": list(agent.skills),
        "missing_skills": readiness.get("missing_skills", []),
        "required_mcp_servers": list(agent.required_mcp_servers),
        "effective_required_mcp_servers": readiness.get("effective_required_mcp_servers", []),
        "missing_mcp_servers": readiness.get("missing_mcp_servers", []),
        "skill_hash_mismatches": readiness.get("skill_hash_mismatches", []),
        "toolsets": effective_toolsets(agent),
        "model_configured": bool(agent.model),
        "provider_configured": bool(agent.provider),
        "default_model": _safe_model_label(getattr(agent, "model", None)),
        "default_provider": _safe_model_label(getattr(agent, "provider", None)),
        "autonomy": agent.autonomy,
        "core_context_files": "enabled" if getattr(agent, "include_core_context_files", False) else "isolated",
        "repo_scope_label": _safe_text(getattr(agent, "repo_scope_label", None)) or _safe_repo_scope_label(getattr(agent, "repo_scope", None)),
    }
    if include_tool_details:
        # Residue-slim R2: same tool-detail eviction as persona_instance_summary.
        # The heavy payloads (tool_resolution / turn_tool_context /
        # permission_state / blocked_tools) leave the row behind a typed
        # ``visibility_ref`` pointer, fetched on demand via
        # ``harness persona-instance detail``; ``agent_hud_state`` is RETIRED
        # (runtime_hud.py is the single HUD authority). The head keeps only the
        # SCALARS the agents drawer renders, derived at emit from the same
        # tool-visibility resolution.
        summary.update(
            {
                "permission_mode": tool_resolution.get("permission_mode") or "profile_default",
                "mutation_boundary": tool_resolution["mutation_boundary"],
                "tool_count": tool_resolution["final_tool_count"],
                "blocked_tools_count": len(tool_resolution["blocked_tools"]),
                "effective_toolsets": tool_resolution["effective_toolsets"],
                "visibility_ref": persona_instance_visibility_ref(agent.id),
            }
        )
    return summary


# Profile-template discovery re-parses every profile YAML (~0.7s of one
# snapshot core, measured 2026-07-09) and the catalog changes only when the
# operator installs/creates a profile. Same TTL-memo treatment as the skill
# catalog in prompt_observability — observability rows, never authority; a
# new profile appears on the first core built after the TTL lapses.
_PROFILE_TEMPLATE_TTL_SECONDS = 15.0
_profile_template_memo: dict = {"at": 0.0, "rows": None, "fn": None}


def _profile_templates_cached() -> list:
    """Memo keyed on BOTH the TTL and the fetcher's identity: a
    monkeypatched `available_profile_templates` invalidates the memo
    immediately instead of being masked for a TTL window."""
    import time

    fetcher = available_profile_templates
    now = time.monotonic()
    if (
        _profile_template_memo["rows"] is not None
        and _profile_template_memo["fn"] is fetcher
        and now - _profile_template_memo["at"] < _PROFILE_TEMPLATE_TTL_SECONDS
    ):
        return _profile_template_memo["rows"]
    try:
        rows = list(fetcher())
    except Exception:
        rows = []
    _profile_template_memo["rows"] = rows
    _profile_template_memo["at"] = now
    _profile_template_memo["fn"] = fetcher
    return rows


def _available_persona_summary(agents) -> list[dict]:
    templates = _profile_templates_cached()
    if not templates:
        return []
    backs_by_profile = {
        str(getattr(agent, "hermes_profile", "") or ""): str(getattr(agent, "id", "") or "")
        for agent in agents
        if getattr(agent, "hermes_profile", None) and getattr(agent, "id", None)
    }
    summaries: list[dict] = []
    for template in templates:
        profile_name = str(getattr(template, "name", "") or "").strip()
        if not profile_name:
            continue
        item = {
            "persona_id": f"profile:{profile_name}",
            "display_name": _display_name_for_profile(profile_name),
            "role": "profile",
            "hermes_profile": profile_name,
            "source": "hermes_profile",
            "template_only": True,
            "profile_readiness": "available",
        }
        description = _safe_text(str(getattr(template, "description", "") or ""))
        if description:
            item["description"] = description
        backs_persona_id = backs_by_profile.get(profile_name)
        if backs_persona_id:
            item["backs_persona_id"] = backs_persona_id
        summaries.append(item)
    return summaries


def _display_name_for_profile(profile_name: str) -> str:
    words = [part for part in re.split(r"[-_\s]+", profile_name.strip()) if part]
    return " ".join(part[:1].upper() + part[1:] for part in words) or profile_name


def _safe_repo_scope_label(value):
    if not value:
        return None
    text = str(value).replace("\\", "/").rstrip("/")
    name = text.rsplit("/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-._")[:64] or None


def _safe_model_label(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.:/@+-]{1,220}", text):
        return None
    return text


def _repo_scopes_summary() -> dict:
    return {
        "harness": _repo_scope_entry("hermes-agent"),
        "frontend": _repo_scope_entry("EterniaLauncher"),
        "backend": _repo_scope_entry("EterniaBackend"),
    }


def _repo_scope_entry(alias: str) -> dict:
    resolved = resolve_affected_repo_workdir(alias)
    return {
        "label": _safe_repo_scope_label(alias),
        "resolved": resolved is not None,
    }


def _run_summary(run):
    decision_type = None
    decision_summary = None
    decision_rationale = None
    has_error = bool(run.error)
    if isinstance(run.final_decision, dict) and not has_error:
        decision_type = run.final_decision.get("type")
        decision_summary = _safe_text(run.final_decision.get("summary"))
        decision_rationale = _safe_text(run.final_decision.get("rationale"))
    llm = _safe_llm(getattr(run, "llm", None))
    return {
        "run_id": run.id,
        "persona_id": run.persona_id,
        "task_id": run.task_id,
        "stage_id": run.stage_id,
        "state": str(run.state),
        "started_at": run.started_at,
        "last_heartbeat_at": run.last_heartbeat_at,
        "finished_at": run.finished_at,
        "duration_ms": _duration_ms(run.started_at, run.finished_at),
        "decision_type": _public_decision_value(decision_type, llm.get("public_decision_type"), llm.get("decision_type")),
        "decision_summary": decision_summary,
        "decision_rationale": decision_rationale,
        "reasoning_summary": decision_rationale,
        "session_id": run.session_id or llm.get("session_id"),
        "llm": llm or None,
        "has_error": has_error,
    }


def _safe_llm(value):
    if not isinstance(value, dict):
        return {}
    allowed = {
        "provider", "model", "base_url_host", "session_id", "api_calls", "tool_turns",
        "input_tokens", "output_tokens", "total_tokens", "latency_ms", "finish_reason",
        "response_len", "validation_status", "decision_type", "public_decision_type",
        "execution_decision_type", "raw_decision_type", "decision_contract_mode",
    }
    safe = {key: value.get(key) for key in allowed if value.get(key) is not None}
    timing = value.get("timing")
    if isinstance(timing, dict):
        safe_timing = {}
        for key, item in timing.items():
            safe_key = str(key)
            if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", safe_key):
                continue
            if isinstance(item, bool):
                continue
            try:
                parsed = int(item)
            except (TypeError, ValueError):
                continue
            if parsed >= 0:
                safe_timing[safe_key] = parsed
        if safe_timing:
            safe["timing"] = safe_timing
    return safe


def _public_decision_value(*candidates):
    for candidate in candidates:
        text = str(candidate or "").strip()
        if not text:
            continue
        return public_decision_type_value(text) or text
    return None


def _safe_text(value):
    """Operator-console text: paths allowed, secret assignments masked in place.

    This used to drop the ENTIRE string when it looked path-ish, which silently
    nulled dev decision rationales/summaries (any real dev rationale names a
    file) and starved the conversation projection of thinking/turn detail.
    Mission Control is an operator surface: repo paths are the content, not a
    leak. Only secret-shaped assignments are redacted, and in place.
    """

    if not isinstance(value, str):
        return None
    text = " ".join(value.strip().split())
    if not text:
        return None
    text = _SECRET_RE.sub("[redacted secret]", text)
    if len(text) > 500:
        return f"{text[:497]}…"
    return text


def _incident_summary(incident):
    return {"incident_id": incident.id, "task_id": incident.task_id, "run_id": incident.run_id, "kind": incident.kind, "is_open": incident.closed_at is None, "opened_at": incident.opened_at, "closed_at": incident.closed_at}


def _proof_summary(proof):
    metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
    summary = {
        "proof_id": proof.id,
        "task_id": proof.task_id,
        "stage_id": proof.stage_id,
        "type": str(proof.type),
        "title": proof.title,
        "status": metadata.get("status") or metadata.get("verdict"),
        "exit_code": metadata.get("exit_code"),
        "duration_ms": metadata.get("duration_ms"),
        "has_artifact": bool(proof.path_or_value),
        "redaction_status": proof.redaction_status,
        "created_by": proof.created_by,
        "created_at": proof.created_at,
    }
    for key in ("lane_id", "persona_instance_id", "repo_bundle_id", "proof_reuse_basis"):
        if isinstance(metadata.get(key), str):
            summary[key] = metadata[key]
    if isinstance(metadata.get("repo_bundle_ids"), list):
        summary["repo_bundle_ids"] = list(metadata["repo_bundle_ids"][:8])
    return summary
