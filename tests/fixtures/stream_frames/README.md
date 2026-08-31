# Stream-frame contract goldens (cross-repo)

Golden `hermes harness stream` frames shared with the EterniaLauncher repo,
which commits **byte-identical copies** under `test/fixtures/harness_stream/`
and parses them through its real decode + read-model pipeline
(`mission_stream_contract_fixture_test.dart`).

## Which files are generated, and which are hand-maintained

`MANIFEST.sha256` pins **sixteen** files, but
`scripts/generate_agent_runtime_stream_fixtures.py` writes only **nine**. The
split is structural, not an oversight, and the script names both halves
(`GENERATED_FRAME_FILES` / `PINNED_ONLY_FILES`).

> **CROSS-STACK COPY STATUS (AX2, 2026-08-31) — OPEN, launcher mirror OWED.**
> Seven generated hydrate/delta goldens lost three core keys with the writerless
> assignment lane: `persona_assignments` (the whole block, with its `recent_ref`
> eviction pointer), `persona_instance_runtime.assignment_store_enabled`, and
> `warnings` (whose only producer emitted only `agent_already_assigned`, a code
> the launcher had already tombstoned). No file was added or removed, so this
> manifest's MEMBERSHIP and LINE ORDER are unchanged and
> `check_producer_contracts.py` compares those clean; seven HASHES moved.
> **`SNAPSHOT_CONTRACT_VERSION` deliberately did NOT move** — the ruling and its
> argument are written at `snapshot._parity_envelope`'s version history, under
> "54 KEPT (AX2)". The launcher must mirror these bytes into
> `test/fixtures/harness_stream/` and update its own `MANIFEST.sha256`; until it
> does, both repos are green while they disagree, which is exactly the drift the
> notes below exist to prevent.

> **CROSS-STACK COPY STATUS (placement verb S0, settled 2026-08-26).**
> `patch_agent_create.json` and `delta_agent_create_narrow_profile.json` were
> added and mirrored byte-for-byte into the launcher's
> `test/fixtures/harness_stream/` in the same landing, rows inserted at this
> manifest's exact position (between `hydrate_authoritative_same_offset.json`
> and `patch.json`) because `check_producer_contracts.py` compares membership
> and line ORDER before it hashes anything. They are the first goldens built
> from a real store WRITE rather than from a seeded event log, which is why the
> generator grew three opt-in normalizations for them — see `_normalize`,
> `_REGISTRY_PROBED_VALUES` and `_FIXTURE_CREATE_OFFSET_BASE`. No existing
> golden's bytes moved.

> **CROSS-STACK COPY STATUS (BO-1, settled 2026-08-22).** `hydrate_stale_first.json`
> and `hydrate_authoritative_same_offset.json` were mirrored byte-for-byte into the
> launcher's `test/fixtures/harness_stream/` (launcher `37762bc0e`), rows inserted at
> this manifest's exact position, plus the pair's convergence case in
> `mission_stream_contract_fixture_test.dart`. The launcher's
> `tool/test_quality/check_producer_contracts.py` (note: that checker lives in the
> LAUNCHER repo, invoked with `--hermes-root`; its default mode runs this repo's
> generators — use `--no-generate` for a read-only comparison) reports both manifests
> matching. That debt is settled.

> **CROSS-STACK COPY STATUS (2026-08-16).** The O-H3 wave's two entries —
> `patch_coverage_manifest.json`'s changed bytes and the new
> `patch_delete_gesture.json` — were mirrored into the launcher by
> `04e5b14e6` / `cdc481040`, so that debt is settled.
>
> D3 (the foldable `persona_instance` create) moves
> `patch_coverage_manifest.json` once more: it gains a
> `persona_instance.create` case beside `office.actor.create`. No generated
> frame moves, because `created` was already an optional key on the
> `state.patched` contract, so `decision_contract_hash` does not shift. The
> launcher copy and BOTH `MANIFEST.sha256` files are updated in the same
> cross-stack change and verified by hashing from both sides — a manifest
> updated on one side only leaves both repos green while they disagree, which
> is the exact drift this note exists to prevent.
>
> WV-H3 (the foldable office SURFACE write, 2026-08-16) adds
> `patch_office_surface.json` and moves `patch_coverage_manifest.json` again:
> `office.surface.updated` joins `covered_domain_events`, the old
> `office.surface.write` case becomes a foldable `office_surface` upsert with a
> bundled frame, and an `office.surface.created` case records the half that
> stays uncovered. Both `MANIFEST.sha256` files move in the same cross-stack
> change, and the ORDER moves with them — the new file sits between
> `patch_delete_gesture.json` and `patch_coverage_manifest.json`, because
> `tool/test_quality/check_producer_contracts.py` compares manifest line order
> BEFORE bytes.

| File | Origin |
| --- | --- |
| `hydrate.json`, `delta.json`, `heartbeat.json`, `delta_batch.json` | **Generated.** Built from a **seeded isolated runtime root** (empty store + two `state.reconciled` events) by the current production frame builders — never from a live store. |
| `hydrate_running_work_owner.json` | **Generated.** A second hydrate frame, taken after the same isolated root is seeded with one persona instance and two background delegations — one spawned from that instance's chat root, one from a plain CLI session. It is the cross-repo pin for `running_work.rows[].owner`, and it exists because the launcher's Activity surface **groups by owner**: a null owner does not make work late, it makes it invisible. Both halves are golden — the owned row names its agent, the unowned row ships an **empty** owner rather than a guess. `pid` / `elapsed_seconds` are normalized (this run's process and wall clock); `status` / `pid_verified` deliberately are **not** — the seed clears the spawn baseline so every platform agrees on `unknown` / `false`. |
| `hydrate_stale_first.json`, `hydrate_authoritative_same_offset.json` | **Generated, and the only goldens that are a PAIR.** EG-3.1's mismatch half taken off the real producer: frame 1 wraps `core_cache.take_stale_first_core`'s labelled core (`freshness.state="stale"`, `core_stale:true`, `core_source:"cache"`), frame 2 is a real gated rebuild (`freshness.state="fresh"`, no `core_stale`, `core_source:"rebuilt"`), and **both carry the same `watermark.event_offset`** because the store's log is idle between them. That equality is the contract: the launcher's ordinary sequence gate is strict `>`, so only `MissionReadModel.staleHeldAwaitsAuthoritative` lets frame 2 land, and a producer that deduped the same-offset re-hydrate would freeze every launcher on a stale canvas. The mismatch is arranged by re-persisting the seeded persona-instance row — a real durable write that appends **no** event, which is both why the fingerprint misses and why the offsets stay equal. They also pin the producer's non-stale token as `fresh` on real bytes for the first time. |
| `patch_agent_create.json`, `delta_agent_create_narrow_profile.json` | **Generated, and the second PAIR here.** The placement verb's S0 (hermes `docs/agent-runtime-harness/06-office-and-board.md`; the plan file this cell used to cite shipped and was deleted 2026-08-27): ONE real `perform_agent_create` against a seeded workspace, rendered through `stream._batch_frames_with_liveness` — the promotion decision itself — for two different subscribers. Frame 1 is what a client declaring the launcher's own `kMissionFoldDeclaredEntities` receives: ONE `patch` carrying the `persona_instance` and `office_actor` creates, both stamped `created: true`, `coalesced_count` 4 because the two paired domain events ride the batch and fold to nothing. Frame 2 is the SAME batch seen by a subscriber declaring only the historical `{persona_instance, incident}` — the full-core demote `accepted_fold_entities`' intersection forces on the whole room the moment one narrow client joins it. Neither says anything alone: the pair's contract is that the promotion decision is the SUBSCRIBER's declaration and nothing else about the create. They are the only goldens built from a real store write, so three values are pinned rather than measured — the event-log byte OFFSETS (a create's events embed absolute paths, so the log's positions are a function of the generating machine's temp dir; the frames' contract is the strict ordering `base_offset` < every `seq` ≤ `watermark`, which the rank map preserves), the four registry-probed tool scalars (`tool_count` / `blocked_tools_count` / `effective_toolsets` / `mutation_boundary`, all counted off the import-populated tool registry), and the `ts` / `last_event_ts` wall clocks. `delta.json` and `delta_batch.json` deliberately do NOT normalize those two stamps — their one-second gap is the batch's own evidence it coalesced two events — which is why the rule is opt-in per frame. |
| `patch.json`, `patch_upsert_profile.json`, `patch_remove.json` | **Hand-maintained.** S6 v2 field-patch frames carrying real wall-clock stamps (`2026-07-17T04:22:55.149761Z`, not the generator's `FIXED_TIME`) and hand-chosen `base_offset`/`seq` pairs demonstrating specific fold semantics over entities the seeded root does not contain. `patch_remove.json` is additionally **un-emittable today**: it is the `incident.closed` remove fold, and S65 de-registered that event with its last writer (`agent_runtime/patch_coverage.py` keeps it in `HISTORICAL_COVERED_DOMAIN_EVENT_TYPES` so an old replayed batch still classifies the way the launcher folded it). Regenerating it would mean resurrecting a retired lane. |
| `patch_delete_gesture.json` | **Hand-maintained.** The office fold-promotion milestone (O-H3, 2026-08-16): the DELETE gesture's coalesced batch as one patch frame — a `persona_instance` remove beside an `office_actor` remove, `coalesced_count` 4 because the two paired domain events ride the batch and fold to nothing. Pinned rather than generated for its siblings' reason (the seeded isolated root has no office surface and no retired placement), and it carries the MIXED batch on purpose: one frame, one watermark, both removes, which is the pairing the office sink's old filtered forwarding broke. |
| `patch_office_surface.json` | **Hand-maintained.** The office write-verbs milestone (WV-H3, 2026-08-16): a FOLDER change as one patch frame — a single `office_surface` **subset** upsert carrying the three fields `update_surface` moves, `coalesced_count` 2 because the paired `office.surface.updated` rides the batch and folds to nothing. Worth a cross-repo pin because it is the row whose shape the two sides could most easily disagree about in silence: it MERGES onto the office row, unlike its `office_actor` sibling's complete-row replace, so a launcher folding it as a replace would drop the actor list on every folder rename with nothing on the producer side able to see it. |
| `patch_coverage_manifest.json` | **Hand-maintained.** Not a frame at all — the S7-A coverage table. |

The hand-maintained ones are validated by **shape + live-classifier agreement**
(`test_stream_patch.py`), not by byte-regeneration. Editing one is a hand edit
under the update rule below; the generator only hashes them.

- Regenerate the nine with
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
