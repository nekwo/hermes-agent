# `runtime.agent.create` — one call places an agent (Plan A, 2026-08-16)

> **Home.** Hermes repo, beside `OFFICE_GESTURE_FOLD_PROMOTION_PLAN_2026-08-16.md`,
> whose §10.3 item 3 is the ask this plan answers. Format follows that plan (the
> house S-stage format). Companion plans written the same night: B
> (`SCOPED_INVALIDATION_PLAN_2026-08-16.md`), C
> (`SINGLE_TRANSPORT_COLLAPSE_PLAN_2026-08-16.md`), D
> (`CORRELATION_ID_PLAN_2026-08-16.md`).

**Evidence tags used throughout** (the fold plan's discipline, §10.2: an
unmarked assumption stated as a fact is how that plan got two claims wrong):

- **READ** — file:line inspected this session.
- **RAN** — command/grep executed this session.
- **MEASURED-§10** — a number inherited from the fold plan's §10.1 live
  measurements, not re-measured here.
- **RELAYED** — operator/coordinator statement not recorded on disk. The
  ruling/incident/task register (#10, #37, #40, #42, #43) exists in NEITHER
  repo nor the live board store (RAN: exhaustive grep of both repos and
  read-only listing of `X:/Eternia/.hermes/agent-runtime/boards/`). Citations
  to those numbers are relayed text, and recording them on disk is named as a
  work item in Plan C §7.
- **ASSUMPTION A-n** — unverified; stage AC-0 verifies before anything builds
  on it.

**Verdict up front.** The operator ruling (RELAYED, verbatim): *"creating chat
shouldn't be a separate thing the launcher calls, it should just ask for new
agent persona in xy position."* Validation confirms the launcher today
SEQUENCES two writes over two different transports with independent failure,
independent retry, and a 600 ms debounce between them — and that both
half-states (instance without placement, placement without instance) are
reachable from the current code, not just from incident lore. The right shape
is one JSON-RPC method beside the four existing `runtime.office.*` methods,
whose handler composes the two EXISTING store chokepoints in a fixed order
with a recorded-progress idempotency reservation, so a replay resumes instead
of duplicating and a failure compensates instead of stranding. **This plan
does NOT make the create foldable.** The unified call emits the same events
the two-call flow emits today; until D3 lands (in flight, another agent), the
batch still carries a `persona_instance` `refresh` and still demotes to a
full core. The ~6.5 s stays. This plan is bought for correctness,
determinism, and measurability — exactly as §10.3 item 3 prices it.

## 0. The ask

Fold plan §10.3 item 3: one call, `{persona, workspace_id, position:[x,y]}`,
returning the placed actor. Kills the coalescing lottery (one call → the two
emissions land ms apart inside one drain window → one batch by construction),
makes the half-created agent unrepresentable to the client (incident #37,
RELAYED), retires the launcher's refuse-early drop branch by construction,
and creates the phase-timing envelope that per-gesture analytics needs.

## 1. Baseline — what a create actually is today (all READ unless noted)

Launcher paths, `lib/features/mission_control/mission_control_page.dart`:

1. **Palette drop of an already-placed persona** — `_addDroppedAgentInstance`
   (`:2364-2394`): first `_addAdditionalAgentPlacement` (`:2492-2584`) mints a
   client-side placement id (`missionMintDeliberatePlacementId`, `:2506`),
   writes the scene item via `_mutateMissionOfficeLayout` (`:2536-2546`), then
   awaits `_submitIntent(_createPersonaChatIntent(..., addInstance: true,
   placementId, workspaceId, realmId))` (`:2385-2394`).
2. **The intent** (`:4507-4536`) is capability `persona.instance.create`,
   target = the persona, args carrying `placement_id` / `workspace_id` /
   `realm_id` / `display_name`, idempotency key
   `new-placement-<persona>-<micros>`. The bridge lowers it to argv
   `harness persona instance create --placement-id ... --workspace-id ...
   --realm-id ... --display-name ... --json`
   (`mission_control_bridge.dart:~3285-3324`), run serve-first with CLI
   fallback (`runMissionControlCommandPreferServe`,
   `mission_control_serve_session_io.dart:1685-1727`).
3. **The placement's DURABLE write is a different lane entirely**: the layout
   mutation stages, and a **600 ms debounce** flushes it to
   `runtime.office.upsert` RPC-first with `harness office actor-upsert` CLI
   fallback (`mission_office_layout_controller.dart:63-67` — the constant and
   its own comment calling the 600 ms "dead time"; `:629-639` RPC-first;
   `:229` the fallback counter; bridge argv at `:3842-3856`).
4. **Roster-only create** (`_createAgentFromTemplate`, `:3285-3359`) mints an
   instance with NO placement — legitimate, and stays a separate door.

So one gesture = one awaited argv-lane write (instance + chat root) plus one
debounced RPC-lane write (placement), in that durable order, with nothing
joining them. Consequences, each verifiable in the code above:

- **Half-state 1 — instance without placement (incident #37's shape,
  RELAYED).** The intent is awaited and durable; the placement flushes ≥600 ms
  later and can be refused (`stale_revision`, `sync_conflict`,
  `class_key_collision`, park — `serve_rpc.py:930-954` READ;
  `mission_office_subscribe_lane.dart` park path) or lost with the process.
  Nothing retires the instance.
- **Half-state 2 — placement without instance.** If the intent fails after the
  layout staged, the debounce still flushes an actor whose
  `persona_instance_id` names an instance the harness never minted. The office
  store validates payload shape and class keys, not instance existence
  (`serve_rpc.py:883-921`, `office_class_key_guard` READ) — the write lands.
  This is the "no-instance-to-thread" state. Note: `_addDistinctPlacement`
  itself NO LONGER EXISTS — it was already replaced by an out-loud refusal
  (`mission_control_page.dart:2421-2460` READ), whose own comment names
  `runtime.agent.create` (task #10, RELAYED numbering) as what retires the
  refusal by construction. The fold plan §10.3 item 3 cites
  `_addDistinctPlacement` as a live problem; that is stale — the live problem
  is the refuse-early branch plus half-state 2.
- **The coalescing lottery** (§10.2(a), corrected): the two halves land
  ~356 ms apart and usually COALESCE into one batch (the drain's 200 ms settle
  is a join window — `stream.py:506-511` READ). "Usually" is the problem: the
  gap is client-debounce-plus-two-transports wide and nothing pins it.
- **Cost** (MEASURED-§10): create demotes → 6.94 s to resync; the demote is
  D3's to fix, not this plan's.

Server surface today (READ): exactly four JSON-RPC methods exist —
`runtime.office.get` / `.subscribe` / `.unsubscribe` / `.upsert`
(`serve_rpc.py:360,485,708,769`; RAN grep). The method table is a decorator
registry (`:135,228-233`) and `manifest()` advertises the method list on
`ready`/`hello_ok`/`version` (`:240-249`) — which gives the launcher
capability detection for free (`mission_runtime_rpc_manifest.dart:78-81`,
evidence sweep this session).

Idempotency precedent (READ): `reserve_persona_chat_mint`
(`persona_chat_mints.py:63-128`) — a keyed, stateful reservation taken
instance-first under the global lock order, exists precisely so "a launcher
timeout/retry can recover the same root instead of creating a duplicate
conversation". Doc 13's recorded debt says the un-deduped create "gets the
same treatment when it bites" (`13-write-path-intent-integrity.md:63-66`
READ). It has bitten.

## 2. Validation

**V1 — Where does the call live, and how does an old pair degrade?** A fifth
method in `serve_rpc.py`, advertised by `manifest()`. Old launcher + new
runtime: never calls it (argv path byte-identical). New launcher + old
runtime: the method is absent from the advertised manifest → the launcher
keeps the two-call path (fail-open, same discipline as the office RPC's
`laneAbsent` degrade, `mission_office_rpc.dart:124,441` READ). No pair needs
a simultaneous deploy.

**V2 — What does the handler call?** The two chokepoints the two lanes
already hit — nothing new is derived:

- instance + chat mint: whatever `harness persona instance create` calls
  today in `persona_assignments.py` (the `open_chat`/create family,
  `:1402-1561` READ via the fold plan's V2; the file is currently owned by
  the D3 implementation agent). **ASSUMPTION A-1:** that logic is callable as
  a function with `(persona_id, placement_id, workspace_id, realm_id,
  display_name, idempotency_key)` and not welded to argparse. AC-0 verifies;
  if welded, AC-1 extracts a shared function — a change INSIDE
  `persona_assignments.py` that must be sequenced after the D3 agent lands
  (see §8 collision note).
- placement: `OfficeStore.upsert_actor` with the same actor payload the RPC
  upsert normalizes today (`serve_rpc.py:923-929` READ), position included
  (the store already rejects "an unparseable position", `:958`).

**V3 — Atomicity.** There is no cross-store transaction and this plan does
not invent one. Order: **instance first, placement second.** Placement-first
is rejected because a placement naming an unminted instance is half-state 2 —
the exact invented-binding the launcher codec refuses to derive
(`mission_control_page.dart:2429-2435` READ). On placement failure the
handler **compensates**: retire the just-minted instance through the same
retire chokepoint the delete gesture uses (`persona_assignments.py:1216-1224`
READ via fold plan) and return a typed error naming the failed phase. The
client never sees a success that isn't both rows.

**V4 — Idempotency: resume, not refuse.** The reservation records progress
(`instance_minted` → `actor_written` → `done`), following the mint ledger's
stateful precedent (`persona_chat_mints.py:137-138` READ shows a state
vocabulary already exists there). A replay with the same `idempotency_key`:
finds `done` → returns the same reply; finds `instance_minted` → skips the
mint, performs the placement, completes. A crash between the two writes
therefore converges on retry instead of stranding half-state 1. This is the
strongest anti-#37 property in the plan, and it is also the answer to the
compensation-itself-crashed case (§6).

**V5 — Batch determinism.** Both emissions happen inside one handler,
milliseconds apart. The producer drains, then sleeps 200 ms and re-drains
into the SAME pending batch (`stream.py:484-533` READ) — for the halves to
split, the second append would have to land >200 ms after the settle began.
One batch by construction, not by lottery. (Still a DEMOTED batch until D3 —
stated in the verdict, restated in §7 "does not fix".)

**V6 — The reply and the phase envelope.** Result:
`{persona_instance_id, actor_key, revision, workspace_id,
default_chat_session_id, phases: {instance_ms, placement_ms, total_ms}}` plus
an echo of `correlation_id` when Plan D lands (additive optional either way).
`actor_key`/`revision` mirror `runtime.office.upsert`'s light ack
(`serve_rpc.py:781` READ) so the launcher's existing prediction/`
expect_revision` bookkeeping keeps working unchanged.

**V7 — Events are byte-identical to the two-call flow.** The anti-goal: no
new event types, no payload changes, no coverage change, no contract-hash
move. A parity test pins it (AC-1 tests). This is also what makes the plan
COMPOSE with D3 rather than collide: D3 changes what the chat-open chokepoint
emits; this handler calls that chokepoint, so it inherits D3's producer
change automatically, whichever lands first.

## 3. Target architecture (one paragraph)

One gesture → one JSON-RPC call → one handler that reserves an idempotency
record, mints the instance+chat at the existing chokepoint, writes the
placement at the existing chokepoint, compensates or resumes across the seam,
and replies with both rows and a phase-timing envelope. The launcher stops
knowing a chat exists; its two-call path survives only as the fail-open
degrade against runtimes that do not advertise the method, and the durable
order/failure matrix moves from "two client lanes and a debounce" to "one
server function under test".

## 4. Stages

### AC-0 — verify the assumptions this plan stands on (hermes, read-only)

**Goal.** No unmarked assumptions survive into AC-1.

- Verify A-1 (create logic importable) by reading the
  `harness persona instance create` handler; record the exact function
  signature in this doc's §9 table.
- Verify what `--placement-id` does server-side (instance id =
  `personainst_<placementId>`? — the launcher assumes exactly that at
  `mission_control_page.dart:2507,4539`), and that the office actor key the
  launcher writes matches it.
- Verify the mint-lock timeout bound so the RPC handler cannot hang a serve
  worker (`mint_lock_unavailable` exists, `persona_chat_mints.py:109-113`
  READ; the bound itself unverified).
- Decide the reservation store location (beside the chat-mint ledger; the
  board store's `idempotency/` dir is a second precedent — RAN, live-root
  listing).

**Does NOT do.** Touch anything. Output is an updated §9 table.

### AC-1 — hermes: the method, the reservation, the compensation

**Change surface.** `serve_rpc.py` (unowned by other agents — RAN against the
coordinator's owned-file list): `@method("runtime.agent.create")`; params
`{persona_id, workspace_id, position: [x, y], display_name?, placement_id?,
idempotency_key, correlation_id?}`. Handler order: validate → workspace
exists (mirror the upsert's refusal, `serve_rpc.py:874-881`) → reserve
(resume if the key is known) → mint instance+chat → build actor payload
(instance-keyed — class-key collision impossible by construction, guard still
run for defence) → `upsert_actor` → mark `done` → reply. On placement
failure: retire the minted instance, mark the reservation failed, return a
typed error `{reason: "placement_failed", phase, rolled_back: true}`. New
sibling module for the reservation ledger (new file — collides with nobody).

**Mixed pairs.** No caller exists yet; dead code against every launcher. The
argv lanes are untouched.

**Tests** (new `tests/agent_runtime/test_serve_rpc_agent_create.py`):
- `create returns instance + actor + phases and both rows are durable` —
  kill-mutation: skip either write.
- `event parity: the emitted event sequence equals the two-call flow's` —
  build both flows against seeded roots, compare event types + payloads
  (modulo ts/ids). Kill: emit anything extra. This test re-bases when D3
  lands; assert against the chokepoint's live output, not pinned bytes.
- `replay with the same key returns the same ids and writes nothing` — kill:
  drop the reservation.
- `crash after mint, replay completes the placement` (inject failure) — kill:
  make replay re-mint (duplicate instance) or refuse.
- `placement refusal retires the instance and reports rolled_back` — kill:
  leave the instance.
- `unknown workspace refused before any write` — kill: reorder validation.

**Rollback.** Revert; the reservation ledger is additive state no other
reader consults. **Perf.** None alone. **Deletes.** Nothing.
**Does NOT do.** Change any emitter, any coverage table, any fixture.

### AC-2 — launcher: call it, prove which lane ran, retire the refusal

**Change surface.** The drop/add-instance flows route through a new bridge
call when `manifest.supports('runtime.agent.create')`; otherwise the existing
two-call sequence runs unchanged. Receipts (via the named log sinks, never
`debugPrint`): `[MissionAgentCreate] lane=rpc|twoCall phases=... corr=...`.
The refuse-early branch (`mission_control_page.dart:2443-2460`) retires on
the RPC lane only — the server can mint from a bare persona, which is the
capability whose absence that refusal exists to survive; it stays for the
twoCall degrade. The optimistic pending row (`_upsertPendingCreatedAgent`)
stays — prediction is §10.3 item 4's territory, not this plan's.

**Mixed pairs.** Old runtime: manifest gate → twoCall, byte-identical wire.
New runtime: one call. **If AC-2 lands and AC-1 never does:** the gate never
opens; nothing breaks — the ordering rule this plan inherits from O-L2's
failure is "the caller gates on the server's advertisement, never on its own
release notes."

**Tests.** Bridge test pinning the params built from a drop (kill: drop
`position`); manifest-gate test (kill: call unconditionally); receipt-format
test referencing one shared lane-name constant.

**Rollback.** Revert; gate closes. **Perf.** Gesture-to-durable loses the
600 ms placement debounce and one transport round trip on the create path —
measure via the phases envelope. The 6.5 s demote is untouched.

### AC-3 — retire the two-call create path (both repos, gated)

Ruling #42's sequence (RELAYED): finish the main path, make fallbacks prove
they are dead, delete cheapest-first. Exit criteria: N operator sessions with
zero `lane=twoCall` receipts while serve was up. Then: launcher deletes the
create-path sequencing (the intent stays for roster-only creates;
`persona.instance.create` argv stays for serve-absent recovery per the
accepted cost "no serve no office" — RELAYED, and if the operator wants
create-with-placement to work serve-down, this stage is where that gets
decided out loud, decision D-A2). Hermes deletes nothing — the argv verbs
remain operator tools.

## 5. Platform facts

- Store chokepoints already emit everything (fold plan §3; the store/event
  invariant `stream.py:374-384` READ) — the handler adds orchestration only.
- The office lock and the instance store have no common lock; the handler
  holds them sequentially, never nested, so no new deadlock order exists.
- The serve dispatcher runs handlers on pool workers with a shared
  `handle_request` error boundary (`serve_rpc.py:311-347` READ) — a raising
  handler is a typed `-32000`, never a dead serve loop.

## 6. Adversarial pass

- **Compensation itself fails** (retire raises after placement refused): the
  reservation holds `instance_minted`+failed → the state is visible, and the
  replay path converges it (retries the placement or completes the retire).
  Worst case equals today's incident #37 state, now with a durable record
  naming it. Not eliminated — bounded and diagnosable. UNANSWERED: whether
  retire can itself be refused for a just-minted instance (guards on
  children/backlinks); AC-0 reads the retire path before AC-1 relies on it.
- **Two rapid drops of the same persona**: two keys, two placements — the
  "(2)" naming currently derives from the CLIENT's layout
  (`:2497-2516` READ). With server-side minting the distinct-name rule needs
  an authority: keep the client-supplied `display_name` (launcher still
  computes the suffix) — named decision D-A1, chosen for AC-1 to avoid
  moving naming authority in the same stage as transactionality.
- **The handler blocks a serve worker for the whole mint** (chat-root lease,
  provider install…): the 46 s title incident (`mission-control-stream.md:
  309-321` READ) is post-lease and NOT in this path, but the mint lock is.
  AC-0 pins the bound; if it can exceed a few seconds, AC-1 must return
  `phase=instance_pending` + resume-by-key rather than hold the worker.
  UNANSWERED until AC-0.
- **Replay after the launcher gave up** (fire, timeout, user re-clicks): a
  re-click mints a NEW idempotency key (the launcher stamps micros into keys,
  `:4534-4535` READ) → a second agent. Same behaviour as today's re-click;
  prediction/UX owns de-duplicating gestures, not this call.
- **Position semantics**: the store normalizes position and refuses garbage
  (`serve_rpc.py:955-960`); occupied-position/snap rules do not exist
  server-side today and this plan adds none (D-A1 note) — the canvas already
  tolerates overlap.
- **What this pass could not answer**: A-1 (importability), the retire-refusal
  question, and the mint-lock bound — all AC-0 items by name.

## 7. What this plan does NOT fix

- The demote: the create's batch still carries `persona_instance` `refresh`
  until D3; ~6.5 s of the 6.94 s remains (MEASURED-§10).
- The ~450 ms poll+settle floor; client prediction (item 4); the 600 ms drag
  debounce for MOVES (item 5 — this plan removes it for creates only); the
  page-open write storm (item 9); the `laneAbsent` boot window (item 10).

## 8. Standing constraints / collision map

Fork-owned files only. `serve_rpc.py`, `office_store.py`, new modules:
UNOWNED tonight (RAN against the coordinator's list). `persona_assignments.py`
is OWNED by the D3 agent: any A-1 extraction lands only after D3 merges, and
the event-parity test re-bases then. Python tests: 30 s cap, no `integration`
marker. Never write under `X:/Eternia/.hermes/`; no casual `harness serve`.
Additive params/replies only; `RPC_CONTRACT_VERSION` stays 1 (adding a method
does not move it — `serve_rpc.py:119-121` READ).

## 9. Verification log

| # | Fact | How established |
|---|---|---|
| A-R1 | Two-call sequence, order, debounce, both half-states reachable | READ mission_control_page.dart:2364-2584,4507-4536; mission_office_layout_controller.dart:63-67,629-639; serve_rpc.py:930-954 |
| A-R2 | `_addDistinctPlacement` already replaced by refuse-early citing this call | READ mission_control_page.dart:2421-2460 |
| A-R3 | Four RPC methods; decorator registry; manifest advertisement | READ serve_rpc.py:135,228-249,360-770; RAN grep |
| A-R4 | Settle window is a join window; in-handler emissions coalesce | READ stream.py:484-533; fold plan §10.2(a) |
| A-R5 | Keyed stateful mint reservation precedent | READ persona_chat_mints.py:63-138 |
| A-R6 | Create-dedup debt recorded 2026-07-09 | READ 13-write-path-intent-integrity.md:63-66 |
| A-R7 | Operator ruling text; incident #37; ruling #42 sequence | RELAYED (not on disk; RAN exhaustive grep) |
| A-R8 | Create demote cost 6.94 s | MEASURED-§10 |
| A-A1 | Create logic importable with the needed signature | ASSUMPTION — AC-0 |
