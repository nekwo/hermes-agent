"""S42 cuts nine helpers that outlived every caller.

Two clusters, one shape: a function whose last real caller left with the mission
lane, kept alive only by an import binding (S41 removed those) or by a
reachability root a witness test had to invent to keep its own unreachable-set
honest.

``agent_runtime/scope_control.py`` — the module stays; it is live on
``decision_contracts``' payload validation. What goes is its lookup half:

* ``untriaged_issue_discoveries`` — S28 removed ``build_observability``'s
  ``tasks`` parameter, which was its ONLY production caller. S28 said so in
  writing and left it for "a future reachability pass"; this is that pass.
* ``find_discovery_task`` — walked a ``TaskStore`` for a discovery id. S8
  reduced ``TaskStore`` to a zero-surface stub, and ``hermes_cli/harness.py``
  bound the name without ever calling it. S41 removed the binding; nothing is
  left.

The two harness command parts (``exec``'d into ``hermes_cli.harness`` globals,
so the namespace is the gate):

* ``board.py::_resolve_board_id_for_read`` — one hit in the whole harness scope,
  its own ``def``.
* ``runtime_commands.py`` — ``_event_value``, ``_task_events``,
  ``_clear_task_recovery_markers``, ``_safe_operator_text``,
  ``_safe_issue_summary``, and the ``_incident_history_row`` /
  ``_incident_cursor_ts`` pair (the cursor helper's only caller was the row
  helper, so they are one unit). Every one addressed a mission record — task
  events, task recovery markers, issue summaries, incident history — that S8/S9
  removed.

No event contract moves: ``event_catalog()`` stays at 88.
"""

from __future__ import annotations

import importlib

from agent_runtime import scope_control


REMOVED_SCOPE_CONTROL = (
    "untriaged_issue_discoveries",
    "find_discovery_task",
)

REMOVED_HARNESS_PART_HELPERS = (
    # board.py
    "_resolve_board_id_for_read",
    # runtime_commands.py
    "_event_value",
    "_task_events",
    "_clear_task_recovery_markers",
    "_safe_operator_text",
    "_safe_issue_summary",
    "_incident_history_row",
    "_incident_cursor_ts",
)


def test_the_scope_control_lookup_half_is_gone():
    assert [name for name in REMOVED_SCOPE_CONTROL if hasattr(scope_control, name)] == []


def test_the_dead_harness_part_helpers_are_gone():
    harness = importlib.import_module("hermes_cli.harness")
    assert [
        name for name in REMOVED_HARNESS_PART_HELPERS if hasattr(harness, name)
    ] == []


def test_the_validation_half_of_scope_control_is_untouched():
    """Negative gate: the module is LIVE and must stay reachable from
    ``decision_contracts``."""

    from agent_runtime import decision_contracts

    assert decision_contracts.validate_discovery_payload is scope_control.validate_discovery_payload
    assert decision_contracts.validate_triage_payload is scope_control.validate_triage_payload
    for name in (
        "CLASSIFICATIONS",
        "RELATIONSHIP_HINTS",
        "SEVERITIES",
        "normalize_severity",
        "normalize_relationship",
        "list_of_strings",
    ):
        assert hasattr(scope_control, name), name


def test_the_scope_control_docstring_no_longer_claims_retired_importers():
    """The module docstring advertised "validation + lookup" and named
    ``observability`` and ``harness`` as live importers of the two removed
    names. A docstring that describes a retired contract is how the next pass
    re-derives a wrong answer, so the claim has to move with the code.

    The gate is the CLAIM, not the words: the rewritten docstring still mentions
    both modules while explaining why neither is an importer any more.
    """

    doc = scope_control.__doc__ or ""
    assert "validation + lookup" not in doc
    assert "which is all the live importers use" not in doc
    assert "exactly one importer" in doc


def test_the_live_neighbours_of_each_removed_part_helper_survive():
    """Negative gate: the command verbs these helpers sat next to still exist."""

    harness = importlib.import_module("hermes_cli.harness")
    for name in (
        "_cmd_status",
        "_cmd_worktree_reap",
        # S54 INVERSION: ``_archived_task_summary`` left this live-neighbour
        # list. S42 kept it as a surviving formatter beside the helpers it cut;
        # by S54 it had no caller of its own and went. Asserted absent below.
        "_cmd_board_list",
    ):
        assert hasattr(harness, name), name
    assert not hasattr(harness, "_archived_task_summary")
