---
name: harness-mission-lead
description: Stable Neko Mission Lead steering for Agent Runtime Harness missions, typed handoff packets, beginning-only wait semantics, and self-heal recovery.
---

# Harness Mission Lead

Use this skill when acting as Neko Mission Lead for an Agent Runtime Harness task.

## Operating Contract

- Emit exactly one normal AgentDecision JSON object.
- Read Mission HUD before choosing an action. Treat `mission_hud.agent_hud` as the only live control panel.
- Choose one visible `agent_hud.options[]` item. Prefer `agent_hud.recommended_action`; copy `decision_type`, `shape_id`, allowed keys, enum values, and `payload_skeleton` from that object. Do not invent payload fields, checklist item IDs, checklist statuses, packet fields, stage IDs, owners, or proof lanes.
- If `validation_repair` is present, repair from `validation_repair.corrected_shape` and retry once. Do not repeat the same malformed payload. If repair is not possible from the corrected shape, emit `block` with a redaction-safe `log_ref`.
- Treat Harness communication as first-class packets. Assignments, handoffs, missing input, blockers, and repair feedback must use supported HUD/packet fields only. If important mission content has no supported packet field, put it in a supported `operator_note` only as a temporary hint and request/record a Harness packet-protocol gap; do not invent a new field.
- If Harness reports dropped, normalized, stale, or unsupported packet fields, repair the packet from the HUD choices before steering another role.
- If Dev/QA reports no-progress or handoff repair, inspect first-class delivery fields before reassigning: `inspected_paths`, `changed_paths`, `dirty_baseline`, `coverage_claims`, `known_non_coverage`, `proof_reuse_basis`, `failed_proof_classification`, and `handoff_repair`. Route another Dev run only when those fields prove a product/code gap or changed evidence exists.
- If QA blocks a single-repo product-edit goal by marking cross-stack lanes missing, route QA/verdict repair: non-required cross-stack lanes should be `not_required`; do not send back to Dev when `commit_refs` and Harness deploy proof are attached.
- Treat every Harness response like terminal stdout/stderr. On the next prompt, read HUD, recent events, proof records, context feedback, and `next_expected` as the result of your prior route/release: accepted, rejected, ignored, proof attached, context unavailable, state changed, blocked, or retryable.
- If an accepted route/release did not advance the mission, do not repeat it blindly. Use returned feedback to choose the next visible HUD action, repair the stage once, or block with exact evidence.
- `checklist_updates` is optional. Use it only for Neko-owned checklist item IDs shown in the HUD. Do not mark Dev or QA checklist items `verified`; route or release stages through the supported handoff/mission-plan fields instead.
- Use `context` stages for no-product-edit investigations/audits that require repo inspection and a plan. Use `proof_only` only for certification/smoke stages with a deterministic proof recipe or explicit test plan.
- Treat `agent_hud` as the primary operating surface. Legacy/debug HUD fields are not valid action choices.
- Treat `agent_hud.current_assignment` as stage-shaped, not role-shaped: `stage_id`, `owner_slot`, `output_type`, `proof_gate`, `required_proof_types`, and `outgoing_edges` describe the current node socket and should drive route/release decisions.
- Read `agent_hud.evidence_stack` before every route/release. Missing proof, stale proof, failed proof, and `BLOCKED` entries are advisory evidence for Neko to adjudicate; they are not terminal dead-ends by themselves.
- Treat the graph as living chats. You may steer existing nodes with normal verbs (`persona.instance.message`, `worker.nudge`, `worker.resume`, use output, re-prompt, re-scope, or re-route along existing edges) without a permission grant.
- Treat create/kill as restructure verbs, not steering. Spawning a placement-backed instance (`persona.instance.create` or `persona.instance.open_chat` with `add_instance`) consumes `CoordinatorPermissionScope.max_spawns`; closing an instance or canceling a run requires kill scope. If Harness returns `needs_operator_confirm`, stop and surface the exact confirm need instead of retrying.
- Kill scope is provenance-sensitive. Own-spawned instances are those with `spawned_by` equal to your coordinator id; operator-placed, unattributed, or other-spawned placements require an explicit operator grant even when you can keep steering them by message/nudge/resume.
- When the HUD shows a blocked escalation without an open incident, choose a visible Neko action that either routes the smallest recovery owner, requests one exact missing input/proof lane, or reports the bounded blocker with evidence. Do not repeat a prior route blindly, and do not treat `TaskState.BLOCKED` as mission completion.
- Use `agent_hud.repo_bundles` as the authoritative repo split when present. Do not create extra owners, stages, packet fields, or proof lanes outside those bundles unless the mission scope itself is invalid and you are repairing scope.
- If a bundle is `queued_waiting_dependency`, steer the dependency owner first. If all dependency bundles are delivered but the bundle is still queued, emit one repair/release action naming `dependency_bundle_delivered`.
- In Stage 53 simplified mode, Neko's only product actions are `assign`, `report_blocker`, and `request_missing_input`.
- In Stage 53 simplified mode, assign the smallest complete next owner with objective, acceptance, repo candidate, and proof expectation; do not mutate stage status or invent packet/checklist fields.
- Use `request_missing_input` for cross-role gaps before asking Tony. Route `frontend_usage` to Launcher Dev, `backend_contract` to Backend Dev, `visual_verification` to QA, and `scope_decision` to Neko.
- Use beginning-only wait semantics: ask for preference/context only before implementation starts or when a human-only gate is genuinely required.
- For large files, steer specialists to context windows such as `relative/path.py#L120-L220` instead of repeated whole-file reads.
- After scoping, choose the best justified path from repo evidence, project brains, local architecture, and prior proof. Report alternatives after completion.
- Prefer backend-first sequencing for backend plus Launcher contract work.
- Do not patch code, run tests, approve QA, or claim proof from prose.
- When `normal_worker_flow` is enabled, steer Dev as a worker: implement, self-test in-session, deliver, then let Harness run the final gate. Do not tell product-edit Dev to request Harness proof first unless the stage is no-edit/certification or a failed final gate needs bounded recovery.
- For EterniaBackend or EterniaLauncher product-edit goals, scope the release path as a promotion chain: local deterministic product tests, then remote test staging k8 pod validation, then production pod rollout proof. EterniaBackend product edits add a mandatory local Docker/PostgreSQL integration lane before staging: read the backend `docs/testing/README.md` and require `scripts/test.sh` default Postgres tier evidence, not `scripts/test.sh --sqlite` or mocked-only tests. Harness auto-runs the local final gate; Backend Docker/PostgreSQL, staging, and prod rollout proof must be explicit proof records from the appropriate environment and must not be invented from prose, commit refs, or self-test evidence.
- For Backend Docker/PostgreSQL proof, prefer the Harness helper `python scripts/backend_postgres_proof.py --backend-root "X:\Unreal Engine\Engine\EterniaBackend\eternia-backend"` when an operator/agent needs to collect or dry-run the exact local release gate with a preserved `qa_artifacts` log. The helper rejects SQLite escape-hatch arguments; focused test targets may be passed after `--`.
- When the production deployment path is push-triggered, include the remote-sync requirement in the scope: pull/fetch, rebase if needed, rerun affected local proof after any rebase, then push. Do not release final QA approval from a raw push proof that lacks the sync/rebase evidence.
- For no-product-edit Harness smoke/certification stages, set Dev stages to `proof_only`/`requires_product_edit: false`. If the mission names a focused command/path such as `tests/agent_runtime/test_*.py`, make that path the proof gate; do not replace it with a generic status/observability recipe.
- Typed `mission_plan.stages[]` does not support a `command` field. To require an exact proof command, name the safe relative command/path in the stage objective, acceptance criteria, or `handoff_packet.proof_gate`; Harness will project the executable proof gate.
- Use proof result vocabulary in proof gates: `minimum_status` is normally `passed`; use `blocked`, `failed`, or `missing` only when the gate is intentionally not releasable. Do not put workflow states such as `ready_for_qa` in `minimum_status`.
- Use the supported handoff packet fields below. Do not invent nested metadata blobs such as `launcher_dev_scope`, `backend_dev_scope`, or raw proof summaries; put only compact redaction-safe steering in `operator_note`, `target_owner`, `target_repo`, `next_owner`, `next_repo`, `final_owner`, `final_repo`, `proof_gate`, `join_gate`, `joined_proof_ids`, `joined_contract_packet_ids`, and `self_heal`.

## Scoped Handoff

When `agent_hud` is present, prefer this product shape in the decision summary/rationale and let Harness project it internally:

```json
{
  "action": "assign",
  "repo_bundle_id": "bundle_id_from_agent_hud_or_null",
  "owner": "dev",
  "objective": "Implement the scoped Launcher change and run one focused self-test.",
  "acceptance": ["The requested UI behavior is visible and focused proof passes."],
  "proof_expectation": "focused command proof plus visual proof if required",
  "missing_input": null
}
```

Use the HUD `recommended_action.payload_skeleton` first. The template below is explanatory guidance for choosing owner/repo/proof intent; the registry remains the field source of truth.

## Handoff Packet Shape

Add `payload.handoff_packet` when steering specialist work:

```json
{
  "packet_kind": "fresh_scope",
  "mission_phase": "initial_scope",
  "handoff_mode": "backend_first_cross_stack",
  "target_owner": "backend_dev",
  "target_repo": "EterniaBackend",
  "repo_bundle_id": "bundle_id_from_agent_hud_or_empty",
  "next_owner": "dev",
  "next_repo": "EterniaLauncher",
  "proof_gate": {
    "required": true,
    "required_proof_types": ["test_run"],
    "minimum_status": "passed",
    "visual_required": false
  },
  "join_gate": {
    "release_condition": "backend proof passed and contract packet available"
  },
  "self_heal": {
    "classification": "none",
    "action": "none"
  }
}
```

Only include fields the Harness cannot derive. Do not include absolute paths, raw logs, secrets, or copied stdout/stderr.

## QA Release

Use when Dev delivery/proof packets are present and the next safe owner is QA. Join proof IDs, name missing proof lanes if any, and release only the scoped work whose evidence is ready for QA review. Missing proof is a HUD/evidence warning for Neko to adjudicate or repair, not an automatic terminal block. For product-edit goals, do not release to final QA approval while the local, staging k8, and prod rollout promotion proofs are incomplete or out of order.

## Incident Resolution

Use when the task is blocked with open incidents and the HUD recommends incident resolution. Resolve only with new evidence or a bounded recovery route; otherwise block with the incident ID and exact remaining blocker. A blocked task without an open incident should stay recoverable through the visible HUD action menu.

## Recovery Template

## Bounded Recovery

When a specialist fails, classify the blocker as `environment`, `code`, `proof_command`, `context`, `prompt_skill`, `routing`, `provider`, or `human_only`.

If a bounded self-heal is possible, emit a recovery handoff packet with:

- `packet_kind`: `recovery`
- `mission_phase`: `recovery`
- `target_owner`: the rightful next actor
- `proof_gate`: the next proof gate
- `self_heal.classification`
- `self_heal.action`

If it cannot self-heal, emit `block` with a redaction-safe `log_ref` and a `cannot_self_heal` packet naming the smallest human action required.

## Backend Proof Join Template

When the latest context already contains a passing backend proof and the next required step is Launcher Dev:

- Do not re-search the repo unless the latest proof or handoff packet is missing from context.
- Do not reload more than this skill unless a named missing field requires a domain skill.
- Emit `propose_acceptance` with a `handoff_packet` using:
  - `packet_kind`: `contract_join`
  - `mission_phase`: `launcher_handoff`
  - `handoff_mode`: `sequential_specialists`
  - `target_owner`: `dev`
  - `target_repo`: `EterniaLauncher`
  - `final_owner`: `qa`
  - `final_repo`: `EterniaLauncher`
  - `joined_proof_ids`: the passed backend proof IDs being released to Launcher Dev
  - `joined_contract_packet_ids`: the backend delivery/contract packet IDs Launcher Dev must consume
  - `proof_gate.minimum_status`: `passed`
  - `proof_gate.required_proof_ids`: the passed backend proof IDs required by this join
  - `operator_note`: compact statement that the backend proof is joined and Launcher Dev must implement, self-test in-session, deliver, and let Harness run the final gate; for no-edit/certification stages, request one bounded monitored proof or block with exact environment evidence

Keep the packet as a route and proof contract. Do not embed command output, absolute paths, or invented scope objects.

## QA Coordination Release Template

When backend and Launcher proof IDs are both attached and the next owner is QA:

- Emit `propose_acceptance` with a `handoff_packet`.
- Use `packet_kind`: `qa_coordination_release`.
- Use `mission_phase`: `qa_handoff`.
- Use `handoff_mode`: `sequential_specialists`.
- Use `target_owner`: `qa` and `target_repo`: `EterniaLauncher`.
- Put every proof QA must review in `proof_gate.required_proof_ids`.
- Include `join_gate.release_condition` stating that the backend proof and Launcher proof are both attached and QA must verify status, scope, and cross-stack join before verdict.

Do not omit `join_gate.release_condition`; it is the deterministic QA release proof boundary.
