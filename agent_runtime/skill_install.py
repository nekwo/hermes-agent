from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from hermes_constants import CANONICAL_SHARED_SKILL_IDS, get_shared_skills_dir

HARNESS_SKILLS = CANONICAL_SHARED_SKILL_IDS

#: Where a package this install DISPLACES is kept instead of deleted. Already in
#: ``agent.skill_utils.EXCLUDED_SKILL_DIRS``, and already the convention
#: ``tools/skill_usage.archive_skill`` uses under a skills root — so an archived
#: copy is resolver-invisible and can never become a collision for the id it
#: holds.
REPLACED_ARCHIVE_DIR_NAME = ".archive"


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
        displaced = False
        try:
            if destination_dir.exists():
                os.replace(destination_dir, backup)
                displaced = True
            os.replace(staging, destination_dir)
        except Exception:
            if not destination_dir.exists() and backup.exists():
                os.replace(backup, destination_dir)
                displaced = False
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            if displaced and backup.exists():
                _archive_replaced_package(skill, backup, shared_root)
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


def _archive_replaced_package(skill: str, package: Path, shared_root: Path) -> Path | None:
    """Keep the package this install DISPLACED instead of deleting it.

    This used to be ``shutil.rmtree(backup, ignore_errors=True)``, which makes a
    repair that targeted the wrong root unrecoverable — and the caller that
    repairs is a git hook, whose environment named no root at all until
    ``scripts/verify_harness_skill_install.py`` was made to refuse to guess one.
    Explicit resolution is the fix; this is the seatbelt for the next way a
    wrong root gets in, and it costs one small directory per actual content
    change (nothing is written when the hashes already match).

    Never raises: a failed archive must not fail an install that has already
    succeeded. And when the move cannot be made the displaced copy is REMOVED
    rather than left where it is — ``.backup-*`` is not resolver-invisible (only
    the exact names in ``agent.skill_utils.EXCLUDED_SKILL_DIRS`` are), so
    leaving one beside the packages would turn a lost archive into a live
    resolution collision on the very id just installed.
    """

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_root = shared_root / REPLACED_ARCHIVE_DIR_NAME
    destination = archive_root / f"{skill}-{stamp}"
    try:
        archive_root.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination = archive_root / f"{skill}-{stamp}-{uuid.uuid4().hex[:8]}"
        os.replace(package, destination)
        return destination
    except OSError:
        try:
            shutil.move(str(package), str(destination))
            return destination
        except Exception:
            shutil.rmtree(package, ignore_errors=True)
            return None


class HarnessSkillInstallDiverged(RuntimeError):
    """The installed package does not match the repo package AFTER an install.

    ``skill``, ``source_hash`` and ``installed_hash`` are the branch points a
    caller renders; the message is operator prose and is free to change.
    """

    def __init__(
        self, skill: str, *, source_hash: str, installed_hash: str | None
    ):
        super().__init__(
            f"installed skill package diverges from the repo package: {skill} "
            f"(repo {source_hash}, installed {installed_hash})"
        )
        self.skill = str(skill)
        self.source_hash = str(source_hash)
        self.installed_hash = installed_hash


def install_and_verify_harness_skill(skill: str) -> SkillInstallResult:
    """Install one canonical skill and REFUSE unless the bytes then match.

    The join the repo→installed pair has never had at a WRITE. Until plan S4
    every reader of :func:`harness_skill_hash_mismatches` was advisory:
    ``profile_readiness`` files it as a severity-15 row and ``prompt_observability``
    as a HUD flag, and the launcher's sync button is a button. Nothing refused,
    so the 2026-08-24 incident — an agent running against a 14457-byte copy of a
    14906-byte skill — was reported and then handed the agent anyway. A verb that
    ASSIGNS a skill is the one place where "the copy is stale" has an answer that
    is not a warning, so this is where it is asked.

    Why the three conditions and not just the mismatch list. Both of the
    obvious probes are silent on the case that matters most:

    * ``harness_skill_hash_mismatches`` ``continue``s past a destination that
      does NOT EXIST, so a copy that never landed produces an EMPTY mismatch
      list — a false all-clear, which is exactly the shape of an unrun gate.
    * ``SkillInstallResult.ok`` is computed from the hash read at the end of the
      install call, so it cannot see a copy displaced between that read and now.

    So all three are asked: the destination exists, the install's own receipt is
    ``ok``, and the mismatch detector — an INDEPENDENT re-read of both packages
    — is empty. Raising :class:`HarnessSkillInstallDiverged` rather than
    returning a flag is deliberate: a caller that forgets to read a flag ships
    the stale copy, which is the defect.
    """

    result = install_harness_skill(skill)
    if result.installed and result.ok and not harness_skill_hash_mismatches([skill]):
        return result
    raise HarnessSkillInstallDiverged(
        skill,
        source_hash=result.source_hash,
        installed_hash=result.installed_hash,
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
