# 08 — Blueprint-as-Script Collapse (v3, implementation-ready; audited against code)

> Purpose: make the **blueprint the script**, the **agents just Hermes agents
> differentiated only by prompt + HUD**, and the **harness a thin substrate — not a
> second control loop**. Today the flow is hardcoded across ~13.7k lines of Python
> special-cases while the blueprint YAML is a decorative 88-line graph, and a capable
> Hermes `AIAgent` loop is throttled into emitting one `AgentDecision` that a *second*
> agent loop (Neko) then adjudicates. The target: Neko's skill authors the stages for
> the actual goal (nothing baked), each agent runs its native Hermes loop to completion
> (work → test → fix → done), steering is the only coordination verb, and the harness
> does five things — resolve workdir, inject prompt+HUD, run the loop, kill hangs,
> capture diff/trace. Nothing hardcoded; nothing babysat.

## 0. Audit ledger (what the v3 deep-audit verified / corrected vs v2)

Every load-bearing claim below was verified against code on 2026-07-03 (floor: 1329
tests). Corrections from the audit — these change the implementation, read them first:

1. **Anti-fake is already harness-observed, not agent-claimed — stronger than v2 assumed.**
   Every `run.tool.finished` progress event flows through `progress.py:190` →
   `record_self_test_from_progress` (`self_test_evidence.py:122`) which records
   `SelfTestEvidence` + a `proof_observed_*` Proof (source=`agent_tool_trace`) with the
   real command, exit code, stdout/stderr artifacts, and **honest status derivation**
   (`_status_from_payload`, `:297` — refuses to default unknown→passed; that default was
   a live bug, fixed, comment documents it). The agent cannot fabricate this record; the
   harness writes it from the tool stream. **Correction:** stage completion must match the
   *stage's specific* `test_command` via `command_hash` — any-green-self-test is not
   enough — and `looks_like_self_test_command`'s runner allowlist (`:183` — pytest,
   flutter test/analyze, dart analyze, manage.py test/check, npm/pnpm test) must contain
   the stage's runner or the authored `test_command` is invalid at authoring time.
2. **"Steer into the live worker mid-turn" does not exist for workers — corrected.**
   `mission_chat_steer.py` is a file-inbox that injects into an ACTIVE *mission-chat*
   turn only. Worker continuity is `run.session_id` reuse: `WorkerSessionStore.resume`
   (`worker_sessions.py:50–78`) + `AgentRunRequest(session_id=...)`
   (`persona_runtime.py:152`) give the next run the same conversation. **Steering =
   a next turn in the same session whose user_message is the steer.** Deterministic,
   cache-friendly, already wired. Mid-turn injection is a possible later extension, not
   part of this plan.
3. **QA screenshots are harness-executed today — corrected.** The QA agent emits
   `request_screenshot` and the harness runs `_collect_visual_proof` (`ticker.py:1601`)
   against the `launcher_qa` MCP. Target model: the QA persona drives the `launcher_qa`
   MCP **itself** in-session (Stage C is already MCP-only for agents), and the harness
   records the screenshot artifact from the tool trace via a sibling recorder to
   `record_self_test_from_progress`. The existing screenshot validity checks (nonblank,
   fullscreen, redaction-clean) are kept as the visual anti-fake.
4. **The diff-weaken check is reusable but must be detached from decision flow.**
   `_handoff_diff_weakens_tests` (`ticker.py:2159`) reads
   `task.harness_self_heal["stage_observations"]` populated by the decision-driven
   observation recorder. Extract the pure logic to `diff_weakens_tests(diff_text) -> bool`;
   `ScriptRunner` captures the diff itself (`git_diff_since_baseline`) and calls it.
5. **Budget propagation verified.** `RunBudgetExceeded(session_id=...)` raises from
   `profile_runner.py:213/257/263` through `persona_runtime.py:167`. ScriptRunner
   catches it directly — no `WAITING_ON_APPROVAL` state needed. `incidents.
   classify_exception` is **kept** (it is error *classification*; only incident
   *routing* dies).
6. **Daemon reuse seam named.** `MissionDaemon(engine_factory=...)` (`daemon.py:54,62,127`)
   is the swap point. Provide a `ScriptEngine` exposing
   `run_until_settled(task_id=..., max_actions=...)` returning `stop_reason`
   (`"task_terminal"` on done) and the whole daemon — targeting, terminal exit,
   auto-archive, liveness thread, orphan-reap, status file — reuses unchanged.
7. **Mission Control compatibility is an invariant, not an accident.** `snapshot.py`
   renders from `task.mission_plan.stages` + `AgentRun` + `Proof` rows
   (`snapshot.py:24,1163,1342`). Authored stages persist as ordinary
   `MissionPlanStage` rows and ScriptRunner updates `StageStatus`/run/proof rows exactly
   as today → the launcher UI and parity envelope keep working with zero changes.
8. **BS6 corrected: `WorkerSessionStore` is the record to KEEP.** It already carries
   ownership (task/persona/stage), `active_run_id`, and the `session_id` that steering
   depends on. `RoleEnvelopeStore` (`role_envelopes.py:48`) and
   `PersonaAssignmentStore` (`persona_assignments.py:739`) are the redundant two that
   get absorbed/deleted. (v2 wrongly said collapse all three into a new record.)
9. **Neko's authored-stage emit format pinned.** One JSON object in the final response,
   parsed by a new small `parse_authored_stages` (reuse
   `decision_schema._extract_json_blobs`), validated by a stage-shape validator, with
   **one** in-session repair turn (same `session_id`, feedback as user_message) before
   `stuck → operator`. The 23-type `AgentDecision` apparatus is not involved.

## 1. Owner's intent (source of truth)

1. **Neko gets the goal. Its skill creates the stages. It hands the stages to the devs.**
2. **The devs do the work, run the tests, and fix the code until the tests pass.** That
   *is* the dev pass.
3. **That's it** — for a dev-only graph, a green dev pass is done.
4. **If QA is in the graph:** QA does another *full* test run to double-check, screenshots,
   then **steers the dev toward improvements**; dev fixes; QA again.
5. **Devs can be steered by QA or Neko.** Steering is the coordination mechanism.
6. **Nothing is hardcoded.** Stages, repos, counts derive from the goal.
7. **The target is what the blueprint is scripted as.** The blueprint is the executable
   contract; the harness runs it, it does not reconstruct it from Python branches.
8. **The agents are already capable.** They run the real Hermes `AIAgent` loop with a
   terminal. An agent "type" is a **prompt + HUD**, nothing more.

## 2. Ground truth (verified seams an implementer starts from)

- `PersonaTickRuntime.run_tick` (`persona_runtime.py:67`) → `_invoke_agent` (`:101`) →
  `ProfileAgentRunner.run(AgentRunRequest(...))` (`:136`) already runs the full
  `AIAgent` loop: `max_iterations=run.iteration_budget`, real `workdir`
  (`repo_ctx.workdir`), `enabled_toolsets=effective_toolsets(persona)`,
  `session_id=run.session_id`, native terminal.
- The prompt then demands *"Return exactly one AgentDecision JSON object"*
  (`persona_runtime.py:871`); `run_tick` parses/validates (`:94–96`); `TickEngine`
  adjudicates the result (re-run gate → incidents → Neko loop → budget handshake).
- **Type = prompt + HUD:** prompt is the assembled `system_message`; HUD is
  `AgentContext`/`ctx.mission_hud` rendered into `user_message` by `context_builder.py`.
  `role_from_persona` survives only to select prompt/HUD/toolset — never flow control.
- Harness-observed evidence already exists per audit item 1 (self-tests) and is extended
  to screenshots per item 3.

## 3. The disease, quantified (deletion targets)

| Module | LOC | Fate |
|---|---:|---|
| `ticker.py` | 4287 | shrink to a stub (gate/recovery/dispatch die; legacy path deleted at BS7) |
| `planning.py` | 2172 | delete ~30 cross-stack/coerce/block branches; shrink to near-zero |
| `context_builder.py` | 1874 | keep HUD rendering; delete decision-menu/next-move projection |
| `mission_plan.py` | 1555 | keep `MissionPlanStage` model/persistence; delete mutation choreography |
| `decision_contract_registry.py` | 1298 | delete (replaced by 5 scripted verbs) |
| `packets.py` | 1147 | delete |
| `default_plan.py` | 420 | delete (Neko authors stages) |
| `blueprints/routing.py` | 349 | shrink: keep `apply_stage_outcome` graph-walk; delete decision-outcome derivation |
| `simplified_contract.py` | 249 | delete |
| `final_gate.py` | 203 | delete (anti-fake helpers move out first) |
| `budget_approval.py` + `incidents.py` | 184 | delete approval lane + incident routing; **keep `classify_exception`** |
| **in scope** | **~13.7k** | |

## 4. Target architecture

```
harness = { resolve workdir · inject prompt+HUD · run AIAgent loop · kill on hang · capture diff+trace }

goal
  → neko  (prompt: "author stages for this goal"; HUD: goal + known repo aliases + slot list)
        → final response contains one JSON stage list (authored; repos derived ⊆ known aliases)
  → for each stage (respecting depends_on):
        dev  (prompt: "own this stage: work, run `<test_command>`, FIX until green, then stop";
              HUD: stage objective + workdir + test_command)
             → native Hermes loop to completion;
               stage done ⇔ harness-observed green run of THIS stage's test_command
                          ∧ diff does not weaken tests
  → if graph has a qa node:
        qa  (prompt: "re-run the full tests yourself, screenshot via launcher_qa MCP,
              then either 'satisfied' or write a steer"; HUD: stage + dev diff + steer format)
             → qa's own run + screenshot recorded from ITS tool trace;
               steer ⇒ next dev turn in the dev's SAME session with the steer as user_message;
               loop until satisfied, bounded by stage max_attempts
  → done  (daemon sees task_terminal → auto-archive, exactly as today)
```

No decision JSON on the hot path, no re-run gate, no incident objects, no adjudication
turn, no approval handshake.

## 5. What the harness keeps (the entire surviving substrate)

- **`ScriptRunner`** — walk authored stages (`depends_on` edges via the kept
  `apply_stage_outcome` graph-walk), dispatch the slot's agent through
  `ProfileAgentRunner.run`, evaluate the completion contract, advance.
- **Hang-kill** — AS0 liveness watchdog (`liveness.py`), unchanged. The one survivor of
  the recovery lane.
- **Anti-fake** — (a) harness-observed green run of the stage's exact `test_command`
  (`SelfTestEvidence.command_hash` match, `status == "passed"`); (b)
  `diff_weakens_tests(diff)` on the ScriptRunner-captured diff; (c) for visual stages,
  nonblank/fullscreen/redaction-clean screenshot recorded from QA's own tool trace.
- **Substrate** — daemon (lease/targeting/auto-archive/orphan-reap), worktree isolation
  (`isolated_repo_context_for_run`), redaction, event log, `WorkerSessionStore` (the one
  ownership + session-continuity record), Mission Control snapshot compatibility (§0.7).

## 6. Resolved design decisions (no open questions for the implementer)

- **D1 — prompt+HUD is the only differentiator.** Role selects prompt/HUD/toolset;
  nothing else branches on role.
- **D2 — the Hermes loop's result IS the outcome.** Stage completion is evaluated from
  harness-observed evidence (D6), not from a structured decision. The only structured
  emit that survives is Neko's authored stage list (§0.9).
- **D3 — steering = next turn, same session.** `WorkerSessionStore` keeps the dev's
  `session_id`; a steer (from QA or Neko) is dispatched as
  `AgentRunRequest(session_id=<dev session>, user_message=<steer>)`. No global steer
  state; no re-dispatch from scratch; no mid-turn injection in this plan.
- **D4 — cross-stack is not special.** More stages in more repos, joined by ordinary
  `depends_on` edges Neko authored.
- **D5 — flag-safe, delete-at-the-end.** Everything behind `blueprint_script_mode`
  (runtime_config.py → config.py → migrations.py). Old path runs until BS7 burn-in
  (doc-03 retirement discipline), then deleted, not left dark.
- **D6 — stage completion contract (exact).** A stage with `test_command` completes when
  a `SelfTestEvidence` row exists with: same `task_id` + `stage_id`, `command_hash ==
  hash(stage.test_command)`, `status == "passed"`, recorded during this stage's run(s) —
  AND `diff_weakens_tests(captured_diff) is False`. A stage with
  `requires_visual_proof` additionally needs a valid screenshot artifact from the QA/dev
  trace. A stage with neither test_command nor visual requirement completes when its
  agent's run finishes without `stuck` (the 2026-07-03 no-required-gate lesson, kept).
- **D7 — failure ladder (exact).** Red run or non-matching evidence → **steer** (bounded
  by `max_attempts_per_stage`); `RunBudgetExceeded` → **one** soft-steer continuation
  turn in the same session ("over budget: wrap up or say stuck"); agent says stuck, or
  attempts exhausted, or budget re-trips → **one** `stuck → operator` surface
  (`operator_attention` task flag + event), then the daemon idles on that goal. No
  incident objects, no adjudication dispatch.
- **D8 — authored-stage schema (exact).**
  `{"stages": [{"id", "title", "owner_slot" ∈ template slots, "repo" (⊆
  known_repo_scope_labels() or absolute existing dir), "objective", "test_command"
  (must satisfy looks_like_self_test_command; omitted ⇒ no-test stage),
  "requires_visual_proof": bool, "depends_on": [ids], "max_attempts": int ≤
  limits.max_attempts_per_stage}]}` — validated by `validate_authored_stages`; one
  in-session repair turn on failure, then operator.

## 7. Baseline gate (before BS0)

```bash
python -m pytest tests/agent_runtime --collect-only -q   # record floor (1329 on 2026-07-03)
```
Every stage: full `tests/agent_runtime` green at ≥ floor + its new tests, plus its own
LIVE proof. Commit + push per stage (worktree can be reset by live automation).

---

## BS0 — Contract, flag, ScriptRunner + ScriptEngine skeleton (no behaviour change)

**Add.**
- `blueprint_script_mode: bool = False` — runtime_config.py → config.py → migrations.py.
- `agent_runtime/script_runner.py`:
  - `ScriptRunner.run_stage(task, stage, worker) -> StageResult` — resolve workdir from
    `stage.repo` (`resolve_affected_repo_workdir`; isolate via
    `isolated_repo_context_for_run`), build prompt+HUD for the slot, call
    `ProfileAgentRunner.run(AgentRunRequest(session_id=worker.session_id, ...))`,
    capture `git_diff_since_baseline`, collect `SelfTestEvidence` for the stage, return
    `StageResult{green, evidence_ids, diff, stuck}` per D6/D7.
  - `ScriptEngine.run_until_settled(task_id=..., max_actions=...)` — daemon-compatible
    facade (audit §0.6): pick next runnable stage by `depends_on` + status, run it,
    return `stop_reason` (`"task_terminal"` when all stages passed → sets
    `TaskState.DONE`).
- `blueprints/neko_default_script.yaml` — slots neko/dev/qa, `stages: []`,
  `on_unhandled: intervention`.

**Change.** `blueprints/schema.py:validate_blueprint` allows an empty stage list.

**Tests.** `tests/agent_runtime/test_script_runner.py::{test_flag_defaults_off,
test_empty_script_template_validates, test_run_stage_dispatches_stubbed_runner_and_captures_diff,
test_script_engine_reports_task_terminal_when_all_stages_pass,
test_completion_requires_command_hash_match}` (stub `ProfileAgentRunner`).

**Proof.** New tests + full suite green at floor; flag off ⇒ zero live-path change.
**Rollback.** Delete module + key.
**Handoff prompt.** "Check your brain first (AGENTS.md, doc-08 §0/§6). Add
`blueprint_script_mode` (3-file), `agent_runtime/script_runner.py` with
`ScriptRunner.run_stage` + daemon-compatible `ScriptEngine.run_until_settled` per doc-08
BS0, and the empty-stage template blueprint. Stage completion per D6 using
`SelfTestEvidence.command_hash`. All dark behind the flag. Suite ≥ 1329 green."

---

## BS1 — Neko authors stages (kills the live bug class) — SHIP FIRST

**Add.**
- `parse_authored_stages(text)` + `validate_authored_stages(stages, template)` in
  `script_runner.py` per D8 (reuse `_extract_json_blobs`; repo validation reuses the
  gap-1 guard `known_repo_scope_labels`/`canonical_repo_scope_label`).
- Neko authoring prompt + HUD block (goal text, known repo aliases, slot list, D8 schema,
  `looks_like_self_test_command` runner list); skill section in `harness-mission-lead`
  (reinstalls on persona bootstrap).
- One in-session repair turn on invalid emit (same `session_id`), then operator (D7).

**Change.** Flag on: `mission_goal.create_mission_goal` instantiates
`neko_default_script` and queues the Neko authoring stage instead of
`ensure_default_mission_plan`. Authored stages persist as `MissionPlanStage` rows (§0.7).

**Delete (flag-on path).** `default_plan.py` usage;
`final_gate.default_blueprint_placeholder_repo_override` + its 3 call sites
(`planning._release_stage_affected_repos`, `final_gate.stage_repo_for_gate`,
`persona_runtime._stage_repo_scope_for_persona`) — the 2026-07-03 crutch dies with the
placeholders it compensated for. (Physical deletion of the files at BS7.)

**Tests.** `tests/agent_runtime/test_neko_authoring.py::{test_single_repo_goal_authors_only_that_repo,
test_cross_stack_goal_authors_backend_and_launcher_stages,
test_unknown_repo_alias_gets_one_repair_turn_then_operator,
test_test_command_must_match_runner_allowlist, test_authored_stages_persist_as_mission_plan_rows}`.

**Proof (LIVE).** Flag on: `harness task create --start-daemon` with the gap-1 trap
description (repo mid-sentence + backticked focused command) → authored stages all in
`hermes-agent`, no placeholder repo anywhere in `affected_repos`/workdir/evidence;
reproduce the task_49f8ee3b / task_8e1e0832 shapes and show they cannot occur.
**Rollback.** Flag off → old path.
**Handoff prompt.** "Check your brain first (doc-08 §0.9/D8, gap-1 guard in
repo_context.py). Behind `blueprint_script_mode`: Neko authors the stage list (one JSON
emit, D8 schema, one repair turn), persisted as MissionPlanStage rows; wire
create_mission_goal; live-prove a single-repo goal never touches another repo. Update +
reinstall harness-mission-lead."

---

## BS2 — Dev pass owns its proof

**Change.** `ScriptRunner.run_stage` completion per D6 is the ONLY gate: harness-observed
green run of the stage's `test_command` (command_hash match) + `diff_weakens_tests`
false. Extract `diff_weakens_tests(diff_text)` as a pure function (from
`ticker._handoff_diff_weakens_tests:2159`) into `script_runner.py` or
`self_test_evidence.py`. Dev prompt: "own this stage; run `<test_command>`; fix until
green; do not stop on red without saying stuck."

**Delete (flag-on path).** No `_build_authoritative_stage_gate_decision`, no
`_should_auto_run_final_gate`, no `final_gate` selection, no gate proof batches, no
`proof_command_policy` narrowing — ScriptRunner never calls them.

**Tests.** `tests/agent_runtime/test_dev_pass_proof.py::{test_green_observed_run_completes_stage,
test_green_run_of_wrong_command_does_not_complete_stage,
test_no_observed_run_means_not_complete, test_weakened_test_diff_blocks_completion,
test_no_harness_rerun_proof_recorded}`.

**Proof (LIVE).** Flag on: a goal reaches done where the ONLY proof rows are
`proof_observed_*` from the dev's own session (no `auto_final_gate` /
`authoritative_gate_after_hand_off` intents); a run with no matching observed command
does not complete and triggers a steer.
**Rollback.** Flag off.
**Handoff prompt.** "Check your brain first (doc-08 D6, self_test_evidence.py). Make D6
the only completion gate in ScriptRunner; extract pure `diff_weakens_tests`; live-prove
done-with-only-observed-proof and steer-on-missing-evidence."

---

## BS3 — QA pass = full retest + screenshot + steer

**Add.**
- QA stage prompt: "re-run the full test suite yourself in the workdir; drive the
  `launcher_qa` MCP for a fullscreen screenshot when the stage is visual; end with
  `satisfied` or a concrete steer." QA toolset includes the `launcher_qa` MCP server
  (persona `required_mcp_servers`).
- Screenshot-evidence recorder: sibling to `record_self_test_from_progress` that captures
  launcher_qa screenshot tool results from `run.tool.finished` into a Proof (kept
  validity checks: nonblank, fullscreen, redaction-clean).
- Steer dispatch: QA's steer text → next dev turn via D3 (dev's `session_id` from
  `WorkerSessionStore`), bounded by stage `max_attempts`.

**Delete (flag-on path).** `qa_verdict`-over-proof-ids, `_collect_visual_proof`
harness-executed lane, `_validate_qa_handoff_proof_readiness`.

**Tests.** `tests/agent_runtime/test_qa_pass.py::{test_qa_full_retest_recorded_from_its_own_trace,
test_screenshot_recorded_from_qa_tool_trace_with_validity_checks,
test_steer_dispatches_next_dev_turn_in_same_session, test_qa_loop_bounded_by_max_attempts,
test_satisfied_completes_qa_stage}`.

**Proof (LIVE).** A QA-in-graph goal shows two independent observed test runs (dev + QA
sessions), a valid screenshot proof from QA's own trace, and one steer that verifiably
lands in the dev's same `session_id` (transcript continuity).
**Rollback.** Flag off restores `qa_verdict` + harness-executed visual proof.
**Handoff prompt.** "Check your brain first (doc-08 §0.3/D3; Stage C MCP runbook). QA
runs its own full retest + drives launcher_qa MCP itself; add the screenshot trace
recorder; steer = same-session dev turn. Live-prove the dev↔QA loop."

---

## BS4 — Steering replaces incidents / adjudication / budget handshake

**Change.** ScriptRunner implements the D7 ladder end-to-end: red → steer (Neko-authored
steer text or QA steer) → soft-budget continuation turn → `stuck → operator`
(`operator_attention` flag + event; daemon idles on the goal).

**Delete (flag-on path).** Incident routing (`open_incident_ids` driving dispatch), the
Neko adjudication dispatch + `recovery_flags` fingerprint, `budget_approval.py` +
`WAITING_ON_APPROVAL`/`approve_continuation`, `_coerce_neko_*`, the settled-boundary
incident carve-outs. **Keep:** liveness hang-kill; `classify_exception`.

**Config.** `stage_soft_budget_steer: bool = True` (3-file).

**Tests.** `tests/agent_runtime/test_steer_recovery.py::{test_red_run_steers_same_session_no_incident,
test_budget_exceeded_gets_one_soft_steer_then_operator,
test_stuck_surfaces_operator_attention_once, test_attempt_cap_ends_in_operator_attention}`.

**Proof (LIVE).** A stage that fails its test recovers via steer with zero incident rows,
zero adjudication runs, zero approval prompts; a truly stuck stage surfaces exactly once
and the daemon idles instead of looping.
**Rollback.** Flag off restores the tower.
**Handoff prompt.** "Check your brain first (doc-08 D7). Implement the failure ladder in
ScriptRunner; delete incident/adjudication/approval from the flag-on path; keep hang-kill
and classify_exception. Live-prove steer-recovery and single operator surface."

---

## BS5 — Collapse the decision contract + packet protocol

**Delete/shrink (flag-on path has no consumers; physical delete at BS7).**
`packets.py`; `decision_contract_registry.py`; `simplified_contract.py`;
`parse_structured_decision`/`validate_planning_decision`/`validate_decision_for_role`
off the hot path (`parse_authored_stages` is the only structured parse left); the
decision-menu/next-move projection in `context_builder.py` (keep the HUD *rendering*
used by ScriptRunner prompts).

**Tests.** `tests/agent_runtime/test_verb_collapse.py::{test_script_path_never_parses_agent_decision,
test_no_flag_on_codepath_references_decision_registry, test_parity_envelope_still_renders}`.

**Proof.** grep-gate on the flag-on path; snapshot parity envelope renders; suite green.
**Handoff prompt.** "Check your brain first (doc-08 §0.9). Remove every AgentDecision
touchpoint from the script path; keep parse_authored_stages as the sole structured emit;
prove by grep-gate + parity render."

---

## BS6 — Cross-stack as authored edges + bookkeeping collapse

**Delete (flag-on path).** The ~30 cross-stack/launcher/backend-first branches in
`planning.py` (`_needs_cross_stack_launcher_completion`,
`_should_release_backend_first_slice`, `_block_launcher_release_until_backend_proof`,
`_route_backend_contract_packet_repair`, the `_payload_is_launcher_handoff` family,
`_ensure_scoped_dev_handoff_stage`, …) — joins are authored `depends_on` edges.

**Change.** Keep `WorkerSessionStore` as THE ownership + session record (audit §0.8);
absorb/delete `RoleEnvelopeStore` + `PersonaAssignmentStore` from the flag-on path.

**Tests.** `tests/agent_runtime/test_cross_stack_as_stages.py::{test_backend_then_launcher_runs_from_edges_only,
test_launcher_stage_blocked_until_backend_stage_green,
test_no_cross_stack_named_function_on_script_path, test_worker_session_is_sole_ownership_record}`.

**Proof (LIVE).** A cross-stack goal (backend stage → launcher stage) runs purely from
authored edges; the launcher stage does not start until the backend stage is green.
**Handoff prompt.** "Check your brain first (doc-08 D4/§0.8). Cross-stack = authored
depends_on edges; WorkerSessionStore is the sole ownership record on the script path.
Live-prove the ordered cross-stack run."

---

## BS7 — Flip the flag, delete the old path

**Change.** After **10 green unattended burn-in runs** (mix: single-repo, cross-stack,
QA-in-graph, chaos-drill `daemon stop` mid-turn), default `blueprint_script_mode: True`.
Then physically delete: `default_plan.py`, `final_gate.py`, `packets.py`,
`simplified_contract.py`, `budget_approval.py`, incident routing, the `planning.py`
choreography, the `ticker.py` gate/recovery/dispatch body, `role_envelopes.py`,
`persona_assignments.py` (per doc-03: grep-gated, no permanent dual fork).

**Proof (LIVE).** LOC in-scope cut to a small fraction of 13.7k (measure and record);
10-run unattended sweep with zero incidents/adjudication/approval; final
`harness status --json` clean; archive batches present; doc-03 ledger updated.
**Rollback.** The delete commit is the only irreversible step; keep one release able to
flip the flag back before it.
**Handoff prompt.** "Check your brain first (doc-03 + doc-08). Run the 10-goal burn-in
matrix, flip the default, execute the grep-gated deletions, record the LOC cut."

---

## 8. Non-goals & risks

- **Non-goal:** changing Stage-C visual-proof validity rules, the daemon lease/targeting/
  auto-archive model, worktree isolation, redaction, or the Mission Control snapshot
  contract — all survive verbatim.
- **Risk — self-certification.** Completion leans on harness-observed evidence. The
  recorder is already harness-side (§0.1) so the agent can't fabricate rows; the residual
  hole is an agent wrapping the real command in something that lies about exit status —
  `_status_from_payload` already refuses unknown→passed; if gamed further, harden the
  recorder (never restore the re-run).
- **Risk — Neko authoring quality.** D8 validation + one repair turn + operator surface
  bound the blast radius of a bad authored plan; the repo guard (gap-1) is structural.
- **Risk — session drift on steering.** Steering reuses the dev session; if the provider
  invalidates the session, `WorkerSessionStore.resume` falls back to a fresh session with
  the steer + stage HUD as context (accept the cache loss, keep the contract).
- **Risk — big deletions.** Strictly flag-staged; nothing physically deleted before BS7
  burn-in.

## 9. Sequencing

BS0 → **BS1 alone first** (kills the live bug class; smallest real win) → BS2 → BS3 →
BS4 (retires the recovery tower) → BS5/BS6 (can overlap) → BS7 (flip + delete). Each
stage independently shippable, flag-gated, suite ≥ floor, LIVE-proven before the next.
