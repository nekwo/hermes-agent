# Field notes — realm sync outbound drift (hermes lane)

Running record for the hermes slices of
`archive/realm-sync-outbound-drift-and-live-checks.md`. Append, never rewrite.

## S1 — office store drift in the status envelope (2026-08-29)

Landed: `_office_store_drift(realm_id, workspaces)` in `agent_runtime/realm_sync.py`
plus three status tests in `tests/agent_runtime/test_realm_sync.py`.

### What the code does

Mirrors `_board_store_drift` exactly: pure, read-only, no git/network. Compares
current `OfficeStore` hashes (`office_models.office_content_hash`) against
`office_sync.read_office_baseline(realm_id)`, keyed with the sync module's own
`_surface_key` / `_actor_key` helpers (imported rather than respelled — the
publish lane writes those same keys, and a second spelling of a key format is
how the two sides silently disagree).

Counters:

- `offices_changed` — surface hash differs from its baseline row (or has no
  baseline row at all).
- `actors_changed` — active actor whose hash differs from a KNOWN baseline row.
- `actors_added` — active actor with no baseline row.
- `actors_removed` — baseline actor key for that workspace with no active actor
  locally (archived or removed since the last publish). This is the counter the
  measured field defect needed.

### Edge cases, and why they under-count on purpose

- **Unreadable actor listing** (`scan_actors(...).unreadable` non-empty): the
  workspace's ENTIRE actor accounting is skipped — no adds, no changes, no
  removals. `scan_actors` is used rather than `list_actors` precisely because
  the thin view drops the files it could not decode; diffing that short list
  against the baseline would report N removals for actors whose files merely
  would not open here. Same refusal discipline as
  `update_office_baseline_after_sync`: prefer a stale-looking zero over a
  delete-shaped lie.
- **Missing/unreadable surface**: only the `offices_changed` row for that
  workspace is skipped; the actor accounting still runs when the listing reads.
- **No office directory at all**: the workspace contributes nothing (plan's
  literal instruction). A never-materialised office is not a removal.
- Both scan/read calls are wrapped so a store-level raise cannot fail a status
  call — status is a read path and must stay answerable offline.

### Findings while implementing

1. `store_drift` had **no consumers in this repo** other than `realm_sync.py`
   and its tests (`grep unpublished_changes` → 2 files), so adding the `office`
   family is purely additive; `_any_store_drift` generalised over families
   already and was not touched.
2. Baseline keys are minted from the office **directory token**
   (`update_office_baseline_after_sync` is called with names resolved from
   `artifact.source.parent`), while the drift helper keys from `Workspace.id`.
   `paths.safe_path_token` is idempotent for the `ws_*` ids the store mints, so
   the two agree today. A workspace id needing sanitisation would make the
   drift read the wrong keys and report false adds/removals. Not fixed here
   (out of S1 scope, and no such id can be created by the current verbs) —
   filed as a latent row.
3. Archiving an actor through `OfficeStore.remove_actor` moves the key into the
   surface's `archived_actor_keys` resurrection guard, so the surface hash
   changes too: the removal shows up as `actors_removed >= 1` **and**
   `offices_changed == 1`. That is correct (both files really do need
   publishing) and the launcher rows in S2 should expect the pair, not treat
   `offices_changed` as noise.

### Verification

`python -m pytest tests/agent_runtime/test_realm_sync.py -q` → **60 passed**
(~61s). Neighbours re-run for the shared baseline machinery:
`python -m pytest tests/agent_runtime/test_office_sync.py tests/agent_runtime/test_board_sync.py -q`
→ **51 passed**.

Not run: any live realm/GitHub check — S1 is a pure local-hash slice, and the
field reproduction belongs to the operator loop in S4.
