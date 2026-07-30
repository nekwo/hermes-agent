# Delivery Directive

> **2026-07-30 — partially describes a removed subsystem.** §Declaration was a
> Stage-38 goal-create field and §Execution stored the directive on `Task` — both
> removed by [16 — Mission Lane Removal](16-mission-lane-removal.md).
> `agent_runtime/delivery_directive.py` still exists with live importers
> (`context_builder.py`, `snapshot.py`, `harness_doctor.py`, `repo_bundles.py`);
> its declaration path is gone.

One declarative contract for what happens to a delivered repo bundle. The
directive is DECLARED on the goal, projected to the HUD, and executed by ONE
executor — the harness makes no ad-hoc promote/cleanup micro-decisions.

## Declaration

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

The resolved directive is stored on the Task (`task.delivery_directive`),
rendered as one line in the persona HUD tick context, and projected in the
snapshot task summary — operator, Dev, and QA read the same declared truth.

## Execution (agent_runtime/delivery_directive.py)

1. **Capture at delivery** — `RepoBundleStore.mark_delivered` captures the
   run worktree's binary-safe patch to
   `repo_bundles/<task_id>/<bundle_id>.patch` BEFORE `active_run_id` is
   cleared, and records `bundle.delivery_capture` (run id, patch name, changed
   files). The archive moves that directory wholesale, so the diff is
   preserved with no archive-writer special cases.
2. **Promote at terminal settle** — `ArchiveStore.archive_tasks` (the choke
   point both daemon auto-archive and manual `task archive` route through)
   executes the directive per delivered bundle: apply the patch to the
   resolved product repo (`git apply --check`, `--reverse` idempotency check,
   `--3way` fallback), guarded against overlapping dirty paths, then commit.
   Failure opens a `bundle_promotion_failed` incident and keeps the worktree.
   Cancelled tasks never promote; their diffs stay archived.
3. **Reap after promote** — the worktree is removed (junction-sever safe)
   only after the promote step succeeded or was a deliberate hold/no-op.
   The archive additionally capture-then-reaps every remaining run worktree of
   the terminal task (`reap_task_run_worktrees`), so litter never outlives the
   goal. Outcomes are written to
   `repo_bundles/<task_id>/<bundle_id>.promotion.json` (archived) and the
   bundle summary reports honest `checkout_applied` / `checkout_status` /
   `closeout_label` values instead of the old hardcoded
   `staged_bundle_not_applied`.

## Orphan janitor

`hermes harness worktree reap [--min-age-seconds N]` — capture-then-reap for
worktrees no open task run owns. Protections: deterministic ownership by open
tasks' runs; min-age guard; dirty diffs are captured to
`<store_root>/wt_reaped_patches/` before removal; git-unrecognized directories
are removed only when they contain no files at all.

## Related operational contracts (same change set)

- `--start-daemon` is honored on the `task create --request-json` path.
- An untargeted `daemon start` adopts the active foreground lane's open task
  (`target_source: foreground_lane`) instead of scanning the stale backlog;
  `GoalRuntimeInstanceStore.active_foreground()` is the resolver.
- `goal.requires_visual_proof` is an explicit request field threaded to
  `task.requires_visual_proof` — no keyword sniffing.

Tests: `tests/agent_runtime/test_delivery_directive.py`, plus daemon-adoption
and goal-create threading cases in `test_daemon.py` / `test_mission_goal.py`.
