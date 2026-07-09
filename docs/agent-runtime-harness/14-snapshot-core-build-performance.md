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

## Remaining plan (in value order, not scheduled)

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
