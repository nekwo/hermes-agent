# Stage 55 - Persona Operations And Diagnostics

## Goal

Make individual agent operations first-class so Neko, Launcher Dev, Backend Dev, and QA can be tested or communicated with without burning a full multi-agent goal. The operator should be able to run one bounded live persona turn, inspect the decision/validation/token shape, then either archive the diagnostic evidence or continue with a normal goal.

## Implemented Surface

Command:

```powershell
python -m hermes_cli.main harness persona diagnose <persona> `
  --title "Neko routing diagnostic" `
  --message "Scope this no-edit proof path and hand off narrowly." `
  --max-actions 1 `
  --max-seconds 240 `
  --json
```

Accepted persona ids and aliases:

- `neko`, `neko_supervisor`
- `dev`, `launcher-dev`, `launcher_dev`
- `backend-dev`, `backend_dev`, `backend`
- `qa`

Runtime class:

- `agent_runtime.persona_diagnostics.PersonaDiagnosticController`
- `PersonaDiagnosticOptions`
- `PersonaDiagnosticResult`

The controller creates a normal Harness task, marks it with stable persona operation metadata, runs the existing `TickEngine`, and returns a compact result with:

- `operation_id`
- `operation_kind`
- `operation_mode`
- `persona_id`
- task/run ids
- expected vs actual Harness action
- latest decision type
- validation status
- token count when available
- diagnostic stage owner/repo

## Scaling Contract

This is intentionally a persona operation primitive, not a one-off Neko script.

- It uses normal Task/Run/Event/WorkerSession stores so Mission Control can render the same evidence path.
- It records `persona_operation_id`, `persona_operation_kind`, and `persona_operation_mode` in task risk flags and worklogs.
- It starts QA diagnostics from `qa_review_plan` so QA routes correctly under both legacy and typed mission-plan routing.
- It creates typed diagnostic stages for Dev/Backend Dev/QA so the existing persona resolver picks the requested worker.
- It keeps `max_actions` and `max_seconds` bounded by default.
- It preserves evidence and leaves archive cleanup to the normal `harness task archive-ready` path.

## Current Limits

- `diagnose` creates a standalone diagnostic task. It does not yet attach an operator message to an existing live task.
- It does not auto-archive, because diagnostics often need immediate inspection in Mission Control before evidence is moved.
- It verifies routing and one persona decision, not end-to-end product acceptance.

## Next Substages

1. Add `harness persona message --task <task_id> <persona>` to append a bounded operator communication to an existing worker context without changing product scope.
2. Add Mission Control buttons for persona diagnostics: Neko probe, Dev probe, QA probe.
3. Add a persona operation history panel grouped by `operation_id`.
4. Add optional `--archive-on-complete` once Mission Control can link archive manifests directly.
5. Add live-token smoke recipes for each persona:
   - Neko: scope proof-only handoff.
   - Launcher Dev: explain/run one focused Launcher proof request.
   - Backend Dev: explain/run one focused backend check.
   - QA: review provided proof metadata and produce a bounded verdict.
