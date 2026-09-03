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

    Why two conditions and not just one. ``SkillInstallResult.ok`` is computed
    from the hash read at the end of the install call, so it cannot see a copy
    displaced between that read and now; and the install having RUN is not the
    same fact as the bytes being right. So both are asked: the install's own
    receipt is ``ok``, and an INDEPENDENT re-read of both packages says
    ``SKILL_HASH_MATCHES``.

    That second condition used to be three, because the only projection was the
    mismatch LIST and an absent destination fell out of it — a copy that never
    landed produced an empty list, the false all-clear this verb exists to
    refuse — so the destination's existence had to be asked separately. It no
    longer does: ``matches`` is a positive claim, and ``not_installed`` is one
    of the states it is not. Raising :class:`HarnessSkillInstallDiverged` rather
    than returning a flag is deliberate: a caller that forgets to read a flag
    ships the stale copy, which is the defect.
    """

    result = install_harness_skill(skill)
    states = harness_skill_hash_states([skill])
    if result.ok and states and states[0].state == SKILL_HASH_MATCHES:
        return result
    raise HarnessSkillInstallDiverged(
        skill,
        source_hash=result.source_hash,
        installed_hash=result.installed_hash,
    )


@dataclass(frozen=True, slots=True)
class SkillSizeCeiling:
    """A per-turn byte budget for one canonical package, and why it is that."""

    limit: int
    reason: str


#: PER-TURN CONTEXT BUDGET, one entry per canonical shared skill id.
#:
#: A ``required_preload`` skill is not read when an agent needs it. Its whole
#: ``SKILL.md`` body is pasted into EVERY turn of every persona that lists it
#: (``mission_chat_turn_context._resolve_skill_preload`` ->
#: ``agent.skill_commands.build_preloaded_skills_prompt`` ->
#: ``_build_skill_message``, which appends ``content.strip()`` verbatim), so the
#: file's length is a cost paid forever by turns that never look at it. Nothing
#: measured that: the install gate compares HASHES and is indifferent to length,
#: and ``tests/agent_runtime/test_persona_skill_policy.py`` asserts STRINGS. The
#: charsheet package's own field notes record a rewrite taking it 26,042 B ->
#: 44,478 B (a 70% growth) with nothing reporting it, and filed the missing
#: measurement as a row. This table is that measurement.
#:
#: WHAT IS COUNTED, and what deliberately is not. ``SKILL.md`` only. A package's
#: ``references/`` are NOT pasted into the turn — ``_build_skill_message`` lists
#: their PATHS under "[This skill has supporting files:]" and the agent fetches
#: one with ``skill_view`` when it needs it, which is exactly the split the
#: reference layout exists to buy. Counting their bytes here would price a cost
#: no turn pays and would push authors to inline them, which is the opposite of
#: the intended move. ``FIELD-NOTES.md`` is the same: 208 KB at the charsheet
#: package root, installed with the package, never in a turn. So "the package
#: got bigger" is not the question this gate asks; "the PRELOAD got bigger" is.
#:
#: WHICH COPY. The REPO copy, because that is the one a diff can defend. The
#: installed copy is derived from it by ``install_harness_skill`` and the same
#: gate already repairs any divergence.
#:
#: Every ceiling is today's measured size rounded up to the next KiB boundary
#: with the headroom stated, so a normal edit lands and a rewrite has to argue.
#: RAISING ONE IS THE POINT: it is a one-line diff someone signs, in the same
#: change as the growth, next to the reason. Deleting a paragraph to fit is the
#: other legitimate answer and usually the better one.
#:
#: A canonical id with NO entry here FAILS the gate rather than passing
#: unmeasured — an unceilinged package is the silence this table exists to end.
SKILL_SIZE_CEILINGS: dict[str, SkillSizeCeiling] = {
    "harness-charsheet-authoring": SkillSizeCeiling(
        24_576,
        "22,449 B on 2026-09-02, the largest preload we ship; 2,127 B (9%) of "
        "headroom, and the four references/ files are where new prose goes.",
    ),
    "harness-dev-delivery": SkillSizeCeiling(
        14_336,
        "12,932 B on 2026-09-02; every dev persona pays it on every turn, so "
        "1,404 B (11%) of headroom and no references/ lane yet to spill into.",
    ),
    "harness-qa-verdict": SkillSizeCeiling(
        11_264,
        "10,155 B on 2026-09-02, the smallest of the four; 1,109 B (11%) of "
        "headroom.",
    ),
    "harness-runtime-model": SkillSizeCeiling(
        16_384,
        "14,414 B on 2026-09-02, already split with a 33 KB references/ "
        "operations doc it does NOT preload; 1,970 B (14%) of headroom.",
    ),
}

#: A canonical id the table forgot. Reported as its own state, never folded into
#: "over ceiling" -- "nobody set a budget" and "the budget was exceeded" are
#: different repairs, the same distinction ``SKILL_HASH_NOT_INSTALLED`` draws.
SKILL_SIZE_NO_CEILING = -1


@dataclass(frozen=True, slots=True)
class SkillSizeState:
    """One canonical id, the bytes its preload costs, and its declared budget."""

    skill: str
    size: int
    ceiling: int
    reason: str

    @property
    def over(self) -> bool:
        return self.ceiling == SKILL_SIZE_NO_CEILING or self.size > self.ceiling


def harness_skill_size_states(skill_names: list[str]) -> list[SkillSizeState]:
    """Per canonical skill, the repo ``SKILL.md`` size against its ceiling.

    A missing source file is not this function's finding -- the hash states
    already own the absent case -- so it is skipped rather than reported as a
    zero-byte pass.
    """

    states: list[SkillSizeState] = []
    for name in skill_names:
        if name not in HARNESS_SKILLS:
            continue
        source = harness_skill_source(name)
        if not source.exists():
            continue
        declared = SKILL_SIZE_CEILINGS.get(name)
        states.append(
            SkillSizeState(
                skill=name,
                size=source.stat().st_size,
                ceiling=declared.limit if declared else SKILL_SIZE_NO_CEILING,
                reason=(
                    declared.reason
                    if declared
                    else "no ceiling declared for this canonical id"
                ),
            )
        )
    return states


def harness_skill_size_overages(skill_names: list[str]) -> list[SkillSizeState]:
    """Only the states that fail: over their ceiling, or without one."""

    return [state for state in harness_skill_size_states(skill_names) if state.over]


#: The four answers to "does the installed package match the repo package".
#: ABSENT IS NOT MATCHING — the distinction this vocabulary exists for.
SKILL_HASH_MATCHES = "matches"
SKILL_HASH_MISMATCH = "mismatch"
#: No installed package: nothing was compared. Distinct from ``matches``
#: because a persona pointed at nothing is not a persona pointed at the right
#: thing, and distinct from ``mismatch`` because "not installed" is a different
#: repair from "installed wrong".
SKILL_HASH_NOT_INSTALLED = "not_installed"
#: No repo package for a canonical id — the source side of the same absence.
SKILL_HASH_NO_SOURCE = "no_source"

#: The states in which NO comparison happened, whatever the caller was hoping.
SKILL_HASH_UNCOMPARED = frozenset({SKILL_HASH_NOT_INSTALLED, SKILL_HASH_NO_SOURCE})


@dataclass(frozen=True, slots=True)
class HarnessSkillHashState:
    """One canonical skill id and what the hash read actually established."""

    skill: str
    state: str

    @property
    def compared(self) -> bool:
        return self.state not in SKILL_HASH_UNCOMPARED


def harness_skill_hash_states(
    skill_names: list[str], *, hermes_home: Path | None = None
) -> list[HarnessSkillHashState]:
    """Per canonical skill, whether the two packages were compared and agreed.

    THE read every advisory surface makes, with the absent case named instead of
    dropped. Until 2026-08-30 the only projection was the mismatch LIST, and a
    skill with no installed package simply did not appear in it — so every
    reader saw the same empty list for "compared and identical" and for "there
    was nothing to compare", which is the false-all-clear shape of an unrun
    gate. Non-canonical ids are not in the result at all: they are not this
    function's subject and never were.
    """

    from agent.skill_utils import skill_package_content_hash

    states: list[HarnessSkillHashState] = []
    for name in skill_names:
        if name not in HARNESS_SKILLS:
            continue
        source = harness_skill_source(name)
        destination = harness_skill_destination(name, hermes_home=hermes_home)
        if not source.exists():
            states.append(HarnessSkillHashState(name, SKILL_HASH_NO_SOURCE))
        elif not destination.exists():
            states.append(HarnessSkillHashState(name, SKILL_HASH_NOT_INSTALLED))
        elif skill_package_content_hash(source.parent, source) != skill_package_content_hash(
            destination.parent, destination
        ):
            states.append(HarnessSkillHashState(name, SKILL_HASH_MISMATCH))
        else:
            states.append(HarnessSkillHashState(name, SKILL_HASH_MATCHES))
    return states


def installed_harness_skill_hash(skill: str, *, hermes_home: Path | None = None) -> str | None:
    """The content hash of the INSTALLED package for one canonical id, right now.

    The read-only half of the ``installed_hash`` :func:`install_harness_skill`
    computes at the end of a write. It exists because that number is an
    OBSERVATION with a shelf life: the package under
    :func:`harness_skill_destination` is displaced by the next install of the
    same id, so a receipt still carrying the hash its own install measured is
    describing bytes that may no longer be there.

    ``None`` for a non-canonical id (there is no repo package to hash, and the
    install lane never produces a receipt row for one) and for a destination
    that does not exist — the same absence :data:`SKILL_HASH_NOT_INSTALLED`
    names, answered positively rather than guessed.
    """

    from agent.skill_utils import skill_package_content_hash

    if skill not in HARNESS_SKILLS:
        return None
    destination = harness_skill_destination(skill, hermes_home=hermes_home)
    if not destination.exists():
        return None
    return skill_package_content_hash(destination.parent, destination)


def harness_skill_hash_mismatches(skill_names: list[str], *, hermes_home: Path | None = None) -> list[str]:
    """The ids whose installed package DIFFERS from the repo package.

    A projection of :func:`harness_skill_hash_states`, unchanged in meaning: a
    skill that was never installed is still not a mismatch. Callers that need
    to tell that apart from a clean compare ask for the states, or for
    :func:`harness_skill_hash_absences` beside this list.

    ``prompt_observability._accessible_skills_context`` is a legitimate caller
    of the LIST alone, and the reason is worth recording rather than
    re-derived: a canonical id's ``source_kind`` is ``shared_core`` only for a
    package under :func:`harness_skill_destination`'s own directory
    (``skill_utils:770``), and a canonical id that resolves anywhere else is
    refused (``:898``). So on that surface "resolved" already entails
    "installed", the resolution status is checked first, and an absence reaches
    the HUD as ``missing`` / ``invalid_source`` before this list is consulted.
    """

    return [
        state.skill
        for state in harness_skill_hash_states(skill_names, hermes_home=hermes_home)
        if state.state == SKILL_HASH_MISMATCH
    ]


def harness_skill_hash_absences(skill_names: list[str], *, hermes_home: Path | None = None) -> list[str]:
    """The ids whose hash could not be read at all — one side of the pair is gone.

    The companion an empty mismatch list needs to be a positive claim.
    """

    return [
        state.skill
        for state in harness_skill_hash_states(skill_names, hermes_home=hermes_home)
        if not state.compared
    ]


def file_sha256(path: Path) -> str:
    # mtime-cached: skill-hash checks run per skill × per persona × per resolver
    # pass during a snapshot build, all hashing the same unchanged files.
    from .parse_cache import cached_file_sha256

    return cached_file_sha256(path)
