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

9. **`Projector.apply_pending` has no production caller (hermes; opened by B-4,
   2026-07-31).** Working in the projector for B-4 surfaced that the entire
   INCREMENTAL projection lane — `acquire_lease`, the watermark diff, the
   pending-event count, `ProjectorResult`'s offset/timing fields — is reached
   only from five test call sites. Repo-wide, the sole production entry to the
   projector is `full_rebuild()` from `_cmd_rebuild_read_model`
   (`runtime_commands.py:437`). The RD3 design (doc 05:348) specified a "ticker
   chokepoint" that would call `apply_pending()` when the lease is held; it was
   never wired. So the tests that assert an incremental SLO
   (`test_apply_pending_is_o_delta_on_rd0_fixture`,
   `test_synthetic_incremental_apply_within_rd3_slo`) are measuring a lane
   nothing runs — the same closed-loop shape as ledger item 2, one level down
   (a METHOD whose whole importer set is its tests, not a module). Compounding
   it: `read_model.enabled` is `False` on the live alice root, so even
   `full_rebuild` only runs when an operator types the verb, and the serve path
   reports `frame_source=built` every time. The ruling needed is whether the
   read-model lane is being finished (wire the ticker) or retired (delete
   `apply_pending` + the lease + the SLO tests and keep `full_rebuild` as an
   operator-invoked cache warmer). NOT taken on B-4's authority: B-4 was
   scoped to the two constant result fields, and deleting a lane is a
   direction call, not a cleanup.

## Proposal ledger (decision-ready; full text in the 2026-07-31 audit reports)

Hermes fork-owned:
- ~~**P0 exec-namespace guard test + explicit imports.**~~ **EXECUTED
  2026-08-01** — `79a7c6542` (symtable-based namespace guard, identity-based
  collision check, self-cleaning ledger), `a07b6c6dd` (`harness_support.py`:
  the 7 spec'd helpers pulled a 20-member dependency closure), `e887cdf26`
  (per-part import headers, 158 bound names; ghost `Callable` retired —
  `runtime_commands.Task` was already comment-only; `__file__` trap →
  `harness_repo_root()`), `21f7b9f3a` (F821 on for agent_runtime/ +
  harness_parts/, 782 -> 0; found + fixed a real never-fired NameError:
  `persona_chat_history.py` `_default_event_log`). Parts remain exec'd; CLI
  shape proven byte-identical via full argparse dump diff. RESIDUALS:
  harness.py keeps 62 ignored F821 (the reverse direction — retired only by
  full module conversion, the still-open companion proposal);
  `PERSONA_CHAT_SESSION_SOURCE` still defined twice in agent_runtime (3 -> 2);
  ~1,153 upstream F821 hits ignored as upstream-PR candidates, incl. a genuine
  `tools/patch_parser.py:345` undefined `PatchResult`.
- ~~**P1 turn-outcome vocabulary.**~~ **EXECUTED 2026-07-31** — `1f64833c4`.
  `agent_runtime/mission_chat_outcome.py`: ExecutionState (7) / ChatErrorKind
  (19) StrEnums, classify_turn_failure, import-time coverage guard; literal
  sites replaced; wire byte-identical.
- ~~**P2 mission-chat turn envelope.**~~ **EXECUTED 2026-07-31** — `68ae37a29`.
  plan (`_cmd_mission_chat_message`) -> `with persona_chat_root_lease` ->
  `_mission_chat_commit_turn`; P6 silent-swallow cluster surfaced as typed
  `finalization_warnings` (additive). Known deviations (pinned by test):
  ERROR_EXIT_CODES untouched; mint precedes the lease by necessity.
- ~~**P3 frozen-HERMES_HOME ratchet (fork slice).**~~ **EXECUTED 2026-08-01** —
  `878115fa1` + `afaf087b6`. Runtime probe (nonce-named tmpdir HERMES_HOME,
  subprocess import, file-not-stdout results) replaces the column-0 regex.
  The ledger GREW 29 -> 51 names — the regex only saw first-hop freezes
  (derived constants like `JOBS_FILE = CRON_DIR/...` were invisible); this is
  unmeasured debt now measured, every entry reasoned, stale entries fail.
  2 UNPROBED on this host (fire/termios), carried with reasons.
  `read_model._default_db_path` -> `_read_model_db_path` (grep-poison name
  retired). Upstream slice (hermes_state dual resolvers, skills_sync
  SKILLS_DIR) untouched — stays an upstream-PR candidate.
- ~~**P4-lite path-form characterization.**~~ **EXECUTED 2026-07-31** —
  `3ac7bb6ba`. 49 pins over the four translators incl. the backslash-pattern
  and verbatim-return edges; purity guard. The PathForm value object itself
  (production change) remains upstream-PR candidate #1.
- ~~**P5 launcher_qa mcp_servers template.**~~ **EXECUTED 2026-07-31** —
  `6190e4d9d`. `CANONICAL_LAUNCHER_QA_MCP_SERVER` (variant A ruled canonical),
  `mcp_server_template_diffs`, advisory `mcp_server_template_drift` issue
  (opt-in via include_template_drift), data test listing variant-B's missing
  env var as expected-drift with per-profile yaml patches in the failure
  message. OPEN OPERATOR DECISIONS: flip advisory -> blocking; apply the five
  variant-B config patches (live action).
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

Launcher Mission Control (F-1..F-8):
- ~~F-1..F-8~~ **ALL EXECUTED 2026-07-31/08-01**, on launcher origin/main
  through `e88efde5`: F-4 `136e8992` (MissionTurnPhase + named predicates —
  NOTE the audit's subset guess was WRONG and landed inverted:
  isTransientHistoryFrame ⊇ hasUndeliveredWork, because latching
  runtimeCallCompleted into the dispose guard would leak it permanently;
  characterization table carries the evidence), F-3 `d5f544f8` (pure
  MissionTranscriptProjector, −989 panel lines, required-key row factories,
  cache key = value object + contentDigest — `revision` deliberately NOT in
  the key), F-5 `e82f202b` (HermesVisibilitySource + honest degradation
  notes), F-6 `5465ee06` (MissionCopyNormalization.nekoFirst; bridge rewriter
  was already dead via s49 — absorbed as a strict-superset history test),
  F-7 `011e4fff` (Capped<T>/MoreIndicator over all 7 cap sites; office
  selected-goal eviction fixed via ofIncluding), F-8 `738babb4`
  (MissionOfficeAuthoringPolicy + typed scene mutation + hidden-goal pill),
  F-2 `ff368c21` (second resolver deleted; widenToSnapshotRoster opt-in at
  the four ex-exact-guard sites; grep gate scoped to the page library),
  F-1 `e88efde5` (MissionSelectionStore, ONE typed write path,
  compiler-enforced — fields have no setters).
  LAUNCHER RESIDUALS (opened by execution):
  - **F-1 reducer relocation**: reconciliation still runs in build() (as typed
    transitions); the ref.listen relocation + Riverpod Notifier promotion are
    ONE follow-up change (Riverpod asserts on mutation-in-build).
  - **F-2-adjacent `persona:` unwrap**: still hand-rolled at
    mission_chat_directory.dart:611/:633 and mission_control_snapshot.dart:2228
    — same class the F-2 authority retired; fold onto
    missionAgentIdentityAliases.
  - **F-3 cache key is O(messages) in time** (contentDigest); O(1) needs an
    adapter-minted conversation revision (wire change).
  - **F-7/F-8 office**: goal-overflow pill live-invisible while
    goalDioramaEnabled:false; MissionOfficeSceneMutation `dropped` lane is
    debug-assert only; MissionOfficeRenderProbe lacks a goals-hidden field.
- **F-9 dead `open_incident_budget_exceeded` alert (opened by B-5,
  2026-07-31).** `mission_control_snapshot.dart:994` branches the snapshot-alert
  label on `warningCodes.contains('open_incident_budget_exceeded')`. The hermes
  producer is now deleted (it had been unreachable since contract 45 anyway), so
  the branch is permanently false and the alert always renders the generic
  "parity warnings N" arm. Launcher-side, one-line cut; deliberately NOT taken
  on this wave's authority since it is the other repo.
  `mission_control_bridge.dart:2862` only *mentions* the sibling
  `open_tasks_without_task_rows` in a comment — reword when F-9 is taken.
