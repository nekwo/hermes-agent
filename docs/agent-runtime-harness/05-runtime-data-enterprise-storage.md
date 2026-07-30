# 05 — Runtime Data: Enterprise-Grade Storage & Access (implementation-ready)

> **2026-07-30 — scoped correction.** The SQLite DDL in this doc creates `goals`,
> `runs`, `proofs`, `incidents` row tables and three indexes that were removed by
> [16 — Mission Lane Removal](16-mission-lane-removal.md) S9 (read-model schema is
> now 2, contract 45). Everything else — the read-model projector, watermarks,
> NDJSON change feed, fail-loud root resolution, backup drills — is live and
> current.

Status: **in-progress: RD0-RD3 shipped, RD4 next** (2026-07-03, v2 — upgraded from the v1
proposal with exact modules, schemas, config keys, test files, proof commands,
rollback paths, and per-stage handoff prompts). Written after the Stage C
Mission Control capture work surfaced, in one session, every major weakness of
the current runtime-data path.

Companions: `01-architecture.md` (write side), `02-execution-engine.md`,
`04-decision-hud-simplification-map.md` (H1–H10 envelope),
`Launcher_Brain/20 — Active Initiatives/mission-control-snapshot-architecture.md`
(consumer side), `.../mission-control-parity-audit.md`.

Live measurements grounding this spec (2026-07-03, root
`X:\Eternia\.hermes\agent-runtime`): `snapshot.json` **26.7 MB**;
`events.jsonl` **60.6 MB / 92,484 events, single unsegmented file**;
post-cache `build_ms` ~10 s (was ~54 s); launcher poll spacing ≥ 4 s; the
runtime root directory also carries dozens of ad-hoc `live_burn_*.err.txt` /
`*_build_*.log` debris files.

---

## 1. Honest current-state audit

### Keep (sound)
- **Event-sourced write core.** Append-only `events.jsonl`
  (`agent_runtime/events.py:19` `EventLog.append`, serialized by
  `locks.events_lock()`) + one-file-per-entity JSON stores
  (`agent_runtime/store.py` — `TaskStore:101`, `RunStore:551`,
  `ProofStore:855`, `IncidentStore:1151`, …) written via `atomic_json_write`
  under per-entity file locks. Terminal-idempotent updates and
  duplicate-active-run guards are certified (R4/H8).
- **Parity envelope.** `agent_runtime/parity.py` — `ProjectionAccountant:51`
  (consider/include/drop with bounded 50-sample drops),
  `events_watermark:114` (O(1) byte-offset + last-ts watermark),
  `snapshot._parity_envelope:311` + `_parity_warnings:368`. Projection loss
  is self-reported. This stays first-class and becomes queryable.
- **SQLite precedent.** `hermes_state.SessionDB` (external package) already
  persists chat redaction-safe; consumed at `snapshot.py:453`.

### Fix (not enterprise-grade)
| # | Defect | Evidence |
|---|---|---|
| D1 | **Monolithic snapshot.** `build_snapshot()` (`snapshot.py:98-308`) assembles ~20 projection sections into one dict; `write_snapshot():447` rewrites all 26.7 MB; every consumer re-parses the world. O(world) per read AND per write. | live file; section list §RD2 |
| D2 | **Poll-based consumption.** Launcher `HermesCliMissionControlBridge._loadSnapshotFromCli` (`mission_control_bridge.dart:161`) shells a fresh `hermes` process per refresh, ≥ 4 s spacing (`:56`), 45 s timeout (`:39`). Post-turn lag is structural. | bridge source |
| D3 | **O(world) event reads.** `EventLog.for_task/for_session` do `read_text().splitlines()` over the whole 60.6 MB file (`events.py:55,84`); `CachedEventLog:110` only dedupes I/O within ONE build. No byte-offset resume reader despite the watermark existing. | events.py |
| D4 | **All-or-nothing consumer guards.** `_cachedSnapshotNeedsContractRefresh` (`mission_control_bridge.dart:113`) throws away an entire valid snapshot when one sub-projection looks incomplete — blank HUD instead of one stale lane. | bridge source |
| D5 | **Ambient root/toolchain resolution.** `paths.store_root()` (`paths.py:11-24`): env `HERMES_AGENT_RUNTIME_ROOT` → config.yaml `agent_runtime.store_root` → `get_default_hermes_root()/agent-runtime`; `get_default_hermes_root()` (`hermes_constants.py:113`) silently collapses to the platform home when `HERMES_HOME` is unset/odd — and `%LOCALAPPDATA%\hermes\agent-runtime` exists EMPTY on this machine, a silent zero-goals honeypot. The launcher side has its own 3-layer fallback (`mission_control_process_io.dart:157-214`) incl. hardcoded `X:\Eternia\...` machine paths. Nothing stamps which root actually served a read. | live incident 2026-07-03 |
| D6 | **Multi-writer file races.** Daemon, operator CLI, and launcher-spawned CLI all write the same store files + snapshot.json, coordinated only by `locks._file_lock` (msvcrt/fcntl) per entity + `tick_lock`. Consumer cache-refresh dances are the symptom. | locks.py:21-83 |
| D7 | **Unbounded growth, no hygiene.** Single 60.6 MB `events.jsonl` (no segments/compaction); `deleted_archive/` grows forever; ad-hoc debug logs accumulate in the root; no backup, no restore drill. | live root listing |
| D8 | **Per-read process spawn.** Every launcher refresh pays Python start + full config load before any data moves. | bridge `Process.run` |

**Definition of done for this doc:** bounded-latency reads (O(delta));
one transactional projector; push-based invalidation; per-lane consumer
degradation; deterministic, stamped, fail-loud environment resolution;
versioned schema + migrations; retention/backup with restore drills; all
enforced by CI gates.

---

## 2. Target architecture

```
                 WRITE SIDE (unchanged concept)
 personas/ticker ──► events.jsonl (append-only, byte-offset watermark)
                        │  incremental projector (O(delta), lease-holding)
                        ▼
                 read_model.db  (SQLite, WAL, in store_root)
                   ├─ goals / stages / stage_verification / runs / proofs
                   │  incidents / agent_instances / operator_channels / …
                   ├─ projection_watermarks (projection → event_offset, ts)
                   ├─ parity (completeness, drops — queryable)
                   └─ meta (schema_version, resolved_root, projector lease)
                        │
        ┌───────────────┼──────────────────────────┐
        ▼               ▼                          ▼
  hermes CLI       change feed                snapshot.json export
  (renders from    (read_model.feed NDJSON    (byte-compatible shim,
  DB, no rebuild)  tail + optional            rendered from DB)
                   launcher push)
                        │
                        ▼
          Launcher Mission Control bridge
          (subscribe → delta fetch; poll = heartbeat fallback;
           per-lane staleness; whole-HUD discard deleted)
```

Invariants:
1. `events.jsonl` stays the source of truth; `read_model.db` is rebuildable
   from it at any watermark (preserves H8 crash-recovery semantics).
2. Exactly one projector materializes at a time (SQLite-backed lease in
   `meta`); every other writer only appends events.
3. Consumers never parse the world — they query rows or receive deltas.
4. Every read is stamped with the resolved root; a pinned-vs-resolved
   mismatch is a typed error, never silently-served wrong data.

---

## 3. Stages (each independently shippable; do not half-build RD2)

Sequencing: RD0 → RD1 are cheap and de-risk everything. RD2–RD3 are the core
migration behind `read_model.enabled` (same dual-run/rollback pattern that
shipped H5). RD4–RD5 are consumer work overlapping RD3. RD6–RD8 close the
envelope. No stage may regress H1–H10, R1–R4 proofs, or the 1,210-test
`tests/agent_runtime` suite.

---

### RD0 — Measurement + SLO baseline

**Deliverables (hermes-agent)**
- `agent_runtime/snapshot.py`: extend `_parity_envelope` with
  `snapshot_bytes` (len of the serialized export when written),
  `event_log_bytes` (already implicit in `events_watermark.event_offset` —
  surface it explicitly), `projection_age_ms` (now − watermark ts).
- `tests/agent_runtime/test_read_model_slo.py` (new): SLO constants + a
  budget test that measures `build_snapshot()` on a synthetic 10k-event /
  50-task fixture root (generated in-test; never the live root):
  - `SLO_FULL_BUILD_MS = 2000`
  - `SLO_INCREMENTAL_APPLY_MS = 150` (asserted from RD3 on; until then the
    test records the metric and asserts the full-build budget only)
  - `SLO_CONSUMER_VISIBLE_LAG_MS = 1500` (asserted live from RD4 on)
- Baseline numbers recorded in a `## Baselines` appendix in THIS doc.

**Proof** `python -m pytest tests/agent_runtime/test_read_model_slo.py -q`
plus full suite ≥ 1210.

**Rollback** none needed (additive fields + one test).

**Handoff prompt** “In hermes-agent, add `snapshot_bytes` /
`event_log_bytes` / `projection_age_ms` to `_parity_envelope`
(`agent_runtime/snapshot.py:311`), then create
`tests/agent_runtime/test_read_model_slo.py` with the SLO constants from
docs/agent-runtime-harness/05 §RD0 and a synthetic-root build budget test
(SLO_FULL_BUILD_MS=2000). Record baselines in the doc appendix. Full suite
must stay ≥ 1210 passed. Check your brain first.”

---

### RD1 — Deterministic root/toolchain resolution (kills the D5 class)

**Deliverables (hermes-agent)**
- New `agent_runtime/resolution.py`:
  ```python
  @dataclass(slots=True, frozen=True)
  class RuntimeResolution:
      store_root: Path
      layer: str            # 'env' | 'config' | 'default'
      hermes_home: str | None
      config_path: str
      trace: tuple[str, ...]  # one line per layer consulted → won/skipped(reason)

  def resolve_runtime(env: Mapping[str, str] | None = None) -> RuntimeResolution
  def assert_pinned(resolution: RuntimeResolution, *, pinned_root: str) -> None
      # raises RuntimeRootMismatch (typed) when resolved != pinned
  ```
  `paths.store_root()` delegates to `resolve_runtime().store_root` (keeps
  the `cached_yaml_file` mtime cache — D3 perf fix must not regress).
- **Empty-root tripwire:** when the winning layer is `default` AND the root
  contains no `tasks/` dir AND another candidate layer pointed elsewhere,
  emit event `runtime.resolution.suspect_default_root` and a parity warning
  `suspect_default_root`. (This is exactly the
  `%LOCALAPPDATA%\hermes\agent-runtime` empty-honeypot case.)
- Stamp every envelope: `_parity_envelope` gains
  `resolution: {store_root, layer, trace}` (the field `runtime_root` already
  exists — keep it, add the trace).
- New CLI `hermes harness doctor` **section** (a doctor command already
  exists — `hermes_cli/doctor.py`; add a `harness` resolution table to it or
  register `subs.add_parser("doctor")` under harness in
  `hermes_cli/harness.py` ~line 940, pattern `_cmd_snapshot:5909`): prints
  the full resolution table (each layer, value, exists?, tasks?, winner).
- `GoalRunOptions`/CLI flag `--runtime-root` → `assert_pinned` before any
  store touch.

**Deliverables (launcher)**
- `mission_control_process_io.dart`: delete the hardcoded
  `X:\Eternia\...` machine defaults from
  `missionControlHermesProcessEnvironment` (move them to
  `MissionControlSettings.defaults` — which already carries
  `hermesRootPath = r'X:\Eternia\.hermes'` — so ONE layer owns machine
  defaults); precedence becomes: explicit process env pins
  (`ETERNIA_HERMES_HOME` / `HERMES_HOME` / `HERMES_AGENT_RUNTIME_ROOT`) >
  persisted `MissionControlSettings` > nothing (surface “unconfigured”
  instead of guessing).
- Bridge surfaces `snapshot.parity.resolution` in the Runtime drawer, and the
  Stage C `get_runtime_state` probe adds `harness_root_tail` +
  `harness_resolution_layer` (redaction-safe: tail only) so QA can assert a
  pin held.

**Tests**
- `tests/agent_runtime/test_resolution.py`: one test per layer; mismatch
  raises `RuntimeRootMismatch`; suspect-default tripwire fires on an empty
  default root with a non-empty env candidate; trace is stable.
- Launcher: `test/features/mission_control/mission_control_resolution_test.dart`
  for the settings/env precedence; Stage C probe field test in
  `test/core/qa/`.

**Proof** unit suites + one live Stage C launch with pins asserting the probe
echoes the pinned root tail.

**Rollback** `resolve_runtime` is a pure refactor of existing precedence;
revert = restore old `store_root()` body. Launcher change is
settings-compatible (defaults preserve current behavior).

**Handoff prompt** “In hermes-agent, extract `paths.store_root()` precedence
into `agent_runtime/resolution.py` (`RuntimeResolution` + `resolve_runtime`
+ `assert_pinned` raising typed `RuntimeRootMismatch`), add the
suspect-default-root tripwire event + parity warning, stamp
`parity.resolution{store_root,layer,trace}`, and add the harness resolution
table to doctor. In EterniaLauncher, collapse
`missionControlHermesProcessEnvironment` machine defaults into
`MissionControlSettings.defaults` (env pins > settings > unconfigured) and
expose `harness_root_tail`/`harness_resolution_layer` on the Stage C
`get_runtime_state` probe. Tests per 05 §RD1; both suites green; one live
pinned Stage C launch as proof. Check your brain first.”

---

### RD2 — Transactional read model (`read_model.db`)

**Deliverables (hermes-agent)**
- New `agent_runtime/read_model.py`:
  ```python
  class ReadModel:
      def __init__(self, db_path: Path | None = None)   # default: paths.store_root()/'read_model.db'
      def connect(self) -> sqlite3.Connection           # WAL, busy_timeout=5000, foreign_keys=ON
      def apply_full_rebuild(self, snapshot: dict, *, watermark: dict) -> None
      def render_snapshot(self) -> dict                 # byte-compatible envelope
      def projection_watermark(self, projection: str) -> dict | None
      def read_projection(self, projection: str, *, since_offset: int | None = None) -> dict
  ```
- DDL v1 (`agent_runtime/read_model_schema.sql`, executed idempotently):
  ```sql
  CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY, value TEXT NOT NULL);            -- schema_version, resolved_root, projector_lease
  CREATE TABLE IF NOT EXISTS projection_watermarks(
    projection TEXT PRIMARY KEY,
    event_offset INTEGER NOT NULL,
    last_event_ts TEXT, applied_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS goals(
    id TEXT PRIMARY KEY, state TEXT NOT NULL, title TEXT,
    workspace_id TEXT, realm_id TEXT, updated_at TEXT,
    payload JSON NOT NULL);                                -- full per-goal envelope section
  CREATE TABLE IF NOT EXISTS stage_verification(
    goal_id TEXT NOT NULL, stage_id TEXT NOT NULL,
    owner TEXT, observed_status TEXT, observed_proof_count INTEGER,
    authoritative_status TEXT, authoritative_proof_count INTEGER,
    tamper_flag INTEGER NOT NULL DEFAULT 0, payload JSON NOT NULL,
    PRIMARY KEY(goal_id, stage_id));
  CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, task_id TEXT, persona_id TEXT,
    state TEXT, stage_id TEXT, updated_at TEXT, payload JSON NOT NULL);
  CREATE TABLE IF NOT EXISTS proofs(id TEXT PRIMARY KEY, task_id TEXT, stage_id TEXT,
    type TEXT, status TEXT, created_by TEXT, payload JSON NOT NULL);
  CREATE TABLE IF NOT EXISTS incidents(id TEXT PRIMARY KEY, task_id TEXT, kind TEXT,
    state TEXT, payload JSON NOT NULL);
  CREATE TABLE IF NOT EXISTS agent_instances(instance_id TEXT PRIMARY KEY,
    persona_id TEXT, status TEXT, task_id TEXT, payload JSON NOT NULL);
  CREATE TABLE IF NOT EXISTS operator_channels(channel_id TEXT PRIMARY KEY,
    persona_id TEXT, session_id TEXT, payload JSON NOT NULL);
  CREATE TABLE IF NOT EXISTS projections_misc(
    projection TEXT PRIMARY KEY, payload JSON NOT NULL);   -- daemon, capabilities, topology,
                                                           -- prompt_observability, parity, …
  CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);
  CREATE INDEX IF NOT EXISTS idx_proofs_task_stage ON proofs(task_id, stage_id);
  CREATE INDEX IF NOT EXISTS idx_incidents_task ON incidents(task_id, state);
  ```
  Design rule: **hot filter columns are real columns; everything else rides
  in `payload` JSON** so the envelope stays byte-compatible without 40-column
  churn. `projections_misc` carries whole sections that have no per-row
  access pattern yet.
- Config: `runtime_config.py` gains
  ```python
  @dataclass(slots=True)
  class ReadModelConfig:
      enabled: bool = False          # master flag — OFF ships inert
      serve_snapshot_from_db: bool = True
      db_filename: str = "read_model.db"
  ```
  parsed in `config.py` beside `_simplified_agent_contract_config` (~:355),
  YAML key `agent_runtime.read_model.*`.
- Wiring (flag ON): `write_snapshot()` (`snapshot.py:447`) additionally calls
  `ReadModel.apply_full_rebuild(snapshot, watermark=events_watermark(...))`
  in ONE transaction; `_cmd_snapshot` (`hermes_cli/harness.py:5909`) renders
  from `ReadModel.render_snapshot()` when
  `read_model.enabled and serve_snapshot_from_db`, else legacy path.
  (RD2 is dual-write + equivalent-read; the projector goes incremental in
  RD3.)
- Redaction-at-rest: rows are written from the already-redaction-safe
  snapshot sections (same trust boundary as snapshot.json today); add a CI
  scan (RD8) as the backstop.

**Tests** (`tests/agent_runtime/test_read_model.py`)
- `test_apply_full_rebuild_then_render_is_equivalent` — build a synthetic
  root, `build_snapshot()` → apply → `render_snapshot()` →
  `to_jsonable` deep-equal (modulo additive `parity.read_model` fields).
- `test_wal_crash_mid_transaction_leaves_db_consistent` — kill a writer
  process mid-`apply_full_rebuild` (subprocess + os.kill), reopen, integrity
  check + old watermark intact.
- `test_flag_off_is_inert` — no `read_model.db` created, legacy render used.
- `test_render_budget` — render ≤ `SLO_FULL_BUILD_MS` on the RD0 fixture.

**Proof** full suite green; live root: flag ON, `hermes harness snapshot
--json` byte-diff vs flag OFF (script the diff; additive-only differences);
`build_ms` for DB render recorded.

**Rollback** flip `read_model.enabled=false` — the legacy path is untouched.
Delete `read_model.db`; nothing else references it.

**Handoff prompt** “In hermes-agent, implement 05 §RD2 exactly:
`agent_runtime/read_model.py` + `read_model_schema.sql` (DDL v1 as written),
`ReadModelConfig` behind `agent_runtime.read_model.enabled` (default OFF),
dual-write from `write_snapshot`, DB-backed render in `_cmd_snapshot` when
enabled, plus the four tests in `tests/agent_runtime/test_read_model.py`.
Envelope must stay byte-compatible (equivalence test). Full suite ≥ 1210.
Do NOT start the incremental projector (that is RD3). Check your brain
first.”

---

### RD3 — Incremental projector (kills O(world))

**Deliverables (hermes-agent)**
- `agent_runtime/events.py`: add byte-offset reader
  `EventLog.iter_from_offset(offset: int) -> Iterator[tuple[int, Event]]`
  (seek + line iterate, returns (new_offset, event)); `events_watermark`
  already exposes the file size as the offset.
- New `agent_runtime/projector.py`:
  ```python
  class Projector:
      def __init__(self, read_model: ReadModel, *, config: AgentRuntimeConfig)
      def acquire_lease(self) -> bool        # meta.projector_lease (pid, ts, ttl)
      def apply_pending(self) -> ProjectorResult   # events since min(watermarks) → row updates
      def full_rebuild(self) -> None         # RD2 path, kept as recovery tool
  ```
  Event-kind → table routing map (task.*, run.*, proof.*, incident.*,
  packet.*, steer.*, chat run.tool.* → the owning rows; unknown kinds → bump
  `projections_misc` staleness so the next full section refresh picks them
  up). Per-batch: one transaction = N row updates + watermark advance.
- Ticker integration: at the end of `TickEngine.tick_once` and on
  `run_until_settled` settle boundaries (the natural “events just landed”
  chokepoints), call `Projector.apply_pending()` when the lease is held;
  daemon holds the lease by default, CLI acquires it opportunistically.
- New CLI: `hermes harness rebuild-read-model` (recovery + certification) and
  `hermes harness read --projection <name> [--since-offset N] --json`
  (registration pattern: `subs.add_parser` in `hermes_cli/harness.py` ~:940).
- Parity: `ProjectionAccountant` results persist to the `parity` misc row per
  apply; `incremental_apply_ms` recorded.

**Tests** (`tests/agent_runtime/test_projector.py`)
- `test_replay_equivalence_full_vs_incremental` — synthetic root; apply
  events incrementally from offset 0 vs `full_rebuild()`; deep-equal DB dump.
  **This is the certification test — RD3 does not ship without it.**
- `test_apply_pending_is_o_delta` — after a 10k-event baseline, one new
  event batch applies ≤ `SLO_INCREMENTAL_APPLY_MS`.
- `test_lease_excludes_second_projector`.
- `test_unknown_event_kind_marks_section_stale_not_dropped`.
- Archive-replay equivalence against 3 real `deleted_archive` fixtures
  (copied into the test as fixtures, redaction-scanned).

**Proof** full suite; live: run a no-edit `neko_two_dev_default` goal with
flag ON and confirm `parity.watermark` advances per turn with
`incremental_apply_ms ≤ 150`, and `hermes harness read --projection goals`
returns without a world rebuild.

**Rollback** flag OFF; `rebuild-read-model` regenerates state after any
projector bug; events.jsonl untouched.

**Handoff prompt** “In hermes-agent, implement 05 §RD3: byte-offset
`EventLog.iter_from_offset`, `agent_runtime/projector.py` with lease +
`apply_pending` (event-kind routing table per the spec), ticker chokepoint
integration, `harness rebuild-read-model` + `harness read` CLIs, and the
five tests in `tests/agent_runtime/test_projector.py` — the
full-vs-incremental replay equivalence test is the ship gate. SLO:
incremental apply ≤ 150 ms. Full suite green. Check your brain first.”

---

### RD4 — Change feed / push invalidation (kills poll lag)

**Deliverables (hermes-agent)**
- `Projector.apply_pending` appends one compact NDJSON record per committed
  batch to `<store_root>/read_model.feed` (rotating at 5 MB, keep 2):
  `{"watermark": <offset>, "ts": ..., "changed": {"goals": [ids…],
  "runs": [...], "sections": ["parity", ...]}}` — ids bounded at 50 per kind,
  overflow → `"all"`.
- Optional push: config `read_model.publish_centrifugo: bool = False` +
  channel `harness:runtime:<sha8(store_root)>`; hermes-agent has NO
  Centrifugo client today (verified), so this lands as a thin optional
  publisher using the backend's existing HTTP publish API — if that
  dependency is unwanted, ship feed-file-only (the launcher can tail a file
  it can already reach; Stage C runs are same-machine).
- `hermes harness read` gains `--follow` (tail the feed, emit deltas).

**Deliverables (launcher)**
- `mission_control_bridge.dart`: a `ReadModelFeedWatcher` (dart `File`
  length-poll at 250 ms on `read_model.feed` — cheap stat, not JSON parse)
  triggers `requestMissionControlImmediateRefresh` with the changed-section
  hint; the existing ≥ 4 s poll (`:56`) stays as the heartbeat fallback; when
  `read_model.publish_centrifugo` is on, subscribe via the existing
  `core/services/realtime/centrifugo_client.dart` instead.
- Refresh with a section hint fetches `harness read --projection X` for the
  changed sections only once RD3's CLI exists; otherwise falls back to full
  snapshot.

**Tests** hermes: feed record shape + rotation + bounded ids
(`test_projector_feed.py`). Launcher: watcher triggers refresh on feed
append; heartbeat still fires with no feed
(`mission_control_feed_watcher_test.dart`).

**Proof** live: event → HUD-fetchable ≤ `SLO_CONSUMER_VISIBLE_LAG_MS`
(1500 ms) measured by stamping a chat turn and polling the bridge.

**Rollback** feed file is additive; launcher watcher behind a settings flag
`missionControlFeedWatcher` defaulting ON only when the feed file exists.

**Handoff prompt** “Implement 05 §RD4: projector NDJSON change feed
(`read_model.feed`, rotation + bounded changed-ids) + `harness read
--follow` in hermes-agent; `ReadModelFeedWatcher` (length-poll → immediate
refresh with section hints, heartbeat fallback intact) in the launcher
bridge. Tests per spec; live lag ≤ 1500 ms proof. Centrifugo publisher is
OPTIONAL — skip unless the backend publish API is already reachable. Check
your brain first.”

---

### RD5 — Consumer graceful degradation (kills all-or-nothing)

**Deliverables (launcher)**
- Delete `_cachedSnapshotNeedsContractRefresh` /
  `_cachedSnapshotNeedsPromptContextRefresh` /
  `_cachedSnapshotNeedsOperatorChannelRefresh`
  (`mission_control_bridge.dart:113-135`) — replace with per-projection
  staleness: `MissionControlSnapshot` gains
  `sectionFreshness: Map<String, MissionSectionFreshness>`
  (`{watermark, fetchedAt, stale}`), populated from `parity.completeness` +
  the RD4 hints; a section that fails validation is dropped ALONE with a
  `sectionFreshness[...].stale = true` entry.
- UI: lanes render stale data with a staleness badge (tokened, reduced-motion
  aware); goals never blank because chat contexts hiccupped. Protected-page
  impact expected minimal (badge lives in the unprotected projection layer +
  small panel hook; follow the STAGEC_MISSION_CONTROL_CAPTURE protected-edit
  protocol if the page must change).
- Stage C: `get_widget_state` gains widget `mission_control.freshness`
  (per-section stale booleans) so QA can assert degradation behavior.

**Tests** launcher: fault-injection widget test — corrupt one projection in
the fixture, assert other lanes render + badge shows
(`mission_control_degradation_test.dart`); Stage C probe test in
`test/core/qa/`.

**Rollback** the old guards are deleted, not flagged — the equivalence bar is
the widget tests + one live session; if a regression appears, revert the
bridge commit (isolated file).

**Handoff prompt** “In EterniaLauncher, implement 05 §RD5: replace the
whole-snapshot refresh guards in `mission_control_bridge.dart` with
per-section `sectionFreshness` (watermark/fetchedAt/stale), stale-badge
rendering in the cockpit projection layer, and the
`mission_control.freshness` Stage C probe. Fault-injection widget tests per
spec. Protected-page edits only per the capture doc protocol. Check your
brain first.”

---

### RD6 — Schema versioning + migrations

**Deliverables (hermes-agent)**
- `meta.schema_version`; `agent_runtime/read_model_migrations.py` — ordered,
  forward-only, each `def migrate_v<N>_to_v<N+1>(conn)` in one transaction;
  opening a newer-schema DB with older code raises typed
  `ReadModelSchemaTooNew` (fail closed).
- Golden fixtures: `tests/agent_runtime/fixtures/read_model/v1/…` — a small
  DB per shipped schema; CI migrates the oldest to head and byte-diffs the
  rendered envelope against the fixture's stored render.
- Launcher contract test: `test/features/mission_control/` golden parse of
  the head-schema rendered envelope (harness generates → committed fixture →
  launcher parses).

**Proof/rollback** migrations tested in CI; rollback = `rebuild-read-model`
regenerates head schema from events.

**Handoff prompt** “Implement 05 §RD6: `meta.schema_version`, forward-only
migration runner (`read_model_migrations.py`), typed fail-closed on
newer-schema DBs, golden fixture chain test, and the cross-repo envelope
contract test. Check your brain first.”

---

### RD7 — Retention, compaction, backup, root hygiene

**Deliverables (hermes-agent)**
- Event-log segmentation: `events.jsonl` rolls to
  `events/<seq>.jsonl` at 32 MB; `EventLog` reads segments transparently;
  offsets become `(segment_seq, byte)` — watermark schema bumps with RD6
  migration. Segments whose every task is archived compact to
  `deleted_archive/events/<seq>.jsonl.zst`.
- Janitor `hermes harness janitor` (+ daemon cadence, reusing the R5 worktree
  GC scheduling): archive-dir size/age caps with an index manifest;
  ad-hoc `*.err.txt` / `*.out.json` / `*.log` debris in the root moved under
  `diagnostics/` with a 14-day TTL. **Never touches unarchived terminal
  state** (the Round-3 lesson: archival is an explicit operator/daemon act).
- `hermes harness backup [--out <file>]` → tar of `read_model.db` (via
  SQLite backup API), current event segment set, and the store dirs;
  `hermes harness restore --verify` → restore to temp root, `rebuild-read-model`,
  byte-diff rendered envelopes. CI runs the drill on a fixture root.

**Handoff prompt** “Implement 05 §RD7: 32 MB event-log segmentation with
transparent multi-segment `EventLog` (watermark schema bump via RD6
migration), the janitor (debris → diagnostics/ TTL, archive caps, never
unarchived terminal state), and `harness backup`/`restore --verify` with the
CI restore drill. Check your brain first.”

---

### RD8 — Certification gates (make it STAY enterprise-grade)

**Deliverables**
- CI perf gates: RD0 SLOs become hard asserts
  (`test_read_model_slo.py` — full-build ≤ 2000 ms, incremental ≤ 150 ms on
  the pinned synthetic fixture; fail, not warn).
- Concurrency soak (`tests/agent_runtime/test_read_model_soak.py`, marked
  `integration`): daemon-style projector + 2 CLI event writers + 1 reader
  process for 60 s on a temp root — zero `HarnessLockUnavailable` leaks to
  callers, zero torn reads (every render passes schema validation).
- Crash drill: kill -9 the projector mid-batch (subprocess), restart, assert
  watermark resume without double-apply (idempotency by offset).
- Redaction-at-rest: Stage C redaction patterns run over a dumped
  `read_model.db` in CI (reuse the launcher's scan patterns or the
  `agent_runtime.redaction` scanner).
- Only after 10 consecutive green CI runs: add a `runtime_data_storage` item
  to `agent_runtime/production_envelope.py` with `implemented` + the real
  control list (per the H5 lesson: no advertised-but-inert controls).

**Handoff prompt** “Implement 05 §RD8: promote the SLOs to hard CI asserts,
add the soak + crash-drill + redaction-at-rest tests, and (only after 10
green runs) the honest `production_envelope` entry. Check your brain first.”

---

## 4. Non-goals
- No client/server database, no cloud dependency — local-first stays.
- No breaking change to the `snapshot --json` envelope or the Stage C MCP
  tool surface; consumer-visible changes are additive behind schema_version.
- No rewrite of the event format; `events.jsonl`'s line schema is the
  durability contract (segmentation only changes file layout).
- The Centrifugo push in RD4 is optional; the feed file is the contract.

## 5. Risk register
| Risk | Mitigation |
|---|---|
| SQLite lock contention with existing file-lock writers | WAL + busy_timeout; only the projector writes the DB; stores keep their file locks (unchanged write path) |
| Envelope drift breaking the launcher / Stage C tooling | RD2 byte-equivalence test + RD6 cross-repo golden fixtures are ship gates |
| Projector bug corrupting the read model | events.jsonl is truth; `rebuild-read-model` + RD3 replay-equivalence certification |
| Windows path/locking quirks | soak + crash drills run on Windows CI (this machine is the reference) |
| Half-built RD2 shipping | flag defaults OFF; the equivalence test must exist in the same PR as the flag |

## Baselines
| Metric | 2026-07-03 (pre-work) | Post-RD2 | Post-RD3 | Post-RD4 |
|---|---|---|---|---|
| snapshot_bytes | 26.7 MB | | | |
| event_log_bytes | 60.6 MB (92,484 events) | | | |
| full build_ms (live root) | ~10,000 | | | |
| full build_ms (RD0 synthetic 10k events / 50 terminal tasks) | 1,552 | 1,552 (legacy build unchanged) | | |
| read_model full_rebuild_ms (RD0 synthetic 10k events / 50 terminal tasks) | n/a | 348 | | |
| read_model render_ms (RD0 synthetic 10k events / 50 terminal tasks) | n/a | 29 | | |
| incremental_apply_ms (RD0 synthetic + 1 goal event) | n/a | n/a | 62 | |
| consumer_visible_lag_ms | ≥ 4,000 (poll) | | | |
