from __future__ import annotations

import re
from typing import Any

from .decision_schema import AgentDecision, DecisionType
from .models import Task, TaskStage
from .stage_intent import no_product_edit_recipe_id, stage_requires_product_edit

_DEFAULT_FINAL_GATE_COMMANDS: dict[str, tuple[str, ...]] = {
    "EterniaBackend": (".EterniaBackendVirtualEnv/Scripts/python.exe manage.py check",),
    "EterniaLauncher": ("flutter analyze",),
    "hermes-agent": ("python -m pytest tests/agent_runtime -q -o addopts=\"\"",),
}

_GOAL_COMMAND_PREFIXES = ("python", "pytest", "flutter", "dart", "npm", "pnpm", ".eterniabackendvirtualenv")


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
    repo = stage_repo_for_gate(task, stage)
    goal_named = goal_named_gate_commands(task, repo)
    if goal_named:
        return goal_named
    return list(_DEFAULT_FINAL_GATE_COMMANDS.get(repo, ()))[:3]


def stage_repo_for_gate(task: Task, stage: TaskStage | None) -> str:
    typed_stage = _typed_plan_stage(task, stage)
    repo = str(getattr(typed_stage, "repo", "") or getattr(stage, "repo", "") or "").strip()
    override = default_blueprint_placeholder_repo_override(task, repo)
    if override:
        return override
    if not repo and len(getattr(task, "affected_repos", []) or []) == 1:
        repo = str((task.affected_repos or [""])[0]).strip()
    return repo


def default_blueprint_placeholder_repo_override(task: Task, stage_repo: str | None) -> str | None:
    """Task-resolved single-repo scope beats a default-blueprint placeholder repo.

    The bundled ``neko_two_dev_default`` graph hardcodes stage repos
    (backend_implementation=EterniaBackend, implement=EterniaLauncher)
    regardless of the goal's actual scope. When Neko has resolved the goal to
    exactly one repo and it contradicts the placeholder, proof commands and
    the gate workdir must follow the goal's repo — observed live 2026-07-03
    (task_49f8ee3b): a hermes-agent goal's authoritative gate ran
    ``flutter analyze`` in EterniaLauncher because the implement placeholder
    won. Explicit graph blueprints keep their per-stage repos untouched.
    """

    stage_repo = str(stage_repo or "").strip()
    if not stage_repo:
        return None
    plan = getattr(task, "mission_plan", None)
    if str(getattr(plan, "blueprint_id", "") or "") != "neko_two_dev_default":
        return None
    task_repos = [str(item).strip() for item in (getattr(task, "affected_repos", []) or []) if str(item).strip()]
    if len(task_repos) == 1 and task_repos[0] != stage_repo:
        task_repo = task_repos[0]
        if task_repo == "hermes-agent":
            return task_repo
        plan_repos = {
            str(getattr(stage, "repo", "") or "").strip()
            for stage in (getattr(plan, "stages", None) or [])
            if str(getattr(stage, "repo", "") or "").strip()
        }
        task_and_stage_are_product_repos = (
            task_repo in {"EterniaBackend", "EterniaLauncher"}
            and stage_repo in {"EterniaBackend", "EterniaLauncher"}
        )
        if task_and_stage_are_product_repos and {task_repo, stage_repo}.issubset(plan_repos):
            return None
        return task_repo
    return None


def goal_named_gate_commands(task: Task, repo: str | None) -> list[str]:
    """Goal-named exact proof commands applicable to the stage's repo.

    The skill/docs rule is that a focused proof command literally named by the
    goal outranks generic proof recipes at the authoritative gate. Commands
    whose repo hint contradicts the stage repo are excluded so a Launcher
    stage never gate-runs a harness pytest and vice versa.
    """

    repo = str(repo or "").strip()
    result: list[str] = []
    for command in goal_named_proof_commands(task):
        hint = _command_repo_hint(command)
        if hint is not None and repo and hint != repo:
            continue
        result.append(command)
    return result[:3]


def goal_named_proof_commands(task: Task) -> list[str]:
    """Exact runnable proof commands literally named by the goal text.

    Conservative extraction: backtick-quoted spans and standalone/bulleted
    lines that start with a known runner. Shell plumbing and secret-shaped
    text are rejected outright.
    """

    texts = [
        str(getattr(task, "title", "") or ""),
        str(getattr(task, "description", "") or ""),
        *[str(item) for item in (getattr(task, "acceptance_criteria", []) or [])],
        *[str(item) for item in (getattr(task, "operator_notes", []) or [])],
    ]
    found: list[str] = []
    for text in texts:
        for command in _extract_command_candidates(text):
            if command not in found:
                found.append(command)
    return found[:3]


def _extract_command_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for match in re.findall(r"`([^`\n]{4,200})`", text):
        candidates.append(match)
    for line in text.splitlines():
        candidates.append(line.strip().lstrip("-*").strip())
    result: list[str] = []
    for candidate in candidates:
        command = " ".join(str(candidate or "").split())
        if not command or len(command) > 200:
            continue
        if not command.lower().startswith(_GOAL_COMMAND_PREFIXES):
            continue
        if not _looks_like_command(command):
            continue
        if re.search(r"[<>|;&$]", command):
            continue
        if re.search(r"(?i)(secret|token|password|credential)", command):
            continue
        result.append(command)
    return result


def _command_repo_hint(command: str) -> str | None:
    lowered = command.lower()
    if "manage.py" in lowered or ".eterniabackendvirtualenv" in lowered or "eternia-backend" in lowered:
        return "EterniaBackend"
    if lowered.startswith(("flutter", "dart", "npm", "pnpm")) or " lib/" in lowered or "integration_test" in lowered:
        return "EterniaLauncher"
    if "agent_runtime" in lowered or "hermes" in lowered:
        return "hermes-agent"
    return None


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
