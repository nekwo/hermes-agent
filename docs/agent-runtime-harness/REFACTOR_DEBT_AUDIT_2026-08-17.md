# Refactor debt audit — the structures that keep producing lane-ambiguity defects (Plan RD, 2026-08-17)

> **Home.** `docs/agent-runtime-harness/`, beside Plans C/E/F/G, whose stages this plan
> sequences against and deliberately does not re-own. Repos as read: hermes `ca19df1e48`
> (main), launcher `6a121cbe9` (main) — both verified (RAN, `git log --oneline -30`).
> Live evidence: `X:/Eternia/.hermes/profiles/base/logs/agent.log` (+ rotations) and
> `errors.log`, read-only; nothing under the live root was written, no serve child
> spawned, port 8090 untouched.
>
> **This is a diagnosis-and-plan document.** No code was changed in its production.

**Evidence tags** — `READ` (file:line inspected first-hand this session, at the SHAs
above), `RAN` (read-only command this session), `LOG` (line from the live logs),
`GIT` (commit inspected via `git show`), `RELAYED` (stated in the operator's brief /
coordinator, not re-derived), `SWEPT` (located by one of this session's two delegated
read-only sweeps and cited with file:line — **every stage-load-bearing SWEPT claim was
re-read first-hand and re-tagged READ**; surviving SWEPT tags are supporting detail
only), `A-n` (assumption, §6).

**Standing rulings this plan is written under**: R#42 (office becomes RPC+push only; a
fallback must prove it is dead before deletion; a fallback that fires is a bug in the
main path), the loud-error rule (a missing answer is a LOUD error, never a quiet
degrade), and **task #60** (2026-08-17, RELAYED): *no reliance on fallbacks — the first
flow is direct AND efficient* — (1) DIRECT: one path for the ordinary flow, globally;
(2) FIRST-TIME: no retry ladder doing a correct first call's job; (3) EFFICIENT:
minimum work, claims measured, witnesses assert counts/ordering, never elapsed-ms.
Every fallback this plan touches is classified **(a)** carries ordinary traffic /
**(b)** genuine-failure-only / **(c)** provably dead — from receipts and code reach,
not preference (§1.6).

---

## 0. Verdict up front — six defect classes still in the tree, and four corrections to the brief

The brief's hypothesis — the dominant defect class is **lane ambiguity**: two paths
answering one question, with "no data" and "data you cannot reach" rendered
identically — is confirmed, and it decomposes into six structural facts, ordered by
what they still cost. Items 1–3 are silent state-loss shapes (R#40's family); 4–6 are
silent diagnosis-loss shapes (the S1 401 family).

1. **A vocabulary fork between what the wire may promote and what the office push lane
   can scope** (operator task #57, independently confirmed here). `patch_coverage.py`
   owns which entities/events promote (three capability tokens,
   `patch_coverage.py:126,149,179` READ); the office sink's scope predicate is a
   private restatement that knows only `office_actor`
   (`serve_office_subscriptions.py:445-456` READ). WV-H3 widened the first vocabulary
   without the second: **a folder-only patch frame is dropped by the office push lane
   with neither a patch nor a resync** (§1.1) — masked solely because the argv
   `harness stream` child still folds the same batch. Hard precondition on deleting
   that child (Plan C TC-4). **RD-H1.**

2. **Watermarks that move without the state they vouch for.** Three independent
   instances, one mechanism — a sequence number advances while the thing it certifies
   does not:
   - hermes ships a `patch` frame with an **empty `patches` list** whenever a covered
     domain event arrives without its paired patch row — the client advances its
     watermark having folded nothing; the row is permanently stale, unrecoverable by
     any gate (`stream.py:292-299` filter with no non-empty guard, `:422-430` promotes
     on coverability alone, both READ; five producer-side re-opening paths, §1.3).
     **RD-H3.**
   - the launcher's CLI-poll apply moves `_sequence` and bumps `coreRevision` but
     leaves the fold base `_lastCore` stale when no `rawCore` is supplied
     (`mission_read_model.dart:447-481` READ; the poll callers are the only
     null-`rawCore` full-core applies, bridge:2546/2552 SWEPT) — a later patch folds
     onto the superseded base and `commitFold` publishes it, silently discarding what
     the poll's core carried; the FC-0 revision fence cannot see it because the poll
     lands before `prepareFold`, not during (§1.4). **RD-L1.**
   - the subscribe handler coerces an **unreadable event log to baseline 0**
     (`serve_rpc.py:692-695` READ: `int(… .get("event_offset") or 0)`), destroying the
     sink's baseline gate (`:397`) and re-opening the hydrate→resync→restart loop the
     gate ordering exists to prevent — while `parity.py`'s own docstring calls zero
     "the single most damaging value this field can carry" and the honest None-handler
     already exists one module over (`stream.py:499-503` SWEPT). **RD-H2.**

3. **Write authority without a guard, and a projection mask nobody is billed for.**
   The upsert arm spends its revision token (`controller:1169-1170` SWEPT); the remove
   and surface arms send none — an unguarded arm can silently overwrite a peer
   (realm sync is a live second writer, `office_sync.py:221` READ). The BW-L6 hold
   sharpens it: held `pending` entries are staged for **every** differing actor in the
   desired layout, not just the gestured one, are never reconciled away by a read
   (`mission_office_layout_controller.dart:1803-1809` READ: pending entries survive
   `removeWhere`, then the overlay payload unconditionally overwrites the
   freshly-read server row), and on the terminal arm `retry()` re-enters the hold —
   **the mask is wholesale and has no expiry**, with no receipt at the masking site
   (escalates task #59). **RD-L2** (guards) **+ RD-L4** (the receipt).

4. **Error-class erasure with absence doing double duty on the provider surface.**
   S1 fixed one instance (`1d1ef64692` GIT); its siblings are live in the same file:
   a raised lane detector deletes the lane from the Limits payload entirely
   (`harness.py:3298-3318` READ) — strictly worse than the "no usage data" S1 fixed,
   since no row is left to carry a reason; `build_account_usage` fail-opens to
   `lanes: []` and the human renderer then prints the **false positive claim** "No
   account-usage lanes (no signed-in providers detected)" (`:3536-3547,3561-3563`
   READ); the visibility payload's blocks are dropped by blanket
   `except Exception: pass` while the launcher feature-detects capability by block
   presence (`:3083-3101` READ + the S1 commit's own words), so "old hermes" and "the
   builder threw" are byte-identical; and the unknown-provider fall-through routes
   back into the exact upstream swallow S1 routed around — **unreachable today**
   (`_USAGE_LANE_PROVIDERS` closed four-tuple, `:164-169` READ), i.e. dead code that
   is also a loaded regression. **RD-H5.**

5. **A degraded read that renders as a complete one, server-side.** `_read_actor_dir`
   skips unreadable actor JSON with `except Exception: continue`
   (`office_store.py:683-692` READ), feeding BOTH `runtime.office.get` and the
   subscribe baseline through the shared projection — which then reports
   `actors_truncated: 0`, a completeness claim computed from the already-shortened
   list (serve_rpc.py:481 SWEPT). This is the exact degraded-read shape that fed R#40;
   the launcher's absence-means-delete inference is gone, but the server still hands
   back silently-shortened projections as normal. An unreadable **archive** file
   additionally resets the revision to 1 (`:461-470` SWEPT) — silently re-arming the
   very guard token RD-L2 depends on. **RD-H4.**

6. **A liveness gap the code itself documents.** The office push lane's
   `onReconnected()` has **no production caller** — the lane's own comments say so
   twice (`mission_office_subscribe_lane.dart:832,847` READ: "it still has no
   caller") — so a serve-child respawn under a surviving session leaves registrations
   on a dead transport: the client believes it is subscribed and is silently stale
   until some other lane resyncs it. A retry ladder elsewhere doing a correct call's
   job — a #60 clause-2 violation verbatim. **RD-L3.**

**Correction 1 to the brief — the office sink line numbers, and the drop is a
regression, not a static gap.** The scope predicate is at
`serve_office_subscriptions.py:445-456`, not `:396-401` (those are the baseline gate).
And WV-H3 made the push lane's folder behaviour strictly WORSE for a token-declaring
client: before, a folder change demoted → `delta` → the lane resynced (the `office.*`
event names the workspace, `:301-309` READ); after, the batch promotes → `patch` →
silently dropped. Correct-and-expensive became silent. The fielded launcher declares
the token on both lanes from one constant (`00cb07558` GIT), so the drop is armed today.

**Correction 2 to the brief — the held write's overwrite is arm-specific; the mask is
worse than stated.** The upsert flush is revision-guarded — a peer's concurrent move
refuses, retracts, and triggers the corrective read; "writes over it" is literal only
for the unguarded remove/surface arms. But the projection mask is WIDER than the brief
recorded: it covers every actor whose content differs (the whole desired layout is
staged, `controller:693-696,869-886` SWEPT), not just the gestured one, and it is
indefinite on the terminal arm. One half of the precondition was over-stated, the
other half under-stated.

**Correction 3 to the brief — the fence-incident numbers.** The on-disk tally (Plan F
§0, READ) is 14 `REFUSED fenced` + 1 gap + 9 `STALE dropped`, 24 resubscribes (11
`fold:fenced` / 13 `push:full_core`), ladder 250 ms→500 ms→1 s, park at 5-in-60 s,
reached #5. The brief's "22 fences and a 4 s backoff ladder" matches no tally on disk.
Substance confirmed; numbers not.

**Correction 4 — two parity assumptions in circulating briefs are false.**

> **SUPERSEDED 2026-08-19 (dead-code audit pass 2, HA-4).** The first half of
> this correction is itself wrong, and was wrong when written.
> `runtime.office.resolve_conflict` **landed at `32a392364b`**, four hours
> after this document was committed. `grep -c '^@method(' agent_runtime/serve_rpc.py`
> returns **8**, not seven: `runtime.office.{get,subscribe,unsubscribe,upsert,remove,surface.update,resolve_conflict}`
> and `runtime.agent.create`. **7 of the 8 have launcher callers** —
> `runtime.office.unsubscribe` has zero (`lib` = 0; the subscribe lane's
> `dispose()` never sends it), which is a launcher WIRING gap, not a hermes
> deletion. The genuinely CLI-only office verb is `office.actor.restore`, as
> the second half of this correction says. Do not re-derive the seven-methods
> claim from here; re-run the `@method` sweep.

`runtime.office.resolve_conflict` never landed — exactly seven RPC methods exist
(RAN, `@method` sweep); the capability lane is not a fallback there but the unfinished
main path (Plan E WV-H4/L6). And `office.actor.restore` is **not dead** — no UI submit
site (SWEPT), but it is the sanctioned operator recovery lane (`serve_rpc.py:870`
names it a class-key-fence override, SWEPT) and it restored the 2026-08-15 mass
archive. Classification: keep, no UI caller (§1.6).

---

## 1. The proof

### 1.1 The folder-only silent drop, end to end (task #57; all READ/GIT)

1. `update_surface` emits an `office_surface` patch whose id is the **bare workspace
   id** — no slash (`state_patches.py:929-949` READ; cross-repo fixture
   `patch_office_surface.json`: `"id":"ws_office_pilot"`, GIT `00cb07558`).
2. `office.surface.updated` is covered iff `office_surface_fold` is declared
   (`patch_coverage.py:151-179,254-260` READ); the launcher declares entity + token on
   both lanes from one constant (GIT WV-L4).
3. A folder-only batch therefore promotes → the hub fans a `patch` frame to the sink.
4. The sink admits a frame iff ≥1 row has `entity == OFFICE_ACTOR_ENTITY` and a
   slash-prefixed id (`serve_office_subscriptions.py:446-450` READ). An
   `office_surface` row fails both conjuncts → `return` at `:456`. No patch, no
   resync — the module's own docstring rule ("A resync is recoverable; a dropped
   change is not", `:427`) is violated by its own predicate.
5. The sibling scope function `_delta_touches_workspace` carries the same fork
   (`:311-314` READ) and is saved only because the paired domain event hits the
   `office.*` arm — the DELTA lane is covered, only the PATCH lane is not.
6. Mixed batches are unaffected (any actor row admits the whole frame, V6 forward),
   which is why this survives most testing; the reachable case is every folder rename
   with no actor write in the same ~450 ms coalescing window.

### 1.2 The baseline `or 0` (all READ unless tagged)

`events_watermark()` returns `{"event_offset": None, "event_offset_error": …}` when
the log cannot be stat'ed — which its docstring says happens "routinely" on this
platform under AV scanning (parity.py:189-213 SWEPT). The subscribe handler's
`int(… or 0)` (`serve_rpc.py:692-695` READ) converts that None to 0 with no exception
— the typed `except (TypeError, ValueError)` never fires, and the error field is
discarded. Consequences: the reply advertises watermark 0 (indistinguishable from an
empty log); the sink's baseline gate becomes `<= 0` — no gate; the mandatory
post-subscribe hydrate emits the unconditional resync; the client re-subscribes; the
producer restarts; a fresh hydrate resyncs it again — the exact loop the
baseline-before-type ordering was built to end (`serve_office_subscriptions.py:381-397`
READ), at ~822 KB per lap for every subscriber in the room, plus redelivery of every
buffered frame. `stream.py:499-503` (SWEPT) is the honest None-handler next door; the
subscribe lane is the one reader still doing `or 0`.

### 1.3 The empty patch frame (READ)

`patch_batch_frame` filters the batch to `STATE_PATCHED_EVENT_TYPE` rows with a
non-empty guard on the BATCH but none on the FILTERED list (`stream.py:292-299` READ);
promotion is decided purely by `batch_is_patch_coverable` (`:422-430` READ). Every
covered domain event is coverable on its own, so a batch carrying the event WITHOUT
its paired patch ships `{"type":"patch", patches: [], watermark: <batch>}` — the
client's watermark advances having folded nothing; the row is stale until an unrelated
full core. `state_patches.py:1032-1039` (SWEPT) names this exact failure as the reason
`emit_office_actor_refresh` exists — but that guard lives in ONE producer, and five
paths re-open it: three best-effort patch-emit swallows in `office_store.py`
(:221-234, :261-270, :284-293 SWEPT), one **no-exception** path (`:383-384` SWEPT —
the surface patch is skipped when `surface_existed` is false while the event still
emits), and the cross-process split of `delta_patches_enabled` (writer and stream
producer evaluate it independently; a transient root-config fault in the writer
suppresses the patch while the stream process happily promotes the event-only batch,
`state_patches.py:326-397` SWEPT). The fix belongs at the frame-builder chokepoint,
where it closes all five at once.

### 1.4 The poll lane's stale fold base (Hazard A; mechanism READ)

`applySnapshot` assigns `_sequence`, bumps `coreRevision`, and calls
`_retainFoldBase(rawCore, …)` — which, with `_patchModeActive` latched and
`rawCore == null`, leaves `_lastCore` at the OLD core (`mission_read_model.dart:447-481`
READ). The CLI `harness snapshot` poll is the only full-core apply passing no
`rawCore` (bridge:2546/2552 SWEPT). A later `patch` whose `base_offset` equals the
poll's offset passes both gates and folds onto the superseded base; `commitFold`
publishes it as truth — silently discarding everything the poll's core carried beyond
the patched rows. The FC-0 revision fence is blind to it (the poll precedes
`prepareFold`). Reachable chain (SWEPT): stream child dies → reconnect backoff not
ready → cache unusable → hydrate fails → `_loadSnapshotFromCli` applies, while the
push lane (independent of the stream child, riding the serve session) keeps
delivering patches. `_lastCore` is never nulled anywhere; the resync lane is safe by
contrast because its re-hydrate supplies `rawCore` (bridge:2469→1296 SWEPT).

### 1.5 The hold's wholesale, unbilled, unbounded mask (mechanism READ)

`_holdFlush` → `_stageHeldOverlay` writes `pending: true, held: true` entries for
**every** actor in `sync.desired` whose content differs from `serverKeys` — `desired`
is the whole layout, staged wholesale (controller:693-696, 838-886 SWEPT). In
`resolveLayout`, pending entries survive the reconcile-away pass and the overlay
payload then unconditionally overwrites the freshly-seeded server rows
(`controller:1803-1809` READ). Held entries are dropped in exactly one place — a real
flush at posture `ready` (`:1107` SWEPT); on the terminal arm they are kept by design
and `retry()` re-enters the hold (`:786-791, :929-950` SWEPT). So for the whole hold
(indefinitely, on terminal) a remote peer's edit to ANY actor in the local scene is
read, discarded, and rendered as the local prediction — with no receipt at that site,
and no test covering a remote change arriving during a hold (the boot-hold suite's
groups enumerate none, SWEPT). BW-L6 landed this knowingly for the gestured actor;
the wholesale width and the terminal-arm unboundedness are what escalate it (#59).

### 1.6 Ruling #60 — the fallback/lane classification table

| Lane | Class | Evidence |
| --- | --- | --- |
| argv `harness stream` NDJSON child | **(a) ordinary traffic** — sole carrier of folder-only changes (§1.1) and of non-office read-model currency; every demoted batch is built twice, once per producer: all 7 demoted offsets in the live window built exactly 2× (LOG tally, 08-16 19:22 → 08-17 08:29). Main-path bugs before deletion: RD-H1 here; TC-2 in Plan C | LOG/RAN; READ §1.1 |
| CLI `harness snapshot` poll apply | **(b) genuine-failure-only** (reachable only behind a four-failure chain, §1.4) — but with a silent-discard tooth (Hazard A). RD-L1 removes the tooth; deletion is Plan C's | SWEPT chain; READ mechanism |
| office argv write arms (upsert/remove/surface) | **(b) by construction** (RPC-first, `Unavailable` arm only) — EXCEPT inside the ~4.3 s page-open `laneAbsent` window, where they carry ordinary gestures: that window is the main-path bug (item 10, unowned) blocking TC-3's exit, not a reason to keep the arms. RD-0 collects the `write lane: N rpc, M cli` receipts | SWEPT controller:1242-1532; RELAYED §10.3-10 |
| `office.resolve_conflict` capability submit | **not a fallback — unfinished main path** (no RPC method; 7 registered, RAN). Plan E WV-H4/L6 own it | RAN |
| health probe child | **(b) failure diagnostic** since BW-L5; single call site, reached only when session absent or boot evidence not ready | GIT dd3e59e51; SWEPT provider:647-673 |
| mass-archive tripwire | **(b) kept deliberately**; retirement is UP-4's. BUT its incident-repro test exercises only the argv arm (§ RD-L2) | SWEPT test:437-627 |
| `office.actor.restore` | **keep — operator recovery lane, no UI caller** (Correction 4). NOT (c) | SWEPT both repos; RELAYED cross-check |
| `--expect-revision` argv arms (upsert/remove/surface lowerings) | **(c) provably dead** — no submit site passes the flag (controller:1256-1259, :1526-1529, :1653-1656 SWEPT); the guard lives on the RPC lane only, and RD-L2 keeps it there. Delete (RD-L6) | SWEPT; RD-0 re-grep |
| `_fetch_usage_lane` fall-through (`harness.py:3355-3357`) | **(c) provably dead** — closed provider tuple (`:164-169` READ); only future activation is the S1 regression. Delete (RD-H5) | READ |
| `missionOfficeRpcFlag` | kill switch (default true, rollout 100, no killAt), not dead — but the `gateClosed` arm has no default-build coverage. Recorded; not staged | SWEPT rpc_flag:38-46 |
| push lane after serve respawn | **main-path liveness bug** — registrations on a dead transport until an unrelated resync (§0.6). RD-L3 | READ subscribe_lane:832-847 |

Also recorded, not analysed (no-go boundary): zero `serve_office_subscription_rebaselined`
receipts across all three log rotations since the 08-17 cold boot booted
FC-H1-bearing code (RAN) — weak-positive evidence that FC-L2 closed the fence ladder;
one quiet session is not proof.

### 1.7 What is already dead, and what documents now contradict the tree

**Dead (deletion staged, each with its prove-dead receipt):**
- `_fetch_usage_lane`'s `fetch_account_usage` fall-through (`harness.py:3355-3357`
  READ) → RD-H5.
- The three `--expect-revision` argv lowerings (bridge:4090-4134 SWEPT) → RD-L6.

**Not dead, despite appearances (do NOT delete):**
- `office.actor.restore` (Correction 4).
- The mass-archive tripwire (backstop by ruling).
- `missionOfficeRpcFlag` (kill switch).
- The 822 KB push gate is a LIVE config (`read_model.delta_patches` SHIPPED=True /
  FALLBACK=False, `runtime_config.py:73,82` SWEPT) — the memory-file note that the
  push is "dark behind a config gate" is stale: it ships on by default.

**Register / plan sentences the tree now contradicts:**
- Gesture plan §10.3 item 6 ("one `office.surface.updated` sinks a 23-event startup
  batch") and item 9's "demotes the boot batch every time" — stale since WV-H3+WV-L4
  for the declaring launcher; whether the BOOT batch promotes still turns on its other
  cargo (Plan E A-1, unanswered — §6.1). Item 9's write-storm half stands.
- Any brief still citing the absence-means-delete inference as extant — deleted
  outright at launcher `7623f99cf` (Plan E §0 Correction 1; re-verified).
- ~~Any parity claim that all four office write verbs ride RPC —
  `resolve_conflict` never landed (Correction 4).~~ **SUPERSEDED 2026-08-19:**
  `resolve_conflict` DID land (`32a392364b`); the registry holds eight methods.
  See the superseded note on Correction 4.
- `03-retirement-ledger.md` no longer exists (mission-lane removal wave, RAN git log);
  the living ledger is doc 19.

### 1.8 isSettled / retire condition — the standing constraint, current text (SWEPT, quoted by the launcher sweep; spot-checked READ)

`_WorkspaceSync.settled` (controller:537-538, migration seed only):
`overlay.isEmpty && removed.isEmpty && timer == null && !flushing`.
`writesInFlight` (:575-576): `timer != null || flushing || overlay.values.any(pending)`.
`isSettled` (:639-640): `!(writesInFlight ?? false)`. `holding` is deliberately NOT a
term; held state is represented only through pending overlay entries. Page retire
(page:3010-3026): `workspaceId != null && !_hasPendingOfficeSave &&
isSettled(workspaceId)`, then `fresh != null && !freshLoading` → override retired.
**Every stage below states its effect against these; a stage that would edit
`mission_office_optimistic_paint_test.dart` is a stage-stopping event, not a test
update** (Plan E's rule, adopted verbatim).

---

## 2. Validation — what each stage buys, honestly

| Stage | Defect class made structurally impossible | Does NOT buy |
| --- | --- | --- |
| RD-0 | none — receipts before belief (FC-0/BW-0 precedent) | any behaviour |
| RD-H1 | a promoted batch a subscribed office lane can neither fold nor resync (§1.1); plus recurrence: the NEXT covered office entity cannot re-open it | stream-child deletion (TC-4); persona-only batches on the office lane (correct drop) |
| RD-H2 | a fabricated baseline — an unreadable log rendered as offset 0 (§1.2) | the stat failure itself (platform fact); resync-storm costs from OTHER causes |
| RD-H3 | a watermark advanced by a frame that folded nothing (§1.3) — closes all five re-opening paths at one chokepoint | the producer swallows themselves (they get receipts, not new control flow) |
| RD-L1 | a fold onto a base the watermark has already superseded (§1.4) | poll-lane deletion (Plan C); the four-failure chain that reaches it |
| RD-L2 | a silent overwrite of a peer's newer state by an unguarded remove/surface write; plus the incident repro's blindness to the RPC arm | the hold's projection mask (RD-L4 bills it; UP-1 re-ranks it); resolve-conflict transport (Plan E) |
| RD-L3 | a push lane that outlives its transport silently (§0.6) | the respawn itself; stream-lane liveness (watchdog exists) |
| RD-H4 | a shortened projection that reports itself complete; a guard token silently reset by an unreadable archive (§0.5) | the unreadable files themselves; launcher rendering of the new counts (follow-on) |
| RD-H5 | absence meaning two things on the provider/limits surface; a false "no signed-in providers" claim; the dead swallow re-entry (§0.4) | upstream's own swallow (fork boundary: route around); gateway/TUI usage surfaces (ledger) |
| RD-L4 | an unbilled projection mask — a held write hiding a divergent remote row with no receipt (§1.5) | the mask itself (BW-L6's accepted cost; re-ranking is UP-1's ledger) |
| RD-L5 | three known silent-skip degrades rendering as honest empties (chat-context eviction vs unreachable; bare-token paste vs 401'd lane; models.dev no-cache vs no-match) | the c7a8e6043 class in general (these are the three found; RD-0 confirms no fourth) |
| RD-H6 | three seam-parity gaps: one config key read at two resolution scopes; one roster fault with two renderings; one unenforced resolver invariant | — |
| RD-L6 | the register-rot class in its cheapest form: argv params nothing passes | anything with a live caller |

### 2.1 What every stage does to `isSettled` and the page retire condition

- **RD-0, RD-H1..H6** — hermes-side / read-only: no launcher predicate reachable.
  RD-H1's launcher-visible effect is that folder-only patches start ARRIVING on the
  push lane; they enter the one FC-L2 fold chain, deduped by the `base == held` gate.
  Read-side only.
- **RD-L1** — read-model only; no controller predicate, overlay, or flush touched.
  Its fold-refusal arm routes to the EXISTING resync path (which re-hydrates with
  `rawCore`); `writesInFlight` terms untouched.
- **RD-L2** — inside `_flush` under `flushing = true`; swaps arguments of
  already-awaited calls (adds `expect_revision`). New refusals ride the existing
  refused/rolledBack arms, which gate the existing corrective read. No predicate
  term changes. OR-4 fence and `mission_office_lane_reattach_test.dart` byte-green.
- **RD-L3** — subscribe-lane lifecycle only; upstream of the fold; no write-lane
  state. The one timing effect is that push currency RESUMES sooner after a respawn —
  the safe direction for retire (the override retires no earlier than today).
- **RD-L4** — receipt + one status field (`holding` gains `maskedCount`). Explicitly
  does NOT touch the overlay, the pending flag, or any predicate term; the BW-L6
  assertion `isSettled == false` while holding stays green byte-unchanged.
- **RD-L5, RD-L6** — dialog/switcher/visibility surfaces and dead argv params; not on
  the office write lane.

---

## 3. Stages

Ordered by defect class eliminated — silent state-loss first, then silent
diagnosis-loss, then hygiene — never by line count. Every stage rolls back by
reverting its commit; none requires simultaneous deployment.

---

### RD-0 — receipts before belief (read-only, both repos + live logs)

**Goal.** Convert this plan's conditional claims to recorded fact; re-confirm the
dead list at today's SHAs before any deletion.

- **R0-a.** Decode ONE real folder-change batch and one page-open batch from the live
  event log (fold-plan §1 method): does a folder-only batch promote un-coalesced in
  the field? Prices RD-H1's "live gap" wording (§6.1).
- **R0-b.** Collect launcher-side write-lane receipts (`write lane: N rpc, M cli`,
  `fallbackReasons`, `REVISION MISS` count) from the operator's diag log — TC-3's
  evidence stream read early; prices RD-L2's stale-refusal exposure and classifies
  the argv arms' ordinary-traffic share (#60).
- **R0-c.** Re-grep at HEAD: no submit site passes `--expect-revision` (RD-L6's
  prove-dead); no UI submit site for `office.actor.restore` (classification, not
  deletion); no fourth silent-skip site of the RD-L5 class in
  `lib/features/mission_control` (catch-return-null feeding a bare empty render).
- **R0-d.** Confirm `patch_gap` / `fold:gap` receipts in the operator's diag log
  (Hazard B's observable): how often does the push lane's baseline disagree with the
  held core today? Prices RD-L3/RD-H2 urgency.
- Output: §1.6/§1.7 tables updated in place; no code.

---

### RD-H1 — hermes: one scope authority for the office push sink (the silent-gap class; task #57)

**Defect removed, evidence §1.1.** After this stage a batch the coverage authority
promotes is, by construction, either FORWARDED or RESYNCED by the office lane — never
dropped — because the sink's scope derives from the same module that owns the id
scheme. Had this existed on 2026-08-16, WV-H3 could not have shipped the regression:
covering `office.surface.updated` without widening the sink would have failed the
partition witness below.

**Prove-dead obligations.** None (nothing deleted). Inverse obligation (#60): the
stream child may NOT be deleted (TC-4) until this stage is live and R0-a confirms
folder-only frames arrive on the push lane.

**Change surface.**
- `agent_runtime/state_patches.py`: one scope function beside the id builders —
  `office_patch_scope(patch) -> workspace_id | None` — office_actor via prefix split,
  office_surface via bare id, else None. Id scheme and scope parser become one module.
- `serve_office_subscriptions.py`: the sink's in-scope test (`:446-450`) and
  `_delta_touches_workspace`'s `state.patched` arm (`:311-314`) both call it.
  Forward-whole (V6) semantics unchanged.
- No wire change, no contract movement, no fixture regeneration (the launcher fold
  already handles `office_surface` — WV-L3; the cross-repo golden exists).

**Tests** (extend `test_serve_rpc_office_subscribe.py` — the sink is a closure built
for hub-free tests, its own docstring says so).
- `a folder-only patch frame is forwarded, not dropped` — frame's only row is the
  mirrored `patch_office_surface.json` row. **Anti-vacuity.** *Mutation:* restore the
  `office_actor`-only predicate. *Probed fields:* the test-owned `emit` recorder's
  single notification has `method == "runtime.office.patch"` AND
  `params.patches[0].entity == "office_surface"` AND `changed.folders` equal to the
  fixture's list. *Why the mutation cannot also set them:* the mutant emits NOTHING
  (the drop is a bare `return`); a second-order mutant emitting a RESYNC instead
  fails the same probes — a resync carries a different method and no `patches`. The
  probes are on the content of a message the mutant never constructs.
- `another workspace's folder change sends nothing` — bare-id mismatch
  (`ws_other`). *Mutation:* scope every surface row in. *Probed field:* `sent == []`.
  The pair pins the id comparison; neither drop-all nor forward-all passes both (two
  driven values — the FC-0 discipline).
- **Second independent witness, different file** (beside the existing partition gate
  in `test_patch_coverage.py`): every office-scoped coverable entity enumerated from
  `patch_coverage`'s own constants (`office_actor`, `office_surface`) must be scoped
  non-None by `office_patch_scope` on a minimal fixture row. *What it kills that
  witness 1 cannot:* the NEXT WV-H3 — covering a new office entity without teaching
  the scope function reds this even though no sink test for that entity exists yet.
  *Why the sink mutant cannot satisfy it:* it never calls the sink; witness 1 pins
  scope→delivery, witness 2 pins authority→scope. The RD-H1 predicate mutant reds
  both; each catches a drift the other is blind to.

**Mixed pairs.** Old launcher (no token): folder batches demote → resync arm —
today's wire byte-identical. Old hermes: no `office_surface` frames exist; inert.
**isSettled/retire:** §2.1. **Rollback.** Revert. **Perf.** None claimed; witnesses
assert delivery and counts, never elapsed-ms (#60).

---

### RD-H2 — hermes: an unreadable event log stops becoming baseline 0

**Defect removed, evidence §1.2.** A fabricated zero baseline — the one value the
watermark module documents as maximally damaging — can no longer be minted by the
subscribe handler; "cannot read the log" becomes a typed, transient refusal the
client's existing degrade ladder already knows how to hold.

**Change surface** (`agent_runtime/serve_rpc.py:692-695`): read
`events_watermark()` once; if `event_offset` is None (carrying `event_offset_error`),
refuse the subscribe with a typed transient reason `baseline_unavailable` (the
`SubscribeOutcome`/reason vocabulary exists — `NO_PUSH_LANE`/`PUSH_LANE_DRAINING`,
`serve_office_subscriptions.py:539-545` READ — this adds a third, same shape), and log
the discarded `event_offset_error` class. The typed `except` stays for genuinely
malformed values. No registration happens on the refusal path.

**Tests** (new group in `test_serve_rpc_office_subscribe.py`):
- `an unreadable watermark refuses typed and registers nothing` — monkeypatch
  `events_watermark` to `{"event_offset": None, "event_offset_error": "OSError"}`.
  **Anti-vacuity.** *Mutation:* restore `or 0`. *Probed fields:* (1) the reply is an
  error whose `data.reason == "baseline_unavailable"`; (2) the test-owned fake hub's
  `subscribe` call record is EMPTY. *Why the mutation cannot also set them:* the
  mutant returns a SUCCESS reply (it cannot carry the refusal reason) and registers a
  sink (the fake hub's record convicts it). Two independent witnesses; either kills.
- `a readable watermark of literally 0 from an EMPTY log still subscribes` — the
  discriminator fixture: `{"event_offset": 0}` (no error field) must register at
  baseline 0. Kills the over-refusing mutant ("refuse all zeros") — an empty log is a
  real state, and this test is what separates the honest zero from the fabricated one.
  *Probed field:* fake hub record length 1 with `baseline_offset == 0`.

**Mixed pairs.** Launcher: a refused subscribe takes the existing typed-degrade arm
(the lane already holds NO_PUSH_LANE-class refusals without spinning); no client
change required. **isSettled/retire:** §2.1. **Rollback.** Revert. **Perf.** None.

---

### RD-H3 — hermes: an empty patch frame can never promote

**Defect removed, evidence §1.3.** A frame that advances the watermark must carry the
state that justifies it. The guard lands at the ONE chokepoint that sees the filtered
list, closing all five producer-side re-opening paths at once — including the
no-exception path and the cross-process config split — instead of patching five
producers.

**Change surface** (`agent_runtime/stream.py`): in `_batch_frames_with_liveness`
(`:422-430`), compute the filtered `STATE_PATCHED` row list before promoting; empty →
fall through to the full-core classification (the batch carried an event that moved
state with no patch to express it — the honest answer is the core, and the existing
`snapshot_build reason=demote` receipt bills it). `patch_batch_frame` additionally
refuses an empty filtered list (`ValueError`) — belt and braces at the builder, same
one-authority pairing as the coverage token's ("one gate saying so is cheaper than two
that can disagree").

**Tests** (extend `test_stream_patch.py`):
- `a covered domain event with no paired patch demotes instead of shipping an empty
  patch frame` — batch of one `office.surface.updated` event, no `state.patched` row.
  **Anti-vacuity.** *Mutation:* promote anyway. *Probed field:* the yielded frame's
  `type != "patch"` for this batch (it is the full-core lane's frame). *Why the
  mutation cannot also set it:* the mutant's only output for this batch IS a patch
  frame; there is no second frame to probe.
- `patch_batch_frame refuses an empty filtered list` — direct builder unit; *Mutation:*
  drop the builder guard. *Probed field:* the raised `ValueError`. **Why two guards
  are two witnesses:** removing the caller guard alone still reds the first test;
  removing the builder guard alone reds the second; removing BOTH reds both. A single
  mutant cannot satisfy either test it touches — and the pairing is itself pinned by
  an integration case (caller guard deleted under a patched builder → the builder's
  refusal surfaces, proving the belt catches what the braces drop).
- Regression fence: every existing patch-frame fixture (non-empty) byte-unchanged.

**Producer receipts (same commit, no control flow):** the three `office_store.py`
best-effort patch-emit swallows log the exception class when they fire — the demote
this stage forces is then attributable instead of mysterious.

**Mixed pairs.** Clients never see a new frame kind — they see a full core where they
previously saw a lying empty patch; every fielded fold handles cores. **Rollback.**
Revert. **Perf.** Honest cost increase on the five failure paths only (a full core
where a free-but-wrong empty frame shipped); ordinary batches unchanged — stated
per #60: correctness first, and the demote receipt measures it.

---

### RD-L1 — launcher: a full-core apply moves the fold base or invalidates it (Hazard A)

**Defect removed, evidence §1.4.** The invariant becomes: `_sequence` and `_lastCore`
move together, or the base is declared unusable — after which the next patch frame
takes the existing resync arm (which re-hydrates WITH a raw core) instead of folding
onto superseded state. The silent-discard outcome stops being representable.

**Change surface** (`mission_read_model.dart:474-481`): `_retainFoldBase` with
`_patchModeActive && rawCore == null` sets `_lastCore = null` (today it keeps the old
core); `prepareFold` against a null base answers the existing typed refusal
(`patch_without_base` → `needsResync`), which the bridge already routes to
`_loadSnapshotFromStreamHydrate(force: true)` — the lane that provably supplies
`rawCore`. Alternative rejected: threading `rawCore` through the CLI poll would make
the (b)-class lane MORE capable, against ruling #42/#60's direction — the poll's
job is to paint, not to host folds.

**Tests** (read-model unit suite):
- `a patch after a base-less full-core apply refuses to fold and requests resync` —
  apply core#1 with rawCore; apply core#2 WITHOUT rawCore (sequence advances);
  deliver a patch at the next offset. **Anti-vacuity.** *Mutation:* keep the old base
  (revert the null-out). *Probed fields:* (1) the fold outcome is the typed resync
  request, and (2) the published snapshot's row X — a row core#2 changed and the patch
  does not touch — equals core#2's value, never core#1's. *Why the mutation cannot
  also set them:* the mutant folds onto core#1, whose X provably differs (the fixture
  drives X to two distinct values across the two cores — probe a field the patch does
  not carry, the BW-H1 discipline); and its outcome is a commit, not a resync request.
- `a full-core apply WITH rawCore keeps folding` — the discriminator: same sequence
  of applies but core#2 supplies rawCore; the patch folds and commits. Kills the
  over-invalidating mutant (null the base always), which would turn every stream
  hydrate into a resync loop. *Probed field:* commit outcome + folded row value.

**isSettled/retire:** §2.1 — read-model only. **Mixed pairs.** N/A (client-internal).
**Rollback.** Revert. **Perf.** One extra hydrate on the rare poll-then-patch seam,
replacing a silent discard; counts, not ms.

---

### RD-L2 — launcher: every office write arm spends the revision it already tracks, and the incident repro watches the lane that runs

**Defect removed.** (i) An unguarded remove/surface write landing over a peer's newer
state with no refusal — Plan E's **D-W1 promoted** to a stage on ruling #60 clause 1,
sharpened by the hold's flush-after-silence edge (§1.5) and by realm sync being a live
second writer. (ii) The witness-vacuity gap: the mass-archive incident repro never
overrides `missionOfficeRpcWriterProvider`, so all four controller assertions
exercise the ARGV fallback — an RPC arm regressed to always-`Unavailable` leaves the
incident repro green (SWEPT test:437-627). The repro must watch the lane that
actually runs in production.

**Prove-first obligation.** R0-b's `REVISION MISS` rate on the guarded upsert arm
prices the refusal exposure. The stage lands either way; the rate decides the rollout
note, not the decision (#60). Dependency: RD-H4's archive-revision fix (an unreadable
archive resetting the token to 1 would let a stale guard PASS — the guard is only as
honest as its token).

**Change surface** (`mission_office_layout_controller.dart` + RPC codecs):
- Remove arm: send `expect_revision: sync.serverRevisions[key]` when held (the
  `_archiveActor` docstring's pre-approved one-argument change); absent → send none
  (first-contact archives stay unguarded — nothing to guard against).
- Surface arm: adopt the revision from the WV-L2 echo into
  `sync.serverSurfaceRevision`; spend it on the next surface write.
- Stale refusals (`4090 stale_revision`) take the EXISTING terminal path: retract,
  count `refused` AND `rolledBack` (the §10.3-4 lesson — an uncounted rollback is an
  invisible one; that bug shipped once), which gates the existing corrective read.
  No argv fallback on refusal (the "not a second chance" ruling).
- RPC lane only; the argv `--expect-revision` lowerings stay dead and RD-L6 deletes
  them.
- Test hygiene: the mass-archive repro gains a variant with
  `missionOfficeRpcWriterProvider` overridden to an advertising fake, asserting the
  tripwire on the RPC arm too.

**Tests** (extend `mission_office_explicit_removal_test.dart` + folder-lane group):
- `a remove for a key with a known server revision sends it` — **Anti-vacuity.**
  *Mutation:* drop the argument. *Probed field:* the fake RPC writer's recorded
  `params.expect_revision`, driven to two distinct injected values (3, 7) seeded via
  the ack-adoption path. A mutant sending nothing records null; a constant cannot
  match two injected values it never reads.
- `a stale-refused remove retracts, counts rolledBack, and requests the corrective
  read` — *Mutation:* treat stale as Ok. *Probed fields (second witness):* fake
  repository refresh-request count == 1 AND key retracted from `sync.removed` AND
  `rolledBack == 1`. Treating-stale-as-Ok leaves the key removed and requests
  nothing; retract-but-forget-the-counter fails the counter probe — the historical
  §10.3-4 bug replayed as a witness, deliberately.
- `a key with no known revision sends none and still archives` — kills the
  over-guarding mutant (a refused first-contact archive would regress the gesture).
- Surface twins of all three, driven through the WV-L2 echo.
- `the incident repro trips on the RPC arm` — the new variant; *Mutation:* make
  `removeActor` always `Unavailable`; the ORIGINAL repro stays green (that is the
  vacuity) while the new variant's probe — the fake RPC writer's recorded remove
  count — goes red. Two lanes, two watchers.
- Paint fences byte-unchanged: `mission_office_optimistic_paint_test.dart`,
  `mission_office_lane_reattach_test.dart`,
  `mission_office_mass_archive_incident_repro_test.dart`'s existing groups.

**isSettled/retire:** §2.1. **Mixed pairs.** Old hermes (no RPC verbs): arms are argv
there and stay behaviour-parity (no guard) — stated in the commit. **Rollback.**
Revert. **Perf.** One integer per request.

---

### RD-L3 — launcher: the push lane learns the serve child came back

**Defect removed, evidence §0.6.** A serve-child respawn under a surviving session no
longer strands office registrations on a dead transport; the lane re-subscribes on
the respawn edge with the reason token the plumbing already mints (`reconnect` —
`onReconnected` exists, `subscribe_lane:418` READ; it has no caller, `:832,847`
READ). Today's recovery is whatever unrelated resync notices first — a retry ladder
doing a correct call's job (#60 clause 2).

**Change surface**: the serve-session lifecycle owner (the provider that watches the
session's ready evidence) invokes `lane.onReconnected()` on the died→ready edge —
the same edge-listen shape the chat outbox drain and BW-L6's hold drain already use.
No lane-internal changes; the method is built and waiting.

**Tests** (subscribe-lane + provider fakes):
- `a serve respawn triggers exactly one resubscribe with reason reconnect` —
  **Anti-vacuity.** *Mutation:* leave the caller unwired (today's tree). *Probed
  field:* the fake transport's recorded subscribe request list gains exactly one
  entry whose `reason == "reconnect"` after the fake session emits died→ready. A
  mutant cannot place a request on the test-owned transport; a receipt-forging mutant
  has no transport record.
- `a pushed patch after the respawn reaches the read model` — the delivery witness
  (second, independent): fake hub pushes a post-respawn frame; *probed field:* the
  folded row's value in the read model. A mutant that resubscribes on the DEAD
  transport (wrong wiring) passes witness 1's count and fails delivery.
- `no resubscribe fires on a ready edge with no preceding death` — kills the
  every-edge mutant (which would re-baseline the room per boot for nothing — each
  re-baseline restarts the producer, §1.1's cost model).

**isSettled/retire:** §2.1 — upstream of the fold; safe direction only.
**Mixed pairs.** Old hermes: resubscribe lands on the same subscribe handler; N/A.
**Rollback.** Revert. **Perf.** Removes a silent-staleness window measured in
"until something else resyncs"; counts, not ms.

---

### RD-H4 — hermes: the office projection counts what it could not read

**Defect removed, evidence §0.5.** A shortened projection stops reporting itself
complete: unreadable actor files are COUNTED and the count rides the projection to
both readers (get + subscribe baseline share `_office_projection` — one chokepoint,
so one field serves both). An unreadable ARCHIVE stops silently resetting the
revision guard token to 1 — the token RD-L2 spends.

**Change surface** (`agent_runtime/office_store.py` + `serve_rpc.py`):
- `_read_actor_dir` (`:683-692`) counts skips and logs each class once per directory
  scan; `list_actors` exposes the count; `_office_projection` carries
  `actors_unreadable: N` (additive key; old launchers ignore it).
- The archived-actor read under remove (`:461-470`): unreadable archive → typed
  refusal (`archive_unreadable`) instead of `archived=None` → base revision 0 →
  revision 1. A remove that cannot read the token it must bump is a fault, not a
  fresh start.
- `archive_actors_for_instance` (`:674-678`): the per-actor swallow keeps the loop
  (a prune must not die on one bad file) but the return grows a failure count so 0/0
  and 0/3 stop being the same answer.

**Tests** (`test_office_store.py` + `test_serve_rpc_office_subscribe.py`):
- `a corrupt actor file is counted, not vanished` — fixture with 1 (then 2) corrupt
  JSON files among readable ones. **Anti-vacuity.** *Mutation:* restore the bare
  `continue`. *Probed fields:* `actors_unreadable` equals the driven count (two
  values) AND the readable actors still list. A mutant reporting a constant cannot
  match both fixtures; a mutant skipping silently reports 0.
- `the subscribe baseline carries the same count` — the chokepoint witness (second,
  different reader): the subscribe reply's projection block carries the identical
  field. Kills a mutant that patches `get`'s path but not the shared projection —
  possible only if the fix forked the chokepoint, which is exactly what the witness
  forbids.
- `a remove over an unreadable archive refuses typed and mints no revision-1 ack` —
  fixture pins live revision 7, corrupts the archive copy. *Mutation:* fall through
  to base 0. *Probed fields:* typed error reason AND no ack — the mutant acks
  `revision: 1`, which the probe rejects; it cannot produce the reason string.

**Mixed pairs.** Additive fields; old clients ignore. **isSettled/retire:** N/A.
**Rollback.** Revert. **Perf.** None.

---

### RD-H5 — hermes: absence stops meaning two things on the provider surface, and the dead lane into the swallow is deleted

**Defect removed, evidence §0.4.** All four sub-shapes of the S1 family: block-absent
vs builder-threw; lane-omitted vs detector-raised; "no signed-in providers" as a
false positive; and the unreachable route back into the upstream swallow.

**Change surface** (`hermes_cli/harness.py`):
- `build_provider_visibility`: each `except Exception: pass` isolator records into an
  additive `block_errors: {block: exception_class}` (class name only — the S1
  disclosure rule).
- `_usage_lane_detected` split: credentials-absent → omitted (today's meaning, now
  exclusive); detector RAISED → lane emitted `{provider, detected: false,
  error: <Class>}` — the Limits panel already renders per-lane failure since S1.
- `build_account_usage`: the three fail-open seams (`:3526-3547`) stamp
  `degraded: <Class>` on the envelope; `_render_account_usage_human` prints the claim
  "no signed-in providers detected" ONLY when detection RAN and found none, and
  prints `usage lanes unavailable (<Class>)` when degraded.
- `_fetch_usage_lane`: delete the fall-through (`:3355-3357`); unknown id → typed
  per-lane failure naming the id. Prove-dead: the closed tuple (`:164-169`), pinned.

**Tests** (extend `tests/test_harness_usage.py` + visibility suite):
- `a thrown catalog builder is named and the block absent` — **Anti-vacuity.**
  *Mutation:* keep the silent pass. *Probed fields:* no `catalog` key AND
  `block_errors["catalog"]` equals the injected class name, driven with two distinct
  injected classes. The silent mutant writes no `block_errors`; a constant cannot
  name two classes it never caught.
- `a healthy build has NO block_errors entry` — kills the always-write mutant; the
  pair pins absent-because-old vs absent-because-threw as different wire states.
- `a raising detector emits the lane with its class; absent credentials omit it` —
  two fixtures, same two-value discipline; probes membership + the error class.
- `a degraded envelope never prints the no-providers claim` — stdout capture: claim
  line ABSENT and degraded line present naming the injected class; the sibling
  fixture (genuinely none detected) asserts the claim line PRESENT. The pair pins the
  discriminator; neither blanket mutant passes both.
- `an unknown provider id never reaches fetch_account_usage` — *Mutation:* restore
  the fall-through. *Probed fields (second witness):* a monkeypatched
  `fetch_account_usage` recorder asserts ZERO calls AND the failure names the id. The
  recorder is test-owned; the mutant must call it to produce any snapshot.
- Existing S1 pins byte-unchanged.

**Mixed pairs.** All additive; the S1 commit's presence-detection argument covers old
launchers. One named risk in §6.4. **Rollback.** Revert. **Perf.** None.

---

### RD-L4 — launcher: the hold bills what it masks

**Defect removed, evidence §1.5.** The wholesale, unbounded mask stops being silent:
the moment a fold or office read delivers a row that a held pending entry is
overriding with DIFFERENT content, a receipt is emitted and the `holding` status
carries the count. The program's own doctrine — "the one shape this program keeps
refusing to ship is the cost nobody is billed for"
(`serve_office_subscriptions.py:98` READ) — applied to its newest cost. This stage
does NOT change the ranking (UP-1's ledger owns held-vs-acked-vs-remote authority)
and does NOT narrow the wholesale staging (intent attribution needs the ledger); it
produces the field evidence both of those decisions need.

**Change surface** (`mission_office_layout_controller.dart` + sync strip): in
`resolveLayout`, before the overlay overwrite loop (`:1807-1809`), compare each held
pending entry's contentKey against the freshly-seeded server row; differing →
`[MissionOfficeWrite] hold-mask: actor=<key> workspace=<ws>` once per (actor, hold) +
`holding.maskedCount`. Strip copy: "N office changes waiting — M conflict with newer
remote changes".

**Tests** (extend `mission_office_boot_hold_test.dart` — which today has NO
remote-change-during-hold coverage at all, SWEPT):
- `a divergent remote row during the hold is billed` — **Anti-vacuity.** *Mutation:*
  drop the comparison. *Probed fields:* the test log sink captures exactly one
  `hold-mask` line carrying the injected actor key AND `holding.maskedCount == 1`,
  driven with two fixtures/two keys. A constant-line mutant cannot name both keys.
- `an identical remote row is NOT billed` — kills the always-emit mutant; the pair
  pins the comparator on the contentKey (equal content must yield count 0, no line).
- **Second independent witness, pre-existing, byte-unchanged:** BW-L6's delivery
  witness (the fake repository RECEIVES the held payload after the healthy edge). A
  mutant that "resolves" the mask by dropping the held write to adopt the remote row
  passes every RD-L4 assertion and reds the delivery witness. The pairing proves this
  stage stayed receipts-only.
- `isSettled == false` while holding — existing assertion, byte-unchanged.

**isSettled/retire:** §2.1 — receipt + status field only. **Rollback.** Revert.
**Perf.** One map lookup per folded actor during holds.

---

### RD-L5 — launcher: the three silent-skip degrades take their honest sibling's shape

**Defect removed.** The c7a8e6043 class (a degraded read rendering as an honest
empty), three callers over — each with an honest sibling already in tree to copy
(SWEPT, spot-verified):
1. `initial_chat_context_dialog.dart:4386,4443` — `fetchPromptContextById` collapses
   spawn error / timeout / non-zero exit / undecodable payload into one null
   (bridge:2594-2624); the dialog silently renders the evicted context. "runtime
   evicted this" and "could not reach the runtime" must render differently — the
   chat panel's explicit-failure sibling (`mission_agent_chat_panel.dart:2384-2395`)
   is the shape.
2. `mission_agent_model_switcher_view_model.dart:885-902` — the BARE-token paste path
   skips unconnected lanes and answers "No connected provider offers …" without
   naming the 401'd lane that holds the model — the exact defect c7a8e6043 fixed for
   search, one arm over; the provider-model form arm (`:856-865`) is the honest shape.
3. `mission_control_hermes_visibility.dart:1266-1283` — `catalog` stays `{}` when
   `readModelsCatalog()` returns null/empty; only the throw arm records an issue.
   "no models.dev cache on disk" and "cache present, no match" both render as 0
   models; the parse-level arms (`:620-629,781-791`) already do it honestly.

**Change surface.** Per site: a typed outcome (or issues entry) distinguishing the
two meanings, rendered with the sibling's existing affordance. No new UI patterns.

**Tests** (one per site, same discipline): drive the two indistinguishable causes as
two fixtures; **probe the rendered discriminator** (dialog error state naming the
injected failure class; switcher message containing the injected 401'd lane id;
visibility issues entry naming cache-absent vs no-match). *Mutation:* restore the
collapse — both fixtures render identically, and the probe on the injected
name/id/reason (two driven values per site) cannot be satisfied by a constant.
*Second witness per site:* the honest sibling's existing test stays byte-unchanged —
a mutant "fixing" the site by rerouting through the sibling and breaking its contract
reds the sibling's own suite.

**isSettled/retire:** N/A (not on the office write lane). **Rollback.** Per-site
commits, independently revertable. **Perf.** None.

---

### RD-H6 — hermes: three seam-parity repairs (one commit each, grouped for reading)

1. **`read_model.enabled` read at two resolution scopes** — `snapshot.py:1758-1759`
   resolves profile-aware; `_cmd_snapshot` reads root-only
   (`runtime_commands.py:480-484`), so `harness snapshot`'s cache preference can
   disagree with `build_snapshot` for the same key (SWEPT) — the exact misplacement
   class that kept the delta-patch lane dark for its whole life. Fix: one resolver.
   *Witness:* both entry points driven against a config tree where profile and root
   disagree must answer identically (two-valued: profile=true/root=false and the
   inverse). *Mutation:* revert to root-only — the pair cannot both pass, because the
   two fixtures disagree in opposite directions.
2. **A roster fault answers the two create lanes differently** —
   `runtime.agent.create` refuses typed `persona_roster_unavailable`;
   `harness agent create` tracebacks (`persona_commands.py:6049` unwrapped, SWEPT;
   the honest arm exists at `agent_create.py:161-177,413`). Fix: wrap the CLI's
   roster read into the same typed refusal. *Witness:* corrupt-roster fixture → CLI
   exit is the typed reason, stderr carries NO traceback marker; *mutation:* unwrap —
   the traceback marker appears and the reason token does not. *Second witness:* the
   RPC lane's existing typed-refusal test, byte-unchanged (parity means both name the
   SAME token — the probe compares the two lanes' reason strings for equality).
3. **The roster-check bypass invariant is unenforced** — `_persona_is_unknown`
   returns False for ANY non-None persona (`agent_create.py:278-279` SWEPT); safe
   today because every caller resolves via the strict roster, but nothing fences the
   seam. Fix: a contract test (the removal-contract pattern) pinning every
   `perform_agent_create` caller's resolver to the strict path — the fence is a test,
   not runtime code, matching the cost of the risk. *Mutation:* add a caller passing
   a synthesized persona — the contract test reds by enumeration.

**isSettled/retire:** N/A. **Rollback.** Per-item revert. **Perf.** None.

---

### RD-L6 — launcher: the (c)-class deletions, receipts attached

- Delete the three `--expect-revision` argv lowerings (bridge:4090-4094, 4105-4109,
  4130-4134 SWEPT). Prove-dead receipt: R0-c's grep (no submit site passes the flag)
  recorded in the commit message, plus RD-L2 scoping the guard RPC-only — the params
  can never become reachable again. Anti-vacuity for a pure deletion: the
  removal-contract suite (`dead_symbol_removal_contract_test.dart` family) gains the
  three symbols; re-introduction is loud by enumeration — the mutation IS re-adding
  the lowering, and the contract test is the witness; no field probe applies.
- Record the corrected classifications in the same commit: `office.actor.restore`
  KEPT (operator recovery lane, no UI caller — Correction 4);
  `office.resolve_conflict` capability submit KEPT (unfinished main path, Plan E's).
- Explicitly NOT deleted here: the live office argv write arms (Plan C TC-3/TC-4 own
  their observation window and deletion), the stream child (TC-4, gated on RD-H1),
  the poll lane (TC; RD-L1 removes its tooth), the mass-archive tripwire (UP-4).

---

## 4. Sequencing constraints

1. **RD-0 lands first and alone** (FC-0/BW-0 precedent). R0-a wordsmiths RD-H1's
   claim; R0-b prices RD-L2; R0-c gates RD-L6; R0-d prices RD-L3/RD-H2 urgency.
2. **RD-H1 is hard-before Plan C's TC-4** (stream-child deletion) and should be
   verified live before TC-2's acceptance run. §1.6's classification (a) is the
   ruling-#60 justification: the child carries ordinary traffic the push lane
   cannot; that is a main-path bug (this stage), not a tolerated fallback.
3. **RD-H4 before RD-L2** — the revision guard is only as honest as its token, and
   RD-H4's archive-unreadable fix is what stops the token silently resetting to 1
   under the guard.
4. **RD-L1, RD-L2, RD-L4 all touch the office controller/read-model family** — Plan
   E's home surface, with WV-H4/WV-L6 (resolve) still unlanded there. Rebase-level;
   never land while an unmerged Plan E branch holds
   `mission_office_layout_controller.dart`. Order among them: RD-L1 (read model) is
   independent; RD-L2 before RD-L4 (both touch the hold/flush seam; L2 changes
   arguments, L4 only observes).
5. **RD-H2 and RD-H3 are independent of everything** and of each other; they are the
   cheapest high-severity stages and should not queue behind the launcher work.
6. **RD-L3 collides with BW-L5/L6's provider plumbing** (the serve-session lifecycle
   watch) — additive listener, rebase-level, but do not land while a BW branch holds
   `mission_control_provider.dart`.
7. **No-go zones respected**: nothing here touches `snapshot_build` /
   `_BUILD_COALESCE`, the boot receipt spans, or the projection-drops /
   parity-warnings HUD chips. RD-0's log reads count receipt LINES only; the §1.6
   double-build tally is classification evidence handed to Plan C's TC-0, not
   investigated here. RD-H3 changes when a batch demotes, never how the core is
   built.
8. **Register hygiene**: when RD-0 lands, strike the §1.7 stale sentences in place in
   the gesture plan (the stale-R#40-text precedent: four commit messages inherited a
   dead sentence before it was caught), citing this document.

## 5. Not in scope

- **The one-transport collapse** (TC-0..4) and the argv write-arm deletions — this
  plan feeds them (RD-H1, §1.6, R0-b) and does not perform them.
- **The `laneAbsent` page-open window** (item 10) — named as the main-path bug
  blocking TC-3's exit criteria; unowned; not staged here.
- **The page-open write storm** (item 9) — largest un-investigated office behaviour;
  unowned.
- **Narrowing the hold to the gestured actor and re-ranking held vs remote** — needs
  intent attribution: UP-1's ledger. RD-L4 deliberately stops at billing.
- **`runtime.office.resolve_conflict`** — Plan E WV-H4/L6.
- **Upstream-owned swallows** (`agent/account_usage.py:893-901`,
  `agent/auxiliary_client.py`'s mislabeled "payment / credit error" on a no-auth
  failure — LOG 08-17 08:29:01.987/.988 — and the gateway/TUI `/usage` surfaces
  still routing through the swallow, `cli.py:11061`, `gateway/slash_commands.py:4894`
  RAN) — route-around discipline; recorded for doc 19's ledger.
- **Hazard B's full fix** (unifying the subscribe lane's `_baselineOffset` with the
  read model's `_sequence`) — the honest arbiter between those two watermarks is the
  D7/TC target topology (one lane, one watermark); RD-L3 fixes the liveness half,
  R0-d measures the gap half, and the unification rides Plan C rather than a
  standalone stage here.
- **The snapshot hydrate cost and the HUD chips** — active no-go zones with agents
  on them.

## 6. Adversarial pass — what I most expect to be wrong

1. **R0-a can demote RD-H1's headline.** The drop is construction-verified, not
   field-observed: if every real folder change coalesces with a demoting neighbour
   inside the ~450 ms join window, the promoted folder-only frame is rare in practice
   and RD-H1 is "only" the TC-4 precondition plus the anti-recurrence structure. The
   stage survives either verdict; its commit message must carry the honest one (the
   §10.2 lesson: a timing claim recorded as fact is how this program gets burned).
2. **RD-H3's demote-on-empty could be masking a producer bug class rather than
   fixing it.** Forcing the full core is honest but expensive if one of the five
   re-opening paths fires often (e.g. the `:383-384` no-exception path on
   lazily-authored surfaces). The producer receipts in the same commit exist exactly
   so a frequent demote is attributable; if one path turns out hot, IT becomes a
   stage — this plan deliberately did not guess which.
3. **RD-L1's null-base choice could surprise a hot path.** If some undiscovered
   caller applies base-less cores more often than the four-failure chain suggests,
   the resync-instead-of-fold arm would fire visibly (it is loud by design). The
   discriminator test keeps stream hydrates folding; R0 cannot fully enumerate
   callers — the `snapshot_apply` writer set is small (three sites, SWEPT) but this
   is the stage where an unknown fifth caller would show up as new resyncs, not as
   silent discard — the correct failure direction, and receipted.
4. **RD-H5's lane-emitted-with-error changes the usage payload's membership
   semantics** (a lane can appear without data). The S1-era launcher renders per-lane
   errors; an OLDER launcher assuming "present ⇒ has usage" would render an empty
   tile. If R0 finds one fielded, gate the emission on a client-sent param — decide
   with the code open.
5. **RD-L2 changes operator-visible behaviour on a rare path** (a guarded remove
   refuses when a peer moved the actor concurrently). Plan E deferred D-W1 for
   exactly this; promoted here on ruling #60 and the hold-edge argument. If the
   operator overrules, the fallback position is receipt-only on the unguarded arms —
   strictly weaker, still billable; the stage says which shipped.
6. **The scout inventories are a floor, not a census.** Both sweeps were breadth-first
   under a no-spawn constraint; every stage-load-bearing claim was re-read first-hand
   (the surviving SWEPT tags are supporting detail), but §1.3's five re-opening paths
   and RD-L5's three sites are "the ones found", not "all there are" — R0-c's
   class-grep is the completeness check for the latter.
7. **Unverified live, all of it** — the standing confession: no gesture touched the
   running launcher, no serve child was spawned; launcher-side receipt counts (R0-b,
   R0-d) are the one evidence class this session could not read, and every dependent
   claim says so.

## 7. Verification log

| # | Fact | How established |
| --- | --- | --- |
| RD-R1 | Exactly 7 RPC methods; `resolve_conflict` absent; remove/surface handlers guard-validating and workspace-refusing before the store | RAN `@method` sweep; READ serve_rpc.py:1017-1136 |
| RD-R2 | Sink scope predicate admits only slash-prefixed `office_actor`; folder-only frame returns at :456 with no emit; `_delta_touches_workspace` shares the fork | READ serve_office_subscriptions.py:262-316,373-493 |
| RD-R3 | `office_surface` patch id is the bare workspace id; entity+token declared on both launcher lanes from one constant; cross-repo golden exists | READ state_patches.py:929-949; GIT 00cb07558 |
| RD-R4 | Baseline `or 0` coercion; typed except cannot fire on None; refusal vocabulary exists one module over | READ serve_rpc.py:683-703; READ serve_office_subscriptions.py:496-545 |
| RD-R5 | `patch_batch_frame` filters with no non-empty guard; promotion on coverability alone | READ stream.py:285-304,415-430 |
| RD-R6 | `_read_actor_dir` and `archive_actors_for_instance` swallow per-file; unreadable-archive revision reset | READ office_store.py:676-692; SWEPT :461-470 |
| RD-R7 | `applySnapshot` advances `_sequence` and keeps the old `_lastCore` when `rawCore == null`; poll callers are the null-rawCore sites | READ mission_read_model.dart:447-494; SWEPT bridge:2546/2552 |
| RD-R8 | Pending overlay entries survive the reconcile pass and unconditionally overwrite freshly-read server rows; requestedRemovals mask reads | READ mission_office_layout_controller.dart:1795-1824 |
| RD-R9 | `onReconnected` exists, mints `reconnect`, and has no production caller — stated twice in its own file | READ subscribe_lane:418; RAN grep; READ :832,847 |
| RD-R10 | S1 fix routes around upstream; block isolators + presence-detection in the same function; usage envelope fail-opens to `lanes: []` with the false-positive claim line; provider tuple closed; fall-through unreachable | GIT 1d1ef64692; READ harness.py:161-169,3083-3101,3296-3357,3520-3563 |
| RD-R11 | Live log: 14 demote builds = 7 offsets ×2 (08-16 19:22 → 08-17 08:29); zero office re-baseline receipts across all rotations | LOG/RAN (counts only) |
| RD-R12 | BW-L6 hold: held entries pending ⇒ isSettled false; hold never discards; wholesale staging; terminal keep + retry re-entry; no remote-change-during-hold test | GIT 0f688d32f; READ :1803-1809; SWEPT controller/test cites §1.5 |
| RD-R13 | Fence-incident on-disk tally 14+1+9 / 24 (11/13); park at 5-in-60s | READ Plan F §0 |
| RD-R14 | Absence-means-delete inference deleted 2026-08-15; tripwire demoted to backstop deliberately | Plan E §0 Corr. 1; GIT 7623f99cf cited there |
| RD-R15 | Agent-create: every minting lane validates via one strict roster predicate; the two residual gaps are the unenforced resolver invariant and the CLI traceback divergence | SWEPT agent_create.py:161-177,249-287,407-419; persona_commands.py:462-464,536,699,6049 |
| RD-R16 | Sink test file has no office_surface case; persona-drop test pins a still-correct behaviour | RAN/READ test_serve_rpc_office_subscribe.py:374-386 |
| RD-A1 | Folder-only batches promote un-coalesced in the field | ASSUMPTION — R0-a |
| RD-A2 | argv write arms carry ordinary gestures only inside the laneAbsent window | ASSUMPTION — R0-b |
| RD-A3 | No fifth base-less full-core caller; no fourth RD-L5-class silent-skip site | ASSUMPTION — R0-c + §6.3 |
