# Stream-frame contract goldens (cross-repo)

Golden `hermes harness stream` frames shared with the EterniaLauncher repo,
which commits **byte-identical copies** under `test/fixtures/harness_stream/`
and parses them through its real decode + read-model pipeline
(`mission_stream_contract_fixture_test.dart`).

## Which files are generated, and which are hand-maintained

`MANIFEST.sha256` pins **eight** files, but
`scripts/generate_agent_runtime_stream_fixtures.py` writes only **four**. The
split is structural, not an oversight, and the script names both halves
(`GENERATED_FRAME_FILES` / `PINNED_ONLY_FILES`).

| File | Origin |
| --- | --- |
| `hydrate.json`, `delta.json`, `heartbeat.json`, `delta_batch.json` | **Generated.** Built from a **seeded isolated runtime root** (empty store + two `state.reconciled` events) by the current production frame builders — never from a live store. |
| `patch.json`, `patch_upsert_profile.json`, `patch_remove.json` | **Hand-maintained.** S6 v2 field-patch frames carrying real wall-clock stamps (`2026-07-17T04:22:55.149761Z`, not the generator's `FIXED_TIME`) and hand-chosen `base_offset`/`seq` pairs demonstrating specific fold semantics over entities the seeded root does not contain. `patch_remove.json` is additionally **un-emittable today**: it is the `incident.closed` remove fold, and S65 de-registered that event with its last writer (`agent_runtime/patch_coverage.py` keeps it in `HISTORICAL_COVERED_DOMAIN_EVENT_TYPES` so an old replayed batch still classifies the way the launcher folded it). Regenerating it would mean resurrecting a retired lane. |
| `patch_coverage_manifest.json` | **Hand-maintained.** Not a frame at all — the S7-A coverage table. |

The hand-maintained four are validated by **shape + live-classifier agreement**
(`test_stream_patch.py`), not by byte-regeneration. Editing one is a hand edit
under the update rule below; the generator only hashes them.

- Regenerate the four with
  `python scripts/generate_agent_runtime_stream_fixtures.py`; it calls the
  current production builders and normalizes only volatile timestamps, timings,
  the temporary root spelling, and `core.repo_scopes[*].resolved`.
- **`repo_scopes[*].resolved` is a pinned fixture constant, not a measurement.**
  `snapshot._repo_scope_entry` derives it from
  `resolve_affected_repo_workdir()`, which probes **hardcoded absolute paths**
  (`agent_runtime/repo_context.py` `_REPO_ALIAS_PATHS`, e.g.
  `X:/Unreal Engine/Engine/Launcher/EterniaLauncher`). Unpinned, `frontend` and
  `backend` emit `true` on a box carrying those checkouts and `false` on CI, a
  fresh clone, or macOS/Linux — so the golden bytes depended on who ran the
  script. The pin asserts nothing about any machine's checkout layout. The three
  `label` values are contractual and are **not** normalized.
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
