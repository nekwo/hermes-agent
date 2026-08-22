# The invisible drag — the page retires the operator's own gesture (2026-08-16)

> **Home.** Hermes repo, beside `UNIFIED_GESTURE_PREDICTION_PLAN_2026-08-16.md`, whose §1
> correction block names three open questions — (a) does the host rebuild during a drag and does the
> override reach it, (b) does `_reconcileAvatars` move existing avatars, (c) which commit broke it —
> and scopes them out to this document. All three are answered below. Launcher paths are relative to
> `X:/Unreal Engine/Engine/Launcher/EterniaLauncher` at `2a2db7467`.

**Evidence tags** (the fold plan's discipline): **READ** (file:line inspected this session) ·
**RAN** (command executed this session) · **MEASURED-LIVE** (operator gesture against the built
runtime, relayed by the coordinator with instruction to trust it) · **RELAYED** (told to me, not on
disk) · **ASSUMPTION A-n** (unverified; OR-0 verifies before anything builds on it).

---

## 0. Verdict up front

**The `interactionMode` framing is wrong, and the unified plan's §1 correction was right to
withdraw it.** The dead field explains nothing: the drag never rendered through a game-side special
path. It rendered through the ordinary one — page override → host model → mount `applyModel` →
`_reconcileAvatars` — and that chain still works end to end (§1). What broke is upstream of all of
it: **the page's paint-path resolver now discards the optimistic layout override, every frame of a
drag, in favour of the stale provider layout** (§2).

**The regression commit is `91fec5ddb` — 2026-08-15 15:19 -0400, "fix(mission-office): a state only
a read could leave was also the state that stopped every read."** It changed
`MissionOfficeLayoutController.isSettled` from "fully reconciled" to "no writes in flight" — a
correct fix for the read-suppression deadlock it names — and, as an unexamined side effect, armed a
retire branch on the page's paint path that had been sitting inert since 2026-07-17 (§3). The drag
was visible before that commit because the deadlock it fixed kept `isSettled` false essentially
forever, which kept the retire branch from ever firing. **The working drag and the dead read lane
were the same bug.** Fixing the second silently removed the first.

The palette place fails to render optimistically through the same branch, with two aggravators of
its own (§4).

---

## 1. The render chain is innocent — open questions (a) and (b)

**(b) `_reconcileAvatars` MOVES existing avatars.** READ `mission_office_game.dart:273-317`: for
every agent node, found-or-created, it runs `avatar.applyTransform(AvatarTransform(position:
node.drawAnchor, ...))`. READ `packages/eternia_spatial/lib/src/render/avatar_component.dart:191-217`:
`applyTransform` assigns `position = projection.worldToScreen(ground)` unconditionally — no tick
dedupe (the constant `tick: 1` is inert), no lerp, no early-out. Desks are props; `_reconcileProps`
(`mission_office_game.dart:319-329`) rebuilds the full descriptor list with `node.center` each
application. **Any model that arrives with new positions paints them.**

**(a) The host rebuilds during a drag, and nothing host-side shadows the page.** The page calls
`setState` on every pan frame (`mission_control_page.dart:3690-3695`), so the subtree rebuilds; the
host's own `_layoutOverride` (`mission_office_host.dart:125`) is set only by pet-picker /
hide-persona / character-draft actions and is null during a drag, so `effectiveLayout =
_layoutOverride ?? widget.layout` (`:147-150`) passes `widget.layout` through. The mount's identity
guard (`mission_office_mount.dart:110-114`) passes because the host builds the model fresh each
`build()` (`mission_office_host.dart:154-165`). `applyModel` runs every frame of a drag.

**It runs with the wrong layout.** `widget.layout` is `officeLayout` from the page
(`mission_control_page.dart:838`), which is `_resolveOfficeLayout(read: false)` (`:639-640`) — and
that is where the drag dies.

---

## 2. The mechanism — the retire branch eats the override

READ `mission_control_page.dart:2779-2814`, `_resolveOfficeLayoutOrNull`:

```dart
final override = _officeLayoutOverride;
if (override != null && override.surfaceId == surfaceId) {
  final workspaceId = MissionOfficeSurfacePolicy.workspaceIdOf(surfaceId);
  if (workspaceId != null &&
      ref.read(missionOfficeLayoutControllerProvider).isSettled(workspaceId)) {
    final (fresh, freshLoading) = loaded(surfaceId);
    if (fresh != null && !freshLoading) {
      scheduleMicrotask(() { ... _officeLayoutOverride = null ... });
      return (fresh, false);          // ← the stale provider layout wins
    }
  }
  return (override, false);
}
```

The frame-by-frame walk of a drag (all READ):

1. Pointer moves → `_handlePanUpdate` streams `onMoveSceneItem` (`mission_office_mount.dart:236-249`)
   → host `_moveSceneItem` (`mission_office_host.dart:891-905`) → page `_moveSceneItem`
   (`mission_control_page.dart:3500-3517`) → `_mutateMissionOfficeLayout` with a message starting
   `'Moved '` → the **debounced** arm (`:3689-3707`): `setState(_officeLayoutOverride = next)`, and
   the save is parked behind a 220 ms **trailing** timer that is *cancelled and re-armed on every
   pan frame*. During continuous motion the save never runs.
2. Because the save never runs, the controller never stages anything: `writesInFlight` — `timer !=
   null || flushing || any overlay entry pending` (`mission_office_layout_controller.dart:406-407`)
   — is false, so `isSettled` (`:440-441`) answers **true** for the whole gesture.
3. The frame builds → the retire branch fires → the paint path returns `fresh`, the **pre-drag**
   provider layout, and schedules a microtask that nulls the override the gesture just wrote.
4. Host → mount → `applyModel` → `_reconcileAvatars` all run faithfully — re-applying the old
   position. The node appears frozen under the cursor.
5. Steps 1-4 repeat per pointer move: the override is re-set, discarded, and cleared, every frame.
6. Release → `onMoveSceneItemEnd` → `_commitMissionOfficeLayoutNow` (`:3744-3760`) → the pending
   save finally runs → `applyLayout` + `commitNow` → flush → ack → `ref.invalidate(provider)`
   (`:3832`) → the reload carries the final position → **the node pops to where it needed to go.**

That is the operator's report, mechanism for mechanism (MEASURED-LIVE: "dragging doesn't work but
on release it pops where it needs to go, i just cant see what i'm dragging").

Two structural notes worth keeping:

- The gesture's only in-flight representation — the page's own 220 ms pending save
  (`_officeLayoutPendingSave` / `_officeLayoutSaveDebounce`, `:3697-3707`) — is **invisible to
  `isSettled`**, which only consults the controller. The page asks the controller "is anything in
  flight?" while itself holding the very write that is.
- The one window where a drag frame WOULD render is `freshLoading == true` — the provider reloads on
  every snapshot publish (READ, the `:2767-2778` doc comment) and a reloading provider protects the
  override. The office-push work (O-H4, 2026-08-15/16) deliberately reduced exactly that churn, so
  the accidental protection thinned out in the same window the retire branch went live. This is a
  contributing-visibility note, not the cause (ASSUMPTION A-1: not measured how often a reload
  overlapped a drag before O-H4).

---

## 3. The archaeology — open question (c), three commits, one verdict

| Commit | Date | What it did to this path | Verdict |
| --- | --- | --- | --- |
| `b2f815cd8` | 2026-06-28 | Renderer rewrite; `interactionMode` born with no consumer (RAN: `git log -S interactionMode` → exactly this commit) | **Not the cause.** The field was dead for the six-plus weeks the drag demonstrably worked (MEASURED-LIVE, operator: "the drag used to work fine"). |
| `c8338ec42` | 2026-07-17 | Realm sync (W-L1/W-L2): added the retire branch to `_resolveOfficeLayoutOrNull` AND `isSettled`, then defined as `settled` — `overlay.isEmpty && removed.isEmpty && timer == null && !flushing` (RAN: `git show 91fec5ddb^:...layout_controller.dart`, line 257) | **Armed the gun.** Overlay entries drain only when a read reconciles them, and the page suppressed reads while holding an override with `isSettled` false — `91fec5ddb`'s own commit message documents that deadlock. Net effect: after the first office write of a process, `isSettled` was false ~permanently, the retire branch ~never fired, and the per-frame override painted. **The drag worked by riding the deadlock.** |
| `91fec5ddb` | 2026-08-15 15:19 | Redefined `isSettled` to `!writesInFlight` — an acked-but-unreconciled write no longer blocks (`:432-441`) | **Fired it.** Correct for the read lane; fatal for the paint lane. With acked state no longer pinning `isSettled` false, a mid-drag frame (nothing in flight, by construction of the trailing debounce) now satisfies the retire branch every time. |

RAN: `git log -S isSettled -- lib/features/mission_control/mission_control_page.dart` → only
`c8338ec42`; the page's branch structure is unchanged since birth — only the controller's answer
changed underneath it. Tonight's commits (`8e9e390af` commit-on-release, `520ac308c` AC-2,
`5d9d23e19` D3) touch when the write *starts*, not what the paint path *returns*; U-3 of the
unified plan already pinned the mount's during-drag path as unchanged.

Honest edge on the "used to work" story: even pre-`91fec5ddb`, the **first** drag of a fresh
process before any office write would have had an empty overlay (`isSettled` → `?? true`) and hit
the retire branch too. The operator never reported that, plausibly because the migration seed /
viewport save / first hydrate populated the sync early in every real session (ASSUMPTION A-2 — the
old build was not re-run to confirm).

---

## 4. The place path — same branch, two aggravators

A palette drop is NOT missing its optimistic write. `_addDroppedAgentInstance`
(`mission_control_page.dart:2370-2412`) → `_addAdditionalAgentPlacement` (`:2621-2713`) stages the
scene item through `_mutateMissionOfficeLayout` with an `'Added …'` message — the **non-debounced**
arm: override set, save awaited immediately. AC-2 (`520ac308c`) kept this deliberately ("the canvas
must move on the drop, not on the round trip" — its own commit message). It does not move on the
drop (MEASURED-LIVE: "a place also does not render optimistically — it appears when the reply
lands"). Three cooperating reasons:

1. **The pre-staging async gap.** `applyLayout` awaits `_viewStore.saveViewState(...)` and
   `_writeCache(...)` — both SharedPreferences platform-channel IO (READ
   `mission_office_layout_controller.dart:470-475`, `mission_office_view_state.dart:101-139`) —
   **before** `_schedule(sync)` stages the debounce timer (`:494`, timer at `:600`). Until the timer
   exists, `writesInFlight` is false. The `setState` that carried the override has already scheduled
   a frame; if that frame builds inside the gap, the retire branch returns stale and destroys the
   override — after which nothing repaints the item until the create reply lands, because the
   provider reload the save triggers reads `resolveLayout`, whose overlay is populated on flush
   *ack*, not on staging, so the reload does not contain the staged item either. Whether the frame
   wins the race is timing (ASSUMPTION A-3, the thing OR-0 measures); the operator's live result
   says it does.
2. **The `missingInstance` window has no repaint bell.** The minted item is instance-shaped
   (`personainst_<placementId>`), so `MissionOfficeProjectionScopePolicy.dropFor` resolves it via
   `resolver.byId` and **drops the node entirely** while the instance is unknown
   (`policy/mission_office_projection_scope_policy.dart:50-59`). The pending roster row that would
   make it resolvable is added by `_upsertPendingCreatedAgent` only *after* the layout save's awaits
   complete — and that method is a bare list mutation with **no `setState`** on this path (READ
   `:3141-3148`; call site `:2676`; contrast the template-create door, which wraps it at
   `:3435-3486`). So even a surviving override renders no node until some unrelated rebuild happens
   to occur after the pending row exists. (The policy's "no scope pointers stays visible" carve-out
   at `:64-66` exists precisely for this pending row — it works once the row is present and a frame
   happens; nothing guarantees the frame.)
3. Same retire branch as §2 thereafter: once the override is gone, the canvas shows the provider
   layout, which acquires the actor only when the `runtime.agent.create` reply / patch / read lands
   — ~1 s cold after D3 (MEASURED-LIVE).

For "when did the place break": before `91fec5ddb` the override survived (§3), so the drop painted
as soon as any rebuild followed the pending-row upsert — late by a frame or two, but inside the same
second. The same commit is the behavioural break for both gestures. Aggravators 1 and 2 predate it
and merely capped how good "working" ever was.

---

## 5. Stages

*No production code was changed for this document; every stage below is future work.*

### OR-0 — pin the mechanism live before touching it (launcher, instrumentation only)

A temporary receipt (debug log, stripped before merge) in the retire branch: log
`retire-during-override surface=… settled=… freshLoading=… gesture=…` with a counter. Reproduce one
drag and one palette drop against the dev build.

*Gate:* the log shows the branch firing per-frame during the drag and (or explicitly not) inside
the place's staging gap. If the place's frames do NOT hit the branch, aggravator 2 (§4) is promoted
to primary for the place and OR-2 reorders accordingly. This stage exists because §2 is
source-derived and §4's race is admitted timing (A-3); neither has been watched live.

### OR-1 — the paint path may not retire a gesture the page itself holds (launcher)

The minimal correct fix, and it is page-side: the retire condition treats "no writes in flight" as
"nothing outstanding", but the page's own 220 ms pending save IS an outstanding write the controller
cannot see. Extend the condition so the override survives while `_officeLayoutSaveDebounce != null
|| _officeLayoutPendingSave != null` (equivalently: introduce a page-level `hasPendingOfficeSave`
and require it false before retiring). This is the honest symmetric completion of `91fec5ddb`, not a
revert: acked-unreconciled state still retires (the deadlock stays fixed — the term expires
deterministically when the save hands off to the controller, at which point `writesInFlight` takes
over the same duty).

*Gate:* (i) a drag renders the node under the cursor, live; (ii) the `91fec5ddb` reattach test —
the one that pins the read lane healing across a child death — still green, byte-unchanged; (iii) a
widget-level regression test that drives pan-start/update through the page path and asserts the
layout handed to `MissionOfficeHost` carries the mid-gesture position — the pin this whole chain
never had. Mutation-test it: reverting OR-1's condition must turn it red.

### OR-2 — the place paints on the drop frame (launcher)

Two small moves, ordered by OR-0's finding: (i) stage before blocking — in `applyLayout`, populate
`sync.desired` and call `_schedule(sync)` (or set an explicit synchronous in-flight mark) *before*
the two view-store awaits, so `writesInFlight` is true from the first microtask of a save; (ii) wrap
the drop path's `_upsertPendingCreatedAgent` in `setState` (or have the caller schedule a rebuild)
so the `missingInstance` window ends with a repaint bell instead of by luck.

*Gate:* a palette drop shows the pending actor within one frame of the drop, cold, with the create
RPC still in flight; a refused create still retracts it (AC-2's refusal arm, unchanged).

### OR-3 — say it in the unified plan (docs, both repos' readers)

`UNIFIED_GESTURE_PREDICTION_PLAN_2026-08-16.md` needs three edits: §1's open questions (a)-(c)
resolved with this document's answers; UP-0 re-scoped — "give `interactionMode` a consumer" is NOT
the smallest fix for the operator-visible bug and would paper over the real one at the game layer
while the page keeps discarding state (UP-0 remains valid as the *future* local-echo layer of UP-2,
where the drag stops streaming mutations entirely); and the §6 adversarial line "UP-0 is not as
small as this plan says" confirmed — it was aimed at the wrong layer altogether.

*Gate:* the unified plan no longer contains a stage whose stated purpose is to fix a bug this
document locates elsewhere.

### OR-4 — decide the retire branch's future under UP-1/UP-2 (design, deferred until UP-1 exists)

OR-1 is a patch on a resolver that juggles three sources (override, controller state, provider).
UP-1's rule — "the canvas renders server state + pending intents" — deletes the juggling: an open
gesture is a pending intent, and a pending intent renders, so there is no retire decision to get
wrong. When UP-1 lands, OR-1's condition should be subsumed, not accreted onto.

*Gate:* one paragraph in the UP-1 design naming what replaces `_officeLayoutOverride` and this
branch — written before UP-1's code, so the patch cannot fossilize.

---

## 6. What this does NOT fix

- **The per-frame mutation stream and the `'Moved '` string gate.** Untouched; UP-2's job. OR-1
  makes the stream visible again, not cheap.
- **The ~1 s cold create round trip.** The place will *render* instantly (OR-2); the authoritative
  actor still costs what it costs. Scoped-invalidation (Plan B) territory.
- **The `laneAbsent` window on page open** and the page-open write storm — named unfixed in the
  unified plan; still unfixed here.
- **`interactionMode` stays dead** until UP-0/UP-2 give it its real consumer. Deleting it now would
  churn the exact lines that work touches.
- The adoption content-key divergence (unified plan U-7). Unrelated lane.

## 7. Deliberately deferred

- Making `isSettled` itself gesture-aware (controller learns about page-level pending saves). The
  layering is backwards — the page owns the gesture, the controller owns the wire — and OR-4 deletes
  the question.
- Rendering drags without any page rebuild (game-local echo). That is UP-0-as-part-of-UP-2, already
  planned; doing it here would fork the design.
- The `freshLoading` accidental-protection window (§2). It becomes irrelevant once OR-1 lands.

## 8. Adversarial pass — what I most expect to be wrong

1. **The place-path race attribution (§4.1) is the weakest claim.** I can prove the gap exists
   (READ) but not that the frame lands inside it — platform-channel replies often beat the next
   vsync, which would mean the operator's non-optimistic place is mostly aggravator 2 (the missing
   `setState`), not the retire branch. This is exactly why OR-0 fronts the plan and why OR-2
   contains both moves. If forced to bet on one mechanism for the place, I bet on the pair, not on
   either alone.
2. **The "worked by riding the deadlock" narrative (§3) is inference from `91fec5ddb`'s own commit
   message plus the old `settled` getter — not a re-run of the old build.** If some pre-08-15 path
   drained the overlay eagerly that I did not find, the drag's pre-08-15 visibility needs a
   different carrier (candidate: the provider's perpetual reload churn pre-O-H4, §2's second note).
   The regression *commit* would not change — both carriers die in the same 08-15/16 window — but
   the story would, and OR-3's edit should then say so.
3. **OR-1's condition could reintroduce a softer deadlock** if any path sets
   `_officeLayoutPendingSave` and never runs it (the closure is cleared before it runs, `:3723-3730`,
   and both exits cancel the timer — but an exception between arm and fire would pin the override
   again). The OR-1 gate's reattach test covers the old deadlock, not this new one; the regression
   test should also drive the exception path.
4. **Unverified live, all of it** — same confession as the unified plan. Source-read plus the
   coordinator-relayed operator measurements; no gesture in this session touched the running
   launcher (constraint: do not disturb the live runtime).

## 9. Verification log

| # | Fact | How established |
| --- | --- | --- |
| R-1 | `_reconcileAvatars` re-positions existing avatars every application; no tick dedupe | READ `mission_office_game.dart:273-317`, `avatar_component.dart:191-217` |
| R-2 | Host rebuilds per drag frame; host-local `_layoutOverride` is null during drags; model built fresh each build | READ `mission_office_host.dart:125,137-170`, `mission_office_mount.dart:110-114` |
| R-3 | Retire branch returns provider layout and clears the override when `isSettled` && fresh loaded | READ `mission_control_page.dart:2779-2814` |
| R-4 | Drag saves ride a 220 ms trailing debounce re-armed per pan frame; controller sees nothing mid-drag | READ `mission_control_page.dart:3689-3707,3500-3517` |
| R-5 | `writesInFlight` = staged timer ‖ flushing ‖ pending overlay entry; `isSettled` = its negation | READ `mission_office_layout_controller.dart:406-407,440-441` |
| R-6 | Pre-fix `isSettled` = `settled` = overlay AND removals empty AND no timer AND not flushing | RAN `git show 91fec5ddb^:…layout_controller.dart` (lines 257-258, 282-283) |
| R-7 | The retire branch + `isSettled` entered the page in `c8338ec42` (2026-07-17); page side untouched since | RAN `git log -S isSettled -- …mission_control_page.dart` |
| R-8 | `isSettled` semantics changed in `91fec5ddb` (2026-08-15 15:19) | RAN `git log -S writesInFlight`, `git show -s 91fec5ddb` |
| R-9 | Pre-fix, overlay entries drained only on a read; reads were suppressed while override held → `settled` pinned false | READ `91fec5ddb` commit message + current `:375-389` doc comment |
| R-10 | `applyLayout` awaits two SharedPreferences writes before staging the timer | READ `…layout_controller.dart:463-495`, `mission_office_view_state.dart:97-139` |
| R-11 | Instance-shaped placements with unresolvable instances are dropped (`missingInstance`), not rendered | READ `mission_office_projection_scope_policy.dart:40-59,121-126`, adapter `:125-139` |
| R-12 | Drop-path `_upsertPendingCreatedAgent` mutates state without `setState` | READ `mission_control_page.dart:3141-3148`, call site `:2676` |
| R-13 | The drop stages the scene item optimistically before the create RPC (AC-2 kept it) | READ `:2370-2412,2621-2713`; RAN `git show -s 520ac308c` |
| R-14 | `interactionMode` dead since `b2f815cd8` (2026-06-28); occurrence count constant since | RAN (coordinator, re-confirmed): `rg interactionMode`, `git log -S interactionMode` |
| R-15 | Drag invisible mid-gesture, pops on release; place appears only when the reply lands; drag previously worked | MEASURED-LIVE (operator, relayed with instruction to trust) |
