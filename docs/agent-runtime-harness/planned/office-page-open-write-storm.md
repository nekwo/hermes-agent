# Planned — the page-open write storm and the cold-open degrade window

**Status:** not built, and never owned. **Owner surface:**
[06 — Office and board](../06-office-and-board.md).
**Origin:** named unfixed by three separate 2026-08-16 plans —
`OFFICE_WRITE_VERBS_RPC_PLAN` §4 ("item 9", "item 10", D-W4),
`UNIFIED_GESTURE_PREDICTION_PLAN` §4, and
`OFFICE_OPTIMISTIC_RENDER_REGRESSION_PLAN` §6 — all archived.

Two adjacent behaviours on cold Mission Office open. Neither has ever had an
owner, and both were repeatedly scoped OUT of plans that fixed things around
them, which is why they are recorded here rather than lost.

## Row 1 — the write storm on page open ("item 9")

**The claim, as inherited.** Opening the Mission Office re-upserts the desk and
rewrites the folder surface off a stale cache, before any operator gesture. Every
plan in the 2026-08-16 family names it; none measured it, and the surface fold
(which made its demote cheap) explicitly did **not** stop it — the write-verbs
plan told implementers to say so in the commit message so nobody read that stage
as the boot fix.

**Why it still matters after the fold landed.** The fold makes a page-open batch
promote instead of demoting; it does not make the writes stop. Writes on open are
also the reason the surface write was ever hot enough to justify the fold, so
fixing this makes the fold's win smaller and is still strictly better.

**Gate.** Before any fix: one instrumented cold open with the write-lane receipts
already in the log (`[MissionOfficeWrite] … write lane:`) plus the correlation
ids that `office/mission_office_correlation.dart` already mints, answering (a)
how many writes a cold open emits, (b) which of them differ from server truth,
(c) which are the stale-cache seed. If the count is small, close this row as a
false alarm and say so — a cause with no consequence is a false alarm, which is
the by-design/anomalous lesson the drops chip already taught.

## Row 2 — the `laneAbsent` degrade window on cold open ("item 10")

**The claim, as inherited.** For roughly 4.3 s after page open the serve manifest
has not arrived, `missionOfficeRpcRefusal` answers `laneAbsent`, and every office
verb degrades to the argv lane inside that window — by design, but it is also the
window in which the fallbacks Row 1 of
[office-write-lane-collapse](office-write-lane-collapse.md) wants deleted are
genuinely reachable.

**What is verified today, and what is not.** The degrade path is live in code:
`missionOfficeRpcRefusal` returns `laneAbsent` when the manifest is absent
(`office/mission_office_rpc.dart:153`), the reason is a real enum member (`:640`),
and it is classified as a nag-worthy degrade (`:720`) with the file's own comment
conceding "`laneAbsent` would nag on every cold boot — true, and accepted"
(`:703`). **The ~4.3 s figure is NOT verified**: the current diag log's
ISO-timestamped era contains zero `laneAbsent` lines, so the window is neither
confirmed nor refuted at HEAD.

**Gate.** Re-measure first. One cold Mission Office open with the current build,
reading `[MissionOfficeRpc] … (laneAbsent)` and
`[MissionOfficeSubscribe] … unavailable (start): laneAbsent` against the serve
child's `spawn_to_ready`. Only then decide whether the window needs shrinking, or
whether the boot-window work already closed it and this row can be retired.

**Sequencing constraint.** Row 2's measurement is a **precondition** for deleting
the argv arms: a fallback that is provably reachable for seconds on every cold
open is not dead code, whatever the steady-state receipts say.
