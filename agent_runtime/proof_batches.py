from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from hermes_time import now
from utils import atomic_json_write

from . import paths
from .models import Event
from .serde import from_jsonable, to_jsonable

PROOF_BATCH_STATUSES = frozenset({"pending", "running", "passed", "failed", "superseded", "blocked"})


@dataclass(slots=True)
class ProofBatch:
    proof_batch_id: str
    task_id: str
    mission_stage_id: str | None
    role_envelope_id: str | None
    recipe_id: str | None
    status: str
    proof_ids: list[str] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    schema_version: int = 1


class ProofBatchStore:
    def __init__(self, event_log=None):
        from .events import EventLog

        self.event_log = event_log or EventLog()

    def get(self, task_id: str, proof_batch_id: str) -> ProofBatch:
        raw = json.loads(paths.proof_batch_path(task_id, proof_batch_id).read_text(encoding="utf-8"))
        return from_jsonable(ProofBatch, raw)

    def save(self, batch: ProofBatch, *, event_type: str | None = None, run_id: str | None = None, persona_id: str | None = None) -> ProofBatch:
        batch.updated_at = now()
        if batch.created_at is None:
            batch.created_at = batch.updated_at
        path = paths.proof_batch_path(batch.task_id, batch.proof_batch_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, to_jsonable(batch), indent=2, sort_keys=True)
        if event_type:
            self.event_log.append(Event(now(), event_type, batch.task_id, run_id, persona_id, proof_batch_summary(batch)))
        return batch

    def record_batch(
        self,
        *,
        task_id: str,
        mission_stage_id: str | None,
        role_envelope_id: str | None,
        recipe_id: str | None,
        proof_ids: list[str],
        status: str,
        run_id: str | None = None,
        persona_id: str | None = None,
    ) -> ProofBatch:
        status = status if status in PROOF_BATCH_STATUSES else "blocked"
        supersedes: list[str] = []
        active = self.active_batch(task_id, mission_stage_id=mission_stage_id, recipe_id=recipe_id)
        if active is not None:
            supersedes.append(active.proof_batch_id)
            active.status = "superseded"
            self.save(active, event_type="proof_batch.superseded", run_id=run_id, persona_id=persona_id)
        batch = ProofBatch(
            proof_batch_id=f"proofbatch_{uuid.uuid4().hex[:12]}",
            task_id=task_id,
            mission_stage_id=mission_stage_id,
            role_envelope_id=role_envelope_id,
            recipe_id=recipe_id,
            status=status,
            proof_ids=list(proof_ids or []),
            supersedes=supersedes,
            created_at=now(),
            updated_at=now(),
        )
        return self.save(batch, event_type="proof_batch.recorded", run_id=run_id, persona_id=persona_id)

    def list_for_task(self, task_id: str) -> list[ProofBatch]:
        root = paths.proof_batches_task_dir(task_id)
        if not root.exists():
            return []
        batches: list[ProofBatch] = []
        for path in sorted(root.glob("*.json")):
            try:
                batches.append(from_jsonable(ProofBatch, json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return sorted(batches, key=lambda item: item.updated_at or item.created_at or now())

    def list_all(self) -> list[ProofBatch]:
        root = paths.proof_batches_dir()
        if not root.exists():
            return []
        batches: list[ProofBatch] = []
        for path in sorted(root.glob("*/*.json")):
            try:
                batches.append(from_jsonable(ProofBatch, json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return sorted(batches, key=lambda item: item.updated_at or item.created_at or now())

    def active_batch(self, task_id: str, *, mission_stage_id: str | None, recipe_id: str | None) -> ProofBatch | None:
        candidates = [
            batch
            for batch in self.list_for_task(task_id)
            if (batch.mission_stage_id or None) == (mission_stage_id or None)
            and (batch.recipe_id or None) == (recipe_id or None)
            and batch.status != "superseded"
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.updated_at or item.created_at or now())


def proof_batch_summary(batch: ProofBatch) -> dict[str, Any]:
    return {
        "proof_batch_id": batch.proof_batch_id,
        "task_id": batch.task_id,
        "mission_stage_id": batch.mission_stage_id,
        "role_envelope_id": batch.role_envelope_id,
        "recipe_id": batch.recipe_id,
        "status": batch.status,
        "proof_ids": list(batch.proof_ids),
        "supersedes": list(batch.supersedes),
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
    }
