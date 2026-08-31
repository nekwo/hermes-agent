"""`--claims-for` answers "which claims anchor where I am about to edit?".

W1-H3 slice 1. The measured gap, from the 2026-08-30 lifecycle merge: two
claims (`a4-doctor-prescribes-retire-for-an-orphan-retire-cannot-reach` and
`ax7-the-doctor-goes-back-to-prescribing-the-tombstoning-form`) both anchor
inside one function's remediation string; the handoff named only one, and the
second was caught by the selector's configuration error rather than by review.
The question "which claims anchor in the symbol I am about to rewrite?" was
answerable only by reading 113 registry rows by eye, so nobody read them.

Two properties this file pins beyond "it prints rows":

* it is a REPORT — a registry whose anchors have already stopped resolving
  still produces an answer, because mid-rewrite is exactly when the question
  gets asked and a pre-flight that dies on the first rotted row is useless
  precisely then;
* the three query spellings are three real questions (this file, this
  directory, this symbol), and the class spelling reaches its methods'
  claims — "I am about to rewrite `OfficeStore`" has an answer.

ANTI-VACUITY throughout: every case writes its own claims file and its own
target, so a match (and a miss) is established here rather than by whatever the
checked-in registry happens to hold on the day the suite runs.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts import changed_line_mutation_check as gate


HOLDERS = '''\
class OfficeStore:
    def upsert_actor(self):
        limit = 5
        return limit

    def scan_actors(self):
        budget = 7
        return budget


def free_function():
    ceiling = 9
    return ceiling
'''


def _claim(identifier: str, target: Path, symbol: str, find: str) -> dict:
    return {
        "id": identifier,
        "path": str(target),
        "symbol": symbol,
        "operator": "flip-a-literal",
        "find": find,
        "replace": find.replace("5", "99").replace("7", "99").replace("9", "99"),
        "test": ["{python}", "-c", "raise SystemExit(1)"],
    }


def _claims_file(tmp_path: Path, claims: list[dict]) -> Path:
    path = tmp_path / "claims.json"
    path.write_text(json.dumps({"claims": claims}), encoding="utf-8")
    return path


def _registry(tmp_path: Path) -> tuple[Path, Path]:
    target = tmp_path / "office_store.py"
    target.write_text(HOLDERS, encoding="utf-8")
    claims = _claims_file(
        tmp_path,
        [
            _claim("upsert-claim", target, "OfficeStore.upsert_actor", "        limit = 5"),
            _claim("scan-claim", target, "OfficeStore.scan_actors", "        budget = 7"),
            _claim("free-claim", target, "free_function", "    ceiling = 9"),
        ],
    )
    return claims, target


def test_a_symbol_query_names_the_claims_anchored_in_that_symbol(tmp_path, capsys):
    """The pin. One symbol, and only its own claim comes back.

    ANTI-VACUITY: three claims of the same shape live in one file, so "one row"
    here is the query's doing and not an empty registry.
    """

    claims, _ = _registry(tmp_path)

    code = gate._claims_for(claims, "upsert_actor")
    out = capsys.readouterr().out

    assert code == 0
    assert "claims anchored at upsert_actor: 1" in out
    assert "upsert-claim:" in out
    assert "scan-claim" not in out
    assert "free-claim" not in out


def test_a_class_query_reaches_every_claim_anchored_in_its_methods(tmp_path, capsys):
    """"I am about to rewrite `OfficeStore`" is a real question with a real
    answer: both methods' claims, and not the module-level function's."""

    claims, _ = _registry(tmp_path)

    gate._claims_for(claims, "OfficeStore")
    out = capsys.readouterr().out

    assert "claims anchored at OfficeStore: 2" in out
    assert "upsert-claim:" in out
    assert "scan-claim:" in out
    assert "free-claim" not in out


def test_a_path_query_names_every_claim_in_the_file(tmp_path, capsys):
    """The other axis: the whole file, whatever it is spelled with.

    The tail spelling is the one a person actually types — the point of a
    pre-flight nobody can be bothered to run is nothing.
    """

    claims, target = _registry(tmp_path)

    gate._claims_for(claims, str(target))
    assert "claims anchored at" in capsys.readouterr().out

    gate._claims_for(claims, "office_store.py")
    out = capsys.readouterr().out
    assert ": 3" in out.splitlines()[0]
    assert "upsert-claim:" in out and "scan-claim:" in out and "free-claim:" in out


def test_a_tail_query_is_segment_aligned(tmp_path, capsys):
    """`store.py` is not `office_store.py`. A substring match here would report
    claims for a file the caller is not touching, which is worse than none —
    the whole value is being able to trust the count."""

    claims, _ = _registry(tmp_path)

    gate._claims_for(claims, "store.py")
    assert "claims anchored at store.py: 0" in capsys.readouterr().out


def test_a_path_and_symbol_query_requires_both(tmp_path, capsys):
    """`path::symbol`, for the case where one bare name lives in two files."""

    claims, _ = _registry(tmp_path)

    gate._claims_for(claims, "office_store.py::scan_actors")
    out = capsys.readouterr().out
    assert ": 1" in out.splitlines()[0]
    assert "scan-claim:" in out

    gate._claims_for(claims, "other_module.py::scan_actors")
    assert ": 0" in capsys.readouterr().out.splitlines()[0]


def test_a_query_that_matches_nothing_says_so_rather_than_printing_a_bare_zero(
    tmp_path, capsys
):
    """A count of 0 and silence reads the same as a broken query. The row says
    what the zero MEANS, because acting on it means starting a rewrite."""

    claims, _ = _registry(tmp_path)

    gate._claims_for(claims, "a_symbol_nobody_has")
    out = capsys.readouterr().out

    assert "claims anchored at a_symbol_nobody_has: 0" in out
    assert "no registered guarantee" in out


def test_a_claim_whose_anchor_no_longer_resolves_is_reported_not_raised(
    tmp_path, capsys
):
    """The property that makes this usable mid-rewrite.

    A run of the GATE would refuse here — a stale anchor is a configuration
    error and must stay one. The pre-flight is the opposite lane: it is asked
    while the file is half-rewritten, so it names the rotted row and keeps
    listing the rest.

    ANTI-VACUITY: the sibling claim in the same file still resolves and still
    prints its line numbers, so "reported" is not "everything degraded".
    """

    target = tmp_path / "office_store.py"
    target.write_text(HOLDERS, encoding="utf-8")
    claims = _claims_file(
        tmp_path,
        [
            _claim("rotted", target, "OfficeStore.upsert_actor", "        limit = 404"),
            _claim("intact", target, "free_function", "    ceiling = 9"),
        ],
    )

    code = gate._claims_for(claims, "office_store.py")
    out = capsys.readouterr().out

    assert code == 0
    assert "rotted:" in out
    assert "ANCHOR DOES NOT RESOLVE TODAY" in out
    assert "mutation source not found" in out
    assert "intact:" in out
    assert "lines 12-12" in out


def test_the_flag_runs_without_a_base_and_never_reaches_the_diff(tmp_path, capsys):
    """The CLI half: `--base` is what makes this a gate, and the pre-flight has
    no base to name. `main` must reach the report without git ever being asked
    for a diff — otherwise the command cannot be run before the rewrite."""

    claims, _ = _registry(tmp_path)

    code = gate.main(["--claims-for", "free_function", "--claims", str(claims)])
    out = capsys.readouterr().out

    assert code == 0
    assert "claims anchored at free_function: 1" in out
    assert "free-claim:" in out
