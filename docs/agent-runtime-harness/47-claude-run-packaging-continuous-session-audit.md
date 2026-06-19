# Stage 47 — Independent Run Packaging, Same-Session, and Multi-Agent Efficiency Audit

Date: 2026-06-05
Owner: Claude independent Harness audit (commissioned by Tony)
Status: audit complete; root cause integrated into the Stage 47 plan as §47A0R
(Contract Robustness and Redaction Remediation). No new stage opened.
Scope: read-only audit of run packaging, same-session continuation, and multi-agent
efficiency. No runtime evidence was hard-deleted. No source files were modified.

This is a Stage 47 sibling artifact (companion to
`47-aaa-burn-in-no-freeze-certification.md`, the same way Stage 45 has three docs). It is
**not** a new stage. Its job is to do the empirical work Stage 47 §9 asked for —
*distinguish* the run-packaging causes with runtime evidence — and to surface one root
cause the Stage 47 doc does not yet name. See §0.1 for the head-to-head comparison.

## 0. Verdict In One Paragraph

The Harness does **not** yet behave like "one uninterrupted competent agent per role."
The dominant reason is **not** tick cadence and **not** `max_actions_per_tick`. It is a
combination of (1) **brittle, fail-fatal payload/packet contract validation plus
over-eager secret redaction** that turns recoverable model formatting slips into terminal
`model_invalid_output` run failures, (2) **per-decision run packaging** where every valid
`AgentDecision` closes the Harness run and the next step opens a brand-new run, and (3) an
**autonomous settler that halts on the first invalid output / action failure**, which is
why a human had to re-launch the mission a dozen times. Session continuation exists and is
correct, but it is *stitched across separate run records*, not a continuous role session.
Recommended direction: change **run lifecycle + contract robustness + settler resilience**
together; tick rate and `max_actions_per_tick` are the wrong levers.

## 0.1 Relationship To `47-aaa-burn-in-no-freeze-certification.md`

Stage 47 already anticipated most of the *structure* of this problem. This audit's value is
not re-proposing it — it is supplying the runtime evidence Stage 47 left open, ranking the
candidates, and naming one missing root cause. Honest accounting:

| Topic | Stage 47 burn-in doc | This audit | Net |
|-------|----------------------|-----------|-----|
| Continuous role-session loop | **Already specced in §10** (open/close conditions verbatim) | I reproduce the same loop as patch P2 | **Stage 47 owns this idea — I confirm and credit it, not invent it** |
| Run-packaging causes | §9 lists 5 candidates and says "Stage 47 *must distinguish*" them | I distinguish them with `task_737ab93c` evidence and **rank** them | **New: the distinguishing/ranking 47 asked for** |
| Tick cadence / `max_actions_per_tick` | §47B lists `tick_cadence_too_slow` and `max_actions_per_tick_bottleneck` as live freeze classes | I show with evidence they are **not** the cause | **New: rejects two candidates 47 kept open** |
| `persona_invalid_output_loop` | §47B lists it as a *symptom to monitor* | I diagnose **why** outputs are invalid: fail-fatal contract validation + false-positive redaction (RC1) | **New and highest-leverage: 47 never names this cause** |
| Autonomy packet | "Cross-Cutting Agent Autonomy Contract" *mandates* an autonomy packet before work | I show the packet/contract validators are exactly what kill runs today (a packet `objective` tripped a false "secret" match) — so the mandate must ship **with** P1 or it will increase invalid-output churn | **New tension 47 should resolve before adding more required packet fields** |
| Settler "walk away" | End State requires unattended completion; §47B has `excessive_short_run_churn`/`premature_run_close` | I locate the mechanism: `run_until_settled` stops on `action_failed`/`incident_opened`, proven by the `operator-runs` re-launch trail (RC3) | **New: the concrete code+evidence behind the symptom** |
| Burn-in runner / ledger / monitor / goal suite / persona cert | §47A–47G own these | Out of scope here; my patches feed §47F (patch-as-you-go) | **Defer to 47** |

Bottom line: **agree with Stage 47's direction; the continuous-session loop (47 §10) is
correct but not sufficient.** The one thing 47 is missing is RC1 — and RC1 must be fixed
*before* the §10 loop and *before* adding more mandatory packet fields, or both will pour
more model output through the same brittle validators.

## 1. Method, Commands Run, and Exit Codes

All analysis was performed read-only against the harness repo and the live runtime root.
Event analysis used PowerShell 7 scripts that parse `events.jsonl` as JSONL (kept under
`.audit_tmp/`, not committed, removed after the audit).

| # | Command | Purpose | Exit |
|---|---------|---------|------|
| 1 | `git status` / `git branch --show-current` / `git log --oneline -8` | confirm clean tree, branch `main` (27 ahead of origin) | 0 |
| 2 | `git log --oneline -- agent_runtime/packets.py` | date packet-validator changes vs. smoke | 0 |
| 3 | `git log -S "_normalize_unknown_handoff_metadata" -- agent_runtime/packets.py` | pinpoint reactive patch commit | 0 |
| 4 | `git log -S "stage46_rules" -- agent_runtime/packets.py` | pinpoint allowed-key addition | 0 |
| 5 | PowerShell: parse `events.jsonl` (8538 lines) → event-type histogram | global event shape | 0 |
| 6 | PowerShell: per-task run/incident/transition/proof/tool aggregation | identify the 29-run task | 0 |
| 7 | PowerShell: per-event schema dump (`run.opened/closed`, `task.transition`, `incident.opened`, `packet.recorded`) | learn payload fields | 0 |
| 8 | PowerShell: full run-by-run timeline for `task_737ab93c` (durations, tokens, api_calls, session in/out) | the forensic core | 0 |
| 9 | PowerShell: incident kinds + invalid-run reasons + tick provenance for `task_737ab93c` | failure taxonomy | 0 |
| 10 | PowerShell: read all `incidents/*.json`, filter by task and by `model_invalid_output` | exact validator error strings | 0 |
| 11 | PowerShell: tool-name distribution + duplicate-tool-start detection + proof summary | skill/read-search churn | 0 |
| 12 | PowerShell: inter-run gap vs. incident-overlap analysis + systemic cross-check (`task_a83fc30a`) | reject tick-rate hypothesis | 0 |

Git status at audit start and end: only `.claude/` (pre-existing) and `.audit_tmp/`
(audit scratch) untracked. No tracked file modified.

## 2. Files Inspected

Source (read-only):
`agent_runtime/ticker.py`, `persona_runtime.py`, `store.py`, `state_machine.py`,
`decision_schema.py`, `decision_contracts.py`, `packets.py`, `dev_discipline.py`,
`proof_runner.py`, `recovery.py`, `runtime_config.py`; tests
`tests/agent_runtime/test_persona_runtime_invalid.py` (house test conventions),
directory listing of `tests/agent_runtime/` (60 test modules).

Runtime evidence (read-only):
`X:\Eternia\.hermes\agent-runtime\events.jsonl` (8538 events, 2026-05-31 → 2026-06-05),
`daemon_status.json`, `incidents/*.json` (25 files), `operator-runs/` listing,
`deleted_archive/` listing (24 archive batches), `context/` listing,
`docs/agent-runtime-harness/47-aaa-burn-in-no-freeze-certification.md`.

## 3. Evidence

### 3.1 The subject mission: `task_737ab93c`

This is the "last real-token smoke" referenced in Stage 47 §Remaining Risk item 7
(29 runs, 12 incidents). Window: 2026-06-05 **05:23:14 → 07:27:01 UTC**.

Run lifecycle (close order; `Valid` = decision parsed and contract-validated):

| # | Persona | Stage | Dur(s) | Decision | Valid | State | api | tokens | sess_in→out |
|---|---------|-------|-------|----------|-------|-------|-----|--------|-------------|
| 1 | neko | — | 80 | — | invalid | failed | 3 | 37339 | —→3a03d3 |
| 2 | neko | — | 77 | — | invalid | failed | 3 | 37436 | —→1f4fa7 |
| 3 | neko | — | 90 | — | invalid | failed | 3 | 37270 | —→5dfc0f |
| 4 | neko | — | 92 | — | invalid | failed | 3 | 37281 | —→421fd8 |
| 5 | neko | — | 72 | propose_acceptance | valid | completed | 3 | 37270 | —→f75666 |
| 6 | neko | — | 75 | propose_acceptance | valid | completed | 3 | 39487 | **f75666→f75666** |
| 7–11 | backend_dev | — | 85–515 | — | invalid | **cancelled** | — | — | — |
| 12 | backend_dev | — | 39 | request_test_run | valid | completed | 4 | 99298 | —→2c45f2 |
| 13 | neko | backend_smoke | 83 | — | invalid | failed | 2 | 27452 | f75666→f75666 |
| 14 | neko | backend_smoke | 99 | — | invalid | failed | 5 | 68563 | —→c02f99 |
| 15 | neko | backend_smoke | 35 | propose_acceptance | valid | completed | 3 | 41986 | —→ac3dae |
| 16 | neko | backend_smoke | 43 | propose_acceptance | valid | completed | 3 | 42004 | **ac3dae→ac3dae** |
| 17 | dev | backend_smoke | 58 | — | invalid | failed | 5 | 138443 | —→9919e1 |
| 18 | neko | backend_smoke | 51 | needs_context | valid | completed | 4 | 60440 | ac3dae→ac3dae |
| 19 | backend_dev | launcher_smoke | 41 | block | valid | completed | 5 | 119580 | 2c45f2→2c45f2 |
| 20 | backend_dev | launcher_smoke | 93 | — | invalid | **cancelled** | — | — | 2c45f2 |
| 21 | dev | launcher_smoke | 58 | request_test_run | valid | completed | 5 | 137395 | —→f7d526 |
| 22 | dev | launcher_smoke | 80 | request_test_run | valid | completed | 5 | 138856 | **f7d526→f7d526** |
| 23 | neko | launcher_smoke | 42 | propose_acceptance | valid | completed | 4 | 79317 | ac3dae→ac3dae |
| 24 | qa | launcher_smoke | 58 | — | invalid | failed | 3 | 40671 | —→bfd14e |
| 25 | neko | launcher_smoke | 39 | propose_acceptance | valid | completed | 3 | 44682 | ac3dae→ac3dae |
| 26 | dev | launcher_smoke | 27 | request_qa_review | valid | completed | 2 | 70820 | f7d526→f7d526 |
| 27 | qa | launcher_smoke | 68 | — | invalid | failed | 2 | 28455 | —→35a1a7 |
| 28 | neko | launcher_smoke | 92 | — | invalid | failed | 3 | 40975 | ac3dae→ac3dae |
| 29 | qa | launcher_smoke | 36 | report_qa_verdict | valid | completed | 2 | 31023 | —→5859e0 |

Totals: **29 runs, 13 valid, 16 invalid (55%)**, **1,436,043 tokens**, 78 api calls,
12 incidents. Persona run counts: neko 14, backend_dev 8, dev 4, qa 3. The mission did
reach a `report_qa_verdict` and the task was later archived `done` — so it "worked," but
at ~1.4M tokens and 16 wasted runs for what is structurally a 5-step
Neko→Backend→Neko→Launcher→QA flow.

### 3.2 The 12 incidents (the churn engine)

| kind | count | meaning |
|------|------|---------|
| `model_invalid_output` | 9 | model output rejected by schema/contract/redaction (terminal run fail) |
| `environment_blocker` | 2 | `launcher_qa_mcp` preflight blocked dev/backend_dev (no run opened) |
| `provider_failure` | 1 | API/provider error mid-run |

Exact `model_invalid_output` reasons (from `incidents/*.json` `summary`):

1. `handoff_packet has unsupported keys: ['stage46_rules']`
2. `handoff_packet has unsupported keys: ['final_owner']`
3. `handoff_packet has unsupported keys: ['failed_proof_reuse']`
4. `handoff_packet.proof_gate missing minimum_status`
5. `handoff_packet.proof_gate.minimum_status is invalid`
6. `packet.launcher_dev_scope.objective contains secret-looking text`  ← false positive
7. `packet.remaining_gaps[0] contains secret-looking text`  ← false positive
8. `qa_review has unsupported keys: ['notes']`
9. `Dev must reference attached failed proof IDs before same-stage proof retry, or block…`

Seven of nine are Neko packet-shape problems; two are **false-positive redaction trips on
legitimate prose**; one is a QA extra-key; one is a Dev discipline gate. Each one **failed
the whole run, opened an incident, set the task `BLOCKED`, and routed to Neko** — i.e. a
formatting slip became a full cold-restart.

Smoking-gun inconsistency: the key `failed_proof_reuse` appears **both** as a fatal
incident (#3 above) **and**, in a `packet.recorded` event on the same task, as a silently
tolerated note: `"operator_note": "ignored unsupported metadata keys: failed_proof_reuse"`.
The same unknown key is fatal in one code path and ignored in another.

### 3.3 Proofs ran correctly and were monitored

`proof.attached` for the task: 2 preflight `log/failed` (the `launcher_qa_mcp` env
blockers), `test_run/passed` (backend), `test_run/failed` **then** `test_run/passed`
(launcher — a correct failed→fixed→retry), and a final `qa_verdict/attached`. Command
proofs are bounded and heartbeated (see §3.6). Proof execution is **not** a freeze source.

### 3.4 Tool economy: skill-navigation and read/search thrash

Tool starts for the task (133 total): `skill_view` 43, `skill_search` 33, `search_files`
25, `read_file` 22, `terminal` 10. **76 of 133 (57%) were skill navigation.** 39
distinct `(run_id, tool_name)` groups fired the same tool more than once in a single run;
the worst offenders are the **cancelled** backend_dev exploration runs (e.g. run
`8ee5a15f…` did 6× `search_files` + 4× `skill_search` + 2× `skill_view`, produced no
patch, and was cancelled). This is the `read_search_without_patch_threshold` pattern
(`dev_discipline.update_progress_telemetry`, threshold = 4 reads with 0 patches).

### 3.5 Drive mode and inter-run gaps (the tick-rate test)

`daemon_status.json` at audit time: `state=idle`, `loops=1`,
`settle_stop_reason=no_eligible_action`, `wait_seconds=30`. There are **zero**
`run.heartbeat` and **zero** daemon-tick events in `events.jsonl`. `operator-runs/`
contains the manual re-launch trail: `stage46_retry`, `stage46_next`,
`stage46_continue`, `stage46_launcher`, `stage46_launcher_dev`,
`stage46_launcher_dev_retry`, `stage46_launcher_resume`, `stage46_qa_final`. The smoke was
**operator-driven via repeated `run-until-settled` invocations**, not a free-running
daemon.

Inter-run gaps for `task_737ab93c` ranged 75–454 s. Gaps that coincide with an
`incident.opened` are recovery cycles (an incident open→close spans ~3 min, e.g.
`05:24:42→05:27:38`). Gaps with no incident are still 96–394 s — consistent with operator
re-launch latency after a settle boundary, **not** the configured 10 s active / 30 s idle
daemon cadence. Faster ticks cannot shrink these gaps because the settler *stops* at
boundaries and incidents.

### 3.6 Relevant config defaults (`runtime_config.py`)

`max_actions_per_tick=1`, `daemon_interval_seconds=10`, `daemon_idle_interval_seconds=30`,
`heartbeat_ttl_seconds=900`, `daemon_max_retries_per_state=3`,
`live_run_max_wall_seconds=300`, `live_run_max_api_calls=20`,
`live_run_max_total_tokens=750_000`, `live_run_iteration_budget=60`,
`mission_max_total_tokens=1_000_000`, `neko_recovery_attempt_cap=2`. The smoke burned
1.44M tokens on one task, exceeding the 1.0M mission token cap — i.e. the mission token
governor did not bound the wasteful churn.

### 3.7 Systemic cross-check and global rates

The earlier 32-run smoke `task_a83fc30a` (2026-06-02, older code) shows the same family of
problems with a different mix: 4 `model_invalid_output` + **7 `run_budget_exceeded`**,
4.40M tokens, and idle gaps up to 11.7 h (left overnight). Across **all** history:
276 `run.closed`, **47 invalid (17%)**; incident kinds **`run_budget_exceeded` 20,
`model_invalid_output` 19, `provider_failure` 6, `environment_blocker` 2**. The two
dominant incident classes are both *run killers that force reroutes/restarts*.

### 3.8 Git timing that changes the reading of the evidence

Commit `4863fc05c feat(harness): harden stage46 persona self-healing` is dated
`2026-06-05 03:27:46 -0400` = **07:27:46 UTC**, ~45 s **after** the smoke's last event
(07:27:01 UTC). That commit (a) added `stage46_rules` and `final_owner` to
`HANDOFF_PACKET_KEYS` and (b) added `_normalize_unknown_handoff_metadata` (graceful ignore
of unknown handoff keys). **Therefore the 29-run smoke ran on pre-`4863fc05c` code, and
that commit was a reactive partial patch to exactly these failures.** No real-token
multi-run smoke has run since. The current tree still has the unfixed slices below.

## 4. Hypothesis Verdicts

| # | Hypothesis | Verdict | Basis |
|---|-----------|---------|-------|
| 1 | Not mainly slow due to low tick rate | **CONFIRMED (accepted)** | daemon idle; gaps are incident-recovery + operator re-launch, not 10–30 s cadence (§3.5) |
| 2 | Bigger issue is run packaging: every valid AgentDecision closes a run | **CONFIRMED but re-ranked** | true in `ticker.py:326`, but accounts for only ~3 mergeable consecutive runs (5→6, 15→16, 21→22). Second root cause, not first (§5 RC2) |
| 3 | Session_id is carried forward, but stitched, not continuous | **CONFIRMED exactly** | `store.latest_session_id`; `sess_in` reuse in §3.1; separate run records per decision |
| 4 | Last smoke reached done but inefficient (29 runs, 12 incidents, invalid outputs, read/search warnings, dup tool starts, QA caveat) | **CONFIRMED in full** | §3.1–3.4; every sub-claim reproduced |
| 5 | max_actions_per_tick conservative, but raising tick rate alone won't fix churn | **CONFIRMED** | it is 1; raising it batches runs/tick but does not reduce run count (one run per action) (§3.6) |
| 6 | Continuous role-session loop is the best direction | **ENDORSED with caveat (this is Stage 47 §10, not a new idea)** | the loop is already specced in Stage 47 §10 and is **not implemented** in code (grep clean); it is necessary but **not sufficient** without first making contracts non-fatal (§5 RC1/RC2) |

### Rejected as primary causes

- **Tick cadence / `daemon_interval_seconds`** — not the bottleneck; the settler halts at
  boundaries regardless.
- **`max_actions_per_tick`** — at 1, but it changes batching, not run count.
- **Session-reuse failing** — it works and correctly refuses poisoned sessions
  (`_latest_run_is_invalid`, `_run_session_is_reusable`).
- **Proof-command monitoring freezing the mission** — bounded + heartbeated (§3.6,
  `proof_runner.py` poll loop, `PROOF_MONITOR_HEARTBEAT_SECONDS=10`, process-tree kill on
  timeout).

## 5. Root-Cause Analysis (severity-ranked)

### RC1 — Brittle, fail-fatal contract validation + over-eager redaction  · P0 · #1 token/incident driver
- `decision_schema._validate_raw_decision` uses `additionalProperties:false` and a 280-char
  `summary` cap; `decision_contracts.validate_planning_decision` → `packets.validate_decision_packets`
  enforce strict packet shapes. Any unknown key, missing enum, or prose containing
  `token|secret|credential|authorization|cookie|bearer` (`packets._scan_packet_redaction`,
  `_SECRET_WORDS`) raises `DecisionPayloadInvalid` → `model_invalid_output` → **terminal run
  failure + incident + task BLOCKED + reroute** (`ticker.py:385–391`).
- Evidence: 9/9 invalid-output reasons in §3.2; 2 are pure false-positive redaction trips on
  legitimate packet prose.
- **Asymmetry**: `handoff_packet` now degrades gracefully (`_normalize_unknown_handoff_metadata`,
  added post-smoke), but `delivery` and `qa_review` still hard-fail via
  `_reject_unknown_packet_keys`. The post-smoke "fix" was whack-a-mole (enumerate each key
  the model invents), not a robustness principle.
- Net effect: recoverable formatting slips are the leading cause of run multiplication and
  the 1.44M-token burn.

### RC2 — Per-decision run packaging: no continuous role session  · P0 · architectural gap vs. "one agent per role"
- `ticker._execute_action` invokes the model exactly once, applies the decision, optionally
  collects proof, then **closes the run** (`ticker.py:326`) and returns. There is no
  loop-back to keep the same run/session alive when the next action is the same
  task/persona/stage. Grep confirms no continuous-session loop exists; only budget-approval
  "same-session continuation" hints (`store.py:476`, `ticker.py:366`).
- Consecutive same-session valid runs (5→6, 15→16, 21→22) prove the model *session* already
  continues, but it is re-wrapped in a fresh Harness *run* each time → context rebuild and
  (after any invalid) re-discovery of skills (the 76 skill-nav calls).
- Compounded by **tight per-run budgets** (`api_calls=20`, `tokens=750k`): a single coherent
  specialist job that needs >20 API calls is forced into `run_budget_exceeded` (20 such
  incidents globally) and a budget-approval handoff — another fragmentation axis.

### RC3 — Autonomous settler halts on first invalid output / action failure  · P0 · breaks "walk away"
- `run_until_settled` stops on `action_failed` (`ticker.py:176–178`) and `incident_opened`
  (`ticker.py:206`). Every `model_invalid_output` halts unattended progress.
- Evidence: the `operator-runs/stage46_{retry,next,continue,resume,launcher_dev_retry,…}`
  trail is a human re-launching the mission after each halt. With 12 incidents, the mission
  could not run unattended end-to-end. This is the most direct violation of the Stage 47
  end state ("start a goal and leave it alone").

### RC4 — Cold-restart amplification  · P1
- `persona_runtime.run_tick` already retries **twice in-session** with a `requires_repair`
  hint, but on double-failure the whole run dies and the next run is a **fresh cold
  session** (poisoned sessions are correctly *not* reused). Runs 1–4 each failed on a
  *different* key (`stage46_rules`, `final_owner`, missing `minimum_status`,
  `failed_proof_reuse`) — whack-a-mole across cold restarts, because accumulated repair
  context is discarded at each run boundary.

### RC5 — Skill-navigation overhead and exploration thrash  · P2
- 57% of tool calls were `skill_search`/`skill_view`; backend_dev exploration loops hit the
  read/search threshold and were `operator_cancelled`. `dev_discipline` correctly *detects*
  this, but enforces via fatal `DecisionPayloadInvalid` (feeding RC1) rather than an
  in-session nudge.

## 6. Severity Ranking

| Rank | Root cause | Severity | Primary symptom it explains |
|------|-----------|----------|------------------------------|
| 1 | RC1 contract brittleness + redaction false positives | P0 | 9/12 incidents, 1.44M tokens, cold-restart churn |
| 2 | RC2 per-decision run packaging (+ tight per-run budgets) | P0 | run-count inflation among valid steps; 20 budget incidents |
| 3 | RC3 settler halts on invalid/failed | P0 | no unattended completion; operator babysitting |
| 4 | RC4 cold-restart amplification | P1 | repeated *different* contract slips per retry |
| 5 | RC5 skill/read-search economy | P2 | 57% skill-nav, cancelled exploration runs |
| — | tick rate / `max_actions_per_tick` | not a cause | — |

## 7. Implementation Plan (patches feed Stage 47 §47F, not a new stage)

These are root-cause patches for Stage 47's "Patch-As-You-Go Loop" (§47F). They are labelled
P1–P5 to avoid colliding with Stage 47's own 47A–47G substage letters. Principle (from Stage
47 Non-Goals, honored here): do not redesign the state machine, do not add a second proof
format, prefer deterministic gates over prompt text, keep each patch tied to an observed
failure with a unit test. P2 is Stage 47 §10's continuous-session loop; it must ship **after**
P1 (contracts made non-fatal), or it will simply retry against brittle validators.

### P1 — Make contract validation degrade, not detonate (fixes RC1; do this first)
- Generalize the existing `handoff_packet` tolerance to **all** packet types: replace
  `_reject_unknown_packet_keys` in `_validate_delivery` / `_validate_qa_review` with the
  `_normalize_unknown_handoff_metadata` pattern (strip unknown keys, record an
  `operator_note`, keep the run alive).
- Split validation severity into `fatal` vs. `repairable`. Unknown keys, over-length
  `summary`, missing-but-defaultable enum (`minimum_status` already auto-defaults at
  `packets.py:275–276` — extend this stance) become **warnings** surfaced to the in-session
  repair hint, not run killers.
- Tighten `_scan_packet_redaction`: only fail on the structured `_SECRET_PATTERNS`
  (assignment / bearer-token shapes) and absolute paths; downgrade the bare `_SECRET_WORDS`
  vocabulary match to a **redact-in-place** (mask the span) instead of raising. This removes
  the two false-positive run kills without weakening real secret defense.
- Emit a new redaction-safe `run.progress` step `contract_repaired` recording what was
  normalized, so QA/burn-in can see tolerance was applied.

### P2 — Continuous role-session loop (fixes RC2; **implements Stage 47 §10**)
- In `ticker._execute_action`, after a valid decision + deterministic side effect + optional
  proof, **refresh task/context/proofs and loop back to invoke the model again in the same
  run/session** when *all* hold: same task, same persona, same `current_stage_id`, no open
  incident requiring Neko/QA/human, no owner transition, no approval gate, redaction-safe
  `session_id`, watchdog counters quiet. Close the run only at a true boundary (owner change,
  QA verdict/blocker, proof-fail without environment delta, two validation failures, budget
  cap, duplicate-tool loop, Neko re-scope). This is exactly the loop sketched in Stage 47 §10.
- Add an **elastic in-session budget**: keep the per-iteration caps but allow N decisions per
  run up to a session wall/token cap, so a specialist can inspect→patch→prove→interpret→hand
  off in one run when budget allows (Stage 47 §9 target).
- Keep `max_actions_per_tick=1` (it is fine); the loop lives *inside* one action.

### P3 — Settler resilience for unattended runs (fixes RC3)
- Distinguish *recoverable* action failures (a single `model_invalid_output` routed to a
  bounded Neko/self-heal pass) from *terminal* boundaries. In `run_until_settled`, instead of
  unconditional `action_failed` stop, allow the settler to **continue into the deterministic
  recovery action** for up to `neko_recovery_attempt_cap` passes per blocker before stopping.
  Stop only when recovery is exhausted or the environment fingerprint is unchanged.
- This is what lets the daemon (or one `run-until-settled`) carry a mission through a
  transient invalid output without a human re-launch — directly addressing the operator-runs
  babysitting trail.

### P4 — In-session repair continuity (fixes RC4)
- When a run closes for `model_invalid_output` but the session is otherwise healthy, carry the
  accumulated repair hint (the exact failing keys/enums) into the next run's first context so
  the model does not re-make a *different* slip from a blank slate. Pairs with P1 to converge
  in ≤2 attempts.

### P5 — Tool/skill economy nudges (fixes RC5)
- Make `read_search_without_patch_threshold` and skill-nav fanout emit an **in-session
  steering nudge** (next-context instruction to stop exploring and patch/prove/block) before
  any fatal gate. Only escalate to a blocker if the nudge is ignored. Reduces the 57%
  skill-nav overhead without killing runs.

## 8. Test Plan

House convention (`tests/agent_runtime/`, fake `agent_factory` returning canned
`final_response`, `pytest.raises(DecisionPayloadInvalid)` / decision assertions).

P1 (`test_decision_contracts.py`, `test_persona_runtime_invalid.py`):
- delivery/qa_review packet with an unknown key → run survives, `operator_note` records the
  ignored key, no incident (mirror existing handoff behavior).
- packet prose `"objective": "validate the auth token refresh contract"` → **not** a fatal
  redaction trip; the word is masked-in-place, run survives.
- real secret shapes (`TOKEN=abc123`, `bearer eyJ…`) and absolute paths → still fatal.
- `minimum_status` absent → defaults to `passed`, run survives (lock current behavior).

P2 (`test_ticker.py`):
- fake runtime emitting two consecutive same-persona/same-stage valid decisions → **one**
  `run.opened`/`run.closed` pair, two decisions applied (assert no premature close).
- owner transition / QA verdict / second validation failure / budget cap each → run closes at
  the boundary (table-driven).
- elastic budget: N decisions within session caps stay in one run; N+1 over cap closes with
  `budget` reason.

P3 (`test_ticker.py`):
- `run_until_settled` with a fake that returns one invalid then valid → settler does **not**
  stop on the first invalid; it runs the bounded recovery and reaches the valid boundary
  (fake-clock, no real model).
- recovery cap exhausted with unchanged environment fingerprint → settler stops with an exact
  terminal reason.

P4 (`test_persona_runtime_invalid.py`):
- two-run sequence where run 1 fails on key A; assert run 2's first user_message carries the
  key-A repair hint (extends the existing `"Previous decision parse failed"` assertion).

P5 (`test_dev_discipline.py`):
- ≥4 reads, 0 patches → emits steering nudge payload, **not** `DecisionPayloadInvalid`, on
  first occurrence; fatal only after the nudge is ignored.

Regression gate: run the full `tests/agent_runtime/` suite (60 modules) before and after each
substage; no net-new failures.

## 9. Risks and Rollback

| Change | Risk | Mitigation | Rollback |
|--------|------|------------|----------|
| P1 relax redaction | masking a genuine secret in a packet body | keep structured secret/path patterns fatal; only downgrade the bare-vocabulary match to masking; add a redaction unit test with real secret shapes | revert `_scan_packet_redaction` change; behavior returns to fail-closed |
| P1 tolerate unknown keys | hiding a real schema drift | record every normalization in `operator_note` + `contract_repaired` progress; burn-in reviews the notes | per-packet-type flag to re-enable strict reject |
| P2 continuous loop | a stuck/looping session runs longer before a watchdog fires | the close conditions include duplicate-tool-loop and two-invalid and budget caps; watchdog counters gate the loop each iteration | feature-flag the loop; default off → exact current per-decision behavior |
| P3 settler continues past failure | masking a real terminal blocker as recoverable | cap at `neko_recovery_attempt_cap`; require environment-fingerprint change to retry; stop with exact reason otherwise | revert to unconditional `action_failed` stop |
| all | broad behavior change without live proof | each substage is independently flagged and unit-tested; re-run Stage 47 Case 1 (no-op) then Case 4 (cross-stack) real-token smoke and compare run/incident/token counts before promoting | flags off restore current behavior |

Evidence preservation: no runtime artifact is hard-deleted; all 24 archive batches and 25
incidents remain. The audit scratch dir `.audit_tmp/` is removed and never committed.

## 10. Direct Answer To "What Should Change?"

- **Tick rate:** no. Not the bottleneck.
- **`max_actions_per_tick`:** no. Leave at 1; the fix lives inside one action.
- **Run lifecycle:** **yes — primary.** Add the continuous role-session loop (P2) and make
  the settler resilient to a single invalid output (P3).
- **Contract/redaction robustness:** **yes — first and highest leverage.** P1 is the single
  biggest token/incident win and must precede P2.
- **Prompts/skills:** minor. P4/P5 (carry repair context; nudge instead of kill). Not a
  substitute for the deterministic fixes; Stage 47 explicitly forbids prompt text as a
  replacement for gates.

So: **run lifecycle + contract robustness + settler resilience (RC1–RC3) together**, with
prompt/skill economy as a small follow-on — **not** tick rate and **not**
`max_actions_per_tick`.

## 11. Acceptance Checklist (feeds Stage 47 §47F patch loop and Acceptance Checklist)

- [ ] P1: unknown keys in delivery/qa_review packets are tolerated with an `operator_note`;
      bare secret-vocabulary prose is masked, not fatal; real secret/path shapes still fatal.
- [ ] P2: two consecutive same-persona/same-stage valid decisions execute in one run; all
      documented boundaries still close the run.
- [ ] P3: `run_until_settled` carries a mission through one `model_invalid_output` without an
      operator re-launch; stops with an exact reason when recovery is exhausted.
- [ ] P4: repair context survives a run boundary.
- [ ] P5: read/search threshold nudges before it blocks.
- [ ] Re-run Stage 47 Case 1 (no-op) and Case 4 (cross-stack) real-token smokes; record
      run-count, incident-count, and token deltas vs. the `task_737ab93c` baseline
      (29 runs / 12 incidents / 1.44M tokens) in `47-aaa-burn-in-results.md`.
- [ ] Full `tests/agent_runtime/` suite green.
- [ ] No hard-deleted runtime evidence; commits recorded.

## 12. Why No Code Was Changed In This Pass

This is an independent audit; its deliverable is this document. RC1–RC3 are architectural
and change validation/run-lifecycle semantics, so per Stage 47's patch discipline they
warrant their own flagged, unit-tested substages with before/after real-token burn-in — not
a drive-by edit during evidence collection. P1 is now staged as Stage 47 §47A0R (the
root-cause prerequisite to the §47A0 continuous loop); the continuous loop (P2) is Stage 47
§47A0. Code implementation of §47A0R can begin on request.
