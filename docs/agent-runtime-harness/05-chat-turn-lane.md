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
flag (`agent_init_cold`, `:92`) and three counters (`registry_probe_rounds`,
`visibility_bundle_builds`, `builds_overlapped`, `:96`); `_BLOCK_ORDER` is the closed set a reader
may see. `visibility_bundle_builds` landed 2026-08-23 with the chat-lane bundle (§4a) and is a
delta of a thread-cumulative counter like `registry_probe_rounds`: `0` on a warm steady-state turn,
`1` when a keyed input moved, and anything above `1` means something is re-resolving what the
bundle holds.

**Six `profile_timing` receipts landed 2026-09-01** (prep-cost Stages 3–5, `3b4923f6c2` /
`139f480a23`; all `*_ms` keys `safe_turn_profile_timing` already admitted — no schema change):
`profile_conversation_turn_context_ms` times `build_turn_context`, the owner of most of the
`provider_request_started → request_assembled` span — the live record falsified the old belief
that tool-schema `request_build` owned it (that bills **1 ms warm**; the expensive schema build
runs at agent construction and is already cached in `model_tools._tool_defs_cache`); exactly one
of `profile_conversation_system_prompt_restore_ms` / `..._build_ms` per prologue entry; exactly
one of `agent_init_tool_defs_build_ms` / `..._cached_ms` per construction; and
`session_db_open_ms` — the per-turn SessionDB writer-open, bench-measured **~6 ms warm**
(5.7/6.5/6.6 median, flat from 0.6 to 170.8 MB store size; cold first-open 56–204 ms), which is
why the open stays per-turn and unpooled. The durable record's block is the TURN's (a superset);
the live result frame keeps the runner's dict byte-for-byte. Evidence + the owed live re-takes:
`planned/chat-turn-prep-stages-3-5-field-notes-2026-09-01.md`.

**Four honesty rules** (`:18-50`), each enforced in code, not by convention:

1. **Absent, never a fake zero.** `snapshot()` emits only what was marked (`:334-355`);
   `safe_turn_phases` drops what it cannot read and never defaults (`:463-499`); `flag()`/`count()`
   treat `None` as "could not establish" and record nothing (`:242-279`) — which matters most for
   the counters, where `0` is a real and interesting answer.
2. **Monotonic only.** Anchor and marks come from one injected callable (`:170-179`); `anchored_at`
   is the single wall stamp, for eyeballing `agent.log` (`:192-196`).
3. **First mark wins**, under a per-phase lock inside `mark()` (`:221-240`) — `provider_first_byte`
   rides the emitter's per-token `delta()` and may arrive on a worker thread.
4. **Release-visible.** No flag, no debug gate; the block rides persists the turn already performs.

Construction IS the anchor, taken as the handler's first statement (`persona_commands.py:2059`;
the handler opens at `:2034`), ahead of the capability bind and the config load, because everything
below is admission cost the operator waits through. Marks land at `:3158`, `:3197`, `:3225`,
`:3266`, `:3282`, `:3295` (write_ahead — deliberately *before* the write it names), `:3450`,
`:3662`, `:3880` and on the emitter (provider_first_byte, `~:4986`); `request_assembled` arrives as
a trace payload, converted in `_stream_progress` (anchors re-read 2026-08-23 after the Stage 0–2
edits moved this file). The emitter's older `ttft_ms` is unchanged; `phases` is a superset, because the
emitter is built ~1,100 lines in and its clock cannot see the profile bootstrap.

## 3. Model selection

Four tiers, highest wins, resolved once in `_chat_effective_model_payload`
(`persona_commands.py:6953`):

```
chat-session override  >  instance override  >  persona default  >  config default
```

The chat-session override persists under `mission_control_chat_model_override`
(`persona_commands.py:6485`, `agent_runtime/persona_chat_history.py:234`) via
`_resolve_chat_model_override` (`:6932`), called at `:3471`. Its scope is literally
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
- `_enabled_toolsets_for_chat` (`:684`) runs the DECLARATION (§4c) → chat capability augmentation →
  the T3/T6a cost policy → the MCP admission scope, and `unbounded` bypasses the cost policy, never
  the declaration. `file` / `terminal` / `code_execution` / `browser` / `vision` are
  therefore present on a default turn because `harness_core` names them; a *bounded* persona
  restores what the cost policy cut via
  `agent_runtime.personas.<id>.chat_lane_restore_toolsets`.
- `agent_chat`, `board` and `clarify` are unconditional chat capabilities
  (`_CHAT_CAPABILITY_TOOLSETS`, `:902`) regardless of the persona's configured list; `clarify` is
  additionally un-blocked by name on the bounded lane (`:603`), which has a clarify bridge.

`apply_chat_lane_tool_scope` (`:890`) is the display-parity door: it threads the REAL chat-lane
resolution onto the operator preview, so `persona tool-diff` reports what the turn ships. It sets
`configured_toolsets` from the same declaration on BOTH modes since S0a; the
`all_registered_toolsets()` arm it used to take under `unbounded` is what made every persona's
preview read 32 toolsets / 79 tools. It also carries the typed account of what the scoping removed
(`chat_lane_capability_drops`, `:744`) — survivors alone were never an account of removals.

### 4c. The declared toolset (S0a, 2026-09-03)

The harness lane admits by the persona's BOUND PROFILE `toolsets:` key, read by
`declared_lane_toolsets` (`agent_runtime/personas.py:224`) and handed to every caller through
`effective_toolsets` (`:328`). A profile that declares nothing — or only the upstream default
`["hermes-cli"]` that `hermes_cli/config_defaults.py` writes for an unset key — resolves
`HARNESS_LANE_DEFAULT_TOOLSETS` (`agent_runtime/personas.py:170`) = `harness_core`, reported as
`toolset_declaration.source: lane_default`; any other list is honored verbatim as `profile_config`;
an unresolvable profile home resolves the same default as `profile_unresolved`. A YAML fault
resolves narrow, never wide. `harness_core` (`toolsets.py:406`) is a composite of 15 member
toolsets — `agent_chat`, `board`, `clarify`, `delegation`, `terminal`, `file`, `web`, `browser`,
`browser-cdp`, `skills`, `memory`, `todo`, `session_search`, `vision`, `code_execution` — expanded
to those NAMES by `expand_toolset_names` (`:861`) so the cost policy, which drops by name, still
sees them. Measured 2026-09-03 on all four mission personas: **43 callable tools, 0 withheld,
`model_tool_tokens` 1149** (was 79 / 17 / 2142). The per-persona `AgentPersona.toolsets` list is
LEGACY DISPLAY: it is reported as `persona_toolsets` / `toolset_declaration.persona_list` with
`persona_toolsets_in_force: false` and admits nothing.

### 4a. One visibility resolve per turn (`agent_runtime/chat_lane_bundle.py`, 2026-08-23)

Those resolvers were being walked **four times per turn** — `capability_block` and `admission_line`
from `build_mission_chat_turn_context`, `tool_contract` and `permission_state` from inside its
`mission_chat_runtime_signature`, and `_enabled_toolsets_for_chat`/`_blocked_tool_names_for_chat` again in
`mission_chat_reply` for the request itself. Each walk re-ran `permission_options_for_chat` →
`effective_toolsets`/`all_registered_toolsets` → the registry `check_fn` sweep, and the caches
underneath expire on 15/30 s TTLs tuned for one snapshot build rather than for operator cadence.
Live receipt: `registry_probe_rounds=27` inside a 1,313 ms `context_built` span, six minutes after a
serve boot, on a chat where nothing had changed (turn `2026-08-23T14:34:57Z`).

`chat_lane_bundle` resolves the lane ONCE and memoizes the **composition** — never the probes. The
key is the lane's own identity: persona revision, chat root + a fresh permission fingerprint (mode,
source, expiry, remaining turns, mode blocks), root + active `config.yaml` `(mtime_ns, size)`,
runtime root, entry-point lane, and `tools.registry.registry_epoch()` — a single integer that moves
on every registration/MCP refresh (`registry.generation`) *and* on every `invalidate_check_fn_cache`
(the availability half, added with this stage). The `check_fn` grace machinery is untouched, and a
down backend still loses its TOOLS at construction because `registry.get_definitions` re-probes on
its own TTL; what can go stale is the toolset NAME in the lane's accounting until the epoch moves.
`invalidate_chat_lane_bundles()` is the explicit hatch. A bundle whose best-effort components
faulted is served to that turn and never stored. Scope is the turn path only — the preview lane,
snapshot builder and `persona_prewarm`'s memo warm still resolve live, because those are routinely
driven with a monkeypatched resolver a config-keyed memo cannot see. (The chat-ACTOR prewarm of
§4b is not an exception: it reads the bundle deliberately, because it is assembling the same
request a turn assembles.) Receipt: `visibility_bundle_builds` (§2).

**The reuse key stopped keying on row liveness at the same time.** `_runtime_signature` hashed
`asdict(instance)` whole, and a chat turn WRITES that row (`state` flips, `updated_at` /
`last_heartbeat_at` are stamped, the handler writes `skill_manifest_hash` back at the end of every
turn), so with `persona_chat.hot_sessions` finally on, the second message of one chat 45 s after the
first recorded `resident_rebuild_runtime_signature_changed` + `resident_actor_reused=0`
(`2026-08-23T14:45:14Z`). It now folds explicit allowlists — `PERSONA_IDENTITY_FIELDS` /
`INSTANCE_IDENTITY_FIELDS` (`mission_chat_turn_context.py`) — of the fields that decide what a
constructed actor IS. Allowlists, not denylists: a new field on either record is presumed
bookkeeping until someone names it.

**And a refused reuse now NAMES the input that moved (2026-08-23).** The allowlist fix was not
enough on its own: three consecutive turns of one neko chat at `19:03:10/23/40Z`, on a root the
boot prewarm had warmed nine seconds earlier, each still recorded
`resident_rebuild_runtime_signature_changed` — and the composite key can only say that *something*
changed, so the diagnosis was a hand archaeology of the live store. The signature is now composed
once as a flat dict (`mission_chat_runtime_signature_components`) and folded twice: `sha256` of the
whole thing is the key, and one digest per component rides with it to `acquire`, which diffs them
and writes `resident_rebuild_component_<name>` per moved component onto the turn record (plus one
`resident_signature_diff root=… components=…` log line). NAMES only — the digests are one-way and
no value is ever emitted. Two components lost their operator-shaped halves in the same pass:
`permissions` folds `{mode, source, expired}` instead of the whole `permission_state_for_chat`
answer (its `blocked_tools` list is resolved over every tool registered in the process, and the
actor is built from `tool_contract`'s two lists, not from that projection), and `current_chat_goal`
left `INSTANCE_IDENTITY_FIELDS` for the reason `goal_id` was never in it. `tool_contract` stays and
may not be dropped — the actor IS constructed from it and `_prepare_resident_persona_chat_agent`
does not re-apply it on reuse. Full receipt: `planned/chat-turn-prep-cost.md` §5 Stage 2a.

### 4b. The resident actor is built before the first turn (2026-08-23)

With reuse finally working, the whole of §2.3's construction cost collapsed onto the FIRST turn
of each chat root — the one turn an operator is watching, and the only turn a new chat ever has.
`agent_runtime/persona_chat_actor_prewarm.py` moves it off: at serve boot (third on the one
prewarm thread) and at chat-open (both arms of `persona instance open-chat`), a background worker
builds that root's agent through `ProfileAgentRunner.prewarm` and registers it under the same
`acquire()`, so the first message arrives to an already-resident entry. Full mechanism and its
residue: **04-boot-and-lifecycle Stage 9a**.

Two facts belong here rather than there. First, the reuse key: the prewarm calls
`mission_chat_runtime_signature` — the SAME function the builder calls, which is why it is public
— because `acquire` compares digests for byte equality and rebuilds on a miss, so an actor warmed
under a re-derived key would be worse than no warm at all. Second, it cannot go through
`build_mission_chat_turn_context`: that builder CONSUMES the queued-skill list, and warming through
it would steal the operator's queued skills from the turn being warmed for.

Receipt on the turn record: `agent_init_cold=false` with `agent_ready − write_ahead < 700 ms` on a
chat's first message. **Half read, half still owed** (2026-08-24 turn store, ten records in the
00:42–00:48Z window). The prewarm itself is proven live — 59 `persona_chat_actor_prewarm` lines in
`profiles/base/logs/agent.log`, items warming in 78–2,531 ms (04's Stage 9a) — and the reuse it
exists to feed is proven on every warm turn: five consecutive turns of one root (00:43:08/17/20/28/56)
each read `resident_actor_reused=1`, `agent_init_cold=false` and no `resident_rebuild_component_*`
key at all. What is NOT yet observed is the fresh-chat FIRST turn reading `agent_init_cold=false`:
all three first turns in the window read `true`, and the two neko ones name
`resident_rebuild_component_workspace_agents` — the `--agents-file` residue 04 names, a workspace-bound
chat mismatching on the one input the prewarm cannot reproduce. The wall half of the receipt does
hold there (`agent_ready − write_ahead` = 125 / 94 / 750 ms), so the residue costs a rebuild, not the
old ~3 s construction (`planned/chat-turn-prep-cost.md` §7).

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
   permission-mode resolution and strips every `mcp-*` toolset this run was not admitted. It was
   load-bearing while `unbounded` resolved `all_registered_toolsets()` in a multi-persona serve
   process (that set includes another persona's admitted surface); since S0a §4c the resolved set is
   the profile's declaration, so the scope is DEFENSIVE — it keeps the property true by construction
   rather than by the shape of today's declarations, and it stays.
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
(`mcp_read_only_subset_unknown`, `:137`). A runtime with **no MCP client at all** —
`tools/mcp_tool._MCP_AVAILABLE` false because the optional `mcp` pip extra is absent — is its
own code, `mcp_sdk_unavailable` (`MCP_SDK_UNAVAILABLE`, read through
`mcp_admission.mcp_sdk_available`), because every such turn used to land on
`mcp_not_registered_on_lane`, whose hint sends the operator at the server and the command —
the two halves that are already healthy. That sentence cost weeks once
(root cause 2026-08-26; `harness-skills/harness-runtime-model/references/proof.md` lists it as
the hazard to check FIRST). Its hint names the extra and the required runtime RESTART, because
availability is read once at import. Warm-vs-cold transport is recorded, not inferred
(`TRANSPORT_WARM` / `TRANSPORT_COLD`) — and **the recorded milliseconds are not a spawn
discriminator**: the honest one is a non-empty admitted SET
(`registered_mcp_server_names()`, `mcp_admitted_servers`), never the clock. And the agent is told when it does not get what it declared:
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

### Cross-install continuity (S2b, R-IP10)

Everything above holds when the target is on another install, with exactly two
differences and both are stated on the refusal that enforces them.

**`session_id` crosses; `clarify_token` does not.** A session id is a name the
far install minted and handed back on the dispatch completion, and it means the
same thing on both machines. A clarify token is minted by THIS install's clarify
gateway and resolves against THIS install's pending questions, so carrying it
across would hand the far side a token it cannot look up. `agent_chat_send`
refuses an install-qualified target that carries one, as
`clarify_token_not_portable`, and the refusal names the route rather than only
the wall: answer them with `session_id` set to the session the delivery reported
as "Their thread".

**There is no shared default thread across installs.** Locally the default
session is the one this pair has been using; on another install the default is
that install's most recently established thread with anybody, which is almost
never the one the caller means. So `agent_chat_open("@mac/dev")` with no
`session_id` is refused `remote_session_required` — before any dial — and names
the same source for the id.

**The far read is a real read.** `agent_chat_open` with an `@install/` target
calls `peer.thread.read`, which requires the `target` as well as the
`session_id` and applies the SAME lane guard the local read applies
(`_session_belongs_to_chat_lane`, through the shared
`peer_directory.read_chat_lane_tail`). So a session id that is not part of that
teammate's chat lane is `foreign_session` on either machine, and a paired
install can spend a pointer it was given and discover nothing else. Until S2b
that pointer — printed on every cross-install delivery since Stage 7 — resolved
to nothing on the machine that received it.

## 9. The agent create path

`runtime.agent.create` (`serve_rpc.py::_runtime_agent_create`) performs roster row + chat root +
office placement in ONE handler with a recorded-progress reservation
(`agent_runtime/agent_create_reservations.py`) and a compensating retire, replacing the launcher's
two sequenced writes over two transports. `harness agent create`
(`persona_commands.py::_cmd_agent_create`) is the same function behind an argv door — every result
field a script reads is the field it would read off the wire — and it works with no `harness serve`
running because every lock in the path is a cross-process file lock. `harness agent retire` and
`runtime.agent.retire` are the same arrangement for the inverse
(`agent_retire.perform_agent_retire`), and `harness persona instance retire` is a THIRD door onto
it; see [06 — The inverse](06-office-and-board.md#the-inverse--one-call-takes-an-agent-off-the-level-and-says-what-left).
An unknown persona is refused with `persona_not_found` (`agent_create.PERSONA_NOT_FOUND_REASON`,
message built by `persona_not_found_message`), kept a separate reason from
`persona_roster_unavailable` because the two need opposite responses. Anchors here are symbols
rather than lines because this paragraph's `serve_rpc.py:1960` was pointing thirty lines short of
its handler by 2026-08-27, and read as verified the whole time.

**The create path has THREE phases, and only two of them are atomic** (plan S4).
`instance` and `placement` are the pair the reservation joins — a failure in the
second compensates the first away. `skills` is deliberately outside that join.
It runs after both writes are durable, the receipt reads `placed`
(`agent_create_reservations.py::STATE_PLACED`), and its refusals stamp
`rolled_back: false` because the agent they refuse for is standing, correct and
messageable: only its skill assignment is owed. A placed agent without its
skills is what every launcher drop produces today; retiring it to satisfy
atomicity would archive a working agent to undo a file copy.

**What the phase does, in order** (`agent_create.py::run_skills_phase`). Every
id must be identical under BOTH `serde.safe_id` and the store's own
`safe_assignment_token` before any root is walked — that is what makes "never
path-joined from input" true and what makes the ack's `assigned` the list the
store HOLDS rather than one it quietly re-spelled. Then every canonical id
(`hermes_constants.CANONICAL_SHARED_SKILL_IDS`) goes through
`skill_install.install_and_verify_harness_skill`, which refuses unless the
destination exists, the install receipt is `ok`, AND an independent
`harness_skill_hash_mismatches` re-read is empty — three conditions because the
mismatch detector alone `continue`s past a destination that does not exist, so
a copy that never landed reads as a clean bill. Then `resolve_skills` must
answer `resolved` for every id. Only then is `skill_overrides` written, at the
INSTANCE tier through `PersonaInstanceStore.update_profile` — never
`persona.skills`, which would reconfigure every other instance of that persona.

**Install runs BEFORE resolve, which inverts the order D5 lists them in.** A
canonical skill's resolvable copy IS the installed one, so resolving first would
refuse `skill_unresolved: missing` on a fresh machine for a skill the next line
would have installed.

**This is the first place the repo↔installed hash is a GATE rather than a
report.** `profile_readiness` files a mismatch as a severity-15 row and
`prompt_observability` as a HUD flag; the launcher's sync control is a button.
Nothing refused, which is how the 2026-08-24 incident handed a running agent a
14457-byte copy of a 14906-byte skill. A verb that ASSIGNS a skill is the one
place where "the copy is stale" has an answer that is not a warning.

**The `None -> []` collapse in `persona instance update-profile` is fixed in the
same slice.** `_cmd_persona_instance_update_profile` passed
`skills=list(getattr(args, "skills", None) or [])`, so an omitted `--skill`
reached the store as an empty LIST and the store's correct
`if skills is not None or clear_skills:` contract then wrote
`skill_overrides = []`: a `--display-name`-only call silently cleared every
skill override on that instance. The store was never wrong — the collapse was in
the layer whose job is to translate absent into absent.

**`correlation_id` rides the create and echoes on its ack** (`agent_create` reads
`params["correlation_id"]`, threads it into the placement write and copies it onto the result), so
one gesture's create is joinable with its patches. Since S8b (`d107d132e0`) the RETIRE reads and
threads the SAME normalisation, and since S8b-b (2026-08-27) BOTH argv doors onto it publish the
flag, so a gesture's create half and delete half share one correlation space on the degraded lane
as well as the RPC one — 06's inverse section has the thread. Nothing else on this lane mints or
rewrites the token: it is the caller's, end to end.

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
per-turn delta (`persona_commands.py:2064`, `:3267`).

## 10. Provider dispatch, and the outcome line

The conversation loop sits a layer below the harness and cannot hold the turn's `TurnPhaseMarks`, so
it announces the dispatch instant as a `run.progress` timing payload and the mission-chat handler
converts it. `_emit_request_assembled_marker` (`agent/conversation_loop.py:347-378`) fires once per
PHYSICAL dispatch attempt, right after the transport preflight (so a codex token refresh lands on
the hermes side of the split) and right before the provider call; it carries no
`duration_ms`/`timing_key` because it names an INSTANT, which also keeps it out of the
profile-timing dict. Step constant: `CONVERSATION_REQUEST_ASSEMBLED_STEP =
"conversation_request_assembled"` (`hermes_constants.py:1533`); `mark_from_trace_payload`
(`mission_chat_phases.py:424-460`) is the only converter, and it takes nothing from a malformed one.

**The payload has to survive the sink to reach that converter.** Its real route is
`agent.status_callback → profile_runner._profile_status_callback (:1489) → ChatProgressSink.emit →
on_trace → _stream_progress`, and the sink drops any `run.progress` payload carrying none of its
Trace-lane signal keys (`progress.py:83-86`) — which a timing marker never carries, because it names
an instant rather than work. That silently unmeasured `request_assembled` on every live turn through
2026-08-23. `ChatProgressSink._forward_phase_timing_marker` (`progress.py:178-224`, called at `:137`
ahead of the filter) now forwards the marker to `on_trace` ONLY: no EventLog row, no
`before_first_trace` latch, no chat-log mirror, and only a `step` matched against the closed set of
marker steps plus a bare `status` token — so the noise rule and the redaction boundary both stay
intact. `phase_timing_marker_step` (`mission_chat_phases.py:391-422`) is the single authority both
sides read, so producer and consumer cannot drift apart again.

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

- **New-chat first send settled REJECTED — OPEN, unreproduced.** 2026-08-22 02:00:47Z; the mint is
  acquitted, the explicit-`session_id` lane implicated, and `[MissionChatOutcome]` (§10) stands
  guard for the next occurrence → [record](planned/new-chat-first-send-rejected.md).
- **Hermes admission cost — REMEDIATED 2026-08-23, warm half RE-TAKEN 2026-08-24; two
  narrow opens left.** The ~4–6 s warm-turn share was remediated by
  `planned/chat-turn-prep-cost.md` Stages 0–2
  (`60c7f46ec1`/`7f2c82f090`/`bfde53b4ae`: visibility bundle, actor prewarm); first live
  reuse receipt: bootstrap 3,782 ms → 62 ms (turns 17:33:01Z/17:33:17Z). The steady-state
  re-take arrived on the 2026-08-24 00:42–00:48Z records: five consecutive warm turns of one
  root read `registry_probe_rounds=0` + `visibility_bundle_builds=0` + `resident_actor_reused=1`
  + `agent_init_cold=false`, and `request_assembled` is present on all ten records in the
  window. Two clauses did NOT close and stay open: **`context_built<500`** held on two of the
  five (343 / 468 ms) against 562 / 796 / 968 on the other three, and the **fresh-chat first
  turn** still reads `agent_init_cold=true` through
  `resident_rebuild_component_workspace_agents` (§4b) →
  [plan](planned/mission-chat-admission-latency.md).
- **Chat-lane context, memory and affordance gaps** — core-context/profile-memory knobs with no
  operator surface, no shell hooks, no slash commands, no attachment *input*, no per-turn worktree
  isolation, skill surface filter → [plan](planned/chat-lane-context-and-memory-gaps.md).
- **The persona binding is not yet the env authority for spawned children** →
  [plan](planned/persona-binding-env-authority.md).

## Unverified carry-forward

Mechanism exists in code; the NUMBER or live condition was not re-measured here.

- **Warm/cold MCP admission timings** (6–8 ms warm, 0.2–0.3 ms teardown, 3,197 ms cold) —
  `mcp_admission.TRANSPORT_WARM` / `TRANSPORT_COLD`, 2026-08-09, one 60-tool stdio server. The
  `warm`/`cold` attribution field is live; the milliseconds are historical **and are not a
  discriminator**. `launcher_qa` is a compiled Dart exe whose real cold spawn measured ~100 ms, so
  the "~3,200 ms means a real cold spawn" rule of thumb reads a genuine spawn as a fast failure —
  it sent a whole investigation down the wrong branch (corrected 2026-08-26; the docstring and
  `profile_runner`'s comment were re-corrected 2026-09-02). The discriminator is the non-empty
  admitted SET. No code compares an elapsed count, and
  `tests/agent_runtime/test_mcp_admission.py::test_no_admission_code_branches_on_an_elapsed_millisecond_count`
  is the fence that keeps it that way.
- **The 2,421 ms `chat_lane_scope_ms`** —
  `tests/agent_runtime/test_agent_create_subphases.py:17-24`, one machine, hermetic home. That file
  states no test asserts a millisecond and none can reproduce the magnitude; the enforced gate is
  the probe-round count. **Annotated 2026-08-23 (prep-cost §3 H2): the 2,421 ms is the UNWARMED
  CREATE subphase (warm create: 859/15 ms) — never re-quote it as a per-turn cost.**
- **The 1,762 ms hermes share of turn `c59ab99e`** (`mission_chat_phases.py:433-434`) and the live
  phase-joined TTFT splits (alice 17.8 s, qa 9.2 s) — 2026-08-22 session receipts, read through the
  launcher's audit tooling; not reproducible from this repo.
- **Tool-schema census** (62 core tools / 93,075 bytes vs 34 deferrable / 32,182; 74% core) —
  `send-policy-decisions-2026-08-09.md` §T7, a registry census on that date. The conclusion it
  supports — no chat-lane schema budget, because deferral is already maxed and the only remaining
  lever is the tool restriction the ruling removed — is a decision, not a measurement.

## Supersedes

`planned/agent-placement-verb.md` — **deleted 2026-08-27 by the S10 fold-in
commit** (`git log --diff-filter=D --oneline -- docs/agent-runtime-harness/planned/agent-placement-verb.md`
recovers it). Its chat-lane half is §9 above: the third phase, its two gates,
the order they actually run in, and the `None -> []` `update-profile` fix.

The rest are archived under [`archive/2026-08-22-pre-consolidation/`](archive/2026-08-22-pre-consolidation/),
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
| `PROVIDER_LOGIN_FIRST_CLASS_PLAN_2026-08-16.md` | PL-1 (`_provider_visibility_catalog` in `hermes_cli/harness.py`) and PL-2 (`hermes_cli/auth_noninteractive.py`) shipped; PL-3/4/5/6 are launcher-repo work, not tracked here |
| `PERSONA_PROFILE_BINDING_AUTHORITY_PLAN_2026-08-16.md` | B-1 shipped; remainder → `planned/persona-binding-env-authority.md` |
| `eager-tool-discovery-audit-2026-08-09.md` | Fix B and Fix C shipped same-night; the lazy-builtin-discovery half is a startup-perf item, not a chat-lane one |
