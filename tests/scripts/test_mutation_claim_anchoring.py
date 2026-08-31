"""A mutation claim is anchored to its SYMBOL, and survives the block moving.

H-H14. Until 2026-08-30 a claim's `find` was matched against the whole file and
had to occur there exactly once, and the `symbol` field was decorative — nothing
ever asked whether the named symbol existed. Two consequences, both measured on
the C-slice, which needed three manual re-anchors inside one nine-commit slice:

* a block that moved or was DEDENTED (an extraction out of a nested `try` is two
  levels) no longer matched its registered bytes, so a live guarantee failed
  configuration and had to be retyped by hand;
* `r1-create-stops-fencing-the-supplied-placement` named `_parse_request`, a
  function `agent_create.py` has not had for months, and the gate reported
  nothing — it was mutating a line inside `normalize_agent_create` under a label
  that pointed somewhere else.

ANTI-VACUITY throughout: every case writes its own target file, so what is
anchored (and what is not) is established here rather than by whatever the
checkout happens to hold on the day the suite runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import changed_line_mutation_check as gate


TWO_HOLDERS = '''\
def decoy():
    limit = 5
    return limit


def target():
    limit = 5
    return limit
'''

NESTED = '''\
def target():
    try:
        if ready:
            record(outcome)
    except OSError:
        pass
'''

DEDENTED = '''\
def target():
    record(outcome)
'''


def _claim(identifier: str, target: Path, symbol: str, find: str, replace: str, test: list[str]) -> dict:
    return {
        "id": identifier,
        "path": str(target),
        "symbol": symbol,
        "operator": "flip-a-literal",
        "find": find,
        "replace": replace,
        "test": test,
    }


def _claims_file(tmp_path: Path, claims: list[dict]) -> Path:
    path = tmp_path / "claims.json"
    path.write_text(json.dumps({"claims": claims}), encoding="utf-8")
    return path


def _exemptions_file(tmp_path: Path) -> Path:
    path = tmp_path / "exemptions.yaml"
    path.write_text(json.dumps({"exemptions": []}), encoding="utf-8")
    return path


@pytest.fixture
def touched(monkeypatch):
    """Inject the changed-line set, keyed by the file the claim names."""

    def _install(mapping: dict[Path, set[int]]):
        def _changed_lines(base: str, relative_path: str) -> set[int]:
            return set(mapping.get(Path(relative_path), set()))

        monkeypatch.setattr(gate, "_changed_lines", _changed_lines)

    return _install


def _line_probe(target: Path, index: int, expected: str) -> list[str]:
    """A claim command that passes only while one NAMED line still reads `expected`.

    The mutation lane's own instrument: it distinguishes "the anchored line was
    rewritten" from "some identical line elsewhere was", which is exactly the
    difference a first-occurrence `str.replace` cannot express.
    """

    code = (
        "import sys, pathlib;"
        f"lines = pathlib.Path({str(target)!r}).read_text().splitlines();"
        f"sys.exit(0 if lines[{index}] == {expected!r} else 1)"
    )
    return ["{python}", "-c", code]


def test_the_anchor_is_scoped_to_the_symbol_the_claim_names(tmp_path):
    """Two identical lines in one file, and the claim names which one it means.

    Under whole-file matching this claim was a configuration error ("must occur
    exactly once") purely because an unrelated function spells the same line.
    """

    target = tmp_path / "holders.py"
    target.write_text(TWO_HOLDERS, encoding="utf-8")
    claim = _claim("scoped", target, "target", "    limit = 5", "    limit = 99", [])

    anchor = gate._anchor_claim(TWO_HOLDERS, claim)

    # Line 7 is `target`'s copy; line 2 is `decoy`'s.
    assert anchor.lines == {7}
    assert anchor.shift == 0


def test_the_decoy_copy_is_anchored_when_the_claim_names_the_decoy(tmp_path):
    """ANTI-VACUITY for the case above: the same file, the same `find`, the
    other symbol — and the anchor moves. So {7} was the symbol's doing and not
    an artefact of where the text happens to sit."""

    target = tmp_path / "holders.py"
    target.write_text(TWO_HOLDERS, encoding="utf-8")
    claim = _claim("scoped", target, "decoy", "    limit = 5", "    limit = 99", [])

    assert gate._anchor_claim(TWO_HOLDERS, claim).lines == {2}


def test_a_claim_registered_against_the_dedented_shape_still_anchors(tmp_path):
    """The C-slice's case: the block was extracted and lost two levels.

    The claim's registered bytes carry the OLD indentation; the file carries the
    new one; the guarantee is the same guarantee, so the anchor finds it and
    reports the shift rather than failing configuration.
    """

    target = tmp_path / "moved.py"
    target.write_text(DEDENTED, encoding="utf-8")
    claim = _claim(
        "dedented",
        target,
        "target",
        "            record(outcome)",
        "            pass",
        [],
    )

    anchor = gate._anchor_claim(DEDENTED, claim)

    assert anchor.shift == -8
    assert anchor.lines == {2}
    assert anchor.find == "    record(outcome)"
    # The replacement follows the block to its new column — a mutant spliced at
    # the old indentation would not even parse.
    assert anchor.replace == "    pass"


def test_a_re_indented_claim_still_anchors_when_the_block_gains_levels(tmp_path):
    """The other direction, so the shift is not a one-signed accident."""

    target = tmp_path / "nested.py"
    target.write_text(NESTED, encoding="utf-8")
    claim = _claim(
        "indented",
        target,
        "target",
        "    if ready:\n        record(outcome)",
        "    if False:\n        record(outcome)",
        [],
    )

    anchor = gate._anchor_claim(NESTED, claim)

    assert anchor.shift == 4
    assert anchor.lines == {3, 4}
    assert anchor.replace == "        if False:\n            record(outcome)"


def test_a_re_indent_that_changes_the_blocks_own_nesting_is_not_an_anchor(tmp_path):
    """The limit of the tolerance, stated: a uniform shift is the same code in a
    new place; a block whose INTERNAL nesting changed is different code, and
    quietly mutating it would be the guessing this anchoring replaces."""

    source = "def target():\n    if ready:\n        record(outcome)\n"
    target = tmp_path / "renested.py"
    target.write_text(source, encoding="utf-8")
    claim = _claim(
        "renested",
        target,
        "target",
        "    if ready:\n            record(outcome)",
        "    pass",
        [],
    )

    with pytest.raises(RuntimeError, match="mutation source not found"):
        gate._anchor_claim(source, claim)


def test_a_symbol_that_no_longer_exists_fails_and_names_the_one_that_holds_it(tmp_path):
    """The `_parse_request` case, which used to pass in silence.

    A stale symbol is now fatal — and the refusal carries the repair, because
    the two ways a claim goes stale (a rename, an extraction) both leave the
    guarantee in a symbol the gate can point at.
    """

    target = tmp_path / "renamed.py"
    target.write_text(TWO_HOLDERS, encoding="utf-8")
    claim = _claim("stale-symbol", target, "_parse_request", "    limit = 5", "    limit = 99", [])

    with pytest.raises(RuntimeError, match=r"symbol not found .*: _parse_request"):
        gate._anchor_claim(TWO_HOLDERS, claim)


def test_a_needle_outside_the_named_symbol_names_where_it_actually_lives(tmp_path):
    """The symbol resolves, the needle is real, and they are not in the same
    place — the extraction case. The error is the re-anchor instruction."""

    target = tmp_path / "extracted.py"
    text = "def keeper():\n    pass\n\n\ndef holder():\n    limit = 5\n"
    target.write_text(text, encoding="utf-8")
    claim = _claim("moved-out", target, "keeper", "    limit = 5", "    limit = 99", [])

    with pytest.raises(RuntimeError, match=r"it is in holder — re-anchor"):
        gate._anchor_claim(text, claim)


def test_module_scope_still_means_the_whole_file(tmp_path):
    """Back-compat for the claims that have no definition to name — an import,
    a module constant, a decorator argument. `module` is a spelling, not a
    symbol lookup that happens to fail."""

    text = "import os\n\n\ndef target():\n    return os\n"
    target = tmp_path / "scoped.py"
    target.write_text(text, encoding="utf-8")
    claim = _claim("module-level", target, "module scope/os", "import os", "import io", [])

    assert gate._anchor_claim(text, claim).lines == {1}


def test_the_mutation_is_spliced_at_the_anchor_not_at_the_first_occurrence(
    tmp_path, touched, capsys
):
    """The end-to-end proof, at the level the guarantee lives at.

    Uniqueness is a property of the SYMBOL now, so an identical line earlier in
    the file is legal — and `str.replace(find, replace, 1)` would rewrite THAT
    one, reporting a killed mutant for a line no claim named. The probe passes
    only while `target`'s own copy is intact, so a mis-spliced run reports
    SURVIVED and a correctly spliced one reports KILLED.
    """

    target = tmp_path / "holders.py"
    target.write_text(TWO_HOLDERS, encoding="utf-8")
    claims = _claims_file(
        tmp_path,
        [
            _claim(
                "spliced",
                target,
                "target",
                "    limit = 5",
                "    limit = 99",
                _line_probe(target, 6, "    limit = 5"),
            )
        ],
    )
    touched({target: {7}})

    code = gate.run(
        "BASE", claims, _exemptions_file(tmp_path), max_candidates=12, list_only=False
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "KILLED: spliced" in out
    # Restored in `finally`, byte for byte, mutated line included.
    assert target.read_text(encoding="utf-8") == TWO_HOLDERS


def test_a_crlf_committed_target_anchors_and_splices_and_keeps_its_bytes(
    tmp_path, touched, capsys
):
    """The offsets and the file agree about line endings — they used not to.

    Measured red on pristine `main` (`0c744aa586`) on a Windows host, where
    `pathlib.write_text` gives the sibling cases above CRLF on disk: the
    anchor was resolved through `read_text` (universal newlines, CRLF decodes
    as LF) and the splice through `read_bytes().decode()` (raw), so the offset
    was one byte short per preceding line and every run refused with "changed
    after the anchor resolved". Not a Windows-only reach: 25 tracked `.py`
    blobs carried CRLF at that sha, and a Linux checkout of one of those is
    CRLF too.

    The restore is asserted at the BYTE level, because the mutant is written
    LF and a run that left a deliberately-CRLF file normalized would be this
    gate silently editing the tree it is measuring.
    """

    target = tmp_path / "holders.py"
    crlf = TWO_HOLDERS.replace("\n", "\r\n").encode("utf-8")
    target.write_bytes(crlf)
    claims = _claims_file(
        tmp_path,
        [
            _claim(
                "crlf-target",
                target,
                "target",
                "    limit = 5",
                "    limit = 99",
                _line_probe(target, 6, "    limit = 5"),
            )
        ],
    )
    touched({target: {7}})

    code = gate.run(
        "BASE", claims, _exemptions_file(tmp_path), max_candidates=12, list_only=False
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "KILLED: crlf-target" in out
    assert target.read_bytes() == crlf


def test_a_re_anchored_claim_says_so_after_the_candidate_line(tmp_path, touched, capsys):
    """A silent re-anchor is the same false all-clear as a silent skip, so the
    run accounts for it — and AFTER `mutation candidates:`, which CI greps at
    line start to decide whether to install the test environment at all."""

    target = tmp_path / "moved.py"
    target.write_text(DEDENTED, encoding="utf-8")
    claims = _claims_file(
        tmp_path,
        [
            _claim(
                "dedented",
                target,
                "target",
                "            record(outcome)",
                "            pass",
                ["{python}", "-c", "raise SystemExit(1)"],
            )
        ],
    )
    touched({})

    code = gate.run(
        "BASE", claims, _exemptions_file(tmp_path), max_candidates=12, list_only=True
    )
    lines = capsys.readouterr().out.splitlines()

    assert code == 0
    assert lines[0] == "mutation candidates: 0 (cap 12)"
    assert any(row.startswith("RE-ANCHORED: dedented") and "-8 columns" in row for row in lines)
