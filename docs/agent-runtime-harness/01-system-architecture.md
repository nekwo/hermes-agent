# 01 — System Architecture: the entities and how they relate

> **What this domain is.** The Agent Runtime Harness is a Hermes-native persona
> runtime for Mission Control. **Chat is the only execution lane**: an operator
> or an agent messages a placed persona instance's durable chat root, and the
> runtime owns identity, chat continuity, realms and workspaces, the Mission
> Office scene, the board, and an enforcement-free agent graph. This document
> is the entity model — what the things ARE and who owns what. How a turn
> executes, how state is stored, and how it syncs are other domains.

Every claim below was verified against code at HEAD on 2026-08-22; anchors are
`path:line` or a named function, and unverifiable carry-forwards are quarantined
in their own section at the bottom.

## The chat-only lane

There is one runtime execution surface. `GPTPersonaRuntime`
(`agent_runtime/persona_runtime.py:51`) exposes exactly **one** public method,
`mission_chat_reply` (`:72`) — there is no `run_persona`, no tick, no worker
loop. The entry point is `_cmd_mission_chat_message` — defined at
`hermes_cli/harness_parts/persona_commands.py:2034`, exec-loaded into
`harness.py` globals (`hermes_cli/harness.py:4419`) and wired to argparse at
`harness.py:1124`.

Turn ingress has one path. Asynchronous agent-to-agent delivery
(`agent_chat_send(wait=false)`) does not inject a message: a serve-hosted drain
forges a real turn through **the same handler an operator message goes through**
(`agent_runtime/dispatch_delivery.py:1029`, docstring `:12-19`), which is what
keeps transcript, live log, turn journal and projection consistent for free.
Two narrower append seams do exist and are deliberate, turn-less writes — the
bounded child-summary mirror (`agent_runtime/continuity.py:52-62`, posted by
`return_summary_to_parent_session`) and the explicit-append seam
(`persona_commands.py:6071`, whose own docstring records the open question of
declaring the persona-chat write path native-only). Doc 05 §8 owns their
contract; neither runs a turn or reaches the provider.

MCP tool admission is data-owned, not role-owned: a persona may admit only the
servers its backing profile declares, and "role names do not narrow or widen
that data-owned set" (`mcp_admission.py:26-29`). `R1_ADMISSIBLE_ROLES` returns
zero hits in production source (its only survivors are quotes in the archived
removal doc).

## The entity chain

Four things, in one chain, each with a distinct lifetime.

**1 — Persona template.** `AgentPersona` (`agent_runtime/models.py:252`) — the
definition: display name, role, model/provider/api_mode, toolsets, skills,
`hermes_profile`, budgets, readiness. Personas are **data**, from the config
block (`config.persona_records_from_config`, `agent_runtime/config.py:524`)
merged with persisted store rows (`ensure_persisted_personas`, `:570`, over
`store.AgentStore` at `store.py:152`). Nothing in code declares them — S11 left
`DEFAULT_PERSONA_IDS`, `BASE_PERSONA_ID`, `DEFAULT_SUPERVISOR_PERSONA_ID`,
`ALLOWED_TOOLSETS_BY_ROLE` and `PER_ROLE_TOOL_DENIES` as scoped tombstone rows
against `agent_runtime.personas` (`tests/agent_runtime/test_tombstone_registry.py`,
s11 rows). A definition can be withheld without being deleted:
`persona_lifecycle.py`'s `DISABLED_ROLE_TOKENS` / `MOTHBALLED_*` keep it in the
administrative catalog but never let it acquire a live instance
(`is_runtime_persona`).

**2 — Durable persona instance (the placement).** `PersonaInstance`
(`models.py:310`). Stable identity, outlives any one chat. Ids are structurally
prefixed `personainst_` (`models.py:15`), and there are exactly two derivations:

- the **canonical operator channel** — `persona_instance_id_for(persona_id)`
  (`persona_assignments.py:2501`), e.g. `personainst_profile_alice`;
- a **placement-backed** row — `persona_instance_id_for_placement(placement_id)`
  (`:2505`), whose tail is the scene `itemId`.

`is_canonical_persona_channel` (`:2509`) is the live discriminator between them.
Every id arriving from outside the store passes the single derivation authority
`canonical_persona_instance_id` (`:2525`), which folds the two structurally
recognizable drift schemes; ids predating that chokepoint are folded by
`persona_instance_identity.py`, whose `identity_aliases_for_rows` ships as
`identity_map` on the snapshot. Per-call UUID instances cannot be minted —
`unique_operator_persona_instance_id` returns zero hits in production source
(one quote survives in the archived doc 01).

**3 — The chat root (swappable).** `PersonaInstance.default_chat_session_id`
(`models.py:360`) is a durable pointer, deliberately independent of everything
else on the row. Ids are minted `persona_chat_<instance>_<hex>` by
`persona_chat_session_id_for` (`persona_assignments.py:2556`), and made
**durable at the bind argument**, not beside it, so a pointer cannot be stored
before the transcript row exists (`_durable_chat_root`, `:2561` — the failure it
fixed was a dragged-in agent whose every send was refused with
`unknown_chat_session`). `chat_head_home` (`models.py:379`) records *where* that
pointer dereferences: which profile DB holds a transcript is a per-conversation
fact no machine-level pointer can answer.

**4 — The scene placement.** `OfficeActor` (`models.py:179`) with its
`OfficeItem`s (`:157`), one file per actor under `office/<workspace>/actors/`.
The `actor_key` is minted only by `OfficeStore` — `canonical_persona_instance_id`
for instance-bound actors, else the persona id (`office_store.py:113`
`_canonical_actor_key`). Actor granularity, not item granularity, is the merge
unit, so an agent and its coupled desk travel together.

**One call creates all of it.** `agent_create.perform_agent_create` writes the
roster row, the durable chat root and the placement inside one function. The
order is instance-first and that is not arbitrary: a placement written first
would be a half-state naming an instance the runtime never minted, and the
launcher's codec refuses on principle to derive a binding for an actor that has
none — the function's own docstring is the long form. (This paragraph carried
`agent_create.py:692` from consolidation until 2026-08-27, by which time the
function was at `:1205` and the second citation, `:726-732`, landed inside an
unrelated constant block. Symbols only here, for that reason.)

**And it chooses WHERE, when the caller did not.** `position` is optional on
both doors (plan S2): absent, the slot comes from
`agent_runtime/office_layout_policy.py` — the same deterministic lattice the
launcher predicts with, pinned across the two repos by a byte-identical case
fixture; present, it is written verbatim. The ack returns `position` (what was
written) and `actor` (the row as stored, in `runtime.office.get`'s own item
shape), so a caller with no canvas — the CLI, a cron, a remote connector over
`call` — needs neither a guess going in nor a second read coming out. Where that
policy read sits relative to `office_lock`, and the one-slot race it leaves, is
stated at `resolve_placement_position` and in
[06 — The office surface](06-office-and-board.md#the-placement-verb--where-an-unaimed-create-lands-and-what-it-hands-back).

**And it can hand the new agent its SKILLS — but that phase is outside the
join.** `skills: [id, …]` on the RPC (`--skill`, repeatable, on the CLI) runs
`agent_create.run_skills_phase` AFTER both writes are durable, writing
`skill_overrides` at the INSTANCE tier through
`PersonaInstanceStore.update_profile` and never `persona.skills`, which would
reconfigure every other instance of that persona. Absent, `skill_overrides`
stays `None` and the agent inherits its persona's skills live; the ack's
`skills.inherited` says which of those two an empty `assigned` list means. The
reservation gains `placed` between `instance_minted` and `done` so a skills
refusal keeps the agent — standing, messageable, resumable under the SAME
idempotency key — instead of retiring a working agent to undo a file copy. The
mechanism and its two gates are [05 §9](05-chat-turn-lane.md#9-the-agent-create-path);
the placement consequence is
[06 — the placement verb](06-office-and-board.md#the-placement-verb--where-an-unaimed-create-lands-and-what-it-hands-back).

**And ONE call takes it all away again.** `perform_agent_retire`
(`agent_runtime/agent_retire.py`) is the inverse, over the store method that
always did both halves (`PersonaInstanceStore.retire` archives the row AND, through
`OfficeStore.archive_actors_for_instance`, every actor bound to the instance).
What it adds is a DOOR (`runtime.agent.retire`, `harness agent retire`, and
`persona instance retire` delegating to the same function — with `persona
instance delete` as a full argparse alias since 2026-08-27, because the
operator's verb is delete) and a RECEIPT: the ack
NAMES every actor it archived (`archived_actor_keys`) beside every one it could
not (`office_archive_failures`). The office half is still best-effort — the roster
archive is authoritative with or without the office projection — but it is no
longer SILENT, which is what let a half-state (row archived, desk still on the
canvas) exist with nothing able to detect it. Retiring an already-archived id
replays the same ack with `already_retired: true` rather than refusing, so a
client that lost the ack can ask again — and the replay first SWEEPS any actor a
client mechanically resurrected back into the archive, because idempotent must
not mean inert (the boot-flush resurrection incident, [06
§inverse](06-office-and-board.md#the-inverse--one-call-takes-an-agent-off-the-level-and-says-what-left)).

**"Placed" is the JOIN, and neither store is folded into the other** (placement
plan D1). Entity 2 answers *does this agent exist* — `persona instance create
--add-instance` is the roster-only recovery door and mints rows with no
placement, on purpose — while entity 4 answers *is it on this level*. So:

- **placed** — a live instance-keyed `OfficeActor` whose `persona_instance_id`
  names a live `PersonaInstance` row. Both halves, or it is not placed.
- **unplaced row** — a live placement-backed row (`is_canonical_persona_channel`
  is the discriminator; a canonical operator channel is not a placement and
  never was) that no live actor references. **Legal**, and the recovery door's
  normal output.
- **orphan actor** — a live instance-keyed actor whose instance is retired or
  missing. A **defect**: it renders as an agent nothing can message.

The join gets a **read**, never a merge: `harness doctor`'s `placement_census`
(`harness_doctor.py::_placement_census_report`) reports the three per workspace
and repairs nothing, because both repairs are operator gestures and a doctor
reconciling them would be picking which store was wrong from one snapshot. See
[07 — Observability](07-observability.md#the-doctors-report-roster).

The chain ends there. There is no Goal and no Task — `Chat → Goal/Task` is
gone; see [the removal section](#what-the-mission-lane-removal-deleted-and-why).

### `mode` is stored, not derived

The archived entity model called `PersonaInstance.mode` derived. It is not:
`open_chat` writes `instance.mode = "chat"` (`persona_assignments.py:1847`),
unbinding the last chat pointer reverts it to `"configured"` (`:856-862`), and
`create_free_floating` mints `"free_floating"` (`:426-444`). The vocabulary also
still contains `"task_bound"`, which is still written — see
[Open rows](#open-rows).

## The agent graph — enforcement-free

The graph is an **authored map that sets one field**. A launcher flow chart
arrives whole as one JSON document (`hermes harness flow set --graph`), is
stored verbatim, and its owner edges are applied to instances that **already
exist**: "Ingest never creates, starts, or deletes an instance, and it never
touches goal membership: a chart states who steers whom, nothing else"
(`agent_runtime/flow_graph.py:9-17`). A parsed doc carries only node id →
optional bound instance, plus an ordered edge list (`FlowGraphDoc`, `:82-93`).
There is no node type in the runtime — a grep for `node_type` across
`agent_runtime/` returns nothing.

**Graph identity IS the owner instance's id** (`graph_id: runtime:<owner>`), so
a map is that one instance's blueprint and may only set or clear *itself* as a
parent in each referenced child. Parents set by another lead's map are
preserved, so two leads' maps compose into fan-in instead of clobbering each
other (`flow_graph.py:19-31`). Non-owner edges drawn on a lead's canvas are
reported, never applied.

The persisted truth is `PersonaInstance.steered_by` — an ordered **set** of
parent instance ids (`models.py:337`), written through the single declarative
chokepoint `PersonaInstanceStore.set_parents` (`persona_assignments.py:517`;
an empty set detaches the child). `spawned_by` (`models.py:333`) is
PROVENANCE, not steering: two live writers outside the store stamp it with a
principal — `agent_create.py:1037` sets `"operator"`, and
`_maybe_stamp_spawned_by` (`persona_commands.py:1499-1504`) stamps
`coordinator_id or "operator"`. (The comment at `models.py:330-332` calling it
a store-written mirror of `steered_by[0]` is stale against its own file's
writers.) Steering itself admits only instance-shaped tokens — read-side
filters apply `looks_like_persona_instance_id` (`models.py:18`;
`snapshot.py:2037`, `runtime_hud.py:639`) so a principal such as "operator" is
provenance, never a parent, which is what keeps the historical "steered by
operator" phantom edge unrepresentable in the graph.

Because graph identity is the owner's id, a stored doc outlives its owner. The
persona-instance reconciler's last phase archives owner-less canvases into
`flow_graphs_stale/` — never deletes — strictly on owner liveness, since an
empty canvas whose owner is live is intended, not garbage
(`flow_graph.py:33-39`, `:446`).

What the graph does *not* do: schedule, gate, or execute. It feeds the agent's
`## Runtime Situation` block, whose declared field roster
(`runtime_hud.py:151` `HUD_FIELDS`) is `preview · scope · lane · mission ·
roster · steering · board` plus two volatile rows (`turn_budget`, capability).
Steering is the one block always emitted, because an explicit empty block is
the honest "standalone" answer (`runtime_hud.py:721-725`).

## Realms and workspaces

`Realm` (`models.py:49`) is the sync and publish boundary; `Workspace`
(`models.py:33`) is the scene and roster boundary. A realm holds
`workspace_ids` plus a `deleted_workspace_ids` resurrection-guard ledger that
travels with it, so a member holding a stale local copy neither republishes nor
re-adopts a deleted workspace. Realms own what publishes: `skill_publish_mode`
(`all` | `selected`) and `agent_publish_mode` (`workspace` | `selected`), with
personas required by a roster or an Office placement pinned regardless, so a
pulled workspace can never point at an absent persona definition. Stores:
`WorkspaceStore` (`store.py:173`), `RealmStore` (`:472`); active pointers are
single files (`paths.active_workspace_path()` / `active_realm_path()`).
Server-bound realms authorize every sync action against the Eternia backend and
**fail closed** (`realm_membership.py:1-12`).

Workspace scoping is its own authority, and it governs **advertising and
bare-persona resolution only** (`agent_runtime/workspace_scope.py`):

- `workspace_id` of `None` on an instance means runtime-global — visible and
  addressable in every workspace.
- A non-`None` pointer is a "belongs to THIS workspace" claim.
- A scope of `None` (no active workspace) degrades to unscoped rather than
  hiding the roster.
- `exclude_global_canonicals` (`:191`) — a persona's auto-derived canonical row
  is never advertised into a real workspace scope, because instance means
  in-level placement.
- `shadow_canonical_by_placement` (`:150`) — where an in-scope placement
  exists, a bare persona id lands on the deliberate placement, not the plumbing
  row.

Explicit `personainst_*` targeting stays legal cross-workspace, and identity
lookups always read the full unfiltered roster — a steering edge into another
workspace is a real graph fact even when it is not addressable.

`workspace_template.py` copies authored **structure** between workspaces (office
taxonomy and placements, the default board's active cards, roster, settings);
history is never copied.

## The board

Workspace-scoped kanban, and **planning state only**: "Cards are planning
state. They do not carry or mutate mission records"
(`agent_runtime/board_store.py:8-15`). `Board` / `BoardColumn` / `BoardCard`
are at `models.py:135` / `:92` / `:108`. `BoardStore` is the single write
chokepoint and emits a typed event on every mutation.

Three properties make it converge across machines without a merge engine: the
default board id is deterministic (`board_default_<workspace_id>`,
`board_models.py:39`) with default columns on fixed ids and behaviour keyed on
`kind`, never `title` (`models.py:92-105`); archive-never-delete, with an
`archived_card_ids` ledger blocking a pulled remote copy from resurrecting a
locally archived card; and card position is a fractional `order_key` whose moves
allocate the midpoint between neighbours (`board_order`).

Agents reach it through the upstream-owned `tools/board_tool.py`, where
resolution is now **two rungs**, not three — explicit `board_id`, else the active
workspace's default board (`board_tool.py:79-102`) — and passively, as the
advisory `board` digest row of `HUD_FIELDS`.

## Skills

`docs/agent-runtime-harness/harness-skills/` is **installed source and stays
live in place**. It is the repo-side origin
(`skill_install.harness_skill_source_root`, `agent_runtime/skill_install.py:27`);
`harness install-harness-skills` (`hermes_cli/harness.py:1343`) copies each
package to the single shared canonical root, `get_shared_skills_dir()` —
root-relative, not per-profile, so every persona references one copy and realm
sync publishes it (`skill_install.py:38-43`). Never edit the installed copy.

The directories present match `hermes_constants.CANONICAL_SHARED_SKILL_IDS`
(`hermes_constants.py:19`) exactly — five since 2026-08-28:
`harness-dev-delivery`, `harness-continuity`, `harness-qa-verdict`,
`harness-runtime-model` and `harness-charsheet-authoring`. The 2026-08-28
skills consolidation deleted `harness-mission-lead` (the mission-lane tombstone)
outright, folded `launcher-analyze-proof` into `harness-qa-verdict`, and folded
the shared-root-only `mission-control-harness` into `harness-runtime-model` —
whose lean `SKILL.md` stays the required preload while the absorbed operating
manual lives in its on-demand `references/operations.md`. Each of those merges
kept the count at five, not raised it. Read
the constant for the count; this list is a gloss and went stale within a day of
`harness-charsheet-authoring` joining it.

**The two copies are joined by `install-harness-skills` and by nothing else,
and the pre-push hook is what makes that reliable.** A turn loads the INSTALLED
package — for a canonical id the resolver rejects every other candidate
(`skill_utils._skill_resolution_status` → `invalid_source` for any
`source_kind` but `shared_core`) — so a commit that edits the repo copy changes
the documentation and changes nothing an agent reads. It happened: the
`harness-charsheet-authoring` package was installed at `5504706978` and edited
twice more the same hour; the installed copy stayed 449 B behind for two days,
and the live gate turn's `used_skills` row carried the stale package's hash.
Tests do not see this — they read the tree, which is correct by construction.
So the check runs where the guarantee lives, on the machine:
`scripts/verify_harness_skill_install.py` installs every canonical package and
then fails if `harness_skill_hash_mismatches` is non-empty (`--check` verifies
without writing), and `.githooks/pre-push` runs it on every push. **One command
per clone arms it** — `git config core.hooksPath .githooks`, which git shares
across every worktree of that clone. The runtime reports the same divergence
passively as the `skill_hash_mismatch` readiness code
(`agent_runtime/profile_readiness.py`), which is where to look when a persona
is behaving like an older version of its own skill.

Two adjacent lanes are live. `skill_promotion.py` is the one guarded door
through which downloaded or authored packages become canonical: downloads land
in a per-realm inbox the resolver never sees, displaced packages are archived
not deleted, promotion is an atomic `os.replace`, and provenance lives outside
skill dirs so it cannot change a package's content hash.
`external_skill_links.py` links the shared root into other harnesses on the
machine (Claude Code, Codex), idempotently and non-destructively.

## Persona identity

Identity is layered, and each layer has an owner. The Mission Control chat
system message is composed by `_mission_chat_surface_message`
(`persona_runtime.py:482`) in this order:

1. **Runtime identity** — a first-person block naming the selected persona and
   making self-relay impossible (`_mission_chat_identity_prompt`, `:434`).
2. **Profile SOUL** — the profile-owned durable character and voice.
3. **Operator-channel rules** — tool, permission, clarification and
   anti-fabrication behaviour, always applied.
4. Optional workspace `AGENTS.md`, behind a fixed preamble that states the
   boundary: a repo doc describes the repo and never redefines how this channel
   handles confirmation (`MISSION_CHAT_WORKSPACE_AGENTS_PREAMBLE`, `:473`).
5. Optional operator per-session surface prompt.

SOUL resolution defaults to `profiles/<hermes_profile>/SOUL.md` with **no
fallthrough**: on a miss a bare `SOUL.md` must never resolve to the operator
profile's SOUL, which is the persona-identity-leak class
(`_mission_chat_soul_overlay`, `:949-960`). Profile memory and core context
files are persona-declared opt-ins (`models.py:266-267`). That whole string is
**byte-stable for the life of a conversation** by invariant, because the
transport keys its cross-turn prompt cache on `sha256(instructions + tools)`;
anything per-turn volatile — the Runtime Situation HUD, the queued-skill
preload — rides the operator's *user* turn instead (`:496-511`,
`_mission_chat_user_message` at `:533`).

Visual identity is a separate live lane: `agent/charsheet/` generates
directional character sheets behind `hermes harness` verbs
(`hermes_cli/harness.py:3001+`), and a placement carries its sprite as
`OfficeItem.pet_slug` (`models.py:174`).

## What the mission-lane removal deleted, and why

On 2026-07-30 the goal/task mission lane was removed whole. Deleted: goal and
task records and their stores; the dispatch loop and worker execution
(`goal_runner`, `ticker`, `supervision`, `worker_actions`, `root_node_engine`,
`node_tools`, `agent_runtime.reconciler`, `recovery`, `planning`, `liveness`);
the proof and gate machinery (`proof_gates`, `gates`, `final_gate`, `promotion_gates`,
`proof_runner`, `burn_in`, `smoke`, `replay_scenarios`); the stage graph
(`mission_plan`, `default_plan`, `state_machine`); and role gating of tools and
MCP admission. The receipt is data-driven and enforced:
`tests/agent_runtime/test_tombstone_registry.py` holds one row per banned name,
scanned against AST-reparsed source so a retirement comment can never satisfy
its own gate.

**Why**, in the words of the archived docs that argued it: the controlling
principle carried forward from the root-node design is *no judgment in Python;
the harness is substrate only*
([08](archive/2026-08-22-pre-consolidation/08-blueprint-as-script-collapse.md),
header note). That is incompatible with a per-task state machine whose `BLOCKED`
was a terminal dead-end and whose proof gates could hard-block a transition.
Judgment moved to the agent in the chat lane; the runtime keeps identity,
continuity, scope and evidence.

Two things were deliberately kept: `agent_runtime/blueprints/resolve.py`, a
permanent re-export of `promote_profile_to_persona` for the upstream
profile-promotion endpoint, and `task_store_stub.TaskStoreStub` (re-exported as
`TaskStore` at `store.py:149`) under ruling R-3 — though its stated cause has
since changed; see Open rows. Personas and profiles were **not** deleted:
nothing under `.hermes/profiles/` was touched, only the hardcoded logic that
declared them.

The full S0–S12 record, the six corrected premises, the five operator rulings
and the final acceptance are in
[16 — Mission Lane Removal](archive/2026-08-22-pre-consolidation/16-mission-lane-removal.md).

## Invariants

- **One template, one Library card; instances are never rendered as templates.**
  Level multiplicity is a feature; Library dedupe is a different surface. The
  "double Alice" defect was the two being conflated.
- **A persona-instance id is derived, never invented** — canonical from the
  persona id, or placement-derived from the scene `itemId`. Never a per-call
  UUID: that is what minted the duplicate rows the reconciler now folds, and it
  is why level multiplicity is attributed rather than orphaned.
- **The chat root is independent of everything else on the row.** Opening
  another chat must not cancel or rebind other work
  (`persona_assignments.py:1807-1810`), and a bind is refused for a
  sibling-steal or a retired instance (`assert_bindable`, `:1654`).
- **A chat pointer is never stored before its transcript exists**
  (`_durable_chat_root`, `:2561`). The ordering is structural — the pointer
  cannot be bound without the durability call having returned.
- **Graph ingest never creates, starts, or deletes an instance**
  (`flow_graph.py:9-17`). A map states who steers whom, and only its own owner's
  edges, so two leads' maps compose into fan-in instead of fighting.
- **Only an instance can be a steering parent** (`models.py:18`). A principal is
  provenance, not a parent.
- **Archive, never delete** — board cards, office actors, persona-instance rows,
  owner-less flow graphs and displaced skill packages move aside and leave a
  resurrection-guard id in a bounded ledger. Deletion loses the evidence that a
  pulled remote copy is stale.
- **Personas and profiles are data.** No code declares a persona, a role
  ceiling, or an MCP admission floor. An unknown role stays active by default.
- **Advertising is scoped; identity is not.** Workspace scope narrows who an
  agent may be offered and who a bare persona id resolves to — never who
  something *is*.
- **Volatile facts never enter the system prompt.** They ride the user turn, or
  the ~13K-token stable prefix is re-billed every turn.

## Open rows

Each links to a `planned/` file carrying its evidence and the gate to open it.

- 2026-08-22 — **`--kill-active` is a no-op that reports success**: accepted by
  `open_chat`, never read; the CLI echoes the operator's own flag back as
  `killed_previous` →
  [planned/chat-swap-kill-active-guard.md](planned/chat-swap-kill-active-guard.md)
- 2026-08-22 — **`mode="task_bound"` is still written** by a flag documented as
  a correlation id, five readers still branch on it, and `TaskStoreStub` has
  lost the upstream caller its permanence rests on →
  [planned/task-bound-vocabulary-retirement.md](planned/task-bound-vocabulary-retirement.md)
- 2026-08-22 — **a writer-less lane store still projects onto `status`**
  (`foreground_runtime`, `runtime_instances`), and two live docstrings describe
  deleted execution lanes →
  [planned/writerless-goal-lane-residue.md](planned/writerless-goal-lane-residue.md)
- Deferred by design — **no node taxonomy, no sub-graph expansion**; the Node
  Charter's four questions are unanswered for every candidate →
  [planned/graph-node-taxonomy-and-subgraphs.md](planned/graph-node-taxonomy-and-subgraphs.md)
- Queued — **the global-singleton persona-instance redesign**, cited at
  `persona_assignments.py:2518` →
  [planned/global-singleton-persona-instances.md](planned/global-singleton-persona-instances.md)
- 2026-08-24 — **a character's state vocabulary is fixed at `start`**: there is
  no `characters add-state`, so adding a strip to an installed sheet means
  re-authoring the character →
  [planned/charsheet-add-state.md](planned/charsheet-add-state.md)

## Unverified carry-forward

- **Launcher-side entity rendering.** The archived model
  ([01-architecture.md](archive/2026-08-22-pre-consolidation/01-architecture.md),
  Part A/C) states Launcher rules — Library shows templates only; single-click
  an agent opens its home graph; single-click a node opens that agent's chat;
  double-click-to-expand is deferred. These are Flutter behaviours in
  `EterniaLauncher`, outside this repo, and were not verified here. The runtime
  half of the "home graph" resolution (`graph_id = runtime:<owner>`, one map per
  owner instance) IS verified above; the click semantics are not.
- **Neko prompt-layer ordering beyond the chat builder.** The eight-step order
  in
  [neko-persona-identity-deploy.md](archive/2026-08-22-pre-consolidation/neko-persona-identity-deploy.md)
  opens with "Hermes core" and closes with profile memory and conversation
  history. Steps 2–5 are verified above against
  `_mission_chat_surface_message`; the Hermes-core preamble and the
  memory/history tail assemble outside this builder and were not re-derived.

## Supersedes

It also replaces `planned/agent-placement-verb.md`, **deleted 2026-08-27 by the
S10 fold-in commit** (`git log --diff-filter=D --oneline -- docs/agent-runtime-harness/planned/agent-placement-verb.md`
recovers it): the entity half of that plan — the create's three phases, the
inverse, and "placed" as a JOIN rather than a merge — is the truth stated above.

Beyond that it replaces, for current truth, the entity/architecture content of
these files under `archive/2026-08-22-pre-consolidation/`:

- [01-architecture.md](archive/2026-08-22-pre-consolidation/01-architecture.md)
  — the locked entity model. Parts A/B's `Chat → Goal/Task` chain, `task_bound`
  mode, tasks-as-HUD and proof gates are history.
- [02-execution-engine.md](archive/2026-08-22-pre-consolidation/02-execution-engine.md)
  — the removed stage-graph engine. Only its profile→persona promotion
  substrate survives, via `agent_runtime/blueprints/resolve.py`.
- [08-blueprint-as-script-collapse.md](archive/2026-08-22-pre-consolidation/08-blueprint-as-script-collapse.md)
  — the root-node execution model; ancestor of "no judgment in Python", nothing
  more.
- [04-decision-hud-simplification-map.md](archive/2026-08-22-pre-consolidation/04-decision-hud-simplification-map.md)
  — its steering sections are the design origin of the kept `steered_by` edges.
- [00-index.md](archive/2026-08-22-pre-consolidation/00-index.md) — the previous
  live-truth ranking.
- [neko-persona-identity-deploy.md](archive/2026-08-22-pre-consolidation/neko-persona-identity-deploy.md)
  and [neko_SOUL_draft.md](archive/2026-08-22-pre-consolidation/neko_SOUL_draft.md)
  — persona identity ownership; the SOUL text remains a live reference copy.
- [CHARACTER_SHEET_8WAY_PLAN_2026-08-17.md](archive/2026-08-22-pre-consolidation/CHARACTER_SHEET_8WAY_PLAN_2026-08-17.md)
  — built and merged; `agent/charsheet/` is live, and the sheet contract itself
  is owned by the Launcher spec.
- [SKILL_INBOX_PROMOTION_DESIGN_2026-07-24.md](archive/2026-08-22-pre-consolidation/SKILL_INBOX_PROMOTION_DESIGN_2026-07-24.md)
  — implemented as `agent_runtime/skill_promotion.py`, which names it as its
  design authority.
- [16-mission-lane-removal.md](archive/2026-08-22-pre-consolidation/16-mission-lane-removal.md)
  — superseded only for the *summary* above. It remains the authority for the
  S0–S12 record and the operator rulings; linked, never reproduced.
