#!/usr/bin/env python3
"""Diff the canonical shared test venv's pins against the live install's.

Row 17 (mission-control-queue.md, 2026-09-03): the canonical test env at
``~/.venvs/hermes-test`` (``$HERMES_TEST_VENV``) was built BY HAND from a one-time freeze of
the live install's venv (see ``docs/agent-runtime-harness/planned/
canonical-test-env-field-notes-2026-09-03.md`` §1 for the recipe). Nothing
compares the two after that; when the live install's pins move, the test
environment silently stops being representative.

This is the "obvious follow-up... not built" the field notes named as an open
question. It does exactly one thing: freeze both venvs and report which
distributions differ, excluding the test-only extras that are SUPPOSED to
differ (pytest and friends — see ``TEST_ONLY_DISTRIBUTIONS`` below, which
must stay in sync with ``req-test-only.txt``'s intent in the field notes).

Usage
-----
    python scripts/check_test_env_drift.py
    python scripts/check_test_env_drift.py --live PATH --test PATH

Exit code is 0 when the two agree (modulo the test-only allowlist), 1 when
they drift, 2 on a setup problem (a venv path does not exist). This is a
REPORT, run by hand by whoever updates the live install — not a gate wired
into any push lane.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

#: Distributions the canonical test venv is EXPECTED to carry beyond the live
#: freeze (the `[dev]` extra + pytest's own transitive deps). A name landing
#: here is not drift; a VERSION difference on one of these still is not
#: reported, since the live install has no opinion on it at all.
TEST_ONLY_DISTRIBUTIONS = {
    "pytest",
    "pytest-asyncio",
    "pytest-timeout",
    "setuptools",
    "ruff",
    "ty",
    "iniconfig",
    "pluggy",
}


def _venv_python(venv_dir: Path) -> Path:
    windows_candidate = venv_dir / "Scripts" / "python.exe"
    if windows_candidate.exists():
        return windows_candidate
    return venv_dir / "bin" / "python"


def freeze(venv_dir: Path) -> list[str]:
    """Run ``pip freeze`` inside ``venv_dir`` and return the raw lines."""
    python = _venv_python(venv_dir)
    if not python.exists():
        raise FileNotFoundError(f"no python found under venv {venv_dir} ({python})")
    result = subprocess.run(
        [str(python), "-m", "pip", "freeze"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def parse_freeze(lines: list[str]) -> dict[str, str]:
    """Parse ``pip freeze`` lines into ``{distribution_lower: version}``.

    Editable installs (``-e ...``) and anything else without a plain
    ``name==version`` shape are skipped — an editable checkout has no
    meaningful "version" to compare (see the field notes §1 "one line dropped
    from the freeze, on purpose").
    """
    pins: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("-e ") or line.startswith("#"):
            continue
        if "==" not in line:
            continue
        name, _, version = line.partition("==")
        pins[name.strip().lower()] = version.strip()
    return pins


def diff_pins(
    live_pins: dict[str, str],
    test_pins: dict[str, str],
    *,
    test_only: frozenset[str] = frozenset(TEST_ONLY_DISTRIBUTIONS),
) -> list[str]:
    """Return one human-readable line per drift, sorted for stable output.

    Three shapes are reported:
    - a distribution pinned in the live venv at one version and the test venv
      at a different one (drift the recipe was supposed to prevent);
    - a distribution the live venv carries that the test venv is missing
      entirely (the environments have diverged, not just moved together);
    - a distribution the test venv carries that is in NEITHER the live venv
      NOR the test-only allowlist (an untracked addition to the shared venv).

    A distribution missing from the live venv but present in the test venv
    IS reported unless it is in ``test_only`` — that is the mechanism that
    keeps the allowlist honest instead of silently swallowing new additions.
    """
    lines: list[str] = []
    for name in sorted(set(live_pins) | set(test_pins)):
        live_version = live_pins.get(name)
        test_version = test_pins.get(name)
        if live_version == test_version:
            continue
        if live_version is None:
            if name in test_only:
                continue
            lines.append(
                f"UNEXPECTED: {name}=={test_version} in the test venv, "
                "absent from the live venv and not in the test-only allowlist"
            )
        elif test_version is None:
            lines.append(f"MISSING: {name}=={live_version} in the live venv, absent from the test venv")
        else:
            lines.append(f"DRIFT: {name} live=={live_version} test=={test_version}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        type=Path,
        default=Path.home() / ".hermes" / "venvs" / "hermes-agent",
        help="the live install's venv (default: ~/.hermes/venvs/hermes-agent)",
    )
    parser.add_argument(
        "--test",
        type=Path,
        default=Path.home() / ".venvs" / "hermes-test",
        help="the canonical shared test venv (default: ~/.venvs/hermes-test)",
    )
    args = parser.parse_args(argv)

    try:
        live_pins = parse_freeze(freeze(args.live))
        test_pins = parse_freeze(freeze(args.test))
    except FileNotFoundError as exc:
        print(f"check_test_env_drift: {exc}", file=sys.stderr)
        return 2

    drift = diff_pins(live_pins, test_pins)
    if not drift:
        print("check_test_env_drift: no drift — the test venv still matches the live install's pins")
        return 0

    print(f"check_test_env_drift: {len(drift)} difference(s) between live and test venvs:")
    for line in drift:
        print(f"  {line}")
    print()
    print(
        "Rebuild the recipe in "
        "docs/agent-runtime-harness/planned/canonical-test-env-field-notes-2026-09-03.md §1 "
        "if this is real drift, not an intentional test-only addition."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
