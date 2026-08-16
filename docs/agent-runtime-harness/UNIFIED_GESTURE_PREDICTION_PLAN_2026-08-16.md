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
| **Intent → RENDER** | the canvas shows it before any round trip | **absent** | absent |
| **Intent → RECONCILE** | resolve against the server by `revision`, retract on miss | present | partial |

The drag has the reconcile half and not the render half — which is exactly what the operator sees:
*"dragging doesn't work but on release it pops where it needs to go, I just can't see what I'm dragging"*
(MEASURED-LIVE). The create is currently *fast* rather than predicted: D3 removed the ~6.5 s full-core
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

**`interactionMode` is a DEAD FIELD — this is the invisible drag's root cause.**
`mission_office_game.dart:199-201`:

```dart
void setInteractionMode(MissionOfficeInteractionMode mode) {
  _renderModel = _renderModel.copyWith(interactionMode: mode);
}
```

`rg interactionMode lib/features/mission_control/office/mission_office_game.dart` returns **only that
line** (RAN). Nothing reads it. The game is told a node drag is in progress and does nothing with the
fact. The enum exists (`mission_office_render_model.dart:155`), the setter exists, the consumer never
did. The node's on-screen position comes solely from the layout being re-applied — so the drag is
invisible until something forces a re-seed, which the release does.

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

### UP-0 — make the render half exist (launcher, standalone, shippable alone)

The smallest change that fixes the operator-visible bug, and it does not depend on the rest of this plan.
Give `interactionMode` a consumer: while `draggingNode`, the game moves the dragged node to the cursor
each frame. Dead field becomes live.

Ship this first even if nothing else here is built. **Do not delete the per-frame `onMoveSceneItem`
stream in this stage** — UP-0 is additive so it can land alone and be judged alone.

*Gate:* an operator drag shows the node following the cursor. A camera pan still pans.

### UP-1 — the intent ledger, alongside (launcher)

Introduce `MissionOfficeIntent` and a store of pending intents. Render `server + pending`. Write it
**beside** the existing overlay/`serverKeys` machinery, not instead of it — both live, both computed.

*Gate:* a test that drives every gesture and asserts the intent ledger and the existing overlay agree on
the resulting layout, for every case. Mutation-test the agreement assertion; an agreement test that
cannot disagree is the vacuity trap this repo has hit twice.

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
