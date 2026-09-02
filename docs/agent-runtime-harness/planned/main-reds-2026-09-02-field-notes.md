# `main` reds, 2026-09-02 — field notes

Running record for the branch `fix/main-reds-flag-readers-drain-accounting`, cut
from `origin/main` at `a4f8e62af7` in the linked worktree
`X:\Eternia\_worktrees\w3-mainreds`. Two rows were handed over: one confirmed
red, one "reported failing today, green for the next person to look".

Both turned out to be the SAME kind of finding — a gate measuring the right
thing through an assumption that had quietly stopped holding — and in neither
case was production wrong. Nothing under `hermes_cli/` or `agent_runtime/`
changed on this branch.

## 1. The flag-reader census, and the third spelling

### The premise, checked

`tests/hermes_cli/test_harness_flag_and_control_reachability.py::test_every_harness_flag_has_a_reader`
was red at `a4f8e62af7` (`1 failed, 2 passed`, reproduced here before anything
was touched) naming six flags:

| flag | registered | reader today |
|---|---|---|
| `persona_instance_return.proof_ids` | `harness.py:1274` | `harness_parts/persona_commands.py:5316` |
| `persona_instance_return.artifact_refs` | `harness.py:1275` | `harness_parts/persona_commands.py:5317` |
| `roots_migrate.configs` | `harness.py:314` | `harness.py:2213` |
| `roots_migrate.root` | `harness.py:315` | `harness.py:2220` |
| `workspace_create.agent` | `harness.py:475` | `harness.py:3042`, `harness.py:3052` |
| `skills_delete_cmd.realms` | `harness.py:819` | `harness.py:2706` |

The attribution handed over — the flag-binding migration `a3b48a06a2` — is
CORRECT, and it is case (b) of the three offered, for all six: the migration
removed no reader and killed no flag. `git show a3b48a06a2` on those lines is
six one-line respellings, every one of them a rename of the reader and nothing
else:

```
-    config_paths = _machine_root_config_paths(list(getattr(args, "configs", []) or []))
+    config_paths = _machine_root_config_paths(list_flag_or_empty(args, "configs"))
-    for item in getattr(args, "root", []) or []:
+    for item in list_flag_or_empty(args, "root"):
-    for value in getattr(args, "realms", None) or []:
+    for value in list_flag_or_empty(args, "realms"):
-        ... len(args.agent or []) ...
+        ... len(list_flag_or_empty(args, "agent")) ...
-    agent_ids = list(args.agent or [])
+    agent_ids = list_flag_or_empty(args, "agent")
-            proof_ids=list(getattr(args, "proof_ids", []) or []),
-            artifact_refs=list(getattr(args, "artifact_refs", []) or []),
+            proof_ids=list_flag_or_empty(args, "proof_ids"),
+            artifact_refs=list_flag_or_empty(args, "artifact_refs"),
```

So: no flag deleted, no CLI contract change, `scripts/dump_cli_contract.py
--check` untouched and green, and **no launcher fixture re-sync is owed by this
branch**. All six flags are live and each still changes what its verb does.

### The class the census missed

The gate's reader census read two spellings — `args.<dest>` (an `ast.Attribute`)
and the `getattr(args, "<dest>")` string form — and deliberately excluded bare
string constants, for a good reason it states itself: counting them would let a
retirement note that NAMES a removed flag keep that flag's gate green.

`hermes_cli/flag_binding.py` invented a third spelling that is neither. The dest
is a **string argument in the `name` slot of a declared reader**
(`list_flag_or_empty(args, "proof_ids")`), and the `getattr` that eventually
runs is one frame down inside `flag_binding._raw`, against a variable. Nothing
was removed; six working flags simply became invisible to the census, which then
reported them as advertised-and-unreachable — the loudest possible accusation
this gate can make, about six flags that work.

The fix is in the census, not in the parser and not in the handlers. The
admitted class is narrow on purpose:

- only calls to functions `hermes_cli.flag_binding.__all__` actually exports,
  read from the live module rather than listed by hand in the test — a
  hand-maintained list of spellings is precisely what produced this red;
- the argument index comes from the live signature, and a reader that is not
  `(args, name, ...)` fails configuration in the census instead of being counted
  at a position it does not have;
- a reader call whose flag name is computed rather than literal is REPORTED in
  the failure message rather than silently skipped (there are none on the lane
  today — scanned);
- a bare string constant anywhere else still counts for nothing, so the
  vacuous-gate hole the original docstring names stays shut.

Both directions are now asserted in the file
(`test_the_census_credits_a_flag_binding_reader_and_still_ignores_a_bare_string`,
`test_the_census_reports_a_reader_whose_flag_name_it_cannot_resolve`), and the
whole-tree behaviour was sabotage-checked: rewriting `harness.py:2706` from
`for value in list_flag_or_empty(args, "realms"):` to `for value in []:` reds
the gate again on exactly `harness.py:819 skills_delete_cmd.realms`, so the
widened census has not been widened into a gate that always says yes.

### The standing lesson

This is the second time in three days that the flag-binding work has been read
as having broken something it did not. It re-spelled twenty-five reads at once,
and every census in this repo keyed on a reader SPELLING is a candidate to have
gone blind in the same commit. The one checked here was the harness reachability
gate; `test_flag_binding_boundary.py` already resolves the same call shape (its
`_flags_read_as_absent_or_given`) and is green, and the two now agree on how a
reader is recognised.

## 2. Serve drain accounting — the numbers, then the mechanism

### The runs

`tests/agent_runtime/test_serve_drain_accounting.py` was handed over with two
tests reported failing earlier today —
`test_the_completion_count_matches_the_exits_under_concurrency[8]`
(`assert 5 == 8`, reported serially) and
`test_a_drain_the_reader_outran_is_declared_abandoned_in_a_frame`
(reported parallel-only) — and green in the orchestrator's own two-file run.

Measured here, on this loaded box (another agent was running suites throughout),
at `a4f8e62af7` with production untouched:

| lane | command | runs | result |
|---|---|---|---|
| parallel | `HERMES_PYTHON=C:/Python312/python.exe scripts/run_tests.sh tests/agent_runtime/test_serve_drain_accounting.py` | 5 | 9 passed each; 8.7 / 8.1 / 8.3 / 7.9 / 7.9 s |
| serial | `C:/Python312/python.exe -m pytest tests/agent_runtime/test_serve_drain_accounting.py -q` (the sanctioned single-file exception) | 3 | 9 passed each; 4.19 / 4.07 / 4.23 s |
| serial, the two named tests only | `... -q -k "concurrency or abandoned"` | 20 | 0 failures |

**28 runs, 0 failures.** The reds were not reproduced by repetition.

### What repetition could not do, a probe did

Repetition being unable to catch a race is not evidence there is none, so the
mechanism was gone after directly. `requests_completed` counts what completed
DURING the drain: `_run` reads the enclosing `drain_state` in its `finally` and
a `None` there means no drain was open, so the request is not counted — while
its `exit` frame is emitted regardless.

`test_the_completion_count_matches_the_exits_under_concurrency` asserts
`requests_completed == len(exits)`, where `exits` counts every exit frame ever
emitted. That is an invariant ONLY while all eight requests are still in flight
when the drain op is parsed, and the test bought that with a 50ms sleep in the
dispatch and the hope that the reader thread reached the drain line first.

A probe (scratch, not committed) that lets all eight land before sending the
drain op — which is exactly what a loaded box can do to the reader thread on its
own — makes the same assertion measure:

```
requests_completed = 0  exits = 8
```

deterministically. **The reviewer's `5 == 8` is that race caught part-way**: three
of the eight had already exited when the drain op was read. It is not FINDING B
returning. It wears FINDING B's numbers, which is why it reads as a regression —
`5 reported for 8 exits` is quoted verbatim in the production comment at
`hermes_cli/harness_parts/serve.py:2324` describing the bug that was FIXED, and
the deterministic pin for it
(`tests/agent_runtime/test_serve_drain_accounting.py:408`,
`test_the_completion_count_cannot_miss_a_request_that_just_landed`) passes every
time.

So: **the serve's completion accounting did not regress.** The pop and the
increment are in one critical section (`serve.py:2310-2332`), the request is put
into `inflight` by the reader thread BEFORE `pool.submit` (`serve.py:4638`), and
neither window the reviewer's shape suggests is open. The test raced the pool.

### The two synchronisation fixes

Test-only, event-based, no new sleeps.

**`..._matches_the_exits_under_concurrency`** — the precondition is now HELD
instead of raced for. Every worker parks in the dispatch on a
`threading.Barrier(concurrency + 1)`; the main thread crosses it knowing all
eight requests are in `inflight`, sends the drain, waits for the `draining`
frame (published after the drain state is installed — `serve.py:4278-4300`), and
only then releases the workers. Every completion is now guaranteed to be
counted, with no wall clock either side. `len(exits) == concurrency` is asserted
too, so the test cannot pass by having lost requests.

Sabotage-checked: making `_DrainState.note_completed` stop incrementing past 5
reds it again — with `5 == 8`, the reviewer's literal numbers, which is the
defect this test exists for.

**`..._is_declared_abandoned_in_a_frame`** — the first attempt here was WRONG and
is recorded because it is the more useful half. Waiting for the request's `exit`
frame before sending `shutdown` looks like the same event-based cure and is not:
the request must still be IN FLIGHT when the shutdown arrives, or the pending
set is already empty and the loop ends the drain cleanly instead of abandoning
it. Tried, `no 'drain_abandoned' frame within 20.0s`, reverted. The dispatch
sleep is load-bearing.

What was actually raced is the GRACE. `_DRAIN_ABANDON_GRACE_SECONDS` was
monkeypatched to 0.2s, and `requests_completed == 1` needs the 50ms dispatch to
finish inside it — a 200ms wall-clock bound, well under the flake policy's floor
of 2s for one, and the plausible cause of the parallel-only red. The grace is
now 2.0s and the monitor's poll goes from 5.0s to 60.0s with it, so widening the
wait cannot hand the race to the monitor and turn a `drain_abandoned` assertion
into a `drain_complete`. Cost: the file goes from ~4.2s to ~5.9s serially.

## 3. What is not done

- The concurrency test's flake was never observed by this session, only proved
  by probe. If the row wants a caught instance, it is not here.
- No production change on this branch, so no mutation claim was added and
  `changed_line_mutation_check.py` selects nothing from this diff. Stated rather
  than skipped.
