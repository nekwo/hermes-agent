"""The mission-chat turn-outcome vocabulary is ONE table, and it is the wire.

``execution_state`` and ``error_kind`` are read by 16 read-only Launcher
consumers that match on the exact spellings. Before this module they were bare
literals inside ``hermes_cli/harness_parts/persona_commands.py`` — a file that
is ``exec``'d rather than imported, so nothing could import the vocabulary to
check it and a typo was a silent wire break on both ends.

Layout, mirroring ``test_turn_state_vocabulary``:

* ``WIRE_VALUES`` — the full spelling of every member, hardcoded here on
  purpose. This file is the SECOND OPINION: if it and the enum disagree, one of
  them is wrong and a human decides which. Changing a row here is changing the
  wire.
* the classifier as a 2xN truth table.
* the import-time guards (a guard that can be deleted without a test failing is
  decoration).
* the consumer seam: every member is actually spelled by the CLI lane, and no
  bare literal survived the migration.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from agent_runtime.mission_chat_outcome import (
    ChatErrorKind,
    DELEGATED_ERROR_KIND_SOURCES,
    ExecutionState,
    FAILURE_EXECUTION_STATES,
    MISSION_CHAT_EXIT_FAILURE,
    MISSION_CHAT_EXIT_OK,
    OK_EXECUTION_STATES,
    TurnOutcome,
    classify_turn_failure,
)

# ---------------------------------------------------------------------------
# The wire, spelled out.
# ---------------------------------------------------------------------------
EXECUTION_STATE_WIRE = {
    "REFUSED": "refused",
    "REJECTED": "rejected",
    "FAILED": "failed",
    "BLOCKED": "blocked",
    "BUDGET_EXHAUSTED": "budget_exhausted",
    "COMPLETED": "completed",
    # S70 removed QUEUED: its only emitter was the retired free-floating
    # assignment queue envelope (tombstone registry, wave s70).
}
CHAT_ERROR_KIND_WIRE = {
    "INVALID_REQUEST": "invalid_request",
    "UNSUPPORTED_PERSONA": "unsupported_persona",
    "PERSONA_INSTANCE_NOT_FOUND": "persona_instance_not_found",
    "PERSONA_INSTANCE_MISMATCH": "persona_instance_mismatch",
    "RETIRED_PERSONA_INSTANCE": "retired_persona_instance",
    "INVALID_CHAT_MODEL_OVERRIDE": "invalid_chat_model_override",
    "UNKNOWN_CHAT_SESSION": "unknown_chat_session",
    "FOREIGN_CHAT_SESSION": "foreign_chat_session",
    "CHAT_BUSY": "chat_busy",
    "CHAT_SESSION_DB_UNAVAILABLE": "chat_session_db_unavailable",
    "CHAT_SESSION_PERSIST_FAILED": "chat_session_persist_failed",
    "CHAT_MODEL_OVERRIDE_PERSIST_FAILED": "chat_model_override_persist_failed",
    "CHAT_PROJECTION_INCOMPLETE": "chat_projection_incomplete",
    # S70 removed CHAT_TRANSCRIPT_PERSIST_FAILED / POST_TURN_PERSIST_FAILED
    # with the free-floating lane, their only producer (registry wave s70).
    "CHAT_TURN_BUDGET_EXHAUSTED": "chat_turn_budget_exhausted",
    "CHAT_TURN_OUTCOME_UNKNOWN": "chat_turn_outcome_unknown",
    "CHAT_TURN_NOT_SUBMITTED": "chat_turn_not_submitted",
    "CHAT_TURN_RESOLUTION_MISMATCH": "chat_turn_resolution_mismatch",
}


@pytest.mark.parametrize(("member", "wire"), sorted(EXECUTION_STATE_WIRE.items()))
def test_execution_state_wire_spelling(member, wire):
    assert getattr(ExecutionState, member).value == wire


@pytest.mark.parametrize(("member", "wire"), sorted(CHAT_ERROR_KIND_WIRE.items()))
def test_chat_error_kind_wire_spelling(member, wire):
    assert getattr(ChatErrorKind, member).value == wire


def test_the_enums_hold_exactly_the_declared_members():
    """A member added to the enum without a row here is an undeclared wire change."""

    assert {member.name for member in ExecutionState} == set(EXECUTION_STATE_WIRE)
    assert {member.name for member in ChatErrorKind} == set(CHAT_ERROR_KIND_WIRE)


@pytest.mark.parametrize(
    "value",
    sorted(set(EXECUTION_STATE_WIRE.values()) | set(CHAT_ERROR_KIND_WIRE.values())),
)
def test_members_serialize_as_their_bare_string(value):
    """``StrEnum`` members must reach the wire as the plain string, on BOTH
    encoders the chat lane uses (``emit_json`` for the CLI envelope, raw
    ``json.dumps`` for the streamed ``chat.final`` frame)."""

    from agent_runtime.cli_format import emit_json

    member = next(
        item
        for group in (ExecutionState, ChatErrorKind)
        for item in group
        if item.value == value
    )
    assert json.loads(json.dumps({"k": member}))["k"] == value
    assert json.loads(emit_json({"k": member}))["k"] == value
    assert member == value
    assert str(member) == value


# ---------------------------------------------------------------------------
# classify_turn_failure — the truth table
# ---------------------------------------------------------------------------
class _WallBudgetTrip(Exception):
    """Stand-in shaped like the runner's wall-budget signal."""


def _budget_error(*, wall_budget):
    from agent_runtime.profile_runner import RunBudgetExceeded

    return RunBudgetExceeded("budget exhausted", wall_budget=wall_budget)


# (exception factory, provider_submitted) -> (execution_state, error_kind)
#
# The middle rows are the 2026-07-26 incident: a wall-budget death reported as
# ``chat_turn_outcome_unknown`` ("I cannot prove what the provider did") froze
# both ends of a relay and cost two manual ``turn-resolve --action abandon``
# calls plus a full re-brief. A budget death is the opposite of ambiguous.
FAILURE_TABLE = (
    # a wall-budget trip AFTER the provider boundary settles as terminal
    (
        lambda: _budget_error(wall_budget={"trigger": "wall_budget_hard_wall"}),
        True,
        ExecutionState.BUDGET_EXHAUSTED,
        ChatErrorKind.CHAT_TURN_BUDGET_EXHAUSTED,
    ),
    # ...but a NON-wall budget trip (api calls / tokens / read-search loop)
    # carries no ``wall_budget`` projection and stays genuinely ambiguous
    (
        lambda: _budget_error(wall_budget=None),
        True,
        ExecutionState.BLOCKED,
        ChatErrorKind.CHAT_TURN_OUTCOME_UNKNOWN,
    ),
    (
        lambda: _budget_error(wall_budget={}),
        True,
        ExecutionState.BLOCKED,
        ChatErrorKind.CHAT_TURN_OUTCOME_UNKNOWN,
    ),
    # any other post-boundary explosion is ambiguous too
    (
        lambda: RuntimeError("provider exploded"),
        True,
        ExecutionState.BLOCKED,
        ChatErrorKind.CHAT_TURN_OUTCOME_UNKNOWN,
    ),
    (
        lambda: _WallBudgetTrip("looks budgety, is not"),
        True,
        ExecutionState.BLOCKED,
        ChatErrorKind.CHAT_TURN_OUTCOME_UNKNOWN,
    ),
    # nothing crossed the boundary: the turn is retryable on the SAME id
    (
        lambda: RuntimeError("blew up assembling the request"),
        False,
        ExecutionState.FAILED,
        ChatErrorKind.CHAT_TURN_NOT_SUBMITTED,
    ),
    # ...including a budget-shaped exception, which cannot have tripped a wall
    # the runner never started enforcing
    (
        lambda: _budget_error(wall_budget={"trigger": "wall_budget_hard_wall"}),
        False,
        ExecutionState.FAILED,
        ChatErrorKind.CHAT_TURN_NOT_SUBMITTED,
    ),
)


@pytest.mark.parametrize(
    ("make_exc", "provider_submitted", "state", "kind"),
    FAILURE_TABLE,
    ids=[f"{index}" for index in range(len(FAILURE_TABLE))],
)
def test_classify_turn_failure(make_exc, provider_submitted, state, kind):
    outcome = classify_turn_failure(
        make_exc(), provider_submitted=provider_submitted
    )
    assert outcome == TurnOutcome(state, kind, MISSION_CHAT_EXIT_FAILURE)


def test_every_failure_state_and_kind_is_reachable_from_the_table():
    """No classifier row is decoration, and none of its vocabulary is dead."""

    produced_states = {row[2] for row in FAILURE_TABLE}
    produced_kinds = {row[3] for row in FAILURE_TABLE}
    assert produced_states <= FAILURE_EXECUTION_STATES | {
        ExecutionState.BUDGET_EXHAUSTED
    }
    assert produced_kinds <= set(ChatErrorKind)
    # A failure classification is never the success verdict.
    assert not (produced_states & OK_EXECUTION_STATES)


def test_a_failed_turn_always_exits_two():
    for make_exc, submitted, _, _ in FAILURE_TABLE:
        outcome = classify_turn_failure(make_exc(), provider_submitted=submitted)
        assert outcome.exit_code == MISSION_CHAT_EXIT_FAILURE
    assert MISSION_CHAT_EXIT_OK == 0
    assert MISSION_CHAT_EXIT_FAILURE == 2


def test_turn_outcome_is_immutable():
    outcome = classify_turn_failure(RuntimeError("x"), provider_submitted=True)
    with pytest.raises(Exception):
        outcome.execution_state = ExecutionState.COMPLETED  # type: ignore[misc]


# ---------------------------------------------------------------------------
# import-time guards
# ---------------------------------------------------------------------------
def test_the_import_time_guard_runs_and_passes():
    from agent_runtime import mission_chat_outcome

    mission_chat_outcome._guard_turn_outcome_vocabulary()


def test_an_unclassified_execution_state_is_rejected(monkeypatch):
    from agent_runtime import mission_chat_outcome as module

    # Empty the OK bucket so COMPLETED is left in no bucket at all — the guard
    # must reject the unclassified state. (S70 removed QUEUED, the member this
    # test originally used as the sole-OK example.)
    monkeypatch.setattr(module, "OK_EXECUTION_STATES", frozenset())
    with pytest.raises(RuntimeError, match="no verdict bucket"):
        module._guard_turn_outcome_vocabulary()


def test_an_error_kind_with_no_family_is_rejected(monkeypatch):
    from agent_runtime import mission_chat_outcome as module

    monkeypatch.setitem(
        module._ERROR_KIND_FAMILIES,
        "CHAT_ROOT_ERROR_KINDS",
        frozenset({ChatErrorKind.CHAT_BUSY}),
    )
    with pytest.raises(RuntimeError, match="no family"):
        module._guard_turn_outcome_vocabulary()


def test_a_state_in_two_buckets_is_rejected(monkeypatch):
    from agent_runtime import mission_chat_outcome as module

    monkeypatch.setattr(
        module,
        "OK_EXECUTION_STATES",
        module.OK_EXECUTION_STATES | {ExecutionState.FAILED},
    )
    with pytest.raises(RuntimeError, match="in both"):
        module._guard_turn_outcome_vocabulary()


def test_a_delegated_kind_may_not_also_be_owned_here():
    """The three sibling decision modules keep their own ``error_kind``.

    Re-spelling one of their values as a member here would be the second
    authority this module exists to retire, so the guard refuses it — and the
    delegation list must stay truthful about who produces what.
    """

    from agent_runtime import relay_policy, target_policy

    owned = {str(kind) for kind in ChatErrorKind}
    for values in DELEGATED_ERROR_KIND_SOURCES.values():
        assert not (set(values) & owned)

    relay_source = Path(relay_policy.__file__).read_text(encoding="utf-8")
    for value in DELEGATED_ERROR_KIND_SOURCES["agent_runtime.relay_policy"]:
        assert f'"{value}"' in relay_source
    assert target_policy.AMBIGUOUS_TARGET in DELEGATED_ERROR_KIND_SOURCES[
        "agent_runtime.target_policy"
    ]


# ---------------------------------------------------------------------------
# the consumer seam
# ---------------------------------------------------------------------------
def _persona_commands_tree() -> ast.Module:
    import hermes_cli.harness as harness

    source = Path(harness.__file__).parent / "harness_parts" / "persona_commands.py"
    return ast.parse(source.read_text(encoding="utf-8"))


#: The single deliberate literal left in the CLI lane, and why. A default
#: argument is evaluated when the ``def`` executes, and ``persona_commands.py``
#: is EXEC'd into ``harness.py``'s globals — so the enum name cannot be bound
#: yet without a module-level import there, which is the namespace collision
#: the exec'd-part discipline forbids. Pinned to the member instead.
DECLARED_LITERAL_EXCEPTIONS = {
    ("_retired_persona_instance_refusal", "error_kind"): (
        ChatErrorKind.RETIRED_PERSONA_INSTANCE
    ),
}


#: The wire keys this gate polices.
_VOCABULARY_KEYS = ("execution_state", "error_kind")


def _vocabulary_literals(tree: ast.AST) -> list[tuple[str, str, str, int, str]]:
    """Every place this tree spells a turn-outcome value as a STRING LITERAL.

    Returns ``(form, enclosing-def-or-'<module>', key, lineno, value)``.

    SIX forms, because the key is not the unit of meaning — the same wire value
    can be written six ways and only two of them used to be inspected. Measured
    on 2026-08-20 by planting one literal per form in ``persona_commands.py``
    and running this gate:

        dict literal          SEEN (red)
        call keyword          SEEN (red)
        kwonly default        BLIND (green)
        positional default    BLIND (green)
        subscript store       BLIND (green)
        bare/annotated assign BLIND (green)

    The kwonly-default hole was not hypothetical: ``persona_commands.py`` uses
    exactly that form at ``_retired_persona_instance_refusal``, and it is the
    file's own DECLARED exception. So the gate could not see the one spelling
    the lane demonstrably prefers, and ``DECLARED_LITERAL_EXCEPTIONS``' claim to
    hold "the single deliberate literal left in the CLI lane" was a claim
    nothing could check — a second one would have been structurally invisible.

    KNOWN BOUND, stated rather than hidden (a gate that overclaims its reach is
    the defect it exists to stop): this reads SHAPE, not dataflow.
    ``_X = "failed"`` at module scope followed by
    ``payload["execution_state"] = _X`` is invisible, and was measured invisible
    alongside the six above. Closing it needs constant propagation; the six
    syntactic forms are what a re-spelling actually looks like when someone
    writes one by hand.
    """

    owner: dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                owner.setdefault(inner, node.name)

    found: list[tuple[str, str, str, int, str]] = []

    def record(form: str, node: ast.AST, key: str, value: ast.AST) -> None:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            found.append((form, owner.get(node, "<module>"), key, node.lineno, value.value))

    for node in ast.walk(tree):
        # 1. {"error_kind": "..."}
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in _VOCABULARY_KEYS:
                    for child in ast.walk(value):
                        record("dict-literal", key, key.value, child)
        # 2. f(error_kind="...")
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in _VOCABULARY_KEYS:
                    record("call-keyword", node, keyword.arg, keyword.value)
        # 3/4. def f(error_kind="...") and def f(*, error_kind="...")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            positional = args.posonlyargs + args.args
            pairs = list(zip(positional[len(positional) - len(args.defaults):], args.defaults))
            pairs += list(zip(args.kwonlyargs, args.kw_defaults))
            for arg, default in pairs:
                if default is not None and arg.arg in _VOCABULARY_KEYS:
                    record("parameter-default", arg, arg.arg, default)
        # 5. payload["error_kind"] = "..."
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value in _VOCABULARY_KEYS
                ):
                    record("subscript-store", target, target.slice.value, node.value)
                if isinstance(target, ast.Name) and target.id in _VOCABULARY_KEYS:
                    record("name-assign", target, target.id, node.value)
        # 6. error_kind: str = "..."
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            target = node.target
            if isinstance(target, ast.Name) and target.id in _VOCABULARY_KEYS:
                record("annotated-assign", target, target.id, node.value)
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Constant)
                and target.slice.value in _VOCABULARY_KEYS
            ):
                record("annotated-assign", target, target.slice.value, node.value)

    return found


def test_the_cli_lane_spells_no_execution_state_or_error_kind_literal():
    """Every wire value is the enum member, not a string that happens to match."""

    offenders = sorted(
        f"{key}={value!r} ({form}) @ L{lineno} in {where}"
        for form, where, key, lineno, value in _vocabulary_literals(_persona_commands_tree())
        if DECLARED_LITERAL_EXCEPTIONS.get((where, key)) is None
    )
    assert not offenders, (
        "persona_commands.py re-spells the turn-outcome vocabulary instead of "
        f"reading agent_runtime.mission_chat_outcome: {offenders}"
    )


def test_the_detector_sees_every_spelling_and_stays_quiet_on_the_enum():
    """Anti-vacuity. The gate above asserts an EMPTY list, which is the shape
    that keeps passing quietly once the reader stops reading — so the reader is
    pinned here, independently of the tree, in each form it must cover.
    """

    planted = ast.parse(
        'def a():\n'
        '    return {"error_kind": "chat_busy"}\n'
        'def b():\n'
        '    return dict(error_kind="chat_busy")\n'
        'def c(*, error_kind: str = "chat_busy"):\n'
        '    return error_kind\n'
        'def d(execution_state="failed"):\n'
        '    return execution_state\n'
        'def e(payload):\n'
        '    payload["execution_state"] = "failed"\n'
        'def f():\n'
        '    error_kind: str = "chat_busy"\n'
        '    return error_kind\n'
    )
    forms = {form for form, _where, _key, _lineno, _value in _vocabulary_literals(planted)}
    assert forms == {
        "dict-literal",
        "call-keyword",
        "parameter-default",
        "subscript-store",
        "annotated-assign",
    }, sorted(forms)

    correct = ast.parse(
        "def a():\n"
        '    return {"error_kind": ChatErrorKind.CHAT_BUSY}\n'
        "def b(*, error_kind: str = ChatErrorKind.CHAT_BUSY):\n"
        "    return error_kind\n"
        "def c(payload):\n"
        '    payload["execution_state"] = ExecutionState.FAILED\n'
        "def d(payload, error_kind):\n"
        '    return {"error_kind": error_kind, "note": "a plain string elsewhere"}\n'
    )
    assert _vocabulary_literals(correct) == []


def test_the_declared_literal_exception_still_matches_its_member():
    """The one literal left standing may not drift from the enum — and must
    still BE there.

    The loop below carried every assertion in this test and iterated a lookup
    that misses silently: if ``_retired_persona_instance_refusal`` were renamed
    or deleted, ``DECLARED_LITERAL_EXCEPTIONS.get(...)`` returns ``None`` for
    every function, the body never runs, and an exemption for a site that no
    longer exists sits here reading like a reviewed decision. An unmatched
    exemption is the ``test_allowlist_has_not_rotted`` failure one file over. So
    the sites are collected first and the coverage is asserted OUTSIDE the loop.
    """

    matched: set[tuple[str, str]] = set()
    for function in (
        node
        for node in _persona_commands_tree().body
        if isinstance(node, ast.FunctionDef)
    ):
        for arg, default in zip(
            function.args.kwonlyargs, function.args.kw_defaults
        ):
            expected = DECLARED_LITERAL_EXCEPTIONS.get((function.name, arg.arg))
            if expected is None:
                continue
            matched.add((function.name, arg.arg))
            assert isinstance(default, ast.Constant)
            assert default.value == expected.value, (
                f"{function.name}({arg.arg}=) drifted from {expected!r}"
            )

    unmatched = sorted(set(DECLARED_LITERAL_EXCEPTIONS) - matched)
    assert not unmatched, (
        "these DECLARED_LITERAL_EXCEPTIONS name no keyword-only parameter in "
        f"persona_commands.py any more: {unmatched}. The site was renamed or "
        "deleted; delete the entry in the same commit. An exemption that "
        "matches nothing grants cover to whatever is next given that name."
    )


def test_every_owned_member_has_a_producer_in_the_cli_lane():
    """A member nothing produces is dead vocabulary; the enum is not a wishlist."""

    tree = _persona_commands_tree()
    spelled = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in ("ExecutionState", "ChatErrorKind")
    }
    # ...plus the classifier, which produces three of them without naming them.
    for row in FAILURE_TABLE:
        spelled.add(row[2].name)
        spelled.add(row[3].name)
    # ...plus the declared literal exception.
    for member in DECLARED_LITERAL_EXCEPTIONS.values():
        spelled.add(member.name)

    unproduced = sorted(
        member.name
        for group in (ExecutionState, ChatErrorKind)
        for member in group
        if member.name not in spelled
    )
    assert not unproduced, (
        "turn-outcome member(s) nothing in the CLI lane can produce: "
        f"{unproduced}"
    )
