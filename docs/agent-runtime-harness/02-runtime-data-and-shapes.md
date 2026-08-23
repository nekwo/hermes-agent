# 02 — Runtime Data and Shapes

Where runtime state lives, what shape it takes, and which artifact is authority
for which question. Three storage families answer three different questions: an
append-only **event log** that owns ordering, a tree of **atomic JSON stores**
that own durable entity state, and per-profile **SQLite** that owns chat
sessions. Everything a surface reads is a *projection* over those three — the
snapshot core — and every projection is rebuilt, never incrementally maintained.
Chat is the only lane; the goal/task mission lane was removed 2026-07-30.

---

## The store root

`paths.store_root()` (`agent_runtime/paths.py:8`) resolves the runtime root —
live, `X:\Eternia\.hermes\agent-runtime`. Every durable JSON write in this tree
goes through `utils.atomic_json_write` (stage a temp file, `os.replace` it into
position). That discipline is what makes the stat fingerprint below sound: a
rename always moves the target's mtime, even for a byte-identical rewrite.

Directories present in the live root, with the module that owns each:

| Path | Owner | Shape |
| --- | --- | --- |
| `events.jsonl` | `agent_runtime/events.py`, `paths.py:240` | the base-0 slice — the live file until the first rotation, **sealed** after it (live: `end_offset` 81417412) |
| `events_archive/` | `agent_runtime/event_rotation.py` | where every post-rotation slice is minted (`:258`) — including the **live, actively-appended** one (live: `events.81417412.jsonl`); sealed slices here are immutable and offset-load-bearing |
| `events_manifest.json` | `event_rotation.manifest_path()` | slice table (below) |
| `persona_instances/` + `_archive/` | `paths.py:20,24` | one `personainst_*.json` per instance |
| `persona_assignments/` + `_archive/` | `paths.py:37,41` | persona↔channel bindings |
| `persona_chat_mint_receipts/` | `paths.py:45` | durable idempotency receipts for server-minted chat roots |
| `persona_chat_leases/`, `persona_chat_clarify_tickets/` | `persona_chat_continuity.py:786,1236` | per-chat leases and clarify tickets |
| `mission_chat_turns/` + `_archive/` | `mission_chat_turns.py:37` | one `<safe_session_key>.json` + `.lock` per chat |
| `mission_chat_steer/` | `mission_chat_steer.py:328` | per-session steer drops |
| `tool_turn_context/`, `queued_skills/` | `tool_turn_history.py:132`, `queued_skills.py:16` | per-turn tool context; skill inbox |
| `prompt_observability/` | `paths.py:306` | one `ctx_<id>.json` per captured prompt context |
| `prompt_observability_catalogs/` | `paths.py:310` | content-addressed `<hash>.json` skill catalogs, written iff absent |
| `prompt_observability_index.json` | `paths.py:324` | latest-pointer cache; **never authority** — a corrupt index falls back to a directory scan |
| `agent_create_reservations/` | `paths.py:54` | recorded-progress receipts for `runtime.agent.create` |
| `boards/`, `office/`, `workspaces/`, `realms/`, `agents/` | `paths.py:81,132,73,77,228` | Mission Board / Office / topology entities |
| `flow_graphs/` | `checkpoint.py:62` | checkpoint flow graphs |
| `realm_sync/`, `realm_sync_state/` | `realm_sync.py:1871` | per-realm git worktrees + sync state |
| `serve_read_model/` | `core_cache.py:244` | the persisted snapshot core (below) |
| `serve_instances/` | `serve_registry.py:119` | one `<pid>.json` per live serve |
| `deleted_archive/`, `migration_backups/`, `wt_reaped_patches/`, `locks/` | `paths.py:302`, `default_scope.py:552`, `delivery_directive.py:65` | archive-never-delete and lock trees |

`paths.py` also declares `runs/`, `runtime_instances/`, `incidents/` and
`prompt_observability_archive/`. None exist in the live root — they are created
on first write, and a store with no such write has no directory.

One more path helper resolves to a file that can no longer exist:
`paths.snapshot_path()` → `snapshot.json` (`paths.py:275`). Its writer was
deleted at Stage 6 (2026-08-22) with the read-model lane, so the helper now
answers *where a legacy copy would be* rather than where a live file is. It is
kept deliberately: without it, a store that ran `harness snapshot` before the cut
has an orphan nothing in the tree can name. `read_model.db` was the other such
file and has no helper at all any more — `ReadModel` is deleted.

### mission_chat_turns

One file per chat session, `mission_chat_turns/<safe_session_key>.json`, holding
that session's `{client_message_id: record}` map, plus a co-located
`.lock` (`mission_chat_turns.py:24-36`). Concurrent turns in *different* chats
never contend. The filename is a sanitized 80-char prefix plus a 12-char sha256
suffix, keeping the total under the Windows `MAX_PATH` budget
(`mission_chat_turns.py:47-52`). The pre-2026-07-17 single-file monolith is
split once on first read/write and renamed to `mission_chat_turns.legacy.json`,
never deleted — that file is still on disk live. Retention is
`_RETENTION_MAX_TURNS_PER_SESSION = 100` turns per session (`:72`, applied
`:800-806` inside the per-session lock), which must stay comfortably above the
projection's displayable tail — `MAX_PERSONA_CHAT_MESSAGE_TAIL = 40` in
`persona_chat_history.py` — or a displayable agent row loses its
`turn_elements` (`:67-71`); plus an opportunistic session-file GC under
`mission_chat_turns.gc.lock`. `_MAX_ELEMENTS = 80` (`:65`) is a DIFFERENT bound:
it caps the `elements` list inside ONE turn record (`_safe_elements`, `:1272`),
not the turns a session keeps. Live counts (2026-08-22): 50
session files, 234 locks, beside 18 persona instances and 19 prompt contexts.

---

## The event log is the ordering authority

`event_offset` is a **byte position**, and every watermark in the runtime is one.
`parity.events_watermark` (`agent_runtime/parity.py:233`) reads it via `stat`, in
O(1), without scanning the log.

**An unreadable log yields `None`, never `0`** (`parity.py:244-256`). Zero is the
single most damaging value the field can carry: every reader treats it as a real
position, so a swallowed stat error replays the entire log as fresh activity.

`read_model.snapshot_watermark` used to restate this rule on the write side and
was deleted with its module at Stage 6 (2026-08-22). The rule did not go with it
— it is a property of the producer, and `parity.events_watermark`'s docstring is
now its only home, which is why that docstring states the argument in full rather
than pointing at a neighbour.

### Rotation preserves logical offsets

`agent_runtime/event_rotation.py` seals slices in place rather than renaming or
truncating, because on Windows `os.replace` of a file with an open handle raises
`PermissionError` and a paused `iter_from_offset` generator holds exactly such a
handle across yields (`event_rotation.py:33-44`).

    logical_offset(x) = slice.start_offset + byte_position_within(slice)

The manifest lists sealed slices as `(file, start_offset, end_offset)` plus one
open-ended live slice carrying `base_offset` (`SliceRef`,
`event_rotation.py:76-86`). `offset_reads()` (`:190`) resolves which slice a
logical offset lives in and seeks there; `log_end_offset()` (`:181`) is
`live.start_offset + live_size`. Every existing reader keeps working unmodified
and stored watermarks resolve exactly as before. With no rotation the manifest is
absent, the single live slice is `events.jsonl` at base 0, and logical == byte
offset. Live cap: `DEFAULT_ROTATION_CAP_BYTES = 16 MiB` (`event_rotation.py:63`),
overridable by `event_log.rotation_cap_bytes` or the env equivalent. Live
manifest, verbatim:

```json
{"version":1,"slices":[{"file":"events.jsonl","start_offset":0,"end_offset":81417412}],
 "live":{"file":"events_archive/events.81417412.jsonl","base_offset":81417412}}
```

`CachedEventLog` (`events.py:323`) reads the log once per build and serves every
`for_task`/`for_session`/`tail` from the cached lines, concatenating slices
oldest-first so the flat cumulative byte position *is* the logical offset. It is
a point-in-time view by design: appends made during a build are not reflected,
and the next builder observes the changed size/mtime and loads a fresh view.

---

## SessionDB — `state.db`

`hermes_state.SessionDB` owns chat sessions and lives at
`get_hermes_home() / "state.db"` (`hermes_state.py:246`) — **per profile**,
not in the store root. Live: 10 of the 11 profile directories under
`.hermes/profiles/` carry one — `profiles/unbounded/` has none — plus a root
`.hermes/state.db`. The path is resolved at
call time, not at import: freezing it at import let a test that only set
`HERMES_HOME` write into the developer's live profile
(`hermes_state.py:256-264`).

Journal mode is `WAL` by default, resolved by `resolve_journal_mode()`
(`hermes_state.py:572`) from `database.journal_mode` in `config.yaml`.
`apply_wal_with_fallback` (`:618`) falls back to `journal_mode=DELETE` when the
filesystem cannot support WAL's shared-memory and byte-range locking (NFS,
SMB/CIFS, some FUSE, WSL1) — and it treats a `PRAGMA journal_mode=WAL` that
returns a non-WAL mode *without raising* as a refusal (`:711-720`), because that
PRAGMA is a query-that-sets. The snapshot's `persona_chat` section reads this
database through `chat_session_scope.open_chat_session_db` (`snapshot.py:2337`).

---

## The snapshot core

A **core** is one dict: the whole read model for the runtime, built by
`build_snapshot()` (`snapshot.py:514`) from the three storage families above.
Top-level sections include `summary`, `runtime_default`, `runtime_config`,
`migration`, `prompt_observability`, `repo_scopes`, `workspaces`, `realms`,
`boards` / `boards_unreadable`, `offices` / `offices_unreadable`,
`running_work`, `persona_chat`, and `parity`.

Seven sections are timed and land in `parity.sections_ms`: `events`,
`agents_readiness`, `prompt_observability`, `boards_offices`, `running_work`,
`persona_chat`, `parity` (`snapshot.py:773-902,1030,1097`).

`parity` is the frame's self-describing provenance envelope
(`snapshot.py:1372-1400`), keyed in build order: `contract_version`,
`generated_at`, `redaction_mode`, `redaction_observed`, `build_ms`,
`sections_ms`, `snapshot_bytes`, `event_log_bytes`, `projection_age_ms`,
`watermark`, `runtime_root`, `resolution`, `profile`, `capabilities`,
`freshness`, `completeness`, `drops`, `warnings` — plus `core_source` and
`frame_source`, stamped afterwards by the core cache and the read-model
resolver.

**Generations and coalescing.** Builds inside a process are numbered; concurrent
callers coalesce. The coalescer is deliberately strict — a caller arriving while
a build runs waits for the *next* build, never the in-flight one, because an
in-flight build began earlier and may miss writes the caller already observed.
`accept_inflight=True` opts out, and both non-test callers are the same
boot-hydrate lane — `hydrate_frame` (`stream.py:270`) and the `stream_frames`
boot job that drives it (`:979`) — because the hydrate's payload carries its own
watermark and the stream tails from exactly that offset
(`snapshot.py:522-536`). Roles:
`BUILD_ROLE_LED` / `RODE` / `SHARED_NEXT` / `CACHE` / `REUSED`
(`snapshot.py:283-306`).

`agent_runtime/demote_core_reuse.py` adds a second, *sequential* saving: one
demote build's core reused by the next demote build **at the same event offset**.
Its claim is about position, not time — which is what makes it safe where riding
an in-flight build is not.

### The build receipt

One line per **actual** build, emitted by the caller that ran it
(`_log_snapshot_build_core`, `snapshot.py:373`); every other line about a build
is a *wait*. Format, pinned:

```
snapshot_build_core role=%s caller=%s generation=%s build_ms=%s offset=%s sections_top=%s pid=%d
```

`pid` rides last deliberately — it is the join key between a launcher boot
receipt and a serve's `agent.log`. `sections_top` is the three most expensive
sections as `name:ms`, sorted cost-descending then by name, so consecutive boots
of the same shape print the same string and a diff means the shape moved
(`snapshot.py:353`). A sibling receipt splits the misleading `agents_readiness`
number into its two halves: `snapshot_agents_readiness walk_ms=%d
tool_visibility_ms=%d pid=%d` (`snapshot.py:432`). The often-quoted numbers for
that split — 4,001 ms first build (3,054 tool visibility / 947 walk) against
183 ms steady state (36 / 146) — are the **bench from `25cd488d33`'s commit
body**, 5 personas against the operator's profiles root, and appear in no live
log. The live 2026-08-22 splits are `walk_ms=2133 tool_visibility_ms=2232` on
the cold boot (pid 30588) and `walk_ms=769 tool_visibility_ms=26` warm
(pid 32164).

---

## The core cache — `serve_read_model/`

`agent_runtime/core_cache.py` persists the built core so the next process pays
**validation** instead of reconstruction. Its module docstring is the design
authority; this is the distillation. **The directory name is a historical trap:
it is not, and never was, the `read_model.db` described below** — that lane is
retired and this one is live.

**A pair is a core plus the fingerprint of every input the build read.** The
on-disk unit is a trio inside a generation directory: `core.json`,
`sidecar.json`, `entries.json` (`core_cache.py:245-252`). The sidecar carries
the digest and the cheap facts read on every consult; `entries.json` holds the
full stat set the digest summarises, in its own file so the cheap half of the
judgement does not pay for the diagnostic half. `live.json` is a pointer naming
the live generation and is **the one file whose replacement publishes a
write-back** (`core_cache.py:254-258`) — a pointer, not a directory rename,
because between two renames there is no live generation at all.

Live pair (2026-08-22 15:46), sidecar verbatim:

```json
{"build_stamp":"git:74702c193e…:clean","contract_versions":{"parity_envelope":1,
 "snapshot_contract":54,"stream_schema":1},"core_sha256":"8bdf4fe4…",
 "event_offset":90007293,"fingerprint":"37f5a746…","fingerprint_entries":2461,
 "fingerprint_home":"X:\\Eternia\\.hermes\\profiles\\base",
 "fingerprint_home_authoritative":true,"generated_at":"…",
 "runtime_root":"X:\\Eternia\\.hermes\\agent-runtime"}
```

**The fingerprint decides validity, full stop.** It is a directory-level walk of
`(path, mtime_ns, size)` triples — directory-level because a file that did not
exist at the last build has no previous triple to compare, and per-file because
replacing an entry does not move the containing directory's mtime on NTFS. An
event-offset key is refused for cause — but only half of that cause is still
measured. The design-era argument reads "the events section is 3 ms of a 5,485 ms
build" (`core_cache.py:27`); that figure is **historical**, and this document's
own live receipt disagrees with it — the cold build's `events` section is 842 ms,
its third most expensive. What carries the refusal today is the other half: two
shipped incidents came from writers that mutate durable state with no EventLog
event at all, which an offset key cannot see no matter how cheap it is.
`event_offset` **is** recorded in the sidecar — as a diagnostic only, never read
into the match decision (`core_cache.py:20-40`).

SQLite is the one mtime-blind case covered explicitly: a WAL commit that has not
checkpointed leaves `state.db`'s mtime untouched, so the `-wal` and `-journal`
siblings are fingerprinted beside it, under a mask that stops *reading* the
database from looking like *writing* it (`core_cache.py:653-706`).

**Demote** is the read-side outcome: a persisted pair that is not served. Every
demote emits `snapshot_core_cache core_source=rebuilt caller=… reason=…`; the ten
reasons are enumerated at `core_cache.py:291-305`. `absent` is deliberately *not*
logged, so a census must never read "no demote line" as "no demote". Read entry
point `core_cache.consult()` (`:3112`); write-back `core_cache.write_back()`
(`:1708`), one unit by MCF-21 — a torn trio is unrepresentable.

A stale-labeled core sets `parity.freshness.state = "stale"`, which the launcher
already maps to `MissionSnapshotHealth.stale`
(`mission_control_snapshot.dart:499-503`; the `declaredStale` predicate that
mapping reads is `:455`). **A cached or stale core never deletes,
never refuses a write, and never wins a conflict** — the 2026-08-15 mass archive
was a projection that had acquired store powers.

Two receipts name the ways this lane fails *quietly* rather than wrongly:
`fingerprint_refused` (a walk hit its entry bound, cache off for this install)
and `never_converged` (consecutive write-backs never agreed, so no later process
can be served the cache at all). `agent_runtime/core_cache_census.py` executes
the census rules as code, run by `scripts/core_cache_demote_census.py`.

---

## The parse cache

`agent_runtime/parse_cache.py` is process-wide and mtime-keyed, for hot
idempotent leaf loads — YAML config/meta files, skill frontmatter, file hashes. A
build re-resolves the same profile and skill tree several times per persona
(readiness plus four tool-visibility passes), and YAML scanning profiled as the
dominant snapshot cost. Keyed on `(path, mtime_ns, size)`, bounded at
`_MAX_ENTRIES = 4096`, self-clearing on overflow. A loader error returns the
default and is **not** cached, so a transient failure self-heals
(`parse_cache.py:42-60`).

---

## The read model — `read_model.db` — RETIRED 2026-08-22

**There is no second cache of the snapshot core.** `serve_read_model/` above is
the only one. This section is history, kept because the lane's name still appears
on disk, in a live config file, and in six committed wire goldens.

**What it was.** `agent_runtime/read_model.py` + `read_model_schema.sql`: a
SQLite database in `store_root()`, WAL journal, holding the whole compact frame
as one `projections_misc` blob plus two row tables (`agent_instances`,
`operator_channels`), at `READ_MODEL_SCHEMA_VERSION = 3`. A `FrameSource` enum
named where a served frame came from (`built` / `cache` / `cache_miss_rebuilt`),
`render_snapshot()` returned `None` rather than a lying `{}`, and
`agent_runtime/projector.py` warmed it through `Projector.full_rebuild()` behind
`hermes harness rebuild-read-model`. It was complete, careful work.

**Why it went.** Four findings, in the order that decided it:

1. `write_snapshot()` had exactly **one** non-test caller —
   `read_model.resolve_snapshot_frame` — reached only from the `harness snapshot`
   CLI verb. The serve path bypassed it by design and said so.
2. `Projector.full_rebuild()` and `write_snapshot()`'s gated
   `ReadModel().apply_full_rebuild(snapshot)` were **two production writers of
   the same database over the same `build_snapshot()` output**, one gated on
   `read_model_enabled()` and one not.
3. `resolve_snapshot_frame` **built the full core first** and only then decided
   whether to serve the cached frame, so a cache HIT cost one full build plus a
   database read. The lane could not save work as shaped.
4. `write_snapshot`'s other output, the `snapshot.json` boot cache, had lost its
   consumer: the launcher's cold-paint reader was retired at MC-7 / P11
   (`mission_control_snapshot.dart:187`), and no reader remains in the launcher's
   `lib/`.

Neither `read_model.db` nor `snapshot.json` existed in the live store root
(verified 2026-08-22) — on a machine that boots the launcher and never runs
`harness snapshot`, both stayed absent whatever the config said. Outcome (2),
**retire**, was the operator ruling; outcome (1), wiring it to the serve path,
would have added a second validity authority beside the fingerprint, which
`core_cache.py:20-40` argues against for the core cache itself.

**What was deleted:** `read_model.py`, `projector.py`, `read_model_schema.sql`,
`snapshot.write_snapshot` (and its temp-file sweeper), and the two CLI verbs
`harness rebuild-read-model` / `harness read`. `harness snapshot` calls
`build_snapshot()` directly and still stamps `parity.frame_source` — now always
`"built"`, because removing a key from the envelope is a contract change and the
additive rule cuts one way only.

**What survives, and why each one is not an oversight:**

| Survivor | Why |
| --- | --- |
| `serve_read_model/` (`core_cache.py:244`) | the LIVE core cache. Never was the read model; the rename that would have de-collided the name is cancelled, because with the other one gone there is nothing left to collide with |
| `read_model.delta_patches` (`runtime_config.py`) | gates the live S7-A patch producer. Its YAML key path is cross-repo wire — the launcher's base seed writes it |
| `ReadModelConfig.enabled` / `.serve_snapshot_from_db` / `.db_filename` | reader-less, but on the snapshot WIRE via `asdict(cfg)` → `core.runtime_config`, in six goldens the launcher mirrors byte-for-byte. Deleting them is a contract bump plus a two-repo manifest change, not a grep-clean cut — see the Open row |
| `paths.snapshot_path()` | the one authority for where a legacy `snapshot.json` lives, so an orphan left by an older build is still nameable |
| `core_cache._EXCLUDED_STORE_ENTRIES`' `read_model.db` trio | a store written before the cut still holds those files; dropping the exclusion would fold them into that store's fingerprint |

**Naming trap, still live.** `CORE_CACHE_DIRNAME = "serve_read_model"` is the
core cache's on-disk home and has never had anything to do with the database
above. Anyone reading this domain cold meets the phrase "read model" in a
directory name whose contents are `core.json` / `sidecar.json` / `entries.json`.
The warning outlives the module it used to disambiguate from.

---

## Every build re-projects the store

There is no incremental projection lane. `build_snapshot()` walks the store
trees, scans the event log, and reads SessionDB on every build; caching happens
*around* the build (core cache, parse cache, coalescing, demote reuse), never
*inside* it as a delta. The cost is real and measured — cold boot 2026-08-22
15:46, live serve log:

```
snapshot_build_core role=led caller=prewarm generation=1 build_ms=11235 offset=90007293
  sections_top=prompt_observability:4520,agents_readiness:4366,events:842 pid=30588
```

Warm builds in the same process land at 1,948–3,733 ms (generations 18–22, same
log, 13:45–13:46). The `events` section is not the problem; the two walks are.

---

## Invariants

1. **`event_offset` is a byte position, and unknown is `None` — never `0`.** A
   swallowed stat error must not render as the head of the log.
2. **Rotation preserves logical offsets.** Sealed slices are immutable and
   offset-load-bearing; nothing rewrites them. A reader mid-iteration keeps its
   handle and continues into the new live slice with no gap and no duplicate.
3. **Every durable JSON write is atomic** (`utils.atomic_json_write`: stage +
   `os.replace`). The stat fingerprint's soundness depends on it.
4. **The fingerprint alone decides cache validity.** No event-tail replay, ever
   — a second validity authority would drift from the first.
5. **The store decides; the projection serves.** A cached or stale-labeled core
   never deletes, never refuses a write, never wins a conflict.
6. **A cache miss is never silent.** Every demote emits a reason; the two quiet
   failure modes have their own named receipts. `absent` is the one deliberate
   exception and a census must account for it.
7. **A write-back is one unit.** The trio is published by replacing `live.json`;
   a torn trio is unrepresentable.
8. **A never-populated projection returns `None`, not `{}`**, and the
   observability index is a cache, never authority — a missing or corrupt
   `prompt_observability_index.json` falls back to a directory scan.
9. **Archive, never delete.** `events_archive/`, `deleted_archive/`, `*_archive/`
   and `mission_chat_turns.legacy.json` are all kept.

---

## Open rows

- **2026-08-22 — the core cache cannot serve boots reliably.**
  `snapshot_core_cache never_converged` fired 10 times between 2026-08-20 18:21
  and 2026-08-22 13:42 (`profiles/base/logs/agent.log`); the diff paths include
  runtime-authored `state.db-wal`, `state.db`, and the live events slice
  `events_archive/events.81417412.jsonl`. Five of the ten carry
  `diff_scope=every_pass` (self-perturbation, the class worth acting on).
  → [planned/core-cache-input-closure.md](planned/core-cache-input-closure.md)
- **2026-08-22 — cold boot costs 11.2 s of re-projection.**
  `build_ms=11235 sections_top=prompt_observability:4520,agents_readiness:4366,events:842`
  (prewarm, generation 1, pid 30588). Every build re-projects the whole store;
  RD3's incremental lane was retired 2026-08-01 with no successor.
  → [planned/incremental-projection.md](planned/incremental-projection.md)
- **2026-08-22 — CLOSED BY RETIREMENT: `read_model.db` was enabled and never
  written.** The live root config set `read_model.enabled: true` while
  `write_snapshot()` had exactly one non-test caller, reached only from
  `harness snapshot`, so neither `read_model.db` nor `snapshot.json` existed in
  the live store root. Ruled outcome (2), **retire**, and executed as Stage 6 of
  [planned/duplicate-implementation-retirement.md](planned/duplicate-implementation-retirement.md);
  the ruling file itself is deleted and its findings are folded into "The read
  model — RETIRED" above. **One residue is still open and is not this row:** the
  live operator `config.yaml` at `X:\Eternia\.hermes\` still carries
  `read_model.enabled: true`, which is now an advertised-but-inert control. The
  parser ignores unknown and retired keys by construction (`.get` per key), so it
  costs nothing at boot — but this codebase has an explicit rule against inert
  controls, and the line wants deleting by whoever owns that file.
- **2026-08-22 — RD4's push invalidation is still absent.** No change feed in the
  codebase; consumers poll. Unaffected by Stage 6 — the question is about the
  LIVE core-cache lane, not the retired database.
  → [planned/read-model-change-feed.md](planned/read-model-change-feed.md)
- **2026-08-22 — MOOT: a schema bump clears the database.** RD6's forward-only
  migration lane and `ReadModelSchemaTooNew` were never built, and the schema
  they would have migrated is deleted. The planned file is left in place as the
  record of a design that had no subject; nothing here is actionable.
  → [planned/read-model-schema-migrations.md](planned/read-model-schema-migrations.md)
- **2026-08-22 — MOOT: no certification gates.** RD8's soak test, crash drill and
  production-envelope entry were specified for the retired lane. Same disposition
  as the row above.
  → [planned/read-model-certification-gates.md](planned/read-model-certification-gates.md)

---

## Unverified carry-forward

Both from archived docs, both touching this domain's stores, neither verified
against current code in this pass:

- **`state.reconciled` bounded-staleness backstop** — SLO "client staleness ≤ 2×
  heartbeat interval (~10s) for ANY write, rule-compliant or not"
  ([12-read-path-freshness-hardening.md](archive/2026-08-22-pre-consolidation/12-read-path-freshness-hardening.md)).
- **Supersede guard at the store chokepoint** — intent basis, `issued_at` /
  `intent_issued_at`, `superseded` vs `duplicate` outcomes
  ([13-write-path-intent-integrity.md](archive/2026-08-22-pre-consolidation/13-write-path-intent-integrity.md)).

---

## Supersedes

- [archive/2026-08-22-pre-consolidation/05-runtime-data-enterprise-storage.md](archive/2026-08-22-pre-consolidation/05-runtime-data-enterprise-storage.md)
  — primary. Its SQLite DDL still creates `goals` / `runs` / `proofs` /
  `incidents`; those tables were removed with the mission lane, were then
  explicitly `DROP TABLE IF EXISTS`-ed on every connect, and finally went with
  the whole database at Stage 6 (2026-08-22).
  Its RD7 `(segment_seq, byte)` segmentation proposal was **not** built as
  specified — `event_rotation.py`'s manifest scheme shipped instead. Its header
  claim that the "NDJSON change feed … is live and current" is false.
- [14-snapshot-core-build-performance.md](archive/2026-08-22-pre-consolidation/14-snapshot-core-build-performance.md)
  — the `base_offset` design originates here, not in doc 05.
- [12-read-path-freshness-hardening.md](archive/2026-08-22-pre-consolidation/12-read-path-freshness-hardening.md)
  · [13-write-path-intent-integrity.md](archive/2026-08-22-pre-consolidation/13-write-path-intent-integrity.md)
  · [MC_DROPS_SNAPSHOT_CACHE_INVESTIGATION_2026-08-18.md](archive/2026-08-22-pre-consolidation/MC_DROPS_SNAPSHOT_CACHE_INVESTIGATION_2026-08-18.md)
  · [SCOPED_INVALIDATION_PLAN_2026-08-16.md](archive/2026-08-22-pre-consolidation/SCOPED_INVALIDATION_PLAN_2026-08-16.md)
  · [EG0_2_RECEIPTS_2026-08-17.md](archive/2026-08-22-pre-consolidation/EG0_2_RECEIPTS_2026-08-17.md)
  · [MISSION_CONTROL_ENTERPRISE_PLAN_2026-08-17.md](archive/2026-08-22-pre-consolidation/MISSION_CONTROL_ENTERPRISE_PLAN_2026-08-17.md)
  · [MISSION_CONTROL_LEDGER_REFACTOR_PLAN_2026-08-17.md](archive/2026-08-22-pre-consolidation/MISSION_CONTROL_LEDGER_REFACTOR_PLAN_2026-08-17.md)
