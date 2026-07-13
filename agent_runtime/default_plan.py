from __future__ import annotations

from typing import Mapping

from hermes_time import now

from .blueprints import BlueprintStore, instantiate_blueprint
from .models import MissionIntent, MissionPlan, MissionPlanStage, Task
from .states import StageStatus


DEFAULT_TASK_BLUEPRINT_ID = "neko_two_dev_default"
DEFAULT_TASK_BLUEPRINT_BINDINGS = {
    "lead": "persona:neko_supervisor",
    "backend_builder": "persona:backend_dev",
    "builder": "persona:dev",
}
_DEFAULT_SLOT_ALIASES = {
    "lead": "neko_supervisor",
    "backend_builder": "backend_dev",
    "builder": "dev",
}


def build_default_mission_plan(
    task: Task,
    *,
    blueprint_id: str = DEFAULT_TASK_BLUEPRINT_ID,
    bindings: Mapping[str, str] | None = None,
) -> MissionPlan:
    bp = BlueprintStore().get(blueprint_id)
    default_bindings = DEFAULT_TASK_BLUEPRINT_BINDINGS if blueprint_id == DEFAULT_TASK_BLUEPRINT_ID else {}
    merged_bindings = {**default_bindings, **dict(bindings or {})}
    plan = instantiate_blueprint(bp, goal=task.description or task.title, bindings=merged_bindings)
    _alias_default_slots_to_personas(plan)
    specialize_default_plan_for_task(task, plan)
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
    if _has_plan(task):
        plan = task.mission_plan
        _upgrade_typed_plan_to_graph(task, plan)
        if not plan.current_stage_id and task.current_stage_id and any(stage.id == task.current_stage_id for stage in plan.stages):
            plan.current_stage_id = task.current_stage_id
    else:
        plan = _plan_from_legacy_task_stages(task) or build_default_mission_plan(task, blueprint_id=blueprint_id, bindings=bindings)
        task.mission_plan = plan
    task.current_stage_id = plan.current_stage_id
    return plan


def specialize_default_plan_for_task(task: Task, plan: MissionPlan) -> None:
    """Apply task-aware defaults to the bundled Neko two-Dev graph.

    Explicit blueprint creation uses the same bundled graph but instantiates it
    before the final Task object exists. Keeping this as a public hook prevents
    explicit ``--blueprint neko_two_dev_default`` goals from bypassing no-edit
    proof recipe specialization.
    """

    _specialize_no_edit_visual_proof_stage(task, plan)
    _specialize_default_implementation_stage(task, plan)
    _mark_out_of_scope_repo_lanes(task, plan)
    if _default_task_is_no_edit_cross_stack(task):
        _specialize_default_no_edit_cross_stack_plan(plan)
    _ensure_default_qa_stage_when_required(task, plan)
    _ensure_default_retry_edges(plan)


def _has_plan(task: Task) -> bool:
    plan = getattr(task, "mission_plan", None)
    return bool(plan and plan.enabled and plan.stages)


def _specialize_no_edit_visual_proof_stage(task: Task, plan: MissionPlan) -> None:
    if not bool(getattr(task, "requires_visual_proof", False)):
        return
    if not _task_is_no_edit_visual_proof(task):
        return
    stage = next((item for item in plan.stages if item.owner in {"dev", "launcher_dev"} and item.kind in {"implementation", "proof_only"}), None)
    if stage is None:
        return
    stage.kind = "proof_only"
    stage.requires_product_edit = False
    stage.requires_visual_proof = True
    stage.proof_recipe_id = None
    stage.test_plan = []
    stage.proof_gate = {
        "required": True,
        "minimum_status": "passed",
        "required_proof_types": ["screenshot"],
        "visual_required": True,
    }
    stage.affected_paths = []
    if not stage.acceptance_criteria:
        stage.acceptance_criteria = list(getattr(task, "acceptance_criteria", []) or [])
    note = "Task requires no-product-edit launcher_qa visual proof; command proof targets are out of scope."
    if note not in stage.audit_notes:
        stage.audit_notes.append(note)


def _task_is_no_edit_visual_proof(task: Task) -> bool:
    text = " ".join(
        [
            str(getattr(task, "title", "") or ""),
            str(getattr(task, "description", "") or ""),
            " ".join(str(item) for item in (getattr(task, "acceptance_criteria", []) or [])),
            " ".join(str(item) for item in (getattr(task, "non_goals", []) or [])),
            " ".join(str(item) for item in (getattr(task, "risk_flags", []) or [])),
        ]
    ).lower()
    return any(marker in text for marker in ("screenshot", "visual proof", "fullscreen", "launcher_qa")) and any(
        marker in text
        for marker in (
            "no product edit",
            "no product edits",
            "no-product-edit",
            "no-product-edits",
            "without product edits",
            "without product edit",
            "no product files are edited",
            "do not modify product",
        )
    )


def _alias_default_slots_to_personas(plan: MissionPlan) -> None:
    if plan.blueprint_id != DEFAULT_TASK_BLUEPRINT_ID:
        return
    for stage in plan.stages:
        owner = _DEFAULT_SLOT_ALIASES.get(stage.owner_slot or stage.owner, stage.owner_slot or stage.owner)
        stage.owner = owner
        stage.owner_slot = owner
    plan.slots = {
        "neko_supervisor": {"role": "neko", "required": True, "description": "Mission lead that clarifies scope and routes recovery."},
        "backend_dev": {"role": "backend_dev", "required": True, "description": "Backend implementation specialist."},
        "dev": {"role": "builder", "required": True, "description": "Agent that implements the scoped work."},
    }
    plan.bindings = {"neko_supervisor": "neko_supervisor", "backend_dev": "backend_dev", "dev": "dev"}
    plan.binding_sources = {
        "neko_supervisor": "persona:neko_supervisor",
        "backend_dev": "persona:backend_dev",
        "dev": "persona:dev",
    }


def _align_default_plan_to_task_state(task: Task, plan: MissionPlan) -> None:
    # De-hardwired: dispatch is graph-driven. We no longer pre-pass stages by id
    # (e.g. auto-passing scope/backend to jump to "implement") or hardcode a target
    # stage — the dispatcher runs the blueprint root first and releases dependents
    # as their `depends_on` pass. The only state-derived shortcut kept is the
    # terminal collapse: an already-integrated/approved task has every stage passed.
    state_value = getattr(getattr(task, "state", None), "value", str(getattr(task, "state", "") or ""))
    if state_value in {"qa_approved", "pm_proof_review", "pm_ready_for_integration", "integrating"}:
        for stage in plan.stages:
            stage.status = StageStatus.PASSED
        plan.current_stage_id = None
        return
    if not plan.current_stage_id and plan.stages:
        plan.current_stage_id = plan.stages[0].id


def _specialize_default_implementation_stage(task: Task, plan: MissionPlan) -> None:
    if plan.blueprint_id != DEFAULT_TASK_BLUEPRINT_ID:
        return
    repo = _single_product_repo_scope(task)
    if repo is None:
        return
    if repo == "EterniaLauncher":
        stage = next((item for item in plan.stages if item.id == "backend_implementation"), None)
        if stage is not None:
            _mark_default_stage_out_of_scope(stage, "Task affected_repos is EterniaLauncher only; Backend Dev is out of scope.")
            _retarget_default_edge(plan, source="scope", outcome="ready", old_target="backend_implementation", new_target="implement")
    elif repo == "EterniaBackend":
        stage = next((item for item in plan.stages if item.id == "implement"), None)
        if stage is not None:
            _mark_default_stage_out_of_scope(stage, "Task affected_repos is EterniaBackend only; Launcher Dev is out of scope.")


def _single_product_repo_scope(task: Task) -> str | None:
    repos = {_canonical_product_repo(str(item)) for item in (getattr(task, "affected_repos", []) or []) if str(item or "").strip()}
    repos.discard(None)
    if len(repos) != 1:
        return None
    repo = next(iter(repos))
    return repo if repo in {"EterniaBackend", "EterniaLauncher"} else None


def _pinned_repo_scope(task: Task) -> set[str]:
    repos = {
        repo
        for repo in (_canonical_product_repo(str(item)) for item in (getattr(task, "affected_repos", []) or []))
        if repo
    }
    return repos


def _mark_out_of_scope_repo_lanes(task: Task, plan: MissionPlan) -> None:
    scoped_repos = _pinned_repo_scope(task)
    if not scoped_repos:
        return
    for stage in plan.stages:
        owner = str(getattr(stage, "owner", "") or "").strip()
        if owner not in {"dev", "backend_dev", "qa"}:
            continue
        repo = _canonical_product_repo(str(getattr(stage, "repo", "") or ""))
        if not repo or repo in scoped_repos:
            continue
        _mark_default_stage_out_of_scope(
            stage,
            f"Task repo_scope is {', '.join(sorted(scoped_repos))}; {stage.title} is not_applicable for {repo}.",
        )


def _canonical_product_repo(value: str) -> str | None:
    lowered = str(value or "").strip().lower()
    if "backend" in lowered:
        return "EterniaBackend"
    if "launcher" in lowered or "frontend" in lowered:
        return "EterniaLauncher"
    if "hermes" in lowered:
        return "hermes-agent"
    return None


def _mark_default_stage_out_of_scope(stage: MissionPlanStage, reason: str) -> None:
    stage.status = StageStatus.PASSED
    stage.kind = "proof_only"
    stage.requires_product_edit = False
    stage.requires_visual_proof = False
    stage.blocks_qa_until = False
    stage.proof_gate = {"required": False}
    stage.proof_recipe_id = None
    stage.test_plan = []
    stage.affected_paths = []
    if reason not in stage.audit_notes:
        stage.audit_notes.append(reason)


def _retarget_default_edge(plan: MissionPlan, *, source: str, outcome: str, old_target: str, new_target: str) -> None:
    for edge in plan.edges:
        if (
            str(edge.get("source") or "") == source
            and str(edge.get("outcome") or "") == outcome
            and str(edge.get("target") or "") == old_target
        ):
            edge["target"] = new_target


def _ensure_default_qa_stage_when_required(task: Task, plan: MissionPlan) -> None:
    if plan.blueprint_id != DEFAULT_TASK_BLUEPRINT_ID:
        return
    if any(stage.owner == "qa" or stage.kind == "qa_verdict" for stage in plan.stages):
        return
    if not _default_task_requires_qa(task):
        return
    dependencies = [
        stage.id
        for stage in plan.stages
        if stage.owner in {"dev", "backend_dev"} and stage.kind in {"implementation", "proof_only"}
    ]
    if not dependencies:
        return
    for stage in plan.stages:
        if stage.id in dependencies and stage.status != StageStatus.PASSED:
            stage.blocks_qa_until = True
    plan.slots.setdefault("qa", {"role": "qa", "required": True, "description": "QA verifier for explicitly requested final approval."})
    plan.bindings.setdefault("qa", "qa")
    plan.binding_sources.setdefault("qa", "persona:qa")
    plan.stages.append(
        MissionPlanStage(
            id="qa_release",
            title="QA Release",
            objective="Verify the completed implementation stages and attached proof.",
            owner="qa",
            owner_slot="qa",
            repo=_qa_repo_for_default_plan(task, plan),
            kind="qa_verdict",
            status=StageStatus.READY,
            depends_on=dependencies,
            blocks_qa_until=False,
            proof_gate={
                "required": True,
                "minimum_status": "approved",
                "required_proof_types": ["qa_verdict"],
            },
            created_at=now(),
            updated_at=now(),
        )
    )
    for edge in plan.edges:
        if str(edge.get("source") or "") in dependencies and str(edge.get("outcome") or "") in {"ready", "passed"}:
            edge["target"] = "qa_release"
    plan.edges.extend(
        [
            {"source": "qa_release", "outcome": "passed", "target": "done"},
            {"source": "qa_release", "outcome": "failed", "target": "scope"},
            {"source": "qa_release", "outcome": "needs_fixes", "target": _default_qa_rework_target(plan, dependencies)},
            {"source": "qa_release", "outcome": "blocked", "target": "intervention"},
        ]
    )
    plan.limits["strict_depends_on_dispatch"] = int(plan.limits.get("strict_depends_on_dispatch", 1) or 1)


def _default_task_requires_qa(task: Task) -> bool:
    text = " ".join(
        [
            str(getattr(task, "title", "") or ""),
            str(getattr(task, "description", "") or ""),
            " ".join(str(item) for item in (getattr(task, "acceptance_criteria", []) or [])),
            " ".join(str(item) for item in (getattr(task, "non_goals", []) or [])),
            " ".join(str(item) for item in (getattr(task, "risk_flags", []) or [])),
        ]
    ).lower()
    if any(marker in text for marker in ("no qa", "without qa", "default_no_qa")):
        return False
    return any(
        marker in text
        for marker in (
            "qa must approve",
            "qa approval required",
            "final qa approval",
            "qa verdict required",
            "requires qa approval",
            "require qa approval",
            "qa must verify",
        )
    )


def _qa_repo_for_default_plan(task: Task, plan: MissionPlan) -> str:
    scoped = _single_product_repo_scope(task)
    if scoped:
        return scoped
    for stage in reversed(plan.stages):
        if stage.owner in {"dev", "backend_dev"} and stage.status != StageStatus.PASSED and stage.repo in {"EterniaBackend", "EterniaLauncher"}:
            return stage.repo
    for stage in reversed(plan.stages):
        if stage.owner in {"dev", "backend_dev"} and stage.repo in {"EterniaBackend", "EterniaLauncher"}:
            return stage.repo
    return "hermes-agent"


def _default_qa_rework_target(plan: MissionPlan, dependencies: list[str]) -> str:
    for stage_id in reversed(dependencies):
        stage = next((item for item in plan.stages if item.id == stage_id), None)
        if stage is not None and stage.status != StageStatus.PASSED:
            return stage.id
    return dependencies[-1] if dependencies else "scope"


def _default_task_is_no_edit_cross_stack(task: Task) -> bool:
    text = " ".join(
        [
            str(getattr(task, "title", "") or ""),
            str(getattr(task, "description", "") or ""),
            " ".join(str(item) for item in (getattr(task, "acceptance_criteria", []) or [])),
            " ".join(str(item) for item in (getattr(task, "non_goals", []) or [])),
            " ".join(str(item) for item in (getattr(task, "risk_flags", []) or [])),
        ]
    ).lower()
    repos = " ".join(str(item).lower() for item in (getattr(task, "affected_repos", []) or []))
    no_edit = any(marker in text for marker in ("no-edit", "no edit", "no-product-edit", "no product edit", "without product edits", "do not modify product"))
    mentions_backend = "backend" in text or "eterniabackend" in text or "backend" in repos or "eterniabackend" in repos
    mentions_launcher = "launcher" in text or "eternialauncher" in text or "launcher" in repos or "eternialauncher" in repos
    return no_edit and mentions_backend and mentions_launcher


def _mark_default_noop_dependencies_passed(
    stages_by_id: dict[str, MissionPlanStage],
    stage: MissionPlanStage,
) -> None:
    for dep_id in list(stage.depends_on or []):
        dep = stages_by_id.get(dep_id)
        if dep is None:
            continue
        _mark_default_noop_dependencies_passed(stages_by_id, dep)
        if _is_default_noop_dependency(dep):
            dep.status = StageStatus.PASSED


def _is_default_noop_dependency(stage: MissionPlanStage) -> bool:
    if stage.id != "backend_implementation":
        return False
    gate = getattr(stage, "proof_gate", {}) or {}
    return not (
        gate.get("required")
        or gate.get("required_proof_types")
        or gate.get("proof_recipe_id")
        or gate.get("commands")
        or getattr(stage, "proof_recipe_id", None)
        or getattr(stage, "requires_product_edit", False)
        or getattr(stage, "requires_visual_proof", False)
    )


def _specialize_default_no_edit_cross_stack_plan(plan: MissionPlan) -> None:
    if plan.blueprint_id != DEFAULT_TASK_BLUEPRINT_ID:
        return
    backend = next((stage for stage in plan.stages if stage.id == "backend_implementation"), None)
    launcher = next((stage for stage in plan.stages if stage.id == "implement"), None)
    if backend is not None:
        backend.title = "Backend No-Edit Contract Proof"
        backend.objective = "Collect the Harness-owned backend_contract_smoke proof without inspecting, editing, or patching backend product files."
        backend.kind = "proof_only"
        backend.proof_recipe_id = "backend_contract_smoke"
        backend.requires_product_edit = False
        backend.proof_gate = {
            "required": True,
            "minimum_status": "passed",
            "required_proof_types": ["test_run"],
            "proof_recipe_id": "backend_contract_smoke",
            "observed_lane_required": True,
            "observed_lane_requirement": "agent_tool_trace/run.tool.finished must populate observed_proof_ids before hand_off",
        }
        backend.test_plan = ["proof_recipe:backend_contract_smoke"]
    if launcher is not None:
        launcher.title = "Launcher No-Edit Contract Proof"
        launcher.objective = "Collect the Harness-owned launcher_contract_smoke proof after backend proof is attached, without editing Launcher product files."
        launcher.kind = "proof_only"
        launcher.proof_recipe_id = "launcher_contract_smoke"
        launcher.requires_product_edit = False
        launcher.proof_gate = {
            "required": True,
            "minimum_status": "passed",
            "required_proof_types": ["test_run"],
            "proof_recipe_id": "launcher_contract_smoke",
            "observed_lane_required": True,
            "observed_lane_requirement": "agent_tool_trace/run.tool.finished must populate observed_proof_ids before hand_off",
        }
        launcher.test_plan = ["proof_recipe:launcher_contract_smoke"]


def _ensure_default_retry_edges(plan: MissionPlan) -> None:
    if plan.blueprint_id != DEFAULT_TASK_BLUEPRINT_ID:
        return
    existing = {(str(edge.get("source") or ""), str(edge.get("outcome") or "")) for edge in plan.edges or []}
    for outcome in ("failed", "needs_fixes", "missing_input"):
        if ("implement", outcome) not in existing:
            plan.edges.append({"source": "implement", "outcome": outcome, "target": "implement"})


def _upgrade_typed_plan_to_graph(task: Task, plan: MissionPlan) -> None:
    if getattr(plan, "blueprint_id", None):
        return
    plan.blueprint_id = DEFAULT_TASK_BLUEPRINT_ID
    plan.blueprint_version = plan.blueprint_version or 1
    for stage in plan.stages:
        slot = stage.owner_slot or stage.owner or "dev"
        stage.owner_slot = slot
        plan.slots.setdefault(slot, {"role": slot, "required": True, "description": f"Migrated typed slot {slot}."})
        plan.bindings.setdefault(slot, slot)
        plan.binding_sources.setdefault(slot, f"persona:{slot}")
    _ensure_migrated_qa_stage(task, plan)
    if not plan.edges:
        plan.edges = _linear_edges_for_migrated_stages(plan.stages)
    plan.limits = plan.limits or {"max_attempts_per_stage": 3, "max_total_stages": 16}
    plan.on_unhandled = plan.on_unhandled or "intervention"


def _ensure_migrated_qa_stage(task: Task, plan: MissionPlan) -> None:
    if any((stage.owner_slot or stage.owner) == "qa" or stage.kind == "qa_verdict" for stage in plan.stages):
        return
    if not plan.stages:
        return
    plan.slots.setdefault("qa", {"role": "qa", "required": True, "description": "Migrated verifier slot qa."})
    plan.bindings.setdefault("qa", "qa")
    plan.binding_sources.setdefault("qa", "persona:qa")
    plan.stages.append(
        MissionPlanStage(
            id="qa_release",
            title="QA Release",
            objective="Verify the migrated mission stages and attached proof.",
            owner="qa",
            owner_slot="qa",
            repo=_repo_for_legacy_stage(task, plan.stages[-1]),
            kind="qa_verdict",
            status=StageStatus.READY,
            depends_on=[stage.id for stage in plan.stages if stage.blocks_qa_until],
            blocks_qa_until=False,
            created_at=now(),
            updated_at=now(),
        )
    )


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
                affected_paths=list(legacy.affected_paths or []),
                acceptance_criteria=list(legacy.acceptance_criteria or []),
                test_plan=list(legacy.test_plan or []),
                audit_notes=list(legacy.audit_notes or []),
                corrections=list(legacy.corrections or []),
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
    qa_identity = ("qa" in id_title or "verify" in id_title or "verification" in id_title or "verdict" in id_title)
    if qa_identity:
        return "qa"
    if "backend" in id_title or "eterniabackend" in id_title or "backend" in path_plan or "eterniabackend" in path_plan:
        return "backend_dev"
    if "qa" in str(getattr(stage, "objective", "") or "").lower() or "verify" in str(getattr(stage, "objective", "") or "").lower():
        return "qa"
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
