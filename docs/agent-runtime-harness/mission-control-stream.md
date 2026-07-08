# Mission Control stream NDJSON contract

`hermes harness stream` emits newline-delimited JSON (NDJSON) frames for Mission Control consumers. The stream contract is implemented in `agent_runtime/stream.py` and is additive to the existing one-shot snapshot surface.

Each output line is one JSON frame. Consumers must parse each line independently, branch on the top-level `type`, and ignore unknown fields for forward compatibility.

## Versioning and compatibility

All stream frames currently carry:

- `schema_version`: `1`
- `generated_at`: the time the frame was emitted
- `watermark`: ordering metadata for the event stream or snapshot boundary

Forward-compatibility expectations:

- Treat `schema_version: 1` as the current stream schema.
- Ignore unknown top-level fields and unknown nested fields.
- Do not require frame-specific fields on a different `type`.
- Continue to support the one-shot `hermes harness snapshot --json` path as the canonical fallback when streaming is unavailable or a consumer needs to rehydrate from a known-good full snapshot.

## Frame ordering and watermarks

The stream starts with exactly one `hydrate` frame. `stream_frames()` reads the hydrate frame's `watermark.event_offset` and then emits `delta` frames from the event log after that offset.

For `delta` frames:

- `watermark.event_offset` is the event-log offset used for that delta.
- `seq` is the same integer offset as `watermark.event_offset`.
- Consumers should apply deltas in ascending `watermark.event_offset` / `seq` order.
- `watermark.last_event_ts` is the event timestamp copied from the event.
- `watermark.captured_at` is the time the stream captured/emitted that watermark.

For `heartbeat` frames:

- `watermark.event_offset` repeats the latest offset known to the stream.
- Heartbeats do not advance the read model by themselves; they only prove the stream is alive at the current offset.
- Heartbeats additionally carry the current `daemon` status block (see the `heartbeat` frame section) so runtime HUDs stay live while an idle daemon emits no deltas. Consumers may merge it into their read model's daemon view; it never changes any other entity.

## `hydrate` frame

The hydrate frame is the initial full read-model snapshot. Mission Control Launcher consumers hydrate `MissionReadModel` from this frame by reading `core` exactly like the output of a fresh `hermes harness snapshot --json` response.

Top-level shape:

```json
{
  "type": "hydrate",
  "schema_version": 1,
  "generated_at": "...",
  "watermark": {},
  "identity_map": {},
  "core": {},
  "completeness": {},
  "drops": [],
  "parity_warnings": []
}
```

Fields:

- `type`: the literal string `hydrate`.
- `schema_version`: stream schema version `1`.
- `generated_at`: `core.generated_at` when present, otherwise the current time.
- `watermark`: copied from `core.parity.watermark` when present; otherwise `{}`.
- `identity_map`: aliases to canonical persona/channel identifiers derived from `core.persona_instances` and `core.operator_channels`.
- `core`: the full snapshot payload returned by the snapshot builder. This is the complete read-model base state.
- `completeness`: copied from `core.parity.completeness` when present; otherwise `{}`.
- `drops`: copied from `core.parity.drops` when present; otherwise `[]`.
- `parity_warnings`: copied from `core.parity.warnings` when present; otherwise `[]`.

`identity_map` is a best-effort alias map. For persona instances, aliases may come from `persona_instance_id`, `instance_id`, `id`, `agent_profile_id`, and `persona_id` values that begin with `profile:`. For operator channels, aliases may come from `persona_instance_id`, `channel_id`, `id`, and `session_id`.

## `delta` frame

Delta frames carry redaction-safe event payloads from the event log after the hydrate watermark. They are intended to update the already-hydrated read model without forcing a full snapshot fetch.

Top-level shape:

```json
{
  "type": "delta",
  "schema_version": 1,
  "generated_at": "...",
  "watermark": {
    "event_offset": 0,
    "last_event_ts": "...",
    "captured_at": "..."
  },
  "seq": 0,
  "op": "event.appended",
  "entity": {
    "event": {},
    "task_id": null,
    "goal_id": null,
    "run_id": null,
    "persona_id": null,
    "session_id": null,
    "correlation_id": null
  }
}
```

Fields:

- `type`: the literal string `delta`.
- `schema_version`: stream schema version `1`.
- `generated_at`: the time the delta frame was emitted.
- `watermark.event_offset`: the event-log offset for the emitted event.
- `watermark.last_event_ts`: the source event's `ts`.
- `watermark.captured_at`: the time the watermark was captured.
- `seq`: the same integer as `watermark.event_offset`.
- `op`: the operation classification derived from the source event type.
- `entity.event`: the source event converted to JSON, with `payload` replaced by a redaction-safe JSON payload.
- `entity.task_id`: the source event's `task_id`.
- `entity.goal_id`: currently populated from the source event's `task_id`.
- `entity.run_id`: the source event's `run_id`.
- `entity.persona_id`: the source event's `persona_id`.
- `entity.session_id`: the source event's `session_id`.
- `entity.correlation_id`: `entity.event.payload.correlation_id` when the redaction-safe payload is an object and contains that key; otherwise `null`.

Redaction behavior:

- `entity.event.payload` is recursively converted to JSON-safe values.
- Lists and tuples are truncated to their first 200 items.
- String assignments whose key looks like a secret, token, password, credential, API key, or key are rewritten as `NAME=[redacted]`.

Current `op` mappings:

| Source event type | `op` |
| --- | --- |
| Starts with `run.tool.` | `chat.trace.appended` |
| Equals `run.progress` | `chat.trace.appended` |
| Equals `task.transition`, `task.blocked`, `task.unblocked`, `task.cancelled`, or `task.archived` | `task.state_changed` |
| Starts with `task.` but is not one of the state-change events above | `task.upserted` |
| Equals `proof.attached` | `proof.added` |
| Starts with `incident.` | the source event type |
| Starts with `daemon.` | `daemon.status` |
| Starts with `persona_assignment.` | `instance.upserted` |
| Any other event type | `event.appended` |

## `heartbeat` frame

Heartbeat frames are emitted when no delta has been emitted for the configured heartbeat interval. They keep long-running stream clients aware that the process is still alive and where the stream is currently parked.

Top-level shape:

```json
{
  "type": "heartbeat",
  "schema_version": 1,
  "generated_at": "...",
  "watermark": {
    "event_offset": 0,
    "captured_at": "..."
  },
  "daemon": {
    "schema_version": 1,
    "state": "idle",
    "pid": 12345,
    "heartbeat_at": "...",
    "target_task_id": null,
    "settle_stop_reason": "no_eligible_action",
    "loops": 47,
    "next_wake_at": "...",
    "wait_seconds": 30
  }
}
```

Fields:

- `type`: the literal string `heartbeat`.
- `schema_version`: stream schema version `1`.
- `generated_at`: the time the heartbeat frame was emitted.
- `watermark.event_offset`: the latest event-log offset known to the stream.
- `watermark.captured_at`: the time the heartbeat watermark was captured.
- `daemon`: the versioned daemon status block (`daemon_status_schema()` — the same shape `harness daemon status --json` returns). The daemon writes its per-loop status to `daemon_status.json`, not the EventLog, so an idle daemon produces no deltas; this block is the only live channel for daemon liveness between deltas. It is read-model telemetry: fire-and-forget, never acknowledged, and a missing/dropped block only means "no update this frame" (the field is omitted if the status file cannot be read). Optional keys beyond the core set (e.g. `next_wake_at`, `wait_seconds`, `last_tick_id`, `liveness`) appear when the daemon has recorded them.

CLI timing flags:

- `--poll-interval`: maps to `poll_interval_seconds`; default `0.25`. The stream sleeps for this interval between event-log polls, with a lower bound of `0.01` seconds.
- `--heartbeat-interval`: maps to `heartbeat_interval_seconds`; default `5.0`. When no delta is emitted for at least this interval, the stream emits a `heartbeat` frame.

`--max-frames` exists as a suppressed CLI/testing control and is not part of the normal consumer contract.

## Consumer guidance

- Start by applying the `hydrate` frame's `core` as the complete Mission Control snapshot.
- Then apply each `delta` in ascending `watermark.event_offset` / `seq` order.
- Treat `heartbeat` frames as liveness and offset markers only.
- If a consumer detects a gap, cannot parse the stream, or needs a canonical re-sync, call `hermes harness snapshot --json`. The one-shot snapshot remains the canonical fallback for consumers.
