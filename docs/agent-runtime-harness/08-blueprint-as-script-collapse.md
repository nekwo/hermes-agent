# 08 — Root Node + Self-Looped Sub-Agents (v4, implementation-ready)

> **2026-07-30 — describes a removed subsystem.** The root-node execution model
> (`run_node`/`steer_node`, stages N0–N3, `root_node_engine.py`) was removed by
> [16 — Mission Lane Removal](16-mission-lane-removal.md). Retained because its
> controlling principle — *no judgment in Python; the harness is substrate only* —
> is the direct ancestor of the chat-only lane and the enforcement-free Agent
> Console graph. Do not implement from it.

> **Cleanup-wave correction (2026-07-30).** The historical account below also
> names surfaces that outlived the subsystem briefly and are now gone:
> `tools/node_control_tool.py` and its `run_node` / `steer_node` tools were
> deleted by `de14b06d2`; the fork-added `node_control` toolset block was removed
> by `e69db6e71`; and the caller-free `git_diff_since_baseline` and
> `diff_weakens_tests` helpers were removed by `354d7555a`. Every later mention
> of those names describes the retired design, not a live helper or tool.

> **Verification status (2026-07-04, adversarial re-derivation + remediation —
> doc-09 Prompt 2):** N0/N1/N2 implementation **VERIFIED**; N3 burn-in still
> **PENDING / flip NOT approved.** Suite green (1347 passed after remediation).
> Two independent live gap-1-trap goals (`task_e13dc2f8`, post-fix
> `task_41f18ab8`) reached `done` unattended: repo derived correctly to
> `hermes-agent`, dev node's own green run the only test evidence
> (`source=agent_tool_trace`), targeted daemon auto-archived, final status clean.
> No judgment-in-Python spirit violations; legacy decision tower byte-for-byte
> untouched. **Findings 2–5 now CLOSED (this commit):** (2) `diff_weakens_tests`
> is a real shared helper (`repo_context.diff_weakens_tests`, extracted from
> `ticker.py:2159`) wired into `run_node`'s evidence, plus the always-empty
> `changed_files` bug fixed; (3) the shared/flag-off path is clean again — the
> `progress.py` screenshot recorder is gated on `root_node_mode`, and QA's
> `launcher_qa` moved out of the static persona into flag-gated dispatch injection
> (`node_tools._child_enabled_toolsets`), restoring scope-aware MCP requirement;
> (4) the wrapper screenshot recorder now binds the artifact to the run's time
> window instead of mtime-globbing an arbitrary PNG; (5) a regression test locks
> `node_control` to root-only exposure. **Only remaining blocker to the flip:** a
> clean **10/10 unattended** burn-in sweep (the prior ledger was 8/10 — rows 08/10
> not-unattended, rows 05–07 no Proof rows). Deliberately deferred: "guarantee
> smoothness before forcing." Full outcome in `09-root-node-execution-prompts.md`.

> One idea: **every node is a standard self-looped Hermes agent; the root node runs the
> goal by spawning and steering sub-agent nodes — the same shape as Hermes'
> `delegate_task`.** A node is customizable but simple: prompt + HUD + toolset + workdir.
> The harness is substrate only: run the loops, resolve workdirs, capture evidence
> server-side, kill hangs, persist for Mission Control. **No judgment in Python.** The
> root's model judges the children's work — at worst, it steers them. This replaces the
> decision-contract / re-run-gate / incident / adjudication tower (~13.7k LOC across
> `ticker.py`, `planning.py`, `packets.py`, `decision_contract_registry.py`,
> `default_plan.py`, `final_gate.py`, `budget_approval.py`, `simplified_contract.py`,
> `mission_plan.py` choreography) with one control loop: the root agent's own.

## The model

```
node  = { prompt, HUD, toolsets, workdir }        # a normal Hermes AIAgent self-loop
root  = the Neko node, one session per goal

goal → root node starts
        skill: author the stages for THIS goal (repos derived, nothing baked)
        for each stage:  run_node(stage)          # spawn a sub-agent node, block, get result
             dev node: work, run the tests, fix until green, summarize
        judge the result (summary + harness-captured evidence in view)
             good  → next stage
             not   → steer_node(...)              # next turn in the child's SAME session
        if the graph has qa:  run_node(qa stage)  # qa re-runs the full tests itself,
             qa screenshots via launcher_qa MCP,   #   steers dev (relayed the same way)
        all stages good → root says done → task_terminal → daemon auto-archives
```

This is Hermes' existing delegation pattern (`tools/delegate_tool.py`: parent blocks on
child summary, leaf/orchestrator roles, depth caps) applied to the harness: the root is
an orchestrator-role agent; dev/qa are leaf nodes.

## What the harness is (all of it)

1. **Run loops** — `ProfileAgentRunner.run(AgentRunRequest(...))`
   (`persona_runtime.py:136`) already runs a full self-looped `AIAgent` with
   `max_iterations`, real `workdir`, real toolsets, `session_id` continuity. Nodes are
   just calls to it with different prompt/HUD/toolset/workdir.
2. **Two service tools, root-only** (service-gated; zero footprint elsewhere — see
   AGENTS.md footprint ladder):
   - `run_node(stage)` → resolve the stage repo to a workdir
     (`resolve_affected_repo_workdir` + `isolated_repo_context_for_run`), run the child
     node to completion, persist the stage/run rows, return
     `{summary, session_id, evidence}`.
   - `steer_node(session_id, message)` → run one more turn in the child's same session
     with the steer as the user message (`WorkerSessionStore` keeps `session_id`;
     `AgentRunRequest(session_id=...)`). Steering is a next turn — there is no mid-turn
     injection and none is needed.
3. **Server-side evidence capture (can't be faked, is NOT a gate)** — already exists:
   every `run.tool.finished` flows through `progress.py:190` →
   `record_self_test_from_progress` (`self_test_evidence.py`) recording real commands,
   exit codes, stdout/stderr with honest status derivation. Add one sibling recorder for
   `launcher_qa` screenshot tool results. `run_node` returns compact evidence handles
   (commands run, pass/fail, diff summary via `git_diff_since_baseline`, a
   `diff_weakens_tests` warning flag) **for the root to read and judge** — the harness
   never decides from them.
4. **Hang-kill + budget as plain limits** — the AS0 liveness watchdog (`liveness.py`)
   reaps genuinely hung runs; `iteration_budget`/`max_wall_seconds` are ordinary
   `AgentRunRequest` caps. `RunBudgetExceeded` returns to the root as a result
   ("child hit its budget") — the root decides to steer, re-run, or report stuck. No
   approval lane.
5. **Persistence for Mission Control** — authored stages persist as `MissionPlanStage`
   rows; child runs as `AgentRun` rows; evidence as `Proof` rows. `snapshot.py` and the
   launcher UI keep working with zero changes.
6. **Substrate that survives verbatim** — daemon (lease, targeting, `task_terminal`
   exit, auto-archive, orphan-reap), worktree isolation, redaction, event log,
   `WorkerSessionStore` (the one ownership + session record).

**Explicitly NOT in the harness:** completion contracts, failure ladders, incidents,
adjudication dispatch, re-run gates, decision JSON parsing/validation, packet
normalization, cross-stack choreography, budget approval. If the root mis-judges, the
fix is its prompt/skill/HUD — not new Python.

## Node customization (the whole config surface)

A node is declared by its persona: `{prompt (system_message), HUD blocks, toolsets,
repo/workdir, iteration_budget, max_attempts}`. The default graph ships three nodes —
root (Neko), dev, qa — but any node is addable by declaring a persona and letting the
root's skill route stages to it. `role_from_persona` survives only to pick
prompt/HUD/toolset. Nothing else in Python knows what a "dev" is.

## Verified seams (audited 2026-07-03; anchors machine-checked)

- Full self-loop already runs per node: `persona_runtime.py:67/101/136` →
  `ProfileAgentRunner.run` (`profile_runner.py:130`).
- Session continuity for steering: `worker_sessions.py:50–78` +
  `AgentRunRequest(session_id=...)` (`persona_runtime.py:152`).
- Evidence recorder: `progress.py:190` → `self_test_evidence.py:122` (honest status:
  `:297`); diff: `git_diff_since_baseline`; weaken-check logic to extract as a pure
  helper from `ticker.py:2159`.
- Budget: `RunBudgetExceeded(session_id=...)` from `profile_runner.py:213/257/263`.
- Daemon seam: `MissionDaemon(engine_factory=...)` (`daemon.py:54/62/127`) — flag-on
  supplies a `RootNodeEngine` whose `run_until_settled(task_id=...)` runs/continues the
  root session and returns `stop_reason="task_terminal"` when the root declares done.
- Delegation precedent: `tools/delegate_tool.py` (blocking child, roles, depth caps).

## Stages (flag: `root_node_mode`, 3-file pattern; floor = 1329 tests; commit+push per stage)

### N0 — Flag + service tools + root engine (dark)
- Add `root_node_mode: bool = False` (runtime_config → config → migrations).
- Add `agent_runtime/node_tools.py`: `run_node` / `steer_node` as service-gated tools
  exposed ONLY to the root persona in harness runs (registered via the existing
  `check_fn` pattern; not in any core toolset).
- Add `RootNodeEngine` (daemon-compatible facade, ~100 lines): starts/continues the
  root session for the target task; `task_terminal` when the root declares done.
- Template `blueprints/neko_default_script.yaml`: slots root/dev/qa, `stages: []`.
- **Tests** `test_root_node_mode.py`: flag off = zero live-path change; `run_node`
  stub-dispatches and returns summary+evidence; `steer_node` reuses the session;
  `RootNodeEngine` reports `task_terminal`.
- **Proof:** suite ≥ floor; flag off unchanged.

### N1 — Root skill: author → run → judge → steer → done (SHIP FIRST; kills the live bug class)
- `harness-mission-lead` skill rewritten for the root node: author stages for THIS goal
  (repos derived; validated ⊆ `known_repo_scope_labels()` — the gap-1 guard stays, as a
  tool-level check inside `run_node`, the one mechanical validation that survives);
  `run_node` each stage; judge from summary+evidence; `steer_node` on red; declare done.
- Flag on: `create_mission_goal` starts the root node instead of
  `ensure_default_mission_plan`. Authored stages persist as `MissionPlanStage` rows.
- Placeholder repos and their crutch (`default_blueprint_placeholder_repo_override` + 3
  call sites, `default_plan._specialize_*`) are unused on this path — deleted at N3.
- **Tests** `test_root_authoring.py`: single-repo goal → only that repo's stages;
  unknown repo alias rejected by `run_node`; stages persist for snapshot.
- **Proof (LIVE):** gap-1 trap goal (repo named mid-sentence, backticked test command)
  → done unattended; every stage/workdir/evidence in `hermes-agent`; the dev node's own
  green run is the only test evidence; auto-archive fires. Reproduce the
  task_49f8ee3b/task_8e1e0832 shapes — unrepresentable.

### N2 — QA node
- QA persona: prompt "re-run the full tests yourself; screenshot via the `launcher_qa`
  MCP; end with satisfied or a concrete steer"; toolsets include `launcher_qa`.
- Screenshot trace recorder (sibling of the self-test recorder; keep nonblank/
  fullscreen/redaction validity checks on the captured artifact).
- Root relays QA's steer to the dev node via `steer_node` (dev's same session).
- **Tests** `test_qa_node.py`: QA's own run recorded from its trace; screenshot proof
  from QA's trace; steer lands in the dev session; loop bounded by the stage's
  `max_attempts`.
- **Proof (LIVE):** QA-in-graph goal: two independent test runs (dev + qa sessions), a
  valid screenshot, one steer round-trip, done unattended.

### N3 — Burn-in, flip, delete the tower
- 10 green unattended runs (single-repo, cross-stack via authored `depends_on` stages,
  QA-in-graph, chaos drill `daemon stop` mid-turn) → default `root_node_mode: True`.
- Then delete (doc-03 grep-gated): `ticker.py` gate/recovery/dispatch body,
  `planning.py` choreography, `packets.py`, `decision_contract_registry.py`,
  `simplified_contract.py`, `default_plan.py`, `final_gate.py`, `budget_approval.py`,
  incident routing (keep `classify_exception`), `role_envelopes.py`,
  `persona_assignments.py` (`WorkerSessionStore` is the keeper).
- **Proof (LIVE):** the 10-run sweep, zero incidents/adjudication/approval rows; final
  `harness status --json` clean; record the LOC cut.

## Risks (decided, not open)

- **Root mis-judges a child.** By design the answer is steer-and-retry, then the root
  reports stuck to the operator — prompt/skill iteration, not new Python. The one
  mechanical guard kept is repo-alias validation in `run_node` (wrong-repo work is the
  one mistake evidence can't cheaply reveal after the fact).
- **Fake green.** Evidence is recorded server-side from the tool stream; a child cannot
  write its own evidence rows. The root is prompted to judge from evidence, not from the
  child's prose. If a gap is found, harden the recorder — never add a re-run gate.
- **Session invalidation on steer.** `steer_node` falls back to a fresh session with the
  stage HUD + steer when the provider rejects the old session; accept the cache loss.
- **Runaway spawning.** `run_node` is root-only, depth 1, sequential by default —
  the same caps as `delegate_task`.

## Sequencing

N0 → **N1 first** (one live goal end-to-end through the root node; kills the bug class)
→ N2 → N3. Each stage flag-gated, suite ≥ floor, LIVE-proven before the next.
