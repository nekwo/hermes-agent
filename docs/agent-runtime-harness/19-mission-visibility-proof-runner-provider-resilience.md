# Stage 19 — Mission Visibility, Proof Runner, and Provider Resilience

## Purpose

Make Mission Control truthful without Alice manually spelunking JSON files. A goal should answer:

- what happened,
- what proof exists,
- what is running now,
- why it is not done,
- whether a blocker is product, provider, harness, or human-intervention work.

## Delivered slice

### 19.1 Redaction-safe goal visibility

`build_snapshot()` now includes per-task operator visibility fields:

- `timeline` — recent redaction-safe task events, run IDs, and persona IDs.
- `proof_summaries` — proof ID, type, pass/fail/verdict status, exit code, duration, creator, and artifact presence without artifact paths.
- `next_action` — deterministic next Harness action or `blocked_by_incident`.
- `why_not_done` — concrete reasons such as open incidents, missing proof requirements, or waiting action.

The snapshot intentionally excludes raw incident summaries, full proof paths, model output, secrets, and local absolute paths.

### 19.2 Deterministic command proof runner hardening

`CommandProofRunner` already executes bounded POSIX/Git-Bash-compatible proof commands, persists redacted artifacts, attaches `test_run` proofs, and feeds proof IDs back into task state. This stage adds proof duration metadata for Mission Control summaries.

Proof metadata includes:

- command status,
- exit code,
- timeout flag,
- timeout budget,
- shell label,
- duration in milliseconds,
- safe artifact handle.

### 19.3 Provider resilience

`TickEngine` now retries transient provider failures inside the same tick before opening a human-blocking incident.

Retryable examples:

- TTFB / first-byte stalls,
- no bytes from provider stream,
- temporary timeout text,
- rate limit classification,
- temporary connection/server failures.

Non-retryable examples:

- auth failures,
- missing runtime dependencies,
- invalid model decision JSON/schema,
- tool policy violations.

Each attempt is persisted as a run with safe retry metadata (`retry_attempt`, `retry_max_attempts`, `retryable`). Exhausted retries still open a classified incident.

### 19.4 Artifact retention cleanup

Launcher drawer QA proof artifacts were moved out of the Launcher repo into Alice's local archive and the archive is pruned to the latest five runs:

```text
<hermes-home>/profiles/<profile>/archive/launcher-qa-artifacts/fresh-drawer-goal-smoke/<timestamp>
```

The Launcher repo is clean after cleanup.

## Verification

Targeted tests added/updated:

```bash
venv/Scripts/python.exe -m pytest --timeout=120 --timeout-method=thread \
  tests/agent_runtime/test_stage19_visibility.py \
  tests/agent_runtime/test_ticker.py::test_transient_provider_ttfb_retries_once_and_records_attempt_visibility \
  tests/agent_runtime/test_ticker.py::test_tick_collects_command_proof_for_request_test_run -q
```

Expected result:

```text
3 passed
```

Broader gate for this stage should include:

```bash
venv/Scripts/python.exe -m pytest --timeout=120 --timeout-method=thread \
  tests/agent_runtime/test_stage19_visibility.py \
  tests/agent_runtime/test_ticker.py \
  tests/agent_runtime/test_aaa_gap_fixes.py \
  tests/agent_runtime/test_context_builder.py \
  tests/agent_runtime/test_decision_schema.py \
  tests/hermes_cli/test_harness_cli.py -q
```

## Remaining AAA interventions

- Full Mission Control UI should render the new `timeline`, `proof_summaries`, `next_action`, and `why_not_done` fields directly instead of making operators inspect raw snapshots.
- Provider fallback to a different model/profile is not implemented yet; this stage adds bounded retry and classification only.
- Proof runner command allowlisting/sandbox policy may need hardening before autonomous arbitrary repo command execution is allowed beyond controlled Harness goals.
