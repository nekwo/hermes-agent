# Stage 59 - HUD / Skill Contract Split

Status: implementation-ready

## Goal

Make the Mission HUD a compact live control panel and make role skills the full
operating manuals.

Agents should not infer packet fields from prose or receive a giant mixed HUD
every turn. They should see a small closed-choice menu, the exact compact shape
for the recommended action, and a pointer to the one relevant skill that contains
the full context, examples, and edge-case rules.

## Product Decision

Use this split:

- HUD = current state, valid options, recommended option, compact shape, validation
  feedback, and relevant skill reference.
- Skill = full context, full packet shapes, examples, decision rules, recovery
  rules, anti-patterns, and proof/QA/Dev/Neko handoff details.

This keeps normal turns fast and deterministic while still letting Neko, Dev, and
QA pull deeper guidance when the decision is non-trivial.

Stage 59 removes legacy worker-facing contract surfaces. Compatibility fields may
exist inside saved snapshots or debug artifacts during migration, but they are no
longer valid prompt input, no longer valid live-agent choices, and no longer part
of the Mission Control primary display.

## Current-Code Audit

The current Harness already has several pieces Stage 59 should reuse rather than
replace:

- `agent_runtime/context_builder.py`
  - builds `mission_hud`;
  - exposes `agent_hud`;
  - builds `decision_menu`, `next_required_move`, `context_expansion_menu`,
    `validation_repair`, and `terminal_feedback`;
  - attaches `recommended_payload` for some primary choices.
- `agent_runtime/decision_contract_registry.py`
  - owns canonical `DecisionContract` and `HudShape` data;
  - exposes allowed payload keys, required payload keys, nested requirements,
    enum choices, and payload templates.
- `agent_runtime/worker_actions.py`
  - chooses visible role actions for normal worker flow.
- `agent_runtime/autonomy.py`
  - compacts Mission HUD for autonomy packets and selects role skills.
- `agent_runtime/persona_runtime.py`
  - tells personas to treat Mission HUD as a closed multiple-choice contract.
- `docs/agent-runtime-harness/stage46-skills/*/SKILL.md`
  - already contain role guidance, but they still duplicate shape rules and
    should become generated/registry-checked where possible.
- Tests already cover many HUD/registry paths:
  - `tests/agent_runtime/test_context_builder.py`
  - `tests/agent_runtime/test_decision_contract_registry.py`
  - `tests/agent_runtime/test_decision_contracts.py`
  - `tests/agent_runtime/test_persona_diagnostics.py`

Do not build a second HUD, packet registry, or skill registry. Stage 59 is a
projection and contract-cleanup stage over the current code.

## Problems To Close

### 1. HUD Still Mixes Control Surface And Debug Manual

`mission_hud` currently contains both the simplified `agent_hud` and the legacy
debug/compat fields. This is useful for Harness implementation work, but it gives
live agents too much surface area and encourages field invention.

Target: remove legacy/debug detail from the prompt-visible worker HUD. If the
Harness needs migration evidence, keep it under non-prompt `debug_hud` in
snapshots only. Live agents must receive only the compact `agent_hud` contract.

### 2. Recommended Action Shape Is Not Uniform

Some actions include `recommended_payload`; some only include allowed keys or
shape IDs. That leaves agents to infer details from the skill or old examples.

Target: every primary/recommended action has a compact executable skeleton with
valid required fields and safe enum values.

### 3. Skills Duplicate Shape Truth

Role skills currently teach shapes manually. This creates drift risk whenever
the registry changes.

Target: skills may contain expanded examples and decision policy, but the field
sets and payload skeletons must be checked against the canonical registry.
Remove legacy packet examples from skills instead of keeping them as fallback
patterns.

### 4. Skill Use Is Not Explicit Enough In The HUD

The autonomy packet selects skills, but the HUD itself does not clearly say:

- which skill is relevant for this action;
- when to open it;
- when not to load more skills.

Target: every recommended action includes `skill_ref`, `skill_section`, and
`skill_reason`.

### 5. Invalid Output Feedback Should Return The Correct Shape

`validation_repair` exists, but repair feedback should behave like terminal
stderr: exact error, exact allowed action, exact corrected shape, and retry
boundary.

Target: invalid packet/decision repair HUD includes the corrected compact shape
for the visible retry action.

### 6. Token Budget Needs A Hard HUD Budget

The HUD should not grow into a hidden prompt tax as the registry grows.

Target: normal prompt HUD has a measured max size and omits full examples. Full
examples live in skills.

## Target HUD Shape

The normal worker-facing HUD should look like this conceptually:

```json
{
  "schema_version": 1,
  "mode": "compact_control_panel",
  "persona": {
    "persona_id": "neko_supervisor",
    "role": "alice_supervisor",
    "display_name": "Neko Mission Lead"
  },
  "assignment": {
    "task_id": "task_xxx",
    "stage_id": "neko_diagnostic",
    "repo_bundle_id": null,
    "objective": "Bounded current objective.",
    "acceptance": ["One concrete criterion."]
  },
  "state": {
    "task_state": "created",
    "stage_status": "ready",
    "next_expected": "assign_scope"
  },
  "options": [
    {
      "choice_id": "A",
      "label": "Assign Scope",
      "action_id": "assign_scope",
      "decision_type": "propose_acceptance",
      "shape_id": "neko.scoped_handoff",
      "primary": true
    },
    {
      "choice_id": "B",
      "label": "Report Blocker",
      "action_id": "report_blocker",
      "decision_type": "block",
      "shape_id": "common.block",
      "primary": false
    }
  ],
  "recommended_action": {
    "choice_id": "A",
    "action_id": "assign_scope",
    "decision_type": "propose_acceptance",
    "shape_id": "neko.scoped_handoff",
    "required_payload_keys": ["objective", "acceptance_criteria", "handoff_packet"],
    "allowed_payload_keys": ["objective", "acceptance_criteria", "non_goals", "affected_repos", "handoff_packet", "release_stage_id"],
    "payload_skeleton": {
      "objective": "<bounded next objective>",
      "acceptance_criteria": ["<proof-backed completion criterion>"],
      "affected_repos": ["hermes-agent|EterniaLauncher|EterniaBackend"],
      "handoff_packet": {
        "packet_kind": "fresh_scope",
        "mission_phase": "<phase>",
        "handoff_mode": "single_specialist",
        "target_owner": "dev|backend_dev|qa|neko_supervisor|human",
        "target_repo": "hermes-agent|EterniaLauncher|EterniaBackend",
        "proof_gate": {
          "required": true,
          "required_proof_types": ["test_run"],
          "minimum_status": "passed",
          "visual_required": false
        },
        "join_gate": {
          "release_condition": "<what allows the next owner to proceed>"
        }
      }
    },
    "skill_ref": "harness-mission-lead",
    "skill_section": "Scoped Handoff",
    "skill_reason": "Open only if the next owner/repo/proof gate is not obvious."
  },
  "feedback": {
    "last_error": null,
    "last_terminal_feedback": null,
    "retry_rule": "If validation fails, use the returned corrected shape once; then block with exact feedback."
  }
}
```

Rules:

- `options` lists every valid visible action.
- `recommended_action` expands exactly one compact executable shape.
- Full variants and examples stay in the skill.
- Unknown fields remain invalid.
- The HUD must be enough for a simple valid response without skill loading.
- The skill must be enough for complex decisions without guessing.

## Skill Responsibilities

### `harness-mission-lead`

Owns full Neko guidance:

- initial scope;
- one repo vs multi-repo split;
- backend-first cross-stack;
- QA release;
- bounded recovery;
- incident resolution;
- diagnostic self-observation;
- when to steer Dev/QA;
- when to request missing input;
- when to block;
- alternatives after goal completion.

### `harness-dev-delivery`

Owns full Dev guidance:

- inspect narrow context;
- patch;
- no-edit investigation delivery;
- self-test inside the session;
- request proof recipe;
- report blocker;
- handoff through delivery packet;
- reuse failed proof IDs;
- one bounded repair;
- repo ownership and dirty-state rules.

### `harness-qa-verdict`

Owns full QA guidance:

- review delivery/proof packets;
- approve;
- reject/needs-fixes;
- request missing proof;
- visual/MCP proof requirements;
- packet freshness checks;
- proof strength classification;
- safe blocker reporting.

## Implementation Stages

### 59A - HUD Field Map And Prompt Boundary

Files:

- `agent_runtime/context_builder.py`
- `agent_runtime/autonomy.py`
- `agent_runtime/persona_runtime.py`
- `tests/agent_runtime/test_context_builder.py`
- `tests/agent_runtime/test_autonomy.py`

Work:

- Define `agent_hud` as the only normal worker control panel.
- Remove legacy/debug contract details from compact autonomy packets.
- Keep migration/debug artifacts only in non-prompt snapshot metadata when needed
  for operator evidence.
- Remove legacy `decision_menu`, `next_required_move`, and compatibility packet
  surfaces from worker prompt payloads after `recommended_action` is available.
- Ensure Mission Control reads the compact contract first and treats any legacy
  snapshot fields as debug-only.

Tests:

- Normal Launcher/Backend/QA/Neko tasks render compact `agent_hud`.
- Compact autonomy packet excludes full `decision_shape_index`, long examples,
  legacy `decision_menu`, and legacy `next_required_move`.
- Snapshot debug metadata, if present, is not included in persona prompt payloads.

Acceptance:

- A normal live worker sees one small control panel and no legacy action surface.

### 59B - Canonical Recommended Action Projection

Files:

- `agent_runtime/decision_contract_registry.py`
- `agent_runtime/context_builder.py`
- `agent_runtime/worker_actions.py`
- `tests/agent_runtime/test_decision_contract_registry.py`
- `tests/agent_runtime/test_context_builder.py`

Work:

- Add a registry helper that projects any `HudShape` into:
  - compact option row;
  - recommended action;
  - payload skeleton.
- Use registry payload templates as skeleton source.
- Overlay context-specific values only where deterministic, such as current
  `stage_id`, current `repo_bundle_id`, exact proof recipe, exact failed proof ID.
- Ensure every primary worker action has `recommended_action.payload_skeleton`.

Tests:

- Every role's primary HUD action has:
  - `decision_type`;
  - `shape_id`;
  - `required_payload_keys`;
  - `allowed_payload_keys`;
  - `payload_skeleton`;
  - `skill_ref`.
- The skeleton validates against the decision contract after placeholders are
  replaced with safe test values.

Acceptance:

- Agents no longer need to infer required packet fields for the recommended move.

### 59C - Skill Reference And Skill Budget Policy

Files:

- `agent_runtime/autonomy.py`
- `agent_runtime/context_builder.py`
- `agent_runtime/config.py`
- `tests/agent_runtime/test_autonomy.py`
- `tests/agent_runtime/test_persona_skill_policy.py`

Work:

- Add `skill_ref`, `skill_section`, and `skill_reason` to recommended actions.
- Keep default selected skills at one Harness role skill unless the stage context
  requires a product-specific skill.
- Add prompt rule:
  - read HUD first;
  - answer from HUD for simple cases;
  - open exactly the `skill_ref` when the action is complex;
  - do not load unrelated skills.

Tests:

- Neko recommended actions reference `harness-mission-lead`.
- Dev recommended actions reference `harness-dev-delivery`.
- QA recommended actions reference `harness-qa-verdict`.
- Skill selection does not include unrelated skills for the Neko diagnostic smoke.

Acceptance:

- Token cost remains bounded and role guidance is reachable on demand.

### 59D - Role Skill Full Shape Sections

Files:

- `docs/agent-runtime-harness/stage46-skills/harness-mission-lead/SKILL.md`
- `docs/agent-runtime-harness/stage46-skills/harness-dev-delivery/SKILL.md`
- `docs/agent-runtime-harness/stage46-skills/harness-qa-verdict/SKILL.md`
- `agent_runtime/decision_contract_examples.py`
- `tests/agent_runtime/test_decision_contract_examples.py`
- `tests/agent_runtime/test_decision_contract_registry.py`

Work:

- Update each skill with explicit sections matching HUD `skill_section` values.
- Move full packet variants and examples into those sections.
- Remove legacy instructions that tell agents to manage internal state-machine
  mechanics directly.
- Remove legacy packet/action names that are not accepted by the current registry.
- Add a generated or registry-checked shape table for each skill.

Tests:

- Every `skill_section` referenced by HUD exists in the role skill.
- Every shape ID listed in a skill exists in the registry.
- Every payload field shown in a skill is allowed by the registry or packet body
  registry.

Acceptance:

- Skills become the source of deep procedural guidance without drifting from the
  canonical contracts.

### 59E - Validation Feedback As Corrected Shape

Files:

- `agent_runtime/context_builder.py`
- `agent_runtime/persona_runtime.py`
- `agent_runtime/ticker.py`
- `agent_runtime/packets.py`
- `tests/agent_runtime/test_context_builder.py`
- `tests/agent_runtime/test_ticker.py`

Work:

- Extend `validation_repair` to include:
  - exact validation error;
  - previous invalid decision type;
  - allowed retry action;
  - corrected compact payload skeleton;
  - relevant `skill_ref`;
  - retry counter and block boundary.
- Ensure packet-specific errors return packet-specific skeletons.
- Preserve raw invalid payload evidence where available.

Tests:

- Unsupported key error returns corrected skeleton without unsupported key.
- Missing required packet field returns skeleton with that field.
- Repeated same malformed output blocks cleanly instead of infinite repair.

Acceptance:

- Invalid outputs behave like terminal feedback: exact, actionable, bounded.

### 59F - Mission Control Rendering Contract

Files:

- Launcher Mission Control renderer files.
- `agent_runtime/snapshot.py`
- `agent_runtime/status.py`
- Launcher tests.

Work:

- Render the compact HUD as the primary live panel.
- Hide legacy/debug HUD from the normal agent/operator workflow. If debug metadata
  is still present in snapshots, expose it only behind an explicit developer
  diagnostics toggle.
- Show `skill_ref`, `shape_id`, selected option, and compact payload skeleton.
- Show validation feedback like terminal stderr.

Tests:

- Snapshot exposes compact HUD fields.
- Mission Control renders option list and recommended shape.
- Fullscreen screenshot proof verifies no blocky legacy dump replaces the compact
  view.

Acceptance:

- Operators can see what options the agent had, what shape it used, and what skill
  it should open.

### 59G - Live Token Certification

Commands:

```powershell
python -m hermes_cli.main harness persona diagnose neko --title "Stage 59 Neko HUD skill split smoke" --message "<bounded diagnostic>" --operation-kind stage59_hud_skill_split --operation-mode neko_only_token_burn --max-actions 1 --max-seconds 180 --affected-repo hermes-agent --json
python -m hermes_cli.main harness persona diagnose launcher-dev --title "Stage 59 Launcher Dev HUD skill split smoke" --message "<bounded diagnostic>" --operation-kind stage59_hud_skill_split --operation-mode dev_only_token_burn --max-actions 1 --max-seconds 180 --affected-repo EterniaLauncher --json
python -m hermes_cli.main harness persona diagnose backend-dev --title "Stage 59 Backend Dev HUD skill split smoke" --message "<bounded diagnostic>" --operation-kind stage59_hud_skill_split --operation-mode backend_dev_only_token_burn --max-actions 1 --max-seconds 180 --affected-repo EterniaBackend --json
python -m hermes_cli.main harness persona diagnose qa --title "Stage 59 QA HUD skill split smoke" --message "<bounded diagnostic>" --operation-kind stage59_hud_skill_split --operation-mode qa_only_token_burn --max-actions 1 --max-seconds 180 --affected-repo EterniaLauncher --json
```

Certification requirements:

- exactly one intended persona runs;
- no unintended Dev/QA/Neko owner launch;
- no malformed packet repair loop in happy path;
- `skill_ref` is visible in the HUD/autonomy packet;
- recommended skeleton validates;
- raw packet artifacts archive successfully;
- status ends with no active/stale runs.

## Deep Audit: Implementation Readiness

### Existing Code Reuse

Ready:

- `HudShape` already has labels, required keys, allowed keys, templates, enum
  choices, and nested requirements.
- `context_builder._decision_menu` and `_worker_action_decision_menu` already
  project registry shapes into HUD menu rows.
- `next_required_move` already provides context-specific recommended payloads.
- `autonomy._compact_mission_hud` already reduces the prompt packet.
- Role skills are installed/configured for all personas.

Needed:

- One canonical helper to avoid different code paths producing slightly different
  menu rows.
- A first-class `recommended_action` object. Today the recommendation is split
  across `next_required_move`, `primary_worker_action`, and
  `decision_menu[0].recommended_payload`.
- A consistent `payload_skeleton` name. Today the field is usually
  `recommended_payload`, which blurs example/template vs concrete payload.

### Risk Audit

Low risk:

- Adding `recommended_action` and migrating prompt consumers to it.
- Adding `skill_ref` to HUD rows.
- Adding tests that compare skill shape references to registry shape IDs.
- Moving full examples from prompt HUD into skills.

Medium risk:

- Removing legacy prompt fields may expose hidden dependencies in autonomy,
  Mission Control rendering, or persona diagnostics. Close this with focused
  tests before live token burn, not by keeping legacy fields in prompts.
- Generated skill sections can become noisy. Start with registry validation
  against curated skill text before introducing full doc generation.

High risk / defer unless needed:

- Removing persisted historical snapshot fields from disk.
- Replacing packet validation with a new schema engine.
- Changing Mission Control UI and Harness prompt schema in one untested commit.
- Making skills load automatically every turn. The desired behavior is on-demand
  deep guidance, not a larger default prompt.

### Test Coverage Required Before Live Burn

Minimum focused suite:

```powershell
python -m pytest -o addopts= tests/agent_runtime/test_context_builder.py tests/agent_runtime/test_decision_contract_registry.py tests/agent_runtime/test_decision_contracts.py tests/agent_runtime/test_autonomy.py tests/agent_runtime/test_persona_skill_policy.py tests/agent_runtime/test_persona_diagnostics.py -q
```

Do not claim Stage 59 complete if:

- live persona prompts still include legacy `decision_menu`, `next_required_move`,
  or full `decision_shape_index`;
- a recommended action lacks a payload skeleton;
- a HUD references a skill section that does not exist;
- a skill contains packet fields the registry rejects;
- invalid output does not return a corrected shape;
- live Neko diagnostic uses schema repair on the happy path;
- live Dev/QA diagnostics load broad unrelated skills for a simple diagnostic.

## Acceptance Criteria

Stage 59 is complete when:

- HUD lists all valid visible options.
- HUD expands exactly one recommended compact shape.
- HUD includes `skill_ref`, `skill_section`, and `skill_reason`.
- Worker prompts contain no legacy action surface.
- Skills contain full role context and full right shapes.
- Registry tests prevent HUD/skill shape drift.
- Invalid payload feedback returns a corrected shape immediately.
- Neko, both Devs, and QA can each complete a single-persona live token smoke
  without malformed packet babysitting.
- Mission Control can render the compact HUD and hide debug detail behind an
  expansion.

## Implementation Order

1. Add `recommended_action` projection to `agent_hud`.
2. Add `skill_ref` metadata to registry/HUD projection.
3. Update role skills with matching `skill_section` headings and full shapes.
4. Add registry/skill drift tests.
5. Tighten autonomy prompt compaction to prefer `agent_hud.recommended_action`
   and remove legacy/debug fields for normal tasks.
6. Add validation repair corrected-shape projection.
7. Update Mission Control to render compact HUD and hide debug metadata behind
   developer diagnostics only.
8. Run focused tests.
9. Run single-persona live smokes.
10. Implement fullscreen screenshot proof.
