# Mission Control boot window — where the 47.5 s actually went, and the stages that buy it back (Plan G, 2026-08-17)

> **Home.** `docs/agent-runtime-harness/`, beside Plans E/F (office write lanes — this plan
> touches the same launcher files and defers lane semantics to them) and doc
> `14-snapshot-core-build-performance.md` (owns the snapshot build's internals; BW-H1 below is
> its item-1/3 family finally pointed at boot). Repos as read: hermes `7a145cd254` (main),
> launcher `2597bdd86` (main) — both verified (RAN, `git log`). Live evidence:
> `X:/Eternia/.hermes/profiles/base/logs/agent.log` + `errors.log` (LOG, read-only — nothing
> under the live root was written) and the operator's boot receipt
> `[05:49:06.583] [MissionBoot] open#1 authoritative` (RELAYED).

**Evidence tags** — `READ` (file:line inspected this session, at the SHAs above), `RAN`
(command run read-only this session), `LOG` (line quoted from the live agent.log/errors.log
with its timestamp), `RELAYED` (measured by the operator, not re-derived here), `A-n`
(assumption, listed in §6).

---

## 0. Verdict up front — five corrections to the brief, then the shape of the fix

The brief's ground truth (the receipt) is right; two of its three findings are not, and the
corrections change which stages are worth building. All timestamps below reconcile the receipt
against the live hermes-side logs to ±0.1 s — the receipt's spans anchor at
`open_requested` = **05:48:19.090** (check: `first_frame=1766` → 05:48:20.856, exactly the
operator's first-frame line; `authoritative=47493` → 05:49:06.583, exactly the receipt's own
log time).

**Correction 1 — FINDING 1 is wrong: the two big phases are NOT serial.** The serve child was
created at **05:48:23.08** (LOG: its ready-frame boot timeline says `total_ms=21859` at
05:48:44.942, and `interpreter_ms + elapsed` back out the same instant), i.e. **open + 3.99 s —
21 s before the gate cleared** (`gate=25156` → 05:48:44.25). `gate` (the health-probe window)
and `spawn_to_ready` overlap almost entirely. `25.2 + 21.9 ≈ 47.5` is an arithmetic
coincidence: the boot contains **three** ~20 s costs — the probe child's cold bootstrap and the
serve child's cold bootstrap (concurrent, contending), then a third one (Correction 3) that is
serial after them. The code agrees with the log: the spawn permit falls back to the PRESENCE
result while the health verdict is still null (`mission_control_provider.dart:662-668` READ),
and the live-snapshot watch at `:689` fires the serve-session provider chain
(`:689 → :214/:220 → :64-77`, `session.ensureStarted()` at `:73`) with no dependency on the
verdict. The spawn argv is a compile-time constant (`mission_control_serve_session_io.dart:1064-1068` READ).

**Correction 2 — the mid-task correction ("Riverpod laziness serializes gate→spawn; fix by
touching the provider early") is also wrong, on its own cited lines.** `:662-668` is not a
health await — it is the presence fallback that makes the spawn concurrent today. An
"eager read at open" stage would be a no-op: the spawn already starts ~1 s after the presence
probe resolves (the residue in front of it is the hygiene-sweep await, Correction 5). This
plan therefore does NOT contain that stage. What survives of ruling 1 is the other half:
the probe child still costs a full cold hermes bootstrap **concurrent with the serve child on
the same drive**, and the verdict it produces gates only WRITES (§1.4), which the serve
`ready` frame answers anyway. Ruling 1's original shape (serve-ready-as-verdict,
probe-on-failure) is the right one — for contention and for the write gate, not for wall
serialization. That is BW-L5.

**Correction 3 — the receipt hides a third ~20 s phase, and it is the largest.** There is no
span between `serve_ready` and `authoritative` in the receipt. The live log fills it: the
first authoritative hydrate's snapshot build ran **19,937 ms** —
LOG `05:49:06.344 INFO agent_runtime.stream: snapshot_build reason=hydrate elapsed_ms=19937
offset=88844923` (two waiters share it via `_BUILD_COALESCE`/`accept_inflight`, reporting
19,625 ms at the same instant; the next build, warm, is 2,406 ms at 05:49:09). Ready was
05:48:44.9; the build started 05:48:46.4; `authoritative` lands 240 ms after the build
finishes. **A cold `build_snapshot()` on the X: drive is the single biggest term in the boot**
— bigger than the interpreter tax the brief asked about.

**Correction 4 — the 18 unexplained seconds inside `spawn_to_booting` are attributed, by the
instrument hermes already ships.** The `booting` frame's self-stamp (`agent_runtime/boot_timeline.py`,
emitted at `serve.py:983-990` before any heavy work): today `interpreter_ms=20421`; the three
prior (warm) boots on 08-16 logged `interpreter_ms=2062 / 1933 / 2092` (LOG, all four lines in
agent.log). Everything after the first hermes instruction cost 1,437 ms today (phases:
`chat_registry 218, root_anchor 16, service_foundations 264, orphaned_turn_sweep 843,
dispatch_restore 93`). So the 18.5 s excess is entirely **interpreter + `hermes_cli` import
tax on a cold day**, and the log names three multipliers inside it:
- **The checkout changed under the boot** (the office/RPC merge, `b6f11b04c5 → 7a145cd254`),
  so the launch-time stale-bytecode guard fired: LOG 05:48:33.926/.938 — **two processes,
  12 ms apart, each cleared ~175 `__pycache__` directories** (`main.py:5194-5237` READ; called
  at `main.py:11197`; no lock — both the probe child and the serve child paid the sweep AND
  the full recompile of the import set they then raced each other to write back).
- **Plugin discovery runs during import, before `booting`**: LOG 05:48:39.7-40.4, two full
  54-plugin discovery passes (one per child). It is triggered at import time —
  "`discover_plugins()` runs at `model_tools` import" (`plugins.py:235` READ) — not by
  `_cmd_serve`, whose own prewarm deliberately defers tool definitions to post-`ready`
  (`serve.py:1496-1508` READ).
- **Two cold children contending for the same drive** for the whole window.
The split between plain cold page-in, sweep+recompile, discovery, and contention is NOT
attributable from this log — that is BW-0's job (the sweep logs no duration; `interpreter_ms`
is one opaque number).

**Correction 5 — smaller factual fixes.**
- **"5 spawns per flush, 15 across three flushes" — zero processes were spawned.** Every
  pre-gate office write is refused in-process at the readiness chokepoint BEFORE the runner:
  `ReadinessGatedMissionActionRepository` wraps the CLI repository
  (`mission_control_provider.dart:307-333` READ; refusal at
  `mission_action_readiness.dart:145-151`, message minted at `:53-59`). The cost was 15
  refusal receipts and a latched `failed` banner, not 15 interpreter starts. Rulings 2-4
  stand — cheaper than believed (BW-L6/BW-L7).
- **The update check is not a suspect.** `hermes --version` calls `check_for_updates()`
  synchronously (`main.py:5011-5022` READ) but it is **local-git only** on this install —
  `git rev-list HEAD..origin/main` against the local ref, no fetch; network (`git ls-remote`)
  only on the nix `HERMES_REVISION` path (`banner.py:276-345` READ) — and cached 6 h in
  `$HERMES_HOME/.update_check`. It is **absent from the serve path** (grep: callers are
  `--version`, the TUI gateway's background prefetch, and the dashboard endpoint — not
  `serve.py`). Cost on `--version`: a couple of git subprocesses.
- **The WAL-vulnerability warning is a log line, not a phase.** Both emissions
  (`async_delegation` 05:48:46, `state.db` 05:48:53) sit inside the already-measured hydrate
  build window; the SQLite opens they mark are part of that build's cost, already counted.
- **The hygiene sweep IS on the spawn path, by ~3.0 s.** Body 3,469 ms, concurrent with the
  gate as documented — but `_spawn()` awaits `_sweepOrphanServesOnce()` before
  `Process.start` (`mission_control_serve_session_io.dart:1059`, invariant stated at
  `:1166-1181`), and this boot the sweep finished at 05:48:23.03 with the child created at
  05:48:23.08: presence resolved 05:48:20.05, so the sweep delayed `Process.start` by
  **2.98 s measured** (BW-L4 splits it).

**The shape of the fix.** Cold critical path as measured:
`open → presence 1.0 → sweep tail 3.0 → serve cold bootstrap 20.5 → ready 1.4 → (1.5 gap) →
hydrate build 19.9 → authoritative` = 47.5. Two ~20 s terms, one ~3 s term. The stages attack,
in measured-seconds order: the hydrate build (BW-H1, ~18 s), the import tax (BW-H2/H3,
bounded by the 18.4 s cold-warm delta, attribution first via BW-0), the sweep-in-front-of-spawn
(BW-L4, 3.0 s), the probe child (BW-L5, contention + the write gate), and the
first-gesture integrity rulings (BW-L6/L7, 0 boot seconds, operator-priority).

---

## 1. The proof

### 1.1 The reconstructed timeline (LOG + RELAYED, reconciled)

| Wall clock | Δopen | Event | Evidence |
| --- | --- | --- | --- |
| 05:48:16.20 | −2.9 | app start (`markAppStart`, `main.dart:40`) | RELAYED `app_start_to_open=2892` |
| 05:48:19.090 | 0.0 | `open_requested` (`beginOpen`, provider `:541`) | anchor; checks below |
| 05:48:20.06 | 0.95 | presence probe done (FS probe) | RELAYED `presence_probe=954` |
| 05:48:20.856 | 1.77 | first frame (cached paint lane) | RELAYED + operator's first-frame line, exact match |
| ~05:48:20.1 | ~1.0 | health probe child spawned (`harness health --json`, 45 s timeout, `mission_control_provider.dart:816-830`, verb at `mission_control_hermes_installer.dart:2642-2648`) | READ; corroborated by the second purge/discovery process in LOG |
| 05:48:23.03 | 3.94 | hygiene sweep done (`hygiene_sweep=3941` from open) | RELAYED |
| 05:48:23.08 | 3.99 | **serve child created** (`Process.start`, after the sweep await) | LOG back-computed from `total_ms=21859` @ 05:48:44.942 |
| 05:48:26–31 | 7–12 | three office flushes refused in-process (`runtimeNotProven`), `failed` banner latches | RELAYED + READ (Correction 5) |
| 05:48:33.93 | 14.8 | **both children clear stale `__pycache__`** (171 + 179 dirs, checkout changed) | LOG ×2 |
| 05:48:39.7–40.4 | 20.6–21.3 | both children run 54-plugin discovery (import-time) | LOG ×2 |
| 05:48:43.5 | 24.4 | serve reaches `_cmd_serve` → `booting` frame (`interpreter_ms=20421`) | LOG self-stamp; RELAYED `spawn_to_booting=20571` (launcher-side, +150 ms frame receipt) |
| 05:48:44.25 | 25.16 | **gate cleared** (`health_resolved`; probe child returned ~24 s) | RELAYED `gate=25156` |
| 05:48:44.94 | 25.85 | serve `ready` (post-booting phases 1,437 ms, incl. orphan sweep 843) | LOG boot-timeline line; RELAYED `spawn_to_ready=21900` |
| 05:48:46.41 | 27.3 | hydrate's snapshot build starts | LOG (06.344 − 19.937) |
| 05:49:06.34 | 47.25 | build done — `snapshot_build reason=hydrate elapsed_ms=19937` (+2 coalesced waiters @19,625) | LOG |
| 05:49:06.583 | 47.49 | **authoritative** (`transportMode != cached`, provider `:1187`) | RELAYED, exact match to receipt log time |

### 1.2 Where the receipt misled

Two receipt properties produced the serial reading: (a) `gate` and `spawn_to_*` are spans with
different anchors (`gate` from `open_requested`, timeline `:269`; `spawn_to_*` from a mark
placed after `Process.start` resolves, `mission_control_serve_session_io.dart:1073-1076`), so
nothing in the receipt says whether they overlap; (b) there is **no span at all** between
`serve_ready` and `authoritative`, so a 19.9 s build in that window is invisible and the
reader's eye closes the books with `25.2 + 21.9 ≈ 47.5`. BW-0 fixes both.

### 1.3 The serve child's 20.4 s, decomposed as far as the log allows

Process create 05:48:23.08 → purge log 05:48:33.94 (**10.9 s**: interpreter start + early
`hermes_cli.main` imports, cold, contending) → discovery 05:48:39.7-40.4 → `_cmd_serve`
05:48:43.5 (**9.6 s**: post-purge imports now RECOMPILING everything the purge deleted, plus
discovery). Warm baseline for the same span: ~2.0 s (three boots on 08-16). The four candidate
components (cold page-in, purge+recompile, discovery, sibling contention) cannot be split
further from this log — the purge line logs no duration and `interpreter_ms` is one number.
That attribution gap is real and is why BW-0 lands first: **a phase nobody can attribute is a
phase nobody can fix** — and 20.4 s is currently one phase.

### 1.4 What the health verdict actually gates, and who consumes it

The full consumer sweep (READ, 14 sites) reduces to four roles:
1. **Write permission** — `missionActionReadinessProvider` → the chokepoint repository
   (`mission_action_readiness.dart:100-151`): writes refused until `paint.healthy` or an
   authoritative live frame. *This* is what the 25 s gate actually withheld today.
2. **Repair-panel routing** — `_HermesRuntimeGate` (`hermes_install_panel.dart:14-32`) and the
   page's gate branch (`mission_control_page.dart:548-568`) show install/repair/reconnect
   panels off the verdict's `state` + `resolvedExecutable`.
3. **Proven-runtime lifecycles** — `mission_control_page.dart:590-636` starts realm sync,
   relay, board overlays only when healthy.
4. **Chat outbox drain** — `mission_agent_chat_panel.dart:485-488` replays persisted sends on
   the healthy edge.
None of these needs a probe process when the serve child reaches `ready` — a runtime that
booted, published its root anchor, and answered the greeting has demonstrated everything
`harness health --json` asserts. What they DO need on the failure path is a **diagnosis**,
which is exactly when a probe spawn is cheap relative to the alternative (BW-L5 keeps it
there). Paint is NOT gated on the verdict today (cached lane at 1.77 s; `permitted` falls back
to presence) — only writes and the lifecycle starts are.

### 1.5 The hygiene sweep

`startMissionControlBootHygieneSweep` (`mission_control_serve_session_io.dart:180-219`):
one process enumeration + up to three `powershell.exe` spawns + N `taskkill`s, sweeping
orphan serves, stray QA windows, orphan MCP servers (`mission_hygiene_reaper.dart:115-130`).
Nothing consumes the `hygiene_sweep_done` mark; the future is awaited in exactly one place —
`_spawn():1059` — because the reaper must never kill the child we are about to own
(`:1166-1181`). Only the **serve** portion of the sweep carries that invariant; the QA-window
and MCP portions are along for the ride. Measured cost in front of `Process.start` this boot:
2.98 s (§0 Correction 5).

---

## 2. Validation

### 2.1 What each stage buys, honestly

| Stage | Buys (cold, this receipt) | Does NOT buy |
| --- | --- | --- |
| BW-0 | 0 s — attribution: splits the 20.4 s into named segments; adds the missing `ready→authoritative` span | any wall time |
| BW-H1 | ~18 s: hydrate build 19.9 → cache-load + tail replay (the wire form of the core is 822 KB ≈ 5 ms to serialize, fold plan MEASURED) | the warm build (2.4 s live) — doc 14 items own that |
| BW-H2 | single-winner bytecode sweep: removes one full purge+recompile and the concurrent rewrite race; share of 18.4 s unattributed until BW-0 | cold page-in of a genuinely cold cache |
| BW-H3 | discovery off the import path: ≥0.7 s warm ×every CLI child, more cold; also shrinks the probe child while it exists | plugin functionality (post-ready prewarm already loads tools) |
| BW-L4 | 3.0 s measured: only the serve-portion sweep blocks `Process.start` | the sweep itself (still runs, still bounded) |
| BW-L5 | one whole cold hermes child (contention share of the 20.4 s — unquantifiable without live A/B) + the write gate shrinks from probe-return to serve-ready + the 45 s probe-timeout tail | direct wall time on this receipt (probe was concurrent) |
| BW-L6 | 0 boot s: first gesture lands instead of refusing (ruling 2) | — |
| BW-L7 | 0 boot s: banner truth (ruling 4) | — |

Post-plan targets (A-2, honest): cold ≈ open 0 + presence 1 + spawn ~0.2 + cold bootstrap
(BW-H2/H3-reduced, floor unknown until BW-0) + ready 1.4 + cached hydrate ~2 s; warm
`authoritative` ≈ 6–8 s (today ~9–10 warm by the same phase arithmetic). No timing number in
this table is a test assertion anywhere in §3 — witnesses assert ordering, counts, and
absences only.

### 2.2 The hold: bound and receipt (ruling 3, designed here so BW-L6 implements it)

The hold's job: a pre-proof office gesture stays STAGED (overlay pending, `sync.dirty` true),
is never submitted to a lane that can only refuse, and flushes the moment readiness flips
healthy (same edge the chat outbox already drains on, `mission_agent_chat_panel.dart:485-488`).

**The bound is the lane's own terminal evidence, not a wall clock.** The events that prove the
lane will not come up already exist and are typed: `spawn_failed` after backoff
(`_noteSpawnFailure`, `mission_control_serve_session_io.dart:1183`, re-armed doubling `:1218`)
and `ready_timeout` (`readyTimeout` = 4 min, `:307`). A clock that fires while a child is
legitimately cold-booting re-creates the 2026-07-26 kill-loop class the `booting` frame was
built to end (`serve.py:969-982`) — so expiry is **event-driven**, with a **time-driven
escalation receipt** layered on top:
- t=0: strip shows a typed *holding* status — "N office changes waiting for the runtime" —
  NOT `failed`. Receipt line: `[MissionOfficeWrite] hold: N staged, lane=<reason>`.
- t=45 s (2× the measured cold `spawn_to_ready` of 21.9 s — derived, not round): escalation —
  strip adds "runtime is taking longer than a cold start"; receipt line repeats with
  `held_for_ms`. The hold continues.
- terminal (`spawn_failed`/`ready_timeout`/verdict broken): the hold converts to a **loud,
  latched, retryable failure** — `failed` status naming the count and the terminal reason, the
  repair panel already open (it keys off the same verdict), the writes STILL staged
  (`sync.dirty` true, overlay pending), the canvas STILL showing them (writesInFlight true via
  the overlay term ⇒ the optimistic override cannot retire, §2.3). Retry or a later healthy
  boot flushes.
**The hold never discards.** Expiry changes what the operator is told, never what is staged —
the mass-archive-in-miniature (canvas showing what the store never took, silently) requires a
*silent* divergence, and every arm above emits a receipt and keeps the staged intent alive
until it is either delivered or loudly refused with the repair surface on screen.

### 2.3 What every stage does to `isSettled` and the retire condition

Current predicates (READ, and Plan E §2.4 is the standing authority):
`isSettled = !writesInFlight` (`mission_office_layout_controller.dart:467-468`);
`writesInFlight = timer != null || flushing || any overlay entry pending` (`:433-434`);
page retire additionally requires `!_hasPendingOfficeSave` (`mission_control_page.dart:426`,
`:2990-3006`).
- **BW-0, BW-H1, BW-H2, BW-H3**: hermes-side / instrumentation — no launcher predicate is
  reachable. No change.
- **BW-L4** (sweep split): not on the write lane; changes when `Process.start` runs, which
  moves when `laneAbsent` ENDS, never the predicates. No change.
- **BW-L5** (probe retirement): readiness flips healthy at serve-ready instead of
  probe-return — upstream of the chokepoint; `writesInFlight` terms untouched. The only
  timing effect is the same safe direction Plan E §2.4 already argues: writes are permitted
  no later than today on the healthy path.
- **BW-L6** (the hold): the load-bearing one. The hold is implemented as *staged overlay
  entries that never enter `_flush`* — `overlay.pending` stays true for held writes, so
  `writesInFlight` stays TRUE and the optimistic override CANNOT retire to a server layout
  that lacks the gesture. The hold must NOT touch `_hasPendingOfficeSave`, must NOT clear
  overlay/removed sets, and must NOT set `flushing` (nothing is flushing). `isSettled`'s
  definition does not change; the fence `mission_office_optimistic_paint_test.dart` (OR-4)
  passes byte-unchanged, and if it needs editing the stage changed paint behaviour and owes
  an explanation before merging (Plan E's standing rule, adopted verbatim).
- **BW-L7** (banner): reads/writes `MissionOfficeSyncStatus` only (`:154-190`); no predicate.

---

## 3. Stages

Naming: BW = boot window; H = hermes, L = launcher. Ordered by measured seconds bought
(attribution first, per FC-0's precedent; the two ruling-mandated gesture stages close the
plan — they buy operator trust, not boot seconds). No stage requires simultaneous deployment;
every stage rolls back by reverting its commit.

---

### BW-0 — both repos: the two opaque numbers get named segments

**Goal.** Convert the two inferences this plan rests on into recorded fact before anything is
built on them: split `interpreter_ms`, and put the hydrate build on the receipt.

**Change surface.**
- hermes `hermes_cli/main.py`: a module-scope `time.monotonic()` captured at the top of the
  module (first hermes code that runs), and elapsed-ms logging on the bytecode sweep — the
  purge line at `:5228` gains `swept_ms=N`, and `_sweep_stale_bytecode_if_checkout_changed`
  records its duration into a module global.
- hermes `hermes_cli/harness_parts/serve.py:2867-2873`: `_cmd_serve`'s `BootTimeline` gains
  additive stamps derived from the above: `main_import_ms` (process create → main.py's
  module-scope capture, via the existing psutil anchor), `bytecode_sweep_ms`,
  `dispatch_ms` (main entry → `_cmd_serve`). All ride the existing `boot` block on the
  `booting` frame (`serve.py:983-990`) — additive keys, consumers ignore unknown keys by
  contract (`boot_timeline.py:22-23`).
- hermes `hermes_cli/plugins.py:1497`: the "discovery complete" line gains `elapsed_ms`.
- launcher `mission_boot_timeline.dart` (spans `:310-423`) + `mission_control_provider.dart:1185-1187`:
  a new span `ready_to_authoritative` (serve_ready → authoritative), and the receipt line
  carries the serve child's self-reported `boot` block (already parsed off the `booting`
  frame at `mission_control_serve_session_io.dart:1288-1298`) so one log line attributes both
  sides.

**Tests.**
- hermes: extend `tests/agent_runtime/test_harness_serve.py::test_booting_frame_carries_the_interpreter_stamp`
  — the booting frame carries `main_import_ms` and it is ≤ `interpreter_ms` — kill: drop the
  stamp. Anti-vacuity: *Mutation:* emit `main_import_ms: 0` unconditionally. *Probed field:*
  the test injects a fake process-start anchor (the existing `process_start_monotonic`
  injection point, `boot_timeline.py:59-67`) and a known module-scope capture, and asserts the
  EXACT derived value, not presence. *Why the mutation cannot also set it:* the expected value
  is computed in the test from two injected instants the mutation does not see; a constant
  cannot match two different injected anchors across the two test cases (two driven values —
  the FC-0 discipline).
- launcher: extend the boot-timeline unit tests — `ready_to_authoritative` equals the gap
  between the two marks — kill: derive it from `authoritative − gate` instead. Anti-vacuity:
  *Mutation:* that mis-derivation. *Probed field:* the span value under a synthetic recorder
  where `health_resolved`, `serve_ready`, `authoritative` are marked at three distinct
  instants such that `authoritative − gate ≠ authoritative − serve_ready`. *Why not settable:*
  the two candidate derivations disagree by construction in the fixture.

**Mixed pairs.** Old launcher + new hermes: unknown `boot` keys ignored (existing contract).
New launcher + old hermes: keys absent, span still recorded from launcher-side marks.
**Rollback.** Revert. **Perf.** None — stamps are subtractions and one dict merge.
**Acceptance (operator, live, next cold boot).** The receipt names where the interpreter time
went (`main_import_ms` / `bytecode_sweep_ms` / `dispatch_ms`) and carries
`ready_to_authoritative` — the two numbers this plan had to reconstruct from three logs
arrive on one line.

---

### BW-H1 — hermes: the hydrate stops paying a cold full build (~18 s cold)

**Goal.** A serve boot serves its first authoritative snapshot from a persisted core plus an
event-tail replay, not a 19.9 s filesystem walk. Doc 14's own numbers say the walk is
metadata, not bandwidth: serializing the 822 KB core costs ~5 ms; re-deriving it from the X:
drive costs 6.9 s cold measured then, 19.9 s cold measured today under boot contention.

**Change surface** (hermes; `agent_runtime/snapshot.py` + `agent_runtime/stream.py` + a new
`agent_runtime/core_cache.py`):
- After each successful default-store build (`_build_snapshot_uncoalesced` return path),
  persist the core's **wire form** (the `to_jsonable` serde already exists for the wire)
  atomically under `<store_root>/serve_read_model/core.json` with a sidecar carrying
  `{event_offset, build_stamp, schema_version}` — the same watermark discipline the stream
  already trusts (`build_snapshot`'s `accept_inflight` docstring `:305-319`: the offset is the
  contract; replay-from-offset loses nothing).
- On the FIRST default-store build of a process (the prewarm or the hydrate, whichever leads):
  if the sidecar's `build_stamp` matches this build (`agent_runtime/build_stamp.py`) and the
  event tail from `event_offset` contains only events the **fold coverage** already declares
  patchable (the same entity-class set the wire fold uses — reuse, do not fork, the
  `patch_coverage` authority), load the core and replay the tail; otherwise fall through to
  the full build, unchanged. Either way the outcome is stamped: the `snapshot_build` log line
  (`stream.py:69`) and the hydrate's envelope gain `core_source=cache|rebuilt` +
  `tail_events=N` — additive.
- Cache writes are best-effort by contract (a failed write logs and changes nothing); the
  cache is NEVER authority — a mismatch on stamp, schema, offset arithmetic (rotation's
  `base_offset` sidecar, `event_rotation.py`), or coverage demotes to the full build.

**Preconditions.** BW-0 (so the win is measured on the same receipt that measured the cost).
Coverage reuse is the load-bearing correctness choice: an event class the fold cannot patch
is exactly an event class the replay cannot apply — one authority answers both.

**Tests** (new `tests/agent_runtime/test_core_cache.py`, reusing `test_harness_serve.py`'s
serve-loop harness and the stream fixtures' store fakes):
- `a second boot serves hydrate from the cache and replays the tail` — kill: ignore the cache.
- `a tail containing an uncovered event class demotes to a full build` — kill: replay anyway.
- `a build-stamp mismatch demotes` — kill: trust a stale install's core.
- `a failed cache write leaves the build path byte-identical` — kill: raise.
- **Anti-vacuity** (two independent witnesses, because a receipt field alone can be forged by
  the mutant): *Mutation:* always rebuild but stamp `core_source=cache`. *Probed fields:*
  (1) the envelope's `core_source`, AND (2) an injected store fake whose walk/read functions
  COUNT invocations — the cache-served case asserts **zero** full-build store reads after the
  cache is written. *Why the mutation cannot also set both:* the counter lives in the test's
  fake store; a mutant that rebuilds must call it. The reverse mutant (serve stale cache
  without replay) is killed by a third case: an event appended after the cache write must be
  visible in the hydrate payload (probed field: the appended entity's value, which the cache
  file — written before the append — provably does not contain).

**Mixed pairs.** Wire shape unchanged; `core_source` is additive on an envelope old launchers
ignore. Old hermes + new launcher: field absent, nothing read. **isSettled/retire:** hermes
only — no launcher predicate reachable (§2.3). **Rollback.** Revert; the cache file is inert
garbage to a reverted build (unknown directory, never read). **Perf.** Cold hydrate ~19.9 s →
cache read + tail replay (ms-scale + N events); warm unchanged (cache also removes the
prewarm/hydrate double-build risk `accept_inflight` documents).
**Acceptance (operator, live, next cold boot after one warm boot).** The boot receipt's
`ready_to_authoritative` collapses from ~21.6 s to ~2 s and the serve log shows
`snapshot_build reason=hydrate … core_source=cache tail_events=N`.

---

### BW-H2 — hermes: the bytecode sweep gets one winner (share of 18.4 s, attributed by BW-0)

**Goal.** A checkout change costs ONE process one sweep — not every concurrently-spawned child
a sweep plus a recompile race (today: two children, 171+179 dirs, 12 ms apart, LOG).

**Change surface** (hermes `hermes_cli/main.py:5194-5237`):
- An `O_EXCL` lock file beside `.bytecode-fingerprint` (same directory, same lifetime rules,
  `serve_auth.py:208`'s O_EXCL precedent READ). Winner sweeps, restamps, releases. Loser does
  NOT sweep: it waits on the lock with a short bound (the sweep is directory unlinks, not
  compilation — sub-second warm), then proceeds; on wait expiry it proceeds WITHOUT sweeping
  (fail-open: worst case is today's pre-guard behaviour for one process, and the winner's
  restamp closes the window for every later spawn). Stale-lock handling: locks older than the
  bound are broken — a crashed sweeper must not brick every future launch.
- The purge log line names the outcome: `swept` / `waited_for_winner` / `proceeded_unswept`.

**Tests** (new `tests/hermes_cli/test_bytecode_sweep_lock.py`):
- `two concurrent entries: exactly one sweep runs` — kill: remove the lock. Anti-vacuity:
  *Mutation:* remove the lock. *Probed field:* a patched `_clear_bytecode_cache` counting
  invocations across two threads driven through the entry — asserted == 1 — plus BOTH
  processes' logged outcomes (one `swept`, one `waited_for_winner`). *Why not settable:* the
  mutant's two threads both reach the patched clear; the counter is the test's, not the
  code's, and cannot read 1 under two unserialized calls (the test drives both sides past the
  fingerprint check before either sweeps, via an injected barrier).
- `a stale lock is broken, not honoured forever` — kill: wait unboundedly.
- `non-git install: unchanged no-op` — kill: throw on missing fingerprint.

**Mixed pairs.** N/A (process-local). **isSettled/retire:** N/A (§2.3). **Rollback.** Revert;
the lock file is inert. **Perf.** Removes one full purge+recompile from every
checkout-changed boot and the concurrent `.pyc` rewrite race; exact seconds = BW-0's
`bytecode_sweep_ms` plus the recompile delta between the two children's `main_import_ms`,
claimed only after BW-0 measures a cold boot.
**Acceptance.** Next post-merge cold boot logs ONE "cleared N stale __pycache__" line, and the
second child logs `waited_for_winner` with a sub-second wait.

---

### BW-H3 — hermes: plugin discovery leaves the import path

**Goal.** No hermes process pays a 54-plugin discovery walk it did not ask for. The serve
child pays it TWICE today in effect — once at import (before `booting`) and its tool prewarm
again post-`ready` — and the probe child pays it for nothing.

**Change surface** (hermes): find the module-scope chain that reaches `model_tools` during
`hermes_cli.main` import (the `plugins.py:235` comment names the mechanism: "discover_plugins()
runs at model_tools import") and defer that import to first use — the same function-local
import discipline `serve.py` already applies to everything agent_runtime-shaped
(`serve.py:991-996`). `discover_plugins()` is idempotent and every consumer already calls it
defensively (`tools_config.py:245,260`, `plugins_cmd.py:1834` READ), so the change is moving
an import, not inventing a lifecycle.

**Tests.**
- `importing hermes_cli.main does not run plugin discovery` — kill: restore the module-scope
  import. Anti-vacuity: *Mutation:* restore the import but pre-seed the discovery memo so the
  walk is a no-op. *Probed field:* not timing — the test imports the module under a patched
  `hermes_cli.plugins.discover_plugins` that records callers, asserting zero calls at import
  AND that the first `get_tool_definitions()` call afterwards triggers exactly one. *Why not
  settable:* the memo-seeding mutant still calls the patched function (the memo lives inside
  it); the assertion is on the call record the test owns.
- Regression fence: the serve prewarm still warms tools post-ready
  (`test_harness_serve.py`'s prewarm coverage) byte-unchanged.

**Mixed pairs.** N/A. **isSettled/retire:** N/A. **Rollback.** Revert. **Perf.** ≥0.7 s warm
per CLI child (LOG span), more cold; multiplied by every spawn in the system, not just boot.
**Acceptance.** A warm `hermes --version` no longer logs plugin registration lines, and BW-0's
`main_import_ms` drops on the next receipt.

---

### BW-L4 — launcher: only the serve-sweep blocks the spawn (3.0 s measured)

**Goal.** `Process.start` waits for the ONE sweep whose invariant requires it (never reap our
own serve) and not for the QA-window and MCP sweeps riding the same future.

**Change surface** (launcher `mission_control_serve_session_io.dart` +
`mission_hygiene_reaper.dart:115-130`): split `sweep()` so the serve portion (`_sweepServe`,
fed by one process enumeration) completes as its own awaitable stage; `_spawn():1059` awaits
ONLY that; `_sweepQaWindows`/`_sweepOrphanMcpServers` continue unawaited on the same
enumeration result. The `hygiene_sweep_body` receipt span keeps measuring the WHOLE sweep
(display contract unchanged); a new additive span `hygiene_serve_gate` measures what the
spawn actually waited on.

**Tests** (extend the reaper's existing unit tests + the serve-session fake-process tests):
- `the spawn proceeds once the serve sweep completes, before the QA sweep does` — kill:
  re-await the full sweep. Anti-vacuity: *Mutation:* re-await the full sweep. *Probed field:*
  ordering, not time — a fake inspector whose QA-sweep future is completed manually AFTER the
  test asserts `_startProcess` was invoked (recorded by the fake process starter). The
  mutant deadlocks the assertion point: `_startProcess` cannot have been called while the
  never-completed QA future is awaited. *Why not settable:* the QA future's completion is
  test-owned; the mutant cannot start the process without it.
- `the serve sweep still completes before the spawn` (the invariant, second independent
  witness) — kill: drop the await entirely. Probed field: the fake inspector records kill
  candidates observed BEFORE the recorded `_startProcess` call; the drop-mutant reorders them.
- Fence: the once-per-process memo (`_bootHygieneSweepOnce`) behaviour byte-unchanged.

**Mixed pairs.** N/A (launcher-internal). **isSettled/retire:** not on the write lane (§2.3).
**Rollback.** Revert. **Perf.** 2.98 s measured off the cold critical path this boot; warm
boots keep the win (the sweep is spawn-count-bound, not cache-bound).
**Acceptance.** Next receipt: `serve_spawn_started` lands ~1 s after presence instead of ~4 s,
and `hygiene_serve_gate` ≪ `hygiene_sweep_body`.

---

### BW-L5 — launcher: serve-ready is the healthy verdict; the probe becomes the failure diagnostic (ruling 1, original shape)

**Goal.** Stop spawning a full hermes child to ask whether hermes works while another hermes
child is demonstrating it. Healthy path: `health_resolved` derives from the serve session's
`ready` (the frame already carries build/root/auth posture, `serve.py:1435-1461`). Failure
path: `spawn_failed` / `ready_timeout` / a `booting`-then-silence triggers the EXISTING probe
(`probeHermesRuntimeHealth`, unchanged) to mint the diagnostic verdict the repair panels need.

**Change surface** (launcher `mission_control_provider.dart:574-603`): after presence, the
health provider no longer unconditionally awaits `_runHermesRuntimeHealthProbe`; it awaits
the serve session's ready/failure evidence (the session already exposes both:
`ready` completer `:1287`, `_noteSpawnFailure` `:1183`, `bootingSince` `:1288-1298`) and maps:
ready → `healthy`; terminal failure → run the probe, return its verdict (probingInstall UI
copy already covers the interim). Presence short-circuits (`:578-581`) unchanged. The
`shouldProbeRuntimeHealth` presence gate keeps its meaning (it now gates "wait on serve
evidence" instead of "spawn a probe").

**What the consumer audit says breaks: nothing, with two edges named.** (1) A broken hermes
now gets a doomed serve child before a verdict: consequence chain is
`spawn_failed → _noteSpawnFailure → backoff (doubling, `:1218`) → probe → verdict → repair
panel` — the operator sees the same panel, later by one spawn attempt instead of earlier by
one probe; the backoff prevents a loop. (2) `resolvedExecutable` for the reconnect panel comes
from PRESENCE (`mission_control_hermes_installer.dart:2678-2683`), not the probe — unaffected.
Chat outbox drain and proven-runtime lifecycles fire on the healthy edge exactly as today,
now at serve-ready.

**Tests** (extend the provider tests' fake serve session + fake probe runner):
- `healthy path: the probe runner is never invoked` — kill: keep the probe spawn.
  Anti-vacuity: *Mutation:* keep the probe. *Probed fields:* (1) the fake probe runner's
  recorded argv list — asserted EMPTY on the healthy path, and (2) `health_resolved` emitted
  with the verdict object constructed from the ready frame's fields (the fake session's ready
  frame carries a sentinel `runtime_root` the fake PROBE would never report — the verdict's
  provenance is probed by that sentinel). *Why the mutation cannot also set both:* the mutant
  invokes the recorded runner (its own record convicts it), and its verdict carries the fake
  probe's root, not the ready frame's sentinel — two independent witnesses, either kills.
- `failure path: spawn_failed leads to exactly one probe invocation and its verdict routes the
  repair panel` — kill: return healthy on spawn failure (probed field: `gateStatus.state`
  from the fake probe's injected `broken` verdict — only the probe fake can mint it).
- `the spawn does not regress to awaiting the verdict` (the accidental-serialization fence
  this whole diagnosis exists to prevent): with a fake probe that NEVER completes, the serve
  session's start is still requested — kill: gate `ensureStarted` on `health_resolved`.
  Probed field: the fake process starter's invocation record while the health future is
  provably pending (the future is test-owned and never completed) — a mutant that awaits it
  cannot reach the starter. **This is the ordering witness the brief asked for; no elapsed-ms
  assertion exists in this stage.**

**Mixed pairs.** Old hermes without the `booting` frame: `serve_booting` is already documented
optional (`mission_boot_timeline.dart:109`); ready still resolves; ancient hermes that never
reaches ready hits `ready_timeout` → probe → verdict, same as a broken install.
**isSettled/retire:** upstream of the chokepoint; predicates untouched (§2.3).
**Rollback.** Revert. **Perf.** One cold interpreter+import removed from every cold boot's
disk contention window (share of the 20.4 s: only measurable live A/B — claimed as
"contention relief", not seconds); the write-gate window becomes `spawn→ready` (21.9 s cold
today, ~2.5 s after BW-H2/H3/L4) instead of probe-return (24 s, 45 s worst case).
**Acceptance (operator, live).** Cold boot: agent.log shows ONE "cleared stale __pycache__" /
ONE discovery pass (the serve child's), receipt `gate` ≈ `spawn_to_ready` + ε, and the repair
panel still opens on a deliberately broken `HERMES_HOME` (the failure rehearsal is part of
acceptance, not optional).

---

### BW-L6 — launcher: pre-proof office writes hold, then land (rulings 2 + 3)

**Goal.** A gesture inside the boot window stages, holds with a receipt, and flushes on the
healthy edge — instead of refusing 15 times into a latched banner. Design per §2.2.

**Change surface** (launcher `mission_office_layout_controller.dart`):
- `_flush` consults readiness ONCE at entry (`ref.read(missionActionReadinessProvider)` via an
  injected readiness fn — the controller must not import provider machinery it can be handed):
  not ready → do NOT iterate the arms, do NOT submit, set the typed *holding* status
  (`MissionOfficeSyncStatus.holding(count, reason)` — new phase in the `:154-190` family),
  emit the hold receipt line, leave `timer`/`dirty`/overlay exactly as they are, and return.
  (The per-arm refusals at `:833/:982/:1084` become unreachable for this class; they remain
  for post-proof refusals.)
- A readiness listener (same edge-listen shape as `mission_agent_chat_panel.dart:485-488`):
  healthy → `commitNow` the held workspaces. Terminal (per §2.2) → convert holding →
  `failed(count, reason: holdTerminal)`, staged state untouched.
- The 45 s escalation timer per §2.2 (receipt + copy change only — no state transition).

**isSettled/retire (the constraint that has bitten twice, stated in full):** the hold keeps
`overlay` entries pending ⇒ `writesInFlight` TRUE ⇒ `isSettled` false ⇒ the page's optimistic
override does NOT retire (`mission_control_page.dart:2990-3006`) — the canvas keeps showing
the gesture the whole hold, which is correct because the gesture is still going to be
delivered. No predicate is redefined; `_hasPendingOfficeSave` untouched; `settled` (`:395-396`,
migration seed) untouched. Fences byte-unchanged: `mission_office_optimistic_paint_test.dart`,
`mission_office_lane_reattach_test.dart`, `mission_office_mass_archive_incident_repro_test.dart`.

**Tests** (new `mission_office_boot_hold_test.dart` + extend the layout-controller suite):
- `a pre-proof flush submits nothing and holds` — kill: fall through to the arms. Probed
  fields: fake repository's recorded intent list EMPTY + status phase `holding` with count.
- `the held write is delivered after the healthy edge` — kill: drop held writes at any point
  (the ruling-3 killer witness). Anti-vacuity: *Mutation:* discard the staged overlay at
  expiry/escalation but keep the receipts. *Probed field:* the fake repository RECEIVES the
  held upsert's exact payload after readiness flips healthy — delivery, not bookkeeping. *Why
  the mutation cannot also set it:* a dropped write cannot arrive at the fake; no status
  field, receipt, or counter can satisfy an assertion on the delivered payload itself.
- `terminal failure converts to a loud failed status and RETAINS the staged write` — kill:
  clear `sync.dirty` on terminal. Probed fields: `dirty == true` AND overlay entry still
  pending AND `failed.reason == holdTerminal` — the second witness pair: a mutant clearing
  staged state cannot keep the overlay entry pending, and the paint fence would also redden
  (override retires mid-hold).
- `the optimistic override does not retire during the hold` — kill: exclude held entries from
  `writesInFlight`'s overlay term. Probed field: `isSettled(workspace) == false` while
  holding — asserted through the CONTROLLER's public predicate, which the mutation directly
  targets; paired with the OR-4 fence which asserts the paint outcome the predicate protects
  (two witnesses, different files — the mutation cannot satisfy the fence by editing the
  predicate the first test pins).

**Mixed pairs.** N/A (launcher-internal; wire unchanged — held writes go out later on the
same lanes Plan E owns). **Rollback.** Revert. **Perf.** None claimed at boot; first-gesture
latency becomes hold-until-ready (~22 s cold today, ~2.5 s post-plan) instead of
refuse-and-retry-by-hand.
**Acceptance (operator, live, cold boot).** Drag a desk actor at t≈5 s: strip shows "1 office
change waiting for the runtime", no failure banner, no retry needed; the flush receipt after
ready shows the write delivered; `[MissionOfficeWrite] hold:` lines bracket it in the log.

---

### BW-L7 — launcher: the failed banner stops outliving its cause (ruling 4)

**Goal.** A `failed` status whose failures were all pre-proof refusals (`runtimeNotProven`
class) clears itself when the lane proves healthy — today it latches until a manual Retry or
an unrelated gesture (`_setStatus` writers are only `_schedule`/`_flush`,
`mission_office_layout_controller.dart:653,664,1140,1154`; nothing lane-health-driven touches
it, READ).

**Change surface** (launcher): failures carry a typed reason class alongside the display
string (the readiness refusal already has a typed reason at the chokepoint —
`MissionActionRefusalReason.runtimeNotProven` — thread it into the failure entries instead of
string prose at `:833/:982/:1084`). The BW-L6 readiness listener, on the healthy edge, when
status is `failed` and ALL failure reasons are `runtimeNotProven`: re-arm a flush if `dirty`,
else clear to `idle`. Real (post-proof) refusals keep today's latch — Plan E owns those
semantics and the write-fallback pill's deliberate latch (`:286-306`) is untouched.

**Tests.**
- `a runtimeNotProven-only failed status clears on the healthy edge` — kill: keep the latch.
  Anti-vacuity: *Mutation:* clear ALL failed statuses on the healthy edge. *Probed field:* a
  second fixture whose failure list contains one store-refusal reason — its status must
  REMAIN `failed` after the edge. The two fixtures together pin the discriminator; a mutant
  satisfying both must actually read the reason class. *Why not settable:* the reason enum on
  the fixture entries is test-authored; neither blanket-clear nor never-clear passes both.
- `clearing does not resubmit refused post-proof writes` — kill: re-flush unconditionally
  (probed field: fake repository intent count for the store-refused fixture == 0 after edge).

**isSettled/retire:** status strip only; predicates untouched (§2.3). **Mixed pairs.** N/A.
**Rollback.** Revert. **Perf.** None. **Acceptance.** After a cold boot with a pre-proof
gesture and NO operator action, the strip shows no stale "N office changes could not sync —
Retry" once the lane is healthy and the held/retried write has landed.

---

## 4. Sequencing constraints

1. **BW-0 lands first and alone** (FC-0's precedent): every other stage's Perf claim is
   measured against receipts BW-0 creates. BW-H1's acceptance in particular reads
   `ready_to_authoritative`, which does not exist until BW-0.
2. **BW-H1 is independent of every launcher stage** and is the largest win; it should not
   queue behind the launcher work.
3. **BW-H2/H3 before BW-L5's acceptance run**, not before its merge: BW-L5's acceptance
   compares cold receipts, and landing the import-tax stages first keeps the A/B honest
   (otherwise the probe-removal win and the import win are confounded in one receipt).
4. **BW-L6 and BW-L7 share the readiness listener and the typed-reason plumbing — land BW-L6
   first**; BW-L7's clear rides the same edge. Both must keep Plan E's fences byte-unchanged
   (§2.3); any edit to `mission_office_optimistic_paint_test.dart` is a stage-stopping event,
   not a test update.
5. **Write-lane collision map**: `mission_office_layout_controller.dart` is Plan E's home
   surface (WV-L1/L2 landed; WV-L5 merged at `2597bdd86`) — BW-L6/L7 touch `_flush` entry and
   status only, not the per-arm RPC ladders; rebase-level, but do not land while an unmerged
   Plan E branch holds the file. `serve.py` is FC-H1's home — BW-0's booting-frame keys are
   additive and collide only textually.
6. **No stage requires simultaneous deployment; each rolls back by reverting its commit.**
   BW-H1's cache file and BW-H2's lock file are inert to reverted builds.

## 5. Not in scope

- **`app_start_to_open` (2,892 ms)** — Flutter app startup before Mission Control opens.
  Unowned; not a Mission Control phase.
- **The warm snapshot build (2.4 s live)** — doc 14's remaining items (per-domain store read
  caches, one scan per build) own it; BW-H1 deliberately changes only the cold path.
- **The ~1.5 s ready→hydrate-start gap** — provider rebuild + request framing; smallest
  attributed term, measure again after BW-H1 before owning it.
- **The page-open write storm** (gesture plan §10.3 item 9) — every open re-upserts 11 actors
  + a surface write; it is why boot flushes exist at all. Still the largest un-investigated
  behaviour on this surface. Unowned; BW-L6 makes its boot-window half hold instead of
  refuse, which is treatment, not cure.
- **Argv-lane deletion** (R#42 / TC-3-TC-4) — BW-L5 removes a PROBE, not a fallback lane; the
  write-lane fallback ledger is untouched.
- **The SQLite 3.40.1 WAL-bug upgrade** — real, owned by `hermes update` tooling, and not a
  boot cost.
- **Serve-side `harness health` verb** — stays; `hermes doctor` and the settings probe ladder
  (`mission_control_hermes_visibility.dart`) still use it.

## 6. Adversarial pass — what I most expect to be wrong

1. **The cold-boot reconstruction rests on one cold sample.** Every number reconciles for
   2026-08-17 05:48, but the split (how much of 20.4 s is purge+recompile vs plain cold
   page-in vs contention) is inferred, and BW-H2's Perf claim inherits that. BW-0 exists
   because of this; if a later attributed cold boot shows the sweep share is small, BW-H2
   demotes below BW-L4 in value order (it stays correct — the double-purge race is real
   regardless).
2. **A-1: the probe child is the second purge/discovery process.** Two processes are in the
   log and only two candidates exist in the code path (probe + serve; the office argv arms
   provably spawned nothing — Correction 5). If some third process (a settings-drawer probe?)
   was live, BW-L5's contention claim weakens but nothing else moves.
3. **BW-H1's coverage-gated replay could demote every real boot** if boot batches always
   carry an uncovered event class (Plan E's A-1 twin). The stage's acceptance requires one
   live cache-served boot; if it demotes, the fix is widening fold coverage (Plan E's
   program), not widening the cache's trust.
4. **A-2: the post-plan targets in §2.1** are phase arithmetic, not measurements, and are
   labeled so. No test asserts them.
5. **The 45 s escalation bound (§2.2)** is derived from ONE cold `spawn_to_ready` (21.9 s).
   If BW-H2/H3 shrink cold boots, 45 s becomes generous — harmless (escalation is copy, not
   state); revisit the constant when receipts justify it.
6. **Unverified live, all of it** — same confession as every plan in this family: no gesture
   in this session touched the running launcher, no serve child was spawned (the constraint
   forbidding it is the same one that made the log forensics possible), and every launcher
   claim is code-read plus receipt-reconciliation, not instrumented re-run.

## 7. Verification log

| # | Fact | How established |
| --- | --- | --- |
| G-R1 | Repos at hermes `7a145cd254` / launcher `2597bdd86` | RAN `git log --oneline -15` both |
| G-R2 | Serve boot self-stamp: cold `interpreter_ms=20421`, warm 1933–2092; post-import phases 1,437 ms | LOG agent.log 05:48:44.942 + three 08-16 lines |
| G-R3 | Hydrate build 19,937 ms ending 240 ms before authoritative; warm build 2,406 ms | LOG 05:49:06.344/.357/.369, 05:49:09.098 |
| G-R4 | Two processes each cleared ~175 `__pycache__` dirs, checkout `b6f11b04c5→7a145cd254`, no lock | LOG 05:48:33.926/.938; READ main.py:5194-5237, :11197 |
| G-R5 | Two 54-plugin discovery passes during import, pre-`booting` | LOG 05:48:39.7-40.4 ×2; READ plugins.py:235, serve.py:1496-1508 |
| G-R6 | Spawn permit falls back to presence while verdict null; spawn argv is constant; no health dependency | READ mission_control_provider.dart:662-689, :64-77; serve_session_io.dart:1056-1076 |
| G-R7 | Receipt spans anchor at `open_requested`; `gate` = open→health_resolved; no ready→authoritative span | READ mission_boot_timeline.dart:269,283-291,310-423; cross-check first_frame/authoritative timestamps exact |
| G-R8 | Pre-proof office writes refuse in-process at the readiness chokepoint; zero spawns | READ mission_control_provider.dart:307-333; mission_action_readiness.dart:53-59,100-151 |
| G-R9 | `booting` emitted before all heavy boot work; phases named booting→ready | READ serve.py:965-1052,1404-1495; test_harness_serve.py:44-48 |
| G-R10 | Update check local-git only, 6 h cache, absent from serve path | READ banner.py:262-345,514-527; main.py:5011-5022; grep of serve.py |
| G-R11 | Sweep awaited before `Process.start`; serve-portion invariant only; 2.98 s measured this boot | READ serve_session_io.dart:1059,1166-1181; reaper:115-130; LOG-derived timestamps |
| G-R12 | `isSettled`/retire predicates and Plan E's §2.4 authority | READ layout_controller.dart:395-396,433-434,467-468; mission_control_page.dart:426,2990-3006 |
| G-R13 | Banner latch: `_setStatus` writers only in the write lane; nothing health-driven clears it | READ layout_controller.dart:653,664,1140,1154,154-190,286-306; sync_strip.dart:28-93 |
| G-R14 | Build coalescing + `accept_inflight` watermark contract; wire core 822 KB ≈ 5 ms serialize | READ snapshot.py:255-388; doc 14; gesture plan §10.1 (MEASURED there) |
| G-A1 | The second cold process is the health probe child | Assumption (two candidates in code; argv arms eliminated) — §6.2 |
| G-A2 | Post-plan targets in §2.1 | Phase arithmetic, labeled, untested — §6.4 |
