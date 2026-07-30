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
    """Operator channels, the snapshot, and the store all project decision types."""

    from agent_runtime import operator_channels, simplified_contract, snapshot, store

    assert operator_channels.public_decision_type_value is simplified_contract.public_decision_type_value
    assert snapshot.public_decision_type_value is simplified_contract.public_decision_type_value
    assert store.public_decision_type_value is simplified_contract.public_decision_type_value


def test_scope_control_survives_on_its_payload_validation_callers():
    """Decision contracts validate discovery/triage payloads through it."""

    from agent_runtime import decision_contracts, observability, scope_control

    assert decision_contracts.validate_discovery_payload is scope_control.validate_discovery_payload
    assert decision_contracts.validate_triage_payload is scope_control.validate_triage_payload
    assert observability.untriaged_issue_discoveries is scope_control.untriaged_issue_discoveries


def test_role_checklists_survives_on_its_role_envelope_callers():
    """Role envelopes own a checklist store; the snapshot summarizes it."""

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
    """It kept three live ``repo_context`` names and never called the fourth."""

    from agent_runtime import persona_runtime, repo_context

    assert persona_runtime.repo_execution_context_for_task is repo_context.repo_execution_context_for_task
    assert persona_runtime.capture_repo_baseline is repo_context.capture_repo_baseline
    assert persona_runtime.RepoExecutionContext is repo_context.RepoExecutionContext
    assert not hasattr(persona_runtime, "isolated_repo_context_for_run")
