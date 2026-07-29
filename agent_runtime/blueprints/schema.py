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
_ALLOWED_ROLES = {"lead", "neko", "builder", "verifier", "reviewer", "dev", "backend_dev", "qa", "harness", "human", "specialist"}
_ALLOWED_BINDING_PREFIXES = ("persona:", "profile:")


class StageOutcome(StrEnum):
    READY = "ready"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    REWORK = "needs_fixes"
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
class BlueprintAgentTopologyEdge:
    source: str
    target: str
    kind: str = "steers"


@dataclass(frozen=True, slots=True)
class BlueprintAgentTopology:
    root: str | None = None
    edges: list[BlueprintAgentTopologyEdge] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ProofGate:
    required: bool = False
    minimum_status: str = "passed"
    required_proof_types: list[str] = field(default_factory=list)
    proof_recipe_id: str | None = None
    commands: list[str] = field(default_factory=list)


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
    proof_gate: ProofGate = field(default_factory=ProofGate)
    output_type: str | None = None
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
    agent_topology: BlueprintAgentTopology = field(default_factory=BlueprintAgentTopology)
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
    agent_topology = _agent_topology_from_dict(raw.get("agent_topology"))
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
        agent_topology=agent_topology,
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
        errors.extend(_validate_proof_gate(stage))
    if not bp.stages and bp.edges:
        errors.append("edges require at least one stage")
    for edge in bp.edges:
        if edge.source not in stage_ids:
            errors.append(f"edge source {edge.source!r} is not a known stage")
        if edge.target not in stage_ids and edge.target not in _RESERVED_TARGETS:
            errors.append(f"edge target {edge.target!r} is not a known stage or terminal target")
    errors.extend(_validate_agent_topology(bp, slot_ids))
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
    output_type = _safe_output_type(raw.get("output_type"))
    proof_gate_raw = raw.get("proof_gate") if isinstance(raw.get("proof_gate"), dict) else {}
    proof_gate = _proof_gate_from_dict(proof_gate_raw)
    if output_type and not proof_gate_raw:
        proof_gate = _proof_gate_for_output_type(output_type)
    proof_recipe_id = str(raw.get("proof_recipe_id") or proof_gate.proof_recipe_id or "").strip() or None
    if proof_recipe_id and proof_gate.proof_recipe_id != proof_recipe_id:
        proof_gate = ProofGate(
            required=proof_gate.required,
            minimum_status=proof_gate.minimum_status,
            required_proof_types=list(proof_gate.required_proof_types),
            proof_recipe_id=proof_recipe_id,
            commands=list(proof_gate.commands),
        )
    return BlueprintStage(
        id=str(raw.get("id") or "").strip(),
        title=str(raw.get("title") or raw.get("id") or "").strip(),
        objective=str(raw.get("objective") or raw.get("title") or "").strip(),
        owner_slot=str(raw.get("owner_slot") or raw.get("owner") or "").strip(),
        kind=str(raw.get("kind") or "implementation").strip(),
        repo=str(raw.get("repo") or "none").strip(),
        depends_on=[str(item).strip() for item in raw.get("depends_on", []) or [] if str(item).strip()],
        blocks_qa_until=bool(raw.get("blocks_qa_until", True)),
        proof_recipe_id=proof_recipe_id,
        proof_gate=proof_gate,
        output_type=output_type or _infer_output_type(proof_gate=proof_gate, kind=str(raw.get("kind") or "implementation").strip()),
        requires_product_edit=bool(raw.get("requires_product_edit", False)),
        requires_visual_proof=bool(raw.get("requires_visual_proof", False)),
    )


def _proof_gate_from_dict(raw: dict[str, Any]) -> ProofGate:
    return ProofGate(
        required=bool(raw.get("required", False)),
        minimum_status=str(raw.get("minimum_status") or "passed").strip().lower(),
        required_proof_types=[str(item).strip() for item in raw.get("required_proof_types", []) or [] if str(item).strip()],
        proof_recipe_id=str(raw.get("proof_recipe_id") or raw.get("recipe_id") or "").strip() or None,
        commands=[str(item).strip() for item in raw.get("commands", []) or [] if str(item).strip()],
    )


def _safe_output_type(value: Any) -> str | None:
    text = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    return " ".join(text.split()) or None


def _proof_gate_for_output_type(output_type: str) -> ProofGate:
    normalized = _safe_output_type(output_type) or ""
    if normalized in {"code feature", "code", "implementation", "software change"}:
        return ProofGate(required=True, minimum_status="passed", required_proof_types=["test_run"])
    if normalized in {"design document", "design doc", "plan", "document"}:
        return ProofGate(required=True, minimum_status="passed", required_proof_types=["artifact"])
    if normalized in {"qa verdict", "verification verdict", "verdict"}:
        return ProofGate(required=True, minimum_status="approved", required_proof_types=["qa_verdict"])
    return ProofGate(required=False)


def _infer_output_type(*, proof_gate: ProofGate, kind: str) -> str:
    required = {str(item) for item in proof_gate.required_proof_types}
    normalized_kind = _safe_output_type(kind) or ""
    if "qa_verdict" in required or normalized_kind in {"qa verdict", "qa_verdict"}:
        return "qa verdict"
    if required & {"test_run", "diff", "diff_stat", "commit"} or normalized_kind in {"implementation", "proof only"}:
        return "code feature"
    if required & {"artifact", "text", "url"} or normalized_kind in {"context", "investigation", "scope"}:
        return "design document"
    return "design document"


def _validate_proof_gate(stage: BlueprintStage) -> list[str]:
    errors: list[str] = []
    gate = stage.proof_gate
    if gate.minimum_status not in {"passed", "approved", "safe"}:
        errors.append(f"stage {stage.id} proof_gate.minimum_status {gate.minimum_status!r} is not allowed")
    for proof_type in gate.required_proof_types:
        try:
            from agent_runtime.models import ProofType

            ProofType(proof_type)
        except ValueError:
            errors.append(f"stage {stage.id} proof_gate.required_proof_types contains unknown type {proof_type!r}")
    return errors


def _edge_from_dict(raw: dict[str, Any]) -> BlueprintEdge:
    return BlueprintEdge(
        source=str(raw.get("source") or "").strip(),
        outcome=StageOutcome(str(raw.get("outcome") or "passed").strip()),
        target=str(raw.get("target") or "").strip(),
    )


def _agent_topology_from_dict(raw: Any) -> BlueprintAgentTopology:
    if not isinstance(raw, dict):
        return BlueprintAgentTopology()
    edges = [
        BlueprintAgentTopologyEdge(
            source=str(item.get("source") or item.get("source_slot") or "").strip(),
            target=str(item.get("target") or item.get("target_slot") or "").strip(),
            kind=str(item.get("kind") or "steers").strip() or "steers",
        )
        for item in raw.get("edges", []) or []
        if isinstance(item, dict)
    ]
    return BlueprintAgentTopology(
        root=str(raw.get("root") or raw.get("root_slot") or "").strip() or None,
        edges=edges,
    )


def _validate_agent_topology(bp: Blueprint, slot_ids: list[str]) -> list[str]:
    errors: list[str] = []
    slot_set = set(slot_ids)
    topology = bp.agent_topology
    if topology.root and topology.root not in slot_set:
        errors.append(f"agent_topology root {topology.root!r} is not a declared slot")
    adjacency: dict[str, list[str]] = {}
    for edge in topology.edges:
        if edge.kind != "steers":
            errors.append(f"agent_topology edge kind {edge.kind!r} is not allowed")
        if edge.source not in slot_set:
            errors.append(f"agent_topology source {edge.source!r} is not a declared slot")
        if edge.target not in slot_set:
            errors.append(f"agent_topology target {edge.target!r} is not a declared slot")
        adjacency.setdefault(edge.source, []).append(edge.target)
    errors.extend(_topology_cycle_errors(adjacency))
    return errors


def _topology_cycle_errors(adjacency: dict[str, list[str]]) -> list[str]:
    errors: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, trail: list[str]) -> None:
        if node in visiting:
            errors.append("agent_topology cycle: " + " -> ".join(trail + [node]))
            return
        if node in visited:
            return
        visiting.add(node)
        for target in adjacency.get(node, []):
            visit(target, trail + [node])
        visiting.remove(node)
        visited.add(node)

    for node in adjacency:
        visit(node, [])
    return errors


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
