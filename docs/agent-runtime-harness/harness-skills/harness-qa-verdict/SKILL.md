---
name: harness-qa-verdict
description: QA review discipline for Eternia work — what counts as evidence, when visual/Stage C proof is required, cross-stack coverage, and reporting gaps honestly on a chat turn.
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
> `docs/agent-runtime-harness/16-mission-lane-removal.md`. There is no HUD, no gate, no
> proof store, and no release you can block. Your verdict is now an opinion delivered in
> chat, and it is worth exactly as much as the evidence you actually looked at.
>
> Stage C visual proof is a KEEP: the `launcher_qa` MCP server, the PowerShell helpers, and
> the screenshot artifacts are all live. "Proof ID" below means a Stage C artifact handle or
> a command result you or someone else really produced — not a row in a deleted store.

Use this skill for QA review of Eternia implementation work.

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

## Stage C Visual Review

Before reviewing or asking for a Launcher Stage C screenshot/video, verify the capture came
from a fresh intended Launcher window. If `eternia_launcher.exe` or a stale
`stagec_qa_mcp_server.exe` was already running before the rebuild/capture, require a
cleanup/relaunch or classify the artifact as stale. This preflight is for Windows
build/Marionette/visual freshness; do not reinterpret ordinary `flutter test` assertion
failures as process-lock failures unless the log shows a lock/attach/build error.

If a matching screenshot/video already exists for the same target and state, inspect it
rather than asking for another copy.

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
