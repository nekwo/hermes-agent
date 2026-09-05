"""The unreachable-branch report's one judgement call, exercised on fixtures.

The report itself runs a real suite through the canonical runner; what is worth
pinning is not that, it is the split it makes of what coverage hands back.
``missing_branches`` mixes an arc off a line the suite never executed (the whole
function is cold — the finding is "nothing calls this") with an arc off a line
that ran every time and never once took that side. Only the second is the
too-weak-fixture class the report exists to surface, and on a narrow suite the
first outnumbers it ten to one.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import scripts.unreachable_branch_report as report  # noqa: E402
from scripts.unreachable_branch_report import _ranges, summarise  # noqa: E402


def _document(**files) -> dict:
    return {"files": files}


def _entry(*, statements, missing_lines, branches, missing_branches) -> dict:
    return {
        "summary": {
            "num_statements": statements,
            "missing_lines": len(missing_lines),
            "num_branches": branches,
        },
        "missing_lines": missing_lines,
        "missing_branches": missing_branches,
    }


def test_an_arc_off_a_cold_line_is_not_reported_as_a_one_armed_branch():
    """Line 40 never ran, so its two arcs say nothing about the predicate."""
    document = _document(
        **{
            "agent/charsheet/pipeline.py": _entry(
                statements=10,
                missing_lines=[40, 41, 42],
                branches=4,
                missing_branches=[[40, 41], [40, 42]],
            )
        }
    )

    record = summarise(document)[0]

    assert record["partialBranches"] == []
    assert record["coldBranches"] == 2
    assert record["missedLines"] == [40, 41, 42]


def test_an_arc_off_a_line_that_DID_run_is_the_finding():
    """The knob no fixture distinguishes: evaluated always, one arm never won."""
    document = _document(
        **{
            "agent/charsheet/palette.py": _entry(
                statements=10,
                missing_lines=[],
                branches=4,
                missing_branches=[[77, 80]],
            )
        }
    )

    record = summarise(document)[0]

    assert record["partialBranches"] == [[77, 80]]
    assert record["coldBranches"] == 0


def test_the_two_kinds_are_split_within_one_module():
    """A module carries both, and the mix is exactly what must not be summed."""
    document = _document(
        **{
            "agent/charsheet/draft.py": _entry(
                statements=20,
                missing_lines=[100, 101],
                branches=8,
                missing_branches=[[100, 101], [55, -1], [60, 70]],
            )
        }
    )

    record = summarise(document)[0]

    assert record["partialBranches"] == [[55, -1], [60, 70]]
    assert record["coldBranches"] == 1


def test_modules_are_ordered_by_the_finding_not_by_coldness():
    """A module with one one-armed branch outranks one that simply never ran.

    Sorting by total missing arcs would bury every real finding under whichever
    module the suite happens not to enter, which is the report's whole failure
    mode on a narrow ``--suite``.
    """
    document = _document(
        **{
            "agent/charsheet/cold.py": _entry(
                statements=90,
                missing_lines=list(range(1, 90)),
                branches=40,
                missing_branches=[[i, i + 1] for i in range(1, 40)],
            ),
            "agent/charsheet/warm.py": _entry(
                statements=10,
                missing_lines=[],
                branches=4,
                missing_branches=[[7, 9]],
            ),
        }
    )

    assert [record["module"] for record in summarise(document)] == [
        "agent/charsheet/warm.py",
        "agent/charsheet/cold.py",
    ]


def test_line_runs_collapse_and_singletons_survive():
    assert _ranges([3, 4, 5, 9]) == "3-5, 9"
    assert _ranges([]) == "-"
    assert _ranges([7, 7, 6]) == "6-7"


# ─────────────────── the runner's per-file cap, under tracing ───────────────────


def _captured_run(monkeypatch):
    """Run `_run_suite` against a stubbed subprocess and return its environment."""
    seen = {}

    class _Result:
        returncode = 0

    def fake_run(argv, cwd=None, env=None):
        seen["argv"] = argv
        seen["env"] = env
        return _Result()

    monkeypatch.setattr(report.shutil, "which", lambda _name: "/usr/bin/bash")
    monkeypatch.setattr(report.subprocess, "run", fake_run)
    return seen


def test_the_traced_run_raises_the_runners_per_file_cap(monkeypatch):
    """A traced file crosses the runner's 300s default and reads as "no tests ran".

    Which reds the suite, and a red suite makes every branch number in the
    report suspect — for a reason that has nothing to do with branches. Measured
    2026-09-05: `test_charsheet_pipeline.py`'s 110 tests took 310s traced and
    were SIGKILL'd mid-collection. The script already raises the PER-TEST
    timeout for exactly this hazard; this is the same hazard one level up.
    """
    seen = _captured_run(monkeypatch)

    report._run_suite(Path("rc"), ["tests/agent/test_charsheet_spec.py"], 4, 120, 1500)

    assert "--file-timeout=1500" in seen["argv"]
    assert "--timeout=120" in seen["argv"]


def test_the_per_file_cap_rides_the_argv_because_the_runner_drops_the_variable(monkeypatch):
    """`run_tests.sh` execs under `env -i` and forwards a NAMED set of variables.

    `HERMES_TEST_FILE_TIMEOUT` is not in it, so a cap set in the environment is
    dropped at that fence and the runner uses its 300s default anyway — measured
    2026-09-05 by a run that came back red with the variable set. Pinned as a
    test because the failure is silent: the variable is accepted, ignored, and
    the report reds for a reason that looks like a branch finding.
    """
    monkeypatch.setenv("HERMES_TEST_FILE_TIMEOUT", "42")
    seen = _captured_run(monkeypatch)

    report._run_suite(Path("rc"), ["tests/agent/test_charsheet_spec.py"], None, 120, 900)

    assert "--file-timeout=900" in seen["argv"]
    assert not any(arg.startswith("--file-timeout=42") for arg in seen["argv"])
