# Planned — widen correlation-id coverage to the remaining write lanes

**Status: the mechanism shipped; coverage is partial by design and the last
stages are open.** Source plan:
`../archive/2026-08-22-pre-consolidation/CORRELATION_ID_PLAN_2026-08-16.md`
(Plan D / EG-2.3, stages CI-0…CI-4). Live wire truth lives in
`../03-transport-and-wire.md` — this file holds only what has NOT landed.

## What already landed (do not re-plan it)

| Stage | Evidence |
|---|---|
| CI-0 — neutrality pinned before any producer changed | `tests/agent_runtime/test_correlation_id.py` (delivered-behaviour tests at `:137`, `:174`; the absent-when-unset golden at `:206`) |
| The payload-side fence | `agent_runtime/state_patches.py::normalize_correlation_id` (`:199`), `CORRELATION_ID_MAX_LEN = 64` (`:189`), `_CORRELATION_ID_RE` (`:196`) |
| The RPC boundary refusal (loud, never sanitized) | `agent_runtime/serve_rpc.py::_correlation_id_param` (`:379`), `CORRELATION_ID_INVALID_REASON` (`:368`) |
| CI-1a/b — the office write path, domain event AND paired patch | `agent_runtime/office_store.py::_emit` (`:173`) threads it through `upsert_actor` / `update_surface` / archive; `agent_runtime/state_patches.py::emit_state_patch` (`:465`) normalizes and attaches |
| CI-2 — launcher mints, sends, and prints | mint: `mintMissionCorrelationId` (defined `lib/features/mission_control/office/mission_office_correlation.dart:72`), called at `office/mission_office_layout_controller.dart:1289`; sent on four RPCs in `office/mission_office_rpc.dart` (`:960`, `:1179`, `:1452`, `:1722`); fold receipt at `mission_control_bridge.dart:2363-2371` |
| CI-4 first half — `runtime.agent.create` | `agent_runtime/agent_create.py:470-487`, echoed at `:1153`; argv `--correlation-id` at `hermes_cli/harness.py:1329` threaded at `hermes_cli/harness_parts/persona_commands.py:494` |

## What is still open

### P1 — CI-3: the acceptance grep, run against a real build

The plan's acceptance is literally one grep: perform one delete and one drag,
grep the diag log and the serve service log for the minted token, and require
the output to contain, in order — launcher send, serve write receipt, patch-row
fold receipt (or a demote receipt naming the id among contributors).

- Not present in `tests/`; there is no scripted acceptance for the cross-process
  join today. `test_correlation_id.py` pins the hermes half only.
- Gate: it must run against the operator's own build reading logs only — no
  `.hermes/` writes, no casual `harness serve`.
- Until it exists, the sanctioned diagnostic procedure it was meant to REPLACE
  (timestamp anchoring) is still what a reader falls back to, which is the exact
  failure the plan was written against.

### P2 — CI-4 second half: the argv capability lanes

`--correlation-id` reaches THREE argv verbs as of 2026-08-27 — `harness agent
create`, `harness agent retire` (S8b, `d107d132e0`) and `harness persona instance
retire` (S8b-b) — which is the whole placement gesture, create half and delete
half, on both of the retire's doors. The remaining capability lanes — persona
open-chat, steer and their siblings — carry no token, so a gesture that goes
through THOSE is uncorrelated while an office gesture in the same session is
correlated. The window is narrower than this plan found it, not closed.

**The retire is the worked example of why the last door matters.** S8b gave the
flag to `agent retire` and withheld it from `persona instance retire`, reasoning
that no gesture stood behind the second door. The launcher's argv fallback IS
that door and always had a token, so the one retire that ran on a degraded
transport was the one an operator could not join — the partial-coverage window
biting hardest exactly where the diagnostic was needed most. Whoever takes the
remaining lanes should check each door's real callers before deciding one has no
gesture behind it.

- The plan names this the **partial-coverage window** and sequences it LAST on
  purpose: argv surface changes fan wide.
- Threading pattern is fixed by the shipped half: an optional argv flag →
  optional param → optional kwarg on the chokepoint → attached to the payloads
  the chokepoint already emits. Nothing on the wire changes.

### P3 — D-D1 stays deferred, deliberately

Registering `correlation_id` as a contract detail field in
`agent_runtime/decision_contract_registry.py` is **refused, not pending**. The
hash derives from the registry rows alone, so registering would move
`decision_contract_hash` inside every generated core and force a cross-stack
fixture landing for zero diagnostic gain. It stays an unregistered optional
payload key. Anyone tempted to "tidy it up" into the registry is re-opening a
trap this plan already closed twice.

## What this plan cannot answer

A demoted full core reflects N gestures at once. The frame's `events[]` names
every contributing id, but the rebuilt core has no per-field attribution and
this plan does not pretend otherwise. Uninstigated changes (agent turns,
watchdog reconciles) have no gesture and carry no id, honestly.
