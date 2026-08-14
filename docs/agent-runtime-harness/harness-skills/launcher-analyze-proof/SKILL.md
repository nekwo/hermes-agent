---
name: launcher-analyze-proof
description: Choose fast, correct Flutter analyze, focused test, and Stage C screenshot commands for Eternia Launcher work. Use when Launcher work needs static analysis, a focused widget/bridge test, or Mission Control visual proof.
metadata:
  hermes:
    surfaces: [mission_chat]
    modes: [standard]
    load_policy: recommended
---

# Launcher Analyze Proof

> **Scope note (2026-07-30).** The worker/stage lane this skill was written for — Mission
> HUD, `agent_hud.current_assignment`, `proof_gate.required_proof_types`, Harness-managed
> `request_test_run`, Stage 47 burn-in, `launcher_contract_smoke` — was removed with the
> mission lane. See `docs/agent-runtime-harness/16-mission-lane-removal.md`. What survives,
> and what this skill is now for, is choosing the *right command* for Launcher work you run
> yourself inside a chat turn, and capturing Stage C visual proof. Stage C is a KEEP: the
> MCP server, the PowerShell helpers, and the screenshot artifacts are all live.

Use this skill when Launcher work needs static analysis, a focused test, or a Mission
Control screenshot. Run the command yourself and report the real result; there is no
proof runner to ask and no gate to satisfy.

## First Preflight For Launcher Windows / Stage C Proof

- Before any Windows debug rebuild, Marionette freshness check, Stage C MCP launch/attach,
  screenshot, or video capture, check for already-running `eternia_launcher.exe` and stale
  `stagec_qa_mcp_server.exe` processes first.
- If either is running and the next capture needs a fresh debug binary or a fresh Stage C
  window, close/kill those processes before rebuilding or capturing. Stale Launcher windows
  can lock `build/windows/.../Debug` files, keep an old `lib/main.dart` binary alive, or make
  MCP attach to the wrong window.
- Do not blame ordinary `flutter test` widget failures on a running Launcher by default;
  inspect the output. A running Launcher mostly affects Windows build/Marionette/Stage C
  visual capture, not headless widget tests.
- After cleanup, rerun the narrowest valid command once. If cleanup is impossible, report a
  redaction-safe environment blocker that names the process owner/IDs and what it blocks.

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
- Never use `flutter --version`, `flutter doctor`, `where flutter`, or `which flutter` as
  evidence that Launcher code works. Those are readiness signals only.
- Report the command, its exit status, and the failure excerpt. Do not paste whole logs into
  the chat; keep the artifact and carry a pointer.

## Stage C Mission Control Screenshot Command Shape

- Mission Control visual proof must be pinned to Tony's live Harness runtime. In every
  `mcp_launcher_qa_open_app_tab` or `mcp_launcher_qa_launch_or_attach` call for Mission
  Control, pass `hermes_profile`, `harness_runtime_root`, and `hermes_home`, then verify the
  envelope shows `hermes_profile` non-null, `harness_runtime_root_configured:true`, and
  `hermes_home_configured:true`. Unpinned pixels do not prove the correct runtime root/profile.
- `mcp_launcher_qa_open_app_tab` owns composed screenshot knobs: `screenshot_stabilize_ms`,
  `screenshot_max_retries`, and `screenshot_retry_delay_ms`.
- `mcp_launcher_qa_screenshot_window` is a primitive and accepts only
  `window_title_prefix`/`window_title`/`window`, `label`, `out_dir`, `foreground`,
  `max_retries`, and `retry_delay_ms`. Do not pass `screenshot_stabilize_ms`,
  `screenshot_max_retries`, or `screenshot_retry_delay_ms` to `screenshot_window`; use
  `max_retries` and `retry_delay_ms` there, or add a bounded `Start-Sleep` between navigation
  and capture.
- For Tony-facing Mission Control screenshots, capture a fullscreen or maximized Launcher
  window. If the artifact is visibly compressed, too small, blank/white, or low-information,
  do not call it done; report the exact blocker and retry once.
