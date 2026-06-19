# Stage 70 — Mission Control Cockpit Contract

Status: contract freeze draft, created from deep audit on 2026-06-17.

## Purpose

This document freezes the Hermes → Launcher contract for the Mission Control cockpit upgrade. Hermes is the runtime authority. Launcher is the cockpit client.

## Non-negotiables

1. Launcher never mutates Harness stores directly.
2. Launcher writes through whitelisted Harness capabilities or legacy audited CLI adapters during migration.
3. Every write response is a receipt; Launcher refreshes authoritative readback after the write.
4. Queue and run are separate operations.
5. No arbitrary shell, executable, or caller-provided subcommand is exposed through capabilities.
6. Worker/agent UI identity is keyed by worker/persona identity, not only role.
7. Private chain-of-thought is never exposed.

## Canonical first-pass capability IDs

Use current `agent_runtime/capabilities.py` IDs for first integration:

- `task.create`
- `persona.message_task`
- `persona.instance.message`
- `persona.instance.open_chat`
- `task.tick`
- `task.run_until_settled`
- `persona.instance.run_once`
- `persona.diagnose`
- `worker.nudge`
- `worker.pause`
- `worker.resume`
- `worker.interrupt`
- `worker.possess`
- `worker.release`
- `run.cancel`
- `run.approve`
- `task.unblock`
- `lane.pause`
- `lane.park`
- `lane.resume`
- `lane.drain`
- `daemon.start`
- `daemon.stop`
- `daemon.run_once`
- `persona.instance.close`
- `persona.instance.archive`
- `task.archive`
- `task.archive_ready`

Add read capabilities if not present:

- `snapshot.read`
- `proof.list`

## Descriptor schema v1

Each descriptor should expose:

```json
{
  "schema_version": 1,
  "capability_id": "persona.message_task",
  "target_kind": "task",
  "target_id": "task_123_or_null_for_global_catalog",
  "label": "Queue Message",
  "group": "queue",
  "description": "Queue a task-bound operator message without ticking.",
  "enabled": true,
  "disabled_reason": null,
  "danger_level": "normal",
  "execution_semantics": "queues_only",
  "readback": ["snapshot", "persona.assignments"],
  "args_schema": {
    "type": "object",
    "additionalProperties": false
  }
}
```

## Generic capability call envelope

```json
{
  "capability_id": "persona.message_task",
  "target_kind": "task",
  "target_id": "task_123",
  "args": {
    "persona_id": "neko_supervisor",
    "message": "Please inspect this mission.",
    "title": "Operator message"
  },
  "idempotency_key": "launcher-...",
  "requested_by": "launcher"
}
```

Receipt:

```json
{
  "status": "accepted",
  "safe_message": "Queued message for Neko Supervisor.",
  "capability_id": "persona.message_task",
  "target_kind": "task",
  "target_id": "task_123",
  "assignment_id": "assign_123",
  "run_id": null,
  "worker_session_id": null,
  "next_expected": "Run harness tick to process the queued assignment.",
  "readback_required": ["snapshot", "persona.assignments"]
}
```

## Cockpit worker summary

Hermes should expose `worker_summaries` alongside legacy `worker_sessions`:

```json
{
  "worker_session_id": "worker_123",
  "persona_id": "dev",
  "display_name": "Launcher Dev Agent",
  "role": "dev",
  "repo_scope_label": "Launcher",
  "compute_state": "computing",
  "activity_label": "Running focused widget test",
  "assignment_label": "Mission Control cockpit refactor",
  "last_heartbeat_at": "2026-06-17T08:44:22Z",
  "active_run_id": "run_123",
  "token_budget_used": 12345,
  "proof_state": "pending",
  "attention_level": "normal"
}
```

Allowed `compute_state`:

- `idle`
- `queued`
- `computing`
- `verifying`
- `blocked`
- `needs_tony`
- `ready`
- `offline`
- `unknown`

## Runtime cockpit health

Hermes should expose one compact object for Command Deck indicators:

```json
{
  "daemon_state": "offline",
  "tick_mode": "manual",
  "last_tick_at": null,
  "last_event_at": "2026-06-17T08:44:22Z",
  "active_run_count": 0,
  "queued_assignment_count": 1,
  "open_incident_count": 0,
  "proof_blocker_count": 0,
  "dirty_state_indicator": {
    "dirty": false,
    "summary": "clean"
  },
  "operator_label": "Manual Tick Mode"
}
```

## Packet rows for Launcher terminal

Hermes should expose compact, redaction-safe packet rows:

```json
{
  "packet_id": "packet_123",
  "packet_type": "delivery",
  "actor": "dev",
  "target_owner": "qa",
  "summary": "Patched Mission Control Agent Console.",
  "validation_status": "valid",
  "normalization_status": "unchanged",
  "redaction_status": "safe",
  "raw_artifact_id": "raw_packet_123",
  "created_at": "2026-06-17T08:44:22Z"
}
```

Required packet types:

- `mission_scope`
- `handoff`
- `context_request`
- `context_result`
- `delivery`
- `proof_request`
- `proof_result`
- `qa_review`
- `blocker`
- `missing_input`
- `repair_feedback`
- `state_transition`

Legacy alias:

- `handoff_packet` maps to `handoff` during migration.

## Proof row/readback

Proof summaries should expose:

- `recipe_id`
- `recipe_version`
- `recipe_hash`
- `status`
- `artifact_refs`
- `safe_summary`
- `redaction_status`
- `next_action` when failed/missing/blocked

Allowed proof state:

- `missing`
- `pending`
- `passed`
- `failed`
- `unsafe`
- `not_required`
- `unknown`


## Harness attachment context decision

Mission Control agent chat should support attachments when they are useful Harness context or proof input. This includes screenshots/images and, where practical, short videos or files. Attachments are **not** arbitrary filesystem access; they are structured, redaction-aware context records passed through a whitelisted capability argument.

Canonical attachment shape:

```json
{
  "kind": "image",
  "mime": "image/png",
  "uri": "artifact://mission-control/screenshot.png",
  "safe_label": "Mission Control screenshot"
}
```

Allowed `kind` values for the first pass:

- `image`
- `video`
- `file`

Capability support:

- `persona.message_task` may accept `attachments`.
- `persona.instance.message` may accept `attachments`.
- `persona.instance.run_once` may accept `attachments`.
- `persona.diagnose` may accept `attachments`.
- Worker/run/daemon/archive controls must not accept `attachments`.

Rules:

1. Attachments must be validated as a list of objects with `kind`, `mime`, and `uri`.
2. `safe_label` is optional but must be a string when present.
3. The UI may reuse the DM composer attachment ergonomics only when the Harness send path is actually wired; otherwise show disabled attach affordances with a truthful reason.
4. The backend/Hermes side must preserve attachments as context/proof handles and keep raw bytes/paths out of public summaries.
5. Stage C screenshots/videos should enter Harness as attachment context/proof handles, not copied into free-text prompts.

## Redaction rules

- No tokens, auth headers, callback codes, raw secrets, or private profile paths.
- Raw artifact handles must be IDs or relative/local-safe paths, never public URLs unless intentionally safe.
- Private chain-of-thought is never exposed. Use `reasoning_summary`, `rationale`, `decision_summary`, or truthful unavailable copy.
- UI-facing display names must be suppressed if snapshot has unsafe fields.
