---
name: harness-qa-verdict
description: QA proof review verdict contract for Harness missions, including cross-stack coverage, missing proof, visual/MCP blockers, and structured qa_review metadata.
---

# Harness QA Verdict

Use this skill for QA review of Harness implementation proof.

## QA Contract

- Review proof IDs, packet summaries, and focused target files only when proof metadata is insufficient.
- Read Mission HUD before choosing an action. Treat `mission_hud.agent_hud` as the only live control panel.
- Choose one visible `agent_hud.options[]` item. Prefer `agent_hud.recommended_action`; copy `decision_type`, `shape_id`, allowed keys, enum values, and `payload_skeleton` from that object. Do not invent payload fields, checklist item IDs, checklist statuses, packet fields, stage IDs, owners, proof lanes, or QA review keys.
- If `validation_repair` is present, repair from `validation_repair.corrected_shape` and retry once. Do not repeat the same malformed payload. If repair is not possible from the corrected shape, emit `block` with a redaction-safe `log_ref`.
- Treat QA verdicts as first-class review packets. Cite the packet IDs and proof IDs reviewed, and put blocking gaps in supported `qa_review.remaining_gaps` or the HUD's current QA verdict field. Do not reject because information is absent from one prose field when it exists in another supported delivery field.
- If Harness reports dropped, normalized, stale, or unsupported packet fields, review the normalized packet only and request one exact packet repair when acceptance-critical content was lost.
- For Dev handoff repair, review first-class delivery fields before rejecting: `inspected_paths`, `changed_paths`, `dirty_baseline`, `coverage_claims`, `known_non_coverage`, `proof_reuse_basis`, `failed_proof_classification`, and `handoff_repair`. If these fields prove the existing proof still covers the stage, do not require another Dev run just because the raw prose was compacted.
- Treat every Harness response like terminal stdout/stderr. On the next prompt, read HUD, recent events, proof records, proof feedback, and `next_expected` as the result of your prior verdict/proof request: accepted, rejected, ignored, proof attached, proof missing, state changed, blocked, or retryable.
- If an accepted verdict/proof request did not advance the mission, do not repeat it blindly. Use returned feedback to choose the next visible HUD action, request the exact missing lane once, or block with exact evidence.
- `checklist_updates` is optional. Use it only for QA-owned checklist item IDs shown in the HUD. QA may mark reviewed QA items `verified`; do not mark Dev/Neko local items verified unless the HUD explicitly lists that item as QA-owned.
- Treat `agent_hud` as the primary operating surface. Legacy/debug HUD fields are not valid action choices.
- Use `agent_hud.repo_bundles`, `agent_hud.qa_waiting_on`, and proof IDs as the release boundary. Do not approve while `qa_waiting_on` contains a non-terminal bundle unless the verdict explicitly rejects/blocks that bundle.
- Include `repo_bundle_id` for every rejected or missing-proof lane when the HUD provides one. Never invent a bundle ID; use only IDs shown in `agent_hud.repo_bundles`.
- In Stage 53 simplified mode, QA's only product actions are `approve`, `reject`, and `request_missing_proof`.
- Judge final outcome coverage, not every intermediate patch. Separate focused command proof, visual runtime proof, visual acceptance proof, unrelated failures, and proof caveats.
- Treat Dev self-test evidence as helpful triage context, not release proof. Approval requires Harness final gate proof IDs and required visual/MCP proof when applicable.
- For product-edit tasks in EterniaBackend or EterniaLauncher, approval also requires the production promotion chain as passed proof records in chronological order: local deterministic product tests, remote test staging k8 pod validation, then production pod rollout proof. EterniaBackend product edits additionally require a local Docker/PostgreSQL integration proof before staging. Accept only the backend `scripts/test.sh` default Postgres tier, or an equivalent proof that cites the backend `docs/testing/README.md` doctrine and real Docker/PostgreSQL services; `scripts/test.sh --sqlite` and mocked-only tests are not release proof. A local final gate or commit alone is not a prod deployment verdict.
- When reviewing Backend Docker/PostgreSQL proof, accept logs from `python scripts/backend_postgres_proof.py --backend-root "X:\Unreal Engine\Engine\EterniaBackend\eternia-backend"` as release-lane evidence only when the run used the default `scripts/test.sh` Docker/PostgreSQL tier and exited green. Reject dry-run output, SQLite escape-hatch output, or focused-only output when the stage requires full local backend proof.
- When prod deployment is push-triggered, approve only if the prod proof shows a remote sync step before the push, such as pull/fetch plus rebase when needed, and the push/deploy trigger. If the proof shows only `git push`, classify the prod rollout lane as missing.
- For no-product-edit investigation/audit stages, command proof may be intentionally absent. Review the latest `delivery` packet instead; cite its packet ID in `delivery_packets_reviewed` or `qa_review.delivery_packets_reviewed`, verify it preserves `summary`, `findings`, `recommendations`, `questions`, `known_gaps`, and any relevant first-class handoff fields, then approve, request fixes, or block based on that packet.
- For NSFW/media-safety investigations, accept explicit local model guidance in supported `delivery.model_options` and WD tagger guidance in supported `delivery.wd_tagger_assessment`; do not require those details to be duplicated in recommendations when those fields are present.
- Do not patch, route directly to Backend Dev, or approve visual claims without visual/MCP proof when required.
- Missing proof is not an implementation defect; report it as missing proof or request one exact proof lane.
- If acceptance criteria or stage text names an exact focused command/path such as `tests/agent_runtime/test_*.py`, approve only proof that covers that path. A generic observability/status recipe is insufficient unless Neko explicitly changed the proof gate and the stage no longer names the focused path.
- For command proofs, review `metadata.original_command` as the requested proof target when present. Harness may record an adapted `metadata.command` with Windows-safe pytest flags plus `metadata.command_adapter`; if `original_command` covers the requested path and the proof passed, do not block only because the executed command has Harness safety flags.
- Cross-stack gaps route to Neko Mission Lead.
- Before requesting or reviewing Launcher Stage C screenshot/video proof, verify the proof lane was captured from a fresh intended Launcher window. If `eternia_launcher.exe` or stale `stagec_qa_mcp_server.exe` processes were already running before rebuild/capture, require cleanup/relaunch or classify the proof as blocked/stale. This preflight is for Windows build/Marionette/visual proof freshness; do not reinterpret normal `flutter test` assertion failures as process-lock failures unless the proof log shows a lock/attach/build error.

## QA Verdict

When `agent_hud` is present, prefer this product shape in the decision summary/rationale and let Harness project it internally:

```json
{
  "action": "approve",
  "repo_bundle_ids": ["bundle_id_from_agent_hud"],
  "proofs_reviewed": ["proof_id"],
  "verdict": "approved",
  "summary": "Acceptance is covered by focused command proof and required visual proof.",
  "follow_up": "none"
}
```

For missing proof:

```json
{
  "action": "request_missing_proof",
  "repo_bundle_id": "bundle_id_from_agent_hud_or_null",
  "missing_lane": "visual_acceptance",
  "minimum_needed": "Screenshot or video showing the specific changed UI state with populated fixture data.",
  "proofs_reviewed": ["proof_id"],
  "why_current_proof_is_insufficient": "The current screenshot proves runtime load only, not the acceptance claim."
}
```

Use the HUD `recommended_action.payload_skeleton` first. The template above is explanatory guidance for deciding what substance belongs in the verdict.

## Request Missing Proof

Use when the HUD recommends a missing proof action or a required proof lane is absent/stale. Name the exact proof lane, proof IDs reviewed, and the minimum acceptable replacement.

## Report Blocker

Use when proof cannot be collected or reviewed due to an external/environmental/Harness blocker. Include exact redaction-safe evidence.

## QA Review Shape

Attach `payload.qa_review` to QA verdicts and proof requests:

```json
{
  "qa_review_version": 1,
  "review_scope": "implementation",
  "mission_phase": "qa_verification",
  "proof_reviewed": [
    "test_task_example_required_proof"
  ],
  "coverage": {
    "backend_contract": "not_required",
    "launcher_integration": "not_required",
    "visual_or_mcp": "not_required",
    "cross_stack_join": "not_required"
  },
  "contract_packets_reviewed": [],
  "delivery_packets_reviewed": [],
  "mcp_status": "not_required",
  "remaining_gaps": [],
  "decision_basis": "proof_packet",
  "next_owner": "harness"
}
```

Coverage values: `not_required`, `missing`, `reviewed`, `blocked`, `failed`.

Allowed `qa_review` keys only: `qa_review_version`, `source_handoff_packet_id`, `review_scope`, `mission_phase`, `proof_reviewed`, `coverage`, `contract_packets_reviewed`, `delivery_packets_reviewed`, `repo_bundle_ids`, `mcp_status`, `decision_basis`, `remaining_gaps`, `next_owner`.

Do not add `notes`, `summary`, `confidence`, `raw_logs`, `paths`, nested extra objects, or absolute paths inside `qa_review`. Put human-readable explanation in the top-level `summary`, `rationale`, `findings`, and `remaining_gaps`. In redaction-scanned packet text, prefer `live-model` over wording that contains secret-bearing terms unless you are citing the task title.

## Implementation Verdict Template

For a passed implementation review, emit this shape and only fill with actual proof IDs:

```json
{
  "type": "report_qa_verdict",
  "summary": "Implementation proof reviewed; required Stage 46 coverage is satisfied.",
  "rationale": "Reviewed attached proof IDs and any required packet/proof lanes. Visual/MCP and cross-stack proof were not required unless listed by HUD.",
  "payload": {
    "review_scope": "implementation",
    "verdict": "approved",
    "proof_ids": [
      "actual_required_proof_id"
    ],
    "findings": [],
    "qa_review": {
      "qa_review_version": 1,
      "review_scope": "implementation",
      "mission_phase": "qa_verification",
      "proof_reviewed": [
        "actual_required_proof_id"
      ],
      "coverage": {
        "backend_contract": "not_required",
        "launcher_integration": "not_required",
        "visual_or_mcp": "not_required",
        "cross_stack_join": "not_required"
      },
      "contract_packets_reviewed": [],
      "delivery_packets_reviewed": [],
      "repo_bundle_ids": ["bundle_id_from_agent_hud"],
      "mcp_status": "not_required",
      "decision_basis": "proof_packet",
      "remaining_gaps": [],
      "next_owner": "harness"
    }
  }
}
```

## Verdict Rules

- Approve only when required coverage is `reviewed` or explicitly `not_required` by Neko.
- For single-repo/non-cross-stack tasks, set `backend_contract`, `launcher_integration`,
  and `cross_stack_join` to `not_required` unless HUD/stage context explicitly requires them.
- If backend or Launcher proof is missing on a cross-stack mission, use `next_owner: "neko_supervisor"`.
- If a real cross-stack lane is `missing` or `failed`, use `next_owner: "neko_supervisor"`.
- If visual/MCP proof is required and unavailable, return a blocked verdict or request one exact screenshot/video proof.
- Findings must cite proof IDs, packet IDs, event IDs, or safe relative handles, never raw logs or absolute paths.

## Commit and Deploy Gate (required for product-edit stages)

For any stage with `requires_product_edit`, do not approve unless the delivery packet
shows the work was committed and deploy-checked:

- `delivery.commit_refs` must contain at least one `<repo>@<branch>:<short_sha>`
  reference for the changed paths. Uncommitted working-tree edits are not a deliverable;
  return `needs_fixes` steering Dev to commit exactly the changed paths (never the
  pre-existing dirty baseline files).
- A passed Harness final-gate proof in task Proof Records satisfies local deploy verification
  even if `delivery.deploy_verification.proof_id` is absent. If the delivery includes
  a proof ID, verify that it references the passed repo deploy-check
  (EterniaBackend: `manage.py check`; EterniaLauncher: focused `flutter analyze`/build/test;
  hermes-agent: focused `pytest`). Do not reject solely because Dev did not copy the
  auto-attached proof ID into `delivery.deploy_verification`.
- For EterniaBackend and EterniaLauncher product-edit stages, local deploy verification is only
  the first promotion lane. Before approving, verify task Proof Records also contain passed
  remote test staging k8 pod validation and passed production pod rollout proof created after
  the local proof in that order. If either lane is absent or stale, request missing proof for the
  exact lane instead of approving from prose.
- If pushing to the protected branch triggers deployment, the production rollout proof must
  include a remote sync/rebase step before `git push`. Reject or request missing proof when
  the proof only demonstrates an unsynced push.
- The Harness also enforces this gate at handoff; if a delivery reached you without
  these fields, treat it as a Harness gap and reject with the exact missing field.
- No-edit (investigation/proof-only/context) stages are exempt; do not demand commits
  for stages that changed nothing.
