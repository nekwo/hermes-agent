from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from hermes_time import now
from utils import atomic_json_write

from agent_runtime import paths
from agent_runtime.models import Task
from agent_runtime.serde import from_jsonable, to_jsonable


@dataclass(slots=True)
class BlueprintRunRecord:
    id: str
    task_id: str
    blueprint_id: str
    blueprint_version: int
    bindings: dict[str, str]
    binding_sources: dict[str, str]
    per_stage_outcomes: dict[str, str]
    started_at: datetime
    ended_at: datetime
    result: str
    schema_version: int = 1


class BlueprintRunStore:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root is not None else paths.store_root() / "blueprint_runs"

    def write(self, record: BlueprintRunRecord) -> BlueprintRunRecord:
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_json_write(self._path(record.id), to_jsonable(record), indent=2, sort_keys=True)
        return record

    def get(self, record_id: str) -> BlueprintRunRecord:
        raw = json.loads(self._path(record_id).read_text(encoding="utf-8"))
        return from_jsonable(BlueprintRunRecord, raw)

    def list_all(self) -> list[BlueprintRunRecord]:
        if not self.root.exists():
            return []
        records: list[BlueprintRunRecord] = []
        for path in self.root.glob("*.json"):
            try:
                records.append(from_jsonable(BlueprintRunRecord, json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return sorted(records, key=lambda record: record.started_at)

    def record_task_terminal(self, task: Task, *, result: str, ended_at: datetime | None = None) -> BlueprintRunRecord | None:
        plan = getattr(task, "mission_plan", None)
        if not plan or not plan.blueprint_id:
            return None
        record = BlueprintRunRecord(
            id=f"bprun_{task.id}",
            task_id=task.id,
            blueprint_id=str(plan.blueprint_id),
            blueprint_version=int(plan.blueprint_version or 0),
            bindings=dict(plan.bindings),
            binding_sources=dict(plan.binding_sources),
            per_stage_outcomes={stage.id: stage.status.value for stage in plan.stages},
            started_at=task.created_at,
            ended_at=ended_at or now(),
            result=str(result or "unknown"),
        )
        return self.write(record)

    def _path(self, record_id: str) -> Path:
        safe_id = "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in str(record_id or "").strip())
        return self.root / f"{safe_id.strip('._') or 'record'}.json"


def blueprint_run_summary(record: BlueprintRunRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "task_id": record.task_id,
        "blueprint_id": record.blueprint_id,
        "blueprint_version": record.blueprint_version,
        "bindings": dict(record.bindings),
        "binding_sources": dict(record.binding_sources),
        "per_stage_outcomes": dict(record.per_stage_outcomes),
        "started_at": record.started_at,
        "ended_at": record.ended_at,
        "result": record.result,
    }
