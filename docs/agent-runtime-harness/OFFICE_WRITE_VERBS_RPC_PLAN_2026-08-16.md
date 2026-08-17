# Office write verbs onto the RPC lane — remove, surface, occupied-chat create, resolve (Plan E, 2026-08-16)

> **Home.** Hermes repo, beside `OFFICE_GESTURE_FOLD_PROMOTION_PLAN_2026-08-16.md` (whose
> §10.4 register defines every `R#nn` cited here) and `AGENT_CREATE_ONE_CALL_PLAN_2026-08-16.md`
> (whose method/reservation shape this plan copies where it applies and deliberately does not
> copy where it does not). This work is **stage 1 of R#42's sequence** as
> `SINGLE_TRANSPORT_COLLAPSE_PLAN_2026-08-16.md` frames it: finish the main path first; the
> fallback deletions stay in that plan's observation window, not in this one.
>
> Repos as read: hermes `db73fe0b2a` (main), launcher `3188a540c` (main) — both verified
> (RAN, `git rev-parse`).

**Evidence tags** (the fold plan's discipline): **READ** (file:line inspected this session) ·
**RAN** (command/grep executed this session) · **MEASURED-§10** (a number inherited from the
fold plan's §10.1 live measurements, not re-measured here) · **RELAYED** (told to me, not on
disk) · **ASSUMPTION A-n** (unverified; WV-0 verifies before the stage that depends on it).

---

## 0. Verdict up front — including two corrections to the ask

The gap is real and exactly as measured: `agent_runtime/serve_rpc.py` registers five methods —
`runtime.agent.create`, `runtime.office.get`, `.subscribe`, `.unsubscribe`, `.upsert` (RAN,
grep re-run at `db73fe0b2a`) — while the launcher's Mission Office submits four `office.*`
capabilities (RAN, grep over `lib/features/mission_control/`): `office.actor.remove`
(`mission_office_layout_controller.dart:1002`), `office.actor.upsert` (`:776`, the argv
FALLBACK arm of the RPC-first upsert), `office.surface.update` (`:885`),
`office.resolve_conflict` (`:602`). Three of the four writes have no RPC method; each one
spawns a `hermes` process per flush through the capability/argv lane
(`mission_control_bridge.dart:3921-3972` READ). A fifth capability, `office.actor.restore`,
has a bridge lowering and a registry row but **no launcher submit site** (RAN, grep) — it is
operator-CLI recovery only and is deliberately not scoped.

**Correction 1 — the brief's rationale for the remove verb is stale.** The brief says
`runtime.office.remove` "is the verb R#43 needs in order to DELETE the absence-means-delete
inference in `_flush`… which retires the branch that archived four live actors in incident
R#40." That branch **no longer exists**. It was deleted — not guarded, deleted — at launcher
`7623f99cf` (2026-08-15, "a save's omissions were its delete list, so the delete list is now
something a caller says out loud"): `_flush` archives only `sync.requestedRemovals`, a set a
caller explicitly named via `officeVacatedActorKeys`, and `desired` is "purely a write list;
an actor missing from it is an actor this save has nothing to say about — never an actor to
delete" (`mission_office_layout_controller.dart:75-92,346-350,810-824` READ). What remains is
`missionOfficeMassArchiveTripped` — **demoted to a backstop on purpose**, with its own
docstring recording that deleting it alongside the inference was considered and rejected
(`:85-92` READ). This plan does not delete it and recommends nobody does: the tripwire is now
a cheap fence on the *explicit* lane, and the proof obligation the brief asks for
("prove the two agree, then delete") belongs to **UP-4** of
`UNIFIED_GESTURE_PREDICTION_PLAN_2026-08-16.md` — the dual-reconciler agreement window that
gates deleting anything once the intent ledger exists — not to this transport change. §2.1
states what the remove RPC actually buys, which is still worth building first.

**Correction 2 — the surface fold half partially reverses a recorded verdict, and says so.**
The fold plan's V1 rejected a separate surface fold entity — correctly, *for the gesture
path*, where nothing the two actor gestures move needs a second wire row. A **folder change**
is different: it moves `folders` and the surface's own `revision`, which are exactly the two
fields V1's table marks "read (canvas); only `update_surface` moves it — stays full-core
(rare operator action)" (`OFFICE_GESTURE_FOLD_PROMOTION_PLAN…` §V1 READ). §10.1 then measured
that "rare" was wrong in practice: the surface write fires on every folder change AND on page
open, its event is uncoverable by design (`patch_coverage.py:223-231` READ), and one surface
write demotes the whole boot batch to a 5.93 s full core (MEASURED-§10). So this plan stages a
**minimal `office_surface` fold entity** — a subset-merge of `{folders, revision, updated_at}`
onto the office row the client already holds, behind a third capability token — and records it
as a conscious narrowing of V1's verdict, not a contradiction of it: the entity V1 rejected
carried the ledger (no reader); this one carries folders (read by the canvas).

Priority order is the brief's, unchanged: remove first, surface second (both halves),
occupied-chat create third, resolve-conflict last.

## 1. Baseline (all READ at the SHAs above unless tagged otherwise)

**The write chokepoint.** All four verbs already flow through ONE launcher file,
`mission_office_layout_controller.dart` — **qualified 2026-08-17 by Plan EG §4.8 (EG-0.2
receipts): one launcher file, NOT one transport; `resolve_conflict` has no RPC method
(RD Correction 4, EG-4.5 owns it):**

- **Remove**: `_archiveActor` (`:997-1011`) — a method, not inline lines, and its own
  docstring names itself "THE seam a `runtime.office.remove` drops into", records that no
  such method exists as of 2026-08-15, and pre-decides the shape: "when the RPC method lands
  it takes the same three-arm shape the upsert already has: OK / REFUSED (terminal) /
  UNAVAILABLE (the only arm the argv lane may cover)". It also names the one carry-over: the
  argv verb accepts `--expect-revision` and this call sends none, so archives are UNGUARDED
  today (`:989-996`).
- **Upsert**: RPC-first with three-arm outcome, argv fallback only on UNAVAILABLE, revision
  prediction + `rolledBack` reconciliation, per-flush lane receipts
  (`:692-808`, `missionOfficePredictedRevision` `:136-142`).
- **Surface**: the folder-taxonomy branch of `_flush` (`:882-902`) — capability
  `office.surface.update`, args `{workspace_id, folders: joined-with-commas}`, no
  `expect_revision` sent; on accept it copies `desiredFolders` into `serverFolders` locally.
- **Resolve**: `resolveConflict` (`:595-617`) — capability submit + immediate refresh; one
  caller, `mission_office_sync_strip.dart:320`.

**The argv lowerings** (`mission_control_bridge.dart` READ): `office.actor.remove` →
`harness office actor-remove --workspace … --actor … [--expect-revision …] --json` (`:3921`);
`office.surface.update` → `harness office set-folders --workspace … --folders … --json`
(`:3946`); `office.resolve_conflict` → `harness office resolve-conflict … --take … --json`
(`:3961`). Each spawn is a fresh `hermes` process per changed item per flush.

**The hermes store chokepoints** (`agent_runtime/office_store.py` READ):

- `remove_actor(workspace_id, actor_key, *, reason, updated_by, expect_revision, dry_run)`
  (`:469-502`): idempotent — an already-archived key returns the archived copy without
  writing; a key that is neither live nor archived raises `NotFound`; `_check_revision`
  raises `StaleRevision`; the archive path (`_archive_actor_locked`) already emits the paired
  `office_actor` remove patch inside the lock (O-H1, landed) plus `office.actor.removed`,
  which is COVERED since O-H3 (`patch_coverage.py:236-239` READ). **A remove RPC therefore
  needs no producer, coverage, or fixture change at all** — the wire under it is done.
- `update_surface(workspace_id, *, folders, updated_by, expect_revision, dry_run)`
  (`:298-341`): normalizes folders (`_normalize_folders` — always prepends
  `DEFAULT_FOLDERS`, dedupes, caps at `MAX_FOLDERS = 64`, `:51,135-144`), bumps the surface
  `revision`, emits `office.surface.updated {workspace_id, change, revision}` (registered
  contract, `decision_contract_registry.py:265` READ) — currently uncovered → every batch
  carrying it demotes. Note: `update_surface` calls `ensure_surface` and would lazily author
  an office for an unknown workspace, which is right for the CLI and wrong for the wire —
  the exact incoherence `runtime.office.upsert`'s docstring rules on
  (`serve_rpc.py:784-795` READ).
- `resolve_conflict(workspace_id, actor_key, *, take, updated_by, allow_class_key, dry_run)`
  (`:533-…`): returns `OfficeActor | None` (None = local archived for an edit-vs-remove
  tombstone); `--take remote` is fenced by `_guard_class_keyed_adoption`
  (`ClassKeyedPlacementRefused`); its event `office.actor.conflict_resolved` is registered
  (`decision_contract_registry.py:269`) and deliberately uncovered — stays so.
- The CLI verbs above these are THIN (`hermes_cli/harness_parts/office.py:239-331` READ):
  unlike `agent-create` (whose CLI held the honest-display-name rule that AC-0 had to hoist),
  `actor-remove` and `set-folders` add **no policy** above the store — no extraction step is
  needed. `resolve-conflict` adds one translation (ClassKeyedPlacementRefused →
  `duplicate_conflict` exit) which the RPC handler reproduces as a typed 4090.

**The RPC plumbing to copy** (READ): the decorator registry + `manifest()` advertisement
means a new method is capability-detectable for free (`serve_rpc.py:228-249`);
`RPC_CONTRACT_VERSION` stays 1 — "adding a method does not move it" (`:119-121`). Launcher
side: `missionOfficeRpcRefusal(manifest, method: …)` gates per-method
(`mission_office_rpc.dart:120-132`), and `MissionOfficeRpcWriter` (`mission_office_rpc_writer.dart`
READ in full) is the gate-ladder template — `enabled → transport → manifest(method) → local
param sanity → call → parse`, nothing throws, three-arm result.

**The occupied-chat two-call dance** (`mission_control_page.dart` READ):
`_resolvePersonaChatActivationPlan` → `_OccupiedChatResolution.addInstance` →
`_addAdditionalAgentPlacement(instance:)` (stages the scene item optimistically at a
policy-chosen slot, mints the placement id) → returns a plan `{addInstance: true,
placementId, placementDisplayName}` (`:2263-2276`); the plan is consumed by TWO awaited
`_submitIntent(_createPersonaChatIntent(… addInstance: plan.addInstance …))` call sites —
`_startPersonaChat` (`:1965-1974`) and `_openPersonaChat` (`:2137-2147`) — whose acks flow
through `MissionChatAckAdoption.resolve` and a console-selection rollback block
(`:1988-2067`). The RPC lane exists but routes ONLY the palette drop:
`_createDroppedAgentOverRpc` (`:2448-2520`), receipts `[MissionAgentCreate]
lane=rpc|twoCall gesture=drop`, adoption via `_adoptServerPlacement` (`:2530-2549`) which
seeds `adoptServerWrite` so the staged debounce does not double-write. The
`runtime.agent.create` handler returns everything the occupied-chat flow needs —
`persona_instance_id`, `default_chat_session_id`, `actor_key`, `revision`, `phases`
(`serve_rpc.py:1249-1267` READ) — and the client/codec
(`mission_agent_create_client.dart`, `mission_agent_create_rpc.dart`) is gesture-agnostic.

**The paint-path contract this plan must not disturb** (READ, per the brief's standing rule):
`isSettled(workspaceId)` = `!writesInFlight` where `writesInFlight = timer != null ||
flushing || any pending overlay entry` (`mission_office_layout_controller.dart:406-441`);
the page's retire condition additionally holds a page-side pending-save term since OR-1
(launcher `7426d2403`, merged as `3188a540c`); `applyLayout` stages sync BEFORE its two
view-store awaits (OR-2, `:470-517`); the OR-4 fence is
`mission_office_optimistic_paint_test.dart` ("every office door paints on the next frame"),
written to name no predicate, and must stay green byte-unchanged through every stage here.

**Costs inherited, not re-measured** (MEASURED-§10): delete gesture folds in 280–368 ms but
its write spawns a process; boot flush (11 actors + surface write) demotes to a 5.93 s full
core; `build_snapshot()` is ~1.17 s warm / ~6.9 s cold on X:; a serve RPC round trip is
~8 ms; the argv fallback resubscribe lag on the flush receipt is 250–650 ms.

## 2. Validation

### 2.1 — What each verb's RPC buys, honestly

| Verb | Buys | Does NOT buy |
|---|---|---|
| `runtime.office.remove` | (a) no process spawn on the delete gesture's write leg — the last per-gesture spawn on the office hot path; (b) a typed three-arm outcome, so a refused archive is TERMINAL (today a store refusal and a spawn failure are both just `status != accepted`, and the argv lane would be "a second chance" at a guard — the exact hazard the upsert arm documents at `:752-764`); (c) the seam for a GUARDED archive later (`expect_revision` — D-W1, not in the first cut, §6); (d) `lane=rpc|cli` receipts joining the R#42 evidence stream. | Deleting any inference (already gone — §0 correction 1); the fold (deletes already promote); the mass-archive backstop's retirement (UP-4's, and recommended kept regardless). |
| `runtime.office.surface.update` (RPC half) | No spawn per folder change; `expect_revision` guard becomes sendable (the argv site never sends one); the reply echoes the NORMALIZED folder list, which closes a latent rewrite loop — today `_flush` copies `desiredFolders` into `serverFolders` on accept (`:895-897`), so a store-side normalization difference (dedupe, 64-cap, default-folder prepend) leaves the two permanently disagreeing and the branch re-firing on every subsequent flush. | The demote. An RPC write emits the same `office.surface.updated`, the batch still sinks to a full core. That is the fold half's job. |
| `office_surface` fold (fold half) | The batch carrying a folder change (or the boot flush's surface write) stops demoting — the ~6 s `build_snapshot()` leaves that path on BOTH producers, same mechanics as O-H3's win. | The page-open write storm itself (item 9, unowned): the fold makes the boot batch's demote cheap; it does not stop the launcher writing on page open. It also does not promote batches that carry OTHER uncovered events (A-1, §7). |
| occupied-chat `runtime.agent.create` | The two-call dance's half-states (R#37's shape) become unrepresentable on this path too; the placement stops waiting out the 600 ms debounce; one batch by construction; `lane=twoCall gesture=addInstance` receipts finally distinguishable from the drop's. | Speed of the create itself (post-D3 it is already ~1 s cold); prediction (UP-3). |
| `runtime.office.resolve_conflict` | No spawn; typed reasons (`class_key_collision` vs `stale`-class errors vs not-found) instead of message strings; the strip's refresh request keeps working unchanged. | The fold — `office.actor.conflict_resolved` stays uncovered on purpose (rare, adopts peer rows past the upsert chokepoint); resolves still ride a full core. |

### 2.2 — Method shapes (the contract, so implementation does not re-derive it)

All three follow `runtime.office.upsert`'s conventions exactly (READ `serve_rpc.py:769-969`):
`workspace_id` validated first with the same reason strings; **unknown workspace refused with
`4001 workspace_not_found` BEFORE any store call** — for `surface.update` this deliberately
overrides the store's lazy `ensure_surface`, on the same reasoning the upsert docstring
records (a wire write must not author a surface for a typo the read lane refuses);
store exceptions translated, never re-implemented; light acks; additive params only;
`RPC_CONTRACT_VERSION` stays 1.

**`runtime.office.remove`** — params `{workspace_id, actor_key, expect_revision?,
updated_by?, reason?}` (reason defaults `"operator"`, same as the CLI). Reply
`{actor_key, revision, state: "archived"}` — the store's post-archive revision, mirroring the
upsert's light ack so the controller's `serverRevisions` bookkeeping can adopt it. Errors:
`NotFound` → `4001 {reason: "actor_not_found"}` (distinct from `workspace_not_found`);
`StaleRevision` → `4090 {reason: "stale_revision"}` (no current revision in `data`, same
rule as the upsert `:930-945`); `ValueError` → `-32602 {reason: "actor_invalid"}`. The
idempotent already-archived arm is an `ok` (the store returns the archived copy) — a repeat
remove is a clean success, matching the fold's own remove-absent no-op. No class-key guard
(archives cannot resurrect anything).

**`runtime.office.surface.update`** — params `{workspace_id, folders: [string,…],
expect_revision?, updated_by?}`. `folders` is a LIST on the wire (typed), not the capability
lane's comma-joined string — the handler passes it to `update_surface(folders=…)` which
normalizes; the comma encoding stays an argv-lane artifact. Reply `{workspace_id, folders:
<normalized list>, revision}` — the echo is load-bearing (§2.1's rewrite loop). Errors:
`workspace_not_found` (checked via `surface_exists` before the store's ensure);
`StaleRevision` → `4090 stale_revision`; `ValueError` → `-32602 folders_invalid`.

**`runtime.office.resolve_conflict`** — params `{workspace_id, actor_key,
take: "local"|"remote", updated_by?}`. **No `allow_class_key` param, and that asymmetry is
copied deliberately from the upsert's ruling** (`serve_rpc.py:807-825`): a wire parameter is
not consent; the CLI keeps the flag. Reply: `{actor_key, take, state: "active"|"archived",
revision?}` — `revision` present iff the store returned an actor (the None arm is the
edit-vs-remove tombstone, `state: "archived"`, no revision). Errors:
`ClassKeyedPlacementRefused` → `4090 {reason: "class_key_collision"}`; `NotFound` → `4001
{reason: "conflict_not_found"}`; invalid `take` → `-32602 {reason: "take_invalid"}` checked
in the handler (the store would ValueError; the typed reason is cheaper client-side).

None of these needs a reservation ledger. The agent-create plan's reservation exists because
that handler composes TWO stores across a seam; each method here is one store call under one
`office_lock`, already idempotent (remove) or guarded (revision), and a transport retry
re-running it converges. Copying the reservation machinery here would be over-building.

### 2.3 — The `office_surface` fold entity (the one wire change in this plan)

Producer (`agent_runtime/state_patches.py` + `office_store.py`): `update_surface` emits, inside
the lock and before the domain event (the O-H1 pattern), an `office_surface` patch — entity
`office_surface`, id `workspace_id`, op `upsert`, `changed = {folders, revision, updated_at}`.
Complete for what it claims to be: a SUBSET-merge row, like `persona_instance`'s, not a
complete-row replace like `office_actor`'s — because the office row also carries actor lists,
derived counts, ledger and conflict keys that this write does not move and must not clobber.
Tiny by construction: folders are ≤64 safe-id strings ≤120 chars (`office_store.py:51-59,142`),
far inside the 3,584-byte value budget.

Coverage: `office.surface.updated` joins `LIVE_COVERED_DOMAIN_EVENT_TYPES` **gated on a third
capability token `office_surface_fold`** — same mechanism, same rationale as
`OFFICE_ACTOR_LIFECYCLE_CAPABILITY` and `PERSONA_INSTANCE_CREATE_CAPABILITY`
(`patch_coverage.py:101-148` READ): the token gates the widened coverage so an undeclared
client keeps today's full cores; a declared client folds. `office.surface.created` stays
uncovered (a create authors a surface the client has never seen — and it is emitted by lazy
`ensure_surface` inside other writes, so covering it would need a pairing audit this plan does
not want). `.restored` / `.conflict_resolved` stay uncovered with their original reasoning
intact (`patch_coverage.py:223-231`) — the rewritten comment block must keep saying so.

Client fold (`mission_read_model.dart`): a new `_applyOfficeSurfacePatch` beside
`_applyOfficeActorPatch` — find the office row by `workspace_id`; absent →
`patch_without_target` (resync; no insert-on-absent: a surface the client has never seen has
no honest base); present → merge exactly the three keys. It does NOT touch `actors`,
`actor_count`, `actors_truncated`, `conflict_actor_keys`, or `archived_actor_keys` (the actor
lifecycle folds own those). The four-pair mixed matrix is V4's, verbatim: old+old and
old-runtime+new-launcher byte-identical (unknown token inert); new-runtime+old-launcher
demotes as today (token undeclared); new+new promotes.

Contract movement: the `state.patched` contract's entity vocabulary — verify whether the
registry constrains entity names (WV-0); `SNAPSHOT_CONTRACT_VERSION` does not move (no
full-core row changes shape); stream schema stays 2 (no new frame kinds). Fixtures: the
producer-contract fixtures gain an `office_surface` case → regenerate via hermes'
`scripts/generate_agent_runtime_stream_fixtures.py`, mirror to the launcher, re-pin BOTH
`MANIFEST.sha256` files, and verify with
`python tool/test_quality/check_producer_contracts.py --hermes-root X:/Eternia/hermes-agent`
run from the launcher repo — it compares manifest line ORDER before bytes, so a by-hash spot
check passes while the gate is red (the 2026-08-16 divergence at launcher `e1d198985` is the
precedent; RELAYED via the transport plan §8).

### 2.4 — What every stage does to `isSettled` and the retire condition

Stages WV-L1/WV-L2 (remove + surface swaps) run **inside `_flush`, under `flushing = true`**,
replacing an awaited `submitIntent` spawn with an awaited RPC call at the same position in the
same loop. No staging, debounce, overlay, or removal-set semantics change; `writesInFlight`'s
three terms are untouched; `settled`'s four terms are untouched. The only timing change is
that the flush **completes sooner** (≈8 ms round trip instead of a process spawn), which
shrinks the window in which `writesInFlight` is true. That is the safe direction for the
retire condition — the override is retired *later* relative to gesture time, never earlier —
and the page-side pending-save term (OR-1) is not consulted by anything here. WV-L5
(occupied-chat create) changes no office staging at all: `_addAdditionalAgentPlacement`'s
optimistic scene write and `applyLayout`'s stage-before-await order are byte-untouched; the
RPC path adds `adoptServerWrite` (already the drop path's behaviour) so the staged flush
skips the redundant upsert. WV-L6 (resolve) is not on the paint path. **Gate on every
launcher stage: `mission_office_optimistic_paint_test.dart` and
`mission_office_lane_reattach_test.dart` pass byte-unchanged.** If either needs editing, the
stage changed operator-visible paint behaviour and owes an explanation before merging.

## 3. Stages

Ordering: WV-0 first; then each verb's hermes stage before its launcher stage (the O-L2
lesson: the caller gates on the server's advertisement, never on release notes — but the
server side is inert without a caller, so hermes-first is the safe order). The surface fold
(WV-L4/WV-H3/WV-L4b) follows the proven O-L1→O-H1→O-L3 activation pattern. No stage requires
simultaneous deployment; every stage rolls back by reverting its commit.

---

### WV-0 — verify the assumptions (read-only, both repos + one diag-log read)

- **ANSWERED 2026-08-17 by Plan EG §4.8 (EG-0.2 receipts §1.5): the boot-batch claim was
  stale — surface events are covered since WV-H3; the live defect is the sink DROP
  (EG-1.3). Discharged; kept for the record.** **A-1 (the boot-batch claim).** §10.3 item 6 says one `office.surface.updated` "sinks a
  23-event startup batch". Verify from the operator's diag log + event log (read-only, the
  fold plan §1's method) what a page-open batch actually carries: if it also carries other
  uncovered events (chat/session traces, `office.surface.created`), the fold half's boot win
  shrinks to "folder-change batches promote" and this doc's §2.1 row is corrected before
  WV-H3 lands. **This is the one assumption that changes scope, which is why it is first.**
- **A-2.** Confirm the `state.patched` contract row (`decision_contract_registry.py:314`
  area) does not enumerate entity names (i.e. `office_surface` needs no registry edit beyond
  what the partition gate `test_stream_patch.py:312` requires for the newly covered event).
- **A-3.** Confirm `MissionAgentCreated` carries everything the occupied-chat adoption block
  consumes — walk `MissionChatAckAdoption.resolve`'s inputs (`mission_control_page.dart:1994-2007`)
  against the create result; list any field that today only arrives via the capability
  result's `capabilityPayload` (e.g. `previous_session_id`) and decide its source (the
  RPC path has no previous session by construction — a minted instance has none — so `null`
  is expected to be honest; verify the adoption tolerates it).
- **A-4.** Confirm no OTHER submit sites exist for the three capabilities beyond the ones in
  §1 (RAN this session for `capabilityId:` — re-run including any indirect constructors).
- Output: this section's table updated; no code.

---

### WV-H1 — hermes: `runtime.office.remove`

**Change surface.** `agent_runtime/serve_rpc.py`: `@method("runtime.office.remove")` per
§2.2, placed beside `_runtime_office_upsert`; imports and error translation copied from it.
No other hermes file changes — the producer/coverage work under archives landed in O-H1/O-H3.

**Tests** (new `tests/agent_runtime/test_serve_rpc_office_remove.py`, shaped like
`test_serve_rpc_office_upsert.py`):
- `remove archives the actor and acks {actor_key, revision, state}` — kill-mutation: return
  the pre-archive revision (the +1 is the client's next guard token).
- `unknown workspace refused 4001 workspace_not_found before any store write` — kill:
  reorder the check after the store call (assert no archive file appeared).
- `unknown actor refused 4001 actor_not_found` — kill: collapse it into workspace_not_found
  (assert the reason string).
- `already-archived key is an ok, not an error, and writes nothing` — kill: make the
  idempotent arm a 4001 (assert ok + unchanged archive mtime/revision).
- `expect_revision mismatch refused 4090 stale_revision with no revision in data` — kill:
  leak the current revision into `data`.
- `the batch a remove lands in still promotes for a lifecycle-declared client` — reuse
  `test_office_state_patches.py`'s delete-gesture fixture builder; kill: emit the archive
  outside the lock. (This pins that the RPC path reaches the SAME chokepoint; it is the
  anti-drift test for "no producer change needed".)

**Mixed pairs.** No caller exists yet; dead code against every launcher. **Rollback.**
Revert. **Perf.** None alone. **Does NOT do.** Guard the launcher's archive (D-W1), change
any emitter, touch fixtures.

---

### WV-L1 — launcher: the archive takes the RPC lane, three arms

**Change surface.**
- `mission_office_rpc.dart`: `kMissionOfficeRemoveRpcMethod = 'runtime.office.remove'`;
  `buildMissionOfficeRemoveRequest({id, workspaceId, actorKey})`;
  `parseMissionOfficeRemoveResponse` → a sealed `MissionOfficeRemoveOutcome`
  (Ok `{actorKey, revision, state}` / Refused `MissionOfficeRpcFault` / Unavailable
  `MissionOfficeRpcDegradeReason`) — same decode ladder as the upsert's, reusing the existing
  reason taxonomy (`actor_not_found` maps onto the existing not-found handling;
  `stale_revision`/`sync_conflict` arms exist already).
- `mission_office_rpc_writer.dart`: `removeActor({workspaceId, actorKey})` — the same gate
  ladder as `upsertActor`, gating on the REMOVE method name (per-method advertisement,
  `missionOfficeRpcRefusal(manifest(), method: kMissionOfficeRemoveRpcMethod)`).
- `mission_office_layout_controller.dart`: the removal loop (`:865-880`) goes RPC-first:
  `Ok` → `removed += 1; rpcWrites += 1` and adopt `ack.revision` into
  `sync.serverRevisions[key]` (the archived key's token — a restore carries it forward, the
  REVISION MISS docstring `:1271-1276` already models this); `Refused` → TERMINAL: existing
  retraction arm (`sync.removed.remove`, `refused += 1`, `rolledBack += 1`, failure line) via
  a `_logRpcRefusal` overload naming the verb — the argv lane is NOT a second chance (same
  ruling as the upsert's, `:752-764`); the `actor_not_found` reason is treated as Ok-shaped
  (the store provably does not have it — retract nothing, count `removed`), matching the
  store's own idempotent arm; `Unavailable` → the existing `_archiveActor` capability submit,
  `cliWrites += 1`, reason into `fallbackReasons`.
- **Receipt spec change, flagged:** the per-flush lane line (`:930-933`) currently reads
  `upsert lane: N rpc, M cli` and its counters are documented as upserts-only
  (`MissionOfficeWriteLaneStatus:224-234`). Removals now ride the same counters; the label
  becomes `write lane:` and the two doc comments update in the same commit. The
  `[MissionOfficeWrite] … flush: X upserted, Y removed, Z refused` line stays byte-identical.

**isSettled/retire:** §2.4 — inside `flushing`, no predicate change; OR-4 fence byte-green.

**Tests** (extend `mission_office_explicit_removal_test.dart` +
`mission_office_upsert_lane_test.dart`'s pattern; new
`mission_office_remove_lane_test.dart` if the file grows unwieldy):
- `an explicit removal rides the RPC lane and never submits the capability` — kill: keep the
  submit (assert the fake repository saw zero `office.actor.remove` intents).
- `a refused remove retracts sync.removed, counts rolledBack, and does NOT fall back to argv`
  — kill: fall through to the capability on Refused (the incident-class mutation).
- `actor_not_found acks as removed without a rollback` — kill: treat it as a refusal (assert
  no corrective-read request).
- `an Unavailable remove falls back to the capability and counts cliWrites` — kill: drop the
  fallback (the "no serve no office" cost is TC-3's decision, not this stage's).
- `the flush receipt says write lane with unified counters` — referencing the shared label
  constant; kill: fork the label string.
- Paint fences: `mission_office_optimistic_paint_test.dart`,
  `mission_office_lane_reattach_test.dart`, `mission_office_mass_archive_incident_repro_test.dart`
  all pass **unmodified** — the tripwire test doubly so, since this stage touches the loop it
  guards.

**Mixed pairs.** Old runtime: method not in manifest → every remove `Unavailable` → argv, wire
byte-identical to today. **Rollback.** Revert. **Perf.** Delete gesture: one spawn removed
from the write leg; measure via the receipt timestamps against MEASURED-§10's 280–368 ms fold.

---

### WV-H2 — hermes: `runtime.office.surface.update`

**Change surface.** `serve_rpc.py`: the method per §2.2 (`surface_exists` refusal BEFORE the
store's lazy ensure — the handler docstring must carry the upsert's incoherence argument).

**Tests** (new `tests/agent_runtime/test_serve_rpc_office_surface_update.py`):
- `folders write acks the NORMALIZED list and the bumped revision` — feed unnormalized input
  (dupes, junk, >64) and assert the echo equals the store's canonical list; kill: echo the
  input back.
- `unknown workspace refused 4001 and NO surface is authored` — kill: let `ensure_surface`
  run first (assert `office.json` absent after the call — this is the test that pins the
  wire/CLI divergence on purpose).
- `expect_revision mismatch → 4090 stale_revision`; `non-list folders → -32602
  folders_invalid` — kill each check.

**Rollback.** Revert. **Does NOT do.** Touch coverage or fixtures (that is WV-H3).

---

### WV-L2 — launcher: the folder branch takes the RPC lane

**Change surface.** `mission_office_rpc.dart` + `mission_office_rpc_writer.dart`: method
constant, `buildMissionOfficeSurfaceUpdateRequest({id, workspaceId, folders})` (a LIST —
the comma-join stays argv-only), `updateSurface(...)` writer, sealed outcome. Controller
`_flush` folder branch (`:882-902`): RPC-first; `Ok` → `sync.serverFolders =
<normalized echo>` (NOT the desired copy — §2.1's rewrite-loop close; a follow-up flush must
diff against what the store actually holds); `Refused` → terminal, existing failure/refusal
accounting; `Unavailable` → existing capability submit.

**isSettled/retire:** §2.4 — inside `flushing`, unchanged.

**Tests** (`mission_office_layout_controller_test.dart` + a folder-lane group):
- `a folder change rides RPC and adopts the normalized echo` — the anti-vacuity probe: the
  fake store normalizes DIFFERENTLY from the input, and the test asserts the NEXT flush
  submits nothing (kill: adopt `desiredFolders` — the second flush re-fires and the assert
  reds; this is the mutation that proves the echo adoption is load-bearing).
- `a refused folder write does not adopt and reports the failure` — kill: adopt on refusal.
- `Unavailable falls back to the capability` — kill: drop the fallback.

**Mixed pairs / rollback / perf.** As WV-L1: old runtime → argv unchanged; revert; one spawn
removed per folder change. **The demote stays** — say so in the commit message so nobody
reads this stage as the boot fix.

---

### WV-L3 — launcher: the `office_surface` fold, dark

**Change surface.** `mission_read_model.dart`: `_officeSurfaceEntity = 'office_surface'`,
`_officeSurfaceFoldCapability = 'office_surface_fold'` (single spelling authority, the O-L1
precedent), `_applyOfficeSurfacePatch` per §2.3 (merge exactly `folders`/`revision`/
`updated_at`; absent row → `patch_without_target`). Wire the entity into the fold dispatch
beside `office_actor`'s.

**Tests** (extend `mission_read_model_office_patch_test.dart`):
- `a surface upsert merges folders and revision and touches nothing else` — the witness
  probes `actor_count` and `conflict_actor_keys` on the same row (fields the patch does not
  carry); kill: make the fold a row replace — the untouched fields vanish and the assert reds.
  (This is the vacuity lesson from D3's M-L2 survivor: probe fields the patch itself does not
  write.)
- `a surface patch for an unknown workspace refuses patch_without_target` — kill: insert on
  absent.

**Mixed pairs.** Dead code until WV-H3 emits + WV-L4 declares. **Rollback.** Revert.

---

### WV-H3 — hermes: surface producer + token gate + coverage

**Change surface.**
- `state_patches.py`: `emit_office_surface_patch(event_log, surface, *, config)` — entity
  `office_surface`, op `upsert`, `changed = {folders, revision, updated_at}`, sized inside
  the existing accounting (folders cannot exceed the value budget by construction, but the
  degrade path stays honest — an oversize marker on this entity falls back to no-patch, i.e.
  the batch demotes as today, never a corrupt merge).
- `office_store.py` `update_surface`: emit the patch inside the lock, before
  `office.surface.updated`, best-effort like `_emit_actor_patch`'s siblings. NOT from
  `ensure_surface` (creates stay full-core) and NOT from the archive/re-add ledger rewrites
  (those do not move folders/revision and are already mirrored client-side — emitting there
  would double-write `updated_at` drift for no reader).
- `patch_coverage.py`: `OFFICE_SURFACE_FOLD_CAPABILITY = "office_surface_fold"`;
  `event_is_patch_coverable`: `office.surface.updated` coverable iff the token is declared;
  `office_surface` patch rows coverable under the same token. `office.surface.updated` joins
  `LIVE_COVERED_DOMAIN_EVENT_TYPES`; the comment block's "must stay absent" list shrinks by
  exactly one entry and keeps `.created`/`.restored`/`.conflict_resolved` with the original
  reasoning.
- Fixtures: regenerate + mirror per §2.3's discipline (generator, verbatim
  `MANIFEST.sha256`, `check_producer_contracts.py` as the only authority — ORDER before
  bytes).

**Preconditions.** WV-L3 merged (the fold exists somewhere fieldable); O-H2's
complete-batch sink and per-client declarations are already live (landed), so the V6 race
cannot re-arm. Safe against every fielded launcher standalone: the token gates everything.

**Tests** (`test_office_state_patches.py` + `test_stream_patch.py`):
- `update_surface emits a subset patch inside the lock before the domain event` — kill: emit
  outside / skip.
- `ensure_surface emits NO patch` — kill: emit on create (the fold would
  patch_without_target every boot).
- `a folder-change batch promotes for a surface-declared client and demotes for the legacy
  declaration` — the paired anti-vacuity check for the gate; kill: drop the token check.
- The both-ways partition gate (`test_every_covered_domain_event_is_registered_or_declared_historical`)
  holds without edits — `office.surface.updated`'s contract is registered
  (`decision_contract_registry.py:265` READ).
- Cross-stack: a committed batch fixture (folder change) with the launcher golden folding it
  byte-identically — the W0/O-H3 fixture family.

**Rollback.** Revert — batches demote again; emitted patches are additive rows old folds
ignore only if… no: an old fold that DECLARED the token cannot exist (the token ships with
the fold). Revert is clean. **Does NOT do.** Cover `.created`; touch the boot write storm.

---

### WV-L4 — launcher: declare `office_surface` + the token on both lanes

**Change surface.** `mission_control_bridge.dart:1375-1376`: `--fold-entities …,office_surface,
office_surface_fold` (spelling from WV-L3's constants; the comment block gains the rationale);
`mission_office_rpc.dart` `buildMissionOfficeSubscribeRequest`: the `fold_entities` list gains
both strings (the RPC subscriber declares the full set — `serve_office_subscriptions.py`'s
per-client declaration, O-H2).

**Preconditions.** WV-L3 in this or an earlier release (declaring what the fold lacks is the
"strictly worse than declaring none" failure — `mission_control_bridge.dart:1370-1374`).

**Tests.** `mission_office_subscribe_codec_test.dart`: request carries the two new strings
(kill: drop one); the bridge argv test referencing the constants (kill: misspell — the
single-authority constant is what this test exists to enforce).

**Acceptance (operator, live, after WV-H3+WV-L4 are both fielded):** rename or add a folder in
the Mission Office → diag log shows a `[MissionFold] applied … (office_surface x1 …)` (or the
office-lane fold receipt) with NO `resubscribe #N (push:full_core)`; `agent.log` shows no
`snapshot_build reason=demote` for that batch. Then open the Mission Office page cold and
read whether the boot batch now promotes — that is A-1's live answer, and this doc's §2.1
row gets corrected if it does not.

---

### WV-L5 — launcher: the occupied-chat "add instance" takes `runtime.agent.create`

**Change surface** (`mission_control_page.dart`; no hermes change — the handler is
gesture-agnostic):
- Extract the drop path's RPC leg into a gesture-labelled helper (rename
  `_createDroppedAgentOverRpc` → `_createPlacedAgentOverRpc({required String gesture, …})`,
  receipts `gesture=drop|addInstance`), keeping the owned-outcome contract (`true` = landed
  or refused; `false` = run two-call).
- In `_resolvePersonaChatActivationPlan`'s `addInstance` arm (`:2263-2276`): after
  `_addAdditionalAgentPlacement` returns the placement, attempt the RPC create THERE — with
  the position the placement policy chose for the staged scene item (the same
  `MissionOfficePlacementPolicy` slot; the staged item already holds it) — and, on
  `MissionAgentCreateOk`, return a plan variant that says **the create already happened**:
  `_PersonaChatActivationPlan(addInstance: true, placementId, placementDisplayName,
  createdOverRpc: created)`. On `Unavailable`, return today's plan unchanged (two-call). On
  `Refused`, surface the refusal (the drop path's message pattern, `:2493-2511`) and return
  null (the gesture is over; hermes compensated).
- In the two consumers (`_startPersonaChat:1965`, `_openPersonaChat:2137`): when
  `plan.createdOverRpc != null`, DO NOT submit `_createPersonaChatIntent`; feed the existing
  adoption block from the created record — `acknowledgedInstanceId =
  created.personaInstanceId`, `acknowledgedSessionId = created.defaultChatSessionId`,
  `previousSessionId = null` (a minted instance has no previous session — A-3 verifies the
  adoption tolerates that). The optimistic-selection `setState`, the epoch guard, and the
  rollback arm stay byte-identical; only the awaited call is swapped. Call
  `_adoptServerPlacement` (as the drop does) so the staged office flush skips the redundant
  upsert.
- The predicted `targetInstanceId = 'personainst_${plan.placementId}'` (`:1934-1936`) must
  equal `created.personaInstanceId`; when it does not, log the drift (the KEY DRIFT
  precedent, `mission_office_layout_controller.dart:1299-1319`) and adopt the server id.

**isSettled/retire:** §2.4 — no office staging changes; `adoptServerWrite` is the drop path's
existing behaviour extended to this gesture.

**Tests** (extend `mission_agent_create_lane_test.dart`; widget-level where the dialog lives):
- `addInstance resolution over an advertising runtime makes ONE rpc call and submits NO
  persona.instance.create intent` — kill: leave the intent submit in (the double-create
  mutation, the exact bug this stage exists to prevent).
- `addInstance against a non-advertising runtime runs the two-call path byte-identically` —
  kill: call unconditionally.
- `a refused create rolls the console selection back and reports why` — reuse the drop
  refusal expectations; kill: swallow the refusal (the "New chat does nothing" regression
  class, `:2031-2039`).
- `the receipt says gesture=addInstance` — kill: reuse the drop label (receipts are TC-3's
  evidence; a mislabelled lane is unverifiable).
- `created.personaInstanceId drift from the predicted id is logged and the server id wins` —
  kill: keep the predicted id (the selection would point at a roster row that never appears).

**Mixed pairs.** Old runtime: manifest gate → two-call, unchanged. **Rollback.** Revert; the
gate closes. **Perf.** The placement stops paying the 600 ms debounce + a spawn on this
gesture; measure via `phases` in the receipt.

---

### WV-H4 — hermes: `runtime.office.resolve_conflict`

**Change surface.** `serve_rpc.py`: the method per §2.2. **Tests** (new
`test_serve_rpc_office_resolve.py`): `take=local keeps the local actor and acks its
revision`; `take=remote adopting an archive tombstone acks state=archived without revision`;
`class-keyed adoption refused 4090 class_key_collision and the sidecar survives` (kill:
resolve anyway); `take=sideways → -32602 take_invalid before any store call`; `unknown
workspace / unknown conflict → 4001` with distinct reasons.

### WV-L6 — launcher: `resolveConflict` takes the RPC lane

**Change surface.** Writer method + outcome; `MissionOfficeLayoutController.resolveConflict`
(`:595-617`) goes RPC-first with the capability submit as the `Unavailable` arm; the
accepted-→-immediate-refresh behaviour is preserved on BOTH lanes (the strip's contract).
The strip's caller (`mission_office_sync_strip.dart:320`) is untouched — the method's
signature and result semantics survive (it can keep returning a `MissionControlActionResult`
synthesized from the outcome, or the strip is updated to a typed outcome in the same commit;
prefer the latter and say so in the diff).

**Tests**: `resolve rides RPC and still requests the immediate refresh` (kill: drop the
refresh — the strip's conflict pill would linger on stale rows); `a class_key_collision
refusal is terminal and names the reason` (kill: fall back to argv on refusal — the argv
verb would hit the same fence, or worse, someone adds `--allow-class-key` to the lowering);
`Unavailable falls back to the capability`.

**Rollback.** Revert. **Perf.** Rare path; no number claimed.

---

### WV-9 — receipts window (observation; owned by R#42's sequence, recorded here)

After WV-L1/L2/L5/L6 are fielded, the write-lane receipts (`write lane: N rpc, M cli`,
`[MissionAgentCreate] lane=…`) are the evidence stream. The DELETION of the argv arms is
`SINGLE_TRANSPORT_COLLAPSE_PLAN` TC-3/TC-4's call, under its exit criteria (`lane=cli` /
`lane=twoCall` = 0 with reason ≠ serve_absent across the agreed window). This plan
deliberately ships the fallbacks intact; a stage that deleted them here would be the
delete-and-see this program keeps getting burned by.

## 4. What this plan does NOT fix

- **The page-open write storm (item 9).** The surface fold makes its demote cheap; the
  launcher still re-upserts the desk and rewrites the surface on page open, off a stale
  cache. Unowned, still the largest un-investigated behaviour on this surface.
- **The ~4.3 s `laneAbsent` window on page open (item 10)** — every verb here degrades to
  argv inside it, by design.
- **Full-core cost for the still-uncovered events**: `office.surface.created`,
  `office.actor.restored`, `office.actor.conflict_resolved`, sync batches — all stay
  honestly full-core.
- **Prediction / the intent ledger (UP-1..4)** and the mass-archive backstop's future —
  UP-4's dual-reconciler window owns "prove the two agree, then delete"; nothing here
  advances or blocks it.
- **The one-transport collapse (R#42/D7)** and the remaining ~38 argv capabilities — the
  DECISION doc's Stage 2 and Plan C, not this plan.
- **Speed of the create itself** — post-D3 it is ~1 s cold (MEASURED-§10 §10.3-1); WV-L5
  removes a debounce and a spawn, not the build.

## 5. Deliberately deferred

- **D-W1 — guarded remove.** `sync.serverRevisions[key]` already holds an honest token when
  a read or ack supplied one; sending it on `runtime.office.remove` is a one-argument change
  (`_archiveActor`'s own docstring pre-approves it). Deferred because it changes operator
  behaviour (a concurrent edit would refuse the delete), and the first cut must be
  behaviour-parity with the argv call it replaces. Decide with a receipt count of
  REVISION-MISS-on-archive once WV-L1 is live.
- **D-W2 — covering `.restored` / `.conflict_resolved`.** Stays uncovered; rare, and both
  move state past the upsert chokepoint.
- **D-W3 — deleting the argv arms.** Plan C's TC-3/TC-4.
- **D-W4 — item 9 investigation** (would shrink the surface-write frequency to genuine
  folder edits, making WV-H3's win smaller and the storm gone — strictly better; someone
  should own it).
- **D-W5 — `runtime.office.restore`.** No launcher call site exists; the wire union does not
  need it until one does.

## 6. Adversarial pass — what I most expect to be wrong

1. **A-1, the boot-batch composition (most likely).** The claim that covering
   `office.surface.updated` promotes the 5.93 s boot batch rests on §10.3 item 6's sentence,
   not on a decode of a boot batch. If page-open batches carry other uncovered events, WV-H3
   buys folder-change promotion only, and the boot number moves only when item 9 is fixed.
   WV-0 decodes one real boot batch before WV-H3 is allowed to claim the number.
2. **The occupied-chat adoption block (most dangerous).** `MissionChatAckAdoption.resolve`
   was built for capability results; feeding it from `MissionAgentCreated` with
   `previousSessionId = null` may hit an arm this session did not read closely (the
   receipt-previous-session fallback at `:2004-2006`). A wrong adoption silently strands the
   console on a dead row — the exact regression class the 2026-07-29 "New chat does nothing"
   report names. That is why WV-L5's refusal/rollback tests reuse the existing block rather
   than reimplementing it, and why A-3 fronts the stage.
3. **The `actor_not_found`-is-Ok choice in WV-L1.** Treating a not-found remove as success
   matches the store's idempotent arm, but if the not-found is actually a KEY-DRIFT (client
   predicted a key the store canonicalizes differently), the actor survives server-side
   while the client counts it removed — a persistent lie until the next read. Mitigation:
   the corrective read already runs on any rolledBack>0 flush, but this arm sets none. If
   WV-0/implementation finds drift reachable on remove keys (the upsert's KEY DRIFT receipt
   proves it is on upserts), the arm must also request the read. Cheap to add; decide with
   the code open.
4. **The normalized-folders echo adoption (WV-L2)** could mask a real disagreement: if the
   store's normalization DROPS a folder the operator added (cap, junk), adopting the echo
   makes the launcher agree silently. The sync-status failure line does not fire (the write
   succeeded). Honest fix if it bites: compare echo vs desired and surface a one-line
   "folders normalized: …" receipt. Named, not staged.
5. **Unverified live, all of it** — same confession as every plan in this family: everything
   above is source-read at the two SHAs plus inherited §10.1 measurements; no gesture in this
   session touched the running launcher and no serve child was spawned.

## 7. Verification log

| # | Fact | How established |
|---|---|---|
| WV-R1 | Five RPC methods registered (`agent.create`, `office.get/subscribe/unsubscribe/upsert`); four `office.*` capability submit sites; `office.actor.restore` lowered but never submitted | RAN greps at hermes `db73fe0b2a` / launcher `3188a540c` |
| WV-R2 | Absence-means-delete inference DELETED (not guarded) 2026-08-15; removals ride `requestedRemovals` only; tripwire demoted to backstop and kept deliberately | READ mission_office_layout_controller.dart:75-107,346-350,810-864; RAN git log (`7623f99cf`) |
| WV-R3 | `_archiveActor` is the named seam; three-arm shape pre-decided; archives unguarded today; `--expect-revision` exists on the argv verb | READ mission_office_layout_controller.dart:975-1011; mission_control_bridge.dart:3921-3934 |
| WV-R4 | `remove_actor` idempotent on archived, NotFound else, StaleRevision guard; paired remove patch + covered `office.actor.removed` already land inside the lock | READ office_store.py:469-502; patch_coverage.py:182-241 |
| WV-R5 | `update_surface` normalizes (64 cap, defaults prepended), bumps surface revision, emits `office.surface.updated` (registered, uncovered); lazily authors via `ensure_surface` | READ office_store.py:51,135-144,298-341; decision_contract_registry.py:265 |
| WV-R6 | `_flush` folder branch adopts `desiredFolders` (not server truth) on accept — the rewrite-loop hazard | READ mission_office_layout_controller.dart:882-902 |
| WV-R7 | `resolve_conflict` returns actor-or-None; class-key fence on `take=remote`; one launcher caller (sync strip) | READ office_store.py:533-…; hermes_cli/harness_parts/office.py:294-331; RAN grep (strip `:320`) |
| WV-R8 | Adding a method does not move `RPC_CONTRACT_VERSION`; manifest advertises per-method; launcher gates per-method | READ serve_rpc.py:119-121,236-249; mission_office_rpc.dart:120-132 |
| WV-R9 | Occupied-chat addInstance: plan minted at :2263-2276, consumed by two awaited two-call sites (:1965, :2137) through the adoption/rollback block; RPC routes only `gesture=drop` (:2448-2520) | READ mission_control_page.dart |
| WV-R10 | `runtime.agent.create` result carries instance id, chat session, actor ack, phases; reservation resume/compensation live | READ serve_rpc.py:1010-1276 |
| WV-R11 | Capability tokens are the proven widening mechanism (two precedents, incl. the subset-vs-complete-row asymmetry this plan's surface fold copies) | READ patch_coverage.py:101-148 |
| WV-R12 | `isSettled` = `!writesInFlight` (3 terms); retire condition carries the page pending-save term since OR-1; `applyLayout` stages before its awaits; OR-4 fence exists | READ mission_office_layout_controller.dart:359-441,463-517; OFFICE_OPTIMISTIC_RENDER_REGRESSION_PLAN §5; RAN git log (`7426d2403`, merged `3188a540c`) |
| WV-R13 | Delete fold 280–368 ms; boot flush demote 5.93 s; build ~1.17 s warm / ~6.9 s cold; RPC round trip ~8 ms | MEASURED-§10 (§10.1) |
| WV-R14 | Fixture authority: `check_producer_contracts.py` compares manifest ORDER before bytes; verbatim-mirror discipline | READ transport plan §8; RELAYED (`e1d198985` incident) |
| WV-A1 | Covering `office.surface.updated` promotes the BOOT batch (not just folder changes) | ASSUMPTION — WV-0 decodes a live boot batch |
| WV-A2 | `state.patched` registry does not constrain entity names | ASSUMPTION — WV-0 |
| WV-A3 | The adoption block tolerates a null previous session fed from `MissionAgentCreated` | ASSUMPTION — WV-0 |
