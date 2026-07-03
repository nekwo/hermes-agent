# 06 — End-to-end implementation prompt (paste into Claude)

> **Status 2026-07-03:** this prompt was executed. AS0–AS7 are implemented on branch
> `recursive-agent-supervised-execution` and independently verified — see doc-06 §6
> (verification appendix) for the audit, closed gaps, and honest residuals. The prompt
> is retained for reference and for re-runs on future stages.

Keep this in lockstep with `06-recursive-agent-supervised-execution.md` (v3.1). The block
below is the complete handoff for implementing AS0–AS7 in one engagement.

---

You are implementing the Recursive Agent-Supervised Execution model in the Hermes Agent
Runtime Harness, end to end.

REPO & GROUND TRUTH
- Repo: `X:\Eternia\hermes-agent` (Windows; run commands with this as workdir).
- Check your brain first: read the repo's `AGENTS.md` / `CLAUDE.md` and
  `docs/agent-runtime-harness/00-index.md` before any code.
- The single source of truth for this work is
  `docs/agent-runtime-harness/06-recursive-agent-supervised-execution.md` (v3, "gaps
  closed"). Read it fully before writing code. Its §0 audit ledger gives you exact
  file:line anchors; §2.5 (D1–D7) has already decided node identity, the parent wake
  mechanism, the flag map, event-bloat policy, the lane concurrency model, and every
  default — do NOT re-litigate those decisions, implement them. Companions if you need
  deeper context: docs 01, 02, 04 (S2/S3 + H1–H10), 05 (RD0–RD3 shipped).
- The harness runtime root is `X:\Eternia\.hermes\agent-runtime`; the editable venv
  `X:\Eternia\.hermes\venvs\hermes-agent` serves the checkout, so harness code edits are
  live on the next CLI call.

NON-NEGOTIABLES
- Work on a branch and commit after EVERY completed stage immediately — the hermes-agent
  main worktree can be reset by live harness automation; uncommitted work is lost work.
- The test suite floor is 1262 (`python -m pytest tests/agent_runtime --collect-only -q`).
  After every stage: `python -m pytest tests/agent_runtime -q` green, count ≥ 1262 + your
  new tests. Never delete or weaken an existing test to get green.
- Never claim something works without running it — paste exact command output. If a stage
  is blocked, say exactly what and why; do not paper over it.
- New event types MUST be registered in `agent_runtime/decision_contract_registry.py`
  (unregistered types throw at `EventLog.append`); payloads ≤ 4096 bytes.
- New config keys MUST follow the 3-file pattern: `runtime_config.py` dataclass default →
  `config.py` parse → `migrations.py` validation.
- All summaries/reasons that reach events/incidents go through the existing redaction
  sanitizers (`safe_assignment_text` / `_safe_text` style). Never write raw paths/secrets.
- Every stage ships behind its flag from doc-06 §2.5 D3; flags off must reproduce today's
  behavior exactly (the serial tick loop is the rollback).
- Do not build parallel taxonomies: AS0 extends `no_freeze_monitor.py` +
  `NoFreezeThresholds`; AS1 extends `return_summary_to_parent_session` and the
  `run.progress` contract; AS2 reuses `steering.py` fan-out caps; AS3 reuses the
  `ticker.py` authoritative gate; AS4 reuses the `SwarmConfig` tiers and the
  `swarm_budget_exceeded` contract.

STAGE ORDER — implement strictly in this order, one commit per stage minimum
1. AS0 — Active liveness watchdog (doc-06 §AS0 + D4/D6/D7). `agent_runtime/liveness.py`
   `LivenessProbe` on a daemon watchdog THREAD (sibling of `_heartbeat_loop`,
   `daemon.py:95` — the engine is synchronous; between-actions hooks cannot catch mid-turn
   hangs). Classify advancing/quiet/hung off `run.last_heartbeat_at` +
   `EventLog.iter_from_offset`; zero model calls, zero snapshot builds. Reuse
   `no_freeze_monitor` finding/incident shapes; add `run_hung`; remediate per D7 (cancel
   run, close worker session, incident linked into `task.open_incident_ids`; the probe
   never re-dispatches). Route worker in-session tool commands through
   `_run_bounded_process` semantics (`proof_runner.py:880`) → `command_hung` on breach.
   Tests: `tests/agent_runtime/test_liveness.py` per doc spec.
   Live proof: artificially stall a worker in a real goal; caught ≤300s (not 900s),
   incident routes to Neko, goal recovers.
2. AS1 — Child status events (doc-06 §AS1 + D2/D4). `child.progress/blocked/returned`
   with `parent_node_id`; `returned` extends `return_summary_to_parent_session`'s
   existing `steer.returned` emission (one write path); `child_events_offset` on persona
   instances; unconsumed returned/blocked past the offset is a new MissionStateMachine
   dispatch reason (the parent wake). A healthy child costs the parent 0 model turns —
   test that explicitly.
3. AS2 + AS3 together, one flag (`supervision.recursive_enabled`) — recursive supervision
   scoped to `spawned_by`-direct children with O(direct-children) parent context
   (distilled summaries only), fan-out/depth caps from `steering.py:177` enforced
   harness-side and logged on hit, upward block aggregation one level at a time; AND the
   authoritative gate (`_build_authoritative_stage_gate_decision` +
   `_collect_command_proof` + `_handoff_diff_weakens_tests`) re-run at EVERY handoff
   boundary on the child's own worktree diff. Never ship AS2 behavior without AS3 gating.
   Tests 2+ levels deep, including a lying/tampering child rejected by its parent's gate.
4. AS4 — Hierarchical budget (`supervision.hierarchical_budget_enabled`): parent
   sub-allocates `swarm.global_token_hard_limit` per child; child exhaustion blocks
   upward, never overspends the global pool; emit `swarm_budget_exceeded` per its
   registered contract.
5. AS5 — Concurrent lanes (doc-06 §AS5 + D5). Preconditions are verified-met; ship the
   lock deltas FIRST: `locks.py:_file_lock` bounded blocking retry
   (`lock_acquire_timeout_seconds=15`; Windows is fail-fast today),
   `repo_land_lock(source_root)` for handoff land-back, per-incident lock around
   `IncidentStore.close`. Then `next_actions(mission) -> list[HarnessAction]` ready-set
   (all `depends_on` satisfied) capped by `max_active_lanes`; `run_until_settled` +
   `daemon.py:121` run lane THREADS inside the one `tick_lock` process. Prove with a
   two-independent-stage blueprint: overlapping run windows in the event log, dependent
   stages still serial, a two-thread store hammer with zero lock violations on Windows
   semantics.
6. AS6 — Deploy verification (`supervision.deploy_verification_enabled`,
   `deploy_timeout_seconds=120`): every spawn verified up; `child.deploy_failed{reason}`
   → parent retry/escalate; the `agent_already_assigned` class
   (`persona_assignments.py:841`) surfaces, never silently swallowed; a never-started
   child trips the AS0 probe.
7. AS7 — Certification: liveness/perf CI (hang caught ≤ `liveness_hung_seconds`;
   healthy-child parent cost = 0 turns), concurrency soak, crash-recovery drill (kill
   mid-tree, rebuild from event log + watermark, no double-apply), redaction-at-rest over
   child summaries. Wire into `burn_in.py`; only after 10 green runs add the
   `production_envelope.py` entry for `recursive_supervision` with honest gated-off copy.

PER-STAGE CLOSEOUT (report before moving on)
- What shipped (files + one-line design note if you deviated — deviations from doc-06
  require updating doc-06 in the same commit).
- Exact commands run + results (test counts, live-proof output).
- Commit hash.

FINAL REPORT
- Branch + all commit hashes; final suite count; flag states; the AS0 live-proof evidence
  (incident id + timing) and the AS5 overlapping-run-windows evidence (event excerpts);
  every deviation from doc-06; remaining known gaps if any. Do not say "works end to end"
  unless every stage's tests and proofs actually ran green.
