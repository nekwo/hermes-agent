from __future__ import annotations

import time

from .config import ensure_persisted_personas, load_agent_runtime_config
from .decision_contract_registry import CONTRACT_SCHEMA_VERSION, contract_hash
from .dirty_state import build_dirty_state
from .events import CachedEventLog, EventLog
from .observability import build_observability
from .operator_channels import operator_channel_summary
from .parity import ProjectionAccountant
from .persona_assignments import (
    PersonaAssignmentStore,
    PersonaInstanceStore,
    active_persona_instance_agent_summaries,
    persona_assignment_summary,
    persona_instance_summary,
)
from .persona_chat_history import DEFAULT_PERSONA_CHAT_MESSAGE_TAIL, persona_chat_history_summary, persona_chat_trace_summary
from .persona_lifecycle import is_runtime_persona
from .profile_readiness import profile_readiness_for_persona
from .provider_health import provider_health_for_personas
from .migrations import effective_config_summary
from .runtime_instances import GoalRuntimeInstanceStore, runtime_instances_summary
from .states import RunState
from .store import ACTIVE_RUN_STATES, AgentStore, IncidentStore, RunStore
from .snapshot import _default_persona_session_db, _parity_envelope
from .resolution import runtime_resolution_scope


def build_status(run_store: RunStore | None = None, incident_store: IncidentStore | None = None, agent_store: AgentStore | None = None, event_log: EventLog | None = None) -> dict:
    with runtime_resolution_scope():
        return _build_status_in_runtime_scope(
            run_store=run_store,
            incident_store=incident_store,
            agent_store=agent_store,
            event_log=event_log,
        )


def _build_status_in_runtime_scope(run_store: RunStore | None = None, incident_store: IncidentStore | None = None, agent_store: AgentStore | None = None, event_log: EventLog | None = None) -> dict:
    _build_started = time.perf_counter()
    run_store = run_store or RunStore()
    incident_store = incident_store or IncidentStore()
    agent_store = agent_store or AgentStore()
    # Status calls persona_chat_trace/for_session dozens of times on the same
    # log; CachedEventLog reads events.jsonl once and serves all of them from
    # memory (same as build_snapshot — this was the 2s hog of a warm status).
    event_log = event_log or CachedEventLog()
    runs = run_store.list_all()
    incidents = incident_store.list_all()
    cfg = load_agent_runtime_config()
    # The background Mission Daemon was retired; execution is always operator/
    # goal-runner driven ("manual").
    execution_mode = "manual"
    catalog_agents = agent_store.list_all() or ensure_persisted_personas(cfg)
    agents = [agent for agent in catalog_agents if is_runtime_persona(agent)]
    runtime_instances = GoalRuntimeInstanceStore(event_log=event_log).list_all()
    active_runs = [r for r in runs if r.state in ACTIVE_RUN_STATES]
    queued_runs = [r for r in active_runs if r.state == RunState.QUEUED]
    waiting_runs = [r for r in active_runs if r.state in {RunState.WAITING_ON_TOOL, RunState.WAITING_ON_APPROVAL}]
    dirty_state = build_dirty_state(runs=runs, incidents=incidents, runtime_instances=runtime_instances)
    recent_events = event_log.tail(20)
    data = {
        # S28 completed the S21 cut this comment used to defer: `open_tasks` and
        # `running_runs` are gone, together with the `_cmd_status` print that was
        # their only reader (hermes_cli/harness_parts/runtime_commands.py). Both
        # were constants wearing the shape of a measurement — `tasks` is a `[]`
        # literal, and no production caller constructs an `AgentRun` or opens a
        # run, so the RUNNING count could never move. The `active_runs` /
        # `queued_runs` / `waiting_runs` / `stale_runs` counters below stay:
        # they read the same persisted rows, and a historical row in any of
        # those states is still information about the store.
        "event_contract_version": CONTRACT_SCHEMA_VERSION,
        "event_contract_hash": contract_hash(),
        "active_runs": len(active_runs),
        "queued_runs": len(queued_runs),
        "waiting_runs": len(waiting_runs),
        "stale_runs": len([r for r in runs if r.state == RunState.STALE]),
        "open_incidents": len([i for i in incidents if i.closed_at is None]),
        "dirty": dirty_state["dirty"],
        "dirty_summary": dirty_state["summary"],
        "dirty_state": dirty_state,
        "runtime_health": provider_health_for_personas(agents),
        "runtime_config": effective_config_summary(cfg),
        # S56 (contract 47) removed six wire rows from this frame, each of which
        # could only ever report one value once its producer was gone:
        #   * `worker_sessions` / `active_worker_sessions` — the
        #     WorkerSessionStore write lane went with this commit.
        #   * `repo_bundles` / `repo_bundle_closeout` / `bundle_queue` /
        #     `repo_locks` — S52 deleted every writer that could create a bundle
        #     row or a repo-bundle lock; doc 19 filed `repo_lock_summary()` as
        #     asserted-constant debt at the time.
        #   * `lanes` — a duplicate of `runtime_instances["lanes"]`, empty by
        #     construction since S53 removed the GoalRuntimeInstance writers.
        #   * `production_envelope` / `swarm` / `swarm_budget` — the envelope was
        #     hand-written prose keyed on flags, several of its claims false
        #     against this tree (see the S56 removal contract); `swarm` and
        #     `swarm_budget` echoed a config block nothing enforced.
        # `runtime_instances` KEEPS its own `lanes` sub-key: that block is the
        # projection, and removing the top-level duplicate changes no number a
        # reader could not still get.
        "recursive_supervision": {
            "enabled": _recursive_supervision_enabled(cfg),
        },
        "foreground_runtime": runtime_instances_summary(runtime_instances),
        "observability": build_observability(runs=runs, incidents=incidents, events=recent_events, execution_mode=execution_mode),
        "agents": [_agent_status(a) for a in agents],
        "runtime_instances": runtime_instances_summary(runtime_instances),
    }
    # S56: the persona-instance roster is UNCONDITIONAL. It used to be gated on
    # `enterprise_worker_sessions.enabled AND .persona_instance_runtime`, a
    # misnomer left over from the worker lane — the roster is the identity
    # substrate every Mission Control surface keys on, and the enabled shape has
    # been the only shape for months. The `persona_instance_runtime` WIRE block
    # stays (the Launcher bridge reads it) and now reports the truth
    # unconditionally.
    instance_store = PersonaInstanceStore(event_log=event_log)
    instances = instance_store.ensure_for_personas(agents)
    personas_by_id = {str(getattr(agent, "id", "") or ""): agent for agent in agents}
    data["agents"] = [
        *data["agents"],
        *active_persona_instance_agent_summaries(instances, personas_by_id),
    ]
    data["persona_instance_runtime"] = {
        "enabled": True,
        "assignment_store_enabled": True,
    }
    data["persona_instances"] = [persona_instance_summary(instance) for instance in instances]
    session_db = _default_persona_session_db()
    assignments = PersonaAssignmentStore(event_log=event_log).list_all()
    history_accountant = ProjectionAccountant("persona_chat_history")
    trace_accountant = ProjectionAccountant("persona_chat_trace")
    data["persona_chat_history"] = persona_chat_history_summary(
        persona_instances=instances,
        session_db=session_db,
        message_tail=DEFAULT_PERSONA_CHAT_MESSAGE_TAIL,
        accountant=history_accountant,
        persona_assignments=assignments,
    )
    data["persona_chat_trace"] = persona_chat_trace_summary(
        persona_instances=instances,
        event_log=event_log,
        message_tail=DEFAULT_PERSONA_CHAT_MESSAGE_TAIL,
        accountant=trace_accountant,
    )
    data["operator_channels"] = operator_channel_summary(
        persona_instances=instances,
        persona_chat_history=data["persona_chat_history"],
        persona_chat_trace=data["persona_chat_trace"],
    )
    completeness = {
        "persona_chat_history": history_accountant.summary(),
        "persona_chat_trace": trace_accountant.summary(),
    }
    drop_samples = history_accountant.drop_samples() + trace_accountant.drop_samples()
    data["persona_assignments"] = {
        "active": [persona_assignment_summary(item) for item in assignments if item.state in {"queued", "assigned", "running", "waiting_on_tool", "waiting_on_proof", "needs_input"}],
        "recent": [persona_assignment_summary(item) for item in assignments[-25:]],
    }
    data["parity"] = _parity_envelope(
        data,
        build_started=_build_started,
        last_event=recent_events[-1] if recent_events else None,
        recent_events=recent_events,
        completeness=completeness,
        drop_samples=drop_samples,
    )
    return data


def _recursive_supervision_enabled(cfg) -> bool:
    # S56 pruned `supervision` to its one live field. The three other flags this
    # OR-chain read (`recursive_enabled`, `hierarchical_budget_enabled`,
    # `deploy_verification_enabled`) and `swarm.enabled` were config the runtime
    # never consulted anywhere else, so they could only ever widen this answer
    # without anything acting on it. `child_events_enabled` is real: continuity.py
    # reads it to decide whether child.* events are emitted.
    supervision = getattr(cfg, "supervision", None)
    return bool(getattr(supervision, "child_events_enabled", False))


# S21: the `next_actions` routing chain (`_next_action`, `_stopped_progress`,
# `_owner_for_action`, and the three `_has_budget_*` predicates) was removed
# here. It answered "what should the harness do with this task next?" for a
# dispatch lane retired in S5, over a `tasks` list that has been a `[]` literal
# since S8 — so the whole chain could only ever produce `[]`. `_owner_for_action`
# had already had zero callers even before that. `snapshot.py` keeps its own
# `_stopped_progress` / `_has_budget_incident`; those are a separate lineage on
# archived rows and are untouched.


def _agent_status(agent):
    readiness = profile_readiness_for_persona(agent)
    return {
        "persona_id": agent.id,
        "hermes_profile": agent.hermes_profile,
        "profile_readiness": readiness["readiness"],
        "readiness_summary": readiness["summary"],
        "missing_skills": readiness.get("missing_skills", []),
        "skill_hash_mismatches": readiness.get("skill_hash_mismatches", []),
        "required_mcp_servers": readiness.get("required_mcp_servers", []),
        "effective_required_mcp_servers": readiness.get("effective_required_mcp_servers", []),
        "missing_mcp_servers": readiness.get("missing_mcp_servers", []),
        "core_context_files": "enabled" if getattr(agent, "include_core_context_files", False) else "isolated",
    }
