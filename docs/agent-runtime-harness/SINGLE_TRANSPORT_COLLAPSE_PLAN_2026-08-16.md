# Collapse to one transport — one producer, one lane, receipts before deletions (Plan C, 2026-08-16)

> **Home.** Hermes repo, beside the fold-promotion plan (its D7 and §10.3
> item 7 are the ask). Evidence tags READ / RAN / MEASURED-§10 / RELAYED /
> ASSUMPTION as defined in `AGENT_CREATE_ONE_CALL_PLAN_2026-08-16.md`.

**Verdict up front, and it contains a correction to the ask.** D7's end-state
is right and this plan stages it: the launcher's read-model currency moves
onto the serve hub's shared producer (`{"op":"subscribe","lane":"stream"}` —
built server-side, `hermes_cli/harness_parts/serve.py:169-177`, and dialled
by NOBODY: no `"op":"subscribe"` exists anywhere in the launcher's Dart tree;
evidence sweep this session), which dissolves the dual-producer topology V6
patches around and makes the reverted O-L2 unnecessary rather than merely
safe. **But §10.3 item 7's performance framing is partly wrong, and the plan
must not be sold on it.** Item 7 says every demoted batch is "built TWICE
CONCURRENTLY — the serve hub's producer and the launcher's own `harness
stream` child — over the same ~2,000 files on the same disk", and offers that
contention as a candidate for the cold 6.9 s. Two facts contradict the
framing (both READ this session): (1) the launcher's stream lane normally
runs as an argv streaming REQUEST inside the SAME serve child
(`_streamingRunner` is injected as
`runMissionControlCommandStreamingPreferServe`,
`mission_control_provider.dart:239-243`; a separate `hermes harness stream`
process spawns only when serve is unavailable,
`mission_control_serve_session_io.dart:1750-1838`); (2) within one process,
`build_snapshot()` is SERIALIZED and coalesced (`_BUILD_COALESCE`,
`snapshot.py:345-388`) — two demoted-batch builds run back-to-back, never
concurrently against the disk. So in the normal topology the cost is "twice,
sequentially" (still pure waste, still worth deleting) and the
cross-process disk-contention hypothesis applies only to the fallback
topology. TC-0 measures instead of guessing — the fold plan's §10.2 lesson
applied to this plan's own motivation. The correctness case (one producer,
one batch boundary, V6 gone by construction) carries the plan on its own.

## 0. The ask, and the state of the ruling's text

- Fold plan D7 (`OFFICE_GESTURE_FOLD_PROMOTION_PLAN_2026-08-16.md:913-918`,
  READ): the real end-state is ONE transport; O-L2/O-H2 were shaped so
  nothing is wasted by the migration (per-row seq folding and complete-batch
  frames are what a single-lane client needs anyway).
- Ruling #42's stated sequence — *finish the main path, make fallbacks prove
  they are dead, delete cheapest-first* — and the operator's accepted cost —
  *"no serve no office"* — are **RELAYED: recorded nowhere on disk.** An
  exhaustive search of both repos and the live board store finds no numbered
  ruling register (RAN, evidence sweep). The nearest on-disk kin is the
  DECISION doc's borrowed cutover pattern: keep old polls as "SLOW BACKSTOPS
  … so nothing goes dark mid-upgrade"
  (`DECISION_push_and_rpc_2026-08-13.md:165-169`, launcher repo, READ).
  **Work item C-W1 (for the coordinator/operator, not this repo):** record
  ruling #42's verbatim text and the accepted-cost sentence into that
  DECISION doc; until then, every citation of #42 is relayed text and this
  plan says so at each use.

## 1. Baseline — the actual transport inventory (evidence sweep + READ)

- **Lane A — read-model currency**: argv `harness stream --fold-entities
  persona_instance,incident,office_actor,office_actor_lifecycle`
  (`mission_control_bridge.dart:1335-1435`), run inside the serve child via
  the serve-preferring streaming runner; separate one-shot process only as
  fallback. Its producer is a PRIVATE `stream_frames()` generator per
  request — NOT the hub.
- **Lane B — the serve child**: `harness serve --ndjson`, NDJSON over
  stdio (`mission_control_serve_session_io.dart:1056-1072`). **There is no
  TCP/WebSocket in the launcher's Mission Control tree at all** (RAN grep:
  zero `Socket`/`WebSocket` hits; hermes' real socket lane
  `serve_socket.py:179` exists on an ephemeral HMAC-gated port and nothing
  dials it). "Socket hub" in prior docs means: the hub, spoken to over this
  stdio pipe. Port 8090 is unrelated (gateway; untouched).
- **Lane B1 — office push**: `runtime.office.subscribe` / `.patch` /
  `.resync` over Lane B; sink = the hub (`serve_office_subscriptions.py`,
  READ in full this session); folds through the SAME fold body as Lane A
  (`mission_control_bridge.dart:1902-1981`).
- **Lane C — CLI one-shot fallback**: every argv site degrades to a fresh
  `hermes` process on `MissionServeUnavailable`
  (`mission_control_serve_session_io.dart:1685-1727`).
- **Lane D — cached `snapshot.json`**: first-paint only, never applied to
  the read model (`mission_cached_snapshot_read.dart`;
  `mission_control_bridge.dart:1111-1134`).
- **No polling loop exists** (RAN: `Timer.periodic` hits are UI tickers
  only).
- **V6 topology**: Lane A and the hub are two producers with independent
  cursors and independent 200 ms debounces feeding ONE `MissionReadModel`
  watermark; O-L2 (per-row seq dedup) shipped, proved actively lossy without
  O-H2-complete frames from BOTH lanes, and was reverted (§10.2(b),
  MEASURED-§10 receipt: `STALE dropped: seq=88661934 …`).
- **The hub is ready for this**: `StreamHub` fans one producer to N
  subscribers with per-subscriber bounded buffers, drop accounting, and
  `on_drop` close-and-resync (`serve_stream_hub.py:461-607` READ);
  subscribe-time declarations intersect per room; `restart_producer=False`
  exists for baseline-carrying joiners (O-H5, `:519-550` READ).

## 2. Validation

**V1 — What exactly is "one transport"?** One PRODUCER and one stream lane:
Lane A's private generator retires; hydrate/delta/patch/heartbeat arrive as
`{"op":"subscribe","lane":"stream","fold_entities":[…]}` frames on the Lane B
stdio connection the office lane already uses. It is NOT: the TCP socket
migration (nothing dials it; deferred D-C3), nor the argv→JSON-RPC CALL-half
migration (the DECISION doc's own Stage 2, separate work), nor deleting
`hermes harness stream` as a CLI verb (it remains an operator/debug tool and
the serve-absent fallback's substrate).

**V2 — What does the hub lane still need before it can carry the launcher?**
Gap list, each an explicit TC-1 deliverable, none assumed done:

- `fold_entities` on the subscribe op — exists per the contract doc
  (`mission-control-stream.md:263` READ) with `subscribe_denied` for
  malformed declarations (`:284-286`).
- Resync semantics — Lane A has `--resync`; on the hub, a re-subscribe
  restarts the producer and re-hydrates by contract
  (`serve_stream_hub.py:527-533` READ), which is the same recovery. Verify a
  re-subscribe after `subscription_dropped` yields hydrate-first, and that
  the office lane's O-H5 restart-free join does NOT suppress the stream
  joiner's hydrate (the floor rule at `:545-549` says it cannot; test it).
- Interleaving: stream frames now share stdout with RPC replies and office
  notifications. Per-connection sink queuing and what a 822 KB frame does to
  latency of a small office patch behind it — **ASSUMPTION C-2: the
  per-connection writer is a single ordered queue and a fat frame delays but
  never corrupts.** TC-1 reads `serve.py`'s `_sink_for`/writer and pins it
  with a test; if head-of-line blocking measures badly, the answer is Plan
  B's scoped cores shrinking the fat frames, not a second connection.
- Capability detection: **ASSUMPTION C-1** — the `ready`/`version` frames
  advertise op support (the RPC manifest rides them, `serve_rpc.py:240-249`
  READ; whether op-level `subscribe` is advertised is unverified). If not
  advertised: TC-1 adds it additively, and the launcher gates on it; a
  probe-subscribe against an old runtime that answers with an error frame is
  the fallback gate.

**V3 — What breaks if each stage lands alone?** Asked per stage below; the
headline: the launcher NEVER unilaterally switches — it subscribes only when
the runtime advertises the lane, and Lane A remains the automatic backstop
until TC-3's receipts justify deletion. The O-L2 lesson is baked in: no
client-side behaviour change lands ahead of its server counterpart.

**V4 — What does V6 become?** With one producer, batch boundaries are single
and frames are totally ordered per connection: `base == held` holds by
construction on the healthy path; O-H2's forward-whole-batch stays (harmless
and still right for the office lane's addressing); O-L2 stays reverted, its
per-row-seq idea recorded as hardening-if-ever-needed (D-C4). The
office-lane park ladder (250 ms→4 s, cap 5/60 s — note: the fold plan §1
says "→1 s"; the lane's code comment says 4 s; minor discrepancy, recorded)
becomes reachable only by genuine defects, as O-H4 intended.

**V5 — The accepted cost.** "No serve no office" (RELAYED) extends to "no
serve, no live stream": in the collapsed topology a dead serve child takes
both. This is LESS of a change than it reads — Lane A already normally lives
inside the serve child (V1 baseline), so the blast radius is today's blast
radius; what is deleted is a fallback that mostly ran only when serve was
already gone, plus the cold boot path's separate-process hydrate, which
STAYS (Lane C one-shot `stream --max-frames 1` and `snapshot --json` remain
the serve-absent recovery, unchanged).

## 3. Target architecture (one paragraph)

One serve child, one stdio connection, one `StreamHub` producer; the
launcher subscribes the stream lane and the office lane on that connection,
folds both through the one fold body it already has, and recovers through
re-subscribe (hydrate-first by hub contract). The argv streaming path
survives as a flagged backstop until its receipts flatline, then its
launcher call sites are deleted cheapest-first; the CLI one-shot lanes and
the cached-snapshot first paint are untouched.

## 4. Stages

### TC-0 — measure before believing (both repos, read-only + one log line)

**Goal.** Replace item 7's contention hypothesis with numbers.

- Consume the `snapshot_build` `elapsed_ms` already on the wire
  (`stream.py:316-327` READ) into the launcher diag log and the serve
  service log — §10.3 item 2, nearly free. **Collision note:** item 2 may be
  in flight with another agent; check before building, adopt theirs if so.
- Count builds per demoted batch across the two producers (service log +
  diag receipts) — confirm "twice, sequentially" vs "twice, concurrently"
  per topology; measure how often Lane A is a real child process
  (fallback-mode frequency) on the operator's machine.
- Pin the warm/cold split cause: same store, serve-resident build timed cold
  vs immediately-after — if cold-cache dominates regardless of process
  count, item 7's contention candidate is retired in writing here.

**Danger if alone:** none; it is instrumentation and a table in this doc.

### TC-1 — hermes: make the hub lane provably launcher-grade (inert)

**Goal.** Close V2's gap list with contract tests; change nothing default.

- Verify/land op advertisement (C-1) additively on `ready`/`version`.
- Byte-parity contract test: for a seeded root and a scripted event
  sequence, frames delivered to a hub stream subscriber ≡ frames emitted by
  argv `harness stream` with the same declaration (same fixtures family as
  `tests/fixtures/stream_frames/` — see §8 for the manifest discipline).
  Kill-mutation: fork the hub path's frame builder — parity must red.
- Interleaving/backpressure test per C-2 (a fat hydrate between two office
  patches: order preserved, office patch delivered, drop accounting exact).
- Re-subscribe recovery test: drop → resubscribe → hydrate-first, watermark
  continuity.

**Mixed pairs.** Server-only, additive; no client change. Alone: nothing
observable.

### TC-2 — launcher: subscribe the hub lane behind a gate; argv becomes the backstop

**Goal.** The main path finished, per the ruling's first clause (RELAYED).

**Change surface.** A stream intake that, when the runtime advertises the
lane (C-1 gate), sends the subscribe op on the serve session and feeds the
EXISTING intake/fold body (`_applyStreamLineAsync` path — one fold body is
already shared, `mission_control_bridge.dart:1902-1981` READ); Lane A's argv
stream is started only on gate-closed or on hub-lane failure (the DECISION
doc's slow-backstop pattern, READ). Receipts: `[MissionStream] lane=hub|argv
reason=…` via the named sinks — the receipts ARE stage TC-3's evidence, so
their taxonomy (why argv ran: `gate_closed | hub_refused | hub_dropped |
serve_absent`) is part of this stage's definition of done.

**Mixed pairs / alone.** Old runtime: gate closed, byte-identical behaviour.
New runtime, TC-1 unlanded: impossible by the gate (the advertisement IS
TC-1's). Kill-mutations: subscribe without the gate (test asserts argv
against a non-advertising fake); fold-path divergence (reuse the byte-parity
fixtures from TC-1 against the launcher fold — the cross-stack golden).

### TC-3 — the fallbacks prove they are dead (observation window)

Exit criteria, all from receipts, none from belief: over the agreed window
(operator's real sessions), `lane=argv` activations with
`reason != serve_absent` = 0; V6-class receipts (`STALE dropped`, `gap`
resyncs) = 0 on the hub lane; office lane parks = 0. Any nonzero finding is
a bug with a receipt attached — fix, restart window.

### TC-4 — delete cheapest-first (launcher first, hermes barely)

Order by deletion cost (the ruling's third clause, RELAYED): (1) launcher:
the argv-stream PRIMARY path and its watchdog plumbing — the backstop spawn
for `serve_absent` stays with Lane C; (2) launcher: V6-era special cases
that exist only because of dual producers (audit list built during TC-3;
O-H2 forward-whole stays, see V4); (3) hermes: nothing — the stream verb,
the office lane, and the hub all remain load-bearing. Rollback: revert (2),
(1) independently; the gate from TC-2 still works underneath.

## 5. Platform facts

- The hub stops producing on an empty room and restarts per generation;
  subscribing is what arms production (`serve_stream_hub.py:519-606` READ)
  — an RPC/office subscriber COUNTS as a subscriber, which is what keeps
  the office alive after Lane A's retirement
  (`serve_office_subscriptions.py:16-23` module docstring, READ).
- One fold body, one watermark, both lanes already
  (`mission_control_bridge.dart:1902-1981`; `mission_read_model.dart:724`).
- The serve child's death and respawn ladder already exists and is the
  recovery story for every lane on it.

## 6. Adversarial pass

- **Head-of-line blocking** (C-2): a demoted 822 KB frame ahead of an office
  patch on one pipe. Mitigations in order: Plan B shrinks demoted frames;
  D3 removes the common demote; the hub's per-subscriber buffers already
  bound memory. If TC-1's measurement still shows unacceptable stalls, the
  recorded alternative is subscribing office and stream on the hub but
  keeping their notifications' relative order UNGUARANTEED across methods —
  rejected for now because per-row idempotent folds tolerate reordering
  poorly under the single-watermark model. UNANSWERED until TC-1 measures.
- **The coalescer masks the win** (this plan's own §0 correction): if TC-0
  shows builds were sequential-and-shared all along, the perf line item
  shrinks to "one build's latency removed from the second lane" — the plan
  survives on correctness; the doc updates its §0 verdict either way. This
  is deliberately the reverse of the fold plan's failure mode: the caveat is
  in the verdict BEFORE the measurement.
- **A hub bug now takes the whole read model** (single lane): the backstop
  ladder (argv spawn on hub failure) exists precisely until TC-3 proves it
  unneeded, and Lane C's one-shot recovery is permanent. What is genuinely
  lost post-TC-4: an INDEPENDENT long-lived redundant producer. That is the
  point — redundancy here was the bug (V6), not the safety.
- **Declaration drift between lanes**: post-collapse there is ONE room, so
  the two-rooms-two-answers scoping caveat in Plan B §6 dissolves; until
  then both lanes declare identical sets by shared constant
  (`kMissionFoldDeclaredEntities`, already single-sourced launcher-side).
- **Ruling text is unrecorded** (C-W1): a future reader cannot audit "did we
  follow #42" — this plan quotes what it follows inline so it is auditable
  against ITSELF even if the register never lands.
- **Unanswered items, named**: C-1, C-2, the park-ladder constant
  discrepancy (250 ms→1 s vs →4 s), and whether any launcher code beyond the
  stream intake secretly assumes a separate stream process (TC-3's audit).

## 7. What this plan does NOT fix

- Build cost (Plans B/D3 territory): one producer still builds full cores
  for uncovered batches.
- The argv CALL half: 38 capability ids / ~46 subcommand paths stay argv
  (inventory: launcher `HANDOFF_durable_service_2026-08-13.md` §5b-c,
  corrected count at `DECISION_…:404-407` — evidence sweep). That migration
  is the DECISION doc's Stage 2, not this plan.
- The TCP socket lane stays undialled (D-C3): a second PROCESS attaching
  (mobile gateway, tools) is its future justification, not the launcher.
- Serve-absent boot latency (`laneAbsent` ~4.3 s window, §10.3 item 10) —
  untouched.

## 8. Standing constraints / collision map

`stream.py` OWNED tonight (TC-1's parity fixtures touch its output, not its
code — the fixtures tree is ALSO owned; TC-1 lands after hand-back).
`mission_control_bridge.dart` unowned but adjacent to owned
`mission_read_model.dart` — TC-2 sequenced after the fold agents hand back.
Fixture discipline for TC-1's goldens: extend via hermes' generator, copy
`MANIFEST.sha256` VERBATIM to the launcher (order is compared before bytes,
`check_producer_contracts.py:59-70`; tonight's divergence and fix at
launcher `e1d198985` — RELAYED); verify with the owning gate, never a
by-hash spot check. No `.hermes/` writes; no casual `harness serve` (TC-0's
serve-resident measurements ride the operator's EXISTING serve child's logs
or a probe root, never a fresh boot). Commit explicit paths.

## 9. Verification log

| # | Fact | How established |
|---|---|---|
| C-R1 | Hub subscribe op exists; launcher never sends it | Evidence sweep (serve.py:169-177; RAN Dart grep zero hits) |
| C-R2 | Lane A normally runs inside the serve child; separate process only as fallback | READ mission_control_provider.dart:239-243; serve_session_io.dart:1750-1838 |
| C-R3 | In-process builds serialized/coalesced, never concurrent | READ snapshot.py:345-388 |
| C-R4 | No TCP/WebSocket in launcher MC tree; hermes socket lane undialled | RAN greps; serve_socket.py:179 |
| C-R5 | Hub: bounded buffers, drop→close→resync, generation supersession, restart-free join floor | READ serve_stream_hub.py:461-607 |
| C-R6 | One fold body/watermark for both lanes | READ mission_control_bridge.dart:1902-1981 (via fold plan R7 + sweep) |
| C-R7 | O-L2 shipped lossy, reverted; live STALE receipt | MEASURED-§10 / §10.2(b) |
| C-R8 | Slow-backstop cutover precedent | READ DECISION_push_and_rpc_2026-08-13.md:165-169 |
| C-R9 | Ruling #42 text + "no serve no office" unrecorded anywhere | RAN exhaustive search; RELAYED quotes |
| C-A1 | Op advertisement on ready/version | ASSUMPTION — TC-1 |
| C-A2 | Single ordered per-connection writer; fat-frame delay only | ASSUMPTION — TC-1 |
