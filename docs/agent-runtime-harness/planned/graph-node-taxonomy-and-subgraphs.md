# Planned — graph node taxonomy and sub-graph expansion

**Owner domain:** system architecture ([01-system-architecture.md](../01-system-architecture.md))
**Status:** not implemented. Deferred by design, not blocked.
**Raised:** 2026-06-22 (Node Charter, archived doc 01 Part C) · re-verified 2026-08-22.

## What is true today

The runtime graph has **no node taxonomy at all**. A flow-graph document is
parsed into `FlowGraphDoc` with exactly two facts per node — an id and an
optional bound persona instance — and a flat edge list
(`agent_runtime/flow_graph.py:82-93`, `parse_flow_graph_doc` at `:95`). A grep
for `node_type` across `agent_runtime/` returns zero hits (verified
2026-08-22). Unknown node keys (x/y layout, anything else the launcher authors)
are ignored on ingest and preserved verbatim in the stored doc — layout is the
operator's, not the runtime's (`flow_graph.py:41-45`).

There is likewise no sub-graph mechanism. Graph identity **is** the owner
instance's id (`graph_id = runtime:<owner>`), so one instance owns exactly one
map (`flow_graph.py:18-31`). Nothing in the runtime opens a node as a container
of further nodes.

## What was designed and not built

Archived doc [01 — Architecture](../archive/2026-08-22-pre-consolidation/01-architecture.md)
Part C specified an agent node with a persona selector, an output→sub-agent
socket, and an objective, plus a **Node Charter**: a new node type ships only
after answering (1) what it represents that an agent node cannot, (2) its
sockets, (3) its harness projection, (4) its lifecycle and permission
semantics. Explicitly deferred candidates: artifact/output-type nodes,
conditional/branch nodes, join (fan-in) nodes, and the expand-to-sub-graph
node.

Note that one deferred candidate has since arrived by a different route:
**fan-in already exists at the data layer** as the multi-parent `steered_by`
set (`agent_runtime/models.py:337`), asserted declaratively by
`PersonaInstanceStore.set_parents` (`persona_assignments.py:517`). It did not
need a join node type. Any Charter answer for a join node must start from that.

## Gate to open this

1. A concrete operator ask that an agent node provably cannot express — the
   Charter's question 1, answered with a real mission, not a category.
2. A projection answer: what the new node becomes in `FlowGraphDoc` and in
   `PersonaInstance` state, given that today the ONLY thing ingest writes is
   `steered_by` (`flow_graph.py:9-17` — "ingest never creates, starts, or
   deletes an instance").
3. Lifecycle + permission semantics, since restructure verbs (create/kill) are
   the gated class and steer verbs are not.

Until all three are answered, the bound-persona node is the only node.
