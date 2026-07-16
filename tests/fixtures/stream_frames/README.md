# Stream-frame contract goldens (cross-repo)

Golden `hermes harness stream` frames shared with the EterniaLauncher repo,
which commits **byte-identical copies** under `test/fixtures/harness_stream/`
and parses them through its real decode + read-model pipeline
(`mission_stream_contract_fixture_test.dart`).

- Generated from a **seeded isolated runtime root** (empty store + two
  `state.reconciled` events) — never from a live store.
- `MANIFEST.sha256` pins the bytes; `test_stream_contract_fixture.py` fails if
  either drifts.
- `delta_batch.json` pins the W1 coalescing shape (`events[]`,
  `coalesced_count`, `entity` = last event, watermark at final offset).

**Update rule:** these fixtures change only in a cross-stack change that lands
hermes **and** the launcher together (regenerate, copy bytes, update both
manifests in the same change). Plan:
`EterniaLauncher/docs/mission_control/TRANSPORT_ENTERPRISE_PLAN_2026-07-16.md`.
