# 19 — Deferred Debt Ledger

> **Status: living ledger, opened 2026-07-31.** Decision-ready refactor proposals
> and operator-ruling items produced by the post-upstream-sync staleness audit
> (Launcher Mission Control + hermes fork-owned surface). Per the
> weakness-escalation rule, deferred debts are recorded here so they are never
> silent. Executed work from the same audit is recorded in doc 18's
> executed-merge record, doc 16's follow-ups, and the s40-s43 (hermes) /
> s40-s52 (Launcher) removal-contract tests.

## Operator-ruling items (blocked on a decision, not on work)

1. ~~**role_envelopes / role_checklists store family (hermes).**~~ **RULED CUT
   and EXECUTED 2026-07-31** — `4e7aa0066` (S44). `role_envelopes.py` deleted
   whole (275 lines); `role_checklists.py` 420 -> 113 lines keeping only
   `validate_checklist_payload_structure` (live via
   `decision_contract_registry.validate_payload_keys`). Six events
   de-registered, `SURVIVING_EVENT_COUNT` 88 -> 82, contract hash
   `73ee514b…` -> `f655bd56…`. The two writer-less checkpoint EntityClass rows
   and eight orphaned path helpers went with them. Wave-3's
   "checklist_for_task_stage is live" ruling was confirmed transitively
   falsified and the S27 witness records the reversal.
2. ~~**Test-only whole modules (hermes).**~~ **RULED CUT and EXECUTED
   2026-07-31** — `be759935c` (S45). `budget_approval.py`,
   `context_requests.py`, `role_contracts.py` and `stage_intent.py` deleted
   whole (902 lines) together with their four dedicated test files (21 tests).
   Settled rule, so future waves stop re-deriving it: **a module whose entire
   importer set is the test written to exercise it is a closed loop, not
   covered code.** `role_envelopes.py` was listed here too and went with item 1.
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
8. **`role_envelope` runtime-config block is now a knob that governs nothing
   (hermes; opened by S44, 2026-07-31).** Cutting the store family left the
   whole config lane behind and it is NOT residue-shaped — it is a live wire
   telling an operator something false. Surface: `RoleEnvelopeConfig` (11
   fields, `runtime_config.py:53`), `RuntimeConfig.role_envelope`
   (`runtime_config.py:340`), `config.py:114/173/535-551`
   (`_role_envelope_config`), and five `migrations.py:82-86` validators that
   still range-check `max_same_session_continuations`,
   `max_no_progress_repeats`, `max_fix_envelopes_per_stage`,
   `max_checklist_items_rendered`, `max_foreign_checklist_summaries`. **It ships
   on the live snapshot wire**: `harness snapshot --json` on alice emits
   `runtime_config.role_envelope` with `enabled: true` — an operator once
   deliberately turned this on (the default is `False`), and nothing has
   implemented it since S44. Deliberately NOT swept on this wave's authority:
   removal changes the snapshot contract and needs Launcher lockstep, so it is
   S9/S10-shaped like item 5, not a narrow cut. Same precedent as S28 recording
   `scope_control.untriaged_issue_discoveries` rather than reaching outside its
   scope. Whoever takes item 5 (`workspaces[].goals`) should take this with it —
   both are constant-by-construction fields on the same wire.

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
- ~~**B-4 read-model serve path** / **B-5 dead parity warnings**.~~ **EXECUTED
  2026-07-31** — `76504fd84`. B-4: `resolve_snapshot_frame(prefer_cache=...)`
  returns `(frame, FrameSource)` over a StrEnum {`built`, `cache`,
  `cache_miss_rebuilt`}, stamped onto `parity.frame_source`; the silent `{}`
  serve is gone (`render_snapshot()` now returns `None` for "no cached frame"
  and the resolver degrades to the built frame). `snapshot_watermark()` is the
  single derivation — `write_snapshot`'s `{}` fallback (which recorded a frame
  as caught-up-at-offset-0) and the projector's `events_watermark()` fallback
  were the same question answered two ways. Blob-vs-rows RULED **blob**: the 27
  duplicate per-section `projections_misc` rows are dropped and
  `read_projection` slices the blob. Measured on live alice: 28 rows /
  1,628,708 payload bytes, of which **543,247 (33%) were duplicates**; the
  `READ_MODEL_SCHEMA_VERSION` 2 -> 3 clear + VACUUM took the file **4,194,304 ->
  1,773,568 bytes**. `ProjectorResult.changed` / `.stale_sections` (constants
  dressed as findings) deleted, and `apply_pending` counts the pending tail
  instead of materializing it. B-5: both summary-keyed warnings, the
  `open_incident_warning_threshold` knob, and the test that was the only
  producer of `open_incidents` all cut; `SnapshotSummary` declares the block's
  field set and `tests/agent_runtime/test_parity_warning_catalog.py` gates every
  remaining warning code as executably producible.
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
- **F-9 dead `open_incident_budget_exceeded` alert (opened by B-5,
  2026-07-31).** `mission_control_snapshot.dart:994` branches the snapshot-alert
  label on `warningCodes.contains('open_incident_budget_exceeded')`. The hermes
  producer is now deleted (it had been unreachable since contract 45 anyway), so
  the branch is permanently false and the alert always renders the generic
  "parity warnings N" arm. Launcher-side, one-line cut; deliberately NOT taken
  on this wave's authority since it is the other repo.
  `mission_control_bridge.dart:2862` only *mentions* the sibling
  `open_tasks_without_task_rows` in a comment — reword when F-9 is taken.
