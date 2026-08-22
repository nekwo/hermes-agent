# Stage 14 — Snapshot core build performance

**Trigger (2026-07-09 evening):** with Stage 13 making scope switches honest,
the remaining operator pain is latency — the live serve child reported
`build_ms 7594` and the launcher spinner held ~8–12s per switch until the
Stage 13 fast-confirm hid the wait. The core build is the single biggest cost
in the read path and is re-paid on EVERY event delta (serve rebuilds a full
core per delta — the Stage 12 deferral).

## Measured profile (2026-07-09, live store, cProfile)

One `build_snapshot()` ≈ 3.7–4.5s wall (7.6s in the loaded serve child):

| Cost | Measurement |
| --- | --- |
| JSON store reads | 7,302 `raw_decode` calls; 13,748 `nt.stat`; 5,652 `io.open` |
| YAML parses | 963 `yaml.load` (~1.1s): installed-skill catalog walked 15× (once per persona chat session), profile templates re-parsed every build (~0.7s) |
| Event-log scans | 22 `events._scan` calls per build (~0.9s); events.jsonl is 81 MB / ~129k events |
| serde | `to_jsonable`/`_coerce` ~1.3s over ~193k objects |

## Slice 1 — catalog TTL memos (SHIPPED this stage)

`_installed_skill_catalog()` (prompt_observability) and
`_profile_templates_cached()` (snapshot) memoize the two catalog walks for
15s, keyed on BOTH the TTL and the fetcher's identity (a monkeypatch or hot
reload invalidates instantly; conftest autouse reset adds cross-test
hygiene). Observability rows only — never authority.

**Result: reported `build_ms` 3700 → 2943 warm (~20%).**

## Slice 2 — coalesced concurrent builds (SHIPPED this stage)

Concurrent default-store builds are strictly additive under the GIL —
measured live: one warm build 3.3s, **three concurrent builds 8.8s EACH**
(this, not the build itself, was the launcher's 9050ms chip: boot fires
hydrate + status polls together). `build_snapshot` now serializes and
coalesces the default path: arrivals during a build wait and share the NEXT
build (never the in-flight one — its state may predate the caller's
arrival), so a boot storm costs at most two sequential fast builds.
Injected stores (tests, doctors) bypass coalescing.

Sharing copies via `copy.deepcopy`, NOT a JSON round-trip — the core
carries datetime objects and `json.dumps` raised `TypeError`, which a
defensive except silently converted into "no sharing at all" on the live
store while the JSON-safe unit fakes stayed green. The regression test now
puts a datetime in the fake core.

**Result: boot-storm first response 8.2s → 3.6s; per-request
[8.24, 8.27, 8.77] → [3.62, 7.78, 7.93]; individual builds no longer
degrade under concurrency.**

## Remaining plan (in value order, not scheduled)

> **Status correction (2026-08-14).** Items 3 and 4 below have since SHIPPED:
> event-log rotation with a sidecar `base_offset` is
> `agent_runtime/event_rotation.py` (the manifest carries
> `{"live": {"file": …, "base_offset": …}}`), and delta diffing on the wire is
> the S7-A patch lane — `agent_runtime/state_patches.py` +
> `schema_version: 2` `patch` frames in `agent_runtime/stream.py`, documented in
> [mission-control-stream.md](mission-control-stream.md). Item 1 shipped in
> part as `agent_runtime/parse_cache.py` (the `(path, mtime_ns, size)` key it
> asks for, applied to the YAML/frontmatter leaf loads rather than the JSON
> store models). Item 2 is still open.

1. **Per-domain store read caches in the serve child** — most of the 7,302
   JSON reads are files that did not change between deltas. Cache parsed
   models keyed on `(path, mtime_ns, size)`; invalidate per event type where
   cheap, else stat-check. Biggest single win; serve-resident so the CLI
   path stays cold-correct.
2. **One event-log scan per build** — build_snapshot consumers trigger 22
   scans; thread a single scan result (or an offset-indexed tail reader)
   through the build.
3. **Event-log compaction with offset preservation** — the watermark IS the
   byte offset of events.jsonl, and the launcher gate is `> current`, so
   naive truncation would freeze every consumer. Compaction must carry a
   sidecar `base_offset` so logical offsets stay monotonic across archive
   rotations. Prerequisite for keeping scans O(recent) forever.
4. **Delta diffing on the wire** (deferred from Stage 12) — stop shipping a
   full core per event once frame sizes/rates are measured.

Target: warm serve-resident core < 500ms; scope-switch confirm < 1s without
the Stage 13 fast-confirm having to hide anything.

## First-message stream liveness hardening (2026-07-22)

### Incident and fact check

The first message in an operator chat persisted and the model reply completed,
but Mission Control declared the runtime offline and the console appeared stuck
on stale state. This was not a Riverpod refresh failure or a missing reply.

Production evidence separated three coupled faults:

1. One first-turn event burst included repeated
   `persona_instance.chat_opened` events plus uncovered projection events, so
   the stream correctly selected an authoritative full-core delta instead of a
   patch.
2. The full-core builder took 36.6 seconds. Prompt observability resolved each
   skill inside the persona-by-skill loop: 502 exhaustive `resolve_skill`
   operations, 20,098 recursive directory walks, and 49,759 frontmatter parses.
   The resolver already exposed `resolve_skills()` for a single collision-safe
   batch, but the snapshot path did not use it.
3. Launcher steady-state liveness expires after 30 seconds. During synchronous
   core construction the generator emitted no heartbeat, so a healthy but busy
   producer looked dead. Cancelling the Launcher subscription did not cancel the
   already-running infinite `harness stream` request; reconnects could therefore
   strand workers in the four-thread serve pool.

### Enterprise invariants

- The EventLog offset of the last *applied* core is the only advertised
  watermark during a rebuild. Liveness must never claim an unbuilt future core.
- One effective skill-root set is walked once per snapshot build. Persona rows
  reuse that build-scoped resolver and content-receipt cache.
- Reopening an already-authoritative chat binding is a read/no-op: no file
  rewrite, fingerprint churn, or duplicate event.
- A finite expensive build cannot block liveness. The stream emits additive
  `activity={kind: snapshot_build, state: busy, elapsed_ms: ...}` heartbeats
  while construction runs away from the generator thread.
- Cancellation is cooperative only for the read-only infinite `harness stream`
  command. Chat turns and mutations keep the existing finish-and-record rule.
- Every accepted stream cancellation releases its serve-pool worker. A new
  status request must complete with a pool size of one in the regression test.
- The consumer treats build activity as live authoritative synchronization,
  never as an outage; true silence still trips the watchdog.

### Implementation and acceptance gates

The prompt-observability snapshot now owns a build-scoped batch resolver keyed
by the effective roots. Package hashes are memoized for the same build. Exact
`open_chat` retries return before store update/event emission. Full-core stream
batches build on a finite daemon worker, with cancellation polling and
last-applied-offset heartbeats. The serve protocol accepts cancellation of an
active runtime stream and leaves every mutating or recording command
uncancellable.

Pinned gates:

- production-shaped 8-persona/60-skill snapshot performs one resolver walk and
  60 hashes, not 480;
- exact repeated chat binding preserves `updated_at` and emits one open event;
- a deliberately blocked full-core build emits activity heartbeats at the held
  watermark before its delta;
- cancellation of an active stream frees a one-worker serve pool for `status`;
- the existing patch/full-core convergence, prompt receipt, chat, and serve
  suites remain green.

Measured against the same live store after implementation: full core
**36.622s -> 5.949s wall** (`build_ms=5882`), an approximately **84% reduction**.
This clears the current 30-second client budget, while the activity heartbeat
and cancellation protocol keep correctness and liveness bounded if the store
grows past it again. The longer-term `<500ms` target and domain/event-log caches
above remain valid follow-up work; they are optimization, not correctness
prerequisites for this incident.
