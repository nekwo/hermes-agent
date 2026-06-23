from __future__ import annotations

from typing import Mapping

from hermes_time import now

from .blueprints import BlueprintStore, instantiate_blueprint
from .mission_plan import has_typed_plan
from .models import MissionIntent, MissionPlan, MissionPlanStage, Task
from .states import StageStatus


DEFAULT_TASK_BLUEPRINT_ID = "neko_dev_qa_basic"
DEFAULT_TASK_BLUEPRINT_BINDINGS = {
    "lead": "persona:neko_supervisor",
    "builder": "persona:dev",
    "verifier": "persona:qa",
}
_DEFAULT_SLOT_ALIASES = {
    "lead": "neko_supervisor",
    "builder": "dev",
    "verifier": "qa",
}


def build_default_mission_plan(
    task: Task,
    *,
    blueprint_id: str = DEFAULT_TASK_BLUEPRINT_ID,
    bindings: Mapping[str, str] | None = None,
) -> MissionPlan:
    bp = BlueprintStore().get(blueprint_id)
    merged_bindings = {**DEFAULT_TASK_BLUEPRINT_BINDINGS, **dict(bindings or {})}
    plan = instantiate_blueprint(bp, goal=task.description or task.title, bindings=merged_bindings)
    _alias_default_slots_to_personas(plan)
    _specialize_default_implementation_stage(task, plan)
    _ensure_default_retry_edges(plan)
    _align_default_plan_to_task_state(task, plan)
    if plan.mission_intent is not None:
        plan.mission_intent.title = task.title
        plan.mission_intent.acceptance_criteria = list(task.acceptance_criteria or [])
        plan.mission_intent.non_goals = list(task.non_goals or [])
        plan.mission_intent.source_task_id = task.id
    return plan


def ensure_default_mission_plan(
    task: Task,
    *,
    blueprint_id: str = DEFAULT_TASK_BLUEPRINT_ID,
    bindings: Mapping[str, str] | None = None,
) -> MissionPlan:
    if has_typed_plan(task):
        plan = task.mission_plan
        if task.current_stage_id and any(stage.id == task.current_stage_id for stage in plan.stages):
            plan.current_stage_id = task.current_stage_id
    else:
        plan = _plan_from_legacy_task_stages(task) or build_default_mission_plan(task, blueprint_id=blueprint_id, bindings=bindings)
        task.mission_plan = plan
    task.current_stage_id = plan.current_stage_id
    return plan


def _alias_default_slots_to_personas(plan: MissionPlan) -> None:
    if plan.blueprint_id != DEFAULT_TASK_BLUEPRINT_ID:
        return
    for stage in plan.stages:
        owner = _DEFAULT_SLOT_ALIASES.get(stage.owner_slot or stage.owner, stage.owner_slot or stage.owner)
        stage.owner = owner
        stage.owner_slot = owner
    plan.slots = {
        "neko_supervisor": {"role": "neko", "required": True, "description": "Mission lead that clarifies scope and routes recovery."},
        "dev": {"role": "builder", "required": True, "description": "Agent that implements the scoped work."},
        "qa": {"role": "verifier", "required": True, "description": "Agent that verifies implementation and proof."},
    }
    plan.bindings = {"neko_supervisor": "neko_supervisor", "dev": "dev", "qa": "qa"}
    plan.binding_sources = {
        "neko_supervisor": "persona:neko_supervisor",
        "dev": "persona:dev",
        "qa": "persona:qa",
    }


def _align_default_plan_to_task_state(task: Task, plan: MissionPlan) -> None:
    state_value = getattr(getattr(task, "state", None), "value", str(getattr(task, "state", "") or ""))
    by_id = {stage.id: stage for stage in plan.stages}
    if state_value in {
        "pm_ready_for_dev",
        "dev_audit",
        "dev_stage_planning",
        "dev_test_design",
        "dev_implementing",
        "qa_needs_fixes",
    } and "implement" in by_id:
        if "scope" in by_id:
            by_id["scope"].status = StageStatus.PASSED
        by_id["implement"].status = StageStatus.IMPLEMENTING
        plan.current_stage_id = "implement"
    elif state_value in {"dev_ready_for_qa", "qa_testing"} and "verify" in by_id:
        if "scope" in by_id:
            by_id["scope"].status = StageStatus.PASSED
        if "implement" in by_id:
            by_id["implement"].status = StageStatus.READY_FOR_QA
        plan.current_stage_id = "verify"
    elif state_value in {"qa_approved", "pm_proof_review", "pm_ready_for_integration", "integrating"}:
        for stage in plan.stages:
            stage.status = StageStatus.PASSED
        plan.current_stage_id = None


def _specialize_default_implementation_stage(task: Task, plan: MissionPlan) -> None:
    repos = {_repo_for_text(str(item)) for item in (getattr(task, "affected_repos", None) or [])}
    flags = {str(item).strip().lower() for item in (getattr(task, "risk_flags", None) or [])}
    implement = next((stage for stage in plan.stages if stage.id == "implement"), None)
    if implement is None:
        return
    if "EterniaBackend" in repos and ("EterniaLauncher" not in repos or "backend_contract_first" in flags):
        implement.owner = "backend_dev"
        implement.owner_slot = "backend_dev"
        implement.repo = "EterniaBackend"
        plan.slots.setdefault("backend_dev", {"role": "backend_dev", "required": True, "description": "Backend implementation specialist."})
        plan.bindings.setdefault("backend_dev", "backend_dev")
        plan.binding_sources.setdefault("backend_dev", "persona:backend_dev")


def _ensure_default_retry_edges(plan: MissionPlan) -> None:
    if plan.blueprint_id != DEFAULT_TASK_BLUEPRINT_ID:
        return
    existing = {(str(edge.get("source") or ""), str(edge.get("outcome") or "")) for edge in plan.edges or []}
    for outcome in ("failed", "needs_fixes", "missing_input"):
        if ("implement", outcome) not in existing:
            plan.edges.append({"source": "implement", "outcome": outcome, "target": "implement"})


def _plan_from_legacy_task_stages(task: Task) -> MissionPlan | None:
    if not getattr(task, "stages", None):
        return None
    stages: list[MissionPlanStage] = []
    slots: dict[str, dict[str, object]] = {}
    for legacy in task.stages:
        owner = _owner_for_legacy_stage(legacy)
        slots.setdefault(owner, {"role": owner, "required": True, "description": f"Migrated legacy owner {owner}."})
        stages.append(
            MissionPlanStage(
                id=legacy.id,
                title=legacy.title,
                objective=legacy.objective,
                owner=owner,
                owner_slot=owner,
                repo=_repo_for_legacy_stage(task, legacy),
                kind="qa_verdict" if owner == "qa" else "implementation",
                status=legacy.status,
                requires_visual_proof=bool(legacy.requires_visual_proof),
                blocks_qa_until=owner != "qa",
                created_at=legacy.created_at or now(),
                updated_at=legacy.updated_at or now(),
            )
        )
    if stages and not any(stage.owner == "qa" for stage in stages):
        slots.setdefault("qa", {"role": "qa", "required": True, "description": "Migrated legacy owner qa."})
        stages.append(
            MissionPlanStage(
                id="verify",
                title="QA verification",
                objective="Verify the migrated legacy mission stages and attached proof.",
                owner="qa",
                owner_slot="qa",
                repo="hermes-agent",
                kind="qa_verdict",
                status=StageStatus.READY,
                blocks_qa_until=False,
                created_at=now(),
                updated_at=now(),
            )
        )
    for index, stage in enumerate(stages):
        if index > 0 and stage.owner != "qa" and not stage.depends_on:
            stage.depends_on = [stages[index - 1].id]
    current_id = task.current_stage_id if task.current_stage_id and any(stage.id == task.current_stage_id for stage in stages) else stages[0].id
    return MissionPlan(
        enabled=True,
        mission_intent=MissionIntent(
            title=task.title,
            objective=task.description or task.title,
            acceptance_criteria=list(task.acceptance_criteria or []),
            non_goals=list(task.non_goals or []),
            source_task_id=task.id,
            locked=True,
        ),
        stages=stages,
        current_stage_id=current_id,
        blueprint_id=DEFAULT_TASK_BLUEPRINT_ID,
        blueprint_version=1,
        slots=slots,
        bindings={slot: slot for slot in slots},
        binding_sources={slot: f"persona:{slot}" for slot in slots},
        edges=_linear_edges_for_migrated_stages(stages),
        limits={"max_attempts_per_stage": 3, "max_total_stages": 16},
        on_unhandled="intervention",
    )


def _linear_edges_for_migrated_stages(stages: list[MissionPlanStage]) -> list[dict[str, str]]:
    edges: list[dict[str, str]] = []
    for index, stage in enumerate(stages):
        target = stages[index + 1].id if index + 1 < len(stages) else "done"
        edges.append({"source": stage.id, "outcome": "ready", "target": target})
        edges.append({"source": stage.id, "outcome": "passed", "target": target})
        edges.append({"source": stage.id, "outcome": "needs_fixes", "target": stage.id})
        edges.append({"source": stage.id, "outcome": "failed", "target": stage.id})
        edges.append({"source": stage.id, "outcome": "missing_input", "target": stage.id})
        edges.append({"source": stage.id, "outcome": "blocked", "target": "intervention"})
    return edges


def _owner_for_legacy_stage(stage) -> str:
    id_title = " ".join([str(getattr(stage, "id", "") or ""), str(getattr(stage, "title", "") or "")]).lower()
    path_plan = " ".join(
        [
            " ".join(str(item) for item in (getattr(stage, "affected_paths", None) or [])),
            " ".join(str(item) for item in (getattr(stage, "test_plan", None) or [])),
        ]
    ).lower()
    text = " ".join([id_title, str(getattr(stage, "objective", "") or "").lower(), path_plan])
    if "qa" in text or "verify" in text:
        return "qa"
    if "backend" in id_title or "eterniabackend" in id_title or "backend" in path_plan or "eterniabackend" in path_plan:
        return "backend_dev"
    if "neko" in text or "scope" in text:
        return "neko_supervisor"
    return "dev"


def _repo_for_text(value: str) -> str:
    lowered = str(value or "").lower()
    if "backend" in lowered:
        return "EterniaBackend"
    if "launcher" in lowered:
        return "EterniaLauncher"
    if "hermes" in lowered:
        return "hermes-agent"
    return lowered


def _repo_for_legacy_stage(task: Task, stage) -> str:
    text = " ".join(
        [
            str(getattr(stage, "id", "") or ""),
            str(getattr(stage, "title", "") or ""),
            str(getattr(stage, "objective", "") or ""),
            " ".join(str(item) for item in (getattr(stage, "affected_paths", None) or [])),
            " ".join(str(item) for item in (getattr(task, "affected_repos", None) or [])),
        ]
    ).lower()
    if "backend" in text or "eterniabackend" in text:
        return "EterniaBackend"
    if "launcher" in text or "eternialauncher" in text:
        return "EterniaLauncher"
    return "hermes-agent"
