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
    persona_assignment_store_enabled,
    persona_assignment_summary,
    persona_instance_runtime_enabled,
    persona_instance_summary,
)
from .persona_chat_history import DEFAULT_PERSONA_CHAT_MESSAGE_TAIL, persona_chat_history_summary, persona_chat_trace_summary
from .profile_readiness import profile_readiness_for_persona
from .provider_health import provider_health_for_personas
from .migrations import effective_config_summary
from .production_envelope import production_envelope_status
from .repo_bundles import RepoBundleStore, bundle_queue_summary, repo_bundle_delivery_summary, repo_bundle_summary, repo_lock_summary
from .runtime_instances import GoalRuntimeInstanceStore, runtime_instances_summary
from .states import RunState
from .store import ACTIVE_RUN_STATES, AgentStore, IncidentStore, RunStore, TaskStore
from .worker_sessions import WorkerSessionStore, worker_session_summary
from .snapshot import _default_persona_session_db, _parity_envelope


def build_status(task_store: TaskStore | None = None, run_store: RunStore | None = None, incident_store: IncidentStore | None = None, agent_store: AgentStore | None = None, event_log: EventLog | None = None, worker_session_store: WorkerSessionStore | None = None) -> dict:
    _build_started = time.perf_counter()
    run_store = run_store or RunStore()
    incident_store = incident_store or IncidentStore()
    agent_store = agent_store or AgentStore()
    # Status calls persona_chat_trace/for_session dozens of times on the same
    # log; CachedEventLog reads events.jsonl once and serves all of them from
    # memory (same as build_snapshot — this was the 2s hog of a warm status).
    event_log = event_log or CachedEventLog()
    worker_session_store = worker_session_store or WorkerSessionStore(event_log=event_log)
    tasks = []
    runs = run_store.list_all()
    workers = worker_session_store.list_all()
    incidents = incident_store.list_all()
    cfg = load_agent_runtime_config()
    # The background Mission Daemon was retired; execution is always operator/
    # goal-runner driven ("manual").
    execution_mode = "manual"
    agents = agent_store.list_all() or ensure_persisted_personas(cfg)
    repo_bundles = RepoBundleStore(event_log=event_log).list_all()
    runtime_instances = GoalRuntimeInstanceStore(event_log=event_log).list_all()
    active_runs = [r for r in runs if r.state in ACTIVE_RUN_STATES]
    queued_runs = [r for r in active_runs if r.state == RunState.QUEUED]
    waiting_runs = [r for r in active_runs if r.state in {RunState.WAITING_ON_TOOL, RunState.WAITING_ON_APPROVAL}]
    active_workers = worker_session_store.find_active()
    dirty_state = build_dirty_state(tasks=tasks, runs=runs, incidents=incidents, workers=workers, runtime_instances=runtime_instances)
    foreground_dirty_state = dirty_state.get("runtime", {}).get("foreground", {})
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
        "open_task_ids": dirty_state.get("runtime", {}).get("open_task_ids", []),
        "background_open_tasks": foreground_dirty_state.get("background_open_tasks", 0),
        "background_task_ids": foreground_dirty_state.get("background_task_ids", []),
        "unparked_open_tasks": foreground_dirty_state.get("unparked_open_tasks", 0),
        "unparked_open_task_ids": foreground_dirty_state.get("unparked_open_task_ids", []),
        "decision_contract_version": CONTRACT_SCHEMA_VERSION,
        "decision_contract_hash": contract_hash(),
        "active_runs": len(active_runs),
        "queued_runs": len(queued_runs),
        "waiting_runs": len(waiting_runs),
        "stale_runs": len([r for r in runs if r.state == RunState.STALE]),
        "open_incidents": len([i for i in incidents if i.closed_at is None]),
        "active_worker_sessions": len(active_workers),
        "dirty": dirty_state["dirty"],
        "dirty_summary": dirty_state["summary"],
        "dirty_state": dirty_state,
        "runtime_health": provider_health_for_personas(agents),
        "runtime_config": effective_config_summary(cfg),
        "production_envelope": production_envelope_status(cfg),
        "swarm": {
            "enabled": bool(getattr(getattr(cfg, "swarm", None), "enabled", False)),
        },
        "recursive_supervision": {
            "enabled": _recursive_supervision_enabled(cfg),
        },
        "foreground_runtime": runtime_instances_summary(runtime_instances),
        "observability": build_observability(runs=runs, incidents=incidents, events=recent_events, execution_mode=execution_mode, worker_sessions=workers),
        "agents": [_agent_status(a) for a in agents],
        "worker_sessions": [worker_session_summary(worker) for worker in workers],
        "repo_bundles": [repo_bundle_summary(bundle) for bundle in repo_bundles],
        "repo_bundle_closeout": repo_bundle_delivery_summary(repo_bundles) if repo_bundles else None,
        "bundle_queue": bundle_queue_summary(repo_bundles),
        "repo_locks": repo_lock_summary(),
        "runtime_instances": runtime_instances_summary(runtime_instances),
        "lanes": runtime_instances_summary(runtime_instances)["lanes"],
        "swarm_budget": _swarm_budget_summary(cfg),
    }
    if persona_instance_runtime_enabled(cfg):
        instance_store = PersonaInstanceStore(event_log=event_log)
        instances = instance_store.derive_from_workers(agents, workers)
        personas_by_id = {str(getattr(agent, "id", "") or ""): agent for agent in agents}
        data["agents"] = [
            *data["agents"],
            *active_persona_instance_agent_summaries(instances, personas_by_id),
        ]
        data["persona_instance_runtime"] = {
            "enabled": True,
            "assignment_store_enabled": persona_assignment_store_enabled(cfg),
        }
        data["persona_instances"] = [persona_instance_summary(instance) for instance in instances]
        session_db = _default_persona_session_db()
        assignments = (
            PersonaAssignmentStore(event_log=event_log).list_all()
            if persona_assignment_store_enabled(cfg)
            else []
        )
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
            tasks=tasks,
        )
        completeness = {
            "persona_chat_history": history_accountant.summary(),
            "persona_chat_trace": trace_accountant.summary(),
        }
        drop_samples = history_accountant.drop_samples() + trace_accountant.drop_samples()
        if persona_assignment_store_enabled(cfg):
            data["persona_assignments"] = {
                "active": [persona_assignment_summary(item) for item in assignments if item.state in {"queued", "assigned", "running", "waiting_on_tool", "waiting_on_proof", "needs_input"}],
                "recent": [persona_assignment_summary(item) for item in assignments[-25:]],
            }
    else:
        data["persona_instance_runtime"] = {"enabled": False}
        completeness = {}
        drop_samples = []
    data["parity"] = _parity_envelope(
        data,
        build_started=_build_started,
        last_event=recent_events[-1] if recent_events else None,
        recent_events=recent_events,
        completeness=completeness,
        drop_samples=drop_samples,
    )
    return data


def _swarm_budget_summary(cfg) -> dict:
    swarm = getattr(cfg, "swarm", None)
    soft_tokens = int(getattr(swarm, "global_token_soft_limit", 0) or 0)
    hard_tokens = int(getattr(swarm, "global_token_hard_limit", 0) or 0)
    soft_calls = int(getattr(swarm, "global_api_call_soft_limit", 0) or 0)
    hard_calls = int(getattr(swarm, "global_api_call_hard_limit", 0) or 0)
    return {
        "state": "ok",
        "global": {"total_tokens": 0, "api_calls": 0},
        "limits": {
            "global_token_soft_limit": soft_tokens,
            "global_token_hard_limit": hard_tokens,
            "global_api_call_soft_limit": soft_calls,
            "global_api_call_hard_limit": hard_calls,
            "per_lane_token_limit": int(getattr(swarm, "per_lane_token_limit", 0) or 0),
            "per_lane_api_call_limit": int(getattr(swarm, "per_lane_api_call_limit", 0) or 0),
        },
        "by_task": {},
    }


def _recursive_supervision_enabled(cfg) -> bool:
    supervision = getattr(cfg, "supervision", None)
    swarm = getattr(cfg, "swarm", None)
    return any(
        [
            bool(getattr(supervision, "child_events_enabled", False)),
            bool(getattr(supervision, "recursive_enabled", False)),
            bool(getattr(supervision, "hierarchical_budget_enabled", False)),
            bool(getattr(supervision, "deploy_verification_enabled", False)),
            bool(getattr(swarm, "enabled", False)),
        ]
    )


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
