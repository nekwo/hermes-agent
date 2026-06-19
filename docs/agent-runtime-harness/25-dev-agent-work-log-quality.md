# Stage 25 — Dev Agent Work Log Quality

## Goal

Make Dev Agent Detail logs explain the engineering workflow instead of repeating raw telemetry labels. Do this by enriching existing Harness event payloads and improving Launcher-side grouping/labels, without adding new daemons, stores, brokers, or runtime services.

## Product stance

This stage is observability-only. It must reduce operator confusion before adding more automation or scheduler behavior. Raw event cards and full terminal logs remain available, but the default Dev detail view should answer: what Dev did, what proof/tool result happened, what warning matters, and what should happen next.

## Current audit evidence

- Dev log rows show tautological summaries like `run.progress · run_id · task_id`.
- Tool activity rows do not expose phase, severity, exit/duration/proof context, or a human useful summary.
- `request_test_run` command proof can succeed while the Dev task remains `dev_implementing`; the UI does not explain that expected next step is QA handoff.
- Harness progress/event payload allowlists drop fields needed by Launcher to group Dev work safely.

## Constraints

- No new runtime points of failure: no new service, background analyzer, DB, log forwarder, broker, or screenshot dependency.
- Keep payloads redaction-safe: no raw stdout, raw command strings, absolute paths, tokens, auth values, or unbounded model output.
- Preserve raw log access as debugging layer.

## Stage A — Harness event contract enrichment

Affected files:
- `agent_runtime/progress.py`
- `agent_runtime/observability.py`
- `agent_runtime/proof_runner.py`
- `agent_runtime/store.py`
- tests under `tests/agent_runtime/`

Implementation:
- Expand safe event/progress payload keys: `phase`, `severity`, `step`, `intent`, `detail`, `exit_code`, `duration_ms`, `iteration`, `max_iterations`, `compact_count`, `total_tokens`, `proof_id`, `proof_count`, `decision_type`, `validation_status`, `error_class`, `next_expected`.
- Emit source summaries for core events:
  - `run.opened`: `Dev run opened for stage ...` with `phase=run_opened`.
  - `run.closed`: `Dev decision parsed: request_test_run` or `Dev run cancelled/failed` with severity.
  - command proof completion: `Command proof passed: exit 0, 467ms, proof <id>` with `phase=proof`, `step=command_proof`, `proof_id`, `exit_code`, `duration_ms`.
- Preserve current redaction filters for string values.

Acceptance:
- Observability event summaries include phase/severity/step/proof fields for command proof events.
- Progress payload rejects path-like and secret-like values for all new string fields.
- Existing tests remain green.

## Stage B — Launcher Dev log model and grouping

Affected files:
- `lib/features/mission_control/data/mission_control_snapshot.dart`
- `lib/features/mission_control/data/mission_control_bridge.dart`
- `lib/features/mission_control/mission_control_page.dart`
- Mission Control widget/bridge tests.

Implementation:
- Extend `MissionAgentLogEvent` with optional `phase`, `severity`, `step`, `status`, `toolName`, `exitCode`, `durationMs`, `proofId`, `decisionType`, `nextExpected`.
- Map Harness event payloads through the CLI bridge.
- Replace generic task-flow labels with Dev work labels when fields exist:
  - `proof/command_proof` → `Command proof passed/failed`.
  - `tool_execution` → `Tool activity`.
  - `decision_parse` → `Dev decision`.
  - `warning` severity → visually/verbally warning row.
- Add a `Dev Work Log` section for Dev persona that groups important work rows above raw events.
- Keep `Live Agent Log` and `Full Terminal Log` raw sections.

Acceptance:
- Widget tests show Dev Agent Detail includes `Dev Work Log`, `Command proof passed`, proof ID, and warning text when provided by events.
- Unsafe snapshot suppression still hides all agent detail content.
- Raw terminal log still includes all event rows.

## Stage C — Verification and proof

Commands:
- Harness: `PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -m pytest -q -o addopts='' tests/agent_runtime`
- Harness compile/diff: `python -m compileall ...`, `git diff --check`
- Launcher targeted tests: Mission Control data/widget tests.
- Launcher analyze.
- Windows debug build / Stage C screenshot when practical; if unavailable, explicitly report `live Stage C screenshot QA not performed` and why.

## Follow-up gaps not in this stage

- Hard token/compaction kill switches.
- Proof handoff automation after `request_test_run`.
- Daemon fairness / priority scheduling.
