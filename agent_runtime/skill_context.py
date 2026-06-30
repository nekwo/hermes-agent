from __future__ import annotations

from dataclasses import dataclass, field

MAX_SKILL_CHARS_PER_PERSONA = 24_000


@dataclass(slots=True)
class PersonaSkillContext:
    text: str
    loaded: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)


def load_persona_skill_context(skill_names: list[str], *, task_id: str | None = None) -> PersonaSkillContext:
    from agent.skill_commands import _build_skill_message, _load_skill_payload

    parts: list[str] = []
    loaded: list[str] = []
    missing: list[str] = []
    truncated: list[str] = []
    remaining = MAX_SKILL_CHARS_PER_PERSONA

    for name in skill_names:
        clean = str(name).strip()
        if not clean:
            continue
        payload = _load_skill_payload(clean, task_id=task_id)
        if payload is None:
            missing.append(clean)
            continue
        loaded_skill, skill_dir, display_name = payload
        block = _build_skill_message(
            loaded_skill,
            skill_dir,
            activation_note=f"# Loaded Harness persona skill: {display_name}",
            runtime_note="Loaded by Agent Runtime Harness persona skill manifest.",
            session_id=task_id,
        )
        if len(block) > remaining:
            parts.append(block[: max(0, remaining)])
            truncated.append(clean)
            remaining = 0
        else:
            parts.append(block)
            remaining -= len(block)
        loaded.append(clean)
        if remaining <= 0:
            remaining = 0
            continue

    return PersonaSkillContext(
        text="\n\n".join(part for part in parts if part),
        loaded=loaded,
        missing=missing,
        truncated=truncated,
    )
