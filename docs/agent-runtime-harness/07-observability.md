# 07 — Observability: the receipts, their contracts, their consumers

How this runtime is measured, and by what. Every receipt below was read out of
its emitter before it was written down: it appears here in the format string the
emitter holds, with that emitter's `path:line`. Three surfaces carry the whole
picture — hermes log receipts in `<HERMES_HOME>/logs/agent.log`, the durable
turn record's `phases` block, and the launcher's diag log. They join by **id**,
never by time, and each has a consumer that fails loudly when it reads nothing.

## An unrun gate is indistinguishable from a passing one

**Stated once here, as a standing PRINCIPLE, and cited thereafter** (operator
ruling 2026-09-03; the launcher's copy is
`EterniaLauncher/docs/mission_control/07-observability-and-qa.md`). It had been
the load-bearing argument in at least three plans without ever being written
down as a rule, which is the same shape it describes: a thing everybody relies
on and nobody checks.

A green report is the conjunction of two facts — the gate ran, and it passed —
and a reader who sees only the verdict cannot tell which one they got. So a
check that did not execute carries the same signal as one that executed and
found nothing, and it carries it *silently*, which is why the failure mode is
always measured in days rather than caught in minutes. The evidence in this
repo is not hypothetical: `main` sat red and unreported from `6979bad59` on
`test_every_json_verb_states_its_root_or_is_classified`, a whole-program gate,
because CI on this fork is largely inert and no local lane ran it. The verb was
fixed in an afternoon; the missing lane was the actual defect
([AGENTS.md](../../AGENTS.md) § Testing tells the same story from the hook's
side, and the launcher's `CLAUDE.md` records three further recurrences of the
class in that repo, one of them inside `test/` itself).

Three consequences this canon actually acts on:

1. **A check whose input is the whole tree must not depend on a trigger set.**
   A path filter turns a whole-program gate into a gate over the paths someone
   remembered, and the paths nobody remembered are exactly where it was needed.
2. **"The gate is green" is only evidence when the run is named.** A verdict in
   a report, a commit message or a hand-back cites the command and its exit, or
   it is a claim about a run that may not have happened.
3. **Removing a lane is a decision about coverage, not about speed.** Both
   repos' pre-push hooks were deleted on 2026-09-03 by ruling and the checks
   stayed as tests — which is a deliberate move of WHEN they run, recorded as
   such, not an unnoticed drift into never.

The rest of this document is about receipts that are RUN. This principle is
about the ones that are not.

## The honesty contract

This is the canonical statement. Every timing surface in either repo inherits
it; the launcher's `mission_boot_timeline.dart` is where it was first written
down, and `agent_runtime/mission_chat_phases.py:18-50` is the hermes copy that
the create receipt (`agent_create_phases.py:23-24`) then inherited verbatim.

1. **Absent is never zero.** A phase that did not happen has no key — not `0`,
   not `null`, not present-and-empty. `safe_turn_phases`
   (`agent_runtime/mission_chat_phases.py:463`) drops keys it
   cannot read rather than defaulting them; `_format_ttfb_token`
   (`agent/conversation_loop.py:382-394`) emits no `ttfb=` token rather than
   `ttfb=0.0s`, "which reads as an instantaneous provider and is a lie no
   downstream reader can detect"; `_log_agents_readiness_split`
   (`snapshot.py:437-454`) prints nothing when the section never ran, and two
   honest zeros when it ran and cost nothing. **Absent-as-zero is the canonical
   lie of this codebase** — it is how a census once MEASURED A FALSE ZERO
   (`core_cache.py:169-172`).
2. **Monotonic only.** `time.monotonic` / `time.perf_counter` by construction,
   never a wall-clock delta: `BootTimeline` (`boot_timeline.py:16-17`),
   `TurnPhaseMarks` (`mission_chat_phases.py:29-32`), `_first_delta_recorder`
   (`conversation_loop.py:402-413`). A clamped `0` beats a nonsense `-3` where a
   span must still be emitted (`boot_timeline.py:181-184`) — clamping a measured
   span is not the same act as inventing an unmeasured one.
3. **First mark wins.** `provider_first_byte` is marked from a callback that
   fires once per token; a second mark must not move the first, and the guard
   lives in the marks object, not at the call site, "because 'the call site
   remembered to guard' is not a property anything asserts" (`:33-36`; items
   4-8 below cite the same file).
4. **Release-visible.** Durable records and ordinary INFO log lines, not debug
   aids behind a flag: the phases block rides persists the turn already performs
   (`:37-39`), the snapshot receipts ride the ordinary `Logger` family so
   `hermes serve` lands them in `agent.log` with no extra flag
   (`snapshot.py:397-398`, `stream.py:126-127`), and the launcher's lines reach
   the diag tee in release too.
5. **Never subtract wall stamps across processes.** `anchored_at` is the single
   wall stamp on a turn record, and exists only for eyeballing the turn against
   `agent.log`. "Subtracting it from anything is a bug" (`:13-16`).
6. **Join by id, never by time.** The launcher diag log stamped dateless local
   time before BO-3 and UTC after; turn records were always UTC, and two lanes
   misread across that boundary in one day. The keys are `turn_id`
   (`agent-chat-send-<uuid4>`, echoed byte-equal by hermes as both
   `client_message_id` and `turn_id`), `pid` for the boot families (the BO-3
   join key, argued in `stream.py`'s boot-receipt docstring), and
   `correlation_id` for the gesture chain (`OfficeStore._emit`, which normalizes
   the token and threads it onto the event and the `state.patched` row). See
   `mission_chat_latency_audit.dart:23-40` in the launcher. (Symbols, not lines:
   the `office_store.py` LINE RANGE this row once carried had drifted onto the
   position-policy alias and the `ActorScan` docstring — a range about actor
   completeness, with no `correlation_id` in it. It is spelled without its
   numbers here on purpose: a cite naming the drift would drift with it.)
7. **A span boundary is a fact about bytes, not intent.** The "provider
   first_byte" span opened before `run_conversation` had begun and so wore the
   provider's name over hermes assembly; `request_assembled` (`:84`, marked
   at `:447`, emitted as a run-progress marker at
   `conversation_loop.py:347-379`) splits it: `provider_request_started →
   request_assembled` is hermes, `request_assembled → provider_first_byte` is
   the client plus the wire.
8. **One authority per span.** A second measurement is a second authority, and
   the two will drift. `build_receipt_facts` (`snapshot.py:324-336`) READS
   `build_ms` off the envelope the build stamped rather than re-timing it;
   `snapshot_build`'s deprecated `elapsed_ms=` carries the identical value as
   `waited_ms=` (`stream.py:142-145`); `agent_create_phases` repeats
   `instance_ms` from the RPC result (`agent_create_phases.py:83-87`).

**Rule 1 has a LIST form**: a list that was shortened is as much a silent zero
as a phase that defaulted, so a row carrying a list must also carry every way
that list can be short. 2026-08-31 closed three instances of it in the office
family (H1, `2638504f9b`) — `actors_truncated` / `actors_unreadable` as REQUIRED
keyword arguments of `office_summary_row`, `conflict_guessed_keys` beside
`conflict_actor_keys`, and `office_actors_unreadable` out of
`copy_workspace_content`. **The three, and the parity warnings that carry them,
are canon in [06 — Office and board](06-office-and-board.md) § "The parity
warnings the office raises"** and not here: they are facts about the office
subject, and a reader asking "what warns, and why" should not have to read two
files that are free to drift. What belongs here is the rule they are instances
of, which is the paragraph above.

**Rule 1 read from the other side, 2026-08-31 (the instance-replication lane).**
`result["persona_instance_sync"]` is emitted **unconditionally** on every realm
pull — including against a peer that publishes no projection, where it carries
`source: null` — because here an absent KEY and a present-but-null answer mean
two different things ("this ack came from an older hermes" vs "this peer runs
one"), and an omitted key can only say the second by accident. Where rule 1
forbids inventing a zero for a phase that did not run, this forbids the mirror:
dropping the key for a lane that DID run and found nothing. Its sibling receipt
is a domain event rather than a log line, and the honesty is the same kind: a
replication mint emits `persona_instance.replicated`
(`decision_contract_registry.py:170`) and never `persona_instance.created`,
because that type means "this machine authored an agent" to every consumer that
reads it, and one pull posing as N local creates is a lie no grep over the log
can detect.

**Rule 1 over the EVENT lane, 2026-09-02 (the sync-honesty lane).** An
event-less write is the same silent zero a defaulted phase is: it does not read
as "nothing happened", it reads as nothing at all, and every watermark-gated
consumer downstream then serves a world it holds no receipt for. Two instances
closed together, both realm-sync ADOPT arms, and both found by the same
asymmetry — the sibling arm that DELETES already emitted, so the missing half was
never the store's rule but one lane's accidental exemption from it.
`board_sync.apply_board_pull` (and `realm_revert._adopt_from_upstream` behind it)
wrote board defs and cards past `BoardStore` with `atomic_json_write`; they now
go through `BoardStore.adopt_remote_board` / `adopt_remote_card`.
`OfficeStore.resolve_conflict`'s `take="remote"` adopt arm wrote the peer's actor
past `_emit_actor_patch`, which its edit-vs-remove sibling never did; it now
emits its `upsert`. Both mechanisms are 06's territory. What belongs here is the
rule they instance, and the one receipt that deliberately did NOT move with them:
`office.actor.conflict_resolved` stays uncovered on the patch lane, now for the
honest reason (the conflict list a patch cannot carry) rather than the
convenient one (06 § the fold model).

**Rule 1 read a third way: a refusal is not an answer.** `realm_sync_status`
authorized the whole verb before reading anything, so a denied credential deleted
the local half of a DIAGNOSTIC — drift counts, held packages, held profile
artifacts, workspace rows — and returned one typed error where an operator needed
four facts. It now degrades: the authorization gates the remote half, and the
denial rides the existing `remote_checked` / `remote_check_error` pair rather
than a new key (01 § realms and workspaces). Same shape rule 1 keeps asking for
— name the half you could not answer, out loud, instead of collapsing the whole
answer down to the missing part.

## Wire safety: receipts ride the LOG, never the envelope

**Observability never adds a key to the parity envelope.** Parity rides the
hydrate frame, and the hydrate frame is byte-pinned by the committed stream
goldens AND by the launcher's mirror of them — so two extra keys there is a
cross-stack fixture landing. Not theory: the `agents_readiness` split was first
written as two `sections_ms` keys and had to be pulled back out (`0e4567f5fd`,
2026-08-21), leaving hermes green and the launcher's producer-contract
byte-compare red on every push in between. Argument at `snapshot.py:824-837`;
`agent_create_phases.py:14-21` cites it as why the create's spans are a log line
instead. The exception that proves the rule: `phases` on the create's RPC result
and on the turn record are client-visible, but both are additive keys on
surfaces that are NOT byte-pinned. Anything on the hydrate frame is — which is
what the fixture mirror below enforces.

## The receipt census

| receipt (grep this) | emitter | consumer |
|---|---|---|
| `snapshot_build_core role=… caller=… generation=… build_ms=… offset=… sections_top=… pid=…` | `agent_runtime/snapshot.py:398-408` (fn `:369`, call site `:683`) | operator grep (`role=led` is the build count); `tests/agent_runtime/test_snapshot_build_logging.py:758` pins the prefix |
| `snapshot_build reason=… waited_ms=… elapsed_ms=… build_ms=… role=… caller=… generation=… offset=… events=…` (+`sections_top=`, +`core_source=`, then `pid=` last) | `agent_runtime/stream.py:264-267` (fn `_log_snapshot_build` `:196`) | operator grep; a launcher in the field still parses `elapsed_ms` (`stream.py:229-230`); `tests/agent_runtime/test_stream_build_timing_log.py` |
| `snapshot_agents_readiness walk_ms=… tool_visibility_ms=… pid=…` | const `snapshot.py:432-434`, emitted `:449-454` | joins `snapshot_build_core` on `pid`; pinned by regex at `tests/agent_runtime/test_agents_readiness_attribution.py:51` |
| `stream_attach op=… purpose=… … pid=…` | `agent_runtime/stream.py:212-218` | boot-investigation join (third `pid=`-bearing family) |
| `snapshot_core_cache …` / `snapshot_core_cache_write …` / `snapshot_core_shadow …` / `snapshot_core_cache_lane_closed …` | `agent_runtime/core_cache.py` — see the channel table below | `agent_runtime/core_cache_census.py` via `scripts/core_cache_demote_census.py` |
| `persona_prewarm done persona=… elapsed_ms=…` | const `PREWARM_DONE_RECEIPT` (`persona_prewarm.py:139`), emitted by `_worker` | pacing census; pinned at `tests/agent_runtime/test_persona_prewarm.py:481` |
| `persona_chat_actor_prewarm root=… outcome=… elapsed_ms=…` | const `persona_chat_actor_prewarm.py` (`CHAT_ACTOR_PREWARM_DONE_RECEIPT`), emitted in `_drain` | did the chat's actor get built before its first message; format pinned at `tests/agent_runtime/test_persona_chat_actor_prewarm.py` |
| `persona_chat_actor_prewarm pass candidates=… queued=… skipped=… elapsed_ms=…` | const `persona_chat_actor_prewarm.py` (`CHAT_ACTOR_PREWARM_PASS_RECEIPT`), emitted in `prewarm_chat_actors_on_boot` | one line per boot pass; the `candidates`/`queued` gap is `max_hot_sessions` doing its job |
| `resident_signature_diff root=… components=…` | const `persona_chat_continuity.py` (`RESIDENT_SIGNATURE_DIFF_RECEIPT`), emitted in `PersonaChatRuntimeRegistry.acquire` | why a resident actor was NOT reused: the signature component NAMES that moved (never digests, never values — the components include prompt- and policy-adjacent material). Twin of the turn record's `resident_rebuild_component_<name>` flags; format pinned at `tests/agent_runtime/test_persona_chat_continuity.py` |
| `agent_create_phases persona=… instance_ms=… phases=… pid=…` | const `agent_create_phases.py:88-90`, emitted `:232-237` | drop-latency attribution; pinned at `tests/agent_runtime/test_agent_create_subphases.py:152` |
| `harness serve boot timeline: <k=v …>` | `hermes_cli/harness_parts/serve.py:1857-1859`, line built by `BootTimeline.log_line` (`boot_timeline.py:173-178`) | operator grep; the same block also rides the `ready` frame (`serve.py:1751`) |
| `API call #N: model=… provider=… in=… out=… total=… latency=…s[ cache=…][ ttfb=…s]` | `agent/conversation_loop.py:3473-3479` | provider-vs-hermes attribution; `tests/run_agent/test_api_call_ttfb.py` |
| turn-record `phases` block (schema v3) | `agent_runtime/mission_chat_phases.py`; the key lands via `_safe_journal_metadata` (`mission_chat_turns.py:428`, `:487`) → `safe_turn_phases` (`:1230`) | `tool/mission_chat_latency_audit.dart` |
| `[MissionChatTiming]` / `[MissionChatOutcome]` / `[MissionDropTiming]` | launcher — see the launcher section below | `tool/mission_chat_latency_audit.dart`; drop line read by eye |
| `[MissionAgentCreate] lane=… gesture=… correlation=… …` and `[MissionOfficeWrite] <ws> retire lane: …` | launcher — see the launcher section below | the placement verb's two lanes, read by eye; the ADOPT line is also read by `mission_office_placement_instance_key_test.dart` |
| `prompt_observability` rows + `trace_events` | `agent_runtime/prompt_observability.py:108`, persisted `:1198-1233` | `harness prompt-context show --context-id` (`hermes_cli/harness.py:835-841`) and the slimmed `chat.final` echo |

### The snapshot build family

`snapshot_build_core` is ONE line per ACTUAL build, emitted by the caller that
ran it, on the thread that paid for it, before its waiters are notified
(`snapshot.py:682-689`). Every other line about a build is a WAIT
(`stream.py`'s `snapshot_build`). Until the two were separated, the 2026-08-17
boot's "three concurrent builds" were one build plus two riders logging their
waits, and the most expensive build of that boot — the serve prewarm — logged
nothing at all (`snapshot.py:374-381`). A build that raised logs nothing: "the
exception is the receipt" (`:685-686`). An injected-store (fixture) build emits
no receipt (`:570-573`). `sections_top` rides every `snapshot_build_core`, and a
WAIT line only when the build under it crossed `BUILD_SECTIONS_WAIT_THRESHOLD_MS`
(`stream.py:153-158`). `pid=` goes LAST on
all three join families so no adjacency moves — an additive field, never a
formatter change, because `%(process)d` would re-shape every line the runtime
emits and break every grep anchored on a neighbour (`stream.py:118-124`).

### The core-cache family and its census

`agent_runtime/core_cache.py:152-195` is **the authority** — a per-receipt
channel table naming, for every line, its family token, whether a second channel
(the `parity` envelope) carries the same fact, and the census rules a counter
must honour. Not duplicated here; amend it there.

The emitters: `snapshot_core_cache core_source=cache …` (`:3155`), the demote
`core_source=rebuilt caller=… reason=… inputs=…` (`_log_demote`, `:3088`), the
stale variant (`:3211`), `snapshot_core_cache_write ok=true` (`:1872`) and its
four `ok=false` reasons (`:1786`, `:1790`, `:1794`, `:1864`),
`fingerprint_refused` (`:1114`), `generation_residue` (`:2000`),
`never_converged` (`:2303`), `..._lane_closed` (`:2996`), and — HC-1, 2026-08-22
— `fingerprint_home_lazy_capture` (`_receipt_fingerprint_home_lazy_capture`,
located by symbol because the line numbers in this paragraph predate the IC/HC
edits). That last one is the ONE receipt here with no census reading it: it fires
when a process that DECLARED a boot instant for its fingerprint-home capture
(`serve_loop:booting_frame_emitted`, or the CLI's harness dispatch) reached its
first fingerprint without having taken it, which is `reason=home_mismatch` seen
from the producing side, one boot earlier.

Three facts a reader must carry. **A cache hit deliberately does NOT emit
`snapshot_build_core`** (`:3112-3119`) — there was no build, and a receipt
claiming one would put the log back where a wait and a build are
indistinguishable. **`reason=absent` is deliberately never logged** (the
ordinary cold start would print a line on every build in every process), so its
only trace is the ABSENCE of a line and "no demote line" must never be read as
"no demote". **Two reason spellings collide across two events** —
`fingerprint_unavailable` and `build_stamp_unknown` are emitted by the write
lane and the read lane alike, so grep the family token with the reason, never
the reason alone. The census that executes those rules is code, not prose:
`agent_runtime/core_cache_census.py` (family token at `:97`), driven by
`scripts/core_cache_demote_census.py` — exit `0` reported, `1` **nothing parsed
or log unreadable (a failure, not a pass)**, `2` a demote named a path the
runtime itself writes, the self-perturbation finding it exists for (`:10-25`).

### Create, prewarm, boot

`agent_create_phases` bills ten named spans of one mint — `bindable_ms`,
`chat_root_ms`, `instance_write_ms`, `create_patch_ms`, `wire_row_ms`,
`permission_options_ms`, `chat_lane_scope_ms`, `tool_visibility_ms`,
`event_append_ms`, `spawned_by_write_ms` (`agent_create_phases.py:103-127`) —
rendered `name:ms,name:ms` in that order, or the token `-` when nothing was
recorded (`NO_PHASES`, `:96`), so a parser never has to tell "no such key" from
"nothing ran". Spans ride a `ContextVar`, so a concurrent snapshot build on
another thread cannot land in a create's receipt (`:60-62`).

**`phases.skills_ms` is an ACK field, not a log line, and that is deliberate.**
The create's third phase (05 §9) bills itself into `result["phases"]["skills_ms"]`
and then RE-STAMPS `total_ms`, because `total_ms` was measured before the phase
existed and would otherwise under-report every create that installs a cold skill
by exactly the cost the phase was built to make visible. Both keys are always
present on both doors. Nothing new joined the parity envelope for it — the wire
rule below holds: a new observability number rides the RPC result a launcher
parser already reads, or it rides a log receipt, never a new key on the envelope.
The `agent_create_phases` LOG line still bills only `instance_ms` and its ten
sub-spans, so a reader wanting the skills cost reads the ack, not the log.

`persona_prewarm done` is the completion half of an otherwise unfalsifiable
claim — "a start with no finish measures nothing" (`persona_prewarm.py:130-135`).
The clock starts AFTER the queue `get`, so an idle worker never reports a
minute-long warm (`:255-258`); a warm that RAISED logs a WARNING carrying the
same elapsed cost, because a census blind to the failures would under-count the
queue's real service time (`:262-274`).

`persona_chat_actor_prewarm` (2026-08-23) is the same discipline for the lane that constructs a
real agent, at a finer grain because its claim is finer: the PASS line is emitted when the boot
pass has QUEUED, and the worker owns one DONE line per item, because a pass line with only totals
cannot answer "which chat took four seconds" and the module's whole claim is a race against the
operator's first message. `outcome=` is a closed set — `warmed`, `already_resident`,
`registry_off`, `skipped_turn_active`, `skipped_no_chat_root`, `skipped_persona_unresolved`,
`skipped_profile_unready`, `skipped_construct_failed` — so a reader never has to tell "it did not
run" from "it ran and found nothing to do". `already_resident` is a SUCCESS: a real turn (or an
earlier warm) got there first and `acquire` handed the entry back. Ids and timings only, on the
same rule as `persona_prewarm`: never a display name, never a resolved toolset.

`harness serve boot timeline:` renders
`BootTimeline.stamps()` as `key=value` pairs: `interpreter_ms` (psutil-derived,
ABSENT when the platform will not report a creation time), the segments that
decompose it — `interpreter_boot_ms`, `main_import_ms`, `dispatch_ms`,
`bytecode_sweep_ms`, `harness_parser_ms` (`hermes_cli/_boot_clock.py:112-152`)
— then the phases in completion order
(`chat_registry_ms`, `head_publish_ms`, `root_anchor_ms`, `store_root_ms`,
`service_foundations_ms`, `orphaned_turn_sweep_ms`, `dispatch_restore_ms`), then
`elapsed_ms` and `total_ms`. A phase recorded twice ACCUMULATES
(`boot_timeline.py:140-151`) and is stamped as it COMPLETES, so a boot that
wedges before `ready` still carries everything it finished.

### The turn record's `phases` block (schema v3)

`TURN_RECORD_SCHEMA_VERSION = 3` (`mission_chat_phases.py:70`) is the version
that carries `phases`. A v2 record is NOT migrated — bumping is how a reader
tells "this turn predates phase spans" from "this turn was instrumented and
never reached that phase", two facts a silent version would collapse. Marks, in
the order a healthy turn passes them (`:76-89`): `request_received` (0 by
definition — the anchor, and the only always-present key), `context_built`,
`observability_built`, `emitter_created`, `write_ahead`, `agent_ready`,
`provider_request_started`, `request_assembled`, `provider_first_byte`,
`stream_done`, `native_committed`, `projected`. Plus the flag `agent_init_cold`
and the counters `registry_probe_rounds` / `builds_overlapped` /
`visibility_bundle_builds` (`:104-108`), all four absent when the fact could not
be established honestly — baselined at
`hermes_cli/harness_parts/persona_commands.py:2064`, `:2069`, delta-counted at
`:3267-3268`, flag and overlap set at `:3461`, `:3467`, overlap counted by
`agent_runtime/snapshot_build_ledger.py`.

Persisted at `<store>/mission_chat_turns/<safe_session_key>.json`
(`mission_chat_turns.py:28-43`) with `sort_keys=True` (`:1073`), so the on-disk order is
alphabetical and **nothing may depend on ordering** — the join contract is the
key names and their meaning (`mission_chat_phases.py:118-134`). A "phase" more
than 24 h after the anchor is rejected on READ as corrupt; the writer cannot
produce one (`:128-132`). The emitter's own `ttft_ms` keeps its meaning: it is
constructed only after replay checks, native history load, turn-context build
and the prompt-observability row build, so its clock cannot see the profile
bootstrap before it. `phases` is a SUPERSET, not a replacement (`:41-46`).

### The launcher's five lines

All of them ride `Logger` into `<temp>/eternia_launcher_diag.log` — the EMITTERS
run unconditionally, but since MCF-83 §2 (launcher `1a012e13d`, 2026-08-20) the
disk tee installs only in debug or opt-in support builds
(`kDebugMode || ETERNIA_VOICE_DIAGNOSTICS`); a release binary emits to a logger
nothing tees. Lines are stamped `[<UTC ISO-8601>] <level> <logger> — <message>`
(`lib/core/telemetry/diag_log_file.dart:278-283`). Bullet paths below are
relative to `lib/features/mission_control/`.

* `[MissionChatTiming] turn_id=<id> send_to_admit_ms=… admit_to_first_delta_ms=…
  first_delta_to_end_ms=… end_to_settle_ms=…` —
  `data/mission_chat_turn_timeline.dart:266-273`, marker const `:296`. One line
  per turn, at settle. Absent fields omitted entirely; no `unresolved=` clause,
  because the field set is CLOSED unlike the open-ended boot receipt (`:255-265`).
* `[MissionChatOutcome] turn_id=… status=… [error_kind=…] message="…"` —
  `agent_chat/mission_agent_chat_runtime_controller.dart:1566-1571`. Landed
  because the 2026-08-22 02:00:47Z new-chat rejection settled leaving NO
  envelope anywhere on disk: the timing line printed bare (correct — no phases
  happened) and the refusal lived only in widget state. Harness error prose only,
  truncated at 220 chars, never operator text.
* `[MissionDropTiming] correlation=… persona=… slug=… layout_mutate_ms=…
  rpc_ms=… rpc_instance_ms=… roster_confirmed_ms=… sprite_ms=…
  sprite_source=cold|warm|absent sprite_lane=serve|cli first_paint_ms=…` —
  `data/mission_drop_timeline.dart:351-368`, marker const `:376`. Closed and
  ordered; `roster_confirmed_ms` went in additively, disturbing no `key=value`
  reader.
* `[MissionAgentCreate] …` — the placement verb's launcher lane, all arms
  through `office/mission_office_drop_log.dart::missionOfficeDropReceipt`,
  emitted from `mission_control_page.dart::_createPlacedAgentOverRpc` and
  `::_adoptServerPlacement`. Five arms, and the set is the point: `lane=twoCall`
  (with `reason=`, so a rollout degrade is SAID rather than silently taken),
  `lane=rpc` on success (carrying `aim=aimed|unaimed`, `pos=sent|server` — the
  D12 decision as it was actually taken for THIS call — `replay=` and the
  server's `phases=`), `lane=rpc … REFUSED` (with `reason`, `phase`,
  `rolled_back`, and `placed=`/`orphan=` split apart, because a skills-phase
  instance is standing and an uncompensated one is not), `ADOPT` (which prints
  the adopted CONTENT KEY IN FULL — the defect it retires was a client hashing
  its own payload and comparing it with itself, which is indistinguishable from
  the store agreeing unless the key is on the line), and `REFETCH` (the replay
  arm that adopts nothing). `correlation=` is the GESTURE's token and is NOT
  `[MissionDropTiming]`'s `drop-N`, which is a per-process instrument key two
  launcher processes would mint identically — the two id spaces share a clause
  name and nothing joins them, which is a filed row, not a fact this doc hides.
* `[MissionOfficeWrite] <workspace> retire lane: rpc|cli instance=… archived=N
  verdict=clean|failed|unknown` —
  `office/mission_office_layout_controller.dart::_adoptRetireAck`, label const
  `kMissionAgentRetireReceiptLabel`. Its own line rather than a column on the
  flush's `write lane: N rpc, N cli`, because a retire is a per-GESTURE call
  outside any flush and folding it in would corrupt that receipt's arithmetic —
  with the consequence, recorded rather than implied, that a session whose every
  delete spawned the CLI still reports `0 cli` on the write-lane pill. The
  refusal and fallback arms print `retire REFUSED … lane=… reason=… code=…`.

### prompt_observability rows and trace_events

Per-turn prompt provenance, not timing: what the model was actually shown. Built
by `mission_chat_prompt_observability` (`prompt_observability.py:108`), turn
results attached at `:551`, persisted through **one** chokepoint —
`persist_prompt_observability_context` (`:1198-1233`) — which ref-transforms the
skills catalogs, writes compactly, updates the latest-pointer index and applies
retention. Layout: `<store>/prompt_observability/<context_id>.json`,
`prompt_observability_catalogs/<hash>.json`, `prompt_observability_archive/`,
`prompt_observability_index.json` (`agent_runtime/paths.py:306-328`). Retention
keeps the newest 2 rows per `(persona_instance_id, session_id)` lane and ARCHIVES
the rest, never deletes (`:1185-1187`); an absent catalog is honest absence,
never a fake empty list (`:1217-1220`). Two consumers: the live `chat.final`
echo carries a slimmed projection (`slim_chat_final_observability`,
`persona_commands.py:4557`); evicted rows are
fetched by `harness prompt-context show --context-id <id> [--json]`
(`hermes_cli/harness.py:831-841`, handler `:2967-2999`) — read-only, honest
`not_found` on absence. `trace_events` are the turn's tool-call trace, passed at
`persona_commands.py:3416` and read by `used_skills_context`
(`prompt_observability.py:2675-2700`) to report which skills were actually
loaded — `skill_view` entries only, redaction-safe.

## The consumers

A timeline with no consumer is a file, not observability: the boot receipts were
honest and release-visible for days while nothing read them, which is how the
2026-08-21 convergence defect survived (`mission_boot_receipt_audit.dart:1-16`).

* **`tool/mission_chat_latency_audit.dart`** (launcher repo; reads only, safe
  while the launcher is open). Joins `[MissionChatTiming]` to the turn record's
  `phases` **by `turn_id`**. Exit codes (`:53-62`, computed at `:384-389`): `0`
  every parsed turn joined a record carrying phase data; `1` log or turn dir
  missing/unreadable; `2` a timing line matched NO record — a broken join key;
  `3` **zero-scan, no timing line parsed**; `4` lines parsed but none joined a
  record carrying `phases`; `64` bad arguments. The unmeasured span — launcher
  dispatch leaving the process to hermes `request_received` — is a BOUNDED
  residual from two same-side durations, never printed as a measurement
  (`:37-40`).
* **`tool/mission_boot_receipt_audit.dart`**. One assertion: a boot whose
  receipt shows `spawn_to_ready` resolved while `authoritative` is still listed
  under `unresolved=` is a defect. Exit `0`/`1`/`2`/`3`/`64` (`:34-42`), where
  `3` covers TWO guards — zero `[MissionBoot]` receipts parsed, or the audited
  phase key matched nothing anywhere in the file. One constant serves all four
  positions that token can occupy, so a misspelling produces exit 3, not a quiet
  green (`:64-70`).
* **`tool/test_quality/check_producer_contracts.py`** (launcher repo, invoked
  with `--hermes-root`). Compares manifest membership AND ORDER before bytes —
  the logic is `compare_family` (`:54-75`), over the two fixture families the
  `FAMILIES` tuple declares (`:23-35`). Default mode RUNS this repo's
  generators first; `--no-generate` compares committed bytes read-only
  (`:101-105`). CI runs it in default mode, in job `hermes-cli-contract`
  (`.github/workflows/ci.yml:42`), at the step `:82-85`.
* **`scripts/core_cache_demote_census.py`** — see the core-cache section.

## The doctor's report roster

`harness doctor` (`agent_runtime/harness_doctor.py::run_harness_doctor`) is the
triage surface, and the honesty contract binds it harder than anything else
here: a false all-clear does not merely misreport, it TERMINATES the
investigation that would have found the real defect. Both headline flags are
therefore derived from `summary.section_health`, never asserted —
`summary.needs_fix` is "some section observed a defect", `ok` is "every section
was examined AND none observed one".

Four health values, and the fourth is the load-bearing one. `ok` / `notice`
(examined, informational — a stale model pin never turns the doctor into a fix
job) / `defect` (examined, actionable) / **`unknown`** (NOT examined — the
probe raised). `unknown` clears `ok` without claiming a defect nobody saw, and
its counterpart in `summary.finding_counts` is `None`, never `0`: a zero there
is what sends an investigator hunting a class the doctor never looked at.

**The sections are DECLARED, once.** `DOCTOR_SECTIONS` (bottom of
`harness_doctor.py`) is one row per section — its name, its probe, where its
report lands in the payload, and which `summary.finding_counts` entries it
contributes — and `summary.section_health`, `summary.finding_counts`, the
payload placement and the CLI printer's per-section detail line are all derived
from it. Until 2026-08-30 those were four hand-maintained lists of one set with
only `section_health`'s key set pinned, so a section added to three of the four
was counted and verdicted while rendering no operator line at all. Adding a
section is now one table row; a missing roster entry is unspellable.

The sections, each contributing one health value (`schema_version: 8`):

| Section | What it observes | Verdict weight |
|---|---|---|
| `orphan_worktrees` | the git sweep across registered worktrees | defect on a reap |
| `snapshot_null_id_rows` | id-less rows in the built frame; the BUILD outcome rides its own `snapshot_build` key so a crash is never counted as rows | defect / unknown |
| `event_log` | live slice + rotation manifest (`event_log_health`) | defect off `index_health` |
| `model_authority` | shadowing/redundant pins (`describe_runtime_default_authority`) | notice only |
| `persona_binding` | config-vs-store divergence (`binding_index`) | defect, with the remediation command |
| `root_config_misplacement` | root-only keys set in a PROFILE, where nothing reads them | defect when inert, notice when duplicated |
| `placement_census` | the roster/office join + the desk-litter item sweep + the duplicate-placement sweep — see below | defect on an orphan actor or a `same_instance` duplicate, notice on an unplaced row, a litter desk or any other duplicate |

**`placement_census`** (placement plan D8) is the join nothing watched:
`persona instance reconcile` prunes orphan ROWS without ever opening the office,
and no doctor section read the two stores together, so a half-state — a retired
instance whose actor survived, a placement whose compensation archived the row
and not the desk — was representable, durable and invisible. It reports, per
workspace, `placed` / `unplaced_rows` / `orphan_actors` with ids; the three
terms are defined once in
[01 — System architecture](01-system-architecture.md#the-entity-chain).
Two properties are the section's contract:

- **Read-only.** No repair, because both repairs are deliberate operator
  gestures and the doctor sees one snapshot.
- **`unknown`, never `ok`, on a short world.** Either store raising, and also
  either scan returning rows beside a nonzero `unreadable` count
  (`PersonaInstanceScan` / `ActorScan` carry that count precisely so a reader
  cannot mistake a short list for a complete one), forces `unknown`. A census
  computed over a short world reports an actor as orphaned because its roster
  row is the file that would not decode — inventing the finding out of the
  outage.

Both sides of the join are compared through `canonical_persona_instance_id`, the
single derivation authority, so id drift the reconcile verb exists to fold does
not surface here as a fabricated orphan. **The actor side only joined that rule
on 2026-08-30** (H-H11): it was read raw off the file, and `upsert_actor` is not
the only writer — the realm pull's `adopt_remote_actor` writes a peer's row
verbatim, legacy spelling and all — so a pulled actor reported as an
`orphan_actor` against the roster row it names correctly.

Each `orphan_actors` row also carries a **`reason`** (H-H4, 2026-08-30) — one of
`retire_incomplete` / `instance_retired` / `instance_unknown`, decided by
`_orphan_actor_reason` from two facts the store recorded about itself: a
retirement tombstone, and a retire receipt (06 § the retire's receipt) whose
recorded `office_archive_failures` names THIS actor key. It is a discrimination,
not a new count and not a health change: the bucket, the `finding_counts` entry
and the `defect` verdict are unchanged. It exists because the three have
different repairs and the remediation string could previously only describe the
split in prose — and because `retire_incomplete` is the standing detector for
the "row archived, desk still live" half-state that the retire ack alone used to
witness. The reason never reads an id's spelling; an absent or unreadable
receipt degrades to `instance_retired`, which is the safe direction.

**`desk_litter`** (desk-litter plan DL-H1) is the same section's ITEM-level
sweep, added because the join above is structurally blind to it: that join is
actor-level and instance-keyed, and a desk minted by `materializeAgentDesk`
carries the persona CLASS id with no binding, so it lands in the class-keyed
actor the join skips while its agent lands in the instance-keyed one. The sweep
walks live `kind: "desk"` items of the same fully-read world and files each into
exactly one of four reasons — `agent_missing` (widowed: no live agent item for
the persona in the workspace), `agent_scope_stale` (every live agent item for
the persona is bound to an instance no live roster row backs — the store-side
shadow of the launcher's projection scope drop, deliberately overlapping
`orphan_actors`), `persona_retired` (no live row and no retirement tombstone
names the persona at all) and `desk_kind_agent_binding` (the item is
structurally an agent's: it rides an actor with a LIVE instance binding, or the
store recorded that it was MINTED as an agent). The last is kept out of
`agent_missing` by design — it wants a re-place, the other three want a reap,
and conflating them is what the 2026-08-30 incident cost a day to. Litter raises
the census to `notice` and never past it: nothing mis-renders, so `needs_fix`
stays off.

**The minted-kind clause asks the store, not the id (H-H12, 2026-08-30).** It
used to parse the `item_id` for the launcher's three minting conventions
(`<persona>_<kind>`, `desk-<agentItemId>`, the bare instance id) — none of which
anything enforces, so a launcher rename would have silently reclassified every
mis-kinded agent as a widowed desk. `OfficeItem.minted_kind` is now stamped by
`OfficeStore.upsert_actor` at an item's FIRST write, keyed on `item_id` and
sticky thereafter (a resurrection carries it forward from the archive, the same
precedence `base_revision` uses), and it is never read off the payload — a value
a client could send would be the self-declaration the field replaced a spelling
with. `None` — every item written before the field, and every one adopted from a
peer that has not upgraded — is CANNOT SAY: such a desk falls through to the
absence buckets and is judged on whether its agent exists, which is the same
softer answer an unreadable id used to get. The field is deliberately not on the
wire; no client decides anything with it — and it is excluded from
`office_content_hash` for the same reason (`office_models._ITEM_HASH_EXCLUDE`).
That exclusion was the ruling on this stage's one open migration question. As
first built, the field rode inside the hash, so the first status or publish
after the upgrade would have seen every actor's hash move once and reported it
as a local change: an unmeasured drift spike on the lane whose whole job is
detecting real drift, and — for as long as any peer ran a store that decoded the
field away — a disagreement between two installs holding identical content. The
hash filter drops the KEY rather than nulling it, which is what makes the fix
provable instead of plausible: the encoded payload is byte-identical to the one
the function produced before the field existed, so no hash on any existing store
moves at all. Nothing real hides there, because `kind` IS content and is still
hashed.

**`duplicate_placements`** (H-H8) is the same section's third sweep, and the
reader the two write fences' known residual needed: an instance-keyed write
claiming an item id another live actor holds passes both fences, and the census
could not see it either — the join is actor-level, so both holders counted as
`placed` and the section reported `ok`. It now opens `actor.items` and reports
one row per item id held by more than one live actor, naming every holder. The
three reasons split on D6's ruling that the INSTANCE, not the persona, is the
unit: `same_instance` (every holder bound to one instance) is a defect;
`cross_instance` is a notice, because two instances of one persona each
authoring a desk mint the same persona-scoped id and that is the instantiated
system working; `unbound_holder` (a class-keyed holder) is a notice, because it
is the class→instance re-key migration's own mint-then-archive transient. It is
a READ, not a third fence — 06's D6 forbids re-keying that fence toward
instances at all.

The text renderer prints every non-`ok` section with its own error text, then
names each orphan actor individually — that id is the remediation argument —
each litter desk with its reason, and each duplicate placement with all of its
holders, while counting unplaced rows, since a healthy runtime can legitimately
carry several.

## The BO-1 fixture mirror

Two families, each a producer directory in hermes mirrored byte-for-byte into a
consumer directory in the launcher: stream frames
(`tests/fixtures/stream_frames` ↔ `test/fixtures/harness_stream`) and response
envelopes (`tests/fixtures/response_envelopes` ↔
`test/fixtures/hermes_responses`). The launcher parses its copies through the
real decode + read-model pipeline (`mission_stream_contract_fixture_test.dart`).
`MANIFEST.sha256` pins fourteen stream files while the generator writes seven —
a structural split named in the script as `GENERATED_FRAME_FILES` /
`PINNED_ONLY_FILES` (`tests/fixtures/stream_frames/README.md:8-13`). BO-1
settled 2026-08-22: `hydrate_stale_first.json` and
`hydrate_authoritative_same_offset.json` mirrored into the launcher
(`37762bc0e`) at this manifest's exact row position, plus the pair's convergence
case in the launcher test (`README.md:15-24`); both manifests match. Adding a
key to `parity` means regenerating these files and landing them in both repos in
one wave — the wire-safety invariant in its concrete form.

## The zero-scan lesson (MCF-53 class)

A scanner that silently matches nothing reports a clean bill of health for a log
it never read. Every consumer above encodes the lesson: the chat audit splits it
in TWO on purpose — `3` "the launcher wrote nothing" versus `4` "the launcher
wrote and hermes did not" — because folding them would make "the other repo has
not landed yet" look identical to "the join is broken"
(`mission_chat_latency_audit.dart:64-80`); the boot audit guards the marker AND
the audited key separately, both watched failing before the tool is trusted
(`mission_boot_receipt_audit.dart:44-62`); the demote census exits `1` on an
empty scan, because `absent` is deliberately unlogged and a `0` there would be
indistinguishable from a healthy runtime — "exactly how a self-invalidating
cache went unnoticed for months" (`scripts/core_cache_demote_census.py:13-19`).

## The swallow audit, and what it changed

The 2026-08-17 lane-ambiguity scout (archived, full text preserved) ranked four
findings. **All four are fixed today — verified by reading the current code,
not by trusting the audit's own status.**

| finding | then | now |
|---|---|---|
| `serve_rpc.py` baseline `or 0` — an unreadable event log became watermark 0, killing the sink's baseline gate and re-opening the resync↔restart loop | `baseline_offset = int(...) or 0` | typed absence: `baseline_offset = event_offset_of(watermark)` then an explicit `is None` arm — `agent_runtime/serve_rpc.py:885-886` |
| empty `patches` shipped as a `patch` frame — the client advanced its watermark having folded nothing | coverable ⇒ promoted | promotion now also requires `batch_carries_patch_rows(batch)`; the honest answer for a pair-less batch is the full core — `agent_runtime/stream.py:808-819`, argued at `:554-581` |
| `office_surface` could never satisfy the office scope gate, so every folder-only patch frame was dropped with no patch and no resync | `entity == OFFICE_ACTOR_ENTITY` and a slash-prefixed id | one predicate: `office_patch_scope(patch) == workspace_id` — `agent_runtime/serve_office_subscriptions.py:486` |
| `_usage_lane_detected` — a credential fault DELETED the lane from the Limits panel, and an empty envelope rendered as a positive claim that no provider is signed in | `except Exception: return False` | three outcomes, not two: true / false / **raise**, with the raise caught per provider and the lane emitted `unavailable` naming the exception class — `hermes_cli/harness.py:5446-5464`, `:5684-5696` |

The highest-value read-side swallow also closed: the actor-directory read
skipped undecodable files and returned a shorter list that described itself as
complete, so `actors_truncated` computed 0 over it. It now returns a typed
`ActorScan(actors, unreadable)` so the two facts travel together
(`agent_runtime/office_store.py` — `ActorScan` at `:219`, `read_actor_dir` at
`:432`, `OfficeStore.scan_actors` at `:1224`). Since AX5 that scan is the ONLY
actor read: the `list_actors` thin view that returned `.actors` and dropped
`.unreadable` is deleted, so dropping the count is now something a call site
WROTE rather than a default it inherited. Since AX6 the reader is module-level
and the realm pull's read of a PEER's actor directory
(`office_sync._read_remote_office`) delegates to it, so the one place where the
two spellings disagreeing produces a DELETION rather than a wrong number cannot
drift.

## Invariants

1. **A receipt's format string is a contract.** Most are pinned by a test that
   greps the prefix or regex (`test_snapshot_build_logging.py:758`,
   `test_agents_readiness_attribution.py:51`, `test_persona_prewarm.py:481`,
   `test_agent_create_subphases.py:152`, `test_core_cache_channel_table.py`).
2. **`pid=` is last; variable-length tails are last.** `generations=`, `diff=`
   and `_lane_closed`'s free-form detail span go last because nothing can be
   field-parsed after them (paths may contain spaces).
3. **Every receipt leads with a family token**, then `key=value`. A census greps
   tokens, never the prose after them (`core_cache.py:156-160`).
4. **Observability must never be the reason something fails.** Instruments are
   defensive by construction (`snapshot.py:333-335`), the boot-timeline
   annotation is wrapped in a bare `except` (`serve.py:1057-1058`), and
   `log_create_subphases` never raises and never measures
   (`agent_create_phases.py:230`).
5. **Receipts carry timings and ids only.** Never a display name, never a
   resolved toolset, never operator message text (`persona_prewarm.py:137-138`,
   `agent_create_phases.py:64-66`,
   `mission_agent_chat_runtime_controller.dart:1562`).
6. **Observability rides log receipts, never new keys on the parity envelope.**
   See the wire-safety section.
7. **A scanner that matches nothing must exit nonzero.**

## Open rows

* **Nothing schedules the three live-log consumers** — the two `dart run` audits
  and the demote census run only when a human remembers.
  → `planned/observability-consumer-runner.md`
* **Residual split, named rather than fixed:** the cache line says `stale=true`
  while the payload says `parity.core_stale` / `parity.freshness.state` — a
  consumer contract predating the lane (`core_cache.py:184`). A census reads both.
* **`reason=absent` has no receipt by design**, so demote counts are lower
  bounds; nothing lifts that without a line per build, per process.
* **The new-chat first-send rejection is still open.** `[MissionChatOutcome]`
  landed to name the next occurrence; it has not yet caught one.
* **C3 is instrumented but unread**: whether a drop followed by a first message
  pays a cold MCP spawn is answerable from the `phases` counters today.

## Supersedes

`planned/agent-placement-verb.md` — **deleted 2026-08-27 by the S10 fold-in
commit** (`git log --diff-filter=D --oneline -- docs/agent-runtime-harness/planned/agent-placement-verb.md`
recovers it). Its observability half is above: `phases.skills_ms` on the ack,
the two launcher receipt families, and `placement_census` in the doctor roster.

All others under `archive/2026-08-22-pre-consolidation/`:

* `SCOUT_HERMES_SWALLOW_AUDIT_2026-08-17.md` — its four top findings and the
  `_read_actor_dir` swallow are fixed; retained as the pre-fix record.
* `CORRELATION_ID_PLAN_2026-08-16.md` — the gesture-token threading landed
  (EG-2.3 / §V2); `correlation_id` is a join key here, not a plan.
* `MISSION_BOOT_WINDOW_PLAN_2026-08-17.md`, `EG0_2_RECEIPTS_2026-08-17.md` — the
  receipts they specified are censused above at their live formats.
* `14-snapshot-core-build-performance.md` — build/cache receipts moved here;
  `core_cache.py:152-195` holds the channel table.
* `MC_DROPS_SNAPSHOT_CACHE_INVESTIGATION_2026-08-18.md` — origin of MCF-53 and
  MCF-54(ii); both are now encoded rules (`generation_residue` exists, the
  zero-scan exits exist).

## Unverified carry-forward

Field numbers relayed from live runs. The receipts that produced them are
verified above; the numbers cannot be re-derived from the tree. First two from
`Launcher_Brain/20 — Active Initiatives/chat-provider-timing-and-speed-2026-08-22.md`.

* **~13% of one turn's "provider first_byte" span was hermes work** — turn
  `c59ab99e`, 2026-08-22 02:00Z: 1,762 ms of 13,532 ms. This is the measurement
  `request_assembled` was added to attribute.
* **The felt gateway-vs-Mission-Control gap was mostly the MODEL** —
  `gpt-5.6-luna` on openai-codex 0.7–1.6 s probe TTFB; `big-pickle` on
  opencode-zen (FREE tier) 10.7–12.5 s total, `429 FreeUsageLimitError` under a
  probe burst. CLOSED 2026-08-23: mission chats ride luna via instance
  overrides. Live luna TTFB expectation (2.2–3.5 s, reasoning at
  effort=medium): see the canonical note on doc 08's luna row.
* **First-build cost, 5 runtime personas, 2026-08-22**: 4,001 ms (3,054 tool
  visibility / 947 readiness walk); later builds in the same process 183 ms
  (36 / 146). A code comment at `agent_runtime/snapshot.py:422-424` — verified
  as written there, not re-measured here.
