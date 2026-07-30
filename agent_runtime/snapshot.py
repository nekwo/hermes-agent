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
from .dirty_state import build_dirty_state
from .events import CachedEventLog, EventLog, event_summary_missing, operator_event_summary
from .migrations import effective_config_summary, migration_status
from .models import looks_like_persona_instance_id
from .observability import build_observability
from .operator_channels import operator_channel_summary
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
from .personas import blocked_tool_names, effective_toolsets
from .prompt_observability import _SkillObservabilityResolver, snapshot_prompt_observability
from .realm_sync import read_realm_sync_sidecar
from .repo_bundles import qa_waiting_on
from .runtime_instances import runtime_instance_summary, runtime_instances_summary
from .repo_context import resolve_affected_repo_workdir
from .role_checklists import RoleChecklist, checklist_summary
from .role_envelopes import RoleEnvelope, role_envelope_summary
from .simplified_contract import public_decision_type_value
from .serde import from_jsonable, to_jsonable
from .states import RunState, TaskState
from .store import ACTIVE_RUN_STATES, AgentStore, RealmStore, RunStore, WorkspaceStore
from .tool_visibility import (
    _profile_readiness_for_visibility,
    resolve_tool_visibility,
)
from .workspace_scope import exact_scoped_instance_ids
from .worker_sessions import WorkerSessionStore, worker_session_summary

STAGE_VERIFICATION_STAGE_CAP = 12
STAGE_VERIFICATION_PROOF_ID_CAP = 8
STAGE_VERIFICATION_PATH_CAP = 6
# Residue-slim R5(a): cap the retired in-head flow timeline at the FRONT
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
    agent_store = agent_store or AgentStore()
    # A snapshot calls for_task/for_session/tail dozens of times on the same log;
    # CachedEventLog reads events.jsonl once and serves all of them from memory.
    event_log = event_log or CachedEventLog()
    # Force + time the one-shot CachedEventLog materialization here (the ~1s
    # event-log read, measured 2026-07-23) before other consumers warm it, so
    # ``sections_ms.events`` honestly attributes that cost. ``recent_events`` is
    # a pure read reused later (build_observability / parity watermark).
    with _timed_section(_sections_ms, "events"):
        recent_events = event_log.tail(20)
    tasks = []
    runs = []
    workers = []
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
    agents = agent_store.list_all()
    # Closed incidents are history-only in the steady-state frame. Avoid
    # recursively coercing the entire closed tail (thousands of files on a
    # mature runtime) merely to count it; the store still validates each JSON
    # row and materializes every open incident used by routing/observability.
    incidents = []
    role_envelopes = []
    role_checklists = []
    repo_bundles = []
    runtime_instances = []
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
    persona_assignments = []
    persona_instances = []
    topology_persona_instances = PersonaInstanceStore(event_log=event_log).list_all()
    personas_by_id = {str(getattr(agent, "id", "") or ""): agent for agent in agents}
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
    # S4: the frame carries these as id-keyed maps (``_keyed`` below). The row
    # LISTS are still needed as an ordered input to the operator-channel
    # projection (``run_summaries`` feeds the goal-turn flow), so build them once
    # here and key them into the frame.
    # ``run_rows`` (ALL runs) still feeds the operator-channel goal-turn flow
    # below; the FRAME ``runs`` map (S8) keeps only ACTIVE runs (attached to a
    # live lane/goal). Historical/terminal runs are old residue — evicted to a
    # count + pointer, fetched on demand via ``harness run list``. No disk
    # change: the run store keeps every row.
    run_rows = []
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
            proof_store=None,
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
            "persona_instances": len(topology_persona_instances),
        },
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
        "repo_scopes": _repo_scopes_summary(),
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
        "agents": agent_summaries,
        "available_personas": available_personas,
        "runtime_paths_diagnostic": _runtime_paths_diagnostic(available_personas),
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
        # 45 removes mission rows while retaining chat/runtime graph projections.
        "contract_version": 45,
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
        ),
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
    role_ids: set[str] = set()
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
) -> list[dict]:
    role_ids: list[str] = []
    role_envelopes = role_envelopes or []
    role_checklists = role_checklists or []
    for run in runs:
        role_ids.append(str(run.get("persona_id") or ""))
    for event in events:
        role_ids.append(str(event.get("persona_id") or ""))
    for envelope in role_envelopes:
        role_ids.append(str(envelope.get("role_id") or ""))
    for checklist in role_checklists:
        role_ids.append(str(checklist.get("role_id") or ""))

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
    current_stage_id = raw.get("current_stage_id")
    return current_stage_id if isinstance(current_stage_id, str) else None


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
    return {}


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
    return getattr(task, "current_stage_id", None)


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
    agents = AgentStore().list_all()
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
    return {
        "schema_version": 1,
        "mission_id": task.id,
        "task_id": task.id,
        "active_stage_id": getattr(task, "current_stage_id", None),
        "actors": [],
        "updated_at": getattr(task, "updated_at", None),
    }


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
    role_ids: list[str] = []
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
        return {
            **_stopped_progress(task, open_incidents, "environment_blocked", "human"),
            "action": "blocked_by_incident",
            "reason": "open incident requires human review",
        }
    if task.state in {TaskState.DONE, TaskState.CANCELLED}:
        return {
            **_stopped_progress(task, [], "settled", "harness"),
            "action": "terminal",
            "reason": "mission is terminal",
        }
    return {
        **_stopped_progress(task, [], "routing_removed", "human"),
        "action": "undispatchable",
        "reason": "the task dispatch lane has been retired",
    }
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


def _role_streams(task, events, runs, workers, *, persona_assignments=None, role_envelopes=None, role_checklists=None) -> list[dict]:
    role_ids: list[str] = []
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
                "events": stream_events,
            }
        )
    return streams


def _stage_streams(task, events) -> list[dict]:
    return []


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
    return persona_id.replace("_", " ").title()


def _role_current_stage(task, persona_id: str) -> str | None:
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
        return "proof"
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
