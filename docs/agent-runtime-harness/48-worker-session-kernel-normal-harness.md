# Stage 48 Worker Session Kernel and Normal Harness Execution

Date: 2026-06-06
Owner: Codex independent Harness investigation
Status: implementation plan ready after Stage 47 live-token burn-in fixes

## Purpose

Stage 48 converts the Agent Runtime Harness from "persistent task state with stitched
agent turns" into a normal harness model: one durable worker session per role per goal,
with the Harness owning lifecycle, proof recipes, watchdogs, cleanup, and release gates.

The product goal is not merely fewer ticks. Tony should be able to start a mission and
walk away while Neko, Backend Dev, Launcher Dev, and QA behave like competent workers.
They should keep their workbench, continue their context, produce proof, self-heal within
bounded rules, and expose enough state that Mission Control can later render them as
possessable 3D workers without inventing a second runtime.

This stage is the bridge between the current CLI/Mission Control harness and the future
worker/3D operator model.

## Current Ground Truth

Stage 47 added important groundwork, but it is not yet the normal-harness endpoint.

Implemented today:

- `RunStore.latest_session_id(...)` can pass a prior safe `session_id` into a later run
  for the same task, persona, and stage.
- `ContinuousRoleSessionConfig` exists in `agent_runtime/runtime_config.py`, defaults
  `enabled=false`, and supports observe-only continuation metrics.
- `agent_runtime/role_sessions.py` has an envelope policy for same-owner/same-stage
  continuation, with redaction-safe `role_session.*` events.
- `TickEngine._execute_action(...)` can loop inside one Harness run when continuous role
  sessions are enabled and not observe-only.
- `run_until_settled(...)` can drive multiple actions, but still stops at boundaries such
  as action failure, open incidents, active runs, approval waits, blocked terminal states,
  or `max_actions`.
- Stage 47 autonomy packets, context receipts, failed-proof reuse, dirty-state gates,
  no-freeze monitors, and aggregate tool-budget guards reduce false success and silent
  loops.

Remaining gap:

- The runtime still centers `AgentRun` records rather than a durable `WorkerSession`
  entity.
- The same-session loop is bounded to a short envelope and closes on many conditions that
  a normal worker should survive through deterministic recovery.
- Proof execution is guarded, but there is no first-class proof recipe registry that
  makes common proof paths a Harness-owned plan.
- Temporary proof artifacts can still be created in product repos during live burn-ins
  unless every agent follows instructions perfectly.
- Mission Control can show runs and tasks, but it cannot yet show a live worker roster
  with stable identity, heartbeat, possession/control affordances, context receipts, and
  current assignment.

## Chosen Direction

Choose D2: durable worker sessions.

Do not rely on D1, which would mostly raise `max_actions_per_tick`, wall-clock budgets, or
token budgets. Higher limits can reduce manual relaunches, but they do not produce worker
identity, clean recovery, proof recipe determinism, or 3D possession readiness.

Also do not collapse the system into one monolithic agent. A single agent can feel smooth,
but Tony's target product needs role-specialized workers, Neko steering, QA independence,
visual proof, and operator possession. Stage 48 should make multi-agent execution feel as
continuous as one strong agent while preserving role boundaries.

## Deep Audit Corrections

These corrections close implementation gaps found by auditing the Stage 48 plan against
the current code.

### D48.1 Define persistence precisely

"Persistent worker session" does not mean Hermes keeps one Python `AIAgent` object alive
forever. The current runner creates an agent for a bounded `run_conversation(...)` call
and returns. Stage 48 persistence has three separate identities:

- `WorkerSession`: durable Harness worker identity for one role on one goal.
- `AgentRun`: immutable execution attempt/proof/decision record.
- model `session_id`: reusable provider/Hermes conversation session when it is safe and
  supported.

The normal-harness guarantee is: the worker keeps identity, assignment, context receipts,
safe model `session_id`, proof ledger, and recovery state across Harness runs. If a future
runner can keep a live in-memory agent process, it may plug into the same contract, but
Stage 48 must not depend on that.

Model session reuse should prefer `WorkerSession.session_id` over
`RunStore.latest_session_id(...)`. Stage-scoped run lookup remains a fallback for
backward compatibility, but durable workers should survive safe same-role stage changes
inside the same goal.

### D48.2 Do not create a second continuation policy

`ContinuousRoleSessionConfig`, `agent_runtime/role_sessions.py`, and the existing
`role_session.*` events are Stage 47 groundwork. Stage 48 must reuse or migrate them.

Single-policy rule:

- `enterprise_worker_sessions.same_session_continuation=true` enables the existing
  role-session policy through a `WorkerSessionPolicy` adapter.
- The adapter may add worker-session state, leases, and proof-recipe awareness, but it
  must not introduce a parallel continue/close decision path.
- Tests must prove `role_session.*` metrics and `worker_session.*` metrics agree for the
  same fake-runtime scenario before any old code path is removed.
- Later cleanup can deprecate `continuous_role_sessions`, but Stage 48 should first keep
  backward-compatible config loading.

### D48.3 Decision schema must be expanded deliberately

Proof recipes require a backwards-compatible decision contract change. Today
`request_test_run` expects `commands`. Stage 48 should allow:

```json
{
  "type": "request_test_run",
  "payload": {
    "stage_id": "launcher_contract_smoke",
    "recipe_id": "launcher_contract_smoke",
    "commands": []
  }
}
```

Rules:

- `recipe_id` is optional for existing agents.
- When `recipe_id` resolves, `commands` may be empty and the Harness supplies commands
  from the recipe.
- When `recipe_id` is unknown, the validator fails with a repairable contract error.
- When both `recipe_id` and `commands` are present, the proof policy either normalizes to
  the recipe command or rejects mismatched commands with a clear proof-policy error.
- The final proof record stores `recipe_id`, `recipe_version`, `recipe_hash`,
  `resolved_workdir_label`, and `sandbox_manifest_path`.

### D48.4 Archive and evidence preservation must include workers

`ArchiveStore` currently preserves task, run, proof, incident, and incident-detail files.
Stage 48 must add worker evidence to archive manifests:

- worker session records;
- worker-session context receipts;
- proof sandbox manifests and cleanup receipts;
- possession handback packets;
- worker-session event counts.

Archive-ready must refuse terminal tasks that still have active or possessed worker
sessions, just as it already refuses active runs.

### D48.5 Event and lock limits are real implementation constraints

`EventLog` currently allowlists event types and limits payloads to 4096 bytes. Stage 48
must add every `worker_session.*` type to the allowlist, keep payloads compact, and store
large details in artifact files.

Add a worker-session lock:

```text
locks/worker_sessions/<worker_session_id>.lock
```

The lock protects:

- resume/open races between daemon and CLI;
- possession acquire/release;
- active run assignment;
- close/archive transitions.

The global tick lock can remain, but worker locks are required for Mission Control
operator controls and future 3D possession.

## Non-Negotiable Product Semantics

1. Standard Hermes behavior stays unchanged by default.
   - New behavior remains behind config.
   - Tony's Agent Runtime Harness profile enables it explicitly.

2. A goal starts from a clean Harness slate.
   - New-goal creation cancels or marks stale previous Harness temp state while
     preserving artifacts.
   - New-goal hygiene closes stale worker sessions, expires possession leases, and marks
     old proof sandboxes read-only before the new task is created.
   - Mission Control exposes a dirty-state indicator before live tokens are spent.

3. Neko can use wait semantics only at the beginning unless a true human/safety blocker
   occurs.
   - Kickoff can ask for missing preference or scope.
   - After kickoff, Neko chooses the best justified implementation path from the brains,
     packets, proofs, skills, and runtime state.
   - Alternatives and tradeoffs are offered after the goal completes, not as a reason to
     freeze mid-mission.

4. Workers should persist by role.
   - Neko, Backend Dev, Launcher Dev, and QA each get a durable worker session for a
     goal.
   - The worker session survives multiple Harness runs, context compression, proof
     retries, and safe same-role continuations.
   - It closes only at role completion, unsafe loop/stall, unrecoverable blocker, user
     cancellation, goal archive, or explicit operator possession/release transition.

5. The Harness owns deterministic operations.
   - Preflight, proof recipes, workdirs, artifact storage, dirty-state gates, cleanup,
     retry policy, and release certification are Harness-owned.
   - Agents provide judgment, implementation, and narrow repair decisions inside those
     lanes.

## Stage 48A. Worker Session Store and Events

Add a first-class `WorkerSession` model separate from `AgentRun`.

Suggested fields:

- `worker_session_id`
- `schema_version`
- `task_id`
- `persona_id`
- `role`
- `display_name`
- `goal_epoch`
- `state`: `idle`, `assigned`, `running`, `waiting_on_tool`, `waiting_on_proof`,
  `self_healing`, `waiting_on_human`, `possessed`, `completed`, `blocked`, `closed`
- `current_stage_id`
- `current_assignment_id`
- `active_run_id`
- `session_id`
- `opened_at`, `last_heartbeat_at`, `last_context_receipt_at`, `closed_at`
- `context_receipt_id`
- `compression_receipt_id`
- `skill_manifest_hash`
- `prompt_contract_hash`
- `model`, `provider`, `api_mode`
- `decision_count`, `proof_count`, `repair_count`, `handoff_count`
- `tool_budget_used`, `read_search_budget_used`, `token_budget_used`
- `watchdog_warning_count`
- `last_environment_fingerprint`
- `last_failed_proof_id`
- `last_repair_signal_hash`
- `possession_state`: `available`, `requested`, `possessed`, `release_pending`,
  `disabled`
- `lease_owner`
- `lease_expires_at`
- `close_reason`

Required events:

- `worker_session.opened`
- `worker_session.assigned`
- `worker_session.resumed`
- `worker_session.heartbeat`
- `worker_session.context_absorbed`
- `worker_session.compressed`
- `worker_session.steered`
- `worker_session.possession_requested`
- `worker_session.possessed`
- `worker_session.released`
- `worker_session.watchdog_warning`
- `worker_session.closed`

All event payloads must be redaction-safe. Raw logs stay in artifact files and are
referenced by artifact/proof/context IDs.

Implementation seams:

- Extend existing `agent_runtime/models.py` or add `agent_runtime/worker_sessions.py`.
- Add store methods near `RunStore` patterns in `agent_runtime/store.py` or a dedicated
  `WorkerSessionStore`.
- Add path helpers in `agent_runtime/paths.py`:
  `worker_sessions_dir()`, `worker_session_path(worker_session_id)`,
  `proof_sandbox_dir(task_id, recipe_id)`, and
  `worker_context_dir(task_id, persona_id)`.
- Add status/snapshot rendering in `agent_runtime/status.py`,
  `agent_runtime/snapshot.py`, and `agent_runtime/observability.py`.
- Preserve `AgentRun` as the immutable execution record. `WorkerSession` is the durable
  worker identity and continuity ledger.

## Stage 48B. Enterprise Config and Default-Off Enablement

Add explicit config:

```yaml
agent_runtime:
  enterprise_worker_sessions:
    enabled: false
    mode: observe_only
    worker_session_store: true
    same_session_continuation: false
    harness_owned_proof_recipes: false
    no_edit_certification_sandbox: false
    possession_controls: false
    static_prompt_strategy: capability_detect
    worker_heartbeat_seconds: 5
    worker_stale_seconds: 600
    possession_lease_seconds: 900
    max_same_worker_repairs_per_stage: 1
    max_worker_context_compressions_per_goal: 3
```

Tony's Harness profile can enable:

```yaml
agent_runtime:
  enterprise_worker_sessions:
    enabled: true
    mode: enforce
    worker_session_store: true
    same_session_continuation: true
    harness_owned_proof_recipes: true
    no_edit_certification_sandbox: true
    possession_controls: true
    static_prompt_strategy: capability_detect
    worker_heartbeat_seconds: 5
    worker_stale_seconds: 600
    possession_lease_seconds: 900
    max_same_worker_repairs_per_stage: 1
    max_worker_context_compressions_per_goal: 3
```

Rules:

- Default off means ordinary Hermes profiles continue loading generic context, running
  normal sessions, and using existing behavior.
- `observe_only` must emit worker-session records without changing routing.
- `enforce` can change routing, continuation, proof execution, and dirty-state blocking.
- `static_prompt_strategy` supports `capability_detect`, `always_send`, and
  `receipt_only`. See Stage 48G.
- Config migration tests must prove missing config preserves current defaults.

## Stage 48C. Worker Execution Kernel

Extract the continuous role logic from `TickEngine._execute_action(...)` into a
`WorkerExecutionKernel`.

The kernel owns:

- opening or resuming the worker session;
- choosing the safe model `session_id`;
- injecting the static session-start prompt once;
- injecting the dynamic HUD/autonomy packet each turn;
- invoking the persona runtime;
- applying decisions through the state machine;
- executing proof recipes or bounded proof commands;
- updating worker heartbeat/progress;
- deciding whether the same worker should continue;
- closing the worker assignment at a real boundary.

Kernel return shape:

```json
{
  "ok": true,
  "action_result": {},
  "worker_session_id": "worker_...",
  "run_id": "run_...",
  "continued": true,
  "boundary": "same_worker_continue|handoff|qa_verdict|blocked|incident|terminal",
  "proof_ids": [],
  "incident_ids": [],
  "dirty_state_delta": null
}
```

`TickEngine` remains the scheduler. The kernel executes one worker envelope and returns a
bounded result. This keeps state-machine scheduling, daemon status, burn-in ledgers, and
Mission Control snapshots from being rewritten all at once.

Continuation policy should prefer staying in the same role session when:

- the next action is the same persona;
- the task is not terminal;
- there is no open unrecoverable incident;
- the session ID is safe;
- watchdogs are below thresholds;
- proof outcome has a deterministic next step for the same worker;
- token/tool budgets remain inside mission caps.

Continuation must stop when:

- ownership changes, unless Neko explicitly delegates a same-worker repair pass;
- QA reports final verdict;
- a human/safety blocker is required;
- the worker repeats the same failed proof without changed evidence;
- a dirty-state or environment blocker requires external action;
- context compression fails;
- the worker exceeds stall, loop, tool, token, or wall-clock limits.

Recoverable failures should not automatically end the goal:

- repairable `model_invalid_output` becomes same-worker contract repair when the worker
  still has a safe session and has not exceeded repair caps;
- failed command proof becomes same-worker repair when the failed proof ID is attached
  and the repair signal changed;
- transient provider failure retries inside the same worker when provider policy allows;
- environment and dirty-state blockers stop before another live model call.

This is where "same session same turn for as long as possible" becomes concrete: a worker
continues until a meaningful boundary or watchdog evidence says continuing is unsafe.

## Stage 48D. Deterministic Proof Recipe Registry

Add a proof recipe registry so common proof paths are Harness-owned.

Suggested module:

```text
agent_runtime/proof_recipes.py
```

Recipe shape:

```json
{
  "recipe_id": "launcher_contract_smoke",
  "stage_id": "launcher_contract_smoke",
  "persona_ids": ["dev"],
  "repo_label": "EterniaLauncher",
  "mode": "no_product_edit",
  "commands": ["flutter test test/stage47_system_health_contract_test.dart"],
  "required_artifacts": ["stdout", "stderr", "exit_code", "environment_fingerprint"],
  "sandbox": "runtime_proof_dir",
  "writes_product_probe": false,
  "cleanup": "none|auto_verified_probe",
  "success_gate": {"exit_code": 0},
  "redaction_policy": "strict"
}
```

Initial recipes:

- `backend_contract_smoke`
- `launcher_contract_smoke`
- `cross_stack_health_contract`
- `qa_release_verdict`
- `mission_control_stagec_visual_fullscreen`
- `runtime_status_snapshot`
- `archive_ready_smoke`

Rules:

- Agents should request `recipe_id` when a recipe exists.
- Raw commands remain allowed only when the proof command policy cannot map a request to
  a recipe.
- Recipe commands run from resolved Harness workdirs, never from redacted path labels.
- Recipe outputs are proof artifacts, not prompt text.
- Failed recipe proof IDs are attached to the next repair prompt.
- Recipe version/hash is stored with the proof record.
- Recipe lookup must be deterministic from `(task_id, stage_id, persona_id,
  proof_intent)` and must not depend on LLM prose matching alone.
- Proof policy owns command normalization. Agents may request a recipe, but they do not
  get to edit recipe commands unless the recipe declares `agent_patchable=true`.
- Recipes that need a generated test file in a product repo must declare
  `writes_product_probe=true` and `cleanup=auto_verified_probe`; otherwise any product
  repo dirty delta fails the recipe.
- Visual recipes must record window mode, requested dimensions, actual screenshot
  dimensions, target route/screen, MCP readiness, and artifact hash. Mission Control
  visual proof uses fullscreen by default.
- Recipe manifests are copied into the proof sandbox and archived with the task.

## Stage 48E. No-Edit Proof Sandbox and Cleanup

Certification and smoke goals should not create temp files in product repos by default.

Add a Harness-owned proof sandbox:

```text
X:\Eternia\.hermes\agent-runtime\proof_sandbox\<task_id>\<recipe_id>\
```

Sandbox responsibilities:

- generated test probes;
- temporary scripts;
- command wrappers;
- screenshots and visual proof metadata;
- recipe manifests;
- cleanup receipts.

Rules:

- `mode=no_product_edit` fails preflight if affected product repos are dirty or if a
  recipe attempts to write outside the sandbox.
- `mode=product_edit_allowed` requires an explicit implementation stage and final dirty
  state report.
- Burn-in manifests must include `dirty_state_before_run`,
  `dirty_state_after_run`, `sandbox_artifacts`, and `cleanup_receipts`.
- The Harness never hard-deletes evidence. It may remove temporary product-repo probes
  only after they are archived or copied into the sandbox and only when the path is
  verified inside the intended repo.

Initial enforcement is diff-based, not an OS filesystem jail:

- record repo dirty state before proof;
- run recipe from the sandbox or resolved repo workdir as required;
- record repo dirty state after proof;
- if a no-edit recipe creates or modifies product-repo files, fail the proof and attach
  the dirty delta;
- clean Harness-created product probes only when the recipe declared
  `writes_product_probe=true`, the file path matches the recipe manifest, the path is
  verified under the intended repo, and a copy plus hash has already been stored in the
  proof sandbox;
- if cleanup cannot prove ownership, leave the dirty file in place, block honestly, and
  surface the dirty-state proof to Mission Control.

Future hardening can add an OS sandbox, but the first enterprise-grade step is
deterministic detection, proof, and cleanup receipts.

## Stage 48F. Watchdogs, Heartbeats, and Freeze Recovery

The normal harness should intervene only when evidence shows looping, stalling, or unsafe
behavior.

Required watchdog signals:

- worker heartbeat age;
- model callback silence;
- tool-start/tool-complete silence;
- proof stdout/stderr heartbeat;
- repeated same proof command;
- repeated failed proof ID without changed environment signal;
- mixed read/search budget without patch/proof progress;
- repeated skill search/load of irrelevant skills;
- repeated Neko scope update without handoff/proof delta;
- context compression loop;
- dirty-state delta after a no-edit run;
- mission token burn rate versus completed stages.

Recovery policy:

1. interrupt the active worker if the agent runtime supports interruption;
2. preserve the run, proof, stdout/stderr, and context receipts;
3. classify the stall with a specific incident kind;
4. route to Neko only when a supervisory choice is needed;
5. otherwise resume the same worker session with the failed proof/stall packet attached;
6. allow one bounded repair attempt unless the environment signal changed;
7. terminal-block honestly if self-healing cannot proceed.

Incident kinds to add or normalize:

- `worker_stale_heartbeat`
- `worker_tool_silence`
- `worker_proof_silence`
- `worker_repeated_failed_proof`
- `worker_context_compression_loop`
- `worker_no_edit_dirty_delta`
- `worker_possession_timeout`
- `worker_invalid_output_repair_exhausted`

Mission Control must display why the Harness is waiting or blocked. "Frozen" should be
replaced with one of: active worker heartbeat, waiting on proof, waiting on tool,
self-healing, interrupted for loop warning, blocked by environment, blocked by dirty
state, blocked by human/safety decision, or terminal complete.

## Stage 48G. Static Prompt, Dynamic HUD, and Context Receipts

Each worker session gets two prompt layers:

- Static session-start contract: injected once when the worker session is opened.
- Dynamic HUD/autonomy packet: injected every turn.

Important implementation detail: current `GPTPersonaRuntime.build_system_prompt(...)`
builds a system prompt for every bounded `run_conversation(...)` call. Stage 48 must
detect whether the active runner/provider actually preserves the static system contract
inside the reusable model `session_id`.

Strategies:

- `capability_detect`: send the full static contract on worker open; on resume, send a
  compact static-contract hash plus any required invariant reminder unless the runner
  proves the session already has the static contract.
- `always_send`: current safe behavior; more tokens, least risk.
- `receipt_only`: lowest token use; allowed only after tests prove the model session keeps
  the static contract across resumed calls.

Do not silently omit core Harness rules just to save tokens. If the provider cannot prove
static prompt persistence, use compact reminders and receipts rather than trusting memory.

The static contract should include:

- role identity and accountability;
- relevant brains/context policy;
- proof discipline;
- skill selection policy;
- no-edit/product-edit mode;
- possession/control semantics;
- redaction and evidence rules.

The dynamic HUD should include:

- current assignment and stage;
- latest Neko steering packet;
- relevant proof recipe IDs;
- attached failed proof IDs;
- context receipt IDs;
- absorbed log cursor and compression receipt;
- dirty-state summary;
- tool/proof/token budgets;
- next expected boundary;
- "do not wait unless this is kickoff or true human/safety blocker."

Context receipt files should be formalized per worker:

```text
context/<task_id>/<persona_id>/
  absorbed_logs.jsonl
  compression_receipts.jsonl
  autonomy_packets.jsonl
  skill_receipts.jsonl
  context_summary.md
  prompt_static_receipt.json
```

The goal is not to stuff raw logs into prompts. The goal is to prove what each worker
absorbed, when it compressed, and why it could or could not continue.

Role-specific HUD overlays:

- Neko: worker roster, kickoff status, latest specialist packets, join/release gates,
  blocker summaries, and final-alternatives slot.
- Backend Dev and Launcher Dev: current recipe, repo label, failed proof IDs,
  patch/proof budget, dirty-state mode, and next handoff packet shape.
- QA: proof ledger, recipe manifests, visual-proof requirement, reviewed packet IDs,
  release/block verdict shape.

Compression policy:

- The worker session remains the same durable identity when model context compression
  rotates or replaces the underlying model `session_id`.
- `compression_receipts.jsonl` records old session ID presence, new session ID presence,
  compression reason, retained proof IDs, retained packet IDs, and summary hash.
- Compression is triggered by evidence: token pressure, provider context limit, or
  operator request. It should not run merely because a new Harness run started.
- If compression fails, the worker blocks with `worker_context_compression_loop` or a
  specific provider/context incident instead of silently cold-starting.

## Stage 48H. Neko Mission Lead Semantics

Neko becomes a durable mission lead, not a babysitter.

Neko intervention points:

- kickoff scope and role plan;
- backend-to-launcher contract join;
- failed proof recovery when a deterministic worker retry is not enough;
- dirty-state or environment blocker explanation;
- QA release handoff;
- final summary with alternatives and follow-up recommendations.

Neko should not:

- ask for preferences after kickoff when the brains and task context justify a path;
- repeatedly restate scope without new proof or blocker evidence;
- micromanage every Dev step;
- route QA before required proof IDs exist;
- hide product-edit or dirty-state deltas.

The Neko HUD should show the worker roster and let her steer with typed packets:

- `mission_scope`
- `specialist_assignment`
- `contract_join`
- `self_heal_directive`
- `qa_release_packet`
- `final_release_summary`

Enforcement detail:

- The first accepted `mission_scope` packet marks kickoff complete.
- After kickoff, Neko `request_human`, wait-style `block`, or preference-seeking language
  is valid only when the payload carries `human_required=true` and one of:
  `safety_blocker`, `credential_required`, `external_service_required`,
  `operator_policy_required`, or `ambiguous_destructive_change`.
- Otherwise the validator returns a repairable error instructing Neko to choose the best
  justified path and report alternatives after completion.

## Stage 48I. Mission Control and Future 3D Possession Contract

Mission Control should expose workers, not just runs.

Snapshot additions:

```json
{
  "worker_sessions": [
    {
      "worker_session_id": "worker_...",
      "task_id": "task_...",
      "persona_id": "backend_dev",
      "display_name": "Backend Dev",
      "state": "running",
      "current_stage_id": "backend_contract_smoke",
      "active_run_id": "run_...",
      "heartbeat_age_seconds": 3,
      "assignment_summary": "Implement backend contract smoke",
      "proof_recipe_id": "backend_contract_smoke",
      "context_receipt_id": "ctx_...",
      "compression_receipt_id": null,
      "possession_state": "available",
      "watchdog_warning_count": 0,
      "next_expected": "proof_or_delivery_packet"
    }
  ]
}
```

Control intents:

- pause worker;
- resume worker;
- interrupt active run;
- nudge with operator note;
- request Neko review;
- request possession;
- release possession;
- open context receipts;
- open proof artifacts;
- archive completed goal.

Launcher bridge implementation targets:

- Mission Control snapshot parser adds `worker_sessions`.
- Agent detail/HUD panels read worker session state before falling back to recent runs.
- The event timeline renders compact worker/run/proof rows with expandable raw-artifact
  links, rather than relying on newest-at-top raw log excerpts.
- Control bridge emits typed intents for pause/resume/interrupt/nudge/possession.
- CLI adapter maps those intents to Harness commands only after CLI surfaces exist:
  `harness worker pause`, `harness worker resume`, `harness worker interrupt`,
  `harness worker nudge`, `harness worker possess`, and `harness worker release`.
- UI feedback must distinguish "intent accepted", "worker already terminal", "blocked by
  lease", and "Harness command failed".

Possession semantics for later 3D:

- Possession must acquire an exclusive worker lease.
- The worker stops autonomous action while possessed unless the operator explicitly
  enables co-pilot mode.
- All operator actions during possession are evented and attributable.
- Release writes a handback packet so the worker can resume with context instead of
  cold-starting.
- Possession never bypasses proof, dirty-state, or release gates.

Stage 48 does not need to build the 3D view. It must produce the runtime data contract so
the 3D view can be real later.

## Stage 48J. Persona, Skill, and Prompt Self-Healing

The normal harness should improve the worker operating system without mutating prompts or
skills blindly during product work.

Self-healing outputs:

- `skill_gap_detected`: worker lacked or misused a needed skill;
- `prompt_contract_gap_detected`: worker repeatedly violated a Harness contract;
- `proof_recipe_gap_detected`: no deterministic recipe existed for a recurring proof;
- `persona_routing_gap_detected`: work routed to the wrong persona or repo;
- `hud_gap_detected`: the dynamic HUD lacked data the worker needed.

Rules:

- During a product goal, self-healing records an improvement packet and uses the safest
  bounded workaround.
- If the goal explicitly targets Harness improvement, Neko may open a Harness-meta stage
  to edit skills/prompts/code with proof.
- After completion, Neko's final summary lists proposed skill/prompt/recipe changes and
  alternatives.
- Improvement packets are evidence, not raw model complaints. They must cite proof IDs,
  incident IDs, worker session IDs, or context receipt IDs.

Tests:

- repeated irrelevant skill loads create `skill_gap_detected`;
- repeated same invalid packet shape creates `prompt_contract_gap_detected`;
- repeated ad hoc proof command for a known stage creates `proof_recipe_gap_detected`;
- self-healing packets are redaction-safe and visible in Mission Control.

## Stage 48K. Test Plan

Unit tests:

- config defaults keep `enterprise_worker_sessions.enabled=false`;
- config compatibility maps enterprise same-session mode onto the existing
  `continuous_role_sessions` policy without duplicate decisions;
- Tony profile config enables enforce mode without changing standard Hermes defaults;
- worker session opens once per persona per goal;
- new-goal hygiene expires stale worker sessions and read-only marks old proof sandboxes;
- worker session resumes with a safe prior `session_id`;
- worker session `session_id` outranks stage-scoped `RunStore.latest_session_id(...)`
  when resuming same-role stage changes;
- unsafe/redacted session IDs are discarded;
- archive-ready refuses active or possessed worker sessions and preserves closed worker
  session artifacts;
- worker CLI commands pause/resume/interrupt/nudge/possess/release honor leases and emit
  redaction-safe events;
- worker session closes on QA verdict, true human blocker, cancellation, or archive;
- kickoff wait allowed, post-kickoff preference wait rejected or routed to Neko
  self-decision;
- `request_test_run.recipe_id` validates and resolves recipe commands;
- unknown recipe IDs produce repairable contract errors;
- proof recipe lookup maps known stage/persona pairs deterministically;
- no-edit recipe writes only under proof sandbox;
- no-edit dirty delta after proof fails the proof with artifacted diff;
- Harness-created product probes are cleaned only from manifest-verified paths with
  archived copies and hashes;
- dirty product repo fails no-edit preflight before live model invocation;
- failed recipe proof ID is attached to the same worker repair packet;
- repeated failed proof without changed environment signal terminal-blocks or routes to
  Neko once;
- watchdog emits specific worker-session warning events.
- static prompt strategies are honored for open/resume and never omit mandatory rules
  without a receipt proving persistence.
- visual proof recipe records fullscreen/window metadata and screenshot dimensions.

Integration tests with fake persona runtime:

- no-op goal runs Neko -> Backend Dev -> Neko -> Launcher Dev -> Neko -> QA with one
  durable worker session per role;
- Backend Dev performs two decisions in the same worker session without opening a cold
  context;
- Launcher Dev fails a proof, receives the failed proof ID, repairs once, and passes;
- Neko joins backend proof into launcher release without asking for preferences;
- QA receives proof recipe artifacts and emits an evidence-backed verdict;
- run-until-settled does not stop on a recoverable same-worker boundary;
- active worker heartbeat prevents stale/frozen classification;
- stale worker heartbeat opens an incident with preserved artifacts;
- new goal creation does not reuse old worker sessions or writable sandboxes;
- context compression writes receipts and resumes the same worker session;
- Mission Control snapshot renders worker roster and possession fields.
- archive-ready preserves worker sessions, proof sandbox files, context receipts, and
  possession handback packets.
- CLI worker commands round-trip through the same store and event contracts that Mission
  Control uses.

Live-token burn-in:

1. simple no-op orchestration from clean runtime;
2. backend-only product edit;
3. launcher-only product edit;
4. cross-stack backend plus launcher contract edit;
5. visual-required Mission Control proof in fullscreen;
6. environment-blocked recovery;
7. dirty-state no-edit blocker;
8. failed-proof repair goal;
9. long-running worker session with context compression;
10. possession-request dry run with no autonomous action while possessed.
11. Harness self-healing goal that improves a skill or proof recipe with tests.

Each burn-in must archive artifacts, clear test goals, and end with:

- `open_tasks=0`;
- `active_runs=0`;
- `open_incidents=0`;
- expected repo dirty state;
- worker sessions closed or idle with explicit close reasons;
- proof IDs listed in the burn-in manifest;
- no unexplained freezes.

## Acceptance Checklist

Stage 48 is complete only when:

- [ ] `WorkerSession` is implemented and visible in status/snapshot.
- [ ] Enterprise worker mode is default-off and Tony-profile enabled.
- [ ] Existing Stage 47 `role_sessions.py` behavior is reused or migrated, not
      duplicated in a parallel loop.
- [ ] The doc's three identities are implemented: worker session, agent run, and safe
      model session.
- [ ] New-goal hygiene expires stale worker sessions and prevents stale sandbox reuse.
- [ ] `request_test_run.recipe_id` is schema-validated and backwards compatible.
- [ ] Same-role workers continue in one durable session until a meaningful boundary.
- [ ] Neko waits only at kickoff or true human/safety blockers.
- [ ] Proof recipes exist for the common backend, launcher, QA, visual, status, and
      archive paths.
- [ ] No-edit certification uses a Harness proof sandbox.
- [ ] Dirty product repo state blocks no-edit live dispatch before model tokens.
- [ ] No-edit dirty deltas after proof fail with archived evidence and cleanup receipts.
- [ ] Harness-created product probes are cleaned only from manifest-verified paths with
      archived copies and hashes.
- [ ] Failed proof repair uses attached proof IDs and one bounded retry.
- [ ] Watchdogs classify freezes with specific worker-session incidents.
- [ ] Context absorption and compression receipts are written per worker.
- [ ] Prompt static/dynamic injection is capability-aware and receipt-backed.
- [ ] Persona/skill/prompt self-healing packets exist and are proof-backed.
- [ ] Archive-ready preserves worker sessions and refuses active/possessed sessions.
- [ ] Mission Control can render worker roster, heartbeat, context receipts, proofs, and
      possession state.
- [ ] Unit and integration tests cover config, sessions, recipes, sandbox, watchdogs,
      Neko semantics, and Mission Control snapshot shape.
- [ ] Simple and complex live-token goals pass from clean baseline.
- [ ] Final burn-in ends with clean Harness runtime state and archived evidence.

## First Implementation Slice

Implement in this order:

1. Add default-off enterprise worker config and tests.
2. Add `WorkerSession` model/store/events and expose observe-only status/snapshot data.
3. Add archive/path/lock/event allowlist support for worker sessions and proof sandbox.
4. Add new-goal worker hygiene and proof-sandbox read-only markers.
5. Connect existing `role_sessions.py` metrics to `WorkerSession` records without
   changing routing.
6. Add `request_test_run.recipe_id` schema support and proof-policy normalization.
7. Add proof recipe registry with one no-edit recipe and one fake-runner integration
   test.
8. Add proof sandbox pathing, dirty-state preflight, and after-proof dirty-delta checks
   for no-edit recipes.
9. Extract the same-role continuation loop into `WorkerExecutionKernel` behind enforce
   mode.
10. Add static prompt strategy receipts and role-specific HUD overlays.
11. Add Neko kickoff-only wait rule and typed steering packet tests.
12. Add self-healing improvement packets for skill, prompt, persona, HUD, and recipe
    gaps.
13. Add Mission Control snapshot worker roster and possession fields.
14. Run fake-runtime integration, then simple live no-op, then complex live cross-stack.

Do not start by raising tick limits. Make the worker identity and lifecycle visible first,
then let the Harness safely run longer because it has proof that the worker is alive,
bounded, and recoverable.
