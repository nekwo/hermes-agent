# Stage 40 — Cross-Stack Contract Smoke and Neko Release Gap

## Goal

Verify, after Stage 39 runtime/provider health closure, that the Harness can coordinate a real bounded frontend/backend contract smoke involving:

- `backend_dev` against EterniaBackend;
- `dev` against EterniaLauncher;
- `neko_supervisor` for specialist handoff and QA release;
- `qa` for proof-backed implementation verdict.

This stage intentionally avoids product code changes. It verifies cross-stack proof semantics and exposes coordination gaps.

## Live Task

- Task ID: `task_cross_stack_contract_smoke_20260604`
- Title: `Cross-stack live smoke: Backend contract + Launcher frontend config + Neko/QA proof gate`
- Final state: `done`
- Final stop reason: `task_terminal`
- Final open incidents: `0`
- Final acceptance proof IDs: `3`

## Contract Proven

Backend proof selected the public health contract:

- Contract: `GET /health/`
- Host family: `api.eternia.co`
- Expected payload: `{ "status": "ok" }`

Frontend proof verified Launcher configuration/API client alignment:

- API client: `lib/core/services/api_client.dart`
- Environment key: `DJANGO_API_URL`
- Production URL: `https://api.eternia.co/api`
- Staging URL: `https://api-staging.eternia.co/api`
- Consumer family: `ApiClient.get/post/patch/put/delete` compose `_baseUrl` plus normalized path.

## Proof Packet

Acceptance proofs retained on the task:

1. `test_task_cross_stack_contract_smoke_20260604_stage_backend_contract_probe_run_651c4b4b4af8_0_480914c5`
   - `actor_requested`: `backend_dev`
   - `status`: `passed`
   - `exit_code`: `0`
   - `duration_ms`: `3425`
2. `test_task_cross_stack_contract_smoke_20260604_stage_frontend_contract_probe_run_e4480624d110_0_057f4602`
   - `actor_requested`: `dev`
   - `status`: `passed`
   - `exit_code`: `0`
   - `duration_ms`: `297`
3. `proof_qa_51654825`
   - `verdict`: `approved`
   - `findings`: `[]`

QA summary: approved the cross-stack contract smoke because both backend and frontend deterministic proofs passed and matched the Django API / `api.eternia.co` contract family.

## Gap Found — Neko Premature Block on Sequential Specialist Handoff

### Evidence

The first run of:

```bash
python -m hermes_cli.main harness run-until-settled --task task_cross_stack_contract_smoke_20260604 --max-actions 10 --max-seconds 1200 --json
```

stopped after two ticks:

- Final task state: `blocked`
- Open incidents: `0`
- Backend proof was already attached and passed.
- Neko decision: `needs_context`
- Neko summary: `QA release is premature: backend proof is SQLite/manage.py-shell only and frontend proof is not yet attached.`

This was partially correct — QA release was premature — but the correct Harness behavior for this task was to release the next specialist (`dev`) onto `stage_frontend_contract_probe`, not block the mission.

### Severity

Medium.

The gap does not corrupt proof or close the task incorrectly, but it can require Alice/operator intervention during multi-specialist live missions. That is below AAA autonomy for Mission Control.

### Current Recovery

Alice performed a narrow operator recovery:

- Preserved backend proof.
- Set state back to `dev_implementing`.
- Set `current_stage_id` to `stage_frontend_contract_probe`.
- Set affected repo to `X:/Unreal Engine/Engine/Launcher/EterniaLauncher`.
- Added risk flag: `operator_recovered_neko_premature_qa_block`.

The resumed run completed successfully:

```bash
python -m hermes_cli.main harness run-until-settled --task task_cross_stack_contract_smoke_20260604 --max-actions 10 --max-seconds 1200 --json
```

Result:

- Final task state: `done`
- Stop reason: `task_terminal`
- Open incidents: `0`
- Ticks: `4`
- Proof IDs: `3`

## Required Upgrade

Stage 40 follow-up should make this handoff deterministic or strongly guided:

1. When a stage has passed command proof and the task has additional ready stages, route to the next dev stage instead of asking Neko for a QA release unless all stages are dev-complete.
2. If `sequential_specialists_required` is present, preserve Neko visibility but constrain the expected decision to specialist release, not `needs_context`, when the next stage is already defined and ready.
3. Add a regression test for multi-stage cross-stack smoke:
   - backend proof passes;
   - frontend stage remains ready;
   - Harness/Neko transition routes to frontend dev;
   - QA is not reached until frontend proof exists;
   - no blocked state is produced without an actionable context request or incident.
4. Add an observability assertion that a blocked task produced by `needs_context` includes a non-empty `context_requests` entry or open intervention; otherwise it should be treated as a routing bug.

## Final Stage 40 Status

- Cross-stack smoke: passed after operator recovery.
- Proof hygiene: passed; acceptance proof packet contains only passed backend proof, passed frontend proof, and QA approval.
- Runtime status after completion: healthy.
- Remaining intervention: deterministic backend→frontend specialist release should be implemented before claiming full AAA autonomy for multi-specialist cross-stack missions.
