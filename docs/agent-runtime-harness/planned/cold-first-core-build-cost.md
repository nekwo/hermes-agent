# Planned — the cold first core build is still ~11s, and its cost is named

**Status:** NOT IMPLEMENTED as a reduction. The MEASUREMENT is shipped and precise; no
stage is currently aimed at the number.
**Owning doc:** [`../04-boot-and-lifecycle.md`](../04-boot-and-lifecycle.md) Stage 7.

**Scope boundary — read this first.** This plan is about the FIRST build in a process
being 3-5× the cost of a later one in the same process, i.e. **cold per-process cache
population**. It is NOT about the build being O(world) on every call; that is
[`incremental-projection.md`](incremental-projection.md), which reaches the same
receipt from the other side and explicitly separates the two ("the cost is cold caches,
not algorithmic"). Both can be true; they need different fixes and different gates.

## The claim

The serve process comes up in ~1.5-3 seconds typically, with a tail to 4.3 s — 28 boots
sampled 2026-08-21/22, `total_ms` 1,042 to 4,335, five of them over 3,000. The first
read-model core takes several times longer than even that tail, and it is the thing the
operator's canvas actually waits for. Every boot-window stage that shipped (BW-0, BW-H1,
BW-H2, BW-H3, HY-0, HY-H1, HY-H2) either instrumented this number or moved work off its
thread. **None of them reduced the cold build itself.**

## Evidence (live, `X:/Eternia/.hermes/profiles/base/logs/agent.log`)

Serve boot, 2026-08-22 15:46:27 — `harness serve boot timeline: … total_ms=1630`.
Eleven seconds later, the same boot's first core:

```
15:46:38 snapshot_build_core role=led caller=prewarm generation=1 build_ms=11235
  offset=90007293 sections_top=prompt_observability:4520,agents_readiness:4366,events:842
```

Three cold first builds, three boots, same shape:

| boot | `build_ms` | top sections |
|---|---|---|
| 2026-08-22 10:05 | 10532 | `agents_readiness:5375` (`tool_visibility:2801`, `walk:2574`) |
| 2026-08-22 13:36 | 7597 | `agents_readiness:3999, prompt_observability:1702, events:968` |
| 2026-08-22 15:46 | 11235 | `prompt_observability:4520, agents_readiness:4366, events:842` |

The readiness split from the 15:46 boot: `snapshot_agents_readiness walk_ms=2133
tool_visibility_ms=2232`. Warm, in an earlier process the same day: `walk_ms=769
tool_visibility_ms=26`.

That the cost is per-process cache fill and not real work is proven inside one process:
generations 21 and 22 cost `build_ms=1948` and `3439` against generation 1's five-figure
number.

## What is already known about where it goes

Named caches, all cold at `generation=1` (see the owning doc, Stage 7):
`agent_runtime/parse_cache.py` (YAML/frontmatter/sha, `(path, mtime_ns, size)`-keyed,
bounded 4096); `tool_visibility._cached_tool_names_for_toolsets` (`lru_cache(128)`,
process lifetime); `tool_visibility._cached_profile_readiness_for_visibility` (15s TTL);
`tools/registry.py::_check_fn_cached` (30s TTL per `check_fn`, 60s failure grace).

Two sections dominate and they alternate for the top slot, which is itself a finding —
`prompt_observability` was not the section the earlier boot-window work was aimed at.

## The two directions, neither chosen

1. **Make the cache hit reliably.** If the persisted core were actually served, the cold
   build would move off the operator's path entirely — measured 210 ms to a cache hit vs
   11,235 ms to a cold build. This is the cheaper win by a wide margin and is blocked
   behind [`core-cache-input-closure.md`](core-cache-input-closure.md) and
   [`core-cache-home-capture-timing.md`](core-cache-home-capture-timing.md). **Do these
   first.**
2. **Make the cold build cheaper**, for the boots that legitimately have no cache — a
   genuinely new store, an upgrade that moved the build stamp. Only worth opening once
   (1) lands, because until then every boot is a cold boot and the measurement cannot
   separate the two populations.

## Staging (coordinator, 2026-08-22)

Direction (1) is chosen and is not this file's work: it is
[`core-cache-input-closure.md`](core-cache-input-closure.md) stages IC-1..IC-4
plus [`core-cache-home-capture-timing.md`](core-cache-home-capture-timing.md)
HC-1..HC-3. This plan stays PARKED until those land and a week of operator
boots shows the hit rate; only then does direction (2) — making the cold build
itself cheaper — get measured against the boots that remain legitimately cold.
Opening (2) before (1) cannot separate the populations (its own § above).

## Gate

Whichever direction is taken, the number that must move is
`snapshot_build_core … generation=1 build_ms=` on a real operator boot, reported from
the live log across at least three boots — not a synthetic benchmark, and not
`elapsed_ms`/`waited_ms`, which measure a caller's wait rather than the build.

If direction (2) is taken, `sections_top=` must show the reduction in the section that
was targeted. A total that fell while the targeted section did not is a measurement
artifact of a warm machine, not a fix.
