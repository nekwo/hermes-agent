# Stage 16 — Deterministic Proof Collection and Live Persona Smoke

## Goal

Close the remaining Stage 14/15 AAA gaps by giving Mission Control a deterministic command/test proof pipeline and a credential-aware live-smoke contract. Dev should be able to request tests/builds through structured JSON; the Harness executes bounded commands, persists redaction-safe proof artifacts, adds proof IDs to the mission, and feeds those IDs back into later Dev/QA handoffs.

## Product stance

Mission Control remains a simple goal → PM → Dev → QA → proof → ready/intervention loop. This stage does not add Kanban, broad workflow designers, or a second agent runtime. It adds the missing proof substrate so agents stop needing to invent or manually reference proof.

## Codebase audit

- `agent_runtime/decision_schema.py` and `decision_contracts.py` already define `request_test_run` with `{stage_id, commands}`.
- `agent_runtime/planning.py` validates `request_test_run` but currently performs no deterministic command execution or proof attachment.
- `agent_runtime/store.py::ProofStore` persists `Proof` records under `proofs/<task_id>/proof_<proof_id>.json` and emits `proof.attached` events.
- `agent_runtime/models.py::Proof` supports `type`, `path_or_value`, `metadata`, and `redaction_status` without schema changes.
- `agent_runtime/ticker.py::TickEngine` is the right seam to run deterministic side effects after a persona decision and before closing the run.
- `agent_runtime/context_builder.py` already feeds `task.proof_ids` back to personas.
- Existing Stage 15 handoff checks already require Dev/QA to reference existing proof IDs when a `ProofStore` is available.

## Architecture decisions

- Add a small `agent_runtime/proof_runner.py` module for bounded local command proof execution.
- Keep proof artifacts under the Agent Runtime store root, not product repos.
- Run shell commands through bash when available so proof collection matches Hermes operator semantics on Windows/Git Bash (`&&`, single quotes, `$(...)`, POSIX utilities). Fall back to the system shell only if bash is unavailable. Store the exact command string in proof metadata because it comes from the structured persona decision and is operational proof context.
- Mark generated command proof `redaction_status: safe` only after applying a conservative redaction pass to stdout/stderr. Secret-like text is replaced before writing artifacts.
- `request_test_run` does not automatically advance to QA. It attaches proof and leaves the mission in its current Dev/fix state so Dev can inspect proof IDs on the next tick and explicitly hand off with `request_qa_review`/`propose_patch`.
- A failed command still creates proof. The proof metadata carries `exit_code`; QA gates already require passed test proof for approval.

## Stage plan

### 16.1 — Proof runner primitive

Affected files:

- `agent_runtime/proof_runner.py`
- `agent_runtime/paths.py`
- `tests/agent_runtime/test_proof_runner.py`

Acceptance:

- Executes one or more commands in a bounded workdir.
- Captures stdout/stderr/exit code/duration.
- Writes redacted artifact logs.
- Attaches `ProofType.TEST_RUN` records with `redaction_status: safe`.
- Produces stable proof IDs containing task/stage/run identity.

### 16.2 — Tick integration for `request_test_run`

Affected files:

- `agent_runtime/ticker.py`
- `agent_runtime/planning.py`
- `tests/agent_runtime/test_ticker.py`

Acceptance:

- A Dev `request_test_run` decision triggers deterministic proof collection.
- The generated proof IDs are merged into `task.proof_ids` before the task is persisted.
- The run closes successfully when proof collection completes, even when a command exits non-zero; the failed command is represented as proof instead of a provider incident.
- If the command executor itself crashes, Harness opens a classified incident and does not fake proof.

### 16.3 — Persona prompt/context contract

Affected files:

- `agent_runtime/persona_runtime.py`
- `tests/agent_runtime/test_persona_prompts.py`

Acceptance:

- Dev prompt explicitly says: when tests are needed, emit `request_test_run`; after Harness returns proof IDs, hand off to QA with those IDs.
- QA prompt explicitly distinguishes passed/failed command proof.

### 16.4 — Live-smoke readiness contract

Affected files:

- `agent_runtime/smoke.py`
- `hermes_cli/harness.py`
- `tests/agent_runtime/test_smoke_goal.py`
- `tests/hermes_cli/test_harness_cli.py`

Acceptance:

- No-model smoke remains deterministic and passing.
- Live smoke either runs with configured provider/profile readiness or returns a truthful redaction-safe unsupported/readiness result; it must not pretend live-model success without credentials.

### 16.5 — Verification and gap review

Run:

```bash
PYTHONPATH=. venv/Scripts/python.exe -m pytest --timeout-method=thread tests/agent_runtime tests/hermes_cli/test_harness_cli.py -q
PYTHONPATH=. venv/Scripts/python.exe -m hermes_cli.main harness smoke --json --temp-root --no-model
PYTHONPATH=. venv/Scripts/python.exe -m compileall agent_runtime hermes_cli -q
venv/Scripts/python.exe -m ruff check agent_runtime tests/agent_runtime tests/hermes_cli/test_harness_cli.py
git diff --check
```

Then commit locally. Do not push unless Tony explicitly orders it.

## Remaining interventions policy

If live-model smoke cannot run because credentials/profile readiness are missing, record it as an environment/operator intervention, not a Harness success. If full repo tests are too broad for the current turn, distinguish targeted PASS from full-suite status.

## Implementation completion log

Completed in this slice:

- **16.1 proof runner primitive:** added `agent_runtime/proof_runner.py::CommandProofRunner`, which executes bounded commands in an explicit workdir, captures stdout/stderr/exit code/timeout status, writes redacted log artifacts under the runtime store root, and attaches `ProofType.TEST_RUN` records with `redaction_status: safe`.
- **16.2 tick integration:** `TickEngine` now handles `request_test_run` decisions by running deterministic command proof collection after the structured persona decision validates, merging generated proof IDs into `task.proof_ids`, and keeping the mission in Dev/fix state for an explicit subsequent QA handoff.
- **16.3 persona prompt contract:** prompt payload contracts now tell Dev to use `request_test_run` for deterministic command/test proof and then hand off to QA with the generated proof IDs.
- **16.4 live-smoke readiness contract:** `run_smoke(no_model=False)` now returns a redaction-safe `ok: false` live-smoke intervention envelope instead of throwing or pretending live-model success. The CLI non-JSON path also handles this envelope safely.

Verification completed:

```text
PYTHONPATH=. venv/Scripts/python.exe -m pytest --timeout-method=thread tests/agent_runtime tests/hermes_cli/test_harness_cli.py -q
182 passed, 1 warning

PYTHONPATH=. venv/Scripts/python.exe -m hermes_cli.main harness smoke --json --temp-root --no-model
ok: true, final_state: done, proof_ids: [proof_smoke_test, proof_smoke_qa]

PYTHONPATH=. venv/Scripts/python.exe -m hermes_cli.main harness smoke --json --temp-root
ok: false, failure_class: live_model_smoke_not_implemented, intervention emitted

PYTHONPATH=. venv/Scripts/python.exe -m compileall agent_runtime hermes_cli -q
PASS

venv/Scripts/python.exe -m ruff check agent_runtime tests/agent_runtime tests/hermes_cli/test_harness_cli.py
PASS

git diff --check
PASS
```

Remaining non-blocking gaps:

- Credentialed live persona smoke reached the real PM Codex dispatch path, proving profile dispatch no longer fails as `model_invalid_output`; the current blocker is a truthful `provider_auth_failure` for the PM profile/global Codex OAuth token (`HTTP 401 token_expired`). Operator action: refresh Codex OAuth from a real `codex` session, then re-run the temp-root live smoke.
- The first QA-requested command proof run exposed a Windows shell mismatch: Python `shell=True` used `cmd.exe`, which rejected POSIX/Git-Bash commands like `cd 'C:/repo/product' && ...`. The proof runner now prefers bash and records `metadata.shell` so future proof artifacts are auditable.
- Existing active Launcher recovery task `task_768cb054` is now truthfully blocked by QA because the current attached proof package is not reviewable/labeled enough for the acceptance criteria; that is a product-proof gap, not a Harness runtime crash.
- Command proof redaction is conservative regex-based. For public/upstream artifact sharing, add deeper scanner integration before marking arbitrary third-party logs safe outside the local runtime store.
