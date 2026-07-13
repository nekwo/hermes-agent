---
name: launcher-analyze-proof
description: Choose fast, correct Flutter analyze and Launcher proof commands for Harness tasks. Use when Launcher Dev sees flutter analyze, launcher_contract_smoke, Mission Control bridge proof, Stage 47 burn-in, no-edit Launcher smoke, Stage C/Marionette QA, or contract-consumption proof.
---

# Launcher Analyze Proof

Use this skill after `harness-dev-delivery` when the current Launcher stage needs static analysis, no-edit smoke, contract-consumption proof, or a focused proof command.

## Stage 76C Proof Posture

- Read `mission_hud.agent_hud.evidence_stack` before choosing a proof command. Missing proof, stale proof, or a blocked-stage entry is advisory evidence for Neko/the goal owner to adjudicate, not a terminal release gate in this skill.
- If proof is missing, choose the narrowest command that can create the missing evidence, or return a bounded blocker through the visible HUD action. Do not claim completion from prose, and do not treat missing proof as a dead-end.
- If the HUD already records a blocked escalation, answer with the smallest recovery proof/input step or exact environment blocker; the Harness keeps the task recoverable.
- Treat `mission_hud.agent_hud.current_assignment` as stage-shaped. Prefer its `proof_gate.required_proof_types`, `proof_gate.commands`, and `output_type` over role defaults when choosing whether Launcher needs analyze, widget proof, visual proof, or a document/artifact proof.

## First Preflight For Launcher Windows / Stage C Proof

- Before any Windows debug rebuild, Marionette freshness check, Stage C MCP launch/attach, screenshot, or video proof, check for already-running `eternia_launcher.exe` and stale `stagec_qa_mcp_server.exe` processes first.
- If either is running and the next proof needs a fresh debug binary or fresh Stage C window, close/kill those processes before rebuilding or capturing proof. Stale Launcher windows can lock `build/windows/.../Debug` files, keep an old `lib/main.dart` binary alive, or make MCP attach to the wrong window.
- Do not blame ordinary `flutter test` widget failures on a running Launcher by default; inspect the proof output. A running Launcher mostly affects Windows build/Marionette/Stage C visual proof, not headless widget tests.
- After cleanup, rerun the narrowest valid proof once. If cleanup is impossible, block with a redaction-safe environment blocker that names the process owner/IDs and the proof lane that is blocked.

## Choose The Narrowest Analyze

- Full release gate only: `flutter analyze`.
- Mission Control feature gate: `flutter analyze lib/features/mission_control test/features/mission_control`.
- Changed-file gate: `flutter analyze <changed lib/test files or containing feature dirs>`.
- Stage C/Marionette wiring gate: `flutter analyze lib/main_marionette.dart lib/core/qa test/core/qa`.
- Do not use `flutter analyze lib/main.dart` unless `lib/main.dart`, bootstrap routing, or app startup wiring changed.

## Contract And Burn-In Proof

- First prove joined packet/proof IDs with a deterministic repo-local command, such as a small `python -c` assertion that names the consumed `packet_id` and `proof_id`.
- Add Flutter analyze only when Launcher Dart changed, Neko explicitly made static analysis the proof gate, or QA needs a real Launcher code gate.
- For `launcher_contract_smoke`, Stage 47 burn-in, no-product-edit, or contract-join stages, the proof must validate the consumed backend packet/proof ID. Tool availability checks are not contract proof.
- For Mission Control page/event-view rendering changes, use the focused page/widget proof: `flutter test test/features/mission_control/mission_control_page_test.dart`.
- For Mission Control bridge/archive/snapshot stages, do not recycle the page/widget proof. Use the bridge regression proof: `flutter test test/features/mission_control/mission_control_snapshot_test.dart test/features/mission_control/mission_control_bridge_test.dart`.
- Never use `flutter --version`, `flutter doctor`, `where flutter`, or `which flutter` as Launcher contract proof. Those are preflight signals only.
- In normal worker flow for product-edit stages, run the narrow analyze/widget command in-session first, then emit `hand_off`; Harness runs the final gate after handoff. Use Harness `request_test_run` only when the HUD exposes `request_gate`, for no-edit/certification proof, QA missing proof, or failed final-gate recovery. Missing proof should become explicit HUD evidence or a focused proof request, not a terminal block.

## Stage C Mission Control Screenshot Command Shape

- Mission Control visual proof must be pinned to Tony's live Harness runtime. In every `mcp_launcher_qa_open_app_tab` or `mcp_launcher_qa_launch_or_attach` call for Mission Control, pass `hermes_profile`, `harness_runtime_root`, and `hermes_home`, then verify the envelope shows `hermes_profile` non-null, `harness_runtime_root_configured:true`, and `hermes_home_configured:true`. Unpinned pixels do not prove the correct runtime root/profile.
- `mcp_launcher_qa_open_app_tab` owns composed screenshot knobs: `screenshot_stabilize_ms`, `screenshot_max_retries`, and `screenshot_retry_delay_ms`.
- `mcp_launcher_qa_screenshot_window` is a primitive and accepts only `window_title_prefix`/`window_title`/`window`, `label`, `out_dir`, `foreground`, `max_retries`, and `retry_delay_ms`. Do not pass `screenshot_stabilize_ms`, `screenshot_max_retries`, or `screenshot_retry_delay_ms` to `screenshot_window`; use `max_retries` and `retry_delay_ms` there, or add a bounded `Start-Sleep` between navigation and capture.
- For Tony-facing Mission Control screenshots, capture a fullscreen or maximized Launcher window. If the artifact is visibly compressed, too small, blank/white, or low-information, do not call visual proof complete; return the exact blocker and reuse the failed proof ID in the next bounded retry.

## Delivery Shape

When delivering a product edit in normal worker flow, keep the handoff compact:

```json
{
  "type": "hand_off",
  "summary": "Mission Control Launcher patch is ready for proof review.",
  "rationale": "The focused Launcher change is complete and the focused self-test ran in this session.",
  "payload": {
    "stage_id": "stage_example",
    "summary": "Updated Mission Control Launcher behavior and ran the focused widget/analyze proof locally.",
    "known_gaps": []
  }
}
```

When requesting an explicit proof gate, keep the delivery packet concise:

```json
{
  "type": "request_test_run",
  "summary": "Run focused Launcher contract proof.",
  "rationale": "Launcher consumed the backend contract packet and needs a deterministic proof gate.",
  "payload": {
    "stage_id": "stage_example",
    "commands": ["flutter test test/features/mission_control/mission_control_page_test.dart"],
    "delivery": {
      "consumed_contract_packet_ids": ["packet_contract_id"],
      "consumed_proof_ids": ["backend_proof_id"],
      "known_gaps": [],
      "next_owner": "neko_supervisor"
    }
  }
}
```
