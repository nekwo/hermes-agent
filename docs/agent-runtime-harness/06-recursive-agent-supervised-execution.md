# 06 — Recursive Agent-Supervised Execution (implementation-ready)

Status: **proposed** (2026-07-03, v1). The target execution model: the harness stops
*ticking* worker turns and becomes the incorruptible **substrate**; agents become
**recursive supervisors** — each node schedules and watches only its **direct** children
and reports a distilled summary upward. This is how the scheduler gets smart: scheduling
becomes a distributed judgment made locally by the agent closest to the work, not a central
tick loop, while the harness guarantees no node can lie, stall silently, or overspend.

Companions: `01-architecture.md` (entities/graph), `02-execution-engine.md` (blueprint
engine), `04-decision-hud-simplification-map.md` (the "harness reads the work, not a form"
reframe + R2 steering + R3 continuity), `05-runtime-data-enterprise-storage.md` (RD2 store
isolation, which the concurrency stage depends on).

Grounding: this is the synthesis of a 2026-07-03 design session that also produced the
goal-flow efficiency fixes (dev-jumping-dependency, backend worktree-junction, persona-
instance reap, assignment-release) and doc 05. Those fixes patch the *sequential* tick model;
this doc is where that model is superseded.

---

## 1. The problems it solves

| # | Problem (observed this session) | Why the tick model can't fix it |
|---|---|---|
| P1 | **Serial execution.** `TickEngine.run_until_settled` runs one lane at a time (`ticker.py:246` while-loop); `swarm.enabled=False`. Two *independent* stages (two frontends, backend + docs) can never overlap — pure wasted wall-clock. | The engine ticks; it has no notion of concurrent lanes beyond the dormant `max_active_lanes=2`. |
| P2 | **Indefinite hangs / "the AI sleeps and the command hangs."** A worker (or a raw CMD command) can stall with no progress; the only backstop is a passive **900s** heartbeat TTL (`mark_stale_runs`, `heartbeat_ttl_seconds` default 900). 15 minutes of silence before anything notices. | TTL expiry is passive and coarse. Nothing *actively* checks "is this still making progress" on a 30s–2m cadence. |
| P3 | **Silent deploy failures / "N agents fail to deploy."** Best match this session: `agent_already_assigned` (retryable:false) — multiple personas silently failed to get a slot because stale terminal goals held their assignments (patched at `e7fba7c61`, but the *class* is "an agent fails to start and nothing shouts"). | A central scheduler that dispatches-and-forgets doesn't verify each child actually came up. |
| P4 | **Central-supervisor context explosion.** If one node (Neko) must track every descendant, its context grows O(whole tree). | Flat supervision doesn't scale past a couple of agents. |
| P5 | **Trust-by-claim.** A worker saying "done" is not proof; three levels deep, a plausible-but-wrong subtree summary is invisible to the top. | Handled at the top boundary today; not recursive. |

Definition of done: independent work runs concurrently under a hard lane/budget cap;
every worker and command is actively liveness-checked on a 30s–2m cadence with an
escalation floor; every child's deploy and every handoff is *verified*, not assumed;
each node holds only its direct children's distilled state; and all of it is
event-sourced so a crash rebuilds (H8).

---

## 2. The core reframe: substrate vs supervisor

```
        SUPERVISOR PLANE (agents, recursive, local judgment)
   Neko ── watches ONLY ── {Backend lead, Frontend lead}
              each of those ── watches ONLY ── its own direct children
              …distilled summaries aggregate UPWARD (R3 return_summary)…

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
   authoritative gate on that child's actual diff/trace. The parent trusts the *gate*, not
   the child's word. Trust is verified at every boundary (the doc-04 reframe, made recursive).
2. **Per-level watchdog.** Each parent actively liveness-checks its direct children; a silent
   child escalates to its parent regardless of whether the parent thought to look, and a
   stalled subtree bubbles up as `blocked` level by level.
3. **Hierarchical budget.** Spend is a global pool; a parent hands each child a *sub-budget*.
   The swarm config already models both tiers (`global_token_hard_limit` + `per_lane_token_limit`).
4. **Durability.** Every spawn / check / return / gate is an event; the tree rebuilds from the
   log at any watermark (H8).

Fallback: the current serial `TickEngine` remains the **degenerate 1-lane, depth-0** case —
this model is a superset, shipped behind flags, with the tick loop as rollback.

---

## 3. Stages (each independently shippable; flag-gated; do not half-build the gate/watchdog)

Sequencing rationale: **AS0 (liveness watchdog) ships first and alone** — it fixes P2 (hangs)
and surfaces P3 (silent deploy failures) even in today's serial model, no new architecture.
AS1–AS4 build the recursive supervisor contract on the still-serial engine. AS5 turns on real
concurrency (needs doc-05 RD2 store isolation). AS6–AS7 harden + certify. No stage regresses
H1–H10, R1–R4, or the ≥1257-test suite.

---

### AS0 — Active liveness watchdog (kills indefinite hangs) — SHIP FIRST

The immediate, standalone win. Today the only backstop is a passive 900s heartbeat TTL. Add
an **active** liveness probe on a **30s–2m cadence** that is cheap by construction — a few
line greps over the run's progress log / event tail, NOT a model turn.

**Deliverables (hermes)**
- `agent_runtime/liveness.py`: `LivenessProbe` — for each active run, on `poll_interval`
  (config `liveness_poll_seconds`, default 60, min 30, max 120): read the last N progress
  lines / the event tail since the run's watermark (byte-offset reader from doc-05 RD3 if
  present, else bounded `tail`), and classify:
  - **advancing** — new progress events / tool activity since last poll → reset strike count.
  - **quiet** — no new events for `liveness_quiet_strikes × poll_interval` → emit
    `run.liveness.warning` (reuse the existing `worker_session.watchdog_warning` contract
    shape), increment `watchdog_warning_count`.
  - **hung** — quiet past `liveness_hung_seconds` (default 300) → open a `run_hung` incident,
    signal the run for graceful cancel/re-dispatch. This is 5× faster than the 900s TTL and
    *active*, not TTL-passive.
- Cheap by contract: the probe does grep/tail + timestamp math, no model call, no full
  snapshot build. Emit `liveness.poll` debug events with the classification so the cadence is
  auditable.
- Wire into the daemon loop and `run_until_settled` between actions; keep `mark_stale_runs`
  (900s) as the final coarse floor.

**Deliverables (raw-command liveness — the CMD-hang case)**
- Command execution already streams to a progress log; the same probe watches command runs:
  a shelled command with no stdout/stderr and no exit for `liveness_hung_seconds` →
  `command_hung` incident + terminate + record, instead of hanging the parent indefinitely.

**Tests** `tests/agent_runtime/test_liveness.py`: advancing resets strikes; quiet warns;
hung opens the incident and signals cancel; probe does zero model calls; a synthetic
never-exits command is caught within `liveness_hung_seconds`.

**Proof** a live goal where a worker is artificially stalled is caught in ≤ `liveness_hung_seconds`
(not 900s). Suite ≥ 1257.

**Handoff prompt** "In hermes-agent, implement 06 §AS0: `agent_runtime/liveness.py`
`LivenessProbe` (30–120s cadence, cheap grep/tail of the progress tail, NO model call),
advancing/quiet/hung classification emitting `run.liveness.warning` + `run_hung`/`command_hung`
incidents at `liveness_hung_seconds` (default 300, 5× faster than the 900s TTL floor which
stays as backstop). Wire into the daemon + run_until_settled + the command runner. Tests per
spec; a stalled-worker live proof caught in ≤300s; suite ≥ 1257. Check your brain first."

---

### AS1 — Honest status-notification contract for children

Children must *emit*, not wait to be *polled at* — a child announces `progress` / `blocked` /
`returned` so the watchdog and the parent are event-driven, not busy-polling.

**Deliverables (hermes)** extend the packet/event contract: every child run emits bounded,
redaction-safe `child.progress` (throttled), `child.blocked{reason}`, `child.returned{summary,
proof_ids, artifact_refs}` (the R3 `return_summary` shape). Parents subscribe to their direct
children's events; AS0's probe treats a fresh `child.progress` as "advancing." "Check as often
as it needs" becomes **wake-on-event**, not fixed-tick — a model turn is spent only when a
child returns or blocks, never to poll a healthy child.

**Tests** child events are bounded + redaction-safe; a parent wakes on `child.returned`, not on
a timer; a healthy child costs the parent 0 model turns.

**Handoff prompt** "Implement 06 §AS1: bounded redaction-safe `child.progress/blocked/returned`
events (returned = R3 return_summary shape); parents wake on them (event-driven, not
busy-poll); AS0's probe consumes progress as liveness. Tests per spec. Check your brain first."

---

### AS2 — Recursive supervision (each node watches ONLY its direct children)

**Deliverables (hermes)** `agent_runtime/supervision.py`: a node's supervisory scope is its
`spawned_by`-direct children only (`snapshot.py` already carries `spawned_by` lineage +
`returned_to`). A parent's context is assembled from its direct children's *distilled
summaries* (R3), never their transcripts — context stays O(direct children). Depth + fan-out
caps per node (the steering executor already has fan-out/depth limits — reuse). A stalled or
blocked child aggregates upward one level at a time (`child.blocked` → parent decides →
if unresolved, parent emits its own `child.blocked` to *its* parent).

**Tests** parent context excludes grandchildren transcripts; a leaf block propagates to the
root through each level; fan-out/depth caps hold; lineage (`spawned_by`/`returned_to`) is
complete tree-wide.

**Handoff prompt** "Implement 06 §AS2: recursive supervision scoped to `spawned_by`-direct
children; parent context = children's R3 distilled summaries (O(direct children), never
grandchild transcripts); per-node fan-out/depth caps; upward block aggregation. Tests per
spec. Check your brain first."

---

### AS3 — Per-boundary recursive gate (no trust-by-claim, at every level)

**Deliverables (hermes)** the authoritative gate (`_build_authoritative_stage_gate_decision` +
`_collect_command_proof`, already harness-executed) fires at **every** handoff boundary in the
tree, not just the top. A parent accepts a child's `returned` iff the harness-re-run gate on
that child's isolated diff passes; a failed gate makes the child `needs_fixes` to its parent,
never silently `done`. `_handoff_diff_weakens_tests` tamper-check applies per level.

**Tests** a child that reports done with a failing/tampered diff is rejected by its parent's
gate; a genuinely-passing child is accepted; recursion holds 2+ levels deep.

**Handoff prompt** "Implement 06 §AS3: re-run the harness authoritative gate at EVERY handoff
boundary (recursive), parent trusts the gate not the child's claim, tamper-check per level.
Tests 2+ levels deep. Check your brain first."

---

### AS4 — Hierarchical budget allocation

**Deliverables (hermes)** a parent sub-allocates from a global ceiling to each child
(`swarm.global_token_hard_limit` → per-child `per_lane_token_limit`-style sub-budgets); a child
that exhausts its allocation blocks up to its parent (which may re-allocate or escalate), never
silently overspends the global pool. Reuse the R2-verified per-run + mission ceilings.

**Tests** child over-allocation blocks up, not out; the global ceiling is never exceeded even
with a deep tree; re-allocation path works.

**Handoff prompt** "Implement 06 §AS4: hierarchical budget — parent sub-allocates a global
ceiling per child; child exhaustion blocks upward; global cap never exceeded. Tests per spec.
Check your brain first."

---

### AS5 — Concurrent lanes (real parallelism) — depends on doc-05 RD2

**Deliverables (hermes)** teach the scheduler to dispatch the **set** of ready-and-independent
stages (all `depends_on` satisfied), capped by `max_active_lanes`, onto concurrent lanes —
`state_machine.next_action` returns a ready-set, not a single action; `run_until_settled`
opens a lane per stage. `depends_on` still gates dependents (backend→frontend stays serial;
frontendA ∥ frontendB overlap). Requires doc-05 RD2 store isolation (WAL read model / lane
locks) so two lanes writing concurrently can't corrupt. `swarm.enabled` becomes real.

**Tests** a fan-out blueprint (two independent stages sharing only `scope`) runs both lanes with
**overlapping run windows** in the event log; dependent stages stay serial; no store corruption
under concurrent writes.

**Handoff prompt** "Implement 06 §AS5 (requires doc-05 RD2): `next_action` returns the
ready-and-independent set capped by `max_active_lanes`; `run_until_settled` runs them on
concurrent lanes; `depends_on` still gates. Prove with a two-independent-frontend blueprint
showing overlapping run windows in the event log, no store corruption. Check your brain first."

---

### AS6 — Deploy reliability (kills silent "N agents fail to deploy")

**Deliverables (hermes)** every spawn is *verified up*: a child that fails to acquire its
slot/worktree/assignment within `deploy_timeout_seconds` emits `child.deploy_failed{reason}`
to its parent (not a silent `agent_already_assigned` swallow — the `e7fba7c61` assignment
release is the precondition; this is the *observability + retry* on top). The parent
re-dispatches or escalates. AS0's probe treats a never-started child as hung.

**Tests** a slot-blocked spawn surfaces `child.deploy_failed` within the timeout and is
retried/escalated, never silently dropped; the assignment-starvation class (this session's
`agent_already_assigned`) is caught, not swallowed.

**Handoff prompt** "Implement 06 §AS6: verify every spawn came up within `deploy_timeout_seconds`;
`child.deploy_failed` → parent retry/escalate; never silently swallow an
`agent_already_assigned`-class failure. Tests per spec. Check your brain first."

---

### AS7 — Certification gates

**Deliverables** perf/liveness CI (hangs caught ≤ `liveness_hung_seconds`; healthy-child parent
cost = 0 model turns); concurrency soak (N lanes deep tree, zero store corruption, zero silent
deploy drops); crash-recovery drill (kill mid-tree, rebuild from event log, subtree resumes
without double-apply); redaction-at-rest over all child summaries. Only after 10 green runs:
`production_envelope` entry for `recursive_supervision` with real controls (no advertised-but-
inert copy — the H5 lesson).

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

## 5. Sequencing
AS0 (alone, immediate — fixes hangs today) → AS1 → AS2 + AS3 together (never AS2 without AS3) →
AS4 → AS5 (after doc-05 RD2) → AS6 → AS7. AS0 and AS6 are the two that pay off even while the
engine is still serial (hangs + deploy failures are orthogonal to concurrency).
