#!/usr/bin/env python3
"""The join gate: what the runtime READS must be what this repo SAYS.

A canonical shared skill has two copies and only one of them is ever executed.
The repo copy under ``docs/agent-runtime-harness/harness-skills/<id>/SKILL.md``
is the source; the copy under ``<hermes root>/shared/skills/<id>/SKILL.md`` is
what a chat turn actually loads, because ``agent.skill_utils`` refuses any
candidate for an id in ``CANONICAL_SHARED_SKILL_IDS`` whose
``source_kind`` is not ``shared_core``. Nothing copies one to the other on
commit, so editing the repo copy changes the documentation and changes nothing
an agent sees.

That is not hypothetical. On 2026-08-24 the ``harness-charsheet-authoring``
package landed, was installed, and was then edited twice more in the same hour
(``5504706978``, ``6ca0622bec``). The installed copy stayed at the first
version — 14457 B against the repo's 14906 B — and the live gate turn's
``used_skills`` row carried the STALE package's content hash. The two edits it
missed were the ones teaching an agent not to declare an over-budget crop, so
the drift did not merely age the text: it re-armed the exact regression
``pipeline.MAX_CARD_PIXELS`` had just been added to prevent.

Tests read the repo copy, which is why every test A0 shipped was green while
this was true. A guarantee about what the runtime reads has to be pinned where
that copy lives — on the machine, not in the tree — so this runs from the
pre-push hook (``.githooks/pre-push``), where a repo-side edit and a machine
meet.

Two modes:

* default — **repair, then verify.** Install every canonical package from the
  repo, then assert nothing diverges. This is what the hook runs: the drift is
  machine state, not a defect in the tree, and the correct response to it is to
  fix it. The verify afterwards is what makes it a gate rather than a wish —
  an install that silently did not take fails the push.
* ``--check`` — **verify only, write nothing.** For proving the gate fires, and
  for any caller that wants to know without changing anything.

Exit 0 clean, 1 on divergence, 2 on a usage/environment error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _canonical_ids() -> list[str]:
    from hermes_constants import CANONICAL_SHARED_SKILL_IDS

    return sorted(CANONICAL_SHARED_SKILL_IDS)


def _report(skills: list[str]) -> list[str]:
    """One line per canonical skill: repo size/hash vs installed size/hash."""
    from agent.skill_utils import skill_package_content_hash
    from agent_runtime.skill_install import (
        harness_skill_destination,
        harness_skill_source,
    )

    lines: list[str] = []
    for skill in skills:
        source = harness_skill_source(skill)
        destination = harness_skill_destination(skill)
        if not source.exists():
            lines.append(f"  {skill}: MISSING SOURCE {source}")
            continue
        source_hash = skill_package_content_hash(source.parent, source)
        if not destination.exists():
            lines.append(
                f"  {skill}: repo {source.stat().st_size} B {source_hash[:16]} "
                f"— NOT INSTALLED at {destination}"
            )
            continue
        installed_hash = skill_package_content_hash(destination.parent, destination)
        state = "ok" if installed_hash == source_hash else "DIVERGED"
        lines.append(
            f"  {skill}: repo {source.stat().st_size} B {source_hash[:16]} | "
            f"installed {destination.stat().st_size} B {installed_hash[:16]} | {state}"
        )
    return lines


def _uninstalled(skills: list[str]) -> list[str]:
    """Canonical ids with no installed package at all.

    ``harness_skill_hash_mismatches`` skips a destination that does not exist —
    correctly, because "not installed" is not "installed wrong". For a gate the
    distinction still matters: an id nobody ever installed resolves to nothing
    and the persona that requires it reports ``missing_skill``, so name it
    separately rather than letting it pass as clean.
    """
    from agent_runtime.skill_install import harness_skill_destination

    return [skill for skill in skills if not harness_skill_destination(skill).exists()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify only; never write. Default is install-then-verify.",
    )
    args = parser.parse_args(argv)

    try:
        from agent_runtime.skill_install import (
            get_shared_skills_dir,
            harness_skill_hash_mismatches,
            harness_skill_source_root,
            install_harness_skills,
        )
    except Exception as exc:  # pragma: no cover - environment failure, reported not swallowed
        print(f"harness-skill-install: cannot import the installer: {exc}", file=sys.stderr)
        return 2

    skills = _canonical_ids()
    print(f"harness-skill-install: {len(skills)} canonical shared skill(s)")
    print(f"  source    {harness_skill_source_root()}")
    print(f"  installed {get_shared_skills_dir()}")

    if not args.check:
        refreshed = [item.skill for item in install_harness_skills(skills=skills) if item.changed]
        if refreshed:
            print(f"  refreshed from the repo: {', '.join(refreshed)}")

    mismatches = harness_skill_hash_mismatches(skills)
    missing = _uninstalled(skills)
    if mismatches or missing:
        print("\n".join(_report(skills)))
        if mismatches:
            print(
                "harness-skill-install: FAILED — the installed package differs from this "
                f"repo for: {', '.join(mismatches)}",
                file=sys.stderr,
            )
        if missing:
            print(
                "harness-skill-install: FAILED — never installed on this machine: "
                f"{', '.join(missing)}",
                file=sys.stderr,
            )
        print(
            "harness-skill-install: a chat turn loads the INSTALLED copy, never the repo "
            "one. Run `hermes harness install-harness-skills` (or this script without "
            "--check) and push again.",
            file=sys.stderr,
        )
        return 1

    print("harness-skill-install: ok — every canonical package installed and current")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
