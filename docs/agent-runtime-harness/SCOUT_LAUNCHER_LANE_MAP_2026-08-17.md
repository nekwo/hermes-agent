# EterniaLauncher lane-ambiguity audit @ 6a121cbe9
Delivered by the Explore scout 2026-08-17. Verbatim relay; paths rooted at
X:\Unreal Engine\Engine\Launcher\EterniaLauncher.

# 1. Read lanes into MissionReadModel / office state

Every writer of the read model (exactly 5 entry points, 3 base-moving sites):

| lane | entry | base moved? |
|---|---|---|
| NDJSON `harness stream` child, full core (`hydrate`/`delta`) | bridge:1894 -> read_model:687-699 | yes, `stream_full_core` (read_model:698) |
| NDJSON child, `patch` frame | bridge:1826-1892 -> `_foldPatchOffIsolate` bridge:2040 | yes, `fold_commit:stream` (bridge:2079) |
| RPC push `runtime.office.patch` | subscribe_lane:632-682 -> `missionOfficePushFoldOnto` subscribe_lane:225-244 -> `foldPushedPatchFrame` bridge:2249 | yes, `fold_commit:push` (bridge:2079) |
| one-shot `harness stream --max-frames 1` hydrate | bridge:1245-1305 | yes, `snapshot_apply` (read_model:465) |
| CLI `harness snapshot` poll | bridge:2546 / 2552 | yes, `snapshot_apply` — but NO rawCore (see hazard A) |
| cached disk snapshot | bridge:1186-1209 | NO — returned to caller without touching the read model (bridge:1208); precedence decided by `_applySequenceOrdering` (provider:1114-1123) |
| `runtime.office.get` (office RPC read) | layout_controller:1693-1725 | NO — feeds `resolveLayout` only, never the read model |
| `runtime.office.subscribe` reply body | subscribe_lane:541-550 | NOT ADOPTED — `onBaseline` deliberately unwired at production mount (subscribe_lane:815-820, 850-854) |
| capability results (`submitIntent`) | — | never enter the read model; only request a refresh (layout_controller:1607/1617) |

The NDJSON child still runs beside the office push lane. Spawn:
`_startLongLivedStream` bridge:1410-1466 (`harness stream --fold-entities
<kMissionFoldDeclaredEntities>` at 1440-1465), kept alive by
`_armLivenessWatchdog`/`_restartStreamIfWedged` (bridge:1383-1408). Gated on
`_preferLongLivedStream` (bridge:763-765), true in production. The push lane
mounts independently at `missionOfficePushLaneProvider` (subscribe_lane:821-862),
watched from mission_control_page.dart:2940-2946. Note:
`mission_control_serve_session_io.dart` does NOT spawn the stream child — it owns
the `harness serve` JSON-RPC child and only special-cases `harness stream` for
cooperative cancel (serve_session_io:1045, 1102, 1540).

FC-L2 state: both `patch` lanes share one body (`_foldPatchOffIsolate`,
bridge:2040-2096) and one serialization chain (`_foldChain`/`_enqueueFold`,
bridge:2140-2155). Stream folds enqueue at bridge:1843; push folds at
bridge:2251. Dedup at `prepareFold`'s strict-> gate (read_model:483-494, drop
`stale_patch:` at 888). Fences: `_streamFoldClaimLost` bridge:2172-2199 and
`_pushFoldClaimLost` bridge:2337-2359, both reading
`coreRevision`/`coreRevisionWriter`/`coreRevisionMovedTo` (read_model:351-403).
Base revision captured INSIDE the chain link (bridge:1849 comment) so queue
ordering is not mistaken for contention. Double-apply via two fold lanes is
closed.

## Hazard A — poll lane moves the watermark without moving the fold base (OPEN)

`applySnapshot` (read_model:447-467) assigns `_sequence` and bumps
`coreRevision`, then calls `_retainFoldBase(rawCore, ...)` (read_model:474-481):
with `_patchModeActive` latched true and `rawCore == null`, `_lastCore` is left
at the OLD core while `_sequence` advances. The CLI poll is the only full-core
apply that passes no `rawCore` — bridge:2546 and bridge:2552. Consequence: a
later `patch` whose `base_offset` equals the poll's offset passes both gates
(`shouldApplySequence` read_model:886, gap check read_model:914) and folds onto
a stale base; `commitFold` (read_model:1015-1041) then publishes it as truth,
silently discarding everything the poll's core carried beyond the patched rows.
The revision fence cannot see it — the poll happened before `prepareFold`, not
during. Reachable path: stream child dies -> `_streamRunning=false` -> reconnect
backoff not ready -> cache unusable -> hydrate fails -> `_loadSnapshotFromCli`
(bridge:1147-1155) applies, while the push lane (independent of the stream
child, on the serve session) keeps delivering patches. `_lastCore` is never
nulled anywhere; the resync lane is safe by contrast because it re-hydrates
through `_loadSnapshotFromStreamHydrate(force: true)` (bridge:2469), which does
supply `rawCore` (bridge:1296).

## Hazard B — two watermarks for the push lane, no reconciliation

`MissionOfficeSubscriptionLane._baselineOffset` (subscribe_lane:359, set at 543,
advanced at 661) is the subscribe reply's `watermark`, but the fold is anchored
on the read model's `_sequence`, which came from the OTHER lane. Since the
subscribe reply's office body is never adopted (subscribe_lane:815-820), a
baseline ahead of the held core produces `patch_gap` ->
`_requestResubscribe('fold:gap')` -> only bound is the park cap
(subscribe_lane:725-740). The lane state and the read model's watermark are two
answers to "as of when" with nothing arbitrating them. Also: `onReconnected`
(subscribe_lane:418) has no production caller, so a serve-child respawn under a
surviving session leaves registrations on a dead transport
(subscribe_lane:844-848).

Test-only fold path with no chain and no fence: `missionOfficePatchFoldOnto`
(subscribe_lane:183-212, @visibleForTesting) and
`MissionReadModel.applyPatchFrame` (read_model:840, @visibleForTesting). No
production caller — correct, but two more copies of the two-phase shape.

# 2. Write lanes (mission_office_layout_controller.dart, read fully)

`_flush` arms (controller:1085-1619), all RPC-first with argv reachable ONLY on
the `Unavailable` arm:

| arm | RPC | argv fallback | receipts |
|---|---|---|---|
| actor upsert | `_writer.upsertActor` :1170 (guarded by `serverRevisions` :1169) | `office.actor.upsert` :1251-1264 | UpsertOk -> prediction check :1186-1190, REVISION MISS :1966-1980, KEY DRIFT :1995-2002; Refused -> `_logRpcRefusal` :1911-1923; argv refusal -> `_logRefusal` :1883-1897 |
| actor remove (WV-L1) | `_writer.removeActor` :1363 | `_archiveActor` :1645-1659 | RemoveOk :1368-1381; RemoveAbsent -> `_logRemoveAbsent` :1935-1946 (counted removed, NOT rolledBack); RemoveRefused :1403-1419 |
| surface/folders (WV-L2/L4) | `_writer.updateSurface` :1470, adopts `ack.folders` :1481 | `office.surface.update` :1521-1532, adopts its OWN desiredFolders :1539 | refusal not counted as rpcWrites :1497-1506 |
| resolve conflict | none — capability only, `office.resolve_conflict` :799-811 | n/a | none beyond the action result |
| create | not in `_flush`: `runtime.agent.create` at mission_control_page.dart:2512+, reconciled via `adoptServerWrite` controller:770-783 (page:2621-2640) |

Per-flush receipts: `flush: N upserted, N removed, N refused` :1558-1563;
`write lane: N rpc, N cli (fallback: ...)` :1570-1579 plus counters as STATE via
`missionOfficeWriteLaneProvider` :1587-1596, rendered as the fallback pill
(sync_strip:151-159). Failure status carries typed reasons
(`MissionOfficeSyncFailureReason` :187-202, `_classifyRefusal` :1026-1030).

## BW-L6 held writes vs the projection — held prediction DOES outrank a remote edit, and it is UNBOUNDED

`_holdFlush` :838-860 -> `_stageHeldOverlay` :869-886 writes
`_OverlayEntry(..., pending: true, held: true)` for EVERY actor in
`sync.desired` whose content differs from `serverKeys` (`desired` is the whole
layout, staged wholesale at :693-696) — not just the actor this gesture moved.
In `resolveLayout`:
- :1803-1806 `sync.overlay.removeWhere((key, entry) => !entry.pending && sync.serverKeys[key] == entry.contentKey)` — held entries are pending: true, so NEVER reconciled away by a read;
- :1807-1809 `for (final entry in sync.overlay.entries) effective[entry.key] = entry.value.payload;` — the overlay payload unconditionally overwrites the server row just seeded from `runtime.office.get` at :1779-1782.

So for the whole hold, a remote peer's edit to ANY actor in the local scene is
read, discarded, and rendered as the local prediction, with no receipt at that
site. Held entries are dropped in exactly one place: `_clearHold(sync,
dropHeldOverlay: true)` at :1107, inside a REAL flush (posture ready). On the
terminal arm, `_convertHoldToFailure` :929-950 deliberately keeps them
(:919-928), and `retry()` :786-791 -> `_flush` :1095 re-enters `_holdFlush`
while the posture is still terminal — so Retry re-emits the same banner and the
mask persists INDEFINITELY. Held REMOVALS stage no overlay entry; masked instead
through `requestedRemovals` at :1819-1821 (documented :636-638). No test in
mission_office_boot_hold_test.dart covers a remote change arriving during a hold
(groups at :168, :221, :290, :352, :396, :459).

# 3. Silent-skip / degrade-renders-as-empty still present

- initial_chat_context_dialog.dart:4386 and :4443 — `fetchPromptContextById`
  (bridge:2594-2624) collapses spawn error, timeout(124), non-zero exit and
  undecodable payload into one null (:2617-2622); dialog silently renders the
  evicted context. "runtime evicted this" and "could not reach the runtime"
  render identically. Sibling path IS honest:
  mission_agent_chat_panel.dart:2384-2395 shows an explicit snackbar. The
  c7a8e6043 class, one caller over.
- mission_agent_model_switcher_view_model.dart:885-902 — BARE-token paste path
  skips unconnected lanes (:887) and answers `unrecognized` with "No connected
  provider offers ..." (:901), never naming the 401'd lane that holds the model.
  provider-model form honest (:856-865); search fixed by c7a8e6043.
- mission_control_hermes_visibility.dart:1266-1283 — `catalog` stays {} when
  `readModelsCatalog()` returns null/empty; only the THROW arm records an issue
  (:1281-1282). "no models.dev cache on disk" and "cache present, no matching
  provider" both render as 0 models. Parse-level empties at :620-629 and
  :781-791 are intentional and honest via issues/source/probeFailure.
- Office layout reads honest: degrade return (controller:1731-1745) preceded by
  `missionOfficeRpcStatusProvider` set (:1696-1698); strip renders distinct
  pills per cause (sync_strip:101-159). Degrade return skips overlay projection
  (paints cache, not staged scene); `applyLayout` writes cache from gesture
  (:715) so the gesture survives.
- Chat/roster: mission_agent_instance_picker.dart:722-731 refuses to collapse on
  transiently empty entries.

# 4. Dead code candidates

- `office.actor.restore` FULLY DEAD. Registry harness_capability_registry.dart:252-261,
  argv lowering bridge:4112-4121, asserted only by gateway_manifest_test.dart:143.
  No submit site, no UI (resurrection goes through upsert + ledger removal,
  read_model:1483-1501).
- EVERY `--expect-revision` argv arm unreachable: bridge:4090-4094 (upsert),
  :4105-4109 (remove), :4130-4134 (surface). No submit site passes
  expect_revision (controller:1256-1259, :1653-1656, :1526-1529). Guard lives on
  RPC lane only (:1170).
- `office.actor.upsert`/`office.surface.update`/`office.actor.remove` argv arms
  LIVE but reachable only on Unavailable (controller:1242-1250, :1420-1428,
  :1508-1520) — R#42 evidence-gated fallbacks, `write lane:` counters are the
  deletion criterion (:1134-1144).
- `probeHermesRuntimeHealth` failure-diagnostic only post-BW-L5. Single call
  site provider:670-673, reached only when session==null or bootEvidence()!=ready
  (provider:647-665). Definition mission_control_hermes_installer.dart:2642.
- mission_office_mass_archive_incident_repro_test.dart still tests live code
  (tripwire `missionOfficeMassArchiveTripped` controller:102-108, called
  :1312-1316, asserted test:609-627). CAVEAT: file never overrides
  `missionOfficeRpcWriterProvider`, so all four controller tests exercise the
  ARGV FALLBACK, not the RPC-first path (asserts on
  `_targets(repository, 'office.actor.upsert')` at test:437, :511, :572). A
  regression making the RPC arm always Unavailable leaves the incident repro
  green. Only the {upsert,remove,surface,explicit_removal,commit_on_release}
  lane tests + agent_create lane test override the writer.
- `missionOfficeRpcFlag` (mission_office_rpc_flag.dart:38-46) defaultValue true,
  rolloutPct 100, no killAt. `gateClosed` degrade (subscribe_lane:448-455)
  unreachable outside `--dart-define=MISSION_OFFICE_RPC=false`. Kill switch, not
  dead — but the gate arm has no default-build coverage.

# 5. isSettled / writesInFlight / retire condition

`_WorkspaceSync.settled` (controller:537-538) — migration seed only (:1752):
  overlay.isEmpty && removed.isEmpty && timer == null && !flushing
`_WorkspaceSync.writesInFlight` (controller:575-576):
  timer != null || flushing || overlay.values.any((e) => e.pending)
`isSettled` (controller:639-640): !(writesInFlight ?? false)

`holding` deliberately NOT a term (:505-519); hold represented only through
pending overlay entries (:624-638). `requestedRemovals` NOT a term (:530-536),
so a held ARCHIVE can leave isSettled true — canvas stays right by a different
mechanism (:1819-1821).

Page retire condition, mission_control_page.dart:3010-3026: workspaceId != null
&& !_hasPendingOfficeSave && isSettled(workspaceId), then fresh != null &&
!freshLoading -> override retired. `_hasPendingOfficeSave` =
`_officeLayoutPendingSave != null` (page:427), bounded by the 220ms debounce and
cleared before the await (page:404-427). During a boot hold the override never
retires (held pending entries) — and during a terminal hold a remote edit is
suppressed at BOTH layers with no expiry and no receipt.

Boundary respected: no analysis of snapshot hydrate cost, snapshot_build,
_BUILD_COALESCE, boot receipt spans, or the projection-drops / parity-warning
HUD chips. Adjacency: the cached-disk lane (bridge:1186-1209) never enters the
read model, precedence at provider:1114-1123; `_recordDrop`
(read_model:291-294) is the drop chokepoint the HUD chips consume, forwarded at
bridge:769-776.
