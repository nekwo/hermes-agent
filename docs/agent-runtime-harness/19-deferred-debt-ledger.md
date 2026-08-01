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
3. ~~**Launcher goal-detail family (A21 remainder).**~~ **RULED "run the pass"
   and EXECUTED 2026-07-31** — Launcher s47–s49 knot pass (pushed via
   `c6d256c7`, −2,099 lib lines): MissionGoalDetail body, MissionIntervention,
   proof-gate/flow-timeline/level-state/topology classes,
   _agentTopologyRuntimeGraphProjection, _missionActorsFromTopology, the
   bridge goal-mapping region, and all six consumer files taken in ONE pass
   (the strand-a-non-compiling-tree hazard this item existed to flag).
   Removal-contract groups s47–s49 in dead_symbol_removal_contract_test.dart.
   (This strike was recorded late — the pass landed before items 5/8/9 were
   executed; nothing else about the entry changed.)
4. ~~**CLI entity rows gain redaction/caps (hermes B-2).**~~ **RULED EXECUTE and
   EXECUTED 2026-08-01 — `71a96b517` (S48).** Every `hermes harness` entity row
   is now a RE-KEY of the snapshot builder that already owned the question; the
   five hand-written twins are gone. The set was re-derived from the tree, not
   from the audit's line numbers (S46/S47 and `e887cdf26` had reshaped these
   files): `harness.py::_workspace_row` -> `_workspace_summary`,
   `harness.py::_realm_row` -> `_realm_summary`, `board.py::_board_row` ->
   `board_summary_row`, `board.py::_card_row` -> `_board_card_row`,
   `office.py::_office_actor_row` -> `_office_actor_summary_row`. Two more went
   with them: `office.py::_office_surface_row` (the container that held the
   uncapped actor list) -> `office_summary_row`, and `_office_item_row` deleted
   outright — it re-declared, key for key, the scene-item block the actor
   builder already projects.

   **What actually leaked.** Only the board/office tier: the CLI printed
   `card.title` / `card.description` / `card.checklist[].text` RAW while the
   wire masked all three, printed descriptions uncapped (the store accepts
   4,000 characters; the projection bound is 2,048), and printed EVERY card /
   EVERY actor on `--full` while the wire bounded them at 500 / 200. The
   workspace and realm rows leaked nothing — what they carried was
   DUPLICATION, and duplication is what shipped both prior defects (the `tasks`
   NameError and the `"in_sync"` lie, `a21ab1a2a`).

   **The visible output change.** Masking is VALUE-LEVEL and IN PLACE:
   `"Rotate api_key: sk-live-… before Friday"` renders `"Rotate api_key:
   [redacted] before Friday"` — no field is blanked, dropped or emptied for
   containing a secret, the prose on both sides survives, and two secrets in one
   string are masked independently. Caps are ACCOUNTED, never silent: the full
   card row gains `description_truncated`, `board show --full` gains
   `cards_truncated`, `office show --full` gains `actors_truncated`. Everything
   else is byte-identical, including `--output table` / `--quiet` / `--fields`
   (timestamps stay `datetime`; `emit_json` -> `to_jsonable` is this lane's
   serialization authority). Live-verified on alice: `workspace show`,
   `realm show|list`, `board list`, `board show --full`, `office show --full`,
   and a `board card add` carrying a secret, which rendered the mask in place.

   **No unmask flag was invented.** `_board_card_row` has no unredacted seam, so
   none was used and none was added — see the residual below.

   **The two new snapshot functions are extractions, not a second lane.**
   `board_summary_row` / `office_summary_row` are the `_boards_summary` /
   `_offices_summary` loop bodies lifted out unchanged; the loops route through
   them (gated by test), and the live `harness snapshot --json` `boards` and
   `offices` sections are byte-identical to the pre-change frame.

   Pins: `tests/hermes_cli/test_entity_row_characterization.py` (22 — masked in
   place with surrounding text intact, cap marker present when capped,
   non-secret rows byte-identical, each row's key set frozen, and a sentinel
   proving each row really routes through its builder) and
   `tests/agent_runtime/test_s48_cli_entity_row_consolidation.py` (21 — removal
   contract, delegation contract, and a self-check that the gate's own
   source-stripping renders dotted attributes, because the S46-style token join
   does NOT and those assertions would have passed vacuously). `a21ab1a2a`'s two
   regression pins are unchanged and still green. Two dead import bindings went
   with the duplication — `read_realm_sync_sidecar` and
   `exact_scoped_instance_ids` in harness.py, S41's class.

   RESIDUALS opened by execution:
   - **`active_cards` vs `active_card_count`.** The CLI column counts cards NOT
     in a `done` column; the wire's counts every non-archived card. Two
     different questions with near-identical names. NOT consolidated —
     collapsing them would silently change a number an operator reads, which is
     worse than the duplication. Kept, with the reason pinned.
   - **No unredacted seam exists.** If an operator debugging locally genuinely
     needs raw secret-bearing card prose, there is no builder parameter for it;
     the on-disk card JSON is the only source. Reported rather than invented on
     this wave's authority.
   - **Four synthetic row literals stay hand-written**, because no builder can
     project an entity that does not exist: `realm create --dry-run`,
     `board create --dry-run`, `office show` on an unauthored surface, and
     `board card resolve-conflict` when the card resolved to archived. Nothing
     gates that their key sets still agree with the real rows.
   - **Cross-repo (Launcher; NOT introduced here).** The board capability lane
     shells out to these very CLI verbs, and
     `mission_board_write.dart::boardCardFromResultPayload` parses the reply
     with the SNAPSHOT card shape — it reads `card_id`, `updated_by`,
     `unpublished`, none of which the CLI row emits (it emits `id`). It
     therefore always returns null and the read-your-writes path silently falls
     back to `provisionalBoardCard`. Pre-existing, and exactly the "wire-test
     idiom" class in the proposal ledger below: producer and consumer each
     pinned, the wire between them never asserted.
5. ~~**workspaces[].goals wire field (hermes B-3).**~~ **RULED CUT and EXECUTED
   2026-08-01 — hermes `d88ea8b55` (S47) + launcher `4739bd4f` (s53).** Taken
   together with item 8: same wire, same defect. The `tasks = []` seed and all
   three constant projections are gone — `_workspace_summary`'s
   `"goals": len(goals)`, `snapshot_prompt_observability`'s `tasks_by_id`, and
   `operator_channel_summary`'s `_TaskLookup` — plus everything reachable only
   through the resolved task: the synthetic `goal_input` conversation message,
   the title fallback, the run-id indirection, `_task_time`, and the
   `goal_id`/`task_id`/`updated_at` fallbacks. `status.build_status` passed its
   OWN `tasks = []` literal to the same parameter, so no production caller could
   supply a task at all; the parameters went with the projections per the
   ledger-item-2 rule. `resolve_situational_hud` KEEPS `task`/`goal_task` — the
   live mission-chat wrapper resolves them from the store per turn.

   **What the Launcher did with it — why this mattered more than item 8:**
   `MissionWorkspace.goalCount` parsed the field, the Manage Workspaces row
   RENDERED it ("N goals · soft isolation"), and `buildWorkspaceManagerModel`
   BLOCKED workspace delete on `goalCount > 0` with "Owns N goals — archive them
   first", attributed to a hermes `workspace_has_goals` refusal that exists only
   as a docstring example in `agent_runtime/errors.py`. An operator therefore
   read a real-looking number that was structurally always 0, behind a guard
   that could never fire and mirrored nothing. All four went with the field;
   delete stays gated by the permission and realm-default arms, which are real.

   Superseded pins INVERTED rather than deleted, each carrying why: S27's
   `test_the_workspace_goals_wire_field_survives` (it protected the count from a
   resemblance sweep — correct then; the ruling cuts it for a different reason),
   S29's `KEPT_LIVE_LOCALS` entry for `tasks`, and Launcher-side the
   policy/dialog/alert expectations — whose fixtures still send the removed
   field, so a stale producer cannot resurrect a reader.
6. **gateway/platforms/base.py:1407** bare expanduser("~") vs sibling's
   $HOME-preferring resolution - aligning widens a denial carve-out.
7. **Packaging:** psutil/fire declared but absent on the ambient test
   interpreter; markdown used by the matrix adapter but declared nowhere.
8. ~~**`role_envelope` runtime-config block is now a knob that governs nothing
   (hermes; opened by S44, 2026-07-31).**~~ **RULED CUT and EXECUTED 2026-08-01
   — hermes `d88ea8b55` (S47) + launcher `4739bd4f` (s53).** The whole surface
   below is gone: `RoleEnvelopeConfig`, `RuntimeConfig.role_envelope`, the
   `config.py` `_role_envelope_config` loader and its two call sites, and the
   five `migrations.py` range validators. Because `effective_config_summary` is
   `asdict(cfg)`, dropping the dataclass field dropped the wire block; an
   operator yaml that still sets it now loads and is ignored (pinned by test).

   **CORRECTION to the original entry's attribution, found during execution:**
   the live `enabled: true` was NOT an operator flip. The LAUNCHER wrote it —
   `mission_control_hermes_installer.dart` generates `config.yaml` from a
   template that carried the whole 11-field block with `enabled: true`. Removed
   there in the same wave, so a fresh install stops seeding it. **No Launcher
   PARSE of `runtime_config.role_envelope` existed** (the plural
   `role_envelopes` on agent rows is a different, still-parsed field) — so the
   lockstep here was producer-side, not consumer-side.

   ORIGINAL ENTRY (preserved): Cutting the store family left the
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

9. ~~**`Projector.apply_pending` has no production caller (hermes; opened by
   B-4, 2026-07-31).**~~ **RULED RETIRE + EXECUTED 2026-08-01 — `3d0935e51`
   (S46).** Original entry preserved below; what shipped, and the split between
   what was cut and what was kept, follows it.

   **CUT** (reachability re-verified per symbol immediately before the cut, and
   checked for dynamic dispatch — the repo contains no `getattr(projector, …)`
   and no string literal naming these methods, so module-level reachability was
   the whole story): `Projector.apply_pending` (5 test callers, 0 production);
   `Projector.acquire_lease`; `Projector._count_pending`; `ProjectorResult`;
   `LEASE_TTL_SECONDS`; the `meta['projector_lease']` row; the `event_log=`
   constructor keyword; and `SLO_INCREMENTAL_APPLY_MS`. Six tests went with the
   lane — the two named SLO tests plus `test_lease_excludes_second_projector`,
   `test_registered_event_rebuilds_the_whole_frame`, and the two
   `test_replay_equivalence_*` tests — along with four helpers in
   `test_projector.py` (`_seed_open_task`, `_write_enterprise_config`,
   `_row_diff`, `_goal`) that were the replay-equivalence pair's goal-row
   comparison scaffolding and were already called by nothing.

   **THE SPLIT ON THE LEASE, since the ruling asked for it explicitly:** the
   lease had NO other live consumer. `full_rebuild` never acquired it — only
   `apply_pending` did — so `acquire_lease`, `LEASE_TTL_SECONDS`, and the
   `projector_lease` meta key died whole with their one caller, and nothing
   about single-writer safety changed. (The projection unit had already been
   the whole compact snapshot rather than row-level deltas, so there was no
   concurrent-partial-write window for the lease to have been protecting.)
   `agent.credential_pool.CredentialPool.acquire_lease` is an unrelated live
   method on a different class with a real caller in `tools/delegate_tool.py`;
   S46 asserts its survival rather than leaving it to a bare-word grep.

   **KEPT:** `full_rebuild()` as the operator-invoked cache warmer behind
   `hermes harness rebuild-read-model`, and the whole serve/read path —
   `resolve_snapshot_frame`, `snapshot_watermark`,
   `ReadModel.projection_watermark`, `read_projection`, `render_snapshot`.
   `EventLog.iter_from_offset` also stays: `_count_pending` was one caller of
   four, not the last one — `stream.py` keeps it load-bearing. No coverage was
   lost with the replay-equivalence pair: `test_snapshot.py` pins
   `contract_version` / no-`goals` / `boards` on `build_snapshot()`, and
   `test_read_model.py::test_apply_full_rebuild_then_render_is_equivalent` pins
   `render_snapshot() == build_snapshot()` through the read model, so the round
   trip stays covered via the LIVE path.

   **Contract:** `tests/agent_runtime/test_s46_incremental_projection_lane_removal.py`
   (22 tests, written red first — 16 failed / 5 passed before the cut). Its
   text gate strips docstrings and comments before matching, so it cannot fire
   on the witness that records its own removal — the mechanical form of the
   rule s45 stated in prose.

   **Gates:** `tests/agent_runtime` 3,343 passed / 0 failed (3,327 baseline − 6
   + 22), run in a clean worktree carrying only this change, because a parallel
   agent's uncommitted snapshot/config work was failing 41 unrelated tests in
   the shared checkout at the time. Canonical `tests/hermes_cli` runner: 3,682
   passed / 0 failed, exit 0. Live smoke on alice through
   `.hermes/venvs/hermes-agent`: `harness snapshot --json` → 2 boards,
   `frame_source=built`, watermark `event_offset=86984208`;
   `harness rebuild-read-model --json` → `ok: true` at the same offset, and
   `harness read --projection snapshot` served the 29-key payload back — i.e.
   the one surviving production path through the projector still round-trips.

   **RESIDUALS this cut deliberately did NOT take** (each still true, none
   ruled): `Projector.config` is stored and never read — an unused constructor
   field, but not lane machinery, and removing it edits
   `runtime_commands.py`; `SLO_CONSUMER_VISIBLE_LAG_MS` in
   `test_read_model_slo.py` has zero assertions (RD4's live consumer-lag proof
   was never written) — same shape as the constant this wave did take, one lane
   over; and `read_model.enabled` is still `False` on the live alice root, so
   `full_rebuild` runs only when an operator types the verb and the serve path
   reports `frame_source=built` every time — retiring the incremental lane did
   not change that, it just stopped pretending a second lane existed.

   Original entry, as filed by B-4:

   Working in the projector for B-4 surfaced that the entire
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
   direction call, not a cleanup. — *Answered: RETIRE. See the S46 block above.*

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
  message. BOTH OPEN OPERATOR DECISIONS EXECUTED 2026-08-01 — `d355787a3`.
  (1) The five variant-B configs were patched live: each gained
  `env.STAGEC_LAUNCH_HELPER` and nothing else; all nine declaring profiles now
  match the template field-for-field (verified through the readiness lane, zero
  drift rows). (2) Advisory -> BLOCKING: `ADVISORY_ISSUE_CODES` deleted (one
  member, nothing left to partition), the `include_template_drift` opt-in
  retired so drift rides the one `mcp_server_issues` lane by default, and
  `profile_readiness` folds it into `machine_root_issues` -> `mcp_attention`
  with the field-level diff in the summary. The advisory-only
  `mcp_template_drift` readiness key went with the class. Report-only is
  unchanged: readiness names the field and prints the pasteable block, and no
  code path rewrites a config. The data test's expected-drift ledger is gone —
  it is now a plain new-drift tripwire.
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
- **The Launcher's generated hermes `config.yaml` is an unaudited producer
  (opened by S47/s53, 2026-08-01).** Executing item 8 found that the live
  `role_envelope.enabled: true` came from
  `mission_control_hermes_installer.dart`'s config template, not from an
  operator. The block is removed, but the template was never audited against
  the runtime's actual config schema and still writes at least one more block
  the runtime does not load: `mission_plan:` (enabled/enforce_routing/
  enforce_hud/version) — `tests/agent_runtime/test_config.py` explicitly
  asserts `not hasattr(cfg, "mission_plan")`. NOT swept on this wave's
  authority (scoped to the ruled fields). The real fix is a gate, not another
  one-off deletion: a test that every top-level key the installer template
  writes under `agent_runtime:` resolves to a field the loader actually
  consumes, so a retired config block fails a build instead of quietly seeding
  operator roots for months.

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
- ~~**F-9 dead `open_incident_budget_exceeded` alert (opened by B-5,
  2026-07-31).**~~ **EXECUTED 2026-08-01 — launcher `4739bd4f`** (landed with
  the s53 contract lockstep for items 5 + 8). The dead branch is cut and
  `snapshotAlerts` renders the one honest "parity warnings N" arm; the codes
  themselves stay visible in the chip's disclosure, and the retargeted test
  still FEEDS `open_incident_budget_exceeded` so a resurrected producer cannot
  quietly restore a label branch. The `mission_control_bridge.dart` comment was
  reworded and CORRECTED while there: it claimed "the harness raises its own
  `open_tasks_without_task_rows` warning on the same condition" — it does not,
  B-5 deleted that producer too, so the comment was asserting a diagnostic
  neither repo performs. A grep gate (`open_incident_budget_exceeded`, code
  lines only) keeps the branch from returning.
