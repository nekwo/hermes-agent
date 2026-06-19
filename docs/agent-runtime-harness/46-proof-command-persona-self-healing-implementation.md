# Stage 46 Proof-Command Persona Self-Healing Implementation

Date: 2026-06-05
Owner: Codex independent Harness investigation
Status: ready for implementation after Stage 45 commit `a5e49b370`

## Purpose

Stage 46 closes the remaining live-smoke blocker from Stage 45: the Harness is now
better guarded against false success and silent loops, but a real cross-stack mission
still stopped before Launcher Dev because environment/proof failures and persona retry
behavior were not deterministic enough.

The target outcome is a full real-token sequence with no babysitting:

1. Neko scopes a backend-first cross-stack slice.
2. Backend Dev executes the backend proof path.
3. Neko joins the backend proof into a Launcher release packet.
4. Launcher Dev executes the Launcher proof path.
5. QA verifies both proof sets and the Harness settles terminally.

Current verdict from Stage 45: not fully working end-to-end yet. The remaining blocker
is proof/persona efficiency and environment preflight, not the initial Mission Control
archive/bridge code path.

## Gap Summary

Severity: P0

1. No deterministic preflight runs before expensive live specialist dispatch.
   - Docker Desktop was installed but offline and was discovered only after spending
     live model budget.
   - Flutter, backend venv, runtime root, repo cleanliness, and expected CLI surfaces
     need the same preflight treatment.

2. Dev personas do not reliably reuse failed proof IDs.
   - After Docker was fixed, Backend Dev did not issue one bounded retry over the
     failed proof command set and instead tripped the early progress gate.

3. Same-stage Dev retries can repeat after a proof-backed environment blocker.
   - The Harness should require a changed environment signal, a self-heal action, or a
     different bounded proof command before authorizing the next Dev run.

4. Neko steering is directionally right but machine-unstable.
   - Neko can phrase the correct plan as `cross_stack_contract_ordering`,
     `cross_stack_sequential_handoff`, `sequential_specialist_handoff`, or natural
     language such as "Backend Dev before Launcher Dev before QA."
   - The Harness now normalizes some of this, but Stage 46 should give Neko a typed
     mission-lead skill and HUD so she emits stable handoff packets instead of relying
     on phrase classifiers.

5. Observability does not yet explain repeated Neko scope updates or Dev read/search
   loops after failed proof.
   - The operator should see why the Harness cannot self-heal when it cannot, and the
     Harness should have counters that trigger deterministic recovery before token
     burn becomes a freeze/crash risk.

## Do Not Reimplement

These are already implemented by Stage 45 and should only be extended where tests prove
a Stage 46 dependency:

- Backend-first cross-stack scope normalization.
- Cross-stack close guard that prevents backend-only false success.
- Launcher-stage classifier hardening.
- Dev stage-plan sanitization.
- Failed proof retry routing from blocked state back to Dev after an external condition
  can be corrected.
- Proof excerpt redaction and self-contained command proof packets from Stage 45
  daemon hardening.
- No-edit exact-command prompt fast path.

Stage 46 should build on those seams rather than add parallel classifiers, duplicate
state transitions, or another proof artifact format.

## Implementation Grounding Corrections (2026-06-04 audit)

The original plan was written against assumed file/skill locations. A code audit on
2026-06-04 confirmed Stage 45 is fully landed (commit `a5e49b370` cross-stack
orchestration, `ef9437354` daemon proof recovery) and that **no Stage 46 code exists
yet** (no `preflight.py`, no packet schemas, no `harness-dev-delivery`/`harness-qa-verdict`
skills, no dynamic MCP readiness). The following corrections must be applied before
coding so substages target real seams.

### C1. Prompt file names

- Dev persona prompt is `agent_runtime/prompts/dev.md` (shared by `dev` and
  `backend_dev`). There is no `hermes_dev.md`. Backend/Launcher differences are persona
  overlays, not separate prompt files.
- Persona prompts present: `dev.md`, `qa.md`, `alice_supervisor.md`, `pm.md`,
  `shared_harness_overlay.md`.

### C2. Skill location and registration (resolves the 46B ambiguity)

Harness persona skills do **not** live under `agent_runtime/skills/` (that directory does
not exist and must not be created). The established mechanism is:

- Skills are `SKILL.md` packages resolved by `agent.skill_commands._load_skill_payload`
  from the runtime profile skills root `SKILLS_DIR` = `HERMES_HOME/skills`. For the live
  `alice` profile this resolves to `X:\Eternia\.hermes\profiles\alice\skills`, plus any
  `skills.external_dirs` from config.
- Existing harness skills already loaded this way include `agent-runtime-harness`,
  `frontend-backend-contract-handoff`, `launcher-stagec-mcp-screenshot`, and
  `harness-handoff-recovery` (none are in the repo tree).
- Personas declare skills by name in `agent_runtime/personas.py` `skills=[...]` lists,
  loaded by `agent_runtime/skill_context.load_persona_skill_context`
  (24k char/persona cap).

Therefore 46B must: author `harness-dev-delivery`, `harness-qa-verdict`, and the Neko
mission-lead skill as `SKILL.md` packages under the profile skills root, then add their
names to the relevant `skills=[...]` lists in `personas.py`. "Profile skill declaration"
in the substage file lists below refers to this, not a repo path.

Enterprise source-of-truth rule:

- The repo must carry a versioned copy of every Stage 46 skill package under
  `docs/agent-runtime-harness/stage46-skills/<skill-name>/SKILL.md`.
- Runtime installation copies those packages into `HERMES_HOME/skills/<skill-name>/SKILL.md`
  for the active profile. The profile copy is a deploy target, not the source of truth.
- The implementation must include an idempotent installer or documented CLI helper that
  verifies source hash -> installed hash before persona readiness is allowed to pass.
- Tests must use a temporary `HERMES_HOME` and install/copy the repo fixtures there; tests
  must not depend on Tony's live `alice` profile having local, uncommitted skills.

### C3. Decision pipeline insertion point

Packet validation (46.2.3 step ordering) inserts into the existing chain, not a new
validator: `agent_runtime/decision_schema.py` (role allowlist) ->
`agent_runtime/decision_contracts.py` (payload contracts) -> `agent_runtime/gates.py`
(gating) -> state mutation. Add `handoff_packet`/`delivery`/`qa_review` subobject
validation inside `decision_contracts.py` after the base contract passes and before
`gates.py` authorizes the transition.

### C4. Prerequisites carried over from Stage 44 (not yet built)

Per `45-stage-44-implementation-dedup-audit.md`, several seams Stage 46 leans on are
marked "mostly missing." Stage 46 must explicitly build or stub these rather than assume
them:

- 2.2 Neko self-heal: failure classifier, self-heal event schema, idempotency keys, and
  `cannot_self_heal` status/snapshot fields. The 46.2 recovery/cannot-self-heal packets
  depend on this.
- 4 HUD/analytics: there is no `agent_runtime/analytics.py` or stable prelude/HUD
  assembly. The 46.2 "stable Neko HUD injected every prompt" needs a host seam in
  `context_builder.py`.
- 2.1 progress leases / repeated-next-action stall detector. The 46.3 anti-freeze
  counters overlap this; extend `recovery.py`/`status.py`/`snapshot.py`, do not add a
  parallel stall detector.

### C5. Visual/MCP proof external dependency (46.2.4 / 46E)

Both the Stage 44 dedup audit and the Stage 45 smoke recorded that `launcher_qa` /
Stage C MCP tools were **not exposed in the agent thread** ("0 tools found"). 46E's
executable screenshot/video proof lanes are blocked until that tool exposure is proven
in a clean thread. Treat MCP exposure as an explicit preflight check (46.0/46C) and a
proof-backed `environment_blocker` when absent, exactly as the doc already prescribes;
do not assume the capture path is available.

### C6. QA and Neko personas have no skills list yet

In `agent_runtime/personas.py` only the `dev` and `backend_dev` personas carry a
`skills=[...]` list. The `qa` persona and the `neko_supervisor` persona have **no
`skills` field at all**. The plan's "add `harness-qa-verdict` to QA persona skill lists"
(46.2.2) and the Neko mission-lead skill (46.2) therefore cannot just append — 46B must
first add a `skills=[...]` field to both personas. This also matters because
`build_system_prompt` only emits skill guidance via `_recommended_skill_guidance(persona.skills)`
and `skill_context.load_persona_skill_context` only loads when the list is non-empty;
without the field the new skills neither load nor appear in the prompt. Add a
`test_persona_skill_policy` assertion that `qa` and `neko_supervisor` expose their new
skills.

### C7. `test_decision_contracts.py` is a new file

`agent_runtime/decision_contracts.py` exists but `tests/agent_runtime/test_decision_contracts.py`
does not. The 46.2.3/46.2.4 "Likely files" and Test Coverage lists reference it as if
extending an existing suite; treat it as new, like `tests/agent_runtime/test_preflight.py`.

## Design Completeness Specifications (close before 46A)

The packet templates above name fields and statuses but leave several load-bearing
mechanics undefined. Two implementers could build them inconsistently. These
specifications close those gaps and align with existing code conventions
(`_fingerprint` in `context_requests.py`, `StageStatus` in `states.py`,
`risk_flags` in `models.py`). Each is a hard requirement with its own test.

### D1. `environment_fingerprint` definition (the retry gate hinges on this)

The whole proof-aware retry contract (46.1/46D) turns on "fingerprint changed," but the
fingerprint is never defined. Define it:

- Composition: the ordered list of preflight check ids paired with their boolean result
  and a coarse identity token per dependency — e.g. `docker_engine=up|down`,
  `flutter=present|absent`, `backend_venv_import=ok|fail`, `runtime_root=<profile-tag>`,
  `profile=<name>`. Use only coarse present/absent/version-class tokens, never raw
  versions, paths, pids, or timestamps, so an unchanged environment always hashes equal.
- Hash with the existing convention: `sha256("env\0" + "\0".join(sorted(tokens)))[:16]`.
- `environment_fingerprint_status` ∈ `unknown|unchanged|changed`, computed by comparing
  the new hash against persisted `last_environment_fingerprint`.
- Persist `last_environment_fingerprint` on the stage/run context (new field, see D8).

### D2. Self-heal attempt accounting

The recovery packets show `attempts_remaining` counting 2 → 1 → 0 but nothing defines who
decrements or what 0 means. Specify:

- Budget is 2 self-heal attempts per `(task_id, stage_id, blocker_classification)` key. A
  different classification resets the budget.
- Each *applied* self-heal action (`preflight_retry`, `bounded_command_retry`,
  `prompt_patch`, `skill_patch`, `routing_patch`) increments `attempt_number` and
  decrements `attempts_remaining` by 1.
- At `attempts_remaining == 0` the only legal next packet for that key is
  `cannot_self_heal` (`decision_status=terminal_blocked`, `target_owner=human`).
- A duplicate packet (same `content_hash`, per 46.2.3 replay rules) does not decrement.
- Counters persist in the self-heal state object (D8) and project into the Neko HUD
  `self_heal_attempts_remaining`.

### D3. Anti-freeze thresholds (46.3 says "repeated" but gives no numbers)

Deterministic recovery needs concrete trip counts. Set them here so the state machine is
not guessing:

- `scope_update_count` for the same normalized scope: `>= 2` triggers typed-packet
  repair; `>= 3` escalates to `cannot_self_heal`.
- `same_stage_retry_count` without a changed fingerprint (D1) or applied self-heal (D2):
  `>= 1` blocks and routes to Neko self-heal.
- `dev_read_search_after_failed_proof`: `>= 1` read/search turn before a decision that
  references the failed proof ids routes to Neko self-heal (matches the existing
  `max_read_search_turns_before_decision: 1` in the recovery packet).
- skill fanout: more than 2 loaded skills, or any `skill_view` fanout after the first
  relevant skill, without a recorded current-stage purpose, routes to proof/handoff
  rather than further context expansion.

### D4. HUD field producers (who computes each injected field)

The 46.2 HUD lists fields with no stated source. Bind each:

- `required_next_decision` / `forbidden_decisions`: derived deterministically by the
  Harness from `mission_phase` + the `decision_schema` role allowlist (a phase→decision
  map the Harness owns). Never model-authored.
- counters (`scope_update_count`, `same_stage_retry_count`,
  `self_heal_attempts_remaining`): from D2/D3 persisted counters.
- `environment_fingerprint_status`: from D1.
- `resume_brief_id`: produced by the resume-brief builder, which is a Stage 44 §2.2
  prerequisite (see C4) and does not exist yet. Until it lands, project `null` and do
  not gate dispatch on it.

### D5. Enum scope — reserved vs in-scope `handoff_mode` values

`handoff_mode` enumerates `parallel_specialists` and `split_child_missions`, but the doc
never describes those flows and the live target is sequential. Mark them reserved:
validators accept them as *known* enum values, but the state machine routes them to a
`cannot_self_heal` / Neko packet with reason `unsupported_handoff_mode` until a later
stage. In-scope for Stage 46: `single_specialist`, `backend_first_cross_stack`,
`sequential_specialists`. This prevents 46 from accidentally implementing parallel/split
orchestration.

### D6. `delivery.work_status` → AgentDecision mapping

`work_status` must be a validated mirror of the decision type, not free text. Required
mapping (enforced in `decision_contracts.py`):

- `planned` → `propose_stage_plan`
- `proof_requested` → `request_test_run`
- `ready_for_qa` → `request_qa_review`
- `blocked` → `block`
- `issue_discovered` → `report_issue_discovery`
- `patched` → internal precursor state; must be followed by `proof_requested` in a later
  tick, never handed to QA directly.

A `delivery.work_status` that disagrees with the enclosing decision type fails validation.

### D7. `qa_review.next_owner` routing constraint

The field allows `dev|backend_dev|neko_supervisor|human`, which lets QA assign a
specialist directly and contradicts "QA never routes; Neko routes the rightful owner."
Constrain: for any cross-stack `contract_mismatch`, `missing_proof`, or cross-stack
coverage gap, `next_owner` must be `neko_supervisor`. QA may set `next_owner=dev` only for
a same-stage single-specialist implementation defect already owned by that persona. QA
never sets `backend_dev` directly.

### D8. New persisted fields inventory

`last_failed_proof_ids`, `last_environment_fingerprint`, the D2 self-heal counters, and
the D3 anti-freeze counters do not exist on the model today (Task carries only
`risk_flags`). Persist them in a structured `harness_self_heal` object on the
task/stage/run context (not by overloading `risk_flags`), content-hashed and replayed
like other state per 46.2.3, and surfaced redaction-safe in `harness status --json` /
`harness snapshot --json`. Proof-gate `minimum_status` values reuse existing vocabulary —
`passed` maps to `StageStatus.PASSED` for test/command proof and `approved` to the QA
verdict result; do not introduce a parallel proof-status enum.

### D9. Scope discipline — packets carry only Harness-underivable data (2026-06-05 review)

The templates in 46.2/46.2.1/46.2.2 are illustrative maximums. A review on 2026-06-05 found
each packet restates fields the Harness already owns authoritatively (actor, resolved repo,
stage, attached `proof_ids`, retry/self-heal counters). Re-validating model copies of
Harness-owned state spends prompt tokens, validation rules, and tests on data the Harness
cannot trust from the model anyway — it must use its own value. Binding rule for every
substage: **a packet's validated *required* keys are only what the Harness cannot derive
from its own state; where a packet restates a Harness-owned fact, the Harness value is
authoritative and the packet field is advisory (optional, never gating).** Unknown-key
rejection and redaction scanning (46.2.3) still apply to whatever keys are present. The
validated core of each packet is:

- `handoff_packet` (Neko routing intent the Harness cannot infer): `packet_kind`,
  `mission_phase`, `handoff_mode`, `target_owner`/`next_owner` plus their repos, `proof_gate`
  (`required`, `required_proof_types`, `minimum_status`, `visual_required`), and
  `join_gate.release_condition` for cross-stack. `self_heal.classification` and proposed
  `action` are validated; `attempt_number`/`attempts_remaining` are advisory — the Harness
  owns the count per D2/D8 and overwrites them. `assumptions_made`, `alternatives_considered`,
  `operator_note`, `resume_brief_id` are optional, never required, never gating.
- `delivery` (Dev data not already in the enclosing payload): `consumed_contract_packet_ids`,
  `produced_contract_packet_id`, `known_gaps`, `next_owner`, and `work_status` (kept only
  because D6 binds it to the decision type). `stage_id`, `repo`, `specialist_persona`,
  `proof_ids`, `changed_paths` are NOT re-validated from the delivery object — the Harness
  reads them from the run, the resolved workdir, and the proof store.
- `qa_review` (the enforcement core): `coverage` (the four-axis map that backstops cross-stack
  approval), `remaining_gaps`, and `next_owner` (constrained by D7). `proof_reviewed`
  duplicates the verdict payload's `proof_ids`; `mcp_status` duplicates `profile_readiness`;
  `contract_packets_reviewed`/`delivery_packets_reviewed` duplicate the packet listing — all
  optional, none re-validated.

HUD (overrides the 46.2 list): inject every Neko tick only the decision-bearing subset
(`phase`, `current_owner`, `required_next_decision`, `forbidden_decisions`, target/next
owner+repo, `join_gate_required`, `proof_gate_status`, `failed_proof_ids`,
`environment_fingerprint_status`). Surface the recovery counters (`scope_update_count`,
`same_stage_retry_count`, `self_heal_attempts_remaining`) and `human_only_gate_status` only
when non-default, so the steady-state prompt stays small for a stage whose purpose is
proof/token efficiency. `mission_id`/`task_id` are one value; `resume_brief_id` stays `null`
per D4.

Launch pins (46.2.4) reference the runtime root by a Harness-assigned `runtime_root_id` token
plus the `hermes_profile` name — never an absolute path — so the redaction scanner does not
reject the Harness's own visual-proof requests.

D9 supersedes the large JSON examples below wherever they disagree. The examples remain useful
as prompt templates and operator-readable shapes, but validators enforce only the D9
underivable core plus redaction rules. Harness-owned fields shown in examples are advisory
echoes for model orientation and must never gate state transitions.

## Implementation Plan

### 46.0 Preflight Dependency Gate

Add a deterministic preflight step before live specialist runs for missions whose
scope declares backend, Launcher, visual/MCP, or proof-command requirements.

Preflight inputs:

- task `affected_repos`
- active stage proof requirements
- persona target (`backend_dev`, `launcher_dev`, `qa`, `neko_supervisor`)
- runtime config
- configured repo roots

Required checks:

- Harness runtime root/profile match the active deployment profile.
- Harness repo cleanliness is reported, not blocked by default.
- Backend repo cleanliness is reported when Backend Dev is in scope.
- Launcher repo cleanliness is reported when Launcher Dev is in scope.
- Backend venv/interpreter import sanity for backend proof commands.
- Docker Desktop/Linux engine availability when backend proof requirements imply Docker
  or compose.
- Flutter availability and debug-target build readiness when Launcher or MCP proof is
  in scope.
- Stage C MCP tool exposure/readiness when visual proof is required.

Acceptance behavior:

- A failed preflight creates a proof-backed `environment_blocker` incident before Dev
  model dispatch.
- Transient/offline dependencies are actionable: include command, exit code, compact
  stderr/stdout excerpt, and exact operator/self-heal action.
- The Harness may auto-retry preflight after a known environment fix signal changes.
- Preflight failures do not consume Dev retry budget.

Likely files:

- `agent_runtime/preflight.py` new module or a narrow extension beside proof runner
  code if an existing helper is a cleaner fit.
- `agent_runtime/state_machine.py`
- `agent_runtime/events.py`
- `agent_runtime/status.py`
- `agent_runtime/snapshot.py`
- `tests/agent_runtime/test_preflight.py`
- `tests/agent_runtime/test_state_machine.py`

### 46.1 Proof-Aware Dev Retry Contract

Teach Dev personas and the Harness retry gate to treat failed proof IDs as first-class
inputs.

Prompt/HUD requirements for Dev personas:

- If the current stage has failed proof IDs, read the attached proof packet first.
- If the blocker is environmental and the environment signal changed, choose exactly
  one bounded retry command or one bounded proof-command batch.
- If the blocker is unchanged, block with the failed proof IDs and exact reason.
- Do not repeat broad read/search loops before deciding on proof retry.
- If the fix requires code, state the smallest code change and the proof command that
  will validate it.

Harness requirements:

- Persist `last_failed_proof_ids` and their environment fingerprint in the stage/run
  context.
- Authorize one same-stage Dev retry only when one of these is true:
  - environment fingerprint changed;
  - a self-heal action was applied;
  - the retry command is narrower or different and explicitly references the failed
    proof IDs.
- Block or route to Neko self-heal when a Dev run repeats read/search-heavy behavior
  after a failed proof without using the proof IDs.

Likely files:

- `agent_runtime/prompts/dev.md` (shared Dev prompt; Backend/Launcher are overlays, see C1).
- `agent_runtime/prompts/shared_harness_overlay.md`
- `agent_runtime/dev_discipline.py`
- `agent_runtime/state_machine.py`
- `agent_runtime/context_builder.py`
- `tests/agent_runtime/test_dev_discipline.py`
- `tests/agent_runtime/test_persona_prompts.py`

### 46.2 Neko Mission-Lead Skill and Stable HUD

Create a Neko-specific Harness mission-lead skill so Neko steers with typed packets
instead of loose natural language.

Core skill responsibilities:

- Convert broad operator goals into executable slices.
- Prefer backend-first for backend+Launcher contract missions.
- Emit stable handoff packets with explicit owners, repos, proof gates, and release
  conditions.
- Use only initial scope wait semantics. After scoping, choose the best justified
  implementation path from repo evidence, project brains, prior proofs, and local
  architecture rules.
- Report alternatives after completion unless the choice is human-only.
- When a specialist fails, classify the failure as environment, code, proof-command,
  context, prompt/skill, routing, provider, or human-only.
- Apply or propose one bounded self-heal action before authorizing retry.

Stable Neko HUD fields injected every prompt:

- `mission_id`
- `task_id`
- `phase`
- `current_owner`
- `required_next_decision`
- `forbidden_decisions`
- `target_dev_persona`
- `target_repo`
- `next_slice_persona`
- `next_slice_repo`
- `join_gate_required`
- `proof_gate_status`
- `failed_proof_ids`
- `environment_fingerprint_status`
- `resume_brief_id`
- `scope_update_count`
- `same_stage_retry_count`
- `self_heal_attempts_remaining`
- `human_only_gate_status`

Stable session prelude injected once per session/resume family:

- persona identity and authority boundaries
- Mission Control lifecycle contract
- wait semantic: only before implementation, bounded, no post-scoping preference waits
- typed handoff schema
- self-heal policy
- protected-surface policy
- untrusted-context warning

AgentDecision envelope:

- Neko still returns exactly one normal `AgentDecision` JSON object.
- Stage 46 adds a validated optional `handoff_packet` payload object for
  `propose_acceptance`, `needs_context`, `block`, and `resolve_incident` decisions.
- The packet is machine-owned contract data, not prose. If Neko needs prose, it belongs
  in `summary`, `rationale`, or a bounded `operator_note`.
- Unknown packet keys fail validation in tests so the Harness does not silently depend
  on fuzzy model language.
- The Harness records the packet in a redaction-safe event and projects the latest
  packet into the next persona HUD.

Common `handoff_packet` shape:

Note: this is an illustrative maximum prompt template. D9 defines the smaller validator
contract; Harness-owned fields in this example are optional/advisory at runtime.

```json
{
  "packet_version": 1,
  "packet_kind": "fresh_scope|join_release|qa_release|recovery|cannot_self_heal",
  "mission_phase": "initial_scope|backend_slice|launcher_slice|qa_release|recovery|terminal_block",
  "handoff_mode": "single_specialist|backend_first_cross_stack|sequential_specialists|parallel_specialists|split_child_missions",
  "decision_status": "ready|needs_context|blocked|retry_authorized|terminal_blocked",
  "current_owner": "neko_supervisor",
  "target_owner": "backend_dev|launcher_dev|qa|human",
  "target_repo": "EterniaBackend|EterniaLauncher|hermes-agent",
  "next_owner": "backend_dev|launcher_dev|qa|neko_supervisor|human",
  "next_repo": "EterniaBackend|EterniaLauncher|hermes-agent",
  "proof_gate": {
    "required": true,
    "existing_proof_ids": [],
    "required_proof_types": ["test_run"],
    "minimum_status": "passed",
    "visual_required": false
  },
  "join_gate": {
    "required": true,
    "release_condition": "backend proof passed and contract packet available",
    "consume_packet_from": "backend_contract_packet"
  },
  "failed_proof_ids": [],
  "environment_fingerprint_id": null,
  "self_heal": {
    "classification": "none|environment|code|proof_command|context|prompt_skill|routing|provider|human_only",
    "action": "none|preflight_retry|bounded_command_retry|prompt_patch|skill_patch|routing_patch|block",
    "attempt_number": 0,
    "attempts_remaining": 2
  },
  "assumptions_made": [],
  "alternatives_considered": [],
  "operator_note": ""
}
```

Fresh cross-stack backend-first scope packet:

```json
{
  "packet_version": 1,
  "packet_kind": "fresh_scope",
  "mission_phase": "initial_scope",
  "handoff_mode": "backend_first_cross_stack",
  "decision_status": "ready",
  "current_owner": "neko_supervisor",
  "target_dev_persona": "backend_dev",
  "target_owner": "backend_dev",
  "target_repo": "EterniaBackend",
  "current_slice": {
    "slice_id": "backend_contract",
    "objective": "Implement or prove the backend contract needed by Launcher.",
    "scope_include": ["backend API/schema/command proof required by the goal"],
    "scope_exclude": ["Launcher UI changes", "QA verdict", "archive operation"]
  },
  "next_owner": "launcher_dev",
  "next_repo": "EterniaLauncher",
  "next_slice": {
    "slice_id": "launcher_integration",
    "release_condition": "backend proof passed and backend_contract_packet available"
  },
  "proof_gate": {
    "required": true,
    "required_proof_types": ["test_run"],
    "minimum_status": "passed",
    "visual_required": false,
    "existing_proof_ids": []
  },
  "join_gate": {
    "required": true,
    "release_condition": "backend proof passed and backend_contract_packet available",
    "consume_packet_from": "backend_contract_packet"
  },
  "qa_release_condition": "backend and launcher proof sets attached",
  "assumptions_made": [],
  "alternatives_considered": []
}
```

Backend-to-Launcher join release packet:

```json
{
  "packet_version": 1,
  "packet_kind": "join_release",
  "mission_phase": "launcher_slice",
  "handoff_mode": "backend_first_cross_stack",
  "decision_status": "ready",
  "current_owner": "neko_supervisor",
  "target_owner": "launcher_dev",
  "target_repo": "EterniaLauncher",
  "source_owner": "backend_dev",
  "source_repo": "EterniaBackend",
  "source_proof_ids": ["proof_backend_contract"],
  "contract_packet": {
    "packet_id": "backend_contract_packet",
    "endpoint_or_interface": "",
    "request_shape": {},
    "response_shape": {},
    "error_shape": {},
    "example_payloads": [],
    "compatibility_notes": []
  },
  "current_slice": {
    "slice_id": "launcher_integration",
    "objective": "Consume the backend contract packet and prove the Launcher side.",
    "scope_include": ["Launcher bridge/UI/client integration required by the goal"],
    "scope_exclude": ["backend schema changes unless Neko reroutes", "QA verdict"]
  },
  "proof_gate": {
    "required": true,
    "required_proof_types": ["test_run", "screenshot"],
    "minimum_status": "passed",
    "visual_required": true,
    "existing_proof_ids": []
  },
  "next_owner": "qa",
  "next_repo": "hermes-agent",
  "qa_release_condition": "backend source proof and Launcher proof both attached"
}
```

Launcher-to-QA release packet:

```json
{
  "packet_version": 1,
  "packet_kind": "qa_release",
  "mission_phase": "qa_release",
  "handoff_mode": "backend_first_cross_stack",
  "decision_status": "ready",
  "current_owner": "neko_supervisor",
  "target_owner": "qa",
  "target_repo": "hermes-agent",
  "backend_proof_ids": ["proof_backend_contract"],
  "launcher_proof_ids": ["proof_launcher_integration"],
  "required_verification": [
    "backend proof passed",
    "Launcher proof passed",
    "Launcher consumed the backend contract packet",
    "no open blocking incidents remain"
  ],
  "known_gaps": [],
  "proof_gate": {
    "required": true,
    "required_proof_types": ["qa_verdict"],
    "minimum_status": "approved",
    "visual_required": false,
    "existing_proof_ids": ["proof_backend_contract", "proof_launcher_integration"]
  },
  "terminal_close_condition": "QA approved over backend and Launcher proof sets"
}
```

Environment blocker recovery packet:

```json
{
  "packet_version": 1,
  "packet_kind": "recovery",
  "mission_phase": "recovery",
  "decision_status": "retry_authorized",
  "current_owner": "neko_supervisor",
  "target_owner": "backend_dev",
  "target_repo": "EterniaBackend",
  "failed_proof_ids": ["proof_failed_docker"],
  "blocker": {
    "classification": "environment",
    "summary": "Docker Desktop Linux engine was offline.",
    "evidence": ["proof_failed_docker exit_code=1", "preflight_docker changed to passed"]
  },
  "environment_fingerprint_id": "envfp_after_docker_started",
  "retry": {
    "authorized": true,
    "max_commands": 1,
    "must_reference_failed_proof_ids": true,
    "command_strategy": "rerun the failed proof command or narrower equivalent"
  },
  "self_heal": {
    "classification": "environment",
    "action": "bounded_command_retry",
    "attempt_number": 1,
    "attempts_remaining": 1
  }
}
```

Prompt/skill/code self-heal recovery packet:

```json
{
  "packet_version": 1,
  "packet_kind": "recovery",
  "mission_phase": "recovery",
  "decision_status": "retry_authorized",
  "current_owner": "neko_supervisor",
  "target_owner": "backend_dev",
  "target_repo": "EterniaBackend",
  "failed_proof_ids": ["proof_failed"],
  "blocker": {
    "classification": "prompt_skill",
    "summary": "Dev ignored attached failed proof and repeated broad file search.",
    "evidence": ["failed_proof_ignored=true", "repeated_search_after_failed_proof=3"]
  },
  "self_heal": {
    "classification": "prompt_skill",
    "action": "prompt_patch",
    "attempt_number": 1,
    "attempts_remaining": 1,
    "patch_target": "agent_runtime/prompts/dev.md",
    "verification": "focused prompt and dev discipline tests"
  },
  "retry": {
    "authorized": true,
    "must_reference_failed_proof_ids": true,
    "max_read_search_turns_before_decision": 1
  }
}
```

Cannot-self-heal terminal block packet:

```json
{
  "packet_version": 1,
  "packet_kind": "cannot_self_heal",
  "mission_phase": "terminal_block",
  "decision_status": "terminal_blocked",
  "current_owner": "neko_supervisor",
  "target_owner": "human",
  "failed_proof_ids": ["proof_failed"],
  "blocker": {
    "classification": "human_only",
    "summary": "Credential or protected external side effect is required.",
    "evidence": ["proof_failed requires missing credential"]
  },
  "self_heal": {
    "classification": "human_only",
    "action": "block",
    "attempt_number": 2,
    "attempts_remaining": 0
  },
  "human_action_required": {
    "question": "Provide the missing credential/config or approve a scoped test substitute.",
    "minimum_response_needed": "credential path or explicit approval for substitute proof"
  },
  "alternatives_considered": [
    "mocked proof substitute",
    "narrower local proof",
    "deferring protected integration"
  ]
}
```

Template rules:

- Every illustrative packet should show `packet_version`, `packet_kind`, `mission_phase`,
  `decision_status`, `current_owner`, and either `target_owner` or `human_action_required`
  so prompts remain readable. Runtime validators require only the D9 underivable core.
- Cross-stack packets must name both repos and the exact join condition.
- Retry packets must carry `failed_proof_ids` and a changed environment fingerprint or
  self-heal action.
- Cannot-self-heal packets must explain why Neko cannot repair the blocker and name the
  smallest human action required.
- No packet may include raw command logs, secrets, absolute local paths, or unbounded
  context excerpts.

Likely files:

- `agent_runtime/prompts/alice_supervisor.md`
- `agent_runtime/prompts/shared_harness_overlay.md`
- new `SKILL.md` packages under the profile skills root `HERMES_HOME/skills`
  (`X:\Eternia\.hermes\profiles\alice\skills` for the live profile), registered by name
  in `agent_runtime/personas.py` `skills=[...]` — see C2. Do NOT create
  `agent_runtime/skills/`.
- `agent_runtime/personas.py` (persona skill list registration)
- `agent_runtime/context_builder.py`
- `tests/agent_runtime/test_persona_prompts.py`
- `tests/agent_runtime/test_context_builder.py`

### 46.2.1 Shared Dev Delivery Skill and Specialist Templates

Create a shared Dev delivery skill so Backend Dev and Launcher Dev use the same
machine-stable delivery shapes. Neko's typed packets decide who should work next; the
Dev delivery skill decides how a specialist proves, retries, blocks, and hands off
without prose drift.

Recommended skill name:

- `harness-dev-delivery`

Skill responsibilities:

- Convert Neko's `handoff_packet` into one minimal Dev work slice.
- Keep Dev inside the resolved repo and current stage.
- Require proof IDs before QA handoff.
- Require failed proof reuse before any same-stage retry.
- Require exact blocker packets when environment, contract, visual/MCP, or protected
  surfaces prevent progress.
- Preserve raw logs/artifacts through Harness proof capture instead of pasting noisy
  output into the decision.
- Produce one of the supported AgentDecision types with a typed delivery object.

Common Dev delivery fields:

```json
{
  "delivery_version": 1,
  "stage_id": "stage_id",
  "source_handoff_packet_id": "packet_id_or_event_id",
  "specialist_persona": "backend_dev|dev",
  "repo": "EterniaBackend|EterniaLauncher",
  "work_status": "planned|patched|proof_requested|ready_for_qa|blocked|issue_discovered",
  "changed_paths": [],
  "consumed_contract_packet_ids": [],
  "produced_contract_packet_id": null,
  "proof_ids": [],
  "failed_proof_ids": [],
  "known_gaps": [],
  "next_owner": "neko_supervisor|qa|human",
  "operator_note": ""
}
```

`propose_stage_plan` delivery template:

```json
{
  "type": "propose_stage_plan",
  "summary": "Split the handoff into one executable specialist slice.",
  "rationale": "The current mission is too large for one bounded Dev tick.",
  "payload": {
    "stages": [
      {
        "id": "launcher_integration",
        "title": "Launcher Integration",
        "objective": "Consume the backend contract packet and prove the Launcher side.",
        "acceptance_criteria": [
          "Contract packet fields are mapped through provider/repository/domain layers.",
          "Focused Flutter tests or analyze pass.",
          "Visual or semantic proof is requested when UI-visible behavior is claimed."
        ],
        "affected_paths": [
          "lib/features/..."
        ],
        "test_plan": [
          "flutter test test/features/..._test.dart",
          "flutter analyze"
        ],
        "delivery": {
          "delivery_version": 1,
          "source_handoff_packet_id": "packet_backend_to_launcher",
          "specialist_persona": "dev",
          "repo": "EterniaLauncher",
          "work_status": "planned",
          "consumed_contract_packet_ids": ["backend_contract_packet"]
        }
      }
    ]
  }
}
```

`request_test_run` delivery template:

```json
{
  "type": "request_test_run",
  "summary": "Request deterministic Harness proof for the completed specialist slice.",
  "rationale": "The patch is narrow and the named commands are the authoritative proof gate.",
  "payload": {
    "stage_id": "launcher_integration",
    "commands": [
      "flutter test test/features/mission_control/mission_control_bridge_test.dart",
      "flutter analyze"
    ],
    "delivery": {
      "delivery_version": 1,
      "source_handoff_packet_id": "packet_backend_to_launcher",
      "specialist_persona": "dev",
      "repo": "EterniaLauncher",
      "work_status": "proof_requested",
      "changed_paths": [
        "lib/features/mission_control/..."
      ],
      "consumed_contract_packet_ids": ["backend_contract_packet"],
      "proof_ids": [],
      "failed_proof_ids": []
    }
  }
}
```

Proof-aware retry delivery template:

```json
{
  "type": "request_test_run",
  "summary": "Retry the failed proof after the blocker signal changed.",
  "rationale": "The attached failed proof identified an environment blocker; preflight now reports the dependency available.",
  "payload": {
    "stage_id": "launcher_integration",
    "commands": [
      "flutter test test/features/mission_control/mission_control_bridge_test.dart"
    ],
    "delivery": {
      "delivery_version": 1,
      "source_handoff_packet_id": "packet_retry_authorized",
      "specialist_persona": "dev",
      "repo": "EterniaLauncher",
      "work_status": "proof_requested",
      "failed_proof_ids": ["proof_failed_flutter"],
      "environment_fingerprint_id": "envfp_flutter_ready",
      "retry_reason": "environment_changed",
      "retry_scope": "one bounded command"
    }
  }
}
```

`request_qa_review` delivery template:

```json
{
  "type": "request_qa_review",
  "summary": "Launcher slice is complete and ready for Neko-coordinated QA release.",
  "rationale": "All planned specialist stages are complete and proof IDs are attached.",
  "payload": {
    "stage_id": "launcher_integration",
    "proof_ids": ["proof_launcher_widget", "proof_launcher_visual"],
    "handoff": {
      "to": "qa",
      "stage_complete": true,
      "known_gaps": [],
      "delivery": {
        "delivery_version": 1,
        "source_handoff_packet_id": "packet_backend_to_launcher",
        "specialist_persona": "dev",
        "repo": "EterniaLauncher",
        "work_status": "ready_for_qa",
        "changed_paths": [
          "lib/features/mission_control/..."
        ],
        "consumed_contract_packet_ids": ["backend_contract_packet"],
        "proof_ids": ["proof_launcher_widget", "proof_launcher_visual"],
        "next_owner": "neko_supervisor"
      }
    }
  }
}
```

Blocker delivery template:

```json
{
  "type": "block",
  "summary": "Launcher Dev cannot proceed without a complete backend contract packet.",
  "rationale": "The handoff is missing response nullability and empty/error examples; implementing would require guessing API semantics.",
  "payload": {
    "reason": "missing_backend_contract_fields",
    "log_ref": {
      "path": "events.jsonl",
      "line": 123,
      "summary": "backend_contract_packet missing response nullability and error examples"
    },
    "delivery": {
      "delivery_version": 1,
      "source_handoff_packet_id": "packet_backend_to_launcher",
      "specialist_persona": "dev",
      "repo": "EterniaLauncher",
      "work_status": "blocked",
      "failed_proof_ids": [],
      "known_gaps": [
        "response nullability",
        "empty-state example",
        "error response shape"
      ],
      "next_owner": "neko_supervisor"
    }
  }
}
```

Launcher Dev overlay:

- Must consume `backend_contract_packet` before backend-dependent UI/provider work.
- Must map fields through repository/provider/domain layers, not widget-local maps.
- Must run focused Windows Flutter proof first, then broaden only when scope requires.
- Must request visual or semantic proof for UI-visible behavior; if `launcher_qa` MCP is
  unavailable, block with the exact MCP/tool exposure gap rather than overclaiming.
- Must preserve UI polish and existing architecture; no redesign unless the handoff
  explicitly asks for it.

Backend Dev overlay:

- Must produce `backend_contract_packet` when Launcher is the downstream consumer.
- Must include endpoint, auth, request/response/error schemas, examples, compatibility,
  and proof IDs.
- Must not release Launcher Dev until the contract packet is tested or explicitly
  fixture-frozen by Neko.
- Must use Docker/Postgres proof when required; if unavailable, return an environment
  blocker packet instead of substituting weaker proof.

Skill/profile integration:

- Add `harness-dev-delivery` to both `dev` and `backend_dev` persona skill lists.
- Keep `frontend-backend-contract-handoff` as the domain-specific cross-stack contract
  skill; `harness-dev-delivery` is the AgentDecision/output-shape skill.
- Skill use is default for non-trivial Dev ticks, but skill loading must be search-first
  and bounded:
  - call native `skill_search(query=...)` using task, stage, repo, proof gate, and
    blocker keywords before loading skill bodies;
  - load the single most relevant skill by default;
  - load a second skill only when the active proof gate or handoff explicitly requires
    it;
  - loading more than two skills requires a current-stage purpose recorded in the final
    AgentDecision delivery object;
  - never preload or bulk-load all recommended persona skills.
- If native skill search is unavailable, fall back to `skills_list` plus one targeted
  `skill_view`; repeated `skill_view` fanout is a Harness/skill intervention signal.
- Add prompt tests proving both Dev personas see the shared delivery templates and the
  correct specialist overlay.
- Add schema/contract tests proving unknown delivery keys fail and required fields are
  present for `request_test_run`, `request_qa_review`, and `block`.

### 46.2.2 QA Verdict Skill and Proof Review Templates

Create a shared QA verdict skill so QA uses the same proof-review shape every time.
QA should not infer mission completion from prose or broad investigation; it should
consume Neko `handoff_packet` data, Dev `delivery` data, and Harness proof packets,
then emit one bounded verdict or one exact missing-proof request.

Recommended skill name:

- `harness-qa-verdict`

Skill responsibilities:

- Make proof-first QA the default shape, not a prompt-only suggestion.
- Consume attached proof IDs before repo/file search.
- Validate backend proof, Launcher proof, visual/MCP proof, and contract handoff
  coverage according to the current mission packet.
- Distinguish missing proof from failed proof, environment/MCP proof blockers, and real
  implementation defects.
- Prevent backend-only proof from approving cross-stack missions.
- Request exactly one missing proof command/screenshot/video when a bounded request can
  unblock the verdict.
- Emit `report_qa_verdict` with structured findings and `qa_review` metadata.

Skill loading rule:

- QA should load `harness-qa-verdict` by default for non-trivial QA ticks.
- QA should load `launcher-stagec-mcp-screenshot` only when the current task/stage
  requires visual proof, Mission Control UI proof, Stage C semantic state, screenshot,
  or video evidence.
- QA should not load broad Launcher/Backend implementation skills unless the proof
  packet references a specific contract/file and the verdict cannot be made from proof
  alone.

Common `qa_review` fields:

```json
{
  "qa_review_version": 1,
  "source_handoff_packet_id": "packet_id_or_event_id",
  "review_scope": "implementation",
  "mission_phase": "qa_release",
  "proof_reviewed": {
    "backend_proof_ids": [],
    "launcher_proof_ids": [],
    "visual_proof_ids": [],
    "qa_requested_proof_ids": []
  },
  "coverage": {
    "backend_contract": "not_required|missing|reviewed|failed",
    "launcher_integration": "not_required|missing|reviewed|failed",
    "visual_or_mcp": "not_required|missing|reviewed|blocked|failed",
    "cross_stack_join": "not_required|missing|reviewed|failed"
  },
  "contract_packets_reviewed": [],
  "delivery_packets_reviewed": [],
  "mcp_status": {
    "required": false,
    "server": "launcher_qa",
    "readiness": "not_required|ready|missing|blocked|failed",
    "blocker": ""
  },
  "decision_basis": "proof_packet|proof_plus_targeted_file_check|missing_proof|environment_blocker",
  "remaining_gaps": [],
  "next_owner": "harness|neko_supervisor|dev|backend_dev|human"
}
```

Approved verdict template:

```json
{
  "type": "report_qa_verdict",
  "summary": "QA approved the implementation from attached proof.",
  "rationale": "Backend, Launcher, and required proof gates were represented by safe proof IDs and no blocking gaps remain.",
  "payload": {
    "review_scope": "implementation",
    "verdict": "approved",
    "proof_ids": [
      "proof_backend_contract",
      "proof_launcher_integration",
      "proof_launcher_visual"
    ],
    "findings": [],
    "qa_review": {
      "qa_review_version": 1,
      "source_handoff_packet_id": "packet_launcher_to_qa",
      "mission_phase": "qa_release",
      "proof_reviewed": {
        "backend_proof_ids": ["proof_backend_contract"],
        "launcher_proof_ids": ["proof_launcher_integration"],
        "visual_proof_ids": ["proof_launcher_visual"],
        "qa_requested_proof_ids": []
      },
      "coverage": {
        "backend_contract": "reviewed",
        "launcher_integration": "reviewed",
        "visual_or_mcp": "reviewed",
        "cross_stack_join": "reviewed"
      },
      "contract_packets_reviewed": ["backend_contract_packet"],
      "delivery_packets_reviewed": ["delivery_launcher_integration"],
      "mcp_status": {
        "required": true,
        "server": "launcher_qa",
        "readiness": "ready",
        "blocker": ""
      },
      "decision_basis": "proof_packet",
      "remaining_gaps": [],
      "next_owner": "harness"
    }
  }
}
```

Needs-fixes verdict template:

```json
{
  "type": "report_qa_verdict",
  "summary": "QA found an implementation defect in the Launcher proof.",
  "rationale": "The visual proof or focused test proof contradicts the acceptance criteria.",
  "payload": {
    "review_scope": "implementation",
    "verdict": "needs_fixes",
    "proof_ids": ["proof_launcher_visual"],
    "findings": [
      {
        "kind": "implementation_defect",
        "severity": "blocking",
        "owner": "dev",
        "summary": "Mission Control shows active count that does not match the pinned Harness snapshot.",
        "evidence": ["proof_launcher_visual", "snapshot_active_count=1", "ui_active_count=0"],
        "required_fix": "Fix Launcher Mission Control read-model parity for the pinned runtime root."
      }
    ],
    "qa_review": {
      "qa_review_version": 1,
      "source_handoff_packet_id": "packet_launcher_to_qa",
      "mission_phase": "qa_release",
      "proof_reviewed": {
        "backend_proof_ids": ["proof_backend_contract"],
        "launcher_proof_ids": ["proof_launcher_integration"],
        "visual_proof_ids": ["proof_launcher_visual"],
        "qa_requested_proof_ids": []
      },
      "coverage": {
        "backend_contract": "reviewed",
        "launcher_integration": "reviewed",
        "visual_or_mcp": "failed",
        "cross_stack_join": "reviewed"
      },
      "decision_basis": "proof_packet",
      "remaining_gaps": ["Launcher visible state does not match pinned Harness snapshot"],
      "next_owner": "dev"
    }
  }
}
```

Blocked missing proof verdict template:

```json
{
  "type": "report_qa_verdict",
  "summary": "QA is blocked because required Launcher proof is missing.",
  "rationale": "The mission is cross-stack, but only backend proof IDs are attached.",
  "payload": {
    "review_scope": "implementation",
    "verdict": "blocked",
    "proof_ids": ["proof_backend_contract"],
    "findings": [
      {
        "kind": "missing_proof",
        "severity": "blocking",
        "owner": "neko_supervisor",
        "summary": "Launcher proof is required before QA can approve this cross-stack mission.",
        "evidence": ["backend proof present", "launcher proof absent"],
        "required_fix": "Release Launcher Dev or attach an accepted fixture-frozen Launcher proof."
      }
    ],
    "qa_review": {
      "qa_review_version": 1,
      "source_handoff_packet_id": "packet_launcher_to_qa",
      "mission_phase": "qa_release",
      "proof_reviewed": {
        "backend_proof_ids": ["proof_backend_contract"],
        "launcher_proof_ids": [],
        "visual_proof_ids": [],
        "qa_requested_proof_ids": []
      },
      "coverage": {
        "backend_contract": "reviewed",
        "launcher_integration": "missing",
        "visual_or_mcp": "missing",
        "cross_stack_join": "missing"
      },
      "decision_basis": "missing_proof",
      "remaining_gaps": ["missing Launcher proof", "missing visual/MCP proof"],
      "next_owner": "neko_supervisor"
    }
  }
}
```

Request one missing proof command template:

```json
{
  "type": "request_test_run",
  "summary": "QA requests one missing deterministic proof command.",
  "rationale": "The implementation proof is otherwise reviewable, but this exact command is required to cover the acceptance criterion.",
  "payload": {
    "stage_id": "launcher_integration",
    "commands": [
      "flutter test test/features/mission_control/mission_control_bridge_test.dart"
    ],
    "qa_review": {
      "qa_review_version": 1,
      "source_handoff_packet_id": "packet_launcher_to_qa",
      "mission_phase": "qa_release",
      "proof_reviewed": {
        "backend_proof_ids": ["proof_backend_contract"],
        "launcher_proof_ids": [],
        "visual_proof_ids": [],
        "qa_requested_proof_ids": []
      },
      "coverage": {
        "backend_contract": "reviewed",
        "launcher_integration": "missing",
        "visual_or_mcp": "not_required",
        "cross_stack_join": "missing"
      },
      "decision_basis": "missing_proof",
      "remaining_gaps": ["missing focused Launcher bridge test proof"],
      "next_owner": "harness"
    }
  }
}
```

Request missing visual/MCP proof template:

```json
{
  "type": "request_screenshot",
  "summary": "QA requests exact Mission Control visual proof.",
  "rationale": "The task requires user-visible Mission Control proof and no safe screenshot/video proof is attached.",
  "payload": {
    "stage_id": "launcher_integration",
    "target": "mission_control",
    "proof_requirement": "pinned Stage C Mission Control screenshot showing active/archive/log state parity",
    "mcp_server": "launcher_qa",
    "required_launch_pins": {
      "hermes_profile": "alice",
      "runtime_root_id": "<harness-assigned runtime-root token; never an absolute path>",
      "expected_instance": "active"
    },
    "qa_review": {
      "qa_review_version": 1,
      "source_handoff_packet_id": "packet_launcher_to_qa",
      "mission_phase": "qa_release",
      "coverage": {
        "backend_contract": "reviewed",
        "launcher_integration": "reviewed",
        "visual_or_mcp": "missing",
        "cross_stack_join": "reviewed"
      },
      "mcp_status": {
        "required": true,
        "server": "launcher_qa",
        "readiness": "missing",
        "blocker": "no visual proof ID attached"
      },
      "decision_basis": "missing_proof",
      "remaining_gaps": ["missing pinned Mission Control screenshot proof"],
      "next_owner": "harness"
    }
  }
}
```

Blocked visual/MCP environment template:

```json
{
  "type": "report_qa_verdict",
  "summary": "QA cannot verify visual proof because the Stage C MCP path is blocked.",
  "rationale": "The required visual proof lane is unavailable, so QA cannot approve a visual Mission Control claim.",
  "payload": {
    "review_scope": "implementation",
    "verdict": "blocked",
    "proof_ids": ["proof_launcher_widget"],
    "findings": [
      {
        "kind": "mcp_environment_blocker",
        "severity": "blocking",
        "owner": "neko_supervisor",
        "summary": "launcher_qa MCP server was missing, stale, or unable to capture readable pixels.",
        "evidence": ["profile_readiness missing_mcp_servers includes launcher_qa"],
        "required_fix": "Repair launcher_qa MCP exposure or attach a clearly-labeled accepted visual fallback."
      }
    ],
    "qa_review": {
      "qa_review_version": 1,
      "source_handoff_packet_id": "packet_launcher_to_qa",
      "mission_phase": "qa_release",
      "proof_reviewed": {
        "backend_proof_ids": [],
        "launcher_proof_ids": ["proof_launcher_widget"],
        "visual_proof_ids": [],
        "qa_requested_proof_ids": []
      },
      "coverage": {
        "backend_contract": "not_required",
        "launcher_integration": "reviewed",
        "visual_or_mcp": "blocked",
        "cross_stack_join": "not_required"
      },
      "mcp_status": {
        "required": true,
        "server": "launcher_qa",
        "readiness": "blocked",
        "blocker": "launcher_qa unavailable or screenshot capture blocked"
      },
      "decision_basis": "environment_blocker",
      "remaining_gaps": ["visual proof unavailable"],
      "next_owner": "neko_supervisor"
    }
  }
}
```

Contract mismatch template:

```json
{
  "type": "report_qa_verdict",
  "summary": "QA found a backend/Launcher contract mismatch.",
  "rationale": "Launcher proof consumed fields or semantics that do not match the backend contract packet.",
  "payload": {
    "review_scope": "implementation",
    "verdict": "needs_fixes",
    "proof_ids": ["proof_backend_contract", "proof_launcher_integration"],
    "findings": [
      {
        "kind": "contract_mismatch",
        "severity": "blocking",
        "owner": "neko_supervisor",
        "summary": "Launcher used a field/default/error shape not present in the backend contract packet.",
        "evidence": ["backend_contract_packet", "delivery_launcher_integration"],
        "required_fix": "Neko must route to the rightful owner: Backend Dev if the contract is wrong or Launcher Dev if the consumer mapping is wrong."
      }
    ],
    "qa_review": {
      "qa_review_version": 1,
      "source_handoff_packet_id": "packet_launcher_to_qa",
      "mission_phase": "qa_release",
      "coverage": {
        "backend_contract": "reviewed",
        "launcher_integration": "failed",
        "visual_or_mcp": "not_required",
        "cross_stack_join": "failed"
      },
      "contract_packets_reviewed": ["backend_contract_packet"],
      "delivery_packets_reviewed": ["delivery_launcher_integration"],
      "decision_basis": "proof_plus_targeted_file_check",
      "remaining_gaps": ["contract mismatch"],
      "next_owner": "neko_supervisor"
    }
  }
}
```

QA template rules:

- QA may approve only when all required coverage fields are `reviewed` or
  `not_required`.
- Cross-stack missions require backend proof and Launcher proof unless Neko's packet
  explicitly marks one side `not_required` with a reason.
- Visual/MCP proof required by task/stage may not be waived by QA. If the proof lane is
  blocked, QA returns `blocked`, not `approved`.
- `request_test_run`, `request_screenshot`, and `request_video` are single-missing-proof
  actions. QA should not ask for multiple broad proof lanes in one decision.
- Findings must be redaction-safe and point to proof IDs, compact metadata, event IDs,
  or safe relative log handles.
- QA never patches, never updates contract shape, and never invents examples; it reports
  the mismatch and lets Neko route the rightful owner.

Skill/profile integration:

- Add `harness-qa-verdict` to QA persona skill lists.
- Keep `launcher-stagec-mcp-screenshot` as the visual/MCP domain skill; load it only
  when visual/MCP proof is required.
- Configure QA readiness so `launcher_qa` is required when a task/stage requires
  Launcher Mission Control visual/MCP proof.
- Add prompt tests proving QA sees the verdict templates and skill-loading order.
- Add schema/contract tests proving `qa_review` required fields for
  `report_qa_verdict`, `request_test_run`, `request_screenshot`, and `request_video`.
- Add state-machine/QA proof-gate tests proving cross-stack backend-only proof cannot
  produce an approved QA verdict.

### 46.2.3 Runtime Packet Persistence and Projection Contract

The Neko, Dev, and QA templates are not accepted as prompt-only guidance. Stage 46 must
wire them into the runtime as validated, replayable, redaction-safe contract packets.

Packet ownership:

- `handoff_packet` is mission steering data owned by Neko. Persist it on the task event
  log and project the latest valid packet into the next persona HUD.
- `delivery` is specialist execution data owned by Backend Dev or Launcher Dev. Persist
  it on the run event log, attach the packet ID to related proof requests, and project
  the latest per-stage delivery into Neko and QA context.
- `qa_review` is verification data owned by QA. Persist it on the QA verdict event and
  attach it to the QA proof/verdict metadata so Mission Control can explain approval,
  needs-fixes, or blocked outcomes without scraping prose.
- Packets are never the only copy of raw proof. Raw command logs, screenshots, videos,
  and proof metadata remain in the existing proof/artifact stores; packets reference
  those artifacts by proof ID, event ID, or safe relative handle.

Every persisted packet receives runtime metadata:

```json
{
  "packet_id": "packet_...",
  "packet_type": "handoff_packet|delivery|qa_review",
  "packet_version": 1,
  "task_id": "task_...",
  "run_id": "run_...",
  "stage_id": "stage_...",
  "actor": "neko_supervisor|backend_dev|dev|qa",
  "created_at": "iso8601",
  "source_decision_type": "<the enclosing decision.type; the carrier set is defined per packet_type in 46.2/46.2.1/46.2.2, not re-enumerated here>",
  "content_hash": "sha256:...",
  "redaction_status": "passed"
}
```

Validation pipeline:

1. Parse a single `AgentDecision` JSON object.
2. Enforce the role allowlist from `decision_schema`.
3. Enforce existing decision payload contracts from `decision_contracts`.
4. Validate optional `handoff_packet`, `delivery`, and `qa_review` subobjects with
   packet-specific schemas.
5. Reject unknown keys inside packet subobjects unless the schema explicitly declares an
   extension field.
6. Run the redaction/path/secret scanner over packet strings.
7. Persist packet events and attach packet IDs to proof requests or verdicts.
8. Mutate task/stage/run state only after packet validation succeeds.
9. Rebuild context/HUD projections from persisted events after resume, daemon restart,
   or archive visibility reads.

Packet redaction rules:

- No absolute local paths such as `X:\...`, `C:\...`, `/home/...`, or UNC paths.
- No secret-bearing path segments such as `.env`, `credentials`, `secrets`, `tokens`,
  `.ssh`, `auth`, or private config dumps.
- No raw stdout/stderr blocks. Use proof IDs, compact proof metadata, event IDs, or safe
  relative log handles.
- No environment variable values, credentials, bearer tokens, cookies, or API keys.
- No unbounded context excerpts. Summaries must be short and operator-actionable.

Replay and idempotency:

- Packet events are append-only and replayable.
- A duplicate packet with the same `content_hash`, task, stage, actor, and source run is
  ignored for state mutation and counted as a duplicate event, not treated as new
  progress.
- A retry authorization must reference either a changed environment fingerprint, a
  verified self-heal event, or a changed/narrower proof command fingerprint.
- Crash recovery must rebuild the latest mission HUD from events before dispatching the
  next live model call.

Likely files:

- `agent_runtime/decision_contracts.py`
- `agent_runtime/events.py`
- `agent_runtime/context_builder.py`
- `agent_runtime/state_machine.py`
- `agent_runtime/status.py`
- `agent_runtime/snapshot.py`
- `tests/agent_runtime/test_decision_contracts.py`
- `tests/agent_runtime/test_context_builder.py`
- `tests/agent_runtime/test_events.py`

### 46.2.4 Visual/MCP Proof Runtime Contract

Visual and MCP proof must be runtime behavior, not only QA wording. If a task or stage
requires Mission Control UI proof, Stage C semantic proof, screenshot proof, or video
proof, the Harness must treat that as an executable proof lane.

`request_screenshot` payload contract:

```json
{
  "stage_id": "launcher_integration",
  "target": "mission_control",
  "proof_requirement": "pinned Mission Control screenshot showing active/archive/log state parity",
  "mcp_server": "launcher_qa",
  "required_launch_pins": {
    "hermes_profile": "alice",
    "runtime_root_id": "<harness-assigned runtime-root token; never an absolute path>",
    "expected_instance": "active"
  },
  "qa_review": {}
}
```

`request_video` uses the same required fields plus:

```json
{
  "duration_seconds": 8,
  "interaction_script": ["open_mission_control", "select_ready_goal", "archive_ready_goal"]
}
```

Runtime behavior:

- Validate `request_screenshot` and `request_video` payloads before state mutation.
- If `launcher_qa` or the configured Stage C MCP path is unavailable, create a
  proof-backed `environment_blocker` before spending another QA/Dev model turn.
- If the MCP path is available, run the capture path, verify the artifact is non-empty
  and tied to the pinned runtime root/profile, then attach the visual proof ID to the
  stage.
- A blank, stale, wrong-profile, or wrong-runtime-root capture is failed proof, not a
  soft warning.
- Mission Control visual proof cannot be waived by QA. Only Neko can mark it
  `not_required`, and must include the reason in a validated `handoff_packet`.
- The screenshot/video proof record must include the capture command/tool name, exit
  code or MCP result status, a redaction-safe artifact handle, the runtime-root id, profile, and compact semantic
  state summary.

Dynamic readiness:

- Static persona config may keep `required_mcp_servers` empty for generic QA work.
- When the active task/stage has `requires_visual_proof=true`, Mission Control scope,
  Stage C semantic proof, screenshot proof, or video proof, the runtime must inject
  `launcher_qa` into the effective QA readiness requirement.
- `harness status --json` and `harness snapshot --json` must show both static
  `required_mcp_servers` and dynamic `effective_required_mcp_servers`.

Likely files:

- `agent_runtime/decision_contracts.py`
- `agent_runtime/profile_readiness.py`
- `agent_runtime/proof_runner.py`
- `agent_runtime/state_machine.py`
- `agent_runtime/status.py`
- `agent_runtime/snapshot.py`
- `tests/agent_runtime/test_decision_contracts.py`
- `tests/agent_runtime/test_profile_readiness.py`
- `tests/agent_runtime/test_proof_runner.py`
- `tests/agent_runtime/test_state_machine.py`

### 46.3 Observability Counters and Anti-Freeze Signals

Add counters that expose inefficient or looping behavior before the operator experiences
it as a freeze/crash.

Counters/events:

- repeated Neko scope update count per task and per blocker.
- repeated same-stage Dev retry count.
- repeated Dev read/search after failed proof.
- repeated skill search/view fanout after the first relevant skill load.
- loaded skill count and selected skill names per run.
- failed proof reused vs ignored.
- environment fingerprint changed vs unchanged.
- self-heal proposed/applied/verified/cannot-self-heal.
- provider invalid-output incidents following proof-backed retry.

State-machine use:

- Repeated Neko scope updates for the same scope should trigger typed packet repair or
  terminal `cannot_self_heal`.
- Repeated Dev read/search after failed proof should route to Neko self-heal, not blind
  Dev retry.
- Repeated skill loading after one or two relevant skills should route to proof, handoff,
  a smaller stage plan, or Neko self-heal rather than continuing context expansion.
- Repeated provider/model invalid output should back off or switch to a supported
  recovery route without consuming environment retry budget.

Operator surface:

- `harness status --json` and `harness snapshot --json` expose compact counters.
- Mission Control can render these later as compact self-heal rows under Stage 5.
- The operator surface must expose the reason for stopped progress as one of:
  `waiting_for_preflight`, `environment_blocked`, `self_heal_pending`,
  `retry_authorized`, `waiting_for_proof_capture`, `qa_blocked_missing_proof`,
  `terminal_human_required`, or `settled`.
- Each non-settled state must include `owner`, `stage_id`, `blocking_event_id`,
  `related_proof_ids`, and the next deterministic action the Harness will take.
- A run that reaches an anti-freeze boundary must produce a terminal or recoverable
  incident before the daemon exits the tick, so "nothing happened" is never the only
  observable result.

Likely files:

- `agent_runtime/events.py`
- `agent_runtime/status.py`
- `agent_runtime/snapshot.py`
- `agent_runtime/state_machine.py`
- `agent_runtime/dev_discipline.py`
- `tests/agent_runtime/test_status.py`
- `tests/agent_runtime/test_snapshot.py`
- `tests/agent_runtime/test_state_machine.py`

### 46.4 Implementation Order and Stage Boundaries

Implement Stage 46 in narrow, ordered substages so the same behavior is not implemented
twice through prompts, classifiers, and state-machine patches. The concrete symbols,
signatures, insertion points (file:line), and test names for each substage are in the
`Build-Ready Implementation Spec (46A-46G)` section below; the list here is the ordering
contract, the build sheet is the engineering detail.

46A - packet schemas and event projection:

- Add validators for `handoff_packet`, `delivery`, and `qa_review`.
- Add packet events, content hashes, redaction checks, duplicate replay handling, and
  HUD/context projection after resume.
- Keep existing natural-language classifiers only as compatibility fallbacks until
  typed packets are present.

46B - skill files and bounded skill resolution:

- Create the Neko mission-lead skill, `harness-dev-delivery`, and
  `harness-qa-verdict`.
- Add the skills to default personas and Alice profile only after the files resolve in
  readiness checks.
- Preserve search-first skill loading and the one-skill-default/two-skill-normal cap.

46C - preflight and dynamic readiness:

- Add dependency preflight for backend, Launcher, visual/MCP, runtime root, and profile
  pinning.
- Add dynamic `effective_required_mcp_servers` so visual/MCP missions require
  `launcher_qa` even if generic QA does not.

46D - proof-aware retry and self-heal gates:

- Persist failed proof IDs and environment fingerprints.
- Authorize bounded same-stage retries only after changed environment, verified
  self-heal, or changed/narrower proof command fingerprints.
- Route repeated proof-ignoring behavior to Neko self-heal.

46E - visual/MCP proof execution:

- Validate and execute `request_screenshot` and `request_video` proof lanes.
- Reject blank, stale, wrong-profile, or wrong-runtime-root captures.
- Attach visual proof IDs or create proof-backed environment blockers.

46F - observability and Mission Control readiness:

- Expose anti-freeze counters, packet summaries, dynamic readiness, and stopped-progress
  reasons in status/snapshot.
- Ensure Mission Control can render compact event rows from structured data without
  losing raw proof artifacts.

46G - real-token smoke and cleanup:

- Run the required Neko -> Backend Dev -> Neko join -> Launcher Dev -> QA sequence.
- Clean disposable goals only through cancel/archive-ready/archive.
- Record exact blockers and self-heal behavior if the sequence does not complete.

Do not double implement:

- Do not add a second proof artifact format. Extend proof metadata and reference proof
  IDs from packets.
- Do not add parallel cross-stack routing classifiers once typed packets exist.
- Do not make QA profile MCP requirements globally hard if only visual/MCP tasks need
  them.
- Do not patch prompts as the only enforcement path; every prompt contract added here
  needs validator or state-machine tests.
- Do not let Mission Control scrape prose when the same information can come from packet
  metadata, proof metadata, status, or snapshot.

### 46.5 Final Real-Token Proof Run

After implementation and focused tests pass, run a disposable real-token cross-stack
goal and monitor the full daemon/tick path.

Required sequence:

1. Neko
2. Backend Dev
3. Neko join release
4. Launcher Dev
5. QA

Required proof:

- preflight proof packet
- backend proof IDs
- Neko join release event with typed handoff packet
- Launcher proof IDs
- QA proof/verdict over both backend and Launcher proof sets, including structured
  `qa_review` metadata from `harness-qa-verdict`
- final `harness status --json`
- final `harness snapshot --json`
- archived evidence manifest if the disposable mission is archived

Failure handling:

- If a blocker occurs, preserve the task/run/proof artifacts.
- Clean up the disposable task only through cancel/archive-ready, never hard delete.
- Record exact commands, exit codes, proof IDs, incident IDs, and the self-heal decision
  that did or did not occur.
- If visual/MCP proof is still blocked by tool exposure, record the exact blocker and
  keep command/test proof complete.

## Build-Ready Implementation Spec (46A-46G)

This section turns the behavioral plan (46.0-46.5) into concrete code seams. Every symbol,
signature, and insertion point below was verified against the Stage 45 tree on 2026-06-05
(commit `a5e49b370`). The behavioral substages above say *what*; this section says *which
symbol, where*. Where a verified fact changes the build from what the prose implies, it is
flagged as a **Delta** and overrides the looser earlier wording.

### Verified grounding deltas (apply before coding any substage)

- **Delta A - packets are `payload` sub-objects, not decision siblings.** Every template
  nests its packet inside `decision.payload`: `payload.handoff_packet`, `payload.delivery`,
  `payload.stages[].delivery`, `payload.handoff.delivery`, `payload.qa_review`.
  `decision_schema.DECISION_SCHEMA` (decision_schema.py:47-61) sets `additionalProperties:
  False` only at the top level; `payload` is `{"type": "object"}` with no key restriction,
  and `_validate_raw_decision` (decision_schema.py:202-223) only rejects unknown *top-level*
  keys. **Therefore 46A does not touch the `AgentDecision` dataclass (decision_schema.py:37-44),
  `DECISION_SCHEMA`, `parse_structured_decision` (decision_schema.py:113), or
  `to_decision_jsonable` (decision_schema.py:134).** All packet validation lives inside
  `decision_contracts.validate_planning_decision`. This supersedes any earlier note about
  extending the decision envelope.
- **Delta B - `request_screenshot`/`request_video` already exist.**
  `DecisionType.REQUEST_SCREENSHOT`/`REQUEST_VIDEO` are defined (decision_schema.py:24-25) and
  already in `ALLOWED_DECISIONS_BY_ROLE[AgentRole.QA]` (decision_schema.py:91-92). 46E adds
  only payload contracts in `decision_contracts.py` plus an execution lane - no new decision
  type and no role-allowlist change.
- **Delta C - event payloads are capped at 4096 bytes** (`events.EVENT_PAYLOAD_LIMIT_BYTES`,
  events.py:13; enforced in `EventLog.append`, events.py:61-68). A maximal-template packet
  exceeds this, which originally forced a side store; the **D9** trim shrinks the validated core
  small enough to inline, so per the 2026-06-05 decision packets are carried in the event log
  itself — a `packet.recorded` event holds the core body plus `packet_id`, `packet_type`,
  `content_hash`, `actor`, `stage_id`, and a <=280-char `summary`; no `PacketStore`. If a trimmed
  packet ever nears the cap, truncate the optional free fields, never the validated core.
- **Delta D - Backend/Launcher overlays already live in code, not prompt files.**
  `persona_runtime._specialist_dev_guidance(persona)` (persona_runtime.py:255-280) emits the
  Backend Dev and Launcher Dev overlays inline, and the payload-contract mirror is the inline
  block in `build_system_prompt` (persona_runtime.py:239-249). 46.1/46.2.1 overlay text
  extends `_specialist_dev_guidance`; the shared delivery contract extends the payload-contract
  block. `prompts/dev.md` is the shared base only (confirms C1).
- **Delta E - read/search discipline already exists.**
  `dev_discipline.update_progress_telemetry` (dev_discipline.py:171) already maintains
  `read_search_count` (dev_discipline.py:189) and emits
  `loop_warning="read_search_without_patch_threshold"` (dev_discipline.py:209), and the runner
  is invoked with `stop_on_repeated_read_search=True` for dev/qa (persona_runtime.py:106).
  46.3's "read/search after failed proof" extends these counters; it must not add a parallel
  detector (honors C4's "do not add a parallel stall detector").

### 46A - Packet schemas, persistence, projection

New module `agent_runtime/packets.py`:

- `PACKET_SCHEMA_VERSION = 1`.
- `@dataclass(slots=True) class Packet:` `packet_id, packet_type, packet_version, task_id,
  run_id, stage_id, actor, created_at, source_decision_type, content_hash, redaction_status,
  body: dict`. `packet_type in {"handoff_packet","delivery","qa_review"}`. After D9 the `body`
  is the small underivable core, so the serialized Packet fits the 4096-byte event cap and is
  carried inline in a `packet.recorded` event — there is no separate stored record.
- `content_hash(body: dict) -> str` -> `"sha256:" + sha256(canonical_json(body)).hexdigest()`
  using the **full** 64-char hex (dedup key; do not truncate). `canonical_json` =
  `json.dumps(body, sort_keys=True, separators=(",", ":"))`. (Truncated `[:16]` is reserved
  for the D1 environment fingerprint only, so the two hash uses cannot be confused.)
- `make_packet_id(packet_type, content_hash) -> str` -> deterministic, e.g.
  `f"packet_{packet_type.split('_')[0][:2]}_{content_hash[7:23]}"`, so an identical body maps
  to the same id (idempotent replay per 46.2.3).

Persistence is the existing event log — no new store (2026-06-05 decision). Add one helper
`record_packet(packet: Packet, *, event_log) -> bool` (in `packets.py` or beside the event-log
writer):

- Append a single `packet.recorded` event whose payload is the serialized `Packet` (D9 core
  body plus the 46.2.3 runtime metadata). The trimmed packet fits the 4096-byte cap, so Delta C
  is satisfied without a side file.
- Idempotent: before appending, scan the task/run events for an existing `packet.recorded` with
  the same `content_hash`; if found, append a `packet.duplicate` event instead and return
  `False` (no new progress) so D2/D3 retry and self-heal counters do not advance on replay.
- No `paths.packets_dir`/`packet_path`, no `PacketStore`, no archive migration: packets live in
  `events.jsonl`, which `ArchiveStore` already moves with the task.
- Reads are derived, not stored: `latest_packet(task_id, packet_type, *, stage_id=None)` scans
  the event log newest-first and returns the first match (used by the HUD projection below).

`events.py`: add `"packet.recorded"` and `"packet.duplicate"` to `ALLOWED_EVENT_TYPES`
(events.py:15-57). No `projection_rebuilt` event — the HUD projection is a derived read over the
event log, not a persisted record.

`decision_contracts.py` - validators (new, called from `validate_planning_decision`,
decision_contracts.py:24):

- Add a single generic pre-dispatch block at the top of `validate_planning_decision`: if
  `payload.handoff_packet` / `payload.delivery` / `payload.qa_review` (or nested
  `payload.stages[].delivery`, `payload.handoff.delivery`) is present, route it to the matching
  validator, passing `decision.type` for the D6/D7 cross-checks. This avoids editing every
  branch and covers types with no current branch (e.g. `needs_context`).
- `_validate_handoff_packet(packet, *, decision_type)` - required keys and enum checks per the
  **D9** underivable core (not the maximal template); **D5** accepts
  `parallel_specialists`/`split_child_missions` as known but
  marks them for the `unsupported_handoff_mode` route.
- `_validate_delivery(packet, *, decision_type)` - required keys; enforces the **D6**
  `work_status` <-> decision-type table.
- `_validate_qa_review(packet, *, decision_type)` - required keys; enforces the **D7**
  `next_owner` constraint (cross-stack mismatch/missing-proof forces `neko_supervisor`; never
  `backend_dev`).
- `_reject_unknown_packet_keys(packet, allowed, label)` - mirror the extra-key check in
  `_validate_raw_decision` (decision_schema.py:205-208).
- `_scan_packet_redaction(packet)` - reuse `proof_runner._SECRET_PATTERNS` (proof_runner.py:25)
  and `_ABSOLUTE_PATH_PATTERNS` (proof_runner.py:29) plus the `_is_safe_log_ref_path` ruleset
  (decision_contracts.py:131-139); raise `DecisionPayloadInvalid` on absolute path, secret
  marker, or raw-log block.

Persistence wiring: after a decision passes validation, record its packets. The validation
call site (persona_runtime.py:64-66) already runs `validate_planning_decision` last, so no
call-site change is needed; add the `record_packet(...)` event append in the apply path
(`MissionStateMachine.apply_decision`, state_machine.py:106, or its daemon caller) so the
`packet.recorded` event lands with state mutation (pipeline steps 7-8 of 46.2.3).

`context_builder.py` - HUD/packet projection:

- `AgentContext` (context_builder.py:13-25): add `mission_hud: dict[str, Any] | None = None`,
  `latest_handoff_packet: dict | None = None`, `latest_delivery: dict | None = None`,
  `latest_qa_review: dict | None = None`.
- `build_context(...)` (context_builder.py:28-57): populate the four new fields by scanning the
  task/run event log for the latest `packet.recorded` of each type (the `latest_packet(...)`
  read above), passed through `_safe_packet_projection`. It already receives the run/event
  source, so no new store parameter is threaded through.
- `render_context(ctx)` (context_builder.py:60): insert a `## Mission HUD` section (Neko only,
  gated by role/phase) and a `## Latest Handoff Packet` / `## Latest Delivery` section after the
  `## Task Snapshot` block (context_builder.py:96) and before `## Proof IDs`
  (context_builder.py:130).
- New `_safe_packet_projection(body) -> dict` mirroring `_safe_proof_metadata`
  (context_builder.py:275-298): allow-key filter plus per-field truncation.

Tests: `tests/agent_runtime/test_decision_contracts.py` (NEW per C7) packet required/unknown-key/
redaction/D5/D6/D7; `tests/agent_runtime/test_events.py` packet event idempotency — a duplicate
`content_hash` appends `packet.duplicate` and signals no new progress;
`tests/agent_runtime/test_context_builder.py` HUD + projection + rebuild-after-resume from events.

### 46B - Skills and bounded resolution

Skill packages authored as versioned repo fixtures first, then installed into the runtime
profile skills root:

- source of truth: `docs/agent-runtime-harness/stage46-skills/<skill-name>/SKILL.md`
- installed copy: `HERMES_HOME/skills/<skill-name>/SKILL.md`
  (`X:\Eternia\.hermes\profiles\alice\skills` for Tony's live profile)

Do not create `agent_runtime/skills/`. The Stage 46 packages are:

`harness-mission-lead` (Neko), `harness-dev-delivery` (Dev),
`launcher-analyze-proof` (Launcher Dev focused analyze/proof selection),
and `harness-qa-verdict` (QA).

Install/readiness rules:

- Add an idempotent install helper or CLI path that copies the repo fixtures into the
  active profile and records/verifies source and installed content hashes.
- Persona readiness fails with `missing_skill` or `skill_hash_mismatch` until the installed
  copy exists and matches the repo source.
- Local profile skills may be used at runtime, but Stage 46 acceptance is based on the
  committed repo fixtures plus installation verification, not untracked local profile files.

`agent_runtime/personas.py`:

- `dev` and `backend_dev` `skills=[...]`: append `"harness-dev-delivery"`.
- `dev` only: append `"launcher-analyze-proof"` after the shared delivery skill so
  Launcher smoke, `flutter analyze`, `launcher_contract_smoke`, Stage 47 burn-in, and
  no-edit contract proof stages get narrow analyze/proof-command guidance without
  loading backend-only skills.
- `qa` persona: add a `skills=["harness-qa-verdict"]` field (it has none today - C6).
- `neko_supervisor` persona: add `skills=["harness-mission-lead"]` (none today - C6).
- These activate through `skill_context.load_persona_skill_context` (24k cap) and render via
  `_recommended_skill_guidance` (persona_runtime.py:219,283), both of which already no-op on an
  empty list, so adding the field is the activation.

Tests: `test_persona_skill_policy` asserts `qa`/`neko_supervisor` expose the new skills and that
readiness resolves them from a temporary `HERMES_HOME` populated by the repo fixtures;
`test_persona_prompts` asserts search-first / one-skill-default wording. No Stage 46 test may
depend on the live `alice` profile having local skills already installed.

### 46C - Preflight and dynamic readiness

New `agent_runtime/preflight.py`:

- `@dataclass(slots=True) class PreflightCheck:` `id, ok: bool, token: str, detail: str,
  actionable_fix: str` (token is the coarse D1 identity, e.g. `docker_engine=up|down`).
- `@dataclass(slots=True) class PreflightResult:` `checks: list[PreflightCheck], ok: bool,
  environment_fingerprint: str, blocker: dict | None, proof_payload: dict | None`.
- `run_preflight(task, *, stage=None, persona_target: str) -> PreflightResult` - runs only the
  checks implied by scope: Docker when backend proof implies compose; Flutter when
  Launcher/visual; backend venv import; runtime-root/profile pinning; repo cleanliness
  (reported, not blocking); Stage C MCP exposure when visual proof is required (C5).
- `environment_fingerprint(checks) -> str` (**D1**): `sha256(("env\0" + "\0".join(sorted(
  f"{c.id}={c.token}" for c in checks))).encode()).hexdigest()[:16]`, matching the `[:16]`
  convention of `context_requests._fingerprint`.

Wiring: the daemon runs `run_preflight` before dispatching a dependency-sensitive Dev/QA model
run. On failure it first attaches a redaction-safe preflight proof record, then opens a
proof-backed `environment_blocker` incident via `IncidentStore.open` (store.py:700) - not a
model call - and does not consume Dev retry budget. (Preferred over a new `HarnessActionType`,
to keep `next_action` a pure read; if an action type is added instead, extend `actions.py` and
`state_machine.next_action`.)

Preflight blocker proof linkage:

- Create a `ProofType.LOG` or dedicated preflight proof if one already exists by implementation
  time; otherwise use `ProofType.LOG` with `metadata.kind="preflight"`.
- Proof metadata must include `check_id`, `ok=false`, `environment_fingerprint`,
  `fingerprint_status`, compact `detail`, `actionable_fix`, and the blocked `persona_target`.
- The incident payload must include `proof_id`, `check_id`, `environment_fingerprint`,
  `blocking_event_id`, `stage_id`, and `persona_target`.
- Status/snapshot stopped-progress fields must point to that `blocking_event_id` and
  `proof_id`; an environment blocker without a proof link is invalid.

`agent_runtime/profile_readiness.py` (**46.2.4 dynamic MCP**):

- Change signature to `profile_readiness_for_persona(persona, *, task=None, stage=None)`.
- Compute `effective = list(persona.required_mcp_servers)`; if `_visual_proof_required(task,
  stage)` (task.requires_visual_proof / stage.requires_visual_proof / visual scope), append
  `"launcher_qa"` when absent. Compute `missing_mcp` against `effective`. Return both
  `required_mcp_servers` (static) and `effective_required_mcp_servers` (profile_readiness.py:72-80).
- Callers `status._agent_status` (status.py:91-98) and `snapshot._agent_summary`
  (snapshot.py:362-381) pass task/stage where in scope and surface both lists.

Tests: `test_preflight` (Docker-required-down blocks before dispatch; no budget consumption; D1
fingerprint stable across volatile noise and changes on a real dependency change);
`test_profile_readiness` (`launcher_qa` injected only when visual scope present);
`test_state_machine`.

### 46D - Proof-aware retry and self-heal gates

`agent_runtime/models.py` (**D8**): add `harness_self_heal: dict[str, Any] =
field(default_factory=dict)` to `Task` (models.py:12-38; today it carries only `risk_flags`).
Shape, keyed by `stage_id`: `{last_failed_proof_ids, last_environment_fingerprint, self_heal:
{key, classification, attempt_number, attempts_remaining}, counters: {scope_update_count,
same_stage_retry_count, dev_read_search_after_failed_proof}}`. Run-scoped counters stay in
`AgentRun.progress` (models.py:103), which `dev_discipline` already populates.

`agent_runtime/state_machine.py` - the BLOCKED, no-incident region of `next_action`
(state_machine.py:42-51) is the retry authority. The existing
`_has_failed_current_stage_test_proof -> RUN_DEV` (state_machine.py:43-44) becomes gated:

- Authorize `RUN_DEV` only when (**D1**) the environment fingerprint changed, or (**D2**) an
  applied self-heal exists, or the retry command is narrower/different and references the failed
  proof ids.
- Else, when (**D3**) `same_stage_retry_count >= 1` with no changed signal, return
  `RUN_NEKO_SUPERVISOR` (self-heal) instead of blind Dev retry.
- New helpers beside `_has_failed_current_stage_test_proof` (state_machine.py:179):
  `_environment_fingerprint_changed(mission)`, `_self_heal_budget_remaining(mission, key)`
  (**D2**: 2 per `(task, stage, classification)`; at 0 only `cannot_self_heal` is legal),
  `_same_stage_retry_blocked(mission)`.

`agent_runtime/dev_discipline.py`:

- `validate_dev_progress_gate` (dev_discipline.py:113): add a proof-reuse clause - if the
  current stage has `last_failed_proof_ids` and the decision is a same-stage `request_test_run`
  that neither references them nor rides a changed fingerprint, raise `DecisionPayloadInvalid`
  steering to reuse the failed proof ids or block (reuses `_decision_has_proof_ids`,
  dev_discipline.py:165).
- `update_progress_telemetry` (dev_discipline.py:171): when the stage's prior proof failed,
  increment `dev_read_search_after_failed_proof` on read/search tools so the **D3** `>= 1`
  threshold is observable.

Tests: `test_state_machine` (fingerprint-changed allows exactly one retry; unchanged routes to
Neko; D2 third attempt forced to `cannot_self_heal`); `test_dev_discipline` (proof-reuse gate;
counter).

### 46E - Visual/MCP proof execution

`agent_runtime/decision_contracts.py`: add `REQUEST_SCREENSHOT`/`REQUEST_VIDEO` branches
(Delta B) requiring `stage_id, target, proof_requirement, mcp_server, required_launch_pins`;
video also requires `duration_seconds` and `interaction_script`; reuse `_validate_qa_review`
for the nested `qa_review`.

Capture lane: reuse the existing visual capture seam in `agent_runtime/proof_capture.py`
(`VisualCaptureProvider`, `ScreenshotRequest`, `VideoRequest`, `CapturedArtifact`) and add the
minimum runner/adapter needed to convert captures into Harness `Proof` records. `CommandProofRunner`
(proof_runner.py:56) only emits `ProofType.TEST_RUN` (proof_runner.py:157); add
`VisualProofRunner` only as an adapter around the existing protocols, either in `proof_runner.py`
or `agent_runtime/visual_proof.py`, not as a parallel capture abstraction:

- `capture(task, *, stage_id, run_id, actor, request: dict, mcp_client) -> Proof` producing
  `ProofType.SCREENSHOT`/`VIDEO`; record capture tool name, MCP result status, artifact path,
  runtime root, profile, and a compact semantic summary; verify the artifact is non-empty and
  tied to the pinned runtime-root/profile; a blank / stale / wrong-profile / wrong-root capture
  sets `status="failed"`; attach via `ProofStore.attach` (store.py:559) with
  `redaction_status="safe"`.
- When `launcher_qa` / Stage C MCP is unavailable, return a proof-backed `environment_blocker`
  (no soft warning).
- `state_machine._has_visual_proof` (state_machine.py:130-141) already gates terminal close on
  `{"screenshot","video"}` + `redaction_status=="safe"` + non-empty `path_or_value`; the capture
  must satisfy exactly that.

Tests: `test_decision_contracts` (screenshot/video payloads); `test_proof_capture` or
`test_proof_runner` (blank/stale/wrong-root fails through the existing capture protocol;
env-blocker on missing MCP); `test_profile_readiness`; `test_state_machine`
(visual-required cannot close without safe visual proof).

### 46F - Observability and stopped-progress reasons

Counters (from `harness_self_heal` and `AgentRun.progress`) are rolled up in
`observability.build_observability(...)`, already called by `status.build_status`
(status.py:53) and `snapshot.build_snapshot` (snapshot.py:63): add a `self_heal` block -
`scope_update`, `same_stage_retry`, `read_search_after_failed_proof`, `skill_fanout`,
`failed_proof_reused_vs_ignored`, `env_fingerprint_changed`,
`self_heal_proposed/applied/cannot`.

Stable stopped-progress reason (one of `waiting_for_preflight | environment_blocked |
self_heal_pending | retry_authorized | waiting_for_proof_capture | qa_blocked_missing_proof |
terminal_human_required | settled`) with `owner, stage_id, blocking_event_id,
related_proof_ids, next_action`: extend `status._next_action` (status.py:69-75) and
`snapshot._next_action_summary` (snapshot.py:319-325) / `snapshot._why_not_done`
(snapshot.py:328-338). Route every new string through `snapshot._safe_text` /
`_looks_sensitive_or_pathish` (snapshot.py:447-466) so no raw path or secret leaks.

Tests: `test_status` and `test_snapshot` (counters present; every non-settled task carries a
stopped reason with owner/stage/blocking event/proof ids; output contains no absolute path or
secret marker).

### 46G - Real-token smoke and cleanup

No new code. Run the required sequence (Neko -> Backend Dev -> Neko join -> Launcher Dev -> QA)
per 46.5; clean disposable goals only through cancel / archive-ready / archive
(`TaskStore.cancel` store.py:123, `ArchiveStore.archive_ready` store.py:152); record exact
blockers, proof ids, incident ids, and the self-heal decision that did or did not occur.

### Build order dependency note

46A (packets) and 46C (preflight/readiness) are independent and can land in parallel. 46B
depends on 46A only for the delivery/qa_review/handoff contract the skills describe. 46D depends
on 46A (the `packet.duplicate` idempotency backstops D2's no-double-decrement rule) and 46C (the D1 fingerprint). 46E
depends on 46C (dynamic `launcher_qa` readiness). 46F reads everything and lands last before the
46G smoke.

## Test Coverage Additions

Stage 46 is not accepted without tests for:

- preflight blocks Dev dispatch when Docker is required and unavailable.
- preflight does not consume Dev retry budget.
- environment fingerprint change allows exactly one proof-aware retry.
- unchanged environment plus same failed proof blocks or routes to Neko self-heal.
- (D1) identical environments produce an identical `environment_fingerprint`; a changed
  dependency token changes it, and volatile noise (paths, pids, timestamps) does not.
- (D2) self-heal decrements per applied action, a duplicate packet does not decrement,
  and the third attempt for one blocker key is forced to `cannot_self_heal`.
- (D3) the scope-update, same-stage-retry, read/search-after-failed-proof, and skill
  fanout counters trip recovery at the specified thresholds.
- (D5) reserved `parallel_specialists`/`split_child_missions` modes validate as known
  enum values but route to `unsupported_handoff_mode` recovery.
- (D6) a `delivery.work_status` that disagrees with its decision type fails validation.
- (D7) QA cross-stack contract-mismatch/missing-proof verdicts force
  `next_owner=neko_supervisor` and cannot assign `backend_dev` directly.
- Dev prompt contains failed-proof reuse instructions.
- Dev/Neko prompts require search-first bounded skill loading, default skill use for
  non-trivial ticks, and no bulk skill preload.
- Dev persona prompts include the shared `harness-dev-delivery` template contract and
  the correct Backend/Launcher specialist overlays.
- Neko prompt/HUD contains typed handoff fields and beginning-only wait semantics.
- Neko backend-first handoff packet routes Backend Dev before Launcher Dev.
- QA prompt includes `harness-qa-verdict` as the default non-trivial QA skill and uses
  `launcher-stagec-mcp-screenshot` only when visual/MCP proof is required.
- QA verdict schema requires `qa_review` metadata for approved, needs-fixes, blocked,
  missing-proof, visual/MCP-blocked, and contract-mismatch decisions.
- QA cannot approve cross-stack work unless backend proof, Launcher proof, and required
  visual/MCP proof are represented or explicitly marked `not_required` by Neko with a
  reason.
- QA `request_test_run`, `request_screenshot`, and `request_video` decisions request
  one bounded missing proof lane at a time.
- QA readiness reports `launcher_qa` missing when Mission Control visual/MCP proof is in
  scope.
- optional `handoff_packet`, `delivery`, and `qa_review` objects are validated after
  the base `AgentDecision` contract and before state mutation.
- packet validators reject unknown keys, absolute paths, secret-bearing paths, raw log
  blocks, and credential-looking strings.
- packet events are append-only, redaction-safe, content-hashed, and idempotent on
  replay.
- latest Neko, Dev, and QA packets project into HUD/context after daemon restart or
  task resume.
- `request_screenshot` and `request_video` payloads are validated and either attach
  visual proof IDs or create proof-backed MCP environment blockers.
- blank, stale, wrong-profile, or wrong-runtime-root visual captures fail proof.
- status/snapshot expose static and dynamic MCP readiness separately.
- repeated Neko scope updates trigger recovery rather than infinite scope loops.
- repeated Dev read/search after failed proof is observable and actionable.
- repeated `skill_view`/skill fanout after the relevant skill is loaded is observable
  and actionable.
- status/snapshot expose stopped-progress reasons and the new counters without leaking
  raw local paths or secrets.

Run at minimum:

- `python -m pytest -o addopts='' -q tests/agent_runtime/test_decision_contracts.py tests/agent_runtime/test_preflight.py tests/agent_runtime/test_profile_readiness.py tests/agent_runtime/test_events.py tests/agent_runtime/test_dev_discipline.py tests/agent_runtime/test_persona_skill_policy.py tests/agent_runtime/test_persona_prompts.py tests/agent_runtime/test_context_builder.py tests/agent_runtime/test_proof_capture.py tests/agent_runtime/test_proof_runner.py tests/agent_runtime/test_state_machine.py tests/agent_runtime/test_status.py tests/agent_runtime/test_snapshot.py`
- `python -m pytest -o addopts='' -q tests/agent_runtime`

## Stage 46 Definition of Done

- A deterministic preflight gate runs before live Dev dispatch for dependency-sensitive
  stages.
- Dev personas are proof-aware and cannot ignore attached failed proof IDs during
  same-stage retry.
- Harness retry rules prevent repeated same-stage Dev loops unless the environment,
  self-heal state, or proof command materially changed.
- Neko has a mission-lead skill/HUD/template path that emits stable backend-first,
  join-release, QA-release, recovery, and cannot-self-heal packets.
- Dev personas use `harness-dev-delivery` templates for stage planning, proof requests,
  proof-aware retries, QA handoff, and blockers.
- QA uses `harness-qa-verdict` templates for approval, needs-fixes, blocked,
  missing-proof, visual/MCP-blocked, and contract-mismatch verdicts.
- Stage 46 skills have committed repo fixtures, install deterministically into the active
  profile, and readiness fails if installed skills are missing or hash-mismatched.
- QA readiness and proof gates prevent visual/MCP or cross-stack false approval when
  required proof is missing.
- Preflight environment blockers always carry a redaction-safe proof ID and blocking event
  ID before any incident is surfaced.
- Neko, Dev, and QA packets are persisted, redaction-safe, idempotent on replay, and
  projected back into context after resume/crash recovery.
- `request_screenshot` and `request_video` are executable proof lanes with validators,
  artifact checks, and proof-backed environment blockers.
- Dynamic MCP readiness requires `launcher_qa` exactly when visual/MCP scope requires
  it, while generic QA work remains unblocked by Launcher-specific MCP absence.
- Status/snapshot include counters that explain repeated Neko and Dev behavior.
- Status/snapshot explain every frozen-looking state with owner, stage, blocker event,
  related proof IDs, and next deterministic action.
- A final real-token smoke either completes the full Neko -> Backend Dev -> Neko join
  release -> Launcher Dev -> QA sequence or blocks with proof-backed, self-heal-aware,
  operator-actionable evidence.
- Runtime cleanup is complete after the smoke: no unintended open tasks, active runs,
  or open task-scoped incidents.
