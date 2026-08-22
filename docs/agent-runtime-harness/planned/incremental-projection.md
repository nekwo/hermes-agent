# Planned — incremental projection (the O(world) build)

**Status:** not implemented; the previous attempt was retired 2026-08-01.
**Domain:** runtime data and shapes. **Opened:** 2026-08-22.
**Owning doc:** [`../02-runtime-data-and-shapes.md`](../02-runtime-data-and-shapes.md).

**Scope boundary.** This plan is about the build being O(world) on *every* call.
The separate fact that the FIRST build in a process costs 3–5× a later one — cold
per-process cache population — is
[`cold-first-core-build-cost.md`](cold-first-core-build-cost.md). Both are true of
the same 11,235 ms receipt; they need different fixes and different gates.

## The problem

Every snapshot build re-projects the whole store. `build_snapshot()`
(`agent_runtime/snapshot.py:514`) walks the entity trees, scans the event log,
and reads SessionDB from scratch on every call. Nothing maintains a projection
incrementally; all the caching in this domain sits *around* the build — the core
cache, the parse cache, the coalescer, `demote_core_reuse` — never *inside* it
as a delta.

Archived doc 05 named this "O(world) per read AND per write" and made an
incremental projector its RD3 stage.

## Evidence

Cold boot, live serve, 2026-08-22 15:46, `profiles/base/logs/agent.log`:

```
snapshot_build_core role=led caller=prewarm generation=1 build_ms=11235 offset=90007293
  sections_top=prompt_observability:4520,agents_readiness:4366,events:842 pid=30588
```

Warm builds in the same process, generations 18–22 (13:45–13:46): 3,733 / 2,213
/ 2,714 / 1,948 / 3,439 ms.

The two projection walks dominate. `events` at 842 ms is not the cost —
`CachedEventLog` (`events.py:323`) already reads the log once per build and
serves every `for_task` / `for_session` / `tail` from cached lines.

`agents_readiness` is two walks under one name. The split receipt
(`snapshot.py:432`) measures, against 5 runtime personas on 2026-08-22: first
build in a process 4,001 ms (3,054 tool-visibility / 947 readiness), every later
build in the same process 183 ms (36 / 146).

## What was retired, and why it must not simply be restored

`agent_runtime/projector.py` still exists but holds only `full_rebuild()`. Its
docstring records the ruling verbatim:

> S46 retired the INCREMENTAL lane (ledger item 9, operator-ruled RETIRE
> 2026-08-01). `apply_pending` — with the `meta.projector_lease` it took, the
> watermark diff it did, the pending-event count it made, and the
> `ProjectorResult` offsets/timings only it produced — had five test callers and
> zero production ones. The RD3 "ticker chokepoint" that was supposed to drive
> it (doc 05:348) was never wired, so its SLO tests were timing a lane nothing
> ran.

`tests/agent_runtime/test_read_model_slo.py:13` carries the matching scar:
`SLO_INCREMENTAL_APPLY_MS = 150` was deleted because its only two assertions
covered the retired lane. `SLO_FULL_BUILD_MS = 2000` and
`SLO_CONSUMER_VISIBLE_LAG_MS = 1500` survive.

**The lesson is the plan's first constraint: a projection lane with no
production driver is dead code that reds nothing.** Any revival must name and
wire its driver before it lands, not after.

## The harder constraint

An offset-keyed incremental lane cannot be sound here on its own. `core_cache.py`
documents why an event offset is refused as a *validity* key, and the same
argument applies to incremental application:

> two shipped incidents came from writers that mutate durable state with NO
> EventLog event (`running_work.py`'s checkpoint, `board_sync`'s
> materialization), so an offset key cannot see them at all.

So "apply the events since the watermark" does not reconstruct the store's
current state. Either those writers gain events (the archived doc 12 direction —
store-level emission with a CI guard) or the incremental lane needs a second
change signal, which reintroduces the two-authorities drift the cache design
explicitly refuses.

## The gate to open this

1. A named production driver, wired, with a test that fails when it stops
   firing.
2. A replay-equivalence certification test: a store projected incrementally
   must equal the same store projected fully, field-for-field. Archived doc 05
   named this `test_replay_equivalence_full_vs_incremental` and made it the ship
   gate; that bar stands.
3. Every no-event durable writer either emits or is covered by a declared
   second signal — with the drift risk between the two authorities argued, not
   assumed away.
4. `SLO_FULL_BUILD_MS = 2000` met on the live store, not a synthetic fixture.

## Cheaper move available first

The two hot sections are `prompt_observability` (4,520 ms) and the
tool-visibility half of `agents_readiness` (3,054 ms first-build). Both are
per-persona resolution walks whose per-process second run is already ~20× cheaper
(183 ms vs 4,001 ms), which says the cost is cold caches, not algorithmic
depth. Reducing those two before building an incremental lane is the lower-risk
ordering, and it may move the number far enough that the lane is not worth its
soundness cost.
