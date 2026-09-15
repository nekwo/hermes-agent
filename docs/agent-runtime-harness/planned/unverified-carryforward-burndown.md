# Planned — burn down the Unverified carry-forward sections

**Status:** IN PROGRESS — first row discharged 2026-08-23 (doc 05's `chat_lane_scope_ms` 2,421: re-verified as the UNWARMED CREATE subphase, never a per-turn cost, and the doc-05 row now carries the annotation — prep-cost §3 H2).
**Opened:** 2026-08-22, from the operator's reading of doc 02's tail.
**Domain:** cross-cutting — every domain doc.

## The problem

Six domain docs carry `## Unverified carry-forward` sections: claims imported
from archived sources during the 2026-08-22 consolidation that no pass has
verified against code. The mechanism is deliberate (an unverified claim is
labelled rather than laundered into stated truth), but nothing owned the
follow-up — the sections would sit forever without this file.

## The worklist (census 2026-08-22)

| Doc | Claims | Note |
| --- | --- | --- |
| 01-system-architecture | 2 | launcher entity rendering; Neko prompt-layer ordering |
| 02-runtime-data-and-shapes | 2 | `state.reconciled` staleness SLO; supersede guard at the store chokepoint |
| 03-transport-and-wire | 2 | persona-chat event-lane 2026-08-09 properties; patch-lane byte saving |
| 05-chat-turn-lane | 4 | MCP admission timings; ~~`chat_lane_scope_ms` 2,421~~ **RE-VERIFIED 2026-08-23** (unwarmed-create subphase, annotated in doc 05); the 1,762 ms hermes share; tool-schema census |
| 06-office-and-board | 4 | `laneAbsent` window; page-open write storm; ~~`office.actor.restore` fully dead~~ **VERIFIED AND EXECUTED 2026-08-22** (launcher cut `e38bb108c` — promote to stated truth); argv-fallback incident-repro test |
| 08-performance-and-debt-ledger | 4 | RD-H2/H4/H5/H6; boot cache-hit measurement; launcher-side rows; the 2026-07-09 cProfile profile |

The launcher canon (`docs/mission_control/`) carries its own Unverified
sections under the same rules; its burn-down mirrors this file and lives
launcher-side when opened.

## The rule per claim

Verify against code at HEAD, then exactly one of:

1. **Promote** — anchors added, claim moves into the doc body as stated truth,
   row deleted here and in the doc's section.
2. **Refute** — claim deleted from the doc (optionally recorded as a
   correction in the owning section), row deleted here.
3. **Re-measure** — for timing claims: stale numbers are re-taken from the
   live log or marked with their vintage and kept only if the mechanism (not
   the number) is the claim.

A claim may not move states without its evidence written down. Empty sections
are deleted, and a doc with no section left drops off this table.

## The gate to close this

Every domain doc's `## Unverified carry-forward` section is gone — not
emptied by fiat, paid down claim by claim. This file is deleted in the same
commit as the last section.
