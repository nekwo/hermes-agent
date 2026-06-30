from __future__ import annotations

from typing import Any

from .decision_schema import AgentDecision, DecisionType
from .models import Task, TaskStage
from .stage_intent import no_product_edit_recipe_id, stage_requires_product_edit

_DEFAULT_FINAL_GATE_COMMANDS: dict[str, tuple[str, ...]] = {
    "EterniaBackend": (".EterniaBackendVirtualEnv/Scripts/python.exe manage.py check",),
    "EterniaLauncher": ("flutter analyze",),
    "hermes-agent": ("python -m pytest tests/agent_runtime -q -o addopts=\"\"",),
}


def final_gate_required(task: Task, stage: TaskStage | None, delivery_packet: dict[str, Any] | None = None) -> bool:
    if stage is None:
        return False
    if not _stage_requires_product_edit(task, stage):
        return False
    return bool(final_gate_commands(task, stage))


def build_final_gate_decision(task: Task, stage: TaskStage | None, *, delivery_packet: dict[str, Any] | None = None) -> AgentDecision | None:
    if stage is None:
        return None
    commands = final_gate_commands(task, stage)
    if not commands:
        return None
    return AgentDecision(
        type=DecisionType.REQUEST_TEST_RUN,
        summary="Run automatic final gate after Dev delivery.",
        rationale="Normal worker flow accepts Dev delivery first, then Harness collects deterministic final proof.",
        payload={
            "stage_id": stage.id,
            "commands": commands,
            "proof_intent": "auto_final_gate_after_delivery",
        },
    )


def final_gate_commands(task: Task, stage: TaskStage | None) -> list[str]:
    if stage is None:
        return []
    if not _stage_requires_product_edit(task, stage):
        return []
    if no_product_edit_recipe_id(stage.id):
        return []
    commands = [_clean_command(item) for item in getattr(stage, "test_plan", []) or [] if _looks_like_command(item)]
    if commands:
        return commands[:3]
    typed_stage = _typed_plan_stage(task, stage)
    repo = str(getattr(typed_stage, "repo", "") or getattr(stage, "repo", "") or "").strip()
    if not repo and len(getattr(task, "affected_repos", []) or []) == 1:
        repo = str((task.affected_repos or [""])[0]).strip()
    return list(_DEFAULT_FINAL_GATE_COMMANDS.get(repo, ()))[:3]


def _stage_requires_product_edit(task: Task, stage: TaskStage) -> bool:
    typed_stage = _typed_plan_stage(task, stage)
    if bool(getattr(typed_stage, "requires_product_edit", False)):
        return True
    return stage_requires_product_edit(task, stage)


def _typed_plan_stage(task: Task, stage: TaskStage | None):
    if stage is None:
        return None
    plan = getattr(task, "mission_plan", None)
    stage_id = str(getattr(stage, "id", "") or "").strip()
    if not stage_id or plan is None:
        return None
    return next((item for item in getattr(plan, "stages", []) or [] if str(getattr(item, "id", "") or "") == stage_id), None)


def _looks_like_command(value: object) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    if any(marker in text for marker in ("--version", " doctor", " where ", " which ")):
        return False
    return any(marker in text for marker in ("pytest", "flutter ", "dart ", "python ", "manage.py", "npm ", "pnpm "))


def _clean_command(value: object) -> str:
    text = str(value or "").strip()
    if "\n" in text and "<<" in text:
        return "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
    return " ".join(text.split())
