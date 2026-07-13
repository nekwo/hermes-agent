# harness serve — settled design (2026-07-08)

Status: **settled with Tony, not yet implemented.** Owner: fork (this repo,
`hermes_cli/harness_parts/`). Launcher counterpart:
`Launcher_Brain/20 — Active Initiatives/mission-control-harness-serve.md`.

## Problem

Every Launcher Mission Control action spawns a fresh `hermes` CLI process.
Measured 2026-07-08 (Tony's machine): bare python 0.07s, `hermes --help` 3.1s
(import graph), `hermes harness snapshot --json` ~10s (~3s imports + ~7s
compute). That import tax is paid on every send, capability call, and
snapshot poll — it is the whole "Mission Control feels slow" complaint.

## Shape

A fork-owned, long-lived **stdio child** of the Launcher:
`hermes harness serve --ndjson`. One warm process; requests dispatch into the
**existing argparse tree and `_cmd_*` handlers, unchanged**.

Explicit non-goals: no network listener, no auth (local stdio child IS the
security model), not the mission daemon, no second chat pipeline, no
capability re-mapping.

## Protocol (schema v1)

- Boot handshake: `{"event":"ready","pid":…,"schema_version":1,"runtime_root":…}`
- Request (argv **verbatim** as the Launcher bridge already builds it):
  `{"id":"req-7","argv":["harness","mission-chat","message","--persona","dev",…]}`
- Frames: every stdout line the handler prints, forwarded live:
  `{"id":"req-7","event":"line","line":"…"}` … `{"id":"req-7","event":"exit","code":0}`
- Control ops: `{"op":"ping"}`, `{"op":"shutdown"}`. No cancel in v1
  (Launcher timeout → kill + respawn covers hangs).
- stderr forwarded untagged (`"id":null`) for logging.

argv-verbatim is the load-bearing choice: intent→argv mapping, capability
registry, streaming NDJSON parsing, and every handler stay byte-identical;
the per-call CLI fallback runs the same argv.

## Per-request stdout (the one hard part)

Handlers `print()` directly and streaming turns emit deltas live. Swap
`sys.stdout` once for a contextvar-dispatching proxy; each worker thread
binds its request id; one write lock keeps frames atomic. Same pattern
upstream proved in `tui_gateway`'s transport binding. No handler changes.

## Concurrency — single pool of 4 (decided)

Cross-process concurrency already exists today (Launcher runs snapshot polls
and sends as parallel CLI processes against the file stores). A pool of 4
mirrors that; handlers construct stores and reload config per call, so warm
staleness risk is low. A 240s chat turn must never block a poll — pool ≥2
guarantees it. Tests must cover concurrent interleaving (frame atomicity,
no cross-request stdout bleed).

## Decisions settled with Tony (2026-07-08)

1. **Spawn timing:** eager at Mission Control open, `ready` handshake awaited.
2. **Scope:** everything through serve v1 — polls, actions, chat.
3. **Degraded mode:** silent per-call CLI fallback + quiet bridge-status chip
   ("degraded (CLI fallback)"); supervisor respawns with backoff.
4. **Pool:** single ThreadPoolExecutor(4).
5. **Turn-aware supervisor (recording safety):** today's one-process-per-command
   model isolates chat turns from kills; serve concentrates everything in one
   process. The Launcher supervisor therefore NEVER kills serve while a chat
   turn is streaming — a hung non-chat request routes to the CLI fallback
   instead, and the process is recycled only when the chat lane is idle.
   Serve advertises in-flight chat turns via a `{"event":"busy","chat_turns":N}`
   frame on ping so the supervisor can decide honestly.

## Future: phone / remote control (designed, NOT scheduled)

Dispatch core is `serve_loop(reader, writer)` — stdio is transport #1. Later:
phone client → Keycloak auth → Django device registry ("which desktop am I
logged in with") → Centrifugo command routing → desktop Launcher feeds the
relayed request into the SAME serve loop, with a Django-side capability
allowlist (dangerous caps like `worker.interrupt` / `run.cancel` gated).
Telegram gateway plugin remains the chat-only cheap alternative. Cross-repo
feature → durable note belongs in the parent ArcadiaLabs brain when scheduled.

## Follow-up slices (separate, measured)

1. **Snapshot compute** stays ~7s. Serve from the persisted read-model /
   in-serve cache with sequence check. Do after serve ships; measure first.
   **SHIPPED 2026-07-08.** Measured first: warm `build_status` was 3.7s and
   `persona_chat_trace` alone was 2.0s — status built with a plain
   `EventLog` and re-read the 80MB log per instance while `build_snapshot`
   already used `CachedEventLog`. Two fixes:
   - `agent_runtime/status.py` now defaults to `CachedEventLog` (same as
     snapshot): warm `build_status` 3.7s → **1.76s**.
   - Serve-side `_ReadModelCache` (`hermes_cli/harness_parts/serve.py`):
     the exact stdout payload of `harness status --json` /
     `harness snapshot --json` is cached keyed on a stat-based
     runtime-state fingerprint (events.jsonl, turn store, daemon status,
     scope pointers, store dirs, SessionDB incl. -wal/-journal) — the
     sequence check. Out-of-band signals (git dirty state, provider
     health) are bounded by a 20s TTL. Replayed responses stamp
     `served_from_cache` + `cache_age_ms` on the exit frame; the payload's
     parity `generated_at` stays the honest build time. Failed builds are
     never cached. Live-verified: first poll 2.70s (build), identical
     second poll **0.19s** served_from_cache, payloads byte-identical,
     clean shutdown. Tests: 5 cache cases in
     `tests/agent_runtime/test_harness_serve.py` (replay, fingerprint
     move, TTL, failed-build, non-poll argv).
2. **Turn durability** (transport-independent; what actually *ensures*
   provider-reply recording). Today: operator message is write-ahead
   persisted, streamed turns persist incrementally, final reply appends with
   client_message_id replay dedup. Gaps: (a) non-streamed turns persist
   nothing between operator message and completion — a mid-turn kill loses
   the reply; run the incremental turn recorder unconditionally, not only
   under --stream. (b) no terminal turn marker — an interrupted turn
   vanishes silently; persist turn status (completed/failed/interrupted) so
   the Launcher can render "turn interrupted — retry" honestly.
   **SHIPPED end-to-end 2026-07-08:** turn store + interrupted-turn history
   rows landed with the turn-durability chain; the operator conversation now
   projects a typed `turn_interrupted` marker (turn identity preserved) and
   settles orphaned still-`running` tool_call rows to `interrupted`
   (`2a9d6639f`); the Launcher renders it as a retryable interruption tile
   (EterniaLauncher `e29442dd`).

## Test plan

- pytest drives `serve_loop` over pipes: request → frame parity with direct
  `_cmd_*` output; concurrent requests interleave cleanly; streaming handler
  emits live frames; malformed request → typed error frame; shutdown op.
- Launcher: bridge tests against a fake serve session; fallback path
  (serve dead → one-shot CLI, same argv); timeout → kill/respawn.
- Live verify: send latency + poll timing before/after, recorded in the
  launcher brain note.
