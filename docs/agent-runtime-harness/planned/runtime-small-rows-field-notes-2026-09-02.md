# Five runtime rows, filed 2026-09-02, closed on `fix/hermes-runtime-small-rows`

Worktree `X:/Eternia/_worktrees/w7-runtime`, cut from `origin/main` at
`10d0d3a41c`. Running record; the report is the commit message.

---

## §1 `migrate-home`'s skipped arm — PREMISE HELD, both halves

`_cmd_characters_migrate_home` carries a comment reading *"Every line names the
DIRECTORY beside the id or slug"*, and the skipped line printed
`{kind} {id-or-slug} {reason}`. The row is right twice over, because the receipt
could not have printed one: `migrate_characters_home._relocate` built its rows
as `{"kind": ..., "id"/"slug": ...}` and appended a `reason` to them. Only the
installed arm's *"no manifest: not an installed character"* skip carried a
`directory`, and that one was not printed either.

The case that makes it matter is not hypothetical. The live store holds an
id-collision pair — `<id>` and `<id>.backup-2026-08-25-nefix`, the SAME id inside
both `draft.json` files, already pinned by
`test_the_id_collision_pair_moves_as_two_entries_under_one_id`. When the library
already holds both leaves, both refusals list under one id, and the source
directory is the only field that tells the two rows apart. A refusal is also
the row an operator must ACT on: the entry is still sitting somewhere.

Fix: `_relocate` builds `left_where_it_is = {**row, "directory": str(child)}`
once and both its skip arms use it, so every skipped row in the receipt now
carries a directory (the installed arm already did); the CLI's skipped line
prints it. Moved rows already carried the source as `from`.

## §2 Three load-flaky tests — PREMISE HELD, and one of them had a second half

### `test_mcp_startup::test_prepare_agent_startup_backgrounds_blocking_mcp_for_chat`

`assert elapsed < 0.2` was a stopwatch reading of a claim the stopwatch cannot
make. Converted to events, not to a looser number: the stub sets `entered`,
blocks on a BOUNDED wait, and sets `left` after it. The main call returning
while `left` is unset is the backgrounding contract stated exactly, with no
clock in it, and an inline regression cannot produce that state.

Probed rather than argued (§7): rewriting `main.py`'s call site to
`_discover_mcp_tools_without_interactive_oauth()` inline reds it on
`assert not left.is_set()`. The bound on the stub's wait is what keeps that a
failure rather than a hang — the first probe, `_discover()` in place of
`thread.start()`, DEADLOCKS instead (`_discover`'s `finally` re-enters
`_mcp_discovery_lock`, which `start_background_mcp_discovery` still holds), so
it proves nothing; that is a property of the mutation, not of the test.

### `test_active_sessions::test_cross_process_acquire_claims_only_one_last_slot`

The rendezvous was already event-based (ready files, then a go file). What was
wrong was the SIZE of the bounds around it: 10 s is the same order as six fresh
interpreters starting and importing `hermes_cli.active_sessions` on a box
already running eight test workers. Two bounds measured that startup rather
than the lock — the parent's readiness wait, and each worker's own go-file wait,
which the parent cannot satisfy until the SLOWEST sibling arrives. All three
(plus `communicate`) now read one `_WORKER_SYNC_TIMEOUT`, and the readiness
failure names how many of six arrived.

**The second half, found by running it and not by reading it.** A first pass set
that constant to 120 s and the test then died under an 8-worker
`tests/hermes_cli` run at 103 s with a pytest-timeout thread dump instead of a
message. `pyproject.toml`'s addopts carry a repo-wide per-test `--timeout=30`:
**a sync bound above the per-test cap can never report.** Whatever it would have
said is replaced by a stack trace. So the test declares
`@pytest.mark.timeout(180)`, the pattern the repo already settled on for a test
whose honest cost exceeds the default (`tests/test_coverage_claims_resolve.py`,
`pytestmark = pytest.mark.timeout(180)`). Sizing the bound to fit inside 30 s
instead would have re-created the row.

### `test_relay_shared_metrics::test_cross_process_model_call_updates_are_transactional`

`join(timeout=15)` + `assert not process.is_alive()` against two `spawn`
interpreters is the same bound-too-tight, and it left two more holes: the
`multiprocessing.Barrier` had NO timeout, so a sibling that dies before the
rendezvous parks the survivor forever; and a failed assertion left the children
running, leaking into the rest of the run. Same `_TEST_TIMEOUT` marker, a
bounded barrier, a `finally` that reaps, and a message on the join assert.

## §3 `test_serve_drain_accounting.py`'s framing — PREMISE HELD

The words are in the TEST's docstring, not the module header: *"kept as a
live-fire check next to the deterministic pin above"*. Nothing in the file runs
a real serve — every test drives `serve_loop` through an injected pipe and sink,
which the module docstring already said. What the concurrency test actually
adds over the pin beside it is CONTENTION: eight pool workers finishing at once
against one counter, where the pin drives one request through a widened window.
Billing that as live fire promised a second, independent KIND of evidence that
was never there. Corrected in both places; the paragraphs the 2026-09-02
synchronisation fix wrote (the held precondition, the probed `0 == 8`) were
already accurate and are kept verbatim.

## §4 The reader-spelling census — SIX gates read, THREE were blind

Search: every test that scans `add_argument` with `ast`/`inspect` (three files),
plus every `... not in source` assertion mentioning `args` (two more), plus the
two the row had already checked. The table is in the commit message. Findings
worth keeping here:

* **The blindness runs in BOTH directions, and the two are not the same bug.**
  A *positive* gate ("every flag has a reader") that misses a spelling goes
  FALSE RED — that is `858c12c7a0`'s six live flags, loud and self-announcing.
  A *negative* gate ("this retired flag is read by nobody") that misses a
  spelling goes FALSE GREEN, silently, forever. `test_s26` and `test_s27` are
  the second kind, and each knew exactly ONE spelling: s26 matched
  `'getattr(args, "task_id"'`, s27 matched `"args.task_id"`. Each recognised
  whatever happened to be typed in its handler the day it was written, so
  `args.task_id` walked through s26 and `getattr(args, 'task_id')` walked
  through s27 — before `a3b48a06a2` existed. That commit added a third door to
  a wall that already had two.
* **A fourth gate nobody had named: the BAN itself.**
  `test_flag_binding_boundary::_is_args_read` recognises `args.X` and the
  `getattr` form, which are the spellings the readers REPLACED. It did not
  recognise `list_flag_or_absent(args, "x") or []` — the collapse re-committed
  one layer up, by a handler that had already been pointed at the right seam.
  That is the spelling the fix itself created and the one the next handler is
  most likely to reach for, because the reader is now the ordinary way to read
  a flag. No live instance today (grepped); fixed as a trap, which is what the
  whole module is.
* The cure is the same everywhere and it is not "add a spelling": reader names
  come off `flag_binding.__all__` and the live signatures, so a fourth reader
  is inside every census the moment it is written. A reader whose first two
  parameters are not `(args, name)` fails configuration rather than being
  counted at a position it does not have.
* The rule is stated ONCE for the pair that shares it —
  `tests/agent_runtime/namespace_reads.py`, alongside the existing
  `office_seed.py` / `persona_samples.py` helper convention — because "three
  handlers each re-derived the correct rule by hand" is the finding
  `a3b48a06a2` was about, and duplicating its census would have been that
  finding again.
* Both directions are pinned: a bare string constant is NOT a read. Crediting
  one would let a retirement note that NAMES a removed flag keep that flag's
  gate green — and `test_s26`'s own module docstring says `task_id` several
  times over, which would have made its gate unfalsifiable.

## §5 The `postinstall` shim fence — PREMISE HELD; the honest fence is symmetry

`_isolate_hermes_shim_dir` is a `monkeypatch.setattr` on
`path_setup._shim_install_dir`. It cannot reach a child process, and its own
docstring already says so. The question the row leaves open is what hermes
should do about it, given the launcher half (set `HOME`/`LOCALAPPDATA` in the
spawned child's env) is filed separately.

Reading the seam answers it: **the two platform arms already disagreed.**
Windows read `LOCALAPPDATA` from the environment and returned `None` when it was
absent — a real fence, with a `shim_dir_unresolved` receipt. POSIX read
`Path.home()`, which consults `HOME` and then falls back to the password
database. So on POSIX an absent `HOME` did not mean "no home stated", it meant
"whichever home this box's passwd file names" — and a spawner that cleared its
environment still got the operator's real `~/.local/bin`, with the run's
throwaway `HERMES_HOME` baked into a durable artifact on their PATH. Measured on
an operator's Mac; that is the incident the seam was cut for.

The fence is therefore not a new mechanism, it is the Windows arm's rule applied
to both: **the environment is the sole authority, and an absent variable is a
refusal.** Setting `HOME` in the child already worked (`Path.home()` reads it
first) — what did not work was UNSETTING it, which is exactly what a hermetic
spawner does. The note stops saying `LOCALAPPDATA` on a platform that has no
such variable and names the one that would have answered.

Both platform arms of `TestShimInstallDir` are now driven by `_IS_WINDOWS`
rather than skipped off the running OS, so the POSIX fence is proven on this
Windows box instead of being a skip that rots.

## §6 Two doc cites re-anchored, for the same reason as `eb1a2fe8e3`

The `harness.py` edit in §1 shifted everything below it by five lines, and Lane
A refused on `01-system-architecture.md:660` (`_cmd_characters_auto`) and
`07-observability.md:614` (`_usage_lane_detected`). Re-anchored, gate green,
zero stale waivers. This is now the second time a small `harness.py` edit has
moved `_cmd_characters_auto`'s cite in two days.

## §7 What the runs actually said

* Red-first on every claim: row 1 both halves (`KeyError: 'directory'`, then the
  skipped line missing the source path), row 5 five arms, row 4's stage42 census
  (`{'fields','root','watch'} <= set()`) and the ban's two new lines
  (`[1,2,3,4] == [1,2,3,4,5,6]`).
* Mutation gate, real run: 6 selected, 6 KILLED (3 pre-existing `path_setup`
  claims re-selected by symbol, plus this branch's 3).
* Lane A green after §6.
* The three converted tests re-run under an 8-worker 64-file load that includes
  the neighbours which previously timed them out: 626 passed, 0 failed. Three
  `dashboard_auth` files — untouched here, and already failing this way in the
  pre-change partial run — needed the runner's file retry.
* A full `tests/hermes_cli` run was started and KILLED at 25% (≈15 min, ~55 min
  projected). Its evidence would have been unsound anyway: the mutation gate's
  own rule — never share the worktree with a test run — applies to editing too,
  and this session was still writing files while it read them.
