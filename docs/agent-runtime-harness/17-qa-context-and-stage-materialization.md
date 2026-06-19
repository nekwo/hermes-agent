# Stage 17 — QA Context and Stage Materialization

## Goal

Finish the live PM → Dev → deterministic proof → QA loop by eliminating the Stage 16 smoke blocker where QA asked for a profile-local task JSON path instead of reviewing the shared Harness state already available in the tick context.

## Product stance

Mission Control stays a simple goal → PM → Dev → QA → proof → ready/intervention loop. Stage 17 does not add new credential systems, Kanban, or broad workflow UI. It makes the existing loop state-consistent and reviewable so QA can verify proof without guessing filesystem paths.

## Live blocker evidence

Live temp-root smoke after Codex auth/global-store sync reached:

- PM `propose_acceptance` — valid live model decision.
- Dev `request_test_run` — valid live model decision.
- Harness attached deterministic command proof: `printf 'smoke-ok\n'`, `exit_code=0`, `metadata.shell=bash`.
- Dev `request_qa_review` — valid live handoff with proof ID.
- QA `request_file_reads` for `~/.hermes/profiles/launcher-qa/agent_runtime/tasks/<task_id>.json` — unsupported/path_not_found because the mission lives in the shared runtime root, not the QA profile home.

The root issue is not auth anymore. It is Harness context/state consistency:

1. `request_test_run` can refer to `stage_1` without a concrete `TaskStage` record.
2. A proof-backed Dev QA handoff from a planning-ish state can route to `qa_review_plan` instead of implementation QA.
3. QA context renders only the current stage and proof IDs, not a complete review snapshot of all stages/proof metadata.

## Architecture decisions

- Treat `request_test_run` with a `stage_id` as an implementation/proof-producing action. If the stage does not exist, synthesize a concrete `TaskStage` using the task acceptance criteria and requested commands as the test plan.
- When command proof is requested from PM-ready/planning states, move the mission into `dev_implementing` so the next proof-backed `request_qa_review` routes to implementation QA (`dev_ready_for_qa`) instead of plan review.
- Render all persisted stages and redaction-safe proof metadata directly in `AgentContext`; QA should not need to read profile-local runtime files.
- Keep task JSON file reads as a last-resort context mechanism. Do not expose raw auth/config paths or encourage profile-local runtime paths.

## Stage plan

### 17.1 — Materialize command-proof stages

Affected files:

- `agent_runtime/planning.py`
- `tests/agent_runtime/test_planning.py` or equivalent existing planning tests

Acceptance:

- A `request_test_run` decision with `stage_id=stage_1` creates `TaskStage(stage_1)` when missing.
- The synthesized stage has acceptance criteria and a test plan derived from the task and requested commands.
- The mission current stage points at the synthesized stage.
- PM-ready/planning states move to `dev_implementing` after proof collection is requested.

### 17.2 — Route proof-backed QA handoffs as implementation review

Affected files:

- `agent_runtime/planning.py`
- `tests/agent_runtime/test_planning.py` / `test_ticker.py`

Acceptance:

- After `request_test_run`, a Dev `request_qa_review` with existing proof IDs routes to `dev_ready_for_qa`.
- Missing/unknown proof IDs still fail through existing proof validation.
- Plan-review routing remains available for actual plan-review states.

### 17.3 — Render a complete QA review context

Affected files:

- `agent_runtime/context_builder.py`
- `tests/agent_runtime/test_context_builder.py`

Acceptance:

- Rendered context includes a compact task snapshot, all stages, current stage, and proof metadata for `task.proof_ids`.
- Proof metadata is redaction-safe and bounded: proof ID, type, stage ID, title, path/value label, redaction status, and selected metadata like command status/exit code/shell.
- QA has enough information to approve/block from context without requesting profile-local task JSON.

### 17.4 — Verify with live temp-root smoke

Run targeted tests, full Harness tests, compile/lint/diff hygiene, then one live temp-root smoke. Desired live result:

```text
PM propose_acceptance
Dev request_test_run
Harness proof attached
Dev request_qa_review
QA report_qa_verdict/approve
PM complete/done or exact remaining intervention
```

## Verification commands

```bash
PYTHONPATH=. venv/Scripts/python.exe -m pytest --timeout-method=thread tests/agent_runtime/test_context_builder.py tests/agent_runtime/test_context_requests.py tests/agent_runtime/test_ticker.py tests/agent_runtime/test_aaa_gap_fixes.py -q
PYTHONPATH=. venv/Scripts/python.exe -m pytest --timeout-method=thread tests/agent_runtime tests/hermes_cli/test_harness_cli.py -q
PYTHONPATH=. venv/Scripts/python.exe -m compileall agent_runtime hermes_cli -q
venv/Scripts/python.exe -m ruff check agent_runtime tests/agent_runtime tests/hermes_cli/test_harness_cli.py
git diff --check
```

## Remaining gap policy

If QA still asks for a nonexistent profile-local runtime path after context/stage fixes, treat it as a prompt/context regression and block with exact evidence. Do not mutate auth stores or profile homes as part of Stage 17.
