---
name: harness-runtime-model
description: Hermes Agent Runtime mental model + first-class commands to view and operate Mission Control (goals / graphs / agents / lanes). Use instead of low-level DB/Python/scripts.
---

# Harness Runtime Model

**Model:** **goal** = a running daemon instance (the mission Tony sets/lists). **graph** = the goal's program (`mission_plan`, instantiated from a blueprint). **nodes** = stages, each owned by one agent. Neko owns the goal + a chat that scopes/steers it; the daemon ticks the graph; each node's bound agent does the work.

Default graph `neko_two_dev_default` = **Neko → Backend Dev → Launcher Dev** (no QA). **QA is a node only if the blueprint binds it.**

**Concurrency (target):** goals run as **lanes**; every agent — Neko included — is **instanced per lane**, so concurrent goals with disjoint agents don't fight; binding a busy agent **warns**; true parallel is gated by `swarm enable`. (Today: one foreground goal at a time; `goal_id == task.id`. Stage 39 lands lanes + goal id.)

`hermes` == `python -m hermes_cli.main`. No `hermes harness runtime` command. Always `--json`. Never use raw DB / Python / ad-hoc scripts to inspect.

## View

| See | Command |
|---|---|
| runtime health / daemon / open+blocked | `hermes harness status --json` |
| installed agents | `hermes harness agents --json` |
| graph templates | `hermes harness blueprint list --json` |
| all goals | `hermes harness task list --json` |
| one goal's graph (nodes/edges/bindings) | `hermes harness task show <id> --json` → `.mission_plan` |
| goal event timeline | `hermes harness task history <id> --json` |
| agent instances (goal_id/spawned_by) | `hermes harness persona list --json` |
| agent ↔ goal assignments | `hermes harness persona assignments [--persona <id>|--task <id>] --json` |
| worker sessions | `hermes harness worker list --json` |
| one run + proof | `hermes harness run show <run_id> --json` · `proof list <id> --json` |
| lanes | `hermes harness lane list --json` |
| concurrency gate / daemon | `hermes harness swarm status --json` · `daemon status --json` |
| aggregate read-model (UI) | `hermes harness snapshot --json` |

**Is QA in a goal?** `task show <id> --json` → look for a `verify`/`qa` node in `.mission_plan.stages`. Don't infer QA from `agents` (lists installed, not bound).

## Operate

| Do | Command |
|---|---|
| start a graph-routed goal | `hermes harness goal run --blueprint <id> --bind <slot>=persona:<id> …` |
| create a goal + self-drive | `hermes harness task create --start-daemon …` |
| run daemon | `hermes harness daemon start` / `stop` / `run-once` |
| steer a goal | `hermes harness task unblock <id> --reason …` / `task cancel <id>` / `task archive <id>` |
| steer a run | `hermes harness run approve\|cancel <run_id>` |
| steer an agent | `hermes harness worker pause\|resume\|interrupt\|nudge\|possess\|release <session>` |
| steer a lane | `hermes harness lane pause\|park\|resume\|drain <lane>` |
| message an agent | `hermes harness mission-chat message --persona <id> --message …` |
| concurrency gate | `hermes harness swarm enable` / `disable` |
| manual advance (debug) | `hermes harness tick` / `run-until-settled` |
