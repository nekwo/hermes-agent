# Send-policy decisions — T2 / T5 / T7 (2026-08-09)

Companion to `PERF_SEND_ANALYSIS_2026-08-09.md` (launcher repo, commit
`f587e841`). T1/T3/T4/T6 shipped earlier the same day (hermes `ec1dd3498`).
This note records what happened to the three operator-approved items that
remained, including the one that is deliberately unimplemented.

Measurement rig for everything below: a real stdio MCP server advertising 60
tools, driven through `admit_mcp_servers` / `teardown_mcp_admission` in-process;
plus a schema census over the live tool registry. Both are reproducible from the
numbers quoted — no estimates.

---

## T2 — MCP admission reuse: **not built, by measurement**

Full write-up in `mission-chat-mcp-admission.md` §4. In short:

| Path | Measured |
| --- | --- |
| Warm admission, 60 tools | 6–8 ms |
| Warm teardown, 60 tools | 0.2–0.3 ms |
| Cold admission, 60 tools (spawn + handshake + `tools/list`) | 3,197 ms |

The analysis attributed a flat 2.35–3.4 s per turn to re-registering an
unchanged server set. Registration is the 6 ms half. Its three probe turns each
ran in a fresh CLI process, so all three paid a cold spawn — turn-1 numbers read
as steady state. A tool-definition cache cannot remove a spawn, so it would buy
~6 ms and add a replay path across the cross-persona isolation chokepoint.

Shipped instead: per-server `warm`/`cold` attribution on the outcome and on the
turn's `profile_timing`, because nothing persisted `mcp_admission_ms` and the
live 3.4 s was therefore unfalsifiable. Plus the isolation pin, aimed at the
reuse path that exists.

---

## T5 — chat-lane compaction default: **shipped at 150,000 tokens**

`agent_runtime.mission_chat.compaction_threshold_tokens`, default `150000`,
applied through `ContextCompressor.threshold_tokens_cap` so it can only make
compaction fire earlier. `0` disables it and is the rollback.

Why the lane needed its own number: the compressor derives its threshold from
`compaction_ratio × window`, which on this lane's 1.05 M-token model is 892,500
— a bound a chat root reaches approximately never. The longest live root reached
~200 k in 19 turns, and one tool-heavy turn on it metered ~826 k prompt tokens
across 4 provider calls where the same turn on a fresh thread meters ~50 k. The
cost is subscription **limit burn**, not dollars: that lane bills
`subscription_included` at `estimated_cost_usd: 0.0`.

Why 150,000 specifically:

1. **~7× a measured fresh-thread turn-1 prefix** (22,753 tokens for a qa turn),
   so a thread keeps 15–30 real turns of headroom. Only a thread that outlived
   its task — which the fresh-session-per-task ruling already says should have
   ended — ever reaches it.
2. **Below the 200 k root** that produced the measured 16× burn, so it engages
   on exactly the state that motivated the task. 892,500 never does.
3. **14% of the window**, far enough that one tool-heavy multi-call turn (826 k
   prompt tokens across 4 calls) cannot cross the window mid-turn.
4. **Never loosens.** It is a cap, so a 32 k-window model whose own threshold is
   ~27 k keeps it. The lane can only compact sooner, never later.

The floor (`16,000`) exists because a cap under turn-1's own prefix — of which
~9.3 k is tool schema no compaction can remove — would summarize every turn and
never converge.

The receipt was fixed with it. `context_budget.compaction_tokens` was a
`window × ratio` derivation that cannot see the cap, so it would have rendered
"compaction at 892,500" for a root the compressor compacts at 150,000. The turn
now records what the live compressor holds and the budget prefers that reading,
labelled by `compaction_basis` (`live_compressor` vs `model_ratio`).

---

## T7 — tool-schema budget on the chat lane: **nothing built, and why**

The task said building nothing was acceptable if a safe budget would amount to
re-imposing the tool restriction the operator removed on 2026-08-09
(`UNBOUNDED_DEFAULT_PLAN_2026-08-09.md`). That is the conclusion, for three
independent reasons — the first alone is decisive.

**1. The mechanism the task proposed is already enabled, and structurally cannot
reach this schema.** T7's suggestion was to "keep `tool_search` / `tool_describe`
/ `tool_call` inline and defer the long tail of full schemas behind them". That
machinery is `tools/tool_search.py`, it ships `enabled: "auto"` by default
(`hermes_cli/config_defaults.py`), and it activates the moment any deferrable
tool is present. It also has a hard design invariant, stated in its own module
header: *"Core tools defined in `toolsets._HERMES_CORE_TOOLS` are never deferred.
Always-load means always-load. No exceptions."*

A census of the live registry says that invariant covers almost all of the cost:

| | Tools | Schema bytes |
| --- | --- | --- |
| Core (never deferrable) | 62 | 93,075 |
| Non-core (deferrable) | 34 | 32,182 |

**74% of the schema bytes are core.** The bridge tools the analysis observed
sitting "alongside the full inline schemas" are not an unused facility — they
are the deferral already running, on everything it is permitted to touch. What
remains inline is what upstream has decided must always be inline. The three
largest single schemas on the lane are `computer_use` (9,647 B), `cronjob`
(7,294 B) and `terminal` (3,526 B) — all core.

**2. Deferring costs more limit burn than it saves, on the persona that has the
tools.** A deferred tool needs a discovery round trip before it can be called,
and every additional provider call on this lane re-meters the **entire** prompt:
the measured 4-call turn totalled ~826 k prompt tokens. Trading a 9,237-token
prefix — which is cache-read from turn 2 onward, so its steady-state cost is
already near zero — for one extra full-prompt call is net negative whenever the
model actually needs the deferred tool. On a QA persona whose whole job is
driving those tools, it always does. This is the same arithmetic that makes T5
worth doing and T7 not: both are about call count × prompt size, and deferral
increases the first to shrink the second.

**3. The only lever left is the one the ruling removed.** With deferral already
maxed out, the remaining ways to shrink the lane's schema are to narrow the
toolset per persona or to strip core tools from the harness lane. Both are
per-persona tool restriction wearing a different name, and an agent cannot call
a tool whose schema it never saw. That is precisely the capability regression
the task said to refuse.

What the measurement does leave on the table, recorded here rather than acted
on: three core schemas (`computer_use`, `cronjob`, `kanban_create`) account for
~22 KB between them. Shrinking a verbose core schema is not a capability
restriction and would be a legitimate upstream contribution — but it is upstream
core surface, not a harness-lane policy, and it is not what T7 asked for.
