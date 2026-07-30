"""S20 retires the caller-free small modules left behind by the mission lane.

The wave opened on five candidates. A repo-wide text sweep of each module name
**and every public symbol** found live production call sites for four of them, so
only ``agent_runtime.role_sessions`` -- reachable from nothing but its own
dedicated test -- is removed here.

The four survivors are pinned below **with the call site that keeps them alive**,
so a later bare-word sweep cannot take them out on the strength of this wave's
title. ``repo_context`` is likewise pinned: its worktree *inventory* half feeds
``delivery_directive.reap_orphan_worktrees`` and must never be swept with the
worktree *creator* half.
"""

from __future__ import annotations

import importlib.util

import pytest


REMOVED_MODULES = ("agent_runtime.role_sessions",)


def test_removed_modules_are_gone():
    assert [name for name in REMOVED_MODULES if importlib.util.find_spec(name) is not None] == []


def test_importing_a_removed_module_raises_module_not_found():
    for name in REMOVED_MODULES:
        with pytest.raises(ModuleNotFoundError):
            __import__(name)


def test_dev_discipline_survives_on_its_progress_telemetry_caller():
    """``progress.RunProgressSink`` merges telemetry through it on every run event."""

    from agent_runtime import dev_discipline, progress

    assert progress.update_progress_telemetry is dev_discipline.update_progress_telemetry


def test_simplified_contract_survives_on_its_public_decision_type_callers():
    """Operator channels and the store project decision types through it.

    Retargeted in S27. This pin originally named ``snapshot`` as a third caller,
    but that was an IMPORT with no surviving call: ``8fe3d6687`` cut the goal/task
    projection cluster, leaving ``snapshot._public_decision_value`` reachable only
    from the run-projection island, and S27 removed that island and the import
    with it. The pin now gates on the two modules that actually CALL the
    normalizer, so it cannot pass on a dangling import again.
    """

    import inspect

    from agent_runtime import operator_channels, simplified_contract, snapshot, store

    assert operator_channels.public_decision_type_value is simplified_contract.public_decision_type_value
    assert store.public_decision_type_value is simplified_contract.public_decision_type_value
    # A live call site, not just a bound name.
    for module in (operator_channels, store):
        assert "public_decision_type_value(" in inspect.getsource(module), module.__name__
    assert not hasattr(snapshot, "public_decision_type_value")


def test_scope_control_survives_on_its_payload_validation_callers():
    """Decision contracts validate discovery/triage payloads through it.

    S28 retarget: this also pinned ``observability.untriaged_issue_discoveries``.
    That import fed the ``issue_discovery_triage_needed`` interventions, which
    were computed over ``build_observability``'s ``tasks`` parameter — a ``[]``
    literal in both callers since S8 — so the import went with the parameter.
    ``scope_control`` still survives on the two decision-contract callers below,
    but ``untriaged_issue_discoveries`` itself now has NO production caller and
    is a candidate for the next reachability pass (see the final report for
    2026-07-30 S28).
    """

    from agent_runtime import decision_contracts, observability, scope_control

    assert decision_contracts.validate_discovery_payload is scope_control.validate_discovery_payload
    assert decision_contracts.validate_triage_payload is scope_control.validate_triage_payload
    assert not hasattr(observability, "untriaged_issue_discoveries")


def test_role_checklists_survives_on_its_role_envelope_callers():
    """Role envelopes own a checklist store.

    S27 note: the snapshot no longer summarizes checklists -- that reader was in
    the ``_role_streams`` island -- so ``role_envelopes`` is the whole live
    surface, alongside ``decision_contract_registry``'s payload-structure check.
    """

    from agent_runtime import role_checklists, role_envelopes

    assert role_envelopes.RoleChecklistStore is role_checklists.RoleChecklistStore
    assert role_envelopes.normalize_role_id is role_checklists.normalize_role_id
    assert role_envelopes.checklist_summary is role_checklists.checklist_summary


def test_repo_context_keeps_the_worktree_inventory_half():
    """The reap path is inventory-driven -- these two are its base resolvers."""

    from agent_runtime import delivery_directive, repo_context

    assert callable(repo_context.legacy_harness_worktree_base_dir)
    assert callable(repo_context.harness_worktree_inventory)
    assert callable(delivery_directive.reap_orphan_worktrees)


def test_persona_runtime_imports_and_drops_the_dead_worktree_creator_name():
    """It kept three live ``repo_context`` names and never called the fourth.

    **Corrected by S29.** Only TWO of the three were live. Every call site of
    ``repo_execution_context_for_task`` in ``persona_runtime`` sat inside the
    ``AgentContext``-typed repo-grounding cluster, which lost its producer when
    S27 removed ``build_context``; S29 removed the cluster, so that name is now
    an import with no caller here — the very shape this test was written to
    catch — and it goes with it. ``capture_repo_baseline`` /
    ``RepoExecutionContext`` still have live callers
    (``_attach_repo_baseline`` / the render + progress payload helpers).
    ``repo_context`` itself is untouched. See
    tests/agent_runtime/test_s29_persona_runtime_context_lane_removal.py."""

    from agent_runtime import persona_runtime, repo_context

    assert persona_runtime.capture_repo_baseline is repo_context.capture_repo_baseline
    assert persona_runtime.RepoExecutionContext is repo_context.RepoExecutionContext
    assert not hasattr(persona_runtime, "isolated_repo_context_for_run")
    assert not hasattr(persona_runtime, "repo_execution_context_for_task")
    assert callable(repo_context.repo_execution_context_for_task)
