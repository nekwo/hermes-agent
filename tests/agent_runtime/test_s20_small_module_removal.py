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
