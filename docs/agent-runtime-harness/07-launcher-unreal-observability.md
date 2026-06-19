# Stage 7 — Launcher + Unreal Observability Contract

## Goal

Define the UI-facing state contract for Launcher pixel agents and Unreal Engine operations-floor characters without building those UIs yet.

The Python harness remains source of truth. Launcher and Unreal are **observers/controllers**, not independent state machines.

## Deep audit findings from current repo/environment

### Existing surfaces to leverage later

- `agent_runtime/events.py` writes JSONL events that are already safe for polling.
- `agent_runtime/paths.py::snapshot_path()` exists but is not populated yet.
- `hermes_state.py::SessionDB` stores session transcripts and can back "open logs/session" UI affordances.
- `hermes_logging.py` has session-context logging and redaction formatters.
- `gateway/` includes API/messaging patterns, but Stage 7 should start with file snapshots to avoid service complexity.
- Launcher Stage C MCP exists in Tony's environment and exposes redaction-safe widget/screenshot state. Observability should accommodate those proof IDs without depending on MCP imports.
- The TUI gateway JSON-RPC patterns show how Hermes can expose runtime state, but Stage 7 should not require TUI.

### Design constraint

Launcher/Unreal should not mutate task JSON directly. They should issue commands through CLI/API later, and the harness validates transitions/gates.

## Snapshot file contract

Initial file:

```text
<hermes-root>/agent-runtime/snapshot.json
```

Update source:

```python
agent_runtime/snapshot.py
```

Snapshot shape:

```json
{
  "schema_version": 1,
  "generated_at": "2026-05-20T22:15:01.412Z",
  "store_root": "redaction-safe path or hash",
  "summary": {
    "open_tasks": 2,
    "running_runs": 1,
    "blocked_tasks": 0,
    "open_incidents": 1,
    "missing_proof": 3
  },
  "tasks": [],
  "agents": [],
  "runs": [],
  "incidents": [],
  "proofs": []
}
```

Rules:

- Snapshot is derived data. If corrupt/missing, rebuild from stores/events.
- Snapshot must be redaction-safe by default.
- Paths should be relative under proof root when possible. Absolute paths are allowed only behind explicit local-only flag.
- No full prompts, raw stdout/stderr, secrets, image bytes, or file contents.

## Task summary contract

```json
{
  "task_id": "task_abc",
  "title": "Fix Launcher auth",
  "state": "qa_testing",
  "current_stage_id": "stage_2",
  "current_stage_title": "Verify login flow",
  "requires_visual_proof": true,
  "missing_proof": ["passed_test", "screenshot_or_video"],
  "open_incident_ids": [],
  "updated_at": "...",
  "status_text": "Waiting for QA visual proof"
}
```

## Agent/character state contract

```json
{
  "agent_id": "dev",
  "display_name": "Dev Agent",
  "role": "dev",
  "state": "planning",
  "task_id": "task_abc",
  "run_id": "run_123",
  "last_heartbeat_at": "...",
  "status_text": "Deep auditing stage 2",
  "animation_hint": "thinking | typing | testing | blocked | celebrating"
}
```

Mapping examples:

- `AgentState.IDLE` -> idle animation
- `READING_CONTEXT` / `AUDITING` -> reading/thinking
- `PLANNING` -> whiteboard/typing
- `TESTING` -> terminal/testing
- `CAPTURING_PROOF` -> camera/recording
- `WAITING_FOR_APPROVAL` -> waiting/hand-raised
- `BLOCKED` / `CRASHED` -> alert state
- `COMPLETE` -> celebration/return idle

## Run summary contract

```json
{
  "run_id": "run_123",
  "task_id": "task_abc",
  "persona_id": "dev",
  "state": "running",
  "started_at": "...",
  "last_heartbeat_at": "...",
  "session_id": "session_...",
  "iterations_used": 5,
  "iteration_budget": 90,
  "last_decision_type": "propose_stage_plan"
}
```

`session_id` lets UI offer "open transcript/log" without embedding message history in snapshot.

## Proof summary contract

```json
{
  "proof_id": "proof_1",
  "task_id": "task_abc",
  "stage_id": "stage_2",
  "type": "screenshot",
  "title": "Library details proof",
  "relative_path": "proofs/task_abc/screenshots/proof_1.png",
  "redaction_status": "safe",
  "created_by": "qa",
  "created_at": "..."
}
```

No binary data. UI can request/open the file locally if allowed.

## Event stream contract

Initial:

```text
<hermes-root>/agent-runtime/events.jsonl
```

Every event already follows Stage 1 shape:

```json
{"ts":"...","type":"task.transition","task_id":"task_abc","run_id":null,"persona_id":"pm","payload":{}}
```

Stage 7 consumers should treat events as append-only and snapshots as current-state materialization.

Future API:

```http
GET /api/agent-runtime/snapshot
GET /api/agent-runtime/events?since=<cursor>
GET /api/agent-runtime/tasks
GET /api/agent-runtime/tasks/{id}
POST /api/agent-runtime/tasks/{id}/approve
POST /api/agent-runtime/runs/{id}/cancel
```

Do not build the API until file snapshot consumers are proven.

## Commands from UI clients

Launcher/Unreal should eventually call commands, not mutate files:

- approve gate
- pause task
- resume task
- cancel run
- request summary
- open artifact
- spawn follow-up only with Tony/PM approval

Each command must flow through Stage 6 CLI or future API and re-run gates.

## Redaction / safety rules

- Snapshot payloads are safe summaries only.
- Artifact paths are relative where possible.
- `redaction_status` is surfaced for every artifact proof.
- UI must not auto-display unsafe screenshots/videos externally.
- Event payloads stay < 4 KB and contain no raw tool output.

## Implementation tasks

1. Add failing tests for snapshot containing task/agent/run/proof summaries.
2. Add failing tests that raw stdout/stderr or prompts are excluded.
3. Add failing tests for missing proof summary from proof gates.
4. Add `snapshot.py` builder from stores.
5. Add `snapshot_writer.py` atomic writer using `utils.atomic_json_write`.
6. Add optional `--json` status output reuse from Stage 6.
7. Add docs/examples for Launcher/Unreal consumers.

## Tests

Required test files:

```text
tests/agent_runtime/test_snapshot.py
tests/agent_runtime/test_observability_contract.py
```

Test matrix:

- Empty store snapshot is valid and stable.
- Snapshot lists open tasks and current stages.
- Snapshot lists agent character states derived from active runs.
- Snapshot includes missing proof keys but not raw logs.
- Snapshot includes proof summaries with redaction status.
- Snapshot rebuild is deterministic from same store state.
- Corrupt/missing snapshot can be regenerated.

## Acceptance criteria

- Harness can export a redaction-safe `snapshot.json`.
- Snapshot includes tasks, agents, current runs, missing proof, incidents, and proof summaries.
- Snapshot is stable enough for Launcher/Unreal to poll.
- UI clients do not need to parse task JSON internals directly.
- No Flutter/Unreal-specific dependency enters `agent_runtime` core.

## Risks / interventions

- **UI driving state directly:** forbid direct task JSON mutation; commands only.
- **Secret leakage:** no raw logs/prompts/file contents in snapshot.
- **Premature API/service:** file snapshot first; API later.
- **Unreal overbuild:** operations floor reads same snapshot/API later, not a separate runtime.
- **Schema drift:** include `schema_version` and snapshot contract tests.
