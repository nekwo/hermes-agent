# Planned — realm-sync writes that never reach a live projection

**Status:** PLANNED. Nothing here is implemented. **Domain:** runtime data and
shapes / stream + office delivery. **Opened:** 2026-08-30, from a live report on
the operator's machine plus a read-only evidence pass over the live store.

**Repos:** hermes-agent (H1–H4), EterniaLauncher (L1–L3). The two halves touch
different git indexes and are safe to run in parallel.

---

## 0. Correction to the reported premise — READ THIS FIRST

The lane was opened on this report:

> a realm-sync pull (realm `cf6d244d-…`, workspace `ws_testv4_afb811`) adopted a
> persona instance created on another PC:
> `personainst_neko_supervisor_agent_2a26ddcc` … the launcher still shows "No
> active agents", the console does not initialize, the agent is not on the level.

**No pull happened.** Three independent measurements, all read-only, all taken
2026-08-30 against the live store `X:\Eternia\.hermes\agent-runtime`:

1. **The status sidecar says so.**
   `realm_sync_state/cf6d244d-7cfa-4fa5-bab9-1401c8493b23.json` carries
   `"last_pull": null` and `"last_publish": "2026-08-30T00:44:14.997213-04:00"`.
   `last_pull` is written by `realm_sync._write_timestamp(repo, "last_pull.txt")`
   (`agent_runtime/realm_sync.py:513`) on every completed pull; a null there is
   "this store has never completed a pull under the current sidecar".
2. **The EventLog says so.** Every completed pull appends `realm.sync.pulled`
   (`realm_sync.py:549` → `_append_realm_sync_event`, `:558`). The live slice
   (`events_archive/events.81417412.jsonl`, per `events_manifest.json`) holds
   exactly three, and the newest is **2026-07-19T08:35:10Z**. The only realm
   events on 2026-08-30 are two `realm.sync.published` (03:55:49Z and
   04:44:15.375650Z — the latter is the live tail, offset 91576451).
3. **The instance's own birth certificate says so.** `2a26ddcc` was created
   **locally, by the launcher's own drop gesture**, at 04:40:15Z:

   | ts (UTC) | event | the tell |
   | --- | --- | --- |
   | 04:40:15.947365 | `state.patched` `persona_instance` `…2a26ddcc` `upsert` | full row minted |
   | 04:40:15.949365 | `persona_instance.chat_opened` | `chat_head_home = X:\Eternia\.hermes\profiles\base` |
   | 04:40:15.990362 | `state.patched` `office_actor` `ws_testv4_afb811/…2a26ddcc` | `"created": true`, `revision: 1`, `unpublished: true` |
   | 04:40:15.991363 | `office.actor.upserted` | `correlation_id: "g-place-1788064814691623-1a56"` |
   | 04:40:16.014364 | `state.patched` `office_actor` … | `revision: 2` |
   | 04:40:16.016361 | `office.actor.upserted` | `correlation_id: "g-office-1788064814662637-1a55"` |
   | 04:40:35.807694 | `run.progress` | `"Hi Tony — Neko here. What's the mission?"` |
   | 04:40:43.834114 | `persona_chat.projected` | second turn committed |

   `g-place-…` / `g-office-…` are the LAUNCHER's own gesture correlation ids.
   The agent was dropped on this machine, the console **did** initialize, and
   the operator had two working chat turns with it. It was then published
   OUTBOUND at 04:44:15 (`realm.sync.published`, `artifacts: 38`).

**What the operator most plausibly saw, in time order.** 04:41:13Z
`realm.activated realm_default` + `workspace.activated ws_default` → they left
the realm; 04:41:33Z they dropped a SECOND neko instance (`…f6844ba8`) in
`ws_default` and chatted with it; 04:43:02 local a **second serve child** was
spawned (§4); 04:43:50Z `realm.activated cf6d244d…` +
`workspace.activated ws_testv4_afb811` → they came back; 04:44:15Z publish. The
symptom is therefore **"the workspace I switched back to did not re-project"**,
not "a pull did not project". That is a different lane from the one this file was
opened for, and §L1 is the stage that instruments it rather than guessing at it.

**None of this makes the lane empty.** The read that falsified the premise also
proved four real defects — three of them latent in the pull path the report
*thought* it was describing, one of them firing on the operator's screen right
now. They are §1–§4 and they are what the stages fix.

---

## 1. The office pull's ADOPT arm writes no event; its ARCHIVE arm does

`agent_runtime/office_sync.py:303` `apply_office_pull` builds an `OfficeStore`
(`:312`) **and uses it only for reads** — `surface_exists`, `scan_actors`,
`get_surface` (`:343`, `:369`). Every adopting write bypasses it:

* `:387-389` — the surface is written with `atomic_json_write(paths.office_surface_path(...))`.
* `:414-419` — `PullAction.WRITE_REMOTE`, **the arm that adopts a remote actor**,
  is `atomic_json_write(paths.office_actor_path(workspace_id, actor_key))`.

`OfficeStore` is documented as emitting "a typed `EventLog` event on EVERY
mutation" (`agent_runtime/office_store.py:6`, `_emit` at `:350`,
`_emit_actor_patch` at `:379`, `_emit_surface_patch` at `:471`). A raw
`atomic_json_write` emits nothing. So:

* a pull that **archives** a desk goes through `store.remove_actor(...)`
  (`office_sync.py:443`) and DOES emit `office.actor.removed` + `state.patched`;
* a pull that **adopts or converges** a desk emits **nothing at all**.

The asymmetry is the defect in one sentence: *a realm pull that deletes your
desk is visible to every live consumer; a realm pull that gives you a desk is
invisible to all of them.*

## 2. The office subscribe lane is deaf to `realm.sync.pulled` (and `.published`)

`agent_runtime/serve_office_subscriptions.py:391` `office_patch_sink` decides
whether a stream frame is worth a `runtime.office.resync`
(`OFFICE_RESYNC_METHOD`, `:266`) push to the subscriber. For a coalesced `delta`
it consults `_delta_touches_workspace` (`:285`), which answers **True** on only
two shapes (`:329-341`):

* an `office.*` event whose payload names this `workspace_id`;
* a `state.patched` whose `office_patch_scope(payload)` is this `workspace_id`.

`realm.sync.pulled`'s payload is `{"realm_id", "changed", "artifacts"}`
(`realm_sync.py:572-576`) — no `workspace_id`, no office scope. It falls through
the loop to `return False` (`:342`), and the sink returns early at `:457`
without pushing a resync.

So even the watermark event that `_append_realm_sync_event`'s own docstring says
exists "so stream / read-model consumers refresh" (`realm_sync.py:559-561`) does
**not** refresh the one consumer that owns the office canvas. Combined with §1 —
whose writes are event-less anyway — a realm pull is invisible to the office lane
through both doors.

`realm.sync.published` takes the same fall-through. This one fires on the
operator's machine **today**: `unpublished` is a DERIVED snapshot field probed
against the office baseline (`snapshot.py:951`, and the board twin at
`:1620-1622`), so `realm sync publish` flips every actor's `unpublished` marker
by writing `office_baseline.json` — with no office event. The read-model lane
recovers because `realm.sync.published` advances the offset and forces a core
rebuild; the office subscribe lane does not, so a published canvas keeps
rendering "unpublished" desks until something else moves.

## 3. "projection drops 1" is HONEST, CURRENT, and undiagnosable

Measured from two fresh `harness snapshot --json` runs (one per home; both
resolve `runtime_root` `X:\Eternia\.hermes\agent-runtime`, watermark
`event_offset: 91576451`):

```
"persona_chat_history": {
  "by_design": ["instance_retired", "limit"],
  "considered": 202, "dropped": 152, "included": 50,
  "reasons": {"instance_retired": 132, "limit": 19, "session_not_in_db": 1}
}
```

`152 − 132 − 19 = 1`. The launcher's
`MissionSnapshotEnvelope.anomalousDroppedProjectionCount`
(`lib/features/mission_control/data/mission_control_snapshot.dart:420-433`)
subtracts exactly the row's own declared `by_design` codes and renders the
remainder as `projection drops $dropped`
(`mission_control_snapshot.dart:1109-1121`). **The chip is right.**

Two facts follow, and they answer the question the lane was asked:

* **It is not a persistent counter.** The label is computed from
  `envelope.anomalousDroppedProjectionCount` on the CURRENTLY HELD envelope,
  every rebuild, with no accumulator anywhere. A single clean envelope clears it.
  There is nothing to "clear on boot" — the correct fix is to stop producing the
  drop, not to reset a tally.
* **It discloses nothing.** `MissionSnapshotAlert.dropSummaries` is fed from
  `envelope.anomalousDropSummaries`, which filters `parity.drops` through
  `_isAnomalousDropSample` (`:577-596`). But `parity.drops` is capped at
  `_MAX_DROP_SAMPLE = 50` (`agent_runtime/parity.py:57`) and the cap is FIFO
  (`parity.py:132-133`), so on this store all 50 sample slots are consumed by
  `by_design: true` `instance_retired` records and the ONE anomalous drop has no
  sample row at all. `hasDetail` is false; the operator gets a bare amber chip
  with no rows behind it. Naming the offender took a Python pass over a saved
  snapshot — which is precisely the cost EG-6.5 shipped `dropSummaries` to remove.

The producer is `persona_chat_history.py:441` — an instance whose
`chat_session_id` points at a row SessionDB no longer holds. Its own comment
already names the repair verb: `PersonaInstanceStore.repair_missing_chat_session_bindings`
via `harness persona-instance reconcile` (`persona_chat_history.py:434-439`). It
is a WRITE, so it is the operator's call, not an agent's. Candidates, derived by
joining `persona_instances[*].chat_session_id` against the 50 history rows in the
fresh snapshot (three instances whose bound session is absent from the projected
list — one of them is the offender, the other two are `limit` casualties):
`personainst_backend_dev`, `personainst_chara_a2_7b31d0e4`,
`personainst_profile_alice`. H4 makes this a one-glance answer instead of a
join.

**Two theories checked and killed, recorded so nobody re-runs them.**
(a) The drop is NOT the `2a26ddcc` chat root — that session is present in
`persona_chat_history` with `message_count: 4` and a real title. (b) The office
actor is NOT accounted by any `ProjectionAccountant`; the only four are
`persona_chat_history`, `persona_chat_trace`, `operator_conversation`,
`running_work` (`snapshot.py:900, 998, 999, 1021`), so no office/roster row can
ever appear in this chip.

## 4. Two live serve children, one socket, one store

`X:\Eternia\.hermes\agent-runtime\serve_instances\` holds two live descriptors,
and both processes are running:

| pid | started (UTC) | transport | port | build |
| --- | --- | --- | --- | --- |
| 36140 | 2026-08-30T04:32:56.768Z | `stdio+socket` | 64494 | `7bbf6db3be` |
| 35232 | 2026-08-30T04:43:05.001Z | `stdio` | — | `a2837bb209` (HEAD) |

Same `store_root`, same `hermes_home` (`…\.hermes\profiles\base`), different
`boot_id`, different commit (one apart — the build delta is not itself
load-bearing). `X:\Eternia\.hermes\profiles\base\logs\agent.log` shows both
building cores against the same offsets from 00:43:56 onward (pid 36140
`generation=21..24`, pid 35232 `generation=2..5`), so this is not a zombie: two
serves are paying for the same work, and the **older** one owns the RPC socket
the office/`runtime.office.get` lane dials.

The younger child spawned at 00:43:02 local — 48 s before the operator's
`workspace.activated ws_testv4_afb811` at 04:43:50Z. That is the exact window the
symptom lands in, which makes this the single most promising live lead for §0's
"switched back and it did not re-project", and the reason L1 instruments the
switch rather than the pull.

---

## Staged implementation

### H1 — the office pull's adopt arm must emit (hermes)

**File:** `agent_runtime/office_sync.py`.

1. Replace the two raw adopting writes with the store's own evented verbs:
   * `:387-389` (surface) → the `OfficeStore` surface-write verb that reaches
     `_emit_surface_patch` (`office_store.py:471`).
   * `:414-419` (`PullAction.WRITE_REMOTE`) → the `OfficeStore` actor-upsert verb
     that reaches `_emit_actor_patch` (`office_store.py:379`).
   The `store` at `:312` is already constructed with the caller's `event_log`, so
   the seam exists — this is a call-site change, not new plumbing.
2. **Preserve three properties the raw writes have and the verbs must not lose.**
   Each needs a test, not a comment: (a) the adopted row's `revision` must be the
   REMOTE's, not a local increment — a pull that renumbers revisions breaks the
   next three-way classify (`classify_three_way_pull`); (b) `updated_by` must
   record the sync, not `"operator"` — the ARCHIVE arm already passes
   `updated_by="realm_sync"` (`:443`) and the adopt arm must match it; (c)
   `baseline[key] = remote_hash` (`:420`) must still be keyed off the REMOTE
   content hash, not a re-read of what the store wrote back.
   If any verb cannot honour (a)–(c), the correct move is to widen the verb with
   an explicit parameter — never to keep the raw write and append an event beside
   it, which is the "two writers, one truth" shape the office lane already paid
   for once.
3. **Test:** extend the office-pull tests (the neighbours of
   `tests/agent_runtime/test_realm_sync.py`'s office fixtures — find them with
   `grep -rn "apply_office_pull" tests/`). Assert, on a pull that adopts one new
   remote actor: `office.actor.upserted` and `state.patched` (`entity:
   office_actor`) both appear in the event log for that `workspace_id`, the
   adopted revision equals the remote's, and `updated_by == "realm_sync"`.
   Assert the archive arm's existing events are unchanged.
4. **Focused run:** `python -m pytest tests/agent_runtime/test_realm_sync.py tests/agent_runtime/test_office_sync.py -q`
   (substitute the real office-sync test file name).
5. **Live proof:** with the launcher's office page open on a workspace, run a
   pull that adopts one desk; the desk appears on the canvas with no page
   navigation and no restart, and the run leaves `office.actor.upserted` in the
   live slice.

### H2 — teach the office sink that a realm sync touches every workspace

**File:** `agent_runtime/serve_office_subscriptions.py`.

1. `_delta_touches_workspace` (`:285`) gains a THIRD true-arm: an event type in a
   declared realm-sync set (`realm.sync.pulled`, `realm.sync.published`) returns
   `True` unconditionally. Rationale to write into the docstring beside the
   existing two arms: a realm sync is scoped to a REALM, and this function is
   asked about a WORKSPACE — the payload cannot answer the question, and the
   module's own stated rule for a question it cannot answer is the conservative
   arm ("A resync is recoverable; a dropped change is not", `:459`). It is
   therefore the same ruling `None` already gets, spelled as an explicit `True`
   so the reason is legible.
2. **Do NOT widen the payload instead.** Adding `workspace_ids` to
   `realm.sync.pulled` was considered and rejected here: the EventLog has a 4 KB
   payload cap this lane already reasons about (`:509`), a realm with many
   workspaces would blow it, and a truncated list is a silent drop — the exact
   failure class the `None` arm exists to refuse. The conservative arm costs one
   refetch per realm sync, which is bounded by operator gestures.
3. **Test:** `tests/agent_runtime/test_serve_rpc_office_subscribe_live_hub.py`
   already hand-builds frames for this sink. Add: a `delta` carrying only
   `realm.sync.pulled` produces exactly one `runtime.office.resync` for a
   subscribed workspace; a `delta` carrying only an unrelated event (a board
   write, another workspace's office) still produces none — the O-H4 scoping
   must not regress.
4. **Focused run:**
   `python -m pytest tests/agent_runtime/test_serve_rpc_office_subscribe_live_hub.py -q`.
5. **Live proof:** the H1 proof above, but read from the serve's side — the pull
   emits one `runtime.office.resync` per subscribed workspace and no more.

### H3 — reserve drop-sample slots for anomalous drops

**File:** `agent_runtime/parity.py`.

1. `ProjectionAccountant.drop` (`:109`) currently appends to `self._drops` only
   while `len(self._drops) < _MAX_DROP_SAMPLE` (`:132`), FIFO. Split the budget:
   keep the total at 50, but reserve a floor for `by_design=False` records so a
   by-design flood can never starve them. Simplest honest shape: two lists
   (`_drops_anomalous`, `_drops_by_design`), each with its own cap, concatenated
   by `drop_samples()` (`:170`) with the anomalous ones FIRST.
2. **The contract does not move.** `drop_samples()` keeps returning
   `list[dict]` in the same record shape, `dropped` keeps counting EVERY drop,
   and `reasons` stays unbounded — the launcher's `_isAnomalousDropSample`
   (`mission_control_snapshot.dart:577`) needs no change and MUST need none.
   Write that in the docstring: this is a sampling-policy change, not an
   envelope-shape change.
3. **Test:** a new pinning test beside the existing parity tests — 60 by-design
   drops followed by 1 anomalous one yields a sample list whose first record is
   the anomalous one, total length ≤ 50, and `reasons` still counts all 61.
4. **Focused run:** `python -m pytest tests/agent_runtime/ -q -k parity`.
5. **Live proof:** a fresh `harness snapshot --json` on this store shows a
   `parity.drops` record with `"code": "session_not_in_db"` and a nameable
   `entity_id` — the fact that currently takes a Python join to recover.

### H4 — name the stale binding in the reconcile verb's own output

**File:** the `harness persona-instance reconcile` command surface
(`repair_missing_chat_session_bindings`'s caller — locate with
`grep -rn "repair_missing_chat_session_bindings" hermes_cli/ agent_runtime/`).

1. Add a **read-only** dry-run view (`--json`, no `--apply`) that lists every
   instance whose bound `chat_session_id` is absent from SessionDB, so an
   operator (or an agent under the read-only evidence rule) can answer "which one
   is the amber chip" without a write and without a snapshot join.
2. **Test:** a fixture store with one stale binding and two healthy ones reports
   exactly the stale one and mutates nothing (assert the store's mtimes /
   the event log length are unchanged).
3. **Focused run:** the persona-instance test file the verb already has.
4. **Live proof:** run it on this store; it names one of
   `personainst_backend_dev` / `personainst_chara_a2_7b31d0e4` /
   `personainst_profile_alice`, and the operator's own `--apply` afterwards
   drops `projection drops 1` off the chip strip on the next frame.

### L1 — instrument the realm/workspace switch (launcher)

**Files:** `lib/features/mission_control/sync/realm_sync_service.dart`,
`lib/features/mission_control/data/mission_transport_receipt.dart`,
`lib/features/mission_control/data/mission_forced_refresh_policy.dart`.

This is the stage that turns §0's "switched back and it did not re-project" from
a guess into a measurement. **Measure before fixing** — a filed row is often
right that something is wrong and wrong about why.

1. **Establish what fires today.** The forced-refresh chokepoint
   (`mission_control_provider.dart:1747` `_requestForcedRefresh`) is the only
   latch writer and it bills a receipt on every consult, DECLINE included. There
   is **no poll timer** on the snapshot lane —
   `MissionControlSnapshotNotifier.build` (`:1808`) is push-driven plus the
   one-shot force flag (`:1838-1840`). So after a workspace switch the surface
   repaints only if (a) the serve pushes a frame, or (b) someone routes a forced
   refresh. `realm_sync_service.dart` routes one after **pull** (`:656`),
   set-default-workspace (`:1136`), adopt-definition (`:1197`) and accept-grant
   (`:1481`) — and after **neither publish nor a realm/workspace activation**.
2. **Add the reason arm, do not smuggle in behaviour.** If the census shows the
   switch takes no forced read, add a `MissionForcedRefreshReason` member for it
   and give the policy table an explicit arm with its argument written at the
   arm, in the style of the existing entries. `realmSyncSettled` already
   documents the shape: "A realm pull/adopt rewrites store state from outside the
   local EventLog" (`mission_forced_refresh_policy.dart:98-100`). A realm/
   workspace activation DOES emit events (`realm.activated`,
   `workspace.activated` — both present in the live log at 04:43:50Z), so the
   honest arm may well be `streamDriven`; if it is, the defect is downstream and
   this stage's deliverable is the receipt proving it, not a forced read.
3. **Test:** the service's existing fake bridge/channel seams — assert exactly
   one receipt with the new reason per switch, and zero extra snapshot loads when
   the policy answers `streamDriven`.
4. **Focused runs:** `flutter analyze lib/features/mission_control/sync` and
   `flutter test test/features/mission_control/realm_sync_widgets_test.dart`
   plus the forced-refresh chokepoint test
   (`mission_forced_refresh_chokepoint_test.dart`) — it derives from the AST that
   no second latch writer exists, so a new reason must keep it green.
5. **Live proof:** switch realm away and back with the transport-receipts drawer
   open; the switch leaves a receipt naming its reason and its mode.

### L2 — one serve child per store, and say so when there are two

**Files:** `lib/features/mission_control/data/mission_control_serve_session*.dart`.

1. **Measure first.** `serve_instances/*.json` carries `pid`, `boot_id`,
   `transport`, `port`, `store_root`, `hermes_home`, `started_at`. Two live
   descriptors for one `(store_root, hermes_home)` pair — with the OLDER owning
   the socket — is either a supervision defect or a designed handoff whose
   cleanup is missing. Read the session code before deciding which; do not patch
   the spawn path on this file's say-so.
2. **The floor deliverable is honesty, not a kill.** Whatever the ruling, a
   launcher that finds a second live descriptor for its own store must SAY so
   (a warning chip / a transport receipt naming both pids and which one owns the
   socket). Silently racing two serves is how "the canvas is stale and nothing on
   screen can tell you why" happens. **Do not kill the operator's running serve
   as part of this stage.**
3. **Test:** a session test with two descriptor fixtures asserts the warning is
   raised and names both pids; one descriptor raises nothing.
4. **Live proof:** the operator's current machine already reproduces it — pids
   36140 (socket, 04:32:56Z) and 35232 (stdio, 04:43:05Z).

### L3 — the amber chip must always carry its rows

**File:** `lib/features/mission_control/data/mission_control_snapshot.dart`.

Strictly the client half of H3, and it lands only AFTER H3 (there is nothing to
render until the producer ships the sample).

1. When `anomalousDroppedProjectionCount > 0` and `anomalousDropSummaries` is
   empty, the chip must say so explicitly rather than rendering as a bare label
   with `hasDetail == false` — "1 drop, sample not shipped by this runtime" is a
   diagnosis; an unexplained amber pill is not. This arm survives H3 as the
   honest fallback for an older harness.
2. **Test:** `mission_control_snapshot`'s envelope tests — an envelope with
   `dropped: 152`, `by_design: ["instance_retired","limit"]`,
   `reasons.session_not_in_db: 1` and a `drops` list of 50 by-design records
   yields count 1 and the explicit no-sample disclosure.
3. **Focused run:** `flutter test test/features/mission_control/` scoped to the
   snapshot envelope test file.
4. **Live proof:** with H3 landed, the chip's hover names
   `persona_chat_history · session_not_in_db · <instance>`.

---

## Ordering and independence

* **H1 → H2** is the natural pair (H1 makes the pull emit; H2 makes the sink
  listen), but they are independently correct: H2 alone fixes the
  `realm.sync.published` half that fires today, and H1 alone fixes any consumer
  that tails `office.*` directly.
* **H3 → L3** is a strict order.
* **H4** and **L1** and **L2** are independent of everything else.
* **Nothing here depends on `realm-sync-outbound-drift-and-live-checks.md`.**
  That plan is about *detecting* drift in the status envelope (S1 landed as
  `19f3aee24a`); this one is about *delivering* sync writes to live surfaces.
  They meet only at the office store and do not touch the same functions —
  S1 added a read-only `_office_store_drift` to `realm_sync_status`; H1 changes
  `office_sync.apply_office_pull`'s write arms. Do not merge them.
* `boot-resubscribe-fourth-core.md` (HY-L2) is likewise adjacent and untouched:
  it is about suppressing a redundant BUILD behind a resubscribe. H2 ADDS
  resubscribes (one per realm sync), so whoever takes HY-L2 should note that its
  position-comparison suppression must not swallow an H2 resync — the offset DOES
  advance across a realm sync, so it will not, but the test belongs in HY-L2.
* `core-cache-input-closure.md` was the report's prime suspect and is
  **exonerated for this lane**: the cache's fingerprint walk is directory-level
  over `paths.store_root()` (`core_cache.py:1615-1628`, and the census rule at
  `:707` says so in as many words), so `persona_instances/`, `office/` and
  `realms/` writes — including a pull's raw ones — all move the key. The cache
  cannot serve a core that predates a pull's writes. Its own open gap
  (`never_converged`) costs boot time, not correctness, and is not this file.

## Discipline

`harness-dev-delivery` applies. Narrow patches, focused self-tests only (never a
broad suite), commit exactly your slice, report command + exit status honestly,
**no push**. The two repos have separate git indexes and are safe in parallel;
within one repo, concurrent sessions share ONE index — stage and commit in one
breath, exact paths only.

Launcher-side running record:
`Launcher_Brain/20 — Active Initiatives/realm-pull-live-projection-notes-2026-08-30.md`.

---

## Field notes (hermes side, 2026-08-30)

Running record of the read-only investigation, in the order it happened. Kept
because three of these are dead ends someone else would otherwise re-walk.

1. **Read the two fresh snapshots first, not the code.** Both
   `harness snapshot --json` captures (one per home, both resolving
   `runtime_root` `X:\Eternia\.hermes\agent-runtime`, both at watermark
   91576451) contain `offices.ws_testv4_afb811` with the actor at position
   `[0.9048, -10.6647]`, `persona_instances[…2a26ddcc]` complete, and the chat
   root in `persona_chat_history` with `message_count: 4`. The producer was
   never the problem. That single reading moved the whole investigation to the
   delivery lane — and, later, off the pull lane entirely.
2. **The report said "ZERO non-by-design drops". It was wrong**, and the
   arithmetic is in §3. Worth stating plainly because the same envelope was read
   by two people and only the subtraction distinguishes them: `dropped` counts
   every drop, and `by_design` is a per-row DECLARATION, not a global allowlist.
3. **Dead end — the core cache.** Chased first because the report nominated it
   and two planned docs describe it as broken. It is broken (it does not
   converge) and it is not this: the walk is directory-level over the store root,
   so every store a pull writes is inside the closure. Recorded in §"Ordering"
   as an exoneration so the next reader does not re-open it.
4. **Dead end — the roster's conversational-channel dedup.** Very promising for
   ten minutes: `_dedupeConversationalInstances`
   (`…/data/mission_agent_roster_policy.dart:421`) folds chat instances by
   `(personaId, displayName)`, and this store has THREE `neko_supervisor` rows
   all displaying "Neko Mission Lead", two of them in `chat` mode
   (`…_agent_2a26ddcc` in `ws_testv4_afb811`, `…_agent_f6844ba8` in
   `ws_default`). It is not a collision:
   `missionInstanceIdIsDeliberatePlacement` (`mission_agent_identity.dart:220`,
   regex `_agent_(\d+|[0-9a-f]{8})$` at `:121`) matches BOTH ids and exempts them
   at `mission_agent_roster_policy.dart:438-441`. The exemption exists for
   exactly this store — its comment names the 2026-07-20 incident where folding
   by display name dropped the row the office actor targets.
5. **Dead end — "No active agents".** It is not the office roster. The only
   producer is `mission_running_work_view.dart:434`, the running-work activity
   chip, which reads `running_work` — `considered: 0, included: 0` in both fresh
   snapshots, i.e. nothing is running, which is TRUE. The operator's sentence and
   the launcher's sentence are about different things.
6. **`harness flow list` has no `runtime_…2a26ddcc` doc, and that is correct.**
   The launcher seeds one lazily on first open —
   `agent_flow_graph_editor.dart:386`
   `seedIfAbsent(AgentFlowGraph(graphId: 'runtime:${agentId}'))`. Console init
   does not require it. **Ruling: realm sync must NOT mint or sync `runtime_*`
   flow docs.** They are per-install editor state keyed to a local instance id;
   syncing them would replicate one member's graph onto every member's install
   under an id that means something different there. No stage.
7. **The premise fell on the third check, not the first.** The sidecar's
   `last_pull: null` was decisive; the EventLog's July-19 last `realm.sync.pulled`
   confirmed it; the `g-place-…` correlation id on the 04:40:15 office event
   explained what really happened. Order matters: the sidecar is one file and one
   `cat`, and it should have been read first.
8. **The two-serve finding was incidental** — it fell out of grepping
   `agent.log` for `snapshot_build_core` and noticing two `pid=` values
   interleaving at the same offsets. It is in §4 and L2 because it sits exactly
   in the symptom's time window, not because anything proved it causal.

---

## Delivery notes — hermes H1–H4 (Backend Dev, 2026-08-30)

Running record of the build, appended as each stage landed. Falsified
assumptions are recorded the moment they falsified.

### H1 — landed

**What the plan said and what was actually possible.** Step 1 says route the two
adopting writes through "the ``OfficeStore`` surface-write verb" and "the
``OfficeStore`` actor-upsert verb". **Neither existing verb can take this write**,
and the plan's own escape hatch (step 2: "widen the verb with an explicit
parameter") turned out to be the wrong shape too — the divergence is not one
parameter, it is four:

* ``upsert_actor`` mints ``base_revision + 1`` (breaks property (a)),
  re-canonicalizes the actor key through ``_canonical_actor_key`` (would rewrite
  a peer's identity), and spends three fences that exist to refuse LOCAL
  authoring intent — the class-key fence, the tombstone fence, and the
  duplicate-desk fence whose own docstring (``office_store.py:1539``) says in as
  many words that "Realm pull is deliberately NOT behind this fence". Routing
  the pull through it would have silently decided task #33 and changed what a
  realm pull means.
* ``update_surface`` lazily creates the surface through ``ensure_surface``,
  which REFUSES a workspace with no local record (``WorkspaceUnresolved``) — and
  a pull is exactly how such a workspace arrives. It also bumps the revision and
  re-normalizes the folder list.

So H1 landed as two NEW store verbs, ``OfficeStore.adopt_remote_surface`` and
``OfficeStore.adopt_remote_actor``: still the store chokepoint, still the store's
lock, still the store's emitters, but writing a peer's record verbatim the way
``resolve_conflict(take="remote")`` already does. The three preserved properties
each have a test.

**Property (b) is free, and the plan did not say why.** Stamping
``updated_by="realm_sync"`` cannot disturb property (c), because
``office_models._HASH_EXCLUDE`` drops ``revision``, ``created_at``,
``updated_at`` and ``updated_by`` from ``office_content_hash``. Worth writing
down: if it were hashed, (b) and (c) would be in direct conflict — the baseline
would be keyed off a hash the file on disk no longer has, and the NEXT pull
would read every adopted desk as a local edit.

**The pinning test moved and had to.**
``tests/agent_runtime/test_office_class_key_one_fence.py`` derives from the AST
the set of functions that write a live actor file and compares it for equality
against a hand-maintained disposition table. H1 moves the write, so the
carve-out entry moved with it: ``("agent_runtime/office_sync.py",
"apply_office_pull")`` → ``("agent_runtime/office_store.py",
"adopt_remote_actor")``, ruling text unchanged and annotated with the
relocation. ``test_the_carve_out_is_a_live_hole_and_not_a_stale_note`` now
asserts BOTH halves — the pull still routes to the carved-out verb, AND the verb
is still unfenced — because asserting only the second half would pass on the day
someone quietly pointed the pull at ``upsert_actor``, which is a #33 ruling
wearing a refactor's clothes. **Task #33 is still open. H1 changed where the
unfenced write lives and what it emits, not whether it is fenced.**

**Gotcha for the next test author.** ``EventLog()`` is the STORE's log, not the
instance's — every construction reads the same file — so "what did this pull
emit" has to be a tail sliced from a mark taken before the call. Filtering the
whole log passes by luck on the first assertion and reds on the second.

**Focused run:** ``python -m pytest tests/agent_runtime/test_office_sync.py
tests/agent_runtime/test_office_class_key_one_fence.py -q`` → 35 passed.
Neighbours re-run because they drive the same function: ``test_realm_sync.py
test_sync_admission.py test_office_class_key_guard.py -q`` → 97 passed.

**Live proof NOT taken.** Two serve children are live on the operator's store
(§4) and were deliberately not restarted; the emission takes effect on the next
serve restart. The office-canvas proof in H1 step 5 is owed and is the
operator's to take.
