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
arrived in the four days after that run. Four dead guarantees, one visible.

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

import json
from pathlib import Path

import pytest

from scripts import changed_line_mutation_check as gate


#: Floors, not exact counts: the registry grows, and a gate that has to be
#: edited on every growth gets edited without being read. They exist because a
#: census that silently reads nothing is green forever (MCF-53). Both were
#: comfortably met at 298 claims across 111 files on 2026-09-06.
MINIMUM_CLAIMS = 200
MINIMUM_DISTINCT_PATHS = 60


@pytest.fixture(scope="module")
def registry() -> list[dict]:
    rows = json.loads(gate.DEFAULT_CLAIMS.read_text(encoding="utf-8"))["claims"]
    assert isinstance(rows, list) and rows
    return rows


def _anchor_failures(rows: list[dict]) -> list[str]:
    """Every claim that does not anchor, with the gate's own message."""

    failures: list[str] = []
    for claim in rows:
        target = gate.REPO_ROOT / str(claim["path"])
        if not target.is_file():
            failures.append(f"{claim['id']}: {claim['path']} does not exist")
            continue
        try:
            gate._anchor_claim(target.read_text(encoding="utf-8"), claim)
        except Exception as exc:  # the gate raises RuntimeError with the repair hint
            failures.append(f"{claim['id']}: {exc}")
    return failures


def test_every_registered_claim_anchors_where_it_says_it_does(registry) -> None:
    """The whole registry, in one read, naming every drifted claim at once."""

    failures = _anchor_failures(registry)
    assert failures == [], (
        "these mutation claims no longer resolve, so they guard nothing. "
        "Re-derive each needle against the code as it stands now and prove the "
        "repair by running the claim's own test against the mutant "
        "(the claim is only repaired when its test goes red):\n"
        + "\n".join(f"  - {line}" for line in failures)
    )


def test_the_gate_read_a_real_registry(registry) -> None:
    """Anti-vacuity: a census that reads nothing passes this file forever."""

    paths = {str(claim["path"]) for claim in registry}
    assert len(registry) >= MINIMUM_CLAIMS, f"only {len(registry)} claims read"
    assert len(paths) >= MINIMUM_DISTINCT_PATHS, f"only {len(paths)} distinct files read"


def test_a_needle_the_code_moved_past_is_reported_by_name(registry) -> None:
    """The killing mutation: break one real needle, and this gate must say so.

    Uses the registry's own first claim rather than a synthetic file, so the
    case exercises the same anchoring path the gate above runs.
    """

    victim = dict(registry[0])
    victim["find"] = victim["find"] + "\n# a line the file does not contain\n"

    failures = _anchor_failures([victim])

    assert len(failures) == 1
    assert failures[0].startswith(f"{victim['id']}: ")
    assert "mutation source not found" in failures[0]


def test_a_claim_naming_a_deleted_file_is_reported_rather_than_raising() -> None:
    """A path that no longer exists is drift too, not a crash in the walk."""

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

    failures = _anchor_failures([orphan])

    assert failures == ["planted-orphan: agent_runtime/a_module_that_was_deleted.py does not exist"]
