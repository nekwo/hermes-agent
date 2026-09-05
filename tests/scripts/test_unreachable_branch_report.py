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
