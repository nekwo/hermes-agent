# 08 — Blueprint-as-Script Collapse (staged plan)

> Purpose: make the **blueprint the script** and the **harness a dumb interpreter of
> it**. Today the flow is hardcoded across ~13.7k lines of Python special-cases
> (`planning.py`, `ticker.py`, `mission_plan.py`, `default_plan.py`,
> `blueprints/routing.py`) while the blueprint YAML is a decorative 88-line graph.
> The target inverts that: Neko's skill authors the stages for the actual goal,
> agents own their work + proof, steering is the only coordination verb, and the
> harness shrinks to two substrate jobs — kill hangs, confirm a test really ran and
> wasn't faked. Nothing hardcoded.

## Owner's intent (source of truth for this plan)

Stated by the product owner, verbatim in shape:

1. **Neko gets the goal. Its skill creates the stages. It hands the stages to the devs.**
2. **The devs do the work, run the tests, and fix the code until the tests pass.** That
   *is* the dev pass — the point of the dev pass is to make the tests pass.
3. **That's it** — for a dev-only graph, a green dev pass is done.
4. **If QA is in the graph:** QA does *another full test run* to double-check, takes a
   screenshot, then **steers the dev toward improvements**; dev fixes; QA again.
5. **Devs can be steered by QA or Neko.** Steering is the coordination mechanism.
6. **Nothing is hardcoded.** Stages, repos, counts are derived from the goal.
7. **The target is what the blueprint is scripted as.** The blueprint is the executable
   contract for the flow; the harness runs the script and does not reconstruct the flow
   from Python branches.

Everything below is in service of exactly this and nothing more.

## The disease, stated precisely

The blueprint is *not* the script today. `neko_two_dev_default.yaml` is 88 static lines
(three stages — `scope → backend_implementation → implement` — with **hardcoded
placeholder repos** `hermes-agent` / `EterniaBackend` / `EterniaLauncher`). The behaviour
that actually decides what happens lives in Python:

| Module | LOC | What it hardcodes |
|---|---:|---|
| `agent_runtime/ticker.py` | 4287 | post-handoff authoritative gate, proof collection, recovery dispatch |
| `agent_runtime/planning.py` | 2172 | ~30 cross-stack / launcher / backend-first / coerce / block special-cases |
| `agent_runtime/context_builder.py` | 1874 | HUD/next-move recommendation projection |
| `agent_runtime/mission_plan.py` | 1555 | plan mutation, repo derivation, handoff normalization |
| `agent_runtime/decision_contract_registry.py` | 1298 | 23 decision types + payload contracts |
| `agent_runtime/packets.py` | 1147 | typed packet normalize/dedupe/carry |
| `agent_runtime/default_plan.py` | 420 | placeholder-repo specialization of the default graph |
| `agent_runtime/blueprints/routing.py` | 349 | outcome derivation, retry bounds, intervention routing |
| `agent_runtime/simplified_contract.py` | 249 | decision-type projection shims |
| `agent_runtime/final_gate.py` | 203 | harness re-run gate command selection |
| `agent_runtime/budget_approval.py` + `incidents.py` | 184 | budget handshake + incident objects |
| **in scope** | **~13.7k** | |

The 2026-07-03 self-drive bug class (gate ran `flutter analyze` in the wrong repo, scope
flipped to `EterniaBackend`, dev grounded in the wrong worktree, Neko plan-release loop)
exists **only because** stages+repos are pre-baked instead of authored by Neko for the
actual goal. In the target model those bugs are not fixed — they are unrepresentable.

## Target shape (what the scripted default blueprint reads as)

```
goal
  → neko.skill authors stages   (repos DERIVED from the goal; ⊆ known repo aliases; nothing baked)
  → for each stage:
        dev works, runs the real tests, FIXES until green   (dev's own green run IS the proof)
  → if the graph has a qa node:
        qa runs the full tests again (independent double-check) + screenshots + STEERS dev
        loop dev↔qa until qa is satisfied
  → done
```

That block of text is the contract. The harness executes it; it does not re-derive it.

## What the harness keeps (the whole surviving substrate)

Only what an untrusted worker genuinely requires around it:

- **Script interpreter** — walk the authored stages, dispatch the slot's agent, advance on
  the stage's own success signal.
- **Hang-kill** — the AS0 liveness watchdog (doc-06) stays: a genuinely frozen run is
  killed. This is the one recovery mechanism that survives.
- **Anti-fake** — two cheap in-loop checks that a green claim is real: the test actually
  ran (`agent_tool_trace` / `_validate_observed_trace_requirement`) and the diff did not
  gut assertions/skips (`_handoff_diff_weakens_tests`). No independent re-run.
- **One daemon lease** + **redaction on persisted text** + **event log** (unchanged).

Everything else in the table above is on the table for deletion.

## Design decisions (resolved before implementation)

- **D1 — Neko authors stages, the harness does not synthesize them.** The default
  blueprint becomes a *template with an empty stage list* that Neko's skill fills at
  runtime. No `_specialize_default_implementation_stage`, no placeholder repos, no
  `default_blueprint_placeholder_repo_override` (the gap-fix crutch is deleted along with
  the thing it compensated for).
- **D2 — Proof = the agent's own green run.** The harness never re-runs the proof command
  in a workdir the agent did not own. It verifies the agent ran it and did not weaken it.
- **D3 — Steering is a message into the same worker session, not a re-dispatch.** Neko/QA
  steer the *live* dev thread (reuse `mission_chat_steer.py` + the worker-session resume
  path). A failure is a steer or an explicit "stuck → operator," never an incident object.
- **D4 — Cross-stack is not special.** A backend+launcher goal is just a goal whose Neko
  skill authors more stages in more repos. Every `_needs_cross_stack_launcher_completion`
  / `_should_release_backend_first_slice` / `_block_launcher_release_until_backend_proof`
  branch is deleted; the join it enforced becomes an ordinary stage dependency in the
  authored script.
- **D5 — Flag-safe migration.** Everything ships behind `blueprint_script_mode`
  (runtime_config → config → migrations, 3-file pattern). Old path stays runnable until
  BS7 proves the new path on the burn-in ledger; then the old path is deleted, not just
  dark.

## Stages (each independently shippable; flag-gated; do not half-build)

### BS0 — Freeze contract + flag + this doc (no behaviour change)
- Land this doc. Add `blueprint_script_mode: bool = False` (runtime_config.py →
  config.py → migrations.py). Add a `stages: []`-allowed template blueprint
  `neko_default_script` (empty stage list, `on_unhandled: intervention`), unused while the
  flag is off.
- **Proof:** suite green at floor; new config key round-trips; template blueprint validates
  with an empty stage list.
- **Invariant preserved:** default path unchanged (flag off).

### BS1 — Neko authors stages dynamically (kills the live bug class)
- Neko's `harness-mission-lead` skill emits the stage list for the goal: each stage carries
  `owner_slot`, `repo` (derived; validated ⊆ `known_repo_scope_labels()` — reuse the gap-1
  guard), `objective`, `test_plan`/`proof` intent. The harness instantiates the template
  with those stages instead of the baked graph.
- **Delete:** `default_plan._specialize_default_implementation_stage`, the placeholder
  repos in `neko_two_dev_default.yaml`, and `final_gate.default_blueprint_placeholder_repo_override`
  + its three call sites (planning release, gate command, dev grounding) — the entire
  2026-07-03 crutch.
- **Proof (LIVE):** a single-repo hermes-agent goal authors only hermes-agent stages;
  a cross-stack goal authors backend+launcher stages; no placeholder repo ever appears in
  `affected_repos`, gate command, or grounding workdir. Reproduce task_49f8ee3b /
  task_8e1e0832 shapes and show they can't recur.
- **Invariant:** repos still fail-closed to a known alias; unknown alias → repair to Neko.

### BS2 — Dev pass owns its proof (collapse the second lane)
- The dev's own green in-session test run completes the stage. `hand_off` requires an
  observed passing `agent_tool_trace` for the stage's proof command; the harness verifies
  it ran + diff-not-weakened and advances. No harness re-run.
- **Delete:** `_build_authoritative_stage_gate_decision`, the `_should_auto_run_final_gate`
  path, `final_gate.py` recipe/default fallback, the gate branch of `_collect_command_proof`,
  the gate proof-batch plumbing, `proof_command_policy` narrowing that only existed for the
  re-run.
- **Proof:** a stage with a passing observed run reaches done with zero harness-owned
  re-run proof; a faked green (no trace) or weakened diff is still rejected in-turn.
- **Invariant:** anti-fake checks stay; a genuinely red stage cannot reach done.

### BS3 — QA pass = full retest + screenshot + steer
- QA is a scripted node that (a) runs the full test suite itself (its own real run — the
  double-check, not a replay of dev's exact command), (b) captures the Stage-C screenshot,
  (c) steers dev with concrete improvements. Dev↔QA loop until QA is satisfied, bounded by
  the stage's `max_attempts`.
- **Change:** `qa_verdict` becomes "satisfied / steer" over QA's own run + shot, not a
  gate over dev-supplied proof IDs. QA steer uses D3's steering channel.
- **Proof (LIVE):** a graph with a QA node runs tests twice (dev + QA), attaches a
  non-blank fullscreen screenshot, and a QA steer reaches the *same* dev session.
- **Invariant:** QA can only run capabilities the backend supports (no fake buttons; visual
  proof rules from the launcher guardrails hold).

### BS4 — Steering replaces incidents / adjudication / budget handshake
- Failure handling is steering, not objects. A stuck stage emits a steer (Neko or QA) into
  the live worker; if unrecoverable, one explicit "stuck → operator." 
- **Delete:** `incidents.py` as a routing driver, the Neko adjudication turn + its
  one-pass fingerprint guard (`recovery_flags.py`), `budget_approval.py` + the
  `WAITING_ON_APPROVAL`/`approve_continuation` handshake, `_coerce_neko_*` coercions in
  `planning.py`, the settled-boundary incident carve-outs in `ticker.py`.
- **Keep:** the hang-kill watchdog (the only survivor of the old recovery lane) and the
  budget as a *soft* per-stage ceiling that steers ("you're over budget, wrap up or say
  you're stuck") instead of opening an approval lane.
- **Proof (LIVE):** a stage that fails its test gets steered and recovers in-session with
  no incident row, no adjudication run, no approval prompt; a truly stuck stage surfaces to
  the operator once.

### BS5 — Collapse the decision contract + packet protocol
- With scripted flow + steering, the agent verbs collapse to: **author-stages** (Neko),
  **work/hand-off** (dev), **steer** (Neko/QA), **satisfied** (QA), **stuck** (any). The
  23-type `DecisionType` enum and the packet normalize/dedupe/carry apparatus are no longer
  load-bearing.
- **Delete/shrink:** `packets.py` (1147) to near-zero, `decision_contract_registry.py`
  payload-contract machinery to the surviving verbs, `simplified_contract.py` projection
  shims, the HUD next-move projection in `context_builder.py` that only fed the old menu.
- **Proof:** every surviving flow expressible with the collapsed verb set; no decision path
  requires a deleted type; parity envelope still renders.
- **Invariant:** redaction-safe projection of the transcript is preserved.

### BS6 — Delete cross-stack choreography + collapse bookkeeping
- The ~30 cross-stack/launcher/backend-first special-cases in `planning.py` are deleted;
  the joins they enforced are ordinary `depends_on` edges in Neko's authored script.
  Collapse `WorkerSessionStore` + role-envelope + `persona_assignment` triple bookkeeping
  to one "who owns this stage now" record.
- **Proof:** a cross-stack goal runs purely from authored stages+edges with no
  cross-stack-named code path executed (grep-gate: those functions are gone).
- **Invariant:** a backend→launcher dependency still blocks the launcher stage until the
  backend stage is green — but as a scripted edge, not a Python gate.

### BS7 — Reduce harness to substrate + delete the old path
- What remains: script interpreter + hang-kill + anti-fake + daemon lease + redaction +
  event log. Flip `blueprint_script_mode` default on after 10 green unattended runs on the
  burn-in ledger; delete the dark old path (no permanent dual-orchestrator fork — see
  doc-03 for the retirement-gate discipline).
- **Proof:** LOC in the in-scope modules cut to a small fraction of 13.7k; 10 consecutive
  unattended goals (single-repo, cross-stack, QA-in-graph, chaos-drill) reach done with
  zero incidents/adjudication/approval; final `harness status --json` clean.

## Non-goals & risks

- **Non-goal:** changing the launcher/Stage-C visual-proof rules, the daemon lease model,
  or redaction. Those survive intact.
- **Risk — self-certification.** Deleting the harness re-run leans on anti-fake checks. If
  an agent finds a way to emit an `agent_tool_trace` without a real run, that's the hole to
  close (harden the trace, not restore the re-run). Called out so it isn't rediscovered as
  a surprise.
- **Risk — Neko authoring quality.** The whole model rests on Neko's skill authoring good
  stages. BS1 must ship with the repo-scope guard (gap-1) and a stage-shape validator so a
  malformed authored plan is repaired, not run.
- **Risk — big deletions.** Ship strictly staged behind the flag; never delete a path until
  its replacement is live-proven (BS7 gates the deletions).

## Sequencing

BS0 (doc+flag) → **BS1 first and alone** (it kills the live bug class and is the smallest
real win) → BS2 (dev owns proof) → BS3 (QA) → BS4 (steering replaces the recovery tower) →
BS5/BS6 (contract + choreography collapse, can overlap) → BS7 (substrate + delete old
path). Each stage is independently shippable and live-proven before the next.
