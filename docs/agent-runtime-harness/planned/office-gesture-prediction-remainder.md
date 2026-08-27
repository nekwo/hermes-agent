# Planned — office gesture prediction: the last two stages

**Status:** not built. **Owner surface:** [06 — Office and board](../06-office-and-board.md).
**Origin:** `UNIFIED_GESTURE_PREDICTION_PLAN_2026-08-16.md` (archived).

Four of that plan's six stages are discharged and must not be re-litigated:

- **UP-0 (game-local echo) — SHIPPED** as the local-echo half of UP-2, exactly as
  the re-scope demanded. `MissionOfficeGame.echoDragTo` draws the dragged node at
  the cursor with no write (`office/mission_office_game.dart:284-305`), guarded on
  `interactionMode == draggingNode`, and `setInteractionMode` drops the echo on any
  other mode so a camera pan cannot strand a node (`:267-271`).
- **UP-1 (intent ledger) — SHIPPED**, with its completeness rule discharged:
  `_officeLayoutOverride`, `_hasPendingOfficeSave`, the page's override branch and
  the paint-path `isSettled` caller are gone, each tombstoned
  (`mission_control_tombstone_registry_test.dart:2980,2995`).
- **UP-2 (one intent per drag) — SHIPPED.** `_handlePanUpdate` no longer calls
  `onMoveSceneItem`; it echoes locally and records `_dragCommitPosition`
  (`office/mission_office_mount.dart:362-373`). `_handlePanEnd` emits the move and
  the commit together or not at all (`:383-391`). The `'Moved '` display-string
  gate — control flow on operator-facing text — is deleted
  (`mission_control_page.dart:3713`).
- **UP-4** — struck at source: the absence-means-delete branch was deleted
  outright at launcher `7623f99cf`, so there is nothing to prove redundant.

## Row 1 — the create's optimistic half is shipped; its *refusal* half is unpinned

**Evidence.** The drop stages its scene item and its identity row before the RPC —
`layout_mutate_ms` is 13–20 ms on every live receipt — and the pending chip layer
now names the wait (`office/mission_drop_pending_chip_layer.dart`). What UP-3's
gate actually asked for is the other direction: *a refused create retracts the
pending actor visibly, and says why.* The drop path has a refusal arm, but no
current test drives a refusal through the intent ledger AND the chip layer
together, so "the chip leaves and the node retracts on a refusal" is asserted
nowhere as one fact.

**Gate.** One widget-level test: drive a palette drop against a writer whose
`runtime.agent.create` returns Refused; assert (a) the pending chip for that
correlation is gone from the tree, (b) the intent is out of the ledger, (c) the
refusal reason is on screen. Mutation: swallow the refusal — the "New chat does
nothing" regression class — must turn it red.

## Row 2 — UP-5: DISCHARGED 2026-08-27 by the placement verb's S7

The ack now carries the actor as stored (`office_models.office_actor_wire_row`, plan D2/D11) and
the launcher adopts THAT: `_adoptServerPlacement` computes the suppression key from the ack's
payload through `officeActorPayloadFromRpcItems`, never from the payload it staged, and re-stages
the scene onto the server's row when the two differ (launcher `f7160c3c7`). See
[agent-placement-verb.md](agent-placement-verb.md) §A D2 and the launcher's
`docs/mission_control/04-office-scene.md` "Drops end-to-end". One caveat this file should not
lose: the adoption is keyed on the ack's CONTENT and the launcher additionally refuses to adopt a
replay stamped `actor_fresh: false`. Row 1 (the refused-create retraction) is untouched and still
open.

**Sequencing.** Row 1 is launcher-only and independent.
