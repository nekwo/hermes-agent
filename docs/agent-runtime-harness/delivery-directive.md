# Delivery Directive

> **Liveness ruling (2026-07-30 follow-up audit to
> [16 — Mission Lane Removal](16-mission-lane-removal.md)).**
> `agent_runtime/delivery_directive.py` is neither wholly live nor wholly
> residue — it is **one live half carrying one unswept residual half**, and it
> must stay importable for the live half. Per function:
>
> **LIVE — the orphan-worktree janitor.**
> - `reap_orphan_worktrees` has two production callers:
>   `hermes harness worktree reap` (`hermes_cli/harness_parts/runtime_commands.py::_cmd_worktree_reap`)
>   and `harness doctor --fix` (`agent_runtime/harness_doctor.py`). Its helpers
>   (`_write_reap_patch_exclusive`, `_is_empty_husk`) live with it. This is the
>   reason the module survives, and it is worktree-inventory-driven — it never
>   touches `Task`.
> - `read_bundle_promotion_record` is a live read surface:
>   `repo_bundles.py` uses it to label bundle summaries
>   (`delivery_contract: delivery_directive`) that `status.py` and the snapshot
>   render. Post-removal it can only ever describe *historical* promotion
>   records; nothing writes new ones.
>
> **DORMANT — importable, but no producer.**
> - `capture_bundle_patch` is still called by
>   `RepoBundleStore.mark_delivered` (`repo_bundles.py:166`), but the worker /
>   dispatch lane that delivered bundles was removed in S5/S8, so nothing marks
>   a bundle delivered anymore.
>
> **UNSWEPT RESIDUE — callers removed with the mission lane, kept green only by
> `tests/agent_runtime/test_delivery_directive.py`.**
> - The **declaration path**: the directive was a Stage-38 goal-create field
>   stored on `Task` — both removed. `task_delivery_directive` now has two
>   vestigial callers: `context_builder.py::_delivery_directive_line` (its HUD
>   entry points `prompt_observability.py` / `runtime_hud.py` can only pass
>   `task=None`, so it always renders the contract default) and
>   `snapshot.py:2608` inside `_task_summary`, which is reachable only from
>   `goal_detail_for_task` — a function neutered to `return None` at its first
>   line in S8/S9. The declared value can never differ from
>   `DEFAULT_DELIVERY_DIRECTIVE` again.
> - The **terminal-settle executors**: `execute_delivery_directive`,
>   `execute_task_delivery_directives`,
>   `execute_task_worktree_delivery_directives`, and `reap_task_run_worktrees`
>   have zero production callers — their choke point
>   (`ArchiveStore.archive_tasks` at task settle) went with the mission lane.
>   This is also the only reason the module still imports `TaskState`.
>
> So: **not a deliberate seam** in the R-3 sense (no upstream file imports it
> unguarded); the residue half simply has not been swept yet. A future
> retirement pass may delete the executor half and the declaration path
> (retargeting `test_delivery_directive.py` in the same commit), but must keep —
> or re-home — `reap_orphan_worktrees` + `read_bundle_promotion_record` and the
> `wt_reaped_patches/` capture contract.

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

1. **Capture at delivery** — `RepoBundleStore.mark_delivered` captures the
   run worktree's binary-safe patch to
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

Tests: `tests/agent_runtime/test_delivery_directive.py` (covers both the live
janitor and the residual executor half), plus
`tests/hermes_cli/test_worktree_reap_cli.py` for the CLI verb.
