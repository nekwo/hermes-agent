# Planned — the boot resubscribe still buys a fourth full core (HY-L2)

**Status:** NOT IMPLEMENTED. Verified absent in the launcher tree 2026-08-22.
**Owning doc:** [`../04-boot-and-lifecycle.md`](../04-boot-and-lifecycle.md) Stage 7/8.
**Source:** `archive/2026-08-22-pre-consolidation/BOOT_HYDRATE_SECOND_READ_2026-08-17.md`,
stage HY-L2 — the ONE stage of that plan that did not ship. HY-0, HY-H1 and HY-H2 all
did (see the owning doc).

## The claim

A boot resubscribe requests a hydrate unconditionally, so the serve builds a core the
boot has already built. HY-L2 was written to delete that build. It was never written.

## Evidence that the mechanism is absent

`EterniaLauncher lib/features/mission_control/office/mission_office_subscribe_lane.dart:640-650`
— `_onResync` calls `_requestResubscribe('push:${params['reason']}')` **unconditionally**.
There is no `base_offset` or watermark comparison on that path. The comment at `:643-648`
states the current design intent is the opposite of the stage ("branched on nowhere").

No `HY-L2` marker exists anywhere in the launcher tree, and no boot-window
subscribe-count test exists.

## Partial mitigation from unrelated later work — which is not the fix

Two shipped mechanisms lower the cost of the redundant build without removing it, and
neither should be mistaken for HY-L2 having landed:

- **Same-offset demote reuse** (`agent_runtime/demote_core_reuse.py`) is gated to
  `reason == BATCH_REASON_DEMOTE` (`agent_runtime/stream.py:709`). A boot resubscribe's
  hydrate build does **not** take that arm.
- **The persisted core cache** (`agent_runtime/core_cache.py`) can serve the redundant
  rebuild cheaply — but only while the lane is armed and the fingerprint matches, and on
  this install it currently does neither reliably (see
  [`core-cache-fingerprint-closure.md`](core-cache-fingerprint-closure.md)).

So the ~3.4 s of redundant serve CPU per boot that HY-L2 targets is reduced on a good
boot and paid in full on a bad one.

## Gate

1. A boot that resubscribes emits **one** `snapshot_build_core role=led` line for the
   boot window, not two. Counted from `X:/Eternia/.hermes/profiles/base/logs/agent.log`
   across at least three boots.
2. The suppression is decided on a POSITION comparison — the subscriber's held offset
   against the frame's — never on a timer or a "we booted recently" heuristic. Same
   authority `demote_core_reuse` and `core_cache._stamp_still_stands` already use:
   equality of a store position, and an UNKNOWN position on either side refuses rather
   than comparing two nulls as "nothing moved".
3. A resubscribe that genuinely missed events still gets its hydrate. A test drives the
   missed-events case, not only the redundant one.

## Explicitly out of scope

Suppressing the resubscribe request itself. The request is how a reconnecting subscriber
proves what it holds; what must stop is the unconditional full-core BUILD behind it.
