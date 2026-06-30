from __future__ import annotations

import uuid

from hermes_time import now

from .models import Proof, Task
from .proof_gates import task_verdict_proof_satisfied
from .proof_rules import ProofType
from .store import ProofStore


def record_qa_verdict(task: Task, *, verdict: str, proof_ids: list[str], findings: list[dict] | None = None, store: ProofStore | None = None) -> Proof:
    proof = Proof(
        id=f"proof_qa_{uuid.uuid4().hex[:8]}",
        task_id=task.id,
        stage_id=task.current_stage_id,
        type=ProofType.QA_VERDICT,
        title=f"QA verdict: {verdict}",
        path_or_value=verdict,
        created_by="qa",
        created_at=now(),
        metadata={"verdict": verdict, "proof_ids": proof_ids, "findings": findings or []},
        redaction_status="safe",
    )
    if store:
        store.attach(proof)
    return proof


def qa_verdict_allows_approval(task: Task, proofs: list[Proof]) -> bool:
    return task_verdict_proof_satisfied(task, proofs).allowed
