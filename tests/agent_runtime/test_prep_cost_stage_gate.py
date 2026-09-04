"""The per-stage billing gate on the chat-turn prep-cost plan.

Queue row (w12/m5): *two of three prep-cost stages named a remedy site the live
record then contradicted — the missing mechanism is a per-stage gate.* The plan's
§6 opening gate is written against the PLAN as a whole ("do not start Stage 1+
until one turn record shows `request_assembled` …"), so it is discharged once,
by the first stage through it. Nothing then re-asked the question per stage, and
Stage 3's named remedy site turned out to bill **1 ms warm** while Stage 4's
pooling half turned out to bill **~6 ms** against its own 100 ms threshold — both
caught only because the 2026-09-01 dispatch brief happened to require a
re-measurement first (field notes
``chat-turn-prep-stages-3-5-field-notes-2026-09-01.md`` §2, §4, §6).

The gate this file enforces: **every stage in §5 states, in its own text, whether
an instrument billed its site before the stage was written** — one of

* ``BILLED`` — a live receipt convicted the site first (the reference is named),
* ``NO REMEDY SITE`` — the stage builds instrumentation and proposes no cure, or
* ``NOT BILLED`` — the site was named ahead of its receipt, and the record's
  verdict is recorded beside it.

A stage that says none of the three cannot be added to the plan without this
test going red. The two ``NOT BILLED`` rows are the recurrence itself, kept in
the document rather than filed away, so the next stage author reads them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PLAN_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "agent-runtime-harness"
    / "planned"
    / "chat-turn-prep-cost.md"
)

#: A stage paragraph opens with a bold ``**Stage <id> — `` run at column 0.
_STAGE_RE = re.compile(r"^\*\*Stage (?P<id>[0-9]+[a-z]?) — ", re.MULTILINE)

#: The marker each stage must carry, and the closed set of verdicts it may take.
_MARKER_RE = re.compile(
    r"^\*Billing gate: (?P<verdict>BILLED|NOT BILLED|NO REMEDY SITE)\b",
    re.MULTILINE,
)

VERDICTS = frozenset({"BILLED", "NOT BILLED", "NO REMEDY SITE"})


def _stage_blocks(text: str) -> dict[str, str]:
    """``{stage id: the stage's own text}``, split at the next stage or §6."""

    starts = [(m.group("id"), m.start()) for m in _STAGE_RE.finditer(text)]
    gate_section = text.find("\n## 6. Opening gate")
    end_of_stages = gate_section if gate_section != -1 else len(text)
    blocks: dict[str, str] = {}
    for index, (stage_id, start) in enumerate(starts):
        stop = starts[index + 1][1] if index + 1 < len(starts) else end_of_stages
        blocks[stage_id] = text[start:stop]
    return blocks


@pytest.fixture(scope="module")
def blocks() -> dict[str, str]:
    return _stage_blocks(PLAN_PATH.read_text(encoding="utf-8"))


def test_the_splitter_finds_the_stages_the_plan_actually_has(blocks):
    """ANTI-VACUITY. A regex that matched nothing would make every assertion
    below trivially true, so the shape of §5 is pinned first: the seven stages
    the EXECUTED ledger and §5's own heading name."""

    assert sorted(blocks) == ["0", "1", "2", "2a", "3", "4", "5"]
    assert all(len(body.strip()) > 200 for body in blocks.values())


def test_every_stage_states_whether_an_instrument_billed_its_site(blocks):
    """The gate. §6 asks this of the plan once; this asks it of each stage."""

    missing = [stage_id for stage_id, body in blocks.items() if not _MARKER_RE.search(body)]
    assert missing == [], (
        "Stages with no `*Billing gate:` line: "
        + ", ".join(missing)
        + ". A stage may not name a remedy site until an instrument bills that "
        "site; say BILLED (and name the receipt), NOT BILLED, or NO REMEDY SITE."
    )
    verdicts = {stage_id: _MARKER_RE.search(body).group("verdict") for stage_id, body in blocks.items()}
    assert set(verdicts.values()) <= VERDICTS


def test_a_billed_stage_names_the_receipt_that_billed_it(blocks):
    """``BILLED`` is a claim about a measurement, so it must point at one. The
    marker line has to carry a section reference (`§`) or a receipt key (`_ms`),
    otherwise "BILLED" is a word rather than evidence."""

    for stage_id, body in blocks.items():
        match = _MARKER_RE.search(body)
        assert match is not None, stage_id
        if match.group("verdict") != "BILLED":
            continue
        line = body[match.start() : body.index("\n", match.start())]
        assert "§" in line or "_ms" in line or "builds_overlapped" in line, (
            f"Stage {stage_id} claims BILLED without naming the receipt: {line}"
        )


def test_the_recurrence_stays_on_the_page(blocks):
    """Stages 3 and 4 are the two the live record contradicted. If a later edit
    launders either into BILLED, the row this gate came from loses its evidence
    — so the verdicts are pinned, not merely present."""

    assert _MARKER_RE.search(blocks["3"]).group("verdict") == "NOT BILLED"
    assert _MARKER_RE.search(blocks["4"]).group("verdict") == "NOT BILLED"


def test_the_gate_is_falsifiable(blocks):
    """The checker itself: strip a marker and the same predicate must fail.
    Without this the gate would pass on a document it never actually read."""

    stripped = _MARKER_RE.sub("*Something else:", blocks["5"], count=1)

    assert _MARKER_RE.search(stripped) is None
