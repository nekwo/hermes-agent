# harness serve — design record and current shape

Status: **shipped 2026-07-08, and substantially outgrown that design since.**
Owner: fork (this repo, `hermes_cli/harness_parts/`). Launcher counterpart:
`Launcher_Brain/20 — Active Initiatives/mission-control-harness-serve.md`.

> **Where the frame-level truth lives.** The module docstring at the top of
> `hermes_cli/harness_parts/serve.py` documents every op, every frame shape,
> the hello handshake, drain semantics, and the method manifest — and it is
> maintained in the same commits that change them. **Read that first for
> "what does the wire look like today."** This file is the durable *why*: the
> decisions, the constraints they came from, and which of them survived.
>
> This split exists because the previous version of this file spent five weeks
> claiming serve was "settled, not yet implemented" while the Launcher note
> cited it as the canonical design — a reader following that pointer got a
> contradiction. Frame shapes rot in prose and don't rot next to the code that
> emits them, so they live there now.

## Problem (2026-07-08, unchanged and still the reason serve exists)

Every Launcher Mission Control action spawned a fresh `hermes` CLI process.
Measured on Tony's machine: bare python 0.07s, `hermes --help` 3.1s (import
graph), `hermes harness snapshot --json` ~10s (~3s imports + ~7s compute). That
import tax was paid on every send, capability call, and snapshot poll — it was
the whole "Mission Control feels slow" complaint.

## Shape

A fork-owned, long-lived warm process: `hermes harness serve --ndjson`.
Requests dispatch into the **existing argparse tree and `_cmd_*` handlers,
unchanged**.

argv-verbatim was the load-bearing choice: intent→argv mapping, capability
registry, streaming NDJSON parsing, and every handler stayed byte-identical, and
the per-call CLI fallback runs the same argv. That is still true of the argv
lane, and it is also the constraint that eventually forced the method lane —
see "What the 2026-07-08 non-goals got wrong" below.

### Per-request stdout (the one hard part)

Handlers `print()` directly and streaming turns emit deltas live. `sys.stdout`
is swapped once for a contextvar-dispatching proxy; each worker thread binds its
request id; one write lock keeps frames atomic. Same pattern upstream proved in
`tui_gateway`'s transport binding. No handler changes. This held up.

### Concurrency — single pool of 4

Cross-process concurrency already existed (the Launcher ran snapshot polls and
sends as parallel CLI processes against the file stores). A pool of 4 mirrors
that; handlers construct stores and reload config per call, so warm staleness
risk is low. A 240s chat turn must never block a poll — pool ≥2 guarantees it.
Still `DEFAULT_POOL_SIZE`, still 4, `--pool-size` overrides.

## Decisions settled with Tony (2026-07-08), scored

1. **Spawn timing:** eager at Mission Control open, `ready` handshake awaited.
   *Held.*
2. **Scope:** everything through serve v1 — polls, actions, chat. *Held.*
3. **Degraded mode:** silent per-call CLI fallback + a quiet bridge-status chip;
   supervisor respawns with backoff. *Held, and generalized —* the office RPC
   read lane reuses the same posture (RPC-first, snapshot backstop, never worse
   than before).
4. **Pool:** single `ThreadPoolExecutor(4)`. *Held.*
5. **Turn-aware supervisor (recording safety):** the Launcher supervisor NEVER
   kills serve while a chat turn is streaming; a hung non-chat request routes to
   the CLI fallback instead, and the process is recycled only when the chat lane
   is idle. Serve advertises in-flight work on ping. *Held, and grew* — the
   ping reply carries `{"event":"busy","chat_turns":N,"pending":M}`, and `drain`
   (below) became the graceful path that recycling actually uses.

## What the 2026-07-08 non-goals got wrong

The original design listed as explicit non-goals: *no network listener, no auth,
no cancel*. All three are now false, and the reasons are worth keeping because
each one was a real constraint that changed rather than a lapse.

- **A localhost socket lane exists.** "Local stdio child IS the security model"
  stopped being true the moment more than one client wanted the same warm
  runtime. The dispatcher is transport-agnostic — one op table, N transports —
  and the socket lane is injected and OFF unless `_cmd_serve` turns it on.
- **There is auth**, because a listener needs it: `agent_runtime/serve_auth.py`,
  an HMAC hello handshake bound to the dialled port, at
  `HELLO_CONTRACT_VERSION = 3`. Designed in rather than retrofitted after the
  socket existed.
- **Cancel exists.** The 2026-07-08 reasoning was "Launcher timeout → kill +
  respawn covers hangs" — which decision 5 then made unsafe for exactly the
  requests most likely to hang. `cancel` is the answer to that contradiction,
  and `cancel_denied` is its honest half: a chat turn or mutation that is still
  executing says so rather than pretending it stopped.

## Op surface

Ten ops today, not the two v1 named: `ping`, `hello`, `version`, `connections`,
`subscribe`, `unsubscribe`, `drain`, `stacks`, `shutdown`, `cancel`.
`SERVE_SCHEMA_VERSION` is still `1` — the frame envelope never broke
compatibility; ops were added, which is why an integer was the wrong versioning
tool for them and a *manifest* is the right one (below).

`drain` is the durable-service verb: refuse new work, let in-flight work land,
report `{"stuck_request_ids":[…],"held_by_chat_turns":N,"terminal":true}`. A
drain that cannot finish keeps serving and re-arms rather than dropping work.

## The method lane (2026-08-14, contract 1)

argv on the wire cannot be versioned — an argv string has no room to say what it
supports — so the first typed method carries its own manifest. JSON-RPC 2.0
framing mirroring upstream's `tui_gateway`, answered on stdio and socket alike,
beside the argv lane rather than replacing it.

`{"rpc":{"contract":1,"methods":["runtime.office.get"]}}` rides `ready` (stdio's
greeting), `hello_ok` (the socket's), and the re-askable `version` reply. It is
**a set plus an integer**: the integer moves when a method's shape changes
incompatibly, the set grows when a method is added — so methods are adoptable
one at a time. A runtime predating the lane has no `rpc` key, which reads as
"argv only", and a client must degrade rather than error on that.

Errors carry `data.reason`; clients branch on the reason, never on message
prose. See `agent_runtime/serve_rpc.py` and
`docs/mission_control/DECISION_push_and_rpc_2026-08-13.md` (launcher repo) for
the fork-boundary reasoning behind building the union of our PUSH and upstream's
CALL.

## Follow-up slices from the original plan

1. **Snapshot compute** (~7s). **SHIPPED 2026-07-08.** Measured first: warm
   `build_status` was 3.7s and `persona_chat_trace` alone 2.0s — status built
   with a plain `EventLog` and re-read the 80MB log per instance while
   `build_snapshot` already used `CachedEventLog`. Two fixes: `status.py` now
   defaults to `CachedEventLog` (3.7s → **1.76s**), and a serve-side
   `_ReadModelCache` keys the exact stdout payload of `harness status --json` /
   `snapshot --json` on a stat-based runtime-state fingerprint. Out-of-band
   signals are bounded by a 20s TTL; replayed responses stamp `served_from_cache`
   + `cache_age_ms`; failed builds are never cached. Live-verified: first poll
   2.70s, identical second poll **0.19s**, payloads byte-identical.
2. **Turn durability. SHIPPED end-to-end 2026-07-08:** turn store +
   interrupted-turn history rows; the operator conversation projects a typed
   `turn_interrupted` marker with turn identity preserved, and settles orphaned
   still-`running` tool_call rows to `interrupted` (`2a9d6639f`). The Launcher
   renders it as a retryable interruption tile (EterniaLauncher `e29442dd`).

## Future: phone / remote control (designed, NOT scheduled)

The dispatch core is `serve_loop(reader, writer)`. Stdio was transport #1; the
localhost socket became #2, which is most of what this needed. Later: phone
client → Keycloak auth → Django device registry ("which desktop am I logged in
with") → Centrifugo command routing → desktop Launcher feeds the relayed request
into the SAME serve loop, with a Django-side capability allowlist (dangerous
caps like `worker.interrupt` / `run.cancel` gated). Telegram gateway plugin
remains the chat-only cheap alternative.

## Test plan

- pytest drives `serve_loop` over pipes: request → frame parity with direct
  `_cmd_*` output; concurrent requests interleave cleanly; streaming handler
  emits live frames; malformed request → typed error frame; every op.
- The argv lane is byte-identity testable against the method lane running beside
  it — that discipline lives in `tests/agent_runtime/test_serve_socket_lane.py`
  and in serve.py's "ONE op table, N transports" docstring, not in a single
  named test.
- Launcher: bridge tests against a fake serve session; fallback path (serve dead
  → one-shot CLI, same argv); timeout → kill/respawn.
- Live verify: send latency + poll timing before/after, recorded in the launcher
  brain note.
