# Stream-frame contract goldens (cross-repo)

Golden `hermes harness stream` frames shared with the EterniaLauncher repo,
which commits **byte-identical copies** under `test/fixtures/harness_stream/`
and parses them through its real decode + read-model pipeline
(`mission_stream_contract_fixture_test.dart`).

## Which files are generated, and which are hand-maintained

`MANIFEST.sha256` pins **eleven** files, but
`scripts/generate_agent_runtime_stream_fixtures.py` writes only **five**. The
split is structural, not an oversight, and the script names both halves
(`GENERATED_FRAME_FILES` / `PINNED_ONLY_FILES`).

> **OUTSTANDING CROSS-STACK COPY (2026-08-16, office fold-promotion O-H3).**
> Two entries moved on the hermes side and the launcher's byte-identical copies
> under `test/fixtures/harness_stream/` have **not** been updated yet, because
> that checkout is the operator's live tree:
>
> * `patch_coverage_manifest.json` — **bytes changed.** `covered_domain_events`
>   gained `persona_instance.retired`, `persona_instance.chat_opened` and
>   `office.actor.removed`; the `office.actor.create` and `office.actor.removed`
>   cases flipped from `refresh`/uncovered to `upsert`/`remove`; three cases
>   were added. The launcher pins its own copy, so it stays green while
>   silently diverging — which is precisely the drift the update rule below
>   exists to prevent.
> * `patch_delete_gesture.json` — **new file.** Additive, so nothing goes red
>   without it; the launcher's golden fold of the milestone batch is what it is
>   for.
>
> Copy both, update the launcher's manifest, in the change that lands O-L1..3.

| File | Origin |
| --- | --- |
| `hydrate.json`, `delta.json`, `heartbeat.json`, `delta_batch.json` | **Generated.** Built from a **seeded isolated runtime root** (empty store + two `state.reconciled` events) by the current production frame builders — never from a live store. |
| `hydrate_running_work_owner.json` | **Generated.** A second hydrate frame, taken after the same isolated root is seeded with one persona instance and two background delegations — one spawned from that instance's chat root, one from a plain CLI session. It is the cross-repo pin for `running_work.rows[].owner`, and it exists because the launcher's Activity surface **groups by owner**: a null owner does not make work late, it makes it invisible. Both halves are golden — the owned row names its agent, the unowned row ships an **empty** owner rather than a guess. `pid` / `elapsed_seconds` are normalized (this run's process and wall clock); `status` / `pid_verified` deliberately are **not** — the seed clears the spawn baseline so every platform agrees on `unknown` / `false`. |
| `patch.json`, `patch_upsert_profile.json`, `patch_remove.json` | **Hand-maintained.** S6 v2 field-patch frames carrying real wall-clock stamps (`2026-07-17T04:22:55.149761Z`, not the generator's `FIXED_TIME`) and hand-chosen `base_offset`/`seq` pairs demonstrating specific fold semantics over entities the seeded root does not contain. `patch_remove.json` is additionally **un-emittable today**: it is the `incident.closed` remove fold, and S65 de-registered that event with its last writer (`agent_runtime/patch_coverage.py` keeps it in `HISTORICAL_COVERED_DOMAIN_EVENT_TYPES` so an old replayed batch still classifies the way the launcher folded it). Regenerating it would mean resurrecting a retired lane. |
| `patch_delete_gesture.json` | **Hand-maintained.** The office fold-promotion milestone (O-H3, 2026-08-16): the DELETE gesture's coalesced batch as one patch frame — a `persona_instance` remove beside an `office_actor` remove, `coalesced_count` 4 because the two paired domain events ride the batch and fold to nothing. Pinned rather than generated for its siblings' reason (the seeded isolated root has no office surface and no retired placement), and it carries the MIXED batch on purpose: one frame, one watermark, both removes, which is the pairing the office sink's old filtered forwarding broke. |
| `patch_coverage_manifest.json` | **Hand-maintained.** Not a frame at all — the S7-A coverage table. |

The hand-maintained four are validated by **shape + live-classifier agreement**
(`test_stream_patch.py`), not by byte-regeneration. Editing one is a hand edit
under the update rule below; the generator only hashes them.

- Regenerate the five with
  `python scripts/generate_agent_runtime_stream_fixtures.py`; it calls the
  current production builders and normalizes only volatile timestamps, timings,
  the temporary root spelling, and `core.repo_scopes[*].resolved`.
- **`repo_scopes[*].resolved` is a pinned fixture constant, not a measurement.**
  `snapshot._repo_scope_entry` derives it from
  `resolve_affected_repo_workdir()`, which now resolves the logical
  `eternia_launcher` / `eternia_backend` bindings through the machine-local
  `machine_roots.json` authority. The isolated generator deliberately has no
  operator bindings, while the committed fixtures preserve the production-like
  `true` values used by both repos. Normalization therefore remains necessary:
  it separates a fixture choice from the generating machine's configuration.
  The three `label` values are contractual and are **not** normalized.
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
