---
name: harness-qa-verdict
description: QA discipline for Eternia work — what counts as evidence, choosing the narrowest correct analyze/test/screenshot command, when visual/Stage C proof is required, and reporting verdicts and gaps honestly on a chat turn.
metadata:
  hermes:
    surfaces: [mission_chat]
    modes: [standard]
    load_policy: recommended
---

# Harness QA Verdict

> **Scope note (2026-07-30).** This skill was written for a proof-gate lane that no longer
> exists: Mission HUD (`agent_hud`, `recommended_action`, `evidence_stack`, `repo_bundles`,
> `qa_waiting_on`, `current_assignment` with `proof_gate` / `required_proof_types` /
> `outgoing_edges`), stage IDs, the `proofs/` store and its proof records, `checklist_updates`,
> `validation_repair`, handoff/delivery packets, `request_missing_proof` as a routed action,
> and Harness final-gate proof IDs. All of it was removed with the mission lane — see
> `docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/16-mission-lane-removal.md`
> (archived with the 2026-08-22 docs consolidation). There is no HUD, no gate, no
> proof store, and no release you can block. Your verdict is now an opinion delivered in
> chat, and it is worth exactly as much as the evidence you actually looked at.
>
> Stage C visual proof is a KEEP: the `launcher_qa` MCP server, the PowerShell helpers, and
> the screenshot artifacts are all live. "Proof ID" below means a Stage C artifact handle or
> a command result you or someone else really produced — not a row in a deleted store.
>
> **Merged (2026-08-28).** This skill absorbed `launcher-analyze-proof`: one skill now
> carries both halves of QA — how to produce the right evidence (narrowest correct command)
> and how to judge it.

Use this skill for QA review of Eternia implementation work, and for choosing the
narrowest correct command when Launcher work needs static analysis, a focused test, or a
Mission Control screenshot. Run the command yourself and report the real result; there is
no proof runner to ask and no gate to satisfy.

## What Counts As Evidence

- Review what was actually run and captured, in this order: the exact command and its exit
  status, the focused test/analyzer output, and the Stage C artifact for any visual claim.
  Prose is not evidence.
- Judge final outcome coverage, not every intermediate patch. Keep these separate: focused
  command evidence, visual runtime evidence, visual acceptance evidence, unrelated failures,
  and caveats.
- Dev self-test output and observed tool traces are good triage context. For a visual claim
  they are not sufficient — a screenshot or video of the specific changed UI state is.
- If acceptance text names an exact command or path such as
  `tests/agent_runtime/test_*.py`, only evidence covering that path counts. A generic
  status/observability recipe does not substitute.
- When a command was adapted for Windows safety (for example pytest timeout-disabling
  flags), compare against the originally requested target. Do not reject a passing run
  solely because the executed command carries safety flags.
- Missing evidence is not an implementation defect. Say exactly which lane is missing and
  what the minimum acceptable replacement is; do not manufacture a blocker or a pass.
- Inspect only the exact files, functions, and commands the claim depends on. Repeated
  broad searching means you are looping: stop and report what you reviewed and what gap
  remains.
- Do not patch the code yourself, and do not approve a visual claim with no visual evidence.
- Never use `flutter --version`, `flutter doctor`, `where flutter`, or `which flutter` as
  evidence that Launcher code works. Those are readiness signals only.

## First Preflight For Launcher Windows / Stage C Proof

- Before any Windows debug rebuild, Marionette freshness check, Stage C MCP launch/attach,
  screenshot, or video capture — and before accepting such an artifact in review — check
  for already-running `eternia_launcher.exe` and stale `stagec_qa_mcp_server.exe`
  processes first.
- If either is running and the next capture needs a fresh debug binary or a fresh Stage C
  window, close/kill those processes before rebuilding or capturing (or, in review, require
  a cleanup/relaunch or classify the artifact as stale). Stale Launcher windows can lock
  `build/windows/.../Debug` files, keep an old `lib/main.dart` binary alive, or make MCP
  attach to the wrong window.
- Do not blame ordinary `flutter test` widget failures on a running Launcher by default;
  inspect the output. A running Launcher mostly affects Windows build/Marionette/Stage C
  visual capture, not headless widget tests.
- After cleanup, rerun the narrowest valid command once. If cleanup is impossible, report a
  redaction-safe environment blocker that names the process owner/IDs and what it blocks.
- If a matching screenshot/video already exists for the same target and state, inspect it
  rather than asking for another copy.

## Choose The Narrowest Analyze

- Full release gate only: `flutter analyze`.
- Mission Control feature gate: `flutter analyze lib/features/mission_control test/features/mission_control`.
- Changed-file gate: `flutter analyze <changed lib/test files or containing feature dirs>`.
- Stage C/Marionette wiring gate: `flutter analyze lib/main_marionette.dart lib/core/qa test/core/qa`.
- Do not use `flutter analyze lib/main.dart` unless `lib/main.dart`, bootstrap routing, or app startup wiring changed.

## Choose The Narrowest Test

- For Mission Control page/event-view rendering changes, use the focused page/widget test:
  `flutter test test/features/mission_control/mission_control_page_test.dart`.
- For Mission Control bridge/archive/snapshot changes, do not recycle the page/widget test.
  Use the bridge regression pair:
  `flutter test test/features/mission_control/mission_control_snapshot_test.dart test/features/mission_control/mission_control_bridge_test.dart`.
- Use repo-wide `flutter test` only when the operator asks for the full suite.
- Report the command, its exit status, and the failure excerpt. Do not paste whole logs into
  the chat; keep the artifact and carry a pointer.

## Stage C Screenshot Essentials

- Mission Control visual proof must be pinned to the live Harness runtime. In every
  `mcp_launcher_qa_open_app_tab` or `mcp_launcher_qa_launch_or_attach` call for Mission
  Control, pass `hermes_profile`, `harness_runtime_root`, and `hermes_home`, then verify the
  envelope shows `hermes_profile` non-null, `harness_runtime_root_configured:true`, and
  `hermes_home_configured:true`. Unpinned pixels do not prove the correct runtime root/profile.
- `mcp_launcher_qa_open_app_tab` owns composed screenshot knobs: `screenshot_stabilize_ms`,
  `screenshot_max_retries`, and `screenshot_retry_delay_ms`.
- `mcp_launcher_qa_screenshot_window` is a primitive and accepts only
  `window_title_prefix`/`window_title`/`window`, `label`, `out_dir`, `foreground`,
  `max_retries`, and `retry_delay_ms`. Do not pass the composed `screenshot_*` knobs to
  `screenshot_window`; use `max_retries` and `retry_delay_ms` there, or add a bounded
  `Start-Sleep` between navigation and capture.
- For operator-facing Mission Control screenshots, capture a fullscreen or maximized
  Launcher window. If the artifact is visibly compressed, too small, blank/white, or
  low-information, do not call it done; report the exact blocker and retry once.
- The full MCP operating workflow — capture vs driven-proof lanes, semantic controls,
  delivery format, credential preflight, and the pitfall corpus — lives in the
  `launcher-mcp-operations` skill.

## Backend And Deployment Review

- For EterniaBackend and EterniaLauncher product edits, a green local test is not a
  production deployment. The chain is local deterministic product tests → remote test
  staging k8s pod validation → production pod rollout, in that order, and EterniaBackend
  additionally needs a local Docker/PostgreSQL run before staging. If a lane was not run,
  say it was not run.
- Accept only the backend `scripts/test.sh` default Postgres tier, or an equivalent run
  citing the backend `docs/testing/README.md` doctrine and real Docker/PostgreSQL services.
  `scripts/test.sh --sqlite` and mocked-only tests are not release evidence.
- Logs from
  `python scripts/backend_postgres_proof.py --backend-root "X:\Unreal Engine\Engine\EterniaBackend\eternia-backend"`
  count as release-lane evidence only when the run used the default Docker/PostgreSQL tier
  and exited green. Dry-run, SQLite-escape-hatch, and focused-only output do not.
- When production deployment is push-triggered, a rollout account must show a remote sync
  step before the push (pull/fetch, rebase when needed) and then the push. A bare `git push`
  is not a rollout account.

## QA Verdict

Say, in plain chat prose:

- the verdict — approved, needs fixes, or blocked;
- exactly what you reviewed (commands, artifact handles, files);
- which coverage lanes were `reviewed`, `not_required`, `missing`, `blocked`, or `failed`;
- for anything missing: the one exact lane, and the minimum acceptable replacement.

Findings must cite artifact handles, command names, or safe relative paths — never raw logs
and never absolute local paths. Route a cross-stack gap by messaging the right agent with
`agent_chat_send`; there is no router that will do it for you.

## Request Missing Proof

When a lane you need was never run or the artifact is stale, name exactly one lane, say why
the existing evidence is insufficient, and state the minimum acceptable replacement. Then
ask the party who can produce it — the operator, or the agent that owns that repo, via
`agent_chat_send`. There is no `request_missing_proof` action and no queue; asking is the
whole mechanism, and nothing is blocked while you wait.

## Report Blocker

Use when evidence cannot be collected or reviewed at all because of an external,
environmental, or tooling blocker. Include exact redaction-safe detail: the command label,
the failure mode, and what would unblock it. Never raw logs, never absolute local paths.
