# Cross-cutting concerns

> Parent: [`../mcp-expansion-roadmap.md`](../mcp-expansion-roadmap.md) §Permission model, §Error classes, §Pitfalls, §Verification checklist.
> Audit context: [`00-deep-audit.md`](00-deep-audit.md) §C.

Every stage doc references this one. The goal is to make the roadmap's cross-cutting rules executable without duplicating them in each stage.

## 1. Permission model — implemented at the profile layer

The roadmap's profile-scoped permissions translate to: **`mcp_servers` entries in `~/.hermes/profiles/<p>/config.yaml` are the permission boundary.** Each new MCP server gets a curated allowlist of profiles, not an in-server ACL.

Default allowlist matrix (revised in pass 3 to include Stage 4.5 PM, Stage 6 Backend, and existing messaging bridge):

| Profile class | S1 launcher | S2 control (R) | S2.5 control (R+W) | S3 brain | S4 agentops | S4.5 PM | S5 release | S6 backend | messaging |
|---|---|---|---|---|---|---|---|---|---|
| `alice` | R | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `pm` | — | ✓ | partial (kanban+cron) | ✓ | ✓ | ✓ | ✓ | lifecycle+check+classify | — |
| `reviewer` | R | ✓ | — | R | — | R (advisory) | R | R | — |
| `brain-writer` | — | ✓ | — | ✓ (write) | — | — | — | — | — |
| `launcher-qa*`, `claude_launcher_qa` | ✓ | ✓ | — | — | — | — | R | — | — |
| `claude_launcher`, `gpt-launcher` | — | kanban-only | — | — | — | — | — | — | — |
| `claude_backend`, `gpt_backend` | — | kanban-only | — | — | — | — | — | gate+check+makemigrations | — |
| `spark_launcher`, `spark_backend`, `spark_testwriter` | — | kanban-only | — | — | — | — | — | (spark_backend: check-only) | — |
| `spark_docs` | — | kanban-only | — | vault-only | — | — | — | — | — |
| `spark_logreader` | — | logs-only | — | — | — | — | — | check-only | — |

Legend: ✓ full · R read-only · — disallowed · "kanban-only" / "lifecycle+check+classify" / etc. = scoped subset

This matrix is the source of truth for the example `mcpServers` configs in [`examples/`](examples/).

## 2. Error classes — one taxonomy, every server

Every tool in every stage returns either a success envelope or:

```json
{"error_class": "<CLASS>", "message": "<short safe string>", "retryable": <bool>}
```

Classes (from [roadmap §Error classes](../mcp-expansion-roadmap.md#error-classes)):

| Class | When | Retryable |
|---|---|---|
| `MISSING_CONTEXT` | Required arg or upstream state missing (Launcher not running, vault not configured) | yes — caller can provide it |
| `AUTH_REQUIRED` | Caller is unauthenticated for an external resource (Keycloak token expired) | yes — refresh and retry |
| `HOST_ENV_MISSING` | Host lacks Docker / VM-service / Python module | no — operator must fix host |
| `TOOLING_PARITY_FAIL` | Direct↔MCP envelope diff exceeds tolerance | no — investigate |
| `MCP_DISCOVERY_FAIL` | `hermes mcp test <server>` failed | no |
| `MCP_TOOL_CALL_FAIL` | Tool call returned non-zero or non-schema-conforming | no |
| `QA_GATE_FAIL` | Stage 5 classifier saw a required gate FAIL | no |
| `REDACTION_FAIL` | Redaction scan found `findings > 0` | **never** retry — always blocking |
| `ARTIFACT_MISSING` | Manifest references a file that does not exist | no |
| `PROCESS_STALE` | Worker heartbeat older than `reap_threshold_s` | no — reap |
| `NOT_RUN_MISSING_CONTEXT` | A required gate was not run because context wasn't satisfied | yes — provide context |

Implementation rule: error envelopes are returned, **not raised**. A raised exception is a Stage 0 catalog-test failure.

## 3. Redaction — single pipeline

There is exactly one source of truth for redaction patterns: `agent/redact.py`. Every new MCP tool MUST funnel return values through one of:

- `agent.redact.redact(text: str, kind: str = "default") -> str` — for free-text log tails.
- `agent.redact.redact_dict(d: dict, paths: list[str]) -> dict` — for structured envelopes; drops keys named in `paths` and recursively redacts string values.
- `Invoke-RedactionScan.ps1` — for after-the-fact scans of artifact roots.

Patterns under audit (current as of Stage C closure):

- `Bearer\s+[A-Za-z0-9._-]{20,}` (commit `3969973e`)
- `pk_live_`, `sk_live_`, `pk_test_`, `sk_test_`
- JWT shape: `eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+`
- VM-service tokens: `wsUri=ws://[^"]*?/ws`
- AWS-shape keys: `AKIA[0-9A-Z]{16}`, `aws_secret_access_key\s*=`
- Keycloak callback codes in URLs: `code=[A-Za-z0-9.-]{20,}`

Any new MCP tool surface must be redaction-scanned in CI against a fixture log; a single finding fails the build.

## 4. Audit log — targeted stores (revised by [Pass 2 §R1](08-second-pass-audit-and-expansion.md#r1--audit-log-location-stage-2--stage-4--cross-cutting-4-correction))

> **Pass-1 statement here was wrong** — `state.db` is the session FTS store, not an event store, and kanban events live in a **shared** DB at the hermes-home root, not per-profile. The revised matrix below is canonical.

There are three audit stores, each owned by its server class:

| Store | Owned by | What it captures | Path |
|-------|----------|------------------|------|
| `control_events` table | Stage 2 / 2.5 / 4.5 / 5 / 6 (all server-side mutations + read audits where opted in) | typed MCP tool calls outside the worker dispatch path | sibling table in shared `~/.hermes/kanban.db` (or `~/.hermes/kanban/boards/<slug>/kanban.db`) — schema in [`schemas/control_events.sql`](schemas/control_events.sql) |
| `processes.json` | Stage 4 agentops | worker spawn / reap / kill with PID + effective toolsets | `~/.hermes/profiles/<caller>/processes.json` (already exists for each worker profile) |
| `.brain-mutation-log.jsonl` | Stage 3 brain | every brain note write (append-only, vault-scoped) | `<vault.root>/.brain-mutation-log.jsonl` |

Rules:

1. **Mutations always audit.** Stage 2.5 / Stage 4 / Stage 5 / Stage 6 mutating tools write a row before returning, even in `dry_run=true`.
2. **Reads audit by opt-in.** `HERMES_CONTROL_MCP_AUDIT_READS=1` enables read auditing on `hermes-control`. Off by default to keep `control_events` from filling with `kanban_list_cards` noise.
3. **Args are stored post-redaction.** The args JSON in `control_events.args_json` is the same shape the caller sent, minus any field caught by `agent/redact.py`.
4. **Retention is the profile curator's job.** `curator.archive_after_days` (90d default) prunes `control_events`. No trigger-based TTL — triggers cost a write-amplification penalty per insert.

`task_events` (existing kanban event table) stays untouched — control-plane mutations are NOT wedged into it with `task_id=NULL` because that would distort `list_events` and the gateway's kanban-notifier watcher.

## 5. Stage 0 verdict matrix → stage start gates

The roadmap orders the stages but does not order their start gates. Concretely:

- Stage 1 starts immediately (it is a closure on existing work).
- Stage 2 starts after Stage 0's audit doc is committed (so we know what to wrap vs. build).
- Stage 3 starts after Stage 2 is green (Stage 3 depends on the audit-log shape Stage 2 locks).
- Stage 4 starts after Stage 3 is green (`agentops_summarize_worker_result` writes to brain via Stage 3).
- Stage 5 starts after Stage 4 is green (Stage 5 reads agentops outputs).

This is dependency, not strict ordering. Stage 2 and Stage 3 read-only surfaces could parallelize if needed; the roadmap explicitly orders them to keep the work focused.

## 6. Verification checklist (executable form)

Per-server checks, lifted from [roadmap §Verification checklist](../mcp-expansion-roadmap.md#verification-checklist) and made testable:

```jsonc
// docs/architecture/mcp-expansion/verification.schema.jsonc
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": [
    "server", "profile", "commit",
    "checks": [
      "mcp_list_shows_server",
      "mcp_test_passes",
      "fresh_session_lists_tools",
      "selftest_passes",
      "real_tool_call_succeeds",
      "mutations_write_audit_rows",
      "artifact_manifest_exists",
      "redaction_scan_zero_findings",
      "out_of_scope_tools_blocked",
      "closure_note_complete"
    ]
  ],
  "additionalProperties": false
}
```

Every stage closure produces one of these manifests under `docs/architecture/mcp-expansion/closures/<DATE>-stage-<N>-<server>.verify.json`.

## 7. Pitfalls — concrete countermeasures

From [roadmap §Pitfalls](../mcp-expansion-roadmap.md#pitfalls):

| Pitfall | Countermeasure |
|---|---|
| One unrestricted MCP server | Read/write split (Stage 2 vs 2.5), vault allowlist (Stage 3), profile allowlist (Stage 4). |
| Every worker gets every write tool | Stage 4 spawn tool refuses non-allowlisted profiles; worker profiles do not list mutating MCPs. |
| Raw logs with secrets | §3 redaction pipeline is mandatory; CI scans new tool outputs against the fixture log. |
| MCP-package tests instead of fresh discovery | §6 checklist requires `fresh_session_lists_tools` and `real_tool_call_succeeds`. |
| Committed profile config / secrets | Example configs under `docs/architecture/mcp-expansion/examples/` use placeholders and the existing `.local.md` / `.local.example.md` convention. |
| Acceptance ambiguity (MCP vs PowerShell vs HTTP) | Stage 1 closure manifest `gates_run[]` names every transport that ran; Stage 5 classifier rejects PASS claims that only ran the direct transport. |

## 8. Naming conventions

All committed in advance to prevent drift:

- Server names: `hermes-control` (S2), `hermes-control-mutate` (S2.5), `hermes-messaging` (existing `mcp_serve.py`), `arcadia-brain` (S3), `arcadia-agentops` (S4), `arcadia-pm` (S4.5), `arcadia-release` (S5), `stagec-launcher-qa` (S1, kept as-is for back-compat), `eternia-backend` (S6).
- Tool names: `<server>_<family>_<verb>` — e.g. `hermes_kanban_list_cards`, `arcadia_brain_create_handoff`, `eternia_launcher_qa_get_runtime_state` (already in flight).
- Profile env vars: `HERMES_ACTIVE_PROFILE`, `HERMES_CONTROL_MCP_PROFILE_SCOPE`, `HERMES_CONTROL_MCP_ALLOW_MUTATIONS`, `ARCADIA_BRAIN_VAULT_<NAME>_ROOT` (vault roots).
- Artifact roots: `qa-artifacts/<server>_<run-kind>_<UTC-timestamp>/`.

## 9. Where the docs live

```
docs/architecture/
  mcp-expansion-roadmap.md                       # source roadmap (not edited here)
  mcp-expansion/
    README.md                                    # reading order
    00-deep-audit.md                             # audit pass 1
    01-stage-0-discovery.md
    02-stage-1-launcher-mcp.md
    03-stage-2-hermes-control-mcp.md
    04-stage-3-arcadia-brain-mcp.md
    05-stage-4-agentops-mcp.md
    06-stage-5-release-mcp.md
    07-cross-cutting.md                          # this file
    08-second-pass-audit-and-expansion.md        # audit pass 2
    09-stage-4.5-arcadia-pm-mcp.md               # added in pass 3
    10-stage-6-eternia-backend-mcp.md            # added in pass 3
    11-stage-2.5-hermes-control-mutate.md        # added in pass 3
    12-third-pass-addendum.md                    # routines audit, mcp_serve reconciliation, ops concerns
    13-upstream-mcp-stdio-env-overrides.md       # per-process stdio MCP env overrides (SHIPPED)
    examples/                                    # placeholder mcpServers JSON, one per server
    schemas/                                     # pinned contracts (verification, control_events, worker handoff, ...)
    templates/                                   # markdown skeletons (handoff, closure note, escalation, recovery card)
    closures/                                    # one verify.json per stage closure (gitignored runtime artifacts)
    handoffs/                                    # discovery/audit handoffs
```
