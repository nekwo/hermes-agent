# Stage 1 — Eternia Launcher MCP hardening

> Parent: [`../mcp-expansion-roadmap.md`](../mcp-expansion-roadmap.md) §Recommended staged build → Stage 1.
> Audit context: [`00-deep-audit.md`](00-deep-audit.md) §A.4, §A.5, §C.

## Why this stage is first

Stage 1 is **not** "build a Launcher MCP server" — that server already exists at [`tool/stagec_qa_mcp_server`](../../../../../Unreal%20Engine/Engine/Launcher/EterniaLauncher/tool/stagec_qa_mcp_server) with 10 typed tools, a global FIFO fixture mutex, JSON-Schema 2020-12, and a `serialised` flag. As of commit `0654d2c` it passed the local full-parity gate (artifact `qa-artifacts/stagec_hermes_mcp_full_parity_20260515_061058`, 5 labels, `parity_mismatches=0`, 87 files / 0 redaction findings).

Stage 1 is **closure + portability**. The roadmap calls Stage C "the path that already proved itself"; this doc spells out the four loose ends that prevent the work being marked done and signs off the launcher MCP before the Stage 2 control-plane work begins.

## Inventory (existing, do not rebuild)

| Asset | Path | Status |
|---|---|---|
| MCP server | [`tool/stagec_qa_mcp_server/lib/server.dart`](../../../../../Unreal%20Engine/Engine/Launcher/EterniaLauncher/tool/stagec_qa_mcp_server/lib/server.dart) | local-PASS on 0654d2c |
| Tool catalog (single source of truth) | [`tool/stagec_qa_mcp_server/lib/tools.dart`](../../../../../Unreal%20Engine/Engine/Launcher/EterniaLauncher/tool/stagec_qa_mcp_server/lib/tools.dart) | 10 tools, all schemas typed |
| Marionette VM-service client | [`tool/stagec_qa_marionette/`](../../../../../Unreal%20Engine/Engine/Launcher/EterniaLauncher/tool/stagec_qa_marionette/) | Dart VM service driver |
| Full-parity test runner | `docs/stages/qa-reboot/scripts/Test-StageCHermesMcpFullParity.ps1` | 5-label PASS |
| Direct↔MCP parity diff | `docs/stages/qa-reboot/scripts/Test-StageCMcpParityDiff.ps1` | runs on every label |
| Screenshot capture | `docs/stages/qa-reboot/scripts/Capture-StageCWindowScreenshot.ps1` | Win32 PrintWindow + blank-pixel sanity |
| Redaction scan | `docs/stages/qa-reboot/scripts/Invoke-RedactionScan.ps1` | bearer matching hardened (commit `3969973e`) |
| Browser PKCE login | `docs/stages/qa-reboot/scripts/stagec_browser_login.py` + commit `b84db539` | headless browser auth |
| Process hygiene | `docs/stages/qa-reboot/scripts/Invoke-StageCProcessHygiene.ps1` | reaps stray Launcher PIDs |
| Operator runbook | `docs/stages/qa-reboot/STAGEC_MCP_OPERATOR_RUNBOOK_2026-05-14.md` | current operator pages |
| Hermes-side recipe | `docs/stages/qa-reboot/STAGEC_MCP_AUTH_RECIPE_2026-05-14.md` | per-step auth flow |

## Deliverables (what closes the stage)

### 1. Stable self-test mode

`tool/stagec_qa_mcp_server/bin/main.dart --self-test` already exists (commit `d89262f7`). Closure work:

- **Deterministic exit codes**: `0` PASS, `2` MISSING_CONTEXT (Launcher not running), `3` MCP_DISCOVERY_FAIL, `4` MCP_TOOL_CALL_FAIL, `5` ARTIFACT_MISSING (artifact dir not writable). Map directly to the [error classes](../mcp-expansion-roadmap.md#error-classes) in the roadmap.
- **No network**: self-test must run without Keycloak, without Django backend, without browser. It exercises tool listing + schema validation + the runtime-state envelope only.
- **Artifact**: `qa-artifacts/stagec_self_test_<ts>/self_test_summary.safe.json` with `{version, exit_code, error_class, tools_listed, schema_violations, duration_ms}`.

### 2. Fresh Hermes Native MCP discovery proof

Per the existing closure plan ([STAGEC_HERMES_MCP_FULL_PARITY_PLAN_2026-05-15.md](../../../../../Unreal%20Engine/Engine/Launcher/EterniaLauncher/docs/stages/qa-reboot/STAGEC_HERMES_MCP_FULL_PARITY_PLAN_2026-05-15.md)) — required because "passes locally via PowerShell" is not the same as "Hermes Native MCP discovers it from a clean session." The closure run must:

1. From a fresh shell, ensure `~/.hermes/profiles/launcher-qa/config.yaml` lists the server under `mcp_servers.stagec-launcher-qa`.
2. Run `hermes mcp list` from inside the `launcher-qa` profile and confirm `stagec-launcher-qa` appears.
3. Run `hermes mcp test stagec-launcher-qa` and confirm a green discovery dump.
4. Start a fresh Hermes session in `launcher-qa`, call each of the 10 `mcp_launcher_qa_*` tools through the model, and capture the request/response envelopes.
5. Diff every envelope against the direct PowerShell runner using `Test-StageCMcpParityDiff.ps1`. Required: `parity_mismatches=0`.

### 3. Direct-runner vs MCP parity where applicable

The doctrine "**debug direct, certify MCP**" (per [Agent QA & Release Doctrine](../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Agent%20QA%20%26%20Release%20Doctrine.md)) means parity is permanent, not a one-time gate. Closure adds:

- A **`parity-only`** mode to `Test-StageCHermesMcpFullParity.ps1` that runs both transports against the same fixture set and writes `parity_diff.safe.json`.
- A `--max-allowed-mismatches` flag (default 0) so future schema additions can be staged without immediately breaking the gate.
- A documented list of fields **excluded from parity** (timestamps, run UUIDs, artifact paths) so the diff is signal, not noise.

### 4. Screenshot and redaction gates

- Screenshots: **non-placeholder check stays load-bearing**. The blank-pixel sanity check in `Capture-StageCWindowScreenshot.ps1` is the closure gate, not "an image file was produced." Document the exact threshold in `docs/stages/qa-reboot/STAGEC_QA_MARIONETTE_CONTRACT.md`.
- Redaction: `Invoke-RedactionScan.ps1` must run over `qa-artifacts/<run_dir>/` and emit `redaction_scan.safe.json` with `{files_scanned, findings: 0, patterns_checked}`. Findings > 0 fails the closure.

### 5. Closure artifact manifest

Single source-of-truth for the closure run:

```
qa-artifacts/stagec_closure_<ts>/
  closure_manifest.safe.json     # commit hash, branch status, gates run, gates not run
  self_test_summary.safe.json
  parity_diff.safe.json
  redaction_scan.safe.json
  per_label/{r2_signed_happy,native_mp4,native_webm,malformed_poster_only,bsky_hls_playlist}/
    mcp/{getRuntimeState,getAuthState,setTab,scrollToFixture,getFeedFixtureState,getMediaPlaybackState,captureScreenshot}.safe.json
    direct/<same set>.safe.json
    screenshot.png                # not safe.json — image; redaction scan covers it
```

`closure_manifest.safe.json` must conform to [`schemas/closure_manifest.schema.json`](schemas/closure_manifest.schema.json). Minimal shape:

```json
{
  "target": "launcher",
  "commit": "<git rev-parse HEAD>",
  "branch": "<git rev-parse --abbrev-ref HEAD>",
  "dirty": false,
  "run_dir": "<absolute path to qa-artifacts/stagec_closure_<ts>/>",
  "gates_run": ["self_test","mcp_discovery","mcp_tool_call","parity_diff","redaction_scan"],
  "gates_not_run": [],
  "labels_covered": ["native_mp4","native_webm","r2_signed_happy","malformed_poster_only","bsky_hls_playlist"],
  "parity_mismatches": 0,
  "redaction_findings": 0,
  "redaction_patterns_checked": 7,
  "credential_contract": {
    "keycloak_client": "qa-stagec-smoke",
    "realm": "EterniaStaging",
    "k8s_secret_path": "eternia-staging/stagec-smoke-credentials",
    "callback_url": "http://localhost:8890/"
  },
  "classification": "PASS"
}
```

The `credential_contract` block is **required** for Stage C closure per [Pass 2 §R4](08-second-pass-audit-and-expansion.md#r4--stage-c-smoke-credential-contract-stage-1--stage-5). Stage 5 refuses PASS if these values drift from the configured contract.

## Portability work (load-bearing, not optional)

Per [Agent QA & Release Doctrine](../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Agent%20QA%20%26%20Release%20Doctrine.md#stage-c--launcher-doctrine-summary): "Tony expects the dev team to finish a new Windows version next week and plans to port this QA/control/MCP work over."

The closure doc must declare:

1. **Paths to revalidate** on new build: Launcher exe path, callback URL (`localhost:8890`), VM-service port discovery, debug runtime envelope.
2. **Contracts that stay**: the 10 `mcp_launcher_qa_*` tool names + schemas, the 5 fixture labels, the `ext.eternia.qa.<verb>` bus shape, the `serialised: true` mutex semantics.
3. **Contracts that move**: any tool that touches Win32 window enumeration (`Capture-StageCWindowScreenshot.ps1`) must be revalidated against the new window class.

## Acceptance

Stage 1 is closed when:

- `closure_manifest.safe.json.classification == "PASS"` on **both** the current Launcher and the new Windows build.
- A fresh Hermes session in `launcher-qa` profile lists `stagec-launcher-qa` via `hermes mcp list` and calls every tool through the model with parity 0 mismatches.
- Redaction findings = 0 across artifact root.
- The portability checklist above has a green box per item.
- Closure manifest committed (the file, not the artifacts — artifacts stay gitignored under `qa-artifacts/`).

## Risks

- **Treating PowerShell green as MCP green.** Already burned us; the parity gate exists for this. Closure must call `mcp_discovery` + `mcp_tool_call` paths explicitly.
- **Screenshot placeholders.** Hardened in commit `97194f58` (live-mode fail-closed for placeholder screenshots) — keep the test.
- **Auth secret stale on hosted Stage C.** Documented recovery in [STAGEC_CREDENTIAL_RESYNC_AND_HOSTED_PASS_2026-05-15.md](../../../../../Unreal%20Engine/Engine/Launcher/EterniaLauncher/docs/stages/qa-reboot/STAGEC_CREDENTIAL_RESYNC_AND_HOSTED_PASS_2026-05-15.md). Closure must reference this doctrine, not duplicate it.
- **Windows stdin BOM regression.** Fixed in commit `baca5406`; tests must lock UTF-8-without-BOM piping for browser-login password input.

## Out of scope

- Generalizing the Launcher MCP into a "product MCP framework" — premature.
- Adding new tools beyond the 10. New tools go through a separate spec doc, not Stage 1.
- macOS / Linux ports. Roadmap explicitly says Windows-first.
