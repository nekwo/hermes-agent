# Stage 29 — Neko Mission Lead Autonomous Handoff

## Goal

Simplify Mission Control by making `neko_supervisor` the mission lead for PM-style scoping, issue triage, proof/integration review, and handoff repair. PM remains as a backward-compatible persona/config key, but the default live Harness path should be:

```text
Tony goal → Neko Mission Lead scopes/routes → Dev implements/proves → QA verifies → Neko/Harness closes or steers
```

Manual targeted ticks remain an operator/debug escape hatch; normal daemon/tick eligibility should choose the next rightful persona without Tony/Alice hand-cranking every handoff.

## Product stance

- Neko is the mission owner / chief-of-staff.
- Dev and QA remain separate for independence.
- Neko may perform PM decisions but must not implement or self-verify Dev/QA proof.
- Handoff/contract weirdness routes to Neko rather than requiring a new Tony-created recovery goal.
- PM compatibility remains so old tasks, tests, configs, snapshots, and profile bindings do not break.

## Code audit evidence

- `agent_runtime/state_machine.py` currently routes `CREATED`/`PM_TRIAGE` and PM issue triage to `RUN_PM`.
- `agent_runtime/actions.py` already has `RUN_NEKO_SUPERVISOR`; no new action type is necessary.
- `agent_runtime/decision_schema.py` restricts `AgentRole.ALICE_SUPERVISOR` to block/context/triage/resolve only, so Neko cannot currently emit `propose_acceptance` or `approve` even though those are needed for Mission Lead duties.
- `agent_runtime/planning.py` already applies `propose_acceptance`, `approve`, and `triage_issue_discovery` generically by actor; it does not require actor `pm`.
- `agent_runtime/ticker.py` already maps `RUN_NEKO_SUPERVISOR` to `neko_supervisor` and applies decisions through the same state machine.
- Existing tests are PM-centric and should keep compatibility while adding Neko-first behavior.

## Architecture decisions

1. **Reuse `RUN_NEKO_SUPERVISOR`** instead of adding `RUN_MISSION_LEAD`; this avoids another action type and keeps Launcher compatibility.
2. **Route default PM duties to Neko** in `MissionStateMachine.next_action()`:
   - created / pm_triage → `run_neko_supervisor`
   - issue discovery triage before Dev → `run_neko_supervisor`
   - post-QA proof/integration review needing persona judgment → `run_neko_supervisor`
   - existing reconciliation/incidents remain Neko
3. **Expand Neko allowed decisions** to include mission-lead decisions without collapsing QA:
   - `propose_acceptance`
   - `triage_issue_discovery`
   - existing `resolve_incident`, `block`, `request_human`, `needs_context`
   - explicitly **not** `approve`, `propose_patch`, or `report_qa_verdict`
4. **Keep PM persona/config** as legacy/manual compatibility. Do not delete `pm` states or persona files in this stage.
5. **No Dev/QA collapse.** Neko must not gain Dev implementation decisions or QA verdict decisions.

## Rejected alternatives

- Delete PM persona entirely: rejected because existing persisted states/config/tests/Launcher mappings still reference PM.
- Add a new `mission_lead` persona ID: rejected because it adds migration complexity; `neko_supervisor` is already the supervisor role and user-approved label.
- Let Neko perform QA verdicts: rejected because it collapses independent verification.

## Implementation tasks

1. Update Stage 29 tests:
   - created tasks route to `run_neko_supervisor` and execute `neko_supervisor` for `propose_acceptance`.
   - severe issue-discovery triage before Dev routes to Neko, not PM.
   - Neko Mission Lead may emit `propose_acceptance`.
   - Neko still may not emit Dev `propose_patch`, QA `report_qa_verdict`, or broad `approve` self-verification.
   - PM compatibility remains: PM can still emit `propose_acceptance` when explicitly run/tested.
2. Patch `decision_schema.py` allowed decisions for `AgentRole.ALICE_SUPERVISOR`.
3. Patch `state_machine.py` next-action routing to prefer Neko Mission Lead for PM-style duties.
4. Patch display metadata (`personas.py`) to label Neko as `Neko Mission Lead` while keeping id `neko_supervisor`.
5. Verify targeted tests, full `tests/agent_runtime`, compileall, diff check, and no-model smoke.
6. Run independent deep audit review for role-boundary regressions and over-broad Neko authority.

## Acceptance criteria

- Fresh mission next action is `run_neko_supervisor`, not `run_pm`.
- Neko can scope a mission using the existing `propose_acceptance` contract.
- Issue triage and proof-review persona judgment route to Neko.
- Dev and QA boundaries remain intact.
- PM compatibility tests still pass.
- No Kanban dependency, no new daemon/service, no new store schema version.

## Rollback

Revert the small routing/decision-schema/display patches. Existing PM states and persona config remain untouched, so rollback is low risk.
