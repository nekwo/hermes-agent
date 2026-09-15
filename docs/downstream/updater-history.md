# Desktop updates after commit consolidation

## Hermes Desktop

Hermes Desktop's Update button uses the repository handoff (`scripts/desktop-update/windows.ps1` or `posix.sh`) and `hermes update`. The desktop continues to own shutdown/relaunch and the backend continues to own Git updates. No second updater or lifecycle owner is introduced.

## Supported path

Keep published main history. A consolidation branch is a review artifact. Merge upstream and preserve the published fork as an ancestor on a separate candidate, test it, then publish by fast-forward. Existing desktop installs can follow that descendant normally, even when the review series contains fewer commits.

## Diverged or folded remote history

Before stashing or switching a fork checkout, the backend compares HEAD with the freshly fetched target. It distinguishes equal, fast-forward, local-ahead, diverged, unrelated and incomplete shallow history. Matching trees with different ancestry are reported as a possible fold, never treated as permission to reset.

A blocked update pins both known tips atomically under `refs/hermes-update-backups/review-<id>/local` and `/remote`. Those recovery refs do not expire automatically. If either ref cannot be created, the update stops and does not claim a successful backup. Dirty files and staged changes remain in place. `--yes` and the desktop's `--force` handoff do not bypass history review. A second check protects the failed-fast-forward fallback. Publishing an upstream sync uses a normal push; it never force-pushes main.

The desktop does not retry a history-review refusal. It relaunches using its existing failure-recovery flow and shows a specific history-review message. The log includes the recovery refs (or backup failure). No automatic squash, reset, rebase, conflict selection or contributor attribution rewrite is performed.

`hermes update --plan` includes a read-only assessment of cached `origin/main` refs for fork installs; it never fetches. The apply path resolves the requested update branch and checks again after fetching. A shallow history that cannot prove ancestry requires review/deepening, not a guessed divergence repair.

## First deployment and maintenance

These protections ship with this source revision. Older desktop builds/updater scripts do not gain them until the maintained checkout and desktop build are updated. For the first deployment, use the prepared history-preserving candidate during an authorized maintenance window. Do not invoke the old desktop updater to reconcile an already rewritten remote history.

Updating a checkout used by a live editable install can mix old and new imports. Inspect the existing fleet plan and use the established owner-managed shutdown/relaunch path; never replace this with PID-substring killing or raw configuration edits. The September synchronization candidate's primary deployment is held until live-service maintenance is authorized.

## Evidence

Real temporary Git tests cover tree-equivalent folds, genuine divergence, normal ancestry, staged/untracked preservation, atomic recovery failure, pre-stash refusal and late fallback refusal. Windows handoff retry tests execute the PowerShell policy. Existing inventory/autostash tests remain relevant. User-facing final messages are wired in both platform handoffs; full desktop installation/relaunch and POSIX visual acceptance require their platform environments.

## Eternia Launcher (the operator/user update button)

Eternia Launcher owns a separate origin/main-only updater:
`EterniaLauncher/lib/features/mission_control/state/hermes_update_apply_controller.dart`
and `data/mission_control_hermes_setup.dart` in the same feature. It does not
invoke Hermes Desktop's handoff or `hermes update`.

The Launcher patch checks freshly fetched history before runtime maintenance.
A diverged/folded history or failed check returns without stopping Hermes. The
existing update service retains `git merge --ff-only` after fetching again, so
remote changes during maintenance cannot trigger a reset/rebase. Its settings
message now calls for preserving/reconciling both histories; it no longer
recommends `reset --hard`. Launcher does not create the CLI's recovery refs.

Users on published main can update normally after the integration is published
as a descendant of that main. Installations stranded on the older July pre-fold
history still require individual ancestry/patch review; neither tree similarity
nor this patch authorizes discarding their local commits.

Launcher verification: 37 updater/controller tests passed and focused analysis
reported no issues. No live update, app rebuild, stop or restart was performed.
