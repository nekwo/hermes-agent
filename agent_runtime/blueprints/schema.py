from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - JSON fallback keeps tests/tooling usable without PyYAML
    yaml = None

_SAFE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
_RESERVED_TARGETS = {"done", "intervention"}
_ALLOWED_ROLES = {"lead", "builder", "verifier", "dev", "backend_dev", "qa", "harness", "human", "specialist"}
_ALLOWED_BINDING_PREFIXES = ("persona:", "profile:")


class StageOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    NEEDS_FIXES = "needs_fixes"
    MISSING_INPUT = "missing_input"


@dataclass(frozen=True, slots=True)
class BlueprintSlot:
    id: str
    role: str
    required: bool = True
    description: str = ""


@dataclass(frozen=True, slots=True)
class BlueprintEdge:
    source: str
    outcome: StageOutcome
    target: str


@dataclass(frozen=True, slots=True)
class BlueprintLimits:
    max_attempts_per_stage: int = 2
    max_total_stages: int = 20


@dataclass(frozen=True, slots=True)
class BlueprintStage:
    id: str
    title: str
    objective: str
    owner_slot: str
    kind: str = "implementation"
    repo: str = "none"
    depends_on: list[str] = field(default_factory=list)
    blocks_qa_until: bool = True
    proof_recipe_id: str | None = None
    requires_product_edit: bool = False
    requires_visual_proof: bool = False


@dataclass(frozen=True, slots=True)
class Blueprint:
    id: str
    version: int
    title: str
    description: str = ""
    slots: list[BlueprintSlot] = field(default_factory=list)
    stages: list[BlueprintStage] = field(default_factory=list)
    edges: list[BlueprintEdge] = field(default_factory=list)
    limits: BlueprintLimits = field(default_factory=BlueprintLimits)
    on_unhandled: str = "intervention"


def load_blueprint(path: str | Path) -> Blueprint:
    p = Path(path)
    raw_text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in {".yaml", ".yml"}:
        if yaml is None:
            raise ValueError("PyYAML is required to load blueprint YAML files")
        raw = yaml.safe_load(raw_text) or {}
    else:
        raw = json.loads(raw_text)
    return blueprint_from_dict(raw)


def blueprint_from_dict(raw: dict[str, Any]) -> Blueprint:
    if not isinstance(raw, dict):
        raise ValueError("blueprint must be an object")
    slots = [_slot_from_dict(item) for item in raw.get("slots", []) if isinstance(item, dict)]
    stages = [_stage_from_dict(item) for item in raw.get("stages", []) if isinstance(item, dict)]
    edges = [_edge_from_dict(item) for item in raw.get("edges", []) if isinstance(item, dict)]
    limits_raw = raw.get("limits") if isinstance(raw.get("limits"), dict) else {}
    limits = BlueprintLimits(
        max_attempts_per_stage=int(limits_raw.get("max_attempts_per_stage", 2)),
        max_total_stages=int(limits_raw.get("max_total_stages", 20)),
    )
    bp = Blueprint(
        id=str(raw.get("id") or "").strip(),
        version=int(raw.get("version") or 1),
        title=str(raw.get("title") or raw.get("id") or "").strip(),
        description=str(raw.get("description") or "").strip(),
        slots=slots,
        stages=stages,
        edges=edges,
        limits=limits,
        on_unhandled=str(raw.get("on_unhandled") or "intervention").strip(),
    )
    errors = validate_blueprint(bp)
    if errors:
        raise ValueError("invalid blueprint: " + "; ".join(errors))
    return bp


def validate_blueprint(bp: Blueprint) -> list[str]:
    errors: list[str] = []
    if not _SAFE_ID.fullmatch(bp.id):
        errors.append("blueprint id must be a redaction-safe token")
    if int(bp.version or 0) < 1:
        errors.append("blueprint version must be >= 1")
    if bp.limits.max_attempts_per_stage < 1:
        errors.append("limits.max_attempts_per_stage must be >= 1")
    if bp.limits.max_total_stages < 1:
        errors.append("limits.max_total_stages must be >= 1")
    slot_ids = [slot.id for slot in bp.slots]
    if len(slot_ids) != len(set(slot_ids)):
        errors.append("slot ids must be unique")
    for slot in bp.slots:
        if not _SAFE_ID.fullmatch(slot.id):
            errors.append(f"slot {slot.id!r} id must be redaction-safe")
        if slot.role not in _ALLOWED_ROLES:
            errors.append(f"slot {slot.id} role {slot.role!r} is not allowed")
    stage_ids = [stage.id for stage in bp.stages]
    if len(stage_ids) != len(set(stage_ids)):
        errors.append("stage ids must be unique")
    for stage in bp.stages:
        if not _SAFE_ID.fullmatch(stage.id):
            errors.append(f"stage {stage.id!r} id must be redaction-safe")
        if stage.owner_slot not in slot_ids:
            errors.append(f"stage {stage.id} owner_slot {stage.owner_slot!r} is not declared")
        for dep in stage.depends_on:
            if dep not in stage_ids:
                errors.append(f"stage {stage.id} depends on unknown stage {dep!r}")
    for edge in bp.edges:
        if edge.source not in stage_ids:
            errors.append(f"edge source {edge.source!r} is not a known stage")
        if edge.target not in stage_ids and edge.target not in _RESERVED_TARGETS:
            errors.append(f"edge target {edge.target!r} is not a known stage or terminal target")
    errors.extend(_dependency_cycle_errors(bp))
    if bp.on_unhandled not in stage_ids and bp.on_unhandled not in _RESERVED_TARGETS:
        errors.append("on_unhandled must be a stage id, done, or intervention")
    return errors


def validate_bindings(bp: Blueprint, bindings: dict[str, str]) -> list[str]:
    errors: list[str] = []
    slot_ids = {slot.id for slot in bp.slots}
    for slot in bp.slots:
        value = str(bindings.get(slot.id) or "").strip()
        if slot.required and not value:
            errors.append(f"missing binding for required slot {slot.id}")
        if value and not value.startswith(_ALLOWED_BINDING_PREFIXES):
            errors.append(f"binding for slot {slot.id} must start with persona: or profile:")
    for slot_id in bindings:
        if slot_id not in slot_ids:
            errors.append(f"binding provided for unknown slot {slot_id}")
    return errors


def _slot_from_dict(raw: dict[str, Any]) -> BlueprintSlot:
    return BlueprintSlot(
        id=str(raw.get("id") or "").strip(),
        role=str(raw.get("role") or "specialist").strip(),
        required=bool(raw.get("required", True)),
        description=str(raw.get("description") or "").strip(),
    )


def _stage_from_dict(raw: dict[str, Any]) -> BlueprintStage:
    return BlueprintStage(
        id=str(raw.get("id") or "").strip(),
        title=str(raw.get("title") or raw.get("id") or "").strip(),
        objective=str(raw.get("objective") or raw.get("title") or "").strip(),
        owner_slot=str(raw.get("owner_slot") or raw.get("owner") or "").strip(),
        kind=str(raw.get("kind") or "implementation").strip(),
        repo=str(raw.get("repo") or "none").strip(),
        depends_on=[str(item).strip() for item in raw.get("depends_on", []) or [] if str(item).strip()],
        blocks_qa_until=bool(raw.get("blocks_qa_until", True)),
        proof_recipe_id=str(raw.get("proof_recipe_id") or "").strip() or None,
        requires_product_edit=bool(raw.get("requires_product_edit", False)),
        requires_visual_proof=bool(raw.get("requires_visual_proof", False)),
    )


def _edge_from_dict(raw: dict[str, Any]) -> BlueprintEdge:
    return BlueprintEdge(
        source=str(raw.get("source") or "").strip(),
        outcome=StageOutcome(str(raw.get("outcome") or "passed").strip()),
        target=str(raw.get("target") or "").strip(),
    )


def _dependency_cycle_errors(bp: Blueprint) -> list[str]:
    deps = {stage.id: set(stage.depends_on) for stage in bp.stages}
    errors: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, trail: list[str]) -> None:
        if node in visiting:
            errors.append("dependency cycle: " + " -> ".join(trail + [node]))
            return
        if node in visited:
            return
        visiting.add(node)
        for dep in deps.get(node, set()):
            visit(dep, trail + [node])
        visiting.remove(node)
        visited.add(node)

    for node in deps:
        visit(node, [])
    return errors
