from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from hermes_constants import CANONICAL_SHARED_SKILL_IDS, get_shared_skills_dir

HARNESS_SKILLS = CANONICAL_SHARED_SKILL_IDS


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


def harness_skill_source_package(skill: str) -> Path:
    return harness_skill_source_root() / skill


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
    from agent.skill_utils import skill_package_content_hash

    source_dir = harness_skill_source_package(skill)
    source = source_dir / "SKILL.md"
    if not source.exists():
        raise FileNotFoundError(str(source))
    destination = harness_skill_destination(skill, hermes_home=hermes_home)
    destination_dir = destination.parent
    source_hash = skill_package_content_hash(source_dir, source)
    installed_hash = (
        skill_package_content_hash(destination_dir, destination)
        if destination.exists()
        else None
    )
    changed = installed_hash != source_hash
    if changed:
        shared_root = destination_dir.parent
        shared_root.mkdir(parents=True, exist_ok=True)
        staging = shared_root / f".install-{skill}-{uuid.uuid4().hex}"
        backup = shared_root / f".backup-{skill}-{uuid.uuid4().hex}"
        shutil.copytree(source_dir, staging)
        try:
            if destination_dir.exists():
                os.replace(destination_dir, backup)
            os.replace(staging, destination_dir)
        except Exception:
            if not destination_dir.exists() and backup.exists():
                os.replace(backup, destination_dir)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(backup, ignore_errors=True)
        installed_hash = skill_package_content_hash(destination_dir, destination)
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
    from agent.skill_utils import skill_package_content_hash

    mismatches: list[str] = []
    for name in skill_names:
        if name not in HARNESS_SKILLS:
            continue
        source = harness_skill_source(name)
        destination = harness_skill_destination(name, hermes_home=hermes_home)
        if not source.exists() or not destination.exists():
            continue
        if skill_package_content_hash(source.parent, source) != skill_package_content_hash(
            destination.parent, destination
        ):
            mismatches.append(name)
    return mismatches


def file_sha256(path: Path) -> str:
    # mtime-cached: skill-hash checks run per skill × per persona × per resolver
    # pass during a snapshot build, all hashing the same unchanged files.
    from .parse_cache import cached_file_sha256

    return cached_file_sha256(path)
