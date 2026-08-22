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

## Row 2 — UP-5: adoption still trusts the client's own content key

**Evidence, re-verified 2026-08-22.** `mission_control_page.dart:2502-2506` calls
`adoptServerWrite(..., contentKey: officeActorContentKey(payload))`, computed from
the **launcher's** payload. Grep for `content_key` in `agent_runtime/serve_rpc.py`
returns nothing, so no create or upsert ack carries the server's key. The skip
guard therefore always fires: suppression means "I assume the server agrees", not
"the server agrees" — the same infer-then-act shape as R#40, bounded only by the
next read re-seeding.

**Gate.** The create/upsert reply carries the server's content key (or its actor
payload); the launcher adopts THAT. Test: a server whose normalization produces a
different key than the client computed must NOT be suppressed — the client asks
for a corrective read. Mutation: adopt the client's key — the test reds.

**Sequencing.** Row 2 is a two-repo change and moves no contract version if the
field is additive on an existing ack. Row 1 is launcher-only and independent.
