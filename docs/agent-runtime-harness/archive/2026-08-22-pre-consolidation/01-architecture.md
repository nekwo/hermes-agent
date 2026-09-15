# 01 — Mission Control Architecture: Entities + Agent Graph

> **2026-07-30 — partially describes a removed subsystem.** The goal/task mission
> lane documented here (Part A's `Chat → Goal/Task` chain and `task_bound` mode,
> Part B's tasks-as-HUD and proof gates, Part C's `mission_plan`/`owner_slot`
> mapping) was removed by [16 — Mission Lane Removal](16-mission-lane-removal.md);
> chat is the only lane. Part C's "one node = one agent, output socket = steering
> edge" graph model remains the design origin of the **kept**
> `agent_runtime/flow_graph.py` + `steered_by` runtime graph, and the
> canonical-instance-id and chat-swap-safety invariants still hold. Read with that
> split in mind.

> **Status: LOCKED.** This is the canonical entity + UI architecture for Mission
> Control. It folds the two locked stages (the entity model, locked with Tony
> 2026-06-22, and the consolidated agent-graph node model) into one source of truth
> and supersedes every prior framing (the four-object model, the "instance↔chat 1:1"
> note, the standalone persona pipeline).
>
> Companions: [02 — Blueprint Goal-Flow Engine](02-execution-engine.md) (how a goal
> actually ran) and `03-retirement-ledger.md` (deleted 2026-07-30; the code the
> two docs above retired).
>
> Repos:
> - Harness: `X:/Eternia/hermes-agent`
> - Launcher: `X:/Unreal Engine/Engine/Launcher/EterniaLauncher` (Mission Control)
> - Runtime stores: `X:/Eternia/.hermes/profiles`, `X:/Eternia/.hermes/agent-runtime`

---

## Part A — The entity model (four entities, one chain)

The "double Alice" bug (two `alice` cards in the Library) was a category error in the
data model: the Library rendered **instances** as **templates**, and two parallel
creation paths each minted a fresh per-call UUID instance. The fix is the model below.

1. **Agent / Template** — the persona definition (`alice`, `dev`, `qa`, …). Defined
   once, reusable. This is the **Library**. One template → one card. Id namespace is
   `profile:<id>`.

2. **Level agent-instance** — a *placement* of a template onto the level (Mission
   Office). **Durable**: stable identity, persists across chats. The **Placed** tab. A
   persona has one **primary** placement by default; more can be added (operator "add
   new instance" or an agent spawning a sub-agent). Multiplicity on the level is a
   **feature**, not the double-Alice bug. Forbidden: *orphaned/unattributed* instances.

3. **Chat** — the session a placed instance is currently running. One active at a time,
   **swappable**: a placement can close its chat and open a fresh one of the same
   persona while staying on the level. The instance outlives any one chat.

4. **Goal / Task** — owned by the **chat**, not the placement. The Neko operator chat
   *owns a goal*; a worker chat *carries a task*.

### Cardinalities

```
Agent/Template        1 ───< N   Level agent-instance   (one placement per persona on the level)
Level agent-instance  1 ───< N   Chat                   (over its life; one active, cyclable)
Chat                  0 ───  1    Goal   ── if it is the Neko operator (conductor) chat
Chat                  0 ───  1    Task   ── if it is a worker (slot) chat
Goal                  1 ───< N    worker Chats           (staffs blueprint slots; each carries a Task)
```

### Two load-bearing facts

- **Goal/task lives on the chat.** Swap a chat → the goal/task does *not* follow. The
  placement persists, goal-less, until it runs a new goal/task chat.
- **A goal is just a chat that owns it** → **N goals = N Neko operator chats** (peers).
  No singleton orchestrator. The cockpit's many-goals view is many Neko operator chats.

### Two surfaces — never conflate them

- **Library (templates).** One card per template, always. The double-Alice bug lived
  *here*. Once "Library shows templates only" is true, the level's instance count never
  affects the Library — five Alice placements still show one Alice card.
- **Level / Placed (runtime instances).** Multiplicity is **expected and desirable**.
  Not a dedupe target.

Add-instance / fork / chat-swap are *level* rules; dedupe is a *Library* rule. They are
orthogonal and never reconcile.

### Sub-agent spawn = a level instance

A spawned sub-agent **is** a level instance. When a goal fans out into dev/qa/backend,
each becomes a placement running its own chat and carrying its task. This makes the
`Goal → staffs → worker instances` cycle a **visible, steerable agent tree**, not a
black box. Level instances therefore have **two attributed sources** — operator
placement and **agent sub-agent spawn** — both carrying provenance (placing operator,
or parent agent + goal/task). Only orphaned/unattributed instances are forbidden.

### Derived `mode`

`PersonaInstance.mode` is **derived, not stored**: state comes from whether the active
chat carries a goal/task (none → operator chat; task → task-bound; goal → goal room).

### Canonical instance id (closed)

- The **primary** placement's instance is `persona_instance_id_for(persona_id)`
  (`personainst_profile_alice`) — the default target for place/open-chat, idempotent.
- **Additional** placements (operator add, or sub-agent spawn) derive their id from the
  scene `itemId` (`personainst_<itemId>`) — deliberate, placement-backed, attributed.
- Per-chat session id is composite — `persona_chat_<canonical_instance_id>_<token>` —
  so the roster can still parse the persona from the session prefix while preserving
  per-chat history.
- **Never reintroduce per-call UUID instances** (`unique_operator_persona_instance_id`
  is deleted — see [03](03-retirement-ledger.md)).

### Chat-swap safety

New-chat / select-old-chat on an occupied placement is **guarded, never silent**:

- **Add new instance** → mint an additional placement; run the new chat there; the
  current placement + its (possibly live) chat are untouched.
- **Replace + kill** → bind the new chat to this placement; if the current chat is live
  (`RunState ∈ {QUEUED, STARTING, RUNNING, WAITING_ON_TOOL, WAITING_ON_APPROVAL}` or an
  active worker), first `run.cancel` + close the worker, *then* swap.
- The active-chat pointer cannot move over a live run without the kill flag; the harness
  returns `chat_busy` otherwise. Run/worker fields are zeroed only when the underlying
  run/worker was actually terminated.

---

## Part B — Tasks are a HUD, not a gate

**A task / task-list is an advisory HUD self-check, not a strict gating state machine.**
It reflects what a chat is doing and flags gaps — mutable, soft, **crash-proof**. A
stuck or malformed task is a stale HUD line, never a dead-end.

What this de-fangs:
- The strict `TaskState` machine — especially **`BLOCKED` as a terminal dead-end**.
  Under the HUD model "blocked" is a warning chip plus an escalation, not a stop.
- The **proof / verdict gates** — the Evidence Stack becomes advisory evidence rows that
  *inform*, instead of `GateResult.missing` hard-blocking a transition.

### Integrity moves up a layer — it does not disappear

"Soft so it can't crash" must not become "claims false success." Resolution:

- **Proof stays as evidence, read by the goal owner.** Surfaced in the HUD and read by
  the **Neko operator chat** (conductor) at the escalation point, not as an automatic
  per-task control-flow gate.
- This **refines, does not repeal** "the harness, not the model, owns proof
  validation": `proof_gates.py` is untouched as a *computation*; what changes is that
  the **goal owner**, not a per-task gate, adjudicates, and an unmet check **escalates**
  instead of dead-ending. (Implementation: [02 §Proof](02-execution-engine.md).)

---

## Part C — The agent graph (one node = one agent)

The operator used to see **two** projections of one system: the **persona pipeline**
(historically `Goal → Neko → Dev → QA → Proof`, now graph-specific; the default is
`Goal → Neko → Backend Dev → Launcher Dev → Done`) and the **blueprint stage graph**
(`Backend Contract → … → Done`, the WHAT). They are not two systems — they are joined
by `stage → owner_slot → bound persona`. The fix: put the agent **on** the node.
**One node = one agent**, carrying both identity (who) and work (what). The standalone
persona pipeline is retired.

### Controlling principle — ONE node type for now

The **agent node is the only node type.** Grow the taxonomy *deliberately*; every new
node type must clear the **Node Charter** (below) first.

### The agent node

A node **is one agent**. Its inspector has:

1. **Persona** *(every node)* — *which agent* this node is. A concrete runtime agent (a
   placed persona instance), not an abstract slot. Swap by re-selecting (dropdown).
2. **Output → sub-agent** *(every node)* — *who it steers*. The output socket wires to a
   downstream node; that wire is both the **steering edge** and the **dataflow** wire
   (this node's output → the sub-agent's input).
3. **Objective** — the **owner node** shows the **Goal**; every sub-agent node shows its
   read-only **inherited input** (the upstream output). Sub-agent nodes have no goal
   field.

On the node face: persona (name + accent), a live **status/proof badge** (running /
blocked / ready), the objective label, and — on the owner node — an explicit **owner
marker** ("placed on level · owns goal"). Single-click opens the agent's chat.
**Double-click → expand into the node's own sub-graph is deferred.**

### Goal ownership & flow

**The goal is owned by the agent *container* — the placed Level instance / its
goal-owner chat — not by any node in the graph.** The graph is the plan that container
runs; its nodes are the sub-agents it coordinates. One goal per container/graph.

- **The container renders as its graph's owner node**, badged "placed on level · owns
  goal". It carries the Goal (entered, or shaped from a goal template). Every *other*
  node is a sub-agent (same node type, no goal).
- **The goal flows down.** The container's goal is the input to its first node(s); each
  edge carries the upstream node's **output → the downstream node's input**. A QA node
  verifies the dev node's output; no node re-states the mission.
- **Recursion is the structure, not a feature.** A node *is itself a container* —
  expand-to-sub-graph (deferred in UI) is this same mechanism one layer down, not a
  special node type.

Simple start: **one placed container + a goal** = it does the goal itself. Sub-agents
are nodes added when it fans out; each new node just receives the flow.

### Behavior

- A node runs its **agent** on its **goal template** (first message = the rendered
  objective); its live trace/HUD is the node's status.
- Its **output steers the wired sub-agent** — a continuous living-graph wire, not a
  one-shot handoff.
- **Neko** is an agent node like any other, but as the lead/coordinator it owns the Goal
  and sits at the root, steering the rest. It is not a separate pipeline entry.

### The graph is living — continuous steering, bounded by permission

The graph is **not a one-shot DAG**. The coordinator keeps steering downstream nodes for
the life of the mission. A node is a **running chat, not a function call** — live,
observable, interruptible. An edge carries **dataflow** (output → next input) *and*
**steering** (the coordinator's live authority). Two steering authorities: the
coordinator (programmatic) and the **operator** (manual, by dropping into any node's
chat).

**Bounded live-steer verb set (the safety line):**

| Class | Verbs | Danger | Permission |
|---|---|---|---|
| **Steer** (shape unchanged) | use/read output, message/re-prompt, re-scope, re-route along existing edges | `normal` | **ungated** |
| **Restructure** (shape changes) | **create** (spawn instance), **kill** (cancel run + close worker) | `warning`/`destructive` | **gated** |

Kill is provenance-sensitive: killing an **agent-spawned own child** is lighter than
killing an **operator-placed** instance (the latter always needs explicit operator
grant). Permission model (`CoordinatorPermissionScope`) is specified in
[02 §Permission](02-execution-engine.md).

### Navigation — clicking an agent opens the graph it's wired into

- **Single-click an agent on the level** → its **home graph** (the graph whose plan
  contains its node; resolved via `goal_id` / `spawned_by`), that node highlighted.
- **Single-click a node inside a graph** → open that agent's **chat**.
- **Double-click a node** *(deferred)* → drill into that agent's **own** sub-graph.
- A **standalone** agent (not wired into a parent) → its own graph (it is the owner
  node). Multiple memberships → deferred; one home graph for now.

### Node Charter — the rule for adding any node type

A new node type ships **only** after it answers all four:

1. What does it represent that an agent node cannot?
2. Sockets — what flows in, what flows out?
3. Harness projection — what does it become in the blueprint / `mission_plan`?
4. Lifecycle + permission — its steer / create / kill semantics.

Until a candidate clears this, the agent node is the only node. Explicitly deferred
candidates: artifact/output-type nodes, conditional/branch nodes, join (fan-in) nodes,
and the expand-to-sub-graph node.

### Mapping to the harness (one source of truth)

The UI agent node is a **projection of a blueprint stage** — no parallel model:

| Agent node (UI) | Blueprint stage (harness) |
|---|---|
| Persona selector | the stage's `owner_slot` binding → a persona |
| Inherited input | the stage's `objective`: container goal → first stage, then each input = upstream output |
| Output → sub-agent | the stage's outgoing **edge** to the next stage |
| Live status badge | the bound persona instance + run state |

The **container** maps to the goal-owner chat (owns the goal + `mission_plan`); a node
maps to a stage *inside* that plan. Engine details: [02](02-execution-engine.md).

---

## Hard invariants

- **One Library card per template, always.** Instances never render as templates.
- **One node = one agent**, one node type until a candidate clears the Node Charter.
- **Goal/task is owned by the chat**; swapping a chat drops it; the placement persists.
- **The goal is owned by the container**, rendered as the graph's explicit owner node;
  sub-agent nodes inherit the flowed-down input and own no goal. One goal per container.
- **A node is itself a container** — recursion is the structure (expand = open the
  node's own sub-graph), not a special node type.
- **Chat swap is guarded, never silent** — add-new-instance or replace+kill; the
  active-chat pointer cannot move over a live run without the kill flag (`chat_busy`).
- **Level multiplicity is attributed, never orphaned.** A second placement exists only
  via an attributed source (operator add, or agent sub-agent spawn); instance id ⟷ scene
  `itemId`. No path silently mints orphaned instances.
- **Clicking an agent opens its home graph** (resolved via `goal_id` / `spawned_by`); a
  standalone agent opens its own graph.
- The canonical instance id is `persona_instance_id_for(persona_id)` and matches the
  Launcher placement key (`personaId`) — **never reintroduce per-call UUID instances**.
- **Goals scale by being chats** (N goals = N Neko operator chats) — never reintroduce a
  singleton goal manager or the hardcoded role-encoded `TaskState` machine.
- **Tasks are advisory HUD; proof is evidence read by the goal owner at escalation** —
  integrity stays, gating-by-crash goes. `proof_gates.py` computation is preserved; only
  its *consumers'* gating behavior changes.
- The graph is the **single system**; the persona pipeline is retired (agents are
  nodes).
