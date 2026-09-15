# Decision / HUD Simplification — Target Model

> **2026-07-30 — partially describes a removed subsystem.** This doc's deletion
> targets (`request_test_run`, `propose_stage_plan`, the `delivery` packet,
> proof-from-trace) were all worker-lane machinery, removed wholesale by
> [16 — Mission Lane Removal](16-mission-lane-removal.md). Its §Steering sections
> are the design origin of the **kept** `steered_by` edges;
> `decision_contract_registry.py` still exists (only its role matrix went in S11).

Status: planning (2026-07-01). **Direction: agents work unbounded; the harness reads the
work instead of making agents fill a form.** This replaces the decision-contract "HUD
options" surface (root cause of the `model_invalid_output` failure class).

Companion: `Launcher_Brain/20 — Active Initiatives/mission-control-crossstack-routing-audit-2026-07-01.md`.

Prerequisites already landed on `main` (this session): operator-chat context bloat fix
(`a477c7c4d`), cross-stack **repo-grounding** fix (`2da7631b1` — a dev now grounds in its
current stage's repo), HUD skill-hash parity fix (`492d5cc86`). The grounding fix is what
makes "the harness reads the git diff of the grounded repo" trustworthy.

## Why this exists — the failure it kills

Concrete example, fully traced (2026-07-01 live run + code):

- A dev decision must carry both a top-level `decision_type` **and** a nested
  `delivery.work_status`, which must match a fixed 1:1 table (`packets.py:169`).
- `work_status` is a **pure bijection of `decision_type`** — zero new information.
- It is **not in any HUD `payload_template`** the agent is shown — the agent is told
  (by skill prose) to emit a field the HUD never presents.
- Its enum offers **all 6 values** when exactly **one** is valid for the chosen decision —
  five of six are traps.
- It is **never read** for routing (no `if work_status ==` anywhere in `agent_runtime`);
  outcomes key off `decision_type` alone.
- On any mismatch the validator **hard-`raise`s** (`packets.py:637`) → `model_invalid_output`
  → the run fails. Live, a backend_dev turn set `work_status: proof_requested` under a
  non-`request_test_run` envelope and died — then the **recovery deadlocked** (an open
  `model_invalid_output` incident makes the ticker only re-run neko, which never closes it,
  so identical neko runs loop and the no-progress guard never trips).

Root cause: **three independently-authored shape layers out of sync** —
`HudShape.payload_template` (what the agent sees) ≠ `DecisionContract.nested_contracts`
(what the validator requires) ≠ `ObjectContract` (nested required/enum). Agents are
required to fill fields they were never shown, in a form that then rejects them. This whole
class disappears once the harness reads the work instead of validating a form.

## The core reframe

Today an agent must copy a `payload_skeleton` and fill a validated packet (`delivery`,
`work_status`, `proof_ids`, `changed_files`, …). It is required to **declare** what it
did in a form that a validator then rejects on mismatch. Two-thirds of that form is either
a duplicate of another field (`work_status` = bijection of `decision_type`), invisible in
the HUD it was told to copy from, or never read for routing.

**New model:** the agent just *works* with its native Hermes tools; the harness *observes*
the work (git diff + tool trace) and runs its own verification. The agent only signals the
few things that are intent, not observable fact.

```
Agent (unbounded)            Harness (reads + verifies + shows)
  edit files        ───▶      git diff of the grounded repo   ─┐
  run tests/analyze ───▶      tool trace (command + exit code) ─┼──▶ HUD (operator dashboard)
  self-correct      ───▶      re-run proof for the gate        ─┘     + gate state + owner
  … then emit one of 3 signals ▶ hand off · block · escalate
```

## Two layers

**Layer 1 — the agent works (unbounded, base Hermes runtime).**
Full native tools it already has: read, search, **edit/patch**, **terminal (run
tests/analyzers)**, skill_view. It loops freely in-session — edit → run test → edit → run
test — exactly like base/alice already do in mission-chat with zero friction. No
`propose_patch`, no `request_test_run`. Patching *is* editing files; testing *is* running
the command. Self-testing happens throughout; the real proof command runs near the end.

**Layer 2 — the harness reads, verifies, and shows.**
It does not wait for a form. It reads ground truth and surfaces it.

## How the harness knows (reads the work, not a form)

| Old: agent fills a field | New: harness reads ground truth | Already exists? |
|---|---|---|
| `changed_files`, `changed_paths`, `patch`, `summary` | `git diff` / `git status` of the grounded repo | partial — `dirty_state` per-repo counts already tracked |
| `request_test_run`, `proof_ids`, `work_status`, `command_summary` | tool trace `run.tool.*` (command_label + exit_code + output) | yes — `ChatProgressSink` already records it; live-verified capturing `flutter test` / `manage.py check` |
| `next_owner`, routing | blueprint graph + gate state | yes |
| `coverage_claims`, `tests`, `proof_summary` | observed test commands + harness re-run result | trace exists; re-run is the new gate piece |

Observed truth beats self-report: an agent can claim `work_status: patch_proposed` with no
edit — the diff can't lie; it can claim a test passed — the exit code is in the trace.

## The 3 signals the agent still emits (intent, not fact)

Because these can't be reliably observed:

1. **hand off / done** — the harness can't always tell "stopped" from "finished," so the
   agent ends its turn with a terminal status. Hand-off goes to the **next graph node** —
   which is QA only if the graph has one (the default `neko_two_dev_default` has no QA;
   `frontend_backend_join` does). QA is *required if present*, not universally. Required to
   finish a goal.
2. **block** — a hard stop with a `reason` (+ `log_ref` when it's a code location).
3. **escalate issue** — only when a discovered problem is too big to fix inline. **Small
   issues → just fix them** (more editing, no signal). On escalate, neko **spawns a helper
   sub-agent to resolve it and steers the result back into the same chat**, so the original
   agent resumes where it left off (the continuity skill).

That's the entire agent-emitted contract. Everything else is deletion.

## What each agent keeps (Layer-1 work + Layer-2 signals)

- **Dev:** works unbounded (edit + self-test). Emits: **hand off** (harness attaches the
  observed diff + proof), **block**, or **escalate**. Small found issues → *just fix them*
  (more editing, no signal).
- **Neko:** **scope & route** (`objective` + `acceptance_criteria` + `target_owner` +
  `target_repo` + `proof_gate`), **resolve incident**, **triage / spawn** a helper.
- **QA:** **verdict** (`approved` / `needs_fixes` / `blocked` + `coverage`), reading the
  harness-verified proof in the HUD (not an agent-declared packet).

Stage planning is **internal** to the agent's own reasoning/todos; the plan is reviewed at
the QA/neko boundary, not adjudicated mid-work as a `propose_stage_plan` decision.

## The HUD has two sides: status (verification) + action (steering)

The apparent contradiction — "remove the HUD menu" vs "the HUD should show how to steer" —
resolves once you split the HUD:

- **Status side (read-only, harness-owned):** the **verification dashboard**. Diff, proofs
  captured, **agent-run vs harness-run** proof, gate pass/fail, current owner, blocks. The
  agent never fills this; QA/neko adjudicate from it.
- **Action side (the ONLY menu left):** **steering/coordination only** — a small legible
  verb set + which targets are steerable *right now*. This is coordination, never work.

What we deleted was the menu for an agent's **own work** (patch/test as a filled packet).
What we keep — and sharpen — is the menu for **steering other agents**. The agent's work
never touches the HUD; steering is the only place a "pick from options" surface survives,
and it's a handful of verbs, not a 30-field packet.

## Steering — the coordination primitive (make it first-class + visible)

The whole system relies on neko / qa / any graph combination steering each other. This is
already half-modeled: `agent_topology` (snapshot `_agent_topology`) exposes nodes + `steers`
edges from the blueprint **and** runtime `spawned_by` lineage. The gap: it's a **static
wiring diagram**, not an **action surface** — it shows who's connected, not "who you can
steer now and how."

**Target: steering works like Codex/Claude sub-agent spawning** — an explicit delegation to
a named target that runs and returns its result into the steerer's chat. A steer =
`pick a steerable target → delegate (route or spawn) → it works → result flows back to your
session`. "Spawn a helper and steer the same chat" (the continuity skill) **is** the steer
primitive.

The HUD **steering view** shows, per the live graph:
- **where control sits** (who holds the turn now);
- **available steer targets** for the current steerer (derived from `steers` edges +
  runtime state) — a discoverable list, like the Agent tool's subagent picker;
- **the steer verbs** available on each target: `route` (hand the active slice over),
  `spawn` (delegate a helper sub-agent), `re-scope`, `resolve incident`, `verdict-back`;
- **spawned-by lineage**, so a helper's result is visibly a child of the steerer and its
  return into the parent chat is legible.

Static capability (who *can* steer whom) comes from the blueprint topology; **available-now**
(who *is* steerable this moment) is derived from the current stage/owner/gate state. The HUD
shows both.

### The steering skill — how agents are taught to steer (sample, don't slurp)

Steering *policy* lives in a skill, not the harness. The one rule that keeps it bounded:

**Sample progress; never absorb the whole transcript.** To check on a steered/spawned
agent, the skill teaches the steerer to pull a **few progress/thought lines** (a bounded
tail / grep for status) to see *how it's flowing* — not read the full output into context.
A steerer's context must stay small no matter how much the child produces.

The skill teaches:
- **Spawn heavy investigation, don't do it in your own context:** if a task needs a lot of
  reading/searching/exploration before you can act, spawn a sub-agent to do the digging and
  return a **distilled summary + refs**. Keep the exploration out of your context so you
  start the actual work (edit/test) **lean** — the parent context is reserved for building,
  not for the pile of files it took to understand the problem.
- **Check by peeking:** decide from the child's *summary + gate state + a short progress
  peek* (last few redaction-safe `run.progress`/thought lines), not a replay of its work.
- **One steer at a time:** issue a single verb (`route`/`spawn`/`re-scope`/…), let the
  child run, re-check by peeking again. Don't micro-manage token-by-token.
- **Intervene on signal, not noise:** re-steer only on a stall, a block, a failed gate, or
  an off-scope drift the peek reveals — otherwise let it run to its summarized return.
- **Never inline a child's raw output** into your own message/decision; reference it by
  artifact/proof id (the harness carries the bytes, you carry the pointer).

This relies on one harness primitive (S1/S5): a **bounded "progress peek"** — last N
redaction-safe progress lines for a node — so the skill can sample cheaply and the full
stream never has to enter a steerer's context.

## The HUD, in one line

**Status = harness-verified truth (diff · observed proof · authoritative gate). Action =
steer only (targets + verbs), like a sub-agent picker. Work never appears as a form.**

## Steering — staged path to AAA enterprise-grade

Current state: `_agent_topology` (snapshot.py) already builds a nodes+edges read-model
(blueprint `steers` edges + runtime `spawned_by` lineage + per-node stages/runs/streams).
It is **half-built**: a static visibility graph, rebuilt in full every snapshot, with
per-node `role_streams` — so it **grows unbounded with spawn count and floods context as
agents operate** (the snapshot is already multi-MB). The stages below take it from
"visibility graph" to "reliable, bounded, first-class steering."

The bar (every stage): bounded output at scale, permission-gated, observable, no orphans,
no deadlocks, parity-truthful. Not "it works once" — "it works at N agents, unattended."

### S1 — Bounded, live-derived read-model
- Derive **available-now** targets from live stage/owner/gate state, not just static edges.
- **Cap and summarize per-node payload**: bound `role_streams`/owned-stage lists per node;
  emit counts + a redaction-safe summary + artifact refs, never the raw stream inline.
- Wire into the parity envelope: any truncation reported in `completeness`/`drops`.
- Ship the **bounded "progress peek"** primitive: last N redaction-safe progress/thought
  lines for a node, so a steerer can sample a child's flow without pulling its transcript
  (the harness side of the skill's "sample, don't slurp" rule).
- **AAA bar:** topology payload size is O(bounded), independent of spawn count; snapshot
  `build_ms` does not climb as agents multiply; parity flags every drop. *(Directly fixes
  the "output is large as it operates" problem.)*

### S2 — Steer action surface (verbs, not a wiring diagram)
- Expose `route` / `spawn` / `re-scope` / `resolve` / `verdict-back` as **executable**
  actions per `(steerer, target)`, derived from state + permission, with a
  `recommended_steer`.
- **AAA bar:** every affordance the HUD shows is actually executable right now — no dead
  buttons, no "available" targets that reject on use.

### S3 — Spawn + return-to-parent-chat, summarized
- The spawn steer verb creates a sub-agent that posts a **redaction-safe summary + proof/
  artifact refs** back into the **parent `session_id`** — never the full transcript.
- Explicit lineage (`spawned_by`, `returned_to`) on the event + read-model.
- **AAA bar:** parent context growth per steer is bounded (summary, not replay); a helper
  that produced 50k tokens returns a paragraph + refs; lineage is legible in the HUD.

### S4 — Steer lifecycle reliability
- Every steer has a terminal state; spawn timeouts; orphan/stale sub-agent reaping;
  re-steer path; failure → incident (never silent, never a neko-only deadlock loop).
- **AAA bar:** no orphaned or runaway sub-agents; a failed steer surfaces and recovers
  without operator hand-ticking; bounded, not infinite.

### S5 — Steering observability
- First-class events: `steer.requested` / `steer.started` / `steer.returned` /
  `steer.failed` in the trace; HUD renders control location + targets + verbs + lineage;
  parity self-checks for steer/lineage integrity (orphan target, edge-without-node).
- **AAA bar:** the operator can see who steered whom, when, and the (summarized) result —
  redaction-safe, no hidden chain-of-thought.

### S6 — Permissions + fan-out safety
- Steer capability gated by topology + role (a node cannot steer outside its edges);
  **spawn budget caps** (max concurrent sub-agents, max depth) to bound fan-out and cost.
- **AAA bar:** bounded fan-out and spend; no privilege escalation via steering; caps are
  logged when hit (no silent truncation of the swarm).

### S7 — Scale + certification
- N-agent steering under load; bounded snapshot `build_ms`; unattended multi-steer goal
  completion (ties to the Stage 61G unattended-certification gate).
- **AAA bar:** `build_ms` stable as agent count grows; a multi-steer cross-stack goal
  reaches `done` unattended, repeatably.

**Sequencing note:** S1 (bounded read-model) and S3 (summarized return) are the two that
address the large-output concern and should land first; S2/S5 make steering usable and
visible; S4/S6 make it safe; S7 certifies it.

## Continuity (spawn + steer same chat) = a skill

The policy — "hit something too big → spawn a helper, hand it this chat's context, wait,
resume where you left off" — is a **skill** composing two runtime primitives:
1. **spawn/delegate** a sub-agent — `harness swarm` / `persona instance create`
   (display-name/placement mint) exist.
2. **post the result back into the shared session** — `harness mission-chat message`
   / `persona instance return-summary` + SessionDB/messaging exist. (S70 note:
   `persona instance message` was the retired free-floating assignment queue and
   no longer exists; the chat lane is the only messaging lane.)

The only thing to confirm is that a spawned agent can write into the **parent's**
`session_id` (true shared chat, not a fork). If yes, continuity is 100% a skill; if not,
that single post-back primitive is the only harness hook needed.

## The honest gate caveat

For the gate to be trustworthy the harness must **re-run** the proof command itself, not
only trust the agent's observed run (an agent could run `echo pass`). So: **observe the
agent's run for the HUD; re-run it harness-side for the authoritative gate.** Show both;
gate on the harness one. "Done" = agent hands off **and** the harness re-run for the
stage's repo is green — so QA never receives an unproven hand-off.

## Where it lives

- **Base Hermes runtime (tools):** edit, terminal, read/search, spawn, post-to-session.
- **Core skill (how to work + steer):** inspect narrowly → edit → self-test → run the real
  proof near the end → hand off; when to fix-inline vs escalate; **spawn heavy investigation
  to a sub-agent and start work lean**; **spawn-and-resume continuity**; **steer by sampling
  a progress peek, never absorbing a child's transcript**.
- **Harness:** diff capture, **proof-from-trace observer**, **gate re-run**, HUD dashboard,
  hand-off/block/escalate routing, the spawn/session primitives.

## What gets deleted from the contract

- Decision types: `propose_patch`, `request_test_run`, `propose_stage_plan`,
  `report_issue_discovery` (→ inline fix or `escalate`). ~19 types → **~5 coordination
  signals** (hand off, block, escalate, neko scope/route/resolve/triage, qa verdict).
- Objects: the entire `delivery` packet (1 req + 29 opt) and `work_status`; the
  HUD `payload_skeleton`/`options[]` fill surface.
- Merges that remain relevant: `dev`/`launcher_dev` owner alias, `recipe_id`/
  `proof_recipe_id`, single-value enums (`mcp_server`, `handoff.to`) → derived.

## Build order

1. **Proof-from-trace observer** — classify `run.tool.*` commands as proof, record
   artifacts. (Unblocks removing `request_test_run`.)
2. **Diff capture on hand-off** — attach `git diff` of the grounded repo to the stage.
   (Unblocks removing `delivery`/`changed_files`.)
3. **Harness gate re-run** — deterministic re-run of the stage proof command; HUD shows
   observed + authoritative.
4. **Collapse the agent contract** to `hand_off` / `block` / `escalate` (+ neko/qa
   coordination). Delete `propose_patch` / `request_test_run` / `delivery` / `work_status`.
5. **HUD dashboard** — render the two sides: status lanes (diff, observed proof, gate) +
   the **steering action surface** (available targets + verbs, control location,
   spawned-by lineage) built on the existing `agent_topology` read-model.
6. **Steering as first-class** — promote `steers` edges from a static graph to an
   available-now action set (route / spawn / re-scope / resolve / verdict), derived from
   stage/owner/gate state; this is the coordination menu that survives.
7. **Continuity skill** — confirm the same-session post-back primitive, then ship the
   spawn-and-resume skill (the spawn steer verb + return-to-parent-chat).
8. **Recovery** — a stray bad signal can't deadlock: neko recovery closes stale
   `model_invalid_output`-class incidents (largely moot once the fill-surface is gone).

## Production hardening — the enterprise envelope (gaps to close)

The model above is the right direction but is a *design*, not yet production-hardened.
"Unbounded work + harness reads it" only becomes AAA/enterprise once these are closed.
Several have partial infra to build on (noted). **H1–H4 are intrinsic and blocking** — they
must land with the build order, not after; H5–H10 are the production-readiness envelope.

- **H1 — Repo isolation for parallel/spawned agents.** *Today dev runs ground in the LIVE
  checkout (`repo_context.py`) with NO worktree isolation* — two agents (or a dev + a spawned
  helper) editing one tree corrupt it, and "read the git diff" becomes ambiguous. Each
  concurrently-active agent works in its own **git worktree** (or a per-repo lock when
  strictly serial); the harness diffs that worktree and lands on hand-off. **AAA bar:** N
  agents work the same repo concurrently with zero cross-contamination; an agent's diff is
  exactly its own.
- **H2 — Diff attribution / clean baseline.** The tree may already be dirty (unrelated WIP —
  e.g. the launcher's uncommitted files). Snapshot a per-turn baseline; attribute only the
  agent's delta; never fold pre-existing dirt into a proof/hand-off. **AAA bar:** a repo with
  pre-existing uncommitted changes yields a hand-off diff that excludes them.
- **H3 — Execution safety envelope.** "Unbounded tools" needs a boundary: command allow/deny
  (no `push`, tree wipes, credential exfil, non-allowlisted network), secret redaction in the
  trace (partly done), irreversible-op gating. Build on `tools/threat_patterns.py` +
  `terminal_tool`. **AAA bar:** an agent can't push, wipe the tree, leak a secret, or touch
  prod without an approval gate; every blocked attempt is logged.
- **H4 — Gate integrity.** The authoritative re-run must be deterministic and honest:
  environment readiness (backend gate needs Docker/Postgres up → **fail closed** if not);
  flaky-test policy (bounded retries/quarantine, never silent pass); and **test-tampering
  detection** — the diff must not weaken/skip the tests being gated (compare test/assertion/
  skip counts pre-vs-post; a shrinking suite fails). **AAA bar:** "green" means the real suite
  passed in the real environment and wasn't neutered; `echo pass` or edited-to-pass can't
  satisfy the gate.
- **H5 — Migration + rollback.** Removing decision types breaks in-flight goals and existing
  blueprints/skills. Ship behind a **feature flag**; dual-run old+new validation during
  transition; compat-shim the old contract; provide rollback. **AAA bar:** flipping the flag
  never strands an in-flight goal; un-migrated blueprints keep running.
- **H6 — Operator control + approval gates.** First-class **pause/resume/kill/take-over** for
  a goal or a whole swarm (lane park/resume exists — extend), + human approval for
  irreversible/prod/destructive actions. **AAA bar:** one action freezes the swarm; an
  operator can take a chat over; no irreversible action without explicit approval.
- **H7 — Cost + fan-out governance.** Beyond count caps (S6): token/$ budgets **per goal and
  per swarm** with enforcement (`run_budget_exceeded` exists per-run — extend), and graceful
  degradation at the ceiling (stop spawning, summarize, hand back). **AAA bar:** a goal/swarm
  can't exceed budget; hitting it degrades cleanly and is logged.
- **H8 — Durability + crash recovery.** The observation path (diff/trace/gate) must survive a
  mid-turn crash: event-sourced, idempotent hand-off, resumable (event log + watermark exist —
  build on them). **AAA bar:** kill the daemon mid-run; on restart the goal resumes with no
  lost/duplicated work.
- **H9 — Multi-goal scheduling.** Concurrent goals across workspaces/realms need fair
  scheduling, resource isolation, and provider backpressure (workspaces/realms/lanes exist —
  integrate). **AAA bar:** one heavy goal can't starve others; rate-limits degrade, not crash.
- **H10 — Test strategy for the observation model.** Golden fixtures for diff/trace
  observation, gate re-run, and steer lifecycle — the model is only trustworthy if its
  observation path is itself tested. **AAA bar:** observe→verify→gate has deterministic tests;
  regressions caught in CI.

## Confirmed vs to-verify

- **Confirmed:** the trace already captures commands+exit codes; `dirty_state` already
  tracks per-repo diffs; `work_status` is dead (bijection, unread, HUD-invisible); base/
  alice/devs already run native tools with zero decision friction.
- **To verify before build:** (a) spawned agent can post into the parent `session_id`;
  (b) the proof-command classifier's allowlist per repo; (c) gate re-run cost/latency is
  acceptable for the final-gate lane.
