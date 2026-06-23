from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from hermes_constants import get_hermes_home

STAGE46_SKILLS = frozenset(
    {
        "harness-mission-lead",
        "harness-dev-delivery",
        "harness-qa-verdict",
        "launcher-analyze-proof",
    }
)


@dataclass(frozen=True, slots=True)
class SkillInstallResult:
    skill: str
    source: str
    destination: str
    source_hash: str
    installed_hash: str | None
    installed: bool
    changed: bool
    ok: bool


def stage46_skill_source_root() -> Path:
    return Path(__file__).resolve().parent.parent / "docs" / "agent-runtime-harness" / "stage46-skills"


def stage46_skill_source(skill: str) -> Path:
    return stage46_skill_source_root() / skill / "SKILL.md"


def stage46_skill_destination(skill: str, *, hermes_home: Path | None = None) -> Path:
    home = hermes_home or get_hermes_home()
    return home / "skills" / skill / "SKILL.md"


def install_stage46_skills(*, hermes_home: Path | None = None, skills: list[str] | None = None) -> list[SkillInstallResult]:
    selected = skills or sorted(STAGE46_SKILLS)
    results: list[SkillInstallResult] = []
    for skill in selected:
        results.append(install_stage46_skill(skill, hermes_home=hermes_home))
    return results


def install_stage46_skills_for_personas(personas) -> list[SkillInstallResult]:
    from .profile_context import resolve_persona_profile

    results: list[SkillInstallResult] = []
    installed_keys: set[tuple[str, str | None]] = set()
    for persona in personas:
        required = stage46_required_skills_for_persona(persona)
        if not required:
            continue
        binding = resolve_persona_profile(persona)
        hermes_home = binding.profile_home
        for skill in required:
            key = (skill, str(hermes_home) if hermes_home else None)
            if key in installed_keys:
                continue
            installed_keys.add(key)
            results.append(install_stage46_skill(skill, hermes_home=hermes_home))
    return results


def stage46_required_skills_for_persona(persona) -> list[str]:
    selected: list[str] = []
    for skill in [skill for skill in getattr(persona, "skills", []) or [] if skill in STAGE46_SKILLS]:
        if skill not in selected:
            selected.append(skill)
    return selected


def install_stage46_skill(skill: str, *, hermes_home: Path | None = None) -> SkillInstallResult:
    if skill not in STAGE46_SKILLS:
        raise ValueError(f"not a Stage 46 skill: {skill}")
    source = stage46_skill_source(skill)
    if not source.exists():
        raise FileNotFoundError(str(source))
    destination = stage46_skill_destination(skill, hermes_home=hermes_home)
    source_hash = file_sha256(source)
    installed_hash = file_sha256(destination) if destination.exists() else None
    changed = installed_hash != source_hash
    if changed:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        installed_hash = file_sha256(destination)
    return SkillInstallResult(
        skill=skill,
        source=str(source),
        destination=str(destination),
        source_hash=source_hash,
        installed_hash=installed_hash,
        installed=destination.exists(),
        changed=changed,
        ok=installed_hash == source_hash,
    )


def stage46_skill_hash_mismatches(skill_names: list[str], *, hermes_home: Path | None = None) -> list[str]:
    mismatches: list[str] = []
    for name in skill_names:
        if name not in STAGE46_SKILLS:
            continue
        source = stage46_skill_source(name)
        destination = stage46_skill_destination(name, hermes_home=hermes_home)
        if not source.exists() or not destination.exists():
            continue
        if file_sha256(source) != file_sha256(destination):
            mismatches.append(name)
    return mismatches


def stage46_skill_installed_ok(skill: str, *, hermes_home: Path | None = None) -> bool:
    if skill not in STAGE46_SKILLS:
        return False
    source = stage46_skill_source(skill)
    destination = stage46_skill_destination(skill, hermes_home=hermes_home)
    return source.exists() and destination.exists() and file_sha256(source) == file_sha256(destination)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()
