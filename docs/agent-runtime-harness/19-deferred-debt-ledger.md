# 19 — Deferred Debt Ledger

> **Status: living ledger, opened 2026-07-31.** Decision-ready refactor proposals
> and operator-ruling items produced by the post-upstream-sync staleness audit
> (Launcher Mission Control + hermes fork-owned surface). Per the
> weakness-escalation rule, deferred debts are recorded here so they are never
> silent. Executed work from the same audit is recorded in doc 18's
> executed-merge record, doc 16's follow-ups, and the s40-s43 (hermes) /
> s40-s52 (Launcher) removal-contract tests.

## Operator-ruling items (blocked on a decision, not on work)

1. **role_envelopes / role_checklists store family (hermes).** Production-
   caller-free; cutting retires six events (role_envelope.opened/continued/
   paused/closed, role_checklist.created/item_updated), moving
   SURVIVING_EVENT_COUNT 88 -> 82 and the contract hash. Wave-3's
   "checklist_for_task_stage is live" ruling is transitively falsified (its
   only justification was the role_envelopes import). The runtime DIRECTORIES
   of this family were already archived as writer-less on 2026-07-30.
2. **Test-only whole modules (hermes):** budget_approval.py,
   context_requests.py, role_contracts.py, most of stage_intent.py,
   role_envelopes.py have zero production importers - ruling needed on whether
   test-only-anchored counts as keep.
3. **Launcher goal-detail family (A21 remainder).** MissionGoalDetail body,
   MissionIntervention, proof-gate/flow-timeline/level-state/topology classes,
   _agentTopologyRuntimeGraphProjection, _missionActorsFromTopology, and the
   bridge goal-mapping region still have live compile-time consumers in six
   files (instance picker x9, secondary_drawers:177, shared_widgets:512,
   chat adapter :847/:867, selection policy :333, page :3770). Needs ONE
   follow-up pass owning all of them together; cutting one side strands a
   non-compiling tree.
4. **CLI entity rows gain redaction/caps (hermes B-2).** Consolidating the five
   duplicated entity-row projections onto snapshot-grade builders changes CLI
   output (cards gain masking/truncation) - a deliberate output change needing
   a ruling. The realm-sync lie and workspace-show crash halves were already
   fixed narrowly (a21ab1a2a).
5. **workspaces[].goals wire field (hermes B-3).** The tasks = [] seed
   (snapshot.py:332) feeds three projections that can only emit constants;
   removal is a contract bump + Launcher lockstep, S9/S10-shaped.
6. **gateway/platforms/base.py:1407** bare expanduser("~") vs sibling's
   $HOME-preferring resolution - aligning widens a denial carve-out.
7. **Packaging:** psutil/fire declared but absent on the ambient test
   interpreter; markdown used by the matrix adapter but declared nowhere.

## Proposal ledger (decision-ready; full text in the 2026-07-31 audit reports)

Hermes fork-owned:
- **P0 exec-namespace guard test** for harness_parts (the live NameError class;
  the :440 defect itself is fixed). Companion: the module-ization proposal
  (123 re-exported names vs 7 real harness-local helpers; unlocks ruff F821).
- **P1 turn-outcome vocabulary** (execution_state 26 sites / error_kind 35
  sites) -> owned StrEnums + classify_turn_failure; one-liner available at
  persona_commands.py:3186 (OPERATOR_RESOLVABLE_TURN_STATES).
- **P2 mission-chat turn envelope**: plan_turn (pure) -> commit_turn (sole
  writer, holds the lease); retires the five pre-lease durable writes and the
  args._* phase-state mutation; folds P6's silent-swallow cluster
  (worst: the IDLE-return + default-session commit at :2696-2703 inside
  except: pass).
- **P3 frozen-HERMES_HOME ratchet**: probe reads not declarations; collapse
  hermes_state dual resolvers (fork side keeps _resolve_default_db_path);
  finish the skills_sync SKILLS_DIR migration (12 sites).
- **P4 PathForm value object** + 4x4 characterization test over the four path
  translators (fork-side early-warning; upstream-PR candidate #1).
- **P5 launcher_qa mcp_servers template**: canonical template constant + new
  issue code on the existing machine_roots.mcp_server_issues() ->
  profile_readiness lane. NOTE: drift already exists (variant A with
  STAGEC_LAUNCH_HELPER: alice/base/neko/unbounded; variant B without:
  backend-dev/gpt-launcher/launcher-dev/launcher-qa/qa) - doc-18's
  "field-identical to launcher-dev" guard would canonicalize the WRONG variant.
- **B-4 read-model serve path**: snapshot_frame resolver with typed source,
  fix the silent {} on cache miss, unify the two watermark fallbacks, decide
  blob-vs-rows (1.5x duplication); **B-5** dead parity warnings
  (open_tasks/open_incidents keys can never exist) + typed summary object.
- **Wire-test idiom**: the relay regression class (producer and consumer pinned,
  wire between them never asserted) - extend the "does the chokepoint actually
  invoke the seam" AST tests to the other persona_commands chokepoints.

Launcher Mission Control (F-1..F-8, full text in the audit report):
- F-1 selection-state chokepoint adoption (~25 bypass sites, reconciliation in
  build()); F-2 delete the second instance resolver (_agentInstanceById);
  F-3 extract MissionTranscriptProjector (the pinning test currently mirrors
  instead of exercising); F-4 MissionTurnPhase enum (4 divergent boolean
  cascades); F-5 HermesVisibilitySource honest-degradation enum; F-6 one
  PM->Neko copy authority; F-7 Capped<T> + "+N more" (incl. the office
  selected-goal eviction); F-8 MissionOfficeAuthoringPolicy.
  Sequencing: F-4 -> F-3 -> F-2 -> F-1; F-5/F-6/F-7 independent; F-8 with F-2.
