# 11 — Neko Fork-Join + De-Hardwiring (executor plan)

> **Principle:** no hardwiring. The **graph** — nodes, the agent bound to each node's
> slot, and stage `depends_on` — is the ONLY source of truth for *who* runs and *in
> what order*. Python must not special-case stage ids (`backend_implementation`,
> `implement`), infer ordering from goal text/flags, or pre-pass stages. This is the
> doc-08 collapse applied to the default (flag-off) pipeline's specialization +
> backend-first choreography.

Status: **core done + proven; test migration is the executor task below.** The tree is
mid-refactor. Do NOT flip any flag or delete the legacy tower here — this is only the
default-blueprint fork-join + removing the stage-id/backend-first hardwiring around it.

---

## Part A — What is already done (this session)

### A1. Blueprint rewritten to fork-join (`agent_runtime/blueprints/neko_two_dev_default.yaml`)
`DEFAULT_TASK_BLUEPRINT_ID` is still `neko_two_dev_default`. New shape:

```
scope (lead/neko, hermes-agent)
  ├─▶ backend_implementation (backend_builder/backend_dev, EterniaBackend)   depends_on: [scope]
  └─▶ implement              (builder/dev, EterniaLauncher)                  depends_on: [scope]
                integrate (lead/neko, hermes-agent)   depends_on: [backend_implementation, implement] ─▶ done
```
- Both dev branches depend ONLY on `scope` → parallel (neither gates the other).
- `integrate` is neko's **join**: it waits for both branches, then the task completes.
- `agent_topology` fans out: `root: lead`, edges `lead→backend_builder` + `lead→builder`
  (was the chain `lead→builder→backend_builder`).
- `validate_blueprint` passes clean.

### A2. De-hardwiring code changes
- `agent_runtime/planning.py::_is_cross_stack_backend_first` → **returns `False`**
  (neutralized). Every downstream backend-first path keys off it
  (`_needs_cross_stack_launcher_completion`, sequential-specialist join,
  launcher-release gating) so ordering now falls entirely to graph `depends_on`.
- `agent_runtime/default_plan.py::_align_default_plan_to_task_state` → no longer
  pre-passes stages by id or hardcodes `"implement"` as the running target. Keeps only
  the terminal collapse (integrated/approved ⇒ all stages passed) and points
  `current_stage_id` at the graph root when unset.
- `agent_runtime/default_plan.py::_specialize_default_implementation_stage` → **no-op**
  (return early). No stage-id/goal-text rewriting; owners/repos come from slot bindings.
- Now-dead (unused; sweep in the executor pass, don't leave orphaned):
  `_specialize_default_no_edit_cross_stack_plan`, `_mark_default_noop_dependencies_passed`,
  `_is_default_noop_dependency` in `default_plan.py`; and the backend-first helper chain
  behind `_is_cross_stack_backend_first` (`_has_cross_stack_backend_first_flag`,
  `_text_implies_backend_first_cross_stack`, and any now-unreachable
  launcher-release/contract-join branches in `planning.py`).

### A3. Proven green (graph-driven dispatch works)
- `tests/agent_runtime/blueprints/test_blueprint_runtime.py` — **30 passed**, including:
  - `test_neko_two_dev_default_fork_join_dispatches_both_devs_then_neko_join` — full
    flow via the concurrent path: scope → both devs → neko join → done.
  - `test_neko_two_dev_default_fires_both_dev_lanes_concurrently_when_lanes_allow` — at
    `max_active_lanes=2`, scope passing releases BOTH dev slots in one tick (this is
    "neko deploys both sub-agents" on the graph).
  - `test_dependency_blocked_join_dispatches_dependencies_not_the_join` — the join is
    never dispatched before both its `depends_on` pass.
  - `test_blueprint_agent_topology_instantiates_to_mission_plan_summary` — fan-out topology.
- `tests/agent_runtime/test_mission_plan.py` — **16 passed** (helper now asserts the
  4-stage fork-join contract; the removed-hardwiring tests were rewritten/deleted with
  rationale comments in place).

### A4. Known behavioral decision (DECIDE in the executor pass)
The **single-lane** `next_action` (swarm off) punts a fork to neko with reason
`"needs goal-owner adjudication"` instead of picking a ready branch. The **concurrent**
`next_actions` path (swarm on, `max_active_lanes ≥ 2`) dispatches both branches cleanly.
Options:
- **(i)** Accept adjudication at single lane (neko takes a turn to choose) — no code change.
- **(ii)** Make single-lane `next_action` deterministically dispatch the first ready
  branch (fully graph-driven, no adjudication punt). Preferred if we want swarm-off to
  behave identically to swarm-on minus concurrency.

Recommended: **(ii)**, so behavior is graph-driven at any lane count; adjudication should
be reserved for genuine ambiguity, not "two nodes are legitimately ready."

---

## Part B — Executor task: migrate the remaining tests to the graph-driven contract

**49 tests across ~12 files** still encode the old backend-first serial behavior. Bring
each to the graph-driven contract. The number reflects how deeply backend-first was
woven — it is expected, not a sign the change is wrong.

### Rules
- **Delete** a test only if it asserts *only* removed behavior (backend auto-pass,
  goal-text recipe injection, forced backend-before-launcher). Leave a rationale comment
  where it stood.
- **Rewrite** a test that has a valid graph-driven analog to assert the new contract.
- **Never** re-introduce hardwiring to make a test pass.
- Full suite must return to green at ≥ its floor (was **1347** before this refactor;
  after deletes/rewrites, record the new floor).
- No new stage-id special cases in product code.

### The graph-driven contract to assert
- Order follows `depends_on` only; agents follow slot bindings; no stage-id branches.
- Parallel siblings (shared single dependency) are both eligible once that dep passes.
- A join node is dispatched only when ALL its `depends_on` have passed.
- At `max_active_lanes ≥ 2`, eligible siblings dispatch in the same tick.
- A no-work node (e.g. a backend slice with nothing to change) is passed by its
  **assigned agent** reporting "no change," not by Python pre-passing it.

### Failing files + categories (from the full run)
1. **`test_planning.py` (~11)** — backend-first / launcher-release / contract-join /
   no-edit inference. Mostly **delete** (they test removed choreography):
   `test_neko_launcher_release_narrows_broad_cross_stack_repos_to_launcher`,
   `test_neko_contract_join_packet_only_releases_launcher_before_qa`,
   `test_neko_proof_backed_join_*`, `test_neko_cannot_release_launcher_before_backend_proof`,
   `test_neko_handoff_no_edit_*`, `test_neko_harness_*_no_edit_status_snapshot_gate`,
   `test_backend_dev_orchestration_only_plan_fails_closed_for_bounded_cross_stack_burn_in`,
   `test_typed_neko_acceptance_creates_plan_without_shrinking_parent_goal`,
   `test_dev_stage_plan_skips_orchestration_only_neko_and_qa_stages`.
2. **`test_ticker.py` (~22)** — end-to-end drives assuming 3 serial stages + backend
   routing. **Rewrite** for the 4-stage fork-join (new stage ids/order, integrate join,
   no backend-first routing). Includes command-proof/incident/budget drives that just
   need the updated default-plan shape, plus backend-routing ones
   (`test_backend_affected_repo_routes_run_dev_to_backend_dev`,
   `test_*_backend_routing`, `test_*_backend_continuation`) that must key off the graph's
   backend node, not backend-first inference.
3. **Cross-stack no-edit specialization** — assert removed recipe injection; **delete**
   or rewrite to graph-driven: `test_goal_runner.py::test_goal_runner_explicit_no_edit_cross_stack_blueprint_uses_recipe_gates`,
   `test_mission_goal.py::test_create_mission_goal_explicit_no_edit_cross_stack_blueprint_uses_recipe_gates`,
   `test_final_gate.py::test_no_required_gate_stage_advances_on_delivery_when_no_gate_command_derivable`,
   `test_unattended_lifecycle_evidence.py::test_handoff_payload_does_not_synthesize_no_edit_cross_stack_launcher_leg`.
4. **Misc end-to-end that drive the default blueprint** — update for the new shape:
   `test_stage52_role_envelopes.py` (3), `test_persona_assignments.py` (2),
   `test_worker_sessions.py` (1), `test_smoke_goal.py::test_no_model_smoke_runs_in_temp_root_and_finishes_done`,
   `test_state_machine.py::test_neko_scoped_launcher_fix_with_harness_support_scope_routes_to_dev`,
   `test_context_requests.py::test_context_request_rejects_unsafe_or_missing_path_without_freezing_scheduling`.
5. **The big one — `test_neko_two_dev_default_full_suite.py::test_neko_two_dev_default_full_suite_no_edit_cross_stack`**
   — the scripted end-to-end. Rewrite for fork-join: neko scopes, the scripted runtime
   plays BOTH dev branches (order-independent), then neko's `integrate` join turn, then
   done. Drop the "backend first" order assertions; keep the observed-lane + isolated-
   worktree grounding + collapsed-contract-parity assertions. Run at
   `max_active_lanes ≥ 2` so both branches dispatch, and handle the `integrate` neko turn
   in the scripted runtime (it must NOT return another `scope_route`).

### Sweep
After the tests, delete the now-dead functions listed in A2 (grep-gate that
`backend_implementation`/`implement` no longer appear as stage-id literals in
`default_plan.py`/`planning.py` product paths).

---

## Part C — "See it work" gate (before handing B to GPT)
Validate the fork-join actually self-drives before the test grind:
1. Live default goal, **swarm on** (`swarm.enabled: true`, cert bypass acknowledged),
   full tick, unblocked. Expect: neko scopes → **both** Backend Dev and Launcher Dev
   prompted (concurrently at ≥2 lanes) → neko `integrate` join → done, unattended.
2. Confirm from the stores: 4 stages, both dev branches ran their own nodes in their own
   repos, integrate ran as neko, no backend-first serialization, no stage-id pre-pass.
3. Mission Control: the Agent Console strip should show neko branching to Backend + Launcher
   (needs the separate `_pipelineStages` topology-driven fix — the strip is currently a
   hardcoded linear `Goal › Neko › devs › QA › Proof` and ignores `agent_topology`).

Only after C passes does the 49-test migration (Part B) get handed to the GPT executor.
