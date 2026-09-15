# Planned — collapse to one stream transport

**Status: partly shipped; the remaining half is an observation window and a
deletion, not a build.** Source plan:
`../archive/2026-08-22-pre-consolidation/SINGLE_TRANSPORT_COLLAPSE_PLAN_2026-08-16.md`
(Plan C, stages TC-0…TC-4; the launcher tracks the same stages as EG-4.1…EG-4.4).
Live transport truth lives in `../03-transport-and-wire.md` — this file holds
only what has NOT landed.

## What already landed (do not re-plan it)

| Stage | Evidence |
|---|---|
| TC-1 / EG-4.1 — advertise the op lane so a client can gate instead of probe | `hermes_cli/harness_parts/serve.py::ops_manifest` (`:298`), `OPS_EVERY_TRANSPORT` (`:276`), `SUBSCRIBE_LANES` (`:295`) |
| TC-1 — prove the hub lane's frames ARE the argv stream's frames | `tests/agent_runtime/test_serve_stream_lane_parity.py` |
| TC-2 / EG-4.2 — launcher subscribes the hub behind the advertisement gate | launcher `lib/features/mission_control/data/mission_runtime_ops_manifest.dart:197`; `mission_control_serve_session_io.dart:1237` sends `fold_entities` on `{"op":"subscribe"}` |
| TC-2 — lane receipts naming lane + reason | launcher `lib/features/mission_control/data/mission_stream_lane.dart` (module header; `anomaly=gate_satisfied`) |

## What is still open

### P1 — TC-3 / EG-4.3: the argv backstop must prove it is dead

The launcher mints one receipt per stream-lane activation naming the lane and
the reason it was chosen. The exit criterion is read off those lines:
`lane=argv` activations with any reason other than `serve_absent` must be zero
over the window.

- Evidence surface: launcher `mission_stream_lane.dart` (the taxonomy and its
  "no unknown reason" rule), `mission_control_bridge.dart:59-82` (the
  `[MissionStream]` receipt family and why a closed gate and a missing
  advertisement are different findings).
- Gate: the window is what AUTHORISES P2. Deleting before it closes would
  remove the fallback on a hunch.
- Nothing in hermes blocks this; it is an observation, not a change.

### P2 — TC-4 / EG-4.4: delete the argv stream lane, launcher first

Only after P1 closes. Scope, in the plan's own cheapest-first order:

1. Launcher: the argv `harness stream` activation path and the separate-process
   spawn fallback (`mission_control_serve_session_io.dart`).
2. Hermes: barely anything. `hermes harness stream` (`hermes_cli/harness.py:1352-1367`,
   `hermes_cli/harness_parts/runtime_commands.py::_cmd_stream`) is ALSO the
   operator's terminal verb and the fixture generator's driver
   (`scripts/generate_agent_runtime_stream_fixtures.py`), so the CLI verb
   survives the collapse even when the launcher stops dialling it. What retires
   is the launcher's dependence on it, not the command.

**Do not delete `stream_frames`' argv entry point as part of this.** The parity
test and the byte-pinned goldens are both generated through it; removing it
would delete the fence that proves the hub lane is the same wire.

### P3 — record ruling #42's verbatim text (C-W1, partly discharged)

The plan's §0 records that ruling #42 — *finish the main path, make fallbacks
prove they are dead, delete cheapest-first* — exists nowhere on disk as
quotation. The paraphrase lives in the archived
`OFFICE_GESTURE_FOLD_PROMOTION_PLAN_2026-08-16.md` §10.4. Until the verbatim
text lands in the launcher's `DECISION_push_and_rpc_2026-08-13.md`, every
citation of `#42` is a citation of a paraphrase and must be read as one.

## What this plan explicitly does not promise

The plan's own §0 correction stands and must not be re-sold: in the normal
topology the launcher's stream lane runs as a streaming argv request INSIDE the
same serve child, and `build_snapshot()` is serialized and coalesced within one
process — so the duplicated work is "twice, sequentially", not two concurrent
disk walks. The correctness case (one producer, one batch boundary) carries the
collapse on its own; the performance framing does not.
