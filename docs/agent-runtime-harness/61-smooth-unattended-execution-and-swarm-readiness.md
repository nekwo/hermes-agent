# Stage 61 - Smooth Unattended Execution and Swarm Readiness

## Problem

The Harness can complete a real goal one-shot (task_af5c257c, 2026-06-10: Neko scope ->
Backend Dev investigation -> QA reject -> Dev re-delivery -> QA approve -> done ->
auto-archive, 5 runs, ~200k tokens, zero manual ticks). But runtime history shows it
still gets stuck in repeatable, classifiable ways, and several stuck classes end in
operator babysitting: manual daemon kills, manual incident closes, abandoned goals.

Swarms (multiple concurrent goals, multiple persona instances) multiply every failure
class. A goal lane that dead-ends once a day becomes a swarm that dead-ends hourly.
Stage 61 closes the remaining stuck classes, proves smoothness with an unattended
certification gate, then layers swarm scheduling on top.

## Runtime Evidence

Incident histogram from `X:\Eternia\.hermes\agent-runtime\incidents` (36 incidents):

```text
16  model_invalid_output
11  run_budget_exceeded
 6  provider_failure
 2  environment_blocker
 1  runtime_freeze (persona_invalid_output_loop)
```

Stuck-loop archetypes reconstructed from run records:

1. Proof-request loop (task_ac3d4af3, 2026-05-30): Dev emitted `request_test_run`
   11 consecutive times, 2-4.5M tokens and 50-90 api calls per run, ~40.9M tokens
   total, ended only by a manual daemon/process-tree kill.
2. Supervisor decision loop (task_56e1c255, 2026-05-21): Neko emitted `needs_context`
   31 consecutive times at ~7k tokens each, ended by manual force-stop. Same task:
   QA needed 9 consecutive `request_file_reads` runs (one run per file read).
3. Schema-rejection retry loop (task_737ab93c, 2026-06-05): 10 failed runs; near-valid
   packets rejected for unsupported keys and two deliveries rejected as
   "contains secret-looking text" on ordinary prose.
4. Block dead-end (task_1500c055, 2026-06-09): recovery menu offered only
   `resolve_incident` with zero open incidents; recovery signal consumed; mission
   noops forever.
5. Process lifecycle lies (2026-06-10, live): `daemon stop` taskkill without `/F`
   survived, status wrote `offline` anyway, second daemon started, two daemons ticked
   concurrently until the loop-top ownership check resolved it.
6. Environment blockers (task_0cbb1ad6, 2026-06-10): preflight `repo_clean` blocked
   backend_dev on the operator's own unrelated dirty files; provider env failures
   (`No module named 'openai'`, jiter import) burned runs.
7. Context starvation (task_af5c257c delivery known_gaps): posts/models.py, routes,
   feed, crosspost rejected as `context_bundle_too_large`; API registry withheld for
   `redaction_risk`. The investigation shipped with holes it could name but not close.
8. Handoff repair / proof visibility drift (task_7010f6c0, 2026-06-10): focused
   backend proof passed, but the follow-up delivery packet lost acceptance-critical
   handoff details after normalization/compaction, so QA could not approve from the
   visible packet. The same run also exposed a multiline here-doc proof command being
   collapsed into an invalid one-liner before final-gate execution.

## Current Implementation Audit - 2026-06-10 (second pass)

Re-verified against the working tree after concurrent fixes landed mid-audit.

Already fixed - do not re-implement:

- Unknown packet keys normalize instead of reject for `handoff_packet`, `delivery`,
  `qa_review`: `agent_runtime/packets.py::_normalize_unknown_packet_metadata`
  (drop + `dropped_fields` + `operator_note`). All four June 5 unsupported-key
  failures intake cleanly today.
- One in-session repair retry for invalid decisions with structured feedback
  (`invalid_field`, `invalid_value` preview): `agent_runtime/ticker.py::
  _should_retry_invalid_decision`, `_decision_repair_feedback`,
  `_DECISION_REPAIR_MAX_ATTEMPTS = 1`.
- Bare secret words masked in place (`packets.py::_mask_bare_secret_terms`) in fields
  where bare terms are allowed, instead of rejecting the packet.
- Blocked-state Neko menu offers `resolve_incident` only with an open incident,
  otherwise the rescope menu (`worker_actions.py::_neko_actions`,
  `context_builder.py::_required_next_decision`; tests in
  `tests/agent_runtime/test_worker_actions_blocked_menu.py`).
- `stop_daemon` escalates to force-kill, verifies pid exit, never reports offline
  while the pid is alive (`agent_runtime/daemon.py::stop_daemon`,
  `_wait_for_pid_exit`; tests in `tests/agent_runtime/test_daemon.py`).
- `read_daemon_status` validates pid liveness and reports
  `{"state": "offline", "cleared_reason": "dead_pid"}` for dead pids.
- Operator unblock exists: `harness task unblock <id> --reason ... [--state ...]
  [--rescope] [--foreground]` (`hermes_cli/harness.py::_cmd_task_unblock`): closes
  open incidents, strips `neko_block_recovery_attempted`, clears recovery markers,
  optional plan rescope and foreground activation. `task history` exists.
- `harness run show <run_id> --json` exists for operator-safe run/event inspection.
- `task show` on an archived task returns the archive summary
  (`_archived_task_summary`) instead of a traceback.
- `status --json` exposes `background_open_tasks` / `unparked_open_tasks` lane counts.
- Checklist sanitizer drops invalid HUD statuses instead of failing valid decisions
  (`role_checklists.py`).
- Auto final-gate command proof preserves multiline here-doc commands and no longer
  collapses them into invalid one-liners (`final_gate.py::_clean_command`; test in
  `tests/agent_runtime/test_final_gate.py`).
- Auto final-gate reuse: if the current stage already has a passed final-gate proof,
  the Harness reuses that proof instead of rerunning the same command during handoff
  repair (`ticker.py::_existing_passed_final_gate_proof_ids`).
- Handoff repair with an existing passed proof can route directly back to QA without
  launching another Dev token run (`ticker.py::_recover_handoff_repair_with_existing_proof`).
- Legacy QA/release stages no longer count as remaining Dev implementation work, and
  deterministic handoff paths sync typed `mission_plan` stage status after proof
  handoff/recovery (`planning.py`, `ticker.py::_sync_typed_plan_stage_status`).

Implementation status by sub-stage - verified against commit `fdc8e093b` (2026-06-10,
third pass; trust this over any earlier audit text):

DONE - do not re-implement (verify only):

- **61A no-progress guard**: enforced at `ticker.py::_apply_no_progress_guard`
  (called from the envelope-continuation site), threshold from
  `runtime_config.role_envelope.max_no_progress_repeats`; envelope closed with
  `close_reason="no_progress_guard"`.
- **61B redaction repair**: `packets.py::_scan_packet_redaction` masks bare terms
  (`_mask_bare_secret_terms`) and path segments (`_mask_path_segments`), truncates
  >4000-char strings to a redaction-safe excerpt; hard-reject remains only for
  secret-shaped values. `ticker.py::_should_retry_invalid_decision` refuses retries
  only for the dev-plan loop guard; everything else gets the one repair retry.
- **61C context lane**: `context_requests.py::MAX_FILE_BYTES = 131_072`,
  `MAX_BUNDLE_BYTES = 262_144`, window reads (`windows` payload key and
  `_parse_window_path`), `_file_skeleton` oversize fallback, line-level masking
  (`<line N redacted>`); whole-file `redaction_risk` only for majority-masked files.
- **61D recovery re-arm**: `recovery_flags.py::current_block_recovery_signal` now
  includes packet count, fulfilled context request count, and closed incident count;
  any new evidence re-arms exactly one recovery attempt. Operator path:
  `harness task unblock`.
- **61E daemon lease**: `daemon.py::_acquire_daemon_lease` /
  `_refresh_daemon_lease` / `_clear_daemon_lease`, `DAEMON_LEASE_TTL_SECONDS = 15`;
  acquisition happens before the loop, refresh in heartbeat, clear in `finally`.
  Plus: merged final offline status on stop-requested exits
  (`_write_final_offline_status`), no clobber of a status owned by another live pid,
  auto-archive of the terminal foreground goal
  (`MissionDaemon._archive_terminal_target`).
- **62I-1 replay registry** (lives in Stage 62 but interlocks here):
  `agent_runtime/replay_scenarios.py` + auto-capture at
  `ticker.py::_capture_replay_scenario` + `harness playground list|show|replay`.
  Every new contract failure becomes a replay scenario automatically.
- Tests live in `tests/agent_runtime/`: `test_daemon.py` (lease, stop, final status,
  auto-archive), `test_redaction.py`, `test_context_requests.py`, `test_recovery.py`,
  `test_replay_scenarios.py`, `test_final_gate.py`, `test_worker_actions_blocked_menu.py`.
  Full suite: 826 passing at `fdc8e093b`. (The per-stage test filenames named in the
  stage sections below were written before implementation; the actual files above
  supersede them.)

STILL OPEN - the remaining implementation scope (61H, 61I):

- **61F contract simplification**: IMPLEMENTED after baseline commit `2a2c086f9`.
  - `decision_contract_registry.py` `normalization_policy` defaults to
    `reject_unknown` (dataclass defaults at lines ~53/114); only five contracts opt
    into `drop_unknown_with_operator_note`/related policies. Flip remaining
    non-acceptance-critical contracts per-contract (do NOT blind-flip the default);
    keep `reject_unknown` for proof gates, verdicts, and stage-plan shapes.
  - `mission_plan.py:845,860` unknown keys still hard-fail.
  - `planning.py` still prose-matches: `_payload_is_launcher_handoff` (lines ~244,
    304, 310, 613, 622) and `_summary_is_missing_launcher_proof`. Replace with the
    typed `handoff_request` payload field per the 61F section below.
  - `Task.risk_flags` (`models.py:74`) is unvalidated freeform; task_7010f6c0 stores
    full sentences as flags. Enum + `operator_notes` migration per 61F section.
- **61G unattended certification gate**: IMPLEMENTED after baseline commit
  `2a2c086f9`; `burn_in.py` now records unattended-case criteria, a consecutive
  green ledger, a `burn-in summarize` certification verdict, and
  `runtime_config.swarm` with the Stage 62A playground exemption.
- **61H swarm scheduler**: gated on 61G green. Build on
  `runtime_instances.GoalRuntimeInstanceStore` and `repo_bundles.RepoBundleStore`
  (verified APIs listed in the Stage 62 audit section).
- **Handoff-repair packet width** (from archetype 8): delivery packet schema is too
  narrow for acceptance-critical handoff repair. Specified as the first item of the
  61F section (extend `DELIVERY_KEYS`, `_validate_delivery`, `_compact_delivery_body`,
  context projection, observability, and the Dev/QA skills so the fields survive
  normalization and compaction). This portion of 61F can land first and independently.

## Product Decision

Smoothness before swarms. The swarm scheduler (61H) must not land until the
certification gate (61G) is green: 10 consecutive unattended completions, including
edit goals, zero manual ticks, zero manual incident closes, zero process kills.
The gate is enforced in config, not by convention.

Simplification beats new machinery: 61A enforces a counter that already exists; 61B
reuses the existing masking/normalization paths; 61D extends an existing signal
string. No new subsystem is introduced before 61H.

## Stage 61A - Enforce the Existing No-Progress Counter

**Status: IMPLEMENTED at `fdc8e093b` (`ticker.py::_apply_no_progress_guard`). Section kept as design record - verify, do not re-implement.**

Scope: wiring only. The counter and config already exist.

- In `ticker.py` at the envelope continuation site (the `record_progress` call,
  currently `ticker.py:839`), after progress is recorded: if
  `envelope.no_progress_count >= config.role_envelope.max_no_progress_repeats`,
  do not schedule the same persona on the same stage again. Instead:
  - For dev/qa envelopes: close the envelope with `close_reason="no_progress_guard"`
    and route a typed `no_progress` escalation packet to Neko (packet body: persona,
    stage, repeated decision_type, `no_progress_count`, last progress hash inputs,
    proof/patch counts). Register the packet kind in the Stage 58 registry.
  - For a Neko envelope tripping the guard: block the task with the signature
    attached to `harness_self_heal` and an `incident.opened`
    (`kind="no_progress_loop"`), so 61D/unblock owns recovery.
- Strengthen `_progress_hash` (`role_envelopes.py:130`) to include a normalized
  acceptance-critical payload digest of the applied decision (not just
  decision_type), so "same request_test_run with identical commands" counts as no
  progress even when proof_ids are empty both times.
- Surface `no_progress_count` and the guard threshold in the persona HUD
  (`context_builder.py` mission HUD) so the persona sees "you already tried exactly
  this" before the guard trips.
- Default `max_no_progress_repeats` stays 1 (third identical attempt never runs).

Tests (`tests/agent_runtime/test_no_progress_guard.py`):

- Replay archetype 1: three identical `request_test_run` decisions with no new proof
  -> third run is not scheduled; Neko receives the `no_progress` packet.
- Replay archetype 2: Neko repeating `needs_context` with unchanged evidence -> task
  blocks with `no_progress_loop` incident instead of a 31-run loop.
- Progress resets the counter: identical decision after a new proof id runs normally.

## Stage 61B - Redaction That Repairs Instead of Rejects

**Status: IMPLEMENTED at `fdc8e093b` (`packets.py::_scan_packet_redaction` masking/truncation; retry policy open). Section kept as design record - verify, do not re-implement.**

Scope: `packets.py::_scan_packet_redaction` and
`ticker.py::_should_retry_invalid_decision`.

Classification change inside `_scan_packet_redaction`:

- Keep hard-reject (run fails, incident) only for secret-shaped values: matches of
  `_SECRET_PATTERNS` / `_SECRET_VALUE_FRAGMENTS` (key=value shapes, bearer tokens,
  high-entropy spans). These indicate a real leak, never prose.
- Convert to mask-in-place (reusing `_mask_bare_secret_terms` mechanics, recorded in
  `truncated_fields`/`dropped_fields` normalization info):
  - bare secret words in fields currently flagged `allow_bare_secret_terms=False`;
  - secret-bearing path segments (`auth`, `config`, `token`, ...): mask the segment,
    keep the rest of the string;
  - absolute paths in non-acceptance-critical fields: rewrite to repo-relative when
    under a known repo root, else mask.
- Convert to truncate-in-place: strings over 4000 chars are truncated with a
  `truncated_fields` record instead of rejecting the packet ("raw log-like text").
- `_should_retry_invalid_decision`: remaining redaction rejections (secret-shaped
  only) become retryable once like other contract failures; the repair feedback names
  the exact path and the offending shape class, never the value.

Tests (`tests/agent_runtime/test_packet_redaction_repair.py`):

- "update the auth/config loader" in `delivery.summary` -> intakes masked, run
  completes.
- 6000-char proof excerpt in `remaining_gaps[0]` -> truncated intake, recorded.
- `API_KEY=sk-...`-shaped value anywhere -> still hard-rejected, retryable once,
  feedback contains path but not value.
- Both June 5 "secret-looking text" payloads (from incident records) -> intake
  without failed runs.

## Stage 61C - Context Lane That Can Feed Real Files

**Status: IMPLEMENTED at `fdc8e093b` (128/256KB caps, windows, skeleton, line-level masking in `context_requests.py`). Section kept as design record - verify, do not re-implement.**

Scope: `agent_runtime/context_requests.py`.

- Raise `MAX_FILE_BYTES` to 131_072 and `MAX_BUNDLE_BYTES` to 262_144. The lane
  serves excerpts to a model context, not a UI; 32KB starves any real module.
- Add windowed reads: request paths accept `path#Lstart-Lend` or a parallel
  `windows` payload key (`{"path": ..., "start_line": ..., "max_lines": ...}`).
  `_fulfill_request` serves the window with line numbers and total-line metadata so
  the persona can request the next window instead of receiving
  `context_bundle_too_large`.
- Oversize fallback: when a whole-file request exceeds `MAX_FILE_BYTES`, return a
  structural skeleton (def/class lines with line numbers, `MAX_DIRECTORY_ENTRIES`
  bounded) plus a `file_too_large_use_windows` path_result instead of `unsupported`.
- Line-level redaction: replace `SECRET_PATTERN` whole-file withholding with
  per-line masking — matched lines ship as `<line N redacted>`; the file is
  delivered. Whole-file `redaction_risk` remains only when more than half the lines
  mask (likely a real secrets file).
- Batch reads stay one run: `request_file_reads` already carries multiple paths;
  with windows and the higher bundle cap, QA's 9-run read loop becomes one run.

Tests (`tests/agent_runtime/test_context_requests_windows.py`):

- 200KB file: whole-file request returns skeleton + window hint; windowed request
  returns exact lines; both under the bundle cap.
- File containing "Authorization: Bearer <token>" doc line: delivered with that line
  masked, not withheld.
- Mixed request (3 small files + 1 oversize) -> `fulfilled_partial` with windows
  hint, not a dead `unsupported`.

## Stage 61D - Recovery That Re-Arms on New Evidence

**Status: IMPLEMENTED at `fdc8e093b` (evidence cursor in `recovery_flags.py::current_block_recovery_signal`; `task unblock` CLI). Section kept as design record - verify, do not re-implement.**

Scope: `agent_runtime/recovery_flags.py`. The operator path (`task unblock`) exists;
this stage automates the common case.

- Extend `current_block_recovery_signal` with an evidence cursor: counts of recorded
  packets, fulfilled context requests, and closed incidents for the task (cheap
  lookups already available at tick time). Any new evidence then re-arms exactly one
  recovery attempt automatically — `block_recovery_attempted_for_current_signal`
  compares against the richer signal and returns False.
- On re-arm, the recovery run's HUD includes what changed since the consumed signal
  (the delta that justified another attempt), so Neko routes on new evidence instead
  of repeating itself (interlocks with 61A: identical re-decision trips the guard).
- Keep one-attempt-per-signal semantics. No retry storms.

Tests (extend `tests/agent_runtime/test_state_machine.py` recovery cases):

- Blocked task + consumed signal + new recorded packet -> next tick routes Neko
  recovery once; without new evidence -> noop persists.
- Replayed task_1500c055 fixture: sanitizer fix + menu fix + re-arm -> task replans
  instead of nooping forever.

## Stage 61E - Daemon Singleton Lease

**Status: IMPLEMENTED at `fdc8e093b` (lease acquire/refresh/clear, final offline status, auto-archive in `daemon.py`). Section kept as design record - verify, do not re-implement.**

Scope: `agent_runtime/daemon.py`.

- Lease file `daemon.lease` next to `daemon_status.json`: `{pid, acquired_at,
  expires_at}` with a TTL of 3x heartbeat interval, written atomically
  (`utils.atomic_json_write`).
- `start_daemon` and `MissionDaemon.run_foreground` startup: acquire the lease or
  exit immediately with `error="daemon_lease_held"` (start) / loop-exit (foreground).
  A stale lease (expired or dead pid) is reclaimed with an event.
- Heartbeat thread refreshes the lease alongside the status heartbeat; losing the
  lease (file owned by another live pid) stops the loop at the next iteration as
  today, but acquisition at startup closes the concurrent window observed 2026-06-10.
- `stop_daemon` clears the lease after verified kill.

Tests (extend `tests/agent_runtime/test_daemon.py`):

- Second `start_daemon` while lease held by live pid -> not started, no Popen.
- Stale lease (dead pid) -> reclaimed, daemon starts.
- `stop_daemon` clears lease only after pid exit.

## Stage 61F - Contract Simplification

**Status: IMPLEMENTED after baseline commit `2a2c086f9`. Section kept as design record.**

Scope: delete special cases the general mechanisms now own.

- First-class delivery/handoff repair fields before the general simplification:
  extend `DELIVERY_KEYS`, compact packet views, context projection, observability,
  Mission Control rendering, and Dev/QA skills so acceptance-critical handoff data
  survives normalization. Required fields/classes:
  - `inspected_paths`: repo-relative files the agent actually inspected.
  - `changed_paths`: repo-relative files changed in the current stage, distinct from
    legacy `changed_files` only until migration is complete.
  - `dirty_baseline`: whether unrelated pre-existing edits were present and how they
    were preserved.
  - `coverage_claims`: specific acceptance criteria covered by attached proof IDs.
  - `known_non_coverage`: explicit areas not covered by the current proof.
  - `proof_reuse_basis`: why an existing passed proof is valid for this handoff.
  - `failed_proof_classification`: e.g. `shell_wrapper_error`, `env_blocker`,
    `product_failure`, `unknown`, with linked proof IDs.
  - `handoff_repair`: typed boolean/object that says the delivery repairs metadata
    only and should not imply a product re-edit.
- Unknown delivery fields remain preserved in raw packet evidence and moved to
  `operator_note`/normalization metadata, but the fields above are first-class and
  must be visible to QA, Mission Control, and `task history`.
- Add a task_7010f6c0 replay fixture: after a passed focused proof and a metadata
  repair delivery, QA sees inspected paths, changed paths, dirty-baseline handling,
  proof reuse basis, and known non-coverage without manual state edits.
- Decision payload and mission_plan unknown keys route through the same
  normalize-and-record path as packets (`decision_contract_registry.py:229,299,495`,
  `mission_plan.py:845,860`): drop unknown keys with `dropped_fields`, hard-fail only
  acceptance-critical absences. The single repair retry stays for genuine failures.
- `Task.risk_flags` becomes enum-validated against a registry of known flags
  (migration: unknown/prose flags move to a new `Task.operator_notes: list[str]`,
  preserved verbatim). Writers (`planning.py`, `state_machine.py`, `worker_actions.py`)
  switch to the enum.
- Replace prose matching in `_coerce_neko_needs_context_to_handoff_continuation`
  (`planning.py`): Neko's `needs_context` payload gains a typed optional field
  `handoff_request` (registered shape, carries target repo/stage). The coercion keys
  on the typed field; `_summary_is_missing_launcher_proof` and
  `_payload_is_launcher_handoff` string heuristics are deleted after one deprecation
  release where both paths log agreement.
- After 61A lands, audit `state_machine.next_action` branches whose only purpose was
  loop prevention (same-stage retry guard wiring) and delete the ones the signature
  guard provably covers (their test cases must pass against the guard first).

## Stage 61G - Unattended Certification Gate

**Status: IMPLEMENTED after baseline commit `2a2c086f9` (`burn_in.py` certification ledger plus `runtime_config.swarm`). Section kept as design record.**

Scope: extend Stage 47 burn-in (`agent_runtime/burn_in.py`) into the smoothness gate.

- A ledger case is "unattended" only if: zero manual ticks, zero manual incident
  closes, zero `task unblock` invocations, zero process kills, daemon self-started
  and self-stopped, task reached `done`, archive succeeded, final status clean.
  Manual-intervention detection: events of type `tick.requested` with actor `cli`
  while a daemon lease is held, `incident.closed` with operator reason, unblock
  events.
- Gate composition (minimum): one no-edit investigation, one backend edit goal with
  command proof, one Launcher visual goal with Stage C proof, one injected provider
  failure that recovers, one injected near-valid packet that recovers via 61B.
- `harness burn-in summarize` prints the gate verdict; a new
  `runtime_config.swarm.requires_certification = true` guard makes 61H refuse to
  enable while red.
- Gate target: 10 consecutive green unattended cases.

## Stage 61H - Swarm Scheduler (gated by 61G)

**Status: OPEN - blocked until 61G is green. Build on `GoalRuntimeInstanceStore` / `RepoBundleStore` (APIs listed in Stage 62 audit).**

Build on Stage 60 lanes; no new runtime model.

- N concurrent foreground lanes (start N=2): `GoalRuntimeInstanceStore` allows N
  `lane="foreground"` instances; each gets its own targeted daemon with its own
  lease (`daemon.lease.<instance_id>`); the global daemon is retired in swarm mode.
- Write isolation by repo bundle: the scheduler refuses to activate a lane whose
  mission plan holds write scope on a repo bundle another active lane holds; it
  queues (parks) instead. Read-only investigation lanes may share repos.
- Persona instance pool: `personainst_<role>` becomes `personainst_<role>_<n>`
  (`persona_instances.py`); assignment binds a free instance per lane; worker-session
  isolation already exists per instance.
- Global budget governor: per-lane token budget plus a global ceiling in
  `runtime_config.swarm`; the scheduler parks (never kills) lanes when the global
  ceiling nears, resumes by priority; parked-by-budget is a first-class lane state in
  `status --json`.
- Swarm observability: `status --json` gains `lanes[]` (task, instance, persona
  instances, tokens burned, last decision, no_progress counters, lease state) so one
  glance answers "is anything stuck" across the swarm.

## Risk Audit

- 61A false positives: a persona legitimately repeating a decision after an external
  fix (e.g. rerunning the same proof command after an env repair) would trip the
  guard. Mitigation: evidence cursor includes incident closes and environment
  fingerprint changes, so a real fix resets the counter (interlock with 61D signal).
- 61B under-redaction: masking instead of rejecting risks shipping a missed secret
  shape. Mitigation: secret-shaped detection stays hard-reject; masking applies only
  to word/path/length classes that have never matched a real secret in the incident
  history; packet records keep `dropped_fields`/`truncated_fields` for audit.
- 61C larger bundles raise token cost per context run. Mitigation: windows default to
  bounded line ranges; the skeleton fallback steers personas to windows; budget gates
  already cap per-run tokens.
- 61E lease file corruption could deadlock daemon starts. Mitigation: stale-lease
  reclaim on expired TTL or dead pid; `daemon stop` clears unconditionally after
  verified kill.
- 61F enum migration could reject legacy tasks. Mitigation: migration maps unknown
  flags to `operator_notes` instead of failing loads; loader tolerates both shapes
  for one schema version.
- 61H write-isolation deadlock (two lanes each waiting on the other's bundle).
  Mitigation: scheduler acquires all bundles for a lane atomically at activation or
  parks; no incremental acquisition.

## Implementation Order

61A-61E are IMPLEMENTED at `fdc8e093b` (see the status audit section). Remaining
order for the implementer:

1. 61F delivery/handoff repair fields (first item of the 61F section; independent,
   fixes the observed task_7010f6c0 QA visibility failure; validate with
   `harness playground replay` against captured scenarios).
2. 61F simplification: per-contract normalization-policy flips, `risk_flags` enum +
   `operator_notes` migration, typed `handoff_request` replacing prose matching.
3. 61G unattended certification gate (greenfield in `burn_in.py` +
   `runtime_config.swarm`; honor the Stage 62A playground exemption).
4. Certification loop until 10/10 green.
5. 61H swarm scheduler (Stage 62 owns the production operating layer on top).

Use the replay playground as the regression harness throughout:
`harness playground replay` must stay green (no `still_failing` regressions) after
every contract change.

## Implementation Readiness Checklist

- [x] 61A: guard wired at `ticker.py::_apply_no_progress_guard`; envelope closes with
      `close_reason="no_progress_guard"`. (Typed `no_progress` packet registration and
      HUD counter surface: verify; add if missing.)
- [x] 61B: redaction classes reclassified (mask/truncate in `packets.py`);
      secret-shaped still hard-rejects; retry policy refuses only the dev-plan loop
      guard.
- [x] 61C: windowed reads + skeleton fallback + line-level masking; caps 128/256KB.
- [x] 61D: evidence cursor in recovery signal; `task unblock` operator path.
- [x] 61E: lease acquire/refresh/reclaim/clear; final offline status; auto-archive;
      dual-start exclusion tested in `test_daemon.py`.
- [x] 61F: payload/mission_plan normalization; risk_flags enum + operator_notes
      migration; typed `handoff_request` replaces prose matching; delivery/handoff
      repair fields survive normalization, context projection, Mission Control, and
      QA review.
- [x] 61G: unattended criteria detect manual interventions; gate verdict in
      `burn-in summarize`; swarm config guard refuses while red.
- [ ] 61H: N-lane scheduler with bundle write isolation, instance pool, budget
      governor, `lanes[]` observability.
- [ ] Skills (`harness-mission-lead`, `harness-dev-delivery`, `harness-qa-verdict`)
      updated and reinstalled for: no_progress packet, handoff_request field,
      windowed context requests, first-class delivery/handoff repair fields.

## Done Criteria

- The three loop archetypes replay as fixtures and are stopped by the guard on the
  third identical decision.
- All 16 historical `model_invalid_output` payloads (including both redaction false
  positives) intake without failed runs; secret-shaped fixtures still reject.
- A 200KB source file is readable via windows; no `context_bundle_too_large` on any
  file under 128KB; keyword-bearing docs ship line-masked.
- A blocked task with consumed recovery re-arms automatically on a new packet and
  completes; without new evidence it stays settled (no loop).
- Two `daemon start` invocations cannot produce two ticking daemons (lease test +
  live check).
- The task_7010f6c0 handoff-repair replay reaches QA approval without manual state
  edits; the normalized delivery packet visibly preserves inspected paths, changed
  paths, dirty baseline, proof reuse basis, failed-proof classification, and known
  non-coverage.
- `harness burn-in summarize` shows the unattended gate green at 10/10.
- Two concurrent investigation goals complete in parallel lanes without sharing a
  write bundle, with truthful per-lane status throughout.
