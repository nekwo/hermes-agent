# 08 — Blueprint-as-Script Collapse (implementation-ready staged plan)

> Purpose: make the **blueprint the script**, the **agents just Hermes agents
> differentiated only by prompt + HUD**, and the **harness a thin substrate — not a
> second control loop**. Today the flow is hardcoded across ~13.7k lines of Python
> special-cases while the blueprint YAML is a decorative 88-line graph, and a capable
> Hermes `AIAgent` loop is throttled into emitting one `AgentDecision` that a *second*
> agent loop (Neko) then adjudicates. The target: Neko's skill authors the stages for
> the actual goal (nothing baked), each agent runs its native Hermes loop to completion
> (work → test → fix → done), steering is the only coordination verb, and the harness
> does exactly four things — resolve workdir, inject prompt+HUD, kill hangs, capture
> diff/trace. Nothing hardcoded; nothing babysat.

## Owner's intent (source of truth)

1. **Neko gets the goal. Its skill creates the stages. It hands the stages to the devs.**
2. **The devs do the work, run the tests, and fix the code until the tests pass.** That
   *is* the dev pass.
3. **That's it** — for a dev-only graph, a green dev pass is done.
4. **If QA is in the graph:** QA does another *full* test run to double-check, screenshots,
   then **steers the dev toward improvements**; dev fixes; QA again.
5. **Devs can be steered by QA or Neko.** Steering is the coordination mechanism.
6. **Nothing is hardcoded.** Stages, repos, counts derive from the goal.
7. **The target is what the blueprint is scripted as.** The blueprint is the executable
   contract; the harness runs it and does not reconstruct it from Python branches.
8. **The agents are already capable.** They run the real Hermes `AIAgent` loop with a
   terminal. An agent "type" is a **prompt + HUD**, nothing more. The harness must not
   babysit a runtime that already iterates.

## Ground truth (verified in code, 2026-07-03)

The harness already runs the full Hermes agent loop, then wraps it:

- `PersonaTickRuntime.run_tick` (`agent_runtime/persona_runtime.py:67`) →
  `_invoke_agent` (`:101`) → `ProfileAgentRunner.run(AgentRunRequest(...))`
  (`:136`) invokes the real `AIAgent` with `max_iterations=run.iteration_budget`, a real
  `workdir`, real `enabled_toolsets`, native terminal. **The agent genuinely iterates
  edit→test→fix.**
- The system prompt then forces that loop to *"Return exactly one AgentDecision JSON
  object"* (`persona_runtime.py:871`).
- `run_tick` parses it: `parse_structured_decision(raw)` → `validate_decision_for_role`
  → `validate_planning_decision` (`:94–96`), returns an `AgentDecision`.
- `TickEngine` (`ticker.py`) then **re-runs proofs, opens incidents, dispatches a second
  Neko loop, runs the budget handshake** — a control loop on top of the loop that already
  ran.

So the "type" of an agent is fully carried by two inputs to a normal Hermes run:
- **Prompt** = `system_message` (built in `persona_runtime._build_*` prompt assembly).
- **HUD** = `AgentContext` / `ctx.mission_hud`, rendered into `user_message` by
  `context_builder.py`.

Everything between "the Hermes loop returns" and "the stage is done" — the decision parse,
the contract validation, the re-run gate, the incident/adjudication/budget tower — is the
removable wrapper.

## The disease, quantified (deletion targets)

| Module | LOC | What it hardcodes (target: delete/shrink) |
|---|---:|---|
| `ticker.py` | 4287 | post-handoff re-run gate, proof collection, recovery dispatch |
| `planning.py` | 2172 | ~30 cross-stack/launcher/backend-first/coerce/block branches |
| `context_builder.py` | 1874 | HUD next-move projection + decision-menu recommendation |
| `mission_plan.py` | 1555 | plan mutation, repo derivation, handoff normalization |
| `decision_contract_registry.py` | 1298 | 23 `DecisionType`s + payload contracts |
| `packets.py` | 1147 | typed packet normalize/dedupe/carry |
| `default_plan.py` | 420 | placeholder-repo specialization of the default graph |
| `blueprints/routing.py` | 349 | outcome derivation, retry bounds, intervention routing |
| `simplified_contract.py` | 249 | decision-type projection shims |
| `final_gate.py` | 203 | harness re-run gate command selection |
| `budget_approval.py`+`incidents.py` | 184 | budget handshake + incident objects |
| **in scope** | **~13.7k** | |

The 2026-07-03 self-drive bug class (wrong-repo gate, scope flip, wrong-worktree grounding,
plan-release loop) is a *symptom* of pre-baked stages + a wrapper that re-adjudicates a
completed loop. Under this plan those bugs are unrepresentable, not fixed.

## Target architecture

```
harness = { resolve workdir · inject prompt+HUD · run AIAgent loop · kill on hang · capture diff+trace }

goal
  → neko  (prompt: "author stages for this goal"; HUD: goal + repo aliases + template)
        → emits the stage list via ONE tool call (authored, repos derived, ⊆ known aliases)
  → for each stage:
        dev  (prompt: "own this stage; work, run tests, FIX until green, then stop";
              HUD: stage objective + workdir + test command)
             → runs its native Hermes loop to completion; green run = done
  → if graph has a qa node:
        qa  (prompt: "re-run full tests, screenshot, steer the dev toward improvements";
             HUD: stage + dev diff + screenshot tool + steer channel)
             → loops with dev via steering until satisfied
  → done
```

There is no decision JSON, no re-run gate, no incident, no adjudication turn. An agent
finishes its Hermes session; the harness reads what it did.

## What the harness keeps (the entire surviving substrate)

- **Script interpreter** — `ScriptRunner`: walk authored stages, for each dispatch the
  slot's agent via `ProfileAgentRunner.run`, advance on the stage's own success signal.
- **Hang-kill** — the AS0 liveness watchdog (`liveness.py`, doc-06) is the one recovery
  mechanism that survives.
- **Anti-fake** — two in-loop checks that a green claim is real: the proof command actually
  ran (`agent_tool_trace` via `_validate_observed_trace_requirement`) and the diff did not
  gut assertions/skips (`_handoff_diff_weakens_tests`). No independent re-run.
- **Substrate** — one daemon lease, redaction on persisted text, the event log, worktree
  isolation. Unchanged.

## Global design decisions

- **D1 — prompt+HUD is the only differentiator.** No role logic in Python beyond "which
  prompt + which HUD + which toolset." `role_from_persona` stays only for prompt/HUD/toolset
  selection, never for flow control.
- **D2 — the Hermes loop's result IS the outcome.** Delete `parse_structured_decision` /
  `validate_planning_decision` / `validate_decision_for_role` from the hot path. The dev's
  green in-session run completes the stage; Neko's stage-authoring tool call sets the plan;
  QA's steer is a message. No `AgentDecision` adjudication.
- **D3 — steering is a message into the live worker session**, not a re-dispatch
  (reuse `mission_chat_steer.py` + worker-session resume). A failure is a steer or an
  explicit "stuck → operator," never an incident object.
- **D4 — cross-stack is not special.** Backend+launcher = a goal whose Neko skill authors
  more stages in more repos joined by ordinary `depends_on` edges.
- **D5 — flag-safe, delete-at-the-end.** All work ships behind `blueprint_script_mode`
  (runtime_config.py → config.py → migrations.py). The old path stays runnable until BS7
  proves the new path on the burn-in ledger (doc-03 retirement discipline); then it is
  deleted, not left dark.

## Baseline gate (before BS0)

```bash
python -m pytest tests/agent_runtime --collect-only -q   # record floor (currently 1329)
```
Every stage: full `tests/agent_runtime` green at ≥ floor + new tests, and its own LIVE proof.

---

## BS0 — Contract, flag, script skeleton (no behaviour change)

**Goal.** Land this doc + the flag + an empty-stage script runner that is dark until BS7.

**Add.**
- `runtime_config.py`: `blueprint_script_mode: bool = False`.
- `config.py`: load/echo the key. `migrations.py`: validate bool.
- `agent_runtime/script_runner.py` — new `ScriptRunner` skeleton: `run_stage(stage, run)`
  = resolve workdir → `ProfileAgentRunner.run(prompt=stage.prompt, hud=stage.hud)` → capture
  diff/trace → return `StageResult`. Guarded off unless `blueprint_script_mode`.
- `blueprints/neko_default_script.yaml` — template: `stages: []`, `slots` = neko/dev/qa,
  `on_unhandled: intervention`.

**Change.** Allow `stages: []` in `blueprints/schema.py:validate_blueprint`.

**Config.** `blueprint_script_mode` (3-file).

**Tests.** `tests/agent_runtime/test_script_runner.py::{test_flag_defaults_off,
test_empty_script_template_validates, test_script_runner_dispatches_single_stage_stubbed}`.

**Proof.** `python -m pytest tests/agent_runtime/test_script_runner.py -q`; full suite green
at floor; `hermes harness blueprint run neko_default_script --goal x` no-ops cleanly.

**Rollback.** Delete the module + key; flag-off means zero live-path change.

**Handoff prompt.** "Add `blueprint_script_mode` via the 3-file pattern, a `ScriptRunner`
skeleton in `agent_runtime/script_runner.py` that runs one stage through
`ProfileAgentRunner.run` and returns diff+trace, and an empty-stage template blueprint.
Everything guarded off. No existing path changes. Suite green at floor."

---

## BS1 — Neko authors stages dynamically (kills the live bug class) — SHIP FIRST

**Goal.** Neko's skill emits the stage list for the goal at runtime; delete the baked graph
and the placeholder repos.

**Add.**
- `harness-mission-lead` skill + a `author_stages` emit path: Neko returns a stage list
  `[{owner_slot, repo, objective, test_command}]`; `repo` derived from the goal and
  validated `⊆ known_repo_scope_labels()` (reuse the gap-1 guard / `explicit_repo_mentions`).
- `ScriptRunner.instantiate(template, authored_stages)`.

**Delete.**
- `default_plan._specialize_default_implementation_stage` + placeholder repos in
  `neko_two_dev_default.yaml`.
- `final_gate.default_blueprint_placeholder_repo_override` and its 3 call sites
  (`planning._release_stage_affected_repos`, `final_gate.stage_repo_for_gate`,
  `persona_runtime._stage_repo_scope_for_persona`) — the entire 2026-07-03 crutch.

**Change.** `mission_goal.create_mission_goal` (flag on) routes to `neko_default_script` +
Neko authoring instead of `ensure_default_mission_plan`.

**Config.** none new.

**Tests.** `tests/agent_runtime/test_neko_authoring.py::{test_single_repo_goal_authors_only_that_repo,
test_cross_stack_goal_authors_backend_and_launcher_stages,
test_authored_repo_must_be_known_alias_else_repair, test_no_placeholder_repo_in_authored_plan}`.

**Proof (LIVE).**
```
hermes harness task create --start-daemon --title "…" --description "In the hermes-agent repo, run `python -m pytest tests/agent_runtime/test_liveness.py -q` …"
# assert: affected_repos == ['hermes-agent'] at every stage; no gate/grounding ever names EterniaLauncher/EterniaBackend
```
Reproduce the task_49f8ee3b / task_8e1e0832 shapes and show they cannot recur.

**Rollback.** Flag off → old `ensure_default_mission_plan` path; deleted crutches restored
from git only if rolled back.

**Handoff prompt.** "Behind `blueprint_script_mode`, make Neko author the stage list for
the goal (repos derived + validated ⊆ known aliases, reuse the gap-1 guard). Delete
`_specialize_default_implementation_stage`, the YAML placeholder repos, and
`default_blueprint_placeholder_repo_override` + its 3 call sites. Live-prove a single-repo
goal never touches another repo."

---

## BS2 — Dev pass owns its proof (collapse the second lane)

**Goal.** The dev's own green in-session run completes the stage. No harness re-run.

**Delete.**
- `ticker._build_authoritative_stage_gate_decision`, `_should_auto_run_final_gate`,
  `_stage_gate_commands`, the gate branch of `_collect_command_proof`.
- `final_gate.py` recipe/default selection (keep nothing but the anti-fake helpers if reused).
- gate proof-batch plumbing + `proof_command_policy` narrowing that only served the re-run.

**Keep.** `_validate_observed_trace_requirement` (proof command really ran) +
`_handoff_diff_weakens_tests` (diff not gutted) as the stage completion check in
`ScriptRunner`.

**Change.** `ScriptRunner.run_stage`: stage passes iff the agent's own `agent_tool_trace`
for the stage's `test_command` is green **and** anti-fake holds.

**Config.** none.

**Tests.** `tests/agent_runtime/test_dev_pass_proof.py::{test_green_observed_run_completes_stage,
test_faked_green_without_trace_rejected, test_weakened_test_diff_rejected,
test_no_harness_rerun_proof_recorded}`.

**Proof (LIVE).** A stage reaches done with only the dev's own trace as proof (no
`auto_final_gate` proof id); a no-trace or weakened-diff hand-off is rejected in-turn.

**Rollback.** Flag off restores the re-run gate.

**Handoff prompt.** "Behind the flag, make the dev's own green observed test run complete
the stage; delete the harness authoritative re-run gate and `final_gate` recipe selection;
keep the trace-ran + diff-not-weakened anti-fake checks as the completion gate."

---

## BS3 — QA pass = full retest + screenshot + steer

**Goal.** QA is a scripted node: its own full test run + screenshot + steer, looping with dev.

**Change.**
- QA prompt: "re-run the full suite yourself, capture the Stage-C screenshot, steer the dev
  with concrete fixes." HUD: dev diff + screenshot tool + steer channel.
- QA outcome is `{satisfied | steer(dev, notes)}` (a message via D3), not a verdict over
  dev-supplied proof ids. Bounded by the stage's `max_attempts`.

**Delete.** `qa_verdict` gate-over-proof-ids path; `_validate_qa_handoff_proof_readiness`.

**Config.** none.

**Tests.** `tests/agent_runtime/test_qa_pass.py::{test_qa_runs_full_suite_independently,
test_qa_attaches_nonblank_screenshot, test_qa_steer_reaches_same_dev_session,
test_qa_loop_bounded_by_max_attempts}`.

**Proof (LIVE).** A QA-in-graph goal shows two real test runs (dev + QA) + a non-blank
fullscreen screenshot; a QA steer reaches the same dev worker session.

**Rollback.** Flag off restores `qa_verdict`.

**Handoff prompt.** "Make the QA node run the full suite itself, screenshot, and steer the
dev (D3 channel), looping to a bounded retry. Replace the proof-id verdict with
satisfied/steer over QA's own run."

---

## BS4 — Steering replaces incidents / adjudication / budget handshake

**Goal.** Failure handling is steering, not objects.

**Delete.**
- `incidents.py` as a routing driver; the Neko adjudication turn + `recovery_flags.py`
  one-pass fingerprint; `budget_approval.py` + `WAITING_ON_APPROVAL`/`approve_continuation`;
  the `_coerce_neko_*` coercions in `planning.py`; the settled-boundary incident carve-outs
  in `ticker.py`.

**Keep.** hang-kill watchdog; budget as a *soft* per-stage ceiling that **steers** ("over
budget — wrap up or say you're stuck") instead of opening an approval lane.

**Change.** A failed/stuck stage → Neko or QA steer into the live worker; unrecoverable →
one explicit `stuck → operator` surface.

**Config.** `stage_soft_budget_steer: bool = True` (3-file) to gate the soft-ceiling steer.

**Tests.** `tests/agent_runtime/test_steer_recovery.py::{test_failed_stage_steered_in_session_no_incident,
test_stuck_stage_surfaces_to_operator_once, test_soft_budget_emits_steer_not_approval}`.

**Proof (LIVE).** A stage that fails its test recovers via steer with zero incident rows,
zero adjudication runs, zero approval prompts; a truly stuck stage surfaces once.

**Rollback.** Flag off restores the incident/adjudication/budget tower.

**Handoff prompt.** "Replace incident→adjudication→budget-approval with steering: a failed
stage is steered into the live worker; unrecoverable → one operator surface; budget becomes
a soft steer. Delete the incident/adjudication/approval machinery behind the flag."

---

## BS5 — Collapse the decision contract + packet protocol

**Goal.** With scripted flow + steering, the agent verbs collapse to **author-stages · work ·
steer · satisfied · stuck**.

**Delete/shrink.** `packets.py` (1147) → near-zero; `decision_contract_registry.py` payload
contracts → the surviving verbs; `simplified_contract.py` shims; the decision-menu next-move
projection in `context_builder.py`; `parse_structured_decision`/`validate_planning_decision`
off the hot path (kept only if a verb still needs a tiny structured emit, e.g. `author_stages`).

**Config.** none.

**Tests.** `tests/agent_runtime/test_verb_collapse.py::{test_every_flow_expressible_with_collapsed_verbs,
test_no_deleted_decision_type_referenced, test_parity_envelope_still_renders}`.

**Proof.** grep-gate: no surviving code path references a deleted `DecisionType`; parity
envelope renders; suite green.

**Rollback.** Flag off restores the contract registry.

**Handoff prompt.** "Collapse the 23 decision types + packet protocol to the five scripted
verbs; delete `packets.py` and the contract/projection machinery behind the flag; keep only
`author_stages` as a structured emit."

---

## BS6 — Delete cross-stack choreography + collapse bookkeeping

**Goal.** Cross-stack becomes ordinary authored stages+edges; one ownership record.

**Delete.** the ~30 cross-stack/launcher/backend-first branches in `planning.py`
(`_needs_cross_stack_launcher_completion`, `_should_release_backend_first_slice`,
`_block_launcher_release_until_backend_proof`, `_route_backend_contract_packet_repair`,
the `_payload_is_launcher_handoff` family, `_ensure_scoped_dev_handoff_stage`, …).

**Change.** Collapse `WorkerSessionStore` + role-envelope + `persona_assignment_store` to one
"who owns this stage now" record consumed by `ScriptRunner`.

**Config.** none.

**Tests.** `tests/agent_runtime/test_cross_stack_as_stages.py::{test_backend_then_launcher_runs_from_edges_only,
test_no_cross_stack_named_function_executed, test_single_ownership_record}`.

**Proof.** A cross-stack goal runs purely from authored stages+`depends_on`; grep-gate shows
the cross-stack functions are gone; a backend→launcher edge still blocks the launcher stage.

**Rollback.** Flag off restores `planning.py` choreography.

**Handoff prompt.** "Delete the cross-stack/launcher/backend-first branches in `planning.py`;
express cross-stack purely as authored `depends_on` edges; collapse the three bookkeeping
stores to one ownership record."

---

## BS7 — Reduce harness to substrate + delete the old path

**Goal.** Flip the flag on after burn-in; delete the dark old path.

**Change.** `blueprint_script_mode` default `True` after 10 green unattended runs on the
burn-in ledger (single-repo, cross-stack, QA-in-graph, chaos-drill). Delete the old
orchestrator path (no permanent dual fork — doc-03 gate).

**Proof (LIVE).** in-scope module LOC cut to a small fraction of 13.7k; 10 consecutive
unattended goals reach done with zero incidents/adjudication/approval; final
`harness status --json` clean; archive batches present.

**Rollback.** The last flag-on flip is the only irreversible step; keep one release able to
flip back before the delete commit.

**Handoff prompt.** "After 10 green unattended burn-in runs, default the flag on and delete
the old orchestrator path per doc-03. Prove the LOC cut and a clean unattended sweep."

---

## Non-goals & risks

- **Non-goal:** changing launcher/Stage-C visual-proof rules, the daemon lease, worktree
  isolation, or redaction — all survive.
- **Risk — self-certification.** Deleting the re-run leans on anti-fake. If an agent can
  emit `agent_tool_trace` without a real run, harden the trace (don't restore the re-run).
- **Risk — Neko authoring quality.** The model rests on Neko authoring good stages: BS1
  ships with the repo-scope guard + a stage-shape validator so a malformed plan is repaired,
  not run.
- **Risk — big deletions.** Strictly staged behind the flag; nothing deleted until its
  replacement is live-proven; BS7 gates the deletes.

## Sequencing

BS0 → **BS1 alone first** (kills the live bug class, smallest real win) → BS2 → BS3 → BS4
(retires the recovery tower) → BS5/BS6 (can overlap) → BS7 (substrate + delete). Each stage
is independently shippable, flag-gated, and LIVE-proven before the next.
