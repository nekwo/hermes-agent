from __future__ import annotations

from typing import Any


DELIVERABLES: dict[str, str] = {
    "code feature": "a working code change with focused proof",
    "design document": "a concise design/document artifact",
    "qa verdict": "a proof-backed QA verdict",
    "mission scope": "a scoped mission route with acceptance and proof expectations",
}

ROLE_OPENERS: dict[str, str] = {
    "lead": "Coordinate this node.",
    "neko": "Coordinate this node.",
    "builder": "Build this node.",
    "dev": "Build this node.",
    "backend_dev": "Build this node.",
    "verifier": "Verify this node.",
    "qa": "Verify this node.",
}


def render_objective(
    stage: object | None,
    *,
    goal: Task | str,
    input_artifact: str | None = None,
    role: str | None = None,
    output_type: str | None = None,
) -> str:
    goal_text = _goal_text(goal)
    objective = _stage_objective(stage) or goal_text
    resolved_role = _normalize(role or getattr(stage, "owner", None) or "")
    resolved_output = _normalize(output_type or getattr(stage, "output_type", None) or _output_type_from_stage(stage))
    deliverable = DELIVERABLES.get(resolved_output, f"a {resolved_output or 'bounded'} deliverable")
    opener = ROLE_OPENERS.get(resolved_role, "Complete this node.")
    lines = [
        opener,
        f"Objective: {objective}",
        f"Input: {input_artifact or goal_text}",
        f"Deliver: {deliverable}.",
    ]
    acceptance = _acceptance(goal, stage)
    if acceptance:
        lines.append("Acceptance: " + "; ".join(acceptance))
    return "\n".join(lines)


def _goal_text(goal: Task | str) -> str:
    if isinstance(goal, Task):
        return str(goal.description or goal.title or "").strip()
    return str(goal or "").strip()


def _stage_objective(stage: object | None) -> str:
    return str(getattr(stage, "objective", "") or "").strip() if stage is not None else ""


def _acceptance(goal: Task | str, stage: object | None) -> list[str]:
    raw = getattr(stage, "acceptance_criteria", None) or (getattr(goal, "acceptance_criteria", None) if isinstance(goal, Task) else None) or []
    return [str(item).strip() for item in raw if str(item).strip()][:5]


def _output_type_from_stage(stage: object | None) -> str:
    gate = getattr(stage, "proof_gate", {}) if stage is not None else {}
    required = set()
    if isinstance(gate, dict):
        required = {str(item) for item in gate.get("required_proof_types", []) or []}
    kind = _normalize(getattr(stage, "kind", "") if stage is not None else "")
    if "qa_verdict" in required or kind in {"qa_verdict", "qa verdict"}:
        return "qa verdict"
    if required & {"test_run", "diff", "diff_stat", "commit"} or kind in {"implementation", "proof_only", "proof only"}:
        return "code feature"
    if required & {"artifact", "text", "url"} or kind in {"scope", "context", "investigation"}:
        return "design document"
    return "design document"


def _normalize(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").replace("-", " ").split())
