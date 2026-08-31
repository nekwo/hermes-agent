"""No tracked text blob carries a carriage return, and the count can only fall.

W1-H3 slice 5. `tests/agent_runtime/test_office_store.py` was CRLF on `main`,
came back from a Mac-side edit as LF, and turned a +149-line stage into a
2513-line whole-file rewrite that no reviewer could read. It was restored to
CRLF so the landing diff stayed reviewable — a correct call for that landing
and a bad steady state, because it left the repo's endings a property of
whichever host last touched a file.

The census that settled it, run at `0c744aa586`: 8862 LF blobs against 33 CRLF
and 5 MIXED. Not one of the 38 had a claim to its endings. Each was a minority
outlier inside its own directory (2 CRLF against 64 LF under
`docs/.../archive/`, 2 against 125 at the repo root), the two root artifacts
arrived together in the fork-foundation import, and every Windows-only script
in the tree (`*.ps1`, `*.cmd`) was already LF. So the deliberate list is EMPTY
and the keeper set below says so rather than being quietly omitted — an empty
exception set that exists is a decision; one that does not exist is a gap.

This gate, not `.gitattributes`, is the enforcement. Attributes govern
conversion on check-in and checkout and say nothing about blobs already in the
object store — which is exactly how 38 of them sat there for months.

The INDEX is what is read (`i/` from `git ls-files --eol`), never the working
tree: a contributor with `core.autocrlf=true` has CRLF on disk by design and
has done nothing wrong. What must not vary is what gets committed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent

#: Paths whose committed CRLF is DELIBERATE. Empty, and that is the finding —
#: see the module docstring. Adding one means adding a `-text` rule to
#: `.gitattributes` too, and writing the reason here: a keeper without a reason
#: is indistinguishable from the accident this gate exists to catch.
DELIBERATE_CRLF: frozenset[str] = frozenset()

#: `git ls-files --eol` index states that mean "this blob carries CR".
#: `i/none` is a file with no line endings at all (a one-line file with no
#: trailing newline) and `i/-text` is a binary, neither of which is a claim
#: about endings.
CARRIES_CR = frozenset({"i/crlf", "i/mixed"})


def _index_line_endings() -> dict[str, str]:
    """Every tracked path's INDEX end-of-line state, in one git call.

    One call rather than a blob read per file: the per-file spelling took over
    two minutes on this tree and a gate nobody waits for is a gate nobody runs.
    """

    completed = subprocess.run(
        ["git", "ls-files", "--eol"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.skip(f"git ls-files failed: {completed.stderr.strip()}")
    states: dict[str, str] = {}
    for row in completed.stdout.splitlines():
        if not row.strip():
            continue
        columns, _, path = row.partition("\t")
        states[path.strip()] = columns.split()[0]
    return states


@pytest.fixture(scope="module")
def endings() -> dict[str, str]:
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    states = _index_line_endings()
    # A collapse to a handful of rows means the parse drifted, not that the
    # tree emptied. Without this the assertion below passes vacuously on any
    # git output shape this stops understanding.
    assert len(states) > 5000, f"only {len(states)} tracked paths — the parse drifted"
    return states


def test_no_tracked_blob_carries_a_carriage_return_unless_it_is_declared(endings):
    """The pin. 38 CR-carrying blobs became 0, and 0 is now the ratchet."""

    carrying = sorted(
        path for path, state in endings.items() if state in CARRIES_CR
    )
    undeclared = [path for path in carrying if path not in DELIBERATE_CRLF]

    assert undeclared == [], (
        "these blobs carry CRLF in the INDEX and are not declared deliberate: "
        f"{undeclared}. Normalize them (read bytes, replace b'\\r\\n' with "
        "b'\\n', write bytes) or add each to DELIBERATE_CRLF with its reason "
        "and a `-text` rule in .gitattributes."
    )


def test_a_declared_keeper_must_actually_be_tracked_and_actually_carry_cr(endings):
    """The other direction, so the keeper set cannot rot into a wish list.

    A path that was normalized, renamed, or deleted while still sitting in
    DELIBERATE_CRLF is a standing exemption for nothing — and the next real
    accident at that path would be waved straight through.
    """

    for path in sorted(DELIBERATE_CRLF):
        assert path in endings, f"declared CRLF keeper is not tracked: {path}"
        assert endings[path] in CARRIES_CR, (
            f"declared CRLF keeper no longer carries CR: {path} ({endings[path]}) "
            "— drop it from DELIBERATE_CRLF"
        )


def test_the_byte_pinned_office_layout_goldens_are_still_lf(endings):
    """The one place in this repo where bytes are a contract: the cross-repo
    placement goldens are `-text` and byte-identical to the launcher's copy,
    with both `MANIFEST.sha256` files carrying the same digest. `-text` means
    git converts nothing in either direction — so what it holds is whatever was
    committed, and the new repo-wide `* text=auto eol=lf` must not have moved
    it. This asserts the bytes it actually holds, LF, rather than trusting rule
    precedence.
    """

    goldens = {
        path: state
        for path, state in endings.items()
        if path.startswith("tests/fixtures/office_layout/")
    }

    assert goldens, "the byte-pinned goldens moved; re-point this pin"
    assert not [path for path, state in goldens.items() if state in CARRIES_CR]
