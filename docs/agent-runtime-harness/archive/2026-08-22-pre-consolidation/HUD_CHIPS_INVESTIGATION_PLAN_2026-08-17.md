# The three HUD chips — one shared build wearing three log lines, a test that unpins its own sandbox, and five orphans the runtime already retired (Plan HC, 2026-08-17)

> **Home.** `docs/agent-runtime-harness/`, beside Plan G (`MISSION_BOOT_WINDOW_PLAN_2026-08-17.md`,
> whose BW-0/H2/H3 landed the morning this was written and whose BW-H1 was REFUSED — that refusal
> is a standing constraint on this plan, §5) and Plan F (`OFFICE_FOLD_FENCE_CONTENTION_PLAN_2026-08-16.md`,
> whose FC-H1 resubscribe-reason receipts appear in this boot's log). Repos as read: hermes
> `ca19df1e48` (main), launcher `6a121cbe9` (main) — both verified (RAN, `git log`). Live evidence:
> `X:/Eternia/.hermes/profiles/base/logs/agent.log` (serve child), the launcher diag log
> (`eternia_launcher_diag.log`, session opened 2026-08-17T12:23:35Z), the live runtime store under
> `X:/Eternia/.hermes/agent-runtime/` (READ ONLY — nothing under the live root was written; the
> SessionDB was **copied** to the session scratchpad and queried there), and the operator's
> 08:29:17 boot receipt (RELAYED).

**Evidence tags** — `READ` (file:line inspected this session at the SHAs above), `RAN` (read-only
command this session), `LOG` (line quoted from a live log with its timestamp), `STORE` (file read
from the live runtime store, or queried from a scratchpad **copy** of it), `RELAYED` (measured by
the operator, not re-derived), `A-n` (assumption, listed in §6).

**The three chips, verbatim:** `projection drops 5` (warning triangle) · `parity warnings 1`
(info) · `snapshot build 24243ms` (info). All three render from
`MissionControlSnapshot.snapshotAlerts` (`mission_control_snapshot.dart:897-928` READ): the drops
chip from `anomalousDroppedProjectionCount` (`:354-368`), the warnings chip from
`envelope.warningCodes` (`:257-260`), the build chip from `parity.build_ms` when ≥ 5000 (`:918-926`).
The icon split the operator sees is the code's own `hasDetail` branch (`snapshot_alert_badge.dart:27-31`
READ): the drops chip has no disclosure detail → warning triangle; the other two do → info.

---

## 0. Verdict up front — what each chip is, and four corrections to the brief

**Chip 1 — `projection drops 5` is honest accounting of five real orphans, and every one is now
named.** The count is `completeness[*].dropped` minus each row's declared `by_design` reasons
(`mission_control_snapshot.dart:354-368` READ). On this runtime the only anomalous contributor is
`persona_chat_history` / code `no_instance_match` (`persona_chat_history.py:372-378` READ): a
persona-chat session in SessionDB whose instance binding resolves to no live instance. The five
(STORE — four from the live store's own `snapshot.json` drop samples of 2026-08-15, the fifth from
a scratchpad copy of `profiles/base/state.db` cross-checked against `persona_instances/`):

| # | Session | Bound instance | Why it drops |
| --- | --- | --- | --- |
| 1 | `persona_chat_personainst_qa_agent_a9f00851_a04adf1dbe28` | `personainst_qa_agent_a9f00851` | instance retired 2026-08-09 (archive `20260809T183345Z_retire`) |
| 2 | `persona_chat_personainst_qa_agent_964f0387_63d19c87c9d6` | `personainst_qa_agent_964f0387` | retired 2026-08-09 (`…181736Z_retire`) |
| 3 | `persona_chat_personainst_qa_agent_c30e16a4_0d25ac4df978` | `personainst_qa_agent_c30e16a4` | retired 2026-08-09 (`…144238Z_retire`) |
| 4 | `persona_chat_personainst_qa_agent_920abc6d_0c65335c2482` | `personainst_qa_agent_920abc6d` | retired 2026-08-09 (`…070118Z_retire`) |
| 5 | `persona_chat_personainst_qa_agent_probe01_9a843c26098a` | `personainst_qa_agent_probe01` | the known `qa_agent_probe01` live artifact (UNIFIED_AGENT_CREATE plan §7); instance retired via the first-class verb 2026-08-16 20:53 with reason "created against a non-existent persona id (qa_agent); operator root cleanup" (LOG, `persona_instance.retired` in the live event slice) — **but the retire left its chat session behind**, and that session is the 4→5 growth the screenshots caught |

So the chip is not a projection bug — but it IS pointing at two real defects: (a) **the retire verb
orphans the instance's chat session**, so the counter grows by one on every retire, forever
(the growth the operator observed across sessions is exactly this); (b) all five drops were
**already handled by the operator via first-class retire** — every one of the five instance ids
resolves in `persona_instances_archive/*_retire/` (STORE) — yet the chip still counts them as
anomalies. The classification is wrong at the emission site, by parity.py's own rule
(`parity.py:36-41` READ: "would this count still be nonzero on a perfectly healthy runtime?" —
yes: retiring instances is normal lifecycle). HC-H2 fixes the classification; the chip goes to 0
on this runtime with no live-root surgery.

**Chip 2 — `parity warnings 1` is a live-store pollution alarm, and it is still being fed.** The
one deterministically-derivable warning on this store is `orphaned_office`
(`snapshot.py:1344-1354`, orphan flag set at `:1335` READ): the office surface
`ws_office_patch_test` exists in the live store (STORE, created `2026-08-16T10:24:31Z`) but no
such workspace exists (`workspaces/` has only `ws_codex-test-workspace_28d285`, `ws_default`,
`ws_testv4_afb811` — STORE). **Root cause found, with a smoking gun:**
`tests/agent_runtime/test_office_state_patches.py:751` calls **`monkeypatch.undo()`** mid-test —
which unwinds not only its own cap patch but the package's autouse
`isolate_agent_runtime_root` env pin (`tests/agent_runtime/conftest.py:91-105` READ; both use the
same function-scoped `monkeypatch` instance) — and then, three lines later (`:754`), calls
`seeded_office.upsert_actor(WORKSPACE, _actor_payload("qa", x=5.0, y=5.0))`, which now resolves
the **operator's live root**. The leaked actor `personainst_qa_agent_0001` sits in the live store
at position `[5.0, 5.0]` (the test's literal arguments), at **revision 67**, last written
`2026-08-17T11:49:30Z` (07:49 local, during this morning's BW work), with 67 `office.actor.upserted`
events in the live slice `events_archive/events.81417412.jsonl` (STORE + LOG) — one write per test
run since 2026-08-16 10:24Z. Two sibling call sites share the class:
`test_persona_chat_continuity.py:156` (physical evidence: the leaked lease
`persona_chat_leases/root_unlock_fail_91f23506f9a2.lock` in the live root — STORE) and
`test_mcp_admission_r2.py:327`. The hermetic guard fixture asserts **before** each test body
(`conftest.py:125-149` READ), so a mid-test unpin is invisible to it. HC-H1 fixes the three sites
and turns the fixture teardown into a tripwire; the chip clears when the operator archives the
leaked surface via the first-class verb the stage ships.

**Chip 3 — `snapshot build 24243ms` is ONE build, not three, and the coalesce worked exactly as
documented.** Full answer in §1.2; corrections to the brief:

1. **The three concurrent hydrates are three RIDERS on one build, not three builds.** The
   `snapshot_build` line is emitted **per hydrate caller** and measures that caller's *wait*
   (`stream.py:119-128` READ: "the number the client actually paid"; the log site is `:43-74`).
   The serve prewarm (`serve.py:1513-1518`, started just before `ready`) led one build
   (~24.3 s, ≈ the chip's 24,243 ms `build_ms`); three stream hydrates arrived at 08:28:54.674,
   :55.016 and :56.202 (back-computed from each line's `elapsed_ms`), joined the RUNNING build via
   `accept_inflight` (`snapshot.py:346-367` READ), and were all released together when it finished
   — which is why the three lines land 13 ms apart with elapsed deltas exactly equal to their
   arrival staggering, at the SAME offset. Not three processes: the log carries exactly ONE
   plugin-discovery pass and ONE serve boot-timeline line in the window (LOG), and Plan G §0
   Correction 3 recorded the identical two-waiter pattern on the 05:48 boot. The fourth line
   (3,389 ms at 08:29:21) is a real second build — warm — led by a caller that arrived after the
   first finished (`accept_inflight` only joins a *running* build).
2. **BW-H3 did move cost into this window — but only ~1–3 s of it, and the brief's file:line is
   the wrong call site.** `snapshot.py:1785` is the *detail-fetch* path
   (`_agent_tool_detail`, served on demand); the build-path call is `_agent_summary` at
   `snapshot.py:1927`, reached per agent inside the `agents_readiness` section (2,774 ms of the
   last recorded warm build — STORE, `snapshot.json` `sections_ms`). Post-BW-H3 the first call
   lazily imports `model_tools` (discovery + registry), visible in the log **after** ready
   (discovery complete 08:28:58.527; registry check_fns through 08:29:02.6 — LOG), i.e. inside
   `ready_to_authoritative`. But the dominant term predates BW-H3: the 05:48 **cold** boot paid
   its first build 19,937 ms *with `model_tools` already imported* (Plan G, LOG). The ~20 s base
   is the first-build-per-process tax (cold per-process caches + sections against this store),
   not the moved import. BW-H3's net: ≈ −19 s interpreter, ≈ +1–3 s first build. Net win stands.
3. **The proposed one-line fix (start `_prewarm_provider_runtime` before the snapshot prewarm
   thread) is a no-op and should not ship.** Thread *start order* sequences nothing: both are
   daemon threads racing under the GIL within microseconds either way, and the import lock
   serializes whoever arrives first regardless. Worse, the intent inverts a documented
   constraint: the snapshot prewarm must start **before** `ready` because only the build that
   started first can be shared with the boot hydrate (`serve.py:1507-1512` READ). The effective
   version of the intent is HC-H3: the provider prewarm runs **after** the first build completes,
   removing its ~4–9 s of import/registry work (and the GIL contention) from the
   `ready_to_authoritative` window. Even then, ~1 s (BW-H3's matched A/B: 0.92 s) of
   `model_tools` import stays inside the first build by construction — `_agent_summary` needs it.
4. **`ready_to_authoritative` = 82% of this boot — confirmed** (24,614 / 29,934 RELAYED), and the
   brief's chip-2/chip-3 kinship intuition is right: `build_ms` and the warnings ride the same
   parity envelope (`snapshot.py:970` `sections_ms`, `:989` warnings).

**The shape of the fix.** Two of the three chips are true alarms with root causes in *this week's
own tooling* (a self-unpinning test; a retire verb that leaks sessions); the third is an
observability artifact sitting on top of a real 24 s window whose dominant term is out of this
plan's scope (§5). Stages, ordered by operator impact: stop the live-root bleeding (HC-H1),
empty and un-grow the drops chip (HC-H2), make the build line say who led and who rode (HC-0),
take the provider warmup out of the authoritative window (HC-H3), and give the drops chip the
disclosure the other two chips already have (HC-L4).

---

## 1. The proof

### 1.1 The 08:28–08:29 boot, reconciled (LOG + RELAYED; launcher and serve clocks agree to ~0.1 s)

| Wall clock | Event | Evidence |
| --- | --- | --- |
| 08:28:48.458 | Mission Control open; first frame from 42 h cache | diag LOG |
| 08:28:48.482 | office push `unavailable (start): laneAbsent` → RPC lane | diag LOG |
| 08:28:53.3 | serve child `ready` (spawn_to_ready 2,943; interpreter_ms **1,506** — BW-H3 working) | agent.log 08:28:53.379 boot line |
| 08:28:53.3 | snapshot prewarm thread starts **just before** `ready` (`serve.py:1513-1518`), leads build #1 | READ + arithmetic below |
| 08:28:54.665 | office push `subscribed (start)` at baseline 88861422 | diag LOG |
| 08:28:54.674 | hydrate rider 1 arrives (23,016 ms wait → released 08:29:17.690) | agent.log, back-computed |
| 08:28:55.016 | hydrate rider 2 (22,687 ms) | agent.log |
| 08:28:56.202 | hydrate rider 3 (21,514 ms) | agent.log |
| 08:28:58.2–58.5 | plugin registration + `Plugin discovery complete: 54 found … elapsed_ms=343` — ONE pass, post-ready | agent.log |
| 08:28:58.5–08:29:02.6 | `tools.registry` check_fns + auxiliary-client init (`get_tool_definitions` territory) | agent.log |
| 08:29:03.6 | `state.db` WAL warning (build opening SessionDB); then **14 silent seconds of build** | agent.log |
| 08:29:17.69/.70/.72 | the three riders released together, same offset 88867005 | agent.log |
| 08:29:17.806 | office push `resubscribe #1 (push:full_core)` — FC-H1's reason receipt | diag LOG |
| 08:29:17.998 | **authoritative**; `ready_to_authoritative=24614` | diag LOG (receipt) |
| 08:29:21.550 | build #2, warm, 3,389 ms — led by a post-build-1 caller | agent.log |

Build #1 therefore ran ≈ 08:28:53.3 → 08:29:17.7 ≈ **24.3 s**, matching the chip's
`build_ms 24243` (the build-thread total the envelope carries; the riders' 23,016/22,687/21,514
are their own waits). The last recorded warm build's section split, for scale
(STORE, 2026-08-15 envelope, total 5,485 ms): `agents_readiness 2774, prompt_observability 1216,
persona_chat 806, parity 50, running_work 36, boards_offices 11, events 3`.

### 1.2 Who the three riders are (mechanism proven; identity inferred, A-1)

Every stream attach runs its own `stream_frames()` → `hydrate_frame()` →
`build_snapshot(accept_inflight=True)` (`stream.py:498`, `:110-128` READ). Three attach paths
exist in this boot's window: the office push lane's hub registration — restart-free by WV-H2,
**except** "if no producer is actually running … one is started regardless"
(`serve_stream_hub.py:534-549` READ), and the office subscribe at 08:28:54.665 is 9 ms before
rider 1 — plus the launcher's long-lived `harness stream` request and its one-shot
`harness stream --max-frames 1` hydrate (`mission_control_bridge.dart:1234-1254` READ; both
execute *inside the serve process* via the argv request pool, which is why they coalesce). The
per-rider identity is not recorded anywhere today — that is HC-0's job. The fourth (warm) build's
trigger — the office `push:full_core` resubscribe vs a launcher re-hydrate — is likewise
unattributed (A-2).

### 1.3 Chip 1's arithmetic, checked against the operator's screenshot history

The 2026-08-15 envelope (STORE): `persona_chat_history` dropped=103, reasons
`{limit: 99, no_instance_match: 4}`, by_design `["limit"]` → anomalous **4** — exactly the older
`projection drops 4` screenshot. `persona_chat_trace` dropped=16, all `tail_truncated`,
by_design → 0. The probe01 session (SessionDB `started_at` 2026-08-17T00:50Z, persona_id
`qa_agent`, instance `personainst_qa_agent_probe01` — STORE, scratchpad copy) is the only
post-08-15 orphan whose resolution provably fails all three lookups
(`persona_chat_history.py:361-371` READ: persisted instance dead; system-prompt marker infers
persona `qa_agent`, which no live instance carries — all 16 live instances are personas
`qa/dev/backend_dev/neko_supervisor/base/profile:alice` — STORE) → the fifth drop. Sessions of
*other* dead qa instances survive via the marker→`instances_by_persona["qa"]` fallback — which
silently renders them under an arbitrary live qa instance (last-wins,
`persona_chat_history.py:290-292`); real, out of scope, named in §5.

### 1.4 Chip 2's forensics, condensed

`test_a_workspace_over_the_projection_cap_keeps_the_honest_refresh`
(`test_office_state_patches.py:716-760` READ): patches the projection cap, asserts the refresh
degrade, then `monkeypatch.undo()` (`:751`) to prove the same write is foldable under the real
cap — and from that line on, `HERMES_AGENT_RUNTIME_ROOT` is gone, `EventLog()`/`OfficeStore()`
resolve the live root, the assertion at `:760` **passes against the operator's runtime**, and the
test stays green. 67 revisions in 30 h = the test suite's run cadence during the office/BW work.
The autouse hermetic assertion runs pre-body only (`conftest.py:125-149`), so nothing reddens.
This is the exact env-gap class the memory index's test fence entry exists for, one layer down:
the pin exists, is asserted before the body, and is un-done *by the test itself*.

---

## 2. Validation

### 2.1 What each stage buys, honestly

| Stage | Buys | Does NOT buy |
| --- | --- | --- |
| HC-H1 | Stops recurring live-root writes (67 so far, ongoing); a first-class verb that lets the operator clear `orphaned_office` → chip 2 to 0; the whole undo-class fenced forever | cleanup itself (operator's hands, §5) |
| HC-H2 | Chip 1: 5 → 0 on this runtime with zero live-root writes; the per-retire growth stops; genuinely-lost bindings STAY anomalous | the marker-rebinding wart (§5) |
| HC-0 | Attribution: every `snapshot_build` line says lead/ride, caller, and section split — the three-line ambiguity that consumed this investigation becomes one read | wall time |
| HC-H3 | The provider warmup's ~4–9 s of import/registry work (and its GIL/import-lock contention) leaves the `ready_to_authoritative` window; claimed as contention relief, measured on the next receipt | the ~20 s first-build tax (doc 14, §5); the ~1 s `model_tools` import inside the build (by construction) |
| HC-L4 | The drops chip becomes a disclosure surface naming hop/code/entity per drop — the next chip-1 investigation is a hover, not a database dig | any count change |

### 2.2 `isSettled` / retire condition — stated for every stage

None of the five stages touches the office write lane, fold timing, overlay state, or
`MissionOfficeSyncStatus`. Per stage: **HC-0/H1/H2/H3** are hermes-side (tests, projection
classification, serve thread order, log text) — no launcher predicate is reachable. **HC-L4**
touches only the header alert chips (`snapshot_alert_badge.dart`, `snapshotAlerts`), which sit on
the read path; `MissionOfficeLayoutController.isSettled` (`mission_office_layout_controller.dart:467-468`),
`writesInFlight` (`:433-434`) and the page retire condition (`mission_control_page.dart:426`,
`:2990-3006`) are untouched, and the standing fences
(`mission_office_optimistic_paint_test.dart` and siblings) must pass **byte-unchanged**; any edit
to them is a stage-stopping event (Plan E/G rule, adopted verbatim).

---

## 3. Stages

Naming: HC = HUD chips; H = hermes, L = launcher; numbering continuous across repos (BW
precedent). Ordered by operator impact: the two live-defect stages first (an actively-polluted
runtime store outranks boot seconds), then attribution, then the boot window, then disclosure.
No stage requires simultaneous deployment; every stage rolls back by reverting its commit. No
stage adds a cache (§5, BW-H1 refusal).

---

### HC-H1 — hermes: the sandbox stops being un-pinnable from inside a test, and the leaked surface gets a first-class exit

**Goal.** No test in `tests/agent_runtime` can write the operator's live root and stay green; the
operator can clear `orphaned_office` without touching the store by hand.

**Change surface.**
- `tests/agent_runtime/test_office_state_patches.py:751`, `test_persona_chat_continuity.py:156`,
  `test_mcp_admission_r2.py:327`: replace the shared-instance `monkeypatch.undo()` with a scoped
  `pytest.MonkeyPatch.context()` around the one patch each test actually meant to drop (the cap,
  the unlock stub, the deregister stub). The tests' assertions are untouched.
- `tests/agent_runtime/conftest.py::isolate_agent_runtime_root` (`:91-105`): the teardown after
  `yield` asserts `os.environ.get("HERMES_AGENT_RUNTIME_ROOT")` still equals the fixture's own
  root — a mid-test unpin now reddens the exact test that did it, with a message naming this
  incident (rev-67 actor, the lease file).
- A structural gate (same shape as the contract-version AST gate,
  `test_snapshot_contract_version_authority.py` precedent): no `monkeypatch.undo()` call under
  `tests/agent_runtime/` — the textual second witness.
- hermes `hermes_cli` office verbs: an archive verb for an **orphaned** office surface (extend the
  existing `harness office` family beside `resolve-conflict`, `snapshot.py:1362-1363`'s own fix
  hint precedent; implementer verifies none exists first — the board warning's "archive to
  repair" has no office twin READ). Refuses a surface whose workspace still resolves. The verb is
  shipped, **not run**: clearing `ws_office_patch_test` (and the lease file, and probe01's
  session row) is live-root surgery, operator's hands only — the standing rule from the
  unified-create plan §7 applies unchanged.

**Tests & anti-vacuity.**
- *Mutation:* re-introduce `monkeypatch.undo()` at `test_office_state_patches.py:751` (the exact
  historical regression). *What goes red:* that test itself, in `isolate_agent_runtime_root`'s
  teardown. *Probed field:* `os.environ["HERMES_AGENT_RUNTIME_ROOT"]` compared for equality
  against `str(root)` — the per-test `tmp_path`-derived value the fixture minted. *Why the
  mutation cannot also set it:* the mutation IS the deletion — `undo()` reverses the fixture's
  `setenv`, removing the variable; to satisfy the teardown it would have to re-set a value it
  cannot know (the fixture's `tmp_path` is per-test and owned by the fixture, not the test body),
  and re-setting it is un-doing the mutation. The structural no-undo gate reddens on the same
  mutant independently (two witnesses, different mechanisms).
- Verb test: archiving an orphaned surface removes it from `_offices_summary`'s rows and the
  `orphaned_office` warning; archiving a non-orphaned surface is refused. *Probed field:* the
  warning list computed by `_office_parity_warnings` over a fixture store before/after — presence
  then absence of the exact `entity_id`; the refusal path asserts the surface row still present.
  No timing anywhere.

**Mixed pairs.** N/A (tests + a new CLI verb; wire unchanged). **isSettled/retire:** §2.2.
**Rollback.** Revert. **Perf.** None.
**Acceptance (operator, live).** After the fix lands and the test suite runs once: the live
actor's `revision` stops advancing (it is at 67; any later write is a new incident). After the
operator runs the archive verb: the HUD shows no `parity warnings` chip on the next fresh frame.

---

### HC-H2 — hermes: a deliberately-retired instance's chat session stops counting as an anomaly

**Goal.** `projection drops` counts *lost* data, not the residue of the operator's own first-class
retires. On this runtime the chip goes 5 → 0; every future retire stops adding one.

**Change surface** (hermes `agent_runtime/persona_chat_history.py:372-378` + the persona-instance
store's archive accessor).
- At the `no_instance_match` drop site: before recording, resolve the session's persisted
  instance id against the persona-instance **archive** (`persona_instances_archive/*/<id>.json`;
  use/extend `PersonaInstanceStore`'s archive listing rather than a second path walk — one
  authority; the listing is read once per build and carried in the accountant's scope, not
  re-scanned per session). Resolves in the archive → `accountant.drop("instance_retired",
  by_design=True, entity_id=…)`. Does not resolve anywhere → today's anomalous
  `no_instance_match`, unchanged.
- Classification is per-CODE and declared at the emission site, which is exactly parity.py's
  contract (`parity.py:36-41`, `:154-172` READ): `by_design` is additive on the completeness row,
  envelope version unchanged, and the launcher already subtracts declared codes with no repo-B
  change (`mission_control_snapshot.dart:339-368` READ — the declaration path wins over the
  static fallback). `SNAPSHOT_CONTRACT_VERSION` (54) does not move: reason codes are open
  strings on an existing map.

**Tests & anti-vacuity** (extend the persona-chat-history projection tests).
- Fixture: three sessions — (a) bound to a live instance, (b) bound to an instance present only
  in the fixture's archive, (c) bound to an id present nowhere. *Assertions:* (a) included; the
  accountant summary's `reasons` == `{"instance_retired": 1, "no_instance_match": 1}` exactly,
  and `by_design` == `["instance_retired"]` exactly.
- *Mutation:* classify every unresolved binding as `instance_retired` (the dangerous direction —
  it would hide real corruption behind a green chip). *What goes red:* case (c). *Probed field:*
  the summary's `reasons` dict — `no_instance_match` must still be present with count 1 and
  MUST NOT appear in `by_design`. *Why the mutation cannot also set it:* (b)'s and (c)'s instance
  ids are distinct test-minted strings, and only (b)'s exists in the archive directory the test
  itself populated; emitting different codes for (b) and (c) requires actually consulting that
  archive — a constant classification fails one of the two by construction. The reverse mutant
  (never consult; keep everything anomalous — i.e., today's code) is killed by (b)'s
  `instance_retired` assertion. Counts and dict contents only; no timing.

**Mixed pairs.** Old launcher + new hermes: `instance_retired` arrives in `by_design`, and the
declaration path already subtracts it; a pre-declaration launcher (none fielded — the
`by_design` contract shipped before contract 54) would fall back to its static list and show the
old count, which is the documented compatibility posture (`mission_control_snapshot.dart:310-330`).
New launcher + old hermes: nothing new read. **isSettled/retire:** §2.2. **Rollback.** Revert.
**Perf.** One archive listing per build (58 directories today), memoized per build.
**Acceptance (operator, live).** Next fresh snapshot: no `projection drops` chip (all five
current drops resolve in the archive — verified STORE); the Snapshot Inspector's completeness
detail still shows the five, now under `instance_retired`/by-design. Then retire any test
instance via the first-class verb: the chip does not appear.

---

### HC-0 — hermes: the `snapshot_build` line says who led, who rode, and where the time went

**Goal.** The ambiguity that cost this investigation its longest detour — three lines that look
like three builds — becomes unreadable-wrong: every line carries the caller and its role, and a
slow build carries its own section split.

**Change surface** (hermes `agent_runtime/snapshot.py` + `agent_runtime/stream.py` +
`hermes_cli/harness_parts/serve.py`, `runtime_commands.py` — log text only, wire unchanged).
- `build_snapshot(..., build_info: dict | None = None)`: when passed, the builder fills
  `{"role": "led"|"rode"|"shared_next", "generation": N}` — `led` for the caller that ran
  `_build_snapshot_uncoalesced`, `rode` for an `accept_inflight` join of a running build,
  `shared_next` for a waiter that shared the next build's result. Pure out-param; return value
  and coalesce behavior untouched.
- `stream_frames(..., caller: str = "cli")` / `hydrate_frame(..., caller=...)`: the serve hub's
  producer factory passes `caller="hub"` (`serve.py:1736-1757`), the CLI stream command passes
  `caller="cli"` (`runtime_commands.py:505`), and `_log_snapshot_build` emits
  `role=… caller=… generation=…` plus, when the snapshot's own `parity.build_ms` ≥ 5000, the top
  three `sections_ms` entries. The prewarm (`serve.py:845-871`) logs one line of its own
  (`reason=prewarm role=led`), closing the gap where the most expensive build in this boot was
  the only one with no line at all.
- The office push resubscribe reason (FC-H1) already reaches the launcher log; this line is the
  serve-side half of the same join.

**Tests & anti-vacuity** (extend `tests/agent_runtime/test_stream.py` + the coalesce tests).
- One test drives three callers through `build_snapshot` on threads, with
  `_build_snapshot_uncoalesced` patched by the test to (i) count invocations and (ii) block on a
  test-owned gate: caller A leads (gate held), caller B joins with `accept_inflight=True`,
  caller C arrives without it; gate released. *Assertions:* A's `build_info["role"] == "led"`,
  B's `== "rode"`, C's `== "shared_next"`; the invocation counter == 2 (A's build + C's next) —
  the coalesce witness as a **call count**, never a duration.
- *Mutation:* hardcode `role="led"`. *What goes red:* the same test, on B and C. *Probed field:*
  each caller's own `build_info` dict (identity-owned by the test; one dict per caller). *Why the
  mutation cannot also set it:* three callers in one test expect three different values produced
  by three different code paths the test's gate forces them down; a constant matches at most one.
  The counter independently convicts a mutant that "fixes" role by making everyone lead
  (counter would read 3).

**Mixed pairs.** Log text only. **isSettled/retire:** §2.2. **Rollback.** Revert. **Perf.** A
dict fill and a string append per build.
**Acceptance (operator, live, next boot).** The boot window's agent.log reads
`reason=prewarm role=led … sections=…` followed by N `reason=hydrate role=rode caller=…` lines —
and the question "why three concurrent builds?" can never be asked of this log again. The
rider-identity assumption (A-1) and the fourth-build trigger (A-2) become recorded facts.

---

### HC-H3 — hermes: the provider prewarm yields to the boot-critical first build

**Goal.** The `ready_to_authoritative` window contains the first build and nothing else hermes
controls: the OpenAI SDK import (~1.7 s), CA verification (~0.7 s), and the
tool-definition/registry warmup (~1.2 s + check_fns; observed 08:28:58.5→08:29:02.6 this boot)
run **after** the first default-store build completes instead of contending with it.

**Change surface** (hermes `hermes_cli/harness_parts/serve.py:1507-1543`).
- `_prewarm_provider_runtime` becomes an injectable parameter of `serve_loop` (the same
  test-injection contract `snapshot_prewarm` already has, `:946` READ).
- The independent thread start at `:1536-1540` is removed; the snapshot-prewarm worker runs
  `build_snapshot()` and then, on the same thread, the provider prewarm. The pre-`ready` start
  of the snapshot prewarm (`:1507-1518`) is untouched — that ordering is load-bearing and
  documented, and this stage preserves the comment block's actual invariant (`:1541-1543`: the
  read-model build "must not queue behind this one's ~3 s SDK import" — it now provably cannot).
- Accepted cost, stated: a chat turn sent inside the (post-HC-H3, shorter) boot window pays the
  cold SDK import inline, exactly as every turn did before the prewarm existed — best-effort by
  the prewarm's own contract (`:874-881`). The canvas is not yet authoritative in that window, so
  the exposure is the rare pre-paint send.
- What this stage does NOT claim: the ~1 s `model_tools` import that `_agent_summary`
  (`snapshot.py:1927`) triggers inside the first build stays there — moving it earlier on the
  same thread changes which line of the same window pays it, and moving it pre-`ready` re-taxes
  `spawn_to_ready`. BW-H3's regression fence ("the serve prewarm still warms tools post-ready")
  keeps passing: the warmup still happens, later.

**Tests & anti-vacuity** (extend `test_harness_serve.py`'s serve-loop harness).
- The test injects a fake `snapshot_prewarm` that blocks on a test-owned gate G, and a fake
  provider prewarm that appends to a test-owned recorder. Drive `serve_loop` to `ready`. *Assert
  in order:* (1) the ready frame was emitted while G is still held (prewarm must not delay
  ready); (2) the provider recorder is **empty** at this instant — probed while the build is
  provably pending, because G is test-owned and unreleased (the BW-L5 "never-completing fake"
  pattern); (3) release G → the recorder gains exactly one entry.
- *Mutation:* restore the independent thread start (today's code). *What goes red:* assertion
  (2). *Probed field:* the recorder's length at the gated instant. *Why the mutation cannot also
  set it:* the mutant starts the provider prewarm unconditionally after `ready`, before anything
  releases G; it cannot un-append, and it cannot release G (the gate is the test's). Ordering
  and counts; the stage's Perf claim lives in HC-0's receipts, not in any test.

**Mixed pairs.** N/A. **isSettled/retire:** §2.2. **Rollback.** Revert. **Perf.** Removes the
provider warmup's CPU and import-lock contention from the window; honest sizing awaits HC-0's
`role`/`sections` line on the next boot (the check_fns are partly network I/O that releases the
GIL — the win is real but smaller than the 9 s wall span, A-3).
**Acceptance (operator, live, next boot).** agent.log shows plugin discovery and the registry
check_fns AFTER the `reason=prewarm` build line completes, and the receipt's
`ready_to_authoritative` drops by the measured contention share.

---

### HC-L4 — launcher: the drops chip gets the disclosure the other two chips already have

**Goal.** `projection drops 5` currently renders `hasDetail=false` — a bare amber triangle with
no way to see WHICH five (`snapshot_alert_badge.dart:43-45`,
`mission_control_snapshot.dart:542-543` READ) — while the envelope has carried per-drop samples
(`hop`/`code`/`entity_id`/`by_design`) this whole time (`parity.py:64-81`, launcher parse at
`mission_control_snapshot.dart:249-252`). This investigation named the five by querying a
database copy; the chip should have named them on hover.

**Change surface** (launcher `mission_control_snapshot.dart` + `snapshot_alert_badge.dart`).
- `MissionSnapshotEnvelope` gains `anomalousDropSummaries`: the `drops` samples with
  `by_design == false`, formatted `hop · code · entity_id` (additive field; the existing
  `dropSummaries` string list is untouched for its consumers).
- `MissionSnapshotAlert` gains `dropSummaries`; `snapshotAlerts` passes them on the
  projection-drops alert. `hasDetail` becomes true when they are non-empty → the chip becomes the
  same `_SnapshotAlertDisclosure` surface as the parity chip (icon flips triangle → info, a
  deliberate, named visual change); the panel gains a "Projection drops" heading rendering each
  summary as its own `SelectableText`, mirroring the warning-codes rows. The **label string is
  byte-identical** (`projection drops N`), so `snapshotAlertLabels`' label-only consumers
  (mobile strip summary, drawer notice — `:930-933`) are unchanged.

**Tests & anti-vacuity** (extend `mission_control_snapshot_test.dart` +
`snapshot_alert_badge_test.dart`).
- Fixture envelope: two anomalous samples with distinct test-minted entity ids, one
  `by_design: true` sample. *Assertions:* `anomalousDropSummaries` contains exactly the two
  anomalous ids; the pumped badge popover contains a `SelectableText` per id.
- *Mutation 1:* build the detail from the unfiltered `drops` list. *What goes red:* the
  by-design sample's entity id appears — asserted ABSENT by id. *Mutation 2:* force
  `hasDetail=true` with empty rows. *What goes red:* the two anomalous ids are asserted PRESENT
  by exact string in the rendered tree. *Probed field, both:* the rendered `SelectableText`
  contents (widget-tree find by the fixture's literal id strings). *Why neither mutation can
  also set it:* the ids are per-fixture strings minted in the test; producing exactly the two
  anomalous ones and not the third requires reading the parsed samples AND their `by_design`
  flag — no constant, no unfiltered copy, satisfies both fixtures.

**Mixed pairs.** Old hermes + new launcher: `drops` already ships; a hermes so old it lacks
per-sample `by_design` yields `false` (parse default) → samples show as anomalous, matching that
hermes' own accounting. **isSettled/retire:** §2.2 — header chips only; office fences
byte-unchanged. **Rollback.** Revert. **Perf.** A filtered list per applied frame.
**Acceptance (operator, live).** Hovering `projection drops N` names each drop
(`persona_chat_history · no_instance_match · persona_chat_personainst_…`) — after HC-H2, an
empty chip, and after any future real loss, the entity id on screen.

---

## 4. Sequencing constraints

1. **HC-H1 lands first and alone** — it is the only stage racing an active defect (the leaked
   actor advanced to revision 67 *during this investigation's morning*; every test-suite run
   until it lands writes the live root again). It has no dependency on any other stage.
2. **HC-H2 is independent of everything** (projection + tests; no wire move). Land any time; its
   acceptance needs one fresh snapshot, not a boot.
3. **HC-0 before HC-H3's acceptance run, not before its merge** (BW-0/BW-H2 precedent): HC-H3's
   Perf claim is read off HC-0's `role`/`sections` line and the receipt's
   `ready_to_authoritative`; landing HC-0 first keeps the A/B honest. HC-H3 also depends on
   HC-0's prewarm log line existing to prove the new ordering live.
4. **HC-L4 is independent**, but its acceptance is more legible after HC-H2 (the chip it
   discloses will usually be absent; the widget-test fixtures carry the content).
5. **Write-lane collision map:** none of these stages touches
   `mission_office_layout_controller.dart`, the office RPC arms, or `stream.py`'s frame shapes.
   `serve.py` edits (HC-0 log line, HC-H3 thread order) are textual neighbors of FC-H1's home
   surface — rebase-level only. `snapshot.py` edits (HC-0 out-param) are additive beside the
   coalesce block; do not land while an unmerged branch holds `build_snapshot`'s signature.
6. **Office fences byte-unchanged** across all five stages
   (`mission_office_optimistic_paint_test.dart`, `mission_office_lane_reattach_test.dart`,
   `mission_office_mass_archive_incident_repro_test.dart`); an edit to any of them is a
   stage-stopping event, not a test update.
7. No stage requires simultaneous deployment; each rolls back by reverting its commit.

## 5. Not in scope

- **The ~20 s first-build-per-process tax** (the dominant term of chip 3; 14 silent seconds of
  this boot's build even after the warmup noise). Doc
  `14-snapshot-core-build-performance.md` owns it. This plan's contribution is the section split
  on the log line (HC-0) so the next owner starts attributed. The warm floor on this store today:
  3,389 ms (LOG) / 5,485 ms with sections (STORE).
- **Any snapshot core cache, including re-litigating BW-H1.** The refusal stands and this plan
  respects its grounds: an event-offset-keyed cache is gated on the wrong axis — events are ~3 ms
  of a 5,485 ms build, and `running_work.py:454-467` (READ) plus `board_sync.py` mutate durable
  state with NO EventLog event; `stream.py:657-692` (READ) documents the two live incidents
  (2026-07-25 frozen Chat History, 2026-08-11 phantom "Terminal · running") that exact gap
  already caused. No HC stage introduces a cache of any kind.
- **Cleaning the live root** (`ws_office_patch_test` + its actor, the `root_unlock_fail_*` lease,
  probe01's session row). Live-root surgery is the operator's hands only — the standing rule the
  unified-create plan recorded for exactly these artifacts. HC-H1 ships the verb; the operator
  runs it.
- **The marker-rebinding wart** (§1.3): a dead instance's session with the
  "Mission Control persona chat for `<persona>`" marker silently renders under whichever live
  instance of that persona iterates last (`persona_chat_history.py:290-292`, `:363-371`).
  Misattribution, not loss; it belongs to the chat-history lane's identity work, and fixing it
  changes visible chat placement — decide out loud, not here.
- **The retire verb ending its chat session at source** (the alternative to HC-H2's
  classification). Rejected for this plan: it changes SessionDB rows' lifecycle (Chat History
  deliberately lists archived sessions — `persona_chat_history.py:905-925` `include_archived=True`
  READ), and HC-H2 empties the chip without touching session state. If the product later wants
  retired-instance chats out of the directory entirely, that is a visible-behavior decision.
- **The boot's triple-attach fan-in** (long-lived stream + one-shot hydrate + hub producer churn,
  §1.2) and the fourth build's trigger. Today they coalesce to one build — the machinery worked;
  collapse the lanes only if HC-0's receipts show real double-builds on future boots.
- **The archive stamp bug**: `persona_instances_archive/20260816T205337Z_retire` encodes local
  time with a `Z` suffix (probe01 retired 00:53:37Z; stamp says 20:53:37Z-as-written). Cosmetic,
  one-line, but it is a store-layout change — noted for the next janitor pass.
- **The `Not checked | Local` strip, the HOSTING pill, and "desktop is a gateway host"** —
  context on the operator's screen, none of it produced by the three chips' data paths (the
  gateway relay subscribes to Centrifugo, not the harness stream — `mission_gateway_relay.dart:95-106`
  READ).
- **SQLite 3.40.1 WAL upgrade** — still real, still owned by `hermes update` tooling.

## 6. Adversarial pass — what I most expect to be wrong

1. **A-1: the rider identities.** "hub producer + long-lived stream + one-shot hydrate" fits the
   timestamps and the code paths, but nothing recorded it. If a fourth attach path exists, HC-0's
   claim is unaffected (it records whatever the truth is); only §1.2's narrative moves.
2. **A-2: the single live parity warning is `orphaned_office`.** Derived offline from store
   predicates (boards clean, instance FKs clean, aliases clean — STORE); the warning producers I
   could not fully evaluate without a build are the trace and operator-channel FK families. If
   the live code differs, chip 2's *label* attribution moves — but the leak finding stands on
   the store and event evidence independently, and HC-H1 is justified either way. HC-L4's
   warning-codes disclosure (already shipped) answers this on screen today: the operator can
   hover the chip and read the code — worth doing before building anything.
3. **A-3: HC-H3's size.** The 4–9 s wall span of warmup inside the window is measured; its GIL
   share is not (check_fns do network I/O). If the receipt moves < 2 s, HC-H3 was still correct
   (the window should not contain foreign work) but drops below HC-L4 in retrospective value.
4. **The fifth drop = probe01** is the strongest candidate consistent with every checked fact
   (post-08-15, provably unresolvable, matches the 4→5 timing), but the live envelope was not
   readable this session (the store's `snapshot.json` is a 2026-08-15 write). If the live fifth
   is a different session, HC-H2 almost certainly still zeroes the chip (every orphaned binding
   found in the SessionDB copy that fails resolution traces to a `*_retire` archive entry), and
   HC-L4 would have answered this in one hover.
5. **The rev-67 write cadence = test-suite runs** is inferred from the interval pattern and the
   literal `[5.0, 5.0]` payload match; no process attribution exists for store writes. If some
   OTHER runner also writes this surface, HC-H1's teardown tripwire catches the pytest vector and
   the verb still clears the surface — but revision 68+ after HC-H1 lands would prove a second
   vector and reopen the hunt.
6. **Unverified live, all of it** — no serve child spawned, no gesture sent, the running launcher
   untouched; every claim is code-read + log/store forensics, per this task's constraints. The
   one thing that would have settled A-2/A-4 in seconds — a fresh parity envelope — is exactly
   what could not be minted without running a build against the live root.

## 7. Verification log

| # | Fact | How established |
| --- | --- | --- |
| HC-R1 | Repos at hermes `ca19df1e48` / launcher `6a121cbe9` | RAN `git log --oneline -20` both |
| HC-R2 | Chip producers: alerts at `mission_control_snapshot.dart:897-928`; anomalous-drop arithmetic `:354-368`; `by_design` declaration wins `:339-346`; build chip gated ≥ 5000 `:918-926`; icon split `snapshot_alert_badge.dart:27-31` | READ |
| HC-R3 | Boot 08:28:48–08:29:21 timeline; three riders' waits 23016/22687/21514 released 13 ms apart at offset 88867005; warm build 3389 | LOG agent.log 1752-1755 + diag session 12:23:35Z |
| HC-R4 | Coalesce serializes; `accept_inflight` joins a RUNNING build only; the log line is per-caller wait | READ snapshot.py:255-388, stream.py:43-151 |
| HC-R5 | ONE process built: one plugin-discovery pass (08:28:58.527), one boot-timeline line; prewarm starts pre-`ready`, provider prewarm post-`ready` on its own thread | LOG + READ serve.py:845-902, 1507-1543 |
| HC-R6 | BW-H3 moved the `model_tools` import to first call; the build-path call is `_agent_summary` snapshot.py:1927 (the brief's `:1785` is the detail fetch); matched A/B 0.92 s | READ snapshot.py:57-60,1783-1797,1919-1949; tool_visibility.py:41-95; git show 84d8249f33, b38f8dbd50 |
| HC-R7 | Cold first build was 19,937 ms with imports prepaid (05:48 boot) — the ~20 s base predates BW-H3 | Plan G §0 Corr. 3 (LOG there) |
| HC-R8 | 2026-08-15 envelope: anomalous = 4 (103 − 99 by-design `limit`), the four `no_instance_match` samples named; `sections_ms` split; warnings empty | STORE snapshot.json (read-only) |
| HC-R9 | probe01: session minted 00:50:45Z, instance retired 00:53:37Z via first-class verb; session survives in SessionDB; persona `qa_agent` matches no live instance | STORE scratchpad copy of state.db + live event slice + persona_instances/ (16 files) |
| HC-R10 | All five orphaned bindings resolve in `persona_instances_archive/*_retire/` | STORE (grep of archive) |
| HC-R11 | `ws_office_patch_test`: surface created 2026-08-16T10:24:31Z, no matching workspace; actor `[5.0, 5.0]` revision 67, last write 2026-08-17T11:49:30Z; 67 upsert events in the live slice | STORE + LOG events_archive/events.81417412.jsonl |
| HC-R12 | The leak vector: `monkeypatch.undo()` at test_office_state_patches.py:751 unwinds the autouse isolation pin, then `:754` writes `x=5.0, y=5.0`; hermetic guard asserts pre-body only; two sibling sites, one with physical evidence (`root_unlock_fail_91f23506f9a2.lock` in the live `persona_chat_leases/`) | READ test file :716-760, conftest.py:60-149; STORE lease dir |
| HC-R13 | `orphaned_office` producer and the orphan flag; board/instance/alias predicates evaluated clean offline | READ snapshot.py:1294-1367,1370-1653; STORE boards/, workspaces/, persona_instances/, persona_instance_aliases.json |
| HC-R14 | Hub: every stream-lane subscribe restarts the producer; office joins restart-free EXCEPT when no producer runs; launcher stream requests execute in the serve process | READ serve_stream_hub.py:520-591, serve.py:1690-1804,2258-2320; mission_control_bridge.dart:1231-1300 |
| HC-R15 | BW-H1 refusal grounds re-verified: event-less writers exist and are documented with two live incidents | READ running_work.py:454-467, stream.py:657-692 |
| HC-R16 | R#nn register (gesture plan §10.4) cross-checked against code read this session: nothing contradicted (R#42's lane ledger still open — `write lane: 5 rpc, 0 cli` diag LOG; R#10 landed per merge history). R#41 (the red tombstone gate) NOT re-verified — no test suite was run this session | READ + LOG |
| HC-A1 | Rider identities; fourth build's trigger | Assumption — §6.1/§6.2, settled by HC-0 |
| HC-A2 | The live warning is `orphaned_office`; the live fifth drop is probe01 | Assumption (strongest offline derivation) — §6.2/§6.4 |
