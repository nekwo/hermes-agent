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

**The walk binds each persona's profile CONTEXT-LOCALLY.**
`profile_readiness_for_persona` enters `profile_context.persona_profile_scope`,
not the env-exporting `persona_profile_context`: it installs the ContextVars
(`set_hermes_home_override`, `set_hermes_auth_home_override`, the head-home
recording) and writes **no** `os.environ`. That matters because this walk runs
on the snapshot builder thread every 2–4 s in the same `harness serve` process
that hosts chat turns, and takes no `profile_runner._WORKDIR_LOCK` — so under
the old env mirror every ambient `get_hermes_home()` reader on every other
thread resolved the WALKED profile for the width of the walk. Measured cost of
that (2026-08-23 turns): a bundle-free turn built context in 453 ms, while turns
overlapping a walk billed 1,796 / 2,343 ms with `visibility_bundle_builds=3/6`,
because `chat_lane_bundle`'s key carries the ACTIVE `config.yaml`'s
`(mtime_ns, size)` and the race moved *which file that was*.

It is sound because the walk reaches no env-pinned reader: it spawns no
subprocess and drives no plugin; its skill, config and machine-root reads
resolve through `get_hermes_home()` (ContextVar-first) or an explicit path;
`get_default_hermes_root()` collapses to the same answer either way, because a
binding's `profile_home` is always `<root>/profiles/<name>`; and the one raw-env
reader it does reach — `hermes_cli.auth._global_auth_file_path`, on the provider
probe — now reads `hermes_constants.get_hermes_auth_home()`, which resolves the
ContextVar first and the `HERMES_AUTH_HOME` env var second. The named residue is
`HOME`: POSIX `os.path.expanduser` has no context-scoped hook, so a `~` expanded
under the binding (a `skills.external_dirs` entry, the `~/.codex` / `~/.qwen`
singletons) resolves to the process home rather than `<profile>/home`. Inert on
native Windows, where `expanduser` consults `USERPROFILE`.

**So does the prompt-observability section**, and it is the more expensive half:
`snapshot_prompt_observability` enters a binding once per roster instance
(`mission_chat_prompt_observability`'s `skill_profile_context`), and that section
bills `prompt_observability:4520` against `agents_readiness:4366` on the
2026-08-22 cold boot. Same switch, same reason. This site also has a **second
lane**: `persona_commands._cmd_mission_chat_message` calls the same function at
`observability_built`, *before* `profile_runner` installs its own locked
binding — so under the env mirror a chat turn was rebinding the process for every
concurrent turn and for the builder, not only the other way round. Both lanes are
fixed by the one switch.

Its branch audit lands in the same place as readiness, with one axis doing more
work. Reached from inside the binding: no subprocess, no plugin dispatch, and no
`hermes_cli.auth` path at all (this block runs no provider probe, so the
`HERMES_AUTH_HOME` reader is not even reachable here). Skill discovery resolves
through `get_hermes_home()` — `skills_tool._skills_dir`, `skill_utils.get_skills_dir`,
`get_config_path` — and the per-persona hash check takes an **explicit**
`hermes_home=` from `resolve_persona_profile`, never the ambient one. The realm
rows are a sidecar file read (`read_realm_sync_sidecar`, "zero git calls in the
snapshot"), and `paths.store_root()` collapses because the env mode exports the
root it resolved *before* the override, which is what the ambient env resolves to
anyway. The identity-prompt, operative-rules, SOUL-overlay and profile-context-file
reads all run **outside** the binding and are unaffected either way.

The one axis that carries weight is `get_default_hermes_root()`, which reads
`HERMES_HOME` raw and is reached five ways from inside the binding
(`get_shared_skills_dir`, `hermes_cli.profiles.get_profile_dir`,
`skill_install.harness_skill_destination`, `skills_inventory.build_shared_catalog`,
and `skill_utils.skill_source_kind` per resolved candidate). It **collapses**, and
the reason is structural rather than incidental: a binding's `profile_home` always
comes from `get_profile_dir` — `get_default_hermes_root()/profiles/<name>` — and
that function is a fixed point over exactly those paths (a path under the native
home maps back to the native home; a custom `<root>/profiles/x` maps back to
`<root>`; with `HERMES_HOME` unset the profiles root *is* the platform default, so
the written home lands under it). `get_shared_skills_dir`'s docstring states the
same property for its own case. It is pinned by
`test_the_profiles_root_survives_dropping_the_HERMES_HOME_write`, parametrized
over all three ambient layouts, rather than left as an argument in prose.

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
the only one. The `read_model.db` lane (module, schema, projector, both CLI
verbs, `write_snapshot` and the `snapshot.json` boot cache) was deleted at
Stage 6 of the duplicate-implementation retirement — the full what/why record
is that stage's row in
[planned/duplicate-implementation-retirement.md](planned/duplicate-implementation-retirement.md)
and the `fac754194e` commit body; the s74 rows in
`tests/agent_runtime/test_tombstone_registry.py` enforce it. `harness snapshot`
calls `build_snapshot()` directly and still stamps `parity.frame_source` — now
always `"built"`, because removing an envelope key is a contract change and the
additive rule cuts one way only.

**What survives, and why each one is not an oversight** (this table is the live
truth a reader needs; the lane's name still appears in a live config file and
six committed wire goldens):

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
- **2026-08-22 — the retired lane's last traces have a removal plan.** Three
  dead config fields still ride the snapshot wire (contract-bump lockstep —
  rides the NEXT bump, never its own), the operator's live `config.yaml` still
  carries the inert `read_model.enabled: true` (operator-owned one-liner), and
  this doc's RETIRED section shrinks when the wire fields go.
  → [planned/read-model-residue-removal.md](planned/read-model-residue-removal.md)
- **2026-08-22 — RD4's push invalidation is still absent.** No change feed in the
  codebase; consumers poll. Unaffected by Stage 6 — the question is about the
  LIVE core-cache lane, not the retired database; any revival names a new
  producer (the projector is gone).
  → [planned/read-model-change-feed.md](planned/read-model-change-feed.md)
- **2026-08-22 — the Unverified carry-forward sections have no burn-down
  owner.** Seven domain docs carry claims from archived sources that no pass
  has verified against code; nothing schedules that verification.
  → [planned/unverified-carryforward-burndown.md](planned/unverified-carryforward-burndown.md)

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
