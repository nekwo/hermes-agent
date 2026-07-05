---
name: harness-dev-delivery
description: Shared Backend Dev and Launcher Dev delivery contract for Harness tasks, proof reuse, bounded skill loading, proof requests, graph handoff, and blockers.
---

# Harness Dev Delivery

Use this skill for non-trivial Backend Dev or Launcher Dev Harness ticks.

## Delivery Rules

- Start from the latest Neko `handoff_packet` and current stage.
- Read Mission HUD before choosing an action. Treat `mission_hud.agent_hud.status_lane` as Harness-verified status and `mission_hud.agent_hud` action fields as bounded steering/handoff affordances.
- For product-edit work, do the work with native tools: inspect narrowly, patch files, run focused self-tests, then hand off with the visible delivery action. Do not turn patching or testing into a per-operation HUD form.
- Prefer the recommended visible action and use only its allowed payload keys. Do not invent payload fields, checklist item IDs, checklist statuses, packet fields, stage IDs, owners, proof lanes, or `delivery.work_status`.
- If `validation_repair` is present, repair from `validation_repair.corrected_shape` and retry once. Do not repeat the same malformed payload. If the corrected shape is insufficient, emit `block` with a redaction-safe `log_ref`.
- Treat `hand_off` as a compact completion signal. Put acceptance-critical caveats in supported fields such as `summary` and `known_gaps`; Harness captures the diff, observed self-test trace, packets, and authoritative gate state.
- If Harness says fields were dropped, normalized, stale, or unsupported, assume those details are not reviewable. Repair once using the visible HUD action and supported fields.
- Treat every Harness response like terminal stdout/stderr. On the next prompt, read HUD, recent events, proof records, context feedback, and `next_expected` as the result of your prior action: accepted, rejected, ignored, proof attached, context unavailable, state changed, blocked, or retryable.
- If an accepted action did not advance the stage, do not repeat it blindly. Use the returned feedback to choose the next visible HUD action. Repeat only when new evidence, changed environment signal, or explicit repair guidance is present.
- Read `agent_hud.evidence_stack` before deciding. Missing proof, stale proof, and blocked-stage entries are advisory evidence for the goal owner to adjudicate; they are not a terminal dead-end. Respond with the visible HUD action that supplies the smallest missing proof/input or reports the exact blocker.
- If context/proof feedback says `unsupported`, `redaction_risk`, `path_not_found`, `stale`, or `missing`, narrow once using repo-relative handles from the HUD; if still unavailable, block with that exact feedback rather than guessing. For investigation stages where exact files are unknown, request one or two likely repo-relative directories first, such as `moderation`, `posts`, `media`, `backend/settings`, or `docs/posts`; Harness returns bounded directory listings that can be followed with exact file reads.
- `checklist_updates` is optional. Use it only for item IDs shown in the current role checklist HUD. Use `self_approved` only for your own role's item, never `verified` unless the item owner is your role, and omit checklist updates when uncertain.
- For `kind: context` no-product-edit investigation/audit stages with no proof recipe/test plan, do not use `request_test_run`. Request bounded file/log context first, then use `hand_off` with a concise findings summary and `known_gaps`, or block with evidence.
- For graph handoff or handoff repair, do not invent packet metadata. Use the visible action, cite existing packet/proof IDs in the summary when needed, and let Harness derive the review state.
- For NSFW/media-safety investigations, summarize model/tool findings in `summary` and unresolved caveats in `known_gaps`; do not invent nested packet fields.
- Treat `agent_hud` as the primary operating surface. Legacy/debug HUD fields are not valid action choices.
- Treat `agent_hud.current_assignment` as stage-shaped, not role-shaped. Use its `stage_id`, `output_type`, `proof_gate`, `required_proof_types`, and `outgoing_edges` to decide whether to patch, deliver a document/artifact, or request the exact proof lane.
- If `agent_hud.current_assignment.repo_bundle_id` is present, include that same `repo_bundle_id` in supported payload fields that accept it. Never invent a bundle ID; use only IDs shown in `agent_hud.repo_bundles`.
- If your bundle is `queued_waiting_dependency`, do not start implementation. Read `dependency_bundle_ids`, consume available contract/proof packets, then either hand off/wait or request the exact missing input from the owning persona.
- In Stage 53 simplified mode, Dev's only product actions are `deliver`, `report_blocker`, and `request_missing_input`.
- In Stage 53 simplified mode, patch, run the narrowest relevant self-test while context is hot, then deliver. Do not ask Harness for a proof recipe when you can run the focused proof directly in the same session.
- Use `request_missing_input` for blocking cross-role gaps: `backend_contract` to Backend Dev, `frontend_usage` to Launcher Dev, `visual_verification` to QA only when the active blueprint includes a QA/verifier node, `scope_decision` to Neko, and `environment_blocker` to Harness preflight/self-heal.
- Stay inside the resolved repo and stage.
- Use skill search first; load one relevant skill by default and two at most unless the AgentDecision explains why more were needed.
- If failed proof IDs are attached, inspect those proof packets before broad read/search.
- When the Mission HUD says `decision_contract_mode: normal_worker_flow` and the current stage is a product edit, do not use `request_test_run` as the normal inner loop. Inspect narrowly, edit files, run focused self-tests in-session with terminal/code tools, then emit `hand_off`; Harness will run the final deterministic gate after handoff.
- In normal worker flow, do not copy self-test evidence IDs into the decision payload. Self-test evidence supports triage and repair through Harness trace capture; final proof IDs still come from Harness gates.
- For product-edit tasks in EterniaBackend or EterniaLauncher, local delivery is not production deployment. After the local Harness final gate, QA approval uses passed proof records in order as evidence: local deterministic product tests, remote test staging k8 pod validation, then production pod rollout proof. EterniaBackend product edits additionally require a local Docker/PostgreSQL integration proof before staging: read the backend `docs/testing/README.md` and use `scripts/test.sh` default Postgres tier. Do not use `scripts/test.sh --sqlite` or mocked-only tests as release proof. Do not invent Backend Docker/PostgreSQL, staging, or prod proof IDs; report/request the exact missing lane through the HUD when those proofs are not present.
- For Backend Docker/PostgreSQL proof collection, use `python scripts/backend_postgres_proof.py --backend-root "X:\Unreal Engine\Engine\EterniaBackend\eternia-backend"` from the Hermes repo, adding focused test targets after `--` only when the stage is explicitly focused. The helper tees the real `scripts/test.sh` default tier to backend `qa_artifacts` and refuses SQLite/mocked-only proof markers.
- If production deployment is triggered by `git push`, sync from remote before pushing: fetch/pull, rebase when needed, resolve conflicts, rerun the relevant local proof when HEAD changes, then push. The production proof must show the remote sync/rebase step and the push/deploy trigger; a raw push alone is not acceptable prod rollout proof.
- For a cross-stack `contract_join`, treat Latest Handoff Packet, Latest Delivery Packet, Proof Records, and Recent Events as the authoritative packet body. If `joined_proof_ids`, `proof_gate.required_proof_ids`, or Proof Records contain the upstream proof, consume it; do not spend a turn asking Neko/Backend for context already present in the Harness prompt.
- Never copy redacted path labels such as `<path:EterniaLauncher>` or `<path:eternia-backend>` into proof commands. The Harness proof runner already executes from the resolved repo workdir; request repo-relative commands.
- After an environment change, choose exactly one bounded retry command or one narrower equivalent command.
- If a no-edit stage names an exact focused command/path, including `tests/agent_runtime/test_*.py`, that exact command/path is the proof gate and outranks generic `available_proof_recipes`. Request the focused command proof; use a recipe only when no exact command/path is named.
- For smoke, observational, or no-edit stages, never request an unbounded full-suite command such as `manage.py test --noinput`, `flutter test`, or `pytest` over an entire repo unless Neko explicitly made full-suite proof the stage gate. Do not chain a readiness command with a later full-suite command. Prefer a targeted smoke command, a single focused test file, `--help`/`--version` readiness proof, or a short import/compile check that proves the current handoff claim.
- For Backend smoke with no product edits, default to `.EterniaBackendVirtualEnv/Scripts/python.exe manage.py check --deploy` or `.EterniaBackendVirtualEnv/Scripts/python.exe manage.py check`. Use `.EterniaBackendVirtualEnv/Scripts/python.exe manage.py test <specific.app.or.test.path> --noinput` only when Neko or the stage identifies that exact target.
- For Stage 47 burn-in, routing-burn-in, bounded-complex-burn-in, or no-product-edit proof stages, do not spend broad read/search turns after loading this skill. If the task, handoff, or stage names a command, return `request_test_run` immediately with that command. If it does not, choose one narrow default proof command for your repo (`.EterniaBackendVirtualEnv/Scripts/python.exe manage.py check` for Backend, or focused `flutter analyze <relevant path>` for Launcher) and request Harness-managed proof. Do not use `flutter --version`, `flutter doctor`, or tool-location checks as Launcher contract proof; those are preflight/readiness signals only. For `launcher_contract_smoke` or cross-stack contract joins, the proof command must deterministically name/validate the consumed backend packet and backend proof ID. If the default is unsafe or impossible, return `block` with the exact missing prerequisite.
- If Backend proof uses inline Python that imports Django directly, set `DJANGO_SETTINGS_MODULE=backend.settings` before `django.setup()` and keep `DJANGO_ENV=dev` or the stage-specified environment as the selector. Do not set `DJANGO_SETTINGS_MODULE` to a submodule such as `backend.settings.dev`; the backend dispatcher owns that choice.
- For Backend health/client proof, prefer `python manage.py shell -c "..."` or an inline Python command with both `DJANGO_SETTINGS_MODULE=backend.settings` and the intended `DJANGO_ENV`. A command with `DJANGO_ENV` alone is not valid Django proof.
- For Backend-to-Launcher contract handoff proof, put the compact backend contract body under `delivery.contract_packet` in the `request_test_run` delivery packet. Use redaction-safe fields such as `endpoint` or `surface`, `request_shape`, `response_shape`, `error_shape`, and `example_response`; set or allow Harness to default `contract_packet_id`, and include `consumed_proof_ids` for the backend proof. If the request requires auth, describe only shape with `request_shape.auth_shape: "required; runtime value omitted; shape only"`; never include auth paths, headers, cookies, tokens, bearer strings, or sample credential values. Do not invent unsupported top-level packet fields.
- For Launcher smoke with no product edits, default to a focused `flutter analyze <changed-or-relevant-path>` or one named widget/unit test file. Use repo-wide `flutter test` only when Neko explicitly made full-suite proof the gate.
- For Launcher Windows debug rebuilds, Marionette freshness checks, Stage C MCP screenshots/videos, or visual proof requests, first check for already-running `eternia_launcher.exe` and stale `stagec_qa_mcp_server.exe` processes. Close/kill them before requesting a fresh rebuild or capture if the proof depends on a fresh window/binary. Do not use this as a blanket excuse for `flutter test` failures; it is a Windows build/Stage C visual-proof preflight.
- Prefer in-session focused self-tests for product-edit implementation while context is hot. Prefer Harness-managed proof requests only when the HUD exposes `request_gate`, the stage is no-edit/certification, QA requests a missing proof lane, or you are repairing a failed final gate. Do not run unbounded suites manually; choose the narrowest self-test or block with evidence.
- If the only available command is long-running, request the narrowest command with a clear timeout expectation in the summary; do not rely on the Harness timeout as the normal success path.
- If Harness repair feedback says `request_test_run` cannot target a later or different stage, either request proof for the current stage or emit `correct_stage` using the repair template below. Do not attach a delivery packet to `correct_stage`.
- Preserve raw logs in Harness proof artifacts; do not paste noisy logs into the decision.

## Commit and Deploy Gate (required for product-edit stages)

The Harness rejects a product-edit graph handoff that lacks commit evidence. In normal
worker flow, Harness runs the deterministic final deploy-check after a valid Dev
handoff and attaches the authoritative proof. Before `hand_off` on a stage with
`requires_product_edit`:

1. Commit exactly your changed paths on the current branch:
   `git add <changed_paths> && git commit -m "<bounded slice summary>"`. Never
   `git add -A`; never stage pre-existing dirty baseline files that are not part of
   your slice. Do not push unless the goal explicitly says to.
2. Mention the commit reference(s) in the handoff summary when they are relevant:
   `"<repo>@<branch>:<short_sha>"` (use `git rev-parse --short HEAD` and
   `git rev-parse --abbrev-ref HEAD`).
3. Run any safe in-session self-checks you need, but do not loop trying to mint a
   Harness proof ID yourself. Harness runs the final deploy-check after `hand_off`:
   - EterniaBackend: `manage.py check` (via the repo venv/test workflow)
   - EterniaLauncher: focused `flutter analyze <changed paths>` (or `flutter build`/`flutter test` when the stage demands it)
   - hermes-agent: focused `pytest` for the changed modules
4. Never invent proof IDs. If Proof Records already show a passed Harness proof ID,
   cite it in prose only when needed.

No-edit (investigation/proof-only/context) stages are exempt; do not invent commits
for stages that changed nothing.

## Hand Off

When `agent_hud` is present, prefer this product shape in the decision summary/rationale and let Harness project it internally:

```json
{
  "action": "deliver",
  "repo_bundle_id": "bundle_id_from_agent_hud_or_null",
  "summary": "Implemented the assignment and ran focused proof.",
  "changed_files": ["relative/path.ext"],
  "commands_run": ["focused command: passed"],
  "proof_refs": ["proof_or_self_test_id"],
  "remaining_risk": "none or exact caveat"
}
```

If blocked:

```json
{
  "action": "request_missing_input",
  "repo_bundle_id": "bundle_id_from_agent_hud_or_null",
  "missing_input_type": "backend_contract",
  "why_blocking": "The UI parser needs the authoritative payload shape.",
  "attempted_self_service": ["checked current assignment", "searched existing frontend usage"],
  "minimum_needed": "Field names and nullability for the post image payload."
}
```

Use the HUD recommended action first. The template above is explanatory guidance for deciding what substance belongs in the delivery.

## Request Context

Use only when the HUD recommended action or visible option is a context request and current attached context is insufficient. Ask for one or two narrow repo-relative paths with a reason. Do not use context requests as a substitute for patching when the files are already known.

For large files, request windows instead of repeating whole-file reads:

```json
{
  "type": "request_file_reads",
  "summary": "Request the relevant file window.",
  "rationale": "The whole file is large; a bounded line window is sufficient.",
  "payload": {
    "paths": ["relative/path.py#L120-L220"],
    "reason": "Need the implementation block around the failing behavior."
  }
}
```

## Request Proof Recipe

Use only when the HUD recommended action or visible option requests Harness-managed proof, the stage is no-edit/certification, QA asks for a missing proof lane, or a failed final gate needs bounded recovery.

## Stage Plan

Use only when no executable current stage exists or Harness repair feedback asks for stage correction. Keep stages bounded, repo-scoped, and proof-backed.

## Report Blocker

Use only after one bounded self-service attempt or when the HUD feedback proves the blocker is external/human/environmental. Include exact redaction-safe evidence.

## Handoff Payload

Use only the payload keys exposed by the HUD. For the current Dev handoff shape,
that normally means `stage_id`, `summary`, and `known_gaps`. Do not attach a
`delivery` object; Harness derives compatibility packets, diff state, and proof
state from the run trace.

## Normal Worker Flow Delivery Template

Use this shape for product-edit stages when the HUD primary action is `hand_off`:

```json
{
  "type": "hand_off",
  "summary": "Implemented the current stage and ran focused self-tests.",
  "rationale": "Changed the scoped files, ran the narrow self-test while context was hot, and delivery is ready for the Harness final gate.",
  "payload": {
    "stage_id": "stage_id",
    "summary": "Changed relative/path.dart and ran flutter test test/features/mission_control/mission_control_page_test.dart: passed.",
    "known_gaps": []
  }
}
```

If no self-test evidence ID is present yet, still include a concise `tests` entry naming the focused command and result or exact not-run reason. Do not paste raw logs.

## Proof Request Template

Use this only when the HUD exposes `request_gate`, for no-edit/certification stages, QA missing proof, or final-gate recovery:

```json
{
  "type": "request_test_run",
  "summary": "Request focused proof for the current stage.",
  "rationale": "The implementation needs deterministic proof before handoff.",
  "payload": {
    "stage_id": "stage_id",
    "commands": ["python -m pytest -q tests/path/test_file.py"],
    "delivery": {
      "source_handoff_packet_id": "packet_id_or_event_id",
      "consumed_contract_packet_ids": [],
      "consumed_proof_ids": [],
      "known_gaps": [],
      "next_owner": "neko_supervisor"
    }
  }
}
```

## Correct Stage Repair Template

Use this template when the current stage needs a proof-plan correction or the Harness rejects a later-stage proof request:

```json
{
  "type": "correct_stage",
  "summary": "Repair proof plan for the current stage.",
  "rationale": "The current stage needs the required proof before later stages can run.",
  "payload": {
    "stage_id": "stage_bridge_archive_regression",
    "corrections": ["Run the bridge/snapshot regression proof before visual proof."],
    "audit_notes": ["Previous page-widget proof does not satisfy bridge/archive acceptance."],
    "affected_paths": [],
    "test_plan": [
      "flutter test test/features/mission_control/mission_control_snapshot_test.dart test/features/mission_control/mission_control_bridge_test.dart"
    ]
  }
}
```

Block with a redaction-safe `log_ref` if proof cannot run because the environment or contract is blocked. This creates recoverable escalation evidence for Neko; it is not a terminal mission stop.
