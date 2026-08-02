"""S24 sweeps the residue half of ``delivery_directive.py``; the live half stays.

The liveness ruling in ``docs/agent-runtime-harness/delivery-directive.md``
split this module in two. Wave 1 removed the last two callers of the declared
directive (``context_builder._delivery_directive_line``, ``snapshot._task_summary``).
This stage removes what those callers were the last consumers of:

* the **declaration path** — the Stage-38 goal-create field validator and its
  contract constants. The request lane that carried ``delivery_directive`` and
  the ``Task`` that stored it are both gone, so the declared value can no longer
  differ from the default, and nothing reads the default.
* the **terminal-settle executors** — ``execute_delivery_directive`` and the
  three functions around it. Their choke point (``ArchiveStore.archive_tasks``)
  went with the mission lane; they were reachable only from each other and from
  ``test_delivery_directive.py``.
* the **delivery-time capture path** — ``capture_bundle_patch`` and its only
  caller ``RepoBundleStore.mark_delivered``. Nothing has marked a bundle
  delivered since S5/S8, so the capture never ran.

The module stays importable for the live half: ``reap_orphan_worktrees`` (two
production callers — ``hermes harness worktree reap`` and ``harness doctor
--fix``) with its ``wt_reaped_patches/`` capture contract.

``worktree.task_reaped`` and ``bundle.worktree_reaped`` were deliberately left
unregistered by S16b because their emitters were residue. Those emitters are
removed here, so the two types are now emitter-free AND contract-free — pinned
below so nobody registers a contract for an emitter that no longer exists.
"""

from __future__ import annotations

import inspect


from agent_runtime import delivery_directive, repo_context


REMOVED_DIRECTIVE_SYMBOLS = (
    # Declaration path — the goal-create field and its validator.
    "DIRECTIVE_KEY",
    "DEFAULT_DELIVERY_DIRECTIVE",
    "PROMOTE_MODES",
    "PRESERVE_DIFF_MODES",
    "WORKTREE_MODES",
    "DeliveryDirectiveInvalid",
    "normalize_delivery_directive",
    "task_delivery_directive",
    # Terminal-settle executors and their private helpers.
    "execute_delivery_directive",
    "execute_task_delivery_directives",
    "execute_task_worktree_delivery_directives",
    "reap_task_run_worktrees",
    "_reap_bundle_worktrees",
    "_emit_task_reap_event",
    "_promote_patch_to_repo",
    "_open_promotion_incident",
    "_write_promotion_record",
    "_synthetic_worktree_bundle",
    "_bundle_run_id",
    "_dirty_paths",
    "_run_git",
    "_emit",
    # Delivery-time capture — no producer since S5/S8.
    "capture_bundle_patch",
    "bundle_patch_path",
)

LIVE_DIRECTIVE_SYMBOLS = (
    "reap_orphan_worktrees",
    "_write_reap_patch_exclusive",
    "_is_empty_husk",
)

# Emitted by the removed executors, never registered (S16b recorded the
# omission as deliberate). Both emitters are gone now.
EMITTER_FREE_EVENT_TYPES = ("worktree.task_reaped", "bundle.worktree_reaped")

REMOVED_BUNDLE_SYMBOLS = (
    "mark_delivered",
    "_record_empty_delivery_guard",
    "_patch_was_proposed_for_delivery",
    "_empty_delivery_is_proof_only_no_product_edit",
    "_task_declares_no_product_edits",
    "_open_delivery_incident_once",
    "_bundle_stage_key",
    "_safe_string_set",
    "PATCH_LANDED_NOWHERE",
    "STAGE_NO_PROGRESS",
    "EMPTY_DELIVERY_THRESHOLD",
    "SIMPLIFIED_PHASES",
    "simplified_phase_for_task",
    "REPO_BUNDLE_OWNER_PERSONAS",
)

REMOVED_REPO_CONTEXT_SYMBOLS = (
    "command_workdir_for_task",
    "existing_run_worktrees_in_bases",
    "harness_worktree_dirs",
    "git_diff_since_baseline",
    "diff_weakens_tests",
    "known_repo_scope_labels",
    "canonical_repo_scope_label",
    "explicit_repo_mentions",
    "_dirty_paths_from_status",
)




def test_the_module_still_imports_and_the_live_half_is_callable():
    for name in LIVE_DIRECTIVE_SYMBOLS:
        assert hasattr(delivery_directive, name), name
        assert callable(getattr(delivery_directive, name)), name


def test_reap_orphan_worktrees_keeps_its_public_signature():
    """``runtime_commands._cmd_worktree_reap`` and ``harness_doctor`` call this
    by keyword; the sweep must not move a parameter under them."""

    signature = inspect.signature(delivery_directive.reap_orphan_worktrees)
    assert list(signature.parameters) == [
        "min_age_seconds",
        "event_log",
        "dry_run",
        "include_legacy_temp",
    ]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )






def test_the_live_janitor_still_emits_its_registered_event_type():
    from agent_runtime.events import ALLOWED_EVENT_TYPES

    source = inspect.getsource(delivery_directive)
    assert "worktree.orphans_reaped" in source
    assert "worktree.orphans_reaped" in ALLOWED_EVENT_TYPES








def test_the_live_repo_context_halves_survive():
    """The inventory half feeds the janitor; the excerpt half feeds the live
    persona/context-builder repo context."""

    for name in (
        "harness_worktree_inventory",
        "legacy_harness_worktree_base_dir",
        "current_harness_worktree_base_dir",
        "_worktree_base_dir",
        "worktree_source_root",
        "remove_orphan_worktree",
        "worktree_patch_text",
        "worktree_patch_size_estimate",
        "repo_execution_context_for_task",
        "resolve_affected_repo_workdir",
            "safe_affected_repo_labels",
    ):
        assert hasattr(repo_context, name), name
    # RepoContextExcerpt was flagged caller-free by an earlier audit; it is not.
    # It types ``RepoExecutionContext.context_excerpts``, which
    # ``persona_runtime`` and ``context_builder`` both render.
    assert repo_context.RepoContextExcerpt is not None
    assert (
        repo_context.RepoExecutionContext.__dataclass_fields__["context_excerpts"]
        is not None
    )


def test_the_worktree_creator_is_kept_as_declared_test_infrastructure():
    """4a ruling: the creator trio stays, and says so in its own docstring.

    ``isolated_repo_context_for_run`` has no production caller, but twelve tests
    in ``test_repo_context_observation.py`` pin its protections — including two
    live-incident regressions (junction severing that once emptied the backend
    venv; the backend ``.env`` copy). Deleting it would delete those, so it is
    kept and LABELLED rather than silently left looking live.
    """

    for name in ("isolated_repo_context_for_run", "_worktree_token", "_ensure_isolated_worktree"):
        assert hasattr(repo_context, name), name
    doc = inspect.getdoc(repo_context.isolated_repo_context_for_run) or ""
    assert "no production caller" in doc.lower()
