# Stage 36 — Launcher Dev Alice-Parity Hardening

## Goal

Make `dev` / Launcher Dev Agent behave closer to Alice for bounded frontend implementation work without weakening proof gates or QA independence.

## Product stance

Launcher Dev is not meant to become an unrestricted Alice clone. It should become a reliable repo-scoped implementation specialist that:

- slices broad missions before implementation;
- produces early patch/test/proof progress or blocks;
- records tool-loop telemetry in redaction-safe run progress/events;
- routes broad or stuck work back to Neko instead of burning model calls;
- uses deterministic proof collection and never self-approves.

## Current audit evidence

- `agent_runtime/persona_runtime.py` invokes profile-bound `AIAgent` with repo scope and budgets, then parses one final `AgentDecision`.
- `agent_runtime/profile_runner.py` already emits tool start/finish/progress callbacks and enforces API/token/wall budgets after results.
- `agent_runtime/progress.py` persists only the latest progress payload; it does not aggregate tool counts or expose patch/test/proof progress flags.
- `agent_runtime/state_machine.py` routes `PM_READY_FOR_DEV` directly to Dev unless context/issue triage blocks it.
- `agent_runtime/planning.py` validates role contracts, but broad Dev missions can still enter expensive live ticks.
- Live evidence: Dev hit old total-token runaway, then after Stage 35 hit API-call budget quickly. Budget stops are safer but not sufficient.

## Fixed architecture decisions

1. Add **Dev discipline telemetry** as redaction-safe run progress fields and events, not raw logs.
2. Add a **pre-Dev broad-scope guard**: broad multi-surface missions with no stages route to Neko for slicing before Dev.
3. Add a **post-run early-progress gate**: Dev runs that spend calls without patch/proof/stage-splitting/precise block become an invalid-output incident instead of silently advancing.
4. Keep `dev` and `backend_dev` as role=`dev`; no self-approval or QA verdict authority.
5. Keep live budget caps low; better to split than continue poisoned broad sessions.

## Stages

### Stage A — RED tests for discipline contracts

Files:

- `tests/agent_runtime/test_dev_discipline.py`
- existing `tests/agent_runtime/test_ticker.py`
- existing `tests/agent_runtime/test_progress.py` if present, otherwise add coverage to new test file.

Tests:

- broad PM-ready mission with no stages and multiple repo/surface signals routes to `RUN_NEKO_SUPERVISOR`, not `RUN_DEV`.
- non-broad PM-ready mission still routes to `RUN_DEV`.
- progress sink aggregates tool counts and marks `has_patch_progress`, `has_test_progress`, and repeated read/search loops safely.
- Dev run with high API calls and no patch/proof/split/block is rejected/open incident.
- Dev run with `propose_stage_plan`, `request_test_run`, `block`, or patch/proof progress is allowed.

### Stage B — Runtime primitives

Files:

- `agent_runtime/dev_discipline.py` (new)
- `agent_runtime/progress.py`
- `agent_runtime/state_machine.py`
- `agent_runtime/ticker.py`

Implementation:

- Heuristic `needs_supervisor_slicing(task)` for broad unsliced missions.
- `RunDisciplineTelemetry` derived from `run.progress` and/or recent events.
- Progress aggregation keys: `tool_call_count`, `read_search_count`, `patch_count`, `test_count`, `has_patch_progress`, `has_test_progress`, `has_proof_progress`, `loop_warning`.
- Post-run `validate_dev_progress_gate(persona, run, decision)` called before state mutation.

### Stage C — Prompts/config/docs

Files:

- `agent_runtime/prompts/dev.md`
- `agent_runtime/prompts/alice_supervisor.md` / Neko prompt if present
- Alice `config.yaml` already caps Dev at 6 API calls / 250k tokens.

Implementation:

- Dev prompt already says at most six model/tool turns; keep tests ensuring it.
- Add Neko prompt wording for broad-scope slicing when relevant.

### Stage D — Proof and smoke

Commands:

```bash
venv/Scripts/python.exe -m pytest -o addopts='' -p no:timeout tests/agent_runtime/test_dev_discipline.py tests/agent_runtime/test_ticker.py tests/agent_runtime/test_progress.py -q
venv/Scripts/python.exe -m pytest -o addopts='' -p no:timeout tests/agent_runtime -q
venv/Scripts/python.exe -m compileall agent_runtime tests/agent_runtime
git diff --check
```

Live smoke:

```bash
venv/Scripts/python.exe -m hermes_cli.main --profile alice harness snapshot --json
venv/Scripts/python.exe -m hermes_cli.main --profile alice harness run-until-settled --task task_a83fc30a --max-actions 1 --max-seconds 420 --json
```

Acceptance:

- broad live mission routes to Neko or Dev produces a small stage/proof/block within budget;
- no open incidents after manual handling;
- test suite passes;
- local commit created.

## AAA gaps to re-stage if found

- Tool callbacks do not expose enough invocation metadata to classify reads/searches reliably.
- Neko cannot split broad work with a strict schema.
- Dev still chooses large-stage plans; may require deterministic task child-splitting rather than only prompts.
- Launcher UI may need to surface discipline telemetry separately after Harness emits it.
