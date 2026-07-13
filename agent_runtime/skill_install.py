from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from hermes_constants import get_shared_skills_dir

HARNESS_SKILLS = frozenset(
    {
        "harness-mission-lead",
        "harness-dev-delivery",
        "harness-continuity",
        "harness-qa-verdict",
        "harness-runtime-model",
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


def harness_skill_source_root() -> Path:
    return Path(__file__).resolve().parent.parent / "docs" / "agent-runtime-harness" / "harness-skills"


def harness_skill_source(skill: str) -> Path:
    return harness_skill_source_root() / skill / "SKILL.md"


def harness_skill_destination(skill: str, *, hermes_home: Path | None = None) -> Path:
    # Built-in Harness skills install to the shared canonical root so every
    # persona references one copy and realm sync publishes it. ``hermes_home``
    # is retained for signature/back-compat but no longer changes the target —
    # placement is now root-relative, not per-profile.
    return get_shared_skills_dir() / skill / "SKILL.md"


def install_harness_skills(*, hermes_home: Path | None = None, skills: list[str] | None = None) -> list[SkillInstallResult]:
    selected = skills or sorted(HARNESS_SKILLS)
    results: list[SkillInstallResult] = []
    for skill in selected:
        results.append(install_harness_skill(skill, hermes_home=hermes_home))
    return results


def install_harness_skills_for_personas(personas) -> list[SkillInstallResult]:
    # Placement is the shared canonical root (see harness_skill_destination),
    # so a skill required by multiple personas installs exactly once. We still
    # iterate personas to collect the union of required Harness skills.
    results: list[SkillInstallResult] = []
    installed: set[str] = set()
    for persona in personas:
        for skill in harness_required_skills_for_persona(persona):
            if skill in installed:
                continue
            installed.add(skill)
            results.append(install_harness_skill(skill))
    return results


def harness_required_skills_for_persona(persona) -> list[str]:
    selected: list[str] = []
    for skill in [skill for skill in getattr(persona, "skills", []) or [] if skill in HARNESS_SKILLS]:
        if skill not in selected:
            selected.append(skill)
    return selected


def install_harness_skill(skill: str, *, hermes_home: Path | None = None) -> SkillInstallResult:
    if skill not in HARNESS_SKILLS:
        raise ValueError(f"not a Harness skill: {skill}")
    source = harness_skill_source(skill)
    if not source.exists():
        raise FileNotFoundError(str(source))
    destination = harness_skill_destination(skill, hermes_home=hermes_home)
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


def harness_skill_hash_mismatches(skill_names: list[str], *, hermes_home: Path | None = None) -> list[str]:
    mismatches: list[str] = []
    for name in skill_names:
        if name not in HARNESS_SKILLS:
            continue
        source = harness_skill_source(name)
        destination = harness_skill_destination(name, hermes_home=hermes_home)
        if not source.exists() or not destination.exists():
            continue
        if file_sha256(source) != file_sha256(destination):
            mismatches.append(name)
    return mismatches


def harness_skill_installed_ok(skill: str, *, hermes_home: Path | None = None) -> bool:
    if skill not in HARNESS_SKILLS:
        return False
    source = harness_skill_source(skill)
    destination = harness_skill_destination(skill, hermes_home=hermes_home)
    return source.exists() and destination.exists() and file_sha256(source) == file_sha256(destination)


def file_sha256(path: Path) -> str:
    # mtime-cached: skill-hash checks run per skill × per persona × per resolver
    # pass during a snapshot build, all hashing the same unchanged files.
    from .parse_cache import cached_file_sha256

    return cached_file_sha256(path)
