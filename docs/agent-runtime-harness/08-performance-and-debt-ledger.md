# 08 — Performance and debt ledger

What this runtime actually costs, measured, and what it still owes. Every number below
carries the date it was taken and the receipt, log line or commit it came from — a
performance number without a source is a rumour, and this file is where rumours go to
die. The burn-down is ordered by value, not by how interesting the work is. Rows that
were convicted and then refused with a measurement are listed too, so nobody spends a
week re-deriving a decision that was already paid for.

**All measurements are from the live install** — serve log
`X:/Eternia/.hermes/profiles/base/logs/agent.log`, launcher diag
`%TEMP%/eternia_launcher_diag.log`, and turn records. The running serve reads
`profiles/base`; measuring under another profile home measures a different runtime.

---

## Live baselines

| what | number | when | source |
| --- | --- | --- | --- |
| serve boot, total | **1,630 ms** (27 boots 08-21→08-22: 1,042–4,335) | 2026-08-22 15:46:27 | agent.log `harness serve boot timeline … total_ms=1630` |
| cold snapshot core build | **11,235 ms**, `sections_top=prompt_observability:4520,agents_readiness:4366,events:842` | 2026-08-22 15:46, fresh serve, generation=1 | agent.log `snapshot_build_core` |
| steady-state core build | **1,948–3,733 ms** per generation (8 consecutive) | 2026-08-22 | agent.log `snapshot_build_core` |
| `agents_readiness` halves, cold | walk **2,133 ms** / tool_visibility **2,232 ms** | 2026-08-22 15:46, pid 30588 | agent.log `snapshot_agents_readiness` |
| `agents_readiness` halves, steady | walk **663–1,918 ms** / tool_visibility **25–2,081 ms** over all 22 receipts on the pid; excluding the cold first build, walk 663–1,151 and tool_visibility mostly ≤28 BUT spiking to 110/692/760/778 on four separate steady builds — tool_visibility is NOT a cold-only cost | 2026-08-22, pid 32164 | same receipt |
| `agents_readiness`, bench (5 personas, operator profiles root) | first build in a process **4,001 ms** (tv 3,054 / walk 947); steady **183 ms** (tv 36 / walk 146) | 2026-08-22 | commit body `25cd488d33` |
| boot: ready→authoritative, cache hit vs rebuild | **911 ms** vs **11,980 ms** (authoritative wall 9,740 vs 19,626) | 2026-08-21 evening | `tool/mission_boot_receipt_audit.dart`, quoted in the launcher Brain |
| core-cache convergence | **10 × `never_converged`**, 08-20 18:21 → 08-22 13:42; 5 carry `diff_scope=every_pass`. **2026-08-23, post IC-1..3: five NEW firings name the chat-turn sidecar family** (`mission_chat_turns/*.json`, `mission_chat_steer/*/active.json`, `persona_chat_leases/*.owner.json`, `prompt_observability/*`) — a runtime-authored churn class recorded in `planned/core-cache-input-closure.md` | 2026-08-20→23 | agent.log WARNING `snapshot_core_cache never_converged` |
| agent drop, cold → warm | rpc **2,466 → 178 ms** (`rpc_instance_ms` 2,406 → 110); second pair **2,086 → 121** (instance 2,030 → 78) | 2026-08-21/22 | `[MissionDropTiming]` |
| agent drop, roster confirmed | cold **8,309 ms** (08-21, pre demote-reuse) → **4,645 ms** (08-22 17:43Z, first cold drop after the same-offset reuse landed); warm 506 | 2026-08-21/22 | `[MissionDropTiming] roster_confirmed_ms` |
| create phases, cold | `instance_ms=1046` — create_patch 984 / wire_row 984 / chat_lane_scope 859 / tool_visibility 125 (phases overlap; they are spans, not a partition) | 2026-08-22 13:43:53 | agent.log `agent_create_phases` |
| persona prewarm, per persona | 109–530 ms, 5 personas, one sequential worker | 2026-08-22 | agent.log `persona_prewarm done` |
| chat TTFT, alice | **17,787 ms**; components admit 1,912 / agent_ready 2,360 / provider first_byte 13,532 / residual 226 (launcher and hermes clocks — components do NOT sum to the TTFT and must not be presented as an equation; the source doc's own rows carry the same gap) | 2026-08-22T02:00Z, turn `c59ab99e` | `tool/mission_chat_latency_audit.dart` |
| — of that provider span, hermes | **1,762 ms** (~13%): conversation prologue + request assembly + client build | same turn | the `request_assembled` split, `785a35beae` |
| chat TTFT, qa | **9,231 ms**; components admit 1,966 / agent_ready 4,078 / provider 3,140 / residual 153 (same two-clock caveat) | 2026-08-22, turn `1f31e082` | same tool |
| hermes admission per warm turn | anchor→`agent_ready` **4,046 ms** (alice) / **5,891 ms** (qa) | 2026-08-22 | turn record `phases` |
| provider: **big-pickle / opencode-zen** (base profile default) | total call **10.7–12.5 s** at in≈10k (4 turns); the probe's 9 requests (3-per-leg burst, then 45–90 s spaced retries) ALL returned `429 FreeUsageLimitError` in 172–531 ms — it is the FREE tier and the cap is a usage window, not burst protection | 2026-08-21/22 | turn records + live probe |
| provider: **gpt-5.6-luna / openai-codex** (alice profile default) | TTFB **0.7–1.6 s**; total **1.8–5.8 s** at in=13,301–29,794 across 13 live calls. **CANONICAL EXPECTATION NOTE (2026-08-23): the raw floor is 0.74–0.92 s at ANY reasoning effort, but LIVE mission-chat TTFB runs 2.2–3.5 s — the surplus is the model REASONING at effort=medium over the ~17.5k-token rule/tool frame before its first visible token (a 96%-prompt-cache turn still showed 6.1 s ttfb, so it is not ingestion). Every other doc quoting luna TTFB points here.** | 2026-08-22/23 | live probe + A/B effort probe + turn records |
| tool-schema payload cost | +39,772 B (probe leg: 30 tools) moved luna TTFB 0.7–1.1 s → 1.3–1.6 s; the live turn carried 31 tools / 39,016 B | 2026-08-22 | probe legs 2 vs 3 |
| event log | rotated: archive base 81,417,412 B + live slice 8.6 MB; logical offset ≈ 90.0 M | 2026-08-22 | `events_archive/` + build `offset=` |

Two readings that matter more than any single row. **The felt slowness of a mission chat
is mostly the provider, and mostly the MODEL** — the same pipeline on luna answers in
1.8–5.8 s. **The cold first build is the runtime's largest self-inflicted cost**, and it
is paid on every boot that the core cache cannot serve, which on this install is every
boot (see open row 2).

Live model defaults, read 2026-08-22: `profiles/base/config.yaml` → `big-pickle` on
`opencode-zen`; `profiles/alice/config.yaml` → `gpt-5.6-luna` on `openai-codex`.
**Superseded for mission chats 2026-08-23:** both alice instances (and the roster
personas already) carry tier-2 instance `set-model` overrides to
`gpt-5.6-luna`/`openai-codex`, which beat the profile default — no mission chat speaks
big-pickle any more. Only the `base` persona still cascades to the base default.

---

## Landed optimizations

Measured before→after, each with the commit that carries the proof in its body.

| what shipped | sha | measured |
| --- | --- | --- |
| **`check_fn` grace backoff** — the transient grace re-ran the whole probe on every call inside its window | `5fd5502374` | 40 probes + 40 warning lines in 2 s for one `check_fn` (759 such lines in one agent.log) → one probe per 5 s backoff; the storm fired *during* the prewarm walk it existed to make cheap |
| **`agents_readiness` split receipts** — the section's timer wrapped two unrelated halves | `25cd488d33` | no time saved by design; it convicted the right half. Cold 4,001 ms = tool_visibility 3,054 + walk 947; steady 183 = 36 + 146. Every earlier plan that read the section number convicted the walk, which is the smaller half |
| **Boot core cache (R1 consult stamp)** — every boot demoted the persisted core for a config file whose bytes had not changed in two days | `2f98eef3ee`, `88151d436b`, `7204896978`, `be38494d5d`, `5feae8cadb` | ready→authoritative 11,980 → **911 ms**. `profiles/alice/config.yaml` was byte-identical (same 23,255 B, same SHA-256) across the "change"; it is atomically replaced, and a `(path, mtime_ns, size)` key cannot tell that from an edit |
| **The `scope` patch entity** — `workspace.activated`/`realm.activated` were uncovered, so a two-scalar pointer flip demoted its whole batch to a full core rebuild | `ffd540bf73` (WS1/WS4, 2026-09-01) | the wait behind a workspace switch was the core republish cadence, measured on the launcher's receipt log at p50 **8.76 s** / p90 **13.54 s** (n=5,120 over 18 days; 90.6 % of republishes ≥ 8 s) against a 1 ms paint — a figure that until this row lived only launcher-side (`EterniaLauncher docs/mission_control/06-board-and-aux-surfaces.md:420-441`, derivation in `planned/dcw-l4-field-notes-2026-08-31.md`). Post-WS1 a switch travels as one few-hundred-byte `scope` patch; core `build_ms` median was 3,083 (max 37,266) per demoted rebuild, no longer billed per switch. Live-serve span re-measure is WS0, operator-composed |
| **Demote same-offset core reuse** — three demote builds at one offset each wrote back the identical fingerprint | `fcccd1a992`, `f78a45d8c7` | generations 17/18/19 at offset 89961793, `build_ms` 3,017/3,210/2,388, all writing `fingerprint=9d655b54f622`. The coalescer merges by *time*; this reuses by *position*, which is checkable. `f78a45d8c7` collapsed the second spelling of "how far does this core reach" so the frame guard and reuse gate cannot disagree |
| **Persona prewarm** — receipts, then the claim gated | `9d695aa762`, `a9445fd5c6`, `55f35937a9` | the first drop of a persona type paid 1.9–3.2 s of cold memo misses inside the create RPC. The prewarm had a start and no finish, so "the memos were filled" was untimeable; `a9445fd5c6` added `persona_prewarm done`. `55f35937a9` measured the "not optional" comment on the scope call FALSE under the unbounded default and TRUE under `default_mode: profile_default`, and gated the test to the config where it holds |
| **Toolset NAMES stop asking for an availability VERDICT** — `personas.all_registered_toolsets` asked `get_available_toolsets()`, whose `available` boolean it discarded | 2026-09-02, this batch | one availability round per toolset, on `perform_agent_create`'s own wire-row projection: **25 rounds → 0** (`tools.registry.probe_rounds_this_thread`), and the first call in a fresh interpreter **3.08 s → 1.65 s** median (interleaved A/B, 5 samples each, min 2.72 → 1.51). The key sets are identical by construction — both fold `entry.toolset` over one registry snapshot. Consequence worth reading beside the prewarm row above: the `check_fn` half of what that stage warms is no longer on the create path at all, and the two tests that measured it were replaced by the invariant that it is gone (`planned/serve-small-batch-field-notes-2026-09-02.md` §2) |
| **`harness_core` replaces the registry-wide unbounded set** — the harness lane finally reads the profile's `toolsets:` declaration | 2026-09-03, S0a A1-A3 (`feat/s0a-atlas-cleanup`) | per persona, all four mission personas identical before and after: callable tools **79 → 43**, withheld **17 → 0** (12 `kanban_*` + 5 `feishu_*`, hygiene-blocked on every turn because `unbounded` resolved every registered toolset), `requirement_failures` **1-3 → 0** (every one had been for a server `--explain-mcp` reported as ADMITTED — R-S0a-4 stops calling admitted-but-torn-down a failure), `model_tool_tokens` **2142 → 1149** (name-envelope heuristic, not a provider bill). Measured with `HERMES_HOME=X:\Eternia\.hermes` and no profile edited: the repo-side lane default is what makes the landing need zero HERMES_HOME writes. Side effect worth reading beside the toolset-NAMES row above: the declaration resolves from static `TOOLSETS` + the mtime-cached profile YAML, so the toolset-name half of an agent create no longer touches the registry at all (88 ms first call, 1.2 ms warm, against 1.7-2.6 s for a cold `import model_tools`) |
| **`request_assembled` split** — the provider span opened before `run_conversation` began | `785a35beae` | 1,762 ms of the alice turn's 13,532 ms "provider first_byte" re-billed to hermes; ~11,770 ms is genuinely the provider |
| **TTFB token for every lane** | `74702c193e` | mission-chat turns get provider TTFB from phase marks; before this, every other lane's first byte survived only as prose |
| **Phase-timing marker through the progress sink + `profile_timing` on the record** | `60c7f46ec1` | `request_assembled`/`agent_init_cold` never reached a live record (the sink's Trace-noise filter ate the marker; the resident registry never existed so the flag had no source); recovers 0 ms, makes Stages 1–2 checkable |
| **Chat-lane visibility bundle** — one resolve per turn, memoized on identity + registry epoch | `7f2c82f090` | `registry_probe_rounds` 23–27/turn → 0 on warm turns; the resident-actor reuse key stopped hashing row liveness (turn 2 reuse receipt: bootstrap 62 ms) |
| **Resident-actor prewarm at boot + chat-open** | `bfde53b4ae` | first-turn construction (~3 s, `agent_construct_ms=3000` on the 17:33:01Z first-after-boot turn) moved off the operator's path; live 2026-08-24: 59 `persona_chat_actor_prewarm` lines, warm turns reuse. Receipt still owed on the one clause that names the remedy: first turn of a fresh chat with `agent_init_cold=false` |
| **Resident-actor rebuild names its cause** — per-component signature digests + two identity fixes | `14271f261f`, `b0c1a668b9` | three consecutive turns of one prewarmed neko root (19:03:10/23/40Z) each rebuilt with only `resident_rebuild_runtime_signature_changed` to show for it, so the diagnosis cost a store archaeology session. `acquire` now diffs a per-component digest map and writes `resident_rebuild_component_<name>`; `permissions` stopped folding the operator-shaped `permission_state` (its `blocked_tools` is resolved over every tool registered in the process) and `current_chat_goal` left the instance allowlist; `b0c1a668b9` stopped the key reading the ambient config document, so a readiness walk's `HERMES_HOME` rebind could no longer rotate a chat's actor. Recovers 0 ms directly; it is what makes the Stage 1/2 reuse claim checkable in one read — and did, on the 2026-08-24 records, where the warm turns carry no `resident_rebuild_component_*` key and the cold ones name `workspace_agents` |
| **Profile binding goes context-local** — the readiness walk, then prompt observability (and with it the chat turn's own `observability_built` call) | `e567a9ff00`, `846848cfc4` | `profile_context`'s save/mutate/restore of process-global `HERMES_HOME` is sound only under `profile_runner._WORKDIR_LOCK`, and both callers entered it WITHOUT the lock — the readiness walk on the snapshot builder thread every 2–4 s, and `_cmd_mission_chat_message` on every turn before `profile_runner` installs its locked binding. So a background walk moved live turns' `chat_lane_bundle` key, whose `(mtime_ns, size)` is read off the ACTIVE `config.yaml`. Measured: a bundle-free turn built context in **453 ms** against **1,796 / 2,343 ms** on turns overlapping a walk with `visibility_bundle_builds=3/6`. `persona_profile_scope` binds ContextVars and writes no `os.environ`; `persona_profile_context(export_env=True)` keeps the mirror for subprocess-facing callers. Mechanism and the reader-by-reader audit: `02-runtime-data-and-shapes.md` §The build receipt |
| **Catalog TTL memos** (`_installed_skill_catalog`, `_profile_templates_cached`) | archived doc 14 §Slice 1 | `build_ms` 3,700 → 2,943 warm (~20%) |
| **Coalesced concurrent builds** | archived doc 14 §Slice 2 | three concurrent builds were 8.8 s EACH; boot-storm first response 8.2 → 3.6 s |
| **Build-scoped batch skill resolver** | archived doc 14 §first-message hardening | full core **36.622 s → 5.949 s** on the same live store (~84%); 502 exhaustive `resolve_skill` calls collapsed to one walk |
| **Event-log rotation with sidecar `base_offset`** | `agent_runtime/event_rotation.py` | logical offsets stay monotonic across archive rotation; live slice is 8.6 MB against an 81 MB archive base |
| **Delta patches on the wire** | `agent_runtime/state_patches.py`, `schema_version: 2` | stops shipping a full core per event |
| **`parse_cache`** — `(path, mtime_ns, size)` keyed leaf loads | `agent_runtime/parse_cache.py` | archived doc 14's item 1 as applied to YAML/frontmatter leaves (the JSON store models are still uncached — open row 4) |
| **Serve read-model response cache** — status/snapshot `--json` replayed on a runtime-state fingerprint, 20 s TTL | `hermes_cli/harness_parts/serve.py:397-408,619` | a replayed response stamps `served_from_cache` + `cache_age_ms`; it caches the *payload*, not the parsed models |

---

## Open rows — in value order

### 1. The cold build is O(world) — CONVICTED in magnitude, UNDIAGNOSED in cause

**Evidence:** `build_ms=11235` on a fresh serve 2026-08-22 15:46, with
`prompt_observability:4520` and `agents_readiness:4366` between them owning 79% of it;
steady-state builds run 1,948–3,733 ms. Both dominant sections are named, neither is
decomposed below section granularity, and the `agents_readiness` split (`25cd488d33`)
already proved that reading a section number off a build log convicts the wrong half.

**Gate to open work:** a section-level audit that decomposes `prompt_observability` the
way `25cd488d33` decomposed `agents_readiness` — instrument first, then choose a target.
No remedy may be proposed against the section-level number alone.
→ [`planned/cold-first-core-build-cost.md`](planned/cold-first-core-build-cost.md),
[`planned/incremental-projection.md`](planned/incremental-projection.md) (boot and
runtime-data lanes own those designs; do not fork them here).

### 2. The core cache never converges — CONVICTED

**Evidence:** 10 `never_converged` WARNINGs between 2026-08-20 18:21 and 2026-08-22
13:42. Five carry `diff_scope=every_pass` — self-perturbation, the class worth acting
on. The named diff paths are runtime-authored: `profiles/base/state.db` and
`state.db-wal`, the live events slice `events_archive/events.81417412.jsonl`,
`realm_sync/<id>/.git/{index,logs/HEAD,refs/heads/main,objects/pack/*.pack}`,
`agent_create_reservations/*.json`, `office/<ws>/archive/*.json`,
`profiles/alice/config.yaml`, and a `persona_instances/*.json` row. The warning's own
text names the fix direction: *widen the fingerprint's input closure, never trust the
cache harder* (`agent_runtime/core_cache.py`).

**Cost of not fixing it:** a write per build that buys nothing, and every boot pays
11,980 ms instead of 911 ms.

**Gate:** the census receipts must attribute each churning path to its writer before any
path is excluded from the closure — an exclusion without a named writer is the same
mistake the cache already made.
→ [`planned/core-cache-input-closure.md`](planned/core-cache-input-closure.md)
(the runtime-data lane owns that design; the boot lane's `planned/` core-cache rows
carry the boot-side half).

### 3. Per-domain store read caches in the serve — OPEN, re-verified 2026-08-22

Archived doc 14's item 1, and still item 1. The serve's `_PollResponseCache`
(`serve.py:1252`; renamed from `_ReadModelCache` in the duplicate-implementation
retirement, Stage 2 — the old name was the third thing in this repo wearing
"read model", and Stage 6 has since deleted the first, so the phrase now means
two things: this repo's live core cache and the directory it lives in) is a
**response** cache — it replays the exact stdout payload of
`status --json` / `snapshot --json` behind a fingerprint and a 20 s TTL. It does not
cache parsed store models, so a build that misses the core cache still re-reads and
re-decodes the JSON store from disk. `parse_cache.py` landed the `(path, mtime_ns,
size)` key doc 14 asked for, but applied it to YAML/frontmatter leaves only.

**Gate:** open row 1's audit must first show what share of `prompt_observability` and
`agents_readiness` is store decode rather than resolution. Doc 14's profile (7,302
`raw_decode`, 13,748 `nt.stat`, 5,652 `io.open` per build) is from 2026-07-09 and
predates the batch resolver, rotation and `parse_cache` — **do not cite it as current.**

### 4. hermes admission, 4–6 s per warm turn — REMEDIATED 2026-08-23, warm re-take READ 2026-08-24, two clauses open

**Evidence:** anchor→`agent_ready` 4,046 ms (alice) / 5,891 ms (qa) on a warm serve,
2026-08-22 turn records. Registry `check_fn` availability re-probes run per turn
(agent.log 22:00:17.8–18.8 — ~1 s of the window), and they land *after*
`provider_request_started`, so before `785a35beae` they were billed to the provider.

**Remediated in THIS repo** by `planned/chat-turn-prep-cost.md` Stages 0–2
(`60c7f46ec1`/`7f2c82f090`/`bfde53b4ae`): the visibility bundle ended the per-turn
probe storms, hot sessions + the identity-allowlist reuse key ended per-turn agent
construction, and the actor prewarm moved first-turn construction off the operator's
path. First live receipt: bootstrap 3,782 ms → **62 ms** on the 17:33:01Z/17:33:17Z
pair. The numbers to beat were 4.0 s and 5.9 s.

**Re-take READ 2026-08-24** (ten records, 00:42–00:48Z, `mission_chat_turns/`). The gate
asked for three consecutive warm turns; **five** consecutive turns of one neko root
(00:43:08 / :17 / :20 / :28 / :56) delivered `registry_probe_rounds=0` +
`visibility_bundle_builds=0` + `resident_actor_reused=1` + `agent_init_cold=false` + no
`resident_rebuild_component_*` key, and `request_assembled` is present on **all ten**
records — the Stage-0 instrument's own gate, closed. Two clauses of this row stay OPEN:

- **`context_built<500` held on two of five** — 343 and 468 ms against 562 / 796 / 968.
  The bundle is no longer being rebuilt, so the residue is elsewhere in the context
  builder; it is the tail, not the storm.
- **The fresh-chat first turn is still cold.** All three first turns in the window read
  `agent_init_cold=true`; the two neko ones name
  `resident_rebuild_component_workspace_agents` — the documented `--agents-file` blind
  spot (04's Stage 9a residue), not a prewarm failure. The third (qa, 00:48:49Z) names no
  component at all, which is a genuine first construct for that root rather than a
  rebuild. Wall cost of the residue is small: `agent_ready − write_ahead` = 125 / 94 /
  750 ms, against the ~3 s construction this stage removed.

### 5. The turn prologue — CONVICTED at n=2, diet GATED

**Evidence:** 1,338 ms (alice) / ~269 ms (qa) between `provider_request_started` and
`api_start_time`. The two turns disagree by 5×, so which prologue item dominates —
system-prompt restore-or-build, preflight compression, memory prefetch, or the
crash-resilience persist, all in `build_turn_context` — is **not** convicted.

**Gate:** ~a week of `request_assembled` spans. If p50 assembly > ~1 s, one targeted
diet against whichever receipt the persisted `prompt_observability` `trace_events`
convict. Nothing lands before that.

### 6. Tool-schema diet — MEASURED at ~0.6 s, GATED on row 7

**Evidence:** +39,772 B of tool schemas moved luna TTFB from 0.7–1.1 s to 1.3–1.6 s. On
big-pickle the effect is invisible under the 11 s baseline.

**Gate:** the fast-model ruling (row 7) lands first, and the audit then shows
`provider_net` p50 dominated by the tools delta. 0.6 s is real and it is the third-largest
number on the board — trimming the chat lane's 31 admitted tools requires measuring which
tools turns actually call (the turn store's `elements` name them) before cutting any.

### 7. The model default — RULED AND APPLIED 2026-08-23 (was: operator-owed, ~10 s per turn)

**Evidence:** the base profile that Mission Control rides defaults to `big-pickle` on
`opencode-zen`, measured at 10.7–12.5 s per first call at in≈10k and 429-limited under a
3-request burst, because it is the FREE tier. The alice profile's `gpt-5.6-luna` on
`openai-codex` answers the same shape at 0.7–1.6 s TTFB and 1.8–5.8 s total on 13 live
calls at *larger* prompts. This is the largest single number on the board and it is one
config value, but which model Mission Control speaks with is an operator's decision, not
an optimization.

**Gate: DISCHARGED.** The operator ruled ("change it to luna") and the change was
applied through `harness persona instance set-model` as instance-tier overrides on both
alice instances (`model_override_issued_at: 2026-08-22T14:49:24Z` on the live rows);
mission chats measured on luna the same day. The base persona still cascades to the
profile default — offered to the operator, not yet ruled. Row 6's tool-schema diet is
now unblocked (its gate was this row).

### 8. The A→B→A cross-install cycle guard — OPEN, BY DESIGN

A cross-install dispatch is a fresh chain root on the receiving install: the
relay depth/cycle policy is a fact about ONE process's turn stack, and B's turn
runs under B's own concurrency cap, budget and drain. So A→B→A is not bounded by
the guard that bounds A→A→A, and S2b's read verbs do not change that (they read;
they start no turn).

Named here rather than left implicit because the S2 wave made the surface wider
without touching it: `peer.roster.list` lets an agent SEE the far roster, so the
shape "ask them, they ask us back" is now one an agent can plan rather than
stumble into. The parent plan's §4 records it as still open by design; the fix,
when it is worth one, is a chain token that survives the wire rather than a
depth counter that does not.

### Refusals with a measurement — do NOT re-propose

- **A per-persona memo over `profile_readiness_for_persona`** (`25cd488d33`). An honest
  key must cover skill package content; that key is the stat set
  `skill_package_content_hash` already computes and caches one layer down. Reproducing it
  above the walk costs ~102 of the 146 steady-state ms to save the remaining ~44. *A key
  that costs what the body costs is the same mistake wearing a hat.* Argument written at
  `agent_runtime/profile_readiness.py`.
- **Bounded concurrency in the prewarm worker**
  (`agent_runtime/persona_prewarm.py:62-88`). N threads would each find the `check_fn`
  TTL cache cold and each run the full toolset sweep — N concurrent `docker version`
  subprocesses, N playwright imports. A second worker cannot shorten the 2,172 ms item;
  it can only run the already-free ones alongside, buying at best 16 ms.

### Closed on verification, 2026-08-22

- **"One event-log scan per build" (archived doc 14 item 2) is CLOSED.**
  `CachedEventLog` (`agent_runtime/events.py:323-363`) reads every rotated slice once
  per build, keys a process-local immutable view on the slices' `(path, mtime_ns, size)`
  fingerprint, and serves `for_task` / `for_session` / `tail` from the cached lines while
  keeping the base class's selective parse. The 22-scans-per-build figure no longer
  holds; the residual `events:842` cold / 679–1,047 steady section cost belongs to open
  row 1, not to re-scanning.

---

## Debt register

Open refactor and dead-code rows only, each re-verified against the tree on 2026-08-22.
Executed history stays archived. The duplicate-implementation retirement's rows
(`create_free_floating`, `PERSONA_CHAT_SESSION_SOURCE`, `--message`) were struck on
2026-08-30 when its stages 1/3/4/5 finished — see
[`planned/duplicate-implementation-retirement.md`](planned/duplicate-implementation-retirement.md).

**From [`19-deferred-debt-ledger.md`](archive/2026-08-22-pre-consolidation/19-deferred-debt-ledger.md):**

- ~~Full `PersonaAssignmentStore` retirement is blocked on Launcher wire consumers (a
  wire-block drop is a snapshot contract bump) and on residual rows whose only settle
  path is close/archive; the module has 6 production importers.~~ **SETTLED 2026-08-31
  (§AX AX2), and the row was wrong on both halves.** The Launcher wire consumers were
  not a block, they were a STAGE: the launcher deleted every read of the
  `persona_assignments` block (`6bf48ba26`) and keeps a test feeding a payload that
  still carries it while asserting nothing in it reaches the instance, so hermes
  dropped the block, `persona_instance_runtime.assignment_store_enabled` and the
  `warnings` lane it fed. The contract-bump claim is also not what the removal rule
  says — see `snapshot._parity_envelope`'s "54 KEPT (AX2)" entry for the first KEPT
  ruling over a departure and the reason a bump was the riskier move here (an
  exact-equality launcher pin turns a bump into `mayWrite == false`). The retire's two
  assignment guards went in the same wave (tombstone wave `s76`).

  What the row got RIGHT is the half that is deliberately not built: residual rows
  whose only settle path is close/archive. `PersonaAssignmentStore`'s read/close
  surface and `harness persona assignments` / `persona instance close`/`archive`
  SURVIVE, because retiring them would strand residue on live stores with no verb
  that can reach it — the same reason the launcher's installer keeps its
  `persona_assignments` preserved-path rows. Read paths retire; stored bytes do not.
  Full store retirement is now gated on one thing only: a census showing zero
  residual rows on every store, and that census does not exist yet.
- Launcher-side handoff (not this repo): the `close`/`archive` bridge lanes have registry
  rows and argv builders but no dispatcher. (The `allowedArgs` half of this row is
  CLOSED: `auto_run`/`max_actions`/`max_seconds` are gone from both create specs, and
  `message` stays DECLARED on purpose — it feeds client-side reads that never rode the
  argv.)
- The `memory` parallel-authority ruling is owed: under the unbounded runtime default the
  upstream `memory` tool is on every chat lane's schema, so it and profile memory
  (`MEMORY.md` / `USER.md`) are two authorities over one question, and an agent can write
  where the profile-memory lane does not read.
- `hermes_cli/harness.py` carries a blanket `F821` per-file ignore (`pyproject.toml:454`)
  because the exec loader makes ~62 `_cmd_*` names genuinely undefined until
  `_load_command_parts()` runs; retiring it needs full module conversion.
- Upstream-owned, report-only: `hermes_cli/env_loader.py:310` and `:541` hand-spell
  `Path.home() / ".hermes"` where the Windows platform default is `%LOCALAPPDATA%\hermes`;
  `hermes_state.py:235` freezes `DEFAULT_DB_PATH` at import time.

**From [`DEAD_CODE_AUDIT_PASS_2_2026-08-18.md`](archive/2026-08-22-pre-consolidation/DEAD_CODE_AUDIT_PASS_2_2026-08-18.md)
— all ten §4 ruling subjects confirmed still present in the tree:**

- `head_agent_profile` — inert config field; recommendation *defer*, trigger "next
  contract bump" (2 lines are not worth a cross-stack golden regeneration).
- `backfill_instance_profile_ids` — land `harness agent set-profile --backfill-instances`
  or retire 138 + 302 test lines (recommendation: land the verb).
- `set_entry_point_lane` — recommendation *cut*; move its two suites to the env-var
  spelling.
- `realm bind-server` / `workspace add-agent|remove-agent` — is `server_id`/`agent_ids`
  write-once-at-create? A yes cuts three verbs and three store methods. Each has exactly
  one production caller (its own CLI verb), so this is a data-model ruling, not a cleanup.
- `contract_manifest` — keep the constant after `contracts dump`, re-word the KEPT pin's
  rationale.
- Pet-gallery empty keys — keep for shape parity, comment why.
- `task_store_stub.py` retirement, after H-P2, with its four pinning tests.
- `mobile_core/` — CI tier or archived (operator).
- `F401` in `pyproject.toml` with upstream per-file-ignores — **still not enabled**
  (`select = ["PLW1514", "F821"]`), and R11 already refused the naive form: F401 cannot
  model an exec namespace, and `harness.py` imports ~100 names *so the exec'd parts can
  see them*.
- `updated_by` on `archive_orphaned_surface` — wire it or drop it (recommendation: wire).
- Cross-stack, launcher-owned: `runtime.office.unsubscribe` has zero launcher callers
  (NEW-2); a launcher test asserts `provider_auth_expired`, a code hermes cannot emit;
  `hermes_cli_contract.json` is stale in both directions.
- Not swept, so absence of a row is not a clean bill: intra-function dead branches inside
  live handlers of `harness.py` / `serve.py` / `persona_commands.py`; `core_cache.py` and
  `realm_sync.py` internals; `serve_socket.py` and `serve_office_subscriptions.py` lock
  and lease semantics; several wire-token vocabularies; `tests/` as subjects.
- Refused with evidence — do not re-derive: the persona-chat append seam (NEW-1, a
  chokepoint between callers, pinned by `tests/hermes_cli/test_persona_chat_append_seam.py`),
  `harness contracts dump` (the only reader of `contract_manifest`), R11 as written, and
  H-CLI-5's store follow-ons.

**From the 2026-08-28 realm skill-delete build (field notes retired with the
plan at the canon fold; lane shipped as `45abf82803` + `32f41be19f` +
`dfc18b882f`, canon in 01 §Skills):**

- **A second member's FIRST `sync pull --ff-only` dies on `.gitattributes`.**
  `_ensure_sync_repo` → `_ensure_repo_gitattributes` writes the managed file
  into the clone *before* the pull runs; once any member's publish has
  committed it, every clone predating that publish holds it untracked and git
  refuses the merge even though the bytes are identical. Pre-existing, hit on
  the first two-HOME round-trip.
- **A pull INSTALLS the `CANONICAL_SHARED_SKILL_IDS` into the puller's shared
  root, and that member's next publish exports them** even if the member never
  adopted a harness skill (`install_harness_skills` on every pull ×
  `_skill_artifacts`). Pre-existing; reproduced in both the S1/S2 round-trip
  and the S3 smoke, and precisely why ruling R-B refuses tombstones on
  canonical ids.
- ~~**Three pre-existing reds in the full `tests/hermes_cli` floor**~~, verified
  failing identically at `32f41be19f` with the S3 change stashed:
  `test_error_exit_code_producers` (`runtime_unavailable` gained a producer —
  deleting the spelling is a cross-stack call the launcher owns),
  `test_completion::test_valid_bash_syntax`, and `test_xai_provider_labels`.
  **All three CLOSED**: the first two by `00fa94dd75` (2026-08-30 — the
  `runtime_unavailable` premise fell once the gateway lane minted it at five
  sites), and all three re-run green at `51b96505f0` on 2026-08-31. The
  unrun-gate CLASS the row belongs to is not closed by that, only these
  instances.

**From the 2026-08-31 decision-close wave (11 stages; field notes in
`planned/dcw-*-field-notes-2026-08-31.md`):**

- **The repo is ALL-LF, and zero is the ratchet** (`301946bc57`). The census at
  `0c744aa586` was 8862 LF blobs against 33 CRLF and 5 mixed; the 38
  CR-carrying blobs were normalized in one commit and **there are no deliberate
  keepers** — that is the finding, not an omission. Every one was a minority
  outlier in its own directory, and every Windows-only script (`*.ps1`,
  `*.cmd`) was already LF. The enforcement is `tests/test_line_endings.py`, not
  `.gitattributes`: attributes govern conversion, and say nothing about blobs
  already committed. A deliberate CRLF file is declarable — a `-text` rule plus
  a row in that gate's keeper set, with the reason; the set is empty on
  purpose. (Unrelated to the `_ensure_repo_gitattributes` row above, which is
  about the realm-sync CLONE's managed file.) The debt this repays is
  attribution: a Mac-side edit of a CRLF file turned a +149-line stage into a
  2513-line whole-file rewrite nobody could review.
- **The mutation gate selects by SYMBOL as well as by line**
  (`scripts/changed_line_mutation_check.py`, `301946bc57`). A claim is now also
  selected when the diff touched the DEFINITION around its needle rather than
  the needle itself, and `SELECTION_KEY` reports which reason fired, because a
  widening nobody can see in the output is indistinguishable from the gate
  having gone vague. Two additions ride with it: `--claims-for
  SYMBOL|PATH|PATH::SYMBOL` is a pre-flight REPORT — "which claims anchor in
  the symbol I am about to rewrite", answerable before there is a base to diff
  against, and it reports unresolvable anchors rather than dying on the first
  one, because mid-rewrite is exactly when it is most useful; and a `platforms`
  claim field (`{posix, windows}`) exists for the one module with a
  module-level platform fork, `agent_runtime/locks.py`, whose duplicate
  definitions have no unambiguous AST node and so anchor at `module`.
- **No test under `tests/hermes_cli` can start a live gateway**
  (`tests/hermes_cli/_gateway_fence.py`, `a35144d584` + `2a0cd784cf`). Measured
  on this workstation: `_cmd_update_impl` parks
  `_resume_windows_gateways_after_update` on `atexit`, which runs AFTER every
  fixture has torn down and every `monkeypatch` has been undone — so
  `HERMES_HOME` is back to the operator's real store and the spawn starts the
  OPERATOR's gateway, silently, because the cold-start path swallows its own
  exceptions by design. `tests/conftest.py::_live_system_guard` is an autouse
  FIXTURE and provably cannot cover that window. The fence is three layers at
  the three levels the escape used. The follow-up correction matters as much as
  the fence: it was first installed process-wide at conftest import and refused
  another directory's honest work, so it is now armed only for the duration of
  this directory's tests (`_gateway_fence_is_armed_for_this_test`) — and the
  original red-proof could not have caught that, which is the lesson the second
  commit records.

**From [`REFACTOR_DEBT_AUDIT_2026-08-17.md`](archive/2026-08-22-pre-consolidation/REFACTOR_DEBT_AUDIT_2026-08-17.md):**

- Two of its six defect classes were re-checked and are **LANDED, so they are dropped
  rather than carried**: RD-H3's empty-patch-frame promotion now has a non-empty guard
  (`agent_runtime/stream.py:493`, with the mechanism written out at `:813`), and RD-H1's
  office push scope predicate is now a documented union over the patch-coverage
  vocabulary rather than a private `office_actor`-only restatement
  (`agent_runtime/serve_office_subscriptions.py:140-163,285`).

---

## Unverified carry-forward

Named with its source, carried because dropping it silently would lose it — but **not**
re-derived in this pass, so do not quote these as current.

- **RD-H2, RD-H4, RD-H5, RD-H6** (the unreadable-event-log baseline `or 0`; the office
  projection counting what it could not read; provider absence meaning two things plus
  the dead lane into the swallow; three seam-parity repairs). Source:
  `REFACTOR_DEBT_AUDIT_2026-08-17.md` §0 items 2–6 and §3. Its line references are
  against `40b0f0c53a` and have drifted; no commit in the 08-17→08-21 window names an
  RD stage, and this pass verified only RD-H1 and RD-H3.
- **The boot cache-hit measurement** (911 ms vs 11,980 ms ready→authoritative). Source:
  `tool/mission_boot_receipt_audit.dart` output quoted in the launcher Brain's
  `chat-turn-latency-observability.md`. The audit tool was not re-run here; the serve log
  alone does not carry a ready→authoritative span.
- **All launcher-side rows** above (bridge argv, registry `allowedArgs`, the missing
  `close`/`archive` dispatcher, `hermes_cli_contract.json`, `provider_auth_expired`,
  `runtime.office.unsubscribe` callers). They live in a repo this pass did not audit;
  the hermes half of each is what was verified.
- **The archived 2026-07-09 cProfile store-read profile** (7,302 `raw_decode`, 13,748
  `nt.stat`, 5,652 `io.open`, 963 `yaml.load` per build). Source: archived doc 14
  §"Measured profile". It predates the batch resolver, event rotation and `parse_cache`;
  it motivates open row 3 but must be re-measured before it can size it.

---

## Supersedes

- [`archive/2026-08-22-pre-consolidation/14-snapshot-core-build-performance.md`](archive/2026-08-22-pre-consolidation/14-snapshot-core-build-performance.md)
  — its Slices 1 and 2, the batch-resolver incident and the 2026-08-14 status correction
  are carried into **Landed optimizations**; its remaining-plan item 1 is open row 3,
  item 2 is closed above, items 3 and 4 shipped. Its 2026-07-09 profile is unverified
  carry-forward.
- [`archive/2026-08-22-pre-consolidation/19-deferred-debt-ledger.md`](archive/2026-08-22-pre-consolidation/19-deferred-debt-ledger.md)
  — nine still-open rows carried into the debt register; its executed waves (S44–S70, the
  tombstone-registry consolidation, the S56–S58 contract waves) stay archived as history.
- [`archive/2026-08-22-pre-consolidation/REFACTOR_DEBT_AUDIT_2026-08-17.md`](archive/2026-08-22-pre-consolidation/REFACTOR_DEBT_AUDIT_2026-08-17.md)
  — RD-H1 and RD-H3 verified landed and dropped; RD-H2/H4/H5/H6 are unverified
  carry-forward.
- [`archive/2026-08-22-pre-consolidation/DEAD_CODE_AUDIT_PASS_2_2026-08-18.md`](archive/2026-08-22-pre-consolidation/DEAD_CODE_AUDIT_PASS_2_2026-08-18.md)
  — its §4 rulings, §5 not-swept scope, §6 cross-stack rows and its refusals are carried;
  its landed stages (HA-*, HB-*, H-CLI-*, H-P*, R*) stay archived.
- [`archive/2026-08-22-pre-consolidation/MISSION_BOOT_WINDOW_PLAN_2026-08-17.md`](archive/2026-08-22-pre-consolidation/MISSION_BOOT_WINDOW_PLAN_2026-08-17.md)
  — numbers only; its stage list and boot mechanism belong to
  [`04-boot-and-lifecycle.md`](04-boot-and-lifecycle.md).

Live provider and drop evidence is sourced from the launcher Brain and is **read-only
from this repo**: `chat-provider-timing-and-speed-2026-08-22.md` and
`mission-control-agent-drop-latency-2026-08-21.md` under
`EterniaLauncher/Launcher_Brain/20 — Active Initiatives/`. Their remedy stages own the
admission, prologue and tool-schema designs; open rows 4–6 above link to them rather than
restate them.
