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
- **`delivery_directive.read_bundle_promotion_record` /
  `bundle_promotion_record_path` are caller-less.** `repo_bundle_summary` was
  their last production caller. **STILL OPEN after S57** — that wave was ruled to
  cut the STORE, and this pair is a separate lane. S57 did make the claim
  legible: the module docstring's "what remains has live callers" heading now
  states outright that this bullet is NOT covered by it, and
  `paths.repo_bundles_dir` / `repo_bundles_task_dir` survive solely because this
  pair still addresses the tree through them. Retiring the pair retires those two
  helpers with it.
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

- **Launcher: `roleChecklists` and `proofBatches` are the SAME dead-by-emptiness
  class.** Both are parsed on `MissionAgentLog` and on the top-level frame; S44
  deleted the store family that fed both, and the live frame carries neither. The
  `_RoleTaskHud` container in `agent_detail_terminal.dart` now renders only from
  these two, i.e. never. NOT cut: outside this wave's ruled scope, and hermes
  still keeps `role_checklists.validate_checklist_payload_structure` alive, which
  deserves its own look before the launcher side is swept.
- **Launcher: the bridge still forwards `raw['role_envelopes']`** as a
  raw-section pass-through beside `role_checklists` / `proof_batches`. With no
  parse behind it the key is inert; removing the forward belongs with the sibling
  cut above rather than half-way through it.
- **`agent_runtime/role_checklists.py` survives for one importer.**
  `decision_contract_registry.py:177` imports
  `validate_checklist_payload_structure` from it. Worth checking whether that
  validator can still be reached by a payload the runtime produces, given S44
  deleted the store family around it.

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
