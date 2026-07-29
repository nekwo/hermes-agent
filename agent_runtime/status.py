from __future__ import annotations

import time

from .config import ensure_persisted_personas, load_agent_runtime_config, load_root_runtime_config
from .budget_approval import budget_incident_can_continue, budget_incident_needs_scope_recovery
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
from .incidents import RUN_BUDGET_EXCEEDED
from .migrations import effective_config_summary
from .production_envelope import production_envelope_status
from .repo_bundles import RepoBundleStore, bundle_queue_summary, repo_bundle_delivery_summary, repo_bundle_summary, repo_lock_summary
from .runtime_instances import GoalRuntimeInstanceStore, runtime_instances_summary
from .states import RunState, TaskState
from .store import ACTIVE_RUN_STATES, AgentStore, IncidentStore, RunStore, TaskStore
from .worker_sessions import WorkerSessionStore, worker_session_summary
from .snapshot import _default_persona_session_db, _parity_envelope


def build_status(task_store: TaskStore | None = None, run_store: RunStore | None = None, incident_store: IncidentStore | None = None, agent_store: AgentStore | None = None, event_log: EventLog | None = None, worker_session_store: WorkerSessionStore | None = None) -> dict:
    _build_started = time.perf_counter()
    task_store = task_store or TaskStore()
    run_store = run_store or RunStore()
    incident_store = incident_store or IncidentStore()
    agent_store = agent_store or AgentStore()
    # Status calls persona_chat_trace/for_session dozens of times on the same
    # log; CachedEventLog reads events.jsonl once and serves all of them from
    # memory (same as build_snapshot — this was the 2s hog of a warm status).
    event_log = event_log or CachedEventLog()
    worker_session_store = worker_session_store or WorkerSessionStore(event_log=event_log)
    tasks = task_store.list_all()
    runs = run_store.list_all()
    workers = worker_session_store.list_all()
    incidents = incident_store.list_all()
    cfg = load_agent_runtime_config()
    # The background Mission Daemon was retired; execution is always operator/
    # goal-runner driven ("manual").
    execution_mode = "manual"
    agents = agent_store.list_all() or ensure_persisted_personas(cfg)
    proofs = []
    repo_bundles = RepoBundleStore(event_log=event_log).list_all()
    runtime_instances = GoalRuntimeInstanceStore(event_log=event_log).list_all()
    open_incident_task_ids = {i.task_id for i in incidents if i.closed_at is None and i.task_id}
    open_incidents_by_task = {}
    for incident in incidents:
        if incident.closed_at is None and incident.task_id:
            open_incidents_by_task.setdefault(incident.task_id, []).append(incident)
    active_runs = [r for r in runs if r.state in ACTIVE_RUN_STATES]
    running_runs = [r for r in active_runs if r.state == RunState.RUNNING]
    queued_runs = [r for r in active_runs if r.state == RunState.QUEUED]
    waiting_runs = [r for r in active_runs if r.state in {RunState.WAITING_ON_TOOL, RunState.WAITING_ON_APPROVAL}]
    active_workers = worker_session_store.find_active()
    dirty_state = build_dirty_state(tasks=tasks, runs=runs, incidents=incidents, workers=workers, runtime_instances=runtime_instances)
    foreground_dirty_state = dirty_state.get("runtime", {}).get("foreground", {})
    recent_events = event_log.tail(20)
    data = {
        "open_tasks": len([t for t in tasks if t.state not in {TaskState.DONE, TaskState.CANCELLED}]),
        "open_task_ids": dirty_state.get("runtime", {}).get("open_task_ids", []),
        "background_open_tasks": foreground_dirty_state.get("background_open_tasks", 0),
        "background_task_ids": foreground_dirty_state.get("background_task_ids", []),
        "unparked_open_tasks": foreground_dirty_state.get("unparked_open_tasks", 0),
        "unparked_open_task_ids": foreground_dirty_state.get("unparked_open_task_ids", []),
        "decision_contract_version": CONTRACT_SCHEMA_VERSION,
        "decision_contract_hash": contract_hash(),
        "active_runs": len(active_runs),
        "running_runs": len(running_runs),
        "queued_runs": len(queued_runs),
        "waiting_runs": len(waiting_runs),
        "stale_runs": len([r for r in runs if r.state == RunState.STALE]),
        "blocked_tasks": len([t for t in tasks if t.state == TaskState.BLOCKED]),
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
        "observability": build_observability(tasks=tasks, runs=runs, incidents=incidents, proofs=proofs, daemon_status=None, events=recent_events, execution_mode=execution_mode, worker_sessions=workers),
        "next_actions": [
            _next_action(
                t,
                blocked=t.id in open_incident_task_ids,
                incidents=open_incidents_by_task.get(t.id, []),
                run_store=run_store,
                config=cfg,
            )
            for t in tasks
            if t.state not in {TaskState.DONE, TaskState.CANCELLED}
        ],
        "agents": [_agent_status(a) for a in agents],
        "worker_sessions": [worker_session_summary(worker) for worker in workers],
        "repo_bundles": [repo_bundle_summary(bundle) for bundle in repo_bundles],
        "repo_bundle_closeout": repo_bundle_delivery_summary(repo_bundles) if repo_bundles else None,
        "bundle_queue": bundle_queue_summary(repo_bundles),
        "repo_locks": repo_lock_summary(),
        "runtime_instances": runtime_instances_summary(runtime_instances),
        "lanes": runtime_instances_summary(runtime_instances)["lanes"],
        "swarm_budget": _swarm_budget_summary(runs, cfg),
    }
    # Top-level so an undispatchable mission is visible without reading every
    # `next_actions` entry. Empty list is the healthy steady state; a non-empty
    # one is a broken routing invariant that needs an operator, not a retry.
    data["undispatchable_missions"] = [
        {"task_id": entry.get("task_id"), "reason": entry.get("reason")}
        for entry in data["next_actions"]
        if entry.get("action") == "undispatchable"
    ]
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


def _swarm_budget_summary(runs, cfg) -> dict:
    total_tokens = 0
    api_calls = 0
    by_task: dict[str, dict[str, int]] = {}
    for run in runs:
        llm = getattr(run, "llm", None) if isinstance(getattr(run, "llm", None), dict) else {}
        tokens = int(llm.get("total_tokens") or 0)
        calls = int(llm.get("api_calls") or 0)
        total_tokens += tokens
        api_calls += calls
        task_id = str(getattr(run, "task_id", "") or "")
        if task_id:
            entry = by_task.setdefault(task_id, {"total_tokens": 0, "api_calls": 0})
            entry["total_tokens"] += tokens
            entry["api_calls"] += calls
    swarm = getattr(cfg, "swarm", None)
    soft_tokens = int(getattr(swarm, "global_token_soft_limit", 0) or 0)
    hard_tokens = int(getattr(swarm, "global_token_hard_limit", 0) or 0)
    soft_calls = int(getattr(swarm, "global_api_call_soft_limit", 0) or 0)
    hard_calls = int(getattr(swarm, "global_api_call_hard_limit", 0) or 0)
    state = "ok"
    if (hard_tokens and total_tokens >= hard_tokens) or (hard_calls and api_calls >= hard_calls):
        state = "hard_limit"
    elif (soft_tokens and total_tokens >= soft_tokens) or (soft_calls and api_calls >= soft_calls):
        state = "soft_limit"
    return {
        "state": state,
        "global": {"total_tokens": total_tokens, "api_calls": api_calls},
        "limits": {
            "global_token_soft_limit": soft_tokens,
            "global_token_hard_limit": hard_tokens,
            "global_api_call_soft_limit": soft_calls,
            "global_api_call_hard_limit": hard_calls,
            "per_lane_token_limit": int(getattr(swarm, "per_lane_token_limit", 0) or 0),
            "per_lane_api_call_limit": int(getattr(swarm, "per_lane_api_call_limit", 0) or 0),
        },
        "by_task": by_task,
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


def _next_action(task, *, blocked: bool = False, incidents=None, run_store: RunStore | None = None, config=None):
    if blocked and _has_budget_scope_recovery_path(incidents or [], run_store or RunStore()):
        return {**_stopped_progress(task, incidents or [], "self_heal_pending", "neko_supervisor"), "task_id": task.id, "action": "run_slot", "reason": "Dev exhausted read/search without patch or proof; Neko must split or narrow the stage before retry"}
    if blocked and _has_budget_approval_path(incidents or [], run_store or RunStore()):
        return {**_stopped_progress(task, incidents or [], "self_heal_pending", "neko_supervisor"), "task_id": task.id, "action": "run_slot", "reason": "needs Neko approval to continue budget-limited Dev run"}
    if blocked:
        reason = "budget_continuation_blocked" if _has_budget_incident(incidents or []) else "environment_blocked"
        message = "budget continuation cap reached; human review required" if reason == "budget_continuation_blocked" else "open incident requires human review"
        return {**_stopped_progress(task, incidents or [], reason, "human"), "task_id": task.id, "action": "blocked_by_incident", "reason": message}
    progress = _stopped_progress(task, [], "routing_removed", "human")
    progress["stopped_progress"]["next_action"] = "inspect_blocker"
    return {
        **progress,
        "task_id": task.id,
        "action": "undispatchable",
        "reason": "the task dispatch lane has been retired",
    }


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


def _owner_for_action(action, *, task=None, run_store: RunStore | None = None) -> str:
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


def _has_budget_approval_path(incidents, run_store: RunStore) -> bool:
    for incident in incidents:
        if budget_incident_can_continue(incident, run_store, cap=load_root_runtime_config().neko_extension_cap):
            return True
    return False


def _has_budget_scope_recovery_path(incidents, run_store: RunStore) -> bool:
    for incident in incidents:
        if budget_incident_needs_scope_recovery(incident, run_store):
            return True
    return False


def _has_budget_incident(incidents) -> bool:
    return any(getattr(incident, "kind", None) == RUN_BUDGET_EXCEEDED for incident in incidents)


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
