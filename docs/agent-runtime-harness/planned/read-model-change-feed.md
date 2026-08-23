# Planned — change feed / push invalidation (archived RD4)

**Status:** not implemented — and MOOT AS SPECIFIED since Stage 6 of the
duplicate-implementation retirement (2026-08-22): the plan's producer was "the
projector", and `agent_runtime/projector.py` is now DELETED with the whole
`read_model.db` lane (doc 02 § the retired read model). Any revival must name
a new producer; the feed-shape and watcher design below survive as reference
only. Kept pending the same delete ruling as
[read-model-certification-gates.md](read-model-certification-gates.md) and
[read-model-schema-migrations.md](read-model-schema-migrations.md).
**Domain:** runtime data and shapes. **Opened:** 2026-08-22.

## Verification

Greps over the whole repo (`*.py`, `*.dart`) on 2026-08-22 return **zero** hits
for `read_model.feed`, `read_model_feed`, `ReadModelFeed`, `change_feed`.
`tests/agent_runtime/test_projector_feed.py` — the certification test the
archived plan named — does not exist. (`agent_runtime/projector.py` was 29
lines containing only `full_rebuild()` when this was verified; Stage 6 has
since deleted the module entirely.)

**Correction to the archived source.** The header of
[05-runtime-data-enterprise-storage.md](../archive/2026-08-22-pre-consolidation/05-runtime-data-enterprise-storage.md)
(correction block dated 2026-07-30) lists the "NDJSON change feed" among things
that are "live and current". That clause is false and must not be propagated.
The same doc's own Baselines table contradicts it — the entire Post-RD4 column
is empty and `consumer_visible_lag_ms` still reads "≥ 4,000 (poll)" with no post
value.

## What the archived plan specified

- The projector appends one compact NDJSON record per committed batch to
  `<store_root>/read_model.feed`, rotating at 5 MB, keeping 2.
- Record shape:
  `{"watermark": <offset>, "ts": …, "changed": {"sections": ["parity", …], …}}`,
  ids bounded at 50 per kind, overflow → `"all"`.
- Launcher side: a `ReadModelFeedWatcher` doing a Dart `File` length-poll at
  250 ms (a cheap stat, not a JSON parse), with the existing ≥ 4 s poll retained
  as heartbeat fallback.
- Push transport optional: "hermes-agent has NO Centrifugo client today
  (verified) … if that dependency is unwanted, ship feed-file-only". **The feed
  file is the contract; the push is not.**
- Proof bar: event → HUD-fetchable ≤ `SLO_CONSUMER_VISIBLE_LAG_MS` (1500 ms).
  That constant still exists at `tests/agent_runtime/test_read_model_slo.py:18`
  and nothing asserts against it end-to-end.

The goal/task entity kinds in the archived record shape (`goals`, `runs`) are
dead — the mission lane was removed 2026-07-30. Sections and persona/chat
entities are what a feed would carry today.

## Blockers to name before building

1. **There is no producer to hang it on.** The archived design attaches the feed
   to the incremental projector's commit boundary. That lane was retired
   2026-08-01 (see [incremental-projection.md](incremental-projection.md)), so
   the feed has no natural chokepoint. A feed written from `write_back()` in
   `core_cache.py` instead would fire once per whole-core build — which is
   coarse, but it is where the runtime actually commits a projection today.
2. **A feed is a second freshness authority.** The archived doc 12 records the
   hard client constraint: the launcher drops a `hydrate`/`delta` frame whose
   offset is not strictly greater. Any invalidation signal must **advance the
   offset**, never re-announce the same one.
3. **A file whose length is polled is a fingerprint input.** Writing
   `read_model.feed` into `store_root()` puts a runtime-authored,
   every-build-mutating file inside the core cache's stat walk. The cache
   already excludes its own writes for exactly this reason
   (`core_cache.py:240-243`); the feed would need the same treatment, ruled
   explicitly rather than discovered as a `never_converged` receipt.

## The gate to open this

- A named commit chokepoint that exists in production code today.
- The offset-advancement rule satisfied by construction.
- An explicit exclusion of the feed file from the core cache fingerprint.
- An end-to-end measurement against `SLO_CONSUMER_VISIBLE_LAG_MS` on the live
  store, not a synthetic root.
- Ship feed-file-only. Do not add a push-transport dependency in the same
  landing.
