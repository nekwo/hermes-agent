from __future__ import annotations

from dataclasses import dataclass, field

from .incidents import CRITICAL_INCIDENT_KINDS
from .mission_plan import blocking_stages_ready_for_qa, has_typed_plan, task_stage_records
from .models import Incident, MissionPlanStage, Proof, Task
from .proof_rules import ProofType


@dataclass(slots=True)
class GateResult:
    allowed: bool
    missing: list[str]
    warnings: list[str] = field(default_factory=list)


def _proofs_of(proofs: list[Proof], *types: ProofType) -> list[Proof]:
    wanted = set(types)
    return [p for p in proofs if p.type in wanted]


def _safe(proof: Proof) -> bool:
    return proof.redaction_status == "safe"


def _exit_code(proof: Proof) -> int:
    raw = proof.metadata.get("exit_code")
    if raw is None or isinstance(raw, bool):
        return 1
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError):
        return 1


def _visual_proofs(proofs: list[Proof]) -> list[Proof]:
    return [p for p in _proofs_of(proofs, ProofType.SCREENSHOT, ProofType.VIDEO) if _safe(p) and p.path_or_value]


def _stage_proofs(stage: MissionPlanStage, proofs: list[Proof]) -> list[Proof]:
    stage_id = str(getattr(stage, "id", "") or "").strip()
    if not stage_id:
        return proofs
    attached_ids = {str(proof_id) for proof_id in (getattr(stage, "proof_ids", None) or []) if str(proof_id)}
    scoped = [proof for proof in proofs if not proof.stage_id or proof.stage_id == stage_id or proof.id in attached_ids]
    return scoped


def _proof_meets_status(proof: Proof, minimum_status: str) -> bool:
    if not _safe(proof):
        return False
    minimum = str(minimum_status or "passed").strip().lower()
    metadata = getattr(proof, "metadata", {}) or {}
    status = str(metadata.get("status") or metadata.get("verdict") or "").strip().lower()
    if proof.type == ProofType.TEST_RUN:
        if "exit_code" in metadata:
            return _exit_code(proof) == 0
        return status in {"passed", "approved", "safe"}
    if minimum in {"passed", "approved"}:
        if status:
            return status in {"passed", "approved", "safe"}
        return bool(proof.path_or_value)
    if minimum == "safe":
        return True
    return False


def stage_proof_satisfied(stage: MissionPlanStage, proofs: list[Proof]) -> GateResult:
    gate = dict(getattr(stage, "proof_gate", {}) or {})
    required = bool(gate.get("required", False))
    required_types = [str(item).strip() for item in gate.get("required_proof_types", []) or [] if str(item).strip()]
    proof_recipe_id = str(gate.get("proof_recipe_id") or getattr(stage, "proof_recipe_id", "") or "").strip()
    commands = [str(item).strip() for item in gate.get("commands", []) or [] if str(item).strip()]
    if proof_recipe_id or commands:
        required = True
        if not required_types:
            required_types = [ProofType.TEST_RUN.value]
    if getattr(stage, "requires_visual_proof", False) and ProofType.SCREENSHOT.value not in required_types and ProofType.VIDEO.value not in required_types:
        required_types.append(ProofType.SCREENSHOT.value)
    if not required and not required_types:
        return GateResult(True, [])

    minimum_status = str(gate.get("minimum_status") or "passed").strip().lower()
    missing: list[str] = []
    scoped = _stage_proofs(stage, proofs)
    if not required_types:
        if not any(_proof_meets_status(proof, minimum_status) for proof in scoped):
            missing.append(f"missing proof with status {minimum_status}")
        return GateResult(not missing, missing)
    for proof_type in required_types:
        try:
            normalized = ProofType(proof_type)
        except ValueError:
            missing.append(f"unknown required proof type {proof_type}")
            continue
        if normalized in {ProofType.SCREENSHOT, ProofType.VIDEO}:
            if not _visual_proofs([proof for proof in scoped if proof.type in {ProofType.SCREENSHOT, ProofType.VIDEO}]):
                missing.append("missing screenshot or video proof")
            continue
        candidates = [proof for proof in scoped if proof.type == normalized]
        if not candidates:
            missing.append(f"missing {normalized.value} proof")
            continue
        if not any(_proof_meets_status(proof, minimum_status) for proof in candidates):
            missing.append(f"missing {minimum_status} {normalized.value} proof")
    return GateResult(not missing, sorted(set(missing)))


def task_requires_visual(task: Task) -> bool:
    return bool(task.requires_visual_proof or any(stage.requires_visual_proof for stage in task_stage_records(task)))


def task_delivery_proof_satisfied(task: Task, proofs: list[Proof]) -> GateResult:
    missing=[]
    has_change = bool([p for p in _proofs_of(proofs, ProofType.COMMIT, ProofType.DIFF, ProofType.DIFF_STAT) if _safe(p)])
    if not has_change and not (task.waiver and task.waiver.get("gate") in {"dev_change_proof", "no_code"}):
        missing.append("missing commit or diff proof")
    test_gate = stage_proof_satisfied(
        _synthetic_gate_stage("delivery", required_proof_types=[ProofType.TEST_RUN.value]),
        proofs,
    )
    if not test_gate.allowed and not (task.waiver and task.waiver.get("gate") == "tests"):
        missing.append("missing passed test proof")
    return GateResult(not missing, missing)


def task_verdict_proof_satisfied(task: Task, proofs: list[Proof]) -> GateResult:
    missing=[]
    if has_typed_plan(task):
        typed_ready, typed_missing = blocking_stages_ready_for_qa(task, proof_store=_ListProofStore(proofs))
        missing.extend(typed_missing)
    verdict_gate = stage_proof_satisfied(
        _synthetic_gate_stage("verdict", required_proof_types=[ProofType.QA_VERDICT.value]),
        proofs,
    )
    if not verdict_gate.allowed:
        missing.append("missing approved QA verdict")
    test_gate = stage_proof_satisfied(
        _synthetic_gate_stage("verdict_tests", required_proof_types=[ProofType.TEST_RUN.value]),
        proofs,
    )
    if not test_gate.allowed and not (task.waiver and task.waiver.get("gate") == "tests"):
        missing.append("missing passed test proof")
    if task_requires_visual(task):
        visual_gate = stage_proof_satisfied(
            _synthetic_gate_stage("verdict_visual", required_proof_types=[ProofType.SCREENSHOT.value]),
            proofs,
        )
        if not visual_gate.allowed:
            missing.append("missing screenshot or video proof")
    return GateResult(not missing, missing)


class _ListProofStore:
    def __init__(self, proofs: list[Proof]):
        self._proofs = {proof.id: proof for proof in proofs}

    def get(self, proof_id: str) -> Proof:
        return self._proofs[proof_id]


def task_release_proof_satisfied(task: Task, proofs: list[Proof], incidents: list[Incident]) -> GateResult:
    missing=[]
    warnings=[]
    qa=task_verdict_proof_satisfied(task, proofs)
    dev=task_delivery_proof_satisfied(task, proofs)
    missing.extend(dev.missing)
    missing.extend(qa.missing)
    open_critical=[i for i in incidents if i.closed_at is None and i.kind in CRITICAL_INCIDENT_KINDS]
    if open_critical:
        missing.append("open critical incidents")
    return GateResult(not missing, sorted(set(missing)), warnings)


def _synthetic_gate_stage(stage_id: str, *, required_proof_types: list[str]) -> MissionPlanStage:
    return MissionPlanStage(
        id="",
        title=stage_id,
        objective=stage_id,
        owner="harness",
        repo="hermes-agent",
        kind="proof_gate",
        proof_gate={
            "required": True,
            "minimum_status": "passed",
            "required_proof_types": required_proof_types,
        },
    )
