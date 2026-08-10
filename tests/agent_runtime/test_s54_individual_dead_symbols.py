"""S54 removes 30 individually-dead symbols from modules that stay LIVE.

The S43 shape, one wave on: nothing here is a module cut. Each module below
keeps real callers; what goes is one name (or one exclusive private chain)
inside it whose last reader left with the mission lane, the ``Task`` record, the
stage graph, or the packet emitter. Every entry was re-verified against the
tree immediately before the cut, by identifier-frequency scan (which catches the
quoted / ``getattr`` forms a def-site grep misses), not inherited from the
scout's snapshot.

**Two safety checks ran over the whole list before anything was cut**, because a
dead-looking Python name can still be a live contract:

* None is a **capability id**. Capability ids in this runtime are dotted strings
  (``persona.instance.return_summary``), never Python identifiers.
* None is a **Launcher-invoked verb**. The Launcher is a separate repo; all 30
  names were searched across its ``lib/``, ``tool/`` and ``docs/`` trees --
  zero hits.

**The claim that DIED on re-verification, and was NOT cut.**
``repo_context.repo_execution_context_for_task`` and
``isolated_repo_context_for_run`` were on the ruled list as a closed loop. They
are not. ``isolated_repo_context_for_run`` is the constructor
``tests/agent_runtime/test_delivery_directive.py`` builds real worktrees with --
NINE call sites -- to test the LIVE ``delivery_directive.reap_orphan_worktrees``
janitor, and its own S24 docstring records why: the suite pins two live-incident
regressions (junction severing that once emptied the backend venv, 2026-07-01;
the backend ``.env`` copy whose absence broke every read-only proof, 2026-07-03)
that are reachable only through it, and says the lane must be "retired together
or not at all". Cutting it would have deleted those regressions and forced the
janitor to be tested against hand-rolled directories instead of real worktrees.
``repo_execution_context_for_task`` shares ``_context_for_workdir`` with that
kept lane, so the whole file is excluded. The S43 KEEP stands, unamended.

**A false claim found in a docstring, cut anyway.**
``prompt_observability.load_final_model_input_for_context`` advertised itself as
the read "the launcher fetch lane and a future ``harness prompt-context
final-model-input`` CLI verb both call through". The Launcher has zero
references to it and the verb was never wired -- an advertised seam with no
caller, which is the same defect class this wave exists to retire. Cut as ruled;
the durability property its tests checked is now asserted directly off
``load_persisted_context_row``, which is live.

**A name deliberately KEPT that a reference count calls dead.**
``incidents.MODEL_INVALID_OUTPUT``. Nothing reads the NAME -- but its VALUE is
live, as a bare string literal in ``observability.py`` and two tests. Cutting
the constant would leave a live concept addressed only by a duplicated literal.
That is a single-name-authority weakness to fix at the observability end, not a
dead symbol to delete here. Pinned below so it stays visible.

**Three prior pins INVERTED, never weakened** (the S47 precedent):
``stagec_artifacts_dir`` was KEPT by S23 and again by S43 -- correctly, both
times, since only its ``_task_dir`` leaf was dead then. By S54 the directory
helper itself had no reader; the "deliberately not under ``proofs/``" note
described where it POINTED, never that anything called it.

No event contract moves at S54: the wave's contract deltas all belong to
S49/S52/S53. ``event_catalog()`` stays at 68 through this cut.

-----------------------------------------------------------------------------
MIGRATED TO ``test_tombstone_registry.py`` (2026-08-01)
-----------------------------------------------------------------------------

Two pure-ABSENCE cases left this file:

* ``test_the_dead_symbols_are_gone`` -> the registry's whole ``-- S54 --``
  section: every name in ``REMOVED`` is now an ``ATTR`` row carrying its own
  module scope and a per-cluster reason. The registry also carries
  ``events._safe_int``, which this file's table had dropped.
* ``test_the_harness_part_helper_is_gone`` -> the S42 ``ATTR`` row
  ``_archived_task_summary`` scoped to ``hermes_cli.harness``. That is the
  honest gate for an exec'd part (the S41 rule this case's own docstring cites),
  and it retires the comment-subtraction hack the source half needed —
  ``.replace("# S54 removed ...", "")`` is exactly the self-flagging class the
  registry's AST scanner exists to end.

``REMOVED`` itself STAYS, and is no longer an absence table: it is the module
list ``test_every_touched_module_still_imports`` walks, which is the assertion
that the cut left every touched module importable.

WHY THE SURVIVORS STAYED — this file is mostly KEEPs and refuted claims, none of
which a removal registry can express:

* ``test_the_live_neighbours_survive`` (``KEPT``) is the POSITIVE half; a
  tombstone table has no positive form.
* ``test_the_repo_context_worktree_lane_was_NOT_cut`` and
  ``test_the_delivery_directive_janitor_still_builds_real_worktrees`` pin the
  scout claim that DIED, including the wire from the janitor suite to the kept
  constructor.
* ``test_the_kept_incident_constant_is_addressed_by_value_somewhere_live``
  records why ``incidents.MODEL_INVALID_OUTPUT`` is deliberately NOT a
  tombstone — its value is live as a bare literal in ``observability.py``.
* ``test_s54_moved_no_event_contract`` is a count pin against S15's single
  authority.
"""

from __future__ import annotations

import importlib

import pytest


#: module -> the names S54 removed from it. The ABSENCE assertion moved to
#: ``test_tombstone_registry`` (see the header); this table survives as the
#: module set ``test_every_touched_module_still_imports`` walks, and as the
#: wave's written record of which name left which module.
REMOVED = {
    "agent_runtime.board_order": ("needs_rebalance",),
    "agent_runtime.checkpoint": ("discover_classes",),
    "agent_runtime.decision_contract_registry": (
        "all_decision_contracts",
        "role_shape_ids",
        "object_contract",
    ),
    "agent_runtime.dirty_state": ("no_product_edit_dirty_check",),
    "agent_runtime.errors": ("LegacyOrchestratorRemoved",),
    "agent_runtime.event_rotation": ("live_base_offset",),
    "agent_runtime.events": (
        "archive_task_events",
        "compact_archived_task_events",
        "_safe_event_task_filename",
        "_line_is_compacted_event",
    ),
    "agent_runtime.flow_graph": ("desired_parents_by_agent",),
    "agent_runtime.incidents": (
        "classify_exception",
        "IncidentClassification",
        "_safe_budget_summary",
        "_safe_exception_summary",
    ),
    "agent_runtime.locks": ("tick_lock",),
    "agent_runtime.paths": ("stagec_artifacts_dir",),
    "agent_runtime.profile_context": (
        "current_profile_context_rows",
        "mcp_owner_profile_name",
        "MCP_SERVER_PERSONA_OWNERS",
    ),
    "agent_runtime.profile_readiness": ("_missing_skill_names",),
    "agent_runtime.prompt_observability": (
        "load_final_model_input_for_context",
        "_mission_chat_template_prompt_chars",
    ),
    "agent_runtime.resolution": ("assert_pinned", "_normalized_path"),
    "agent_runtime.skill_promotion": ("promotion_provenance",),
    "agent_runtime.state_patches": ("emit_task_refresh",),
}

#: module -> names on the SAME module (often the same line) that must survive.
KEPT = {
    "agent_runtime.board_order": ("rebalance", "MAX_KEY_LENGTH"),
    "agent_runtime.checkpoint": ("ENTITY_CLASSES", "ENTITY_CLASS_NAMES", "build_checkpoint"),
    "agent_runtime.decision_contract_registry": (
        "contract_manifest",
        "event_catalog",
        "validate_event_payload",
    ),
    "agent_runtime.dirty_state": ("build_dirty_state",),
    "agent_runtime.events": ("EventLog", "operator_event_summary", "event_with_operator_summary"),
    "agent_runtime.incidents": ("MODEL_INVALID_OUTPUT", "CRITICAL_INCIDENT_KINDS"),
    "agent_runtime.locks": ("task_lock", "events_lock"),
    "agent_runtime.paths": ("store_root",),
    "agent_runtime.profile_readiness": ("_missing_skill_ids", "_resolve_skill_names"),
    "agent_runtime.prompt_observability": ("load_persisted_context_row", "snapshot_prompt_observability"),
    "agent_runtime.resolution": ("RuntimeResolution",),
}


@pytest.mark.parametrize("dotted", sorted(KEPT))
def test_the_live_neighbours_survive(dotted: str):
    module = importlib.import_module(dotted)
    assert [name for name in KEPT[dotted] if not hasattr(module, name)] == []


def test_the_repo_context_worktree_lane_was_NOT_cut():
    """The scout claim that died, pinned so it is not re-attempted.

    ``isolated_repo_context_for_run`` was ruled a closed loop. It is not: it is
    the fixture constructor the delivery-directive janitor suite builds real
    worktrees with, and its S24 docstring names two live-incident regressions
    reachable only through it. The whole lane stays.
    """

    from agent_runtime import repo_context

    for name in (
        "repo_execution_context_for_task",
        "isolated_repo_context_for_run",
        "_worktree_token",
        "_ensure_isolated_worktree",
        "existing_run_worktrees",
        "remove_harness_worktree_for_repo",
    ):
        assert hasattr(repo_context, name), name


def test_the_delivery_directive_janitor_still_builds_real_worktrees(
    isolate_agent_runtime_root,
):
    """The wire, not just the symbol: the janitor suite must still reach the
    kept constructor. If this import ever dies, the two live-incident
    regressions died with it.

    REPLACED 2026-08-09. The assertion here used to be::

        assert "worktree" in inspect.getsource(reap_orphan_worktrees)

    which is **permanently green by construction** — the substring ``worktree``
    is in the function's own ``def`` line, so no edit to its body, however
    destructive, could ever fail it. That is not a weak test; it is a
    zero-information assertion sitting where the real one goes, and it read as
    coverage to every audit that walked past it.

    What the janitor actually has to do is REACH the kept ``repo_context``
    worktree API. So it is now RUN — ``dry_run=True``, against an isolated root,
    which inspects and reports without removing anything — and its typed result
    asserted. A gutted body returns the wrong shape or raises; neither survives
    this."""

    from pathlib import Path

    source = Path(__file__).resolve().parent.joinpath("test_delivery_directive.py").read_text(encoding="utf-8")
    assert "isolated_repo_context_for_run" in source
    from agent_runtime import delivery_directive

    assert callable(delivery_directive.reap_orphan_worktrees)

    result = delivery_directive.reap_orphan_worktrees(dry_run=True)

    # The typed contract of the janitor — only a body that still walks the
    # worktree inventory can produce it.
    assert set(result) == {
        "reaped",
        "kept",
        "capture_dir",
        "dry_run",
        "include_legacy_temp",
    }, result
    assert result["dry_run"] is True, "dry_run must never be silently downgraded"
    assert isinstance(result["reaped"], list)
    assert isinstance(result["kept"], list)
    # The capture directory is where the "nothing is ever deleted with an
    # uncaptured diff" guarantee lands. A janitor that no longer knows where to
    # put a patch has lost the protection the two live incidents bought.
    assert result["capture_dir"].endswith("wt_reaped_patches")


def test_the_kept_incident_constant_is_addressed_by_value_somewhere_live():
    """``MODEL_INVALID_OUTPUT`` is KEPT despite zero name-references, because
    its VALUE is live. This asserts the reason, so the keep cannot rot into an
    unexplained exception."""

    import inspect

    from agent_runtime import incidents, observability

    assert incidents.MODEL_INVALID_OUTPUT == "model_invalid_output"
    assert "model_invalid_output" in inspect.getsource(observability)


def test_s54_moved_no_event_contract():
    """Negative gate: the wave's contract deltas belong to S49/S52/S53.

    S56 correction (2026-08-01), the same housekeeping S49 applied to
    test_s44_role_envelope_family_removal.py: the body carried ``== 68``, a
    SECOND copy of the absolute catalog size inside a test whose whole claim is
    that S54 moved NO contract. S56 legitimately moved the count (58, the ten
    ``worker_session.*`` types deregistered) and broke this line, which is
    exactly the failure mode the "one number to maintain" rule exists to stop.
    The literal is dropped; the assertion now defers to the single authority,
    ``test_s15_event_contract_pruning.SURVIVING_EVENT_COUNT``, which is the real
    invariant here — this file's cut must not make the catalog disagree with it.
    """

    from agent_runtime.decision_contract_registry import event_catalog
    from tests.agent_runtime.test_s15_event_contract_pruning import SURVIVING_EVENT_COUNT

    assert len(event_catalog()) == SURVIVING_EVENT_COUNT


def test_every_touched_module_still_imports():
    for dotted in sorted(set(REMOVED) | set(KEPT)):
        importlib.import_module(dotted)
