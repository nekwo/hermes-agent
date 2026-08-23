# Planned — chat-turn prep cost (the ~4 s of hermes between admission and the provider)

**Status:** measured and decomposed, not remediated. **Owner doc:**
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
`hermes_cli/harness_parts/persona_commands.py:2003`). All turns below are
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

**Vintage caveat (feeds the opening gate):** none of the sampled records carries
`request_assembled` or `agent_init_cold`, though both are in HEAD
(`mission_chat_phases.py:84`, `:92`; emitter `agent/conversation_loop.py:2469`; flag site
`persona_commands.py:3391-3396`). The running serve predates commits `74702c193e` /
`785a35beae` (or the marker payload is not reaching `_stream_progress` — less likely,
same wiring as the counters that DO appear). Until a restarted serve writes those keys,
the assembly-vs-TTFB split inside `→first_byte` rests on the one measured carry-forward
(1,762 ms, turn `c59ab99e`, quoted at `mission_chat_phases.py:360-369`) plus the log
cross-reference above.

---

## 2. Who owns each span

### 2.1 `request_received → context_built` (0.15–3.9 s, typically 1–2 s warm)

One span, four owners, in execution order inside `_cmd_mission_chat_message`:

1. **Admission guards + session resolution** (`persona_commands.py:2003-2470`): config
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
`persona_commands.py:2008`/`:3203`) proves the sweep runs **many times per turn**: a
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
exactly why the span is bimodal. **Nothing pre-constructs the actor before turn 1** —
not `persona_prewarm` (visibility memos only, §3), not serve boot
(`_prewarm_provider_runtime`, `hermes_cli/harness_parts/serve.py:1029-1057`, warms the
SDK import, the SSL context, and the *shared* parts of `get_tool_definitions` — not a
persona-shaped agent).

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
`mission_chat_phases.py:360-369`). The `request_assembled` mark that makes this a
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
then throws the component results away.

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
| Agent construction on chat-root turn 1 | §2.3 | **(b)** per-chat-root lazy; no prewarm covers it | ~3.0–3.6 s (once per chat root, and again on signature change) |
| Profile `.env` + context install | §2.3 | **(b)** | ~0.2–0.8 s |
| SessionDB cold open ×1 + history read ×2 | §2.1.1/2.2 | **(a)/(d)** | unmeasured |
| Prologue + request assembly | §2.4 | mix **(a)** (schemas, system prompt) + **(c)** (hooks, message) | ~1.1–1.8 s |
| Write-ahead persist, lease, guards, HUD deltas | §2.1 | **(c)** genuine | ~0.2–0.4 s |
| Snapshot-build GIL contention | §2.5 | **(d)** infrastructural | unattributed inflation |
| Provider TTFB | provider | **(c)** | 1.0–3.1 s (luna) |

---

## 5. Stages (ordered by value; none started)

**Stage 0 — restore the instrument before touching anything (opening gate, §6).**
Restart the serve on HEAD; confirm the next turn record carries `request_assembled` and
`agent_init_cold`. Additionally persist the runner's timing dict
(`runtime_resolve_ms`, `agent_construct_ms`, MCP admission ms,
`conversation_call_ms` — built at `profile_runner.py:787,828,962,1051` and currently
dropped after streaming) onto the turn record or the observability context, so Stages
1–5 each have a before/after receipt that is not a log-grep.
*Recovers 0 ms; makes every later claim checkable.* Risk: none (additive keys).

**Stage 1 — one visibility resolve per turn, memoized on the signature.**
Compute the resolver bundle (enabled toolsets, blocks, capability drops, admission line,
tool contract, permission state) ONCE per turn and reuse it across the ≥4 call sites in
`build_mission_chat_turn_context`/`apply_chat_lane_tool_scope`; memoize the bundle
across turns keyed on the already-computed `_runtime_signature` components + an explicit
registry epoch (bumped by `invalidate_check_fn_cache`, `tools/registry.py:373`), instead
of the 15/30 s TTLs. *Expected: `registry_probe_rounds` → 0 on steady-state turns;
ctx span from ~1.4–2.0 s to <0.5 s; obs span from ~0.45 to ~0.2 s. Receipt:
`registry_probe_rounds=0` + `context_built<500` on three consecutive warm turns of one
chat.* Risk: **medium** — staleness surface moves from "30 s" to "explicit
invalidation"; a missed invalidation hides a genuinely-down backend until epoch bump.
Keep the check_fn grace machinery untouched; cache the *composition*, not the probes.

**Stage 2 — pre-construct the resident actor at chat-open (or first prewarm after
placement).** The registry + factory already exist (`profile_runner.py:967-986`); an
`open_chat`/placement hook that runs `_construct_agent` through the same
`acquire()` off the turn's critical path converts §2.3's 3.0–3.6 s first-turn cost into
background boot cost. *Expected: −2.5–3.5 s on every first turn of a chat (the exact
turn an operator is watching). Receipt: `agent_init_cold=false` + `agent_ready−write_ahead
< 700 ms` on the FIRST turn of a freshly opened chat.* Risk: **medium** — construction
touches `_WORKDIR_LOCK`/cwd and MCP admission; must run under the same scopes as a real
run and must not race a genuinely concurrent turn (the registry's signature check
already guards reuse correctness).

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

**Do not start Stage 1+ until Stage 0 has landed and one restarted-serve turn record
shows `request_assembled` and `agent_init_cold` present.** Every sampled live record
lacks both keys (§1 vintage caveat), so today the assembly-vs-provider split and the
cold/warm attribution inside `agent_ready` rest on one carry-forward turn and log
eyeballing. Acting on Stage 1/2/3 without the restored instrument would repeat the
exact failure mode this audit exists to end: remedies measured against numbers nobody
can re-take.

Secondary gates, inherited:

- **Honesty contract** (`mission_chat_phases.py:18-50`): absent-never-zero,
  monotonic-only, first-mark-wins, release-visible. A fix that unmeasures a phase is a
  regression.
- **Do not re-quote `chat_lane_scope_ms=2421` as a turn cost.** Verified here (§3 H2):
  it is the *unwarmed create* subphase; the warm create is 859/15 ms. Doc 05's
  carry-forward row should be annotated accordingly when doc 05 is next touched.
- **The provider half stays out of scope**: luna TTFB 0.7–3.1 s here is the floor the
  hermes work is measured against, and the alice-lane free-tier ruling is still owed to
  the operator (see `planned/mission-chat-admission-latency.md` §5).

## 7. Uncertain / unverified, stated plainly

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
