# Field notes — chat-turn prep cost, Stages 3–5 (2026-09-01)

Running record for the branch `perf/chat-turn-prep-stages-3-5`, cut from
`98d43d0c8604adeebfeacd074382f66022e0f5dd`. Authority:
[`chat-turn-prep-cost.md`](chat-turn-prep-cost.md) §5.

Everything here is either a measurement taken before the fix, or a correction to
the plan. Where a note corrects the plan, it says what the plan claimed, what the
live record says, and what was built instead.

---

## 0. The opening gate — VERIFIED OPEN, and on fresher records than the brief cited

The gate (§6) asks for a turn record from a serve on the Stage-0 tree carrying
`request_assembled`, `agent_init_cold` and `profile_timing`. Re-verified here
read-only against `X:/Eternia/.hermes/agent-runtime/mission_chat_turns/`
(323 files, 135 records carrying a `phases` block).

Two records dated **2026-09-01** carry all three, from a serve at **05:43–05:44Z**
— later than the 01:43Z record the brief pointed at:

| `started_at` (UTC) | file |
|---|---|
| `2026-09-01T05:44:03.882851Z` | `persona_chat_personainst_neko_supervisor_agent_2e94fab3_9e7368053df1_c1b09ed80a92.json` |
| `2026-09-01T05:43:48.388226Z` | (same root) |

Both carry `request_assembled`, `agent_init_cold`, `registry_probe_rounds`,
`visibility_bundle_builds`, `builds_overlapped` and a full `profile_timing`
block. **The gate is open. Stage 3 was built against a real split.**

## 1. Re-measurement BEFORE the fix — the baseline this branch is judged against

Freshest seven records carrying `phases`, all seven of the seven most recent
turns in the store (2026-08-30 and 2026-09-01 windows). `assemb` is the Stage-3
span, `provider_request_started → request_assembled`; `rqbld` is
`profile_conversation_request_build_ms`.

| `started_at` | ctx | obs | WA | ready | prs | **assemb** | **rqbld** | ovlp | probes | reuse |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-09-01T05:44:03 | 375 | 593 | 625 | 655 | 655 | **703** | **1** | 1 | 0 | 1 |
| 2026-09-01T05:43:48 | 3718 | 4233 | 4250 | 6640 | 6640 | **2000** | **128** | 2 | 7 | 0 |
| 2026-08-30T04:41:53 | 2406 | 2921 | 2953 | 3906 | 3906 | 937 | 1 | — | — | 0 |
| 2026-08-30T04:41:37 | 375 | 843 | 858 | 2686 | 2686 | 939 | 1 | — | — | 0 |
| 2026-08-30T04:40:41 | 781 | 1265 | 1281 | 1343 | 1343 | 657 | 1 | — | — | 1 |
| 2026-08-30T04:40:28 | 265 | 562 | 578 | 4578 | 4578 | 1125 | 64 | — | — | 0 |
| 2026-08-30T04:28:37 | 155 | 436 | 452 | 3000 | 3014 | 2422 | 51 | — | — | 0 |

(The `ovlp`/`probes` columns are only populated where the record carried them;
the 08-30 rows were read through a scanner that only lifted the two 09-01 rows'
counters. Both 09-01 rows carry `builds_overlapped` = 1 and 2 respectively.)

Also read off the same two 09-01 records:

* **`session_db` visibility: NONE.** No key anywhere on a turn record names the
  SessionDB open. That is Stage 4's entire point, and it is confirmed rather than
  assumed.
* **`builds_overlapped` is 1 and 2** on the two freshest turns — a live snapshot
  build overlapped BOTH. That is Stage 5's premise, standing in the freshest data
  rather than the 2026-08-22 carry-forward.
* `profile_agent_init_tool_setup_ms` = **1674** on the cold turn (of an
  `agent_construct_ms` of 1683 — 99.5% of the whole construction), and 853 on the
  08-30 cold turn.
* `profile_conversation_pre_api_hook_ms` = **0** on every record.

---

## 2. FALSIFIED: Stage 3's named site is not where the cost is

**The plan says** (§5 Stage 3, and §3 H5): cache tool-schema serialization,
which lives in `request_build` inside `agent/conversation_loop.py`, expecting
**−0.5–1.0 s per turn**.

**The live record says** `profile_conversation_request_build_ms` — the span that
*contains* that serialization — bills **1 ms** on every warm turn (4 of the 7
above) and 51–128 ms on a cold one. `pre_api_hook` is 0. So the plan's named
remedy addresses, at most, single-digit milliseconds of a 657–2,422 ms span.

**Why, in code.** On the live lane (`api_mode == "codex_responses"`) the
serialization is `agent/codex_responses_adapter.py:_responses_tools` — a loop
over ~30–40 already-built tool dicts that copies four keys and passes
`parameters` **by reference**. There is nothing there to cache. The expensive
thing the plan was thinking of is the tool-DEFINITION build one layer up
(`model_tools.get_tool_definitions`, billed as
`profile_agent_init_tool_setup_ms` = 853–1,674 ms) — and that runs at agent
CONSTRUCTION, not per turn, and is already skipped entirely on a reused resident
actor.

**And that cache already exists.** `model_tools._tool_defs_cache` is keyed on
`frozenset(enabled_toolsets)`, `frozenset(disabled_toolsets)`,
`frozenset(blocked_tool_names)`, **`registry._generation`** (the epoch the brief
asked for), the config `(mtime_ns, size)` fingerprint, the kanban toggle and the
delegated-child context; it is bounded (`_TOOL_DEFS_CACHE_MAX = 8`, LRU-evicting);
both arms return `list(...)` so a caller appending memory/LCM schemas cannot
poison it; and `_clear_tool_defs_cache()` is the explicit hatch. Every property
Stage 3 asked for is already true, one level below where the plan looked.

**Therefore: no second cache was built.** Writing one would be a parallel
authority over a question that already has an answer — the exact defect class
this repo's system-integrity rules name. What the memo genuinely lacked is the
one other thing Stage 3 asked for: a receipt distinguishing a hit from a build.
That was built (§3 below).

**What actually owns the 703 ms.** With `request_build` at 1 ms and
`pre_api_hook` at 0, the remainder of the assembly span is `build_turn_context`
and the unmeasured prologue around it. That call was never timed. It is now.

---

## 3. What Stage 3 landed

1. **`profile_conversation_turn_context_ms`** — `build_turn_context` is timed
   through the existing `_emit_conversation_timing` seam. This names the owner of
   the several hundred milliseconds that had no owner on the record.
2. **The system-prompt restore receipt, as a POSITIVE pair.** Exactly one of
   `profile_conversation_system_prompt_restore_ms` (the stored prompt was found,
   matched the runtime identity and was reused verbatim) or
   `profile_conversation_system_prompt_build_ms` (it was rebuilt, and this is what
   that cost) is emitted per turn. Neither is emitted by a turn that never entered
   the prologue — a third state that must stay distinguishable from "restored in
   0 ms". `agent._system_prompt_restored_from_session` carries the same fact to
   the enclosing `turn_context` timing without re-deriving it, and is reset inside
   the function so a gateway-cached agent cannot carry a stale `True` forward.
3. **The tool-schema memo's hit/miss receipt.** `model_tools` grew thread-local
   cumulative `hits`/`misses` counters (the same shape, and the same reasoning,
   as `tools.registry.probe_rounds_this_thread` and
   `chat_lane_bundle.bundle_builds_this_thread`), and `agent/agent_init.py` emits
   exactly one of `agent_init_tool_defs_build_ms` (the memo missed — the registry
   walk, the schema filter and the `check_fn` sweep were paid) or
   `agent_init_tool_defs_cached_ms` (it hit). **Neither** is emitted when the
   counter cannot be read at either end: "I could not ask" is not "it hit".
   Deliberately NOT routed through `_emit_init_timing`, whose rolling checkpoint
   would have silently redefined the existing `agent_init_tool_setup_ms` to mean
   "the rest of tool setup" — a receipt must not move a measurement that already
   has readers.

Every key rides the existing `*_ms` profile-timing channel, which
`mission_chat_turns.safe_turn_profile_timing` already admits. **No schema change,
no new phase key, no new config field, no producer surface moved.**

---

## 4. What Stage 4 landed, and the half that is PARKED

**Landed: the instrument.** `_cmd_mission_chat_message` times its own
`_default_persona_session_db()` call on `time.monotonic` and folds
`session_db_open_ms` into the `profile_timing` block that reaches the durable
record. A turn whose open FAILED never reaches the fold, so the key stays absent
rather than reporting the cost of an open that did not succeed.

One deliberate asymmetry, documented at both ends
(`mission_chat_turns.TURN_PROFILE_TIMING_KEY` and the fold site): the handler
takes a **copy** of the runner's dict, so the LIVE result frame keeps carrying
the runner's own accounting byte-for-byte, while the DURABLE RECORD carries the
turn's, which is a superset. That is what keeps this lane off the producer
surface.

**PARKED: the pooling.** See §5 for the measurement and the decision rule.

---

## 5. What Stage 5 landed

`_defer_demote_build_for_active_turns` in `agent_runtime/stream.py`, called from
`_full_core_batch_frames` **after** the demote-core reuse gate (a reuse builds
nothing, so there is no CPU to yield) and **before** the build job exists (the
yield must be decided before a worker thread starts, or it yields nothing —
pinned by a test that asserts that ORDER).

* **The signal is not a new authority.** It forwards to
  `profile_runner.agent_runs_in_flight()`, the Stage-2 prewarm yield rule's own
  counter. Its window (run entry → exit) is marginally wider than the plan's
  `write_ahead → stream_done`; that is stated in the docstring at the site and is
  bounded by the ceiling either way.
* **Bounded at `SNAPSHOT_DEMOTE_DEFERRAL_MAX_MS = 1000`** — a module constant with
  a doctrine comment, not a config key (Stage 2 §7: a `PersonaChatConfig` field is
  projected onto the read-model wire and would make this a cross-stack landing).
  The bound is on the WHOLE deferral of one build request, so the worst case it
  adds to HUD staleness is one second.
* **Scope: demote only.** Boot/hydrate (`accept_inflight=True`) and the
  `full_core` lane are a consumer waiting on an answer, not a cadence rebuilding
  state nobody asked for; making an operator's first paint wait on a chat turn
  would trade this inflation for a worse one.
* **Unknown is not "a turn is running".** An unreadable counter does not defer.
* **A cancelled request abandons the wait immediately.**
* **`builds_overlapped` still counts honestly.** Nothing here touches the build
  ledger, so a build that waited the full second and then overlapped anyway is
  still counted against that turn. The deferral cannot launder its own failures
  out of the receipt.
* **Receipt:** one `snapshot_build_deferred reason=… caller=… waited_ms=…
  runs_in_flight_at_exit=… bound_ms=…` line per deferral (never per poll), closed
  vocabulary, ids and timings only.
