# Stage 77 - Consolidated Agent Graph: one node = one agent

> Collapse the two views the operator sees today — the **persona pipeline**
> (`Goal → Neko → Dev → QA → Proof`, the WHO) and the **blueprint stage graph**
> (`Backend Contract → … → Done`, the WHAT) — into **one graph** where a node is a
> single agent. Retire the standalone persona pipeline. Builds on the Stage 76
> entity model (a node = `{agent · objective · output}`); this stage is the UI/UX
> realization of it.
>
> Repos: Harness `X:/Eternia/hermes-agent`; Launcher
> `X:/Unreal Engine/Engine/Launcher/EterniaLauncher` (Mission Control).

## Why this stage exists

The persona pipeline and the blueprint graph are **not two systems** — they are two
projections of one graph, joined by `stage → owner_slot → bound persona`. Drawing
them separately is the redundancy the operator hit ("two graphs"). The fix is to put
the agent **on** the node: one node = one agent, carrying both its identity (who)
and its work (what).

## Controlling principle — ONE node type for now

**The agent node is the only node type.** We grow the node taxonomy *deliberately*;
every new node type must clear the **Node Charter** (below) before it exists. Start
with a single agent node and earn complexity.

## The agent node

A node **is one agent**. Its details/inspector panel has:

1. **Persona** *(every node)* — *which agent* this node is. A concrete runtime agent
   (a placed persona instance), not an abstract slot. Swap by re-selecting here.
2. **Output → sub-agent** *(every node)* — *who it steers*. The output socket wires
   to a downstream agent node; that wire is the **steering edge** (Stage 76D.3:
   steer is ungated; create/kill is permission-gated). It is also the **dataflow**
   wire — this node's output becomes the sub-agent's input.
3. **Objective** — the **owner node** (the container) shows the **Goal**; every other
   (sub-agent) node shows its read-only **inherited input** (the upstream output). A
   sub-agent node has no goal field (see "Goal ownership & flow").

On the node face: the persona (name + accent), a live **status/proof badge**
(running / blocked / ready), its objective label, and — on the owner node — an
explicit **owner marker** ("placed on level · owns goal"). The agent is set by a
**dropdown** (persona selector). Single-click opens the agent's chat. **Double-click
→ open this node's own sub-graph (a node is itself a container) is deferred.**

## Goal ownership & flow (the container owns the goal, not a node)

**The goal is owned by the agent *container* — the placed Level agent-instance — not
by any node in the graph.** This is the Stage 76 entity model verbatim: the
**placement owns its chat, and the chat owns the goal**. The graph is just the plan
that container runs; its nodes are the sub-agents it coordinates. One goal per
container/graph.

- **The container is rendered as its graph's owner node**, explicitly badged
  ("placed on level · owns goal"). That owner node *is* the container (same entity),
  so it carries the **Goal** (entered, or shaped from a goal template, 76D.4). It is
  the one distinguished node; every *other* node is a sub-agent (same node type, no
  goal). The owner is explicit and meaningful — not a hidden special-case.
- **The goal flows down into the graph.** The container's goal is the input to its
  first node(s); each edge then carries the upstream node's **output → the
  downstream node's input** (76D.4 dataflow). Sub-agent nodes **never own a goal** —
  their objective *is* the flowing input. A QA node verifies the dev node's output;
  no node re-states the mission.
- **Recursion is the structure, not a feature.** A node *is itself a container*, so
  opening it shows its own sub-graph with *its* (flowed-in) goal. The level is a
  graph of containers; expand-to-sub-graph (deferred in the UI) is just this same
  mechanism one layer down — no special "expand node" type.

Simple start: **one placed container + a goal** = it does the goal itself (empty/
trivial sub-graph). Sub-agents are nodes added to its graph when it fans out; each
new node just receives the flow. Recursion is deferred in the UI, free in the model.

### Behavior

- A node runs its **agent** on its **goal template** (the first message = the
  rendered objective, 76D.4); its live trace/HUD is the node's status.
- Its **output steers the wired sub-agent** — the living-graph steering wire
  (76D.3), continuous, not a one-shot handoff.
- **One node alone** = a single agent working a goal, no sub-agent.
- **Wire the output to a second node** = the first agent steers that sub-agent.
- **Neko** is an agent node like any other, but as the **lead/coordinator** it owns
  the Goal and usually sits at the root, steering the rest. It is not a separate
  pipeline entry.

## Navigation — clicking an agent opens the graph it's wired into

Clicking a placed agent on the level opens **the graph it is a member of** — the
graph it's wired into — with that agent highlighted. If Dev is a sub-agent wired into
Neko's graph, clicking Dev (on the level) shows **Neko's graph**, because that's where
Dev operates. You always land in the agent's operating context, never a detached view.

- **Membership = the wiring.** An agent's home graph is the one whose plan contains
  its node. The pointer already exists — 76D.2's `goal_id` / `spawned_by` on the
  instance; clicking resolves that to the owning graph and selects the node.
- **Standalone agent** (not wired into any parent) → clicking opens **its own** graph
  (it is the owner node, 77.1).
- **Two gestures, two directions:**
  - **Single-click an agent on the level** = go *up/across* to the graph you're a
    member of (your operating context — Neko's graph).
  - **Single-click a node inside a graph** = open that agent's **chat**.
  - **Double-click a node** *(deferred)* = drill *down* into that agent's **own**
    sub-graph (its internals).
- Multiple memberships (an agent wired into more than one graph) need
  disambiguation — deferred; one home graph for now.

## Node Charter — the rule for adding any node type

Because we "think out every node we add," a new node type ships **only** after it
answers all four:

1. **What does it represent that an agent node cannot?**
2. **Sockets** — what flows in, what flows out?
3. **Harness projection** — what does it become in the blueprint / `mission_plan`?
4. **Lifecycle + permission** — its steer / create / kill semantics (76D.3).

Until a candidate clears this, the **agent node is the only node**. Explicitly
deferred candidates (each must earn its charter later): artifact/output-type nodes,
conditional/branch nodes, join (fan-in) nodes, and the **expand-to-sub-graph** node.

## Mapping to the harness (one source of truth)

The UI agent node is a **projection of a blueprint stage** — no parallel model:

| Agent node (UI) | Blueprint stage (harness) |
|---|---|
| Persona selector | the stage's `owner_slot` binding → a persona |
| Inherited input | each stage's `objective`: the container goal → first stage, then each stage's input = the upstream stage's output (76D.4 dataflow) |
| Output → sub-agent | the stage's outgoing **edge** to the next stage (carries output→input) |

The **container** (the placed Level instance) maps to the Stage 76 **goal-owner
chat** — it owns the goal and the `mission_plan` (the graph); a node maps to a stage
*inside* that plan.
| Live status badge | the bound persona instance + run state (76D.2) |

So the harness blueprint (stages / edges / `RUN_SLOT`) is unchanged underneath; the
node renders the stage **with its bound agent on it**. Template state shows the
slot/role + selected persona; running state shows the attributed level instance +
status.

## What this retires

- The **standalone persona pipeline** — the agents are the nodes; there is no
  separate `Goal → Neko → Dev → QA → Proof` strip (the strip becomes the collapsed
  graph, Stage 76E).
- The **agent-vs-stage split** — one node is both.

## Stages

### 77.1 — One owner node, changed by dropdown (start here)

Click the agent placed on the level (e.g. Neko) → its graph shows **exactly one
node**: the **owner node** (the container itself), badged "placed on level · owns
goal", its agent chosen by a **dropdown**, its objective = the container's Goal.
Nothing else yet — no sub-agents.
- Launcher `blueprint_editor/blueprint_graph_editor_page.dart` — the node
  (`_buildCanvas` node) renders the persona + the **owner badge** + the goal label;
  `_buildInspector` carries the **persona dropdown** and the **Goal** field (owner
  node only).
- The arrow strip (Stage 76E) shows just the owner node and updates live when a
  sub-agent is later added.
- Harness: the goal is on the container's goal-owner chat (Stage 76); the dropdown
  selection is the owner binding.
- **Acceptance:** click a placed agent → exactly one owner node, agent swappable by
  dropdown, badged as the level-placed owner, carrying the goal; `flutter analyze`
  clean.

### 77.1b — Add a sub-agent node; the strip updates

Add a second node (e.g. a Dev) as a sub-agent of the owner. It is a plain node
(persona dropdown, inherited input, **no goal**); wiring the owner's output to it is
the steering edge (77.3). The arrow strip updates `Neko → Dev` live (76E) — proving
the graph grows one node at a time, owner-first.

### 77.2 — Runtime: bound agent + live status on the node

When a goal runs, the node shows the **bound persona instance + status** (76D.2's
attributed level instance), and single-click opens that agent's chat-for-this-stage.

### 77.3 — Output = sub-agent steering wire

The node's **output socket** connects to a downstream agent node; the wire is the
steering edge. Setting a node's output in the details panel (or dragging the socket)
selects the sub-agent it steers (76D.3 verb set: steer ungated, create/kill gated).

### Deferred — expand a node into a sub-graph

**Double-click a node → open a new graph** that the node coordinates (an agent that
runs its own sub-graph). Advanced; deferred until the single-node graph is solid and
the expand node clears the Node Charter.

## Tests & skills (per Stage 76 discipline)

- **Tests:** the node renders its selected persona; the inspector binds persona →
  stage `owner_slot`; the goal template renders the agent's first message; the
  output socket creates a steering edge to the sub-agent node. Headless: harness
  binding/template tests; Launcher `flutter analyze` + node/inspector widget tests.
- **Skills:** `harness-mission-lead` describes Neko as the root coordinator that
  owns the Goal and steers wired sub-agents (consistent with 76C/76D skill map). No
  new skill surface — the node is the existing model rendered.

## Hard invariants

- **One node = one agent.** One node type (agent) until a candidate clears the Node
  Charter.
- **The goal is owned by the container** (the placed Level instance / its goal-owner
  chat), rendered as the graph's explicit **owner node** (badged "placed on level ·
  owns goal"); sub-agent nodes inherit the flowed-down input and own no goal. One
  goal per container/graph.
- **A node is itself a container** — recursion is the structure (expand = open the
  node's own sub-graph), not a special node type.
- **Clicking an agent opens its home graph** — the graph it's wired into (resolved
  via `goal_id` / `spawned_by`), agent highlighted; a standalone agent opens its own
  graph. Single-click on the level = home graph (up/across); double-click a node =
  its own sub-graph (down, deferred).
- The graph is the **single system**; the persona pipeline is retired (agents are
  nodes).
- A node's persona is a **concrete runtime agent**, not an abstract slot — swap by
  re-selecting.
- The output edge is the **steering wire** (ungated steer; create/kill permission-
  gated, 76D.3).
- The node is a **projection of a blueprint stage** — never a parallel model.
