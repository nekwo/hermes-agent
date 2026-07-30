# 06 — Recursive Agent-Supervised Execution (implementation-ready)

> **2026-07-30 — describes a removed subsystem.** The supervised-execution
> machinery this doc designed and verified (`liveness.py`, `no_freeze_monitor.py`,
> `supervision.py`, `recovery.py`, the ticker, hierarchical budgets, steering
> fanout limits) was removed by
> [16 — Mission Lane Removal](16-mission-lane-removal.md) S5–S7. Retained as a
> design record for its audit ledger, resolved design decisions, and independent
> verification appendix. Do not implement from it.

Status: **implemented + independently verified** (2026-07-03, v3.1 — AS0–AS7 landed on
branch `recursive-agent-supervised-execution`, commits `60bec0008`…`bbefde6ea` + the v3.1
verification commit; suite 1290 collected / green; the §6 verification appendix records
the independent audit of that implementation, the two closed post-implementation gaps
— in-flight tool-wait grace, `repo_land_lock` consumer honesty — and the real-daemon
liveness proof. v3 closed all design gaps pre-implementation: §0 is the v2 code-anchor
audit, §2.5 D1–D7 are the resolved design decisions. The end-to-end implementation prompt
lives at `06-implementation-prompt.md`.) The target execution model: the harness stops *ticking* worker turns and
becomes the incorruptible **substrate**; agents become **recursive supervisors** — each node
schedules and watches only its **direct** children and reports a distilled summary upward.
This is how the scheduler gets smart: scheduling becomes a distributed judgment made locally
by the agent closest to the work, not a central tick loop, while the harness guarantees no
node can lie, stall silently, or overspend.

Companions: `01-architecture.md` (entities/graph), `02-execution-engine.md` (blueprint
engine), `04-decision-hud-simplification-map.md` (the "harness reads the work, not a form"
reframe + **§S2 steer action surface** + **§S3 spawn/return-to-parent continuity** + the
**H1–H10** production-hardening envelope), `05-runtime-data-enterprise-storage.md`
(**v2: RD0–RD3 shipped, RD4 next** — the store-isolation dependency picture below reflects
that). Naming note: `continuity.py:30` calls `return_summary_to_parent_session` "the R3
continuity primitive" — that's the historical session name for what doc-04 ships as §S3;
this doc says **S2/S3** and reserves **R1–R4** for doc-03's retirement ledger (all ✅).

Grounding: synthesis of a 2026-07-03 design session that also produced the goal-flow
efficiency fixes (dev-jumping-dependency `693ec7460`, backend worktree-junction `b6823d46b`,
persona-instance reap `028ed4516`, assignment-release `e7fba7c61`) and doc 05. Those fixes
patch the *sequential* tick model; this doc is where that model is superseded.

---

## 0. Audit ledger (what the v2 pass verified / corrected)

Verified anchors (exact, current):

| Claim | Anchor |
|---|---|
| Serial engine: one action at a time | `ticker.py:230` `run_until_settled`, `:246` while-loop; `daemon.py:121` calls it with `max_actions=10` |
| Passive 900s TTL is the only *live* backstop | `runtime_config.py:123` `heartbeat_ttl_seconds: int = 900`; `recovery.py:12` `mark_stale_runs` → `stale_run` incident; swept at `ticker.py:124,242` |
| Run heartbeat plumbing exists | `store.py:656` `RunStore.heartbeat`; `progress.py:65` bumps `run.last_heartbeat_at` on every recorded progress; `worker_sessions.py` bumps `last_heartbeat_at` on tool/decision activity |
| Byte-offset event reader (doc-05 RD3) **shipped** | `events.py:50` `EventLog.iter_from_offset`; watermark in `parity.py` (`events_watermark`) |
| Event types are registry-enforced, payload-capped | `events.py:21` rejects unregistered types; `EVENT_PAYLOAD_LIMIT_BYTES = 4096` (`events.py:14`); contracts live in `decision_contract_registry.py` (~line 1212 `run.progress`, `:1272` `worker_session.watchdog_warning`) |
| Proof commands are ALREADY hang-proof | `proof_runner.py:880` `_run_bounded_process`: hard `timeout_seconds`, 10s heartbeat callbacks (`PROOF_MONITOR_HEARTBEAT_SECONDS`, `:52`), process-tree kill (`:970`) |
| A freeze classifier already exists — but burn-in-only | `no_freeze_monitor.py:27` `classify_freezes` (`active_run_heartbeat_stale_seconds=120`, `no_progress_seconds=300`, `run_stalled`/`runtime_freeze` findings→incidents, redaction-safe); sole caller `burn_in.py:159` — never runs during live goals |
| Cancel primitive exists | `RunStore.cancel(run_id, reason=…)` — used at `ticker.py:836,912,1307` |
| Lineage fields exist | `spawned_by`/`returned_to` on persona instances, projected at `snapshot.py:1825-1826`; topology edges from `runtime_spawned_by` (`snapshot.py:1727-1783`) |
| Return-summary primitive exists | `continuity.py:17` `return_summary_to_parent_session` — `SUMMARY_LIMIT=1200`, `REF_LIMIT=8`, writes parent SessionDB row + emits `steer.returned` |
| Fan-out/depth caps exist | `steering.py:177` `fanout_limits{max_children_per_steerer, max_depth}` (`DEFAULT_MAX_CHILDREN_PER_STEERER`, `DEFAULT_MAX_STEER_DEPTH`) |
| Authoritative gate + tamper check | `ticker.py:1768` `_build_authoritative_stage_gate_decision`, `:1391` `_collect_command_proof`, `:1978` `_handoff_diff_weakens_tests`, applied at handoff `:993-1010` |
| Two-tier budget config exists (dormant) | `runtime_config.py:104-109` `max_active_lanes=2`, `global_token_hard_limit=3_000_000`, `per_lane_token_limit=1_000_000`; `swarm_budget_exceeded` contract (`decision_contract_registry.py:1217`); surfaced by `status.py:182` `_swarm_budget_summary` |
| Single-action scheduler | `state_machine.py:53` `MissionStateMachine.next_action(mission) -> HarnessAction` (singular); strict `depends_on` dispatch via `_first_unpassed_blueprint_dependency` (`693ec7460`) |
| Deploy-failure class | `agent_already_assigned` raised at `persona_assignments.py:841` (retryable:false); release-on-terminal fixed by `e7fba7c61` |
| Certification plumbing exists | `burn_in.py:325` `swarm_certification_allows_production`; `production_envelope.py` per-area entries with real-controls copy (the H5 lesson) |
| Suite size | **1262** collected (`python -m pytest tests/agent_runtime --collect-only -q`, 2026-07-03) — the no-regress floor below is ≥1262, not v1's ≥1257 |
| Per-run worktree isolation is generic and LIVE | `repo_context.py:87` `isolated_repo_context_for_run` (worktree token = repo\|task\|run); called for every grounded persona run (`persona_runtime.py:111`) and for gate proof re-runs (`ticker.py:3788` `_proof` suffix); baseline capture + GC exist. Doc-04 H1/H2 are substantially landed |
| Store write-lock coverage (AS5 audit, executed) | TaskStore writes under `task_lock` (`store.py:111,145`), RunStore under `run_lock` (`:639,683,737`), worker sessions under `worker_session_lock`, events single-appender under `events_lock`. **Lockless:** `ProofStore.attach` (`store.py:887`) and `IncidentStore.open` (`:1203`) — safe-by-uniqueness (each write is a fresh uuid-keyed path), but `IncidentStore.close` (`:1236`) mutates an existing file in place |
| Windows lock semantics are FAIL-FAST | `locks.py:32` uses `msvcrt.locking(LK_NBLCK)` → contention raises `HarnessLockUnavailable` instead of waiting (POSIX `flock` blocks). Fine while one engine process serializes everything under `tick_lock`; **lethal under concurrent lanes** — a contended event append would crash a lane, not queue it |
| `swarm.enabled` today gates ONLY the budget check | `ticker.py:2519-2534` — with the flag off, the global swarm token ceiling is not even evaluated (per-run + mission ceilings still are, `:2505`). It is a budget flag, not a concurrency flag; AS5 gives it its real meaning |

Corrections from v1 (design-relevant, not cosmetic):

1. **Doc-05 moved under us.** RD0–RD3 are shipped (doc-05 is v2). AS0 must *use*
   `iter_from_offset` + `events_watermark` as fact, not "if present." AS5's dependency is
   no longer "doc-05 RD2" wholesale — the read model exists; what remains for AS5 is
   **write-path lane isolation** (see AS5 preconditions).
2. **AS0 cannot be only "between actions."** `run_until_settled` is synchronous and
   single-threaded; a persona turn or in-turn tool call that hangs never returns control,
   so a between-actions hook fires exactly never during the hang. The probe MUST run
   out-of-band — a daemon watchdog **thread** (precedent: the daemon already runs a
   `_heartbeat_loop` thread, `daemon.py:95-97`).
3. **The raw-command hang gap is narrower than v1 claimed.** Harness-side proof commands
   already run under `_run_bounded_process` (hard timeout + heartbeat + tree-kill). The
   real remaining lanes are (a) worker **in-session tool commands** (the persona's own
   terminal tool) and (b) any subprocess call not routed through `_run_bounded_process`.
   AS0's command deliverable is an audit-and-route, not a new mechanism.
4. **Don't build a second freeze brain.** `no_freeze_monitor.py` already classifies
   stalls with tested thresholds and redaction-safe findings but only runs during burn-in.
   AS0 = give that class of detection a live cadence + active remediation, reusing its
   finding/incident shapes (`runtime_freeze` kind), not a parallel `liveness` taxonomy.

---

## 1. The problems it solves

| # | Problem (observed 2026-07-03 session) | Why the tick model can't fix it |
|---|---|---|
| P1 | **Serial execution.** `TickEngine.run_until_settled` runs one action at a time (`ticker.py:246` while-loop); `swarm.enabled=False`. Two *independent* stages (two frontends, backend + docs) can never overlap — pure wasted wall-clock. | The engine ticks; it has no notion of concurrent lanes beyond the dormant `max_active_lanes=2`. |
| P2 | **Indefinite hangs / "the AI sleeps and the command hangs."** A worker turn (or an in-session tool command) can stall with no progress; the only backstop **on the live path** is the passive **900s** heartbeat TTL (`mark_stale_runs`). The existing freeze classifier (`no_freeze_monitor.py`, 120s/300s thresholds) never runs outside burn-in. 15 minutes of silence before anything notices. | TTL expiry is passive and coarse; the classifier has no live cadence; and nothing can fire mid-turn because the engine is synchronous. |
| P3 | **Silent deploy failures / "N agents fail to deploy."** Best match this session: `agent_already_assigned` (`persona_assignments.py:841`, retryable:false) — personas silently failed to get a slot because stale terminal goals held their assignments (released now at `e7fba7c61`, but the *class* is "an agent fails to start and nothing shouts"). | A central scheduler that dispatches-and-forgets doesn't verify each child actually came up. |
| P4 | **Central-supervisor context explosion.** If one node (Neko) must track every descendant, its context grows O(whole tree). | Flat supervision doesn't scale past a couple of agents. |
| P5 | **Trust-by-claim.** A worker saying "done" is not proof; three levels deep, a plausible-but-wrong subtree summary is invisible to the top. | The authoritative gate (`ticker.py:993`) fires at the top boundary today; not recursive. |

Definition of done: independent work runs concurrently under a hard lane/budget cap;
every worker and command is actively liveness-checked on a 30s–2m cadence with an
escalation floor; every child's deploy and every handoff is *verified*, not assumed;
each node holds only its direct children's distilled state; and all of it is
event-sourced so a crash rebuilds (doc-04 H8).

---

## 2. The core reframe: substrate vs supervisor

```
        SUPERVISOR PLANE (agents, recursive, local judgment)
   Neko ── watches ONLY ── {Backend lead, Frontend lead}
              each of those ── watches ONLY ── its own direct children
              …distilled summaries aggregate UPWARD (S3 return_summary)…

        SUBSTRATE PLANE (harness, incorruptible, applied PER NODE)
   • gate every handoff boundary (re-run authoritative proof — no trust-by-claim)
   • watchdog every level (active liveness probe + escalate-on-silence)
   • budget hierarchically (parent sub-allocates from a global ceiling)
   • event-source everything (spawn / check / return / gate → replayable)
```

The scheduler does NOT get smarter by adding logic to one central place. It gets smarter by
**distributing** the judgment to every node (each agent schedules its own direct children
with its own context) and having the harness make that delegation *safe*. A node owns
**dispatch + supervise**; it does NOT tick its children's internal turns (the child runs
itself). The harness owns the invariants that a self-interested or negligent node could
otherwise violate.

**What stays harness-owned, applied per node (not central, not delegated away):**
1. **Recursive gate.** When any child reports "done" to its parent, the harness re-runs the
   authoritative gate on that child's actual diff/trace (`_build_authoritative_stage_gate_decision`
   + `_collect_command_proof`). The parent trusts the *gate*, not the child's word. Trust is
   verified at every boundary (the doc-04 reframe, made recursive).
2. **Per-level watchdog.** Each parent actively liveness-checks its direct children; a silent
   child escalates to its parent regardless of whether the parent thought to look, and a
   stalled subtree bubbles up as `blocked` level by level.
3. **Hierarchical budget.** Spend is a global pool; a parent hands each child a *sub-budget*.
   The swarm config already models both tiers (`runtime_config.py:104-109`:
   `global_token_hard_limit` + `per_lane_token_limit`).
4. **Durability.** Every spawn / check / return / gate is an event; the tree rebuilds from the
   log at any watermark (doc-04 H8; `iter_from_offset` + `events_watermark` are shipped).

Fallback: the current serial `TickEngine` remains the **degenerate 1-lane, depth-0** case —
this model is a superset, shipped behind flags, with the tick loop as rollback.

---

## 2.5 Resolved design decisions (v3 — no open questions left for the implementer)

**D1 — Node identity.** A supervision node IS a persona instance (`persona_instance_id`).
The root node of a mission is its `neko_supervisor` instance; stage-slot runs map to nodes
through the existing assignment machinery (`persona_assignments.py`). The parent pointer is
the existing `spawned_by` field; nothing new is persisted for identity. Canonicalize with
`_canonical_persona_id`, never `safe_assignment_token` (the known identity-mangling trap).

**D2 — Parent wake mechanism (AS1).** Every `child.*` event carries `parent_node_id`.
Each persona instance gains one integer field, `child_events_offset` — the event-log byte
offset up to which the parent has consumed its children's events. "Unconsumed
`child.returned`/`child.blocked` events exist past a parent's offset" becomes a first-class
dispatch reason in `MissionStateMachine`: the engine opens a parent turn for exactly that
reason, injects the pending child events (distilled form) into the parent's context, and
advances the offset when the turn commits. No new store, no busy-poll: the scheduler check
is one `iter_from_offset(child_events_offset)` scan, and a healthy child (only throttled
`child.progress`) never triggers a parent turn — progress feeds the AS0 probe, not the
parent's model.

**D3 — Flag map (all new behavior is opt-out-able; rollback = flags off = today's engine).**
- `liveness_enabled: bool = True` — AS0. Default ON once shipped: it only observes,
  incidents route through existing Neko adjudication, and the 900s TTL remains beneath it.
- `supervision.child_events_enabled: bool = False` — AS1.
- `supervision.recursive_enabled: bool = False` — AS2+AS3 (one flag: AS2 must never run
  without AS3, so they share it).
- `supervision.hierarchical_budget_enabled: bool = False` — AS4.
- `swarm.enabled` (existing) — AS5 makes it mean real concurrency; it stays behind
  `swarm.requires_certification` + `burn_in.swarm_certification_allows_production`.
- `supervision.deploy_verification_enabled: bool = False` — AS6.
All follow the 3-file config pattern; `production_envelope.py` copy must state gated-off
status honestly (the H5 lesson).

**D4 — Event-bloat policy.** `events.jsonl` is append-only and already tens of MB.
`liveness.poll` is emitted **only on classification change** (advancing→quiet,
quiet→hung, …) plus one heartbeat summary every 10th poll per run — never one event per
poll per run. `child.progress` is throttled harness-side to at most one event per
`child_progress_min_interval_seconds` (default 30) per child; excess updates fold into the
next emission.

**D5 — Lane concurrency model (AS5).** Lanes are **threads inside the single engine
process** that already holds `tick_lock` — one process, N lane threads, each running one
persona turn end-to-end. Cross-process exclusion stays exactly as today (`tick_lock`).
Because per-entity file locks are then contended *between threads of one process*, and
Windows `_file_lock` is fail-fast (audit row above), AS5 must first change lock
acquisition to **bounded blocking retry** (spin on `HarnessLockUnavailable` with short
sleep, `lock_acquire_timeout_seconds` default 15, then raise) — a one-function change in
`locks.py:_file_lock` + tests. Two additional serialization points: (a) **land-on-handoff**
— applying a lane's worktree diff back to the shared source root serializes under a new
`repo_land_lock(source_root)`; (b) `IncidentStore.close` takes a per-incident lock (it
mutates in place). Everything else is already per-run/per-task isolated (worktrees are
per-run; store locks are per-entity).

**D6 — Defaults for new knobs.** `liveness_poll_seconds=60` (clamp 30–120),
`liveness_quiet_strikes=2`, `liveness_hung_seconds=300`, `child_progress_min_interval_seconds=30`,
`deploy_timeout_seconds=120`, `lock_acquire_timeout_seconds=15`. All validated `_positive`
in `migrations.py`; `liveness_hung_seconds` must validate `< heartbeat_ttl_seconds`.

**D7 — What "cancel/re-dispatch" means for a hung run (AS0).** The probe calls
`RunStore.cancel(run_id, reason="liveness_hung")`, closes the worker session via the
existing `update_after_run` path (close_reason `liveness_hung`, `count_decision=False`),
and opens the `run_hung` incident linked into `task.open_incident_ids` — which makes
`MissionStateMachine.next_action` route Neko adjudication on the very next action
(`state_machine.py:67-70`). The probe itself never re-dispatches; Neko (or operator
`task unblock`) decides. One incident per `(kind, run_id)` — dedupe on open, like
`recovery.py:18`.

---

## 3. Stages (each independently shippable; flag-gated; do not half-build the gate/watchdog)

Sequencing rationale: **AS0 (liveness watchdog) ships first and alone** — it fixes P2 (hangs)
and surfaces P3 (silent deploy failures) even in today's serial model, no new architecture.
AS1–AS4 build the recursive supervisor contract on the still-serial engine. AS5 turns on real
concurrency (write-path isolation preconditions below). AS6–AS7 harden + certify. No stage
regresses doc-04 H1–H10, doc-03 R1–R4, or the **≥1262**-test suite.

Cross-cutting implementation rules (apply to every stage):

- **New config keys follow the 3-file pattern:** dataclass field + default in
  `runtime_config.py`, parse in `config.py`, `_positive`/range validation in
  `migrations.py` (see `heartbeat_ttl_seconds` and the `SwarmConfig` block for the shape).
- **New event types must be registered** in `decision_contract_registry.py`
  (`EventContract` with required/optional keys) or `EventLog.append` raises; payloads are
  hard-capped at 4096 bytes — child summaries reuse `continuity.py` bounds
  (`SUMMARY_LIMIT=1200`, `REF_LIMIT=8`), which fit.
- **New incident kinds** follow `recovery.py`/`no_freeze_monitor.py`: open via
  `IncidentStore.open(Incident(kind=…, summary=…))`, dedupe on open `(kind, run_id)`,
  link into `task.open_incident_ids` so `MissionStateMachine.next_action` routes Neko
  adjudication (`state_machine.py:67-70`) for free.
- **Redaction:** any text that can reach an event/incident/summary goes through the
  existing `safe_assignment_text` / `_safe_text`-style sanitizers; never raw paths/secrets.

---

### AS0 — Active liveness watchdog (kills indefinite hangs) — SHIP FIRST

The immediate, standalone win. Today the live path's only backstop is the passive 900s
heartbeat TTL; the tested freeze classifier (`no_freeze_monitor.classify_freezes`) runs
only during burn-in. AS0 gives it a live cadence, makes it **active** (remediates, not
just records), and closes the in-turn hang blind spot.

**Architecture constraint (v2):** the engine is synchronous — a hung persona turn never
returns control to `run_until_settled`. The probe therefore runs on a **daemon watchdog
thread** (sibling to the existing `_heartbeat_loop` thread, `daemon.py:95-97`), reading
stores/event-log only (both are file-lock safe for readers), never calling the engine.
Manual/non-daemon runs get the same checks best-effort between actions; the thread is the
guarantee.

**Deliverables (hermes)**
- `agent_runtime/liveness.py`: `LivenessProbe` — on `liveness_poll_seconds` (default 60,
  clamp 30–120), for each active run:
  - read `run.last_heartbeat_at` (bumped by `progress.py:65` + `worker_sessions.py` on real
    activity) and the run's new events since its last watermark via
    `EventLog.iter_from_offset` (`events.py:50`) — cheap by construction: byte-seek + tail,
    **no model call, no snapshot build**;
  - classify **advancing** (fresh heartbeat/events → reset strikes), **quiet** (silent for
    `liveness_quiet_strikes × poll` → emit `run.liveness.warning`, increment the existing
    `watchdog_warning_count` on the worker session, `worker_sessions.py:226`), **hung**
    (silent past `liveness_hung_seconds`, default 300 → open a `run_hung` incident and
    call `RunStore.cancel(run_id, reason="liveness_hung")` for graceful cancel/re-dispatch).
    5× faster than the 900s TTL and *active*, not TTL-passive.
- Classification reuses `no_freeze_monitor` shapes: emit findings in its `_finding` format,
  record via `record_freeze_findings` (proof + incident, redaction-safe) so Mission Control
  rendering and Neko adjudication work unchanged; add the `run_hung` finding kind alongside
  `run_stalled`. Do NOT fork a second threshold taxonomy — `NoFreezeThresholds` gains the
  new knobs.
- Register `run.liveness.warning` + `liveness.poll` event contracts in
  `decision_contract_registry.py`; `liveness.poll` carries the classification and is
  emitted per the D4 bloat policy (classification change + every-10th-poll heartbeat only).
- Remediation per D7: cancel run + close worker session + `run_hung` incident linked into
  `task.open_incident_ids` → Neko adjudicates; the probe never re-dispatches itself.
- Config (3-file pattern, D6): `liveness_enabled=True`, `liveness_poll_seconds=60`,
  `liveness_quiet_strikes=2`, `liveness_hung_seconds=300` (validated
  `< heartbeat_ttl_seconds`). Keep `heartbeat_ttl_seconds=900` + `mark_stale_runs` as the
  final coarse floor.
- Wire: daemon watchdog thread (primary); a between-actions call in `run_until_settled`
  (secondary, for manual ticks); daemon `status` surfaces last-poll ts + open liveness
  incidents.

**Deliverables (command liveness — the CMD-hang case)**
- Proof commands are already bounded (`_run_bounded_process`, `proof_runner.py:880`: hard
  timeout, 10s heartbeat callbacks, `_terminate_process_tree`). The gap is the **worker's
  in-session terminal tool** and any subprocess path not using it. Deliverable: audit every
  harness-spawned subprocess (`rg "subprocess" agent_runtime hermes_cli tools`), route
  worker tool commands through `_run_bounded_process` semantics (timeout + heartbeat
  progress emits that bump `run.last_heartbeat_at`), and on breach open `command_hung` +
  terminate + record — instead of hanging the parent turn indefinitely. A tool command
  emitting 10s heartbeats also feeds the probe's "advancing" signal for free.

**Tests** `tests/agent_runtime/test_liveness.py`: advancing resets strikes; quiet warns
(`run.liveness.warning` + `watchdog_warning_count` bump); hung opens `run_hung` and cancels
the run; probe does zero model calls and zero snapshot builds; a synthetic never-exits
command is caught within `liveness_hung_seconds`; thresholds live in `NoFreezeThresholds`;
config keys validated by `migrations.py`.

**Proof** a live goal with an artificially stalled worker is caught in ≤ `liveness_hung_seconds`
(not 900s), the incident routes to Neko, and the goal recovers. Suite ≥ 1262.

**Handoff prompt** "In hermes-agent, implement 06 §AS0 (v2): `agent_runtime/liveness.py`
`LivenessProbe` on a daemon watchdog THREAD (sibling of `_heartbeat_loop`, daemon.py:95 —
the engine is synchronous, between-actions hooks can't catch mid-turn hangs). Cadence
30–120s (`liveness_poll_seconds=60`); advancing/quiet/hung off `run.last_heartbeat_at` +
`EventLog.iter_from_offset`; NO model calls/snapshot builds. Reuse `no_freeze_monitor`
finding/incident shapes (`record_freeze_findings`, add `run_hung` kind, extend
`NoFreezeThresholds`); register `run.liveness.warning`/`liveness.poll` contracts; cancel
via `RunStore.cancel`. Route worker in-session tool commands through
`_run_bounded_process` semantics → `command_hung` on breach (proof commands are already
bounded). Config via the runtime_config/config/migrations 3-file pattern; 900s TTL stays
as backstop. Tests per spec; live stalled-worker proof caught ≤300s; suite ≥ 1262. Check
your brain first."

---

### AS1 — Honest status-notification contract for children

Children must *emit*, not wait to be *polled at* — a child announces `progress` / `blocked` /
`returned` so the watchdog and the parent are event-driven, not busy-polling.

**Deliverables (hermes)** extend the packet/event contract: every child run emits bounded,
redaction-safe `child.progress` (throttled), `child.blocked{reason}`, `child.returned{summary,
proof_ids, artifact_refs}`. `child.returned` IS the `return_summary_to_parent_session`
payload (`continuity.py:17` — `SUMMARY_LIMIT=1200`, `REF_LIMIT=8`; it already writes the
parent SessionDB row and emits `steer.returned` — extend that emission rather than adding a
parallel one). `child.progress` piggybacks the existing `run.progress` contract
(`decision_contract_registry.py:1212`) with a `parent_node_id`; all three register in the
contract registry and respect the 4096-byte payload cap; `child.progress` is throttled per
D4 (`child_progress_min_interval_seconds=30`). Parent wake is the D2 inbox: every `child.*`
event carries `parent_node_id`; each persona instance gains `child_events_offset`;
unconsumed `child.returned`/`child.blocked` past that offset become a first-class
`MissionStateMachine` dispatch reason that opens the parent's turn with the distilled
events injected, advancing the offset on commit. AS0's probe treats a fresh
`child.progress` as "advancing." "Check as often as it needs" becomes **wake-on-event**,
not fixed-tick — a model turn is spent only when a child returns or blocks, never to poll
a healthy child. Flag: `supervision.child_events_enabled` (D3, default off).

**Tests** child events are bounded + redaction-safe + registry-valid; a parent wakes on
`child.returned`, not on a timer; a healthy child costs the parent 0 model turns;
`child.returned` and `steer.returned`/SessionDB post-back stay consistent (one write path).

**Handoff prompt** "Implement 06 §AS1: bounded redaction-safe `child.progress/blocked/returned`
events — `returned` = the `return_summary_to_parent_session` shape (continuity.py, extend its
`steer.returned` emission, don't fork it); `progress` extends the `run.progress` contract;
register all in `decision_contract_registry`, ≤4096B payloads. Parents wake on them
(event-driven, not busy-poll); AS0's probe consumes progress as liveness. Tests per spec.
Check your brain first."

---

### AS2 — Recursive supervision (each node watches ONLY its direct children)

**Deliverables (hermes)** `agent_runtime/supervision.py`: a node's supervisory scope is its
`spawned_by`-direct children only (`snapshot.py:1825` already projects `spawned_by` +
`returned_to`; topology edges from `runtime_spawned_by`, `snapshot.py:1727-1783`). A parent's
context is assembled from its direct children's *distilled summaries* (the S3 return shape),
never their transcripts — context stays O(direct children). Depth + fan-out caps per node:
reuse the steering executor's `fanout_limits` (`steering.py:177` —
`DEFAULT_MAX_CHILDREN_PER_STEERER`, `DEFAULT_MAX_STEER_DEPTH`), enforced harness-side, cap
hits logged (doc-04 S6 bar: no silent truncation). A stalled or blocked child aggregates
upward one level at a time (`child.blocked` → parent decides → if unresolved, parent emits
its own `child.blocked` to *its* parent).

**Tests** parent context excludes grandchildren transcripts; a leaf block propagates to the
root through each level; fan-out/depth caps hold and log when hit; lineage
(`spawned_by`/`returned_to`) is complete tree-wide.

**Handoff prompt** "Implement 06 §AS2: recursive supervision scoped to `spawned_by`-direct
children (snapshot.py already projects lineage); parent context = children's distilled S3
summaries (O(direct children), never grandchild transcripts); per-node fan-out/depth caps
reusing steering.py `fanout_limits`, logged on hit; upward block aggregation one level at a
time. Tests per spec. Check your brain first."

---

### AS3 — Per-boundary recursive gate (no trust-by-claim, at every level)

**Deliverables (hermes)** the authoritative gate (`_build_authoritative_stage_gate_decision`
ticker.py:1768 + `_collect_command_proof` ticker.py:1391, already harness-executed at the
top-level handoff, ticker.py:993-1010) fires at **every** handoff boundary in the tree, not
just the top. A parent accepts a child's `returned` iff the harness-re-run gate on that
child's isolated diff passes; a failed gate makes the child `needs_fixes` to its parent,
never silently `done`. `_handoff_diff_weakens_tests` (ticker.py:1978) tamper-check applies
per level. Precondition per doc-04 H1/H2: the child's diff must be *its own* (worktree /
baseline attribution) or the gate is judging someone else's dirt — gate + isolation land
together for spawned children.

**Tests** a child that reports done with a failing/tampered diff is rejected by its parent's
gate; a genuinely-passing child is accepted; recursion holds 2+ levels deep; the gated diff
is attributable to the gated child alone.

**Handoff prompt** "Implement 06 §AS3: re-run the harness authoritative gate
(`_build_authoritative_stage_gate_decision`/`_collect_command_proof`) at EVERY handoff
boundary (recursive), parent trusts the gate not the child's claim,
`_handoff_diff_weakens_tests` per level, child diff isolated/attributable (doc-04 H1/H2).
Tests 2+ levels deep. Check your brain first."

---

### AS4 — Hierarchical budget allocation

**Deliverables (hermes)** a parent sub-allocates from a global ceiling to each child
(`swarm.global_token_hard_limit` → per-child `per_lane_token_limit`-style sub-budgets;
config tiers already exist at `runtime_config.py:104-109` and are surfaced by
`status.py:182` `_swarm_budget_summary`); a child that exhausts its allocation blocks up to
its parent (which may re-allocate or escalate), never silently overspends the global pool.
Emit the existing `swarm_budget_exceeded` contract (`decision_contract_registry.py:1217`)
on ceiling hits; reuse the per-run + mission ceilings that doc-04 H7 verified
(`run_budget_exceeded` lane).

**Tests** child over-allocation blocks up, not out; the global ceiling is never exceeded even
with a deep tree; re-allocation path works; `swarm_budget_exceeded` emitted with
`total_tokens`/`limit` per contract.

**Handoff prompt** "Implement 06 §AS4: hierarchical budget — parent sub-allocates the
existing `swarm.global_token_hard_limit` per child (per_lane-style); child exhaustion blocks
upward; global cap never exceeded; emit `swarm_budget_exceeded` per its registered contract.
Tests per spec. Check your brain first."

---

### AS5 — Concurrent lanes (real parallelism)

**Preconditions (v3 — the audits were executed; all three are now known-state, not homework):**
1. ✅ Read path: transactional `read_model.db` + incremental projector exist (doc-05 v2).
2. ✅ Store write locks per entity exist and are used on every hot mutation path (audit row
   in §0): tasks, runs, worker sessions, events. `ProofStore.attach`/`IncidentStore.open`
   are lockless but safe-by-uniqueness (fresh uuid-keyed paths).
3. ✅ Repo isolation between lanes already exists: per-run worktrees are generic and live
   for every grounded persona run (`persona_runtime.py:111`) and gate proof re-runs
   (`ticker.py:3788`).

**Deltas AS5 must ship (from the audit, per D5):**
- `locks.py:_file_lock` bounded blocking retry (Windows is fail-fast today; a contended
  lane must wait ≤ `lock_acquire_timeout_seconds=15`, then raise) + contention tests.
- `repo_land_lock(source_root)` — serialize applying a lane's worktree diff back to the
  shared source root on handoff.
- Per-incident lock around `IncidentStore.close` (in-place mutation).
- Lanes = threads inside the single `tick_lock`-holding engine process (D5); no second
  engine process, ever.

**Deliverables (hermes)** teach the scheduler to dispatch the **set** of ready-and-independent
stages (all `depends_on` satisfied — strict dependency dispatch already exists,
`_first_unpassed_blueprint_dependency`, `693ec7460`), capped by `max_active_lanes`:
`MissionStateMachine.next_action` (state_machine.py:53) gains a `next_actions(mission) ->
list[HarnessAction]` ready-set form; `run_until_settled` opens a lane thread per action;
`daemon.py:121` consumes the same loop. `depends_on` still gates dependents (backend→frontend
stays serial; frontendA ∥ frontendB overlap). `swarm.enabled` becomes real — today it only
gates the global budget check (`ticker.py:2519`) — and stays behind
`burn_in.swarm_certification_allows_production` (burn_in.py:325).

**Tests** a fan-out blueprint (two independent stages sharing only `scope`) runs both lanes with
**overlapping run windows** in the event log; dependent stages stay serial; concurrent
writes hit no lock violations / lost updates (hammer TaskStore/RunStore/EventLog from two
lane threads on Windows semantics); land-on-handoff serializes under `repo_land_lock`;
lock-retry timeout raises cleanly.

**Handoff prompt** "Implement 06 §AS5 per §2.5 D5 (preconditions all verified-met; read
model, per-entity locks, per-run worktrees exist): FIRST the lock deltas —
`locks.py:_file_lock` bounded blocking retry (`lock_acquire_timeout_seconds=15`),
`repo_land_lock(source_root)` for handoff land, per-incident close lock. THEN `next_actions`
ready-set on MissionStateMachine capped by `max_active_lanes`; `run_until_settled`/daemon
run them on concurrent lane THREADS inside the one tick_lock process; `depends_on` still
gates; `swarm.enabled` real but certification-gated. Prove with a two-independent-stage
blueprint showing overlapping run windows in the event log, zero lock violations under a
two-thread store hammer on Windows. Check your brain first."

---

### AS6 — Deploy reliability (kills silent "N agents fail to deploy")

**Deliverables (hermes)** every spawn is *verified up*: a child that fails to acquire its
slot/worktree/assignment within `deploy_timeout_seconds` (default 120, 3-file pattern, D6;
flag `supervision.deploy_verification_enabled`, D3)
emits `child.deploy_failed{reason}` to its parent (not a silent `agent_already_assigned`
swallow — `persona_assignments.py:841`; the `e7fba7c61` assignment release is the
precondition; this is the *observability + retry* on top). The parent re-dispatches or
escalates. AS0's probe treats a never-started child (run created, no first heartbeat) as
hung.

**Tests** a slot-blocked spawn surfaces `child.deploy_failed` within the timeout and is
retried/escalated, never silently dropped; the assignment-starvation class (this session's
`agent_already_assigned`) is caught, not swallowed; a never-started child trips the probe.

**Handoff prompt** "Implement 06 §AS6: verify every spawn came up within
`deploy_timeout_seconds` (new config, 3-file pattern); `child.deploy_failed` → parent
retry/escalate; never silently swallow an `agent_already_assigned`-class failure
(persona_assignments.py:841); AS0 probe catches never-started children. Tests per spec.
Check your brain first."

---

### AS7 — Certification gates

**Deliverables** perf/liveness CI (hangs caught ≤ `liveness_hung_seconds`; healthy-child parent
cost = 0 model turns); concurrency soak (N lanes deep tree, zero store corruption, zero silent
deploy drops); crash-recovery drill (kill mid-tree, rebuild from event log + watermark, subtree
resumes without double-apply); redaction-at-rest over all child summaries. Certification rides
the existing plumbing: `burn_in.swarm_certification_allows_production` (burn_in.py:325) and the
Stage 61G unattended gate. Only after 10 green runs: `production_envelope.py` entry for
`recursive_supervision` with real controls (no advertised-but-inert copy — the doc-04 H5
lesson; the envelope's existing swarm entries show the honest gated-off wording to follow).

---

## 4. Non-goals & risks
- Not a rewrite of the event log or the blueprint DSL; the tick engine stays as the degenerate
  1-lane/depth-0 fallback + rollback.
- No cloud/distributed execution; local-first, single-node, bounded by `max_active_lanes`.
- **Top risk — subtree poisoning:** a negligent supervisor or a lying child corrupts its whole
  subtree, invisible to the top. Mitigation is the entire substrate plane: AS3 (gate every
  boundary) + AS0/AS2 (watchdog + upward block aggregation every level) + AS6 (verified deploy).
  Ship none of the supervisor stages (AS2+) without AS0 + AS3 landed — delegation without the
  per-level gate/watchdog is strictly worse than the tick loop.
- **Cost risk:** supervision must be event-driven (AS1), never busy-poll; a healthy child costs
  its parent 0 model turns or the model is just an expensive tick loop.
- **Taxonomy risk (new, v2):** AS0 must extend `no_freeze_monitor`/`NoFreezeThresholds`, not
  stand up a parallel liveness vocabulary — two stall taxonomies means two half-trusted alarms.

## 5. Sequencing
AS0 (alone, immediate — fixes hangs today) → AS1 → AS2 + AS3 together (never AS2 without AS3;
one shared flag, D3) → AS4 → AS5 (preconditions verified-met; ship the D5 lock deltas first
inside the stage) → AS6 → AS7. AS0 and AS6 are the two that pay off even while the engine is
still serial (hangs + deploy failures are orthogonal to concurrency).

The end-to-end handoff prompt (all stages, one implementer) is maintained at
[`06-implementation-prompt.md`](06-implementation-prompt.md) — keep it in lockstep with this
doc.

---

## 6. Verification appendix (v3.1 — independent audit of the implementation)

AS0–AS7 were implemented on branch `recursive-agent-supervised-execution`
(`60bec0008` AS0 → `2b383f8fe` AS1 → `92dd1abc6` AS2/AS3 → `31d4fb825` AS4 →
`2edfe54bd` AS5 → `ac4179557` AS6 → `6ec1dd164` AS7 → `bbefde6ea` offset-commit +
proof-gate hardening). A second, independent audit then verified the branch against
this doc and closed two residual gaps.

**Independently verified (not taken from the implementer's report):**
- Suite: `python -m pytest tests/agent_runtime -q` → green, full count re-run
  post-audit (see status line); ~29 files changed, +2.4k lines, 8 new test files.
- AS0 wiring is real: `daemon.py` runs a `mission-daemon-liveness` thread (sibling of
  the heartbeat thread), clamped 30–120s, status-surfaced; ticker keeps a secondary
  between-actions poll. Probe is store-only (no model, no snapshot).
- Flags per D3: `SupervisionConfig` all-off defaults; `liveness_enabled=True`;
  lane concurrency requires `swarm.enabled` AND burn-in certification
  (`recursive_supervision_certification_allows_production`, 10 green unattended runs).
  `production_envelope.py` copy is honest about the gate ("blocked until ledger green").
- D-decisions honored: child-event inbox with post-turn offset commit
  (`_commit_child_event_offset`, `bbefde6ea`); child return gate is fail-closed —
  requires passed + redaction-safe + **harness-owned** proof (agent tool traces
  rejected: `supervision.child_return_gate_passed`); locks retry-bounded
  (`_file_lock` deadline + 50ms spin); `incident_lock` wired into
  `IncidentStore.close`; migrations validate D6 (`liveness_hung_seconds <
  heartbeat_ttl_seconds`, poll clamp 30–120).

**Gaps found by the audit and closed in v3.1:**
1. **False-positive hang cancel on long quiet tools (real bug, fixed in code).**
   `run.tool.started` fires at tool start, then a long legitimate command (a 6-minute
   build) emits nothing until `finished`; with `liveness_enabled` defaulting ON the
   probe would have cancelled the run at 300s. Fix: in-flight tool-wait grace — an
   unmatched `run.tool.started` raises the hung threshold by
   `tool_wait_timeout_seconds` (default 300 → 600s total, still well under the 900s
   TTL floor). Regression test added.
2. **`repo_land_lock` had no consumer — resolved as documentation, not wiring.**
   The audit confirmed no land-on-handoff path exists anywhere in the runtime: per-run
   git worktrees share the repository object store, and the harness never writes a
   lane's diff back to the source root (diffs are consumed as proof/gate inputs).
   The lock is therefore a ready primitive whose consumer is the *future* land step;
   wiring it today would serialize nothing. If/when a land step is introduced, it MUST
   take `repo_land_lock(source_root)` — that requirement stays in §AS5.
3. **Worker in-session command routing (§AS0 deliverable) — narrower than specced,
   accepted with rationale.** The shared `tools/terminal_tool.py` already bounds
   foreground commands (`TERMINAL_TIMEOUT` default 180s, `FOREGROUND_MAX_TIMEOUT`
   cap) with background-process management, so the "unbounded worker CMD" lane the
   v1 doc feared is already bounded tool-side for persona runs.
   `run_liveness_bounded_command` is the harness-side seam (timeout + 10s heartbeat
   emits + `command_hung` incident) for harness-executed worker commands; rewiring the
   shared hermes terminal tool (used far beyond the harness) was out of proportion.
   Residual coverage for anything that slips both bounds: the AS0 probe (with tool
   grace) + the 900s TTL.

**Live proof (real daemon, no model — isolated `HERMES_AGENT_RUNTIME_ROOT`):** a run
seeded 400s heartbeat-stale under a real `MissionDaemon.run_foreground` was caught by
the liveness thread in **0.5s wall-clock** — `run_hung` incident opened, run
`cancelled`, incident linked into `task.open_incident_ids` (Neko-adjudication route).
The v1 world would have waited 900s for the passive TTL. Full-goal stall proof with a
live model remains open until a stall-injection hook exists — tracked as the honest
residual, not claimed.

**Residuals (explicitly NOT claimed as done):**
- Production enablement of recursive lanes awaits the burn-in ledger's 10 green
  unattended runs (by design — the AS7 gate is real, not advisory).
- A full model-driven stalled-goal proof needs a stall-injection hook (e.g. a test
  persona whose provider hangs); the real-daemon isolated proof above is the current
  strongest evidence.
- Daemon `_liveness_loop` reads config once at thread start; flag/threshold changes
  need a daemon restart (matches existing daemon config semantics).

### §6.1 — 07 self-drive gap audit closure (2026-07-03, second session)

The five gaps recorded by the first 2026-07-03 session (doc 07) were audited,
reproduced against main, fixed, and live-proven. Commits `0170c67c2` (gap 1 scope
guard), `9fd801c39` (gap 2 targeted daemon restore), `9d697ae1d` (gap 3 goal-named
gate commands), `8989d408c` (gap 4 adjudication incident close), `634b990ee`
(gap 5 orphan reap), `4e5f52bf1` (liveness status carry + budget-lane audit note).
Root-cause note for gap 2: live-automation state commit `5a63d0b0b` (2026-06-30)
had severed daemon targeting AND inverted the five daemon tests guarding it.

Three additional self-drive bugs surfaced during the LIVE acceptance runs and were
fixed at the root in the same session:
- `303193e9d` — a stage that declares NO required proof gate never completed when
  the auto gate had no safe command; the accepted hand_off now completes it
  (task_49f8ee3b looped backend_implementation 8x).
- `3ffab38ed` + `138fd1bbc` — default-blueprint placeholder stage repos
  (scope=hermes-agent, backend_implementation=EterniaBackend,
  implement=EterniaLauncher) leaked into `task.affected_repos` at typed-plan
  release and into gate command/workdir selection
  (`default_blueprint_placeholder_repo_override`).
- `13b19e7c0` — dev grounding used the placeholder stage repo, so the isolated
  worktree (and therefore the gate workdir) was the wrong tree
  (task_8e1e0832: goal-named pytest failed exit-4 file-not-found twice).

Live acceptance (unattended, current main):
- task_5ed6f049 "Scope contract regression audit": trap description naming
  hermes-agent only mid-sentence scoped to `['hermes-agent']`, TARGETED daemon
  (queue_mode foreground), **1m29s create-to-done**, both harness-owned gate
  proofs ran exactly the goal-named focused command (exit 0), auto-archive batch
  `20260703T102437950655Z_archive_ready`, zero incidents, zero operator actions.
- task_5008f128 chaos drill: `daemon stop` mid-turn cancelled in-flight
  `run_ec9b019f6abd` IMMEDIATELY (stop result `orphan_runs_cancelled`), targeted
  restart, **done unattended 2m26s** including the kill; archive batch
  `20260703T102849374432Z_archive_ready`. Final `harness status --json` clean.

New residuals found live (NOT fixed this session):
- **Neko plan-release loop**: after a failed authoritative gate, Neko adjudication
  emitted `mission_plan.updated` (propose_acceptance with no release_stage_id)
  every ~20s without progress (task_8e1e0832, 14:08–14:12); the no-progress guard
  does not cover this shape. Next step: fingerprint repeated no-op plan releases
  the same way repeated block decisions are fingerprinted, and settle to
  wait-on-intervention.
- Budget-approval lane (audited, no code change): the lane exists end to end via
  Neko `resolve_incident`/coerced continuation bounded by `neko_extension_cap`;
  the observed dead-end was the blocked-state-only HUD recommendation, fixed by
  `8989d408c`. A cap-exhausted incident still requires operator `task unblock`
  by design.
