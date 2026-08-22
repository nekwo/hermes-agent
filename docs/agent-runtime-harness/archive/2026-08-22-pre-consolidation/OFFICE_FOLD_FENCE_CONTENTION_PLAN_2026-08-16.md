# Office fold fence contention — two lanes, one read model, dedup at the wrong phase (Plan F, 2026-08-16)

> **Home.** Hermes repo, beside `OFFICE_GESTURE_FOLD_PROMOTION_PLAN_2026-08-16.md` (whose
> §10.4 register defines the `R#nn` rows cited here), `CORRELATION_ID_PLAN_2026-08-16.md`
> (Plan D — **spec-of-record for correlation ids**; FC-0 deliberately stops short of them),
> `OFFICE_WRITE_VERBS_RPC_PLAN_2026-08-16.md` (Plan E — owns the `push:full_core` demote
> class this plan explicitly does NOT chase), and `SINGLE_TRANSPORT_COLLAPSE_PLAN_2026-08-16.md`
> (Plan C — owns the lane deletion this plan feeds evidence to and does not perform).
>
> Repos as read: hermes `effb3b5e7f` (main), launcher `ee17f89db` (main) — both verified
> (RAN, `git rev-parse`). Live evidence: the operator's diag log
> `C:/Users/beast/AppData/Local/Temp/eternia_launcher_diag.log` (5,197 lines, read this
> session) and the live event log read READ-ONLY at
> `X:/Eternia/.hermes/agent-runtime/events_archive/events.81417412.jsonl` via the rotation
> manifest (`events_manifest.json`: logical offset = 81417412 + byte position).

**Evidence tags**: **READ** (file:line inspected this session) · **RAN** (command/grep
executed this session) · **LOG** (line quoted from the operator's diag log, timestamps
local) · **EVENTS** (row read from the live event log at a named logical offset) ·
**MEASURED-OP** (a number the operator measured and this session verified against the log) ·
**ASSUMPTION A-n** (unverified; the depending stage verifies first).

---

## 0. Verdict up front — the mechanism, and three corrections to the operator's brief

**The operator's hypothesis is right in substance.** The launcher runs two subscribers that
carry the same rows — the NDJSON `harness stream` child (spawned with `--fold-entities`,
`mission_control_bridge.dart:1424-1449` READ) and the `runtime.office.subscribe` push lane
(`mission_office_subscribe_lane.dart:817-858` READ) — and both fold into **one**
`MissionReadModel`. That duplication is not an accident: hermes forwards the office
subscriber THE WHOLE patch batch on purpose, and the comment that does it names the
launcher's stale gate as the intended dedup (`serve_office_subscriptions.py:407-430` READ:
*"The launcher folds BOTH transports into ONE `MissionReadModel` with ONE sequence and a
`base == held` gate"*). Both lanes even ride one serve child — the stream runner prefers
serve (`mission_control_provider.dart:239-246` READ), so one `StreamHub` fans one
`patch_batch_frame` out to both (`serve_office_subscriptions.py:17` READ: *"one producer,
so a patch cannot exist on one lane and not the other"*).

**The defect is WHERE the dedup runs.** The stale gate runs at `prepareFold`
(`mission_read_model.dart:781-792` READ), but the fold is two-phase: prepare → a ~4 MB
off-isolate re-projection → commit (`mission_control_bridge.dart:1999-2029` READ). When the
same batch arrives on both lanes within one re-projection window (~50–500 ms), **both
prepares pass the gate against the same base**, and what happens next is a delivery-skew
lottery with three outcomes, all present in the log (§1.2). The dedup is a
check-then-act race — TOCTOU — and the "fence contention" is that race being adjudicated
by a fence that only one lane carries.

**Correction 1 — the fence loop is asymmetric, and the un-fenced side is its own bug.**
The brief models a symmetric loop ("each lane's commit fences the other lane's
re-projection, which resubscribes, which bumps it again"). Only the **push lane** fences on
`coreRevision` (`mission_control_bridge.dart:2114,2120,2167-2183` READ). The **stream
lane's** resume fence checks only its child generation (`_streamClaimLost`,
`mission_control_bridge.dart:1613-1624` READ) — so when the push lane commits first, the
stream lane **commits the same batch a second time**: double `coreRevision` bump, double
snapshot publish, duplicate `[MissionFold] applied` lines at the identical span
(LOG 20:51:51.341/.390, §1.2). Nothing corrupts — the rows are identical and the watermark
lands on the same value — but every duplicate invalidates the page a second time and
double-counts the revision, and the same unfenced commit path would regress the watermark
if it raced a full-core hydrate instead of a twin patch (§4 FC-L2 test d).

**Correction 2 — the resubscribe does NOT bump the revision; the loop does not
self-amplify through the fence.** The subscribe reply's baseline is deliberately not
adopted into the read model (`mission_office_subscribe_lane.dart:811-816` READ: `onBaseline`
unwired — "adopting it a second time from here would be two writers for one surface"), and
since O-H5 a non-narrowing rejoin attaches to the running producer **without a restart**
(`serve_office_subscriptions.py:686-695` READ), so a re-subscribe no longer manufactures a
hydrate either. Each fence therefore costs one wasted ~4 MB re-projection, one resubscribe
lap on the backoff ladder, and one server-side office projection under `office_lock` — but
it does not re-arm itself. The ladders in the log are fed by NEW gestures colliding, plus
the separate `push:full_core` demote class (Correction 3).

**Correction 3 — the counts, split honestly.** LOG whole-file tallies (RAN): **67 applied,
14 REFUSED fenced, 1 REFUSED gap, 9 STALE dropped** (the brief's "15 REFUSED" is 14 + the
one gap), and **24 resubscribes: 11 `fold:fenced`, 13 `push:full_core`**. The
`push:full_core` half is a **different defect** — batches demoted by uncovered events
(page-open surface writes among them) reaching the office sink as full cores
(`serve_office_subscriptions.py:349-394` READ) — owned by Plan E (WV-H3 + A-1) and item 9,
and out of scope here. This plan targets the `fold:fenced` half and the duplicate-commit
half. At 20:51:54 the window count reached 5-in-60s (LOG `resubscribe #5 (fold:fenced)`),
i.e. **one more fence inside the window would have PARKED the lane**
(`mission_office_subscribe_lane.dart:721-736` READ) — the fence class alone nearly consumed
the park threshold that exists to stop genuine spins.

**The fix (FC-L2) is a contained launcher change**: serialize the two lanes' fold bodies on
one chain so the second delivery's `prepareFold` runs after the first's `commitFold` — the
stale gate then does exactly the dedup job hermes designed it for, at zero projection cost
for the loser — and extend the commit-time revision fence to the stream lane as a backstop.
No lane is deleted, no wire byte moves, and the fence itself survives for the one job it
still has (a full-core apply landing mid-fold). FC-0 first makes the next occurrence
self-diagnosing by naming the writer that moved the revision — which either confirms this
diagnosis in the field or names the real third writer, and is worth more than any fix built
on an unattributed race.

## 1. The proof

### 1.1 The mechanism, file by file

1. **One read model, one revision counter.** `MissionReadModel.coreRevision` bumps at
   exactly three sites — `applySnapshot` (:371-372), the stream full-core apply (:601-602),
   and `commitFold` (:910-912) (`mission_read_model.dart` READ; the field's own doc at
   :270-313 names the fold-across-`await` hazard this plan is about). The stale gate is
   strict-`>` at prepare (:390-401, :781-792).
2. **One fold body, two callers, two different fences.** `_foldPatchOffIsolate`
   (`mission_control_bridge.dart:1999-2044` READ) is prepare → off-isolate re-projection →
   `claimLost()` → commit. The stream patch path calls it with the **generation** fence
   (:1820-1827); the push path calls it with the **coreRevision** fence
   (:2114-2120, `_pushFoldClaimLost` :2167-2183 — the `[MissionFold] REFUSED fenced` line).
   The push path's fenced verdict is mapped to `needsResync('fenced')` (:2139-2144), which
   the subscribe lane turns into `_requestResubscribe('fold:fenced')`
   (`mission_office_subscribe_lane.dart:675-677` READ).
3. **Push folds are serialized against each other but not against the stream lane** —
   stated in the code itself: *"the stream lane's work is not serialized with this one, so
   that is a live race, not a theoretical one"* (`mission_control_bridge.dart:2074-2084`
   READ; `_pushFoldChain` :2047-2099).
4. **Hermes delivers the identical batch to both lanes by design.** `patch_batch_frame`
   (`stream.py:273-312` READ) goes to the hub; the office sink forwards **the whole batch**
   with the full span rather than a workspace-filtered subset, precisely so that whichever
   lane folds first leaves the other a clean stale drop
   (`serve_office_subscriptions.py:395-441` READ, the §V6 comment). Same serve child hosts
   both subscribers (`mission_control_provider.dart:239-246` READ;
   `serve_stream_hub.py:42-58,519-537` READ).

### 1.2 The three collision outcomes, all in one log cluster (LOG, 20:51 window)

| Skew | What the log shows | Verdict |
|---|---|---|
| Second delivery prepares **after** first commit | `20:51:13.331 applied 88800193->88803230` + same-ms `folded push` (push lane won) → `20:51:13.500 STALE dropped: seq=88803230 not ahead of held 88803230` | **Intended dedup.** The design working. |
| Push in flight when stream commits | `20:51:31.136 applied 88808379->88812292` (no `folded push` companion → stream lane) → `20:51:31.137 REFUSED fenced: core revision moved 25->26` → `.137 resubscribe #2 (fold:fenced)` | **The fence misfire**: the fenced batch had ALREADY been applied by the other lane — the resubscribe re-fetches a baseline nothing lost. |
| Stream in flight when push commits | `20:51:51.341 applied 88818417->88819612` + `folded push` → `20:51:51.390 applied 88818417->88819612` **again** | **The unfenced double-commit**: same span applied twice, two publishes, two revision bumps. |

Every one of the 14 `REFUSED fenced` lines shows a delta of exactly +1 and sits ≤65 ms
after an `applied` line that has no `folded push` companion receipt (RAN, full-log sweep);
the 9 `STALE dropped` lines sit 46–403 ms after an applied at the same span. The outcome
class is pure delivery-skew lottery over the same duplicated batch.

### 1.3 The alternatives, ruled in or out

- **A genuine third writer** (serve prewarm, gateway, second launcher window, background
  hermes): ruled out for the fences observed. The fence compares an **in-process** counter
  with three bump sites (§1.1.1); no other process can touch it. Every fence line's +1 is
  accounted by a same-window in-process applied line (§1.2), and `coreRevision` resets
  between clusters (LOG: 29->30 at 19:34, then 8->9 at 20:05, then 3->4 at 20:48) mark
  repository re-creations, not external writers. Residual: lane attribution above is
  receipt-adjacency inference — exactly the timestamp-anchoring failure shape R#53 names —
  which is why FC-0 records the writer instead of inferring it.
- **"The fence is correct and the fold is just slow"** (window too wide): the window is the
  forced off-isolate ~4 MB re-projection (`mission_control_bridge.dart:2013-2025` READ) and
  cannot shrink to zero; but even at zero the race remains, because the two deliveries land
  within 1 ms of each other (LOG §1.2 row 2). And the fence's VERDICT is wrong for this
  case regardless of width — it resubscribes over a batch that was already applied. Fixing
  the width is neither necessary nor sufficient; moving the dedup to commit order is both.
- **"The two lanes are intentionally separate and something else bumps the revision"**: the
  intent is the opposite — intentionally duplicated with the stale gate as dedup
  (§1.1.4, the §V6 comment). Bump attribution per above.
- **"The oscillating item count is a cause"**: it is not a cause and not a symptom — it is
  real state (§2).

## 2. The item-count oscillation — genuine churn, not a bug

The `— N items` number on each subscribe receipt is the office baseline's item list length
at that watermark. Two facts settle it:

1. **The count is a deterministic function of the offset.** Every repeated baseline offset
   repeats its count exactly (LOG, RAN): `88727134 → 8` (17:40:18, 19:21:55),
   `88765784 → 8` (19:34:11, 19:41:13, 19:42:45), `88772195 → 8` (20:05:39, 20:07:49,
   20:10:14). A scoping wobble or projection race would show the same offset answering
   differently; it never does. (The full range this session was 8–11, not 8–10:
   `88761313 → 11 items` at 19:22:28.)
2. **The changes are real creates and archives.** Growth windows coincide with
   `[MissionAgentCreate] lane=rpc gesture=drop` receipts (LOG 20:51:13/26/30/40 — the
   operator's create burst, MEASURED-OP "13 creates, all lane=rpc"); shrink windows are
   genuine retires, read directly from the live event log (EVENTS, logical
   88761313–88765784): three `persona_instance.retired` events (*"placement removed from
   Mission Office"*, `requested_by: launcher`) each paired with `state.patched
   office_actor remove` + `office.actor.removed (reason: instance_reaped)` for
   `ws_codex-test-workspace_28d285` at 23:34:03/05/10Z — exactly the 11→…→8 slope the
   receipts show at 19:34 local.

The workspace is the codex test workspace under a create/delete test loop. **No stage
addresses this; none should.** FC-0's enriched receipts keep the question answerable in
future sessions without an event-log excavation.

## 3. What was NOT proven, and what settles it

1. **Per-fence lane attribution is inferred, not recorded.** The "no `folded push`
   companion ⇒ stream lane" reading in §1.2 is timestamp/adjacency inference — the R#53
   failure shape. FC-0 settles it: the fence line names the recorded writer. If the
   two-lane diagnosis is wrong anywhere, FC-0's first field session says so by printing a
   writer this document did not predict.
2. **The two mid-cluster `push:full_core` triggers** (20:51:35.924, 20:51:41.921) were not
   attributed to specific demoted batches (candidate: chat/session events from the create,
   or a surface write). Owned by Plan E's WV-0/A-1 boot-batch decode; not re-derived here.
3. **Whether `runtime.office.subscribe` ignores unknown params** (needed by FC-H1's
   additive `reason`) — **ASSUMPTION A-1**; Plan D verified the write handlers read known
   keys only (`serve_rpc.py` D-R7), FC-H1 verifies the subscribe handler specifically
   before landing.
4. **Live confirmation that FC-L2 ends the fence class** — by construction it cannot be
   proven from code; the acceptance in FC-L2 is the operator's next create/delete burst
   showing `fold:fenced = 0` with FC-0's writers printed on whatever remains.

## 4. Stages

Naming: FC = fold contention; H = hermes, L = launcher. FC-0 lands first and alone —
it is the stage that converts this document's inference into recorded fact.

---

### FC-0 — launcher: the fence names the writer that moved the revision (receipts only)

**Goal.** The next `REFUSED fenced` line answers "which lane committed underneath this
batch" by itself, with zero behavior change. Per the operator: if the two-lane diagnosis is
right, this stage alone proves it on the next session; if wrong, it names the actual
writer. Either way it settles the question.

**The resubscribe chokepoint is already unified — do not "unify" it.** All re-subscribes
flow through one door: `MissionOfficeSubscriptionLane._requestResubscribe`
(`mission_office_subscribe_lane.dart:696-756`), fed by one subscribe entry
(`_subscribe(cause)` :434), settled at one point (`_settle` :531), scheduled by one timer
(:750-755), counted in one rolling window (`_resubscribeAt` :352, cap :336, backoff
:294-300). Every cause string in the log (`start`, `fold:fenced`, `push:full_core`,
`reconnect`, `deferred:*`, `fold_threw`) already goes through it — that single chokepoint
is why the ladder was legible enough to diagnose. The gap is **attribution**, not
unification.

**Change surface** (launcher; `mission_read_model.dart` + `mission_control_bridge.dart`):
- `MissionReadModel` gains `coreRevisionWriter` (string) + `coreRevisionMovedTo` (the
  sequence the write landed at), assigned at the three existing bump sites and nowhere
  else: `applySnapshot` → `snapshot_apply`, the stream full-core apply → `stream_full_core`,
  `commitFold` → a caller-supplied tag. `commitFold` takes a `writer:` argument; the fold
  body passes `fold_commit:stream` / `fold_commit:push` derived from the lane it already
  distinguishes (`generation == null` ⇒ push, `mission_control_bridge.dart:2119`).
- `_pushFoldClaimLost` (and FC-L2's stream twin, when it lands) prints them:
  `REFUSED fenced: core revision moved 25->26 (writer=fold_commit:stream at seq=88812292)
  during the re-projection of batch 88808379->88812292 …` — writer, writer's landing
  sequence, and the fenced batch's own span. The `staleUnitFenced` receipt's `detail`
  carries the same fields.
- **No correlation ids.** Plan D (CI-2) owns `corr=` on these lines; its wire slot is
  verified real (`stream.py:221` surfaces `payload.correlation_id` into delta entities;
  patch rows spread payloads verbatim, `stream.py:296` — both READ this session, correcting
  Plan D's stale line numbers 157-168/242-246). FC-0 adds only what no other plan owns:
  the in-process writer identity.

**Tests** (extend `mission_office_push_fold_intake_test.dart`; sink capture via
`missionFoldLogSink` as `mission_fold_log_receipt_test.dart` does):
- (a) *a stream commit during a push fold's re-projection is named*: latch the injected
  reproject, commit a patch via the stream path mid-window, release; assert the fence line
  contains `writer=fold_commit:stream` and the committed sequence.
- (b) *a full-core apply during a push fold's re-projection is named differently*: same
  latch, `applySnapshot` mid-window; assert `writer=snapshot_apply`.
- **Anti-vacuity.** Mutation: stamp the writer as a constant at the fence site (or skip the
  assignment at one bump site). The probed field is the **writer token inside the captured
  fence line**, and the pair of tests demands two DIFFERENT values (`fold_commit:stream`
  vs `snapshot_apply`) produced by two different commit paths. The mutation cannot also set
  it: the fence site has no access to which path committed — that fact exists only in the
  read model's field written at the bump site the mutant bypassed — so a constant satisfies
  at most one of the two tests and the other goes red. (This is the same two-driven-values
  discipline that keeps a witness from passing under its own mutant.)

**Mixed pairs.** None — receipts only, no wire change. **Rollback.** Revert.
**Collisions.** `mission_read_model.dart` is also touched by Plan E's WV-L3
(`_applyOfficeSurfacePatch`) — different functions, trivial rebase; coordinate with the
concurrent `office.surface.update` agent before landing (§5).

---

### FC-H1 — hermes: the subscribe carries its cause, so the server log can join the ladder

**Goal.** Today the server sees a re-subscribe with no way to tell `fold:fenced` from
`push:full_core` from `start` — the client-side cause dies in the launcher log, and joining
the two logs is (again) timestamp inference. One additive param closes it.

**Change surface** (hermes `agent_runtime/serve_rpc.py` + `serve_office_subscriptions.py`):
- `runtime.office.subscribe` accepts optional `reason` (string). Boundary-validated with
  Plan D's covert-channel discipline (V1): ≤64 chars, charset `[a-z0-9_:.-]`, refused
  `-32602 {reason: "reason_invalid"}` otherwise. Passed through to
  `OfficeSubscriptions.subscribe` and stamped verbatim on the subscribe/rebaseline service
  receipt (the `serve_office_subscription_rebaselined` family), printed `-` when absent so
  absence is visible.
- Launcher half: `buildMissionOfficeSubscribeRequest` gains `reason:`;
  `MissionOfficeSubscriptionLane._subscribe(cause)` already carries the exact string —
  thread it through. **Collision (§5): Plan E's WV-L4 edits the same builder** (adds
  `office_surface` to `fold_entities`); land FC-H1's launcher half after WV-L4 or rebase.
- Precondition: A-1 (§3) — verify the fielded subscribe handler ignores unknown params, so
  a new launcher against an old runtime degrades to "reason dropped", never a refusal.

**Tests**:
- hermes (`tests/agent_runtime/test_serve_office_subscriptions.py` +
  `test_serve_rpc*`): subscribe with `reason='fold:fenced'` → the receipt carries exactly
  that token; subscribe without → `-`; over-long/bad-charset → `-32602 reason_invalid`
  before any store/hub call.
- launcher (`mission_office_subscribe_codec_test.dart` +
  `mission_office_subscribe_lane_test.dart`): the request envelope's params carry
  `reason: start` from `start()` and `reason: fold:fenced` from a fenced resubscribe (drive
  the lane's fold to `needsResync('fenced')` with a fake fold and capture the built
  envelope).
- **Anti-vacuity.** Mutation: hardcode the receipt's reason (hermes) or the param
  (launcher). Probed field: the receipt/envelope `reason` under **two driven values plus
  the absent case** — present must echo the exact caller string (which the test chooses),
  absent must print `-`. A mutant constant cannot equal both driven values and the
  sentinel; the echo requirement is what the mutation cannot fake, because the expected
  value originates in the test's own input, not in any code the mutant can edit.

**Mixed pairs.** Old launcher + new runtime: no `reason`, receipt prints `-` — inert. New
launcher + old runtime: unknown param ignored (A-1). **Rollback.** Revert either side
independently. **Does NOT do.** Correlation ids (Plan D), any change to what subscribe
returns or restarts.

---

### FC-L2 — launcher: one fold chain for both lanes; the revision fence becomes a backstop on both

**Goal.** Move the dedup to where it is race-free: serialize every `_foldPatchOffIsolate`
invocation — stream patch frames and push notifications — on ONE chain, so the second
delivery of a duplicated batch prepares against the post-commit base and stale-drops at
`prepareFold`, before paying any re-projection. This converts all three §1.2 outcomes into
the first (intended) one: winner applies, loser drops, zero fences, zero double-commits,
zero `fold:fenced` resubscribes, no park pressure from this class.

**Why serialization and not the alternative.** A re-prepare-on-fence loop (retry the
two-phase fold when fenced) was considered and rejected as primary: it still pays the
loser's ~4 MB re-projection before discovering the duplicate, and it leaves the stream
lane's double-commit unfixed unless the stream also fences — at which point both lanes
carry fence-retry machinery to recover from a race the chain removes outright. The chain
also costs nothing in the common case: fold links only ever wait on other folds, and a
duplicated batch's second fold is work that should not run at all.

**Change surface** (`mission_control_bridge.dart` only):
- Generalize `_pushFoldChain` (:2047-2048) to `_foldChain`; both callers enqueue their
  fold through it. The stream patch branch (:1810-1851) awaits its chained fold inside its
  existing stream work unit — stream wire order is preserved (the unit still completes
  before the next stream unit runs), and no fold link ever awaits stream work, so no cycle.
- The push lane's fence is unchanged. The **stream lane's `claimLost` additionally checks
  `coreRevision`** (captured at its link's start), keeping its existing generation check.
  Its fenced arm stays "discard, no resync" (:1844-1848) — correct, because post-chain the
  only writers that can move the base mid-fold are full-core applies, which supersede the
  patch. This closes the latent watermark-regression hazard: an unfenced stream commit
  after a hydrate would drag `_sequence` and `_lastCore` backwards
  (`commitFold`, `mission_read_model.dart:907-912`).
- The fence is NOT deleted and its verdict is NOT changed: post-FC-L2 it fires only for a
  genuine base move (a full-core apply landing mid-fold), where re-hydrating is honest —
  and FC-0 now prints `writer=snapshot_apply`/`stream_full_core` on exactly those.

**Tests** (new `mission_fold_lane_contention_test.dart` beside
`mission_office_push_fold_intake_test.dart`, reusing its latched-reproject harness):
- (a) *push wins, stream drops at prepare*: deliver the same batch to the push intake, then
  to the stream patch path while the push re-projection is latched; release. Assert: the
  stream fold's outcome is `staleDropped`, **`model.coreRevision` moved by exactly 1**
  across the whole scenario, exactly one `[MissionFold] applied` line and one stale line in
  the captured sink, exactly one snapshot-publish tick.
- (b) mirror of (a), stream first.
- (c) *the fence still guards a genuine base move*: latch a push fold's re-projection,
  `applySnapshot` a full core mid-window, release. Assert the push result is
  `needsResync('fenced')` and the fence receipt fires (with FC-0's
  `writer=snapshot_apply`).
- (d) *no watermark regression on the stream lane*: latch a stream patch fold, apply a
  full core at a HIGHER offset mid-window, release. Assert the fold is discarded and
  `model.sequence` still equals the hydrate's offset.
- **Anti-vacuity.** Mutation for (a)/(b): revert the serialization — give the stream path
  back its own unchained call (the pre-FC-L2 shape). Under the latch both prepares then
  pass the stale gate and both commit. Probed field: **`MissionReadModel.coreRevision`**
  (asserted delta == 1) plus the captured applied-line count. The mutation cannot also set
  it: the counter is bumped inside `commitFold` in `mission_read_model.dart` — a file the
  mutation (bridge chaining) does not touch — and the only way a build keeps the delta at 1
  is by not committing the second fold, which is precisely the behavior under test. The
  publish-tick assertion is the operator-visible twin of the same fact and is counted from
  the repository's `snapshotUpdates` stream, which `commitFold` cannot suppress.
  Mutation for (c)/(d): make the respective lane's `claimLost` return false
  unconditionally. (c) reds on the absent `needsResync('fenced')` verdict — which only
  `_pushFoldClaimLost`'s revision comparison can produce — and (d) reds on
  `model.sequence` regressing to the batch's span, a value written by the very commit the
  mutant failed to fence.
- Regression fences that must pass byte-unchanged: `mission_office_push_fold_intake_test.dart`,
  `mission_office_subscribe_lane_test.dart`, `mission_read_model_patch_test.dart`,
  `mission_stream_fold_declaration_test.dart`.

**Acceptance (operator, live, after FC-0 + FC-L2).** Repeat a create/delete burst like
20:48–20:52's. Expected: `folded push` / `STALE dropped` receipts only; **zero
`resubscribe #N (fold:fenced)`**; zero duplicate `applied` lines at one span; any residual
`REFUSED fenced` names a full-core writer via FC-0. `push:full_core` resubscribes are
expected to REMAIN — they are Plan E's number, and this acceptance must not claim them.

**Mixed pairs.** Launcher-only; no wire change; old runtimes unaffected. **Rollback.**
Revert — behavior returns to the measured baseline, receipts keep FC-0's attribution.
**Perf.** Removes ~one wasted 4 MB re-projection + one resubscribe round trip per
collision (14 + 9 collisions this session); adds one chain hop per fold (negligible; push
folds already chain).

---

## 5. Sequencing constraints

1. **FC-0 → FC-L2 ordering is load-bearing.** FC-0 must be in the field before or with
   FC-L2 so that (i) one operator session can confirm the writer attribution while fences
   still occur, and (ii) post-fix, any surviving fence self-identifies. Landing FC-L2 first
   would fix the symptom while destroying the evidence.
2. **FC-H1 is independent** of both and may land any time; its launcher half must sequence
   against Plan E's WV-L4 (same builder function, `mission_office_rpc.dart`
   `buildMissionOfficeSubscribeRequest`).
3. **Concurrent-agent collision map** (the `office.surface.update` implementation in
   flight touches the launcher `office/` directory and hermes `state_patches.py`):
   - FC-0/FC-L2 touch `mission_read_model.dart` and `mission_control_bridge.dart` — inside
     the launcher `data/` directory, not `office/`; no hermes producer files. Overlap with
     Plan E is WV-L3 (`mission_read_model.dart`, different function) and WV-L4
     (`mission_control_bridge.dart:1449` argv list, different region). Rebase-level, not
     semantic — but do not land while that agent's branch is unmerged without coordinating.
   - FC-H1's hermes half touches `serve_rpc.py`/`serve_office_subscriptions.py` — files
     Plan E's WV-H2 also edits (`serve_rpc.py`); additive methods vs additive param, no
     shared lines expected; rebase after whichever lands first.
   - No FC stage touches `state_patches.py`, `office_store.py`, `patch_coverage.py`, or
     any `mission_office_layout_controller.dart`/`mission_office_rpc_writer.dart` region.
4. **Plan D deference.** Correlation-id minting/printing on these same receipts is Plan
   D's CI-1/CI-2; FC-0 adds only lane/writer identity and must not grow a competing id
   scheme. When CI-2 lands, the fence line carries both (`writer=… corr=…`).
5. No stage requires simultaneous deployment; each rolls back by reverting its commit.

## 6. The lane-deletion question (R#42) — what this plan feeds it, and does not do

The standing ruling is RPC+push only with every redundant read lane deleted **after each
fallback proves it is dead** (R#42; Plan C TC-3/TC-4 own the exit criteria). This plan
deliberately neither deletes nor entrenches:

- **It removes the pressure to delete for the wrong reason.** The fence storm is the
  loudest current cost of running both lanes; FC-L2 reduces the duplication cost to one
  stale-dropped prepare per batch (~µs, no projection).
- **It produces the deletion's evidence for free.** Post-FC-L2, every duplicated batch
  yields exactly one `folded push`-or-`applied` and one `STALE dropped`/`dropped push as
  stale` receipt pair, and FC-0 stamps who won. A Plan C observation window can then read,
  from receipts alone, whether the push lane ever applies a row the stream lane missed
  (the "prove the fallback is dead" obligation) — the same dual-lane visibility Plan D's
  adversarial pass predicted the correlation id would provide.
- **The proof obligation for deleting a lane**, stated so Plan C can lift it verbatim: an
  agreed window in which the candidate lane's fold receipts show `applied = 0` with every
  drop attributed to the surviving lane's earlier commit (not to gaps, parks, or
  `laneAbsent`), across sessions that include create/delete bursts, workspace switches,
  and a serve-child respawn. **Not performed here.**

## 7. Not in scope

- **The `push:full_core` resubscribe class** (13 of 24 this session) — demote-driven; owned
  by Plan E (WV-H3 surface fold, WV-0/A-1 boot-batch decode) and item 9 (page-open write
  storm). This plan's acceptance explicitly excludes it.
- **The item-count oscillation** — proven genuine churn (§2); no fix exists to build.
- **Correlation ids** — Plan D is spec-of-record; the wire slot is verified delivered
  (`stream.py:221,296` READ).
- **Deleting any read lane, or the argv fallbacks** — Plan C (R#42), with §6's obligation.
- **Park-recovery policy** (a parked lane has no un-park path short of a manifest rebuild)
  and the unwired `onReconnected` respawn gap
  (`mission_office_subscribe_lane.dart:840-844` READ) — both pre-existing, both named,
  neither touched. FC-L2 removes this session's dominant park pressure; it does not add a
  recovery lane.
- **Shrinking the re-projection window** or moving the fold on-isolate — §1.3's second
  bullet says why that is the wrong lever.

## 8. Adversarial pass — what I most expect to be wrong

1. **The receipt-adjacency lane attribution** (§1.2) could misattribute an individual
   fence — e.g. a poll-path `applySnapshot` landing in the same millisecond as a stream
   applied line. It cannot survive FC-0: that is the point of FC-0. Nothing in FC-L2's
   design depends on WHICH in-process writer wins the race, only that racing folds of the
   same batch dedup at prepare — which holds for any writer mix.
2. **Chaining the stream patch fold could add latency under sustained patch storms** (a
   push fold ahead of a stream fold in the chain delays the stream unit, and the stream
   backlog tripwire at `_enqueueStreamWork` (:1512-1518) could fire earlier). Judged
   acceptable: the chained work is the same total projection work minus the duplicates,
   and the backlog tripwire firing is a receipt, not a failure. If a real storm surfaces,
   the escape is dropping the loser at DELIVERY (compare the notification span against
   `model.sequence` before enqueueing) — a cheap pre-filter that composes with, and does
   not replace, the chain. Named, not staged.
3. **The stream lane's new revision fence (FC-L2) discards a fold a resync will not
   replace** — if a full-core apply moved the base to an offset BELOW the batch (a forced
   rollback hydrate), discarding the patch is still right (the producer is behind; the
   next frames re-deliver), but this is the least-exercised corner. Test (d) pins the
   forward case; the backward case inherits `applyForcedSnapshot`'s existing
   producer-violation accounting (`mission_read_model.dart:409-440`) and is asserted not
   to regress `sequence` — if implementation finds a live backward path beyond that, it
   owes a fifth test, not a design change.
4. **A-1** (unknown-param tolerance of the fielded subscribe handler) — verified before
   FC-H1 lands, per §3.
5. **Unverified live, all of it** — same confession as every plan in this family: code
   read at the two SHAs, log and event-log evidence from the operator's real session, but
   no gesture in this session touched the running launcher and no serve child was spawned.

## 9. Verification log

| # | Fact | How established |
|---|---|---|
| F-R1 | Both lanes declare `kMissionFoldDeclaredEntities` and fold into ONE `MissionReadModel` via ONE fold body | READ mission_control_bridge.dart:1424-1449,1980-2044; mission_office_rpc.dart:1369-1380; mission_office_subscribe_lane.dart:210-240 |
| F-R2 | Push lane fences on `coreRevision`; stream lane fences on generation only | READ mission_control_bridge.dart:2114-2120,2167-2183 vs 1613-1624,1820-1827 |
| F-R3 | Fold dedup is prepare-time strict-`>`; commit bumps `coreRevision`; three bump sites total | READ mission_read_model.dart:390-401,781-792,371-372,601-602,903-927 |
| F-R4 | Hermes forwards the office subscriber the WHOLE batch, naming the launcher's stale gate as the dedup (§V6) | READ serve_office_subscriptions.py:395-441 |
| F-R5 | One serve child hosts both lanes; hub subscribe re-baselines; office rejoin is restart-free when non-narrowing (O-H5) | READ mission_control_provider.dart:239-246; serve_stream_hub.py:42-58,519-537; serve_office_subscriptions.py:686-695 |
| F-R6 | Subscribe baseline never enters the read model (`onBaseline` unwired) — no fence loop through the baseline | READ mission_office_subscribe_lane.dart:811-816 |
| F-R7 | Session tallies: 67 applied, 14 fenced, 1 gap, 9 stale; 24 resubscribes = 11 fold:fenced + 13 push:full_core; window hit 5-in-60s at 20:51:54 | RAN grep/counts over the diag log |
| F-R8 | All three collision outcomes present, incl. a duplicate `applied` at one span (20:51:51.341/.390) and a 1 ms fence (20:51:31.136/.137) | LOG §1.2 |
| F-R9 | Every fence delta is +1 and pairs with an in-process applied line; `coreRevision` resets mark repo re-creations | RAN full-log sweep; READ mission_read_model.dart:312-313 |
| F-R10 | Item count is deterministic per offset (three offsets, eight repeats) and its slopes match create receipts and real retires in the event log | LOG; EVENTS at logical 88761313+ via events_manifest.json |
| F-R11 | The resubscribe chokepoint is already unified (one door, one window, one cap, one backoff) | READ mission_office_subscribe_lane.dart:434,531,696-756,352,342,294-301 |
| F-R12 | Correlation-id wire slot is delivered (delta entities + patch-row payload spread); Plan D's cited line numbers are stale but the mechanism is real | READ stream.py:206-222,296 |
| F-R13 | R#53 (no end-to-end correlation id) and R#42 (RPC+push only, prove-then-delete) are the registered owners this plan defers to | READ OFFICE_GESTURE_FOLD_PROMOTION_PLAN §10.4 rows R#53, R#42 |
| F-A1 | Fielded `runtime.office.subscribe` handler ignores unknown params | ASSUMPTION — FC-H1 verifies before landing |
