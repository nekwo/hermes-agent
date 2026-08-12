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
6. ~~**gateway/platforms/base.py:1407** bare expanduser("~") vs sibling's
   $HOME-preferring resolution - aligning widens a denial carve-out.~~
   **RECLASSIFIED upstream-ward 2026-08-01:** the file is upstream-owned and
   the fork boundary forbids editing it (edits become upstream-PR candidates)
   — moved to the parked upstream-PR bucket (doc 18). The denial-carve-out
   concern travels with it: any future PR must state that aligning the two
   resolvers WIDENS the `~` denial carve-out, which upstream may or may not
   want — it is a behavior question for the upstream maintainers, not a
   mechanical fix.
7. ~~**Packaging:** psutil/fire declared but absent on the ambient test
   interpreter; markdown used by the matrix adapter but declared nowhere.~~
   **RULED EXECUTE and EXECUTED 2026-08-01 — hermes `6d3611da0`.** The item had
   two halves. Only one of them was real.

   **The packaging half was a FALSE finding, and the correction matters more
   than the fix.** `markdown` is not "declared nowhere". `pyproject.toml` has
   carried it in `[project.dependencies]` since upstream `c1eb2dcda`, as
   `"Markdown==3.10.2"` (line 79) under an eight-line comment explaining why it
   is core rather than matrix-scoped; `uv.lock` corroborates, listing `markdown`
   in the `hermes-agent` package's core `dependencies` array with no
   `extra ==` marker and pinning `markdown==3.10.2` in `requires-dist`. The
   original audit grepped case-sensitively for lowercase `markdown` and missed
   the capitalized spelling — PEP 503 treats the two as one package, `grep`
   does not. **No pyproject edit was made or needed**: all three packages were
   already correctly declared core — `fire==0.7.1` (line 43),
   `Markdown==3.10.2` (line 79), `psutil==7.2.2` (line 99). Worth carrying
   forward as a method note: "declared nowhere" claims sourced from a
   case-sensitive grep of a packaging file are not evidence.

   **The environment half was real, and was the whole of the actual work.** The
   ambient test interpreter `C:\Python312\python.exe` (3.12.5) — the one the
   env-gap fence registries are pinned to — did not have them. Installed at
   exactly the declared pins: `psutil 7.2.2`, `fire 0.7.1`, `Markdown 3.10.2`,
   plus `termcolor 3.3.0` pulled transitively by fire. No pin was invented or
   floated; each matches pyproject and uv.lock.

   **The cascade: 22 fence rows / 11 groups retired (305 → 283 nodes, 139 → 128
   groups), 2 re-diagnosed, 0 left on a stale reason.** Swept under the AMBIENT
   interpreter — never the runtime venv, which is the inversion the
   `tests/tools/conftest.py` header warns about — and diffed pre/post BY NAME,
   because the suites flake by ±1 and a count-only diff would have masked a new
   failure behind a retirement. Retired: gateway `test_memory_monitor.py` (3),
   `test_status.py` (1), `test_whatsapp_bridge_pidfile.py` (1),
   `test_matrix.py` (1); hermes_cli `test_arcee_provider.py` (1),
   `test_install_cua_driver.py` (2), `test_update_interrupted_recovery.py` (1);
   tools `test_browser_orphan_reaper.py` (4), `test_config_null_guard.py` (2),
   `test_process_registry.py::TestTerminateHostPidPosix` (2).

   **Four more rows came from the stale-row DETECTOR, not from grepping
   reasons** — their reason text never named a package, so no amount of reading
   the registry would have found them; only running the suite did:
   `test_approved_command_clean_slate` (1, the kill path stops tripping the
   live-system guard), `test_mcp_stability` (1, same), and
   `test_process_registry::TestPidReuseGuard` (1, its start-time comparison is
   a psutil read). The fourth,
   `test_process_registry::TestSpawnEnvSanitization`, is **NOT attributable to
   this install**: the detector reported it passing on the PRE-install sweep as
   well, so that row had already silently stopped being a fence. Deleted per
   the contract, with its recorded cause preserved in a comment — the
   underlying import-time `get_hermes_home()` fragility in
   `tools/environments/singularity.py` is unchanged and still worth retiring at
   the source.

   **Two rows survived the install, and they are the most valuable part: the
   missing import had been MASKING two real platform defects.** Both
   `test_process_registry.py` nodes were registered as "psutil is not
   installed". With psutil present they still fail, for causes the absent
   import had hidden — `test_popen_killed_when_thread_creation_fails` patches
   `os.getpgid`, which does not exist on Windows at all (`mock` raises
   `AttributeError`); and `test_kill_detached_session_uses_host_pid` asserts
   `psutil.Process(pid).terminate()` was called, which is the POSIX kill path
   only, since `_terminate_host_pid` shells out to `taskkill /PID <pid> /T /F`
   on Windows and never constructs a `psutil.Process` — so the kill SUCCEEDS
   and only the POSIX call assertion fails. Both were re-marked
   `windows_env_gap` with the real cause inline. This is the fence contract
   working exactly as designed: a row naming the wrong cause has stopped being
   a fence, and the only way to discover that is to remove the cause it names.

   **Frozen-home ledger (`tests/test_no_frozen_hermes_home.py`).** With `fire`
   installed the probe imported `trajectory_compressor.py` for the first time
   on this host. Its `FROZEN_LEDGER` entry had been carried from the pre-probe
   regex ledger, whose own caveat was that the regex "only ever saw the first
   hop" and a real run might find more names. It did not: the measurement found
   exactly `_hermes_home`. The reason was updated from carried to measured, and
   the entry deliberately KEEPS its `UNPROBED` row — per that test's own
   `test_ledger_reasons_are_present`, `UNPROBED` is per-environment and both
   entries are legitimate at once so long as the module is ledgered. 4/4
   passing, no new frozen names, no stale ledger entries.

   **Discovered, not fixed (pre-existing, recorded so it is not silent):**
   `tests/hermes_cli/test_update_interrupted_recovery.py` fails under a
   whole-directory `pytest tests/hermes_cli` run but passes per-file. Its
   sibling `test_marker_round_trip` — never fenced — fails the same way both
   before and after this change, so the file carries a pre-existing
   order/pollution dependence (`_write_update_incomplete_marker()` not
   producing the marker under suite ordering). Not re-fenced: fence rules 3–4
   forbid registering a defect in our own code, and the canonical per-file
   runner is unaffected.

   **Gates.** `tests/agent_runtime`: 3383 passed / 1 skipped / 0 failed. The +5
   against the ledger's 3378 baseline is entirely the parallel agent's items
   10–11 (`d89059dd7`, `587dbd6c7`) adding tests to
   `test_launcher_qa_template_drift.py` and `test_machine_roots.py`; this
   change touches no file under `agent_runtime/` or `tests/agent_runtime/`. The
   1 skip (not 2) is environmental and predates the install — measured at 1
   before any package was touched — `no live profile tree at
   %LOCALAPPDATA%\hermes\profiles`. Canonical hermes_cli runner: exit 0, 3708
   passed / 0 failed, with deselection dropping 93 → 89, i.e. exactly the four
   retired hermes_cli nodes now selected and passing. gateway 69 → 63 failed
   (−6 fixed, 0 new, by name); tools 135 → 124 failed (−11 fixed, 0 new, by
   name). Every suite's stale-row detector clean.
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

10. ~~**Readiness only validates REQUIRED MCP servers, so the drift block is a
    no-op on the live snapshot lane (hermes; opened by the P5 flip,
    2026-08-01).**~~ **RULED EXECUTE and EXECUTED 2026-08-01 — `d89059dd7`**
    (third slice of the P5 lane, after `6190e4d9d` and `d355787a3`).
    `profile_readiness_for_persona` scoped
    `mcp_server_issues(only=effective_required_mcp_servers)`, and on the live
    tree every snapshot agent's required list is EMPTY — so the subset handed
    to the checker was `{}`, the now-blocking `mcp_server_template_drift` check
    (and the binding checks it rides with) evaluated NOTHING for them, and
    their `ready` verdict said nothing about their configured `launcher_qa`
    block. The line was held only by the data test
    (`test_every_live_launcher_qa_block_matches_the_canonical_template`) — a CI
    tripwire, not a runtime one.

    **The scoping split as landed.** One lane still (no second checker), with
    the split explicit in code rather than implied by call sites: the new
    `machine_roots.mcp_servers_in_issue_scope(servers, required=…)` resolves
    which configured blocks `mcp_server_issues` validates, and the old
    `only=` parameter is DELETED (a stale caller gets a `TypeError`, pinned,
    rather than silently keeping the no-op).

    - **CONFIGURED scope** — every configured block that has a canonical
      template, validated wherever it is declared and whoever requires it.
      That carries both issue classes that are about a DECLARATION being
      wrong: `mcp_server_template_drift` AND the binding failures on the same
      block (`unbound_root`, `root_target_missing`, `invalid_root_token`,
      `platform_unsupported`).
    - **REQUIRED scope** — the required names, validated exactly as before,
      plus the missing-server class, which stays required-scoped and is still
      computed in `profile_readiness` from the required list (a profile that
      does not declare an un-required server is not a defect).
    - The widening is gated on *has a canonical template* — the only names the
      module can state a correct shape for. Validating every configured block
      of every profile would fail a persona over an operator's unrelated
      experimental server; that is a different ruling and was not taken.

    Semantics untouched: R-1 (profile-declares-the-server) and the
    spawn/admission paths (`resolve_mcp_servers`, `mcp_admission.py`,
    `tools/mcp_tool.py`) are unchanged — this is readiness REPORTING only, and
    no path here writes a config. Nothing in the codebase gates on the
    readiness verdict; it is reported through `snapshot`/`status`/
    `tool_visibility` and read by operators.

    **Live proof (live venv, `HERMES_HOME=X:\Eternia\.hermes\profiles\alice`).**
    All 9 profiles that declare `launcher_qa` (alice, backend-dev, base,
    gpt-launcher, launcher-dev, launcher-qa, neko, qa, unbounded) now enter the
    configured scope and report ZERO issue rows; the non-templated blocks they
    also carry (`dart`, `marionette`) correctly stay out of scope. The
    6-persona roster's readiness rows are IDENTICAL computed under the old
    filter scope and the widened scope (all `ready` except the pre-existing
    `pm` → `missing_profile`, unchanged by this), so the widening changes no
    live verdict today — as expected on a tree the P5 fix already
    canonicalized. The counterfactual is pinned by
    `test_a_configured_but_unrequired_drifted_block_degrades_readiness` and
    re-demonstrated on a scratch `HERMES_HOME`: a drifted, configured,
    UNREQUIRED `launcher_qa` block reads `ready` under the old scope and
    `mcp_attention` (naming `env.STAGEC_LAUNCH_HELPER`) under the new one.

    Gates: `tests/agent_runtime` 3,383 passed / 1 skipped against a
    same-box clean-HEAD baseline of 3,378 / 1 (delta = the 5 new tests);
    hermes_cli canonical runner exit 0, 3,704 tests, 0 failed.

11. ~~**Launcher board write path parses the CLI reply with the SNAPSHOT card
    shape — read-your-writes has silently never worked (Launcher; surfaced by
    S48, 2026-08-01).**~~ **RULED EXECUTE and EXECUTED 2026-08-01 — launcher
    `97ba5cfc`** (on launcher `origin/main`).
    `mission_board_write.dart::boardCardFromResultPayload` read
    `card_id`/`updated_by`/`unpublished` (and, secondarily, a nested `card`
    envelope); the CLI card row emits `id` and none of the others, so the parse
    ALWAYS returned null and the lane silently fell back to
    `provisionalBoardCard`. Pre-existing (not introduced by S48, which changed
    no key names) and exactly the wire-test idiom class: producer pinned,
    consumer pinned, wire never asserted.

    **The empirical shape**, captured from the venv hermes (v0.19.1 /
    2026.7.30, upstream `bc5fab7d`, post-S48 `71a96b517`) against a throwaway
    `HERMES_HOME`: all five mutating card verbs plus `board resolve-conflict`
    print `_object_envelope("card", _card_row(card, full=True))`, which is
    FLAT — `{schema_version, kind:"card", id, column_id, title, priority,
    state, updated_at, board_id, description, description_truncated, labels,
    assignee, checklist, order_key, created_by, revision, created_at}`. No
    `updated_by`, no `unpublished`, no `linked_goal_id`, and no nested `card`
    key — `_object_envelope` splats the row into the root, so that branch had
    no producer either. The parser now takes identity in DECLARED precedence
    (`card_id` for a snapshot-shaped row, then `id` for the CLI envelope) and
    rejects a payload whose `kind` names a different row type (`workspace`
    replies also carry `id`).

    **The gate**: `mission_board_write_wire_test.dart` walks verb → the
    captured real reply → parsed card for every verb (golden fixture
    `test/features/mission_control/fixtures/hermes_board_card_live.json`),
    asserts the reply key set EQUALS {envelope metadata} ∪ {keys mapped to
    model fields} so a hermes-side rename fails loudly instead of degrading
    read-your-writes in silence, and carries a regression row proving the
    pre-fix snapshot-shape parser returns null on every real reply. Sibling
    audit found no second dead parse: `flow.apply`'s `reconciled[]` keys are
    all emitted by `ingest_flow_graph`, the chat controller's
    `chat_turn_outcome_unknown` branch reads three keys all present in the
    refusal dict at `persona_commands.py:2351`, and every typed getter the
    bridge promotes has a live producer. Launcher gates: `flutter analyze` 0
    issues; `flutter test test/features/mission_control test/core/qa` 3288
    passed / 2 skipped / 0 failed.

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
  it is now a plain new-drift tripwire. THIRD SLICE 2026-08-01 — `d89059dd7`:
  the readiness SCOPE the flip inherited (`only=effective_required_mcp_servers`,
  empty for every snapshot agent) was widened per ledger item 10, so drift and
  the binding checks now run over every CONFIGURED block that has a canonical
  template while the missing-server class stays `required`-scoped.
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
- ~~**The Launcher's generated hermes `config.yaml` is an unaudited producer
  (opened by S47/s53, 2026-08-01).**~~ **RULED EXECUTE and EXECUTED 2026-08-01 —
  launcher `e7ee54af` (s54).** The gate this entry asked for exists:
  `mission_control_hermes_installer_template_test.dart` asserts every top-level
  key the template writes under `agent_runtime:` resolves to a field the loader
  consumes, and its red-proof is EXECUTED (the pre-fix template is preserved
  verbatim; a passing case asserts the gate fails on it). SIX blocks were seeded
  that the runtime does not load — `enterprise_worker_sessions`,
  `normal_worker_flow`, `repo_bundle_routing`, `simplified_agent_contract`,
  `swarm` and `mission_plan` — plus four PHANTOM sub-keys the dataclasses never
  had. The template now writes four keys. ORIGINAL ENTRY preserved: Executing item 8 found that the live
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

## S49-S55 mechanical cut wave (hermes, 2026-08-01)

> Scout findings -> operator ruling CUT -> executed. Every claim was
> re-verified against the tree immediately before cutting; two did not survive
> that re-verification and are recorded below as **DIED**, not forced. Event
> contract count moved **82 -> 68** across three of the five cuts;
> `contract_hash()` moved
> `f655bd56bb378c1fa818f360a0f401d5d957c17df33b6a65cb2fd2a6982acfe6` ->
> `f5c5663ffeedff3d7d791f505e56b27287592eaafdb4a98874d009d33b9d0b31`. The
> absolute count authority remains
> `tests/agent_runtime/test_s15_event_contract_pruning.SURVIVING_EVENT_COUNT`.

### Executed

| Cut | What went | Contracts | Commit |
| --- | --- | --- | --- |
| S49 | `agent_runtime/operator_control.py` whole (145L) | -3 (`operator.takeover.*`), 82 -> 79 | `c58d759b9` |
| S50 | `agent_runtime/launcher_process_hygiene.py` whole (164L) | none | `c58d759b9` |
| S52 | `RepoBundleStore` WRITE lane; `repo_bundles.py` 521 -> 270 | -7 (`repo_bundle.*`), 79 -> 72 | `15ee23b21` |
| S53 | `GoalRuntimeInstanceStore` WRITE lane; `runtime_instances.py` 264 -> 135 | -4 (`lane.*`, `foreground_runtime.closed`), 72 -> 68 | `2f8f74b9e` |
| S54 | 30 individually dead symbols across 21 modules | none | `90dbe908a` |
| S55 | Structural gate: registered event type => production emitter | n/a | `134c8dc9d` |

`S51` (worker_sessions), the config-block items and the roster gate were NOT in
this wave's scope and are untouched.

### Claims that DIED on re-verification (reported, not forced)

1. **`repo_context.isolated_repo_context_for_run` + `repo_execution_context_for_task`
   are NOT a closed loop.** Ruled cut on the basis that their only callers were
   tests. `isolated_repo_context_for_run` is the constructor
   `tests/agent_runtime/test_delivery_directive.py` builds real worktrees with
   (NINE call sites) to test the LIVE
   `delivery_directive.reap_orphan_worktrees` janitor. Its own S24 docstring
   records that the suite pins two live-incident regressions reachable only
   through it — junction severing that once emptied the backend venv
   (2026-07-01) and the backend `.env` copy whose absence broke every read-only
   proof (2026-07-03) — and states the lane must be "retired together or not at
   all". `repo_execution_context_for_task` shares `_context_for_workdir` with
   that kept lane. **The whole file was excluded; the S43 KEEP stands.** A cut
   was prepared (953 -> 537 lines) and reverted once the janitor dependency
   surfaced. Pinned in `test_s54_individual_dead_symbols.py` so it is not
   re-attempted.
2. **The gate's "24 emitter-less pre-cut" figure is a REACHABILITY count, not a
   presence count.** A static presence scan reports 3 both before and after the
   wave, because the 14 de-registered types had emitters that were physically
   present inside dead write lanes. S55 therefore gates presence and says so
   explicitly; the unreachable-but-present class stays with the per-cut
   removal-contract tests. Consequence: the planned temporary "S51 pending"
   allowlist entry for the ten `worker_session.*` types **was not added and is
   not needed** — they still have live emitters. Adding it would have been a
   hole, since S55 is exactly what should go red if S51 cuts that write lane
   without de-registering them in the same commit. **There is nothing here for
   the follow-up wave to remove.**

### New debt opened by this wave (S47 item-5 class: wires that can only report a constant)

- ~~**`repo_lock_summary()` can no longer be non-empty.**~~ **CLOSED at S56.**
  S52 deleted both `acquire_repo_bundle_locks` and `release_repo_bundle_locks`,
  so nothing could write `repo_bundle_locks.json`; the summary was still read
  and still published by `status.py` as `repo_locks`, where it could only ever
  report `{"lock_count": 0, "locks": []}`. The wire row and the function are
  gone, together with `bundle_queue_summary` / `repo_bundle_summary` /
  `repo_bundle_delivery_summary`, which had the same shape for the same reason.
- ~~**`status.py`'s `lanes` is the same shape after S53**~~ **CLOSED at S56** —
  no writer can create a `GoalRuntimeInstance` row, so the top-level projection
  was empty by construction. Only the DUPLICATE went;
  `runtime_instances["lanes"]` is the projection and stays.

Both were asserted as constants in the S52/S53 removal tests so they stayed
visible rather than being inferred later. Retiring either edits the emitted
status frame (a contract move + Launcher lockstep), which is why this wave —
scoped to mechanical removal — did not take them; S56 did, and both pins are
inverted there.

### Deliberate KEEP a reference count calls dead

- **`incidents.MODEL_INVALID_OUTPUT`.** Nothing reads the NAME, but its VALUE is
  live as a bare string literal in `observability.py` (`incident.kind in
  {"model_invalid_output"}`) and in two tests. Cutting the constant would leave
  a live concept addressed only by a duplicated literal. The real fix is a
  single-name-authority one at the `observability.py` end (read the constant
  instead of re-typing the string); it was not on the ruled list, so it is
  recorded here rather than taken. Pinned with its reason in
  `test_s54_individual_dead_symbols.py`.

### A seam that advertised a caller it never had

- `prompt_observability.load_final_model_input_for_context` documented itself as
  the read "the launcher fetch lane and a future `harness prompt-context
  final-model-input` CLI verb both call through". The Launcher repo has **zero**
  references to it and the verb was never wired. Cut as ruled — an advertised
  seam with no caller is the same defect class as a registered event with no
  emitter — and noted here because the docstring, not the code, was the thing
  asserting something false.

### Housekeeping correction to an inherited pattern

Three "delta-only" removal tests (S44, and then S49/S52/S53 as this wave wrote
them) each held a SECOND copy of the absolute event count while their docstrings
promised not to. Every later cut in the same wave then broke them. All four now
assert only their own delta plus agreement with S15's single authority.


## S56 contract wave — worker-session lane, seven config blocks, roster gate (hermes contract 46 -> 47 + Launcher s54, 2026-08-01)

> One coherent contract move, bumped once. Events **68 -> 58**; `contract_hash()`
> `f5c5663ffeedff3d7d791f505e56b27287592eaafdb4a98874d009d33b9d0b31` ->
> `20639a26ccf30348e0c1317f409ad6c45b201f9f91c4c76bc6efb99d49cf7bc8`. The
> absolute count authority remains
> `tests/agent_runtime/test_s15_event_contract_pruning.SURVIVING_EVENT_COUNT`.

### Executed

| Item | What went |
| --- | --- |
| S56 worker lane | `agent_runtime/worker_sessions.py` DELETED WHOLE (618L). All 10 `worker_session.*` contracts de-registered in the SAME commit as their emitters (the S55 gate makes splitting them red, on purpose). `models.WorkerSession`, `PersonaInstance.active_worker_session_id`, `paths.worker_sessions_dir` / `worker_session_path` / `worker_context_dir` / `proof_sandbox_root`, `locks.worker_session_lock`, the `worker_sessions` checkpoint EntityClass, the `serve` fingerprint dir entry and the `migration_status` count all went with it. |
| S56 worker-derived persona surface | `derive_from_workers` -> **`ensure_for_personas(personas)`** (worker-free, same behaviour); `update_from_worker`, `_goal_id_for_worker`, `_worker_carries_live_binding`, `ACTIVE_PERSONA_WORKER_STATES` deleted; `ChatBusyError` / `_live_chat_bindings` / `_terminate_live_chat_bindings` reduced to their run arm (`_live_chat_binding` / `_terminate_live_chat_binding`); `persona_profile_binding.BUSY_ACTIVE_WORKER` retired. |
| S56 wire (status) | Removed `worker_sessions`, `active_worker_sessions`, `repo_bundles`, `repo_bundle_closeout`, `bundle_queue`, `repo_locks`, `lanes`, `production_envelope`, `swarm`, `swarm_budget`. `observability` lost its `worker_sessions=` parameter and everything it fed; `dirty_state` lost its `workers=` parameter and four rows. `runtime_instances` / `foreground_runtime` KEPT — the block's own `lanes` sub-key is the projection; only the top-level duplicate went. |
| S56 config | SIX blocks removed whole — `continuous_role_sessions`, `enterprise_worker_sessions`, `normal_worker_flow`, `repo_bundle_routing`, `simplified_agent_contract`, `swarm` — plus `_apply_enterprise_role_session_compat` (one unread block mapped onto another) and their 20 `migrations` range validators. `supervision` PRUNED to `child_events_enabled`, the one field `continuity.py:88` reads. |
| S56 roster gate | `persona_instance_runtime_enabled()` / `persona_assignment_store_enabled()` retired; the persona-instance roster and the persona-assignments section are UNCONDITIONAL in both `build_snapshot` and `build_status`. The `persona_instance_runtime` WIRE block survives (the Launcher bridge reads it) and now reports the truth. |
| S56 gate | New structural gate: every `RuntimeConfig` field must have a production reader or an explicit ledger entry with a reason. |
| Launcher s54 | Contract pin 46 -> 47; `activeWorkerSessionId` / `workerSessionId` and the four surfaces that consumed them removed; the installer config template fixed and gated. |

### The guarded sub-step: the scout claim that DIED, and the method note that outlives it

The S51 ruling allowed cutting the worker-derived persona surface only if no
live persona instance carried a non-null `active_worker_session_id`. The first
scan said it did — **three of four** instances under
`X:\Eternia\.hermes\profiles\alice\agent_runtime\persona_instances` carry one
(`personainst_backend_dev` -> `worker_237134aae4bd`, `_neko_supervisor` ->
`worker_ff10543cffdc`, `_qa` -> `worker_e392c8d410a2`), beside a
`worker_sessions/` directory holding sixteen rows.

**That tree is not the live runtime root.** Under
`HERMES_HOME=...\profiles\alice`, `paths.store_root()` resolves to
`X:\Eternia\.hermes\agent-runtime` — the root `runtime_commands` pins for
`live-tony` — where there are **fifteen** persona instances, **every one** with
`active_worker_session_id: null`, and **no `worker_sessions/` directory at
all**. The profile-scoped tree was last written 2026-06-18 and nothing resolves
to it. The guard's condition held on the root that matters, so the surface was
CUT (and with it `close`, the tenth event, and the whole module).

Method note, worth more than the result: **a profile directory that LOOKS like a
store root is not evidence about the store root.** Ask `paths.store_root()`.

### The read-side split, as landed

There is none — the split collapsed. Once the write lane went, the only
remaining consumers of `WorkerSessionStore` were `derive_from_workers` (which
`build_snapshot` had been feeding a `workers = []` literal since S47, so the
worker branch was unreachable on the live tree) and `_live_chat_bindings` /
`_has_live_binding`, whose worker arms read a field nothing can set. All three
went, so the read side had no consumer left and the module was deleted whole
rather than kept as a store nobody calls.

### production_envelope verdict: DELETED

Checked claim by claim against the tree rather than reworded a third time:

- H6 "worker.pause and worker.resume capabilities are registered" — only
  `worker.resume` was ever registered (`coordinator_permissions.py:12`), and
  both verbs' implementations went with the write lane. **FALSE.**
- H6 "daemon stop/kill paths exist", H8 "stale runs are recoverable through the
  ticker", H8 "mid-run daemon loss ... a restarted ticker", H9 "daemon queue mode
  is lane-based" — the Mission Daemon was retired; `status` hardcodes
  `execution_mode="manual"`. **FALSE.**
- H8 "role envelopes ... are file-backed" — S44 deleted that store. **FALSE.**
- H9 "repo bundle queueing gates dependent handoffs" — S52 deleted every writer.
  **FALSE.**
- H7/H9 swarm-ceiling arms — the enforcement never existed. **FALSE.**
- H10's five entries are prose about unit tests with no executable backing.

After the config cuts, what remained was hand-written prose keyed on flags that
no longer exist. Deleted from the wire (`status` and `effective_config_summary`)
and the module removed. The Launcher never parsed it. S49's pin on the H6 claims
is INVERTED, not deleted, with the reason kept.

### Roster gate as landed, and the counterfactual

Made unconditional; no disable consumer exists in either repo — the CLI's
"runtime is disabled" print was the only branch, and it reported the flag rather
than acting on it. Counterfactual pinned in `test_s56_config_block_removal.py`:
an operator config that still sets the old block to `false` no longer suppresses
the roster, and the block-absent DEFAULT (`enabled` / `persona_instance_runtime`
both `False`) no longer omits the section. **Not vacuous**: six live profiles —
qa, launcher-dev, backend-dev, launcher-qa, gpt-launcher, aliceimagecron — set
no `enterprise_worker_sessions` block at all and therefore GAIN the roster; the
four that set it (alice, neko, base, unbounded) set it true and are unaffected.

### Debt entries this wave STRIKES

- ~~**`repo_lock_summary()` can only report a constant.**~~ CLOSED — the wire row
  `repo_locks` and the function are gone.
- ~~**`status.py`'s `lanes` is empty by construction.**~~ CLOSED — the duplicate
  top-level row is gone; `runtime_instances["lanes"]` (the projection) stays.
- ~~**The Launcher's generated hermes `config.yaml` is an unaudited producer.**~~
  CLOSED — the template now writes only `redaction_mode`, `read_model`,
  `default_api_mode`, `root_node_mode`. Six blocks removed
  (`enterprise_worker_sessions`, `normal_worker_flow`, `mission_plan`,
  `repo_bundle_routing`, `simplified_agent_contract`, `swarm`), including four
  PHANTOM sub-keys the dataclasses never had
  (`repo_bundle_routing.auto_create_from_mission_plan`,
  `normal_worker_flow.auto_final_gate_after_delivery` and
  `.max_auto_final_gate_repairs_per_stage`,
  `simplified_agent_contract.allow_legacy_decision_aliases`). Gated by
  `mission_control_hermes_installer_template_test.dart`, whose red-proof is
  EXECUTED: the pre-fix template is preserved verbatim in the test and a passing
  case asserts the gate fails on it, naming exactly those six.

### New debt this wave OPENED (measured, not inferred)

- ~~**Twenty-nine `RuntimeConfig` scalar fields have no production reader.**~~
  **CLOSED at S57** (`23be05e00`) — all 29 CUT after per-field re-verification.
  Found by the new gate, carried in its FROZEN `UNRULED_DEBT` ledger with a
  per-field reason: the whole `daemon_*` family, the four `live_run_*` budgets,
  the four `liveness_*` knobs, the three `artifact_storage_*` watermarks,
  `mission_max_total_tokens` / `mission_wall_clock_deadline_seconds`,
  `neko_recovery_attempt_cap` / `neko_extension_cap`, `heartbeat_ttl_seconds`,
  `max_actions_per_tick`, `root_node_mode`, `preferred_goal_execution_mode`,
  `scope_wait_deadline_seconds`, `run_lease_seconds`,
  `tool_wait_timeout_seconds`, `child_progress_min_interval_seconds`,
  `deploy_timeout_seconds`. PRE-EXISTING — most lost their reader when the
  mission/daemon lanes were retired; `run_lease_seconds` lost its last one in
  the S56 commit with `production_envelope`.
- ~~**`RepoBundleStore` now has ZERO production importers.**~~ **CLOSED at S57**
  (`23be05e00`) — module + `RepoBundle` model + `paths.repo_bundle_path` +
  `migration.counts.repo_bundles` + the `serve` fingerprint entry DELETED WHOLE.
  `status.py` was the last importer. `runtime_instances` KEPT its checkpoint row
  deliberately: also writer-less since S53, but its rows still ship on the status
  wire.
- ~~**`delivery_directive.read_bundle_promotion_record` /
  `bundle_promotion_record_path` are caller-less.**~~ **CLOSED at S59**
  (`799249fbf`) — receiver-aware verification confirmed the pair called only
  each other and tests. Both functions and their sole path support,
  `paths.repo_bundles_dir` / `repo_bundles_task_dir`, were removed together.
- ~~**Launcher: the whole `MissionRoleEnvelope` / `roleEnvelopes` lane is dead by
  emptiness.**~~ **CLOSED at Launcher s55** (`e81ed6fc`) — model, BOTH parses,
  and every consumer removed. hermes S44 deleted `agent_runtime/role_envelopes.py`, so
  nothing has produced the `role_envelopes` frame section since; the parse at
  `mission_control_snapshot.dart:725` could only yield `[]`. The s54 text gate
  was scoped AROUND it; s55 took it and gates it globally.
- **`tests/agent_runtime/test_persona_assignments.py` is mixed-EOL in the index**
  (4,395 CRLF + 794 LF lines). The Edit tool silently normalizes such a file
  whole, turning a 95-line change into a 1,769-line diff. Restored byte-wise this
  time; worth a deliberate normalization commit.

### Superseded pins INVERTED rather than deleted (each carrying why)

`test_s55_registered_events_have_emitters::test_the_worker_session_family_needs_no_temporary_exemption`
(the ten types are now absent from BOTH sides, and the inverted form is what
catches a re-registration with no emitter behind it);
`test_s49_operator_control_removal`'s H6 controls pin (the envelope's absence is
now the only way its original concern can be true); `test_s23_paths_orphan_removal`'s
`proof_sandbox_root` keep and its checkpoint keep-set;
`test_s44_role_envelope_family_removal`'s `repo_bundles` writer keep;
`test_s24_delivery_directive_residue_removal`'s bundle-summary survival;
`test_s29_snapshot_dead_local_removal`'s `KEPT_LIVE_LOCALS` `workers` entry;
`test_s52_repo_bundle_write_lane_removal`'s two constant-wire pins;
`test_s28_status_observe_shrink`'s worker signals;
`test_root_config_pinning::test_swarm_config_resolves_from_root` (fixture kept,
now proving load-and-ignore); Launcher-side the s53 contract pin, the
`activeWorkerSessionId` parse expectations, the busy-reason table and the
orphan-classification truth table — all with fixtures still SENDING the removed
fields so a stale producer cannot resurrect a reader.

### Stream goldens moved WITH the contract, in this change

`tests/fixtures/stream_frames/{hydrate,delta,delta_batch}.json` +
`MANIFEST.sha256`, and their byte-identical Launcher copies under
`test/fixtures/harness_stream/`. Edited per the S47 precedent — drop the keys the
wave removed, bump `parity.contract_version` — rather than regenerated from a
fresh seeded root, which would churn unrelated pre-existing staleness in the same
bytes. `test_stream_contract_fixture` holds ONE `CONTRACT_VERSION` constant, not
a split live/golden pair: a split pin would let the launcher's
`kSupportedMissionContractVersion` sit against a golden nobody bumped, which is
exactly the drift that file exists to catch.

### Also recorded

- `tests/agent_runtime/test_worker_actions_blocked_menu.py` was expected to go
  with the lane. It does NOT: it pins the retirement of
  `agent_runtime.worker_actions`, a different module retired earlier, and never
  touched the worker-session store. KEPT.
- The `parity.completeness` block changed shape as a side effect of the roster
  becoming unconditional: the three persona-chat `ProjectionAccountant`s now
  always run, so an empty runtime reports three zero rows instead of `{}`.
  `test_parity` was retargeted to the exact keyset rather than `== {}`.


## S57 contract wave — the 29-field config ledger, the repo-bundle store, the Launcher role-envelope lane (hermes contract 47 -> 48 + Launcher s55, 2026-08-01)

> One coherent contract move, bumped once. No event contracts moved: the
> absolute count authority stays
> `tests/agent_runtime/test_s15_event_contract_pruning.SURVIVING_EVENT_COUNT`.
> This wave closes out the S49-S56 campaign — the reader gate's debt bucket is
> EMPTY, the repo-bundle lane is gone in full, and the Launcher's last
> dead-by-emptiness collection from the S44 store cut is retired.

### Executed

| Item | What went |
| --- | --- |
| S57 config | TWENTY-NINE `RuntimeConfig` scalars removed — dataclass row + `config.py` load line + `migrations` range validator each. Plus FOUR cross-field validators that related two or three DEAD knobs to each other (`live_run_max_total_tokens` <= `mission_max_total_tokens`; `liveness_poll_seconds` 30..120; `liveness_hung_seconds` < `heartbeat_ttl_seconds`; `artifact_storage_*` low<=high<=critical). `UNRULED_DEBT` shrinks 29 -> **0** and stays frozen there. |
| S57 store | `agent_runtime/repo_bundles.py` DELETED WHOLE with `models.RepoBundle` (31 fields), `paths.repo_bundle_path`, `migration_status()`'s `counts.repo_bundles` row, and the `serve` `_FINGERPRINT_STORE_DIRS` entry. `tests/agent_runtime/test_repo_bundles.py` deleted; its ONE live case re-homed. |
| S57 gate | S56's reader gate becomes a pure tripwire — with the bucket empty and `REPORT_ONLY` holding one wire/version field, a new unread `RuntimeConfig` field fails outright with nowhere to be parked. |
| Launcher s55 | Contract pin 47 -> 48; `MissionRoleEnvelope` + `roleEnvelopes` cut whole (model, BOTH parses, 11 field/ctor sites, the two HUD pills); installer template drops `root_node_mode`; the `kHermesRuntimeConfigKeys` mirror drops THIRTY entries. |

### The 29-field disposition table

Every field re-verified by hand before the cut — AST attribute form, `getattr`
string form, and a plain repo-wide text scan across `agent_runtime` /
`hermes_cli` / `tools` / `tests` / yaml / dart. **Not one survived with a reader
the gate had missed**, so the gate's scanner needed no correction and nothing was
annotated as a false positive. The "only reference found" column is what the scan
actually returned.

| # | Field | Only reference found (outside the definition) | Disposition |
| --- | --- | --- | --- |
| 1 | `heartbeat_ttl_seconds` | `config.py` load; `_positive`; the `liveness_hung_seconds` cross-check | CUT |
| 2 | `max_actions_per_tick` | `config.py` load; `_positive` | CUT |
| 3 | `daemon_enabled` | `config.py` load | CUT |
| 4 | `daemon_interval_seconds` | `config.py` load; `_positive` | CUT |
| 5 | `daemon_idle_interval_seconds` | `config.py` load; `_positive` | CUT |
| 6 | `daemon_heartbeat_seconds` | `config.py` load; `_positive` | CUT |
| 7 | `task_create_auto_start_daemon` | `config.py` load | CUT |
| 8 | `root_node_mode` | `config.py` load; a bool-type validator. **The 60 other hits are a ContextVar + kwarg of the SAME NAME** (`skill_utils`, `prompt_builder`, `skills_tool`) that never reads the config field | CUT |
| 9 | `preferred_goal_execution_mode` | `config.py` load | CUT |
| 10 | `live_run_max_wall_seconds` | `config.py` load; `_positive` | CUT |
| 11 | `live_run_max_api_calls` | `config.py` load; `_positive` | CUT |
| 12 | `live_run_max_total_tokens` | `config.py` load; `_positive`; the ceiling cross-check | CUT |
| 13 | `live_run_iteration_budget` | `config.py` load; `_positive` | CUT |
| 14 | `scope_wait_deadline_seconds` | `config.py` load; `_positive` | CUT |
| 15 | `run_lease_seconds` | `config.py` load; `_positive` | CUT |
| 16 | `tool_wait_timeout_seconds` | `config.py` load; `_positive` | CUT |
| 17 | `liveness_enabled` | `config.py` load | CUT |
| 18 | `liveness_poll_seconds` | `config.py` load; `_positive`; the 30..120 range check | CUT |
| 19 | `liveness_quiet_strikes` | `config.py` load; `_positive` | CUT |
| 20 | `liveness_hung_seconds` | `config.py` load; `_positive`; the ordering check | CUT |
| 21 | `child_progress_min_interval_seconds` | `config.py` load; `_positive` | CUT |
| 22 | `deploy_timeout_seconds` | `config.py` load; `_positive` | CUT |
| 23 | `mission_max_total_tokens` | `config.py` load; `_positive`; the ceiling cross-check | CUT |
| 24 | `mission_wall_clock_deadline_seconds` | `config.py` load; `_positive`; a `config.py` COMMENT deriving `MISSION_CHAT_MAX_MAX_SECONDS` from it | CUT (comment rewritten; 86400 is now that constant's own value) |
| 25 | `neko_recovery_attempt_cap` | `config.py` load; `_positive` | CUT |
| 26 | `neko_extension_cap` | `config.py` load; `_positive`; a TEST comment claiming two `snapshot.py` seams read it — **false**, see below | CUT |
| 27 | `artifact_storage_low_watermark_mb` | `config.py` load; `_positive`; the ordering check | CUT |
| 28 | `artifact_storage_high_watermark_mb` | `config.py` load; `_positive`; the ordering check | CUT |
| 29 | `artifact_storage_critical_watermark_mb` | `config.py` load; `_positive`; the ordering check | CUT |
| — | `lock_acquire_timeout_seconds` | **`locks.py:133`**, via `getattr(load_root_runtime_config(), "lock_acquire_timeout_seconds", 15)` | **KEPT** — the neighbour that proves the gate's shape |

**Cut: 29. Kept-with-evidence: 0. Survivor in the same neighbourhood: 1
(`lock_acquire_timeout_seconds`).**

The keep matters more than any single cut. It sits among the removed scalars,
reads identically, and is LIVE — through the `getattr` STRING form the S56 gate
was built to see. A prefix-shaped trim (`*_seconds`) or an eyeball pass takes it.
That is the whole argument for an AST + string-form resolver rather than a grep,
and it is pinned in `test_s57_unruled_config_debt_removal`.

### The claim that was false, and where it lived

`test_root_config_pinning.test_neko_extension_cap_resolves_from_root` carried a
comment asserting the field is "consumed inside embedded seams that need live
Incident + RunStore state to reach (`snapshot._run_blocked_reason`,
`snapshot._next_action_summary`); those read
`load_root_runtime_config().neko_extension_cap`". Neither seam reads it. The
bounded-continuation lane reads its own constants.

This is the third time this campaign has found the same shape — a TEST COMMENT,
not code, asserting a reader no scan can locate (S56 found it in
`prompt_observability.load_final_model_input_for_context`'s docstring, S49 in
`production_envelope`'s H6 prose). The comment is QUOTED in the inverted test
rather than deleted, because the pattern is the finding.

### The contract decision, with evidence

**BUMPED, 47 -> 48.** `effective_config_summary` is `asdict(cfg)` and
`snapshot.py:458` publishes it as the frame's `runtime_config` block, so the
question was whether these fields actually reached the emitted frame. They did —
measured, not inferred: `harness snapshot --json` under
`HERMES_HOME=X:\Eternia\.hermes\profiles\alice` (store root
`X:\Eternia\.hermes\agent-runtime`) returned `runtime_config` carrying ALL 29 at
contract 47, alongside `migration.counts.repo_bundles: 0`. Removing them edits
the wire, which is the S9/S10 rule, so the contract moves and the Launcher pin
moves in the same wave.

Note for a future reader: two of the 29 rode the wire as `"<redacted>"` rather
than their value (`live_run_max_total_tokens`, `mission_max_total_tokens` — the
`_redaction_safe_config` key filter matches "token"). They were on the frame
either way.

### `migration.counts.repo_bundles`, handled rather than dropped

The ruling said re-home or retire it honestly if it read through the store. **It
did not** — it was a direct `_count_nested_json(root / "repo_bundles")`
filesystem read, so a "does it call the store?" check would have left it
standing. It is RETIRED anyway, and the reason is recorded so it is not mistaken
for a scope overrun: the last writer went at S52, the checkpoint EntityClass row
went at S56, the module went here, and the live root has **no `repo_bundles/`
directory at all** — the row reported `0` by construction on a wire operators
read. There is nothing left to re-home it onto. The other seven counts are
untouched and pinned by exact keyset.

### Launcher role-envelope: the wire verification the ruling demanded

The ruling required proving which `role_envelopes` field the Launcher parses, and
stopping the sub-cut if it turned out to read a STILL-EMITTED one. There were
**two** parses — the top-level frame section (`MissionControlSnapshot`) and a
PLURAL field on agent rows (`MissionAgentLog`). Checked three ways against hermes
`8740d227f`:

1. `grep -rn role_envelopes agent_runtime hermes_cli tools` returns ONLY comments
   recording the S44 retirement. No emitter, top-level or per-agent-row.
2. `models.py` carries no `role_envelopes` field on any row that reaches the
   frame, so no `asdict` path can produce one either.
3. The LIVE frame settles it: `snapshot --json` contains **zero** occurrences of
   the string `role_envelope`.

**No contradiction with the scout — both parses were dead, and both were cut.**
The surviving `role_checklists.validate_checklist_payload_structure` belongs to a
DIFFERENT wire key and is untouched.

### New debt this wave OPENED

- ~~**Launcher: `roleChecklists` and `proofBatches` are the SAME dead-by-emptiness
  class.**~~ **CLOSED at Launcher s56** — cut whole in one pass with the sibling
  bullet below (models, BOTH parses each, the `_RoleTaskHud` container and
  everything reachable only through it, the three inert bridge forwards).
  ORIGINAL ENTRY: Both are parsed on `MissionAgentLog` and on the top-level
  frame; S44 deleted the store family that fed both, and the live frame carries
  neither. The `_RoleTaskHud` container in `agent_detail_terminal.dart` now
  renders only from these two, i.e. never. NOT cut: outside this wave's ruled
  scope, and hermes still keeps
  `role_checklists.validate_checklist_payload_structure` alive, which deserves
  its own look before the launcher side is swept.
- ~~**Launcher: the bridge still forwards `raw['role_envelopes']`**~~ **CLOSED at
  Launcher s56** — all three raw-section forwards (`role_envelopes`,
  `role_checklists`, `proof_batches`) went in the same pass. ORIGINAL ENTRY: a
  raw-section pass-through beside `role_checklists` / `proof_batches`. With no
  parse behind it the key is inert; removing the forward belongs with the sibling
  cut above rather than half-way through it.
- ~~**`agent_runtime/role_checklists.py` survives for one importer.**~~
  **CHECKED at s56 — the importer is LIVE PRODUCTION; the module STAYS, and no
  hermes-side cut was taken.** The chain is `hermes harness contracts
  verify-examples` -> `runtime_commands._cmd_contracts_verify_examples` ->
  `decision_contract_examples.verify_harness_skill_examples` ->
  `decision_contracts.validate_planning_decision` ->
  `decision_contract_registry.validate_payload_keys` ->
  `role_checklists.validate_checklist_payload_structure`. Run live on alice
  (`--json`, exit 0, `ok: true`, `contract_hash 20639a26…`, `event_count 58`), so
  the validator is reached by a payload the runtime produces on every typed
  decision, exactly as its docstring claims — the one seam in this campaign whose
  advertised caller turned out to be real. ORIGINAL ENTRY:
  `decision_contract_registry.py:177` imports
  `validate_checklist_payload_structure` from it. Worth checking whether that
  validator can still be reached by a payload the runtime produces, given S44
  deleted the store family around it.

  **The distinction that made the Launcher cut safe**, recorded because it is the
  kind of thing a future wave will re-derive: the surviving validator belongs to
  a DIFFERENT wire key. It validates a `checklist` block inside a DECISION
  payload. Nothing in `agent_runtime` / `hermes_cli` / `tools` emits a
  `role_checklists` or `proof_batches` frame SECTION — which is what the two
  Launcher parses read — and the live contract-48 frame contains **zero**
  occurrences of `role_checklist`, `proof_batch` or `role_envelope`. (The only
  two `checklist` hits in that frame are board-CARD checklists, an unrelated live
  concept.) A module surviving for one importer is not evidence that a wire
  section survives.

### Superseded pins INVERTED rather than deleted (each carrying why)

`test_config.test_config_loads_live_run_budget_fields` ->
`test_live_run_budget_config_loads_and_is_ignored`; `test_migrations`'
`rejects_bad_ceiling_order` / `rejects_bad_storage_watermark_order` -> the two
"the cross-check is gone with both its fields" cases;
`test_root_config_pinning.test_neko_extension_cap_resolves_from_root` ->
load-and-ignore, with its false claim quoted and the root-over-profile property
re-asserted on `lock_acquire_timeout_seconds` (the same retarget applied to
`test_swarm_config_resolves_from_root`'s tail);
`test_s56_config_block_removal`'s `root_node_mode` witness -> retargeted onto
`supervision.child_events_enabled`; `test_s52`'s three module-reaching pins ->
one module-absence pin plus a distinctive-name repo scan; `test_s24`'s "the READ
side deliberately survives" and `test_s56_worker`'s
`test_the_repo_bundle_store_read_side_survives` -> both inverted to absence;
`test_s43`'s `repo_bundles` entry -> moved into `RETIRED_WHOLE_MODULES` (its
fourth wave); `test_s56_worker`'s contract pin -> a FLOOR (`>= 47`) rather than a
second copy of the current number; Launcher-side the s54 contract pin -> `>= 47`,
and s48's `class MissionRoleEnvelope` survival pin -> `isFalse`.

Fixtures still SEND every removed key — the 29 scalars, `swarm`, the six S56
blocks, `role_envelopes` top-level AND on an agent row — so a stale producer
cannot resurrect a reader.

### A method note the removal tests needed

Three assertions in this wave initially went RED against a correct tree: they
scanned `inspect.getsource(...)` for a removed name and matched the RETIREMENT
COMMENT this wave had just written. Fixed by round-tripping the function through
`ast.parse` / `ast.unparse`, which drops comments and keeps every real name. Any
"is this symbol gone?" test over source text has this bug latent in it the moment
the cut documents itself; the helper is `_code_only` in
`test_s57_unruled_config_debt_removal`.

Related: a repo-wide `def <name>(` gate over the removed store methods is a false
positive machine — `update`, `get`, `_write` are ordinary names every store has.
The S52 pin was narrowed to the DISTINCTIVE subset (`create_or_update_from_task`,
`wake_ready_dependencies`, `owner_for_repo`, ...), which is what actually
discriminates a re-introduction from a coincidence.

### Also recorded

- `tests/agent_runtime/test_repo_bundles.py` was deleted with the module. Eight
  of its ten cases were already hollow shells (`assert not hasattr(TaskStore(),
  "create")`) and one was an inverted S52/S56 pin re-expressed in the new removal
  contract. The tenth, `test_assignment_signal_hash_includes_repo_bundle_id`, is
  LIVE and about a DIFFERENT store — `repo_bundle_id` is a component of
  `assignment_signal_hash` and therefore decides assignment identity. RE-HOMED
  into `test_persona_assignments.py`, appended byte-wise with CRLF endings and
  the bare-LF count verified unchanged (796 before, 796 after) per this ledger's
  own mixed-EOL warning.
- `tests/agent_runtime/test_delivery_directive.py`'s `_bundle` helper built a
  full 31-field `RepoBundle` to pass a `(task_id, bundle_id)` pair to the
  promotion-record read. Replaced with a `SimpleNamespace`. That coupling is
  exactly what made the model look load-bearing to a reachability scan.
- `checkpoint.EntityClass`'s docstring used `repo_bundles/<task_id>/...` as its
  example of a nested store. Retargeted to `self_tests/<task_id>/...`, a class
  that still exists.


## Tombstone-registry consolidation (both repos, 2026-08-01)

> Not a contract move — no wire, no event and no production symbol changed on the
> hermes side. This is the removal CAMPAIGN's own test debt being retired: the
> per-wave absence contracts collapse into one data-driven registry per repo,
> behind one shared AST scanner. Executed together with Launcher **s56**, the
> role-checklist / proof-batch cut recorded at the end.

### Why — the finding that forced it

Twenty `test_sNN_*` files on the hermes side, and two grep-gate files on the
Launcher side, each re-implemented the same question: *is this removed symbol
still absent from production source?* Two defects rode along with that
duplication, and both are recurrences, which is itself the finding.

**1. The vacuous-gate class — four instances, three of them this week.** A
removal test that greps raw source cannot tell a re-grown reference from the
retirement COMMENT the cut just wrote.

| Found by | The defect |
| --- | --- |
| S48 | `test_s46`'s token-join helper renders `card.title` as three newline-separated tokens, so every dotted-attribute assertion through it could never match and **passed vacuously**. |
| S57 | Three assertions went **RED against a correct tree** because they matched the retirement comment that wave had just written. |
| S40 (earlier) | Its repo-wide gate had already been red once for exactly the same reason. |
| Launcher | `_offendersForForm` grew a `startsWith('//')` heuristic to paper over it, which misses doc comments, trailing comments and block comments. That heuristic is why several Launcher rows had to be **weakened** to DECLARE/READ forms (`final int goalCount`, `.goalCount`) instead of banning the bare name — the bare name would have fired on prose. |

Each time the fix was the same: parse to an AST and re-render. Writing that fix
once, in a shared scanner, is what this consolidation is.

**2. Twenty different answers to "what counts as production source."** Six
package tuples were in use across the hermes removal tests — `agent_runtime`
alone; `+ hermes_cli + tools`; `+ gateway + agent + acp_adapter`; the
twelve-package tuple; `pkgutil.iter_modules` over `agent_runtime` top level only,
which does **not** descend into subpackages. A row's protection was whatever
tuple its author happened to pick. Both registries now have exactly one scope,
chosen as the WIDEST any migrated row used, so consolidation could only widen.
That was a real hazard rather than a tidiness point: S40's rows were gated over
every `.py` in the repo, so a six-package tuple would have silently dropped
`cron` / `mobile_core` / `providers` / `tui_gateway` / `apps` and the root
modules from that row's reach.

### The two registries

| | hermes | Launcher |
| --- | --- | --- |
| File | `tests/agent_runtime/test_tombstone_registry.py` | `test/features/mission_control/mission_control_tombstone_registry_test.dart` |
| Scanner | `ast.parse` -> strip docstrings on the tree -> `ast.unparse` | `parseString` (the real Dart front end) -> walk the TOKEN STREAM |
| Comment-immune because | comments never survive `ast.parse`; docstrings stripped explicitly | comments are not in the token stream at all — the scanner hangs them off `Token.precedingComments` and never links them into `next` |
| Row forms | `MODULE` / `ATTR` / `CLASS_ATTR` / `EVENT` / `CODE` / `PATH` | `name` / `dotted` / `literal` |
| Scope | S55's twelve-package tuple + repo-root modules | `lib/` + `tool/stagec_qa_mcp_server/lib/` |

**Both keep string literals in scope, deliberately.** Event kinds, wire keys and
QA capability ids are invoked BY NAME, never as identifiers — the S44/S55 lesson.
A registry that only saw identifiers would protect half the surface, and would
report every de-registered event type as absent while its emitter sat there
spelling it out.

**The Dart side takes the same mechanism as S48's, not S46's.** `ast.unparse` and
the Dart token stream both render dotted attributes AS dotted attributes, which
is precisely what S46's token-join could not do.

`analyzer` is promoted from a transitive to an explicit Launcher dev dependency;
it resolves to the same `9.0.0` the SDK already pulled in.

### Protection parity — the acceptance criterion, computed rather than asserted

Both sides were checked by a script diffing the **pre-change git blobs** against
the registry table, not by reading. Both BEFORE sets are deliberate
OVER-approximations — every symbol-shaped string literal harvested from the old
files' ASTs, including some that were never bans — so a false "unmapped" row
gets triaged by hand rather than filtered out silently. A filter is exactly how
a parity check goes vacuous.

**hermes: 750 BEFORE symbol-shaped literals across the 19 migrated files ->
451 registry row texts + 585 still asserted in the shrunk survivors + 15
superseded by a MODULE row + 8 old-scanner machinery + 4 subsumed by a broader
CODE row. UNMAPPED = 0.**

Three triage buckets are worth naming, because each is a real
protection-preserving mechanism rather than an excuse:

- **Superseded by a MODULE row (15, strictly stronger).** s43's
  `RETIRED_WHOLE_MODULES` recorded per-symbol verdicts against four modules a
  LATER wave then deleted whole (`role_envelopes` S44, `budget_approval` /
  `stage_intent` S45, `repo_bundles` S57). A banned MODULE makes every name on
  it unreachable, which is stronger than banning the names one at a time. The
  script ASSERTS each claimed supersession against the registry's module rows
  rather than trusting the mapping.
- **Old-scanner machinery (8, never bans).** `*`, `.git`, `.venv`,
  `node_modules`, `venvs`, `.md` and two pytest parametrize ids — glob patterns
  and skip-dir names from the old files' own file walkers.
- **Subsumed by a broader CODE row (4).** The CODE form matches by SUBSTRING
  against rendered source, so one bare-name row bans every shape containing it.

**That last bucket was a real gap the parity check FOUND, not a bookkeeping
note.** S47 had gated the config key as three separate source SHAPES scoped to
two modules — `raw.get("role_envelope")` and `role_envelope=` in `config.py`,
`getattr(cfg, "role_envelope"` in `migrations.py` — and none of them had a
registry row; the migration would have dropped all three. Closed by adding one
`Form.CODE` row for the bare name, which subsumes all three AND widens them
repo-wide. The text scanner could never have banned that bare name:
`role_envelope` still appears in **six** production files today, every one of
them a comment recording its own retirement. This is the consolidation paying
for itself.

**Launcher: 196 BEFORE subjects (the union of both old files) -> 197 registry
rows + 15 kept shape pins + 15 strengthened spellings. UNMAPPED = 0.**

Six Launcher rows are **strictly stronger** than what they replace: `goalCount`,
`workerSessionId`, `roleEnvelopes`, `activeWorkerSessionId`, `goal_count` and
`role_envelopes` are now banned as bare NAMES. The text scanners could not do
that, because every one of them still appears in `lib/` today — in prose
explaining its own removal. The registry also promotes six bridge wire keys
(`goals`, `archived_tasks`, `mission_level_state`, `mission_flow_timeline`,
`proof_gate_state`, `mission_plan`) from shape-only substring checks to real
scoped literal rows.

### Red-proof — run before any old file was deleted, in both repos

Not a claim; a sabotage. A scratch production file was added carrying BOTH a
re-introduced tombstone in real code AND a block of tombstoned names in its
docstring and comments.

- **hermes**, twice: a re-introduced NAME (`wake_ready_dependencies`) and a
  re-introduced STRING LITERAL (`render_objective`) each failed the gate naming
  their row, its wave and its reason — while eight tombstoned names in that same
  file's docstring and comments fired **nothing**.
- **Launcher**: a re-introduced literal (`role_envelopes`) and a re-introduced
  name (`roleChecklists`) each failed naming their row and line — while five
  tombstoned names in the comment and doc-comment block above them fired
  **nothing**. Exactly two offenders, both real code.

Both sabotages reverted. The comment-immunity half is ALSO pinned as a permanent
test in each registry, so it cannot silently regress.

### What is deliberately NOT a registry row

Absence assertions whose subject is a runtime SHAPE rather than a name, because a
name scan cannot honestly assert them and forcing them in would mean going back
to substring matching for the whole table:

- **parameter absence** — "passing `tasks=` must raise `TypeError`" is a
  signature fact, checked by calling.
- **wire-key absence** — "`build_status()` must not emit `repo_locks`" is a fact
  about a produced dict.
- **exact key-set / count pins** — `migration.counts`, `RunStore`'s public
  surface, `OPERATOR_SUMMARY_EVENT_TYPES`.
- **source SHAPES and scoped bodies** (Launcher) — `raw['goals']`,
  `_int(json['goals']`, the two interpolated HUD pill expressions, YAML written
  inside a Dart string, and `MissionGoalSummary`'s class-body-scoped `detail`.
- **reader-side config gates** (Launcher) — the `kStageCQaCommandBus*` allowlists
  and the MCP surface registry lists, which read live constants.

These stay in their per-wave files alongside the behaviour pins. Nothing was
dropped; the split is by what a row can honestly assert, and the header of each
registry says so.

### Files kept WHOLE, because they are rules or authorities rather than lists

`test_s15_event_contract_pruning` (the single `SURVIVING_EVENT_COUNT` /
`contract_hash` authority, imported by six other files), `test_s55` (the
registered-event -> emitter structural gate), `test_s56_runtime_config_reader_gate`
(the `RuntimeConfig`-field -> reader gate, whose `UNRULED_DEBT` s57 imports),
`test_s47` (`CURRENT_CONTRACT_VERSION`), `test_s53` (exports `seed_lane_row` to
s21), `test_s25_graph_prune_on_reap` and `test_s38` (characterization), the wire
tests, and the "historical rows still read back" family (de-registration gates
APPENDS, not reads).

### Superseded pins INVERTED rather than deleted (Launcher)

Eight assertions, each carrying why. s48 and s55 had both explicitly KEPT
`MissionRoleChecklist` / `MissionProofBatch` **as a decision** — s55's test was
even named "the sibling collections are deliberately KEPT" — and s56 reversed
both after verifying the hermes side. Keeping the reversal legible, rather than
quietly deleting a claim that turned out wrong, is the point. Two orphaned widget
tests were inverted the same way with their fixtures intact (see s56 below).

### Wave 4 — s1-s39 registry migration (executed 2026-08-01)

Cut commit: `f5cadc9cb`.

The previously deferred older layer is now consolidated. The pre-change tree at
`e05f1066a` contained exactly **50** s1-s39 files and **6,198** lines. The
survivors now contain **5,472** lines (726 removed), and three files whose whole
contract was absorbed were deleted. Behavior, historical-read, wire-shape,
exact count/hash, and characterization assertions remain in their original
surviving files; only name/module/event absence moved into this registry.

Protection parity was computed from the pre-change git blobs, not from the
shrunk working files. The deliberately broad harvest found **1,609
symbol-shaped literal occurrences** (1,287 distinct file/literal pairs):

- 542 occurrences map to an exact registry row;
- 1,037 map to a literal still present in a surviving assertion/authority;
- 30 were manually triaged, leaving **UNMAPPED = 0**.

The 30 manual rows are explicit rather than filtered: 18 are the fragments used
to concatenate s11's full persona-policy names (the constructed names are
registry rows); two s21 method names are superseded by scoped `CLASS_ATTR` rows
and `status.` is old scanner machinery; two s25 values and three s32 values are
fixture payloads; s29's `context_builder` fragment is superseded by the whole
`agent_runtime.context_builder` module row; and s5's two method names are
superseded by scoped `CLASS_ATTR` rows while `module_name` is the old pytest
parameter id.

Before those three files were deleted, a scratch production module supplied a
real `run_node` binding and a real `"render_objective"` literal. Each failed its
exact registry row and named the scratch file. Extra tombstones in the same
module's comments and docstring produced no offender. The sabotage was then
reverted and the deletions applied.

### Retirement rule — rows are never dropped silently

Written into both registry headers, and the two rules differ on purpose.

- **hermes**: a row may be dropped only after **one upstream sync has merged
  cleanly over that symbol's region**, and the sync commit is recorded on the row
  when it goes. This is a FORK: a tombstone here is not only "we deleted it", it
  is "upstream may still carry it, and a sync could hand it back". Until a sync
  has passed over that region without conflict, the row is doing work.
- **Launcher**: one stable month, because that repo has no upstream.

Dropping a row EDITS THE VISIBLE TABLE in both. No expiry, no allowlist, no
silent decay. **The next removal wave adds a ROW, not a FILE.**

### Launcher s56 — the role-checklist / proof-batch knot (executed with this wave)

The last dead-by-emptiness collection from the S44 store cut, filed by S57 and
ruled the same class as s55's `MissionRoleEnvelope`. Verified three ways before
cutting: a grep of hermes `agent_runtime` / `hermes_cli` / `tools` returns only
retirement comments plus ONE live import; the LIVE contract-48 frame contains
**zero** occurrences of `role_checklist`, `proof_batch` or `role_envelope` (its
only two `checklist` hits are board-CARD checklists, an unrelated live concept);
and the one live importer was traced end to end and **KEPT** — see the struck
S57 bullet above for the full chain and the distinction that made the Launcher
cut safe.

Cut in ONE pass (the s47-s49 precedent — never strand a non-compiling tree):
`MissionRoleChecklist` / `MissionRoleChecklistItem` / `MissionProofBatch`, BOTH
parses of each collection (top-level frame AND agent row), the `_RoleTaskHud`
container and everything reachable only through it (`_RoleChecklistRow`, the
three display caps, `_missionElidedJoin`), and the three inert bridge raw-section
forwards — `role_envelopes` went with its siblings, since a forward with no parse
behind it can only carry an absent key.

**The finding worth keeping.** The HUD's render condition was
`roleChecklists.isNotEmpty || proofBatches.isNotEmpty`, false on every live
frame, so an operator was told a "Role Task List" panel existed and could never
see it. Two widget tests were its last live references and BOTH are inverted with
their fixtures intact. One of them — F-7's cap-accounting proof in
`mission_capped_surfaces_test.dart` — is the sharpest instance of this campaign's
recurring shape: **it was green only because its own repository fixture hand-fed
an over-cap `role_checklists` / `proof_batches` payload that no producer emits.**
A fixture manufacturing the very data whose absence made the surface dead. Its
inversion now asserts the Run Inspector still mounts (non-vacuity) BEFORE
asserting the HUD does not.

## S58 contract wave — Wave 3 orphan audit (contract 48 -> 49, 2026-08-01)

Executed in the campaign cut commit recorded in the tombstone registry: eight
receiver-verified production orphans, the 21 redundant top-level Harness import
bindings, and the byte-for-byte duplicate `runtime_config.migration` frame block.
The authoritative top-level `migration` block remains. The Launcher had no
reader of either migration copy; only its supported-contract pin and fixtures
move in lockstep.

Deliberately kept and filed for operator ruling because these are persisted-row
or historical projection surfaces, not safe name-only cuts:

- `PersonaInstanceStore.ensure_for_goal` and
  `PersonaAssignmentStore.list_for_goal`, `attach_run`, `attach_proof`, and
  `record_context`: zero production callers at this HEAD, but each creates,
  reads, or mutates persisted assignment/instance row shapes used by historical
  compatibility tests.
- `SelfTestEvidence.worker_session_id`: a disk-schema field read from existing
  evidence JSON and accepted from historical progress payloads.
- `PersonaAssignment.repo_bundle_id` and its signal-hash/projection chain: a
  persisted assignment field with explicit historical round-trip coverage.
- `task.transition` trace branches in `persona_chat_history.py`: a historical
  event projection; removing it would change rendering of old event-log rows.
- Observability allowlist keys `worker_session_id`, `worker_state`,
  `worker_heartbeat_age_seconds`, `possession_state`, and `lease_owner`:
  projection compatibility keys. In addition, `tools/kanban_tools.py` still
  stamps `worker_session_id`, so the prompt's implied zero-producer premise is
  false at this HEAD.

## S59 / Launcher w3 — Round 2 dead-code closeout (2026-08-02)

Executed in hermes cut `799249fbf` and Launcher cut `68d9449a`. The registries
gain 14 hermes rows and 12 Launcher rows; both additions were red-proved with a
scratch production offender, then proved comment-immune, then reverted.

### The Wave-1 L2 ruling is explicitly reversed

Round 1 reported `MissionControlCounts` as "remained live." That was wrong and
the deviation existed only in chat, which is itself a ledger defect. The
producer set is closed: `SnapshotSummary` emits only `persona_instances`, while
the Launcher bridge read four impossible summary keys and converted all four
absences into zero. Its only consumer then compared the invented
`running_agents == 0` with real running agent rows and emitted a false parity
warning. Launcher w3 removes the class, snapshot field/parse, bridge synthesis,
count-mismatch alert and drawer row, and the mapper's false `counts: summary`
lockstep claim. The four obsolete literals are scoped tombstones.

### Hermes S59 cuts and deviations from the brief

- Removed the dead `ChatBusyError`, `_live_chat_binding`,
  `_terminate_live_chat_binding`, `_chat_busy_payload`, and all imports and
  classifiers. The order named three catch arms and two import bindings; HEAD
  actually contained **five** catch arms plus a third import and an
  `isinstance` classifier in `harness_support.py`. Receiver-aware AST and string
  scans found no raiser, caller, `getattr`, or string dispatch, so the complete
  cluster was cut. The separate `PersonaChatBusyError` continuity lease remains
  untouched.
- Removed `get_persisted_persona`; its sole remaining importer was its own
  test. The base-profile test now reaches the persisted merge behavior through
  the live `ensure_persisted_personas` authority.
- Removed `read_bundle_promotion_record`, `bundle_promotion_record_path`,
  `repo_bundles_dir`, and `repo_bundles_task_dir` after receiver-aware tracing
  confirmed that the first pair called only each other/tests and the path pair
  served only that closed loop.
- Repaired `delta_batch.json` minimally to contract 49, removed its stale
  nested `runtime_config.migration`, `role_envelope`, `mission_plan`, and
  `open_incident_warning_threshold`, copied it byte-identically to Launcher,
  and updated both manifests. The agent-runtime fixture gate now pins all three
  frame-bearing goldens plus the live hydrate fixture to contract 49.
- Made the noninteractive-git environment test deterministic by setting hostile
  ambient GCM/Git values and proving the helper hardens only its returned copy.
  The whole-file CLI timeout was not suite-order pollution: a renderer-only
  doctor test invoked the real worktree janitor, which ran `git diff` across the
  operator's registered worktrees. It now injects the minimal doctor report
  whose rendering it actually tests.

Mixed-EOL proof for `tests/agent_runtime/test_persona_assignments.py`: before,
4,427 CRLF and 796 bare-LF endings; after the intended one-line deletion, 4,426
CRLF and 796 bare-LF endings. No whole-file normalization occurred.

### Launcher 27-lead receiver-aware verdicts

Five leads were CUT as test-only closed loops:
`missionAgentLatestChatHistoryEntry`, `missionAgentLatestChatSessionId`,
`MissionConversationNavigationPolicy`,
`buildMissionPetPickerDialogForTest`, and
`runtimeOverviewGraphForSelection`. The navigation policy's private result and
reason types died with it. Petdex coverage now instantiates the living shared
`PetdexPickerDialog` directly; graph tests call the living runtime projection
authority directly.

Twenty-two leads were KEPT with these living production callers:

- `AgentRuntimeContextRow`, `AgentRuntimeDetailsView`, and
  `missionContextOccupancyLabel`: `agents_drawer.dart` builds the details view,
  which builds the context row, which formats the occupancy label.
- `missionAgentHasRuntimeGraphContext`,
  `missionAgentIsCoordinatorPersona`,
  `missionAgentIsRelatedRuntimeInstance`,
  `missionAgentRuntimeGraphInstances`, `missionAgentRuntimeHomeOwner`,
  `missionAgentRuntimeUpstreamStageFor`,
  `missionAgentRuntimeUpstreamStagesFor`, and
  `missionAgentGraphPointerForPersona`: internal edges of
  `missionAgentRuntimeGraphProjection`, rooted by the instance picker, Mission
  Control page/flow editor, and agents drawer production projections.
- `missionAgentStatusToChatStatus`: live adapter construction for both
  configured and free-floating chat agents.
- `resolveBoundNodeDisplay`: called by the production
  `BoundNodeDisplayResolver.resolve` path.
- `bridgeErrorCodeFromString`: called by bridge-error envelope parsing.
- `missionTimestampUtc`: called by the persisted history `createdAtUtc` and
  `updatedAtUtc` getters.
- `missionOfficeRenameDraft`: called by the office rename submission path.
- `agentFlowRoster`: called by the live flow projection.
- `MissionPersonaNewChatAction`: the callback type of two production picker
  surfaces.
- `missionChatHistoryQaLabel`: generates the live history-row QA label in the
  instance picker and is reachable through the Stage C QA surface.
- `missionConversationBodyStateFor`: called by the production conversation
  model's `bodyState` getter.
- `missionLocalRowOrderInputs` and `missionPendingRowOrderInputs`: called while
  constructing ordered local and pending transcript rows.

No kept item relies solely on a test import, string-keyed dispatch, or a
test-only QA seam.

### Discovered but not cut (contract authority required)

- Removing the last worker-chat replacement path also exposed
  `RunStore.cancel` -> `close_run` -> `run.closed` as production-callerless.
  `run.closed` is a registered event contract, so this campaign did not remove
  the methods or event. The stale S17 liveness prose and behavior-test name were
  corrected to describe an explicit compatibility hold pending an operator
  event ruling.
- The full agent-runtime gate exposed one more superseded survival assertion in
  S49 that still named the deleted persona replacement caller. It was removed;
  S17's compatibility-hold behavior test is the sole pin for `run.closed` now.
- The current hydrate and delta fixtures still carry older configuration keys
  (`mission_plan` / `open_incident_warning_threshold`, and `role_envelope` in
  delta). Item D authorized the stale `delta_batch` repair, not a broader
  golden rewrite. They are filed here for a later contract-fixture ruling.

Housekeeping: the landed Launcher Round-1 worktree registration and 13 stale
Temp registrations were removed/pruned. Windows denied deletion of the old
unregistered Round-1 directory, so it was preserved rather than manually
deleted. Hermes `rescue/wave4-20260801` was verified as an ancestor of `main`
and deleted locally; no origin branch existed.

## S60 / Round 3 — rule-first dead-code closure (2026-08-02)

Round 3 removed two remaining Hermes declaration-only surfaces after tracing
receivers and string dispatch, not just symbol counts:

- `harness_support.ERROR_EXIT_CODES['agent_busy']` was a stale compatibility
  literal. The live busy refusal is `chat_busy`, backed by
  `PersonaChatBusyError`; that lane is unchanged.
- `persona_assignments.LIVE_RUN_STATES` had no production reader. Its former
  run-liveness role had already moved elsewhere, so the constant and its now
  unused `RunState` import were removed.

The stream-fixture gate now compares the nested `runtime_config` key set for
every frame-bearing golden (`hydrate`, `delta`, and `delta_batch`) with the
live hydrate core. Contract 49 fixture repair was deliberately key-only:
`mcp_admission`, `mission_chat`, `persona_chat`, and `terminal_envelope` were
added; `mission_plan` and `open_incident_warning_threshold` were removed; the
delta-only `role_envelope` residue was removed. Retained values and every
non-`runtime_config` byte-equivalent JSON value stayed unchanged, and the three
goldens remain byte-identical across Hermes and Launcher.

### Test janitor containment

Agent-runtime tests now pin both janitor base resolvers beneath each test's
`tmp_path`; production fallback behavior requires an explicit fixture opt-in.
The production default itself is unchanged. This is a safety boundary, not a
new runtime policy: a renderer-only or unrelated test can no longer enumerate
or reap `%TEMP%\hermes-agent-wt` or the operator's registered worktrees.

The production janitor's global `%TEMP%\hermes-agent-wt` fallback remains a
real blast-radius concern: it treats every sufficiently old directory under a
shared machine-wide root as inventory. That behavior is held for compatibility
in this campaign; changing it needs an operator ruling and a migration plan,
not a test-suite side effect.

### Report-only holds

- `RunStore.cancel` -> `close_run` -> `run.closed` remains a registered event
  compatibility chain. Do not cut it without an event-contract ruling.
- `_safe_operator_reason` remains a policy boundary despite its low direct
  call count.
- The typed error chain, parity module, and remote gateway client remain
  architectural seams, not dead-code candidates.

### Round 3 proof

- `tests/agent_runtime`: 4,216 passed / 1 skipped. The +4 versus the measured
  4,212 baseline is the nested stream-golden coverage, janitor boundary guard,
  and production-fallback characterization; no test disappeared.
- Canonical isolated CLI runner: 3,708 passed / 0 failed / exit 0. Nine
  timeout-affected files passed the runner's one-worker bounded retry.
- Live installed runtime (`profiles/alice`): snapshot schema 2, parity contract
  49, 2 boards, 15 persona instances, top-level migration present,
  `runtime_config.migration` absent, warnings empty, and zero occurrences of
  all cut names; status health `ok`; Neko chat `round3-20260802` completed.

## S61 — Profile-owned persona behavior and closed-loop retirement (2026-08-02)

The operator ruling is now structural: SOUL/profile/persona configuration is
the sole behavioral-prompt authority. The runtime no longer compiles role-name
branches for Neko, QA, or developer voices. Mission chat still supplies one
universal identity and operative/tool contract, so arbitrary custom roles keep
the same chat capability without inheriting a built-in persona. A byte-parity
test proves that changing only the role label does not change the runtime
surface message.

The unused `GPTPersonaRuntime.chat_reply` free-chat pipeline and its prompt and
voice helpers were removed. `mission_chat_reply` is the only runtime chat turn.
Likewise, `mission_chat` is the sole governed terminal-envelope lane; the
retired mission-worker, mission-node, root-node, and persona-chat aliases no
longer manufacture grants or typed refusals. Unknown/stale lane names are
ignored rather than becoming an authority surface.

Task-era persona residue was retired from persona instance, assignment, and
runtime-instance stores. The same pass removed independently proven closed
test loops: board default/archive wrappers, event-log convenience iterators,
read-model integrity wrapper, incident open-list wrapper, volatile-tail row
wrapper, diverged-binding wrapper, singular queued-skill wrapper, stream frame
selector, and the standalone basic redaction scanner/status pair. Their live
underlying readers, batch paths, and shared redaction patterns remain.

`canonical_mcp_server_yaml` was promoted rather than cut: MCP template-drift
issues now embed its exact pasteable YAML in the operator fix hint. The hint no
longer tells an operator to invoke an internal Python helper.

At the end of this first cut, MCP role config, run-progress/self-test evidence,
and `RunStore.cancel` through `run.closed` were held for an explicit contract
ruling. The contract-authority cleanup below records the subsequent decision
and retirement. No unnamed decorative constant was removed without an
independently proved closed reachability loop; those candidates remain deferred
for a named audit.

Red proof was captured before production cuts: the S61 registry additions
failed 38 rows (759 passed). Green proof is recorded in the campaign handoff;
on this Windows worktree the Bash wrapper cannot resolve its WSL/git worktree
path, so focused verification uses the repository Python pytest environment
and reports the wrapper limitation rather than claiming it ran.

### S62 follow-up — remove surviving compiled topology

Adversarial review found three active authority leaks after S61. Prompt
observability still emitted a fixed Neko/two-developer `default_flow` and
manufactured a Neko context for an empty instance roster. Operator
conversations mirrored child assignment traces into a persona selected by id
and synthesized every task thread beneath a Neko parent. Raw profile instances
were also rewritten to the `alice_supervisor` role for visibility resolution.

Integrated ancestor `f813115c8` removes those assumptions. Prompt observability now contains
only live instance contexts and omits absent flow. Operator conversation
ancestry follows the persisted `PersonaInstance.steered_by` primary-parent
chain; an absent/out-of-roster parent or cycle degrades to a standalone thread,
and child trace remains on the child channel. Profile visibility preserves the
raw profile identity and role, or overlays the matching persisted persona's
configured role/tool surface without rewriting the raw id. Custom-role and
empty-roster behavior tests pin all three boundaries. The read-model crash test
now executes SQLite's `PRAGMA integrity_check` directly, preserving storage
proof without restoring the deleted production convenience wrapper.

## S61 / Round 4 — contract-authority cleanup (2026-08-02)

The operator ruled the three Round-3 holds removable. The root MCP `roles`
table and `mcp_not_admitted_for_role` vocabulary were inert after S11 made
profile declarations authoritative, so parsing/serialization and stale
guidance are gone. Role and lane remain informational admission metadata.

The production-callerless task progress island (`RunProgressSink`,
`dev_discipline.py`, `self_test_evidence.py`) retired with its self-test store,
checkpoint/migration row, delivery evidence-id field, and the two self-test
write contracts. Live `self_test_command` / `focused_self_test` packet fields
and `ChatProgressSink` remain. Historical `self_test.recorded` display handling
is intentionally preserved.

`RunStore.cancel` / `close_run` and the `run.closed` write contract also
retired. Historical `run.closed` rows still receive the existing operator
summary. The event catalog moves 58 → 55 and snapshot contract 49 → 50.

## S64 — Integration-review authority cleanup (2026-08-02)

The compiled profile-chat fallback toolset table is removed. A profile-backed
persona now receives declarations from an exact persona id first, or from a
profile match only when that match is unique. Ambiguous shared-profile matches
inherit nothing. This closes the last runtime path where a role label could
manufacture capabilities absent operator-owned persona/profile data.

The remaining `alice_supervisor` ⇄ `neko_supervisor` spellings are retained as
wire/config compatibility only: persisted role-envelope keys and historical
decision-contract values still need to decode. They do not select a behavioral
prompt or restore a compiled profile toolset. Similarly, existing `neko.*`
decision-contract ids remain public protocol vocabulary, not an implicit Neko
persona system.

The production-callerless repeated read/search policy fields and their closed
guard branches are retired. Live API-call, token, MCP-call, repeated-tool, and
wall-clock accounting remain. QA readiness no longer injects `launcher_qa`
from a role name; an MCP dependency exists only when the persona/profile
declares it. Historical `run.closed` events remain readable through the typed
observability projection even though no live writer remains.

Twenty-two obsolete attribute tombstones that imported the now-deleted
`dev_discipline` / `self_test_evidence` modules were removed. Their stronger
S63 module tombstones already protect the complete deleted surfaces; retaining
attribute imports made the registry fail before it could inspect live code.

Stream fixtures are now regenerated by
`scripts/generate_agent_runtime_stream_fixtures.py` from the current production
builders in an isolated root. Hydrate, delta, and delta-batch carry one exact
core, including current parity capabilities and completeness semantics, and
the generator rejects any reappearance of the retired `default_flow`.

## S65 — final contract-island and historical-store closeout (2026-08-02)

The final reachability pass retired the structured `AgentDecision` contract
island: its schema, payload/role contracts, examples, packet/scope helpers, and
harness decision/example dump paths had no production entry point. The shared
registry remains as the event-contract authority used by `EventLog.append`.
Historical event rows remain readable; only new writes are validated.

Task-era persona APIs (`ensure_for_goal`, goal lookup, run/proof attachment,
context recording, and task-owner release inference) retired after production
callers converged on chat-owned persona instances. Their goal/task path helpers
went with them. Placement retirement and office cleanup remain live, as do the
`TaskStore` compatibility reader and all chat/history steering APIs.

`RunStore` and `IncidentStore` are now explicitly historical readers. Their
caller-less update/list-by-task and incident open/close writers were removed,
along with four now-unemittable event registrations. Tests that exercise read
projections seed representative historical rows directly instead of preserving
a test-only production writer.

The status/snapshot/checkpoint pass removed empty task branches, stale
task/proof/goal/daemon fingerprints and migration counts, the packet-artifact
checkpoint/sync exclusion, and the test-only run-summary conversation
projection. Live persona chat history and event trace messages remain the
operator-channel source. Inactive coordinator tokens retired while
`mission.chat.message`, `re_route`, `update_profile`, and `set_model` remain.

The event catalog moves 55 → 51 and snapshot parity contract 50 → 51. The
Launcher-facing legacy contract field names remain compatibility aliases, but
their hash now comes from the event-only registry. S64 tombstones protect the
deleted modules, methods, path helpers, event types, and inactive action
vocabulary from accidental resurrection.

## S66 — Round-4 audit gap closure (2026-08-02)

Nine items from a read-only audit of the round-4 persona de-hardcoding
campaign. **No wire, event or contract moved**: parity contract stays 51, the
event catalog stays 51, `contract_hash()` stays
`4a55b49fce311a450ac568e593a853d3524b4bd9842a638934d406b613822c07`. One
Launcher file moves in lockstep (the CLI contract fixture) because a CLI verb
retired.

### 1. `harness persona-instance sweep-orphans` was BROKEN ON MAIN — RETIRED

Not dead code: a **live operator verb that raised `AttributeError` on every
invocation**, while the Launcher's committed CLI contract advertised it with a
full flag set. `f9aa0faab` (S65) deleted
`PersonaInstanceStore.sweep_orphaned_task_bound_instances`; the caller survived
as the first statement of `persona_commands.py::_cmd_persona_instance_sweep_orphans`
and the subparser stayed registered at `harness.py:883`.

**Intent, established from the tree rather than guessed — RETIRE.** Three
independent proofs, all inside that one commit:

1. The janitor's whole decision basis went with it —
   `_persona_instance_owner_release_state` → `_owning_task_release_state` →
   `paths.task_path` / `goal_path` / `goals_dir`. The `goals/` store does not
   exist, so a task-bound instance's owner can never be resolved again.
2. S65's own ledger entry names the cut: *"task-owner release inference
   retired"*.
3. **It is unrestorable without a contract move.** The reap emitted
   `persona_instance.reaped`, which the same wave DE-REGISTERED — `EventLog.append`
   would now refuse it. Restoring the verb means re-registering an event, i.e.
   an operator ruling, not a repair.

Executed: handler and subparser removed, each replaced by a comment carrying the
reason. `persona instance retire` is the live end-of-life verb and is untouched.

**Proof, both directions, against a throwaway `HERMES_HOME` (never the live
root).** Pre-fix, HEAD code: `AttributeError: 'PersonaInstanceStore' object has
no attribute 'sweep_orphaned_task_bound_instances'`. Post-fix: argparse rejects
with exit 2 and lists the twelve surviving verbs including `retire`; the sibling
`persona instance retire --help` still resolves on the same scratch root.

Launcher lockstep: `test/features/mission_control/fixtures/hermes_cli_contract.json`
regenerated — **149 → 148 command paths**, a 37-line surgical deletion, nothing
else in the file moved.

### 2. 104 uncovered cut symbols — 32 rows added, two stale rows corrected

An independent audit found 104 of 382 named cut symbols in
`4a21f0779..597715ba5` with no covering registry row.

**The cause is worth keeping, because it will recur: s65's `Form.MODULE` rows
protect modules DELETED WHOLE.** `decision_contract_registry.py` was **gutted**
— 918 lines removed, the file kept as the event-contract authority — so not one
of the twelve public names it lost was covered by anything. *A survivor module
needs symbol rows.*

Rows added (each verified genuinely absent before rowing, then red-proved):

- `decision_contract_registry`: `DecisionContract`, `HudShape`,
  `ObjectContract`, `agent_decision_json_schema`, `canonical_role_value`,
  `context_expansion_shape_ids`, `decision_contract`, `hud_shape`,
  `hud_shape_index_for_stage`, `payload_contract`, `validate_object_payload`,
  `validate_payload_keys` (12, ATTR).
- `AgentRole.ALICE_SUPERVISOR` / `.DEV` / `.QA` — **the campaign's central cut,
  which had no resurrection guard at all.** Scoped `CLASS_ATTR`, because bare
  `DEV` / `QA` are un-rowable: they are ordinary words in live code.
- `paths.self_tests_dir` / `self_test_task_dir` / `self_test_record_path` /
  `self_test_artifacts_dir` — the exact siblings of `goals_dir` / `goal_path` /
  `task_path` / `packet_artifacts_dir`, which WERE rowed in the same commit.
- `store.TERMINAL_RUN_STATES`; `profile_runner.PATCH_TOOLS` /
  `READ_SEARCH_TOOLS`; `models.PersonaInstance.prompt_contract_hash`.
- S66's own: `sweep_orphaned_task_bound_instances`,
  `_cmd_persona_instance_sweep_orphans`, `sweep-orphans`, `verify-examples`,
  `_cmd_contracts_verify_examples` (CODE); `run_lock`, `incident_lock`,
  `emit_incident_remove`, `verify_registry` (ATTR).

**`READ_SEARCH_TOOLS` is deliberately an ATTR row, not CODE**: the name is LIVE
in `model_tools.py`, so a repo-wide ban would fail on correct code. The scoped
attribute absence is what actually discriminates. Same reasoning as S57's
`root_node_mode` note.

Two stale row artifacts corrected rather than dropped:

- **s16 `decision_contract_registry.DecisionContract.allowed_roles`** asserted an
  attribute on a class this range deleted outright. Kept (the fork retirement
  rule wants a clean upstream sync first) and superseded by the new
  `DecisionContract` ATTR row, with the supersession written onto the row.
- **s44's reason** still claimed `validate_checklist_payload_structure` was live
  "because `decision_contract_registry.validate_payload_keys` calls it, live via
  `hermes harness contracts verify-examples`". **Both the caller and the verb are
  gone.** Reason corrected in place; the rows stay.

### 3. The registry's own no-silent-decay guarantee, restored

This range ADDED two silent-skip guards that turned a mis-scoped row into a
permanent pass — `if find_spec(dotted) is None: continue` (ATTR) and
`if owner is None: return` (CLASS_ATTR). The header promises "no expiry, no
allowlist, no silent decay"; these were exactly silent decay.

The legitimate case they reached for is real — a later wave often retires the
whole owner module, a STRICTLY STRONGER absence. S66 keeps that case working but
makes it **prove itself**: the stronger absence must be a row in this same
table. `_module_row_covers` accepts the module row or any ancestor package row;
for the class-deleted-but-module-survives case, `_attr_row_covers` requires a row
banning the class itself. Anything else FAILS, naming what to add.

Red-proved both arms before reverting: an ATTR row mis-scoped to
`agent_runtime.state_patches_TYPO` and the s16 CLASS_ATTR row with its covering
ATTR row removed each failed with an actionable message. **Under the old guards
both passed silently.**

### 4. Four orphans cut; and the gate that would have surfaced the fifth

Receiver-aware verification found **zero** production references repo-wide —
not even a test — for `locks.run_lock`, `locks.incident_lock` and
`state_patches.emit_incident_remove`. These were orphaned by S65's OWN cuts:
`RunStore` / `IncidentStore` became historical READERS there, and a reader takes
no write lock; `IncidentStore.close` was `emit_incident_remove`'s only
chokepoint. `decision_contract_registry.verify_registry` was test-only — its one
caller restated an event count the line above already asserted off
`event_catalog()` (the ledger's settled closed-loop rule). All four cut.

**`patch_coverage.COVERED_DOMAIN_EVENT_TYPES` KEPT, but no longer flat.** Its
`incident.closed` / `persona_instance.reaped` entries are a recorded intentional
cross-stack fixture-compatibility keep — but the flat set could not tell them
apart from live entries, so a fold vocabulary was outliving its producer
invisibly. Split into `LIVE_COVERED_DOMAIN_EVENT_TYPES` and
`HISTORICAL_COVERED_DOMAIN_EVENT_TYPES`, gated BOTH ways by
`test_stream_patch.py::test_every_covered_domain_event_is_registered_or_declared_historical`:
every LIVE entry must be in `event_catalog()`, every HISTORICAL entry must be
OUT of it. So a new entry cannot be invented without a producer, a
de-registration cannot happen out from under a live entry, and a resurrected
contract forces its entry back onto the live half.

### 5. Coverage this range deleted while the surface stayed live — re-homed

`operator_channels._apply_conversation_cap` is LIVE (called at `:636`), but its
only test was deleted with the `run_summaries=` vector it fed:
`grep -rn "turns_collapsed\|collapsed_count" tests/` returned **0**.

Re-homed onto the CURRENT live vector, not the removed one. The finding that
made this non-obvious: **the trimmable kinds are produced by the TRACE lane, not
by history.** History rows project to `operator_message` / `reply` /
`system_message`, every one of which is PROTECTED — so a history-driven test
could never reach the cap. The re-homed tests drive it the way production does:
a long tool trace with an operator prompt and a final reply around it. Three
tests assert the marker, `refs.collapsed_count` (derived, not restated), the
protected/trimmable partition, that the NEWEST turns are the ones kept, dense
`seq` re-stamping, and the `accountant.drop("turn_cap_trimmed", …)` accounting.
A non-vacuity guard asserts the marker is ABSENT below the cap.

### 6. A test helper that swallowed the argument its suite existed to vary

`test_mcp_admission.py`'s `_cfg` did `kwargs.pop("roles", None)` with a comment
conceding it was a regression. 43 call sites fed a discarded argument, collapsing
the wrong-lane, non-`mission_chat`-lane, wildcard-expansion and
`roles={}`-vs-`roles=_QA_ALLOW` contrasts into the same test.

The pop is gone and all 43 dead arguments with it; `_cfg` now forwards straight
to `McpAdmissionConfig`, so a stale caller gets a loud `TypeError`. **Six tests
DELETED** because their subject no longer exists (each was byte-equivalent to a
surviving happy-path case): `test_role_absent_from_config_still_admits_declared_server`,
`test_lane_absent_from_role_config_still_admits_declared_server`,
`test_legacy_wildcard_config_does_not_change_profile_authority`,
`test_an_allowed_but_undeclared_server_is_never_admitted`,
`test_dev_admitted_under_a_qa_only_config_uses_profile_authority`,
`test_legacy_empty_role_list_does_not_hide_a_declared_servers_manual`. Two more
collapsed into one; three renamed onto what they now prove. Several docstrings
were asserting a role floor that does not exist.

**Two production defects surfaced by that pass and fixed here.**
`mcp_admission.py:125`'s comment still said "Declared + role-admitted".
`mcp_admission.py:671` was worse — **live wire text** an operator and the agent
both read on a denied turn: *"'{name}' was requested by role '{role}'"*. Nothing
requests by role since S64; the request is `required_mcp_servers` ∪ the
profile's `mcp_servers` block. Being told the wrong authority denied you sends
the fix to the wrong file. Same class as the campaign's recurring
"prose asserting a reader that does not exist" — one level worse, because it
ships.

### 7. The stream goldens were machine-local, not deterministic

`hydrate` / `delta` / `delta_batch` embed `repo_scopes.{harness,frontend,backend}.resolved`,
produced by `resolve_affected_repo_workdir()`, which probes **hardcoded absolute
developer-machine paths** (`repo_context.py:907-923`). The generator's docstring
claimed "byte-reproducible across machines". It was not.

Fixed with **zero byte change**: `resolved` is pinned to the value the committed
goldens already carry, so all nine files stay byte-identical across hermes and
the Launcher and no manifest moved. Measured on a simulated foreign box (only
the two Eternia alias roots made to report `is_dir() == False`): unpinned
`hydrate.json` hashed `7095f747…` against the committed `0259c26c…`; pinned, it
reproduces `0259c26c…` exactly. Docstring corrected to state what is now true and
name what had been machine-dependent (`harness` never was — it resolves via
`Path(__file__).parents[1]`).

**A correction to the audit's stated mechanism, which matters more than the fix.**
The audit expected CI to go red. It would not: each repo's manifest is
self-consistent and **no test invokes the generator**. The real exposure is
worse because it is SILENT — a regeneration on a machine lacking the checkouts
rewrites a byte-pinned cross-repo golden, and nothing anywhere turns red.

`FRAME_FILES` pinned 8 files while `main()` regenerated 4. Now structural:
`GENERATED_FRAME_FILES` + `PINNED_ONLY_FILES` (each with its reason), with
`main()` asserting the generated set so a frame cannot silently drop out of it.
The split is not effort — it is structural: `patch_remove.json` is the
`incident.closed` remove fold whose event S65 de-registered with its last
writer, so no live producer exists.

### 8. Stale claims, dead parameters, and one real bug

- **A REAL BUG, live-reachable via `POST /api/profiles/{name}/promote`:**
  `personas.py::promote_profile_to_persona` called
  `profile_chat_toolsets(profile_name)` with no persona list, so `declared` was
  always `[]` and **every promoted persona was minted with zero toolsets**. The
  declared set was two branches up the whole time (the `known` map). Fixed.
- **A permission-path alias with no ruling behind it.**
  `terminal_envelope._ROLE_ALIASES` carried a THIRD entry, bare
  `"neko" -> "alice_supervisor"`, feeding
  `resolve_terminal_envelope_grants`. The S64 ruling covers only the
  `alice_supervisor ⇄ neko_supervisor` pair. Removed. It was inert — the live
  roster is `alice_supervisor` / `dev` / `profile` / `qa` and the configured
  grants are `dev` / `backend_dev` — which is precisely why it sat unnoticed.
- **A provenance report that renamed what it found.**
  `config.describe_runtime_default_authority` reported a pin persisted under
  `alice_supervisor` as `"persona_id": "neko_supervisor"` — an operator could not
  find the key by searching for the name the report gave them. Now reports the
  PERSISTED spelling, with the alias in a separate `persona_id_alias` field.
- **Un-exclusion has no role backstop — say so.** `chat_lane_toolsets` and
  `config.chat_lane_restore_toolsets` both justified safety with "(role gating
  still applies)". It does not: `personas.validate_toolsets` is a pure dedupe
  with no ceiling. The conclusion still holds, but ONLY via un-exclusion
  semantics. Restated so a future reader does not relax un-exclusion believing a
  backstop exists.
- **Dead parameters removed through the chain:** `resolve_mcp_admission(task=,
  stage=)` → `_requested_servers` → `_effective_required_mcp_servers`. Ignored at
  the bottom, passed by no production caller.
  `profile_readiness_for_persona`'s `task`/`stage` are KEPT and now documented as
  deliberately ignored: `test_profile_readiness_requires_explicit_mcp_declaration_for_visual_scope`
  calls it WITH a visual-scoped task to prove a work description can no longer
  manufacture a `launcher_qa` requirement. Removing them would delete that pin's
  only vector.
- **Unused parameters removed:** `validate_toolsets(role, …)` and
  `blocked_tool_names(persona)`. The latter returned the same constant for every
  argument — a constant dressed as a per-persona lookup. Its test was renamed
  from `test_sample_personas_keep_persona_blocklists` to
  `test_the_chat_blocklist_is_runtime_wide_not_per_persona`: the old name and its
  loop promised a contrast the runtime stopped drawing when the per-role deny
  tables went.
- **Ambiguous shared-profile ownership is now ACCOUNTED, still fail-CLOSED.**
  `profile_chat_toolsets` returned `[]` indistinguishably for "two personas claim
  this profile" (a configuration defect) and "no persona claims it" (normal).
  Split into `profile_chat_toolset_resolution` returning a typed reason, with an
  operator-visible warning on the ambiguous arm. **Behaviour unchanged.**
- EOL: the two files this range newly mixed are normalized to pure CRLF —
  `env-determinism-audit.md` (715/13 → 728/0) and `test_stage19_visibility.py`
  (31/1 → 32/0). Every other touched file was byte-verified against its HEAD
  blob: **no file flipped its dominant ending.**
- Fork boundary: `scripts/generate_agent_runtime_stream_fixtures.py` is a
  fork-added file in the upstream-owned `scripts/` directory and had **no row**
  in doc 17. Added.

### Method notes worth carrying forward

- **`grep -c $'\r$'` under Git Bash on Windows lies.** It reported
  `test_tombstone_registry.py` as 2,912 CRLF / 0 LF; the file is pure LF in git
  and on disk. Every EOL claim in this wave was re-measured byte-wise in Python
  against the HEAD blob. A wave that trusts that grep will "prove" it preserved
  endings it never measured.
- **A sabotage that does not apply is not a red-proof.** The first AgentRole
  sabotage patched a CRLF pattern into an LF file, matched nothing, and the gate
  went green — which reads exactly like a passing red-proof. Assert the
  sabotage applied before believing the result.
- **`prompt_contract_hash` is still on the live wire** — 9 occurrences under
  `prompt_observability.chat_contexts[]` — as HISTORICAL PERSISTED rows echoed
  through `_merge_latest_contexts(disk_rows=…)`, carrying stale contract hashes
  (`73ee514b…` = contract 44, `20639a26…` = contract 46). No production code
  writes it; `test_prompt_observability.py:388` pins that fresh rows do not
  manufacture it. The `models.PersonaInstance` field row is correct and does not
  conflict — and this is why it was rowed as a scoped CLASS_ATTR rather than a
  CODE row. Recorded, not cut: it is persisted-row read-side compatibility.

### Items 9 — OPERATOR RULINGS OWED (nothing implemented)

1. **A structural gate that no test deleted in a wave leaves a still-live
   production symbol uncovered.** This is the shared root cause of items 5 and 6,
   and recurrence is itself the finding. Both were the same shape: a wave deleted
   a test's INPUT VECTOR and took the test with it, without asking whether the
   SUBJECT was still live. `_apply_conversation_cap` lost 100% of its coverage
   and stayed on every channel; the `roles` kwarg lost its meaning and 43 call
   sites kept feeding it. **Target shape:** a per-wave gate that diffs deleted
   test node ids against the production symbols they reached (import graph +
   receiver-aware call graph over the deleted bodies), and fails unless each
   still-live symbol either retains another test or is named in an explicit
   ledger row. **Blast radius:** test-infrastructure only; no production change.
   **Test plan:** red-proof by deleting a test of a live symbol and asserting the
   gate names it; green-proof by deleting a test whose subject went in the same
   commit. Recommended — this class has now cost two waves.
2. ~~**`repo_context.py:907-923` hardcodes absolute developer-machine paths** as
   the alias table for `resolve_affected_repo_workdir()`. Item 7 stopped it
   leaking into byte-pinned goldens, but the PRODUCTION frame still ships an
   operator-machine-shaped `repo_scopes.resolved` to every consumer, and the
   Launcher is CLAUDE.md-bound not to hardcode personal machine paths. Retiring
   it properly means config/env-driven repo roots with an explicit
   "unconfigured" state rather than a silent `false` — a cross-stack refactor,
   not a cleanup.~~ **RULED and EXECUTED 2026-08-03 — see S68 below.**
3. **No item required a contract move**, so nothing from items 1–8 escalates on
   that ground. Item 1 is the near miss and the reason it is worth naming: the
   sweep verb could not have been RESTORED without re-registering
   `persona_instance.reaped`. Retiring it moved nothing.

## S67 / Round 4 follow-on rulings (2026-08-02)

### The deleted-test/input-vector recurrence is now a gate

The item 9 ruling above is executed in `test_tombstone_registry.py`. The gate
diffs round-4 test nodes from `4a21f0779`, resolves production imports and
receiver-qualified module calls through ASTs, and asks one question for every
deleted `test_*` node: does each production class/function it reached still
exist, and if so does any current test still reference it? A live subject with
no remaining test fails naming the deleted node and fully-qualified production
symbol. A subject deleted with its test is ignored because it is no longer live.

Running it exposed `decision_contract_registry.contract_hash`: its two direct
tests had gone while the function still feeds snapshot/status. Coverage was
re-homed into the surviving S15 contract authority by asserting the manifest's
hash equals `contract_hash()`. Red proof then removed that reference and the
gate failed naming both deleted tests and the live function; restoring it made
the gate green. This is the mechanical version of the
`_apply_conversation_cap` lesson, not another per-wave name list.

### Persona precedence has one authority

The audit claim survived re-verification. `personas.py` resolved exact persona
id before unique profile owner for toolsets, while
`persona_commands._persona_by_id` independently selected
`profile_matches[0] if len(profile_matches) == 1` for model/provider/API mode,
autonomy, core-context and readiness. `profile_persona_resolution` now owns the
complete decision and typed reason; both the toolset resolver and CLI synthetic
profile path consume it. Exact-id, unique-owner, ambiguous and unowned outcomes
are unchanged, including fail-closed ambiguous ownership.

### Registry scope rules made explicit

The apparent private-helper row inconsistency is resolved as policy. Private
helpers receive permanent rows only when name/string dispatch makes the
spelling contractual, resurrection could bypass a surviving public tombstone,
or a ruling explicitly makes the name stable vocabulary. Ordinary private
implementation churn is governed by the surviving behavior pin, not permanent
reservation of every underscore name. The registry header now says this.

Wire-key absence likewise has a home: every future producer-key cut must add or
identify an exact producer-frame behavior pin; a CODE tombstone is not a
substitute. Cross-stack top-level snapshot reads are additionally governed by
the Launcher's analyzer-AST producer-presence gate over the byte-pinned real
hydrate/delta/delta-batch/heartbeat frames. Existing historical
`prompt_contract_hash` compatibility remains the deliberate keep recorded in
S66.

### Janitor boundary claim DIED on re-verification

No new janitor fixture was needed. S60 already installed an AUTOUSE
`tests/agent_runtime/conftest.py::isolate_agent_runtime_root` boundary that pins
both `_worktree_base_dir` and `legacy_harness_worktree_base_dir` to per-test
temporary paths. `test_delivery_directive.py::test_janitor_uses_the_suite_isolated_worktree_bases`
is the guard, and the original per-test pins remain as defense in depth. The
production `reap_orphan_worktrees(dry_run=False)` default was not changed; that
would be a user-visible janitor policy change outside this ruling.

### Cross-repo rulings recorded here for campaign continuity

The Launcher's typed `MissionBridgeErrorCode` lane remains cut. Stage C's
mandated `error_id` is not that lane: the MCP dispatcher actively produces and
returns `correlation_id`, `error_id`, and `diagnostic`, while the snapshot
diagnostics fields were parsed and discarded and the UI reads `rawErrorCode`.
The new Launcher producer-presence gate prevents top-level snapshot readers
outliving all real frames and requires visible, reason-bearing compatibility
annotations.

The proposed Launcher `incident -> incidents` patch-mapping cut DIED. Although
S65 removed the current writer, byte-pinned `patch_remove.json` is the explicit
historical `incident.closed` compatibility frame and the cross-repo coverage
manifest requires the Launcher to fold it. Removing the mapping made the full
suite return `needsResync` where the manifest requires `applied`; the mapping
therefore remains as a reasoned historical read.

The repair restore-failure and missing-preflight paths remain fail-closed. The
evidence is destructive risk: proceeding can replace operator `.env` and
`SOUL.md`. Launcher copy now tells the operator to restart and retry Repair
from Harness Settings, and its native-host test proves all four filesystem
seams exist on every supported `dart:io` target. No event, wire, schema, or
contract move was required by any S67 item.

## S68 — correctness and portability closeout (2026-08-03)

### Repository scopes now consume the machine-roots authority

The S66 item-9 ruling is closed without a wire or contract move.
`resolve_affected_repo_workdir()` retains explicit absolute paths and the
Hermes install's self-root, but Eternia aliases now map to the existing logical
`eternia_launcher` / `eternia_backend` bindings loaded by
`agent_runtime.machine_roots`. The alternatives were rejected: adding absolute
fields to `RuntimeConfig` would duplicate the machine-roots authority, and
environment-variable/default-directory probing would recreate the unaccounted
machine-local fallback this fix removes. An absent binding resolves `None`, so
the unchanged frame shape reports `resolved: false` honestly.

The Alice machine already carries both bindings in the machine-wide
`machine_roots.json`; no live config write or Backend checkout edit was needed.
Focused tests bind synthetic roots through `MachineRoots` and prove both aliases
resolve there, then supply an empty registry and prove neither alias probes a
compiled stranger layout. The live `repo_scopes` checkpoint remains all-true.

S66's stream-fixture normalization stays. The generator intentionally runs with
an isolated Hermes root and no operator bindings, while the committed cross-repo
fixtures deliberately pin production-like true values. Normalization now
separates that fixture choice from machine configuration; it no longer masks a
compiled path. The frame files and both manifests do not move.

### Launcher-QA template drift has an unconditional subject

`test_launcher_qa_template_drift.py` now builds a profile-shaped synthetic tree
from `CANONICAL_LAUNCHER_QA_MCP_SERVER` itself, so the fixture does not mirror
the authority by hand. The same tripwire helper checks that tree on every run
and rejects a deliberately drifted block naming
`env.STAGEC_LAUNCH_HELPER: missing`. A separate live-tree case remains: it
checks the real profiles when present and skips with an explicit environmental
message when absent. Therefore the old single test can no longer turn a missing
profile tree into a vacuous all-clear; on a bare machine only the additional
live-environment checkpoint skips, while the synthetic drift logic passes.

## S69 — the config read-guard was over budget by construction (2026-08-03)

### The canonical CLI gate was red for a reason that was never contention

`scripts/run_tests_parallel.py` returned `3,706 passed, 0 failed, exit 1` with
no failing test. The exit code came from one file that produced no tests:
`tests/hermes_cli/test_config_read_guard.py` was killed by the repo-wide
`--timeout=30` per-test cap in `pyproject.toml`, and the runner's bounded
1-worker straggler retry was killed the same way. Its 2 tests were exactly the
3,706-vs-3,708 delta. The same file had been observed failing this way over
several days by different operators and waved off as pool contention every time.

Contention was the trigger, not the cause. `_iter_source_files()` walked
`REPO_ROOT.rglob("*.py")` and applied `EXCLUDED_DIR_PARTS` as a POST-filter, so
every excluded subtree was still enumerated in full. Worse, the exclusion set
listed `.venv` by name and this machine's environment is `.venv-ci`: **5,606 of
the 6,682 files the guard opened and regex-scanned line-by-line — 84% — were
third-party site-packages**. Measured on the primary checkout:

| | files enumerated | enumerate | guard test | file via canonical runner |
|---|---|---|---|---|
| before | 6,682 | 1.36s | 3.62s | 7.2s warm / 34.1s cold-and-idle / >30s under pool load |
| after  | 1,076 | 0.17s | 1.13s | 4.5s |

That is not a budget question, so no budget moved and nothing was fenced. Fence
rules 3–4 forbid an env-gap fence over a defect in our own test, and this was
one.

### Scanning a virtualenv was also a correctness defect

The offender set depended on which third-party packages happened to be
installed on the box, and any vendored library shipping a `safe_load` within
`PROXIMITY` lines of a `"config.yaml"` string would have failed OUR guard.
`_iter_source_files()` now walks with `os.walk` and prunes `dirnames` in place —
exactly equivalent to the old "any part of the relative path is excluded" test,
because a pruned directory can contribute no descendants — and additionally
prunes any directory carrying a `pyvenv.cfg` marker. Interpreter environments
are excluded by MARKER, not by name, so `.venv-ci`, `.venv-py313` and `venv/`
cannot reintroduce the hole the way `.venv-ci` did.

Equivalence was measured, not assumed: old and new enumeration were diffed as
sets against the primary checkout. 5,606 dropped, all under `.venv-ci`; **zero
first-party files dropped and zero added**. Nothing the guard asserts changed.

### Red-proof

A `yaml.safe_load(open("config.yaml"))` was planted in
`agent_runtime/machine_roots.py` (not allowlisted, first-party). The guard
failed naming `agent_runtime/machine_roots.py:967` with the offender line, in
1.50s. Sabotage reverted; the file went green in 4.5s through the canonical
runner. No contract, wire, event or schema moved.

### Durable finding

A structural gate that walks the repo is only as hermetic as its prune list, and
a prune list keyed on DIRECTORY NAMES rots the first time an operator names an
environment something else. Prefer a marker/typed test over a name list for
anything whose whole purpose is "this is not our code".

## Unbounded-by-default rollout — the debts it un-defers (2026-08-09)

The unbounded-by-default implementation
([`UNBOUNDED_DEFAULT_PLAN_2026-08-09.md`](UNBOUNDED_DEFAULT_PLAN_2026-08-09.md),
§8) names two items that this ledger now owns. Neither is a defect in the
landing; both are consequences the ruling accepted, recorded here so they are
never silent.

1. **`memory` parallel authority is no longer deferred by a block.** The upstream
   `memory` tool sat in `PERSONA_BLOCKED_TOOLS` partly on a real ruling: upstream
   memory writes and profile memory (`MEMORY.md` / `USER.md`, the
   `include_profile_memory` opt-in) are two authorities over the same question,
   and blocking the tool deferred the reconciliation. With `unbounded` as the
   runtime default the tool is on the schema of every chat lane, so the conflict
   is now REACHABLE rather than resolved. **Owed:** decide which store is
   authoritative for a persona's durable memory, and either route the upstream
   tool at the profile-memory store or keep them separate with an explicit,
   documented split. Until then an agent can write to a memory surface the
   profile-memory lane does not read.
2. **Per-turn schema cost.** `chat_lane_toolsets`' cost cuts (browser / vision /
   code_execution / debugging / file / terminal + `skill_manage`) do not apply
   under `unbounded`, so every conversational turn now ships the full registry
   schema and each session pays one prompt-cache invalidation as the mode flips.
   The plan's §4.3 ruling stands: if bills spike, the fix is a narrow knob (e.g.
   `unbounded_keep_chat_lane_cost_cuts`), **not** quietly re-blocking tools — and
   it is not built speculatively.

Also recorded, unchanged by this landing: the still-owed one-line delegation of
`tools/terminal_tool.py::_log_harness_blocked_attempt` to
`terminal_envelope.record_legacy_block` (the legacy writer keeps its silent-drop
branch on lanes that bind no scope; see that function's docstring).

The safety tradeoff itself — `network_egress` was the last exfiltration brake
after ruling R-2 removed the secret-read floor, and the replacement is detective
(receipts) rather than preventive — is recorded in the plan's §4 and was accepted
by the operator before implementation. Any preventive replacement (egress
allowlists, secret-scoped env) remains a separate ruling; nothing here builds one.

## S70 — free-floating assignment lane strip (2026-08-09)

Trigger (verified live 2026-08-09): `harness persona instance create
--add-instance --display-name … --message … --auto-run` created the placement
but landed NO turn anywhere — the display-name branch silently discarded the
*required* `--message`/`--auto-run` flags. The display-name-less branch queued
a "free-floating persona assignment" instead: a row whose only durable
consumer was the tick loop the 2026-07-30 chat-only purge removed. A queued
row therefore dead-ended forever (its envelope's `next_expected` advertised a
`persona instance run-once` verb that never existed), and the `--auto-run`
in-process runner was a second, parallel turn authority beside
`mission-chat message`.

### Cut (tombstone registry, wave s70)

`persona instance message` (verb + handler), `create`'s queue branch and its
`--auto-run/--stream/--max-actions/--max-seconds` flags (argparse now REJECTS
them — a removed lane must not degrade to a silent ignore),
`_queue_free_floating_assignment`, `_run_free_floating_assignment_once`,
`_bind_free_floating_chat_session`, the assignment mint side
(`PersonaAssignmentStore.create`/`create_or_resume`, `PersonaAssignmentSpec`,
`assignment_evidence_kind`/`assignment_archive_scope`/`assignment_signal_hash*`),
`ExecutionState.QUEUED`, and `ChatErrorKind.CHAT_TRANSCRIPT_PERSIST_FAILED` /
`POST_TURN_PERSIST_FAILED` — the last three had ONLY free-floating emitters;
the outcome module's "every owned member has a producer" gate went red on them
the moment the lane died, which is exactly that gate doing its job.

### Alive-that-looked-dead (why the cut stops where it does)

- **`persona.instance.create` is gateway-exposed launcher-side** — the
  capability registry publishes it to the Agent Gateway with `message` /
  `auto_run` in `allowedArgs`, so the queue branch was string-reachable from a
  remote grant, not just the CLI. Post-cut a remote `auto_run: true` call gets
  a clean argparse rejection through the capability envelope.
- **The `persona_assignments` wire block is CONSUMED**: the Launcher parses it
  (`mission_control_snapshot.dart` `_personaAssignmentsFromJson`) and folds
  assignments into the agent roster (`mission_agent_roster_policy.dart`).
- The instance-mode value `free_floating` is still live conversational-mode
  vocabulary (`operator_channels` / `persona_chat_history` /
  `persona_instance_identity` mode sets) — it is a MODE, not the lane.

### Parked — decision-ready (do NOT cut without the named coordination)

1. **`--title`/`--message` on `persona instance create`** — accepted for wire
   compat and IGNORED (title only feeds the display-name fallback). The
   Launcher emits both on every `persona.instance.create` /
   `persona.profile.instantiate` call (`mission_control_bridge.dart`).
   Removal = lockstep launcher change (bridge argv + registry
   required/default args + both call sites in `mission_control_page.dart`).
2. **Full `PersonaAssignmentStore` retirement** (read/close side, the
   snapshot/status `persona_assignments` wire block, `persona assignments`
   list verb, `persona instance close`/`archive` maintenance verbs, the
   `persona_assignment.closed` event, the retire guard's `assignment_active`
   refusal, `migrate_retired_persona_assignment_task_ids`). Blocked on: the
   Launcher consumers above (wire-block drop = snapshot contract bump per
   `agent_runtime/snapshot.py` ledger rules) and on residual rows on live
   runtime roots (close/archive are the only settle path; retire would
   deadlock on an active residual row without them).
3. **Launcher-side handoff** (not this repo's tree): registry
   `persona.instance.create` `allowedArgs` still advertises
   `auto_run`/`max_actions`/`max_seconds`/`message`; the bridge still emits
   the `--auto-run` block when `auto_run: true`; the
   `persona.instance.close`/`archive` bridge lanes have NO dispatcher
   (registry rows + argv builders only). All three should be pruned
   launcher-side; until then a remote `auto_run: true` gets the argparse
   refusal.
4. **`PersonaInstanceStore.create_free_floating`** — production-callerless
   after the cut (its one production caller was the queue), but ~10 test
   files across flow-graph/checkpoint/state-patch suites use it as their
   instance-mint fixture. Fold it into a shared test fixture (or rename to a
   test-support mint) in a mechanical follow-up; not cut here because the
   blast radius is unrelated suites in a live runtime.
5. ~~**Dead-but-contract-relevant persona-instance wire fields**~~ — **LANDED
   2026-08-09 at contract 54.** See "S70 wire prune" below for what actually
   moved and for the three claims in the original entry that did not survive
   verification.

### S70 wire prune — landed at contract 54 (2026-08-09)

Six keys left every `persona_instances` row; the Launcher pin moved to 54 in
the same wave. **Cut:** `context_receipt_id`, `compression_receipt_id`,
`tool_budget_used`, `watchdog_warning_count` (writer-less, and no consumer past
a Launcher model copy — these four also leave `PersonaInstance` itself, which is
safe because `serde._coerce` builds kwargs from the dataclass fields and ignores
stale keys on persisted rows) plus the two duplicate ALIASES
`current_work_assignment_id` and `attached_task_id`.

**Three corrections to item 5 as written.** They are the reason "writer-less"
must never be used as a synonym for "dead":

- **`attached_task_id` was NOT writer-less.** It is a wire alias of
  `current_task_id`, which is written live by the steer/`goal_id` lane
  (`persona_assignments.py`). It was cut as *redundant* — the same value still
  ships under the canonical key — not as dead. Same shape for
  `current_work_assignment_id` (alias of `current_assignment_id`).
- **The item missed the incremental lane entirely.** `attached_task_id` was also
  projected by `state_patches._persona_instance_wire_row` and mapped in
  `_PERSONA_INSTANCE_STORE_TO_WIRE`. The Launcher folds whatever wire fields a
  patch carries with no allowlist, so cutting only the full-snapshot copy would
  have let the first incremental update re-add a key the rebuild had dropped —
  the two lanes must move together.
- **`token_budget_used` and `last_heartbeat_at` are writer-less but NOT
  reader-less, so they STAY.** `token_budget_used` feeds the Launcher's
  token-total fallback (`totalTokens ?? tokenBudgetUsed` in
  `mission_agent_instance.dart`), not just a parse.  `last_heartbeat_at` has
  three live readers: the Launcher's roster-recency tiebreak
  (`mission_agent_roster_policy.dart`), its Agent Gateway state frame
  (`gateway_state_frame.dart`, which re-emits the value), and — in THIS repo —
  `classify_orphan_persona_instances`, where a fresh heartbeat is the
  `held-heartbeat` reason that protects a row from being archived. Retiring
  either is a reader-side ruling with its own blast radius, not a wire cleanup.

`current_assignment_id` also stays: the Launcher folds it into the agent roster
against the `persona_assignments` block, and residual rows still carry values.
It remains part of batch (2).

**Also found, unrelated to the prune but fixed in the same wave:** the emitted
contract version is asserted by SIX literals across five test files with no
shared authority, and the 53 landing moved only two of them — `test_s47`
(which calls itself "the only live pin"), `test_office_store`,
`test_s57_unruled_config_debt_removal` and `test_stage19_visibility` were all
RED on `main` at 52-vs-53 before this wave. All six now read 54. The structural
fix — import one constant from the producer — is filed, not done.

## Root-observability wave (2026-08-12) — upstream divergent platform-default spellings

The wave that shipped the machine root anchor (`agent_runtime/root_anchor.py`),
the per-verb `resolution` / `chat_scope` envelope blocks
(`agent_runtime/root_observability.py` + the
`test_harness_json_root_observability.py` gate), and the
`active_profile_name()` fallback fix collapsed the divergent platform-default
spelling in FORK-OWNED code only. The remaining divergences live in
upstream-owned files and are recorded here rather than edited (fork boundary;
edits become upstream-PR candidates):

1. **`hermes_cli/env_loader.py:310` and `:541`** both hand-spell the default
   home as `Path.home() / ".hermes"`. On native Windows the platform default
   is `%LOCALAPPDATA%\hermes` (`hermes_constants._get_platform_default_hermes_home`),
   so an env-file load that falls through to this spelling reads a `.env` from
   a directory no other resolver uses. Any upstream PR must route both sites
   through `hermes_constants.get_default_hermes_root()` / `get_hermes_home()`.
2. **`hermes_state.py:235`** — `DEFAULT_DB_PATH = get_hermes_home() / "state.db"`
   freezes the ambient home at import time (the frozen-home class the P3
   ratchet measures; the module's per-call paths do route through the
   canonical resolver). Already carried by the frozen-home ledger
   (`tests/test_no_frozen_hermes_home.py`); listed here because it is the
   `hermes_state` half of the "divergent default resolution" family this wave
   closed fork-side.

Fork-side closure for the record: `agent_runtime/profile_context.py`'s
`active_profile_name()` no longer reads a hand-spelled
`Path.home()/".hermes"/active_profile` (a marker file nothing on native
Windows writes — it made `agents --json`'s `source_profile` column lie under
ambient environment); the fallback now delegates to the canonical
`hermes_cli.profiles.get_active_profile()`.
