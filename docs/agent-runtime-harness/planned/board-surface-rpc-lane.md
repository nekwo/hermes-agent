# Planned — the board surface has no RPC lane and no fold

**Status:** not built. **Owner surface:**
[06 — Office and board](../06-office-and-board.md).
**Origin:** no dedicated plan. This row exists because the office finished the
journey the board has not started, and the asymmetry is invisible unless someone
writes it down.

## What is true today (verified 2026-08-22)

**Every board write is an argv capability.** Six submit sites, all in
`board/mission_board_write.dart`: `board.card.add` (`:133`), `board.card.move`
(`:158`), `board.card.edit` (`:181`), `board.card.archive` (`:199`),
`board.card.restore` (`:208`), `board.resolve_conflict` (`:219`). Each spawns a
`hermes` process per write.

**No `runtime.board.*` method exists.** Grep over `agent_runtime/serve_rpc.py`
returns nothing for `runtime.board`; the nine registered methods are the five
office verbs, `runtime.agent.create`, `runtime.persona.prewarm`, and the two
subscribe/unsubscribe verbs.

**Board writes are uncovered, so every board batch demotes.**
`agent_runtime/patch_coverage.py:33` lists "board/flow writes" among the events
that fall back to a full core, and the module is conservative by construction —
one uncovered event demotes the whole batch.

**The board DOES send a revision guard, and the office does not.**
`mission_board_write.dart:166,192` attach `expect_revision` from `card.revision`
(`board/mission_board_card_panel.dart:233`, `board/mission_board_drawer.dart:112`).
The office's argv arms deliberately omit it (`harness_capability_registry.dart:224`)
because its guard lives on the RPC lane. So the two surfaces guard writes on
opposite transports — worth knowing before either is changed.

**The board has a CLI home:** `hermes_cli/harness_parts/board.py`, beside
`office.py`.

## Why this may be correct as-is

Board writes are operator-paced and rare compared with office gestures — nobody
drags a card sixty times a second — so the per-write process spawn is not on a hot
path, and the demote is paid by a surface the operator is already looking at. The
office's RPC lane was justified by measured gesture latency, not by principle.
**Do not port the office's lane to the board on symmetry alone.**

## Gate, if this is picked up

1. **Measure before designing.** A board card move, timed end to end with a
   receipt in the shape of `[MissionDropTiming]` — spawn, store write, batch
   demote, repaint. If the felt latency is under a gesture's tolerance, close this
   row as "correct as-is" and record the number.
2. **If it is slow, the office's shape is the template and its lessons are
   binding**: RPC-first with argv only on `Unavailable`; a refusal is terminal;
   the ack echoes STORE truth (normalized, post-write revision) rather than the
   caller's input; the entity's fold is a subset merge gated on its own capability
   token so an undeclared client keeps today's full cores.
3. **A fold for board cards needs a paired producer inside the store lock**, and a
   covered event with no patch beside it is silent data loss — the failure the
   office's `emit_office_surface_refresh` exists to prevent
   (`agent_runtime/state_patches.py:1156`). Any board fold owes the same
   accounted-refresh escape hatch for writes it cannot express.
