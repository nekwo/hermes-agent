# Stage 5 — Release MCP

> Parent: [`../mcp-expansion-roadmap.md`](../mcp-expansion-roadmap.md) §Layer 2 → `arcadia_release_mcp` + §Recommended staged build → Stage 5.
> Audit context: [`00-deep-audit.md`](00-deep-audit.md) §A.6, §A.7.

## Goal

Encode the company release classification rubric ([Agent QA & Release Doctrine](../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Agent%20QA%20%26%20Release%20Doctrine.md)) as a typed MCP surface. Stage 5 does not run tests itself — it **collects** results from Stage 1 / Stage 4 outputs, **verifies** artifact manifests and redaction scans, and **classifies** the run.

The output is a closure note that Tony / PM / reviewer can act on without each rebuilding the rubric in chat.

## Inventory (existing)

| Asset | Path | Role |
|---|---|---|
| Doctrine | [`ArcadiaLabs_Brain/Agent QA & Release Doctrine.md`](../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Agent%20QA%20%26%20Release%20Doctrine.md) | canonical PASS / NEEDS_FIX / FAIL_NON_BLOCKING_TOOLING_PARITY / NOT_RUN_MISSING_CONTEXT rubric |
| Env policy | [`ArcadiaLabs_Brain/Environment & Staging Policy.md`](../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Environment%20%26%20Staging%20Policy.md) | what staging-vs-production touches mean |
| Backend gate | `eternia-backend/scripts/test.sh` | Postgres full gate, sqlite escape hatch |
| Launcher gate | `tool/stagec_qa_mcp_server` + `Test-StageCHermesMcpFullParity.ps1` | 5-label parity gate |
| Stage 1 closure manifest shape | [`02-stage-1-launcher-mcp.md`](02-stage-1-launcher-mcp.md#5-closure-artifact-manifest) | the shape Stage 5 consumes |
| Redaction | `agent/redact.py`, `Invoke-RedactionScan.ps1` | redaction patterns |

## Tool surface

### Collection

| Tool | Args | Returns |
|---|---|---|
| `arcadia_release_collect_gate_results` | `target` (`launcher` / `backend` / `cross`), `commit?`, `since?` | Walks the artifact root for the target, finds `*.safe.json` manifests, returns a flat list of gates run + their classifications. |
| `arcadia_release_verify_artifacts` | `manifest_path` | Validates that every file referenced from the manifest exists, sizes are non-zero, paths are inside the artifact root, no `..` escapes. |
| `arcadia_release_verify_redaction` | `artifact_root` | Re-runs the redaction scan over the artifact root using the canonical pattern set. Returns `{files_scanned, findings, patterns_checked}`. Findings > 0 forces `REDACTION_FAIL`. |
| `arcadia_release_check_branch_state` | `repo_path` | Returns `{commit, branch, dirty, ahead_of_main, behind_main, untracked_count}`. Required input for classification. |
| `arcadia_release_verify_credential_contract` | `target` (`launcher` only for now) | Per [Pass 2 §R4](08-second-pass-audit-and-expansion.md#r4--stage-c-smoke-credential-contract-stage-1--stage-5). Verifies the live k8s secret exists at `eternia-staging/stagec-smoke-credentials` and the Keycloak realm `EterniaStaging` is reachable at its well-known config endpoint. **Never fetches secret values.** Returns `{secret_present: bool, realm_reachable: bool, callback_url_matches: bool}`. Drift returns `error_class: AUTH_REQUIRED`. |

### Classification

| Tool | Args | Returns |
|---|---|---|
| `arcadia_release_classify` | `gate_results[]`, `artifact_verify`, `redaction_verify`, `branch_state`, `target` | Applies the rubric (see below) and returns `{classification, blockers[], next_actions[], rationale}`. Pure function — no I/O. |
| `arcadia_release_create_closure_note` | `vault="arcadia"`, `target`, `commit`, `classification`, `summary_md`, `manifest_path`, `dry_run=true` | Wraps [Stage 3 `arcadia_brain_create_handoff`](04-stage-3-arcadia-brain-mcp.md#mutating-tools-append-only-by-default) with the release-specific shape. Refuses to write a `PASS` closure if `artifact_verify`/`redaction_verify` are not green. |

## Classification rubric (encoded)

Inputs and outputs (all pure data):

```jsonc
// Input
{
  "target": "launcher" | "backend" | "cross",
  "gate_results": [
    {"gate": "self_test",        "status": "PASS"|"FAIL"|"NOT_RUN", "evidence_path": "..."},
    {"gate": "mcp_discovery",    "status": "...", "evidence_path": "..."},
    {"gate": "mcp_tool_call",    "status": "...", "evidence_path": "..."},
    {"gate": "parity_diff",      "status": "...", "mismatches": 0},
    {"gate": "postgres_full",    "status": "...", "exit_code": 0},
    {"gate": "makemigrations",   "status": "...", "drift_detected": false},
    {"gate": "manage_check",     "status": "...", "exit_code": 0}
  ],
  "artifact_verify": {"ok": true, "missing": []},
  "redaction_verify": {"findings": 0, "files_scanned": 87},
  "branch_state": {"commit": "0654d2c", "dirty": false, "branch": "main", "ahead_of_main": 0}
}

// Output classes (all from the roadmap's error class table)
//   PASS                                  — every required gate green, redaction 0, artifact verify ok
//   NEEDS_FIX                             — at least one required gate FAIL or recoverable
//   FAIL_NON_BLOCKING_TOOLING_PARITY      — gates green, parity diff has acceptable mismatches
//   NOT_RUN_MISSING_CONTEXT               — required gate NOT_RUN (host env missing, no docker, etc.)
//   REDACTION_FAIL                        — redaction_verify.findings > 0 (always blocking)
//   ARTIFACT_MISSING                      — artifact_verify.missing non-empty
```

Required-gate matrix:

| Target | Required gates |
|---|---|
| `launcher` | `self_test`, `mcp_discovery`, `mcp_tool_call`, `parity_diff`, `screenshot_sanity`, `redaction` |
| `backend` | `manage_check`, `makemigrations`, `postgres_full`, `redaction` |
| `cross` | both lists, union |

The rubric is locked: any addition to required gates is a config-level change to the MCP, not a per-call argument. This is what makes it auditable.

## Sqlite-escape-hatch policy (encoded)

Per [Agent QA & Release Doctrine](../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Agent%20QA%20%26%20Release%20Doctrine.md#backend-deployment-doctrine-summary): "SQLite is only an explicit Tony-requested escape hatch."

`classify` accepts a `sqlite_escape_hatch: bool = false` flag. If true:

- `postgres_full` is **not** required.
- The classification can still be PASS, but the output `rationale` MUST include `"sqlite_escape_hatch=true"` and the `next_actions[]` MUST include `"re-run scripts/test.sh without --sqlite before push/deploy"`.
- `create_closure_note` writes that caveat to the note as a bold callout.

## CI parity rule (encoded)

Per same doctrine: "GitHub Actions may be authoritative if local WSL full gate hangs, but local PASS should not be claimed without the local full gate."

`classify` accepts `ci_evidence_url: str | null = null`:

- If present and the local `postgres_full` is `NOT_RUN`, the classification can be `PASS` only with `ci_evidence_url` set AND the URL must be from the configured CI host (allowlist).
- Otherwise `NOT_RUN_MISSING_CONTEXT`.

## Closure note shape

`arcadia_release_create_closure_note` writes to `ArcadiaLabs_Brain/closures/<DATE>-<target>-<classification>-<short-commit>.md`:

```markdown
---
type: closure
target: launcher
classification: PASS
commit: 0654d2c
branch: main
created_at: 2026-05-15T18:42:11Z
manifest_path: qa-artifacts/stagec_closure_20260515_184201/closure_manifest.safe.json
---

# Launcher closure — PASS @ 0654d2c

## Gates run
- self_test: PASS
- mcp_discovery: PASS
- mcp_tool_call: PASS
- parity_diff: PASS (0 mismatches)
- screenshot_sanity: PASS
- redaction: PASS (87 files, 0 findings)

## Branch state
clean, on main, 0 ahead, 0 behind

## Blockers
none

## Next actions
- Port to next-week Windows build per portability checklist
- Wire Stage 2.5 mutating control-plane after read-only is shipped

## Linked artifacts
- closure_manifest.safe.json (sha256: ...)
- parity_diff.safe.json
- redaction_scan.safe.json
```

## Acceptance

Stage 5 is done when:

1. `hermes mcp arcadia release serve` runs and exposes the 6 tools above.
2. `classify` returns the documented classification for each of these scripted scenarios:
   - All gates green → PASS
   - `postgres_full=NOT_RUN`, no CI evidence → `NOT_RUN_MISSING_CONTEXT`
   - `postgres_full=NOT_RUN`, valid CI evidence → PASS (with rationale citing CI)
   - `redaction.findings=1` → `REDACTION_FAIL` even if everything else green
   - `parity_diff.mismatches=1` with `--max-allowed-mismatches=2` → `FAIL_NON_BLOCKING_TOOLING_PARITY`
   - Sqlite escape hatch true → PASS with mandatory caveat in `next_actions[]`
3. `create_closure_note` writes to the right path and refuses to write PASS when redaction or artifact verification failed.
4. The rubric is locked behind config — a test asserts no caller can inject a new gate name through args.

## Risks

- **Hidden gate added by caller.** Mitigation: `classify` ignores unknown gates and lists them under `rationale.ignored_gates[]`; never silently accepts a new required gate.
- **CI URL spoof.** Mitigation: CI allowlist is config-level, not arg-level. `ci_evidence_url` must match host pattern.
- **Schema drift over time.** Mitigation: the locked rubric is versioned (`rubric_version: 1`); a bump requires a doc change AND a test update.
- **Closure note overwrite.** Already guarded by Stage 3's append-only contract — `create_closure_note` refuses if the target path exists.

## Out of scope

- Auto-deploy on PASS. The closure note is the artifact; the human still calls `kubectl` / `git push`.
- Multi-commit PRs. One closure note per commit.
- Roll-up cross-project releases beyond `target: cross` (launcher + backend). Multi-product roll-up is a future Stage 6.
