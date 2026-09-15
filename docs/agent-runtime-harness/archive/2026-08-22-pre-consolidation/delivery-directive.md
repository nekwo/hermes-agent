# Delivery Directive

> **Liveness ruling — SWEPT 2026-07-30 (S24), follow-up to
> [16 — Mission Lane Removal](16-mission-lane-removal.md).**
> `agent_runtime/delivery_directive.py` was ruled **one live half carrying one
> unswept residual half**. The residue half is now gone; the module stays for
> the live half, and this is the record of where the line was drawn.
>
> **LIVE — kept, unchanged signatures.**
> - `reap_orphan_worktrees` has two production callers:
>   `hermes harness worktree reap` (`hermes_cli/harness_parts/runtime_commands.py::_cmd_worktree_reap`)
>   and `harness doctor --fix` (`agent_runtime/harness_doctor.py`). Its helpers
>   (`_write_reap_patch_exclusive`, `_is_empty_husk`) and the
>   `wt_reaped_patches/` capture-before-delete contract live with it. It is
>   worktree-inventory-driven — it never touched `Task`, which is why it
>   survived the lane it shipped in.
>
> **ROUND 2 follow-up (2026-08-02).** The claimed historical read surface was
> stale: `repo_bundles.repo_bundle_summary` retired with the repo-bundle store,
> leaving `read_bundle_promotion_record` and `bundle_promotion_record_path` as a
> test-only closed loop. Both readers were removed; the janitor is the module's
> sole live production lane.
>
> **REMOVED in S24 — every one re-verified caller-free by text grep first.**
> - The **declaration path**: `task_delivery_directive`,
>   `normalize_delivery_directive`, `DeliveryDirectiveInvalid`,
>   `DEFAULT_DELIVERY_DIRECTIVE`, `DIRECTIVE_KEY`, and the three mode
>   frozensets. The directive was a Stage-38 goal-create field stored on `Task`
>   — request lane and `Task` both gone. Its last two callers went in wave 1
>   (`context_builder::_delivery_directive_line`; `snapshot::_task_summary`,
>   which was reachable only from the neutered `goal_detail_for_task`), leaving
>   a validator for a field no request can carry and a default nothing reads.
> - The **terminal-settle executors**: `execute_delivery_directive`,
>   `execute_task_delivery_directives`,
>   `execute_task_worktree_delivery_directives`, `reap_task_run_worktrees`, and
>   their private helpers (`_reap_bundle_worktrees`, `_emit_task_reap_event`,
>   `_promote_patch_to_repo`, `_open_promotion_incident`,
>   `_write_promotion_record`, `_synthetic_worktree_bundle`, `_bundle_run_id`,
>   `_dirty_paths`, `_run_git`, `_emit`). Their choke point
>   (`ArchiveStore.archive_tasks`) went with the mission lane; they were
>   reachable only from each other. The `TaskState` import left with them.
> - The **delivery-time capture**: `capture_bundle_patch`, `bundle_patch_path`,
>   and their only caller `RepoBundleStore.mark_delivered` — plus that method's
>   exclusive helper cluster (`_record_empty_delivery_guard`,
>   `_patch_was_proposed_for_delivery`,
>   `_empty_delivery_is_proof_only_no_product_edit`,
>   `_task_declares_no_product_edits`, `_open_delivery_incident_once`,
>   `_bundle_stage_key`, `_safe_string_set`, `PATCH_LANDED_NOWHERE`,
>   `STAGE_NO_PROGRESS`, `EMPTY_DELIVERY_THRESHOLD`). Nothing had marked a
>   bundle delivered since S5/S8, so the capture never ran.
>
> **Two event types are now emitter-free.** S16b deliberately left
> `worktree.task_reaped` and `bundle.worktree_reaped` unregistered because their
> emitters were residue. Those emitters are deleted — the types are now
> emitter-free AND contract-free, and must stay that way. (`bundle.promoted`,
> `bundle.diff_captured`, `bundle.diff_capture_failed` were never registered
> either; their appends were being swallowed by `_emit`'s `except`.)
>
> **Discharged same-day (2026-07-30):** `repo_bundle.delivered` briefly became a
> registered-but-unemittable contract when this sweep deleted `mark_delivered`
> (its only emitter); the registry owner de-registered it in `f9febb32b`
> together with its `OPERATOR_SUMMARY_EVENT_TYPES` row and formatter arm. The
> seven sibling `repo_bundle.*` types remain live via `RepoBundleStore.update`.
>
> **Fixture ruling (4a): the worktree creator is KEPT and labelled.**
> `repo_context.isolated_repo_context_for_run` + `_worktree_token` +
> `_ensure_isolated_worktree` have **no production caller**, but deletion was
> rejected: the earlier audit assumed `test_delivery_directive.py` was the only
> consumer, and it is not. Twelve tests in `test_repo_context_observation.py`
> drive the creator to pin protections reachable only through it (GC count cap,
> dirty/fresh sparing, fail-closed `worktree add`, checkout timeout) — two of
> them are live-incident regressions: junction severing that once emptied the
> backend venv (2026-07-01) and the backend `.env` copy whose absence broke every
> read-only proof (2026-07-03). Rebuilding the fixtures on raw
> `git worktree add` would delete those regressions to save a function, so the
> trio is kept with a docstring that says it is not live. `existing_run_worktrees`
> and `remove_harness_worktree_for_repo` (caller-free after this sweep) stay with
> it: **that lane retires whole or not at all.** The consequence to state plainly
> — nothing in production creates harness worktrees any more, so the live janitor
> now only ever cleans *historical* litter.
>
> **Also swept in the same commit** (caller-free, re-verified):
> `repo_context.command_workdir_for_task`, `existing_run_worktrees_in_bases`,
> `harness_worktree_dirs`, `git_diff_since_baseline` (+ its private diff
> cascade), `diff_weakens_tests` (+ the `_DIFF_*` regexes),
> `changed_files_from_diff`, `known_repo_scope_labels`,
> `canonical_repo_scope_label`, `explicit_repo_mentions`,
> `_dirty_paths_from_status`; and `repo_bundles.simplified_phase_for_task`,
> `SIMPLIFIED_PHASES`, `REPO_BUNDLE_OWNER_PERSONAS`, and six unused `WAKE_*`
> constants. `capture_repo_baseline` is NOT swept — `persona_runtime` calls it —
> so its own behaviour is now pinned directly in
> `test_repo_context_observation.py` instead of only through the deleted diff
> reader. **Contradiction found and skipped:** `RepoContextExcerpt` was listed as
> caller-free by an earlier audit and is not — it types
> `RepoExecutionContext.context_excerpts`, which `persona_runtime` and
> `context_builder` both render. `docs/agent-runtime-harness/08-blueprint-as-script-collapse.md`
> still cites `git_diff_since_baseline` / `diff_weakens_tests` as live helpers;
> that doc is owed a correction.

Everything below describes the contract as designed, most of which executed on
the removed goal/task lane and is retained as historical reference.

One declarative contract for what happens to a delivered repo bundle. The
directive is DECLARED on the goal, projected to the HUD, and executed by ONE
executor — the harness makes no ad-hoc promote/cleanup micro-decisions.

## Declaration (removed lane)

Stage 38 goal-create request (all fields optional; missing keys take the
contract default):

```json
{
  "goal": {
    "title": "...",
    "description": "...",
    "requires_visual_proof": true,
    "delivery_directive": {
      "promote": "apply_to_repo",   // or "hold"
      "preserve_diff": "archive",   // or "none"
      "worktree": "reap_after_promote"  // or "keep"
    }
  }
}
```

Defaults: `promote: apply_to_repo`, `preserve_diff: archive`,
`worktree: reap_after_promote`. Invalid keys/values reject the goal-create
request (`invalid_request`), never silently coerce.

The resolved directive was stored on the Task (`task.delivery_directive`),
rendered as one line in the persona HUD tick context, and projected in the
snapshot task summary — operator, Dev, and QA read the same declared truth.

## Execution (removed lane)

1. **Capture at delivery** (removed S24) — `RepoBundleStore.mark_delivered`
   captured the run worktree's binary-safe patch to
   `repo_bundles/<task_id>/<bundle_id>.patch` BEFORE `active_run_id` is
   cleared, and records `bundle.delivery_capture` (run id, patch name, changed
   files). The archive moves that directory wholesale, so the diff is
   preserved with no archive-writer special cases.
2. **Promote at terminal settle** — `ArchiveStore.archive_tasks` (the choke
   point both daemon auto-archive and manual `task archive` routed through)
   executed the directive per delivered bundle: apply the patch to the
   resolved product repo (`git apply --check`, `--reverse` idempotency check,
   `--3way` fallback), guarded against overlapping dirty paths, then commit.
   Failure opened a `bundle_promotion_failed` incident and kept the worktree.
   Cancelled tasks never promoted; their diffs stayed archived.
3. **Reap after promote** — the worktree was removed (junction-sever safe)
   only after the promote step succeeded or was a deliberate hold/no-op.
   The archive additionally capture-then-reaped every remaining run worktree of
   the terminal task (`reap_task_run_worktrees`), so litter never outlived the
   goal. Outcomes were written to
   `repo_bundles/<task_id>/<bundle_id>.promotion.json` (archived) and the
   bundle summary reports honest `checkout_applied` / `checkout_status` /
   `closeout_label` values instead of the old hardcoded
   `staged_bundle_not_applied`.

## Orphan janitor (LIVE)

`hermes harness worktree reap [--min-age-seconds N] [--dry-run] [--include-legacy-temp]`
— capture-then-reap for worktrees no open task run owns; also run by
`harness doctor --fix`. Protections: min-age guard; dirty diffs are captured to
`<store_root>/wt_reaped_patches/` (collision-proof exclusive-create names)
before removal; git-unrecognized directories are removed only when they contain
no files at all.

Tests: `tests/agent_runtime/test_delivery_directive.py` (live half only since
S24 — janitor protections, the capture contract, the registered
`worktree.orphans_reaped` emission, and the promotion-record read/labelling),
`tests/agent_runtime/test_s24_delivery_directive_residue_removal.py` (pins the
removal and the keep-set, including the 4a fixture ruling), plus
`tests/hermes_cli/test_worktree_reap_cli.py` for the CLI verb.
