from __future__ import annotations

from dataclasses import asdict

from agent_runtime.models import MissionIntent, MissionPlan, MissionPlanStage
from agent_runtime.states import StageStatus

from .schema import Blueprint, validate_bindings


def instantiate_blueprint(bp: Blueprint, *, goal: str, bindings: dict[str, str], resolver=None) -> MissionPlan:
    errors = validate_bindings(bp, bindings)
    if errors:
        raise ValueError("invalid blueprint bindings: " + "; ".join(errors))
    if resolver is not None:
        slot_roles = {slot.id: slot.role for slot in bp.slots}
        resolved_bindings = {
            slot_id: resolver.resolve(value, slot_role=slot_roles.get(slot_id, "dev"))
            for slot_id, value in bindings.items()
        }
    else:
        resolved_bindings = {slot_id: _binding_to_persona_id(value) for slot_id, value in bindings.items()}
    stages = [
        MissionPlanStage(
            id=stage.id,
            title=stage.title,
            objective=stage.objective,
            owner=resolved_bindings.get(stage.owner_slot, stage.owner_slot),
            owner_slot=stage.owner_slot,
            repo=stage.repo,
            kind=stage.kind,
            status=StageStatus.READY,
            proof_recipe_id=stage.proof_recipe_id,
            proof_gate={
                "required": stage.proof_gate.required,
                "minimum_status": stage.proof_gate.minimum_status,
                "required_proof_types": list(stage.proof_gate.required_proof_types),
                "proof_recipe_id": stage.proof_gate.proof_recipe_id,
                "commands": list(stage.proof_gate.commands),
            },
            output_type=stage.output_type,
            requires_product_edit=stage.requires_product_edit,
            requires_visual_proof=stage.requires_visual_proof,
            depends_on=list(stage.depends_on),
            blocks_qa_until=stage.blocks_qa_until,
        )
        for stage in bp.stages
    ]
    limits = asdict(bp.limits)
    limits["strict_depends_on_dispatch"] = 1
    return MissionPlan(
        enabled=True,
        mission_intent=MissionIntent(title=bp.title, objective=goal, acceptance_criteria=[], locked=True),
        stages=stages,
        current_stage_id=stages[0].id if stages else None,
        blueprint_id=bp.id,
        blueprint_version=bp.version,
        slots={slot.id: {"role": slot.role, "required": slot.required, "description": slot.description} for slot in bp.slots},
        bindings=resolved_bindings,
        binding_sources=dict(bindings),
        edges=[{"source": edge.source, "outcome": edge.outcome.value, "target": edge.target} for edge in bp.edges],
        agent_topology={
            "root": bp.agent_topology.root,
            "edges": [
                {"source": edge.source, "target": edge.target, "kind": edge.kind}
                for edge in bp.agent_topology.edges
            ],
        },
        limits=limits,
        stage_attempts={},
        on_unhandled=bp.on_unhandled,
    )


def _binding_to_persona_id(value: str) -> str:
    text = str(value).strip()
    if text.startswith("persona:"):
        return text.split(":", 1)[1]
    if text.startswith("profile:"):
        return text.split(":", 1)[1]
    raise ValueError("binding must start with persona: or profile:")
