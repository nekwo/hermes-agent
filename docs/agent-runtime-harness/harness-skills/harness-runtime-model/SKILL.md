---
name: harness-runtime-model
description: Hermes Agent Runtime mental model + first-class commands to view and operate Mission Control (goals / graphs / agents / lanes). Use instead of low-level DB/Python/scripts.
metadata:
  hermes:
    surfaces: [mission_chat, mission_worker]
    modes: [standard, root_node]
    load_policy: required_preload
---

# Harness Runtime Model

**Model:** **goal** = a running daemon instance (the mission Tony sets/lists). **graph** = the goal's program (`mission_plan`, instantiated from a blueprint). **nodes** = stages, each owned by one agent. Neko owns the goal + a chat that scopes/steers it; the daemon ticks the graph; each node's bound agent does the work.

Default graph `neko_two_dev_default` = **Neko scope → Backend Dev → Launcher Dev** (no QA). **QA is a node only if the selected blueprint binds it.**

**Two graphs live in `mission_plan`** (both from the blueprint): the **stage graph**
(`.stages` + `.edges` — *execution*: stage owners, `depends_on`, and `outcome → target`
routing) and the **agent topology** (`.agent_topology` — *supervision*: a `root` plus
`source → target` `steers` edges, e.g. `lead → builder → verifier`). Default when asked
"what's the flow" is the stage graph (blueprint id, stages in order, owners, edges); add the
`agent_topology` steering graph when the operator asks who steers/coordinates whom. The two
can differ in ordering — topology is a supervision graph, not the execution order.

**One orchestrator.** The stage graph is the ONLY thing that picks the next agent.
There is no legacy/text-inference fallback and no routing on/off switch: every goal is
graph-typed at creation, and a goal the graph cannot dispatch **refuses loudly**
(`legacy_orchestrator_removed`) rather than guessing an owner from the goal's wording.
So an unscoped goal starts at its graph root (Neko scopes first) — it does not jump
straight to a Dev because the title mentions Launcher or backend. If routing looks
wrong, read `.mission_plan.stages`/`.edges`; the answer is always in the graph.

**Concurrency:** goals run as **lanes**; every agent — Neko included — is **instanced per lane**, so concurrent goals with disjoint agents don't fight; binding a busy agent **warns**; true parallel is gated by `swarm enable` (a live toggle) plus the burn-in **certification gate** (`swarm status --json` → `certification_allows_production`; 10 consecutive green unattended cases required). `goal_id == task.id`.

`hermes` == `python -m hermes_cli.main`. No `hermes harness runtime` command. Always `--json`. Never use raw DB / Python / ad-hoc scripts to inspect.

## View

| See | Command |
|---|---|
| runtime health / open+blocked | `hermes harness status --json` |
| installed agents | `hermes harness agent list --json` |
| graph templates | `hermes harness blueprint list --json` |
| all goals | `hermes harness task list --json` |
| full graph for one goal (stage nodes/edges/bindings) | `hermes harness task show <id> --json` → `.mission_plan` |
| supervision/steering graph for one goal | `hermes harness task show <id> --json` → `.mission_plan.agent_topology` (`root` + `steers` edges) |
| goal event timeline | `hermes harness task history <id> --json` |
| agent instances (goal_id/spawned_by) | `hermes harness persona list --json` |
| agent ↔ goal assignments | `hermes harness persona assignments [--persona <id>|--goal <id>] --json` |
| worker sessions | `hermes harness worker list --json` |
| one run + proof | `hermes harness run show <run_id> --json` · `proof list <id> --json` |
| lanes | `hermes harness lane list --json` |
| concurrency gate + certification | `hermes harness swarm status --json` |
| aggregate read-model (UI) | `hermes harness snapshot --json` |
| level agents shown in Mission Control | Stage C MCP `mcp_launcher_qa_get_buttons` with `scope=mission_control.agent` |
| compact Mission Control graph probe | Stage C MCP `mcp_launcher_qa_get_widget_state` with `widget=mission_control.graph` |

**Is QA in a goal?** `task show <id> --json` → look for a `verify`/`qa` node in `.mission_plan.stages`. Don't infer QA from `agents` (lists installed, not bound).

**Do not use for level agents:** `status.agents` and `hermes harness agent list --json` rosters show configured/installed Harness agents. They do **not** show the graph-bound agents currently on a Mission Control level. Use `.mission_plan` for the full graph and Stage C MCP `mission_control.agent` for the visible level-agent selection surface.

**Normal persona chat is globally chat-only.** For every role, a one-off request stays
on the existing Mission Control persona/session and must not create or enter a
goal/task/graph/worker lane. The runtime strips `mission_goal_create` from ordinary
chat, including unbounded permission mode. Heavy mission creation is a separate
operator workflow and requires the dedicated `mission-chat message
--allow-mission-goal` opt-in on that exact turn; `intent_hint`, `assign_work`, broad
investigation language, MCP verification, and multi-agent coordination never imply it.

## Operate

| Do | Command |
|---|---|
| start a graph-routed goal | `hermes harness goal run --blueprint <id> --bind <slot>=persona:<id> …` |
| create a goal + self-drive | `hermes harness task create --start-daemon …` (there is NO standalone `daemon` command — the Mission Daemon starts only via `--start-daemon` at create) |
| steer a goal | `hermes harness task unblock <id> --reason …` / `task cancel <id>` / `task archive <id>` |
| steer a run | `hermes harness run approve\|cancel <run_id>` |
| steer an agent | `hermes harness worker pause\|resume\|interrupt\|nudge\|possess\|release\|takeover <session>` |
| steer a lane | `hermes harness lane pause\|park\|resume\|drain <lane>` |
| find the on-level chat instances to message | `hermes harness persona list --json` → chat-mode `personainst_<role>_agent_<hash>` rows (cross-check Stage C `mission_control.agent` buttons) |
| continue an existing chat root | `hermes harness persona instance open-chat --persona-instance-id <instance> --persona <id> --session-id <root> --json` (`--session-id` is required unless `--new-session` or `--add-instance`) |
| create a new server-minted chat on an existing instance | `hermes harness persona instance open-chat --persona-instance-id <instance> --persona <id> --new-session --idempotency-key <key> --json` |
| message an exact chat root (default; chat-only) | `hermes harness mission-chat message --persona <id> --persona-instance-id <instance> --session-id <root> --client-message-id <id> --message … --json` |
| explicitly opt one turn into heavy goal creation | `hermes harness mission-chat message --persona <eligible-id> --session-id <root> --allow-mission-goal --client-message-id <id> --message … --json` |
| abandon an outcome-unknown turn | `hermes harness mission-chat turn-resolve --session-id <root> --client-message-id <id> --turn-id <turn> --action abandon --json` |
| concurrency gate | `hermes harness swarm enable` / `disable` |
| manual advance (debug) | `hermes harness tick` / `run-until-settled` |

## Persona chat continuity

**Message the on-level instance.** Persona instances and their chat roots use
one chat lane. Legacy lifecycle metadata does not create a separate routing
class and does not change whether an exact, owned chat root can receive a turn.

`PersonaInstance.default_chat_session_id` is the operator-chat pointer. Worker
and run session IDs are separate and must never be used as chat roots. Hermes
mints every new root; callers may use a local draft identity only while waiting
for the `open-chat --new-session` result.

The pointer can go stale: `mission-chat message` may reject a roster-listed
root with `unknown_chat_session` ("unknown explicit persona chat root"). Do not
keep retrying it — mint a fresh root with `open-chat --new-session
--idempotency-key <key>` and message that. External operators must run the CLI
with the runtime's `HERMES_HOME` (the profile that owns the harness store);
under the wrong home, `persona list` returns an empty roster and chat roots
resolve nowhere.

Treat `session_id` in chat commands as the stable root. Native compression may
rotate `active_session_id`; it does not change the root selected by Mission
Control. Runtime-state projections are observer-qualified: only the owning
long-lived serve process may report `hot`, `busy`, `cold`, or `failed` from its
resident registry; external CLI snapshots report `unknown`.

If a turn returns `chat_turn_outcome_unknown`, do not retry it. Resolve the
exact `(root, client_message_id, turn_id)` tuple with `turn-resolve ...
--action abandon`, then send the text as a new turn with a fresh client
message ID.
