from __future__ import annotations

import json
import re
from datetime import datetime

from hermes_cli.profiles import available_profile_templates
from hermes_time import now
from utils import atomic_json_write

from . import paths
from .blueprints.runs import BlueprintRunStore, blueprint_run_summary
from .blueprints.store import BlueprintStore, blueprint_summary
from .budget_approval import budget_incident_can_continue, budget_incident_needs_scope_recovery
from .capabilities import capability_descriptors
from .config import configured_personas, load_agent_runtime_config
from .daemon import read_daemon_status
from .decision_contract_registry import CONTRACT_SCHEMA_VERSION, contract_hash, event_catalog
from .dirty_state import build_dirty_state
from .events import EventLog
from .migrations import effective_config_summary, migration_status
from .mission_plan import mission_plan_summary
from .observability import build_observability
from .persona_assignments import (
    ACTIVE_ASSIGNMENT_STATES,
    PersonaAssignmentStore,
    PersonaInstanceStore,
    persona_assignment_store_enabled,
    persona_assignment_summary,
    persona_instance_runtime_enabled,
    persona_instance_summary,
)
from .persona_chat_history import persona_chat_history_summary
from .personas import blocked_tool_names, effective_toolsets
from .profile_readiness import profile_readiness_for_persona
from .proof_gates import task_verdict_proof_satisfied
from .repo_bundles import RepoBundleStore, bundle_queue_summary, qa_waiting_on, repo_bundle_summary, simplified_phase_for_task
from .runtime_instances import GoalRuntimeInstanceStore, runtime_instance_summary, runtime_instances_summary
from .repo_context import resolve_affected_repo_workdir
from .role_checklists import RoleChecklist, RoleChecklistStore, checklist_summary
from .role_envelopes import RoleEnvelope, RoleEnvelopeStore, role_envelope_summary
from .proof_batches import ProofBatch, ProofBatchStore, proof_batch_summary
from .scope_control import issue_discovery_counts, untriaged_issue_discoveries
from .state_machine import MissionStateMachine
from .serde import from_jsonable, to_jsonable
from .self_test_evidence import SelfTestEvidenceStore, self_test_summary
from .states import RunState, StageStatus, TaskState
from .store import ACTIVE_RUN_STATES, AgentStore, IncidentStore, ProofStore, RunStore, TaskStore
from .worker_sessions import WorkerSessionStore, worker_session_summary


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


def build_snapshot(task_store=None, run_store=None, agent_store=None, proof_store=None, incident_store=None, event_log=None, worker_session_store=None) -> dict:
    task_store = task_store or TaskStore()
    run_store = run_store or RunStore()
    agent_store = agent_store or AgentStore()
    proof_store = proof_store or ProofStore()
    incident_store = incident_store or IncidentStore()
    event_log = event_log or EventLog()
    worker_session_store = worker_session_store or WorkerSessionStore(event_log=event_log)
    tasks = task_store.list_all()
    runs = run_store.list_all()
    workers = worker_session_store.list_all()
    cfg = load_agent_runtime_config()
    agents = agent_store.list_all() or configured_personas(cfg)
    incidents = incident_store.list_all()
    role_envelope_store = RoleEnvelopeStore(event_log=event_log)
    role_checklist_store = RoleChecklistStore(event_log=event_log)
    proof_batch_store = ProofBatchStore(event_log=event_log)
    repo_bundle_store = RepoBundleStore(event_log=event_log)
    runtime_instance_store = GoalRuntimeInstanceStore(event_log=event_log)
    blueprint_run_store = BlueprintRunStore()
    role_envelopes = role_envelope_store.list_all()
    role_checklists = role_checklist_store.list_all()
    proof_batches = proof_batch_store.list_all()
    repo_bundles = repo_bundle_store.list_all()
    runtime_instances = runtime_instance_store.list_all()
    blueprint_runs = blueprint_run_store.list_all()
    agent_summaries = [_agent_summary(a) for a in agents]
    available_personas = _available_persona_summary(agents)
    proofs = []
    self_tests = []
    for task in tasks:
        proofs.extend(proof_store.list_for_task(task.id))
        self_tests.extend(SelfTestEvidenceStore(event_log=event_log).list_for_task(task.id))
    daemon_status = read_daemon_status()
    recent_events = event_log.tail(20)
    active_runs = [run for run in runs if run.state in ACTIVE_RUN_STATES]
    active_workers = worker_session_store.find_active()
    running_runs = [run for run in active_runs if run.state == RunState.RUNNING]
    queued_runs = [run for run in active_runs if run.state == RunState.QUEUED]
    waiting_runs = [run for run in active_runs if run.state in {RunState.WAITING_ON_TOOL, RunState.WAITING_ON_APPROVAL}]
    dirty_state = build_dirty_state(tasks=tasks, runs=runs, incidents=incidents, workers=workers, runtime_instances=runtime_instances)
    persona_assignments = []
    persona_instances = []
    if persona_instance_runtime_enabled(cfg):
        instance_store = PersonaInstanceStore(event_log=event_log)
        persona_instances = instance_store.derive_from_workers(agents, workers)
        if persona_assignment_store_enabled(cfg):
            persona_assignments = PersonaAssignmentStore(event_log=event_log).list_all()
    data = {
        "schema_version": 1,
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
        "daemon": daemon_status,
        "foreground_runtime": runtime_instances_summary(runtime_instances),
        "execution_mode": cfg.execution_mode,
        "runtime_config": effective_config_summary(cfg),
        "migration": migration_status(),
        "capabilities": capability_descriptors(),
        "observability": build_observability(tasks=tasks, runs=runs, incidents=incidents, proofs=proofs, daemon_status=daemon_status, events=recent_events, execution_mode=cfg.execution_mode, worker_sessions=workers),
        "repo_scopes": _repo_scopes_summary(),
        "tasks": [
            _task_summary(
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
            )
            for t in tasks
        ],
        "archived_tasks": _archived_task_summaries(),
        "agents": agent_summaries,
        "available_personas": available_personas,
        "blueprints": [blueprint_summary(bp) for bp in BlueprintStore().list()],
        "blueprint_runs": [blueprint_run_summary(record) for record in blueprint_runs[-50:]],
        "runtime_paths_diagnostic": _runtime_paths_diagnostic(available_personas),
        "worker_sessions": [worker_session_summary(worker) for worker in workers],
        "role_envelopes": [role_envelope_summary(item, checklist_store=role_checklist_store) for item in role_envelopes],
        "role_checklists": [checklist_summary(item) for item in role_checklists],
        "proof_batches": [proof_batch_summary(item) for item in proof_batches],
        "repo_bundles": [repo_bundle_summary(item) for item in repo_bundles],
        "bundle_queue": bundle_queue_summary(repo_bundles),
        "runtime_instances": [runtime_instance_summary(item) for item in runtime_instances],
        "event_contracts": event_catalog(),
        "runs": [_run_summary(r) for r in runs],
        "incidents": [_incident_summary(i) for i in incidents],
        "proofs": [_proof_summary(p) for p in proofs],
        "self_tests": [self_test_summary(item) for item in self_tests],
    }
    if persona_instance_runtime_enabled(cfg):
        data["persona_instance_runtime"] = {
            "enabled": True,
            "assignment_store_enabled": persona_assignment_store_enabled(cfg),
        }
        data["persona_instances"] = [persona_instance_summary(instance) for instance in persona_instances]
        data["persona_chat_history"] = persona_chat_history_summary(
            persona_instances=persona_instances,
        )
        data["persona_assignments"] = {
            "active": [persona_assignment_summary(item) for item in persona_assignments if item.state in ACTIVE_ASSIGNMENT_STATES],
            "recent": [persona_assignment_summary(item) for item in persona_assignments[-50:]],
        }
    else:
        data["persona_instance_runtime"] = {"enabled": False}
    return data


def write_snapshot(snapshot: dict | None = None) -> dict:
    snapshot = snapshot or build_snapshot()
    atomic_json_write(paths.snapshot_path(), to_jsonable(snapshot), indent=2, sort_keys=True)
    return snapshot


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
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
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
    recent_events = _archived_transcript_events(task_id, runs, proofs)
    persona_timing_summaries = _persona_timing_summaries(runs)
    return {
        "task_id": task_id,
        "title": str(raw.get("title") or "Archived mission"),
        "state": "archived",
        "original_state": original_state,
        "archive_batch": archive_batch,
        "archive_reason": reason,
        "archived_at": archived_at,
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
                "task_id": raw.get("task_id"),
                "stage_id": raw.get("stage_id"),
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
        "decision_type": llm.get("decision_type") or final_decision.get("type"),
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
        role_events = [event for event in events if str(event.get("persona_id") or "") == role_id]
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
    summary = _safe_text(event.get("summary"))
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


def _task_summary(task, proofs, all_tasks=None, incidents=None, runs=None, events=None, workers=None, run_store=None, self_tests=None, role_envelopes=None, role_checklists=None, proof_batches=None, persona_assignments=None, repo_bundles=None, runtime_instances=None):
    gate = task_verdict_proof_satisfied(task, proofs)
    current = next((s for s in task.stages if s.id == task.current_stage_id), None)
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
    next_action = _next_action_summary(task, open_incidents, run_store=run_store)
    assignments = list(persona_assignments or [])
    bundles = list(repo_bundles or [])
    runtime_lane = _runtime_lane_summary(runtime_instances or [])
    return {
        "task_id": task.id,
        "title": task.title,
        "state": str(task.state),
        "parent_task_id": task.parent_task_id,
        "child_task_count": child_count,
        "current_stage_id": task.current_stage_id,
        "current_stage_title": current.title if current else None,
        "simplified_phase": simplified_phase_for_task(task, bundles),
        "active_assignment_id": next((assignment.id for assignment in assignments if assignment.state in ACTIVE_ASSIGNMENT_STATES), None),
        "repo_bundle_ids": [bundle.id for bundle in bundles],
        "repo_bundles": [repo_bundle_summary(bundle) for bundle in bundles],
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
        "missing_proof": gate.missing,
        "open_incident_count": len(task.open_incident_ids) or len(open_incidents),
        "issue_discovery_counts": issue_discovery_counts(task),
        "untriaged_issue_severities": sorted({str(item.get("severity", "medium")) for item in untriaged}),
        "proof_summaries": [_proof_visibility_summary(proof) for proof in proofs],
        "self_test_summaries": [self_test_summary(item) for item in (self_tests or [])],
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
        cfg = load_agent_runtime_config()
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


def _next_action_summary(task, open_incidents, *, run_store=None):
    if open_incidents:
        runs = run_store or RunStore()
        cfg = load_agent_runtime_config()
        if any(budget_incident_needs_scope_recovery(incident, runs) for incident in open_incidents):
            return {**_stopped_progress(task, open_incidents, "self_heal_pending", "neko_supervisor"), "action": "run_slot", "reason": "Dev exhausted read/search without patch or proof; Neko must split or narrow the stage before retry"}
        if any(budget_incident_can_continue(incident, runs, cap=cfg.neko_extension_cap) for incident in open_incidents):
            return {**_stopped_progress(task, open_incidents, "self_heal_pending", "neko_supervisor"), "action": "run_slot", "reason": "needs Neko approval to continue budget-limited Dev run"}
        reason = "budget_continuation_blocked" if _has_budget_incident(open_incidents) else "environment_blocked"
        message = "budget continuation cap reached; human review required" if reason == "budget_continuation_blocked" else "open incident requires human review"
        return {**_stopped_progress(task, open_incidents, reason, "human"), "action": "blocked_by_incident", "reason": message}
    if task.state in {TaskState.DONE, TaskState.CANCELLED}:
        return {**_stopped_progress(task, [], "settled", "harness"), "action": "terminal", "reason": "mission is terminal"}
    cfg = load_agent_runtime_config()
    action = MissionStateMachine(config=cfg).next_action(task)
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
        try:
            from .ticker import _dev_persona_id_for_task

            return _dev_persona_id_for_task(task, run_store=run_store)
        except Exception:
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
        for event in events
        if event.task_id == task_id
    ][-20:]


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
        role_events = [event for event in events if event.task_id == task.id and event.persona_id == role_id]
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
        stages = list(getattr(task, "stages", []) or [])
    streams: list[dict] = []
    for stage in stages:
        stage_id = getattr(stage, "id", None)
        if not stage_id:
            continue
        stage_events = [
            event
            for event in events
            if event.task_id == task.id and (event.payload or {}).get("stage_id") == stage_id
        ]
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
    for key in ("phase", "step", "status", "decision_type", "reasoning_summary"):
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


def _agent_summary(agent):
    readiness = profile_readiness_for_persona(agent)
    return {
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
        "blocked_tools_count": len(blocked_tool_names(agent)),
        "model_configured": bool(agent.model),
        "provider_configured": bool(agent.provider),
        "autonomy": agent.autonomy,
        "core_context_files": "enabled" if getattr(agent, "include_core_context_files", False) else "isolated",
        "repo_scope_label": _safe_text(getattr(agent, "repo_scope_label", None)) or _safe_repo_scope_label(getattr(agent, "repo_scope", None)),
    }


def _available_persona_summary(agents) -> list[dict]:
    try:
        templates = available_profile_templates()
    except Exception:
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
        "decision_type": llm.get("decision_type") or decision_type,
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
        "response_len", "validation_status", "decision_type",
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


def _safe_text(value):
    if not isinstance(value, str):
        return None
    text = " ".join(value.strip().split())
    if not text or _looks_sensitive_or_pathish(text):
        return None
    if len(text) > 500:
        return f"{text[:497]}…"
    return text


def _looks_sensitive_or_pathish(value: str) -> bool:
    lowered = value.lower()
    if any(marker in lowered for marker in ("secret", "token", "password", "api_key", "apikey", "authorization", "bearer", "credential", "cookie", "private_key", "sk-")):
        return True
    if ":/" in value or "\\" in value or value.startswith(("/", "~")):
        return True
    if re.search(r"(^|\s)([A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+", value):
        return True
    return False


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
