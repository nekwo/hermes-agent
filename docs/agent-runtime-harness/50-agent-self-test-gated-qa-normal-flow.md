# Stage 50 Agent Self-Test and Gated QA Normal Flow

Date: 2026-06-07
Owner: Codex independent Harness investigation
Status: implementation plan ready; expanded with code touchpoints and gap closures

## Purpose

Stage 50 makes the Harness feel like a normal competent agent harness:

```text
Neko scopes -> Dev implements -> Dev self-tests in the same worker session -> Harness runs final gates -> QA verifies -> done
```

The current runtime has strong internal evidence handling, worker-session persistence,
contract validation, no-freeze guards, and proof preservation. The remaining UX/problem
is that the model-facing surface is still wider than the job requires. Agents can see
too many valid-looking moves, especially proof-related moves, and can pick a valid shape
that is wrong for the product stage.

Stage 50 keeps the rich internal control plane, but reduces the agent-facing loop to a
small worker contract.

Core principle:

```text
Keep rich internal states. Simplify the agent contract.
```

The Harness may keep detailed states/events for recovery, proof, archive, Mission
Control, dirty-state hygiene, worker leases, possession, screenshots, and live-token
observability. The agents should not have to reason over that full graph. They should see
the next worker move, a tiny set of alternatives, and one valid packet skeleton.

## Product Decision

Dev workers should not ask Harness to run proof for every inner-loop test.

Instead:

- Dev runs narrow local tests/build/analyze/screenshot checks inside the same worker
  session while context is hot.
- Dev fixes failures immediately.
- Dev delivers the patch with redaction-safe self-test evidence references.
- Harness automatically runs the final deterministic gate for the stage.
- QA independently reviews the final gate proof and visual proof when required.

Harness-owned proof remains important, but it is a gate and recovery tool, not the main
developer inner loop.

## Current Ground Truth

Implemented before this stage:

- Stage 48 introduced durable worker sessions and same-session continuation.
- Stage 49 introduced the canonical decision contract registry and HUD shape generation.
- `request_test_run` supports recipe-based proof and strict command validation.
- The latest Stage 49 patch prevents no-edit smoke proof from satisfying product-edit
  stages and reroutes smoke drift back to the real implementation stage.
- Dev personas have terminal/code tools available, and the runtime already records
  `run.tool.*`, role-session metrics, token/tool counts, proof IDs, and redaction-safe
  progress events.

Remaining gap:

- The HUD and prompt still teach Dev that `request_test_run` is a normal next move after
  reading or planning.
- `propose_patch`, `request_test_run`, and `request_qa_review` are separate model-visible
  choices even when the Harness could own the transition from "patch delivered" to
  "final gate" to "QA".
- Self-test evidence from agent-run terminal commands is not first-class enough for the
  Harness to use as context, retry guidance, and Mission Control display.
- QA sees final proof artifacts, but the model-facing path to produce those artifacts is
  too manual and can create extra turns.
- The current HUD shape index can expose common shapes that the role validator rejects.
  Example: Dev/QA can see `request_human` from common shapes even though their allowed
  decision set does not permit it, and Neko can see `report_issue_discovery` from common
  shapes even though Neko cannot emit it. Stage 50 must close this before adding another
  projection layer.

## Non-Goals

- Do not remove the existing decision registry, event catalog, proof stores, or archive
  contracts.
- Do not expose hidden provider chain-of-thought. Mission Control may show safe reasoning
  summaries, tool calls, command logs, code edits, diffs, and decisions.
- Do not let Dev self-reported text satisfy release proof gates.
- Do not make every command unlimited or every proof full-suite.
- Do not collapse Neko, Dev, Backend Dev, and QA into one role.
- Do not change standard Hermes behavior by default.

## Target Semantics

Agent-facing loop:

```text
Neko:
  scope once at kickoff, assign workers, reroute only from evidence

Dev:
  inspect narrowly, edit, self-test in-session, deliver patch, fix failed gate, or block

QA:
  review final gate proof, run/capture independent visual proof when required, verdict
```

Internal Harness loop:

```text
implementation stage
  -> worker self-test evidence recorded
  -> delivery accepted
  -> Harness final gate proof collected
  -> same worker resumes on gate failure
  -> QA review after gate pass
  -> terminal/archive-ready only after QA gate
```

The model sees a small set of valid moves. The Harness keeps the detailed states, proof
recipes, event display metadata, dirty-state checks, watchdogs, archive manifests, and
Mission Control observability.

## Implementation Readiness Audit and Gap Closures

These gaps must be closed as part of Stage 50 before live-token certification.

| Gap | Risk | Required closure |
| --- | --- | --- |
| HUD exposes role-invalid common shapes | Agents can choose a visible option that validation rejects | Filter HUD shapes by both `HudShape.allowed_roles` and `allowed_decisions_for_role(role)` before rendering. Add a registry invariant test that every role-visible shape maps to an allowed decision. |
| `request_test_run` is treated as Dev's default inner loop | Extra turns and stage drift | Add normal-worker-flow HUD projection and hide `request_gate` for product-edit stages until delivery/final-gate recovery. |
| No self-test evidence store | Dev terminal checks disappear into noisy logs or prose | Add a first-class self-test evidence model/store with artifact paths, redaction status, stage/run/worker IDs, and event IDs. |
| Delivery cannot cite self-tests in a strict way | Dev either over-pastes logs or relies on prose | Add `delivery.self_test_evidence_ids` and validate IDs are known, same task, same stage or accepted upstream handoff. |
| Auto final gate has no single owner | Could duplicate `request_test_run` path or bypass proof policy | Add a final-gate resolver and call the existing proof collection path after accepted delivery. Do not create a second command runner. |
| Self-test might be mistaken for QA proof | False done | Proof gates must explicitly ignore self-test evidence for release/QA approval unless a later stage intentionally upgrades it with Harness-captured proof. |
| Failed final gate may spawn a fresh worker | Context loss and repeated read/search loops | Resume the same worker session with failed proof ID, command summary, and a bounded repair HUD. |
| Mission Control feed has no compact worker taxonomy | Logs still feel like blocks instead of readable worker progress | Project events into compact display categories while preserving raw artifact handles. |
| Archive does not know about self-test evidence yet | Evidence loss | Archive task manifest must preserve self-test evidence records and artifacts alongside proofs/runs/workers. |

## Code Touch Map

Harness files expected to change:

- `agent_runtime/runtime_config.py`: add `NormalWorkerFlowConfig`.
- `agent_runtime/config.py`: load/validate `normal_worker_flow` and keep default off.
- `agent_runtime/decision_contract_registry.py`: add worker-action projection metadata,
  filter role HUD shapes by validator-allowed decisions, expose worker action manifest.
- `agent_runtime/context_builder.py`: render primary worker action, alternatives,
  not-allowed-yet reasons, self-test evidence, and final gate proof IDs.
- `agent_runtime/persona_runtime.py`: update compact prompt text so normal-worker-flow
  agents self-test in-session and only request gates when exposed by HUD.
- `agent_runtime/decision_contracts.py`: validate `delivery.self_test_evidence_ids`.
- `agent_runtime/packets.py`: preserve `self_test_evidence_ids` in delivery packets.
- `agent_runtime/paths.py`: add self-test evidence paths.
- `agent_runtime/self_test_evidence.py`: new model/store/detector utilities.
- `agent_runtime/events.py` or `agent_runtime/decision_contract_registry.py`: add
  `self_test.recorded`, `self_test.reused`, and display metadata.
- `agent_runtime/ticker.py`: after accepted `propose_patch`, resolve and run final
  gate using the existing proof collection path; resume same worker on failure.
- `agent_runtime/stage_intent.py`: reuse product-edit/no-edit classification for worker
  action visibility and final-gate eligibility.
- `agent_runtime/proof_recipes.py`: mark recipes as final-gate, no-edit, visual, or
  recovery eligible.
- `agent_runtime/proof_gates.py`: ensure QA/release gates ignore self-test evidence.
- `agent_runtime/no_freeze_monitor.py` and `agent_runtime/dev_discipline.py`: add
  counters for repeated self-test/final-gate loops.
- `agent_runtime/snapshot.py`, `agent_runtime/observability.py`, and
  `agent_runtime/status.py`: expose compact worker action/evidence summaries.
- `agent_runtime/store.py`: archive self-test evidence records/artifacts.
- `hermes_cli/harness.py`: expose contract/action dumps and status fields if needed.

Launcher Mission Control files expected to change for Stage 50.8:

- `X:\Unreal Engine\Engine\Launcher\EterniaLauncher\lib\features\mission_control\data\mission_control_snapshot.dart`
- `X:\Unreal Engine\Engine\Launcher\EterniaLauncher\lib\features\mission_control\data\mission_control_bridge.dart`
- `X:\Unreal Engine\Engine\Launcher\EterniaLauncher\lib\features\mission_control\mission_control_page.dart`
- `X:\Unreal Engine\Engine\Launcher\EterniaLauncher\test\features\mission_control\mission_control_snapshot_test.dart`
- `X:\Unreal Engine\Engine\Launcher\EterniaLauncher\test\features\mission_control\mission_control_page_test.dart`
- `X:\Unreal Engine\Engine\Launcher\EterniaLauncher\test\features\mission_control\mission_control_bridge_test.dart`

Do not edit Launcher UI before the Harness snapshot schema is stable.

## Data Contracts

### `NormalWorkerFlowConfig`

Add to `RuntimeConfig`:

```python
@dataclass(slots=True)
class NormalWorkerFlowConfig:
    enabled: bool = False
    dev_self_tests_in_session: bool = True
    auto_final_gate_after_delivery: bool = True
    hide_request_test_run_until_gate: bool = True
    self_test_evidence_capture: bool = True
    max_self_test_repeats_without_change: int = 1
    max_auto_final_gate_repairs_per_stage: int = 1
    expose_worker_actions_in_contract_dump: bool = True
```

Tony's Harness profile may enable it. Standard Hermes must default it off.

### `SelfTestEvidence`

Suggested persisted record:

```json
{
  "schema_version": 1,
  "evidence_id": "selftest_...",
  "task_id": "task_...",
  "worker_session_id": "worker_...",
  "run_id": "run_...",
  "persona_id": "dev",
  "stage_id": "mc_terminal_dm_bubble_rows",
  "repo_label": "EterniaLauncher",
  "workdir_label": "EterniaLauncher",
  "command_label": "flutter test test/features/mission_control/mission_control_page_test.dart",
  "command_hash": "sha256:...",
  "exit_code": 0,
  "status": "passed",
  "started_at": "2026-06-07T00:00:00Z",
  "finished_at": "2026-06-07T00:00:10Z",
  "elapsed_ms": 10000,
  "stdout_path": "self_tests/task_.../selftest_....stdout.txt",
  "stderr_path": "self_tests/task_.../selftest_....stderr.txt",
  "stdout_excerpt": "redaction-safe excerpt",
  "stderr_excerpt": "",
  "redaction_status": "safe|needs_scan|unsafe",
  "git_fingerprint_before": "optional",
  "git_fingerprint_after": "optional",
  "source": "worker_tool|harness_final_gate",
  "satisfies_release_gate": false
}
```

Storage:

```text
<runtime_root>/self_tests/<task_id>/selftest_<id>.json
<runtime_root>/self_tests/<task_id>/artifacts/<id>.stdout.txt
<runtime_root>/self_tests/<task_id>/artifacts/<id>.stderr.txt
```

Self-test evidence is evidence, not proof. Final release proof remains in
`proofs/<task_id>/proof_<proof_id>.json`.

### Worker Action Projection

Do not add new `DecisionType` values in the first implementation pass. Add a projection:

```json
{
  "worker_action_id": "deliver_patch",
  "underlying_decision_type": "propose_patch",
  "visible": true,
  "primary": true,
  "reason": "Product-edit stage is implementing; deliver patch after self-test.",
  "payload_template": {},
  "not_allowed_reason": null
}
```

The projection is advisory and HUD-facing. The underlying strict `AgentDecision`
validator remains authoritative.

## Stage 50A. Worker-Facing Action Projection

Add a worker-facing action projection on top of the Stage 49 decision registry.

Do not start by adding many new `DecisionType` values. Instead, generate a compact HUD
projection that maps simple action names to existing strict decisions.

Suggested Dev action projection:

| Worker action | Existing decision | When visible |
| --- | --- | --- |
| `deliver_patch` | `propose_patch` | Product-edit stage after code/file changes or concrete patch summary |
| `handoff_to_qa` | `request_qa_review` | Harness final gate passed or explicit no-code review stage |
| `request_context` | `needs_context` / `request_file_reads` | Missing bounded context only |
| `report_blocker` | `block` | Environment, dependency, provider, or human-only blocker with evidence |
| `repair_stage` | `correct_stage` | Harness repair hint or clear stage mismatch only |
| `request_gate` | `request_test_run` | No-edit certification stage, explicit Harness-owned gate, or recovery lane only |

Suggested QA action projection:

| Worker action | Existing decision | When visible |
| --- | --- | --- |
| `qa_verdict` | `report_qa_verdict` | Required gate proof is attached |
| `request_missing_visual_gate` | `request_screenshot` / `request_video` | Visual proof required but absent/stale |
| `request_missing_command_gate` | `request_test_run` | Required command gate absent/stale |
| `report_blocker` | `block` | QA cannot verify because proof/environment is blocked |

Suggested Neko action projection:

| Worker action | Existing decision | When visible |
| --- | --- | --- |
| `assign_scope` | `propose_stage_plan` / `propose_acceptance` | Kickoff or evidence-backed rescope |
| `release_handoff` | `propose_stage_plan` / current handoff packet path | Cross-stack join after proof exists |
| `route_repair` | `correct_stage` / `triage_issue_discovery` | Evidence-backed route repair |
| `report_blocker` | `block` / `request_human` | True human/safety blocker |

Acceptance:

- HUD shows one `primary_action` and at most three `allowed_alternatives`.
- Product-edit Dev HUD does not show `request_gate` until after a delivery or explicit
  Harness repair lane.
- Existing registry validation still owns the real decision packet.
- A contract dump can show both the simple worker action and the underlying decision.

## Stage 50B. In-Session Self-Test Evidence

Make agent-run local tests/builds/analyze checks first-class evidence without treating
them as release proof.

Add a `self_test_evidence` record type or proof metadata mode with:

- worker session ID;
- run ID;
- persona ID;
- stage ID;
- repo label/workdir;
- command label;
- exit code;
- elapsed time;
- stdout/stderr artifact paths or safe excerpts;
- redaction status;
- git dirty fingerprint before/after if available;
- whether the command was run by worker tool or Harness proof runner.

Rules:

- Self-test evidence may support Dev delivery and QA triage.
- Self-test evidence does not by itself satisfy final release gates.
- Failed self-test evidence is shown to the same worker on continuation so it can repair
  without rediscovering context.
- Large logs stay in artifact files; events carry only IDs, status, summary, and safe
  excerpts.

Acceptance:

- Terminal/code-execution tool completions that look like tests/builds/analyze checks can
  be recorded as self-test evidence.
- Failed self-test evidence appears in the next worker HUD with exact command/status/log
  reference.
- Mission Control can render self-test rows separately from final gate proof rows.
- Redaction scanner rules match existing proof artifact rules.

## Stage 50C. Delivery Triggers Final Gate Automatically

Demote `request_test_run` from the normal Dev inner loop.

When Dev emits `propose_patch`/`deliver_patch` with changed files and self-test evidence:

1. Harness validates the delivery packet.
2. Harness checks whether the current stage requires a final command gate, visual gate,
   or both.
3. Harness resolves the final gate recipe from stage intent, affected repo, test plan,
   and proof registry.
4. Harness runs the final gate without asking Dev to emit `request_test_run`.
5. If the gate passes, Harness advances to QA or the next stage.
6. If the gate fails, Harness resumes the same Dev worker with the failed proof ID,
   command summary, and one bounded repair lane.

Use `request_test_run` only for:

- no-edit certification stages;
- QA missing-proof requests;
- explicit Harness-owned final gate requests;
- recovery after a failed gate when the HUD says `request_gate` is allowed;
- operator/manual proof capture.

Acceptance:

- A product-edit stage can go from Dev delivery to Harness final gate in one tick without
  another model decision.
- A failed final gate resumes the same worker session with proof details and does not
  spawn a fresh context-heavy loop.
- A no-edit stage still uses `request_test_run`/recipe proof directly.
- Stage 49 no-edit smoke drift protections remain green.

## Stage 50D. Simplified HUD and Repair Feedback

Change the Mission HUD from "all valid decision shapes" to "the next valid worker move."

HUD must include:

- current role and current stage;
- `primary_action`;
- `allowed_alternatives`;
- `not_allowed_yet` with short reasons;
- latest self-test evidence IDs;
- latest final gate proof IDs;
- failed proof IDs to reuse;
- exact packet skeleton for the primary action only;
- compact context expansion menu.

Example Dev HUD:

```json
{
  "current_stage_id": "mc_terminal_dm_bubble_rows",
  "primary_action": "deliver_patch",
  "allowed_alternatives": ["request_context", "report_blocker"],
  "not_allowed_yet": [
    {
      "action": "request_gate",
      "reason": "Product-edit stage has no delivered patch yet; run self-tests locally, then deliver."
    }
  ],
  "primary_payload_template": {
    "summary": "<patch summary>",
    "changed_files": ["<relative path>"],
    "tests": ["<self-test command and status or not-run reason>"],
    "delivery": {
      "delivery_version": 1,
      "work_status": "patch_proposed",
      "self_test_evidence_ids": ["<evidence id>"]
    }
  }
}
```

Repair feedback must name the correct simple action, not only the raw invalid field.

Acceptance:

- An invalid `request_test_run` on a product-edit stage returns a repair message that says
  to use `deliver_patch` first.
- The HUD does not dump unrelated packet shapes unless the agent requests context
  expansion.
- Contract examples and Stage 46 skills stay aligned with the generated HUD projection.

## Stage 50E. Persona and Skill Updates

Update Neko, Dev, Backend Dev, Launcher Dev, and QA guidance to match the normal flow.

Dev guidance:

- Use skills, but load only the few recommended skills plus task-relevant skills.
- Inspect narrowly.
- Edit files.
- Run narrow self-tests in-session using terminal/code tools.
- Prefer monitored/background command execution for long tests so the worker can keep
  progress visible and avoid hidden hangs.
- Deliver patch with self-test evidence.
- Do not request Harness proof unless the HUD explicitly exposes `request_gate`.

QA guidance:

- Review final gate proof, Dev self-test evidence, and visual/MCP proof when required.
- Do not approve from Dev prose or self-test evidence alone.
- Request one exact missing gate if proof is missing.

Neko guidance:

- Wait semantics are kickoff-only unless there is a true human/safety blocker.
- Route by evidence, not guesswork.
- Prefer continuing the same worker with a repaired HUD over spawning a new worker.

Acceptance:

- Stage 46 skill examples validate against the contract verifier.
- Persona prompt tests prove the old "request Harness proof after patch" default is not
  shown for product-edit inner loops.
- Live Dev logs show local self-test before delivery on product-edit goals.

## Stage 50F. QA Gate Independence

Keep QA independent while removing extra Dev proof turns.

QA should receive:

- Dev delivery packet;
- self-test evidence IDs;
- final Harness gate proof IDs;
- visual/MCP proof IDs when required;
- changed files/diff summary;
- open gaps/blockers.

QA should not receive:

- hidden chain-of-thought;
- noisy raw logs inline;
- unrelated skill dumps;
- every historical event unless expanded.

Acceptance:

- QA can approve only when final gate proof satisfies required proof policy.
- QA can request one missing visual/command gate with a closed action shape.
- QA verdict cites proof/evidence IDs and changed files.

## Stage 50G. Anti-Freeze and Efficiency Policy

Stage 50 should reduce turns, but still keep watchdogs.

Add or tune counters for:

- self-test commands run before delivery;
- delivery-to-final-gate latency;
- failed final gate resumed by same worker;
- repeated `request_gate` attempts before delivery;
- repeated read/search after failed self-test or failed final gate;
- worker action invalid-shape repairs;
- time spent in tool execution vs model calls.

Policy:

- If Dev has no file edits and asks for a product gate, repair to `deliver_patch` or
  `request_context`.
- If Dev runs the same failing self-test twice without edits/environment change, require
  a blocker or Neko route repair.
- If the final gate command hangs, Harness owns timeout/process cleanup and resumes the
  same worker with proof ID.
- If QA screenshot proof is compressed/stale/wrong runtime root, QA requests one exact
  visual gate retry and names the stale artifact.

Acceptance:

- No-freeze monitor flags repeated self-test loops.
- Worker session remains active across a failed final gate repair.
- Mission Control shows why a run cannot proceed if the worker cannot self-heal.

## Stage 50H. Mission Control Display

Mission Control should reflect the simpler worker loop while preserving raw proof.

Agent terminal/event view should group rows into compact DM-bubble style sections:

- `Thinking summary` safe summaries only;
- `Tool call`;
- `Self-test`;
- `Code edit`;
- `Delivery`;
- `Final gate`;
- `QA verdict`;
- `Blocker`.

Each bubble can expand to details/artifact links. Raw logs remain preserved under proof
or evidence artifacts, not pasted into the main feed.

Acceptance:

- The default view tells Tony what the worker is doing without requiring log archaeology.
- Expanded rows expose command, exit code, artifact handles, changed files, and proof IDs.
- Fullscreen visual proof remains required for Mission Control UI changes.

## Stage 50I. Compatibility and Migration

Gate Stage 50 behind Harness config.

Suggested config:

```json
{
  "normal_worker_flow": {
    "enabled": true,
    "dev_self_tests_in_session": true,
    "auto_final_gate_after_delivery": true,
    "hide_request_test_run_until_gate": true,
    "self_test_evidence_capture": true
  }
}
```

Defaults:

- Standard Hermes: off.
- Tony Agent Runtime Harness profile: on after tests pass.

Migration:

- Keep old decisions accepted.
- Project new worker actions into existing decisions.
- Keep `request_test_run` valid for explicit proof stages and backward compatibility.
- Add tests before removing any old HUD paths.

## Stage 50J. Test and Certification Matrix

Unit tests:

- Worker action projection maps to valid Stage 49 decisions.
- Product-edit HUD hides `request_gate` before delivery.
- No-edit proof stage still exposes `request_gate`.
- Self-test evidence records command/status/artifact metadata.
- Dev delivery with self-test evidence triggers final gate automatically.
- Failed final gate resumes same worker with failed proof ID.
- QA cannot approve from self-test evidence alone.
- Invalid product-stage `request_test_run` repairs to `deliver_patch`.
- Archive manifests preserve self-test evidence and final gate proof.

Integration tests:

- Product-edit fake Launcher stage: edit marker -> self-test evidence -> auto gate ->
  QA review.
- Failed self-test loop: same command twice without edits triggers blocker/repair.
- Failed final gate: same worker resumes and fixes.
- No-edit smoke stage: direct recipe proof still works.
- Visual proof stage: QA missing screenshot requests one visual gate.
- Mission Control snapshot includes worker action, self-test evidence, final gate proof,
  and compact event display metadata.

Live-token certification:

1. Small single-stack goal.
   - Expected: Neko -> Dev edit/self-test/deliver -> auto gate -> QA -> done.
   - No manual nudges.
   - No product-stage `request_test_run` before delivery.

2. Complex frontend/backend goal.
   - Expected: Neko -> Backend Dev edit/self-test/deliver -> backend gate -> Neko join
     -> Launcher Dev edit/self-test/deliver -> Launcher gate/visual gate -> QA -> done.
   - Same worker sessions continue through failed gates if any.
   - QA cites final proof IDs and visual artifacts.

3. Mission Control UI goal with screenshot.
   - Expected: fullscreen Stage C screenshot proof, correct runtime root/profile, no
     compressed default-window artifact accepted.

Success bar:

- No babysitting.
- No smoke proof drift.
- No repeated broad read/search loops after failed proof.
- No hidden hangs.
- Clear observability when blocked.
- Token/turn count is materially lower than the Stage 49 drift run.

## Double-Implementation Traps to Avoid

Stage 50 is a simplification layer, not a replacement runtime.

Do not double-implement:

- **Decision schema.** Use Stage 49 registry contracts. Worker actions project to
  existing strict `AgentDecision` packets.
- **Proof runner.** Auto final gates must call the existing command/visual proof
  collection paths. Do not add another subprocess runner for release proof.
- **State machine.** Keep task/stage transitions in `planning.py` and
  `state_machine.py`; Stage 50 should add gating decisions around existing transitions.
- **Worker continuation policy.** Reuse Stage 48 worker sessions and Stage 47 role
  session policy. Do not add a parallel "normal flow session" object.
- **Mission Control schema truth.** Harness snapshot remains the schema source;
  Launcher only renders projected metadata.
- **Skill contract truth.** Skills contain examples and guidance, but contract verifier
  remains authoritative.

Safe additions:

- Worker-action projection helpers.
- Self-test evidence store.
- Final-gate resolver that calls the existing proof runner.
- HUD display projection.
- Mission Control display categories.

Rollback strategy:

- `normal_worker_flow.enabled=false` restores the Stage 49 behavior.
- Self-test evidence records remain preserved even if the simplified HUD is disabled.
- Auto final gate can be disabled independently with
  `auto_final_gate_after_delivery=false`, leaving delivery packet validation intact.

## Implementation Order

These substages are intended to be implemented in order. Each one should leave the
Harness runnable and testable.

### Stage 50.1. Config Gate and Worker Action Projection

Add `normal_worker_flow` config and a worker-action projection layer over the Stage 49
registry. Standard Hermes remains unchanged. Tony's Harness profile enables the flow
after tests pass.

Implementation:

- Add `NormalWorkerFlowConfig` to `agent_runtime/runtime_config.py`.
- Parse it in `agent_runtime/config.py`.
- Add `agent_runtime/worker_actions.py` with pure helpers:
  - `worker_actions_for_role(role, task, run, config, proof_store=None)`;
  - `primary_worker_action(...)`;
  - `project_worker_action_to_decision_shape(...)`.
- Reuse `stage_intent.stage_requires_product_edit(...)` and
  `no_product_edit_recipe_conflicts_with_stage(...)`.
- Do not mutate task state in this module.
- Add registry invariant: every visible worker action maps to a role-allowed decision.

Exit criteria:

- Config validates.
- Worker actions map to existing decisions.
- Contract dump exposes both worker action and underlying decision.
- Standard config with `normal_worker_flow.enabled=false` produces the existing HUD
  shape index.
- Role-visible HUD shapes are also validator-allowed shapes.

### Stage 50.2. Simplified HUD Menus

Update HUD generation so each worker sees one primary action, a few alternatives, and
not-allowed-yet reasons instead of every possible packet shape.

Implementation:

- In `context_builder.py`, branch HUD rendering when
  `config.normal_worker_flow.enabled` is true.
- Add `worker_action_menu`, `primary_worker_action`, `allowed_alternatives`, and
  `not_allowed_yet`.
- Keep `decision_shape_index`, but include only the primary action shape and explicit
  context expansion shapes by default.
- Fix `hud_shape_index_for_role(...)` so common shapes are role-filtered even when the
  normal flow is disabled.
- Update repair HUD text for premature product-stage proof:
  `Use deliver_patch first; run self-tests locally and cite self_test_evidence_ids.`

Exit criteria:

- Product-edit Dev HUD shows `deliver_patch`, not premature `request_gate`.
- No-edit stages still show direct proof/gate actions.
- Repair feedback names the simple action to use next.
- Dev/QA/Neko HUD menus contain no shape whose decision type fails
  `validate_decision_for_role`.
- Existing Stage 49 tests either remain green or are deliberately split into legacy-HUD
  and normal-flow-HUD expectations.

### Stage 50.3. Self-Test Evidence Capture

Record terminal/code-execution test/build/analyze checks as self-test evidence tied to
worker session, run, stage, command, exit code, log artifacts, and redaction status.

Implementation:

- Add `agent_runtime/self_test_evidence.py`:
  - `SelfTestEvidence` dataclass;
  - `SelfTestEvidenceStore`;
  - `looks_like_self_test_command(command)`;
  - `record_self_test_from_tool_event(...)`.
- Add `paths.self_tests_dir()`, `paths.self_test_task_dir(task_id)`, and
  `paths.self_test_record_path(task_id, evidence_id)`.
- Add event contracts:
  - `self_test.recorded`;
  - `self_test.reused`;
  - `self_test.loop_detected`.
- Capture from structured tool callbacks if available. If only redaction-safe tool
  summaries are available, record the summary and artifact handle, not raw logs.
- Treat `flutter analyze`, focused `flutter test`, `pytest`, `python -m pytest`,
  Django `manage.py test/check`, `dart analyze`, and repo-local smoke commands as
  self-test candidates.
- Do not auto-record generic `where`, `which`, `--version`, `doctor`, broad repo search,
  or Docker sanity checks as self-tests. Those are preflight or environment evidence.

Exit criteria:

- Passing and failing self-tests persist as evidence.
- Failed evidence appears in the next same-worker HUD.
- Self-test evidence cannot satisfy final QA/release gates by itself.
- Re-running the same failed command without file/env change increments a loop counter.

### Stage 50.4. Delivery Packet Upgrade

Allow Dev delivery packets to cite self-test evidence IDs and changed files while keeping
the Stage 49 registry strict.

Implementation:

- Add `self_test_evidence_ids` to the `delivery` object contract.
- Add semantic validation:
  - IDs must exist;
  - IDs must belong to the same task;
  - Dev IDs must match the same stage unless explicitly joined by Neko;
  - unsafe evidence may be cited but must not be displayed inline or used as gate proof.
- Update `decision_contract_examples.py` wrappers so delivery examples still validate.
- Update Stage 46 skills:
  - `harness-dev-delivery`;
  - `launcher-analyze-proof`;
  - `harness-qa-verdict`;
  - `harness-mission-lead`.

Exit criteria:

- `deliver_patch` projection emits a valid `propose_patch` packet.
- Unknown delivery fields still reject.
- Stage 46 skill examples validate.
- Delivery packets preserve `self_test_evidence_ids` in `packet.recorded` events and
  snapshot summaries.

### Stage 50.5. Auto Final Gate After Delivery

After accepted Dev delivery, Harness resolves and runs the required final gate without a
second Dev `request_test_run` turn.

Implementation:

- Add `agent_runtime/final_gate.py` with pure helpers:
  - `final_gate_required(task, stage, delivery_packet)`;
  - `resolve_final_gate_recipe(task, stage, proof_recipes, delivery_packet)`;
  - `build_final_gate_decision(task, stage, recipe_or_commands)`.
- In `ticker.py`, after `state_machine.apply_decision(...)` handles
  `DecisionType.PROPOSE_PATCH`, check normal-flow config and call the same proof
  collection path used by `REQUEST_TEST_RUN`.
- Store final-gate proof metadata with:
  - `gate_source: "auto_after_delivery"`;
  - `delivery_packet_id`;
  - `self_test_evidence_ids`;
  - `recipe_id`/`recipe_hash` when applicable.
- Do not resolve a broad fallback command if no safe gate exists. In that case, route to
  Neko/QA with an explicit missing-gate reason.
- Preserve Stage 49 protections:
  - no no-edit smoke proof for product-edit stages;
  - no later-stage proof bypass;
  - workdir repo-intent validation.

Exit criteria:

- Product-edit delivery triggers command/visual gate automatically when required.
- Gate pass advances to QA or next stage.
- Gate failure resumes the same worker with proof ID and repair HUD.
- No second model decision is needed between `deliver_patch` and final command proof.

### Stage 50.6. Same-Worker Gate Repair

Make failed final gates feel like one uninterrupted worker session, not a fresh
assignment.

Implementation:

- Reuse `WorkerSessionStore` and existing same-session continuation policy.
- Attach failed final-gate proof IDs to worker context receipts.
- Add HUD repair state:
  - `primary_action: deliver_patch`;
  - failed proof IDs;
  - exact failed command;
  - "do not repeat without edit/environment change" warning.
- Add self-heal counters for:
  - same failed final gate;
  - same failed self-test;
  - read/search after failed gate without edits.
- If the worker cannot self-heal after the configured cap, route to Neko with
  `route_repair` and proof IDs.

Exit criteria:

- Same worker receives failed proof ID, command summary, and bounded retry lane.
- Repeating the same failed self-test/gate without edits or environment change is
  blocked or routed to Neko.
- No broad read/search loop restarts after failed proof.
- Worker/session metrics show continuity across the failed gate and repair attempt.

### Stage 50.7. Persona and Skill Alignment

Update Neko, Dev, Backend Dev, Launcher Dev, and QA skills/prompts to prefer the normal
flow: implement, self-test, deliver, Harness gates, QA verifies.

Implementation:

- Update `persona_runtime.py` prompt text behind the normal-flow config.
- Update Stage 46 skills in `docs/agent-runtime-harness/stage46-skills/`.
- For Dev skills, replace "prefer Harness-managed proof requests" as the default with:
  "run narrow self-tests in-session; deliver with evidence; Harness will run the final
  gate."
- Keep Harness-managed proof language for no-edit certification, final gates, QA missing
  proof, and recovery lanes.
- Add prompt tests that inspect the generated system prompt/HUD text for product-edit
  stages.

Exit criteria:

- Dev guidance no longer treats `request_test_run` as the default inner-loop move for
  product edits.
- QA guidance distinguishes self-test evidence from final gate proof.
- Neko guidance favors same-worker repair over new-worker churn.
- Skill examples include at least one `self_test_evidence_ids` delivery shape.

### Stage 50.8. Mission Control Compact Worker Feed

Render the simplified flow as compact DM-bubble event rows with expandable details while
preserving raw logs/artifacts.

Implementation:

- Harness snapshot adds compact event display fields:
  - `display_kind`;
  - `display_title`;
  - `display_summary`;
  - `artifact_refs`;
  - `expandable_details`;
  - `redaction_status`.
- Event kinds:
  - `thinking_summary`;
  - `tool_call`;
  - `self_test`;
  - `code_edit`;
  - `delivery`;
  - `final_gate`;
  - `qa_verdict`;
  - `blocker`.
- Launcher parses these fields in `mission_control_snapshot.dart`.
- Launcher renders compact bubbles in `mission_control_page.dart`.
- Keep raw log/proof/evidence artifacts behind expanders or proof inspectors.
- Do not render hidden provider chain-of-thought. Keep the current placeholder language
  for non-redaction-safe reasoning summaries.

Exit criteria:

- Feed groups safe summaries, tools, self-tests, code edits, delivery, final gates, QA
  verdicts, and blockers.
- Expanded rows expose artifact/proof/evidence handles.
- Raw logs stay in artifacts.
- Fullscreen Stage C screenshot validates Mission Control compact feed with real Harness
  runtime root/profile.

### Stage 50.9. Test Coverage and Regression Gates

Add unit, integration, contract, archive, and Mission Control snapshot tests for the new
flow.

Implementation:

- Add/extend Harness tests:
  - `tests/agent_runtime/test_worker_actions.py`;
  - `tests/agent_runtime/test_self_test_evidence.py`;
  - `tests/agent_runtime/test_context_builder.py`;
  - `tests/agent_runtime/test_decision_contract_registry.py`;
  - `tests/agent_runtime/test_decision_contracts.py`;
  - `tests/agent_runtime/test_ticker.py`;
  - `tests/agent_runtime/test_store.py`;
  - `tests/agent_runtime/test_snapshot.py`;
  - `tests/agent_runtime/test_no_freeze_monitor.py`.
- Add/extend Launcher tests:
  - `test/features/mission_control/mission_control_snapshot_test.dart`;
  - `test/features/mission_control/mission_control_page_test.dart`;
  - `test/features/mission_control/mission_control_bridge_test.dart`.
- Keep the Stage 49 full regression command:
  `python -m pytest -o addopts='' tests/agent_runtime -q`.
- Keep contract verification:
  `python -m hermes_cli.main harness contracts verify-examples --json`.

Exit criteria:

- Full `tests/agent_runtime` passes.
- Contract examples verify.
- Archive manifests preserve self-test evidence.
- Existing no-edit proof stage behavior remains valid.
- Focused Launcher Mission Control tests pass before visual certification.

### Stage 50.10. Live-Token Certification

Run small, complex cross-stack, and visual Mission Control live-token goals.

Implementation:

- Before live tokens:
  - verify `harness status --json` is clean;
  - clear/archive test goals while preserving evidence;
  - confirm `normal_worker_flow.enabled=true` only for Tony Harness profile;
  - rebuild Launcher debug target when visual proof is in scope;
  - close stale `eternia_launcher.exe` and `stagec_qa_mcp_server.exe` processes before
    fresh visual proof.
- Monitor:
  - worker action sequence;
  - self-test evidence IDs;
  - final gate proof IDs;
  - same-session continuation;
  - repeated read/search counters;
  - screenshot runtime root/profile pins.
- If a live run fails, patch the root cause and rerun the smallest certification that
  proves the fix before attempting the complex goal again.

Exit criteria:

- No babysitting.
- No premature product-stage `request_test_run`.
- Dev self-tests before delivery.
- Harness final gate runs automatically.
- QA verifies from final proof IDs and visual artifacts.
- Token/turn count improves over the Stage 49 drift run.

## Expected Result

After Stage 50, the Harness still has enterprise-grade proof, audit, archive, worker
session, and Mission Control internals. The agents, however, experience a simple loop:

```text
do the task, self-test it, deliver it, let Harness gate it, let QA verify it
```

That is the correct balance: rich control plane, small worker interface.
