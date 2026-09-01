# Planned — hermes test-suite performance (the 32:37 serial wall)

**Status:** PLANNED 2026-09-01 — diagnosis complete, nothing implemented.
**Evidence:** [`hermes-suite-perf-field-notes-2026-09-01.md`](hermes-suite-perf-field-notes-2026-09-01.md)
(all § references below point there). Diagnose-only lane: this plan and the
field notes are the whole deliverable; no test or fixture was changed.

**Question this answers** (operator, 2026-09-01): *why is the suite slow on
this machine — is it hangs?* — **No. There are no happy-path hangs** (§4: every
≥10 s sleep literal is a kill-target child or a "never responds" thread stub;
poll intervals are 0.01 s; the 20 s/180 s constants are failure ceilings under
a 30 s pytest-timeout). The 32:37 is structural, and it decomposes cleanly:

| # | cost | share of the measured lanes | evidence |
|---|---|---|---|
| 1 | **Serial, single-process execution** — plain `python -m pytest` uses 1 of 16 cores; the repo's own per-file parallel runner is not on the operator's path | the whole wall is exposed to it | §5, §7 |
| 2 | **Per-test setup tax, and it AGES** — 28 ms/test autouse floor in a fresh process, **77 ms mean (max 670 ms) by the end of a 4.7 k-test process** (numbered tmp-dir scans degrade 0.46→5.8 ms/call as `basetemp` fills; the rest unattributed). Measured setup+teardown phases total **~13.4 min ≈ 41% of the 32:37** | ~13 min across the lanes | §2, §6, §6b-i |
| 3 | — of which `_live_system_guard`'s per-test `psutil children(recursive=True)` walk alone | **10.9 ms/test ≈ 2.1 min** | §2 |
| 4 | **Concentrated slow-test classes** — call time is top-heavy (top-400 rows = 72% of agent_runtime call time): (a) deliberate e2e serve boots ~117 s (untouchable); (b) whole-tree source-walk/AST gates **~190 s** re-walking the repo per test; (c) real `hermes_cli.main` CLI-child spawns where in-process seams exist (~100 s, `test_realm_sync.py` alone 82.5 s / 26 spawns) | ~5–7 min | §6 |
| 5 | **Windows Defender** on every non-excluded path: 2.3× file-churn slowdown same-drive, 2.9× on `%TEMP%` — where every per-test `tmp_path`/hermetic home lives | multiplies items 2 and the runner's per-file overhead | §5 |
| 6 | **Collection/import** — ~21 s per big directory serial; ~1.9 s/file (solo) to 4.2 s/file (8-way contended) interpreter+import+collect overhead on the parallel path | bounds the parallel win | §5, §6 |
| 7 | Gateway fence — **exonerated**: 0.17 µs/test arming, 10 µs/spawn classify | nil | §5 |

Ceiling check: the deliberate e2e/acceptance class plus genuinely-earned call
time is on the order of 8–10 minutes serial; everything above that is overhead
this plan attacks. The two independent levers compound: shaving the flat tax
helps the serial lane ~7–8 min; the parallel lane divides whatever remains by
an effective 3–6×.

---

## Non-goals

- **No deleting or skipping tests.** Every test keeps running on the lane it
  runs on today.
- **No blanket timeout cuts.** `--timeout=30` and the 20 s/180 s failure
  ceilings stay; they are hit only when something is already wrong.
- **No converting deliberate real-serve acceptances to fakes.** The e2e files
  (`test_gateway_peer_*_e2e.py`, `test_serve_socket_child_e2e.py`, the
  serve-lane WAIT suites) prove process-boundary claims and are untouchable.
- **No weakening the gateway fence** (measured cost is nanoscale; it defends a
  measured live escape) **and no weakening the live-system guard's guarantee**
  — Stage 1 moves its *cost*, never its refusal semantics.
- **No system-settings changes by the implementer.** Defender configuration is
  the operator's; this plan only carries a numbers-backed recommendation row.
- **No xdist.** The repo already ruled it out (`scripts/run_tests_parallel.py`
  docstring: persistent workers accumulate exactly the cross-file state the
  per-file design exists to kill). The rulings table asks the operator to
  close that question formally, not to reopen it.

---

## Stages (smallest first; each landable and testable on its own)

### Stage 1 — take the psutil process-table walk off the per-test path

**What:** in `tests/conftest.py::_live_system_guard`, stop calling
`psutil.Process(pid).children(recursive=True)` eagerly at every test's setup.
The snapshot only pre-approves children that existed *before* the test began —
almost always the empty set — and `_is_own_subtree()` already holds the
authoritative fallback (a live `psutil.Process(pid).parents()` walk at kill
time). Options, in preference order: (a) drop the eager snapshot and rely on
the live-walk fallback (it answers the same question, at kill time, when a
kill actually happens — rare); (b) compute the snapshot lazily on first
`os.kill` interception instead of at setup.

**Measurement justifying it:** 10.9 ms/test, ~2.1 min/suite; the walk is
`psutil_windows.ppid_map` scanning the whole process table 11,549 times (§2).

**Acceptance:** overhead probe (200 no-op tests, §2 method) shows
`_live_system_guard` setup ≤2 ms/test; `tests/test_live_system_guard.py` and
`tests/test_live_system_guard_self_test.py` green; the CI flake the snapshot
comment cites (`test_entire_tree_is_sigkilled_not_just_parent`) green ×10.

**Risks:** a test whose child was spawned by a *fixture* before guard setup
and killed by PID during the test would previously fast-path through the
static set and now takes the live `parents()` walk — same verdict, slightly
slower on that rare path. A behavior difference is only possible where psutil
cannot resolve the process chain (already handled: unresolvable ⇒ stale-PID
allow / foreign refuse, unchanged).

### Stage 2 — attribute and cure the AGING floor (28 ms fresh → 77 ms aged)

**What:** the biggest single number after parallelism. First task is the
missing attribution: embed the 200-no-op probe at the START and END of a full
`tests/hermes_cli` run and cProfile the aged tail in-situ (the §2 method, at
the other end of the process), splitting the ~49 ms of growth between (a) the
numbered tmp-dir scan (mechanism pinned: 0.46→5.8 ms/call as `basetemp`
fills, §6b-i), (b) guards that activate once their target modules import,
(c) anything else the profile names. Then fix what the profile convicts —
candidate mechanics, in order of likely yield: give the suite a
flat-namespace tmp strategy that doesn't rescan a filling directory
(pytest's `tmp_path` factory pointed at per-N-hundred-test subdirectories,
or a session-scoped counter that skips the scan); the Stage-1-style shaves
of `_hermetic_environment` (4.4 ms fresh: full-`os.environ` scan + 6
Defender-taxed `mkdir`s) and `_ensure_current_event_loop` (1.7 ms).

**Measurement justifying it:** §6b-i — no-op setup 77 ms mean / 670 ms max at
end-of-process vs 28 ms fresh; setup+teardown phases sum to ~13.4 min ≈ 41%
of the operator's 32:37. Even halving the aged floor returns ~5–6 min serial.

**Acceptance:** the start/end probe pair shows end-of-process setup within
1.5× of start-of-process; full `tests/hermes_cli` serial wall drops
accordingly with an identical pass/skip/xfail set to the §6b baseline
(4,623/100/1); the invariants at the top of `tests/conftest.py` still hold
(guard self-tests + `test_log_isolation.py` green).

**Risks:** low-to-medium — mechanical rewrites inside fixtures whose behavior
is pinned by existing tests, but tmp-dir strategy touches every test's
`tmp_path`; keep pytest's per-test-unique, auto-cleaned contract exactly.
The one semantic trap: the env sweep must keep scanning live `os.environ`
(credential vars set mid-session by leaky tests are its point) — cheaper,
never a session-start snapshot. Note the interaction with Stage 3: per-file
processes never age, so the parallel lane sidesteps most of this cost
structurally — Stage 2 is what keeps the SERIAL lane honest.

### Stage 3 — make the in-repo parallel runner the operator's full-suite lane

**What:** no new machinery — validate and document
`scripts/run_tests_parallel.py` (per-file subprocess isolation, 8 workers,
duration-balanced via its own `test_durations.json` cache) as the way this
machine runs "the full suite", replacing serial `python -m pytest <dirs>`.
The stage's work is the **safety re-verification**, because the July
precedent is disqualifying until re-measured: a 2026-07-26 full parallel run
produced ~338 spurious failures and destroyed a detached worktree, and a
2026-08-01 serial-but-unfenced run moved `main` via the updater tests. The
2026-08-31 gateway fence and the dcw-h2 test-isolation lane postdate all of
that; whether they closed it is a claim to prove, not assume.

**Measurement justifying it:** §5 — 82.8 s vs 131.0 s (1.58×) on the
smallest, worst-case scope (102 files); the ratio improves with scale as
per-file overhead amortizes and the duration cache balances shards. Serial
32:37 with 15 idle cores is the single largest number in the diagnosis.
Bonus that the small-scope measurement understates: per-file processes never
age, so the lane also sidesteps the §6b-i aging floor (77 ms → 28 ms per
test) structurally.

**Acceptance:** (1) parallel full-lane run (agent_runtime + hermes_cli + cli
+ state + integration) from a throwaway worktree has a failure set identical
to the serial baseline (expected: empty); (2) `git worktree list`,
`git branch -v` and the reflog on the primary repo are byte-identical before
and after; (3) no orphan `%TEMP%\hermes-agent-wt` registrations appear;
(4) wall time and per-file stats recorded in the field notes.

**Risks:** the July failure classes resurface — then the stage's deliverable
is the *list of offending files* (each is a real isolation bug by the repo's
own standards, filed as queue rows), not a forced landing. Run only from a
worktree until (2) has passed twice.

### Stage 4 — tune the parallel lane's throughput

**What:** with Stage 3's parity gate green, sweep the knobs the runner
already exposes: `HERMES_TEST_WORKERS` 8 → 12 → 16 (16 logical cores;
default caps at 8), and measure per-child pytest boot trims
(`-p no:cacheprovider`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD` with an explicit
`-p` list) against the measured 1.9 s solo / 4.2 s contended per-file
overhead across ~990 files.

**Measurement justifying it:** §5 — per-file overhead totals 542.8 s CPU-wall
on a 102-file scope whose serial test time was ~118 s; overhead, not test
time, dominates the parallel lane's cost today.

**Acceptance:** identical pass/fail set at every setting; the
wall-vs-workers curve and chosen default recorded in the field notes; runner
default updated only if the win is ≥15% and stable across two runs.

**Risks:** >8 workers deepens Defender/CPU contention (the 4.2 s number IS
that contention) — the sweep may conclude 8 is right; that is a valid
outcome. Disabling plugin autoload must keep `pytest-asyncio`/`pytest-timeout`
explicitly listed or every file fails on the addopts `--timeout` flag.

### Stage 5 — one walk for the source-walk gate class

**What:** the ~190 s class (§6: S27/S29/S46/S49/S50/S56 removal gates,
`test_stream_stale_first_routing`, `test_office_class_key_guard`,
`test_persona_roster_bypass_contract`, `test_tombstone_registry`'s per-test
`git` subprocesses, `test_hermes_home_env_gate`'s 9.2 s setup) re-parses the
same unchanged tree once per test. Provide ONE session-scoped fixture owning
the expensive raw material — a parsed-AST index over `git ls-files` output —
and port gate tests to consume it; their assertions stay untouched. Under the
per-file parallel runner the cache warms once per file, which is exactly the
sharing that exists today, minus the intra-file duplication.

**Measurement justifying it:** §6 class total ~190 s in agent_runtime alone;
`test_tombstone_registry.py` 65.1 s / 22 tests and
`test_stream_stale_first_routing.py` 26.2 s are single-file stragglers that
also stretch the parallel lane's critical path.

**Acceptance:** every ported gate still REDDENS on a planted violation
(sabotage round-trip per the house rule: apply the mutation, watch red,
revert, watch green — one planted case per ported file); class total drops
≥60% on the §6 re-measurement; no gate's enumeration source changes (the
fixture feeds it the same `git ls-files`/AST facts it derived itself).

**Risks:** the vault's gate rulings are strict ("enumerate from the thing
itself"; a walk that cannot see a `part`-equivalent is not an enumeration) —
the fixture must be a *materialization* of the same enumeration, not a second
authority; where a gate's walk differs (tombstone's git-history reads), it
keeps its own. This is why the stage is gated on a ruling row below.

### Stage 6 — reclassify CLI-child spawns that stand in for unit seams

**What:** audit the ~10 agent_runtime files using the
`_run_harness`-style pattern (real `python -m hermes_cli.main harness …`
child per assertion; §6 class 3), plus the same-shaped hermes_cli sinks the
§6b run ranked (`test_harness_characters_cli.py` 67.3 s,
`test_doctor.py` 57.8 s, `test_completion.py`'s 14.1 s bash-syntax spawn). For each call site, classify: (a) the claim
is process-boundary (exit code, argv parsing at the real entry, env
inheritance) → stays a real child; (b) the claim is handler wiring/output
shape → drive the parser+handler in-process, as the live-system guard's own
refusal message already instructs. Convert only class (b).

**Measurement justifying it:** `test_realm_sync.py` 82.5 s ≈ 26 spawns ×
~3 s; two spawns in one test cost 20.4 s under contention (§6). Warm
in-process equivalent is milliseconds.

**Acceptance:** per-file classification table lands in the field notes
BEFORE any conversion; converted tests red on the same planted defects as
before (sabotage round-trip); `test_realm_sync.py` wall drops ≥50%; every
call site classified (a) is left byte-identical.

**Risks:** misclassifying an acceptance as wiring — mitigated by the
table-first mechanic and the ruling row; the operator strikes any row they
consider a deliberate acceptance before conversion starts.

### Stage 7 — recommendation only: a Defender-excluded test-temp root

**What:** no code change lands without the ruling. If the operator adds a
real-time-scan exclusion for a dedicated, test-only directory (e.g.
`X:\test-tmp` — NEVER inside `X:\Eternia\.hermes`, which is the live store),
wire pytest's `--basetemp`/`TMP` for test runs at that root via an opt-in
documented in the runner, so every per-test `tmp_path` and hermetic
HERMES_HOME escapes the measured 2.3–2.9× Defender file-op tax.

**Measurement justifying it:** §5 churn table (0.32 s excluded vs 0.73 s
same-drive non-excluded vs 0.92 s `%TEMP%`); §2 (tmp_path + hermetic mkdirs
are per-test); the per-file runner's children also pay it on every import.

**Acceptance (post-ruling):** §5 churn benchmark re-run inside the new root
shows the excluded-class number; overhead probe and one full directory run
quantify the realized win; a misconfigured/missing exclusion degrades to
today's behavior, never to an error.

**Risks:** an exclusion root that overlaps anything non-test weakens the
machine's defense — the recommendation is a *dedicated* directory used by
nothing else; the operator owns the trade.

---

## Rulings needed (operator decisions — none pre-empted by this plan)

| # | question | trade | plan's recommendation, with numbers |
|---|---|---|---|
| R1 | **pytest-xdist**: adopt for parallelism? | speed vs the per-file isolation design (persistent xdist workers carry cross-file module state; the runner docstring records dropping it for exactly that) | **No.** The serve one-owner locks would NOT forbid it (per-root, §5) and the fence arms per-test — the objection is state leakage by design, not locks. The in-repo per-file runner is the sanctioned lane (Stage 3); formalize that and close the question. |
| R2 | **Defender exclusion** for a dedicated test-temp root (system setting — operator-only) | scan coverage on one throwaway directory vs a 2.3–2.9× tax on every test file-op (§5 churn table) | Recommend: create `X:\test-tmp`, exclude it, land Stage 7's opt-in wiring. Bounded win: minutes across serial and parallel lanes; zero effect on non-test paths. |
| R3 | **Parallel lane as the documented default** for full-suite runs on this machine | July 2026 precedent (338 spurious failures, destroyed worktree) vs 15 idle cores | Adopt AFTER Stage 3's parity + repo-integrity gates pass twice from a worktree. Until then serial stays the default. |
| R4 | **Source-walk gate caching** (Stage 5): may gate tests consume a shared session-scoped materialization of the same walk? | per-test independent enumeration vs ~190 s/run; vault ruling "enumerate from the thing itself" must not be diluted into a second authority | Allow, with the sabotage round-trip acceptance per ported file; gates whose walk is genuinely distinct (git-history reads) keep their own. |
| R5 | **CLI-child conversion list** (Stage 6): which `_run_harness`-class call sites are deliberate process-boundary acceptances? | test fidelity vs ~100 s/run | Classification table lands first; operator strikes rows; only unstruck class-(b) rows convert. |
| R6 | **Batching multiple small files per runner child** (possible Stage 4 extension): weaken per-FILE isolation to per-BATCH to amortize the 1.9–4.2 s/file overhead? | isolation granularity (the repo's chosen boundary) vs the single biggest parallel-lane cost | **Not recommended now** — take Stages 3–4 first and re-measure; only bring this back with data showing the remaining overhead still dominates. |

## Follow-ups this diagnosis surfaced (not perf, filed here for routing)

- `tests/acp/*` collection failure in worktrees (`No module named 'acp'`) is
  known/pre-existing (editable install resolves to the primary) — any Stage 3
  worktree lane needs the same `--ignore`/install answer CI uses.
- 3 further collection errors exist in fringe dirs under a full-tree
  `pytest tests/ --collect-only` (§6c) — outside the operator's lanes, noted
  for whoever owns those dirs.
