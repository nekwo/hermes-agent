# Agent Runtime Harness — Staged Implementation Plan

> Purpose: replace flaky Kanban-as-agent-manager behavior with a lean, reliable,
> Hermes-native ticking agent harness. GPT personas are first-class Hermes actors
> invoked through Hermes' model/tool runtime; the harness owns state, proof,
> transitions, and scheduling. Not a wrapper system.

## Canonical docs (read these)

The 70+ exploratory stage docs were folded down on 2026-06-25 into three canonical
documents after the codebase was rewritten several times. The journey is in git history;
these three are the live truth.

1. **[01 — Architecture: Entities + Agent Graph](01-architecture.md)** — *LOCKED.* The
   entity model (`Template → durable Level Instance → swappable Chat → Goal/Task`),
   tasks-as-HUD, and the consolidated agent graph (one node = one agent; the persona
   pipeline is retired). **What the system is.**

2. **[02 — Blueprint Goal-Flow Engine](02-execution-engine.md)** — the execution engine:
   a stable graph of swappable agent bindings (slots/bindings/edges/proof gates), 1 to N
   agents; the slot↔instance/chat/goal binding; dynamic stage-shaped HUD/skills/proof;
   the coordinator permission scope. **How a goal runs.** Implementation reference lives
   beside the code at `agent_runtime/docs/blueprint_goal_flow_stages.md`.

3. **[03 — Retirement Ledger](03-retirement-ledger.md)** — the single grep-gated
   worklist for deleting the legacy execution path (the `TaskState` ladder remnants, the
   `has_typed_plan` dual-orchestrator fork, role-shaped HUD/skill map, launcher
   cross-stack special cases). **What gets deleted.**

4. **[04 — Decision / HUD Simplification: Target Model](04-decision-hud-simplification-map.md)** —
   agents work **unbounded** (edit + run tests with native tools, no per-op decision); the
   **harness reads the work** (git diff + tool trace + its own gate re-run) instead of making
   agents fill a validated form, and surfaces it in an operator **HUD dashboard**. Collapses
   ~19 decision types + the `delivery`/`work_status` packet to ~5 coordination signals
   (hand off · block · escalate · neko scope/route · qa verdict). Kills the
   `model_invalid_output` failure class. **What the decision contract simplifies to.**

5. **[05 — Runtime Data: Enterprise-Grade Storage & Access](05-runtime-data-enterprise-storage.md)** —
   *implementation-ready.* Staged spec (RD0–RD8) with exact modules, SQLite DDL, config
   keys, test files, proof commands, rollback paths, and per-stage handoff prompts:
   monolithic `snapshot.json` + poll → transactional `read_model.db` with an incremental
   lease-holding projector, NDJSON change feed, per-lane consumer degradation,
   deterministic fail-loud runtime-root resolution, event-log segmentation +
   backup/restore drills, and hard CI perf/concurrency/crash gates. **How the runtime's
   data layer becomes production-grade.**

6. **[06 — Recursive Agent-Supervised Execution](06-recursive-agent-supervised-execution.md)** —
   *implemented + verified (v3.1) on branch `recursive-agent-supervised-execution`;
   production recursive lanes remain burn-in-ledger-gated (see doc §6).* The target execution model: the harness stops *ticking*
   worker turns and becomes the incorruptible substrate (gate every handoff, watchdog every
   level, hierarchical budget, event-source everything); agents become recursive supervisors —
   each node schedules/watches only its DIRECT children and reports a distilled summary up
   (builds on doc-04 S2 steering + S3 continuity). Stages AS0–AS7: AS0 active liveness watchdog
   (kills indefinite hangs, ships first; runs on a daemon thread and extends the burn-in-only
   `no_freeze_monitor`) → honest child status events → recursive supervision → per-boundary
   gate → hierarchical budget → real concurrent lanes (store-lock audit + doc-04 H1 worktree
   isolation; the doc-05 read-model dependency is already shipped) → verified deploy → CI
   gates. **How the scheduler becomes distributed AI judgment instead of a serial tick loop.**

7. **[08 — Root Node + Self-Looped Sub-Agents](08-blueprint-as-script-collapse.md)** —
   *implementation-ready (v4); staged plan, not yet implemented.* The over-engineering
   correction, final shape: **every node is a standard self-looped Hermes agent
   (customizable but simple: prompt + HUD + toolset + workdir), and the root node runs the
   goal by spawning and steering sub-agent nodes** — the same shape as Hermes'
   `delegate_task`. The harness is substrate only (run loops, resolve workdirs, capture
   evidence server-side, kill hangs, persist for Mission Control); **no judgment in
   Python** — the root's model judges children's work from harness-captured evidence and
   at worst steers them. Two root-only service tools (`run_node`/`steer_node`), stages
   N0–N3, deletes the ~13.7k-line decision-contract/gate/incident/adjudication tower at
   N3 after burn-in, and makes the 2026-07-03 self-drive bug class unrepresentable at N1.

## Product stance

Build the smallest reliable core first:

```text
Mission state machine + Agent state + Mission Daemon + GPT persona runtime + Proof gates
```

Do **not** start with Launcher UI, Unreal UI, Postgres, Centrifugo, or Claude/CLI
wrappers. Those are consumers/adapters.

## MVP success criteria

The first end-to-end vertical slice works locally when:

```text
hermes harness blueprint run one_agent_smoke --goal "..." --bind builder=profile:gpt-launcher
  -> agent receives the objective, acts, returns evidence
  -> result + stage outcome visible in Mission Control snapshot
hermes harness blueprint run two_agent_build_verify --goal "..." \
    --bind builder=profile:gpt-launcher --bind verifier=persona:qa
  -> builder works, verifier checks, failed verify routes back (bounded), passed -> done
```

## AAA non-negotiables

See [02 — AAA non-negotiables](02-execution-engine.md#aaa-non-negotiables) for the full,
current statement (proof is harness-owned evidence the goal owner *adjudicates*; an unmet
check escalates, it does not dead-end). In short:

- State transitions are explicit and tested.
- The harness, not the model, owns proof validation; the goal owner adjudicates.
- Dev claims are backed by commits/diff/test output; visual QA needs passed tests + a
  screenshot or video.
- Process failures are incidents/retryable run failures, not product task failures.
- No duplicate recovery spam; no Claude/CLI wrappers in the core design.

---

*Historical note:* Stages 01–77 (the exploratory journey, including the rewrites) were
removed from the working tree on 2026-06-25 and folded into the three docs above. To read
the original staging, check out a commit before that date, e.g.
`git show baf366cb4:docs/agent-runtime-harness/76-unified-template-instance-chat-goal-model.md`.
