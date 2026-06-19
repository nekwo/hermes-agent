# Stage 39 — Runtime Environment and CLI Finalization

## Goal

Make Mission Control live-token execution boring and operator-safe by standardizing the runtime interpreter/CLI entrypoint and adding health checks for provider dependencies before a live Harness tick starts.

## Why this exists

Stage 38 QA hardening passed, but the finalization loop exposed environment drift:

- `python -m hermes` was unavailable from global Python in this checkout.
- Global Python could run tests but initially lacked `openai`.
- The shared Hermes venv had a broken/locked `jiter` install during the first live-token rerun.
- Runtime import health was later verified from the shared venv (`jiter.from_json=True`, `openai 2.24.0`) and global Python (`openai 2.41.0`, `jiter.from_json=True`), but the operator path is still too implicit.

A final Mission Control system should not require Alice/Tony to remember which Python happens to work today.

## Product stance

Mission Control should have one blessed local operator entrypoint for live Harness runs. Before spending tokens, it should fail fast with a compact, redaction-safe health report if provider/client dependencies are missing or corrupted.

## Current evidence

- Stage 38 full Harness gate: `python -m pytest -o addopts="-m 'not integration'" tests/agent_runtime -q` → `347 passed in 20.12s`.
- Stage 38 real-token smokes completed with Dev → Neko → QA → done and no open incidents.
- Environment incident evidence:
  - global Python before repair: missing `openai`.
  - shared Hermes venv before repair attempt: `cannot import name 'from_json' from 'jiter'`.
  - shared Hermes venv after repair/lock churn: imports `jiter.from_json=True` and `openai 2.24.0`.

## Stage 39.1 — Declare the blessed Harness operator command

### Affected files

- `hermes_cli/harness.py`
- `agent_runtime/status.py`
- Harness docs under `docs/agent-runtime-harness/`.

### Implementation actions

- Blessed local operator entrypoint from `X:/Eternia/hermes-agent` is the repo CLI module:
  - `python -m hermes_cli.main harness health --json`
  - `python -m hermes_cli.main harness status --json`
  - `python -m hermes_cli.main harness task create --title "..." --description "..." --requested-by tony --json`
  - `python -m hermes_cli.main harness task list --state open --json`
  - `python -m hermes_cli.main harness run-until-settled --task <task_id> --max-actions <n> --max-seconds <seconds> --json`
- `python -m hermes` remains intentionally out of scope; do not rely on that module path.
- `harness status` now includes the same runtime health block as the dedicated health command so Mission Control/operator surfaces can see drift without log archaeology.

### Proof

- `python -m hermes_cli.main harness health --json` captures interpreter/runtime dependency health without token spend.
- `python -m hermes_cli.main harness status --json` includes `runtime_health` with interpreter, runtime root, Hermes home/profile, required packages, and issues.
- No reliance on `python -m hermes` unless that module path is intentionally implemented.

## Stage 39.2 — Provider dependency preflight

### Affected files

- `hermes_cli/runtime_environment.py`
- `agent_runtime/provider_health.py`
- `agent_runtime/persona_runtime.py`
- Tests under `tests/agent_runtime/test_provider_health.py`.

### Implementation actions

- Added a preflight that verifies required provider dependencies before opening/spending a live persona run:
  - `openai` import availability for OpenAI/Codex providers.
  - `jiter.from_json` availability/callability when OpenAI client runtime is required.
- `GPTPersonaRuntime` calls the preflight before `ProfileAgentRunner.run(...)`.
- Failures raise a bounded `ImportError` naming only dependency kind/package and the interpreter path; no credentials, raw env, request bodies, or provider config are emitted.

### Proof

- Unit test for healthy dependency/status surface.
- Unit test for corrupt/missing `jiter.from_json` producing a bounded provider-health failure before token spend.
- Unit test proving the live persona path raises before constructing/calling the fake runner.

## Stage 39.3 — Runtime root/profile consistency proof

### Affected files

- `agent_runtime/provider_health.py`
- `agent_runtime/status.py`
- `hermes_cli/harness.py`
- Optional Launcher Mission Control docs if UI consumes the status block directly.

### Implementation actions

- Added a documented status/health surface showing:
  - active runtime root,
  - active Hermes home/profile,
  - interpreter path for live ticks,
  - required provider packages and availability,
  - open task/run/incident counts through `harness status`.
- Values are redaction-safe: no tokens, raw env dumps, base URLs, request payloads, or credential fields.

### Proof

- `build_status` test asserts `runtime_health` fields are present and tied to the isolated runtime root.
- Manual command output verifies the local Stage C/Mission Control operator path uses the same runtime health surface Alice uses.

## Acceptance criteria

- A fresh operator can run one documented command to verify Harness runtime health before live-token missions.
- Live Harness ticks fail before token spend when `openai`/`jiter` provider dependencies are missing/corrupt.
- The blessed entrypoint is documented and works on Tony's Windows/Git Bash environment.
- Mission Control status makes runtime-root/profile/interpreter drift visible instead of requiring log archaeology.
- Failed command attempts remain auditable in ProofStore/events but do not pollute final acceptance `task.proof_ids` or QA proof packets unless the stage is explicitly a RED/failing-test proof stage.

## Live multi-agent smoke proof

2026-06-04 follow-up live mission: `task_stage39_live_multi_agent`.

Observed path:

1. Frontend Dev (`dev`, profile `gpt-launcher`) grounded in Launcher and attached command proof.
2. Neko coordinated the sequential specialist join.
3. Backend Dev (`backend_dev`, profile `backend-dev`) grounded in EterniaBackend and attached command proof.
4. Neko coordinated final QA release.
5. QA (`qa`, profile `launcher-qa`) recorded approved implementation verdict.
6. Harness completed the task with `final_task_state=done`, `stop_reason=task_terminal`, `open_incidents=0`.

Gap found during monitoring: early frontend command attempts failed before the successful Launcher repo path was selected, and those failed proof IDs were retained in `task.proof_ids`/QA packet. Closed by changing deterministic command proof handoff so failed attempts remain in ProofStore/event audit history but are not promoted as acceptance proof IDs unless the stage is explicitly a RED/failing-test proof stage.

Proof gates:

- Targeted proof semantics tests: failed non-RED command proof is audited but not promoted; RED-stage failed proof remains acceptable.
- Full Harness non-integration suite rerun after the proof semantics fix.

## Current status

Opened as the next Mission Control finalization gap after Stage 38. Not implemented in Stage 38 because QA hardening itself is complete and verified; this is broader runtime/operator polish.
