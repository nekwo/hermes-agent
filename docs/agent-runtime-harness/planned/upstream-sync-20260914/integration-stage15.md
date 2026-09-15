# Checkpoint 15: authorized delivery preparation

The operator authorized continuing and finishing after the request for a brief managed-Hermes maintenance window. This supersedes checkpoint 14's maintenance hold. Launcher must not be restarted, and unrelated runtime configuration/worktrees remain preserved.

## Delivered companion

Launcher local main and origin/main are now `241391dd66a55d4375fee81609c9195d8240c1b7`, fast-forwarded from `084a169fd802b80e2a0789a398f500f010bba44b`. The five contract paths were disjoint from all 65 dirty primary paths. The landed contract worktree was removed through Git. Existing proof: 150 CLI contract tests, 46 response tests, and exact regeneration from the Hermes producer. Updater safeguards were already on the parent (37 tests and focused analysis passed). No Launcher build or restart occurred.

## Hermes candidate and CI

Candidate `7f80bec98c8255e6c7e030343800d04a88b138e5` adds only an ESLint repair to the retained desktop session-dot-state test. The focused desktop test passed. The upstream-identical virtual-history offset test failed once under CI load; after building its Ink dependency, a focused rerun passed all 17 tests. No assertion or production behavior was removed.

The maintainer-label gate now passes after the operator's go-ahead; the fork's missing `ci-reviewed` label was created and applied to PR #1. A stale label-triggered rerun displaced the latest run. The stale run was canceled and the current candidate's CI run `35007978005` restarted (attempt 2). Full CI remains pending; no full-suite pass is claimed.

## Maintenance preflight

Hermes local and origin/main remain `34ad8ba33f2508ab10bb24a26f0377ddb62660cb`. A dependency dry run using the actual managed Python and `[messaging,web,cli,mcp]` extras succeeded without installing anything (`qa-artifacts/live-install-dry-run.log` and JSON report).

The managed Alice gateway is the only detected Hermes process group. Its receipt identifies PID 16700 and profile `X:/Eternia/.hermes/profiles/alice`, with zero active agents. Telegram reports connected; WhatsApp-not-paired is a pre-existing status. Rediscover PIDs immediately before maintenance. The profile has its existing `Hermes_Gateway_alice` scheduled task and service wrapper. Use the Windows owner stop path, which writes the planned-stop marker and drains work before bounded termination; do not use a substring process kill. Restart the same profile only after dependency installation succeeds. No runtime stop/start has occurred at this checkpoint.

Configuration SHA256 before maintenance (contents not recorded):

- Alice config: `D7607A2BB011EB1FCF9AABC09F6D933A2CA1F6DF06AA74F6BCA86AF083A1E678`.
- Root config: `75638B3556997AD44EBDF19932C8D92F69C0A6BE362AFDB00A8FB1B10D195DC2`.

All registered worktrees were checked again. Unrelated dirty work remains: Hermes Local llama seven QA directories; Launcher primary 65 paths; Launcher maps-voice nine paths. Other registered worktrees were clean.
