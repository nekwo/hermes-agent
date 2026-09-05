# 04 — Boot and lifecycle

What happens between `Process.start` and a runtime that can answer authoritatively, in the
order the code runs it. Every stage is named by the receipt it emits, because a cold boot is
the boot nobody is watching a console for — the instrument is the only witness. The serve
process is not the slow part: typically ~1.5-3 s, with a tail to 4.3 s (Stage 6). The expensive
half is the FIRST READ-MODEL CORE, which has its own cache, its own receipts and its own live
defects. Read
`## Open rows` before trusting any boot number you measure today.

**Stage 0, briefly, because it is the Launcher's:** the serve child is spawned with an explicit
env set of FOUR keys — `ETERNIA_HERMES_ROOT`, `HERMES_HOME`, `HERMES_HEAD_HOME`,
`HERMES_AGENT_RUNTIME_ROOT`, with `HERMES_HOME` and `HERMES_HEAD_HOME` both at the **base
profile home**, not the state root
(`EterniaLauncher .../data/mission_control_hermes_installer.dart`, `runtimeEnvironment`).
The spawn RECEIPT records three of them, dropping `ETERNIA_HERMES_ROOT`
(`.../data/mission_control_serve_session_io.dart`, the
`MissionTransportReceiptKind.serveSpawn` receipt).
So **the live serve reads `profiles/base`** — measuring under another home measures a different
runtime. Everything else launcher-side belongs to the Launcher's docs.

---

## Stage 1 — interpreter and import tax (`interpreter_ms` and its segments)

`_cmd_serve` starts a `BootTimeline` as its first instruction (`serve.py:5131-5137`).
Everything before that instant is `interpreter_ms`: process creation → the command's own first
statement, resolved through psutil and **simply absent when the platform will not give a
creation time** (`agent_runtime/boot_timeline.py:108-118`). That one number used to be the
whole story, and on the 2026-08-17 cold boot it was 20,421 ms. It is now split by
module-global anchors written by the four places that can see the boundaries
(`hermes_cli/_boot_clock.py:47-61`), merged on by `_annotate_import_tax`
(`serve.py:1842`):

`interpreter_boot_ms` (process creation → `main.py`'s first statement: interpreter + `site` +
package import) · `main_import_ms` (`main.py`'s 71 module-scope import statements, 166 imported
names) · `dispatch_ms`
(`main()` entry → this command's first instruction) · and, as DURATIONS inside `dispatch_ms`,
`bytecode_sweep_ms` (the launch-time stale-`__pycache__` sweep) and `harness_parser_ms`
(importing `hermes_cli.harness` + parser registration).

`_boot_clock` is **stdlib only, and must stay that way**: `main.py` imports it at its very top,
so anything it reached for would be paid before the first anchor is readable — the measurement
inside the thing measured (`_boot_clock.py:29-31`).

Two segments have had work aimed at them, and both landed. **`bytecode_sweep_ms`** — two hermes
processes booting against one checkout is a REAL concurrency, so the sweep is claimed with
`O_EXCL` and the loser waits rather than overwriting the winner's claim
(`hermes_cli/main.py:5079-5097`, `:5120`, stale-lock break
`_break_stale_bytecode_sweep_lock` `:5056`;
`tests/hermes_cli/test_bytecode_sweep_lock.py`). **`harness_parser_ms`** — the
`hermes_cli.harness` import used to drag a full plugin-discovery walk in through
`tool_visibility` → `model_tools`; it is now function-local (`tool_visibility.py:100`), guarded
by `tests/agent_runtime/test_tool_visibility_import_deferral.py`, which drives the real chain
through subprocesses after recording that the originally specified assertion was vacuous.

## Stages 2-3 — `booting`, then registry, head pointer, root anchor

The `booting` frame is emitted before ANY heavy boot work, carrying `boot: timeline.stamps()`
(`serve.py:1962`). A supervising launcher can tell a live cold boot from a wedged child by
this frame alone — which is what keeps a short watchdog from killing a cold boot mid-flight and
respawning into another cold boot forever (2026-07-26 kill-loop incident).

**The first statement after that frame is the fingerprint-home capture (HC-1, 2026-08-22)** —
`core_cache.declare_fingerprint_home_boot_site(FINGERPRINT_HOME_BOOT_SITE)` then
`capture_fingerprint_home()`. `HERMES_HOME` is already final (resolved at `hermes_cli.main`
module import by `_apply_profile_override`) and `HERMES_HEAD_HOME` is the launcher's spawn env,
so there is no earlier point where both authorities are valid — and everything AFTER it is
either something that could take a fingerprint or something that could install a persona's
context-local home override. Captured lazily, as it was until HC-1, the instant was whichever
build or consult won the race, which on a persona turn is a thread with the override live: the
operator's SINGLE-PROFILE install demoted `reason=home_mismatch` on three callers in one boot,
twice. The lazy path remains as the fallback for processes with no boot sequence, but in a
process that declared an instant it now emits `snapshot_core_cache fingerprint_home_lazy_capture`
(WARNING) rather than pinning a scope's home in silence. Unmarked on the timeline on purpose:
the capture is two env reads, and its `core_cache` import is ~90% dependencies the boot below
pays for before `ready` anyway. Then three cheap marked phases:

- `chat_registry_ms` — the persona-chat hot-session registry, sized from
  `load_root_runtime_config().persona_chat` (`serve.py:2081-2087`).
- `head_publish_ms` — `publish_chat_head_home()`, the ONE writer of the shared chat-head pointer
  (`serve.py:2095-2098`). Without it a later plain CLI turn degrades to its own profile database
  and mints transcripts where the cockpit never looks.
- `root_anchor_ms` — publishes `agent_runtime.store_root` into the platform-default home's
  `config.yaml`, so an ambient process with no `HERMES_HOME` resolves THIS serve's real runtime
  root instead of a `%LOCALAPPDATA%` shadow (`serve.py:1985-2012`). Injected and OFF unless the
  real entry point turns it on, so a unit test can never write machine-global config. The typed
  outcome is emitted either way — a silent skip is the false-all-clear class the anchor retires.

### Running a chat-binding maintenance verb OUTSIDE the launcher's serve — set `HERMES_HEAD_HOME`

The serve child is spawned with `HERMES_HEAD_HOME` (Stage 0 above). An operator's own shell
usually is not, and that changes what the chat-binding verbs are allowed to conclude — so the
skip below is the fence working, not the runtime broken.

Anything that could call a live chat binding STALE fails closed unless **this process** named
the head home, by `HERMES_HEAD_HOME` or a relay context
(`persona_assignments._session_presence_probe`). The recorded head pointer for the shared runtime root is enough to
read or mint a transcript and deliberately not enough to clear a binding: without the rule, a
verb run under a profile home probes THAT profile's populated database and reads every operator
chat as absent — which is not hypothetical, it is the 2026-07-25 incident, where a reconcile
under the `alice` profile home cleared 10 live bindings on a false
`session_missing_from_session_db` verdict.

What an operator sees, and the way through:

- `harness persona-instance chat-bindings` prints `chat bindings: NOT ANSWERED
  (head_home_not_authoritative)` and **exits 1** — the question was not answered, which is not
  the same as "nothing is stale" (`runtime_commands.py:118-128`).
- `harness persona-instance reconcile` runs its other phases and reports
  `session_binding_skipped: "head_home_not_authoritative"` with zero repairs
  (`persona_instance_identity.py:508`, `:547`).
- Re-run with `HERMES_HEAD_HOME=<the head home>` set — the same value the launcher spawns the
  serve with, i.e. the **base profile home** (`<root>/profiles/base`), not the state root.

The two nearby preconditions fail closed the same way and for the same reason: a database that
does not resolve is `session_db_unavailable`, and one that enumerates zero sessions is
`session_db_empty` — because "the home moved" must never present as "every chat was deleted".

## Stage 4 — service foundations (`service_foundations_ms`)

Four things, all BEFORE `ready`, because `ready` is the frame that carries them — a client that
must ask a second question to learn what code it connected to has a window in which it does not
know (`serve.py:1419-1632`):

1. **Which code** — `build_stamp().frame_payload()`. A durable service that silently pins last
   week's code is the shape of the dispatch dead-flag-proxy incident.
2. **The secret** — `ensure_token(store_root)`. The frame carries the POSTURE only; the token
   value must never appear in a frame, a log, or an event.
3. **The transport** — one serve per root owns the socket lane, decided by an OS-held exclusive
   lock rather than by who booted first. The loser keeps serving stdio and SAYS so on the ready
   frame. Bound here so the ready frame and the registry entry both carry the real port;
   accepting starts later, once the pool exists. **A dead owner yields** (R-L2): the identity
   sidecar outlives its process, so the lock proves the pid with `serve_registry.pid_alive` —
   the same probe behind `stale_dead_pid` one directory over — and, when it is provably gone,
   takes the lane and reports `took_over_from` / `owner_started_at`. A live owner is refused
   unchanged. Also the boot step that decides whether there is a LAN listener at all: the
   `gateway` block's `socket_unavailable` outcome exists because this lane failing is how the
   second door stays shut (§1.1 of `03-transport-and-wire.md`).
4. **Discovery** — `register_serve_instance(...)`, then `prune_stale_serve_instances(...)`, in
   that order on purpose: this serve's entry then classifies `live`, so the sweep can never remove
   it. Only `stale_dead_pid` is pruned — deleting on a failed probe is how a sweep removes a
   RUNNING service's record.

Liveness is proven at READ time, never trusted from the file: a clean exit removes its own entry,
a crash leaves it.

## Stage 5 — the hygiene sweeps

**Orphaned turns** (`orphaned_turn_sweep_ms`, `serve.py:2896` →
`agent_runtime/persona_chat_continuity.py:891`). A native turn holds the OS-backed root lease for
its entire execution and the kernel releases it when the holder dies, so "in-flight record AND
acquirable lease" is proof the turn can no longer settle itself; a session whose lease is HELD is
a live turn in another process and is skipped. It runs BEFORE `ready` — the first hydrate is only
requested after — so repaired records project as typed `turn_interrupted` markers in that hydrate
instead of a console stuck "running" forever. When anything flips, a `state.reconciled` event is
appended so already-connected watermark-gated consumers converge too. Best-effort.

**Detached dispatches** (`dispatch_restore_ms`, `serve.py:2912`). Same moment, same reason:
a row still marked `running` whose owning process is provably gone can never finish, and the
sender is owed that answer. Identity-verified — a recycled PID is not the old owner — and
fail-open. Both counts ride the ready frame when nonzero.

## Stage 6 — `ready`, and the one prewarm thread

The ready frame carries `boot_id`, `build`, `auth`, `install`, `instance`, `socket`, `gateway`,
the RPC manifest, the ops manifest, and `boot_timeline` (`serve.py:1837-1881`). The blocks are
**always present, never conditional on success**: a missing block would read as "old runtime",
while a block whose fields say `error:…` reads as what it is.

**Present is not the same as informative**, and R-L1 is the ruling that cost a session to learn.
The `gateway` block was always there and always carried an outcome — but three different failures
all produced `disabled`, the word for "the operator never asked for a listener", and the one
reader that mattered had just asked. A block states its own outcome AND distinguishes its own
causes, or absence has merely moved inside the block.

The prewarm thread starts **just before** `frames.emit(ready_frame)` (`serve.py:1949-1975`), not
after. The launcher's first request lands within milliseconds of that frame and only the build
that STARTED FIRST can be shared; if the request wins the race it leads its own build and the
warmup queues a redundant second one behind it. Starting a daemon thread costs microseconds, so
`ready` is not delayed.

**ONE thread, and the ordering is the whole stage** (code spells it `EG-3.2`; the same fix is
`HY-H2`, and `serve.py:1715-1716` records two independent investigations reaching it). The
read-model build runs first, the provider warmup (`_load_openai_cls`, `shared_ssl_context`,
`verify_ca_bundle`, `get_tool_definitions`) second, and since 2026-08-23 the persona-chat
actor prewarm (Stage 9a) third — all on the same thread (`serve.py:1718-1728`, `:1060-1086`).
The first two used to be two threads; under the GIL the provider's ~5-8 s of CPU was
subtracted from the build the launcher's canvas is waiting on, and nothing it warms is
consumable before that canvas is authoritative. The third step inherits that reasoning twice
over: it is behind the build for the canvas's sake, and behind the provider warmup because an
agent construction that runs after `_load_openai_cls` does not pay the SDK import itself —
which is the largest single item in a cold construct. All three are injected (a loop unit test
must not import the OpenAI SDK, and must not construct a persona agent, to observe a ready
frame) and each step is failure-isolated. Named cost, carried rather than hidden:
a chat turn sent inside the boot window pays the cold SDK import inline. The boot line then
lands on `agent.log` (`serve.py:1832-1839`):

```
harness serve boot timeline: interpreter_ms=1426 interpreter_boot_ms=114 main_import_ms=328
dispatch_ms=983 bytecode_sweep_ms=0 harness_parser_ms=514 chat_registry_ms=47
head_publish_ms=0 root_anchor_ms=0 store_root_ms=0 service_foundations_ms=125
orphaned_turn_sweep_ms=31 dispatch_restore_ms=0 elapsed_ms=204 total_ms=1630
```

(Live serve, 2026-08-22 15:46:27.) `elapsed_ms` excludes the interpreter; `total_ms` counts
from process creation. **The 2026-08-21/22 population through this boot is 27 boots, `total_ms`
1,042 to 4,335** — typical ~1.5-3 s, with five boots over 3,000 and a tail at 4,335
(08-21 21:59:47). (Corrected 2026-08-24: this line read 28 against doc 08's 27 for the same
window and the same range; a re-census of `harness serve boot timeline` lines in
`profiles/base/logs/agent.log` through 2026-08-22 15:46:27 counts 27, and the two docs now
agree. The same census run to the end of 08-22 counts 33 boots, 1,042 to 4,713 — the log is
append-only, so a later re-take is a superset, not a contradiction.)
Even the tail is an order of magnitude under the cold core build below, which is the
comparison that matters; a six-boot sample reading 1,488-2,990 was the narrower window.

## Stage 7 — the first read-model core, and why it is cold

`_prewarm_read_model_snapshot` calls `build_snapshot(build_info={"caller": "prewarm"})`
(`serve.py:1673`). Naming the caller is what makes this build appear in the log at all:
until the builder emitted its own receipt, every `snapshot_build` line in the boot window
belonged to a caller that RODE this build — which is how one build came to look like three.
The receipt (`agent_runtime.snapshot`), live 2026-08-22 15:46:38:

```
snapshot_build_core role=led caller=prewarm generation=1 build_ms=11235 offset=90007293
sections_top=prompt_observability:4520,agents_readiness:4366,events:842 pid=30588
```

`generation=1` is what makes it cold, and the same process proves it: generations 21 and 22 in an
earlier serve cost `build_ms=1948` and `3439`. The delta is per-process cache fill, and the caches
are named: `agent_runtime/parse_cache.py` (YAML/frontmatter/sha, `(path, mtime_ns, size)`-keyed,
bounded 4096 — profiled as the dominant snapshot cost);
`tool_visibility._cached_tool_names_for_toolsets` (`lru_cache(128)`, process lifetime,
`tool_visibility.py:599-600`); `_cached_profile_readiness_for_visibility` (15 s TTL, `:550`);
`tools/registry.py::_check_fn_cached` (30 s TTL per `check_fn`, `registry.py:225`).

The readiness section publishes its own split. Same cold boot:
`snapshot_agents_readiness walk_ms=2133 tool_visibility_ms=2232`; warm, same day,
`walk_ms=769 tool_visibility_ms=26`.

The walk's per-persona profile binding is `persona_profile_scope` —
context-local, no `os.environ` writes, and therefore no `_WORKDIR_LOCK`. On this
lane that is not a nicety: the walk runs on the builder thread every 2–4 s in the
process that also serves chat turns, so the env-exporting binding rebound
`HERMES_HOME` for every other thread every few seconds. See
02-runtime-data-and-shapes' readiness note for the reachability audit, the
1.3–1.9 s of turn cost it was billing, and the one named residue (`HOME`). The
`prompt_observability` section binds the same way and for the same reason — it is
the more expensive of the two, and its binding also sits on the CHAT-TURN path
(`observability_built`, ahead of the runner's own locked binding), so the switch
stops turns rebinding the process for each other as well.

## Stage 8 — the boot core cache: consult → fingerprint → serve or demote

`agent_runtime/core_cache.py`. The claim it rests on: the first build is not bandwidth
(serializing the core is ~5 ms) — **it IS validation, done by reconstruction.** The module wrote
that claim against a ~20 s first build; that is the design-era figure, and the live cold build is
11,235 ms (Stage 7's receipt). So validation is
made to cost what validation costs. Every successful default-store build persists the core plus
a sidecar carrying the fingerprint of every input the build read; the next process stats those
inputs again. The read path, in order:

1. `build_snapshot` calls `core_cache.consult(caller=...)` **before the coalescer**
   (`snapshot.py:586`) — a ~50 ms stat check with no shared state, which behind the build
   lock would serialize the cheap answer behind an expensive build.
2. `consult` returns immediately unless `lane_armed()` (`core_cache.py:3587`). The riders
   of one boot share ONE judgement and each still emits its own receipt.
3. Match → `label_core(source="cache")` and
   `snapshot_core_cache core_source=cache caller=… inputs=… fingerprint=… offset=…`. It
   deliberately does NOT emit `snapshot_build_core role=led`: there was no build.
4. Miss → `_log_demote` with a reason from the `DEMOTE_*` vocabulary (`core_cache.py:3704`).
   `absent` is the one reason NOT logged — the ordinary cold start would print a line on every
   build in every process — so **a census must not read "no demote line" as "no demote."**
5. A cache hit ALSO starts `maybe_start_shadow_validation` (`snapshot.py:597`): the full build
   runs in the background and compares field-for-field, at most once per process, marked as a
   shadow so completing it does not close the lane.
6. On a full build: `pre_build_fingerprint()` (the consult's own key, reused — an OLDER key can
   only cost the next process a rebuild), `write_back`, then `note_full_build_completed()`
   (`snapshot.py:699`), which disarms the lane (`:659-703`).

**Validity is the stat fingerprint, full stop.** `event_offset` is recorded in the sidecar as a
diagnostic and never read as an input to the match. The offset-keyed design stays refused — but
on one leg, not two. The module's first argument, "the events section is 3 ms of a 5,485 ms
build" (`core_cache.py:27`), is a design-era measurement: Stage 7's live receipt reads
`events:842`, the third most expensive section of that boot, so "an offset key buys almost
nothing" no longer holds on its own. The refusal rests on the second argument, which the numbers
cannot touch: two shipped incidents came from writers that mutate durable state with no EventLog
event at all, and an offset key cannot see them at any price.

A mismatch does not mean a blank canvas: `take_stale_first_core` serves the last persisted core
**labeled stale** while the build runs (`core_cache.py:3817`, `stream.py:1321`). The one-shot
belongs to the SUBSCRIBER, not the process — derived at producer-build time by
`serve.py::_room_wants_stale_first` (`:3375`) — because a boot starts two `stream_frames`
generators and the module-global version handed the allowance to whichever raced first. A
forced-refresh one-shot is refused the stale core outright.

**A cache-hit boot, measured.** Live 2026-08-21 23:02:29 — boot timeline `total_ms=1488`, then
210 ms later `snapshot_core_cache core_source=cache caller=prewarm inputs=2381
fingerprint=cb84a99801e0`, with three riders at `snapshot_build reason=hydrate … role=cache
waited_ms=62 / 30 / 31`. Against an 11,235 ms cold build — that is what the lane buys **when
it hits**; see `## Open rows` for why it currently hits by luck.

**The shadow lane is live and it is finding things.** Five `snapshot_core_shadow_divergence`
lines against four clean `snapshot_core_shadow ok=true divergence=none`, every divergence on
`caller=prewarm`, over three distinct sections: `parity.event_log_bytes` (2026-08-20, ×3),
`prompt_observability` (2026-08-21 17:20), `runtime_config` (2026-08-21 23:02). The rebuilt
core is adopted each time, so no stale canvas shipped — the receipt is doing its job, and each
line is an un-widened closure gap.

**A write-back is one unit** (MCF-21). The cache is three files — core, sidecar, entries — and
every write-back mints `gen-<stamp>/` under `serve_read_model/`, writes all three while nothing
points at it, and lands by replacing ONE small pointer file (`live.json`). Atomicity rides that
single replace; a crash at any earlier point leaves a directory the pointer never named,
invisible to every reader and reaped by the next write-back.

Receipts are indexed, not merely emitted: the channel table at `core_cache.py:178-193` lists every
token, its second channel and its census rule; `test_core_cache_channel_table.py` drives BOTH
directions (a token no row names, a row naming a token no writer emits); and
`core_cache_census.py` executes those rules as code (`scripts/core_cache_demote_census.py`).

## Stage 9 — persona prewarm

`agent_runtime/persona_prewarm.py`. Not a boot stage — it is **gesture-triggered**, via the
JSON-RPC verb `runtime.persona.prewarm` (`agent_runtime/serve_rpc.py:2009`), which the launcher
fires per persona chip when the palette opens.

The verb resolves the persona SYNCHRONOUSLY through `agent_create.resolve_persona` — the same
lookup `runtime.agent.create` refuses on, so "unknown persona" is one fact with one spelling
across the pair — then queues and returns
`{persona_id, accepted: true, state: "started" | "already_running"}` without waiting. `profile:`
ids are refused with their own reason: their memo keys are keyed on the instance the create
mints, so there is nothing to warm beforehand.

The work is `warm_persona_memos` (`persona_prewarm.py:225-285`): run the create's own
visibility resolution and DISCARD it. It fills process-lifetime memos and nothing else — no
store write, no event, no minted id, no lock — which is why it can be fire-and-forget.

**ONE daemon worker, serialized, and that is measured rather than assumed.** N threads would
each find the `check_fn` TTL cache cold and each run the full toolset sweep — N concurrent
`docker version` subprocesses. In the **bench series** — six synthetic personas warmed back to
back — the costs were `2172 / 0 / 16 / 0 / 16 / 0` ms: the queue's whole cost is ONE item, so a
second worker could buy at best 16 ms while running a second cold sweep beside it. Bounded
concurrency is therefore NOT implemented. But that premise is the bench's, and the live series
below does not have that shape — every warm after the first costs 109-375 ms, not ~0 — so warm
#2 on a REAL roster is the number to re-measure before revisiting the decision.

**KEEP, ruled 2026-09-04 (operator).** The stage was re-opened on the reading that the
`check_fn` sweep leaving this path had taken its value with it. It had not: the FIRST warm
in a cold process still costs 1,418–1,543 ms of registry import that the first create would
otherwise pay inline, while the SECOND costs 1–2 ms. So the worker, the verb and the
launcher trigger all stay, and any per-persona warm beyond the first is ruled OUT. The
argument and both measurements are in `agent_runtime/persona_prewarm.py`; the refusal is
listed in doc 08.

Receipt, format-pinned by `tests/agent_runtime/test_persona_prewarm.py` —
`persona_prewarm done persona=<id> elapsed_ms=<n>`. Live 2026-08-22 15:46:39-40:
`backend_dev 515`, `base 109`, `dev 235`, `neko_supervisor 375`, `qa 171`. That is NOT "the
first warm pays the sweep, the rest ride it": `neko_supervisor` alone costs 73% of the first
warm, and no later warm falls to the bench's ~0. The riding is partial, and how much of each
later warm is residual per-persona work is unattributed.

**The toolset-key warm, and where it is load-bearing.** `warm_persona_memos` applies
`apply_chat_lane_tool_scope` before resolving, to land on the EXACT `(toolsets, blocked)` key the
create reads. The older claim that warming without it "primes nothing the create reads" was
**measured false on 2026-08-22** and is corrected in place: every expensive input (registry
populate, plugin discovery, `check_fn` probes) is process/callable-keyed and warms either way.
The alignment is load-bearing on exactly one configuration — an install whose runtime default is
the BOUNDED posture, root `config.yaml` `agent_runtime.tool_permissions.default_mode:
profile_default` (`config.py:1015-1047`). The shipped default is `unbounded`, under which the two
keys coincide and the call is free; `test_the_warm_fills_the_exact_toolset_key_the_create_reads`
is the gate that reds if the line is removed.

## Stage 9a — persona-chat actor prewarm (2026-08-23)

`agent_runtime/persona_chat_actor_prewarm.py`. Both a boot stage and a gesture-triggered one,
and the only warmup in the harness that constructs a real agent.

**What it removes.** `write_ahead → agent_ready` is bimodal: 60-600 ms on turn 2+ of a chat
root, **3.0-3.6 s on turn 1**, because that is where `ProfileRunner._execute_agent_run`
builds the agent (OpenAI client, tool-definition build with its own `check_fn` sweep,
`tool_search` activation). Live pair, 2026-08-23 with `persona_chat.hot_sessions_enabled: true`:
`17:33:01Z` (first message after the boot) `agent_init_cold=true`, bootstrap 3,782 ms of which
`agent_construct_ms=3000`, first byte 10.0 s; `17:33:17Z` (second message, same chat)
`resident_actor_reused=1`, bootstrap **62 ms**, first byte 3.4 s. The registry and the factory
already existed; nothing called them off the turn.

**Boot pass** — third on the one prewarm thread (Stage 6), warming at most
`persona_chat.max_hot_sessions` chats, most-recently-active first, from instances with a bound
`default_chat_session_id`. It only QUEUES; the constructions run on this module's own single
daemon worker. **Chat-open** — both arms of `persona instance open-chat`
(`_cmd_persona_instance_open_chat`, and the mint arm `_cmd_persona_instance_open_new_chat`,
which is the higher-value one: a freshly minted root has no turn that is not its first).
Deliberately NOT `PersonaInstanceStore.open_chat`, which the send path re-enters on every turn.
Gated on `hot_sessions_enabled` alone and inert without it — no registry, nowhere to put an
actor, no thread started; that is the state of every CLI one-shot.

**Three properties it is built around**, each of which is the difference between a saving and
a pure cost:

1. **The construction runs the REAL path.** `AgentRunRequest.prewarm_only` re-enters
   `_execute_agent_run`, so the agent is built inside `_WORKDIR_LOCK`,
   `persona_profile_context`, the workdir and tool/terminal/skill scopes, and this persona's
   MCP admission — with the same teardown on the way out. It stops immediately after the
   `acquire()` bookkeeping: no `agent_ready`, no conversation.
2. **The signature is the turn's own.** `acquire` reuses only on a byte-equal signature and
   revision, and a mismatch CLOSES the entry and rebuilds. So the prewarm calls the turn's
   own `mission_chat_turn_context.mission_chat_runtime_signature` (made public for this) and
   reproduces each input through the turn's own resolver, with the tip and revision read via
   `_persona_chat_native_tip` / `_persona_chat_native_revision`.
3. **It yields.** `profile_runner.agent_runs_in_flight()` counts real runs from `run()`'s
   entry, not from the lock; the prewarm stands down as `skipped_turn_active` rather than
   holding `_WORKDIR_LOCK` while an operator message waits.

**Named residue.** `--agents-file` — the operator's workspace `AGENTS.md`, attached per turn
from a launcher-side selection — cannot be known here, so a workspace-bound chat mismatches on
its first turn and rebuilds: wasted background work, never a wrong answer.

Receipts: `persona_chat_actor_prewarm root=… outcome=… elapsed_ms=…` per item and
`persona_chat_actor_prewarm pass candidates=… queued=… skipped=… elapsed_ms=…` per boot pass
(07-observability's census). **Read live 2026-08-24: 59 such lines in
`profiles/base/logs/agent.log`** — the passes fire and the items warm, e.g. `2026-08-23
20:59:32 … outcome=warmed elapsed_ms=250`, alongside the same pass's 2,109 / 1,328 / 78 ms
items; no `skipped_turn_active` storm, so the yield rule is not firing too eagerly. What the
lines do NOT yet close is the first-turn half: **every sampled fresh-chat first turn still
reads `agent_init_cold=true`**, and the two neko ones name
`resident_rebuild_component_workspace_agents` as the moving component — the `--agents-file`
residue above, exactly as predicted. That re-take stays owed
(`planned/chat-turn-prep-cost.md` §7).

## Stage 10 — demote builds and same-offset core reuse

`agent_runtime/demote_core_reuse.py`, consumed only by `agent_runtime/stream.py` (`:966`,
`:992`, `:1050`). The waste: three `snapshot_build reason=demote role=led` lines at the SAME
offset 89961793 on 2026-08-22 10:50, `build_ms` 3017 / 3210 / 2388, identical fingerprint.
`build_snapshot`'s coalescer cannot merge them — it is deliberately strict, and a caller
arriving mid-build waits for the NEXT build rather than riding the in-flight one. That rule is
right; the waste is SEQUENTIAL, between builds.

What makes reuse safe where riding an in-flight build is not: the strict rule protects against
content that may predate the caller's arrival — a claim about TIME. This module claims
POSITION instead, and position is checkable. A core stamps `parity.watermark.event_offset`,
captured BEFORE the build starts reading, so it is a lower bound on what the core contains;
when the log's end offset right now equals that number, nothing was appended in between. An
UNKNOWN position on either side refuses rather than comparing two `None`s as "nothing moved."

It is a reuse of the CORE, never a dedupe of emissions: every consumer still gets its own
complete frame from its own batch, and its own deep copy of the shared dict. Provenance is
labeled `core_source=reused_same_offset`, and `build_ms` stays the ORIGINAL build's cost read off
the reused envelope — `build_ms=0` would be a lie about a build that cost three seconds.
`waited_ms` is the reusing caller's own wait: the number the remedy moves.

## Stage 11 — agent-create phases

`agent_runtime/agent_create_phases.py`, receipt at `:88-89` —
`agent_create_phases persona=%s instance_ms=%d phases=%s pid=%d`.

`PHASE_ORDER` (`:103-127`) is the authority and the nesting is encoded in the order:
`bindable_ms`, `chat_root_ms`, `instance_write_ms`, `create_patch_ms`, `wire_row_ms`, then the
three reads inside that projection (`permission_options_ms`, `chat_lane_scope_ms`,
`tool_visibility_ms`), then `event_append_ms`, `spawned_by_write_ms`. A key absent from the
tuple is a bug at the call site and `record` refuses it loudly rather than printing a span
nothing documents; only what was recorded is rendered, never a default.

This is where Stage 9's warm is falsifiable. Live 2026-08-22 13:43-13:44, same persona, same
pid: `instance_ms=1046` (`chat_lane_scope_ms:859`) cold, then `78` and `186` warm.

## Shutdown

Two exits, different frames on purpose (`serve.py:3117-3200`).

**Ordinary.** The reader loop ends on a `shutdown` op or EOF, `pool.shutdown(wait=True)` joins,
`liveness_stop.set()` stops the pump, the socket lane closes, the registry entry is removed,
and a `{"event": "shutdown", "pid": …}` frame is emitted. Socket clients hear it BEFORE the
transport closes under them — an attached client whose socket simply died could not otherwise
tell a clean shutdown from a crash. Returns 0.

**Detached (`--service` only, L-h 2026-09-05).** Under `harness serve --ndjson --service`
those two endings stop being one. A `shutdown` op is an ORDER and takes the paragraph above,
unchanged. EOF is an OBSERVATION — *the starter detached* — so the loop logs
`stdio_owner_detached`, broadcasts it to attached socket clients, swaps the stdio frame sink
for a null sink (`_FrameWriter.detach`; a late write that loses the pipe latches the same
state from its `BrokenPipeError` rather than raising), keeps BOTH socket lanes serving, and
parks the main thread on a `service_stop` event ABOVE the finalization — so releasing it runs
the teardown above in its untouched order rather than a second copy. Three things release it:
`{"op":"drain","force":true}` over the socket (`_finish_drain` sets the event beside the
reader's existing `drain_wakeup`, at the same instant), `SIGTERM` where the platform delivers
one (which Windows does not — `os.kill(pid, SIGTERM)` is `TerminateProcess` and runs no
handler), and a stdio `shutdown` received before EOF, which never reaches the park. A
`--service` starter that LOSES the socket ownership lock exits 0 with
`{"event":"serve_owner_exists","pid":…,"port":…}` before a pool, a registry row or a `ready`
frame exists — a stdio loser still serves stdio, but a service loser would be a second
executor against one store that nothing discovers and nothing can stop.
`--service --no-socket` is refused: the socket is the only lane a drain can arrive on.

**Drained.** When a drain is in flight the DRAIN owns the terminal frame (`drain_complete` /
`drain_timeout`); emitting `shutdown` too would tell a consumer that a TIMED-OUT drain ended
cleanly. If the reader gets there first it waits `_DRAIN_ABANDON_GRACE_SECONDS = 5.0` for the
monitor and, failing that, emits a typed `drain_abandoned` frame and returns
`DRAIN_TIMEOUT_EXIT_CODE = 3` — the SAME code a timeout uses, so a supervisor can tell "drained"
from "gave up". A drain that exits silently is exactly the crash it exists to replace. After a
TIMEOUT the pool is joined with `wait=False` and the process leaves via `os._exit`:
`concurrent.futures` registers an atexit hook that JOINS every worker thread, so an interpreter
carrying a stuck worker hangs on the way out. Deadline `DEFAULT_DRAIN_DEADLINE_SECONDS = 30.0`.

`shutdown` is stdio-only (`OPS_STDIO_ONLY`, `serve.py:342`). There is no shutdown hook for the
snapshot prewarm, the persona-prewarm worker, or the delivery drain: all three are daemons,
deliberately, because a process on its way down must not wait on a cache fill.

---

## Invariants

1. **Absent means "not measured", never zero** — a fabricated zero cannot be told from a
   genuinely instant phase (`boot_timeline.py:18-21`, `_boot_clock.py:33-36`).
2. **A phase is recorded as it COMPLETES**, so a boot that wedges before `ready` still carries
   what it finished; a phase recorded twice ACCUMULATES (`boot_timeline.py:140-151`).
3. **An instrument must never take the boot down.** Every emission, annotation and stamp
   resolution on the boot path is exception-guarded.
4. **`booting` precedes all heavy work**, and every block on `ready` is present unconditionally,
   saying `error:…` rather than being absent.
5. **`_boot_clock` imports stdlib only** — anything else is measured by what it delays.
6. **The store decides; the projection serves.** A cached or stale-labeled core never deletes,
   never refuses a write, never wins a conflict; a stale core is `parity.freshness.state =
   "stale"` and therefore never `live` (`core_cache.py:104-116`).
7. **If a shadow receipt shows divergence, the fix is WIDENING the stat set — never trusting the
   cache harder** (`core_cache.py:90-91`).
8. **The fingerprint decides cache validity, full stop.** No event-tail replay, ever.
9. **Adding a receipt to the core-cache lane means adding a ROW to the channel table**, and the
   test drives both directions (`core_cache.py:194-198`).
10. **The serve's cwd is a per-turn value**, safe to mutate process-globally only while turns are
    serialized by `profile_runner._WORKDIR_LOCK` (an `RLock`, `profile_runner.py:1434`) held for
    the WHOLE run. Nothing else enforces it, and widening turn concurrency starts by failing
    `tests/agent_runtime/test_serve_cwd_serialization_invariant.py`.
11. **A frozen `snapshot.json` / `read_model.db` mtime says nothing about liveness** — a live
    serve answers from in-memory lanes and a 20 s payload cache (`_CACHEABLE_ARGV`,
    `_READ_CACHE_MAX_AGE_SECONDS = 20.0`, `serve.py:1054`). Check frames, not mtimes.
    Stronger since Stage 6 (2026-08-22): both files now have NO writer at all —
    the lane that produced them is retired — so a copy left on disk is a legacy
    artifact and its mtime is not merely uninformative, it is meaningless.
12. **A Windows `python.exe` under a base `Python311\` in the process tree is HEALTHY** — the venv
    trampoline's target, not a re-exec; `gateway.py::_filter_venv_launcher_stubs` (`:588`) already
    collapses the duplicate PID.

## Open rows

Nothing below is implemented. Each links to a plan carrying its own evidence and gate.

- **The boot core cache never converges here** — `never_converged` 10× 2026-08-20→22, and
  post-IC-1..3 five MORE on 2026-08-23 naming the chat-turn sidecar family
  (`mission_chat_turns/`, `mission_chat_steer/`, `persona_chat_leases/`,
  `prompt_observability/`); the closure admits runtime-authored churn, so cache-hit
  boots are luck. → [`planned/core-cache-input-closure.md`](planned/core-cache-input-closure.md)
- **`home_mismatch` demotes on a single-profile install** — fired 2026-08-21 16:04:32 and again
  2026-08-22 13:36, both the same one-boot/three-caller shape; the census executes the rule
  calling this a producer defect, not an ordinary
  miss. → [`planned/core-cache-home-capture-timing.md`](planned/core-cache-home-capture-timing.md)
- **The cold first build is still ~11 s**, dominant sections known by name, no shipped stage
  aimed at it. → [`planned/cold-first-core-build-cost.md`](planned/cold-first-core-build-cost.md)
- **The boot resubscribe still buys a fourth full core** — HY-L2, the one stage of its plan that
  did not ship. → [`planned/boot-resubscribe-fourth-core.md`](planned/boot-resubscribe-fourth-core.md)
- **Turn concurrency is capped by a process-global `chdir`** (Invariant 10); retiring it is
  designed, unscheduled. → [`planned/serve-cwd-per-run-paths.md`](planned/serve-cwd-per-run-paths.md)

## Supersedes

- [`archive/2026-08-22-pre-consolidation/MISSION_BOOT_WINDOW_PLAN_2026-08-17.md`](archive/2026-08-22-pre-consolidation/MISSION_BOOT_WINDOW_PLAN_2026-08-17.md)
  — the 47.5 s decomposition. Its naming is `BW = boot window, H = hermes, L = launcher` in
  one sequence: **BW-0, BW-H1, BW-H2, BW-H3, BW-L4, BW-L5, BW-L6, BW-L7** — eight stages, not
  `BW-1..N`/`L0..L6`. **All eight shipped**, verified against code 2026-08-22. The four
  hermes-side ones are carried above: BW-0 is Stage 1, BW-H1 is Stage 8, BW-H2 and BW-H3 are
  the two segment fixes in Stage 1. BW-L4..L7 are launcher-side.
- [`archive/2026-08-22-pre-consolidation/BOOT_HYDRATE_SECOND_READ_2026-08-17.md`](archive/2026-08-22-pre-consolidation/BOOT_HYDRATE_SECOND_READ_2026-08-17.md)
  — stages **HY-0, HY-H1, HY-H2, HY-L2**. HY-0 is the `snapshot_build_core` receipt and the
  `waited_ms` rename (Stage 7); HY-H1 is the persisted core, spelled `EG-3.1` in code (Stage 8);
  HY-H2 is the single prewarm thread, spelled `EG-3.2` (Stage 6). **HY-L2 did not ship** — see
  `planned/`. The doc contains no `EG-*` stages of its own; that identifier is another family.
- [`.../harness-serve-design.md`](archive/2026-08-22-pre-consolidation/harness-serve-design.md)
  — the serve's original design record. Constants carried forward: `DEFAULT_POOL_SIZE = 4`
  (`serve.py:236`) and the 20 s read cache (Invariant 11).
- [`.../serve-runtime-truth.md`](archive/2026-08-22-pre-consolidation/serve-runtime-truth.md)
  — both false diagnostic tells carried forward as Invariants 11 and 12.
- [`.../env-determinism-audit.md`](archive/2026-08-22-pre-consolidation/env-determinism-audit.md)
  — §4's serve-cwd concurrency invariant carried forward as Invariant 10.

There is no `## Unverified carry-forward` section: every claim above cites a `path:line`, a named
function, or a quoted live receipt. Archived claims that could not be re-verified in today's code
were dropped rather than carried.
