# Planned — widen the core cache's fingerprint input closure

**Status:** not implemented. **Domain:** runtime data and shapes.
**Opened:** 2026-08-22, from live receipts.

## The problem

`agent_runtime/core_cache.py` validates a persisted snapshot core by re-stat'ing
every input the build read and comparing `(path, mtime_ns, size)` triples. When
consecutive write-backs each produce a key that disagrees with the one before
it, no later process can ever be served the cache: the lane costs a write per
build and buys nothing. That state emits
`snapshot_core_cache never_converged` (`core_cache.py:322`).

It is currently the steady state on the operator's machine.

## Evidence

`X:\Eternia\.hermes\profiles\base\logs\agent.log`, 10 firings between
2026-08-20 18:21 and 2026-08-22 13:42:

| When | `diff_scope` | `changed` | Named paths |
| --- | --- | --- | --- |
| 08-20 18:21 | `every_pass` | 60 | `realm_sync/<realm>/.git/index`, `.git/logs/HEAD`, `.git/objects/pack/*.pack`, … |
| 08-20 18:30 | `last_pair` | 2 | `events_archive/events.81417412.jsonl`, `profiles/alice/config.yaml` |
| 08-20 18:38 | `last_pair` | 2 | same pair |
| 08-20 20:37 | `every_pass` | 2 | live events slice, `persona_instances/personainst_neko_supervisor_agent_f6f7a51b.json` |
| 08-21 13:51 | `last_pair` | 1 | `profiles/alice/config.yaml` |
| 08-21 17:18 | `every_pass` | 1 | `profiles/base/state.db` |
| 08-21 17:22 | `every_pass` | 1 | `profiles/base/state.db-wal` |
| 08-21 22:00 | `last_pair` | 1 | live events slice |
| 08-22 10:48 | `last_pair` | 1 | live events slice |
| 08-22 13:42 | `every_pass` | 1 | `profiles/base/state.db-wal` |

Receipt format, verbatim:

```
snapshot_core_cache never_converged builds=3 diff_scope=every_pass changed=1
  diff=X:\Eternia\.hermes\profiles\base\state.db-wal — 3 consecutive write-backs
  each wrote a key that disagreed with the one before it …
```

Demote histogram over the same log: 57 `build_stamp_mismatch`, 24
`fingerprint_mismatch`, 8 `home_mismatch`. Serve outcomes: 50 `core_source=cache`,
41 `core_source=cache stale=true`, 89 `core_source=rebuilt`.

## Reading the evidence

The module's own census rule (`core_cache.py:181`) is the interpretation
authority: `diff_scope=every_pass` means the inputs oscillate — self-perturbation,
the defect worth acting on. `diff_scope=last_pair` means the store is simply
moving and the receipt is true without naming a defect.

Five of the ten firings are `every_pass` (08-20 18:21, 08-20 20:37, 08-21 17:18,
08-21 17:22, 08-22 13:42). Their named paths split two ways:

- **Runtime-authored** — `state.db`, `state.db-wal`, `persona_instances/*.json`.
  The runtime writes these while a build stats them. This is the actionable
  class.
- **`realm_sync/<realm>/.git/**`** — a git worktree the runtime syncs into,
  under the store root. 60 changed entries in one pass. Git's own index, reflog
  and packfiles are not runtime state the projection reads; they are inside the
  fingerprint walk because the walk is directory-level over the store root.

`state.db-wal` is the subtlest: `core_cache.py:856` already masks a frameless
WAL to `(path, 0, 0)` so that *reading* the database does not look like writing
it. The 08-21 17:22 and 08-22 13:42 firings say the mask is not sufficient for
the way this runtime commits during a build.

## The sanctioned direction

The module docstring states it and this plan must not deviate:

> If a shadow receipt ever shows divergence, the fix is WIDENING the stat set —
> never trusting the cache harder.

Widening means making the closure a function of the store rather than of the
instant, over the *named* inputs. Two candidate moves, neither yet ruled:

1. **Exclude `realm_sync/*/.git/`** from the walk. The projection reads
   `realm_sync_state/<realm>.json`, not the worktree's git internals — so the
   `.git` subtree is arguably outside the input closure entirely, which makes
   this an exclusion rather than a widening. Verify against every reader before
   ruling: an excluded input that *is* read is exactly the failure class
   (unlabeled stale served as authoritative) the lane exists to end.
2. **Extend the SQLite mask** so a WAL that gains and loses frames within a
   build keys identically. `_wal_without_frames_is_content_free`
   (`core_cache.py:856`) is the existing precedent for a content-free WAL state.

## Reader audit — DONE (2026-08-22, read-only evidence lane)

The gate's audit obligation is discharged; the findings reshape the plan.

1. **`realm_sync/*/.git/` has zero build readers** — every section builder
   resolves realm-sync state to `realm_sync_state/<realm>.json`
   (`realm_sync.py:1870`; design intent written at `snapshot.py:2493-2496`:
   the build "must never shell out to git"). BUT the build DOES read two
   siblings one directory up: `realm_sync/<realm>/board_baseline.json`
   (`snapshot.py:1610` → `paths.py:129`) and `office_baseline.json`
   (`snapshot.py:1837` → `paths.py:214`). The exclusion must target the
   literal `.git` name only — and note `_sync_repo_path` keys worktrees by
   SERVER token while baselines key by realm id, so a realm-id-keyed skip
   would be wrong.
2. **The walk excludes top-level names only** (`_walk_tree` `exclude_top`,
   `core_cache.py:830-832`; doctrine pinned at `:638-646` with a test).
   `.git` sits at depth ≥2 — a nested-exclusion mechanism is new code, and
   the doctrine block plus its pinning test must be amended in the same
   commit.
3. **The build is not a pure reader — five proven self-perturbation writes:**
   (a) `snapshot.py:871` → `ensure_for_personas` recreates any
   missing/UNREADABLE persona-instance row on EVERY build
   (`persona_assignments.py:441-454` — the bare `except Exception` means a
   corrupt row re-mints a file write + a `persona_instance.created` event
   per pass, a non-converging trigger); (b) drift rewrites (`:463-465`);
   (c) stale-binding resets stamping fresh `updated_at` (`:2219-2235`);
   (d) the build's own SessionDB close drains token deltas and runs a
   TRUNCATE WAL checkpoint (`snapshot.py:2350-2356` →
   `hermes_state.py:2542-2548`) — moving `state.db`'s main-file triple with
   zero logical change, the 08-21 17:18 shape; (e) DB opens during the build
   flip the stream scope fingerprint, appending synthetic `state.reconciled`
   to the live events slice (`stream.py:1359-1368`; measured 96.9% of all
   events at 9s median spacing, `stream.py:1240-1249`) — a genuine
   build→event→key-flip→demote→build feedback loop.
4. **WAL mask soundness bounds:** collapsing two different UNCHECKPOINTED
   frame sets is forbidden (`core_cache.py:696-698` — hides a commit
   invisible in the main file). Collapsing frames-present with
   post-checkpoint-frameless is sound ONLY because the checkpoint moves
   `state.db`'s own triple. Content-keying the WAL by raw-read frame digest
   (precedent: `_config_input_is_content_keyed`, `:929`) is sound in the
   invalidation direction — but **no WAL mask alone converges this store
   while (d) runs a TRUNCATE checkpoint on every build's close**.
5. `agent_create_reservations/` is unread by the build and low-churn — leave
   it in the walk (excluding it buys nothing and costs an audit obligation).

## Staged implementation (coordinator, 2026-08-22)

- **IC-1 — nested `.git` exclusion.** New nested-exclusion support in
  `_walk_tree` scoped to the literal `.git` name under `realm_sync/*`;
  amend the top-level-only doctrine (`:638-646`) and its pinning test in the
  same commit; add the obligation comment (pattern `:545-552`). Kills the
  60-entry class from the 08-20 18:21 firing. Mechanical.
- **IC-2 — key the write-back on post-build reality.** `write_back` persists
  the CONSULT-time `pre_build_fingerprint` (`core_cache.py:2892-2902`), so
  the build's own writes (3a-3e) guarantee the persisted key disagrees with
  the next consult's stat — the exact `never_converged` mechanism. Re-stat
  the self-perturbed inputs (DB triples, live slice, persona_instances) at
  write-back time, AFTER the SessionDB close, so the persisted key matches
  what the next consult will see. The equivalence golden
  (`test_core_fingerprint_cache.py:1501`) is the authority that this does
  not serve stale: the re-stat happens only on the write path, never widens
  what a consult accepts. Needs a design note answering `core_cache.py:20-40`
  in writing.
- **IC-3 — stop the corrupt-row rebuild loop.** Narrow
  `persona_assignments.py:441`'s bare `except Exception`: an unreadable row
  should surface a warning receipt and re-mint ONCE, not silently re-mint
  every build.
- **IC-4 — WAL raw-content keying** for the mtime-moved-bytes-identical
  class, after IC-2 (measure first — IC-2 may make it unnecessary).
- **Deliberately out of scope:** suppressing the build-close TRUNCATE
  checkpoint (a SessionDB lifecycle change owned elsewhere), and the
  `state.reconciled` feedback loop's stream half (3e) — record its census
  numbers here, but the fix belongs to the stream scope-fingerprint lane.

## The gate to open this

- ~~Every candidate exclusion has a named reader audit proving nothing in
  `build_snapshot()` reads it.~~ DONE above for `.git`; any FURTHER exclusion
  re-arms this obligation.
- The equivalence golden stays green —
  `test_the_cache_served_core_equals_the_rebuilt_core_field_for_field`
  (`tests/agent_runtime/test_core_fingerprint_cache.py:1501`, filed under
  "7. The equivalence golden — THE authority guard").
- A shadow-validation window over the live store shows zero divergence.
- `never_converged` with `diff_scope=every_pass` drops to zero over a week of
  operator boots, measured by `scripts/core_cache_demote_census.py`.

## Why it matters

Cold boot currently costs 11,235 ms of re-projection
(`snapshot_build_core role=led caller=prewarm generation=1 build_ms=11235`,
2026-08-22 15:46). The cache exists to replace that reconstruction with
validation. While it never converges, every boot pays the full build *and* a
write-back.
