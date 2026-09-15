"""Selection is about the SYMBOL a diff rewrote, not about two surviving lines.

W1-H3 slices 2 and 3. Two measured misses, both of which reported a green run:

* **H-H2 (`0ecb921b9d`).** That landing rewrote 82 lines of `agent_create.py`,
  41 of them inside `_reply`, and git rendered the exact two lines the NEW
  claim `hh2-the-one-reply-builder-stops-observing-the-revision` anchors on as
  unchanged CONTEXT. The claim registered for that slice was not selected by
  its own landing diff. H-H14 (`2523bd440`) had made anchoring symbol-scoped —
  but only for the SPLICE; selection still read the anchor's own two lines.
* **The Z1 landing (`4bf4387760`).** `_changed_lines` summed each hunk's `+`
  count, so a deletion-only hunk (`@@ -32 +31,0 @@`) contributed nothing and a
  pure-retirement diff selected zero claims by construction.

The historical case is exercised against the REAL blob at the REAL sha with
the REAL registered claim — not a fixture shaped like it — because the row this
closes is a statement about that commit and a rewritten miniature could be made
to say anything. The changed-line set is still injected, which is this suite's
standing anti-vacuity rule: what the diff touched is established here.

The limit is pinned too. A `module`-scope claim's span IS the file, so
widening it would select such a claim on any diff that touched the file at all
— which is not a symbol claim, it is no claim.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import changed_line_mutation_check as gate


HH2_SHA = "0ecb921b9d"
HH2_PATH = "agent_runtime/agent_create.py"
HH2_CLAIM = "hh2-the-one-reply-builder-stops-observing-the-revision"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=gate.REPO_ROOT, check=False, capture_output=True
    )


@pytest.fixture(scope="module")
def hh2():
    """The H-H2 landing as it really was: the file after, the lines it changed.

    Skipped rather than failed on a shallow checkout — the sha is an ancestor
    of `main` and CI checks out with `fetch-depth: 0`, but a worktree that
    cannot see it has nothing to say about this row either way.
    """

    blob = _git("cat-file", "-p", f"{HH2_SHA}:{HH2_PATH}")
    if blob.returncode != 0:
        pytest.skip(f"{HH2_SHA} is not in this checkout")
    text = blob.stdout.decode("utf-8").replace("\r\n", "\n")

    diff = _git("diff", "--unified=0", f"{HH2_SHA}^", HH2_SHA, "--", HH2_PATH)
    changed: set[int] = set()
    for row in diff.stdout.decode("utf-8").splitlines():
        match = gate.HUNK.match(row)
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        changed.update(range(start, start + count))
    assert changed, "the H-H2 diff touched agent_create.py; if not, this pin is wrong"
    return text, changed


@pytest.fixture(scope="module")
def hh2_claim() -> dict:
    rows = json.loads(
        (gate.REPO_ROOT / "tests" / "mutation_claims.json").read_text(encoding="utf-8")
    )["claims"]
    for row in rows:
        if row["id"] == HH2_CLAIM:
            return row
    pytest.skip(f"{HH2_CLAIM} is no longer registered")


def test_the_hh2_claims_own_landing_diff_never_touched_its_anchored_lines(hh2, hh2_claim):
    """The miss, still true and still measured — this is what the fix is FOR.

    If this ever goes green on its own, the case has changed and the sibling
    below stops proving anything, so it is asserted rather than assumed.
    """

    text, changed = hh2
    anchor = gate._anchor_claim(text, hh2_claim)

    assert anchor.lines == {1170, 1171}
    assert not anchor.lines & changed


def test_the_hh2_claim_is_selected_because_the_diff_rewrote_its_symbol(hh2, hh2_claim):
    """The fix, on the row's own case: `_reply` was rewritten, so its claim runs."""

    text, changed = hh2
    anchor = gate._anchor_claim(text, hh2_claim)

    assert anchor.symbol_lines, "_reply is a real symbol, not module scope"
    assert len(anchor.symbol_lines & changed) == 41


def test_the_hh2_case_end_to_end_reports_it_was_selected_by_symbol(
    tmp_path, monkeypatch, capsys, hh2, hh2_claim
):
    """Through `run`, at the level the guarantee lives at — and the output SAYS
    the widening happened. A claim that quietly appears in the candidate list
    for reasons the reader cannot reconstruct is how a gate stops being read.
    """

    text, changed = hh2
    target = tmp_path / "agent_create.py"
    target.write_bytes(text.encode("utf-8"))
    claim = dict(hh2_claim)
    claim["path"] = str(target)
    claims = tmp_path / "claims.json"
    claims.write_text(json.dumps({"claims": [claim]}), encoding="utf-8")
    exemptions = tmp_path / "exemptions.yaml"
    exemptions.write_text(json.dumps({"exemptions": []}), encoding="utf-8")
    monkeypatch.setattr(gate, "_changed_lines", lambda base, path: set(changed))

    code = gate.run("BASE", claims, exemptions, wall_budget_seconds=900, list_only=True)
    out = capsys.readouterr().out

    assert code == 0
    assert "mutation candidates: 1 " in out
    assert f"  {HH2_CLAIM}:" in out
    assert "(selected by symbol)" in out
    assert "UNSELECTED" not in out


def test_a_line_selected_claim_is_not_labelled_as_selected_by_symbol(
    tmp_path, monkeypatch, capsys, hh2, hh2_claim
):
    """ANTI-VACUITY for the label: the same claim, the same file, and a changed
    set that DOES cover its anchored lines — the row loses the suffix. So the
    suffix reports the selection path and is not printed unconditionally."""

    text, _ = hh2
    target = tmp_path / "agent_create.py"
    target.write_bytes(text.encode("utf-8"))
    claim = dict(hh2_claim)
    claim["path"] = str(target)
    claims = tmp_path / "claims.json"
    claims.write_text(json.dumps({"claims": [claim]}), encoding="utf-8")
    exemptions = tmp_path / "exemptions.yaml"
    exemptions.write_text(json.dumps({"exemptions": []}), encoding="utf-8")
    monkeypatch.setattr(gate, "_changed_lines", lambda base, path: {1170})

    gate.run("BASE", claims, exemptions, wall_budget_seconds=900, list_only=True)
    out = capsys.readouterr().out

    assert f"  {HH2_CLAIM}:" in out
    assert "(selected by symbol)" not in out


MODULE_TARGET = '''\
import os

CEILING = 5


def unrelated():
    return os, CEILING
'''


def test_a_module_scope_claim_is_not_widened_to_the_whole_file(
    tmp_path, monkeypatch, capsys
):
    """The stated limit. `module` means "there is no AST node to scope to", so
    its span is the file — and selecting on the file would put such a claim on
    the hook for every diff that touched the module, which is a claim about
    nothing. It keeps line selection, and a diff elsewhere in the file leaves
    it unselected.
    """

    target = tmp_path / "module_scoped.py"
    target.write_bytes(MODULE_TARGET.encode("utf-8"))
    claim = {
        "id": "module-claim",
        "path": str(target),
        "symbol": "module scope/os",
        "operator": "flip-an-import",
        "find": "import os",
        "replace": "import io as os",
        "test": ["{python}", "-c", "raise SystemExit(1)"],
    }
    claims = tmp_path / "claims.json"
    claims.write_text(json.dumps({"claims": [claim]}), encoding="utf-8")
    exemptions = tmp_path / "exemptions.yaml"
    exemptions.write_text(json.dumps({"exemptions": []}), encoding="utf-8")
    # Line 7 is inside `unrelated`, nowhere near the anchored import.
    monkeypatch.setattr(gate, "_changed_lines", lambda base, path: {7})

    code = gate.run("BASE", claims, exemptions, wall_budget_seconds=900, list_only=True)
    out = capsys.readouterr().out

    assert code == 0
    assert "mutation candidates: 0 " in out
    assert "UNSELECTED (0 changed lines): module-claim" in out


def test_a_symbol_scoped_claim_in_the_same_shape_is_selected(
    tmp_path, monkeypatch, capsys
):
    """ANTI-VACUITY for the case above: identical file, identical changed line,
    and a claim anchored on a real SYMBOL instead of `module` — and it is
    selected. So the unselected result there is the module-scope rule and not
    the fixture being unreachable."""

    target = tmp_path / "module_scoped.py"
    target.write_bytes(MODULE_TARGET.encode("utf-8"))
    claim = {
        "id": "symbol-claim",
        "path": str(target),
        "symbol": "unrelated",
        "operator": "flip-a-return",
        "find": "    return os, CEILING",
        "replace": "    return os",
        "test": ["{python}", "-c", "raise SystemExit(1)"],
    }
    claims = tmp_path / "claims.json"
    claims.write_text(json.dumps({"claims": [claim]}), encoding="utf-8")
    exemptions = tmp_path / "exemptions.yaml"
    exemptions.write_text(json.dumps({"exemptions": []}), encoding="utf-8")
    monkeypatch.setattr(gate, "_changed_lines", lambda base, path: {6})

    gate.run("BASE", claims, exemptions, wall_budget_seconds=900, list_only=True)

    assert "mutation candidates: 1 " in capsys.readouterr().out


def _diff_returning(stdout: str, monkeypatch):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(gate.subprocess, "run", fake_run)


def test_a_deletion_only_hunk_now_contributes_its_surrounding_lines(monkeypatch):
    """The Z1 case, in the parser that caused it.

    `@@ -32 +31,0 @@` is one line removed and none added. `range(31, 31)` is
    empty, so the whole hunk used to vanish and a retirement wave selected
    nothing. The two new-file lines the removed text sat between are what is
    left of it.
    """

    _diff_returning("@@ -32 +31,0 @@ from agent_runtime.persona_assignments import (\n", monkeypatch)

    assert gate._changed_lines("BASE", "any.py") == {31, 32}


def test_an_addition_hunk_is_unchanged_by_the_deletion_rule(monkeypatch):
    """ANTI-VACUITY: the ordinary hunk still means exactly what it meant, so
    the deletion arm is an addition to the parser and not a re-interpretation
    of it."""

    _diff_returning("@@ -10,2 +10,3 @@ def target():\n", monkeypatch)

    assert gate._changed_lines("BASE", "any.py") == {10, 11, 12}


def test_a_deletion_at_the_top_of_a_file_does_not_ask_for_line_zero(monkeypatch):
    """`+0,0` is what git emits when the removed lines were the file's first.
    There is no line 0; the clamp says line 1, which is where the survivors
    are."""

    _diff_returning("@@ -1,3 +0,0 @@\n", monkeypatch)

    assert gate._changed_lines("BASE", "any.py") == {1}
