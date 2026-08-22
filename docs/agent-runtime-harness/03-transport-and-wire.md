# 03 — Transport and Wire

How a shape leaves this process. The runtime speaks four transports — an argv
lane, a JSON-RPC method lane, an op lane carrying a push subscription, and MCP —
and one warm serve process answers all of them. This is the current,
code-verified contract for each: the frames, how a consumer negotiates what it
can fold, and which parts of the wire are pinned byte-for-byte across the repo
boundary. The goal/task mission lane was removed 2026-07-30; chat is the only
lane and nothing below carries a task frame.

## 1. The serve process model

`hermes harness serve --ndjson` is one warm process replacing the per-call CLI
spawns the Launcher bridge otherwise pays a ~3s import tax on
(`hermes_cli/harness_parts/serve.py:1-7`). Requests arrive as NDJSON, one frame
per line, and dispatch into the **existing** harness argparse tree unchanged:
`dispatch_argv` (`serve.py:937`) builds a fresh parser per request
(`_build_harness_parser`, `:925`) and calls the same `_cmd_*` handler the CLI
would, including the harness error-envelope contract — argv arrives verbatim as
the bridge already builds it, which keeps the per-call CLI fallback
byte-identical to the served path. **`ready` is a BOOT frame, not a request
frame** — classified as boot in the protocol block (`serve.py:20`) and emitted
exactly once per process, at `:1663-1664`. What rides a REQUEST is `line` × N,
`exit` and `stderr` (`:31-37`), plus a typed `error` frame when argv parsing or
dispatch fails (`:1346`, `:1355`). Handlers `print()` directly and streaming
turns emit deltas live, so stdout/stderr are swapped once for contextvar-dispatching
proxies (`_LineFrameProxy`, `:706`), one write lock keeps frames atomic, and
writes from handler-spawned threads carry `"id": null`.

**Chat turns are marked at the argv boundary, and the mark is a safety
contract.** `_CHAT_TURN_COMMANDS = (("mission-chat", "message"), ("mission-chat",
"steer"))` (`serve.py:379`); `_ArgvRequest.__init__` (`:832`) matches the argv
tail against it. The `ping` reply counts them (`_busy_frame`, `:1274`) because
the Launcher supervisor must never recycle serve while one is in flight, and a
drain deadline expiring with `held_by_chat_turns > 0` emits a NON-terminal
`drain_timeout`, keeps serving, and re-arms (`"event": "drain_timeout"` `:2418`,
`held_by_chat_turns` `:2434`, `"terminal": not chat_turn_ids` `:2436`, the
keep-serving re-arm `:2438-2445`; contract at `:76-82`).
Recording safety outranks restart latency. A running `harness stream` request is
the sole exception to "running requests cannot be cancelled" — cooperatively
cancelled, releasing its pool worker (`is_runtime_stream`, `:835`, used `:2952`).

**Two transports, one dispatcher.** `serve_loop` is transport-agnostic. One
serve per root owns a localhost socket, decided by an OS-held exclusive lock
(`agent_runtime/serve_socket.py`); the loser runs stdio-only and says so on
`ready`. The handshake is challenge-response and the SERVER speaks first —
`server_hello` carries a 64-hex nonce, the client answers
`HMAC-SHA256(key=<per-root token>, msg=<nonce>)`. **The token never travels**: it
is the HMAC key, never a field, so a captured transcript is unreplayable
(`serve.py:150-171`). Only authentication failures (`bad_proof`,
`hello_required`, `hello_malformed`) charge the rate limiter; server-state
reasons never do, because charging them made a blocked window extend itself
forever.

## 2. Capability advertisement — `rpc` and `ops`

A durable service outlives the install it was started from, so "what does the
thing I am attached to carry" must be answerable at any time. Two manifests ride
`ready` (stdio), `hello_ok` (socket), and the re-askable `version` reply:
`"rpc"` from `serve_rpc.manifest()` (`agent_runtime/serve_rpc.py:255`) — nine
methods registered via `@method` today (`:455, 592, 920, 981, 1268, 1483, 1661,
1960, 2009`) — and `"ops"` from `ops_manifest(transport=…)` (`serve.py:298`),
`{"contract", "transport", "ops", "subscribe_lanes"}`.

Both follow one discipline: **a set plus an integer.** The set grows when a verb
is added — a client only ever sends a verb it FOUND — and the integer moves only
when an existing verb's shape changes incompatibly. `ops` is answered **per
transport** because the answer genuinely differs: `shutdown` is the stdio
owner's verb and is refused on the socket, so `OPS_STDIO_ONLY` (`:289`) is
unioned in only for stdio, and the block names the transport it describes so a
cached copy cannot be mis-applied.

**The push gate is `subscribe_lanes`, not `ops`.** `"subscribe" in ops` says the
op is dispatched; `"stream" in subscribe_lanes` says THIS lane is carried
(`SUBSCRIBE_LANES = ("stream",)`, `:295`). A runtime predating the advertisement
carries no `ops` key — read that as "undiscoverable, keep the existing lane",
not as a failure. The advertisement exists because a probe cannot distinguish
"too old" from "refused THIS subscribe": `unsupported_lane`, `draining` and
`already_subscribed` are all real answers a live lane gives (`:2595-2651`).

## 3. The mission-control stream

`agent_runtime/stream.py::stream_frames` (`:841`) is the single producer body.
It yields exactly one `hydrate`, then tails the event log from that frame's
`watermark.event_offset` (`_resume_offset`, `:305`) emitting `patch`, full-core
`delta`, and `heartbeat` frames. **An unknown resume position is not byte 0**:
`_resume_offset` returns `None` rather than folding a missing watermark key and
an explicitly-unknown one into `0`, because tailing from `0` replayed the entire
event log as fresh activity at the root of every Mission Control surface
(`:883-889`).

**Freshness backstop.** Every store mutation is supposed to append an event; a
write that slips the rule would freeze watermark-gated consumers forever, since
they drop same-offset re-hydrates. At heartbeat cadence the loop fingerprints
the scope/catalog state no evented store guards, and a fingerprint change with
no offset advance appends a synthetic `state.reconciled` that flows out as an
ordinary full-core delta (`_scope_fingerprint`, `:1194`;
`_append_state_reconciled`, `:1346`). SLO: staleness ≤ 2× heartbeat interval.
Every `state.reconciled` names a producer bug to fix at source.

| Frame | Builder | `schema_version` | Carries a core? |
|---|---|---|---|
| `hydrate` | `hydrate_frame` (`:223`) | 1 | yes — the full snapshot under `core` |
| `delta` | `delta_frame` (`:376`) / `delta_batch_frame` (`:393`) | 1 | yes — ONE core per batch |
| `patch` | `patch_batch_frame` (`:456`) | 2 | **no** |
| `heartbeat` | `heartbeat_frame` (`:328`) | 1 | no |

`hydrate` carries `core`, `identity_map`, `watermark`, and the parity envelope's
`completeness` / `drops` / `parity_warnings`. With the patch lane on it also
carries `delta_patches: true` — the signal to RETAIN this frame's raw core as
the patch base — and the ACCEPTED `fold_entities`, sorted (`:291-293`). Both are
absent when the flag is off, so a flag-off hydrate stays byte-identical.

`delta_batch_frame` carries the SAME core exactly once per drained batch — the
old loop shipped one `build_snapshot()` per event, a ~9MB rebuild per append.
Its shape is strictly ADDITIVE over the single-delta shape: `watermark`/`seq` at
the FINAL offset so a `>`-only sequence gate applies the batch once,
`entity`/`op` still the LAST event for pre-batch consumers, `events` +
`coalesced_count` the additions (`:396-412`). Cap `_DELTA_BATCH_CAP = 256`.

`patch_batch_frame` carries op-based wire patches and **no core**. `base_offset`
is the watermark the batch applies FROM — the client folds only when its held
watermark equals it, and a mismatch is a sequence gap → checkpoint resync;
`watermark.event_offset` is the post-batch offset the fold advances to
(`:456-510`). **A frame that advances a watermark must carry the state that
justifies it**, which `batch_carries_patch_rows` (`:424`) guards: coverability is
decided per event, so a covered domain event whose paired `state.patched` never
arrived is coverable ON ITS OWN, and the frame that used to ship was
`{"type":"patch","patches":[],…}` — the client advanced its watermark having
folded nothing, with no downstream gate able to see it. Five producer paths
reach that case, so the guard sits at the drain, and `patch_batch_frame` refuses
to BUILD it too (`:493`).

`heartbeat` is liveness only. `offset=None` is the honest heartbeat of a stream
that could not read the log's tail and must not be stamped `0`, which every
watermark-gated reader would take as a real cursor at the head of the log
(`:337-341`). A heartbeat whose offset is AHEAD of the consumer's last applied
state-bearing frame proves a frame was missed: keep the applied watermark and
rehydrate.

`_delta_op` (`:1375`) is a four-arm table — `run.tool.*` / `run.progress` →
`chat.trace.appended`, `incident.*` → the source type verbatim,
`persona_assignment.*` → `instance.upserted`, everything else →
`event.appended`. `_redaction_safe_json` (`:1426`) makes payloads JSON-safe:
lists truncated to 200 items, secrets rewritten via the single-homed
`redaction.ENV_SECRET_ASSIGNMENT_RE`.

## 4. Patch frames and the fold negotiation

The producer half is `agent_runtime/state_patches.py`. With
`read_model.delta_patches` on, a store chokepoint mutating a keyed entity
appends a `state.patched` event carrying a **wire-level op** — `upsert` (with
`changed`), `remove`, or `refresh` (`:96-98`). The projection is hermes': at
emit time the changed entity goes through the exact per-entity projection
`snapshot.py` uses, so the launcher folds projected fields verbatim and never
re-derives. One authority, no per-field allowlist (`:1-32`).

The lane ships ON — `SHIPPED_DELTA_PATCHES = True`
(`agent_runtime/runtime_config.py:73`). A config FAULT lands on
`FALLBACK_DELTA_PATCHES = False` (`:82`), off AND loud, because "emit a new
class of event from every store chokepoint" is not what a runtime should infer
from silence it could not read. Sizing is capped by
`EVENT_PAYLOAD_LIMIT_BYTES = 4096`: an oversized `changed` value becomes an
accounted `{"oversize": true, "bytes": N}` marker, and if the payload still
overflows the patch degrades to `op: "refresh"` — accounted, never a partial
merge the client cannot vouch for (`PATCH_VALUE_BUDGET_BYTES`, `:115-116`).

**Capability negotiation.** A consumer's fold is only as wide as its own entity →
core-section table, and a `patch` for an entity it cannot fold is strictly WORSE
than the `delta` it replaced — the consumer pays the patch and then a whole core
anyway. So it declares: `hermes harness stream --fold-entities a,b`
(`hermes_cli/harness.py:1367`, parsed by
`agent_runtime/patch_coverage.py::parse_fold_entities_option`, `:404`)
or `{"op":"subscribe","lane":"stream","fold_entities":[…]}` (`:2623-2631`).
Rules, all in `agent_runtime/patch_coverage.py`:

- **Omitting the declaration means `HISTORICAL_FOLD_ENTITIES = {persona_instance,
  incident}`, not the empty set** (`:355`) — absence is what every fielded client
  sends and that set is exactly today's wire, so defaulting to empty would have
  demoted every connected client to full cores forever. An EXPLICIT empty
  declaration is honoured as empty (`normalize_fold_entities`, `:363`).
- **The shared producer accepts the INTERSECTION**, never the union
  (`accepted_fold_entities`, `:380`): one producer fans every frame to every
  subscriber, so a promotion must be safe for everyone in the room.
- **The accepted set is echoed, not assumed** — on the `subscribed` ack
  (`serve.py:2737`) and again on the hydrate (`stream.py:293`). A client can be
  honoured for strictly less than it asked for.
- **A malformed declaration is REFUSED**, not read as absent (`subscribe_denied`
  / `invalid_fold_entities`, `serve.py:2633-2639`) — a client that meant to
  narrow and was silently widened back would get patches it cannot fold.
- A batch naming any undeclared entity is demoted IN FULL to a core-bearing
  frame; there is no partial patch frame (`_batch_frames_with_liveness`,
  `stream.py:795-838`). The demotion bills `snapshot_build reason=demote`
  (`BATCH_REASON_DEMOTE`, `:64`), which makes a foldable update that paid for a
  whole snapshot greppable.

Negotiation makes a new entity POSSIBLE; it does not enable one. The producer
still emits `state.patched` only for entities its chokepoints cover.

## 5. Attachment receipts

`log_stream_attach` (`stream.py:176`) writes ONE line per attachment to the
shared producer, at subscribe time, into the serve child's own `agent.log`.
Three call sites attach a reader to the same producer, and until this line
existed the log named none of them:

| Caller | `op` / `purpose` | Site |
|---|---|---|
| socket/stdio op lane | `subscribe` / `stream_lane` | `serve.py:2712` |
| RPC office lane | `runtime.office.subscribe` / `office_patch` | `serve_office_subscriptions.py:902` |
| argv CLI | `harness_stream` / `cli_stream` | `runtime_commands.py:507` |

`op` is the call as the client made it, `purpose` is what the attachment is FOR
— neither implies the other. `pid` rides LAST here and on both build families
(`snapshot_build`, `snapshot_build_core`), so an attachment and the builds it
paid for join on one key instead of on wall clocks (`stream.py:108-124`). It
never raises — an instrument must not be why a subscribe fails.

**Who paints the boot's one stale core is a property of the ROOM**, so
`stream_frames(wants_stale_first=…)` is stated by the caller —
`serve.py::_room_wants_stale_first` (`:1954`) reads the hub's two subscriber
tables at producer-build time, `_cmd_stream` (`runtime_commands.py:544`) states
`True`, default `False`. It cannot be re-derived inside the producer: the
subscriber attaching FIRST at boot is the RPC office lane, whose sink discards
every non-`office_actor` row, and measured 2026-08-18 two boots in three handed
the stale paint to that sink (`stream.py:897-913`).

## 6. The office push lane is a re-envelope, not a second derivation

`agent_runtime/serve_office_subscriptions.py` registers with the SAME
`StreamHub` every stream client uses, under a sink re-wrapping the frames it
cares about as JSON-RPC notifications. It adds no derivation, and three
consequences followed: one producer, so a patch cannot exist on one lane and not
the other; an RPC subscriber COUNTS as a subscriber, so the hub does not stop
producing under it; backpressure and drop accounting inherited whole (`:1-24`).
What crosses is decided by `state_patches.office_patch_scope`, the ONE authority
for "is this row in this workspace" — single-homed because it was not, and the
fork cost a silent drop when `office_surface` became promotable with a BARE
workspace id that failed the private "id under `<workspace_id>/`" restatement
(`:26-45`). A `hydrate` or `delta` means the batch was not patch-coverable, so it
becomes a resync notification; an UNKNOWN frame type takes the same branch
deliberately. Drops are typed, never silent: a subscriber outrunning its bounded
buffer gets `subscription_dropped` naming which of the two bounds tripped —
frame count or bytes — then is unsubscribed (`serve.py:2676-2689`).

## 7. The PUSH-vs-RPC boundary, and the fork boundary

**The 2026-08-13 ruling stands: this fork owns the better PUSH lane; upstream
owns the better CALL lane.** The push half is live-on-by-default —
`SHIPPED_DELTA_PATCHES = True`, the hub lane advertised on every greeting, the
launcher gating on `subscribe_lanes`. The call half deliberately mirrors
`tui_gateway`'s JSON-RPC 2.0 shape and error codes rather than minting a third
convention, and sits BESIDE the argv lane: a frame is claimed by the method lane
only when it names `jsonrpc` or `method`, neither of which an argv request has
ever carried (`serve.py:110-115`).

**The fork boundary is the reverse of the natural assumption: `agent_runtime/`
is not in upstream at all.** The check that proves it is a path filter, not a
judgement call — `git diff --name-only <baseline>..HEAD` minus
`^(agent/|agent_runtime/|hermes_cli/harness)` and minus
`^tools/(agent_chat_tool|mission_goal_tool)\.py$` — and anything it surfaces
needs a ledger row. Archived doc 17 carries the rows; doc 18 carries the merge
rehearsal that priced them — 59 conflicted files in its first table, 63 on the
same-day re-rehearsal (`:151-153`), and only one named by doc 17 either way, so
the next sync is not a two-file merge. One transport-relevant row:
`scripts/generate_agent_runtime_stream_fixtures.py` is fork-created and
fork-only by construction, and sat in upstream-owned `scripts/` with no row
until it was filed.

## 8. The launcher-side consumer contract

`tests/fixtures/stream_frames/` holds fourteen manifest-pinned files. The
EterniaLauncher repo commits **byte-identical copies** under
`test/fixtures/harness_stream/` and parses them through its real decode +
read-model pipeline (`mission_stream_contract_fixture_test.dart`). Verified
2026-08-22: the two `MANIFEST.sha256` files are identical. Seven are generated
by `scripts/generate_agent_runtime_stream_fixtures.py` from a **seeded isolated
runtime root** through the current production builders, never from a live store;
the rest are hand-maintained and validated by shape + live-classifier agreement
(`tests/agent_runtime/test_stream_patch.py`) because they demonstrate fold
semantics over entities the seeded root does not contain. `patch_remove.json` is
un-emittable today — the `incident.closed` remove fold, de-registered with its
last writer, kept classifying by
`patch_coverage.HISTORICAL_COVERED_DOMAIN_EVENT_TYPES` (`:204`).

**Update rule: these fixtures change only in a cross-stack change that lands
hermes AND the launcher together** — regenerate, copy bytes, update both
manifests in the same change. Membership AND ORDER are compared before bytes by
the launcher's `tool/test_quality/check_producer_contracts.py` (that checker
lives in the LAUNCHER repo, invoked with `--hermes-root`), so a new row goes in
at the same position on both sides.

**The additive-only wire rule — why `sections_ms`-style keys must never ride the
parity envelope.** `sections_ms` rides the parity envelope, which rides the
hydrate frame, which is byte-pinned by these goldens AND by the launcher's
mirror. Two keys there for an observability nicety would be a cross-stack
fixture landing, and a hermes-only half of one is exactly the failure this repo
already paid for: hermes green, the launcher's byte-compare red on every push.
So the readiness-split attribution went to a LOG RECEIPT (`sections_top` on
`snapshot_build_core`) and `agents_readiness` kept its exact span, meaning and
key (`agent_runtime/snapshot.py:824-837`). The same discipline keeps
`correlation_id` an UNREGISTERED optional payload key — the contract hash
derives from the registry rows alone, so registering it would shift
`decision_contract_hash` inside every generated core.

## 9. Delivery directives — a name that no longer describes a transport

`agent_runtime/delivery_directive.py` is 222 lines with no wire surface. Its
transport-shaped half — a directive DECLARED on a goal-create request, stored on
the `Task`, executed at terminal settle by `ArchiveStore.archive_tasks` — went
with the mission lane; S24 swept the executors, the declaration path, and the
delivery-time patch capture that had no producer left. What remains is
`reap_orphan_worktrees` (`:38`), a capture-then-reap janitor driven by
`hermes harness worktree reap` and `harness doctor --fix`. Nothing is deleted
with an uncaptured diff: dirty candidates go to `<store_root>/wt_reaped_patches/`
under collision-proof exclusive-create names (`:162`). One registered event,
`worktree.orphans_reaped`.

## 10. MCP transport

MCP tool registration (`tools.mcp_tool.discover_mcp_tools`) is wired to the CLI
entry points that host a model turn against the operator's full MCP surface —
bare `hermes` / `chat` / `acp` / `rl`, plus `cron run|tick`, `gateway run`,
`mcp serve`. **`hermes harness …` is deliberately excluded and must stay
excluded**: the harness lane is a fast, deterministic control plane and cannot
pay an MCP connect budget on every command (`agent_runtime/mcp_lane.py:1-20`).
Admission is therefore per-RUN and per-PERSONA via
`register_mcp_servers({name: cfg})` over an explicitly resolved subset, and two
transport facts follow. Registration is **single-flight**, because
`tools.registry` and `tools/mcp_tool._servers` are process-global while a serve
process is multi-persona (`ThreadPoolExecutor(4)`) — interleaved admissions are
refused as `mcp_admission_lane_busy` rather than raced. And **the registry scope
belongs to the run while the transport belongs to the process**:
`teardown_mcp_admission` removes the admitted tools at every admitted run's end
while the connection in `tools/mcp_tool._servers` stays warm for the next
(`agent_runtime/mcp_admission.py:38-52`). Admission POLICY belongs to the
chat-lane doc. Relay hops are gated by `agent_runtime/relay_policy.py` — one
authority, so in-process tool relay, CLI and serve transport get the same depth
/ cycle / budget answer from explicit envelope data rather than inference.

## Invariants

1. **Parse per line, branch on `type`, ignore unknown fields.** Hub-vs-argv
   parity is a property of the DECODED frame, not the line — `harness stream`
   writes compact separators, `_FrameWriter` json's defaults
   (`test_serve_stream_lane_parity.py`).
2. **Every wire addition is additive**; byte-pinned frames move cross-stack.
3. **Observability never adds a key to a BYTE-PINNED surface** — an operator's
   number belongs on an `agent.log` line, not on the parity envelope the stream
   goldens and the launcher's mirror compare byte-for-byte. Additive keys on
   surfaces that are NOT byte-pinned are the deliberate exception: `phases` on
   the create RPC's result and on the turn record are both client-visible and
   both allowed (`agent_create_phases.py:14-21`).
4. **Absence and emptiness are different statements** — for `fold_entities`, for
   `watermark.event_offset`, for a heartbeat with no position. Collapsing either
   pair has cost a live incident each time.
5. **A frame that advances a watermark carries the state that justifies it.** No
   empty `patches` list, ever.
6. **A shared producer promotes on the INTERSECTION of its room's declarations**
   and echoes what it accepted; widening it may only name entities every
   fielded client already folds.
7. **The harness lane never registers MCP globally.** Admission is per-run,
   single-flight, torn down at run end.
8. **`agent_runtime/` is fork-only.** Any edit outside
   `agent/ | agent_runtime/ | hermes_cli/harness*` needs a boundary-ledger row.

## Open rows

- **Single-transport collapse is half-done** — the hub lane is primary and
  gated; the argv backstop's deletion waits on an observation window
  ([planned/single-transport-collapse.md](planned/single-transport-collapse.md)).
- **Correlation-id coverage is partial** — the office lane and
  `runtime.agent.create` carry the token, the remaining argv capability lanes do
  not, and the one-grep acceptance was never scripted
  ([planned/correlation-id-coverage.md](planned/correlation-id-coverage.md)).
- **`serve.py:105` says the RPC registry holds "currently eight" methods; nine
  are registered.** That same docstring warns "a docstring that copies a
  register starts lying the first time the register moves" — it has.
- **Two module docstrings cite doc paths that moved into `archive/`** —
  `delivery_directive.py:8` → `delivery-directive.md`, `serve.py:9` →
  `harness-serve-design.md`. Both pointers are dangling.
- **`tests/fixtures/stream_frames/README.md` describes thirteen of its fourteen
  pinned files** — `patch_office_actor.json` has no row in the origin table.

## Unverified carry-forward

- **The persona-chat event lane's two 2026-08-09 properties** — that the
  auto-title now runs AFTER the chat-root lease releases (so
  `persona_chat.projected` and `persona_chat.metadata_updated` for one turn are
  separated by a whole auxiliary-LLM round trip, and the root accepts a send in
  between), and that `persona_chat.send_refused` omits the message text because
  the sanitising chokepoints sit inside the lease the refusal never took. All
  three types are registered (`decision_contract_registry.py:184-196`) and
  emitted from `persona_commands.py` (`:1593`, `:1640`, `:1727`), but the
  ORDERING claim and the omission RATIONALE were not re-verified against the
  lease code. Source: `…/mission-control-stream.md:361-383`.
- **The measured patch-lane saving** — 486 bytes against an 822,671-byte core,
  the ~99.96% reduction the S6/S7 acceptance names. Cited at
  `patch_coverage.py:347` and `stream.py:459-462` as 2026-07/08 measurements;
  not re-measured here, so a historical figure, not a benchmark.

## Supersedes — all under `archive/2026-08-22-pre-consolidation/`

| Archived doc | Where it went |
|---|---|
| [`mission-control-stream.md`](archive/2026-08-22-pre-consolidation/mission-control-stream.md) | primary source for §3–§5 |
| [`harness-serve-design.md`](archive/2026-08-22-pre-consolidation/harness-serve-design.md) | the 2026-07-08 settled design behind §1 |
| [`delivery-directive.md`](archive/2026-08-22-pre-consolidation/delivery-directive.md) | removed contract; §9 is the live half |
| [`17-upstream-boundary-ledger.md`](archive/2026-08-22-pre-consolidation/17-upstream-boundary-ledger.md) · [`18-upstream-merge-rehearsal-20260730.md`](archive/2026-08-22-pre-consolidation/18-upstream-merge-rehearsal-20260730.md) | summarised in §7; still the authority for their own detail |
| [`SINGLE_TRANSPORT_COLLAPSE_PLAN_2026-08-16.md`](archive/2026-08-22-pre-consolidation/SINGLE_TRANSPORT_COLLAPSE_PLAN_2026-08-16.md) | landed stages → §2/§5; open half → `planned/single-transport-collapse.md` |
| [`CORRELATION_ID_PLAN_2026-08-16.md`](archive/2026-08-22-pre-consolidation/CORRELATION_ID_PLAN_2026-08-16.md) | landed stages → §8; open half → `planned/correlation-id-coverage.md` |
