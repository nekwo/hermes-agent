# Stage 58 - First-Class Harness Packet Protocol

Status: implementation-in-progress; packet envelope exists but protocol is not complete

## Goal

Remove the recurring class of Harness failures where the agent's intent is correct
but the preserved communication is incomplete, dropped, stale, truncated, or hidden
in unsupported fields.

Stage 57 keeps the agent-facing contract small. Stage 58 makes the internal
communication layer richer and stricter: every Harness communication becomes a
typed, schema-backed packet that can be validated, carried across stages, rendered
in Mission Control, and repaired without babysitting.

## Problem Statement

Recent live-token runs exposed the same failure pattern repeatedly:

- Dev or QA chose the right high-level action.
- The Harness accepted or partially accepted the decision.
- Important details landed in unsupported fields, generic prose, truncated list
  entries, stale stage-local packets, or packet bodies that the next role could not
  see.
- QA correctly rejected the claim because the preserved packet did not prove what
  the agent said it proved.

This is not primarily a prompt-quality problem. It is a protocol problem.

The Harness must not rely on agents guessing ad hoc payload shapes. The HUD should
present a small action menu, and each action should map to a first-class packet
type with visible required/optional fields.

## Current Implementation Audit

The current Harness checkout already contains a partial packet system:

- `agent_runtime/packets.py` defines a `Packet` dataclass, packet IDs, packet recording, duplicate detection, body compaction, and validation for `handoff_packet`, `delivery`, and `qa_review`.
- `agent_runtime/decision_contract_registry.py` includes packet-related decision/event fields and events such as `packet.recorded` and `packet.duplicate`.
- `agent_runtime/context_builder.py` renders latest handoff, delivery, and QA review packets into persona context.
- `agent_runtime/snapshot.py` and observability code expose packet events enough for the Launcher terminal to show some packet rows.
- Persona skills already warn agents not to invent fields and to use HUD-supported packet fields.
- Tests cover several packet rendering and validation paths.

Do not replace this with a second packet system. Stage 58 should harden and complete the existing packet layer.

### Remaining Stage 58 Gaps

1. **Packet envelope is incomplete.**
   - Current packet records include core fields, but the Stage 58 target requires first-class `assignment_id`, `target_owner`, `validation_status`, `normalization_status`, and raw artifact handles.
   - Needed: adapter/backfill tests proving old packets still load while new packets expose those fields.

2. **Packet type coverage is still narrow.**
   - Current first-class types are mainly `handoff_packet`, `delivery`, and `qa_review`.
   - Needed: explicit adapters or typed packets for context request/result, proof request/result, blocker, missing input, repair feedback, and state transition events.

3. **No-silent-drop behavior is not complete.**
   - Unsupported fields are validated in places, but the system still needs a durable raw input artifact plus visible `packet.normalized` events when fields are dropped, renamed, truncated, or moved.
   - Needed: tests distinguishing harmless metadata normalization from acceptance-critical dropped content that must produce repair feedback.

4. **Schema-backed body registry is not unified.**
   - Decision contracts and packet keys exist, but HUD generation, validation, skill templates, QA review, and Mission Control rendering do not yet consume one shared packet body registry.
   - Needed: tests proving every HUD/skill packet field exists in the registry.

5. **Cross-stage packet visibility needs stronger selectors.**
   - Latest packet rendering exists, but QA/Dev recovery must deterministically prefer the newest relevant packet across stages and assignments.
   - Needed: stale packet tests where old stage-local delivery cannot shadow newer Dev delivery, and Dev sees the newest QA review after rejection.

6. **Repair feedback must behave like terminal stderr.**
   - Invalid decisions have repair paths, but packet-specific repair feedback must be visible in the next HUD with allowed fields/actions and retry limits.
   - Needed: malformed packet tests proving the state machine does not crash, the next prompt shows exact repair feedback, and repeated identical malformed output blocks cleanly.

7. **Mission Control packet renderer still needs proof.**
   - UI rows exist for some events, but Stage 58 requires compact DM-style packet rows for every persona, expandable normalized body, dropped/renamed/truncated fields, and raw artifact handles.
   - Needed: fullscreen Stage C screenshot proof after implementation.

8. **Skill installation/hash proof is still open.**
   - Source skill docs are updated, but installed profile copies/hashes must be verified after final packet changes.

9. **Live-token certification is still open.**
   - Required: no packet-formatting incidents, no acceptance-critical silent drops, QA reviews newest relevant packet, and archive preserves raw plus normalized packet evidence.

### Implementation-Ready Work Items

Implement Stage 58 by extending the current `agent_runtime/packets.py` system. Do not introduce a separate packet store or second registry.

| Item | Files | Required implementation | Required tests |
| --- | --- | --- | --- |
| 58-1 Complete envelope projection | `agent_runtime/packets.py`, `agent_runtime/context_builder.py`, `agent_runtime/snapshot.py` | Add envelope fields for `assignment_id`, `target_owner`, `validation_status`, `normalization_status`, `raw_artifact_id`, and `normalized_at`. Preserve backwards compatibility for old packet events. | Packet recording test asserts new fields; old packet event fixture still renders. |
| 58-2 Raw packet evidence | `agent_runtime/packets.py`, `agent_runtime/paths.py`, `agent_runtime/store.py` | Persist raw packet input under runtime evidence before compaction/redaction; archive raw and normalized packet evidence with the task. | Archive test proves raw packet artifact and normalized event are preserved. |
| 58-3 No-silent-drop normalization | `agent_runtime/packets.py`, `agent_runtime/decision_contracts.py` | Unknown/dropped/renamed/truncated fields emit `packet.normalized` with `dropped_fields`, `renamed_fields`, `truncated_fields`; acceptance-critical drops create repair feedback. | Unsupported harmless metadata records normalization; acceptance-critical unsupported field triggers repair feedback. |
| 58-4 Body registry | `agent_runtime/packets.py`, `agent_runtime/decision_contract_registry.py` | Centralize packet body allowed fields and aliases so validation, HUD, skills, and QA selectors use one source. | Test every skill template field exists in the packet registry. |
| 58-5 Cross-stage selectors | `agent_runtime/packets.py`, `agent_runtime/context_builder.py` | QA sees newest relevant Dev delivery across stages/assignments; Dev sees newest QA review after rejection; stale stage-local packets cannot shadow newer packets. | Existing stale packet tests plus assignment-aware selector tests. |
| 58-6 Repair feedback packet | `agent_runtime/packets.py`, `agent_runtime/ticker.py`, `agent_runtime/context_builder.py` | Invalid/normalized acceptance-critical packet creates `repair_feedback` packet and next HUD terminal feedback with closed allowed actions. | Malformed packet test asserts no crash, repair packet visible, repeat blocks cleanly. |
| 58-7 Mission Control packet renderer | Launcher Mission Control renderer | Render DM-style packet rows for all personas, expand normalized body, show raw artifact handle and normalization fields. | Fullscreen Stage C screenshot proof after Launcher implementation. |
| 58-8 Skill installation proof | `docs/agent-runtime-harness/stage46-skills/*`, profile install location | Skills teach packet protocol and installed profile copies/hashes match source. | Skill hash/install test or command proof. |

Implementation is complete only after 58-x tests pass and Stage 58H live-token certification completes without packet-format babysitting.

### Implementation Pass - 2026-06-09

Closed in code:

- Packet events now include first-class envelope fields: `assignment_id`, `target_owner`, `validation_status`, `normalization_status`, `raw_artifact_id`, `raw_artifact_path`, `normalized_at`, and normalized field lists.
- Raw packet artifacts are written under runtime packet artifacts and archived with terminal tasks.
- Unsupported packet metadata still preserves legacy strict-shape behavior by being removed from the normalized decision packet, while the private raw artifact retains the dropped values.
- `packet.normalized` is now a first-class event with registry coverage.
- Oversized delivery packets are compacted for event payload limits while raw packet artifacts preserve dropped/raw input.
- Tests cover packet envelope fields, normalization events, raw artifact preservation, archive preservation, and cross-stage packet selectors.

Focused tests:

```powershell
python -m pytest -o addopts= tests/agent_runtime/test_events.py tests/agent_runtime/test_decision_contracts.py tests/agent_runtime/test_context_builder.py -q
```

Remaining before Stage 58 can be called fully certified:

- Launcher Mission Control DM-style packet renderer fullscreen proof.
- Installed profile skill hash proof after final UI renderer changes.
- Live-token Stage 58H certification goals.

## Design Principle

Keep the external agent contract simple. Make the Harness packet protocol complete.

Agents should think in a few choices:

- Neko: assign, request missing input, report blocker.
- Dev: deliver, request missing input, report blocker.
- QA: approve, reject, request missing proof.

Harness should normalize those choices into typed packets:

- mission scope packet
- handoff packet
- context request packet
- context result packet
- delivery packet
- proof request packet
- proof result packet
- QA review packet
- blocker packet
- missing input packet
- repair feedback packet
- state transition packet

## Canonical Packet Envelope

All packets must share one envelope:

```json
{
  "packet_id": "packet_de_xxx",
  "packet_type": "delivery",
  "schema_version": 1,
  "actor": "backend_dev",
  "target_owner": "qa",
  "task_id": "task_xxx",
  "stage_id": "backend_investigation",
  "assignment_id": "assign_xxx",
  "run_id": "run_xxx",
  "source_decision_type": "propose_patch",
  "summary": "Compact redaction-safe summary.",
  "body": {},
  "validation_status": "valid",
  "normalization_status": "unchanged",
  "redaction_status": "safe",
  "created_at": "timestamp"
}
```

Required behavior:

- Envelope fields are stable for every packet type.
- `packet_id` is the durable handle shown to agents and Mission Control.
- `assignment_id`, `stage_id`, and `run_id` are first-class correlation fields,
  not hidden in logs.
- `validation_status`, `normalization_status`, and `redaction_status` are visible
  in status/snapshot and Mission Control.
- Raw packet input is preserved separately from normalized packet output.

## Typed Packet Bodies

### Mission Scope Packet

Used by Neko to create or repair the mission plan.

Required fields:

- `objective`
- `repo_bundles`
- `acceptance`
- `non_goals`
- `proof_expectations`
- `routing_plan`

### Handoff Packet

Used for role-to-role release.

Required fields:

- `handoff_mode`
- `target_owner`
- `target_repo`
- `objective`
- `acceptance`
- `required_inputs`
- `proof_gate`

### Delivery Packet

Used by Dev for product edits, no-edit investigations, proof-only stages, and
contract handoffs.

Required fields:

- `work_status`
- `summary`

Common optional fields:

- `changed_files`
- `self_test_evidence_ids`
- `proof_ids`
- `findings`
- `recommendations`
- `questions`
- `known_gaps`
- `model_options`
- `policy_assessment`
- `wd_tagger_assessment`
- `implementation_stages`
- `contract_packet`
- `repo_bundle_id`
- `next_owner`

Rules:

- Investigation-specific content must live in supported delivery fields, not only
  in top-level summary/rationale.
- Domain-specific fields must be schema-backed before a live run depends on them.
- If a field is too domain-specific to promote immediately, use a supported
  `analysis_sections[]` entry with `section_id`, `title`, `summary`, and `items`.

### QA Review Packet

Used by QA for approve/reject/missing-proof.

Required fields:

- `verdict`
- `decision_basis`
- `review_scope`
- `delivery_packets_reviewed`
- `proof_reviewed`
- `coverage`

Optional fields:

- `remaining_gaps`
- `missing_proof`
- `accepted_risk`
- `next_owner`

Rules:

- QA must cite packet IDs and proof IDs it reviewed.
- QA must not reject because details are absent from one field if the details are
  present in another supported field.
- QA rejection must produce machine-readable `remaining_gaps` that Dev receives in
  the next prompt.

### Missing Input Packet

Used when a role needs another role's answer.

Required fields:

- `missing_input_type`
- `target_owner`
- `why_blocking`
- `attempted_self_service`
- `minimum_needed`

### Blocker Packet

Used for terminal or retryable blockers.

Required fields:

- `blocker_kind`
- `severity`
- `why_blocking`
- `evidence`
- `retry_policy`
- `next_owner`

### Repair Feedback Packet

Created by Harness when a decision or packet is invalid or normalized.

Required fields:

- `invalid_field`
- `rejected_value`
- `allowed_values`
- `repair_hint`
- `next_allowed_actions`
- `retry_limit`

Rules:

- Invalid packets should not crash the state machine unless the Harness cannot
  preserve or explain the failure.
- Agents should receive repair feedback like terminal stderr on the next prompt.
- Repeating the same malformed packet without changed feedback should be blocked.

## No Silent Drops

Unknown fields must not silently disappear.

Required behavior:

- Preserve the raw packet input in an audit artifact.
- Create a normalized packet with supported fields only.
- Emit `packet.normalized` when fields are dropped, renamed, truncated, or moved.
- Include `dropped_fields`, `renamed_fields`, and `truncated_fields` in the event.
- Add a repair feedback packet to the next HUD when dropped fields affect
  acceptance or QA review.

Acceptable normalization:

- Drop harmless checklist metadata while preserving the mission decision.
- Move known legacy aliases into supported fields.
- Truncate fields with an explicit `truncated_fields` record.

Unacceptable normalization:

- Dropping acceptance-critical domain content without visible repair feedback.
- Hiding dropped fields only in `operator_note`.
- Letting QA review stale packets when a newer relevant packet exists.

## Packet Visibility Rules

The packet read model must be role-aware and stage-aware.

Required visibility:

- Neko sees latest scope, handoff, delivery, QA review, blocker, and repair
  packets for the mission.
- Dev sees latest Neko handoff, context result, QA review, missing input, blocker,
  and repair packets relevant to its assignment.
- QA sees latest Dev delivery, proof result, missing proof, blocker, and repair
  packets relevant to the release stage.
- Mission Control can render every packet per persona, stage, assignment, and run.

Ordering rule:

- For QA review, newest relevant Dev delivery outranks stale stage-local delivery.
- For Dev recovery, newest QA review outranks stale implementation-stage state.
- Packet selectors must be tested with cross-stage and stale-packet scenarios.

## Mission Control UI Requirements

Mission Control should render packets as compact DM-style event rows with expandable
details.

Each row shows:

- persona/actor
- packet type
- packet summary
- validation/normalization/redaction status
- packet ID
- stage/assignment/run IDs
- reviewed proof IDs or delivery IDs when relevant

Expanded details show:

- normalized body
- dropped/renamed/truncated fields
- raw packet artifact handle
- repair feedback if present

The UI must not require reading raw JSON to understand why QA rejected a packet or
why Dev needs to revise.

## Skill Updates

All Harness persona skills must teach the packet protocol directly.

Neko skill:

- Choose only HUD actions.
- Assign work through scope/handoff packets.
- Do not invent packet fields.
- Use repair feedback when Harness rejects or normalizes a packet.

Dev skill:

- Deliver via supported delivery fields.
- Put investigation/domain substance into first-class packet fields or
  `analysis_sections`.
- Treat dropped fields as a failed delivery and repair once.

QA skill:

- Review supported fields across the latest relevant packet.
- Reject with machine-readable `remaining_gaps`.
- Do not require duplicate prose if the required content exists in any supported
  field.

Codex Mission Control skill:

- When live runs fail because of packet formatting, treat that as a Harness
  protocol gap.
- Prefer adding first-class packet fields/visibility/repair over prompt-only fixes.

## Implementation Stages

### 58A - Packet Inventory Audit

Audit current packet and pseudo-packet paths:

- `handoff_packet`
- `delivery`
- `qa_review`
- context requests/results
- proof requests/results
- blockers/incidents
- validation repair
- repo bundles
- role checklist updates
- run progress and thinking summaries

Deliverables:

- table of current fields, unsupported aliases, normalization behavior, visibility,
  and Mission Control rendering.
- list of acceptance-critical fields currently at risk of being dropped.

### 58B - Canonical Envelope

Add a canonical packet envelope model or adapter.

Deliverables:

- shared envelope projection for all packet types;
- first-class `assignment_id`, `target_owner`, `validation_status`,
  `normalization_status`, and `created_at`;
- migration/backfill adapter for existing event payloads.

Tests:

- old packets still load;
- new packets render with envelope fields;
- archive preserves raw and normalized packet evidence.

### 58C - Schema-Backed Body Registry

Move packet body contracts into a single registry consumed by:

- validation;
- HUD shape generation;
- skills/templates;
- Mission Control rendering;
- QA review rules.

Tests:

- every HUD packet field exists in registry;
- every skill template field exists in registry;
- unknown fields produce `packet.normalized` or repair feedback.

### 58D - No-Silent-Drop Normalization

Replace hidden `operator_note` drops with explicit normalization events and repair
feedback.

Tests:

- unsupported harmless metadata is recorded as normalized;
- unsupported acceptance-critical metadata triggers repair feedback;
- repeated same malformed packet is blocked with a clear reason.

### 58E - Cross-Stage Packet Visibility

Make packet selection deterministic and role-aware.

Tests:

- QA sees newest relevant Dev delivery across stages;
- Dev sees newest QA review across stages;
- Neko sees blocker and repair packets;
- stale stage-local packets cannot shadow newer relevant packets.

### 58F - Mission Control Packet Renderer

Render first-class packets in the Launcher Mission Control terminal.

Tests/proof:

- Neko, Launcher Dev, Backend Dev, and QA packet rows render.
- Expanders show normalized body and raw artifact handle.
- Dropped/truncated/renamed fields are visible.
- Fullscreen screenshot proof captures the packet workflow.

### 58G - Persona Skill And Prompt Updates

Update and reinstall skills:

- `harness-mission-lead`
- `harness-dev-delivery`
- `harness-qa-verdict`
- Codex `mission-control-harness`

Tests:

- installed profile skill hashes match source docs;
- prompt/HUD regression includes first-class packet guidance;
- invalid packet repair feedback appears in the next context.

### 58H - Live Token Certification

Run live goals after normal tests pass:

1. No-edit investigation with domain-specific fields.
2. Product-edit Launcher change with self-test and final gate.
3. Cross-stack Backend-to-Launcher handoff.

Certification:

- no packet-formatting incidents;
- no acceptance-critical content dropped silently;
- QA reviews newest relevant packet;
- task reaches `done` or a truthful terminal blocker;
- archive preserves raw and normalized packet evidence.

## Definition Of Done

Stage 58 is complete only when:

- every Harness communication path has a typed packet or explicit adapter;
- no acceptance-critical field can be silently dropped;
- packet normalization is visible to agents and Mission Control;
- Neko/Dev/QA skills reference first-class packet behavior;
- QA and Dev cross-stage packet visibility is deterministic and tested;
- Mission Control renders packet rows for every persona;
- live-token certification completes without packet-format babysitting.
