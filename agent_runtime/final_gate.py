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

_GOAL_COMMAND_PREFIXES = ("python", "pytest", "flutter", "dart", "npm", "pnpm", "echo", ".eterniabackendvirtualenv")


def final_gate_required(
    task: Task,
    stage: TaskStage | None,
    delivery_packet: dict[str, Any] | None = None,
    handoff_packet: dict[str, Any] | None = None,
) -> bool:
    if stage is None:
        return False
    if not _stage_requires_product_edit(task, stage):
        return False
    return bool(final_gate_commands(task, stage, delivery_packet=delivery_packet, handoff_packet=handoff_packet))


def build_final_gate_decision(
    task: Task,
    stage: TaskStage | None,
    *,
    delivery_packet: dict[str, Any] | None = None,
    handoff_packet: dict[str, Any] | None = None,
) -> AgentDecision | None:
    if stage is None:
        return None
    commands = final_gate_commands(task, stage, delivery_packet=delivery_packet, handoff_packet=handoff_packet)
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


def final_gate_commands(
    task: Task,
    stage: TaskStage | None,
    *,
    delivery_packet: dict[str, Any] | None = None,
    handoff_packet: dict[str, Any] | None = None,
) -> list[str]:
    if stage is None:
        return []
    if not _stage_requires_product_edit(task, stage):
        return []
    if no_product_edit_recipe_id(stage.id):
        return []
    repo = stage_repo_for_gate(task, stage)
    forbidden = packet_forbidden_gate_commands(handoff_packet, delivery_packet)
    packet_named = packet_named_gate_commands(
        task,
        stage,
        repo,
        delivery_packet=delivery_packet,
        handoff_packet=handoff_packet,
    )
    if packet_named:
        return filter_forbidden_gate_commands(packet_named, forbidden)[:3]
    goal_named = goal_named_gate_commands(task, repo)
    if goal_named and goal_demands_exact_proof(task):
        return filter_forbidden_gate_commands(goal_named, forbidden)[:3]
    commands = [_clean_command(item) for item in getattr(stage, "test_plan", []) or [] if _looks_like_command(item)]
    if commands:
        return filter_forbidden_gate_commands(commands, forbidden)[:3]
    if goal_named:
        return filter_forbidden_gate_commands(goal_named, forbidden)[:3]
    return filter_forbidden_gate_commands(list(_DEFAULT_FINAL_GATE_COMMANDS.get(repo, ())), forbidden)[:3]


def packet_named_gate_commands(
    task: Task,
    stage: TaskStage | None,
    repo: str | None,
    *,
    delivery_packet: dict[str, Any] | None = None,
    handoff_packet: dict[str, Any] | None = None,
) -> list[str]:
    """Exact proof commands declared by structured handoff/delivery packets.

    Live Neko handoffs can carry exact proof expectations in
    ``handoff_packet.proof_gate.commands`` even when the task prose is phrased
    as "exact proof command: echo ..." rather than a backtick command. Those
    packet commands are as authoritative as a goal-named exact command.
    """

    repo = str(repo or "").strip()
    result: list[str] = []
    for packet in (handoff_packet, delivery_packet):
        body = _packet_body(packet)
        if not body:
            continue
        packet_repo = str(body.get("target_repo") or body.get("next_repo") or body.get("final_repo") or "").strip()
        if packet_repo and repo and packet_repo != repo:
            continue
        proof_gate = body.get("proof_gate") if isinstance(body.get("proof_gate"), dict) else {}
        for command in _packet_command_values(proof_gate):
            hint = _command_repo_hint(command)
            if hint is not None and repo and hint != repo:
                continue
            if command not in result:
                result.append(command)
    return result[:3]


def packet_forbidden_gate_commands(*packets: dict[str, Any] | None) -> list[str]:
    forbidden: list[str] = []
    for packet in packets:
        body = _packet_body(packet)
        if not body:
            continue
        proof_gate = body.get("proof_gate") if isinstance(body.get("proof_gate"), dict) else {}
        raw_values = []
        if isinstance(proof_gate, dict):
            raw_values.extend(_string_or_list(proof_gate.get("forbidden_commands")))
        raw_values.extend(_string_or_list(body.get("forbidden_commands")))
        for item in raw_values:
            command = _clean_command(item)
            if command and command not in forbidden:
                forbidden.append(command)
    return forbidden[:12]


def filter_forbidden_gate_commands(commands: list[str], forbidden: list[str]) -> list[str]:
    if not forbidden:
        return commands
    return [command for command in commands if not _command_is_forbidden(command, forbidden)]


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

    intent = getattr(getattr(task, "mission_plan", None), "mission_intent", None)
    texts = [
        str(getattr(task, "title", "") or ""),
        str(getattr(task, "description", "") or ""),
        *[str(item) for item in (getattr(task, "acceptance_criteria", []) or [])],
        *[str(item) for item in (getattr(task, "proof_expectations", []) or [])],
        *[str(item) for item in (getattr(task, "operator_notes", []) or [])],
    ]
    if intent is not None:
        texts.extend(
            [
                str(getattr(intent, "title", "") or ""),
                str(getattr(intent, "objective", "") or ""),
                *[str(item) for item in (getattr(intent, "acceptance_criteria", []) or [])],
            ]
        )
    found: list[str] = []
    for text in texts:
        for command in _extract_command_candidates(text):
            if command not in found:
                found.append(command)
    return found[:3]


def goal_demands_exact_proof(task: Task) -> bool:
    intent = getattr(getattr(task, "mission_plan", None), "mission_intent", None)
    text = " ".join(
        [
            str(getattr(task, "title", "") or ""),
            str(getattr(task, "description", "") or ""),
            *[str(item) for item in (getattr(task, "acceptance_criteria", []) or [])],
            *[str(item) for item in (getattr(task, "proof_expectations", []) or [])],
            *[str(item) for item in (getattr(task, "non_goals", []) or [])],
            *[str(item) for item in (getattr(task, "operator_notes", []) or [])],
            str(getattr(intent, "title", "") or "") if intent is not None else "",
            str(getattr(intent, "objective", "") or "") if intent is not None else "",
            *([str(item) for item in (getattr(intent, "acceptance_criteria", []) or [])] if intent is not None else []),
            *([str(item) for item in (getattr(intent, "non_goals", []) or [])] if intent is not None else []),
        ]
    ).lower()
    exact_markers = (
        "exact proof",
        "exact command proof",
        "exact focused proof",
        "must run exactly",
        "run exactly",
        "demanded exactly",
    )
    forbid_generic_markers = (
        "no flutter test",
        "no flutter tests",
        "no generic proof",
        "forbid flutter",
        "forbade flutter",
        "do not run flutter",
    )
    has_proof_expectation_command = bool(getattr(task, "proof_expectations", None)) and bool(goal_named_proof_commands(task))
    return (
        any(marker in text for marker in exact_markers)
        or any(marker in text for marker in forbid_generic_markers)
        or has_proof_expectation_command
    )


def _packet_body(packet: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(packet, dict):
        return {}
    body = packet.get("body")
    if isinstance(body, dict):
        return body
    return packet


def _packet_command_values(proof_gate: Any) -> list[str]:
    if not isinstance(proof_gate, dict):
        return []
    raw: list[Any] = []
    commands = proof_gate.get("commands")
    if isinstance(commands, list):
        raw.extend(commands)
    for key in ("command", "self_test_command", "focused_self_test"):
        if proof_gate.get(key):
            raw.append(proof_gate.get(key))
    result: list[str] = []
    for item in raw:
        command = _clean_command(item)
        if not command or not _looks_like_command(command):
            continue
        if re.search(r"[<>|;&$]", command):
            continue
        if re.search(r"(?i)(secret|token|password|credential)", command):
            continue
        if command not in result:
            result.append(command)
    return result


def _string_or_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value:
        return [value]
    return []


def _extract_command_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for match in re.findall(r"`([^`\n]{4,200})`", text):
        candidates.append(match)
    for line in text.splitlines():
        stripped = line.strip().lstrip("-*").strip()
        candidates.append(stripped)
        exact_tail = _exact_command_tail(stripped)
        if exact_tail:
            candidates.append(exact_tail)
    result: list[str] = []
    for candidate in candidates:
        command = _clean_goal_command_candidate(candidate)
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


def _exact_command_tail(line: str) -> str:
    text = str(line or "").strip()
    if not text:
        return ""
    match = re.search(
        r"(?i)(?:"
        r"exact\s+(?:focused\s+)?proof(?:\s+command)?"
        r"|exact\s+command\s+proof"
        r"|command\s+proof\s+must\s+run\s+exactly"
        r"|must\s+run\s+exactly"
        r"|run\s+exactly"
        r"|demanded\s+exactly"
        r")\s*(?:[:=-]\s*|\s+)(.+)$",
        text,
    )
    if not match:
        return ""
    return match.group(1)


def _clean_goal_command_candidate(value: object) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    text = re.split(r"(?i)\s+passes(?:\s|\(|$)", text, maxsplit=1)[0]
    text = re.split(r"(?i)\s+(?:do\s+not|don't|without|forbid|forbidden)\b", text, maxsplit=1)[0]
    text = re.split(r"(?i)\s+no\s+flutter\b", text, maxsplit=1)[0]
    return text.rstrip(".,;")


def _command_is_forbidden(command: str, forbidden: list[str]) -> bool:
    normalized = _normalize_forbidden_compare(command)
    if not normalized:
        return False
    for item in forbidden:
        blocked = _normalize_forbidden_compare(item)
        if not blocked:
            continue
        if normalized == blocked or normalized.startswith(f"{blocked} "):
            return True
    return False


def _normalize_forbidden_compare(command: object) -> str:
    return " ".join(str(command or "").strip().lower().split())


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
    return any(marker in text for marker in ("pytest", "flutter ", "dart ", "python ", "manage.py", "npm ", "pnpm ", "echo "))


def _clean_command(value: object) -> str:
    text = str(value or "").strip()
    if "\n" in text and "<<" in text:
        return "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
    return " ".join(text.split())
