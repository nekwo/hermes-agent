# Stage 62 - Production Swarm Operations and Fleet Readiness

## Problem

Stage 61 makes smooth unattended execution and first swarm readiness possible: loop guards,
redaction repair, context windows, recovery re-arm, daemon leases, certification, and a gated
N-lane scheduler. That is the right foundation, but it is not yet enough for Tony to run a real
agent fleet with confidence.

A swarm multiplies both throughput and ambiguity. Without first-class lane lifecycle, write-scope
isolation, global budget governance, proof attribution, operator controls, and a multi-lane burn-in,
Mission Control can show "agents running" while the actual operating truth is hidden in scattered
task files, incidents, proofs, daemon state, and persona sessions.

Stage 62 turns Stage 61's swarm primitives into a production-grade operating layer:

```text
certified Stage 61 smoothness -> explicit lanes -> safe repo locks -> global budget governor ->
operator controls -> proof/incident attribution -> fleet burn-in -> Mission Control readiness
```

## Product Stance

Stage 62 is **swarm operations, not swarm invention**.

Do not introduce a parallel runtime model. Build on the Stage 61 surfaces:

- `GoalRuntimeInstanceStore` / runtime instance concepts.
- foreground/background lane accounting.
- persona instance assignment.
- daemon leases.
- repo bundle/write-scope isolation.
- burn-in certification.
- `status --json` observability.
- CLI Harness controls.

The target is simple for Tony:

> I can start multiple goals, see exactly which agent lane is doing what, know why any lane is
> parked/blocked, trust that two agents will not trample the same repo, and recover/drain/archive the
> fleet without manual file surgery.

## Current Implementation Audit - 2026-06-10

Grounded against the local Hermes checkout after Stage 61 planning/implementation work was present
in the working tree.

### Existing Stage 61 anchors

- Stage 61 source-of-truth doc exists at
  `docs/agent-runtime-harness/61-smooth-unattended-execution-and-swarm-readiness.md`.
- Stage index currently lists Stage 61 as the newest harness stage.
- Stage 61 defines the required prerequisite stance: smooth unattended execution before swarms.
- Stage 61 done criteria include:
  - 10/10 unattended burn-in gate.
  - two concurrent investigation goals in parallel lanes.
  - no shared write bundle.
  - truthful per-lane status.

### Working-tree evidence

The active working tree already touches the intended Stage 62 seams:

- `agent_runtime/autonomy.py`
- `agent_runtime/budget_approval.py`
- `agent_runtime/burn_in.py`
- `agent_runtime/config.py`
- `agent_runtime/context_builder.py`
- `agent_runtime/context_requests.py`
- `agent_runtime/daemon.py`
- `agent_runtime/dirty_state.py`
- `agent_runtime/goal_hygiene.py`
- `agent_runtime/models.py`
- `agent_runtime/observability.py`
- `agent_runtime/packets.py`
- `agent_runtime/persona_assignments.py`
- `agent_runtime/persona_runtime.py`
- `agent_runtime/preflight.py`
- `agent_runtime/repo_bundles.py`
- `agent_runtime/runtime_config.py`
- `agent_runtime/runtime_instances.py`
- `agent_runtime/snapshot.py`
- `agent_runtime/status.py`
- `agent_runtime/store.py`
- `agent_runtime/ticker.py`
- `hermes_cli/harness.py`
- `tests/agent_runtime/*`

This means Stage 62 should avoid broad rewrites and instead harden the lanes/budget/status/proof
interfaces already emerging from Stage 60-61.

### Verified API anchors - commit `fdc8e093b` (2026-06-10)

Bind to these existing surfaces; do not invent parallel ones.

`agent_runtime/runtime_instances.py::GoalRuntimeInstanceStore` (62B builds on this):

```text
create_foreground(task_id, started_by) -> GoalRuntimeInstance
park_open_task(task_id, reason) / park_foreground_except(task_id, reason)
mark_terminal_for_task(task_id, reason) -> list[str]
get / save(instance, event_type, reason) / list_all / list_for_task
latest_for_task / active_for_task / active_foreground
runtime_instance_summary(instance) / runtime_instances_summary(instances)
```

Current instance model knows `lane` ("foreground"/"background") and `state`
("active"/"parked"/...). 62B extends this store with the full lane state set,
`lane_kind` (production/playground), and the lane fields list - it does not replace it.

`agent_runtime/repo_bundles.py::RepoBundleStore` (62C builds on this):

```text
create_or_update_from_task(task) -> list[RepoBundle]
get / update / list_for_task / list_all
find_for_assignment / attach_assignment
mark_running / mark_delivered / mark_verified / mark_rejected
wake_ready_dependencies(task_id) / cancel_superseded(task, desired_ids)
desired_bundles_for_task(task) / merge_desired_bundle(existing, desired)
```

Bundles are currently per-task work units, not cross-lane locks. 62C adds the
cross-lane lock layer (read/write/exclusive_maintenance acquisition at lane
activation) on top of these bundles - the lock registry is new, the bundle shape
is not.

`agent_runtime/daemon.py` lease primitives (62B lane leases reuse the pattern):
`_acquire_daemon_lease` / `_refresh_daemon_lease` / `_clear_daemon_lease`,
`DAEMON_LEASE_TTL_SECONDS = 15`.

Baseline gaps from commit `2a2c086f9` (now implemented for 62A-62F after that baseline):

- `runtime_config.swarm` now exists with certification and budget fields.
- Certification/unattended-gate machinery now exists in `burn_in.py`.
- `harness swarm` and `harness lane` CLI subcommands now exist.
- Proof/incident metadata can now carry lane attribution.
- `GoalRuntimeInstance` now carries `lane_kind`, including the playground exemption path.

Already shipped from this stage (62I-1, do not re-implement):

- `agent_runtime/replay_scenarios.py` (registry, dedupe, `replay_scenario`/`replay_all`).
- Auto-capture at `ticker.py::_capture_replay_scenario` on every live
  `DecisionPayloadInvalid` in the repair path.
- CLI `harness playground list | show <id> | replay [<id>] [--json]`
  (`hermes_cli/harness.py::_cmd_playground_*`).
- Tests: `tests/agent_runtime/test_replay_scenarios.py` (7 cases).

## Non-Negotiable Architecture Decisions

1. **Stage 61 certification gates Stage 62 swarm operation.**
   - Swarm enablement must refuse while Stage 61's unattended gate is red.
   - Operators may run targeted local swarm tests, but production/autonomous swarm mode stays gated.

2. **The scheduler owns lane truth, not the model.**
   - Personas can request, report, or block.
   - They cannot decide repo lock ownership, budget park/resume, or lane lifecycle state.

3. **Lanes are first-class persisted runtime objects.**
   - A lane is not just a task status string.
   - It has identity, lease, persona assignments, budget counters, repo locks, current stage,
     proof links, and incident links.

4. **Parking is not failure.**
   - `parked_by_budget`, `parked_by_repo_lock`, and `parked_by_operator` are settled safe states.
   - Parked lanes must not burn model calls trying to explain that they are parked.

5. **All proof and incidents are lane-attributed.**
   - In swarm mode, "Dev passed proof" is insufficient.
   - The proof must identify lane, task, stage, persona instance, repo bundle, command/artifact, and
     reuse basis when applicable.

6. **Mission Control consumes a compact fleet snapshot.**
   - Mission Control should not reconstruct swarm truth by crawling raw tasks/proofs/incidents.
   - Hermes should expose a stable operator/fleet summary in CLI/status/snapshot surfaces first.

## Stage 62A - Certification Baseline and Red-Gate Enforcement

Scope: `agent_runtime/burn_in.py`, `agent_runtime/runtime_config.py`, `agent_runtime/status.py`,
`hermes_cli/harness.py`, relevant burn-in/status tests.

Before enabling production swarm behavior, verify and expose Stage 61's certification gate.

Implementation tasks:

- Add/confirm a persisted certification ledger summary that records:
  - last gate verdict;
  - consecutive green unattended cases;
  - last failure class;
  - manual intervention counts;
  - certification timestamp;
  - code/config fingerprint used for the verdict where practical.
- Ensure `runtime_config.swarm.requires_certification = true` blocks production swarm enablement
  unless the ledger is green.
- Add an explicit override mode for local development only, named as unsafe, e.g.
  `--allow-uncertified-dev-swarm`, never default.
- **Playground exemption (playground, not cage):** `lane_kind="playground"` lanes and
  scenario replay are exempt from the certification gate. The playground's primary use
  is getting certification green — replaying failures and tuning skills while the gate
  is red. Playground lanes are read-locked by default (that is their safety property),
  excluded from burn-in success counts, and never require `swarm enable`.
- Surface certification in `harness status --json` / fleet status:
  - `swarm.certification.required`
  - `swarm.certification.state`
  - `swarm.certification.consecutive_green`
  - `swarm.certification.last_failure_class`

Tests:

- Red certification refuses `swarm enable` without the unsafe dev override.
- Green certification allows `swarm enable`.
- Status reports the same certification state used by the enable gate.
- Manual intervention event resets/invalidates the green streak.

Acceptance:

- No production swarm can start from an uncertified Stage 61 base by accident.

## Stage 62B - First-Class Lane Lifecycle Controller

Scope: `agent_runtime/runtime_instances.py`, `agent_runtime/persona_assignments.py`,
`agent_runtime/ticker.py`, `agent_runtime/autonomy.py`, `agent_runtime/store.py`,
`agent_runtime/status.py`.

Create a persisted lane lifecycle that the scheduler, CLI, and Mission Control can trust.

Lane states:

```text
queued
activating
running
parked_by_budget
parked_by_repo_lock
parked_by_operator
blocked
done
archiving
archived
failed_runtime
```

Lane fields:

- `lane_id`
- `task_id`
- `goal_id` / runtime instance id where applicable
- `priority`
- `state`
- `state_reason`
- `current_stage_id`
- `current_owner`
- `persona_instance_ids`
- `repo_bundle_locks`
- `daemon_lease_id`
- `budget_counters`
- `last_decision_type`
- `last_progress_at`
- `open_incident_ids`
- `latest_proof_ids`

Implementation tasks:

- Persist lane records atomically alongside the runtime instance store.
- Make lane state transitions explicit helper functions, not ad hoc task mutations.
- Add transition validation:
  - `queued -> activating -> running`
  - `running -> parked_* -> running`
  - `running -> blocked/done/failed_runtime`
  - `done -> archiving -> archived`
- Ensure parked lanes do not tick persona runtimes.
- Ensure blocked lanes tick only through bounded recovery paths already defined by Stage 61.

Tests:

- Lane activation persists all required fields.
- Parked lane is skipped by the ticker without launching model calls.
- Resume from budget/repo/operator park returns to `running` only when the reason has cleared.
- Invalid transition is rejected and records a runtime incident.

Acceptance:

- `status --json` can explain every active/queued/parked lane without inspecting task internals.

## Stage 62C - Repo Bundle Write-Scope Isolation

Scope: `agent_runtime/repo_bundles.py`, `agent_runtime/dirty_state.py`,
`agent_runtime/preflight.py`, `agent_runtime/goal_hygiene.py`, `agent_runtime/runtime_instances.py`.

Stage 61 requires write isolation. Stage 62 makes it production-safe.

Implementation tasks:

- Define canonical repo bundles for known work areas:
  - Hermes / Agent Runtime Harness.
  - Launcher.
  - Backend.
  - TonyBrain / Arcadia brain surfaces where applicable.
- Determine requested lock mode from mission plan / stage intent:
  - `read`
  - `write`
  - `exclusive_maintenance`
- Acquire all required write locks atomically before lane activation.
- Allow read-only lanes to share bundles unless an exclusive maintenance lock is held.
- Park lanes that cannot acquire locks:
  - state: `parked_by_repo_lock`
  - reason includes owning lane/task, bundle id, and lock mode.
- Release locks only when lane reaches terminal state or operator drain completes.
- Preserve dirty baseline per lane so unrelated user edits are not blamed on the swarm.

Tests:

- Two write lanes for the same repo: one runs, one parks.
- Two read lanes for the same repo: both run.
- Write lane plus read lane policy is explicit and tested; default should be conservative when the
  read lane's proof commands could mutate generated artifacts.
- Dead/archived lane releases locks.
- Dirty baseline is lane-local and shown in lane status.

Acceptance:

- Two agents cannot silently edit the same repo bundle concurrently.

## Stage 62D - Global Budget Governor

Scope: `agent_runtime/budget_approval.py`, `agent_runtime/runtime_config.py`,
`agent_runtime/persona_runtime.py`, `agent_runtime/ticker.py`, `agent_runtime/status.py`.

Swarm mode needs both per-lane and global ceilings.

Implementation tasks:

- Add/confirm config fields:
  - `swarm.max_active_lanes`
  - `swarm.global_token_soft_limit`
  - `swarm.global_token_hard_limit`
  - `swarm.global_api_call_soft_limit`
  - `swarm.global_api_call_hard_limit`
  - `swarm.per_lane_token_limit`
  - `swarm.per_lane_api_call_limit`
- Track per-lane and global usage from run metadata.
- At soft limits:
  - park lower-priority lanes with `parked_by_budget`;
  - keep active lane count under the remaining budget;
  - do not kill in-flight safe finalization unless hard limit is reached.
- At hard limits:
  - stop scheduling model calls;
  - open a budget incident;
  - require operator approval or deterministic scope reduction.
- Expose budget state in fleet status.

Tests:

- Per-lane budget exceeded parks only that lane.
- Global soft limit parks lower-priority lanes.
- Global hard limit prevents new model calls and opens one incident, not incident spam.
- Approved continuation resumes the same lane when safe.

Acceptance:

- Swarm cannot burn runaway tokens/API calls across multiple lanes without a visible governor.

## Stage 62E - Operator CLI Controls

Scope: `hermes_cli/harness.py`, `agent_runtime/status.py`, lane store helpers.

Add CLI controls before relying on Launcher/Mission Control UI.

Target commands:

```text
hermes harness swarm status --json
hermes harness swarm enable --lanes 2
hermes harness swarm disable
hermes harness swarm drain
hermes harness lane list --json
hermes harness lane show <lane_id> --json
hermes harness lane pause <lane_id> --reason <reason>
hermes harness lane resume <lane_id>
hermes harness lane park <lane_id> --reason <reason>
hermes harness lane drain <lane_id>
```

Implementation tasks:

- Route all commands through lane lifecycle helpers.
- `pause`/`park` must not erase proof, incident, or dirty baseline evidence.
- `drain` means no new model calls; finish safe deterministic transitions and archive when done.
- `disable` should prevent new lane activation but should not kill active lanes unless combined with
  an explicit drain/stop command.
- JSON output must be stable enough for Mission Control consumption.

Tests:

- Commands mutate lane state correctly.
- Disabled swarm refuses new lane activation.
- Drain preserves evidence and reaches settled state.
- Bad lane id returns a clear operator-safe error.

Acceptance:

- Tony can operate the fleet without manual edits under `.hermes/agent-runtime`.

## Stage 62F - Swarm Proof and Incident Ledger

Scope: `agent_runtime/observability.py`, `agent_runtime/models.py`, `agent_runtime/store.py`,
`agent_runtime/snapshot.py`, `agent_runtime/status.py`.

Make proof and incidents attributable in multi-lane operation.

Implementation tasks:

- Extend proof metadata with:
  - `lane_id`
  - `task_id`
  - `stage_id`
  - `persona_instance_id`
  - `repo_bundle_ids`
  - `command_or_artifact_ref`
  - `proof_reuse_basis` when reused.
- Extend incidents with lane attribution:
  - `lane_id`
  - `lane_state_at_open`
  - `budget_state`
  - `repo_lock_state`
- Add fleet snapshot aggregation:
  - lanes by state;
  - open incidents by lane;
  - latest proofs by lane;
  - stuck/no-progress counters by lane;
  - budget usage by lane.
- Preserve lane attribution in archive summaries.

Tests:

- Proof created in lane includes lane/persona/stage/task metadata.
- Incident opened by a lane appears in both task history and fleet status.
- Archive summary retains lane proof/incident attribution.
- Proof reuse across handoff preserves original lane plus reuse lane where applicable.

Acceptance:

- In swarm mode, every claim can be traced to the lane/persona/stage that produced it.

## Stage 62G - Production Swarm Burn-In

Scope: `agent_runtime/burn_in.py`, `agent_runtime/runtime_config.py`, tests and fixtures.

Extend Stage 61's unattended certification into a multi-lane fleet gate.

Minimum gate cases:

1. Two concurrent read-only investigation goals complete.
2. One write lane and one read-only lane complete without repo-lock conflict.
3. Two write lanes targeting the same repo bundle: one runs, one parks, then resumes after release.
4. Provider failure in one lane opens/retries/settles without poisoning the other lane.
5. Global soft budget parks lower-priority lane and later resumes.
6. Operator pause/resume preserves proof and incident evidence.
7. Archive preserves per-lane evidence.

Gate target:

- 10 consecutive green fleet cases.
- zero manual task file edits.
- zero process kills.
- zero duplicate daemon/lane leases.
- zero unexplained active lanes at completion.

Tests:

- Burn-in fixtures cover every gate case.
- `harness burn-in summarize` includes a fleet readiness verdict distinct from Stage 61 smoothness.
- Swarm production readiness requires both Stage 61 smoothness and Stage 62 fleet burn-in.

Acceptance:

- The fleet is certified for real unattended multi-goal operation, not just unit-tested lane logic.

## Stage 62H - Persona Skill and Mission Control Contract Sync

Scope:

- `docs/agent-runtime-harness/stage46-skills/harness-mission-lead/SKILL.md`
- `docs/agent-runtime-harness/stage46-skills/harness-dev-delivery/SKILL.md`
- `docs/agent-runtime-harness/stage46-skills/harness-qa-verdict/SKILL.md`
- Mission Control/Launcher contract docs once the CLI JSON is stable.

Implementation tasks:

- Update Neko/Mission Lead contract:
  - understand lane states;
  - treat parked as settled-safe, not failure;
  - escalate only when scheduler says intervention is needed.
- Update Dev contract:
  - declare requested repo bundles and write/read intent;
  - never assume it owns a repo because it can see files;
  - include lane id in delivery packets/proofs when visible.
- Update QA contract:
  - verify proof attribution;
  - require lane-safe dirty baseline explanation for write lanes;
  - distinguish product failure from lane/scheduler/budget failure.
- Define Mission Control consumption shape:
  - fleet summary card;
  - lane rows;
  - budget panel;
  - repo lock panel;
  - proof/incident drilldown.

Tests/proof:

- Skill docs updated and synced to relevant Harness profiles using the established skill sync path.
- Contract tests assert lane/budget/repo-lock fields exist in JSON consumed by UI.

Acceptance:

- Personas and Mission Control speak the same swarm language as the scheduler.

## Stage 62I - Mission Control Skill Playground and Feedback Loop

Scope:

- Mission Control UI/contract surfaces once the CLI JSON is stable.
- `agent_runtime/context_builder.py` and decision/packet registry metadata that explain available
  actions.
- stage46 persona skills and profile skill sync.
- future skill authoring/validation commands where needed.

Product goal:

Mission Control should become the operator-safe playground for skills, prompts, and persona contracts:
Tony can see what skill/contract an agent is using, run controlled playground missions, inspect why the
agent chose an action, tune the skill/contract, and graduate the improved behavior into production
swarm lanes only after proof.

This is not a toy chat sandbox. It is a controlled Harness training/proving surface for real skills.

62I splits into three independently shippable slices:

- **62I-1 Replay scenario registry + CLI (no dependencies — shipped first, before 62A-62H).**
  Scenario replay is a pure function: preserved raw payload -> current contract ->
  verdict. It needs no scheduler, no daemon, no lane records. Status 2026-06-10:
  implemented and tested in the working tree:
  - `agent_runtime/replay_scenarios.py`: scenario store under
    `<runtime root>/replay_scenarios/`, dedupe by payload+error digest,
    `replay_scenario` / `replay_all` validate against the live
    `validate_payload_keys` + `validate_decision_packets` path with verdicts
    `passes_current_contract` / `still_failing` (with `error_changed`).
  - **Auto-capture ratchet:** every live `DecisionPayloadInvalid` in the ticker repair
    path auto-records a replay scenario candidate
    (`ticker.py::_capture_replay_scenario`) — pointer-sized, deduped, capped, never
    able to fail the repair path. The corpus grows for free as the swarm runs; the
    operator curates, never transcribes.
  - CLI: `harness playground list | show <id> | replay [<id>] [--json]`.
  - Tests: `tests/agent_runtime/test_replay_scenarios.py`, including a historical
    qa_review unknown-key payload that now replays green against current contracts.
- **62I-2 Dry-run playground lanes** (needs 62B `lane_kind` + 62F attribution).
- **62I-3 Mission Control playground UI** (needs stable status/contract JSON; the UI
  renders JSON that must exist anyway — CLI-first like every other stage).

Skill identity anchor: `skill_versions` should reuse the existing skill hash machinery
(`skill_hash_mismatches` in `harness agents`/status readiness) rather than inventing a
new version scheme.

Required playground modes:

1. **Skill inspection mode**
   - Show which skill(s), prompt contract, decision shapes, and HUD actions are active for a lane or
     persona instance.
   - Show skill version/source path/profile target without exposing secrets or raw host paths beyond
     approved repo-relative/operator-safe summaries.
   - Show the exact contract fields the agent is expected to emit.

2. **Scenario replay mode**
   - Replay historical incidents, failed packets, stuck loops, blocked tasks, and proof handoffs against
     the current skill/contract without mutating production tasks.
   - Include Stage 61 fixtures: no-progress loops, redaction false positives, context starvation,
     recovery dead-end, daemon lifecycle conflict, and handoff repair visibility drift.

3. **Dry-run mission mode**
   - Create isolated playground lanes that use the same scheduler/skills/contracts but cannot write to
     production repos unless explicitly promoted.
   - Dry-run lanes can request context, produce packets, and run non-mutating proof checks.
   - Any attempted write/proof command with mutation risk must be blocked or require explicit operator
     promotion.

4. **Skill critique and patch proposal mode**
   - After a failed/rejected decision, Mission Control can show:
     - skill section involved;
     - violated contract field;
     - repair feedback;
     - suggested skill wording change;
     - regression scenario to add before promotion.
   - The system may draft a patch, but applying it remains an explicit operator action or a gated
     Harness task with proof.

5. **Promotion mode**
   - Playground improvements graduate only after:
     - replay scenario passes;
     - targeted contract tests pass;
     - relevant persona skill sync completes;
     - a small live lane proves the behavior;
     - rollback path is recorded.

Implementation tasks:

- Add a stable Mission Control skill-playground contract in the Stage 62 JSON/fleet snapshot:
  - active skill ids and versions by lane/persona;
  - decision shape ids and validation failures;
  - replay scenario id/status;
  - playground lane id/state;
  - proposed skill patch metadata;
  - promotion readiness verdict.
- Extend lane status with a redaction-safe `skill_context` block:
  - `profile`
  - `persona_role`
  - `skill_names`
  - `skill_versions` where available
  - `contract_shape_ids`
  - `last_contract_failure_class`
- Add non-mutating playground lane type:
  - `lane_kind="playground"`
  - cannot acquire write locks by default;
  - excluded from production burn-in success counts unless promoted;
  - included in observability and proof attribution.
- Add replay scenario registry for known Harness failure classes.
- Teach Mission Lead/Dev/QA skills to interpret playground state:
  - playground failures are training/proving signals, not product task failures;
  - no agent may claim a skill fix is production-ready without replay + test + live-lane proof.

Tests:

- Playground lane cannot mutate production repo by default.
- Historical scenario replay does not alter production task state.
- Skill context appears in lane status without leaking secrets/raw provider internals.
- Contract failure links to the active skill/shape metadata.
- Promotion requires replay pass + targeted tests + sync proof.

Acceptance:

- Mission Control can be used as Tony's playground for improving Harness skills/contracts safely.
- Skill changes have an evidence loop: observe -> replay -> patch proposal -> test -> sync -> live proof
  -> promote.
- Production lanes never silently consume unproven playground skill changes.

## Implementation Order

1. 62I-1 replay scenario registry + auto-capture + CLI (no dependencies; shipped —
   it is the regression harness for everything that follows).
2. 62A certification baseline/gate (with the playground exemption).
3. 62B lane lifecycle controller.
4. 62C repo bundle lock hardening.
5. 62D global budget governor.
6. 62E CLI operator controls.
7. 62F proof/incident attribution.
8. 62G production swarm burn-in.
9. 62H skill/Mission Control contract sync.
10. 62I-2/62I-3 playground lanes and Mission Control playground UI.

62B and 62C may proceed in parallel if their shared lane record shape is frozen first. 62E should wait
until 62B's helpers exist so CLI commands do not mutate files ad hoc. 62H should wait until status JSON
is stable enough to avoid teaching agents a shape that will immediately churn. 62I depends on the same
stable status/contract metadata, but its replay/dry-run lane pieces can begin once lane_kind and proof
attribution are available.

## Verification Matrix

- Targeted unit tests:
  - lane lifecycle transitions;
  - repo lock acquire/park/release;
  - budget park/resume/hard-stop;
  - CLI command behavior;
  - proof/incident metadata.
- Integration tests:
  - two-lane read-only swarm;
  - write-lock contention swarm;
  - provider-failure isolation;
  - budget parking/resume.
- Burn-in:
  - Stage 61 smoothness green;
  - Stage 62 fleet readiness green.
- Operator proof:
  - `hermes harness swarm status --json` shows certification, lanes, budget, locks, incidents, and proofs.
  - `hermes harness lane show <lane_id> --json` explains exactly why the lane is running/parked/blocked.
- Skill playground proof:
  - Mission Control/JSON shows redaction-safe skill context for a lane.
  - A historical Stage 61 failure scenario replays without mutating production state.
  - A playground lane cannot acquire production write locks by default.
  - A proposed skill patch cannot be promoted without replay, tests, sync, and live-lane proof.

## AAA Gap Checklist

Before Stage 62 can be called complete:

- [x] Stage 61 smoothness gate is green or Stage 62 refuses production swarm enablement.
- [x] Lane lifecycle is persisted, explicit, and tested.
- [x] Repo write isolation prevents concurrent conflicting edits.
- [x] Budget governor prevents multi-lane runaway spend.
- [x] CLI controls operate without manual runtime file edits.
- [x] Proofs and incidents are lane-attributed.
- [ ] Fleet burn-in is green across concurrent, contended, failed-provider, budget, and archive cases.
- [ ] Persona skills and Mission Control contract docs are updated.
- [ ] Mission Control skill playground supports skill inspection, historical replay, dry-run lanes,
      patch proposals, and gated promotion.
- [ ] Playground lanes cannot mutate production repos by default and cannot silently feed unproven
      skill changes into production lanes.
- [ ] No raw secrets/paths/provider internals are exposed in fleet status.
- [ ] Archive preserves enough evidence for later QA/review without keeping live lanes open.

## TODO - Live Findings From the 2026-06-11 Certification and Continuation Runs

Found during the first green certification ladder (10/10) and the task_3cb6dd82
continuation goal. Ordered by priority; none blocks single-lane operation.

- [ ] **Swarm enablement does not persist across new-goal hygiene.** `harness swarm
      enable --lanes 2` succeeded with certification green, but after the next
      `task create` hygiene cycle `status --json` reported `swarm.enabled: false`
      (certification stayed green). Fleet mode needs one explicit state owner that
      goal hygiene cannot reset; add a regression test: enable swarm -> create a
      task -> swarm must still be enabled.
- [ ] **QA token asymmetry: delta review.** QA re-reads the full evidence corpus on
      every verdict (25k tokens on the continuation goal, 343-430k on large goals).
      Acceptable single-lane; multiplies linearly with lanes in swarm mode. Give QA
      a delta-review path: review only packets/proofs recorded since its last
      verdict for the same stage, with the prior verdict summary carried forward.
- [ ] **Finish the 61F deletion pass before swarm scale.** The typed
      `handoff_request` field is live with agreement logging, but the prose-matching
      heuristics (`_payload_is_launcher_handoff`, `_summary_is_missing_launcher_proof`)
      and the state-machine branches the 61A no-progress guard made redundant are
      still standing. Delete after one release of logged agreement; shrinking the
      special-case ladder matters more once multiple lanes execute it concurrently.
- [ ] **Failure-origin classification: harness repair vs context starvation vs
      context overload.** Today every invalid decision funnels into one bucket
      (model_invalid_output -> repair retry -> replay scenario), which hides whose
      fault it was. Add a `failure_origin` classifier at the repair/incident/replay
      capture sites with three verdicts:
      - `contract_violation`: the payload references entities that resolve nowhere
        in harness stores (invented field/shape with the needed data in view).
        Correct response: repair feedback; recurring -> skill/contract fix.
      - `context_starvation`: the payload references entities that ARE real
        (proof/stage/packet ids resolve in stores, or fields exist in the packet
        registry) but were not projected into the persona's HUD/context. Correct
        response: harness supplies the missing projection; do not blame the persona.
        (task_7010f6c0's QA-visibility history was this class.)
      - `context_overload`: failure correlates with high `context_size_estimate`
        or truncated compression receipts on the same run (mis-copied ids,
        repeated decisions, lost thread). Correct response: projection/compression
        tuning, not repair text.
      Signals already recorded per run: `context_size_estimate`, compression
      receipts, context-request fulfillment history, and store-resolvability of
      referenced ids. Wire `failure_origin` into: replay scenario records,
      incident metadata for model_invalid_output, and origin counts in
      status/fleet snapshot so the operator can see which lever to pull
      (skills vs projection vs compression).
      DONE (initial): `replay_scenarios.classify_failure_origin` +
      `estimate_context_relevance` classify by **grounding coverage, not size** -
      an entity the decision needs that resolves in the store but was not in the
      projected set (task.proof_ids / plan stage ids+proofs) is starvation even
      in a huge context; overload requires full grounding plus a large prompt
      (dilution). Stored on replay scenarios, shown in `playground list`.
      REMAINING: incident metadata wiring, fleet-status origin counts, and a
      richer projected-set source than task.proof_ids (read the actual context
      receipt's projected id set, and add a relevance-density measure:
      relevant-projected-bytes / total-projected-bytes, so a context that is
      large AND mostly irrelevant is flagged distinctly from a tight one).
- [ ] **Complete the certification case mix.** The 10/10 streak used
      backend-only-edit and noop-orchestration shapes. The 61G composition still
      owes: launcher-only-edit, mission-control-visual (needs a warm Flutter/
      Stage C environment), an injected provider-failure recovery case, and an
      injected near-valid-packet repair case. Add them to
      `scripts/certification_ladder.py` rotation once the Launcher build
      environment is available; fleet burn-in (62G) should not be called green
      without them.

## Done Criteria

Stage 62 is done when:

- `harness swarm enable --lanes 2` refuses while certification is red and succeeds when certification is green.
- Two concurrent read-only goals complete with distinct lane/persona/proof attribution.
- Two same-repo write goals do not run concurrently; one parks with a clear repo-lock reason and resumes after release.
- A global budget soft limit parks a lower-priority lane without killing the active lane.
- A global hard limit opens one budget incident and prevents further model calls until approval/recovery.
- `harness swarm status --json` gives a complete compact fleet snapshot.
- `harness lane pause/resume/drain/show` work and preserve evidence.
- Fleet burn-in reports 10/10 green.
- Mission Control has a stable JSON contract to render fleet/lane/budget/repo-lock/proof state.
- Mission Control can run a non-mutating skill playground lane, replay at least one historical Stage 61
  failure scenario, show the active skill/contract context, and gate promotion of a skill patch behind
  replay + tests + sync + live-lane proof.
