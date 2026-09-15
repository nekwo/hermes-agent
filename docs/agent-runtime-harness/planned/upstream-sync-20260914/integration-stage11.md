# Checkpoint 11 — desktop updater and companion checks

User identified the Desktop app Update button as the entry point. Candidate `5272404b844ec990871c421d55cd8bc83628987f` on `codex/updater-history-safety-20260915` includes the entire upstream integration plus two scoped commits: `cb1056f0ee` updater history safety and `5272404b84` fork CI runner fallback. Draft PR: https://github.com/nekwo/hermes-agent/pull/1 . Hosted checks are running; not a pass yet.

## Behavior

Existing Windows/Posix handoffs continue to invoke `hermes update`. Fork updates compare ancestry before stashing/switching, stop on divergence/unrelated/incomplete history, atomically preserve known local and remote tips in non-expiring recovery refs, and never take the same-name-branch reset shortcut. Matching trees describe a possible fold, not permission to rewrite. A late fallback guard covers failed fast-forwards. Upstream synchronization now uses normal push only. Desktop history refusals skip retries and show a specific preserved-checkout/review message. No new lifecycle owner or installer methods.

Read-only update plan includes cached origin/main ancestry; actual application rechecks the resolved branch after fetching. The safe delivered main remains a descendant of existing main, so ordinary users fast-forward normally. Review-only consolidation is never the deployment branch. Old desktop installations need the first maintained deployment before these new safeguards exist there.

## Proof

- Canonical Python wrapper updater regression: 51 passed, 48 existing platform skips (47 in test_cmd_update plus one autostash skip), zero failures. Real Git tests prove recovery refs, branch/index/untracked preservation, divergent/equal-tree histories and no-force push.
- Desktop history/handoff batch: 17 passed, zero failed. PowerShell retry policy is executed; both handoff scripts parse.
- Launcher fresh worktree from dd20ddb621: 17 settings/RPC/control tests passed. Initial fresh-worktree run lacked the ignored .env asset; copied tracked .env.example to that isolated worktree and reran successfully. No live secrets/config copied, no Launcher source change.
- Initial Git recovery test found Windows subprocess text mode converting transaction LF to CRLF. Binary UTF-8 input fixes the actual recovery path; the real Git test now verifies both refs rather than merely observing refusal.
- Fork workflows use standard hosted runners; upstream retains its 32/96-core labels. Python fork CI is capped at four workers.

See updater-proof.json for log hashes. Counts overlap across batches. Main is still unchanged pending live-service maintenance. No live service stopped, no runtime settings changed, and no full desktop installation/relaunch/Stage C or second-host claim. The earlier no-restart constraint requires a specific maintenance decision before changing the checkout imported by the operator service.
