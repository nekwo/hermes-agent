# Decision brief — does the flow graph replicate with its agent?

**Status: RULING OWED. Nothing here is built.** This document exists because the
queue row that asks for the ruling described the mechanism in terms that do not
resolve against the code, so the question the operator was being asked was the
wrong one. Below: what was re-measured (2026-09-04), the three corrections, and
the actual choice with its costs. Out of tier 1 by the 2026-08-31 ruling and
still out of it.

Owner doc: [`06-office-and-board.md`](../06-office-and-board.md) for the sync
contracts; [`02-runtime-data-and-shapes.md`](../02-runtime-data-and-shapes.md) for the flow graph's
place. Origin: the instance-replication survey (2026-08-31), whose plan lives in
the launcher repo (`docs/mission_control/planned/instance-replication.md` §1.4).

## 1. What the row said, and what is actually there

The row: *"a runtime flow graph is keyed on its owner instance's id, so a
replicated agent arrives on the receiving machine with no blueprint, and
`blueprints`/`blueprint_runs` are in `HARD_EXCLUDED_PATH_PARTS` deliberately."*

Three corrections, each read off the tree on 2026-09-04:

**C1 — `blueprints`/`blueprint_runs` are not the flow graph's store.** The
runtime flow graph is persisted under `<runtime_root>/flow_graphs/`
(`agent_runtime/flow_graph.py`, `FlowGraphStore`; the archive lane writes
`flow_graphs_stale/`). The two names in
`agent_runtime/realm_sync.py:HARD_EXCLUDED_PATH_PARTS` are leftovers of the
REMOVED mission lane — the same pair `agent_runtime/snapshot.py` records as
zero-reader frame sections. Reading them as the flow graph's deliberate
exclusion attributes a decision to a line that was never about this store.

**C2 — the flow graph is not excluded. It was never considered.** `flow_graph`
appears in none of the three sync contracts: not
`agent_runtime/realm_sync.py`, not `agent_runtime/persona_config_sync.py`, not
`agent_runtime/persona_instance_sync.py`. It is neither published nor blocked
nor accounted; it is simply outside every door. That is a materially different
starting position from "deliberately excluded", and it is why nothing on either
machine says the canvas did not travel.

**C3 — the agent's STEERING does replicate; only the CANVAS does not.** The
flow graph's semantic content is the owner's outbound edges, and ingest applies
them by writing the OWNER into each referenced child's `steered_by`
(`flow_graph.py`, per-instance blueprint ownership, 2026-07-18). `steered_by`
**is** in `PERSONA_INSTANCE_ALLOWED_KEYS`, and so is `id` — so on the receiving
machine the relations the drawing asserted are present, and a graph document
keyed `runtime:<owner id>` would address the correct owner verbatim, because the
instance id travels unchanged. What is missing on machine 2 is the authored
document: node ids, x/y layout, node→agent bindings, and the non-owner edges the
operator drew as local context (which ingest reports and never applies anyway).

So the failure is **"the second machine shows a blank canvas for a working
agent"**, not "the replica arrives broken". That is a smaller and a different
problem than the row states, and it changes which options are reasonable.

## 2. What an operator sees today

A pulled desk mints its instance (`realm_sync.apply_persona_instance_pull`),
the agent works, its steering relations are correct in the HUD — and the Agent
Flow chart for it is empty, with no line anywhere saying why. The silence is
the part that is not defensible under any of the options below: every other
sync lane in this codebase accounts for what it dropped (`dropped_keys`,
`config_shadowed_keys`, `incomplete`, `unexpected_key`).

## 3. The options

**A — leave it local, and say so.** The canvas is one operator's local layout on
one machine, like a window position. Cost: one accounting surface (a
`flow_graph_local_only` note on the pull, or a badge on an empty chart whose
owner instance is realm-scoped) so the blank canvas is explained rather than
mysterious. Nothing travels. This is the cheapest option and the only one that
needs no new merge semantics.

**B — travel the document, adopt-or-hold.** Publish a projection of
`flow_graphs/<graph_id>.json` alongside the instance projection, on the same
allowlist-and-account discipline the other three projections use, and merge on
pull with the existing `PullAction` vocabulary (`sync_merge.classify_three_way_pull`
— adopt / converge / hold), never last-write-wins. Costs, all real:

- **Two operators, two canvases, one graph id.** The document is a drawing;
  three-way merge on node positions has no natural resolution, so realistically
  it is adopt-or-hold at whole-document granularity with a `hold` surfaced to
  both sides.
- **Dangling node→agent references.** A canvas may name instances that did not
  travel (not in the realm, or not yet pulled). Ingest already refuses to create
  instances, so the pull must either drop those nodes with accounting or hold
  the whole document.
- **Reap interaction.** Owner-liveness reaping archives a graph whose owner is
  gone. A pulled graph whose owner has not been minted yet would be reaped on
  arrival unless the two lanes are ordered.

**C — re-derive the canvas from `steered_by` on arrival.** Since the steering
travels, machine 2 could auto-lay-out a graph from the relations it already has.
No new document crosses the wire and no merge problem exists. Cost: the derived
canvas is not the operator's drawing (layout differs, non-owner context edges
are gone), and a subsequent edit on machine 2 then diverges from machine 1 with
no shared identity — which walks straight back into option B's merge question
one release later.

## 4. What the ruling has to answer

1. Is the authored canvas realm-wide (like a workspace) or machine-local (like a
   window layout)? Everything else follows from that one answer.
2. If it travels: adopt-or-hold at whole-document granularity, or per-node?
3. Either way: **the accounting is not optional.** Option A without a surface
   that says "this chart is local to this machine" leaves the same silence that
   made this row necessary.

## 5. Re-measurement record (2026-09-04)

| check | result |
|---|---|
| `agent_runtime/flow_graph.py` store dir | `<runtime_root>/flow_graphs/`, archive `flow_graphs_stale/` |
| `grep flow_graph` in `realm_sync.py`, `persona_config_sync.py`, `persona_instance_sync.py` | no match in any of the three |
| `HARD_EXCLUDED_PATH_PARTS` | contains `blueprints`, `blueprint_runs` (retired mission lane), not `flow_graphs` |
| `PERSONA_INSTANCE_ALLOWED_KEYS` | contains `id` and `steered_by` |
| graph identity | `graph_id` is `runtime:<owner instance id>`, and that id travels unchanged |
