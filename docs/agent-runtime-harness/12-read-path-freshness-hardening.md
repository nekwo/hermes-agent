# Stage 12 — Read-Path Freshness Hardening (event-less mutation chokepoint)

Status: IN PROGRESS (2026-07-09). Branch: `fix/eventless-mutation-chokepoint`.
Owner: harness (fork-owned surfaces only: `agent_runtime/`, `hermes_cli/harness.py`).

## Problem

Mission Control's read path is watermark-gated on the EventLog offset: the
launcher read model only applies a snapshot whose `event_offset` is strictly
greater than the one it holds, and `stream_frames()` sends the full core only
on hydrate and per NEW event. Any store mutation that changes client-visible
snapshot state **without appending an EventLog event** is therefore invisible
to every stream consumer until an unrelated event advances the offset — the
"two-trap staleness" class. It has recurred three times (realm adopt →
realm sync → realm/workspace use), each fixed by adding events at the call
site. The recurrence is the finding: emission is convention, not enforcement.

Client-gate subtlety that constrains any fix: the launcher DROPS a
`hydrate`/`delta` frame whose offset is not strictly greater
(`mission_read_model.dart` `shouldApplySequence`). A backstop that re-sends
the same-offset snapshot is rejected; any backstop must ADVANCE the offset.

## Audit (2026-07-09, verified on main @ `640973756`)

| Mutation | Snapshot state | Event? | Where |
| --- | --- | --- | --- |
| `WorkspaceStore.set_active` (use / create-in-active-realm / realm-use reconcile) | `active_workspace_id` | yes — `workspace.activated` | CLI verbs (`_append_scope_event`) |
| `RealmStore.set_active` (use) | `active_realm_id` | yes — `realm.activated` | CLI verb |
| `WorkspaceStore.create/add_agent/remove_agent/rename/archive` | `workspaces[]` | yes — `workspace.created/.updated/.archived` | CLI verbs |
| `RealmStore.create/bind_server` | `realms[]` | yes — `realm.created/.updated` | CLI verbs |
| `RealmStore.save` via realm adopt / sync | `realms[]` | yes — `realm.adopted`, `realm.sync.*` | `realm_membership.py` / realm_sync |
| `save_blueprint` via `blueprint save` | `blueprints[]` | **NO — open gap (slice A)** | — |
| `AgentStore.save` (persona seed) | `agents[]` | **NO — no event seam at all (slice B)** | — |
| `RunStore.update` (bare) | run rows | not itself; every caller (open/heartbeat/close/cancel/approve) emits `run.*` | allowlisted |
| daemon `daemon_status.json` | daemon HUD | deliberately event-less; heartbeat frames carry the block | grandfathered (slice F pins its schema) |
| Task/Run/Proof/Incident/WorkerSession/RepoBundle/RuntimeInstance/RoleState stores | projections | yes (evented stores) | store layer |

Structural gaps: (1) scope emission lives in `hermes_cli/harness.py` verbs, not
in the stores — any programmatic caller of `set_active`/`save` silently
regresses; (2) nothing bounds staleness when the rule is violated anyway;
(3) event payload contracts are decorative (no validation on append — the
reconcile-clear already emits `workspace.activated` with an empty payload
against a contract that names `workspace_id`).

## Design

Every fix follows one principle: **make the violation impossible (CI) or
self-announcing (runtime), never "be more careful".**

### Slice A — `blueprint.saved` event (close the live gap)
`_cmd_blueprint_save` appends `blueprint.saved` after a successful
`save_blueprint`. Contract:
`EventContract("blueprint.saved", "Blueprint saved", ("blueprint_id",), ("version", "title"))`.

### Slice B — store-level emission + CI guard (the chokepoint)
- B1: `WorkspaceStore` / `RealmStore` / `AgentStore` gain
  `__init__(event_log: EventLog | None = None)` and emit inside every mutator
  (best-effort, log-on-failure — a broken event log never fails the write,
  matching the shipped verb semantics). `save()` emits a generic
  `*.updated {change: "saved"}` unless called with `emit_event=False` by a
  named mutator that emits its own specific event. The verb-layer
  `_append_scope_event` calls for scope verbs are deleted in the same commit
  (no double emission); event types and payloads are unchanged, so the
  existing stage42 verb tests double as the behavior-preservation proof.
  New contract: `persona.updated` for `AgentStore.save`.
  Clear-activation emits `{"cleared": true}` (fixes the empty-payload nit).
- B2: guard test `tests/agent_runtime/test_store_event_invariant.py` —
  AST-walks `agent_runtime/store.py`, finds every function/method containing a
  `_write_model(` call, and fails unless the name is classified in the test's
  curated map (EVENT_COUPLED with the event it emits, or EXEMPT with a written
  justification). A new write path fails CI until consciously classified.
  This is the enforcement that retires the recurrence.

### Slice C — bounded-staleness backstop (`state.reconciled`)
`stream_frames()` computes a cheap fingerprint (mtime_ns/size) of the
scope/catalog state that is NOT guarded by evented stores at runtime:
`active_realm.json`, `active_workspace.json`, workspaces/realms/agents dirs,
blueprint catalog dir. At heartbeat cadence, if the fingerprint changed while
the offset did NOT, append a synthetic
`state.reconciled {fingerprint, source: "stream_watchdog"}` event. It flows
out as an ordinary delta with a full rebuilt core, the offset advances, and
the launcher gate passes with zero client changes. Every `state.reconciled`
in the log names a rule-violating write path to fix at the source.
Guards: per-process last-fingerprint memo + at most one per heartbeat
interval; cross-process duplicates are harmless extra deltas.
Contract: `EventContract("state.reconciled", "Read model reconciled after event-less write", ("fingerprint",), ("source",))`.

**Declared SLO: client staleness ≤ 2× heartbeat interval (~10s) for ANY
write, rule-compliant or not.** Asserted by the backstop test.

Rejected alternatives: periodic same-offset rehydrate (client-dropped),
heartbeat-carried core (same gate problem + client change), heartbeat
fingerprint + client force-poll (rebuilds the forceFresh crutch).

### Slice D — schema-validated appends
`validate_event_payload(event_type, payload)` in
`decision_contract_registry.py` checks a contract's `summary_fields` are
present (unknown extra keys stay allowed — additive evolution is cheap).
`EventLog.append` wires it: **strict** (raise) when
`HERMES_EVENT_CONTRACT_STRICT=1`, **observe** (logging.warning, deduped once
per (type, missing-fields) shape per process so a high-frequency drift like
run.heartbeat can never spam logs) otherwise. The flag is enabled inside the
Stage 12 tests.

**Strict-run measurement (2026-07-09): 314 failed / 1310 passed** across
`tests/agent_runtime` — contract/emitter drift is broad (ticker, worker
sessions, transition events, plus many test fixtures that append minimal
events). The two highest-frequency REAL emitter drifts were fixed in this
stage (`task.created` missing `title`; `run.heartbeat` carrying no payload
against a contract naming `run_id`/`state`). Flipping strict suite-wide is
recorded follow-up debt: burn the remaining drift down type-by-type (fix the
emitter or right-size the contract), then enable the flag in conftest. Until
then observe-mode warnings name each drifted shape once per process.

### Slice F — pin the heartbeat side channel
The daemon block on heartbeat frames is the one sanctioned out-of-band
channel (eventing per-loop status writes would flood the log). Pin it:
a stream test freezes `daemon_status_schema()` required keys +
`schema_version`, and the module docs state the rule — new event-less state
gets EVENTS, not a second heartbeat rider.

### Slice E — launcher: forceFresh demoted from correctness to tripwire
(EterniaLauncher repo.) Instrument the forced-apply path: when a forced full
snapshot would have been REJECTED by the watermark gate, count + debug-log it
(`producer violation`). After A–D are live and the counter reads zero across
normal operation, remove the gate bypass; the counter stays as the tripwire
that names any future regression.

## Deferred with tripwires (deliberate, not forgotten)

- **G. Transport.** G1 versioned handshake frame (kills serve-child code-drift
  footgun) → G2 socket/named-pipe serve with reconnect semantics (retires the
  orphan class) → G3 multi-consumer fan-out (converge with the agent-gateway
  workstream; build the socket layer once).
- **H. Full-core deltas.** Add frame-size / deltas-per-minute telemetry;
  trigger written here: sustained > ~5 deltas/s or core > ~1MB ⇒ implement
  core diffing. Until then full-core-per-event is a documented decision.

## Test plan

- Store-level: mutators append the right event + advance the offset;
  clear-activation payload `{"cleared": true}`; stage42 verb tests pass
  unmodified (no double emission).
- B2 guard test fails on a synthetic unclassified `_write_model` caller.
- Stream: scope switch mid-stream yields a delta whose
  `core["active_realm_id"]` is the new realm; raw pointer-file write
  (rule violation simulated) yields a `state.reconciled` delta with the fresh
  pointer within the SLO.
- `blueprint save` appends `blueprint.saved`.
- D: strict mode rejects a missing-summary-field payload; observe mode warns
  and still appends.
- F: daemon block schema pin.
- Suites: `tests/agent_runtime/test_stream.py`,
  `test_goal_workspace_realm_stage42.py`, `blueprints/`,
  `test_realm_membership.py`, new `test_store_event_invariant.py`.

## Live proof

1. `hermes harness stream --ndjson` + `hermes harness realm use <other>` →
   delta with new `active_realm_id` within one poll cycle, no forced poll.
2. `blueprint save` while MC open → blueprint list refreshes.
3. Raw write to `active_realm.json` (bypassing the store) → visible
   `state.reconciled` delta within ~10s.
4. Launcher: scope switch from CLI while MC is open on stream — switcher
   updates with no operator action in the launcher (the background-mutation
   case forceFresh never covered). Live venv serves the CHECKOUT — the serve
   child must run this branch (or main after merge) for the proof.

## Log

- 2026-07-09: audit complete; plan written; implementation begins.
- 2026-07-09: slices A, B1, B2, C, D, F implemented on
  `fix/eventless-mutation-chokepoint` (per-slice commits). Launcher slice E
  (producer-violation tripwire: `MissionReadModel.applyForcedSnapshot` +
  `producerViolationCount`, both bridge force sites routed through it)
  implemented with tests in the EterniaLauncher repo. Strict-run measured
  314/1310 → suite-wide strict recorded as follow-up debt (see slice D).
- 2026-07-09 validation: full gate `tests/agent_runtime` +
  `tests/hermes_cli/test_harness_cli.py` — **1658 passed, 0 failed** (default
  posture). LIVE PROOF (real CLI, isolated root): (1) `realm use` reached the
  stream as a `realm.activated` delta carrying the new `active_realm_id`
  within one cycle, no forced poll; (2) a RAW pointer write (rule violation,
  no store, no event) reconciled via a `state.reconciled` delta in **2.78s**
  (SLO 4s at 2s heartbeat). The live proof also caught a real defect the unit
  tests missed: the watchdog memo must be taken BEFORE the delta batch — a
  post-batch memo absorbed writes racing the batch (fixed `8e12a5e02`,
  regression-pinned in `test_stage12_freshness.py`). Launcher: tripwire tests
  4/4 + bridge suite 74/74, `flutter analyze` clean; launcher edits left
  uncommitted (entangled with pre-existing uncommitted MC work in the same
  files — commit together with that set).
- REMAINING TO SHIP: merge this branch to hermes `main` (+ push per deploy
  policy); commit the launcher MC working set; live-soak
  `producerViolationCount` at zero, then remove the forceFresh gate bypass.
