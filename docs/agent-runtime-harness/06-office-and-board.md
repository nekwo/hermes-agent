# 06 — The office surface: how Mission Control's level writes and reads scene state

The Mission Office is the canvas where a persona placement is a desk, a drag is a
write, and a palette drop mints an agent. This doc is the CURRENT truth of that
surface: which verbs the level may call, how a write comes back as a fold instead
of a 5-second rebuild, and what the operator sees between letting go of a chip and
the runtime agreeing the actor exists. Every claim below was re-checked against
code, receipts, or the live diag log on 2026-08-22 — anything that could not be
is quarantined under `## Unverified carry-forward`, and anything not yet built
lives in `planned/`, not here. The goal/task mission lane was removed 2026-07-30
and appears nowhere on this surface.

---

## The write verbs — four gestures, one RPC lane

Every office mutation is one of four verbs, and all four are registered JSON-RPC
methods on the serve child today (`agent_runtime/serve_rpc.py`, `@method(...)`):

| Gesture | Method | Handler | Ack |
|---|---|---|---|
| place / move | `runtime.office.upsert` | `_runtime_office_upsert` (`serve_rpc.py:981`) | `{actor_key, revision}` |
| delete | `runtime.office.remove` | `_runtime_office_remove` (`serve_rpc.py:1268`) | `{actor_key, revision, state}` |
| folder taxonomy | `runtime.office.surface.update` | `_runtime_office_surface_update` (`serve_rpc.py:1483`) | `{workspace_id, folders, revision}` |
| realm-sync resolve | `runtime.office.resolve_conflict` | `_runtime_office_resolve_conflict` (`serve_rpc.py:1661`) | `{actor_key, take, state, revision?}` |

Three ack shapes are load-bearing and the handler docstrings say why. The remove
returns the **post**-archive revision, because `_archive_actor_locked` bumps on
the way out and an archived key carries the number forward through a restore — a
pre-archive number would hand the client a guard token already one behind. The
remove also carries a constant `state` word so a decoder cannot mistake a deletion
ack for a placement ack (`{actor_key, revision}` means the opposite thing).
The surface update echoes the folder list **as the store normalized it**, and
`folders` is a LIST on the wire: the capability lane's comma-join is an argv
artifact that splits `"Design, Ops"` into two folders, and a typed lane does not
copy an encoding's accidents. The resolve echoes the STORE's key and a normalized
`take`, because `take=remote` writes the key the peer's record carries and a
sidecar's filename may disagree with its record.

Launcher side, all four are RPC-first through one writer
(`office/mission_office_rpc_writer.dart`: `upsertActor:76`, `removeActor:167`,
`updateSurface:257`, `resolveConflict:352`), each gated per-method on the serve
manifest. The argv capability lane survives on exactly one arm — `Unavailable` —
and a **refusal is terminal and never falls back**
(`mission_office_layout_controller.dart:351`). The per-flush receipt is
`kMissionOfficeWriteLaneReceiptLabel = 'write lane:'` (`:434`), and the live log
reads `[MissionOfficeWrite] ws_codex-test-workspace_28d285 write lane: 1 rpc, 0 cli`
on every 2026-08-22 flush — in the ISO-stamped log era the argv arms are fielded
and unexercised (the undated era carries 4 `0 rpc, 5 cli (fallback: laneAbsent)`
flushes; see the `laneAbsent` carry-forward), which is the evidence R#42's
deletion criterion asks for.

The layout mutation path itself mints one correlation id per gesture
(`office/mission_office_correlation.dart`), stamped **before** the await, so a
call whose reply never lands is still nameable. That token exists because on
2026-08-16 a timestamp inference read "deletes take 3.8 s" for writes that took
280–368 ms — the launcher's flush receipt lags the RPC by 250–650 ms — and a
wrong prioritisation followed from it.

## The fold model — what a fold is, and who promotes a batch

> The generic patch-frame contract and the office push lane's re-envelope live in
> [03 — Transport and wire](03-transport-and-wire.md) §4 and §6. This section is
> the office's own half: the `office_surface` entity, the two-lane fence, and what
> the level declares.

A hermes write emits a domain event AND, inside the same lock, a `state.patched`
row. When every event in a coalesced batch is *coverable*, the batch ships as a
patch frame the launcher merges into its held core; when even one event is not,
the whole batch demotes to a full core — a `build_snapshot()` the client waits
on. `patch_coverage.py` is conservative by construction: one uncovered event
demotes everything (`patch_coverage.py:25-40`).

Promotion is **negotiated per client** by capability tokens, not decided by the
server alone. The office surface's own fold is `office_surface`
(`state_patches.py:1019`, `emit_office_surface_patch` at `:1089`), gated on
`OFFICE_SURFACE_FOLD_CAPABILITY = "office_surface_fold"`
(`patch_coverage.py:179`), which is what lets `office.surface.updated` join the
covered set (`patch_coverage.py:331`). It is a **subset merge** of
`{folders, revision, updated_at}`, not a row replace, because the office row also
carries actor lists, counts and ledger keys this write does not move. The launcher
declares both strings in one authority list — `kMissionFoldDeclaredEntities`
(`data/mission_read_model.dart:161`) — used verbatim by the argv stream child and
the `runtime.office.subscribe` request, so the two lanes cannot drift.

Where a surface write is genuinely unfoldable, hermes emits an accounted
**refresh** instead of pretending: `emit_office_surface_refresh`
(`state_patches.py:1156`) exists because archiving an orphaned surface removes the
office row and every actor under it in one move, and a covered event with no patch
beside it would ship an EMPTY patch list — advancing the client's watermark having
folded nothing, keeping the archived surface and its chip forever.

### The fold fence, and why it stopped firing

Two subscribers carry the same rows into one `MissionReadModel` — the NDJSON
`harness stream` child and the `runtime.office.subscribe` push lane — on purpose,
with the stale gate as the intended dedup. The gate ran at `prepareFold` while the
fold was prepare → off-isolate re-projection → commit, so two deliveries inside
one window both passed against the same base: a check-then-act race whose outcomes
were a fenced resubscribe, or the same batch committed twice.

Both halves are fixed and both fixes are live. `_foldChain`
(`data/mission_control_bridge.dart:2420`, `_enqueueFold` `:2429`) serializes every
fold from both lanes, so the loser stale-drops at prepare having paid no
projection; `MissionReadModel` records `coreRevisionWriter` (`:430`) at each of
its bump sites so a surviving fence NAMES the writer that moved the base
(`mission_control_bridge.dart:2459`, `:2650`) instead of leaving it to adjacency
inference. Live evidence, 2026-08-22 diag log: **zero** `fold:fenced`, **zero**
`REFUSED fenced`, **zero** `push:full_core` resubscribes in the ISO-timestamped
era; the only residual causes are `fold:no_base` (6) and `fold:gap` (2).

`fold:no_base` is itself a fix, not a defect. A full-core apply that supplies no
raw core now NULLS the retained fold base (`mission_read_model.dart:563-577`), so
the next patch takes the typed `patch_without_base` refusal → `no_base` resync
(`:1309`) → a forced hydrate on the one lane that provably supplies a core. The
alternative — folding onto a stale base and publishing it as truth — was
silently discarding whatever the poll's core carried, invisibly to the fence.
The rejected repair is recorded too: threading a raw core through the CLI poll
would make a fallback lane MORE capable, against rulings #42/#60.

The subscribe carries an optional `reason` so the server log can join the client's
resubscribe ladder — boundary-validated (≤64 chars, `[a-z0-9_:.-]`, refused
`-32602 reason_invalid` before any store, hub or producer call) and stamped on the
receipt, with absence printed as `-`. The docstring states the rule out loud: *a
cause the client chose is evidence, never authority* — a server that branched on
it would be taking dispatch orders from an untrusted string
(`serve_rpc.py:725-740`).

## Optimistic rendering vs snapshot truth

The page used to hold a whole-layout override, and on 2026-08-15 a predicate
redefined in a different file for a correct and unrelated reason caused the paint
path to discard that override on every frame of a drag — the office's optimistic
paint died for every gesture at once, with no test to say so.

That class is now unrepresentable. The office runs an **intent ledger**
(`office/mission_office_intent.dart`), keyed by surface id, and the paint path
reads the layout provider UNCONDITIONALLY and exactly once, then applies
`withPendingIntents(surfaceId:, server:)` on top
(`mission_control_page.dart:2881-2894`). `_officeLayoutOverride`,
`_hasPendingOfficeSave`, the override branch and the paint-path `isSettled` caller
are deleted, each with a row in `mission_control_tombstone_registry_test.dart`
(`:2980`, `:2995`) so they cannot come back. The ledger is keyed by SURFACE rather
than workspace deliberately: not every surface is harness-backed, and a ledger
hanging off `_WorkspaceSync` would have left SharedPreferences-backed surfaces
flickering on every gesture.

The ledger deliberately drops two of the five fields UP-1 sketched. `move` vs
`create` is not a kind — the distinction is `knownToServer`, which already lives
in `serverKeys`; and `predictedRevision` is not on the record, because the
revision authority sits at SEND time and a number stamped at STAGE time would be
the staler of two answers to one question.

**A drag is now one write, not hundreds.** `_handlePanUpdate` no longer calls
`onMoveSceneItem`; it records the commit position and asks the game to echo the
node at the cursor with no write at all
(`office/mission_office_mount.dart:362-373`, `office/mission_office_game.dart:284-305`).
`_handlePanEnd` emits the move and the commit **together or not at all** — firing
the commit alone would ask the write lane to flush somebody else's staged edit
early, which a camera pan or an unmoved node press would otherwise do (`:374-391`).
The `'Moved '` display-string gate that used to route the debounce — control flow
on operator-facing text — is deleted (`mission_control_page.dart:3713`).

**Snapshot truth is joined separately, and the type is the guard.** The
`roster_confirmed` mark reads `MissionControlSnapshot.offices[].actors[].actorKey`
— the producer's own folded state — never the page's overlaid layout
(`mission_control_page.dart:2310-2340`). Joining on the overlaid layout would find
the actor in the very turn that placed it and report near-zero on every drop
forever. The parameter TYPE is a `MissionControlSnapshot`, so rewiring it to the
overlay does not type-check.

**The drop pipeline** is instrumented end to end by `MissionDropTimeline`
(`data/mission_drop_timeline.dart`, 954 lines) with five phases —
`drop_started` (`:76`), `layout_mutated` (`:83`), `rpc_settled` (`:87`),
`roster_confirmed` (`:118`), `first_paint` (`:126`) — emitted as one
`[MissionDropTiming]` line at settle. Four honesty rules are enforced by the file:
an unresolved phase is ABSENT, never a fake `0`; marks come off a monotonic
`Stopwatch`; first mark wins within a drop; and the line ships in RELEASE, unlike
the chat timings that sit inside `assert` and compiled out of the 2026-08-09
analysis entirely. `rpc_instance_ms` is hermes' own `phases.instance_ms` ECHOED,
never derived by subtracting a launcher stamp from a hermes stamp — the diag log
is local-with-no-dates and hermes records are UTC, and that boundary produced two
misreads in one day.

**The pending-chip lane** (`office/mission_drop_pending_chips.dart`,
`office/mission_drop_pending_chip_layer.dart`) draws one chip per in-flight drop,
anchored to the placement's WORLD position and pushed through the scene's own
camera projector, because office actors are Flame components with no render box.
It says *placed, waiting for the runtime to confirm* and it may **never** say
"ready" — readiness is the roster's verdict, rendered where the roster is. With
no projector it degrades to a corner rail that still names the persona: "no
anchor" degrades to "wrong place", never to "gone".

## Drop latency — current numbers

The receipt's field list is catalogued in
[07 — Observability](07-observability.md), and the headline numbers are carried in
[08 — Performance and debt](08-performance-and-debt-ledger.md); this table is the
office's own read of them, boot by boot. From the live diag log
(`%TEMP%\eternia_launcher_diag.log`), one boot per row:

| Boot | Drop | `layout_mutate_ms` | `rpc_ms` | `rpc_instance_ms` | `roster_confirmed_ms` |
|---|---|---|---|---|---|
| 14:50Z | drop-1 (cold) | 19 | 2086 | 2030 | 8309 |
| 14:50Z | drop-2 (warm) | 16 | 121 | 78 | 4506 |
| 17:43Z | drop-1 (cold) | 20 | 1093 | 1046 | 4645 |
| 17:43Z | drop-2 | 15 | 360 | 78 | 841 |
| 17:43Z | drop-3 | 13 | 224 | 186 | 506 |

The node lands in ~20 ms every time; what the operator felt was the roster window.
The 14:50Z `roster_confirmed=8309` decomposed server-side into THREE full demote
builds at the SAME offset; the 17:43Z row is the same gesture after same-offset
demote reuse landed, and the tail is down by ~44%.

**The cold-create attribution is closed.** The 14:50Z open row said the 2,030 ms
first create was not a prewarm miss and needed one live drop on the new build to
name its owner. That drop happened. `agent_create_phases persona=qa
instance_ms=1046 phases=… create_patch_ms:984, chat_lane_scope_ms:859,
tool_visibility_ms:125 …` (serve `agent.log`, 13:43:53 local = the 17:43Z drop-1):
the cold cost is the **chat-lane tool-scope application**, 859 of 1,046 ms, and
the warm drop-2 pays `chat_lane_scope_ms:0` for a 78 ms create. `sprite_source=absent`
on every row — the `qa` persona binds no sheet, so `first_paint` is correctly
absent and the sprite lane owns none of this.

## HUD chips — what the investigation concluded, what shipped

Three header chips read from one parity envelope. The 2026-08-17 investigation's
verdict was that two were true alarms caused by *that week's own tooling*, and the
third was an observability artifact over a real 24 s window.

- **`projection drops N`** counted the residue of the operator's own first-class
  retires as anomalies. Fixed at the emission site: a session whose binding
  resolves in the persona-instance archive now drops as `instance_retired` with
  `by_design=True` (`persona_chat_history.py:401`), so the count means *lost*
  data. The chip also gained the disclosure the other two had —
  `anomalousDropSummaries` (`data/mission_control_snapshot.dart:345`, parsed
  `:263`, passed to the alert `:1070`).
- **`parity warnings N`** was a live-store pollution alarm with a smoking gun: a
  test called `monkeypatch.undo()` mid-body, which unwound the package's autouse
  root pin and let the next line write the operator's real store. Both halves
  landed — a structural gate (`tests/agent_runtime/test_no_midtest_monkeypatch_undo.py`)
  and a teardown tripwire, hoisted 2026-08-18 to `tests/conftest.py:545`
  (`_shared_monkeypatch_pin_tripwire`; the incident narrative stays in
  `tests/agent_runtime/conftest.py`'s docstring) — plus a
  first-class `archive_orphaned_surface` verb so the operator can clear the
  leaked surface without hand surgery.
- **`snapshot build Nms`** was ONE build wearing three log lines: the line is
  emitted per *hydrate caller* and measures that caller's WAIT. The vocabulary
  now says so — `led` / `rode` / `shared_next` (`snapshot.py:283-285`) plus a
  fourth, `cache`, added when the persisted-core fingerprint hit — printed on
  `snapshot_build_core role=… caller=… generation=… build_ms=… offset=…`
  (`snapshot.py:403`). **A boot's build count is the count of `led` lines, never
  the count of lines.** The provider prewarm was moved behind the read-model
  build on one thread (`serve.py:3290-3300`, injected rather than hardcoded) so
  its SDK import stops contending with the boot-critical build.

## The agent console — the dead lane and the limits chip

The 2026-08-16 investigation found the two defects did not share a cause but
shared an unnamed aggravator: **the console and the operator's shell interrogate
two different Hermes homes**, so `hermes auth list` in a shell was never a
diagnostic for the console. It also *corrected* the proposed mechanism — a failing
lane is NOT filtered out of the catalog; its 91 models load and the lane renders
with its verbatim 401 under "Not connected". What was genuinely silent was the
search gesture, the all-lanes-down collapse, and home provenance.

All four surfacings shipped: `searchMissionAgentUnavailableModels`
(`mission_agent_model_switcher_view_model.dart:726`, rendered
`agent_model_menu.dart:467`) so typing a model name on a dead lane no longer
renders copy identical to a typo; the `catalogUnavailable` collapse carries the
lane reasons (`agent_chat/mission_agent_model_switcher_view_model.dart:497-505`);
`probedHomeCaption` puts the probed home on screen
(`agent_model_menu.dart:235`); and the swallowed 401 became a stated status —
`usage {phase} failed (HTTP {status} — re-auth may be required)`
(`hermes_cli/harness.py:3840-3843`), with class-name-only discipline preserved for
everything that is not an HTTP status, because a bare status code leaks nothing.

## The board — still capability-only

The board is the office's sibling surface and it has NOT made the same journey.
All six board writes go out as argv capabilities — `board.card.add`, `.move`,
`.edit`, `.archive`, `.restore`, `board.resolve_conflict`
(`board/mission_board_write.dart:133-219`) — with no `runtime.board.*` RPC method
registered in `serve_rpc.py`. Board writes are named in the uncovered list
(`patch_coverage.py:33`), so a board batch demotes to a full core by design.
One asymmetry worth knowing: the board DOES send `expect_revision`
(`mission_board_write.dart:166,192`, sourced from `card.revision` at
`mission_board_card_panel.dart:233`), while the office's argv arms deliberately
omit it — the office's revision guard lives on the RPC lane only.

## Invariants

1. **A refusal is terminal; only `Unavailable` falls back.** The argv lane is not
   a second chance at a guard the store already refused.
2. **The canvas renders `server state + pending intents`.** A gesture is visible
   because it is in the ledger, never because a caller remembered to poke an
   override. There is no retire decision on the paint path to get wrong.
3. **The paint path always reads.** A predicate that suppressed the only read
   which could clear it shut the office lane for a whole process once
   (`mission_office_lane_reattach_test.dart`); a path that always reads cannot.
4. **An unresolved phase is absent, never zero.** `sprite_ms=0` would claim the
   sheet arrived instantly, which is the opposite of what happened.
5. **Never cross the wire with a clock.** Server-measured spans are echoed, never
   derived from a launcher stamp minus a hermes stamp.
6. **A client's declared cause is evidence, never authority.** No server dispatch
   branches on a `reason` string.
7. **One uncovered event demotes the whole batch**, and a covered event with no
   patch beside it is data loss — hence the accounted `refresh`.
8. **A boot's build count is the count of `led` lines.**
9. **Fold coverage is negotiated per client by capability token**, so an
   undeclared client keeps today's full cores and never receives a patch it
   cannot fold.
10. **A staged office change is never silently dropped.** A terminal hold keeps
    the intent alive, bills what it masks (`hold: N staged, lane=terminal`), and
    waits for delivery or a loud refusal with the repair surface on screen.
11. **The office fences pass byte-unchanged or the stage stops.**
    `mission_office_optimistic_paint_test.dart`,
    `mission_office_lane_reattach_test.dart`,
    `mission_office_mass_archive_incident_repro_test.dart` — an edit to any of
    them is a stage-stopping event, not a test update.
12. **Dead-symbol claims are repo-scoped or they are nothing.** A file-scoped grep
    answers "is it used here", not "is it dead"; and a `file:line` citation goes
    on reading as verified long after the code at it has moved.

## Unverified carry-forward

- **The `~4.3 s laneAbsent` window on page open.** The degrade path is live in
  code (`office/mission_office_rpc.dart:153,640,720`) and the plans of 2026-08-16
  size the window at ~4.3 s, but the current diag log's ISO-timestamped era
  contains **zero** `laneAbsent` lines, so the number is neither confirmed nor
  refuted today. Source: `OFFICE_WRITE_VERBS_RPC_PLAN_2026-08-16.md` §4.
- **The page-open write storm ("item 9").** Named unowned by three separate
  2026-08-16 plans; not re-derived from a live boot this pass. Source:
  `OFFICE_WRITE_VERBS_RPC_PLAN_2026-08-16.md` §4 / §5 D-W4.
- ~~**`office.actor.restore` is fully dead.**~~ VERIFIED AND EXECUTED
  2026-08-22: the launcher delete audit re-proved zero submit sites plus
  `localOnly` exposure, and the shell (registry row + argv lowering) was
  deleted in launcher `e38bb108c` with w23 tombstone rows in `379e70d5c`. The
  hermes CLI verb `actor-restore` deliberately survives pending the launcher
  docket's ruling 2 (kept product feature vs deleted with its store arm).
- **The office controller's incident-repro test exercises the argv fallback, not
  the RPC path**, because it never overrides `missionOfficeRpcWriterProvider` —
  so a regression making the RPC arm always `Unavailable` would leave it green.
  Flagged 2026-08-17; the override list was not re-audited this pass. Source:
  `SCOUT_LAUNCHER_LANE_MAP_2026-08-17.md` §4.

## Open rows

- The placement verb: `harness agent create` gains server-side layout, a
  skills phase with an install gate, a store-level desk fence, an RPC-first
  inverse, and a live proof that a second-process create reaches the fold →
  [planned/agent-placement-verb.md](planned/agent-placement-verb.md)
  (both repos; its §0 corrects four premises of the 2026-08-24 brief)
- Gesture prediction's two remaining stages (an unpinned create-refusal
  retraction, and adoption still trusting the client's own content key) →
  [planned/office-gesture-prediction-remainder.md](planned/office-gesture-prediction-remainder.md)
- Collapsing the office write lane to one transport (guarded remove, argv-arm
  deletion, the unbuilt restore verb) →
  [planned/office-write-lane-collapse.md](planned/office-write-lane-collapse.md)
- The page-open write storm and the `laneAbsent` window on cold open →
  [planned/office-page-open-write-storm.md](planned/office-page-open-write-storm.md)
- The board surface's missing RPC lane and fold coverage →
  [planned/board-surface-rpc-lane.md](planned/board-surface-rpc-lane.md)
- Remaining console lane-ambiguity: browsable catalog under a failing lane, the
  bare-token paste path, catalog empties →
  [planned/agent-console-lane-honesty.md](planned/agent-console-lane-honesty.md)

## Supersedes

- [OFFICE_WRITE_VERBS_RPC_PLAN_2026-08-16.md](archive/2026-08-22-pre-consolidation/OFFICE_WRITE_VERBS_RPC_PLAN_2026-08-16.md) — all four verbs shipped; its remaining deferrals are in `planned/`.
- [OFFICE_FOLD_FENCE_CONTENTION_PLAN_2026-08-16.md](archive/2026-08-22-pre-consolidation/OFFICE_FOLD_FENCE_CONTENTION_PLAN_2026-08-16.md) — FC-0/FC-H1/FC-L2 all shipped; the fence class is zero on live receipts.
- [OFFICE_GESTURE_FOLD_PROMOTION_PLAN_2026-08-16.md](archive/2026-08-22-pre-consolidation/OFFICE_GESTURE_FOLD_PROMOTION_PLAN_2026-08-16.md) — the fold model and its `R#nn` register; the mechanism it designed is the one described above.
- [OFFICE_OPTIMISTIC_RENDER_REGRESSION_PLAN_2026-08-16.md](archive/2026-08-22-pre-consolidation/OFFICE_OPTIMISTIC_RENDER_REGRESSION_PLAN_2026-08-16.md) — OR-1/OR-2 shipped and were then SUBSUMED by the intent ledger, exactly as OR-4 required. OR-0 and OR-3 have no separately recorded disposition — the shipped surface is the intent ledger described in the body; consult the archived plan before citing either stage.
- [UNIFIED_GESTURE_PREDICTION_PLAN_2026-08-16.md](archive/2026-08-22-pre-consolidation/UNIFIED_GESTURE_PREDICTION_PLAN_2026-08-16.md) — UP-0/1/2 shipped and UP-4 struck at source; UP-3/5 in `planned/office-gesture-prediction-remainder.md`.
- [HUD_CHIPS_INVESTIGATION_PLAN_2026-08-17.md](archive/2026-08-22-pre-consolidation/HUD_CHIPS_INVESTIGATION_PLAN_2026-08-17.md) — all five stages shipped.
- [AGENT_CONSOLE_DEAD_LANE_AND_LIMITS_PLAN_2026-08-16.md](archive/2026-08-22-pre-consolidation/AGENT_CONSOLE_DEAD_LANE_AND_LIMITS_PLAN_2026-08-16.md) — S1–S4 shipped; the two-homes split is the standing finding.
- [SCOUT_LAUNCHER_LANE_MAP_2026-08-17.md](archive/2026-08-22-pre-consolidation/SCOUT_LAUNCHER_LANE_MAP_2026-08-17.md) — its Hazard A and Hazard B are both CLOSED (see the fold section and `onReconnected`'s production caller at `mission_office_subscribe_lane.dart:787`); do not re-quote them as open.
