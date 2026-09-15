# Realm Sync — Outbound Drift Accounting + Live Checks (staged plan)

**Status, corrected 2026-08-30 on archiving: ~~PLANNED (S1/S2/S3 dispatched
2026-08-29)~~ — S1, S2 and S3 all LANDED 2026-08-29.** hermes S1 `19f3aee24`
(`_office_store_drift` in `agent_runtime/realm_sync.py`, in the status
envelope); launcher S2 and S3 landed with their verification logs in
`EterniaLauncher/Launcher_Brain/20 — Active Initiatives/realm-sync-outbound-drift-notes-2026-08-29.md`.
**S4 is the open remainder and it is not agent work** — it is the operator's
end-to-end live verification, still un-taken, and the launcher notes' own gap 1
says everything below S1 is fixture-driven and has never been proven against a
real `test realm` status run. Filed as a row on the launcher's Mission Control
queue rather than kept here. **One decision inside S2 has since been
superseded** ("N desks added" → "N placements added"; the counters count actors,
not furniture) — see the SUPERSEDED section of those launcher notes.
Author: Fable (plan), Opus agents (implementation)
Repos: hermes-agent (S1), EterniaLauncher (S2, S3)

## Problem (measured 2026-08-29)

Operator deleted every actor in workspace `ws_testv4_afb811` of realm
`cf6d244d-7cfa-4fa5-bab9-1401c8493b23` ("test realm", GitHub
`nekwo/testtest`). The realm sheet kept saying **In sync** with no prompt to
publish. Ground truth at time of report:

- Remote `main` = local `main` = `03e1632` (last publish, Aug 20). The remote
  still holds all four actor files. Nothing inbound to detect.
- Local `office/ws_testv4_afb811/actors/` is EMPTY — all four actor files moved
  to `archive/` on Aug 29. The change is **outbound**: local store drifted from
  the published baseline.
- `realm_sync_status`'s `store_drift` counts **boards only**
  (`store_drift = {"boards": _board_store_drift(...)}` in
  `agent_runtime/realm_sync.py`). The office/actor family has NO store-drift
  accounting, so `unpublished_changes` stays false and the launcher chip has
  nothing to mark.
- Separately: the status sidecar (`realm_sync_state/<realm>.json`) was last
  written Aug 20 — "Check now" failures on the credential-brokered path are
  quiet (`lastError` only; the chip keeps rendering the stale state).

Already landed (context, do not redo): hermes `47c888529a` — status/publish now
`git fetch` first (`_refresh_remote_tracking`), so INBOUND remote changes mark
`behind`; envelope gained additive `remote_checked` / `remote_check_error`.

## Frozen contract (S1 output, launcher consumes)

`hermes harness realm sync status <realm> --json` envelope, additive only:

```json
{
  "store_drift": {
    "boards":  {"boards_changed": 0, "cards_changed": 0, "cards_added": 0, "cards_removed": 0},
    "office":  {"offices_changed": 0, "actors_changed": 0, "actors_added": 0, "actors_removed": 1}
  },
  "unpublished_changes": true,
  "remote_checked": true,
  "remote_check_error": null
}
```

- `store_drift.office` is new; all four counters are non-negative ints,
  always present when the block is present.
- `unpublished_changes` already ORs every family via `_any_store_drift` — no
  shape change, it just becomes true for office drift too.
- Absent-tolerance unchanged: a launcher reading an old backend sees no
  `office` block and must behave as before (null drift, no marker).

## S1 — hermes: office store drift in the status envelope

File: `agent_runtime/realm_sync.py`.

1. Add `_office_store_drift(realm_id, workspaces)` mirroring
   `_board_store_drift` directly above it (same docstring discipline: pure,
   read-only, no git/network — office plan §10 simplicity budget):
   - `from .office_store import OfficeStore`, `from .office_sync import
     read_office_baseline`, `from . import office_models`.
   - Baseline keys are `f"{workspace_id}:office"` (surface) and
     `f"{workspace_id}:actor:{actor_key}"` (actors) — see
     `office_sync._surface_key` / `_actor_key`. Import those two helpers
     rather than respelling the formats (they are module-private by
     underscore only; import is fine within the package, same as
     `skill_promotion._archive_package` is imported today).
   - For each workspace in `workspaces` (the realm's workspaces, same input
     `_board_store_drift` takes): read the surface via
     `OfficeStore().get_surface(ws.id)` (tolerate a missing/unreadable
     surface: skip that workspace's surface row, still count its baseline
     actors as removed only if the actor listing is readable — when in doubt
     prefer under-counting to a delete-shaped lie; a workspace with no office
     dir contributes nothing). Compare
     `office_models.office_content_hash(surface)` vs baseline surface key →
     `offices_changed`.
   - List current ACTIVE actors (`OfficeStore` — use the same listing the
     publish scan uses, `scan_actors`, and skip the workspace's actor
     accounting entirely when `scan.unreadable` is non-empty; an unreadable
     dir must not masquerade as N removals). Per actor:
     no baseline entry → `actors_added`; hash differs → `actors_changed`.
     Baseline actor keys for that workspace with no current actor →
     `actors_removed`.
   - Return `{"offices_changed": int, "actors_changed": int,
     "actors_added": int, "actors_removed": int}`.
2. In `realm_sync_status`, extend
   `store_drift = {"boards": board_drift, "office": _office_store_drift(realm.id, workspaces)}`.
   `_any_store_drift` needs no change.
3. Tests (`tests/agent_runtime/test_realm_sync.py`, follow the existing
   status-test idioms; there are board-drift precedents — find them with
   `grep -n "store_drift" tests/agent_runtime/`):
   - Publish a realm with an office actor (see existing office publish tests
     in `tests/agent_runtime/test_board_sync.py` /
     `test_realm_sync.py` for fixture shape — office fixtures live where
     `office_store`/`office_sync` tests build them; reuse their helpers, do
     not invent a new fixture dialect). After publish: status reports all-zero
     office drift and `unpublished_changes` false.
   - Archive/remove the actor locally through `OfficeStore`'s own verb (not
     raw file deletion) → status reports `actors_removed >= 1` and
     `unpublished_changes` true, while git `state` stays `in_sync`.
   - A realm with no baseline (never published, server-bound) counts current
     actors as added — assert honest nonzero, mirroring the board rule.
4. Focused run:
   `python -m pytest tests/agent_runtime/test_realm_sync.py -q` (and any
   office-sync test file touched). Commit exactly the two files.

## S2 — launcher: render office drift + loud check failures

Files: `lib/features/mission_control/sync/realm_sync_models.dart`,
`.../widgets/realm_sync_detail_sheet.dart`, `.../widgets/realm_sync_chip.dart`,
`.../realm_sync_service.dart`. Tests:
`test/features/mission_control/realm_sync_widgets_test.dart` and the models'
test neighbors.

1. Models: add `OfficeStoreDrift` beside `BoardStoreDrift`
   (`fromStoreDriftJson` reads `store_drift.office`; null-tolerant for old
   backends, exactly like the boards block). Extend
   `RealmSyncStatusReport` with it. Parse the already-landed
   `remote_checked` / `remote_check_error` fields (bool?, String?) —
   absent-tolerant.
2. Detail sheet: the existing drift strip (rendered
   `when unpublishedChanges && !drift.isEmpty`, see
   `realm_sync_detail_sheet.dart:185` and the board-drift rows near `:1176`)
   gains office rows: "N desk(s) removed / added / changed" (+ "office layout
   changed" for `offices_changed`). Wording follows the board rows' grammar.
3. Chip: `realm_sync_chip.dart:93` already flips the chip when
   `unpublishedChanges && state == inSync` — verify it renders a
   distinguishable "changes to publish" state and add/extend a widget test
   with an office-drift fixture payload.
4. Loud check failures: `refreshStatus` failure currently only sets
   `entry.lastError` — the sheet keeps showing the stale state pill.
   - Sheet: when `entry.lastError != null` and the last refresh failed, the
     state pill must not claim "In sync"; render a warning variant
     ("Check failed") with the error reachable (tooltip or inline row).
   - When the envelope has `remote_checked == false`, render a quiet inline
     note ("Couldn't reach the realm remote — showing last known state",
     include `remote_check_error` code) instead of silently presenting local
     state as truth.
5. Focused runs: `flutter analyze lib/features/mission_control/sync` and
   `flutter test test/features/mission_control/realm_sync_widgets_test.dart`
   (plus any model test file touched). Commit exactly the touched files.

Fixture payloads for tests come from the frozen contract above — do not
reverse-engineer the backend.

## S3 — launcher: sync check on start

File: `lib/features/mission_control/sync/realm_sync_service.dart` (+ its test
file `test/features/mission_control/realm_sync_*_test.dart` neighborhood).

1. When the service first materializes its realm roster (the same place
   entries are seeded — where `_entries[realmId]` rows are created from the
   snapshot/grants), schedule ONE startup `refreshStatus` per server-bound,
   sync-capable realm through the existing `_scheduleStatusRefresh` debounce,
   staggered (reuse `_statusDebounce`; add a small per-realm stagger so N
   realms do not spawn N simultaneous CLI+credential round-trips).
2. Guards: never for local realms (no doomed round-trips — house rule "no
   fake affordances"), never while `entry.isBusy`, at most once per service
   lifetime per realm (a `_startupChecked` set), and skip when the personal
   channel is not yet authenticated if the credential broker requires it —
   in that case run it on the first successful channel attach instead.
3. Test: fake bridge/channel seams already exist in the service tests —
   assert that constructing the service with two server-bound realms issues
   exactly one status invocation each after startup settle, and zero for a
   local realm.
4. Same focused analyze/test commands as S2. Commit exactly the touched files.

With S3 + the landed fetch-first fix, a GitHub-side change (including the
web-UI delete scenario) is detected on every launcher start and every
"Check now", and flips the realm to `behind` → Prompt banner.

## S4 — publish → notify connected clients (verify, operator loop)

Not agent work; verified live after S1–S3 land. The pipeline already exists on
paper: member publishes → broker emits `realm.sync.published` on the personal
channel → `realm_sync_service._onPersonalEvent` → debounced `refreshStatus` →
(fetch-first) status sees `behind` → update-policy banner. Verify end-to-end
with two installs; file gaps as their own rows. Out-of-scope until an operator
ruling: server-side webhook for raw git pushes that bypass hermes, and a
periodic re-check timer while the launcher runs (interval + default gating are
operator decisions).

## Discipline (both agents)

- harness-dev-delivery contract applies: narrow patches, focused self-tests,
  commit exactly your slice, report command + exit status honestly, no push.
- Field notes: each agent keeps a running-record note in ITS repo —
  hermes: `docs/agent-runtime-harness/archive/field-notes-realm-sync-outbound-drift.md`;
  launcher: `Launcher_Brain/20 — Active Initiatives/realm-sync-outbound-drift-notes-2026-08-29.md`.
  Append as you go; the note is part of the commit.
- The two agents work in DIFFERENT repos (separate git indexes) — safe in
  parallel. Do not touch the other repo.
