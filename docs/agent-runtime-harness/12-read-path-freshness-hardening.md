# Stage 12 — Read-Path Freshness Hardening (event-less mutation chokepoint)

Status: HISTORICAL IMPLEMENTATION RECORD. The surviving freshness controls are
on `main`; post-mission-removal inventory corrected 2026-07-30.

> **Post-removal correction (2026-07-30).** The implementation log below is
> retained, but its event inventory is no longer the live catalog. The table and
> dated notes now distinguish surviving events from types removed with the
> mission lane and its cleanup waves. The authority is the current
> `agent_runtime/decision_contract_registry.py`; stream routing is the current
> `agent_runtime/stream.py`.

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
| `WorkspaceStore.set_active` | `active_workspace_id` | yes — `workspace.activated` | store layer |
| `RealmStore.set_active` | `active_realm_id` | yes — `realm.activated` | store layer |
| `WorkspaceStore.create/add_agent/remove_agent/rename/archive` | `workspaces[]` | yes — `workspace.created`, `workspace.updated`, `workspace.archived` | store layer |
| `RealmStore.create/bind_server/archive` | `realms[]` | yes — `realm.created`, `realm.updated`, `realm.archived` | store layer; `realm.archived` was correctly registered by `a7e679972` |
| Realm adopt / sync | `realms[]` | yes — `realm.adopted`, `realm.sync.pulled`, `realm.sync.published` | `realm_membership.py` / `realm_sync.py` |
| Blueprint catalog mutation | removed | `blueprint.saved` was removed with the stage-graph/blueprint lane (`d3a414cac`) | no live `blueprint save` surface |
| `AgentStore.save` | `agents[]` | yes — `persona.updated` | store layer |
| `RunStore.cancel/close_run` | persisted historical run rows | yes — `run.closed`; it is live and remains registered | store layer; `run.heartbeat` and `run.approved` were removed by `8c1c8e6cc`, then `run.opened` by `06eee42fa` |
| stream watchdog | scope/catalog freshness | yes — `state.reconciled` | `agent_runtime/stream.py` |
| background Mission Daemon | removed | its event family and heartbeat side channel are gone | heartbeat frames now carry only optional stream activity |

Historical gaps at the 2026-07-09 audit boundary: (1) scope emission lived in
`hermes_cli/harness.py` verbs rather than the stores, so programmatic
`set_active`/`save` callers could regress; (2) nothing bounded staleness when
the rule was violated; (3) event payload contracts were decorative. Slices
B–D below closed those gaps. The current table above reflects their surviving
post-removal state.

## Design

Every fix follows one principle: **make the violation impossible (CI) or
self-announcing (runtime), never "be more careful".**

### Slice A — historical `blueprint.saved` event

This slice closed the live blueprint-catalog gap in 2026-07-09. The entire
blueprint save surface and its event contract were later removed with the
stage graph (`d3a414cac`). `blueprint.saved` is not a current event type.

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

**2026-07-30 correction:** those two names describe the 2026-07-09 strict-run
measurement, not current contracts. `task.created` was de-registered with the
mission event catalog in `afd6c0a83`; `run.heartbeat` left with its writer in
`8c1c8e6cc`.

### Slice F — pin the heartbeat side channel

Historical outcome: this slice pinned the then-live daemon block. The
background Mission Daemon was later retired (`6b558417f`), and current
heartbeat frames no longer carry daemon status. Their only optional side block
is transient stream activity such as an in-progress snapshot build; durable
read-model changes still require events.

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
- Historical only: `blueprint save` appended `blueprint.saved`; both are now
  removed (`d3a414cac`).
- D: strict mode rejects a missing-summary-field payload; observe mode warns
  and still appends.
- F: historical daemon block schema pin; the daemon and block are now removed.
- Suites: `tests/agent_runtime/test_stream.py`,
  `test_goal_workspace_realm_stage42.py`, `blueprints/`,
  `test_realm_membership.py`, new `test_store_event_invariant.py`.

## 2026-07-09 live proof (historical)

1. `hermes harness stream` + `hermes harness realm use <other>` →
   delta with new `active_realm_id` within one poll cycle, no forced poll.
2. Historical proof only: `blueprint save` refreshed the blueprint list before
   the stage-graph lane was removed.
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
- 2026-07-09 recorded next steps (historical): merge the Hermes branch; commit
  the Launcher working set; live-soak `producerViolationCount` at zero, then
  remove the forceFresh gate bypass.
- 2026-07-30 correction: the Hermes changes are present on `main`. This
  docs-only cleanup did not reassess the off-repo Launcher follow-ups.
