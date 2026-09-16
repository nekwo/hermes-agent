# Stage 2: updater source delivered, release pending

Launcher local/main and origin/main both contain
`77422fe63c5cf89216beb02a82b75034bd62b958`. Its 65 unrelated primary-worktree
changes were preserved. The release branch advanced by fast-forward from
`b2c52177a20bdbae88182f92392a3e421fd54146`; the old release tip is pushed as
`codex/release-backup-20260915` in Launcher.

The 50 focused updater tests passed (193 seconds), including 13 real-Git
migration cases and 37 existing updater/controller cases. Analysis of all six
changed Dart/test files passed with no issues after formatting-only fixes.
Test logs are retained in the Launcher task worktree. No runtime or Launcher
process was stopped. No live visual QA is claimed.

Release 1.0.4+5 is building through the existing isolated GitHub workflow:
https://github.com/ArcadiaLabsLLC/EterniaLauncher/actions/runs/35038208617
The existing trusted manifest signing key was configured through the repository's
encrypted Actions secret, without printing or committing key material. The
local smoke lane cannot run while the operator's Launcher is open; the existing
Windows-hosted release smoke provides isolation instead.

Hermes archive, final candidate and migration metadata are pushed. Against
upstream main `416a8177c25d87aa9929dfcf31f7964137d7fcdd`, the final candidate is
15 ahead and 642 behind. Its tree still exactly equals frozen main.
Hermes main is intentionally unchanged until the signed Launcher release is
available. A passing source test is not binary publication evidence.

## Existing CI gap, independent of the replacement

Frozen main CI https://github.com/nekwo/hermes-agent/actions/runs/35034006672
passed Python tests/e2e/mutation checks, OS-specific checks and Python lints.
The JS/TS job failed one Electron desktop UI test: “pins the quickstart progress
view while the job runs” in
`apps/desktop/src/app/settings/local-models-settings.test.tsx:473`.
Its checks cover visible quickstart progress text and absence of the setup
button; the available job log does not identify the precise failed assertion.
Both test and component are byte-identical to the pinned upstream baseline.
No change is made to these files under the identical-tracked-tree requirement.
This is an explicit remaining gap, not a claim that all frozen-source CI is green.

## First-publication readiness correction

The backend download endpoint returned 503 manifest_unavailable. Read-only
object metadata checks found no launcher objects in its configured bucket;
access to the bucket works. This is a first publication. The active release
run is https://github.com/ArcadiaLabsLLC/EterniaLauncher/actions/runs/35038700512
with stable channel and explicit min_supported=0.0.0 (no mandatory-update floor).
Earlier build-only attempts were cancelled before publication to load the newly
configured signing secret and supply the required first-publication input.
GitHub reads repository secrets when a run is queued. The local service
configuration and running applications were unchanged. Public signed-manifest
availability remains the promotion gate.
