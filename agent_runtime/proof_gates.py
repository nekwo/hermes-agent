from __future__ import annotations

from dataclasses import dataclass, field

from .incidents import CRITICAL_INCIDENT_KINDS
from .mission_plan import blocking_stages_ready_for_qa, has_typed_plan
from .models import Incident, Proof, Task
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


def _passed_tests(proofs: list[Proof]) -> list[Proof]:
    return [p for p in _proofs_of(proofs, ProofType.TEST_RUN) if _safe(p) and _exit_code(p) == 0]


def _visual_proofs(proofs: list[Proof]) -> list[Proof]:
    return [p for p in _proofs_of(proofs, ProofType.SCREENSHOT, ProofType.VIDEO) if _safe(p) and p.path_or_value]


def task_requires_visual(task: Task) -> bool:
    return bool(task.requires_visual_proof or any(stage.requires_visual_proof for stage in task.stages))


def implementation_proof_satisfied(task: Task, proofs: list[Proof]) -> GateResult:
    missing=[]
    has_change = bool([p for p in _proofs_of(proofs, ProofType.COMMIT, ProofType.DIFF, ProofType.DIFF_STAT) if _safe(p)])
    if not has_change and not (task.waiver and task.waiver.get("gate") in {"dev_change_proof", "no_code"}):
        missing.append("missing commit or diff proof")
    if not _passed_tests(proofs) and not (task.waiver and task.waiver.get("gate") == "tests"):
        missing.append("missing passed test proof")
    return GateResult(not missing, missing)


def verification_proof_satisfied(task: Task, proofs: list[Proof]) -> GateResult:
    missing=[]
    if has_typed_plan(task):
        typed_ready, typed_missing = blocking_stages_ready_for_qa(task, proof_store=_ListProofStore(proofs))
        missing.extend(typed_missing)
    verdicts=[p for p in _proofs_of(proofs, ProofType.QA_VERDICT) if _safe(p) and p.metadata.get("verdict") == "approved"]
    if not verdicts:
        missing.append("missing approved QA verdict")
    if not _passed_tests(proofs) and not (task.waiver and task.waiver.get("gate") == "tests"):
        missing.append("missing passed test proof")
    if task_requires_visual(task) and not _visual_proofs(proofs):
        missing.append("missing screenshot or video proof")
    return GateResult(not missing, missing)


class _ListProofStore:
    def __init__(self, proofs: list[Proof]):
        self._proofs = {proof.id: proof for proof in proofs}

    def get(self, proof_id: str) -> Proof:
        return self._proofs[proof_id]


def integration_proof_satisfied(task: Task, proofs: list[Proof], incidents: list[Incident]) -> GateResult:
    missing=[]
    warnings=[]
    qa=verification_proof_satisfied(task, proofs)
    dev=implementation_proof_satisfied(task, proofs)
    missing.extend(dev.missing)
    missing.extend(qa.missing)
    open_critical=[i for i in incidents if i.closed_at is None and i.kind in CRITICAL_INCIDENT_KINDS]
    if open_critical:
        missing.append("open critical incidents")
    return GateResult(not missing, sorted(set(missing)), warnings)
