"""The volatile tail is a registered, budgeted roster — never an anonymous list.

The tail is the ONE channel emitted on every runtime-context delivery, so it is
the one channel an agent is guaranteed to read each turn. Before
``agent_runtime.volatile_tail`` it was a hand-joined list of bullet strings in
the exec'd mission-chat CLI body: no roster (who contributes was knowable only
by reading the command), no bound (any renderer could turn three bullets into a
wall), and therefore no way to bound it without silently truncating the one
channel whose entire purpose is "what is true this turn".

These tests pin the replacement contract: contributors register by NAME with
their own BYTE BUDGET, and a shortfall is always visible twice — in band, to the
agent, and as a typed accounting row, to the operator.
"""

from __future__ import annotations

import pytest

from agent_runtime.volatile_tail import (
    STATUS_DROPPED,
    STATUS_EMITTED,
    STATUS_EMPTY,
    STATUS_TRUNCATED,
    TRUNCATION_MIN_BODY_BYTES,
    VolatileContribution,
    VolatileTailBuilder,
    compose_volatile_tail,
)


def _tail(*pairs):
    return compose_volatile_tail(
        VolatileContribution(name=name, content=content, budget_bytes=budget)
        for name, content, budget in pairs
    )


# ── the ordinary turn ───────────────────────────────────────────────────────


def test_a_within_budget_tail_is_byte_identical_to_the_hand_joined_list():
    """The refactor's load-bearing claim.

    Budgets are set far above every renderer's realistic maximum precisely so a
    standard turn composes to exactly the bytes the pre-refactor
    ``"\\n".join(line for line in lines if line)`` produced. If this ever fails,
    the composition changed what the agent reads — which is a behavior change
    wearing a refactor's clothes.
    """

    lines = [
        "- Wall budget: ~180s left of 240s (this turn only).",
        "- Dropped on this lane: toolsets file, terminal.",
        "- MCP: launcher_qa (mcp_server_not_admitted) — not available this turn.",
    ]
    legacy = "\n".join(line for line in lines if line)

    tail = _tail(
        ("turn_budget", lines[0], 1024),
        ("capability", lines[1], 4096),
        ("mcp_admission", lines[2], 2048),
    )
    assert tail.content == legacy
    assert tail.complete
    assert [entry.status for entry in tail.entries] == [STATUS_EMITTED] * 3


def test_an_empty_contributor_pays_nothing_but_is_still_accounted():
    """Honest silence is free — and distinguishable from a drop.

    "the capability account had nothing to say" and "the capability account did
    not fit" are different facts. A row set that only listed what was emitted
    could not tell an operator which one happened.
    """

    tail = _tail(
        ("turn_budget", "- Wall budget: ~10s left.", 1024),
        ("capability", "", 4096),
        ("mcp_admission", "   ", 2048),
    )
    assert tail.content == "- Wall budget: ~10s left."
    assert tail.complete  # empty is not a shortfall
    statuses = {entry.name: entry.status for entry in tail.entries}
    assert statuses == {
        "turn_budget": STATUS_EMITTED,
        "capability": STATUS_EMPTY,
        "mcp_admission": STATUS_EMPTY,
    }
    # Every contributor appears in the accounting, including the silent ones.
    assert [row["name"] for row in tail.rows()] == [
        "turn_budget",
        "capability",
        "mcp_admission",
    ]


def test_registration_order_is_the_delivery_order():
    """The roster IS the contract: the agent sees the same facts in the same
    order every turn, so a missing one is noticeable rather than ambiguous."""

    tail = _tail(("a", "- A", 64), ("b", "- B", 64), ("c", "- C", 64))
    assert tail.content == "- A\n- B\n- C"


# ── no silent caps ──────────────────────────────────────────────────────────


def test_an_over_budget_contributor_is_truncated_and_says_so_in_band():
    """A partial capability account that LOOKS complete is worse than none.

    The agent must read, in the same channel, that what it just saw is not the
    whole fact — otherwise it reasons from a fragment as if it were the total.
    """

    body = "- Dropped on this lane: " + ("toolset_name, " * 200)
    tail = _tail(("capability", body, 400))

    entry = tail.entries[0]
    assert entry.status == STATUS_TRUNCATED
    assert entry.original_bytes == len(body.strip().encode("utf-8"))
    assert TRUNCATION_MIN_BODY_BYTES <= entry.emitted_bytes <= 400 < entry.original_bytes
    assert entry.short
    assert not tail.complete

    assert "TRUNCATED" in tail.content
    assert "'capability'" in tail.content
    assert "UNKNOWN" in tail.content
    # The surviving prefix is still delivered — a bounded fact beats no fact.
    assert tail.content.startswith("- Dropped on this lane:")


def test_a_contributor_too_large_to_truncate_usefully_is_dropped_but_never_silently():
    """Below the useful-prefix floor a fragment reads as a complete sentence and
    misleads, so the body goes and only the note stays. The note is the point:
    the agent is told the fact EXISTS and that it did not fit."""

    tail = _tail(("capability", "x" * 5000, TRUNCATION_MIN_BODY_BYTES - 1))

    entry = tail.entries[0]
    assert entry.status == STATUS_DROPPED
    assert entry.emitted_bytes == 0
    assert entry.original_bytes == 5000

    assert "xxx" not in tail.content
    assert "DROPPED" in tail.content
    assert "'capability'" in tail.content
    assert "nothing to report" in tail.content


def test_every_shortfall_is_reachable_as_a_typed_row():
    """The operator-side half of the same accounting. In-band prose tells the
    agent; these rows tell an observability consumer, which must never have to
    grep the rendered text to learn a fact was clipped."""

    tail = _tail(
        ("turn_budget", "- fine", 1024),
        ("capability", "y" * 5000, 300),
        ("mcp_admission", "", 2048),
    )
    rows = [shortfall.row() for shortfall in tail.shortfalls]
    assert [row["name"] for row in rows] == ["capability"]
    row = rows[0]
    assert row["status"] == STATUS_TRUNCATED
    assert row["budget_bytes"] == 300
    assert row["original_bytes"] == 5000
    assert 0 < row["emitted_bytes"] <= 300


def test_truncation_never_splits_a_utf8_codepoint():
    """Budgets are in BYTES but the tail is text: a naive byte slice would emit
    a broken codepoint into the model's context."""

    tail = _tail(("capability", "é" * 500, 301))
    body = tail.content.split("\n")[0]
    # Round-trips cleanly, and stayed inside its budget.
    assert body.encode("utf-8").decode("utf-8") == body
    assert len(body.encode("utf-8")) <= 301


# ── construction errors are loud ────────────────────────────────────────────


def test_a_duplicate_contributor_name_is_refused():
    """A second contributor under one name would shadow the first's accounting —
    the exact silent-loss class this module exists to retire. The roster is
    fixed and small, so this can only be a programming error."""

    with pytest.raises(ValueError, match="duplicate"):
        _tail(("capability", "- one", 64), ("capability", "- two", 64))


def test_a_contributor_must_declare_a_positive_budget():
    with pytest.raises(ValueError, match="positive byte budget"):
        _tail(("capability", "- one", 0))


def test_a_contributor_must_be_named():
    with pytest.raises(ValueError, match="named"):
        _tail(("  ", "- one", 64))


# ── the builder ─────────────────────────────────────────────────────────────


def test_the_builder_registers_name_and_budget_per_contributor():
    builder = VolatileTailBuilder()
    builder.add("turn_budget", "- Wall budget: ~5s left.", budget_bytes=1024)
    builder.add("capability", None, budget_bytes=4096)

    assert [c.name for c in builder.contributions] == ["turn_budget", "capability"]
    tail = builder.build()
    assert tail.content == "- Wall budget: ~5s left."
    assert tail.total_bytes == len(tail.content.encode("utf-8"))
