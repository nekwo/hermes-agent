# Scoped invalidation — uncoverable stops meaning "re-fetch everything" (Plan B, 2026-08-16)

> **Home.** Hermes repo, beside the fold-promotion plan whose §10.3 item 6 is
> the ask and whose R15 names the upstream shape (`{dirs: Set<string>, full:
> bool}` — an invalidation that NAMES ITS SCOPE). Evidence tags: READ / RAN /
> MEASURED-§10 / RELAYED / ASSUMPTION — defined in
> `AGENT_CREATE_ONE_CALL_PLAN_2026-08-16.md` and used identically here.

**Verdict up front.** Today one uncoverable event demotes its whole batch to
an unscoped `build_snapshot()` (~6.3–6.6 s of the create's 6.94 s,
MEASURED-§10) even when the server can enumerate exactly what the batch
touched — the delta frame already carries its `events` list, and the batch
enumerability is the same fact O-H4 exploited for the office lane. The right
shape is a **section-scoped core**: the demoted frame carries only the
snapshot sections the batch's events can have dirtied, plus a `core_sections`
list naming them, gated behind a `scoped_core` capability token in the
existing declaration channel so every mixed pair degrades to today's full
core. **Two things temper the ambition, and this plan states them as gates
rather than hiding them.** First, the snapshot's sections are NOT independent:
several cheap-looking sections take `persona_instances` (and more) as inputs
(READ, §2 V3), so the dirty map must carry a dependency closure, and the
closure can eat the win for persona-heavy batches. Second, therefore, **stage
SI-0 is a measurement gate that can kill or reshape this plan** — the scope
vocabulary is designed against the measured per-section cost map
(`sections_ms` is already instrumented, `snapshot.py:281-292,694-702` READ),
not against section names. What survives regardless: the boot demote fix (one
`office.surface.updated` sinking a 23-event startup batch buys a full core
today and an offices-scoped one after), and the structural point — foldability
becomes an optimisation, not a precondition, ending the per-event
whack-a-mole the fold-promotion plan is itself an instance of.

## 0. The ask

Fold plan §10.3 item 6. Upstream's instructive pattern (R15, RELAYED via the
2026-08-16 upstream research): invalidation names `{dirs, full}`; a consumer
refetches the named scope; `full` stays available for the unenumerable case.
Our analogue of upstream's `dirs` is not filesystem dirs — it is the
top-level SECTIONS of the read-model core.

## 1. Baseline (READ unless noted)

**The cliff.** `_batch_frames_with_liveness` (`stream.py:338-360`): a batch
that fails `batch_is_patch_coverable` yields `_full_core_batch_frames` →
`build_snapshot()` on a worker with liveness heartbeats (`:295-335`). The
fallback is unscoped by construction. One uncovered event demotes the whole
batch (`patch_coverage.py:410-428`).

**What the core is.** `_build_snapshot_in_runtime_scope`
(`snapshot.py:409-716`) assembles, in section terms: config/scope block
(`runtime_default`, `runtime_config`, `migration`, `active_*`, `repo_scopes`),
`prompt_observability`, `workspaces`, `realms`, `boards`, `offices`,
`running_work`, `agents` (+readiness), `available_personas`,
`persona_instances` + `identity_map`, `persona_chat_history`,
`persona_chat_trace`, `operator_channels`, `persona_assignments`, `warnings`,
`parity`. Per-section wall time is ALREADY accumulated into
`parity.sections_ms` under keys `events`, `agents_readiness`,
`prompt_observability`, `persona_chat`, `boards_offices`, `running_work`,
`parity` (`:281-292,418,428,468,511,523,533,649,694-715`).

**Where the time goes** (source comments + history; live re-measure is SI-0):
`prompt_observability` — "profiled hot: ~5s, the skills-catalog walks inside
it" (`snapshot.py:510` comment, READ); it once cost 36.6 s before batching
(`14-snapshot-core-build-performance.md:96-149` READ) and its expensive walks
are TTL-memoized for 15 s (`doc 14 slice 1`); `events` — the one-shot
CachedEventLog materialization, ~1 s measured 2026-07-23 (`snapshot.py:423-429`
comment); `running_work` — six subsystems, sqlite + process probes;
`persona_chat` — session-DB reads; `agents_readiness` — per-persona
`profile_readiness_for_persona`. Cold/warm split 6.92 s / 1.17 s
(MEASURED-§10); the metadata-heavy profile (4,065 stats etc.) is §10.1's.

**The boot demote.** One `office.surface.updated` sinks a 23-event startup
batch (RELAYED-§10.3 item 6; consistent with the coverage table — surface
events are deliberately uncovered, `patch_coverage.py:199-207` READ).

**What already exists of the idea.** O-H4 scoped the OFFICE lane's resync by
reading the delta frame's `events` list (`serve_office_subscriptions.py:
212-266,349-394` READ) — the office lane now refetches only its ~2 KB RPC
projection and only when touched. The STREAM lane — the read model's actual
currency — still pays the full core. The enumerability fact O-H4 stands on
(`delta` frames enumerate their batch, `stream.py:215`) is the same one this
plan stands on.

**The safety substrate.** Every store mutation appends an event (the
store/event invariant, enforced by test — `stream.py:374-384` READ), so the
union of a batch's events bounds what the batch mutated, EXCEPT for the
writers the Stage-12 watchdog exists to catch — and those arrive as
`state.reconciled`, which this plan maps to `full` (see V2).

## 2. Validation

**V1 — Why sections and not entities?** The patch lane already handles
entities. What demotes is precisely what is NOT expressible per-row: surface
writes, restores, conflict resolutions, sync adoptions, chat traces,
board/flow writes, `state.reconciled`. Their honest unit is "which projection
must be re-derived" — a section. Entities stay the fold lane's vocabulary;
sections become the fallback's. The ladder ends up exactly upstream's: patch
→ scoped refetch → full hydrate (fold plan V5 named this ladder and deferred
it as D4; this plan IS D4's first rung).

**V2 — The dirty map, and where its honesty comes from.** A new module (new
file, colliding with no owned file) declares `EVENT_SCOPE: event_type →
frozenset[section] | FULL`. Rules:

- Unknown event type → FULL. `state.reconciled` → FULL (it exists because
  enumeration failed). `hydrate` never scoped.
- `state.patched` in a demoted batch scopes by its `entity` (entity→section
  is knowable server-side; the launcher's own `_entitySection` table is the
  client half, `patch_coverage.py:44-57` READ).
- A partition fence test in the style of `test_stream_patch.py:312` (the gate
  the fold plan leaned on): **every type in `event_catalog()` must appear in
  `EVENT_SCOPE` or in an explicit `SCOPE_FULL_TYPES` list** — a new event
  type cannot ship without a scoping decision, which is what keeps the map
  from rotting into silent under-scoping.
- Per-entry review discipline: an entry may name a narrow scope only with the
  emitting chokepoint cited beside it (the map is a claims ledger, each row
  tagged like this document's facts).

**V3 — The dependency closure (the finding that recalibrates the win).**
Sections read each other's inputs (all READ, `snapshot.py`):
`workspaces` embeds `persona_instances` (`:559-566`); `agents` appends
`active_persona_instance_agent_summaries(persona_instances, …)` (`:499-504`);
`prompt_observability` takes `personas` + `persona_instances` + `session_db`
(`:511-522`); `identity_map` derives from `persona_instance_rows`
(`:603-617`); `operator_channels` derives from `persona_chat_history` +
`persona_chat_trace` (`:641-656`); `summary` counts instances (`:543`);
`parity` derives from the whole frame (`:703-712`). So `dirty(section)` must
be closed over a declared dependency graph, and a bare `persona_instance`
event closes over most of the expensive half of the build. Countervailing
facts, to be QUANTIFIED at SI-0 rather than asserted: the expensive interior
of `prompt_observability` is TTL-memoized (15 s) so an intra-TTL scoped
rebuild pays its assembly, not its walks; and the highest-frequency
uncoverable batches on a live runtime are chat-trace/run batches whose
closure is the persona_chat family, NOT prompt_observability or
running_work. **ASSUMPTION B-1 (the plan's go/no-go): for the live event mix,
the closure-adjusted scoped build costs ≤ ⅓ of a full build for ≥ ⅔ of
demoted batches.** SI-0 measures exactly this from a probe copy + a decoded
window of the live log (read-only, the fold plan §1's method).

**V4 — Wire shape: additive keys, no new frame kind.** The stream contract's
discipline is additive frames only (`mission-control-stream.md` §Versioning
READ). A scoped fallback frame is a `delta` with two additive keys:
`core_sections: [names…]` and a `core` containing ONLY those sections (plus
the always-present envelope: `generated_at`, watermark, and a
`parity.partial: true` marker). `schema_version` stays 1; the launcher today
reads `type/watermark/identity_map/core` and ignores unknown keys
(`stream.py:200-207` comment READ) — but an OLD client handed a partial
`core` would apply it as a WHOLE core and silently lose every other section.
**Therefore the scoped frame is emitted only to declared clients** —
`scoped_core` capability token in the existing declaration channel, exactly
the V4 pattern that shipped for `office_actor_lifecycle`: strings are
uninterpreted set members (`patch_coverage.py:248-262` READ), absence means
today's wire, the room intersects (`accepted_fold_entities`, `:265-286`).
Undeclared client → full core, byte-identical. `identity_map` rides the frame
top-level whenever `persona_instances` is among the sections (it is derived
from exactly those rows, `stream.py:735-766` READ).

**V5 — Client fold semantics.** Replace the named sections in the held core;
keep every other section; advance the one watermark (a scoped delta is
ordered by the same `watermark.event_offset` gate as any delta). Completeness/
parity: keep held values for untouched lanes, adopt the frame's for rebuilt
ones — the "projection drops" pill must not flap on a scoped frame that
carried no `persona_chat` lane. Truncated/partial bases are not a hazard here
the way they were for O-L1: sections are replaced whole, never merged.

**V6 — Producer-side build.** `build_snapshot(sections=…)` (or a sibling
entry point) that runs only the named sections' builders plus the envelope.
Two constraints found by READ: (a) the **coalescing wrapper**
(`snapshot.py:345-388`) serializes and shares default-store builds — a scoped
build must NOT poison the shared full-build result; it takes the
injected-store bypass path (`custom_stores` branch, `:331-344`) or an
explicit bypass flag, accepting that scoped builds do not coalesce with each
other (they are the cheap case). (b) **byte parity fence**: for any store
state, the scoped build's sections must byte-equal the same sections of a
full build — a golden test, and it is THE fence against a section builder
quietly depending on an unbuilt sibling (the failure V3 predicts).

**V7 — What does the office lane get?** Nothing new — O-H4 already scoped
it, and its refetch is the 2 KB RPC projection. This plan is the STREAM
lane's. Stated to prevent double-building the same idea.

## 3. Target architecture (one paragraph)

The coverage decision stays exactly as it is; what changes is the fallback's
blast radius. A demoted batch consults the event→scope map, closes over the
section dependency graph, and — for a `scoped_core`-declared room — ships a
delta whose `core` holds only the dirtied sections; the client splices them
into its held core under the same single watermark. `full` remains the honest
answer for unknown types, `state.reconciled`, hydrates, and any batch whose
closure reaches "most of the build". Foldability improvements (D3, future
entities) then merely shrink how often the scoped path runs — a step, not a
cliff, in both directions.

## 4. Stages

Ordering rule (the O-L2 lesson, §10.2(b)): the client-side fold must exist
and be DECLARED before the producer ever emits a scoped frame; the producer
gates on the token, so the dangerous half is structurally last. SI-1/SI-2 are
inert server-side machinery; SI-3/SI-4 launcher; SI-5 activation; SI-6 docs.

### SI-0 — measurement gate (read-only; go/no-go for B-1)

Probe copy of a live-shaped root (fold plan §10.1's method — nothing under
`X:/Eternia/.hermes/` is written). Produce: (a) per-section cost table from
`parity.sections_ms` over warm and cold builds; (b) event-frequency ×
scope-closure table from a decoded window of the live archive (read-only
byte-offset reads via the rotation manifest, the §1 method); (c) the
closure-adjusted expected saving per demoted batch. **If B-1 fails**, the
recorded fallback design is coarse two-way scoping — `{persona-ish
(expensive closure) | office/board-ish (cheap closure)}` — which still fixes
the boot demote and every office/board/sync demote; the plan then re-scopes
to that and says so here.

### SI-1 — hermes: the scope map + partition fence (inert)

New module `agent_runtime/invalidation_scope.py`: `EVENT_SCOPE`,
`SCOPE_FULL_TYPES`, `sections_for_batch(events) -> frozenset[str] | FULL`,
the dependency graph + closure, each map row citing its chokepoint. Tests:
partition fence over `event_catalog()` (kill-mutation: add an event type to
the catalog fixture without a scope row — the fence must go red); closure
tests for the V3 edges (kill: drop the workspaces←persona_instances edge);
`state.reconciled → FULL` (kill: scope it). No caller yet; if SI-1 lands
alone, nothing changes on any wire.

### SI-2 — hermes: the scoped builder + byte-parity fence (inert)

`build_snapshot` gains `sections=` (default None = today, byte-identical
path untouched); scoped path bypasses the coalescer; envelope carries
`parity.partial` + `sections_ms` for the run sections. Tests: byte parity
per section vs a full build on a seeded root (kill: make `agents` skip the
instance summaries — parity must catch the missing rows); coalescer
isolation (kill: let a scoped build satisfy a waiting full-build caller —
assert the waiter still gets a full core). Alone: dead code.

### SI-3 — launcher: fold scoped cores (inert)

`mission_read_model.dart` (OWNED tonight by the fold agents — this stage is
sequenced after their hand-back; see §8): apply a `delta` with
`core_sections` by splicing named sections, adopting `identity_map` when
present, merging parity per V5, advancing `_sequence` normally. New constant
`_scopedCoreCapability = 'scoped_core'`. Tests: splice keeps unnamed
sections (kill: apply as whole core — assert an untouched section survives);
identity_map adoption (kill: skip it); watermark gate unchanged for scoped
frames (kill: exempt them). Alone: dead code — no runtime emits the keys.

### SI-4 — launcher: declare the token

Both declaration channels (`--fold-entities …,scoped_core` on the stream
lane; `fold_entities` in the office subscribe params for room-intersection
consistency). Precondition: SI-3 in the same or an earlier release — the
"declares what it cannot fold" failure is the one V4 exists to prevent.
Alone against an old runtime: unknown string, inert (`normalize_fold_entities`
passes tokens through, READ).

### SI-5 — hermes: the producer uses it (activation)

`_batch_frames_with_liveness` (in `stream.py` — OWNED tonight; sequenced
after hand-back): on demote, if the room's accepted set holds `scoped_core`
and `sections_for_batch` ≠ FULL → build scoped, emit the scoped delta (same
liveness-heartbeat wrapper); else today's path. The office sink needs no
change: a scoped delta still enumerates `events`, so O-H4's
`_delta_touches_workspace` classification is unchanged (READ
`serve_office_subscriptions.py:349-394`). Acceptance, live: (1) boot batch
with `office.surface.updated` ships `core_sections:[offices,…]` and the
launcher paints without a 5.9 s stall (baseline MEASURED-§10: 5.93 s); (2) a
chat-turn batch ships the persona_chat closure, not `prompt_observability`;
(3) `folded 0` never regresses — the patch lane's receipts are untouched.
Kill-mutations: emit scoped without the token (the mixed-pair test must
red); map an office event to a persona section (parity + fence).

### SI-6 — docs: the stream contract catches up (work items folded in)

`mission-control-stream.md`: add the missing `## patch frame` section (the
frame kind is enumerated at `:296-297` but never specified — RELAYED,
coordinator's evidence sweep; confirmed absent by the doc's own section list
READ); correct `:253` ("The Launcher's has two entries") to the current four
declarations incl. the lifecycle token; document `core_sections` and the
`scoped_core` token. Also record here the OUTSTANDING stale banner at
`tests/fixtures/stream_frames/README.md:15-31` ("OUTSTANDING CROSS-STACK
COPY (2026-08-16, O-H3)") — the copies landed (launcher `e1d198985`,
RELAYED); the banner's removal belongs to whoever next touches the fixtures
tree (they are owned by other agents tonight), and it is named here so it
cannot be forgotten silently.

## 5. Platform facts

- Delta frames enumerate their batches (`stream.py:215`); patch rows carry
  entity + id (`:242-246`); the declaration channel is uninterpreted strings
  intersected per room (`patch_coverage.py:248-286`; `serve.py` hub wiring).
- The store/event invariant bounds what a batch can have mutated; the
  watchdog covers the known slippers and maps to FULL (`stream.py:374-384,
  576-675`).
- `read_model.delta_patches: false` remains the lane-wide kill switch; a
  scoped core only ever replaces a FALLBACK frame, so the flag darkens this
  plan with the rest of the lane.

## 6. Adversarial pass

- **An under-scoped map entry is silent staleness** — worse than O-L2's
  race, because no resync heals it until an unrelated full core. This is the
  plan's real risk, and three fences answer it: the partition fence (no
  unclassified types), the per-entry chokepoint citation rule, and the
  byte-parity fence (a builder with a hidden dependency fails parity). What
  they do NOT cover: a store write whose event maps narrow but whose
  chokepoint ALSO mutates a store outside the closure — exactly the class
  the store/event invariant makes rare and the watchdog makes bounded for
  scope/catalog state only. **Named residual risk**, accepted with the
  conservative-default rule; a read-set audit (instrument builders to record
  which stores they touched, compare against the closure) is the real fence
  and is deferred (D-B2) with a note that it converts this from
  reviewed-honest to machine-checked.
- **The closure eats the win** (V3): answered by making SI-0 a go/no-go with
  a pre-committed coarse fallback shape, instead of discovering it after
  five stages — the fold plan's own §10 criticises exactly the reverse
  ordering.
- **Old client, scoped frame**: impossible by token gating; the dangerous
  direction (client declares, fold missing) is forbidden by SI-3/SI-4
  same-release rule and testable (declare-without-fold is a red bridge test).
- **Two producers, one client** (until Plan C lands): both producers make
  the same per-batch scoping decision from the same accepted set — but the
  two rooms' accepted sets can DIFFER (stream child declares via argv, hub
  room via subscribers). A scoped frame from one lane and a full core from
  the other at the same watermark are both correct and both splice/apply
  idempotently; the stale-drop gate handles the overlap as today. No new
  race is introduced; V6's existing race is Plan C's to dissolve.
- **`parity`/`completeness` flapping**: V5's keep-held rule; test pins it.
- **A batch of 256 events hits the cap mid-gesture** (`_DELTA_BATCH_CAP`,
  `stream.py:262-265` READ): two frames, each scoped independently —
  correct, since each carries its own events. No action.
- **Unanswered**: the exact live section costs (SI-0 exists because of it);
  whether `events`' ~1 s CachedEventLog materialization is avoidable for
  scoped builds that don't need tails (recorded as an SI-2 option, not
  assumed).

## 7. What this plan does NOT fix

- The full build when full is honest: boot hydrate, reconnect re-baselines,
  `state.reconciled`, unknown types. The cold 6.9 s build itself is untouched
  (doc 14's remaining items and Plan C's topology work own that).
- The two-producer double build (Plan C) and the demote itself (D3 makes the
  create batch coverable; this plan makes the residue cheap).
- It does not widen coverage anywhere: no event becomes foldable that was
  not; the honesty rule ("a patch frame never lies") is untouched.

## 8. Standing constraints / collision map

`stream.py` and `mission_read_model.dart` are OWNED tonight — SI-3 and SI-5
land only after those agents hand back; SI-0/SI-1/SI-2 collide with nothing
(new files + `snapshot.py`, unowned). Fixture consequences, named: SI-5's
committed frame fixtures (a scoped delta golden + launcher byte-identical
copy) extend `GENERATED_FRAME_FILES`, which regenerates `MANIFEST.sha256` in
BOTH repos — the manifest must be copied verbatim from hermes' generator,
never rebuilt launcher-side (the line-ORDER divergence found and fixed
tonight, launcher `e1d198985`, RELAYED — the gate compares order before
bytes, `check_producer_contracts.py:59-70` RELAYED). `decision_contract_hash`
does NOT move (no registry row changes — hash derives from the registry
alone, `decision_contract_registry.py:73-83` READ). All the usual: 30 s test
cap, no `.hermes/` writes, additive keys only, kill switch inherited.

## 9. Verification log

| # | Fact | How established |
|---|---|---|
| B-R1 | Unscoped fallback; one uncovered event demotes batch | READ stream.py:295-360; patch_coverage.py:410-428 |
| B-R2 | Section inventory + sections_ms instrumentation | READ snapshot.py:281-292,409-716 |
| B-R3 | Cross-section input edges (workspaces/agents/prompt_obs/identity/channels/parity ← persona rows etc.) | READ snapshot.py:499-522,543,559-566,603-656,703-712 |
| B-R4 | O-H4 precedent: delta frames enumerable, office lane already scoped | READ serve_office_subscriptions.py:212-266,349-394; stream.py:215 |
| B-R5 | Declaration channel: uninterpreted strings, room intersection, absence = historical | READ patch_coverage.py:248-286 |
| B-R6 | Coalescer semantics + injected-store bypass | READ snapshot.py:321-388 |
| B-R7 | Store/event invariant + watchdog boundary | READ stream.py:374-384,576-675 |
| B-R8 | Costs: 6.3–6.6 s build in the 6.94 s create; 5.93 s boot demote; warm/cold 1.17/6.92 | MEASURED-§10 |
| B-R9 | prompt_observability history (36.6 s → 5.9 s; TTL memos) | READ 14-snapshot-core-build-performance.md |
| B-R10 | Manifest order gate + tonight's divergence fix | RELAYED (launcher e1d198985; check_producer_contracts.py:59-70) |
| B-A1 | Closure-adjusted saving ≥ ⅔ of demotes at ≤ ⅓ cost | ASSUMPTION — SI-0 go/no-go |
