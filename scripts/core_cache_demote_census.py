"""Read-only census of the core cache's demote receipts (BO-4).

    python scripts/core_cache_demote_census.py
    python scripts/core_cache_demote_census.py --log path/to/agent.log --last 200
    python scripts/core_cache_demote_census.py --json

Reads ``<HERMES_HOME>/logs/agent.log`` (or ``--log``) and writes NOTHING. Safe
to point at a live serve's log.

EXIT CODES, and why the quiet one is not zero
---------------------------------------------
* ``0`` — receipts were found and reported.
* ``1`` — **no demote receipt was parsed, or the log could not be read.** This
  is a FAILURE, not a pass, and it is the MCF-53 zero-scan lesson applied here:
  ``core_cache`` deliberately does not log the ``absent`` demote (the ordinary
  cold start would print a line on every build in every process), so "no demote
  line" cannot be read as "no demote". A census that exited 0 on an empty scan
  would be indistinguishable from one measuring a healthy runtime, which is
  exactly how a self-invalidating cache went unnoticed for months.
* ``2`` — at least one demote named a path the runtime itself writes. The
  finding this tool exists for.

The classification, the census rules it honours and the list of runtime-authored
names all live in :mod:`agent_runtime.core_cache_census`; this file is argv, IO
and an exit code.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

EXIT_OK = 0
EXIT_UNMEASURED = 1
EXIT_SELF_PERTURBATION = 2


def main(argv: list[str] | None = None) -> int:
    from agent_runtime.core_cache_census import (
        DEFAULT_WINDOW,
        VERDICT_NO_LINES,
        VERDICT_SELF_PERTURBATION,
        census_demotes,
        default_log_path,
        format_census,
    )

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--log",
        type=Path,
        default=None,
        help="log file to read (default: <HERMES_HOME>/logs/agent.log)",
    )
    parser.add_argument(
        "--last",
        type=int,
        default=DEFAULT_WINDOW,
        help=(
            "how many of the most recent demote RECEIPTS to census "
            f"(default {DEFAULT_WINDOW}); receipts, not log lines"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    path = args.log or default_log_path()
    try:
        # ``errors="replace"``: a log is a byte stream a crashed writer may have
        # torn mid-character, and refusing to census a runtime because one line
        # is malformed would be the tool failing at its own job.
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(
            f"core-cache demote census: cannot read {path}: "
            f"{type(exc).__name__}: {exc}\n"
            "  UNMEASURED, not clean — see this script's exit-code note.",
            file=sys.stderr,
        )
        return EXIT_UNMEASURED

    report = census_demotes(text.splitlines(), window=max(0, int(args.last)))
    if args.json:
        print(json.dumps({**report, "log_path": str(path)}, indent=2, sort_keys=True))
    else:
        print(format_census(report, source=str(path)))

    if report["verdict"] == VERDICT_NO_LINES:
        return EXIT_UNMEASURED
    if report["verdict"] == VERDICT_SELF_PERTURBATION:
        return EXIT_SELF_PERTURBATION
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
