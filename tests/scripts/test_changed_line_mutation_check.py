"""`--list` accounts for the claims it did NOT select, instead of dropping them.

A claim whose `find` still resolves but whose line the diff never touched is
correctly not run. It is not correctly *invisible*: the inventory then shows the
same thing for "registered but not exercised by this diff" and for "never
written at all", and the reader has no way to tell which they are looking at.

Measured on the S4 landing, where
`s4-a-pre-plan-done-receipt-re-enters-the-skills-phase` anchors a line that
slice did not change and therefore appeared in no output anywhere — its
guarantee silently sat out every run.

ANTI-VACUITY throughout: the claims here point at files this test writes, and
the changed-line set is injected, so each case's selected/unselected split is
established by the test rather than by whatever the working tree happens to
contain on the day it runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import changed_line_mutation_check as gate


#: Captured at import, BEFORE `conftest.no_changed_sources_by_default` swaps
#: the module attribute for a stub. The census's own test has to call the real
#: thing, and reaching for `gate._changed_sources` inside a test would reach the
#: stub every time.
REAL_CHANGED_SOURCES = gate._changed_sources

FIRST = "alpha = 1\nbeta = 2\ngamma = 3\n"
SECOND = "delta = 4\nepsilon = 5\n"


@pytest.fixture
def claim_files(tmp_path: Path) -> dict[str, Path]:
    """Two real files, referenced by ABSOLUTE path.

    `REPO_ROOT / "<absolute>"` is that absolute path (pathlib drops the left
    operand), so a claim can name a file outside the checkout and every other
    check in the module — the target exists, the `find` occurs exactly once —
    runs unchanged against it.
    """

    first = tmp_path / "first.py"
    first.write_text(FIRST, encoding="utf-8")
    second = tmp_path / "second.py"
    second.write_text(SECOND, encoding="utf-8")
    return {"first": first, "second": second}


def _claims_file(tmp_path: Path, claims: list[dict]) -> Path:
    path = tmp_path / "claims.json"
    path.write_text(json.dumps({"claims": claims}), encoding="utf-8")
    return path


def _exemptions_file(tmp_path: Path) -> Path:
    path = tmp_path / "exemptions.yaml"
    path.write_text(json.dumps({"exemptions": []}), encoding="utf-8")
    return path


def _claim(identifier: str, target: Path, find: str, replace: str) -> dict:
    return {
        "id": identifier,
        "path": str(target),
        "symbol": "module",
        "operator": "flip-a-literal",
        "find": find,
        "replace": replace,
        "test": ["{python}", "-c", "raise SystemExit(1)"],
    }


@pytest.fixture
def touched(monkeypatch):
    """Inject the changed-line set, keyed by the file the claim names."""

    def _install(mapping: dict[Path, set[int]]):
        def _changed_lines(base: str, relative_path: str) -> set[int]:
            return set(mapping.get(Path(relative_path), set()))

        monkeypatch.setattr(gate, "_changed_lines", _changed_lines)

    return _install


def test_a_resolving_claim_the_diff_never_touched_is_listed_as_unselected(
    tmp_path, claim_files, touched, capsys
):
    """The pin. `--list` names both halves and says which is which.

    ANTI-VACUITY: the two claims are the SAME shape against the same kind of
    file, and only the injected changed-line set differs — so "selected" and
    "unselected" here cannot be an artefact of one claim being malformed.
    """

    claims = _claims_file(
        tmp_path,
        [
            _claim("touched-claim", claim_files["first"], "beta = 2", "beta = 99"),
            _claim("untouched-claim", claim_files["second"], "delta = 4", "delta = 99"),
        ],
    )
    touched({claim_files["first"]: {2}, claim_files["second"]: set()})

    code = gate.run(
        "BASE", claims, _exemptions_file(tmp_path), max_candidates=12, list_only=True
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "mutation candidates: 1 (cap 12)" in out
    assert "  touched-claim:" in out
    assert "UNSELECTED (0 changed lines): untouched-claim" in out
    # The selected claim is reported ONCE, as a candidate — never in both lists.
    assert "UNSELECTED (0 changed lines): touched-claim" not in out


def test_every_claim_unselected_still_reports_zero_candidates_first(
    tmp_path, claim_files, touched, capsys
):
    """CI's selector greps `^mutation candidates: 0 ` to decide whether to
    install the test environment at all. The new rows are additive and must not
    displace or precede that line, or a diff that selects nothing starts paying
    for a full `uv sync`.
    """

    claims = _claims_file(
        tmp_path,
        [
            _claim("a", claim_files["first"], "alpha = 1", "alpha = 99"),
            _claim("b", claim_files["second"], "epsilon = 5", "epsilon = 99"),
        ],
    )
    touched({})

    code = gate.run(
        "BASE", claims, _exemptions_file(tmp_path), max_candidates=12, list_only=True
    )
    lines = capsys.readouterr().out.splitlines()

    assert code == 0
    assert lines[0] == "mutation candidates: 0 (cap 12)"
    assert lines[1:] == [
        "changed production sources: 0 (0 carry no registered claim)",
        "UNSELECTED (0 changed lines): a",
        "UNSELECTED (0 changed lines): b",
    ]


def test_a_real_run_does_not_print_the_unselected_rows(
    tmp_path, claim_files, touched, capsys
):
    """The inventory is `--list`'s job. A run prints what it is about to mutate.

    ANTI-VACUITY: the sibling above proves these same two claims DO produce
    rows under `--list`, so this is the flag's effect and not an empty set.
    """

    claims = _claims_file(
        tmp_path,
        [
            _claim("a", claim_files["first"], "alpha = 1", "alpha = 99"),
            _claim("b", claim_files["second"], "epsilon = 5", "epsilon = 99"),
        ],
    )
    touched({})

    code = gate.run(
        "BASE", claims, _exemptions_file(tmp_path), max_candidates=12, list_only=False
    )
    out = capsys.readouterr().out

    # Nothing selected, so no baseline and no mutant ran — the early return this
    # lane has always had.
    assert code == 0
    assert "UNSELECTED" not in out


def test_the_cap_still_refuses_even_while_the_inventory_prints(
    tmp_path, claim_files, touched, capsys
):
    """Exit semantics are unchanged by the new rows: an over-cap `--list` is
    still a configuration refusal (2), not a report."""

    claims = _claims_file(
        tmp_path,
        [
            _claim("a", claim_files["first"], "alpha = 1", "alpha = 99"),
            _claim("b", claim_files["first"], "gamma = 3", "gamma = 99"),
            _claim("c", claim_files["second"], "epsilon = 5", "epsilon = 99"),
        ],
    )
    touched({claim_files["first"]: {1, 3}, claim_files["second"]: set()})

    code = gate.run(
        "BASE", claims, _exemptions_file(tmp_path), max_candidates=1, list_only=True
    )
    out = capsys.readouterr().out

    assert code == 2
    assert "mutation candidates: 2 (cap 1)" in out
    assert "UNSELECTED (0 changed lines): c" in out


def test_a_claim_whose_find_no_longer_resolves_is_still_a_configuration_error(
    tmp_path, claim_files, touched, capsys
):
    """The unselected lane is for claims that RESOLVE and miss the diff. A stale
    `find` is a different thing and must keep failing loudly — reporting it as
    "unselected" would turn every rotted claim into a quiet line of inventory.
    """

    claims = _claims_file(
        tmp_path,
        [_claim("stale", claim_files["first"], "omega = 0", "omega = 99")],
    )
    touched({})

    with pytest.raises(RuntimeError, match="mutation source not found"):
        gate.run(
            "BASE",
            claims,
            _exemptions_file(tmp_path),
            max_candidates=12,
            list_only=True,
        )


def test_a_zero_with_changed_sources_reads_differently_from_a_zero_with_none(
    tmp_path, claim_files, touched, monkeypatch, capsys
):
    """The row: a registration-gated gate that reports nothing when nobody
    registers is indistinguishable from one that had nothing to do.

    S5 landed with zero executable claims and printed exactly what an
    empty-by-right diff prints. Both zeros still say `mutation candidates: 0` —
    CI's grep depends on that — but the line under it now names the changed
    production sources and how many of them no claim anchors in.

    ANTI-VACUITY: the SAME claims and the SAME (empty) changed-line set produce
    both readings, so the difference is the census and not the selection.
    """

    claims = _claims_file(
        tmp_path, [_claim("a", claim_files["first"], "alpha = 1", "alpha = 99")]
    )
    touched({})

    monkeypatch.setattr(gate, "_changed_sources", lambda base: [])
    gate.run("BASE", claims, _exemptions_file(tmp_path), max_candidates=12, list_only=True)
    quiet = capsys.readouterr().out

    monkeypatch.setattr(
        gate, "_changed_sources", lambda base: ["agent_runtime/office_store.py", "hermes_cli/x.py"]
    )
    gate.run("BASE", claims, _exemptions_file(tmp_path), max_candidates=12, list_only=True)
    loud = capsys.readouterr().out

    assert quiet.splitlines()[0] == "mutation candidates: 0 (cap 12)"
    assert loud.splitlines()[0] == "mutation candidates: 0 (cap 12)"
    assert "changed production sources: 0 (0 carry no registered claim)" in quiet
    assert "NO CLAIM ANCHORS HERE" not in quiet
    assert "changed production sources: 2 (2 carry no registered claim)" in loud
    assert "  NO CLAIM ANCHORS HERE: agent_runtime/office_store.py" in loud
    assert "  NO CLAIM ANCHORS HERE: hermes_cli/x.py" in loud


def test_a_changed_source_a_claim_anchors_in_is_not_reported_unregistered(
    tmp_path, claim_files, touched, monkeypatch, capsys
):
    """Registered is registered whether or not THIS diff selected the claim.

    The census answers "did anybody ever write a guarantee for this file", so
    an unselected claim still accounts for its file — otherwise every run would
    list most of the tree and the rows would be filtered by eye, which is how a
    report stops being read.
    """

    claims = _claims_file(
        tmp_path, [_claim("a", claim_files["first"], "alpha = 1", "alpha = 99")]
    )
    touched({})
    anchored = str(claim_files["first"]).replace("\\", "/")

    monkeypatch.setattr(gate, "_changed_sources", lambda base: [anchored, "agent/other.py"])
    gate.run("BASE", claims, _exemptions_file(tmp_path), max_candidates=12, list_only=True)
    out = capsys.readouterr().out

    assert "changed production sources: 2 (1 carry no registered claim)" in out
    assert "  NO CLAIM ANCHORS HERE: agent/other.py" in out
    assert anchored not in out.split("NO CLAIM ANCHORS HERE", 1)[-1]


def test_the_census_reads_the_diff_and_leaves_tests_and_deletions_out(monkeypatch):
    """`_changed_sources` is a `git diff --name-only` over `*.py`, minus the
    roots no claim can anchor in and minus files the diff deleted.

    ANTI-VACUITY: the recorded argv is asserted alongside the filtering, so a
    version that returned the right list from the wrong question would fail.
    """

    recorded: list[list[str]] = []

    class _Completed:
        returncode = 0
        stdout = "agent_runtime/office_store.py\ntests/agent_runtime/test_x.py\nhermes_cli\y.py\n"
        stderr = ""

    def _run(argv, **kwargs):
        recorded.append(argv)
        return _Completed()

    monkeypatch.setattr(gate.subprocess, "run", _run)

    assert REAL_CHANGED_SOURCES("BASE") == [
        "agent_runtime/office_store.py",
        "hermes_cli/y.py",
    ]
    assert recorded == [
        ["git", "diff", "--name-only", "--diff-filter=d", "BASE", "--", "*.py"]
    ]
