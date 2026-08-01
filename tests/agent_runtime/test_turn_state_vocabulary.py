"""The turn-lifecycle state vocabulary is ONE table, and it decides everything.

Wave-4 fixed the same defect twice, ~700 lines apart: "which turn states are
over" was written as a hardcoded string in a consumer instead of read from the
store that owns the states. The first spelling (``state != "interrupted"``) hid
``budget_exhausted`` from the marker synthesizer; the second hid it from the
tool-call settler. Recurrence is the finding — the shared root cause is that
consumers were free to re-spell the vocabulary.

This file is the behavioral pin for the consolidation. Every case below is
written against the PUBLIC behavior (the store functions and the CLI seams),
not against the names of the sets, so it runs identically before and after the
table-ization and proves the refactor changed no decision.

Layout:

* ``TURN_STATE_CLASSIFICATION`` — the full known universe, one row per state,
  hardcoded here on purpose. The test is the second opinion; if the runtime
  table and this table disagree, one of them is wrong and a human decides which.
* repair / retention / GC decisions, driven off that classification.
* the CLI journal seams (``mission chat`` resend + ``turn-resolve``), whose
  membership tests used to be inline literals.
* the import-time set guards (post-consolidation only — a guard that can be
  deleted without a test failing is decoration).
"""

from __future__ import annotations

import pytest

from agent_runtime.mission_chat_turns import (
    MissionChatTurnPersistOutcome,
    abandon_mission_chat_turn,
    inflight_chat_session_roots,
    mark_stale_inflight_turns_interrupted,
    mission_chat_turn_record,
    persist_mission_chat_turn,
    transition_mission_chat_turn,
)

# ---------------------------------------------------------------------------
# The known universe, and what each state means to the repair sweep.
# ---------------------------------------------------------------------------
# ``lifecycle`` is the bucket a state belongs to:
#   inflight  — an executor still owes this record a settlement; the repair
#               sweep flips it to ``interrupted`` and retention/GC protect it.
#   settling  — the reply is durable but the projection has not committed; NOT
#               repairable (the reply must never be lost to a flip) and not yet
#               terminal. This bucket was unnamed before the consolidation.
#   terminal  — settled; no repair, no operator resolution.
# ``journal`` marks the states reachable through ``transition_mission_chat_turn``
# (the exactly-once journal) as opposed to the legacy streaming vocabulary.
TURN_STATE_CLASSIFICATION = (
    # state,              lifecycle,   journal
    ("pending",           "inflight",  True),
    ("executing",         "inflight",  True),
    ("outcome_unknown",   "inflight",  True),
    ("running",           "inflight",  False),
    ("native_committed",  "settling",  True),
    ("projected",         "terminal",  True),
    ("abandoned",         "terminal",  True),
    ("budget_exhausted",  "terminal",  True),
    ("completed",         "terminal",  False),
    ("failed",            "terminal",  False),
    ("interrupted",       "terminal",  False),
)

_INFLIGHT = tuple(state for state, bucket, _ in TURN_STATE_CLASSIFICATION if bucket == "inflight")
_REPAIRABLE = _INFLIGHT

# The journal walk that reaches a given state from a fresh record. Legacy states
# are reached with a direct ``persist_mission_chat_turn`` instead.
_JOURNAL_WALK = {
    "pending": ("pending",),
    "executing": ("pending", "executing"),
    "outcome_unknown": ("pending", "executing", "outcome_unknown"),
    "native_committed": ("pending", "executing", "native_committed"),
    "projected": ("pending", "executing", "native_committed", "projected"),
    "abandoned": ("pending", "abandoned"),
    "budget_exhausted": ("pending", "executing", "budget_exhausted"),
}


def _seed(
    session_id: str,
    message_key: str,
    state: str,
    *,
    metadata: dict[str, object] | None = None,
) -> None:
    """Put one record into ``state`` through the real write paths.

    Legacy states are written straight through ``persist_mission_chat_turn``
    (their own lane): the journal refuses a legacy request over a journal state
    by design, so a legacy record must never be walked there first.
    """

    walk = _JOURNAL_WALK.get(state)
    if walk is None:
        persist_mission_chat_turn(
            session_id=session_id,
            client_message_id=message_key,
            turn_id=message_key,
            elements=[],
            state=state,
            write_ahead=True,
            metadata=metadata,
        )
        return
    for step in walk:
        transition_mission_chat_turn(
            session_id=session_id,
            client_message_id=message_key,
            turn_id=message_key,
            state=step,
            metadata=metadata,
        )


@pytest.mark.parametrize(("state", "lifecycle", "journal"), TURN_STATE_CLASSIFICATION)
def test_every_known_state_is_reachable_and_stores_as_itself(state, lifecycle, journal):
    """The universe table is honest: each state is a state the store can hold."""

    _seed("s_reach", state, state)
    record = mission_chat_turn_record(session_id="s_reach", client_message_id=state)
    assert record is not None, f"{state} unreachable through the real write paths"
    assert record["state"] == state
    assert (state in _JOURNAL_WALK) is journal


@pytest.mark.parametrize(("state", "lifecycle", "_journal"), TURN_STATE_CLASSIFICATION)
def test_repair_sweep_flips_exactly_the_inflight_bucket(state, lifecycle, _journal):
    """``mark_stale_inflight_turns_interrupted`` is the classification, live.

    An in-flight record is a corpse the sweep must settle; a settling record
    holds a durable reply a flip would destroy; a terminal record is already
    over. One decision per bucket, no exceptions.
    """

    _seed("s_repair", "m", state)
    flipped = mark_stale_inflight_turns_interrupted(
        session_id="s_repair",
        active_client_message_id=None,
    )
    record = mission_chat_turn_record(session_id="s_repair", client_message_id="m")

    if lifecycle == "inflight":
        assert flipped == ["m"]
        assert record["state"] == "interrupted"
    else:
        assert flipped == []
        assert record["state"] == state


def test_the_repair_sweep_never_touches_the_active_turn():
    """The live turn is protected by identity, not by its state."""

    for message_key in ("m_active", "m_dead"):
        _seed("s_active", message_key, "executing")

    flipped = mark_stale_inflight_turns_interrupted(
        session_id="s_active",
        active_client_message_id="m_active",
    )

    assert flipped == ["m_dead"]
    assert (
        mission_chat_turn_record(session_id="s_active", client_message_id="m_active")["state"]
        == "executing"
    )


@pytest.mark.parametrize(("state", "lifecycle", "_journal"), TURN_STATE_CLASSIFICATION)
def test_boot_sweep_reports_exactly_the_inflight_bucket(state, lifecycle, _journal):
    """The serve-boot orphan scan and the repair share one classification."""

    _seed("s_boot", "m", state, metadata={"root_chat_session_id": "s_boot"})

    roots = inflight_chat_session_roots()

    assert ("s_boot" in roots) is (lifecycle == "inflight")


# ---------------------------------------------------------------------------
# Operator resolution — the one state ``turn-resolve --action abandon`` accepts.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("state", "_lifecycle", "_journal"), TURN_STATE_CLASSIFICATION)
def test_abandon_accepts_only_the_genuinely_ambiguous_state(state, _lifecycle, _journal):
    """``outcome_unknown`` is the ONLY resolvable state.

    Notably ``budget_exhausted`` is not: the harness knows exactly why that turn
    stopped, so routing an operator to ``turn-resolve`` would be a lie.
    """

    _seed("s_abandon", "m", state)
    outcome = abandon_mission_chat_turn(
        session_id="s_abandon",
        client_message_id="m",
        turn_id="m",
        resolution_actor="operator",
        resolution_reason="pin",
    )
    record = mission_chat_turn_record(session_id="s_abandon", client_message_id="m")

    if state == "outcome_unknown":
        assert outcome is MissionChatTurnPersistOutcome.PERSISTED
        assert record["state"] == "abandoned"
    else:
        assert outcome is MissionChatTurnPersistOutcome.REJECTED_STALE_TRANSITION
        assert record["state"] == state


# ---------------------------------------------------------------------------
# Retention / GC — protection follows the same classification.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("state", "lifecycle", "_journal"), TURN_STATE_CLASSIFICATION)
def test_retention_evicts_everything_except_the_inflight_bucket(state, lifecycle, _journal, monkeypatch):
    """A live turn must never lose its write-ahead marker to the turn cap."""

    from agent_runtime import mission_chat_turns

    monkeypatch.setattr(mission_chat_turns, "_RETENTION_MAX_TURNS_PER_SESSION", 1)
    _seed("s_cap", "m_old", state)
    # The protected record being written pushes the session over the cap.
    persist_mission_chat_turn(
        session_id="s_cap",
        client_message_id="m_new",
        turn_id="m_new",
        elements=[],
        state="running",
        write_ahead=True,
    )

    survived = (
        mission_chat_turn_record(session_id="s_cap", client_message_id="m_old") is not None
    )
    assert survived is (lifecycle == "inflight")


# ---------------------------------------------------------------------------
# The journal transition table, as the CLI resend seam depends on it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "requested", "accepted"),
    [
        # A durable reply proven on replay promotes from every state the resend
        # seam recovers (executing / outcome_unknown / budget_exhausted).
        ("executing", "native_committed", True),
        ("outcome_unknown", "native_committed", True),
        ("budget_exhausted", "native_committed", True),
        # ...and from nowhere else.
        ("pending", "native_committed", False),
        ("projected", "native_committed", False),
        ("abandoned", "native_committed", False),
        # The projection walk.
        ("native_committed", "projected", True),
        ("executing", "projected", False),
        # Budget settles from the states that can still be running.
        ("pending", "budget_exhausted", True),
        ("executing", "budget_exhausted", True),
        ("outcome_unknown", "budget_exhausted", True),
        ("projected", "budget_exhausted", False),
        ("abandoned", "budget_exhausted", False),
        # A settled budget turn never resurrects to pending.
        ("budget_exhausted", "pending", False),
        ("budget_exhausted", "executing", False),
        ("budget_exhausted", "outcome_unknown", False),
        ("budget_exhausted", "abandoned", False),
    ],
)
def test_journal_transition_acceptance(current, requested, accepted):
    _seed("s_walk", "m", current)
    outcome = transition_mission_chat_turn(
        session_id="s_walk",
        client_message_id="m",
        turn_id="m",
        state=requested,
    )
    expected = (
        MissionChatTurnPersistOutcome.PERSISTED
        if accepted
        else MissionChatTurnPersistOutcome.REJECTED_STALE_TRANSITION
    )
    assert outcome is expected
    record = mission_chat_turn_record(session_id="s_walk", client_message_id="m")
    assert record["state"] == (requested if accepted else current)


# ---------------------------------------------------------------------------
# The table itself: the import-time guards, and the consumers that must read it.
# ---------------------------------------------------------------------------


def test_the_runtime_table_agrees_with_this_files_classification():
    """The two tables are independent opinions; they must not disagree.

    If this fails, someone changed one and not the other. That is the review
    conversation the wall-budget incident never got to have.
    """

    from agent_runtime import mission_chat_turns as store

    buckets = {
        "inflight": store.INFLIGHT_TURN_STATES,
        "settling": store.SETTLING_TURN_STATES,
        "terminal": store.TERMINAL_TURN_STATES,
    }
    expected = {
        bucket: {state for state, own, _ in TURN_STATE_CLASSIFICATION if own == bucket}
        for bucket in buckets
    }
    assert {name: set(value) for name, value in buckets.items()} == expected
    assert store.ALL_TURN_STATES == {state for state, _, _ in TURN_STATE_CLASSIFICATION}
    assert store.JOURNAL_TURN_STATES == {
        state for state, _, journal in TURN_STATE_CLASSIFICATION if journal
    }


@pytest.mark.parametrize(
    "mutation",
    [
        # An unclassified state — the wall-budget spinner, reintroduced.
        {"JOURNAL_TURN_STATES": frozenset({"pending", "invented"})},
        # A bucket claiming a state another bucket already owns.
        {"INFLIGHT_TURN_STATES": frozenset({"running", "pending", "executing", "outcome_unknown", "projected"})},
        # A decision set naming a state the store cannot hold (a typo that
        # would otherwise just never match anything).
        {"OPERATOR_RESOLVABLE_TURN_STATES": frozenset({"outcome_unkown"})},
        # The refusal ladder inverted.
        {"RESEND_BLOCKING_TURN_STATES": frozenset({"executing", "outcome_unknown", "abandoned"})},
        # The recoverable set drifting away from the transition table it views.
        {"REPLY_RECOVERABLE_TURN_STATES": frozenset({"executing"})},
    ],
)
def test_the_import_guards_reject_every_way_the_table_can_rot(monkeypatch, mutation):
    """A guard nothing can trip is decoration. Trip each one."""

    from agent_runtime import mission_chat_turns as store

    for name, value in mutation.items():
        monkeypatch.setattr(store, name, value)
    if "JOURNAL_TURN_STATES" in mutation:
        monkeypatch.setattr(
            store,
            "ALL_TURN_STATES",
            mutation["JOURNAL_TURN_STATES"] | store.LEGACY_TURN_STATES,
        )
    with pytest.raises(RuntimeError):
        store._guard_turn_state_vocabulary()


def test_the_cli_chat_lane_reads_the_vocabulary_instead_of_spelling_it():
    """``harness_parts/persona_commands.py`` runs in ``harness.py``'s globals.

    Every vocabulary name its body uses must therefore be RESOLVABLE there — an
    unresolvable one is a ``NameError`` on a live chat turn, not an import error
    a test run would notice. Two bindings satisfy that, and both are checked:

    * ``harness.py`` imports the name at module level (the free-name lane), or
    * the using function imports it ITSELF (``from
      agent_runtime.mission_chat_turns import ...`` inside the body). This is
      the stronger of the two — it needs no cooperation from ``harness.py`` at
      all — and is the established idiom in this file for names harness.py does
      not re-export.

    Guard the seam that makes reading the table cheaper than re-spelling it.
    """

    import ast
    from pathlib import Path

    import hermes_cli.harness as harness

    def _is_vocabulary(name: str) -> bool:
        return name.startswith("TURN_STATE_") or name.endswith("_TURN_STATES")

    source = Path(harness.__file__).parent / "harness_parts" / "persona_commands.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    # vocabulary name -> the top-level functions whose body USES it.
    used: dict[str, set[str]] = {}
    # top-level function -> the vocabulary names it imports for itself.
    locally_imported: dict[str, set[str]] = {}
    for function in (node for node in tree.body if isinstance(node, ast.FunctionDef)):
        for node in ast.walk(function):
            if isinstance(node, ast.Name) and _is_vocabulary(node.id):
                used.setdefault(node.id, set()).add(function.name)
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    bound = alias.asname or alias.name
                    if _is_vocabulary(bound):
                        locally_imported.setdefault(function.name, set()).add(bound)
    # A use outside every function can only ever be a free name.
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and _is_vocabulary(node.id):
            used.setdefault(node.id, set())
    for name, functions in used.items():
        if not functions:
            functions.add("<module>")

    assert used, "the chat lane stopped reading the turn-state vocabulary entirely"
    unresolvable = sorted(
        f"{name} (in {function})"
        for name, functions in used.items()
        for function in functions
        if not hasattr(harness, name)
        and name not in locally_imported.get(function, ())
    )
    assert not unresolvable, (
        "persona_commands.py uses vocabulary names that resolve neither through "
        f"a harness.py import nor a function-local one: {unresolvable}"
    )
