# Planned — collapsing the office write lane to one transport

**Status:** not built. **Owner surface:** [06 — Office and board](../06-office-and-board.md).
**Origin:** `OFFICE_WRITE_VERBS_RPC_PLAN_2026-08-16.md` §5 (deferrals D-W1..D-W5)
and `SINGLE_TRANSPORT_COLLAPSE_PLAN_2026-08-16.md` (TC-3/TC-4), both archived.

All four office verbs are on the RPC lane today. What remains is deleting the
fallbacks they were shipped beside, and the one guard that was deliberately held
back for behaviour parity.

**Not the same file as [single-transport-collapse](single-transport-collapse.md).**
That one owns the READ side — the argv `harness stream` child versus the serve hub
lane. This one owns the WRITE side — the `office.*` capability lowerings under the
four RPC verbs. They share TC-3/TC-4's *criterion* (a fallback proves it is dead
before it is deleted) and nothing else; neither deletion gates the other.

## Row 1 — delete the argv arms (D-W3 / TC-3, TC-4)

**Evidence.** The argv lowerings are live but reachable only on the `Unavailable`
arm: `office.actor.upsert`, `office.actor.remove`, `office.surface.update`,
`office.resolve_conflict` (`data/mission_control_bridge.dart`, capability rows in
`data/harness_capability_registry.dart`). The deletion criterion is the receipt
stream, and it is currently unanimous — every 2026-08-22 flush in the diag log
reads `[MissionOfficeWrite] … write lane: 1 rpc, 0 cli`, and
`MissionOfficeWriteLaneStatus.usedFallbackLane`
(`office/mission_office_layout_controller.dart:410`) is the state the fallback
pill renders from.

**Gate (lifted verbatim from the fold-fence plan §6, so nobody re-derives it).**
An agreed observation window in which `lane=cli` is 0 with every degrade
attributed to `serve_absent` — never to gaps, parks, or `laneAbsent` — across
sessions that include create/delete bursts, workspace switches, and a serve-child
respawn. Then delete, in one commit per verb, with the capability row and the argv
lowering going together.

**Do not delete first and watch.** That is the delete-and-see this program has
already been burned by.

## Row 2 — the guarded remove (D-W1) — RE-SHAPED 2026-08-27 by the placement verb's D7/S8

**Evidence.** `sync.serverRevisions[key]` already holds an honest token whenever a
read or an ack supplied one, and `runtime.office.remove` accepts `expect_revision`
(`_runtime_office_remove`'s docstring). The launcher's remove sends none, so
**archives are unguarded today**, on both lanes.

**Why it was held back.** It changes operator behaviour — a concurrent edit would
refuse a delete — and the first cut of the RPC remove had to be behaviour-parity
with the argv call it replaced.

**What D7 changed, and it changes this row's WORTH rather than its shape.** The
gesture this row was written about — an operator deleting a placed agent — no
longer reaches `runtime.office.remove` at all. Since S8 the launcher SPLITS the
vacated set at its mutation chokepoint (`missionOfficeRemovalPlanFor`):
placement-shaped keys go to `runtime.agent.retire`, and only the remainder — a
class-keyed actor, an authored desk owning its own row — reaches the remove verb.
So the population this guard would cover has shrunk to exactly the deletions no
launcher gesture mints in bulk, and the frequent case took its own lane, which
has NO revision guard either and is a different question (a retire is keyed on
the instance and archives whatever is bound to it; there is no per-actor token to
present). Two consequences, both stated so neither is discovered later:

- the "REVISION-MISS-on-archive" count the gate below asks for will now be
  measured over a much smaller and much rarer population, so a live window that
  used to be days long is now weeks long — plan the evidence, do not shorten it;
- if the guard is wanted for DELETION generally rather than for this verb, the
  retire lane needs its own answer and this row is not it.

**Gate.** Unchanged in kind: decide with a receipt count of
REVISION-MISS-on-archive from a live window, not from source reading. If misses
are ~0, sending the guard is a one-argument change; if they are common, the
refusal UX has to be designed first. Re-scope the window to the post-S8
population before counting.

## Row 3 — `runtime.office.restore` (D-W5) and the dead capability under it

**Evidence.** `office.actor.restore` has a registry row
(`data/harness_capability_registry.dart:250`) and an argv lowering
(`data/mission_control_bridge.dart:4475`), asserted only by
`gateway_manifest_test.dart:143` and `harness_capability_argv_test.dart:613`.
Grep finds no submit site; the 2026-08-17 lane map found none either. Resurrection
in the UI goes through upsert plus a ledger removal, not through this verb.

**Gate.** Either (a) delete the capability row, the lowering and their two test
assertions — with a repo-wide grep as the evidence, never a file-scoped one — or
(b) if operator-CLI recovery is a kept product feature, say so in writing and
leave the CLI verb while deleting the launcher-side registry row and lowering,
which no caller can reach.

## Row 4 — DISCHARGED (verified 2026-08-22, launcher delete audit)

The office `--expect-revision` argv arms this row scheduled were already
deleted by the EG-0.3 reap (reap 1). The two lowerings the old evidence cited
(`mission_control_bridge.dart:4374`, `:4411`) are the two BOARD lowerings the
reap deliberately kept (`board/mission_board_write.dart:166,192` is the board's
own guard), and the launcher's
`mission_control_office_argv_reap_contract_test.dart` pins the surviving
`--expect-revision` population at exactly those two. Nothing left to do; do
not re-plan this deletion.

## Row 5 — coverage for `.restored` and `.conflict_resolved` (D-W2)

Both stay uncovered on purpose (rare, and both move state past the upsert
chokepoint), so resolves and restores ride a full core. Re-open only if live
receipts show either event demoting batches at a rate that matters.
