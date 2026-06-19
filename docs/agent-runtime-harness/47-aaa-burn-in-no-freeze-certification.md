# Stage 47 AAA Burn-In, No-Freeze Hardening, and Agent Behavior Certification

Date: 2026-06-05
Owner: Codex independent Harness investigation
Status: deterministic Stage 47 gates implemented and local QA green; post-watchdog live-token certification pending

## Purpose

Stage 46 made Mission Control and the Agent Runtime Harness substantially more
proof-aware and self-healing. Stage 47 is the certification stage: run the system
through live operational goals, monitor it like production software, patch the exact
failure patterns that appear, and repeat until Tony can start a goal and walk away.

This stage exists because "the code path works once" is not the AAA bar. The AAA bar is:

- no false done;
- no silent freeze;
- no repeated same-stage loops without changed evidence;
- no unbounded token burn;
- no missing proof for visual/user-facing claims;
- no orphan active task/run/proof state after cleanup;
- and when the Harness cannot proceed, Mission Control explains why in operator-useful
  language with proof IDs, incident IDs, and next action.

The implementation output of Stage 47 is not only more code. It is a burn-in harness,
live proof ledger, failure taxonomy, targeted fixes, and a final certification report.

## Current Ground Truth

The following are already closed and must not be reimplemented in parallel:

- Stage 46 persona self-healing hardening:
  - `4863fc05c feat(harness): harden stage46 persona self-healing`
  - `888c3e18e feat(harness): add Stage 46 self-healing persona runtime`
- Stage C Mission Control visual proof bridge:
  - `e2fb56627 feat(harness): wire stagec mission control visual proof`
- Fullscreen/default proof-window correction:
  - Harness `6adae66e7 fix(harness): relaunch launcher for visual proof captures`
  - Launcher `bb2f66ad fix(mission-control): maximize stagec proof window`
- Archive-ready evidence preservation and Launcher archive bridge:
  - Harness Stage 41/42 docs and implementation
  - Launcher `26f38f9b fix(mission-control): wire archive button to harness CLI`
- Stage 46 skills and persona defaults:
  - `harness-mission-lead`
  - `harness-dev-delivery`
  - `harness-qa-verdict`
- Dynamic `launcher_qa` MCP readiness and screenshot proof:
  - live preflight can report `launcher_qa_mcp=ready`
  - live screenshot proof captured at `2560x1400`

Stage 47 should extend these seams, not fork them.

## Current Implementation Status

Implemented in this Stage 47 slice:

- packet contract robustness for `handoff_packet`, `delivery`, and `qa_review`;
- scoped redaction repair for benign packet vocabulary, while structured secrets,
  path-like values, bearer/assignment shapes, raw logs, and long fields remain fatal;
- cross-run `model_invalid_output` repair context so the next run sees the prior parse
  failure instead of cold-starting blind;
- in-run repair context preservation so the second repair attempt keeps proof,
  incident, repo, HUD, and packet context;
- continuous role-session config, policy, redaction-safe events, observe-only metrics,
  and enabled-mode continuation tests;
- burn-in ledger module and `harness burn-in create/run/status/summarize` CLI;
- no-freeze classifier and proof/incident recorder;
- heartbeat freshness in status/snapshot run summaries;
- archive preservation for incidents and incident detail files, so archived test tasks
  do not leave live open blockers;
- profile-readiness skill lookup fixed for persona-scoped nested skill folders.
- Harness command return codes now propagate at the `python -m hermes_cli.main harness ...`
  process boundary, and missing burn-in ledgers return clean JSON with exit code `2`
  instead of a traceback.
- Harness-owned redaction-safe autonomy packets are generated before each persona model
  invocation, injected into the tick prompt, written under runtime context artifacts,
  and recorded in run progress with budget/receipt IDs.
- Per-agent context artifacts now include `autonomy_packets.jsonl`,
  `absorbed_logs.jsonl`, `compression_receipts.jsonl`, and `context_summary.md` so a
  resumed role has an explicit receipt for what logs/proofs were absorbed without
  copying raw logs into prompts.
- Dev/QA tool-loop guards now consume the autonomy packet read/search and skill-load
  budgets, and no-freeze monitoring classifies missing packets, missing resume receipts,
  and budget overruns without patch/test/proof progress.
- Command proof artifacts now carry proof intent plus redaction-safe environment
  fingerprint/status metadata, while strict fake proof runners remain compatible through
  optional-kwarg detection.
- Live no-op burn-in after the initial Stage 47 commits exposed a repo-routing defect:
  the backend no-op stage command contained a literal `launcher/` dirty-path filter, so
  command-proof routing selected `EterniaLauncher` even though the stage identity was
  backend-owned. The fix is deterministic: stage id/title/scope now outrank incidental
  command text, proof records carry a redaction-safe `workdir_label`, Neko/QA context
  renders that label, and deterministic proof handoff blocks a passed proof that clearly
  came from another known repo.
- Simple live no-op orchestration after proof-routing hardening passed:
  `task_burn_d2dfc2a8`, burn-in ledger
  `20260605T233645Z_noop-orchestration_0cb1e3`, status `passed`, no incidents, no
  active tasks/runs after completion. Actual sequence was
  `neko_supervisor -> backend_dev -> backend_dev -> backend_dev -> neko_supervisor ->
  dev -> neko_supervisor -> qa`; expected sequence was
  `neko_supervisor -> backend_dev -> neko_supervisor -> dev -> qa`.
- That simple burn-in exposed an efficiency gap: the first backend command proof failed
  because `python manage.py check` ran without the backend virtual environment
  (`ModuleNotFoundError: No module named 'django'`). The first failed proof IDs were
  preserved in ProofStore and the burn-in ledger but were not attached to
  `task.proof_ids` early enough for the next Dev prompt/self-heal gate.
- Follow-up hardening now attaches failed command proofs as task evidence without
  marking the stage ready, records failed proof IDs in stage `harness_self_heal`, injects
  exact failed proof IDs into retry autonomy packets, clears retry memory after a passing
  proof, and routes a second same-stage failed proof to Neko self-heal instead of another
  Dev retry.
- Follow-up complex burn-ins exposed that proof-backed backend completion could still
  produce a `needs_context` detour before Launcher release. Neko planning now coerces
  proof-backed backend-ready/missing-Launcher states directly to a Launcher
  `contract_join` handoff, and the ticker reports this deterministic handoff as
  `backend_join_ready` with `next_expected=neko_cross_stack_launcher_release`.
- Launcher contract-join context now includes the prior backend delivery packet,
  joined proof IDs, joined contract packet IDs, relevant recent events, and safe proof
  summaries. Launcher Dev no longer has to ask for packet bodies that were already in
  Harness context.
- Packet contracts now preserve machine-readable handoff/delivery/QA fields including
  `joined_proof_ids`, `joined_contract_packet_ids`, `consumed_proof_ids`,
  `changed_files`, `proof_ids`, and QA reviewed packet/proof lists. QA coordination
  releases default missing `join_gate.release_condition` and proof-gate proof types
  when required proof IDs are present.
- The command proof runner now strips only a leading redacted workdir placeholder
  prefix such as `cd '<path:EterniaLauncher>' && ...` or `Set-Location '<path:...>'; ...`
  before executing in the already resolved repo workdir. This preserves redaction while
  preventing placeholder paths from becoming real failed commands.
- Dev prompt and `harness-dev-delivery` skill now explicitly forbid copying redacted
  path labels into proof commands and tell no-edit/proof-only stages to request the
  deterministic proof immediately.
- Post-complex watchdog hardening now uses one shared tool-budget guard across progress,
  tool-start, and tool-complete callbacks. It counts mixed `search_files`/`read_file`/
  `session_search` inspection against the aggregate autonomy budget, calls the agent's
  native `interrupt()` path when the budget is exceeded, and raises
  `RunBudgetExceeded` after the runner returns even if the underlying tool executor
  swallowed the callback exception. Role-session close/progress payloads now carry
  `watchdog_warnings` when `loop_warning` or critical budget pressure occurred.
- Prompt isolation is now explicit. Harness persona profiles default
  `include_core_context_files=false`, so generic Hermes profile context files do not
  silently override the Harness contract. Harness-controlled repo context excerpts are
  still injected from affected repos, bounded and redacted. Standard Hermes behavior can
  opt back in per persona with `include_core_context_files=true`.
- New-goal hygiene is now first-class. `harness task create` performs a conservative
  hygiene pass that marks stale runs and cancels orphan active runs while preserving
  evidence. Stage-47 burn-in creation additionally cancels previous Harness-owned
  `stage47_burn_in` temp tasks/runs so a rerun starts from a clean Harness slate instead
  of inheriting partial test state.
- Mission Control status and snapshots now expose a top-level `dirty_state` block with
  runtime dirtiness, Stage-47 temp state, orphan active runs, open incidents, and known
  repo worktree dirtiness for `EterniaBackend`, `EterniaLauncher`, and `hermes-agent`.
  This is an indicator only: the Harness never auto-reverts product repo changes.
- No-product-edit/routing burn-ins now fail closed at preflight when any affected repo is
  dirty or unresolved. The attached preflight proof preserves dirty labels, dirty counts,
  and bounded `git status --short` excerpts, turning "mysterious failed proof" into a
  clear operator-visible blocker before Dev burns tokens.
- Dev blocking after reusing a failed proof without an environment change now increments
  the same-stage retry counter and routes to Neko self-heal. This closes the loop shape
  where Dev receives the failed proof, says it is still blocked, and then the Harness
  offers another same-stage Dev retry without a changed signal.
- Live no-op burn-in after aggregate-watchdog hardening opened `task_burn_b7e6c60f` and
  exposed this dirty-state class: Backend proof saw a pre-existing backend repo change
  (`M api/tests.py`) from an earlier live agent attempt, and the Harness did not surface
  that clearly at new-goal creation time. The run was stopped to avoid further token
  waste; stdout/stderr artifacts were preserved under
  `X:\Eternia\.hermes\agent-runtime\stage47_live_runs\simple_after_aggregate_watchdog_20260605T222825.*`.
  The finding is now implemented as the new-goal hygiene and dirty-state/preflight gates
  above.

Current live Harness status after final hygiene:

- `open_tasks=0`
- `active_runs=0`
- `open_incidents=0`
- daemon status `offline` after new-goal hygiene cleared stale idle PID `792`
- runtime dirty state `false`
- repo dirty state `repo_dirty=EterniaBackend`, with `M api/tests.py` preserved for
  operator decision; the Harness did not revert or delete it.
- Hermes repo is clean after commit; `.claude/` is ignored as local machine-specific
  agent tooling alongside `.codex/`, `.cursor/`, `.gemini/`, and `.zed/`.
- Neko, Launcher Dev, Backend Dev, and QA profile readiness `ready`

Important boundary: earlier simple and complex live burn-ins exposed the bugs above and
some runs reached `done`, but they happened before the final aggregate-watchdog
interruption hardening. They are valid failure/proof evidence, not final certification
of the current implementation. The simple and complex live-token certification cases
must be rerun after the current local-green implementation. One accidental live burn-in
CLI smoke was started during
command-surface verification; it opened `task_burn_3953e882`, exposed a monitor
classification (`persona_invalid_output_loop`), and was then cancelled, archived, and its
stranded incident closed. Evidence was preserved in archive batch
`20260605T181530791998Z_archive_ready`. That accidental run is recorded as a useful
finding, not as Stage 47 certification.

## Remaining Risk After Stage 46

Severity: P0 until proven by burn-in.

1. The Harness has not yet completed a fresh set of real-token product-edit missions
   after the visual/MCP/fullscreen fixes.
2. Daemon/manual tick behavior is cleaner, but long-running unattended behavior is not
   certified across multiple goal shapes.
3. Neko steering is improved but still needs live evidence that she:
   - scopes once at the beginning;
   - resumes context effectively after compaction or retry;
   - joins backend proof into Launcher release handoff;
   - reports alternatives after completion instead of freezing for preferences.
4. Dev personas now have better skills, but live missions must prove:
   - relevant skill selection only;
   - bounded proof commands;
   - reuse of failed proof IDs;
   - no repeated read/search loops after proof-backed blockers.
5. QA proof behavior must prove:
   - evidence-based verdicts;
   - exact visual proof request only when required;
   - no unnecessary screenshot demand when test proof is enough;
   - visual proof cannot be waived silently.
6. Mission Control visual proof works, but the screenshot revealed a real UI polish gap:
   the agent-map labels can overlap nodes even in fullscreen. This is a Launcher UI
   defect, not a Harness proof-path blocker, and should be logged separately during
   burn-in if it affects operator readability.
7. The last real-token smoke reached `done`, but it was not yet efficient enough to call
   AAA:
   - 29 runs were opened for one test goal;
   - 12 incidents opened and closed;
   - several Neko/Dev/QA runs failed validation before recovery;
   - Backend/Launcher Dev emitted `read_search_without_patch_threshold` warnings;
   - duplicate tool-start events appeared around parallel search/read behavior;
   - QA still caveated that cross-stack behavior was command-level evidence, not a full
     product-edit burn-in.

These are not reasons to throw the architecture away. They are reasons to make local
agent autonomy, tool economy, and recovery quality first-class certification gates.

## Stage 47 End State

Tony can start the Harness daemon or run-until-settled on a meaningful goal and leave it
alone. The system reaches one of two acceptable outcomes:

- `done` with attached proof packet, QA verdict, visual proof where required, and
  archived evidence when requested.
- terminal `blocked` with exact blocker class, incident, proof/log handle, attempted
  self-heal actions, why further self-heal is unsafe or impossible, and the next human
  action.

No other outcome is acceptable. In particular, `running` forever, stale daemon heartbeat,
open incident with no next action, repeated Neko scope loops, repeated Dev proof attempts
with unchanged environment, or QA verdict without proof are Stage 47 failures.

## Non-Goals

- Do not redesign the state machine from scratch.
- Do not create a second proof artifact format.
- Do not add another MCP server abstraction when `VisualCaptureProvider` and
  `StageCLauncherMcpVisualCaptureProvider` already exist.
- Do not add broad model prompt text as a substitute for deterministic Harness gates.
- Do not run expensive broad product test suites unless the task scope justifies them.
- Do not hard-delete Harness task/run/proof artifacts. Test goals must be archived or
  cancelled through Harness commands.

## Implementation Contract

Every Stage 47 substage must produce:

- code/doc change list;
- commands run and exit codes;
- exact task IDs, run IDs, proof IDs, incident IDs, and archive batch IDs;
- before/after Harness status summary;
- failure taxonomy update if anything failed;
- tests added or changed;
- commit hash.

No "works" claim is accepted without live CLI output or proof artifact references.

## Cross-Cutting Agent Autonomy Contract

The Harness should not turn strong models into passive state-machine workers. Each agent
must carry a compact local judgment loop comparable to what a single Claude/Codex harness
does implicitly.

Every live specialist run must emit a redaction-safe autonomy packet before substantial
work:

```json
{
  "agent": "launcher_dev",
  "goal_read": "One-sentence interpretation of the local mission.",
  "role_scope": "What this agent owns and what it refuses to own.",
  "proof_strategy": "Smallest proof path that can honestly verify the claim.",
  "selected_skills": ["skill ids with short reasons"],
  "rejected_skills": ["skill ids skipped with short reasons"],
  "inspection_budget": {
    "read_search_limit": 4,
    "proof_retry_limit": 1
  },
  "self_heal_plan": "What can be fixed locally before escalating.",
  "handoff_shape": "Packet owed to Neko, QA, or the next specialist."
}
```

This packet is not chain-of-thought and must not contain secrets. It is a public operating
summary that lets Mission Control and QA see whether the agent is thinking or merely
wandering.

### Required autonomy behavior

- Agents decide the smallest useful inspection path before opening many files.
- Agents select the default recommended skill plus context-relevant skills only.
- Agents reject irrelevant skills explicitly enough for observability, without loading
  their full bodies.
- Agents choose one bounded proof command before proof execution.
- Agents reuse failed proof IDs after an environment fix.
- Agents stop repeating the same command, read/search pattern, or handoff request when
  the environment fingerprint has not changed.
- Agents report self-heal opportunities for their own prompt, skill, or tool contract
  when a run was inefficient.
- Agents hand off compact packets instead of forcing the next agent to rediscover raw
  logs.

### Runtime gates

Stage 47 should add deterministic checks around this packet:

- fail closed when a specialist run has no autonomy packet before its first proof;
- warn when read/search exceeds the declared budget without patch/proof progress;
- warn when selected skills exceed the recommended plus task-context skills;
- warn when rejected skills are absent for high-scope tasks;
- block repeat proof attempts after unchanged environment fingerprints;
- record autonomy packet IDs in QA verdicts and burn-in ledgers.

## Performance Parity Gaps Versus A Single-Agent Harness

The HUD gives each agent current state, but a performant harness needs more than state
visibility. Stage 47 should close these gaps:

1. **Decision Ledger**

   Record compact agent decisions, rejected paths, proof strategy, and handoff intent.
   Without this, the next run infers intent from raw logs and burns tokens rediscovering
   context.

2. **Context Memory With Compression Receipts**

   Maintain per-agent context files under runtime artifacts:

   ```text
   <runtime_root>/context/<task_id>/<agent_id>/
     absorbed_logs.jsonl
     context_summary.md
     compression_receipts.jsonl
   ```

   Each receipt records source event ranges, token estimate, compression timestamp,
   summary hash, and what was intentionally dropped. This lets resumed agents know what
   they already absorbed.

3. **Tool Economy Governor**

   Track read/search/test/proof deltas against the autonomy packet budget. The governor
   should nudge, warn, or block depending on severity. A read/search loop after a failed
   proof should require a stated new signal.

4. **Proof Intent Before Proof Command**

   Require agents to declare what a proof command will prove and what result changes the
   state. This prevents expensive proof commands from becoming generic sanity checks.

5. **Recovery Diffing**

   Store environment fingerprints and proof failure signatures. Recovery is allowed only
   when something changed: dependency state, code patch, config, task scope, or proof
   command. Otherwise Neko should escalate to `cannot_self_heal`.

6. **Parallelism Policy**

   Duplicate parallel read/search starts should be visible as a performance smell. The
   Harness should either deduplicate identical tool requests or mark the run inefficient
   for QA/burn-in review.

7. **Mode Selection**

   Neko should select the cheapest orchestration mode:

   - single-specialist for simple single-stack goals;
   - specialist plus QA for normal code changes;
   - full multi-agent release flow for cross-stack, visual, release, or high-risk goals.

   This avoids paying multi-agent coordination cost for a task one specialist can finish.

8. **Product-Level Proof Ladder**

   Distinguish command-level proof from product-level proof. QA can approve a small code
   claim with command proof, but Stage 47 cross-stack certification requires at least one
   product-level edit mission with real backend/frontend evidence and, where relevant,
   fullscreen visual proof.

9. **Turn Cadence And Run Packaging**

   The last real-token smoke opened many short runs. Several completed or failed after
   only one to four tool turns even though the configured live iteration budget was much
   higher. Stage 47 must distinguish:

   - daemon cadence too slow;
   - `max_actions_per_tick` too low;
   - run closing too early after a request for proof/steering;
   - validation failures forcing new runs;
   - stage handoff boundaries that split one coherent specialist job into multiple model
     sessions.

   The target is not "longer runs" by itself. The target is fewer wasteful restarts:
   a specialist should be able to inspect, patch, run one proof, interpret it, and emit a
   handoff in one coherent run when wall clock, token budget, and tool budget allow it.

10. **Continuous Role Session Mode**

   The Harness already carries a previous `session_id` into the next run for the same
   task/persona/stage when it is safe. That is necessary but not sufficient. Today, each
   valid `AgentDecision` still closes the Harness run, so the same model session is
   resumed across multiple Harness run records instead of acting like one uninterrupted
   competent specialist.

   Stage 47 should introduce or simulate a continuous role-session loop:

   ```text
   open specialist run
   invoke model
   parse decision
   apply deterministic side effect
   collect proof if requested
   refresh task/context/proofs
   continue same role session when the next action is still the same persona/stage
   close only at role boundary, proof/block terminal boundary, budget boundary, or watchdog boundary
   ```

   Same-session continuation should be preferred when all are true:

   - same task;
   - same persona;
   - same current stage;
   - no open incident requiring Neko/QA/human intervention;
   - no role boundary transition;
   - no unsafe approval requirement;
   - session id remains redaction-safe;
   - watchdog counters do not show looping.

   Close the continuous session when any are true:

   - state transitions to a different owner;
   - QA approval/blocker is emitted;
   - proof command fails and same-run recovery is not allowed by environment diff;
   - model output fails validation twice;
   - wall/token/tool budget hits the elastic cap;
   - duplicate tool/read/search loop exceeds budget;
   - Neko must re-scope or self-heal.

   This gives Tony the behavior of "one uninterrupted competent agent per role" while
   keeping enterprise watchdogs around the loop.

## 47A0R. Contract Robustness and Redaction Remediation (root-cause prerequisite to 47A0)

### Goal

Make recoverable model formatting slips *recoverable* instead of *terminal*. Today a single
unknown packet key, a missing-but-defaultable enum, an over-length `summary`, or legitimate
prose containing a word like "token" raises `DecisionPayloadInvalid` →
`model_invalid_output` → terminal run failure + incident + task `BLOCKED` + reroute. The
continuous role-session loop (47A0) treats invalid output only as a *close condition*; it
does not stop output from being judged invalid in the first place. If 47A0 ships without
this, the same brittle validators run *inside* the loop and the loop merely retries against
them. This is the single highest-leverage token/incident win and must land first.

This does not weaken proof or secret defense, and it does not remove incidents for
genuinely invalid output. It removes *false* invalidations so that a `model_invalid_output`
incident again means something is really wrong.

### Evidence (from `47-claude-run-packaging-continuous-session-audit.md`)

The 29-run smoke `task_737ab93c` burned **1.44M tokens** with **16/29 runs invalid**. Of
its 12 incidents, **9 were `model_invalid_output`**, decomposing as:

- 5 `handoff_packet` shape issues — unsupported keys `stage46_rules` / `final_owner` /
  `failed_proof_reuse`; `proof_gate` missing or invalid `minimum_status`;
- **2 false-positive redaction trips on legitimate packet prose** —
  `packet.launcher_dev_scope.objective contains secret-looking text` and
  `packet.remaining_gaps[0] contains secret-looking text`;
- 1 `qa_review has unsupported keys: ['notes']`;
- 1 Dev failed-proof-reuse gate.

Design inconsistency proven in code: the *same* key `failed_proof_reuse` is fatal in one
path yet silently ignored as metadata in another (`packet.recorded.operator_note`).
`handoff_packet` now degrades gracefully (post-smoke commit `4863fc05c`) but `delivery` and
`qa_review` still hard-fail via `_reject_unknown_packet_keys`, and
`_scan_packet_redaction` still raises on the bare secret-word vocabulary. The smoke ran on
pre-`4863fc05c` code, and that commit was a reactive whack-a-mole patch (enumerate each key
the model invents), not a robustness principle.

### Implementation

Keep each change narrow and tied to one observed failure:

- **Uniform unknown-key tolerance.** Generalize the existing `handoff_packet` tolerance to
  all packet types: replace `_reject_unknown_packet_keys` in `_validate_delivery` /
  `_validate_qa_review` (`packets.py`) with the `_normalize_unknown_handoff_metadata`
  pattern — strip unknown keys, record an `operator_note`, keep the run alive.
- **Severity split (`fatal` vs `repairable`).** Unknown keys, over-length `summary`
  (`decision_schema` 280-char cap), and missing-but-defaultable enums
  (`proof_gate.minimum_status` already auto-defaults at `packets.py:275–276` — extend that
  stance) become **repairable warnings** surfaced to the existing two-attempt in-session
  repair hint in `GPTPersonaRuntime.run_tick`, not run killers.
- **Redaction precision.** In `_scan_packet_redaction`, keep the structured
  `_SECRET_PATTERNS` (assignment / bearer shapes) and absolute-path patterns **fatal**;
  downgrade the bare `_SECRET_WORDS` vocabulary match to **redact-in-place** (mask the span)
  so a packet `objective` can say "auth token refresh contract" without killing the run.
- **Repair continuity across run boundaries.** When a run still closes for
  `model_invalid_output`, carry the exact failing keys/enums into the next run's first
  context so a cold restart does not re-make a *different* slip (the smoke's runs 1–4 each
  failed on a different key).
- **Observability.** Emit a redaction-safe `run.progress` step `contract_repaired`
  recording what was normalized, so QA/burn-in can distinguish "model self-corrected" from
  "harness tolerated," and so a rising tolerance rate is itself a visible smell.

### Tests

House convention (`tests/agent_runtime/`, fake `agent_factory` returning canned
`final_response`):

- `delivery`/`qa_review` packet with an unknown key → run survives, `operator_note` records
  it, no incident (mirror existing `handoff_packet` behavior).
- packet prose `objective: "validate the auth token refresh contract"` → **not** a fatal
  redaction trip; masked in place; run survives.
- real secret shapes (`TOKEN=abc123`, `bearer eyJ…`) and absolute paths → **still fatal**.
- `proof_gate.minimum_status` absent → defaults to `passed`, run survives (lock current
  behavior).
- two-run sequence where run 1 fails on key A → run 2's first `user_message` carries the
  key-A repair hint (extend the existing `"Previous decision parse failed"` assertion in
  `test_persona_runtime_invalid.py`).

### Implementation readiness audit (verified against current code)

Edit map — each row verified against the named symbol in the tree:

| File · symbol | Change | Notes / precedent |
|---|---|---|
| `packets.py` · `_validate_delivery`, `_validate_qa_review` | Replace `_reject_unknown_packet_keys(...)` with a generalized `_normalize_unknown_packet_metadata(packet, allowed, label)` that strips unknown keys, records an `operator_note`, and keeps the run valid | mirrors `_normalize_unknown_handoff_metadata` already used for `handoff_packet`; `record_decision_packets` already strips via `compact_packet_body`, so this only makes *validation* agree with *recording* |
| `packets.py` · `DELIVERY_KEYS`, `QA_REVIEW_KEYS` | Add `"operator_note"` to both frozensets | without this, `compact_packet_body` drops the note before `packet.recorded`, so the tolerance would be invisible to Mission Control/QA |
| `packets.py` · `_scan_packet_redaction` | Split into **hard-raise** (`_SECRET_PATTERNS`, `_ABSOLUTE_PATH_PATTERNS`, secret-bearing path segments, raw-log markers, >4000 chars) and **soft-mask** (bare `_SECRET_WORDS`): reassign the matched span to a fixed token (e.g. `[redacted-term]`) in the parent dict/list instead of raising | in-place packet mutation during validation is already the established pattern — `_normalize_unknown_handoff_metadata` pops keys and sets `operator_note`; the masked body persists into `record_decision_packets` because `iter_packet_payloads` returns the live `decision.payload` dicts |
| `ticker.py` · `_execute_action` (before the attempt loop, ~line 262) | When the prior run for the same task/persona/stage closed `FAILED` with `model_invalid_output`, read its `run.error["message"]` and pass `requires_repair=True, repair_error=<message>` into the first `build_context(...)` at line 285 | `build_context` already accepts these (`context_builder.py:40`) and renders "## Previous decision parse failed" (line 204); today only the in-run 2-attempt loop uses them, so each fresh run starts blind and re-makes a *different* slip |
| `packets.py` → emit | After a normalize/mask, emit `run.progress` with `step="contract_repaired"` listing what was normalized | `run.progress` is already in `EventLog.ALLOWED_EVENT_TYPES`; `append` checks only `evt.type`, not the step → **no allowlist change needed** |

### Decision record

- **D1 — qa_review/delivery unknown-key strictness.**
  `test_qa_review_rejects_notes_metadata_so_skill_shape_stays_strict` and
  `harness-qa-verdict/SKILL.md` ("Do not add `notes`") make the strictness *deliberate*.
  **Accepted implementation:** keep the intent, drop the lethality — strip unsupported
  metadata keys, record `operator_note`, and keep the run alive. QA is still steered to
  structured fields, but one extra key no longer creates a terminal invalid-output
  incident. SKILL.md guidance and `test_harness_qa_skill_documents_exact_packet_keys` stay
  valid because the documented target shape remains exact.
- **D2 — bare secret-word prose.**
  `test_qa_review_allows_stage46_real_token_phrase_without_allowing_secrets` shows the author
  already narrowed redaction (benign "real/live token") but still treats
  prose like `"auth token refresh contract"` as fatal. **Accepted implementation:** mask
  benign bare vocabulary in values, keep structured value/path/log detectors fatal. A packet
  may contain the word "token" as domain prose, but never a token value, assignment, bearer
  string, absolute path, secret-bearing path segment, raw log, or overlong field. Phrases
  that imply an actual exposure such as `"access token was printed"` remain fatal.

### Existing tests this patch must update (same commit)

- `test_decision_contracts.py:269` (qa_review `notes`) -> assert stripped + `operator_note`, no raise (per D1).
- `test_decision_contracts.py:299` (bare-word redaction) -> assert masked value present + no raise (per D2).
- **Must stay green / unchanged:** `:115–144` (minimum_status defaulting), `:160`/`:196`
  (handoff strip + note), `:314` (SKILL.md keys), and every structured-secret / absolute-path
  raise case (`api_key="value"`, `bearer …`, `X:/secret/file.txt`, `"access token was
  printed"` value-leak phrasing if D2 is scoped to mask only no-value vocabulary).

### Blast radius and non-goals

- **In scope:** `packets.py` validation/redaction, `DELIVERY_KEYS`/`QA_REVIEW_KEYS`, one
  `ticker.py` repair-continuity hook, the test updates above, one optional SKILL.md
  clarification.
- **Untouched:** the 280-char top-level `summary` cap (`decision_schema._validate_raw_decision`)
  — no smoke evidence it failed; leave it repairable via the existing hint. The deterministic
  proof-handoff optimization. `ProfileAgentRunner`. The state machine.
- **No new event types and no config-schema change** for 47A0R itself (the `enabled`/flag
  plumbing belongs to 47A0). The net effect is to make *validation* as tolerant as *recording*
  already is — not the reverse.

### Sequencing and related root causes

Land this **before** 47A0's continue path is enabled by default:
`47A0R contract robustness` → `47A0` observe-only metrics → `47A0` continue enabled. The
continuous loop's value is realized only when the model's outputs are not being judged
invalid for recoverable reasons.

Related follow-on (tracked, lower priority): the autonomous settler `run_until_settled`
stops on the first `action_failed` / `incident_opened`, which is why the smoke required a
dozen manual re-launches (`operator-runs/stage46_*`). 47A0R reduces the *trigger rate*; a
later settler-resilience patch should let one bounded recovery pass continue unattended
within `neko_recovery_attempt_cap` before stopping. The "Cross-Cutting Agent Autonomy
Contract" packet mandate must not add new *required* packet fields until 47A0R ships, or it
will increase invalid-output churn.

## 47A0. Continuous Role Session Implementation Plan

### Goal

Make each role-owned work unit behave like a single competent harness agent: the model
keeps working in the same session through inspect, edit, proof, proof interpretation,
and handoff when the same persona still owns the same stage. Watchdogs, not artificial
decision boundaries, should stop the loop.

This is the highest-leverage Stage 47 change because it targets the observed `29 runs for
one goal` failure pattern directly.

### Testing coverage gap

The existing Harness suite has broad component coverage, but the continuous-session gap
proves it is not yet sufficient for enterprise runtime confidence. Passing hundreds of
unit tests did not catch that `session_id` reuse existed while every valid
`AgentDecision` still closed the Harness run.

This is not primarily a raw test-count problem. It is a missing behavioral certification
layer around live-like multi-turn runtime behavior:

- excessive short-run churn;
- too many model sessions per goal;
- same-session stitching versus continuous role ownership;
- specialist ability to inspect, patch, prove, interpret proof, and hand off coherently;
- proof-command interpretation inside the same role-owned work unit;
- run packaging efficiency compared with a single-agent harness;
- Mission Control smoothness and operator readability during real multi-agent flow.

Stage 47 must treat this as a P0 test gap. The code can be well unit-tested while the
runtime behavior remains uncertified.

Minimum new behavioral test shape:

```text
Given fake Dev runtime:
1. Dev emits correct_stage or another valid intermediate same-owner decision.
2. Harness applies the deterministic state update.
3. The refreshed task still routes to the same Dev persona/stage.
4. Dev emits request_test_run.
5. Harness proof runner returns passing proof.
6. Harness closes or hands off according to the state machine.

Expected:
- one role-session envelope;
- one active run or one continuous session record;
- two model invocations in the same role-owned work unit before true handoff;
- no repeated read/search warning;
- no incident;
- proof ID attached if a proof was requested;
- close reason explains whether the boundary was same-owner continuation, deterministic
  proof handoff, QA handoff, or watchdog stop.
```

Minimum burn-in assertions:

```text
No-op orchestration:
- run count <= 8
- incidents == 0
- invalid outputs == 0
- repeated next action <= threshold
- no stale active runs
- proof IDs attached
- QA verdict evidence-backed

Cross-stack product edit:
- run count <= 12
- backend proof and Launcher proof both reviewed
- Neko joins backend proof into Launcher handoff
- QA distinguishes command-level proof from product-level proof
```

### Current lifecycle to preserve

Today the Harness has useful pieces that must stay:

- `RunStore.latest_session_id()` can reuse a safe session for the same
  task/persona/stage.
- `TickEngine._execute_persona_action()` already applies deterministic state-machine
  decisions after model output.
- command and visual proofs are collected by Harness-owned proof runners, not trusted
  from model claims.
- invalid model output opens incidents and prevents unsafe session reuse.
- proof handoff can advance stages without another Dev model tick when the proof is
  already enough.

The implementation should extend this lifecycle, not replace it.

### Deep implementation audit

This feature must be implemented as a lifecycle refinement, not a broad refactor.

Findings from the current code:

- `TickEngine._execute_persona_action()` is the correct integration point because it owns
  persona invocation, decision validation, state-machine application, proof collection,
  and run closure.
- `RunStore.latest_session_id()` already reuses a safe provider session for the same
  task/persona/stage, but the Harness still closes each run after one valid
  `AgentDecision`.
- `ProfileAgentRunner` receives `session_id`, `max_iterations`, wall-clock budget,
  API-call budget, and token budget. Same-session continuation can reuse the provider
  session without changing the profile runner.
- `CommandProofRunner` already emits proof progress and timeout events. Continuous role
  sessions should reuse it instead of creating a second proof path.
- `EventLog.ALLOWED_EVENT_TYPES`, observability event allowlists, config parsing, and
  config validation must all be updated. Otherwise the new events either crash at append
  time or disappear from Mission Control.
- `PROPOSE_PATCH` is not a good first fake-runtime test decision because the current
  planning contract requires proof IDs before using it as an implementation handoff when
  proof storage is active. Live Dev agents usually edit through tools inside the model
  run, then emit `request_test_run`.
- Passing command proof currently triggers deterministic proof handoff in many cases.
  That is already an efficiency optimization and must not be bypassed casually.

Important limitation:

- "Same session" is achievable.
- "Same single model turn forever" is not the right target because Harness-owned proof,
  visual capture, incident creation, and state-machine transitions require the Harness to
  regain control between model decisions.

The correct target is therefore:

```text
same provider session + same Harness run envelope + multiple controlled model decisions
until the state machine reaches a real owner/proof/watchdog boundary
```

### Preferred implementation strategy

Use an incremental, testable sequence:

1. **Observe-only metrics**

   Add role-session envelope bookkeeping but do not change behavior. After each completed
   decision, compute whether the Harness would have continued if continuous mode were
   enabled. Emit `role_session.closed` with `would_continue=true/false` and
   `close_reason`.

2. **Pure continuation policy**

   Add `agent_runtime/role_sessions.py` with a pure
   `should_continue_role_session(...)` function. Pass already-computed values into it
   instead of importing `TickEngine` helpers from the module. This avoids circular imports.

3. **Config-gated same-run loop**

   In `_execute_persona_action()`, wrap the existing "invoke model -> validate -> apply
   decision -> collect proof -> update task" block in a small `while` loop only when
   `continuous_role_sessions.enabled` is true.

4. **Preserve deterministic handoff**

   If proof collection and `_apply_deterministic_proof_handoff()` route the task to Neko,
   QA, another stage, or another persona, close the role session. Do not force Dev to
   interpret proof after the state machine has already made a safe deterministic handoff.

5. **Optional later proof-interpretation mode**

   Only after burn-in proves value, consider a separate flag that delays deterministic
   proof handoff long enough for Dev to emit a compact proof interpretation packet. This
   is not the first implementation because it changes established proof routing behavior.

### New runtime concept: role-session envelope

Add a lightweight internal envelope around one or more `AgentDecision` cycles:

```text
RoleSessionEnvelope
  envelope_id
  task_id
  persona_id
  stage_id
  opened_run_id
  session_id
  decision_count
  proof_count
  tool_turn_count
  api_call_count
  token_count
  continuation_count
  watchdog_warnings
  close_reason
```

This can be implemented first as in-memory bookkeeping in `TickEngine`; it does not need
a new persisted model on day one. Persist summary fields into the final run progress and
`run.closed` event so burn-in can measure it.

### Config

Add explicit config with conservative defaults:

```yaml
continuous_role_sessions:
  enabled: false
  observe_only: true
  max_decisions_per_envelope: 4
  max_proofs_per_envelope: 2
  max_continuations_per_stage: 3
  continue_after_passing_proof: true
  continue_after_failed_proof: false
  close_on_state_owner_change: true
  close_on_open_incident: true
  close_on_invalid_output: true
  close_on_budget_warning: true
```

Rollout should start behind this flag. Stage 47 burn-in enables it only after unit tests
prove deterministic boundaries.

Implementation detail:

- add a small `ContinuousRoleSessionConfig` dataclass rather than an untyped dict;
- parse it from `agent_runtime.continuous_role_sessions`;
- include it in `effective_config_summary`;
- validate positive integer caps in `validate_runtime_config`;
- default to disabled and observe-only so existing live behavior is unchanged.

### Continue decision

After applying an `AgentDecision`, proof collection, and task refresh, evaluate:

```python
should_continue_role_session(
    before_task,
    after_task,
    persona,
    run,
    decision,
    envelope,
    incidents,
    watchdogs,
) -> ContinueDecision
```

Return values:

- `continue_same_run`: same run/session should receive refreshed context and another
  model invocation.
- `close_completed`: role unit is complete; close run as completed.
- `close_handoff`: next owner/persona/stage changed.
- `close_blocked`: proof, incident, or self-heal boundary requires another actor.
- `close_watchdog`: loop/budget/stall risk.
- `close_invalid`: output/session is unsafe.

### Continue when

Continue in the same role session only when all are true:

- config flag is enabled;
- task id is unchanged;
- persona id is unchanged;
- `current_stage_id` is unchanged;
- `state_machine.next_action(refreshed_task)` resolves to the same persona action;
- no open incident exists;
- task is not terminal;
- run is not terminal;
- current `run.session_id` is absent only for fake runtimes or present and redaction-safe
  for live runtimes;
- decision is not `block`, `request_human`, `report_qa_verdict`, `approve`, or
  `complete`;
- if a proof was collected, either it passed or config explicitly allows same-run failed
  proof recovery;
- environment fingerprint did not show an unresolved blocker;
- watchdog counters remain below threshold.

### Close when

Close immediately when any are true:

- next action belongs to a different role;
- state changed to QA, Neko coordination, done, cancelled, or blocked;
- decision output failed validation;
- proof command failed and the task needs environment/self-heal analysis;
- repeated read/search warning persists without patch/proof progress;
- duplicate parallel tool requests exceed policy;
- token/API/tool/wall budget reaches the envelope cap;
- same continuation would exceed `max_continuations_per_stage`;
- context refresh fails or would omit required proof/incident state.
- deterministic proof handoff already routes to Neko, QA, another stage, or another
  specialist.

### Proof handling inside the loop

Passing proof should continue the same specialist session only if the refreshed
state-machine next action is still the same persona/stage. If deterministic proof handoff
already advanced to Neko, QA, another stage, or another specialist, close the role session
with `close_reason=deterministic_proof_handoff`.

Failed proof should default to closing the role session and routing through existing
recovery unless there is a clear same-run self-heal signal:

- the failure is from the edited code, not environment;
- a focused patch is still inside the same stage;
- proof retry count is below one;
- the next proof command is materially different or code changed.

Do not let continuous sessions become unlimited test/fix loops.

### Context refresh

Between continuations, rebuild context from source of truth:

- refreshed task;
- current run progress;
- attached proofs;
- open/closed incident summaries;
- latest decision packets;
- autonomy packet;
- context compression receipt if present.

Do not pass raw accumulated transcript back as the only source of truth. The session may
reuse provider context, but Harness state remains authoritative.

### Metric aggregation

`run.llm` currently represents the latest model invocation. Continuous role sessions need
aggregate metrics without breaking existing run summaries.

Store aggregate envelope metrics under `run.progress.role_session` and in role-session
events:

- `decision_count`
- `model_invocation_count`
- `continuation_count`
- `proof_count`
- `api_calls_total`
- `input_tokens_total`
- `output_tokens_total`
- `total_tokens_total`
- `tool_turns_total`
- `close_reason`

Keep `run.llm` as the latest invocation unless a later migration explicitly changes the
run schema.

### Observability

Emit events:

```text
role_session.opened
role_session.continued
role_session.watchdog_warning
role_session.closed
```

Include:

- `envelope_id`
- `run_id`
- `task_id`
- `persona_id`
- `stage_id`
- `decision_count`
- `continuation_count`
- `close_reason`
- `next_action_before`
- `next_action_after`
- `proof_ids_added`
- `incident_ids_opened`
- `session_id_present` as boolean only
- aggregate token/API/tool counts

Never log raw session IDs in role-session events unless already redaction-safe and needed
for an existing run event.

### Mission Control UX

Mission Control should show this as one active role session, not as a confusing sequence
of micro-runs:

- current role owner;
- current stage;
- current proof command if one is running;
- continuation count;
- last watchdog warning;
- close reason once finished.

This can initially come from snapshot/observability fields; a separate Launcher UI polish
stage can make it prettier.

### Tests

Add unit tests before live burn-in:

- continues after a non-terminal same-persona/same-stage decision;
- closes on owner change;
- closes on QA verdict;
- closes on open incident;
- closes on failed proof by default;
- continues after passing proof when next action remains same persona/stage;
- refuses continuation after invalid model output;
- refuses continuation after max continuation cap;
- emits `role_session.*` events with redaction-safe payloads;
- does not break existing deterministic proof handoff.

Add an integration-style fake runtime test:

```text
Dev decision 1: correct_stage
Dev decision 2: request_test_run
Harness proof: passed
Expected: one role-session envelope, one active run, two model invocations,
then close_handoff if deterministic proof handoff routes away from Dev.
```

Add a second fake runtime test only if delayed proof interpretation is explicitly enabled:

```text
Dev decision 1: request_test_run
Harness proof: passed
Dev decision 2: request_qa_review
Expected: one role-session envelope, proof ID in handoff, close_handoff to QA/Neko.
```

### Rollout sequence

1. Add metrics only: count would-have-continued decisions without changing behavior.
2. Add config flag and unit-tested `should_continue_role_session`.
3. Enable continuous mode in fake runtime tests only.
4. Enable for Dev only in Stage 47 burn-in Case 1.
5. Enable for Launcher Dev.
6. Enable for QA where useful, mostly proof/verdict review.
7. Enable for Neko only for bounded scope/release continuation, not repeated wait loops.
8. If burn-in improves run count without increasing incidents, make it the default for
   live Harness mode.

### Success metrics

For comparable burn-in goals, continuous mode should show:

- fewer total runs per task;
- fewer invalid-output incidents;
- fewer repeated read/search warnings;
- no increase in false success;
- no unmonitored proof hangs;
- no stale active runs after cleanup;
- same or better QA evidence quality;
- Mission Control shows one understandable active role session.

Target for the first no-op/cross-stack burn-in comparison:

- no-op orchestration: under 8 runs;
- simple backend-only edit: under 5 runs;
- simple Launcher-only edit: under 5 runs;
- cross-stack backend plus Launcher: under 12 runs;
- zero repeated same-stage retries without changed signal.

## 47A. Burn-In Runner and Ledger

### Goal

Create a deterministic way to run burn-in goals and capture the same evidence every time.
The operator should not be manually copying status snapshots, proof paths, run IDs, or
archive batch names after each run.

### Implementation

Add a Stage 47 burn-in ledger under Harness runtime artifacts, not source-controlled:

```text
<runtime_root>/burn_in/<timestamp>_<goal_slug>/
  manifest.json
  status_before.json
  task_create.json
  tick_log.jsonl
  monitor_log.jsonl
  status_after.json
  snapshot_after.json
  archive_result.json
  certification_notes.md
```

Add CLI support, preferably:

```text
python -m hermes_cli.main harness burn-in create --suite aaa-stage47 --json
python -m hermes_cli.main harness burn-in run <case_id> --json
python -m hermes_cli.main harness burn-in status <burn_id> --json
python -m hermes_cli.main harness burn-in summarize <burn_id> --json
```

Implemented command group:

- `agent_runtime/burn_in.py`
- `hermes_cli/harness.py` command group `harness burn-in`
- `tests/agent_runtime/test_burn_in.py`
- `tests/hermes_cli/test_harness_cli.py`

The runner writes the ledger files above, creates a Harness task when needed, runs
`TickEngine.run_until_settled`, captures status/snapshot after monitor recording, appends
monitor findings, writes `archive_result.json`, and fails closed in `summarize` when any
required evidence file is missing. Live task/run/proof artifacts are preserved for review;
the burn-in runner records an explicit deferred archive decision instead of moving evidence
automatically.

### Manifest fields

Required:

- `burn_id`
- `case_id`
- `task_id`
- `started_at`
- `finished_at`
- `runtime_root`
- `hermes_home`
- `expected_persona_sequence`
- `actual_persona_sequence`
- `proof_ids`
- `incident_ids`
- `archive_batch`
- `status`
- `failure_class`
- `fix_commit`
- `rerun_of`

### Tests

- Unit test manifest creation with redaction-safe paths.
- Unit test burn-in run writes ledger files and summary passes only with complete evidence.
- Unit test summary fails closed when task/run/proof data is missing.
- Unit test freeze findings become monitor proofs and runtime-freeze incidents.

## 47B. No-Freeze Monitor

### Goal

Make freezes visible and actionable while a goal is running, not after the user notices
Mission Control stopped moving.

### Monitor signals

The burn-in monitor must sample:

- `harness status --json`
- `snapshot.observability.health`
- active run heartbeat age
- daemon heartbeat age
- task state and `updated_at`
- current stage id
- open incident count and kinds
- last run progress event
- proof count delta
- repeated next action
- repeated same persona/stage
- repeated context requests
- repeated skill load fanout
- repeated Dev read/search after failed proof
- autonomy packet presence and declared budgets
- selected/rejected skill counts
- proof intent attached before proof command
- environment fingerprint change before retry
- context compression receipt freshness
- run packaging metrics: tool turns per run, API calls per run, early close reason,
  and tick-to-run latency

### Freeze classifications

Add or reuse these classifications:

- `daemon_stale`
- `run_stalled`
- `task_state_unchanged`
- `same_next_action_repeated`
- `same_stage_retry_without_signal_change`
- `mcp_visual_capture_timeout`
- `autonomy_packet_missing`
- `tool_budget_exceeded_without_new_signal`
- `proof_retry_without_environment_delta`
- `context_resume_without_receipt`
- `duplicate_parallel_tool_request`
- `premature_run_close`
- `excessive_short_run_churn`
- `tick_cadence_too_slow`
- `max_actions_per_tick_bottleneck`
- `proof_command_timeout`
- `persona_invalid_output_loop`
- `neko_scope_loop`
- `qa_waiting_without_proof`

### Thresholds

Default burn-in thresholds:

- active run heartbeat stale: 2 minutes
- no task state/proof/progress change: 5 minutes
- same `next_action` repeated: 3 ticks
- same persona/stage retry: 2 retries unless environment fingerprint changed
- MCP visual capture: configured timeout plus 30 seconds
- proof command: configured timeout plus 30 seconds
- Neko scope updates: max 2 for same blocker

These thresholds are for certification. Production config may tune them, but tests must
prove defaults.

### Required behavior

When a freeze classification trips:

1. attach a monitor proof/log record;
2. open or update an incident with `kind=runtime_freeze` or a more specific kind;
3. route to deterministic recovery if safe;
4. otherwise route to Neko once;
5. if unchanged after Neko recovery cap, terminal-block with exact reason.

Implemented slice:

- `agent_runtime/no_freeze_monitor.py`
- `classify_freezes(...)` for `daemon_stale`, `run_stalled`,
  `same_next_action_repeated`, `same_stage_retry_without_signal_change`, and
  `persona_invalid_output_loop`
- `record_freeze_findings(...)` attaches redaction-safe log proofs, opens
  `runtime_freeze` incidents, links them back to the task, and blocks non-terminal tasks
- status/snapshot run summaries now expose `last_heartbeat_at`, making stale active runs
  detectable from Mission Control-safe data

Remaining monitor classes in the list above are still certification gaps and should be
implemented only when a deterministic test or live burn-in finding exercises them.

### Tests

- Fake clock test for stale run.
- Repeated next-action test.
- Same-stage retry without changed environment test.
- Monitor proof redaction test.

## 47C. Burn-In Goal Suite

Run these cases in order. A later case cannot be counted as certified if an earlier case
has an unresolved Harness bug.

### Case 1: No-op orchestration

Purpose: prove routing, handoffs, proof packet shape, QA verdict, and cleanup without
product edits.

Expected sequence:

```text
Neko -> Backend Dev -> Neko join/release -> Launcher Dev -> QA
```

Pass criteria:

- no code edits required;
- bounded proof commands only;
- no broad Docker/Flutter proof unless justified by task text;
- QA verdict cites proof packet;
- task ends terminal or intentionally blocked with exact reason;
- archive-ready preserves all evidence.

### Case 2: Backend-only edit

Purpose: prove one specialist can make a small real change, run focused tests, and hand
QA enough evidence.

Suggested shape:

- Harness-side or backend-side doc/test/code micro-change.
- Acceptance requires a focused Python/backend proof command.

Pass criteria:

- Backend Dev uses `harness-dev-delivery`;
- one bounded proof command after edits;
- failed proof, if any, is reused by proof ID;
- QA does not demand visual proof.

### Case 3: Launcher-only edit

Purpose: prove Launcher Dev can make a small UI/test change without dragging backend
or Docker into the mission.

Suggested shape:

- Mission Control visual polish or test-only assertion.
- Focused `flutter test <file>` proof.

Pass criteria:

- Launcher Dev uses Launcher skills only;
- no backend preflight;
- no visual proof unless task is user-visible;
- if visual required, fullscreen `launcher_qa` screenshot attached.

### Case 4: Cross-stack frontend/backend goal

Purpose: prove the real one-shot flow that Stage 45/46 were built for.

Expected sequence:

```text
Neko
Backend Dev
Neko join/release
Launcher Dev
QA
```

Pass criteria:

- Backend proof cannot close full goal by itself.
- Neko joins backend proof into a Launcher release packet.
- Launcher Dev receives only relevant context, not all backend logs.
- QA verifies both sides.
- If one side blocks, the blocker is proof-backed and does not become false success.

### Case 5: Visual-required Mission Control goal

Purpose: certify the MCP screenshot path under agent control.

Pass criteria:

- visual preflight reports `launcher_qa_mcp=ready` or proof-backed blocker;
- Harness requests fullscreen/fresh relaunch;
- screenshot artifact dimensions are at least 1920x1080 or display-maximized equivalent;
- proof metadata includes `capture_provider=launcher_qa`;
- QA cannot approve without the visual proof ID.

### Case 6: Environment-blocked recovery

Purpose: prove the system blocks honestly and does not retry forever.

Use a safe blocker only. Do not break credentials or delete dependencies.

Acceptable simulated blockers:

- temporary impossible proof command in a test task;
- fake missing MCP config in temp profile;
- fake preflight check in test harness;
- offline Docker only if already offline and operator accepts the environment state.

Pass criteria:

- proof-backed environment blocker created;
- Neko attempts bounded recovery or declares cannot self-heal;
- Dev does not repeat same command after unchanged environment;
- Mission Control shows exact blocker and next action.

## 47D. Persona Behavior Certification

### Neko

Neko passes Stage 47 only if live burn-in shows:

- wait semantics only at beginning;
- proceeds with assumptions after scope deadline when safe;
- no repeated scope updates beyond threshold;
- chooses single-specialist, specialist-plus-QA, or full multi-agent flow based on
  task risk instead of always paying the full orchestration cost;
- uses backend-first release joining when task is cross-stack;
- emits handoff packet with stable owner/repo/proof gates;
- reports alternatives after completion, not before;
- creates `cannot_self_heal` when recovery cap is exhausted.

Evidence required:

- Neko run IDs;
- handoff packet event IDs;
- scope update counter;
- self-heal counter;
- final QA or blocked verdict.

### Backend Dev

Backend Dev passes only if:

- uses relevant skills, not full skill fanout;
- emits an autonomy packet before broad inspection;
- runs one focused proof command after bounded inspection;
- declares proof intent before proof command execution;
- avoids Launcher/MCP proof unless task text requires it;
- reuses failed proof IDs after an environment fix;
- does not rerun same stage when environment fingerprint is unchanged.

### Launcher Dev

Launcher Dev passes only if:

- uses Launcher workflow and Stage C screenshot skills only when relevant;
- emits an autonomy packet before broad inspection;
- runs focused Flutter or Stage C proof;
- declares whether proof is command-level or product/visual-level;
- asks for visual proof when user-visible UI changed;
- does not use backend Docker checks unless the stage is cross-stack and backend-owned.

### QA

QA passes only if:

- verdict is evidence-backed;
- reviews autonomy packet IDs for each specialist run being approved;
- visual proof is required for user-visible visual behavior;
- visual proof is not required for non-visual test-validated claims;
- verdict packet contains exact proof IDs and remaining risk;
- blocker verdict explains why approval is impossible.

## 47E. Mission Control Operator Proof

Mission Control itself must be inspected during burn-in.

Required proof:

- fullscreen Stage C screenshot of Mission Control after at least one burn-in task;
- Run Inspector shows current/last task accurately;
- State Summary counts match CLI snapshot;
- Mission History/Archive area reflects archived test goals;
- visual overlap or truncation is recorded as a Launcher UI issue if present.

Known follow-up candidate:

- Agent map label/node overlap in fullscreen screenshot. If this affects readability,
  split into a Launcher UI stage and do not hide it under Harness certification.

## 47F. Patch-As-You-Go Loop

After each burn-in case:

1. run status and snapshot;
2. archive or cancel/close test artifacts through Harness CLI;
3. classify inefficiencies and blockers;
4. patch root cause;
5. run focused tests;
6. rerun the same burn-in case until it passes;
7. only then proceed to the next case.

Every patch must be narrow and tied to an observed failure. Do not add broad prompt text
or speculative state machine paths without a failing burn-in case or unit test.

## 47G. Final Certification Report

Create:

```text
docs/agent-runtime-harness/47-aaa-burn-in-results.md
```

Required sections:

- environment/runtime root/profile;
- commits under certification;
- burn-in case table;
- task IDs and archive batches;
- persona sequence per case;
- proof IDs and screenshot artifacts;
- failures observed;
- fixes implemented;
- tests run;
- remaining gaps;
- current verdict.

Certification levels:

- `not certified`: any P0/P1 burn-in case failed or was not run.
- `certified with caveats`: all P0 cases passed, but P2 UI polish/efficiency gaps remain.
- `AAA certified local`: all cases passed twice from clean baseline, runtime ended clean,
  and visual proof artifacts are archived.

## Deterministic Evidence Captured

Commands run after implementation:

- `python -m pytest tests/agent_runtime -q` -> exit `0`, `441 passed`
- `python -m pytest tests/hermes_cli/test_harness_cli.py tests/hermes_cli/test_startup_plugin_gating.py -q` -> exit `0`, `55 passed`
- `python -m ruff check agent_runtime/burn_in.py agent_runtime/no_freeze_monitor.py agent_runtime/role_sessions.py agent_runtime/ticker.py agent_runtime/profile_readiness.py agent_runtime/store.py agent_runtime/snapshot.py agent_runtime/observability.py hermes_cli/harness.py tests/...` -> exit `0`, all checks passed
- `python -m compileall -q agent_runtime hermes_cli tests/agent_runtime tests/hermes_cli/test_harness_cli.py` -> exit `0`
- `python -m hermes_cli.main harness burn-in summarize missing_burn --json` -> process exit `2`, clean JSON `ok=false`
- `python -m hermes_cli.main harness status --json` -> exit `0`, `open_tasks=0`, `active_runs=0`, `open_incidents=0`, health `healthy`, all personas `ready`
- `.venv\Scripts\python.exe -m pytest tests/agent_runtime/test_autonomy.py tests/agent_runtime/test_proof_runner.py::test_command_proof_runner_records_intent_and_safe_environment_fingerprint tests/agent_runtime/test_ticker.py::test_tick_injects_autonomy_packet_before_persona_runtime tests/agent_runtime/test_ticker.py::test_tick_passes_proof_intent_and_environment_fingerprint_metadata_to_runner tests/agent_runtime/test_no_freeze_monitor.py::test_no_freeze_monitor_classifies_autonomy_and_tool_budget_gaps -q` -> exit `0`, `5 passed`
- `.venv\Scripts\python.exe -m ruff check agent_runtime/autonomy.py agent_runtime/context_builder.py agent_runtime/persona_runtime.py agent_runtime/profile_runner.py agent_runtime/proof_runner.py agent_runtime/ticker.py agent_runtime/progress.py agent_runtime/observability.py agent_runtime/no_freeze_monitor.py tests/agent_runtime/test_autonomy.py tests/agent_runtime/test_ticker.py tests/agent_runtime/test_proof_runner.py tests/agent_runtime/test_no_freeze_monitor.py` -> exit `0`, all checks passed
- `.venv\Scripts\python.exe -m pytest tests/agent_runtime -q` -> exit `0`, `446 passed`
- `.venv\Scripts\python.exe -m pytest tests/hermes_cli/test_harness_cli.py tests/hermes_cli/test_startup_plugin_gating.py -q` -> exit `0`, `55 passed`
- `.venv\Scripts\python.exe -m ruff check agent_runtime tests\agent_runtime tests\hermes_cli\test_harness_cli.py tests\hermes_cli\test_startup_plugin_gating.py hermes_cli\harness.py` -> exit `0`, all checks passed
- `.venv\Scripts\python.exe -m compileall -q agent_runtime hermes_cli tests\agent_runtime tests\hermes_cli\test_harness_cli.py` -> exit `0`
- `& .venv\Scripts\python.exe -m hermes_cli.main harness burn-in summarize missing_burn --json; Write-Output "LASTEXIT=$LASTEXITCODE"` -> command wrapper exit `0`, inner CLI `LASTEXIT=2`, clean JSON `ok=false`
- `.venv\Scripts\python.exe -m hermes_cli.main harness status --json` -> exit `0`, `open_tasks=0`, `active_runs=0`, `open_incidents=0`, health `healthy`, all personas `ready`
- `.venv\Scripts\python.exe -m hermes_cli.main harness snapshot --json` -> exit `0`, summary `open_tasks=0`, `active_runs=0`, `open_incidents=0`
- `.venv\Scripts\python.exe -m pytest tests\agent_runtime\test_ticker.py -q -k "failed_command_proof or request_test_run_failed_proof_stays or second_same_stage"` -> exit `0`, `4 passed`
- `.venv\Scripts\python.exe -m pytest tests\agent_runtime\test_autonomy.py tests\agent_runtime\test_ticker.py -q -k "autonomy_packet or failed_command_proof or request_test_run_failed_proof_stays or second_same_stage"` -> exit `0`, `6 passed`
- `.venv\Scripts\python.exe -m pytest tests\agent_runtime -q` -> exit `0`, `459 passed`
- `.venv\Scripts\python.exe -m pytest tests\hermes_cli\test_harness_cli.py tests\hermes_cli\test_startup_plugin_gating.py -q` -> exit `0`, `55 passed`
- `.venv\Scripts\ruff.exe check agent_runtime tests\agent_runtime tests\hermes_cli\test_harness_cli.py tests\hermes_cli\test_startup_plugin_gating.py` -> exit `0`, all checks passed
- `.venv\Scripts\python.exe -m compileall -q agent_runtime hermes_cli tests\agent_runtime tests\hermes_cli` -> exit `0`
- `pytest tests\agent_runtime\test_profile_runner.py::test_progress_adapter_stops_mixed_read_search_budget_when_enabled tests\agent_runtime\test_profile_runner.py::test_runner_interrupts_agent_when_mixed_read_search_budget_is_swallowed -q` -> exit `0`, `2 passed`
- `pytest tests\agent_runtime\test_role_sessions.py::test_role_session_counts_loop_warning_as_watchdog_warning tests\agent_runtime\test_dev_discipline.py::test_progress_sink_uses_autonomy_read_search_limit_for_loop_warning -q` -> exit `0`, `2 passed`
- `pytest tests\agent_runtime\test_profile_runner.py tests\agent_runtime\test_role_sessions.py tests\agent_runtime\test_dev_discipline.py -q` -> exit `0`, `46 passed`
- `pytest tests\agent_runtime\test_context_builder.py tests\agent_runtime\test_decision_contracts.py tests\agent_runtime\test_planning.py tests\agent_runtime\test_proof_runner.py tests\agent_runtime\test_ticker.py::test_multi_agent_autonomy_runs_backend_neko_launcher_neko_qa_to_done -q` -> exit `0`, `80 passed`
- `ruff check agent_runtime tests\agent_runtime tests\hermes_cli\test_harness_cli.py tests\hermes_cli\test_startup_plugin_gating.py` -> exit `0`, all checks passed
- `pytest tests\agent_runtime -q` -> exit `0`, `489 passed`, `1 warning`
- `pytest tests\hermes_cli\test_harness_cli.py tests\hermes_cli\test_startup_plugin_gating.py -q` -> exit `0`, `55 passed`
- `python -m compileall -q agent_runtime hermes_cli tests\agent_runtime tests\hermes_cli` -> exit `0`
- `git diff --check` -> exit `0`
- `python -m hermes_cli.main harness status --json` -> exit `0`, `open_tasks=0`, `active_runs=0`, `open_incidents=0`, all personas `ready`
- `python -m hermes_cli.main harness snapshot --json` -> exit `0`, summary `open_tasks=0`, `active_runs=0`, `open_incidents=0`
- `python -m pytest -o addopts= -q tests\agent_runtime\test_dirty_state.py tests\agent_runtime\test_preflight.py tests\agent_runtime\test_burn_in.py tests\agent_runtime\test_status.py tests\agent_runtime\test_snapshot.py tests\agent_runtime\test_ticker.py tests\hermes_cli\test_harness_cli.py` -> exit `0`, `125 passed`
- `python -m pytest -o addopts= -q tests\agent_runtime` -> exit `0`, `499 passed`
- `python -m pytest -o addopts= -q tests\hermes_cli\test_harness_cli.py tests\hermes_cli\test_startup_plugin_gating.py` -> exit `0`, `56 passed`
- `.venv\Scripts\python.exe -m ruff check agent_runtime\dirty_state.py agent_runtime\goal_hygiene.py agent_runtime\preflight.py agent_runtime\ticker.py agent_runtime\repo_context.py agent_runtime\status.py agent_runtime\snapshot.py agent_runtime\burn_in.py hermes_cli\harness.py tests\agent_runtime\test_dirty_state.py tests\agent_runtime\test_preflight.py tests\agent_runtime\test_burn_in.py tests\agent_runtime\test_status.py tests\agent_runtime\test_snapshot.py tests\agent_runtime\test_ticker.py tests\hermes_cli\test_harness_cli.py` -> exit `0`, all checks passed
- `python -m compileall agent_runtime\dirty_state.py agent_runtime\goal_hygiene.py agent_runtime\preflight.py agent_runtime\ticker.py agent_runtime\repo_context.py agent_runtime\status.py agent_runtime\snapshot.py agent_runtime\burn_in.py hermes_cli\harness.py` -> exit `0`
- `python -m hermes_cli.main harness run cancel run_1d692d8918b5 --reason ... --json` -> exit `0`, run cancelled
- `python -m hermes_cli.main harness task cancel task_burn_b7e6c60f --reason ... --json` -> exit `0`, task cancelled
- `python -m hermes_cli.main harness task archive-ready --json` -> exit `0`, archive batch `20260605T225931247486Z_archive_ready`
- `python -m hermes_cli.main harness status --json` -> exit `0`, final compact status `open_tasks=0`, `active_runs=0`, `open_incidents=0`, daemon `offline`, dirty summary `repo_dirty=EterniaBackend`

Runtime cleanup evidence:

- Accidental live smoke task: `task_burn_3953e882`
- Run preserved: `run_05c9ec3d5ccf`
- Monitor proof preserved: `proof_monitor_5f7c7f3b`
- Runtime-freeze incident closed: `inc_b73ba9c3`
- Archive batch: `20260605T181530791998Z_archive_ready`

Live patch-as-you-go evidence:

- Failed no-op burn-in task: `task_burn_177fbec1`
- Failed burn-in manifest: `20260605T232334Z_noop-orchestration_94028b`
- Wrong-repo proof preserved:
  `test_task_burn_177fbec1_backend_no_op_route_proof_run_1d87331e9aa8_0_3ee45cdf`
- Proof artifact showed `workdir: <workdir:EterniaLauncher>` and `REPO: EterniaLauncher`
  for a backend-owned stage.
- Preflight blocker preserved:
  `preflight_task_burn_177fbec1_backend_no_op_route_proof_508793de`
- Incident preserved: `inc_766d6a78`
- Root-cause tests added:
  backend stage proof routing ignores incidental `launcher/` command text;
  wrong-known-repo command proof blocks instead of advancing;
  proof/context records expose `workdir_label`.
- Passed simple no-op burn-in task: `task_burn_d2dfc2a8`
- Passed simple burn-in ledger: `20260605T233645Z_noop-orchestration_0cb1e3`
- Simple burn-in proof IDs:
  `test_task_burn_d2dfc2a8_backend_no_op_route_run_a0dc825e7654_0_54c12760`
  (failed; missing backend venv),
  `test_task_burn_d2dfc2a8_backend_no_op_route_run_ce14526f7d8f_0_f845c792`
  (failed; missing backend venv),
  `test_task_burn_d2dfc2a8_backend_no_op_route_run_bbf370dd8da1_0_fcd4f0aa`
  (passed; backend venv activated),
  `test_task_burn_d2dfc2a8_launcher_contract_smoke_run_fb4a767bff37_0_54375a9b`
  (passed), and `proof_qa_88e01f15`.
- Root-cause tests added after the simple run:
  failed command proof attaches to `task.proof_ids`;
  failed proof ID is visible in the next retry context and autonomy packet;
  passing retry clears failed-proof memory;
  second same-stage failed proof routes to Neko self-heal.
- Passed simple no-op after context/packet hardening:
  `task_burn_38ad86bf`, stdout artifact
  `X:\Eternia\.hermes\agent-runtime\stage47_live_runs\simple_after_context_packet_fix_20260605T214103.stdout.json`,
  actual sequence `neko_supervisor -> backend_dev -> neko_supervisor -> dev ->
  neko_supervisor -> qa`, no incidents, archived in
  `20260605T214448139255Z_archive_ready`.
- Complex run after context/packet hardening blocked honestly:
  `task_burn_9e90d957`, stdout artifact
  `X:\Eternia\.hermes\agent-runtime\stage47_live_runs\complex_after_context_packet_fix_20260605T214458.stdout.json`,
  incident `inc_bfb003e2` for missing `handoff_packet.join_gate.release_condition`.
  Root fix: default QA-release join gate from required proof IDs and document/prompt the
  QA coordination packet shape. Task was cancelled and archived in
  `20260605T215456693623Z_archive_ready`.
- Complex run after QA-release fix blocked honestly:
  `task_burn_4623d6af`, stdout artifact
  `X:\Eternia\.hermes\agent-runtime\stage47_live_runs\complex_after_qa_release_fix_20260605T215507.stdout.json`,
  incident `inc_32c2f5b0` for missing `handoff_packet.proof_gate.required_proof_types`.
  The first Launcher proof failed because the generated command copied a redacted
  `<path:EterniaLauncher>` workdir prefix; the retry passed with a repo-relative command.
  Root fix: proof runner strips leading redacted workdir placeholders, prompt/skill forbid
  copying path labels, and QA proof-gate defaults are deterministic. Task was cancelled
  and archived in `20260605T220615050904Z_archive_ready`.
- Complex run after command/proof-gate fix reached `done`:
  `task_burn_395434be`, stdout artifact
  `X:\Eternia\.hermes\agent-runtime\stage47_live_runs\complex_after_command_and_qagate_fix_20260605T220624.stdout.json`,
  proof IDs
  `test_task_burn_395434be_backend_contract_burn_in_run_fbe7cffb0426_0_65e4a60a`,
  `test_task_burn_395434be_launcher_contract_smoke_run_03b7b749ed8f_0_91ed3585`,
  and `proof_qa_51c6bb73`, no incidents. This run exposed the final efficiency gap:
  Backend Dev used `312171` tokens and Launcher Dev used `369413` tokens, both with
  `read_search_without_patch_threshold`. Root fix after this run: aggregate read/search
  watchdog interruption and role-session `watchdog_warnings` accounting.

This evidence proves the deterministic Stage 47 implementation surface is ready for the
planned live-token certification goals. It does not yet certify the live simple/complex
goal suite after the final aggregate-watchdog implementation.

## Acceptance Checklist

Stage 47 is complete only when:

- [x] Unknown keys in `delivery`/`qa_review` packets are tolerated with an `operator_note`; real secret/path shapes still fatal.
- [x] Bare secret-vocabulary prose in packet bodies is masked-in-place, not a terminal `model_invalid_output`.
- [x] Missing-but-defaultable packet enums (e.g. `proof_gate.minimum_status`) default instead of failing the run.
- [x] `model_invalid_output` repair context survives a run boundary (no cold-restart whack-a-mole).
- [ ] Burn-in shows invalid-output incidents per goal trending toward 0 versus the `task_737ab93c` baseline (16/29 invalid, 1.44M tokens).
- [x] Continuous role-session metrics identify would-have-continued runs.
- [x] `should_continue_role_session` is unit-tested and behind config.
- [ ] Continuous role-session mode is burn-in tested for Dev and Launcher Dev.
- [x] Role-session events appear in observability with redaction-safe payloads.
- [x] Burn-in runner or documented ledger creates repeatable evidence directories.
- [x] No-freeze monitor classifies and records stalls.
- [x] Every specialist run emits a redaction-safe autonomy packet before substantial work.
- [x] Tool economy governor records read/search/proof budgets and flags budget overruns.
- [x] Mixed read/search budget overruns interrupt Dev/QA runs through the agent `interrupt()` path even when callback exceptions are swallowed.
- [x] Role-session progress/close payloads expose `watchdog_warnings` for loop/budget pressure.
- [x] Context compression receipts exist for resumed or long-running agent context.
- [x] Proof commands have proof intent and environment fingerprint metadata.
- [ ] No-op orchestration goal passes after aggregate-watchdog hardening.
- [ ] Backend-only edit goal passes.
- [ ] Launcher-only edit goal passes.
- [ ] Cross-stack frontend/backend goal passes after aggregate-watchdog hardening.
- [ ] Visual-required Mission Control goal passes with fullscreen screenshot.
- [ ] Environment-blocked recovery goal terminal-blocks honestly.
- [x] Test artifacts are cancelled/archived without hard delete.
- [x] Harness status ends with `0` open tasks, `0` open incidents, `0` active runs.
- [x] Full relevant test suite passes.
- [ ] Final Stage 47 results doc exists.
- [ ] Commits are recorded.

## First Implementation Slice

Start with a narrow slice:

1. Land 47A0R contract robustness first: uniform unknown-key tolerance for
   `delivery`/`qa_review`, redact-in-place for bare secret-words, defaultable enums, and
   the contract-robustness unit tests. This removes the dominant `model_invalid_output`
   churn before any loop change.
2. Add role-session metrics in observe-only mode.
3. Add `should_continue_role_session` behind config with unit tests.
4. Add burn-in ledger writer and monitor sampler.
5. Add no-freeze and short-run-churn classification tests.
6. Run Case 1 no-op orchestration with real tokens, first observe-only and then with
   continuous Dev mode enabled.
7. Patch only the first observed failure.
8. Archive artifacts and write the first section of `47-aaa-burn-in-results.md`.

Do not jump straight to the cross-stack edit before Case 1 passes. The goal is to make
the Harness boring and trustworthy one failure pattern at a time.
