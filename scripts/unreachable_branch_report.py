#!/usr/bin/env python3
"""Per-module unreachable-branch REPORT over the charsheet pipeline suite.

WHY THIS EXISTS
---------------
The too-weak-fixture defect recurred four times in one module, and three of the
four were a knob no reachable input could distinguish — a branch the suite
cannot express a case for. The class's standing answers (assert the trap before
the repair; delete a branch no fixture can reach; express a masking branch off
the knob it masks) were applied by hand twice in wave 14 and by nobody
automatically, because the cheap half of the question — *is any branch in this
module unreachable today* — could not be measured at all: ``coverage`` was not
in the canonical test environment.

It is now (``pyproject.toml``'s ``[dev]`` extra pins it, and
``scripts/check_test_env_drift.py`` lists it as test-only). This script is the
measurement, and the operator ruling that authorised it is explicit about its
shape: **a report, not a gate.** Nothing consumes its exit code — no push lane,
no workflow, no test. The expensive half the row also names (the sabotage
exercise over a declared knob list) is a program and is deliberately not here.

WHAT IT MEASURES, AND WHAT THAT MEANS
-------------------------------------
It runs a suite through ``scripts/run_tests.sh`` — the canonical runner, so the
pins, the hermetic environment and the per-file subprocess isolation are the
ones every other result on this repo was produced under — with branch tracing
switched on by the one variable that runner forwards for it
(``HERMES_TEST_COVERAGE_RC``). It then combines the per-file data and reports,
per module:

* statements the suite never executed, and
* **partial branches**: a conditional both of whose outcomes the module can
  reach in principle, where the suite only ever produced one of them.

A partial branch is where the class lives. It is exactly "a knob no fixture
distinguishes": the line is executed, the predicate is evaluated, and one arm is
dead as far as every case anyone has written is concerned.

**The caveat is load-bearing and is printed with every run.** "Unreachable" here
means *unreached by the suite this run traced*. A branch a DIFFERENT suite
covers reads as uncovered under a narrow ``--suite``, which is not the same
claim. Widen ``--suite`` before concluding anything about a branch, and read a
finding as a question ("what case would reach this?") rather than a verdict.

USAGE
-----
    python scripts/unreachable_branch_report.py
    python scripts/unreachable_branch_report.py --source agent/charsheet --source agent/pet
    python scripts/unreachable_branch_report.py --suite tests/agent/test_charsheet_pipeline.py
    python scripts/unreachable_branch_report.py --json report.json -j 4

Exit code: 0 when a report was produced, 1 when the traced suite itself was red
(the numbers are then suspect and the script says so), 2 on a setup problem.
Nothing reads it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The suite the row named. Globs, not a hand-listed set, so a charsheet test
#: file added tomorrow is measured without anyone remembering this constant.
DEFAULT_SUITE = ("tests/agent/test_charsheet_*.py",)

#: The modules the report is ABOUT. ``agent/charsheet`` is the pipeline package;
#: the atlas encoder it composes through lives in ``agent/pet/generate`` and is
#: deliberately not included by default — it is a different module's suite's
#: question, and a source root the suite barely enters reports as one big red
#: block that buries the real findings.
DEFAULT_SOURCES = ("agent/charsheet",)

#: Tracing costs wall time per test, and the repo's ``addopts`` passes
#: ``--timeout=30`` unconditionally. A traced run of an image-pipeline test can
#: cross that on a busy box and report a timeout as a failure, which would make
#: the whole measurement look red for a reason that is not about branches.
DEFAULT_TEST_TIMEOUT = 120

#: The runner's own PER-FILE cap, raised for the same reason one level up. Its
#: 300s default is generous for an untraced file and not for a traced one: the
#: charsheet pipeline's 110 tests cross it and come back as "no tests ran",
#: which reds the suite and makes the whole report suspect.
DEFAULT_FILE_TIMEOUT = 1500


def _coverage_config(destination: Path, data_file: Path, sources: list[str]) -> Path:
    """Write the config the traced subprocesses read.

    Everything except the file's own path lives here rather than in the
    environment: the runner's seam carries ONE variable and no policy, so
    changing what is traced never means changing the runner.
    """
    body = [
        "[run]",
        "branch = true",
        # Each per-file pytest is its own process; parallel mode gives each one
        # a suffixed data file, and `coverage combine` folds them together.
        "parallel = true",
        f"data_file = {data_file}",
        "source =",
    ]
    body += [f"    {source}" for source in sources]
    body += [
        "",
        "[report]",
        "show_missing = true",
    ]
    path = destination / "coveragerc"
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def _run_suite(
    rcfile: Path, suite: list[str], jobs: int | None, timeout: int, file_timeout: int
) -> int:
    """Run the suite through the canonical runner with tracing on."""
    bash = shutil.which("bash")
    if bash is None:
        raise FileNotFoundError(
            "no `bash` on PATH — this report runs the suite through "
            "scripts/run_tests.sh on purpose (the pins and the hermetic env are "
            "that script's, not this one's). Run it from Git Bash / a POSIX shell."
        )
    # The runner's PER-FILE cap is the same hazard as the per-test one above,
    # one level up, and it bites harder: 110 traced pipeline tests in one file
    # cross the 300s default, the runner SIGKILLs the process tree and reports
    # "no tests ran" — which reds the suite and makes every number below read as
    # suspect for a reason that is not about branches. Measured 2026-09-05,
    # twice.
    #
    # It rides the ARGV and not the environment, which is not a style choice:
    # `run_tests.sh` execs the runner under `env -i` and forwards exactly the
    # variables it names, and `HERMES_TEST_FILE_TIMEOUT` is not one of them. A
    # `--file-timeout` in the environment is silently dropped at that fence —
    # measured, by a run that came back red with the variable set.
    argv = [
        bash,
        "scripts/run_tests.sh",
        *suite,
        f"--timeout={timeout}",
        f"--file-timeout={file_timeout}",
    ]
    if jobs:
        argv += ["-j", str(jobs)]
    environment = dict(os.environ)
    environment["HERMES_TEST_COVERAGE_RC"] = str(rcfile)
    print(f"▶ tracing: {' '.join(argv)}", flush=True)
    return subprocess.run(argv, cwd=REPO_ROOT, env=environment).returncode


def _combine_and_dump(rcfile: Path, data_file: Path, out: Path) -> dict:
    """``coverage combine`` then ``coverage json`` — the measured document."""
    common = [sys.executable, "-m", "coverage"]
    combine = subprocess.run(
        [*common, "combine", f"--rcfile={rcfile}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if combine.returncode != 0:
        raise RuntimeError(
            "coverage combine failed — no per-file data was written, so the "
            f"suite ran untraced:\n{combine.stdout}\n{combine.stderr}"
        )
    if not data_file.exists():
        raise RuntimeError(f"coverage combine produced no data file at {data_file}")
    dump = subprocess.run(
        [*common, "json", f"--rcfile={rcfile}", "-o", str(out)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if dump.returncode != 0:
        raise RuntimeError(f"coverage json failed:\n{dump.stdout}\n{dump.stderr}")
    return json.loads(out.read_text(encoding="utf-8"))


def _ranges(numbers: list[int]) -> str:
    """``[3, 4, 5, 9]`` -> ``3-5, 9``. Line lists get long; runs read better."""
    if not numbers:
        return "-"
    ordered = sorted(set(numbers))
    spans: list[tuple[int, int]] = [(ordered[0], ordered[0])]
    for value in ordered[1:]:
        start, end = spans[-1]
        if value == end + 1:
            spans[-1] = (start, value)
        else:
            spans.append((value, value))
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in spans)


def summarise(document: dict) -> list[dict]:
    """One record per traced module, worst first.

    ``partialBranches`` are ``[line, destination]`` pairs, destination ``-1``
    meaning the function's exit — a predicate that never fell through.

    The split from ``coldBranches`` is the whole point of this report and is NOT
    what coverage hands back: its ``missing_branches`` mixes two very different
    facts. An arc off a line the suite never executed at all says nothing about
    the predicate — the whole function is cold, and the finding is "no test
    calls this". An arc off a line that DID execute is the class this report
    exists for: the condition was evaluated, over and over, and one arm never
    once won. Reporting them together buries the second under the first, which
    on a narrow suite outnumbers it ten to one.
    """
    records = []
    for path, entry in sorted(document.get("files", {}).items()):
        summary = entry.get("summary", {})
        cold_lines = set(entry.get("missing_lines", []))
        partial, cold = [], 0
        for pair in entry.get("missing_branches", []):
            if pair[0] in cold_lines:
                cold += 1
            else:
                partial.append(list(pair))
        records.append(
            {
                "module": path.replace("\\", "/"),
                "statements": summary.get("num_statements", 0),
                "missedStatements": summary.get("missing_lines", 0),
                "branches": summary.get("num_branches", 0),
                "partialBranches": partial,
                "coldBranches": cold,
                "missedLines": sorted(cold_lines),
            }
        )
    records.sort(key=lambda r: (-len(r["partialBranches"]), -r["missedStatements"], r["module"]))
    return records


def render(records: list[dict], suite: list[str], suite_code: int) -> None:
    print()
    print("── unreachable-branch report ───────────────────────────────────────")
    print(f"   suite : {' '.join(suite)}")
    print(f"   verdict of that suite: exit {suite_code}"
          + ("" if suite_code == 0 else "  ← RED: read every number below as suspect"))
    print()
    print(f"   {'module':<44} {'stmts':>6} {'cold':>6} {'brs':>5} {'one-armed':>10} {'cold brs':>9}")
    for record in records:
        print(
            f"   {record['module']:<44} {record['statements']:>6} "
            f"{record['missedStatements']:>6} {record['branches']:>5} "
            f"{len(record['partialBranches']):>10} {record['coldBranches']:>9}"
        )
    print()
    print("   ONE-ARMED is the finding: the line ran, the predicate was")
    print("   evaluated, and that arm never once won. COLD is a different fact —")
    print("   nothing in this suite calls the code at all.")
    print()
    for record in records:
        if not record["partialBranches"] and not record["missedLines"]:
            continue
        print(f"   {record['module']}")
        if record["partialBranches"]:
            arcs = ", ".join(
                f"{line}→{'exit' if dest < 0 else dest}"
                for line, dest in record["partialBranches"]
            )
            print(f"     one-armed : {arcs}")
        if record["missedLines"]:
            print(f"     cold      : lines {_ranges(record['missedLines'])}")
        print()
    print("   READ THIS AS A QUESTION, NOT A VERDICT. 'Unreachable' here means")
    print("   unreached by the suite named above. A branch another suite covers")
    print("   reads the same way, so widen --suite before concluding a branch is")
    print("   dead — and when it IS dead, the standing answer is to delete it")
    print("   rather than leave a knob no fixture can distinguish.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--suite",
        action="append",
        default=None,
        help=f"test path or glob to trace (repeatable; default: {' '.join(DEFAULT_SUITE)})",
    )
    parser.add_argument(
        "--source",
        action="append",
        default=None,
        help=f"module root to measure (repeatable; default: {' '.join(DEFAULT_SOURCES)})",
    )
    parser.add_argument("-j", "--jobs", type=int, default=None, help="runner parallelism")
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TEST_TIMEOUT,
        help=f"per-test timeout under tracing (default: {DEFAULT_TEST_TIMEOUT}s)",
    )
    parser.add_argument(
        "--file-timeout",
        type=int,
        default=DEFAULT_FILE_TIMEOUT,
        help=(
            "per-FILE wall-clock cap handed to the runner under tracing "
            f"(default: {DEFAULT_FILE_TIMEOUT}s)"
        ),
    )
    parser.add_argument("--json", type=Path, default=None, help="also write the records here")
    args = parser.parse_args(argv)

    sources = args.source or list(DEFAULT_SOURCES)
    patterns = args.suite or list(DEFAULT_SUITE)
    suite: list[str] = []
    for pattern in patterns:
        matches = sorted(str(p.relative_to(REPO_ROOT)) for p in REPO_ROOT.glob(pattern))
        suite.extend(matches or [pattern])
    if not suite:
        print("unreachable_branch_report: no test files matched", file=sys.stderr)
        return 2

    try:
        import coverage  # noqa: F401
    except ImportError:
        print(
            "unreachable_branch_report: coverage is not installed in this "
            "interpreter. It is pinned in pyproject.toml's [dev] extra; install "
            "it into the canonical test venv:\n"
            "  ~/.venvs/hermes-test/Scripts/python.exe -m pip install coverage",
            file=sys.stderr,
        )
        return 2

    with tempfile.TemporaryDirectory(prefix="hermes-branch-report-") as tmp:
        workspace = Path(tmp)
        data_file = workspace / "coverage-data"
        rcfile = _coverage_config(workspace, data_file, sources)
        suite_code = _run_suite(
            rcfile, suite, args.jobs, args.timeout, args.file_timeout
        )
        try:
            document = _combine_and_dump(rcfile, data_file, workspace / "coverage.json")
        except (RuntimeError, FileNotFoundError) as exc:
            print(f"unreachable_branch_report: {exc}", file=sys.stderr)
            return 2

    records = summarise(document)
    render(records, suite, suite_code)
    if args.json:
        args.json.write_text(
            json.dumps({"suite": suite, "suiteExit": suite_code, "modules": records}, indent=2),
            encoding="utf-8",
        )
        print(f"\n   records written to {args.json}")
    return 0 if suite_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
