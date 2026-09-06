# Runtime observability — the hermes half's running record (2026-09-06)

Field notes for the hermes stages of
`EterniaLauncher/docs/mission_control/planned/runtime-observability.md`. The
launcher half's notes live beside that plan; this file is the record of what was
built in THIS repo, what it cost to learn, and what is still not proven.

## O-h

Rulings built: **RO-3** (hermes logs what it removes) and **RO-7**'s producer
half (the additive `timing` block on a chat turn's terminal frame).

Branch `feat/runtime-observability-hermes`, from `origin/main` at
`af894484` — in a hermes worktree at `X:\wt\o-h-hermes`. The worktree the
dispatch named (`X:\wt\o-h`) is a worktree of the **EterniaLauncher** repo, not
this one; it was left untouched.

### What was built

**RO-3 — `agent_runtime/serve_registry.py`.**
`prune_stale_serve_instances` takes two new keyword arguments, `emit` and
`boot_id`, both optional and both ignored by every existing caller. For each row
it ACTS on it calls `_emit_pruned_event`, which builds one
`serve_registry_pruned` event:

```json
{"event": "serve_registry_pruned", "action": "removed", "pid": 43244,
 "reason": "stale_dead_pid", "classification_reason": "pid_not_running",
 "by_pid": 11728, "row_boot_id": "1cb3ba79...", "boot_id": "e64d4601..."}
```

* `action` ∈ `SERVE_REGISTRY_PRUNE_ACTIONS` = `removed | refused |
  remove_failed`. `refused` is the ruling's "and what it refuses" — the recycled
  and unclassifiable rows the prune deliberately KEEPS. A **`live` row emits
  nothing**: it is this boot's own entry on every boot.
* **Vocabulary deviation, as the dispatch allowed for.** The dispatch proposed
  `dead_pid | recycled_pid | unknown_row | foreign_store`. `classify_serve_instance`
  produces neither that set nor anything like `foreign_store`: its classifications
  are `live | stale_dead_pid | stale_recycled_pid | unknown` and its reasons are
  `pid_missing | liveness_unreadable | pid_not_running | no_identity_baseline |
  start_time_unreadable | start_time_mismatch | cmdline_unreadable |
  cmdline_not_serve_like | record_unreadable`. The event therefore carries the
  CLASSIFIER's words: `reason` = the classification verbatim,
  `classification_reason` = the finer word. Those are also the two key names the
  aggregate `serve_instances_pruned` report's rows already use, so no reader has
  to learn a second spelling. Nothing in this repo produces `foreign_store` — the
  prune is scoped to one store root by construction.
* `boot_id` is the PRUNER's (absent when the caller names none — never null),
  `row_boot_id` the pruned row's. That matches how `serve_instances_pruned`
  already joins to a boot's `ready` frame.
* The sink is called inside a bare `try/except`: bookkeeping about bookkeeping
  must not fail a boot or leave a dead row behind.

**RO-3 — `hermes_cli/harness_parts/serve.py`.** The boot prune passes
`emit=_service_log, boot_id=boot_id` — the sink the serve already owns, which is
the `--service` runtime's `<pid>.stderr.log` and an ordinary `stderr` frame to
the supervisor otherwise. **One ordering move was required and is the whole
reason the e2e is worth having:** the RL-19 stderr-log arming
(`open_serve_stderr_log` + `stderr_proxy.set_mirror`) ran AFTER the prune, so
under `--service` — where RL-17 hands all three stdio handles `DEVNULL` — every
prune verdict would have been written to a stderr nobody reads. The arming block
now sits between registration and the prune, and the registration `if
store_root_path is not None:` is split in two around it (no re-indentation of
either body). Safe there because the log is not a registry row
(`_NON_ROW_SUFFIXES`), so nothing between the two points can see it.

*Deviation from the dispatch's letter:* the dispatch asked for "the stdout NDJSON
`event` frame when on stdio". `_service_log` writes to `sys.stderr`, which the
supervisor already reads as `{"id":null,"event":"stderr","line":…}` — the lane
`serve_instances_pruned` has always used. A second stdout frame shape would be a
new frame type for one line of telemetry. Same sink as the existing report,
deliberately.

**RO-7 — `agent_runtime/mission_chat_phases.py`.** New
`turn_timing_block(*, phases, profile_timing)` plus `TURN_TIMING_KEY` and
`TURN_TIMING_ORDER` (the closed set). It COPIES, never derives:

| wire key | source |
|---|---|
| `turn_context_ms` | `profile_timing.profile_conversation_turn_context_ms` |
| `request_assembled_ms` | `phases.request_assembled` |
| `provider_first_byte_ms` | `phases.provider_first_byte` |
| `responses_create_ms` | `profile_timing.profile_provider_responses_create_ms` |
| `stream_consume_ms` | `profile_timing.profile_provider_stream_consume_ms` |
| `builds_overlapped` | `phases.builds_overlapped` |
| `resident_actor_reused` | `profile_timing.resident_actor_reused`, as a bool |

Absent stays absent; a MEASURED zero survives; non-integers, a `True` in a
millisecond slot, negatives and values past the `phases` block's own ceilings are
dropped rather than coerced. An empty block is `None`, and the key is then not on
the payload at all.

**RO-7 — `hermes_cli/harness_parts/persona_commands.py`.**
`_mission_chat_commit_turn` builds the block immediately before the success
payload, from `turn_phases.snapshot()` and the handler's `_profile_timing`
superset — the same two instruments the terminal `transition_mission_chat_turn`
persist writes in the same breath — and spreads it into `data` under `timing`
when it is non-empty. The failure and replay payloads are untouched: they ran no
turn, and a block there would be a claim about a turn that did not happen.

### The frame the block rides

**The `--json` / `chat.final` payload — the last frame on the per-request lane
that carries a payload — and NOT the `exit` frame.** The dispatch said to put it
on "the frame the launcher's `requestFrames` fold treats as terminal". Read
against the launcher's code, those are two different frames and only one of them
can carry it:

* `mission_control_serve_session_io.dart::_routeExit` is the only site that calls
  `_observeRequestFrame(..., terminal: true)`, and it builds
  `MissionControlStreamLine.exit(code)` — an int and nothing else. A `timing`
  block on hermes' `exit` frame would be dropped by the launcher's own stream-line
  model before any consumer saw it.
* `mission_control_bridge.dart`'s fold decodes `type == 'chat.final'` into a map,
  names it "the conversational terminal", and builds the turn's
  `MissionControlActionResult` from it — which is the object RO-7's consumer half
  (`[MissionChatTiming]`) reads.

So the block rides the one dict `_mission_chat_emit` hands out, which IS both
lanes the dispatch named: the argv lane's `--json` exit payload, and the served
lane's `chat.final` frame. Sample, from
`test_the_terminal_payload_carries_the_seven_key_timing_block`:

```json
"timing": {"turn_context_ms": 1233, "request_assembled_ms": 7000,
           "provider_first_byte_ms": 8000, "responses_create_ms": 889,
           "stream_consume_ms": 1630, "builds_overlapped": 1,
           "resident_actor_reused": true}
```

(the two mark-derived values are the scripted clock's ticks, not a real turn's).

`runtime.chat.steer` has no terminal turn payload of its own —
`_cmd_mission_chat_steer` drops a steer into a running turn's queue through
`submit_mission_chat_steer` and returns; the block rides the MESSAGE turn's
terminal frame that the steer was folded into.

### Regenerated fixtures

Both byte-pinned producer families were regenerated with the same generators the
launcher's `tool/test_quality/check_producer_contracts.py` runs:

* `python scripts/generate_agent_runtime_stream_fixtures.py` →
  `tests/fixtures/stream_frames/` — **no byte changed** (18 files + MANIFEST).
* `python scripts/generate_agent_runtime_response_fixtures.py` →
  `tests/fixtures/response_envelopes/` — **no byte changed** (5 files + MANIFEST).

Neither family contains a chat-turn terminal payload or a serve `exit` frame, so
an additive key on the chat payload cannot reach them. `git status` over
`tests/fixtures/` was empty after both runs, and the launcher's checker agreed:
`producer contract fixtures match Hermes: stream frames + response envelopes`.
**The launcher has nothing to re-mirror for this stage.**

### Mutation table (red-first)

Every new assertion was run against a deliberate sabotage of the production
behaviour it claims to pin.

| # | Mutation | Red |
|---|---|---|
| 1 | drop `_emit_pruned_event(..., action="removed")` from the delete arm | `test_serve_registry.py::test_a_prune_that_removes_a_dead_row_says_so_once` — `assert 0 == 1` |
| 2 | emit for every row (drop the `!= CLASSIFICATION_LIVE` guard) | `test_serve_registry.py::test_a_live_row_says_nothing_at_all` |
| 3 | default a missing profile duration to `0` in `turn_timing_block` | `test_mission_chat_turn_timing_block.py` — `test_a_turn_whose_runner_reported_nothing_carries_only_what_was_marked`, `test_a_zero_that_was_MEASURED_survives`, `test_nothing_measured_is_no_block_at_all` and every `test_the_projection_drops_what_it_cannot_read` case |
| 4 | drop the `timing` entry from the terminal payload | 5 rows in `test_mission_chat_turn_timing_block.py`, incl. `test_the_terminal_payload_carries_the_seven_key_timing_block` and `test_the_non_streamed_json_payload_carries_it_too` |

Two more mutations are asserted but were not run as separate suite passes,
because the assertion is an equality against a constant the mutation would move:
bumping `RPC_CONTRACT_VERSION` reds `test_serve_gateway_chat_reply_lanes.py`'s
`assert hello["rpc"]["contract"] == RPC_CONTRACT_VERSION` only if the constant and
the greeting disagree — the row that actually pins the integer against an
additive change is `test_peer_authorization.py::test_the_method_joined_the_manifest_without_moving_the_contract_integer`,
which is unchanged and green; and arming the stderr log after the prune (RO-3's
ordering) is named as the killing mutation on the `--service` e2e but was not
re-run in that state, because the same claim is the reason the block moved and
the e2e is a 90-second real-boot row.

### Gate lines, verbatim

Every file alone, through the canonical runner (it silently drops paths when
given several).

```
$ bash scripts/run_tests.sh --paths tests/agent_runtime/test_serve_registry.py
=== Summary: 1 files, 35 tests passed, 0 failed (100% complete) in 17.3s (8 workers) ===

$ bash scripts/run_tests.sh --paths tests/agent_runtime/test_serve_socket_child_e2e.py
=== Summary: 1 files, 8 tests passed, 0 failed (100% complete) in 402.2s (8 workers) ===

$ bash scripts/run_tests.sh --paths tests/agent_runtime/test_serve_service_mode.py
=== Summary: 1 files, 11 tests passed, 0 failed (100% complete) in 27.0s (8 workers) ===

$ bash scripts/run_tests.sh --paths tests/agent_runtime/test_serve_service_foundations.py
=== Summary: 1 files, 27 tests passed, 0 failed (100% complete) in 27.2s (8 workers) ===

$ bash scripts/run_tests.sh --paths tests/agent_runtime/test_serve_stderr_log.py
=== Summary: 1 files, 20 tests passed, 0 failed (100% complete) in 65.2s (8 workers) ===

$ bash scripts/run_tests.sh --paths tests/agent_runtime/test_serve_gateway_chat_reply_lanes.py
=== Summary: 1 files, 2 tests passed, 0 failed (100% complete) in 55.2s (8 workers) ===

$ bash scripts/run_tests.sh --paths tests/hermes_cli/test_mission_chat_turn_timing_block.py
=== Summary: 1 files, 18 tests passed, 0 failed (100% complete) in 14.5s (8 workers) ===

$ bash scripts/run_tests.sh --paths tests/hermes_cli/test_mission_chat_turn_phases.py
=== Summary: 1 files, 42 tests passed, 0 failed (100% complete) in 27.9s (8 workers) ===

$ bash scripts/run_tests.sh --paths tests/hermes_cli/test_mission_chat_turn_phase_attribution.py
=== Summary: 1 files, 24 tests passed, 0 failed (100% complete) in 25.0s (8 workers) ===

$ bash scripts/run_tests.sh --paths tests/hermes_cli/test_mission_chat_budget_payload.py
=== Summary: 1 files, 10 tests passed, 0 failed (100% complete) in 15.7s (8 workers) ===

$ bash scripts/run_tests.sh --paths tests/agent_runtime/test_serve_rpc_method_tiers.py
=== Summary: 1 files, 7 tests passed, 0 failed (100% complete) in 10.1s (8 workers) ===

$ bash scripts/run_tests.sh --paths tests/agent_runtime/test_serve_rpc_office.py
=== Summary: 1 files, 17 tests passed, 0 failed (100% complete) in 18.6s (8 workers) ===

$ bash scripts/run_tests.sh --paths tests/agent_runtime/test_serve_rpc_chat_params.py
=== Summary: 1 files, 25 tests passed, 0 failed (100% complete) in 16.2s (8 workers) ===

$ bash scripts/run_tests.sh --paths tests/test_byte_pinned_fixture_families.py
=== Summary: 1 files, 3 tests passed, 0 failed (100% complete) in 18.0s (8 workers) ===

$ bash scripts/run_tests.sh --paths tests/agent_runtime/test_stream_contract_fixture.py
=== Summary: 1 files, 20 tests passed, 0 failed (100% complete) in 35.8s (8 workers) ===

$ bash scripts/run_tests.sh --paths tests/agent_runtime/test_office_layout_policy.py
=== Summary: 1 files, 49 tests passed, 0 failed (100% complete) in 45.5s (8 workers) ===

$ bash scripts/run_tests.sh --paths tests/agent_runtime/test_serve_ended_sidecar.py
=== Summary: 1 files, 29 tests passed, 0 failed (100% complete) in 20.3s (8 workers) ===

$ bash scripts/run_tests.sh --paths tests/agent_runtime/test_serve_rpc_chat_turn.py
=== Summary: 1 files, 22 tests passed, 0 failed (100% complete) in 37.3s (8 workers) ===

$ bash scripts/run_tests.sh --paths tests/test_coverage_claims_resolve.py
=== Summary: 1 files, 4 tests passed, 0 failed (100% complete) in 135.2s (8 workers) ===
```

Cross-repo, from the launcher checkout (read-only there):

```
$ python tool/test_quality/check_producer_contracts.py --hermes-root X:/wt/o-h-hermes
producer contract fixtures match Hermes: stream frames + response envelopes
```

Doc cite adjacency, against the same probe on `origin/main` (red on main by a
known baseline; the FAILED count must not move):

```
$ python scripts/doc_cite_adjacency.py --exclude archive --exclude planned
    machine-checkable         : 287
      passed, adjacent        : 169
      passed, inside symbol   : 11
      FAILED                  : 107          <- main: 107, after this stage: 107
```

Ruff: **not run.** `X:\Eternia\.hermes\venvs\hermes-agent\Scripts\` holds no
`ruff`, and `command -v ruff` finds none on PATH in this shell — the dispatch's
stated fallback.

### Flake seen once

The first run of `test_serve_socket_child_e2e.py` failed on
`test_probe_then_drain_over_the_socket_against_a_real_serve_child` with the
`harness serve connect --drain` CLI subprocess exiting `3221227274`
(`0xC000070A`-family, a Windows process-level crash, not an assertion). Two
subsequent runs of the same file were 8/8 green, and the failing row is one this
stage did not touch. Recorded, not chased.

### What is NOT proven

* **No real chat turn with a real provider carries the block.** Every RO-7 row
  runs a scripted agent (the `test_mission_chat_turn_phases` provider fake, and
  `_ScriptedAgent` on the real serve), so `turn_context_ms`,
  `responses_create_ms` and `stream_consume_ms` were asserted against a scripted
  `profile_timing` dict, never against numbers a provider produced. The served
  turn's block honestly carries FEWER keys for that reason, and the test asserts
  the join rule (`present in timing` iff `present in profile_timing`) rather than
  the seven.
* **`builds_overlapped` on the served lane is untested.** A serve worker in this
  test process has never led a snapshot build, so the counter is honestly absent
  there; the handler-level row arranges an overlapping build in the scripted
  clock's frame to get `1`.
* **No operator-visible proof of RO-3 on the live runtime.** The `--service` e2e
  plants a dead row and reads the line back out of `<pid>.stderr.log`, which is
  the mechanism; nobody has yet watched a real launcher restart produce one.
  That is RO-9's field arm, in the launcher's stages.
* **The RO-3 ordering move rests on one green row**,
  `test_serve_socket_child_e2e.py::test_a_service_boots_and_writes_what_its_prune_removed_into_its_own_log`.
  It was not re-run with the arming back in its old position.
* **RO-7's consumer half does not exist yet** — the launcher's `[MissionChatTiming]`
  line does not read `timing`, so nothing outside a test consumes the block. That
  is O-l-b.
* **`.steer` was reasoned, not measured.** No test sends a steer over the method
  lane and reads the message turn's terminal frame back.
