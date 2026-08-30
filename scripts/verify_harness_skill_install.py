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
``pipeline.MAX_CONSOLE_CARD_PIXELS`` (then spelled
``MAX_CARD_PIXELS``) had just been added to prevent.

Tests read the repo copy, which is why every test A0 shipped was green while
this was true. A guarantee about what the runtime reads has to be pinned where
that copy lives — on the machine, not in the tree.

WHO CALLS THIS, and why it is no longer a push gate. Until 2026-08-30 it ran
from ``.githooks/pre-push``. That is the moment the PRODUCER publishes, and it
repaired the machine whose repo copy was already the newest thing in the realm.
The drift is acquired by a CONSUMER, so the callers are now the two moments a
consumer acquires it: ``.githooks/post-merge`` (this script, after a pull) and
``harness serve`` boot (``hermes_cli/harness_parts/serve.py``
``install_harness_skills_at_boot``, which runs the installers directly rather
than shelling out to this file — a boot has an explicitly pinned home and needs
none of the resolution ladder below). Neither can block, and neither has to:
a boot that finds drift repairs it, and the next boot retries for free.

Two modes:

* default — **repair, then verify.** Install every canonical package from the
  repo, then assert nothing diverges. This is what the hook runs: the drift is
  machine state, not a defect in the tree, and the correct response to it is to
  fix it. The verify afterwards is what turns a repair into a REPORT rather than
  a wish — an install that silently did not take exits nonzero and says which
  package.
* ``--check`` — **verify only, write nothing.** For proving the gate fires, and
  for any caller that wants to know without changing anything.

WHICH ROOT — and why this refuses to guess one
----------------------------------------------

The gate is only a gate against the tree a turn actually reads. That tree is
``get_shared_skills_dir()`` → ``get_default_hermes_root()``, which with no
``HERMES_HOME`` falls through to the platform default — on Windows
``%LOCALAPPDATA%\\hermes``. A git hook inherits the pushing shell's
environment, and that shell usually has no ``HERMES_HOME`` at all, so the
first version of this script targeted the platform default from any ordinary
push. That is not an empty directory: it is the real, populated SHADOW runtime
the launcher's own spawn site names as the hazard it pins ``HERMES_HOME``
against (``hermes_process_identity.dart``, "where a %LOCALAPPDATA% shadow root
is reachable"), and on this machine it held six canonical packages whose bytes
were not this repo's. Repair mode would have overwritten all six, reported
``ok — every canonical package installed and current`` about the copy it had
just written, and never once looked at the root every persona in the live
roster reads. A gate that passes on the wrong artifact is worse than no gate.

So the root is EXPLICIT here, never ambient. :func:`resolve_gate_hermes_home`
answers from, in order:

1. ``ETERNIA_HERMES_HOME`` — the launcher's own pin, first for the same reason
   and in the same order as ``hermes_cli_io.dart``'s
   ``hermesProcessEnvironment`` (``ETERNIA_HERMES_HOME ?? HERMES_HOME``), so
   the gate targets the home the serve child was spawned with.
2. ``HERMES_HOME``.
3. ``agent_runtime.head_home`` in this machine's runtime config — the machine
   root anchor ``harness serve`` publishes at boot
   (``agent_runtime/root_anchor.py``) and
   ``chat_session_scope.declared_chat_head_home`` owns the reading of. A
   DECLARATION is not a guess: it is the one durable record of "this
   operator's runtime root is elsewhere", written by the process that provably
   knew. Read through that function rather than a second parser of the same
   key, and it already refuses a declaration naming a home that no longer
   exists.
4. Nothing — :class:`HermesHomeUnresolved`, exit 2. Not the platform default.

Failing loud is the hook's own stated principle (it already refuses to skip on
a missing interpreter rather than pass quietly), and printing the resolved root
would not have been enough: nobody reads a passing hook's output.

(The serve-boot caller does not come through this ladder at all. It is inside a
process whose home was pinned at spawn and already resolved, which is precisely
the ambiguity the ladder exists to refuse — see the plan's note on why boot is
the strongest of the trigger sites.)

Exit 0 clean, 1 on divergence, 2 on a usage/environment error.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class HermesHomeUnresolved(RuntimeError):
    """No explicit pin and no machine declaration named a hermes home.

    Raised instead of letting ``get_default_hermes_root()`` fall through to the
    platform default. See the module docstring: on Windows that default is a
    populated shadow runtime, and repairing into it destroys packages while
    verifying an artifact no persona reads.
    """


def resolve_gate_hermes_home(env: dict[str, str] | None = None) -> tuple[str, str]:
    """Return ``(home, rung)`` — the EXPLICIT hermes home this gate targets.

    ``rung`` names where the answer came from and is printed, so a passing run
    still says which tree it checked. The ladder is documented in the module
    docstring; the one thing it never does is guess.
    """

    source = os.environ if env is None else env
    for name in ("ETERNIA_HERMES_HOME", "HERMES_HOME"):
        value = str(source.get(name) or "").strip()
        if value:
            return value, f"env {name}"

    declared = None
    try:
        from agent_runtime.chat_session_scope import declared_chat_head_home

        declared = declared_chat_head_home()
    except Exception:  # pragma: no cover - defensive; a missing declaration is the same answer
        declared = None
    if declared is not None:
        return str(declared), "config agent_runtime.head_home"

    from hermes_constants import _get_platform_default_hermes_home

    _target = (
        f"{_get_platform_default_hermes_home()}{os.sep}shared{os.sep}skills"
    )
    raise HermesHomeUnresolved(
        "REFUSING to guess the hermes home. Neither ETERNIA_HERMES_HOME nor "
        "HERMES_HOME is set, and this machine's runtime config declares no "
        "agent_runtime.head_home. The installed skill root is derived from that "
        f"home, so with none of them set this would target {_target}"
        f" — the platform default, {_describe_target(_target)}, not the root "
        "your personas read. Repairing there would overwrite packages that "
        "are not this repo's and then verify the copy it had just written. "
        "Export the home the launcher spawns serve with "
        "(HERMES_HOME=<hermes root>/profiles/base) and run this again."
    )


def _describe_target(path: str) -> str:
    """Say what is actually AT the refused path, rather than asserting it.

    The first wording of this refusal claimed the platform default "is a
    populated SHADOW runtime" unconditionally. True on a machine that has run
    other hermes builds — it held six foreign canonical packages here — and
    false on the fresh clone that is this message's most likely audience, where
    it is empty or absent. A refusal that tells a new dev to go find a shadow
    runtime they do not have sends them hunting; stat it and report which case
    they are in.
    """
    if not os.path.isdir(path):
        return "which does not exist on this machine"
    try:
        packages = [
            name
            for name in os.listdir(path)
            if os.path.isfile(os.path.join(path, name, "SKILL.md"))
        ]
    except OSError:
        return "which could not be read"
    if not packages:
        return (
            "empty here, but a populated SHADOW runtime on any machine that "
            "has run another hermes build"
        )
    return (
        f"a populated SHADOW runtime here holding {len(packages)} package(s) "
        "whose bytes are not this repo's"
    )


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


def _by_state(skills: list[str]) -> dict[str, list[str]]:
    """Every canonical id bucketed by what the hash read established.

    This used to be a ``harness_skill_hash_mismatches`` call beside a hand-rolled
    ``harness_skill_destination(...).exists()`` walk, because the mismatch list
    dropped the absent case on the floor and the gate had to re-derive it. The
    detector states it now, so there is one authority and the third bucket —
    a canonical id with no package IN THE REPO, which used to pass this gate in
    silence whenever something was installed under its name — is named too.
    """
    from agent_runtime.skill_install import harness_skill_hash_states

    buckets: dict[str, list[str]] = {}
    for row in harness_skill_hash_states(skills):
        buckets.setdefault(row.state, []).append(row.skill)
    return buckets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify only; never write. Default is install-then-verify.",
    )
    args = parser.parse_args(argv)

    # BEFORE any resolution: pin the home so every downstream call — the
    # installer's destination, the mismatch check, the report — resolves the one
    # tree through the same chokepoint a chat turn does, instead of each
    # re-deriving it from an environment that may name nothing.
    try:
        home, rung = resolve_gate_hermes_home()
    except HermesHomeUnresolved as exc:
        print(f"harness-skill-install: {exc}", file=sys.stderr)
        return 2
    os.environ["HERMES_HOME"] = home

    try:
        from agent_runtime.skill_install import (
            SKILL_HASH_MISMATCH,
            SKILL_HASH_NO_SOURCE,
            SKILL_HASH_NOT_INSTALLED,
            get_shared_skills_dir,
            harness_skill_source_root,
            install_harness_skills,
        )
    except Exception as exc:  # pragma: no cover - environment failure, reported not swallowed
        print(f"harness-skill-install: cannot import the installer: {exc}", file=sys.stderr)
        return 2

    skills = _canonical_ids()
    print(f"harness-skill-install: {len(skills)} canonical shared skill(s)")
    print(f"  home      {home}  (via {rung})")
    print(f"  source    {harness_skill_source_root()}")
    print(f"  installed {get_shared_skills_dir()}")

    if not args.check:
        refreshed = [item.skill for item in install_harness_skills(skills=skills) if item.changed]
        if refreshed:
            print(f"  refreshed from the repo: {', '.join(refreshed)}")

    buckets = _by_state(skills)
    mismatches = buckets.get(SKILL_HASH_MISMATCH, [])
    missing = buckets.get(SKILL_HASH_NOT_INSTALLED, [])
    sourceless = buckets.get(SKILL_HASH_NO_SOURCE, [])
    if mismatches or missing or sourceless:
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
        if sourceless:
            print(
                "harness-skill-install: FAILED — canonical id with no package in this "
                f"repo: {', '.join(sourceless)}",
                file=sys.stderr,
            )
        print(
            "harness-skill-install: a chat turn loads the INSTALLED copy, never the repo "
            "one. Run `hermes harness install-harness-skills` (or this script without "
            "--check), or restart `harness serve`, which installs at boot.",
            file=sys.stderr,
        )
        return 1

    print("harness-skill-install: ok — every canonical package installed and current")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
