"""Admission for a payload FLAG: a boolean does not ship on the author's memory.

The recurring defect, twice in one week and both caught by review rather than by
a test: a payload boolean whose guarantee lives only in prose. `MAX_CARD_PIXELS`
carried a "can never drift" comment that was false at both ends, and `cardSafe`
was the console budget wearing the own-sheet bound's name — both passed a green
suite, because every test asserted the predicate's ARITHMETIC and none asserted
a case where the DOCUMENTED guarantee and the IMPLEMENTED one disagree.

The instances were repaired one at a time. The CLASS was not: nothing anywhere
looked at a new flag and asked what admits it, so the next one arrives on
whoever writes it remembering this file exists.

This is that check, and it has two halves that have to be read together:

* the flag POPULATION is measured, never declared — `build_flag_inventory` runs
  the real verbs and reads the booleans off what they PRINT, so a flag that
  reaches a payload cannot fail to reach this test;
* the ADMISSION is declared, because what admits a flag is a judgement: it is
  either a GUARANTEE (and then it names the pure predicate that computes it and
  the test that pins a DISAGREEMENT) or it is DATA about the item, which has no
  guarantee to drift. The table below is the admission record. A new boolean
  reds this test until somebody writes which of the two it is.

This is the second-order lesson of the class stated as a mechanism: the
population is the half a person forgets, so it is measured; the classification
is the half a machine cannot do, so it is written down where the next author
trips over it.
"""

from __future__ import annotations

import ast
import importlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from hermes_cli.charsheet_payload_contract import build_flag_inventory

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Guarantee:
    """A flag that STATES something; admitted with its predicate and a disagreement.

    *predicate* is ``module:function`` — pure, public and the single computation
    of the flag, so prose cannot drift from it without the function moving.
    *disagreement* is ``file::test`` — a case where this flag and its nearest
    neighbour (or its own documented promise) come out DIFFERENT. A test where
    every flag always agrees is the shape `cardSafe` passed for a month.
    """

    predicate: str
    disagreement: str


@dataclass(frozen=True)
class Data:
    """A flag that REPORTS a fact about the item, with no guarantee to drift.

    `rejected` is not a promise about anything; it is what the operator did. The
    entry still has to be written, because "this one is only data" is exactly the
    judgement the class needs recorded rather than assumed.
    """

    why: str


#: The admission record: every boolean these payloads carry, and what admits it.
#: Keyed ``<kind>.<contract path>`` — the path spelling the contract dump uses,
#: placeholder included.
ADMITTED: dict[str, Guarantee | Data] = {
    # ── the envelope ──
    "sprite.ok": Data(why="the CLI envelope's own success flag, not a claim about the picture"),
    "thumb.ok": Data(why="the CLI envelope's own success flag"),
    "status.ok": Data(why="the CLI envelope's own success flag"),
    "list.ok": Data(why="the CLI envelope's own success flag"),
    # ── the two crop bounds: the instances that taught the class ──
    "thumb.withinConsoleBudget": Guarantee(
        predicate="agent.charsheet.pipeline:fits_console_budget",
        disagreement=(
            "tests/agent/test_charsheet_draft.py::"
            "test_a_crop_under_the_console_ceiling_can_be_many_times_its_OWN_sheet"
        ),
    ),
    "thumb.withinOwnSheet": Guarantee(
        predicate="agent.charsheet.pipeline:fits_own_sheet",
        disagreement=(
            "tests/agent/test_charsheet_draft.py::"
            "test_a_crop_over_the_console_ceiling_can_be_LIGHTER_than_its_own_sheet"
        ),
    ),
    # ── shape and taxonomy ──
    "thumb.square": Data(why="which SHAPE was written, echoed from the caller's own flag"),
    "sprite.character.states[].directional": Data(
        why="a fact about the state as authored (`cheer:4:fixed`), copied from the spec"
    ),
    "status.status.spec.states[].directional": Data(why="the same spec fact, on the draft"),
    "list.characters[].installed": Data(
        why="whether the sheet file is on disk beside the manifest — a stat, not a promise"
    ),
    # ── QA history ──
    "status.status.rows.{}.history[].rejected": Data(
        why="what the operator did to that attempt; the store records it, nothing computes it"
    ),
    "status.status.turnaround.{}.history[].rejected": Data(why="the same, for a direction"),
}


def _defined_tests(relative: str) -> set[str]:
    """Every test function name defined in *relative*, by AST.

    Not a substring search: a name that appears only inside a docstring or a
    `-k` string is not a test that exists, and the whole point of this arm is
    that a cited test resolves.
    """
    tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


@pytest.fixture(scope="module")
def inventory():
    return build_flag_inventory()


def test_every_payload_flag_is_admitted_and_every_admission_is_still_a_flag(inventory):
    """Both directions, because both failures are silent.

    An UNADMITTED flag is the class itself: a boolean shipped with its guarantee
    in prose. A STALE admission is the other half — an entry for a flag that no
    longer exists reads as coverage of something nobody publishes any more, and
    is how a table like this rots into decoration.
    """
    measured = {
        f"{kind}.{path}" for kind, paths in inventory.items() for path in paths
    }

    assert measured == set(ADMITTED), (
        "unadmitted flag(s): "
        f"{sorted(measured - set(ADMITTED))}; stale admission(s): "
        f"{sorted(set(ADMITTED) - measured)}. A new payload boolean is admitted "
        "by writing what it is: a Guarantee (naming its pure predicate and a "
        "test where it DISAGREES with its neighbour) or Data (saying why there "
        "is no guarantee to drift)."
    )


def test_every_guarantee_names_a_predicate_that_exists_and_is_callable():
    """The prose-only guarantee is the defect, so the flag must have ONE home.

    A flag computed inline in the producer can be described two ways in two
    places, which is `MAX_CARD_PIXELS`'s "can never drift" comment exactly. A
    named public function cannot: the comment and the caller point at the same
    object.
    """
    for flag, entry in ADMITTED.items():
        if not isinstance(entry, Guarantee):
            continue
        module_name, _, function = entry.predicate.partition(":")
        module = importlib.import_module(module_name)
        assert hasattr(module, function), f"{flag}: {entry.predicate} does not exist"
        assert callable(getattr(module, function)), f"{flag}: {entry.predicate} is not callable"


def test_every_guarantee_names_a_disagreement_test_that_exists():
    """The citation has to resolve, or the admission is a sentence about nothing.

    This is an EXISTENCE check and says so: it cannot prove the named test
    exercises a disagreement, which is the vacuous class and is invisible to any
    gate of this shape. What it does prove is that the citation is not rotted —
    which is precisely what nobody was checking when `cardSafe` was admitted.
    """
    for flag, entry in ADMITTED.items():
        if not isinstance(entry, Guarantee):
            continue
        relative, _, name = entry.disagreement.partition("::")
        assert (REPO_ROOT / relative).is_file(), f"{flag}: {relative} does not exist"
        assert name in _defined_tests(relative), (
            f"{flag}: {relative} defines no {name!r}"
        )


def test_the_two_crop_bounds_are_admitted_as_guarantees_and_not_as_data():
    """The one classification this file is not free to get wrong.

    These two ARE the instances. If a later edit ever downgrades them to Data,
    the table has forgotten the two defects it was written from, and this test
    is the thing that says so.
    """
    for flag in ("thumb.withinConsoleBudget", "thumb.withinOwnSheet"):
        assert isinstance(ADMITTED[flag], Guarantee), flag
    assert ADMITTED["thumb.withinConsoleBudget"] != ADMITTED["thumb.withinOwnSheet"], (
        "two flags admitted by one predicate is the cardSafe shape returning"
    )
