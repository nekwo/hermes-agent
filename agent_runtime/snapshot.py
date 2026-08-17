from __future__ import annotations

import copy
import json
import logging
import re
import threading
import time
import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
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
from .config import load_agent_runtime_config, load_root_runtime_config
from .decision_contract_registry import CONTRACT_SCHEMA_VERSION, contract_hash
from .events import CachedEventLog, event_summary_missing
from .migrations import effective_config_summary, migration_status
from .models import looks_like_persona_instance_id
from .operator_channels import operator_channel_summary
from .persona_assignments import (
    ACTIVE_ASSIGNMENT_STATES,
    PersonaAssignmentStore,
    PersonaInstanceStore,
    active_persona_instance_agent_summaries,
    persona_assignment_summary,
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
from .persona_lifecycle import is_runtime_persona
from .parity import PARITY_ENVELOPE_VERSION, ProjectionAccountant, events_watermark
from .resolution import resolution_payload, resolve_runtime, runtime_resolution_scope, suspect_default_root
from .personas import effective_toolsets
from .prompt_observability import _SkillObservabilityResolver, snapshot_prompt_observability
from .realm_sync import read_realm_sync_sidecar
from .repo_context import resolve_affected_repo_workdir
from .running_work import build_running_work
from .serde import to_jsonable
from .store import AgentStore, RealmStore, WorkspaceStore
from .tool_visibility import (
    _profile_readiness_for_visibility,
    resolve_tool_visibility,
)
from .workspace_scope import exact_scoped_instance_ids


#: THE snapshot wire contract version — the single producer-side authority.
#:
#: Every consumer of this number, in this repo and in the Launcher, derives it
#: from here rather than restating it. That rule exists because restating it is
#: what kept breaking: the contract-53 landing moved two of six hermes test
#: literals and left four red on ``main`` for five days, and the 53 -> 54 sweep
#: then found eleven more still pinned at 52. Each of those was a *negative*
#: gate — "this cut did NOT move the contract" — expressed as an absolute
#: number, so every unrelated bump made a true statement go red and taught
#: readers that a contract failure is routine noise.
#:
#: Consequently:
#:
#: * production reads :data:`SNAPSHOT_CONTRACT_VERSION` (see the parity envelope
#:   in :func:`_parity_envelope`, whose comment block carries the full
#:   version-by-version history and the bump/keep rulings behind it);
#: * tests import it and assert RELATIVE to it (``== SNAPSHOT_CONTRACT_VERSION``,
#:   ``- 1``, ``+ 1``) so a bump moves them for free;
#: * exactly ONE test states the literal —
#:   ``test_snapshot_contract_version_authority.py`` — because the number is a
#:   cross-repo lockstep with the Launcher's ``kSupportedMissionContractVersion``
#:   and moving it must be a deliberate, reviewed edit rather than a silent
#:   consequence of a refactor;
#: * a structural (AST) gate in that same file fails if any other test states it.
SNAPSHOT_CONTRACT_VERSION = 54

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SnapshotSummary:
    """The frame's ``summary`` block, as a CLOSED set of fields.

    ``summary`` was a bare dict literal, and two parity warnings kept reading
    ``open_tasks`` / ``open_incidents`` out of it for the two contract versions
    after the mission-row removal stopped emitting them — a dict answers
    ``.get()`` for any key, so the branches read ``None``, evaluated false, and
    the warnings simply never fired. Nothing failed; the checks just quietly
    stopped existing. Declaring the field set here means a reader of a field the
    summary does not emit is an ``AttributeError`` where it is written, not a
    silent no-op that survives audits.
    """

    persona_instances: int

    def as_dict(self) -> dict[str, int]:
        return {"persona_instances": int(self.persona_instances)}


@dataclass(slots=True)
class SnapshotBuildContext:
    """Reusable, non-wire caches owned by one long-lived serve process."""

    skill_root_registries: dict[str, object] = field(default_factory=dict)


_SNAPSHOT_BUILD_CONTEXT: ContextVar[SnapshotBuildContext | None] = ContextVar(
    "snapshot_build_context", default=None
)


@contextmanager
def snapshot_build_context_scope(context: SnapshotBuildContext):
    token = _SNAPSHOT_BUILD_CONTEXT.set(context)
    try:
        yield context
    finally:
        _SNAPSHOT_BUILD_CONTEXT.reset(token)


# S2 read-model — history out of the live frame (operator move 6).
# The persona-chat message tails are append-only HISTORY: read on demand at
# most, never advancing. They are evicted from the steady-state frame and served
# via a paged on-demand query (``harness persona chat history``).
# Archive-never-delete: eviction removes rows from the FRAME, never from disk.
#
# S7-B RULING-0 COMPAT STRIP (2026-07-16): the ``read_model.history_in_frame``
# kill-switch and its full-in-frame legacy branches were removed here — the
# evicted (pointer-stub) shape is the ONLY shape. Rollback = ``git revert``, not
# a flag flip. The helper below always evicts.
#
# S29: the sibling ``archived_tasks`` and ``incidents`` eviction helpers are
# gone. S9 removed both as frame sections and S27 removed the archived-task
# reader, so ``_open_incidents_frame`` had no list left to split and
# ``snapshot_section_bytes`` had no section left to weigh; both survived S27
# only as extra reachability roots seeded from a TEST pin, which is not a
# caller. See tests/agent_runtime/test_s29_snapshot_dead_local_removal.py.


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


#: The three roles a caller of :func:`build_snapshot` can leave in with, and the
#: exact tokens both this module's receipt line and
#: ``agent_runtime.stream``'s wait line print. ONE vocabulary, because the whole
#: point of the attribution is that three log lines stop looking like three
#: builds: ``led`` ran a build, ``rode`` shared the build that was ALREADY
#: running (``accept_inflight``), ``shared_next`` waited and got the deep copy of
#: the next build somebody else led. A boot's build COUNT is therefore the count
#: of ``led`` lines — never the count of lines.
BUILD_ROLE_LED = "led"
BUILD_ROLE_RODE = "rode"
BUILD_ROLE_SHARED_NEXT = "shared_next"

#: A caller that named itself nothing. Kept as a token rather than an empty
#: value so the receipt line's key is never absent — a parser reading
#: ``caller=`` off the line must not have to tell "no such key" apart from
#: "nobody said".
BUILD_CALLER_UNKNOWN = "unknown"

#: Sections are printed on a WAIT line only when the underlying build was slow
#: enough for the split to be the question being asked (HC-0). The leader's own
#: receipt always carries them: a build that took 900 ms still answers "of
#: what?" for free, and it is one line per build, not one per caller.
BUILD_SECTIONS_WAIT_THRESHOLD_MS = 5000


def build_receipt_facts(snapshot: Any) -> dict[str, Any]:
    """The attribution facts a build's receipt line carries, read off the core.

    ``build_ms``, ``sections_ms`` and ``watermark.event_offset`` are already
    computed by the build itself and shipped on the parity envelope. This reads
    them; it does not measure anything. A second measurement of the same span
    would be a second authority on it, and the two would drift — which is the
    exact defect the heartbeat's mid-build ``elapsed_ms`` sample already is
    (see ``agent_runtime.stream._log_snapshot_build``).

    Single-homed here because two log lines print these facts — the leader's
    ``snapshot_build_core`` below and the waiter's ``snapshot_build`` in the
    stream — and a launcher-side parser reads both. Defensive by construction:
    a caller may hold a fake or partially-built core (every unit fake does), and
    an instrument must never be the reason a build fails.
    """

    parity = snapshot.get("parity") if isinstance(snapshot, dict) else None
    if not isinstance(parity, dict):
        parity = {}
    watermark = parity.get("watermark")
    if not isinstance(watermark, dict):
        watermark = {}
    raw_build_ms = parity.get("build_ms")
    raw_offset = watermark.get("event_offset")
    return {
        "build_ms": int(raw_build_ms) if isinstance(raw_build_ms, (int, float)) else None,
        "offset": int(raw_offset) if isinstance(raw_offset, (int, float)) else None,
        "sections_top": _sections_top(parity.get("sections_ms")),
    }


def _sections_top(sections: Any, limit: int = 3) -> str:
    """The ``limit`` most expensive build sections, as ``name:ms,name:ms``.

    Sorted by cost descending, then by name, so consecutive boots of the same
    shape print the same string and a diff means the shape moved.
    """

    if not isinstance(sections, dict):
        return "-"
    rows = [
        (str(name), int(value))
        for name, value in sections.items()
        if isinstance(value, (int, float))
    ]
    if not rows:
        return "-"
    rows.sort(key=lambda row: (-row[1], row[0]))
    return ",".join(f"{name}:{value}" for name, value in rows[:limit])


def _log_snapshot_build_core(*, caller: str, generation: int | None, snapshot: Any) -> None:
    """ONE line per ACTUAL build, emitted by the caller that ran it.

    Every other line about a build is a WAIT (``agent_runtime.stream``'s
    ``snapshot_build``), and until this line existed the two were
    indistinguishable: the 2026-08-17 boot's three "concurrent builds" were one
    build plus two riders logging their waits, and the most expensive build of
    that boot — the serve prewarm — logged nothing at all, because nothing on
    its path had a line to emit. Grep ``role=led`` for the build count.

    Emitted on the default-store coalesced path only. An injected-store build
    (tests, doctors, a detail-fetch catalog capture) is a fixture, not a boot,
    and printing one line per unit test would bury the boot's own lines.

    Rides the ordinary ``Logger`` family, so ``hermes serve`` lands it in
    ``<HERMES_HOME>/logs/agent.log`` at INFO with no extra flag.
    """

    facts = build_receipt_facts(snapshot)
    logger.info(
        "snapshot_build_core role=%s caller=%s generation=%s build_ms=%s offset=%s sections_top=%s",
        BUILD_ROLE_LED,
        caller,
        "-" if generation is None else int(generation),
        "unknown" if facts["build_ms"] is None else int(facts["build_ms"]),
        "unknown" if facts["offset"] is None else int(facts["offset"]),
        facts["sections_top"],
    )


def _record_build_info(
    build_info: dict | None,
    *,
    role: str,
    caller: str,
    generation: int | None,
    snapshot: Any = None,
) -> None:
    """Fill the caller's ``build_info`` out-param. Never raises, never reads.

    The dict belongs to the CALLER (one per caller, by construction — that is
    what makes the role matrix in
    ``tests/agent_runtime/test_snapshot_build_logging.py`` able to convict a
    hardcoded role). ``build_ms`` rides along so a caller can name the cost of
    the build it rode without re-deriving the envelope.
    """

    if build_info is None:
        return
    build_info["role"] = role
    build_info["caller"] = caller
    build_info["generation"] = generation
    if snapshot is not None:
        build_info["build_ms"] = build_receipt_facts(snapshot)["build_ms"]


def _build_caller(build_info: dict | None) -> str:
    """Who is asking, in their own words — the ONE input side of ``build_info``.

    Pre-seeded by the caller (``{"caller": "hub"}``); everything else in the
    dict is filled by the builder. Threaded rather than inferred because the
    builder genuinely cannot know: hub, cli, prewarm and the office lane all
    reach the same function through the same call.
    """

    if not isinstance(build_info, dict):
        return BUILD_CALLER_UNKNOWN
    caller = build_info.get("caller")
    text = str(caller).strip() if caller is not None else ""
    return text or BUILD_CALLER_UNKNOWN


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
    incident_store=None,
    event_log=None,
    prompt_skills_catalogs=None,
    *,
    accept_inflight: bool = False,
    build_info: dict | None = None,
) -> dict:
    """Build the read-model core, coalescing concurrent builds (see above).

    ``accept_inflight`` opts a caller into sharing the build that is ALREADY
    running rather than waiting for the next one. It is not a freshness
    shortcut and must only be used where the payload carries its own
    watermark and the consumer replays everything after it — the stream
    hydrate is the one such caller (``agent_runtime.stream.hydrate_frame``):
    its ``watermark.event_offset`` comes from the snapshot itself and the
    stream loop tails events from exactly that offset, so a core built one
    build-duration earlier loses no event, it only delays it to the first
    delta frame. Without this opt-in, a background prewarm build and the
    hydrate that arrives moments later would cost TWO sequential builds
    (strict coalescing makes the second caller wait for the first AND then
    lead its own) — worse than no prewarm at all.

    ``build_info`` is one dict used in both directions, and the asymmetry is the
    point. IN: the caller may pre-seed ``{"caller": "hub"|"cli"|"prewarm"|…}``,
    the one fact the builder cannot derive. OUT: the builder fills ``role``
    (:data:`BUILD_ROLE_LED` / :data:`BUILD_ROLE_RODE` /
    :data:`BUILD_ROLE_SHARED_NEXT`), ``caller``, ``generation`` and ``build_ms``,
    so the caller can say which of those three things happened to IT rather than
    logging a wait that reads like a build. Pure out-param otherwise: the return
    value and the coalesce behaviour are untouched by its presence, and a caller
    that passes nothing gets today's function exactly.
    """

    caller = _build_caller(build_info)
    custom_stores = any(
        value is not None
        for value in (
            task_store,
            run_store,
            agent_store,
            incident_store,
            event_log,
        )
    )
    if custom_stores or prompt_skills_catalogs is not None:
        # Injected stores (tests, doctors) must observe exactly their own
        # fixtures — never a coalesced result built from the default stores.
        # A detail-fetch catalog capture likewise needs the exact build's
        # transient bodies; the shared result intentionally contains hashes
        # only, so it cannot satisfy that internal projection request.
        injected = _build_snapshot_uncoalesced(
            task_store=task_store,
            run_store=run_store,
            agent_store=agent_store,
            incident_store=incident_store,
            event_log=event_log,
            prompt_skills_catalogs=prompt_skills_catalogs,
        )
        # An injected-store build leads its own by definition (it never touched
        # the coalescer), so the role is honest — but it has no generation in
        # the default path's sequence and it emits no receipt line: see
        # ``_log_snapshot_build_core`` for why a fixture must not print one.
        _record_build_info(
            build_info,
            role=BUILD_ROLE_LED,
            caller=caller,
            generation=None,
            snapshot=injected,
        )
        return injected
    state = _build_coalesce_state
    with _BUILD_COALESCE:
        # The first build STARTED at/after arrival is the one that satisfies
        # this caller; an in-flight build began earlier and may miss writes
        # this caller has already observed. ``accept_inflight`` callers opt out
        # of that guarantee (see the docstring) and join the running build —
        # only the RUNNING one: while ``running`` is true ``done`` is strictly
        # behind ``started``, so this can never hand back a finished build's
        # leftover result.
        rode_inflight = bool(accept_inflight and state["running"])
        target = state["started"] + (0 if rode_inflight else 1)
        while True:
            if state["done"] >= target and state["result"] is not None:
                payload = copy.deepcopy(state["result"])
                if state["waiters"] == 0:
                    state["result"] = None
                # Two different things end here and they are NOT the same
                # attribution: a caller that opted into the build already
                # running (``rode``) and a caller that waited for a build
                # started after it arrived and got somebody else's copy of it
                # (``shared_next``). ``rode_inflight`` is read at ARRIVAL, above,
                # because by now the build it named has finished and the state
                # can no longer answer which one this caller asked for.
                _record_build_info(
                    build_info,
                    role=BUILD_ROLE_RODE if rode_inflight else BUILD_ROLE_SHARED_NEXT,
                    caller=caller,
                    generation=target,
                    snapshot=payload,
                )
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
        _record_build_info(
            build_info,
            role=BUILD_ROLE_LED,
            caller=caller,
            generation=generation,
            snapshot=result,
        )
        # The build's OWN line, on the thread that paid for it, before the
        # waiters are notified below — so the receipt for a build always
        # precedes the wait lines of the callers that rode it, in the order an
        # operator reads the log. A build that raised logs nothing: there is no
        # envelope to read the cost off, and the exception is the receipt.
        _log_snapshot_build_core(
            caller=caller, generation=generation, snapshot=result
        )
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
    incident_store=None,
    event_log=None,
    prompt_skills_catalogs=None,
) -> dict:
    with runtime_resolution_scope():
        return _build_snapshot_in_runtime_scope(
            task_store=task_store,
            run_store=run_store,
            agent_store=agent_store,
            incident_store=incident_store,
            event_log=event_log,
            prompt_skills_catalogs=prompt_skills_catalogs,
        )


def _build_snapshot_in_runtime_scope(
    task_store=None,
    run_store=None,
    agent_store=None,
    incident_store=None,
    event_log=None,
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
    # a pure read reused later by the parity envelope (watermark + event-summary
    # warnings); the observability consumer it also named went with S9.
    with _timed_section(_sections_ms, "events"):
        recent_events = event_log.tail(20)
    # S56 removed the LAST of these seeds. ``workers = []`` was kept by S47 as
    # "the one surviving seed still passed into a live projection"
    # (``derive_from_workers``) — but an always-empty list can only make a
    # projection emit a constant, which is exactly why ``tasks`` and
    # ``workspaces[].goals`` went at S47 (ledger item 5). The projection is now
    # ``PersonaInstanceStore.ensure_for_personas(personas)`` and takes no worker
    # argument; the worker session store it read is deleted. The eight earlier
    # look-alikes (``runs``, ``incidents``, ``proofs``,
    # ``role_envelopes``, ``role_checklists``, ``repo_bundles``,
    # ``runtime_instances``) fed the mission projections S9/S18/S27 removed and
    # were assigned-never-read; so were ``execution_mode`` (the retired Mission
    # Daemon's mode string) and ``live_channel_task_ids``.
    cfg = load_agent_runtime_config()
    # Base-profile foundation: Mission Control shows the seeded store (base only). On a
    # cold store, fall back to the base seed itself — NOT ensure_persisted_personas, which
    # also returns the dormant typed catalog for resolution and would surface mothballed
    # pipeline personas that are not meant to be shown.
    agents = [agent for agent in agent_store.list_all() if is_runtime_persona(agent)]
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
    build_context = _SNAPSHOT_BUILD_CONTEXT.get()
    skill_resolver = _SkillObservabilityResolver(
        root_registries=(
            build_context.skill_root_registries if build_context is not None else None
        )
    )
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
    personas_by_id = {str(getattr(agent, "id", "") or ""): agent for agent in agents}
    # S56: the persona-instance roster is UNCONDITIONAL. It was gated on
    # ``enterprise_worker_sessions.enabled AND .persona_instance_runtime`` — a
    # misnomer inherited from the worker lane — and the assignment list on a
    # second field of the same block. The roster IS the identity substrate every
    # Mission Control surface keys on, the enabled shape has been the only shape
    # for months, and no code in either repo branches on a disabled verdict. Both
    # gates went with the config block.
    instance_store = PersonaInstanceStore(event_log=event_log)
    persona_instances = instance_store.ensure_for_personas(agents)
    topology_persona_instances = persona_instances
    agent_summaries = [
        *agent_summaries,
        *active_persona_instance_agent_summaries(
            persona_instances, personas_by_id, readiness_by_persona_id
        ),
    ]
    persona_assignments = PersonaAssignmentStore(event_log=event_log).list_all()
    session_db = _default_persona_session_db()
    migration = migration_status()
    # Hoisted out of the ``data`` literal so their cost is attributable in
    # ``sections_ms`` (both were profiled hot: prompt_observability ~5s, the
    # skills-catalog walks inside it; boards/offices are local-only reads).
    with _timed_section(_sections_ms, "prompt_observability"):
        prompt_observability_section = snapshot_prompt_observability(
            personas=agents,
            persona_instances=persona_instances,
            session_db=session_db,
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
    running_work_accountant = ProjectionAccountant("running_work")
    with _timed_section(_sections_ms, "running_work"):
        running_work_section = build_running_work(running_work_accountant)
    data = {
        "schema_version": 2,
        # Legacy wire names retained for Launcher/core-fixture compatibility;
        # the hash is now computed from the event-only registry.
        "decision_contract_version": CONTRACT_SCHEMA_VERSION,
        "decision_contract_hash": contract_hash(),
        "event_contract_version": CONTRACT_SCHEMA_VERSION,
        "generated_at": now(),
        "summary": SnapshotSummary(persona_instances=len(topology_persona_instances)).as_dict(),
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
        "runtime_config": effective_config_summary(cfg),
        "migration": migration,
        "prompt_observability": prompt_observability_section,
        "repo_scopes": _repo_scopes_summary(),
        "workspaces": [
            _workspace_summary(
                item,
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
        # Unified background-work projection: terminal processes, subagent
        # delegations, in-flight chat turns, MCP servers, cron jobs — one row
        # vocabulary across six subsystems, durable-first so a cold CLI lane
        # answers as honestly as the serve process that spawned the work. Every
        # lane carries its own ok/unavailable health under ``sources``; an
        # unreadable lane is REPORTED, never rendered as "nothing running".
        "running_work": running_work_section,
        "active_workspace_id": workspace_store.active_id(),
        "active_realm_id": realm_store.active_id(),
        "warnings": _snapshot_warnings(persona_assignments),
        "agents": agent_summaries,
        "available_personas": available_personas,
        "runtime_paths_diagnostic": _runtime_paths_diagnostic(available_personas),
    }
    data["persona_instance_runtime"] = {
        "enabled": True,
        "assignment_store_enabled": True,
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
        accountant=conversation_accountant,
        intentionally_omitted_history_session_ids=omitted_history_session_ids,
    )
    _sections_ms["persona_chat"] = int(
        max(0.0, (time.perf_counter() - _persona_chat_started)) * 1000
    )
    # S4: operator_channels ships as an id-keyed map (channel_id). S18
    # removed the archived-task channels this used to be merged with, so the
    # live channels are the whole input; ``_keyed`` still keeps the first
    # occurrence on a duplicate id rather than silently overwriting.
    data["operator_channels"] = _keyed(live_operator_channels, "channel_id")
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
        "running_work": running_work_accountant.summary(),
    }
    drop_samples = (
        history_accountant.drop_samples()
        + trace_accountant.drop_samples()
        + conversation_accountant.drop_samples()
        + running_work_accountant.drop_samples()
    )
    # Guarantee the documented section keys exist even when a lane was skipped
    # (e.g. persona_chat when persona-instance runtime is disabled) so consumers
    # can rely on a stable shape.
    for _section_key in (
        "agents_readiness",
        "prompt_observability",
        "events",
        "persona_chat",
        "boards_offices",
        "running_work",
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
    if watermark.get("event_offset") is None:
        # The frame cannot date itself against the log. Riders must resync
        # rather than read the absent position as byte 0 (= "caught up").
        warnings.append(
            {
                "code": "event_offset_unknown",
                "detail": (
                    "the event log's end offset could not be read, so this frame carries no "
                    "source position; watermark-gated consumers must resync instead of resuming "
                    f"({watermark.get('event_offset_error') or 'unreadable'})"
                ),
            }
        )
    if suspect_default_root(resolution):
        warnings.append(
            {
                "code": "suspect_default_root",
                "detail": "runtime root resolved through the default layer, but no store marker directories (persona_instances/, sessions/, agents/) exist; check runtime-root pins",
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
        #
        # 46 (S47, 2026-08-01): two emitted fields whose values no code path
        # could move leave the frame — ``runtime_config.role_envelope`` (the
        # config block S44's store-family cut left governing nothing, shipping
        # ``enabled: true``) and ``workspaces[].goals`` (a count over an
        # always-empty seed). Field removals, not section removals, but the
        # S9/S10 rule is the same: anything that leaves the wire bumps, and the
        # Launcher pin moves in the same wave.
        # 47 (S56, 2026-08-01): one coherent wave, one bump. Leaving the frame:
        # every `worker_session` trace (the store is deleted — `status`'s
        # `worker_sessions` + `active_worker_sessions` rows, the
        # `observability` worker signals/rows, `dirty_state`'s four worker
        # counters, `persona_instances[].active_worker_session_id`); the
        # constant-by-construction repo-bundle wires `repo_bundles` /
        # `repo_bundle_closeout` / `bundle_queue` / `repo_locks` and the
        # duplicate `lanes` (doc 19 filed the last two as asserted-constant
        # debt); `production_envelope` (hand-written prose, several claims false
        # against this tree) with `swarm` / `swarm_budget`; and SEVEN
        # `runtime_config` blocks that no production code read —
        # `continuous_role_sessions`, `enterprise_worker_sessions`,
        # `normal_worker_flow`, `repo_bundle_routing`,
        # `simplified_agent_contract`, `swarm`, plus three of the four
        # `supervision` fields. The persona-instance roster stops being gated on
        # `enterprise_worker_sessions.persona_instance_runtime` and ships
        # unconditionally. The Launcher pin moves in the same wave.
        #
        # 48 (S57, 2026-08-01): the S56 reader-gate's `UNRULED_DEBT` bucket is
        # emptied and the last whole store of the repo-bundle lane goes. Leaving
        # the frame: TWENTY-NINE `runtime_config` scalars with no production
        # reader (the `daemon_*` family, the four `live_run_*` budgets, the four
        # `liveness_*` knobs, the three `artifact_storage_*` watermarks, the two
        # mission ceilings, the two neko caps, `heartbeat_ttl_seconds`,
        # `max_actions_per_tick`, `root_node_mode`,
        # `preferred_goal_execution_mode`, `scope_wait_deadline_seconds`,
        # `run_lease_seconds`, `tool_wait_timeout_seconds`,
        # `child_progress_min_interval_seconds`, `deploy_timeout_seconds`) —
        # all VERIFIED present on the live frame at contract 47, so this edits
        # the wire rather than only the code — together with the validator arms
        # that range-checked them, and `migration.counts.repo_bundles`, the last
        # wire trace of the `RepoBundleStore` deleted whole in the same wave. The
        # Launcher pin moves in the same wave.
        #
        # 49 (S58, 2026-08-01): ``runtime_config.migration`` was a byte-for-byte
        # duplicate of the authoritative top-level ``migration`` block. The
        # Launcher reads neither copy; its supported-contract pin moves in
        # lockstep with this emitted-field removal.
        # 51 retires the last task/proof migration-count rows and the
        # run-summary conversation source. The legacy contract-hash wire names
        # remain compatibility aliases for the event-only registry.
        #
        # 52 (WP-H1, 2026-08-03) ADDS the ``running_work`` section — the first
        # additive move in several waves, and the reason the pin still has to
        # travel: the Launcher's ``MissionSnapshotEnvelope.health()`` requires
        # EXACT equality, so an additive section that arrives under an unbumped
        # version would be invisible rather than merely unread. The section
        # carries its own ``sources`` health block and a ``completeness``
        # accountant row of the same name. The Launcher pin
        # (``kSupportedMissionContractVersion``) moves to 52 in WP-L1, and the
        # live venv is refreshed only AFTER that lands — refreshing first
        # fail-closes the console with a ``newerContract`` banner.
        #
        # 52 KEPT (WP-H2, 2026-08-03) — ``running_work`` gained its sixth lane,
        # ``dispatch``, and this entry records WHY that did not bump. The rule
        # this ledger actually states is not "any wire change bumps"; it is
        # (a) anything that LEAVES the wire bumps (the S9/S10 rule, cited at 46),
        # and (b) an ADDITION bumps when it would otherwise be "invisible rather
        # than merely unread" — the 52 entry above, where the Launcher had no
        # parse for the new section at all. Neither applies here: nothing left
        # the wire, and 52 itself published ``dispatch`` inside
        # ``RUNNING_WORK_KINDS`` with the stated intent that "the wire
        # vocabulary is complete from the first landing and a consumer does not
        # have to re-derive it when the lane arrives". What arrives is a sixth
        # key in a health MAP plus rows carrying a ``kind`` string the version
        # already declared, both of which a consumer pinned at 52 consumes. The
        # Launcher pin has also not moved to 52 yet (WP-L1 is pending), so the
        # section and its sixth lane reach the Launcher in the SAME wave either
        # way. The constraint this puts on WP-L1 is explicit and load-bearing:
        # parse ``sources`` as a map and ``kind`` as an open string. An
        # exhaustive five-lane enum switch would turn this ruling into a
        # fail-closed frame and would have needed the bump instead.
        #
        # 52 KEPT AGAIN (WP-L2 attribution, 2026-08-03) — the operator
        # conversation gained a ``harness_delivery`` message ``kind`` and a
        # typed ``delivery`` sub-block ({dispatch_id, notify_operator}) on the
        # row a dispatch DELIVERY turn produces. The same two-part rule answers
        # it: (a) nothing LEFT the wire — the row still projects role="operator"
        # with the same id/text/timestamps, so a consumer that ignores the kind
        # renders exactly what it renders today; and (b) it is not "invisible
        # rather than merely unread" — the Launcher's conversation adapter falls
        # through unknown kinds to a generic bubble BY DESIGN, and it parses
        # this one in the SAME wave (WP-L2 lands both halves). The 52 entry
        # above bumped because the Launcher had no parse for a whole new
        # SECTION; here the section, the message list and the row are all
        # pre-existing and already parsed. The constraint this puts on the
        # consumer is the same one the dispatch lane put there: conversation
        # ``kind`` stays an OPEN string. An exhaustive switch over it would turn
        # this ruling into a fail-closed frame and would have needed the bump.
        #
        # 52 KEPT, THIRD TIME (WP-L2 review fixes, 2026-08-03) — the
        # ``delivery`` sub-block gained ``state`` (the settled dispatch outcome)
        # and the ``running_work`` dispatch lane gained terminal ``error`` rows
        # for completions whose delivery was ABANDONED. Both are additions to
        # blocks a consumer already parses, on a ``kind`` it already knows, and
        # both are consumed by the Launcher in the same wave. The reason
        # ``state`` had to be added at all is worth recording: without it an
        # ``error`` dispatch — which ``pending_deliveries`` selects and delivers
        # exactly like a successful one — was distinguishable from success only
        # in the prose body, so a consumer would have had to sentence-match to
        # phrase an honest notification. That is precisely what the typed marker
        # exists to prevent, so the fix belongs on the wire rather than in the
        # reader.
        #
        # 53 (Activity ownership correction, 2026-08-04) REMOVES the
        # ``mcp_server`` running-work source and its rows. A connected MCP
        # transport is reusable capability infrastructure, not a background
        # task: it can stay warm after the admitting turn settles and several
        # persona instances can share the same profile/server. It therefore has
        # no single truthful owner and must not make an idle runtime say
        # "1 running". Active calls remain visible on their owning chat turn's
        # tool trace. This is a wire removal, so the contract and Launcher pin
        # move together under the removal rule recorded at 46.
        #
        # 54 (S70 persona-instance wire prune, 2026-08-09) REMOVES six keys from
        # every ``persona_instances`` row. Two were duplicate ALIASES that
        # projected byte-identical values to the canonical key beside them —
        # ``current_work_assignment_id`` (= ``current_assignment_id``) and
        # ``attached_task_id`` (= ``current_task_id``); ``attached_task_id`` also
        # leaves the ``state_patches`` projection in this wave, because a patch
        # lane that kept emitting a key the full rebuild dropped would make the
        # two lanes disagree about the row's shape. Four were writer-less since
        # the worker/goal lanes died, with no consumer past a Launcher model copy:
        # ``context_receipt_id``, ``compression_receipt_id``, ``tool_budget_used``,
        # ``watchdog_warning_count`` (these four also leave ``PersonaInstance``
        # itself; ``serde._coerce`` ignores the stale keys still on disk).
        #
        # Two fields the deferred-debt ledger grouped with them deliberately DID
        # NOT move, and the distinction is the point of this entry: writer-less is
        # not the same as reader-less. ``token_budget_used`` feeds the Launcher's
        # token-total fallback (``totalTokens ?? tokenBudgetUsed``) and
        # ``last_heartbeat_at`` is read by the Launcher's roster-recency tiebreak
        # AND re-emitted on the Agent Gateway state frame AND read here by
        # ``classify_orphan_persona_instances`` as the heartbeat HOLD. Dropping
        # either would silently retire a live consumer, so retiring them is a
        # reader-side decision that needs its own ruling — not a wire cleanup.
        # This is a wire removal, so the contract and Launcher pin move together
        # under the removal rule recorded at 46.
        #
        # 54 KEPT (running_work contract/ambient split, 2026-08-09) — the
        # ``running_work`` section stops folding MACHINE-LOCAL state into its
        # per-source ``detail`` prose, gains a typed ``live_enrichment_error`` on
        # a source entry, and gains a sibling ``ambient`` block naming the
        # resolved background-work home. The two-part rule recorded at "52 KEPT"
        # answers it, and the FIRST part needs stating carefully, because a key
        # does stop appearing on some entries:
        #
        # (a) Nothing LEAVES the wire. ``detail`` is not removed — it keeps its
        # key, its type and its documented meaning ("bounded operator detail,
        # WHEN hermes attached one"), and on the entries a consumer actually
        # renders it from (``unavailable`` lanes, where it has always been a bare
        # exception class name) it is byte-identical. Its presence was ALREADY
        # conditional on both sides: three of the five lanes ship ``ok`` entries
        # with no ``detail`` today, and the Launcher models it as
        # ``_string(json['detail'], fallback: '')``. What changes is which values
        # an optional diagnostic takes on the ``ok`` entries of two lanes — not
        # the schema. That is categorically different from 46/49/53/54, each of
        # which deleted a key or a section a consumer modelled and could no
        # longer find.
        #
        # (b) The additions are "merely unread", not "invisible". ``sources`` is
        # parsed as a MAP with named-key reads — the constraint the WP-H2 ruling
        # put on the consumer, for exactly this reason — so an unknown
        # ``live_enrichment_error`` is ignored; ``ambient`` is a new sibling key
        # on a section the Launcher already parses field-by-field, so a
        # pinned-54 reader ignores it rather than fail-closing.
        #
        # Why it had to move at all: the old ``detail`` concatenated contract
        # with ambient filesystem state, and the filesystem half was perturbed by
        # the projection's OWN lazy import (``_collect_chat_turns`` reaches a
        # tool singleton whose constructor creates ``state.db``), so the same
        # producer emitted two different strings for identical work depending on
        # import order. A wire field no consumer can rely on and no test can pin
        # honestly is worse than no field.
        #
        # The number itself lives at module scope as
        # :data:`SNAPSHOT_CONTRACT_VERSION` so that consumers derive it instead
        # of restating it; the history above stays here, where the rulings are.
        "contract_version": SNAPSHOT_CONTRACT_VERSION,
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


def board_summary_row(
    board,
    cards,
    *,
    card_unpublished=None,
    board_unpublished: bool | None = None,
    orphaned: bool = False,
) -> dict:
    """ONE board projection row — the single authority for the board card
    projection bound, the card-body masking and the truncation accounting.

    Extracted from ``_boards_summary``'s loop body verbatim (S48, ledger item
    4) so the ``hermes harness board`` CLI tier renders the SAME row instead of
    hand-rolling a second, unmasked, uncapped one. ``cards`` is the caller's
    already-fetched active card list (the CLI needs it for its own non-done
    count, and a second store read would be a second answer).

    ``card_unpublished`` is the caller-owned per-card baseline probe; a caller
    with no realm baseline passes ``None`` and the per-card flag is omitted
    entirely, exactly as the un-extracted loop did.
    """

    projected = cards[:MAX_BOARD_CARDS_PROJECTED]
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
        "cards": [
            _board_card_row(c, unpublished=(card_unpublished(c) if card_unpublished is not None else None))
            for c in projected
        ],
        "active_card_count": len(cards),
        "cards_truncated": max(0, len(cards) - len(projected)),
        "conflict_card_ids": _board_conflict_card_ids(board.board_id),
        "archived_card_ids": list(board.archived_card_ids),
        # A board whose workspace no longer resolves is accounted, never
        # silently hidden (repair via archive) — parity warning below.
        "orphaned": orphaned,
    }
    # Publication honesty is only meaningful for realm-bound boards; omit the
    # flag entirely otherwise (mirrors the office ``unpublished=None`` posture).
    if board_unpublished is not None:
        row["unpublished"] = board_unpublished
    return row


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

        boards.append(
            board_summary_row(
                board,
                cards,
                card_unpublished=_card_unpublished,
                board_unpublished=board_unpublished,
                orphaned=board.workspace_id not in workspace_ids,
            )
        )
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


def office_summary_row(
    surface,
    actors,
    *,
    conflict_actor_keys=(),
    actor_unpublished=None,
    orphaned: bool = False,
) -> dict:
    """ONE Mission Office surface projection row — the single authority for the
    actor projection bound and its truncation accounting.

    Extracted from ``_offices_summary``'s loop body verbatim (S48, ledger item
    4), same reason as :func:`board_summary_row`: the ``hermes harness office``
    CLI tier rendered its own uncapped actor list. ``actors`` and
    ``conflict_actor_keys`` are the caller's already-fetched store reads.
    """

    projected = actors[:MAX_OFFICE_ACTORS_PROJECTED]
    return {
        "workspace_id": surface.workspace_id,
        "folders": list(surface.folders),
        "actors": [
            _office_actor_summary_row(a, unpublished=(actor_unpublished(a) if actor_unpublished is not None else None))
            for a in projected
        ],
        "actor_count": len(actors),
        "actors_truncated": max(0, len(actors) - len(projected)),
        "conflict_actor_keys": list(conflict_actor_keys),
        "archived_actor_keys": list(surface.archived_actor_keys),
        "revision": surface.revision,
        "updated_at": to_jsonable(surface.updated_at),
        # A surface whose workspace no longer resolves is accounted, never
        # silently hidden — parity warning below.
        "orphaned": orphaned,
    }


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
            office_summary_row(
                surface,
                actors,
                conflict_actor_keys=office_store.conflict_actor_keys(workspace_token),
                actor_unpublished=_actor_unpublished,
                orphaned=surface.workspace_id not in workspace_ids,
            )
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

    # B5 (2026-07-31): two more warnings stood here — ``open_tasks_without_task_rows``
    # (keyed on ``summary.open_tasks``) and ``open_incident_budget_exceeded``
    # (keyed on ``summary.open_incidents``, gated by the
    # ``open_incident_warning_threshold`` config knob). Neither key has existed
    # on ``summary`` since the mission-row removal (S9/contract 45) reduced the
    # block to ``persona_instances``, so both branches were unreachable by
    # construction: an operator tuning the threshold was tuning nothing, and the
    # Launcher's ``open_incident_budget_exceeded`` alert could never fire. The
    # config knob went with them. ``SnapshotSummary`` now declares the block's
    # field set so the next warning cannot be written against a field the
    # summary does not emit, and
    # ``tests/agent_runtime/test_parity_warning_catalog.py`` fails if any
    # warning code in this module stops being producible.
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

        # Watermark derivation lives in ``read_model.snapshot_watermark`` — this
        # site used to fall back to ``{}`` where the projector fell back to a
        # measured ``events_watermark()``, so one frame could be stamped
        # caught-up-at-offset-0 or correctly depending on which writer got it.
        ReadModel().apply_full_rebuild(snapshot)
    return snapshot


def _default_persona_session_db():
    # Same acquisition as the projection lane it feeds — see
    # ``chat_session_scope`` for the resolution ladder.
    from .chat_session_scope import open_chat_session_db

    return open_chat_session_db()


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
    agents = AgentStore().list_all()
    personas_by_id = {str(getattr(a, "id", "") or ""): a for a in agents}
    for instance in PersonaInstanceStore(event_log=event_log).ensure_for_personas(agents):
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
    persona_instances=(),
    active_id: str | None = None,
) -> dict:
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
        # S47 removed ``"goals": len(goals)``. It counted an always-empty seed,
        # so the wire published a permanent 0 that the Launcher rendered as a
        # count and gated workspace deletion on (ledger item 5).
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
        # Deferred: ``tool_permissions`` imports ``tool_visibility``, which this
        # module already imports at load time; keeping the reader local avoids
        # widening the module-load graph for one scalar fallback.
        from .tool_permissions import default_permission_mode

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
                # Fallback follows the RUNTIME DEFAULT, not a bounded literal: the
                # agents drawer must not render a posture no turn actually runs
                # under (2026-08-09 ruling). ``blocked_tools_count`` moves 22 → 17
                # under the unbounded default — the registry-hygiene names, which
                # never yield to a mode.
                "permission_mode": tool_resolution.get("permission_mode") or default_permission_mode(),
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
