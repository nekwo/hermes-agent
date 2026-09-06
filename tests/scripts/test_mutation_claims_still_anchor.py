"""Every registered mutation claim must still anchor in the tree it names.

The class this gate exists for
------------------------------
A mutation claim is a needle (``find``) inside a named symbol. When the code it
guards is edited around it, the needle stops resolving and the claim guards
nothing — but nothing local says so. ``scripts/changed_line_mutation_check.py``
does notice: anchoring every claim is the first thing its ``--list`` inventory
does, and a needle that no longer resolves is a *configuration error*, exit 2.
That is the right severity and the wrong PLACE for the only detector: the
inventory runs in CI's ``Changed-line mutation claims`` job, so a drift lands on
main and is discovered by whoever reads the run page.

Measured on run 33969282189 (2026-09-05, the first main push whose slices
collected): the job was red on
``ir-h4-the-instance-family-is-not-addressable-by-revert``, whose needle stopped
matching when ``DRIFT_FAMILY_FLOW_GRAPH`` joined ``realm_revert.FAMILIES``.
Behind it, invisible because the inventory raises on the first one it hits, were
``iws-ws1-the-activate-events-free-ride-at-an-undeclaring-client`` (a third entry
joined the token-gated dict) and ``bipv-the-crossing-refusal-escapes-the-catch-all``
(the 2026-09-04 catch-all ruling deleted the four hand-placed rows its needle
named). A fourth, ``dcw-h4-the-autopilot-stream-is-framed-in-indented-blocks``,
arrived in the four days after that run. Four dead guarantees, one visible, and
one CI round per repair.

So the check moves to where a developer meets it in seconds, and reports ALL of
them at once rather than the first.

WHAT THIS GATE CANNOT SEE
-------------------------
It proves each needle still RESOLVES, exactly once, inside the symbol its claim
names. It does not run the mutation, so it cannot tell you the claim still
KILLS its test — a needle can survive a refactor that made the sabotage
harmless. That proof is the mutating run's, and it costs a test process per
claim; this one costs a file read.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from scripts import changed_line_mutation_check as gate


#: Floors, not exact counts: the registry grows, and a gate that has to be
#: edited on every growth gets edited without being read. They exist because a
#: census that silently reads nothing is green forever (MCF-53), and they sit
#: well below the measurement — 298 claims across 60 files on 2026-09-06 — so
#: that retiring a claim is not an unrelated red.
MINIMUM_CLAIMS = 200
MINIMUM_DISTINCT_PATHS = 40


class _ParseMemo:
    """A stand-in for the gate's ``ast`` module that parses each source once.

    ``_symbol_span`` re-parses the claim's whole file per claim, and the
    registry points 298 claims at 60 files — five parses of every file, several
    of which are multi-thousand-line modules. That is ~18 s, against this
    repo's 30 s per-test cap (``pyproject.toml`` addopts): a gate that close to
    its own ceiling reds on a loaded machine and teaches its reader to rerun.
    Parsing once per distinct source takes the walk to ~2 s.

    Installed on the ``gate`` module's own ``ast`` reference, never on the
    stdlib module, so nothing outside this file sees it. Every other attribute
    proxies through — the gate reads ``ast.If``, ``ast.Module`` and friends.
    """

    def __init__(self) -> None:
        self._trees: dict[str, ast.Module] = {}

    def parse(self, source, *args, **kwargs):
        if args or kwargs or not isinstance(source, str):
            return ast.parse(source, *args, **kwargs)
        tree = self._trees.get(source)
        if tree is None:
            tree = self._trees[source] = ast.parse(source)
        return tree

    def __getattr__(self, name: str):
        return getattr(ast, name)


@pytest.fixture(scope="module")
def anchoring():
    """The gate's own anchoring, memoized, plus the registry it read.

    ``_qualified_definitions`` is keyed by ``id(tree)``, which is only sound
    because :class:`_ParseMemo` holds every tree alive for the fixture's life —
    an id cannot be recycled while its object is referenced.
    """

    memo = _ParseMemo()
    definitions: dict[int, dict] = {}
    real_definitions = gate._qualified_definitions

    def memo_definitions(tree):
        key = id(tree)
        if key not in definitions:
            definitions[key] = real_definitions(tree)
        return definitions[key]

    original_ast = gate.ast
    gate.ast = memo
    gate._qualified_definitions = memo_definitions
    try:
        rows = json.loads(gate.DEFAULT_CLAIMS.read_text(encoding="utf-8"))["claims"]
        assert isinstance(rows, list) and rows

        texts: dict[str, str] = {}

        def anchor_failures(claims: list[dict]) -> list[str]:
            """Every claim that does not anchor, with the gate's own message."""

            failures: list[str] = []
            for claim in claims:
                relative = str(claim["path"])
                if relative not in texts:
                    target = gate.REPO_ROOT / relative
                    texts[relative] = target.read_text(encoding="utf-8") if target.is_file() else ""
                if not texts[relative]:
                    failures.append(f"{claim['id']}: {relative} does not exist")
                    continue
                try:
                    gate._anchor_claim(texts[relative], claim)
                except Exception as exc:  # RuntimeError, carrying the repair hint
                    failures.append(f"{claim['id']}: {exc}")
            return failures

        yield rows, anchor_failures
    finally:
        gate.ast = original_ast
        gate._qualified_definitions = real_definitions


def test_every_registered_claim_anchors_where_it_says_it_does(anchoring) -> None:
    """The whole registry, in one read, naming every drifted claim at once."""

    rows, anchor_failures = anchoring
    failures = anchor_failures(rows)
    assert failures == [], (
        "these mutation claims no longer resolve, so they guard nothing. "
        "Re-derive each needle against the code as it stands now and prove the "
        "repair by running the claim's own test against the mutant "
        "(the claim is only repaired when its test goes red):\n"
        + "\n".join(f"  - {line}" for line in failures)
    )


def test_the_gate_read_a_real_registry(anchoring) -> None:
    """Anti-vacuity: a census that reads nothing passes this file forever."""

    rows, _ = anchoring
    paths = {str(claim["path"]) for claim in rows}
    assert len(rows) >= MINIMUM_CLAIMS, f"only {len(rows)} claims read"
    assert len(paths) >= MINIMUM_DISTINCT_PATHS, f"only {len(paths)} distinct files read"


def test_a_needle_the_code_moved_past_is_reported_by_name(anchoring) -> None:
    """The killing mutation: break one real needle, and this gate must say so.

    Uses the registry's own first claim rather than a synthetic file, so the
    case exercises the same anchoring path the gate above runs.
    """

    rows, anchor_failures = anchoring
    victim = dict(rows[0])
    victim["find"] = victim["find"] + "\n# a line the file does not contain\n"

    failures = anchor_failures([victim])

    assert len(failures) == 1
    assert failures[0].startswith(f"{victim['id']}: ")
    assert "mutation source not found" in failures[0]


def test_a_claim_naming_a_deleted_file_is_reported_rather_than_raising(anchoring) -> None:
    """A path that no longer exists is drift too, not a crash in the walk."""

    _, anchor_failures = anchoring
    orphan = {
        "id": "planted-orphan",
        "path": "agent_runtime/a_module_that_was_deleted.py",
        "symbol": "module",
        "operator": "never-runs",
        "find": "x = 1",
        "replace": "x = 2",
        "test": ["{python}", "-c", "raise SystemExit(1)"],
    }
    assert not (gate.REPO_ROOT / orphan["path"]).exists()

    failures = anchor_failures([orphan])

    assert failures == ["planted-orphan: agent_runtime/a_module_that_was_deleted.py does not exist"]
