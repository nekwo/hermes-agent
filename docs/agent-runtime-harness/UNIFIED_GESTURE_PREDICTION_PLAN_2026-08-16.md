# Unified gesture prediction — one intent, rendered now, reconciled once

**2026-08-16.** Operator ruling: *"so we need prediction for both — maybe we should set this up as a
general unified system"* and *"in this case on release trigger the rpc"*.

Evidence tags: **READ** (source, file:line) · **RAN** (command I executed) · **MEASURED-LIVE** (operator
gesture against the built runtime) · **RELAYED** (told to me, not on disk) · **ASSUMPTION**.

---

## 0. The distinction this plan is built on

Two different things are being called prediction. Building them as one feature without separating them
first would produce the wrong thing.

| | Meaning | Drag today | Create today |
| --- | --- | --- | --- |
| **Intent → RENDER** | the canvas shows it before any round trip | ~~absent~~ **present but ad hoc** (see below) | ~~absent~~ **present but ad hoc** |
| **Intent → RECONCILE** | resolve against the server by `revision`, retract on miss | present | partial |

The drag appeared to have the reconcile half and not the render half — which is exactly what the operator
saw: *"dragging doesn't work but on release it pops where it needs to go, I just can't see what I'm
dragging"* (MEASURED-LIVE).

> **CORRECTED 2026-08-16, and this changes the plan's premise rather than its design.** The render half
> was not absent. It existed — `_officeLayoutOverride`, set inside `setState` on every mutation — and it
> had been *working for six weeks*. What broke on 2026-08-15 is that the paint path started **discarding
> it on every frame**, for the reasons in §1's ANSWERED block. Both gestures render optimistically again
> as of the OR-1/OR-2 work.
>
> Everything below still holds, because the distinction was never really about presence. It is about
> whether the render half is a PROPERTY OF THE MODEL or a thing each gesture path remembers to poke. The
> override is the second kind, and the regression is the proof: one predicate changed in a different
> file, for a good reason, and the render half silently stopped existing for every gesture at once with
> no test to say so. **UP-1's rule — "the canvas renders server state + pending intents" — is what makes
> that unrepresentable**, which is a stronger argument for this plan than "the drag does not render"
> ever was. The create is currently *fast* rather than predicted: D3 removed the ~6.5 s full-core
build, so it lands in about a second cold and instantly warm (MEASURED-LIVE). Speed is not prediction —
the cold build still costs the first one, and prediction is what would hide that too.

**The prize is larger than either half.** Today the write path infers what the operator did by diffing
the whole in-memory layout against what it believes the server holds; anything present server-side and
absent locally is archived. That inference is R#40 — a degraded read produced a layout the write path
could not distinguish from intent to delete, and four actors were archived by one drag. An explicit
per-gesture intent record has **nothing to infer**, which is what finally lets that branch be deleted
rather than fenced.

---

## 1. Baseline — what actually exists (all READ unless noted)

**The mount streams a mutation per pointer move.** `mission_office_mount.dart:233-247`:
`_handlePanStart` sets `_dragNode` and calls `setInteractionMode(draggingNode)`; `_handlePanUpdate` calls
`widget.onMoveSceneItem!(dragging, _game.layoutPositionForDrag(...))` on every move. A drag therefore
emits hundreds of mutations.

**`interactionMode` is a DEAD FIELD.** True, and worth knowing — but **NOT established as the invisible
drag's root cause**, which an earlier revision of this section claimed. See the correction below.
`mission_office_game.dart:199-201`:

```dart
void setInteractionMode(MissionOfficeInteractionMode mode) {
  _renderModel = _renderModel.copyWith(interactionMode: mode);
}
```

`rg interactionMode lib/features/mission_control/office/mission_office_game.dart` returns **only that
line** (RAN). Nothing reads it. The game is told a node drag is in progress and does nothing with the
fact. The enum exists (`mission_office_render_model.dart:155`), the setter exists, the consumer never
did. `git log -S interactionMode` returns exactly one commit — `b2f815cd8`, the 2026-06-28 renderer
rewrite — so the field has been dead since it was born.

> **CORRECTION, same day.** This section first said the dead field *is* the root cause. That does not
> hold, and the operator was right to push back with *"the drag used to work fine, I used to be dragging
> around."* Re-reading the chain: the mount re-applies on `!identical(oldWidget.model, widget.model)`
> (`mission_office_mount.dart:110-114`), and the host builds `model` **fresh in every `build()`**
> (`mission_office_host.dart:161-165`), so the identity guard should pass and `applyModel` →
> `_reconcileAvatars` should run on each frame of a drag. The node should therefore re-render *without*
> anything reading `interactionMode`. It does not, and **why is unresolved.**
>
> The open questions, none of which I settled: (a) does the host actually rebuild during a drag, and does
> `_officeLayoutOverride` reach `widget.layout`, or does the host's own `_layoutOverride` (`:147`) shadow
> it; (b) does `_reconcileAvatars` (`:273`) *move* existing avatars or only add and remove them; (c)
> which commit broke it, since the operator reports it working previously. Scoped out separately.
>
> The lesson is the one §10.2 of the fold plan already records twice: I found a true fact — the dead
> field — and promoted it to a cause because it was the first thing that looked like an explanation. A
> dead field explains why there is no *special* drag rendering; it does not explain why the *ordinary*
> re-render fails.

> **ANSWERED, 2026-08-16**, by `OFFICE_OPTIMISTIC_RENDER_REGRESSION_PLAN_2026-08-16.md` and the launcher
> work that followed it. All three, and none of them is the render chain.
>
> **(a) The host DOES rebuild, and nothing shadows the page.** `setState` runs on every pan frame
> (`mission_control_page.dart:3690-3695`); the host's own `_layoutOverride` is set only by
> pet-picker / hide-persona / character-draft actions and is null during a drag, so `effectiveLayout`
> passes `widget.layout` straight through. **(b) `_reconcileAvatars` MOVES existing avatars** —
> `applyTransform` assigns `position = projection.worldToScreen(ground)` unconditionally, no dedupe, no
> lerp. Any model that arrives with new positions paints them. **(c) The regression commit is
> `91fec5ddb`** (2026-08-15 15:19), which redefined `MissionOfficeLayoutController.isSettled` from
> "fully reconciled" to "no writes in flight" — a correct fix for a read-suppression deadlock, which as
> a side effect armed a retire branch on the page's paint path that had been inert since `c8338ec42`.
>
> **The mechanism was upstream of everything this section describes.** The page's paint path discarded
> its own optimistic override on every frame of a drag, in favour of the stale provider layout: a drag's
> save rides a 220 ms trailing debounce that is cancelled and re-armed per pan frame, so nothing is ever
> staged mid-gesture, so the controller truthfully answered "nothing in flight" while the page itself
> held the write. Instrumented and counted before anything was changed — 4/4 retiring frames with a
> page-pending save outstanding (OR-0). Fixed in OR-1 by making the page's own pending save a term of
> the retire condition, and in OR-2 by staging the write lane's sync BEFORE `applyLayout`'s two
> SharedPreferences awaits, so no frame can find the save invisible.
>
> **So the dead field explains nothing here, and it was never in the path.** It was dead for the six-plus
> weeks the drag demonstrably worked. §10.2's lesson stands and is now doubly earned: the correction
> above was right to withdraw the claim, and the replacement cause was found by measuring the branch
> rather than by reading further down the same chain.

**The debounces exist only to clean up after the per-frame streaming.** `mission_control_page.dart:3689`
gates a 220 ms debounce on whether the operator-facing message string starts with `'Moved '`; the write
lane adds `kMissionOfficeSyncDebounce` = 600 ms. Both are trailing. The page comment already calls the
string gate *"FRAGILE AND LOAD-BEARING … control flow on DISPLAY TEXT"*. Note what this means: **the
debounces are not a feature, they are a repair for a problem the per-frame streaming creates.**

**The reconcile half is real and mutation-tested.** `mission_office_layout_controller.dart:127`
`missionOfficePredictedRevision`; the `MissionOfficeUpsertOk` arm compares predicted vs acked, retracts
the overlay and counts `rolledBack` on a miss; the corrective read is gated on `rolledBack > 0`. The
archive arms were only wired into that counter on 2026-08-16.

**Adoption currently trusts the client's own key.** `mission_control_page.dart:2519` passes
`contentKey: officeActorContentKey(payload)` computed from the *launcher's* layout — the create reply
carries only `actorKey` and `revision` — so the skip guard always fires. Recorded at merge; it is the
same infer-then-act shape as R#40, bounded by the next read re-seeding.

---

## 2. The design

One record per gesture:

```
MissionOfficeIntent {
  kind: move | create | remove
  actorKey            // or a minted placement id for a create
  payload             // the desired actor state
  predictedRevision   // what we expect the server to answer
  correlationId       // R#53, free here — every intent needs an id anyway
}
```

Two rules, and the whole design follows from them:

1. **The canvas renders `server state + pending intents`.** Not "server state, and also every gesture
   path remembers to poke an override". One place, so a new gesture is visible immediately *by
   construction* rather than by remembering.
2. **An intent resolves against its own call's reply.** Match on `revision` → drop it. Miss or refusal →
   retract it and ask for a corrective read. The write's own reply is the acknowledgement; push
   notifications remain for *other* clients' changes.

**A drag becomes: render locally while the pointer is down, emit ONE intent on release.** Per the
operator's ruling — on release, trigger the RPC. No per-frame mutations, therefore nothing to coalesce,
therefore **the 220 ms and 600 ms debounces stop applying to drags entirely**. They survive only for
changes that arrive without a gesture boundary (a rename, the migration seed, a viewport save), which is
what they were always for.

**What this retires:** the `'Moved '` string gate, the per-frame mutation stream, and — after Stage
UP-4 proves it — the absence-means-delete branch in `_flush`.

---

## 3. Stages

### UP-0 — game-local echo for a drag (launcher, **no longer the first stage**)

> **RE-SCOPED 2026-08-16.** This stage used to open *"the smallest change that fixes the operator-visible
> bug"*. It is not, and was never aimed at the right layer. The invisible drag was the page discarding
> its own optimistic override on every frame (§1's ANSWERED block); giving `interactionMode` a consumer
> would have painted a node at the cursor at the game layer while the page kept throwing the operator's
> gesture away one level up — a second source of truth papering over a first that was actively lying.
> The bug is fixed, in the page, by OR-1 and OR-2 of
> `OFFICE_OPTIMISTIC_RENDER_REGRESSION_PLAN_2026-08-16.md`. **Nothing here is a bug fix any more, and
> UP-0 must not be shipped as one.**

Give `interactionMode` a consumer: while `draggingNode`, the game moves the dragged node to the cursor
each frame. Dead field becomes live.

What that is genuinely FOR is UP-2. Once a drag stops streaming a mutation per pointer move and emits
one intent on release, something has to draw the node while the pointer is down and no mutation is being
emitted at all — and that something is this. **UP-0 is the local-echo half of UP-2, not a standalone
stage**, and it should land with UP-2 or immediately before it, judged against UP-2's gate rather than
against an operator bug report.

*Gate:* with the per-frame `onMoveSceneItem` stream removed (UP-2), an operator drag still shows the node
following the cursor. A camera pan still pans. Shipping it while the stream is still live buys nothing —
the page already paints every frame of a drag correctly.

### UP-1 — the intent ledger, alongside (launcher)

Introduce `MissionOfficeIntent` and a store of pending intents. Render `server + pending`. Write it
**beside** the existing overlay/`serverKeys` machinery, not instead of it — both live, both computed.

**What this replaces, named before the code exists** (the OR-4 gate of
`OFFICE_OPTIMISTIC_RENDER_REGRESSION_PLAN_2026-08-16.md`, so the patch below cannot fossilise). The
ledger subsumes `_officeLayoutOverride` and the retire branch in
`_resolveOfficeLayoutOrNull` **outright — it does not accrete onto them**. Today that resolver juggles
three sources (the page's override, the controller's flight state, the provider's projection) and has to
DECIDE which wins; the two-term retire condition OR-1 left behind is a patch on that decision, and every
term in it is a fact about *who currently holds a write*, which is a question the resolver only has to
ask because the override is invisible to the model. Under UP-1 an open gesture is a pending intent, a
pending intent renders by construction, and an intent leaves when its own call's reply resolves it — so
there is no retire decision to get wrong, no `isSettled` consumer on the paint path, and no page-owned
pending-save term. Concretely, when UP-1 lands: `_officeLayoutOverride`, `_hasPendingOfficeSave` and the
whole `if (override != null …)` branch are DELETED, and
`MissionOfficeLayoutController.isSettled` loses its only caller and goes with them. If any of those
survive UP-1, UP-1 is not done.

*Gate:* a test that drives every gesture and asserts the intent ledger and the existing overlay agree on
the resulting layout, for every case. Mutation-test the agreement assertion; an agreement test that
cannot disagree is the vacuity trap this repo has hit twice. **Plus:** the OR-4 fence
(`mission_office_optimistic_paint_test.dart`, group "every office door paints on the next frame") is
written to name no predicate, so it must stay green through this rewrite unchanged. If UP-1 needs it
edited, UP-1 changed operator-visible behaviour and owes an explanation.

### UP-2 — drags emit one intent on release (launcher)

`_handlePanUpdate` stops calling `onMoveSceneItem`; it updates the local drag intent only. `_handlePanEnd`
emits one intent and fires the RPC. Delete the `'Moved '` gate and the 220 ms debounce's drag role.

*Gate:* one write per drag however many frames it spanned — the property the debounce used to provide,
now provided by construction. Assert the write count, not the debounce's existence.

### UP-3 — creates predict too (launcher)

The drop renders a pending actor immediately and emits a create intent; `runtime.agent.create` resolves
it. Removes the cold-build second from the operator's perception without hiding it from the log — the
`snapshot_build reason=` line still records what it cost.

*Gate:* a refused create retracts the pending actor visibly, and says why.

### UP-4 — prove the inference is redundant, then delete it (launcher)

Run both reconcilers for a period with a receipt whenever they disagree. **A disagreement is a bug in the
intent ledger, not a reason to keep the diff.** When the log is clean on real gestures, delete the
absence-means-delete branch. The mass-archive tripwire demotes from load-bearing safety to a backstop.

*Gate:* zero disagreement receipts across a real session, and the R#40 scenario replayed against the
intent path.

### UP-5 — adoption stops trusting the client's key (both repos)

Have the create/upsert reply carry the server's content key (or its actor payload) and adopt **that**, so
suppression means "the server agrees" rather than "I assume it agrees". Closes the divergence recorded at
the Plan A merge.

---

## 4. What this does NOT fix

- **The ~4.3 s `laneAbsent` window on every page open.** Untouched. A gesture inside it still degrades.
- **The page-open write storm.** Untouched, and still un-investigated.
- **The cold `build_snapshot()` itself.** UP-3 hides it from the operator; scoped invalidation is what
  would remove it.
- **The half-created agent** — that is `runtime.agent.create`, already landed.
- Speed. Nothing here makes a round trip faster; it makes the operator stop waiting on one.

---

## 5. Deliberately deferred

- Offline/queued intents surviving a restart. Pending intents are in-memory and die with the process,
  which is honest: an unacknowledged gesture is not a promise.
- Multi-client conflict UX beyond the existing revision-miss retraction.
- Extending intents to non-office surfaces. The office is the pilot; generalising before it works is the
  over-building this program has already been warned about once.

---

## 6. Adversarial pass — what I expect to be wrong

**Most likely wrong: UP-0 is not as small as this plan says.** I verified `interactionMode` has no
consumer (RAN). I did **not** read how `applyRenderModel` seeds node positions, whether nodes are
recreated or mutated per model application, or whether a per-frame position write would fight the
reconciler. If nodes are rebuilt from the layout each application, "move the node to the cursor" may need
a real render-model change rather than a hook. **UP-0 must begin by reading that path**, and if it is not
small, that is a finding to report rather than to push through.

> **CONFIRMED, and worse than predicted — 2026-08-16.** The line was right for the wrong reason. Reading
> that path settled the render question in UP-0's favour: `_reconcileAvatars` re-positions existing
> avatars unconditionally and would not have fought a per-frame write, so the hook really was small.
> UP-0 was simply **aimed at the wrong layer**. The bug it claimed to fix lived a level up, in the page's
> paint-path resolver, and shipping UP-0 alone would have produced a canvas that looked fixed while the
> page went on discarding the operator's gesture — a defect that would then have been invisible until
> UP-2 removed the echo again. The adversarial instinct ("this stage is not what it says") was correct;
> the specific worry (implementation size) was not the thing to worry about. Re-scoped in §3.

**Second: UP-2 changes what a drag emits, and the office write path is where the data loss happened.**
Fewer, larger writes is the safer direction, but it is still a change to the code that archived four
actors. UP-1's agreement gate exists precisely so UP-2 is not the first time the two models are compared.

**Third: the agreement test in UP-1 is the most likely place to write something vacuous.** Two models
built from the same source will agree trivially unless the witness is independent. Probe a field the
intent path does not itself set — the lesson from D3's `M-L2` survivor, where a replace-vs-merge test
probed a field the create's own payload wrote and passed either way.

**Not verified anywhere in this plan:** any of it against a live runtime. Everything above about the
current behaviour is source-read plus two operator observations.

---

## 7. Verification log

| # | Fact | How established |
| --- | --- | --- |
| U-1 | `interactionMode` has exactly one reference — its own setter | READ `mission_office_game.dart:199-201`; RAN `rg interactionMode` |
| U-2 | `_handlePanUpdate` streams a mutation per pointer move | READ `mission_office_mount.dart:233-247` |
| U-3 | The mount's during-drag path is unchanged by the 2026-08-16 commit-on-release work | RAN `git diff 2eeb25c45..HEAD -- mission_office_mount.dart` — only `onMoveSceneItemEnd` and `_handlePanEnd` moved |
| U-4 | The 220 ms debounce is gated on a display-string prefix | READ `mission_control_page.dart:3689` |
| U-5 | `_officeLayoutOverride` updates inside `setState` on every mutation | READ `mission_control_page.dart:3690-3695` |
| U-6 | Prediction/reconciliation by revision already exists and is mutation-tested | READ `mission_office_layout_controller.dart:127,592-609` |
| U-7 | Adoption computes its content key from the launcher's own layout | READ `mission_control_page.dart:2519` |
| U-8 | Creates land in ~1 s cold, instantly warm, after D3 | MEASURED-LIVE, operator |
| U-9 | The drag is invisible mid-gesture and correct on release | MEASURED-LIVE, operator |
| U-10 | The absence-means-delete branch archived four actors | R#40 (see fold plan §10.4 register) |
| U-11 | Open questions (a)-(c) of §1 — host rebuild, avatar movement, regression commit | Answered in `OFFICE_OPTIMISTIC_RENDER_REGRESSION_PLAN_2026-08-16.md` §1 and §3; verdict `91fec5ddb` |
| U-12 | The retire branch fires on every frame of a drag, with the page holding the save | RAN, OR-0 instrumentation at widget level: 4/4 frames, `settled=true` and `pendingPageSave=true` on all four. NOT measured against the live runtime |
| U-13 | A frame inside `applyLayout`'s pre-staging gap destroys a palette drop permanently | RAN, OR-0 with the view store held open. Whether a production frame LANDS in that gap is still undecided — OR-2 closes the gap structurally instead of winning the race |
| U-14 | UP-0's render-layer premise is sound; its stage placement was not | READ `avatar_component.dart:191-217`, `mission_office_game.dart:273-317` |
