from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_default_hermes_root
from hermes_time import now
from utils import atomic_json_write

from . import paths
from .goal_hygiene import prepare_new_goal_runtime
from .models import Task
from .budget_approval import budget_incident_can_continue
from .errors import LegacyOrchestratorRemoved
from .events import EventLog
from .serde import to_jsonable
from .snapshot import build_snapshot
from .states import TaskState
from .status import build_status
from .store import IncidentStore, ProofStore, RunStore, TaskStore


STAGE47_SUITE = "aaa-stage47"
CERTIFICATION_TARGET_CONSECUTIVE_GREEN = 10

STAGE47_CASES: dict[str, dict[str, Any]] = {
    "noop-orchestration": {
        "title": "Stage 47 no-op orchestration burn-in",
        "description": "Prove Neko Mission Lead, Backend Dev, and Launcher Dev default routing without product edits.",
        "acceptance_criteria": [
            "Backend Dev receives the first no-op route and attaches proof without modifying product files.",
            "The default graph releases Launcher Dev only after backend proof exists.",
            "Launcher Dev receives the second no-op route and attaches proof without modifying product files.",
            "The default graph reaches done without adding a QA leg.",
            "No invalid-output incidents occur.",
        ],
        "affected_repos": ["EterniaBackend", "EterniaLauncher", "hermes-agent"],
        "suggested_roles": ["neko_supervisor", "backend_dev", "dev"],
        "risk_flags": ["cross_stack_sequential_handoff", "backend_contract_first", "routing_burn_in_only", "no_product_edits", "default_no_qa"],
        "non_goals": ["No product code edits.", "No repository refactors.", "No visual proof required.", "No live product behavior changes."],
        "expected_persona_sequence": ["neko_supervisor", "backend_dev", "dev"],
    },
    "backend-only-edit": {
        "title": "Stage 47 backend-only burn-in",
        "description": "Prove a bounded backend-focused implementation and proof cycle through the default graph.",
        "acceptance_criteria": ["Backend proof is attached.", "The default graph does not add QA unless a QA blueprint is selected."],
        "expected_persona_sequence": ["neko_supervisor", "backend_dev", "dev"],
    },
    "launcher-only-edit": {
        "title": "Stage 47 Launcher-only burn-in",
        "description": "Prove a bounded Launcher implementation and proof cycle through the default graph.",
        "acceptance_criteria": ["Launcher proof is attached.", "The default graph does not add QA unless a QA blueprint is selected."],
        "expected_persona_sequence": ["neko_supervisor", "backend_dev", "dev"],
    },
    "cross-stack-edit": {
        "title": "Stage 47 cross-stack burn-in",
        "description": "Prove a complex backend-first, Launcher-second multi-agent contract workflow through the default graph.",
        "acceptance_criteria": [
            "Neko scopes an explicit backend-first contract stage and names the Launcher release gate.",
            "Backend Dev attaches deterministic backend proof and handoff notes without unbounded repo exploration.",
            "The default graph releases Launcher Dev after Backend Dev proof exists.",
            "Launcher Dev consumes the joined backend proof and attaches deterministic Launcher proof.",
            "The default graph reaches done without adding a QA leg.",
        ],
        "affected_repos": ["EterniaBackend", "EterniaLauncher", "hermes-agent"],
        "suggested_roles": ["neko_supervisor", "backend_dev", "dev"],
        "risk_flags": [
            "cross_stack_sequential_handoff",
            "backend_contract_first",
            "launcher_contract_second",
            "default_no_qa",
            "bounded_complex_burn_in",
        ],
        "non_goals": [
            "Do not make broad product refactors.",
            "Do not run visual proof unless Neko explicitly marks a user-visible UI claim.",
            "Do not add QA unless a QA blueprint is explicitly selected.",
        ],
        "expected_persona_sequence": ["neko_supervisor", "backend_dev", "dev"],
    },
    "mission-control-visual": {
        "title": "Stage 47 Mission Control visual burn-in",
        "description": "Prove Mission Control visual capture with fullscreen screenshot evidence.",
        "acceptance_criteria": ["Fullscreen Mission Control screenshot proof is attached."],
        "expected_persona_sequence": ["neko_supervisor", "backend_dev", "dev"],
    },
    "environment-blocked": {
        "title": "Stage 47 environment-blocked burn-in",
        "description": "Prove environment blocker handling terminal-blocks honestly with evidence.",
        "acceptance_criteria": ["Environment blocker proof is attached.", "Task does not false-pass."],
        "expected_persona_sequence": ["neko_supervisor", "dev", "neko_supervisor"],
    },
    "custom-backend-proof": {
        "title": "Custom backend proof burn-in",
        "description": "Run an authored non-default Neko-to-Backend proof-only blueprint with no product edits.",
        "acceptance_criteria": ["Backend Dev receives the custom backend proof stage.", "The custom blueprint reaches done with backend_contract_smoke proof."],
        "affected_repos": ["EterniaBackend", "hermes-agent"],
        "suggested_roles": ["neko_supervisor", "backend_dev"],
        "risk_flags": ["custom_blueprint", "no_product_edits", "backend_contract_first"],
        "non_goals": ["No product code edits.", "No Launcher stage.", "No QA stage."],
        "expected_persona_sequence": ["neko_supervisor", "backend_dev"],
        "custom_blueprint": True,
        "blueprint": "custom_backend_proof",
    },
    "custom-launcher-proof": {
        "title": "Custom Launcher proof burn-in",
        "description": "Run an authored non-default Neko-to-Launcher proof-only blueprint with no product edits.",
        "acceptance_criteria": ["Launcher Dev receives the custom Launcher proof stage.", "The custom blueprint reaches done with launcher_contract_smoke proof."],
        "affected_repos": ["EterniaLauncher", "hermes-agent"],
        "suggested_roles": ["neko_supervisor", "dev"],
        "risk_flags": ["custom_blueprint", "no_product_edits"],
        "non_goals": ["No product code edits.", "No Backend stage.", "No QA stage."],
        "expected_persona_sequence": ["neko_supervisor", "dev"],
        "custom_blueprint": True,
        "blueprint": "custom_launcher_proof",
    },
    "custom-cross-stack-proof": {
        "title": "Custom cross-stack proof burn-in",
        "description": "Run an authored non-default backend-first Launcher-second blueprint with no product edits.",
        "acceptance_criteria": [
            "Backend Dev completes backend_contract_smoke before Launcher Dev starts.",
            "Launcher Dev completes launcher_contract_smoke after backend proof exists.",
            "The custom blueprint reaches done without QA.",
        ],
        "affected_repos": ["EterniaBackend", "EterniaLauncher", "hermes-agent"],
        "suggested_roles": ["neko_supervisor", "backend_dev", "dev"],
        "risk_flags": ["custom_blueprint", "no_product_edits", "backend_contract_first", "cross_stack_sequential_handoff"],
        "non_goals": ["No product code edits.", "No QA stage.", "No visual proof."],
        "expected_persona_sequence": ["neko_supervisor", "backend_dev", "dev"],
        "custom_blueprint": True,
        "blueprint": "custom_cross_stack_proof",
    },
}


def create_burn_in(*, suite: str = STAGE47_SUITE, case_id: str | None = None, rerun_of: str | None = None) -> dict[str, Any]:
    hygiene = prepare_new_goal_runtime(cleanup_stage47_temp=True)
    burn_id = _new_burn_id(suite, case_id)
    root = burn_in_dir(burn_id)
    root.mkdir(parents=True, exist_ok=False)
    manifest = _base_manifest(burn_id=burn_id, suite=suite, case_id=case_id, rerun_of=rerun_of)
    manifest["new_goal_hygiene"] = hygiene
    _write_json(root / "manifest.json", manifest)
    _write_json(root / "new_goal_hygiene.json", hygiene)
    _write_json(root / "status_before.json", build_status())
    return manifest


def run_burn_in_case(
    case_id: str,
    *,
    burn_id: str | None = None,
    max_actions: int = 12,
    persona_runtime=None,
    engine=None,
) -> dict[str, Any]:
    raise LegacyOrchestratorRemoved(
        "mission burn-in execution is unavailable because the dispatch loop is retired",
        safe_details={"case_id": case_id, "burn_id": burn_id},
    )
    if case_id not in STAGE47_CASES:
        raise ValueError(f"unknown burn-in case: {case_id}")
    if burn_id is None:
        manifest = create_burn_in(case_id=case_id)
        burn_id = manifest["burn_id"]
    root = burn_in_dir(burn_id)
    manifest = _read_manifest(root)
    manifest["case_id"] = case_id
    manifest["status"] = "running"
    case = STAGE47_CASES[case_id]
    task_id = manifest.get("task_id")
    if not task_id:
        task = _create_case_task(case_id, case, hygiene=manifest.get("new_goal_hygiene"))
        task_id = task.id
        manifest["task_id"] = task_id
        manifest["expected_persona_sequence"] = list(case.get("expected_persona_sequence") or [])
        _write_json(root / "task_create.json", {"task": task})
    _write_json(root / "manifest.json", manifest)
    task_store = getattr(engine, "task_store", TaskStore())
    run_store = getattr(engine, "run_store", RunStore())
    proof_store = getattr(engine, "proof_store", ProofStore())
    incident_store = getattr(engine, "incident_store", IncidentStore())
    status_before = build_status(task_store=task_store, run_store=run_store, proof_store=proof_store, incident_store=incident_store)
    dirty_state_before_run = status_before.get("dirty_state") if isinstance(status_before, dict) else None
    result, settle_segments = _run_burn_in_until_boundary(
        engine,
        task_id=task_id,
        max_actions=max_actions,
        run_store=run_store,
        incident_store=incident_store,
    )
    for segment in settle_segments:
        _append_jsonl(root / "tick_log.jsonl", segment)
    status_after = build_status(task_store=task_store, run_store=run_store, proof_store=proof_store, incident_store=incident_store)
    snapshot_after = build_snapshot(task_store=task_store, run_store=run_store, proof_store=proof_store, incident_store=incident_store)
    findings = classify_freezes(status=status_after, snapshot=snapshot_after)
    monitor_record = (
        record_freeze_findings(
            task_id=task_id,
            findings=findings,
            proof_store=proof_store,
            incident_store=incident_store,
            task_store=task_store,
        )
        if findings
        else {"proof_ids": [], "incident_ids": [], "finding_count": 0}
    )
    if findings:
        status_after = build_status(task_store=task_store, run_store=run_store, proof_store=proof_store, incident_store=incident_store)
        snapshot_after = build_snapshot(task_store=task_store, run_store=run_store, proof_store=proof_store, incident_store=incident_store)
    _write_json(root / "status_after.json", status_after)
    _write_json(root / "snapshot_after.json", snapshot_after)
    _append_jsonl(root / "monitor_log.jsonl", {"generated_at": now(), "findings": findings, "recorded": monitor_record})
    runs = run_store.list_for_task(task_id)
    proofs = proof_store.list_for_task(task_id)
    incidents = [incident for incident in incident_store.list_all() if incident.task_id == task_id]
    # Archive only after run/proof/incident evidence is gathered: archiving moves
    # the live records into the archive batch.
    archive_result = _archive_result_for_case(task_store, task_id)
    _write_json(root / "archive_result.json", archive_result)
    case_status, failure_class = _case_status(case_id, result, findings=findings, incidents=incidents)
    dirty_state_after_run = status_after.get("dirty_state") if isinstance(status_after, dict) else None
    manifest.update(
        {
            "finished_at": now(),
            "actual_persona_sequence": [run.persona_id for run in sorted(runs, key=lambda item: item.started_at)],
            "proof_ids": [proof.id for proof in proofs],
            "incident_ids": [incident.id for incident in incidents],
            "monitor_proof_ids": monitor_record["proof_ids"],
            "monitor_incident_ids": monitor_record["incident_ids"],
            "archive_batch": archive_result["archive_batch"],
            "archive_dir": archive_result.get("archive_dir"),
            "status": case_status,
            "failure_class": failure_class,
            "settle_segments": len(settle_segments),
            "dirty_state_after_run": dirty_state_after_run,
            "product_repos_modified": _product_repos_modified_since(dirty_state_before_run, dirty_state_after_run),
        }
    )
    manifest["unattended"] = _unattended_case_summary(
        manifest,
        events=EventLog().for_task(task_id, limit=0),
        archive_result=archive_result,
        status_after=status_after,
    )
    _write_json(root / "manifest.json", manifest)
    _write_json(root / "certification_ledger.json", update_certification_ledger(manifest))
    _write_notes(root / "certification_notes.md", manifest, findings)
    return manifest


def _run_burn_in_until_boundary(
    engine,
    *,
    task_id: str,
    max_actions: int,
    run_store: RunStore,
    incident_store: IncidentStore,
):
    remaining = max(1, int(max_actions or 1))
    segments = []
    result = None
    event_log = EventLog()
    _emit_driver_lifecycle(event_log, task_id, "daemon.started")
    try:
        while remaining > 0:
            result = engine.run_until_settled(task_id=task_id, max_actions=remaining)
            segments.append(result)
            used = max(1, len(getattr(result, "actions_taken", []) or []))
            remaining = max(0, remaining - used)
            incidents = [incident for incident in incident_store.list_all() if incident.task_id == task_id]
            cap = getattr(getattr(engine, "config", None), "neko_extension_cap", 2)
            if not _has_open_budget_approval_path(incidents, run_store, cap=cap):
                break
            if remaining <= 0:
                break
    finally:
        _emit_driver_lifecycle(event_log, task_id, "daemon.stopped", reason=getattr(result, "stop_reason", None))
    return result, segments


def _emit_driver_lifecycle(event_log: EventLog, task_id: str, event_type: str, *, reason: str | None = None) -> None:
    from .models import Event

    payload: dict[str, Any] = {"mode": "burn_in_inprocess", "self_driven": True, "pid": os.getpid()}
    if reason:
        payload["reason"] = str(reason)[:120]
    try:
        event_log.append(Event(now(), event_type, task_id, None, None, payload))
    except Exception:
        pass


def _has_open_budget_approval_path(incidents: list[Any], run_store: RunStore, *, cap: int = 2) -> bool:
    for incident in incidents:
        if budget_incident_can_continue(incident, run_store, cap=cap):
            return True
    return False


def burn_in_status(burn_id: str) -> dict[str, Any]:
    root = burn_in_dir(burn_id)
    manifest = _read_manifest(root)
    files = {path.name: path.exists() for path in _expected_files(root)}
    return {"burn_id": burn_id, "root": _safe_path(root), "manifest": manifest, "files": files}


def summarize_burn_in(burn_id: str) -> dict[str, Any]:
    root = burn_in_dir(burn_id)
    manifest = _read_manifest(root)
    missing = [path.name for path in _expected_files(root) if not path.exists()]
    ok = not missing and bool(manifest.get("task_id")) and manifest.get("status") == "passed"
    failure_class = None if ok else manifest.get("failure_class") or ("incomplete_evidence" if missing else "not_passed")
    return {
        "burn_id": burn_id,
        "ok": ok,
        "status": manifest.get("status"),
        "case_id": manifest.get("case_id"),
        "task_id": manifest.get("task_id"),
        "proof_ids": manifest.get("proof_ids") or [],
        "incident_ids": manifest.get("incident_ids") or [],
        "missing_files": missing,
        "failure_class": failure_class,
        "certification": certification_summary(),
        "case_unattended": manifest.get("unattended") if isinstance(manifest.get("unattended"), dict) else None,
    }


def certification_ledger_path() -> Path:
    return burn_in_root() / "certification_ledger.json"


def certification_summary() -> dict[str, Any]:
    return _certification_public_summary(_read_certification_ledger())


def update_certification_ledger(manifest: dict[str, Any]) -> dict[str, Any]:
    ledger = _read_certification_ledger()
    unattended = manifest.get("unattended") if isinstance(manifest.get("unattended"), dict) else {}
    green = bool(unattended.get("green"))
    if green:
        ledger["consecutive_green"] = int(ledger.get("consecutive_green") or 0) + 1
        ledger["last_failure_class"] = None
    else:
        ledger["consecutive_green"] = 0
        ledger["last_failure_class"] = unattended.get("failure_class") or manifest.get("failure_class") or "not_unattended"
    ledger["last_case_id"] = manifest.get("case_id")
    ledger["last_burn_id"] = manifest.get("burn_id")
    ledger["manual_intervention_counts"] = unattended.get("manual_intervention_counts") or {}
    ledger["updated_at"] = now()
    ledger["required_consecutive_green"] = CERTIFICATION_TARGET_CONSECUTIVE_GREEN
    if int(ledger.get("consecutive_green") or 0) >= CERTIFICATION_TARGET_CONSECUTIVE_GREEN:
        ledger["state"] = "green"
        ledger["certified_at"] = ledger.get("certified_at") or now()
    else:
        ledger["state"] = "red"
        ledger["certified_at"] = None
    _write_json(certification_ledger_path(), ledger)
    return ledger


def swarm_certification_allows_production(
    *,
    lane_kind: str = "production",
    allow_uncertified_dev_swarm: bool = False,
    requires_certification: bool = True,
) -> tuple[bool, dict[str, Any]]:
    summary = certification_summary()
    if lane_kind == "playground":
        return True, {**summary, "exempt": True, "exemption": "playground"}
    if not requires_certification or allow_uncertified_dev_swarm:
        return True, {**summary, "override": bool(allow_uncertified_dev_swarm)}
    return summary.get("state") == "green", summary


def recursive_supervision_certification_allows_production(
    *,
    allow_uncertified_recursive_supervision: bool = False,
    requires_certification: bool = True,
) -> tuple[bool, dict[str, Any]]:
    summary = certification_summary()
    if not requires_certification or allow_uncertified_recursive_supervision:
        return True, {**summary, "override": bool(allow_uncertified_recursive_supervision)}
    return summary.get("state") == "green", summary


def burn_in_root() -> Path:
    return paths.store_root() / "burn_in"


def burn_in_dir(burn_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", burn_id):
        raise ValueError("invalid burn_id")
    return burn_in_root() / burn_id


def _base_manifest(*, burn_id: str, suite: str, case_id: str | None, rerun_of: str | None) -> dict[str, Any]:
    case = STAGE47_CASES.get(case_id or "", {})
    return {
        "burn_id": burn_id,
        "suite": suite,
        "case_id": case_id,
        "task_id": None,
        "started_at": now(),
        "finished_at": None,
        "runtime_root": _safe_path(paths.store_root()),
        "hermes_home": _safe_path(Path(os.getenv("HERMES_HOME") or get_default_hermes_root())),
        "expected_persona_sequence": list(case.get("expected_persona_sequence") or []),
        "actual_persona_sequence": [],
        "proof_ids": [],
        "incident_ids": [],
        "archive_batch": None,
        "status": "created",
        "failure_class": None,
        "fix_commit": None,
        "rerun_of": rerun_of,
        "new_goal_hygiene": None,
        "dirty_state_after_run": None,
        "product_repos_modified": False,
        "unattended": None,
    }


def _unattended_case_summary(manifest: dict[str, Any], *, events: list[Any], archive_result: dict[str, Any], status_after: dict[str, Any]) -> dict[str, Any]:
    counts = _manual_intervention_counts(events)
    archive_succeeded = bool(archive_result.get("archive_batch"))
    final_status_clean = _final_status_clean(status_after)
    daemon_lifecycle = _daemon_self_lifecycle(events)
    green = (
        manifest.get("status") == "passed"
        and not any(counts.values())
        and daemon_lifecycle
        and archive_succeeded
        and final_status_clean
    )
    failure_class = None
    if not green:
        if any(counts.values()):
            failure_class = "manual_intervention"
        elif manifest.get("status") != "passed":
            failure_class = manifest.get("failure_class") or "case_not_passed"
        elif not daemon_lifecycle:
            failure_class = "daemon_lifecycle_not_unattended"
        elif not archive_succeeded:
            failure_class = "archive_not_completed"
        elif not final_status_clean:
            failure_class = "final_status_not_clean"
        else:
            failure_class = "not_unattended"
    return {
        "green": green,
        "failure_class": failure_class,
        "manual_intervention_counts": counts,
        "daemon_self_started_and_stopped": daemon_lifecycle,
        "archive_succeeded": archive_succeeded,
        "final_status_clean": final_status_clean,
    }


def _manual_intervention_counts(events: list[Any]) -> dict[str, int]:
    counts = {"manual_ticks": 0, "manual_incident_closes": 0, "task_unblocks": 0, "process_kills": 0}
    for event in events:
        payload = event.payload if isinstance(getattr(event, "payload", None), dict) else {}
        if event.type == "tick.requested" and payload.get("actor") == "cli":
            counts["manual_ticks"] += 1
        elif event.type == "incident.closed" and str(payload.get("reason") or "").strip():
            counts["manual_incident_closes"] += 1
        elif event.type == "task.unblocked" and payload.get("actor") == "cli":
            # Automated recovery also transitions blocked -> active and emits
            # task.unblocked (actor = persona/harness); only the operator CLI
            # unblock counts as manual intervention.
            counts["task_unblocks"] += 1
        elif event.type in {"daemon.process_killed", "process.killed"}:
            counts["process_kills"] += 1
    return counts


def _daemon_self_lifecycle(events: list[Any]) -> bool:
    types = {event.type for event in events}
    return "daemon.started" in types and "daemon.stopped" in types


def _final_status_clean(status_after: dict[str, Any]) -> bool:
    if not isinstance(status_after, dict):
        return False
    return int(status_after.get("running_runs") or 0) == 0 and int(status_after.get("waiting_runs") or 0) == 0 and int(status_after.get("open_incidents") or 0) == 0


def _read_certification_ledger() -> dict[str, Any]:
    path = certification_ledger_path()
    if not path.exists():
        return _empty_certification_ledger()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty_certification_ledger()
    return data if isinstance(data, dict) else _empty_certification_ledger()


def _empty_certification_ledger() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "state": "red",
        "consecutive_green": 0,
        "required_consecutive_green": CERTIFICATION_TARGET_CONSECUTIVE_GREEN,
        "last_failure_class": "no_certification_ledger",
        "manual_intervention_counts": {},
        "certified_at": None,
        "updated_at": None,
    }


def _certification_public_summary(ledger: dict[str, Any]) -> dict[str, Any]:
    return {
        "required": True,
        "state": str(ledger.get("state") or "red"),
        "consecutive_green": int(ledger.get("consecutive_green") or 0),
        "required_consecutive_green": int(ledger.get("required_consecutive_green") or CERTIFICATION_TARGET_CONSECUTIVE_GREEN),
        "last_failure_class": ledger.get("last_failure_class"),
        "certified_at": ledger.get("certified_at"),
        "updated_at": ledger.get("updated_at"),
        "last_burn_id": ledger.get("last_burn_id"),
        "last_case_id": ledger.get("last_case_id"),
        "manual_intervention_counts": ledger.get("manual_intervention_counts") or {},
    }


def _create_case_task(case_id: str, case: dict[str, Any], *, hygiene: dict[str, Any] | None = None) -> Task:
    from .default_plan import ensure_default_mission_plan
    from .goal_hygiene import repo_clean_baseline_from_hygiene

    ts = now()
    task = Task(
        id=f"task_burn_{uuid.uuid4().hex[:8]}",
        title=str(case["title"]),
        description=str(case["description"]),
        state=TaskState.CREATED,
        created_at=ts,
        updated_at=ts,
        requested_by="stage47_burn_in",
        acceptance_criteria=list(case.get("acceptance_criteria") or []),
        affected_repos=list(case.get("affected_repos") or []),
        suggested_roles=list(case.get("suggested_roles") or []),
        risk_flags=list(case.get("risk_flags") or []),
        non_goals=list(case.get("non_goals") or []),
        requires_visual_proof=bool(case.get("requires_visual_proof", False)),
    )
    blueprint_id = str(case.get("blueprint") or "").strip()
    if blueprint_id:
        _apply_custom_blueprint(task, blueprint_id)
    else:
        ensure_default_mission_plan(task)
    # Same baseline capture as `harness task create`: pre-existing operator dirt
    # must not preflight-block a burn-in case (observed live: case 1 blocked on
    # repo_clean for unrelated EterniaBackend edits).
    task.harness_self_heal["repo_clean_baseline"] = repo_clean_baseline_from_hygiene(hygiene)
    TaskStore().create(task)
    return task


def _apply_custom_blueprint(task: Task, blueprint_id: str) -> None:
    from .blueprints import instantiate_blueprint
    from .blueprints.schema import blueprint_from_dict

    blueprint = blueprint_from_dict(_custom_blueprint_raw(blueprint_id))
    bindings = {
        key: value
        for key, value in {
            "lead": "persona:neko_supervisor",
            "backend_builder": "persona:backend_dev",
            "builder": "persona:dev",
        }.items()
        if key in {slot.id for slot in blueprint.slots}
    }
    plan = instantiate_blueprint(
        blueprint,
        goal=task.description or task.title,
        bindings=bindings,
    )
    task.mission_plan = plan
    task.current_stage_id = plan.current_stage_id


# Read-compat only, modelled on `_LEGACY_RUNNING_TASK_STATE_VALUES` in `states.py`.
# These custom burn-in blueprints used to be named after Stage 46; the stage is
# retired and the number carries no meaning, so the ids are now intent-named.
# Audit (2026-07-29) of the runtime root found the old ids in **no** machine-read
# record — not in `burn_in/*/task_create.json`, `tasks/`, `goals/`, `runs/`,
# `blueprint_runs/`, `proofs/`, `events.jsonl`, `read_model.db`, nor
# `burn_in/certification_ledger.json` (which keys on `case_id`, not blueprint id).
# They survive only in human-readable `cert_streak.md` evidence tables. This map
# exists so replaying any such legacy id still resolves instead of raising.
_LEGACY_CUSTOM_BLUEPRINT_IDS = {
    "stage46_custom_backend_proof": "custom_backend_proof",
    "stage46_custom_launcher_proof": "custom_launcher_proof",
    "stage46_custom_cross_stack_proof": "custom_cross_stack_proof",
}


def _custom_blueprint_raw(blueprint_id: str) -> dict[str, Any]:
    blueprint_id = _LEGACY_CUSTOM_BLUEPRINT_IDS.get(blueprint_id, blueprint_id)
    common_scope = {
        "id": "scope",
        "title": "Scope",
        "objective": "Confirm the proof-only stage, owner, repository, and stop condition, then release the custom graph.",
        "owner_slot": "lead",
        "repo": "hermes-agent",
        "kind": "scope",
        "blocks_qa_until": False,
        "proof_gate": {"required": False},
    }
    backend_stage = {
        "id": "backend_implementation",
        "title": "Backend Proof",
        "objective": "Attach deterministic backend_contract_smoke proof without product edits.",
        "owner_slot": "backend_builder",
        "repo": "EterniaBackend",
        "kind": "implementation",
        "depends_on": ["scope"],
        "blocks_qa_until": False,
        "proof_recipe_id": "backend_contract_smoke",
        "proof_gate": {
            "required": True,
            "minimum_status": "passed",
            "required_proof_types": ["test_run"],
            "proof_recipe_id": "backend_contract_smoke",
        },
    }
    launcher_stage = {
        "id": "implement",
        "title": "Launcher Proof",
        "objective": "Attach deterministic launcher_contract_smoke proof without product edits.",
        "owner_slot": "builder",
        "repo": "EterniaLauncher",
        "kind": "implementation",
        "depends_on": ["scope"],
        "blocks_qa_until": False,
        "proof_recipe_id": "launcher_contract_smoke",
        "proof_gate": {
            "required": True,
            "minimum_status": "passed",
            "required_proof_types": ["test_run"],
            "proof_recipe_id": "launcher_contract_smoke",
        },
    }
    if blueprint_id == "custom_backend_proof":
        return {
            "id": blueprint_id,
            "version": 1,
            "title": "Custom Backend Proof",
            "description": "Neko releases one Backend Dev proof-only stage.",
            "slots": [
                {"id": "lead", "role": "neko", "required": True},
                {"id": "backend_builder", "role": "backend_dev", "required": True},
            ],
            "stages": [common_scope, backend_stage],
            "edges": [
                {"source": "scope", "outcome": "ready", "target": "backend_implementation"},
                {"source": "scope", "outcome": "missing_input", "target": "intervention"},
                {"source": "backend_implementation", "outcome": "passed", "target": "done"},
                {"source": "backend_implementation", "outcome": "needs_fixes", "target": "backend_implementation"},
                {"source": "backend_implementation", "outcome": "blocked", "target": "scope"},
            ],
            "agent_topology": {"root": "lead", "edges": [{"source": "lead", "target": "backend_builder", "kind": "steers"}]},
            "limits": {"max_attempts_per_stage": 3, "max_total_stages": 8},
        }
    if blueprint_id == "custom_launcher_proof":
        return {
            "id": blueprint_id,
            "version": 1,
            "title": "Custom Launcher Proof",
            "description": "Neko releases one Launcher Dev proof-only stage.",
            "slots": [
                {"id": "lead", "role": "neko", "required": True},
                {"id": "builder", "role": "dev", "required": True},
            ],
            "stages": [common_scope, launcher_stage],
            "edges": [
                {"source": "scope", "outcome": "ready", "target": "implement"},
                {"source": "scope", "outcome": "missing_input", "target": "intervention"},
                {"source": "implement", "outcome": "passed", "target": "done"},
                {"source": "implement", "outcome": "needs_fixes", "target": "implement"},
                {"source": "implement", "outcome": "blocked", "target": "scope"},
            ],
            "agent_topology": {"root": "lead", "edges": [{"source": "lead", "target": "builder", "kind": "steers"}]},
            "limits": {"max_attempts_per_stage": 3, "max_total_stages": 8},
        }
    if blueprint_id == "custom_cross_stack_proof":
        launcher_after_backend = dict(launcher_stage)
        launcher_after_backend["depends_on"] = ["backend_implementation"]
        return {
            "id": blueprint_id,
            "version": 1,
            "title": "Custom Cross-Stack Proof",
            "description": "Neko releases Backend Dev first, then Launcher Dev after backend proof.",
            "slots": [
                {"id": "lead", "role": "neko", "required": True},
                {"id": "backend_builder", "role": "backend_dev", "required": True},
                {"id": "builder", "role": "dev", "required": True},
            ],
            "stages": [common_scope, backend_stage, launcher_after_backend],
            "edges": [
                {"source": "scope", "outcome": "ready", "target": "backend_implementation"},
                {"source": "scope", "outcome": "missing_input", "target": "intervention"},
                {"source": "backend_implementation", "outcome": "passed", "target": "implement"},
                {"source": "backend_implementation", "outcome": "needs_fixes", "target": "backend_implementation"},
                {"source": "backend_implementation", "outcome": "blocked", "target": "scope"},
                {"source": "implement", "outcome": "passed", "target": "done"},
                {"source": "implement", "outcome": "needs_fixes", "target": "implement"},
                {"source": "implement", "outcome": "blocked", "target": "scope"},
            ],
            "agent_topology": {
                "root": "lead",
                "edges": [
                    {"source": "lead", "target": "backend_builder", "kind": "steers"},
                    {"source": "lead", "target": "builder", "kind": "steers"},
                ],
            },
            "limits": {"max_attempts_per_stage": 3, "max_total_stages": 12},
        }
    raise ValueError(f"unknown custom burn-in blueprint: {blueprint_id}")


def _new_burn_id(suite: str, case_id: str | None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = _slug(case_id or suite)
    return f"{stamp}_{slug}_{uuid.uuid4().hex[:6]}"


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip().lower()).strip("-")
    return slug[:48] or "burn-in"


def _read_manifest(root: Path) -> dict[str, Any]:
    path = root / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest must be an object")
    return data


def _expected_files(root: Path) -> list[Path]:
    return [
        root / "manifest.json",
        root / "new_goal_hygiene.json",
        root / "status_before.json",
        root / "task_create.json",
        root / "tick_log.jsonl",
        root / "monitor_log.jsonl",
        root / "status_after.json",
        root / "snapshot_after.json",
        root / "archive_result.json",
        root / "certification_notes.md",
    ]


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, to_jsonable(payload), indent=2, sort_keys=True)


def _append_jsonl(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(to_jsonable(payload), ensure_ascii=False, separators=(",", ":")) + "\n")


def _write_notes(path: Path, manifest: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    lines = [
        f"# Stage 47 Burn-In: {manifest.get('case_id')}",
        "",
        f"- burn_id: {manifest.get('burn_id')}",
        f"- status: {manifest.get('status')}",
        f"- task_id: {manifest.get('task_id')}",
        f"- proof_count: {len(manifest.get('proof_ids') or [])}",
        f"- incident_count: {len(manifest.get('incident_ids') or [])}",
        f"- freeze_findings: {len(findings)}",
        f"- product_repos_modified: {bool(manifest.get('product_repos_modified'))}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _deferred_archive_result() -> dict[str, Any]:
    return {
        "attempted": False,
        "archive_batch": None,
        "reason": "case task is not terminal; archive deferred until done/cancelled",
    }


def _archive_result_for_case(task_store: TaskStore, task_id: str) -> dict[str, Any]:
    """Archive a terminal case task so the unattended gate can go green.

    The evidence-preserving archive is part of the unattended definition
    (`archive_succeeded`); deferring it forever made every passed case fail
    `archive_not_completed`. Non-terminal outcomes stay live for recovery.
    The burn directory keeps its own manifest/tick/status copies regardless.
    """
    try:
        task = task_store.get(task_id)
    except Exception:
        return _deferred_archive_result()
    if task.state not in {TaskState.DONE, TaskState.CANCELLED}:
        return _deferred_archive_result()
    try:
        from .store import ArchiveStore

        result = ArchiveStore().archive_tasks([task_id], actor="burn_in", reason="auto-archive terminal certification case")
        return {
            "attempted": True,
            "archive_batch": result.get("archive_batch"),
            "archive_dir": result.get("archive_dir"),
        }
    except Exception as exc:
        return {"attempted": True, "archive_batch": None, "error_class": type(exc).__name__}


def _product_repos_modified(dirty_state: Any) -> bool:
    if not isinstance(dirty_state, dict):
        return False
    product_labels = {"EterniaBackend", "EterniaLauncher"}
    repos = dirty_state.get("repos")
    if not isinstance(repos, list):
        return False
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        if str(repo.get("label") or "") in product_labels and (repo.get("dirty") or repo.get("error")):
            return True
    return False


def _product_repos_modified_since(before: Any, after: Any) -> bool:
    if not isinstance(after, dict):
        return False
    if not isinstance(before, dict):
        return _product_repos_modified(after)
    before_repos = _repo_state_by_label(before)
    after_repos = _repo_state_by_label(after)
    for label, after_repo in after_repos.items():
        if label not in {"EterniaBackend", "EterniaLauncher"}:
            continue
        before_repo = before_repos.get(label, {})
        if _repo_dirty_signature(before_repo) != _repo_dirty_signature(after_repo):
            if after_repo.get("dirty") or after_repo.get("error"):
                return True
    return False


def _repo_state_by_label(dirty_state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    repos = dirty_state.get("repos")
    if not isinstance(repos, list):
        return {}
    return {
        str(repo.get("label") or ""): repo
        for repo in repos
        if isinstance(repo, dict) and str(repo.get("label") or "")
    }


def _repo_dirty_signature(repo: dict[str, Any]) -> tuple:
    return (
        bool(repo.get("dirty")),
        str(repo.get("error") or ""),
        int(repo.get("dirty_count") or 0),
        tuple(repo.get("status_excerpt") or []),
    )


def _case_status(case_id: str, result: Any, *, findings: list[dict[str, Any]], incidents: list[Any]) -> tuple[str, str | None]:
    if findings:
        return "blocked", str(findings[0].get("kind") or "freeze_finding")
    final_state = _state_value(getattr(result, "final_task_state", None))
    stop_reason = str(getattr(result, "stop_reason", "") or "")
    open_incidents = [incident for incident in incidents if getattr(incident, "closed_at", None) is None]
    if case_id == "environment-blocked":
        if final_state == TaskState.DONE.value:
            return "blocked", "environment_case_false_passed"
        if open_incidents or final_state == TaskState.BLOCKED.value or stop_reason in {"incident_opened", "task_blocked", "action_failed"}:
            return "passed", None
        return "blocked", stop_reason or "environment_blocker_not_proven"
    if stop_reason == "task_terminal" and final_state == TaskState.DONE.value and not open_incidents:
        return "passed", None
    if open_incidents:
        return "blocked", "open_incident"
    return "blocked", stop_reason or "not_settled"


def _state_value(value: Any) -> str | None:
    return value.value if hasattr(value, "value") else str(value) if value is not None else None


def _safe_path(path: Path) -> str:
    text = str(path)
    if ":/" in text.replace("\\", "/") or text.startswith(("/", "~")):
        return f"<path:{path.name or 'root'}>"
    return text
