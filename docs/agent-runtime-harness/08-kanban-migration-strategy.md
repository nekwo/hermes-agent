# Stage 8 — Kanban Non-Dependency / Clean Separation Strategy

## Goal

Keep the Agent Runtime Harness independent from Kanban.

Tony's direction: **no Kanban import, no Kanban bridge, and no Kanban dependency for Mission Control / Agent Runtime Harness.** The harness should stand on its own as the PM/dev/QA agent runtime. Existing Kanban code can continue to exist for current Hermes users, but it is not part of the harness roadmap.

## Product stance

The harness is not a Kanban migration project. It is a replacement runtime for agent orchestration.

Correct framing:

- Kanban remains whatever upstream Hermes currently provides.
- Harness is a separate runtime for stateful agent work.
- Tony/Arcadia agent ops should use harness directly, not import or sync Kanban tasks.
- No effort should be spent building Kanban compatibility features unless Tony explicitly reopens that scope.

## Deep audit findings from current repo

### Existing Kanban is substantial but out of scope

The repo contains a mature Kanban implementation:

- `hermes_cli/kanban.py` — large CLI surface.
- `hermes_cli/kanban_db.py` — SQLite schema, WAL/CAS, claim locks, workers, boards, logs.
- `plugins/kanban/` — plugin/dashboard/worker integrations.
- Kanban tools and gateway/dispatcher integrations.
- Existing docs/tests across CLI, tools, plugins, and website.

This means accidental coupling is the main risk. The harness should not import Kanban modules, query Kanban DBs, mirror Kanban statuses, or depend on Kanban worker lifecycle.

### Lessons we can keep without dependency

General engineering lessons are still useful:

- shared coordination root for multi-profile agents
- atomic/concurrency discipline
- heartbeat and stale-run concepts
- bounded context and artifact/log hygiene
- modular CLI parser layout

But these should be implemented harness-natively. Do not reference Kanban schemas or runtime state in harness code.

### Failure modes to avoid

- card-per-recovery noise
- board columns as agent truth
- comment chains as evidence store
- subprocess worker lifecycle as the core actor model
- automatic recovery/follow-up creation
- compatibility layers that keep old noisy workflows alive

## Explicit non-goals

Do **not** implement:

```bash
hermes harness import-kanban ...
```

Do **not** implement:

- Kanban task import
- Kanban live sync
- Kanban status bridge
- Kanban comment bridge
- Kanban external refs on harness tasks
- Kanban compatibility dashboard
- Kanban recovery-card suppression logic inside harness
- tests that require Kanban DB fixtures for harness behavior

If a user has useful context in a Kanban card, they can paste/summarize it into a new harness task manually. That is enough for v0.

## Separation principles

1. `agent_runtime/` must not import from `hermes_cli.kanban*`, `plugins.kanban*`, or Kanban tools.
2. `hermes_cli/harness.py` must not call Kanban command handlers.
3. Harness state lives under `agent-runtime/`, not Kanban DB/workspace paths.
4. Harness events/proofs/incidents are source of truth for agent work.
5. Harness recovery creates incidents, not cards.
6. Existing Kanban behavior should remain untouched as an unrelated feature.
7. Any future migration/bridge needs a fresh explicit product decision.

## Harness-native future scale plan

If JSON storage becomes insufficient, migrate to a harness-native SQLite schema. Do not reuse the Kanban DB schema.

Migration trigger:

- `tasks/` exceeds 5000 JSON files
- `events.jsonl` exceeds 100 MB
- parallel ticks become required
- snapshot generation becomes too slow for Launcher/Unreal polling

Potential harness-native entities:

- `tasks`
- `task_stages`
- `agent_runs`
- `proofs`
- `incidents`
- `events`
- `plan_reviews`
- `approvals`

This is a storage upgrade, not a Kanban migration.

## Implementation tasks

Stage 8 implementation, if needed, is mostly guardrails:

1. Add dependency-boundary tests ensuring `agent_runtime/` imports no Kanban modules.
2. Add CLI parser tests ensuring `hermes harness` and `hermes kanban` command groups do not conflict.
3. Add store-path tests ensuring harness paths do not overlap Kanban paths.
4. Add docs/tests making clear that recovery creates incidents, not Kanban cards.
5. Remove any previous docs/plans that mention Kanban import or bridge.

## Tests

Required test files:

```text
tests/agent_runtime/test_no_kanban_dependency.py
tests/agent_runtime/test_store_path_isolation.py
tests/hermes_cli/test_harness_command_isolation.py
```

Test matrix:

- No `agent_runtime` module imports `hermes_cli.kanban`, `hermes_cli.kanban_db`, or `plugins.kanban`.
- Harness store root is distinct from any Kanban DB/workspace/log root.
- Harness incident creation does not create Kanban cards/comments.
- Harness CLI registration does not alter Kanban parser behavior.
- Search guard catches reintroduction of `import-kanban` command unless explicitly approved.

## Acceptance criteria

- Harness has zero runtime dependency on Kanban.
- Harness docs contain no active Kanban import/bridge roadmap.
- Existing Kanban users see no behavior change because harness does not touch Kanban.
- Tony/Arcadia agent workflows can ignore Kanban entirely.
- Future storage scale path is harness-native, not Kanban-schema-based.

## Risks / interventions

- **Accidental coupling:** add import-boundary tests before broad CLI integration.
- **Scope creep:** reject import/sync/bridge requests unless Tony explicitly reopens scope.
- **Old habits:** agents may try to create Kanban cards for recovery. Harness docs and persona prompts must say recovery is incidents/proofs inside one task.
- **Upstream compatibility anxiety:** leaving Kanban untouched is enough; no bridge is needed.
