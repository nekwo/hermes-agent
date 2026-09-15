# Chat-turn prep cost — running record of the 2026-09-07 re-arm (hermes half)

Field notes for [`chat-turn-prep-cost.md`](chat-turn-prep-cost.md) Stages 6–10. This file is written by whoever builds a stage, in the repo the stage stands in (the launcher half of Stage 6 — the `rt_write_ahead_ms` clause and the fixture mirrors — writes its own notes beside `EterniaLauncher/docs/mission_control/planned/runtime-observability.md`). The skill, if one is written, is written LAST from these notes.

## 0. The read that re-armed the plan (Fable, 2026-09-07, read-only)

What was read and how, so the next reader can re-take every number in the plan's §0 without this session:

- **Ledger:** `<store>/mission_chat_turns/persona_chat_personainst_neko_supervisor_agent_f6844ba8_*.json` — the three 07:48–07:49Z turns (root `…894297972f70`) and the 2026-09-06 turns (roots `…115b37660a88`, `…3d6466fce9a8`, `…a9f7d06394ef`, `…cd75c54589eb`, `…6707159dd4c8`). Table script: a plain loop over each record's `phases` and `profile_timing`; the join key for the log is `phases.anchored_at`, NOT `started_at` (§0 preamble of the plan — `started_at` is 0.9–3.2 s later, it is the write-ahead persist stamp).
- **Log:** the neko profile's `logs/agent.log`, pid 28184 (build `42a07c5dfa`, register row `serve_instances/28184.json`, `hermes_home` = the store root's `profiles/base`), lines 03:48:45–03:49:45 local. The prewarm line, the eight `snapshot_build_core` lines with their `sections_top`, the six `snapshot_agents_readiness walk_ms=` lines, the three `snapshot_build_deferred` lines, the three `API call #1 … ttfb=` lines, and the `never_converged … diff=chat_turn_reservations/…` warning.
- **Launcher:** `[MissionChatTiming]` for this morning is unrecoverable — the diag log is deleted on open past 2 MB (`EterniaLauncher/lib/core/telemetry/diag_log_file.dart`) and today's file opened at 13:15:44Z; the receipts file (`diagnostics/mission_transport/receipts.jsonl` under application support) carries no chat-timing kind. The 2026-09-06 lines quoted in `runtime-observability.md` §0.4 were joined instead (plan §0.4).
- **Sandbox profile:** a robocopy of the live root minus `cache`, `lsp`, `logs`, `audio_cache`, `image_cache`, `*_archive`, `migration_backups`, `curator`, `sandboxes`, `events.jsonl` and `*.lock` (2.8 GB; 78 MB of locked files skipped); `HERMES_HOME` → `<copy>/profiles/base`, `HERMES_AGENT_RUNTIME_ROOT` → `<copy>/agent-runtime`, `HOME`/`USERPROFILE`/`APPDATA`/`LOCALAPPDATA` → `<copy>/userhome`; interpreter = the serve's own (the register row's chain). The script imports `hermes_cli.harness`, then calls, in the handler's order: `load_agent_runtime_config`, `_persona_by_id`, `_default_persona_session_db`, `PersonaInstanceStore().ensure_for_personas(ensure_persisted_personas(cfg))`, `store.get(instance)`, `apply_instance_model_overrides`, `_resolve_chat_model_override(requested_override=None)`, `_chat_effective_model_payload`, `_persona_chat_existing_turn`, `mission_chat_turn_record`, `_persona_chat_native_tip`, `_persona_chat_native_history`, `mission_chat_turn_records(session_id=)`, `_persona_chat_native_revision`, `_session_model_config`, `permission_options_for_chat`, `chat_lane_bundle_key_material`, `chat_lane_bundle`, `build_mission_chat_turn_context(agents_file=None, surface_prompt="")`, `mission_chat_prompt_observability(skill_resolver=None)`, `runtime_context_envelope`, `instance_store.update`; each wrapped in `time.perf_counter`, the two builders under `cProfile` on the cold pass and on the TTL-expired pass. Passes: cold → warm immediately → warm immediately (run 1); cold → warm immediately → warm after 17 s (run 2). The numbers in plan §0.3 are run 1's cold and immediate-warm columns and run 2's 17 s column; run 2's cold column read 595 / 339 / 619 for the bundle / context / observability (OS file cache warm from run 1), which is why the plan quotes run 1 for "cold".
- **Not run:** no serve started, no chat turn sent, nothing written under the live root; the write the script performs (queued-skill consume, model-override persist, instance update, the sandbox SessionDB open) landed in the copy.

## 1. Open at re-arm time — what the plan could not measure and says so

- The Mac's ledger (its `write_ahead`, sub-spans, build cadence) — Stage 6 is what makes it a one-grep read.
- Which bundle key component moves on a quiet live turn (`visibility_bundle_builds`=1 on all 15 turns since 08-29) — Stage 6 item 3.
- The GIL share of the prewarm's 5,750 ms (its client closes and probes are I/O-bound; its tool-defs build and `tool_search` activation are not) — Stage 6's `prewarm_overlapped`.
- Defender / disk / filesystem `stat` cost differences between the machines — not evidenced, not claimed.

## 2. Stage 6

Built 2026-09-07 in the worktree `X:/wt/prep-stage6` on branch
`feat/prep-cost-stage6` off `c670168049`. Five items, red-first, each red quoted
below before its fix. The launcher half writes its own notes beside
`EterniaLauncher/docs/mission_control/planned/runtime-observability.md`.

### 2.1 Item 1 — the `timing` block grows six keys

`agent_runtime/mission_chat_phases.py`: `_TIMING_FROM_PHASES` gains
`context_built_ms`, `observability_built_ms`, `write_ahead_ms`, `agent_ready_ms`
and `visibility_bundle_builds`; `_TIMING_FROM_PROFILE` gains `runtime_resolve_ms`
— the one wire key whose source name carries no `profile_` prefix, because the
runner writes it bare. `_TIMING_COUNT_KEYS` replaces the inline
`wire_key == "builds_overlapped"` comparison so the second counter takes the
count ceiling rather than the millisecond one.

**Red first** (`tests/hermes_cli/test_mission_chat_turn_timing_block.py`, ten rows):

```
E       KeyError: 'runtime_resolve_ms'
E           AssertionError: context_built_ms must project onto the terminal payload
E           assert 'context_built_ms' in {'builds_overlapped': 1, 'provider_first_byte_ms': 8000, 'request_assembled_ms': 7000, 'resident_actor_reused': True, ...}
E       AssertionError: assert None == {'agent_ready_ms': 952, 'context_built_ms': 468, 'observability_built_ms': 906, 'runtime_resolve_ms': 12, ...}
E       assert None is not None            (x5, the absence parametrization)
E       AssertionError: assert set() == {'agent_ready...ite_ahead_ms'}
```

**A decision the plan left open: the six are APPENDED to `TURN_TIMING_ORDER`,
not interleaved chronologically.** "Additive in the strict sense (no existing key
moves)" is taken literally — no existing key moves in name OR position — so a
consumer written against the pre-Stage-6 block reads the payload exactly as
before. Nothing reads the block positionally (the launcher's
`MissionRuntimeTurnTiming` reads it by key name), so the only cost is that a
person scanning the tuple finds the pre-admit half below the post-admit half.
Recorded because the other reading is defensible and the next editor should not
have to re-derive which one was taken.

One PRE-EXISTING row had to move.
`test_the_projection_reads_both_instruments_and_renames_neither_wrongly`
asserted `list(block) == list(TURN_TIMING_ORDER)`, which held only while every
scripted input measured all seven keys. It now compares against that order
FILTERED to the keys the input measured — the same property, stated so it
survives the block growing.

### 2.2 Item 2 — the pre-admit sub-spans

Three in `agent_runtime/mission_chat_turn_context.py` (`CONTEXT_TIMING_KEYS`,
returned on `MissionChatTurnContext.timings`), four in
`agent_runtime/prompt_observability.py` (`OBSERVABILITY_TIMING_KEYS`, returned
under `PROMPT_OBSERVABILITY_TIMINGS_KEY`), folded by
`persona_commands._safe_pre_admit_timings` into `_profile_timing` beside
`session_db_open_ms`.

**Red first** (`tests/hermes_cli/test_mission_chat_turn_phases.py`):

```
E           AssertionError: context_skill_preload_ms was measured on this turn and must be recorded
E           assert 'context_skill_preload_ms' in {'resident_actor_reused': 1, 'session_db_open_ms': 0}
E       KeyError: 'context_skill_preload_ms'
E       AssertionError: a blind runner must contribute nothing; only the handler's own measurements may appear
E       assert {'session_db_open_ms'} == {'context_hud...alog_ms', ...}
E       KeyError: 'observability_catalog_cached'
```

Three things the plan did not anticipate, recorded rather than smoothed over:

1. **`safe_turn_profile_timing` did NOT already admit `*_cached`.** The plan's
   item 2 says it does. It admits `*_ms`, `resident_actor_reused` and
   `resident_rebuild_*` and nothing else, so `observability_catalog_cached` AND
   CP-7's `visibility_bundle_rebuild_component_*` were both being dropped
   silently. Two admitted shapes were added — `_PROFILE_TIMING_CACHED_SUFFIX`
   and `_PROFILE_TIMING_BUNDLE_REBUILD_PREFIX`, each ceiling 1 — and the census
   row `test_the_sanitizer_admits_the_handlers_census_and_the_rebuild_flags`
   pins both, plus the two shapes that must still be refused.
2. **The catalog walk and the shared catalog are timed WHERE THEY RUN, not at
   the call site.** `_installed_skill_catalog()` is reached from three places
   inside one build (the builder's own call, the resolver's union pass,
   `available_skills_context`), so timing the builder's call would bill one walk
   of three. A thread-local accumulator (`_accumulate_span`) collects both; the
   builder resets it when it opens its skill block and reads it when the block
   closes. The three spans are DISJOINT by construction — the two walks are
   subtracted out of the block's total — so `observability_skill_rows_ms` is the
   resolve plus the row composition and nothing else, and adding the three up
   returns the block rather than something larger than it
   (`test_the_sub_spans_are_bounded_by_the_spans_they_decompose`).
3. **`observability_catalog_cached` keys on the walk COUNT, not the walk MS.** A
   walk that finished under half a millisecond still walked, and rounding it to
   `0 ms` would report the TTL as having held.

**The `timings` mapping leaks to a THIRD consumer, and the fixtures caught it.**
The plan says the mapping is "stripped before persist". It is — but the SNAPSHOT
lane builds rows through the same function with no handler in between and puts
them straight onto the read-model frame, so the first producer-contract run went
red with two rows of the launcher's `delta_agent_create_narrow_profile.json`
carrying a wall-clock-dependent mapping onto byte-pinned bytes. Fixed at the seam
its two neighbours already use: `_evict_builder_timings(chat_contexts)` beside
`_evict_final_model_input` / `_evict_prompt_layer_content`. All three exits now
drop it — the handler pops it, the persist chokepoint pops it, the frame evicts
it — pinned by `test_the_builders_own_sub_spans_never_reach_the_FRAME` and
`test_the_persisted_row_drops_the_sub_spans_too`.

### 2.3 Item 3 — CP-7, a rebuild names the component that moved

`agent_runtime/chat_lane_bundle.py`'s `_memo` becomes `(key, material, bundle)`;
`chat_lane_bundle` composes the material once and hashes it itself rather than
calling `chat_lane_bundle_key` (which would compose it a second time). On a key
mismatch `_note_key_material_moves` records the differing TOP-LEVEL entry names,
and the handler folds them as `visibility_bundle_rebuild_component_<name>=1`.

**Red first** (`tests/agent_runtime/test_chat_lane_bundle.py`,
`tests/hermes_cli/test_mission_chat_turn_phases.py`):

```
E       AttributeError: module 'agent_runtime.chat_lane_bundle' has no attribute 'key_material_moves_this_thread'   (x4)
E       KeyError: 'visibility_bundle_rebuild_component_registry_epoch'
E       AssertionError: a rebuild on this turn must name at least one component
E       assert []
```

Shape decisions:

* **Top-level names only.** `permission` is a nested dict, and "the permission
  fingerprint moved" is the actionable fact; descending would trade one honest
  name for six that all mean the same thing.
* **A FIRST build names nothing.** "Built for the first time" and "rebuilt
  because an input changed" are different facts, and naming every component on a
  cold lookup would make every cold turn look like a cache that will not hold.
* **Cumulative + cursor, never reset**, exactly like `bundle_builds_this_thread`:
  serve runs concurrent turns on pooled threads. The cursor rides the turn plan
  (`MissionChatTurnPlan.bundle_key_material_cursor`) for the same reason
  `session_db_open_ms` does — sampled in the plan phase at the anchor, read in
  `_mission_chat_commit_turn`, and the plan IS the declared boundary between
  those two functions. The remembered-names list is bounded at 64.
* **Names, never values**, bounded twice: at the source (top-level keys of a
  dict this module owns) and again at the fold (lowercase ASCII, at most 40
  chars, `[a-z0-9_]` only).

### 2.4 Item 4 — CP-2 as a recorder

`agent_runtime/turn_activity.py` (new): `chat_turns_admitted()` and
`admitted_turn()`. `stream._chat_turns_admitted()` forwards it and the
`snapshot_build_deferred` line gains `admitted_at_exit=`, reading `unknown` when
the module cannot be consulted. `persona_chat_actor_prewarm` gains a
construction-span ledger (`record_construction`, `overlapping_constructions`,
`reset_construction_spans_for_tests`, `_ConstructionSpan`) shaped exactly like
`snapshot_build_ledger`, and `phases.prewarm_overlapped` is counted beside
`builds_overlapped` off the same window.

**Red first** (`tests/agent_runtime/test_snapshot_demote_deferral.py`):

```
E       ImportError: cannot import name 'turn_activity' from 'agent_runtime' (X:\wt\prep-stage6\agent_runtime\__init__.py)   (x4)
E       AttributeError: <module 'agent_runtime.stream'> has no attribute '_chat_turns_admitted'
E       AttributeError: module 'agent_runtime.persona_chat_actor_prewarm' has no attribute 'reset_construction_spans_for_tests'   (x2)
```

**DEVIATION, and the reason.** The plan says the context manager is "entered by
the handler at the anchor". `_cmd_mission_chat_message`'s plan phase is ~700
lines with a dozen refusal returns above the lease, and the commit phase it
dispatches to has fourteen terminal transitions; a `with` around the body means
re-indenting the most-live code in the harness, and an explicit
increment/decrement pair repeated at every exit is exactly the shape that leaks
one and wedges the demote lane for the life of the process once Stage 7 reads
it. So it is a decorator, `_within_admitted_turn`, applied to the handler.
`functools.wraps` keeps `inspect.getsource` and the AST gates over
`_cmd_mission_chat_message` reading the real function (both re-run green:
`test_s26_retired_mission_chat_task_goal_flags.py`,
`test_mission_chat_relay_guard.py`). The window it opens is the handler's first
instruction and the anchor is two local imports later, so for every purpose this
counter has, they begin at the same instant.

**A prewarm that stood DOWN records no span.** `_ConstructionSpan` opens past
the first `agent_runs_in_flight()` yield and covers `_prepare` +
`runner.prewarm` — which is the whole of the §0.2 line it exists to bill
(`elapsed_ms=5750`), since `_prepare` reads SessionDB, resolves the lane bundle
and composes the runtime signature. A refusal that constructed nothing must not
be counted as a span some turn overlapped.

`prewarm_overlapped` is a fourth `PHASE_COUNTER`, so it is a new key in the
`phases` block and not on the wire `timing` block. Stage 6 records it; nothing
reads it to decide anything.

### 2.5 Item 5 — the join rule, and the canon it landed in

The join rule is in `../07-observability.md` beside the `phases` census: join
`agent.log` on `phases.anchored_at`; `started_at` is the write-ahead persist
stamp, 0.9–3.2 s later. That census also grew `prewarm_overlapped`, the seven
handler sub-spans, CP-7's flag family and the sanitizer's two new admitted
shapes. `../05-chat-turn-lane.md` § 2 and § 2a grew the same facts on the owner
side, including the thirteen-key projection table.

**Collateral: 57 cite tokens and 21 waiver keys renumbered.** The source
insertions shifted `stream.py` by +26 then +32, `persona_commands.py` by +2 /
+156 / +215, and four other cited files — pointing 23 previously-green canon
cites at unrelated text. Renumbered by a difflib old→new map over
`git show HEAD:<path>` against the worktree file (never by a constant offset:
the offsets differ per hunk) across 01/02/03/04/05/07/08, and
`cite-adjacency-baseline.json`'s waiver keys renumbered with them — a waiver
follows its cite, precedent `d4cca42fe1`. FIVE waivers were then genuinely
stale and were DELETED rather than renumbered: their cites are the sentences
this stage rewrote out of 07's census paragraph. Baseline 72 → 67 keys;
unwaived failures 0.

### 2.6 Gates

Run in `X:/wt/prep-stage6`:

| gate | verdict |
|---|---|
| `tests/hermes_cli/test_mission_chat_turn_timing_block.py` | 31 passed |
| `tests/hermes_cli/test_mission_chat_turn_phases.py` | 48 passed |
| `tests/agent_runtime/test_chat_lane_bundle.py` | 18 passed |
| `tests/agent_runtime/test_snapshot_demote_deferral.py` | 18 passed |
| `tests/agent_runtime/test_snapshot_prompt_hoist.py` | 18 passed |
| the plan's full gate list (seven paths) | 214 passed |
| `EterniaLauncher/tool/test_quality/check_producer_contracts.py --hermes-root=X:/wt/prep-stage6` | `producer contract fixtures match Hermes: stream frames + response envelopes` — RED before `_evict_builder_timings`, green after |

**A pre-existing red, not this stage's.**
`tests/agent_runtime/test_harness_serve.py::test_ready_line_and_exit_frames`
fails identically on clean `main` in `X:/Eternia/hermes-agent`
(`assert 'stderr' == 'ready'`): the linked SQLite 3.45.3 WAL warning becomes a
`stderr` frame ahead of `ready`. Environmental; rowed here, not fixed here.

### 2.7 Owed

* The gate is an operator read, not a number: one agent-chat turn per machine
  whose `[MissionChatTiming]` line carries `rt_write_ahead_ms`, and whose record
  carries the seven sub-spans and — on the PC — at least one
  `visibility_bundle_rebuild_component_*` name.
* §4.1's ledger row for Stage 6 fills at landing.

## 3. Stage 7

**Status: BUILT, pending Fable verification and landing.** Branch
`codex/prep-cost-stage7-admitted-turn`, worktree `X:/wt/prep-stage7`, cut from
local `main` at `68b8361de0`. Nothing pushed, nothing merged; §4.1's landed sha
stays empty until it actually lands.

**Scope, and why it is only this.** CP-2 and CP-3 made live at the two sites
Stage 6 instrumented and deliberately left alone. Stage 6's own `turn_activity`
module is unchanged — it was already the sole admission authority, and its
process-wide count already covers the same-root prewarm case, so no second
per-root admission map was added. The handler's `_within_admitted_turn`
decorator and its `finally` cleanup are untouched.

### 3.1 The source changes

| file | change |
|---|---|
| `agent_runtime/stream.py` | `SNAPSHOT_DEMOTE_DEFERRAL_MAX_MS` 1,000 → **3,500** (CP-3); new `_a_turn_holds_the_gil(admitted=, in_flight=)` union helper; `_defer_demote_build_for_active_turns` reads admitted OR running **at entry and at every poll**; the exit receipt samples **both** counters freshly |
| `agent_runtime/persona_chat_actor_prewarm.py` | both yield reads — before `_prepare` and before `runner.prewarm` — take `chat_turns_admitted() > 0 or agent_runs_in_flight() > 0`; the module docstring's stated guard corrected to match |

Preserved deliberately, each with a test naming it: cancellation, the finite
deadline, the 25 ms poll cadence, demote-only eligibility (hydrate / `boot` /
`full_core` never wait), the existing `skipped_turn_active` outcome token, the
build and prewarm span ledgers, and `builds_overlapped` still counting a build
that exhausts the bound and overlaps anyway — the deferral must not be able to
launder its own failures out of the receipt.

**`None` handling, stated because it is a real decision.** Both forwarders
answer `None` when their module cannot be consulted. Stage 5's rule — unknown
means "do not defer" — is preserved: an unreadable counter contributes nothing
to the union. What it must not do is cancel a deferral the *other* counter
already earned, which is why the helper is a union of two independently-falsy
reads and not one fused gauge. `unknown` is still preserved on the receipt.

### 3.2 Red first, then green

The Stage 7 test IDs proposed in the implementation brief were authored against
the pre-fix tree and **measured red there**. The three guard tests pass pre-fix
by design — they pin behaviour Stage 7 preserves, not behaviour it flips, and a
guard test that started red would be pinning the wrong thing.

Run: `pytest tests/agent_runtime/test_snapshot_demote_deferral.py tests/agent_runtime/test_persona_chat_actor_prewarm.py`

| test ID | pre-fix | post-fix |
|---|---|---|
| `test_snapshot_demote_deferral.py::test_admitted_before_runner_defers_demote` | **RED** | green |
| `…::test_demote_bound_covers_two_seconds_but_releases_at_3500` | **RED** | green |
| `…::test_the_bound_covers_the_measured_pre_admit_p95_and_is_a_constant` | **RED** | green |
| `test_persona_chat_actor_prewarm.py::test_admitted_same_root_skips_before_prepare` | **RED** | green |
| `…::test_admission_during_prepare_skips_before_construction` | **RED** | green |
| `test_snapshot_demote_deferral.py::test_admission_release_exception_and_refusal_do_not_leak` | green (guard) | green |
| `…::test_hydrate_full_core_and_cancel_preserve_bypass` | green (guard) | green |
| `…::test_an_unreadable_admitted_counter_still_defers_for_a_live_run` | green (guard) | green |

Pre-fix `5 failed, 48 passed`; post-fix **`53 passed`, exit 0**.

**One pre-stage test was replaced rather than deleted.** Stage 6's
`test_stage_six_changes_no_deferral_DECISION` asserted the opposite of Stage 7
on purpose, and said so in its own docstring: *"a demote build requested while a
turn is admitted but not yet running must therefore still proceed today — and
this row is the one Stage 7 flips."* This is that flip. The block replacing it
carries the same sentence as its header comment, so the boundary stays readable
in the diff. Likewise `test_the_bound_is_one_second_and_is_a_constant_not_a_literal`
became `test_the_bound_covers_the_measured_pre_admit_p95_and_is_a_constant` —
the VALUE is still pinned separately from the behaviour, so a later edit cannot
move both together and stay green.

### 3.3 Mutation proof

Each mutation applied to the worktree source, the focused set re-run, then
**restored before commit** — verified afterwards by `git diff` against the
staged tree showing only the test files. No mutation survived.

| mutation | reds |
|---|---|
| deferral decides on the run counter alone (admission dropped from the union) | `test_admitted_before_runner_defers_demote`, `test_demote_bound_covers_two_seconds_but_releases_at_3500` |
| bound left at Stage 5's 1,000 | `test_the_bound_covers_the_measured_pre_admit_p95_and_is_a_constant`, `test_demote_bound_covers_two_seconds_but_releases_at_3500` |
| second prewarm check removed | `test_admission_during_prepare_skips_before_construction` |
| first prewarm check drops admission | `test_admitted_same_root_skips_before_prepare` |

### 3.4 Sandbox and gates

All runs under an isolated sandbox: `HERMES_HOME`, `HERMES_HEAD_HOME`,
`HERMES_AGENT_RUNTIME_ROOT`, `HOME`, `USERPROFILE`, `APPDATA` and
`LOCALAPPDATA` redirected to a throwaway tree and **echoed before every run**;
the worktree's own code on `PYTHONPATH`; `PYTHONDONTWRITEBYTECODE=1`. The
interpreter is the repo's shared TEST venv (`$HOME/.venvs/hermes-test`, one of
`scripts/run_tests.sh`'s own candidates), resolved *before* `HOME` is redirected
and then pinned by absolute path — read and executed only, never written. The
live store and the live venv were not written, and nothing here started a serve.

### 3.5 The canon cite remap this stage owed

`stream.py` grew 40 lines, and the dead-link gate
(`tests/scripts/test_doc_cite_adjacency.py`) reds on line-numbered cites into it.
Clean `main` passes that gate (40 passed), so every failure was this branch's
drift. **Twenty-seven** cite tokens across five canon docs were remapped — not
only the thirteen the gate flagged, because a cite that still passes the gate by
luck while pointing at the wrong code is worse than one that fails.

The offsets were derived from this branch's own diff hunks, per range, never as
a blanket shift:

| original line range | offset |
|---|---|
| ≤ 88 | +0 |
| 89–141 | +10 |
| 142–180 | +31 |
| 181–193 | +32 |
| 194–212 | +33 |
| 213–214 | +39 |
| ≥ 215 | +40 |

Every remap was then **verified byte-identical** — old line content against new
line content — before it was written, and the applier refuses any token that is
absent from its stated doc line, repeated on it, or ambiguous between the bare
`stream.py:N` and prefixed `agent_runtime/stream.py:N` spellings.

**Eight of the twenty-seven were WAIVED cites**, and the waiver keys in
`cite-adjacency-baseline.json` carry the line number, so moving a cite without
moving its key reds the gate twice — once as an unwaived failure at the new
line, once as a stale waiver at the old one. The eight keys were renumbered to
follow their cites. **No waiver was added and none was deleted; the count is
unchanged at 67.** This follows the file's own precedent, recorded in its
`_comment` for the 2026-09-04 D7h shift that moved `stream.py` by 47 lines, and
this stage appended its own amendment note there in the same shape.

**Two pre-existing stale cites found in passing, and deliberately NOT fixed
here.** Both already pointed at unrelated code on clean `main` — they were green
because they are waived, not because they are right:

* `03-transport-and-wire.md:874` cites `stream.py:108-148` for the claim that
  `pid` rides last on both build families; those lines are the run-counter
  forwarders and the deferral.
* `07-observability.md:218` cites `stream.py:177-182` for the
  `BUILD_SECTIONS_WAIT_THRESHOLD_MS` WAIT line; those lines are the deferral's
  early return.

They were remapped mechanically so they point at the same code they pointed at
before (`118-179` and `208-214`), preserving the status quo rather than silently
rewriting prose this stage was not asked to touch. **Rowed for whoever owns the
07/03 currency pass.** The `03:874` range now also encloses the 21 inserted
lines of `_a_turn_holds_the_gil`, which is unavoidable for a contiguous range
citing code that was split by an insertion.

### 3.6 The cross-stack checks, and the one fixture Fable must re-capture

Both launcher-side hermes checks were run against this candidate, sandboxed:

| check | result |
|---|---|
| `tool/test_quality/check_producer_contracts.py --hermes-root=X:/wt/prep-stage7` | **exit 0** — `producer contract fixtures match Hermes: stream frames + response envelopes` |
| `tool/hermes_serve_frames/generate.py --hermes-root=X:/wt/prep-stage7 --check` | **exit 1** — `ready.json: committed bytes differ from a fresh capture` |
| the same generator against clean hermes main `68b8361de0` | exit 0 (control — the red is this branch's, not the tree's) |

**The red was read rather than regenerated away.** A fresh capture from this
branch was diffed against the committed fixture, and **exactly two fields
move**:

```
capture.hermes_commit : 3eb8cd43a2… -> 4a59b76fc9…
frame.build.code_tree : c3864c6291… -> 60e5a4a692…
```

`hermes_commit` is capture provenance, not a frame field (the checker ignores
it — it prints `captured from …, probed …` notes for all 24 other frames and
passes them). `build.code_tree` is the build-identity hash landed on
2026-09-07 by `3eb8cd43a2` — "a commit is not a build, so the row says which
code" — and it is a hash OF `agent_runtime`. **No field the launcher's decoder
switches on moved.** That is the evidence for "Stage 7 changes no wire
contract", and it is why this is a fixture recapture rather than a contract
change.

**Deliberately NOT re-captured on this branch.** The captured `code_tree` and
`hermes_commit` are this branch tip's, and both are wrong the moment the branch
is rebased or landed — Fable's landed sha is not `4a59b76fc9`. Committing them
here would bake a stale hash into a byte-pinned fixture and hand the next reader
a green gate certifying the wrong build. **Landing step for Fable:** after
Stage 7 is on hermes main, re-run

```
python tool/hermes_serve_frames/generate.py --hermes-root=<landed hermes main> --python=<interpreter>
```

and commit the refreshed `ready.json` in the launcher, exactly as launcher
`0691128d9` did when `code_tree` first landed.

**The launcher gates on the claim branch** (`codex/prep-cost-stage7-claim`,
one queue line in `Launcher_Brain`): `flutter analyze` — *No issues found*;
`tool/stagec_qa_mcp_server` `no_dead_docs_link_test.dart` — *All tests passed*;
`flutter test test/features/mission_control test/architecture` — **+7926 ~1,
All tests passed, exit 0**.

**One flake seen and chased down, recorded so a later red is not misread as
this stage.** The FIRST full-suite run on that branch reported
`+7925 ~1 -1` with
`test/features/mission_control/mission_boot_anchor_receipt_test.dart`
("receipts.jsonl opens with the anchor line, then session_start") failing. It
was not attributed by assertion — it was tested three ways: standalone on clean
launcher `main` (11 passed), standalone in the claim worktree (11 passed), the
full suite on clean launcher `main` (+7926 ~1, passed), and the full suite on
the claim branch a second time (+7926 ~1, passed). A one-line Brain markdown
change has no mechanism to reach a receipts end-to-end test, and the totals
match clean main exactly. It is a full-suite flake in that test — most likely
contention on its receipts file — and it is **rowed, not fixed here**.

### 3.7 Deviations from the plan text

1. **The union is a named helper, not an inline `or`.** `_a_turn_holds_the_gil`
   exists so the `None`-is-unknown rule is stated once and tested once, rather
   than duplicated at the entry check and the poll check where the two could
   drift apart.
2. **`runs_in_flight_at_exit` is now re-sampled at the receipt**, where it
   previously reported whichever poll ended the loop. On the deadline path —
   the path whose honesty matters most, because something was still holding —
   the old value was one poll stale. The brief asked for both counts sampled
   freshly; this is that, and because it slightly changes an existing key's
   meaning it is called out rather than buried.
3. **A guard test was added beyond the brief's list**
   (`test_an_unreadable_admitted_counter_still_defers_for_a_live_run`). Going
   from one gauge to two makes "unknown" ambiguous in a way it was not before,
   and nothing in the brief's list covered it.
4. **No launcher CODE slice, but one launcher fixture must be re-captured at
   landing.** Stage 7 changes no wire, projection or timing key — proved, not
   asserted, in §3.6 below. It does, however, change `agent_runtime` source, and
   the `ready` frame carries `build.code_tree`, a hash OF that source. So the
   byte-pinned `ready.json` fixture moves by design.

### 3.8 Owed — what this branch does NOT establish

* **The number.** CP-1's verdict for Stage 7 is a field read, not a test result:
  ten consecutive PC agent-chat turns with pre-admit build overlap zero on ≥ 9,
  `write_ahead` p50 ≤ 1.3 × the same-day uncontended p50, and no prewarm overlap
  on a newly-opened root. Derive pre-admit overlap from the anchored interval
  and the actual build spans — do not relabel an existing whole-turn counter as
  pre-admit. A missed target is a finding, not permission to widen this stage
  into hydrate deferral or eviction surgery.
* **CP-9's Stage 6 read: ABSENT while this branch was built, TAKEN on the PC
  immediately after.** Checked read-only while preparing the branch,
  2026-09-07: the launcher diag log (318,191 B) contained **zero**
  `rt_write_ahead_ms`, and the live serve's register row read build
  `c670168049` — pre-Stage-6. That is why this pass built Stage 7 alone and
  left Stages 8 and 10 untaken.

  **A correction to how that was checked.** The record-side half of that claim
  ("none of the 50 records carries a sub-span") was produced by a probe that
  read `record['phases']` and `record['profile_timing']` directly. Each file
  under `mission_chat_turns/` is keyed by TURN ID at the top level and the
  phases live one level down, so the probe read `None` for every field of every
  record and would have said "absent" whatever the file held. The conclusion
  was right for the other two reasons — no `rt_write_ahead_ms` on any launcher
  line, and a pre-Stage-6 build in the register row — but one leg of it was not
  evidence. Recorded because the same probe shape would mislead the next reader.

  **The read, taken 2026-09-08T00:03–00:05Z (PC).** Operator rebuilt the
  launcher and restarted; serve pid 33460 came up at 00:00:43Z on `68b8361de0`
  (`code_tree c3864c6291…`), which contains Stage 6 and **not** Stage 7. Nine
  agent-chat turns, all `projected`, all provider-submitted. All nine carry the
  seven sub-spans, and all nine carry
  `visibility_bundle_rebuild_component_registry_epoch`; the launcher line
  carries `rt_write_ahead_ms=` and `rt_bundle_builds=`. **The PC half of CP-9 is
  closed.** The Mac half cannot be taken: Stage 6 is unpushed on both repos
  (hermes local main is 9 commits ahead of `origin/main` `42a07c5dfa`, launcher
  30 ahead of `43b751e14`), so the Mac cannot obtain it.

  | turn | `write_ahead` | ovl | preload | hud | sig | obs rows | walk | shcat | cached |
  |---|---|---|---|---|---|---|---|---|---|
  | 69bbf2b0 | 421 | 1 | 125 | 15 | 0 | 203 | 0 | 31 | 1 |
  | 6ef3108a | **438** | **0** | 139 | 16 | 0 | 171 | 0 | 16 | 1 |
  | 18cdd8d2 | **593** | **0** | 280 | 16 | 0 | 157 | 62 | 31 | 0 |
  | 77a02631 | 796 | 1 | 202 | 0 | 16 | 469 | 0 | 31 | 1 |
  | ae94d0e3 | 844 | 1 | 171 | 0 | 16 | 484 | 0 | 62 | 1 |
  | 1135dcee | 891 | 1 | 202 | 0 | 16 | 516 | 0 | 77 | 1 |
  | e33502ed | 891 | 2 | 203 | 0 | 0 | 547 | 0 | 46 | 1 |
  | 4e7c51e8 | 921 | 1 | 484 | 0 | 15 | 282 | 0 | 31 | 1 |
  | 6e40c028 | 1,016 | 1 | 500 | 14 | 0 | 374 | 0 | 31 | 1 |

  These nine are the WINDOWS turns only. Three more turns followed at
  00:09:44–00:10:17Z on the method lane to the remote install
  (`912c69ce-…`, the Mac): they carry `rt_write_ahead_ms=-` and
  `rt_bundle_builds=-`, which is the three-absences rule reading correctly on a
  runtime that predates the key. They are excluded from every number here —
  they have no sub-spans to give, and Windows is the machine the operator's
  question is about. They do turn the Mac half of CP-9 from an inference into a
  measurement: the Mac ran real turns and reported the dash, so its runtime
  demonstrably predates Stage 6, exactly as "unpushed" predicts. As a
  by-product they are the only proof so far that Stage 6's launcher clause
  prints `-` rather than a zero or an omission against a genuinely older
  runtime — the PC alone could not demonstrate that.

  **The Windows numbers, computed over those nine:**

  | statistic | value |
  |---|---|
  | `write_ahead` p50, all nine | **844 ms** |
  | `write_ahead` p50, uncontended (2 turns) | **516 ms** (438, 593) |
  | `write_ahead` p50, contended (7 turns) | 891 ms |
  | CP-1 target ≤ 300 ms uncontended | **NOT MET** at 516 |
  | Stage 7 target p50 ≤ 1.30 × same-day uncontended p50 | **1.64 ×** — this is the PRE-Stage-7 reading, and it is the ratio Stage 7 has to move |
  | `context_skill_preload_ms` p50 | 202 (125–500) |
  | `observability_skill_rows_ms` p50 | **374** (157–547) |
  | `observability_catalog_walk_ms` p50 | 0 (one 62, the single `cached=0` turn) |
  | `observability_shared_catalog_ms` p50 | 31 (16–77) |

  **Skill work is 74–89 % of `write_ahead`, median 88.2 %**
  (`context_skill_preload_ms` + `observability_skill_rows_ms` +
  `observability_shared_catalog_ms` + `observability_catalog_walk_ms` against
  the turn's own span, per turn). That is the whole case for Stage 8 in one
  number, and it is measured on live operator turns rather than derived from
  the §0.3 sandbox profile.

  **This is the pre-Stage-7 baseline, and it is not Stage 7's result** — Stage 7
  is unlanded and absent from `68b8361de0`. Against §0.1's pre-Stage-6 read, the
  CONTENDED band moved from 2,796–3,172 ms to 796–1,016 and the uncontended
  figure from 906 to 438/593. The cause is not this stage and is not claimed by
  it: no build storm is present in this window (`rt_bundle_builds=1` and
  `builds_overlapped` 0–2 per turn, against §0.2's eight led builds in 55 s).
  Stage 7's own number must still be taken against a same-day uncontended p50.

  **What the sub-spans now bill, on measured numbers rather than a sandbox
  profile:** `observability_skill_rows_ms` is **157–547 ms** (median 374)
  against Stage 8's ≤ 30 ms target, and `context_skill_preload_ms` is
  **125–500 ms** against a ≤ 250 ms whole-context target. Together they are
  ~60–80 % of every `write_ahead` above. `observability_catalog_walk_ms` is
  already 0 on eight of nine — the one 62 ms walk is the single turn with
  `observability_catalog_cached=0`, which is the 15 s TTL missing exactly as
  §0.3 predicted. **Stage 8 is now billed on live turns, not only on the
  sandbox.**

  **A plan open question, answered.** §1 listed "which bundle key component
  moves on a quiet live turn" as unmeasured. It is `registry_epoch`, on **nine
  turns out of nine**, including the uncontended ones with no prewarm in
  flight — so the epoch bump is not only the prewarm's registration cycle that
  old §7.4 accepted.
* **A restart warning is owed before landing.** This is runtime code, not docs:
  Fable tells the operator the local runtime will restart before landing it.

## 4. Stage 8

**Status: BUILT, pending verification and landing.** Branch
`codex/prep-cost-stage8-one-walk`, worktree `X:/wt/prep-stage8`, cut from main
`483bcf6fab` (Stage 7 landed). Nothing pushed, nothing merged; §4.1's landed sha
stays empty until it actually lands.

**Released against the CP-9 read, not against the plan's sandbox profile.** §3.8
records the read: skill work is 74–89 % of `write_ahead` (median 88.2 %) on nine
live Windows turns, with `observability_skill_rows_ms` at 157–547 ms against a
30 ms target. That is what put this stage in scope.

### 4.1 What the source audit changed about the stage's shape

Two findings, both recorded as clarifications in the plan's Stage 8 section
before any code was written (CP-4a, CP-4b, CP-5a there):

1. **`_skill_root_registry`'s cache does not skip the walk.** It is keyed on the
   root and validated BY fingerprint, so reaching it at all re-runs
   `iter_skill_index_files`, a whole-root `rglob("*.md")` and a `stat` per path;
   only the frontmatter parse is skipped on a hit. Sharing *resolution results*
   while each lane still called it would have moved nothing. **Sharing the
   registry map is the load-bearing move**, and it is the only one that removes
   filesystem work.
2. **There is a fourth walker, and the plan's §0.3 did not name it.** §0.3
   counted three (`resolve_skills` ×2, `_installed_skill_catalog`,
   `build_shared_catalog`). But `used_skills_context` calls
   `_resolved_skill_receipt(name)` per name, which called the SINGULAR
   `resolve_skill(name)` — **one full per-root walk per used / queued /
   required-preload skill name**, unbatched, and sitting inside the very span
   the read measured at 157–547 ms. A turn naming a dozen skills walked every
   root a dozen times.

### 4.2 The change

| file | change |
|---|---|
| `agent/skill_utils.py` | `resolve_skill` (singular) gains `_root_registries`, the same in/out accumulator `resolve_skills` already had; `required_preload_skill_ids` gains it and forwards it; new thread-local `skill_root_walks_this_thread()` counts WALKS |
| `agent_runtime/prompt_observability.py` | `used_skills_context` takes `root_registries` and threads it to every `_resolved_skill_receipt`; the in-turn call site passes the resolver's own map |
| `agent_runtime/mission_chat_turn_context.py` | `build_mission_chat_turn_context` and `_resolve_skill_preload` take `root_registries`; `_default_required_preload_skills` forwards it; `_required_preload_skills` adapter offers the keyword only to resolvers that accept it |
| `agent_runtime/skills_inventory.py` | `_content_hash` memoized on `_package_fingerprint` — `(relpath, mtime_ns, size)` over exactly the files it hashes |
| `hermes_cli/harness_parts/persona_commands.py` | one turn-local `_turn_root_registries` map, handed to the context builder and to a `_turn_skill_resolver` built around the same object |

**Why keying by resolved root PATH settles CP-5a.** The two lanes can
legitimately enumerate different root LISTS — the observability row runs inside
`persona_profile_scope` and the context builder does not, and
`get_all_skills_dirs()[0]` is `get_hermes_home()`-relative. A per-path key needs
no agreement about the list: a root both lanes see is walked once, a root only
one lane sees is walked by that lane, and a registry is a pure function of its
root's contents either way. No lane can be handed a root it did not ask for,
which a tuple-keyed "same list or nothing" scheme would also have achieved but
only by falling back to two full walks whenever the lists differed.

**Turn-local by construction.** The map is born in the handler frame, dies with
it, and is never attached to the context, the row, a persisted record or a wire
frame — the brief's "do not serialize them" rule, satisfied by never giving them
a home outside the frame rather than by remembering to strip them. This also
sidesteps the Stage 6 hazard where `timings` leaked into the snapshot lane's
`chat_contexts[]` and had to be evicted at three exits.

**Degradation is to the old behaviour, never to a failure.** `_turn_skill_resolver`
returns `None` on any construction failure, and `mission_chat_prompt_observability`
already builds its own resolver when handed `None`. A turn must not fail because
an optimisation could not be constructed.

### 4.3 Red, green, mutation

Run: `pytest tests/agent/test_skill_utils.py tests/agent_runtime/test_skills_inventory.py`

**A deviation to state plainly: the walk-count tests were authored AFTER the
implementation, not before it.** Measured against the pre-stage source they show
`3 failed, 3 passed`, but two of those three reds are `AttributeError` on the
new walk counter rather than a behavioural disagreement — the counter is part of
the stage, so reverting the stage removes the instrument too. Only
`test_one_registry_fingerprint_walk_per_root_per_turn` is a genuine behavioural
red there. **The mutation proof below is therefore the load-bearing evidence for
this stage**, not the red-first run, and it is reported that way rather than
dressed up.

Green: **34 passed** (`test_skill_utils.py`), **11 passed**
(`test_skills_inventory.py`), and the full gate set **286 passed, exit 0**.

| mutation | reds |
|---|---|
| `resolve_skill` ignores the shared map (the fourth walker returns) | `test_one_registry_fingerprint_walk_per_root_per_turn` |
| the preload policy stops forwarding the map | `test_one_registry_fingerprint_walk_per_root_per_turn` |
| the shared map loses its per-root key (one bucket for all roots) | 5 tests, including two PRE-EXISTING ones — `test_resolve_skills_batched_matches_per_name_resolve_skill` and `test_cached_skill_registry_preserves_root_precedence_and_profile_classification` |
| the package-hash memo stops checking its fingerprint | 3 tests, including the pre-existing `test_content_hash_tracks_content_changes` |
| the package fingerprint omits support files | `test_support_file_change_invalidates_shared_catalog_hash`, `test_two_packages_are_cached_independently`, `test_the_fingerprint_covers_exactly_the_files_the_hash_reads` |

All five killed; every mutation restored before commit, verified by an empty
unstaged source diff. That two mutations are caught by tests written before this
stage is the stronger signal — the shared map has to preserve precedence,
collision and content-change semantics that were already pinned.

**An honest limitation, pinned rather than papered over.**
`test_same_size_same_mtime_edit_is_a_known_fingerprint_limitation` asserts that a
same-size edit forced to the same `mtime_ns` is invisible to the package
fingerprint. That is inherited verbatim from
`skill_utils.skill_package_content_hash`, which has keyed the identical file set
this way since before this stage; it is recorded so the next reader meets it as
a known property with a named owner.

**Blast radius, checked because `skill_utils` is imported far outside the chat
lane.** Every test file in the repo that touches `resolve_skill`,
`required_preload_skill_ids`, `_skill_root_registry`, `build_shared_catalog`,
`used_skills_context` or `skill_package_content_hash` was run: skill commands 22,
skill utils 34, agent-create service 47, MCP admission 74, persona skill policy
27, profile context 12, profile readiness 10, dead-symbol census 16, skill
promotion 51, skills inventory 11, chat capability visibility 18, skills delete
verbs 25 — **all green**.

One file could not be run: `tests/agent_runtime/test_realm_sync_skill_inbox.py`
hangs in a subprocess (`CreateProcess` / `tools/environments/base.py::_drain`)
under the sandbox. **It hangs identically on clean `main`**, so it is
environmental and pre-existing, not this stage; rowed here, not fixed here. A
`tests/agent` directory-wide run hits the same class of hang and was abandoned
rather than reported as a red — the plan's gate list is the bounded set, and
"no unbounded full Hermes suite" is a standing rule.

### 4.4 The canon cite remap this stage owed

The edits shifted `persona_commands.py` by 41 lines,
`mission_chat_turn_context.py` by 11 and `prompt_observability.py` by 1.
**Fifteen live cites across four canon docs** were re-anchored, each verified
byte-identical between the old and the new line before it was written, and
**three waived keys renumbered** to follow their cites (none added, none
deleted; the count stays 67). The baseline's `_comment` carries the amendment in
the same shape as its 2026-09-04 and Stage 7 precedents.

Two judgement calls worth naming:

* **Cites under `archive/` were deliberately NOT re-anchored.** A first pass
  remapped them; that was reverted. The archive is a record of what was true
  when it was written, and one existing waiver already refuses re-anchoring on
  exactly that ground ("QUOTED ROT, not a live cite … re-anchoring it would
  falsify the record").
* **One cite was missed by the automated pass and fixed by hand** —
  `05-chat-turn-lane.md:466`'s `` `:526-539` `` is a BARE continuation cite with
  no basename before the colon, which the scanner's pattern did not match. Worth
  knowing: any future remap script that keys on a filename will silently skip
  every continuation cite in the canon.

### 4.5 Owed

* **The numbers.** The stage's targets are a sandbox re-take plus ten live turns
  after landing: uncontended observability ≤ 150 ms, context ≤ 250 ms, warm
  `observability_catalog_walk_ms` = 0 and skill-row composition ≤ 30 ms, with
  cold / immediate-warm / 17-second-warm reported separately. **Nothing here
  claims a millisecond** — the CP-9 read gives the before (`observability_skill_rows_ms`
  p50 374, `context_skill_preload_ms` p50 202), and the after is owed.
* **Stage 9's gate reads off that re-take**, not off this branch: Stage 9 is
  REFUSED if live observability comes in at or under 150 ms. Missing live data
  never opens Stage 9.
* **A restart warning** before landing — this is runtime code.
* The two out-of-turn `used_skills_context` callers (the persisted record and
  the snapshot item) still pass no map and still resolve per name. They are not
  on the turn path and were left alone deliberately; if the snapshot lane ever
  bills for it, its own build-scoped registries are already the map to pass.

## 5. Stage 9

## 6. Stage 10
