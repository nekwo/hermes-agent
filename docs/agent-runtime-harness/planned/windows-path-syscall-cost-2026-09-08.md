# Field notes — where a snapshot build's wall clock goes on Windows (2026-09-08)

**Status:** the first term is FIXED (`hermes_constants.get_default_hermes_root`
is memoised); the rest are measured and rowed, not fixed. **Owning doc:**
[07 — Observability](../07-observability.md) for the receipts,
[08 — Performance and debt ledger](../08-performance-and-debt-ledger.md) for the
numbers. **Origin:** the operator reported this Windows install feels slower than
their Mac install of the same runtime.

## Method — stated, because a perf number without one is a rumour

Two lanes, both taken on the operator's Windows box (16 logical processors) while
their agents were running, so every figure is a LOADED figure and the paired A/B
below is the only claim made about it.

1. **Read-only, against the live store.** A probe that stubs every write
   primitive (`open` in a writing mode, `mkdir`, `makedirs`, `remove`, `unlink`,
   `rmdir`, `replace`, `rename`) and RECORDS any attempt before the hermes import,
   so the run proves rather than asserts that it did not touch operator state. It
   reported zero attempts. Used for `core_cache.build_input_fingerprint` and for
   the `agents_readiness` walk.
2. **Against an ISOLATED COPY**, for anything that builds. `HERMES_AGENT_RUNTIME_ROOT`
   pointed at a copy of the live store named `agent-runtime-probe-perf` with
   `HERMES_REQUIRE_ISOLATED_ROOT=1`, so `agent_runtime.resolution.assert_probe_isolation`
   hard-fails the run before any store I/O if it would resolve the live root. Used
   for `snapshot.build_snapshot`.

Section attribution is the runtime's OWN instrumentation — `sections_ms`,
`snapshot._log_agents_readiness_split`, and the `snapshot_core_cache_write`
restat fields — never a parallel one.

## The leading hypothesis was HALF right, and the half that was wrong is the useful half

The hypothesis was: the build stats thousands of paths, Windows charges far more
per path than APFS, therefore the fingerprint walk is the gap.

**The fingerprint walk is not the gap.** `build_input_fingerprint` over the live
store declares 4,428 inputs and costs **382 ms cold, 77–88 ms warm** — about 3% of
a 2.5 s build. It is already written to avoid the expensive call: `_walk_tree`
takes its triples off `os.DirEntry`, which on Windows is populated by the
directory enumeration itself, so 2,320 store-root entries cost 20–40 ms while
3,518 bare `os.stat` calls over the same files cost 106 ms (**30 us/path**).

**The per-path cost is real, and it lands somewhere else entirely.** One warm
uncoalesced `build_snapshot` against the isolated copy, with `os.stat`,
`os.scandir` and `realpath` counted and attributed to their call sites:

| syscall | calls per build | time |
|---|---|---|
| `realpath` (`nt._getfinalpathname`) | 1,231 | 197 ms |
| `os.stat` | 3,035 | 159 ms |
| `os.scandir` | 902 | 22 ms |

**~378 ms of a 714 ms instrumented build — 53% — is Windows path syscalls.**
`_getfinalpathname` is the one that separates the platforms: it OPENS the path to
ask the filesystem for its canonical name (~71 us here), where a POSIX
`realpath(3)` is a few microseconds. That is the Windows/macOS gap, and it is a
CONSTANT multiplier on a call count — which makes the call count, not the
filesystem, the thing to fix.

## The call count, attributed

`realpath`, by originating site:

| calls | site |
|---|---|
| 394 | `hermes_constants.get_default_hermes_root` <- `hermes_cli.profiles._get_default_hermes_home` <- `_get_profiles_root` |
| 158 | `get_default_hermes_root` <- `agent_runtime.config.harness_root_config_path` <- `load_root_runtime_config` |
| 160 x3 | `agent.skill_utils._resolved_path` <- `record` / `skill_source_kind` <- `resolve_skills` |
| 36 | `get_default_hermes_root` <- `hermes_constants.get_shared_skills_dir` <- `skill_utils.get_all_skills_dirs` |
| 32 | `get_default_hermes_root` <- `agent_runtime.machine_roots.machine_roots_registry_paths` |
| 22 | `get_default_hermes_root` <- `harness_root_config_path` <- `mission_chat_workdir` |
| 20 | `get_default_hermes_root` <- `get_shared_skills_dir` <- `skill_install.harness_skill_destination` |

**662 of 1,231 — 54% — are one function**, `get_default_hermes_root`, which also
contributed 394 of the build's `os.stat` calls. Every one of those 662 asked about
the same two paths and got the same answer.

`os.stat`, by originating site (top four): 698 `skill_utils.is_skill_support_path`
<- `_skill_root_registry` <- `resolve_skills`; 394 `get_default_hermes_root`; 247
`parse_cache._stamp` <- `_cached_skill_frontmatter`; 243
`hermes_cli.venv_integrity._metadata_dirs_by_distribution` <- `metadata_issues` <-
`venv_integrity_issues`.

## What was fixed, and the paired measurement

`get_default_hermes_root` is memoised on `(HERMES_HOME, platform default)`. The
reasoning, the key's second component and the one staleness window it admits are
written at `hermes_constants._DEFAULT_HERMES_ROOT_CACHE`; three tests in
`tests/test_hermes_constants.py::TestGetDefaultHermesRootMemo` pin it.

Paired A/B, same process shape, same store, alternated back to back by stashing
only `hermes_constants.py` — nine warm uncoalesced builds per run, two runs each:

| | median | min |
|---|---|---|
| before (`origin/main` at `3c3a5631c8`) | **608.8 ms** / 635.1 ms | 595 ms |
| after (memo) | **411.0 ms** / 411.3 ms | 400 ms |

**-32.5% and -35.2%** — roughly 200 ms off every warm build, from one dict. The
build is not incidentally faster; it stopped asking Windows the same question 662
times.

Two other candidates were sized the same way before any code was written, and
both were REFUSED as fixes on the number: memoising
`hermes_cli.runtime_environment.runtime_environment_status` (which re-scans
site-packages once per persona per build) bought **7 ms**, and disabling the
core-cache write-back entirely bought **4 ms** on this store. They are rowed
below on their structure, not on their cost.

## What the measurement did NOT explain, and is rowed rather than guessed

1. **The live serve's `agents_readiness` walk costs 5-10x the same walk in an
   isolated process.** `snapshot_agents_readiness walk_ms=` reads 651–1595 ms in
   the operator's `agent.log` (664 samples), against **126–155 ms** for the
   identical five-persona walk measured read-only against the same live store in a
   quiet process. The 2026-08-22 note at `agent_runtime/profile_readiness.py`
   records 146 ms and matches the quiet figure, so the walk's algorithm is not
   what moved — something about the serve process is. Contention, GC, or a cache
   invalidated by the store's own churn are all candidates; none is measured.

2. **The core cache is pure cost on this install, and it is not the fingerprint
   walk's fault.** Today's `agent.log` holds **2,094 `snapshot_core_cache_write`
   lines and ZERO `core_source=cache` serves** — the only 42 `snapshot_core_cache`
   lines in the file are `never_converged`. On the isolated COPY, where nothing
   churns, the same code SERVES the persisted core (226–241 ms against a
   1.0–2.2 s cold build), which is direct evidence that convergence — not the
   cache's design — is the whole blocker. See
   [`core-cache-input-closure.md`](core-cache-input-closure.md), whose IC-4 gate
   says "measure first"; this is part of that measurement.

3. **`agent/skill_utils` spends 480 `realpath` + 698 `os.stat` calls per build**
   in `_resolved_path` and `is_skill_support_path`, both reached from
   `resolve_skills`. Upstream-owned file; not touched here, per the fork's
   leave-upstream-alone rule.

4. **`venv_integrity_issues` re-enumerates site-packages once per persona per
   build** — 243 `os.stat` calls answering a question about the interpreter's
   venv, which cannot change between two builds two seconds apart and is identical
   for all five personas (`site_package_dirs()` reads `sysconfig`, which is
   process-global and not profile-scoped). Small in milliseconds after the memo
   above; structurally a per-persona loop around a process-global fact.

5. **`sections_top=` prints three sections.** Every remedy plan that reads a
   build's attribution off `agent.log` is reading a truncated list, which is how
   `prompt_observability` — the section that ties `agents_readiness` for the top
   slot — went unnamed by the boot-window work. The full `sections_ms` map exists;
   the receipt shows a third of it.

## Gate

Any further work here moves `snapshot_build_core … build_ms=` on a real operator
boot, read from the live log across at least three builds — not `elapsed_ms` or
`waited_ms`, which measure a caller's wait, and not a synthetic benchmark. The
fix above is reported on a paired A/B against a copy precisely because it makes no
claim about a live boot yet.
