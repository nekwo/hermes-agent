# Proof Expectations (Backend, Launcher, Stage C visual)

There are no proof gates, proof IDs, or a `proofs/` store any more — that machinery was
removed with the mission lane on 2026-07-30. "Proof" now means exactly what it says: the
command you ran, its real output, and the artifact it produced, reported inside the chat
turn that did the work.

## Backend product edits

The deploy gate is the Docker/PostgreSQL + Redis/Centrifugo tier — read
`X:\Unreal Engine\Engine\EterniaBackend\eternia-backend\docs\testing\README.md`
before claiming release proof (`scripts/test.sh --sqlite` and mocked-only runs are
iteration aids, not release proof). From the hermes repo:

```powershell
python scripts/backend_postgres_proof.py --backend-root "X:\Unreal Engine\Engine\EterniaBackend\eternia-backend"
```

Focused targets go after `--`; preserve the generated backend `qa_artifacts` log in the
final report. Promotion order: local deterministic tests → staging k8 pod validation →
production rollout proof. If prod deploy is push-triggered, fetch/rebase before pushing
and rerun affected local proof after any rebase.

## Launcher code

```powershell
flutter analyze <focused paths>
flutter test <focused tests>
```

## Launcher visual proof (Stage C) — the MCP path is the only path

Operator ruling, 2026-07-30: **Stage C visual proof is the MCP server
(`X:\Unreal Engine\Engine\Launcher\EterniaLauncher\tool\stagec_qa_mcp_server`) driven
through the `launcher-mcp-operations` skill (renamed from `launcher-stagec-mcp-screenshot`
2026-08-28), and nothing else.** The hardcoded
`agent_runtime` Python capture path **is gone** (verified 2026-08-28: no capture module
remains under `agent_runtime/`; the harness-side `qa.request_screenshot` decision contract
went with the mission lane, and `stagec_artifacts_dir` was removed in S54 —
`agent_runtime/paths.py:284`). The only `capture_screenshot` / `screenshot_window` strings
left in hermes are MCP **tool names** in the admission allowlist
(`agent_runtime/mcp_admission.py`), which is the sanctioned path, not a second one. Never
reach for a hermes-side capture path, never cite it, never describe it to an operator as an
option — there is nothing behind it.

Load `launcher-mcp-operations` and follow it. In outline:

- **"Screenshot the current app / what's on screen now"** → message this task's QA
  persona/session (fresh instance per task — see `operations.md`, not "whichever QA
  row is already on the level") with `hermes harness mission-chat message` and have that admitted turn
  call `mcp_launcher_qa_screenshot_window` directly. It is a pure capture primitive —
  no launch, no attach, no login, no reap — and captures the live window by title prefix.
  Do not ask QA to run `open_app_tab` / `launch_or_attach` first; those cannot attach to a
  user-launched Launcher and will spawn a second instance.
- **Driven proof (navigate, click, login, verify state)** → the full MCP marionette
  control path: `launch_or_attach` (picks up the DebugStageC build), `login` via the Stage C
  smoke credential path if gated, navigate with the nav tools / batched `run_actions`, then
  capture with `screenshot_window`. Default `reap_stale:false`.
- **Never kill Tony's live Launcher session to take a screenshot.**
- Deliver each capture as a `MEDIA:<absolute path>` line, verbatim, on a line of its own.
  Canonical delivery rule: `launcher-mcp-operations` SKILL.md, "Screenshot capture and delivery".

The acceptance-matrix PS1 scripts under `docs/stages/qa-reboot/scripts/` (e.g.
`Test-StageCAppTabMcpE2E.ps1`) and the marionette build command
(`flutter build windows --debug --target lib/main_marionette.dart`) are human/CI operator
lanes — an agent does not shell them as a substitute for the MCP path.

Fullscreen screenshots must be at least desktop-sized, redaction-clean, nonblank, and tied
to the intended Launcher debug build/profile — otherwise the visual proof is not complete.

Known Stage C proof hazards to check:

- stale `stagec_qa_mcp_server.exe` source/build mismatch;
- stale `stagec_qa_mcp_server` process locking rebuild;
- **the serve venv missing the `mcp` pip extra** — the turn admits nothing and blames the
  server (`mcp_not_registered_on_lane`) while the real fault is that hermes has no MCP
  client at all. This cost weeks once. Check it FIRST when a turn reports
  `mcp_admitted_servers: 0`:
  `X:\Eternia\.hermes\venvs\hermes-agent\Scripts\python.exe -c "import mcp"`. Note the fix
  needs a runtime restart — `_MCP_AVAILABLE` is read at import (2026-08-26, fixed);
- helper using `C:\Users\beast\.hermes\profiles` instead of `X:\Eternia\.hermes\profiles`
  — the wrong ROOT is the real hazard here, and a `%LOCALAPPDATA%` shadow root is the
  failure it produces. Which profile under the right root is a separate question with a
  separate answer: `alice` is the operator-CLI home, while the launcher's serve child runs
  under `profiles\base` (or the persona's own bound profile). Pin the root; then state
  which home your receipt came from, because a profile-scoped answer (MCP declarations,
  model/provider, `.parity.profile`) read under alice is not the answer the serve turn saw.
  See "base vs alice" in `operations.md`;
- a proof runner requiring an exact old MCP tool count instead of accepting compatible
  tool growth;
- stale direct-control manifest causing `app_attached_but_gate_failed`;
- fixture row not mounted, e.g. `stage34_target_not_active`.
