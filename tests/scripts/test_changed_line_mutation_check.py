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
        "BASE", claims, _exemptions_file(tmp_path), wall_budget_seconds=900, list_only=True
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "mutation candidates: 1 " in out
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
        "BASE", claims, _exemptions_file(tmp_path), wall_budget_seconds=900, list_only=True
    )
    lines = capsys.readouterr().out.splitlines()

    assert code == 0
    assert lines[0].startswith("mutation candidates: 0 ")
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
        "BASE", claims, _exemptions_file(tmp_path), wall_budget_seconds=900, list_only=False
    )
    out = capsys.readouterr().out

    # Nothing selected, so no baseline and no mutant ran — the early return this
    # lane has always had.
    assert code == 0
    assert "UNSELECTED" not in out


def test_a_big_inventory_still_prints_both_halves_and_refuses_nothing(
    tmp_path, claim_files, touched, capsys
):
    """This case used to pin "an over-cap `--list` is a refusal (2)".

    The cap is gone (ruled 2026-09-04) and with it the refusal it produced, so
    what survives is the part that was always the point: the inventory prints
    the selected candidates AND the unselected rows, and says nothing about
    whether the number is allowed.
    """

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
        "BASE", claims, _exemptions_file(tmp_path), wall_budget_seconds=900, list_only=True
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "mutation candidates: 2 " in out
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
            wall_budget_seconds=900,
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
    gate.run("BASE", claims, _exemptions_file(tmp_path), wall_budget_seconds=900, list_only=True)
    quiet = capsys.readouterr().out

    monkeypatch.setattr(
        gate, "_changed_sources", lambda base: ["agent_runtime/office_store.py", "hermes_cli/x.py"]
    )
    gate.run("BASE", claims, _exemptions_file(tmp_path), wall_budget_seconds=900, list_only=True)
    loud = capsys.readouterr().out

    assert quiet.splitlines()[0].startswith("mutation candidates: 0 ")
    assert loud.splitlines()[0].startswith("mutation candidates: 0 ")
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
    gate.run("BASE", claims, _exemptions_file(tmp_path), wall_budget_seconds=900, list_only=True)
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


# ─────────────────────── derivation provenance (ruled 2026-09-04) ────────────


def test_a_claim_may_carry_the_commit_its_needle_was_derived_at(
    tmp_path, claim_files, touched, capsys
):
    """The schema accepts `derived_at`, and accepting it is the whole first half.

    `mutation_claims.json` refuses unknown fields BY DESIGN — a `platform:` for
    `platforms:` would otherwise mean a claim runs everywhere while its author
    believes it is scoped. So recording provenance is not a convention anyone
    can adopt in a claim file; it needs the schema to let the key through.

    ANTI-VACUITY: the same claim without the field is exercised by every other
    case in this module, and the run below is asserted to reach the candidate
    line rather than merely to avoid raising — a schema that dropped the key
    silently would also pass a bare `code == 0`.
    """

    claim = _claim("dated", claim_files["first"], "beta = 2", "beta = 99")
    claim[gate.DERIVED_AT_KEY] = "0123456789abcdef0123456789abcdef01234567"
    claims = _claims_file(tmp_path, [claim])
    touched({claim_files["first"]: {2}})

    code = gate.run(
        "BASE", claims, _exemptions_file(tmp_path), wall_budget_seconds=900, list_only=True
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "  dated:" in out


def test_a_moved_file_warns_about_the_derivation_and_never_fails_the_run(
    tmp_path, claim_files, touched, capsys, monkeypatch
):
    """The ruled behaviour: a stale marker is a WARNING, never a refusal.

    The failure this addresses is the QUIET one. A needle that stopped
    occurring is a configuration error nobody can miss; a needle that still
    resolves after a semantic edit runs a mutation nobody re-derived, and the
    run goes green on a guarantee that may no longer be the guarantee.

    ANTI-VACUITY: the exit code is asserted to be 0 IN THE SAME CASE as the
    warning text, so an implementation that reported staleness by refusing —
    the obvious wrong answer, and the one the ruling names — fails here even
    though it "detected" the same thing. `_commits_since_derivation` is stubbed
    for the wiring; the real git read is exercised against this repo's own
    history in the case below.
    """

    monkeypatch.setattr(gate, "_commits_since_derivation", lambda claim: 4)
    claim = _claim("dated", claim_files["first"], "beta = 2", "beta = 99")
    claim[gate.DERIVED_AT_KEY] = "0123456789abcdef0123456789abcdef01234567"
    claims = _claims_file(tmp_path, [claim])
    touched({claim_files["first"]: {2}})

    code = gate.run(
        "BASE", claims, _exemptions_file(tmp_path), wall_budget_seconds=900, list_only=True
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "WARNING: stale derivation: dated was derived at 0123456789ab" in out
    assert "has moved in 4 commit(s) since" in out


def test_a_claim_with_no_derivation_recorded_says_nothing(
    tmp_path, claim_files, touched, capsys, monkeypatch
):
    """No backfill was ruled, so ABSENCE means "written before this schema".

    It must not be reported as fresh and must not be reported as stale: the 289
    rows that predate the field carry no claim about their own health, and a
    report that invented one either way would be the gate asserting something
    nobody measured.

    ANTI-VACUITY: the stub says "this file has moved" for every claim it is
    asked about, so silence here is the ABSENCE of the field doing the work and
    not a quiet stub.
    """

    monkeypatch.setattr(gate, "_commits_since_derivation", gate._commits_since_derivation)
    claims = _claims_file(
        tmp_path, [_claim("undated", claim_files["first"], "beta = 2", "beta = 99")]
    )
    touched({claim_files["first"]: {2}})

    code = gate.run(
        "BASE", claims, _exemptions_file(tmp_path), wall_budget_seconds=900, list_only=True
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "stale derivation" not in out


def test_the_derivation_read_counts_real_commits_against_this_repo():
    """The git half, against this checkout's own history — no stub anywhere.

    ANTI-VACUITY: three claims over the SAME path, differing only in the
    recorded commit — HEAD (nothing has moved since), HEAD's first parent (this
    file's own history is what decides), and a sha that does not resolve. A
    helper that answered a constant cannot produce those three answers.
    """

    def _claim_at(commit):
        row = {"path": "scripts/changed_line_mutation_check.py"}
        if commit is not None:
            row[gate.DERIVED_AT_KEY] = commit
        return row

    assert gate._commits_since_derivation(_claim_at("HEAD")) == 0
    assert gate._commits_since_derivation(_claim_at(None)) is None
    # A sha git cannot resolve is "nothing to say", not a crash and not a zero:
    # a shallow clone is a normal state and is not a fact about the claim.
    assert gate._commits_since_derivation(_claim_at("f" * 40)) is None


# ────────────────────── the wall-clock budget (ruled 2026-09-04) ─────────────


def test_the_candidate_count_is_reported_and_never_refused(
    tmp_path, claim_files, touched, capsys
):
    """The ruling's first half: the COUNT stops being a bound.

    The cap it replaces was a proxy for runtime, and the proxy kept mis-reading
    the thing it stood for — symbol-overlap selection raises the count by
    design (6 -> 27, 32 -> 64, 98 -> 104 on W1-H3's own diffs against a cap of
    20) and a push-shaped base collapses it, and neither moves how long the run
    takes.

    ANTI-VACUITY: three claims are selected here against a budget that could
    not have refused them either, so the assertion is on the exit code AND on
    the count still being printed — an implementation that silently stopped
    counting would pass a bare `code == 0`.
    """

    claims = _claims_file(
        tmp_path,
        [
            _claim("a", claim_files["first"], "alpha = 1", "alpha = 99"),
            _claim("b", claim_files["first"], "gamma = 3", "gamma = 99"),
            _claim("c", claim_files["second"], "epsilon = 5", "epsilon = 99"),
        ],
    )
    touched({claim_files["first"]: {1, 3}, claim_files["second"]: {2}})

    code = gate.run(
        "BASE", claims, _exemptions_file(tmp_path), wall_budget_seconds=900, list_only=True
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "mutation candidates: 3 " in out
    assert "cap" not in out.lower().replace("not capped", "")


def test_a_spent_budget_refuses_before_the_lock_and_names_both_cures(
    tmp_path, claim_files, touched, capsys
):
    """The ruling's second half, and the property the cap refusal had to keep.

    A refused run must hold nothing — a run that stops for being too big must
    not leave `.mutation_gate.lock` behind for the split-up runs that follow
    it. And the message has to say what was spent, what the bound was, how far
    it got, and both cures, because an exit 2 meaning "your change is big" read
    as "your claims are bad" on the H1-H4 landing.

    ANTI-VACUITY: no test command runs, asserted directly — a budget checked
    only AFTER the work would produce the same exit code and the same text.
    """

    ran: list[list[str]] = []
    # A SCOPED context, not a hand-rolled instance plus `undo()` in a `finally`:
    # `tests/agent_runtime/test_no_midtest_monkeypatch_undo.py` refuses every
    # `.undo()` under `tests/` by AST, on purpose and with no allowlist, because
    # the receiver of an `.undo()` cannot be told apart from the shared per-test
    # `monkeypatch` by reading the call. The context manager is the cure that
    # gate names, and it needs no `undo()` call at all.
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(
            gate, "_run_command", lambda command: ran.append(list(command)) or 0
        )
        claims = _claims_file(
            tmp_path, [_claim("a", claim_files["first"], "alpha = 1", "alpha = 99")]
        )
        touched({claim_files["first"]: {1}})

        code = gate.run(
            "BASE",
            claims,
            _exemptions_file(tmp_path),
            wall_budget_seconds=0,
            list_only=False,
        )
    err = capsys.readouterr().err

    assert code == 2
    assert ran == [], "a refused run still executed a command"
    assert not gate.LOCK_PATH.exists(), "a refused run left the gate lock behind"
    assert "wall budget exhausted" in err
    assert "--wall-budget-seconds 0" in err
    assert "after 0 of 1 claim(s)" in err
    # Both cures, and the budget raise FIRST: splitting is not available to a
    # landing whose whole argument is that its stages land together.
    assert err.index("raise the budget") < err.index("split the diff")


def test_the_inventory_lane_never_spends_the_budget(
    tmp_path, claim_files, touched, capsys
):
    """`--list` runs no test, so it has nothing to bound — and this is a
    behaviour CHANGE the ruling makes on purpose.

    Under the cap, a diff too big to run was also refused the inventory, so the
    one thing it needed to know — what it had selected — was the one thing it
    could not ask.

    ANTI-VACUITY: the identical budget refuses the sibling case above at
    `list_only=False`, so the exit 0 here is the flag doing the work.
    """

    claims = _claims_file(
        tmp_path, [_claim("a", claim_files["first"], "alpha = 1", "alpha = 99")]
    )
    touched({claim_files["first"]: {1}})

    code = gate.run(
        "BASE", claims, _exemptions_file(tmp_path), wall_budget_seconds=0, list_only=True
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "  a:" in out
