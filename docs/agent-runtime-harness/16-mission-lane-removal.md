# 16 — Mission Lane Removal

> **Status: the executable plan.** Removes the goal/task mission lane and the two old
> graphs, keeps chat, agents, the runtime agent graph, the board, realms, and Stage C
> visual proof. Derived from a full dependency map (2026-07-29) that **corrected six
> premises** the earlier framing assumed — read §0 before acting on prior notes.
>
> Baseline at authoring: `tests/agent_runtime` 3743 · `tests/hermes_cli` 8468 ·
> Launcher `test/features/mission_control` 2750 / 2 skipped. Tree clean, `main` 4 commits
> ahead of `origin/main`.

---

## 0. Corrections to earlier premises — read first

1. **The kept graph is NOT persisted as a Blueprint.** `EterniaLauncher/.../data/blueprint_models.dart`
   (122 lines) is a read-only in-memory **view model** — a name collision with Python's
   `agent_runtime/blueprints/`, not a shared container. Dart `BlueprintEdge.outcome` is a
   free-form string carrying `'steers'`; Python's is a StrEnum that would reject it. The
   runtime graph persists via `agent_runtime/flow_graph.py` (`graph_id: runtime:<owner>`)
   plus `persona_instances[].steered_by`. **Deleting Python's blueprint package does not
   touch the kept graph.** The real seam is `MissionTypedPlan`.
2. **`agent_topology` is neither dead nor live.** 6 of 12 persisted goals carry a populated
   topology, but it is absent from the contract-44 frame (S4 removed it), so the Dart
   `_agentTopologyRuntimeGraphProjection` branch cannot fire. Dormant, not primary.
3. **`scripts/upstream_sync_gate.py` does NOT enforce the upstream boundary.** Its
   `AGENT_TOOL_SEAMS` only decides whether to additionally run the Flutter lane. It is a
   test runner. The real check is the `git diff --name-only upstream/main...HEAD` filter in
   the final gate below.
4. **Personas and profiles are DATA and are not deleted.** Nothing under
   `.hermes/profiles/` is removed. Only the *hardcoded logic* that declares them goes.
5. **The kept graph is 100% chat-mode and already goal-free** — 8 runtime-created instances
   in two workspace-scoped `steered_by` trees, zero task binding. The 6 hardcoded seeds are
   outside it, and 3 of them are exactly the 3 real task-bound instances.
6. **No `serde.py` compat shim is needed.** `serde._coerce` iterates `fields(dataclass)`,
   so persisted `mission_plan` / `agent_topology` / `linked_goal_id` keys with no field are
   silently dropped. The `_LEGACY_RUNNING_TASK_STATE_VALUES` precedent does not apply — it
   exists because enum *values* raise, not because fields vanish.

## Operator rulings (2026-07-29)

- **R-1 — Role floor removed.** `R1_ADMISSIBLE_ROLES` goes; MCP admission becomes governed
  by whether a **profile declares the server**. Effect: `alice`, `aliceimagecron`, `base`,
  `launcher-dev`, `launcher-qa`, `neko`, `qa`, `unbounded` gain `launcher_qa`;
  `backend-dev` and `gpt-launcher` do not (no `mcp_servers` block). This deliberately
  overturns the invariant at `mcp_admission.py:33-38` — **replace that docstring, do not
  leave it contradicting the code.**
- **R-2 — Hard floors removed.** `credential_read`, `credential_exfil`, `prod_operation`
  are removed. Operator will re-add secret blocking after seeing the system work. **Must be
  its own commit** (S12) so re-adding is a single revert, not an archaeology exercise.
- **R-3 — `TaskStore` stub is permanent.** Upstream `tools/board_tool.py:85` imports it
  unguarded. A stub stands indefinitely.
- **R-4 — The Agent Console `Inspect ▾` menu is KEEP, in full**, except that the
  *custom harness* permission gating goes while **upstream permission state stays visible**.
  Menu items: Run detail · Context Inspector · Turn tool context · Permissions
  (`agent_visibility_dialog.dart:35 showAgentPermissionsDialog`) · Skills Context. Also keep
  the model selector, Session %, and Filter. The Permissions dialog is **retargeted, not
  deleted**: it stops reporting role-keyed harness gates (removed in S11/S12) and reports
  the upstream permission surface instead.
  **`Run detail` is REMOVED** (operator ruling, same session): it resolves a `run_id`, and
  runs go in S5/S8, so it could only ever render its empty state. Drop the menu item, its
  handler, and `open_run_detail` — including the Stage C control
  `mission_control.agent_chat.open_run_detail`, so `get_buttons` stops advertising it.
  The other four items stay.
- **R-5 — `Assign Work` is REMOVED.** Button + semantic control
  `mission_control.agent_chat.action.assign_work`. Surfaces:
  `agent_chat/mission_agent_chat_adapter.dart`, `mission_agent_chat_panel.dart`,
  `mission_agent_chat_panel_parts/controls_and_helpers.dart`, `mission_control_page.dart`.
  It assigns goal work and has no meaning once goals are gone. Remove in S10 alongside the
  other Launcher lockstep work, and drop its Stage C control registration so
  `get_buttons` stops advertising it.

---

## Stages

Each stage leaves the tree importable and green. Gates are `rg`-returns-zero plus a named
test command. **All gates are path-scoped — never a bare word grep** (see §Hazards).

| Stage | Status | Verification note |
|---|---|---|
| S0 | complete | Runtime mission data quarantined; 11 chat instances retained. |
| S1 | complete | Protective keep-set stage committed as `0a7d96cd1`. |
| S2 | complete | Launcher cosmetic-read removal committed as `429f26a5`. |
| S3 | complete | Board→goal bridge removed. Operator authorized the required upstream projection edit in `tools/board_tool.py`. |
| S4 | complete | Goal creation, CLI verbs, toolset, and opt-in gate removed; `3742 passed`. Operator authorized upstream edits in `tools/agent_chat_tool.py`, `tools/board_tool.py`, and `tools/tool_full_descriptions.py`, plus deletion of `tools/mission_goal_tool.py`. Dedicated-worktree verification exposed two stale checkout-name assumptions: runtime repo labels now remain canonical and the resolver test asserts the exact active repo root. |
| S5 | complete | Dispatch loop and worker execution modules removed; `3222 passed`; affected Harness CLI module `50 passed`. The dependency map omitted two later-stage importers: `burn_in.py` (S6) and `persona_diagnostics.py`/its CLI verbs (originally assigned to S8). They failed closed with `LegacyOrchestratorRemoved` at this boundary. Final acceptance found that `persona_diagnostics.py` had survived S8 and removed it together with the unlisted mission-only `goal_hygiene.py`. Main-loop review also found and retargeted stale `goal_runner`/`run_tick` tests in `tests/hermes_cli/test_harness_cli.py`; no upstream-owned file changed in S5. |
| S6 | complete | Mission proof/gate machinery, burn-in, smoke, and replay paths removed; Stage C capture/provider/parser/policy paths remain; `3125 passed`; affected Harness CLI module `46 passed`; cert-streak tombstone `1 passed`. The dependency map omitted proof-only `qa_verdict.py`. The S1 extractions also left `visual_proof.py` and `visual_trace_evidence.py` with no independent KEEP half, so both wrappers were removed whole while their Stage C primitives remain. A non-proof default-blueprint repo-grounding helper formerly housed in `final_gate.py` was re-homed in `persona_runtime.py` to preserve behavior. Boundary exception: upstream-owned `scripts/cert_streak.py` was deleted exactly as S6 requires, under the operator's explicit authorization to document upstream changes. |
| S7 | complete | Python blueprint implementation/data and the stage-graph runtime were removed while the permanent `agent_runtime.blueprints.resolve` promotion shim remains; `3022 passed`; affected Harness CLI modules `50 passed`. The dependency map warned only about the upstream profile-promotion import, but two additional upstream seams depended on the removed graph: `hermes_cli/web_server.py` exposed blueprint list/run endpoints and `hermes_cli/profiles.py` blocked profile deletion through live blueprint bindings. Both were removed under the operator's explicit authorization to document upstream edits; the promotion endpoint remains green through the shim. |
| S8 | complete | `Task`/`Goal` records and the 57 mission CLI leaves are removed; `TaskStore` is the permanent zero-surface S1 stub. Red proof: the new removal contract initially failed twice; green: `2 passed`, affected Harness CLI `46 passed`, adjacent profile coverage `156 passed, 2 deselected` (the two deselections are the pre-existing Windows `.env` mode mismatch). Live checkpoint completed on `personainst_neko_supervisor_agent_f6f7a51b`: canonical chat replied `CHAT_LANE_OK`, board list returned 2 boards, and flow list returned 10 runtime graphs; the turn HUD rendered all three steered children. The literal TaskStore gate has one intentional upstream hit at `tools/board_tool.py:93`, the permanent R-3 seam whose `NotFound` is caught by that tool; all fork-owned direct calls are gone. Full runtime inventory at this intermediate boundary was `104 failed, 2920 passed`: failures are stale mission-record assertions plus S9 snapshot/read-model expectations and are retained for retargeting, not skipped or xfailed. |
| S9 | complete | Snapshot/read model contract is 45/schema 2. Mission rows (`goals`, stage verification, runs, proofs, incidents), their three indexes, and `agent_instances.task_id` are removed; the projector now atomically refreshes the compact chat/runtime frame on new events. Red proof: the new contract test failed on schema 1; green: the nine affected snapshot/projector/read-model modules are `53 passed`. Live Alice snapshot reported contract 45 with no mission sections, and `harness rebuild-read-model --json` succeeded at event offset 86813555. |
| S10 | complete | Launcher consumes contract 45; plan graph DTOs/fallbacks, Assign Work, and the Agent Console Run detail opener are removed. Cross-repo stream fixtures are byte-identical and pinned at 45. Red proof: the contract test expected 45 while production emitted 44. Green: `2756 passed, 2 skipped`; full analyze reports exactly the two pre-existing findings. Contract-adjacent Stage-38 and reopen fixtures also required a 44→45 envelope bump. |
| S11 | complete | Persona/profile data is authoritative: bundled seed/catalog constants, role tool ceilings, decision-role filtering, MCP role floor, and the legacy task-bound chat-root split are removed. Red proof: the new declaration/unknown-role tests failed `2`; green: focused S11 slice `162 passed`, exact MCP gate `72 passed`, and the removal grep is empty outside this plan. Live Alice roster remains `11` chat instances (`15` total including `4` configured rows). Final acceptance exposed one remaining template assumption in profile promotion: an unseen data-declared role returned HTTP 400 when no persona template existed. Promotion now preserves explicit-template cloning but otherwise mints a profile-backed persona from runtime defaults while retaining the supplied role as data. The live shared runtime-model skill and its repo copy now describe the single chat lane. |
| S12 | complete | Ruling R-2 removed governed-lane blocking for credential-file reads, credential-shaped loopback traffic, and production kubectl/helm/terraform mutations. Red proof: the new removal contract failed while all three classes remained; green: the exact grant suite is `82 passed`, adjacent envelope/operator-context coverage is `42 passed`, and `tools.terminal_tool` remains importable. The upstream fallback pattern table is intentionally unchanged and still applies on lanes with no bound envelope scope. |

Final acceptance (2026-07-30): `tests/agent_runtime` is `3035 passed`; the bounded
mission/persona/blueprint/read-model CLI set is `138 passed`; Launcher Mission Control
is `2756 passed, 2 skipped`; and full Launcher analysis contains exactly the two
pre-existing findings. The live Alice snapshot is schema 2 with no mission sections,
2 boards, and 11 chat instances. A real on-level Dev turn completed with `run_ids: []`,
the returned HUD carried the Neko→Dev steering edge, and the board's live card listed.
Pinned Stage C (`alice` / `X:\Eternia\.hermes\agent-runtime`) rendered the four-agent
runtime graph in a redaction-safe 2560×1400 PNG at
`X:\tmp\stagec\screenshots\mission_lane_removal_final_20260730072431682.png`.

### S0 — Data migration, no code change
- Run `harness persona instance sweep-orphans` **while `TaskStore` still exists** to reap
  the 4 task-bound instances (`_owning_task_release_state` returns `archived` when the task
  file is gone).
- Archive `goals/`, `tasks/`, `runs/`, `blueprint_runs/`, `proofs/`, `incidents/`,
  `burn_in/` out of the live root.
**Proof:** zero `mode == task_bound` instances remain; the 11 chat-mode instances survive;
`harness snapshot --json` still builds.

### S1 — Protect the KEEP set first
- `TaskStore` stub contract for upstream `tools/board_tool.py` (call site is already inside
  `try/except Exception: pass`, so a stub raising on `.get()` suffices).
- Re-home `promote_profile_to_persona` (`blueprints/resolve.py:108`) → `personas.py`. Two
  live callers, one upstream (`web_server.py:12671`, `/api/profiles/{name}/promote`).
- Extract Stage C arg validation from `proof_command_policy.py:113-197`.
- Extract Stage C trace parsers from `visual_trace_evidence.py:97-183`.
- Repoint `stagec_mcp_visual_provider._write_rebuild_artifact` (842-855) off `proofs/`.
- **Make `_augment_chat_capabilities` (`persona_runtime.py:1293`) append unconditionally** —
  it is the ONLY path adding `board` and `agent_chat` to a chat lane, and it reads
  `ALLOWED_TOOLSETS_BY_ROLE`.
**Gate:** `rg "proofs/" agent_runtime/stagec_mcp_visual_provider.py` → 0 ·
`pytest tests/agent_runtime/test_board_agent_tools.py tests/agent_runtime/test_chat_lane_toolsets.py -q`

### S2 — Sever the Launcher's cosmetic reads
Delete `_topologyNodeRepo` (projection:737-750) and the `repo:` write (:581); drop the
`limits`/`onUnhandled` writes (:624-626). `missionAgentRuntimeGoalText` (:1092) already
falls back to goal title → `owner.taskTitle` → `'No goal captured yet.'`
**Gate:** `rg "missionPlan\.(stages|limits|onUnhandled)" <projection file>` → 0 ·
`flutter test test/features/mission_control`

### S3 — Board → goal bridge
Delete `BoardStore.escalate` (`board_store.py:467-524`), `_cmd_board_escalate`
(`board.py:222`), parser `board_escalate` (`harness.py:943`), contract
`board.card.escalated`, and `BoardCard.linked_goal_id`. All 3 live cards have it `null`;
it is decorative provenance only. **No shim** (see §0.6).
**Gate:** `rg "linked_goal_id|escalate" agent_runtime/board_store.py hermes_cli/harness_parts/board.py` → 0 ·
`pytest tests/agent_runtime/test_board_store.py tests/agent_runtime/test_board_sync.py tests/agent_runtime/test_board_agent_tools.py -q`

### S4 — Goal creation
Delete `tools/mission_goal_tool.py`, `agent_runtime/mission_goal.py`,
`--allow-mission-goal`, the `mission_goal` toolset and its role gate
(`persona_runtime.py:1063-1067`).
**Gate:** `rg "mission_goal_create|allow_mission_goal" agent_runtime hermes_cli tools --glob '!*.md'` → 0 ·
`pytest tests/agent_runtime -q`

### S5 — Dispatch loop
Delete `ticker.py` + `ticker_parts/`, `goal_runner.py`, `planning.py`, `autonomy.py`,
`liveness.py`, `no_freeze_monitor.py`, `recovery.py`, `reconciler.py`, `supervision.py`,
`root_node_engine.py` (zero importers — already dead), `node_tools.py`,
`worker_actions.py`, and `persona_runtime.run_tick` / `_invoke_agent`.
**Gate:** `rg "from .ticker|TickEngine|run_tick|_invoke_agent|MissionRuntimeController" agent_runtime hermes_cli` → 0 ·
`pytest tests/agent_runtime -q`

### S6 — Proof machinery and burn-in
Delete the 9 proof/gate files, `burn_in.py`, `smoke.py`, `replay_scenarios.py`,
`scripts/cert_streak.py`, and the REMOVE halves of `visual_proof.py` (from line 60) and
`visual_trace_evidence.py` (from line 44).
**KEEP — named like the remove set, are not:** `parity.py` (ProjectionAccountant,
read-model infra), `patch_coverage.py` (stream frames), `proof_capture.py` (Stage C
dataclasses — **rename it**).
**Gate:** `rg "proof_gates|proof_runner|proof_batches|promotion_gates|final_gate|ProofStore" agent_runtime hermes_cli` → 0 ·
**negative gate:** `rg "ProjectionAccountant" agent_runtime/parity.py` → non-zero ·
`pytest tests/agent_runtime -q`

### S7 — The stage graph itself
Delete `agent_runtime/blueprints/` (1,318 lines, 6 .py + 9 .yaml), `default_plan.py`,
`mission_plan.py`, `state_machine.py`, and `MissionPlan` / `MissionPlanStage` / `TaskStage`
from `models.py`.
**Gate:** `rg "MissionPlan|mission_plan|BlueprintStore|instantiate_blueprint|owner_slot|agent_topology|ensure_default_mission_plan" agent_runtime hermes_cli/harness.py hermes_cli/harness_parts` → 0 ·
`pytest tests/agent_runtime -q`

### S8 — `Task` / `TaskStore` and the CLI
Reduce `TaskStore` to the S1 stub; delete `Task`; delete the 57 REMOVE subcommands
(of 168). **KEEP:** `persona`, `instance`, `open-chat`, `mission-chat`, `board`, `card`,
`realm`, `workspace`, `agent`, `skills`, `snapshot`, `status`, `stream`, `serve`.
**Gate:** `rg "TaskStore\(\)\.(get|list_all|create|update)" agent_runtime hermes_cli tools` → 0 outside the stub ·
`pytest tests/hermes_cli -q`

### S9 — Snapshot / read model retarget
Remove goal sections; drop `goals`, `stage_verification`, `runs`, `proofs`, `incidents`
from `ROW_TABLES` (`read_model.py:28`) and their 3 indexes. **KEEP** `agent_instances`
(drop its `task_id` column), `operator_channels`, `meta`, `projection_watermarks`,
`projections_misc`. Bump `READ_MODEL_SCHEMA_VERSION` 1→2 and `contract_version` 44→45.
**Not stage-sourced — do not delete by name:** `goals[].stage_verification` reads
`task.harness_self_heal["stage_observations"]`.
**Gate:** `rg '"mission_plan"|"mission_flow_timeline"|"proof_gate_state"|"agent_topology"' agent_runtime/snapshot.py` → 0 ·
`harness snapshot --json` builds · `harness rebuild-read-model`

### S10 — Launcher lockstep
Bump `kSupportedMissionContractVersion` 44→45; delete `MissionTypedStage` / `MissionTypedEdge`
/ `MissionTypedSlot` / `MissionTypedLimits`, `_qaGraphActorsFromPlan`,
`_missionActorsFromPlan`; trim `blueprint_models.dart` to the pointer-resolution fields
(`BlueprintSlot.id`+`.role`, `BlueprintStage.id`/`.title`/`.objective`/`.ownerSlot`,
`BlueprintEdge.source`/`.target`, `Blueprint.slots`/`.stages`/`.edges`); default
`BlueprintStage.kind` → `'sub_agent'`; rename `BlueprintEdge.outcome` → `kind`. Regenerate
`test/fixtures/harness_stream/*.json` + `MANIFEST.sha256`; fix the hardcoded
`expect(kSupportedMissionContractVersion, 44)` at `read_model_s2_history_test.dart:79`.
**Gate:** `flutter test test/features/mission_control` · `rg "kSupportedMissionContractVersion, 44" test/` → 0

### S11 — De-hardcode personas and roles
**Profiles and persona records are untouched — this removes only the code that declares
them.** Delete from `personas.py`: `default_personas()`, `seed_personas()`,
`BUNDLED_PERSONA_PROFILES`, `BUNDLED_PERSONA_IDS`, `DEFAULT_PERSONA_IDS`,
`BASE_PERSONA_ID`, `DEFAULT_SUPERVISOR_PERSONA_ID`, `ALLOWED_TOOLSETS_BY_ROLE`,
`PER_ROLE_TOOL_DENIES`. Also: `config.py:397` (**drops a persona whose role is unknown —
must go, or non-hardcoded personas start disappearing**), the `decision_contract_registry`
role matrix, and residual name references in `context_builder.py` / `snapshot.py`.
Remove `R1_ADMISSIBLE_ROLES` (ruling R-1) and **replace the `mcp_admission.py:33-38`
invariant docstring** with the profile-declares-the-server rule.
Also the now-unreachable `task_bound_chat_root` guard — **surgical, not `git revert`**
(a revert would restore two deleted deprecated aliases and delete 52 unrelated tests):
`TASK_BOUND_INSTANCE_MODE` (`persona_commands.py:4957`),
`_mission_chat_carries_mission_context` (4961), `_chat_mode_redirect_targets` (4993),
`_task_bound_chat_root_refusal` (5054), the call site (1621-1639), 5 of 57 tests in
`test_relay_session_lifecycle.py`, and `harness-runtime-model/SKILL.md:91-99`.
**Gate:** `rg "task_bound_chat_root|R1_ADMISSIBLE_ROLES|ALLOWED_TOOLSETS_BY_ROLE|default_personas" agent_runtime hermes_cli tests docs` → 0 ·
**negative gate:** `harness persona list --json` still returns 11 chat instances ·
`pytest tests/agent_runtime/test_mcp_admission.py -q`

### S12 — Hard floors (ruling R-2) — ITS OWN COMMIT
Remove `credential_read`, `credential_exfil`, `prod_operation` from `terminal_envelope.py`
and `_lanes_for_role`. **This is a deliberate security-posture change, not a side effect**;
the operator will re-add secret blocking later, so this must be one revertable commit with
a commit message stating exactly what protection was removed and why.
Note the ordering that made this dangerous: the grant check (`:683-686`) runs **before**
the floor check (`:703`), and the "grants never contain a floor class" invariant lives at
`:598` inside the role-keyed resolver. `terminal_envelope.py` must remain **importable** —
upstream `tools/terminal_tool.py:2101,2139` imports `envelope_decision` and
`record_legacy_block` unguarded.
**Gate:** `pytest tests/agent_runtime/test_terminal_envelope_grants.py -q` ·
`python -c "import tools.terminal_tool"` succeeds

---

## Final gate — every stage and once at the end

```bash
python -m pytest tests/agent_runtime -q
python -m pytest tests/hermes_cli -q
cd "X:/Unreal Engine/Engine/Launcher/EterniaLauncher" && flutter test test/features/mission_control
git diff --name-only 2360f5b18..HEAD \
  | grep -vE '^(agent/|agent_runtime/|hermes_cli/harness)' \
  | grep -vE '^tools/(agent_chat_tool|mission_goal_tool)\.py$'
```
The last command is the **real** upstream check (§0.3). It must return only paths the
operator has accepted — expect `tests/`, `docs/`, and the deleted `tools/mission_goal_tool.py`.

> **Corrected 2026-07-29 (S1 verification).** This gate originally diffed
> `upstream/main...HEAD`, which compares the **entire fork divergence** and surfaces
> **122 pre-existing** out-of-boundary paths from fork history — noise that makes the gate
> unusable per stage. The baseline must be the last commit before this refactor,
> `e471c23d2` ("fix mission control snapshot diagnostics"). If you branch or rebase, re-pin
> the baseline rather than reaching for `upstream/main`. Verifying an S-stage introduced no
> upstream edit is a question about *this range*, not about the fork's whole life.

> **History folded 2026-07-31.** `main`'s pre-fold history (679 commits past the
> upstream merge-base `9de9c25f6`) was folded into 7 tree-identical thematic commits.
> Every commit hash cited in this doc remains permanently resolvable via the archive
> refs `archive/pre-fold-main-20260731` (branch) and `pre-fold-main-20260731` (tag),
> both pushed to origin. The baseline `e471c23d2` is no longer in `main`'s log; its
> **byte-identical** fold equivalent is `2360f5b18` ("fix(harness): persona-chat +
> runtime hardening to the pre-removal baseline"), and the gate command above is
> re-pinned to it — the diff it produces is unchanged. Old→new fold map (all
> tree-identical): `e471c23d2`→`2360f5b18` · `25e2651ac`→`2154f0542` ·
> `91be96091`→`5a1267ef6` · `4f06910b5`→`0c9d48d9f` · `0c155cf4b`→`0ad80754f` ·
> `ca4ebf6d5`→`101d95eeb` · `e2f47bdbe`→`a9fe20773`. Intermediate hashes (the S-stage
> commits above, the wave-3 close `3d1e0049c`, the wave-3 merge `2f1c0671f`) exist
> only on the archive branch — fetch it before checking them out.

## Hazards — five words with a keep-side and a remove-side meaning

| Word | REMOVE | KEEP |
|---|---|---|
| goal | harness mission records | `hermes_cli/goals.py` (upstream session goals / Ralph loop) |
| kanban / board | — | upstream kanban (~14k lines) **and** the fork board |
| proof | proof gates, `proofs/` store | Stage C MCP + PS helper, `proof_capture.py` |
| task | goal/task records | `TaskStore` stub (permanent, ruling R-3) |
| graph | stage graph, `agent_topology` | the agent runtime graph + `flow_graph.py` |

**Never gate on a bare word.** A name-based pass during planning flagged the kept graph's
own tests and all 15 Mission Office files as removal candidates purely on filename match.

Additional traps: `agent_runtime/sync_merge.py` looks generic, is a hard board dependency ·
`hermes_cli/harness_parts/board.py` is `exec`'d into `harness.py` globals, not imported, and
needs 8 helpers from that scope · `agent_runtime/office_*.py` is a 1:1 clone of the board
family sharing `sync_merge` — treat Office and Board as one blast radius ·
`shared_harness_overlay.md:9` has the anti-Kanban rule and the Mission Board carve-out in
the same sentence.

## Acceptance criteria

1. All three suites green at their baselines or above; nothing skipped, xfailed, or loosened.
2. The upstream diff filter returns only accepted paths.
3. `harness persona list --json` returns the 11 chat-mode instances; the two `steered_by`
   trees render in the Agent Console on an ordinary chat turn.
4. A board card can be created and listed; realms and workspaces round-trip.
5. A Stage C screenshot captures and returns a MEDIA path.
6. Every doc and skill describing the removed lane is updated in the same stage that
   removes it — a skill describing a retired contract makes agents behave wrong even when
   the code is right.

## Post-removal follow-ups (2026-07-30 audit)

- **Persona prompt-builder lane deleted.** `persona_runtime.build_system_prompt` had zero
  production callers after S5/S8 (the live chat lane builds its prompt inline in
  `_persona_chat_system_prompt`); it, `_load_persona_system_prompt`,
  `personas.load_bundled_prompt`, the helpers only it used (`_recommended_skill_guidance`,
  `_specialist_dev_guidance`, `_normal_worker_flow_guidance`, the `_simplified_contract_*`
  trio), and all five `agent_runtime/prompts/*.md` files (the four role prompts plus
  `shared_harness_overlay.md`) are removed. The overlay's board sentence is pinned on the
  chat prompt path only now (`test_board_agent_tools.py::test_board_sentence_present_in_chat_prompt`);
  its anti-Kanban rule governed the deleted GOAL PIPELINE and needed no migration. The
  bundled-prompt pinning tests in `test_personas.py` / `test_persona_prompts.py` /
  `test_persona_skill_policy.py` / `test_decision_contract_registry.py` /
  `test_decision_schema.py` were retargeted or removed in the same commit;
  `test_persona_skill_guidance.py` went with its helper.
  `_safe_read_soul_overlay` and `prompt_sources.resolve_persona_system_prompt_path` remain —
  both have live chat-lane / profile-binding callers.
- **`delivery_directive.py` was ruled mixed live/residue** — the initial audit kept the
  orphan-worktree janitor and promotion-record reads and queued the Task-declared
  directive / terminal-settle half for follow-up. That residue was swept in Wave 3
  (`354d7555a`); [delivery-directive.md](delivery-directive.md) records the split.

### Wave 3 outcomes (2026-07-30)

This follow-up wave re-derived every cut from the named symbols' current callers. The
outcomes below are the commit-recorded decisions, not a filename-based deletion list:

- **Five small-module candidates:** only `agent_runtime/role_sessions.py` was actually
  dead and was removed. The other four — `dev_discipline`, `simplified_contract`,
  `scope_control`, and `role_checklists` — have live production callers, so whole-module
  deletion was skipped. The same commit removed only the already-dead `repo_context`
  worktree-creator import from `persona_runtime.py` (`3a32ec617`). A later
  reachability pass established the precise correction: **four of the five modules are
  live modules with dead insides.** It removed 800 lines of unreachable mission-lane
  internals while retaining each imported surface (`5c16417f6`); notably,
  `role_checklists.checklist_for_task_stage` was kept as live and the formerly listed
  keep `stage_checklist_hud` was removed as dead.
- **CLI mission-record flags:** `persona instance return-summary --task/--stage` were
  removed, while `--proof-id` and `--artifact-ref` remain live output fields
  (`64d2f9176`); the now-CLI-unreachable `task_id` / `stage_id` continuity parameter
  chain was then removed while live `proof_ids` stayed (`304eb42c0`).
  `mission-chat message --task/--goal` and the write that re-armed `task_bound` state
  were removed (`b9f0043c4`). `persona instance steer --goal` is a separate, live
  contract-45 wire and stays; `persona instance update-profile --goal` and
  `persona tool-diff --task/--goal` also remain outside that cut.
- **Dead read/HUD/status clusters:** the caller-free context-builder HUD cluster was cut
  (`8fa9ee283`); the unreachable goal/task projection builders were cut from snapshot
  (`8fe3d6687`), followed by the remaining 78-name unreachable snapshot island
  (`064d46a27`); the tick-only `build_context` / `render_context` lane and its private
  helper graph were removed while `AgentContext` stayed as a live annotation
  (`539bf5813`); and constant-by-construction status, runtime-instance,
  stream-routing, event-summary, and observability fields/arms were removed
  (`c12e6850d`). The latter is also why `_delta_op` no longer has `task.*`,
  `proof.attached`, or `daemon.*` arms.
- **Store and event catalog:** writer-less proof/archive helpers, dead `RunStore`
  methods, and `run.heartbeat` / `run.approved` were removed (`8c1c8e6cc`).
  **`run.closed` is live and stays registered** because live cancel paths reach
  `close_run`. `realm.archived`, `persona_chat.deleted`, and
  `worktree.orphans_reaped` were registered from their real emitters
  (`a7e679972`). `moa.*` were explicitly skipped because they are TUI/display
  callbacks in `agent/moa_loop.py`, not `EventLog` events. The last writer-less
  `run.opened` contract was then retired while `flow_graph.pruned` was registered
  (`06eee42fa`), and reconcile now archives owner-less runtime graphs and emits that
  event in its final graph-prune phase (`6c5040ed2`). The registered-but-writer-less
  `repo_bundle.delivered` contract and its operator-summary arm were retired after
  the S24 emitter deletion (`f9febb32b`).
- **Narrow residue cuts:** writer-less task/proof/daemon path helpers and only the
  `proofs` checkpoint class were removed (`633772c34`); the retired stage-graph
  `StageStatus` enum was removed while the four live sibling enums stayed
  (`9ca3f8743`); and only the two zero-reference packet symbols were removed while
  the test-reached packet emission path stayed pending an operator decision
  (`ac751ea2f`). The six-hop `proof_store` parameter chain was also removed after
  every hop was confirmed to pass it onward without reading it (`dc926aa6c`).
- **Node control:** the broken `tools/node_control_tool.py` implementation of
  `run_node` / `steer_node` was deleted (`de14b06d2`), followed by the fork-added
  `node_control` block in upstream-owned `toolsets.py` under explicit operator
  authorization (`e69db6e71`).
- **Delivery residue:** declaration, terminal-settle, delivery-capture, and caller-free
  repo-context/repo-bundle helpers — including `git_diff_since_baseline` and
  `diff_weakens_tests` — were removed, while the orphan-worktree janitor and historical
  promotion-record reader stayed live (`354d7555a`). The
  `repo_context.isolated_repo_context_for_run` / `_worktree_token` /
  `_ensure_isolated_worktree` trio is intentionally kept as regression-test
  infrastructure: twelve tests exercise its worktree safety behavior, including two
  live-incident regressions. It has no production creator caller and is labelled as such.

### S28 — the two cuts S21 could not reach (2026-07-30)

S21 (`c12e6850d`) removed the constant-by-construction status/observe fields but
**deferred two**, saying so in its commit body and pinning the reasoning in-code:
`build_status` kept `open_tasks` / `running_runs`, and `build_observability` kept
its `tasks` / `proofs` / `daemon_status` parameters, because the only reader left
was `hermes_cli/harness_parts/runtime_commands.py` — a module another lane owned.
S28 landed both halves once that module was free.

- **Status shrink** (`026bc7b30`): `open_tasks` / `running_runs` and the
  `_cmd_status` print that was their sole reader. All three removed observability
  parameters were `[]` / `None` literals in **both** callers, so everything they
  alone fed went too: `signals.open_tasks` / `proofs_total` / `stale_daemon` /
  `repeated_context_request_tasks` / `untriaged_issue_discoveries`, the whole
  `freshness.daemon_*` block, the daemon / context-request / issue-discovery
  intervention families with their `_risk_if_ignored` + `_allowed_actions` arms,
  and seven of ten `_self_heal_signals` counters (the task half). **Keep side:**
  `active_runs` / `queued_runs` / `waiting_runs` / `stale_runs` and every
  incident/run/worker signal — those read live parameters. Live JSON key-shape
  diff on alice: 17 removals on `harness status`, 15 on `harness observe`, zero
  additions, no surviving field changed.
- **Consequence, recorded rather than hidden:** the removed
  `scope_control.untriaged_issue_discoveries` import was that function's ONLY
  production caller, so it is now caller-free while `scope_control` itself stays
  live. It was NOT deleted (different module, live siblings); the S27 reachability
  roots now say so explicitly. Re-checked in the same pass:
  `scope_control.find_discovery_task` is imported at `hermes_cli/harness.py:155`
  with **no call site anywhere in `hermes_cli/`** — a bound name, not a use. Both
  belong to a future reachability pass.
- **Reconcile graph render** (`75ffaac02`): the graph-prune phase (`6c5040ed2`)
  archives owner-less runtime graphs and appends `flow_graph.pruned`, but reported
  only through `--json`. The human render now carries `graphs_pruned` /
  `graphs_held` / `graph_steering_settled` plus matching detail rows.
  `graph_steering_settled` counts only `changed: True` entries — phase 3 usually
  strips a departed owner's edge first, and reporting that agreement as a repair
  would be the same class of untruth. The JSON wire is byte-identical.

**Doctor speed is NOT fixed and is not this wave's to fix.**
`tests/hermes_cli/test_harness_cli.py -k doctor` is 8 fast tests plus
`test_harness_doctor_human_branch_renders_the_surviving_findings`, the only one
that invokes `_cmd_doctor`. It **exceeds the repo's 30 s per-test cap**
(`pyproject.toml:371`). Cause: `C:\Users\beast\AppData\Local\Temp\hermes-agent-wt`
holds **4,245** leftover worktree directories. Under pytest the runtime root is a
long tmp path, so `_worktree_base_dir()` (`repo_context.py:130-134`) falls back to
that `%TEMP%` base, and `reap_orphan_worktrees` spawns one
`git rev-parse --git-common-dir` per entry (`repo_context.py:570`) — **15.3 ms
measured × 4,245 ≈ 65 s**. Every probe returns `None` and every husk holds real
files (`.git` + `README.md`), so `_is_empty_husk` is false and the janitor
classifies them `not_a_git_worktree_with_files` and **keeps them**. This never
self-heals: see the operator-owned leftovers item below.

### S33–S39 residue outcomes (2026-07-30)

- **S33 repo-baseline retirement** (`a3296b437`): the zero-caller
  `_repo_context_for_render`, `_repo_context_progress_payload`, and
  `_attach_repo_baseline` trio left `persona_runtime.py`; its last-use imports and the
  now-production-caller-free `capture_repo_baseline` went too. The worktree-creator
  trio remains deliberate regression-test infrastructure.
- **S34 `AgentRun.llm` retirement** (`e0190436f`): the zero-caller metadata writer and
  exclusive helper cluster were removed with the model field and its unreachable read
  arms. Persisted v1 rows carrying `llm` remain readable because model deserialization
  ignores unknown historical keys.
- **S35 assignment migration** (`5a1c107a1`): the dry-run-capable archive migration
  classified 55 legacy live rows with non-null `task_id`; apply archived all 55, and the
  idempotence check reported zero eligible / zero archived. `evidence_kind` is now the
  free-floating discriminator and `goal_id` the live ownership key. The persisted model
  slot remains for archived-row compatibility.
- **Hermes CLI fixture containment** (`a56767cd8`, `faab6b556`): the three kanban
  fixtures restore purged modules, the env-loader test restores both the module and
  parent binding, dashboard auth restores `bound_host` / `bound_port`, and the kanban
  subprocess probes are portable on Windows.
- **S36 packet emit retirement** (`13e59568e`): `record_decision_packets`,
  `record_packet`, `make_packet`, and their exclusive writer helpers were removed while
  validation and historical packet readers stayed. `packet.recorded` was deregistered
  and the event-count authority moved 91 → 90. The residue pass then proved that
  `packet.duplicate` and `packet.normalized` also had no emitter and deregistered both
  (`93473e94d`), moving 90 → 88 and the contract hash from
  `68e2a44a60fc8587a6ca9ea0275a62b05dabba9120f52e8a85f5fdac16f0cbd1` to
  `73ee514b5454b513cbfb74138a86cd12b5ee2312c071e4e55e46528821f5a9b1`.
- **Canonical runner reliability** (`957589215`, `306ad3035`): missing output from
  `communicate()` is safe on timeout and normal completion; automatic parallelism is
  logged and bounded at `min(cpu_count, 8)` while explicit overrides remain uncapped;
  timeout-shaped nonzero stragglers receive one logged retry after the pool drains at
  one-worker isolation. Assertion failures are never retried.
- **S39 `mission_hud` ruling** (`149a9ae53`; Launcher characterization
  `3190babe`): the Launcher reader stays for historical data. Transcript rows retain
  `runtime_context.context_id`; `harness prompt-context show` resolves the persisted row
  from both the live and archive-never-delete observability stores. Fresh Hermes rows
  therefore dropped only the always-empty `mission_hud` parameter, emitter, and
  backfill, while the Launcher parser is pinned to tolerate an absent key and still
  renders a historical non-empty payload. `situational_hud` remains the distinct live
  runtime/steering projection.

### 2026-07-31 staleness audit (post-upstream-sync) — s40–s43 + live-defect fixes

Ran after the executed upstream merge (doc 18). Fork-owned surface re-audited by
AST reachability (3,625 defs / 139 files); the sync itself left **zero** fork
symbols newly caller-free. Outcomes on `main`:

- **Three live defects fixed** (`a21ab1a2a`): the exec'd free name
  `persona_instance_id_for_placement` (NameError on every `--add-instance`,
  swallowed — named-placement preservation was dead since introduction); the S8
  `tasks` NameError crashing `workspace show`/`delete --dry-run`/`archive`; and
  `_realm_row`'s hardcoded `"sync": "in_sync"` (now reads the sidecar honestly).
- **Relay sender attribution restored** (`7c2a8a7d8`): `c60413e17` (07-20) had
  deleted the marker call site — relayed rows rendered as the operator for 11
  days while the resolver's own tests stayed green. Re-wired through the
  continuity-era persistence path (staged `finish_reason` on the runtime's own
  user-row flush; prompt byte-identical) + 6 wire tests walking chokepoint →
  persisted row → attribution.
- **Removal waves s40–s43** (`4d395f6be`, `0e0917bd5`, `22c5a473c`,
  `e9c9b3d3d`): `objective_templates.py` whole, 20 dead import bindings, the
  S28 `scope_control` open question closed (`untriaged_issue_discoveries` +
  `find_discovery_task` cut; validators live), 8 harness_parts helpers, 30
  individual symbols across 22 live modules. S27/S29 witnesses retargeted to
  assert absence, never deleted. Event registry untouched at 88.
- Launcher Mission Control ran its parallel wave same-day (s40–s46 + s50–s52,
  −1,090 lib lines + QA-lane retirements; see the Launcher brain's Mission
  Control note).
- **Deferred debts and pending rulings are ledgered in
  [19 — Deferred Debt Ledger](19-deferred-debt-ledger.md)** — notably the
  role_envelopes/role_checklists family (would move the event count 88 → 82),
  the Launcher goal-detail consumer knot, and the P0–P5/F-1–F-8 proposal sets.

### s44–s45 — the two ruled cuts (2026-07-31)

The operator ruled CUT on ledger items 1 and 2 the same day they were opened.
Both were executed on `main` with red-first removal contracts.

- **S44 — role_envelopes / role_checklists store family** (`4e7aa0066`).
  `agent_runtime/role_envelopes.py` deleted whole (275 lines): its ONE
  production edge pointed **outward** (checkpoint's EntityClass read the
  directory; nothing imported the module), and its only importer anywhere was
  an **unused** import in `test_projector.py` — an unused import is what a
  module-level reachability gate counts as a caller, which is how this family
  survived three earlier waves. `RoleEnvelopeStore.open_or_resume` annotates
  `task: Task`, deleted at S8 and never imported here: a latent `NameError`
  hidden by `from __future__ import annotations`, the same tell S27 found.
  `role_checklists.py` went 420 → 113 lines, keeping only
  `validate_checklist_payload_structure` (live via
  `decision_contract_registry.validate_payload_keys`) and staying in place as a
  small leaf. Six contracts de-registered
  (`role_envelope.opened/continued/paused/closed`,
  `role_checklist.created/item_updated`) per the S36/S37 rule; the two
  writer-less checkpoint EntityClass rows and eight orphaned path helpers went
  too. **Event count 88 → 82; contract hash
  `73ee514b5454b513cbfb74138a86cd12b5ee2312c071e4e55e46528821f5a9b1` →
  `f655bd56bb378c1fa818f360a0f401d5d957c17df33b6a65cb2fd2a6982acfe6`.**
  Wave-3's `checklist_for_task_stage`-is-live ruling was **transitively
  falsified**, not overridden: it was correct when written, and its entire
  justification was the `role_envelopes` import. S15's near-miss pin on
  `role_envelope.paused` — the subtlest live emit in the registry, on the else
  branch of a ternary — was re-derived onto the removed side rather than
  deleted.
- **S45 — the four test-only whole modules** (`be759935c`).
  `budget_approval.py`, `context_requests.py`, `role_contracts.py`,
  `stage_intent.py` (902 lines) deleted with their four dedicated test files
  (21 tests). **Settled rule: a module whose entire importer set is the test
  written to exercise it is a closed loop, not covered code.** S29 had already
  recorded the contradiction verbatim for `context_requests` and deferred the
  call; this executed it. Two traps are now pinned in the retargeted witnesses:
  **internal self-reference is not liveness** (S29 kept `stage_intent` because
  it called `stage_requires_product_edit` three times *internally*, at the exact
  moment S29 deleted its last external caller — that check would have passed
  forever), and **a module reported dead but left importable reads as live to
  the next reachability pass**, which is why `context_requests` sat two extra
  waves.
- **Pre-existing gate bug fixed** (`703411f68`): `test_s40` forbade the strings
  `objective_templates` / `render_objective` in **any** `.py` or `.md`, and
  `2b0d8dd94` — the commit that opened the ledger — broke it by recording
  "`objective_templates.py` whole" in this very file. The gate was already red on
  `main`. `.py` keeps the absolute gate; Markdown is now gated on code forms
  only. A prose gate that cannot tell "this code calls it" from "this document
  says we removed it" makes the removal log unwritable.
- **Verification.** Red proof 19 failed / 8 passed before the cuts. Isolated
  committed-tree arithmetic: `tests/agent_runtime` 3300 → 3304 (−21 deleted
  tests, −3 s43 re-parametrization, +1 s23, +27 new contract tests). Live alice
  sanity: `harness snapshot --json` builds at schema 2 with 2 boards and the new
  contract hash; `harness status` renders; `harness checkpoint classes` returns
  8 classes with `role_envelopes` / `role_checklists` correctly absent.
- **Opened, not swept:** the `role_envelope` runtime-config block (11 fields +
  5 migrations validators) now governs nothing yet still ships on the live
  snapshot wire reading `enabled: true`. Recorded as ledger item 8 — removing it
  is a snapshot contract change needing Launcher lockstep, the same shape as
  item 5.

### Operator-owned leftovers — nothing in this repo will clean these up

Deliberate holds, not oversights. Each is outside git and safe to leave indefinitely;
listed so a future session does not re-derive them or assume they were missed.

1. **`X:\Eternia\.hermes\agent-runtime-archive-20260730-writerless\`** (~88 MB) — every
   writer-less runtime directory archived aside on 2026-07-30 rather than deleted:
   `role_checklists`, `role_envelopes`, `proof_batches`, `proof_sandbox`,
   `replay_scenarios`, `stage47_live_runs`, `worker_sessions`, `packet_artifacts`,
   `repo_bundles`, `operator-runs`, `self_tests`, `context`, plus the S0 residue
   (`incidents` 1,925 files, `burn_in` 78 MB — both byte-identical to the existing
   `agent-runtime-archive-20260729-mission-lane` copies), stale daemon/probe/PID files,
   ~62 root run-logs, `operator_backups/`, and the pre-edit profile config backups.
   Every archive move was gated on a live read afterwards (`harness status`,
   `checkpoint classes`, and `persona list` returning 15 rows / 11 chat). **Deleting this
   directory is the operator's call and finishes the reclaim.**
2. **Profile config backups** — `X:\Eternia\.hermes\profiles\backend-dev\config.yaml.bak-20260730`
   and the `gpt-launcher` / `backend-dev` copies inside the archive root above, from the
   2026-07-30 `launcher_qa` grants (both profiles verified field-identical to
   `launcher-dev`'s block afterwards). Disposable once the grants are trusted.
3. **`C:\Users\beast\AppData\Local\Temp\hermes-agent-wt\` — 4,245 broken worktree
   directories** (found 2026-07-30 by S28's doctor-speed check, above). These are
   NOT the thirteen registered `%TEMP%` worktrees already reclaimed via
   `git worktree remove`; these are the unregistered husks left behind. Each holds
   a `.git` pointer file and a `README.md`, so `harness worktree reap` classifies
   every one `not_a_git_worktree_with_files` and keeps it — **the janitor will
   never clear this**, and every `harness doctor` pays ~65 s of `git rev-parse`
   probes for it. Deleting the directory is the operator's call and is the whole
   fix; the alternative (making `reap_orphan_worktrees` bound or batch its probe)
   is a real code change and was not taken on this wave's authority.
4. **Shared-skill backups** — `mission-control-harness.bak-20260730` and
   `launcher-stagec-mcp-screenshot.bak-20260730` under `X:\Eternia\.hermes\shared\skills\`,
   from the same-day rewrites. Note these sit **inside the shared skills root**: they are
   not installed from this repo, so `harness install-harness-skills` will neither refresh
   nor remove them.

Already done and needing no action: the `wt-hml-s3-s12` worktree directory, the four
runtime Launcher worktrees under `.hermes/agent-runtime/wt/` (382 MiB), and the thirteen
registered `%TEMP%\hermes-agent-wt` worktrees (1.78 GiB) were all removed through
`git worktree remove` after their contents were proven recoverable from git objects —
that ~2.2 GiB is already reclaimed and is **not** what the archive directory above holds.
