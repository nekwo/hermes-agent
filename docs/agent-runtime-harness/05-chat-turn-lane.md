# 05 — The chat turn lane: one turn, end to end

The goal/task mission lane was removed 2026-07-30. What remains is the chat lane, and it is the
whole operating surface: an operator (or a relaying agent) sends one message to one persona
instance, and a bounded, receipted, durable turn runs. This walks that turn in the order it happens.
Every claim cites the file and line holding it at HEAD; anything the code could not be made to say
sits under `## Open rows`, `## Unverified carry-forward`, or is gone. The handler is
`_cmd_mission_chat_message` in `hermes_cli/harness_parts/persona_commands.py` — a command part
`exec`-loaded into `harness.py`'s globals, which is why most of the turn's logic lives in importable
`agent_runtime/` modules it calls.

## 1. Send admission — the turn's identity and its thread

**One id, minted launcher-side, echoed byte-equal.** The launcher mints `agent-chat-send-<uuid4>` as
the intent's `idempotencyKey` (`mission_agent_chat_panel.dart:1870`), sends it as the RPC's
`client_message_id` (`mission_agent_chat_adapter.dart:1485`), and hermes echoes it as `turn_id`
(`persona_commands.py:3111`, `:3145`) after reading it at `:2189-2195`. Absent, hermes mints
`agent-chat-send-<hex12>` and writes it back onto `args` so the serve lane and the turn store agree.
The launcher's timeline names this the join key in its own docstring
(`mission_chat_turn_timeline.dart:157-161`): one key, minted once, so the cross-process join is
never a time-proximity guess.

**Explicit `session_id`** (new-chat and per-chat sends) runs two SessionDB guards, both
`REJECTED`/exit 2:

- `unknown_chat_session` (`persona_commands.py:2236-2251`) — the named root is not in the canonical
  SessionDB. `next_expected`: *open a server-minted chat root before sending*.
- `foreign_chat_session` (`:2252-2280`) — the root exists but is not owned by the target: no owner,
  no owner row, a differing persona, or a pinned `persona_instance_id` that is not the owner. On
  success the turn ADOPTS the owner as its instance identity (`:2280`), so the bind below cannot
  receive a different one. A clarify-token continuation resolves BEFORE this point (`:2147-2157`)
  precisely so a ticket-supplied session flows through these guards, not around them.

**Omitted `session_id`** (the dispatch lane, and any first-turn open) never reaches them. It
resolves a target — explicit pin, else the single in-scope placement, else the canonical channel
(`:2312-2320`) — then asks `resolve_dispatch_session_decision` whether to continue or mint
(`:2331-2334`). Default policy is `new_per_dispatch`; the console's argparse `--new-session` is
`store_true`, so an operator send arrives as explicit `False` and continues the instance's current
default thread.

**The pre-mint gate.** The mint is the turn's first durable side effect: a titled session row, plus
a repoint of the instance's default-thread pointer once `open_chat` binds it. So every refusal
decidable from `args` alone, plus a read-only retired-target pre-flight, is evaluated first
(`:2368-2379`). The mint is idempotent through `PersonaChatMintReceiptStore().mint` (`:2397-2412`),
keyed on the caller's `idempotency_key` or `send:<client_message_id>`. Two typed refusals escape it:
`RetiredPersonaInstanceError` (`:2413-2428`) and `PersonaChatPersistenceError` (`:2429+`, the
bind-landed/transcript-didn't case, which retracts the bind rather than returning a root that
dereferences nowhere). An ambiguous BARE persona target — more than one live instance, no pin, no
session — is refused before all of this with the candidate `@handles` (`:2210-2234`).

## 2. The turn phases contract

`agent_runtime/mission_chat_phases.py` is the turn's monotonic timeline; records carry it under
`phases` at schema **v3** (`:61`, `:70`). A v2 record has no `phases` key and is never migrated —
the bump is how a reader tells "predates instrumentation" from "instrumented and never got there".
`PHASE_ORDER` (`:76-89`):

```
request_received → context_built → observability_built → emitter_created →
write_ahead → agent_ready → provider_request_started → request_assembled →
provider_first_byte → stream_done → native_committed → projected
```

`request_assembled` landed 2026-08-22 (`785a35beae`) and splits the old "provider" span:
`provider_request_started → request_assembled` is hermes assembly, `request_assembled →
provider_first_byte` is client init + network + provider (`:352-375`). Beside the marks ride one
flag (`agent_init_cold`, `:92`) and two counters (`registry_probe_rounds`, `builds_overlapped`,
`:96`); `_BLOCK_ORDER` (`:106-122`) is the closed set a reader may see.

**Four honesty rules** (`:18-50`), each enforced in code, not by convention:

1. **Absent, never a fake zero.** `snapshot()` emits only what was marked (`:321-342`);
   `safe_turn_phases` drops what it cannot read and never defaults (`:387-422`); `flag()`/`count()`
   treat `None` as "could not establish" and record nothing (`:242-279`) — which matters most for
   the counters, where `0` is a real and interesting answer.
2. **Monotonic only.** Anchor and marks come from one injected callable (`:170-179`); `anchored_at`
   is the single wall stamp, for eyeballing `agent.log` (`:192-196`).
3. **First mark wins**, under a per-phase lock inside `mark()` (`:221-240`) — `provider_first_byte`
   rides the emitter's per-token `delta()` and may arrive on a worker thread.
4. **Release-visible.** No flag, no debug gate; the block rides persists the turn already performs.

Construction IS the anchor, taken as the handler's first statement (`persona_commands.py:2003`),
ahead of the capability bind and the config load, because everything below is admission cost the
operator waits through. Marks land at `:3094`, `:3133`, `:3161`, `:3202`, `:3217`, `:3230`
(write_ahead — deliberately *before* the write it names), `:3385`, `:3597`, `:3800` and `:4950`
(provider_first_byte, on the emitter); `request_assembled` arrives as a trace payload, converted at
`:3171-3181`. The emitter's older `ttft_ms` is unchanged; `phases` is a superset, because the
emitter is built ~1,100 lines in and its clock cannot see the profile bootstrap.

## 3. Model selection

Four tiers, highest wins, resolved once in `_chat_effective_model_payload`
(`persona_commands.py:5752-5790`):

```
chat-session override  >  instance override  >  persona default  >  config default
```

The chat-session override persists under `mission_control_chat_model_override`
(`persona_commands.py:5284`, `agent_runtime/persona_chat_history.py:226`) via
`_resolve_chat_model_override` (`:5738-5749`), called at `:2734`. Its scope is literally
`mission_control_chat_session` (`:5788`, inside `_chat_effective_model_payload`) — per-thread,
not per-instance. Values validate against
`^[A-Za-z0-9_.:/@+-]{1,200}$` (`:5285`); a violation is a typed refusal, a persist failure is
`CHAT_MODEL_OVERRIDE_PERSIST_FAILED` (`:2745-2760`). The payload reports every tier separately
rather than folding them, so `model_is_default` and `model_is_instance_override` are answerable from
one record. Chat-lane compaction has its own cap:
`agent_runtime.mission_chat.compaction_threshold_tokens`, default **150,000**
(`agent_runtime/runtime_config.py:270`), applied so it can only make compaction fire *earlier* than
the compressor's own `ratio × window` derivation.

## 4. Tool access posture — unbounded by default, restriction by exception

The 2026-08-09 ruling inverted this lane. `agent_runtime/tool_permissions.py` answers two questions
that used to be one (`:1-21`). **Standing posture**: `default_permission_mode()` (`:221-251`) reads
`agent_runtime.tool_permissions.default_mode` from the ROOT config, shipping as `unbounded`
(`permission_modes.py:69`). It loads through `load_root_runtime_config` so a sticky-active profile
cannot widen the runtime, and any config fault resolves to the *bounded* fallback
(`permission_modes.py:75`) — the shipped default is wide, but nothing the runtime failed to read
hands out capability. **Did the operator narrow THIS session?**: the `ChatToolPermissionStore`
record (`:79-130`), which survives the ruling **inverted** — once the escalation lane, now the
RESTRICTION lane. A stored `profile_default` is the no-opinion sentinel the expiry writeback has
written for months and is explicitly not read as a pin (`:269-282`); reading it as one would have
frozen every session that ever held a lapsed grant into the old bounded tier. An unrecognised stored
mode clamps to `bounded`, not to the wide default (`:91-103`).

Every consumer reads one answer, from `permission_options_for_chat` (`:285-301`). What unbounded
does on the lane (`agent_runtime/persona_runtime.py`):

- `_blocked_tool_names_for_chat` returns `[]` outright (`:583-586`), so the pre-ruling
  `PERSONA_BLOCKED_TOOLS` set (`personas.py:111-118`: `delegate_task`, `clarify`, `memory`,
  `send_message`, `cronjob`) does not apply to the default posture. Registry-hygiene names are still
  unioned at agent construction on every lane — hygiene is junk removal, not a permission tier
  (`:594-597`).
- `_enabled_toolsets_for_chat` (`:639-660`) runs permission mode → toolset resolution → chat
  capability augmentation → the T3/T6a cost policy → the MCP admission scope, and `unbounded`
  bypasses the cost policy. `file` / `terminal` / `code_execution` / `browser` / `vision` are
  therefore present on a default turn; a *bounded* persona restores them via
  `agent_runtime.personas.<id>.chat_lane_restore_toolsets`.
- `agent_chat`, `board` and `clarify` are unconditional chat capabilities
  (`_CHAT_CAPABILITY_TOOLSETS`, `:902`) regardless of the persona's configured list; `clarify` is
  additionally un-blocked by name on the bounded lane (`:603`), which has a clarify bridge.

`apply_chat_lane_tool_scope` (`:834-885`) is the display-parity door: it threads the REAL chat-lane
resolution onto the operator preview, so `persona tool-diff` stops reporting the persona's raw
configured set (under unbounded it sets `configured_toolsets = all_registered_toolsets()`,
`:867-868`), and carries the typed account of what the scoping removed
(`chat_lane_capability_drops`, `:688`) — survivors alone were never an account of removals.

## 5. MCP admission — the profile declares the server

`agent_runtime/mcp_admission.py` turns the lane's honest refusal into a per-run, per-persona
admission. The invariants it holds (`:19-63`):

1. **The lane blanket never flips.** `discover_mcp_tools()` is never called from here; admission is
   `register_mcp_servers({name: cfg})` over an explicitly resolved subset.
2. **The profile declaration is the admission authority.** `_requested_servers` =
   `declared_mcp_server_names` ∪ `_effective_required_mcp_servers` (`:786-812`). **There is no role
   lane.** S64 made declaration the sole authority; S66 removed the residual `task`/`stage`
   parameters and the "role-admitted" wire text (`:130-132`, `:707-712`, `:789-791`). The `role` on
   the decision record (`:352`, `:382`) is REPORTING only.
3. **`unbounded` never crosses the declared set.** `scope_toolsets_to_admission` (`:956`) runs AFTER
   permission-mode resolution and strips every `mcp-*` toolset this run was not admitted — necessary
   because `all_registered_toolsets()` in a multi-persona serve process would otherwise include
   another persona's admitted surface.
4. **Single-flight and bounded in time.** An interleaved second admission is refused as
   `mcp_admission_lane_busy` (`:123`); an over-budget registration degrades to
   `mcp_admission_timeout` (`:124`) and the turn continues without those tools.
5. **Bounded in CALLS too.** `McpCallBudget`, installed by `admit_mcp_servers` (`:1124`), refuses
   past `max_tool_calls_per_run` with `mcp_admission_budget_exhausted` (`:146`) — the CALL is
   refused, never the turn. Default 120, ~2× the heaviest honest Stage C drill (`:305`).

`teardown_mcp_admission` (`:1593`) removes the run's registry scope at the end of every admitted run
while the transport stays warm in `tools/mcp_tool._servers`; a teardown fault is a typed
`mcp_admission_teardown_failed` row and never fails a finished turn. `read_only` admits from a
**positive** allowlist — the `reviewer` row of the launcher's own per-profile allowlist, pinned as a
26-tool snapshot (`:190-258`) — so a tool the server grows later is denied by default rather than
silently inherited; a server whose mutating tools cannot be named admits nothing
(`mcp_read_only_subset_unknown`, `:137`). Warm-vs-cold transport is recorded, not inferred
(`:159-181`). And the agent is told when it does not get what it declared:
`render_mcp_admission_line` (`:1713`) puts one compact line on the volatile tail, kept a SEPARATE
voice from the capability block — its denials resolve at a different lifecycle point and it has its
own kill switch, and folding them would give one fact two voices. `MCP_OPERATING_SKILLS`
(`:292-294`) also preloads the surface's operating manual; admitting a tool surface without its
manual burned the live 2026-07-29 QA turn.

## 6. Terminal envelope grants

`agent_runtime/terminal_envelope.py` is the ONE deterministic answer to "may this command run on
this lane?". It exists because the same lane behaved two opposite ways on 2026-07-26: fail-CLOSED for
a profile-bound persona (the legacy envelope fires on the mere presence of
`HERMES_AGENT_RUNTIME_ROOT`, with no channel to obtain the demanded approval) and fail-OPEN for one
binding no profile (the variable never exported, the envelope inert, `git push origin main`
ungated). A bound `TerminalEnvelopeScope` closes the fail-open branch (`:1-58`).

Four classes (`:156-163`): `git_push`, `destructive_git`, `recursive_delete`, `network_egress`.
**Two independent grant doors** (`:56-70`): the **grants table**, deny-by-default, keyed
`agent_runtime.terminal_envelope.grants.<role>.<lane>` (`:486`) and read from ROOT config so a
profile cannot grant itself the right to push (`:489-497`); and the **permission mode**, where
`resolve` grants any class in `GRANTABLE_COMMAND_CLASSES` on `resolved.unbounded` alone, without
consulting the table (`:687`).

Ruling R-2 removed the last hard floors, so `GRANTABLE_COMMAND_CLASSES` **is** `COMMAND_CLASSES`
(`:168`) and `hard_floor_command_classes()` returns empty (`:1196-1207`). With `unbounded` shipping
as the default, **every envelope class runs on a governed lane today with no stanza written
anywhere.** That is deliberate: R-2 traded a preventive control for a detective one, and the
compensating control is that a mode grant is receipted through `record_envelope_decision`
(`:908-920`) with its provenance. `envelope_decision` returning `None` means "not governed, keep
legacy behavior byte-for-byte" (`:616-628`) — callers must treat it as fall-through, never allow.

## 7. Turn durability and the run budget

**One file per chat session** — `mission_chat_turns/<safe_session_key>.json` with a co-located lock
(`agent_runtime/mission_chat_turns.py:26-52`), so concurrent turns in different chats never contend;
the legacy monolith splits once on first read/write and is renamed aside, never deleted. Retention:
100 turns per session, 50 session files, inside the per-session lock (`:72-73`).

**The state table decides everything** (`:75-233`). Journal states: `pending`, `executing`,
`outcome_unknown`, `native_committed`, `projected`, `abandoned`, `budget_exhausted`; legacy
streaming states are read but never produced by `transition_mission_chat_turn` (`:440`). Every state
belongs to exactly one lifecycle bucket, guarded at import time by *raised* (not asserted) contract
checks so `python -O` cannot strip them (`:233-240`). The rule exists because the same defect landed
twice ~700 lines apart — a consumer spelling "which turn states are over" as its own literal, which
then did not know about `budget_exhausted`, leaving a turn over for minutes rendering a live spinner.
Two subtleties worth not tidying: `budget_exhausted` is settled and deliberately absent from
`INFLIGHT_TURN_STATES`, so no repair sweep may reopen it; and it may still promote to
`native_committed` (`:223-226`), because a reply proven after the budget settled must never be lost.
`turn-resolve --action abandon` accepts `outcome_unknown` only (`:192`).

**The run budget is an accounting seam, not a scheduler.** `agent_runtime/run_budget.py` holds no
policy, enforces nothing, never raises (`:29-44`); each mechanism decides on its own terms and then
declares itself. Four kinds (`:66-72`): `wall`, `api_calls`, `total_tokens`, `mcp_calls`. Three
deliberately distinct enforcement semantics (`:75-93`): `TRIPS_RUN` (raises `RunBudgetExceeded`),
`LANDS_TURN` (steers a final checkpoint reply; the turn settles `budget_exhausted`), `REFUSES_CALL`
(the call is declined, the turn continues). The block lands under `run_budget` in `profile_timing`
(`:391`) and rides `RunBudgetExceeded.run_budget` so it survives the raised path.
`safe_accounting_block` (`:394`) is the ONE reader every persistence/projection boundary goes
through, and `turn_run_budget_metadata` (`:419`) the one adapter turning "the run that just
ended" into the journal fragment a settle point splices in (`run_budget.py:46`) — both
absence-preserving. Neither is the only code that touches the block: two live consumers read it
straight off the record they were handed (`operator_channels.py:927-929`,
`persona_chat_history.py:1690-1692`). Default wall budget is **240 s**
(`runtime_config.py:150-164`), tunable at
`agent_runtime.mission_chat.default_max_seconds` and clamped; an explicit `--max-seconds` always
wins, including outside the clamp (`config.py:820-840`). The last `max(60s, 15%)` is reserved for
the graceful checkpoint, so a default turn has ~180 s of tool-using time.

**The volatile tail** is how the agent is told any of this. Contributors register by name with their
own byte budget — `turn_budget` 1024, `capability` 4096, `mcp_admission` 2048
(`mission_chat_turn_context.py:114-122`, composed at `:467-479`). Per-contributor, not global, so a
long capability account cannot squeeze out the countdown. Over-budget content states its shortfall
twice: in band, so the agent reads it was not told everything, and as a typed accounting row, so no
operator has to grep prose to learn a fact was clipped.

## 8. Session scope and continuity

`agent_runtime/chat_session_scope.py` is the ONE authority for *which* chat `SessionDB` is
operator-visible. It exists because the persona-instance store deliberately collapses per-profile
homes onto the shared runtime root while the chat SessionDB did not — so a CLI-lane turn minted its
transcript into `profiles/<active>/state.db` while writing the binding into the shared store, and
the cockpit dropped the row as `session_not_in_db` (`:1-38`). Ladder, highest first — six rungs
(`:43-75`):

| rung | source |
| --- | --- |
| `RELAY_CONTEXT` | the `_HERMES_HEAD_HOME` ContextVar — a nested relay turn cannot escape the operator that started it |
| `ENV_HEAD_HOME` | `HERMES_HEAD_HOME`, what the Launcher supplies |
| `INSTANCE_RECORDED` | `PersonaInstance.chat_head_home` — only when the caller names a `session_id`, because the chat head is a per-CONVERSATION fact |
| `SHARED_ROOT_POINTER` | `<store_root>/chat_head_home.json`, published by an explicitly-headed process |
| `CONFIG_DECLARED` | `agent_runtime.head_home`, written at `harness serve` boot by `root_anchor.py` |
| `AMBIENT_HOME` | degraded: `get_hermes_home()` (`:72-74`). **NOT authoritative** — this is the rung the 2026-07-25 binding massacre and the 2026-07-27 read-lane gap were both computed in |

Env and relay context always win over the pointer, so a recorded head can only ever narrow the
ambient fallback (`:37-39`). Public surface: `resolve_chat_session_scope` (`:381`),
`open_chat_session_db` (`:615`), `publish_chat_head_home` (`:716`), `declared_chat_head_home`
(`:659`); `is_canonical_session_persistence` (`:151`) is the predicate the two §1 guards gate on.

`agent_runtime/continuity.py` is narrower than its name: `return_summary_to_parent_session`
(`:21-60`) posts ONE bounded child summary (1,200 chars, ≤8 refs) into the parent's chat session as
an assistant message, through the same `mirrored_persona_chat_append` seam every other explicit
persona-chat append uses — refs and a distillation, never the child transcript.

## 9. The agent create path

`runtime.agent.create` (`agent_runtime/serve_rpc.py:1960`) performs roster row + chat root + office
placement in ONE handler with a recorded-progress reservation
(`agent_runtime/agent_create_reservations.py`) and a compensating retire, replacing the launcher's
two sequenced writes over two transports. `harness agent create` (`persona_commands.py:435-455`) is
the same function behind an argv door — every result field a script reads is the field it would read
off the wire — and it works with no `harness serve` running because every lock in the path is a
cross-process file lock. An unknown persona is refused with `persona_not_found`
(`agent_create.py:207-217`), kept a separate reason from `persona_roster_unavailable` because the
two need opposite responses.

**The create's cost is now attributed.** `agent_runtime/agent_create_phases.py` splits the single
`phases.instance_ms` into named spans as an INFO log receipt rather than new wire fields — `phases`
rides the RPC result and a launcher parser reads it, so nothing there was touched (`:14-21`). It
inherits the same honesty rules with one addition: **accumulate, never overwrite** (`:34-36`). The
nesting is deliberate and encoded in `PHASE_ORDER`: `create_patch_ms` ⊃ `wire_row_ms` ⊃
{`permission_options_ms`, `chat_lane_scope_ms`, `tool_visibility_ms`} — summing the printed keys
over-counts, and that is the correct trade (`:40-52`). Instrumentation is free when nobody records
— one `ContextVar` read, and only `perform_agent_create` installs a recorder.

**The verdict the split produced is not the suspect it opened with.** Measured in a hermetic home
with the `check_fn` cache holed (`tests/agent_runtime/test_agent_create_subphases.py:1-34`): an
unwarmed create bills `instance_ms` 2,781 ms, of which `chat_lane_scope_ms` alone is **2,421** —
`apply_chat_lane_tool_scope`, not `resolve_tool_visibility`, which measures **0** because the scope
application already filled every shared cache it would have reached. The same create after
`warm_persona_memos` bills 281 ms with `chat_lane_scope_ms` at 15 and zero probe rounds; the
neighbouring-memo-key suspicion is ACQUITTED at HEAD. No test asserts a millisecond; the gate is the
counted mechanism — the registry's probe-round counter (`tools/registry.py:286`), sampled as a
per-turn delta (`persona_commands.py:2008`, `:3203`).

## 10. Provider dispatch, and the outcome line

The conversation loop sits a layer below the harness and cannot hold the turn's `TurnPhaseMarks`, so
it announces the dispatch instant as a `run.progress` timing payload and the mission-chat handler
converts it. `_emit_request_assembled_marker` (`agent/conversation_loop.py:347-378`) fires once per
PHYSICAL dispatch attempt, right after the transport preflight (so a codex token refresh lands on
the hermes side of the split) and right before the provider call; it carries no
`duration_ms`/`timing_key` because it names an INSTANT, which also keeps it out of the
profile-timing dict. Step constant: `CONVERSATION_REQUEST_ASSEMBLED_STEP =
"conversation_request_assembled"` (`hermes_constants.py:1400`); `mark_from_trace_payload`
(`mission_chat_phases.py:352-384`) is the only converter, and it takes nothing from a malformed one.

The live measurement that motivated the split (turn `c59ab99e`, 2026-08-22): **1,762 ms of a 13,532
ms "provider" span elapsed before the request client existed** — prologue, tool-schema
serialization, prompt-cache decoration, request middleware, the `pre_api_request` hook and the
per-request client build all sat inside the span the launcher rendered as provider time. The
sibling receipt for every non-mission-chat lane is the `ttfb=` token on the `API call #N` log
line (`_format_ttfb_token`, `conversation_loop.py:381-394`, commit `74702c193e`). Same
absent-never-zero rule: `None` means no first-byte instant was observed — a non-streaming call, or a
stream whose first-delta callback never fired — and the token vanishes rather than printing
`ttfb=0.0s`, which reads as an instantaneous provider and is a lie no reader can detect.

**Outcome recording is launcher-side, one line, at the settle chokepoint.** A turn that settles
WITHOUT acceptance emits `[MissionChatOutcome] turn_id=… status=… error_kind=… message="…"` through
`Logger`, so the release-build diag tee carries it
(`mission_agent_chat_runtime_controller.dart:1555-1571`) — harness error prose only, never the
operator's text.

## Invariants

1. **One id for the turn, minted launcher-side**: `idempotencyKey` → `client_message_id` →
   `turn_id`, byte-equal at every hop. Nothing re-mints it; nothing joins on time proximity.
2. **An unresolved phase is ABSENT, never a fake zero** — `mission_chat_phases`,
   `agent_create_phases`, and the `ttfb=` token alike. First mark wins, monotonic clock only, and no
   mark is ever subtracted from one taken in another process.
3. **An instrument may never steer what it measures.** `TurnPhaseMarks.get` serves one caller; no
   turn decision may branch on a mark.
4. **The profile declaration is the sole MCP admission authority.** Role names neither narrow nor
   widen it; the `role` on the decision record is reporting. **`unbounded` never crosses the
   declared set**, and the scope applies after permission-mode resolution.
5. **A config fault narrows** — permission default and envelope grants alike — and every
   harness-wide posture knob reads the ROOT config, never the active profile's.
6. **The mint is the first durable side effect**, so every refusal decidable without it comes first.
7. **A recorded reply is never lost to a repair flip.** `budget_exhausted` and `outcome_unknown`
   both promote to `native_committed`; nothing resurrects to `pending`.
8. **Adding a turn state extends the table**, guarded by raised import-time checks.
9. **The MCP admission line and the capability block stay two voices** — two budgets, two
   independent failure modes.
10. **The volatile tail is budgeted per contributor**; a shortfall is stated in band AND as a row.

## Open rows

- **New-chat first send settled REJECTED — OPEN, unreproduced.** 2026-08-22 01:00:47Z; the mint is
  acquitted, the explicit-`session_id` lane implicated, and `[MissionChatOutcome]` (§10) stands
  guard for the next occurrence → [record](planned/new-chat-first-send-rejected.md).
- **Hermes admission costs ~4–6 s of every warm turn**; convicted contributor is
  `apply_chat_lane_tool_scope` (§9) → [plan](planned/mission-chat-admission-latency.md).
- **Chat-lane context, memory and affordance gaps** — core-context/profile-memory knobs with no
  operator surface, no shell hooks, no slash commands, no attachment *input*, no per-turn worktree
  isolation, skill surface filter → [plan](planned/chat-lane-context-and-memory-gaps.md).
- **The persona binding is not yet the env authority for spawned children** →
  [plan](planned/persona-binding-env-authority.md).

## Unverified carry-forward

Mechanism exists in code; the NUMBER or live condition was not re-measured here.

- **Warm/cold MCP admission timings** (6–8 ms warm, 0.2–0.3 ms teardown, 3,197 ms cold) —
  `mcp_admission.py:159-181`, 2026-08-09, one 60-tool stdio server. The `warm`/`cold` attribution
  field is live; the milliseconds are historical.
- **The 2,421 ms `chat_lane_scope_ms`** —
  `tests/agent_runtime/test_agent_create_subphases.py:17-24`, one machine, hermetic home. That file
  states no test asserts a millisecond and none can reproduce the magnitude; the enforced gate is
  the probe-round count.
- **The 1,762 ms hermes share of turn `c59ab99e`** (`mission_chat_phases.py:360-363`) and the live
  phase-joined TTFT splits (alice 17.8 s, qa 9.2 s) — 2026-08-22 session receipts, read through the
  launcher's audit tooling; not reproducible from this repo.
- **Tool-schema census** (62 core tools / 93,075 bytes vs 34 deferrable / 32,182; 74% core) —
  `send-policy-decisions-2026-08-09.md` §T7, a registry census on that date. The conclusion it
  supports — no chat-lane schema budget, because deferral is already maxed and the only remaining
  lever is the tool restriction the ruling removed — is a decision, not a measurement.

## Supersedes

Archived under [`archive/2026-08-22-pre-consolidation/`](archive/2026-08-22-pre-consolidation/),
superseded for the chat-turn lane by this document.

| archived doc | disposition |
| --- | --- |
| `mission-chat-turn-context.md` | §7 (tail roster, budgets, HUD field contract) |
| `mission-chat-mcp-admission.md` | §5. **Its role-lane language (`qa` at R1, `qa`+`dev` from the 2026-07-29 R4 widening) is STALE** — S64/S66 removed role admission entirely |
| `mission-chat-terminal-envelope-grants.md` | §6 |
| `UNBOUNDED_DEFAULT_PLAN_2026-08-09.md` | shipped; §4 is the as-built |
| `mission-chat-lane-gap-audit.md` | G3/G4/G5/G5b/G6/G7/G10 landed; G1/G2/G9/G11/G12/G13 resolved *for the default posture* by the unbounded ruling (§4); remainder → `planned/chat-lane-context-and-memory-gaps.md` |
| `chat-session-presence-authority.md` | §8 |
| `turn-durability-design.md`, `run-budget-accounting.md` | §7 |
| `send-policy-decisions-2026-08-09.md` | T5 shipped (§3); T2 and T7 are recorded decisions **not** to build |
| `AGENT_CREATE_ONE_CALL_PLAN_2026-08-16.md`, `UNIFIED_AGENT_CREATE_CALL_PLAN_2026-08-16.md` | both shipped on the hermes side; §9 |
| `PROVIDER_LOGIN_FIRST_CLASS_PLAN_2026-08-16.md` | PL-1 (`_provider_visibility_catalog`, `hermes_cli/harness.py:3431`) and PL-2 (`hermes_cli/auth_noninteractive.py`) shipped; PL-3/4/5/6 are launcher-repo work, not tracked here |
| `PERSONA_PROFILE_BINDING_AUTHORITY_PLAN_2026-08-16.md` | B-1 shipped; remainder → `planned/persona-binding-env-authority.md` |
| `eager-tool-discovery-audit-2026-08-09.md` | Fix B and Fix C shipped same-night; the lazy-builtin-discovery half is a startup-perf item, not a chat-lane one |
