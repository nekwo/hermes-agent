# End-to-end correlation id — gesture → RPC → events → batch → frame → fold (Plan D, 2026-08-16)

> **Home.** Hermes repo, beside the fold-promotion plan (§10.3 item 8 is the
> ask). Evidence tags READ / RAN / MEASURED-§10 / RELAYED / ASSUMPTION as
> defined in `AGENT_CREATE_ONE_CALL_PLAN_2026-08-16.md`.

**Verdict up front.** The argument for this is not convenience. Causality
across the pipeline is inferred from timestamps today, and that inference
produced a CONFIDENTLY WRONG diagnosis on 2026-08-16: anchoring on the
launcher's flush receipt — which lags the real RPC by 250–650 ms — yielded
"deletes take 3.8 s" when they take 280–368 ms, and from that a wrong
recommendation to prioritise prediction over D3 (§10.3 item 8, MEASURED-§10).
The same night produced the same lesson one layer down: eleven by-hash
fixture checks all passed while the repos disagreed, because the check was a
locally-invented weaker gate; the owning gate (`check_producer_contracts.py`,
which compares manifest membership AND order before bytes) was red the whole
time (RELAYED; fixed at launcher `e1d198985`). Both are one failure shape:
**an inference standing in for a receipt.** The cost of no correlation id is
not slow diagnosis; it is fluent, confident, wrong diagnosis. Validation's
best finding: **most of the pipe already exists.** Delta frames already
surface `entity.correlation_id` from event payloads (`stream.py:157-168`
READ; contracted at `mission-control-stream.md:182` READ) and patch rows are
a verbatim spread of the event payload (`stream.py:242-246` READ) — so a
`correlation_id` key placed into an event payload at the chokepoint rides
BOTH frame kinds with zero wire changes. Nothing populates it on any gesture
path (RAN: repo-wide grep — the only producers are subagent lifecycle and
error envelopes). This plan mints the id at the gesture, threads it through
the write boundary into the payloads, and prints it on every receipt that
already exists.

## 0. The ask

§10.3 item 8: gesture → RPC → emitted events → producer batch → push frame →
client fold, one id surviving every hop. Partial handles exist and each
survives ONE hop; none answers "which RPC produced this launcher update".

## 1. Baseline — the partial handles, and where each one dies

| Handle | Hop it survives | Where it dies | Evidence |
|---|---|---|---|
| Intent `idempotencyKey` → `--idempotency-key` argv | launcher intent → harness CLI (chat mints only: sent only when `new_session`) | inside the mint reservation; never enters an event payload | READ mission_control_bridge.dart:3343-3345; persona_chat_mints.py:63-128 |
| `client_message_id` (chat sends) | send → dedup journal → `persona_chat.send_refused` payload | not on `state.patched` rows or office events | READ mission-control-stream.md:300-331 |
| `--issued-at` intent basis (scope writes) | click → store supersede guard | guard only; not emitted onward | READ 13-write-path-intent-integrity.md |
| JSON-RPC `id` | request → its own reply | per-connection, never touches events | READ serve_rpc.py:138-155 |
| Patch-row `seq` / frame `base_offset`+watermark | event log → frame → fold gate | names ORDER, not CAUSE; two producers stamp independently (until Plan C) | READ stream.py:220-259,479-505 |
| Office lane `workspace_id` | subscribe → patch/resync notifications | names the ROOM, not the write | READ serve_office_subscriptions.py:383-441 |
| `entity.correlation_id` slot in delta frames | payload → frame — ALREADY WIRED | no gesture-path producer ever sets it | READ stream.py:157-168; RAN grep: agent/subagent_lifecycle.py only |
| Diag/service receipts (`[MissionFold]`, `[MissionOfficeSubscribe]`, `[MissionOfficeDrop]`, `serve_office_subscription_rebaselined`) | each names its own lane's moment | joinable only by timestamp — the exact inference that failed | READ fold plan §1; mission_control_page.dart:2447-2452; serve_office_subscriptions.py:766-787 |

**The §10 nuance this table adds:** item 8 says "none answers which RPC
produced this launcher update" — true, but the wire SLOT for the answer
already exists end-to-end on the read side. The missing 40% is minting and
threading; the delivered 60% (payload → delta frame `events[]` → patch rows
→ one grep) is already shipped and contracted. This makes the plan cheaper
than item 8 prices it.

## 2. Validation

**V1 — One id or several?** One `correlation_id`, minted at the GESTURE
(launcher), reusing the existing payload key name so the delivered wire
surfacing applies unchanged. It is NOT the idempotency key (dedup identity —
a replay reuses it by design) and NOT `issued_at` (ordering basis). A retry
of the same gesture carries the SAME correlation_id (it is the same
gesture); receipts distinguish attempts by their own timestamps. Generated
token only (`g-<lane>-<micros>-<rand4>`), never operator text — so redaction
(`_redaction_safe_json`, `stream.py:769-778` READ) and payload-size budgets
are non-issues; boundary-validated ≤ 64 chars mirroring the idempotency-key
cap discipline (`persona_chat_mints.py:117-127` READ).

**V2 — Where does it enter the runtime?** As an additive optional param on
the write boundaries, threaded as an optional kwarg to the chokepoint, and
attached into the payloads the chokepoint already emits — the exact
`created`-key pattern: absent-when-unset keeps every existing payload
byte-identical (`state_patches.py:184-189,211-218` READ). Contract-hash
neutrality VERIFIED: the hash derives from the registry rows alone
(`decision_contract_registry.py:73-83` READ) and `validate_event_payload`
checks required-field PRESENCE only — unregistered optional keys are legal
(`:53-62` READ). So CI-1 moves NO `decision_contract_hash`, NO golden bytes
(fixtures are generated without ids), and both repos stay green with nothing
to mirror. Registering `correlation_id` as a detail field is deliberately
DEFERRED (D-D1) into some later planned golden-regeneration wave, because
registering moves the hash inside every generated core and forces the
cross-stack copy dance for zero diagnostic gain.

**V3 — Which write paths, in what order?** By diagnostic value over the
gestures that produced wrong diagnoses:

1. `runtime.office.upsert` (drags, the delete's client half) and
   `runtime.office.*` — `serve_rpc.py` + `office_store.py`, both UNOWNED
   tonight. The office domain events (`office.actor.upserted/removed`) are
   emitted by `office_store` itself — id attachable immediately. The PAIRED
   `state.patched` rows go through `emit_office_actor_patch` in
   `state_patches.py` — **OWNED tonight**; that half lands after hand-back
   (CI-1 splits a/b along exactly this line).
2. `runtime.agent.create` (Plan A) — its params already reserve
   `correlation_id`; the phases envelope joins it.
3. The argv capability lanes (persona create/open-chat/retire…) — a
   `--correlation-id` flag threaded the same way; LAST because argv surface
   changes fan wide (38 capabilities) and the first two cover tonight's
   misdiagnosed gestures. Named partial-coverage window: until CI-4, a
   create's roster half is uncorrelated while its office half is correlated.

**V4 — How does it come back out?** Zero-change surfaces (all READ): delta
frames (`entity.correlation_id` + per-event in `events[]`), patch frames
(payload spread → every patch row carries it). Receipt changes (small,
additive): the launcher fold receipt lists distinct ids it applied
(`[MissionFold] applied 2 of 2 rows … corr=g-…`), the office subscribe
receipt likewise, the RPC reply echoes it, and serve-side service-log lines
for the write carry it. Then "which RPC produced this launcher update" is
answered by ONE grep over two logs for one token — the acceptance test is
literally that grep.

**V5 — What can it never answer?** A full-core demote reflects N gestures:
the frame's `events[]` names all contributing ids, but the CORE is a rebuilt
whole — per-field attribution inside a demoted core does not exist and this
plan does not pretend it does (the receipts say `core rebuilt;
contributing corr=[…]`). Durations across processes still need per-process
monotonic pairs — the id JOINS receipts; it does not synchronize clocks. The
anchoring failure is prevented because the join no longer NEEDS clock
comparisons to establish which receipt belongs to which gesture.

## 3. Target architecture (one paragraph)

The launcher mints one token per gesture and sends it with the write; every
event the write's chokepoint appends carries it as an optional payload key;
the existing frame builders surface it on both lanes for free; every receipt
that names a moment also names the token. Causality becomes a grep, timing
becomes a join of per-process monotonic receipts on that grep, and the class
of "confident wrong diagnosis by timestamp anchoring" loses its mechanism.

## 4. Stages

### CI-0 — pin the neutrality claims and the discipline (read-only + tests)

- Turn V2's two neutrality claims into committed tests BEFORE any producer
  changes: (a) an event payload with `correlation_id` round-trips the frame
  builders into `entity.correlation_id`/patch rows (this is a test of
  DELIVERED behaviour — it must pass against today's code with a hand-built
  event); (b) absent-when-unset: a payload without the key is byte-identical
  to today (golden). Kill-mutations: strip the key in `_redaction_safe_json`
  (a would red); default the kwarg to a generated value (b would red).
- Record the verification discipline this plan inherits from tonight
  (RELAYED, launcher `e1d198985`): **verify with the gate that OWNS the
  contract, never a locally-invented weaker check** — for fixtures that is
  `check_producer_contracts.py` (membership AND order AND bytes), for frames
  it is the generated-golden suites, and for this plan's acceptance it is
  the single-grep test in CI-3, not eyeballed timestamp tables.

### CI-1 — hermes: thread the id through the office write path

**a (UNOWNED files, can land now):** `serve_rpc.py` write handlers accept
optional `correlation_id` (boundary-validated; unknown-param behaviour of
old runtimes verified harmless — handlers read known keys only,
`serve_rpc.py:832-967` READ); `office_store.upsert_actor` /
`_archive_actor_locked` take the optional kwarg and attach it to the DOMAIN
events they emit. **b (after `state_patches.py` hand-back):**
`emit_office_actor_patch` / `emit_state_patch` pass-through, inside the
existing 4 KB accounting exactly as `created` is. Tests: id present on both
the domain event and the paired patch from one write (kill: drop either);
absent-when-unset goldens hold (kill: stamp unconditionally); oversize
shrink loop re-measures with the key (reuse the `created` sizing test
shape). **Alone:** extra ignored payload key — inert for every reader in the
field (fixed key-set folds, READ via fold plan V4).

### CI-2 — launcher: mint, send, and print

Mint at the gesture sites that feed the office writer (drag commit, archive,
restore, drop) — the writer already funnels through one RPC builder
(`mission_office_rpc.dart` write leg, `:638-707` sweep) so the stamp is one
site; print on: RPC send/reply receipts, the fold receipt (distinct ids
applied), the office subscribe receipt. **Collision:** the fold receipt
lives in `mission_read_model.dart`/bridge fold body — OWNED tonight;
sequence after hand-back. **Alone against an old runtime:** the param is an
unknown key handlers never read — verified harmless (CI-1a note). Receipts
print `corr=-` for id-less rows so absence is visible, not invisible.

### CI-3 — acceptance: one grep answers the question

Scripted against the operator's own build (the fold plan §5 O-H3 acceptance
style): perform one delete and one drag; grep diag + service logs for the
minted token; the output must contain, in order: launcher send, serve write
receipt, patch-row fold receipt (or demote receipt naming the id among
contributors) — and the phase table it yields must match the §10.1
measurement method within noise. This test replaces timestamp anchoring as
the sanctioned diagnostic procedure, and the doc's diagnostic runbook
paragraph says so explicitly.

### CI-4 — widen: `runtime.agent.create`, then the argv capability lanes

Plan A's method reserves the param from birth; the argv lanes gain
`--correlation-id` threaded to their chokepoints (touches
`persona_assignments.py` — OWNED tonight; after hand-back, and after D3 to
avoid churning the same functions twice). Deliberately LAST per V3; the
partial-coverage window closes here.

## 5. Platform facts

- Payload keys are free-form past required-field validation; the 4 KB cap is
  enforced at append and the patch builder's shrink loop accounts additions
  (`state_patches.py:199-258` READ).
- Both frame kinds and the office notification are payload-preserving
  (`stream.py:242-246`; `serve_office_subscriptions.py:431-441` — rows
  forwarded verbatim).
- Receipts already go through named log sinks on both sides — the plan adds
  fields to lines that exist, not new logging channels.

## 6. Adversarial pass

- **Coalescing smears attribution**: N gestures, one frame — answered per-row
  (patch lane) and per-event (`events[]` on demotes); frame-level
  attribution is refused on purpose (V5). The residual: a demoted CORE's
  fields are unattributed — accepted, printed honestly.
- **The id becomes a covert channel for text**: boundary validation (charset
  + length) and generated-token rule; a free-text id is refused at the RPC
  boundary, not sanitized.
- **Someone registers it in the contract registry casually**: moves
  `decision_contract_hash` inside every generated golden core → both repos'
  fixture dance for nothing (the trap that bit twice today). D-D1 pins the
  decision; CI-0's neutrality golden reds if the hash moves.
- **Two attempts, one id** (retry semantics, V1): a duplicated write's two
  receipts share the id — which is the TRUTH (one gesture); dedup identity
  stays the idempotency key's job. A diagnostician counting receipts per id
  learns the retry happened — a feature, and tonight's flush-receipt error
  would have been visible as exactly that.
- **Plan C interaction**: until the collapse, the same patch row arrives on
  two lanes; both carry the same id; the fold receipt prints which lane
  applied it — the id makes the dual-lane duplication VISIBLE, which is
  Plan C's TC-3 evidence for free.
- **What this pass cannot answer**: whether serve-side service logs exist
  for every office write today (the upsert path's logging was not
  exhaustively read — if a write has no service-log line, CI-1a adds one,
  small and named here as possible scope growth); and the argv lanes'
  flag-threading blast radius (CI-4 sized only as "38 capabilities, wide" —
  RELAYED inventory, launcher HANDOFF doc §5b-c).

## 7. What this plan does NOT fix

- No latency changes anywhere; no batch becomes foldable; no build gets
  cheaper. It is pure observability.
- It does not replace Plan A's phases envelope (in-handler timing) — it
  joins envelopes across processes.
- It does not correlate uninstigated changes (agent turns, watchdog
  reconciles) — those have no gesture and carry no id, honestly.

## 8. Standing constraints / collision map

OWNED tonight and therefore sequenced after hand-back: `state_patches.py`
(CI-1b), `persona_assignments.py` (CI-4), `mission_read_model.dart` fold
receipt (CI-2 half). Land-now surface: `serve_rpc.py`, `office_store.py`,
launcher RPC builder + send receipts. Fixture consequences: NONE by design
(V2) — and the neutrality golden is the fence that keeps it true. No
`.hermes/` writes; CI-3 runs against the operator's build by the operator,
reading logs only. Commit explicit paths; no push.

## 9. Verification log

| # | Fact | How established |
|---|---|---|
| D-R1 | `entity.correlation_id` slot wired payload→frame; contracted | READ stream.py:157-168; mission-control-stream.md:182 |
| D-R2 | Patch rows are a verbatim payload spread | READ stream.py:242-246 |
| D-R3 | No gesture-path producer sets correlation_id | RAN repo grep (subagent_lifecycle + error envelopes only) |
| D-R4 | `created` precedent: additive optional payload key, absent-when-unset, sized in-cap | READ state_patches.py:184-258 |
| D-R5 | Contract hash derives from registry only; extra payload keys legal | READ decision_contract_registry.py:53-83 |
| D-R6 | Partial-handle inventory (§1 table) | READ per-row citations |
| D-R7 | RPC handlers ignore unknown params | READ serve_rpc.py:832-967 |
| D-R8 | The 3.8 s misdiagnosis; flush-receipt lag 250–650 ms | MEASURED-§10 / §10.3 item 8 |
| D-R9 | The weaker-gate manifest incident + fix | RELAYED (launcher e1d198985; check_producer_contracts.py:59-70) |
| D-R10 | Office notification forwards rows verbatim | READ serve_office_subscriptions.py:395-441 |
