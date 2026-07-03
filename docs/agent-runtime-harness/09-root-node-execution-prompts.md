# 09 — Root-Node Execution Prompts (executor: GPT · verifier: Claude)

Two self-contained prompts. The first is pasted into a GPT session to execute doc-08 v4
end to end in one engagement. The second is pasted into a fresh Claude session afterward
to adversarially verify the execution — it does not trust the executor's report.

---

## PROMPT 1 — EXECUTOR (paste into GPT)

You are implementing the Root Node + Self-Looped Sub-Agents architecture in the Hermes
Agent Runtime Harness, end to end, in this one engagement.

REPO & GROUND TRUTH
- Repo: `X:\Eternia\hermes-agent` (Windows). Runtime root: `X:\Eternia\.hermes\agent-runtime`.
  The editable venv serves the checkout — harness edits are live on the next CLI call,
  but a RUNNING daemon must be restarted to pick them up.
- Check your brain first: read `AGENTS.md`, `docs/agent-runtime-harness/00-index.md`,
  then the ONLY spec you implement:
  `docs/agent-runtime-harness/08-blueprint-as-script-collapse.md` (v4). Doc-08 §"Verified
  seams" gives you machine-checked file:line anchors — start from them, do not re-derive.
- The main worktree can be reset by live harness automation at any time:
  **commit + push to main after EVERY completed stage. Uncommitted work is lost work.**

THE MODEL (do not drift from it)
- Every node is a standard self-looped Hermes agent: `{prompt, HUD, toolset, workdir}`.
  That is the entire customization surface.
- The ROOT node (Neko) runs the goal: its skill authors the stages for THIS goal, runs
  each stage as a sub-agent node via `run_node`, judges the result from the child's
  summary + harness-captured evidence, steers via `steer_node` (next turn, child's SAME
  session), declares done. Same shape as Hermes' `tools/delegate_tool.py`.
- The harness is substrate ONLY: run loops, resolve workdirs, capture evidence
  server-side, hang-kill, plain budget caps, persist for Mission Control.
- **NO JUDGMENT IN PYTHON.** No completion contracts, no failure ladders, no incident
  routing, no re-run gates, no decision JSON on this path. Exactly ONE mechanical check
  survives: repo-alias validation inside `run_node`
  (`known_repo_scope_labels`/`canonical_repo_scope_label` in `repo_context.py`).
  If you find yourself writing an if-branch that decides whether a child's work is good,
  STOP — that decision belongs to the root's model; your job is to put the evidence in
  front of it.

NON-NEGOTIABLES
- Before starting: `python -m pytest tests/agent_runtime --collect-only -q` and record
  the count (floor: expected ≥ 1329). After every stage the FULL suite must be green at
  ≥ floor + your new tests. Never weaken or delete an existing test to get green; if one
  asserts behavior your stage deliberately retires, update it with a rationale comment
  and say so in the commit message.
- Everything ships behind `root_node_mode: bool = False`
  (runtime_config.py → config.py → migrations.py, the 3-file pattern). Flag off = zero
  live-path change, proven by the suite.
- Never claim something works without running it — paste exact command output in your
  report. Live proofs are LIVE: real `harness task create --start-daemon` goals against
  the real runtime root, unattended (any manual tick / incident close / unblock
  disqualifies the run — say so if it happens).
- Redaction-safe text everywhere; no absolute paths or secrets in persisted
  events/proofs/summaries.
- New service tools are root-only via the existing `check_fn` gating pattern — they must
  NOT appear in any other persona's toolset (AGENTS.md footprint ladder).
- Preserve Mission Control compatibility: authored stages persist as `MissionPlanStage`
  rows, child runs as `AgentRun` rows, evidence as `Proof` rows; `snapshot.py` renders
  without modification.

EXECUTE, IN ORDER (doc-08 v4 stages; per-stage detail lives in the doc — follow it)

1. **N0 — flag + `run_node`/`steer_node` + `RootNodeEngine` (dark).**
   `agent_runtime/node_tools.py` + the daemon-compatible `RootNodeEngine`
   (`MissionDaemon(engine_factory=...)` seam, `stop_reason="task_terminal"` when the
   root declares done) + template `blueprints/neko_default_script.yaml` (slots
   root/dev/qa, `stages: []` — relax `validate_blueprint` to allow it).
   Tests per doc-08 N0. Commit + push.
2. **N1 — root skill: author → run → judge → steer → done.**
   Rewrite `harness-mission-lead` for the root node (it reinstalls on persona
   bootstrap). Flag on: `create_mission_goal` starts the root node instead of
   `ensure_default_mission_plan`. Repo-alias validation inside `run_node`. Tests per
   doc-08 N1. **LIVE PROOF:** the gap-1 trap goal (repo named only mid-sentence +
   backticked focused test command) reaches `done` unattended; every stage/workdir/
   evidence row in the named repo; dev node's own green run is the only test evidence;
   targeted daemon auto-archives. Paste task id, timings, evidence ids, archive batch
   path. Commit + push.
3. **N2 — QA node.**
   QA persona with `launcher_qa` MCP in ITS toolset; screenshot trace recorder (sibling
   of `record_self_test_from_progress`, keep nonblank/fullscreen/redaction validity
   checks); root relays QA's steer to the dev's same session. Tests per doc-08 N2.
   **LIVE PROOF:** QA-in-graph goal with two independent test runs (dev + qa sessions),
   a valid screenshot proof from QA's own trace, one steer round-trip landing in the
   dev's session, done unattended. Commit + push.
4. **N3 — burn-in only.** Run the 10-goal unattended matrix (single-repo, cross-stack
   via authored `depends_on`, QA-in-graph, chaos drill `daemon stop` mid-turn →
   restart). Record every run's task id, duration, and outcome.
   **Do NOT flip the default flag and do NOT delete the legacy tower in this
   engagement** — those are gated on independent verification (Prompt 2). If any burn-in
   run needs manual recovery, record exactly what and why; do not retry-and-hide.

CLOSEOUT (blunt, evidence-based — this exact shape)
- Per stage: commit hash → files touched → test names added → suite count.
- Live proofs: task ids, create-to-done timings, evidence/proof ids, archive batch
  paths, final `harness status --json`.
- Burn-in ledger: 10 rows (goal shape, task id, duration, unattended yes/no, notes).
- Anything NOT done or requiring manual recovery, with the exact reason and the next
  concrete step. An honest gap beats a padded claim — the verifier will find it anyway.

---

## PROMPT 2 — VERIFIER (paste into a fresh Claude session)

You are adversarially verifying a GPT engagement that claims to have implemented
doc-08 v4 (Root Node + Self-Looped Sub-Agents) in `X:\Eternia\hermes-agent`. Do not
trust the executor's report — verify everything independently and try to break it.

GROUND TRUTH
- Check your brain first: `AGENTS.md`, `docs/agent-runtime-harness/00-index.md`,
  `docs/agent-runtime-harness/08-blueprint-as-script-collapse.md` (v4 — the spec),
  `docs/agent-runtime-harness/09-root-node-execution-prompts.md` (this engagement).
- Runtime root `X:\Eternia\.hermes\agent-runtime`; editable venv serves the checkout;
  restart any running daemon before live checks so it runs current code.
- The executor's closeout report is INPUT, not evidence. Every claim gets re-derived.

VERIFY, IN ORDER — paste exact command output for each

1. **Commits are real and scoped.** `git log --oneline` since the pre-engagement base;
   per stage confirm the named files/symbols exist (`run_node`, `steer_node`,
   `RootNodeEngine`, `root_node_mode` in all 3 config files, the QA trace recorder, the
   rewritten `harness-mission-lead`).
2. **Suite honestly green.** `python -m pytest tests/agent_runtime -q` yourself. Count
   ≥ 1329 + the executor's claimed new tests. Then `git diff <base> -- tests/` and audit
   every MODIFIED (not added) test: any weakened/deleted assertion without a written
   rationale is a finding.
3. **Flag off = zero change.** With `root_node_mode` off (default), run one legacy live
   goal end to end; confirm behavior and stores match pre-engagement shape.
4. **No judgment in Python (the spirit check).** Read `node_tools.py` +
   `RootNodeEngine` + the root skill diff. Findings if you see: any Python branch that
   decides whether a child's work is acceptable (beyond the single repo-alias check),
   any completion contract, retry ladder, incident open, decision-JSON parse on the new
   path, or `run_node`/`steer_node` exposed to a non-root persona.
5. **Your own live N1 goal.** Do not reuse the executor's. Create a fresh gap-1-trap
   goal (`--start-daemon`, flag on), watch it unattended: correct repo everywhere,
   dev node's own green run as the only test evidence, targeted daemon, auto-archive,
   clean final `harness status --json`. Inspect the proof rows yourself (source =
   `agent_tool_trace`, command matches the goal's named test).
6. **Your own live N2 goal.** QA-in-graph: verify from the STORES (not the report) that
   the QA test run and screenshot came from QA's OWN session trace, the screenshot
   passes the validity checks, and the steer turn shares the dev's `session_id`.
7. **Chaos drill.** `daemon stop` mid-turn → orphan run cancelled immediately (stop
   result lists it) → restart → the goal still reaches done unattended.
8. **Burn-in ledger audit.** For each of the executor's 10 claimed runs: confirm the
   archive batch exists and the event history shows no manual tick/unblock/incident
   close. Re-run any row that looks doctored.
9. **Mission Control compatibility.** `hermes harness snapshot --json` renders the
   root-node goals (stages/runs/proofs visible; `.parity` warnings clean).
10. **Substrate intact.** Liveness watchdog, daemon lease/targeting/auto-archive,
    orphan reap, redaction: spot-check each still works on the new path.

VERDICT (blunt)
- Per stage: VERIFIED / PARTIAL / FAILED with the exact evidence line for each.
- List every finding (spirit violations from step 4 included) ranked by severity.
- State explicitly whether the system is ready for the N3 flag-flip + tower deletion,
  or exactly what must change first. Only if 10/10 burn-in rows verify unattended AND
  steps 1–10 pass do you approve the flip; the deletion itself remains a separate,
  human-approved engagement.
- Update doc-08's header with the verification result and this doc with the outcome,
  in the same commit as any fixes you make.
