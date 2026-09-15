# The 24-second hydrate, read a second time — one build, three logged waits, and where +2.9 s actually came from (Plan HY, 2026-08-17)

> **Home.** `docs/agent-runtime-harness/`, beside `MISSION_BOOT_WINDOW_PLAN_2026-08-17.md`
> (Plan G — this plan is the independent second read of the same boot family; it corrects two
> of Plan G's derived numbers and defers first-build demotion design to BW-H1) and doc
> `14-snapshot-core-build-performance.md` (owns warm-build internals). Repos as read: hermes
> `ca19df1e48` (main), launcher `6a121cbe9` (main) — both verified (RAN, `git log`). Live
> evidence: `X:/Eternia/.hermes/profiles/base/logs/agent.log` (LOG, read-only), the launcher
> receipt `[08:29:17.998] [MissionBoot] open#1 authoritative` in
> `eternia_launcher_diag.log` (LOG), and one read-only SQLite open of
> `X:/Eternia/.hermes/agent-runtime/read_model.db` (`mode=ro&immutable=1` — no WAL/SHM
> created; RAN). Nothing under the live root was written, and no serve/gateway process was
> spawned.

**Evidence tags** — `READ` (file:line inspected at the SHAs above), `RAN` (read-only command
this session), `LOG` (line quoted with its timestamp from a live log), `RELAYED` (operator's
measurement, taken as given), `INFERRED` (derived, with the derivation shown), `A-n`
(assumption, §6).

---

## 0. Verdict up front

**Q1 — the three concurrent hydrate lines are ONE build, reported by three waiters; the
builder itself is invisible in the log.** `elapsed_ms` on a `reason=hydrate` line is
explicitly the caller's WAIT, not the build's duration — the code says so in as many words
(`agent_runtime/stream.py:123-128`: "NOTE what this measures: the hydrate's WAIT, which under
``accept_inflight`` may be a short ride on a build somebody else started (serve prewarms one
right after ``ready``)" — READ). The leader was the serve snapshot prewarm
(`serve.py:1507-1518` starts it just before the `ready` frame; `_prewarm_read_model_snapshot`
at `serve.py:845-871` calls `build_snapshot()` directly, which logs **nothing** — only
`hydrate_frame` emits `snapshot_build` lines, `stream.py:145-151` READ). The three hydrate
callers joined the running build via `accept_inflight` (`snapshot.py:354`:
`target = state["started"] + 0` while `running` — READ) and each logged its own arrival-to-
completion span. `_BUILD_COALESCE` did exactly what it documents. Proof it was one build in
one process, not three processes: §1.2.

**Q2 — the warm hydrate was not 23.0 s and the cold one was not 19.9 s; both numbers are
waits.** The true durations are **24,243 ms warm (MEASURED — the HUD chip IS the build-thread
number)** and **≈21.4 s cold (INFERRED ±0.5 s)**. The regression is therefore **≈+2.9 s, not
+3.1 s**, and it decomposes as: **≈+1.35 s** in the build segment that now runs concurrent
with the relocated `model_tools` import + plugin discovery (BW-H3's own matched A/B priced
that work at 1,332 ms warm), plus **≈+1.5 s unattributed**, which sits inside this build's
demonstrated warm-run variance band (2.4–3.9 s across five warm builds on 05:49's own log) and
is **not** data growth (exactly ONE store JSON file changed between the boots — RAN) and
**not** a build-code change (the `agent_runtime` diff between the two boots' checkouts touches
only `boot_timeline.py` and `tool_visibility.py` — RAN). The arithmetic closes to within the
variance band; it does not close to zero, and no stage below claims the residual.

**The parent hypothesis, judged.** Right mechanism, wrong route, wrong fix:
- RIGHT: BW-H3 moved the `model_tools` import (builtin-tool imports + 54-plugin discovery)
  out of the interpreter and into the post-`ready` window, where the first build now runs.
  That relocation is the only build-relevant code change between the boots and its measured
  price (~1.3 s) matches the attributable half of the regression.
- WRONG ROUTE: `snapshot.py:59` no longer triggers `model_tools` at import — that is the
  entire point of BW-H3's accessor (`tool_visibility.py:38-87` READ; module-scope it imports
  only `tools.registry`, `:11`). The payment site is CALL time: `_agent_summary` →
  `resolve_tool_visibility` at `snapshot.py:1927`, inside the `agents_readiness` timed
  section (`:468-486`). (`snapshot.py:1785` is `_agent_tool_detail` — the on-demand detail
  verb, not the build path.) And in THIS boot the importer was not the build thread at all:
  the provider-prewarm thread (`serve.py:1536-1540`, `from model_tools import
  get_tool_definitions` at `:896`) reached the import first — discovery registration lines
  run 08:28:58.259→58.527 and `get_tool_definitions`' check_fn storm starts at 58.541 in the
  same breath (LOG). The build thread most likely never blocked on the import lock; it lost
  **GIL share** to ~1.3 s of extra concurrent import CPU on a sibling thread, inside a build
  that is CPU-bound, not IO-bound (§1.4).
- WRONG FIX: starting `_prewarm_provider_runtime` BEFORE the snapshot-prewarm thread is a
  no-op. Both are daemon threads started within the same millisecond
  (`serve.py:1513-1518`, `:1536-1540`); reordering the two `Thread.start()` calls does not
  reorder the work. The provider prewarm spends its first ~5 s on the OpenAI SDK import + SSL
  context (`:882-893`) before it touches `model_tools` either way — measured both boots: the
  import landed at ready+4.8 s regardless. The fix that follows from the mechanism is the
  OPPOSITE shape: **serialize** the provider prewarm behind the first build (HY-H2), because
  nothing it warms is usable before the canvas is authoritative.

**The number that should redirect attention.** A fully WARM serve child (interpreter
1,506 ms, OS caches hot, zero data growth) still built its first core in **24.2 s**, against
2.4–3.9 s for its second build. ~21 s of the first build is per-process work (skill-registry
walks, YAML parse-cache fill, readiness memos, event-log materialization) that OS cache state
barely dents — which is why "warm" beat "cold" by so little that contention noise inverted
them. The only stage that buys the operator's 24.6 s back is first-build demotion (Plan G
BW-H1); everything else here is instrumentation, contention hygiene, and one launcher-side
redundant rebuild.

---

## 1. The proof

### 1.1 Reconstructed timeline, 08:29 boot (LOG + RELAYED, reconciled to ±0.1 s)

Anchor: `authoritative` logged 08:29:17.998 with `authoritative=29934` →
`open_requested` = 08:28:48.064 (check: `first_frame=681` → 08:28:48.745, and the
first-frame line is at 08:28:48.746 — exact).

| Wall clock | Event | Evidence |
| --- | --- | --- |
| 08:28:48.064 | `open_requested` | RELAYED receipt, anchor |
| 08:28:48.458 | first frame from 42 h-old cache | LOG launcher |
| 08:28:50.44 | serve child `Process.start` (`hygiene_serve_gate=2345`, BW-L4 live) | RELAYED spans |
| 08:28:53.379 | serve `ready`; boot timeline `interpreter_ms=1506 … bytecode_sweep_ms=0` | LOG agent.log |
| 08:28:53.447 | **first build starts, led by the snapshot prewarm** (= 17.690 − 24.243) | INFERRED from MEASURED build_ms; ready+68 ms matches the prewarm thread started just before the `ready` emit (`serve.py:1507-1518` READ) |
| 08:28:53.925 | `state.db (async_delegation)` WAL warning (once-per-process) | LOG |
| 08:28:54.665 | office lane subscribed (start) at baseline 88861422 | LOG launcher |
| 08:28:54.674 / 55.016 / 56.202 | three hydrate callers arrive and JOIN the running build (arrival = line time − elapsed_ms) | INFERRED from LOG |
| 08:28:58.18–58.53 | `model_tools` import completes on the provider-prewarm thread: 54-plugin discovery, `elapsed_ms=343` | LOG |
| 08:28:58.54–08:29:02.61 | `get_tool_definitions` availability storm (check_fns, credential pool, vision autodetect, `tool_search activated`) — concurrent with the build | LOG |
| 08:29:03.595 | `state.db` (SessionDB) WAL warning — the build has passed `agents_readiness` (`snapshot.py:506` opens it after `:468-486`) | LOG + READ |
| 08:29:17.690 / .703 / .716 | build done; three waiters wake and deep-copy out, 13 ms apart, all `offset=88867005` | LOG |
| 08:29:17.806 | office lane `resubscribe #1 (push:full_core) in 250ms` | LOG launcher |
| 08:29:17.998 | **authoritative** (`ready_to_authoritative=24614` ≈ 0.07 lead-in + 24.243 build + 0.3 delivery) | RELAYED, exact |
| 08:29:18.161→21.550 | **build #4**, led by the resubscribed stream producer: 3,389 ms, same offset | LOG; §1.5 |

### 1.2 Q1 settled: one build, one process, three waits — four independent witnesses

1. **The wait semantics are documented at the emit site.** `stream.py:121-128` (READ): the
   hydrate measures its own `build_snapshot(accept_inflight=True)` call, which under
   coalescing is a ride on the prewarm's build. The three `elapsed_ms` values differ by
   exactly the three arrival staggers (23,016/22,687/21,514 ↔ arrivals 54.674/55.016/56.202,
   all ending together).
2. **One process.** Both WAL warnings fire "once per process per database" (LOG text) and
   each appears exactly once in the window. A second building process (e.g. the
   `harness stream --max-frames 1` CLI hydrate lane, `mission_control_bridge.dart:1245-1261`
   READ) would have logged its own pair. None did — that lane never fired (it sits behind
   the readiness chokepoint pre-proof).
3. **One build.** Three completions 13 ms apart with elapsed values spanning 1.5 s cannot be
   three GIL-sharing builds; they are three sequential `copy.deepcopy` hand-offs out of the
   share slot (`snapshot.py:357`, `:381` READ). Identical `offset=88867005` on all three is
   the shared watermark.
4. **The leader is structurally invisible.** `_prewarm_read_model_snapshot` calls
   `build_snapshot()` with no logging (`serve.py:862-865` READ); `_log_snapshot_build` rides
   only `hydrate_frame`/rebaseline (`stream.py:146`, `:404` READ). So a prewarm-led boot logs
   N waits and zero builds — which is precisely the misread the brief arrived with, and what
   HY-0 fixes.

Who the three callers are: `hydrate_frame` is reachable only from `stream_frames`
(`stream.py:498`, `:537` READ), so each line is one stream attachment. One is the shared
socket-lane `StreamHub` producer, started by the office subscribe at 54.665
(`serve.py:1736-1804` READ — "One per serve, never per client"); one is the launcher's
long-lived stdio `stream` watch (`mission_control_bridge.dart:1442` READ). The third
attachment's identity is **not established** (§5) — it does not change the answer: N callers,
one build.

### 1.3 Q2 settled: the true durations, and where +2.9 s went

**Warm build = 24,243 ms, MEASURED.** The HUD chip is `'snapshot build ${buildMs}ms'`
(`mission_control_snapshot.dart:922` READ) rendering `parity.build_ms`, which is taken on the
build thread from build start to envelope (`snapshot.py:417`, `:965` READ). End 17.690 −
24,243 = start 08:28:53.447 = ready + 68 ms — the prewarm lead. `ready_to_authoritative`
24,614 = 68 lead-in + 24,243 build + ~300 delivery. Everything reconciles.

**Cold build ≈ 21.4 s, INFERRED (±0.5 s).** At 05:48 the same prewarm existed
(`serve.py:1507` predates the boot stages — the deferral diff touched only
`tool_visibility.py`/`boot_timeline.py`, RAN). Ready 05:48:44.942; the three hydrate waiters
arrived 46.407/46.732/46.744 and the build ended 06.344, so a prewarm-led build ran
≈44.95→06.344 ≈ 21.4 s. Corroboration: the `async_delegation` WAL mark fired at build+0.48 s
in the measured 08:29 boot and at 05:48:46.036 — i.e. ~1.1 s after 44.95, before the first
hydrate even arrived, so SOMETHING was already building. The alternative (prewarm crashed
silently — its failure is DEBUG-logged, invisible at INFO — and hydrate #1 led at 46.407,
making the cold build 19.9 s and the regression 4.3 s) cannot be fully excluded from this log
(A-1); the WAL timing argues against it.

**Attribution of +2.9 s.** Two process-wide clock probes bracket the build in both boots
(build start → SessionDB `state.db` WAL = events + agents_readiness + instance stores;
that WAL → build end = persona_chat + prompt_observability + boards + running_work + parity):

| Segment | 05:48 (cold) | 08:29 (warm) | Δ |
| --- | --- | --- | --- |
| start → SessionDB open | 8.80 s | 10.15 s | **+1.35 s** |
| SessionDB open → done | 12.59 s | 14.10 s | **+1.51 s** |

- The **+1.35 s** lands exactly where the relocated import ran (58.18–58.53 discovery,
  58.54–02.61 check_fn storm — all inside segment A) and matches BW-H3's own matched A/B
  price for that work: interpreter_ms 2710→1378, −1,332 ms warm
  (`tool_visibility.py:57-61` READ). Mechanism: GIL share stolen from a CPU-bound build by
  ~1.3 s of extra concurrent import work, not an import-lock block — by the time the build's
  first `_agent_summary` needed the registry, discovery was complete (the check_fns start
  13 ms after discovery-complete, so the prewarm thread owned the import). INFERRED, but
  measured at both ends.
- The **+1.51 s** is unattributed. Refuted as causes: data growth (ONE store file changed
  between boots, `events.jsonl` +22 KB — RAN `find -newermt`), build-code change (RAN diff),
  a second building process (§1.2). Remaining candidates are environment (drive/queue state
  on X:, which took >120 s to stat 12,384 store files this session — RAN — and OS cache
  residency) and the tail of the check_fn storm crossing the segment boundary. The same
  build's warm runs on 05:49's own log span 2,406→3,921 ms (five samples, LOG), a ±40%
  variance band that comfortably contains ±1.5 s at first-build scale. Claiming a cause from
  two samples would be manufacturing signal; HY-0 makes the next regression self-attributing
  instead.

### 1.4 Why "warm" barely beats "cold": the build is per-process CPU, not disk

Second-build-in-process: 2.4–3.9 s. First-build-in-process: 21.4–24.2 s regardless of OS
cache temperature. The ~20 s delta is per-process cache fill — the prewarm's own docstring
says so (`serve.py:848-852`: "~5s of that is per-process cache fill" as of 2026-08-09; it has
tripled since). The stale persisted core (read_model.db, written 2026-08-15 by the
`write_snapshot` path, `snapshot.py:1757-1766` READ — not on the serve path, hence 42 h old)
gives the warm section weights: `agents_readiness 2774, prompt_observability 1216,
persona_chat 806, events 3, boards_offices 11, running_work 36` of `build_ms 5485` (RAN,
read-only). The `agents_readiness` cost is skill-registry resolution walks + profile YAML +
content hashes (`profile_readiness.py:403-434` READ — the "102 recursive rglob/scandir walks
× ~104ms" note), **not** `store.py`'s `_read_json`: `store.py:57-64` is confirmed uncached
(READ) but the stores it serves total ~15 files here (5 agents + 3 workspaces + 3 realms —
RAN). The brief's "~7,300 raw_decode calls against agents_readiness" pairing is therefore
partially REFUTED: the uncached reader is real, the attribution to it is not established, and
the decode count itself could not be verified without profiling a build against the live root
(§5).

### 1.5 The fourth build: the launcher bought it

Build #4 (3,389 ms, led at 18.161, same offset) began 163 ms after `authoritative`. The
office lane had subscribed pre-hydrate at baseline 88861422 (54.665), saw the first
`full_core` push, and resubscribed (`resubscribe #1 (push:full_core) in 250ms` — LOG).
`StreamHub.subscribe` on a rejoin **restarts the shared producer**, and the serve code
documents the bill: "a re-baseline makes every OTHER subscriber on this hub pay a fresh full
core" (`serve.py:1786-1792` READ). A restarted producer runs `stream_frames` from the top →
`hydrate_frame` → a full build. Zero events had been appended (same offset), so the entire
build was redundant by watermark. This is the cheapest real seconds on the table after BW-H1,
and it is a launcher/lane-protocol fix, not a snapshot fix (HY-L2).

---

## 2. Validation

| Stage | Buys | Grade | Does NOT buy |
| --- | --- | --- | --- |
| HY-0 | 0 s — the leader, the build count, `build_ms`, and top sections land on the log line and the receipt; the wait stops impersonating the build | attribution | wall time |
| HY-H1 | ~20 s warm AND cold: `ready_to_authoritative` 24.6 s → ~2 s (Plan G BW-H1, adopted with two new constraints) | INFERRED target | warm rebuilds (doc 14) |
| HY-H2 | ≥1.3 s off the first build (the concurrent prewarm CPU share; bounded below by the BW-H3 A/B, measured post-hoc by HY-0) | INFERRED | the import itself (still paid once, after the build) |
| HY-L2 | one full redundant build (3,389 ms MEASURED this boot) + the HUD chip stops being overwritten by a post-boot rebuild | MEASURED churn | boot wall ms (it lands post-authoritative) |

No timing number above is a test assertion anywhere in §3 — witnesses are counts, ordering,
and absences.

---

## 3. Stages

Naming: HY = hydrate second read; H = hermes, L = launcher. Ordered by seconds bought
(attribution first, per FC-0/BW-0 precedent). Every stage rolls back by reverting its commit.

---

### HY-0 — both repos: the build leader becomes visible; waits stop impersonating builds

**Goal.** A boot's log answers, without reconstruction: how many builds ran, who led each,
how long the BUILD took (vs each caller's wait), and which sections dominated.

**Change surface.**
- hermes `agent_runtime/snapshot.py`: the coalesce leader path (`:371` return of
  `_build_snapshot_uncoalesced`) logs ONE line per actual build —
  `snapshot_build_core role=led build_ms=N offset=… sections_top=agents_readiness:N,…` —
  sourced from the parity envelope already computed in the result (no new measurement). The
  prewarm thereby logs itself with zero serve.py changes.
- hermes `agent_runtime/stream.py:_log_snapshot_build`: the hydrate line gains
  `waited_ms=` (rename-in-place of today's value, old key kept one release) and
  `build_ms=` read from the returned snap's `parity.build_ms`, so a wait and its underlying
  build are distinguishable on one line.
- launcher `mission_boot_timeline.dart` + receipt: the authoritative line carries the
  hydrate's `parity.build_ms` + top-3 `sections_ms` (both already delivered on the wire —
  parse, no protocol change); each stream attachment logs op + purpose at subscribe, closing
  the subscriber-census gap (§1.2, third caller).

**Tests.**
- hermes (new, `tests/agent_runtime/test_snapshot_build_logging.py`, using the existing
  `_build_snapshot_uncoalesced` monkeypatch seam): drive one leader and one
  `accept_inflight` joiner through `build_snapshot` with a barrier-gated counting fake.
  *Killing mutation:* emit `role=led` on every path. *Probed fields:* (1) the count of
  `role=led` records captured by the test's log handler == 1 while callers == 2, AND (2) the
  fake builder's invocation counter == 1. *Why the mutation cannot also set them:* the mutant
  emits two `led` records for two callers (its own output convicts it), and the counter lives
  in the test's fake — a mutant that double-builds must call it twice; a mutant that fakes
  the role cannot reduce the record count without restoring the branch it mutated.
- launcher: receipt-parsing unit test — the receipt's `build_ms` equals the value in a
  synthetic hydrate envelope, not any launcher-side subtraction. *Mutation:* derive it from
  `ready_to_authoritative` minus a constant. *Probed field:* the receipt value under a
  fixture whose envelope `build_ms` deliberately ≠ any span arithmetic available to the
  mutant (two driven values, FC-0 discipline).

**Rollback.** Revert; additive keys, consumers ignore unknown keys by contract.
**Acceptance (operator, next cold boot).** One `role=led` line per build; the receipt names
`build_ms` and the dominant section without anyone opening three logs.

---

### HY-H1 — hermes: the first build stops being a full build (adopt Plan G BW-H1; two constraints added by this read)

**Goal.** Unchanged from BW-H1 (persisted core + event-tail replay); this plan does not
re-design it. What this read adds:
1. **The value is larger than Plan G recorded.** BW-H1 was priced off a 19.9 s cold build;
   the measured reality is 24.2 s on a fully WARM child — the build is per-process work, so
   BW-H1 pays on EVERY boot, not just cold ones (§1.4).
2. **New constraint: the cache-hit path must not touch `resolve_tool_visibility`.** The
   persisted core already carries the tool rows; a cache-hit that still triggers the deferred
   `model_tools` import re-buys ~1.3 s and the check_fn storm. *Killing test (added to
   BW-H1's suite):* a cache-served boot under a test-owned `sys.meta_path` recorder asserts
   zero imports of `model_tools`. *Mutation:* import it anyway (e.g. via an eager
   "just in case" warm). *Why not settable:* the recorder is the test's finder; any import
   attempt must traverse it.
3. **Standing constraint, restated because it was nearly re-litigated today:** the freshness
   key is the stat-based fingerprint family (`serve.py:401 _runtime_state_fingerprint` READ,
   `running_work_store_paths` at `running_work.py:454-467` READ, scope backstop at
   `stream.py:657-692` READ with its two production incidents), never an event-offset-only
   key. The events section is 3 ms of a 5,485 ms build (RAN, §1.4) — an offset-keyed cache
   buys nothing and goes stale undetectably. The refused shape stays refused.

**Buys.** `ready_to_authoritative` ≈24.6 s → ~2 s (INFERRED target, measured by HY-0's
receipt after landing). **Sequencing.** After HY-0, independent of everything else here.

---

### HY-H2 — hermes: the provider prewarm follows the first build instead of fighting it

**Goal.** The first build gets the process to itself. Today two daemon threads race from
`ready`: the snapshot prewarm (leads the build the launcher is waiting on) and the provider
prewarm (~5–8 s of CPU: OpenAI SDK import, SSL context, and — since BW-H3 — the
`model_tools` import + discovery + check_fn storm), and under the GIL the second is
subtracted from the first (§1.3). Nothing the provider prewarm warms is consumable before
the canvas is authoritative: its purpose is the first chat turn's latency
(`serve.py:1528-1535` READ).

**Change surface** (hermes `serve.py:1507-1543`): one daemon thread, sequential:
`snapshot_prewarm()` then `_prewarm_provider_runtime()` (each already exception-isolated, so
a failed build still warms providers). The thread still starts before the `ready` emit so the
build leads the launcher's first request, exactly as today.

**Explicitly rejected alternative — the brief's one-line fix** (start
`_prewarm_provider_runtime` before the snapshot-prewarm thread): reordering two
`Thread.start()` calls issued microseconds apart schedules nothing; the provider prewarm
reaches `model_tools` ~5 s in (after SDK+SSL) in both measured boots regardless of start
order, and the contention integral is unchanged. REFUSED as a no-op, not as wrong-direction —
the direction (get the import out of the build's way) is this stage's, inverted.

**Edge named, not hidden.** With BW-L5 live, the chat outbox drains on the healthy edge
(serve-ready), so a queued send arriving mid-build now finds the SDK cold and pays ~1.7 s
inline plus contention with the build — which it already does today when the operator types
early; the prewarm was never a guarantee, only a usually-won race. If HY-0's receipts show
first-turn warmup misses after this lands, the refinement is "provider prewarm starts at
first-request-enqueue OR build-completion, whichever is first", not a revert.

**Tests** (extend `test_harness_serve.py`'s serve-loop harness — both prewarms are already
injectable):
- `the provider prewarm does not begin until the snapshot prewarm returns` — *Killing
  mutation:* restore the two parallel threads. *Probed field:* ordering via test-owned
  fakes — the snapshot-prewarm fake blocks on a gate the TEST holds; while held, the
  provider-prewarm fake's invocation record is asserted EMPTY; after release, asserted
  non-empty. *Why the mutation cannot also set it:* a parallel mutant invokes the provider
  fake while the gate is held — the record is appended by the fake the test owns, and the
  mutant cannot reach "warmed" without calling it. No elapsed-ms assertion exists.
- `ready is still emitted before either prewarm completes` (the boot must not slow down) —
  *Mutation:* join the prewarm thread before emitting ready. *Probed field:* frame order in
  the fake emitter vs the gated fake's still-held state — a joining mutant deadlocks before
  the assertion point can observe `ready`.

**Buys.** ≥1.3 s off the first build (the measured A/B price of the relocated import alone;
the SDK/SSL share on top is real but unpriced — INFERRED total 2–4 s), verified post-hoc by
comparing HY-0 `build_ms` receipts, never asserted in tests. **Rollback.** Revert.

---

### HY-L2 — launcher: the boot resubscribe stops buying a fourth full core (3.4 s MEASURED churn)

**Goal.** A boot in which zero events land between hydrate and resubscribe costs ONE build.
Today the office lane subscribes before the first hydrate, resubscribes on the first
`full_core` push, and the hub restart makes the shared producer re-run `hydrate_frame` — a
full redundant build 163 ms after authoritative (§1.5), which also overwrites the HUD's
build chip and steals the first seconds of post-boot interactivity.

**Change surface** (launcher office push lane — semantics deferred to Plans E/FC, which own
the fold fences): the boot-path office subscribe waits for the authoritative hydrate and
subscribes AT its watermark (the baseline the resubscribe converges to anyway), OR the
`push:full_core` resubscribe adopts the already-held core without a producer restart when its
baseline matches the in-hand watermark. Either shape must leave the FC-L2 fence semantics
byte-unchanged for genuine divergence (a full_core at a DIFFERENT offset still resubscribes).

**Tests** (launcher, extend the office-push lane suite):
- `a boot-window full_core at the held watermark does not request a resubscribe` — *Killing
  mutation:* resubscribe unconditionally. *Probed field:* the fake transport's recorded
  subscribe-request list (test-owned) — asserted length 1 across the boot sequence; the
  mutant's second request is recorded by the fake it cannot avoid calling.
- `a full_core at a DIFFERENT offset still resubscribes` (the fence keeps its teeth) —
  *Mutation:* never resubscribe. *Probed field:* the same list, asserted length 2 under a
  divergent-offset fixture. The two fixtures together pin the discriminator to the watermark
  comparison; neither blanket behavior passes both.
- hermes-side witness, once HY-0 lands: the acceptance run's log shows exactly ONE
  `role=led` build line for the boot window. **The witness is a BUILD COUNT.** No duration is
  asserted anywhere.

**Buys.** 3,389 ms of serve CPU per boot (MEASURED this boot), zero boot-wall ms (post-
authoritative). Ordered last for that reason despite being the best-measured number here.
**Rollback.** Revert. **Collision.** Same lane files as FC-L2/FC-H1 — do not land while an
unmerged FC branch holds them.

---

## 4. Sequencing

1. **HY-0 first and alone** — HY-H2's and HY-L2's acceptance both read receipts it creates,
   and BW-H1's acceptance (Plan G §BW-H1) gets `build_ms` for free from it.
2. **HY-H1 (= BW-H1) is the payload** and is independent of HY-H2/HY-L2. If it lands first,
   HY-H2 shrinks to hygiene (a cache-hit boot has no 24 s build to protect) — re-price
   before building HY-H2 in that order.
3. **HY-L2 defers to Plan E/FC fences** and should ride behind whatever FC stage next touches
   the fold chain.
4. **Do not revert BW-H3.** It is net-positive (−1.3 s × every hermes child, probe children
   included while they existed); this plan relocates its one regression, it does not
   re-litigate the deferral.

## 5. What could NOT be measured (and is therefore not claimed)

- **The 05:48 leader's true `build_ms`** — the prewarm logs nothing pre-HY-0; 21.4 s is
  INFERRED (±0.5 s), and the crashed-prewarm alternative (19.9 s, regression 4.3 s) is
  argued against, not excluded (§1.3, A-1).
- **The sections_ms of the 24,243 ms build** — delivered to the launcher in the hydrate's
  parity envelope and held in memory only; the on-disk core is 42 h stale. HY-0 puts it on
  the receipt.
- **The third stream attachment's identity** (§1.2) — census lands with HY-0.
- **The ~7,300 `raw_decode` count** — verifying it requires profiling a build against the
  live root (a ~20 s CPU+IO storm on the operator's drive mid-session) or a fixture rig;
  refused this session. The claim's ATTRIBUTION to `store.py:57-64` is independently
  refuted (§1.4); the count itself stays unverified.
- **The GIL-share split inside +1.35 s** (import vs check_fns vs scheduler noise) — needs
  HY-0's per-build sections on consecutive boots.

## 6. Adversarial pass — what I most expect to be wrong

1. **A-1: the prewarm led both first builds.** Evidence is the 68 ms lead reconstruction
   (08:29, strong — it reproduces `parity.build_ms` exactly) and the WAL-timing argument
   (05:48, weaker). If the 05:48 prewarm silently failed, the cold build was 19.9 s, the
   regression is +4.3 s, and the unattributed residual grows to ~2.9 s — strengthening
   HY-0's case and weakening nothing else (HY-H2's mechanism and bound are measured at
   08:29 alone).
2. **The +1.35/+1.51 segment split leans on the SessionDB WAL line as a phase probe.** It is
   a once-per-process marker of first open (`snapshot.py:506` path READ), but some earlier
   consumer opening the same DB in-window would shift the boundary. The 05:48/08:29 usage is
   symmetric, so the DELTA survives a constant shift; a boot where it does not fire at all
   would blind the comparison. HY-0's sections replace this crutch.
3. **HY-H2's edge case** (early chat send now always pays cold SDK) is priced from the SDK
   import figure in serve.py's own comment (~1.7 s), not re-measured. If receipts show it
   biting, the whichever-first refinement in the stage is the answer.
4. **HY-L2 assumes the resubscribe is watermark-redundant in general**, proven only for this
   boot (same offset). A boot where events land during the build makes the resubscribe carry
   real news; the divergent-offset fixture keeps that path alive by construction.
5. **Same confession as Plan G §6.6** — no live gesture, no spawned serve; every claim is
   log-forensics plus code reading at `ca19df1e48`/`6a121cbe9`.

## 7. Verification log

| # | Fact | How established |
| --- | --- | --- |
| HY-R1 | Repos at hermes `ca19df1e48` / launcher `6a121cbe9` | RAN `git log --oneline -20` both |
| HY-R2 | `elapsed_ms` on hydrate lines = caller's wait under `accept_inflight` | READ stream.py:110-128 |
| HY-R3 | Coalesce: joiner shares the RUNNING build; leader logs nothing; waiters deep-copy out | READ snapshot.py:256-387; serve.py:845-871, 1507-1518 |
| HY-R4 | 08:29 boot: ready 53.379; build 53.447→17.690; waits 23,016/22,687/21,514 @ offset 88867005; build #4 18.161→21.550 | LOG agent.log 1678-1755 |
| HY-R5 | 05:48 boot: ready 44.942; waits 19,937/19,625/19,625 @ 88844923; warm builds 2,406→3,921 (five samples) | LOG agent.log 1582-1677 |
| HY-R6 | HUD chip renders `parity.build_ms`; `build_ms` measured on the build thread | READ mission_control_snapshot.dart:541,922; snapshot.py:417,965 |
| HY-R7 | Receipt anchors: authoritative 08:29:17.998, spans reconcile to ±0.1 s | LOG eternia_launcher_diag.log 16915-16931 |
| HY-R8 | BW-H3 accessor: module scope imports only `tools.registry`; `model_tools` deferred to call time; matched A/B −1,332 ms | READ tool_visibility.py:11,38-99 |
| HY-R9 | Build pays tool visibility at snapshot.py:1927 (agents_readiness); :1785 is the detail verb | READ snapshot.py:468-486,1778-1797,1919-1927 |
| HY-R10 | Import ran on the provider-prewarm thread: discovery 58.18-58.53 (`elapsed_ms=343`), check_fns from 58.541 | LOG; READ serve.py:874-902,1536-1540 |
| HY-R11 | One process in-window: each WAL warning ("once per process per database") appears exactly once per boot | LOG |
| HY-R12 | Only build-path code diff between boots = tool_visibility deferral (+ boot_timeline stamps) | RAN `git diff --stat 7a145cd254..ca19df1e48 -- agent_runtime/` |
| HY-R13 | Data growth nil: ONE store JSON changed 05:50→08:28; events.jsonl 81.4 MB, offset +22 KB | RAN `find -newermt` (took >120 s over 12,384 files — the drive datum) |
| HY-R14 | Stale core sections: agents_readiness 2,774 / prompt_observability 1,216 / persona_chat 806 / events 3 of build_ms 5,485; written 2026-08-15 by `write_snapshot` (not on serve path) | RAN read-only sqlite (`mode=ro&immutable=1`); READ snapshot.py:1744-1767 |
| HY-R15 | agents_readiness driver = skill-registry walks + YAML + hashes, not store.py `_read_json`; store.py:57-64 uncached but serves ~15 files | READ profile_readiness.py:76-77,360,403-434; store.py:57-64; RAN store counts |
| HY-R16 | Hub restart on resubscribe re-bills every subscriber a full core; office resubscribe at 17.806 preceded build #4 | READ serve.py:1736-1804; LOG both logs |
| HY-R17 | Cache-refusal grounds intact: non-evented writers + two incidents + stat fingerprint | READ running_work.py:454-472; stream.py:657-692; serve.py:401-424; board_sync.py:172-216 |
| HY-A1 | Prewarm led the 05:48 build (⇒ cold ≈21.4 s) | Assumption argued in §1.3, fallout in §6.1 |
