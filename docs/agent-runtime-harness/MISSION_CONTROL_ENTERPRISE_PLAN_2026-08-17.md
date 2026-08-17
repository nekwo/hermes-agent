# Mission Control + hermes to enterprise grade — the consolidated staged plan (Plan EG, 2026-08-17)

> **Home.** `docs/agent-runtime-harness/`, above every plan it consolidates. Repos as read:
> hermes `0e125dcf64` (main, pushed), launcher `6a121cbe9` (main, + boot stages, pushed).
> **This is a consolidation, not an investigation.** Every finding below is owned by a source
> document and cited by shorthand; nothing was re-derived, no code was changed, nothing under
> `X:/Eternia/.hermes/` was touched. Where two sources disagree, the reconciliation is stated
> and the source of the ruling named.
>
> **Source shorthands.** RD = `REFACTOR_DEBT_AUDIT_2026-08-17.md` · HY =
> `BOOT_HYDRATE_SECOND_READ_2026-08-17.md` · HC = `HUD_CHIPS_INVESTIGATION_PLAN_2026-08-17.md`
> · G/BW = `MISSION_BOOT_WINDOW_PLAN_2026-08-17.md` · C/TC =
> `SINGLE_TRANSPORT_COLLAPSE_PLAN_2026-08-16.md` · D/CI = `CORRELATION_ID_PLAN_2026-08-16.md`
> · E/WV = `OFFICE_WRITE_VERBS_RPC_PLAN_2026-08-16.md` · F/FC =
> `OFFICE_FOLD_FENCE_CONTENTION_PLAN_2026-08-16.md` · UP =
> `UNIFIED_GESTURE_PREDICTION_PLAN_2026-08-16.md` · PL =
> `PROVIDER_LOGIN_FIRST_CLASS_PLAN_2026-08-16.md` · CO =
> `AGENT_CONSOLE_DEAD_LANE_AND_LIMITS_PLAN_2026-08-16.md` · B =
> `PERSONA_PROFILE_BINDING_AUTHORITY_PLAN_2026-08-16.md` · UC =
> `UNIFIED_AGENT_CREATE_CALL_PLAN_2026-08-16.md` · SCOUT-L / SCOUT-H = the 2026-08-17
> launcher lane-map and hermes swallow-audit scout inventories (session scratchpad).

**Disposition tags** — **LANDED** (already on main; foundation, not a stage) · **ADOPTED**
(stage taken unchanged; the source doc is the spec of record; this doc adds only sequencing)
· **MERGED** (two or more source stages reconciled into one; the reconciliation is stated) ·
**NEW** (no source stage; full anti-vacuity spec carried here) · **REAP** (deletion with its
prove-dead receipt) · **DEFERRED** / **HELD** / **REJECTED** (with the reason).

**Standing rulings this plan is written under.** **#60** — direct, first-time, efficient: the
primary path is the ONLY path for the ordinary flow; a fallback that fires in normal operation
is a main-path bug; no retry ladder doing a correct first call's job; witnesses assert
counts/ordering, never elapsed-ms. **#45** — a missing answer is a LOUD error, never a quiet
degrade. **#42** — fallbacks prove they are dead (receipts, not reasoning), then get deleted.

---

## 0. Verdict up front — the shape of the whole

**What is already the foundation** (git log, both repos, this week): the office write verbs on
RPC — remove, surface.update with normalized-echo adoption, the `office_surface` fold behind
its token (WV-H1/H2/H3, WV-L1..L5); the unified agent create with the roster refusal and its
CLI (UC-H1..H4); the fold-fence fix — one fold chain, a fence that names the writer, a
resubscribe that says why it came back (FC-0/FC-L2/FC-H1); the boot stages — named segments,
single-winner bytecode sweep, import-path discovery deferral, sweep-split, serve-ready-as-
verdict, the pre-proof hold, the honest banner (BW-0/H2/H3/L4/L5/L6/L7); provider catalog v3 +
the S1 401 fix + the console's dead-lane honesty (PL-1/2/3, S1–S4); the profile-binding
receipts (B-1/B-4/B-5). Treat all of it as ground, not as stages.

**The acceptance bar.** Every stage below names which of the eight properties it moves:

| # | Property | This week's witness for why it is the bar |
| --- | --- | --- |
| 1 | Survives more than one writer | held writes mask peers wholesale + unbounded on terminal (#59); apply_office_pull double-place (#33) |
| 2 | Recovers without the operator | gateway unsupervised since ~Aug 1 (#55); parked fold lane has no un-park; `onReconnected` has no caller |
| 3 | Cannot lose a write silently | empty patch frames advance watermarks folding nothing; CLI-poll stale fold base (Hazard A); baseline `or 0` |
| 4 | Instruments attribute, not just bound | the 24 s span exists but cannot say what is inside it; no end-to-end correlation id (#53) |
| 5 | Upgrades are safe | checkout change raced two full recompiles (H2 fixed the race; the class remains); schema version nothing reads |
| 6 | One lane per question | the week's dominant defect class — six confirmed instances; ruling #42 |
| 7 | Boot is a product characteristic | 29.9 s warm today, 82% in one phase |
| 8 | Readability, with teeth | a readability stage must name the defect class it prevents; "an identical sibling copy nobody can find" IS one (S1's siblings; the class-key fence as three copies). One question, one place, findable. Line count is not the target |

**The centerpiece** (operator-set, Phase 3): the persisted read-model core validated by a
**stat fingerprint**. The operator's own framing: *"the 23 seconds IS validation — done by
reconstruction; make validation cost what validation costs."* Fingerprint = (path, mtime_ns,
size) over ALL build inputs, SQLite DBs included, with directory-level enumeration so ADDED
files flip it — sound here specifically because every durable write goes through
`atomic_json_write` (temp+rename always moves mtime). Match → load the materialized core,
serve, authoritative in ~2 s. Mismatch → serve LABELED stale (the banner exists) while
rebuilding; write the cache back after EVERY build (today `snapshot.json` is two days stale
and serve never reads it). Two representations, ONE authority: the store decides; the
projection serves; an unvalidated projection wears the stale label loudly; the memory copy
never deletes, refuses, or wins a conflict on its own say-so (the mass archive was a
projection with store powers). BW-H1's event-offset key stays REFUSED for cause (events are
3 ms of a 5,485 ms build; `running_work.py:454-467` and `board_sync.py` mutate with NO event;
two shipped incidents — HY §3 HY-H1 constraint 3, HC §5). `serve.py:401
_runtime_state_fingerprint` is the existing mechanism to extend.

**Convergences carried, not reopened.** (a) The "three concurrent hydrates" were ONE build
plus riders logging their waits — HY §1.2 and HC §0.3 converged independently; settled.
(b) Both derived the SAME prewarm fix — defer the provider prewarm behind the first build
(HY-H2 = HC-H3); adopted ONCE as EG-3.2. (c) The honest divergence: the fourth (3,389 ms)
build — HY-L2 attributes it to the watermark-equal `push:full_core` resubscribe restarting the
shared producer; HC says the trigger is unrecorded pending HC-0. Resolution: EG-2.1's
attribution lands BEFORE EG-3.3's fix, and EG-3.3's commit message carries whichever trigger
the receipts name. (d) `office.actor.restore` is NOT dead (operator recovery lane, restored
the 2026-08-15 mass archive); `runtime.office.resolve_conflict` NEVER landed (exactly 7 RPC
methods) — both scouts' disagreement resolved (RD §0 Correction 4, SCOUT-H §4); carried in
the keep-list and EG-4.5 respectively.

**Phase order and why.** Stop active damage → close silent-loss holes → attribution &
recovery → the fingerprint core → lane collapse + reaps → multi-writer safety →
diagnosis-loss + readability sweeps. The core sits at Phase 3, not Phase 1, for three reasons
stated rather than assumed: (1) HC-H1 races an ACTIVE defect — every test run writes the
operator's live store, the very store the core would fingerprint and fossilize; (2) a
persisted core built over the Phase-1 loss holes would *validate* the losses — RD-H4's
silently-shortened projection would persist as fingerprint-blessed truth; (3) EG-2.1's
receipts are the instrument the core's acceptance is measured with (build_ms and `role=led`
on the receipt). Phases 1–2 are small, parallelizable stages; the core starts the moment
EG-1.5 and EG-2.1 land, not after the whole of Phase 2.

---

## 1. Stage zero — URGENT, before everything

### EG-0.1 — the live-store test leak (ADOPTED unchanged: **HC-H1**, HC §3)

`tests/agent_runtime/test_office_state_patches.py:751` calls `monkeypatch.undo()` mid-test,
unwinding the package's autouse root-isolation pin along with its own cap patch; three lines
later the test writes the **operator's live store** — the leaked actor sits at revision 67
and climbs on every test run; two sibling sites (`test_persona_chat_continuity.py:156` with
physical evidence, `test_mcp_admission_r2.py:327`); the hermetic guard asserts pre-body only,
so nothing reddens. Every agent running tests today makes it worse. **This lands first, alone,
before any other stage in this plan.** HC-H1's spec is complete and adopted verbatim: scoped
`MonkeyPatch.context()` at the three sites, the teardown tripwire in
`isolate_agent_runtime_root`, the structural no-`undo()` gate, and the orphaned-surface
archive verb (shipped, NOT run — live-root surgery stays the operator's hands only).
**Property:** 3 (a write landing where no write was intended is the loss class inverted) and
8 (the fence becomes findable — the teardown names this incident). **Acceptance:** the leaked
actor's revision is frozen at 67 across a full suite run; any later write is a NEW incident by
definition (HC §6.5).

---

## 2. Dead-code reaps — two classes, opposite sequencing (operator directive)

**Class A — provably dead now, no caller. Reaped BEFORE the stages** (Phase 0, after the
EG-0.2 receipts confirm the prove-dead greps at today's SHAs):

| Reap | Evidence of death | Receipt discipline |
| --- | --- | --- |
| The three `--expect-revision` argv lowerings (bridge:4090-4094, 4105-4109, 4130-4134) | no submit site passes the flag; the guard lives RPC-only and EG-5.1 keeps it there (RD §1.6, SCOUT-L §4) | RD-L6 adopted: removal-contract enumeration; R0-c grep recorded in the commit |
| `_fetch_usage_lane`'s `fetch_account_usage` fall-through (`harness.py:3355-3357`) | closed four-provider tuple (`:164-169`); the only surviving route back into the upstream swallow S1 routed around — dead code that is also a loaded regression (RD §0.4, SCOUT-H §4) | delete; unknown id → typed per-lane failure naming the id; second witness: monkeypatched `fetch_account_usage` recorder asserts ZERO calls (carved out of RD-H5 and moved here per the Class-A directive; the rest of RD-H5 is EG-6.1) |
| The test-only fold copies: `missionOfficePatchFoldOnto` (subscribe_lane:183-212) and `MissionReadModel.applyPatchFrame` (read_model:840), both `@visibleForTesting`, no production caller (SCOUT-L §1) | two more copies of the two-phase fold shape — property 8's named defect class | re-point their tests at the production FC-L2 chain FIRST (the tests must red under the FC-L2 fence mutations, proving they now watch the lane that runs — the RD-L2 incident-repro lesson), then delete; removal-contract entries. Honest exit: if re-pointing shows a test genuinely needs the narrow seam, that copy STAYS, renamed and documented, and its contract entry is dropped |

**Class B — live fallbacks. Reaped AFTER their proving stage, per #42** (Phase 4):

| Reap | Proving stage / gate | Receipt |
| --- | --- | --- |
| The argv office write arms (upsert/remove/surface `Unavailable` arms) | EG-4.0 closes the `laneAbsent` window (their last ordinary traffic); EG-4.3's window | `write lane: N rpc, M cli` — `cli` with reason ≠ `serve_absent` = 0 over the agreed window |
| The NDJSON `harness stream` child (argv PRIMARY path) | **HARD precondition: EG-1.3 (RD-H1, the #57 sink fix) live + R0-a confirming folder-only frames arrive on the push lane** — the child is today the SOLE carrier of folder-only changes; AND EG-1.4 (RD-L1) — the poll lane the deletion falls back to must have lost its silent-discard tooth first | TC-3 exit criteria; `lane=argv` activations with reason ≠ `serve_absent` = 0 |
| The disk-cache boot paint (`snapshot.json` cached lane, bridge:1186-1209 — the orphan with "no freshness gate, no receipt, no owner", SCOUT-H §2) | superseded by EG-3.1: the fingerprint core IS the fast first paint, validated and labeled | one release of receipts showing the cached lane never painted after the core landed |

**Keep-list — looks dead, is not; nobody deletes these:** `office.actor.restore` (operator
recovery lane, no UI caller by design — RD Correction 4); the mass-archive tripwire
(deliberate backstop; retirement is UP-4's call and this plan recommends keeping it
regardless — E §0 Correction 1); `missionOfficeRpcFlag` (kill switch, default true — SCOUT-L
§4; its `gateClosed` arm gets default-build coverage when touched, not deletion).

---

## 3. Phases and stages

No stage requires simultaneous deployment; every stage rolls back by reverting its commit.
Every ADOPTED stage's source doc is its spec of record — tests, mutations, and probed fields
live there and are not restated; MERGED and NEW stages carry their spec here.

---

### Phase 0 — stop active damage. *Observable: the live root is byte-stable across a full test-suite run; the Class-A symbols are enumerated in removal contracts.*

- **EG-0.1** — the live-store leak (§1 above). First, alone.
- **EG-0.2 — receipts before belief** (MERGED: **RD-0** + the surviving half of **TC-0**).
  Read-only, both repos + operator logs. R0-a: decode ONE real folder-change batch and one
  page-open batch — does a folder-only batch promote un-coalesced in the field (prices
  EG-1.3's headline and gates the stream-child reap)? R0-b: collect `write lane:` /
  `fallbackReasons` / `REVISION MISS` receipts — prices EG-5.1's refusal exposure, classifies
  the argv arms' ordinary-traffic share, and **prices EG-4.0's `laneAbsent` window**. R0-c:
  the Class-A prove-dead greps at HEAD. R0-d: `patch_gap` / `fold:gap` receipts — prices
  EG-1.1/EG-2.2 urgency. TC-0's contention hypothesis is already retired by HY §1.2 (one
  process, coalesced, sequential double-builds — 7 offsets × 2 in the live window); its one
  surviving question (how often Lane A runs as a real child) folds into R0-b. **Property:** 4.
- **EG-0.3 — the Class A reaps** (§2 table; REAP; adopts **RD-L6**'s discipline for all
  three). **Property:** 8, 6.

### Phase 1 — close the silent-loss holes. *Observable: an empty `patch` frame is unrepresentable; a folder-only rename folds on the push lane with no resubscribe; an unreadable event log refuses typed instead of minting baseline 0; the projection carries `actors_unreadable`.*

All five are ADOPTED unchanged from RD; ordered cheapest-independent first (RD §4.5).

- **EG-1.1** = **RD-H2** — an unreadable event log stops becoming baseline 0
  (`serve_rpc.py:692-695` `or 0` → typed transient `baseline_unavailable` refusal; the
  honest None-handler already exists one module over). Kills the hydrate→resync→restart loop
  at ~822 KB/lap. **Property:** 3, 2.
- **EG-1.2** = **RD-H3** — an empty patch frame can never promote; the guard lands at the ONE
  frame-builder chokepoint, closing all five producer-side re-opening paths at once; producer
  swallows gain receipts in the same commit. **Property:** 3.
- **EG-1.3** = **RD-H1** — one scope authority for the office push sink (task #57):
  `office_patch_scope` beside the id builders; the sink and `_delta_touches_workspace` both
  call it. After this, a batch the coverage authority promotes is by construction FORWARDED or
  RESYNCED, never dropped — and the NEXT covered office entity cannot re-open the gap
  (the second witness enumerates coverage's own constants). **Hard-before** the stream-child
  reap (Class B). **Property:** 3, 6, 8 (one question — "is this row office-scoped?" — one
  place).
- **EG-1.4** = **RD-L1** — a full-core apply moves the fold base or invalidates it (Hazard
  A): base-less apply nulls `_lastCore`; the next patch takes the existing typed resync arm
  instead of folding onto superseded state. Precondition for the poll-lane's role in the
  stream-child reap. **Property:** 3.
- **EG-1.5** = **RD-H4** — the office projection counts what it could not read
  (`actors_unreadable` on the shared chokepoint both readers use); an unreadable archive
  refuses typed instead of silently resetting the revision guard token to 1. **Hard-before
  EG-5.1** (the guard is only as honest as its token). **Also a precondition for EG-3.1**:
  a persisted core must not fingerprint-bless a silently-shortened projection. **Property:**
  3, 1.

### Phase 2 — instruments attribute; lanes recover. *Observable: one `role=led` line per build and every wait line carries `build_ms`; a serve respawn yields exactly one resubscribe with `reason=reconnect`; one grep on a gesture's token yields send → write → fold across both logs.*

- **EG-2.1 — the build leader becomes visible** (MERGED: **HY-0** + **HC-0** — the two plans
  specified the same stage from the two ends of the wire; reconciled here into one).
  Union of the two specs, no conflicts: hermes — the coalesce leader logs ONE
  `snapshot_build_core role=led build_ms=… sections_top=…` line per actual build (HY-0), and
  `build_snapshot` gains the `build_info` out-param with `role=led|rode|shared_next` +
  `caller=` threaded from `stream_frames` (HC-0), so the prewarm — today the most expensive
  build in the boot and the only one with no line — logs itself; hydrate lines rename
  `elapsed_ms`→`waited_ms` (old key kept one release) and carry the underlying `build_ms`;
  launcher — the authoritative receipt carries `build_ms` + top-3 `sections_ms` off the
  envelope (parse, no protocol change), and each stream attachment logs op + purpose at
  subscribe, closing the third-rider census gap. Tests: both sources' killing mutations adopted
  as written (HY-0's led-count + invocation-counter pair; HC-0's three-caller role matrix —
  each kills a drift the other is blind to). **Property:** 4. Settles HY/HC assumptions A-1/A-2
  as recorded facts.
- **EG-2.2** = **RD-L3** (ADOPTED) — the push lane learns the serve child came back: the
  session lifecycle owner invokes the already-built `onReconnected()` on the died→ready edge.
  Today's recovery is whatever unrelated resync notices first — #60 clause 2 verbatim.
  **Property:** 2.
- **EG-2.3 — the correlation id, minted and threaded** (ADOPTED: **CI-0 + CI-1 + CI-2 +
  CI-3**, Plan D — the pipe's read side already exists end-to-end; the missing 40% is minting
  and threading). One note from the editor: Plan D's ownership caveats ("`state_patches.py`
  OWNED tonight", CI-1 split a/b) are stale — no branch holds those files; CI-1 lands whole.
  CI-3's acceptance — ONE grep over two logs replaces timestamp anchoring as the sanctioned
  diagnostic — is this phase's exit witness. **CI-4** (widening to `runtime.agent.create`
  joins for free — its params already reserve the key; the 38 argv capability lanes are
  DEFERRED to Phase 6's tail, after the lane collapse shrinks the surface it would have to
  thread). **Property:** 4 (#53).

### Phase 3 — the fingerprint core. *Observable: a fingerprint-match boot's receipt shows `ready_to_authoritative` ≈ 2 s with `core_source=cache`; a mismatch boot paints LABELED stale, then exactly ONE `role=led` rebuild; the provider prewarm's discovery/check_fn storm appears AFTER the build line.*

#### EG-3.1 — the persisted core, validated by a stat fingerprint (NEW — supersedes the REFUSED BW-H1; adopts HY-H1's constraints 1–2 and BW-H1's transport-shaped tests; full spec here)

**Property:** 7 (the ~20 s per-process first build leaves every boot), 5 (a build-stamp
mismatch demotes honestly — an upgrade can never serve the old install's core), 2 (a mismatch
serves labeled stale instead of nothing).

**Design** (the operator's, §0): 

- **Fingerprint.** Extend the `serve.py:401 _runtime_state_fingerprint` family into the build
  layer: sorted (path, mtime_ns, size) tuples over ALL build inputs — the agent-runtime store
  root with directory-level enumeration (added files flip it; the boards-tree per-card stat
  pattern already exists), the `running_work` stores via `running_work_store_paths` (the one
  path authority — do not duplicate the list), SessionDB `state.db` **plus its `-wal`/`-shm`
  siblings** (a WAL commit that has not checkpointed must still flip the key), and the
  profile/skill inputs `agents_readiness` reads (profile YAML, skill registries, content-hash
  inputs — the input closure is derived from the build's own readers, and the equivalence
  golden below is the completeness guard). Soundness ground: every durable write goes through
  `atomic_json_write` — temp+rename always moves mtime.
- **Write-back after EVERY successful default-store build:** persist the core's wire form +
  a sidecar `{fingerprint, build_stamp, contract_versions, event_offset}` atomically under
  the store root. Today `snapshot.json` is written only by `write_snapshot` (not on the serve
  path) and serve never reads it — this stage makes serve both writer and reader.
- **First build of a process:** compute the fingerprint. Sidecar match (fingerprint AND
  build_stamp AND contract versions) → load the core, serve it **authoritative**, ~2 s.
  Mismatch → serve the persisted core immediately **LABELED stale** (the launcher's stale
  banner lane exists; the label is an envelope field, and a stale-labeled frame is never
  `authoritative`) while the full build runs; on completion, replace and write back.
- **One authority:** the store decides; the projection serves. The cached/stale projection
  never deletes, refuses a write, or wins a conflict on its own say-so. No event-tail replay:
  the fingerprint decides validity, full stop — a replay would be a SECOND validity authority,
  property 6 applied to the cache itself, and the event-offset axis is exactly what was
  refused (non-evented writers, two shipped incidents).
- **Shadow-validation window** (the UP-4 pattern, applied to this cache): for the first
  release, cache-hit boots ALSO run the full build in the background and compare
  field-for-field; a divergence is a loud receipt naming the differing section AND the
  rebuilt core is adopted. The window converts the input-closure risk (§6.1) into receipts;
  its retirement criterion is TC-3-shaped — zero divergence receipts across the agreed window.
- **HY-H1 constraint 2, adopted:** the cache-hit path must not import `model_tools` (it
  re-buys ~1.3 s + the check_fn storm).

**Tests & anti-vacuity** (new `tests/agent_runtime/test_core_fingerprint_cache.py`, reusing
the serve-loop harness and the injectable `fingerprint` param `serve_loop` already has):

1. `a fingerprint-match boot serves the cache and reads no store` — build once (writes core);
   fresh build context, unchanged fixture. *Mutation:* always rebuild but stamp
   `core_source=cache`. *Probed fields:* (a) the envelope's `core_source == "cache"`, AND
   (b) an injected store fake whose walk/read functions COUNT invocations — asserted ZERO.
   *Why the mutant cannot also set them:* the counter lives in the test's fake; a rebuilding
   mutant must call it. (BW-H1's two-witness shape, re-keyed to the fingerprint.)
2. `a changed input rebuilds and serves the new value` — atomically rewrite one store file,
   driving row X to a second distinct value. *Mutation:* serve the cache on mismatch. *Probed
   fields:* `core_source == "rebuilt"` AND served row X equals the NEW value — which the
   persisted core, written before the change, provably does not contain. Two driven values
   across fixtures; a constant matches at most one.
3. `an ADDED file flips the fingerprint` — add a new store file, touch nothing else. *Mutation:*
   fingerprint only previously-known paths. *Probed field:* rebuild triggered AND the added
   entity present in the served snapshot — absent from the cache by construction.
4. `a SessionDB-only mutation flips the fingerprint` — write one row through SessionDB's own
   writer (WAL path). *Mutation:* skip DB files in the stat set. *Probed field:* rebuild
   triggered; the mutant's fingerprint is unchanged by construction and serves the cache,
   convicted by the `core_source` probe.
5. `a mismatch serves LABELED stale first, authoritative after` — gate the rebuild on a
   test-owned gate (the BW-L5 never-completing-fake pattern). *Probed fields:* frame 1 arrives
   WHILE the gate is held and carries the stale marker + is not authoritative; after release,
   frame 2 carries no marker. *Why not settable:* a mutant that blocks until rebuilt cannot
   deliver frame 1 while the test holds the gate; a mutant serving unlabeled fails frame 1's
   marker probe.
6. `every build writes back` — three-context sequence: build, mutate+rebuild, unchanged third
   context must be `core_source=cache` against the SECOND build's content (probed by row X's
   second value). *Mutation:* write back only on boot builds — the third context rebuilds or
   serves the first build's X; either reds a probe.
7. **Equivalence golden (the authority guard):** for the same fingerprint, the cache-served
   core ≡ the rebuilt core field-for-field (modulo build stamps). This is what makes an
   input-closure gap red inside the fixture matrix instead of silent in the field — and the
   shadow-validation receipts are its live twin.
8. `a cache-hit boot imports no model_tools` — test-owned `sys.meta_path` recorder, zero
   imports (HY-H1's spec, adopted).
9. Adopted from BW-H1 unchanged: `a build-stamp mismatch demotes`; `a failed cache write
   leaves the build path byte-identical`.

**Mixed pairs.** `core_source` and the stale marker are additive envelope fields; old
launchers ignore them (the stale marker's launcher half is a parse + routing into the
EXISTING stale-banner lane — one small launcher commit, no new UI pattern).
**isSettled/retire:** hermes-side + one launcher parse; no write-lane predicate reachable; a
stale-labeled frame is non-authoritative, so the optimistic override retires no earlier than
today — the safe direction. **Rollback.** Revert; core + sidecar are inert files to a
reverted build. **Perf.** Witnesses assert `core_source`, counts, and ordering — never
elapsed-ms; the seconds are read off EG-2.1's receipts (#60).

#### EG-3.2 — the provider prewarm follows the first build (MERGED: **HY-H2** = **HC-H3** — two independent investigations, same fix; adopt once)

One daemon thread, sequential: `snapshot_prewarm()` then `_prewarm_provider_runtime()`, each
exception-isolated; still started before `ready` so the build leads the launcher's first
request. The brief's one-line fix (reorder the two `Thread.start()` calls) stays REJECTED as
a no-op — both sources refused it independently, for the same mechanism (HY §0 "WRONG FIX",
HC §0.3). Union of the two test specs: HY-H2's gate-ordering witness (provider recorder EMPTY
while the test holds the snapshot gate) + `ready` still emitted before either prewarm
completes; HC-H3's injectable-parameter contract. Edge named by both, carried: a chat send
inside the (now shorter) boot window pays the cold SDK inline; if receipts show it biting,
the refinement is "provider prewarm starts at first-request-enqueue OR build-completion,
whichever is first" — not a revert. Note: once EG-3.1 lands, a cache-hit boot has no 24 s
build to protect and this stage's value shrinks to hygiene — it still lands (mismatch boots
keep the full build), but it is priced accordingly (HY §4.2). **Property:** 7.

#### EG-3.3 — the boot resubscribe stops buying a redundant build (ADOPTED: **HY-L2**, sequenced per the convergence note)

**Hard-after EG-2.1** — the fourth build's trigger must be a recorded fact before the fix
claims it (HC's honest objection, carried). Then: the boot-path office subscribe waits for
the authoritative hydrate and subscribes AT its watermark, OR the `push:full_core`
resubscribe adopts the held core without a producer restart when its baseline matches —
either shape leaves FC-L2 fence semantics byte-unchanged for genuine divergence. Witness is
a BUILD COUNT (one `role=led` line in the boot window), never a duration. Collision: same
lane files as FC-L2/FC-H1 — do not land while an unmerged FC branch holds them. With EG-3.1
live the redundant build is ~2 s of cache-load rather than 3.4 s of build — still wrong,
still reaped. **Property:** 7, 6.

### Phase 4 — lane collapse + the Class B reaps. *Observable: TC-3's exit criteria from receipts — `lane=argv` with reason ≠ `serve_absent` = 0, V6-class receipts = 0, office parks = 0 over the agreed window; then the reap commits land with removal contracts; `resolve_conflict` rides RPC.*

- **EG-4.0 — the `laneAbsent` window becomes owned** (NEW — RD §5 and E §4 both name item 10
  as the unowned main-path bug **blocking TC-3's exit**; a consolidation that leaves the
  collapse's known blocker unowned would be a hole). **Property:** 6, #60 clause 1.
  *Priced first:* R0-b (EG-0.2) measures the window's ordinary-gesture share after the boot
  stages + EG-3.1 shrink it; if the share is ~0, this stage closes as measurement and TC-3
  is unblocked for free. Otherwise, the default design: the readiness reason `laneAbsent`
  joins the BW-L6 hold's reason set, so an office gesture inside the window STAGES AND HOLDS
  (seconds, with the receipt and escalation copy BW-L6 already ships) instead of riding the
  argv arm — the hold flushes on the lane-up edge via the existing listener. *Killing test:*
  fake writer answering `Unavailable(laneAbsent)` + a readiness fake; a gesture during
  absence → *probed fields:* the capability repository's recorded intent list is EMPTY and
  the status is `holding` with count 1; flip the lane up → the fake RPC writer RECEIVES the
  held upsert's exact payload (the BW-L6 delivery witness, reused). *Mutation 1:* fall
  through to the argv arm — the capability record is non-empty; the fake it cannot avoid
  calling convicts it. *Mutation 2:* drop the hold on the edge — the delivery witness reds;
  no counter or receipt can satisfy an assertion on the delivered payload itself. *Why the
  pair is anti-vacuous:* the two probes sit on two different test-owned fakes; no single
  mutant can keep both records right without actually holding and then delivering.
  *isSettled/retire:* identical semantics to BW-L6 (pending overlay ⇒ `writesInFlight` true ⇒
  the override does not retire mid-hold); the paint fences pass byte-unchanged — any edit to
  them is a stage-stopping event.
- **EG-4.1** = **TC-1** (ADOPTED) — the hub lane made provably launcher-grade, inert:
  op advertisement (C-1), byte-parity contract test vs argv `harness stream`, the
  interleaving/backpressure pin (C-2), re-subscribe recovery. **Property:** 6.
- **EG-4.2** = **TC-2** (ADOPTED) — the launcher subscribes the hub lane behind the
  advertisement gate; argv becomes the receipted backstop (`lane=hub|argv reason=…`
  taxonomy is part of the definition of done). **Property:** 6.
- **EG-4.3** = **TC-3** (ADOPTED; absorbs **WV-9** and **UC-6** — three plans defined the
  same observation window; it is ONE window with one receipt set). Exit criteria from
  receipts, none from belief. Any nonzero finding is a bug with a receipt attached — fix,
  restart window. **Property:** 6 (#42's second clause).
- **EG-4.4** = **TC-4** + the Class B reaps (§2 table; REAP) — delete cheapest-first:
  launcher argv-stream primary path (its HARD preconditions restated in §2), V6-era special
  cases audited during the window, the disk-cache boot paint, then the argv office write
  arms. Hermes deletes nothing — the stream verb, office lane, and hub remain load-bearing.
  **Property:** 6, 8.
- **EG-4.5** = **WV-H4 + WV-L6** (ADOPTED, Plan E) — `runtime.office.resolve_conflict`: the
  last office write verb off the process-spawn lane; not a fallback deletion but the
  unfinished main path (RD Correction 4). Plan E §2.2's method shape is the contract
  (no `allow_class_key` on the wire — a wire parameter is not consent). **Property:** 6.

### Phase 5 — multi-writer safety. *Observable: every office write arm spends the revision it tracks (stale peers refuse, retract, corrective-read); the hold bills what it masks (`hold-mask` receipts + `maskedCount`); UP-1's completeness gate — the override machinery is DELETED, not accreted onto; one write per drag.*

- **EG-5.1** = **RD-L2** (ADOPTED; subsumes Plan E's deferred **D-W1**, promoted on ruling
  #60 clause 1) — the remove and surface arms spend `expect_revision`; stale refusals take
  the existing terminal path counting `refused` AND `rolledBack`; the mass-archive incident
  repro gains the RPC-arm variant (today it watches only the argv fallback — a regressed RPC
  arm leaves it green). **Hard-after EG-1.5** (the token must be honest first). R0-b's
  REVISION-MISS rate decides the rollout note, not the decision. **Property:** 1.
- **EG-5.2** = **RD-L4** (ADOPTED) — the hold bills what it masks: a receipt + `maskedCount`
  the moment a fold delivers a row a held entry is overriding with different content. Bills
  only — the ranking (held vs acked vs remote) and the narrowing of the wholesale staging
  need intent attribution, which is EG-5.3's ledger; this stage produces the field evidence
  that decision needs (#59 escalation, RD §1.5). **Property:** 1, 4.
- **EG-5.3** = **UP-1** (ADOPTED) — the intent ledger: the canvas renders `server state +
  pending intents`; an intent resolves against its own call's reply. The completeness gate is
  the stage's own rule, kept with teeth: when UP-1 lands, `_officeLayoutOverride`,
  `_hasPendingOfficeSave`, the override branch, and `isSettled`'s paint-path caller are
  DELETED — *"if any of those survive UP-1, UP-1 is not done."* The OR-4 paint fence stays
  green byte-unchanged through the rewrite. The agreement witness probes a field the intent
  path does not itself set (the M-L2 vacuity lesson, named in the source). **Property:** 1,
  8 (the three-source paint resolver becomes one question in one place).
- **EG-5.4** = **UP-2** (ADOPTED; **UP-0** folded in as its local-echo half, per the source's
  own re-scope — UP-0 is not a standalone stage and must not ship as a bug fix) — a drag
  renders locally and emits ONE intent on release; the `'Moved '` string gate and the
  per-frame mutation stream retire. Gate: write count per drag == 1, asserted as a count.
  **Property:** 1, 3.
- **EG-5.5** = **UP-4 + UP-5** (ADOPTED) — the dual-reconciler agreement window (a
  disagreement is a bug in the ledger, not a reason to keep the diff), then adoption stops
  trusting the client's own content key (the reply carries the server's). The
  hold-narrowing/re-ranking decision (RD §5) is taken HERE, with EG-5.2's receipts and the
  ledger in hand — deliberately not staged earlier, because staging it without attribution
  would be guessing. **Property:** 1.
- **UP-3** (creates predict too) — **DEFERRED**: post-D3 the create lands in ~1 s and the
  remaster-cathedral directive says stop over-building ahead of need; revisit only if
  EG-5.5's window shows operator-felt latency. (REJECTED for now, with the reason.)

### Phase 6 — diagnosis-loss + readability sweeps. *Observable: absence never means two things on the provider surface (a thrown builder is named; the "no signed-in providers" claim prints only when detection RAN); the three silent-skip degrades render their causes; the HUD chips are honest (drops 0 on this runtime, disclosure on hover); the class-key fence lives in ONE place with three-lane refusal parity.*

- **EG-6.1** = **RD-H5** (ADOPTED, minus the Class-A fall-through deletion already taken in
  EG-0.3) — all remaining S1-family sub-shapes: `block_errors` on the visibility payload,
  detector-raised lanes emitted with their class, the degraded envelope never printing the
  false "no signed-in providers" claim. **Property:** 4, 8 (S1's siblings stop being
  unfindable copies).
- **EG-6.2** = **RD-L5** (ADOPTED) — the three silent-skip degrades take their honest
  sibling's shape (chat-context dialog, bare-token paste, models.dev catalog). Per-site
  commits, independently revertable. **Property:** 4.
- **EG-6.3** = **RD-H6** (ADOPTED) — the three seam-parity repairs: one resolver for
  `read_model.enabled` (the exact misplacement class that kept the delta-patch lane dark —
  and the seam EG-3.1's cache preference reads through, so it lands before or with EG-3.1's
  release); the CLI create's roster fault takes the RPC lane's typed refusal instead of a
  traceback; the `_persona_is_unknown` resolver invariant gets its contract-test fence.
  **Property:** 8, 5.
- **EG-6.4** = **HC-H2** (ADOPTED) — a deliberately-retired instance's chat session stops
  counting as an anomaly (`instance_retired`, by-design, declared at the emission site per
  parity.py's own contract); the chip goes 5 → 0 on this runtime with zero live-root writes,
  and stops growing by one per retire. The dangerous-direction mutant (classify every
  unresolved binding as retired) is killed by the source's case-(c) fixture. **Property:** 4.
- **EG-6.5** = **HC-L4** (ADOPTED) — the drops chip gets the disclosure the other two chips
  already have (hop · code · entity_id per anomalous drop; the next chip investigation is a
  hover, not a database dig). **Property:** 4.
- **EG-6.6 — the class-key fence becomes one fence** (NEW — property 8's own named example,
  staged with teeth). Today `class_key_collision` is guarded at THREE call sites
  (`serve_rpc.py:928`, `agent_create.py:794`, `office.py`) around one store, and the rekey
  script itself warns that any new writer reaching `_write_actor` directly is unfenced
  (SCOUT-H §3). **Change:** hoist the guard into the `OfficeStore` write chokepoint
  (`upsert_actor` / the `_write_actor` gate); the three callers keep only their
  transport-shaped translations of the store's typed refusal. `office.actor.restore` and
  `resolve_conflict --take remote` keep their sanctioned-override arms — the override becomes
  an explicit store-level parameter, not a caller-side fence omission. **Tests &
  anti-vacuity:** drive a class-key-colliding write through EACH of the three lanes against a
  store seeded with a colliding class-keyed actor; *probed fields:* all three lanes refuse
  with the SAME typed reason string (compared for equality across lanes) AND the store's
  actor file set is unchanged after each refusal. *Mutation:* delete the store-level fence
  but keep one caller's local copy (the historical shape) — the other two lanes' refusal
  probes go red; a single surviving copy covers at most one lane by construction, and the
  refusal requires consulting the store's class-key index, which a caller-local constant
  never reads. *Second witness:* an enumeration/contract test pinning every production path
  to `_write_actor` through the fenced chokepoint — a fourth writer reds by enumeration.
  *Defect class prevented, named per property 8's rule:* an identical sibling copy nobody can
  find — the next writer cannot ship unfenced. **Property:** 8, 1.
- **EG-6.7 — adopted-by-reference tails** (ADOPTED, specs unchanged in their home plans;
  sequenced here so the consolidation is complete): **PL-4** (Settings performs logins),
  **PL-5** (console consequences), **PL-6** (gated retirement) — the provider surface's
  operator affordance, riding PL-1/2/3 already landed; **B-3** (the launcher's global
  HERMES_HOME demoted to a default in title/copy/docs — the two-homes aggravator CO §0
  measured); **CI-4** (the argv capability lanes' `--correlation-id`, now over the
  post-collapse, smaller surface). **B-2 stays HELD** — the hold was placed at merge
  (`7a145cd254` family) and this plan does not reopen it; B-5's gate results are the record.
  **Property:** 2, 4, 8.

---

## 4. Sequencing constraints

1. **EG-0.1 first, alone, before everything** — it races an active defect; every test-suite
   run until it lands writes the operator's live store again.
2. **The isSettled / retire-condition rule** (has bitten twice; a predicate redefinition
   silently killed optimistic paint for a day — `91fec5ddb`): every stage that touches the
   office write lane states its effect against `writesInFlight`'s three terms, `isSettled`,
   and the page retire condition (current text: RD §1.8, the standing authority). The paint
   fences — `mission_office_optimistic_paint_test.dart`,
   `mission_office_lane_reattach_test.dart`,
   `mission_office_mass_archive_incident_repro_test.dart` — pass **byte-unchanged** through
   every stage except where a stage's spec explicitly owns them (EG-5.3's deletion set); any
   other edit to them is a **stage-stopping event, not a test update**. Stages with a stated
   effect here: EG-4.0 (hold semantics, BW-L6-identical), EG-5.1 (inside `flushing`, argument
   swap only), EG-5.2 (receipt + status field only), EG-5.3/5.4 (the owned rewrite, gated by
   OR-4 staying green), EG-3.1 (stale-labeled frames are non-authoritative — override retires
   no earlier than today).
3. **The #57 hard precondition** on stream-child deletion (EG-4.4): EG-1.3 live AND R0-a
   confirming folder-only frames arrive on the push lane AND EG-1.4 (the poll lane the
   deletion degrades to must have lost its silent-discard tooth). The child is (a)-class —
   ordinary traffic — until then; deleting it earlier is the delete-and-see this program
   keeps getting burned by.
4. **EG-1.5 before EG-5.1** (the revision guard's token must stop silently resetting) and
   **EG-1.5 before EG-3.1's release** (a persisted core must not bless a shortened
   projection). **EG-5.1 before EG-5.2** (both touch the hold/flush seam; L2 changes
   arguments, L4 only observes).
5. **EG-2.1 before EG-3.3** (the fourth build's trigger becomes a recorded fact before the
   fix claims it — the one honest divergence between HY and HC, resolved by ordering) and
   **before EG-3.2's acceptance run** (the A/B is read off its receipts).
6. **File collision map** (rebase-level; never land while an unmerged branch holds the file):
   `mission_office_layout_controller.dart` — EG-4.0, EG-5.1, EG-5.2, EG-5.3 in that order;
   `serve.py` — EG-2.1/EG-3.1/EG-3.2 are textual neighbors of FC-H1's home; the office
   subscribe-lane files — EG-2.2/EG-3.3 vs any FC follow-up; `snapshot.py` — EG-2.1's
   out-param and EG-3.1's write-back both touch `build_snapshot`'s seam — EG-2.1 lands first,
   EG-3.1 rebases on it.
7. **Phases are verification groupings, not a serial gate.** Cross-phase dependencies are
   exactly the ones named above; everything else parallelizes. In particular EG-3.1 may start
   the moment EG-1.5 + EG-2.1 land — it must not queue behind the rest of Phase 2, and the
   launcher stages of Phase 5 must not queue behind Phase 4's observation window (only the
   REAPS wait on the window; the safety stages do not).
8. **Register hygiene** (RD §4.8): when EG-0.2 lands, strike the stale sentences RD §1.7
   enumerates (gesture-plan items 6/9's stale halves, any all-four-verbs-ride-RPC parity
   claim) in place, citing this document — the four-commits-inherited-a-dead-sentence
   precedent.

## 5. Not in scope — what enterprise-grade does NOT mean here

- **Multi-tenancy, RBAC, or a compliance/audit regime.** One operator, one machine, one
  trust domain. "Enterprise grade" here means the eight properties — durability, recovery,
  attribution, honest failure — not seat management.
- **High availability / clustering / a second runtime.** The serve child's death-and-respawn
  ladder plus EG-2.2/EG-3.1's recovery IS the availability story.
- **The TCP socket lane** (undialled; D-C3) and **the argv CALL-half migration** (38
  capability ids → JSON-RPC; the DECISION doc's Stage 2, and the PUSH-vs-RPC fork ruling's
  "upstream owns the better CALL" applies) — deliberately not this plan's.
- **The page-open write storm (item 9)** — every open re-upserts 11 actors + a surface
  write. Named by four source plans as the largest un-investigated office behaviour; still
  unowned. EG-4.0 treats its boot-window symptom; the cure needs its own investigation
  charter, and this plan flags it as the top candidate for the NEXT one.
- **Upstream-owned swallows** (`agent/account_usage.py:893-901`, the auxiliary client's
  mislabeled no-auth error, the gateway/TUI `/usage` routes) — route-around discipline;
  doc 19's ledger.
- **Warm-build internals beyond the fingerprint core** (per-domain store read caches, one
  scan per build) — doc 14 owns them; EG-3.1 changes what a boot pays, not what a rebuild
  costs.
- **The marker-rebinding wart** (a dead instance's chat renders under an arbitrary live
  sibling) — visible-behavior decision, chat-lane identity work (HC §5).
- **`app_start_to_open`** (Flutter startup), the SQLite WAL upgrade (`hermes update`
  tooling), and the mission-lane resurrection (removed 2026-07-30; chat is the only lane).

## 6. Adversarial pass — where this plan is most likely wrong

1. **EG-3.1's input closure is the plan's single biggest bet.** The fingerprint is only as
   sound as the enumeration of build inputs, and the build's true closure is wider than the
   store root (`agents_readiness` walks skill registries, profile YAML, content hashes;
   `running_work` lives under the HERMES home). A missed input serves **unlabeled stale as
   authoritative** — the exact failure class this plan exists to end, inverted. Three
   mitigations are load-bearing, not decorative: the closure is derived from the build's own
   readers (one authority), the equivalence golden reds a gap inside the fixture matrix, and
   the shadow-validation window reds it in the field with a receipt. If shadow receipts show
   divergence, the fix is widening the stat set — never trusting the cache harder.
2. **Ordering the core behind Phases 0–2 delays the operator's biggest felt win.** Defended
   in §0 (active damage, loss-fossilization, measurement), and softened by constraint §4.7
   (the core starts after EG-1.5 + EG-2.1, not after all of Phase 2) — but if Phase 1's
   launcher stages drag, the honest adjustment is to pull EG-3.1 forward past EG-1.1/1.2
   (they share no files with it), not to skip EG-1.5.
3. **TC-3's window may never flatline** if EG-4.0's pricing is wrong — if the `laneAbsent`
   share stays material after the boot stages + core, the hold-extension trades gesture
   directness for lane purity, and #60's "direct, first-time" cuts against a hold that fires
   on every page open. The fallback position is named in the stage: fix the lane's
   availability (arm the RPC writer at page open) instead of holding — decide with R0-b's
   receipts, not preference.
4. **EG-5.3 (UP-1) is the largest adopted rewrite with the oldest evidence** — its source
   plan predates this week's hold/fence work and was corrected twice in one day. Its gates
   are strong (agreement witness probing an independent field; OR-4 byte-green; the
   completeness rule), but its cost is the least priced number in this plan. That is why it
   sits behind EG-5.2's receipts: if the ledger's justifying defect (the unbilled mask) turns
   out rare in the field, UP-1 shrinks to the drag-path subset (EG-5.4's prerequisite) and
   the full subsumption waits.
5. **The Class-A "test-only fold copies" reap may find the copies load-bearing** for fence
   coverage the production chain cannot express — the honest exit (keep, rename, document) is
   named in §2, and taking it is not a failed stage.
6. **This document's own risk:** it is an editor's consolidation of fourteen documents, and
   every adoption inherits its source's "unverified live" confession — no gesture touched the
   running launcher, no serve child was spawned, in any of them. The first live acceptance
   run of each phase is where a source's log-forensics could still be overturned; the phase
   observables exist so that overturning is loud.

## 7. Traceability — every source stage, dispositioned

| Source | Disposition |
| --- | --- |
| HC-H1 / HC-H2 / HC-0 / HC-H3 / HC-L4 | EG-0.1 ADOPTED · EG-6.4 ADOPTED · MERGED→EG-2.1 · MERGED→EG-3.2 · EG-6.5 ADOPTED |
| HY-0 / HY-H1 / HY-H2 / HY-L2 | MERGED→EG-2.1 · SUPERSEDED by EG-3.1 (constraints 1–2 adopted into it) · MERGED→EG-3.2 · EG-3.3 ADOPTED (sequenced after EG-2.1) |
| BW-0/H2/H3/L4/L5/L6/L7 | LANDED (foundation) |
| BW-H1 | REJECTED as specified (event-offset key REFUSED for cause: non-evented writers, two shipped incidents; events 3 ms of 5,485 ms) → superseded by EG-3.1's stat fingerprint; its transport-shaped tests adopted |
| RD-0 / RD-H1 / RD-H2 / RD-H3 / RD-H4 / RD-H5 / RD-H6 | MERGED→EG-0.2 · EG-1.3 · EG-1.1 · EG-1.2 · EG-1.5 · EG-6.1 (fall-through deletion carved into EG-0.3) · EG-6.3 — all ADOPTED |
| RD-L1 / RD-L2 / RD-L3 / RD-L4 / RD-L5 / RD-L6 | EG-1.4 · EG-5.1 (absorbs Plan E D-W1) · EG-2.2 · EG-5.2 · EG-6.2 · MERGED→EG-0.3 — all ADOPTED |
| TC-0 / TC-1 / TC-2 / TC-3 / TC-4 | MERGED→EG-0.2 (contention hypothesis already retired by HY §1.2) · EG-4.1 · EG-4.2 · EG-4.3 (absorbs WV-9, UC-6) · EG-4.4 + Class B reaps |
| CI-0/1/2/3 · CI-4 | ADOPTED as EG-2.3 (stale ownership caveats noted; lands whole) · DEFERRED to EG-6.7 |
| WV-H1/H2/H3, WV-L1..L5, FC-0/L2/H1, UC-H1..H4, PL-1/2/3, S1–S4, B-1/B-4/B-5 | LANDED (foundation) |
| WV-0 | partially discharged; the surviving A-1 (boot-batch composition) folds into R0-a |
| WV-H4 / WV-L6 | EG-4.5 ADOPTED (unfinished main path, not a fallback) |
| UP-0 / UP-1 / UP-2 / UP-3 / UP-4 / UP-5 | folded into EG-5.4 (per source's own re-scope) · EG-5.3 · EG-5.4 · DEFERRED (over-building ahead of need) · EG-5.5 · EG-5.5 |
| PL-4 / PL-5 / PL-6 · B-3 | ADOPTED-BY-REFERENCE, EG-6.7 |
| B-2 | HELD — the hold placed at merge stands; not reopened here |
| HY-H2-brief's thread-reorder one-liner | REJECTED (no-op; both sources independently) |

---

## 8. EG-0.2 receipt outcomes (appended 2026-08-17, same day — the pricing pass ran; spec of record: `EG0_2_RECEIPTS_2026-08-17.md`)

The receipts pass discharged with corrections that BIND the stages below. Implementers read
this section as part of any stage it names.

1. **EG-4.0 CLOSES AS MEASUREMENT.** Post-BW `laneAbsent` ordinary-gesture share is **0 of 5**
   — the boot-window writes were held and delivered whole on RPC (`hold: 5 staged` →
   `write lane: 5 rpc, 0 cli`). No hold-extension is built. **TC-3/EG-4.3 is unblocked for
   free.** All 22 cli activations in the whole log sit on `laneAbsent` lines; cli with any
   other reason = 0.
2. **EG-1.3's headline is one severity HIGHER:** a folder-only batch does not promote
   un-coalesced — it **vanishes with neither patch nor resync** (`serve_office_subscriptions.py:446-456`
   tests `office_actor` only; the resync twin `:311-316` matches; 0 of 85 field folds carry
   `office_surface` while a folder write landed on RPC with no fold after it). §2's stream-child
   precondition "R0-a confirming folder-only frames arrive on the push lane" was unsatisfiable
   as written — it is a POST-condition of EG-1.3 and is now **EG-1.3's own live acceptance
   step** (one real folder gesture after it lands).
3. **EG-5.1's exposure is mechanistic, not rate-driven:** 12 of 12 field `REVISION MISS`es are
   page-open re-upserts predicting revision **1** against servers at 2–30, all
   `lane=rpc guarded=false`. Arming the guard as specced refuses **12/12** — EG-5.1 MUST make
   the re-upsert path spend the revision it actually holds (the read model's), not the constant,
   or hold those writes, before the guard arms. §5's item 9 (page-open write storm) is promoted:
   it is the SOLE measured source of revision divergence.
4. **Class-A reaps: all three CONFIRMED DEAD, with two corrections the reap commit carries.**
   (a) `bridge.dart` has **five** `--expect-revision` lowerings; only the three OFFICE ones
   (:4090-4094, :4105-4109, :4130-4134) are dead — the two BOARD ones (:4009-4012, :4046-4049)
   are LIVE (`mission_board_write.dart:166/:192`, fenced by `mission_board_projection_test.dart:347`).
   A pattern-driven sweep breaks the board lane. (b) the `_fetch_usage_lane` fall-through drifted
   to `hermes_cli/harness.py:3702-3704`. (c) fold-copy re-pointing: `missionOfficePatchFoldOnto`
   is 1 file / 6 sites; `MissionReadModel.applyPatchFrame` is ~58 sites across 7 files plus the
   parity comparator `test/support/mission_read_model_parity.dart:83` — the comparator likely
   takes §2's honest exit (keep, rename, document) since a parity check needs a synchronous
   same-isolate fold by construction.
5. **Receipt-channel facts for later stages:** `patch_gap` reasons route to
   `.../diagnostics/mission_transport/receipts.jsonl` (the diag log words the same event
   `REFUSED gap:`) — greps against the diag log alone return false zeros; the serve-child log is
   **office-blind across 12 MB** (zero `office`/`patch_gap`/`stream_frames`/`write lane` lines) —
   EG-2.1's "log op + purpose at subscribe" is the missing line, and TC-0's survivor (Lane A as a
   real child) stays unanswerable until it lands. Gap counts: 1 `patch_gap` in ~4 days, 0 post-BW.
   Pre-BW window correction to §2: the argv arms did not CARRY the 5 staged writes — they
   **failed** them (refused 20 times across 4 flush cycles, never retried; all 20 write refusals
   in the log are these).
6. **Register hygiene (§4.8) EXECUTED** same day: eight strikes/qualifications across
   `OFFICE_GESTURE_FOLD_PROMOTION_PLAN` (:1120, :1150), `SCOPED_INVALIDATION_PLAN` (:27, :71-73),
   `OFFICE_WRITE_VERBS_RPC_PLAN` (:70-71 qualified, :290-295 marked ANSWERED),
   `UNIFIED_GESTURE_PREDICTION_PLAN` (:166, :243); UP `:322` kept as the incident record per the
   receipts' own flag.

---

*Written 2026-08-17 as the consolidation of record. The source documents remain the specs of
record for their adopted stages; this document owns the ordering, the reaps, the acceptance
bar, and the three NEW stages (EG-3.1, EG-4.0, EG-6.6).*
