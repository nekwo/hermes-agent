from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class HarnessActionType(StrEnum):
    RUN_PM = "run_pm"
    RUN_DEV = "run_dev"
    RUN_QA = "run_qa"
    RUN_NEKO_SUPERVISOR = "run_neko_supervisor"
    COMPLETE_TASK = "complete_task"
    NOOP = "noop"
    RECOVER_STALE_RUN = "recover_stale_run"


@dataclass(slots=True)
class HarnessAction:
    type: HarnessActionType
    task_id: str | None = None
    run_id: str | None = None
    reason: str = ""


@dataclass(slots=True)
class HarnessActionResult:
    action: HarnessAction
    ok: bool
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
