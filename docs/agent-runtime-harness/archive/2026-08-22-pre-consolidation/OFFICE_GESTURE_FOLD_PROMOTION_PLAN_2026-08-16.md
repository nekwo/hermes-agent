# Mission Office Gesture Fold Promotion — Validated Design + Stages (2026-08-16)

> **Home.** This plan lives in the hermes repo beside the stream contract it
> changes (`docs/agent-runtime-harness/mission-control-stream.md`) because five
> of its eight stages are hermes-owned and every producer/coverage test it names
> is in `tests/agent_runtime/`. Its format follows the launcher repo's staged
> plan family (`docs/mission_control/READ_MODEL_ENTERPRISE_PLAN_2026-07-16.md`
> — the S-stage house format the S7-A code cites as "the plan"), because that is
> the format the read-model workstream's stages have always used. It was NOT
> placed in the launcher's `docs/mission_control/` only because that checkout is
> the operator's live tree and this task's constraint is not to touch it; when
> landed, a copy or pointer belongs there beside
> `DECISION_push_and_rpc_2026-08-13.md`.

**Verdict up front, because it changes the ask.** The brief proposed "make the
office SURFACE a fold entity, following the `office_actor` pattern."
Validation says **no new entity is needed, and building one would be the wrong
shape**: every surface field the two gestures move is either *derivable by the
client from rows it already folds* (`actor_count`, `actors_truncated`, and —
under the two lifecycle ops — the `archived_actor_keys` ledger delta) or
*unread by any client* (`updated_at` drift, documented below). The correct
change is smaller: teach the existing `office_actor` entity two lifecycle ops
(create-insert, remove), derive the container state client-side (the exact
pattern upstream's project tree uses — derived container fields are computed
client-side, never wire-synced), and gate the widened ops behind a new
**negotiation capability token** so no mixed version pair ever regresses.
Validation also found **two defects the brief did not ask about** — a
data-loss race between the two producers that share one client watermark (V6),
and an office resync triggered by every uncovered batch runtime-wide (V7) —
and the first of these is a hard prerequisite of the milestone, not optional
hardening.

Eight stages: three launcher, five hermes. **A real operator gesture first
promotes to a folded patch (`folded > 0` on the office lane receipt) when six
of them are live** — the three launcher stages (shippable as one release) plus
hermes O-H1..O-H3. O-H4/O-H5 remove the remaining full-core rebuilds that are
not on the gesture path itself.

---

## 0. The ask, and the operator ruling this plan serves

- 2026-08-13 ruling (`DECISION_push_and_rpc_2026-08-13.md`, launcher repo): we
  own the better PUSH, upstream owns the better CALL; build the union. The
  office push lane (`runtime.office.subscribe` / `runtime.office.patch`) is
  the PUSH half; it must actually fold.
- 2026-08-16 upstream research (`NousResearch/hermes-agent`,
  `upstream/main = 1f8fdc7bd8`): upstream has **no wire row-patch protocol at
  all**; its instructive pattern is that **derived container fields are never
  wire-synced** — they are computed client-side from rows the client already
  holds (`apps/desktop/src/app/chat/sidebar/projects/workspace-groups.ts:574`)
  — and its invalidation names its scope (`{dirs, full}`,
  `apps/desktop/src/store/workspace-events.ts:12-18`). This plan adopts the
  derivation rule as its center, the scope-naming for the office resync
  (O-H4), and negotiation hygiene (fail-open legacy defaults, unknown keys
  filtered at the trust boundary) throughout. It deliberately does NOT adopt
  invalidate-and-refetch as a primary mechanism, whole-list replacement, or
  the no-replay reconnect — we are event-sourced at an in-process write
  chokepoint, which is strictly stronger, and the per-row `seq`, the
  negotiated fold intersection, and the one-fold-body-for-both-transports
  properties are defended by name in the stages.

## 1. Measured baseline (live, 2026-08-15/16 — RAN unless marked READ)

All live reads below were taken read-only from the operator's running root
(`X:/Eternia/.hermes/agent-runtime/`) and the launcher's diag log
(`%TEMP%/eternia_launcher_diag.log`). Nothing was written or restarted.

**The receipt that defines the problem** (RAN: grep of the diag log):

```
[16:48:18.159] [MissionOfficeSubscribe] ws_codex-test-workspace_28d285 subscribed (start) at baseline 88604954 — 9 items, folded 0, resubscribes 0
[18:28:33.089] [MissionOfficeSubscribe] ... resubscribe #1 (push:full_core) in 250ms (cap 5 per 60s)
[18:28:33.345] [MissionOfficeSubscribe] ... subscribed (push:full_core) at baseline 88621795 — 8 items, folded 0, resubscribes 1
[18:28:49.250] [MissionOfficeSubscribe] ... resubscribe #2 (push:full_core) in 500ms ...
[18:29:25.949] [MissionOfficeSubscribe] ... resubscribe #3 (push:full_core) in 1000ms ...
```

Every subscribe across the whole session reads `folded 0`. Every gesture
becomes `resubscribe #N (push:full_core)` with the backoff ladder
250ms→500ms→1s (cap 5 per 60s, then the lane parks —
`mission_office_subscribe_lane.dart:294-302,721-735`, READ).

**The two gesture batches, decoded from the live event log** (RAN: byte-offset
read of `events_archive/events.81417412.jsonl` at cumulative offset 88621795,
via the rotation manifest `events_manifest.json`):

```
ADD    22:28:42 persona_instance.chat_opened   {session_id: persona_chat_personainst_qa_agent_b956655e_...}
       22:28:43 state.patched                  {entity: office_actor, id: ws_.../personainst_qa_agent_b956655e, op: refresh}
       22:28:43 office.actor.upserted          {actor_key: personainst_qa_agent_b956655e, items: 1, revision: 1}
DELETE 22:38:39 persona_instance.retired       {reason: placement removed from Mission Office}
       22:38:39 state.patched                  {entity: persona_instance, id: personainst_qa_agent_8c4c5c38, op: remove}
       22:38:39 office.actor.removed           {actor_key: personainst_qa_agent_8c4c5c38, reason: instance_reaped}
```

This confirms the brief's batch enumeration exactly, and adds one fact the
brief did not have: **the add gesture's chat-open and office write land ~1
second apart** — outside the 200ms coalescing debounce
(`agent_runtime/stream.py:506-511`, READ) — so the two halves of the add can
and often will land in *separate* batches. `revision: 1` confirms the add was
a CREATE (new actor file, new persona-instance row).

**Why each gesture costs two full-core builds** (all READ):

1. The batch demotes: the add carries `op: refresh`
   (`OfficeStore.upsert_actor` → `_emit_actor_patch`,
   `agent_runtime/office_store.py:200-203` — `replaced_existing=False` on a
   create → `emit_office_actor_refresh`); the delete carries
   `office.actor.removed`, deliberately uncovered
   (`agent_runtime/patch_coverage.py:135-144`). One uncovered event demotes
   the whole batch (`patch_coverage.py:300-318`), and the fallback is an
   unscoped `build_snapshot()` (`stream.py:295-335, 356-360`) — the full core
   measures 822,671 bytes against 486 for a patch (`patch_coverage.py:164`).
   That build happens on the serve hub's shared producer
   (`hermes_cli/harness_parts/serve.py:1704-1725`) *and independently* on the
   launcher's `harness stream` child
   (`mission_control_bridge.dart:1361-1376`).
2. The office sink cannot express a full core: any non-`patch` frame past the
   baseline becomes `runtime.office.resync`
   (`agent_runtime/serve_office_subscriptions.py:243-263`), the client
   re-subscribes, and `StreamHub.subscribe` **restarts the producer by
   contract** (`agent_runtime/serve_stream_hub.py:505-545`: "Restarts the
   producer so this subscriber's first frame is a hydrate... not an
   optimisation to remove") — a second 822KB build whose hydrate the office
   sink then *drops at its own baseline gate*
   (`serve_office_subscriptions.py:227-244`). The operator measured
   3.8–5.7s per gesture, twice.

**Current promotion state** (READ): the launcher declares
`persona_instance,incident,office_actor` on the stream child
(`mission_control_bridge.dart:1375-1376`); the office RPC lane declares the
server-side constant `OFFICE_FOLD_ENTITIES`
(`serve_office_subscriptions.py:148`); the hub intersects both tables
(`serve.py:1651-1689`). Pure position drags of an existing actor already emit
a coverable `office_actor` upsert (`office_store.py:200-201`); tonight's
session shows they never got the chance to fold — every observed gesture was a
create or an archive.

## 2. Validation (the five questions, plus what the audit found)

Each claim is tagged with how it was established: (READ file:line) for code
read in this session, (RAN) for live log/event reads executed above.

### V1 — Q1: Is a separate surface entity the right shape? **No.**

Field-by-field audit of the office surface wire row
(`agent_runtime/snapshot.py:1274-1291`, READ), against what each gesture
actually moves (`office_store.py:330-417` create/re-add, `:594-616` archive,
READ):

| Wire field | Authority | Moved by add? | Moved by delete? | Client need |
|---|---|---|---|---|
| `folders` | stored on surface | no | no | read (canvas); only `update_surface` moves it (`office_store.py:277`) — stays full-core (rare operator action) |
| `actors[]` | the rows themselves | yes (new row) | yes (row leaves) | **the actual content — foldable per-actor** |
| `actor_count` | **derived**: `len(actors)` (`snapshot.py:1282`) | yes | yes | derivable client-side from the folded list (upstream's container rule) |
| `actors_truncated` | **derived**: `max(0, n-200)` (`snapshot.py:1283`) | yes | yes | derivable; guarded (V-adversarial: >200 actors) |
| `conflict_actor_keys` | sidecar files (`snapshot.py:1284`) | no | no | read (`mission_office_sync_strip.dart:31`, `mission_office_layout_controller.dart:1065-1068`) — moved only by sync/resolve flows, all uncovered → full core, unchanged |
| `archived_actor_keys` | stored ledger (`office_store.py:606-607`) | only on resurrection re-add (`:396-398`) | **yes — grows** | parsed (`mission_control_snapshot.dart:1665,1677`) but **no widget reads it** (exhaustive grep of `lib/`, READ); its consumers are server-side guards. Its *delta under the two lifecycle ops is exactly determined*: archive appends the key (if absent, cap 5000), re-add/restore removes it — so the client can mirror it during the fold, byte-for-byte for the covered paths |
| `revision` | stored; moved **only** by `update_surface` (`office_store.py:279`) — verified NOT moved by archive/re-add | no | no | none on the gesture path |
| `updated_at` | stored; moved by archive/re-add (`:399, 608`) | resurrection only | yes | **no reader found** of the stream office row's `updated_at` in the launcher (grep, READ; the office feature reads only its own RPC projection's `updatedAt`, `mission_office_rpc.dart:547`). Accepted, documented drift until the next full core |
| `orphaned` | derived server-side | no | no | none |

So the composite-update problem the brief built the entity for **evaporates**:
nothing the gestures move needs a second wire row. The three shapes the brief
asked to compare, for the record:

- **Separate surface entity** — wire cost small, but it puts a *second row*
  on the wire whose only load-bearing content (the ledger) has no client
  reader; it forces a fold-order/atomicity story between two rows (what does a
  client that folded the actor but not the surface render?); its
  `archived_actor_keys` payload has a hard oversize cliff (~110 keys against
  the 3584-byte per-value budget, `state_patches.py:94-95`) after which every
  archive would demote anyway; and it obligates every future client to fold a
  row that exists mostly for server-side guards. Fails-safe (undeclared entity
  demotes the batch) but is permanent wire surface for no reader.
- **Richer actor payload (surface counters inside the actor patch)** — worst
  of the three: it breaks the single-row fold contract (`changed` IS the row —
  `mission_read_model.dart:934-942`), duplicates parent truth into N sibling
  patches with no rule for which copy wins under partial application, and
  couples every actor row's size to the surface's unbounded ledger.
- **Office-scoped composite frame** — a new frame kind, which the stream
  contract's whole discipline exists to avoid
  (`mission-control-stream.md` §Versioning: additive frames only); it would
  need its own negotiation, its own resync path, and would still not answer
  the mixed-batch problem (V6).

**Chosen shape:** two lifecycle ops on the existing `office_actor` entity
(upsert-with-insert on create/re-add, remove on archive), client-side
derivation of `actor_count`/`actors_truncated` and mirror-maintenance of the
`archived_actor_keys` ledger during those folds, `updated_at` drift accepted
and documented. No new entity. No contract version move (nothing about the
full-core row shape changes).

### V2 — Q2: The complete blocker set, event by event

Both batches enumerated from the live log (RAN, §1). Verdict per event:

**Delete batch** (`persona_assignments.py:1216-1224` → 
`_archive_office_placements` → `OfficeStore.remove_actor` →
`_archive_actor_locked`, READ):

| Event | Today | Verdict |
|---|---|---|
| `persona_instance.retired` | uncovered → demotes | **Coverable ride-along.** Its fold state is the row's departure, and the paired `persona_instance remove` patch is emitted at the same chokepoint two lines later (`persona_assignments.py:1220`). Registered contract exists (`decision_contract_registry.py:143`) so the `test_stream_patch.py:312` partition gate passes. One caveat: `retire` also releases child backlinks (`_release_parent_references`, `persona_assignments.py:756-775`), but those route through the steer chokepoint whose events (`persona_instance.steered`) are already covered live with paired upserts — no gap. |
| `state.patched persona_instance remove` | foldable, declared | Already promotes. No change. |
| `office.actor.removed` | uncovered *on purpose* (`patch_coverage.py:135-144`) | **Coverable only after two producer changes**: (1) `_archive_actor_locked` emits a paired `office_actor remove` patch (does not today — verified `office_store.py:594-616` emits only the domain event); (2) the client folds the remove AND mirrors the surface delta (ledger append + derived counts) — otherwise covering it ships a patch frame that silently drops the surface change, the exact failure the current comment block names. The 2026-08-14 reasoning in that block was correct **for the wire as it stood**; what changed is the derivability analysis (V1), not the honesty rule. |

**Add batch** (`persona_assignments.py:1402-1561` open_chat +
`office_store.py:330-417`, READ; live timing shows the two halves may split
into separate batches — §1):

| Event | Today | Verdict |
|---|---|---|
| `persona_instance.chat_opened` | uncovered → demotes | **Coverable only with a paired producer that does not exist yet.** `open_chat` writes real wire-visible state — `mode`, `workspace_id`, `realm_id`, `profile_id`, `display_name`, `default_chat_session_id` (+ its `chat_session_id`/`session_id` mirrors), all present in `persona_instance_summary` (`persona_assignments.py:2263-2335`) — and emits **no** `state.patched` (verified: no emitter call in `open_chat`). Covering the event without pairing it would silently drop those fields from every connected client. Two sub-cases: **(a) re-open/update** (`created=False`, before≠after): emit a `persona_instance` upsert for the diffed fields — requires extending the store→wire map and wire-row builder (O-H1). **(b) create** (`created=True`, tonight's live case): the row is NEW; the launcher's generic fold refuses an upsert for a missing row (`mission_read_model.dart:897-899`) and a full row cannot be assumed to fit the 4KB cap (`state_patches.py:29-30` claims ~18KB; post-R2 slimming this is unverified — Open Decision D3). The honest answer is `op: refresh` on `persona_instance` → the batch demotes to a full core. **A brand-new agent's roster row rides the full core; the office half of the same gesture still promotes because it is (live-proven) usually a separate batch.** The brief's framing — "a surface entity alone does NOT fix the add if chat_opened still demotes" — is right, and this is the fix. |
| `state.patched office_actor op:refresh` | unfoldable by design | **Becomes `op: upsert` carrying the complete row plus a `created: true` marker** (the marker is for coverage gating, V4 — the fold itself doesn't need it). The row is complete by construction (`emit_office_actor_patch` ships the full `_office_actor_summary_row`, `state_patches.py:617-653`), which is what makes insert-on-absent safe. Guard: if the post-write actor count exceeds `MAX_OFFICE_ACTORS_PROJECTED` (200, `snapshot.py:1226`), keep the refresh — projection membership is not client-decidable under truncation. |
| `office.actor.upserted` | covered live (`patch_coverage.py:144`) | Unchanged. |

**Ride-along safety rule** (applies to all three additions): a covered domain
event whose paired patch failed to emit (the emitters are deliberately
best-effort — `office_store.py:204-209`) or landed in the next batch (appends
are adjacent but the drain can split them — `stream.py:484-505`) produces a
promoted patch frame whose `patches` list omits that row. The launcher
tolerates this: an empty/partial `patches` list applies cleanly and advances
the watermark (`mission_read_model.dart:739-784`), and the paired patch then
folds in the following batch. The failure cost is one frame of latency, never
corruption — same posture the already-covered `persona_instance.steered`
lives with today.

### V3 — Q3: Is `refresh` overloaded? **Yes — and the fix is deletion, not vocabulary.**

Confirmed defect: `refresh`'s documented meaning is "this row is too big to
fold, re-fetch it" (`patch_coverage.py:16-24`, `state_patches.py:29-33`), for
which a full core IS the correct fallback. The office create raises it for an
unrelated reason — "a second row changed and I have no vocabulary for it"
(`state_patches.py:656-671`). After V1's derivation move, **the second meaning
has no remaining call site**: create and re-add emit real upserts, archive
emits a real remove, and the only surviving `refresh` producers are the
genuine ones (oversize degrade inside `build_state_patch`,
`state_patches.py:178-188`, and the new >200-actors truncation guard, which is
a true "not expressible per-row" case). No new op is needed; the conflation
dies with its misuse. Upstream's `{dirs, full}` split independently validates
that the two meanings are load-bearing — and the optional refinement it
suggests (a `refresh` that carries the row's `revision` so a client holding
that revision can skip the refetch, upstream `live-sync.ts:25-27`) is recorded
as Deferred D2, not staged: after these stages the office `refresh` is nearly
extinct, so the comparison would optimize a lane that no longer fires.

### V4 — Q4: Backward compatibility. **Safe — but only because of a piece the brief didn't ask for.**

The negotiation is per-ENTITY (`patch_coverage.py:271-297`), and this plan
**widens the OPS of an already-declared entity** — a case the entity
vocabulary cannot express. That is the audit's most important compatibility
finding: if a new runtime simply started emitting create-upserts and removes
under `office_actor`, every fielded launcher (which declares `office_actor`
today, `mission_control_bridge.dart:1376`) would have those batches PROMOTED
at it and would answer each with `patch_without_target` /
`patch_unsupported_op` → a full re-hydrate (`mission_read_model.dart:995-999,
1012-1013`) — strictly worse than today's full core, the exact "declares what
it cannot fold" failure the bridge comment warns about
(`mission_control_bridge.dart:1370-1374`).

**Fix: a capability token in the existing declaration channel.** The
declaration set is just strings (`normalize_fold_entities`,
`patch_coverage.py:177-191` — nothing requires each string to be an entity).
New launchers declare `office_actor_lifecycle` alongside the entities, on both
lanes. Coverage gates the widened rows on it: an `office_actor` patch with
`op: remove`, or with `created: true`, is coverable only when the declaration
holds the token; a plain move upsert stays coverable under bare `office_actor`
exactly as today. Mixed-pair matrix:

| Pair | Behaviour |
|---|---|
| old runtime + old launcher | byte-identical to today |
| old runtime + new launcher | token is an unknown string in a frozenset — inert (`parse_fold_entities_option` passes any token through; old coverage code never consults it). Today's wire, byte-identical. |
| **new runtime + old launcher** | gestures keep demoting to full cores (no token declared) — today's behaviour, no regression, no simultaneous deploy needed |
| new runtime + new launcher | gestures promote |

One hole remains and O-H2 closes it: the office RPC lane's declaration is a
**server-side constant** (`OFFICE_FOLD_ENTITIES`,
`serve_office_subscriptions.py:148`) — the server cannot know whether the
*connected* launcher's fold is widened, so `runtime.office.subscribe` gains an
optional `fold_entities` param, defaulting to the legacy constant when absent
(fail-open for old clients — upstream's `descriptor.py:82-96` discipline).

**Version gates:** the stream schema stays at 2 (`stream.py:33` — no frame
shape changes; `created` is one additive key inside a patch row, and the
launcher fold reads only `entity/id/op/changed/seq`,
`mission_read_model.dart:850-852,883` — unknown keys ignored).
`SNAPSHOT_CONTRACT_VERSION` stays at 54 (`snapshot.py:88`) — V1's derivation
approach means **no full-core row changes shape** (the earlier draft of this
design retired `archived_actor_keys` from the wire row and moved the contract;
the client-side ledger mirror makes that unnecessary — recorded as rejected
alternative in D1). The one registry row that must move:
`decision_contract_registry.py:314` (`state.patched` contract) gains `created`
as an optional key. `agent_runtime` is fork-owned end to end — upstream has no
counterpart to coordinate with (2026-08-13 ruling, re-verified by the
2026-08-16 upstream sweep).

### V5 — Q5: Fallback scoping. **Separate work — and partly superseded in scope.**

Confirmed: `stream.py:348-360` promotes with `fold_entities` but the fallback
builds a full, unscoped `build_snapshot()`. Scoping that fallback to the
negotiated entities would change what a `delta` frame's `core` MEANS (today:
the whole authoritative read model; scoped: a partial one), which is a new
client contract for every consumer of the full-core lane — a workstream of its
own, with upstream's `{dirs, full}` dirty-set as the right design skeleton
(resync frames naming *which* sections are stale, a ladder of patch → scoped
refetch → full hydrate). **Not staged here.** Two reasons it also matters
less after this plan: (1) the office gestures leave the fallback path
entirely; (2) the office lane's *own* over-broad fallback trigger — resyncing
on every uncovered batch runtime-wide, even ones carrying nothing for its
workspace — IS in scope and cheap, because the delta frame already enumerates
its events (`stream.py:215`): that is O-H4. Deferred ledger D4 holds the full
fallback-scoping design pointer.

### V6 — Found defect: two producers, one client watermark, filtered rows (milestone prerequisite)

The launcher folds BOTH transports into one `MissionReadModel` with one
`_sequence` and a strict `base == held` gap gate
(`mission_read_model.dart:724`; one fold body for both lanes,
`mission_control_bridge.dart:1902-1914`). But the two transports have
**independent producers with independent batch boundaries**: the `harness
stream` child (`mission_control_bridge.dart:1361`) and the serve hub
(`serve.py:1704-1743`) each tail the same EventLog with their own cursor and
their own 200ms debounce. And the office sink forwards a **workspace-filtered
subset** of a batch's rows stamped with the **full batch's watermark**
(`serve_office_subscriptions.py:265-285`).

Today this is latent only because the office lane has never folded (`folded
0`, §1). The moment O-H3 makes mixed batches promote, two failures arm:

- **Data loss:** delete batch `[persona_instance remove + office_actor remove]`
  → office notification carries only the office row but claims the full span →
  if it folds first, the stream frame at the same watermark is `staleDropped`
  (`mission_read_model.dart:696-706`) and the persona remove **never applies**
  — a silently stale roster until some later write. Unrecoverable by any gate
  because the watermark says the span was applied.
- **Resync churn:** whenever the two producers' batch boundaries differ
  (live-proven possible — the add gesture's halves land ~1s apart and each
  producer draws its own boundaries), one lane's next `base` mismatches the
  shared `held` → `gap` → a full re-hydrate on whichever lane lost.

Fixes, both required, both cheap: **(a)** the office sink forwards the
batch's **complete** patch row list whenever any row is in scope for the
subscription (rows are ≤4KB each; the fold already handles every entity;
"addressed, not broadcast" is preserved for batches with nothing in scope) —
O-H2; **(b)** the fold accepts `base ≤ held` and applies only rows with
`seq > held` (every patch row carries its own `seq` — `stream.py:243`), so
same-span and overlapping-span frames from the two lanes dedup per-row instead
of colliding per-frame; `base > held` remains a genuine gap → resync — O-L2.
Full-row replace semantics make per-row replay idempotent; the
`persona_instance` merge-fold is protected by the `seq` filter (no old subset
can overwrite a newer row).

### V7 — Found defect: the resubscribe's mandatory producer restart

Every office re-subscribe restarts the shared producer to manufacture a
hydrate the office sink then provably discards at its baseline gate
(`serve_stream_hub.py:512-517`, `serve_office_subscriptions.py:84-98,243`).
The restart exists for late-joining STREAM subscribers, whose first frame must
be a hydrate; an office subscriber's baseline rides its RPC reply instead
(`serve_rpc.py:597-654`). O-H5 makes the join restart-free when it cannot
narrow the accepted fold set; combined with O-H4 (fewer resyncs at all), the
"second 822KB build" term of the baseline goes to zero. Note the seed-quietly
property upstream item 6 asks about is already satisfied on this lane: the
subscribe reply carries the baseline and the sink drops everything at or
behind it (`serve_office_subscriptions.py:243`, fixed at `2ccf3ab337`) — boot
does not manufacture a storm.

## 3. Platform facts (the substrate, verified this session)

- One write chokepoint per domain, event-per-mutation, cross-process
  `office_lock` held across write+append — EventLog order agrees with revision
  order per actor (`office_store.py:162-209`).
- Patch rows carry `seq`/`ts` from the log envelope (`stream.py:243`);
  `base_offset`/watermark chain contiguously per producer (`stream.py:479-505`).
- The office patch is the COMPLETE wire row (`state_patches.py:623-641`), which
  is what makes replace/insert folds and per-row replay idempotent.
- The launcher fold is two-phase (prepare → off-isolate re-project → commit)
  with one body for both transports (`mission_control_bridge.dart:1921-1967`);
  receipts land in `eternia_launcher_diag.log` via `missionFoldLogSink` /
  `missionOfficeSubscribeLogSink`.
- Negotiation: per-subscriber declared strings, intersection across the hub
  room, absence = `{persona_instance, incident}` (`patch_coverage.py:169-215`,
  `serve.py:1651-1689`).
- Kill-switch inherited by every stage: root config
  `read_model.delta_patches: false` darkens the whole producer lane
  (`state_patches.py:237-308`) — patches stop, everything rides full cores.
- Realm-sync pull archives route through the same chokepoint
  (`office_sync.py:261` calls `store.remove_actor`), so O-H1's paired patches
  cover sync-driven archives too; sync batches still demote on their own
  uncovered `office.actor.*`/sync events — honest, unchanged.

## 4. Target architecture (one paragraph)

One write → one coalesced batch → **per-row op patches for everything the two
gestures move**: the actor row (insert/replace/remove, complete-row semantics)
and the persona-instance row (merged field subset on re-open; honest
refresh-demote on create). The client derives container state from the rows —
counts recomputed, ledger mirrored — instead of receiving a second row. Both
transports carry the same rows with the same `seq`s; the client folds per-row
against one watermark, so the transports are redundant rather than racing.
Coverage widens only under a declared capability token, so every mixed pair
degrades to exactly today's wire. Full cores remain the honest fallback for
everything else (surface edits, restores, conflicts, sync, creates' roster
rows) — and stop being manufactured twice for gestures that no longer need
them.

## 5. Stages

**Dependency order.** Launcher first — O-L1/O-L2 are inert against every
runtime and O-L3 (the declaration) requires both in the same or an earlier
release. Hermes activates behind the token: O-H1 (producer + gate) is safe in
any order against any launcher; O-H2 (sink completeness + per-client
declarations) must land **before** O-H3 (coverage widening) because promoted
mixed batches without the complete-batch sink are V6's data-loss race; O-H4
and O-H5 are independent cost removals. **Milestone: the first real gesture
folds (`folded > 0`) once O-L1..3 + O-H1..3 are live — six stages. The add
gesture's office half can fold one hermes stage earlier (O-H1 + the launcher
three) when it lands in its own batch, which the live 1s gap makes common.**

No stage requires simultaneous deployment. No stage changes the shape of
durable state (EventLog payloads gain one additive optional key; every store
file is untouched), so **every stage rolls back by reverting its commit** —
noted per stage anyway, with the one nuance each has.

---

### O-L1 — Launcher: lifecycle folds + client-side container derivation

**Goal.** The read model can fold an office actor create and archive, keeping
the office row's derived/ledger state in byte-parity with a server rebuild —
capability built, not yet declared.

**Change surface** (`lib/features/mission_control/data/mission_read_model.dart`):
- `_applyOfficeActorPatch` (`:952-1017`):
  - `upsert`: on `index < 0`, **insert** the complete row in `actor_key` sort
    order (parity with `list_actors`' sort, `office_store.py:310`) instead of
    `patch_without_target`; on any upsert, remove `actor_key` from the row's
    `archived_actor_keys` if present (mirrors `office_store.py:396-398`).
  - `remove` (currently refused at `:1012-1013`): splice the actor by key
    (absent → clean no-op, idempotent, same rule as the generic remove
    `:864-876`); append `actor_key` to `archived_actor_keys` if absent,
    trimmed to the last 5000 (mirrors `office_store.py:606-607`).
  - after either: recompute `actor_count = actors.length` and
    `actors_truncated = 0`; **guard**: if the held row shows
    `actors_truncated > 0` or `actor_count != actors.length` *before* the
    fold, the base is a truncated projection → return
    `patch_truncated_base:<id>` (resync) — derivation over a truncated list
    would lie.
- New constant beside `_officeActorEntity` (`:919`):
  `_officeActorLifecycleCapability = 'office_actor_lifecycle'` — single
  spelling authority for O-L3 and the RPC lane.

**Atomicity/idempotency.** Row + derived state move in one working-core
mutation inside one `prepareFold`; commit is all-or-nothing per frame
(`:759-772`). Replay-safe: insert-or-replace by key, remove-if-present,
ledger add-if-absent/remove-if-present.

**Mixed pairs.** Old runtime never emits these rows → dead code until O-H1;
new runtime never promotes them at this build until O-L3 declares the token.

**Tests** (extend `test/features/mission_control/mission_read_model_office_patch_test.dart`):
- `office actor create upsert inserts in actor_key order and recomputes actor_count` — kill-mutation: revert insert to refusal, or skip the recompute.
- `office actor remove splices the row, appends the ledger key once, and recomputes` — kill: leave `actor_count` stale or double-append the key.
- `re-add upsert clears the ledger key` — kill: drop the ledger mirror.
- `remove of an absent actor is a clean no-op` — kill: make absent-remove a resync.
- `truncated base refuses lifecycle folds with patch_truncated_base` — kill: delete the guard.
- **Spec changes, flagged:** the existing `remove` expectation asserting
  `patch_unsupported_op` inverts — that assertion pinned a producer behaviour
  O-H1 retires, so the test changes because the spec does (the file's comment
  block `:944-951` is rewritten in the same commit).

**Rollback.** Revert the commit. No persisted state involved.

**Perf.** No number moves (capability is dark). **Deletes.** The
remove-refusal branch + its justifying comment become dead at O-H1; this stage
makes them *removable*, O-H1's landing note removes them.

**Does NOT do.** Declare anything on the wire; touch the subscribe lane;
change generic-entity folds.

---

### O-L2 — Launcher: per-row `seq` folding (the two-producer fix, client half)

**Goal.** Overlapping frames from the stream child and the office push lane
dedup per-row instead of colliding per-frame (V6).

**Change surface** (`mission_read_model.dart`):
- `prepareFold` gap gate (`:724`): `base > held` → `gap` resync (unchanged);
  `base ≤ held` → proceed, and the apply loop skips any row whose
  `seq ≤ held` (rows carry `seq`, `stream.py:243`; a row *missing* `seq` is
  applied only when `base == held` — the conservative legacy path for old
  fixtures). Stale gate (`:696`) unchanged: a frame whose watermark is not
  ahead of held is dropped whole.
- `MissionPreparedFold` gains a `skippedCount`; the `commitFold` receipt line
  (`:836-840`) reports `applied X of Y rows (Z below watermark)` so a
  partially-skipped fold is readable, not silent.

**Why sound.** Office rows are complete-row ops (idempotent replay); persona
rows are subset merges, protected from regression by the `seq > held` filter —
no older subset can overwrite a newer row through this gate.

**Tests** (`mission_read_model_patch_test.dart` +
`mission_office_push_fold_intake_test.dart`):
- `overlapping frame with base below held applies only rows above the watermark` — kill: apply all rows (an old subset overwrites a newer field → assert the field).
- `frame with base above held still refuses as gap` — kill: relax the gate entirely.
- `row without seq applies only at exact base match` — kill: apply it on overlap.

**Mixed pairs.** Pure client hardening; both runtimes' contiguous frames hit
the `base == held` path byte-identically.

**Rollback.** Revert. **Perf.** Prevents the V6 churn from ever being
measured; no baseline number moves yet. **Deletes.** Nothing.
**Does NOT do.** Change what the office sink sends (that is O-H2).

---

### O-L3 — Launcher: declare the capability on both lanes

**Goal.** Activation, launcher side.

**Change surface.**
- `mission_control_bridge.dart:1375-1376`: `--fold-entities
  persona_instance,incident,office_actor,office_actor_lifecycle` (spelling
  from O-L1's constant; the comment block `:1364-1374` gains the token's
  rationale).
- `mission_office_rpc.dart:1043` `buildMissionOfficeSubscribeRequest`: params
  gain `"fold_entities": [persona_instance, incident, office_actor,
  office_actor_lifecycle]` (full set, not just the token — the RPC subscriber
  must declare what its shared fold can actually fold, same reasoning as
  `OFFICE_FOLD_ENTITIES`' superset, `serve_office_subscriptions.py:120-147`).

**Preconditions.** O-L1 and O-L2 in this or an earlier release — declaring a
capability the fold lacks is the "strictly worse than declaring none" failure
(`mission_control_bridge.dart:1372-1374`).

**Mixed pairs.** Old runtime: unknown strings are inert on both channels
(`parse_fold_entities_option` `patch_coverage.py:218-229`; subscribe params —
old handler ignores unknown params by construction, verify in
`test_serve_rpc_office_subscribe.py`). New runtime: promotion arms.

**Tests.** `mission_office_subscribe_codec_test.dart`: request carries
`fold_entities` (kill: drop the param); a bridge argv test pinning the
`--fold-entities` string (kill: misspell the token — this is the drift the
O-L1 constant exists to prevent, so the test must reference the constant).

**Rollback.** Revert — the runtime falls back to legacy behaviour per-client
on its next subscribe/spawn. **Perf.** None alone. **Deletes.** Nothing.

---

### O-H1 — Hermes: lifecycle producer + the coverage gate that keeps old clients safe

**Goal.** Creates, re-adds, and archives emit foldable ops; `chat_opened`
gets its missing pair; the widened ops are inert for every client that has not
declared the token. **This stage carries its own safety gate — it is
mixed-pair safe standalone.**

**Change surface.**
- `agent_runtime/state_patches.py`:
  - `build_state_patch`/`emit_state_patch` accept an optional
    `created: bool` that lands as an additive payload key (sized inside the
    existing 4KB accounting, `:178-188`).
  - `emit_office_actor_patch` passes `created` through; new
    `emit_office_actor_remove(event_log, workspace_id, actor_key, *, config)`
    (op `remove`, no `changed` — always tiny).
  - `emit_office_actor_refresh` (`:656-682`): **misuse retired.** Its two
    callers' create/re-add cases become upserts; it survives solely as the
    truncation degrade (docstring rewritten to say only that).
  - Persona side: `_persona_instance_wire_row` (`:380-414`) gains
    `workspace_id`, `realm_id`, `profile_id`/`backing_profile`/
    `source_profile_id` (derivation mirrors `persona_instance_summary`
    `persona_assignments.py:2239`: `instance.profile_id or
    persona.hermes_profile`), `default_chat_session_id`/`chat_session_id`/
    `session_id` (all mirror `default_chat_session_id`,
    `persona_assignments.py:2320-2322`), and `updated_at`.
    `_PERSONA_INSTANCE_STORE_TO_WIRE` (`:103-121`) gains the matching rows.
    The byte-parity golden in `tests/agent_runtime/test_state_patches.py`
    extends to the new fields — it is the drift fence.
- `agent_runtime/office_store.py`:
  - `_emit_actor_patch` (`:162-209`): signature becomes
    `(actor, *, created: bool)`; body: post-write
    `actor_count = len(self.list_actors(wsid))` under the already-held lock;
    `> MAX_OFFICE_ACTORS_PROJECTED` → `emit_office_actor_refresh` (the one
    honest remaining refresh) else `emit_office_actor_patch(...,
    created=created)`. `created = (existing is None)` — a resurrection re-add
    is `created=True` (the row is absent from the client's list). The
    `surface_rewritten` parameter dies.
  - `_archive_actor_locked` (`:594-616`): emit `emit_office_actor_remove`
    inside the lock, before the domain event, best-effort like its siblings.
    (Also fires for `resolve_conflict`'s `emit=False` archive — correct: the
    row did leave; that batch demotes anyway on `office.actor.conflict_resolved`.)
- `agent_runtime/persona_assignments.py` `open_chat` (`:1547-1561`):
  `created=True` → `emit_state_patch(entity="persona_instance", op=refresh)`;
  else diff the before/after tuple (`:1468-1546`) to store-field names and
  call `emit_persona_instance_patch` — `updated_at` always among them, so the
  pair is never empty (`chat_head_home` alone would otherwise produce a
  patch-less covered event).
- `agent_runtime/patch_coverage.py` — **the gate, in this stage by
  necessity**: `event_is_patch_coverable` (`:271-297`) learns: `office_actor`
  + (`op == remove` or payload `created is True`) → coverable only if
  `"office_actor_lifecycle" ∈ declared`. Without this, this stage's
  create-upserts would be promoted at old launchers (they declare
  `office_actor`) and each would cost a patch **plus** a re-hydrate — the
  regression V4 names.
- `agent_runtime/decision_contract_registry.py:314`: `state.patched` optional
  keys gain `created`.

**Atomicity.** All emissions inside `office_lock` in write order (the
monotonicity contract, `office_store.py:162-195`); the retire pair is
adjacent-append (`persona_assignments.py:1216-1220`). Batch splits are
tolerated per the V2 ride-along rule.

**Mixed pairs.** Old launcher: creates/removes carry the gate → demote →
today's wire. New launcher pre-O-H3: split add batches
(`office_actor created-upsert + office.actor.upserted`) already promote —
the milestone's early half.

**Tests** (`tests/agent_runtime/test_office_state_patches.py`,
`test_state_patches.py`, `test_stream_patch.py`):
- **Spec inversions, flagged as such:**
  `test_create_degrades_to_refresh_because_actor_count_moves` (`:389`),
  `test_readd_of_an_archived_key_degrades_to_refresh` (`:409`),
  `test_a_live_actor_still_in_the_ledger_degrades_to_refresh` (`:436`) assert
  the behaviour this stage retires → they invert to assert
  `op: upsert, created: true` (their docstrings must cite this plan);
  `test_remove_and_restore_stay_uncovered` (`:504`) splits — restore stays
  uncovered, remove's coverage flips at O-H3.
- New: `create emits a complete-row upsert stamped created` (kill: drop the
  stamp — the O-H1 gate test below then proves the regression);
  `archive emits a remove patch inside the lock before the domain event`
  (kill: emit outside/skip); `201st actor degrades to refresh` (kill: delete
  the guard); `undeclared client never sees a created upsert promoted` /
  `declared client does` (kill: remove the coverage gate — this pair is the
  anti-vacuity check for the whole stage);
  `open_chat reopen emits the diffed persona upsert with parity fields` (kill:
  drop a field from the wire map — the golden catches byte drift, this
  catches omission); `open_chat create emits a persona refresh` (kill: emit an
  upsert).

**Rollback.** Revert; emitted `created` keys already in the log are additive
payload keys old readers ignore (verified: dart fold reads a fixed key set;
`validate_event_payload` change rides the same revert). **One-way nothing.**

**Perf.** Split add batches stop building full cores on both producers once
the launcher declares (O-L3): the office half of an add goes from 2×822KB
builds to one ≤4KB patch frame. Delete unchanged until O-H3.
**Deletes.** The `surface_rewritten` refresh branch and its rationale
comments (`office_store.py:187-194`, `state_patches.py:630-641` rewritten);
launcher-side dead branch noted in O-L1 gets removed by the launcher commit
that lands alongside.
**Does NOT do.** Cover any domain event (delete still demotes); touch the
sink; make persona creates foldable (D3).

---

### O-H2 — Hermes: sink completeness + per-client RPC declarations

**Goal.** The office push lane can carry a promoted **mixed** batch without
claiming coverage it did not deliver (V6 server half), and declares only what
its actual client can fold (V4's hole).

**Change surface** (`agent_runtime/serve_office_subscriptions.py`,
`agent_runtime/serve_rpc.py`):
- `office_patch_sink` (`:203-288`): when any patch row is in scope (an
  `office_actor` id under `workspace_id/`), forward the frame's **entire**
  `patches` list (still: no rows in scope → send nothing). The notification
  params keep their shape; docstring records why filtered-subset forwarding
  was the V6 race.
- `OfficeSubscriptions.subscribe` gains `fold_entities: frozenset[str] | None`;
  stored per key; `declarations()` (`:576-587`) returns the stored set per
  live subscription, `OFFICE_FOLD_ENTITIES` when the client said nothing
  (fail-open legacy default — the constant survives as exactly that).
- `_runtime_office_subscribe` (`serve_rpc.py:485-654`): parse optional
  `fold_entities` (list of strings; degenerate values normalized at the
  boundary, unknown entries passed through — upstream `descriptor.py:104-124`
  discipline), thread to the registry; echo the accepted set in the reply
  beside `watermark`/`replaced` (additive key, same "always present" rule).

**Mixed pairs.** Old launcher (no param): legacy constant → lifecycle rows
never promoted into its room → today. New launcher: declares the full set.
Intersection over the room still protects a mixed room
(`accepted_fold_entities`, `patch_coverage.py:194-215`).

**Tests** (`tests/agent_runtime/test_serve_office_subscriptions.py` /
`test_serve_rpc_office_subscribe.py` / `test_serve_rpc_office_subscribe_live_hub.py`):
- `a mixed promoted batch is forwarded whole when one row is in scope` (kill:
  restore the entity filter — assert the persona row is present);
- `a batch with no office rows for this workspace sends nothing` (kill:
  forward everything always);
- `an absent fold_entities param declares the legacy constant` /
  `a declared param narrows or widens the room's intersection` (kill: ignore
  the param — the live-hub test's intersection assertion at `:110-112` style
  goes red);
- `the reply echoes the accepted set` (kill: drop the echo).

**Rollback.** Revert; subscriptions re-negotiate on their next subscribe.
**Perf.** None alone; prerequisite of the milestone.
**Deletes.** Nothing yet — makes `OFFICE_FOLD_ENTITIES`-as-sole-authority
removable once no fielded launcher omits the param (deferred ledger D5).
**Does NOT do.** Widen coverage; change resync classification (O-H4).

---

### O-H3 — Hermes: cover the three gesture events — **the milestone lands here**

**Goal.** Both operator gestures promote end-to-end.

**Change surface** (`agent_runtime/patch_coverage.py`):
- `LIVE_COVERED_DOMAIN_EVENT_TYPES` (`:126-146`) gains
  `persona_instance.retired`, `persona_instance.chat_opened`,
  `office.actor.removed`. All three have registered contracts
  (`decision_contract_registry.py:143,162,267`) so the both-ways partition
  gate (`test_stream_patch.py:312`,
  `test_every_covered_domain_event_is_registered_or_declared_historical`)
  holds without edits — the gate is the reason this is a constant change, not
  a leap of faith.
- The sibling comment block (`:135-144`) is rewritten: `office.actor.removed`
  moves from "deliberately absent" to covered-with-pair, citing V1's
  derivation table; `.restored` / `.conflict_resolved` / `office.surface.*`
  stay uncovered with the original reasoning intact.

**Preconditions.** O-H1 (the pairs exist — covering `office.actor.removed`
without the remove emitter ships promoted frames whose office rows never
arrive: a permanently stale desk, the exact hazard
`emit_office_actor_refresh`'s docstring names). O-H2 (mixed batches are
forwarded whole). Launcher O-L1..3 fielded for the operator's own build —
but NOT required for safety anywhere (undeclared clients demote).

**Tests.**
- `test_office_state_patches.py`: `a delete gesture batch promotes for a
  lifecycle-declared client` — build the live-proven batch (retire → archive
  fan-out) against a seeded root, assert `batch_is_patch_coverable` under the
  full declaration and NOT under the legacy triple (kill-mutation: remove any
  one of the three constants, or the O-H1 gate).
- `an add gesture batch promotes` — same shape for
  `chat_opened + created-upsert + office.actor.upserted` (reopen case) and
  demotes for the create case (persona refresh) (kill: cover creates).
- Cross-stack fixture: a new committed batch fixture in
  `tests/fixtures/stream_frames/` mirroring the delete gesture, with the
  launcher's byte-identical golden
  (`mission_read_model_office_patch_test.dart`) folding it — the
  manifest-hash discipline the transport plan's W0 goldens established.

**Acceptance — the live check the operator runs against their own build:**
1. Delete an agent from the Mission Office. `eternia_launcher_diag.log` must
   show `[MissionOfficeSubscribe] <ws> folded push <B>-><W> — N of M rows, 1
   folded on this lane` and the next subscribe line must read `folded ≥ 1` —
   today every such line reads `folded 0` (§1). No
   `resubscribe #N (push:full_core)` line for the gesture.
2. Add an agent. Same receipt on the office lane for the office half; the
   stream lane shows `[MissionFold] applied … (office_actor x1, …)`; a
   full-core line may still appear for the roster half (documented: D3).
3. Hermes service log: no `serve_office_subscription_rebaselined` for either
   gesture.
4. Gesture-to-paint: the office change renders in under one second (patch +
   fold + re-projection) against the measured 3.8–5.7s-twice baseline.

**Rollback.** Revert the constants — batches demote again; nothing else
changes. **Perf.** The gesture path stops building full cores entirely:
per gesture, 2× ~822KB builds + resubscribe → one ≤4KB patch frame + one
off-isolate re-projection. **Deletes.** The `patch_coverage.py:135-144`
absence rationale (rewritten, not lost — it moves into this plan and the new
comment). **Does NOT do.** Cover `.restored`, `.conflict_resolved`,
`office.surface.*`, or sync/board/task events — all stay honestly full-core.

---

### O-H4 — Hermes: scoped resync — the office lane ignores other people's full cores

**Goal.** An uncovered batch that carries nothing for a workspace no longer
resyncs that workspace's push lane (V7 half one; upstream's dirty-set,
minimally).

**Change surface** (`serve_office_subscriptions.py:243-263`): for a `delta`
frame past the baseline, inspect `frame["events"]` (present on every
coalesced delta, `stream.py:215`): if no entry is an `office.*` event for
this workspace and no `state.patched` names an `office_actor` id under it →
**skip** (no resync, no forward). `hydrate` frames and unknown types keep the
unconditional resync (a frame that cannot be enumerated cannot be scoped —
upstream's `full` bit). Docstring caveat, stated for the future stream-less
client: skipping is sound today because the stream lane owns read-model
currency; a client with no stream lane needs a bookmark advance in the skip
path before this rule can carry it alone.

**Tests** (`test_serve_office_subscriptions.py`): `a chat-trace delta does not
resync the office lane` (kill: restore unconditional resync); `an
office-bearing delta still resyncs` (kill: over-scope the skip);
`a delta with an unreadable events list resyncs` (anti-vacuity: the
conservative arm must be reachable).

**Rollback.** Revert — over-resyncing returns, correctness unaffected.
**Perf.** Resubscribes during unrelated activity (agent turns, board writes)
→ 0; today each is a producer restart away from an 822KB build. The
5/60s park threshold stops being reachable by background noise — which is
why upstream item 5's slow-backstop (unpark on a timer) is deferred (D6)
rather than staged: after O-H4 the park is reserved for genuine defects, its
designed meaning. **Deletes.** Nothing. **Does NOT do.** Scope the stream
lane's fallback core (V5/D4).

---

### O-H5 — Hermes: restart-free rejoin when nothing narrows

**Goal.** An office re-subscribe stops billing the room a fresh full core for
a hydrate its own sink discards (V7 half two).

**Change surface** (`serve_stream_hub.py:505-545`,
`serve_office_subscriptions.py:458-529`): `StreamHub.subscribe` gains
`restart_producer: bool = True`. The office registry passes `False` **iff** a
producer is currently live for the current generation AND the joiner's
declaration is a superset of the accepted set already in force (no narrowing —
narrowing without a restart would leave a producer promoting rows the room can
no longer fold). First-subscriber joins and narrowing joins keep the restart.
The `serve_office_subscription_rebaselined` service-log line
(`serve_office_subscriptions.py:509-528`) gains `producer_restarted:
true|false` honestly instead of the current constant `True`.

**Tests** (`test_serve_stream_hub.py` / `test_serve_rpc_office_subscribe_live_hub.py`):
`a non-narrowing office rejoin does not bump the generation` (kill: always
restart — assert generation stable and no fresh hydrate produced);
`a narrowing rejoin still restarts` (kill: never restart — the intersection
safety assert goes red); `first subscriber still gets a producer` (kill:
attach to nothing).

**Rollback.** Revert — rejoins get expensive again, nothing breaks.
**Perf.** The "second 822KB build" term of the baseline goes to zero for
every re-baseline that does not change the room's accepted set.
**Deletes.** Nothing. **Does NOT do.** Change stream-lane join semantics
(default stays restart-always).

---

## 6. Adversarial pass (argued against the design; unanswered items are named)

- **Workspace with hundreds of actors.** Server: >200 → refresh (O-H1 guard);
  client: truncated base → `patch_truncated_base` resync (O-L1 guard). Both
  degrade honestly to full cores. Cost note: the O-H1 count runs
  `list_actors` (a dir glob + JSON parse per actor) inside the lock on every
  create/archive — at 200 actors this is real I/O on a mutation path; if it
  measures badly the count can come from a filename glob at the price of
  counting unparseable files (named trade-off, decided at implementation with
  a measurement).
- **A 32-item actor.** `items` is one value inside `changed`; past ~3.5KB the
  per-value oversize marker replaces it and the fold resyncs
  (`state_patches.py:172-188`, `mission_read_model.dart:975-978`). Live
  actors are 663–764 bytes; a maximal desk (~15+ items with long display
  names) would demote its own drags to full cores. Accounted degrade, named
  limitation, no action staged.
- **Two clients mutating one office.** `office_lock` serializes writes and
  the append order matches revision order (`office_store.py:162-195`;
  `test_concurrent_writers_never_invert_revision_against_offset`). Folds are
  complete-row ops applied in `seq` order; `expect_revision` refuses lost
  updates at the write (`serve_rpc.py:879-894`). Last-writer-wins per row,
  consistent with log order on every client.
- **Resurrected actor key.** Re-add emits `created: true` upsert; O-L1's fold
  inserts the row AND clears the mirrored ledger key — parity with
  `office_store.py:396-401`. The class-key guard paths that refuse
  resurrections outright are unchanged and uncovered.
- **A client one build behind on shape.** Every widened behaviour sits behind
  the declared token (O-H1's gate), and unknown payload keys are ignored by
  the fold's fixed key-set read. The V4 matrix covers all four pairs; there
  is no pair that regresses below today's wire.
- **Realm sync adopting a remote archive mid-flight.** The pull archives via
  `store.remove_actor` (`office_sync.py:261`) → paired patches fire inside
  the same lock; the sync batch still demotes on its own uncovered events, so
  clients converge via the full core. The pull's *adoption* (write) path was
  not line-verified this session — **open question OQ-1**: confirm every
  office_sync write reaches `upsert_actor`/`_archive_actor_locked` (the store
  chokepoint rule says it must; verify during O-H1, add a test if any bypass
  exists).
- **The two-producer race under promotion.** V6's fix pair (O-L2/O-H2) is in
  the dependency chain before O-H3, deliberately. Residual: a launcher
  running O-L3 against a runtime with O-H3 but somehow without O-H2 would
  re-arm the race — impossible by stage ordering within this plan, named so a
  cherry-pick cannot silently do it.
- **`updated_at` drift on the office row.** Accepted (V1): no launcher reader
  of the stream row's `updated_at`; drift heals at the next full core. If a
  reader ever appears, the fold can stamp the folded actor's `updated_at`
  onto the row (semantically, not byte, equal — the store takes two `now()`
  calls microseconds apart, `office_store.py:594-609`); recorded here so
  that decision is made consciously.
- **Covered event whose pair split into the next batch.** Promoted frame with
  a missing pair-row → watermark advances, pair folds next batch (≤ debounce
  + poll later). One frame of staleness, bounded, self-healing; live timing
  (§1) shows the add gesture already splits this way today.

## 7. Open decisions / deferred ledger

- **D1 (rejected alternative, recorded):** retiring `archived_actor_keys`
  from the wire row (contract 54→55). Made unnecessary by the client-side
  ledger mirror; rejected to avoid a contract move and the launcher pin
  lockstep for a field nobody reads.
- **D2:** revision-stamped `refresh` (upstream `live-sync.ts:25-27`) — worth
  adopting if any refresh path becomes hot again; none is, after O-H1.
- **D3:** foldable persona-instance CREATE. Requires measuring the post-R2
  `persona_instance_summary` row against the 3584-byte value budget (the
  ~18KB figure at `state_patches.py:30` predates residue-slimming) and a
  create-on-absent rule for the generic fold under a second capability token.
  Until then a new agent's roster row rides one full core — the only
  remaining full-core build on the add path.
- **D4:** fallback scoping for the stream lane (V5): the dirty-set ladder
  (patch → scoped section refetch → full hydrate), resync frames naming
  machine-actionable scope. Its own plan; this document's V5/V7 findings are
  its inputs.
- **D5:** retire `OFFICE_FOLD_ENTITIES` as sole authority once no fielded
  launcher omits the subscribe param.
- **D6:** unpark-on-timer backstop for the office lane (upstream item 5) —
  revisit only if parks still occur after O-H4.
- **D7:** the real end-state per the 2026-08-13 ruling remains ONE transport:
  the launcher leaving the `harness stream` child for the socket hub, which
  dissolves the dual-producer topology V6 patches around. Out of scope;
  O-L2/O-H2 are shaped so nothing in them is wasted by that migration
  (per-row `seq` folding and complete-batch frames are what the single-lane
  client needs anyway).
- **OQ-1:** office_sync adoption-path chokepoint verification (see
  adversarial pass).

## 8. Standing constraints (every stage)

Fork-owned files only (`agent_runtime/`, `hermes_cli/` — upstream has no
counterpart, §0). Python tests: 30s per-test cap, `integration` marker
forbidden. No `dart format` sweeps. Never write under `X:/Eternia/.hermes/`;
never spawn `harness serve` casually (boot publishes the store root into
machine-global config). Additive frames and payload keys only; the
`read_model.delta_patches` root flag remains the lane-wide kill-switch; every
new covered event must clear the `test_stream_patch.py:312` partition gate;
every new receipt goes through the named log sinks, never `debugPrint`.

## 9. Verification log (what this plan's claims rest on)

| # | Fact | How established |
|---|---|---|
| R1 | `folded 0` on every subscribe; every gesture → `resubscribe (push:full_core)`; backoff 250ms→1s, cap 5/60s | RAN: diag log grep, §1 |
| R2 | Add batch = `chat_opened` + `office_actor refresh` + `actor.upserted` (revision 1 = create); delete batch = `retired` + `persona remove` + `actor.removed`; halves ~1s apart | RAN: live event log decode at offset 88621795 via rotation manifest |
| R3 | Create/re-add → refresh; archive emits no patch | READ: office_store.py:200-208, 594-616 |
| R4 | One uncovered event demotes the batch; fallback is unscoped `build_snapshot()`; 822,671-byte core vs 486-byte patch | READ: patch_coverage.py:29-39,164; stream.py:295-360 |
| R5 | Office sink resyncs on ANY non-patch frame past baseline; forwards workspace-filtered rows with full-batch watermark | READ: serve_office_subscriptions.py:243-285 |
| R6 | `StreamHub.subscribe` restarts the producer by contract | READ: serve_stream_hub.py:505-545 |
| R7 | Launcher: strict `base == held` gap gate; one fold body, one `_sequence`, both transports | READ: mission_read_model.dart:696-738; mission_control_bridge.dart:1902-1967 |
| R8 | Launcher stream lane is a spawned `harness stream` child with its own producer, declaring `persona_instance,incident,office_actor` | READ: mission_control_bridge.dart:1361-1376 |
| R9 | Surface `revision` moves only in `update_surface`; archive/re-add move ledger + `updated_at` only; counts are derived at projection | READ: office_store.py:279,396-401,606-609; snapshot.py:1282-1283 |
| R10 | `archivedActorKeys` parsed but read by no widget; `conflict_actor_keys` IS read from the snapshot row | READ: grep of launcher lib/; mission_control_snapshot.dart:1665,1677; mission_office_sync_strip.dart:31 |
| R11 | `open_chat` writes wire-visible fields with no paired patch; create vs reopen split at `created` | READ: persona_assignments.py:1447-1561 |
| R12 | Declaration strings are uninterpreted set members; absence = historical pair; room = both lanes' tables | READ: patch_coverage.py:169-229; serve.py:1651-1689 |
| R13 | Patch rows carry per-row `seq`; delta frames enumerate their events | READ: stream.py:215,243 |
| R14 | Sync archives route through `remove_actor` | READ: office_sync.py:261 |
| R15 | Upstream has no wire patch protocol; derived container fields are client-computed; `{dirs, full}` scope-naming | Coordinator's 2026-08-16 upstream research (upstream/main 1f8fdc7bd8), cited file:line therein |

## 10. Post-landing: what it bought, what this document got wrong, and the ordered follow-on

All eight stages landed 2026-08-16 (hermes `3416603571`, launcher `8aa8c36c1`). O-L2 shipped and was
reverted (`38cef46fe` / `6ecba85c2`) — see the correction below. Then the operator drove real gestures
against the built runtime, which is where the numbers in this section come from.

### 10.1 Measured, live, after landing

| Gesture | Path | Time | Evidence |
| --- | --- | --- | --- |
| Delete an agent | **PROMOTED** — folds | **280–368 ms** | Event log `15:29:51.398–51.440Z` to fold `51.808`; `55.550–55.602Z` to `55.882` |
| Create an agent (drag-drop) | DEMOTED — full core | **6.94 s** | Last event `15:30:00.305Z` to resync `~11:30:07.24` |
| Boot flush (11 actors + surface write) | DEMOTED — full core | 5.93 s | Last event `15:29:40.547Z` to notify `~46.42` |

The milestone is real: **archives promote, and the mixed batch folds** — `[MissionFold] applied 2 of 2
rows (office_actor x1, persona_instance x1)`, the roster row and the actor row from one frame. That also
proves the capability token, the declaration, the room intersection and the launcher fold all work end
to end against a real runtime, not just in tests.

**Where the create's 6.94 s goes** (measured on an isolated probe copy; nothing written under `.hermes`):

| Segment | Cost |
| --- | --- |
| Producer poll wake + 200 ms settle, batch drained | 0.0–0.1 s |
| Coverage classification (the demote decision) | ~0 (in-memory frozenset) |
| **`build_snapshot()` full core** | **~6.3–6.6 s** |
| Frame construction + JSON of 829,955 bytes | 0.005 s |
| Hub fanout, sink classify, localhost notify | ms-scale |
| 500 ms resubscribe backoff + subscribe RPC | **outside** the window (round trip ~8 ms) |

The demotion is essentially the entire cost. `build_snapshot()` measures **1.17 s warm / 6.92 s cold on
the X: drive**; live builds sit at the cold number. It is a metadata-heavy filesystem workload — 4,065
`nt.stat`, 3,170 `nt._getfinalpathname`, 1,585 `pathlib.resolve` per build over ~2,000 files. **It is not
a bandwidth problem.** Serialising 822 KB costs 5 ms; re-deriving it from disk costs six and a half
seconds.

### 10.2 Two claims in this document that were wrong

**(a) §V2/§1 — "the two halves land ~1 s apart, outside the 200 ms coalescing debounce, so they can and
often will land in separate batches."** False, and the mechanism is the reverse of what was assumed.
`stream.py:484-533` drains everything available into `pending`, then **sleeps the 200 ms and drains
again into the same `pending`** — its own comment says "one bounded sleep lets the tail join the SAME
frame". It is a **join** window, not a splitter; with the 250 ms poll on top, effective coalescing runs
to ~450 ms. The operator's create landed its two halves **356 ms apart** and they coalesced. The
`persona_instance op:refresh` then sank the batch and took the perfectly foldable `office_actor
created:true` upsert with it. The original claim rested on one observation with one-second-granularity
timestamps — a timing bet recorded as a fact. **Acceptance criterion 2 cannot hold in the coalesced
case.**

**(b) §5 stage ordering — "O-L1/O-L2 are inert against every runtime."** True of O-L1, false of O-L2,
which is *actively lossy* against any runtime without O-H2: its overlap dedup skips rows the filtered
office lane never delivered, while the shared watermark has already advanced past them. Proven with a
red probe pre-landing, then corroborated by the live runtime's own receipt — `STALE dropped:
seq=88661934 not ahead of held 88661934 — 5 rows, 0 applied`. O-L2 is reverted; if revived it belongs
**after** O-H2, never first.

Both errors are the same shape: a claim about *ordering in time* asserted from thin evidence. Treat the
remaining timing claims in this document as unverified until measured.

### 10.3 The ordered follow-on

Ordered by measured value, not by tidiness. Each item states what it buys and what it does **not**.

1. ~~**D3 — make the `persona_instance` create foldable.**~~ **MERGED 2026-08-16** — hermes
   `6cc2da693f`, launcher `5d9d23e19`. Verified on the merged tips, not on the branches:
   `pytest tests/agent_runtime` **5328 passed / 1 skipped / exit 0** (+8); launcher
   `test/features/mission_control` **3714 / 1 skipped / 1 failed** (+6), the failure being the
   pre-existing education tombstone gate (R#41, not ours); and
   `check_producer_contracts.py --hermes-root …` **exit 0**, "producer contract fixtures match Hermes".

   **What that green does and does not mean.** It means the coverage classifier now promotes the create
   batch instead of demoting it, so the ~6.5 s `build_snapshot()` should no longer be on the path.
   It is **not** a measurement: nothing in D3 ran against a live runtime, by constraint. The ~20x figure
   remains an inference from §10.1's breakdown plus the promotion, and the acceptance check is an
   operator gesture — a `[MissionFold] applied … rows` receipt on an add with no
   `resubscribe #N (push:full_core)` behind it. Until that is seen, treat this row as *built*, not *proven*.

   *Does not* fix the race, the half-created agent, or the ~450 ms poll+settle floor. See **§10.5** for
   the measurement that unblocked it and the two deviations from the sketch above.

2. ~~**Log the `snapshot_build` `elapsed_ms` that is already on the wire.**~~ **LANDED 2026-08-16** —
   launcher `2eeb25c45`, hermes producer half merged the same day. **And the premise of this item, as I
   wrote it, was wrong: the number was never on the wire.** I claimed the cost was already being emitted
   and merely needed a consumer. It was not, in three independent ways, each verified in code:

   1. **The heartbeat's `elapsed_ms` is a mid-build cadence sample, not a total.** The envelope emits
      `int((current - started) * 1000)` each time a heartbeat comes due, on `harness serve`'s 5 s
      interval. The measured 6.3–6.6 s build therefore yields exactly **one** sample, at ~5000. It never
      reports what the build cost.
   2. **A build shorter than one interval emits nothing at all.** The 1.17 s warm build — half of the
      warm/cold comparison this plan rests on — was completely invisible on the wire.
   3. **It only ever existed on the uncovered-batch lane.** `hydrate_frame` and `delta_frame` build
      synchronously with no liveness envelope, so the boot hydrate — a full build — had no heartbeat to
      carry anything.

   The samples are also taken from the *envelope's* clock, which starts before the build thread is
   spawned, so a heartbeat landing near completion can report **more** than the build took. Not a total in
   either direction.

   What actually landed is a real measurement: the build times itself **on the build thread** (not around
   the `done.wait`, whose 100 ms cancel poll would round every build up) and emits one INFO line per full
   core to `<HERMES_HOME>/logs/agent.log`:

   ```
   snapshot_build reason=demote elapsed_ms=234 offset=780 events=4
   ```

   `reason` is the load-bearing field. `demote` and `full_core` cost identical seconds and mean opposite
   things — a foldable update that paid for a whole snapshot, versus the flag-off wire working as
   designed. Collapsing them would make the one grep that matters return every frame ever built. Note the
   fold flag is **on by default** in this build, so the ordinary batch label is `demote`.

   This item is the cleanest instance of the failure §10.2 names: I asserted a value was available because
   a field with the right *name* appeared on the wire, without checking what the field measured. The
   correction cost nothing here only because someone read the emitter before building on it.

3. **`runtime.agent.create` — one call: `{persona, workspace_id, position:[x,y]}` returning the placed
   actor.** The launcher must not know that creating an agent involves a chat; that is backend mechanics
   leaking into the client. Kills the coalescing lottery (one call, one event, one batch, deterministic),
   makes the half-created agent unrepresentable (R#37), and creates the timing envelope phase-level
   analytics needs. **Correction:** an earlier draft of this item said it "moots `_addDistinctPlacement`'s
   no-instance-to-thread problem". That function no longer exists — launcher `af2c62541` replaced it with
   `_refuseDropWithNoResolvableInstance` (`mission_control_page.dart:2443`), an out-loud refusal whose own
   docstring names `runtime.agent.create` as its retirement. The live problems this item actually retires
   are that refusal branch (a drop the operator cannot complete) and the placement-without-instance write
   the office store still accepts. **Does not
   make the create foldable** — if the unified call still emits `refresh`, the batch still demotes and the
   6.5 s stays. Do it *for* correctness and measurability, not for speed.

4. **Client prediction + revision reconciliation (§7 / R#43).** ~~The canvas moves on release~~ —
   **LANDED 2026-08-16 (launcher `2a7f0fc65`), and this item was substantially wrong about what remained
   to build.** The prediction loop already existed and was already mutation-tested before the branch that
   "added" it: `missionOfficePredictedRevision`, the `MissionOfficeUpsertOk` revision compare, the miss
   rollback with its `REVISION MISS` receipt, and the corrective read gated on `rolledBack > 0` are all
   present at `2eeb25c45`. The canvas was never waiting on the echo to paint either — the page sets
   `_officeLayoutOverride` inside `setState` on every pan update. What it waited for was the round trip to
   *start*, which is item 5's subject, not this one.

   **The one real gap was the archive half, and it was a live defect.** A deletion is a prediction too:
   the placement leaves the canvas the instant the operator asks, before anything is submitted. When the
   store refused `office.actor.remove` the lane correctly retracted (`sync.removed.remove(key)`) and
   incremented `refused`, but **never `rolledBack`** — the counter the corrective read is gated on. So the
   rollback was real and *invisible*: the actor stayed gone from a canvas the store still had it on until
   some later poll happened to re-resolve. Both arms now count (the store's refusal and the mass-archive
   backstop's). A prediction that never visibly retracts is worse than no prediction, because the canvas
   then lies persistently rather than briefly.

   The masking caveat stands: prediction hides latency, it does not remove it. Applied before D3 it would
   have concealed six and a half seconds of real work.

5. **Commit on pointer-up rather than 600 ms after the last movement.** **LANDED 2026-08-16** (same
   merge). The cost was larger than this item priced it: a drag paid **two** trailing debounces, 220 ms in
   the page and 600 ms in the write lane — 820 ms, not 600. Both stay (they are what collapses a drag's
   hundreds of frames into one write); a node release now runs the page's staged save immediately and asks
   the controller to flush rather than waiting out its timer. A camera pan or a tap commits nothing.
   Makes nothing faster — stops the launcher waiting before a round trip whose cost is unchanged.

   Two loose ends recorded rather than half-fixed: the page glue between the release event and `commitNow`
   has no widget test (both ends are pinned, the ~5 lines between them are not, and covering it needs the
   Flame canvas inside the full page); and the page debounce is still gated on whether a human-readable
   message string starts with `'Moved '`, so it coalesces by coincidence rather than intent. The honest
   shape is a `coalesces:` flag on the mutation.

6. **Scoped invalidation — name what changed instead of "re-fetch everything."** Upstream's `{dirs, full}`
   shape (R15). Turns every future uncoverable event from a cliff into a step, and stops the
   whack-a-mole this plan is an instance of. Bigger than D3 and touches the producer contract, but it is
   the change that makes foldability an optimisation rather than a precondition. ~~Also fixes the boot
   demote for free: one `office.surface.updated` currently sinks a 23-event startup batch.~~ —
   **struck 2026-08-17 by Plan EG §4.8 (EG-0.2 receipts): surface events are covered since WV-H3;
   the surviving defect is the sink DROP (EG-1.3), not a demote.**

7. **Collapse to one transport (D7 / R#42).** Now a *performance* item as well as a correctness one:
   every demoted batch is built **twice** — once for the serve hub's producer and once for the launcher's
   own stream request — over the same ~2,000 files. Pure waste either way.

   **Correction to an earlier draft, which said "twice *concurrently*, same disk, contention".** That is
   wrong in the normal topology, for two independent reasons, both verified in code rather than assumed:
   (a) the launcher's stream lane rides **inside the serve child** as an argv streaming request —
   `mission_control_provider.dart:240` injects `runMissionControlCommandStreamingPreferServe`, which
   spawns a separate `hermes harness stream` process only when there is no serve session or the child
   answers stale (`mission_control_serve_session_io.dart:1750-1775`); (b) within one process
   `build_snapshot()` is serialised by `_BUILD_COALESCE` (`snapshot.py:345-388`), so two demoted-batch
   builds run **back to back, never against the disk at once**. The disk-contention hypothesis for why
   builds hit the cold 6.9 s rather than the warm 1.2 s therefore holds only in the *fallback* topology,
   and should not be carried forward as the leading explanation. Measure before believing it.

8. **End-to-end correlation id** (gesture, RPC, events, batch, frame, fold). Every receipt today is
   per-lane; causality is inferred from timestamps. That inference **actively misled this investigation**:
   anchoring on the launcher's flush receipt (which lags the RPC by 250–650 ms) produced a "deletes take
   3.8 s" figure and, from it, a confident and wrong recommendation to prioritise prediction over D3. The
   cost of no correlation id is not slow diagnosis; it is fluent, confident, wrong diagnosis.

   **Cheaper than this item first priced it:** the read half of the wire is already built and contracted.
   `stream.py:157-168` surfaces `entity.correlation_id` from the event payload on every delta frame
   (`mission-control-stream.md:182`), and patch rows spread the payload verbatim (`stream.py:242-246`), so
   a correlation id present in a payload already reaches the launcher. The slot exists end to end and is
   populated by nothing on any gesture path. The work is minting and stamping, not plumbing.

9. **The page-open write storm.** Every Mission Office open re-upserts all 11 desk actors plus a surface
   write, off a 21-hour-old cache with five dropped predictions. ~~Its surface write demotes the boot batch
   every time.~~ — **struck 2026-08-17 by Plan EG §4.8 (EG-0.2 receipts): the demote claim is stale;
   the storm itself STANDS, and EG-0.2 §3 promotes it to the sole measured source of revision
   divergence (12/12 REVISION MISS).** The same shape — the launcher inferring server state and writing off that inference —
   caused R#40. Unowned, and the largest un-investigated behaviour left on this surface.

10. **The ~4.3 s `laneAbsent` window on every page open** (not just cold boot). A gesture inside it falls
    back to the CLI lane and raises the fallback toast. Unowned, and untouched by items 1–9.

**If only three:** D3 (removes the delay), the `elapsed_ms` log line (stops the next diagnosis costing a
night), prediction (makes it feel instant regardless).

### 10.4 Register — the numbers this document cites

This document and its four follow-on plans cite incidents, rulings and tasks by number. Those numbers
originated in a working session list and were **recorded nowhere on disk**: an agent searched both repos
and the live board store and found no definition for any of them. A reader of the committed docs got
dangling references. They are defined here, and this section is the authority until a real register
exists.

Cited as `R#nn` from here on, to make clear they resolve to this table and not to a GitHub issue.

| Ref | Kind | 2026 | What it is |
| --- | --- | --- | --- |
| **R#10** | task | 08-14 | `runtime.agent.create` — one atomic call minting roster row, placement and chat root together. Plan A. |
| **R#37** | incident | 08-15 | An office drag created the roster instance but never persisted the placement. The two halves came apart; the write lane emitted no receipt, so "never submitted" was indistinguishable from "submitted and refused". |
| **R#40** | incident | 08-15 | **Data loss.** One drag archived every other actor in the workspace — `_flush` treated absence from the in-memory layout as intent to delete, while the read path is designed to hand back degraded layouts as a normal outcome. Restored via `office actor-restore`. |
| **R#41** | defect | 08-15 | **Not ours.** The launcher's tombstone gate (`mission_control_tombstone_registry_test.dart`, "THE GATE") fails on clean `main`: tombstoned symbols reappeared in `lib/features/education/`. It is the single red test in every `test/features/mission_control` run quoted in this document. The gate is working correctly; the education lane owes either a rename or a retired registry row. **RESOLVED IN HALVES — ML-2 handover note, 2026-08-17.** "The gate is working correctly" was only half right: it was red because its `isUnavailable` row was written **unscoped**, banning the symbol repo-wide, so it reddened against four live and legitimate `lib/features/education/` uses (`media_availability.dart:101`, `lesson_video_surface.dart:278-279`, `video_source_providers.dart:117`) that were never part of the retired agent-chat wave. **The registry's own half is fixed:** ML-5 scoped the row to the surfaces the s40 wave actually retired — using the `scopes:` precedent already sitting one row down (`maxMessageLength`) — landing as launcher `6cc75dbd6`, with the education files untouched. **The remaining half is handed to the education lane (task #41): whether that lane wants its own `isUnavailable` naming.** That is a naming decision, not a gate defect, and nothing is blocked on it — the gate is green either way now that the row no longer reaches into that lane. ML-2 records this handover and deliberately does not absorb it. |
| **R#42** | ruling | 08-15 | Office becomes **RPC + push only**; every fallback lane is deleted once the main path is proven. Operator-stated accepted cost: *"no serve no office"*. Caching may return later as an optimisation, never as a fifth fallback. Plan C. |
| **R#43** | task | 08-15 | Client prediction + revision reconciliation on drag. The write's own reply is the acknowledgement; notifications are for other clients' changes. |
| **R#53** | task | 08-16 | No end-to-end correlation id — every receipt is per-lane, so causality is inferred from timestamps. Plan D. |

Two of these are load-bearing for the plans and should be read before the plans that depend on them:
**R#42** is the only recorded statement of the single-transport sequence, and **R#40** is the evidence
that motivates prediction (R#43) being a correctness change rather than UX polish.

The lesson worth keeping is narrower than "write things down": a number is not a reference. Every one of
these was cited confidently in committed prose while resolving to nothing — the documentary form of the
same error §10.2 records twice, an assertion whose backing was never checked.

---

### 10.5 D3, landed — the measurement, and where it deviated from the sketch

**The gate, measured.** §7's D3 entry and the `state_patches.py` header both said a full
`persona_instance` row is "~18 KB" against the 4,096-byte `EventLog.append` cap. That figure predated
R2's residue slimming, which evicted `tool_resolution` / `turn_tool_context` / `permission_state` /
`blocked_tools` — ~97% of the row's bytes — behind a typed `visibility_ref`. Re-measured against the
operator's live roster (17 instances, copied read-only into an isolated probe; nothing written under
`.hermes`):

| | bytes | budget |
| --- | --- | --- |
| largest complete row | 3,012 | — |
| largest assembled `{entity,id,op,changed,created}` payload | **3,133** | 4,096 (`EVENT_PAYLOAD_LIMIT_BYTES`) |
| largest single value (`skills`) | 504 | 3,584 (`PATCH_VALUE_BUDGET_BYTES`) |

Headroom is ~960 bytes, i.e. ~24%. That is real but not unlimited, and it is a property of
`persona_instance_summary`'s field list — which moves. So the number is re-taken by a test
(`test_open_chat_create_row_fits_the_payload_cap_with_headroom`) rather than trusted, and the emitter
degrades per row rather than assuming.

**The one deviation from the sketch.** §10.3 described D3 as "add create-on-absent to the launcher's
generic `persona_instance` fold behind a second capability token — the same V4 pattern that shipped
cleanly for `office_actor`". The pattern is the same, but one detail is NOT, and it is load-bearing:

* For `office_actor` the launcher's fold inserts on absent **unconditionally**, and `created` is purely
  hermes' coverage gate (`build_state_patch`'s docstring says so). That is safe because every office
  upsert already carries the complete row — the store has no per-field office write.
* For `persona_instance` the upserts are **subsets** (the fields one steer/profile write moved). An
  unconditional insert-on-absent would assemble a roster row out of whichever three fields happened to
  move. So the launcher's generic fold **reads** `created`: insert (and REPLACE) only when it is
  literally `true`, `patch_without_target` otherwise. The stamp is part of the fold contract here, not
  only part of the negotiation.

A second deviation, smaller: `build_state_patch`'s oversize shrink loop is correct for a subset upsert
(mark the value, the client refetches that field) and **wrong for a create**, where the marker would
become the inserted row's value — a fabricated roster row rather than an accounted degrade. So
`emit_persona_instance_create` treats a create as all-or-nothing: any marker, or any degrade the loop
already made, and the whole patch falls back to `refresh`. The worst case is therefore exactly the
pre-D3 wire, never a corrupt insert.

**Contract movement: none.** `created` was already an optional key on the `state.patched` contract
(O-H1), so `decision_contract_hash` does not move and no generated golden changed — verified by running
`scripts/generate_agent_runtime_stream_fixtures.py` and getting an empty diff on every frame. The one
committed byte change is the hand-maintained `patch_coverage_manifest.json`, which gains a
`persona_instance.create` case, mirrored byte-identically into the launcher with both `MANIFEST.sha256`
files re-pinned and cross-checked from both sides.

**What is NOT verified.** Nothing was measured against a live runtime — no `harness serve` child was
spawned and the operator's running launcher was not disturbed, per this task's constraints. The 6.94 s →
sub-second claim rests on §10.1's measurement of where the time goes plus the classifier now promoting
the create batch; the live receipt (`folded ≥ 1` on an add gesture, no `resubscribe #N (push:full_core)`)
is the operator's acceptance check, unrun.
