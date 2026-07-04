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

---

## VERIFICATION OUTCOME (2026-07-04, adversarial re-derivation by Claude)

Base = `49cdcf4ad` (this doc). Engagement commits: `369fe4f34` (N0), `67acdc300`
(N1), `a56fc0081` (N2), `a91693b2a` (N3 ledger). Every claim below was
independently re-derived; the executor's report was treated as input only.

### Per-step verdicts

1. **Commits real & scoped — VERIFIED.** `run_node`/`steer_node`
   (`node_tools.py`), `RootNodeEngine` (`root_node_engine.py`), `root_node_mode`
   in all 3 config files (`runtime_config.py`/`config.py`/`migrations.py`), QA
   trace recorder (`visual_trace_evidence.py`), rewritten `harness-mission-lead`
   SKILL all present. Daemon seam flag-gated at `hermes_cli/harness.py:5886`.
2. **Suite honestly green — VERIFIED.** `1343 passed in 300.36s` (floor 1329 + 14
   new: `test_root_node_mode.py`×5, `test_root_authoring.py`×3, `test_qa_node.py`×6).
   One modified test (`test_decision_contract_registry.py`) dropped
   `harness-mission-lead` from the decision-contract example set — defensible (the
   root-node skill emits no AgentDecision JSON) but shipped with **no rationale
   comment and a generic commit message**. Rationale comment added in this commit.
3. **Flag off = zero change — VERIFIED WITH EXCEPTION.** Legacy decision tower
   (`ticker/planning/packets/final_gate/default_plan/decision_contract_registry/`
   `budget_approval/simplified_contract`) is byte-for-byte untouched; goal-create,
   daemon engine selection, and node-tool permission all gate on the flag. **But**
   three additive changes are **not** flag-gated and alter the shared path:
   `progress.py` screenshot-recorder hook, `personas.py` QA `launcher_qa` toolset +
   `required_mcp_servers`, and `mission_plan.py` snapshot stage fields. Not "zero
   change" in the strict sense. (Did not run a separate full legacy live goal;
   basis = clean flag-gating + green suite + untouched tower.)
4. **No judgment in Python — VERIFIED (spirit upheld), one substrate gap.** No
   completion contract, retry ladder, incident open, or decision-JSON parse on the
   new path; the sole mechanical check is repo-alias validation
   (`_canonical_repo_or_raise`). Tools are root-only by toolset assignment (no
   dev/qa persona carries `node_control`). **Gap:** `run_node` returns
   `diff_weakens_tests` hardcoded `False` (`node_tools.py:439`) — the weaken-check
   the doc mandates (extract from `ticker.py:2159`) was never wired, so the root
   cannot see a child weakening tests. `check_fn` gates on the flag, not the
   persona (defense-in-depth only; effective gate is toolset membership).
5. **Own live N1 goal — VERIFIED.** `task_e13dc2f8`, gap-1 trap (repo named only
   mid-sentence + backticked focused command), flag on, `--start-daemon`,
   unattended. Root (`neko_supervisor`) authored one stage `n1_verify_smoke`
   (repo=`hermes-agent`), ran dev child, judged, declared done. Stores:
   `affected_repos=['hermes-agent']`; self-test `source=worker_tool status=passed`
   command = the exact named test from the **dev** run; proof
   `proof_observed_9c1a0fc0bb` `type=test_run created_by=dev source=agent_tool_trace
   status=passed`; targeted daemon auto-archived
   `20260704T000730975440Z_archive_ready`; final `harness status` clean. Note:
   stage row stayed `status=ready` (no Python stage-completion on this path — by
   design, minor Mission Control cosmetic).
6. **N2 QA-in-graph — VERIFIED FROM STORES (not independently re-run live).**
   Executor row 09 (`task_fc937bb8`): dev ran **twice in one session**
   (`20260703_233114_ac2f0e` — steer landed in the dev's own session), QA ran in
   its own session (`20260703_233211_7467f7`), screenshot proof `created_by=qa`
   `run_id=`the QA run, `status=passed nonblank/fullscreen redaction=safe 2560×1400`.
   **Caveat:** the screenshot came via `launcher_qa_terminal_wrapper_trace`, whose
   recorder force-marks redaction `safe` and mtime-selects the PNG from a directory
   — real QA-run provenance, weaker artifact binding than the direct-MCP path.
7. **Chaos drill — VERIFIED FROM STORES (not independently re-run live).** Row 10
   (`task_d75547fd`): `daemon stop` mid-turn cancelled `run_787754bdd093` and
   `run_eba710c2ac01` (`state=cancelled err=operator_cancelled`); targeted restart
   completed root+dev runs → `state=done`.
8. **Burn-in ledger — PARTIAL (self-admitted, not doctored).** All 5 spot-checked
   archive batches exist. The executor's own ledger states it is **not** 10/10
   unattended: row 08 (`37070s`, manual restart) and row 10 (chaos) are
   not-unattended; rows 05–07 produced zero Proof rows. No manual
   tick/unblock/incident-close claimed on the unattended rows.
9. **Mission Control compatibility — VERIFIED.** `snapshot --json` builds in
   ~2.3s; no root-node parity warnings (`.parity.warnings` are pre-existing
   `operator_channel` chat-trace items unrelated to this work). Archived root-node
   task renders full mission_plan/stages/runs/proofs.
10. **Substrate intact — VERIFIED (spot-check).** Targeting + auto-archive proven
    by the live N1 run; orphan reap by row 10's `operator_cancelled` runs;
    redaction by `redaction_status=safe` proofs and secret-marker stripping in
    `_safe_prompt_text`; liveness watchdog thread is daemon-loop-level and
    engine-agnostic (unchanged by the engine swap).

### Findings, ranked

1. **[BLOCKER] Burn-in is not 10/10 unattended.** Rows 08 + 10 not-unattended,
   rows 05–07 no Proof rows (executor honest about this). Flip gate not met.
2. **[MEDIUM] `diff_weakens_tests` is a hardcoded `False` stub** (`node_tools.py:439`).
   The one evidence signal designed to catch "fake green via weakened tests" is
   inert on the root-node path. Extract the helper from `ticker.py:2159` and wire
   it into `_diff_summary` before the flip.
3. **[MEDIUM] Three ungated changes on the flag-off path** (`progress.py`,
   `personas.py`, `mission_plan.py`) violate the strict "flag off = zero change"
   non-negotiable. Additive + test-green, but gate them (at least the screenshot
   hook) or document the intentional shared-path change.
4. **[LOW-MEDIUM] Wrapper screenshot recorder auto-marks redaction `safe` +
   mtime-selects the artifact** (`visual_trace_evidence.py`
   `_terminal_wrapper_screenshot`). Spoofable; harden to bind the artifact to the
   tool call's returned path and actually scan.
5. **[LOW] `check_fn` gates on flag, not persona.** Effective root-only-ness is
   toolset membership; `NodeToolService` does not verify the caller is root.
   Add a caller-identity assertion for defense-in-depth.
6. **[LOW] Modified test shipped without rationale/commit note.** Fixed in this
   commit (rationale comment added).

### Readiness

**The N3 flag-flip and legacy-tower deletion are NOT approved.** The approval gate
("10/10 burn-in unattended AND steps 1–10 pass") is not met: burn-in is 8/10 at
best (finding 1), and findings 2–3 are correctness/spec gaps that must land first.
This aligns with the executor's own conclusion. The **implementation itself
(N0–N2) is sound and live-proven** — the root-node path authors, runs, judges,
steers, and completes a real goal unattended with honest server-side evidence.
Required before a re-verification of the flip: fix findings 2 + 3, then land a
clean 10/10 unattended burn-in sweep (including a QA-in-graph run whose screenshot
uses the direct-MCP path, and the chaos drill counted honestly). The tower
deletion remains a separate, human-approved engagement even after the flip.

---

## REMEDIATION (2026-07-04, same verifier — findings 2–6 closed; burn-in deferred)

Directive: "close all gaps except the burn-in sweep — we still need to guarantee
smoothness before forcing something." Findings 2–6 fixed; the 10/10 burn-in sweep
(finding 1) is intentionally left as the remaining flip gate. Suite after fixes:
**1347 passed, 0 failed.** Re-verified live with a fresh gap-1-trap goal
(`task_41f18ab8`) → `done` unattended, repo `hermes-agent`, dev's own green run the
only evidence (`agent_tool_trace` proofs), auto-archived, final status clean.

- **Finding 2 (weaken-check stub) — CLOSED.** Extracted `diff_weakens_tests` +
  `changed_files_from_diff` as pure helpers in `repo_context.py`; refactored the
  legacy `ticker._handoff_diff_weakens_tests` to delegate to the shared scanner
  (identical behavior, 5 ticker weaken/tamper tests still green); wired the real
  flag into `node_tools._diff_summary`. Also fixed the latent bug that
  `changed_files` was always empty (`git_diff_since_baseline` has no
  `changed_files` key — now derived from the diff text). Tests:
  `test_diff_weakens_tests_and_changed_files_helpers`,
  `test_run_node_diff_evidence_flags_test_weakening`.
- **Finding 3 (ungated shared-path changes) — CLOSED.**
  (a) `progress._maybe_record_visual_screenshot` now returns early unless
  `root_node_mode` is on, so the flag-off path writes no new Proof rows.
  (b) Reverted `personas.py` — QA no longer statically carries `launcher_qa` in its
  toolset/allowlist/`required_mcp_servers`; instead `node_tools._child_enabled_toolsets`
  injects `launcher_qa` for QA child nodes at dispatch (root-node path only,
  symmetric with the root's `node_control`). This also restores the pre-existing
  scope-aware behavior where `launcher_qa` is required only for visual-proof tasks
  (`test_profile_readiness_injects_launcher_qa_only_for_visual_scope`), which the
  executor's static `required_mcp_servers` had overridden to always-required.
  (c) The `mission_plan_summary` stage fields (`acceptance_criteria`, `test_plan`,
  `affected_paths`) are kept — read-only, additive snapshot projection needed for
  root-node stage rendering and harmless for legacy goals; gating a read-model
  schema on a runtime flag would hurt consumers. Tests:
  `test_qa_persona_does_not_statically_carry_launcher_qa`,
  `test_qa_child_gets_launcher_qa_injected_at_dispatch`.
- **Finding 4 (wrapper screenshot spoofing surface) — CLOSED.**
  `visual_trace_evidence._latest_wrapper_artifact` now rejects any candidate PNG
  older than the run's `started_at` (minus a 120s skew tolerance), so a
  stale/foreign artifact left in the output directory can't be mtime-globbed in as
  fake evidence. Redaction-safe is asserted only for an in-window launcher_qa
  artifact.
- **Finding 5 (`check_fn` gates on flag not persona) — CLOSED via lock.** The
  caller persona is not plumbed to tool handlers (`agent.run_conversation` passes
  only `task_id`), so a runtime caller-identity guard would be fragile. Instead
  `test_node_control_tool_is_root_only` locks the real guarantee: no default
  persona carries `node_control`, and only `_root_toolsets` injects it — a child
  node can never invoke `run_node`/`steer_node`.
- **Finding 6 — CLOSED** (rationale comment added earlier).

**Still open: finding 1 (burn-in).** Not attempted here by direction. The flip
remains NOT approved until a clean 10/10 unattended burn-in sweep lands (direct-MCP
QA screenshot, honest chaos drill). Everything else is closed and re-verified.
