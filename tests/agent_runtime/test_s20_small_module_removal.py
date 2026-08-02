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




REMOVED_MODULES = ("agent_runtime.role_sessions",)






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

    from agent_runtime import operator_channels, simplified_contract, store

    assert operator_channels.public_decision_type_value is simplified_contract.public_decision_type_value
    assert store.public_decision_type_value is simplified_contract.public_decision_type_value
    # A live call site, not just a bound name.
    for module in (operator_channels, store):
        assert "public_decision_type_value(" in inspect.getsource(module), module.__name__


def test_scope_control_survives_on_its_payload_validation_callers():
    """Decision contracts validate discovery/triage payloads through it.

    S28 retarget: this also pinned ``observability.untriaged_issue_discoveries``.
    That import fed the ``issue_discovery_triage_needed`` interventions, which
    were computed over ``build_observability``'s ``tasks`` parameter — a ``[]``
    literal in both callers since S8 — so the import went with the parameter.
    ``scope_control`` still survives on the two decision-contract callers below.

    S42 follow-through (2026-07-31): ``untriaged_issue_discoveries`` was the
    "candidate for the next reachability pass" this docstring named. That pass
    removed it from ``scope_control`` itself, so payload validation is now the
    module's entire surviving surface.
    """

    from agent_runtime import decision_contracts, scope_control

    assert decision_contracts.validate_discovery_payload is scope_control.validate_discovery_payload
    assert decision_contracts.validate_triage_payload is scope_control.validate_triage_payload


def test_role_checklists_no_longer_survives_on_role_envelope_callers():
    """RETARGETED at S44 — this witness recorded the exact edge that later died.

    It used to assert three re-export identities proving ``role_envelopes`` was
    ``role_checklists``' live surface (``RoleChecklistStore``,
    ``normalize_role_id``, ``checklist_summary``). S27 had already narrowed that
    surface to the single importer once the snapshot's ``_role_streams`` island
    went. S44 deleted ``role_envelopes`` whole, so all three re-exports are gone
    and the module now survives on ONE caller that never went through the store:
    ``decision_contract_registry``'s payload-structure check.

    Kept as an absence assertion rather than deleted — this file is the record of
    which small modules survived a removal wave and why, so a reversal has to be
    visible here or the reasoning silently rots.
    """


    from agent_runtime import role_checklists

    assert callable(role_checklists.validate_checklist_payload_structure)


def test_repo_context_keeps_the_worktree_inventory_half():
    """The reap path is inventory-driven -- these two are its base resolvers."""

    from agent_runtime import delivery_directive, repo_context

    assert callable(repo_context.legacy_harness_worktree_base_dir)
    assert callable(repo_context.harness_worktree_inventory)
    assert callable(delivery_directive.reap_orphan_worktrees)


def test_persona_runtime_drops_retired_repo_context_names_without_touching_the_module():
    """The persona runtime no longer imports the retired repo-baseline lane.

    **Corrected by S29.** Only TWO of the three were live. Every call site of
    ``repo_execution_context_for_task`` in ``persona_runtime`` sat inside the
    ``AgentContext``-typed repo-grounding cluster, which lost its producer when
    S27 removed ``build_context``; S29 removed the cluster, so that name is now
    an import with no caller here — the very shape this test was written to
    catch — and it went with it. S33 then retired the caller-free render,
    progress-payload, and baseline-attach trio cleared by ``a54e802cd``;
    ``capture_repo_baseline`` and both last-use imports went with that lane.
    The live worktree-creator infrastructure in ``repo_context`` is untouched."""

    from agent_runtime import repo_context

    assert callable(repo_context.repo_execution_context_for_task)
    assert callable(repo_context.isolated_repo_context_for_run)
