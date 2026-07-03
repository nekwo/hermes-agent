# 06 — Recursive Agent-Supervised Execution (implementation-ready)

Status: **audited, implementation-ready** (2026-07-03, v2 — deep audit of the v1 proposal
against the live tree at `ff4edffcc`; every code anchor below was verified file:line, stale
claims corrected). The target execution model: the harness stops *ticking* worker turns and
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
- Register `run.liveness.warning` + `liveness.poll` (debug, bounded) event contracts in
  `decision_contract_registry.py`; `liveness.poll` carries the classification so the
  cadence is auditable.
- Config (3-file pattern): `liveness_poll_seconds=60`, `liveness_quiet_strikes=2`,
  `liveness_hung_seconds=300`. Keep `heartbeat_ttl_seconds=900` + `mark_stale_runs` as the
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
contract registry and respect the 4096-byte payload cap. Parents subscribe to their direct
children's events; AS0's probe treats a fresh `child.progress` as "advancing." "Check as often
as it needs" becomes **wake-on-event**, not fixed-tick — a model turn is spent only when a
child returns or blocks, never to poll a healthy child.

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

**Preconditions (updated by the v2 audit — doc-05 RD2/RD3 are shipped):**
1. ✅ Read path: transactional `read_model.db` + incremental projector exist (doc-05 v2).
2. ✅ Store write locks exist per entity: `locks.py` — `task_lock(task_id)`, `run_lock(run_id)`,
   `worker_session_lock`, single-appender `events_lock`. Verify lock coverage on every
   write path two lanes can share (TaskStore/RunStore/ProofStore mutations) — that audit,
   not new infrastructure, is the store precondition.
3. ⬜ **Repo isolation between lanes (doc-04 H1) is the real remaining blocker** for two
   lanes touching the same repo: per-lane git worktrees (backend worktree lane exists —
   `repo_context.py`, `worktree_events.jsonl`, hardened by `b6823d46b`; launcher-side and
   generic per-lane isolation must be verified/extended). Two lanes in *different* repos
   can ship first.

**Deliverables (hermes)** teach the scheduler to dispatch the **set** of ready-and-independent
stages (all `depends_on` satisfied — strict dependency dispatch already exists,
`_first_unpassed_blueprint_dependency`, `693ec7460`), capped by `max_active_lanes`:
`MissionStateMachine.next_action` (state_machine.py:53) gains a `next_actions(mission) ->
list[HarnessAction]` ready-set form; `run_until_settled` opens a lane per action;
`daemon.py:121` consumes the same loop. `depends_on` still gates dependents (backend→frontend
stays serial; frontendA ∥ frontendB overlap). `swarm.enabled` becomes real (and stays behind
`burn_in.swarm_certification_allows_production`, burn_in.py:325).

**Tests** a fan-out blueprint (two independent stages sharing only `scope`) runs both lanes with
**overlapping run windows** in the event log; dependent stages stay serial; concurrent
writes hit no lock violations / lost updates (hammer TaskStore/RunStore/EventLog from two
lanes); same-repo overlap is refused until the lane has worktree isolation.

**Handoff prompt** "Implement 06 §AS5 (preconditions: store-lock audit + doc-04 H1 worktree
isolation for same-repo overlap; read model already shipped): `next_actions` ready-set on
MissionStateMachine capped by `max_active_lanes`; `run_until_settled`/daemon run them on
concurrent lanes; `depends_on` still gates; `swarm.enabled` real but certification-gated.
Prove with a two-independent-stage blueprint showing overlapping run windows in the event
log, zero lock violations, same-repo overlap refused without isolation. Check your brain
first."

---

### AS6 — Deploy reliability (kills silent "N agents fail to deploy")

**Deliverables (hermes)** every spawn is *verified up*: a child that fails to acquire its
slot/worktree/assignment within `deploy_timeout_seconds` (new config key, 3-file pattern)
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
AS0 (alone, immediate — fixes hangs today) → AS1 → AS2 + AS3 together (never AS2 without AS3) →
AS4 → AS5 (after the store-lock audit + doc-04 H1 worktree isolation; the doc-05 read-model
dependency is already satisfied) → AS6 → AS7. AS0 and AS6 are the two that pay off even while
the engine is still serial (hangs + deploy failures are orthogonal to concurrency).
