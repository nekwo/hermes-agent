# Persona Chat (operator channel) + Agent-to-Agent Orchestration

## Operator channel behavior

Chat is the only lane (2026-07-30). The operator channel answers like a teammate, uses
real tools when asked (permission-gated), and **never fabricates** tool output. Tool calls
are recorded redaction-safe by `ChatProgressSink` (session-keyed `run.tool.*` events) and
surface in the Trace lane via `persona_chat_trace_summary`. Quick live checks:

```powershell
# conversational reply (no tools needed)
hermes harness mission-chat message --persona neko_supervisor --message "hey, how are you" --json
# real tool call -> expect run.tool.* trace, not a fabricated "Output:" with no trace row
hermes harness mission-chat message --persona neko_supervisor --message "run: echo hello — paste the exact output" --json
```

Treat as a Harness gap (not a model quirk): decision JSON or planner/stage scaffolding
leaking into the reply; a claimed command with **no `persona_chat_trace` row**
(fabrication); or chat refusing to use a tool it has. The operator transcript is
projected redaction-safe from SessionDB (`persona_chat_history`).

### There is no heavier route

The goal/task/graph/worker escalation lane was removed. `mission_goal_create`, the
`mission_goal` toolset, `--allow-mission-goal`, and every `task` / `daemon` / `tick` /
`run` / `proof` / `blueprint` CLI verb are gone from the parser and the runtime. A turn
that "needs a real mission" does not exist as a concept any more: it is a chat turn that
does the work with its own tools, or it is a chat turn that reports the exact blocker.

If a request genuinely exceeds one turn, the shapes available are: bound the ask and send
several turns; hand part of it to another agent with `agent_chat_send`; or record the
remainder as a Mission Board card (planning state only, `harness board card add`). Do not
invent an assignment surface, and do not tell the operator one exists.

Vestigial `--task` / `--goal` flags still parse on a few verbs (`mission-chat message`,
`persona instance return-summary`, and `persona instance steer --goal`). On `steer`,
`--goal` is a **correlation id** the Launcher groups agent rooms by — not a mission goal.
The others are dead residue. Do not build workflow on any of them.

### One-off QA MCP requests: Mission Control persona chat only

A bounded QA action — one semantic Launcher probe, one screenshot — is a chat turn on a QA
instance's chat root, and nothing heavier.

**Which instance: a FRESH one per task (operator ruling, 2026-08-09).** An earlier revision
of this file said to reuse the existing on-level QA instance and not to spawn a second; that
contradicted the ruling in `operations.md` and is superseded. A new session on a shared
QA instance still collides with concurrent QA work (chat-root leases, operator UI threads on
the same instance), so place a new QA agent on the level, use it, and delete it on task
close. The create/place/message/delete recipe — including the second, separate office write
the create path does not perform — is in `operations.md`; follow it there. What stays
true from the old text is only the ROUTE and SESSION mechanics below: they apply to whichever
instance you are messaging.

Use this order:

1. Boot the normal Launcher and let Mission Control settle.
2. Read `hermes harness persona list --json` (or `snapshot --json`) and inspect
   `persona_instances` to confirm your instance is on the level. Read its exact tool detail
   with `hermes harness persona-instance detail <instance> --json`. (Reuse an existing
   instance's `default_chat_session_id` only for follow-up turns on the SAME task.)
3. Message it with explicit `--persona qa`, `--persona-instance-id`, `--session-id` (the
   root the create call returned, or the existing root for a follow-up), and a unique
   `--client-message-id`.
4. First send a no-tool identity/schema turn. The message result — not the static
   Harness preview — is the admission authority: require
   `profile_timing.mcp_admitted_servers=1`, the expected Launcher QA tool count, and
   zero calls spent.
5. Then request one non-mutating MCP semantic call. Require a real chat trace and
   `profile_timing.mcp_calls_spent=1`; a prose claim without those receipts is not
   proof. Only after this may the requested bounded action run.

The static `persona-instance detail` preview labels its entry point `harness` and may
report `mcp_not_registered_on_lane`. That describes the preview lane, not the subsequent
`mission-chat message` turn, which performs the real admission/discovery. (If the real
turn ALSO reports it, that same code covers a second cause — the venv missing the `mcp`
pip extra, i.e. no MCP client at all. Check the import first; see the MCP admission scope
note in `operations.md`.) Verified live on
`personainst_qa` / session `persona_chat_personainst_qa_469e5554a197` (2026-07-28):
identity turn `stagec-direct-route-identity-20260728-1633` admitted one MCP server and
loaded 26 Launcher QA tools; semantic turn `stagec-direct-runtime-state-20260728-1635`
spent exactly one MCP call and `mcp_launcher_qa_get_runtime_state` succeeded. Both
returned `run_ids: []`.

MCP admission is governed by **whether the profile declares the server** (operator ruling
R-1, 2026-07-29). The old role floor `R1_ADMISSIBLE_ROLES` is removed — a role is data and
never gates admission or tools.

## Canonical NEW-chat contract (what the Launcher bridge sends)

```powershell
hermes harness mission-chat message --persona <persona_id> `
  --persona-instance-id <instance> `
  --session-id "persona_chat_<instance_token>_<unique>" `
  --title "<display name> operator channel" `
  --message "<text>" --intent-hint chat --requested-by launcher `
  --client-message-id <unique> --json
```

- Omit `--session-id` and the harness mints one; `--new-session --idempotency-key <key>`
  forces a fresh server-owned root. `client_message_id` is the dedup key — reuse it on
  retry, never on a new message.
- Persona eligibility is decided by the harness ONLY (typed `unsupported_persona` /
  `invalid_request` errors; no client whitelist). The harness is the single identity
  authority: `--persona` accepts a persona id, `profile:<name>`, OR an instance id
  (`personainst_*`) — all chat entry points canonicalize at one chokepoint (hermes
  `3254b6853`). The launcher-side `canonicalMissionChatPersonaId` rescue is legacy debt
  slated for retirement, not a pattern to extend.
- The harness binds chat to its own operator-channel instance
  (`personainst_operator_<hash>`); the join back to the level agent is by `persona_id`.
  Don't chase that as an identity bug. The session appears in snapshot
  `persona_chat_history` (auto-titled) on the next refresh.
- Treat `session_id` as the stable root. Native compression may rotate
  `active_session_id`; it does not change the root Mission Control selected.

## Latency

The serve bridge **shipped and was live-verified 2026-07-08**: Mission Control app boot
spawns one persistent `hermes harness serve --ndjson` child that dispatches harness argv
requests in a warm process, plus a `_ReadModelCache` slice for `status --json` /
`snapshot --json` (20s TTL, replays stamp `served_from_cache` + `cache_age_ms`). Measured:
poll lane first poll ~2.7s (build) → repeat poll ~0.19s (cached, byte-identical), versus
~6.65s for a cold one-shot CLI call. A bare `python -m hermes_cli.main` invocation from a
terminal still pays the full ~3s import tax — that is the CLI lane, not the bridge lane.
Full baseline: `Launcher_Brain/20 — Active Initiatives/mission-control-harness-serve.md`.
The original design doc moved in the 2026-08-22 docs consolidation and is now historical
only, at `docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/harness-serve-design.md`
in the hermes repo; for the CURRENT canon read `docs/agent-runtime-harness/00-index.md` and
its `03-transport-and-wire.md` / `04-boot-and-lifecycle.md` domain docs.

## Turn identity is first-class

`client_message_id` and `turn_id` flow through history, trace, and operator conversations
(hermes `8d7b4bab8` / `ac62bbca8`); the launcher reconciles live-overlay vs snapshot lanes
by those keys in `MissionTurnEchoReconciler` — ONE place. A double-rendered turn is a
regression there; do NOT add another body-matching/heuristic dedup (7 were retired to get
here).

If a turn returns `chat_turn_outcome_unknown`, do not retry it. Resolve the exact
`(session_id, client_message_id, turn_id)` tuple with `hermes harness mission-chat
turn-resolve … --action abandon --json`, then send the text as a new turn with a fresh
client message id. `budget_exhausted` is terminal and has no turn-resolve — the turn spent
its `--max-seconds` wall budget.

## Agent-to-Agent Orchestration (`agent_chat_send`)

Any chat persona can brief ANY other persona over the canonical chat lane — "Alice,
deploy Neko on X" is the core flow. Tool: `agent_chat_send` (`tools/agent_chat_tool.py`,
toolset `agent_chat`, granted to every role). It delivers into the TARGET persona's own
Mission Control chat session (full history/trace/dedup) and returns the reply. It does
NOT assign, dispatch, or schedule anything — messaging IS the orchestration primitive,
and it is the only one.

- Operator phrasing: "brief him in chat" — the source relays with provenance ("From Tony
  via Alice: …") and reports the reply.
- When the reply being relayed, quoted, or summarized carries a MEDIA:<absolute image
  path> line (or a bare absolute screenshot path standing alone on its own line),
  reproduce it VERBATIM on a line of its own — provenance prose goes AROUND the line,
  never inside it. Full rule + rationale (the ONE canonical copy):
  `launcher-mcp-operations` SKILL.md, "Screenshot capture and delivery".
- **Chat turns have NO api-call cap** (2026-07-13): the lane matches base Hermes —
  bounded by the tool-calling loop (`max_iterations=90`) plus the wall-clock /
  shared relay deadline, not a hard call count. (Was `max_api_calls=8`, which also
  throttled the operator's own multi-step chat asks — operator chat and
  `agent_chat_send` relays share `mission_chat_reply`.) A briefing that makes the target
  do light multi-step work completes in chat; a genuinely large ask must be bounded and
  split, because there is no heavier lane to escalate into.
- The relay must appear in the SOURCE's trace lane (`agent_chat_send` rows) — a
  relayed-reply claim with no trace row is fabrication. The briefing lands in the
  TARGET's `persona_chat_history` attributed `requested_by: agent:<source session>`.
- Each relay opens a NEW target session unless the returned `session_id` is passed back
  in. Relays CHAIN (Alice → Neko → Dev). Policy authority is
  `agent_runtime/relay_policy.py`, evaluated by the mission-chat handler at the
  canonical persona chokepoint (instance-id targets cannot dodge it): depth caps at
  `HERMES_AGENT_CHAT_MAX_DEPTH` hops (default 3, clamped 1..8); relaying to a persona
  already on the chain is typed `relay_cycle`; all hops share ONE wall deadline
  (`relay_budget_exhausted` when spent). The chain travels as EXPLICIT envelope fields
  (`--relay-chain` / `--relay-deadline-epoch` on `mission-chat message`) so provenance
  survives process boundaries; success payloads carry `relay_chain` lineage and all
  typed refusals (`relay_depth_limit`/`relay_cycle`/`relay_budget_exhausted`) include
  it. `agent_chat_send` only carries the envelope — do not add guard logic there.
  `HERMES_AGENT_CHAT_SCOPE=off` disables the tool.
