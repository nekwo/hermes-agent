# Stage 49 Canonical Decision Contract Registry and HUD Schema Generation

Date: 2026-06-06
Owner: Codex independent Harness investigation
Status: implemented in Harness code; live-token certification not run in this pass

## Purpose

Stage 49 eliminates drift between agent decision validation, Mission HUD multiple-choice
menus, repair hints, prompts, skills, and Mission Control display metadata.

The product goal is simple: when Neko, Dev, Backend Dev, or QA needs to emit a Harness
decision, the agent should see a closed set of valid moves and valid field shapes, and
the Harness should validate that exact same contract. No hand-written prompt template
should claim a field is valid unless the runtime validator and HUD generator agree.

This stage follows Stage 47 and Stage 48:

- Stage 47 proved that closed-choice HUDs reduce invented fields and bad handoffs.
- Stage 48 makes worker sessions feel like normal durable workers.
- Stage 49 makes the worker packet language canonical so those durable workers cannot
  drift into prompt-only or UI-only contracts.

## Implementation Result

Implemented in this pass:

- Added `agent_runtime/decision_contract_registry.py` as the canonical source for
  `DecisionType` schema projection, role permissions, payload key contracts, nested
  object contracts, HUD shapes, context expansion choices, event display metadata,
  contract hash, prompt contract text, and registry self-checks.
- Converted `decision_schema.py`, `decision_payload_contracts.py`, `events.py`,
  `context_builder.py`, `persona_runtime.py`, `snapshot.py`, and `status.py` to consume
  registry projections instead of maintaining separate contract truth.
- Removed the legacy unreachable hard-coded HUD shape table from `context_builder.py`.
- Added `agent_runtime/decision_contract_examples.py` and
  `harness contracts verify-examples --json` to parse and validate Stage 46 skill JSON
  examples against the live role and planning validators.
- Added HUD template validation to `verify_registry()` so invalid menu skeletons fail
  before live agents see them.
- Added `neko_supervisor`, `backend_dev`, and `launcher_dev` role alias support to CLI
  contract dumps.
- Aligned `delivery.work_status="patch_proposed"` with packet validation and updated the
  Dev skill contract.
- Kept the Stage 48 proof recipe metadata internal to the proof runner; agents see
  `recipe_id`, while action payloads no longer leak internal `proof_recipe` metadata.

Verification run:

```text
pytest tests/agent_runtime/test_decision_schema.py tests/agent_runtime/test_decision_contracts.py tests/agent_runtime/test_decision_contract_registry.py tests/agent_runtime/test_context_builder.py tests/agent_runtime/test_autonomy.py tests/agent_runtime/test_persona_prompts.py tests/agent_runtime/test_worker_sessions.py tests/agent_runtime/test_role_sessions.py tests/agent_runtime/test_dirty_state.py tests/agent_runtime/test_proof_command_policy.py tests/agent_runtime/test_ticker.py tests/agent_runtime/test_snapshot.py tests/agent_runtime/test_status.py -q
# 236 passed

python -m compileall agent_runtime hermes_cli\harness.py
python -m hermes_cli.main harness contracts verify-examples --json
python -m hermes_cli.main harness status --json
python -m hermes_cli.main harness snapshot --json
python -m hermes_cli.main harness install-stage46-skills --json
git diff --check
```

Readiness after skill reinstall:

- Neko, Launcher Dev, Backend Dev, and QA persona profiles report `ready`.
- Stage 46 skill examples checked: 7.
- Contract hash: `e9812599d7d8be8b6273ff01a77e7326a9a42a0fc412552c273ba79de36668eb`.
- Remaining dirty-state indicator is expected until this repo change is committed.

## Pre-Stage 49 Split Truth

Implemented today:

- `agent_runtime/decision_schema.py` owns `DecisionType`, the top-level AgentDecision
  JSON schema, and role-to-decision permissions.
- `agent_runtime/decision_payload_contracts.py` owns top-level payload key allowlists.
- `agent_runtime/decision_contracts.py` owns deeper semantic validation for many
  packet types.
- `agent_runtime/packets.py`, `agent_runtime/scope_control.py`, and related modules own
  additional packet normalization and safety checks.
- `agent_runtime/context_builder.py` hand-builds `decision_menu`,
  `context_expansion_menu`, and `decision_shape_index` for the Mission HUD.
- `agent_runtime/persona_runtime.py` manually describes the closed-choice HUD behavior
  in prompt text.
- Stage 46 skills contain example packet shapes that must stay aligned with runtime
  validation.

The current system is much safer than before, but it is still not enterprise-grade as a
contract source because the same truth is split across multiple files.

## Gaps This Stage Closes

1. Payload key contracts can drift from HUD menu entries.
2. Role permissions can drift from HUD choices.
3. Prompt examples can drift from runtime validation.
4. Stage 46 skill examples can drift from runtime validation.
5. Nested packet shapes such as `handoff_packet`, `proof_gate`, `join_gate`,
   `delivery`, `contract_packet`, `qa_review`, `log_ref`, and visual launch pins are
   partly template-driven instead of schema-driven.
6. Repair messages know the error, but do not have a single canonical source for the
   nearest valid shape, enum choices, and nested object requirements.
7. Mission Control renders event data, but internal Harness event types do not yet have
   a parallel event catalog that can drive compact display rules without UI guessing.
8. Stage 48 proof recipes will add `request_test_run.recipe_id`; that field must be
   generated into the same schema/HUD/validator surface rather than patched in one file.

## Non-Goals

- Do not change standard Hermes behavior by default.
- Do not make the model choose internal Harness event shapes such as `run.progress`.
  Agents emit decisions. Harness emits events.
- Do not expose hidden provider chain-of-thought.
- Do not remove compatibility normalizers without a migration. Existing safe
  normalizations must become explicit registry policies first.
- Do not make Mission Control own contract truth. Mission Control consumes projected
  metadata from the Harness.

## Target End State

One canonical registry defines:

- decision type id;
- role permission matrix;
- top-level AgentDecision schema;
- required, optional, and forbidden payload keys;
- nested object contracts;
- enum choices and aliases;
- normalization policy;
- redaction policy;
- repair hint;
- HUD shape id and label;
- prompt/example shape;
- Mission Control display metadata;
- schema version and content hash.

All consumers read from that registry:

- parser and top-level schema;
- role validation;
- payload validation;
- packet/nested validators;
- Mission HUD menus;
- context expansion menus;
- repair hints;
- prompt compact contract text;
- Stage 46 skill contract snippets;
- CLI contract dumps;
- tests and golden fixtures.

## Stage 49A. Contract Registry Data Model

Create `agent_runtime/decision_contract_registry.py`.

Define explicit dataclasses or typed dictionaries:

```python
DecisionContract
PayloadField
ObjectContract
ListContract
EnumContract
NormalizationPolicy
HudShape
RepairHint
EventContract
```

Registry requirements:

- Every `DecisionType` has exactly one `DecisionContract`.
- Every role in `AgentRole` has a role permission list generated from contracts, not a
  second handwritten mapping.
- Each decision contract defines:
  - `decision_type`;
  - `summary_limit`;
  - `rationale_limit`;
  - `allowed_roles`;
  - `required_payload_keys`;
  - `optional_payload_keys`;
  - `payload_fields`;
  - `nested_contracts`;
  - `shape_hint`;
  - `repair_hint`;
  - `hud_shapes`;
  - `prompt_examples`;
  - `redaction_policy`;
  - `normalization_policy`.
- Registry exports:
  - `decision_contract(decision_type)`;
  - `all_decision_contracts()`;
  - `allowed_decisions_for_role(role)`;
  - `payload_contract(decision_type)`;
  - `agent_decision_json_schema()`;
  - `hud_shapes_for_role(role)`;
  - `context_expansion_shapes_for_role(role)`;
  - `contract_manifest()`;
  - `contract_hash()`.

Implementation notes:

- Keep `agent_runtime/decision_payload_contracts.py` as a compatibility facade at first.
- Keep `agent_runtime/decision_schema.py` as the public parser module, but generate
  `DECISION_SCHEMA` and `ALLOWED_DECISIONS_BY_ROLE` from the registry.
- Use plain Python data first. Do not add a heavy schema dependency unless the local
  Harness already depends on it.

Acceptance:

- A test fails if any `DecisionType` is missing from the registry.
- A test fails if any role menu references a decision type not allowed for that role.
- A test fails if compatibility facades return data different from the registry.

## Stage 49B. Central Payload and Nested Validation

Move required/optional/unknown payload key validation into the registry.

Validation rules:

- Top-level unknown payload keys reject by default.
- Required payload keys reject centrally before semantic validation.
- Nested object unknown keys reject unless the registry explicitly marks the object as
  compatibility-normalized.
- Enum fields expose closed allowed choices and optional safe aliases.
- Lists define item type and minimum length where needed.
- Object fields define required nested keys where needed.
- Redaction-sensitive fields either reject or normalize through an explicit
  redaction policy.

Compatibility normalization policy:

- Some current packets safely normalize legacy metadata, such as dropping unsupported
  `handoff_packet` metadata with an `operator_note`.
- Stage 49 must not silently remove that behavior.
- Instead, encode it as `normalization_policy="drop_unknown_with_operator_note"` or
  a similarly explicit policy on that nested object.
- Every normalization emits or preserves a redaction-safe operator note.
- Unknown top-level agent payload keys stay invalid; compatibility normalization is only
  for named nested packet objects where the current tests already require it.

Initial nested contracts:

- `handoff_packet`;
- `handoff_packet.proof_gate`;
- `handoff_packet.join_gate`;
- `request_qa_review.handoff`;
- `qa_review`;
- `qa_review.coverage`;
- `delivery`;
- `delivery.contract_packet`;
- `block.log_ref`;
- `request_screenshot.required_launch_pins`;
- `request_video.required_launch_pins`;
- `propose_stage_plan.stages[]`.

Acceptance:

- Existing `test_decision_contracts.py` behavior remains green.
- New tests prove required keys are enforced by the registry, not only by handwritten
  branches in `decision_contracts.py`.
- New tests prove nested unknown keys reject or normalize according to explicit policy.
- New tests prove enum repair errors include valid choices.

## Stage 49C. HUD and Menu Generation From Registry

Refactor `agent_runtime/context_builder.py` so it no longer hand-builds full decision
shape dictionaries.

The context builder may still choose the next recommended move based on task/run state,
but the shape details must come from the registry.

Context builder responsibilities:

- choose `next_required_move`;
- choose the primary `shape_id`;
- attach context-aware `recommended_payload`;
- attach current proof command hints;
- attach current failed proof ids;
- attach current environment fingerprint status.

Registry responsibilities:

- valid `shape_id`;
- `decision_type`;
- required and allowed payload keys;
- nested required fields;
- enum choices;
- forbidden unknown key policy;
- prompt-safe template;
- repair hint;
- display label.

HUD menu requirements:

- `decision_contract_mode` remains `closed_choice`.
- `decision_menu` entries include:
  - `choice_id`;
  - `primary`;
  - `shape_id`;
  - `decision_type`;
  - `label`;
  - `when`;
  - `required_payload_keys`;
  - `allowed_payload_keys`;
  - `nested_required`;
  - `enum_choices`;
  - `forbid_unknown_payload_keys`;
  - optional `recommended_payload`.
- `context_expansion_menu` is also generated from registry shapes.
- Agents must continue to choose exactly one shape and only use that shape's allowed
  payload keys.

Acceptance:

- Existing Mission HUD tests remain green.
- New golden tests compare Dev, Backend Dev, Neko, and QA HUDs against registry-derived
  fixtures.
- A static test fails if `context_builder.py` contains a second hard-coded
  required/allowed key list for a decision type.

## Stage 49D. Repair Hints, Prompt Text, and Skill Examples From Registry

Refactor repair and prompt surfaces to consume registry projections.

Surfaces:

- `context_builder` validation repair HUD;
- `persona_runtime` compact prompt contract;
- Stage 46 skill snippets;
- future Stage 48 static prompt packet.

Rules:

- Prompt text may explain behavior, but must not be the only source of field truth.
- Prompt examples are generated from registry examples or verified against them.
- Skill examples must be machine-tested against the registry.
- Repair messages include:
  - invalid field path;
  - chosen decision type if known;
  - nearest valid shape id if inferable;
  - allowed top-level payload keys;
  - required nested keys;
  - enum choices;
  - context expansion alternative when missing context caused the error.

Acceptance:

- Tests parse every JSON packet example in Stage 46 skill docs and validate it.
- Tests assert `persona_runtime` contract text includes the current registry
  `contract_hash`.
- Tests assert repair HUD for invented keys points to the closed `decision_menu` and
  includes valid alternatives.

## Stage 49E. Proof Recipe Integration for Stage 48

Fold Stage 48 proof recipes into the canonical registry.

`request_test_run` must support:

```json
{
  "stage_id": "stage_launcher_contract_smoke",
  "recipe_id": "launcher_contract_smoke"
}
```

Rules:

- `commands` stays supported for existing agents.
- `recipe_id` must resolve through the proof recipe registry.
- If `recipe_id` resolves and `commands` is absent or empty, Harness supplies the
  command from the recipe.
- If both are present, proof policy checks whether commands match the recipe.
- Unknown `recipe_id` returns a repairable contract error with advertised recipe choices
  when safe.
- HUD menu may recommend recipe IDs instead of command strings for known stage types.

Acceptance:

- `request_test_run.recipe_id` appears in registry, HUD, prompt, and validation from one
  source.
- Existing command-based tests remain green.
- New recipe-based tests prove command resolution, mismatch rejection, and proof metadata
  persistence.

## Stage 49F. Internal Event Catalog for Mission Control

Agents do not emit Harness events, but Mission Control still needs stable event shapes.

Add an internal event catalog beside the decision registry:

```python
EventContract(
    event_type="run.progress",
    display_label="Run progress",
    severity_field="severity",
    timestamp_field="ts",
    safe_summary_fields=[...],
    expandable_detail_fields=[...],
    redacted_fields=[...],
)
```

Initial event contract coverage:

- `task.transition`;
- `run.opened`;
- `run.progress`;
- `run.tool.started`;
- `run.tool.finished`;
- `run.closed`;
- `proof.attached`;
- `packet.recorded`;
- `incident.opened`;
- `incident.closed`;
- `worker_session.opened`;
- `worker_session.assigned`;
- `worker_session.context_absorbed`;
- `worker_session.compressed`;
- `worker_session.possessed`;
- `worker_session.released`;
- `role_session.continued`;
- `role_session.closed`.

Rules:

- Event catalog is for Harness-emitted events and Mission Control rendering only.
- It must not be presented to agents as choices.
- Event payloads remain redaction-safe and size-bounded.
- Large details remain artifact-backed, not inline event payloads.

Acceptance:

- Event log allowlist tests prove all catalog event types are accepted.
- Mission Control snapshot exposes `event_contract_version` and safe display hints.
- Launcher tests prove unknown event types render fallback rows without crashing.

## Stage 49G. CLI and Artifact Projections

Add operator-facing contract inspection commands:

```text
python -m hermes_cli.main harness contracts dump --json
python -m hermes_cli.main harness contracts dump --role dev --json
python -m hermes_cli.main harness contracts dump --decision request_test_run --json
python -m hermes_cli.main harness contracts verify-examples --json
```

Artifacts:

- store a compact `contracts_manifest.json` under Harness runtime metadata;
- include `contract_hash` on autonomy packets and worker context receipts;
- include `decision_contract_version` in snapshots;
- include contract hash in proof metadata when a proof was requested by an agent packet.

Acceptance:

- CLI contract dump exits 0 and is redaction-safe.
- Snapshot includes a contract version/hash suitable for Mission Control display.
- A test proves stale contract hash in a worker context receipt is observable.

## Stage 49H. Rollout and Backward Compatibility

Use staged rollout instead of a risky big bang. The implemented rollout keeps standard
Hermes behavior unchanged by containing the registry in `agent_runtime` Harness modules
and keeping compatibility facades for existing imports.

Implemented rollout:

1. Build registry with parity tests while facades preserve public module names.
2. Enable `contract_hash` on status, snapshot, autonomy packets, and worker context
   receipts.
3. Enable registry-generated HUD shape details for Harness context.
4. Validate Stage 46 skill JSON examples in tests and CLI.
5. Keep packet compatibility normalizers in `packets.py`, but document their object
   policies in the registry manifest.
6. Remove duplicated hard-coded HUD shape lists after parity is proven.

Default behavior:

- Standard Hermes remains unchanged because the registry is only used by Harness runtime
  modules.
- Tony's Agent Runtime Harness profile consumes registry-generated HUD and contract
  metadata.

Rollback:

- Revert the Stage 49 commit to return to Stage 48 behavior.
- Keep compatibility facades until at least one live-token complex goal passes after
  Stage 49.

## Stage 49I. Test Plan

Unit tests:

- registry covers every `DecisionType`;
- every role menu shape maps to an allowed decision type;
- top-level schema generated by registry matches parser expectations;
- payload required/optional/unknown key validation is central;
- nested object validation covers handoff, delivery, QA review, visual pins, and block
  log refs;
- enum choices are closed and exposed in repair hints;
- compatibility normalization emits operator notes;
- context builder HUDs are generated from registry;
- persona prompt contract hash matches registry hash;
- Stage 46 skill JSON examples validate;
- proof recipe IDs validate and resolve;
- event catalog covers emitted event types.

Integration tests:

- fake Dev emits invented top-level payload key and gets deterministic repair HUD;
- fake QA emits invalid `qa_review.coverage` enum and gets valid choices;
- fake Neko emits unsupported handoff mode and gets valid handoff modes;
- fake Dev requests `recipe_id` proof and Harness resolves it;
- Mission Control snapshot includes contract version/hash without leaking paths/secrets.

Live-token certification:

- small goal: one Dev proof request with a deliberately constrained HUD path;
- invalid-packet repair goal: agent receives repair HUD and fixes packet without human
  intervention;
- complex goal: Neko -> Backend Dev -> Neko join -> Launcher Dev -> QA, with registry
  enforcement on and no invented-field incidents.

Regression commands:

```text
pytest tests/agent_runtime/test_decision_schema.py tests/agent_runtime/test_decision_contracts.py tests/agent_runtime/test_context_builder.py tests/agent_runtime/test_autonomy.py tests/agent_runtime/test_ticker.py -q
pytest tests/agent_runtime/test_proof_command_policy.py tests/agent_runtime/test_aaa_gap_fixes.py -q
python -m compileall agent_runtime hermes_cli\harness.py
python -m hermes_cli.main harness contracts dump --json
python -m hermes_cli.main harness status --json
```

Launcher checks if event catalog/snapshot projection changes:

```text
flutter analyze lib/features/mission_control test/features/mission_control
flutter test test/features/mission_control/mission_control_page_test.dart test/features/mission_control/mission_control_snapshot_test.dart
```

## Stage 49J. Definition of Done

Stage 49 implementation is complete only when:

- [x] One registry is the source for decision schema, payload key contracts, role
  permissions, HUD shapes, prompt examples, repair hints, and contract hashes.
- [x] `decision_payload_contracts.py` and `decision_schema.py` are compatibility facades
  or thin projections, not separate truth sources.
- [x] `context_builder.py` no longer hard-codes required/allowed payload key lists for
  decision types.
- [x] Nested packet contracts are schema-driven or explicitly marked as compatibility
  normalizers.
- [x] Every compatibility normalization remains redaction-safe and operator-note backed.
- [x] Stage 46 skill examples are validated by tests and CLI.
- [x] Mission HUD exposes closed choices, nested requirements, and enum choices for Neko,
  Dev, Backend Dev, and QA.
- [x] Invalid invented top-level fields remain repairable through closed payload
  contracts and HUD menu alternatives.
- [x] Stage 48 proof recipe IDs flow through `request_test_run` contract projection.
- [x] Mission Control receives event contract/version hints without owning contract truth.
- [x] Standard Hermes behavior remains default-compatible.
- [ ] Tony's Agent Runtime Harness profile runs one small and one complex live-token goal
  with registry enforcement enabled.

## Implementation Order

1. Add registry data model and parity projections.
2. Move `payload_contract` and role permissions onto registry facades.
3. Generate HUD shape details from registry while keeping current next-move selection.
4. Add nested contract definitions in audit mode.
5. Add repair hint generation from registry.
6. Add prompt/skill example verification.
7. Add proof recipe contract integration.
8. Add internal event catalog and snapshot projection.
9. Enable Tony profile enforcement after tests.
10. Run live-token certification and record results.

## Risk Register

| Risk | Mitigation |
| --- | --- |
| Over-tightening breaks useful legacy packets | Start with parity tests; encode existing safe normalizers explicitly before enforcement |
| Registry becomes another duplicate source | Static tests forbid duplicate required/allowed key lists in context builder and payload facade |
| Prompt examples drift again | Parse and validate skill/prompt examples in tests |
| Mission Control depends on unstable internal details | Expose event contract version and display hints, not raw validator internals |
| Agents fail because nested schema is too large | HUD shows compact closed choices plus bounded expansion; prompts carry hash and rules, not full dumps |
| Standard Hermes behavior changes | Feature flag defaults off outside Agent Runtime Harness profile |
