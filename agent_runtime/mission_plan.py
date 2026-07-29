from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Iterable

from hermes_time import now

from .decision_schema import DecisionPayloadInvalid, DecisionType
from .final_gate import goal_demands_exact_proof, goal_named_gate_commands
from .models import MissionIntent, MissionPlan, MissionPlanStage, Proof, Task, TaskStage
from .proof_recipes import resolve_proof_recipe
from .proof_rules import ProofType
from .stage_intent import no_product_edit_recipe_id
from .states import StageStatus


ALLOWED_REPOS = frozenset({"EterniaLauncher", "EterniaBackend", "hermes-agent", "none"})
ALLOWED_KINDS = frozenset({"planning", "proof_only", "implementation", "qa_verdict", "recovery", "context"})
READY_STATUSES = frozenset({StageStatus.READY_FOR_QA, StageStatus.PASSED})
INCOMPLETE_STATUSES = frozenset({StageStatus.DRAFT, StageStatus.AUDITED, StageStatus.READY, StageStatus.IMPLEMENTING, StageStatus.REWORK, StageStatus.BLOCKED})


def mission_plan_enabled(config) -> bool:
    plan = getattr(config, "mission_plan", None)
    return bool(getattr(plan, "enabled", False))


def mission_plan_hud_enabled(config) -> bool:
    plan = getattr(config, "mission_plan", None)
    return bool(getattr(plan, "enabled", False)) and bool(getattr(plan, "enforce_hud", True))


def task_stage_records(task: Task) -> list[MissionPlanStage | TaskStage]:
    plan = getattr(task, "mission_plan", None)
    if plan and plan.enabled:
        _merge_legacy_stage_data_into_plan(task, plan)
        return plan.stages
    return getattr(task, "stages", []) or []


def _merge_legacy_stage_data_into_plan(task: Task, plan: MissionPlan) -> None:
    legacy_stages = list(getattr(task, "stages", []) or [])
    if not legacy_stages:
        return
    by_id = {stage.id: stage for stage in plan.stages}
    changed = False
    for legacy in legacy_stages:
        typed = by_id.get(legacy.id)
        if typed is None:
            typed = _plan_stage_from_task_stage(task, legacy)
            plan.stages.append(typed)
            _ensure_dynamic_stage_edges(plan, typed)
            by_id[typed.id] = typed
            changed = True
        for field in ("affected_paths", "acceptance_criteria", "test_plan", "audit_notes", "corrections"):
            incoming = list(getattr(legacy, field, []) or [])
            if incoming and not getattr(typed, field, None):
                setattr(typed, field, incoming)
                changed = True
        if legacy.requires_visual_proof is not None and typed.requires_visual_proof != bool(legacy.requires_visual_proof):
            typed.requires_visual_proof = bool(legacy.requires_visual_proof)
            changed = True
    if changed:
        plan.revision = int(getattr(plan, "revision", 0) or 0) + 1


def current_task_stage_record(task: Task) -> MissionPlanStage | TaskStage | None:
    stage_id = str(getattr(task, "current_stage_id", "") or "").strip()
    if not stage_id:
        return None
    return next((stage for stage in task_stage_records(task) if stage.id == stage_id), None)


def append_task_stage_record(task: Task, stage: MissionPlanStage | TaskStage) -> None:
    plan = getattr(task, "mission_plan", None)
    if plan and plan.enabled:
        typed = _plan_stage_from_task_stage(task, stage)
        plan.stages.append(typed)
        _ensure_dynamic_stage_edges(plan, typed)
        _sync_task_stage_compat_from_plan(task)
        return
    task.stages.append(stage if isinstance(stage, TaskStage) else _task_stage_from_plan_stage(task, stage))


def _ensure_dynamic_stage_edges(plan: MissionPlan, stage: MissionPlanStage) -> None:
    if any(edge.get("source") == stage.id for edge in plan.edges):
        return
    target = next((item.id for item in plan.stages if item.kind == "qa_verdict" and item.id != stage.id), "done")
    if target != "done":
        qa_stage = next((item for item in plan.stages if item.id == target), None)
        if qa_stage is not None and stage.id not in qa_stage.depends_on:
            qa_stage.depends_on.append(stage.id)
    plan.edges.extend(
        [
            {"source": stage.id, "outcome": "ready", "target": target},
            {"source": stage.id, "outcome": "passed", "target": target},
            {"source": stage.id, "outcome": "needs_fixes", "target": stage.id},
            {"source": stage.id, "outcome": "failed", "target": stage.id},
            {"source": stage.id, "outcome": "missing_input", "target": stage.id},
            {"source": stage.id, "outcome": "blocked", "target": "intervention"},
        ]
    )


def _plan_stage_from_task_stage(task: Task, stage: MissionPlanStage | TaskStage) -> MissionPlanStage:
    if isinstance(stage, MissionPlanStage):
        return stage
    owner = "qa" if "qa" in f"{stage.id} {stage.title}".lower() else "dev"
    repo = (getattr(task, "affected_repos", None) or ["hermes-agent"])[0] if getattr(task, "affected_repos", None) else "hermes-agent"
    return MissionPlanStage(
        id=stage.id,
        title=stage.title,
        objective=stage.objective,
        owner=owner,
        owner_slot=owner,
        repo=repo,
        kind="qa_verdict" if owner == "qa" else "implementation",
        status=stage.status,
        requires_visual_proof=bool(stage.requires_visual_proof),
        affected_paths=list(stage.affected_paths or []),
        acceptance_criteria=list(stage.acceptance_criteria or []),
        test_plan=list(stage.test_plan or []),
        audit_notes=list(stage.audit_notes or []),
        corrections=list(stage.corrections or []),
        created_at=stage.created_at,
        updated_at=stage.updated_at,
    )


def _task_stage_from_plan_stage(task: Task, stage: MissionPlanStage) -> TaskStage:
    visual_gate = _plan_stage_has_visual_gate(stage)
    return TaskStage(
        id=stage.id,
        title=stage.title,
        objective=stage.objective,
        status=stage.status,
        affected_paths=list(getattr(stage, "affected_paths", []) or []),
        acceptance_criteria=list(getattr(stage, "acceptance_criteria", []) or []),
        test_plan=[] if visual_gate else list(getattr(stage, "test_plan", []) or []),
        audit_notes=list(getattr(stage, "audit_notes", []) or []),
        corrections=list(getattr(stage, "corrections", []) or []),
        requires_visual_proof=stage.requires_visual_proof,
        created_at=stage.created_at,
        updated_at=stage.updated_at,
    )


def is_mission_lead_actor(task: Task, actor: str | None) -> bool:
    actor_id = str(actor or "").strip()
    if not actor_id:
        return False
    plan = getattr(task, "mission_plan", None)
    if not plan:
        return actor_id == "neko_supervisor"
    lead_ids: set[str] = set()
    slots = getattr(plan, "slots", None) or {}
    bindings = getattr(plan, "bindings", None) or {}
    for slot_id, raw_slot in slots.items():
        slot = raw_slot if isinstance(raw_slot, dict) else {}
        role = str(slot.get("role") or "").strip()
        slot_name = str(slot_id or "").strip()
        if role in {"lead", "neko", "pm"} or slot_name in {"lead", "neko_supervisor"}:
            lead_ids.add(_binding_persona_id(str(bindings.get(slot_name) or slot_name)))
    if lead_ids:
        return actor_id in lead_ids
    for stage in getattr(plan, "stages", []) or []:
        owner = str(getattr(stage, "owner", "") or "").strip()
        owner_slot = str(getattr(stage, "owner_slot", "") or "").strip()
        if owner == "neko_supervisor":
            lead_ids.add(_binding_persona_id(str(bindings.get(owner_slot) or owner)))
        elif owner_slot in {"lead", "neko_supervisor"}:
            lead_ids.add(_binding_persona_id(str(bindings.get(owner_slot) or owner or owner_slot)))
    return actor_id in (lead_ids or {"neko_supervisor"})


def _binding_persona_id(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("persona:"):
        return text.split(":", 1)[1].strip()
    return text


def ensure_mission_plan(task: Task, payload: dict[str, Any] | None = None, *, actor: str | None = None) -> MissionPlan:
    payload = payload if isinstance(payload, dict) else {}
    existing = getattr(task, "mission_plan", None)
    if existing is not None and existing.enabled:
        plan = existing
    else:
        plan = MissionPlan(
            enabled=True,
            mission_intent=_mission_intent_from_task(task),
            stages=[],
            current_stage_id=None,
            revision=0,
        )
    if isinstance(payload.get("mission_plan"), dict):
        plan = _plan_from_payload(task, payload["mission_plan"], existing=plan)
    elif isinstance(payload.get("mission_plan_patch"), dict):
        plan = _apply_plan_patch(task, plan, payload["mission_plan_patch"])
    elif not plan.stages:
        from .default_plan import build_default_mission_plan

        plan = build_default_mission_plan(task)
    _merge_handoff_observed_lane_requirement(plan, payload)
    release_stage_id = str(payload.get("release_stage_id") or "").strip()
    if release_stage_id:
        _set_current_stage(plan, release_stage_id)
    plan.revision = int(plan.revision or 0) + 1
    task.mission_plan = plan
    _sync_task_stage_compat_from_plan(task)
    return plan


def _merge_handoff_observed_lane_requirement(plan: MissionPlan, payload: dict[str, Any]) -> None:
    handoff = payload.get("handoff_packet")
    if not isinstance(handoff, dict):
        return
    proof_gate = handoff.get("proof_gate") if isinstance(handoff.get("proof_gate"), dict) else {}
    observed: dict[str, Any] = {}
    for key in ("observed_lane_required", "observed_lane_requirement", "observed_lane_expectation"):
        if key in proof_gate:
            observed[key] = proof_gate[key]
    if not observed:
        return
    target = _stage_for_handoff(plan, handoff)
    if target is None:
        return
    merged = dict(target.proof_gate or {})
    merged.update(observed)
    target.proof_gate = merged
    target.updated_at = now()


def _stage_for_handoff(plan: MissionPlan, handoff: dict[str, Any]) -> MissionPlanStage | None:
    target_repo = _canonical_repo(str(handoff.get("target_repo") or ""))
    target_owner = _canonical_owner(str(handoff.get("target_owner") or handoff.get("target_dev_persona") or ""))
    proof_gate = handoff.get("proof_gate") if isinstance(handoff.get("proof_gate"), dict) else {}
    recipe = str(proof_gate.get("proof_recipe_id") or proof_gate.get("recipe_id") or "").strip()
    candidates = [
        stage
        for stage in plan.stages
        if (not target_repo or stage.repo == target_repo)
        and (not target_owner or _canonical_owner(stage.owner) == target_owner)
    ]
    if recipe:
        recipe_match = next((stage for stage in candidates if stage.proof_recipe_id == recipe), None)
        if recipe_match is not None:
            return recipe_match
    return next((stage for stage in candidates if stage.status in INCOMPLETE_STATUSES), None) or (candidates[0] if candidates else None)


def validate_mission_plan(plan: MissionPlan) -> list[str]:
    errors: list[str] = []
    if int(plan.version or 0) != 1:
        errors.append("mission_plan.version must be 1")
    if not plan.mission_intent:
        errors.append("mission_plan.mission_intent is required")
    ids: set[str] = set()
    for stage in plan.stages:
        if not _safe_id(stage.id):
            errors.append(f"stage {stage.id!r} id must be a redaction-safe token")
        if stage.id in ids:
            errors.append(f"duplicate stage id: {stage.id}")
        ids.add(stage.id)
        owner_slot = stage.owner_slot or stage.owner
        if plan.slots:
            if owner_slot not in plan.slots:
                errors.append(f"stage {stage.id} owner_slot {owner_slot!r} is not declared in mission_plan.slots")
        if stage.repo not in ALLOWED_REPOS:
            errors.append(f"stage {stage.id} repo {stage.repo!r} is not allowed")
        if stage.kind not in ALLOWED_KINDS:
            errors.append(f"stage {stage.id} kind {stage.kind!r} is not allowed")
        if stage.proof_recipe_id:
            try:
                resolve_proof_recipe(stage.proof_recipe_id)
            except Exception as exc:
                errors.append(f"stage {stage.id} proof_recipe_id {stage.proof_recipe_id!r} is invalid: {exc}")
        for dep in stage.depends_on:
            if dep not in ids and not any(candidate.id == dep for candidate in plan.stages):
                errors.append(f"stage {stage.id} depends on unknown stage {dep!r}")
    errors.extend(_dependency_cycle_errors(plan))
    if plan.current_stage_id and plan.current_stage_id not in {stage.id for stage in plan.stages}:
        errors.append(f"current_stage_id {plan.current_stage_id!r} is not a known stage")
    return errors


def validate_mission_plan_payload(payload: dict[str, Any]) -> None:
    raw_plan = payload.get("mission_plan")
    raw_patch = payload.get("mission_plan_patch")
    if raw_plan is not None and not isinstance(raw_plan, dict):
        raise DecisionPayloadInvalid("mission_plan must be an object")
    if raw_patch is not None and not isinstance(raw_patch, dict):
        raise DecisionPayloadInvalid("mission_plan_patch must be an object")
    for raw in (raw_plan, raw_patch):
        if isinstance(raw, dict):
            _validate_raw_plan_keys(raw)
    release_stage_id = payload.get("release_stage_id")
    if release_stage_id is not None and not _safe_id(str(release_stage_id)):
        raise DecisionPayloadInvalid("release_stage_id must be a redaction-safe stage id")


def _sync_task_stage_compat_from_plan(task: Task) -> None:
    plan = getattr(task, "mission_plan", None)
    if not plan:
        return
    by_id = {stage.id: stage for stage in getattr(task, "stages", []) or []}
    projected: list[TaskStage] = []
    for typed in plan.stages:
        existing = by_id.get(typed.id)
        visual_gate = _plan_stage_has_visual_gate(typed)
        test_plan = []
        if typed.proof_recipe_id and not visual_gate:
            test_plan.append(f"proof_recipe:{typed.proof_recipe_id}")
        defaults = _legacy_stage_defaults(task, typed, plan)
        if existing is None:
            existing = TaskStage(
                id=typed.id,
                title=typed.title,
                objective=typed.objective,
                status=typed.status,
                affected_paths=list(defaults.get("affected_paths") or []),
                acceptance_criteria=list((plan.mission_intent.acceptance_criteria if plan.mission_intent else []) or []),
                test_plan=[] if visual_gate else test_plan or list(defaults.get("test_plan") or []),
                requires_visual_proof=typed.requires_visual_proof,
                created_at=typed.created_at or now(),
                updated_at=typed.updated_at or now(),
            )
            for field in ("affected_paths", "acceptance_criteria", "test_plan", "audit_notes", "corrections"):
                typed_value = list(getattr(typed, field, []) or [])
                if typed_value:
                    setattr(existing, field, typed_value)
        else:
            existing.title = typed.title
            existing.objective = typed.objective
            existing.status = typed.status
            existing.requires_visual_proof = typed.requires_visual_proof
            if visual_gate:
                existing.test_plan = []
                typed.test_plan = []
            elif test_plan:
                existing.test_plan = list(test_plan)
            elif defaults.get("test_plan") and not existing.test_plan:
                existing.test_plan = list(defaults["test_plan"])
            if defaults.get("affected_paths") and not existing.affected_paths:
                existing.affected_paths = list(defaults["affected_paths"])
            existing.updated_at = typed.updated_at or existing.updated_at or now()
            for field in ("affected_paths", "acceptance_criteria", "test_plan", "audit_notes", "corrections"):
                if field == "test_plan" and visual_gate:
                    existing.test_plan = []
                    typed.test_plan = []
                    continue
                typed_value = list(getattr(typed, field, []) or [])
                if typed_value:
                    setattr(existing, field, typed_value)
                else:
                    setattr(typed, field, list(getattr(existing, field, []) or []))
        projected.append(existing)
    task.stages = projected
    task.current_stage_id = plan.current_stage_id
    if plan.mission_intent:
        task.requires_visual_proof = bool(task.requires_visual_proof or any(stage.requires_visual_proof for stage in plan.stages))


def _plan_stage_has_visual_gate(stage: MissionPlanStage) -> bool:
    gate = getattr(stage, "proof_gate", {}) or {}
    required = {str(item).strip().lower() for item in (gate.get("required_proof_types") or []) if str(item).strip()}
    return bool(
        getattr(stage, "requires_product_edit", None) is not True
        and (getattr(stage, "requires_visual_proof", False) or gate.get("visual_required") is True or required & {"screenshot", "video"})
    )


def current_plan_stage(task: Task) -> MissionPlanStage | None:
    plan = getattr(task, "mission_plan", None)
    if not plan:
        return None
    current_id = str(plan.current_stage_id or getattr(task, "current_stage_id", "") or "").strip()
    if current_id:
        for stage in plan.stages:
            if stage.id == current_id:
                return stage
    return next((stage for stage in plan.stages if stage.status not in READY_STATUSES and stage.owner != "qa"), None)


def next_unblocked_stage(task: Task, *, include_qa: bool = True) -> MissionPlanStage | None:
    plan = getattr(task, "mission_plan", None)
    if not plan:
        return None
    passed_or_ready = {stage.id for stage in plan.stages if stage.status in READY_STATUSES}
    for stage in plan.stages:
        if stage.status in READY_STATUSES:
            continue
        if not include_qa and stage.owner == "qa":
            continue
        if all(dep in passed_or_ready for dep in stage.depends_on):
            return stage
    return None


def next_incomplete_blocking_stage(task: Task) -> MissionPlanStage | None:
    plan = getattr(task, "mission_plan", None)
    if not plan:
        return None
    for stage in plan.stages:
        if stage.owner == "qa":
            continue
        if stage.blocks_qa_until and stage.status not in READY_STATUSES:
            return stage
    return None


def blocking_stages_ready_for_qa(task: Task, *, proof_store=None) -> tuple[bool, list[str]]:
    plan = getattr(task, "mission_plan", None)
    if not plan:
        return True, []
    missing: list[str] = []
    for stage in plan.stages:
        if not stage.blocks_qa_until or stage.owner == "qa":
            continue
        delivered_bundle = _stage_repo_bundle_delivered(task, stage)
        if stage.status not in READY_STATUSES and not delivered_bundle:
            missing.append(f"typed stage {stage.id} is {stage.status.value}, not ready_for_qa")
            continue
        if delivered_bundle:
            continue
        if _stage_requires_passed_command(stage) and not _has_passed_command_proof(task, stage, proof_store=proof_store):
            missing.append(f"typed stage {stage.id} missing passed command proof")
        if stage.requires_visual_proof and not _has_passed_visual_proof(task, stage, proof_store=proof_store):
            missing.append(f"typed stage {stage.id} missing screenshot or video proof")
    return not missing, missing


def _stage_repo_bundle_delivered(task: Task, stage: MissionPlanStage) -> bool:
    repo = str(getattr(stage, "repo", "") or "").strip()
    if not repo:
        return False
    try:
        from .repo_bundles import RepoBundleStore

        bundles = RepoBundleStore().list_for_task(task.id)
    except Exception:
        return False
    for bundle in bundles:
        if str(getattr(bundle, "repo", "") or "") != repo:
            continue
        if str(getattr(bundle, "state", "") or "").lower() in {"delivered", "delivered_waiting_for_qa", "verified"}:
            return True
    return False


def all_blocking_stages_passed(task: Task) -> bool:
    plan = getattr(task, "mission_plan", None)
    if not plan:
        return False
    return all((not stage.blocks_qa_until) or stage.status == StageStatus.PASSED for stage in plan.stages if stage.owner != "qa")


def attach_proofs_to_plan_stage(task: Task, stage_id: str | None, proof_ids: Iterable[str], *, proof_store=None) -> None:
    plan = getattr(task, "mission_plan", None)
    if not plan:
        return
    sid = str(stage_id or plan.current_stage_id or getattr(task, "current_stage_id", "") or "").strip()
    stage = _stage_by_id(plan, sid) if sid else current_plan_stage(task)
    if stage is None:
        stage = current_plan_stage(task)
    if stage is None:
        return
    added = False
    for proof_id in proof_ids:
        clean = str(proof_id).strip()
        if clean and clean not in stage.proof_ids:
            stage.proof_ids.append(clean)
            added = True
    if not added:
        return
    _refresh_stage_status_from_proofs(task, stage, proof_store=proof_store)
    stage.updated_at = now()
    plan.revision = int(plan.revision or 0) + 1
    _sync_task_stage_compat_from_plan(task)


def mark_plan_stage_from_decision(task: Task, decision, *, actor: str, proof_store=None) -> None:
    plan = getattr(task, "mission_plan", None)
    if not plan:
        return
    stage = current_plan_stage(task)
    if stage is None:
        return
    if decision.type in {DecisionType.HAND_OFF, DecisionType.PROPOSE_PATCH} and actor in {"dev", "backend_dev"}:
        if stage.kind == "implementation" and stage.status in {StageStatus.READY, StageStatus.DRAFT, StageStatus.BLOCKED, StageStatus.REWORK}:
            stage.status = StageStatus.IMPLEMENTING
            stage.updated_at = now()
        elif stage.kind in {"context", "investigation", "audit"} and not stage.requires_product_edit:
            stage.status = StageStatus.READY_FOR_QA
            stage.updated_at = now()
    elif decision.type == DecisionType.REQUEST_TEST_RUN:
        requested = str(decision.payload.get("stage_id") or stage.id).strip()
        target = _stage_by_id(plan, requested) or stage
        if target.status in {StageStatus.DRAFT, StageStatus.READY, StageStatus.BLOCKED, StageStatus.REWORK}:
            target.status = StageStatus.IMPLEMENTING
            target.updated_at = now()
    elif decision.type in {DecisionType.QA_VERDICT, DecisionType.REPORT_QA_VERDICT}:
        verdict = str(decision.payload.get("verdict") or "").strip()
        if verdict == "approved":
            ready, missing = blocking_stages_ready_for_qa(task, proof_store=proof_store)
            if not ready:
                raise DecisionPayloadInvalid(f"QA approval blocked by typed mission plan: {missing}")
            for item in plan.stages:
                if item.blocks_qa_until and item.owner != "qa":
                    item.status = StageStatus.PASSED
                    item.updated_at = now()
            qa_stage = next((item for item in plan.stages if item.owner == "qa" or item.kind == "qa_verdict"), None)
            if qa_stage is not None:
                qa_stage.status = StageStatus.PASSED
                qa_stage.updated_at = now()
            plan.current_stage_id = None
            task.current_stage_id = None
        elif verdict in {"needs_fixes", "blocked"}:
            failed = _first_stage_with_missing_proof(task, proof_store=proof_store)
            if failed is not None:
                failed.status = StageStatus.REWORK if verdict == "needs_fixes" else StageStatus.BLOCKED
                failed.updated_at = now()
                plan.current_stage_id = failed.id
                task.current_stage_id = failed.id
    plan.revision = int(plan.revision or 0) + 1
    _sync_task_stage_compat_from_plan(task)


def release_next_stage(task: Task, stage_id: str | None = None) -> MissionPlanStage | None:
    plan = getattr(task, "mission_plan", None)
    if not plan:
        return None
    target = _stage_by_id(plan, stage_id) if stage_id else next_unblocked_stage(task, include_qa=True)
    if target is None:
        return None
    plan.current_stage_id = target.id
    if target.status in {StageStatus.DRAFT, StageStatus.READY, StageStatus.BLOCKED} and target.owner not in {"qa", "neko_supervisor"}:
        target.status = StageStatus.IMPLEMENTING
    target.updated_at = now()
    plan.revision = int(plan.revision or 0) + 1
    _sync_task_stage_compat_from_plan(task)
    return target


def mission_plan_summary(task: Task) -> dict[str, Any] | None:
    plan = getattr(task, "mission_plan", None)
    if not plan:
        return None
    return {
        "enabled": plan.enabled,
        "version": plan.version,
        "current_stage_id": plan.current_stage_id,
        "revision": plan.revision,
        "blueprint_id": plan.blueprint_id,
        "blueprint_version": plan.blueprint_version,
        "slots": dict(plan.slots),
        "bindings": dict(plan.bindings),
        "binding_sources": dict(plan.binding_sources),
        "edges": list(plan.edges),
        "agent_topology": dict(plan.agent_topology),
        "limits": dict(plan.limits),
        "stage_attempts": dict(plan.stage_attempts),
        "on_unhandled": plan.on_unhandled,
        "mission_intent": {
            "title": plan.mission_intent.title,
            "objective": plan.mission_intent.objective,
            "acceptance_criteria": list(plan.mission_intent.acceptance_criteria),
            "non_goals": list(plan.mission_intent.non_goals),
            "locked": plan.mission_intent.locked,
        } if plan.mission_intent else None,
        "stages": [
            {
                "id": stage.id,
                "title": stage.title,
                "owner": stage.owner,
                "owner_slot": stage.owner_slot or stage.owner,
                "repo": stage.repo,
                "kind": stage.kind,
                "status": stage.status.value,
                "output_type": stage.output_type,
                "proof_recipe_id": stage.proof_recipe_id,
                "proof_gate": dict(getattr(stage, "proof_gate", {}) or {}),
                "requires_product_edit": stage.requires_product_edit,
                "requires_visual_proof": stage.requires_visual_proof,
                "affected_paths": list(stage.affected_paths),
                "acceptance_criteria": list(stage.acceptance_criteria),
                "test_plan": list(stage.test_plan),
                "depends_on": list(stage.depends_on),
                "blocks_qa_until": stage.blocks_qa_until,
                "proof_ids": list(stage.proof_ids),
                "packet_ids": list(stage.packet_ids),
                "blocker_ids": list(stage.blocker_ids),
            }
            for stage in plan.stages
        ],
    }


def _mission_intent_from_task(task: Task) -> MissionIntent:
    return MissionIntent(
        title=str(task.title or "").strip(),
        objective=str(task.description or task.title or "").strip(),
        acceptance_criteria=list(task.acceptance_criteria or []),
        non_goals=list(task.non_goals or []),
        source_task_id=task.id,
        locked=True,
    )


def _plan_from_payload(task: Task, raw: dict[str, Any], *, existing: MissionPlan) -> MissionPlan:
    _validate_raw_plan_keys(raw)
    intent_raw = raw.get("mission_intent") if isinstance(raw.get("mission_intent"), dict) else {}
    intent = existing.mission_intent or _mission_intent_from_task(task)
    if intent_raw and not intent.locked:
        intent = MissionIntent(
            title=str(intent_raw.get("title") or intent.title),
            objective=str(intent_raw.get("objective") or intent.objective),
            acceptance_criteria=_string_list(intent_raw.get("acceptance_criteria")) or list(intent.acceptance_criteria),
            non_goals=_string_list(intent_raw.get("non_goals")) or list(intent.non_goals),
            source_task_id=str(intent_raw.get("source_task_id") or intent.source_task_id or task.id),
            locked=bool(intent_raw.get("locked", True)),
        )
    stages = [_stage_from_raw(task, item, intent=intent) for item in raw.get("stages", []) if isinstance(item, dict)]
    stages = _normalize_stages_for_task_scope(task, intent, stages)
    if _parent_requires_launcher(task, intent) and not any(stage.repo == "EterniaLauncher" and stage.owner in {"dev", "qa"} for stage in stages):
        stages.extend(_default_launcher_and_qa_stages(task, intent, depends_on=[stage.id for stage in stages if stage.owner != "qa"][:1]))
    plan = MissionPlan(
        version=int(raw.get("version") or existing.version or 1),
        enabled=bool(raw.get("enabled", True)),
        mission_intent=intent,
        stages=stages,
        current_stage_id=str(raw.get("current_stage_id") or "").strip() or (stages[0].id if stages else None),
        revision=int(existing.revision or 0),
        blueprint_id=str(raw.get("blueprint_id") or existing.blueprint_id or "").strip() or None,
        blueprint_version=int(raw.get("blueprint_version") or existing.blueprint_version or 0) or None,
        slots=dict(raw.get("slots") if isinstance(raw.get("slots"), dict) else existing.slots),
        bindings={str(k): str(v) for k, v in (raw.get("bindings") if isinstance(raw.get("bindings"), dict) else existing.bindings).items()},
        binding_sources={str(k): str(v) for k, v in (raw.get("binding_sources") if isinstance(raw.get("binding_sources"), dict) else existing.binding_sources).items()},
        edges=list(raw.get("edges") if isinstance(raw.get("edges"), list) else existing.edges),
        agent_topology=dict(raw.get("agent_topology") if isinstance(raw.get("agent_topology"), dict) else existing.agent_topology),
        limits=dict(raw.get("limits") if isinstance(raw.get("limits"), dict) else existing.limits),
        stage_attempts={str(k): int(v) for k, v in (raw.get("stage_attempts") if isinstance(raw.get("stage_attempts"), dict) else existing.stage_attempts).items()},
        on_unhandled=str(raw.get("on_unhandled") or existing.on_unhandled or "intervention"),
    )
    errors = validate_mission_plan(plan)
    if errors:
        raise DecisionPayloadInvalid(f"invalid mission_plan: {errors}")
    return plan


def _apply_plan_patch(task: Task, plan: MissionPlan, raw: dict[str, Any]) -> MissionPlan:
    _validate_raw_plan_keys(raw)
    stages = list(plan.stages)
    by_id = {stage.id: stage for stage in stages}
    for item in raw.get("stages", []) or []:
        if not isinstance(item, dict):
            continue
        stage = _stage_from_raw(task, item, intent=plan.mission_intent or _mission_intent_from_task(task), existing=by_id.get(str(item.get("id") or "")))
        by_id[stage.id] = stage
    stages = list(by_id.values())
    patched = replace(plan, stages=stages, current_stage_id=str(raw.get("current_stage_id") or plan.current_stage_id or "").strip() or None)
    errors = validate_mission_plan(patched)
    if errors:
        raise DecisionPayloadInvalid(f"invalid mission_plan_patch: {errors}")
    return patched


def _is_persona_diagnostic_self_observation(task: Task, handoff: dict[str, Any]) -> bool:
    if str(handoff.get("target_owner") or "").strip() != "neko_supervisor":
        return False
    if str(handoff.get("mission_phase") or "").strip() not in {"neko_only_contract_diagnostic", "neko_only_contract_probe"}:
        return False
    flags = {str(flag or "").strip() for flag in (getattr(task, "risk_flags", []) or [])}
    return "persona_operation" in flags and "diagnostic_persona:neko_supervisor" in flags


def _stage_from_raw(task: Task, raw: dict[str, Any], *, intent: MissionIntent, existing: MissionPlanStage | None = None) -> MissionPlanStage:
    sid = _safe_stage_identifier(str(raw.get("id") or (existing.id if existing else "") or "stage"))
    owner = _canonical_owner(str(raw.get("owner") or (existing.owner if existing else "dev")))
    owner_slot = str(raw.get("owner_slot") or (existing.owner_slot if existing else "") or owner).strip()
    repo = _canonical_repo(str(raw.get("repo") or (existing.repo if existing else "EterniaLauncher")))
    proof_gate = raw.get("proof_gate") if isinstance(raw.get("proof_gate"), dict) else dict(existing.proof_gate if existing else {})
    output_type = str(raw.get("output_type") or (existing.output_type if existing else "") or "").strip() or None
    recipe = str(raw.get("proof_recipe_id") or proof_gate.get("proof_recipe_id") or proof_gate.get("recipe_id") or (existing.proof_recipe_id if existing else "") or "").strip() or None
    if (
        recipe
        and repo == "hermes-agent"
        and recipe in {"harness_runtime_status_snapshot", "qa_release_verdict_smoke"}
        and _raw_stage_agent_runtime_test_files(raw, intent=intent, task=task)
    ):
        recipe = None
    if recipe:
        resolve_proof_recipe(recipe)
    kind = str(raw.get("kind") or (existing.kind if existing else ("proof_only" if recipe else "implementation"))).strip() or "implementation"
    if kind not in ALLOWED_KINDS:
        kind = "proof_only" if recipe else "implementation"
    no_edit_stage = _raw_stage_is_no_product_edit(raw, intent=intent, task=task)
    if no_edit_stage and owner in {"dev", "backend_dev"}:
        kind = "proof_only"
    requires_product_edit = bool(raw.get("requires_product_edit", existing.requires_product_edit if existing else kind == "implementation"))
    requires_visual_proof = bool(raw.get("requires_visual_proof", existing.requires_visual_proof if existing else False))
    if kind == "proof_only":
        requires_product_edit = False
    if no_edit_stage and raw.get("requires_product_edit") is None:
        requires_product_edit = False
    if kind == "implementation" and raw.get("requires_product_edit") is None:
        requires_product_edit = True
    return MissionPlanStage(
        id=sid,
        title=str(raw.get("title") or (existing.title if existing else sid.replace("_", " ").title())).strip(),
        objective=str(raw.get("objective") or (existing.objective if existing else intent.objective)).strip(),
        owner=owner,
        owner_slot=owner_slot,
        repo=repo,
        kind=kind,
        status=_stage_status(raw.get("status"), existing.status if existing else StageStatus.READY),
        proof_recipe_id=recipe,
        proof_gate=dict(proof_gate),
        output_type=output_type,
        requires_product_edit=requires_product_edit,
        requires_visual_proof=requires_visual_proof,
        depends_on=_string_list(raw.get("depends_on")) or list(existing.depends_on if existing else []),
        blocks_qa_until=bool(raw.get("blocks_qa_until", existing.blocks_qa_until if existing else owner != "qa")),
        proof_ids=_string_list(raw.get("proof_ids")) or list(existing.proof_ids if existing else []),
        packet_ids=_string_list(raw.get("packet_ids")) or list(existing.packet_ids if existing else []),
        blocker_ids=_string_list(raw.get("blocker_ids")) or list(existing.blocker_ids if existing else []),
        created_at=existing.created_at if existing else now(),
        updated_at=now(),
    )


def _make_stage(
    sid: str,
    *,
    title: str,
    objective: str,
    owner: str,
    repo: str,
    kind: str,
    proof_recipe_id: str | None = None,
    output_type: str | None = None,
    requires_product_edit: bool = False,
    requires_visual_proof: bool = False,
    depends_on: list[str] | None = None,
    blocks_qa_until: bool = True,
) -> MissionPlanStage:
    return MissionPlanStage(
        id=_safe_stage_identifier(sid),
        title=title,
        objective=objective,
        owner=owner,
        owner_slot=owner,
        repo=repo,
        kind=kind,
        proof_recipe_id=proof_recipe_id,
        output_type=output_type,
        requires_product_edit=requires_product_edit,
        requires_visual_proof=requires_visual_proof,
        depends_on=list(depends_on or []),
        blocks_qa_until=blocks_qa_until,
        created_at=now(),
        updated_at=now(),
    )


def _default_launcher_and_qa_stages(task: Task, intent: MissionIntent, *, depends_on: list[str], requires_product_edit: bool = True) -> list[MissionPlanStage]:
    launcher = _make_stage(
        "launcher_implementation",
        title="Launcher Mission Control Implementation" if requires_product_edit else "Launcher Route Proof",
        objective="Complete the Launcher/Mission Control side of the parent goal." if requires_product_edit else "Attach the Launcher-side no-product-edit route proof for the parent goal.",
        owner="dev",
        repo="EterniaLauncher",
        kind="implementation" if requires_product_edit else "proof_only",
        requires_product_edit=requires_product_edit,
        requires_visual_proof=_parent_requires_visual(task, intent),
        depends_on=depends_on,
    )
    return [launcher, _qa_stage(intent, depends_on=[*depends_on, launcher.id], repo="EterniaLauncher")]


def _legacy_stage_defaults(task: Task, typed: MissionPlanStage, plan: MissionPlan) -> dict[str, list[str]]:
    exact = _exact_goal_proof_stage_defaults(task, typed)
    if exact is not None:
        return exact
    if _is_hermes_agent_no_edit_proof_stage(task, typed, plan):
        return _hermes_agent_no_edit_proof_defaults(task, typed, plan)
    if _is_hermes_agent_implementation(task, typed, plan):
        return _hermes_agent_implementation_defaults(task, typed, plan)
    if _is_launcher_post_media_stage(task, typed, plan):
        return _launcher_post_media_defaults()
    if not _is_mission_control_launcher_implementation(task, typed, plan):
        return {}
    return {
        "affected_paths": [
            "lib/features/mission_control/",
            "test/features/mission_control/",
        ],
        "test_plan": [
            "flutter analyze lib/features/mission_control test/features/mission_control",
            "flutter test test/features/mission_control",
        ],
    }


def _exact_goal_proof_stage_defaults(task: Task, typed: MissionPlanStage) -> dict[str, list[str]] | None:
    if typed.owner not in {"dev", "backend_dev"} or typed.kind not in {"implementation", "proof_only"}:
        return None
    if not goal_demands_exact_proof(task):
        return None
    if not _exact_goal_proof_applies_to_repo(task, typed.repo):
        return None
    commands = goal_named_gate_commands(task, typed.repo)
    if not commands:
        return None
    return {
        "affected_paths": _extract_goal_repo_relative_paths(task),
        "test_plan": commands,
    }


def _exact_goal_proof_applies_to_repo(task: Task, repo: str) -> bool:
    stage_repo = _canonical_repo(repo)
    if stage_repo not in {"EterniaLauncher", "EterniaBackend", "hermes-agent"}:
        return False
    repos = [
        _canonical_repo(str(item))
        for item in (getattr(task, "affected_repos", []) or [])
        if str(item or "").strip()
    ]
    repos = [item for item in repos if item != "none"]
    if repos and stage_repo not in repos:
        return False
    if len(set(repos)) > 1:
        return False
    return True


def _extract_goal_repo_relative_paths(task: Task) -> list[str]:
    intent = getattr(getattr(task, "mission_plan", None), "mission_intent", None)
    text = " ".join(
        [
            str(getattr(task, "title", "") or ""),
            str(getattr(task, "description", "") or ""),
            " ".join(str(item) for item in (getattr(task, "acceptance_criteria", []) or [])),
            " ".join(str(item) for item in (getattr(task, "non_goals", []) or [])),
            " ".join(str(item) for item in (getattr(task, "operator_notes", []) or [])),
            str(getattr(intent, "title", "") or "") if intent is not None else "",
            str(getattr(intent, "objective", "") or "") if intent is not None else "",
            " ".join(str(item) for item in (getattr(intent, "acceptance_criteria", []) or [])) if intent is not None else "",
            " ".join(str(item) for item in (getattr(intent, "non_goals", []) or [])) if intent is not None else "",
        ]
    )
    found: list[str] = []
    for match in re.findall(r"(?<![\w:/\\.-])(?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.-]+", text):
        path = match.replace("\\", "/").rstrip(".,;:)]}")
        first = path.split("/", 1)[0].lower()
        if first in {"http", "https", "python", "pytest", "flutter", "dart", "npm", "pnpm"}:
            continue
        leaf = path.rsplit("/", 1)[-1]
        if "." not in leaf and first not in {"agent_runtime", "assets", "docs", "integration_test", "lib", "scripts", "src", "test", "tests"}:
            continue
        if path and path not in found:
            found.append(path)
    return found[:8]


def _is_mission_control_launcher_implementation(task: Task, typed: MissionPlanStage, plan: MissionPlan) -> bool:
    if typed.repo != "EterniaLauncher" or typed.owner != "dev" or typed.kind != "implementation":
        return False
    intent = plan.mission_intent or _mission_intent_from_task(task)
    text = _parent_text(task, intent)
    stage_text = " ".join([typed.id, typed.title, typed.objective]).lower()
    return "mission control" in text or "mission_control" in text or "mission control" in stage_text or "mission_control" in stage_text


def _is_launcher_post_media_stage(task: Task, typed: MissionPlanStage, plan: MissionPlan) -> bool:
    if typed.repo != "EterniaLauncher" or typed.owner != "dev" or typed.kind not in {"implementation", "proof_only"}:
        return False
    intent = plan.mission_intent or _mission_intent_from_task(task)
    text = " ".join([_parent_text(task, intent), typed.id, typed.title, typed.objective]).lower()
    post_markers = ("post", "posts", "feed")
    media_markers = (
        "thumbnail",
        "thumbnails",
        "image",
        "images",
        "video",
        "videos",
        "portrait video",
        "media",
    )
    return any(marker in text for marker in post_markers) and any(marker in text for marker in media_markers)


def _launcher_post_media_defaults() -> dict[str, list[str]]:
    return {
        "affected_paths": [
            "lib/features/posts/",
            "test/features/posts/",
        ],
        "test_plan": [
            "flutter analyze lib/features/posts test/features/posts",
            "flutter test test/features/posts",
        ],
    }


def _is_hermes_agent_implementation(task: Task, typed: MissionPlanStage, plan: MissionPlan) -> bool:
    return typed.repo == "hermes-agent" and typed.owner in {"dev", "backend_dev"} and typed.kind == "implementation"


def _is_hermes_agent_no_edit_proof_stage(task: Task, typed: MissionPlanStage, plan: MissionPlan) -> bool:
    if typed.repo != "hermes-agent" or typed.owner not in {"dev", "backend_dev"}:
        return False
    if typed.requires_product_edit:
        return False
    if typed.kind == "proof_only":
        return True
    intent = plan.mission_intent or _mission_intent_from_task(task)
    return _text_has_no_product_edit_marker(" ".join([_parent_text(task, intent), typed.id, typed.title, typed.objective]))


def _hermes_agent_no_edit_proof_defaults(task: Task, typed: MissionPlanStage, plan: MissionPlan) -> dict[str, list[str]]:
    intent = plan.mission_intent or _mission_intent_from_task(task)
    text = " ".join([_parent_text(task, intent), typed.id, typed.title, typed.objective])
    test_files = _extract_agent_runtime_test_files(text)
    if test_files:
        return {
            "affected_paths": test_files,
            "test_plan": [f"python -m pytest {' '.join(test_files)} -q"],
        }
    if typed.proof_recipe_id:
        return {}
    return {
        "affected_paths": ["tests/agent_runtime/"],
        "test_plan": ["python -m pytest tests/agent_runtime -q"],
    }


def _hermes_agent_implementation_defaults(task: Task, typed: MissionPlanStage, plan: MissionPlan) -> dict[str, list[str]]:
    intent = plan.mission_intent or _mission_intent_from_task(task)
    text = " ".join([_parent_text(task, intent), typed.id, typed.title, typed.objective]).lower()
    if any(marker in text for marker in ("goalrunner", "goal runner", "goal_run", "final_summary", "proof_summary", "blocker_summary")):
        return {
            "affected_paths": [
                "agent_runtime/goal_runner.py",
                "agent_runtime/mission_plan.py",
                "agent_runtime/planning.py",
                "tests/agent_runtime/test_goal_runner.py",
                "tests/agent_runtime/test_planning.py",
            ],
            "test_plan": [
                "python -m pytest tests/agent_runtime/test_goal_runner.py tests/agent_runtime/test_planning.py -q",
            ],
        }
    return {
        "affected_paths": [
            "agent_runtime/",
            "tests/agent_runtime/",
        ],
        "test_plan": [
            "python -m pytest tests/agent_runtime -q",
        ],
    }


def _qa_stage(intent: MissionIntent, *, depends_on: list[str], repo: str) -> MissionPlanStage:
    return _make_stage(
        "qa_release",
        title="QA Release Verdict",
        objective=f"Verify typed mission plan coverage for {intent.title}.",
        owner="qa",
        repo=repo if repo in ALLOWED_REPOS and repo != "none" else "EterniaLauncher",
        kind="qa_verdict",
        depends_on=depends_on,
        blocks_qa_until=False,
    )


def _refresh_stage_status_from_proofs(task: Task, stage: MissionPlanStage, *, proof_store=None) -> None:
    if _stage_has_failed_proof(task, stage, proof_store=proof_store):
        stage.status = StageStatus.REWORK
        return
    if _stage_required_proofs_pass(task, stage, proof_store=proof_store):
        stage.status = StageStatus.READY_FOR_QA
    elif stage.status in {StageStatus.DRAFT, StageStatus.READY, StageStatus.BLOCKED}:
        stage.status = StageStatus.IMPLEMENTING


def _stage_required_proofs_pass(task: Task, stage: MissionPlanStage, *, proof_store=None) -> bool:
    if _stage_requires_passed_command(stage) and not _has_passed_command_proof(task, stage, proof_store=proof_store):
        return False
    if stage.requires_visual_proof and not _has_passed_visual_proof(task, stage, proof_store=proof_store):
        return False
    if stage.kind == "implementation" and _has_passed_command_proof(task, stage, proof_store=proof_store):
        return True
    if not _stage_requires_passed_command(stage) and not stage.requires_visual_proof:
        return stage.kind == "proof_only" and bool(stage.proof_ids)
    return True


def _stage_requires_passed_command(stage: MissionPlanStage) -> bool:
    return bool(stage.proof_recipe_id or stage.kind == "proof_only")


def _has_passed_command_proof(task: Task, stage: MissionPlanStage, *, proof_store=None) -> bool:
    for proof in _stage_proofs(task, stage, proof_store=proof_store):
        if proof.type != ProofType.TEST_RUN:
            continue
        metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
        if str(metadata.get("status") or "").strip().lower() == "passed" or _exit_code(metadata) == 0:
            if stage.proof_recipe_id and str(metadata.get("proof_recipe_id") or metadata.get("recipe_id") or "").strip() not in {"", stage.proof_recipe_id}:
                continue
            return True
    return False


def _has_passed_visual_proof(task: Task, stage: MissionPlanStage, *, proof_store=None) -> bool:
    for proof in _stage_proofs(task, stage, proof_store=proof_store):
        if proof.type in {ProofType.SCREENSHOT, ProofType.VIDEO} and proof.path_or_value and proof.redaction_status == "safe":
            return True
    return False


def _stage_has_failed_proof(task: Task, stage: MissionPlanStage, *, proof_store=None) -> bool:
    latest_recoverable_status: str | None = None
    for proof in _stage_proofs(task, stage, proof_store=proof_store):
        metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
        status = str(metadata.get("status") or metadata.get("verdict") or "").strip().lower()
        if proof.type == ProofType.TEST_RUN:
            latest_recoverable_status = status or ("passed" if _exit_code(metadata) == 0 else "failed")
            continue
        if status in {"failed", "blocked", "timed_out"}:
            latest_recoverable_status = status
    return latest_recoverable_status in {"failed", "blocked", "timed_out"}


def _stage_proofs(task: Task, stage: MissionPlanStage, *, proof_store=None) -> list[Proof]:
    proofs: list[Proof] = []
    ids = list(stage.proof_ids or [])
    if not ids:
        ids = [proof_id for proof_id in getattr(task, "proof_ids", []) or [] if _safe_token(stage.id) in _safe_token(proof_id)]
    if proof_store is None:
        return []
    for proof_id in ids:
        try:
            proof = proof_store.get(proof_id)
        except Exception:
            continue
        if proof.task_id == task.id and (not proof.stage_id or proof.stage_id == stage.id):
            proofs.append(proof)
    return proofs


def _first_stage_with_missing_proof(task: Task, *, proof_store=None) -> MissionPlanStage | None:
    plan = getattr(task, "mission_plan", None)
    if not plan:
        return None
    for stage in plan.stages:
        if stage.owner == "qa" or not stage.blocks_qa_until:
            continue
        if stage.status not in READY_STATUSES or not _stage_required_proofs_pass(task, stage, proof_store=proof_store):
            return stage
    return None


def _stage_by_id(plan: MissionPlan, stage_id: str | None) -> MissionPlanStage | None:
    sid = str(stage_id or "").strip()
    return next((stage for stage in plan.stages if stage.id == sid), None)


def _set_current_stage(plan: MissionPlan, stage_id: str) -> None:
    if not _stage_by_id(plan, stage_id):
        raise DecisionPayloadInvalid(f"release_stage_id is not in mission_plan: {stage_id}")
    plan.current_stage_id = stage_id


def _validate_raw_plan_keys(raw: dict[str, Any]) -> None:
    allowed_plan_keys = {
        "version", "enabled", "mission_intent", "stages", "current_stage_id", "_normalization",
        "blueprint_id", "blueprint_version", "slots", "bindings", "binding_sources",
        "edges", "limits", "stage_attempts", "on_unhandled",
    }
    extra = sorted(set(raw) - allowed_plan_keys)
    if extra:
        _merge_raw_normalization(raw, dropped_fields=[f"mission_plan.{key}" for key in extra])
        for key in extra:
            raw.pop(key, None)
    stages = raw.get("stages")
    if stages is not None:
        if not isinstance(stages, list):
            raise DecisionPayloadInvalid("mission_plan.stages must be a list")
        allowed_stage_keys = {
            "id", "title", "objective", "owner", "owner_slot", "repo", "kind", "status",
            "proof_recipe_id", "proof_gate", "output_type", "requires_product_edit", "requires_visual_proof",
            "depends_on", "blocks_qa_until", "proof_ids", "packet_ids", "blocker_ids", "_normalization",
        }
        for idx, stage in enumerate(stages):
            if not isinstance(stage, dict):
                raise DecisionPayloadInvalid("mission_plan.stages[] must be objects")
            extra_stage = sorted(set(stage) - allowed_stage_keys)
            if extra_stage:
                _merge_raw_normalization(stage, dropped_fields=[f"mission_plan.stages[{idx}].{key}" for key in extra_stage])
                for key in extra_stage:
                    stage.pop(key, None)


def _merge_raw_normalization(raw: dict[str, Any], *, dropped_fields: list[str]) -> None:
    info = raw.get("_normalization") if isinstance(raw.get("_normalization"), dict) else {}
    existing = [str(item) for item in (info.get("dropped_fields") or []) if str(item).strip()]
    merged = []
    seen: set[str] = set()
    for item in [*existing, *dropped_fields]:
        text = str(item or "").strip()
        if text and text not in seen:
            merged.append(text[:160])
            seen.add(text)
    raw["_normalization"] = {"dropped_fields": merged}


def _dependency_cycle_errors(plan: MissionPlan) -> list[str]:
    by_id = {stage.id: stage for stage in plan.stages}
    errors: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(stage_id: str, trail: list[str]) -> None:
        if stage_id in visited:
            return
        if stage_id in visiting:
            errors.append("dependency cycle: " + " -> ".join([*trail, stage_id]))
            return
        visiting.add(stage_id)
        stage = by_id.get(stage_id)
        if stage is not None:
            for dep in stage.depends_on:
                visit(dep, [*trail, stage_id])
        visiting.remove(stage_id)
        visited.add(stage_id)

    for stage in plan.stages:
        visit(stage.id, [])
    return errors


def _recipe_from_payload(payload: dict[str, Any], handoff: dict[str, Any], *, target_repo: str) -> str | None:
    backend_text = " ".join(
        [
            str(payload.get("objective") or ""),
            " ".join(str(item) for item in (payload.get("acceptance_criteria") or [])),
            str(handoff),
        ]
    ).lower()
    candidates: list[tuple[Any, bool]] = [
        (payload.get("proof_recipe_id"), True),
        (payload.get("recipe_id"), True),
        ((handoff.get("proof_gate") or {}).get("proof_recipe_id") if isinstance(handoff.get("proof_gate"), dict) else None, True),
        ((handoff.get("proof_gate") or {}).get("recipe_id") if isinstance(handoff.get("proof_gate"), dict) else None, True),
    ]
    if target_repo == "EterniaBackend" and _is_backend_no_edit_recipe_text(backend_text):
        candidates.append(("backend_contract_smoke", False))
    if target_repo == "hermes-agent":
        text = " ".join(
            [
                str(payload.get("objective") or ""),
                " ".join(str(item) for item in (payload.get("acceptance_criteria") or [])),
                str(handoff),
            ]
        ).lower()
        if _is_harness_no_edit_recipe_text(text):
            candidates.append(("harness_runtime_status_snapshot", False))
    for candidate, explicit in candidates:
        recipe = str(candidate or "").strip()
        if not recipe:
            continue
        if not explicit and target_repo == "EterniaBackend" and recipe == "backend_contract_smoke" and not _is_backend_no_edit_recipe_text(backend_text):
            continue
        try:
            resolve_proof_recipe(recipe)
        except Exception:
            continue
        return recipe
    return None


def _stage_id_for(repo: str, recipe: str | None, objective: str) -> str:
    if recipe:
        return recipe
    if repo == "EterniaBackend" and _text_mentions_investigation(objective):
        return "backend_investigation"
    if repo == "EterniaBackend":
        return "backend_implementation"
    if repo == "EterniaLauncher":
        return "launcher_implementation"
    return _safe_stage_identifier(objective) or "stage_1"


def _stage_title_for(repo: str, recipe: str | None) -> str:
    if recipe == "harness_runtime_status_snapshot":
        return "Harness Runtime Status Snapshot"
    if recipe == "backend_contract_smoke":
        return "Backend Contract Smoke"
    if repo == "EterniaBackend":
        return "Backend Investigation"
    if repo == "EterniaLauncher":
        return "Launcher Implementation"
    return "Implementation"


def _is_harness_no_edit_recipe_text(text: str) -> bool:
    lowered = str(text or "").lower()
    if "harness" not in lowered:
        return False
    if any(marker in lowered for marker in ("implement", "add ", "update", "patch", "change", "code", "test cover", "focused tests")):
        return False
    return any(marker in lowered for marker in ("status", "snapshot", "log", "logs", "thinking", "observability", "smoke"))


def _is_backend_no_edit_recipe_text(text: str) -> bool:
    lowered = str(text or "").lower()
    if any(marker in lowered for marker in ("implement", "add ", "update", "patch", "change", "code", "focused tests", "test coverage", "product-edit", "product edit")):
        return False
    return any(marker in lowered for marker in ("no-product-edit", "no product edit", "no edit", "certification", "contract smoke", "smoke proof", "status snapshot"))


def _raw_stage_is_no_product_edit(raw: dict[str, Any], *, intent: MissionIntent, task: Task) -> bool:
    if raw.get("requires_product_edit") is True and not _locked_intent_forbids_product_edits(intent, task):
        return False
    text = " ".join(
        [
            str(raw.get("id") or ""),
            str(raw.get("title") or ""),
            str(raw.get("objective") or ""),
            str(raw.get("kind") or ""),
            str(intent.objective or ""),
            " ".join(str(item) for item in (intent.non_goals or [])),
            str(getattr(task, "description", "") or ""),
            " ".join(str(item) for item in (getattr(task, "non_goals", []) or [])),
        ]
    )
    if not _text_has_no_product_edit_marker(text):
        return False
    return any(marker in text.lower() for marker in ("proof", "verify", "verification", "smoke", "test", "tests/"))


def _task_is_no_product_edit_certification(
    task: Task,
    intent: MissionIntent,
    *,
    payload: dict[str, Any],
    handoff: dict[str, Any],
    objective: str,
) -> bool:
    text = " ".join(
        [
            str(getattr(task, "title", "") or ""),
            str(getattr(task, "description", "") or ""),
            " ".join(str(item) for item in (getattr(task, "acceptance_criteria", []) or [])),
            " ".join(str(item) for item in (getattr(task, "non_goals", []) or [])),
            str(intent.title or ""),
            str(intent.objective or ""),
            " ".join(str(item) for item in (intent.acceptance_criteria or [])),
            " ".join(str(item) for item in (intent.non_goals or [])),
            str(objective or ""),
            str(payload),
            str(handoff),
        ]
    )
    if not _text_has_no_product_edit_marker(text):
        return False
    lowered = text.lower()
    if not any(marker in lowered for marker in ("certif", "verify", "verification", "proof", "smoke", "test", "already landed", "committed")):
        return False
    return not any(marker in lowered for marker in ("product edit required", "must edit product", "requires product edit"))


def _task_is_no_product_edit_investigation(
    task: Task,
    intent: MissionIntent,
    *,
    payload: dict[str, Any],
    handoff: dict[str, Any],
    objective: str,
) -> bool:
    text = " ".join(
        [
            str(getattr(task, "title", "") or ""),
            str(getattr(task, "description", "") or ""),
            " ".join(str(item) for item in (getattr(task, "acceptance_criteria", []) or [])),
            " ".join(str(item) for item in (getattr(task, "non_goals", []) or [])),
            str(intent.title or ""),
            str(intent.objective or ""),
            " ".join(str(item) for item in (intent.acceptance_criteria or [])),
            " ".join(str(item) for item in (intent.non_goals or [])),
            str(objective or ""),
            str(payload),
            str(handoff),
        ]
    )
    lowered = text.lower()
    if any(marker in lowered for marker in ("product edit required", "must edit product", "requires product edit")):
        return False
    if not (_text_has_no_product_edit_marker(text) or "not a product implementation" in lowered or "not a product implementation goal" in lowered):
        return False
    return any(marker in lowered for marker in ("investigat", "audit", "gap report", "implementation plan", "staged plan", "hardening plan"))


def _raw_stage_agent_runtime_test_files(raw: dict[str, Any], *, intent: MissionIntent, task: Task) -> list[str]:
    text = " ".join(
        [
            str(raw.get("id") or ""),
            str(raw.get("title") or ""),
            str(raw.get("objective") or ""),
            " ".join(str(item) for item in (raw.get("test_plan") or [])),
            str(intent.objective or ""),
            str(getattr(task, "description", "") or ""),
        ]
    )
    return _extract_agent_runtime_test_files(text)


def _handoff_is_no_product_edit_proof(
    task: Task,
    payload: dict[str, Any],
    handoff: dict[str, Any],
    *,
    intent: MissionIntent,
    objective: str,
) -> bool:
    target_repo = _canonical_repo(str(handoff.get("target_repo") or _first_repo(payload.get("affected_repos")) or ""))
    if target_repo != "hermes-agent":
        return False
    text = " ".join(
        [
            str(objective or ""),
            str(intent.objective or ""),
            " ".join(str(item) for item in (intent.acceptance_criteria or [])),
            " ".join(str(item) for item in (intent.non_goals or [])),
            str(getattr(task, "description", "") or ""),
            " ".join(str(item) for item in (getattr(task, "acceptance_criteria", []) or [])),
            " ".join(str(item) for item in (getattr(task, "non_goals", []) or [])),
            str(handoff),
        ]
    )
    if not _text_has_no_product_edit_marker(text):
        return False
    lowered = text.lower()
    if not any(marker in lowered for marker in ("proof", "proof-only", "proof only", "smoke", "verify", "verification", "pytest", "tests/agent_runtime/")):
        return False
    return not any(marker in lowered for marker in ("implement code", "patch code", "product edit required"))


def _handoff_agent_runtime_test_files(
    task: Task,
    payload: dict[str, Any],
    handoff: dict[str, Any],
    *,
    intent: MissionIntent,
    objective: str,
) -> list[str]:
    text = " ".join(
        [
            str(objective or ""),
            str(intent.objective or ""),
            " ".join(str(item) for item in (intent.acceptance_criteria or [])),
            " ".join(str(item) for item in (intent.non_goals or [])),
            str(getattr(task, "description", "") or ""),
            " ".join(str(item) for item in (getattr(task, "acceptance_criteria", []) or [])),
            " ".join(str(item) for item in (getattr(task, "non_goals", []) or [])),
            str(handoff),
            str(payload),
        ]
    )
    return _extract_agent_runtime_test_files(text)


def _locked_intent_forbids_product_edits(intent: MissionIntent, task: Task) -> bool:
    non_goal_text = " ".join(str(item) for item in [*(intent.non_goals or []), *(getattr(task, "non_goals", []) or [])])
    if _text_has_no_product_edit_marker(non_goal_text):
        return True
    objective_text = " ".join([str(intent.objective or ""), str(getattr(task, "description", "") or "")]).lower()
    return any(
        marker in objective_text
        for marker in (
            "no product edits",
            "no-product-edit",
            "no_product_edit",
            "without product edits",
            "without editing product code",
            "do not edit product code",
        )
    )


def _text_has_no_product_edit_marker(text: str) -> bool:
    lowered = str(text or "").lower().replace("_", " ").replace("-", " ")
    return any(
        marker in lowered
        for marker in (
            "no product edit",
            "no product edits",
            "no edit",
            "no edits",
            "without product edits",
            "without editing product",
            "do not edit product",
            "do not edit code",
        )
    )


def _extract_agent_runtime_test_files(text: str) -> list[str]:
    matches = re.findall(r"tests[/\\]agent_runtime[/\\]test_[A-Za-z0-9_./\\-]+?\.py", str(text or ""))
    normalized = [match.replace("\\", "/").rstrip(".,;:)]}") for match in matches]
    result: list[str] = []
    for item in normalized:
        if item not in result:
            result.append(item)
    return result[:8]


def _parent_requires_launcher(task: Task, intent: MissionIntent) -> bool:
    if _task_scope_is_hermes_only(task):
        return False
    text = _parent_text(task, intent)
    if _parent_excludes_launcher(text):
        return False
    if _parent_is_no_product_edit_investigation(text):
        return False
    return any(marker in text for marker in ("launcher", "frontend", "front-end", "mission control", "eternialauncher"))


def _parent_excludes_launcher(text: str) -> bool:
    lowered = str(text or "").lower()
    explicit_excludes = (
        "no launcher",
        "no launcher/frontend",
        "no frontend",
        "no front-end",
        "no eternialauncher",
        "do not implement admin ui",
        "do not implement ui",
        "do not touch launcher",
        "do not touch frontend",
        "no launcher/frontend changes",
        "no launcher changes",
        "no frontend changes",
        "backend-only",
        "backend only",
        "patch only eterniabackend",
        "only eterniabackend",
    )
    return any(marker in lowered for marker in explicit_excludes)


def _parent_is_no_product_edit_investigation(text: str) -> bool:
    lowered = str(text or "").lower()
    if any(marker in lowered for marker in ("product edit required", "must edit product", "requires product edit")):
        return False
    if not (_text_has_no_product_edit_marker(lowered) or "not a product implementation" in lowered):
        return False
    return any(marker in lowered for marker in ("investigat", "audit", "gap report", "implementation plan", "staged plan", "hardening plan"))


def _task_scope_is_hermes_only(task: Task) -> bool:
    repos = [str(item).strip() for item in (getattr(task, "affected_repos", []) or []) if str(item).strip()]
    if not repos:
        return False
    canonical = {_canonical_repo(repo) for repo in repos}
    return canonical == {"hermes-agent"}


def _normalize_stages_for_task_scope(task: Task, intent: MissionIntent, stages: list[MissionPlanStage]) -> list[MissionPlanStage]:
    if _task_scope_excludes_launcher(task, intent):
        return _remove_launcher_stages_for_excluded_scope(task, intent, stages)
    if not _task_scope_is_hermes_only(task):
        return stages
    return _remove_launcher_stages_for_excluded_scope(task, intent, stages, fallback_repo="hermes-agent")


def _task_scope_excludes_launcher(task: Task, intent: MissionIntent) -> bool:
    text = _parent_text(task, intent)
    if _parent_excludes_launcher(text):
        return True
    repos = [str(item).strip() for item in (getattr(task, "affected_repos", []) or []) if str(item).strip()]
    if not repos:
        return False
    canonical = {_canonical_repo(repo) for repo in repos}
    return canonical and "EterniaLauncher" not in canonical


def _remove_launcher_stages_for_excluded_scope(task: Task, intent: MissionIntent, stages: list[MissionPlanStage], *, fallback_repo: str | None = None) -> list[MissionPlanStage]:
    kept: list[MissionPlanStage] = []
    removed_ids: set[str] = set()
    fallback_repo = fallback_repo or _fallback_repo_for_scope(task)
    for stage in stages:
        if stage.repo == "EterniaLauncher" and stage.owner == "dev":
            removed_ids.add(stage.id)
            continue
        if stage.owner == "qa" and stage.repo == "EterniaLauncher":
            stage = replace(stage, repo=fallback_repo)
        kept.append(stage)
    normalized: list[MissionPlanStage] = []
    for stage in kept:
        if stage.depends_on:
            stage = replace(stage, depends_on=[dep for dep in stage.depends_on if dep not in removed_ids])
        normalized.append(stage)
    if not normalized:
        objective = str(intent.objective or task.description or task.title).strip()
        normalized.append(
            _make_stage(
                _stage_id_for(fallback_repo, None, objective),
                title="Scoped Implementation",
                objective=objective,
                owner="dev",
                repo=fallback_repo,
                kind="implementation",
                requires_product_edit=True,
                requires_visual_proof=False,
            )
        )
    persona_diagnostic_self_observation = any(
        stage.id == "neko_diagnostic" and stage.owner == "neko_supervisor"
        for stage in normalized
    ) and "diagnostic_persona:neko_supervisor" in {str(flag or "").strip() for flag in (getattr(task, "risk_flags", []) or [])}
    if not persona_diagnostic_self_observation and not any(stage.owner == "qa" for stage in normalized):
        normalized.append(_qa_stage(intent, depends_on=[stage.id for stage in normalized if stage.owner != "qa"], repo=fallback_repo))
    return normalized


def _fallback_repo_for_scope(task: Task) -> str:
    repos = [_canonical_repo(str(item)) for item in (getattr(task, "affected_repos", []) or []) if str(item).strip()]
    for repo in repos:
        if repo in {"EterniaBackend", "hermes-agent"}:
            return repo
    return "hermes-agent" if _task_scope_is_hermes_only(task) else "EterniaBackend"


def _parent_requires_visual(task: Task, intent: MissionIntent) -> bool:
    return bool(getattr(task, "requires_visual_proof", False) or _text_mentions_visual(_parent_text(task, intent)))


def _parent_text(task: Task, intent: MissionIntent) -> str:
    return " ".join(
        [
            str(getattr(task, "title", "") or ""),
            str(getattr(task, "description", "") or ""),
            " ".join(str(item) for item in (getattr(task, "acceptance_criteria", []) or [])),
            str(intent.title or ""),
            str(intent.objective or ""),
            " ".join(str(item) for item in (intent.acceptance_criteria or [])),
            " ".join(str(item) for item in (intent.non_goals or [])),
        ]
    ).lower()


def _text_mentions_visual(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in ("visual", "screenshot", "fullscreen", "mcp"))


def _text_mentions_investigation(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in ("investigate", "investigation", "audit", "analysis", "hardening plan", "staged plan"))


def _canonical_owner(value: str) -> str:
    text = str(value or "").strip()
    mapping = {
        "launcher_dev": "dev",
        "frontend_dev": "dev",
        "backend": "backend_dev",
        "backend-dev": "backend_dev",
        "qa_agent": "qa",
        "harness": "harness",
        "human": "human",
        "neko": "neko_supervisor",
        "alice_supervisor": "neko_supervisor",
    }
    resolved = mapping.get(text, text)
    return resolved if _safe_id(resolved) else "dev"


def _canonical_repo(value: str) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if "backend" in lowered:
        return "EterniaBackend"
    if "launcher" in lowered or "frontend" in lowered or "ui" == lowered:
        return "EterniaLauncher"
    if "hermes" in lowered:
        return "hermes-agent"
    if text in ALLOWED_REPOS:
        return text
    return "none" if not text else "EterniaLauncher"


def _first_repo(value: Any) -> str:
    if isinstance(value, list) and value:
        return str(value[0])
    return str(value or "")


def _stage_status(value: Any, default: StageStatus) -> StageStatus:
    if isinstance(value, StageStatus):
        return value
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return StageStatus(text)
    except ValueError:
        return default


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _safe_id(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text) and len(text) <= 128 and all(ch.isalnum() or ch in "_.-" for ch in text)


def _safe_stage_identifier(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_.-]+", "_", text).strip("_.-")
    return text[:96] or "stage"


def _safe_token(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", str(value or "")).strip("_").lower()


def _exit_code(metadata: dict[str, Any]) -> int:
    try:
        return int(metadata.get("exit_code"))
    except (TypeError, ValueError, OverflowError):
        return 1
