# Planned — chat-turn prep cost (the ~4 s of hermes between admission and the provider)

**Status:** Stages 0–2 LANDED 2026-08-23 (`60c7f46ec1` / `7f2c82f090` / `bfde53b4ae`), Stage 2a landed in two parts (`14271f261f` = the instrument + convictions 4–6; conviction 7, the ambient config document, in the working tree — the instrument's first field validation); live steady-state re-take receipts owed for 1, 2 and 2a; Stages 3–5 not started. **Owner doc:**
[`../05-chat-turn-lane.md`](../05-chat-turn-lane.md).
**Question this answers** (operator, 2026-08-22): *"maybe something we are doing with the
chat isn't initializing fully or fast enough?"* — the answer is **yes, twice over**: the
turn path re-derives per turn what could live per process (and the per-process caches it
does have expire on 15/30 s TTLs tuned for snapshot builds, not for operator cadence), and
the one warm path that exists (`persona_prewarm`) warms the *create* lane's memos at boot,
not the *turn* lane's costs at turn time.

Everything below is read from live turn records under
`X:/Eternia/.hermes/agent-runtime/mission_chat_turns/` (v3 `phases` blocks), the live
serve log `X:/Eternia/.hermes/profiles/base/logs/agent.log`, and HEAD code. Carry-forward
numbers are marked as such.

---

## 1. The measured phase table (live turns, 2026-08-22/23 UTC)

Deltas in ms between consecutive marks of the v3 `phases` block
(`agent_runtime/mission_chat_phases.py:76-89`; anchor = handler entry,
`hermes_cli/harness_parts/persona_commands.py:2059`). All turns below are
`gpt-5.6-luna` / `openai-codex` mission chats read from the turn store on 2026-08-22.

| turn (`started_at` UTC) | ctx | obs | emit/WA | →agent_ready | →first_byte | probe rounds | builds overlapped | note |
|---|---|---|---|---|---|---|---|---|
| `…bda7c2d49abb` 08-23 02:04:20 (= diag turn **4a80f05e**) | 344 | 453 | 15 | **3,016** | **4,344** | 0 | 1 | first turn after a 22:03 serve restart; new chat |
| `…e880b26e2c95` 08-23 02:55:06 | 1,953 | 468 | 15 | **3,642** | 3,782 | 27 | 1 | warm serve, new chat (cold resident actor) |
| `…b1ec49d62a8c` 08-23 02:56:29 | 1,375 | 453 | 16 | **500** | 1,062 | 26 | 0 | same chat, turn 2 (resident actor reused) |
| `…dff8c307101a` 08-22 17:44:24 | 1,203 | 391 | 15 | 3,171 | 2,250 | 27 | 1 | qa |
| `…c7b12bd01053` 08-22 17:41:52 | 3,921 | 531 | 16 | 3,219 | 4,078 | 23 | 3 | neko |
| `…d7033c756f1f` 08-22 14:51:42 | 1,844 | 531 | 15 | 110 | 3,250 | 0 | 1 | reuse-warm |
| `…457c7fbdea98` 08-22 14:51:23 | 1,969 | 421 | 15 | 3,360 | 4,985 | 27 | 2 | |
| `…b8ba46db0d97` 08-22 14:49:47 | 1,077 | 625 | 17 | 625 | 1,389 | 26 | 0 | |
| `…d3b728e445e3` 08-22 14:48:35 | 157 | 1,858 | 31 | 1,423 | 10,469 | 0 | 3 | fb span includes provider stall |

Column key: **ctx** = `request_received→context_built`, **obs** =
`context_built→observability_built`, **emit/WA** = `→emitter_created/write_ahead`
(always ~15–30 ms), **→agent_ready** = `write_ahead→agent_ready` (profile bootstrap),
**→first_byte** = `provider_request_started→provider_first_byte` (contains BOTH hermes
assembly and provider TTFB — see §2.4).

**The diag turn 4a80f05e reconciles exactly.** Launcher diag said
`send_to_admit=921ms, admit_to_first_delta=7,391ms`. The record
(`mission_chat_turns/persona_chat_personainst_neko_supervisor_agent_f6f7a51b_e5a076d45827_463c52dff9ac.json`,
key `agent-chat-send-4a80f05e-…`) says `write_ahead=812`, `provider_first_byte=8172` —
`8172−812 = 7,360 ≈ 7,391`, and `921 ≈ 812 + ~110 ms` of launcher→serve transport.
So **the launcher's "admit" IS the hermes `write_ahead` mark**, and the launcher/transport
share of `send_to_admit` is ~0.1 s; the rest is the handler's own pre-write-ahead work
(§2.1–2.2). Hypothesis 6 is thereby bounded: admission cost is hermes-dominated.

The provider call inside was `latency=3.4s ttfb=3.1s` (agent.log
`2026-08-22 22:04:27,793 … API call #1: model=gpt-5.6-luna … in=17633 … latency=3.4s ttfb=3.1s`),
so of the 4,344 ms `provider_request_started→provider_first_byte` span, **~1.2 s was
hermes** (prologue + assembly, §2.4) and 3.1 s the provider. Total hermes prep for that
turn: 0.34 + 0.45 + 3.02 + ~1.2 ≈ **5.0 s**; on the steadier warm turns the same sum is
**~3.5–4.5 s** — the operator's "~4 s".

**Vintage caveat — RESOLVED 2026-08-23, and it was not vintage.** None of the sampled
records carries `request_assembled` or `agent_init_cold`, though both are in HEAD
(`mission_chat_phases.py:84`, `:92`; emitter `agent/conversation_loop.py:2469`; flag site
`persona_commands.py:3394-3399`). This section originally offered two explanations and
ranked them; **the serve-vintage arm is dead and the "less likely" arm was right** —
twice over, by two independent mechanisms. The serve was restarted on HEAD (confirmed:
`snapshot_core_cache_write … restat=` in the live log) and the two turns that followed
(`03:57:37Z`, `03:57:52Z`, both in the store) still carried neither key.

* **`request_assembled` — the payload never reached the handler.** The marker travels
  `conversation_loop._emit_request_assembled_marker → agent.status_callback →
  profile_runner._profile_status_callback → ChatProgressSink.emit → on_trace →
  _stream_progress`. `ChatProgressSink.emit` drops any `run.progress` payload carrying
  none of its Trace-lane signal keys (`progress.py:83-86`) — the rule that keeps bare
  "Run progress update" rows out of the operator's console. A timing marker names an
  INSTANT and carries no `tool_name`, no `command_label`, no work summary, so it was
  filtered as noise on every turn. Every existing test agreed the wiring was healthy
  because each drove the mark/emit ENDS directly and skipped the sink in the middle.
* **`agent_init_cold` — the runner had nothing to report, and knew it.** The flag is
  derived from `profile_timing["resident_actor_reused"]`, which `profile_runner` wrote
  ONLY on the resident-actor branch (`:967-986`). That branch needs a registry, and
  `persona_chat.hot_sessions_enabled` defaults to **False** (`runtime_config.py:142`)
  with no `persona_chat` stanza in the live root config — so
  `initialize_persona_chat_runtime_registry(enabled=False)` leaves the registry `None`
  (`serve.py:1242`), the handler's `runtime_registry` is `None`, and every turn took the
  `else: agent = _construct_agent()` branch, which recorded nothing. The handler then correctly refused
  to guess. **Corollary: no sampled live turn reused a resident actor — none could.**
  The fast second turn (`write_ahead → agent_ready` of 63 ms against turn 1's 2,000 ms on
  the same 03:57Z pair) is the OTHER warm caches
  (core-cache snapshot, runtime-resolve TTL, registry probe TTL), not actor reuse, and
  §2.3's "bimodal 0.1–0.6 s reused" rows must be re-read as that. Stage 2's premise is
  unaffected — a resident actor is not being reused because there is not one — but its
  receipt (`agent_init_cold=false`) also requires the config stanza to be turned on.
  **Update 2026-08-23: the stanza WAS turned on, the registry appeared — and reuse still
  did not happen, for a second and independent reason.** `_runtime_signature` hashed the
  whole persona-instance row, and a chat turn writes that row (`state` flips,
  `updated_at`/`last_heartbeat_at` are stamped, `skill_manifest_hash` is written back at
  the end of every turn), so the reuse key could not match twice. Turn `14:45:14Z` — the
  second message of one neko chat, 45 s after the first, nothing changed — carries
  `resident_rebuild_runtime_signature_changed` + `resident_actor_reused=0`. Fixed as part
  of Stage 1 (§5) with an explicit actor-identity allowlist.

Both are fixed at Stage 0 (§5). Until a serve restarted on the fixed tree writes the
keys, the assembly-vs-TTFB split inside `→first_byte` still rests on the one measured
carry-forward (1,762 ms, turn `c59ab99e`, quoted at `mission_chat_phases.py:420-424`)
plus the log cross-reference above.

---

## 2. Who owns each span

### 2.1 `request_received → context_built` (0.15–3.9 s, typically 1–2 s warm)

One span, four owners, in execution order inside `_cmd_mission_chat_message`:

1. **Admission guards + session resolution** (`persona_commands.py:2034-2530`): config
   load (mtime-cached, `agent_runtime/config.py:112-116` — fine), relay guard, target
   decision, clarify binding, **fresh SessionDB open**
   (`persona_commands.py:2076 _default_persona_session_db()` →
   `agent_runtime/snapshot.py:2312-2317` → `chat_session_scope.py:615-626` →
   `hermes_state.py:1825` — the non-read-only constructor runs schema init + FTS probe
   per open; the WAL-reset check logs on this path:
   `2026-08-22 22:04:15,970 WARNING hermes_state: state.db: linked SQLite 3.45.3 is vulnerable to the WAL-reset corruption bug …`).
2. **Chat-root lease** (`persona_commands.py:2523`): a concurrent turn/settlement on the
   same root serializes here — lease wait lands in this span undistinguished.
3. **Native history load** (`persona_commands.py:3017-3036`): full lineage read from
   SessionDB + per-turn re-filter against abandoned turn records
   (`mission_chat_turn_records(session_id=…)`, `:3021`).
4. **`build_mission_chat_turn_context`** (`agent_runtime/mission_chat_turn_context.py:329-440`):
   skill preload (catalog reads), workspace AGENTS.md, **and — the expensive part — the
   toolset/visibility resolver family invoked repeatedly**: `capability_block`
   (→ `chat_lane_capability_drops`, `persona_runtime.py:688-734`), `admission_line`
   (`:737`), and inside `_runtime_signature` (`mission_chat_turn_context.py:635-636`)
   `tool_contract` + `permission_state` — each independently walking
   `permission_options_for_chat` → `effective_toolsets`/`all_registered_toolsets`
   (`personas.py:249-252` → `model_tools.get_available_toolsets` → the registry
   check_fn sweep).

The `registry_probe_rounds` receipt (23–27 on most warm turns, baseline/delta at
`persona_commands.py:2064`/`:3267`) proves the sweep runs **many times per turn**: a
round is only counted when at least one `check_fn` actually EXECUTED
(`tools/registry.py:264-283`), and the live log shows the interleaved doubled sweep in
this exact window — e.g. turn `…e880b26e2c95`:
`22:55:04,005 WARNING tools.registry: check_fn _browser_cdp_check returned False…` twice
within 35 ms, then the whole family (`check_computer_use_requirements`,
`_check_feishu` ×2, `check_bfl_requirements`, `_check_kanban_mode`,
`check_video_generation_requirements`, `check_vision_requirements`,
`check_web_api_key`, `check_x_search_requirements`, `_check_yuanbao`,
`_check_spotify_available`, …) twice over, 22:55:03.97–05.46 — squarely inside that
turn's 1,953 ms ctx span.

### 2.2 `context_built → observability_built` (0.39–1.9 s, typically ~0.45 s)

Owner: `mission_chat_prompt_observability`
(`agent_runtime/prompt_observability.py:108-320`): skill-catalog walk behind a **15 s
TTL** memo (`_SKILL_CATALOG_TTL_SECONDS = 15.0`, `:2419`), the skill resolver, a
**second SessionDB history read** (`_chat_history_context`, `:169`), profile
context-file hashing (SOUL.md / MEMORY.md rows), identity-prompt reads. The C1
build-once note at `persona_commands.py:3404-3410` already killed the post-turn
*second* build; the *pre-turn* build still re-derives rows whose cache key
(`skill_cache_key`, `:196-203`) is stable across consecutive turns of the same chat —
the memo just doesn't outlive 15 s.

### 2.3 `write_ahead → agent_ready` (bimodal: 0.1–0.6 s reused / 3.0–3.6 s cold)

Owner: `ProfileRunner._execute_agent_run` (`agent_runtime/profile_runner.py:780-1033`).
Inside this span, in order: the global **`_WORKDIR_LOCK`** (`:797` — all runs in the
process serialize here), `persona_profile_context` install (the `.env` load:
`22:04:20,973 INFO run_agent: Loaded environment variables from X:\Eternia\.hermes\profiles\neko\.env`),
runtime resolve (cached, `:1587`), MCP admission (`:869`), and — on a chat root's FIRST
turn — **agent construction** (`_construct_agent`, `:939-965`): OpenAI SDK client
(`22:04:21,089 … OpenAI client created (agent_init, shared=True) … model=gpt-5.6-luna` —
~150 ms after the env load), tool-definition build with its own check_fn sweep
(`22:55:08,552 … check_fn check_close_terminal_requirements returned False…` — the
terminal-family probes fire *during construction*, a second probe population beyond
§2.1's), and tool_search activation
(`22:04:22,701 INFO tools.tool_search: tool_search activated (tier 1): 27 core/visible tools kept, 10 deferred`).

The resident-actor registry (`:967-986`) makes turn 2+ of the same chat root cheap
(`agent_ready` 110–625 ms, factory never called, `resident_actor_reused=1`), which is
exactly why the span is bimodal. **Nothing pre-constructed the actor before turn 1** —
not `persona_prewarm` (visibility memos only, §3), not serve boot
(`_prewarm_provider_runtime`, `hermes_cli/harness_parts/serve.py:1029-1057`, warms the
SDK import, the SSL context, and the *shared* parts of `get_tool_definitions` — not a
persona-shaped agent). **Stage 2 (§5) is the thing that now does**, at serve boot and at
chat-open, through the same `acquire()`; this paragraph describes the state it changed.

### 2.4 `provider_request_started → provider_first_byte` (hermes share ~1.1–1.8 s)

Owner: the `run_conversation` prologue + request assembly, all BEFORE any byte leaves:
`build_turn_context` (`agent/conversation_loop.py:1317-1358` → `agent/turn_context.py`:
stdio guard, sanitization, todo/nudge hydration, system-prompt restore-or-build,
preflight compression, `pre_llm_call` hook, external-memory prefetch, crash-resilience
persistence), then `request_build` (tool-schema serialization,
`conversation_loop.py:2261-2263`), LLM request middleware (`:2285-2302`),
`pre_api_request` hook (`:2307-2363`), codex transport preflight incl. token refresh
(`:2459-2464`), and a **per-request client build**
(`22:04:24,630 INFO run_agent: OpenAI client created (codex_stream_request, shared=False)`
— on the 4a80f05e turn the prologue's own `conversation turn:` line landed at
22:04:24,241, 1.09 s after `agent_ready` at ~23.15). Measured once with the split
instrument: **1,762 ms of a 13,532 ms "provider" span** (turn `c59ab99e`, quoted at
`mission_chat_phases.py:420-424`). The `request_assembled` mark that makes this a
standing distribution is in HEAD but absent from live records (§1 vintage caveat).

### 2.5 Cross-cutting: in-process snapshot builds

`builds_overlapped` was 1–3 on most sampled turns, and each led build costs seconds of
CPU in the same process (log during the 4a80f05e turn:
`22:04:26,279 … snapshot_build_core role=led caller=cli generation=2 build_ms=3979 … sections_top=agents_readiness:1517,…`;
boot-window builds up to `build_ms=9131`). The builder also runs its own
`tool_visibility` resolution (`22:04:03,213 … snapshot_agents_readiness walk_ms=1845 tool_visibility_ms=3218`),
which is what keeps the 30 s check_fn TTL perpetually churning and steals GIL time from
whatever span the turn happens to be in. This inflates every number above without
owning any single mark.

---

## 3. Hypotheses: confirmed / killed

**H1 — prewarm caches keyed differently on prewarm vs turn path: SPLIT.**
The *key-mismatch* arm is **KILLED**: `warm_persona_memos` aligns onto the exact
`(toolsets, blocked)` key the create reads
(`agent_runtime/persona_prewarm.py:205-232`, pinned by
`test_the_warm_fills_the_exact_toolset_key_the_create_reads`), and the
`session_id=None`-vs-minted-id suspicion was already acquitted
(`planned/mission-chat-admission-latency.md` §2). The *TTL* arm is **CONFIRMED**, and it
is what makes prewarm cosmetic for TURNS: the memos prewarm fills expire on
`_PROFILE_READINESS_TTL_SECONDS = 15.0` (`tool_visibility.py:466`),
`_CHECK_FN_TTL_SECONDS = 30.0` (`tools/registry.py:225`), and
`_SKILL_CATALOG_TTL_SECONDS = 15.0` (`prompt_observability.py:2419`). Prewarm fires at
boot/palette-open (`persona_prewarm done … elapsed_ms=530/219/280/266/203`, log
22:04:07–08); any turn arriving >30 s later pays the full sweep again — receipt:
23–27 probe rounds on turns HOURS after boot (§1 table). The only prewarm-filled cache
that genuinely survives is the process-lifetime
`_cached_tool_names_for_toolsets` lru (`tool_visibility.py:520`).
Also confirmed: prewarm never touches §2.3's agent construction or §2.4's prologue at all.

**H2 — `chat_lane_scope`: CONFIRMED as a CREATE-lane span; the turn lane re-runs the
same resolvers, uncached.** `chat_lane_scope_ms` is an `agent_create_phases` subphase
(`agent_runtime/persona_assignments.py:3057-3062`,
`agent_runtime/agent_create_phases.py:121`); the 2,421 ms figure in doc 05 is the
**unwarmed create** of 2026-08-22 (`tests/agent_runtime/test_agent_create_subphases.py:18-24`),
859 ms on a warm-process cold create and ~15 ms after a warm
(doc `08-performance-and-debt-ledger.md:31`) — it is NOT a per-turn phase and must not
be quoted as one. What the TURN path pays instead is the same underlying work: the
resolver family (`_enabled_toolsets_for_chat`, `chat_lane_capability_drops`,
`mission_chat_admission_line`, `chat_runtime_tool_contract`,
`permission_state_for_chat`) invoked ≥4× per turn from
`build_mission_chat_turn_context` (§2.1.4), each repeating
`permission_options_for_chat` + toolset resolution. It IS cacheable per
(persona instance, session, permission revision): the turn already computes exactly such
a composite key — `_runtime_signature` (`mission_chat_turn_context.py:605-645`) — and
then throws the component results away. **REMEDIED 2026-08-23 by Stage 1** — see §5. The
landed key is NOT `_runtime_signature`'s: it drops the instance (no component reads it)
and adds an explicit registry epoch, because the signature is a key over the ACTOR and
this is a key over the LANE. Same insight, two different questions.

**H3 — SessionDB opened cold per turn: CONFIRMED in code, magnitude UNMEASURED.**
Every turn constructs a fresh `SessionDB` (chain in §2.1.1); the writer-path constructor
runs `_init_schema` (DDL + FTS probe + column reconcile) and the WAL checks. No phase
mark isolates it, so its ms share inside the ctx span is honestly unknown — a pooling
stage needs a measurement first. (The IC-2 work covered the CLOSE side's checkpoint;
this is the OPEN side.)

**H4 — history re-read/re-serialized per turn: CONFIRMED, partially legitimate.**
The native lineage is read from SessionDB every turn (`persona_commands.py:3017-3036`)
and read a SECOND time by the observability row (`prompt_observability.py:169`); the
whole ~17.6k-token input is re-serialized into `conversation_kwargs`
(`profile_runner.py:1038-1049`) and re-shipped. A content-address exists —
`native_history_revision` already keys resident-actor reuse
(`persona_commands.py:5797-5807`, `profile_runner.py:974-979`) — so both the second
read and the re-serialization on an unchanged-revision turn are class-(d) waste; the
provider-side token cost is governed separately by `cache_scope_id` header hints
(`profile_runner.py:933-935`) and is out of scope here.

**H5 — prologue inside the "provider" span: CONFIRMED** (§2.4). Named residents of the
span, in order: `build_turn_context`, tool-schema `request_build`, request middleware,
`pre_api_request` hook, codex preflight + token refresh, per-request client build. The
carry-forward split: 1,762 ms (turn `c59ab99e`); live corroboration ~1.1–1.5 s on the
4a80f05e log timeline. Cacheable members: tool-schema serialization (keyed by toolset
tuple — the schemas are stable across turns of one chat), system-prompt
restore-or-build (already restore-first, verify hit rate). Genuinely per-turn: the
hooks, compression preflight, the user-message row.

**H6 — admission split: CONFIRMED hermes-dominated** (§1): launcher+transport ≈ 0.1 s
of `send_to_admit`; the remaining ~0.8 s (up to ~2.4 s on other turns) is the handler's
own pre-`write_ahead` work, i.e. spans 2.1–2.2. There is no separate "serve argv
dispatch" cost worth chasing.

---

## 4. Classification summary

| Cost | Owner | Class | Warm ms (typ.) |
|---|---|---|---|
| Toolset/check_fn sweeps, ≥4 resolver walks/turn | §2.1.4 + §2.3 construction | **(d) waste** within a turn, **(a)** across turns (TTL-expired memos) | ~0.8–1.5 s spread over ctx + agent_ready |
| Prompt-observability row rebuild | §2.2 | **(a)** (15 s TTL memos; key stable across turns) | ~0.4–0.6 s |
| Agent construction on chat-root turn 1 | §2.3 | **(b)** per-chat-root lazy; **covered by Stage 2's prewarm since 2026-08-23** — the cost still exists, it moved off the turn | ~3.0–3.6 s (once per chat root, and again on signature change) |
| Profile `.env` + context install | §2.3 | **(b)** | ~0.2–0.8 s |
| SessionDB cold open ×1 + history read ×2 | §2.1.1/2.2 | **(a)/(d)** | unmeasured |
| Prologue + request assembly | §2.4 | mix **(a)** (schemas, system prompt) + **(c)** (hooks, message) | ~1.1–1.8 s |
| Write-ahead persist, lease, guards, HUD deltas | §2.1 | **(c)** genuine | ~0.2–0.4 s |
| Snapshot-build GIL contention | §2.5 | **(d)** infrastructural | unattributed inflation |
| Provider TTFB | provider | **(c)** | 1.0–3.1 s (luna) |

---

## 5. Stages (ordered by value; Stages 0, 1, 2 and 2a have landed, Stages 3–5 not started)

**Stage 0 — restore the instrument before touching anything (opening gate, §6).**
**Code half LANDED 2026-08-23, commit `60c7f46ec1`**; the live re-take is still
owed. A serve restart was NOT the fix — see the resolved caveat in §1. Three changes:

1. **The sink forwards a phase-timing marker past its own noise filter.**
   `ChatProgressSink._forward_phase_timing_marker` (`agent_runtime/progress.py:178-224`,
   called at `:137` BEFORE `_chat_progress_has_signal`) recognizes the marker through
   `mission_chat_phases.phase_timing_marker_step` (`:378-409` — one authority, read by
   both the sink and the converter) and hands it to `on_trace` only. It is an
   instrument, not an event: **no EventLog row, no `before_first_trace` latch, no chat-log
   mirror**, so the Trace-lane rule at `progress.py:83-86` stays literally true. Nothing
   from the payload is forwarded verbatim except a `step` matched against the closed set
   and a bare `status` token — no free-text field crosses at all.
2. **The runner reports the cold construct it performed.** `profile_runner.py:1054` writes
   `resident_actor_reused = 0` on the no-registry branch. That branch KNOWS it built an
   agent; absent-never-zero protects an unknown fact, and this one was never unknown.
   `agent_init_cold=true` now lands on a stock (hot-sessions-off) serve.
3. **The runner's timing dict is persisted.** `profile_timing` rides the same
   native-commit persist as `run_budget` (`persona_commands.py:3623-3638`) and is bounded
   at the store boundary by `safe_turn_profile_timing`
   (`mission_chat_turns.py:1190-1254`): `*_ms` ints, `resident_actor_reused`,
   `resident_rebuild_*`, nothing else — the runner's dict is an open namespace that also
   carries transport labels and real paths. Absent stays absent, and a REUSED actor still
   has no `agent_construct_ms` (nothing was built, so there is no cost to report).

Test seam closed with it: the handler-level fake now emits its marker through the REAL
`_profile_status_callback` → `ChatProgressSink` chain
(`tests/hermes_cli/test_mission_chat_turn_phases.py`), plus a whole-chain row in
`tests/agent_runtime/test_progress.py`. Restoring the old sink filter reds five rows.
*Recovers 0 ms; makes every later claim checkable.* Risk: none (additive keys).

**Stage 1 — one visibility resolve per turn, memoized on identity. CODE LANDED
2026-08-23, commit `7f2c82f090`**; the live re-take is owed.

What landed:

1. **`agent_runtime/chat_lane_bundle.py`** — the lane's whole visibility (permission
   mode, MCP admission, enabled toolsets, blocked tool names, capability account,
   admission line, operating manuals, tool contract, permission state) resolved ONCE and
   memoized. It caches the **composition, not the probes**: the `check_fn` cache, its
   grace window and its re-probe backoff are untouched, and `registry.get_definitions`
   still re-probes every `check_fn` at agent construction — so a down backend still loses
   its TOOLS. What can go stale is the toolset NAME in the lane's accounting.
2. **The key is identity, not a clock:** persona revision · chat root · a permission
   fingerprint read FRESH on every lookup (mode / source / expiry / turns_remaining /
   mode blocks — so a `consume_turn` decrement or an operator restriction rebuilds, and
   an `unbounded` bundle can never reach a bounded turn) · root **and** active
   `config.yaml` `(mtime_ns, size)` · runtime root · entry-point lane ·
   `tools.registry.registry_epoch()`. Deliberately NOT the instance revision — no
   component reads the instance, and the instance still enters `_runtime_signature`
   directly.
3. **`tools/registry.py` grew the epoch**: `ToolRegistry.generation` (public; bumped by
   `register` / `deregister` / `register_toolset_alias`, hence every MCP refresh) plus a
   `_check_fn_epoch` bumped by `invalidate_check_fn_cache` — the availability half, which
   nothing announced before. `registry_epoch()` is their sum, compared for equality only.
4. **Bounded and non-poisoning:** one entry per (persona, chat root), replaced rather than
   accumulated, capped at 256; a bundle whose best-effort components faulted is served to
   that turn and never stored; the accessors deep-copy, so a consumer that decorates the
   capability account cannot write into the cache. `invalidate_chat_lane_bundles()` is the
   explicit hatch.
5. **Scope is the turn path only** — `mission_chat_turn_context`'s resolver defaults and
   `mission_chat_reply`. `apply_chat_lane_tool_scope`, the snapshot builder and
   `persona_prewarm` still resolve live, because those are routinely driven with a
   monkeypatched resolver rather than a changed config, which a config-keyed memo cannot
   see. (So §2.5's builder-side churn is Stage 5's, not this stage's.)
6. **The receipt:** `visibility_bundle_builds`, a third `PHASE_COUNTERS` member under the
   same absent-never-zero contract — a delta of a thread-cumulative counter, baseline at
   the handler anchor and second read at `agent_ready`, beside `registry_probe_rounds`.
   `0` on a warm steady-state turn; `>1` means something is re-resolving.

**Carried in from a live discovery mid-stage (14:45:14Z, hot_sessions ON): the resident
actor could never be reused, for a reason that had nothing to do with the registry.**
`_runtime_signature` hashed `asdict(instance)` whole, and a chat turn WRITES that row —
`state` flips, `updated_at`/`last_heartbeat_at` are stamped, and the handler writes
`skill_manifest_hash` back at the end of every turn. The second message of one neko chat,
45 s after the first with nothing changed, recorded
`resident_rebuild_runtime_signature_changed` + `resident_actor_reused=0`. The persona and
instance now contribute an explicit ACTOR-IDENTITY allowlist
(`PERSONA_IDENTITY_FIELDS` / `INSTANCE_IDENTITY_FIELDS`); a real `set-model` still rotates
the key, a turn's bookkeeping no longer does. **This is a precondition for Stage 2** — a
pre-constructed resident actor would have been discarded on its first reuse attempt.

*Expected: `registry_probe_rounds` → 0 on steady-state turns; ctx span from ~1.4–2.0 s to
<0.5 s. Receipt: `registry_probe_rounds=0` + `visibility_bundle_builds=0` +
`context_built<500` on three consecutive warm turns of one chat — plus, now that hot
sessions are on, `resident_actor_reused=1` on turn 2 of one chat.* The obs-span
(`prompt_observability`) half of the original estimate is NOT addressed here — that memo
is the separate `_SKILL_CATALOG_TTL_SECONDS` one and stays a 15 s TTL.
Risk: **medium**, as stated — staleness surface moves from "30 s" to "explicit
invalidation". Residue after the epoch: a backend that dies with nobody calling
`invalidate_check_fn_cache`, and a profile MCP declaration edited on disk before
registration. Both are named in the module's doctrine.

**Stage 2 — pre-construct the resident actor at chat-open (or first prewarm after
placement). CODE LANDED 2026-08-23, commit `bfde53b4ae`**; the live re-take is
owed. The registry + factory already existed (`profile_runner.py:1021-1040`); nothing called
them off the turn's critical path. *Expected: −2.5–3.5 s on every first turn of a chat (the
exact turn an operator is watching). Receipt: `agent_init_cold=false` +
`agent_ready−write_ahead < 700 ms` on the FIRST turn of a freshly opened chat.* Risk:
**medium** — construction touches `_WORKDIR_LOCK`/cwd and MCP admission.

**Two of this stage's preconditions were cleared on 2026-08-23 and both were invisible
until the other moved.** The operator turned `persona_chat.hot_sessions` on and restarted,
which finally made the resident-actor registry exist — and the very first pair of turns
proved the registry alone buys nothing, because `_runtime_signature` was keyed on
persona-instance row liveness and could not match twice (Stage 1, §5). With the identity
allowlist in, reuse is possible for the first time — and the live pair that opened this
stage's window confirms it: `17:33:01Z` (first message after a boot) `agent_init_cold=true`,
bootstrap 3,782 ms of which `agent_construct_ms=3000`, first byte 10.0 s; `17:33:17Z`
(second message, same chat) `resident_actor_reused=1`, bootstrap **62 ms**, first byte 3.4 s.
So the remaining defect is exactly and only the FIRST turn.

What landed:

1. **`agent_runtime/persona_chat_actor_prewarm.py`** — the whole lane. `prewarm_chat_actor`
   assembles the request one chat's first turn would build and runs it; a single daemon
   worker serializes the constructions; `request_chat_actor_prewarm` is the queueing hook
   and answers `registry_off` (no thread, no queue entry) whenever
   `persona_chat_runtime_registry()` is `None` — the state of every CLI one-shot.
2. **The construction runs the REAL path, not a copy.**
   `AgentRunRequest.prewarm_only` + `ProfileAgentRunner.prewarm` re-enter
   `_execute_agent_run`, so the agent is built inside `_WORKDIR_LOCK`,
   `persona_profile_context`, the workdir, the tool-execution / chat-root /
   terminal-envelope / skill scopes and this persona's MCP admission — and the `with`
   block unwinds normally, so the admitted MCP scope is torn down while the run still
   holds the lock, exactly as a real run's is. The early return sits immediately after
   the `acquire()` bookkeeping: no turn-scoped attributes, no compression threshold, no
   `agent_ready` notification, no conversation.
3. **Signature parity is by SHARED FUNCTION, not by agreement.** `_runtime_signature`
   became public as
   `mission_chat_turn_context.mission_chat_runtime_signature`; the prewarm calls it with
   the same arguments the builder passes, and reproduces every input through the turn's
   own authority (`_persona_by_id`, `apply_instance_model_overrides`,
   `_chat_effective_model_payload`, `_session_model_config`, `load_agent_runtime_config`,
   `chat_lane_bundle`). The tip and revision `acquire` compares come from
   `_persona_chat_native_tip` / `_persona_chat_native_revision` — the send path's own
   helpers, so a match is a REUSE and not a `disk_revision_changed` rebuild. The gate is
   an end-to-end test that asserts the prewarm's digest is byte-equal to the one
   `build_mission_chat_turn_context` puts on the turn context for the same chat, through
   real stores.
   *It cannot call the builder itself*: `build_mission_chat_turn_context` CONSUMES the
   queued-skill list, so warming through it would steal the operator's queued skills from
   the turn it is warming for.
4. **Two triggers.** *Boot* — `_prewarm_persona_chat_actors` runs THIRD on serve's one
   existing prewarm thread, behind the read-model build (the launcher's canvas waits on
   it) and behind the provider warmup (whose SDK import every construction would otherwise
   pay itself), warming at most `persona_chat.max_hot_sessions` chats,
   most-recently-active first, chosen from instances with a bound
   `default_chat_session_id`. *Chat-open* — both arms of `persona instance open-chat`
   (`_cmd_persona_instance_open_chat` and the mint arm
   `_cmd_persona_instance_open_new_chat`, which is the higher-value one: a freshly minted
   root has no turn that is not its first). Deliberately NOT
   `PersonaInstanceStore.open_chat`, which the send path re-enters on every turn — hooking
   there would fire a background construction against every live turn. A call-site census
   test pins exactly those two sites.
5. **Yielding is a rule, not a hope.** `profile_runner.agent_runs_in_flight()` counts real
   runs from `run()`'s entry (not from the lock — by the time a turn blocks on the lock the
   damage is done); the prewarm reads it before assembling and again before entering the
   scope stack, and stands down as `skipped_turn_active` rather than queueing behind a
   turn. One construction at a time, on one worker. The residual race — a turn arriving
   DURING a construction — is bounded by that one construction and is a NO-OP when the turn
   is for the same chat root, which is the common case at chat-open: that turn would have
   built this exact actor itself.
6. **Receipts** (07-observability's census): `persona_chat_actor_prewarm root=<id>
   outcome=<token> elapsed_ms=<n>` per item, and `persona_chat_actor_prewarm pass
   candidates=<n> queued=<n> skipped=<n> elapsed_ms=<n>` per boot pass. Outcomes are a
   closed set: `warmed`, `already_resident`, `registry_off`, `skipped_turn_active`,
   `skipped_no_chat_root`, `skipped_persona_unresolved`, `skipped_profile_unready`,
   `skipped_construct_failed`. Ids and timings only — never a display name, never a
   resolved toolset.
7. **No new config key, and that is a decision.** A `prewarm_on_boot` flag was written and
   withdrawn: every field of `PersonaChatConfig` is projected onto the read-model wire
   (`core.runtime_config.persona_chat.*`), so adding one reds the stream-contract goldens
   and is a cross-stack landing (regenerate fixtures, mirror bytes into the Launcher,
   update both manifests). `hot_sessions_enabled` already gates the lane end to end — with
   no registry there is nowhere to put a pre-built actor. The refusal is pinned by a field
   census on the dataclass so it is re-taken rather than drifted into.

**The one input it cannot know, stated rather than guessed:** `--agents-file`, the
operator's workspace `AGENTS.md`, which the launcher attaches per turn from a client-side
selection. The prewarm warms with no workspace file. A WORKSPACE-BOUND chat therefore
mismatches on its first turn and `acquire` rebuilds — that turn pays exactly what it pays
today and the prewarmed actor is discarded, so the residue is wasted background work, never
a wrong answer. Fabricating a path instead would ground a real agent's terminal at a
directory the operator never chose.

**Nothing is sent to a model.** Construction builds an OpenAI client object
(`OpenAI client created (agent_init, shared=True)`) over already-resolved credentials; the
first byte on the wire is `codex_stream_request`, inside `run_conversation`, on the far side
of the early return. No prompt, no completion, no token spend. Two side effects ARE inherited
from the real path and are named rather than denied, because both are the first turn's own
work performed earlier: (1) `resolve_runtime_provider` reads credentials — a local
`auth.json` read on `openai-codex`, but a Vertex persona mints an OAuth2 token and a Nous
pool may refresh an expired agent key, and its result is cached for the turn behind it;
(2) MCP admission spawns this persona's declared servers and tears them down on the way out.

**Stage 2a — a refused reuse NAMES the input that moved. CODE LANDED 2026-08-23**
(working tree); the live re-take is owed. Not a latency stage: it is the instrument
Stage 2's claim could not be checked without, plus the two identity fixes it convicted.

*The receipt that forced it (2026-08-23T19:03Z, serve on `bfde53b4ae`+docs).* The boot
prewarm warmed root `persona_chat_personainst_neko_supervisor_agent_f6f7a51b_3b7230c1b8d2`
at `19:03:01Z` (`outcome=warmed elapsed_ms=469`). The next THREE turns of that one root —
`19:03:10` / `19:03:23` / `19:03:40`, an `agent-chat-send` relay each — recorded
`resident_actor_reused=0` and `resident_rebuild_runtime_signature_changed=1`. Every one.
Construction was cheap on those turns (`agent_construct_ms` 10 / 15 / 12, warm TTLs), so
nothing looked broken — but reuse never happened, which is the entire benefit Stage 1's
allowlist fix and Stage 2's prewarm exist to buy. The composite key could only say that
*something* moved, so the diagnosis had to be done by hand against the live store.

*What the store could and could not settle.* Read-only, from the two persisted
observability rows for that root plus the turn records:

| suspect | live answer |
|---|---|
| `surface_prompt` | EMPTY on both turns (`surface_prompt_is_blank: true`) — not the mover |
| `workspace_agents` receipt | byte-identical across both turns (`AGENTS.md`, `sha256 682CC0E9…`, 1,859 B) — not the turn-to-turn mover, but IS the prewarm→turn-1 mover, since the prewarm cannot know `--agents-file` |
| `model_selection`, `session_model_config`, `skill_manifest_hash`, HUD revision | identical across both turns |
| persona / instance identity revisions, tool contract, permission state | byte-stable at rest, computed twice 3 s apart |
| `visibility_bundle_builds` | `0` / **`2`** / `0` — the chat-lane bundle's own key moved twice INSIDE the middle turn |

That last row is the one that pointed: turns 1 and 3 were memo HITS on the bundle, turn 2
rebuilt it twice, and the bundle's content is what the signature folds as `tool_contract`
and `permissions`. The bundle key carries `tools.registry.registry_epoch`, and under the
shipped default permission mode (`unbounded`, `SHIPPED_DEFAULT_PERMISSION_MODE`; the live
root config sets no `tool_permissions` block and the chat has no stored grant)
`_enabled_toolsets_for_chat` resolves `all_registered_toolsets()` and
`permission_state_for_chat` resolves `blocked_tools` over EVERY tool registered in the
process. In a warm multi-persona `harness serve` — which is what this is; the same log
window shows a full plugin-discovery pass (`54 found, 47 enabled`) at `15:03:02` local,
between the prewarm and turn 1 — that set moves whenever anything registers or
deregisters.

*What landed.*

1. **The signature is composed once and folded twice.**
   `mission_chat_runtime_signature_components` returns the flat component dict;
   `mission_chat_runtime_signature_from_components` is the ONE fold to the composite key;
   `mission_chat_runtime_signature_digests` turns the same dict into a per-component
   digest map, which rides `MissionChatTurnContext.runtime_signature_digests` →
   `mission_chat_reply(runtime_signature_components=)` →
   `AgentRunRequest.persona_chat_runtime_signature_components` →
   `PersonaChatRuntimeRegistry.acquire`. The prewarm seeds the same map on the entry it
   registers, so the prewarm-vs-turn half of the diff is answerable too.
2. **`acquire` diffs the map and reports NAMES.** It returns a fourth element (the moved
   component names) and logs one line, `resident_signature_diff root=<id>
   components=<a,b>`. Reuse is still decided by the composite alone — a second authority
   for "is this the same actor" is how two answers drift. A caller that supplied no map
   gets `()`, not a guess.
3. **The names reach the durable record inside the existing vocabulary.** The runner
   writes `resident_rebuild_component_<name> = 1` per moved component, which
   `safe_turn_profile_timing` already admits (`resident_rebuild_*`, ints). A joined
   string would be free text and would be dropped at that gate by construction — so the
   diff rides as flags, which is also what makes it queryable across turns. NAMES only:
   the digests are one-way and no value is ever emitted, because the components include
   prompt- and policy-adjacent material (`surface_prompt_sha256`, the tool contract).
4. **Convicted and removed: the operator-shaped half of `permissions`.** The key folded
   the whole `permission_state_for_chat` answer — `blocked_tools` entries, `workdir`,
   `repo_scope`, `can_run_terminal` / `can_mutate_files`, `expires_at`,
   `turns_remaining`. None of it reaches the agent factory: `_execute_agent_run` builds
   an actor from `enabled_toolsets` and `blocked_tool_names`, which the key already
   carries verbatim as `tool_contract`, plus scopes derived from the permission MODE. So
   the projection was the row-liveness defect of `7f2c82f090` wearing different clothes,
   and under `unbounded` its `blocked_tools` list is exactly the registry-shaped thing
   that moves in a warm multi-persona process. What stays is `{mode, source, expired}` —
   the facts that decide what is constructed. A grant decrementing 5 → 4 changes nothing
   about the actor; the turn it reaches 0 flips `expired`, which is still in the key.
5. **Convicted and removed: `current_chat_goal` from `INSTANCE_IDENTITY_FIELDS`.** Read
   the allowlist's own rule consistently — `goal_id` is excluded because it "renders into
   the HUD, which rides the volatile tail and is therefore not part of the cached actor at
   all", and `current_chat_goal`'s only readers are the chat-list TITLE
   (`persona_chat_history`) and the operator projections / situational HUD. A
   `persona instance steer --goal` changes what the next turn SAYS, not what its actor IS.
   (Not the live churn source — the chat send never writes it — but a rebuild nobody
   should ever have paid.)
6. **`tool_contract` STAYS, and the doctrine says why.** However volatile it is, the actor
   is constructed from those two lists and `_prepare_resident_persona_chat_agent` does not
   re-apply them on reuse — it refreshes callbacks, the cache scope and the iteration cap
   and nothing else. An actor whose tool surface moved is stale, so that rebuild is
   correct behaviour. The receipt's job is to say so by name instead of leaving it
   indistinguishable from a defect.

7. **Convicted and removed: the whole config DOCUMENT from
   `relevant_config_revision` — and this one the instrument caught in ONE READ.**

   *The instrument's first field validation (2026-08-23T21:38:29Z, serve on
   `14271f261f`).* Root
   `persona_chat_personainst_neko_supervisor_agent_f6f7a51b_66a438245225`:

   ```
   21:38:29Z INFO agent_runtime.persona_chat_continuity: resident_signature_diff
     root=persona_chat_personainst_neko_supervisor_agent_f6f7a51b_66a438245225
     components=relevant_config_revision
   ```

   …and again at `21:39:07`, `21:39:19`, `21:40:36`, `21:40:40`. Five consecutive turns
   of one chat, five rebuilds, ONE component named every time. No store archaeology, no
   cross-referencing two persisted observability rows by hand: the line named the input
   and the whole diagnosis started from a single grep. That is what items 1–3 were built
   for, and it is the first time they were asked in the field.

   *What it convicted.* Not a config edit: no config file was written in that window
   (root `config.yaml` hours older, the profile's days older), and
   `_revision_hash(_as_plain(load_agent_runtime_config()))` is deterministic — equal
   twice in one process and equal across two fresh processes. What moved was not the
   file, it was **which file**. `load_agent_runtime_config()` resolves
   `get_hermes_home()/config.yaml`; with no context-local override on the turn's thread
   that is the process-global `HERMES_HOME`, and `profile_context.persona_profile_context`
   rewrites that variable for the width of a profile binding (its own docstring states
   the invariant: sound only while runs are serialized by `profile_runner._WORKDIR_LOCK`).
   The readiness walk behind every snapshot build enters it once per persona — in the
   same `harness serve` process that hosts the chat turns, on another thread, every few
   seconds (the same log window: `snapshot_agents_readiness` at 17:38:29 / :32 / :36 /
   :39 / :45 / :48 local, pid `28624`, while turns ran on `harness-serve_1` and
   `harness-serve_2`). So the document a turn hashed was whichever profile the walk
   happened to be standing in, and two turns of an unchanged chat could not agree.

   *The fix, by the doctrine items 4 and 5 already set.* `relevant_config_revision` now
   hashes an ALLOWLIST projection (`ACTOR_CONFIG_IDENTITY_FIELDS`) through the same
   `_identity_revision` the persona and instance use. The allowlist is EMPTY, and that is
   a finding rather than a stub: every config block that reaches a constructed actor
   reaches it RESOLVED, and each resolved form is already a component — the runtime model
   defaults as `provider`/`model`/`api_mode`, `personas.<id>.*` as `persona_revision`,
   `store_root` as `runtime_root`, `tool_permissions.default_mode` as `permissions.mode`,
   and `mcp_admission` + the chat-lane toolset knobs as `tool_contract` (admission is an
   input to the bundle's `_enabled_toolsets_for_chat`, so it is in those two lists
   verbatim). `terminal_envelope.grants` binds a scope per run; `mission_chat.*` is
   per-turn and its compaction cap is re-applied even on a reused actor; `persona_chat.*`
   decides whether an actor is resident at all, never what one is. Empty is also the only
   projection that is stable under an AMBIENT document: any non-empty projection can still
   move for a reason that has nothing to do with this chat. A field that genuinely decides
   what an actor IS goes there by name and the key rotates on it again —
   `test_a_NAMED_actor_config_field_still_rotates_the_key` witnesses that the mechanism is
   live rather than decorative.

   *The hazard was NOT fixed here — it was fixed at the source next, and this debt is now
   CLOSED.* This item fixed the KEY: the reuse key stopped depending on `HERMES_HOME`
   holding still. What it left open was every other ambient reader on a turn's thread,
   notably `chat_lane_bundle`'s key carrying the ACTIVE `config.yaml`'s `(mtime_ns, size)`,
   so the same race still forced visibility-bundle rebuilds — and the live receipts said so
   in milliseconds: a bundle-free turn built context in 453 ms, while turns overlapping a
   readiness walk billed 1,796 / 2,343 ms with `visibility_bundle_builds=3/6` and 11 probe
   rounds. The hazard owned ~1.3–1.9 s of turn variance.

   **What shipped.** `profile_context.persona_profile_context` grew a context-local-only
   mode (`export_env=False`) reached through a named sibling,
   `profile_context.persona_profile_scope`; one authority, one body, the flag decides only
   whether the `os.environ` mirror is written. `profile_readiness_for_persona` binds through
   that sibling. The env-writing mode is untouched and keeps its `_WORKDIR_LOCK` invariant
   comment — `profile_runner` still uses it, and must, because a ContextVar never crosses a
   subprocess boundary.

   **What the audit had to answer first**, since the seam's own comment says the writes are
   pinned by live readers. Per env write → reader that pins it → reachable from the walk:
   `HERMES_AGENT_RUNTIME_ROOT` → the legacy terminal envelope → **not reachable** (the walk
   runs no tools). `HERMES_HOME` → in-process plugins reading it raw → **not reachable** (the
   walk loads no plugin), and every home resolution it does perform goes through
   `get_hermes_home()` (ContextVar-first) or an explicit path; the one raw-env reader,
   `get_default_hermes_root()`, **collapses to the same answer** either way because a
   binding's `profile_home` is always `<root>/profiles/<name>`. `HERMES_AUTH_HOME` →
   `hermes_cli.auth._global_auth_file_path` → **REACHABLE, on every walk**, via
   `_provider_issue` → `load_pool` / `probe_runtime_provider` → `read_credential_pool` →
   `_load_global_auth_store`; without a remedy the walk's provider probe would have judged a
   persona against `<root>/auth.json` instead of the head profile's `auth.json` (both files
   exist on the live install). Closed by giving that authority a second channel:
   `hermes_constants.get_hermes_auth_home()` (ContextVar first, env second), which
   `_global_auth_file_path` and the readiness provider memo key now read. `HOME` → POSIX
   `expanduser` / `Path.home()` → **reachable in principle, and the one named residue**: no
   context-scoped hook exists, so a `~` expanded under the binding (a
   `skills.external_dirs` entry, the `~/.codex` / `~/.qwen` credential singletons) resolves
   to the process home rather than `<profile>/home`. Unobservable on native Windows
   (`ntpath.expanduser` consults `USERPROFILE`), and `external_dirs` is `[]` in every profile
   on this install. It is written into `persona_profile_scope`'s docstring rather than left
   to be rediscovered.

   **The sibling, closed in the same wave.** Retiring the readiness walk's env writes left
   ONE unserialized binding on the snapshot lane, and it was the bigger one:
   `snapshot_prompt_observability` enters `mission_chat_prompt_observability`'s
   `skill_profile_context` once per roster instance, and that section bills
   `prompt_observability:4520` against `agents_readiness:4366` on the 2026-08-22 cold boot.
   It now binds through `persona_profile_scope` too. The audit answered the same way: no
   subprocess, no plugin dispatch, and — unlike readiness — no `hermes_cli.auth` path at all,
   because this block runs no provider probe, so the `HERMES_AUTH_HOME` reader is not even
   reachable here. Skill discovery resolves through `get_hermes_home()`
   (`skills_tool._skills_dir`, `skill_utils.get_skills_dir`, `get_config_path`); the
   per-persona hash check takes an EXPLICIT `hermes_home=`; the realm rows are a sidecar file
   read; `paths.store_root()` collapses because the env mode exports the root it resolved
   BEFORE the override. The one axis carrying weight, `get_default_hermes_root()` (raw
   `HERMES_HOME`, reached five ways from inside the binding), collapses structurally — a
   binding's `profile_home` is always `get_profile_dir`'s
   `get_default_hermes_root()/profiles/<name>`, and that function is a fixed point over
   exactly those paths — and that is now PINNED by
   `test_the_profiles_root_survives_dropping_the_HERMES_HOME_write`, parametrized over all
   three ambient layouts including `HERMES_HOME` unset, rather than argued in prose. Same
   `HOME` residue, same bound.

   That site turned out to carry a second lane nobody had named:
   `persona_commands._cmd_mission_chat_message` calls the same function at
   `observability_built`, BEFORE `profile_runner` installs its own locked binding. So a chat
   TURN was rebinding the process for every concurrent turn (`harness-serve_1` /
   `harness-serve_2`) and for the snapshot builder — the hazard ran in both directions, and
   the one switch closes both.

   *Receipt to take on the next restart:* on turns that overlap a `snapshot_agents_readiness`
   or `prompt_observability` build, `visibility_bundle_builds=0` and a
   `request_received → context_built` span in the same band as a non-overlapping turn. With
   both snapshot bindings context-local and the turn lane's own binding no longer
   process-global, `resident_rebuild_component_relevant_config_revision` should not appear at
   all.

*What this does NOT claim.* The live turns were not re-run — the 19:03 serve was on
`bfde53b4ae` and the 21:38 one on `14271f261f`; neither was restarted onto this tree.
Removals 4, 5 and 7 are convicted on what the actor is built from, which is a code fact
and does not need a live re-take; whether they are sufficient to make those chats reuse
their actors is exactly what the next serve restart answers, and the
`resident_rebuild_component_*` flags are what will answer it in one read instead of a
store archaeology session — as removal 7 already demonstrates.

*Receipt to take on the next restart:* on the second turn of one chat, either
`resident_actor_reused=1`, or a `resident_rebuild_component_<name>` naming the remaining
mover. A first turn of a workspace-bound chat is EXPECTED to show
`resident_rebuild_component_workspace_agents` — that is the prewarm's documented blind
spot finally self-reporting rather than being indistinguishable from a defect.

**Stage 3 — prologue diet, gated on the Stage-0 split data.** Cache tool-schema
serialization per toolset tuple and verify the system-prompt restore path actually hits
on turn 2+ (both live inside the `provider_request_started→request_assembled` span).
*Expected: −0.5–1.0 s per turn. Receipt: `request_assembled−provider_request_started`
median under 700 ms across a week.* Risk: low-medium. **Explicitly gated** by the
standing rule in `planned/mission-chat-admission-latency.md` §5: no prologue work until
the split is a distribution, not one turn.

**Stage 4 — SessionDB open-side: measure, then pool.** Add a timing around
`_default_persona_session_db()` (Stage 0 can carry it); if it bills >100 ms warm, hold
one writer handle per serve process (the close-side checkpoint discipline from IC-2
stays intact — pooling changes WHEN close happens, not whether). *Expected: unknown
until measured. Receipt: the new timing key.* Risk: low for measuring; medium for
pooling (multi-process WAL discipline is why per-open close exists).

**Stage 5 — stop paying the snapshot builder during live turns.** `builds_overlapped`
is already recorded; if Stage-0 data shows turn spans correlate with overlap (the
4a80f05e boot window suggests they do), defer demote-cadence led builds while a turn is
between `write_ahead` and `stream_done`. *Expected: removes the unattributed inflation,
sharpens every other number. Receipt: span medians at `builds_overlapped=0` vs `>0`.*
Risk: medium — the launcher's HUD freshness rides those builds; deferral must be
bounded (hundreds of ms), not a starvation.

Deliberately NOT staged: re-tuning the 15/30 s TTL constants upward. That trades the
measured storm for a staleness window on every consumer (snapshot drawers included)
without removing the per-turn re-composition that Stage 1 removes properly.

---

## 6. Opening gate

**Do not start Stage 1+ until one turn record from a serve running the Stage-0 tree
shows `request_assembled`, `agent_init_cold` and `profile_timing` present.**

**Status 2026-08-23: the gate opened, and the first thing through it changed a stage.**
The operator restarted a serve on the Stage-0 tree with `persona_chat.hot_sessions`
enabled; live turn records now carry the runner's `profile_timing` (turn `14:45:14Z`
shows `resident_rebuild_runtime_signature_changed` and `resident_actor_reused=0`), which
is exactly the class of fact the instrument existed to surface — and it falsified the
assumption behind Stage 2 within two turns (§1 update). Stage 1 proceeded on that
evidence. **Still owed on this gate: `request_assembled` observed on a live record.**
Until then §2.4's assembly-vs-TTFB split rests on the one carry-forward measurement. Note what the first attempt at
this gate proved — the serve HAD been restarted on HEAD and the keys still did not appear
(§1), because the causes were a redaction/noise filter and a default-off config, not
vintage. Acting on Stage 1/2/3 without the restored instrument would repeat the exact
failure mode this audit exists to end: remedies measured against numbers nobody can
re-take.

Secondary gates, inherited:

- **Honesty contract** (`mission_chat_phases.py:18-50`): absent-never-zero,
  monotonic-only, first-mark-wins, release-visible. A fix that unmeasures a phase is a
  regression.
- **Do not re-quote `chat_lane_scope_ms=2421` as a turn cost.** Verified here (§3 H2):
  it is the *unwarmed create* subphase; the warm create is 859/15 ms. Doc 05's
  carry-forward row carries the annotation as of 2026-08-23.
- **The provider half stays out of scope**: the raw luna floor is 0.74–0.92 s at any
  effort; live mission-chat TTFB runs 2.2–3.5 s because the model REASONS at
  effort=medium before its first visible token (canonical explanation: doc 08's luna
  row — a 96%-prompt-cache turn still showed 6.1 s ttfb, so it is not ingestion). The
  alice-lane free-tier ruling is CLOSED: the operator ruled 2026-08-23 and both alice
  instances carry the gpt-5.6-luna/openai-codex instance override
  (`model_override_issued_at: 2026-08-22T14:49:24Z`; see
  `planned/mission-chat-admission-latency.md` §5).

## 7. Uncertain / unverified, stated plainly

### 7.4 `visibility_bundle_builds=2` inside one turn — diagnosed, deliberately NOT changed

Live: `0` / `2` / `0` across the three 19:03 turns of one root (and `2` on the 17:33:17
turn before Stage 2 landed). Two builds means the chat-lane bundle's key moved once
mid-turn, between the context builder's first bundle read and a later one. The key carries
`tools.registry.registry_epoch`, which is bumped by every registration change — including
the turn's own MCP admission register/deregister cycle (turn 3 recorded
`mcp_admission_ms=45`) and any concurrent run's, since `harness serve` runs turns from
several personas on pooled threads in one process.

The tempting fix is to read a registration generation that EXCLUDES the per-turn MCP
scope's own cycle. **Refused, and the reason is a correctness rule, not taste.** It would
be admissible only if the bundle's content did not depend on registry state — but under
`unbounded` its `enabled_toolsets` IS `all_registered_toolsets()`, and
`scope_toolsets_to_admission` recognizes another persona's MCP toolsets through a LIVE
alias read of the same registry. A bundle pinned across a registration change can
therefore hand the next turn a toolset name that is no longer registered, or miss one that
is. That is a wrong answer about what the turn may do, traded for a memo hit; the epoch
stays in the key. The cost is bounded and now visible: at most one extra resolve on a turn
that touches MCP registration.

### 7.5 `runtime_resolve_ms` 878 / 0 / 1589 — the memo's TTL is write-time, not use-time

`_resolve_request_runtime` (`profile_runner.py:1756-1785`) memoizes on
`(HERMES_HOME, provider, model, (mtime_ns,size) of the profile's config.yaml and .env)`
for `RUNTIME_RESOLVE_CACHE_TTL_SECONDS = 30`. Nothing in production calls
`reset_runtime_resolve_cache` — the docstring's "profile teardown" has no caller — so a
cross-persona turn cannot be wiping it. The stamp is written at the COLD resolve and is
never refreshed on a hit, so the entry dies 30 s after that resolve however heavily it is
used: turn 1 at `19:03:10` resolved cold (878 ms), turn 2 at `19:03:23` hit (0 ms), turn 3
at `19:03:40` — **30.3 s after turn 1's write** — missed by three tenths of a second and
paid 1,589 ms.

**Not fixed, on purpose.** A sliding window (restamping on a hit) makes the cached
credential's effective age unbounded, and the constant's own doctrine pins the opposite
invariant: it must stay well under the refresh skew so a served credential has ≥ 90 s of
validity left. The honest options are a shorter TTL with a *separate* freshness floor, or
leaving it — and neither belongs in this stage. The remaining unexplained gap is
prewarm→turn 1 (9 s apart, should have hit): either the prewarm thread resolved under a
different `HERMES_HOME` than the turn's profile context, or one of the two stamped files
was rewritten between them. Both are one restart's worth of receipts away, and the memo
already writes `runtime_resolve_cached=0/1` to tell them apart.


- The **second plugin-discovery walk** at 22:04:18 (139 ms,
  `Plugin discovery complete: 54 found, 47 enabled`) during the 4a80f05e window: the log
  carries no pid on those lines, so whether it ran in the serve or a sibling hermes
  child is unattributed. Small, but it fired inside the turn window.
- The **SessionDB open cost** (H3) is a code-shape finding, not a measured one.
- The **prologue split** on the live turns is inferred from log timestamps
  (23.15 → 24.24 → 24.63) plus one carry-forward measurement; the standing mark is not
  yet on any record.
- `send_to_admit`'s ~110 ms transport share is derived from ONE turn's
  launcher-vs-record reconciliation; other turns may differ under launcher load.
- **Stage 2's own receipt is OWED, on the same terms as Stage 1's.** The lane is pinned by
  tests — the prewarmed actor being REUSED by the next real turn rather than rebuilt, the
  end-to-end signature parity against real stores, the stand-down under a genuinely
  concurrent run, the call-site census that keeps the hook off the per-turn seam, the boot
  cap and ordering — but no live turn record written by a serve running it has been read.
  Until one first-turn record of a freshly opened chat shows `agent_init_cold=false` and
  `agent_ready − write_ahead < 700 ms`, the −2.5–3.5 s is a prediction. The second thing to
  read on that serve is the `persona_chat_actor_prewarm` lines themselves: a pass whose
  items all read `skipped_turn_active` means the yield rule is firing too eagerly, and a
  first turn that still reads `agent_init_cold=true` next to a `warmed` line for its root
  means a signature input the prewarm cannot reproduce (check for `--agents-file` first).
- **Stage 1's own receipt is OWED.** The code landed and is pinned by tests (memo hit,
  every keyed input rebuilding, the degraded-bundle rule, the copy-on-read rule, the
  actor-identity allowlist, the new counter reaching the record), but no live turn record
  written by a serve running it has been read. Until one shows
  `registry_probe_rounds=0` + `visibility_bundle_builds=0` on three consecutive warm turns
  of one chat — and `resident_actor_reused=1` on turn 2 — the ctx-span improvement is a
  prediction, not a measurement. The same rule this audit exists to enforce applies to its
  own remedies.
