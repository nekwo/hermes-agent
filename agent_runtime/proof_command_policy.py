from __future__ import annotations

import re
import shlex
from pathlib import Path

from .decision_schema import AgentDecision, DecisionPayloadInvalid, DecisionType
from .models import Task
from .stagec_command_policy import (
    reject_invalid_stagec_screenshot_window_args as _reject_invalid_stagec_screenshot_window_args,
    reject_unpinned_mission_control_stagec_commands as _reject_unpinned_mission_control_stagec_commands,
)

# The two Stage C argument checks moved to ``agent_runtime.stagec_command_policy``
# (S1 of the mission-lane removal). They inspect the MCP command string, not a
# Task, so they outlive this module — everything else here reads Task fields and
# dies with the goal/task lane. The aliases keep the call sites below unchanged.


def validate_request_test_run_policy(task: Task, decision: AgentDecision) -> None:
    if decision.type != DecisionType.REQUEST_TEST_RUN:
        return
    payload = decision.payload if isinstance(decision.payload, dict) else {}
    commands = [str(command).strip() for command in (payload.get("commands") or []) if str(command).strip()]
    if not commands:
        return
    stage_id = str(payload.get("stage_id") or "")
    proof_intent = str(payload.get("proof_intent") or payload.get("intent") or "")
    _reject_invalid_stagec_screenshot_window_args(commands)
    _reject_non_goal_commands(task, commands)
    if _task_requires_mission_control_stagec_env_pins(
        task,
        stage_id=stage_id,
        summary=decision.summary,
        rationale=decision.rationale,
        proof_intent=proof_intent,
    ):
        _reject_unpinned_mission_control_stagec_commands(commands)
    if task_requires_launcher_contract_consumption_proof(
        task,
        stage_id=stage_id,
        summary=decision.summary,
        rationale=decision.rationale,
        proof_intent=proof_intent,
    ):
        for command in commands:
            if is_generic_launcher_readiness_command(command):
                raise DecisionPayloadInvalid(
                    "Launcher contract proof policy failed: generic Flutter/Dart readiness commands do not prove backend contract packet consumption."
                )
    if not task_requires_bounded_smoke_proof(task, stage_id=stage_id, summary=decision.summary, rationale=decision.rationale):
        return
    for command in commands:
        if is_unbounded_full_suite_command(command):
            raise DecisionPayloadInvalid(
                "Smoke/no-edit proof command policy failed: request a targeted smoke/readiness command, not an unbounded full-suite command."
            )


def _reject_non_goal_commands(task: Task, commands: list[str]) -> None:
    non_goals = [str(item or "").strip() for item in (getattr(task, "non_goals", []) or []) if str(item or "").strip()]
    if not non_goals:
        return
    for command in commands:
        for non_goal in non_goals:
            if _command_matches_non_goal(command, non_goal):
                raise DecisionPayloadInvalid(
                    f"Proof command rejected by bundle non-goal: {non_goal[:180]}"
                )


def _command_matches_non_goal(command: str, non_goal: str) -> bool:
    command_text = _normalize_for_non_goal_match(command)
    non_goal_text = _normalize_for_non_goal_match(non_goal)
    if not command_text or not non_goal_text:
        return False
    broad_only = any(marker in non_goal_text for marker in ("broad", "suite", "full suite", "full-suite"))
    for marker in _non_goal_path_markers(non_goal_text):
        if _command_targets_non_goal_marker(command_text, marker, broad_only=broad_only):
            return True
    return False


def _command_targets_non_goal_marker(command_text: str, marker: str, *, broad_only: bool) -> bool:
    if not broad_only:
        return marker in command_text
    pattern = rf"(?<![a-z0-9_./-]){re.escape(marker)}(?!/[a-z0-9_.-])"
    return re.search(pattern, command_text) is not None


def _non_goal_path_markers(text: str) -> list[str]:
    markers = []
    for marker in re.findall(r"[a-z0-9_.-]+/[a-z0-9_./-]+", text):
        if marker not in markers:
            markers.append(marker)
    return markers


def _normalize_for_non_goal_match(value: str) -> str:
    return " ".join(str(value or "").replace("\\", "/").lower().split())


def narrow_launcher_contract_analyze_command(
    task: Task,
    command: str,
    *,
    stage_id: str | None = None,
    proof_intent: str = "",
) -> str:
    if not task_requires_launcher_contract_consumption_proof(task, stage_id=stage_id, proof_intent=proof_intent):
        return command
    if _explicit_launcher_main_analyze_gate_text(_policy_text(task, stage_id=stage_id, proof_intent=proof_intent)):
        return command
    if not _contains_contract_consumption_signal(command):
        return command
    return _strip_trailing_launcher_main_analyze(command)


def task_requires_bounded_smoke_proof(task: Task, *, stage_id: str | None = None, summary: str = "", rationale: str = "") -> bool:
    text = _policy_text(task, stage_id=stage_id, summary=summary, rationale=rationale)
    if not any(marker in text for marker in ("smoke", "observational", "no-edit", "no product edits", "without product edits")):
        return False
    return not _explicit_full_suite_gate_text(text)


def task_requires_launcher_contract_consumption_proof(
    task: Task,
    *,
    stage_id: str | None = None,
    summary: str = "",
    rationale: str = "",
    proof_intent: str = "",
) -> bool:
    text = _policy_text(task, stage_id=stage_id, summary=summary, rationale=rationale, proof_intent=proof_intent)
    if "launcher_contract_smoke" in text:
        return True
    if "launcher" not in text or "contract" not in text:
        return False
    return any(marker in text for marker in ("backend proof", "handoff", "joined", "packet", "consum"))


def _task_requires_mission_control_stagec_env_pins(
    task: Task,
    *,
    stage_id: str | None = None,
    summary: str = "",
    rationale: str = "",
    proof_intent: str = "",
) -> bool:
    text = _policy_text(task, stage_id=stage_id, summary=summary, rationale=rationale, proof_intent=proof_intent)
    if "mission control" not in text and "missioncontrol" not in text:
        return False
    return any(marker in text for marker in ("stage c", "stagec", "mcp", "screenshot", "visual", "marionette"))


def _explicit_full_suite_gate_text(text: str) -> bool:
    return any(marker in text for marker in ("full-suite gate", "full suite gate", "full-suite proof", "full suite proof"))


def _explicit_launcher_main_analyze_gate_text(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "lib/main.dart changed",
            "main.dart changed",
            "bootstrap changed",
            "app bootstrap",
            "startup wiring",
            "route wiring",
            "entrypoint changed",
            "entry point changed",
        )
    )


def _strip_trailing_launcher_main_analyze(command: str) -> str:
    stripped = re.sub(
        r"(?is)\s*(?:&&|;)\s*flutter\s+analyze\s+lib[\\/]main\.dart\s*$",
        "",
        str(command or "").strip(),
        count=1,
    ).strip()
    return stripped or command


def is_unbounded_full_suite_command(command: str) -> bool:
    normalized = _normalize_command(command)
    parts = _split_command(normalized)
    for segment in _command_segments(parts):
        if _manage_py_test_without_target(segment):
            return True
        if _flutter_test_without_target(segment):
            return True
        if segment and (_is_pytest_executable(segment[0]) or _is_python_executable(segment[0])):
            if _pytest_without_target(segment):
                return True
    return False


def is_generic_launcher_readiness_command(command: str) -> bool:
    normalized = _normalize_command(command)
    if _contains_contract_consumption_signal(normalized):
        return False
    parts = _split_command(normalized)
    segments = [_strip_navigation_segment(segment) for segment in _command_segments(parts)]
    meaningful = [segment for segment in segments if segment]
    if not meaningful:
        return False
    return all(_is_generic_launcher_readiness_segment(segment) for segment in meaningful)


def _command_segments(parts: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for part in parts:
        if part in {"&&", "||", ";", "|"}:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(part)
    if current:
        segments.append(current)
    return segments


def _strip_navigation_segment(parts: list[str]) -> list[str]:
    if not parts:
        return []
    if parts[0] in {"cd", "set-location", "push-location"}:
        return []
    if parts[0] in {"export", "set"}:
        return []
    return parts


def _is_generic_launcher_readiness_segment(parts: list[str]) -> bool:
    if not parts:
        return True
    executable = Path(parts[0]).name.lower()
    args = parts[1:]
    if executable == "flutter" and args:
        return args[0] in {"--version", "-v", "doctor", "config", "devices"}
    if executable == "dart" and args:
        return args[0] in {"--version", "-v"}
    if executable in {"where", "which"} and any(Path(arg).name.lower() in {"flutter", "dart"} for arg in args):
        return True
    return False


def _contains_contract_consumption_signal(command: str) -> bool:
    lowered = command.lower()
    return any(
        marker in lowered
        for marker in (
            "backend_proof",
            "backend proof",
            "contract_packet",
            "contract packet",
            "contract_consum",
            "joined_backend",
            "launcher_contract_smoke",
            "proof_consumed",
            "packet_consumed",
        )
    )


def _manage_py_test_without_target(parts: list[str]) -> bool:
    manage_index = next((index for index, part in enumerate(parts) if Path(part).name.lower() == "manage.py"), -1)
    if manage_index < 0 or len(parts) <= manage_index + 1 or parts[manage_index + 1] != "test":
        return False
    if manage_index > 0 and not all(_is_python_executable(part) or part in {"uv", "poetry", "run"} for part in parts[:manage_index]):
        return False
    args = parts[manage_index + 2 :]
    positional = [arg for arg in args if arg and not arg.startswith("-") and "=" not in arg]
    return not positional


def _flutter_test_without_target(parts: list[str]) -> bool:
    if len(parts) < 2 or Path(parts[0]).name.lower() != "flutter" or parts[1] != "test":
        return False
    args = parts[2:]
    positional = [arg for arg in args if arg and not arg.startswith("-") and "=" not in arg]
    return not positional


def _pytest_without_target(parts: list[str]) -> bool:
    if len(parts) >= 3 and _is_python_executable(parts[0]) and parts[1:3] == ["-m", "pytest"]:
        args = parts[3:]
    elif parts and _is_pytest_executable(parts[0]):
        args = parts[1:]
    else:
        return False
    positional = [arg for arg in args if arg and not arg.startswith("-") and "=" not in arg]
    return not positional


def _is_python_executable(value: str) -> bool:
    return Path(value).name.lower() in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}


def _is_pytest_executable(value: str) -> bool:
    return Path(value).name.lower() in {"pytest", "pytest.exe"}


def _normalize_command(command: str) -> str:
    return " ".join(str(command or "").strip().split())


def _split_command(command: str) -> list[str]:
    try:
        return [part.lower() for part in shlex.split(command, posix=True)]
    except ValueError:
        return command.lower().split()


def _policy_text(task: Task, *, stage_id: str | None = None, summary: str = "", rationale: str = "", proof_intent: str = "") -> str:
    parts = [
        getattr(task, "title", ""),
        getattr(task, "description", ""),
        " ".join(getattr(task, "acceptance_criteria", []) or []),
        " ".join(getattr(task, "non_goals", []) or []),
        " ".join(getattr(task, "risk_flags", []) or []),
        str(stage_id or ""),
        summary,
        rationale,
        proof_intent,
    ]
    return " ".join(str(part or "") for part in parts).lower()
