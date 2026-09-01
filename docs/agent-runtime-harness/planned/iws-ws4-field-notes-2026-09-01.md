# Field notes — IWS WS4, hermes half (2026-09-01)

Running record for lane C of the instant-workspace-switching wave. Authority:
`EterniaLauncher/docs/mission_control/planned/instant-workspace-switching.md`
§1.4 / stage WS4 / rulings R-W0, R-W1; hermes pointer
`planned/instant-workspace-switching.md`.

## 0. Baseline and worktree

- Baseline given by the orchestrator: hermes `c894c2b159` (NOT the dispatch
  doc's `9d4e94bf04` — `c894c2b159` is two commits later and carries the hermes
  pointer doc itself). Verified `c894c2b159` is a descendant of `9d4e94bf04`.
  Same for the launcher: `44a1cb77e` descends from the dispatch doc's
  `6cab38882`. **Deviation from the dispatch doc, recorded, and it is the
  orchestrator's more recent instruction that wins.**
- Worktree: `X:/Eternia/hermes-agent-ws4`, branch `ws4-workspace-rpc`. No
  state-changing git in the primary (a concurrent session holds the shared
  index).

## 1. Survey — what the plan said, and what the code says

### 1.1 The accept semantics live in two CLI handlers, and nothing else calls them

`hermes_cli/harness.py` at baseline:

| symbol | line | what it owns |
|---|---|---|
| `_cmd_workspace_use` | :3166 | `set_active` + the applied/declined split |
| `_activation_outcome_row` | :3183 | the declined row (`superseded` / `duplicate`) |
| `_cmd_realm_use` | :3358 | `set_active` + reconcile + the same split |
| `_reconcile_active_workspace_to_realm` | :3412 | the realm switch's workspace fallback |
| `_workspace_row` / `_realm_row` | :2984 / :3298 | the row both answers render |

No other module in the repo references any of them (grepped `hermes_cli/`,
`agent_runtime/`). So the move is a pure relocation, not a widening.

### 1.2 CONFIRMED — `PEER_METHOD_ALLOWLIST` covers the new methods with zero edits

`tests/agent_runtime/test_peer_authorization.py:85-86` builds its expectation by
iterating the LIVE registry (`set(registry) - PEER_METHOD_ALLOWLIST`), so the
two new names join the refused set automatically. Re-verified by running the
suite after registration — see §4. The plan's claim here is TRUE at baseline.

### 1.3 CORRECTION — "the device arm is an equality, so device/peer callers get
the typed tier refusal" is only HALF true, and the half that is false is the
one R-B needs

The plan (§1.4) and the hermes pointer both argue that declaring the methods
`console` is enough, because `call_authorization.authorize_call`'s device arm is
an equality against the stored tier word. Re-measured against the code:

- a device paired at `read` → equality fails → `scope_denied`. Correct.
- a device paired at **`console`** → `held == normalized` → **ALLOWED**.

And paired console devices are not hypothetical: `runtime.chat.message`'s own
docstring (`serve_rpc.py:2449-2464`) declares `console` *precisely so* that
R11's "a paired console device may chat" holds. So on the plan's literal
construction, a remote cockpit on a console-tier device could park the desktop
operator's scope pointer — the exact thing R-B says it must not be able to do,
and the sentence WS4 is supposed to make true by construction.

**Fixed rather than papered over.** `call_authorization.LOCAL_CONSOLE_METHODS`
is a new membership set beside `PEER_METHOD_ALLOWLIST`, evaluated in
`authorize_call` BEFORE every other arm (the position is load-bearing: placed
after the device arm it is unreachable for the exact caller it exists to
refuse). The tier still says what STRENGTH the verb wants; the set says the
authority is a KIND. Refusal shape is unchanged — the same
`CallAuthorization.refusal_data()` the launcher's decoders already branch on.

Enforcement is at the chokepoint and NOWHERE else: the two handlers contain no
authorization code, so `serve_rpc` keeps rendering refusals rather than
authoring them (its own module docstring's argument).

### 1.4 The manifest integer does NOT move — checked, not assumed

`serve_rpc.RPC_CONTRACT_VERSION` is documented "bump ONLY when an existing
method's request or result shape changes incompatibly; adding a method does not
move it" (`:142-144`), and `runtime.persona.prewarm`'s docstring restates it.
Adding two methods grows the SET only. The launcher's greeting-frame fixtures
still move, because they pin the literal method list.

## 2. What landed (hermes)

- **`agent_runtime/scope_activation.py`** (new) — the one implementation:
  `activate_workspace` / `activate_realm` (decision + reconcile + row),
  `activation_outcome_row` and `reconcile_active_workspace_to_realm` (moved
  verbatim from `harness.py`), `workspace_row` / `realm_row` (moved verbatim —
  see below), and `perform_scope_activation`, the method lane's shim.
- **`hermes_cli/harness.py`** — `_cmd_workspace_use` / `_cmd_realm_use` reduced
  to "call the shared function, print the envelope". The four moved symbols are
  imported back under their old private names, so all ten existing CLI call
  sites and both characterization suites are untouched.
- **`agent_runtime/serve_rpc.py`** — `runtime.workspace.use` /
  `runtime.realm.use` at `TIER_CONSOLE`.
- **`agent_runtime/call_authorization.py`** — `LOCAL_CONSOLE_METHODS` + the arm.

### Why the ROW builders moved too (a decision, recorded)

The alternative was to leave `_workspace_row` in the CLI and let the method lane
render something smaller. Rejected: "the same accept semantics as the argv verb"
is a claim about the ANSWER as much as the decision, and a method whose result
had its own shape would be a second contract nobody ruled on. They are re-keys
of `agent_runtime.snapshot`'s summary builders (S48 ledger item 4), so
agent_runtime is where they belonged anyway. The S48 pin
(`test_s48_cli_entity_row_consolidation.py`) resolves them through
`getattr(harness, name)` + `inspect.getsource`, so it follows the move with no
edit — verified green.

### The declined arms answer as a RESULT, not an error

`superseded` / `duplicate` exit 0 on the argv lane. Rendering them as JSON-RPC
errors would make the launcher's accept path treat a correctly-ordered switch as
a failure and raise the R-A parked-elsewhere surface for something that worked.
Only two refusals exist, and both are the argv lane's own failures: a missing id
(`-32602`) and an unknown one (`4001`).

### Serialization

The row carries a `datetime` (`updated_at`). The argv lane serializes through
Stage-42's printer (`emit_json` → `to_jsonable`); the method lane calls
`to_jsonable` in `perform_scope_activation`, so both doors render one row
through one serializer. Without it the method would answer a frame `json.dumps`
refuses — caught by the acceptance run, not by reasoning.

## 3. Suites and receipts

All with the system `python -m pytest`.

| suite | result |
|---|---|
| `test_scope_use_methods.py` (new) | 28 passed |
| `test_scope_patch_coverage.py` (lane A's, unedited) | 13 passed against this refactor |
| `test_scope_use_serve_acceptance.py` (new) | 5 passed |
| authorization + gateway + office-rpc + row-consolidation focused set | 151 passed |
| `tests/agent_runtime` + `tests/hermes_cli` | see the closing report's count |
| mutation gate (`--base c894c2b159 --max-candidates 40`) | 6 candidates, **6 KILLED, 0 survived** |

The mutation gate ran in its OWN worktree (`hermes-agent-ws4-mut`, detached at
the branch head, removed afterwards) — it rewrites source in place and must
never share a tree with a pytest run.

`tests/acp/*` and `tests/test_run_tests_parallel.py` fail to COLLECT in a
worktree (`ModuleNotFoundError: No module named 'acp'` — the editable install
resolves against the primary checkout). Pre-existing environment condition,
nothing to do with WS4; the `agent_runtime` + `hermes_cli` roots cover
everything this lane touched.

### The acceptance transcript (real serve, both listeners, real paired device)

```
ready.rpc.tiers.scope: {"runtime.realm.use": "console", "runtime.workspace.use": "console"}
local_console applied:   result {... "applied": true}
local_console duplicate: result {... "applied": false, "reason": "duplicate"}
device[read]    hello: "hello_ok"
device[read]    refusal: error -32000 data {"reason":"scope_denied","tier":"console","caller":"device"}
device[console] hello: "hello_ok"
device[console] refusal: error -32000 data {"reason":"scope_denied","tier":"console","caller":"device"}
pointer after device attempts: ws_transcript-a_…   (unmoved — the device wrote nothing)
```

The `device[console]` line is the one the stage exists for. The first
transcript also exposed a wording bug: the dispatcher's generic sentence told a
console-tier device it *"requires the console tier"*, which it holds.
`CallAuthorization` now carries an optional `detail` the policy sets and the
dispatcher renders; the wire contract (`reason` / `tier` / `caller`) is
untouched, because that is what the launcher's decoders branch on.

### The cross-lane property, asserted rather than assumed

WS1 emits the `scope` patch from INSIDE `WorkspaceStore.set_active` — the store
chokepoint — and this lane's shared implementation goes through that same call.
So a switch carried by `runtime.workspace.use` produces the same patch a switch
carried by argv produces, with no WS4 code knowing the patch exists. That is
now a test (`test_the_method_lane_inherits_WS1s_scope_patch_for_free`) rather
than a coincidence: had either lane emitted from its own handler instead, this
is the assertion that would have caught it. Lane A's own suite also runs green
unedited against the refactor.

## 4. Landing mechanics

Rebased onto lane A's hermes head `cf9abaac4b` (WS1+WS2) on the orchestrator's
instruction. ONE conflict: `tests/mutation_claims.json`, where both lanes
appended to the same array tail. Resolved as a **union** — lane A's four
`iws-ws1-*` claims and this lane's six `ws4-*` claims all present, 178 total —
never a pick. Every affected suite re-run green after the rebase.

## 5. Open / owed

- WS4's measured acceptance (`gesture_to_accept` p50) is an OPERATOR span
  capture (WS0's instrument), not something this lane can produce headless.
- **A measurement caveat worth filing:** TERM 2 in the plan's cost model calls
  the argv accept "a Python subprocess per gesture (~1-2 s)". The launcher
  already routes argv through `runMissionControlCommandPreferServe`, i.e. over
  the serve socket when a serve child exists, so the spawn is hermes-side
  (serve's argv lane) rather than launcher-side. The RPC method still removes a
  whole argv dispatch, but the "process spawn per gesture" framing is not what
  the launcher does when a serve session is up. Recorded for WS0's span table
  to settle.
