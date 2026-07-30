# Mission Control stream NDJSON contract

`hermes harness stream` emits newline-delimited JSON (NDJSON) frames for Mission Control consumers. The stream contract is implemented in `agent_runtime/stream.py` and is additive to the existing one-shot snapshot surface.

Each output line is one JSON frame. Consumers must parse each line independently, branch on the top-level `type`, and ignore unknown fields for forward compatibility.

## Versioning and compatibility

Hydrate, delta, and heartbeat frames currently carry:

- `schema_version`: `1`
- `generated_at`: the time the frame was emitted
- `watermark`: ordering metadata for the event stream or snapshot boundary

Patch frames carry `schema_version: 2`; they are the additive, op-based
delta-patch lane implemented alongside the schema-1 full-core frames.

Forward-compatibility expectations:

- Treat `schema_version: 1` as the full-core stream schema and
  `schema_version: 2` as the patch-frame schema.
- Ignore unknown top-level fields and unknown nested fields.
- Do not require frame-specific fields on a different `type`.
- Continue to support the one-shot `hermes harness snapshot --json` path as the canonical fallback when streaming is unavailable or a consumer needs to rehydrate from a known-good full snapshot.

## Frame ordering and watermarks

The stream starts with exactly one `hydrate` frame. `stream_frames()` reads the hydrate frame's `watermark.event_offset` and then emits full-core `delta` frames or schema-2 `patch` frames from the event log after that offset.

For `delta` frames:

- `watermark.event_offset` is the event-log offset used for that delta.
- `seq` is the same integer offset as `watermark.event_offset`.
- Consumers should apply deltas in ascending `watermark.event_offset` / `seq` order.
- `watermark.last_event_ts` is the event timestamp copied from the event.
- `watermark.captured_at` is the time the stream captured/emitted that watermark.

For `heartbeat` frames:

- `watermark.event_offset` repeats the latest offset known to the stream.
- Heartbeats do not advance the read model by themselves; they only prove the stream is alive at the current offset.
- If that offset is ahead of the consumer's last applied state-bearing frame,
  the heartbeat proves a frame was missed. The consumer must keep its applied
  watermark unchanged and rehydrate; advancing to the heartbeat offset would
  make the missing delta look stale when it later arrives.
- A heartbeat may carry an `activity` block while a snapshot build is in
  progress. The background Mission Daemon and its heartbeat status block were
  removed; no daemon state is carried now.

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

### `completeness` row shape

`completeness` is keyed by the current projection names
(`persona_chat_history`, `persona_chat_trace`, `operator_conversation`). Every
row is one `ProjectionAccountant.summary()`:

```json
{
  "persona_chat_history": {
    "considered": 163,
    "included": 50,
    "dropped": 113,
    "reasons": {"limit": 103, "session_not_in_db": 10},
    "truncated": true,
    "by_design": ["limit"]
  }
}
```

`by_design` (additive; the parity `envelope_version` is unchanged) lists the
reason codes this projection declared as **deliberate bounds** — caps, tail
windows, page limits, collapse markers. Their data is still reachable through
the lane's paging/detail fetch and a nonzero count is the steady state on a
healthy runtime. Every other reason discloses **lost or inconsistent data**
(a join that did not resolve, a row missing from its store, an unrenderable
entry) and is worth an operator's attention.

`dropped` still counts EVERY drop, so a reader computes the anomaly count
itself:

```
anomalous = dropped - sum(reasons[code] for code in by_design)
```

Do not reimplement the classification with a local reason allowlist: that copy
goes stale every time a new bounded lane ships (it already did twice —
`flow_item_cap`, then the persona-chat `limit`, each pinning Mission Control's
"projection drops" pill permanently amber). A reader that predates the key sees
no `by_design` and behaves exactly as before.

`identity_map` is a best-effort alias map. For persona instances, aliases may come from `persona_instance_id`, `instance_id`, `id`, `agent_profile_id`, and `persona_id` values that begin with `profile:`. For operator channels, aliases may come from `persona_instance_id`, `channel_id`, `id`, and `session_id`.

## `delta` frame

Delta frames carry redaction-safe event payloads from the event log after the hydrate watermark. They are intended to update the already-hydrated read model without forcing a full snapshot fetch.

When the debounce window coalesces a burst, the frame retains `entity` and
`op` for the last event and adds ordered `events` plus `coalesced_count` for
batch-aware consumers.

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
| Starts with `incident.` | the source event type |
| Starts with `persona_assignment.` | `instance.upserted` |
| Any other event type | `event.appended` |

**Corrected 2026-07-30 (`c12e6850d`):** the `task.*`, `proof.attached`, and
`daemon.*` classifier arms were removed after those event families were
de-registered. The five rows above are the complete current `_delta_op` table.

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
  "activity": {
    "kind": "snapshot_build",
    "state": "busy",
    "elapsed_ms": 1250
  }
}
```

Fields:

- `type`: the literal string `heartbeat`.
- `schema_version`: stream schema version `1`.
- `generated_at`: the time the heartbeat frame was emitted.
- `watermark.event_offset`: the latest event-log offset known to the stream.
- `watermark.captured_at`: the time the heartbeat watermark was captured.
- `activity`: optional fire-and-forget telemetry emitted while a snapshot build
  is busy. Its current keys are `kind`, `state`, and `elapsed_ms`; the field is
  absent on an ordinary idle heartbeat.

CLI timing flags:

- `--poll-interval`: maps to `poll_interval_seconds`; default `0.25`. The stream sleeps for this interval between event-log polls, with a lower bound of `0.01` seconds.
- `--heartbeat-interval`: maps to `heartbeat_interval_seconds`; default `5.0`. When no delta is emitted for at least this interval, the stream emits a `heartbeat` frame.
- `--delta-debounce-ms`: settle window for coalescing an event burst into one
  delta frame; default `200`, and `0` disables coalescing.
- `--resync`: forces the first post-hydrate batch to use a full-core delta so a
  reconnecting patch consumer can re-baseline before folding patches.

## Consumer guidance

- Start by applying the `hydrate` frame's `core` as the complete Mission Control snapshot.
- Then apply each `delta` in ascending `watermark.event_offset` / `seq` order.
- Treat `heartbeat` frames as liveness and offset markers only.
- Count only decoded protocol frames (`hydrate`, `delta`, `patch`, or
  `heartbeat`) as stream liveness. Arbitrary stdout is not a heartbeat.
- If a consumer detects a gap, cannot parse the stream, or needs a canonical re-sync, call `hermes harness snapshot --json`. The one-shot snapshot remains the canonical fallback for consumers.

Persona chat commands participate in this event-driven contract. After the
native transcript and Mission Control turn projection commit,
`mission.chat.message` emits `persona_chat.projected`; a first-turn auto-title
change emits `persona_chat.metadata_updated`. The exactly-once turn journal
records `projection_event_emitted`, so normal idempotent replays do not append a
second notification, while an older or interrupted projected turn repairs a
missing notification on replay. `agent_chat_send` invokes the same command
handler, so agent-to-agent and direct CLI messages share this path.
