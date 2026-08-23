# Planned — certification gates for the data layer (archived RD8)

**Status:** not implemented. **Domain:** runtime data and shapes.
**Opened:** 2026-08-22.

## Verification

- `tests/agent_runtime/test_read_model_soak.py` does not exist. The archived doc
  05 already recorded this itself (MCF-78, 2026-08-20: "not built").
- `agent_runtime/production_envelope.py` **does not exist at all**, so the
  archived plan's ending — "add a `runtime_data_storage` item to
  `agent_runtime/production_envelope.py`" — has no target module.
- What does exist: `tests/agent_runtime/test_read_model_slo.py`, holding
  `SLO_FULL_BUILD_MS = 2000` (line 12) and `SLO_CONSUMER_VISIBLE_LAG_MS = 1500`
  (line 18), and one test — `test_synthetic_snapshot_full_build_within_rd0_slo`
  (line 23), which asserts `build_ms <= SLO_FULL_BUILD_MS` against an
  `isolate_agent_runtime_root` fixture.

`SLO_INCREMENTAL_APPLY_MS = 150` was deleted with the incremental lane; the
comment at line 13 records that its only two assertions covered code nothing ran.

## The gap this leaves

The one surviving SLO assertion runs against a **synthetic isolated root**. The
live store's cold build is 11,235 ms — 5.6× the 2,000 ms SLO — and nothing in CI
notices, because CI never measures the shape that is slow. A green SLO suite and
an 11-second boot coexist today without contradiction.

That is the specific failure mode worth fixing, and it is narrower than the
archived RD8 stage: **the SLO must be measured against a fixture whose shape
resembles the live store**, or it certifies nothing.

## What the archived plan specified

1. Promote the SLOs to hard CI asserts — "fail, not warn".
2. A concurrency soak (`test_read_model_soak.py`, marked `integration`).
3. A `kill -9` crash drill.
4. A redaction-at-rest scan over persisted state.
5. Only after 10 consecutive green CI runs: a `runtime_data_storage` item in the
   production envelope, "per the H5 lesson: no advertised-but-inert controls".

Items 1 and 4 are the ones that apply cleanly to today's shape. Items 2 and 3
were written for the transactional read model with a single-writer projector
lease; that lane is retired, so a soak and a crash drill would now target
`core_cache.write_back()`'s generation publish instead — a different mechanism
with its own already-documented tearing argument (MCF-21: a write-back is one
unit, published by replacing `live.json`).

## Ordering constraint

The archived plan's final step gates on ten consecutive green runs *of the gates
above it*. That ordering stands, and one more precondition sits under all of it:
gates certifying a lane that nothing populates certify nothing. The ruling in
`read-model-db-serve-population.md` (deleted; ruled RETIRE 2026-08-22) came
first.

## The gate to open this

- A live-shaped fixture root (order-of-magnitude matching the operator store:
  ~90 MB event log, ~5 personas, ~50 chat sessions) so `SLO_FULL_BUILD_MS` means
  something.
- The core-cache lane converging first — see
  [core-cache-input-closure.md](core-cache-input-closure.md). Certifying build
  latency while the cache never serves measures the uncached path forever.
- A named home for the production-envelope entry, since the module the archived
  plan names does not exist.
- Redaction-at-rest scan scoped to the stores that actually hold text today:
  `mission_chat_turns/`, `prompt_observability/`, `serve_read_model/core.json`.
