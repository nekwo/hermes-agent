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

=============================================================================
MIGRATED to ``tests/agent_runtime/test_tombstone_registry.py`` (2026-08-01)
=============================================================================

BOTH absence tables moved, and the tests that read them went with them. Every
name this wave cut is a pure ``module.name`` absence, which is exactly the
``Form.ATTR`` row the registry owns:

* ``scope_control.untriaged_issue_discoveries`` / ``find_discovery_task`` — one
  cluster row scoped to ``agent_runtime.scope_control``.
* the nine harness-part helpers (``_resolve_board_id_for_read``,
  ``_event_value``, ``_task_events``, ``_clear_task_recovery_markers``,
  ``_safe_operator_text``, ``_safe_issue_summary``, ``_incident_history_row``,
  ``_incident_cursor_ts``, and S54's ``_archived_task_summary``) — one cluster
  row scoped to ``hermes_cli.harness``. The scope is unchanged: the parts are
  ``exec``'d into that module's globals, so the NAMESPACE is still the gate.

WHAT STAYED, AND WHY EACH IS NOT A ROW. The three survivors are not absence
assertions at all:

* ``test_the_validation_half_of_scope_control_is_untouched`` is a KEEP pin, and
  an identity one — it asserts ``decision_contracts.validate_discovery_payload
  IS scope_control.validate_discovery_payload``, i.e. that the live lane still
  routes through this module rather than through a second copy.
* ``test_the_scope_control_docstring_no_longer_claims_retired_importers`` gates
  a CLAIM in prose. The registry's scanner strips docstrings by construction, so
  it is structurally incapable of asserting this and always will be.
* ``test_the_live_neighbours_of_each_removed_part_helper_survive`` is a KEEP pin
  carrying one INVERTED arm (S54's ``_archived_task_summary``). Inverted pins
  record a reversal and stay whole where the reversal happened; dissolving one
  into a row would lose which wave falsified which earlier ruling.
"""

from __future__ import annotations

import importlib

from agent_runtime import scope_control


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
