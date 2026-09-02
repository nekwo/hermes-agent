# Hermes suite performance — field notes, 2026-09-01

Raw measurements behind `hermes-suite-perf.md`. Diagnose-only lane: nothing in
this note changed test code. All numbers from this Windows 10 workstation
(16 logical cores, Python 3.12.5, pytest 9.0.3) in the throwaway worktree
`X:\wt\perf-suite` at `cf9abaac4b` (main's tip at worktree creation).

## 0. Operator field data (the anchor, 2026-09-01)

- Full combined suite: **11,549 tests in ~32:37** (1,957 s) → **169 ms/test average**.
- `tests/agent_runtime` alone: ~7,262 tests.
- Sweep `tests/hermes_cli tests/cli tests/state tests/integration`:
  **5,196 passed in 20:50** (1,250 s) → **240 ms/test average**.
- Runner: plain `python -m pytest` — one process, one interpreter, serial.
- Known pre-existing, not the slowness: `tests/acp/*` fails to collect in
  worktrees (`No module named 'acp'`; editable install resolves to the primary).

## 1. Methodology caveats

- The worktree is a fresh checkout on `X:\wt` (NOT Defender-excluded — see §5),
  so first-touch costs include cold `.pyc` compilation and Defender scans of
  9,077 new files. Probe runs were repeated so warm numbers are quoted.
- Several long measurements overlapped each other and an 8-worker parallel-runner
  run on this box; contended numbers are flagged where they matter. Ratios and
  attributions are trustworthy; treat absolute walls of the two big serial runs
  as upper bounds.
- Every `pytest` invocation used `-p no:cacheprovider` where noted to avoid
  cross-run cache effects; otherwise defaults from `pyproject.toml`
  (`-m 'not integration' --timeout=30 --timeout-method=thread` — conftest flips
  method to `thread` on win32).

## 2. Fixed per-test overhead (the autouse fixture stack)

Probe: a throwaway file of **200 no-op tests** placed under `tests/` so the full
root-conftest autouse stack applies (probe file not committed).

- `python -m pytest tests/test_zzz_perf_probe.py -q -p no:cacheprovider`:
  **200 passed in 6.30 s** (repeat 6.64 s under cProfile). Subtracting ~0.7–1 s
  startup/collection ⇒ **~28 ms fixed overhead per test**.
- Same probe copied into `tests/hermes_cli/`, `tests/agent_runtime/`,
  `tests/tools/` (their directory conftests added): 6.61 s / 6.67 s / 6.69 s —
  **directory conftests add ~1.5 ms/test, negligible**.

cProfile attribution of the 5.56 s of fixture time in the 200-test run
(`_fillfixtures` cumulative), per test:

| autouse fixture (tests/conftest.py) | per test | × 11,549 tests |
|---|---|---|
| `_live_system_guard` (line 1319) | **16.7 ms** | **~3.2 min** |
| — of which `psutil` `children(recursive=True)` → `psutil_windows.ppid_map` | 10.9 ms | ~2.1 min |
| `_hermetic_environment` (line 622: env sweep + 6 mkdirs) | 4.4 ms | ~50 s |
| pytest `tmp_path` (numbered dir under %TEMP%) | 2.6 ms | ~30 s |
| `_ensure_current_event_loop` | 1.7 ms | ~19 s |
| everything else (webbrowser/keychain/audio/kanban/tui-state/tripwire…) | ~2.6 ms | ~30 s |
| **total** | **~28 ms** | **~5.4 min ≈ 16% of 32:37** |

Standalone microbenchmark corroborates the psutil line:
`psutil.Process().children(recursive=True)` × 20 = 296.5 ms → **14.8 ms/call**
(cost scales with the machine's process count — it walks the whole process
table via `ppid_map` on every call).

## 3. Where the remaining ~140 ms/test lives (tests/state + tests/cli serial run)

`python -m pytest tests/state tests/cli -q --durations=0 -p no:cacheprovider`
(contended — overlapped the agent_runtime run):

- **776 tests in 131.0 s** → 169 ms/test.
- Reported durations ≥5 ms sum to 118.0 s: **setup 38.5 s over 773 rows**
  (~50 ms/test — root autouse stack plus these dirs' real fixtures),
  **call 79.5 s over 279 rows**, teardown ~0. 1,272 rows <5 ms hidden
  (sum bounded by ~6 s).
- Call time is **concentrated, not smeared**: top-10 calls = 28.5 s (36%),
  top-50 = 51.8 s (65%), top-100 = 63.7 s (80%) of all call time.
- Top sinks are deliberate-looking multi-second tests: `test_worktree.py`
  prune-parallel 5.09 s, `test_write_lock_patience.py` (holds real multi-second
  locks) 3.29+3.03+2.00 s, surrogate-sanitization 3.61 s, etc.

## 4. Hang check — sleeps, polls, timeouts (static sweep)

- `time.sleep` literals across `tests/`: 88 hits in `tests/agent_runtime`,
  83 `tests/tools`, 46 `tests/gateway`, 30 `tests/agent`, 27 `tests/hermes_cli`,
  25 `tests/run_agent`, 22 `tests/cli`.
- Duration histogram (literal args): 80×0.01 s, 43×0.02 s, 54×0.05 s, 29×0.1 s,
  32×0.2 s, 27×0.5 s … then a thin tail: 7×1.0 s, 2×2.5 s, 2×5, 2×10, 2×20,
  7×30, 7×60, 1×600.
- Every ≥10 s literal inspected is a **kill-target child process or a
  "never responds" stand-in on a background thread** (e.g.
  `tests/hermes_cli/test_goals.py` spawns `python -c "time.sleep(30)"` as a
  victim; `tests/cli/test_slash_confirm_windows.py`'s `_blocking_input` sleeps
  30 s on a worker thread while the test resolves via the modal path;
  `tests/agent/test_bounded_response.py`'s HTTP stub stalls on purpose). The
  600 s is a string inside `test_run_tests_parallel.py`'s fixture program.
  **None is slept on the happy path of the test that owns it.**
- Poll helpers found use tight intervals (0.01 s in
  `test_harness_serve.py::_wait_for`; the serve e2e `_wait_for`s block on
  events). `WAIT = 20 s` / `BOOT_TIMEOUT_SECONDS = 180 s` are failure ceilings;
  with the suite-wide `--timeout=30` (thread method) a happy-path serve boot
  must and does come in far below it.
- **Conclusion: no evidence of systematic happy-path hangs.** The suspicion
  "tests waiting on sleeps/timeouts that shouldn't" is not supported at the
  32-minute scale; the cost is fixed overhead + concentrated real work.
- `--timeout-method=thread` spawns one timer thread per test — bounded cost,
  not measured separately (it is inside the 28 ms floor).

## 5. Windows suspects

### Defender (real-time protection: ON — confirmed via `Get-MpComputerStatus`)

Exclusion list not readable without admin (`Get-MpPreference` refused);
`X:\Eternia\.hermes` is excluded per operator. File-churn microbenchmark
(300 units of mkdir×3 + write-50-line-py + read + delete, interleaved 3×100):

| path | time / 300 units | vs excluded |
|---|---|---|
| `X:\Eternia\.hermes` (excluded) | 0.32 s | 1.0× |
| `X:\wt` (same drive, NOT excluded) | 0.73 s | **2.3×** |
| `%TEMP%` on C: (NOT excluded) | 0.92 s | **2.9×** |

Same-drive delta isolates Defender (not disk). Every per-test `tmp_path` and
hermetic HERMES_HOME lives under `%TEMP%` — the 2.6 ms tmp_path +
part of the 4.4 ms hermetic-env cost in §2 is roughly the Defender-taxed
number; an excluded temp base would cut those by ~2–3× (bounded win:
tens of seconds across the suite for the fixture part, more for tests that
churn many files in tmp).

### pytest-xdist

`pytest-xdist 3.8.0` **is installed**. But `scripts/run_tests_parallel.py`'s
docstring records the repo **deliberately dropped xdist**: persistent workers
accumulate cross-file module state — the exact leakage class the per-file
isolation design exists to kill. The gateway fence would work per-worker
(wrappers are process-global, arming is per-test), and the serve one-owner
lock is **per store-root** (`agent_runtime/serve_socket.py` §"the one-owner
lock"; `test_gateway_peer_two_roots_e2e.py:276` states per-root = no
contention), so hermetic per-test roots don't collide — the locks do NOT
forbid parallelism. The design ruling against xdist is state leakage, not locks.

### The canonical parallel runner (already in-repo)

`HERMES_TEST_PATHS="tests/state:tests/cli" python scripts/run_tests_parallel.py -q`:

- **102 files, 773 passed, 0 failed in 82.8 s with 8 workers** vs 131.0 s
  serial → only **1.58×**, because per-file subprocess CPU-wall totalled
  542.8 s: **~4.2 s/file overhead under 8-way contention**.
- Solo (uncontended) per-file overhead: `test_write_lock_patience.py` wall
  11.34 s vs pytest-internal 9.40 s → **~1.9 s/file** (interpreter + imports +
  collection + conftest).
- Warm import costs are small (`hermes_cli.main` 0.46 s, `hermes_state`
  0.43 s, pytest boot 0.38 s, bare python 0.04 s) — the 4.2 s figure is
  mostly contention (CPU + Defender on non-excluded paths) inflating each
  child's wall.
- Default workers: `min(cpu_count, 8)` = 8 on this 16-core box —
  **headroom exists** (HERMES_TEST_WORKERS).

### Gateway fence (tests/hermes_cli/_gateway_fence.py)

Measured directly — **exonerated**:
- per-test arming (`arm()`+`disarm()` via the autouse fixture): **0.17 µs/test**;
- `classify()` on a benign argv: **10 µs per actual spawn**.
It defends a real measured escape (an atexit-window gateway boot against the
operator's live store) at nanoscale cost. Do not touch it for performance.

## 6. Serial full-directory run: tests/agent_runtime

`python -m pytest tests/agent_runtime -q --durations=0 -p no:cacheprovider`,
fresh worktree, partially contended (see §1):

**7,263 passed, 2 skipped in 1,287.5 s (21:27)** → 177 ms/test.

Phase split (durations ≥5 ms; 11,298 rows <5 ms hidden, sum ≲23 s):

| phase | total | rows | shape |
|---|---|---|---|
| setup | **410 s (32%)** | 7,263 | **flat tax**: mean 57 ms, median 50 ms; top-1,000 rows only 130 s — NOT concentrated |
| call | **698 s (54%)** | 3,204 | **concentrated**: top-10 = 156 s, top-100 = 368 s (53%), top-400 = 503 s (72%) |
| teardown | 17 s | 28 | — |
| residual (collection, import, <5 ms rows) | ~162 s | — | full-tree `--collect-only` measured separately at 255.8 s for 33,075 tests (contended); hermes_cli+integration alone collects in 20.9 s |

The 50 ms setup median decomposes as ~28 ms root autouse stack (§2) +
~1.5 ms dir conftest + ~20 ms of the tests' own fixtures — a uniform floor,
which is why no single fixture shows up as a spike.

Call-time classes in the top of the ranking:

1. **Deliberate e2e serve/gateway-peer boots** (untouchable):
   `test_gateway_peer_cross_install_chat_e2e.py` 67.7 s,
   `test_gateway_peer_two_roots_e2e.py` 26.4 s,
   `test_serve_socket_child_e2e.py` 23.2 s — ≈117 s across 3 files.
2. **Whole-tree source-walk / AST / reachability gates**, each re-walking the
   repo inside its own test: `test_s27_snapshot_orphan_tree_removal.py` 27.6 s,
   `test_stream_stale_first_routing.py` 26.2 s, `test_s49…` 13.9 s,
   `test_s29…` 11.4 s, `test_s46…` 7.7 s, `test_s50…` 8.2 s, `test_s56…` ≥5.3 s,
   `test_persona_roster_bypass_contract.py` 5.7 s,
   `test_office_class_key_guard.py` 18.8 s, `test_tombstone_registry.py`
   **65.1 s over 22 tests** (~3 s/test — per-test `git` subprocess walks), plus
   `test_hermes_home_env_gate.py` with a **9.18 s setup** row. Class total
   conservatively **~190 s** in this directory alone.
3. **Real CLI-child spawns standing in for unit seams**:
   `test_realm_sync.py` **82.5 s** — its `_run_harness` helper spawns
   `python -m hermes_cli.main harness …` as a real subprocess at **26 call
   sites** (e.g. `test_cli_env_credential_fallback` 20.4 s for two spawns);
   ~10 files across the directory use the same pattern.
4. Everything else is genuinely quick: below the top ~400 rows, call durations
   drop under 0.5 s.

## 6b. Serial full-directory run: tests/hermes_cli

`python -m pytest tests/hermes_cli -q --durations=0 -p no:cacheprovider`,
**uncontended** (run alone after everything else finished):

- **4,623 passed, 100 skipped, 1 xfailed in 890.8 s (14:50)** → 193 ms/test.
- Phases (≥5 ms rows, sum 860 s): **setup 326 s / 4,629 rows (mean 70 ms)**,
  call 505 s / 2,174 rows, teardown 28 s / 2,818 rows.
- Call concentration again: top-10 = 98 s, top-50 = 208 s (41%),
  top-300 = 376 s (74%).
- Top files: `test_harness_characters_cli.py` 67.3 s (drives full CLI flows),
  `test_doctor.py` 57.8 s (18.3 s single test — vercel diagnostics),
  `test_web_server.py` 31.7 s, `test_urllib_security.py` 19.5 s,
  `test_harness_cli.py` 15.7 s, `test_completion.py` 14.5 s
  (14.1 s validating generated bash syntax — spawns bash).

### 6b-i. THE AGING FLOOR (found by accident, then pinned)

The 200-no-op probe file was still present in `tests/hermes_cli/` during this
full run (alphabetically last → ran near the end of the 4.7 k-test process):

- **probe setup mean 77 ms, max 670 ms** at end-of-process — vs **28 ms** in a
  fresh process (§2). The fixed per-test floor roughly **triples as the
  single pytest process ages**; call rows stayed <5 ms (it is pure overhead).
- Summing measured setup+teardown phases across the three serial runs:
  410 s (agent_runtime) + 354 s (hermes_cli) + ~39 s (state+cli) ≈
  **13.4 min — ~41% of the operator's 32:37 is fixture overhead**, not test
  bodies, once aging is included.
- One mechanism pinned by synthetic bench: numbered-dir creation in a
  filling directory (the shape of pytest's `make_numbered_dir` under
  `basetemp`) degrades **0.46 ms → 5.8 ms per call between 0 and 4,800
  entries** (17.7 s cumulative for 4,800; scan is O(entries) per call). Every
  test's `tmp_path` pays this, in a Defender-taxed `%TEMP%`.
- The remaining growth (~tens of ms) is unattributed — candidates: guards
  that become active once their target modules import
  (`_kanban_write_guard`, `_reset_tui_gateway_server_state` snapshot copies),
  environ growth, GC pressure. Needs an in-situ profile at the aged end of a
  long run (plan Stage 2's first task).

## 6c. Misc measured constants

- `subprocess.run([sys.executable, "-c", "pass"])` on this box: **33 ms** warm —
  bare child spawn is cheap; children that IMPORT the hermes tree cost
  ~0.5 s+ (warm) each, multi-second cold/contended.
- Full-tree `pytest tests/ --collect-only` (acp ignored): **33,075 tests,
  255.8 s** (contended; 3 further collection errors in fringe dirs). The
  operator's 11,549-test run is therefore a directory subset
  (agent_runtime + the 20:50 sweep dirs ≈ 12.4 k collected, minus
  deselects/skips), NOT `pytest tests/`.

## 7. Structural facts that shape the plan

- Plain `python -m pytest` = ONE process, serial, no isolation subprocesses.
  The per-file isolation the conftest documents exists **only** through
  `scripts/run_tests_parallel.py`.
- 590 test files in `tests/hermes_cli`, 401 in `tests/agent_runtime`;
  ~97 files across `tests/` spawn `sys.executable` children.
- pytest-timeout floor: `--timeout=30 --timeout-method=thread` from addopts —
  the per-test hang ceiling that makes the 180 s constants unreachable.
- Known-destructive precedents (memory, 2026-07/08): unfenced
  `tests/hermes_cli` in the PRIMARY checkout ran gateway-updater tests that
  `git branch -f main origin/main` (2026-08-01 incident); a July full parallel
  run produced ~338 spurious failures and destroyed a detached worktree.
  The 2026-08-31 fence and the dcw-h2 test-isolation lane postdate those; the
  plan treats "parallel run is clean now" as a claim to re-verify, not assume.

---

# IMPLEMENTATION LOG — 2026-09-01, same session (operator go-ahead on all stages)

New precondition, verified before anything else: **the operator excluded
`X:\Eternia` in Defender.** Churn re-bench: `X:\Eternia` 0.25 s vs `X:\wt`
0.57 s vs `%TEMP%` 0.76 s per 300 units — the exclusion is live. Work moved to
worktree `X:\Eternia\_worktrees\perf-h` (branch `perf-hermes-suite`, rebased;
the plan-docs commit had already been landed on main as `a426ea3494` by
another session).

## 8. Stage 1 — psutil walk off the per-test path (LANDED)

Eager `children(recursive=True)` in `_live_system_guard` → lazy memoized
capture at first guarded kill. Semantics: the snapshot only fast-paths PIDs
the live `parents()` walk (still the authority, unchanged) would allow anyway;
lazy capture is a superset of eager among still-alive PIDs and contains no
foreign PID, so refusals are identical.

* 200-no-op probe, fresh process: **6.30 s → 2.65 s** (~28 ms → ~8 ms/test)
  — this includes Stage 2's tmp fix; the psutil share alone was 10.9 ms.
* `test_live_system_guard.py` + `test_live_system_guard_self_test.py`:
  identical result set before/after my change (2 failed, 40 passed,
  1 skipped) — the 2 failures are PRE-EXISTING Windows environment gaps
  (`termios`/pty does not exist on win32; `WinError 87`), proven by running
  the stashed baseline.

## 9. Stage 2 — the aging floor (LANDED)

Attribution completed with pytest's REAL allocator:
`_pytest.pathlib.make_numbered_dir` in a filling root costs
**2.69 ms (fresh) → 12.51 ms (4.2 k entries) → 23.94 ms (11 k entries),
150.8 s cumulative for 11,000 calls** in `%TEMP%`. That is the dominant
attributed component of the 28→77 ms aged floor (§6b-i).

Fix: `tests/conftest.py` now overrides `tmp_path` with an O(1)
counter-allocated directory under the session basetemp (same contract:
unique, empty, test-named, kept for the session — matching default
`tmp_path_retention_policy="all"`). Also: credential env-var predicate
compiled to one regex. `tmp_path_factory` deliberately not overridden.

## 10. Stage 7 — excluded test-tmp root (LANDED as opt-in wiring)

`HERMES_TEST_TMP_ROOT` (env, opt-in, unset in CI): relocates the session
HERMES_HOME sandbox AND pytest's basetemp into the named root, one
`hermes-pytest-<pid>` subdir per process (concurrent runs and the parallel
runner's children never share), stale run dirs pruned best-effort after 24 h.
Verified: probe run with the root set lands all dirs under
`X:\Eternia\.test-tmp\hermes-pytest-<pid>\`, 200 probe tests in 2.37 s.
Recommended operator config: `HERMES_TEST_TMP_ROOT=X:\Eternia\.test-tmp`.

## 11. Stage 5 — source-walk gates share one parse (LANDED)

`tests/agent_runtime/_tree_index.py`: process-lifetime memoization of file
text, parsed AST (keyed on path + decode-errors mode) and git query lines.
NOT an enumeration authority — every gate keeps its own walk/glob/skip-list;
only I/O and parsing are shared. Ported: s27 (+`lru_cache` on its derived
surface, callers verified read-only), s29, s46, s49, s50, s56,
stream-stale-first (its `_repo_python_sources` deliberately left UNCACHED —
it enumerates with `git ls-files --others`, and its anti-vacuity test plants
an ignored file and re-enumerates, which a cache would make vacuous),
persona-roster, hermes-home-env-gate. Tombstone registry NOT ported (already
internally cached, pinned by its own tests); office-class-key reclassified to
Stage 6 (its cost was CLI children, not walks).

* Same-command measurement, all 9 ported files, identical conditions:
  **125.92 s (baseline via stash) → 70.45 s** — 44% off the wall, more off
  the call time (the wall includes ~10 s of pytest boot both sides).
* **Sabotage round-trips, one per ported file, all red for the RIGHT
  reason and all reverted clean** (`git status` empty after each):
  planted orphan in `snapshot.py` → s27 AND s29 red; function-local
  `from agent_runtime import operator_control` / `launcher_process_hygiene`
  in production files → s49 / s50 red (note: a MODULE-level plant breaks
  collection instead — wrong-reason failure, rejected); planted
  `def apply_pending` → s46 red; planted dead `RuntimeConfig` field → s56
  red naming the field; planted undeclared `require_known_persona` call →
  roster red; planted `os.environ.get("HERMES_HEAD_HOME")` → env-gate red;
  planted `stream_frames` call → stream gate red.

## 12. Stage 6 — CLI-child spawns (CONVERTED — R5 strike pass RULED: upheld, no strikes)

**R5 status: RULED 2026-09-01 by delegated fresh-context review (operator's
delegation), all 37 sites upheld, zero struck.** The reviewer read every
converted call site's assertions against the seam: each claims verb behavior
only — exit code, stdout envelope, store effect under the hermetic home —
and none claims the process boundary (no signal, child-lifetime, fresh-import
or stderr-content assertion anywhere; the two env-credential sites claim the
fallback LOGIC, which reads `os.environ` at handler time identically on both
paths). The seam is production end-to-end — `dispatch_argv` builds the same
`hermes_cli.harness.build_parser` tree `main.py` registers and maps errors
through the same `emit_harness_error`/`ERROR_EXIT_CODES` — and non-vacuity
holds structurally (nearly every site `json.loads(proc.stdout)`, so an
undispatched call reds on empty stdout) on top of the recorded sabotage
round-trip below. Both files re-verified green post-ruling: 89 passed.
The one-helper-per-file revert mechanic below remains available if a future
test genuinely needs the boundary.

The seam: `hermes_cli.harness_parts.serve.dispatch_argv` — production's own
in-process dispatcher ("exactly as `hermes <argv…>` would, including the
harness error-envelope contract"), wrapped by
`tests/agent_runtime/_harness_cli.run_harness_in_process` (argv needs the
`harness` prefix; found by the first red run). Classification:

| file / claim | sites | class | disposition |
|---|---|---|---|
| `test_realm_sync.py` — verb behavior: envelope shape, exit code, store effects | 25 in 15 tests | (b) wiring | CONVERTED (helper swapped; call sites untouched) |
| `test_office_class_key_guard.py` — refusal/override verb behavior | 12 in 12 tests | (b) wiring | CONVERTED (same) |
| `tests/hermes_cli/test_harness_characters_cli.py` (67.3 s) — "the full QA flow runs through the CLI" | — | (a) deliberate CLI acceptance | LEFT byte-identical |
| `tests/hermes_cli/test_doctor.py` (57.8 s; 18.3 s single test) — profiled solo: 7.1 s inside `run_doctor` = its own late imports (~5.7 s) + probe-thread joins (~3.0 s); the test is already in-process and its subject IS real diagnostics | — | (a) | LEFT |
| `tests/hermes_cli/test_completion.py` (14.1 s) — subject is the generated bash script's validity, proven by real bash | — | (a) | LEFT |

* Both converted files: **89 passed in 32.84 s** (baseline: ~101 s of
  reported ≥5 ms rows alone, contended).
* Seam sabotage: `code = 0` without dispatching → immediate reds (assertions
  depend on the dispatched work, not just exit codes); reverted, re-run green.
* No converted site asserts stderr CONTENT (only concatenated into failure
  messages) — the known logging-handler capture gap cannot flip a verdict.

## 13. Stage 7 collision + resolution (same day)

A second session (`hermes-agent-6b`) independently landed Stage 7 on main
(`98d43d0c86`) as a conftest-import-time redirect of TMP/TEMP/TMPDIR +
`tempfile.tempdir` into a fresh `run-*` dir under `HERMES_TEST_TMP_ROOT`
(same env var), plus `tests/test_tmp_root_optin.py`. That design SUBSUMES
this branch's basetemp wiring (pytest derives basetemp from
`tempfile.gettempdir()`, so the redirect moves it too, per-process), so the
rebase resolved the conftest overlap in main's favor and dropped the
basetemp/pytest_configure variant. Kept from this branch: the O(1) counter
`tmp_path`, the credential regex, the lazy psutil walk. Verified merged:
their 12 Stage 7 tests green; probe lands its dirs under
`X:\Eternia	est-tmp
un-*`. Coordination closed by message — that session
confirmed it is building no further stages.

Peer also claimed canon `08-performance-and-debt-ledger.md` carries the
32:37 wall / aging-floor numbers needing re-truing — **checked, does not
verify**: no such rows exist in the canon docs on this tree. Nothing to
re-true.

## 14. Full-lane verification (quiet, hands-off reruns)

Method note: the FIRST verification pair was invalidated twice over — the
serial agent_runtime run crashed at 76% when the original process-lifetime
AST cache (785 MB retained, measured) pushed a 13 s gate over the 30 s
pytest-timeout ceiling, and the hermes_cli timing was contaminated by
benchmarks I ran during its window. Both lessons recorded; the runs below
are post-fix and untouched.

Serial, `HERMES_TEST_TMP_ROOT` set, identical result sets to baseline:

| lane | baseline (§6/§6b) | after | result set |
|---|---|---|---|
| `tests/agent_runtime` | 1,287.5 s (21:27), 7,263/2 | **999.3 s (16:39)**, 7,296/2 (+33 tests from main's advance) | ✓ pass/skip identical modulo new tests |
| `tests/hermes_cli` | 890.8 s (14:50), 4,623/100/1 | **707.7 s (11:47)** | ✓ identical (4,623/100/1) |
| setup phase | 410 s / 326 s | **150 s / 186 s** | — |
| aged-floor probe (end of hermes_cli run) | 77 ms mean / 670 max | **36 ms mean / 580 max** | fresh floor is ~8 ms |

Converted/ported sinks, same runs: `test_realm_sync.py` 82.5→23.7 s,
`test_office_class_key_guard.py` 18.8→2.8 s, `test_hermes_home_env_gate.py`
→2.8 s, s27 27.6→16.7 s. Per-gate comparisons are NOT SHA-clean (main
advanced under the rebase and several walks scan a bigger tree; s29/s46/s50
read higher than their contended baselines) — the honest deltas are the lane
totals and the setup phase.

**OPEN (filed as a plan follow-up): residual aging.** 36 ms aged vs ~8 ms
fresh misses the ≤1.5× acceptance. The numbered-dir scan is gone (tmp_path
is O(1) now); prime remaining suspect is gen-2 GC cost scaling with the
imported-module heap as the process ages. Unattributed — needs an in-situ
profile at the aged tail before any further fix.

Parallel (Stage 3), lane `agent_runtime:hermes_cli:cli:state`, from this
worktree, tmp root set:

* **Pass 1 (8 workers): 1,089 files, 12,492 passed, 0 failed, 1,096.2 s
  (18:16).** The July-2026 destructive-run failure class did NOT reproduce.
  Repo integrity: worktree list and all branch refs byte-identical before/
  after except `main`, which advanced by ANOTHER session's landings —
  verified fast-forward ancestry, not a reset; no new `%TEMP%`
  hermes-agent-wt registrations.
* Per-file overhead now dominates the parallel lane: CPU-wall 8,681 s for
  ~1,700 s of serial test time ⇒ ~6.4 s/file under 8-way contention
  (the runner docstring's ~250 ms assumption is a Linux-era number).
* **Pass 2 (R3's second pass, doubling as the Stage 4 probe):**

* **Pass 2, 12-worker Stage 4 probe first: WORSE.** 1,435.5 s (23:55),
  12,484 passed / **2 failed**, CPU-wall 14,887 s — oversubscription past 8
  workers inflates per-file overhead faster than the extra workers divide it.
  The 2 failures (`test_serve_rpc_office_subscribe_live_hub.py`'s re-baseline
  pair) are the KNOWN load-sensitivity class (mc-hygiene precedent: compare
  SETS with serial confirmation) — serially confirmed green (25 passed,
  17.46 s) immediately after. **Stage 4 verdict: 8 workers stands; the
  12-worker config is rejected on both wall and stability.**
* **Pass 2 at the recommended 8 workers (R3's second pass): 1,089 files,
  12,494 passed, 0 failed, 1,056.2 s (17:36)**, CPU-wall 8,396.5 s. Repo
  integrity across both passes: worktree list and branch refs show ONLY
  additions made by other, disjoint sessions (two new branches/worktrees);
  nothing removed, no ref reset, `main` untouched, no stray
  `%TEMP%\hermes-agent-wt` registrations.
* **R3 outcome: parity + integrity verified twice at 8 workers (18:16 and
  17:36, 0 failed both). The parallel lane is RECOMMENDED as the full-suite
  default on this machine; the actual doc/default flip is left to the
  operator, per the ruling.** Serial same-lane ≈ 30 min → parallel ≈ 17:36
  (1.7×); the remaining gap is per-file overhead (~6 s/file under 8-way,
  measured both passes), which is the R6/batching conversation the plan
  deliberately deferred.
* **R3 RULED 2026-09-01 by delegated fresh-context review: the parallel
  runner (8 workers) IS the documented full-suite default on this machine;
  serial pytest is demoted to the exception** (single-file debugging, and
  the integration/e2e/docker lanes the runner deliberately skips). The two
  passes above satisfy the ruling condition as written, and the runner's
  code was verified to provide the documented guarantees (per-file fresh
  interpreter, loud FLAKY policy, 1-worker straggler retry, nothing-ran
  guard). **12 workers stays REJECTED** — slower (23:55) and 2 load-flaked
  failures, per the Pass-2 probe. Doc home: `AGENTS.md` §Testing (amended
  same commit); `run_tests.sh` now forwards `HERMES_TEST_TMP_ROOT` through
  its `env -i` so the Stage 7 tmp root survives the canonical entry point.

## 15. Residual aging ATTRIBUTED (2026-09-01 evening, probe worktree aging-h, measure-only)

Method: full serial tests/hermes_cli (minus test_doctor.py — its 18 s vercel test
crosses the 30 s thread-method ceiling under load and the thread method kills the
whole process) with the 200-no-op probe collected last; four uncommitted
instruments: exact per-phase durations via a logreport-hook plugin (pytest's
--durations output quantizes to 10 ms), a gc.callbacks pause meter over the probe
window, an unprofiled per-fixture setup timer, and an in-process micro-lab at the
aged tail. Fresh floor this day/box: setup 14.5 ms median (not 8 — box drift; all
comparisons same-session). Aged tail: setup 45.2 ms / teardown 8.2 ms median.
The growth is NOT gen-2-GC-dominated and NOT tmp_path (O(1) fix holds: 0.43 ms aged).

| mechanism | fresh | aged | delta /test |
|---|---|---|---|
| _sys_modules_identity_is_restored setup half (tests/hermes_cli/conftest.py:179) | 0.42 ms | 29.9 ms | +29.5 — DOMINANT (~65%) |
| same guard's teardown half (2nd O(N) loop) + GC landing there | ~1.5 | ~8.2 | most of teardown +6.7 |
| GC pauses (direct meter): gen0 0.29->4.1 ms/pass x182/window; one gen2 pass 611 ms aged vs 27 fresh | 0.33 | 5-8 | owns the 500-2000 ms SPIKES, not the mean |
| _live_system_guard | 4.4 | 10.1 | +5.7 (undecomposed) |
| _hermetic_environment (environ 111->199) | 2.0 | 3.5 | +1.5 |
| _ensure_current_event_loop | 1.5 | 2.2 | +0.7 |
| pytest-timeout timer thread; capture tmpfile | — | 0.25 / 0.31 ms | acquitted (micro-lab) |

Cross-check: the aged per-fixture table sums to 46.9 ms ≈ the 45.2 ms measured floor.

- The guard scales with sys.modules (626 fresh -> 14,843 aged; growth happens DURING
  the run, not at collection). Aged probe window: 27.7M function calls vs 3.6M fresh
  for identical tests. A synthetic dict-only replica of its loops at 14.8k entries
  costs 5.4 ms — the other ~24 ms is hasattr(parent, child) firing module
  __getattr__ lazy-import machinery on real modules (failed import: 2.04 ms aged vs
  0.73 fresh; the no-op probe tests stat 120 files/test via importlib _path_stat).
- gc.freeze() after collection, matched uncontended buckets (tests 3000-4000):
  setup 31.5 vs 36.6, teardown 5.7 vs 6.6 -> recovers ~6 ms/test and suppresses the
  gen-2 spike class; does NOT touch the guard. Walls 797 s (freeze) vs 712 s
  (no freeze) — no whole-run win. Second-order, not the fix.
- Heap inflation alone (842 production modules pre-imported into a fresh process):
  14.5->20.1 ms — a fat heap reproduces only a fraction; the run itself is required.
- Aged-process hygiene at the 4.6k-test tail: 31 live threads (fresh 3), 10,900
  open handles (fresh 663), RSS 646 MB.
- Contention trap re-validated: identical run 18:29 contended vs 11:52 uncontended —
  never compare walls across sessions.

Recommended fix (decision-ready): keep the identity guarantee, retire the per-test
O(sys.modules) module-object work — run the parent-binding repair once per session
(or only when len(sys.modules) changed), probe via vars(parent) membership so module
__getattr__ never fires, keep the before-copy + teardown identity loop (dict ops,
~1-5 ms). Expected: aged floor 45->~20 ms, ~60-90 s off every full hermes_cli run,
<=1.5x aging acceptance passes. gc.freeze optional second-order (~40-60 s/run) at the
price of never-collected frozen garbage. OPEN: _live_system_guard's +5.7 ms aged
growth is measured, undecomposed.

## 16. Residual aging CURED, and the doctor-binding row re-measured (2026-09-02, worktree w1-aging)

Two rows off the launcher's Mission Control queue, worked together because the
second one's evidence lives in this directory's fixtures too.

**Load caveat, and it is large.** The box was contended throughout — other
sessions were running suites on the same 16 cores — so every number below is
comparable ONLY against the numbers beside it, taken the same session, and NONE
of them against §15's. The size of the tax is visible in the fresh/aged pair:
this session's fresh floor reproduces §15's almost exactly (15.3 ms vs 14.5),
while the aged floor is roughly double (92.7 ms vs 45.2). The aging MECHANISM is
the same one; what doubled is the price of every page fault and every GC pass
under contention — exactly §15's own warning ("identical run 18:29 contended vs
11:52 uncontended — never compare walls across sessions").

### 16a. The aging floor: the guard keeps its guarantee and loses its walk

§15 attributed ~65% of the aged setup floor to the SETUP half of
`_sys_modules_identity_is_restored` and wrote a decision-ready fix. Landed as
written, with one addition the prescription left open.

`tests/hermes_cli/_module_identity.py::ParentBindingRepair` now owns the
parent-binding repair the fixture used to run inline:

- **`child in vars(parent)`, never `hasattr(parent, child)`** — the ~24 ms half.
  `hasattr` fires PEP 562 module `__getattr__` and the lazy-import machinery
  behind it; `vars()` reads `__dict__` and cannot. The verdict is identical for
  the only names considered: ones whose module object is already in
  `sys.modules`, so nothing is left to lazily load.
- **The walk is gated on `len(sys.modules)` having changed**, and when it does
  run it skips every name already bound — the other ~5 ms. Binding a child
  changes no `sys.modules` row, so a test that imports nothing new leaves the
  mapping in exactly the state the last pass fixed.
- **The addition:** a name is remembered as done only after its parent RESOLVED.
  A child whose parent is not imported yet stays unmarked, so the pass after the
  parent's own import reconsiders it. Without that, the count gate would have
  been a hole rather than a free skip.

`before = sys.modules.copy()` and the teardown restore loop are untouched — §15
measured them cheap, and they are the identity guarantee itself.

**Numbers.** 200-no-op probe collected last; serial `tests/hermes_cli` minus
`test_doctor.py` (§15's method); `--timeout=600` because the 30 s default's
thread method killed the whole process at `test_config_loader_e2e.py` under
load; phase durations read off `report.duration`, which is unquantized, rather
than `--durations`, which rounds to 10 ms.

| probe phase (median, ms) | fresh process | aged tail, BEFORE |
|---|---|---|
| setup | 15.34 | 92.74 |
| teardown | 1.38 | 24.30 |

Aged setup mean / p90 / max BEFORE: 115.66 / 166.44 / 2329.23. Wall **31:48**;
1 failed / 4624 passed / 100 skipped / 1 xfailed. The single red is
`test_harness_flag_and_control_reachability.py::test_every_harness_flag_has_a_reader`
and it is **pre-existing at `b9e7a27988`, not this lane's**: that gate's scan
lane is `hermes_cli/harness.py` + `harness_support.py` + `harness_parts/*.py`,
none of which this branch touches, and it names six `harness.py` flags with no
reader (`persona_instance_return.proof_ids` / `.artifact_refs`,
`roots_migrate.configs` / `.root`, `workspace_create.agent`,
`skills_delete_cmd.realms`) — the flag-binding migration's territory.

**The whole-run AFTER arm could not be taken comparably, and is OWED.** Two
attempts: the first was killed at 93% by this session's own background-task
cleanup (before the probe file, which is collected last, had run); on the second
the box saturated — `wmic cpu get loadpercentage` = 100 with 24 python processes
from other sessions — and the same command that took 31:48 an hour earlier was
on pace for three hours. A wall measured under 4x the contention is not a
comparison, it is a different experiment, which is §15's own rule. So the AFTER
side is carried by two cheaper measurements that ARE load-robust, plus the
attribution §15 already established:

*The mechanism, interleaved A/B in one hermetic process* (the two algorithms
alternate on the same mapping, so drift cancels; 15 reps, medians), real
`sys.modules` inflated to **2,810** by importing this repo's own tree:

| per call | median | min | max |
|---|---|---|---|
| OLD `hasattr` walk | 3.479 ms | 2.834 | 4.765 |
| NEW, nothing imported since the last pass | 0.003 ms | 0.002 | 0.007 |
| NEW, one module HAS landed (it walks) | 0.393 ms | — | 0.902 |

*The fixture, bracketed A/B* — the 200-no-op probe file run alone against a
`sys.modules` aged to **2,957** by a session-start inflation plugin, so the
guard sees a realistic mapping without a 30-minute suite. Arms alternated and
each side run twice to bracket the load:

| arm | probe setup median (ms) |
|---|---|
| NEW (this branch) | 18.44, 19.62 |
| OLD (pre-fix walk planted back) | 25.23, 23.51 |

**~5.3 ms/test at 2,957 modules.** The real tail is 14,843 — five times that
mapping — and §15 measured the same term at 29.9 ms there, which is what this
scales to. Note what the inflation lab CANNOT show: §15 established that a fat
heap alone reproduces only a fraction of the total aging (14.5 -> 20.1 ms), so
these numbers are the GUARD's contribution, not the floor's, and the floor's
own AFTER number remains owed.

**The guarantee, proven both ways.** Both pollution classes the guard's
docstring names, re-run against the new code in one process in the order that
reproduces them:

- `test_skills_subparser.py` then files that patch `hermes_cli.main`: identity
  restored on all three spellings — the `sys.modules` row, the `main` attribute
  on the `hermes_cli` package, and `_build_web_ui.__globals__`. 29 passed / 1
  skipped over `test_update_venv_health.py` + `test_web_ui_build.py`.
- `test_dashboard_admin_endpoints.py` then
  `test_doctor.py::TestHonchoDoctorConfigDetection`, one process, that order:
  **39 passed**. Counterfactual — same command, repair call commented out:
  **1 failed**, and it is the documented failure verbatim, `AttributeError:
  'module' object at plugins.memory.honcho has no attribute 'honcho'`.

New tests: `tests/hermes_cli/test_module_identity_guard.py` (8). Red-first
against the pre-fix walk planted back into `run()`: **3 failed, 5 passed** — the
`__getattr__` probe, the count-gate skip and "only the new name is examined" go
red; the five that assert the GUARANTEE are green both ways, which is the shape
a refactor's test set should have.

### 16b. `doctor.HERMES_HOME`: the binding was real, its consequence was not

The row read: *`doctor.HERMES_HOME` is bound at module import, so the suite
resolves a tool out of the operator's LIVE profile, and `_gateway_fence.py`
carries an exemption for exactly that.* **The first clause is true. The second
does not follow, and the exemption is not deletable by fixing the first.**

Verified on this host at `b9e7a27988`:

- `hermes_cli/doctor.py` did bind `HERMES_HOME = get_hermes_home()` at module
  scope, and under a bare `python -m pytest` in the operator's shell that
  resolves at COLLECTION time to `X:\Eternia\.hermes` — the live store — because
  `_apply_profile_override()` is gated off outside a hermes entrypoint and the
  hermetic-home fixture only runs afterwards. Printed from a probe test whose
  module-level import captured it: `doctor.HERMES_HOME (module-import-bound):
  X:\Eternia\.hermes`.
- But **none of doctor's three profile-relative browser candidates**
  (`<home>/node/bin`, `<home>/node`, `<home>/node_modules/.bin`) exists under
  that root, so the binding resolves nothing. The
  `X:\Eternia\.hermes\profiles\alice\node\agent-browser.CMD --version` spawn the
  fence exempts comes from `shutil.which("agent-browser")` — the operator's
  **PATH**, which `scripts/run_tests.sh` forwards verbatim (`PATH="$PATH"`) and
  which no profile redirection can reach.
- Measured after the binding moved to call time: deleting the exemption still
  reds **19 tests in `test_doctor.py` alone**, and the refusal names that same
  PATH-resolved CMD.

The binding was fixed on its own merits, which are real and were already
documented in the tree by the test that had to work around it
(`TestGitHubTokenCheck._isolate_home`): `setenv("HERMES_HOME")` alone left
doctor probing the operator's `~/.hermes`, where a large real `state.db` made
`PRAGMA integrity_check` blow a 300 s per-file budget. Importing the module also
loaded the operator's real `.env` into the test process. `run_doctor` now
resolves the home AND that `.env` at call time; `hermes_cli/doctor.py`'s entry
in `tests/test_no_frozen_hermes_home.py` shrinks from
`{HERMES_HOME, _DHH, _env_path}` to `{_DHH}` (a display LABEL, never a
filesystem read). 31 `monkeypatch.setattr(doctor_mod, "HERMES_HOME", ...)` sites
across 4 test files became `monkeypatch.setenv`.

Red-first: the two new `test_doctor.py::TestDoctorResolvesTheHomeAtCallTime`
tests fail 2/2 with the import-time binding planted back, pass 2/2 with it gone;
they assert on a file doctor must have READ (a planted skills-hub `lock.json`
count), not on the printed home, so patching the label cannot fake them.
Mutation claim `doctor-home-is-read-at-call-time-not-at-import` anchors
`run_doctor/HERMES_HOME`. The four migrated files run **85 passed / 3 skipped**.

**The fence exemption was NARROWED, not deleted.** It was spelled "any argv
without a hermes entry point", which let every non-hermes command through — an
`npm --prefix <real root> install`, a `node <real root>/...` — for the sake of
one read-only `--version`. It is now spelled as the probe itself:
`_is_agent_browser_version_probe` (exactly two tokens, an `agent-browser`
basename, `--version`). Four new parametrized cases pin the tightening and fail
4/4 against the old spelling; the fence file runs 33 passed.

### 16c. Two things this lane found and did not fix

- **The fence's real-store arm is inert under `scripts/run_tests.sh`.**
  `_real_hermes_root()` reads `get_default_hermes_root()`, which is env-derived;
  `run_tests.sh` execs `env -i` without forwarding `HERMES_HOME`, so
  `tests/conftest.py` has already installed a session temp home in the env by
  the time the fence computes `_REAL_ROOT`. Measured: `_REAL_ROOT` came back as
  `...\Temp\hermes-test-home-0p7v68i_`, and `classify()` of the live
  `profiles/alice` agent-browser CMD returned `None`. The arm defends the
  operator's store only on the bare-`pytest` path — the path AGENTS.md calls the
  exception. No fix exists inside the fence: under `env -i` this box's actual
  store root (`X:\Eternia\.hermes`, a custom deployment, not the platform
  default `%LOCALAPPDATA%\hermes`) is unknowable from the environment the runner
  hands the process.
- **The remaining half of the agent-browser row is a TEST-side seam**, not a
  production binding. The suite runs doctor's real resolver, and production is
  entitled to consult PATH. `agent_browser_runnable`'s per-path memoisation
  already cut the spawns to one per process; killing the last one means stubbing
  the resolver for the tests that only want doctor's report.
