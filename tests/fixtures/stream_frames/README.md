# Stream-frame contract goldens (cross-repo)

Golden `hermes harness stream` frames shared with the EterniaLauncher repo,
which commits **byte-identical copies** under `test/fixtures/harness_stream/`
and parses them through its real decode + read-model pipeline
(`mission_stream_contract_fixture_test.dart`).

- Generated from a **seeded isolated runtime root** (empty store + two
  `state.reconciled` events) — never from a live store.
- Regenerate with `python scripts/generate_agent_runtime_stream_fixtures.py`;
  the script calls the current production builders and normalizes only volatile
  timestamps, timings, and the temporary root spelling.
- `MANIFEST.sha256` pins the bytes; `test_stream_contract_fixture.py` fails if
  either drifts.
- `delta_batch.json` pins the W1 coalescing shape (`events[]`,
  `coalesced_count`, `entity` = last event, watermark at final offset).
- `patch.json` pins the S6 v2 field-patch frame (`type:"patch"`,
  `schema_version:2`, `base_offset`, `patches[] = {seq,ts,entity,id,changed}`,
  `coalesced_count`, **no `core`**). Shipped only when
  `read_model.delta_patches` is on and the batch is fully coverable; the
  launcher folds it into its keyed tables (`mission_read_model_test.dart`).

**Update rule:** these fixtures change only in a cross-stack change that lands
hermes **and** the launcher together (regenerate, copy bytes, update both
manifests in the same change). Plan:
`EterniaLauncher/docs/mission_control/TRANSPORT_ENTERPRISE_PLAN_2026-07-16.md`.
