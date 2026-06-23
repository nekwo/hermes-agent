# Agent Runtime Harness — Staged Implementation Plan

> Purpose: replace flaky Kanban-as-agent-manager behavior with a lean, reliable, Hermes-native ticking agent harness. This is intentionally *not* a wrapper system: GPT personas are first-class Hermes actors invoked through Hermes' model/tool runtime, while the harness owns state, proof, transitions, and scheduling.

## Product stance

Build the smallest reliable core first:

```text
Mission state machine + Agent state + Mission Daemon + GPT persona runtime + Proof gates
```

Do **not** start with Launcher UI, Unreal UI, Postgres, Centrifugo, or Claude/CLI wrappers. Those are consumers/adapters later.

## Current-repo audit anchors

This plan was written after auditing the local Hermes checkout at the repository root:

- `AGENTS.md` documents the load-bearing paths: `run_agent.py`, `agent/conversation_loop.py`, `agent/agent_init.py`, `hermes_cli/main.py`, `tools/`, `gateway/`, `cron/`, `plugins/kanban/`, and tests.
- `run_agent.py` exposes `AIAgent.chat()` and `AIAgent.run_conversation()` as the existing model/tool runtime entrypoint.
- `agent/conversation_loop.py` owns the synchronous tool-calling loop, task-id isolation, session DB hydration, callbacks, and interrupt/steer behavior.
- `agent/agent_init.py` already chooses provider API modes, including direct OpenAI/GPT Responses-style modes (`codex_responses`) and chat-completions fallback.
- `tools/delegate_tool.py` already proves Hermes can spawn isolated model-backed subagents, but it is blocking and conversation-oriented rather than mission/task-state oriented.
- `hermes_cli/kanban.py` and `hermes_cli/kanban_db.py` provide a large SQLite Kanban system with CAS claims, boards, workers, logs, and statuses. We should mine its good persistence/concurrency lessons, but avoid reusing the board mental model.
- `docs/` already exists; this staged design lives in `docs/agent-runtime-harness/`.

## Stage list

1. [State Machine Core](01-state-machine-core.md)
2. [GPT Persona Runtime](02-gpt-persona-runtime.md)
3. [Task Planning + Stage Audit Loop](03-planning-and-stage-audit.md)
4. [QA Proof + Visual Evidence Rules](04-qa-proof-and-visual-evidence.md)
5. [Ticker, Daemon, and Recovery](05-ticker-daemon-and-recovery.md)
6. [Hermes CLI/Profile Integration](06-hermes-cli-profile-integration.md)
7. [Launcher + Unreal Observability Contract](07-launcher-unreal-observability.md)
8. [Kanban Migration / Compatibility Strategy](08-kanban-migration-strategy.md)
9. [Profile-Bound Personas, Souls, and Skills](09-profile-bound-personas-souls-and-skills.md)
10. [Mission Daemon and AAA State Machine](10-mission-daemon-state-machine.md)
11. [LLM State Observability Deep Audit and Fix Stages](11-llm-state-observability-audit-and-fix-stages.md)
12. [Unrelated Issue Forking and Scope Control](12-unrelated-issue-forking-and-scope-control.md)
13. [Run Progress Streaming and Worker Handoff Contracts](13-run-progress-and-worker-handoff-contracts.md)
14. [Hermes-Native Agent Runner and Mission Control Hardening](14-hermes-native-agent-runner-and-mission-control-hardening.md)
15. [Persona Agency, Proof Handoff, and Stage Ownership](15-persona-agency-proof-handoff.md)
16. [Deterministic Proof Collection and Live Persona Smoke](16-deterministic-proof-collection-and-live-smoke.md)
17. [QA Context and Stage Materialization](17-qa-context-and-stage-materialization.md)
18. [Goals MVP Blocked QA Recovery](18-goals-mvp-blocked-qa-recovery.md)
19. [Mission Visibility, Proof Runner, and Provider Resilience](19-mission-visibility-proof-runner-provider-resilience.md)
20. [Runtime Liveness and Execution Truth](20-runtime-liveness-and-execution-truth.md)
21. [Block Decisions Require Log Evidence](21-block-decisions-require-log-evidence.md)
24. [Live Tick Operator Controls and Runaway Guards](24-live-tick-operator-controls-and-runaway-guards.md)
25. [Dev Agent Work Log Quality](25-dev-agent-work-log-quality.md)
26. [Deterministic Proof Handoff and Live Run Budgets](26-deterministic-proof-handoff-and-live-run-budgets.md)
27. [Live Run Budget Enforcement and Provider Interrupt](27-live-run-budget-enforcement-and-provider-interrupt.md)
28. [Budget Approval Same-Session Continuation](28-budget-approval-same-session-continuation.md)
29. [Neko Mission Lead Autonomous Handoff](29-neko-mission-lead-autonomous-handoff.md)
30. [Autonomous Run Until Settled](30-autonomous-run-until-settled.md)
31. [Repo-Grounded Dev Sessions](31-repo-grounded-dev-sessions.md)
32. [Full Polish Repo Proof Observability](32-full-polish-repo-proof-observability.md)
33. [Dev Work Event Instrumentation](33-dev-work-event-instrumentation.md)
34. [Safe Agent Thinking Process Observability](34-safe-agent-thinking-process-observability.md)
35. [Specialist Dev Agent Stage 0](35-specialist-dev-agent-stage0-autonomous-capability.md)
36. [Launcher Dev Alice-Parity Hardening](36-launcher-dev-alice-parity-hardening.md)
37. [Swarm-Ready Specialist Agent Model](37-swarm-ready-specialist-agent-model.md)
38. [QA Proof-First Intelligence Hardening](38-qa-proof-first-intelligence-hardening.md)
39. [Runtime Environment and CLI Finalization](39-runtime-environment-and-cli-finalization.md)
40. [Cross-Stack Contract Smoke Neko Release Gap](40-cross-stack-contract-smoke-neko-release-gap.md)
41. [Archive-Ready Live Goal Gaps](41-archive-ready-live-goal-gaps.md)
42. [Mission Control Archive Button Live Bridge](42-mission-control-archive-button-live-bridge.md)
43. [Mission Control Independent Investigation Brief](43-mission-control-independent-investigation-brief.md)
44. [Merged Mission Control AAA / One-Shot Autonomy Plan](44-merged-mission-control-aaa-plan.md) — single source-of-truth plan (folds in the prior Claude/Codex investigations + plans, now removed)
45. [Stage 44 Implementation Dedup Audit](45-stage-44-implementation-dedup-audit.md) — current-code implementation readiness map to avoid double-implementing existing Harness/Launcher behavior
46. [Proof-Command Persona Self-Healing Implementation](46-proof-command-persona-self-healing-implementation.md)
47. [AAA Burn-In, No-Freeze Hardening, and Agent Behavior Certification](47-aaa-burn-in-no-freeze-certification.md)
48. [Worker Session Kernel and Normal Harness Execution](48-worker-session-kernel-normal-harness.md)
49. [Canonical Decision Contract Registry and HUD Schema Generation](49-canonical-decision-contract-registry.md)
50. [Agent Self-Test and Gated QA Normal Flow](50-agent-self-test-gated-qa-normal-flow.md)
51. [Typed Mission Plan Simplification](51-typed-mission-plan-simplification.md)
52. [One Worker Per Role Continuous Envelope](52-one-worker-per-role-continuous-envelope.md)
53. [In-Process Goal Runner Controller](53-in-process-goal-runner-controller.md)
54. [Operational Stages With Role-Local Task Lists](54-operational-stages-role-local-task-lists.md)
55. [Persona Operations Diagnostics](55-persona-operations-diagnostics.md)
56. [Persona Instance Assignment Runtime](56-persona-instance-assignment-runtime.md)
57. [Simplified State Machine and Agent Contract](57-simplified-state-machine-agent-contract.md)
58. [First-Class Harness Packet Protocol](58-first-class-harness-packet-protocol.md)
59. [HUD / Skill Contract Split](59-hud-skill-contract-split.md)
60. [Foreground Goal Runtime Cleanup and Scheduler](60-foreground-goal-runtime-cleanup-and-scheduler.md)
61. [Smooth Unattended Execution and Swarm Readiness](61-smooth-unattended-execution-and-swarm-readiness.md)
62. [Production Swarm Operations and Fleet Readiness](62-production-swarm-operations-and-fleet-readiness.md)
63. [First-Class Persona Instances in Mission Control](63-first-class-persona-instances.md)
64. [Callable Harness Capabilities](64-callable-harness-capabilities.md)
64b. [AAA Persona Instance Chat History Contract](64-aaa-persona-instance-chat-history-contract.md)
65. [Claude-Grade Agent Harness Runtime](65-claude-grade-agent-harness-runtime.md)
70. [Mission Control Cockpit Contract](70-mission-control-cockpit-contract.md)
71. [Mission Control Agent Chat Streaming Deep Audit](71-mission-control-agent-chat-streaming-audit.md)
72. [Streaming Operator Chat Implementation](72-operator-chat-streaming-implementation.md)
73. [Mission Control Agent Console Enterprise-Grade Upgrade](73-mission-control-agent-console-enterprise-grade-stages.md)
74. [Open Persona Runtime Blueprint Graph](74-open-persona-runtime-blueprint-graph.md)
75. [Persona Library and Console Interactibility](75-persona-library-and-console-interactibility.md) — audit-backed execution path for Stage 74: ship the `available_personas` contract gap + console/label/glow asks first, plus a Mission Office UI remaster (dock layout, agent cards, library shelf); defer the graph editor and taskless runtime loops
76. [Unified Template / Instance / Chat / Goal Model](76-unified-template-instance-chat-goal-model.md) — the locked entity architecture (2026-06-22): Template → durable Level Instance (placement) → swappable Chat → Goal/Task owned by the chat; goals scale as Neko operator chats (N goals = N chats); tasks are an advisory HUD, proof is conductor-read escalation not a per-task crash gate. Supersedes Stage 74's object model and the mid-75 "instance↔chat 1:1" note

## MVP success criteria

The first end-to-end vertical slice is successful when this works locally:

```text
hermes harness task create "Small repo-safe task"
hermes harness tick
  -> PM persona fleshes task into acceptance criteria
hermes harness tick
  -> Dev persona audits and writes staged plan
hermes harness tick
  -> QA persona reviews plan and either approves or requests corrections
```

Then the next slice adds patch/test/commit execution and QA proof.

## AAA non-negotiables

- State transitions are explicit and tested.
- The harness, not the model, owns proof validation.
- Dev claims can be backed by commits/diff/test output.
- QA claims for UI/game/visual work require passed tests plus screenshot or video evidence.
- Process failures are incidents/retryable run failures, not product task failures.
- No duplicate recovery spam; recover in place where possible.
- No Claude/CLI wrappers in the core design.
