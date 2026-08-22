# RunBudget — one accounting authority for a run's bounds (2026-07-26)

Status: **shipped** (`agent_runtime/run_budget.py`, wave 4). Structure refactor —
**no behavior change**. Every mechanism's trip semantics are preserved exactly;
what is new is that all of them account into one readable block.

Sibling docs: `turn-durability-design.md` (the wall checkpoint and the
`budget_exhausted` terminal state), `mission-chat-mcp-admission.md` §3/§7 (the
per-run MCP call budget), `12-read-path-freshness-hardening.md` ("make the
violation impossible or self-announcing").

---

## 1. What was weak

A persona run is bounded by four independent mechanisms, living in two modules,
each keeping its own private bookkeeping:

| mechanism | site | expression |
| --- | --- | --- |
| read/search loop bounds | `profile_runner._ToolBudgetGuard` | **raises** `RunBudgetExceeded` |
| graceful wall checkpoint | `profile_runner.WallBudgetCheckpoint` | **lands the turn** (steer + iteration drain, final reply, `budget_exhausted`) |
| hard wall timer | `profile_runner._execute_agent_run` | interrupts, then **raises** |
| per-run admitted MCP calls | `mcp_admission.McpCallBudget` | **refuses the call**, turn continues |
| post-run api-call / token bounds | `profile_runner._enforce_result_budgets` | **raises** |

Three different trip semantics is not the defect — each is deliberate. The
defect was the bookkeeping:

- **No single answer to "what bounded this turn?"** An operator had to know
  which of four mechanisms to go looking for, in two modules, on two different
  surfaces (an exception message, a progress event).
- **Free-form trip reasons.** The typed half of a trip existed only as an
  f-string assembled at the raise site, so any downstream reader that wanted to
  branch on *which* bound fired had to match on a message.
- **A tripped budget was invisible after the fact.** Two of the five wrote flat
  ints into `profile_timing` (`mcp_calls_spent`, `mcp_call_budget`); the rest
  wrote nothing. On the raised path there is no result at all, so the accounting
  vanished with the exception.
- **No headroom.** Nothing recorded a budget's limit *and* consumption when it
  did **not** trip, so "the turn stopped at 6/6 read/search calls" and "the turn
  used 2 of 6" were indistinguishable from the run record.

## 2. The shape

`agent_runtime/run_budget.py` is a **pure value-object ledger**. It holds no
policy, makes no decision, enforces nothing, and never raises. Each mechanism
still decides on its own terms, at exactly the site it decided before, and then
*declares* itself here.

```
RunBudgetKind         wall | read_search | api_calls | total_tokens | mcp_calls
RunBudgetEnforcement  trips_run | lands_turn | refuses_call   (+ .severity)
RunBudgetTripReason   repeated_read_search_loop
                      aggregate_read_search_exceeded
                      wall_checkpoint_engaged
                      wall_clock_exceeded
                      api_calls_exceeded
                      total_tokens_exceeded
                      mcp_calls_exhausted
```

`RunBudgetLedger` — thread-safe (the wall records from a timer thread, MCP from
whichever thread dispatched a tool), with three verbs: `declare` (this run IS
bounded by X, limit L), `observe` / `consumed_provider` (how much of it went),
`trip` (it was exhausted, typed reason, and what that did).

Two rules worth knowing:

- **`consumed_provider` over a copied count.** A mechanism that already owns a
  live counter (elapsed wall, the MCP meter, the guard's aggregate) is *read* at
  accounting time. One authority per number; no second tally to drift from the
  one the enforcement itself used.
- **Only declared budgets appear.** A run with no `max_wall_seconds` gets no
  wall row — the runner builds a stand-in checkpoint with a 115-year deadline so
  the tool-start gate has one unconditional shape, and accounting that as a
  "limit" would report a bound nobody has.

## 3. The accounting block

Rendered into `AgentRunResult.profile_timing["run_budget"]`, and — because a
tripped run never returns a result — also onto `RunBudgetExceeded.run_budget`.

```json
{
  "bounded_by": "wall",
  "trip_reason": "wall_clock_exceeded",
  "enforcement": "trips_run",
  "tripped": ["mcp_calls", "wall"],
  "budgets": [
    {"kind": "wall", "enforcement": "trips_run", "unit": "seconds",
     "limit": 540, "consumed": 540, "remaining": 0, "tripped": true,
     "trip_reason": "wall_clock_exceeded", "detail": "wall_seconds=540"},
    {"kind": "mcp_calls", "enforcement": "refuses_call", "unit": "calls",
     "limit": 120, "consumed": 120, "remaining": 0, "tripped": true,
     "trip_reason": "mcp_calls_exhausted", "detail": "mcp_admission_budget_exhausted"},
    {"kind": "read_search", "enforcement": "trips_run", "unit": "calls",
     "limit": 6, "consumed": 2, "remaining": 4, "tripped": false,
     "trip_reason": null}
  ]
}
```

**`bounded_by` is the most TERMINAL tripped budget, first-trip order breaking
ties** — not simply the first. A refused MCP call did not bound the turn, it
bounded one call; a run that refused a call at t=10 and then hit its wall at
t=540 was bounded by the wall. Ordering alone would answer `mcp_calls` there,
which reads as the opposite of what happened.

**The wall may ESCALATE.** Its graceful checkpoint can open (`lands_turn`) and
its hard wall then fire anyway (`trips_run`). Both are true; the row reports the
worse one. De-escalation is refused.

Placement notes for readers of `profile_timing`: every pre-existing key is an
`_ms` / `_count` integer and is untouched. `run_budget` is the one structured
entry. Existing consumers either copy the dict wholesale
(`node_tools`, `root_node_engine`, `persona_commands`) or filter to
`_ms`/`_count` keys (`persona_runtime._record_timing_value`), so it is additive
for all of them; no launcher change is required (the launcher does not read
`profile_timing` at all).

**On the run record (2026-07-27).** That scalar filter meant the block reached
the CLI envelope (which copies `profile_timing` wholesale) but was silently
dropped from `AgentRun.llm`, so the run record kept no answer to "what bounded
this run?". `persona_runtime._apply_llm_metadata` now lifts it onto
`run.llm["run_budget"]` as its own key — the `timing` map keeps its integer
contract, and nothing has to smuggle a nested dict through a filter written for
scalars. A later result without a block does not erase a recorded one (the same
carry-forward the timing map already had), and a run that declared no budget
records no key at all. Pinned in `tests/agent_runtime/test_run_budget.py`
(§"The block reaches the RUN RECORD").

**On the mission-chat TURN record (2026-07-27, operator ruling).** A pure chat
turn produces no `AgentRun` at all — `runs/` is the goal/task lane — so the
paragraph above never reaches it: the block rode the live envelope and then
evaporated, and the cockpit had nowhere to read "what bounded this turn?" once
the turn settled. **The mission-chat turn journal IS the chat lane's run
record**, so the block is persisted there, under the same key and in the same
verbatim shape:

- **Key** — `run_budget`, **top-level** on the turn record
  (`agent_runtime/mission_chat_turns.py`, `_JOURNAL_RUN_BUDGET_FIELD`). Not
  nested under the wall provenance beside it: `budget_trigger` /
  `budget_summary` describe the wall checkpoint specifically; this describes
  every bound the turn had.
- **Schema version** — unchanged at `2`. This store versions its record
  *shape*, and every optional additive key so far (`started_at`,
  `budget_exhausted`, `budget_trigger`, `budget_summary`) landed at 2 with
  absence as the signal.
- **Settle points** — the same ones that already write the wall provenance, in
  `hermes_cli/harness_parts/persona_commands.py::_cmd_mission_chat_message`:
  the `native_committed` transition on a completed turn (**unconditional** — an
  untripped turn's headroom is exactly what makes "stopped at the bound"
  distinguishable from "finished with room to spare"), the `budget_exhausted`
  transition on a wall trip, and `outcome_unknown`, where a non-wall trip
  (read/search, api calls, tokens) settles. All three go through one adapter,
  `run_budget.turn_run_budget_metadata`, so no settle site re-reads the block's
  shape for itself.
- **Absent semantics** — absent stays absent. No block from either source ⇒ no
  key, never `{}`. A real run that declared no budget still records its (empty)
  ledger: "accounted, nothing bounded" and "written before any of this existed"
  are different facts.
- **Projection** — the chat-history rows are an explicit allowlist, so the key
  does **not** ride through on its own. `persona_chat_history` carries it
  additively onto both shapes a turn can project as: the agent reply row (beside
  `turn_elements`) and the terminal marker row a reply-less budget-exhausted
  turn gets instead. Read-only / emit-path: the projection reads the journal and
  never writes it (pinned). The same pass folded the per-row journal reads into
  one read per page, so the key costs no extra I/O.
- **One reader** — `run_budget.safe_accounting_block` is now the single bounding
  reader at every persistence boundary; `persona_runtime._safe_run_budget_block`
  delegates to it instead of keeping a second copy of the same logic.

Pinned in `tests/agent_runtime/test_mission_chat_turn_run_budget.py`.

## 4. What deliberately did NOT change

Config keys, defaults, clamps, exception types, exception message strings,
envelope shapes, the checkpoint's reserve math, the order the post-run budgets
are enforced in, and the serve-cwd `_WORKDIR_LOCK` regions. No new config
surface. `mcp_admission.py` is unmodified: the runner reads its meter and hooks
its existing `on_budget_exhausted` callback, so the MCP budget has no second
authority.

Enforcement wiring stays where it is, on purpose. Moving the timers, the
tool-start gate, the registry handler swap or the reserve math into
`run_budget.py` would fold three intentionally different trip semantics into
one — the opposite of the goal. This is the accounting seam, not a scheduler.

## 5. Tests

`tests/agent_runtime/test_run_budget.py` — the transition table: each mechanism
× {untripped, tripped}, pinned against the observable behavior it had before
(same exception type, same message text, same `wall_budget_checkpoint` envelope
on the landed turn, same typed refusal payload on the refused call, same
progress events), plus the new accounting assertions. Nothing spawns an MCP
server or connects a transport; the one wall-checkpoint run replaces
`turn_budget.checkpoint_reserve_seconds` (the function, never its math) so the
graceful window is reachable inside a second.

## 6. Follow-ups

- ~~Link this doc from `00-index.md` under Operator forensics (left out of the
  landing commit to avoid colliding with parallel wave-4 edits to the index).~~
  **DONE** — `00-index.md` links it under the mission-chat lane docs.
- ~~The block is not yet rendered on any operator surface~~ — the run-record row
  landed in the launcher (Mission Control `mission_run_budget_row.dart`), and as
  of 2026-07-27 the chat lane has its own home on the turn record (§3) for the
  cockpit's chat surface to render from.
