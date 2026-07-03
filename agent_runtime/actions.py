from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class HarnessActionType(StrEnum):
    RUN_SLOT = "run_slot"
    COMPLETE_TASK = "complete_task"
    NOOP = "noop"
    RECOVER_STALE_RUN = "recover_stale_run"


@dataclass(slots=True)
class HarnessAction:
    type: HarnessActionType
    task_id: str | None = None
    run_id: str | None = None
    reason: str = ""
    slot_id: str | None = None
    stage_id: str | None = None
    parent_node_id: str | None = None
    child_events_offset: int | None = None


@dataclass(slots=True)
class HarnessActionResult:
    action: HarnessAction
    ok: bool
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
